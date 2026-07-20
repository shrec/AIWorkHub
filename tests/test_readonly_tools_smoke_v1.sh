#!/usr/bin/env bash
set -euo pipefail

# ── test_readonly_tools_smoke_v1.sh ──────────────────────────────────
# Smoke test for geoai-task-mcp read-only tools.
# Runs the Python test harness with ALLOW_WRITES=0.
# Must be run from repo root.
#
# Usage:
#   bash tools/geoai-task-mcp/tests/test_readonly_tools_smoke_v1.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES=0

echo "=== Read-Only Tool Smoke Test v1 ==="
echo "ROOT=$ROOT"
echo "PYTHONPATH=$PYTHONPATH"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo ""

# ── Run the Python test harness ─────────────────────────────────────
python3 "$MCPROOT/tests/readonly_tools_smoke.py"
RC=$?

echo ""
if [ $RC -eq 0 ]; then
    echo "=== test_readonly_tools_smoke_v1.sh: PASS ==="
else
    echo "=== test_readonly_tools_smoke_v1.sh: FAIL (exit=$RC) ==="
fi
exit $RC
