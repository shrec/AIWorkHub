#!/usr/bin/env bash
set -euo pipefail

# ── test_mcp_review_summarizer_server_wiring_b111_v1.sh ─────────────
# Smoke + contract test for B111 server wiring of review_summarizer.
# Runs the Python test harness with ALLOW_WRITES=0.
# Must be run from repo root.
#
# Usage:
#   bash tools/geoai-task-mcp/tests/test_mcp_review_summarizer_server_wiring_b111_v1.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES=0

echo "=== B111 Review Summarizer Server Wiring Test ==="
echo "ROOT=$ROOT"
echo "PYTHONPATH=$PYTHONPATH"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo ""

# ── Run the Python smoke test ───────────────────────────────────────
python3 "$MCPROOT/tests/mcp_review_summarizer_server_wiring_smoke.py"
RC=$?

echo ""
if [ $RC -eq 0 ]; then
    echo "=== test_mcp_review_summarizer_server_wiring_b111_v1.sh: PASS ==="
else
    echo "=== test_mcp_review_summarizer_server_wiring_b111_v1.sh: FAIL (exit=$RC) ==="
fi
exit $RC
