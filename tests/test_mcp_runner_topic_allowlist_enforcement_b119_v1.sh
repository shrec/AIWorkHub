#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_runner_topic_allowlist_enforcement_b119_v1.sh
# Harness for the B119 runner/topic allowlist enforcement patch.
#
# Verifies:
#   1. runner_topic_allowlist_enforcement_smoke.py passes (allowlist matrix,
#      8 malformed fixtures, layer-order priority, audit sanitization,
#      isolated-queue allow/deny proceed correctly, legacy no-identity call
#      sites unaffected).
#   2. the write gate itself is untouched: still off by default, still the
#      FIRST check (core.WRITE_COMMANDS unchanged; writes_allowed() honors
#      GEOAI_TASK_MCP_ALLOW_WRITES exactly as before).
#   3. no launch capability was added (launch_queue_contract invariants
#      unchanged: LAUNCH_IMPLEMENTED is still the constant False).
#   4. the REAL parent task queue is not mutated (taskctl verify).
#
# Isolation: the python smoke test operates on per-run mktemp copies of the
# queue and a per-run mktemp audit log path; this harness adds its own
# per-run mktemp audit path for the defense-in-depth checks below. No shared
# repo artifact is written. Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

TMPDIR_AUDIT="$(mktemp -d "${TMPDIR:-/tmp}/geoai_b119_allowlist_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR_AUDIT"' EXIT

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES=0
export GEOAI_TASK_MCP_AUDIT_LOG_PATH="$TMPDIR_AUDIT/audit.jsonl"

echo "=== MCP Runner/Topic Allowlist Enforcement Smoke Test B119 v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo "AUDIT_LOG=$GEOAI_TASK_MCP_AUDIT_LOG_PATH"

python3 "$MCPROOT/tests/runner_topic_allowlist_enforcement_smoke.py"
echo ""
echo "=== Defense-in-depth checks ==="

# The allowlist layer is additive: WRITE_COMMANDS is unchanged, the write gate
# is still checked (and still off by default), the new pure decision function
# exists, and no server.py / launch_queue_contract.py invariant moved.
GATE_STATE="$(python3 -c '
import sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from geoai_task_mcp import core, launch_queue_contract as lqc
write_commands_ok = core.WRITE_COMMANDS == {"auto-pickup", "done", "export-jsonl", "review", "start", "usage"}
has_allowlist_fn = hasattr(core, "check_runner_topic_allowlist")
has_matrix = hasattr(core, "RUNNER_TOPIC_ALLOWLIST") and len(core.RUNNER_TOPIC_ALLOWLIST) == 11
has_codex = hasattr(core, "CODEX_RUNNER") and core.CODEX_RUNNER == "codex"
gate_off = core.writes_allowed() is False
launch_still_disabled = lqc.LAUNCH_IMPLEMENTED is False and lqc.launch_enabled() is False
print(",".join(str(x) for x in (write_commands_ok, has_allowlist_fn, has_matrix, has_codex, gate_off, launch_still_disabled)))
')"
if [ "$GATE_STATE" != "True,True,True,True,True,True" ]; then
    echo "FAIL: allowlist wiring/invariants wrong (got $GATE_STATE)"
    exit 1
fi
echo "allowlist matrix present + write gate untouched + launch still disabled: OK ($GATE_STATE)"

# Read-only tools stay unaffected: none of them are write commands.
READONLY_OK="$(python3 -c '
import sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from geoai_task_mcp import core
ro = ["list", "show", "export", "verify", "review-queue", "collision-guard", "usage-report"]
print(all(not core._is_write_command([c]) for c in ro))
')"
if [ "$READONLY_OK" != "True" ]; then
    echo "FAIL: a read-only command was classified as a write command"
    exit 1
fi
echo "read-only tools confirmed unaffected: OK"

# Write gate still off.
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
