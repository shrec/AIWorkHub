from __future__ import annotations

import json

import pytest

from aiworkhub import quality_evidence as qe


def _check(
    check_id: str = "tests",
    *,
    kind: str = "test",
    status: str = qe.STATUS_PASSED,
    summary: str = "",
) -> qe.EvidenceCheck:
    return qe.EvidenceCheck(
        check_id=check_id,
        kind=kind,
        status=status,
        summary=summary,
    )


def _report(
    lens: str,
    *,
    provider: str = "reviewer-b",
    findings: list[dict[str, str]] | None = None,
    **extra: object,
) -> dict[str, object]:
    return {
        "lens": lens,
        "provider": provider,
        "read_only": True,
        "can_mutate_repo": False,
        "findings": findings or [],
        **extra,
    }


def _finding(
    finding_id: str = "bug-1",
    *,
    severity: str = qe.SEVERITY_HIGH,
    disposition: str | None = None,
) -> dict[str, str]:
    finding = {
        "id": finding_id,
        "severity": severity,
        "summary": "boundary behavior is incorrect",
        "evidence": "tests/test_boundary.py::test_empty does not exercise the changed branch",
    }
    if disposition is not None:
        finding["disposition"] = disposition
    return finding


def test_low_risk_good_mechanical_evidence_is_verified() -> None:
    verdict = qe.fold_quality_verdict([_check()])

    assert verdict["status"] == "verified"
    assert verdict["passed"] is True
    assert verdict["blocking_evidence"] == []
    assert verdict["risk_profile"]["effective_tier"] == "low"


def test_mechanical_failure_is_always_blocking() -> None:
    verdict = qe.fold_quality_verdict([_check(status=qe.STATUS_FAILED)])

    assert verdict["passed"] is False
    assert verdict["blocking_evidence"] == ["tests"]


def test_unavailable_required_mechanical_evidence_is_never_passed() -> None:
    verdict = qe.fold_quality_verdict(
        [_check("security", kind="security", status=qe.STATUS_NOT_AVAILABLE)]
    )

    assert verdict["passed"] is False
    assert verdict["blocking_evidence"] == ["security"]


def test_risk_signals_can_raise_but_never_lower_requested_tier() -> None:
    raised = qe.resolve_risk_profile("low", signals=["security_sensitive"])
    retained = qe.resolve_risk_profile("high", signals=["public_api"])
    released = qe.resolve_risk_profile("low", signals=["release"])

    assert raised["effective_tier"] == "high"
    assert retained["effective_tier"] == "high"
    assert released["effective_tier"] == "critical"


def test_risk_signals_are_derived_from_exact_card_and_changed_paths() -> None:
    signals = qe.derive_risk_signals(
        {
            "task_type": "code",
            "validation": ["pytest -q"],
        },
        ["src/aiworkhub/runtime_adapters.py", "tests/test_runtime_adapters.py"],
    )

    assert "code_change" in signals
    assert "combined_change" in signals
    assert "public_api" in signals
    profile = qe.resolve_risk_profile("low", signals=signals)
    assert profile["effective_tier"] == "medium"
    assert profile["required_reviewer_lenses"] == [qe.LENS_CORRECTNESS]


def test_missing_validation_and_destructive_diff_raise_automatic_floor() -> None:
    signals = qe.derive_risk_signals(
        {"task_type": "code", "validation": []},
        ["src/aiworkhub/core.py"],
        destructive_checks=[_check("destructive", status=qe.STATUS_FAILED)],
    )

    assert "missing_validation" in signals
    assert "destructive_change" in signals
    assert qe.resolve_risk_profile("low", signals=signals)["effective_tier"] == "high"


def test_unknown_risk_input_fails_closed() -> None:
    with pytest.raises(qe.MalformedConfigError):
        qe.resolve_risk_profile("tiny")
    with pytest.raises(qe.MalformedConfigError):
        qe.resolve_risk_profile("low", signals=["worker_says_safe"])


def test_medium_risk_requires_correctness_review_and_combined_tree() -> None:
    profile = qe.resolve_risk_profile("medium")
    verdict = qe.fold_quality_verdict([_check()], risk_profile=profile)

    assert verdict["passed"] is False
    assert "required_reviewer_missing:correctness" in verdict["blocking_evidence"]
    assert "combined_tree_evidence_missing" in verdict["blocking_evidence"]


def test_medium_risk_passes_with_read_only_review_and_green_union() -> None:
    profile = qe.resolve_risk_profile("medium")
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[_report(qe.LENS_CORRECTNESS)],
        combined_tree_checks=[_check("union-tests")],
        worker_provider="worker-a",
    )

    assert verdict["passed"] is True
    assert verdict["blocking_evidence"] == []


def test_high_risk_same_provider_review_records_fresh_context_rung() -> None:
    # Before: this asserted ``verdict["passed"] is False`` and that every
    # judgment lens carried ``independent_reviewer_missing`` -- a same-provider
    # reviewer was refused purely for sharing the worker's vendor. After the
    # ladder conversion that vendor refusal is gone: a same-provider (here also
    # single-model) review degrades to the same_model_fresh_context rung and is
    # accepted, and the achieved rung is recorded on the verdict and each lens.
    profile = qe.resolve_risk_profile("high")
    reports = [
        _report(lens, provider="worker-a")
        for lens in sorted(qe.JUDGMENT_LENSES)
    ]
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=reports,
        combined_tree_checks=[_check("union-tests")],
        worker_provider="worker-a",
        human_approval=True,
    )

    assert verdict["passed"] is True
    assert not any(
        b.startswith("independent_reviewer_missing")
        for b in verdict["blocking_evidence"]
    )
    for lens in sorted(qe.JUDGMENT_LENSES):
        assert (
            verdict["independence_rungs"][lens]["rung"]
            == qe.quality_review.RUNG_SAME_MODEL_FRESH_CONTEXT
        )
        row = next(r for r in verdict["lenses"] if r["lens"] == lens)
        assert (
            row["independence_rung"]
            == qe.quality_review.RUNG_SAME_MODEL_FRESH_CONTEXT
        )


def test_high_risk_cross_provider_clean_reports_are_verified() -> None:
    profile = qe.resolve_risk_profile("high")
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[_report(lens) for lens in sorted(qe.JUDGMENT_LENSES)],
        combined_tree_checks=[_check("union-tests")],
        worker_provider="worker-a",
        human_approval=True,
    )

    assert verdict["passed"] is True
    assert verdict["status"] == "verified"


def test_high_risk_clean_evidence_still_requires_explicit_approval() -> None:
    profile = qe.resolve_risk_profile("high")
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[_report(lens) for lens in sorted(qe.JUDGMENT_LENSES)],
        combined_tree_checks=[_check("union-tests")],
        worker_provider="worker-a",
    )

    assert verdict["passed"] is False
    assert "explicit_human_approval_missing" in verdict["blocking_evidence"]


def test_model_supplied_pass_cannot_override_blocking_finding() -> None:
    verdict = qe.fold_quality_verdict(
        [_check()],
        reviewer_reports=[
            _report(
                qe.LENS_CORRECTNESS,
                findings=[_finding()],
                verdict="PASS",
                passed=True,
            )
        ],
    )

    assert verdict["passed"] is False
    assert verdict["blocking_evidence"] == ["reviewer:correctness:bug-1"]
    assert "verdict" not in verdict["reviewer_reports"][0]


def test_nonblocking_correctness_finding_still_requires_refinement() -> None:
    verdict = qe.fold_quality_verdict(
        [_check()],
        reviewer_reports=[
            _report(
                qe.LENS_CORRECTNESS,
                findings=[_finding(severity=qe.SEVERITY_LOW)],
            )
        ],
    )

    assert verdict["passed"] is False
    assert verdict["refine_required"] is True
    assert verdict["blocking_evidence"] == [
        "refinement_required:reviewer:correctness:bug-1"
    ]


@pytest.mark.parametrize("disposition", ["observation", "process_limit"])
def test_low_nonactionable_reviewer_item_is_retained_without_blocking(
    disposition: str,
) -> None:
    verdict = qe.fold_quality_verdict(
        [_check()],
        reviewer_reports=[
            _report(
                qe.LENS_CORRECTNESS,
                findings=[_finding(
                    severity=qe.SEVERITY_LOW,
                    disposition=disposition,
                )],
            )
        ],
    )

    assert verdict["passed"] is True
    assert verdict["refine_required"] is False
    assert verdict["blocking_evidence"] == []
    correctness = next(
        row for row in verdict["lenses"] if row["lens"] == qe.LENS_CORRECTNESS
    )
    assert correctness["finding_ids"] == []
    assert correctness["observation_ids"] == ["reviewer:correctness:bug-1"]
    finding = verdict["reviewer_reports"][0]["findings"][0]
    assert finding["disposition"] == disposition
    assert finding["actionable"] is False


def test_nondefect_reviewer_item_cannot_hide_medium_or_higher_severity() -> None:
    verdict = qe.fold_quality_verdict(
        [_check()],
        reviewer_reports=[
            _report(
                qe.LENS_SECURITY,
                findings=[_finding(
                    severity=qe.SEVERITY_MEDIUM,
                    disposition="observation",
                )],
            )
        ],
    )

    assert verdict["passed"] is False
    assert verdict["blocking_evidence"] == [
        "reviewer_schema:0:0:nondefect_severity_must_be_low"
    ]


def test_malformed_or_mutating_reviewer_report_fails_closed() -> None:
    report = _report(qe.LENS_SECURITY)
    report["can_mutate_repo"] = True
    verdict = qe.fold_quality_verdict([_check()], reviewer_reports=[report])

    assert verdict["passed"] is False
    assert verdict["blocking_evidence"] == ["reviewer_schema:0:not_read_only"]


def test_required_combined_tree_generic_skipped_is_blocking() -> None:
    profile = qe.resolve_risk_profile("medium")
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[_report(qe.LENS_CORRECTNESS)],
        combined_tree_checks=[_check("union", status=qe.STATUS_SKIPPED)],
    )

    assert verdict["passed"] is False
    assert verdict["blocking_evidence"] == ["combined_tree:union"]


def test_required_combined_tree_changed_paths_not_applicable_is_nonblocking() -> None:
    profile = qe.resolve_risk_profile("medium")
    verdict = qe.fold_quality_verdict(
        [_check()],
        risk_profile=profile,
        reviewer_reports=[_report(qe.LENS_CORRECTNESS)],
        combined_tree_checks=[
            _check(
                "union",
                status=qe.STATUS_SKIPPED,
                summary="changed_paths_not_applicable",
            )
        ],
    )

    assert verdict["passed"] is True
    assert verdict["blocking_evidence"] == []


def test_completion_gate_exposes_same_pure_verdict_without_breaking_low_risk(tmp_path) -> None:
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = qe.run_completion_quality_gate(tmp_path, changed_paths=["good.py"])

    assert packet["passed"] is True
    assert packet["quality_verdict"]["passed"] is True
    assert packet["risk_profile"]["effective_tier"] == "low"
    assert packet["blocking_checks"] == packet["quality_verdict"]["blocking_evidence"]


def test_completion_gate_fail_closes_invalid_requested_risk(tmp_path) -> None:
    (tmp_path / "good.py").write_text("value = 1\n", encoding="utf-8")

    packet = qe.run_completion_quality_gate(
        tmp_path,
        changed_paths=["good.py"],
        requested_risk_tier="worker-invented",
    )

    assert packet["passed"] is False
    assert packet["blocking_checks"] == ["quality_verdict_schema_error"]


def test_public_contract_names_pure_verdict_and_all_profiles() -> None:
    contract = qe.quality_verdict_contract()

    assert contract["verdict_owner"] == "pure_deterministic_fold"
    assert contract["model_verdict_accepted"] is False
    assert contract["quality_lenses"] == list(qe.QUALITY_LENSES)
    assert list(contract["profiles"]) == list(qe.RISK_TIERS)


def test_negative_fixture_matrix_good_green_each_bad_red(tmp_path) -> None:
    """Deterministic gate trust: reference passes; each targeted defect fails."""

    good = tmp_path / "good"
    syntax_bad = tmp_path / "syntax_bad"
    unavailable_bad = tmp_path / "unavailable_bad"
    for root in (good, syntax_bad, unavailable_bad):
        root.mkdir()
    (good / "module.py").write_text("answer = 42\n", encoding="utf-8")
    (syntax_bad / "module.py").write_text("def broken(:\n", encoding="utf-8")
    (unavailable_bad / "module.py").write_text("answer = 42\n", encoding="utf-8")
    (unavailable_bad / ".aiworkhub").mkdir()
    (unavailable_bad / ".aiworkhub" / "quality.json").write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "id": "required-security",
                        "kind": "security",
                        "command": ["not-a-real-quality-command"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    matrix = {
        "good": qe.run_completion_quality_gate(good, changed_paths=["module.py"]),
        "bad_syntax": qe.run_completion_quality_gate(
            syntax_bad, changed_paths=["module.py"]
        ),
        "bad_unavailable": qe.run_completion_quality_gate(
            unavailable_bad, changed_paths=["module.py"]
        ),
    }

    assert matrix["good"]["passed"] is True
    assert matrix["bad_syntax"]["passed"] is False
    assert matrix["bad_unavailable"]["passed"] is False


@pytest.mark.parametrize(
    "path",
    [
        "scripts/build.sh",
        "scripts/build.bash",
        "scripts/build.zsh",
        "scripts/module.mjs",
        "scripts/module.cjs",
        "scripts/build.ps1",
        "scripts/BUILD.SH",
    ],
)
def test_script_and_module_suffixes_are_code_changes(path: str) -> None:
    signals = qe.derive_risk_signals(
        {"task_type": "docs", "validation": ["pytest -q"]},
        [path],
    )

    assert "code_change" in signals


def test_production_project_context_code_task_cannot_bypass_validation_floor() -> None:
    signals = qe.derive_risk_signals(
        {"project_context": {"task_type": "code"}},
        ["scripts/run_stdio.sh"],
    )

    assert {"code_change", "missing_validation"} <= set(signals)
    profile = qe.resolve_risk_profile("low", signals=signals)
    assert profile["effective_tier"] != qe.RISK_LOW
    assert profile["required_reviewer_lenses"]


def test_explicit_non_code_task_type_overrides_project_context() -> None:
    signals = qe.derive_risk_signals(
        {
            "task_type": "docs",
            "project_context": {"task_type": "code"},
        },
        ["README.md"],
    )

    assert "code_change" not in signals
    assert "missing_validation" not in signals
