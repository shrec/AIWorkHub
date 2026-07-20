#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_launch_disabled_queue_contract_b117_v1.sh
# Harness for the disabled-by-default MCP launch-queue contract (no launch).
#
# Verifies:
#   1. launch_disabled_queue_contract_smoke.py passes (disabled default, refusal,
#      no process/network call, schemas round-trip, no secret leak);
#   2. launch stays disabled even with BOTH gates forced on (defense in depth);
#   3. parent task queue is not mutated (taskctl verify).
#
# Isolation: per-run mktemp working dir; the module is loaded by FILE PATH (the
# package __init__ is never imported), so a concurrent worker editing server.py
# cannot affect this test. No shared repo artifact is written. Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
MODULE="$MCPROOT/src/geoai_task_mcp/launch_queue_contract.py"

TMPWORK="$(mktemp -d "${TMPDIR:-/tmp}/geoai_launch_disabled_queue_b117.XXXXXX")"
trap 'rm -rf "$TMPWORK"' EXIT

# Ensure gates are unset for the primary run (do not inherit an enabled ambient).
unset GEOAI_TASK_MCP_ALLOW_LAUNCH || true
unset GEOAI_TASK_MCP_ALLOW_WRITES || true
export GEOAI_REPO="$ROOT"

echo "=== Launch-Disabled Queue Contract Smoke Test B117 v1 ==="
echo "GEOAI_REPO=$GEOAI_REPO"
echo "MODULE=$MODULE"
echo "TMPWORK=$TMPWORK"

# (1) Primary smoke (gates unset). Run from the temp dir for extra isolation.
( cd "$TMPWORK" && python3 "$MCPROOT/tests/launch_disabled_queue_contract_smoke.py" )

echo ""
echo "=== Defense-in-depth checks ==="

# (2) Launch must stay disabled even with BOTH enable flags forced on. Load the
#     module by file path so the package __init__ is never imported.
STATE="$(GEOAI_TASK_MCP_ALLOW_LAUNCH=1 GEOAI_TASK_MCP_ALLOW_WRITES=1 python3 - "$MODULE" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("lqc_b117_check", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m  # so @dataclass can resolve annotations
spec.loader.exec_module(m)
refused = False
try:
    m.attempt_start(m.enqueue_intent(
        task_id="T", runner="r", topic="task_mcp", adapter_id="claude_cli"))
except m.LaunchDisabledError:
    refused = True
print(f"{m.launch_enabled()},{m.env_gates_open()},{refused},{m.LAUNCH_IMPLEMENTED}")
PY
)"
if [ "$STATE" != "False,True,True,False" ]; then
    echo "FAIL: launch not disabled with BOTH gates on (got $STATE, want False,True,True,False)"
    exit 1
fi
echo "launch disabled + refused with BOTH gates on: OK ($STATE)"

# (3) Parent queue intact.
python3 "$ROOT/AITools/taskctl.py" verify
echo "taskctl verify: PASS (parent queue intact)"

echo ""
echo "ALL CHECKS PASSED"
exit 0
