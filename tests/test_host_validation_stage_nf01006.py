"""NF-2026-01006: host-stage rows use a short repo-local TEMP and keep FAILED lines.

Fakes only for the env/argv shape; one real ``run_argv`` of the current
interpreter proves the FAILED/ERROR lines survive a tail that drops them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from aiworkhub import host_validation_stage as hvs

REQUEST = "req_nf01006abcdef0123"


def _union2_home(tmp_path: Path) -> Path:
    return (
        tmp_path / "repo" / ".aiworkhub" / "runtime" / "worktrees"
        / ("union2_" + "a" * 32 + "_" + "b" * 16) / "home"
    )


def test_row_temp_and_basetemp_live_under_short_neutral_scratch_not_union2_home(tmp_path):
    repo = tmp_path / "repo"
    tree = tmp_path / "cand"
    tree.mkdir()
    home = _union2_home(tmp_path)
    workspace = SimpleNamespace(path=tree, repo=repo, home=home)
    scratch_root = hvs.neutral_scratch_root(repo, REQUEST)

    argv, env = hvs.validation_command(workspace, ["pytest", "-q", "tests"], scratch_root)

    (basetemp,) = [token for token in argv if token.startswith("--basetemp=")]
    basetemp_path = Path(basetemp.split("=", 1)[1]).resolve()
    root = scratch_root.resolve()
    assert root in basetemp_path.parents
    assert home.resolve() not in basetemp_path.parents
    temps = {env[key] for key in ("TEMP", "TMP", "TMPDIR")}
    assert len(temps) == 1
    temp = Path(temps.pop()).resolve()
    assert temp.is_dir()
    assert root in temp.parents
    assert home.resolve() not in temp.parents
    # A short per-row leaf: <scratch_root>/<8 hex>/t.
    assert len(str(temp)) - len(str(root)) <= len("/12345678/t")
    # The request HOME itself is unchanged.
    assert Path(env["HOME"]) == home.resolve()


def test_non_pytest_row_also_gets_the_short_temp(tmp_path):
    repo = tmp_path / "repo"
    tree = tmp_path / "cand"
    tree.mkdir()
    home = _union2_home(tmp_path)
    workspace = SimpleNamespace(path=tree, repo=repo, home=home)
    scratch_root = hvs.neutral_scratch_root(repo, REQUEST)

    argv, env = hvs.validation_command(workspace, ["git", "diff", "--check"], scratch_root)

    assert not any(token.startswith("--basetemp") for token in argv)
    temp = Path(env["TEMP"]).resolve()
    assert scratch_root.resolve() in temp.parents
    assert home.resolve() not in temp.parents


def test_two_rows_get_distinct_temp_leaves(tmp_path):
    repo = tmp_path / "repo"
    tree = tmp_path / "cand"
    tree.mkdir()
    workspace = SimpleNamespace(path=tree, repo=repo, home=_union2_home(tmp_path))
    scratch_root = hvs.neutral_scratch_root(repo, REQUEST)
    _a, first = hvs.validation_command(workspace, ["pytest", "-q"], scratch_root)
    _b, second = hvs.validation_command(workspace, ["pytest", "-q"], scratch_root)
    assert first["TEMP"] != second["TEMP"]


def _long_output_script() -> str:
    filler = hvs.MAX_OUTPUT_TAIL_BYTES + 4096
    return (
        "import sys\n"
        "w = sys.stdout.write\n"
        "w('FAILED tests/test_a.py::test_one - AssertionError\\n')\n"
        "w('ERROR tests/test_b.py::test_two - RuntimeError\\n')\n"
        "w('noise FAILED not-a-summary-line\\n')\n"
        f"w(('x' * 99 + '\\n') * ({filler} // 100 + 1))\n"
        "w('1 failed, 1 error\\n')\n"
        "sys.exit(1)\n"
    )


def test_failed_lines_kept_from_full_output_when_tail_drops_them(tmp_path):
    row = hvs.run_argv([sys.executable, "-c", _long_output_script()], tmp_path)
    assert row["returncode"] == 1
    assert row["stdout_truncated"] is True
    assert "test_one" not in row["stdout_tail"]
    assert row["failed_tests"] == [
        "FAILED tests/test_a.py::test_one - AssertionError",
        "ERROR tests/test_b.py::test_two - RuntimeError",
    ]
    summary = hvs._row_summary(row)
    assert summary["failed_tests"] == row["failed_tests"]
    assert "test_one" not in summary["output_tail"]


def test_failure_lines_are_bounded(tmp_path):
    out = tmp_path / "stdout"
    long_id = "y" * (hvs.MAX_FAILURE_LINE_CHARS * 2)
    out.write_text(
        "".join(f"FAILED tests/t.py::test_{index}{long_id}\n" for index in range(200)),
        encoding="utf-8",
    )
    lines = hvs.failure_summary_lines(out, tmp_path / "missing")
    assert len(lines) == hvs.MAX_FAILURE_LINES
    assert all(len(line) == hvs.MAX_FAILURE_LINE_CHARS for line in lines)
    assert lines[0].startswith("FAILED tests/t.py::test_0")


def test_failure_lines_read_stderr_too(tmp_path):
    out, err = tmp_path / "stdout", tmp_path / "stderr"
    out.write_text("ok\n", encoding="utf-8")
    err.write_text("ERROR tests/t.py - ImportError\n", encoding="utf-8")
    assert hvs.failure_summary_lines(out, err) == ["ERROR tests/t.py - ImportError"]
