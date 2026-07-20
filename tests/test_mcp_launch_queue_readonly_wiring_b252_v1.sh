#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_launch_queue_readonly_wiring_b252_v1.sh
# Verifies live @mcp.tool() wiring of the B252 read-only launch-queue tools
# (geoai_launch_queue_describe_readonly / _evaluate_readonly /
# _audit_summary_readonly) into the FastMCP server instance in server.py.
# These tools wrap the disabled B119 launch_queue_contract.py /
# launch_queue_persist.py modules WITHOUT modifying either file -- server.py
# imports and calls their existing pure functions directly.
#
# Checks:
#   1. Exactly the 3 new tools are registered on server.mcp, additive to the
#      pre-existing tools (including the B106/B107 read-only CLI adapter set).
#   2. Each tool's input schema serializes to a valid JSON schema.
#   3. describe/evaluate always report launch_enabled=False,
#      launch_implemented=False, permitted=False,
#      decision=blocked_launch_disabled -- with GEOAI_TASK_MCP_ALLOW_LAUNCH
#      unset AND forced to 1.
#   4. None of the 3 tools write anything (state dir + launch-queue log file
#      untouched), even with ALLOW_WRITES=1 and ALLOW_LAUNCH=1 forced together.
#   5. audit_summary_readonly surfaces a pre-seeded log entry (written by the
#      test harness directly via launch_queue_persist.persist_intent, never
#      via the MCP tool) without appending a second line.
#   6. server.py source contains no subprocess/exec/fork/shell launch code.
#   7. Parent task queue stays unmutated (taskctl verify).
#
# Isolation: all state written to a per-run mktemp dir; never touches a shared
# fixed path. Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

TMPDIR_STATE="$(mktemp -d "${TMPDIR:-/tmp}/geoai_launch_queue_ro_wiring_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR_STATE"' EXIT

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_AUDIT_LOG_PATH="$TMPDIR_STATE/task_audit.jsonl"
export GEOAI_TASK_MCP_LAUNCH_QUEUE_LOG_PATH="$TMPDIR_STATE/launch_queue_audit.jsonl"

echo "=== MCP Launch-Queue Read-Only Wiring Test B252 v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "STATE_DIR=$TMPDIR_STATE"

# --- 0. server.py source contains no process-launch code ------------------
SERVER_SRC="$MCPROOT/src/geoai_task_mcp/server.py"
for pat in "subprocess" "os.system" "os.popen" "os.exec" "os.fork" "os.spawn" "Popen(" "shell=True" "pty.spawn"; do
    if grep -Fq -- "$pat" "$SERVER_SRC"; then
        echo "FAIL: forbidden launch pattern '$pat' found in server.py"
        exit 1
    fi
done
echo "server.py source: no launch/exec/shell code: OK"

# --- 1..5. registration, schema validity, gate invariants, no-write proof --
python3 <<'PYEOF'
import asyncio
import json
import os
from pathlib import Path

import jsonschema

from geoai_task_mcp import server
from geoai_task_mcp import launch_queue_contract as lqc
from geoai_task_mcp import launch_queue_persist as lqp
from geoai_task_mcp import cli_adapter_readonly_tool as ro

STATE_DIR = Path(os.environ["GEOAI_TASK_MCP_AUDIT_LOG_PATH"]).parent
LAUNCH_LOG = Path(os.environ["GEOAI_TASK_MCP_LAUNCH_QUEUE_LOG_PATH"])

NEW_TOOL_NAMES = (
    "geoai_launch_queue_describe_readonly",
    "geoai_launch_queue_evaluate_readonly",
    "geoai_launch_queue_audit_summary_readonly",
)


def _snapshot():
    if not STATE_DIR.exists():
        return []
    return sorted(
        (str(p.relative_to(STATE_DIR)), p.stat().st_size)
        for p in STATE_DIR.rglob("*")
        if p.is_file()
    )


def _call(name, args):
    return asyncio.run(server.mcp.call_tool(name, args))


async def _list_names():
    tools = await server.mcp.list_tools()
    return {t.name: t for t in tools}


tools_by_name = asyncio.run(_list_names())

# --- 1. exactly the 3 new tools are registered, additive -------------------
found = [n for n in tools_by_name if n in NEW_TOOL_NAMES]
assert len(found) == 3, f"expected 3 new launch-queue readonly tools, got {found}"
assert set(found) == set(NEW_TOOL_NAMES), found
print("PASS: exactly 3 new launch-queue read-only tools registered:", sorted(found))

# pre-existing tools (B106/B107 + core) survive -- additive wiring only.
assert "geoai_task_health" in tools_by_name
assert "geoai_task_audit_log_read" in tools_by_name
for n in ro.READONLY_TOOL_NAMES:
    assert n in tools_by_name, n
print("PASS: pre-existing server tools (incl. B106/B107 readonly set) still registered")

# --- 2. schemas serialize to valid JSON schema -----------------------------
for name in NEW_TOOL_NAMES:
    schema = tools_by_name[name].inputSchema
    json.dumps(schema)  # must be JSON-serializable
    assert isinstance(schema, dict)
    assert schema.get("type") == "object", schema
    assert isinstance(schema.get("properties"), dict), schema
    validator_cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft7Validator)
    validator_cls.check_schema(schema)
print("PASS: all 3 new tool input schemas are valid JSON schema")

# --- 3. gate invariants hold with ALLOW_LAUNCH unset AND forced to 1 -------
describe_args = {
    "task_id": "B252-T1", "runner": "test_runner", "topic": "task_mcp",
    "adapter_id": "claude_cli", "argv_template": ["claude", "-p", "hi"],
}
evaluate_args = dict(describe_args)


def _check_gate_invariants(allow_launch):
    if allow_launch is None:
        os.environ.pop("GEOAI_TASK_MCP_ALLOW_LAUNCH", None)
        label = "<unset>"
    else:
        os.environ["GEOAI_TASK_MCP_ALLOW_LAUNCH"] = allow_launch
        label = allow_launch

    _content, described = _call("geoai_launch_queue_describe_readonly", describe_args)
    assert described["decision"]["permitted"] is False, described
    assert described["decision"]["launch_enabled"] is False, described
    assert described["decision"]["launch_implemented"] is False, described
    assert described["decision"]["decision"] == "blocked_launch_disabled", described
    assert described["readonly"] is True, described
    assert described["authority_flags"]["process_launch"] is False, described
    assert described["authority_flags"]["process_launch_authority"] is False, described

    _content, evaluated = _call("geoai_launch_queue_evaluate_readonly", evaluate_args)
    assert evaluated["permitted"] is False, evaluated
    assert evaluated["launch_enabled"] is False, evaluated
    assert evaluated["launch_implemented"] is False, evaluated
    assert evaluated["decision"] == "blocked_launch_disabled", evaluated
    assert evaluated["readonly"] is True, evaluated

    assert lqc.launch_enabled() is False
    assert lqc.LAUNCH_IMPLEMENTED is False
    print(f"PASS: gate invariants hold with GEOAI_TASK_MCP_ALLOW_LAUNCH={label}")


_check_gate_invariants(None)
_check_gate_invariants("1")
os.environ.pop("GEOAI_TASK_MCP_ALLOW_LAUNCH", None)

# --- 4. no writes with ALLOW_WRITES/ALLOW_LAUNCH unset AND forced to 1 -----
def _run_round(allow_writes, allow_launch):
    for name, val in (
        ("GEOAI_TASK_MCP_ALLOW_WRITES", allow_writes),
        ("GEOAI_TASK_MCP_ALLOW_LAUNCH", allow_launch),
    ):
        if val is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = val

    before = _snapshot()
    launch_log_existed_before = LAUNCH_LOG.exists()

    for name, args in (
        ("geoai_launch_queue_describe_readonly", describe_args),
        ("geoai_launch_queue_evaluate_readonly", evaluate_args),
        ("geoai_launch_queue_audit_summary_readonly", {"max_entries": 10}),
    ):
        _content, structured = _call(name, args)
        assert structured is not None, name
        assert structured.get("readonly") is True, (name, structured)

    after = _snapshot()
    assert after == before, f"state dir mutated: before={before} after={after}"
    assert LAUNCH_LOG.exists() == launch_log_existed_before, (
        "launch-queue audit log existence changed after a read-only tool call"
    )
    print(
        f"PASS: no writes with ALLOW_WRITES={allow_writes} ALLOW_LAUNCH={allow_launch} "
        f"(state snapshot: {after}, launch_log_exists={LAUNCH_LOG.exists()})"
    )


_run_round(None, None)
_run_round("1", "1")
os.environ.pop("GEOAI_TASK_MCP_ALLOW_WRITES", None)
os.environ.pop("GEOAI_TASK_MCP_ALLOW_LAUNCH", None)

# --- 5. audit_summary_readonly reads a real persisted log without writing --
# Seed exactly one entry via the harness calling launch_queue_persist directly
# (the ONLY write path in that module) -- never via the MCP tool.
lqp.persist_intent(
    task_id="B252-SEED", runner="seed_runner", topic="task_mcp",
    adapter_id="claude_cli", ts=1.0,
)
lines_after_seed = sum(1 for _ in open(LAUNCH_LOG, encoding="utf-8"))
assert lines_after_seed == 1, lines_after_seed

_content, summary = _call("geoai_launch_queue_audit_summary_readonly", {"max_entries": 10})
assert summary["total_entries"] == 1, summary
assert summary["all_blocked_launch_disabled"] is True, summary
assert summary["readonly"] is True, summary

lines_after_read = sum(1 for _ in open(LAUNCH_LOG, encoding="utf-8"))
assert lines_after_read == 1, (
    f"audit_summary_readonly tool call appended to the log (now {lines_after_read} lines)"
)
print("PASS: audit_summary_readonly surfaces a seeded entry without appending")

print("ALL PYTHON CHECKS PASSED")
PYEOF

echo ""
echo "=== Parent task queue integrity ==="
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
