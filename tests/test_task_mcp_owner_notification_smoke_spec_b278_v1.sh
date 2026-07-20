#!/usr/bin/env bash
# test_task_mcp_owner_notification_smoke_spec_b278_v1.sh
# Validation test for CLAUDE_TASK_MCP_OWNER_NOTIFICATION_SMOKE_SPEC_B278_V1
#
# Tests:
#   1. Eval JSON exists and is valid JSON
#   2. Eval JSON contains all required top-level keys
#   3. All protocol sections present
#   4. Label mapping (section_0) present, canonical B277 enum note present, no overclaim note present
#   5. All 6 smoke scenarios present with fixture + expected_event_kind + owner_facing_summary
#   6. tool_error scenario is explicitly distinguished from failure/blocked
#   7. no_pending_task edge case defined (zero-events, not an error)
#   8. duplicate_runner edge case cross-referenced
#   9. Owner-facing summary template index has all 7 entries (6 scenarios + no_pending_task)
#  10. All authority_flags (top-level + section_5) are explicitly false
#  11. No subprocess/model-launch/network-send code markers in eval JSON
#  12. server.py unmodified per artifact claim (production_mcp_server_api_altered == false)
#  13. Artifact size under 25MB
#  14. Next-wave JSON exists and is valid, has >=2 cards with disjoint allowed_writes
#  15. Next-wave blockers array is non-empty and explicit (no overclaim)
#  16. taskctl verify

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
EVAL_JSON="$REPO_ROOT/tools/geoai-task-mcp/eval/task_mcp_owner_notification_smoke_spec_b278_v1.json"
NEXT_WAVE="$REPO_ROOT/tools/geoai-task-mcp/data/tasking/task_mcp_owner_notification_smoke_spec_next_wave_b278_v1.json"
PASS=0
FAIL=0

green() { echo -e "\033[32m  PASS\033[0m $1"; ((PASS++)) || true; }
red()   { echo -e "\033[31m  FAIL\033[0m $1"; ((FAIL++)) || true; }

echo "=== B278 Owner Notification Smoke Spec Test ==="
echo ""

# ── 1. Eval JSON exists ───────────────────────────────────────────────
echo "[1] Eval JSON file exists"
if [ -f "$EVAL_JSON" ]; then
    green "eval JSON found at $EVAL_JSON"
else
    red "eval JSON MISSING: $EVAL_JSON"
fi

# ── 2. Eval JSON is valid JSON ────────────────────────────────────────
echo "[2] Eval JSON is valid JSON"
if python3 -c "import json; json.load(open('$EVAL_JSON'))" 2>/dev/null; then
    green "eval JSON parses correctly"
else
    red "eval JSON is INVALID"
fi

# ── 3. Required top-level keys ─────────────────────────────────────────
echo "[3] Required top-level keys present"
REQUIRED_KEYS=(
    "eval_id" "task_id" "runner" "topic" "mode" "verdict"
    "protocol_sections" "gates" "authority_flags"
    "files_changed" "commit_contract" "no_subprocess_model_launch"
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

# ── 4. Protocol sections present ───────────────────────────────────────
echo "[4] All protocol sections present"
SECTIONS=(
    "section_0_scope_and_label_mapping"
    "section_1_smoke_scenarios"
    "section_2_tool_error_definition"
    "section_3_edge_cases"
    "section_4_owner_facing_summary_index"
    "section_5_authority_flags"
)
for sec in "${SECTIONS[@]}"; do
    if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
ps = d['protocol_sections']
assert '$sec' in ps, 'missing section: $sec'
" 2>/dev/null; then
        green "section present: $sec"
    else
        red "MISSING section: $sec"
    fi
done

# ── 5. Label mapping honesty note ──────────────────────────────────────
echo "[5] Label mapping present, canonical B277 enum unchanged, no-overclaim note present"
if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
sec0 = d['protocol_sections']['section_0_scope_and_label_mapping']
assert 'canonical_event_kind_enum_unchanged' in sec0
assert 'failure' in sec0['canonical_event_kind_enum_unchanged']
mapping = sec0['card_wording_to_canonical_mapping']
for k in ['completion', 'blocked', 'duplicate-runner', 'stale', 'usage', 'error']:
    assert k in mapping, f'missing mapping for {k}'
assert 'no_overclaim_note' in sec0
assert sec0['server_py_modified'] is False
assert sec0['no_new_mcp_tool_registered'] is True
print('OK')
" 2>/dev/null | grep -q OK; then
    green "label mapping + honesty notes present"
else
    red "label mapping or honesty notes missing/incomplete"
fi

# ── 6. All 6 smoke scenarios present ───────────────────────────────────
echo "[6] All 6 smoke scenarios defined with fixture + expected_event_kind + owner summary"
SCENARIOS=("scenario_completion" "scenario_blocked" "scenario_duplicate_runner" "scenario_stale" "scenario_usage" "scenario_tool_error")
for scen in "${SCENARIOS[@]}"; do
    if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
scens = d['protocol_sections']['section_1_smoke_scenarios']['scenarios']
assert '$scen' in scens, 'missing scenario: $scen'
s = scens['$scen']
assert 'expected_event_kind' in s, 'missing expected_event_kind for $scen'
has_fixture = 'fixture' in s or 'fixture_condition' in s
assert has_fixture, 'missing fixture for $scen'
has_summary = any(k.startswith('owner_facing_summary') for k in s)
assert has_summary, 'missing owner_facing_summary for $scen'
" 2>/dev/null; then
        green "scenario OK: $scen"
    else
        red "MISSING or INCOMPLETE scenario: $scen"
    fi
done

# ── 7. tool_error distinguished from failure/blocked ───────────────────
echo "[7] tool_error scenario explicitly distinguished from failure/blocked"
if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
te = d['protocol_sections']['section_1_smoke_scenarios']['scenarios']['scenario_tool_error']
assert 'distinct_from_failure_note' in te
note = te['distinct_from_failure_note'].lower()
assert 'not' in note and 'failure' in note
assert te['expected_payload'].get('task_id') is None, 'tool_error payload must have null task_id (no row was read)'
print('OK')
" 2>/dev/null | grep -q OK; then
    green "tool_error is explicitly distinguished from failure/blocked"
else
    red "tool_error disambiguation missing or incomplete"
fi

# ── 8. no_pending_task edge case ───────────────────────────────────────
echo "[8] no_pending_task edge case defined as zero-events, not an error"
if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
ec = d['protocol_sections']['section_3_edge_cases']
assert 'no_pending_task' in ec
npt = ec['no_pending_task']
assert 'expected_behavior' in npt
beh = npt['expected_behavior'].lower()
assert 'zero events' in beh
assert 'not' in beh and 'error' in beh
print('OK')
" 2>/dev/null | grep -q OK; then
    green "no_pending_task edge case present and honestly scoped"
else
    red "no_pending_task edge case missing or missing zero-events/not-an-error framing"
fi

# ── 9. duplicate_runner edge case cross-reference ──────────────────────
echo "[9] duplicate_runner edge case present (cross-referenced to scenario)"
if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
ec = d['protocol_sections']['section_3_edge_cases']
assert 'duplicate_runner_conflict_detail' in ec
print('OK')
" 2>/dev/null | grep -q OK; then
    green "duplicate_runner edge case present"
else
    red "duplicate_runner edge case missing"
fi

# ── 10. Owner-facing summary template index complete ───────────────────
echo "[10] Owner-facing summary template index has all 7 entries"
if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
templates = d['protocol_sections']['section_4_owner_facing_summary_index']['templates']
required = ['completion', 'blocked_failure', 'duplicate_runner', 'stale', 'usage', 'tool_error', 'no_pending_task']
for r in required:
    assert r in templates, f'missing template: {r}'
    assert len(templates[r]) > 10
print('OK')
" 2>/dev/null | grep -q OK; then
    green "all 7 owner-facing summary templates present"
else
    red "owner-facing summary template index incomplete"
fi

# ── 11. All authority_flags explicitly false (top-level + section_5) ───
echo "[11] All authority_flags are explicitly false"
CHECK=$(python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
top = d.get('authority_flags', {})
sec5 = d['protocol_sections']['section_5_authority_flags']['flags']
bad = []
for k, v in top.items():
    if v is not False:
        bad.append(f'top:{k}={v}')
for k, v in sec5.items():
    if v is not False:
        bad.append(f'sec5:{k}={v}')
required = ['runtime_authority','support_authority','model_weight_write','score_apply',
            'canonical_lexicon_mutation','atlas_bank_mutation','agent_or_process_launch_apply',
            'training_launch']
for r in required:
    assert r in top, f'missing required flag: {r}'
if bad:
    print('BAD:' + ','.join(bad))
else:
    print('ALL_FALSE_OK')
" 2>/dev/null)
if echo "$CHECK" | grep -q "ALL_FALSE_OK"; then
    green "all authority_flags explicitly false"
else
    red "authority flag violation: $CHECK"
fi

# ── 12. No subprocess/launch code markers in eval JSON ─────────────────
echo "[12] No subprocess/Popen/exec/model-launch code markers"
if python3 -c "
import json
text = open('$EVAL_JSON').read().lower()
forbidden = ['subprocess.popen', 'os.exec', 'os.system(', 'shell=true', 'pty.spawn', 'requests.post(', 'smtplib.', 'claude --print', 'codex exec']
for f in forbidden:
    if f in text:
        print(f'FORBIDDEN_MARKER: {f}')
        raise SystemExit(1)
print('CLEAN')
" 2>/dev/null | grep -q CLEAN; then
    green "no process-launch/network-send code markers in eval JSON"
else
    red "FORBIDDEN marker found in eval JSON"
fi

# ── 13. server.py unmodified per artifact claim ────────────────────────
echo "[13] Artifact claims server.py unaltered"
if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
assert d.get('production_mcp_server_api_altered') is False
assert d.get('no_subprocess_model_launch') is True
print('OK')
" 2>/dev/null | grep -q OK; then
    green "production_mcp_server_api_altered == false, no_subprocess_model_launch == true"
else
    red "server.py alteration/no-launch claims missing or wrong"
fi

# ── 14. Artifact size under 25MB ────────────────────────────────────────
echo "[14] Eval JSON under 25MB"
SIZE=$(stat -c%s "$EVAL_JSON" 2>/dev/null || echo 0)
if [ "$SIZE" -lt 26214400 ]; then
    green "eval JSON size: $SIZE bytes (< 25MB)"
else
    red "eval JSON size: $SIZE bytes (>= 25MB LIMIT)"
fi

# ── 15. Next-wave JSON exists, valid, >=2 cards, disjoint allowed_writes ─
echo "[15] Next-wave JSON exists, valid, >=2 cards, disjoint allowed_writes"
if [ -f "$NEXT_WAVE" ] && python3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
cards = d.get('next_wave_cards', [])
assert len(cards) >= 2, f'only {len(cards)} cards'
all_paths = []
for c in cards:
    all_paths.extend(c.get('allowed_writes', []))
seen = set()
dups = [p for p in all_paths if p in seen or seen.add(p)]
assert not dups, f'overlap: {dups}'
print('OK')
" 2>/dev/null | grep -q OK; then
    green "next-wave has >=2 cards with disjoint allowed_writes"
else
    red "next-wave missing, invalid, or has <2 cards / overlapping allowed_writes"
fi

# ── 16. Next-wave blockers are explicit and non-empty ───────────────────
echo "[16] Next-wave blockers explicit and non-empty (no overclaim)"
if python3 -c "
import json
d = json.load(open('$NEXT_WAVE'))
blockers = d.get('blockers', [])
assert len(blockers) >= 2, f'only {len(blockers)} blockers'
for b in blockers:
    assert len(b) > 20, 'blocker too terse: ' + b
print('OK')
" 2>/dev/null | grep -q OK; then
    green "next-wave blockers present and substantive"
else
    red "next-wave blockers missing or too terse"
fi

# ── 17. taskctl verify ──────────────────────────────────────────────────
echo "[17] taskctl verify"
if python3 "$REPO_ROOT/AITools/taskctl.py" verify 2>/dev/null; then
    green "taskctl verify PASS"
else
    red "taskctl verify FAIL"
fi

# ── Summary ────────────────────────────────────────────────────────────
echo ""
echo "=== RESULTS: $PASS PASS, $FAIL FAIL ==="
if [ "$FAIL" -gt 0 ]; then
    echo "VERDICT: FAIL"
    exit 1
else
    echo "VERDICT: PASS"
    exit 0
fi
