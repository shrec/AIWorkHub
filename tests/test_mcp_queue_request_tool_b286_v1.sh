#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

export PYTHONPATH="$REPO/tools/geoai-task-mcp/src"
export GEOAI_REPO="$REPO"
export GEOAI_TASK_MCP_LAUNCH_QUEUE_LOG_PATH="$TMPDIR/launch_queue_audit.jsonl"
unset GEOAI_TASK_MCP_ALLOW_WRITES

python3 - <<'PY'
import json
import os
from pathlib import Path

from geoai_task_mcp import core, launch_queue_contract

repo = Path(os.environ["GEOAI_REPO"])
log_path = Path(os.environ["GEOAI_TASK_MCP_LAUNCH_QUEUE_LOG_PATH"])
eval_path = repo / "tools/geoai-task-mcp/eval/mcp_queue_request_tool_b286_v1.json"

requested_at = "2026-07-09T00:00:00+00:00"
task_id = "B286_FIXTURE_TASK"

assert launch_queue_contract.LAUNCH_IMPLEMENTED is False

gate_off = core.queue_request(
    task_id=task_id,
    runner="claude_task_mcp_queue_request_b286",
    topic="task_mcp",
    adapter_id="claude_cli",
    model="claude-test",
    owner_prompt="dryrun",
    requested_at=requested_at,
)
assert gate_off["ok"] is False, gate_off
assert gate_off["persisted_entry"] is None, gate_off
assert not log_path.exists(), "write gate off must not create launch log"

os.environ["GEOAI_TASK_MCP_ALLOW_WRITES"] = "1"
first = core.queue_request(
    task_id=task_id,
    runner="claude_task_mcp_queue_request_b286",
    topic="task_mcp",
    adapter_id="claude_cli",
    model="claude-test",
    owner_prompt="dryrun",
    requested_at=requested_at,
)
assert first["ok"] is True, first
assert first["persisted_entry"] is not None, first
assert first["decision"]["permitted"] is False, first
assert first["decision"]["decision"] == "blocked_launch_disabled", first
assert first["request"]["request_id"] == launch_queue_contract.deterministic_request_id(task_id, "claude_task_mcp_queue_request_b286", requested_at)
assert log_path.exists(), "write gate on should append one launch log row"
lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
assert len(lines) == 1, lines
entry = json.loads(lines[0])
for key in ("model", "owner_prompt", "requested_at", "stale_timeout_seconds", "rollback_of_request_id"):
    assert key in entry, entry

again = core.queue_request(
    task_id=task_id,
    runner="claude_task_mcp_queue_request_b286",
    topic="task_mcp",
    adapter_id="claude_cli",
    model="claude-test",
    owner_prompt="dryrun",
    requested_at=requested_at,
)
assert again["ok"] is True, again
assert again["idempotent_noop"] is True, again
assert again["persisted_entry"] is None, again
assert len([ln for ln in log_path.read_text().splitlines() if ln.strip()]) == 1

dupe = core.queue_request(
    task_id=task_id,
    runner="claude_task_mcp_queue_request_b286_other",
    topic="task_mcp",
    adapter_id="claude_cli",
    model="claude-test",
    owner_prompt="dryrun",
    requested_at="2026-07-09T00:00:01+00:00",
)
assert dupe["ok"] is False, dupe
assert dupe["duplicate_runner_blocked"] is True, dupe
assert dupe["persisted_entry"] is None, dupe
assert len([ln for ln in log_path.read_text().splitlines() if ln.strip()]) == 1

bad = core.queue_request(
    task_id="B286_BAD_ADAPTER",
    runner="claude_task_mcp_queue_request_b286",
    topic="task_mcp",
    adapter_id="not_real",
    requested_at=requested_at,
)
assert bad["ok"] is False and bad["persisted_entry"] is None
assert "unknown_adapter" in bad["blocked_reason"]

doc = {
    "schema_id": "geoai.mcp_queue_request_tool.b286.v1",
    "task_id": "CLAUDE_TASK_MCP_QUEUE_REQUEST_TOOL_EXTEND_B286_V1",
    "runner": "claude_task_mcp_queue_request_b286",
    "topic": "task_mcp",
    "verdict": "PASS",
    "authority_flags": {
        "runtime_authority": False,
        "support_authority": False,
        "training_authority": False,
        "process_launch": False,
        "queue_write_gated": True,
    },
    "metrics": {
        "gate_off_log_created": False,
        "gate_on_lines_after_first": 1,
        "lines_after_idempotent_retry": 1,
        "lines_after_duplicate_runner": 1,
        "launch_implemented": launch_queue_contract.LAUNCH_IMPLEMENTED,
    },
    "checks": {
        "write_gate_off_no_append": True,
        "write_gate_on_one_append": True,
        "idempotency_no_second_append": True,
        "duplicate_runner_no_append": True,
        "unknown_adapter_no_append": True,
        "launch_still_disabled": launch_queue_contract.LAUNCH_IMPLEMENTED is False,
    },
}
eval_path.parent.mkdir(parents=True, exist_ok=True)
eval_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
print("B286_QUEUE_REQUEST_TOOL_VERDICT=PASS")
PY

grep -q '"verdict": "PASS"' tools/geoai-task-mcp/eval/mcp_queue_request_tool_b286_v1.json
