"""NF-2026-01848: bounded pytest failure digest on failed validation rows.

SURFACE ONLY: the digest names WHICH tests failed; it never changes a verdict.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import completion_inbox, validation_runner  # noqa: E402

_SCHEMA = "aiworkhub.pytest_failure_digest.v1"


def _digest(stdout: str, returncode: int = 1):
    return validation_runner.pytest_validation_evidence(stdout, returncode).get(
        "pytest_failure_digest"
    )


def test_short_summary_names_all_failures_and_totals():
    stdout = (
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_a.py::test_one - assert 1 == 2\n"
        "FAILED tests/test_a.py::test_two[x y] - ValueError: boom\n"
        "FAILED tests/test_b.py::test_three\n"
        "ERROR tests/test_c.py::test_four - RuntimeError: setup\n"
        "=================== 3 failed, 1 error, 10 passed in 1.20s ====================\n"
    )
    digest = _digest(stdout)
    assert digest["schema"] == _SCHEMA
    assert digest["failures"] == [
        {"kind": "failed", "test": "tests/test_a.py::test_one", "message": "assert 1 == 2"},
        {"kind": "failed", "test": "tests/test_a.py::test_two[x y]", "message": "ValueError: boom"},
        {"kind": "failed", "test": "tests/test_b.py::test_three", "message": ""},
        {"kind": "error", "test": "tests/test_c.py::test_four", "message": "RuntimeError: setup"},
    ]
    assert digest["totals"] == "3 failed, 1 error, 10 passed in 1.20s"
    assert digest["omitted"] == 0


def test_headers_with_first_e_line_when_no_short_summary():
    stdout = (
        "=================================== FAILURES ===================================\n"
        "__________________________________ test_one ____________________________________\n"
        "    def test_one():\n"
        ">       assert 1 == 2\n"
        "E       assert 1 == 2\n"
        "E       second line\n"
        "__________________________________ test_two ____________________________________\n"
        "E   KeyError: 'k'\n"
        "=================== 2 failed in 0.10s ====================\n"
    )
    digest = _digest(stdout)
    assert [(f["kind"], f["test"], f["message"]) for f in digest["failures"]] == [
        ("failed", "test_one", "assert 1 == 2"),
        ("failed", "test_two", "KeyError: 'k'"),
    ]
    assert digest["totals"] == "2 failed in 0.10s"


def test_headers_without_totals_still_populate_digest():
    stdout = (
        "=================================== ERRORS =====================================\n"
        "_______________________ ERROR collecting tests/test_x.py _______________________\n"
        "E   ImportError: nope\n"
    )
    digest = _digest(stdout)
    assert digest["failures"] == [
        {"kind": "error", "test": "ERROR collecting tests/test_x.py", "message": "ImportError: nope"}
    ]
    assert digest["totals"] is None


def test_more_than_40_failures_are_clipped_and_messages_bounded():
    lines = ["=========================== short test summary info ============================"]
    for index in range(45):
        lines.append(f"FAILED tests/test_m.py::test_{index} - " + "m" * 500)
    lines.append("=================== 45 failed in 2.00s ====================")
    digest = _digest("\n".join(lines) + "\n")
    assert len(digest["failures"]) == 40
    assert digest["omitted"] == 5
    assert all(len(f["message"]) <= 300 for f in digest["failures"])
    assert digest["failures"][0]["test"] == "tests/test_m.py::test_0"


def test_green_run_has_no_digest_and_outcomes_unchanged():
    stdout = "FAILED tests/x.py::t - not real\n3 passed in 0.10s\n"
    result = validation_runner.pytest_validation_evidence(stdout, 0)
    assert "pytest_failure_digest" not in result
    assert result == {"pytest_outcomes": {"passed": 3}}


def test_unparseable_failed_run_adds_nothing():
    assert validation_runner.pytest_validation_evidence("boom\n", 1) == {}


def _rows(count: int):
    digest = {
        "schema": _SCHEMA,
        "failures": [{"kind": "failed", "test": "t::a", "message": "m"}],
        "totals": "1 failed in 0.10s",
        "omitted": 0,
    }
    return [
        {
            "declared_command": f"pytest tests/test_{index}.py",
            "returncode": 1,
            "stdout_tail": "x" * 4000,
            "stderr_tail": "y" * 4000,
            "pytest_first_failure": "z" * 2000,
            "pytest_failure_digest": digest,
        }
        for index in range(count)
    ]


def test_packet_keeps_digest_after_tail_drop():
    packet = completion_inbox.review_packet(
        "R-DIGEST",
        card={
            "task_id": "T-DIGEST",
            "terminal_review": {"evidence": {"validation": _rows(5)}},
        },
        task_id="T-DIGEST",
        max_bytes=20_000,
    )
    assert "validation_output_tails" in packet["truncation"]["dropped_sections"]
    for row in packet["gates"]["validation"]:
        assert row["stdout_tail"] == ""
        assert "pytest_first_failure" not in row
        assert row["pytest_failure_digest"]["failures"][0]["test"] == "t::a"
        assert row["pytest_failure_digest"]["totals"] == "1 failed in 0.10s"
