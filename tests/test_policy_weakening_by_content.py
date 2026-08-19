"""Regression: self-weakening is detected by CONTENT, not by count.

NF-2026-00343. ``assess_quality_policy_authority`` compared only the NUMBER of
declared checks, so a candidate that keeps the same count but replaces every
command with a no-op, narrows the paths, or raises minimum_risk reported
``weakened=False``. Worse, an unreadable canonical config was swallowed as
``weakened=False`` -- the one state in which comparison is impossible was
reported as "no weakening detected". These tests pin content comparison
(command identity, path scope, minimum_risk) and the distinct
``unable_to_compare`` outcome.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import quality_evidence as qe


def _declare(root: Path, checks: list[dict]) -> None:
    config = root / ".aiworkhub" / "quality.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"checks": checks}), encoding="utf-8")


REAL_CHECKS = [
    {"id": "a", "kind": "test", "command": ["python3", "-m", "pytest", "-q"]},
    {"id": "b", "kind": "security", "command": ["python3", "-m", "bandit", "-r", "src"]},
]


def test_same_count_but_noop_commands_and_widened_bar_is_weakened(
    tmp_path: Path,
) -> None:
    # The count is unchanged (two -> two) but the commands are replaced with a
    # no-op, paths are narrowed to docs/**, and minimum_risk is raised to
    # critical. Content comparison must still detect the hollowed-out policy.
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    _declare(canonical, REAL_CHECKS)
    _declare(
        candidate,
        [
            {
                "id": "a",
                "kind": "test",
                "command": ["true"],
                "paths": ["docs/**"],
                "minimum_risk": "critical",
            },
            {
                "id": "b",
                "kind": "security",
                "command": ["true"],
                "paths": ["docs/**"],
                "minimum_risk": "critical",
            },
        ],
    )

    report = qe.assess_quality_policy_authority(canonical, candidate)

    assert report["weakened"] is True
    assert report["outcome"] == "weakened"
    assert report["action"] == "escalate_risk_tier"
    assert report["escalation_signal"] == qe.QUALITY_POLICY_SELF_WEAKENED_SIGNAL
    assert report["canonical_declared_checks"] == 2
    assert report["candidate_declared_checks"] == 2
    assert report["reason"]

    # The escalation signal floors the effective tier so the bare candidate
    # cannot pass on an emptied policy.
    profile = qe.resolve_risk_profile("low", signals=[report["escalation_signal"]])
    assert qe._RISK_RANK[profile["effective_tier"]] >= qe._RISK_RANK["high"]


def test_narrowing_paths_alone_is_weakened(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    _declare(
        canonical,
        [{"id": "a", "kind": "test", "command": ["python3", "-m", "pytest"]}],
    )
    # Same command, but now scoped to a narrow subtree -> runs far less often.
    _declare(
        candidate,
        [
            {
                "id": "a",
                "kind": "test",
                "command": ["python3", "-m", "pytest"],
                "paths": ["docs/**"],
            }
        ],
    )

    report = qe.assess_quality_policy_authority(canonical, candidate)

    assert report["weakened"] is True


def test_raising_minimum_risk_alone_is_weakened(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    _declare(
        canonical,
        [{"id": "a", "kind": "test", "command": ["python3", "-m", "pytest"]}],
    )
    _declare(
        candidate,
        [
            {
                "id": "a",
                "kind": "test",
                "command": ["python3", "-m", "pytest"],
                "minimum_risk": "critical",
            }
        ],
    )

    report = qe.assess_quality_policy_authority(canonical, candidate)

    assert report["weakened"] is True


def test_broadening_scope_and_adding_checks_is_not_weakened(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    _declare(
        canonical,
        [
            {
                "id": "a",
                "kind": "test",
                "command": ["python3", "-m", "pytest"],
                "paths": ["src/**"],
            }
        ],
    )
    # The candidate broadens 'a' to all paths and adds an extra check: strictly
    # stronger, never weaker.
    _declare(
        candidate,
        [
            {"id": "a", "kind": "test", "command": ["python3", "-m", "pytest"]},
            {"id": "extra", "kind": "lint", "command": ["ruff", "check"]},
        ],
    )

    report = qe.assess_quality_policy_authority(canonical, candidate)

    assert report["weakened"] is False
    assert report["outcome"] == "preserved"
    assert report["action"] == "none"
    assert report["escalation_signal"] == ""


def test_identical_policy_is_not_weakened(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    _declare(canonical, REAL_CHECKS)
    _declare(candidate, REAL_CHECKS)

    report = qe.assess_quality_policy_authority(canonical, candidate)

    assert report["weakened"] is False
    assert report["outcome"] == "preserved"


def test_malformed_canonical_config_is_unable_to_compare_never_weakened_false(
    tmp_path: Path,
) -> None:
    # A broken canonical config makes comparison impossible. That is its own
    # outcome and must never read as "no weakening detected".
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    (canonical / ".aiworkhub").mkdir(parents=True)
    (canonical / ".aiworkhub" / "quality.json").write_text(
        "{ not: valid json", encoding="utf-8"
    )
    _declare(candidate, [{"id": "a", "kind": "test", "command": ["true"]}])

    report = qe.assess_quality_policy_authority(canonical, candidate)

    assert report["outcome"] == "unable_to_compare"
    assert report["weakened"] is not False
    assert report["weakened"] is None
    assert report["canonical_config_readable"] is False
    assert report["escalation_signal"] == ""
    assert report["action"] == "none"


def test_malformed_candidate_config_is_unable_to_compare(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    _declare(
        canonical,
        [{"id": "a", "kind": "test", "command": ["python3", "-m", "pytest"]}],
    )
    (candidate / ".aiworkhub").mkdir(parents=True)
    # A top-level JSON array is not a valid quality.json object.
    (candidate / ".aiworkhub" / "quality.json").write_text("[]", encoding="utf-8")

    report = qe.assess_quality_policy_authority(canonical, candidate)

    assert report["outcome"] == "unable_to_compare"
    assert report["weakened"] is None
    assert report["candidate_config_readable"] is False
