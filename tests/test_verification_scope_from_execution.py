"""Regression: verification_scope is derived from checks that EXECUTED.

NF-2026-00342. ``verification_scope`` was computed from
``declared_check_count > 0`` alone, so a run in which every declared check was
skipped still claimed ``repository_policy`` -- asserting the repository's policy
governed a verdict while not one of that policy's checks ran. These tests pin
the corrected behavior: only a run where at least one declared check actually
executed reports ``repository_policy`` scope.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import quality_evidence as qe


def _declare(root: Path, checks: list[dict]) -> None:
    config = root / ".aiworkhub" / "quality.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"checks": checks}), encoding="utf-8")


def test_all_skipped_run_via_risk_below_minimum_is_not_repository_policy(
    tmp_path: Path,
) -> None:
    # A declared check whose minimum_risk (high) is above the effective tier
    # (low) is skipped as risk_below_minimum. The policy is configured but none
    # of it ran, so the scope must not claim repository_policy.
    _declare(
        tmp_path,
        [
            {
                "id": "high-only-security",
                "kind": "security",
                "command": ["python3", "-c", "raise SystemExit(0)"],
                "minimum_risk": "high",
            }
        ],
    )
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = qe.run_completion_quality_gate(tmp_path, changed_paths=["good.py"])

    declared = next(
        row for row in packet["checks"] if row["check_id"] == "high-only-security"
    )
    assert declared["status"] == "skipped"
    assert declared["summary"] == "risk_below_minimum:low<high"
    # The policy IS present/configured...
    assert packet["repository_quality_policy"]["status"] == "configured"
    # ...but nothing it declared executed, so scope is not repository_policy.
    assert packet["verification_scope"] == "builtin_and_task_contract_only"
    # A silently-skipped declared check does not block, but neither does it
    # let the run pretend the policy governed it.
    assert packet["passed"] is True


def test_all_skipped_run_via_changed_paths_not_applicable_is_not_repository_policy(
    tmp_path: Path,
) -> None:
    _declare(
        tmp_path,
        [
            {
                "id": "extension-only",
                "kind": "test",
                "command": ["python3", "-c", "raise SystemExit(0)"],
                "paths": ["vscode-extension/**"],
            }
        ],
    )
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = qe.run_completion_quality_gate(tmp_path, changed_paths=["good.py"])

    declared = next(
        row for row in packet["checks"] if row["check_id"] == "extension-only"
    )
    assert declared["status"] == "skipped"
    assert declared["summary"] == "changed_paths_not_applicable"
    assert packet["repository_quality_policy"]["status"] == "configured"
    assert packet["verification_scope"] == "builtin_and_task_contract_only"


def test_executed_declared_check_restores_repository_policy_scope(
    tmp_path: Path,
) -> None:
    # The distinguishing property is EXECUTION, not the mere presence of a
    # policy: a declared check that actually runs reports repository_policy.
    _declare(
        tmp_path,
        [
            {
                "id": "always-security",
                "kind": "security",
                "command": ["python3", "-c", "raise SystemExit(0)"],
            }
        ],
    )
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = qe.run_completion_quality_gate(tmp_path, changed_paths=["good.py"])

    declared = next(
        row for row in packet["checks"] if row["check_id"] == "always-security"
    )
    assert declared["status"] == "passed"
    assert packet["verification_scope"] == "repository_policy"


def test_no_declared_policy_is_builtin_and_task_contract_only(tmp_path: Path) -> None:
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = qe.run_completion_quality_gate(tmp_path, changed_paths=["good.py"])

    assert packet["repository_quality_policy"]["status"] == "unverified"
    assert packet["verification_scope"] == "builtin_and_task_contract_only"
