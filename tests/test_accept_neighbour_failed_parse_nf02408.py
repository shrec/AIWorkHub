"""NF-2026-02408: a neighbour refusal names the failing tests or carries the output."""

from __future__ import annotations

import json

from aiworkhub.accept_neighbour_tests import (
    NEIGHBOUR_FAILURE_DETAIL_CHARS,
    NEIGHBOUR_FAILURE_PREFIX,
    _failed_tests,
    _refusal,
)


def _detail(exc: Exception) -> dict:
    text = str(exc)
    assert text.startswith(NEIGHBOUR_FAILURE_PREFIX)
    body = text[len(NEIGHBOUR_FAILURE_PREFIX) :]
    assert len(body) <= NEIGHBOUR_FAILURE_DETAIL_CHARS
    return json.loads(body)


def test_ansi_coloured_summary_line_is_parsed() -> None:
    row = {"stdout_tail": "\x1b[31mFAILED\x1b[0m tests/a.py::t - assert 1 == 2\n"}
    failed, first = _failed_tests([row])
    assert failed == ["tests/a.py::t"]
    assert first.startswith("FAILED tests/a.py::t")


def test_xdist_worker_prefix_is_parsed() -> None:
    row = {"stdout_tail": "[gw1] FAILED tests/a.py::t\n[gw3] [ 50%] ERROR tests/b.py::u\n"}
    failed, _first = _failed_tests([row])
    assert failed == ["tests/a.py::t", "tests/b.py::u"]


def test_plain_summary_line_still_parsed_from_stderr() -> None:
    failed, _first = _failed_tests([{"stderr_tail": "FAILED tests/c.py::v - boom\n"}])
    assert failed == ["tests/c.py::v"]


def test_red_run_without_node_id_carries_output_tail() -> None:
    rows = [
        {
            "returncode": 1,
            "stdout_tail": "\x1b[31mcollection exploded\x1b[0m\n",
            "stderr_tail": "Traceback: ImportError",
        }
    ]
    detail = _detail(_refusal(["tests/a.py"], rows, ""))
    assert detail["failed"] == []
    assert "collection exploded" in detail["output_tail"]
    assert "ImportError" in detail["output_tail"]
    assert "\x1b" not in detail["output_tail"]
    assert detail["first_failure"]


def test_named_failures_do_not_add_output_tail() -> None:
    rows = [{"returncode": 1, "stdout_tail": "FAILED tests/a.py::t\n"}]
    detail = _detail(_refusal(["tests/a.py"], rows, ""))
    assert detail["failed"] == ["tests/a.py::t"]
    assert "output_tail" not in detail


def test_oversized_output_tail_is_trimmed_not_the_json() -> None:
    rows = [{"returncode": 1, "stdout_tail": "x\n" * 6000, "stderr_tail": "é" * 5000}]
    tests = [f"tests/test_{i}.py" for i in range(40)]
    detail = _detail(_refusal(tests, rows, "", base_red_refused="r" * 800))
    assert detail["tests"] == tests
    assert detail["output_tail"]
    assert len(detail["output_tail"]) <= 3000


def test_long_failed_ids_and_escaped_texts_keep_the_hard_bound() -> None:
    # Review fix: once tail and tests are gone, failed / first_failure /
    # base_red_refused alone (JSON-escaped non-ASCII) must still fit the bound.
    nodes = [f"tests/test_{i}.py::t[{'é' * 600}]" for i in range(20)]
    rows = [{"returncode": 1, "stdout_tail": "".join(f"FAILED {n}\n" for n in nodes)}]
    detail = _detail(_refusal(["tests/a.py"], rows, "", base_red_refused="é" * 800))
    assert detail["failed"] == [nodes[0]]
    assert detail["first_failure"].startswith("FAILED tests/test_0.py::t[")
