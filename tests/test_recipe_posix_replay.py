"""``aiworkhub.operator.posix_replay``: a committed ref replayed on POSIX."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from aiworkhub import manager_recipe_tools as mrt
from aiworkhub import tool_recipes as tr
from aiworkhub.platform_io import is_windows
from aiworkhub.recipes import posix_replay as pr

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _done(rc=0, out="", err="", timed_out=False):
    return pr.Completed(rc, out, err, timed_out)


# ---------------------------------------------------------------------------
# Summary parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3 passed in 0.02s", (3, 0, 0, 0)),
        ("1 failed, 2 passed in 0.50s", (2, 1, 0, 0)),
        ("2 errors in 0.10s", (0, 0, 0, 2)),
        ("1 error in 0.10s", (0, 0, 0, 1)),
        ("4 skipped in 0.01s", (0, 0, 4, 0)),
        ("1 failed, 2 passed, 3 skipped, 1 error in 1.00s", (2, 1, 3, 1)),
        ("=== 5 passed, 1 skipped, 2 warnings in 3.21s ===", (5, 0, 1, 0)),
        ("1 failed, 9 passed in 12.30s (0:00:12)", (9, 1, 0, 0)),
        ("no tests ran in 0.01s", (0, 0, 0, 0)),
    ],
)
def test_summary_line_is_parsed_into_counts(text, expected):
    parsed = pr.parse_summary("noise\n" + text + "\n")
    assert parsed["found"] is True
    assert (
        parsed["passed"],
        parsed["failed"],
        parsed["skipped"],
        parsed["errors"],
    ) == expected


def test_the_last_summary_line_wins_and_missing_summary_is_reported():
    assert pr.parse_summary("9 passed in 1.00s\n1 failed in 0.10s\n")["failed"] == 1
    parsed = pr.parse_summary("Traceback (most recent call last):\nboom\n")
    assert parsed["found"] is False
    assert parsed["passed"] == parsed["failed"] == 0


def test_failing_node_ids_are_deduplicated_and_capped_at_fifty():
    lines = ["FAILED tests/test_a.py::test_x - assert 1 == 2", "ERROR tests/test_b.py"]
    lines += [f"FAILED tests/test_c.py::test_{n}" for n in range(80)]
    lines += ["FAILED tests/test_a.py::test_x - again"]
    found = pr.parse_failing("\n".join(lines))
    assert found[0] == "tests/test_a.py::test_x"
    assert found[1] == "tests/test_b.py"
    assert len(found) == pr.MAX_FAILING_TESTS == 50
    assert len(set(found)) == len(found)


@pytest.mark.parametrize(
    ("done", "state", "reason"),
    [
        (_done(0, "2 passed in 0.1s"), "passed", None),
        (_done(1, "FAILED t.py::a\n1 failed, 1 passed in 0.1s"), "failed", None),
        (_done(2, "ERROR t.py\n1 error in 0.1s"), "failed", None),
        (_done(5, "no tests ran in 0.01s"), "error", "no_tests_collected"),
        (_done(127, "", "env: 'python3': No such file or directory"), "error", "interpreter_unavailable"),
        (_done(1, "garbage"), "error", "no_pytest_summary"),
        (_done(None, "partial", timed_out=True), "error", "pytest_timeout"),
    ],
)
def test_verdict_classification(done, state, reason):
    verdict = pr._verdict(done)
    assert verdict["state"] == state
    assert verdict.get("reason") == reason


def test_tail_is_the_last_4000_characters_of_the_output():
    verdict = pr._verdict(_done(0, "x" * 9000 + "\n3 passed in 0.1s"))
    assert len(verdict["tail"]) == pr.TAIL_CHARS == 4000
    assert verdict["tail"].endswith("3 passed in 0.1s")


# ---------------------------------------------------------------------------
# argv construction (never a shell)
# ---------------------------------------------------------------------------


def test_posix_pytest_argv_runs_the_whole_suite_by_default():
    assert pr.pytest_argv("python3", []) == [
        "python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "pythonpath=", "tests",
    ]


def test_posix_pytest_argv_names_the_requested_tests():
    argv = pr.pytest_argv("/usr/bin/python3.12", ["tests/test_a.py", "tests/test_b.py::t"])
    assert argv[:8] == [
        "/usr/bin/python3.12", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "pythonpath=",
    ]
    assert argv[8:] == ["tests/test_a.py", "tests/test_b.py::t"]


def test_wsl_argv_uses_cd_and_e_with_an_optional_user():
    inner = ["env", "PYTHONPATH=src", *pr.pytest_argv("python3", [])]
    assert pr.wsl_argv(None, "/tmp/x", inner) == ["wsl", "--cd", "/tmp/x", "-e", *inner]
    assert pr.wsl_argv("bob", "/tmp/x", inner) == [
        "wsl", "-u", "bob", "--cd", "/tmp/x", "-e", *inner,
    ]
    for argv in (pr.wsl_argv(None, "/tmp/x", inner), pr.wsl_argv("bob", "/tmp/x", inner)):
        assert "bash" not in argv and "sh" not in argv and "-c" not in argv


@pytest.mark.parametrize("bad", ["-x", "/etc/passwd", "../x.py", "a\\b.py", "C:/x.py", "tests/../../y"])
def test_unsafe_test_paths_are_refused(bad):
    with pytest.raises(pr.Refusal) as caught:
        pr.parse_tests("tests/ok.py " + bad)
    assert caught.value.reason == "invalid_tests"


def test_empty_and_sentinel_tests_mean_the_whole_suite():
    assert pr.parse_tests("") == [] == pr.parse_tests("all")
    assert pr.parse_tests("tests/a.py  tests/b.py") == ["tests/a.py", "tests/b.py"]


def test_the_module_never_asks_for_a_shell():
    source = Path(pr.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "bash -c" not in source and '"bash"' not in source


# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------


def test_extraction_refuses_a_member_that_escapes_the_destination(tmp_path):
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("../escaped.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(pr.Refusal) as caught:
        pr.safe_extract(archive, dest)
    assert caught.value.reason == "archive_member_escapes"
    assert not (tmp_path / "escaped.txt").exists()


# ---------------------------------------------------------------------------
# Windows host
# ---------------------------------------------------------------------------


def _run_main(capsys, *argv):
    code = pr.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_windows_without_wsl_is_unavailable_not_an_exception(monkeypatch, capsys):
    monkeypatch.setattr(pr, "_is_windows", lambda: True)
    monkeypatch.setattr(pr.shutil, "which", lambda name: None)
    code, payload = _run_main(capsys)
    assert code == 2
    assert payload["state"] == "unavailable"
    assert payload["reason"] == "wsl_not_found"
    assert payload["ok"] is False
    assert payload["ref"] == "HEAD"
    assert payload["failing_tests"] == []


def _init_repo(root: Path, files: dict[str, str]) -> str:
    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "-c", "commit.gpgsign=false", *args],
            cwd=root, check=True, capture_output=True, text=True,
        ).stdout.strip()

    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q")
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    return git("rev-parse", "HEAD")


@pytest.fixture
def scratch_root(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def _fake_wsl(monkeypatch, *, probe=None):
    """Answer ``wsl`` argv lists with scripted replies; any other argv runs for real.

    ``probe`` answers the interpreter version query (default: CI's newest leg).
    Returns the list that records every ``wsl`` argv in call order.
    """
    real_run = pr.run_argv
    wsl_calls: list[list[str]] = []
    version_reply = probe if probe is not None else _done(0, pr.CI_NEWEST_PYTHON + "\n")

    def fake_run(argv, **kwargs):
        if argv[0] != "wsl":
            return real_run(argv, **kwargs)
        wsl_calls.append(list(argv))
        tail = argv[argv.index("-e") + 1 :]
        if tail[0] == "mktemp":
            return _done(0, "/tmp/tmp.replay\n")
        if tail[1:2] == ["-c"]:
            return version_reply
        if tail[0] == "env":
            return _done(0, "1 passed in 0.01s\n")
        return _done(0)

    monkeypatch.setattr(pr, "run_argv", fake_run)
    return wsl_calls


@needs_git
def test_wsl_path_extracts_and_runs_pytest_through_wsl_argv_lists(
    tmp_path, monkeypatch, capsys, scratch_root
):
    repo = tmp_path / "repo"
    sha = _init_repo(repo, {"tests/test_ok.py": "def test_ok():\n    pass\n"})
    monkeypatch.chdir(repo)
    monkeypatch.setattr(pr, "_is_windows", lambda: True)
    monkeypatch.setattr(pr.shutil, "which", lambda name: "wsl.exe" if name == "wsl" else None)

    wsl_calls = _fake_wsl(monkeypatch)
    code, payload = _run_main(
        capsys, "--ref", "HEAD", "--wsl-user", "bob", "--tests", "tests/test_ok.py"
    )

    assert (code, payload["state"], payload["passed"]) == (0, "passed", 1)
    assert payload["commit_sha"] == sha
    assert all(call[:3] == ["wsl", "-u", "bob"] for call in wsl_calls)
    assert [call[call.index("-e") + 1] for call in wsl_calls] == [
        "mktemp", "tar", "python3", "env", "rm",
    ]
    probe_call = wsl_calls[2]
    assert probe_call[probe_call.index("--cd") + 1] == "/tmp/tmp.replay"
    assert probe_call[probe_call.index("-e") + 1 :] == [
        "python3", "-c", "import sys;print('%d.%d'%sys.version_info[:2])",
    ]
    pytest_call = wsl_calls[3]
    assert pytest_call[pytest_call.index("--cd") + 1] == "/tmp/tmp.replay"
    assert pytest_call[pytest_call.index("-e") + 1 :] == [
        "env", "PYTHONPATH=src", "python3", "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "-o", "pythonpath=", "tests/test_ok.py",
    ]
    assert wsl_calls[4][-3:] == ["-rf", "--", "/tmp/tmp.replay"]
    assert payload["wsl_python_version"] == pr.CI_NEWEST_PYTHON
    assert "python_version_mismatch" not in payload
    assert payload["temp_removed"] is True
    assert list(scratch_root.iterdir()) == []


@needs_git
def test_wsl_failing_before_pytest_is_unavailable(tmp_path, monkeypatch, capsys, scratch_root):
    repo = tmp_path / "repo"
    _init_repo(repo, {"tests/test_ok.py": "def test_ok():\n    pass\n"})
    monkeypatch.chdir(repo)
    monkeypatch.setattr(pr, "_is_windows", lambda: True)
    monkeypatch.setattr(pr.shutil, "which", lambda name: "wsl.exe")
    real_run = pr.run_argv

    def fake_run(argv, **kwargs):
        if argv[0] != "wsl":
            return real_run(argv, **kwargs)
        return _done(0xFFFFFFFF, "", "There is no distribution with the supplied name.")

    monkeypatch.setattr(pr, "run_argv", fake_run)
    code, payload = _run_main(capsys)
    assert code == 2
    assert payload["state"] == "unavailable"
    assert payload["reason"] == "wsl_error"
    assert "no distribution" in payload["tail"]
    assert list(scratch_root.iterdir()) == []


def test_the_wsl_gate_reports_a_python_version_off_ci_newest_without_failing(
    monkeypatch, tmp_path
):
    wsl_calls = _fake_wsl(monkeypatch, probe=_done(0, "3.12\n"))
    verdict = pr._run_in_wsl(tmp_path / "archive.tar", tmp_path, None, "python3.12", [])

    assert verdict["state"] == "passed"
    assert verdict["wsl_python_version"] == "3.12"
    assert verdict["python_version_mismatch"] == {"wsl": "3.12", "ci_newest": pr.CI_NEWEST_PYTHON}
    probe_call, pytest_call = wsl_calls[2], wsl_calls[3]
    assert probe_call[probe_call.index("-e") + 1 :] == [
        "python3.12", "-c", "import sys;print('%d.%d'%sys.version_info[:2])",
    ]
    inner = pytest_call[pytest_call.index("-e") + 1 :]
    assert inner[:3] == ["env", "PYTHONPATH=src", "python3.12"]
    assert set(verdict) - set(pr._verdict(_done(0, "1 passed in 0.01s\n"))) == {
        "wsl_python_version", "python_version_mismatch",
    }


@pytest.mark.parametrize(
    "probe",
    [
        _done(127, "", "env: 'python3': No such file or directory"),
        _done(0, "Python 3.14.0\n"),
        _done(None, "", timed_out=True),
    ],
)
def test_an_unreadable_wsl_python_version_is_a_mismatch_never_a_match(
    monkeypatch, tmp_path, probe
):
    _fake_wsl(monkeypatch, probe=probe)
    verdict = pr._run_in_wsl(tmp_path / "archive.tar", tmp_path, None, "python3", [])
    assert verdict["state"] == "passed"
    assert verdict["wsl_python_version"] is None
    assert verdict["python_version_mismatch"] == {"wsl": None, "ci_newest": pr.CI_NEWEST_PYTHON}


def test_python_version_report_adds_a_mismatch_only_off_ci_newest():
    assert pr.python_version_report(pr.CI_NEWEST_PYTHON) == {
        "wsl_python_version": pr.CI_NEWEST_PYTHON
    }
    assert pr.python_version_report("3.13")["python_version_mismatch"] == {
        "wsl": "3.13", "ci_newest": pr.CI_NEWEST_PYTHON,
    }


def test_ci_newest_python_is_the_newest_leg_of_the_ci_test_matrix():
    ci = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    if not ci.is_file():
        pytest.skip("the CI workflow is not in this checkout")
    matrices = re.findall(r"python-version:\s*\[([^\]]*)\]", ci.read_text(encoding="utf-8"))
    legs = [(int(a), int(b)) for a, b in re.findall(r"(\d+)\.(\d+)", " ".join(matrices))]
    assert legs, "no python-version matrix in .github/workflows/ci.yml"
    major, minor = max(legs)
    assert f"{major}.{minor}" == pr.CI_NEWEST_PYTHON


# ---------------------------------------------------------------------------
# POSIX host: a real replay of a committed ref
# ---------------------------------------------------------------------------

posix_only = pytest.mark.skipif(is_windows(), reason="POSIX host only")

_FILES = {
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "tests/test_bad.py": "def test_fails():\n    assert 1 == 2\n",
}


@needs_git
@posix_only
def test_posix_run_reports_failed_with_counts_and_node_ids(
    tmp_path, monkeypatch, capsys, scratch_root
):
    repo = tmp_path / "repo"
    sha = _init_repo(repo, _FILES)
    (repo / "tests" / "test_uncommitted.py").write_text(
        "def test_never_seen():\n    assert False\n", encoding="utf-8"
    )
    monkeypatch.chdir(repo)

    code, payload = _run_main(capsys, "--interpreter", sys.executable)

    assert code == 0
    assert payload["state"] == "failed"
    assert (payload["passed"], payload["failed"]) == (1, 1)
    assert payload["errors"] == 0
    assert payload["commit_sha"] == sha
    assert payload["ref"] == "HEAD"
    assert payload["failing_tests"] == ["tests/test_bad.py::test_fails"]
    assert len(payload["tail"]) <= 4000
    assert payload["temp_removed"] is True
    assert payload["duration_seconds"] >= 0
    assert list(scratch_root.iterdir()) == []
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert status.strip() == "?? tests/test_uncommitted.py"


@needs_git
@posix_only
def test_posix_run_of_a_named_passing_test_passes(tmp_path, monkeypatch, capsys, scratch_root):
    repo = tmp_path / "repo"
    _init_repo(repo, _FILES)
    monkeypatch.chdir(repo)
    code, payload = _run_main(
        capsys, "--interpreter", sys.executable, "--tests", "tests/test_ok.py"
    )
    assert (code, payload["state"]) == (0, "passed")
    assert (payload["passed"], payload["failed"]) == (1, 0)
    assert payload["failing_tests"] == []
    assert list(scratch_root.iterdir()) == []


@needs_git
def test_temporary_directory_is_removed_when_the_run_fails(
    tmp_path, monkeypatch, capsys, scratch_root
):
    repo = tmp_path / "repo"
    _init_repo(repo, _FILES)
    monkeypatch.chdir(repo)

    def boom(root, sha, target):
        target.write_bytes(b"partial")
        raise pr.Refusal("error", "git_archive_failed", "disk full")

    monkeypatch.setattr(pr, "export_archive", boom)
    code, payload = _run_main(capsys)
    assert (code, payload["state"], payload["reason"]) == (2, "error", "git_archive_failed")
    assert payload["temp_removed"] is True
    assert list(scratch_root.iterdir()) == []


@needs_git
def test_unknown_ref_is_an_error_without_a_temporary_directory(
    tmp_path, monkeypatch, capsys, scratch_root
):
    repo = tmp_path / "repo"
    _init_repo(repo, _FILES)
    monkeypatch.chdir(repo)
    code, payload = _run_main(capsys, "--ref", "no-such-ref")
    assert (code, payload["state"], payload["reason"]) == (2, "error", "ref_not_found")
    assert payload["commit_sha"] is None
    assert list(scratch_root.iterdir()) == []


# ---------------------------------------------------------------------------
# Catalogue entry
# ---------------------------------------------------------------------------


def test_the_recipe_is_catalogued_with_a_full_suite_bound():
    by_id = {recipe.id: recipe for recipe in mrt.CANONICAL_RECIPES}
    recipe = by_id["aiworkhub.operator.posix_replay"]
    assert "src/aiworkhub/recipes/posix_replay.py" in recipe.purpose
    assert "never touches the working tree" in recipe.purpose
    assert recipe.risk_class is not tr.RiskClass.NONE
    assert recipe.resource_bounds.max_runtime_seconds == 1800
    assert tr.CAPABILITY_WRITE in recipe.capabilities
    assert tuple(
        (token.text if isinstance(token, tr.ArgvLiteral) else token.param)
        for token in recipe.argv[:3]
    ) == ("python", "-m", "aiworkhub.recipes.posix_replay")
    assert tr.build_argv(recipe, {}) == (
        "python", "-m", "aiworkhub.recipes.posix_replay",
        "--ref", "HEAD", "--tests", "all", "--interpreter", "python3", "--wsl-user", "all",
    )
    assert ("aiworkhub.operator.posix_replay", "aiworkhub.recipes.posix_replay") in (
        mrt.OPERATOR_SCRIPT_PATHS
    )


def test_decode_keeps_utf8_with_a_stray_nul_and_reads_wsl_utf16():
    assert pr._decode("3 passed in 0.10s\x00 ok".encode()) == "3 passed in 0.10s ok"
    assert pr._decode("გამარჯობა\x00".encode()) == "გამარჯობა"
    message = "There is no distribution installed."
    assert pr._decode(message.encode("utf-16-le")) == message
