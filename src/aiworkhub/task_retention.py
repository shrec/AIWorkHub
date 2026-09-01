"""Preview-first lifecycle for removing old archived tasks safely.

Task rows are never deleted directly from the dashboard.  Only already-
archived tasks older than the repository policy are eligible, and any task
with an undelivered callback remains protected.  A confirmed cleanup stores
the complete task, event and callback rows in the canonical SQLite database
before removing them from the live queue.  The batch can be restored during
the seven-day undo window; permanent purge is a separate explicit action.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple

from . import repo_policy, task_store
from .sqlite_readonly import connect_readonly


SCHEMA_ID = "aiworkhub.task_retention.v1"
UNDO_DAYS = 7
MAX_TASKS_PER_BATCH = 200
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")

# Automatic queue hygiene is deliberately a same-day mechanism.  These are
# operational bounds, not lifecycle authority: malformed configuration turns
# the pass into a no-op.
HYGIENE_TTL_ENV = "AIWORKHUB_TASK_HYGIENE_TTL_SECONDS"
HYGIENE_INTERVAL_ENV = "AIWORKHUB_TASK_HYGIENE_INTERVAL_SECONDS"
HYGIENE_BATCH_ENV = "AIWORKHUB_TASK_HYGIENE_BATCH_SIZE"
HYGIENE_TTL_DEFAULT = 6 * 60 * 60
HYGIENE_INTERVAL_DEFAULT = 15 * 60
HYGIENE_BATCH_DEFAULT = 25
_HYGIENE_TTL_RANGE = (60 * 60, 24 * 60 * 60)
_HYGIENE_INTERVAL_RANGE = (60, 24 * 60 * 60)
_HYGIENE_BATCH_RANGE = (1, 100)
_HYGIENE_SCAN_LIMIT = 2_000
_FAMILY_SUFFIX_RE = re.compile(
    r"^(?P<family>[A-Za-z0-9][A-Za-z0-9_.:-]*?)"
    r"(?:[_-](?:V(?P<version>[1-9][0-9]*)|R(?P<retry>[1-9][0-9]*)))"
    r"(?:_(?:CODEX|CLAUDE|GLM|DEEPSEEK)[A-Z0-9-]*)?$",
    re.IGNORECASE,
)
_REVIEWER_SUFFIX_RE = re.compile(
    r"^(?P<family>[A-Za-z0-9][A-Za-z0-9_.:-]*?)_E(?P<lens>[1-9][0-9]*)_"
    r"(?P<name>[A-Z][A-Z0-9_]*)_V(?P<version>[1-9][0-9]*)"
    r"(?:_(?:CODEX|CLAUDE|GLM|DEEPSEEK)[A-Z0-9-]*)?$"
)
_TERMINAL_LEDGER_STATES = frozenset(
    {
        "accepted",
        "blocked",
        "cancelled",
        "finished",
        "launch_failed",
        "review_ready",
        "superseded",
        "validation_failed",
        "worker_failed",
    }
)
_TERMINAL_TASK_STATES = frozenset({"blocked", "finished", "superseded"})


class TaskFamily(NamedTuple):
    family: str
    generation: tuple[int, int]
    reviewer: bool


class TaskRetentionError(RuntimeError):
    pass


_BATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_retention_batches (
  batch_id TEXT PRIMARY KEY,
  task_count INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL DEFAULT '{}',
  quarantined_at TEXT NOT NULL,
  restore_deadline TEXT NOT NULL,
  restored_at TEXT NOT NULL DEFAULT '',
  purged_at TEXT NOT NULL DEFAULT ''
)
"""
_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_retention_audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL,
  event TEXT NOT NULL,
  task_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
)
"""


def _connect(root: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    path = task_store.canonical_db_path(root)
    if readonly:
        connection = connect_readonly(path)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    return connection


def _policy_days(root: Path | str) -> int:
    try:
        value = repo_policy.load_policy(root)["retention"]["archived_tasks_days"]
        return max(7, min(int(value), 3650))
    except (KeyError, TypeError, ValueError, repo_policy.RepoPolicyError):
        return int(repo_policy.DEFAULT_POLICY["retention"]["archived_tasks_days"])


def _table_exists(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_retention_batches'"
    ).fetchone() is not None


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_BATCH_SCHEMA)
    connection.execute(_AUDIT_SCHEMA)


def _candidate_rows(
    connection: sqlite3.Connection,
    cutoff: str,
) -> tuple[list[sqlite3.Row], int]:
    where = (
        "archived_at<>'' AND archived_at<=? AND NOT EXISTS ("
        "SELECT 1 FROM callback_outbox o WHERE o.task_id=tasks.task_id "
        "AND o.state IN ('pending','inflight'))"
    )
    total = int(
        connection.execute(f"SELECT COUNT(*) FROM tasks WHERE {where}", (cutoff,)).fetchone()[0]
    )
    rows = connection.execute(
        f"SELECT task_id,archived_at,LENGTH(card_json) AS card_bytes FROM tasks "
        f"WHERE {where} ORDER BY archived_at,task_id LIMIT ?",
        (cutoff, MAX_TASKS_PER_BATCH),
    ).fetchall()
    return rows, total


def preview(root: Path | str, *, older_than_days: int | None = None) -> dict[str, Any]:
    policy_days = _policy_days(root)
    days = policy_days if older_than_days is None else int(older_than_days)
    if not 7 <= days <= 3650:
        raise TaskRetentionError("task_retention_days_out_of_range")
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    connection = _connect(root, readonly=True)
    try:
        rows, total = _candidate_rows(connection, cutoff)
        archived_total = int(
            connection.execute("SELECT COUNT(*) FROM tasks WHERE archived_at<>''").fetchone()[0]
        )
        protected_callbacks = int(
            connection.execute(
                "SELECT COUNT(DISTINCT tasks.task_id) FROM tasks JOIN callback_outbox o "
                "ON o.task_id=tasks.task_id WHERE tasks.archived_at<>'' "
                "AND o.state IN ('pending','inflight')"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    candidates = [
        {
            "task_id": str(row["task_id"]),
            "archived_at": str(row["archived_at"]),
            "estimated_bytes": int(row["card_bytes"] or 0),
        }
        for row in rows
    ]
    digest_material = {
        "schema_id": SCHEMA_ID,
        "days": days,
        "candidates": candidates,
    }
    digest = hashlib.sha256(
        json.dumps(digest_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "dry_run": True,
        "repository_scoped": True,
        "policy_days": policy_days,
        "older_than_days": days,
        "cutoff": cutoff,
        "candidate_count": len(candidates),
        "candidate_total": total,
        "candidate_overflow_count": max(0, total - len(candidates)),
        "candidate_bytes": sum(item["estimated_bytes"] for item in candidates),
        "archived_total": archived_total,
        "protected_callback_count": protected_callbacks,
        "preview_digest": digest,
        "candidates": candidates,
    }


def _rows(connection: sqlite3.Connection, table: str, clause: str, values: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE {clause}", values)]


def quarantine(
    root: Path | str,
    *,
    preview_digest: str,
    older_than_days: int | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    if confirm is not True:
        raise TaskRetentionError("task_retention_confirmation_required")
    current = preview(root, older_than_days=older_than_days)
    if not preview_digest or preview_digest != current["preview_digest"]:
        raise TaskRetentionError("task_retention_preview_changed")
    task_ids = [str(item["task_id"]) for item in current["candidates"]]
    if not task_ids:
        return {"ok": True, "quarantined": 0, "batch_id": ""}
    now = datetime.now(timezone.utc)
    batch_id = "tasks-" + now.strftime("%Y%m%dT%H%M%S") + "-" + preview_digest[:10]
    deadline = now + timedelta(days=UNDO_DAYS)
    placeholders = ",".join("?" for _ in task_ids)
    connection = _connect(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        tasks = _rows(connection, "tasks", f"task_id IN ({placeholders})", tuple(task_ids))
        if sorted(str(row["task_id"]) for row in tasks) != sorted(task_ids):
            raise TaskRetentionError("task_retention_candidate_changed")
        events = _rows(connection, "task_events", f"task_id IN ({placeholders})", tuple(task_ids))
        outbox = _rows(connection, "callback_outbox", f"task_id IN ({placeholders})", tuple(task_ids))
        if any(str(row.get("state") or "") in {"pending", "inflight"} for row in outbox):
            raise TaskRetentionError("task_retention_callback_pending")
        batch_ids = sorted({str(row.get("batch_id") or "") for row in outbox if row.get("batch_id")})
        callback_batches: list[dict[str, Any]] = []
        if batch_ids:
            batch_placeholders = ",".join("?" for _ in batch_ids)
            callback_batches = _rows(
                connection,
                "callback_batches",
                f"batch_id IN ({batch_placeholders})",
                tuple(batch_ids),
            )
        payload = {
            "schema_id": SCHEMA_ID,
            "batch_id": batch_id,
            "tasks": tasks,
            "task_events": events,
            "callback_outbox": outbox,
            "callback_batches": callback_batches,
        }
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise TaskRetentionError("task_retention_batch_too_large")
        connection.execute(
            "INSERT INTO task_retention_batches("
            "batch_id,task_count,payload_json,quarantined_at,restore_deadline"
            ") VALUES (?,?,?,?,?)",
            (batch_id, len(tasks), payload_json, now.isoformat(), deadline.isoformat()),
        )
        connection.execute(
            "INSERT INTO task_retention_audit(batch_id,event,task_count,created_at) "
            "VALUES(?,?,?,?)",
            (batch_id, "quarantined", len(tasks), now.isoformat()),
        )
        connection.execute(f"DELETE FROM task_events WHERE task_id IN ({placeholders})", tuple(task_ids))
        connection.execute(f"DELETE FROM callback_outbox WHERE task_id IN ({placeholders})", tuple(task_ids))
        connection.execute(f"DELETE FROM tasks WHERE task_id IN ({placeholders})", tuple(task_ids))
        for callback_batch_id in batch_ids:
            connection.execute(
                "DELETE FROM callback_batches WHERE batch_id=? AND NOT EXISTS ("
                "SELECT 1 FROM callback_outbox WHERE callback_outbox.batch_id=callback_batches.batch_id)",
                (callback_batch_id,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "ok": True,
        "batch_id": batch_id,
        "quarantined": len(task_ids),
        "restore_deadline": deadline.isoformat(),
    }


def list_batches(root: Path | str) -> dict[str, Any]:
    connection = _connect(root, readonly=True)
    try:
        if not _table_exists(connection):
            rows: list[sqlite3.Row] = []
        else:
            rows = connection.execute(
                "SELECT batch_id,task_count,LENGTH(payload_json) AS bytes,quarantined_at,"
                "restore_deadline,restored_at FROM task_retention_batches "
                "WHERE purged_at='' ORDER BY quarantined_at DESC LIMIT 100"
            ).fetchall()
    finally:
        connection.close()
    now = datetime.now(timezone.utc)
    batches = []
    for row in rows:
        restored = bool(str(row["restored_at"] or ""))
        try:
            deadline = datetime.fromisoformat(str(row["restore_deadline"]))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
        except ValueError:
            deadline = now + timedelta(days=UNDO_DAYS)
        batches.append(
            {
                "batch_id": str(row["batch_id"]),
                "task_count": int(row["task_count"] or 0),
                "bytes": int(row["bytes"] or 0),
                "quarantined_at": str(row["quarantined_at"]),
                "restore_deadline": str(row["restore_deadline"]),
                "restored": restored,
                "purge_eligible": restored or now >= deadline,
            }
        )
    return {"ok": True, "batches": batches, "count": len(batches)}


def _insert_rows(connection: sqlite3.Connection, table: str, rows: list[Mapping[str, Any]]) -> None:
    if not rows:
        return
    allowed = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    for row in rows:
        columns = [key for key in row if key in allowed]
        if not columns:
            continue
        placeholders = ",".join("?" for _ in columns)
        connection.execute(
            f"INSERT OR IGNORE INTO {table}({','.join(columns)}) VALUES ({placeholders})",
            tuple(row[key] for key in columns),
        )


def restore(root: Path | str, *, batch_id: str, confirm: bool = False) -> dict[str, Any]:
    if confirm is not True:
        raise TaskRetentionError("task_retention_confirmation_required")
    if not _BATCH_ID_RE.fullmatch(batch_id):
        raise TaskRetentionError("task_retention_batch_id_invalid")
    connection = _connect(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        row = connection.execute(
            "SELECT payload_json,restored_at FROM task_retention_batches WHERE batch_id=? AND purged_at=''",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise TaskRetentionError("task_retention_batch_not_found")
        if str(row["restored_at"] or ""):
            connection.rollback()
            return {"ok": True, "restored": 0, "already_restored": True}
        payload = json.loads(str(row["payload_json"] or "{}"))
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, list):
            raise TaskRetentionError("task_retention_payload_invalid")
        collisions = [
            str(item["task_id"])
            for item in tasks
            if connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (item.get("task_id"),)).fetchone()
        ]
        if collisions:
            raise TaskRetentionError("task_retention_restore_collision:" + ",".join(collisions[:5]))
        _insert_rows(connection, "callback_batches", list(payload.get("callback_batches") or []))
        _insert_rows(connection, "tasks", tasks)
        _insert_rows(connection, "task_events", list(payload.get("task_events") or []))
        _insert_rows(connection, "callback_outbox", list(payload.get("callback_outbox") or []))
        connection.execute(
            "UPDATE task_retention_batches SET restored_at=? WHERE batch_id=?",
            (datetime.now(timezone.utc).isoformat(), batch_id),
        )
        connection.execute(
            "INSERT INTO task_retention_audit(batch_id,event,task_count,created_at) "
            "VALUES(?,?,?,?)",
            (batch_id, "restored", len(tasks), datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
    except (json.JSONDecodeError, TypeError) as exc:
        connection.rollback()
        raise TaskRetentionError("task_retention_payload_invalid") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"ok": True, "restored": len(tasks), "batch_id": batch_id}


def purge(root: Path | str, *, batch_id: str, confirm: bool = False) -> dict[str, Any]:
    if confirm is not True:
        raise TaskRetentionError("task_retention_confirmation_required")
    if not _BATCH_ID_RE.fullmatch(batch_id):
        raise TaskRetentionError("task_retention_batch_id_invalid")
    connection = _connect(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        row = connection.execute(
            "SELECT restore_deadline,restored_at FROM task_retention_batches "
            "WHERE batch_id=? AND purged_at=''",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise TaskRetentionError("task_retention_batch_not_found")
        restored = bool(str(row["restored_at"] or ""))
        deadline = datetime.fromisoformat(str(row["restore_deadline"]))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if not restored and datetime.now(timezone.utc) < deadline:
            raise TaskRetentionError("task_retention_undo_window_active")
        task_count = int(
            connection.execute(
                "SELECT task_count FROM task_retention_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO task_retention_audit(batch_id,event,task_count,created_at) "
            "VALUES(?,?,?,?)",
            (batch_id, "purged", task_count, datetime.now(timezone.utc).isoformat()),
        )
        connection.execute("DELETE FROM task_retention_batches WHERE batch_id=?", (batch_id,))
        connection.commit()
    except ValueError as exc:
        connection.rollback()
        raise TaskRetentionError("task_retention_deadline_invalid") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"ok": True, "purged": True, "batch_id": batch_id}


ACCEPTED_CLEANUP_EVIDENCE_SCHEMA = "aiworkhub.accepted_cleanup_evidence.v1"
_ARTIFACT_GC_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_FINISHED_CLEANUP_STATUSES = frozenset({"finished", "archived", "superseded"})
_LIVE_PROCESS_STATUSES = frozenset({"processing", "review"})
_LIVE_SUPERVISOR_STATUSES = frozenset({"claimed", "in_progress"})
_CLEARED_PREDECESSOR_PHASES = frozenset(
    {"predecessor_unpin_intent", "predecessor_unpinned", "completed"}
)
_ACCEPTED_EVIDENCE_KEYS = frozenset(
    {"schema_id", "task_id", "request_id", "canonical_digest", "predecessor_request_id"}
)
_PROCESS_IDENTITY_KEYS = (
    ("pid", "pid_start_ticks"),
    ("provider_pid", "provider_pid_start_ticks"),
    ("child_pid", "child_pid_start_ticks"),
)
_EVENT_IDENTITY_KEYS = (
    "request_id",
    "task_id",
    "runner",
    "topic",
    "adapter_id",
    "model",
    "pid",
    "pid_start_ticks",
    "provider_pid",
    "provider_pid_start_ticks",
    "stdout_path",
    "stderr_path",
    "metadata_path",
    "supervisor_status_path",
    "cancel_path",
    "sandbox_backend",
    "exit_code",
    "error",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_predecessor_request_id(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def canonical_acceptance_digest(
    *,
    task_id: str,
    request_id: str,
    status: str,
    accepted_request_id: str,
    accept_evidence: Any,
    predecessor_request_id: Any = "",
) -> str:
    payload = {
        "accept_evidence": accept_evidence if isinstance(accept_evidence, dict) else {},
        "accepted_request_id": str(accepted_request_id or ""),
        "predecessor_request_id": _normalize_predecessor_request_id(predecessor_request_id),
        "request_id": str(request_id or ""),
        "status": str(status or ""),
        "task_id": str(task_id or ""),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _load_task_cleanup_row(root: Path | str, task_id: str) -> dict[str, Any] | None:
    connection = _connect(root, readonly=True)
    try:
        row = connection.execute(
            "SELECT task_id, status, worker_status, archived_at, card_json "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            card = json.loads(row["card_json"] or "{}")
        except json.JSONDecodeError:
            card = {}
        if not isinstance(card, dict):
            card = {}
        predecessor = card.get("rework_predecessor")
        return {
            "accept_evidence": (
                card.get("accept_evidence")
                if isinstance(card.get("accept_evidence"), dict)
                else {}
            ),
            "accepted_request_id": str(card.get("accepted_request_id") or "").strip(),
            "card": card,
            "launch_request_id": str(card.get("launch_request_id") or "").strip(),
            "predecessor_request_id": (
                _normalize_predecessor_request_id(predecessor.get("request_id"))
                if isinstance(predecessor, dict)
                else ""
            ),
            "status": task_store.canonical_status(dict(row)),
            "task_id": str(row["task_id"] or ""),
        }
    finally:
        connection.close()


def live_rework_references(root: Path | str, request_id: str) -> tuple[list[str], bool]:
    if not _BATCH_ID_RE.fullmatch(str(request_id or "")):
        return [], False
    try:
        connection = _connect(root, readonly=True)
    except (task_store.TaskStoreError, sqlite3.Error, OSError):
        return [], False
    try:
        rows = connection.execute(
            "SELECT task_id, status, worker_status, archived_at, card_json FROM tasks"
        ).fetchall()
        pins: list[str] = []
        for row in rows:
            status = task_store.canonical_status(dict(row))
            if status in _FINISHED_CLEANUP_STATUSES:
                continue
            try:
                card = json.loads(row["card_json"] or "{}")
            except json.JSONDecodeError:
                return [], False
            if not isinstance(card, dict):
                return [], False
            predecessor = card.get("rework_predecessor")
            if (
                isinstance(predecessor, dict)
                and str(predecessor.get("request_id") or "").strip() == request_id
            ):
                card_id = str(row["task_id"] or "").strip()
                if card_id:
                    pins.append(card_id)
        return sorted(set(pins)), True
    except sqlite3.Error:
        return [], False
    finally:
        connection.close()


def _process_event_log_path(root: Path | str) -> Path:
    from . import process_launcher

    return Path(
        os.environ.get(
            process_launcher.PROCESS_LOG_ENV,
            str(Path(root) / process_launcher.PROCESS_LOG_DEFAULT_REL),
        )
    )


def _read_request_metadata(path: Path) -> Mapping[str, Any] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _merge_identity_fields(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for pid_key, ticks_key in _PROCESS_IDENTITY_KEYS:
        for key in (pid_key, ticks_key):
            if key in source:
                target[key] = source[key]


def _load_request_process_evidence(
    root: Path | str,
    request_id: str,
) -> tuple[Mapping[str, Any] | None, bool]:
    from . import process_event_ledger, process_launcher

    merged: dict[str, Any] = {}
    try:
        for event in process_event_ledger.iter_events(_process_event_log_path(root)):
            if not isinstance(event, Mapping):
                return None, False
            if str(event.get("request_id") or "").strip() != request_id:
                continue
            for key in _EVENT_IDENTITY_KEYS:
                if event.get(key) is not None:
                    merged[key] = event[key]
    except OSError:
        return None, False
    metadata_raw = merged.get("metadata_path")
    if metadata_raw:
        metadata = _read_request_metadata(Path(str(metadata_raw)))
        if metadata is None:
            return None, False
        _merge_identity_fields(merged, metadata)
    status_raw = merged.get("supervisor_status_path")
    if status_raw:
        status = process_launcher.read_supervisor_status(Path(str(status_raw)))
        if not status:
            return None, False
        _merge_identity_fields(merged, status)
    return merged, True


def _pid_role_state(pid: Any, ticks: Any) -> str:
    from .process_launcher import PidIdentityVerdict, _pid_identity_evidence

    evidence = _pid_identity_evidence(pid, ticks)
    if evidence.verdict is PidIdentityVerdict.MATCH:
        return "live"
    if evidence.verdict is PidIdentityVerdict.UNKNOWN:
        return "unknown"
    if evidence.observed_start_ticks is not None:
        return "unknown"
    return "idle"


def _request_process_identity_state(root: Path | str, request_id: str) -> tuple[str, bool]:
    blob, verified = _load_request_process_evidence(root, request_id)
    if not verified or blob is None:
        return "unknown", False
    state = "idle"
    seen_identity = False
    for pid_key, ticks_key in _PROCESS_IDENTITY_KEYS:
        if pid_key not in blob and ticks_key not in blob:
            continue
        seen_identity = True
        role_state = _pid_role_state(blob.get(pid_key), blob.get(ticks_key))
        if role_state == "unknown":
            return "unknown", False
        if role_state == "live":
            state = "live"
    if not seen_identity:
        return "unknown", False
    return state, True


def live_process_holders(root: Path | str, request_id: str) -> tuple[list[str], bool]:
    if not _BATCH_ID_RE.fullmatch(str(request_id or "")):
        return [], False
    identity, identity_verified = _request_process_identity_state(root, request_id)
    if not identity_verified:
        return [], False
    try:
        connection = _connect(root, readonly=True)
    except (task_store.TaskStoreError, sqlite3.Error, OSError):
        return [], False
    try:
        rows = connection.execute(
            "SELECT task_id, status, worker_status, archived_at, card_json FROM tasks"
        ).fetchall()
        holders: list[str] = []
        for row in rows:
            status = task_store.canonical_status(dict(row))
            worker_status = str(row["worker_status"] or "").strip().lower()
            supervisor_live = worker_status in _LIVE_SUPERVISOR_STATUSES
            process_live = status in _LIVE_PROCESS_STATUSES
            try:
                card = json.loads(row["card_json"] or "{}")
            except json.JSONDecodeError:
                if process_live or supervisor_live:
                    return [], False
                continue
            if not isinstance(card, dict):
                if process_live or supervisor_live:
                    return [], False
                continue
            bound_ids = {
                str(card.get("launch_request_id") or "").strip(),
                str(card.get("accepted_request_id") or "").strip(),
            }
            if request_id not in bound_ids:
                continue
            if not process_live and not supervisor_live and identity != "live":
                continue
            card_id = str(row["task_id"] or "").strip()
            if card_id:
                holders.append(card_id)
        return sorted(set(holders)), True
    except sqlite3.Error:
        return [], False
    finally:
        connection.close()


def phase_permits_cleared_predecessor(
    phase_evidence: Mapping[str, Any] | None,
    *,
    request_id: str,
    digest: str,
    predecessor_request_id: str,
) -> bool:
    evidence_predecessor = _normalize_predecessor_request_id(predecessor_request_id)
    if not evidence_predecessor or not isinstance(phase_evidence, Mapping):
        return False
    if str(phase_evidence.get("request_id") or "") != request_id:
        return False
    if str(phase_evidence.get("canonical_digest") or "") != digest:
        return False
    if str(phase_evidence.get("predecessor_unpinned") or "").strip() != evidence_predecessor:
        return False
    phase_name = str(phase_evidence.get("phase") or "")
    if phase_name == "quarantined":
        retry = phase_evidence.get("retry_evidence")
        if not isinstance(retry, dict):
            return False
        phase_name = str(retry.get("phase") or "")
    return phase_name in _CLEARED_PREDECESSOR_PHASES


def validate_accepted_cleanup_evidence(
    root: Path | str,
    evidence: Mapping[str, Any] | None,
    *,
    phase_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    failed = {
        "canonical_digest": "",
        "deleted": False,
        "ok": False,
        "predecessor_request_id": "",
        "reason": "unknown_identity",
        "request_id": "",
        "schema_id": ACCEPTED_CLEANUP_EVIDENCE_SCHEMA,
        "task_id": "",
    }
    if not isinstance(evidence, Mapping):
        return dict(failed)
    extra = set(evidence.keys()) - _ACCEPTED_EVIDENCE_KEYS
    task_id = str(evidence.get("task_id") or "").strip()
    request_id = str(evidence.get("request_id") or "").strip()
    digest = str(evidence.get("canonical_digest") or "").strip().lower()
    predecessor_request_id = _normalize_predecessor_request_id(
        evidence.get("predecessor_request_id")
    )
    failed["predecessor_request_id"] = predecessor_request_id
    failed["request_id"] = request_id
    failed["task_id"] = task_id
    if extra or str(evidence.get("schema_id") or "") != ACCEPTED_CLEANUP_EVIDENCE_SCHEMA:
        return dict(failed)
    if not _TASK_ID_RE.fullmatch(task_id) or not _BATCH_ID_RE.fullmatch(request_id):
        return dict(failed)
    if not _ARTIFACT_GC_DIGEST_RE.fullmatch(digest):
        return dict(failed)
    try:
        row = _load_task_cleanup_row(root, task_id)
    except (task_store.TaskStoreError, sqlite3.Error, OSError):
        failed["reason"] = "unresolved_task"
        return failed
    if row is None:
        failed["reason"] = "unresolved_task"
        return failed
    if row["status"] in _LIVE_PROCESS_STATUSES:
        failed["reason"] = "live_process"
        return failed
    if row["status"] not in _FINISHED_CLEANUP_STATUSES:
        failed["reason"] = "unresolved_task"
        return failed
    if row["accepted_request_id"] != request_id:
        return dict(failed)
    row_predecessor = _normalize_predecessor_request_id(row["predecessor_request_id"])
    resolved_predecessor = row_predecessor
    if predecessor_request_id != row_predecessor:
        if row_predecessor:
            return dict(failed)
        if not phase_permits_cleared_predecessor(
            phase_evidence,
            request_id=request_id,
            digest=digest,
            predecessor_request_id=predecessor_request_id,
        ):
            return dict(failed)
        resolved_predecessor = predecessor_request_id
    expected = canonical_acceptance_digest(
        accept_evidence=row["accept_evidence"],
        accepted_request_id=row["accepted_request_id"],
        predecessor_request_id=predecessor_request_id,
        request_id=request_id,
        status=row["status"],
        task_id=task_id,
    )
    if expected != digest:
        return dict(failed)
    holders, process_verified = live_process_holders(root, request_id)
    if not process_verified:
        failed["reason"] = "ambiguous_ownership"
        return failed
    if holders:
        failed["reason"] = "live_process"
        return failed
    pins, pin_verified = live_rework_references(root, request_id)
    if not pin_verified:
        failed["reason"] = "ambiguous_ownership"
        return failed
    if pins:
        failed["reason"] = "active_rework"
        return failed
    return {
        "accept_evidence": row["accept_evidence"],
        "canonical_digest": digest,
        "deleted": False,
        "ok": True,
        "predecessor_request_id": resolved_predecessor,
        "reason": "",
        "request_id": request_id,
        "schema_id": ACCEPTED_CLEANUP_EVIDENCE_SCHEMA,
        "status": row["status"],
        "task_id": task_id,
    }


def task_family(task_id: str) -> TaskFamily | None:
    """Parse only the explicit queue version/retry/reviewer suffix grammar."""

    reviewer = _REVIEWER_SUFFIX_RE.fullmatch(task_id)
    if reviewer:
        return TaskFamily(
            reviewer.group("family"),
            (int(reviewer.group("version")), int(reviewer.group("lens"))),
            True,
        )
    match = _FAMILY_SUFFIX_RE.fullmatch(task_id)
    if not match:
        return None
    version = int(match.group("version") or 0)
    retry = int(match.group("retry") or 0)
    return TaskFamily(match.group("family"), (version, retry), False)


def _bounded_env_int(name: str, default: int, bounds: tuple[int, int]) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise TaskRetentionError(f"task_hygiene_invalid_config:{name}")
    value = int(raw)
    if not bounds[0] <= value <= bounds[1]:
        raise TaskRetentionError(f"task_hygiene_invalid_config:{name}")
    return value


def hygiene_config() -> dict[str, int]:
    return {
        "ttl_seconds": _bounded_env_int(
            HYGIENE_TTL_ENV, HYGIENE_TTL_DEFAULT, _HYGIENE_TTL_RANGE
        ),
        "interval_seconds": _bounded_env_int(
            HYGIENE_INTERVAL_ENV, HYGIENE_INTERVAL_DEFAULT, _HYGIENE_INTERVAL_RANGE
        ),
        "batch_size": _bounded_env_int(
            HYGIENE_BATCH_ENV, HYGIENE_BATCH_DEFAULT, _HYGIENE_BATCH_RANGE
        ),
    }


def _skip(summary: dict[str, Any], reason: str) -> None:
    summary["skipped"] += 1
    reasons = summary["reasons"]
    reasons[reason] = int(reasons.get(reason, 0)) + 1


def _hygiene_rows(root: Path | str) -> list[dict[str, Any]]:
    connection = _connect(root, readonly=True)
    try:
        rows = connection.execute(
            "SELECT task_id,topic,status,worker_status,claimed_by,updated_at,completed_at,"
            "archived_at,card_json FROM tasks WHERE archived_at='' "
            "ORDER BY updated_at DESC,task_id DESC LIMIT ?",
            (_HYGIENE_SCAN_LIMIT,),
        ).fetchall()
    finally:
        connection.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            card = json.loads(row["card_json"] or "{}")
        except json.JSONDecodeError:
            card = None
        result.append({**dict(row), "card": card})
    return result


def _quality_reviewer(row: Mapping[str, Any]) -> bool:
    card = row.get("card")
    if not isinstance(card, Mapping):
        return False
    parsed = task_family(str(row.get("task_id") or ""))
    return bool(
        parsed
        and parsed.reviewer
        and str(row.get("status") or "").lower() in _TERMINAL_TASK_STATES
        and str(row.get("worker_status") or "").lower()
        in {"blocked", "done", "review", "superseded"}
        and str(card.get("topic") or row.get("topic") or "").lower() == "quality_review"
    )


def _expired(row: Mapping[str, Any], cutoff: datetime) -> bool:
    raw = str(row.get("completed_at") or row.get("updated_at") or "")
    try:
        observed = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return observed <= cutoff


def _candidate_ids(rows: list[dict[str, Any]], cutoff: datetime) -> set[str]:
    families: dict[str, list[tuple[TaskFamily, dict[str, Any]]]] = {}
    selected: set[str] = set()
    for row in rows:
        task_id = str(row.get("task_id") or "")
        parsed = task_family(task_id)
        if parsed and not parsed.reviewer:
            families.setdefault(parsed.family, []).append((parsed, row))
        elif _quality_reviewer(row) and _expired(row, cutoff):
            selected.add(task_id)
    for members in families.values():
        # Generation first and task_id second makes the retained head stable
        # even if malformed historical data duplicates a generation.
        head = max(members, key=lambda item: (item[0].generation, str(item[1]["task_id"])))
        for parsed, row in members:
            task_id = str(row["task_id"])
            if row is head[1]:
                continue
            if (
                str(row.get("status") or "").lower() in _TERMINAL_TASK_STATES
                and _expired(row, cutoff)
            ):
                selected.add(task_id)
    return selected


def _latest_process_row(root: Path | str, request_id: str) -> Mapping[str, Any] | None:
    from . import process_event_ledger

    return process_event_ledger.latest_events(
        _process_event_log_path(root), key_field="request_id"
    ).get(request_id)


def _final_archive_fence(
    root: Path | str, task_id: str, expected_request_id: str
) -> tuple[dict[str, Any] | None, str]:
    # This is intentionally a fresh canonical read immediately before the
    # authority call.  Selection-time card bytes are never archive authority.
    card = task_store.get_task(root, task_id)
    if not isinstance(card, dict):
        return None, "task_missing"
    if str(card.get("task_id") or "") != task_id:
        return None, "task_mismatch"
    request_id = str(card.get("launch_request_id") or "").strip()
    if not request_id or request_id != expected_request_id:
        return None, "request_mismatch"
    status = str(card.get("status") or "").lower()
    worker_status = str(card.get("worker_status") or "").lower()
    if status not in _TERMINAL_TASK_STATES or worker_status in {
        "claimed", "in_progress", "processing", "running", "starting"
    }:
        return None, "task_live"
    # Terminal failures intentionally retain ``claimed_by`` for attribution.
    # It is not reservation authority once both canonical task state and the
    # exact latest process-ledger row are terminal.  Explicit reservation
    # artifacts remain a hard fence here, and archive_task performs its own
    # final transactional reservation check.
    reservation_keys = ("launch_reservation", "reservation", "reservation_id")
    if any(card.get(key) for key in reservation_keys):
        return None, "task_reserved"
    terminal_review = card.get("terminal_review")
    terminal_evidence = (
        terminal_review.get("evidence") if isinstance(terminal_review, Mapping) else None
    )
    if any(
        card.get(key)
        for key in (
            "candidate",
            "candidate_evidence",
            "retained_candidate",
            "retained_workspace",
            "rework_predecessor",
            "workspace",
        )
    ) or (
        isinstance(terminal_evidence, Mapping)
        and any(
            terminal_evidence.get(key)
            for key in ("candidate", "candidate_evidence", "changed_path_hashes", "workspace")
        )
    ):
        return None, "retained_evidence"
    connection = _connect(root, readonly=True)
    try:
        callback = connection.execute(
            "SELECT 1 FROM callback_outbox WHERE task_id=? AND state IN ('pending','inflight') LIMIT 1",
            (task_id,),
        ).fetchone()
    finally:
        connection.close()
    if callback:
        return None, "callback_live"
    ledger = _latest_process_row(root, request_id)
    if not isinstance(ledger, Mapping):
        return None, "ledger_missing"
    if str(ledger.get("task_id") or "") != task_id:
        return None, "ledger_task_mismatch"
    if str(ledger.get("request_id") or "") != request_id:
        return None, "ledger_request_mismatch"
    ledger_state = str(ledger.get("state") or ledger.get("terminal_state") or "").lower()
    if ledger_state in {"running", "starting", "processing", "claimed", "in_progress"}:
        return None, "ledger_live"
    if ledger_state not in _TERMINAL_LEDGER_STATES:
        return None, "ledger_not_terminal"
    ledger_timestamp = str(ledger.get("timestamp") or "")
    card_updated = str(card.get("updated_at") or "")
    try:
        if datetime.fromisoformat(ledger_timestamp) < datetime.fromisoformat(card_updated):
            return None, "ledger_stale"
    except (TypeError, ValueError):
        return None, "ledger_stale"
    return card, ""


def run_automatic_hygiene(
    root: Path | str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Archive a bounded set through ``task_store.archive_task`` only."""

    summary: dict[str, Any] = {
        "ok": True,
        "scanned": 0,
        "eligible": 0,
        "archived": 0,
        "skipped": 0,
        "reasons": {},
    }
    if os.environ.get("AIWORKHUB_ALLOW_WRITES") != "1":
        summary.update(ok=False, reasons={"writes_disabled": 1})
        return summary
    try:
        config = hygiene_config()
        rows = _hygiene_rows(root)
    except (TaskRetentionError, task_store.TaskStoreError, sqlite3.Error, OSError) as exc:
        summary.update(ok=False, reasons={str(exc)[:80] or "scan_failed": 1})
        return summary
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current - timedelta(seconds=config["ttl_seconds"])
    candidates = _candidate_ids(rows, cutoff)
    summary["scanned"] = len(rows)
    summary["eligible"] = len(candidates)
    indexed = {str(row["task_id"]): row for row in rows}
    # Bottleneck audit B4 (2026-09-01): the batch budget bounds ARCHIVE
    # attempts, never the candidates examined.  Taking a fixed sorted prefix
    # of candidates let 25 permanently fenced cards (retained evidence) occupy
    # the whole batch on every run, so the other 238 eligible cards were never
    # examined and the graveyard could not drain (measured: eligible 263,
    # archived 0, batch_limited 238 on every bootstrap).  Fence reads are
    # cheap canonical reads; the bounded, expensive step is the archive.
    batch_size = int(config["batch_size"])
    attempted = 0
    overflow = 0
    for task_id in sorted(candidates):
        if attempted >= batch_size:
            overflow += 1
            continue
        initial = indexed[task_id]
        card = initial.get("card")
        request_id = (
            str(card.get("launch_request_id") or "").strip()
            if isinstance(card, Mapping)
            else ""
        )
        if not request_id:
            _skip(summary, "request_missing")
            continue
        try:
            fenced, reason = _final_archive_fence(root, task_id, request_id)
        except (task_store.TaskStoreError, sqlite3.Error, OSError, ValueError):
            _skip(summary, "authority_unavailable")
            continue
        if fenced is None:
            _skip(summary, reason)
            continue
        attempted += 1
        try:
            ok, authority_reason = task_store.archive_task(
                root,
                task_id,
                actor="manager",
                reason="automatic_task_hygiene",
                operation="archived",
            )
        except (task_store.TaskStoreError, sqlite3.Error, OSError):
            _skip(summary, "archive_authority_error")
            continue
        if not ok:
            _skip(summary, f"archive_refused:{str(authority_reason)[:48]}")
            continue
        summary["archived"] += 1
    if overflow:
        summary["skipped"] += overflow
        summary["reasons"]["batch_limited"] = overflow
    summary["reasons"] = dict(
        sorted(summary["reasons"].items(), key=lambda item: (-item[1], item[0]))[:12]
    )
    return summary


__all__ = [
    "SCHEMA_ID",
    "TaskRetentionError",
    "canonical_acceptance_digest",
    "list_batches",
    "live_process_holders",
    "live_rework_references",
    "phase_permits_cleared_predecessor",
    "preview",
    "purge",
    "quarantine",
    "restore",
    "hygiene_config",
    "run_automatic_hygiene",
    "task_family",
    "validate_accepted_cleanup_evidence",
]
