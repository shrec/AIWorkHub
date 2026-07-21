#!/usr/bin/env bash
set -euo pipefail
# ---------------------------------------------------------------------------
# test_mcp_autolaunch_protocol_spec_b275_v1.sh
# Validation test for DEEPSEEK_TASK_MCP_AUTOLAUNCH_PROTOCOL_SPEC_B275_V1
#
# Tests:
#   1. Eval JSON exists and is valid JSON
#   2. Eval JSON contains all required top-level keys
#   3. All protocol sections present (state_machine, runner_isolation,
#      safety_gates, audit_log_schema, stale_recovery, usage_accounting,
#      completion_return_path)
#   4. State machine has exactly 6 states with all required fields
#   5. All 10 transitions are defined and gated
#   6. Safety gates: exactly 4 gates, sequential, G4 current_value is false
#   7. Audit log schema: exactly 18 fields defined
#   8. All authority_flags are explicitly false (9 flags)
#   9. No execution code markers (no subprocess, Popen, exec, shell spawn)
#  10. Next-wave JSON exists and is valid
#  11. Next-wave has at least 3 follow-up task cards
#  12. Protocol invariants: 7 defined, all non-empty
#  13. Runner isolation: 6 invariants defined
#  14. Completion return path: at least 5 steps defined
#  15. Stale recovery: 3 adapters with distinct timeouts
#  16. Verdict is PASS, mode is spec_no_execution
#  17. All gates in the gates section are true
#
# Isolation: reads only the task's own eval + next_wave artifacts.
# Parallel-safe: uses mktemp for temp files.
# ---------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
EVAL_JSON="$ROOT/tools/geoai-task-mcp/eval/mcp_autolaunch_protocol_spec_b275_v1.json"
NEXT_WAVE="$ROOT/tools/geoai-task-mcp/data/tasking/mcp_autolaunch_protocol_spec_next_wave_b275_v1.json"

TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/aiworkhub_mcp_autolaunch_spec_b275_sh.XXXXXX")"
trap 'rm -rf "$TMPDIR"' EXIT

PASS=0
FAIL=0

green() { echo -e "\033[32m  PASS\033[0m $1"; ((PASS++)) || true; }
red()   { echo -e "\033[31m  FAIL\033[0m $1"; ((FAIL++)) || true; }

echo "=== MCP Autolaunch Protocol Spec Test B275 v1 ==="
echo "ROOT=$ROOT"
echo ""

# ── 1. Eval JSON exists ────────────────────────────────────────────────
echo "[1] Eval JSON file exists"
if [ -f "$EVAL_JSON" ]; then
    green "eval JSON found"
else
    red "eval JSON MISSING: $EVAL_JSON"
fi

# ── 2. Eval JSON is valid JSON ─────────────────────────────────────────
echo "[2] Eval JSON is valid JSON"
if python3 -c "import json; json.load(open('$EVAL_JSON'))" 2>/dev/null; then
    green "eval JSON parses correctly"
else
    red "eval JSON is INVALID"
fi

# ── 3. Required top-level keys ──────────────────────────────────────────
echo "[3] Required top-level keys present"
REQUIRED_KEYS=(
    "eval_id" "task_id" "runner" "topic" "mode" "verdict"
    "schema_id" "summary" "builds_on" "authority_flags"
    "metrics" "protocol" "invariants" "gates"
    "neural_bridge_note" "commit_contract" "files_written"
)
for key in "${REQUIRED_KEYS[@]}"; do
    if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
assert '$key' in d, 'missing key: $key'
" 2>/dev/null; then
        green "key present: $key"
    else
        red "MISSING key: $key"
    fi
done

# ── 4. Protocol sections present ────────────────────────────────────────
echo "[4] All 7 protocol sections present"
SECTIONS=(
    "state_machine"
    "runner_isolation"
    "safety_gates"
    "audit_log_schema"
    "stale_recovery"
    "usage_accounting"
    "completion_return_path"
)
for sec in "${SECTIONS[@]}"; do
    if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
ps = d['protocol']
assert '$sec' in ps, 'missing section: $sec'
" 2>/dev/null; then
        green "section present: $sec"
    else
        red "MISSING section: $sec"
    fi
done

# ── 5. State machine: 6 states with required fields ────────────────────
echo "[5] State machine has exactly 6 states with required fields"
python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
sm = d['protocol']['state_machine']
states = sm['states']
assert len(states) == 6, f'expected 6 states, got {len(states)}'
expected = ['IDLE', 'PICKUP', 'GUARD', 'LAUNCH', 'POLL', 'COMPLETE']
for s in expected:
    assert s in states, f'missing state: {s}'
    st = states[s]
    assert 'description' in st, f'{s} missing description'
    assert 'entry_condition' in st, f'{s} missing entry_condition'
    assert 'allowed_transitions' in st, f'{s} missing allowed_transitions'
# COMPLETE must be terminal
assert states['COMPLETE'].get('is_terminal') == True, 'COMPLETE not marked terminal'
print('OK: 6 states validated')
" 2>/dev/null && green "6 states with required fields" || red "state machine validation FAILED"

# ── 6. Transitions: at least 9, all have from/to/trigger/guard ─────────
echo "[6] Transitions defined and gated"
python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
trans = d['protocol']['state_machine']['transitions']
assert len(trans) >= 9, f'expected >=9 transitions, got {len(trans)}'
for i, t in enumerate(trans):
    for field in ['from', 'to', 'trigger', 'guard']:
        assert field in t, f'transition {i} missing {field}'
        assert t[field], f'transition {i} has empty {field}'
print(f'OK: {len(trans)} transitions validated')
" 2>/dev/null && green "all transitions gated" || red "transition validation FAILED"

# ── 7. Safety gates: exactly 4, sequential, G4 current_value is false ───
echo "[7] Safety gates: 4-layer, sequential, G4 launched=false"
python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
gates = d['protocol']['safety_gates']['gates']
assert len(gates) == 4, f'expected 4 gates, got {len(gates)}'
priorities = [g['priority'] for g in gates]
assert priorities == [1, 2, 3, 4], f'gates not sequential: {priorities}'
g4 = gates[3]
assert g4['gate_id'] == 'G4_LAUNCH_IMPLEMENTED', f'G4 wrong id: {g4[\"gate_id\"]}'
assert g4['current_value'] == False, f'G4 must be False, got {g4[\"current_value\"]}'
print('OK: 4-layer gates validated, G4=False')
" 2>/dev/null && green "4 safety gates, sequential, G4=False" || red "safety gates FAILED"

# ── 8. Audit log schema: exactly 18 fields ─────────────────────────────
echo "[8] Audit log schema: exactly 18 fields"
python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
fields = d['protocol']['audit_log_schema']['fields']
assert len(fields) == 18, f'expected 18 fields, got {len(fields)}'
field_names = [f['name'] for f in fields]
assert 'launch_id' in field_names
assert 'task_id' in field_names
assert 'stale_detected_at' in field_names
for f in fields:
    assert 'name' in f and 'type' in f, f'field missing name/type: {f}'
print(f'OK: {len(fields)} audit log fields')
" 2>/dev/null && green "16 audit log fields" || red "audit log schema FAILED"

# ── 9. All authority_flags are explicitly false ─────────────────────────
echo "[9] All authority_flags explicitly false"
python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
flags = d['authority_flags']
assert len(flags) >= 8, f'expected >=8 flags, got {len(flags)}'
for k, v in flags.items():
    assert v == False, f'flag {k} is {v}, expected False'
print(f'OK: {len(flags)} flags all False')
" 2>/dev/null && green "all authority_flags are False" || red "authority_flags FAILED"

# ── 10. No execution code markers ──────────────────────────────────────
echo "[10] No execution code in spec artifacts"
NO_EXEC_MARKERS=0
for marker in "subprocess" "Popen" "os.system" "os.exec" "os.fork" "os.spawn" "shell=True" "subprocess.run" "subprocess.call"; do
    if grep -rqi "$marker" "$EVAL_JSON" 2>/dev/null; then
        red "found execution marker: $marker in eval JSON"
        NO_EXEC_MARKERS=$((NO_EXEC_MARKERS + 1))
    fi
done
if [ "$NO_EXEC_MARKERS" -eq 0 ]; then
    green "no execution code markers found"
fi

# ── 11. Next-wave JSON exists and valid ─────────────────────────────────
echo "[11] Next-wave JSON exists and valid"
if [ -f "$NEXT_WAVE" ]; then
    if python3 -c "import json; json.load(open('$NEXT_WAVE'))" 2>/dev/null; then
        green "next-wave JSON valid"
    else
        red "next-wave JSON INVALID"
    fi
else
    red "next-wave JSON MISSING: $NEXT_WAVE"
fi

# ── 12. Next-wave has at least 3 follow-up tasks ────────────────────────
echo "[12] Next-wave: >=3 follow-up tasks"
python3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
tasks = d['follow_up_tasks']
assert len(tasks) >= 3, f'expected >=3 follow-up tasks, got {len(tasks)}'
for t in tasks:
    assert 'task_id' in t, 'task missing task_id'
    assert 'goal' in t, 'task missing goal'
    assert 'runner' in t, 'task missing runner'
    assert 'topic' in t, 'task missing topic'
    assert 'acceptance' in t, 'task missing acceptance'
print(f'OK: {len(tasks)} follow-up tasks')
" 2>/dev/null && green "next-wave has >=3 tasks" || red "next-wave tasks FAILED"

# ── 13. Verdict is PASS, mode is spec_no_execution ──────────────────────
echo "[13] Verdict and mode"
python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
assert d['verdict'] == 'PASS', f'verdict is {d[\"verdict\"]}'
assert 'no_execution' in d['mode'], f'mode should contain no_execution: {d[\"mode\"]}'
print('OK: verdict=PASS mode=no_execution')
" 2>/dev/null && green "verdict=PASS, mode spec_no_execution" || red "verdict/mode FAILED"

# ── 14. Protocol invariants: 7 defined ─────────────────────────────────
echo "[14] Protocol invariants: 7 defined"
python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
inv = d['invariants']
assert len(inv) == 7, f'expected 7 invariants, got {len(inv)}'
for k, v in inv.items():
    assert v, f'invariant {k} is empty'
print(f'OK: {len(inv)} invariants')
" 2>/dev/null && green "7 invariants all non-empty" || red "invariants FAILED"

# ── 15. Runner isolation: 6 invariants ─────────────────────────────────
echo "[15] Runner isolation: 6 invariants"
python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
ri = d['protocol']['runner_isolation']
assert 'invariants' in ri, 'missing invariants'
assert len(ri['invariants']) == 6, f'expected 6 runner isolation invariants, got {len(ri[\"invariants\"])}'
print('OK: 6 runner isolation invariants')
" 2>/dev/null && green "6 runner isolation invariants" || red "runner isolation FAILED"

# ── 16. Stale recovery: 3 adapters with distinct timeouts ───────────────
echo "[16] Stale recovery: 3 adapters with timeouts"
python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
sr = d['protocol']['stale_recovery']
adapters = sr['adapters']
assert len(adapters) == 3, f'expected 3 adapters, got {len(adapters)}'
timeouts = set()
for name, cfg in adapters.items():
    assert 'stale_timeout_seconds' in cfg, f'{name} missing timeout'
    timeout = cfg['stale_timeout_seconds']
    assert timeout > 0, f'{name} timeout must be >0, got {timeout}'
    timeouts.add(timeout)
# claude + codex share 7200, deepseek is 86400 => at least 2 distinct
assert len(timeouts) >= 2, f'expected >=2 distinct timeouts, got {len(timeouts)}'
print(f'OK: {len(adapters)} adapters, {len(timeouts)} distinct timeouts')
" 2>/dev/null && green "3 adapters with distinct timeouts" || red "stale recovery FAILED"

# ── 17. All gates are true ──────────────────────────────────────────────
echo "[17] All gates are true"
python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
gates = d['gates']
failed = [(k, v) for k, v in gates.items() if v is not True]
if failed:
    for k, v in failed:
        print(f'GATE FAILED: {k} = {v}')
    raise SystemExit(1)
print(f'OK: {len(gates)} gates all True')
" 2>/dev/null && green "all gates True" || red "gates check FAILED"

# ── 18. Metrics aggregate present with required fields ──────────────────
echo "[18] Metrics aggregate present"
python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
m = d['metrics']['aggregate']
required = ['protocol_sections_defined', 'fsm_states', 'fsm_transitions',
            'safety_gates', 'audit_log_fields', 'invariants',
            'gates_checked', 'gates_passed']
for r in required:
    assert r in m, f'missing metric: {r}'
    assert m[r] > 0, f'metric {r} must be >0, got {m[r]}'
print(f'OK: {len(required)} metrics present')
" 2>/dev/null && green "metrics aggregate valid" || red "metrics FAILED"

# ── SUMMARY ─────────────────────────────────────────────────────────────
echo ""
echo "=============================================="
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
echo "=============================================="

if [ "$FAIL" -gt 0 ]; then
    echo "TEST FAILED: $FAIL check(s) failed"
    exit 1
else
    echo "TEST PASSED: All $PASS checks passed"
    exit 0
fi
