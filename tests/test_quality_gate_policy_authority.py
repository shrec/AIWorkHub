from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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


def test_sparse_omission_inherits_verified_unchanged_canonical_policy(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    sparse = tmp_path / "sparse"
    _declare(canonical, [{"id": "test", "kind": "test", "command": [sys.executable, "-c", "pass"]}])
    sparse.mkdir()

    # Without delta provenance, two-root comparison remains deletion-sensitive.
    assert quality_evidence.assess_quality_policy_authority(canonical, sparse)["weakened"] is True
    report = quality_evidence.assess_quality_policy_authority(
        canonical, sparse, changed_paths=["src/feature.py", "tests/test_feature.py"]
    )
    assert report["weakened"] is False
    assert report["candidate_policy_source"] == "canonical_unchanged"
    assert report["candidate_declared_checks"] == report["canonical_declared_checks"] == 1
    assert report["escalation_signal"] == ""
    assert not (sparse / ".aiworkhub" / "quality.json").exists()


@pytest.mark.parametrize("deleted_path", [
    ".aiworkhub/quality.json", "./.aiworkhub/quality.json", r".aiworkhub\quality.json", ".aiworkhub",
])
def test_verified_policy_deletion_does_not_inherit(tmp_path: Path, deleted_path: str) -> None:
    canonical = tmp_path / "canonical"
    sparse = tmp_path / "sparse"
    _declare(canonical, [{"id": "test", "kind": "test", "command": [sys.executable, "-c", "pass"]}])
    sparse.mkdir()
    report = quality_evidence.assess_quality_policy_authority(
        canonical, sparse, changed_paths=["src/feature.py", deleted_path]
    )
    assert report["weakened"] is True
    assert report["candidate_policy_source"] == "candidate"
    assert report["candidate_declared_checks"] == 0
    profile = quality_evidence.resolve_risk_profile("low", signals=[report["escalation_signal"]])
    assert profile["explicit_human_approval_required"] is True


@pytest.mark.parametrize("candidate_checks", [
    [],
    [{"id": "test", "kind": "test", "command": [sys.executable, "-c", "pass"]}],
    [{"id": "test", "kind": "test", "command": [sys.executable, "-m", "pytest"], "paths": ["docs/**"]}],
    [{"id": "test", "kind": "test", "command": [sys.executable, "-m", "pytest"], "minimum_risk": "critical"}],
])
def test_present_policy_never_uses_sparse_omission_exception(tmp_path: Path, candidate_checks: list[dict]) -> None:
    canonical = tmp_path / "canonical"
    sparse = tmp_path / "sparse"
    _declare(canonical, [{"id": "test", "kind": "test", "command": [sys.executable, "-m", "pytest"]}])
    _declare(sparse, candidate_checks)
    report = quality_evidence.assess_quality_policy_authority(
        canonical, sparse, changed_paths=["src/feature.py"]
    )
    assert report["weakened"] is True
    assert report["candidate_policy_source"] == "candidate"


@pytest.mark.parametrize("bad_node", ["malformed", "directory", "file_symlink", "parent_symlink"])
def test_invalid_policy_is_not_treated_as_sparse_omission(tmp_path: Path, bad_node: str) -> None:
    canonical = tmp_path / "canonical"
    sparse = tmp_path / "sparse"
    _declare(canonical, [{"id": "test", "kind": "test", "command": [sys.executable, "-c", "pass"]}])
    sparse.mkdir()
    policy = sparse / ".aiworkhub" / "quality.json"
    if bad_node == "parent_symlink":
        try:
            policy.parent.symlink_to(tmp_path / "missing", target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation unavailable")
    else:
        policy.parent.mkdir()
        if bad_node == "malformed":
            policy.write_text("[broken", encoding="utf-8")
        elif bad_node == "directory":
            policy.mkdir()
        else:
            try:
                policy.symlink_to(tmp_path / "missing")
            except OSError:
                pytest.skip("symlink creation unavailable")
    report = quality_evidence.assess_quality_policy_authority(
        canonical, sparse, changed_paths=["src/feature.py"]
    )
    assert report["weakened"] is None
    assert report["outcome"] == "unable_to_compare"
    assert report["candidate_policy_source"] == "candidate"
    gate = quality_evidence.run_completion_quality_gate(sparse)
    assert gate["passed"] is False
    assert gate["config_error"]


@pytest.mark.parametrize("union_value, passes", [("candidate", True), ("bad", False)])
def test_inherited_policy_executes_against_combined_tree_not_canonical(
    tmp_path: Path, union_value: str, passes: bool,
) -> None:
    canonical = tmp_path / "canonical"
    union = tmp_path / "union"
    _declare(canonical, [{
        "id": "candidate-check", "kind": "test", "command": [sys.executable, "-c",
            "from pathlib import Path; assert Path('value.txt').read_text() == 'candidate'"],
    }])
    (canonical / "value.txt").write_text("canonical", encoding="utf-8")
    union.mkdir()
    (union / "value.txt").write_text(union_value, encoding="utf-8")
    gate = quality_evidence.run_completion_quality_gate(
        union, policy_root=canonical, changed_paths=["value.txt"], combined_tree_scope=True,
    )
    assert gate["passed"] is passes
    assert gate["verification_scope"] == "repository_policy"
    check = next(row for row in gate["checks"] if row["check_id"] == "candidate-check")
    assert check["status"] == ("passed" if passes else "failed")
    assert not (union / ".aiworkhub" / "quality.json").exists()


def test_malformed_inherited_canonical_policy_blocks_union(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    union = tmp_path / "union"
    _declare(canonical, [])
    (canonical / ".aiworkhub" / "quality.json").write_text("[broken", encoding="utf-8")
    union.mkdir()
    report = quality_evidence.assess_quality_policy_authority(
        canonical, union, changed_paths=["src/feature.py"]
    )
    assert report["candidate_policy_source"] == "canonical_unchanged"
    assert report["canonical_config_readable"] is False
    gate = quality_evidence.run_completion_quality_gate(union, policy_root=canonical, combined_tree_scope=True)
    assert gate["passed"] is False
    assert gate["config_error"]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_acceptance_enforces_omitted_policy_in_union_before_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int,
) -> None:
    # Reuse the existing authenticated retained-attempt fixture. Only union
    # materialization is substituted; risk resolution, policy selection and
    # both quality gates execute the real acceptance path and real commands.
    from test_aiworkhub_accept_review_input_hash_guard_b919 import _FakeWorkspace, _fixture

    from aiworkhub import process_launcher

    (
        manager, _card, request_id, task_id, _runner, _topic, canonical,
        sparse, promote_calls, accept_calls,
    ) = _fixture(monkeypatch, tmp_path)
    _declare(canonical, [{
        "id": "canonical-policy-check", "kind": "test", "command": [sys.executable, "-c",
            "from pathlib import Path; "
            "assert Path('dep/report.json').is_file(); "
            "assert Path('out/result.txt').read_text() == 'worker-result\\n'; "
            "Path('policy-executed.txt').write_text('union'); "
            f"raise SystemExit({exit_code})"],
    }])
    union = tmp_path / "union"
    union_calls: list[list[str]] = []

    def materialize(workspace, card, changed):
        assert workspace.path == sparse
        union_calls.append(list(changed))
        (union / "dep").mkdir(parents=True)
        (union / "dep/report.json").write_bytes((canonical / "dep/report.json").read_bytes())
        (union / "out").mkdir()
        (union / "out/result.txt").write_bytes((sparse / "out/result.txt").read_bytes())
        return _FakeWorkspace(canonical, "union-request", union, union), {"candidate_paths": list(changed)}

    monkeypatch.setattr(process_launcher, "create_combined_validation_workspace", materialize)
    result = manager.accept_review(request_id, task_id)

    assert union_calls == [["out/result.txt"]]
    assert (union / "policy-executed.txt").read_text() == "union"
    assert not (canonical / "policy-executed.txt").exists()
    assert not (sparse / "policy-executed.txt").exists()
    if exit_code:
        assert result["ok"] is False
        prefix = "revalidation_failed:combined_tree_quality_failed:"
        assert result["error"].startswith(prefix)
        failure = json.loads(result["error"][len(prefix):])
        assert failure["blockers"] == ["canonical-policy-check"]
        assert len(failure["failed_checks"]) == 1
        assert failure["failed_checks"][0]["check_id"] == "canonical-policy-check"
        assert failure["failed_checks"][0]["status"] == "failed"
        assert not promote_calls
        assert not accept_calls
    else:
        assert result["ok"] is True, result
        assert promote_calls == [["out/result.txt"]]
        assert len(accept_calls) == 1
        gate = accept_calls[0]["evidence"]["quality_gate"]
        assert gate["quality_policy_authority"]["candidate_policy_source"] == "canonical_unchanged"
        assert gate["risk_profile"]["effective_tier"] == "low"


def test_acceptance_rejects_malformed_quality_blockers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_aiworkhub_accept_review_input_hash_guard_b919 import _fixture

    manager, _card, request_id, task_id, _runner, _topic, _canonical, _sparse, promote_calls, accept_calls = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        quality_evidence, "run_completion_quality_gate",
        lambda *args, **kwargs: {"passed": False, "blocking_checks": object()},
    )
    result = manager.accept_review(request_id, task_id)
    assert result["ok"] is False
    assert result["error"] == "revalidation_failed:quality_gate_failed:invalid_blocking_checks"
    assert not promote_calls
    assert not accept_calls
