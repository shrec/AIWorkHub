"""NF-2026-01155: pytest outcome counts and first-failure excerpt surfacing.

Worker validation records only a returncode and a 4096-char head/tail per
command, so a pytest run that skips most of its tests reads as a clean pass
and a failing run loses its traceback to the tail cut. These tests cover the
pure parser (``validation_runner.pytest_validation_evidence``) and its
forwarding into ``completion_inbox.review_packet``. SURFACE ONLY: nothing
here asserts a pass/fail/environment-blocked verdict.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import completion_inbox, validation_runner  # noqa: E402

# ---------------------------------------------------------------------------
# validation_runner.pytest_validation_evidence
# ---------------------------------------------------------------------------


def test_passed_and_skipped_summary_line():
    assert validation_runner.pytest_validation_evidence(
        "51 passed, 46 skipped in 3.20s", 0
    ) == {"pytest_outcomes": {"passed": 51, "skipped": 46}}


def test_decorated_summary_line_with_failed_and_warning():
    stdout = "==== 2 failed, 165 passed, 4 skipped, 1 warning in 12.01s ===="
    result = validation_runner.pytest_validation_evidence(stdout, 1)
    assert result["pytest_outcomes"] == {
        "failed": 2,
        "passed": 165,
        "skipped": 4,
        "warnings": 1,
    }


def test_single_error_summary_line():
    assert validation_runner.pytest_validation_evidence("1 error in 0.10s", 1) == {
        "pytest_outcomes": {"errors": 1}
    }


def test_no_tests_ran_gives_empty_outcomes_dict():
    assert validation_runner.pytest_validation_evidence(
        "no tests ran in 0.01s", 0
    ) == {"pytest_outcomes": {}}


def test_non_pytest_output_returns_empty_dict_and_never_raises():
    assert validation_runner.pytest_validation_evidence("All checks passed!", 0) == {}
    assert validation_runner.pytest_validation_evidence("", None) == {}
    assert (
        validation_runner.pytest_validation_evidence("\x00\x01binary\xffgarbage\x02", 1)
        == {}
    )


def test_last_final_summary_line_wins():
    stdout = (
        "1 passed in 0.01s\n"
        "some noise about in progress\n"
        "3 failed, 1 passed in 0.50s\n"
    )
    result = validation_runner.pytest_validation_evidence(stdout, 1)
    assert result["pytest_outcomes"] == {"failed": 3, "passed": 1}


def test_first_failure_excerpt_excludes_second_failure():
    stdout = (
        "=================================== FAILURES ===================================\n"
        "__________________________________ test_one ____________________________________\n"
        "\n"
        "    def test_one():\n"
        ">       assert False\n"
        "E       assert False\n"
        "__________________________________ test_two ____________________________________\n"
        "\n"
        "    def test_two():\n"
        ">       assert 1 == 2\n"
        "E       assert 1 == 2\n"
        "=========================== short test summary info ============================\n"
        "2 failed in 0.20s\n"
    )
    result = validation_runner.pytest_validation_evidence(stdout, 1)
    first_failure = result["pytest_first_failure"]
    assert "test_one" in first_failure
    assert "E       assert False" in first_failure
    assert "test_two" not in first_failure


def test_first_failure_absent_when_returncode_is_zero():
    stdout = (
        "=================================== FAILURES ===================================\n"
        "__________________________________ test_one ____________________________________\n"
        "E       assert False\n"
        "1 failed in 0.20s\n"
    )
    assert "pytest_first_failure" not in validation_runner.pytest_validation_evidence(
        stdout, 0
    )


def test_first_failure_block_over_4000_chars_is_bounded():
    body = "\n".join(f"line {i} " + "z" * 60 for i in range(120))
    stdout = (
        "=================================== FAILURES ===================================\n"
        "__________________________________ test_one ____________________________________\n"
        f"{body}\n"
        "=========================== short test summary info ============================\n"
        "1 failed in 0.20s\n"
    )
    result = validation_runner.pytest_validation_evidence(stdout, 1)
    first_failure = result["pytest_first_failure"]
    assert len(first_failure) <= 4000 + len("\n...[truncated]...\n")
    assert first_failure.count("...[truncated]...") == 1


# ---------------------------------------------------------------------------
# completion_inbox.review_packet wiring
# ---------------------------------------------------------------------------


def _card(validation_rows):
    return {
        "task_id": "T-PYTEST-EVIDENCE",
        "terminal_review": {"evidence": {"validation": validation_rows}},
    }


def test_packet_row_carries_pytest_outcomes_and_first_failure():
    packet = completion_inbox.review_packet(
        "R-PYTEST-EVIDENCE",
        card=_card(
            [
                {
                    "declared_command": "pytest tests/",
                    "returncode": 1,
                    "pytest_outcomes": {"failed": 1, "passed": 2, "skipped": 3},
                    "pytest_first_failure": "____ test_one ____\nE   assert False",
                    "stdout_tail": "1 failed, 2 passed, 3 skipped in 0.10s",
                }
            ]
        ),
        task_id="T-PYTEST-EVIDENCE",
    )
    row = packet["gates"]["validation"][0]
    assert row["pytest_outcomes"] == {"failed": 1, "passed": 2, "skipped": 3}
    assert row["pytest_first_failure"] == "____ test_one ____\nE   assert False"
    assert packet["validation_skipped_total"] == 3


def test_packet_row_derives_outcomes_for_legacy_row_without_the_key():
    packet = completion_inbox.review_packet(
        "R-PYTEST-EVIDENCE",
        card=_card(
            [
                {
                    "declared_command": "pytest tests/",
                    "returncode": 0,
                    "stdout_tail": "5 passed, 2 skipped in 0.10s",
                }
            ]
        ),
        task_id="T-PYTEST-EVIDENCE",
    )
    row = packet["gates"]["validation"][0]
    assert row["pytest_outcomes"] == {"passed": 5, "skipped": 2}
    assert packet["validation_skipped_total"] == 2


def test_packet_omits_outcomes_for_a_non_pytest_row():
    packet = completion_inbox.review_packet(
        "R-PYTEST-EVIDENCE",
        card=_card(
            [
                {
                    "declared_command": "ruff check src",
                    "returncode": 0,
                    "stdout_tail": "All checks passed!",
                }
            ]
        ),
        task_id="T-PYTEST-EVIDENCE",
    )
    row = packet["gates"]["validation"][0]
    assert "pytest_outcomes" not in row
    assert packet["validation_skipped_total"] == 0


def test_validation_skipped_total_sums_across_rows():
    packet = completion_inbox.review_packet(
        "R-PYTEST-EVIDENCE",
        card=_card(
            [
                {
                    "declared_command": "pytest a",
                    "returncode": 0,
                    "pytest_outcomes": {"passed": 1, "skipped": 2},
                },
                {
                    "declared_command": "pytest b",
                    "returncode": 0,
                    "pytest_outcomes": {"passed": 4, "skipped": 5},
                },
            ]
        ),
        task_id="T-PYTEST-EVIDENCE",
    )
    assert packet["validation_skipped_total"] == 7


def test_byte_budget_drop_keeps_outcomes_and_total_but_drops_first_failure():
    rows = [
        {
            "declared_command": f"pytest tests/test_{index}.py",
            "returncode": 1,
            "stdout_tail": "x" * 4000,
            "stderr_tail": "y" * 4000,
            "pytest_outcomes": {"failed": 1, "skipped": 2},
            "pytest_first_failure": "z" * 2000,
        }
        for index in range(5)
    ]
    packet = completion_inbox.review_packet(
        "R-PYTEST-EVIDENCE",
        card=_card(rows),
        task_id="T-PYTEST-EVIDENCE",
        max_bytes=20_000,
    )
    assert packet["encoded_bytes"] <= 20_000
    assert "validation_output_tails" in packet["truncation"]["dropped_sections"]
    rows_after = packet["gates"]["validation"]
    assert len(rows_after) == 5
    for row in rows_after:
        assert "pytest_first_failure" not in row
        assert row["pytest_outcomes"] == {"failed": 1, "skipped": 2}
    assert packet["validation_skipped_total"] == 10


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected_outcomes"),
    [
        (
            "257 passed, 12 skipped in 75.27s (0:01:15)",
            0,
            {"passed": 257, "skipped": 12},
        ),
        (
            "======= 3 failed, 254 passed, 12 skipped in 122.04s (0:02:02) =======",
            1,
            {"failed": 3, "passed": 254, "skipped": 12},
        ),
        (
            "51 passed, 46 skipped in 3.20s",
            0,
            {"passed": 51, "skipped": 46},
        ),
    ],
)
def test_summary_line_with_hms_duration_suffix_still_parses(
    stdout, returncode, expected_outcomes
):
    assert validation_runner.pytest_validation_evidence(stdout, returncode) == {
        "pytest_outcomes": expected_outcomes
    }


# ---------------------------------------------------------------------------
# Rework 2: unbounded digit run / non-int pytest_outcomes values (security)
# ---------------------------------------------------------------------------


def test_summary_count_over_nine_digits_does_not_raise():
    assert (
        validation_runner.pytest_validation_evidence("9" * 5000 + " passed in 1.0s\n", 0)
        == {}
    )


def test_summary_count_nine_digits_still_parses():
    assert validation_runner.pytest_validation_evidence(
        "123456789 passed in 1.0s\n", 0
    ) == {"pytest_outcomes": {"passed": 123456789}}


def test_packet_row_filters_non_int_pytest_outcomes_values():
    rows = completion_inbox._packet_validation_rows(
        {
            "validation": [
                {
                    "command": "pytest",
                    "returncode": 0,
                    "pytest_outcomes": {
                        "passed": 3,
                        "skipped": "abc",
                        "errors": [1],
                        "failed": None,
                        "xfailed": True,
                    },
                }
            ]
        }
    )
    assert len(rows) == 1
    assert rows[0]["pytest_outcomes"] == {"passed": 3}


def test_packet_row_keeps_real_int_skipped_count():
    rows = completion_inbox._packet_validation_rows(
        {
            "validation": [
                {
                    "command": "pytest",
                    "returncode": 0,
                    "pytest_outcomes": {
                        "passed": 3,
                        "skipped": 4,
                        "errors": [1],
                        "failed": None,
                        "xfailed": True,
                    },
                }
            ]
        }
    )
    assert len(rows) == 1
    assert rows[0]["pytest_outcomes"] == {"passed": 3, "skipped": 4}
