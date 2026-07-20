from __future__ import annotations

from typing import Any

from geoai_task_mcp import completion_inbox, launch_queue_persist


ACTION_RANK = {
    "kill_zombie": 0,
    "requeue": 1,
    "mark_done": 2,
    "escalate": 3,
}


def _open_entries_by_task(launch_log: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    entries = launch_log.get("last_entries", []) or []
    out: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        task_id = entry.get("task_id")
        if not task_id:
            continue
        if entry.get("decision") == "rolled_back":
            continue
        if entry.get("to_state") not in {"queued", "planned", "blocked_launch_disabled"}:
            continue
        out.setdefault(str(task_id), []).append(entry)
    return out


def classify_stale_row(row: dict[str, Any], open_entries: list[dict[str, Any]]) -> dict[str, Any]:
    task_id = str(row.get("task_id") or "")
    validation_status = str(row.get("validation_status") or row.get("latest_validation_status") or "").upper()
    has_artifact = bool(row.get("has_artifact") or row.get("artifact_present") or row.get("artifacts_existing_count"))
    zombie_hint = bool(row.get("zombie_hint") or row.get("process_state") == "zombie")

    if zombie_hint:
        action_type = "kill_zombie"
        confidence = "low" if not row.get("pid") else "medium"
        reason = "stale row has zombie hint; pid-aware kill remains low-confidence until real pid tracking is available"
    elif open_entries:
        action_type = "requeue"
        confidence = "medium"
        reason = "stale processing row has an open launch-queue request; recommend requeue/owner recovery before a fresh request"
    elif validation_status == "PASS" or has_artifact:
        action_type = "mark_done"
        confidence = "medium"
        reason = "stale row appears to have successful validation or artifacts; recommend Codex review/finalization"
    else:
        action_type = "escalate"
        confidence = "low"
        reason = "stale row lacks launch-log evidence, validation success, artifacts, or pid data"

    return {
        "task_id": task_id,
        "runner": row.get("runner") or row.get("claimed_by"),
        "topic": row.get("topic"),
        "action_type": action_type,
        "confidence": confidence,
        "reason": reason,
        "rank": ACTION_RANK[action_type],
        "readonly": True,
    }


def build_recovery_actions(
    *,
    inbox: dict[str, Any] | None = None,
    launch_log: dict[str, Any] | None = None,
    topic: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Read-only stale recovery recommendations.

    No taskctl write commands are issued here. When ``inbox``/``launch_log``
    are omitted, this reads the existing completion inbox and launch audit log.
    """
    inbox = inbox if inbox is not None else completion_inbox.build_completion_inbox(topic=topic, limit=limit)
    launch_log = launch_log if launch_log is not None else launch_queue_persist.read_persisted_log(max_entries=limit)
    open_by_task = _open_entries_by_task(launch_log)
    stale_rows = inbox.get("stale_processing", []) or []
    actions = [
        classify_stale_row(row, open_by_task.get(str(row.get("task_id") or ""), []))
        for row in stale_rows
    ]
    actions.sort(key=lambda row: (row["rank"], row.get("task_id") or ""))
    return {
        "tool": "geoai_task_stale_recovery_recommend",
        "contract": "B287_v1_readonly_recommendations",
        "readonly": True,
        "topic": topic,
        "recovery_actions": actions,
        "counts": {
            "stale_processing": len(stale_rows),
            "recovery_actions": len(actions),
            "by_action": {
                action: sum(1 for row in actions if row["action_type"] == action)
                for action in ACTION_RANK
            },
        },
        "authority_flags": {
            "runtime_authority": False,
            "support_authority": False,
            "queue_write": False,
            "audit_write": False,
            "process_launch": False,
            "agent_launch": False,
        },
    }
