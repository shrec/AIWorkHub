#!/usr/bin/env bash
set -euo pipefail

# ── test_mcp_codex_handoff_e2e_b116_v1.sh ───────────────────────────────
# E2E test for the B116 Codex-ready MCP handoff report tool.
# Runs the Python harness that:
#   - builds a TEMP fixture review queue (per-test mktemp dir)
#   - invokes review_summarizer.build_codex_handoff_report + the registered
#     server tool geoai_task_codex_handoff
#   - asserts all handoff fields present (task_id, allowed_writes, validation,
#     risks, commit_hygiene, recommended stage list)
#   - asserts ZERO mutation of the queue (fixture dir hash unchanged +
#     no write taskctl command issued; live review-queue stdout unchanged)
#   - asserts write gate default-off, empty + fixture-queue cases
#
# Isolation-safe / parallel-safe: the Python harness uses mktemp dirs; this
# wrapper writes no fixed shared path and performs no real parent-queue write.
#
# Usage:
#   GEOAI_TASK_MCP_ALLOW_WRITES=0 bash \
#     tools/geoai-task-mcp/tests/test_mcp_codex_handoff_e2e_b116_v1.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES="${GEOAI_TASK_MCP_ALLOW_WRITES:-0}"

echo "=== B116 MCP Codex Handoff E2E Test ==="
echo "ROOT=$ROOT"
echo "PYTHONPATH=$PYTHONPATH"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo ""

# ── Validate ALLOW_WRITES is off (write gate must stay default-off) ─────
if [ "$GEOAI_TASK_MCP_ALLOW_WRITES" != "0" ]; then
    echo "FATAL: GEOAI_TASK_MCP_ALLOW_WRITES must be 0, got '$GEOAI_TASK_MCP_ALLOW_WRITES'"
    exit 2
fi

# ── Run the Python e2e harness ──────────────────────────────────────────
python3 "$MCPROOT/tests/mcp_codex_handoff_e2e.py"
RC=$?

echo ""
if [ $RC -eq 0 ]; then
    echo "=== test_mcp_codex_handoff_e2e_b116_v1.sh: PASS ==="
else
    echo "=== test_mcp_codex_handoff_e2e_b116_v1.sh: FAIL (exit=$RC) ==="
fi
exit $RC
