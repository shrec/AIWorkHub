from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import callback_store


SCHEMA_ID = "aiworkhub.dependency_autolaunch_outcome.v1"

# NF-2026-00549 M4: the write-gate audit measured 3,047 launch-blocked
# entries over 40 days, re-issued in bursts of 4 per 200ms because every
# reconcile trigger re-attempted every denied launch identically.  A denial
# whose reason is deterministic for the current card configuration can only
# repeat, so it holds until the card row changes; anything else backs off
# exponentially instead of retrying on the next trigger.
DETERMINISTIC_DENIAL_PREFIXES = (
    "card_scoped_task_unresolved",
    "card_scoped_identity_mismatch",
    "workforce_route_absent",
    "workforce_model_mismatch",
    "workforce_route_disabled",
    "workforce_route_unavailable",
    "workforce_route_risk_incapable",
    "runner_mismatch",
    "malformed_topic",
    "topic_mismatch",
    "identical_relaunch_blocked",
    "repo_policy",
)
TRANSIENT_BACKOFF_BASE_SECONDS = 5.0
TRANSIENT_BACKOFF_MAX_SECONDS = 300.0
SUCCESS_STATUSES = frozenset({"finished", "completed", "stale_already_done"})
SUCCESS_WORKER_STATUSES = frozenset({"done"})
FAILED_STATUSES = frozenset({"failed", "cancelled", "canceled"})
FAILED_WORKER_STATUSES = frozenset(
    {
        "failed",
        "cancelled",
        "canceled",
        "worker_failed",
        "validation_failed",
        "launch_failed",
    }
)
ACTIVE_STATUSES = frozenset({"pending"})
ACTIVE_WORKER_STATUSES = frozenset({"", "unclaimed"})


LaunchFn = Callable[[str, str, str, str], Mapping[str, Any]]


@dataclass(frozen=True)
class _TaskRow:
    task_id: str
    runner: str
    topic: str
    status: str
    worker_status: str
    card: dict[str, Any]
    updated_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_card(raw: object) -> dict[str, Any]:
    try:
        card = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        card = {}
    return card if isinstance(card, dict) else {}


def _depends_on(card: Mapping[str, Any]) -> list[str]:
    value = card.get("depends_on")
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        dep = str(item or "").strip()
        if dep and dep not in out:
            out.append(dep)
    return out


def _state(row: _TaskRow) -> str:
    status = (row.status or str(row.card.get("status") or "")).strip().lower()
    worker = (row.worker_status or str(row.card.get("worker_status") or "")).strip().lower()
    if status in SUCCESS_STATUSES or worker in SUCCESS_WORKER_STATUSES:
        return "success"
    if status in FAILED_STATUSES or worker in FAILED_WORKER_STATUSES:
        return "failed"
    if status == "review" or worker == "review":
        substatus = str(row.card.get("substatus") or "").strip().lower()
        if substatus in FAILED_WORKER_STATUSES or substatus == "dependency_blocked":
            return "failed"
        return "review"
    if status in {"processing", "in_progress"} or worker in {"claimed", "in_progress"}:
        return "processing"
    return "pending"


def _repo_db(repo_root: Path) -> Path:
    return Path(repo_root) / ".aiworkhub" / "tasking" / "task_queue.sqlite"


def _read_rows(conn: sqlite3.Connection) -> dict[str, _TaskRow]:
    rows: dict[str, _TaskRow] = {}
    for row in conn.execute(
        "SELECT task_id, runner, topic, status, worker_status, card_json, updated_at "
        "FROM tasks WHERE COALESCE(archived_at, '') = ''"
    ):
        card = _load_card(row["card_json"])
        task_id = str(row["task_id"])
        rows[task_id] = _TaskRow(
            task_id=task_id,
            runner=str(row["runner"] or card.get("runner") or ""),
            topic=str(row["topic"] or card.get("topic") or ""),
            status=str(row["status"] or card.get("status") or ""),
            worker_status=str(row["worker_status"] or card.get("worker_status") or ""),
            card=card,
            updated_at=str(row["updated_at"] or ""),
        )
    return rows


def _ensure_holds_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dependency_autolaunch_holds ("
        "task_id TEXT PRIMARY KEY, reason TEXT NOT NULL, kind TEXT NOT NULL, "
        "attempts INTEGER NOT NULL, card_updated_at TEXT NOT NULL, "
        "next_attempt_at TEXT NOT NULL, recorded_at TEXT NOT NULL)"
    )


def _denial_kind(reason: str) -> str:
    text = str(reason or "").strip()
    for prefix in DETERMINISTIC_DENIAL_PREFIXES:
        if prefix in text:
            return "deterministic"
    return "transient"


def _hold_for(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT reason, kind, attempts, card_updated_at, next_attempt_at "
        "FROM dependency_autolaunch_holds WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "reason": str(row["reason"]),
        "kind": str(row["kind"]),
        "attempts": int(row["attempts"]),
        "card_updated_at": str(row["card_updated_at"]),
        "next_attempt_at": str(row["next_attempt_at"]),
    }


def _record_denial(
    conn: sqlite3.Connection, child: _TaskRow, reason: str
) -> dict[str, Any]:
    prior = _hold_for(conn, child.task_id)
    attempts = (prior["attempts"] if prior is not None else 0) + 1
    kind = _denial_kind(reason)
    if kind == "deterministic":
        next_attempt_at = ""
    else:
        delay = min(
            TRANSIENT_BACKOFF_MAX_SECONDS,
            TRANSIENT_BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)),
        )
        next_attempt_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat()
    conn.execute(
        "INSERT INTO dependency_autolaunch_holds"
        "(task_id, reason, kind, attempts, card_updated_at, next_attempt_at, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(task_id) DO UPDATE SET reason=excluded.reason, "
        "kind=excluded.kind, attempts=excluded.attempts, "
        "card_updated_at=excluded.card_updated_at, "
        "next_attempt_at=excluded.next_attempt_at, recorded_at=excluded.recorded_at",
        (
            child.task_id,
            str(reason or "")[:240],
            kind,
            attempts,
            child.updated_at,
            next_attempt_at,
            _now(),
        ),
    )
    return {"kind": kind, "attempts": attempts, "next_attempt_at": next_attempt_at}


def _clear_hold(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(
        "DELETE FROM dependency_autolaunch_holds WHERE task_id=?", (task_id,)
    )


def _request_id(parent_task_id: str, child_task_id: str) -> str:
    return f"dependency-autolaunch:{parent_task_id}:{child_task_id}"


def _mark_dependency_blocked(
    conn: sqlite3.Connection,
    child: _TaskRow,
    blocked_by: Iterable[str],
    trigger_task_id: str,
) -> bool:
    now = _now()
    blocked = sorted(set(blocked_by))
    card = dict(child.card)
    card.update(
        status="review",
        worker_status="review",
        substatus="dependency_blocked",
        dependency_blocked_by=blocked,
    )
    cur = conn.execute(
        "UPDATE tasks SET status='review', worker_status='review', card_json=?, updated_at=? "
        "WHERE task_id=? AND status='pending' AND worker_status='unclaimed'",
        (json.dumps(card, ensure_ascii=False), now, child.task_id),
    )
    if cur.rowcount != 1:
        return False
    conn.execute(
        "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) VALUES(?,?,?,?,?)",
        (
            child.task_id,
            "dependency_blocked",
            "codex",
            json.dumps(
                {
                    "trigger_task_id": trigger_task_id,
                    "blocked_by": blocked,
                    "schema_id": SCHEMA_ID,
                },
                ensure_ascii=False,
            ),
            now,
        ),
    )
    # dependency_blocked is still a terminal review outcome.  Enqueue its
    # manager wake in the same transaction instead of relying solely on the
    # dispatcher's repair scan.
    origin_thread_id = str(
        child.card.get("origin_thread_id") or ""
    ).strip()
    callback_store.enqueue_callback(
        conn,
        child.task_id,
        origin_thread_id,
        "blocked",
        provider=str(child.card.get("coordinator_provider") or "").strip().lower(),
        episode_id=str(child.card.get("claim_epoch") or 0),
    )
    return True


def reconcile(
    repo_root: Path | str,
    *,
    trigger_task_id: str = "",
    launch: LaunchFn,
    capacity: int | None = None,
) -> dict[str, Any]:
    """Launch newly-ready dependents through the canonical exact claim/start hook.

    The durable exact-once guard is the task row itself: children are updated
    from pending/unclaimed to processing/claimed only by ``launch``. Reloads,
    repeated accept hooks, and concurrent reconcilers therefore race on the
    canonical ``claim_start_exact`` compare-and-update instead of an auxiliary
    resolver state file.
    """
    root = Path(repo_root)
    db = _repo_db(root)
    outcome: dict[str, Any] = {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "repo_root": str(root),
        "trigger_task_id": trigger_task_id,
        "launched": [],
        "blocked": [],
        "delayed": [],
        "skipped": [],
    }
    if not db.exists():
        outcome["ok"] = False
        outcome["error"] = "task_db_missing"
        return outcome

    conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        callback_store.init_db(conn)
        _ensure_holds_table(conn)
        conn.commit()
        rows = _read_rows(conn)
        launched_count = 0
        for child in sorted(rows.values(), key=lambda r: r.task_id):
            deps = _depends_on(child.card)
            if not deps:
                continue
            child_state = _state(child)
            if child_state != "pending" or child.worker_status.strip().lower() not in ACTIVE_WORKER_STATUSES:
                outcome["skipped"].append({"task_id": child.task_id, "reason": f"not_pending_unclaimed:{child_state}"})
                continue
            dep_rows = [rows.get(dep) for dep in deps]
            missing = [dep for dep, dep_row in zip(deps, dep_rows) if dep_row is None]
            if missing:
                outcome["delayed"].append({"task_id": child.task_id, "reason": "missing_dependencies", "dependencies": missing})
                continue
            failed = sorted(dep.task_id for dep in dep_rows if dep is not None and _state(dep) == "failed")
            if failed:
                if _mark_dependency_blocked(conn, child, failed, trigger_task_id):
                    conn.commit()
                    outcome["blocked"].append({"task_id": child.task_id, "blocked_by": failed})
                else:
                    conn.rollback()
                    outcome["delayed"].append({"task_id": child.task_id, "reason": "dependency_block_race"})
                continue
            waiting = [dep.task_id for dep in dep_rows if dep is not None and _state(dep) != "success"]
            if waiting:
                outcome["delayed"].append({"task_id": child.task_id, "reason": "waiting_dependencies", "dependencies": waiting})
                continue
            hold = _hold_for(conn, child.task_id)
            if hold is not None:
                if child.updated_at and child.updated_at > hold["card_updated_at"]:
                    # The card changed since the recorded denial: the hold no
                    # longer describes this configuration, so it is released.
                    _clear_hold(conn, child.task_id)
                    conn.commit()
                elif hold["kind"] == "deterministic":
                    outcome["skipped"].append(
                        {
                            "task_id": child.task_id,
                            "reason": "deterministic_denial_hold:"
                            + hold["reason"][:120],
                        }
                    )
                    continue
                elif hold["next_attempt_at"] > _now():
                    outcome["delayed"].append(
                        {
                            "task_id": child.task_id,
                            "reason": "transient_backoff_hold",
                            "next_attempt_at": hold["next_attempt_at"],
                        }
                    )
                    continue
            if capacity is not None and launched_count >= capacity:
                outcome["delayed"].append({"task_id": child.task_id, "reason": "capacity"})
                continue
            result = dict(launch(child.task_id, child.runner, child.topic, _request_id(trigger_task_id, child.task_id)))
            if result.get("ok"):
                launched_count += 1
                _clear_hold(conn, child.task_id)
                conn.commit()
                outcome["launched"].append({"task_id": child.task_id, "runner": child.runner, "topic": child.topic})
            else:
                denial = str(result.get("stderr") or result.get("error") or "")
                hold_state = _record_denial(conn, child, denial)
                conn.commit()
                outcome["delayed"].append(
                    {
                        "task_id": child.task_id,
                        "reason": "launch_not_claimed",
                        "stderr": denial[:240],
                        "denial_kind": hold_state["kind"],
                        "attempts": hold_state["attempts"],
                        "next_attempt_at": hold_state["next_attempt_at"],
                    }
                )
        return outcome
    finally:
        conn.close()


def reconcile_after_accept(repo_root: Path | str, accepted_task_id: str, launch: LaunchFn) -> dict[str, Any]:
    return reconcile(repo_root, trigger_task_id=accepted_task_id, launch=launch)


def reconcile_startup(repo_root: Path | str, launch: LaunchFn, *, capacity: int | None = None) -> dict[str, Any]:
    return reconcile(repo_root, trigger_task_id="startup", launch=launch, capacity=capacity)
