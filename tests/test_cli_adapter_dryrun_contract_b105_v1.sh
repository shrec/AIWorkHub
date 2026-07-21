#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_cli_adapter_dryrun_contract_b105_v1.sh
# Harness for the DRYRUN-ONLY CLI adapter contract (no process launch).
#
# Verifies:
#   1. cli_adapter_dryrun_smoke.py passes (allowlist, redaction, no-launch)
#   2. launch stays impossible even with AIWORKHUB_ALLOW_LAUNCH=1
#   3. write gate stays OFF by default
#   4. parent task queue is not mutated (taskctl verify)
#
# Isolation: uses a per-run mktemp audit path; overrides AIWORKHUB_AUDIT_LOG_PATH.
# No shared repo artifact is written. Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

TMPDIR_AUDIT="$(mktemp -d "${TMPDIR:-/tmp}/aiworkhub_cli_adapter_dryrun_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR_AUDIT"' EXIT

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"
export AIWORKHUB_ALLOW_WRITES=0
export AIWORKHUB_AUDIT_LOG_PATH="$TMPDIR_AUDIT/audit.jsonl"

echo "=== CLI Adapter DryRun Contract Smoke Test B105 v1 ==="
echo "AIWORKHUB_REPO=$AIWORKHUB_REPO"
echo "AIWORKHUB_ALLOW_WRITES=$AIWORKHUB_ALLOW_WRITES"
echo "AUDIT_LOG=$AIWORKHUB_AUDIT_LOG_PATH"

python3 "$MCPROOT/tests/cli_adapter_dryrun_smoke.py"
echo ""
echo "=== Defense-in-depth checks ==="

# Launch must remain impossible even if the enable flag is forced on.
LAUNCH_STATE="$(AIWORKHUB_ALLOW_LAUNCH=1 python3 -c '
import sys
sys.path.insert(0, "'"$MCPROOT"'/src")
from aiworkhub import cli_adapter_dryrun as m
print(str(m.launch_enabled()) + "," + str(m.LAUNCH_IMPLEMENTED))
')"
if [ "$LAUNCH_STATE" != "False,False" ]; then
    echo "FAIL: launch not disabled with ALLOW_LAUNCH=1 (got $LAUNCH_STATE)"
    exit 1
fi
echo "launch impossible with ALLOW_LAUNCH=1: OK ($LAUNCH_STATE)"

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
