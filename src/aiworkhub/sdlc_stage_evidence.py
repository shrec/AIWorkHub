"""Server-resolved evidence gate for SDLC stage transitions (NF-2026-00945).

A stage is ``ready`` only when this module proves it from native,
repository-bound receipts. A caller's payload contributes the structured
content the spec asks a person or model to write -- the Plan's intent, the
Design's criteria -- and, for Build and Test, exact identity pointers that must
equal what the canonical stores already say. It never contributes a verdict:
approval, task binding, the current candidate, and validation/acceptance
provenance are read here, read-only and bounded, from the repository's own
canonical task store. Nothing here writes, spawns a process, or calls a
provider.

What proves each stage:

plan      the case's canonical task exists, is not withdrawn, and carries a
          non-empty objective -- the approved intent the manager recorded.
design    that task's contract is falsifiable (acceptance criteria, validation
          commands, and a write scope unless read-only); its
          ``task_store.card_content_identity`` is the versioned design.
build     the current claim sealed a ``review_ready`` candidate -- request
          identity, workspace base, changed-path hashes, attempt manifest -- on
          the claimed route, against exactly that design version; the attempt
          bundle that manifest pins re-hashes and names the effective route;
          the request's terminal process event agrees with both; and its
          semantic-edit ledger and effective effort/context receipt verify,
          or are verifiably not owed by that task type or adapter.
test      the sealed validation evidence re-derives, through
          ``task_fsm.deterministic_verification``, to the stored passing
          verdict; the coordinator's accepted-outcome receipt for that same
          candidate passes ``task_engine``'s canonical validator, which
          re-hashes the promoted paths so a later edit makes the candidate
          stale; and its ``accept_review`` event exists.
deploy    no target allowlist, release/build provenance, install, rollback or
maintain  approval-policy producer exists, nor observed outcome metrics or a
          control-limit policy. The gate names each missing producer (see
          ``MISSING_PRODUCERS``) and never infers a pass. Maintain still
          resolves what is available -- the accepted candidate and its
          learning disposition -- into its refusal.

``not_applicable`` needs a verifiable policy. No canonical policy registry
exists, so every such claim is refused with that producer named rather than
letting an arbitrary ``reason``/``policy_ref`` skip a stage.

Resolution is split so the store can keep proof outside its write lease: a
``StageDecision`` carries a fingerprint of the card-only identity it was drawn
from, and ``identity_unchanged`` re-derives just that identity -- one row read,
no file hashing -- immediately before the store commits a receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import attempt_artifacts, task_fsm, task_store
from .sqlite_readonly import connect_readonly

SCHEMA_ID = "aiworkhub.sdlc_stage_evidence.v1"
REFUSAL_PREFIX = "stage_evidence_refused"

MAX_TASK_CARD_CHARS = 4 * 1024 * 1024
MAX_CONTENT_CHARS = 2048
MAX_CONTENT_ITEMS = 32
MAX_EVIDENCE_REFS = 32
MAX_EVIDENCE_REF_CHARS = 256
MAX_IDENTITY_CHARS = 256
MAX_CANDIDATE_PATHS = 512
MAX_PATH_CHARS = 1024
MAX_VALIDATION_RECORDS = 256
MAX_PROMOTED_FILE_BYTES = 16 * 1024 * 1024
MAX_ACCEPT_EVENTS = 16
MAX_SESSION_TASKS = 8
MAX_TOKEN_CHARS = 64

WITHDRAWN_STATUSES = frozenset({"archived", "superseded"})
SEALED_SUBSTATUS = "review_ready"

PLAN_TEXT_FIELDS = ("intent", "problem", "owner", "expected_outcome", "risk")
# Minimum item count of each Design list; zero still requires the list itself.
DESIGN_LIST_FIELDS = {
    "acceptance_criteria": 1,
    "constraints": 0,
    "affected_contracts": 1,
    "alternatives": 0,
}
IDENTITY_POINTERS = ("task_id", "request_id", "claim_epoch")
# A stage that certifies one candidate must name it; the server then proves the
# name, so an empty payload can never certify whatever happens to be current.
REQUIRED_POINTERS: dict[str, tuple[str, ...]] = {
    "build": IDENTITY_POINTERS,
    "test": IDENTITY_POINTERS,
}
READY_FIELDS: dict[str, frozenset[str]] = {
    "plan": frozenset({*PLAN_TEXT_FIELDS, "evidence_refs", "task_id"}),
    "design": frozenset({*DESIGN_LIST_FIELDS, "evidence_refs", "task_id"}),
    "build": frozenset({*IDENTITY_POINTERS, "evidence_refs"}),
    "test": frozenset({*IDENTITY_POINTERS, "evidence_refs"}),
    "deploy": frozenset({"task_id", "request_id", "target", "evidence_refs"}),
    "maintain": frozenset({"task_id", "request_id", "evidence_refs"}),
}
# Verdict- or proof-shaped keys: what only the server may conclude or resolve.
SELF_ATTESTED_FIELDS = frozenset({
    "accepted", "approved", "complete", "completed", "digest", "done", "green",
    "hash", "ok", "outcome", "pass", "passed", "ready", "receipt", "receipt_id",
    "receipts", "result", "sha256", "state", "status", "success", "verdict",
    "verified",
})
SELF_ATTESTED_SUFFIXES = (
    "_digest", "_hash", "_passed", "_receipt", "_receipts", "_sha256", "_verified",
)
EVIDENCE_REF_SCHEMES = frozenset({"file", "https", "needfix", "roadmap", "task"})
_REF_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")

# Producers the approved six-stage contract needs and this repository does not
# have yet: the exact target list for the dependent deployment/Maintain card.
MISSING_PRODUCERS: dict[str, tuple[str, ...]] = {
    "deploy": (
        "deploy_target_allowlist",
        "release_build_provenance_receipt",
        "install_receipt",
        "rollback_receipt",
        "deploy_approval_policy",
    ),
    "maintain": (
        "deployed_release_identity",
        "observed_outcome_metrics",
        "control_limit_policy",
    ),
    "not_applicable": ("sdlc_not_applicable_policy_registry",),
}
# The launcher's own receipt schemas a proven Build is joined through.
ATTEMPT_BUNDLE_RECEIPT_SCHEMA_ID = "aiworkhub.attempt_artifact_bundle_receipt.v1"
SEMANTIC_EDIT_RUNTIME_SCHEMA_ID = "aiworkhub.semantic_edit_runtime_evidence.v1"
MAX_ATTEMPT_MANIFEST_BYTES = 256 * 1024
MAX_REQUEST_EVENTS = 64
MAX_EDIT_RECEIPTS = 128

# Each stage's refused binding to what an earlier, re-proven stage recorded.
PREDECESSOR_BINDINGS: dict[str, tuple[tuple[str, str], ...]] = {
    "design": (("plan", "task_id"),),
    "build": (("design", "task_id"), ("design", "contract_sha256")),
    "test": (("build", "task_id"), ("build", "candidate_sha256")),
    "maintain": (("test", "candidate_sha256"), ("test", "accepted_outcome_receipt_id")),
}

NEXT_ACTIONS: dict[str, str] = {
    "plan": (
        "record plan with intent, problem, owner, expected_outcome, risk and "
        "evidence_refs on a case bound to a canonical task whose objective is set"
    ),
    "design": (
        "give the bound task a falsifiable contract (acceptance, validation, "
        "allowed_writes) and record design with acceptance_criteria, constraints, "
        "affected_contracts and alternatives"
    ),
    "build": (
        "let the claimed worker seal a review_ready candidate under the current "
        "contract, then record build naming its task_id, request_id and claim_epoch"
    ),
    "test": (
        "have the coordinator accept that sealed candidate with passing validation, "
        "then record test naming it while its promoted paths are unchanged"
    ),
    "deploy": (
        "no deploy producer exists: a deployment-policy card must add a target "
        "allowlist and release, install and rollback receipts first"
    ),
    "maintain": (
        "maintain needs a deployed release, observed outcome metrics and a "
        "control-limit policy; none has a producer yet"
    ),
    "binding": (
        "bind the case to its canonical task (aiworkhub_manager_sdlc_case_create_for_task) "
        "before recording this stage"
    ),
    "store": (
        "restore this repository's canonical task store; evidence from another "
        "repository never counts"
    ),
    "not_applicable": (
        "no canonical not_applicable policy registry exists; record the stage "
        "when its evidence exists instead of skipping it"
    ),
}
_BINDING_CODES = frozenset({"approval_authority_missing", "case_not_task_bound"})
_STORE_CODES = frozenset({
    "task_store_not_ready", "task_store_unreadable", "cross_repository_evidence",
})


@dataclass(frozen=True)
class StageDecision:
    """The server's conclusion for one requested stage transition.

    ``code`` is empty only when the stage is proven. ``evidence`` is the
    canonical, bounded proof the store persists for a proven stage, or the
    partial detail behind a refusal. ``fingerprint`` digests the card-only
    identity the proof was drawn from, for the store's pre-commit recheck.
    """

    stage: str
    code: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    next_action: str = ""

    @property
    def ready(self) -> bool:
        return not self.code

    @property
    def reason(self) -> str:
        return f"{REFUSAL_PREFIX}:{self.stage}:{self.code}"

    def message(self) -> str:
        return f"{self.reason}; next_action={self.next_action}"


@dataclass(frozen=True)
class TaskSnapshot:
    """One canonical task row as the recorder sees it: SQL lifecycle plus card."""

    task_id: str
    runner: str
    objective: str
    status: str
    claimed_by: str
    created_at: str
    card: dict[str, Any]


def canonical_evidence(value: Mapping[str, Any]) -> tuple[str, str]:
    """Canonical JSON text of ``value`` and the SHA-256 hex of exactly that text.

    The serialisation matches the accepted-outcome receipt's own, so a Build's
    manifest digest is directly comparable to ``attempt_artifact_manifest_id``.
    """

    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def next_action(stage: str, code: str) -> str:
    """The actionable step a typed refusal points at."""

    if code in _BINDING_CODES:
        return NEXT_ACTIONS["binding"]
    if code in _STORE_CODES:
        return NEXT_ACTIONS["store"]
    if code.startswith("not_applicable"):
        return NEXT_ACTIONS["not_applicable"]
    return NEXT_ACTIONS[stage]


def _refusal(stage: str, code: str, **detail: Any) -> StageDecision:
    return StageDecision(
        stage=stage, code=code, evidence=detail, next_action=next_action(stage, code)
    )


class EvidenceReader:
    """One bounded, read-only resolution session over the canonical task store.

    Each task row is read at most once per session, so every stage a session
    judges -- the six packets of one case read, or a transition plus the
    predecessors it re-proves -- is judged against one snapshot.
    """

    def __init__(self, root: Path, repo_id: str) -> None:
        self.root = Path(root)
        self.repo_id = repo_id
        self._database: Path | str | None = None
        self._tasks: dict[str, TaskSnapshot | str] = {}

    def database(self) -> Path | str:
        """This repository's canonical task database, or a refusal code."""

        if self._database is None:
            try:
                readiness = task_store.storage_readiness(self.root)
            except (task_store.TaskStoreError, OSError, sqlite3.Error, ValueError):
                self._database = "task_store_not_ready"
            else:
                if not readiness.ready:
                    self._database = "task_store_not_ready"
                elif readiness.repo_id != self.repo_id:
                    self._database = "cross_repository_evidence"
                else:
                    self._database = Path(readiness.canonical_db)
        return self._database

    def _rows(
        self, sql: str, params: tuple[Any, ...], *, absent_table_ok: bool = False
    ) -> list[sqlite3.Row] | str:
        database = self.database()
        if isinstance(database, str):
            return database
        try:
            conn = connect_readonly(database)
        except (sqlite3.Error, OSError, ValueError):
            return "task_store_unreadable"
        try:
            conn.row_factory = sqlite3.Row
            return list(conn.execute(sql, params).fetchall())
        except sqlite3.OperationalError as exc:
            if absent_table_ok and "no such table" in str(exc):
                return []
            return "task_store_unreadable"
        except sqlite3.Error:
            return "task_store_unreadable"
        finally:
            conn.close()

    def task(self, task_id: str) -> TaskSnapshot | str:
        """This session's snapshot of one canonical task, or a refusal code."""

        known = self._tasks.get(task_id)
        if known is not None:
            return known
        if len(self._tasks) >= MAX_SESSION_TASKS:
            self._tasks.clear()
        rows = self._rows(
            "SELECT task_id, runner, objective, status, worker_status, "
            "claimed_by, created_at, archived_at, "
            "CASE WHEN length(card_json) <= ? THEN card_json END AS card_json "
            "FROM tasks WHERE task_id=?",
            (MAX_TASK_CARD_CHARS, task_id),
        )
        snapshot = _snapshot(rows)
        self._tasks[task_id] = snapshot
        return snapshot

    def accept_event(self, task_id: str, request_id: str, receipt_id: str) -> int | str:
        """The ``accept_review`` event that recorded exactly this receipt."""

        rows = self._rows(
            "SELECT event_id, payload_json FROM task_events "
            "WHERE task_id=? AND event='accept_review' AND length(payload_json) <= ? "
            "ORDER BY event_id DESC LIMIT ?",
            (task_id, MAX_TASK_CARD_CHARS, MAX_ACCEPT_EVENTS),
        )
        if isinstance(rows, str):
            return rows
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("request_id") != request_id:
                continue
            receipt = payload.get("accepted_outcome_receipt")
            if isinstance(receipt, dict) and receipt.get("receipt_id") == receipt_id:
                return int(row["event_id"])
        return "acceptance_event_missing"

    def learning(self, task_id: str, request_id: str) -> dict[str, Any]:
        """The learning disposition recorded for one accepted request, if any."""

        rows = self._rows(
            "SELECT disposition, reason, commit_id FROM learning_dispositions "
            "WHERE task_id=? AND request_id=?",
            (task_id, request_id),
            absent_table_ok=True,
        )
        if isinstance(rows, str):
            return {"state": "unknown", "reason": rows}
        if rows:
            return {
                "state": "recorded",
                "source": "learning_dispositions",
                "disposition": _token(rows[0]["disposition"]),
                "reason": _token(rows[0]["reason"]),
                "commit_id": _token(rows[0]["commit_id"]),
            }
        rows = self._rows(
            "SELECT commit_id, outcome, state FROM learning_commits "
            "WHERE task_id=? AND request_id=?",
            (task_id, request_id),
            absent_table_ok=True,
        )
        if isinstance(rows, str):
            return {"state": "unknown", "reason": rows}
        if rows:
            return {
                "state": "recorded",
                "source": "learning_commits",
                "commit_id": _token(rows[0]["commit_id"]),
                "outcome": _token(rows[0]["outcome"]),
                "commit_state": _token(rows[0]["state"]),
            }
        return {"state": "unknown", "reason": "learning_disposition_missing"}

    def _process_path(self, env: str, default: Path) -> Path:
        # The launcher's own resolution, so the gate reads what it wrote.
        return Path(os.environ.get(env) or self.root / default)

    def attempt_bundle(self, request_id: str, receipt: Any) -> dict[str, Any] | str:
        """The sealed attempt bundle, re-hashed against the card's receipt, or why not.

        Only the metadata, diff and validation roles are parsed, each from bytes
        whose digest was just matched to the manifest the card pins.
        """

        from . import process_launcher

        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_id") != ATTEMPT_BUNDLE_RECEIPT_SCHEMA_ID
            or not isinstance(receipt.get("manifest_sha256"), str)
            or not _SHA256_HEX.fullmatch(receipt["manifest_sha256"])
        ):
            return "attempt_bundle_receipt_missing"
        if receipt.get("attempt_id") != request_id or not _REF_IDENTIFIER.fullmatch(request_id):
            return "attempt_bundle_identity_mismatch"
        process_dir = self._process_path(
            process_launcher.PROCESS_DIR_ENV, process_launcher.PROCESS_DIR_DEFAULT_REL
        )
        bundle_dir = process_dir / "attempt-artifacts" / request_id
        manifest_path = bundle_dir / attempt_artifacts.MANIFEST_FILENAME
        recorded = receipt.get("manifest_path")
        try:
            if not isinstance(recorded, str) or (
                Path(recorded).resolve() != manifest_path.resolve()
            ):
                return "attempt_bundle_foreign"
        except (OSError, RuntimeError, ValueError):
            return "attempt_bundle_foreign"
        raw = _read_bounded(manifest_path, MAX_ATTEMPT_MANIFEST_BYTES)
        if isinstance(raw, str):
            return raw
        if hashlib.sha256(raw).hexdigest() != receipt["manifest_sha256"]:
            return "attempt_bundle_stale"
        try:
            attempt_artifacts.verify_json_bundle(bundle_dir)
            manifest = attempt_artifacts.parse_manifest_json(raw.decode("utf-8"))
        except (attempt_artifacts.InvalidManifestError, attempt_artifacts.InvalidArtifactError,
                OSError, UnicodeDecodeError, ValueError):
            return "attempt_bundle_invalid"
        if manifest.attempt_id != request_id:
            return "attempt_bundle_identity_mismatch"
        entries = {entry.role: entry for entry in manifest.artifacts}
        bundle: dict[str, Any] = {"manifest_sha256": receipt["manifest_sha256"]}
        for role in ("metadata", "diff", "validation"):
            entry = entries.get(role)
            if entry is None:
                return "attempt_bundle_invalid"
            data = _read_bounded(bundle_dir / entry.path, attempt_artifacts.MAX_ARTIFACT_BYTES)
            if isinstance(data, str):
                return data
            if len(data) != entry.byte_count or hashlib.sha256(data).hexdigest() != entry.sha256:
                return "attempt_bundle_stale"
            try:
                payload = json.loads(data)
            except (UnicodeDecodeError, ValueError, RecursionError):
                return "attempt_bundle_invalid"
            if not isinstance(payload, dict):
                return "attempt_bundle_invalid"
            bundle[role] = payload
        return bundle

    def terminal_event(self, task_id: str, request_id: str) -> dict[str, Any] | str:
        """The launcher's ``review_ready`` terminal event for exactly this request."""

        from . import process_event_ledger, process_launcher

        log_path = self._process_path(
            process_launcher.PROCESS_LOG_ENV, process_launcher.PROCESS_LOG_DEFAULT_REL
        )
        try:
            rows = process_event_ledger.events_for_requests(log_path, [request_id])
        except (OSError, ValueError, RecursionError):
            return "process_ledger_unreadable"
        sealed = [
            row
            for row in rows.get(request_id, [])[-MAX_REQUEST_EVENTS:]
            if row.get("state") == SEALED_SUBSTATUS
        ]
        if not sealed:
            return "build_terminal_event_missing"
        event = sealed[-1]
        if event.get("task_id") != task_id:
            return "build_terminal_event_foreign"
        try:
            if len(canonical_evidence(event)[0]) > MAX_TASK_CARD_CHARS:
                return "evidence_oversized:terminal_event"
        except (TypeError, ValueError, RecursionError):
            return "build_terminal_event_malformed"
        return event


def _read_bounded(path: Path, limit: int) -> bytes | str:
    """Bytes of one regular, non-symlink file no larger than ``limit``."""

    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return "attempt_bundle_missing"
        if info.st_size > limit:
            return "evidence_oversized:attempt_bundle"
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except FileNotFoundError:
        return "attempt_bundle_missing"
    except OSError:
        return "attempt_bundle_invalid"
    return data if len(data) <= limit else "evidence_oversized:attempt_bundle"


def _snapshot(rows: list[sqlite3.Row] | str) -> TaskSnapshot | str:
    if isinstance(rows, str):
        return rows
    if not rows:
        return "task_missing"
    row = rows[0]
    if row["card_json"] is None:
        return "evidence_oversized:task_card"
    try:
        card = json.loads(row["card_json"])
    except (TypeError, ValueError):
        return "task_card_malformed"
    if not isinstance(card, dict):
        return "task_card_malformed"
    objective = row["objective"] or card.get("objective")
    return TaskSnapshot(
        task_id=str(row["task_id"]),
        runner=str(row["runner"] or ""),
        objective=objective if isinstance(objective, str) else "",
        status=task_store.canonical_status(
            {key: row[key] for key in ("status", "worker_status", "archived_at")}
        ),
        claimed_by=str(row["claimed_by"] or ""),
        created_at=str(row["created_at"] or ""),
        card=card,
    )


# --------------------------------------------------------------------------- #
# payload: structured content and identity pointers, never a verdict
# --------------------------------------------------------------------------- #


def _token(value: Any) -> str:
    """A bounded, printable rendering of a value for a typed code or detail."""

    text = value if isinstance(value, str) else ("" if value is None else type(value).__name__)
    cleaned = "".join(
        char if char.isprintable() and not char.isspace() else "_"
        for char in text[:MAX_TOKEN_CHARS]
    )
    return cleaned or "none"


def _has_control(text: str) -> bool:
    return any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in text)


def _identity_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= MAX_IDENTITY_CHARS
        and not _has_control(value)
    )


def _content_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= MAX_CONTENT_CHARS
        and "\x00" not in value
    )


def _safe_relative(path: str) -> bool:
    """A repository-relative POSIX path that cannot name anything outside it."""

    if not path or len(path) > MAX_PATH_CHARS or "\\" in path or _has_control(path):
        return False
    if path.startswith("/") or _DRIVE_PREFIX.match(path):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _ref_refusal(ref: Any) -> str:
    if not isinstance(ref, str):
        return "evidence_ref_malformed"
    if len(ref) > MAX_EVIDENCE_REF_CHARS:
        return "evidence_ref_oversized"
    scheme, _, value = ref.partition(":")
    if (
        scheme not in EVIDENCE_REF_SCHEMES
        or not value
        or any(not char.isprintable() or char.isspace() for char in ref)
    ):
        return "evidence_ref_malformed"
    if scheme == "file":
        return "" if _safe_relative(value) else "evidence_ref_path_traversal"
    if scheme == "https":
        return "" if value.startswith("//") and len(value) > 2 else "evidence_ref_malformed"
    return "" if _REF_IDENTIFIER.fullmatch(value) else "evidence_ref_malformed"


def _refs_refusal(refs: Any, *, required: bool) -> str:
    if refs is None:
        return "evidence_refs_missing" if required else ""
    if not isinstance(refs, list):
        return "evidence_ref_malformed"
    if len(refs) > MAX_EVIDENCE_REFS:
        return "evidence_ref_oversized"
    if required and not refs:
        return "evidence_refs_missing"
    for ref in refs:
        code = _ref_refusal(ref)
        if code:
            return code
    return ""


def _pointer_valid(key: str, value: Any) -> bool:
    if key == "claim_epoch":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return _identity_text(value)


def _content_refusal(stage: str, payload: Mapping[str, Any]) -> str:
    if stage == "plan":
        for name in PLAN_TEXT_FIELDS:
            if name not in payload:
                return f"plan_content_missing:{name}"
            if not _content_text(payload[name]):
                return f"plan_content_invalid:{name}"
    if stage == "design":
        for name, minimum in DESIGN_LIST_FIELDS.items():
            items = payload.get(name)
            if name not in payload:
                return f"design_content_missing:{name}"
            if (
                not isinstance(items, list)
                or not minimum <= len(items) <= MAX_CONTENT_ITEMS
                or not all(_content_text(item) for item in items)
            ):
                return f"design_content_invalid:{name}"
    if "target" in payload and not _identity_text(payload["target"]):
        return "deploy_target_malformed"
    return ""


def payload_refusal(stage: str, payload: Mapping[str, Any]) -> StageDecision | None:
    """Refuse a ``ready`` payload that asserts, omits or malforms what it may carry.

    Checked before anything is resolved so a verdict-shaped key is refused as
    such at every stage, whatever the state of the stage's predecessors.
    """

    allowed = READY_FIELDS[stage]
    for key in payload:
        if not isinstance(key, str):
            return _refusal(stage, "payload_field_malformed")
        if key in allowed:
            continue
        name = key.strip().lower()
        if name in SELF_ATTESTED_FIELDS or name.endswith(SELF_ATTESTED_SUFFIXES):
            return _refusal(stage, f"self_attested_verdict:{_token(key)}")
        return _refusal(stage, f"payload_field_not_allowed:{_token(key)}")
    for key in REQUIRED_POINTERS.get(stage, ()):
        if key not in payload:
            return _refusal(stage, f"identity_pointer_missing:{key}")
    for key in IDENTITY_POINTERS:
        if key in payload and not _pointer_valid(key, payload[key]):
            return _refusal(stage, f"identity_pointer_malformed:{key}")
    code = _content_refusal(stage, payload) or _refs_refusal(
        payload.get("evidence_refs"), required=stage == "plan"
    )
    return _refusal(stage, code) if code else None


def refuse_not_applicable(stage: str, payload: Mapping[str, Any]) -> StageDecision:
    """No canonical policy can vouch for skipping a stage yet; name that producer."""

    return _refusal(
        stage,
        "not_applicable_policy_unverifiable",
        policy_ref=_token(payload.get("policy_ref")),
        missing_producers=list(MISSING_PRODUCERS["not_applicable"]),
    )


# --------------------------------------------------------------------------- #
# canonical identity: card-only, cheap enough to re-derive inside a write lease
# --------------------------------------------------------------------------- #


def _card_strings(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        return []
    return value


def _contract_sha256(card: Mapping[str, Any]) -> str:
    try:
        return task_store.card_content_identity(card)
    except (TypeError, ValueError, RecursionError):
        return ""


def _plan_identity(snapshot: TaskSnapshot) -> dict[str, Any] | str:
    objective = snapshot.objective.strip()
    if not objective:
        return "approved_intent_missing"
    return {
        "task_id": snapshot.task_id,
        "task_created_at": _token(snapshot.created_at),
        "objective_sha256": hashlib.sha256(objective.encode("utf-8")).hexdigest(),
    }


def _design_identity(snapshot: TaskSnapshot) -> dict[str, Any] | str:
    card = snapshot.card
    acceptance = _card_strings(card.get("acceptance"))
    validation = _card_strings(card.get("validation"))
    writes = _card_strings(card.get("allowed_writes"))
    read_only = card.get("read_only") is True
    if not acceptance:
        return "design_acceptance_missing"
    if not validation:
        return "design_validation_missing"
    if not writes and not read_only:
        return "design_write_scope_missing"
    contract = _contract_sha256(card)
    if not contract:
        return "task_card_malformed"
    return {
        "task_id": snapshot.task_id,
        "contract_sha256": contract,
        "acceptance_count": len(acceptance),
        "validation_count": len(validation),
        "write_scope_count": len(writes),
        "read_only": read_only,
    }


def _paths_refusal(paths: Any) -> str:
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        return "candidate_paths_malformed"
    if len(paths) > MAX_CANDIDATE_PATHS:
        return "evidence_oversized:candidate_paths"
    if len(set(paths)) != len(paths):
        return "candidate_paths_malformed"
    if not all(_safe_relative(path) for path in paths):
        return "evidence_path_traversal"
    return ""


def _candidate_identity(snapshot: TaskSnapshot) -> dict[str, Any] | str:
    """The sealed candidate of the task's current claim, or why there is none."""

    card = snapshot.card
    epoch = card.get("claim_epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        return "claim_missing"
    terminal = card.get("terminal_review")
    if not isinstance(terminal, dict):
        return "candidate_not_sealed:none"
    if terminal.get("substatus") != SEALED_SUBSTATUS:
        return f"candidate_not_sealed:{_token(terminal.get('substatus'))}"
    if terminal.get("claim_epoch") != epoch:
        return "claim_epoch_mismatch"
    runner = snapshot.runner
    if not runner or terminal.get("runner") != runner or snapshot.claimed_by != runner:
        return "route_mismatch"
    sealed = terminal.get("evidence")
    if not isinstance(sealed, dict):
        return "candidate_evidence_missing"
    identity = sealed.get("request_identity")
    request_id = identity.get("request_id") if isinstance(identity, dict) else None
    if not _identity_text(request_id):
        return "candidate_request_identity_missing"
    workspace = sealed.get("workspace")
    base_oid = workspace.get("base_oid") if isinstance(workspace, dict) else None
    if not _identity_text(base_oid):
        return "workspace_identity_missing"
    paths = sealed.get("changed_paths")
    code = _paths_refusal(paths)
    if code:
        return code
    hashes = sealed.get("changed_path_hashes")
    if (
        not isinstance(hashes, dict)
        or set(hashes) != set(paths)
        or any(
            value is not None and not (isinstance(value, str) and _SHA256_HEX.fullmatch(value))
            for value in hashes.values()
        )
    ):
        return "candidate_hashes_invalid"
    manifest = sealed.get("attempt_artifact_manifest")
    if not isinstance(manifest, dict):
        return "attempt_manifest_missing"
    contract = _contract_sha256(card)
    if not contract or terminal.get("card_content_sha256") != contract:
        return "candidate_contract_mismatch"
    manifest_sha256 = canonical_evidence(manifest)[1]
    candidate = {
        "task_id": snapshot.task_id,
        "request_id": request_id,
        "claim_epoch": epoch,
        "base_oid": base_oid,
        "changed_path_hashes": hashes,
        "attempt_artifact_manifest_sha256": manifest_sha256,
        "contract_sha256": contract,
    }
    return {
        "task_id": snapshot.task_id,
        "request_id": request_id,
        "claim_epoch": epoch,
        "route": {"runner": runner, "adapter_id": _token(terminal.get("adapter_id"))},
        "base_oid": base_oid,
        "changed_path_count": len(paths),
        "changed_paths_sha256": canonical_evidence(hashes)[1],
        "attempt_artifact_manifest_sha256": manifest_sha256,
        "contract_sha256": contract,
        "candidate_sha256": canonical_evidence(candidate)[1],
    }


def _acceptance_identity(
    snapshot: TaskSnapshot, candidate: Mapping[str, Any]
) -> dict[str, Any] | str:
    """The coordinator's acceptance of exactly ``candidate``, or why not."""

    if snapshot.status != "finished":
        return f"candidate_not_accepted:{snapshot.status}"
    card = snapshot.card
    if card.get("accepted_request_id") != candidate["request_id"]:
        return "acceptance_identity_mismatch"
    evidence = card.get("accept_evidence")
    receipt = evidence.get("accepted_outcome_receipt") if isinstance(evidence, dict) else None
    if not isinstance(receipt, dict) or not _identity_text(receipt.get("receipt_id")):
        return "acceptance_receipt_missing"
    return {
        "task_id": candidate["task_id"],
        "request_id": candidate["request_id"],
        "claim_epoch": candidate["claim_epoch"],
        "candidate_sha256": candidate["candidate_sha256"],
        "accepted_outcome_receipt_id": receipt["receipt_id"],
        "repository_revision": _token(receipt.get("repository_revision")),
        "accepted_by": _token(card.get("accepted_by")),
        "accepted_at": _token(card.get("accepted_at")),
    }


def _identity(stage: str, snapshot: TaskSnapshot) -> dict[str, Any] | str:
    """The card-only canonical identity ``stage`` is drawn from, or a refusal code."""

    if snapshot.status in WITHDRAWN_STATUSES:
        return f"task_withdrawn:{snapshot.status}"
    if stage == "plan":
        return _plan_identity(snapshot)
    if stage == "design":
        return _design_identity(snapshot)
    candidate = _candidate_identity(snapshot)
    if stage == "build" or isinstance(candidate, str):
        return candidate
    return _acceptance_identity(snapshot, candidate)


def _pointer_refusal(payload: Mapping[str, Any], identity: Mapping[str, Any]) -> str:
    for key in IDENTITY_POINTERS:
        if key in payload and key in identity and payload[key] != identity[key]:
            return f"identity_swap:{key}"
    return ""


def _predecessor_refusal(
    stage: str, identity: Mapping[str, Any], predecessors: Mapping[str, Mapping[str, Any]]
) -> str:
    for previous, key in PREDECESSOR_BINDINGS.get(stage, ()):
        prior = predecessors.get(previous)
        if prior is not None and prior.get(key) != identity.get(key):
            return f"predecessor_mismatch:{previous}.{key}"
    return ""


# --------------------------------------------------------------------------- #
# verification: the bounded reads a proven Test (and Maintain) also needs
# --------------------------------------------------------------------------- #


def _records(value: Any, *, required: bool) -> list[Any] | None:
    if value is None and not required:
        return []
    if (
        not isinstance(value, list)
        or len(value) > MAX_VALIDATION_RECORDS
        or (required and not value)
        or not all(isinstance(item, dict) for item in value)
    ):
        return None
    return value


def _promoted_refusal(root: Path, paths: Any) -> str:
    """Refuse promoted paths the canonical validator must never be pointed at."""

    code = _paths_refusal(paths)
    if code:
        return code
    try:
        base = root.resolve()
    except OSError:
        return "acceptance_receipt_unverifiable"
    for relative in paths:
        target = root / relative
        try:
            resolved = target.resolve()
            size = target.stat().st_size if target.is_file() else 0
        except (OSError, RuntimeError):
            return "acceptance_receipt_unverifiable"
        if base not in resolved.parents:
            return "evidence_path_traversal"
        if size > MAX_PROMOTED_FILE_BYTES:
            return "evidence_oversized:promoted_file"
    return ""


def _verify_acceptance(
    reader: EvidenceReader, snapshot: TaskSnapshot, identity: Mapping[str, Any]
) -> dict[str, Any] | str:
    """Re-derive the recorded verdict and re-run the canonical acceptance validator."""

    card = snapshot.card
    terminal = card["terminal_review"]
    sealed = terminal["evidence"]
    validations = _records(sealed.get("validation"), required=True)
    outputs = _records(sealed.get("required_outputs"), required=False)
    if validations is None or outputs is None:
        return "validation_evidence_malformed"
    recomputed = task_fsm.deterministic_verification(
        SEALED_SUBSTATUS, validations, outputs, claim_epoch=identity["claim_epoch"]
    )
    recorded = terminal.get("deterministic_verification")
    if (
        not isinstance(recorded, dict)
        or canonical_evidence(recorded)[0] != canonical_evidence(recomputed)[0]
    ):
        return "verification_record_contradicted"
    if recomputed.get("applicable") is not True or recomputed.get("pass") is not True:
        return f"validation_not_passed:{_token(recomputed.get('reason'))}"
    receipt = card["accept_evidence"]["accepted_outcome_receipt"]
    code = _promoted_refusal(reader.root, receipt.get("promoted_paths"))
    if code:
        return code
    # ``_validate_accepted_outcome_receipt`` is the one canonical acceptance
    # authority; accept_review, external_qualification and the trajectory
    # export all bind to it by this name rather than restating its rules.
    # Imported lazily because task_engine pulls in the whole core module.
    from . import task_engine

    try:
        validated, error = task_engine._validate_accepted_outcome_receipt(
            reader.root, card, snapshot.task_id, identity["request_id"], receipt
        )
    except (OSError, TypeError, ValueError):
        return "acceptance_receipt_unverifiable"
    if validated is None:
        return f"acceptance_receipt_invalid:{_token(error)}"
    event_id = reader.accept_event(
        snapshot.task_id, identity["request_id"], identity["accepted_outcome_receipt_id"]
    )
    if isinstance(event_id, str):
        return event_id
    return {
        "verification": {
            "reason": _token(recomputed.get("reason")),
            "validation_count": len(validations),
            "required_output_count": len(outputs),
        },
        "accept_event_id": event_id,
    }


# --------------------------------------------------------------------------- #
# build: the effective route, edit and effort-context receipts of the attempt
# --------------------------------------------------------------------------- #


def _same(left: Any, right: Any) -> bool:
    try:
        return canonical_evidence({"v": left})[0] == canonical_evidence({"v": right})[0]
    except (TypeError, ValueError, RecursionError):
        return False


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _route(
    bundle: Mapping[str, Any], snapshot: TaskSnapshot, request_id: str
) -> dict[str, Any] | str:
    """The route the launcher recorded in the sealed bundle for this very attempt."""

    metadata = bundle["metadata"]
    recorded = metadata.get("request_identity")
    if not isinstance(recorded, dict) or (
        recorded.get("request_id"), recorded.get("task_id"), recorded.get("runner")
    ) != (request_id, snapshot.task_id, snapshot.runner):
        return "attempt_bundle_identity_mismatch"
    adapter_id = metadata.get("adapter_id")
    if not _identity_text(adapter_id):
        return "route_unresolved"
    sealed_adapter = snapshot.card["terminal_review"].get("adapter_id")
    if sealed_adapter and sealed_adapter != adapter_id:
        return "route_mismatch"
    model = metadata.get("model")
    return {
        "runner": snapshot.runner,
        "adapter_id": adapter_id,
        "model": model if _identity_text(model) else "",
        "execution_mode": _token(metadata.get("execution_mode")),
        "sandbox_backend": _token(metadata.get("sandbox_backend")),
        "attempt_manifest_sha256": bundle["manifest_sha256"],
    }


def _coverage_summary(coverage: Mapping[str, Any]) -> dict[str, Any]:
    ratio = coverage.get("coverage_ratio")
    return {
        "measured": coverage.get("measured") is True,
        "unmeasured_reason": _token(coverage.get("unmeasured_reason") or None),
        "coverage_ratio": ratio if isinstance(ratio, (int, float)) and not isinstance(
            ratio, bool
        ) else None,
        "eligible_paths_count": _count(coverage.get("eligible_paths_count")),
        "paths_with_apply": _count(coverage.get("paths_with_apply")),
        "undeclared_raw_only_count": _count(coverage.get("undeclared_raw_only_count")),
    }


def _edit_evidence(
    gate: Mapping[str, Any], event: Mapping[str, Any], changed_paths: list[str]
) -> dict[str, Any] | str:
    """Semantic-edit truth from the authenticated worker ledger the candidate sealed.

    Coverage is the launcher's measurement and is reported, never thresholded;
    what must hold is that the ledger it was measured from is authenticated,
    or that the task was verifiably outside the worker-MCP gate.
    """

    from . import process_launcher

    if gate.get("satisfied") is not True:
        return "worker_mcp_gate_unsatisfied"
    coverage = event.get("semantic_edit_coverage")
    if (
        not isinstance(coverage, dict)
        or coverage.get("schema_id") != process_launcher.SEMANTIC_EDIT_COVERAGE_SCHEMA_ID
    ):
        return "semantic_edit_coverage_missing"
    runtime = event.get("semantic_edit")
    if not isinstance(runtime, dict) or runtime.get("schema_id") != SEMANTIC_EDIT_RUNTIME_SCHEMA_ID:
        return "semantic_edit_evidence_missing"
    summary = {
        "runtime_observed": runtime.get("observed") is True,
        "coverage": _coverage_summary(coverage),
    }
    verification = gate.get("verification")
    if not isinstance(verification, dict) or verification.get("ok") is not True:
        if gate.get("gated") is True:
            return "edit_ledger_unverified"
        return {
            "state": "not_applicable",
            "reason": "worker_mcp_gate_not_gated",
            "task_type": _token(gate.get("task_type") or None),
            **summary,
        }
    receipts = verification.get("semantic_edit_apply_receipts", [])
    if (
        not isinstance(receipts, list)
        or len(receipts) > MAX_EDIT_RECEIPTS
        or not all(isinstance(row, dict) for row in receipts)
    ):
        return "edit_ledger_malformed"
    changed = {process_launcher.semantic_edit_path_identifier(path) for path in changed_paths}
    return {
        "state": "verified",
        "source": "worker_mcp_gate.verification",
        "apply_receipt_count": len(receipts),
        "apply_receipts_joined": sum(1 for row in receipts if row.get("path_sha256") in changed),
        **summary,
    }


def _effort_evidence(
    event: Mapping[str, Any], route: Mapping[str, Any], identity: Mapping[str, Any]
) -> dict[str, Any] | str:
    """The effective effort/context receipt, where the attempt's adapter produces one.

    The launcher attaches ``reasoning_context_attempt`` exactly for its VS Code
    LM in-process adapters, so that set -- not a caller -- decides whether the
    receipt is owed. A present receipt is re-checked by the worker's own
    validator against the launcher-pinned identity; an ``unknown`` send is
    reported as unknown, never as an effort that was applied.
    """

    from . import process_launcher, vscode_lm_worker

    attempt = event.get("reasoning_context_attempt")
    # Private by name, but it is the one set the launcher consults before it
    # emits or withholds this receipt; restating it here would let them drift.
    if route["adapter_id"] not in process_launcher._VSCODE_LM_IN_PROCESS_ADAPTERS:
        if attempt is not None:
            return "effective_effort_context_contradicted"
        return {
            "state": "not_applicable",
            "reason": "adapter_emits_no_effort_context_receipt",
            "adapter_id": route["adapter_id"],
        }
    if (
        not isinstance(attempt, dict)
        or attempt.get("schema_id") != process_launcher.REASONING_CONTEXT_ATTEMPT_EVENT_SCHEMA_ID
        or not isinstance(attempt.get("identity"), dict)
        or not isinstance(attempt.get("receipt"), dict)
    ):
        return "effective_effort_context_missing"
    pinned, receipt = attempt["identity"], attempt["receipt"]
    expected = {
        "task_id": identity["task_id"],
        "request_id": identity["request_id"],
        "adapter_id": route["adapter_id"],
        "claim_epoch": identity["claim_epoch"],
    }
    if any(pinned.get(key) != value for key, value in expected.items()) or (
        route["model"] and pinned.get("model") != route["model"]
    ):
        return "effective_effort_context_identity_mismatch"
    if receipt.get("send_state") == "unknown":
        return f"effective_effort_context_unknown:{_token(receipt.get('unknown_reason'))}"
    spec = {
        "request_id": identity["request_id"],
        "repo_id": pinned.get("repo_id"),
        "model": pinned.get("model"),
    }
    code = vscode_lm_worker._attempt_refusal(receipt, spec)
    if code:
        return f"effective_effort_context_invalid:{_token(code)}"
    return {
        "state": "verified",
        "source": "reasoning_context_attempt",
        "requested_profile": receipt["requested_profile"],
        "option_status": receipt["option_status"],
        "send_turn_count": receipt["send_turn_count"],
        "context_capacity_tokens": receipt["context_capacity_tokens"],
        "context_capacity_source": receipt["context_capacity_source"],
        "host_model_id": _token(receipt["host_model"].get("id")),
    }


def _verify_build(
    reader: EvidenceReader, snapshot: TaskSnapshot, identity: Mapping[str, Any]
) -> dict[str, Any] | str:
    """Join the sealed candidate to the launcher's own receipts for that attempt.

    The card's sealed ``attempt_artifact_manifest`` names a hash-bound bundle;
    the bundle and the request's terminal process event must both agree with
    the card on the manifest, the candidate hashes and the worker-MCP gate, so
    no single store can be rewritten to certify another attempt.
    """

    sealed = snapshot.card["terminal_review"]["evidence"]
    request_id = identity["request_id"]
    bundle = reader.attempt_bundle(request_id, sealed["attempt_artifact_manifest"])
    if isinstance(bundle, str):
        return bundle
    route = _route(bundle, snapshot, request_id)
    if isinstance(route, str):
        return route
    if not _same(bundle["diff"].get("changed_path_hashes"), sealed["changed_path_hashes"]):
        return "attempt_bundle_candidate_mismatch"
    gate = sealed.get("worker_mcp_gate")
    if not isinstance(gate, dict) or not _same(gate, bundle["validation"].get("worker_mcp_gate")):
        return "worker_mcp_gate_contradicted"
    event = reader.terminal_event(snapshot.task_id, request_id)
    if isinstance(event, str):
        return event
    if (event.get("runner"), event.get("adapter_id")) != (snapshot.runner, route["adapter_id"]):
        return "route_mismatch"
    if not _same(event.get("attempt_artifact_manifest"), sealed["attempt_artifact_manifest"]) or (
        not _same(event.get("worker_mcp_gate"), gate)
    ):
        return "build_terminal_event_contradicted"
    edit = _edit_evidence(gate, event, sealed["changed_paths"])
    if isinstance(edit, str):
        return edit
    effort = _effort_evidence(event, route, identity)
    if isinstance(effort, str):
        return effort
    return {"route": route, "semantic_edit": edit, "effective_effort_context": effort}


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #


def decide(
    reader: EvidenceReader,
    *,
    stage: str,
    payload: Mapping[str, Any],
    task_id: str | None,
    predecessors: Mapping[str, Mapping[str, Any]],
) -> StageDecision:
    """Decide whether ``stage`` may be ``ready`` for the case bound to ``task_id``.

    ``predecessors`` maps each earlier stage the caller re-proved in this same
    session to its stored evidence; one naming another task, contract or
    candidate than this stage resolves fails closed. Unsupported stages return
    their missing producers. A refusal never carries a fingerprint.
    """

    refused = payload_refusal(stage, payload)
    if refused is not None:
        return refused
    if stage == "deploy":
        code = (
            "deploy_target_unknown"
            if "target" in payload
            else "missing_producer:release_build_provenance_receipt"
        )
        return _refusal(stage, code, missing_producers=list(MISSING_PRODUCERS["deploy"]))
    if not task_id:
        return _refusal(
            stage, "approval_authority_missing" if stage == "plan" else "case_not_task_bound"
        )
    snapshot = reader.task(task_id)
    if isinstance(snapshot, str):
        return _refusal(stage, snapshot)
    identity = _identity("test" if stage == "maintain" else stage, snapshot)
    if isinstance(identity, str):
        return _refusal(stage, identity)
    code = _pointer_refusal(payload, identity) or _predecessor_refusal(
        stage, identity, predecessors
    )
    if code:
        return _refusal(stage, code)
    proof: dict[str, Any] | str = {}
    if stage == "build":
        proof = _verify_build(reader, snapshot, identity)
    elif stage in ("test", "maintain"):
        proof = _verify_acceptance(reader, snapshot, identity)
    if isinstance(proof, str):
        return _refusal(stage, proof)
    if stage == "maintain":
        return _refusal(
            stage,
            "missing_producer:observed_outcome_metrics",
            accepted_outcome_receipt_id=identity["accepted_outcome_receipt_id"],
            learning=reader.learning(snapshot.task_id, identity["request_id"]),
            missing_producers=list(MISSING_PRODUCERS["maintain"]),
        )
    return StageDecision(
        stage=stage,
        evidence={"schema_id": SCHEMA_ID, "stage": stage, **identity, **proof},
        fingerprint=canonical_evidence(identity)[1],
    )


def identity_unchanged(
    root: Path, repo_id: str, decision: StageDecision, task_id: str | None
) -> bool:
    """True when the canonical identity ``decision`` was drawn from still stands.

    One fresh row read and no file hashing, so the store can call it inside its
    write lease immediately before committing: a candidate, claim, contract or
    acceptance replaced after the decision was drawn makes this False.
    """

    if not decision.ready or not task_id:
        return False
    snapshot = EvidenceReader(root, repo_id).task(task_id)
    if isinstance(snapshot, str):
        return False
    identity = _identity(decision.stage, snapshot)
    return not isinstance(identity, str) and canonical_evidence(identity)[1] == decision.fingerprint
