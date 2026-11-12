"""Tests for the deterministic validation-output distiller."""

from __future__ import annotations

import time

from aiworkhub.validation_output_distill import (
    _repo_relative,
    distill_validation_output,
)

_TRANSCRIPT = (
    "============================= test session starts ==============================\n"
    "collected 3 items\n\n"
    "tests/test_foo.py F                                                    [ 33%]\n"
    "tests/test_bar.py F                                                    [ 66%]\n"
    "tests/test_baz.py .                                                    [100%]\n\n"
    "=================================== FAILURES ===================================\n"
    "____________________________ test_foo_does_a_thing ____________________________\n\n"
    "    def test_foo_does_a_thing():\n"
    ">       assert 1 == 2\n"
    "E       assert 1 == 2\n\n"
    "tests/test_foo.py:12: AssertionError\n\n"
    "/usr/lib/python3.11/site-packages/_pytest/runner.py:150: in run\n"
    "    something()\n"
    "/usr/lib/python3.11/site-packages/pluggy/_hooks.py:200: in call\n"
    "    hook()\n"
    "____________________________ test_bar_raises_value_error ____________________________\n\n"
    "    def test_bar_raises_value_error():\n"
    ">       raise ValueError(\"boom\")\n"
    "E       ValueError: boom\n\n"
    "tests/test_bar.py:34: ValueError\n\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_foo.py::test_foo_does_a_thing - assert 1 == 2\n"
    "FAILED tests/test_bar.py::test_bar_raises_value_error - ValueError: boom\n"
    "========================= 1 passed, 2 failed in 0.12s ===========================\n"
)


def test_distills_counts_and_failing_node_ids() -> None:
    result = distill_validation_output(
        command="python -m pytest -q", exit_status=1, output=_TRANSCRIPT
    )

    assert result.distilled is True
    assert result.exit_status == 1
    assert result.counts.get("passed") == 1
    assert result.counts.get("failed") == 2
    assert "tests/test_foo.py::test_foo_does_a_thing" in result.failing_node_ids
    assert "tests/test_bar.py::test_bar_raises_value_error" in result.failing_node_ids


def test_repo_frames_survive_and_non_repo_frames_are_dropped() -> None:
    result = distill_validation_output(
        command="python -m pytest -q", exit_status=1, output=_TRANSCRIPT
    )

    foo_frames = result.frames_by_node["tests/test_foo.py::test_foo_does_a_thing"]
    assert any("tests/test_foo.py:12" in frame for frame in foo_frames)
    assert not any("site-packages" in frame for frame in foo_frames)
    assert not any("pluggy" in frame for frame in foo_frames)


def test_assertion_summary_preserved() -> None:
    result = distill_validation_output(
        command="python -m pytest -q", exit_status=1, output=_TRANSCRIPT
    )

    summaries = result.assertion_summaries
    assert "tests/test_bar.py::test_bar_raises_value_error" in summaries
    assert "boom" in summaries["tests/test_bar.py::test_bar_raises_value_error"]


def test_distilled_output_smaller_than_raw_with_all_node_ids_surviving() -> None:
    big_transcript = _TRANSCRIPT * 20
    result = distill_validation_output(
        command="python -m pytest -q", exit_status=1, output=big_transcript
    )

    assert len(result.text) < len(big_transcript)
    assert "tests/test_foo.py::test_foo_does_a_thing" in result.failing_node_ids
    assert "tests/test_bar.py::test_bar_raises_value_error" in result.failing_node_ids


def test_non_zero_exit_never_reported_as_pass() -> None:
    result = distill_validation_output(
        command="python -m pytest -q", exit_status=1, output=_TRANSCRIPT
    )

    assert result.exit_status != 0
    assert result.failing_node_ids


def test_unparseable_output_falls_back_to_raw_tail_with_distilled_false() -> None:
    garbage = "not a pytest transcript at all, just noise\n" * 5
    result = distill_validation_output(
        command="python -m pytest -q", exit_status=1, output=garbage
    )

    assert result.distilled is False
    assert "distilled: False" in result.text
    assert result.raw_sha256
    assert result.raw_byte_length == len(garbage.encode("utf-8"))


def test_raw_sha256_and_byte_length_always_recorded() -> None:
    result = distill_validation_output(
        command="python -m pytest -q", exit_status=0, output=_TRANSCRIPT
    )

    assert len(result.raw_sha256) == 64
    assert result.raw_byte_length == len(_TRANSCRIPT.encode("utf-8"))


def test_failure_signal_without_parseable_node_id_falls_back_to_raw() -> None:
    raw = "collected 2 items\n" + "." * 50 + "\n1 failed, 1 passed in 0.1s\n"
    result = distill_validation_output(command="pytest", exit_status=1, output=raw)

    assert result.distilled is False
    assert raw in result.text


def test_location_and_assertion_lines_are_kept_per_failure() -> None:
    result = distill_validation_output(
        command="python -m pytest -q", exit_status=1, output=_TRANSCRIPT
    )

    assert "tests/test_foo.py:12: AssertionError" in result.text
    assert "tests/test_bar.py:34: ValueError" in result.text
    assert "E       assert 1 == 2" in result.text
    assert "E       ValueError: boom" in result.text


def test_non_repository_frames_kept_when_they_are_the_only_frames() -> None:
    raw = (
        "____ test_only_lib ____\n\n"
        "E       RuntimeError: boom\n\n"
        "/usr/lib/python3.11/site-packages/_pytest/runner.py:150: RuntimeError\n"
        "FAILED tests/test_x.py::test_only_lib - RuntimeError: boom\n"
        "=== 1 failed in 0.1s ===\n"
    )
    result = distill_validation_output(command="pytest", exit_status=1, output=raw)

    assert result.distilled is True
    frames = result.frames_by_node["tests/test_x.py::test_only_lib"]
    assert frames and "runner.py:150" in frames[0]


def test_distilled_text_is_deterministic_and_carries_no_host_path() -> None:
    raw = _TRANSCRIPT.replace(
        "tests/test_foo.py:12:", "/home/someone/repo/tests/test_foo.py:12:"
    )
    first = distill_validation_output(command="pytest", exit_status=1, output=raw)
    second = distill_validation_output(command="pytest", exit_status=1, output=raw)

    assert first.text == second.text
    assert "/home/someone" not in first.text
    assert "tests/test_foo.py:12: AssertionError" in first.text


def test_realistic_multi_failure_transcript_is_smaller_and_keeps_every_node() -> None:
    count = 6
    nodes = [f"tests/test_mod_{i}.py::test_case_{i}" for i in range(count)]
    parts = ["collected 200 items\n"]
    parts += ["." * 80 + " [ 50%]\n"] * 3
    parts.append("=== FAILURES ===\n")
    for i in range(count):
        parts.append(f"____ test_case_{i} ____\n\n")
        parts.append(f"    def test_case_{i}():\n>       assert {i} == {i + 1}\n")
        parts.append(f"E       assert {i} == {i + 1}\n\n")
        parts.append(f"tests/test_mod_{i}.py:{10 + i}: AssertionError\n")
        for depth in range(2):
            parts.append(
                f"/venv/lib/python3.11/site-packages/_pytest/python.py:{depth}: in call\n"
                "    result = hook()\n"
            )
    parts.append("=== warnings summary ===\n" + "DeprecationWarning: x\n" * 10)
    parts.append("=== short test summary info ===\n")
    parts += [f"FAILED {node} - AssertionError\n" for node in nodes]
    parts.append(f"=== {count} failed, {200 - count} passed in 3.2s ===\n")
    raw = "".join(parts)
    assert len(raw) <= 4096

    result = distill_validation_output(command="pytest", exit_status=1, output=raw)

    assert result.distilled is True
    assert len(result.text.encode()) < len(raw.encode())
    for i, node in enumerate(nodes):
        assert node in result.text
        assert f"tests/test_mod_{i}.py:{10 + i}" in result.text
    assert "site-packages" not in result.text


def _summary_only(*nodes: str, counts: str) -> str:
    return "".join(f"FAILED {node} - boom\n" for node in nodes) + counts + "\n"


def test_parametrize_id_with_spaces_keeps_the_failing_node() -> None:
    raw = _summary_only("tests/test_a.py::test_a[x y]", counts="=== 1 failed in 0.1s ===")
    result = distill_validation_output(command="pytest", exit_status=1, output=raw)

    assert result.distilled is True
    assert result.failing_node_ids == ["tests/test_a.py::test_a[x y]"]


def test_count_mismatch_with_extracted_nodes_fails_open_to_raw() -> None:
    raw = _summary_only("tests/test_a.py::test_a", counts="=== 2 failed in 0.1s ===")
    result = distill_validation_output(command="pytest", exit_status=1, output=raw)

    assert result.distilled is False
    assert raw in result.text


def test_counts_come_only_from_the_final_summary_line() -> None:
    raw = "E       AssertionError: expected 7 passed, 9 failed in 1s\n" + _summary_only(
        "tests/test_a.py::test_a", counts="=== 1 failed, 3 passed in 0.1s ==="
    )
    result = distill_validation_output(command="pytest", exit_status=1, output=raw)

    assert result.distilled is True
    assert result.counts == {"failed": 1, "passed": 3}


def test_same_function_name_in_two_modules_attaches_no_frames_or_e_lines() -> None:
    raw = (
        "____ test_x ____\n\n"
        "E       assert 1 == 2\n\n"
        "tests/test_one.py:5: AssertionError\n"
        "____ test_x ____\n\n"
        "E       assert 3 == 4\n\n"
        "tests/test_two.py:9: AssertionError\n"
        + _summary_only(
            "tests/test_one.py::test_x",
            "tests/test_two.py::test_x",
            counts="=== 2 failed in 0.1s ===",
        )
    )
    result = distill_validation_output(command="pytest", exit_status=1, output=raw)

    assert result.distilled is True
    assert result.failing_node_ids == [
        "tests/test_one.py::test_x",
        "tests/test_two.py::test_x",
    ]
    assert not any(result.frames_by_node.values())
    assert not result.assertion_summaries
    assert "assert 1 == 2" not in result.text


def test_repo_relative_never_emits_host_prefix_user_or_parent_traversal() -> None:
    assert _repo_relative("/home/u/src/proj/src/aiworkhub/x.py") == "src/aiworkhub/x.py"
    assert _repo_relative("..\\..\\Users\\bob\\AppData\\x.py") == "x.py"
    assert _repo_relative("C:\\Users\\bob\\proj\\tests\\test_x.py") == "tests/test_x.py"
    assert _repo_relative("/opt/other/y.py") == "y.py"
    assert _repo_relative("D:\\work\\z.py") == "z.py"


def test_hostile_input_is_bounded_before_patterns_run() -> None:
    raw = ("___ " + "a " * 50_000 + "x\n") * 3 + "FAILED " + "[" * 20_000
    started = time.monotonic()
    result = distill_validation_output(command="pytest", exit_status=1, output=raw)

    assert time.monotonic() - started < 2.0
    assert result.distilled is False
    assert result.raw_byte_length == len(raw.encode("utf-8"))


def test_warnings_summary_frames_do_not_attach_to_last_failure() -> None:
    transcript = _TRANSCRIPT.replace(
        "=========================== short test summary info",
        "=============================== warnings summary ===============================\n"
        "tests/test_warn.py:7: DeprecationWarning: old api\n\n"
        "=========================== short test summary info",
    )
    text = distill_validation_output(command="pytest", exit_status=1, output=transcript).to_text()
    assert "test_warn.py:7" not in text


def test_windows_stdlib_and_foreign_paths_never_look_repo_relative() -> None:
    assert _repo_relative(r"C:\Python312\Lib\unittest\case.py") == "case.py"
    assert _repo_relative(r"/venv/lib/python3.11/site-packages/pkg/tests/helpers.py") == "helpers.py"
    assert _repo_relative(r"C:Users\bob\x.py") == "x.py"
    assert _repo_relative(r"D:\Dev\AIWorkHub\src\aiworkhub\x.py") == "src/aiworkhub/x.py"
