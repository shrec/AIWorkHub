from __future__ import annotations

import hashlib

import pytest

from aiworkhub import quality_evidence as qe
from aiworkhub import quality_reviewer


def _check(
    check_id: str = "tests",
    *,
    kind: str = "test",
    status: str = qe.STATUS_PASSED,
) -> qe.EvidenceCheck:
    return qe.EvidenceCheck(
        check_id=check_id,
        kind=kind,
        status=status,
        summary="",
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
    finding_id: str = "pl-1",
    *,
    severity: str = qe.SEVERITY_LOW,
    disposition: str = qe.FINDING_DISPOSITION_PROCESS_LIMIT,
) -> dict[str, str]:
    return {
        "id": finding_id,
        "severity": severity,
        "disposition": disposition,
        "summary": "reviewer could not inspect the packet",
        "evidence": "no file-read tool was available for the packet path",
    }


def _medium_verdict(reports: list[dict[str, object]]) -> dict[str, object]:
    return qe.fold_quality_verdict(
        [],
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        reviewer_reports=reports,
        combined_tree_checks=[_check("union-tests")],
    )


def _lens_row(verdict: dict[str, object], lens: str) -> dict[str, object]:
    return next(row for row in verdict["lenses"] if row["lens"] == lens)


def test_all_process_limit_findings_flag_lens_could_not_inspect() -> None:
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, findings=[_finding()])])

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]
    assert (
        _lens_row(verdict, qe.LENS_CORRECTNESS)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )


def test_present_zero_activity_usage_flags_lens_could_not_inspect() -> None:
    usage = {"usage_observed": False, "input_tokens": 0, "output_tokens": 0}
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, usage=usage)])

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]
    assert (
        _lens_row(verdict, qe.LENS_CORRECTNESS)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )


def test_missing_usage_telemetry_still_satisfies_lens() -> None:
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS)])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]
    assert _lens_row(verdict, qe.LENS_CORRECTNESS)["status"] == qe.STATUS_PASSED


def test_genuine_defect_finding_is_not_blind() -> None:
    finding = _finding("defect-1", disposition="defect")
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, findings=[finding])])

    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]
    assert verdict["refine_required"] is True
    assert (
        _lens_row(verdict, qe.LENS_CORRECTNESS)["status"]
        != qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )


def test_genuine_observation_finding_is_not_blind() -> None:
    finding = _finding("obs-1", disposition="observation")
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, findings=[finding])])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]


def test_genuine_zero_findings_review_satisfies_lens() -> None:
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS)])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]
    assert _lens_row(verdict, qe.LENS_CORRECTNESS)["status"] == qe.STATUS_PASSED


def test_low_risk_process_limit_finding_stays_non_blocking() -> None:
    verdict = qe.fold_quality_verdict(
        [],
        risk_profile=qe.resolve_risk_profile(qe.RISK_LOW),
        reviewer_reports=[_report(qe.LENS_CORRECTNESS, findings=[_finding()])],
    )

    assert verdict["passed"] is True
    assert verdict["blocking_evidence"] == []


def test_mixed_process_limit_and_observation_is_not_blind() -> None:
    findings = [
        _finding("pl-1"),
        _finding("obs-1", disposition="observation"),
    ]
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, findings=findings)])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]


def test_zero_activity_usage_with_no_findings_flags_blind() -> None:
    usage = {"usage_observed": False, "input_tokens": 0, "output_tokens": 0}
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, usage=usage)])

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]


def test_present_nonzero_usage_is_not_blind() -> None:
    usage = {"usage_observed": True, "input_tokens": 120, "output_tokens": 45}
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, usage=usage)])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]


def test_partial_zero_usage_is_not_blind() -> None:
    usage = {"usage_observed": False, "input_tokens": 0, "output_tokens": 5}
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, usage=usage)])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]


def test_usage_observed_true_with_zero_tokens_is_not_blind() -> None:
    usage = {"usage_observed": True, "input_tokens": 0, "output_tokens": 0}
    verdict = _medium_verdict([_report(qe.LENS_CORRECTNESS, usage=usage)])

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]


def test_high_risk_blind_security_lens_is_flagged() -> None:
    reports = [
        _report(qe.LENS_CORRECTNESS),
        _report(qe.LENS_SECURITY, findings=[_finding("pl-sec")]),
        _report(qe.LENS_CODE_QUALITY),
    ]
    verdict = qe.fold_quality_verdict(
        [],
        risk_profile=qe.resolve_risk_profile(qe.RISK_HIGH),
        reviewer_reports=reports,
        combined_tree_checks=[_check("union-tests")],
        worker_provider="worker-a",
        human_approval=True,
    )

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:security" in verdict["blocking_evidence"]
    assert (
        _lens_row(verdict, qe.LENS_SECURITY)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )


def test_normalize_reviewer_reports_drops_usage_key() -> None:
    usage = {"usage_observed": False, "input_tokens": 0, "output_tokens": 0}
    normalized, errors = qe.normalize_reviewer_reports(
        [_report(qe.LENS_CORRECTNESS, usage=usage)]
    )

    assert errors == []
    assert set(normalized[0].keys()) == {
        "lens",
        "provider",
        "read_only",
        "can_mutate_repo",
        "findings",
    }


def test_helper_all_process_limit_findings_true() -> None:
    assert qe.reviewer_report_could_not_inspect(
        {"findings": [{"disposition": "process_limit"}]}
    ) is True


def test_helper_zero_activity_usage_true() -> None:
    assert (
        qe.reviewer_report_could_not_inspect(
            {"usage": {"usage_observed": False, "input_tokens": 0, "output_tokens": 0}}
        )
        is True
    )


def test_helper_empty_findings_false() -> None:
    assert qe.reviewer_report_could_not_inspect({"findings": []}) is False


def test_helper_missing_usage_is_not_blind() -> None:
    assert qe.reviewer_report_could_not_inspect({}) is False


def test_helper_mixed_disposition_false() -> None:
    assert (
        qe.reviewer_report_could_not_inspect(
            {"findings": [{"disposition": "process_limit"}, {"disposition": "defect"}]}
        )
        is False
    )


def test_helper_nonzero_usage_false() -> None:
    assert (
        qe.reviewer_report_could_not_inspect(
            {"usage": {"usage_observed": True, "input_tokens": 1, "output_tokens": 0}}
        )
        is False
    )


def test_helper_non_mapping_usage_is_not_blind() -> None:
    assert qe.reviewer_report_could_not_inspect({"usage": "n/a"}) is False


# ---------------------------------------------------------------------------
# NF-2026-00931: the gate is unchanged.  A reviewer that read the omitted hunks
# through the digest-bound candidate overlay and judged them satisfies its lens;
# a report that files only process_limit -- or a lens whose hunks are not
# provably reachable -- still blocks as reviewer_could_not_inspect.  The packet
# proves that a read was POSSIBLE; it never turns a blind report into a clean one.
# ---------------------------------------------------------------------------

_ALPHA = "src/aiworkhub/alpha_review.py"
_SOUND_HUNK_OBSERVATION = {
    "id": "obs-overlay",
    "severity": qe.SEVERITY_LOW,
    "disposition": "observation",
    "summary": "omitted hunk read through the candidate overlay and found sound",
    "evidence": f"{_ALPHA}:11",
}


def _digest(path: str) -> str:
    return hashlib.sha256(f"candidate bytes of {path}".encode("utf-8")).hexdigest()


def _truncated_diff_packet() -> dict:
    """NF-2026-00930: diff_complete=false, candidate digest sealed, one hunk omitted."""

    segments = [
        {
            "kind": "replace",
            "candidate_start_line": start,
            "candidate_end_line": start + 3,
            "changed_start_line": start,
            "changed_end_line": start + 3,
            "baseline_start_line": start,
            "baseline_end_line": start + 3,
            "excerpt_bytes": 40,
            "truncated": truncated,
        }
        for start, truncated in ((1, False), (11, True))
    ]
    excerpt = "@@ replace @@\n+kept\n"
    return quality_reviewer.build_review_packet(
        request_id="R-NF930",
        task_id="T-NF930",
        claim_epoch=7,
        worker_provider="codex_cli",
        changed_path_hashes={_ALPHA: _digest(_ALPHA)},
        source_evidence={
            _ALPHA: {
                "candidate_sha256": _digest(_ALPHA),
                "excerpt": excerpt,
                "excerpt_bytes": len(excerpt),
                "source_bytes": 8192,
                "truncated": True,
                "diff_complete": False,
                "segments": segments,
                "omission_reason": "changed_hunks_omitted:1",
            }
        },
    )


def _resealed(packet: dict) -> dict:
    body = {key: value for key, value in packet.items() if key != "packet_sha256"}
    return {**body, "packet_sha256": quality_reviewer._canonical_digest(body)}


def _verdict_over(packet: dict, reports: list[dict[str, object]]) -> dict[str, object]:
    return qe.fold_quality_verdict(
        [],
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        reviewer_reports=reports,
        combined_tree_checks=[_check("union-tests")],
        review_packets={qe.LENS_CORRECTNESS: packet},
    )


@pytest.mark.parametrize(
    "findings",
    [[], [_SOUND_HUNK_OBSERVATION]],
    ids=["clean", "sound-hunk-observation"],
)
def test_reviewer_that_read_the_omitted_hunks_through_the_overlay_satisfies_the_lens(
    findings: list[dict[str, str]],
) -> None:
    packet = _truncated_diff_packet()
    assert quality_reviewer.candidate_hunk_inspection_coverage(packet)["complete"] is True
    # The reviewer is told to read those hunks, not to escalate them unread.
    prompt = quality_reviewer.build_review_prompt(packet, lens=qe.LENS_CORRECTNESS)
    assert "OMITTED CHANGED HUNKS." in prompt
    usage = {"usage_observed": True, "input_tokens": 4200, "output_tokens": 310}

    verdict = _verdict_over(
        packet, [_report(qe.LENS_CORRECTNESS, findings=findings, usage=usage)]
    )

    assert verdict["passed"] is True
    assert "reviewer_could_not_inspect:correctness" not in verdict["blocking_evidence"]
    assert _lens_row(verdict, qe.LENS_CORRECTNESS)["status"] == qe.STATUS_PASSED
    assert verdict["supplemental_inspection"] == []


def test_process_limit_only_report_over_a_readable_overlay_still_blocks() -> None:
    verdict = _verdict_over(
        _truncated_diff_packet(), [_report(qe.LENS_CORRECTNESS, findings=[_finding()])]
    )

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]
    assert (
        _lens_row(verdict, qe.LENS_CORRECTNESS)["status"]
        == qe.STATUS_REVIEWER_COULD_NOT_INSPECT
    )
    (plan,) = verdict["supplemental_inspection"]
    assert plan["eligible"] is True
    assert plan["reason"] == ""
    (target,) = plan["inspection_targets"]
    assert target["path"] == _ALPHA
    assert target["authority_source"] == "candidate_overlay"


def test_zero_activity_report_over_a_readable_overlay_is_still_blind() -> None:
    usage = {"usage_observed": False, "input_tokens": 0, "output_tokens": 0}

    verdict = _verdict_over(
        _truncated_diff_packet(), [_report(qe.LENS_CORRECTNESS, usage=usage)]
    )

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]


def _mismatched_digest(packet: dict) -> None:
    packet["candidate"]["source_evidence"][0]["candidate_sha256"] = "f" * 64


def _missing_digest(packet: dict) -> None:
    packet["candidate"]["source_evidence"][0]["candidate_sha256"] = None


def _out_of_delta_path(packet: dict) -> None:
    packet["candidate"]["source_evidence"].append(
        {
            "path": "src/aiworkhub/secret.py",
            "candidate_sha256": None,
            "excerpt": "x",
            "diff_complete": True,
            "segments": [],
        }
    )


@pytest.mark.parametrize(
    ("tamper", "reason", "unreachable"),
    [
        (_mismatched_digest, "candidate_hunks_unreachable", [_ALPHA]),
        (_missing_digest, "candidate_hunks_unreachable", [_ALPHA]),
        (_out_of_delta_path, "candidate_source_evidence_path_mismatch", []),
    ],
    ids=["mismatched-digest", "missing-digest", "out-of-delta-path"],
)
def test_blind_lens_over_an_unproven_overlay_is_refused_a_reread(
    tamper, reason: str, unreachable: list[str]
) -> None:
    packet = _truncated_diff_packet()
    tamper(packet)

    verdict = _verdict_over(
        _resealed(packet), [_report(qe.LENS_CORRECTNESS, findings=[_finding()])]
    )

    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]
    (plan,) = verdict["supplemental_inspection"]
    assert plan["eligible"] is False
    assert plan["reason"] == reason
    assert plan["coverage"]["unreachable"] == unreachable
