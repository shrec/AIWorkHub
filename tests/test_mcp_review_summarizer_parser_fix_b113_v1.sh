#!/usr/bin/env bash
set -euo pipefail

# ── test_mcp_review_summarizer_parser_fix_b113_v1.sh ─────────────────
# B113 parser fix validation: suffix-tolerant regex + fetch_errors=[].
# Runs the real-queue integration test which now asserts that:
#   - Real-queue lines with ' — description' suffix parse into task entries
#   - Empty-queue result includes fetch_errors=[]
#   - Live queue sha256 before/after remains byte-identical
#   - No queue mutation or agent launch
#
# Usage:
#   GEOAI_TASK_MCP_ALLOW_WRITES=0 bash \
#     tools/geoai-task-mcp/tests/test_mcp_review_summarizer_parser_fix_b113_v1.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES="${GEOAI_TASK_MCP_ALLOW_WRITES:-0}"

echo "=== B113 Review Summarizer Parser Fix Test ==="
echo "ROOT=$ROOT"
echo "PYTHONPATH=$PYTHONPATH"
echo "GEOAI_TASK_MCP_ALLOW_WRITES=$GEOAI_TASK_MCP_ALLOW_WRITES"
echo ""

# ── Validate ALLOW_WRITES is off ────────────────────────────────────
if [ "$GEOAI_TASK_MCP_ALLOW_WRITES" != "0" ]; then
    echo "FATAL: GEOAI_TASK_MCP_ALLOW_WRITES must be 0, got '$GEOAI_TASK_MCP_ALLOW_WRITES'"
    exit 2
fi

# ── Quick regex unit test ───────────────────────────────────────────
echo "--- Regex unit test ---"
python3 -c "
import re
pat = re.compile(r'^\s*\[(?P<topic>[^\]]+)\]\s*\[(?P<runner>[^\]]+)\]\s*(?P<task_id>\S+)(?:\s+.*)?$')

# Test 1: clean line (no suffix)
m1 = pat.match('  [coding] [deepseek_coding] DEEPSEEK_TASK_X_V1')
assert m1, 'clean line should match'
assert m1.group('task_id') == 'DEEPSEEK_TASK_X_V1', f'got {m1.group(\"task_id\")}'

# Test 2: line with em-dash description suffix
m2 = pat.match('  [task_mcp] [claude_worker] CLAUDE_TASK_Y_V1 — description here')
assert m2, 'suffix line should match'
assert m2.group('task_id') == 'CLAUDE_TASK_Y_V1', f'got {m2.group(\"task_id\")}'
assert m2.group('topic') == 'task_mcp'
assert m2.group('runner') == 'claude_worker'

# Test 3: empty line should not match
m3 = pat.match('')
assert m3 is None, 'empty line should not match'

# Test 4: header line should not match
m4 = pat.match('=== Codex Review Queue (3) ===')
assert m4 is None, 'header line should not match'

print('  PASS regex_suffix_tolerant: 4/4')
"
RC_REGEX=$?
if [ $RC_REGEX -ne 0 ]; then
    echo "FAIL: regex unit test failed"
    exit 1
fi

# ── Quick fetch_errors unit test ────────────────────────────────────
echo "--- fetch_errors unit test ---"
python3 -c "
import sys, os
sys.path.insert(0, os.path.join('$MCPROOT', 'src'))
from geoai_task_mcp import review_summarizer

# Stub empty queue
def stub_empty_run(args, **kw):
    class R:
        stdout = '=== Codex Review Queue (0) ===\n'
    return R()

def stub_empty_show(task_id):
    return {'returncode': 1, 'stdout': '{}', 'stderr': 'not found'}

result = review_summarizer.summarize_review_queue(
    _run_taskctl=stub_empty_run, _show_task=stub_empty_show)
assert 'fetch_errors' in result, 'fetch_errors missing from empty result'
assert result['fetch_errors'] == [], f'expected [], got {result[\"fetch_errors\"]}'
assert result['task_count'] == 0
print('  PASS fetch_errors_empty: fetch_errors=[] present in empty result')
"
RC_FE=$?
if [ $RC_FE -ne 0 ]; then
    echo "FAIL: fetch_errors unit test failed"
    exit 1
fi

# ── Eval JSON validity ──────────────────────────────────────────────
echo "--- Eval JSON validity ---"
python3 -m json.tool "$MCPROOT/eval/mcp_review_summarizer_parser_fix_b113_v1.json" >/dev/null
echo "  PASS eval_json_valid"

# ── Run the full real-queue integration test ────────────────────────
echo ""
echo "--- Full real-queue integration test ---"
python3 "$MCPROOT/tests/mcp_review_summarizer_real_queue_integration.py"
RC=$?

echo ""
if [ $RC -eq 0 ]; then
    echo "=== test_mcp_review_summarizer_parser_fix_b113_v1.sh: PASS ==="
else
    echo "=== test_mcp_review_summarizer_parser_fix_b113_v1.sh: FAIL (exit=$RC) ==="
fi
exit $RC
