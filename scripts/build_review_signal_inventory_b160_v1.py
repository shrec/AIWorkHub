#!/usr/bin/env python3
"""Build compact review-signal inventory for MCP-Codex task triage (B160 V1).

READ-ONLY inventory — reads the production task-card registry, classifies every
review-relevant field into signal categories, identifies MCP-exposure gaps, and
recommends compact neural priority target fields for faster Codex triage.

No queue mutation, no process launch, no write-gate change, no training launch.
Deterministic given the registry snapshot on disk.

Outputs:
  tools/geoai-task-mcp/data/review_signal_inventory_b160_v1.jsonl
  tools/geoai-task-mcp/eval/review_signal_inventory_b160_v1.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]

TASK_ID = "DEEPSEEK_TASK_MCP_REVIEW_SIGNAL_INVENTORY_B160_V1"
REGISTRY_PATH = REPO_ROOT / "bitnnv2" / "data" / "tasking" / "machine_task_cards_v1.jsonl"
OUT_JSONL = REPO_ROOT / "tools" / "geoai-task-mcp" / "data" / "review_signal_inventory_b160_v1.jsonl"
OUT_EVAL = REPO_ROOT / "tools" / "geoai-task-mcp" / "eval" / "review_signal_inventory_b160_v1.json"

# ---------------------------------------------------------------------------
# Signal category definitions
# ---------------------------------------------------------------------------
CATEGORIES = {
    "risk": "Risk signals — authority flags, forbidden scope, declared risk, mode riskiness",
    "change_size": "Change-size signals — allowed_writes count, file types, validation count",
    "validation": "Validation signals — test commands, acceptance criteria, verify result",
    "cost": "Cost signals — token in/out, cost_usd, records count from usage report",
    "collision": "Collision signals — overlapping allowed_writes, runner/topic conflicts",
    "staleness": "Staleness signals — age, elapsed hours, review-wait, completed_at presence",
}

# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------

def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _elapsed_hours(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 3600.0, 2)


def _status_family(status: str, worker_status: str) -> str:
    """Normalize to compact lifecycle family."""
    s = (status or "").strip().lower()
    w = (worker_status or "").strip().lower()
    if s in {"finished", "completed", "stale_already_done"} or w == "done":
        return "finished"
    if s.startswith("blocked") or w.startswith(("blocked", "deferred")):
        return "blocked"
    if w in {"review", "ready_for_review", "codex_review", "awaiting_review"}:
        return "review_ready"
    if s == "review" and w not in {"review", "ready_for_review"}:
        return "review_worker_finished"
    if s in {"processing", "in_progress"} or w in {"claimed", "in_progress"}:
        return "processing"
    return "pending"


def _file_type_counts(paths: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in paths:
        ext = Path(p).suffix.lower()
        if ext:
            counts[ext] = counts.get(ext, 0) + 1
        else:
            counts["no_ext"] = counts.get("no_ext", 0) + 1
    return counts


def extract_signals(card: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Extract all review-relevant signals from a single task card."""
    tid = card.get("task_id", "?")
    status = str(card.get("status") or "")
    worker_status = str(card.get("worker_status") or "")

    # ── Risk signals ──
    authority_flags = card.get("authority_flags", {}) or {}
    forbidden = card.get("forbidden", []) or []
    human_brief = card.get("human_brief", {}) or {}
    declared_risk = str(human_brief.get("risk", "")).strip().lower() if isinstance(human_brief, dict) else ""
    mode = str(card.get("mode") or "")
    mode_is_readonly = "no_runtime" in mode or "readonly" in mode or "inventory" in mode
    mode_has_runtime = "implementation" in mode or "runtime" in mode
    commit_contract = str(card.get("commit_contract") or "")

    risk_signals = {
        "declared_risk": declared_risk or None,
        "authority_runtime": bool(authority_flags.get("runtime_authority")),
        "authority_default": bool(authority_flags.get("default_authority")),
        "authority_training_launch": bool(authority_flags.get("training_launch")),
        "forbidden_count": len(forbidden),
        "forbidden_items": forbidden,
        "mode": mode,
        "mode_is_readonly": mode_is_readonly,
        "mode_has_runtime": mode_has_runtime,
        "has_forbidden_git_add_a": any("git_add_A" in f or "git add -A" in f.lower() for f in forbidden),
        "has_forbidden_mixed_task": any("mixed_task_commit" in f for f in forbidden),
        "commit_contract_no_commit": "no_commit" in commit_contract.lower(),
        "commit_contract_explicit": bool(commit_contract),
    }

    # ── Change-size signals ──
    allowed_writes = card.get("allowed_writes", []) or []
    validation_commands = card.get("validation", []) or []
    acceptance = card.get("acceptance", []) or []
    read_first = card.get("read_first", []) or []

    change_signals = {
        "allowed_writes_count": len(allowed_writes),
        "allowed_writes_file_types": _file_type_counts(allowed_writes),
        "validation_commands_count": len(validation_commands),
        "acceptance_criteria_count": len(acceptance),
        "read_first_count": len(read_first),
        "allowed_writes_has_server_py": any("server.py" in str(p) for p in allowed_writes),
        "allowed_writes_has_registry": any("registry" in str(p).lower() for p in allowed_writes),
        "allowed_writes_has_default": any("default" in str(p).lower() or "active_default" in str(p) for p in allowed_writes),
    }

    # ── Validation signals ──
    has_bash_test = any("bash" in str(v).lower() for v in validation_commands)
    has_pytest = any("pytest" in str(v).lower() or "unittest" in str(v).lower() for v in validation_commands)
    has_python_script = any(".py" in str(v) for v in validation_commands)
    has_taskctl_verify = any("taskctl.py verify" in str(v) for v in validation_commands)

    validation_signals = {
        "validation_commands": validation_commands,
        "has_bash_test": has_bash_test,
        "has_pytest": has_pytest,
        "has_python_script": has_python_script,
        "has_taskctl_verify": has_taskctl_verify,
        "has_any_validation": len(validation_commands) > 0,
        "acceptance_criteria": acceptance,
    }

    # ── Staleness signals ──
    created_at = _parse_iso(card.get("created_at"))
    started_at = _parse_iso(card.get("started_at"))
    completed_at = _parse_iso(card.get("completed_at"))
    review_at = _parse_iso(card.get("review_at"))
    updated_at = _parse_iso(card.get("updated_at"))
    claimed_at = _parse_iso(card.get("claimed_at"))

    age_hours = _elapsed_hours(created_at, now) if created_at else None
    started_elapsed = _elapsed_hours(started_at, now) if started_at else None
    review_wait_hours = _elapsed_hours(review_at, completed_at or now) if review_at else None
    completed_hours_ago = _elapsed_hours(completed_at, now) if completed_at else None

    staleness_signals = {
        "status_family": _status_family(status, worker_status),
        "status": status,
        "worker_status": worker_status,
        "created_at": card.get("created_at"),
        "started_at": card.get("started_at"),
        "completed_at": card.get("completed_at"),
        "review_at": card.get("review_at"),
        "age_hours": age_hours,
        "started_elapsed_hours": started_elapsed,
        "review_wait_hours": review_wait_hours,
        "completed_hours_ago": completed_hours_ago,
        "is_stale": bool(completed_at and completed_hours_ago is not None and completed_hours_ago > 720),  # 30 days
    }

    # ── Cost signals (placeholder — filled from usage_report separately) ──
    cost_signals: dict[str, Any] = {
        "usage_report_available": False,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "usage_records": None,
    }

    # ── Collision signals (placeholder — filled from collision table separately) ──
    collision_signals: dict[str, Any] = {
        "has_collision": None,
        "collision_files": [],
    }

    # ── Reviewer-focus signals ──
    reviewer_focus = str(human_brief.get("reviewer_focus", "")).strip() if isinstance(human_brief, dict) else ""
    intent = str(human_brief.get("intent", "")).strip() if isinstance(human_brief, dict) else ""
    expected_win = str(human_brief.get("expected_win", "")).strip() if isinstance(human_brief, dict) else ""

    focus_signals = {
        "reviewer_focus": reviewer_focus or None,
        "intent": intent or None,
        "expected_win": expected_win or None,
        "human_brief_present": bool(human_brief),
    }

    return {
        "task_id": tid,
        "runner": card.get("runner", ""),
        "topic": card.get("topic", ""),
        "priority": card.get("priority", ""),
        "queue_order": card.get("queue_order", 1000),
        "objective": card.get("objective", ""),
        "risk_signals": risk_signals,
        "change_size_signals": change_signals,
        "validation_signals": validation_signals,
        "staleness_signals": staleness_signals,
        "cost_signals": cost_signals,
        "collision_signals": collision_signals,
        "focus_signals": focus_signals,
    }


# ---------------------------------------------------------------------------
# MCP exposure analysis
# ---------------------------------------------------------------------------

def _mcp_tool_coverage() -> dict[str, Any]:
    """Map each signal category to current MCP tool exposure."""
    return {
        "risk": {
            "exposed_via": ["geoai_task_show", "geoai_task_review_summarize"],
            "exposed_fields": ["authority_flags", "forbidden", "human_brief.risk", "mode"],
            "gap": "No aggregated risk score across queue; human_brief.risk is optional text, not enumerated.",
        },
        "change_size": {
            "exposed_via": ["geoai_task_show", "geoai_task_review_summarize"],
            "exposed_fields": ["allowed_writes", "validation", "acceptance", "read_first"],
            "gap": "No file-type breakdown or scope-warning flag in MCP tool response; counts are implicit.",
        },
        "validation": {
            "exposed_via": ["geoai_task_show", "geoai_task_health (verify)"],
            "exposed_fields": ["validation commands", "verify returncode"],
            "gap": "No per-task test-pass/fail state stored; verify is queue-wide, not per-task.",
        },
        "cost": {
            "exposed_via": ["geoai_task_usage_report"],
            "exposed_fields": ["tokens in/out", "cost_usd", "records"],
            "gap": "Cost not joined into task card view; requires separate call. No cumulative cost per topic.",
        },
        "collision": {
            "exposed_via": ["geoai_task_collision_guard"],
            "exposed_fields": ["collision_free", "collision_count", "file_collisions"],
            "gap": "Per-card collision state not surfaced in show/list; requires full guard scan.",
        },
        "staleness": {
            "exposed_via": ["geoai_task_list (status filter)", "geoai_task_show (timestamps)"],
            "exposed_fields": ["status", "worker_status", "created_at", "completed_at"],
            "gap": "No computed staleness score or age-hours in tool response; raw timestamps only.",
        },
    }


# ---------------------------------------------------------------------------
# Neural priority target recommendations
# ---------------------------------------------------------------------------

def _recommended_neural_targets() -> list[dict[str, Any]]:
    """Compact fields a neural controller should learn to use for triage ranking."""
    return [
        {
            "field": "risk_level",
            "type": "enum{low,medium,high}",
            "source": "Combined from authority_flags, forbidden scope, declared_risk, mode",
            "rationale": "Single compact risk tier for quick filtering before deep review.",
            "priority": "P0",
        },
        {
            "field": "change_scope",
            "type": "enum{tiny(0-2),small(3-6),medium(7-15),large(16+)}",
            "source": "allowed_writes count + file-type diversity",
            "rationale": "Codex reviewer needs to know review effort before opening card.",
            "priority": "P0",
        },
        {
            "field": "validation_gate",
            "type": "enum{pass,fail,unknown}",
            "source": "Per-task test result (currently not stored per-task; needs infra)",
            "rationale": "A task with unknown validation is riskier than one with green tests.",
            "priority": "P1",
        },
        {
            "field": "staleness_hours",
            "type": "float",
            "source": "Elapsed hours since started_at (if processing) or review_at (if review)",
            "rationale": "Stale review items decay in relevance; oldest-first or risk-weighted.",
            "priority": "P1",
        },
        {
            "field": "collision_risk",
            "type": "bool",
            "source": "Whether this task's allowed_writes overlap any other active task",
            "rationale": "Colliding tasks must be sequenced; flag before claiming.",
            "priority": "P0",
        },
        {
            "field": "has_human_brief",
            "type": "bool",
            "source": "Presence of human_brief with reviewer_focus",
            "rationale": "Cards with explicit reviewer guidance are faster to triage.",
            "priority": "P2",
        },
        {
            "field": "commit_hygiene_ok",
            "type": "bool",
            "source": "commit_contract contains no-commit + git-add-all prohibition + mixed-task prohibition",
            "rationale": "Poor commit hygiene correlates with review-back pressure.",
            "priority": "P1",
        },
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    registry = REGISTRY_PATH
    if not registry.exists():
        print(f"ERROR: registry not found: {registry}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    cards: list[dict[str, Any]] = []
    with open(registry, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                cards.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    print(f"Loaded {len(cards)} cards from registry.")

    # ── Extract signals per card ──
    rows: list[dict[str, Any]] = []
    status_family_counts: Counter[str] = Counter()
    topic_counts: Counter[str] = Counter()
    runner_counts: Counter[str] = Counter()
    declared_risk_counts: Counter[str] = Counter()
    allowed_writes_all: list[int] = []
    age_hours_all: list[float] = []
    review_wait_all: list[float] = []

    for card in cards:
        sig = extract_signals(card, now)
        rows.append(sig)
        status_family_counts[sig["staleness_signals"]["status_family"]] += 1
        topic_counts[sig["topic"]] += 1
        runner_counts[sig["runner"]] += 1
        dr = sig["risk_signals"]["declared_risk"]
        if dr:
            declared_risk_counts[dr] += 1
        allowed_writes_all.append(sig["change_size_signals"]["allowed_writes_count"])
        ah = sig["staleness_signals"]["age_hours"]
        if ah is not None:
            age_hours_all.append(ah)
        rw = sig["staleness_signals"]["review_wait_hours"]
        if rw is not None:
            review_wait_all.append(rw)

    # ── Write JSONL ──
    OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSONL, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"Wrote {len(rows)} rows to {OUT_JSONL}")

    # ── Enrich: find examples per status family ──
    examples: dict[str, list[str]] = {}
    for fam in ["finished", "review_ready", "review_worker_finished", "blocked", "processing", "pending"]:
        matches = [r for r in rows if r["staleness_signals"]["status_family"] == fam]
        examples[fam] = [m["task_id"] for m in matches[:5]]

    # ── Identify gaps ──
    mcp_coverage = _mcp_tool_coverage()
    gaps = []
    for cat, info in mcp_coverage.items():
        if info["gap"]:
            gaps.append({
                "category": cat,
                "gap": info["gap"],
                "exposed_via": info["exposed_via"],
            })

    # ── Compute aggregate stats ──
    aw_sorted = sorted(allowed_writes_all)
    p50_idx = len(aw_sorted) // 2
    p90_idx = int(len(aw_sorted) * 0.9)
    p95_idx = int(len(aw_sorted) * 0.95)

    age_sorted = sorted(age_hours_all)
    rw_sorted = sorted(review_wait_all)

    # ── Write eval JSON ──
    eval_data: dict[str, Any] = {
        "schema_id": "geoai.review_signal_inventory.v1",
        "task_id": TASK_ID,
        "builder_contract": "B160_v1_review_signal_inventory_no_runtime",
        "generated_at": now.isoformat(),
        "source_registry": str(registry),
        "total_cards": len(cards),
        "signal_categories": CATEGORIES,
        "status_family_counts": dict(status_family_counts),
        "topic_counts": dict(topic_counts.most_common(30)),
        "runner_counts": dict(runner_counts.most_common(30)),
        "declared_risk_counts": dict(declared_risk_counts),
        "examples_by_status_family": examples,
        "aggregate_stats": {
            "allowed_writes": {
                "min": aw_sorted[0] if aw_sorted else 0,
                "p50": aw_sorted[p50_idx] if aw_sorted else 0,
                "p90": aw_sorted[p90_idx] if aw_sorted else 0,
                "p95": aw_sorted[p95_idx] if aw_sorted else 0,
                "max": aw_sorted[-1] if aw_sorted else 0,
                "mean": round(sum(allowed_writes_all) / len(allowed_writes_all), 2) if allowed_writes_all else 0,
            },
            "age_hours": {
                "min": round(age_sorted[0], 1) if age_sorted else 0,
                "p50": round(age_sorted[len(age_sorted)//2], 1) if age_sorted else 0,
                "p90": round(age_sorted[int(len(age_sorted)*0.9)], 1) if age_sorted else 0,
                "max": round(age_sorted[-1], 1) if age_sorted else 0,
            },
            "review_wait_hours": {
                "min": round(rw_sorted[0], 1) if rw_sorted else 0,
                "p50": round(rw_sorted[len(rw_sorted)//2], 1) if rw_sorted else 0,
                "p90": round(rw_sorted[int(len(rw_sorted)*0.9)], 1) if rw_sorted else 0,
                "max": round(rw_sorted[-1], 1) if rw_sorted else 0,
            },
        },
        "mcp_exposure_gaps": gaps,
        "recommended_neural_priority_targets": _recommended_neural_targets(),
        "signal_inventory_field_list": {
            "risk": list(rows[0]["risk_signals"].keys()) if rows else [],
            "change_size": list(rows[0]["change_size_signals"].keys()) if rows else [],
            "validation": list(rows[0]["validation_signals"].keys()) if rows else [],
            "cost": list(rows[0]["cost_signals"].keys()) if rows else [],
            "collision": list(rows[0]["collision_signals"].keys()) if rows else [],
            "staleness": list(rows[0]["staleness_signals"].keys()) if rows else [],
            "focus": list(rows[0]["focus_signals"].keys()) if rows else [],
        },
    }

    OUT_EVAL.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_EVAL, "w", encoding="utf-8") as fh:
        json.dump(eval_data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"Wrote eval to {OUT_EVAL}")

    print(f"\nSignal categories: {list(CATEGORIES.keys())}")
    print(f"Status families: {dict(status_family_counts)}")
    print(f"Gaps identified: {len(gaps)}")
    print(f"Neural targets recommended: {len(_recommended_neural_targets())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
