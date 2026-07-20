#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_cli_adapter_readonly_tool_b106_v1.sh
# Harness for the READ-ONLY MCP tool contract over the CLI adapter dryrun.
#
# Verifies:
#   1. cli_adapter_readonly_tool_smoke.py passes (read-only, allowlist, redaction, no-launch)
#   2. launch stays impossible even with GEOAI_TASK_MCP_ALLOW_LAUNCH=1
#   3. the plan tool appends NO audit entry even with GEOAI_TASK_MCP_ALLOW_WRITES=1
#   4. write gate stays OFF by default
#   5. parent task queue is not mutated (taskctl verify)
#
# Isolation: uses a per-run mktemp audit path; overrides GEOAI_TASK_MCP_AUDIT_LOG_PATH.
# No shared repo artifact is written. Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

TMPDIR_AUDIT="$(mktemp -d "${TMPDIR:-/tmp}/geoai_cli_adapter_readonly_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR_AUDIT"' EXIT

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES=0
export GEOAI_TASK_MCP_AUDIT_LOG_PATH="$TMPDIR_AUDIT/audit.jsonl"

echo "=== CLI Adapter Read-Only Tool Contract Smoke Test B106 v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo "AUDIT_LOG=$GEOAI_TASK_MCP_AUDIT_LOG_PATH"

python3 "$MCPROOT/tests/cli_adapter_readonly_tool_smoke.py"
echo ""
echo "=== Defense-in-depth checks ==="

# Launch must remain impossible even if the enable flag is forced on.
LAUNCH_STATE="$(GEOAI_TASK_MCP_ALLOW_LAUNCH=1 python3 -c '
import sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from geoai_task_mcp import cli_adapter_readonly_tool as m
print(str(m.launch_enabled()) + "," + str(m.LAUNCH_IMPLEMENTED) + "," + str(m.READONLY))
')"
if [ "$LAUNCH_STATE" != "False,False,True" ]; then
    echo "FAIL: launch/readonly state wrong with ALLOW_LAUNCH=1 (got $LAUNCH_STATE)"
    exit 1
fi
echo "launch impossible + readonly=True with ALLOW_LAUNCH=1: OK ($LAUNCH_STATE)"

# Read-only proof: the plan tool must NOT append to the audit log even when the
# write gate is forced on. Use a dedicated temp audit path for this probe.
RO_AUDIT="$TMPDIR_AUDIT/ro_probe.jsonl"
RO_LINES="$(GEOAI_TASK_MCP_ALLOW_WRITES=1 GEOAI_TASK_MCP_AUDIT_LOG_PATH="$RO_AUDIT" python3 -c '
import os, sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from geoai_task_mcp import cli_adapter_readonly_tool as m
m.plan_command_readonly(task_id="P", runner="r", topic="task_mcp",
                        adapter_id="claude_cli", argv=["claude", "-p", "x"])
m.audit_summary_readonly()
p = "'"$RO_AUDIT"'"
print(0 if not os.path.exists(p) else sum(1 for _ in open(p)))
')"
if [ "$RO_LINES" != "0" ]; then
    echo "FAIL: read-only plan tool wrote $RO_LINES audit line(s) with ALLOW_WRITES=1 (expected 0)"
    exit 1
fi
echo "read-only: no audit written with ALLOW_WRITES=1: OK ($RO_LINES lines)"

# Write gate still off in the harness env.
ACTUAL="$(python3 -c 'import os; print(os.environ.get("GEOAI_TASK_MCP_ALLOW_WRITES","0"))')"
if [ "$ACTUAL" != "0" ]; then
    echo "FAIL: GEOAI_TASK_MCP_ALLOW_WRITES=$ACTUAL (expected 0)"
    exit 1
fi
echo "GEOAI_TASK_MCP_ALLOW_WRITES confirmed still 0 (off)"

# Parent queue intact.
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
