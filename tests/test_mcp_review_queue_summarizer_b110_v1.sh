#!/usr/bin/env bash
set -euo pipefail

# ── test_mcp_review_queue_summarizer_b110_v1.sh ─────────────────────
# Smoke + contract test for B110 review queue summarizer.
# Runs the Python test harness with ALLOW_WRITES=0.
# Must be run from repo root.
#
# Usage:
#   bash tools/geoai-task-mcp/tests/test_mcp_review_queue_summarizer_b110_v1.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES=0

echo "=== B110 Review Queue Summarizer Test ==="
echo "ROOT=$ROOT"
echo "PYTHONPATH=$PYTHONPATH"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo ""

# ── Run the Python smoke test ───────────────────────────────────────
python3 "$MCPROOT/tests/mcp_review_queue_summarizer_smoke.py"
RC=$?

echo ""
if [ $RC -eq 0 ]; then
    echo "=== test_mcp_review_queue_summarizer_b110_v1.sh: PASS ==="
else
    echo "=== test_mcp_review_queue_summarizer_b110_v1.sh: FAIL (exit=$RC) ==="
fi
exit $RC
