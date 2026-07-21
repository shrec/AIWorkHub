#!/usr/bin/env bash
set -euo pipefail

# ── test_mcp_review_summarizer_real_queue_b112_v1.sh ─────────────────
# Real-queue integration test for B112 review_summarizer MCP tool.
# Runs the Python harness that calls the summarizer WITHOUT stub injection,
# verifying:
#   - Real MCP tool path (no stubs on run_taskctl/show_task)
#   - Live task queue unchanged before/after
#   - Empty queue and non-empty fixture queue cases
#   - Write gate default-off
#   - process_launch_authority=false
#
# Usage:
#   AIWORKHUB_ALLOW_WRITES=0 bash \
#     tools/geoai-task-mcp/tests/test_mcp_review_summarizer_real_queue_b112_v1.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

export PYTHONPATH="$MCPROOT/src"
export AIWORKHUB_REPO="$ROOT"
export AIWORKHUB_ALLOW_WRITES="${AIWORKHUB_ALLOW_WRITES:-0}"

echo "=== B112 Review Summarizer Real Queue Integration Test ==="
echo "ROOT=$ROOT"
echo "PYTHONPATH=$PYTHONPATH"
echo "AIWORKHUB_ALLOW_WRITES=$AIWORKHUB_ALLOW_WRITES"
echo ""

# ── Validate ALLOW_WRITES is off ────────────────────────────────────
if [ "$AIWORKHUB_ALLOW_WRITES" != "0" ]; then
    echo "FATAL: AIWORKHUB_ALLOW_WRITES must be 0, got '$AIWORKHUB_ALLOW_WRITES'"
    exit 2
fi

# ── Run the Python integration test ─────────────────────────────────
python3 "$MCPROOT/tests/mcp_review_summarizer_real_queue_integration.py"
RC=$?

echo ""
if [ $RC -eq 0 ]; then
    echo "=== test_mcp_review_summarizer_real_queue_b112_v1.sh: PASS ==="
else
    echo "=== test_mcp_review_summarizer_real_queue_b112_v1.sh: FAIL (exit=$RC) ==="
fi
exit $RC
