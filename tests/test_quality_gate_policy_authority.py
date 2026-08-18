from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import quality_evidence


def _declare(root: Path, checks: list[dict]) -> None:
    config = root / ".aiworkhub" / "quality.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"checks": checks}), encoding="utf-8")


# --- fix #6: combined-tree validation carries the parent fold's risk tier ----


def test_combined_tree_scope_carries_parent_risk_tier(tmp_path: Path) -> None:
    """A ``minimum_risk`` medium declared check must run in the combined tree
    when the parent fold is at medium, so it can no longer be skipped as
    ``risk_below_minimum`` and reappear as a permanent ``combined_tree``
    blocker in the parent."""

    _declare(
        tmp_path,
        [
            {
                "id": "med-check",
                "kind": "test",
                "command": ["python3", "-c", "raise SystemExit(0)"],
                "paths": ["src/*.py"],
                "minimum_risk": "medium",
            }
        ],
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("value = 1\n", encoding="utf-8")

    scoped = quality_evidence.run_completion_quality_gate(
        tmp_path,
        changed_paths=["src/x.py"],
        requested_risk_tier="medium",
        combined_tree_scope=True,
    )
    med = next(c for c in scoped["checks"] if c["check_id"] == "med-check")
    # The medium check RAN in the combined tree (not risk_below_minimum-skipped).
    assert med["status"] == "passed"
    # The sub-gate does not recursively demand its own combined tree/reviewers.
    assert scoped["passed"] is True
    assert scoped["blocking_checks"] == []

    # Feeding the scoped rows into a medium parent fold produces no permanent
    # risk_below_minimum -> combined_tree blocker.
    profile = quality_evidence.resolve_risk_profile("medium")
    verdict = quality_evidence.fold_quality_verdict(
        [],
        risk_profile=profile,
        combined_tree_checks=[dict(row) for row in scoped["checks"]],
    )
    assert "combined_tree:med-check" not in verdict["blocking_evidence"]


def test_low_tier_combined_tree_would_skip_and_block(tmp_path: Path) -> None:
    """Regression witness: dropping the parent tier (low sub-gate) skips the
    medium check as risk_below_minimum, which the parent fold then blocks --
    exactly the permanent blocker fix #6 removes."""

    _declare(
        tmp_path,
        [
            {
                "id": "med-check",
                "kind": "test",
                "command": ["python3", "-c", "raise SystemExit(0)"],
                "paths": ["src/*.py"],
                "minimum_risk": "medium",
            }
        ],
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("value = 1\n", encoding="utf-8")

    low_scoped = quality_evidence.run_completion_quality_gate(
        tmp_path,
        changed_paths=["src/x.py"],
        requested_risk_tier="low",
        combined_tree_scope=True,
    )
    med = next(c for c in low_scoped["checks"] if c["check_id"] == "med-check")
    assert med["status"] == "skipped"
    assert med["summary"] == "risk_below_minimum:low<medium"

    profile = quality_evidence.resolve_risk_profile("medium")
    verdict = quality_evidence.fold_quality_verdict(
        [],
        risk_profile=profile,
        combined_tree_checks=[dict(row) for row in low_scoped["checks"]],
    )
    assert "combined_tree:med-check" in verdict["blocking_evidence"]


# --- fix #7: a candidate cannot weaken its own quality policy ----------------


def test_emptying_quality_policy_escalates_tier(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    _declare(
        canonical,
        [
            {"id": "a", "kind": "test", "command": ["python3", "-c", "pass"]},
            {"id": "b", "kind": "lint", "command": ["python3", "-c", "pass"]},
        ],
    )
    _declare(candidate, [])  # candidate empties its own policy

    report = quality_evidence.assess_quality_policy_authority(canonical, candidate)
    assert report["weakened"] is True
    assert report["action"] == "escalate_risk_tier"
    assert (
        report["escalation_signal"]
        == quality_evidence.QUALITY_POLICY_SELF_WEAKENED_SIGNAL
    )
    assert report["canonical_declared_checks"] == 2
    assert report["candidate_declared_checks"] == 0
    assert report["reason"]

    # The escalation signal floors the effective tier at high, so the gate
    # then demands combined-tree and reviewer evidence rather than passing.
    profile = quality_evidence.resolve_risk_profile(
        "low", signals=[report["escalation_signal"]]
    )
    assert profile["effective_tier"] == "high"


def test_removing_quality_policy_is_also_weakening(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    _declare(
        canonical,
        [{"id": "a", "kind": "test", "command": ["python3", "-c", "pass"]}],
    )
    (candidate / "src").mkdir(parents=True)  # candidate has no quality.json at all

    report = quality_evidence.assess_quality_policy_authority(canonical, candidate)
    assert report["weakened"] is True
    assert report["candidate_declared_checks"] == 0
    assert report["blocks_gate"] is False


def test_unchanged_policy_is_not_weakened(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    checks = [
        {"id": "a", "kind": "test", "command": ["python3", "-c", "pass"]},
        {"id": "b", "kind": "lint", "command": ["python3", "-c", "pass"]},
    ]
    _declare(canonical, checks)
    _declare(candidate, checks)

    report = quality_evidence.assess_quality_policy_authority(canonical, candidate)
    assert report["weakened"] is False
    assert report["action"] == "none"
    assert report["escalation_signal"] == ""
