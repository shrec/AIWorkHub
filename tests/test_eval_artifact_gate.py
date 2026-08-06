from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import eval_artifact_gate, quality_evidence


def _write_registry(root: Path) -> None:
    config = root / ".aiworkhub"
    config.mkdir()
    (config / "eval-artifacts.json").write_text(
        json.dumps({
            "artifacts": [{
                "id": "demo",
                "summary_path": "eval/summary.json",
                "rows_path": "eval/rows.jsonl",
                "row_format": "jsonl",
                "required_row_fields": ["request_id", "bytes"],
                "minimum_rows": 1,
                "count_fields": ["record_count"],
                "aggregates": [{
                    "summary_field": "total_bytes",
                    "row_field": "bytes",
                    "operation": "sum",
                }],
            }],
        }),
        encoding="utf-8",
    )


def test_zero_eligible_rows_can_never_pass(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "summary.json").write_text(
        json.dumps({"verdict": "PASS", "record_count": 0, "total_bytes": 0}),
        encoding="utf-8",
    )
    (eval_dir / "rows.jsonl").write_text(
        json.dumps({"measurement_status": "no_records"}) + "\n",
        encoding="utf-8",
    )

    report = eval_artifact_gate.evaluate(tmp_path)

    assert report["passed"] is False
    assert report["artifacts"][0]["status"] == "inconclusive"
    assert "demo:pass_without_eligible_rows" in report["blocking_reasons"]


def test_registered_counts_and_aggregates_are_recomputed(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "summary.json").write_text(
        json.dumps({"verdict": "PASS", "record_count": 2, "total_bytes": 7}),
        encoding="utf-8",
    )
    (eval_dir / "rows.jsonl").write_text(
        json.dumps({"request_id": "a", "bytes": 3}) + "\n"
        + json.dumps({"request_id": "b", "bytes": 4}) + "\n",
        encoding="utf-8",
    )

    report = eval_artifact_gate.evaluate(tmp_path)

    assert report["passed"] is True
    assert report["artifacts"][0]["eligible_row_count"] == 2


def test_changed_path_scoping_and_quality_floor_integration(tmp_path: Path) -> None:
    _write_registry(tmp_path)
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "summary.json").write_text(
        json.dumps({"verdict": "PASS", "record_count": 0, "total_bytes": 0}),
        encoding="utf-8",
    )
    (eval_dir / "rows.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "module.py").write_text("value = 1\n", encoding="utf-8")

    unrelated = quality_evidence.run_builtin_static_checks(
        tmp_path, changed_paths=["module.py"]
    )
    unrelated_check = next(
        row for row in unrelated if row.check_id == "builtin:eval_artifact_truth"
    )
    assert unrelated_check.status == quality_evidence.STATUS_SKIPPED
    assert unrelated_check.summary == "changed_paths_not_applicable"

    related = quality_evidence.run_builtin_static_checks(
        tmp_path, changed_paths=["eval/summary.json"]
    )
    check = next(row for row in related if row.check_id == "builtin:eval_artifact_truth")
    assert check.status == quality_evidence.STATUS_FAILED


def _passing_check() -> quality_evidence.EvidenceCheck:
    return quality_evidence.EvidenceCheck(
        check_id="pass",
        kind="static_analysis",
        status=quality_evidence.STATUS_PASSED,
    )


def test_non_applicable_eval_artifact_does_not_block_combined_tree() -> None:
    profile = quality_evidence.resolve_risk_profile("medium")
    combined_checks = [
        quality_evidence.EvidenceCheck(
            check_id="builtin:eval_artifact_truth",
            kind="requirements",
            status=quality_evidence.STATUS_SKIPPED,
            summary="changed_paths_not_applicable",
        )
    ]
    verdict = quality_evidence.fold_quality_verdict(
        [_passing_check()],
        risk_profile=profile,
        combined_tree_checks=combined_checks,
    )
    assert not any(
        blocker.startswith("combined_tree:")
        for blocker in verdict["blocking_evidence"]
    )


def test_generic_skipped_eval_artifact_still_blocks() -> None:
    profile = quality_evidence.resolve_risk_profile("medium")
    combined_checks = [
        quality_evidence.EvidenceCheck(
            check_id="builtin:eval_artifact_truth",
            kind="requirements",
            status=quality_evidence.STATUS_SKIPPED,
            summary="some other reason",
        )
    ]
    verdict = quality_evidence.fold_quality_verdict(
        [_passing_check()],
        risk_profile=profile,
        combined_tree_checks=combined_checks,
    )
    assert (
        "combined_tree:builtin:eval_artifact_truth"
        in verdict["blocking_evidence"]
    )


def test_failed_eval_artifact_always_blocks() -> None:
    profile = quality_evidence.resolve_risk_profile("medium")
    combined_checks = [
        quality_evidence.EvidenceCheck(
            check_id="builtin:eval_artifact_truth",
            kind="requirements",
            status=quality_evidence.STATUS_FAILED,
            summary="eval_artifact_evidence_diverged",
        )
    ]
    verdict = quality_evidence.fold_quality_verdict(
        [_passing_check()],
        risk_profile=profile,
        combined_tree_checks=combined_checks,
    )
    assert (
        "combined_tree:builtin:eval_artifact_truth"
        in verdict["blocking_evidence"]
    )
