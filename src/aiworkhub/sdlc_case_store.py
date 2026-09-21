from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiworkhub.repository_state import inspect_repository
from aiworkhub import sdlc_stage_evidence, task_store

STAGES = ("plan", "design", "build", "test", "deploy", "maintain")
STATES = frozenset({"ready", "blocked", "unknown", "not_applicable"})
# States that claim progress, so only server-resolved proof may record them.
PROVEN_STATES = frozenset({"ready", "not_applicable"})
CASES_DB_REL = (".aiworkhub", "sdlc", "cases.sqlite")
SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 16384
MAX_SOURCE_REFS = 32
MAX_REF_CHARS = 256
MAX_LINKS = 16
TASK_LINK_KEY = "task_id"
EVIDENCE_COLUMNS = ("evidence_json", "evidence_sha256")


class SdlcCaseConflict(Exception):
    """Identity or request-id replay conflict."""


class SdlcCaseValidationError(Exception):
    """Invalid stage, state, payload, or missing ready predecessor."""


class SdlcStageEvidenceRefusal(SdlcCaseValidationError):
    """A transition the server could not prove from canonical receipts.

    ``str()`` is the typed reason and next action an MCP caller receives; the
    full decision, with any partial evidence resolved, stays on ``decision``.
    """

    def __init__(self, decision: sdlc_stage_evidence.StageDecision) -> None:
        super().__init__(decision.message())
        self.decision = decision


def _canonical_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(payload: dict[str, Any]) -> str:
    raw = _canonical_dumps(payload).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_repo_id(repo_root: Path, repo_id: str) -> Path:
    state = inspect_repository(repo_root)
    if state.manifest.repo_id != repo_id:
        raise SdlcCaseConflict("cross_repository")
    return Path(state.root)


def _db_path(repo_root: Path) -> Path:
    return Path(repo_root).joinpath(*CASES_DB_REL)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cases ("
        "case_id TEXT PRIMARY KEY,"
        "repo_id TEXT NOT NULL,"
        "request_id TEXT NOT NULL,"
        "links_json TEXT NOT NULL,"
        "canonical_sha256 TEXT NOT NULL,"
        "created_at TEXT NOT NULL,"
        "task_id TEXT)"
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(cases)")}
    if "task_id" not in columns:
        conn.execute("ALTER TABLE cases ADD COLUMN task_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cases_repo_task "
        "ON cases(repo_id, task_id) WHERE task_id IS NOT NULL"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stage_receipts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "case_id TEXT NOT NULL,"
        "repo_id TEXT NOT NULL,"
        "request_id TEXT NOT NULL,"
        "stage TEXT NOT NULL,"
        "state TEXT NOT NULL,"
        "payload_json TEXT NOT NULL,"
        "receipt_sha256 TEXT NOT NULL,"
        "created_at TEXT NOT NULL,"
        "evidence_json TEXT,"
        "evidence_sha256 TEXT,"
        "UNIQUE(case_id, request_id),"
        "FOREIGN KEY(case_id) REFERENCES cases(case_id))"
    )
    # Receipts written before the evidence gate keep NULL evidence: preserved
    # for audit, never counted as proof.
    receipt_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(stage_receipts)")
    }
    for column in EVIDENCE_COLUMNS:
        if column not in receipt_columns:
            conn.execute(f"ALTER TABLE stage_receipts ADD COLUMN {column} TEXT")


def _require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SdlcCaseValidationError(f"{name} required")
    return value


def _require_links(links: dict[str, str]) -> dict[str, str]:
    if not isinstance(links, dict):
        raise SdlcCaseValidationError("links must be an object")
    if len(links) > MAX_LINKS:
        raise SdlcCaseValidationError("links exceed bound")
    for key, value in links.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise SdlcCaseValidationError("links values must be strings")
        if len(key) > MAX_REF_CHARS or len(value) > MAX_REF_CHARS:
            raise SdlcCaseValidationError("links exceed bound")
    return links


def _bounded_links(raw: str) -> dict[str, str]:
    links = json.loads(raw)
    if not isinstance(links, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in links.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        out[key[:MAX_REF_CHARS]] = value[:MAX_REF_CHARS]
        if len(out) >= MAX_LINKS:
            break
    return out


def _require_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SdlcCaseValidationError("payload must be an object")
    raw = _canonical_dumps(payload)
    if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise SdlcCaseValidationError("payload exceeds bound")
    return payload


def _source_refs(payload: dict[str, Any]) -> list[str]:
    refs = payload.get("evidence_refs")
    if not isinstance(refs, list):
        return []
    out: list[str] = []
    for item in refs:
        if isinstance(item, str) and item:
            out.append(item[:MAX_REF_CHARS])
            if len(out) >= MAX_SOURCE_REFS:
                break
    return out


def _unknown_packet(
    repo_id: str, case_id: str, stage: str, reason: str = "no_stage_receipt"
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "repo_id": repo_id,
        "case_id": case_id,
        "stage": stage,
        "state": "unknown",
        "source_refs": [],
        "reason": reason,
    }


def _stored_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _packet_from_row(row: sqlite3.Row) -> dict[str, Any]:
    packet = {
        "schema_version": SCHEMA_VERSION,
        "repo_id": row["repo_id"],
        "case_id": row["case_id"],
        "stage": row["stage"],
        "state": row["state"],
        "source_refs": _source_refs(_stored_payload(row)),
        "receipt_sha256": row["receipt_sha256"],
        "created_at": row["created_at"],
    }
    if "evidence_sha256" in row.keys() and row["evidence_sha256"]:
        packet["evidence_sha256"] = row["evidence_sha256"]
    return packet


def _latest_stage_row(
    conn: sqlite3.Connection, case_id: str, repo_id: str, stage: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM stage_receipts WHERE case_id=? AND repo_id=? AND stage=? "
        "ORDER BY id DESC LIMIT 1",
        (case_id, repo_id, stage),
    ).fetchone()


def _case_row(
    conn: sqlite3.Connection, case_id: str, repo_id: str
) -> sqlite3.Row | None:
    try:
        return conn.execute(
            "SELECT * FROM cases WHERE case_id=? AND repo_id=?", (case_id, repo_id)
        ).fetchone()
    except sqlite3.OperationalError:
        # A schema-less file holds no case; callers treat that as absent.
        return None


def _case_task_id(case_row: sqlite3.Row) -> str | None:
    """The case's verified task binding, never the unverified links text."""
    task_id = case_row[TASK_LINK_KEY] if TASK_LINK_KEY in case_row.keys() else None
    return task_id if isinstance(task_id, str) and task_id else None


def _stored_evidence(row: sqlite3.Row) -> dict[str, Any] | str | None:
    """A receipt's server-resolved evidence.

    ``None`` marks a legacy row recorded before this gate existed; a string is
    the typed reason its stored bytes can no longer be trusted.
    """
    raw = row["evidence_json"] if "evidence_json" in row.keys() else None
    if raw is None:
        return None
    try:
        evidence = json.loads(raw)
    except (TypeError, ValueError):
        return "evidence_integrity_mismatch"
    if not isinstance(evidence, dict):
        return "evidence_integrity_mismatch"
    text, digest = sdlc_stage_evidence.canonical_evidence(evidence)
    if text != raw or digest != row["evidence_sha256"]:
        return "evidence_integrity_mismatch"
    if evidence.get("schema_id") != sdlc_stage_evidence.SCHEMA_ID:
        return "evidence_schema_unsupported"
    return evidence


def _demote(packet: dict[str, Any], reason: str) -> dict[str, Any]:
    """Keep a recorded receipt visible for audit while refusing it as proof."""
    packet["recorded_state"] = packet["state"]
    packet["state"] = "unknown"
    packet["reason"] = reason
    return packet


_Proven = dict[str, tuple[int, dict[str, Any]]]


def _project_row(
    reader: sdlc_stage_evidence.EvidenceReader,
    repo_id: str,
    case_id: str,
    stage: str,
    row: sqlite3.Row | None,
    task_id: str | None,
    proven: _Proven,
) -> dict[str, Any]:
    """One stage's effective packet; a ready row counts only once re-proven now."""
    if row is None:
        return _unknown_packet(repo_id, case_id, stage)
    packet = _packet_from_row(row)
    if packet["state"] == "not_applicable":
        return _demote(packet, "not_applicable_unverified")
    if packet["state"] != "ready":
        return packet
    evidence = _stored_evidence(row)
    if evidence is None:
        return _demote(packet, "legacy_unverified")
    if isinstance(evidence, str):
        return _demote(packet, evidence)
    row_id = int(row["id"])
    for previous in STAGES[: STAGES.index(stage)]:
        prior = proven.get(previous)
        if prior is None or prior[0] > row_id:
            packet = _demote(packet, "stale_predecessor")
            packet["stale_predecessor"] = previous
            return packet
    decision = sdlc_stage_evidence.decide(
        reader,
        stage=stage,
        payload=_stored_payload(row),
        task_id=task_id,
        predecessors={name: prior[1] for name, prior in proven.items()},
    )
    if not decision.ready:
        packet = _demote(packet, "evidence_stale")
        packet["evidence_code"] = decision.code
        packet["next_action"] = decision.next_action
        return packet
    if sdlc_stage_evidence.canonical_evidence(decision.evidence)[1] != row["evidence_sha256"]:
        return _demote(packet, "evidence_changed")
    proven[stage] = (row_id, evidence)
    packet["evidence"] = evidence
    return packet


def _project_stages(
    conn: sqlite3.Connection,
    reader: sdlc_stage_evidence.EvidenceReader,
    repo_id: str,
    case_id: str,
    task_id: str | None,
    *,
    through: str = STAGES[-1],
) -> tuple[dict[str, dict[str, Any]], _Proven]:
    """Effective packets in stage order, and the stages re-proven from receipts.

    One reader session judges every row: a ready row's stored evidence must
    still hash, every earlier stage must be proven by an older row, and
    resolving its payload now must reproduce that evidence byte for byte. A
    legacy ready row keeps its receipt for audit but proves nothing.
    """
    packets: dict[str, dict[str, Any]] = {}
    proven: _Proven = {}
    for stage in STAGES[: STAGES.index(through) + 1]:
        row = _latest_stage_row(conn, case_id, repo_id, stage)
        packets[stage] = _project_row(reader, repo_id, case_id, stage, row, task_id, proven)
    return packets, proven


def _cycle(stages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The whole loop is complete only when all six stages are proven now."""
    for stage in STAGES:
        packet = stages[stage]
        if packet["state"] != "ready":
            return {
                "state": "incomplete",
                "blocking_stage": stage,
                "reason": packet.get("reason") or packet["state"],
            }
    return {"state": "complete", "blocking_stage": None, "reason": None}


def create_case(
    repo_root: Path,
    repo_id: str,
    case_id: str,
    request_id: str,
    links: dict[str, str],
) -> dict[str, Any]:
    case_id = _require_text("case_id", case_id)
    request_id = _require_text("request_id", request_id)
    links = _require_links(links)
    root = _require_repo_id(Path(repo_root), repo_id)
    canonical = {
        "case_id": case_id,
        "links": links,
        "repo_id": repo_id,
        "request_id": request_id,
    }
    digest = _digest(canonical)
    path = _db_path(root)

    # Resolve an existing case_id before any task verification so an exact
    # replay stays idempotent even if the linked task was later retired, and a
    # conflicting replay fails before touching the task store.
    if path.is_file():
        probe = _connect(path)
        try:
            try:
                row = probe.execute(
                    "SELECT request_id, links_json, canonical_sha256 FROM cases WHERE case_id=?",
                    (case_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                # A schema-less file (empty or interrupted creation) has no
                # cases table to replay; the write path initializes it below.
                row = None
        finally:
            probe.close()
        if row is not None:
            if row["request_id"] == request_id and row["canonical_sha256"] == digest:
                return {
                    "case_id": case_id,
                    "repo_id": repo_id,
                    "request_id": request_id,
                    "receipt_sha256": digest,
                    "idempotent": True,
                    "links": json.loads(row["links_json"]),
                }
            raise SdlcCaseConflict("request_id conflict")

    task_id = _verify_task_link(root, links)

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT request_id, links_json, canonical_sha256 FROM cases WHERE case_id=?",
                (case_id,),
            ).fetchone()
            if row is not None:
                if row["request_id"] == request_id and row["canonical_sha256"] == digest:
                    conn.execute("COMMIT")
                    return {
                        "case_id": case_id,
                        "repo_id": repo_id,
                        "request_id": request_id,
                        "receipt_sha256": digest,
                        "idempotent": True,
                        "links": json.loads(row["links_json"]),
                    }
                raise SdlcCaseConflict("request_id conflict")
            conn.execute(
                "INSERT INTO cases("
                "case_id, repo_id, request_id, links_json, canonical_sha256, created_at, task_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    repo_id,
                    request_id,
                    _canonical_dumps(links),
                    digest,
                    _now(),
                    task_id,
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise SdlcCaseConflict("task already bound") from None
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {
        "case_id": case_id,
        "repo_id": repo_id,
        "request_id": request_id,
        "receipt_sha256": digest,
        "idempotent": False,
        "links": links,
    }


def _receipt(
    case_id: str, repo_id: str, stage: str, state: str, request_id: str, digest: str
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "repo_id": repo_id,
        "stage": stage,
        "state": state,
        "request_id": request_id,
        "receipt_sha256": digest,
    }


def _replay_row(
    conn: sqlite3.Connection, case_id: str, request_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM stage_receipts WHERE case_id=? AND request_id=?",
        (case_id, request_id),
    ).fetchone()


def _replay_fields(row: sqlite3.Row) -> dict[str, Any]:
    evidence_sha256 = row["evidence_sha256"] if "evidence_sha256" in row.keys() else None
    return {"idempotent": True, "evidence_sha256": evidence_sha256}


def _replayed(
    path: Path, case_id: str, request_id: str, digest: str
) -> dict[str, Any] | None:
    """An exact replay, answered before any evidence is resolved or lease taken."""
    if not path.is_file():
        return None
    conn = _connect(path)
    try:
        try:
            row = _replay_row(conn, case_id, request_id)
        except sqlite3.OperationalError:
            # No receipts table yet, so nothing to replay; the write path creates it.
            return None
    finally:
        conn.close()
    if row is None:
        return None
    if row["receipt_sha256"] != digest:
        raise SdlcCaseConflict("request_id conflict")
    return _replay_fields(row)


@dataclass(frozen=True)
class _Proof:
    """A transition proven outside the write lease, and exactly what it rested on."""

    decision: sdlc_stage_evidence.StageDecision
    task_id: str | None
    predecessor_rows: dict[str, int]
    evidence_json: str
    evidence_sha256: str


def _prove(
    path: Path,
    root: Path,
    repo_id: str,
    case_id: str,
    stage: str,
    state: str,
    payload: dict[str, Any],
) -> _Proof:
    """Resolve a progress-claiming request against canonical receipts, or refuse it.

    Runs before the write lease: the payload shape first, so a verdict-shaped
    key is refused as such at every stage; then every earlier stage, re-proven
    in one reader session; then this stage's own evidence.
    """
    if state == "not_applicable":
        raise SdlcStageEvidenceRefusal(
            sdlc_stage_evidence.refuse_not_applicable(stage, payload)
        )
    refused = sdlc_stage_evidence.payload_refusal(stage, payload)
    if refused is not None:
        raise SdlcStageEvidenceRefusal(refused)
    if not path.is_file():
        raise SdlcCaseValidationError("case not found")
    reader = sdlc_stage_evidence.EvidenceReader(root, repo_id)
    earlier = STAGES[: STAGES.index(stage)]
    conn = _connect(path)
    try:
        case_row = _case_row(conn, case_id, repo_id)
        if case_row is None:
            raise SdlcCaseValidationError("case not found")
        task_id = _case_task_id(case_row)
        packets, proven = (
            _project_stages(conn, reader, repo_id, case_id, task_id, through=earlier[-1])
            if earlier
            else ({}, {})
        )
    finally:
        conn.close()
    for previous in earlier:
        if previous not in proven:
            packet = packets[previous]
            raise SdlcCaseValidationError(
                f"missing ready predecessor: {previous} "
                f"({packet.get('reason') or packet['state']})"
            )
    decision = sdlc_stage_evidence.decide(
        reader,
        stage=stage,
        payload=payload,
        task_id=task_id,
        predecessors={name: prior[1] for name, prior in proven.items()},
    )
    if not decision.ready:
        raise SdlcStageEvidenceRefusal(decision)
    evidence_json, evidence_sha256 = sdlc_stage_evidence.canonical_evidence(
        decision.evidence
    )
    return _Proof(
        decision=decision,
        task_id=task_id,
        predecessor_rows={name: prior[0] for name, prior in proven.items()},
        evidence_json=evidence_json,
        evidence_sha256=evidence_sha256,
    )


def _recheck(
    conn: sqlite3.Connection, root: Path, repo_id: str, case_id: str, proof: _Proof
) -> None:
    """Inside the write lease: refuse when anything the proof rested on has moved."""
    for stage, row_id in proof.predecessor_rows.items():
        latest = _latest_stage_row(conn, case_id, repo_id, stage)
        if latest is None or int(latest["id"]) != row_id:
            raise SdlcCaseConflict("stage_evidence_changed_during_commit")
    if not sdlc_stage_evidence.identity_unchanged(
        root, repo_id, proof.decision, proof.task_id
    ):
        raise SdlcCaseConflict("stage_evidence_changed_during_commit")


def append_stage(
    repo_root: Path,
    repo_id: str,
    case_id: str,
    stage: str,
    state: str,
    payload: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """Append one immutable stage receipt; ``ready`` only when the server proves it.

    ``ready`` is resolved from canonical receipts before the write lease and
    only its identity is rechecked inside it; ``not_applicable`` is refused
    until a canonical policy can vouch for it; ``blocked``/``unknown`` record
    as stated. An exact replay returns the stored receipt without resolving
    again, and changed bytes under one request id conflict.
    """
    case_id = _require_text("case_id", case_id)
    request_id = _require_text("request_id", request_id)
    if stage not in STAGES:
        raise SdlcCaseValidationError("invalid stage")
    if state not in STATES:
        raise SdlcCaseValidationError("invalid state")
    payload = _require_payload(payload)
    if state == "not_applicable":
        reason = payload.get("reason")
        policy_ref = payload.get("policy_ref")
        if not isinstance(reason, str) or not reason.strip():
            raise SdlcCaseValidationError("reason required for not_applicable")
        if not isinstance(policy_ref, str) or not policy_ref.strip():
            raise SdlcCaseValidationError("policy_ref required for not_applicable")
    root = _require_repo_id(Path(repo_root), repo_id)
    canonical = {
        "case_id": case_id,
        "payload": payload,
        "repo_id": repo_id,
        "request_id": request_id,
        "stage": stage,
        "state": state,
    }
    digest = _digest(canonical)
    receipt = _receipt(case_id, repo_id, stage, state, request_id, digest)
    path = _db_path(root)
    replayed = _replayed(path, case_id, request_id, digest)
    if replayed is not None:
        return {**receipt, **replayed}
    proof = (
        _prove(path, root, repo_id, case_id, stage, state, payload)
        if state in PROVEN_STATES
        else None
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_schema(conn)
            case_row = conn.execute(
                "SELECT case_id FROM cases WHERE case_id=? AND repo_id=?",
                (case_id, repo_id),
            ).fetchone()
            if case_row is None:
                raise SdlcCaseValidationError("case not found")
            existing = _replay_row(conn, case_id, request_id)
            if existing is not None:
                if existing["receipt_sha256"] == digest:
                    conn.execute("COMMIT")
                    return {**receipt, **_replay_fields(existing)}
                raise SdlcCaseConflict("request_id conflict")
            if proof is not None:
                _recheck(conn, root, repo_id, case_id, proof)
            conn.execute(
                "INSERT INTO stage_receipts("
                "case_id, repo_id, request_id, stage, state, payload_json, "
                "receipt_sha256, created_at, evidence_json, evidence_sha256"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    repo_id,
                    request_id,
                    stage,
                    state,
                    _canonical_dumps(payload),
                    digest,
                    _now(),
                    proof.evidence_json if proof else None,
                    proof.evidence_sha256 if proof else None,
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            replay = _replay_row(conn, case_id, request_id)
            if replay is not None and replay["receipt_sha256"] == digest:
                return {**receipt, **_replay_fields(replay)}
            raise SdlcCaseConflict("request_id conflict") from None
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {
        **receipt,
        "idempotent": False,
        "evidence_sha256": proof.evidence_sha256 if proof else None,
    }


def read_case(repo_root: Path, repo_id: str, case_id: str) -> dict[str, Any]:
    case_id = _require_text("case_id", case_id)
    root = _require_repo_id(Path(repo_root), repo_id)
    path = _db_path(root)
    if not path.is_file():
        raise SdlcCaseValidationError("case not found")
    conn = _connect(path)
    try:
        case_row = _case_row(conn, case_id, repo_id)
        if case_row is None:
            raise SdlcCaseValidationError("case not found")
        reader = sdlc_stage_evidence.EvidenceReader(root, repo_id)
        stages, _proven = _project_stages(
            conn, reader, repo_id, case_id, _case_task_id(case_row)
        )
        links = _bounded_links(case_row["links_json"])
    finally:
        conn.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "repo_id": repo_id,
        "links": links,
        "stages": stages,
        "cycle": _cycle(stages),
    }


def stage_packet(
    repo_root: Path, repo_id: str, case_id: str, stage: str
) -> dict[str, Any]:
    case_id = _require_text("case_id", case_id)
    if stage not in STAGES:
        raise SdlcCaseValidationError("invalid stage")
    root = _require_repo_id(Path(repo_root), repo_id)
    path = _db_path(root)
    if not path.is_file():
        return _unknown_packet(repo_id, case_id, stage, "case_not_found")
    conn = _connect(path)
    try:
        case_row = _case_row(conn, case_id, repo_id)
        if case_row is None:
            return _unknown_packet(repo_id, case_id, stage, "case_not_found")
        reader = sdlc_stage_evidence.EvidenceReader(root, repo_id)
        packets, _proven = _project_stages(
            conn, reader, repo_id, case_id, _case_task_id(case_row), through=stage
        )
        return packets[stage]
    finally:
        conn.close()


def _verify_task_link(root: Path, links: dict[str, str]) -> str | None:
    task_id = links.get(TASK_LINK_KEY)
    if task_id is None:
        return None
    task_id = _require_text(TASK_LINK_KEY, task_id)
    try:
        found = task_store.get_task(root, task_id)
    except task_store.StorageNotReadyError as exc:
        raise SdlcCaseValidationError(f"task store not ready: {exc}") from exc
    if found is None:
        raise SdlcCaseValidationError("task not found")
    return task_id


def _unknown_task_case(repo_id: str, task_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "repo_id": repo_id,
        "task_id": task_id,
        "case_id": None,
        "state": "unknown",
        "links": {},
        "stages": {},
    }


def case_for_task(repo_root: Path, repo_id: str, task_id: str) -> dict[str, Any]:
    task_id = _require_text(TASK_LINK_KEY, task_id)
    root = _require_repo_id(Path(repo_root), repo_id)
    path = _db_path(root)
    if not path.is_file():
        return _unknown_task_case(repo_id, task_id)
    conn = _connect(path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(cases)")}
        if TASK_LINK_KEY not in columns:
            return _unknown_task_case(repo_id, task_id)
        rows = conn.execute(
            "SELECT case_id FROM cases WHERE repo_id=? AND task_id=?",
            (repo_id, task_id),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return _unknown_task_case(repo_id, task_id)
    if len(rows) > 1:
        raise SdlcCaseConflict("ambiguous task link")
    packet = read_case(repo_root, repo_id, rows[0]["case_id"])
    packet[TASK_LINK_KEY] = task_id
    packet["state"] = "bound"
    return packet
