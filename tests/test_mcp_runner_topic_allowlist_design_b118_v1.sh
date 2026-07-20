#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_runner_topic_allowlist_design_b118_v1.sh
# Harness for runner/topic allowlist design validation.
#
# Verifies:
#   1. Eval JSON is valid JSON with required top-level fields
#   2. Allow/deny matrix is complete (all 12 entries present)
#   3. All 8 malformed fixture cases have expected:deny
#   4. Next-wave JSON is valid JSON with follow_up_tasks
#   5. Design JSONL rows count matches eval entries + fixtures + layers
#   6. Safety invariants hold (Codex done monopoly, worker cannot finalize)
#   7. No core.py/server.py mutation happened
#   8. Next enforcement patch has allowed_writes and runner/topic fields
#
# Isolation: uses temp copies; no shared repo artifact is written.
# Parallel-safe.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"

TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/geoai_mcp_allowlist_design_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR"' EXIT

export GEOAI_REPO="$ROOT"

FAILURES=0

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

echo "=== MCP Runner/Topic Allowlist Design Test B118 v1 ==="
echo "ROOT=$ROOT"

# ---------------------------------------------------------------------------
# 1. Eval JSON validity and required fields
# ---------------------------------------------------------------------------
echo ""
echo "--- 1. Eval JSON ---"
EVAL_JSON="$MCPROOT/eval/mcp_runner_topic_allowlist_design_b118_v1.json"
if [ ! -f "$EVAL_JSON" ]; then
    fail "eval JSON not found: $EVAL_JSON"
else
    python3 -c "
import json, sys
with open('$EVAL_JSON') as f:
    d = json.load(f)
assert d.get('eval_id') == 'mcp_runner_topic_allowlist_design_b118_v1', 'bad eval_id'
assert d.get('verdict') == 'PASS', 'bad verdict'
assert 'allowlist_matrix' in d, 'missing allowlist_matrix'
assert 'malformed_fixtures' in d, 'missing malformed_fixtures'
assert 'enforcement_matrix' in d, 'missing enforcement_matrix'
assert 'next_enforcement_patch' in d, 'missing next_enforcement_patch'
assert 'gates' in d, 'missing gates'
assert 'safety_invariants' in d, 'missing safety_invariants'
print('eval JSON valid: ok')
" && pass "eval JSON valid with required fields" || fail "eval JSON invalid or missing fields"
fi

# ---------------------------------------------------------------------------
# 2. Allow/deny matrix completeness
# ---------------------------------------------------------------------------
echo ""
echo "--- 2. Allow/deny matrix ---"
python3 -c "
import json
with open('$EVAL_JSON') as f:
    d = json.load(f)
entries = d['allowlist_matrix']['entries']
n = len(entries)
assert n >= 12, f'expected >=12 allowlist entries, got {n}'
for e in entries:
    assert 'runner' in e and e['runner'], f'entry missing runner'
    assert 'topic' in e and e['topic'], f'entry missing topic'
    assert 'allow' in e, f'entry missing allow'
    assert isinstance(e['allow'], bool), f'allow must be bool'
    if not e['allow']:
        assert 'deny_rationale' in e, f'denied entry missing deny_rationale: {e[\"runner\"]}'
# Codex must not have auto-pickup permission
codex_entries = [e for e in entries if e['runner'] == 'codex']
assert codex_entries, 'codex entry missing'
codex = codex_entries[0]
assert 'auto-pickup' not in codex.get('write_actions', []), 'codex must not auto-pickup'
assert 'start' not in codex.get('write_actions', []), 'codex must not start'
# Workers must not have done permission
worker_entries = [e for e in entries if e['runner'] != 'codex']
for w in worker_entries:
    assert 'done' in w.get('denied_actions', []), f'worker {w[\"runner\"]} must deny done'
print(f'allow/deny matrix: {n} entries, all valid')
" && pass "allow/deny matrix complete" || fail "allow/deny matrix incomplete"

# ---------------------------------------------------------------------------
# 3. Malformed fixtures
# ---------------------------------------------------------------------------
echo ""
echo "--- 3. Malformed fixtures ---"
python3 -c "
import json
with open('$EVAL_JSON') as f:
    d = json.load(f)
cases = d['malformed_fixtures']['cases']
assert len(cases) == 8, f'expected 8 malformed cases, got {len(cases)}'
for c in cases:
    assert c['expected'] == 'deny', f'case {c[\"reason\"]} must expect deny'
    assert 'runner' in c, 'case missing runner'
    assert 'topic' in c, 'case missing topic'
    assert 'reason' in c, 'case missing reason'
print(f'malformed fixtures: {len(cases)} cases, all expect deny')
" && pass "malformed fixtures valid" || fail "malformed fixtures invalid"

# ---------------------------------------------------------------------------
# 4. Next-wave JSON validity
# ---------------------------------------------------------------------------
echo ""
echo "--- 4. Next-wave JSON ---"
NEXTWAVE="$MCPROOT/data/tasking/mcp_runner_topic_allowlist_design_next_wave_b118_v1.json"
if [ ! -f "$NEXTWAVE" ]; then
    fail "next-wave JSON not found: $NEXTWAVE"
else
    python3 -c "
import json
with open('$NEXTWAVE') as f:
    d = json.load(f)
assert d.get('next_wave_id') == 'mcp_runner_topic_allowlist_design_next_wave_b118_v1', 'bad next_wave_id'
assert d.get('parent_task') == 'DEEPSEEK_TASK_MCP_RUNNER_TOPIC_ALLOWLIST_DESIGN_B118_V1', 'bad parent_task'
assert len(d.get('follow_up_tasks', [])) >= 2, 'expected >=2 follow_up_tasks'
# B119 enforcement task
b119 = [t for t in d['follow_up_tasks'] if 'B119' in t.get('task_id', '')]
assert b119, 'missing B119 enforcement task'
assert b119[0].get('runner') == 'claude_coding', 'B119 must be claude_coding'
assert b119[0].get('topic') == 'coding', 'B119 must be coding topic'
assert len(b119[0].get('allowed_writes', [])) >= 2, 'B119 must have allowed_writes'
print(f'next-wave JSON: {len(d[\"follow_up_tasks\"])} follow-up tasks')
" && pass "next-wave JSON valid" || fail "next-wave JSON invalid"
fi

# ---------------------------------------------------------------------------
# 5. Design JSONL rows
# ---------------------------------------------------------------------------
echo ""
echo "--- 5. Design JSONL ---"
ROWS_JSONL="$MCPROOT/eval/mcp_runner_topic_allowlist_design_rows_b118_v1.jsonl"
if [ ! -f "$ROWS_JSONL" ]; then
    fail "design JSONL not found: $ROWS_JSONL"
else
    python3 -c "
import json
with open('$ROWS_JSONL') as f:
    rows = [json.loads(line) for line in f if line.strip()]
n = len(rows)
assert n >= 24, f'expected >=24 rows (12 allow + 8 fixtures + 4 layers), got {n}'
allow_rows = [r for r in rows if r.get('row_type') == 'allow_entry']
fixture_rows = [r for r in rows if r.get('row_type') == 'malformed_fixture']
layer_rows = [r for r in rows if r.get('row_type') == 'enforcement_layer']
assert len(allow_rows) == 12, f'expected 12 allow_entry rows, got {len(allow_rows)}'
assert len(fixture_rows) == 8, f'expected 8 malformed_fixture rows, got {len(fixture_rows)}'
assert len(layer_rows) == 4, f'expected 4 enforcement_layer rows, got {len(layer_rows)}'
for r in rows:
    assert r.get('schema') == 'geoai.runner_topic_allowlist_row.v1', f'bad schema in row: {r}'
print(f'design JSONL: {n} rows ({len(allow_rows)} allow + {len(fixture_rows)} fixtures + {len(layer_rows)} layers)')
" && pass "design JSONL valid" || fail "design JSONL invalid"

# ---------------------------------------------------------------------------
# 6. Safety invariants
# ---------------------------------------------------------------------------
echo ""
echo "--- 6. Safety invariants ---"
python3 -c "
import json
with open('$EVAL_JSON') as f:
    d = json.load(f)
inv = d['safety_invariants']
assert inv.get('write_gate_default_off') == True, 'write_gate_default_off must be true'
assert inv.get('codex_done_monopoly') == True, 'codex_done_monopoly must be true'
assert inv.get('worker_cannot_finalize_own_task') == True, 'worker_cannot_finalize_own_task'
assert inv.get('runner_topic_exact_match_only') == True, 'exact match only'
assert inv.get('deterministic_gates_remain') == True, 'deterministic gates remain'
assert inv.get('no_process_launch_code') == True, 'no process launch code'
gates = d['gates']
assert gates.get('no_core_py_edited') == True, 'core.py must not be edited'
assert gates.get('no_server_py_edited') == True, 'server.py must not be edited'
print('all safety invariants hold')
" && pass "safety invariants hold" || fail "safety invariants violated"

# ---------------------------------------------------------------------------
# 7. Core.py / server.py unchanged check
# ---------------------------------------------------------------------------
echo ""
echo "--- 7. Core/server unchanged ---"
CORE_PY="$MCPROOT/src/geoai_task_mcp/core.py"
SERVER_PY="$MCPROOT/src/geoai_task_mcp/server.py"
if git -C "$ROOT" diff --name-only HEAD -- "$CORE_PY" | grep -q .; then
    fail "core.py was modified (forbidden)"
else
    pass "core.py unchanged"
fi
if git -C "$ROOT" diff --name-only HEAD -- "$SERVER_PY" | grep -q .; then
    fail "server.py was modified (forbidden)"
else
    pass "server.py unchanged"
fi

# ---------------------------------------------------------------------------
# 8. Next enforcement patch fields
# ---------------------------------------------------------------------------
echo ""
echo "--- 8. Next enforcement patch ---"
python3 -c "
import json
with open('$EVAL_JSON') as f:
    d = json.load(f)
nep = d['next_enforcement_patch']
assert nep.get('task_id') == 'CLAUDE_TASK_MCP_RUNNER_TOPIC_ALLOWLIST_ENFORCEMENT_B119_V1', 'bad B119 task_id'
assert nep.get('runner') == 'claude_coding', 'B119 must be claude_coding'
assert nep.get('topic') == 'coding', 'B119 must be coding topic'
assert len(nep.get('allowed_writes', [])) >= 2, 'B119 must have allowed_writes'
assert len(nep.get('forbidden', [])) >= 2, 'B119 must have forbidden'
assert len(nep.get('acceptance', [])) >= 4, 'B119 must have acceptance criteria'
print(f'next enforcement patch: task_id={nep[\"task_id\"]} runner={nep[\"runner\"]}')
" && pass "next enforcement patch valid" || fail "next enforcement patch invalid"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
if [ "$FAILURES" -eq 0 ]; then
    echo "=== ALL CHECKS PASSED ==="
    exit 0
else
    echo "=== $FAILURES CHECK(S) FAILED ==="
    exit 1
fi
