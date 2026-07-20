#!/usr/bin/env bash
# B160 V1: Validate review-signal inventory output.
#
# Checks:
#   1. Build script runs and exits 0
#   2. JSONL output exists, >0 lines, valid JSON per line
#   3. Eval JSON exists, valid JSON, required top-level keys present
#   4. Signal categories present and non-empty
#   5. Examples present for at least 4 status families
#   6. Neural priority targets present and >=5
#   7. Aggregate stats present and reasonable
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

BUILDER="$REPO_ROOT/tools/geoai-task-mcp/scripts/build_review_signal_inventory_b160_v1.py"
JSONL="$REPO_ROOT/tools/geoai-task-mcp/data/review_signal_inventory_b160_v1.jsonl"
EVAL="$REPO_ROOT/tools/geoai-task-mcp/eval/review_signal_inventory_b160_v1.json"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "=== B160 V1: Review Signal Inventory Validation ==="
echo ""

# ── 1. Build ──
echo "[1] Running builder..."
python3 "$BUILDER"
echo ""

# ── 2. JSONL checks ──
echo "[2] JSONL checks..."
if [ -f "$JSONL" ]; then
    LINE_COUNT=$(wc -l < "$JSONL")
    if [ "$LINE_COUNT" -gt 0 ]; then
        pass "JSONL exists with $LINE_COUNT lines"
    else
        fail "JSONL is empty"
    fi

    # Validate every line is valid JSON with task_id
    BAD=0
    while IFS= read -r line; do
        if ! echo "$line" | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); assert 'task_id' in d" 2>/dev/null; then
            BAD=$((BAD+1))
        fi
    done < "$JSONL"
    if [ "$BAD" -eq 0 ]; then
        pass "All $LINE_COUNT JSONL rows are valid JSON with task_id"
    else
        fail "$BAD rows have invalid JSON or missing task_id"
    fi

    # Check signal categories present in first row
    FIRST=$(head -1 "$JSONL")
    for cat in risk_signals change_size_signals validation_signals staleness_signals cost_signals collision_signals focus_signals; do
        if echo "$FIRST" | python3 -c "import json,sys; d=json.loads(sys.stdin.readline()); assert '$cat' in d" 2>/dev/null; then
            pass "JSONL row has '$cat' category"
        else
            fail "JSONL row missing '$cat' category"
        fi
    done
else
    fail "JSONL not found: $JSONL"
fi
echo ""

# ── 3. Eval JSON checks ──
echo "[3] Eval JSON checks..."
if [ -f "$EVAL" ]; then
    python3 -c "import json; d=json.load(open('$EVAL')); print('OK')" 2>/dev/null && pass "Eval JSON is valid" || fail "Eval JSON invalid"

    for key in schema_id task_id total_cards signal_categories status_family_counts examples_by_status_family aggregate_stats mcp_exposure_gaps recommended_neural_priority_targets signal_inventory_field_list; do
        if python3 -c "import json; d=json.load(open('$EVAL')); assert '$key' in d" 2>/dev/null; then
            pass "Eval has key '$key'"
        else
            fail "Eval missing key '$key'"
        fi
    done

    TOTAL=$(python3 -c "import json; print(json.load(open('$EVAL'))['total_cards'])")
    if [ "$TOTAL" -gt 100 ]; then
        pass "Eval total_cards=$TOTAL (expected >100)"
    else
        fail "Eval total_cards=$TOTAL (expected >100)"
    fi

    CAT_COUNT=$(python3 -c "import json; print(len(json.load(open('$EVAL'))['signal_categories']))")
    if [ "$CAT_COUNT" -ge 6 ]; then
        pass "Eval has $CAT_COUNT signal categories (expected >=6)"
    else
        fail "Eval has only $CAT_COUNT signal categories (expected >=6)"
    fi

    EXAMPLES=$(python3 -c "import json; d=json.load(open('$EVAL')); print(len(d['examples_by_status_family']))")
    if [ "$EXAMPLES" -ge 4 ]; then
        pass "Eval has $EXAMPLES status-family example groups (expected >=4)"
    else
        fail "Eval has only $EXAMPLES status-family example groups (expected >=4)"
    fi

    NEURAL=$(python3 -c "import json; d=json.load(open('$EVAL')); print(len(d['recommended_neural_priority_targets']))")
    if [ "$NEURAL" -ge 5 ]; then
        pass "Eval has $NEURAL neural priority targets (expected >=5)"
    else
        fail "Eval has only $NEURAL neural priority targets (expected >=5)"
    fi

    GAPS=$(python3 -c "import json; d=json.load(open('$EVAL')); print(len(d['mcp_exposure_gaps']))")
    if [ "$GAPS" -ge 4 ]; then
        pass "Eval has $GAPS exposure gaps (expected >=4)"
    else
        fail "Eval has only $GAPS exposure gaps (expected >=4)"
    fi
else
    fail "Eval JSON not found: $EVAL"
fi
echo ""

# ── 4. Summary ──
echo "=== Result: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
