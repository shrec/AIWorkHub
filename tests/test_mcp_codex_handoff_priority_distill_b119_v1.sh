#!/usr/bin/env bash
set -euo pipefail

# ── test_mcp_codex_handoff_priority_distill_b119_v1.sh ───────────────────
# Substantive test for the B122 repair of the REJECTED B121/B119 Codex
# handoff-priority distillation (that attempt was a placeholder builder +
# echo-only test using a deterministic risk score as ground truth).
#
# This test:
#   1. Asserts neither the builder script nor this test file are still the
#      old one-line placeholder content.
#   2. Hashes the REAL source registry (bitnnv2/data/tasking/
#      machine_task_cards_v1.jsonl) before running the builder, runs the
#      builder with ZERO positional arguments, then hashes it again and
#      asserts byte-identical (the builder only READS the registry; it is
#      not in allowed_writes and must never be mutated -- zero-mutation
#      proof, no queue mutation).
#   3. Loads the generated eval JSON / eval rows JSONL / curriculum JSONL
#      and asserts non-trivial invariants: non-empty, row counts agree
#      across files, schema fields present on every row, split counts sum
#      to total, and BOTH heldout and train buckets are non-empty.
#   4. Asserts the label distribution is NOT constant/degenerate: all three
#      classes (high, needs_codex_judgment, low) have at least one real
#      example, and no single class is the entirety of the data.
#   5. Asserts the label is NOT an echo of the packaged deterministic
#      risk_level score: recomputes the risk_level-mapped-baseline accuracy
#      directly from the rows and checks it is (a) strictly less than 1.0
#      (i.e. real disagreement exists -- not an echo) and (b) matches the
#      value the builder itself reported in the eval JSON (internal
#      consistency, not a hardcoded number).
#   6. Asserts no taskctl subprocess/write commands appear anywhere in the
#      builder script text (it must derive everything from the on-disk
#      registry + review_summarizer's pure functions, never invoke taskctl).
#
# Isolation-safe: only reads the shared registry (never writes it); the
# artifacts this test writes are all within this task's allowed_writes.
#
# Usage:
#   bash tools/geoai-task-mcp/tests/test_mcp_codex_handoff_priority_distill_b119_v1.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
BUILDER="$MCPROOT/scripts/build_mcp_codex_handoff_priority_distill_b119_v1.py"
EVAL_JSON="$MCPROOT/eval/mcp_codex_handoff_priority_distill_b119_v1.json"
EVAL_ROWS="$MCPROOT/eval/mcp_codex_handoff_priority_distill_rows_b119_v1.jsonl"
CURRICULUM="$ROOT/bitnnv2/data/curriculum/mcp_codex_handoff_priority_targets_b119_v1.jsonl"
SOURCE_REGISTRY="$ROOT/bitnnv2/data/tasking/machine_task_cards_v1.jsonl"

export PYTHONPATH="$MCPROOT/src"
export GEOAI_REPO="$ROOT"
export GEOAI_TASK_MCP_ALLOW_WRITES="${GEOAI_TASK_MCP_ALLOW_WRITES:-0}"

echo "=== B122 MCP Codex Handoff Priority Distill (real) Test ==="
echo "ROOT=$ROOT"
echo "BUILDER=$BUILDER"

if [ "$GEOAI_TASK_MCP_ALLOW_WRITES" != "0" ]; then
    echo "FATAL: GEOAI_TASK_MCP_ALLOW_WRITES must be 0, got '$GEOAI_TASK_MCP_ALLOW_WRITES'"
    exit 2
fi

if [ ! -f "$BUILDER" ]; then
    echo "FATAL: builder script missing: $BUILDER"
    exit 2
fi

BUILDER_BYTES=$(wc -c < "$BUILDER" | tr -d ' ')
if [ "$BUILDER_BYTES" -lt 2000 ]; then
    echo "FATAL: builder script looks like a placeholder (only $BUILDER_BYTES bytes)"
    exit 2
fi
if grep -q "^# Build script placeholder\$" "$BUILDER"; then
    echo "FATAL: builder script still contains the rejected placeholder line"
    exit 2
fi

TEST_BYTES=$(wc -c < "$MCPROOT/tests/test_mcp_codex_handoff_priority_distill_b119_v1.sh" | tr -d ' ')
if [ "$TEST_BYTES" -lt 2000 ]; then
    echo "FATAL: this test file itself looks like a placeholder ($TEST_BYTES bytes)"
    exit 2
fi

if grep -Eq 'run_taskctl\(\[|core\.run_taskctl|"done"|"review"|"auto-pickup"' "$BUILDER"; then
    echo "FATAL: builder script references taskctl write/read subprocess calls -- must be registry-file-only"
    exit 2
fi

if [ ! -f "$SOURCE_REGISTRY" ]; then
    echo "FATAL: real source registry missing: $SOURCE_REGISTRY"
    exit 2
fi
REGISTRY_HASH_BEFORE=$(sha256sum "$SOURCE_REGISTRY" | awk '{print $1}')

echo ""
echo "[1] running builder with ZERO positional arguments..."
python3 "$BUILDER"
BUILD_RC=$?
if [ $BUILD_RC -ne 0 ]; then
    echo "FATAL: builder exited $BUILD_RC"
    exit $BUILD_RC
fi

REGISTRY_HASH_AFTER=$(sha256sum "$SOURCE_REGISTRY" | awk '{print $1}')
if [ "$REGISTRY_HASH_BEFORE" != "$REGISTRY_HASH_AFTER" ]; then
    echo "FATAL: real source registry was mutated by the builder (zero-mutation violated)"
    exit 1
fi
echo "  PASS - real source registry sha256 unchanged before/after builder run"

echo ""
echo "[2] validating generated artifacts..."
python3 - "$EVAL_JSON" "$EVAL_ROWS" "$CURRICULUM" <<'PYEOF'
from __future__ import annotations

import json
import sys

FAILURES: list[str] = []
PASSES: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        PASSES.append(label)
        print(f"  PASS - {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL - {label}")


eval_json_path, eval_rows_path, curriculum_path = sys.argv[1:4]

with open(eval_json_path, "r", encoding="utf-8") as fh:
    summary = json.load(fh)

rows: list[dict] = []
with open(eval_rows_path, "r", encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line:
            rows.append(json.loads(line))

curriculum_rows: list[dict] = []
with open(curriculum_path, "r", encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line:
            curriculum_rows.append(json.loads(line))

# ---- basic non-empty / verdict ----
check(summary.get("verdict") == "PASS", "eval summary verdict == PASS")
check(len(rows) > 500, f"eval rows non-trivial count ({len(rows)} > 500)")
check(len(rows) == summary.get("row_count"), "eval rows count matches summary.row_count")
check(len(curriculum_rows) == len(rows), "curriculum row count matches eval row count")

for key in ("class_balance", "split_counts", "feature_coverage", "leakage_checks", "deterministic_baseline", "label_rule"):
    check(key in summary, f"summary contains required key: {key}")

# ---- split ----
split_counts = summary.get("split_counts", {})
train_n = split_counts.get("train", 0)
heldout_n = split_counts.get("heldout", 0)
check(train_n > 0, "train split non-empty")
check(heldout_n > 0, "heldout split non-empty")
check(train_n + heldout_n == len(rows), "train + heldout counts sum to total rows")
heldout_frac = heldout_n / len(rows) if rows else 0.0
check(0.05 <= heldout_frac <= 0.40, f"heldout fraction plausible ({heldout_frac:.3f} in [0.05, 0.40])")

# ---- class balance non-degenerate ----
labels_seen = {r.get("expected_priority_label") for r in rows}
check(labels_seen == {"high", "needs_codex_judgment", "low"}, f"all 3 classes present: {labels_seen}")
counts = summary.get("class_balance", {}).get("counts", {})
total = sum(counts.values()) or 1
check(all(v > 0 for v in counts.values()), f"every class has >=1 example: {counts}")
max_frac = max(counts.values()) / total
check(max_frac < 0.90, f"no single class dominates (max fraction {max_frac:.3f} < 0.90)")

# ---- positive/negative/needs_codex_judgment concrete examples ----
by_label: dict[str, dict] = {}
for r in rows:
    lbl = r.get("expected_priority_label")
    if lbl not in by_label:
        by_label[lbl] = r
for lbl in ("high", "needs_codex_judgment", "low"):
    check(lbl in by_label, f"a concrete example row exists for class '{lbl}'")

# ---- per-row schema fields ----
required_row_keys = {
    "task_id", "runner", "topic", "risk_features", "allowed_writes_shape",
    "validation_commands", "expected_priority_label", "label_rule_id",
    "label_basis", "split", "source", "provenance",
}
missing_any = [r["row_id"] for r in rows if not required_row_keys.issubset(r.keys())]
check(not missing_any, f"all rows have required schema fields (missing on {len(missing_any)} rows)")

required_risk_feature_keys = {
    "risk_level", "risks", "commit_hygiene_status", "blocked_review_failed",
    "stale_flag", "stale_hours", "review_wait_flag", "review_wait_hours",
    "missing_validation", "shared_file_write", "wide_write_scope",
    "weak_evidence_count", "strong_evidence",
}
rf_missing = [
    r["row_id"] for r in rows
    if not required_risk_feature_keys.issubset(r.get("risk_features", {}).keys())
]
check(not rf_missing, f"all rows have required risk_features keys (missing on {len(rf_missing)} rows)")

split_values_ok = {r.get("split") for r in rows} == {"train", "heldout"}
check(split_values_ok, "row split values are exactly {train, heldout}")

label_values_ok = all(
    r.get("expected_priority_label") in ("high", "needs_codex_judgment", "low") for r in rows
)
check(label_values_ok, "every row's expected_priority_label is one of the 3 valid classes")

# ---- real join / provenance: task_id must exist and be non-synthetic ----
non_real_ids = [r["row_id"] for r in rows if not str(r.get("task_id", "")).strip() or r["task_id"].startswith("unknown_")]
check(not non_real_ids, f"every row joins to a real task_id (no synthetic/unknown ids: {len(non_real_ids)} bad)")
unique_ids = {r["task_id"] for r in rows}
check(len(unique_ids) == len(rows), "one row per unique real task_id (no duplicate join)")

# ---- NOT an echo of the deterministic risk_level score ----
risk_to_label = {"high": "high", "medium": "needs_codex_judgment", "low": "low"}
agree = sum(
    1 for r in rows
    if risk_to_label.get(r["risk_features"]["risk_level"], "low") == r["expected_priority_label"]
)
recomputed_baseline_acc = agree / len(rows) if rows else 0.0
reported_baseline_acc = summary["deterministic_baseline"]["risk_level_mapped_baseline_accuracy"]
check(
    abs(recomputed_baseline_acc - reported_baseline_acc) < 1e-9,
    f"recomputed risk_level-mapped baseline accuracy matches reported ({recomputed_baseline_acc:.6f})",
)
check(
    recomputed_baseline_acc < 0.999,
    f"label is NOT an echo of risk_level (baseline accuracy {recomputed_baseline_acc:.3f} < 0.999, real disagreement exists)",
)
disagree_count = len(rows) - agree
check(disagree_count > 50, f"substantial real disagreement between risk_level and label ({disagree_count} rows)")

# ---- label_basis is not vacuous for high/needs_codex_judgment rows ----
non_low_rows = [r for r in rows if r["expected_priority_label"] != "low"]
vacuous_basis = [r["row_id"] for r in non_low_rows if not r.get("label_basis")]
check(not vacuous_basis, f"every non-'low' row documents a non-empty label_basis ({len(vacuous_basis)} vacuous)")

low_rows = [r for r in rows if r["expected_priority_label"] == "low"]
nonempty_basis_on_low = [r["row_id"] for r in low_rows if r.get("label_basis")]
check(not nonempty_basis_on_low, "'low' rows have an empty label_basis (no evidence == low, by construction)")

# ---- leakage checks reported and true ----
lk = summary.get("leakage_checks", {})
check(lk.get("one_row_per_task_id") is True, "leakage_checks.one_row_per_task_id is True")
check(lk.get("no_taskctl_subprocess_calls") is True, "leakage_checks.no_taskctl_subprocess_calls is True")
check(lk.get("no_live_queue_mutation") is True, "leakage_checks.no_live_queue_mutation is True")

# ---- authority flags stay off (evidence/baseline only, no runtime authority) ----
af = summary.get("authority_flags", {})
check(af.get("runtime_authority") is False, "authority_flags.runtime_authority False")
check(af.get("write_gate_enabled") is False, "authority_flags.write_gate_enabled False")
check(af.get("training_launch") is False, "authority_flags.training_launch False")

# ---- curriculum rows carry the same label + safety doctrine fields ----
curr_missing = [
    c["curriculum_id"] for c in curriculum_rows
    if "labels" not in c or "expected_priority_label" not in c["labels"] or "safety" not in c
]
check(not curr_missing, f"all curriculum rows have labels+safety blocks (missing on {len(curr_missing)})")
curr_labels_seen = {c["labels"]["expected_priority_label"] for c in curriculum_rows}
check(curr_labels_seen == {"high", "needs_codex_judgment", "low"}, "curriculum rows also cover all 3 classes")
safety_bad = [
    c["curriculum_id"] for c in curriculum_rows
    if c["safety"].get("neural_is_priority_routing_only_not_authority") is not True
    or c["safety"].get("queue_mutation") is not False
]
check(not safety_bad, f"curriculum safety doctrine flags correct ({len(safety_bad)} bad rows)")

print("")
print(f"PASS={len(PASSES)} FAIL={len(FAILURES)}")
if FAILURES:
    print("FAILURES:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
PYEOF
RC=$?

echo ""
if [ $RC -eq 0 ]; then
    echo "=== test_mcp_codex_handoff_priority_distill_b119_v1.sh: PASS ==="
else
    echo "=== test_mcp_codex_handoff_priority_distill_b119_v1.sh: FAIL (exit=$RC) ==="
fi
exit $RC
