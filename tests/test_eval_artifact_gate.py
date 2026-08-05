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

    related = quality_evidence.run_builtin_static_checks(
        tmp_path, changed_paths=["eval/summary.json"]
    )
    check = next(row for row in related if row.check_id == "builtin:eval_artifact_truth")
    assert check.status == quality_evidence.STATUS_FAILED
