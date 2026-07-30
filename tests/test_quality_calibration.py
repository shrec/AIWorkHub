from __future__ import annotations

from aiworkhub import quality_calibration


def test_every_quality_verdict_blocker_family_is_calibrated() -> None:
    report = quality_calibration.run_calibration()

    assert report["ok"] is True
    assert report["case_count"] >= 25
    assert report["positive_count"] >= 3
    assert report["negative_count"] >= 22
    assert report["false_green_count"] == 0
    assert report["false_red_count"] == 0
    assert report["false_green_rate"] == 0.0
    assert report["false_red_rate"] == 0.0
    assert report["uncalibrated_cases"] == []
    assert all(row["calibrated"] for row in report["cases"])


def test_reviewer_overflow_is_a_blocker_not_silent_truncation() -> None:
    report = quality_calibration.run_calibration()
    row = next(case for case in report["cases"] if case["case_id"] == "reviewer_report_overflow")

    assert row["actual_pass"] is False
    assert row["expected_blocker_observed"] is True
    assert "reviewer_schema:too_many_reports" in row["blocking_evidence"]
