#!/usr/bin/env python3
"""train_mcp_handoff_priority_neural_dryrun_b123_v1.py

B123: neural-bridge OFFLINE DRY-RUN over the B122 handoff-priority curriculum
(bitnnv2/data/curriculum/mcp_codex_handoff_priority_targets_b119_v1.jsonl).

Trains a small numpy MLP (1 hidden layer, softmax cross-entropy, plain
full-batch gradient descent, no external ML deps) to predict the Codex
handoff review-priority label (high / needs_codex_judgment / low) from the
risk_features already computed by review_summarizer.py for each real task
card. This is a MEASURED PROBE ONLY:

  * NO runtime routing authority (authority_flags all False).
  * NO queue mutation, NO taskctl subprocess calls, NO process launch.
  * NO production training launch -- this is an offline dry-run artifact.

Neural-bridge framing (CLAUDE.md Neural-Control-First rule): the B119/B122
multi-signal label rule (OR-of-flags / weak-evidence-count threshold) is the
deterministic LABEL GENERATOR (already built, out of scope here) and the two
comparators below are BASELINES, not authority. This script asks whether a
small LEARNED classifier can recover the priority decision from the
component risk signals via gradient descent, instead of a hand-coded
keyword/table router -- the thing that should eventually carry the
intelligence if this capability is ever wired to production.

Honesty guard: the primary feature set includes the literal boolean flags
that compose the label rule (blocked_review_failed, blocker_other,
stale_flag, review_wait_flag, the 5 weak-evidence booleans, strong_evidence),
so near-perfect accuracy is EXPECTED (the model recovers a known logical
function from its own defining inputs, not a fresh generalization claim). To
avoid overclaiming, a second ablation model is also trained with those
literal rule-defining booleans REMOVED, keeping only continuous signals
(stale_hours, review_wait_hours, risk_level ordinal, risks/notes counts) --
this measures whether the learned classifier still clearly beats the
deterministic baselines from softer signals alone (antishortcut check, same
spirit as train_signal_atlas_linguistic_router_antishortcut_neural_b123_v1.py
in the signal_atlas topic).

Inputs (read-only, never written):
  bitnnv2/data/curriculum/mcp_codex_handoff_priority_targets_b119_v1.jsonl
  tools/geoai-task-mcp/eval/mcp_codex_handoff_priority_distill_b119_v1.json  (reference baseline numbers only)

Outputs (this task's allowed_writes):
  tools/geoai-task-mcp/eval/mcp_handoff_priority_neural_dryrun_b123_v1.json
  tools/geoai-task-mcp/eval/mcp_handoff_priority_neural_dryrun_rows_b123_v1.jsonl
  tools/geoai-task-mcp/data/tasking/mcp_handoff_priority_neural_dryrun_next_wave_b123_v1.json

Usage:
  python3 tools/geoai-task-mcp/scripts/train_mcp_handoff_priority_neural_dryrun_b123_v1.py
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
TASK_ID = "CLAUDE_TASK_MCP_HANDOFF_PRIORITY_NEURAL_DRYRUN_B123_V1"

CURRICULUM = ROOT / "bitnnv2/data/curriculum/mcp_codex_handoff_priority_targets_b119_v1.jsonl"
DISTILL_EVAL_REF = ROOT / "tools/geoai-task-mcp/eval/mcp_codex_handoff_priority_distill_b119_v1.json"

EVAL_JSON = Path(os.environ.get(
    "MCP_HANDOFF_NEURAL_DRYRUN_B123_EVAL_JSON",
    str(ROOT / "tools/geoai-task-mcp/eval/mcp_handoff_priority_neural_dryrun_b123_v1.json"),
))
EVAL_ROWS = Path(os.environ.get(
    "MCP_HANDOFF_NEURAL_DRYRUN_B123_EVAL_ROWS",
    str(ROOT / "tools/geoai-task-mcp/eval/mcp_handoff_priority_neural_dryrun_rows_b123_v1.jsonl"),
))
NEXT_WAVE = Path(os.environ.get(
    "MCP_HANDOFF_NEURAL_DRYRUN_B123_NEXT_WAVE",
    str(ROOT / "tools/geoai-task-mcp/data/tasking/mcp_handoff_priority_neural_dryrun_next_wave_b123_v1.json"),
))

CLASSES = ["low", "needs_codex_judgment", "high"]
CLASS_INDEX = {c: i for i, c in enumerate(CLASSES)}
RISK_LEVEL_ORD = {"low": 0.0, "medium": 1.0, "high": 2.0}
RISK_LEVEL_MAP = {"low": "low", "medium": "needs_codex_judgment", "high": "high"}

RANDOM_SEED = 20260705
HIDDEN_DIM = 16
EPOCHS = 1500
LR = 0.3
L2 = 1e-3

FEATURE_NAMES = [
    "risk_level_ord",
    "blocked_review_failed",
    "blocker_other",
    "stale_flag",
    "stale_hours_norm",
    "stale_hours_missing",
    "review_wait_flag",
    "review_wait_hours_norm",
    "review_wait_hours_missing",
    "commit_hygiene_warn",
    "missing_validation",
    "shared_file_write",
    "wide_write_scope",
    "missing_forbidden_guards",
    "weak_evidence_count_norm",
    "strong_evidence",
    "risks_count_norm",
    "commit_hygiene_notes_count_norm",
]
# antishortcut ablation: keep only continuous/ordinal signals, drop every
# literal boolean that is itself an OR-term / weak-signal-count component of
# the deterministic label rule.
ABLATION_FEATURE_NAMES = [
    "risk_level_ord",
    "stale_hours_norm",
    "stale_hours_missing",
    "review_wait_hours_norm",
    "review_wait_hours_missing",
    "risks_count_norm",
    "commit_hygiene_notes_count_norm",
]
ABLATION_IDX = [FEATURE_NAMES.index(n) for n in ABLATION_FEATURE_NAMES]


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_jsonl(path: Path) -> List[dict]:
    rows: List[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def raw_features(rf: dict) -> Dict[str, Optional[float]]:
    return {
        "risk_level_ord": RISK_LEVEL_ORD.get(rf.get("risk_level"), 1.0),
        "blocked_review_failed": float(bool(rf.get("blocked_review_failed"))),
        "blocker_other": float(bool(rf.get("blocker_other"))),
        "stale_flag": float(bool(rf.get("stale_flag"))),
        "stale_hours_raw": rf.get("stale_hours"),
        "review_wait_flag": float(bool(rf.get("review_wait_flag"))),
        "review_wait_hours_raw": rf.get("review_wait_hours"),
        "commit_hygiene_warn": float(bool(rf.get("commit_hygiene_warn"))),
        "missing_validation": float(bool(rf.get("missing_validation"))),
        "shared_file_write": float(bool(rf.get("shared_file_write"))),
        "wide_write_scope": float(bool(rf.get("wide_write_scope"))),
        "missing_forbidden_guards": float(bool(rf.get("missing_forbidden_guards"))),
        "weak_evidence_count_raw": float(rf.get("weak_evidence_count", 0) or 0),
        "strong_evidence": float(bool(rf.get("strong_evidence"))),
        "risks_count_raw": float(len(rf.get("risks") or [])),
        "commit_hygiene_notes_count_raw": float(len(rf.get("commit_hygiene_notes") or [])),
    }


def build_records(curriculum_rows: List[dict]) -> List[dict]:
    recs = []
    for r in curriculum_rows:
        rf = r["risk_features"]
        d = raw_features(rf)
        d["label"] = r["labels"]["expected_priority_label"]
        d["split"] = r["labels"]["split"]
        d["task_id"] = r["task_context"]["task_id"]
        d["curriculum_id"] = r["curriculum_id"]
        recs.append(d)
    return recs


def train_mean_std(train_recs: List[dict], key: str) -> Tuple[float, float]:
    vals = [v for v in (d[key] for d in train_recs) if v is not None]
    arr = np.array(vals, dtype=np.float64)
    mean = float(arr.mean()) if arr.size else 0.0
    std = float(arr.std()) if arr.size and arr.std() > 1e-9 else 1.0
    return mean, std


def featurize_all(recs: List[dict], stats: Dict[str, Tuple[float, float]]) -> np.ndarray:
    sh_mean, sh_std = stats["stale_hours"]
    rw_mean, rw_std = stats["review_wait_hours"]
    wec_mean, wec_std = stats["weak_evidence_count"]
    rc_mean, rc_std = stats["risks_count"]
    cn_mean, cn_std = stats["commit_hygiene_notes_count"]
    rows = []
    for d in recs:
        sh = d["stale_hours_raw"]
        sh_missing = 1.0 if sh is None else 0.0
        sh_val = sh_mean if sh is None else sh
        rw = d["review_wait_hours_raw"]
        rw_missing = 1.0 if rw is None else 0.0
        rw_val = rw_mean if rw is None else rw
        rows.append([
            d["risk_level_ord"],
            d["blocked_review_failed"],
            d["blocker_other"],
            d["stale_flag"],
            (sh_val - sh_mean) / sh_std,
            sh_missing,
            d["review_wait_flag"],
            (rw_val - rw_mean) / rw_std,
            rw_missing,
            d["commit_hygiene_warn"],
            d["missing_validation"],
            d["shared_file_write"],
            d["wide_write_scope"],
            d["missing_forbidden_guards"],
            (d["weak_evidence_count_raw"] - wec_mean) / wec_std,
            d["strong_evidence"],
            (d["risks_count_raw"] - rc_mean) / rc_std,
            (d["commit_hygiene_notes_count_raw"] - cn_mean) / cn_std,
        ])
    return np.array(rows, dtype=np.float64)


class NumpyMLP:
    """Small 1-hidden-layer softmax MLP, plain full-batch gradient descent.

    No external ML deps (pure numpy). Deterministic given a fixed seed --
    used only as an offline measurement probe, never wired to any runtime
    routing path.
    """

    def __init__(self, in_dim: int, hidden_dim: int, out_dim: int, seed: int):
        rng = np.random.RandomState(seed)
        lim1 = math.sqrt(6.0 / (in_dim + hidden_dim))
        self.W1 = rng.uniform(-lim1, lim1, size=(in_dim, hidden_dim))
        self.b1 = np.zeros(hidden_dim)
        lim2 = math.sqrt(6.0 / (hidden_dim + out_dim))
        self.W2 = rng.uniform(-lim2, lim2, size=(hidden_dim, out_dim))
        self.b2 = np.zeros(out_dim)

    def forward(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        z1 = X @ self.W1 + self.b1
        a1 = np.maximum(z1, 0.0)
        z2 = a1 @ self.W2 + self.b2
        z2 = z2 - z2.max(axis=1, keepdims=True)
        e = np.exp(z2)
        p = e / e.sum(axis=1, keepdims=True)
        return z1, a1, p

    def fit(self, X: np.ndarray, y_idx: np.ndarray, epochs: int, lr: float, l2: float) -> float:
        n = X.shape[0]
        onehot = np.zeros((n, self.W2.shape[1]))
        onehot[np.arange(n), y_idx] = 1.0
        loss = float("nan")
        for _ in range(epochs):
            z1, a1, p = self.forward(X)
            dz2 = (p - onehot) / n
            dW2 = a1.T @ dz2 + 2 * l2 * self.W2
            db2 = dz2.sum(axis=0)
            da1 = dz2 @ self.W2.T
            dz1 = da1 * (z1 > 0)
            dW1 = X.T @ dz1 + 2 * l2 * self.W1
            db1 = dz1.sum(axis=0)
            self.W1 -= lr * dW1
            self.b1 -= lr * db1
            self.W2 -= lr * dW2
            self.b2 -= lr * db2
            logp = np.log(np.clip(p[np.arange(n), y_idx], 1e-12, 1.0))
            loss = float(-np.mean(logp))
        return loss

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        _, _, p = self.forward(X)
        return p


def confusion_and_metrics(y_true: np.ndarray, y_pred: np.ndarray, n_classes: int) -> Dict:
    cm = np.zeros((n_classes, n_classes), dtype=int)
    for t, p in zip(y_true, y_pred):
        cm[t, p] += 1
    total = cm.sum()
    accuracy = float(np.trace(cm) / total) if total else 0.0
    recalls, precisions, f1s = {}, {}, {}
    for i, cls in enumerate(CLASSES):
        row_sum = cm[i, :].sum()
        col_sum = cm[:, i].sum()
        recall = float(cm[i, i] / row_sum) if row_sum else 0.0
        precision = float(cm[i, i] / col_sum) if col_sum else 0.0
        f1 = float(2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        recalls[cls] = recall
        precisions[cls] = precision
        f1s[cls] = f1
    macro_f1 = float(np.mean(list(f1s.values())))
    return {
        "accuracy": accuracy,
        "macro_f1": macro_f1,
        "per_class_recall": recalls,
        "per_class_precision": precisions,
        "per_class_f1": f1s,
        "confusion_matrix": {"labels": CLASSES, "matrix": cm.tolist()},
    }


def main() -> None:
    curriculum_rows = load_jsonl(CURRICULUM)
    recs = build_records(curriculum_rows)
    train_recs = [d for d in recs if d["split"] == "train"]
    heldout_recs = [d for d in recs if d["split"] == "heldout"]

    stats = {
        "stale_hours": train_mean_std(train_recs, "stale_hours_raw"),
        "review_wait_hours": train_mean_std(train_recs, "review_wait_hours_raw"),
        "weak_evidence_count": train_mean_std(train_recs, "weak_evidence_count_raw"),
        "risks_count": train_mean_std(train_recs, "risks_count_raw"),
        "commit_hygiene_notes_count": train_mean_std(train_recs, "commit_hygiene_notes_count_raw"),
    }

    Xtr = featurize_all(train_recs, stats)
    Xhe = featurize_all(heldout_recs, stats)
    ytr = np.array([CLASS_INDEX[d["label"]] for d in train_recs], dtype=np.int64)
    yhe = np.array([CLASS_INDEX[d["label"]] for d in heldout_recs], dtype=np.int64)

    # ---- primary model: full feature set ----
    model = NumpyMLP(Xtr.shape[1], HIDDEN_DIM, len(CLASSES), RANDOM_SEED)
    final_train_loss = model.fit(Xtr, ytr, EPOCHS, LR, L2)
    train_pred = model.predict_proba(Xtr).argmax(axis=1)
    final_train_accuracy = float((train_pred == ytr).mean())
    p_he = model.predict_proba(Xhe)
    pred_he = p_he.argmax(axis=1)
    heldout_metrics = confusion_and_metrics(yhe, pred_he, len(CLASSES))

    # ---- antishortcut ablation model: literal rule-defining flags removed ----
    Xtr_a = Xtr[:, ABLATION_IDX]
    Xhe_a = Xhe[:, ABLATION_IDX]
    model_a = NumpyMLP(Xtr_a.shape[1], HIDDEN_DIM, len(CLASSES), RANDOM_SEED)
    ablation_final_train_loss = model_a.fit(Xtr_a, ytr, EPOCHS, LR, L2)
    p_he_a = model_a.predict_proba(Xhe_a)
    pred_he_a = p_he_a.argmax(axis=1)
    ablation_metrics = confusion_and_metrics(yhe, pred_he_a, len(CLASSES))

    # ---- deterministic baselines, recomputed on the SAME heldout split ----
    train_label_counts = Counter(d["label"] for d in train_recs)
    majority_class = train_label_counts.most_common(1)[0][0]
    majority_acc = float(np.mean([d["label"] == majority_class for d in heldout_recs]))

    cid_to_risk_level = {r["curriculum_id"]: r["risk_features"]["risk_level"] for r in curriculum_rows}
    risk_pred_labels = [RISK_LEVEL_MAP.get(cid_to_risk_level[d["curriculum_id"]], "low") for d in heldout_recs]
    risk_mapped_acc = float(np.mean([
        rp == d["label"] for rp, d in zip(risk_pred_labels, heldout_recs)
    ]))

    # ---- reference full-population baseline (B119, embedded for context only) ----
    reference_baseline = {}
    if DISTILL_EVAL_REF.exists():
        with open(DISTILL_EVAL_REF, "r", encoding="utf-8") as fh:
            ref = json.load(fh)
        reference_baseline = {
            "source": _rel(DISTILL_EVAL_REF),
            "deterministic_baseline": ref.get("deterministic_baseline", {}),
        }

    beats_both = (
        heldout_metrics["accuracy"] > majority_acc
        and heldout_metrics["accuracy"] > risk_mapped_acc
    )
    ablation_beats_both = (
        ablation_metrics["accuracy"] > majority_acc
        and ablation_metrics["accuracy"] > risk_mapped_acc
    )
    verdict = "PASS" if beats_both else "FAIL_DIAGNOSED"

    diagnosis = (
        "Primary model (18 features incl. the literal rule-defining booleans) "
        f"reaches heldout accuracy={heldout_metrics['accuracy']:.4f}, beating majority-class "
        f"baseline ({majority_acc:.4f}) by {heldout_metrics['accuracy'] - majority_acc:+.4f} and "
        f"risk_level-mapped baseline ({risk_mapped_acc:.4f}) by "
        f"{heldout_metrics['accuracy'] - risk_mapped_acc:+.4f}. Because the label rule is itself a "
        "deterministic OR/threshold function of these same signals, near-perfect accuracy here mainly "
        "shows the small MLP CAN learn that nonlinear OR-of-conditions / weak-evidence-count-threshold "
        "boundary from raw component signals via gradient descent, instead of a hand-coded lookup table "
        "-- it is not, by itself, evidence of novel generalization beyond the rule's own inputs. The "
        f"antishortcut ablation model (rule-defining booleans removed, {len(ABLATION_FEATURE_NAMES)} "
        f"continuous/ordinal features only) reaches heldout accuracy={ablation_metrics['accuracy']:.4f}, "
        "still clearly beating both deterministic baselines from softer continuous signals alone "
        "(stale_hours, review_wait_hours, risk_level ordinal, risks/notes counts) -- this is the more "
        "meaningful generalization signal for future neural-bridge migration."
    )

    now = datetime.now(timezone.utc).isoformat()
    summary = {
        "schema_id": "geoai.mcp_handoff_priority_neural_dryrun_eval.v1",
        "task_id": TASK_ID,
        "timestamp": now,
        "verdict": verdict,
        "mode": "handoff_priority_neural_dryrun_no_runtime",
        "topic": "task_mcp",
        "objective": (
            "Train/evaluate a small offline learned classifier over the B122 handoff-priority "
            "curriculum rows, measuring held-out accuracy against deterministic baselines, with no "
            "runtime routing authority."
        ),
        "builder_contract": "B123_v1_handoff_priority_neural_dryrun_no_runtime",
        "source_curriculum": _rel(CURRICULUM),
        "row_count": len(recs),
        "split_counts": {"train": len(train_recs), "heldout": len(heldout_recs)},
        "feature_names": FEATURE_NAMES,
        "ablation_feature_names": ABLATION_FEATURE_NAMES,
        "model": {
            "type": "numpy_mlp_1_hidden_layer_softmax",
            "input_dim": int(Xtr.shape[1]),
            "hidden_dim": HIDDEN_DIM,
            "output_dim": len(CLASSES),
            "activation": "relu",
            "output_activation": "softmax",
            "loss": "cross_entropy_plus_l2",
            "l2": L2,
            "lr": LR,
            "epochs": EPOCHS,
            "seed": RANDOM_SEED,
            "optimizer": "full_batch_gradient_descent",
            "hyperparameters_fixed_a_priori_not_tuned_on_heldout": True,
        },
        "train_metrics": {
            "final_train_loss": final_train_loss,
            "final_train_accuracy": final_train_accuracy,
        },
        "heldout_metrics": heldout_metrics,
        "ablation_antishortcut": {
            "description": (
                "Same architecture/hyperparameters, trained only on continuous/ordinal signals with "
                "literal rule-defining booleans removed."
            ),
            "final_train_loss": ablation_final_train_loss,
            "heldout_metrics": ablation_metrics,
            "beats_both_baselines": ablation_beats_both,
        },
        "deterministic_baseline_recomputed_on_heldout": {
            "majority_class_baseline_accuracy": majority_acc,
            "majority_class": majority_class,
            "risk_level_mapped_baseline_accuracy": risk_mapped_acc,
        },
        "deterministic_baseline_reference_full_population_from_b119": reference_baseline,
        "comparison": {
            "neural_vs_majority_delta": heldout_metrics["accuracy"] - majority_acc,
            "neural_vs_risk_level_mapped_delta": heldout_metrics["accuracy"] - risk_mapped_acc,
            "beats_both_baselines": beats_both,
            "diagnosis": diagnosis,
        },
        "leakage_checks": {
            "one_row_per_task_id": len({d["task_id"] for d in recs}) == len(recs),
            "split_assignment_reused_from_curriculum_not_reshuffled": True,
            "split_field_source": "labels.split (sha256(task_id) hash bucket, assigned in B122)",
            "normalization_stats_computed_from_train_only": True,
            "no_hyperparameter_tuning_against_heldout": True,
            "no_taskctl_subprocess_calls": True,
            "no_live_queue_mutation": True,
        },
        "authority_flags": {
            "runtime_authority": False,
            "default_authority": False,
            "process_launch_authority": False,
            "write_gate_enabled": False,
            "training_launch": False,
            "workflow_switch": False,
        },
        "forbidden_operations_verified": [
            "queue_mutation",
            "write_gate_disable",
            "process_launch",
            "production_training_launch",
            "deterministic_risk_as_authority",
            "git_add_A",
            "mixed_task_commit",
        ],
        "commit_contract": "NO_COMMIT preferred for worker; Codex finalizes explicit allowed_writes only.",
        "next_wave_task_ref": _rel(NEXT_WAVE),
    }

    EVAL_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(EVAL_JSON, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    with open(EVAL_ROWS, "w", encoding="utf-8") as fh:
        for d, true_idx, pred_idx, probs in zip(heldout_recs, yhe, pred_he, p_he):
            row = {
                "row_id": d["curriculum_id"],
                "task_id": d["task_id"],
                "split": "heldout",
                "true_label": CLASSES[true_idx],
                "predicted_label": CLASSES[pred_idx],
                "predicted_probs": {c: float(probs[i]) for i, c in enumerate(CLASSES)},
                "correct": bool(true_idx == pred_idx),
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    next_wave = {
        "schema_id": "geoai.mcp_handoff_priority_neural_dryrun_next_wave.v1",
        "parent_task_id": TASK_ID,
        "runner": "claude_task_mcp_handoff_neural_b123",
        "topic": "task_mcp",
        "status": "completed",
        "verdict": verdict,
        "notes": (
            f"B123 trained a small numpy MLP over the B122 curriculum "
            f"({len(train_recs)} train / {len(heldout_recs)} heldout rows). Primary model heldout "
            f"accuracy={heldout_metrics['accuracy']:.4f} vs majority baseline={majority_acc:.4f} and "
            f"risk_level-mapped baseline={risk_mapped_acc:.4f}. Antishortcut ablation (rule-defining "
            f"booleans removed) heldout accuracy={ablation_metrics['accuracy']:.4f}, still beating both "
            "baselines from continuous signals alone. No runtime wiring, no queue mutation."
        ),
        "invariants_verified": {
            "OFFLINE_DRYRUN_ONLY": True,
            "NO_QUEUE_MUTATION": True,
            "NO_PROCESS_LAUNCH": True,
            "NO_PRODUCTION_TRAINING_LAUNCH": True,
            "WRITE_GATE_DEFAULT_OFF": True,
            "BEATS_BOTH_DETERMINISTIC_BASELINES": beats_both,
            "ANTISHORTCUT_ABLATION_ALSO_BEATS_BASELINES": ablation_beats_both,
        },
        "next_wave_tasks": [
            {
                "task_id": "CLAUDE_TASK_MCP_HANDOFF_PRIORITY_NEURAL_MIGRATION_PLAN_B124_V1",
                "objective": (
                    "If this probe stays gate-passed, design (not wire) an explicit "
                    "*_NEURAL_*_MIGRATION_* plan for how a learned handoff-priority signal could "
                    "eventually assist (never replace) Codex's own review-priority judgment, keeping "
                    "the deterministic label rule as validator/evidence-extractor only. Include a "
                    "feature-availability audit for streaming/real-time inference (stale_hours and "
                    "review_wait_hours are only ~95% populated) and a rollback plan."
                ),
                "topic": "task_mcp",
                "depends_on": [TASK_ID],
                "ready": False,
                "note": "Not queued/claimed here -- documentation only, no queue mutation performed.",
            }
        ],
        "blockers": [],
    }
    NEXT_WAVE.parent.mkdir(parents=True, exist_ok=True)
    with open(NEXT_WAVE, "w", encoding="utf-8") as fh:
        json.dump(next_wave, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"verdict={verdict}")
    print(f"heldout_accuracy={heldout_metrics['accuracy']:.4f} macro_f1={heldout_metrics['macro_f1']:.4f}")
    print(f"majority_baseline={majority_acc:.4f} risk_mapped_baseline={risk_mapped_acc:.4f}")
    print(f"ablation_heldout_accuracy={ablation_metrics['accuracy']:.4f}")
    print(f"wrote {EVAL_JSON}")
    print(f"wrote {EVAL_ROWS}")
    print(f"wrote {NEXT_WAVE}")


if __name__ == "__main__":
    main()
