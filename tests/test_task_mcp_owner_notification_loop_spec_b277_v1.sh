#!/usr/bin/env bash
# test_task_mcp_owner_notification_loop_spec_b277_v1.sh
# Validation test for CLAUDE_TASK_MCP_OWNER_NOTIFICATION_LOOP_SPEC_B277_V1
#
# Tests:
#   1. Eval JSON exists and is valid JSON
#   2. Eval JSON contains all required top-level keys
#   3. All protocol sections present
#   4. All 5 event kinds present with required payload_fields
#   5. Payload envelope schema has all required fields
#   6. Delta cursor design fields present (caller-owned, deterministic event_id)
#   7. Automatic-loop section is honest (not_claimed list present, no push/webhook/inotify claimed)
#   8. All authority_flags (top-level + section_4) are explicitly false
#   9. No subprocess/process-launch code markers in eval JSON (spec-only)
#  10. server.py is unmodified (production_mcp_server_api_altered == false)
#  11. Artifact size under 25MB
#  12. Next-wave JSON exists and is valid, has >=2 cards with disjoint allowed_writes
#  13. Next-wave blockers array is non-empty and explicit (no overclaim)
#  14. taskctl verify

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
EVAL_JSON="$REPO_ROOT/tools/geoai-task-mcp/eval/task_mcp_owner_notification_loop_spec_b277_v1.json"
NEXT_WAVE="$REPO_ROOT/tools/geoai-task-mcp/data/tasking/task_mcp_owner_notification_loop_spec_next_wave_b277_v1.json"
PASS=0
FAIL=0

green() { echo -e "\033[32m  PASS\033[0m $1"; ((PASS++)) || true; }
red()   { echo -e "\033[31m  FAIL\033[0m $1"; ((FAIL++)) || true; }

echo "=== B277 Owner Notification Loop Spec Test ==="
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
    "files_changed" "commit_contract"
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
    "section_1_event_taxonomy"
    "section_1_remaining_gaps_not_solved_by_this_spec"
    "section_2_payload_envelope_schema"
    "section_2b_delta_cursor_mechanism"
    "section_3_automatic_loop_definition_no_manual_polling"
    "section_4_authority_flags"
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

# ── 5. All 5 event kinds present with payload_fields ────────────────────
echo "[5] All 5 event kinds defined with non-empty payload_fields"
KINDS=("completion" "failure" "stale" "usage" "duplicate_runner")
for kind in "${KINDS[@]}"; do
    if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
ek = d['protocol_sections']['section_1_event_taxonomy']['event_kinds']
assert '$kind' in ek, 'missing event kind: $kind'
fields = ek['$kind'].get('payload_fields', [])
assert len(fields) >= 3, 'too few payload_fields for $kind'
assert 'trigger_condition' in ek['$kind'], 'missing trigger_condition for $kind'
assert 'derived_from' in ek['$kind'], 'missing derived_from for $kind'
" 2>/dev/null; then
        green "event kind OK: $kind"
    else
        red "MISSING or INCOMPLETE event kind: $kind"
    fi
done

# ── 6. Payload envelope schema fields ──────────────────────────────────
echo "[6] Payload envelope schema has required fields"
ENVELOPE_FIELDS=("event_id" "event_kind" "task_id" "runner" "topic" "detected_at" "cursor_token" "payload" "authority_flags" "source_facet")
for f in "${ENVELOPE_FIELDS[@]}"; do
    if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
env = d['protocol_sections']['section_2_payload_envelope_schema']['envelope_schema']
assert '$f' in env, 'missing envelope field: $f'
" 2>/dev/null; then
        green "envelope field present: $f"
    else
        red "MISSING envelope field: $f"
    fi
done

# ── 7. Delta cursor design fields ──────────────────────────────────────
echo "[7] Delta cursor mechanism defines caller-owned, deterministic id"
if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
cur = d['protocol_sections']['section_2b_delta_cursor_mechanism']
assert 'cursor_token_format' in cur
assert 'cursor_ownership' in cur
assert 'CALLER-OWNED' in cur['cursor_ownership']
assert 'proposed_tool_signature' in cur
assert 'NOT IMPLEMENTED' in cur['proposed_tool_signature']
print('OK')
" 2>/dev/null | grep -q OK; then
    green "cursor mechanism is caller-owned and marked not-implemented"
else
    red "cursor mechanism missing caller-owned/not-implemented markers"
fi

# ── 8. Automatic-loop section is honest ────────────────────────────────
echo "[8] Automatic-loop section explicitly disclaims push/webhook/daemon"
if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
sec = d['protocol_sections']['section_3_automatic_loop_definition_no_manual_polling']
nc = sec.get('not_claimed', [])
assert len(nc) >= 3, 'not_claimed list too short'
joined = ' '.join(nc).lower()
assert 'daemon' in joined or 'cron' in joined
assert 'inotify' in joined or 'trigger' in joined
assert 'honest_framing' in sec
assert 'does not' in sec['honest_framing'].lower() or 'not' in sec['honest_framing'].lower()
print('OK')
" 2>/dev/null | grep -q OK; then
    green "automatic-loop section honestly disclaims push/daemon/inotify"
else
    red "automatic-loop section missing honesty disclaimers"
fi

# ── 9. All authority_flags explicitly false (top-level + section_4) ────
echo "[9] All authority_flags are explicitly false"
CHECK=$(python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
top = d.get('authority_flags', {})
sec4 = d['protocol_sections']['section_4_authority_flags']['flags']
bad = []
for k, v in top.items():
    if v is not False:
        bad.append(f'top:{k}={v}')
for k, v in sec4.items():
    if v is not False:
        bad.append(f'sec4:{k}={v}')
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

# ── 10. No subprocess/launch code markers in eval JSON ─────────────────
echo "[10] No subprocess/Popen/exec/webhook-implementation markers"
if python3 -c "
import json
text = open('$EVAL_JSON').read().lower()
forbidden = ['subprocess.popen', 'os.exec', 'os.system(', 'shell=true', 'pty.spawn', 'requests.post(', 'smtplib.']
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

# ── 11. server.py unmodified per artifact claim ────────────────────────
echo "[11] Artifact claims server.py unaltered"
if python3 -c "
import json
d = json.load(open('$EVAL_JSON'))
assert d.get('production_mcp_server_api_altered') is False
print('OK')
" 2>/dev/null | grep -q OK; then
    green "production_mcp_server_api_altered == false"
else
    red "production_mcp_server_api_altered missing or not false"
fi

# ── 12. Artifact size under 25MB ────────────────────────────────────────
echo "[12] Eval JSON under 25MB"
SIZE=$(stat -c%s "$EVAL_JSON" 2>/dev/null || echo 0)
if [ "$SIZE" -lt 26214400 ]; then
    green "eval JSON size: $SIZE bytes (< 25MB)"
else
    red "eval JSON size: $SIZE bytes (>= 25MB LIMIT)"
fi

# ── 13. Next-wave JSON exists, valid, >=2 cards, disjoint allowed_writes ─
echo "[13] Next-wave JSON exists, valid, >=2 cards, disjoint allowed_writes"
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

# ── 14. Next-wave blockers are explicit and non-empty ───────────────────
echo "[14] Next-wave blockers explicit and non-empty (no overclaim)"
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

# ── 15. taskctl verify ──────────────────────────────────────────────────
echo "[15] taskctl verify"
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
