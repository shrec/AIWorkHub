#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO"

export PYTHONPATH="$REPO/tools/geoai-task-mcp/src"
export AIWORKHUB_REPO="$REPO"

python3 - <<'PY'
import json
from pathlib import Path

from aiworkhub import stale_recovery

repo = Path(__import__("os").environ["AIWORKHUB_REPO"])
eval_path = repo / "tools/geoai-task-mcp/eval/mcp_stale_recovery_orchestrator_b287_v1.json"

inbox = {
    "stale_processing": [
        {"task_id": "T_KILL", "runner": "r1", "topic": "task_mcp", "zombie_hint": True},
        {"task_id": "T_REQUEUE", "runner": "r2", "topic": "task_mcp"},
        {"task_id": "T_DONE", "runner": "r3", "topic": "task_mcp", "validation_status": "PASS"},
        {"task_id": "T_ESC", "runner": "r4", "topic": "task_mcp"},
    ]
}
launch_log = {
    "last_entries": [
        {
            "request_id": "rq1",
            "task_id": "T_REQUEUE",
            "runner": "r2",
            "topic": "task_mcp",
            "to_state": "blocked_launch_disabled",
            "decision": "blocked_launch_disabled",
        }
    ]
}
result = stale_recovery.build_recovery_actions(inbox=inbox, launch_log=launch_log)
actions = result["recovery_actions"]
by_task = {row["task_id"]: row for row in actions}
assert by_task["T_KILL"]["action_type"] == "kill_zombie"
assert by_task["T_KILL"]["confidence"] == "low"
assert by_task["T_REQUEUE"]["action_type"] == "requeue"
assert by_task["T_DONE"]["action_type"] == "mark_done"
assert by_task["T_ESC"]["action_type"] == "escalate"
assert [row["action_type"] for row in actions] == ["kill_zombie", "requeue", "mark_done", "escalate"], actions
assert all(row["readonly"] is True for row in actions)
assert all(v is False for v in result["authority_flags"].values())

doc = {
    "schema_id": "aiworkhub.mcp_stale_recovery_orchestrator.b287.v1",
    "task_id": "CLAUDE_TASK_MCP_STALE_RECOVERY_ORCHESTRATOR_B287_V1",
    "runner": "claude_task_mcp_stale_recovery_b287",
    "topic": "task_mcp",
    "verdict": "PASS",
    "metrics": result["counts"],
    "authority_flags": result["authority_flags"],
    "action_types": [row["action_type"] for row in actions],
}
eval_path.parent.mkdir(parents=True, exist_ok=True)
eval_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
print("B287_STALE_RECOVERY_VERDICT=PASS")
PY

grep -q '"verdict": "PASS"' tools/geoai-task-mcp/eval/mcp_stale_recovery_orchestrator_b287_v1.json
