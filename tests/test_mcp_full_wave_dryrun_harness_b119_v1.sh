#!/usr/bin/env bash
# B122 real repair: exercises the full-wave dryrun harness against the REAL
# live task queue (read-only) plus 2 isolated negative fixtures. Never
# mutates the production queue, never launches real auto-pickup/done.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES=0

echo "=== MCP Full Wave Dryrun Harness (B122 real repair) ==="

BUILD_SCRIPT="$MCPROOT/scripts/build_mcp_full_wave_dryrun_harness_b119_v1.py"
EVAL_OUT="$MCPROOT/eval/mcp_full_wave_dryrun_harness_b119_v1.json"
ROWS_OUT="$MCPROOT/eval/mcp_full_wave_dryrun_harness_rows_b119_v1.jsonl"
NEXT_WAVE_OUT="$MCPROOT/data/tasking/mcp_full_wave_harness_real_repair_next_wave_b122_v1.json"

REAL_DB="$ROOT/bitnnv2/data/tasking/task_queue_v1.sqlite"
REAL_CARDS="$ROOT/bitnnv2/data/tasking/machine_task_cards_v1.jsonl"

TMPDIR_TEST="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_TEST"' EXIT

# --- 1. Missing-queue negative fixture (isolated sandbox, never the real
#        queue): the harness must FAIL, never emit a not_found==not_found
#        vacuous passing hash (the exact B121 rejection reason). ---
MISSING_EVAL="$TMPDIR_TEST/missing_eval.json"
MISSING_NEXT="$TMPDIR_TEST/missing_next.json"
set +e
BITNN_TASK_QUEUE_DB="$TMPDIR_TEST/nonexistent_queue.sqlite" \
BITNN_TASK_CARDS_PATH="$TMPDIR_TEST/nonexistent_cards.jsonl" \
BITNN_TASK_CARDS_MANIFEST="$TMPDIR_TEST/nonexistent_manifest.json" \
  python3 "$BUILD_SCRIPT" "$MISSING_EVAL" "$MISSING_NEXT"
MISSING_RC=$?
set -e
if [ "$MISSING_RC" -eq 0 ]; then
  echo "FAIL: harness exited 0 with a missing parent queue (should FAIL)"; exit 1
fi
MISSING_VERDICT=$(python3 -c "import json; print(json.load(open('$MISSING_EVAL'))['verdict'])")
if [ "$MISSING_VERDICT" != "FAIL" ]; then
  echo "FAIL: missing-queue fixture verdict=$MISSING_VERDICT expected FAIL"; exit 1
fi
MISSING_SHA=$(python3 -c "import json; print(json.load(open('$MISSING_EVAL')).get('parent_queue_sha_before'))")
if [ "$MISSING_SHA" = "not_found" ]; then
  echo "FAIL: missing-queue fixture still uses the B121 not_found passing-hash bug"; exit 1
fi
echo "PASS: missing parent queue path correctly fails (not a not_found passing hash)."

# --- 2. Reject-old-B121-artifact regression: recreate the exact hollow
#        B121 shape and confirm our own validation logic rejects it. ---
OLD_B121_FIXTURE="$TMPDIR_TEST/old_b121_style.json"
cat > "$OLD_B121_FIXTURE" <<'EOF'
{
  "eval_id": "mcp_full_wave_dryrun_harness_b119_v1",
  "verdict": "PASS",
  "checks_total": 4,
  "checks_passed": 4,
  "parent_queue_sha_before": "not_found",
  "parent_queue_sha_after": "not_found",
  "collision_preflight": {"pass": true, "collisions": 0},
  "go_no_go": true
}
EOF
set +e
python3 - "$OLD_B121_FIXTURE" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
ok = True
if d.get("parent_queue_sha_before") == "not_found":
    ok = False
if not any(isinstance(d.get(k), dict) and d.get(k) for k in ("gates", "acceptance_results", "invariants_verified")):
    ok = False
sys.exit(0 if not ok else 1)
PYEOF
OLD_FIXTURE_REJECTED_RC=$?
set -e
if [ "$OLD_FIXTURE_REJECTED_RC" -ne 0 ]; then
  echo "FAIL: validator did not reject the B121-style not_found/hardcoded artifact"; exit 1
fi
echo "PASS: B121-style hollow artifact is correctly rejected by validation."

# --- 3. REAL run against the REAL live queue (read-only). Byte-diff the
#        actual production inputs before/after as corroboration on top of
#        the harness's own internal mode=ro write-rejection probe. ---
if [ ! -f "$REAL_DB" ]; then echo "FAIL: real queue DB not found at $REAL_DB"; exit 1; fi
if [ ! -f "$REAL_CARDS" ]; then echo "FAIL: real cards jsonl not found at $REAL_CARDS"; exit 1; fi
SHA_DB_BEFORE=$(sha256sum "$REAL_DB" | awk '{print $1}')
SHA_CARDS_BEFORE=$(sha256sum "$REAL_CARDS" | awk '{print $1}')

python3 "$BUILD_SCRIPT" "$EVAL_OUT" "$NEXT_WAVE_OUT"

SHA_DB_AFTER=$(sha256sum "$REAL_DB" | awk '{print $1}')
SHA_CARDS_AFTER=$(sha256sum "$REAL_CARDS" | awk '{print $1}')

if [ "$SHA_DB_BEFORE" != "$SHA_DB_AFTER" ]; then
  echo "NOTE: real queue DB bytes changed during the run (expected under concurrent workers; this harness never wrote to it -- see readonly_connection_write_rejected gate)"
fi
if [ "$SHA_CARDS_BEFORE" != "$SHA_CARDS_AFTER" ]; then
  echo "NOTE: real cards jsonl bytes changed during the run (expected under concurrent workers)"
fi

if [ ! -s "$EVAL_OUT" ]; then echo "FAIL: EVAL_OUT is empty"; exit 1; fi
if [ ! -s "$ROWS_OUT" ]; then echo "FAIL: ROWS_OUT is empty"; exit 1; fi
if [ ! -s "$NEXT_WAVE_OUT" ]; then echo "FAIL: NEXT_WAVE_OUT is empty"; exit 1; fi

# --- 4. Strict non-vacuous content assertions on the REAL artifact. ---
python3 - "$EVAL_OUT" "$ROWS_OUT" "$REAL_CARDS" <<'PYEOF'
import json, sys

eval_path, rows_path, real_cards_path = sys.argv[1:4]
d = json.load(open(eval_path))

assert d.get("verdict") == "PASS", f"verdict={d.get('verdict')}"
assert d.get("go_no_go") is True

for key in ("gates", "acceptance_results", "invariants_verified"):
    assert isinstance(d.get(key), dict) and len(d[key]) > 0, f"missing/empty metric container: {key}"

sha_before = d.get("parent_queue_sha_before")
sha_after = d.get("parent_queue_sha_after")
assert sha_before and sha_before != "not_found" and len(sha_before) == 64, f"bad sha_before={sha_before!r}"
assert sha_after and sha_after != "not_found" and len(sha_after) == 64, f"bad sha_after={sha_after!r}"

gates = d["gates"]
for g in ("parent_queue_present", "readonly_connection_write_rejected",
          "collision_guard_prevents_double_claim", "stale_manifest_detector_has_teeth",
          "write_gate_detector_has_teeth", "real_write_gate_held_this_run"):
    assert gates.get(g) is True, f"gate {g} not True: {gates.get(g)!r}"

qs = d.get("queue_source", {})
assert qs.get("total_cards_loaded", 0) > 0, "total_cards_loaded must be a real positive count"
assert qs.get("db_exists") is True and qs.get("cards_jsonl_exists") is True

collision = d.get("collision_fixture", {})
assert collision.get("applicable") is True
real_task_id = collision.get("basis_task_id_real")
assert real_task_id, "collision fixture missing a real basis task_id"

# Cross-check: that task_id must actually be present in the live cards
# JSONL on disk right now -- proves it was pulled from the real queue at
# run time, not typed in as a string-literal constant.
found = False
with open(real_cards_path, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("task_id") == real_task_id:
            found = True
            break
assert found, f"basis_task_id_real={real_task_id!r} not found in live {real_cards_path}"

stale = d.get("stale_manifest_fixture", {})
assert stale.get("synthetic_corruption_detected_stale") is True
assert stale.get("synthetic_identical_state_detected_fresh") is True

wg = d.get("write_gate_bypass_fixture", {})
assert wg.get("synthetic_bypass_env_detected") is True
assert wg.get("synthetic_clean_env_detected_as_clean") is True
assert wg.get("real_bypass_detected") is False

rows = [json.loads(l) for l in open(rows_path, encoding="utf-8") if l.strip()]
assert len(rows) > 0, "rows JSONL must be non-empty"
assert any(r.get("basis_task_id_real") == real_task_id for r in rows if r.get("kind") == "collision_fixture"), \
    "collision_fixture row missing from rows JSONL or task_id mismatch"

# Not-a-trivially-constant-fixture check: checks_total/checks_passed must
# equal the real len(gates)/sum(gates), not a hardcoded 4/4 pair.
assert d.get("checks_total") == len(gates), f"checks_total={d.get('checks_total')} != len(gates)={len(gates)}"
assert d.get("checks_passed") == sum(1 for v in gates.values() if v)

print("PASS: real artifact content is non-vacuous and internally consistent.")
PYEOF

echo "PASS: test complete."
