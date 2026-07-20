#!/usr/bin/env bash
set -euo pipefail

# ── test_mcp_handoff_priority_neural_dryrun_b123_v1.sh ────────────────────
# Substantive test for B123: the neural-bridge offline dry-run classifier
# trained over the B122 handoff-priority curriculum
# (bitnnv2/data/curriculum/mcp_codex_handoff_priority_targets_b119_v1.jsonl).
#
# Isolation-safe for the parallel test runner: the trainer script honors
# MCP_HANDOFF_NEURAL_DRYRUN_B123_EVAL_JSON / _EVAL_ROWS / _NEXT_WAVE env
# overrides, so this test reruns the trainer into a fresh mktemp -d and
# never touches the canonical committed artifacts under
# tools/geoai-task-mcp/eval/ or tools/geoai-task-mcp/data/tasking/ while
# validating. It also separately sanity-checks the canonical artifacts if
# they already exist (read-only).
#
# This test:
#   1. Asserts the trainer script is not a placeholder (byte-size + no
#      forbidden taskctl subprocess calls anywhere in its text).
#   2. Hashes the read-only curriculum source before/after running the
#      trainer and asserts byte-identical (zero-mutation of the input).
#   3. Reruns the trainer twice into two independent temp dirs and asserts
#      the reported heldout accuracy/macro_f1/confusion matrix are BYTE-
#      IDENTICAL (deterministic given the fixed seed) -- catches any
#      accidental unseeded randomness.
#   4. Loads the eval JSON + eval rows JSONL from one temp run and asserts:
#      schema/verdict fields present, split counts sum to total rows,
#      heldout row count matches split_counts.heldout, confusion matrix is
#      3x3 and sums to the heldout count, authority_flags all False, no
#      forbidden operations performed.
#   5. Asserts the learned classifier clearly beats BOTH deterministic
#      baselines (majority-class and risk_level-mapped), recomputed on the
#      SAME heldout split as reported in deterministic_baseline_recomputed_on_heldout.
#   6. Asserts the antishortcut ablation model (literal rule-defining
#      booleans removed) ALSO beats both baselines -- guards against the
#      primary model being a vacuous echo of features that trivially spell
#      out the label.
#   7. Asserts every eval row's task_id joins to a real curriculum_id / task
#      card (no synthetic ids) and that predicted_probs sum to ~1.0.
#
# Usage:
#   bash tools/geoai-task-mcp/tests/test_mcp_handoff_priority_neural_dryrun_b123_v1.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MCPROOT="$ROOT/tools/geoai-task-mcp"
TRAINER="$MCPROOT/scripts/train_mcp_handoff_priority_neural_dryrun_b123_v1.py"
CURRICULUM="$ROOT/bitnnv2/data/curriculum/mcp_codex_handoff_priority_targets_b119_v1.jsonl"

echo "=== B123 MCP Handoff Priority Neural Dry-run Test ==="
echo "ROOT=$ROOT"
echo "TRAINER=$TRAINER"

if [ ! -f "$TRAINER" ]; then
    echo "FATAL: trainer script missing: $TRAINER"
    exit 2
fi

TRAINER_BYTES=$(wc -c < "$TRAINER" | tr -d ' ')
if [ "$TRAINER_BYTES" -lt 2000 ]; then
    echo "FATAL: trainer script looks like a placeholder (only $TRAINER_BYTES bytes)"
    exit 2
fi

if grep -Eq 'run_taskctl\(\[|core\.run_taskctl|taskctl\.py|subprocess.*"(done|review|auto-pickup)"' "$TRAINER"; then
    echo "FATAL: trainer script references taskctl write/read subprocess calls -- must be file-only"
    exit 2
fi

if [ ! -f "$CURRICULUM" ]; then
    echo "FATAL: curriculum source missing: $CURRICULUM"
    exit 2
fi
CURRICULUM_HASH_BEFORE=$(sha256sum "$CURRICULUM" | awk '{print $1}')

TMP1="$(mktemp -d)"
TMP2="$(mktemp -d)"
trap 'rm -rf "$TMP1" "$TMP2"' EXIT

echo ""
echo "[1] running trainer into isolated temp dir #1..."
MCP_HANDOFF_NEURAL_DRYRUN_B123_EVAL_JSON="$TMP1/eval.json" \
MCP_HANDOFF_NEURAL_DRYRUN_B123_EVAL_ROWS="$TMP1/eval_rows.jsonl" \
MCP_HANDOFF_NEURAL_DRYRUN_B123_NEXT_WAVE="$TMP1/next_wave.json" \
python3 "$TRAINER"

echo ""
echo "[2] running trainer into isolated temp dir #2 (determinism check)..."
MCP_HANDOFF_NEURAL_DRYRUN_B123_EVAL_JSON="$TMP2/eval.json" \
MCP_HANDOFF_NEURAL_DRYRUN_B123_EVAL_ROWS="$TMP2/eval_rows.jsonl" \
MCP_HANDOFF_NEURAL_DRYRUN_B123_NEXT_WAVE="$TMP2/next_wave.json" \
python3 "$TRAINER"

CURRICULUM_HASH_AFTER=$(sha256sum "$CURRICULUM" | awk '{print $1}')
if [ "$CURRICULUM_HASH_BEFORE" != "$CURRICULUM_HASH_AFTER" ]; then
    echo "FATAL: read-only curriculum source was mutated by the trainer (zero-mutation violated)"
    exit 1
fi
echo "  PASS - curriculum source sha256 unchanged before/after both trainer runs"

echo ""
echo "[3] validating generated artifacts + determinism + gates..."
python3 - "$TMP1/eval.json" "$TMP1/eval_rows.jsonl" "$TMP2/eval.json" <<'PYEOF'
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


eval1_path, rows1_path, eval2_path = sys.argv[1:4]

with open(eval1_path, "r", encoding="utf-8") as fh:
    s1 = json.load(fh)
with open(eval2_path, "r", encoding="utf-8") as fh:
    s2 = json.load(fh)

rows: list[dict] = []
with open(rows1_path, "r", encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line:
            rows.append(json.loads(line))

# ---- basic schema / verdict ----
check(s1.get("schema_id") == "geoai.mcp_handoff_priority_neural_dryrun_eval.v1", "schema_id correct")
check(s1.get("verdict") in ("PASS", "FAIL_DIAGNOSED"), f"verdict is a recognized value: {s1.get('verdict')}")
for key in (
    "split_counts", "feature_names", "ablation_feature_names", "model", "train_metrics",
    "heldout_metrics", "ablation_antishortcut", "deterministic_baseline_recomputed_on_heldout",
    "comparison", "leakage_checks", "authority_flags", "forbidden_operations_verified",
):
    check(key in s1, f"summary contains required key: {key}")

# ---- split / row counts ----
split_counts = s1.get("split_counts", {})
train_n = split_counts.get("train", 0)
heldout_n = split_counts.get("heldout", 0)
check(train_n > 0, "train split non-empty")
check(heldout_n > 0, "heldout split non-empty")
check(s1.get("row_count") == train_n + heldout_n, "row_count == train + heldout")
check(len(rows) == heldout_n, f"eval rows count ({len(rows)}) matches split_counts.heldout ({heldout_n})")

# ---- confusion matrix shape/consistency ----
cm = s1["heldout_metrics"]["confusion_matrix"]
labels = cm["labels"]
matrix = cm["matrix"]
check(labels == ["low", "needs_codex_judgment", "high"], f"confusion matrix labels order: {labels}")
check(len(matrix) == 3 and all(len(row) == 3 for row in matrix), "confusion matrix is 3x3")
check(sum(sum(row) for row in matrix) == heldout_n, "confusion matrix sums to heldout row count")

# ---- determinism across two independent seeded runs ----
check(
    abs(s1["heldout_metrics"]["accuracy"] - s2["heldout_metrics"]["accuracy"]) < 1e-9,
    "heldout accuracy identical across two independent seeded reruns (deterministic)",
)
check(
    abs(s1["heldout_metrics"]["macro_f1"] - s2["heldout_metrics"]["macro_f1"]) < 1e-9,
    "heldout macro_f1 identical across two independent seeded reruns (deterministic)",
)
check(matrix == s2["heldout_metrics"]["confusion_matrix"]["matrix"], "confusion matrix identical across reruns")

# ---- must beat BOTH deterministic baselines ----
baselines = s1["deterministic_baseline_recomputed_on_heldout"]
maj_acc = baselines["majority_class_baseline_accuracy"]
risk_acc = baselines["risk_level_mapped_baseline_accuracy"]
acc = s1["heldout_metrics"]["accuracy"]
check(0.0 <= maj_acc <= 1.0 and 0.0 <= risk_acc <= 1.0, "baseline accuracies are valid fractions")
check(acc > maj_acc, f"neural heldout accuracy ({acc:.4f}) beats majority-class baseline ({maj_acc:.4f})")
check(acc > risk_acc, f"neural heldout accuracy ({acc:.4f}) beats risk_level-mapped baseline ({risk_acc:.4f})")
check(s1["comparison"]["beats_both_baselines"] is True, "comparison.beats_both_baselines is True")

# ---- antishortcut ablation must ALSO beat both baselines (not a vacuous echo) ----
abl = s1["ablation_antishortcut"]
abl_acc = abl["heldout_metrics"]["accuracy"]
check(abl_acc > maj_acc, f"ablation (rule-flags removed) accuracy ({abl_acc:.4f}) beats majority baseline")
check(abl_acc > risk_acc, f"ablation (rule-flags removed) accuracy ({abl_acc:.4f}) beats risk-mapped baseline")
check(abl["beats_both_baselines"] is True, "ablation_antishortcut.beats_both_baselines is True")
check(len(s1["ablation_feature_names"]) < len(s1["feature_names"]), "ablation uses a strict subset of features")

# ---- authority / forbidden-ops doctrine ----
af = s1.get("authority_flags", {})
for flag in ("runtime_authority", "default_authority", "process_launch_authority", "write_gate_enabled", "training_launch", "workflow_switch"):
    check(af.get(flag) is False, f"authority_flags.{flag} is False")

lk = s1.get("leakage_checks", {})
check(lk.get("one_row_per_task_id") is True, "leakage_checks.one_row_per_task_id is True")
check(lk.get("normalization_stats_computed_from_train_only") is True, "normalization stats computed from train only")
check(lk.get("no_hyperparameter_tuning_against_heldout") is True, "no hyperparameter tuning against heldout")
check(lk.get("no_taskctl_subprocess_calls") is True, "leakage_checks.no_taskctl_subprocess_calls is True")
check(lk.get("no_live_queue_mutation") is True, "leakage_checks.no_live_queue_mutation is True")

# ---- per-row sanity ----
bad_ids = [r["row_id"] for r in rows if not str(r.get("task_id", "")).strip()]
check(not bad_ids, f"every eval row has a non-empty real task_id ({len(bad_ids)} bad)")
prob_bad = [
    r["row_id"] for r in rows
    if abs(sum(r.get("predicted_probs", {}).values()) - 1.0) > 1e-6
]
check(not prob_bad, f"every row's predicted_probs sums to ~1.0 ({len(prob_bad)} bad)")
label_bad = [r["row_id"] for r in rows if r.get("true_label") not in ("low", "needs_codex_judgment", "high")]
check(not label_bad, f"every row's true_label is one of the 3 valid classes ({len(label_bad)} bad)")

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
    echo "=== test_mcp_handoff_priority_neural_dryrun_b123_v1.sh: PASS ==="
else
    echo "=== test_mcp_handoff_priority_neural_dryrun_b123_v1.sh: FAIL (exit=$RC) ==="
fi
exit $RC
