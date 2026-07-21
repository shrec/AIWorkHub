#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_autopickup_dryrun_b118_v1.sh
# Harness for the READ-ONLY auto-pickup DRY-RUN preview (B118).
#
# Verifies:
#   1. mcp_autopickup_dryrun_smoke.py passes (candidate reported; queue
#      byte+logical identical before/after; runner/topic filtering visible;
#      real auto_pickup stays write-gated).
#   2. the new aiworkhub_task_auto_pickup_dryrun tool is registered on the server.
#   3. dry-run does NOT bypass the write gate (works with ALLOW_WRITES=0) and
#      the real auto_pickup command stays in WRITE_COMMANDS (write-gated).
#   4. write gate stays OFF by default.
#   5. the REAL parent task queue is not mutated (taskctl verify).
#
# Isolation: the smoke test operates on a per-run mktemp COPY of the queue via
# BITNN_TASK_QUEUE_DB/_CARDS_PATH/_CARDS_MANIFEST overrides; this harness adds a
# per-run mktemp audit path. No shared repo artifact is written. Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

TMPDIR_AUDIT="$(mktemp -d "${TMPDIR:-/tmp}/aiworkhub_autopickup_dryrun_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR_AUDIT"' EXIT

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"
export AIWORKHUB_ALLOW_WRITES=0
export AIWORKHUB_AUDIT_LOG_PATH="$TMPDIR_AUDIT/audit.jsonl"

echo "=== MCP Auto-Pickup DryRun Preview Smoke Test B118 v1 ==="
echo "AIWORKHUB_REPO=$AIWORKHUB_REPO"
echo "AIWORKHUB_ALLOW_WRITES=$AIWORKHUB_ALLOW_WRITES"
echo "AUDIT_LOG=$AIWORKHUB_AUDIT_LOG_PATH"

python3 "$MCPROOT/tests/mcp_autopickup_dryrun_smoke.py"
echo ""
echo "=== Defense-in-depth checks ==="

# The new dry-run tool is registered read-only; the real claim path stays
# write-gated (auto-pickup remains in WRITE_COMMANDS); dry-run is NOT a write
# command and cannot flip the write gate.
GATE_STATE="$(python3 -c '
import sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from aiworkhub import core, server
has_core = hasattr(core, "auto_pickup_dryrun")
has_tool = hasattr(server, "aiworkhub_task_auto_pickup_dryrun")
real_gated = "auto-pickup" in core.WRITE_COMMANDS
dryrun_not_write = "auto-pickup-dryrun" not in core.WRITE_COMMANDS
print(",".join(str(x) for x in (has_core, has_tool, real_gated, dryrun_not_write, core.writes_allowed())))
')"
if [ "$GATE_STATE" != "True,True,True,True,False" ]; then
    echo "FAIL: dryrun/gate wiring wrong (got $GATE_STATE)"
    exit 1
fi
echo "dryrun tool wired + real auto-pickup write-gated + gate off: OK ($GATE_STATE)"

# Write gate still off.
ACTUAL="$(python3 -c 'import os; print(os.environ.get("AIWORKHUB_ALLOW_WRITES","0"))')"
if [ "$ACTUAL" != "0" ]; then
    echo "FAIL: AIWORKHUB_ALLOW_WRITES=$ACTUAL (expected 0)"
    exit 1
fi
echo "AIWORKHUB_ALLOW_WRITES confirmed still 0 (off)"

# Parent queue intact.
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
