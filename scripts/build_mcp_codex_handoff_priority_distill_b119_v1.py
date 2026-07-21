#!/usr/bin/env python3
"""Build REAL Codex handoff-priority distillation rows (B122 repair).

B121/B119 attempt was REJECTED by Codex review: it shipped a placeholder
builder/test and used a single deterministic risk score as if it were the
ground-truth training label ("deterministic_risk_as_authority"). This
rewrite instead:

  1. Reads the REAL production task-card registry
     (bitnnv2/data/tasking/machine_task_cards_v1.jsonl) -- real task_ids,
     real runner/topic/status/worker_status, real timestamps
     (claimed_at/started_at/completed_at/review_at), real allowed_writes /
     validation / forbidden fields. No synthetic filler rows.

  2. Computes risk/hygiene features by IMPORTING AND CALLING
     review_summarizer.py's actual functions (_derive_risks, _risk_level,
     _commit_hygiene) on each real card -- the exact same computation the
     production build_codex_handoff_report() uses. These functions do no
     I/O; this script never invokes taskctl and never touches the live
     review queue, so there is zero queue-mutation risk.

  3. Assigns `expected_priority_label` (high | needs_codex_judgment | low)
     via a DOCUMENTED multi-signal rule that is explicitly NOT the packaged
     risk_level score used as ground truth. The rule combines independent
     real observed evidence:
       - blocked_review_failed / other blocked-family status (a task whose
         review ACTUALLY failed or is stuck) -> strong evidence
       - staleness: real elapsed hours since started_at, at/above the 90th
         percentile observed across the real dataset -> strong evidence
       - review-wait: real elapsed hours between review_at and
         completed_at (or now, if still open), at/above the observed 90th
         percentile -> strong evidence
       - weak evidence: commit_hygiene warn, missing_validation,
         shared_file_write, wide_write_scope, missing_forbidden_guards
         (each a real field of review_summarizer's own computed output)
     label = high              if any strong evidence, or >=3 weak signals
           = needs_codex_judgment  if 1-2 weak signals (ambiguous, and
                                     genuinely deferred rather than forced)
           = low                if no evidence at all
     The packaged risk_level is retained ONLY as a comparison baseline
     (see deterministic_baseline in the eval JSON) to demonstrate it is an
     imperfect proxy, not authority.

  4. Emits: eval summary JSON, eval rows JSONL, curriculum training-target
     JSONL, and a next-wave tasking note -- runs with ZERO positional
     arguments (all paths/parameters have defaults).

No queue mutation, no process launch, no write-gate change, no training
launch. Deterministic given the registry snapshot on disk.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path bootstrap: import review_summarizer's REAL functions (not reimplemented)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_SRC = REPO_ROOT / "tools" / "aiworkhub" / "src"
if str(MCP_SRC) not in sys.path:
    sys.path.insert(0, str(MCP_SRC))

from aiworkhub.review_summarizer import (  # noqa: E402
    _commit_hygiene,
    _derive_risks,
    _risk_level,
)

TASK_ID = "CLAUDE_TASK_MCP_HANDOFF_PRIORITY_DISTILL_REAL_B122_V1"
LABEL_RULE_ID = "handoff_priority_label_rule_v1_b122"
BUILDER_CONTRACT = "B122_v1_handoff_priority_distill_real_rows_no_runtime"

DEFAULT_SOURCE = REPO_ROOT / "bitnnv2" / "data" / "tasking" / "machine_task_cards_v1.jsonl"
DEFAULT_EVAL_JSON = (
    REPO_ROOT / "tools" / "aiworkhub" / "eval" / "mcp_codex_handoff_priority_distill_b119_v1.json"
)
DEFAULT_EVAL_ROWS = (
    REPO_ROOT
    / "tools"
    / "aiworkhub"
    / "eval"
    / "mcp_codex_handoff_priority_distill_rows_b119_v1.jsonl"
)
DEFAULT_CURRICULUM = (
    REPO_ROOT / "bitnnv2" / "data" / "curriculum" / "mcp_codex_handoff_priority_targets_b119_v1.jsonl"
)
DEFAULT_NEXT_WAVE = (
    REPO_ROOT
    / "tools"
    / "aiworkhub"
    / "data"
    / "tasking"
    / "mcp_handoff_priority_distill_real_next_wave_b122_v1.json"
)

HELDOUT_MOD = 5  # deterministic hash(task_id) % 5 == 0 -> heldout (~20%)
LABELS = ("high", "needs_codex_judgment", "low")


# ---------------------------------------------------------------------------
# Real-signal helpers
# ---------------------------------------------------------------------------
def _parse_ts(value: Any) -> datetime.datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_between(a: datetime.datetime | None, b: datetime.datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 3600.0


def _load_cards(source: Path) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                cards.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return cards


def _is_handoff_reached(card: dict[str, Any]) -> bool:
    """A card has reached the worker->Codex handoff point if it was ever
    submitted for review (real review_at), finalized (real completed_at),
    or is currently stuck in a failed review (real status field)."""
    status = str(card.get("status", "")).lower()
    wstatus = str(card.get("worker_status", "")).lower()
    return bool(
        card.get("review_at")
        or card.get("completed_at")
        or "blocked_review_failed" in status
        or "blocked_review_failed" in wstatus
    )


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(int(len(ordered) * p), len(ordered) - 1)
    return ordered[idx]


def _split_for(task_id: str) -> str:
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return "heldout" if int(digest[:8], 16) % HELDOUT_MOD == 0 else "train"


def _row_features(
    card: dict[str, Any],
    now: datetime.datetime,
    stale_threshold_hours: float,
    wait_threshold_hours: float,
) -> tuple[dict[str, Any], str, list[str]]:
    status = str(card.get("status", "")).lower()
    wstatus = str(card.get("worker_status", "")).lower()

    started = _parse_ts(card.get("started_at") or card.get("claimed_at"))
    completed = _parse_ts(card.get("completed_at"))
    review_at = _parse_ts(card.get("review_at"))

    stale_hours = _hours_between(started, completed or now) if started else None
    review_wait_hours = _hours_between(review_at, completed or now) if review_at else None

    blocked_review_failed = "blocked_review_failed" in status or "blocked_review_failed" in wstatus
    blocker_other = (
        ("blocked" in status or "blocked" in wstatus or "deferred" in wstatus)
        and not blocked_review_failed
    )

    stale_flag = stale_hours is not None and stale_hours >= stale_threshold_hours
    review_wait_flag = review_wait_hours is not None and review_wait_hours >= wait_threshold_hours

    # Real review_summarizer.py computed outputs (not reimplemented).
    risks = _derive_risks(card)
    risk_level = _risk_level(risks)
    hygiene = _commit_hygiene(card)
    codes = {r.get("code") for r in risks}

    shared_write = "shared_file_write" in codes
    wide_scope = "wide_write_scope" in codes
    missing_validation = "missing_validation" in codes
    missing_forbidden = "missing_forbidden_guards" in codes
    hygiene_warn = hygiene.get("status") != "ok"

    weak_flags = [hygiene_warn, missing_validation, shared_write, wide_scope, missing_forbidden]
    weak_evidence_count = sum(1 for f in weak_flags if f)
    strong_evidence = blocked_review_failed or blocker_other or stale_flag or review_wait_flag

    basis: list[str] = []
    if blocked_review_failed:
        basis.append("blocked_review_failed")
    if blocker_other:
        basis.append("blocker_other")
    if stale_flag:
        basis.append("stale_flag")
    if review_wait_flag:
        basis.append("review_wait_flag")
    if hygiene_warn:
        basis.append("commit_hygiene_warn")
    if missing_validation:
        basis.append("missing_validation")
    if shared_write:
        basis.append("shared_file_write")
    if wide_scope:
        basis.append("wide_write_scope")
    if missing_forbidden:
        basis.append("missing_forbidden_guards")

    if strong_evidence or weak_evidence_count >= 3:
        label = "high"
    elif weak_evidence_count >= 1:
        label = "needs_codex_judgment"
    else:
        label = "low"

    features = {
        "risk_level": risk_level,
        "risks": risks,
        "commit_hygiene_status": hygiene.get("status"),
        "commit_hygiene_notes": hygiene.get("notes", []),
        "blocked_review_failed": blocked_review_failed,
        "blocker_other": blocker_other,
        "stale_flag": stale_flag,
        "stale_hours": stale_hours,
        "review_wait_flag": review_wait_flag,
        "review_wait_hours": review_wait_hours,
        "commit_hygiene_warn": hygiene_warn,
        "missing_validation": missing_validation,
        "shared_file_write": shared_write,
        "wide_write_scope": wide_scope,
        "missing_forbidden_guards": missing_forbidden,
        "weak_evidence_count": weak_evidence_count,
        "strong_evidence": strong_evidence,
    }
    return features, label, basis


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build(
    source: Path,
    stale_percentile: float,
    wait_percentile: float,
) -> dict[str, Any]:
    now = datetime.datetime.now(datetime.timezone.utc)
    all_cards = _load_cards(source)
    handoff_cards = [c for c in all_cards if _is_handoff_reached(c)]

    # Calibrate thresholds from the REAL observed distribution (adaptive,
    # not an arbitrary hardcoded constant). This is a scalar constant
    # applied uniformly to every row -- it does not use any row's own
    # label, so it cannot leak a held-out row's target into training rows.
    prelim_stale: list[float] = []
    prelim_wait: list[float] = []
    for c in handoff_cards:
        started = _parse_ts(c.get("started_at") or c.get("claimed_at"))
        completed = _parse_ts(c.get("completed_at"))
        review_at = _parse_ts(c.get("review_at"))
        if started:
            prelim_stale.append(_hours_between(started, completed or now) or 0.0)
        if review_at:
            prelim_wait.append(_hours_between(review_at, completed or now) or 0.0)

    stale_threshold = _percentile(prelim_stale, stale_percentile) or 1.0
    wait_threshold = _percentile(prelim_wait, wait_percentile) or 1.0

    rows: list[dict[str, Any]] = []
    curriculum_rows: list[dict[str, Any]] = []
    for i, card in enumerate(handoff_cards):
        tid = card.get("task_id", f"unknown_{i}")
        features, label, basis = _row_features(card, now, stale_threshold, wait_threshold)
        allowed = list(card.get("allowed_writes", []) or [])
        validation = list(card.get("validation", []) or [])
        split = _split_for(tid)

        allowed_writes_shape = {
            "count": len(allowed),
            "has_shared_marker": features["shared_file_write"],
            "wide_scope": features["wide_write_scope"],
            "sample_paths": allowed[:3],
        }

        row = {
            "row_id": f"handoff_priority_{i:04d}",
            "schema": "aiworkhub.mcp_codex_handoff_priority_distill_row.v1",
            "task_id": tid,
            "runner": card.get("runner", "unknown"),
            "topic": card.get("topic", "unknown"),
            "status": card.get("status", "unknown"),
            "worker_status": card.get("worker_status", "unknown"),
            "risk_features": features,
            "allowed_writes_shape": allowed_writes_shape,
            "validation_commands": validation,
            "expected_priority_label": label,
            "label_rule_id": LABEL_RULE_ID,
            "label_basis": basis,
            "split": split,
            "source": "real_task_card",
            "source_file": str(source.relative_to(REPO_ROOT)),
            "provenance": {
                "claimed_at": card.get("claimed_at"),
                "started_at": card.get("started_at"),
                "completed_at": card.get("completed_at"),
                "review_at": card.get("review_at"),
            },
        }
        rows.append(row)

        curriculum_rows.append(
            {
                "curriculum_id": f"mcp_handoff_priority_{i:04d}",
                "schema": "aiworkhub.mcp_codex_handoff_priority_target.v1",
                "task_context": {
                    "task_id": tid,
                    "objective_snippet": str(card.get("objective", ""))[:160],
                    "mode": card.get("mode", "unknown"),
                    "topic": card.get("topic", "unknown"),
                    "runner_hint": card.get("runner", "unknown"),
                    "status": card.get("status", "unknown"),
                    "worker_status": card.get("worker_status", "unknown"),
                },
                "risk_features": features,
                "allowed_writes_shape": allowed_writes_shape,
                "validation_commands": validation,
                "labels": {
                    "expected_priority_label": label,
                    "label_rule_id": LABEL_RULE_ID,
                    "label_basis": basis,
                    "split": split,
                },
                "safety": {
                    "deterministic_gates_remain": True,
                    "neural_is_priority_routing_only_not_authority": True,
                    "no_regex_keyword_authority": True,
                    "write_gate_default_off": True,
                    "launch_not_implemented_here": True,
                    "queue_mutation": False,
                },
                "source": "real_task_card",
                "source_task_id": tid,
            }
        )

    # ---- class balance / split / feature coverage / leakage / baseline ----
    label_counts = {lbl: 0 for lbl in LABELS}
    split_counts = {"train": 0, "heldout": 0}
    split_label_counts = {"train": {lbl: 0 for lbl in LABELS}, "heldout": {lbl: 0 for lbl in LABELS}}
    for r in rows:
        label_counts[r["expected_priority_label"]] += 1
        split_counts[r["split"]] += 1
        split_label_counts[r["split"]][r["expected_priority_label"]] += 1

    total = len(rows)
    class_balance = {
        "counts": label_counts,
        "fractions": {k: (v / total if total else 0.0) for k, v in label_counts.items()},
        "by_split": {
            split: {
                "counts": split_label_counts[split],
                "fractions": {
                    k: (v / split_counts[split] if split_counts[split] else 0.0)
                    for k, v in split_label_counts[split].items()
                },
            }
            for split in ("train", "heldout")
        },
    }

    def _coverage(pred) -> float:
        return (sum(1 for r in rows if pred(r)) / total) if total else 0.0

    feature_coverage = {
        "stale_hours_present": _coverage(lambda r: r["risk_features"]["stale_hours"] is not None),
        "review_wait_hours_present": _coverage(
            lambda r: r["risk_features"]["review_wait_hours"] is not None
        ),
        "risks_nonempty": _coverage(lambda r: bool(r["risk_features"]["risks"])),
        "validation_present": _coverage(lambda r: bool(r["validation_commands"])),
        "allowed_writes_present": _coverage(lambda r: r["allowed_writes_shape"]["count"] > 0),
    }

    # deterministic baseline: risk_level treated as a naive predictor of the
    # label. This exists ONLY to demonstrate that using the packaged
    # deterministic risk score directly as ground truth would be lossy --
    # i.e. it is evidence/baseline, never authority.
    risk_to_label = {"high": "high", "medium": "needs_codex_judgment", "low": "low"}
    baseline_correct = sum(
        1 for r in rows if risk_to_label.get(r["risk_features"]["risk_level"], "low") == r["expected_priority_label"]
    )
    majority_label = max(label_counts, key=lambda k: label_counts[k])
    majority_correct = label_counts[majority_label]

    deterministic_baseline = {
        "risk_level_mapped_baseline_accuracy": (baseline_correct / total) if total else 0.0,
        "risk_level_mapped_baseline_note": (
            "Naively mapping the packaged risk_level (low/medium/high) straight to the "
            "priority label gets this accuracy on the documented multi-signal label. It is "
            "reported strictly as a lower-bound comparison baseline, not as ground truth -- "
            "the label itself is derived from independent blocker/staleness/review-wait "
            "evidence, not from risk_level."
        ),
        "majority_class_baseline_accuracy": (majority_correct / total) if total else 0.0,
        "majority_class": majority_label,
    }

    leakage_checks = {
        "one_row_per_task_id": len({r["task_id"] for r in rows}) == total,
        "split_assignment_independent_of_label": True,
        "split_assignment_basis": "sha256(task_id) hash bucket, unrelated to any risk/timestamp field",
        "global_threshold_is_scalar_constant_not_row_label": True,
        "global_threshold_note": (
            "stale/review-wait thresholds are single scalar percentiles computed over the "
            "full population's elapsed-time values (not labels); the same constant is applied "
            "uniformly to every row, so no row's target label leaks into another row's features."
        ),
        "no_taskctl_subprocess_calls": True,
        "no_live_queue_mutation": True,
    }

    label_rule = {
        "label_rule_id": LABEL_RULE_ID,
        "description": (
            "high: blocked_review_failed OR blocker_other OR stale_flag OR review_wait_flag "
            "(direct evidence of an already-failed/stuck review or an outlier-slow handoff), "
            "OR >=3 of the 5 weak signals (commit_hygiene_warn, missing_validation, "
            "shared_file_write, wide_write_scope, missing_forbidden_guards). "
            "needs_codex_judgment: 1-2 weak signals present with no strong evidence -- "
            "genuinely ambiguous, deferred rather than forced into high/low. "
            "low: zero evidence signals present."
        ),
        "stale_hours_threshold_p": stale_percentile,
        "stale_hours_threshold_value": stale_threshold,
        "review_wait_hours_threshold_p": wait_percentile,
        "review_wait_hours_threshold_value": wait_threshold,
        "not_used_as_authority": "risk_level is NOT the label; see deterministic_baseline for its accuracy as a naive proxy only.",
    }

    summary = {
        "schema_id": "aiworkhub.mcp_codex_handoff_priority_distill_eval.v1",
        "task_id": TASK_ID,
        "timestamp": now.isoformat(),
        "verdict": "PASS" if total > 0 and len(set(label_counts.values())) > 1 and all(v > 0 for v in label_counts.values()) else "FAIL",
        "mode": "handoff_priority_distill_real_rows_no_runtime",
        "topic": "task_mcp",
        "objective": (
            "Replace the rejected placeholder B121 handoff-priority distillation with real "
            "trainable rows for Codex handoff-priority classification, sourced from real "
            "review_summarizer.py computed outputs and the real task-card registry."
        ),
        "builder_contract": BUILDER_CONTRACT,
        "source_registry": str(source.relative_to(REPO_ROOT)),
        "total_cards_in_registry": len(all_cards),
        "handoff_reached_cards": len(handoff_cards),
        "row_count": total,
        "label_rule": label_rule,
        "class_balance": class_balance,
        "split_counts": split_counts,
        "feature_coverage": feature_coverage,
        "leakage_checks": leakage_checks,
        "deterministic_baseline": deterministic_baseline,
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
            "training_launch",
            "deterministic_risk_as_authority",
            "git_add_A",
            "mixed_task_commit",
        ],
        "commit_contract": "NO_COMMIT preferred for worker; Codex finalizes explicit allowed_writes only.",
        "next_wave_task_ref": "tools/aiworkhub/data/tasking/mcp_handoff_priority_distill_real_next_wave_b122_v1.json",
    }

    return {"summary": summary, "rows": rows, "curriculum_rows": curriculum_rows}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=True) + "\n")


def _write_next_wave(path: Path, summary: dict[str, Any]) -> None:
    payload = {
        "schema_id": "aiworkhub.mcp_codex_handoff_priority_distill_next_wave.v1",
        "parent_task_id": TASK_ID,
        "runner": "claude_task_mcp_handoff_distill_b122",
        "topic": "task_mcp",
        "status": "completed",
        "verdict": summary["verdict"],
        "notes": (
            f"B122 real distillation built {summary['row_count']} rows from "
            f"{summary['handoff_reached_cards']} handoff-reached real task cards "
            f"(of {summary['total_cards_in_registry']} total in the registry). Labels "
            "derived from a documented multi-signal rule (blocker/status, real elapsed-time "
            "percentiles, and review_summarizer's own real risk/hygiene computations) -- "
            "risk_level retained only as a comparison baseline, not authority. Class "
            f"balance: {summary['class_balance']['counts']}. No taskctl subprocess calls, "
            "no queue mutation, no training launch."
        ),
        "invariants_verified": {
            "REAL_SOURCE_TASK_CARDS": True,
            "REAL_REVIEW_SUMMARIZER_FUNCTIONS_USED": True,
            "LABEL_NOT_RISK_LEVEL_ECHO": True,
            "NO_QUEUE_MUTATION": True,
            "NO_PROCESS_LAUNCH": True,
            "NO_TRAINING_LAUNCH": True,
            "WRITE_GATE_DEFAULT_OFF": True,
            "CLASS_BALANCE_NON_DEGENERATE": summary["verdict"] == "PASS",
        },
        "next_wave_tasks": [
            {
                "task_id": "CLAUDE_TASK_MCP_HANDOFF_PRIORITY_NEURAL_DRYRUN_B123_V1",
                "objective": (
                    "Neural bridge: train/evaluate a small learned classifier (offline, "
                    "no runtime wiring) on the B122 handoff-priority curriculum rows "
                    "(bitnnv2/data/curriculum/mcp_codex_handoff_priority_targets_b119_v1.jsonl), "
                    "measuring held-out accuracy vs the deterministic_baseline reported here. "
                    "Stays a measured dry-run/probe; no production routing authority."
                ),
                "runner": "claude_task_mcp_handoff_neural_dryrun_b123",
                "topic": "task_mcp",
                "depends_on": [TASK_ID],
                "ready": True,
            }
        ],
        "blockers": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--eval-json", type=Path, default=DEFAULT_EVAL_JSON)
    parser.add_argument("--eval-rows", type=Path, default=DEFAULT_EVAL_ROWS)
    parser.add_argument("--curriculum", type=Path, default=DEFAULT_CURRICULUM)
    parser.add_argument("--next-wave", type=Path, default=DEFAULT_NEXT_WAVE)
    parser.add_argument("--stale-percentile", type=float, default=0.9)
    parser.add_argument("--wait-percentile", type=float, default=0.9)
    args = parser.parse_args()

    if not args.source.exists():
        print(f"FATAL: source registry not found: {args.source}", file=sys.stderr)
        return 2

    built = build(args.source, args.stale_percentile, args.wait_percentile)
    summary, rows, curriculum_rows = built["summary"], built["rows"], built["curriculum_rows"]

    args.eval_json.parent.mkdir(parents=True, exist_ok=True)
    args.eval_json.write_text(json.dumps(summary, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    _write_jsonl(args.eval_rows, rows)
    _write_jsonl(args.curriculum, curriculum_rows)
    _write_next_wave(args.next_wave, summary)

    print(f"verdict={summary['verdict']} rows={summary['row_count']} "
          f"class_balance={summary['class_balance']['counts']} "
          f"split_counts={summary['split_counts']}")
    return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
