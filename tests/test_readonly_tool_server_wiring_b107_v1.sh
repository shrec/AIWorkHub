#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_readonly_tool_server_wiring_b107_v1.sh
# Verifies live @mcp.tool() wiring of the B106 read-only CLI adapter tools
# (cli_adapter_readonly_tool.register(mcp)) into the FastMCP server instance
# defined in server.py.
#
# Checks:
#   1. Exactly the 3 read-only tools are registered on server.mcp, additive
#      to the pre-existing queue tools (registration does not replace them).
#   2. Each read-only tool's input schema serializes to a valid JSON schema.
#   3. Each tool, invoked once with ALLOW_WRITES unset and once with
#      ALLOW_WRITES=1, writes NOTHING to a dedicated temp state dir.
#   4. server.py source contains no subprocess/exec/fork/shell launch code.
#   5. Parent task queue stays unmutated (taskctl verify).
#
# Isolation: all state written to a per-run mktemp dir; never touches a
# shared fixed path. Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

TMPDIR_STATE="$(mktemp -d "${TMPDIR:-/tmp}/geoai_readonly_wiring_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR_STATE"' EXIT

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_AUDIT_LOG_PATH="$TMPDIR_STATE/audit.jsonl"

echo "=== Readonly Tool Server Wiring Test B107 v1 ==="
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

# --- 1..3. registration, schema validity, no-write proof (python) ---------
python3 <<'PYEOF'
import asyncio
import json
import os
from pathlib import Path

import jsonschema

from geoai_task_mcp import server
from geoai_task_mcp import cli_adapter_readonly_tool as ro

AUDIT = Path(os.environ["GEOAI_TASK_MCP_AUDIT_LOG_PATH"])
STATE_DIR = AUDIT.parent


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

# --- 1. exactly the 3 read-only tools are registered -----------------------
ro_names = [n for n in tools_by_name if n in ro.READONLY_TOOL_NAMES]
assert len(ro_names) == 3, f"expected 3 readonly tools registered, got {ro_names}"
assert set(ro_names) == set(ro.READONLY_TOOL_NAMES), ro_names
print("PASS: exactly 3 read-only tools registered:", sorted(ro_names))

# server did not lose its own pre-existing tools (additive wiring, not a
# takeover of the FastMCP instance).
assert "geoai_task_health" in tools_by_name
assert "geoai_task_audit_log_read" in tools_by_name
print("PASS: pre-existing server tools still registered (additive wiring)")

# --- 2. schemas serialize to valid JSON schema -----------------------------
for name in ro.READONLY_TOOL_NAMES:
    schema = tools_by_name[name].inputSchema
    json.dumps(schema)  # must be JSON-serializable
    assert isinstance(schema, dict)
    assert schema.get("type") == "object", schema
    assert isinstance(schema.get("properties"), dict), schema
    validator_cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft7Validator)
    validator_cls.check_schema(schema)
print("PASS: all 3 tool input schemas are valid JSON schema")

# --- 3. no writes with ALLOW_WRITES unset AND with ALLOW_WRITES=1 ----------
calls = {
    "geoai_cli_adapter_plan_readonly": {
        "task_id": "B107-T1", "runner": "test_runner", "topic": "task_mcp",
        "adapter_id": "claude_cli", "argv": ["claude", "-p", "hi"],
    },
    "geoai_cli_adapter_audit_summary_readonly": {"max_entries": 10},
    "geoai_cli_adapter_report_readonly": {
        "task_id": "B107-T2", "runner": "test_runner", "topic": "task_mcp",
        "adapter_id": "claude_cli", "argv": ["codex", "exec", "review"],
    },
}


def _run_round(allow_writes):
    if allow_writes is None:
        os.environ.pop("GEOAI_TASK_MCP_ALLOW_WRITES", None)
        label = "<unset>"
    else:
        os.environ["GEOAI_TASK_MCP_ALLOW_WRITES"] = allow_writes
        label = allow_writes
    before = _snapshot()
    for name, args in calls.items():
        _content, structured = _call(name, args)
        assert structured is not None, name
        assert structured.get("readonly") is True, (name, structured)
    after = _snapshot()
    assert after == before, (
        f"state dir mutated with ALLOW_WRITES={label}: before={before} after={after}"
    )
    assert after == [], f"state dir not empty with ALLOW_WRITES={label}: {after}"
    print(f"PASS: no writes with GEOAI_TASK_MCP_ALLOW_WRITES={label} "
          f"(state snapshot: {after})")


_run_round(None)
_run_round("1")

print("ALL PYTHON CHECKS PASSED")
PYEOF

echo ""
echo "=== Parent task queue integrity ==="
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
