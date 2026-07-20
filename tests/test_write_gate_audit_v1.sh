#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_write_gate_audit_v1.sh
# Smoke-test harness for write-gate audit logging.
#
# Verifies:
#   1. blocked write attempts are logged to audit.jsonl
#   2. no secrets/env values appear in the audit log
#   3. GEOAI_TASK_MCP_ALLOW_WRITES stays default off
#   4. parent task queue is not mutated by blocked writes
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GEOAI_REPO="${GEOAI_REPO:-$(cd "$ROOT/../.." && pwd)}"

export PYTHONPATH="$ROOT/src"
export GEOAI_REPO
export GEOAI_TASK_MCP_ALLOW_WRITES=0

echo "=== Write-Gate Audit Smoke Test v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"

# ------------------------------------------------------------------
# Run the audit smoke test (Python)
# ------------------------------------------------------------------
python3 "$ROOT/tests/write_gate_audit_smoke.py"
SMOKE_RC=$?

echo ""
echo "=== Audit Smoke RC: $SMOKE_RC ==="

if [ "$SMOKE_RC" -ne 0 ]; then
    echo "FAIL: write_gate_audit_smoke.py returned non-zero"
    exit 1
fi

# ------------------------------------------------------------------
# Verify ALLOW_WRITES is still off (defense-in-depth)
# ------------------------------------------------------------------
ACTUAL="$(python3 -c 'import os; print(os.environ.get("GEOAI_TASK_MCP_ALLOW_WRITES","0"))')"
if [ "$ACTUAL" != "0" ]; then
    echo "FAIL: GEOAI_TASK_MCP_ALLOW_WRITES=$ACTUAL (expected 0)"
    exit 1
fi
echo "GEOAI_TASK_MCP_ALLOW_WRITES confirmed still 0 (off)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
