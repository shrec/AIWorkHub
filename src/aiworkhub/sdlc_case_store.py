from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiworkhub.repository_state import inspect_repository

STAGES = ("plan", "design", "build", "test", "deploy", "maintain")
STATES = frozenset({"ready", "blocked", "unknown", "not_applicable"})
CASES_DB_REL = (".aiworkhub", "sdlc", "cases.sqlite")
SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 16384
MAX_SOURCE_REFS = 32
MAX_REF_CHARS = 256
MAX_LINKS = 16


class SdlcCaseConflict(Exception):
    """Identity or request-id replay conflict."""


class SdlcCaseValidationError(Exception):
    """Invalid stage, state, payload, or missing ready predecessor."""


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
        "created_at TEXT NOT NULL)"
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
        "UNIQUE(case_id, request_id),"
        "FOREIGN KEY(case_id) REFERENCES cases(case_id))"
    )


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


def _unknown_packet(repo_id: str, case_id: str, stage: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "repo_id": repo_id,
        "case_id": case_id,
        "stage": stage,
        "state": "unknown",
        "source_refs": [],
    }


def _packet_from_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        payload = {}
    return {
        "schema_version": SCHEMA_VERSION,
        "repo_id": row["repo_id"],
        "case_id": row["case_id"],
        "stage": row["stage"],
        "state": row["state"],
        "source_refs": _source_refs(payload),
        "receipt_sha256": row["receipt_sha256"],
        "created_at": row["created_at"],
    }


def _missing_ready_predecessor(
    conn: sqlite3.Connection,
    case_id: str,
    stage: str,
    bound_id: int | None = None,
) -> str | None:
    index = STAGES.index(stage)
    for previous in STAGES[:index]:
        row = conn.execute(
            "SELECT id, state FROM stage_receipts "
            "WHERE case_id=? AND stage=? ORDER BY id DESC LIMIT 1",
            (case_id, previous),
        ).fetchone()
        if row is None or row["state"] != "ready":
            return previous
        if bound_id is not None and int(row["id"]) > bound_id:
            return previous
        nested = _missing_ready_predecessor(
            conn, case_id, previous, bound_id=int(row["id"])
        )
        if nested is not None:
            return previous
    return None


def _latest_stage_row(
    conn: sqlite3.Connection, case_id: str, repo_id: str, stage: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM stage_receipts WHERE case_id=? AND repo_id=? AND stage=? "
        "ORDER BY id DESC LIMIT 1",
        (case_id, repo_id, stage),
    ).fetchone()


def _effective_packet(
    conn: sqlite3.Connection,
    repo_id: str,
    case_id: str,
    stage: str,
    row: sqlite3.Row | None,
) -> dict[str, Any]:
    if row is None:
        return _unknown_packet(repo_id, case_id, stage)
    packet = _packet_from_row(row)
    if packet["state"] != "ready":
        return packet
    missing = _missing_ready_predecessor(
        conn, case_id, stage, bound_id=int(row["id"])
    )
    if missing is None:
        return packet
    packet["state"] = "unknown"
    packet["reason"] = "stale_predecessor"
    packet["stale_predecessor"] = missing
    return packet


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
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_schema(conn)
            existing = conn.execute(
                "SELECT request_id, links_json, canonical_sha256 FROM cases WHERE case_id=?",
                (case_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["request_id"] == request_id
                    and existing["canonical_sha256"] == digest
                ):
                    conn.execute("COMMIT")
                    return {
                        "case_id": case_id,
                        "repo_id": repo_id,
                        "request_id": request_id,
                        "receipt_sha256": digest,
                        "idempotent": True,
                        "links": json.loads(existing["links_json"]),
                    }
                raise SdlcCaseConflict("request_id conflict")
            conn.execute(
                "INSERT INTO cases("
                "case_id, repo_id, request_id, links_json, canonical_sha256, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (case_id, repo_id, request_id, _canonical_dumps(links), digest, _now()),
            )
            conn.execute("COMMIT")
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


def append_stage(
    repo_root: Path,
    repo_id: str,
    case_id: str,
    stage: str,
    state: str,
    payload: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
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
    path = _db_path(root)
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
            existing = conn.execute(
                "SELECT receipt_sha256, payload_json, stage, state FROM stage_receipts "
                "WHERE case_id=? AND request_id=?",
                (case_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["receipt_sha256"] == digest:
                    conn.execute("COMMIT")
                    return {
                        "case_id": case_id,
                        "repo_id": repo_id,
                        "stage": stage,
                        "state": state,
                        "request_id": request_id,
                        "receipt_sha256": digest,
                        "idempotent": True,
                    }
                raise SdlcCaseConflict("request_id conflict")
            if state == "ready":
                missing = _missing_ready_predecessor(conn, case_id, stage)
                if missing is not None:
                    raise SdlcCaseValidationError(f"missing ready predecessor: {missing}")
            conn.execute(
                "INSERT INTO stage_receipts("
                "case_id, repo_id, request_id, stage, state, payload_json, "
                "receipt_sha256, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    repo_id,
                    request_id,
                    stage,
                    state,
                    _canonical_dumps(payload),
                    digest,
                    _now(),
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            replay = conn.execute(
                "SELECT receipt_sha256 FROM stage_receipts "
                "WHERE case_id=? AND request_id=?",
                (case_id, request_id),
            ).fetchone()
            if replay is not None and replay["receipt_sha256"] == digest:
                return {
                    "case_id": case_id,
                    "repo_id": repo_id,
                    "stage": stage,
                    "state": state,
                    "request_id": request_id,
                    "receipt_sha256": digest,
                    "idempotent": True,
                }
            raise SdlcCaseConflict("request_id conflict") from None
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {
        "case_id": case_id,
        "repo_id": repo_id,
        "stage": stage,
        "state": state,
        "request_id": request_id,
        "receipt_sha256": digest,
        "idempotent": False,
    }


def read_case(repo_root: Path, repo_id: str, case_id: str) -> dict[str, Any]:
    case_id = _require_text("case_id", case_id)
    root = _require_repo_id(Path(repo_root), repo_id)
    path = _db_path(root)
    if not path.is_file():
        raise SdlcCaseValidationError("case not found")
    conn = _connect(path)
    try:
        case_row = conn.execute(
            "SELECT case_id, repo_id, links_json FROM cases WHERE case_id=? AND repo_id=?",
            (case_id, repo_id),
        ).fetchone()
        if case_row is None:
            raise SdlcCaseValidationError("case not found")
        stages = {
            stage: _effective_packet(
                conn, repo_id, case_id, stage, _latest_stage_row(conn, case_id, repo_id, stage)
            )
            for stage in STAGES
        }
        links = _bounded_links(case_row["links_json"])
    finally:
        conn.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "repo_id": repo_id,
        "links": links,
        "stages": stages,
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
        return _unknown_packet(repo_id, case_id, stage)
    conn = _connect(path)
    try:
        case_row = conn.execute(
            "SELECT case_id FROM cases WHERE case_id=? AND repo_id=?",
            (case_id, repo_id),
        ).fetchone()
        if case_row is None:
            return _unknown_packet(repo_id, case_id, stage)
        row = _latest_stage_row(conn, case_id, repo_id, stage)
        return _effective_packet(conn, repo_id, case_id, stage, row)
    finally:
        conn.close()
