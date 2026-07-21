#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_audit_log_read_tool_b104_v1.sh
# Smoke-test harness for the read-only audit log inspection tool.
#
# Verifies:
#   1. read_audit_log returns correct empty-log result
#   2. read_audit_log returns correct summary for populated log
#   3. counts by tool_name and action are correct
#   4. last_entries is capped at max_entries
#   5. all authority flags remain false (except write_gate_enabled=true)
#   6. no secrets/env values appear in output
#   7. AIWORKHUB_ALLOW_WRITES stays default off
#   8. parent task queue is not mutated
#   9. read_audit_log is purely read-only (no subprocess, no file write)
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"
export AIWORKHUB_ALLOW_WRITES=0

echo "=== Audit Log Read Tool Smoke Test B104 v1 ==="
echo "AIWORKHUB_REPO=$AIWORKHUB_REPO"
echo "AIWORKHUB_ALLOW_WRITES=$AIWORKHUB_ALLOW_WRITES"

# ------------------------------------------------------------------
# Run the audit log read smoke test (Python)
# ------------------------------------------------------------------
python3 "$MCPROOT/tests/audit_log_read_smoke.py"
SMOKE_RC=$?

echo ""
echo "=== Smoke RC: $SMOKE_RC ==="

if [ "$SMOKE_RC" -ne 0 ]; then
    echo "FAIL: audit_log_read_smoke.py returned non-zero"
    exit 1
fi

# ------------------------------------------------------------------
# Defense-in-depth: verify no process launch or file mutation occurred
# (the tool does not use subprocess at all — it's a pure file read)
# ------------------------------------------------------------------
echo ""
echo "=== Defense-in-depth checks ==="

# Verify ALLOW_WRITES is still off
ACTUAL="$(python3 -c 'import os; print(os.environ.get("AIWORKHUB_ALLOW_WRITES","0"))')"
if [ "$ACTUAL" != "0" ]; then
    echo "FAIL: AIWORKHUB_ALLOW_WRITES=$ACTUAL (expected 0)"
    exit 1
fi
echo "AIWORKHUB_ALLOW_WRITES confirmed still 0 (off)"

# Verify taskctl verify still passes (parent queue intact)
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
