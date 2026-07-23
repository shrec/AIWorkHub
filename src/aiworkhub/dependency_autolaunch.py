from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


SCHEMA_ID = "aiworkhub.dependency_autolaunch_outcome.v1"
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
        "SELECT task_id, runner, topic, status, worker_status, card_json FROM tasks WHERE COALESCE(archived_at, '') = ''"
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
        )
    return rows


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
            if capacity is not None and launched_count >= capacity:
                outcome["delayed"].append({"task_id": child.task_id, "reason": "capacity"})
                continue
            result = dict(launch(child.task_id, child.runner, child.topic, _request_id(trigger_task_id, child.task_id)))
            if result.get("ok"):
                launched_count += 1
                outcome["launched"].append({"task_id": child.task_id, "runner": child.runner, "topic": child.topic})
            else:
                outcome["delayed"].append(
                    {
                        "task_id": child.task_id,
                        "reason": "launch_not_claimed",
                        "stderr": str(result.get("stderr") or result.get("error") or "")[:240],
                    }
                )
        return outcome
    finally:
        conn.close()


def reconcile_after_accept(repo_root: Path | str, accepted_task_id: str, launch: LaunchFn) -> dict[str, Any]:
    return reconcile(repo_root, trigger_task_id=accepted_task_id, launch=launch)


def reconcile_startup(repo_root: Path | str, launch: LaunchFn, *, capacity: int | None = None) -> dict[str, Any]:
    return reconcile(repo_root, trigger_task_id="startup", launch=launch, capacity=capacity)
