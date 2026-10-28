"""WorkHub Mail phase 1: a same-machine, signed hub-to-hub mailbox.

One hub (a repository's AIWorkHub, identified by its router ``repo_id``) mails
another through a spool under ``<home>/.aiworkhub/mail/<hub_id>/`` with the
boxes ``inbox``, ``outbox``, ``archive`` and ``quarantine``.  Every envelope is
``aiworkhub.hub_mail.v1``, signed with HMAC-SHA256 over its canonical JSON by a
machine-local key that only processes of this user on this machine can read.

Mail is DATA.  Nothing here executes, applies or forwards mail content, and
every mail handed back to a caller carries ``untrusted_data: true``.

Fail closed throughout: a refused send names its reason and writes nothing; an
inbox file that fails any check is moved to ``quarantine`` with a sidecar reason
JSON and counted, never silently dropped.  The sender only ever creates files
in the recipient's spool -- it never lists, edits or deletes anything there.

Phase 2 (``hub_mail_net``) carries the same envelopes between machines: mail
to or from a paired peer's hub is signed with that peer's secret instead of
the machine key, and a sender hub no key belongs to is ``unknown_sender``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__, shared_router
from .worker_workspace import _redact_validation_secrets

SCHEMA_ID = "aiworkhub.hub_mail.v1"
KINDS = ("problem_report", "status_receipt", "note")
STATUSES = ("accepted", "rejected", "needs_info", "fixed_in")
BOXES = ("inbox", "outbox", "archive", "quarantine")
MAX_SUBJECT_CHARS = 300
MAX_BODY_BYTES = 64 * 1024
MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 256 * 1024
MAX_ATTACHMENT_NAME_CHARS = 255
MAX_VERSION_CHARS = 64
# Body, every attachment at its cap, and generous JSON/escaping headroom.
MAX_ENVELOPE_BYTES = 8 * 1024 * 1024
DEFAULT_TTL = timedelta(days=30)
KEY_BYTES = 32
MAX_LIST_LIMIT = 200
MAX_SCAN_FILES = 1000
MAX_LOCAL_HUBS = 256
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_MESSAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_HUB_ID_RE = re.compile(r"^repo_[a-f0-9]{32}$")
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
# What a crafted spool file can still raise past validation (RecursionError:
# JSON nested past the parser's depth limit); it quarantines that one file
# as ``envelope_unprocessable`` instead of failing the read.
_UNPROCESSABLE = (TypeError, ValueError, KeyError, AttributeError, RecursionError)
_ENVELOPE_KEYS = frozenset(
    {
        "schema_id", "message_id", "thread_id", "in_reply_to", "from_hub", "to_hub",
        "kind", "status", "fixed_in", "subject", "body", "attachments",
        "created_at", "expires_at", "signature",
    }
)


class HubMailError(RuntimeError):
    """A named, fail-closed refusal; ``str(exc)`` is the stable reason."""


def mail_root(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".aiworkhub" / "mail"


def key_path(home: Path | None = None) -> Path:
    return mail_root(home) / ".keys" / "local.key"


def box_dir(hub_id: str, box: str, home: Path | None = None) -> Path:
    if not _HUB_ID_RE.fullmatch(str(hub_id or "")) or box not in BOXES:
        raise HubMailError("hub_mail_box_invalid")
    return mail_root(home) / hub_id / box


def load_or_create_key(home: Path | None = None) -> bytes:
    """The machine-local signing key, created exactly once with O_EXCL and 0600.

    A fresh key goes to a fsynced 0600 tmp file (O_EXCL) that is published with
    ``_publish_new``: a crash never leaves an empty ``local.key``, and when two
    first calls race the loser reads the winner's key.
    """

    path = key_path(home)
    if not path.exists():
        _publish_new(path, secrets.token_bytes(KEY_BYTES))
    try:
        with path.open("rb") as handle:
            key = handle.read(KEY_BYTES + 1)
    except OSError as exc:
        raise HubMailError(f"mail_key_unreadable:{type(exc).__name__}") from exc
    if len(key) != KEY_BYTES:
        raise HubMailError("mail_key_invalid")
    return key


def canonical_bytes(envelope: dict[str, Any]) -> bytes:
    """Canonical JSON of the envelope WITHOUT its signature: what is signed."""

    return envelope_bytes({k: v for k, v in envelope.items() if k != "signature"})


def sign(envelope: dict[str, Any], key: bytes) -> str:
    return hmac.new(key, canonical_bytes(envelope), hashlib.sha256).hexdigest()


def envelope_bytes(envelope: dict[str, Any]) -> bytes:
    """The exact on-disk bytes of a signed envelope."""

    return json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TIME_FORMAT)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utf8_encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _write_tmp(path: Path, data: bytes) -> Path:
    """Write ``data`` to a fresh, fsynced 0600 tmp file beside ``path``."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to a tmp file in the same directory, then os.replace it."""

    temporary = _write_tmp(path, data)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_new(target: Path, data: bytes) -> bool:
    """Create ``target`` holding ``data`` complete, never over an existing file.

    The bytes go to a fsynced tmp file that is hard-linked into place, so the
    target either appears whole or not at all; ``False`` means it already
    existed and was left untouched.
    """

    temporary = _write_tmp(target, data)
    try:
        os.link(temporary, target)
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


def validate_envelope(envelope: Any) -> str:
    """The first structural/size reason ``envelope`` is invalid, or ``""``.

    Signature, recipient and expiry are checked separately because they need
    the key, the local hub and the clock.
    """

    if not isinstance(envelope, dict):
        return "envelope_not_object"
    if envelope.get("schema_id") != SCHEMA_ID:
        return "schema_mismatch"
    if set(envelope) != _ENVELOPE_KEYS:
        return "envelope_fields_invalid"
    # A lone surrogate (json.loads accepts "\ud800") cannot be UTF-8 encoded;
    # refusing it here makes every later encode() of this envelope safe.
    try:
        canonical_bytes(envelope)
    except (UnicodeEncodeError, TypeError, ValueError):
        return "encoding_invalid"
    message_id = envelope["message_id"]
    if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(message_id):
        return "message_id_invalid"
    thread_id = envelope["thread_id"]
    if not isinstance(thread_id, str) or not _MESSAGE_ID_RE.fullmatch(thread_id):
        return "thread_id_invalid"
    in_reply_to = envelope["in_reply_to"]
    if in_reply_to is None:
        if thread_id != message_id:
            return "thread_id_without_parent"
    elif not isinstance(in_reply_to, str) or not _MESSAGE_ID_RE.fullmatch(in_reply_to):
        return "in_reply_to_invalid"
    from_hub = envelope["from_hub"]
    if (
        not isinstance(from_hub, dict)
        or set(from_hub) != {"hub_id", "repo_root", "aiworkhub_version"}
        or not _HUB_ID_RE.fullmatch(str(from_hub.get("hub_id") or ""))
        or not all(isinstance(from_hub[k], str) for k in from_hub)
    ):
        return "from_hub_invalid"
    to_hub = envelope["to_hub"]
    if (
        not isinstance(to_hub, dict)
        or set(to_hub) != {"hub_id"}
        or not _HUB_ID_RE.fullmatch(str(to_hub.get("hub_id") or ""))
    ):
        return "to_hub_invalid"
    kind = envelope["kind"]
    if kind not in KINDS:
        return "kind_unknown"
    status, fixed_in = envelope["status"], envelope["fixed_in"]
    if kind == "status_receipt":
        if status not in STATUSES:
            return "status_invalid"
    elif status is not None:
        return "status_not_allowed_for_kind"
    if status == "fixed_in":
        if not isinstance(fixed_in, str) or not fixed_in.strip() or len(fixed_in) > MAX_VERSION_CHARS:
            return "fixed_in_version_required"
    elif fixed_in is not None:
        return "fixed_in_without_fixed_in_status"
    subject, body = envelope["subject"], envelope["body"]
    if not isinstance(subject, str) or not subject.strip():
        return "subject_required"
    if len(subject) > MAX_SUBJECT_CHARS:
        return "subject_too_long"
    if not isinstance(body, str):
        return "body_invalid"
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        return "body_too_large"
    attachments = envelope["attachments"]
    if not isinstance(attachments, list):
        return "attachments_invalid"
    if len(attachments) > MAX_ATTACHMENTS:
        return "too_many_attachments"
    for item in attachments:
        if not isinstance(item, dict) or set(item) != {"name", "sha256", "text"}:
            return "attachment_invalid"
        name, text = item["name"], item["text"]
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_ATTACHMENT_NAME_CHARS:
            return "attachment_name_invalid"
        if not isinstance(text, str):
            return "attachment_invalid"
        if len(text.encode("utf-8")) > MAX_ATTACHMENT_BYTES:
            return "attachment_too_large"
        if item["sha256"] != _sha256_text(text):
            return "attachment_sha256_mismatch"
    created, expires = _parse_time(envelope["created_at"]), _parse_time(envelope["expires_at"])
    if created is None or expires is None or expires <= created:
        return "timestamps_invalid"
    signature = envelope["signature"]
    if not isinstance(signature, str) or not _SIGNATURE_RE.fullmatch(signature):
        return "signature_invalid"
    return ""


def check_envelope(
    envelope: Any, *, key: bytes | Keyring, local_hub_id: str, now: datetime | None = None,
    check_expiry: bool = True,
) -> str:
    """Every read-side check; ``""`` when the mail is valid for this hub.

    With a :class:`Keyring` the signing key is picked by ``from_hub.hub_id``:
    a local hub signs with the machine key (or the peer's secret when it
    writes to a peer hub), a peer hub with that peer's secret, and any other
    sender is ``unknown_sender``.  Raw ``bytes`` check against that one key.
    """

    reason = validate_envelope(envelope)
    if reason:
        return reason
    signing_key = key.key_for(envelope) if isinstance(key, Keyring) else key
    if signing_key is None:
        return "unknown_sender"
    if not hmac.compare_digest(str(envelope["signature"]), sign(envelope, signing_key)):
        return "bad_signature"
    if envelope["to_hub"]["hub_id"] != local_hub_id:
        return "wrong_to_hub"
    expires = _parse_time(envelope["expires_at"])
    if check_expiry and expires is not None and expires <= _now(now):
        return "expired"
    return ""


def local_hub(root: Path) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """This repository's hub identity from its own manifest, or a refusal."""

    hub_id = shared_router._read_manifest_repo_id(root)
    if not _HUB_ID_RE.fullmatch(hub_id):
        return None, {"ok": False, "error": "local_hub_identity_unavailable"}
    return {"hub_id": hub_id, "repo_root": str(root), "aiworkhub_version": __version__}, None


def local_hubs(home: Path | None = None) -> dict[str, str]:
    """This machine's hubs, ``hub_id -> repo_name``, from the router registry under ``home``."""

    directory = shared_router.registry_dir(home)
    try:
        paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    except OSError as exc:
        raise HubMailError(f"registry_unavailable:{type(exc).__name__}") from exc
    now = time.time()
    hubs: dict[str, str] = {}
    for path in paths[:MAX_LOCAL_HUBS]:
        record = shared_router._bounded_record(path, now=now)
        if record.get("ok"):
            hubs[str(record["repo_id"])] = str(record.get("repo_name") or "")
    return hubs


@dataclass(frozen=True)
class Keyring:
    """Which key signs which envelope: the machine key or one peer's secret.

    ``peers`` maps a peer hub id to its single owning peer record (see
    ``hub_mail_net.peer_hub_index``): a hub id this machine owns, or one two
    peers claim, is in nobody's map, so no peer passes as a local hub or as
    another peer.
    """

    local_key: bytes
    local_hub_ids: frozenset[str]
    peers: dict[str, dict[str, Any]] = field(default_factory=dict)

    def peer_key(self, hub_id: str) -> bytes | None:
        peer = self.peers.get(hub_id)
        return bytes.fromhex(peer["secret"]) if peer is not None else None

    def key_for(self, envelope: dict[str, Any]) -> bytes | None:
        from_id, to_id = envelope["from_hub"]["hub_id"], envelope["to_hub"]["hub_id"]
        if from_id in self.local_hub_ids:
            return self.peer_key(to_id) or self.local_key
        return self.peer_key(from_id)

    def node(self, hub_id: str) -> dict[str, str] | None:
        peer = self.peers.get(hub_id)
        if peer is None:
            return None
        return {"machine_id": peer["machine_id"], "machine_label": peer["machine_label"]}


def load_keyring(home: Path | None = None, *, create: bool = True) -> Keyring:
    """The machine key plus every peer's secret.

    ``create=False`` never writes: with no machine key yet, an ephemeral one
    stands in, so local mail fails its signature check instead of a key being
    minted by a read.
    """

    from . import hub_mail_net  # the transport imports this module

    local_ids = frozenset(local_hubs(home))
    if create or key_path(home).exists():
        local_key = load_or_create_key(home)
    else:
        local_key = secrets.token_bytes(KEY_BYTES)
    return Keyring(
        local_key=local_key,
        local_hub_ids=local_ids,
        peers=hub_mail_net.peer_hub_index(home, exclude=local_ids),
    )


def _retry_pending(home: Path | None, now: datetime | None) -> None:
    """Give queued peer mail another chance; never fails the caller's read or send."""

    from . import hub_mail_net

    try:
        hub_mail_net.retry_pending(home, now=now)
    except (HubMailError, OSError):
        pass


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(MAX_ENVELOPE_BYTES + 1)
    if len(data) > MAX_ENVELOPE_BYTES:
        raise HubMailError("envelope_too_large")
    return data


def _load_envelope(path: Path) -> tuple[Any, str]:
    try:
        data = _read_bounded(path)
    except HubMailError as exc:
        return None, str(exc)
    except OSError as exc:
        return None, f"read_failed:{type(exc).__name__}"
    try:
        return json.loads(data.decode("utf-8")), ""
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "json_invalid"


def _write_reason(target: Path, *, source_box: str, reason: str) -> None:
    """The sidecar reason JSON that sits beside every quarantined file."""

    sidecar = {
        "schema_id": "aiworkhub.hub_mail.quarantine.v1",
        "file": target.name,
        "source_box": source_box,
        "reason": reason,
        "quarantined_at": _format_time(_now(None)),
    }
    _atomic_write(
        target.with_name(f"{target.name}.reason.json"),
        json.dumps(sidecar, sort_keys=True).encode("utf-8"),
    )


def _quarantine(path: Path, hub_id: str, reason: str, home: Path | None) -> dict[str, Any]:
    """Move one spool file to quarantine beside a sidecar reason JSON."""

    target_dir = box_dir(hub_id, "quarantine", home)
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target_dir / path.name
    if target.exists():
        target = target_dir / f"{path.stem}.{time.time_ns()}{path.suffix}"
    _write_reason(target, source_box=path.parent.name, reason=reason)
    os.replace(path, target)
    return {"file": target.name, "reason": reason}


def _project(
    envelope: dict[str, Any], box: str, *, full: bool, keyring: Keyring | None = None,
) -> dict[str, Any]:
    mail = {
        "untrusted_data": True,
        "box": box,
        "message_id": envelope["message_id"],
        "thread_id": envelope["thread_id"],
        "in_reply_to": envelope["in_reply_to"],
        "from_hub": dict(envelope["from_hub"]),
        "to_hub": dict(envelope["to_hub"]),
        "kind": envelope["kind"],
        "status": envelope["status"],
        "fixed_in": envelope["fixed_in"],
        "subject": envelope["subject"],
        "created_at": envelope["created_at"],
        "expires_at": envelope["expires_at"],
        "body_bytes": len(envelope["body"].encode("utf-8")),
        "attachment_count": len(envelope["attachments"]),
    }
    # Mail from a peer machine names that node; its label is display text only.
    node = keyring.node(envelope["from_hub"]["hub_id"]) if keyring is not None else None
    if node is not None:
        mail["from_node"] = node
    if full:
        mail["body"] = envelope["body"]
        mail["attachments"] = [dict(item) for item in envelope["attachments"]]
    return mail


def _scan_box(
    hub_id: str, box: str, *, key: bytes | Keyring, home: Path | None, now: datetime | None,
    quarantine: bool = True,
) -> tuple[list[tuple[Path, dict[str, Any]]], list[dict[str, Any]], int]:
    """Valid envelopes of one box; every invalid file is quarantined, not skipped.

    With ``quarantine=False`` an invalid file is reported but left in place.
    The third value counts the files beyond ``MAX_SCAN_FILES`` left unscanned.
    """

    directory = box_dir(hub_id, box, home)
    if not directory.is_dir():
        return [], [], 0
    valid: list[tuple[Path, dict[str, Any]]] = []
    quarantined: list[dict[str, Any]] = []
    seen: set[str] = set()
    paths = sorted(p for p in directory.glob("*.json") if not p.name.startswith("."))
    for path in paths[:MAX_SCAN_FILES]:
        try:
            envelope, reason = _load_envelope(path)
            if not reason:
                reason = check_envelope(
                    envelope, key=key, local_hub_id=hub_id, now=now,
                    check_expiry=box == "inbox",
                )
            if not reason and path.name != f"{envelope['message_id']}.json":
                reason = "filename_mismatch"
            if not reason and envelope["message_id"] in seen:
                reason = "duplicate_conflict"
        except _UNPROCESSABLE:
            reason = "envelope_unprocessable"
        if reason and not quarantine:
            quarantined.append({"file": path.name, "reason": reason, "moved": False})
            continue
        if reason:
            try:
                quarantined.append(_quarantine(path, hub_id, reason, home))
            except OSError as exc:
                quarantined.append(
                    {"file": path.name, "reason": reason,
                     "error": f"quarantine_failed:{type(exc).__name__}"}
                )
            continue
        seen.add(envelope["message_id"])
        valid.append((path, envelope))
    return valid, quarantined, max(0, len(paths) - MAX_SCAN_FILES)


def _quarantine_total(hub_id: str, home: Path | None) -> int:
    directory = box_dir(hub_id, "quarantine", home)
    if not directory.is_dir():
        return 0
    return sum(1 for _ in directory.glob("*.reason.json"))


def _find_local(
    hub_id: str, message_id: str, boxes: tuple[str, ...], *, key: bytes | Keyring,
    home: Path | None, now: datetime | None, quarantine: bool = True,
) -> tuple[str, dict[str, Any]] | None:
    for box in boxes:
        path = box_dir(hub_id, box, home) / f"{message_id}.json"
        if not path.is_file():
            continue
        try:
            envelope, reason = _load_envelope(path)
            # The outbox holds mail this hub sent, addressed to someone else.
            owner = envelope["to_hub"]["hub_id"] if (
                box == "outbox" and not reason and isinstance(envelope, dict)
                and isinstance(envelope.get("to_hub"), dict)
            ) else hub_id
            if not reason:
                reason = check_envelope(
                    envelope, key=key, local_hub_id=owner, now=now, check_expiry=box == "inbox"
                )
        except _UNPROCESSABLE:
            reason = "envelope_unprocessable"
        if reason:
            if box != "outbox" and quarantine:
                _quarantine(path, hub_id, reason, home)
            continue
        return box, envelope
    return None


def deliver(data: bytes, *, to_hub_id: str, message_id: str, home: Path | None = None) -> str:
    """Create one envelope in the recipient inbox, never overwriting.

    Identical bytes already there answer ``already_delivered``.  Different
    bytes under the same ``message_id`` are written to the recipient's
    quarantine with reason ``duplicate_conflict`` and the inbox file is left
    byte-identical.  Nothing else of the recipient is listed, read or touched.
    """

    if not _MESSAGE_ID_RE.fullmatch(str(message_id or "")):
        raise HubMailError("message_id_invalid")
    target = box_dir(to_hub_id, "inbox", home) / f"{message_id}.json"
    if _publish_new(target, data):
        return "delivered"
    try:
        existing = _read_bounded(target)
    except (OSError, HubMailError):
        existing = b""
    if existing == data:
        return "already_delivered"
    conflict_dir = box_dir(to_hub_id, "quarantine", home)
    conflict = conflict_dir / f"{message_id}.{time.time_ns()}.json"
    _atomic_write(conflict, data)
    _write_reason(conflict, source_box="delivery", reason="duplicate_conflict")
    return "duplicate_conflict"


def send(
    *,
    local: dict[str, str],
    to_hub_id: str,
    kind: str,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]] | None = None,
    in_reply_to: str | None = None,
    status: str | None = None,
    fixed_in: str | None = None,
    home: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Redact, build, validate, sign, then spool one mail; refuse fail-closed.

    A local recipient gets the mail in its spool; a paired peer's hub gets it
    over the network at once or, when unreachable, ``delivery: queued``.
    """

    _retry_pending(home, now)
    try:
        return _send(
            local=local, to_hub_id=to_hub_id, kind=kind, subject=subject, body=body,
            attachments=attachments, in_reply_to=in_reply_to, status=status,
            fixed_in=fixed_in, home=home, now=now,
        )
    except HubMailError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"spool_write_failed:{type(exc).__name__}"}


def _send(
    *, local: dict[str, str], to_hub_id: str, kind: str, subject: str, body: str,
    attachments: list[dict[str, Any]] | None, in_reply_to: str | None,
    status: str | None, fixed_in: str | None, home: Path | None, now: datetime | None,
) -> dict[str, Any]:
    local_id = str(local.get("hub_id") or "")
    if not _HUB_ID_RE.fullmatch(local_id):
        raise HubMailError("local_hub_identity_unavailable")
    if not isinstance(to_hub_id, str) or not _HUB_ID_RE.fullmatch(to_hub_id):
        raise HubMailError("to_hub_invalid")
    if to_hub_id == local_id:
        raise HubMailError("send_to_self_refused")
    if not isinstance(subject, str) or not isinstance(body, str):
        raise HubMailError("subject_or_body_invalid")
    if not _utf8_encodable(subject) or not _utf8_encodable(body):
        raise HubMailError("encoding_invalid")
    # Bounded before redaction too: the redactor collapses long repeated runs,
    # so an oversize input must not shrink its way past the limits.
    if len(subject) > MAX_SUBJECT_CHARS:
        raise HubMailError("subject_too_long")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise HubMailError("body_too_large")
    if isinstance(fixed_in, str) and len(fixed_in) > MAX_VERSION_CHARS:
        raise HubMailError("fixed_in_version_required")
    raw_attachments = list(attachments or [])
    if len(raw_attachments) > MAX_ATTACHMENTS:
        raise HubMailError("too_many_attachments")
    redacted_attachments: list[dict[str, str]] = []
    for item in raw_attachments:
        if not isinstance(item, dict) or not set(item) <= {"name", "text", "sha256"}:
            raise HubMailError("attachment_invalid")
        text = item.get("text")
        if not isinstance(text, str):
            raise HubMailError("attachment_invalid")
        name = str(item.get("name") or "")
        if not _utf8_encodable(text) or not _utf8_encodable(name):
            raise HubMailError("encoding_invalid")
        if len(name) > MAX_ATTACHMENT_NAME_CHARS:
            raise HubMailError("attachment_name_invalid")
        if len(text.encode("utf-8")) > MAX_ATTACHMENT_BYTES:
            raise HubMailError("attachment_too_large")
        if "sha256" in item and item["sha256"] != _sha256_text(text):
            raise HubMailError("attachment_sha256_mismatch")
        clean = _redact_validation_secrets(text)
        # A redacted name that breaks the name rule is refused by validate_envelope.
        redacted_attachments.append(
            {"name": _redact_validation_secrets(name), "text": clean, "sha256": _sha256_text(clean)}
        )
    keyring = load_keyring(home)
    moment = _now(now)
    message_id = uuid.uuid4().hex
    thread_id = message_id
    if in_reply_to is not None:
        if not isinstance(in_reply_to, str) or not _MESSAGE_ID_RE.fullmatch(in_reply_to):
            raise HubMailError("in_reply_to_invalid")
        parent = _find_local(
            local_id, in_reply_to, ("inbox", "archive", "outbox"),
            key=keyring, home=home, now=moment,
        )
        if parent is None:
            raise HubMailError("in_reply_to_unknown")
        thread_id = parent[1]["thread_id"]
    envelope: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "message_id": message_id,
        "thread_id": thread_id,
        "in_reply_to": in_reply_to,
        "from_hub": {
            "hub_id": local_id,
            "repo_root": str(local.get("repo_root") or ""),
            "aiworkhub_version": str(local.get("aiworkhub_version") or ""),
        },
        "to_hub": {"hub_id": to_hub_id},
        "kind": kind,
        "status": status,
        "fixed_in": _redact_validation_secrets(fixed_in) if isinstance(fixed_in, str) else fixed_in,
        "subject": _redact_validation_secrets(subject),
        "body": _redact_validation_secrets(body),
        "attachments": redacted_attachments,
        "created_at": _format_time(moment),
        "expires_at": _format_time(moment + DEFAULT_TTL),
        # A well-formed placeholder; sign() never covers the signature field.
        "signature": "0" * 64,
    }
    reason = validate_envelope(envelope)
    if reason:
        raise HubMailError(reason)
    peer = keyring.peers.get(to_hub_id)
    if to_hub_id not in keyring.local_hub_ids and peer is None:
        raise HubMailError("unknown_recipient")
    envelope["signature"] = sign(envelope, keyring.peer_key(to_hub_id) or keyring.local_key)
    data = envelope_bytes(envelope)
    sent_copy = box_dir(local_id, "outbox", home) / f"{message_id}.json"
    _atomic_write(sent_copy, data)
    outcome: dict[str, str]
    if peer is not None:
        from . import hub_mail_net

        # The outbox copy stays whatever happens: queued or failed mail is
        # tracked by its sidecar, never dropped.
        outcome = hub_mail_net.forward(
            data, from_hub_id=local_id, to_hub_id=to_hub_id, message_id=message_id,
            peer=peer, home=home,
        )
    else:
        try:
            outcome = {"delivery": deliver(data, to_hub_id=to_hub_id, message_id=message_id, home=home)}
        except (OSError, HubMailError):
            # A refused send leaves nothing behind, not even the sender's copy.
            sent_copy.unlink(missing_ok=True)
            raise
    result: dict[str, Any] = {
        "ok": outcome["delivery"] not in {"duplicate_conflict", "delivery_failed"},
        "schema_id": SCHEMA_ID,
        **outcome,
        "mail": _project(envelope, "outbox", full=False),
    }
    if peer is not None:
        result["to_node"] = keyring.node(to_hub_id)
    return result


def inbox(
    *, local_hub_id: str, limit: int = 50, include_archived: bool = False,
    home: Path | None = None, now: datetime | None = None, quarantine: bool = True,
) -> dict[str, Any]:
    """Validate and list this hub's mail; invalid files go to quarantine.

    ``quarantine=False`` is the read-only form the dashboard uses: invalid
    files are validated and counted in ``invalid_count`` but left where they
    are, queued peer mail is not retried, and no key is created.
    """

    if quarantine:
        _retry_pending(home, now)
    try:
        keyring = load_keyring(home, create=quarantine)
        boxes = ("inbox", "archive") if include_archived else ("inbox",)
        mails: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        unscanned = 0
        for box in boxes:
            valid, rejected, skipped = _scan_box(
                local_hub_id, box, key=keyring, home=home, now=now, quarantine=quarantine,
            )
            invalid.extend(rejected)
            unscanned += skipped
            mails.extend(
                _project(envelope, box, full=False, keyring=keyring) for _, envelope in valid
            )
        total = _quarantine_total(local_hub_id, home)
    except HubMailError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"spool_read_failed:{type(exc).__name__}"}
    mails.sort(key=lambda mail: (mail["created_at"], mail["message_id"]), reverse=True)
    bounded = max(1, min(int(limit or 50), MAX_LIST_LIMIT))
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "hub_id": local_hub_id,
        "untrusted_data": True,
        "count": min(len(mails), bounded),
        "total": len(mails),
        "truncated": len(mails) > bounded,
        "mails": mails[:bounded],
        "scan_truncated": unscanned > 0,
        "unscanned_count": unscanned,
        "invalid_count": len(invalid),
        "quarantined_count": len(invalid) if quarantine else 0,
        "quarantined": invalid if quarantine else [],
        "quarantine_total": total,
    }


def show(
    *, local_hub_id: str, message_id: str, home: Path | None = None,
    now: datetime | None = None, quarantine: bool = True,
) -> dict[str, Any]:
    """One received mail in full, from the inbox or the archive.

    ``quarantine=False`` is the read-only form: an invalid file is skipped in
    place instead of being moved to quarantine.
    """

    if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(message_id):
        return {"ok": False, "error": "message_id_invalid"}
    try:
        keyring = load_keyring(home, create=quarantine)
        found = _find_local(
            local_hub_id, message_id, ("inbox", "archive"), key=keyring, home=home, now=now,
            quarantine=quarantine,
        )
    except HubMailError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"spool_read_failed:{type(exc).__name__}"}
    if found is None:
        return {"ok": False, "error": "message_not_found", "message_id": message_id}
    box, envelope = found
    return {
        "ok": True, "schema_id": SCHEMA_ID,
        "mail": _project(envelope, box, full=True, keyring=keyring),
    }


def archive(
    *, local_hub_id: str, message_id: str, home: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Move one valid inbox mail to the archive (atomic os.replace)."""

    if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(message_id):
        return {"ok": False, "error": "message_id_invalid"}
    try:
        keyring = load_keyring(home)
        found = _find_local(local_hub_id, message_id, ("inbox",), key=keyring, home=home, now=now)
        if found is None:
            return {"ok": False, "error": "message_not_found", "message_id": message_id}
        source = box_dir(local_hub_id, "inbox", home) / f"{message_id}.json"
        target = box_dir(local_hub_id, "archive", home) / f"{message_id}.json"
        if target.exists():
            return {"ok": False, "error": "already_archived", "message_id": message_id}
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(source, target)
    except HubMailError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"spool_write_failed:{type(exc).__name__}"}
    return {
        "ok": True, "schema_id": SCHEMA_ID,
        "mail": _project(found[1], "archive", full=False, keyring=keyring),
    }
