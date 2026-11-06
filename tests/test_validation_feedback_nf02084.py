"""NF-2026-02084: marker-based first_failure, every failing case in the rework reason, UTF-8 child output."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    mcp_summary_folds,
    validation_auto_rework as auto,
    validation_failed_packet,
    validation_failure_markers as markers,
    validation_runner,
)

_CATCH2_STDOUT = (
    "Randomness seeded to: 1234\n"
    "\n"
    "-------------------------------------------------------------------------------\n"
    "adds two numbers\n"
    "-------------------------------------------------------------------------------\n"
    "/src/awh/tests/test_math.cpp:10\n"
    "...............................................................................\n"
    "\n"
    "/src/awh/tests/test_math.cpp:12: FAILED:\n"
    "  REQUIRE( add(1, 1) == 3 )\n"
    "with expansion:\n"
    "  2 == 3\n"
    "\n"
    "===============================================================================\n"
    "test cases: 2 | 1 passed | 1 failed\n"
    "assertions: 4 | 3 passed | 1 failed\n"
)
_WARNING = "/src/awh/src/math.cpp:3:9: warning: unused variable 'x' [-Wunused-variable]\n"


def test_first_failure_shows_the_catch2_failed_block_not_the_stderr_warning() -> None:
    row = {"command": "ctest --output-on-failure", "returncode": 8,
           "stderr_tail": _WARNING, "stdout_tail": _CATCH2_STDOUT}
    first = mcp_summary_folds.first_failure_from_status({"validation": [row]}, {})
    assert first["tail_source"] == "stdout"
    assert "/src/awh/tests/test_math.cpp:12: FAILED:" in first["tail"]
    assert "REQUIRE( add(1, 1) == 3 )" in first["tail"]
    assert "adds two numbers" in first["tail"]
    assert "warning:" not in first["tail"]
    assert len(first["tail"].encode("utf-8")) <= mcp_summary_folds.FIRST_FAILURE_TAIL_BYTES


def test_first_failure_marker_vocabulary_and_tail_fallback() -> None:
    for line in (
        "FAILED tests/test_x.py::test_a - assert 1 == 2",
        "______________________________ test_a ______________________________",
        "E   assert 1 == 2",
        "C:\\awh\\tests\\test_math.cpp(12): FAILED:",
        "  3/10 Test  #3: math_tests .......................***Failed    0.02 sec",
        "50% tests passed, 1 tests failed out of 2",
        "[  FAILED  ] MathTest.Adds (0 ms)",
        "/src/awh/src/math.cpp:3:9: error: expected ';' after expression",
    ):
        assert markers.is_failure_marker(line), line
    assert not markers.is_failure_marker(_WARNING.strip())
    # no marker anywhere: the old stderr-first tail is the fallback
    row = {"command": "make", "returncode": 2, "stderr_tail": "make: *** [all] Stop 2", "stdout_tail": "x"}
    first = mcp_summary_folds.first_failure_from_status({"validation": [row]}, {})
    assert (first["tail"], first["tail_source"]) == ("make: *** [all] Stop 2", "stderr")


def _pytest_output(durations: tuple[str, str, str, str]) -> str:
    a, b, c, total = durations
    return (
        "============================= test session starts =============================\n"
        "collected 5 items\n\n"
        "tests/test_x.py F.F.F                                                    [100%]\n\n"
        "================================== FAILURES ===================================\n"
        "___________________________________ test_a ____________________________________\n"
        "E   assert 1 == 2\n"
        f"tests/test_x.py:3: AssertionError ({a}s)\n"
        "___________________________________ test_b ____________________________________\n"
        "E   assert 'a' == 'b'\n"
        f"tests/test_x.py:7: AssertionError ({b}s)\n"
        "___________________________________ test_c ____________________________________\n"
        "E   KeyError: 'k'\n"
        f"tests/test_x.py:11: KeyError ({c}s)\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_x.py::test_a - assert 1 == 2\n"
        "FAILED tests/test_x.py::test_b - AssertionError: assert 'a' == 'b'\n"
        "FAILED tests/test_x.py::test_c - KeyError: 'k'\n"
        f"========================= 3 failed, 2 passed in {total}s =========================\n"
    )


def _packet(stdout: str) -> dict:
    evidence = {"validation": [{"declared_command": "python -m pytest -q tests/test_x.py", "returncode": 1,
                                "stdout_tail": stdout, "stderr_tail": ""}]}
    return validation_failed_packet.packet_from_evidence(
        task_id="T", request_id="req", evidence=evidence, roles=None)


def test_rework_reason_lists_every_failing_case_and_signature_ignores_durations() -> None:
    fast = _packet(_pytest_output(("0.01", "0.02", "0.03", "0.12")))
    slow = _packet(_pytest_output(("1.51", "2.72", "3.93", "45.67")))
    signature = auto.failure_signature(fast)
    assert signature == auto.failure_signature(slow)
    reason = auto.build_reason(fast, signature, 1)
    assert "failing cases:" in reason
    for name, message in (("test_a", "assert 1 == 2"), ("test_b", "AssertionError: assert 'a' == 'b'"),
                          ("test_c", "KeyError: 'k'")):
        assert f"    FAILED tests/test_x.py::{name} - {message}" in reason
    assert len(reason.encode("utf-8")) <= auto.REASON_MAX_BYTES


def test_failing_case_headers_are_bounded_with_an_omitted_count_and_deduplicated() -> None:
    many = "".join(f"FAILED tests/test_x.py::test_{i:02d}\n" for i in range(25))
    headers, omitted = markers.failing_case_headers(many)
    assert headers == [f"FAILED tests/test_x.py::test_{i:02d}" for i in range(markers.CASE_HEADER_LIMIT)]
    assert omitted == 25 - markers.CASE_HEADER_LIMIT
    gtest = ("[  FAILED  ] MathTest.Adds (3 ms)\n[==========] 2 tests ran.\n"
             "[  FAILED  ] 1 test, listed below:\n[  FAILED  ] MathTest.Adds\n")
    assert markers.failing_case_headers(gtest) == (["[  FAILED  ] MathTest.Adds (3 ms)"], 0)
    reason = auto.build_reason(_packet(many), "0" * 64, 1)
    assert f"... {25 - markers.CASE_HEADER_LIMIT} more failing case header(s) omitted" in reason


def test_child_output_round_trips_georgian_text_and_a_check_mark(tmp_path: Path) -> None:
    from aiworkhub import worker_ai_tools_mcp

    expected = "გამარჯობა ✓"
    # the REAL worker-side builder the validation tool spawns the child with
    env = worker_ai_tools_mcp._validation_child_env(None, {}, tmp_path)
    code = (
        "import sys; text = '\\u10d2\\u10d0\\u10db\\u10d0\\u10e0\\u10ef\\u10dd\\u10d1\\u10d0 \\u2713'; "
        "print(text); print(text, file=sys.stderr)"
    )
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=120, check=False)
    assert result.returncode == 0
    # read back exactly as the runner does: UTF-8 with replacement, never the locale code page
    assert result.stdout.decode("utf-8", errors="replace").strip() == expected
    assert result.stderr.decode("utf-8", errors="replace").strip() == expected
    assert validation_runner.validation_child_env({"PYTHONIOENCODING": "cp1252"})["PYTHONIOENCODING"] == "utf-8"


def test_worker_ai_tools_validation_child_env_pins_utf8(tmp_path: Path, monkeypatch) -> None:
    from aiworkhub import worker_ai_tools_mcp

    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    env = worker_ai_tools_mcp._validation_child_env(None, {"is_pytest": True}, tmp_path)
    assert (env["PYTHONIOENCODING"], env["PYTHONUTF8"]) == ("utf-8", "1")
    assert env["PYTEST_ADDOPTS"]


def test_worker_workspace_validation_affordance_env_pins_utf8(tmp_path: Path) -> None:
    from aiworkhub import worker_workspace

    env = worker_workspace.worker_validation_affordance_env(tmp_path, str(tmp_path / "tmp"))
    assert (env["PYTHONIOENCODING"], env["PYTHONUTF8"]) == ("utf-8", "1")
    assert env["PYTEST_ADDOPTS"] == worker_workspace._WORKER_PYTEST_ADDOPTS
    assert {k: env[k] for k in validation_runner.VALIDATION_CHILD_UTF8_ENV} == validation_runner.VALIDATION_CHILD_UTF8_ENV


def test_marker_table_has_one_owner() -> None:
    assert mcp_summary_folds.validation_failure_markers is markers
    assert auto.validation_failure_markers is markers
