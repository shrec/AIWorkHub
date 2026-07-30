"""Authenticated worker proposals for manager-arbitrated context writes.

Workers never open the canonical Session, AI Memory, or KB databases.  They
append a bounded HMAC-authenticated proposal to their existing request-local
MCP ledger.  A verified manager may later inspect and explicitly accept or
reject that proposal; accepted writes are applied through ``context_writes``
and therefore retain its idempotency, provenance, audit, and soft-delete
guarantees.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import errno
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from . import context_writes, storage_registry
from .platform_io import chmod_fd


INTENT_SCHEMA_ID = "aiworkhub.context_write_intent.v1"
DECISION_SCHEMA_ID = "aiworkhub.context_write_intent_decision.v1"
Component = Literal["session", "memory", "kb"]
Decision = Literal["accepted", "rejected"]

MAX_INTENT_BYTES = 40 * 1024
MAX_REASON_BYTES = 2048
MAX_INTENTS_PER_REQUEST = 64
_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,191}$")

_ACTIONS: dict[str, frozenset[str]] = {
    "session": frozenset({"start", "event", "checkpoint", "state", "handoff", "close"}),
    "memory": frozenset({"remember", "update", "supersede", "archive"}),
    "kb": frozenset({"upsert", "ingest", "supersede", "archive"}),
}


class ContextWriteIntentError(RuntimeError):
    """Fail-closed intent validation or disposition error."""


def _bounded_text(value: Any, *, field: str, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ContextWriteIntentError(f"invalid_{field}")
    result = value.strip()
    if (required and not result) or "\x00" in result or len(result.encode("utf-8")) > limit:
        raise ContextWriteIntentError(f"invalid_{field}")
    return result


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signature(entry: dict[str, Any], key: bytes) -> str:
    return hmac.new(key, _canonical_json(entry), hashlib.sha256).hexdigest()


def _append_line_0600(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        try:
            chmod_fd(fd, 0o600)
        except OSError as exc:
            mode = os.fstat(fd).st_mode & 0o777
            if exc.errno not in {errno.EPERM, errno.EACCES} or mode != 0o600:
                raise
        os.write(fd, json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n")
    finally:
        os.close(fd)


def append_intent(
    *, ledger_path: Path, key_path: Path, task_id: str, runner: str, topic: str,
    request_id: str, authority_repo: Path, component: Component, action: str,
    payload: dict[str, Any], idempotency_key: str, provenance: str,
) -> dict[str, Any]:
    """Append one request-bound proposal without opening a canonical DB."""

    component = _bounded_text(component, field="component", limit=16)  # type: ignore[assignment]
    if component not in _ACTIONS:
        raise ContextWriteIntentError("invalid_component")
    action = _bounded_text(action, field="action", limit=32)
    if action not in _ACTIONS[component]:
        raise ContextWriteIntentError("invalid_action")
    if not isinstance(payload, dict):
        raise ContextWriteIntentError("invalid_payload")
    provenance = _bounded_text(
        provenance, field="provenance", limit=context_writes.MAX_PROVENANCE_BYTES,
    )
    idempotency_key = _bounded_text(idempotency_key, field="idempotency_key", limit=192)
    if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise ContextWriteIntentError("invalid_idempotency_key")
    binding = {
        "task_id": _bounded_text(task_id, field="task_id", limit=256),
        "runner": _bounded_text(runner, field="runner", limit=256),
        "topic": _bounded_text(topic, field="topic", limit=128),
        "request_id": _bounded_text(request_id, field="request_id", limit=128),
        "authority_repo": str(authority_repo.resolve()),
    }
    body = {
        "schema_id": INTENT_SCHEMA_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **binding,
        "component": component,
        "action": action,
        "payload": payload,
        "idempotency_key": idempotency_key,
        "provenance": provenance,
    }
    if len(_canonical_json(body)) > MAX_INTENT_BYTES:
        raise ContextWriteIntentError("intent_too_large")
    stable = {key: value for key, value in body.items() if key != "timestamp"}
    intent_id = hashlib.sha256(_canonical_json(stable)).hexdigest()
    entry = {**body, "intent_id": intent_id}
    if ledger_path.is_symlink() or key_path.is_symlink():
        raise ContextWriteIntentError("intent_runtime_symlink_forbidden")
    try:
        key = key_path.read_bytes()
    except OSError as exc:
        raise ContextWriteIntentError("intent_key_unreadable") from exc
    if len(key) < 32:
        raise ContextWriteIntentError("intent_key_invalid")
    try:
        existing = read_verified_intents(
            ledger_path=ledger_path,
            key_path=key_path,
            task_id=binding["task_id"],
            runner=binding["runner"],
            topic=binding["topic"],
            request_id=binding["request_id"],
            authority_repo=authority_repo,
        )
    except ContextWriteIntentError:
        existing = []
    if any(str(row.get("intent_id")) == intent_id for row in existing):
        return {
            "ok": True,
            "idempotent": True,
            "intent_id": intent_id,
            "component": component,
            "action": action,
            "status": "pending_manager_review",
        }
    signed = {**entry, "hmac_sha256": _signature(entry, key)}
    _append_line_0600(ledger_path, signed)
    return {
        "ok": True,
        "idempotent": False,
        "intent_id": intent_id,
        "component": component,
        "action": action,
        "status": "pending_manager_review",
    }


def read_verified_intents(
    *, ledger_path: Path, key_path: Path, task_id: str, runner: str,
    topic: str, request_id: str, authority_repo: Path,
) -> list[dict[str, Any]]:
    """Return unique, valid proposals for one exact immutable request binding."""

    try:
        key = key_path.read_bytes()
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ContextWriteIntentError("intent_ledger_unreadable") from exc
    expected_repo = str(authority_repo.resolve())
    unique: dict[str, dict[str, Any]] = {}
    for raw in lines:
        try:
            signed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(signed, dict) or signed.get("schema_id") != INTENT_SCHEMA_ID:
            continue
        digest = signed.get("hmac_sha256")
        entry = {k: v for k, v in signed.items() if k != "hmac_sha256"}
        if not isinstance(digest, str) or not hmac.compare_digest(_signature(entry, key), digest):
            continue
        if (
            str(entry.get("task_id")) != task_id
            or str(entry.get("runner")) != runner
            or str(entry.get("topic")) != topic
            or str(entry.get("request_id")) != request_id
            or str(entry.get("authority_repo")) != expected_repo
        ):
            continue
        intent_id = str(entry.get("intent_id") or "")
        stable = {k: v for k, v in entry.items() if k not in {"timestamp", "intent_id"}}
        if not _ID_RE.fullmatch(intent_id) or hashlib.sha256(_canonical_json(stable)).hexdigest() != intent_id:
            continue
        unique.setdefault(intent_id, entry)
    return list(unique.values())[-MAX_INTENTS_PER_REQUEST:]


def _decision_connection(repo: Path, *, writable: bool) -> sqlite3.Connection:
    registry = storage_registry.load_storage_registry(repo)
    path = storage_registry.resolve_database_path(registry, "task_queue")
    if writable:
        con = sqlite3.connect(str(path), timeout=5)
    else:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        con.execute("PRAGMA query_only=ON")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    if writable:
        con.execute(
            "CREATE TABLE IF NOT EXISTS context_write_intent_decisions("
            "intent_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,task_id TEXT NOT NULL,"
            "component TEXT NOT NULL,action TEXT NOT NULL,decision TEXT NOT NULL,"
            "reason TEXT NOT NULL,manager_provider TEXT NOT NULL,manager_session_id TEXT NOT NULL,"
            "result_json TEXT NOT NULL,decided_at TEXT NOT NULL)"
        )
    return con


def decisions(repo: Path, *, request_id: str) -> dict[str, dict[str, Any]]:
    con = _decision_connection(repo, writable=False)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_write_intent_decisions'"
        ).fetchone()
        if exists is None:
            return {}
        rows = con.execute(
            "SELECT * FROM context_write_intent_decisions WHERE request_id=? ORDER BY decided_at,intent_id",
            (request_id,),
        ).fetchall()
        return {str(row["intent_id"]): dict(row) for row in rows}
    finally:
        con.close()


def record_decision(
    repo: Path, *, intent: dict[str, Any], decision: Decision, reason: str,
    manager_provider: str, manager_session_id: str, result: dict[str, Any],
) -> dict[str, Any]:
    if decision not in {"accepted", "rejected"}:
        raise ContextWriteIntentError("invalid_decision")
    reason = _bounded_text(reason, field="reason", limit=MAX_REASON_BYTES)
    intent_id = str(intent.get("intent_id") or "")
    if not _ID_RE.fullmatch(intent_id):
        raise ContextWriteIntentError("invalid_intent_id")
    con = _decision_connection(repo, writable=True)
    try:
        con.execute("BEGIN IMMEDIATE")
        prior = con.execute(
            "SELECT * FROM context_write_intent_decisions WHERE intent_id=?", (intent_id,),
        ).fetchone()
        if prior is not None:
            con.rollback()
            row = dict(prior)
            if row["decision"] != decision:
                raise ContextWriteIntentError("intent_already_disposed")
            return {"ok": True, "idempotent": True, **row}
        now = datetime.now(timezone.utc).isoformat()
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
        con.execute(
            "INSERT INTO context_write_intent_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                intent_id, str(intent["request_id"]), str(intent["task_id"]),
                str(intent["component"]), str(intent["action"]), decision, reason,
                manager_provider, manager_session_id, result_json, now,
            ),
        )
        con.commit()
        return {
            "ok": True, "idempotent": False, "intent_id": intent_id,
            "decision": decision, "reason": reason, "decided_at": now,
            "result": result,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def apply_accepted_intent(
    repo: Path, *, intent: dict[str, Any], manager_provider: str,
    manager_session_id: str,
) -> dict[str, Any]:
    """Apply an already-verified proposal via the canonical mutation layer."""

    actor = {
        "role": "manager",
        "actor_id": manager_session_id,
        "task_id": str(intent["task_id"]),
        "provider": manager_provider,
        "session_id": manager_session_id,
    }
    payload = intent.get("payload")
    if not isinstance(payload, dict):
        raise ContextWriteIntentError("invalid_payload")
    common = {
        "actor": actor,
        "action": str(intent["action"]),
        "idempotency_key": str(intent["idempotency_key"]),
        "provenance": str(intent["provenance"]),
    }
    try:
        if intent["component"] == "session":
            return context_writes.session_write(
                repo, **common, topic=payload.get("topic", ""), content=payload.get("content", ""),
            )
        if intent["component"] == "memory":
            return context_writes.memory_write(
                repo, **common, key=payload.get("key", ""), value=payload.get("value", ""),
                tags=payload.get("tags", ""), scope=payload.get("scope", "project"),
            )
        if intent["component"] == "kb":
            return context_writes.kb_write(
                repo, **common, key=payload.get("key", ""), title=payload.get("title", ""),
                body=payload.get("body", ""), category=payload.get("category", ""),
                tags=payload.get("tags", ""), source_refs=payload.get("source_refs", ""),
                replacement_key=payload.get("replacement_key", ""),
            )
    except context_writes.ContextWriteError:
        raise
    raise ContextWriteIntentError("invalid_component")


__all__ = [
    "ContextWriteIntentError", "DECISION_SCHEMA_ID", "INTENT_SCHEMA_ID",
    "append_intent", "apply_accepted_intent", "decisions", "read_verified_intents",
    "record_decision",
]
