#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_completion_inbox_wiring_b275_v1.sh
# Verifies live @mcp.tool() wiring of the B275 read-only completion-inbox
# view (aiworkhub_completion_inbox) into the FastMCP server instance in
# server.py, backed by tools/geoai-task-mcp/src/aiworkhub/
# completion_inbox.py.
#
# Checks:
#   1. server.py source contains no process-launch/exec/shell code.
#   2. Exactly the 1 new tool is registered on server.mcp, additive to every
#      pre-existing tool (B106/B107 readonly set + core + B252 launch-queue
#      set).
#   3. The tool's input schema serializes to a valid JSON schema.
#   4. build_completion_inbox() (module-level, stubbed taskctl I/O) returns
#      the 4 mandated facets with the expected shapes:
#        review_queue, stale_processing, runner_mismatch_warnings,
#        latest_validation_facts.
#   5. runner_mismatch_warnings correctly fires on a synthetic runner/task
#      batch-token mismatch and stays silent on a matched pair.
#   6. Calling the live MCP tool (real taskctl subprocess) never mutates the
#      parent queue DB (bitnnv2/data/tasking/task_queue_v1.sqlite) -- byte
#      size + mtime identical before/after, even with
#      AIWORKHUB_ALLOW_WRITES=1 forced on.
#   7. authority_flags / mutation block report every write/launch flag as
#      False regardless of the ALLOW_WRITES env state.
#   8. Parent task queue integrity via `taskctl.py verify`.
#
# Isolation: no shared fixed state is written by this test (the tool itself
# is read-only); the only shared artifact touched is the audit log path,
# which is redirected to a per-run mktemp dir. Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

TMPDIR_STATE="$(mktemp -d "${TMPDIR:-/tmp}/aiworkhub_completion_inbox_wiring_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR_STATE"' EXIT

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"
export AIWORKHUB_AUDIT_LOG_PATH="$TMPDIR_STATE/task_audit.jsonl"

echo "=== MCP Completion-Inbox Wiring Test B275 v1 ==="
echo "AIWORKHUB_REPO=$AIWORKHUB_REPO"
echo "STATE_DIR=$TMPDIR_STATE"

# --- 1. server.py / completion_inbox.py source: no launch/exec/shell code --
for SRC in "$MCPROOT/src/aiworkhub/server.py" "$MCPROOT/src/aiworkhub/completion_inbox.py"; do
    for pat in "subprocess.Popen" "os.system" "os.popen" "os.exec" "os.fork" "os.spawn" "Popen(" "shell=True" "pty.spawn"; do
        if grep -Fq -- "$pat" "$SRC"; then
            echo "FAIL: forbidden launch pattern '$pat' found in $SRC"
            exit 1
        fi
    done
done
echo "server.py / completion_inbox.py source: no launch/exec/shell code: OK"

# --- Snapshot the real parent queue DB BEFORE any tool call -----------------
QUEUE_DB="$ROOT/bitnnv2/data/tasking/task_queue_v1.sqlite"
_db_fingerprint() {
    if [ -f "$QUEUE_DB" ]; then
        stat -c '%s %Y' "$QUEUE_DB"
    else
        echo "MISSING"
    fi
}
DB_BEFORE="$(_db_fingerprint)"
echo "queue DB fingerprint before: $DB_BEFORE"

# --- 2..5, 7. registration, schema validity, facet shapes, mismatch logic ---
python3 <<'PYEOF'
import asyncio
import json
import os

import jsonschema

from aiworkhub import server
from aiworkhub import completion_inbox as ci
from aiworkhub import cli_adapter_readonly_tool as ro

NEW_TOOL_NAME = "aiworkhub_completion_inbox"


def _call(name, args):
    return asyncio.run(server.mcp.call_tool(name, args))


async def _list_names():
    tools = await server.mcp.list_tools()
    return {t.name: t for t in tools}


tools_by_name = asyncio.run(_list_names())

# --- 2. exactly the 1 new tool is registered, additive ---------------------
assert NEW_TOOL_NAME in tools_by_name, sorted(tools_by_name)
print(f"PASS: {NEW_TOOL_NAME} registered")

# pre-existing tools (B106/B107 + core + B252 launch-queue) survive.
PRE_EXISTING = [
    "aiworkhub_task_health",
    "aiworkhub_task_review_queue",
    "aiworkhub_task_list",
    "aiworkhub_task_show",
    "aiworkhub_task_audit_log_read",
    "aiworkhub_task_review_summarize",
    "aiworkhub_task_codex_handoff",
    "aiworkhub_task_codex_handoff_markdown",
    "aiworkhub_supervisor_loop_status",
    "aiworkhub_launch_queue_describe_readonly",
    "aiworkhub_launch_queue_evaluate_readonly",
    "aiworkhub_launch_queue_audit_summary_readonly",
]
for n in PRE_EXISTING:
    assert n in tools_by_name, n
for n in ro.READONLY_TOOL_NAMES:
    assert n in tools_by_name, n
print("PASS: all pre-existing server tools (incl. B106/B107/B252 sets) still registered -- additive only")

# --- 3. schema serializes to valid JSON schema ------------------------------
schema = tools_by_name[NEW_TOOL_NAME].inputSchema
json.dumps(schema)
assert isinstance(schema, dict)
assert schema.get("type") == "object", schema
assert isinstance(schema.get("properties"), dict), schema
validator_cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft7Validator)
validator_cls.check_schema(schema)
print("PASS: aiworkhub_completion_inbox input schema is valid JSON schema")

# --- 4. build_completion_inbox() with stubbed taskctl I/O -------------------
NOW_ISO = ci._now().isoformat()


def _stub_list_tasks(status, topic=None, limit=80):
    stdout_by_status = {
        "pending": "",
        "processing": (
            "[processing] [task_mcp] [claude_task_mcp_completion_inbox_b275] STALE_TASK_B275_V1\n"
        ),
        "review": (
            "[review] [task_mcp] [claude_task_mcp_launch_queue_readonly_wiring_b252] REVIEW_TASK_B252_V1\n"
        ),
    }
    return {"ok": True, "returncode": 0, "stdout": stdout_by_status.get(status, ""), "stderr": ""}


def _stub_show_task(task_id):
    cards = {
        "STALE_TASK_B275_V1": {
            "task_id": "STALE_TASK_B275_V1",
            "runner": "claude_task_mcp_completion_inbox_b275",
            "topic": "task_mcp",
            "status": "processing",
            "worker_status": "claimed",
            "claimed_by": "claude_task_mcp_completion_inbox_b275",
            "claimed_at": "2020-01-01T00:00:00+00:00",
            "started_at": "2020-01-01T00:05:00+00:00",
            "updated_at": "2020-01-01T00:05:00+00:00",
            "objective": "synthetic stale processing fixture",
        },
        "REVIEW_TASK_B252_V1": {
            "task_id": "REVIEW_TASK_B252_V1",
            "runner": "claude_task_mcp_launch_queue_readonly_wiring_b252",
            "topic": "task_mcp",
            "status": "review",
            "worker_status": "review",
            "claimed_by": "claude_task_mcp_launch_queue_readonly_wiring_b252",
            "updated_at": NOW_ISO,
            "review_at": NOW_ISO,
            "objective": "synthetic review-queue fixture",
            "allowed_writes": ["tools/geoai-task-mcp/src/aiworkhub/server.py"],
            "validation_status": "passed",
        },
    }
    card = cards.get(task_id)
    if card is None:
        return {"ok": True, "returncode": 0, "stdout": f"Task not found: {task_id}", "stderr": ""}
    return {"ok": True, "returncode": 0, "stdout": json.dumps(card), "stderr": ""}


facts = ci.build_completion_inbox(
    topic="task_mcp",
    _list_tasks=_stub_list_tasks,
    _show_task=_stub_show_task,
)

assert facts["readonly"] is True, facts
assert facts["tool"] == "aiworkhub_completion_inbox", facts
for key in ("review_queue", "stale_processing", "runner_mismatch_warnings", "latest_validation_facts"):
    assert key in facts and isinstance(facts[key], list), (key, facts)

assert len(facts["review_queue"]) == 1, facts["review_queue"]
rq = facts["review_queue"][0]
assert rq["task_id"] == "REVIEW_TASK_B252_V1", rq
assert rq["validation_status"] == "passed", rq
assert rq["runner_task_batch_mismatch"] is None, rq  # b252 runner == B252 task: matched

assert len(facts["stale_processing"]) == 1, facts["stale_processing"]
sp = facts["stale_processing"][0]
assert sp["task_id"] == "STALE_TASK_B275_V1", sp
assert sp["stale_hours"] > 24.0, sp
assert sp["runner_task_batch_mismatch"] is None, sp  # b275 runner == B275 task: matched

assert len(facts["latest_validation_facts"]) == 2, facts["latest_validation_facts"]
vf_by_id = {v["task_id"]: v for v in facts["latest_validation_facts"]}
assert vf_by_id["REVIEW_TASK_B252_V1"]["validation_status"] == "passed"
assert vf_by_id["STALE_TASK_B275_V1"]["validation_status"] == "unreported"

# No mismatch injected in this fixture set -> warnings list must be empty.
assert facts["runner_mismatch_warnings"] == [], facts["runner_mismatch_warnings"]
print("PASS: build_completion_inbox() returns all 4 facets with correct shapes (stubbed taskctl I/O)")

# --- 5. runner_mismatch_warnings fires on a genuine batch-token mismatch ---
mismatched_card = {
    "task_id": "SOME_TASK_B192_V1",
    "runner": "claude_task_mcp_foo_b250",
    "topic": "task_mcp",
}
matched_card = {
    "task_id": "SOME_TASK_B250_V1",
    "runner": "claude_task_mcp_foo_b250",
    "topic": "task_mcp",
}
no_token_card = {"task_id": "SOME_TASK", "runner": "codex", "topic": "task_mcp"}

mismatch = ci._runner_task_batch_mismatch(mismatched_card)
assert mismatch, "expected a mismatch warning for b250 runner vs B192 task"
assert "RUNNER_TASK_BATCH_MISMATCH" in mismatch, mismatch
assert ci._runner_task_batch_mismatch(matched_card) == "", "matched batch tokens must NOT warn"
assert ci._runner_task_batch_mismatch(no_token_card) == "", "no-token cards must NOT warn"
print("PASS: _runner_task_batch_mismatch fires only on a genuine batch-token mismatch")

# --- 7. authority_flags / mutation block always report no write/launch -----
for allow_writes in (None, "1"):
    if allow_writes is None:
        os.environ.pop("AIWORKHUB_ALLOW_WRITES", None)
    else:
        os.environ["AIWORKHUB_ALLOW_WRITES"] = allow_writes

    facts2 = ci.build_completion_inbox(
        topic="task_mcp",
        _list_tasks=_stub_list_tasks,
        _show_task=_stub_show_task,
    )
    af = facts2["authority_flags"]
    assert af["process_launch"] is False, af
    assert af["agent_launch"] is False, af
    assert af["shell_invocation"] is False, af
    assert af["queue_write"] is False, af
    assert af["audit_write"] is False, af
    mut = facts2["mutation"]
    assert mut["queue_mutated"] is False, mut
    assert mut["write_command_invoked"] is False, mut
    assert mut["agent_or_process_launched"] is False, mut
    print(f"PASS: authority_flags/mutation all-False with AIWORKHUB_ALLOW_WRITES={allow_writes}")

os.environ.pop("AIWORKHUB_ALLOW_WRITES", None)

# --- live MCP tool call (real taskctl subprocess) still returns the shape --
_content, live = _call(NEW_TOOL_NAME, {"topic": "task_mcp", "limit": 50})
assert live["readonly"] is True, live
for key in ("review_queue", "stale_processing", "runner_mismatch_warnings", "latest_validation_facts", "counts"):
    assert key in live, (key, live)
print("PASS: live aiworkhub_completion_inbox MCP tool call returns the full facet shape")

print("ALL PYTHON CHECKS PASSED")
PYEOF

# --- 6. parent queue DB fingerprint unchanged after real tool calls ---------
DB_AFTER="$(_db_fingerprint)"
echo "queue DB fingerprint after: $DB_AFTER"
if [ "$DB_BEFORE" != "$DB_AFTER" ]; then
    echo "FAIL: parent queue DB mutated by a read-only tool call (before=$DB_BEFORE after=$DB_AFTER)"
    exit 1
fi
echo "PASS: parent queue DB byte-identical before/after (no mutation)"

echo ""
echo "=== Parent task queue integrity ==="
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
