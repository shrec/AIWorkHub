"""WorkHub Mail phase 2: the LAN transport between machines, stdlib only.

Identity: a hub is its router ``repo_id``; a node is one machine's mail home,
named by a random ``machine_id`` persisted once under ``~/.aiworkhub/mail``.
Labels (``machine_label``, ``repo_name``) are display text only: capped at
64 characters, control characters stripped, and never used for routing,
signing or authentication.

Pairing: ``pair_begin`` mints a single-use out-of-band code (120 bits as
grouped base32) that expires after 10 minutes and dies after 5 failed proofs.
The code never crosses the network: both machines derive ``secret =
HMAC-SHA256(code, "aiworkhub.hub_mail.peer.v1|" + sorted machine ids)`` and
prove possession of the code with HMACs over the pairing transcript.

Listener: a ThreadingHTTPServer on an EXPLICIT ``bind_host``/``port`` serving
``POST /v1/pair``, ``POST /v1/mail`` and ``GET /v1/hubs``.  Every non-pair
request carries the caller's machine id, a timestamp, a nonce and
``HMAC(secret, method|path|timestamp|nonce|sha256(body))``; every authenticated
response is signed back over the request nonce.  A refused caller only ever
gets an error code.  ``POST /v1/mail`` only calls ``hub_mail.deliver`` into a
local hub; nothing else is readable or mutable over the wire.

Store-and-forward: a mail to a peer hub gets a ``pending`` sidecar beside its
outbox copy before the first attempt; it is retried by the listener loop and
on every send/inbox call until delivered, or past ``expires_at`` when a
``delivery_failed`` sidecar records it.  Nothing is dropped silently.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import hub_mail, platform_io, runtime_temp, shared_router

PEER_SCHEMA_ID = "aiworkhub.hub_mail.peer.v1"
PAIRING_SCHEMA_ID = "aiworkhub.hub_mail.pairing.v1"
LISTENER_SCHEMA_ID = "aiworkhub.hub_mail.listener.v1"
SIDECAR_SCHEMA_ID = "aiworkhub.hub_mail.delivery.v1"
SECRET_CONTEXT = "aiworkhub.hub_mail.peer.v1|"
_PROOF_CONTEXT = b"aiworkhub.hub_mail.pair.proof.v1"
_RESPONSE_PROOF_CONTEXT = b"aiworkhub.hub_mail.pair.response.v1|"
PAIR_CODE_BYTES = 15  # 120 bits -> 24 base32 characters, no padding
PAIR_CODE_TTL_SECONDS = 600
PAIR_MAX_FAILURES = 5
CLOCK_SKEW_SECONDS = 300
MAX_LABEL_CHARS = 64
MAX_PEERS = 64
MAX_PEER_HUBS = 256
MAX_STATE_FILE_BYTES = 256 * 1024
MAX_PAIR_BODY_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_NONCES = 65536
MAX_PENDING_SCAN = 1000
RETRY_INTERVAL_SECONDS = 60.0
SOCKET_TIMEOUT_SECONDS = 10.0
ENSURE_BACKOFF_SECONDS = 30.0
LISTENER_START_WAIT_SECONDS = 15.0
LISTENER_STOP_WAIT_SECONDS = 10.0
PENDING_SUFFIX = ".pending.json"
FAILED_SUFFIX = ".delivery_failed.json"
HEADER_PEER = "X-AIWorkHub-Peer"
HEADER_TIMESTAMP = "X-AIWorkHub-Timestamp"
HEADER_NONCE = "X-AIWorkHub-Nonce"
HEADER_SIGNATURE = "X-AIWorkHub-Signature"
DELIVERIES = ("delivered", "already_delivered", "duplicate_conflict")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{1,12}$")
_CODE_RE = re.compile(r"^[A-Z2-7]{24}$")
_ADDRESS_RE = re.compile(r"^(?:\[([0-9A-Fa-f:.]{2,45})\]|([A-Za-z0-9.\-]{1,253})):([0-9]{1,5})$")
_PEM_CERT_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.DOTALL
)
# Status codes after which retrying the same bytes can never succeed.
_PERMANENT_STATUSES = frozenset({400, 404, 413})
# Wall clock for every timestamp and window here; tests move it.
_clock = time.time
# The listener binds every interface by default; discovery probes the LAN
# broadcast unless this is a list of explicit "host:port" targets (tests, off-LAN).
DEFAULT_BIND_HOST = "0.0.0.0"
DISCOVERY_TARGETS: list[Any] | None = None

_PEER_LOCK = threading.Lock()
_PAIR_LOCK = threading.Lock()
_RETRY_LOCK = threading.Lock()
_ENSURE_ATTEMPTS: dict[str, float] = {}
_REDISCOVER_ATTEMPTS: dict[str, float] = {}
_PAIR_FLOW_PATHS = frozenset({"/v1/pair/offer", "/v1/pair/reveal", "/v1/pair/confirm"})
# Spawned listener processes are daemons by design; holding the handle stops
# Popen.__del__ from warning about a child that is meant to outlive the call.
_SPAWNED: list[subprocess.Popen[bytes]] = []


class PeerUnreachable(hub_mail.HubMailError):
    """The peer could not be reached or failed transiently; retry later."""


class PeerRefused(hub_mail.HubMailError):
    """The peer answered with a named refusal."""

    def __init__(self, reason: str, *, permanent: bool = False) -> None:
        super().__init__(reason)
        self.permanent = permanent


# ---------------------------------------------------------------- paths


def machine_id_path(home: Path | None = None) -> Path:
    return hub_mail.mail_root(home) / ".machine_id"


def node_path(home: Path | None = None) -> Path:
    return hub_mail.mail_root(home) / ".node.json"


def peers_dir(home: Path | None = None) -> Path:
    return hub_mail.mail_root(home) / ".peers"


def peer_path(machine_id: str, home: Path | None = None) -> Path:
    if not _HEX32_RE.fullmatch(str(machine_id or "")):
        raise hub_mail.HubMailError("machine_id_invalid")
    return peers_dir(home) / f"{machine_id}.json"


def pairing_path(home: Path | None = None) -> Path:
    return hub_mail.mail_root(home) / ".pairing.json"


def pidfile_path(home: Path | None = None) -> Path:
    return hub_mail.mail_root(home) / ".listener.json"


def listener_config_path(home: Path | None = None) -> Path:
    return hub_mail.mail_root(home) / ".listener.config.json"


def stop_request_path(home: Path | None = None) -> Path:
    return hub_mail.mail_root(home) / ".listener.stop"


def listener_log_path(home: Path | None = None) -> Path:
    return hub_mail.mail_root(home) / ".listener.log"


def listener_lock_path(home: Path | None = None) -> Path:
    return hub_mail.mail_root(home) / ".listener.lock"


# ---------------------------------------------------------------- helpers


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _json_object(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_json(path: Path, limit: int = MAX_STATE_FILE_BYTES) -> dict[str, Any] | None:
    """A small JSON object file, or ``None`` when missing, oversize or corrupt."""

    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        return None
    return _json_object(data) or None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    hub_mail._write_over(path, json.dumps(value, sort_keys=True, indent=1).encode("utf-8"))


def _iso(seconds: float) -> str:
    return hub_mail._format_time(datetime.fromtimestamp(seconds, timezone.utc))


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pid_alive(pid: Any) -> bool:
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    return number > 0 and bool(shared_router.process_is_alive(number))


def clean_label(value: Any) -> str:
    """Untrusted display text: control/format characters stripped, 64 chars."""

    text = value if isinstance(value, str) else ""
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("C"))
    return text.strip()[:MAX_LABEL_CHARS].strip()


def clean_hubs(value: Any) -> list[dict[str, str]]:
    """A peer's advertised hubs: valid unique hub ids with display names only."""

    hubs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        if len(hubs) >= MAX_PEER_HUBS:
            break
        if not isinstance(item, dict):
            continue
        hub_id = item.get("hub_id")
        if not isinstance(hub_id, str) or not hub_mail._HUB_ID_RE.fullmatch(hub_id) or hub_id in seen:
            continue
        seen.add(hub_id)
        hubs.append({"hub_id": hub_id, "repo_name": clean_label(item.get("repo_name"))})
    return hubs


def local_hub_list(home: Path | None = None) -> list[dict[str, str]]:
    return clean_hubs(
        [{"hub_id": hub_id, "repo_name": name} for hub_id, name in sorted(hub_mail.local_hubs(home).items())]
    )


def parse_address(address: Any) -> tuple[str, int]:
    """``host:port`` (or ``[v6]:port``) -> ``(host, port)``; refuse anything else."""

    match = _ADDRESS_RE.fullmatch(str(address or "").strip())
    if match is None:
        raise hub_mail.HubMailError("address_invalid")
    port = int(match.group(3))
    if not 1 <= port <= 65535:
        raise hub_mail.HubMailError("address_invalid")
    return match.group(1) or match.group(2), port


def format_address(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def cert_sha256(cert_path: str | Path) -> str:
    """sha256 of the DER form of the first certificate in a PEM file."""

    try:
        with Path(cert_path).open("r", encoding="ascii") as handle:
            text = handle.read(MAX_STATE_FILE_BYTES)
    except (OSError, UnicodeDecodeError) as exc:
        raise hub_mail.HubMailError(f"tls_cert_unreadable:{type(exc).__name__}") from exc
    match = _PEM_CERT_RE.search(text)
    if match is None:
        raise hub_mail.HubMailError("tls_cert_invalid")
    try:
        der = ssl.PEM_cert_to_DER_cert(match.group(0))
    except ValueError as exc:
        raise hub_mail.HubMailError("tls_cert_invalid") from exc
    return _sha256_hex(der)


# ---------------------------------------------------------------- node identity


def machine_id(home: Path | None = None) -> str:
    """This node's random 32-hex id, created exactly once (O_EXCL, 0600)."""

    path = machine_id_path(home)
    if not path.exists():
        hub_mail._publish_new(path, secrets.token_hex(16).encode("ascii"))
    try:
        with path.open("rb") as handle:
            value = handle.read(65).decode("ascii", "replace").strip()
    except OSError as exc:
        raise hub_mail.HubMailError(f"machine_id_unreadable:{type(exc).__name__}") from exc
    if not _HEX32_RE.fullmatch(value):
        raise hub_mail.HubMailError("machine_id_invalid")
    return value


def machine_label(home: Path | None = None) -> str:
    """The owner's label for this node, else the host name; display only."""

    node = _read_json(node_path(home)) or {}
    return clean_label(node.get("machine_label")) or clean_label(socket.gethostname()) or "unnamed"


def set_machine_label(label: str, home: Path | None = None) -> str:
    clean = clean_label(label)
    if not clean:
        raise hub_mail.HubMailError("machine_label_invalid")
    _write_json(node_path(home), {"schema_id": PEER_SCHEMA_ID, "machine_label": clean})
    return clean


# ---------------------------------------------------------------- peer store


def load_peer(peer_id: str, home: Path | None = None) -> dict[str, Any] | None:
    """One paired peer, or ``None`` when unknown or its record is invalid."""

    if not _HEX32_RE.fullmatch(str(peer_id or "")):
        return None
    record = _read_json(peer_path(peer_id, home))
    if (
        not record
        or record.get("schema_id") != PEER_SCHEMA_ID
        or record.get("machine_id") != peer_id
        or not _HEX64_RE.fullmatch(str(record.get("secret") or ""))
    ):
        return None
    try:
        parse_address(record.get("address"))
    except hub_mail.HubMailError:
        return None
    record["machine_label"] = clean_label(record.get("machine_label"))
    record["hubs"] = clean_hubs(record.get("hubs"))
    pin = str(record.get("tls_cert_sha256") or "")
    record["tls_cert_sha256"] = pin if _HEX64_RE.fullmatch(pin) else ""
    return record


def list_peers(home: Path | None = None) -> list[dict[str, Any]]:
    directory = peers_dir(home)
    if not directory.is_dir():
        return []
    paths = sorted(p for p in directory.glob("*.json") if not p.name.startswith("."))
    peers = (load_peer(path.stem, home) for path in paths[:MAX_PEERS])
    return [peer for peer in peers if peer is not None]


def _store_peer(record: dict[str, Any], home: Path | None) -> None:
    with _PEER_LOCK:
        _write_json(peer_path(record["machine_id"], home), record)


def _update_peer(peer_id: str, secret: str, home: Path | None, **changes: Any) -> None:
    """Change fields of a stored peer unless it was re-paired meanwhile."""

    with _PEER_LOCK:
        current = load_peer(peer_id, home)
        if current is None or current["secret"] != secret:
            return
        current.update(changes)
        _write_json(peer_path(peer_id, home), current)


def public_peer(peer: dict[str, Any]) -> dict[str, Any]:
    """What a caller may see of a peer: never its secret."""

    return {
        "machine_id": peer["machine_id"],
        "machine_label": peer["machine_label"],
        "address": peer["address"],
        "hubs": [dict(hub) for hub in peer["hubs"]],
        "paired_at": peer.get("paired_at") or "",
        "last_seen": peer.get("last_seen") or "",
        "tls_cert_sha256": peer["tls_cert_sha256"],
    }


def peers(home: Path | None = None) -> dict[str, Any]:
    try:
        own_id = machine_id(home)
        listing = [public_peer(peer) for peer in list_peers(home)]
    except hub_mail.HubMailError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"peer_store_unreadable:{type(exc).__name__}"}
    return {
        "ok": True,
        "schema_id": PEER_SCHEMA_ID,
        "machine_id": own_id,
        "machine_label": machine_label(home),
        "untrusted_labels": True,
        "count": len(listing),
        "peers": listing,
    }


def peer_hub_index(
    home: Path | None = None, *, exclude: frozenset[str] | set[str] = frozenset()
) -> dict[str, dict[str, Any]]:
    """``hub_id -> owning peer``.

    A hub id claimed by this machine (``exclude``) or by two peers belongs to
    nobody, so no peer can ever pass as a local hub or as another peer.
    """

    owners: dict[str, list[dict[str, Any]]] = {}
    for peer in list_peers(home):
        for hub in peer["hubs"]:
            owners.setdefault(hub["hub_id"], []).append(peer)
    return {
        hub_id: claimants[0]
        for hub_id, claimants in owners.items()
        if len(claimants) == 1 and hub_id not in exclude
    }


# ---------------------------------------------------------------- pairing crypto


def normalize_code(code: Any) -> str:
    text = re.sub(r"[\s\-]", "", str(code or "")).upper()
    if not _CODE_RE.fullmatch(text):
        raise hub_mail.HubMailError("pair_code_invalid")
    return text


def derive_secret(code: str, machine_a: str, machine_b: str) -> bytes:
    """The per-peer secret both machines derive; the code never leaves either."""

    ids = "|".join(sorted((machine_a, machine_b)))
    return hmac.new(
        normalize_code(code).encode("ascii"), (SECRET_CONTEXT + ids).encode("ascii"), hashlib.sha256
    ).digest()


def _proof_key(code: str) -> bytes:
    return hmac.new(normalize_code(code).encode("ascii"), _PROOF_CONTEXT, hashlib.sha256).digest()


def _request_proof(code: str, fields: dict[str, Any]) -> str:
    return hmac.new(_proof_key(code), _canonical(fields), hashlib.sha256).hexdigest()


def _response_proof(secret: bytes, request_body: bytes, fields: dict[str, Any]) -> str:
    message = _RESPONSE_PROOF_CONTEXT + _sha256_hex(request_body).encode("ascii") + b"|" + _canonical(fields)
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _request_mac(secret: bytes, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = f"{method}|{path}|{timestamp}|{nonce}|{_sha256_hex(body)}"
    return hmac.new(secret, message.encode("ascii"), hashlib.sha256).hexdigest()


def _response_mac(secret: bytes, status: int, nonce: str, body: bytes) -> str:
    message = f"response|{status}|{nonce}|{_sha256_hex(body)}"
    return hmac.new(secret, message.encode("ascii"), hashlib.sha256).hexdigest()


def pair_begin(home: Path | None = None) -> dict[str, Any]:
    """Mint this node's single-use pairing code; any older code dies."""

    try:
        own_id = machine_id(home)
        text = base64.b32encode(secrets.token_bytes(PAIR_CODE_BYTES)).decode("ascii").rstrip("=")
        created = _clock()
        with _PAIR_LOCK:
            _write_json(
                pairing_path(home),
                {
                    "schema_id": PAIRING_SCHEMA_ID,
                    "code": text,
                    "created_at": created,
                    "expires_at": created + PAIR_CODE_TTL_SECONDS,
                    "failures": 0,
                },
            )
    except hub_mail.HubMailError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"pairing_write_failed:{type(exc).__name__}"}
    return {
        "ok": True,
        "schema_id": PAIRING_SCHEMA_ID,
        "code": "-".join(text[i : i + 4] for i in range(0, len(text), 4)),
        "expires_at": _iso(created + PAIR_CODE_TTL_SECONDS),
        "max_failed_proofs": PAIR_MAX_FAILURES,
        "machine_id": own_id,
        "machine_label": machine_label(home),
        "listener": listener_status(home),
    }


def pair_complete(
    address: str, code: str, *, home: Path | None = None, tls: bool = False,
    listener_address: str | None = None,
) -> dict[str, Any]:
    """Pair with the node listening at ``address`` using its out-of-band code."""

    try:
        return _pair_complete(address, code, home=home, tls=tls, listener_address=listener_address)
    except hub_mail.HubMailError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"peer_store_write_failed:{type(exc).__name__}"}


def _pair_complete(
    address: str, code: str, *, home: Path | None, tls: bool, listener_address: str | None,
) -> dict[str, Any]:
    code = normalize_code(code)
    parse_address(address)
    own_id = machine_id(home)
    status = listener_status(home)
    if listener_address:
        own_address = listener_address
    elif not status.get("running"):
        raise hub_mail.HubMailError("local_listener_not_running")
    else:
        # A wildcard bind with no routable interface advertises "": the owner
        # must name the address peers dial back instead of a loopback one.
        own_address = str(status.get("address") or "")
        if not own_address:
            raise hub_mail.HubMailError("listener_address_unroutable")
    parse_address(own_address)
    fields = {
        "machine_id": own_id,
        "machine_label": machine_label(home),
        "address": own_address,
        "hubs": local_hub_list(home),
        "nonce": secrets.token_hex(16),
        "timestamp": int(_clock()),
        "tls_cert_sha256": str(status.get("tls_cert_sha256") or ""),
    }
    body = _canonical({**fields, "proof": _request_proof(code, fields)})
    status_code, _, data, observed = _exchange(
        address, "POST", "/v1/pair", body, {"Content-Type": "application/json"}, tls=tls, pin=None,
    )
    reply = _json_object(data)
    if status_code != 200:
        raise PeerRefused(f"peer_refused:{_error_code(reply, status_code)}")
    peer_id = str(reply.get("machine_id") or "")
    if not _HEX32_RE.fullmatch(peer_id) or peer_id == own_id:
        raise PeerRefused("pair_response_invalid")
    secret = derive_secret(code, own_id, peer_id)
    answered = {k: reply.get(k) for k in ("machine_id", "machine_label", "hubs", "tls_cert_sha256")}
    if not hmac.compare_digest(str(reply.get("proof") or ""), _response_proof(secret, body, answered)):
        raise PeerRefused("pair_response_proof_invalid")
    if tls and not hmac.compare_digest(str(reply.get("tls_cert_sha256") or ""), observed):
        raise PeerRefused("tls_cert_mismatch")
    now = _iso(_clock())
    record = {
        "schema_id": PEER_SCHEMA_ID,
        "machine_id": peer_id,
        "machine_label": clean_label(reply.get("machine_label")),
        "address": address,
        "hubs": clean_hubs(reply.get("hubs")),
        "secret": secret.hex(),
        "paired_at": now,
        "last_seen": now,
        "tls_cert_sha256": observed if tls else "",
    }
    _store_peer(record, home)
    return {"ok": True, "schema_id": PEER_SCHEMA_ID, "peer": public_peer(load_peer(peer_id, home) or record)}


def _accept_pair(request: dict[str, Any], body: bytes, *, home: Path | None, own_tls: str) -> tuple[int, dict[str, Any]]:
    """The listener half of pairing: verify the proof, store, answer with ours."""

    fields = {k: request.get(k) for k in (
        "machine_id", "machine_label", "address", "hubs", "nonce", "timestamp", "tls_cert_sha256",
    )}
    peer_id = str(fields["machine_id"] or "")
    try:
        parse_address(fields["address"])
        valid = (
            _HEX32_RE.fullmatch(peer_id) is not None
            and _HEX32_RE.fullmatch(str(fields["nonce"] or "")) is not None
            and isinstance(fields["timestamp"], int)
            and isinstance(request.get("proof"), str)
            and (not fields["tls_cert_sha256"] or _HEX64_RE.fullmatch(str(fields["tls_cert_sha256"])) is not None)
        )
    except hub_mail.HubMailError:
        valid = False
    if not valid or set(request) != {*fields, "proof"}:
        return 400, {"ok": False, "error": "pair_request_invalid"}
    own_id = machine_id(home)
    if peer_id == own_id:
        return 400, {"ok": False, "error": "pair_self_refused"}
    with _PAIR_LOCK:
        pending = _read_json(pairing_path(home))
        if not pending or pending.get("schema_id") != PAIRING_SCHEMA_ID:
            return 403, {"ok": False, "error": "pair_code_unavailable"}
        if _clock() >= float(pending.get("expires_at") or 0):
            pairing_path(home).unlink(missing_ok=True)
            return 403, {"ok": False, "error": "pair_code_expired"}
        failures = int(pending.get("failures") or 0)
        if failures >= PAIR_MAX_FAILURES:
            pairing_path(home).unlink(missing_ok=True)
            return 403, {"ok": False, "error": "pair_code_exhausted"}
        if abs(_clock() - fields["timestamp"]) > CLOCK_SKEW_SECONDS:
            return 401, {"ok": False, "error": "timestamp_out_of_window"}
        code = str(pending.get("code") or "")
        if not hmac.compare_digest(str(request["proof"]), _request_proof(code, fields)):
            pending["failures"] = failures + 1
            if pending["failures"] >= PAIR_MAX_FAILURES:
                pairing_path(home).unlink(missing_ok=True)
            else:
                _write_json(pairing_path(home), pending)
            return 403, {"ok": False, "error": "pair_proof_invalid"}
        # Single use: the code is gone before the peer is stored.
        pairing_path(home).unlink(missing_ok=True)
    secret = derive_secret(code, own_id, peer_id)
    now = _iso(_clock())
    _store_peer(
        {
            "schema_id": PEER_SCHEMA_ID,
            "machine_id": peer_id,
            "machine_label": clean_label(fields["machine_label"]),
            "address": str(fields["address"]),
            "hubs": clean_hubs(fields["hubs"]),
            "secret": secret.hex(),
            "paired_at": now,
            "last_seen": now,
            "tls_cert_sha256": str(fields["tls_cert_sha256"] or ""),
        },
        home,
    )
    answer = {
        "machine_id": own_id,
        "machine_label": machine_label(home),
        "hubs": local_hub_list(home),
        "tls_cert_sha256": own_tls,
    }
    return 200, {"ok": True, **answer, "proof": _response_proof(secret, body, answer)}


# ---------------------------------------------------------------- client


def _error_code(reply: dict[str, Any], status: int) -> str:
    error = reply.get("error")
    return clean_label(error) if isinstance(error, str) and error else f"http_{status}"


def _open(address: str, *, tls: bool, pin: str | None, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    """A connected client; with TLS the server cert sha256 must equal ``pin``."""

    host, port = parse_address(address)
    connection: http.client.HTTPConnection
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Trust is the pinned certificate sha256 compared below, not a CA.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.connect()
    except OSError as exc:
        connection.close()
        raise PeerUnreachable(f"peer_unreachable:{type(exc).__name__}") from exc
    observed = ""
    if tls:
        getpeercert = getattr(connection.sock, "getpeercert", None)
        der = getpeercert(binary_form=True) if callable(getpeercert) else None
        observed = _sha256_hex(der) if der else ""
        if not observed or (pin is not None and not hmac.compare_digest(observed, pin)):
            connection.close()
            raise PeerRefused("tls_cert_mismatch")
    return connection, observed


def _exchange(
    address: str, method: str, path: str, body: bytes, headers: dict[str, str], *,
    tls: bool, pin: str | None, timeout: float = SOCKET_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, str], bytes, str]:
    """One bounded HTTP round trip: (status, lowercased headers, body, cert sha256)."""

    connection, observed = _open(address, tls=tls, pin=pin, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
        status = response.status
        reply_headers = {key.lower(): value for key, value in response.getheaders()}
    except (OSError, http.client.HTTPException) as exc:
        raise PeerUnreachable(f"peer_unreachable:{type(exc).__name__}") from exc
    finally:
        connection.close()
    if len(data) > MAX_RESPONSE_BYTES:
        raise PeerRefused("response_too_large")
    return status, reply_headers, data, observed


def _signed_headers(
    peer: dict[str, Any], home: Path | None, method: str, path: str, body: bytes, *,
    timestamp: str | None = None, nonce: str | None = None,
) -> dict[str, str]:
    stamp = timestamp if timestamp is not None else str(int(_clock()))
    once = nonce if nonce is not None else secrets.token_hex(16)
    return {
        HEADER_PEER: machine_id(home),
        HEADER_TIMESTAMP: stamp,
        HEADER_NONCE: once,
        HEADER_SIGNATURE: _request_mac(bytes.fromhex(peer["secret"]), method, path, stamp, once, body),
        "Content-Type": "application/json",
    }


def call_peer(
    peer: dict[str, Any], *, home: Path | None, method: str, path: str, body: bytes = b"",
    timeout: float = SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One authenticated request to a paired peer; its reply must be signed back."""

    headers = _signed_headers(peer, home, method, path, body)
    pin = peer["tls_cert_sha256"] or None
    status, reply_headers, data, _ = _exchange(
        peer["address"], method, path, body, headers, tls=pin is not None, pin=pin, timeout=timeout,
    )
    reply = _json_object(data)
    if status >= 500:
        raise PeerUnreachable(f"peer_error:{_error_code(reply, status)}")
    if status != 200:
        raise PeerRefused(f"peer_refused:{_error_code(reply, status)}", permanent=status in _PERMANENT_STATUSES)
    expected = _response_mac(bytes.fromhex(peer["secret"]), status, headers[HEADER_NONCE], data)
    if not hmac.compare_digest(reply_headers.get(HEADER_SIGNATURE.lower(), ""), expected):
        raise PeerRefused("response_signature_invalid")
    return reply


def post_mail(peer: dict[str, Any], data: bytes, *, home: Path | None) -> str:
    reply = call_peer(peer, home=home, method="POST", path="/v1/mail", body=data)
    delivery = reply.get("delivery")
    if delivery not in DELIVERIES:
        raise PeerRefused("peer_reply_invalid")
    return str(delivery)


def refresh_peers(home: Path | None = None) -> dict[str, Any]:
    """Re-read every peer's hubs over authenticated ``GET /v1/hubs``."""

    results: list[dict[str, Any]] = []
    for peer in list_peers(home):
        try:
            reply = call_peer(peer, home=home, method="GET", path="/v1/hubs")
            if reply.get("machine_id") != peer["machine_id"]:
                raise PeerRefused("peer_identity_mismatch")
        except hub_mail.HubMailError as exc:
            results.append({"machine_id": peer["machine_id"], "ok": False, "error": str(exc)})
            continue
        _update_peer(
            peer["machine_id"], peer["secret"], home,
            hubs=clean_hubs(reply.get("hubs")),
            machine_label=clean_label(reply.get("machine_label")) or peer["machine_label"],
            last_seen=_iso(_clock()),
        )
        results.append({"machine_id": peer["machine_id"], "ok": True})
    return {"ok": True, "refreshed": results}


# ---------------------------------------------------------------- store-and-forward


def _sidecar(copy: Path, suffix: str) -> Path:
    return copy.with_name(f"{copy.name}{suffix}")


def _write_sidecar(copy: Path, suffix: str, state: dict[str, Any]) -> None:
    _write_json(_sidecar(copy, suffix), {"schema_id": SIDECAR_SCHEMA_ID, "file": copy.name, **state})


def _fail(copy: Path, state: dict[str, Any], reason: str) -> None:
    """Record a mail that will never be delivered; the outbox copy stays."""

    _write_sidecar(copy, FAILED_SUFFIX, {**state, "state": "delivery_failed", "reason": reason,
                                         "failed_at": _iso(_clock())})
    _sidecar(copy, PENDING_SUFFIX).unlink(missing_ok=True)


def _rediscover(peer: dict[str, Any], home: Path | None) -> dict[str, Any] | None:
    """The same machine at a new address, verified by a signed call, else ``None``.

    A discovery reply is only a hint: the stored address changes after the
    peer proves it holds the pairing secret at the candidate address.
    """

    key = f"{hub_mail.mail_root(home)}|{peer['machine_id']}"
    if time.monotonic() - _REDISCOVER_ATTEMPTS.get(key, -ENSURE_BACKOFF_SECONDS) < ENSURE_BACKOFF_SECONDS:
        return None
    _REDISCOVER_ATTEMPTS[key] = time.monotonic()
    pairing = _pairing()
    found = pairing.discover(
        pairing.REDISCOVER_TIMEOUT_SECONDS, home=home, until_machine_id=peer["machine_id"],
    )
    entry = next((e for e in found.get("found", []) if e["machine_id"] == peer["machine_id"]), None)
    if entry is None or entry["address"] == peer["address"]:
        return None
    candidate = {**peer, "address": entry["address"]}
    try:
        reply = call_peer(candidate, home=home, method="GET", path="/v1/hubs")
    except hub_mail.HubMailError:
        return None
    if reply.get("machine_id") != peer["machine_id"]:
        return None
    _update_peer(peer["machine_id"], peer["secret"], home, address=entry["address"], last_seen=_iso(_clock()))
    return candidate


def forward(
    data: bytes, *, from_hub_id: str, to_hub_id: str, message_id: str,
    peer: dict[str, Any], home: Path | None,
) -> dict[str, str]:
    """Deliver one already-spooled outbox copy to its peer now, or queue it.

    The pending sidecar is written BEFORE the first attempt so a crash between
    spooling and posting still leaves the mail queued.  An unreachable peer is
    looked for once on the LAN; if it answers elsewhere the address is updated.
    """

    copy = hub_mail.box_dir(from_hub_id, "outbox", home) / f"{message_id}.json"
    state = {"state": "pending", "message_id": message_id, "to_hub_id": to_hub_id,
             "machine_id": peer["machine_id"], "attempts": 1, "last_error": "",
             "last_attempt_at": _iso(_clock())}
    _write_sidecar(copy, PENDING_SUFFIX, state)
    try:
        try:
            delivery = post_mail(peer, data, home=home)
        except PeerUnreachable:
            moved = _rediscover(peer, home)
            if moved is None:
                raise
            delivery = post_mail(moved, data, home=home)
    except PeerRefused as exc:
        if exc.permanent:
            _fail(copy, state, str(exc))
            return {"delivery": "delivery_failed", "error": str(exc)}
        _write_sidecar(copy, PENDING_SUFFIX, {**state, "last_error": str(exc)})
        return {"delivery": "queued", "last_error": str(exc)}
    except hub_mail.HubMailError as exc:
        _write_sidecar(copy, PENDING_SUFFIX, {**state, "last_error": str(exc)})
        return {"delivery": "queued", "last_error": str(exc)}
    _sidecar(copy, PENDING_SUFFIX).unlink(missing_ok=True)
    return {"delivery": delivery}


def retry_pending(home: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Retry every queued peer mail once; expire the ones past ``expires_at``."""

    if not _RETRY_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": "retry_in_progress"}
    try:
        local = hub_mail.local_hubs(home)
        sidecars: list[Path] = []
        for hub_id in sorted(local):
            directory = hub_mail.box_dir(hub_id, "outbox", home)
            if directory.is_dir():
                sidecars.extend(sorted(directory.glob(f"*{PENDING_SUFFIX}")))
        outcome: dict[str, list[str]] = {"delivered": [], "queued": [], "failed": []}
        if not sidecars:
            return {"ok": True, **outcome}
        index = peer_hub_index(home, exclude=frozenset(local))
        down: set[str] = set()
        moment = hub_mail._now(now)
        for sidecar in sidecars[:MAX_PENDING_SCAN]:
            copy = sidecar.with_name(sidecar.name[: -len(PENDING_SUFFIX)])
            result = _retry_one(copy, sidecar, index=index, down=down, home=home, moment=moment)
            outcome[result].append(copy.name)
        return {"ok": True, **outcome, "unscanned_count": max(0, len(sidecars) - MAX_PENDING_SCAN)}
    finally:
        _RETRY_LOCK.release()


def _retry_one(
    copy: Path, sidecar: Path, *, index: dict[str, dict[str, Any]], down: set[str],
    home: Path | None, moment: datetime,
) -> str:
    state = _read_json(sidecar) or {}
    state.pop("schema_id", None)
    state.pop("file", None)
    try:
        data = hub_mail._read_bounded(copy)
        envelope = _json_object(data)
        to_hub_id = str(envelope["to_hub"]["hub_id"])
    except (OSError, hub_mail.HubMailError, KeyError, TypeError):
        _fail(copy, state, "outbox_copy_unreadable")
        return "failed"
    expires = hub_mail._parse_time(envelope.get("expires_at"))
    if expires is None or expires <= moment:
        _fail(copy, state, "expired")
        return "failed"
    peer = index.get(to_hub_id)
    attempt = {**state, "last_attempt_at": _iso(_clock())}
    if peer is None:
        _write_sidecar(copy, PENDING_SUFFIX, {**attempt, "last_error": "recipient_peer_unknown"})
        return "queued"
    if peer["machine_id"] in down:
        return "queued"
    attempt["attempts"] = int(state.get("attempts") or 0) + 1
    try:
        post_mail(peer, data, home=home)
    except PeerRefused as exc:
        if exc.permanent:
            _fail(copy, attempt, str(exc))
            return "failed"
        down.add(peer["machine_id"])
        _write_sidecar(copy, PENDING_SUFFIX, {**attempt, "last_error": str(exc)})
        return "queued"
    except hub_mail.HubMailError as exc:
        down.add(peer["machine_id"])
        _write_sidecar(copy, PENDING_SUFFIX, {**attempt, "last_error": str(exc)})
        return "queued"
    sidecar.unlink(missing_ok=True)
    return "delivered"


def _sweep_pair_requests(home: Path | None = None) -> dict[str, Any]:
    return _pairing().sweep_requests(home)


def maintain(home: Path | None = None) -> dict[str, Any]:
    """One listener-loop pass: refresh peers' hubs, retry queued mail, drop expired pair requests."""

    report: dict[str, Any] = {"ok": True}
    for name, step in (("refresh", refresh_peers), ("retry", retry_pending), ("pairing", _sweep_pair_requests)):
        try:
            report[name] = step(home)
        except (hub_mail.HubMailError, OSError) as exc:
            report[name] = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    return report


# ---------------------------------------------------------------- listener


class _NonceStore:
    """Seen ``(peer, nonce)`` pairs, each kept until its timestamp ages out."""

    def __init__(self, limit: int = MAX_NONCES) -> None:
        self._seen: dict[tuple[str, str], float] = {}
        self._limit = limit
        self._lock = threading.Lock()

    def add(self, peer_id: str, nonce: str, timestamp: int, now: float) -> str:
        with self._lock:
            key = (peer_id, nonce)
            if self._seen.get(key, 0.0) > now:
                return "nonce_replayed"
            if len(self._seen) >= self._limit:
                self._seen = {k: v for k, v in self._seen.items() if v > now}
            if len(self._seen) >= self._limit:
                return "nonce_store_full"
            self._seen[key] = timestamp + CLOCK_SKEW_SECONDS + 1.0
            return ""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    # SO_REUSEADDR on Windows lets a second socket bind a port that is in use.
    allow_reuse_address = not platform_io.is_windows()

    def __init__(self, address: tuple[str, int], *, home: Path | None, tls_sha256: str) -> None:
        pairing = _pairing()
        self.home = home
        self.tls_sha256 = tls_sha256
        self.nonces = _NonceStore()
        self.offer_limiter = pairing.RateLimiter(pairing.OFFER_RATE_LIMIT, pairing.OFFER_RATE_WINDOW_SECONDS)
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind calls getfqdn(), a reverse DNS lookup.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A broken or hostile connection is dropped without a traceback."""


class _Server6(_Server):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "AIWorkHubMail/1"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = SOCKET_TIMEOUT_SECONDS

    def setup(self) -> None:
        self.request.settimeout(self.timeout)
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()
        super().setup()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        """No per-request stderr noise."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib dispatch name
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib dispatch name
        self._dispatch("POST")

    def _reply(self, status: int, payload: dict[str, Any], *, secret: bytes | None = None, nonce: str = "") -> None:
        data = _canonical(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if secret is not None:
            self.send_header(HEADER_SIGNATURE, _response_mac(secret, status, nonce, data))
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self, status: int, error: str) -> None:
        self._reply(status, {"ok": False, "error": error})

    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except (hub_mail.HubMailError, OSError, ValueError, KeyError, TypeError):
            try:
                self._refuse(500, "internal_error")
            except OSError:
                pass

    def _read_body(self, method: str, limit: int) -> bytes | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            if method == "POST":
                self._refuse(411, "length_required")
                return None
            return b""
        if not raw.strip().isdigit():
            self._refuse(400, "content_length_invalid")
            return None
        length = int(raw)
        if length > limit:
            self._refuse(413, "body_too_large")
            return None
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            self._refuse(400, "body_incomplete")
            return None
        return body

    def _route(self, method: str) -> None:
        known = {("POST", "/v1/pair"), ("POST", "/v1/mail"), ("GET", "/v1/hubs")}
        known.update(("POST", path) for path in _PAIR_FLOW_PATHS)
        if (method, self.path) not in known:
            self._refuse(404, "not_found")
            return
        limit = MAX_PAIR_BODY_BYTES if self.path.startswith("/v1/pair") else hub_mail.MAX_ENVELOPE_BYTES
        body = self._read_body(method, limit)
        if body is None:
            return
        home = self.server.home
        if self.path == "/v1/pair":
            status, payload = _accept_pair(_json_object(body), body, home=home, own_tls=self.server.tls_sha256)
            self._reply(status, payload)
            return
        if self.path in _PAIR_FLOW_PATHS:
            self._pair_flow(_json_object(body))
            return
        authenticated = self._authenticate(method, body)
        if authenticated is None:
            return
        peer, nonce = authenticated
        secret = bytes.fromhex(peer["secret"])
        if self.path == "/v1/hubs":
            payload = {"ok": True, "machine_id": machine_id(home), "machine_label": machine_label(home),
                       "hubs": local_hub_list(home)}
            self._reply(200, payload, secret=secret, nonce=nonce)
            return
        status, payload = _accept_mail(peer, body, home=home)
        self._reply(status, payload, secret=secret, nonce=nonce)

    def _pair_flow(self, request: dict[str, Any]) -> None:
        """The code-free pairing steps; the request id (128 random bits) is the only handle."""

        pairing = _pairing()
        home = self.server.home
        if self.path == "/v1/pair/offer":
            status, payload = pairing.handle_offer(
                request, source=pairing.source_host(str(self.client_address[0])), home=home,
                limiter=self.server.offer_limiter, own_tls=self.server.tls_sha256,
            )
            self._reply(status, payload)
        elif self.path == "/v1/pair/reveal":
            status, payload = pairing.handle_reveal(request, home=home)
            self._reply(status, payload)
        else:
            status, payload, secret = pairing.handle_confirm(request, home=home)
            if secret is None:
                self._reply(status, payload)
            else:
                self._reply(status, payload, secret=secret, nonce=str(request["request_id"]))

    def _authenticate(self, method: str, body: bytes) -> tuple[dict[str, Any], str] | None:
        peer_id = self.headers.get(HEADER_PEER) or ""
        stamp = self.headers.get(HEADER_TIMESTAMP) or ""
        nonce = self.headers.get(HEADER_NONCE) or ""
        signature = self.headers.get(HEADER_SIGNATURE) or ""
        if not (peer_id and stamp and nonce and signature):
            self._refuse(401, "auth_missing")
            return None
        if not (_HEX32_RE.fullmatch(peer_id) and _TIMESTAMP_RE.fullmatch(stamp)
                and _HEX32_RE.fullmatch(nonce) and _HEX64_RE.fullmatch(signature)):
            self._refuse(401, "auth_malformed")
            return None
        home = self.server.home
        peer = load_peer(peer_id, home)
        if peer is None:
            self._refuse(401, "unknown_peer")
            return None
        now = _clock()
        if abs(now - int(stamp)) > CLOCK_SKEW_SECONDS:
            self._refuse(401, "timestamp_out_of_window")
            return None
        expected = _request_mac(bytes.fromhex(peer["secret"]), method, self.path, stamp, nonce, body)
        if not hmac.compare_digest(signature, expected):
            self._refuse(401, "bad_request_signature")
            return None
        # Only an authenticated request may spend a nonce.
        replay = self.server.nonces.add(peer_id, nonce, int(stamp), now)
        if replay:
            self._refuse(401 if replay == "nonce_replayed" else 503, replay)
            return None
        _update_peer(peer_id, peer["secret"], home, last_seen=_iso(now))
        return peer, nonce


def _accept_mail(peer: dict[str, Any], body: bytes, *, home: Path | None) -> tuple[int, dict[str, Any]]:
    """Deliver one authenticated peer envelope into a local hub's inbox."""

    envelope = _json_object(body)
    reason = hub_mail.validate_envelope(envelope) if envelope else "json_invalid"
    if reason:
        return 400, {"ok": False, "error": f"envelope_invalid:{reason}"}
    local = hub_mail.local_hubs(home)
    to_hub_id = envelope["to_hub"]["hub_id"]
    if to_hub_id not in local:
        return 404, {"ok": False, "error": "unknown_recipient"}
    _adopt_sender_hub(peer, envelope["from_hub"]["hub_id"], local, home)
    delivery = hub_mail.deliver(body, to_hub_id=to_hub_id, message_id=envelope["message_id"], home=home)
    return 200, {"ok": True, "delivery": delivery, "message_id": envelope["message_id"]}


def _adopt_sender_hub(peer: dict[str, Any], hub_id: str, local: dict[str, str], home: Path | None) -> None:
    """A hub created on the peer after the last refresh is learnt from its first mail.

    Only a hub id nobody claims yet: never a local hub, never another peer's.
    The signature check at read time still decides whether the mail is valid.
    """

    if hub_id in local or len(peer["hubs"]) >= MAX_PEER_HUBS:
        return
    if any(hub["hub_id"] == hub_id for other in list_peers(home) for hub in other["hubs"]):
        return
    _update_peer(peer["machine_id"], peer["secret"], home,
                 hubs=[*peer["hubs"], {"hub_id": hub_id, "repo_name": ""}])


def _server_context(tls_cert: str | None, tls_key: str | None) -> ssl.SSLContext | None:
    if not tls_cert and not tls_key:
        return None
    if not tls_cert or not tls_key:
        raise hub_mail.HubMailError("tls_cert_and_key_required")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(certfile=str(tls_cert), keyfile=str(tls_key))
    except (OSError, ssl.SSLError) as exc:
        raise hub_mail.HubMailError(f"tls_cert_load_failed:{type(exc).__name__}") from exc
    return context


def _validate_bind(bind_host: Any, port: Any) -> tuple[str, int]:
    host = str(bind_host or "").strip()
    if not host or any(ch.isspace() for ch in host):
        raise hub_mail.HubMailError("bind_host_required")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise hub_mail.HubMailError("port_invalid")
    return host, port


def _route_host(ipv6: bool) -> str:
    """The local address the default route uses; a UDP connect sends no packet."""

    family, probe = (socket.AF_INET6, "2001:db8::1") if ipv6 else (socket.AF_INET, "192.0.2.1")
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as udp:
            udp.connect((probe, 9))
            return str(udp.getsockname()[0])
    except OSError:
        return ""


def _routable(host: str, version: int) -> bool:
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return address.version == version and not (address.is_loopback or address.is_unspecified)


def _advertised_host(bind_host: str) -> str:
    """The address peers dial back; "" when a wildcard bind has no routable one.

    Debian/Ubuntu resolve the hostname to 127.0.1.1, so a loopback answer is
    never advertised for a wildcard bind.
    """

    if bind_host not in {"0.0.0.0", "::"}:
        return bind_host
    candidates = [_route_host(bind_host == "::")]
    try:
        candidates.append(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    # A "::" socket is IPv6-only on Windows: it never advertises an IPv4 address.
    version = 6 if bind_host == "::" else 4
    return next((host for host in candidates if _routable(host, version)), "")


def _acquire_listener_lock(home: Path | None) -> int | None:
    """The per-home listener lock (held for the listener's life), or ``None`` when another holds it."""

    path = listener_lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        platform_io.lock_fd(fd, blocking=False)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_listener_lock(fd: int) -> None:
    try:
        platform_io.unlock_fd(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class Listener:
    """One home's listener: HTTP(S) server thread plus the maintenance loop.

    There is no default bind host: the owner names the interface explicitly.
    A lock file under the home admits one active listener; a second contender
    never binds (``start`` raises, or with ``standby_ok`` reports standby).
    """

    def __init__(
        self, *, bind_host: str, port: int, home: Path | None = None,
        tls_cert: str | None = None, tls_key: str | None = None,
        retry_interval: float = RETRY_INTERVAL_SECONDS, start_token: str | None = None,
        discovery_port: int | None = None,
    ) -> None:
        self.bind_host, self.port = _validate_bind(bind_host, port)
        self.home = home
        self.tls_cert, self.tls_key = tls_cert, tls_key
        self.retry_interval = float(retry_interval)
        self.start_token = start_token
        self.discovery_port = discovery_port
        self.state = "new"
        self.record: dict[str, Any] = {}
        self._server: _Server | None = None
        self._discovery: socket.socket | None = None
        self._lock_fd: int | None = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._close_lock = threading.Lock()

    @property
    def address(self) -> str:
        return str(self.record.get("address") or "")

    def start(self, *, standby_ok: bool = False) -> dict[str, Any]:
        lock_fd = _acquire_listener_lock(self.home)
        if lock_fd is None:
            if not standby_ok:
                raise hub_mail.HubMailError("listener_already_running")
            self.state = "standby"
            self._closed.set()
            return {"state": "standby", "bound": False, "port": 0, "active_port": listener_status(self.home)["port"]}
        try:
            record = self._start_locked()
        except BaseException:
            _release_listener_lock(lock_fd)
            raise
        self._lock_fd = lock_fd
        return record

    def _open_discovery(self) -> socket.socket | None:
        """The UDP probe socket; a busy discovery port only disables discovery."""

        if self.discovery_port is None:
            return None
        family = socket.AF_INET6 if ":" in self.bind_host else socket.AF_INET
        udp = socket.socket(family, socket.SOCK_DGRAM)
        try:
            udp.bind((self.bind_host, self.discovery_port))
            udp.settimeout(0.25)
        except OSError:
            udp.close()
            return None
        return udp

    def _start_locked(self) -> dict[str, Any]:
        context = _server_context(self.tls_cert, self.tls_key)
        tls_sha = cert_sha256(self.tls_cert) if context is not None and self.tls_cert else ""
        server_class = _Server6 if ":" in self.bind_host else _Server
        fallback = False
        try:
            server = server_class((self.bind_host, self.port), home=self.home, tls_sha256=tls_sha)
        except OSError:
            if self.port == 0:
                raise
            server = server_class((self.bind_host, 0), home=self.home, tls_sha256=tls_sha)
            fallback = True
        if context is not None:
            server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        port = int(server.server_address[1])
        advertised = _advertised_host(self.bind_host)
        discovery = self._open_discovery()
        self.record = {
            "schema_id": LISTENER_SCHEMA_ID,
            "pid": os.getpid(),
            "start_ticks": runtime_temp.process_start_ticks(os.getpid()),
            "token": self.start_token or secrets.token_hex(16),
            "bind_host": self.bind_host,
            "port": port,
            "requested_port": self.port,
            "port_fallback": fallback,
            "discovery_port": int(discovery.getsockname()[1]) if discovery is not None else 0,
            "address": format_address(advertised, port) if advertised else "",
            "tls": context is not None,
            "tls_cert_sha256": tls_sha,
            "started_at": _iso(_clock()),
        }
        try:
            stop_request_path(self.home).unlink(missing_ok=True)
            _claim_pidfile(self.record, self.home)
        except BaseException:
            server.server_close()
            if discovery is not None:
                discovery.close()
            raise
        self._server, self._discovery = server, discovery
        self.state = "active"
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25},
                                   name="hub-mail-listener", daemon=True)
        looping = threading.Thread(target=self._loop, name="hub-mail-maintenance", daemon=True)
        self._threads = [serving, looping]
        if discovery is not None:
            self._threads.append(threading.Thread(target=self._discovery_loop, args=(discovery, port),
                                                  name="hub-mail-discovery", daemon=True))
        for thread in self._threads:
            thread.start()
        return dict(self.record)

    def _discovery_loop(self, udp: socket.socket, port: int) -> None:
        pairing = _pairing()
        limiter = pairing.RateLimiter(pairing.DISCOVER_RATE_LIMIT, pairing.DISCOVER_RATE_WINDOW_SECONDS)
        while not self._stop.is_set():
            try:
                data, origin = udp.recvfrom(pairing.MAX_DATAGRAM_BYTES + 1)
            except socket.timeout:
                continue
            except OSError:
                # Windows reports an ICMP "port unreachable" for an earlier reply as a recv error.
                if self._stop.wait(0.01) or udp.fileno() == -1:
                    break
                continue
            reply = pairing.discovery_reply(
                data, source=pairing.source_host(str(origin[0])), home=self.home, port=port, limiter=limiter,
            )
            if reply is not None:
                try:
                    udp.sendto(reply, origin)
                except OSError:
                    pass

    def _loop(self) -> None:
        next_run = time.monotonic() + self.retry_interval
        while not self._stop.wait(0.25):
            if stop_request_path(self.home).exists():
                self._stop.set()
                break
            if time.monotonic() >= next_run:
                maintain(self.home)
                next_run = time.monotonic() + self.retry_interval
        threading.Thread(target=self.close, name="hub-mail-close", daemon=True).start()

    def request_stop(self) -> None:
        self._stop.set()

    def wait(self) -> None:
        while not self._closed.wait(0.5):
            pass

    def close(self) -> None:
        # The maintenance loop also closes on a stop request; one close wins.
        with self._close_lock:
            if self._closed.is_set():
                return
            self._stop.set()
            server, self._server = self._server, None
            if server is not None:
                server.shutdown()
                server.server_close()
            discovery, self._discovery = self._discovery, None
            if discovery is not None:
                discovery.close()
            current = threading.current_thread()
            for thread in self._threads:
                if thread is not current:
                    thread.join(timeout=5.0)
            _release_pidfile(self.record, self.home)
            if self._lock_fd is not None:
                _release_listener_lock(self._lock_fd)
                self._lock_fd = None
            self.state = "closed"
            self._closed.set()

    def __enter__(self) -> Listener:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _record_alive(record: dict[str, Any]) -> bool:
    """The pidfile's process is still the listener that wrote it.

    A bare pid is recycled by the OS, so a recorded ``start_ticks`` must match;
    a record without one (older format) falls back to pid liveness.
    """

    pid = record.get("pid")
    if not _pid_alive(pid):
        return False
    ticks = record.get("start_ticks")
    if ticks is None:
        return True
    return runtime_temp.process_start_ticks(int(pid)) == ticks


def _claim_pidfile(record: dict[str, Any], home: Path | None) -> None:
    """One listener per home: the pidfile is created exclusively (0600)."""

    path = pidfile_path(home)
    data = json.dumps(record, sort_keys=True).encode("utf-8")
    for _ in range(2):
        if hub_mail._publish_new(path, data):
            return
        current = _read_json(path)
        if current and _record_alive(current):
            raise hub_mail.HubMailError("listener_already_running")
        path.unlink(missing_ok=True)
    raise hub_mail.HubMailError("listener_already_running")


def _release_pidfile(record: dict[str, Any], home: Path | None) -> None:
    path = pidfile_path(home)
    current = _read_json(path)
    if current and current.get("token") == record.get("token"):
        path.unlink(missing_ok=True)


def listener_status(home: Path | None = None) -> dict[str, Any]:
    record = _read_json(pidfile_path(home)) or {}
    config = _read_json(listener_config_path(home)) or {}
    running = bool(record) and _record_alive(record)
    return {
        "ok": True,
        "schema_id": LISTENER_SCHEMA_ID,
        "running": running,
        "enabled": bool(config.get("enabled")),
        "pid": int(record.get("pid") or 0) if running else 0,
        "bind_host": str(record.get("bind_host") or config.get("bind_host") or ""),
        "port": int(record.get("port") or 0) if running else 0,
        "requested_port": int(record.get("requested_port") or 0) if running else 0,
        "port_fallback": bool(record.get("port_fallback")) if running else False,
        "discovery_port": int(record.get("discovery_port") or 0) if running else 0,
        "address": str(record.get("address") or "") if running else "",
        "tls": bool(record.get("tls")) if running else False,
        "tls_cert_sha256": str(record.get("tls_cert_sha256") or "") if running else "",
        "started_at": str(record.get("started_at") or "") if running else "",
        "stale_pidfile": bool(record) and not running,
    }


def _write_config(home: Path | None, **config: Any) -> None:
    _write_json(listener_config_path(home), {"schema_id": LISTENER_SCHEMA_ID, **config})


def start_listener(
    *, bind_host: str, port: int, home: Path | None = None,
    tls_cert: str | None = None, tls_key: str | None = None,
    wait_seconds: float = LISTENER_START_WAIT_SECONDS,
) -> dict[str, Any]:
    """Start this home's listener process (``python -m aiworkhub.hub_mail_net serve``)."""

    try:
        host, port = _validate_bind(bind_host, port)
        _server_context(tls_cert, tls_key)
    except hub_mail.HubMailError as exc:
        return {"ok": False, "error": str(exc)}
    status = listener_status(home)
    if status["running"]:
        return {**status, "already_running": True}
    root = hub_mail.mail_root(home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_config(home, enabled=True, bind_host=host, port=port,
                  tls_cert=str(tls_cert or ""), tls_key=str(tls_key or ""))
    # A Windows venv python.exe is a redirector that runs the base interpreter as
    # a child, so the listener's pid is not Popen.pid: match a minted token.
    start_token = secrets.token_hex(16)
    argv = [sys.executable, "-m", "aiworkhub.hub_mail_net", "serve", "--host", host,
            "--port", str(port), "--home", str(home or Path.home()), "--start-token", start_token]
    if tls_cert and tls_key:
        argv += ["--tls-cert", str(tls_cert), "--tls-key", str(tls_key)]
    env = dict(os.environ)
    package_parent = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (package_parent, env.get("PYTHONPATH", "")) if p)
    options: dict[str, Any] = {}
    if platform_io.is_windows():
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    try:
        with listener_log_path(home).open("ab") as log:
            process = subprocess.Popen(  # noqa: S603 - argv list, no shell
                argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                cwd=str(root), env=env, close_fds=True, **options,
            )
    except OSError as exc:
        return {"ok": False, "error": f"listener_spawn_failed:{type(exc).__name__}"}
    _SPAWNED.append(process)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        record = _read_json(pidfile_path(home)) or {}
        if record.get("token") == start_token and _record_alive(record):
            return {**listener_status(home), "already_running": False}
        code = process.poll()
        if code == 0 and listener_status(home)["running"]:
            return {**listener_status(home), "already_running": True}
        if code is not None:
            return {"ok": False, "error": f"listener_start_failed:exit_{code}",
                    "log": str(listener_log_path(home))}
        time.sleep(0.1)
    process.terminate()
    if (_read_json(pidfile_path(home)) or {}).get("token") == start_token:
        # The redirector's base-interpreter child may outlive it: ask it to stop.
        hub_mail._write_over(stop_request_path(home), b"stop")
    return {"ok": False, "error": "listener_start_timeout", "log": str(listener_log_path(home))}


def stop_listener(home: Path | None = None, *, wait_seconds: float = LISTENER_STOP_WAIT_SECONDS) -> dict[str, Any]:
    """Stop this home's listener and persist enabled=false.

    A signal is sent only to a pid whose recorded start identity still matches;
    a recycled or unknown identity is never killed.
    """

    config = _read_json(listener_config_path(home)) or {}
    config.pop("schema_id", None)
    _write_config(home, **{**config, "enabled": False})
    status = listener_status(home)
    if not status["running"]:
        if status["stale_pidfile"]:
            pidfile_path(home).unlink(missing_ok=True)
        return {**listener_status(home), "stopped": False}
    pid = status["pid"]
    stop_file = stop_request_path(home)
    hub_mail._write_over(stop_file, b"stop")
    deadline = time.monotonic() + wait_seconds
    try:
        while time.monotonic() < deadline and listener_status(home)["running"]:
            time.sleep(0.1)
        record = _read_json(pidfile_path(home)) or {}
        if listener_status(home)["running"] and pid != os.getpid() and int(record.get("pid") or 0) == pid:
            ticks = record.get("start_ticks")
            if ticks is not None and runtime_temp.process_start_ticks(pid) == ticks:
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    if not _pid_alive(pid):
                        break
                    time.sleep(0.1)
                if not _pid_alive(pid):
                    pidfile_path(home).unlink(missing_ok=True)
            elif ticks is not None:
                pidfile_path(home).unlink(missing_ok=True)
    finally:
        stop_file.unlink(missing_ok=True)
    final = listener_status(home)
    if final["running"]:
        return {**final, "ok": False, "error": "listener_stop_failed", "stopped": False}
    return {**final, "stopped": True}


def mark_enabled(home: Path | None = None) -> bool:
    """Persist enabled=true at first use; an existing config (even enabled=false) stays the owner's word."""

    if listener_config_path(home).exists():
        return False
    hub_mail.mail_root(home).mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_config(home, enabled=True)
    return True


def ensure_listener(home: Path | None = None) -> dict[str, Any] | None:
    """Start the listener once Hub Mail is enabled; a fresh install opens no port."""

    config = _read_json(listener_config_path(home)) or {}
    if not config.get("enabled"):
        return None
    status = listener_status(home)
    if status["running"]:
        return status
    key = str(hub_mail.mail_root(home))
    if time.monotonic() - _ENSURE_ATTEMPTS.get(key, -ENSURE_BACKOFF_SECONDS) < ENSURE_BACKOFF_SECONDS:
        return status
    _ENSURE_ATTEMPTS[key] = time.monotonic()
    port = config.get("port")
    if isinstance(port, bool) or not isinstance(port, int):
        port = _pairing().default_port()
    return start_listener(
        bind_host=str(config.get("bind_host") or "") or DEFAULT_BIND_HOST, port=port, home=home,
        tls_cert=str(config.get("tls_cert") or "") or None, tls_key=str(config.get("tls_key") or "") or None,
    )


# ---------------------------------------------------------------- code-free pairing (hub_mail_pairing)


def _pairing() -> Any:
    from . import hub_mail_pairing

    return hub_mail_pairing


def discover(timeout: float | None = None, targets: Any = None, *, home: Path | None = None) -> dict[str, Any]:
    """Probe the LAN (or explicit ``targets``) for hubs; replies are hints, never trusted."""

    options = {} if timeout is None else {"timeout": timeout}
    return _pairing().discover(targets=targets, home=home, **options)


def pair(address: str, *, home: Path | None = None, tls: bool = False) -> dict[str, Any]:
    """Start code-free pairing with the node at ``address``; compare the returned SAS."""

    return _pairing().pair(address, home=home, tls=tls)


def pair_requests(home: Path | None = None) -> dict[str, Any]:
    return _pairing().pair_requests(home)


def accept(request_id: str, *, home: Path | None = None) -> dict[str, Any]:
    return _pairing().accept(request_id, home=home)


def reject(request_id: str, *, home: Path | None = None) -> dict[str, Any]:
    return _pairing().reject(request_id, home=home)


def listener_action(
    action: str, *, bind_host: str | None = None, port: int | None = None,
    tls_cert: str | None = None, tls_key: str | None = None, home: Path | None = None,
) -> dict[str, Any]:
    if action == "status":
        return listener_status(home)
    if action == "stop":
        return stop_listener(home)
    if action == "start":
        if not bind_host:
            return {"ok": False, "error": "bind_host_required"}
        if port is None:
            return {"ok": False, "error": "port_required"}
        return start_listener(bind_host=bind_host, port=port, home=home, tls_cert=tls_cert, tls_key=tls_key)
    return {"ok": False, "error": "listener_action_invalid"}


# ---------------------------------------------------------------- CLI


def serve(
    *, bind_host: str, port: int, home: Path | None, tls_cert: str | None, tls_key: str | None,
    start_token: str | None = None,
) -> int:
    listener = Listener(bind_host=bind_host, port=port, home=home, tls_cert=tls_cert, tls_key=tls_key,
                        start_token=start_token, discovery_port=_pairing().default_port())
    try:
        record = listener.start(standby_ok=True)
    except (hub_mail.HubMailError, OSError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{exc}"}), flush=True)
        return 2
    print(json.dumps({"ok": True, "listener": {k: v for k, v in record.items() if k != "token"}}), flush=True)
    if listener.state == "standby":
        return 0
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: listener.request_stop())
    try:
        listener.wait()
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aiworkhub.hub_mail_net")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve", help="run this home's WorkHub Mail listener")
    serve_parser.add_argument("--host", required=True, help="explicit bind host; there is no default")
    serve_parser.add_argument("--port", required=True, type=int)
    serve_parser.add_argument("--home", default=None)
    serve_parser.add_argument("--tls-cert", default=None)
    serve_parser.add_argument("--tls-key", default=None)
    serve_parser.add_argument("--start-token", default=None,
                              help="token start_listener matches in the pidfile (never the spawned pid)")
    args = parser.parse_args(argv)
    return serve(
        bind_host=args.host, port=args.port, home=Path(args.home) if args.home else None,
        tls_cert=args.tls_cert, tls_key=args.tls_key, start_token=args.start_token,
    )


if __name__ == "__main__":
    sys.exit(main())
