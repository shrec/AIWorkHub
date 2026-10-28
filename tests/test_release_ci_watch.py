"""``scripts/release_ci_watch.py``: a release tag's CI and Release runs, watched."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_ci_watch.py"
_SPEC = importlib.util.spec_from_file_location("release_ci_watch", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
rcw = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rcw)

TAG = "v0.12.73"
SHA = "a" * 40


def gh_run(workflow, run_id, *, status="completed", conclusion="success", event="push"):
    return {
        "databaseId": run_id,
        "workflowName": workflow,
        "status": status,
        "conclusion": conclusion if status == "completed" else "",
        "event": event,
        "url": f"https://github.com/owner/repo/actions/runs/{run_id}",
    }


def job(name, conclusion, *steps):
    return {
        "name": name,
        "conclusion": conclusion,
        "steps": [{"name": step, "conclusion": outcome} for step, outcome in steps],
    }


class FakeGh:
    """Scripted ``gh``/``git`` replies; records every argv list it is handed.

    Each ``gh run list`` takes the next reply in ``polls``; the last one repeats.
    """

    def __init__(self, polls, jobs=None, *, tag_commit=SHA):
        self.polls = list(polls)
        self.jobs = jobs or {}
        self.tag_commit = tag_commit
        self.calls = []

    def __call__(self, argv):
        assert isinstance(argv, list) and all(isinstance(arg, str) for arg in argv)
        self.calls.append(list(argv))
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, self.tag_commit + "\n", "")
        if argv[:3] == ["gh", "run", "list"]:
            reply = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            return subprocess.CompletedProcess(argv, 0, json.dumps(reply), "")
        if argv[:3] == ["gh", "run", "view"]:
            reply = {"jobs": self.jobs[int(argv[3])]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(reply), "")
        raise AssertionError(f"unexpected argv {argv}")

    def viewed(self):
        return [call[3] for call in self.calls if call[:3] == ["gh", "run", "view"]]


class Ticker:
    """A monotonic clock that only moves when the watch sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def run_watch(capsys, runner, *args, ticker=None):
    ticker = ticker or Ticker()
    code = rcw.main(["--tag", TAG, *args], runner=runner, sleep=ticker.sleep, clock=ticker.clock)
    return code, json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def test_all_green_runs_exit_zero_with_no_drafts(capsys):
    gh = FakeGh(
        [
            [
                gh_run("CI", 11),
                gh_run("Release", 12),
                gh_run("CI", 13, conclusion="failure", event="pull_request"),
                gh_run("Docs", 14, conclusion="failure"),
            ]
        ]
    )
    code, result = run_watch(capsys, gh, "--sha", SHA)

    assert (code, result["state"]) == (0, "green")
    assert (result["tag"], result["sha"]) == (TAG, SHA)
    assert [(run["workflow"], run["id"], run["conclusion"]) for run in result["runs"]] == [
        ("CI", 11, "success"),
        ("Release", 12, "success"),
    ]
    assert result["needfix_drafts"] == [] and result["pending"] == []
    assert gh.calls == [
        [
            "gh", "run", "list", "--commit", SHA, "--limit", str(rcw.RUN_LIST_LIMIT),
            "--json", "databaseId,workflowName,status,conclusion,event,url",
        ]
    ]


def test_a_failed_ci_run_is_drafted_with_its_failing_job_and_step_names(capsys):
    failed_ci = gh_run("CI", 21, conclusion="failure")
    gh = FakeGh(
        [
            [failed_ci, gh_run("Release", 22, status="in_progress")],
            [failed_ci, gh_run("Release", 22, conclusion="success")],
        ],
        {
            21: [
                job(
                    "test (ubuntu-latest, 3.14)",
                    "failure",
                    ("Set up job", "success"),
                    ("Run pytest", "failure"),
                    ("Upload report", "skipped"),
                ),
                job("test (ubuntu-latest, 3.12)", "success", ("Run pytest", "success")),
                job("package", "skipped"),
            ]
        },
    )
    ticker = Ticker()
    code, result = run_watch(capsys, gh, "--sha", SHA, ticker=ticker)

    assert (code, result["state"]) == (1, "failed")
    assert ticker.sleeps == [30.0]
    assert gh.viewed() == ["21"]
    failing = [{"name": "test (ubuntu-latest, 3.14)", "failed_steps": ["Run pytest"]}]
    assert result["runs"][0]["failed_jobs"] == failing
    assert result["runs"][1]["failed_jobs"] == []
    [draft] = result["needfix_drafts"]
    assert draft["title"] == "Release v0.12.73: CI run 21 concluded failure"
    assert (draft["kind"], draft["severity"]) == ("bug", "high")
    assert draft["tags"] == ["release", "ci", "v0.12.73"]
    assert draft["evidence"] == {
        "tag": TAG,
        "sha": SHA,
        "workflow": "CI",
        "run_id": 21,
        "run_url": "https://github.com/owner/repo/actions/runs/21",
        "conclusion": "failure",
        "failed_jobs": failing,
    }
    assert "- test (ubuntu-latest, 3.14): Run pytest" in draft["description"]


def test_runs_still_running_past_the_timeout_exit_two_without_drafts(capsys):
    gh = FakeGh([[gh_run("CI", 31, status="in_progress")]])
    ticker = Ticker()
    code, result = run_watch(
        capsys, gh, "--sha", SHA, "--timeout-seconds", "90", "--poll-seconds", "30", ticker=ticker
    )

    assert (code, result["state"]) == (2, "timeout")
    assert result["needfix_drafts"] == []
    assert result["pending"] == [
        {"workflow": "CI", "id": 31, "status": "in_progress"},
        {"workflow": "Release", "id": None, "status": "not_registered"},
    ]
    assert ticker.sleeps == [30.0, 30.0, 30.0]
    assert [call[:3] for call in gh.calls] == [["gh", "run", "list"]] * 4


def test_a_poll_interval_longer_than_the_timeout_waits_only_until_the_deadline(capsys):
    gh = FakeGh(
        [
            [gh_run("CI", 33, status="in_progress"), gh_run("Release", 34)],
            [gh_run("CI", 33), gh_run("Release", 34)],
        ]
    )
    ticker = Ticker()
    code = rcw.main(
        ["--tag", TAG, "--sha", SHA, "--timeout-seconds", "10", "--poll-seconds", "30"],
        runner=gh,
        sleep=ticker.sleep,
        clock=ticker.clock,
    )
    captured = capsys.readouterr()

    assert (code, json.loads(captured.out)["state"]) == (0, "green")
    assert ticker.sleeps == [10.0]
    assert "next check in 10s" in captured.err
    assert [call[:3] for call in gh.calls] == [["gh", "run", "list"]] * 2


def test_a_failed_run_exits_one_even_with_another_pending_at_the_timeout(capsys):
    gh = FakeGh(
        [[gh_run("CI", 35, conclusion="failure"), gh_run("Release", 36, status="queued")]],
        {35: [job("test (ubuntu-latest, 3.14)", "failure", ("Run pytest", "failure"))]},
    )
    ticker = Ticker()
    code, result = run_watch(
        capsys, gh, "--sha", SHA, "--timeout-seconds", "60", "--poll-seconds", "30", ticker=ticker
    )

    assert (code, result["state"]) == (1, "failed")
    assert result["pending"] == [{"workflow": "Release", "id": 36, "status": "queued"}]
    [draft] = result["needfix_drafts"]
    assert draft["title"] == "Release v0.12.73: CI run 35 concluded failure"
    assert draft["evidence"]["failed_jobs"] == [
        {"name": "test (ubuntu-latest, 3.14)", "failed_steps": ["Run pytest"]}
    ]
    assert ticker.sleeps == [30.0, 30.0]


def test_every_failed_run_gets_a_draft_and_repo_and_output_are_honoured(tmp_path, capsys):
    gh = FakeGh(
        [[gh_run("Release", 42, conclusion="cancelled"), gh_run("CI", 41, conclusion="failure")]],
        {41: [job("lint", "failure", ("ruff", "failure"))], 42: []},
    )
    output = tmp_path / "drafts.json"
    code, result = run_watch(
        capsys, gh, "--sha", SHA, "--repo", "owner/repo", "--output", str(output)
    )

    assert (code, result["state"]) == (1, "failed")
    assert [draft["title"] for draft in result["needfix_drafts"]] == [
        "Release v0.12.73: CI run 41 concluded failure",
        "Release v0.12.73: Release run 42 concluded cancelled",
    ]
    assert result["needfix_drafts"][1]["evidence"]["failed_jobs"] == []
    assert gh.viewed() == ["41", "42"]
    assert all(call[-2:] == ["--repo", "owner/repo"] for call in gh.calls)
    assert json.loads(output.read_text(encoding="utf-8")) == result


def test_without_sha_the_tag_commit_comes_from_git_rev_parse(capsys):
    commit = "b" * 40
    gh = FakeGh([[gh_run("CI", 51), gh_run("Release", 52)]], tag_commit=commit)
    code, result = run_watch(capsys, gh)

    assert (code, result["sha"]) == (0, commit)
    assert gh.calls[0] == ["git", "rev-parse", "--verify", "v0.12.73^{commit}"]
    assert gh.calls[1][:5] == ["gh", "run", "list", "--commit", commit]


# ---------------------------------------------------------------------------
# Errors: exit 2 with a named reason; only a run already seen failing is drafted
# ---------------------------------------------------------------------------


def _replying(rc, stdout="", stderr=""):
    return lambda argv: subprocess.CompletedProcess(argv, rc, stdout, stderr)


def _missing(argv):
    raise FileNotFoundError(2, "No such file or directory", argv[0])


def _hanging(argv):
    raise subprocess.TimeoutExpired(argv, rcw.COMMAND_TIMEOUT_SECONDS)


_TOO_MANY_RUNS = json.dumps([gh_run("Other", n) for n in range(rcw.RUN_LIST_LIMIT)])


@pytest.mark.parametrize(
    ("runner", "reason"),
    [
        (_replying(4, stderr="run gh auth login"), "gh_failed"),
        (_replying(0, stdout="not json"), "gh_bad_output"),
        (_replying(0, stdout=json.dumps({"runs": []})), "gh_bad_output"),
        (_replying(0, stdout=_TOO_MANY_RUNS), "gh_run_list_truncated"),
        (_missing, "gh_unavailable"),
        (_hanging, "gh_timeout"),
    ],
)
def test_a_gh_problem_exits_two_with_a_named_reason_and_no_drafts(capsys, runner, reason):
    code, result = run_watch(capsys, runner, "--sha", SHA)
    assert (code, result["state"], result["reason"]) == (2, "error", reason)
    assert result["needfix_drafts"] == []


def test_an_unknown_tag_or_a_short_sha_exits_two_before_gh_runs(capsys):
    code, result = run_watch(capsys, _replying(128, stderr="fatal: Needed a single revision"))
    assert (code, result["state"], result["reason"]) == (2, "error", "tag_not_found")
    gh = FakeGh([[]])
    code, result = run_watch(capsys, gh, "--sha", "abc1234")
    assert (code, result["reason"]) == (2, "invalid_sha")
    assert gh.calls == []


def test_a_gh_error_after_a_run_failed_exits_two_and_still_drafts_that_run(capsys):
    failed_ci = gh_run("CI", 61, conclusion="failure")
    gh = FakeGh([[failed_ci, gh_run("Release", 62, status="in_progress")]])
    polls = 0

    def runner(argv):
        nonlocal polls
        if argv[:3] == ["gh", "run", "list"]:
            polls += 1
            if polls > 1:
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 502: Bad Gateway")
        return gh(argv)

    code, result = run_watch(capsys, runner, "--sha", SHA)

    assert (code, result["state"], result["reason"]) == (2, "error", "gh_failed")
    assert polls == 2
    [draft] = result["needfix_drafts"]
    assert draft["title"] == "Release v0.12.73: CI run 61 concluded failure"
    assert draft["evidence"]["failed_jobs"] == []
    assert gh.viewed() == []


def test_an_unwritable_output_exits_two_after_printing_the_verdict(tmp_path, capsys):
    gh = FakeGh([[gh_run("CI", 71), gh_run("Release", 72)]])
    ticker = Ticker()
    code = rcw.main(
        ["--tag", TAG, "--sha", SHA, "--output", str(tmp_path)],
        runner=gh,
        sleep=ticker.sleep,
        clock=ticker.clock,
    )
    captured = capsys.readouterr()

    assert code == 2
    assert json.loads(captured.out)["state"] == "green"
    assert f"cannot write {tmp_path}" in captured.err
    assert tmp_path.is_dir()


@pytest.mark.parametrize(
    "args",
    [
        ["--tag=-x"],
        ["--tag", "v1 x"],
        ["--tag", TAG, "--repo", "not a repo"],
        ["--tag", TAG, "--timeout-seconds", "-1"],
        ["--tag", TAG, "--timeout-seconds", "inf"],
        ["--tag", TAG, "--poll-seconds", "0"],
    ],
)
def test_bad_arguments_exit_two_before_any_command_runs(args):
    gh = FakeGh([[]])
    with pytest.raises(SystemExit) as caught:
        rcw.main(args, runner=gh)
    assert caught.value.code == 2
    assert gh.calls == []


def test_main_needs_no_module_docstring(monkeypatch, capsys):
    # ``python -OO`` strips docstrings, so the parser description must not index into one.
    monkeypatch.setattr(rcw, "__doc__", None)
    gh = FakeGh([[gh_run("CI", 81), gh_run("Release", 82)]])
    code, result = run_watch(capsys, gh, "--sha", SHA)

    assert (code, result["state"]) == (0, "green")


# ---------------------------------------------------------------------------
# argv lists, never a shell
# ---------------------------------------------------------------------------


def test_the_default_runner_runs_an_argv_list_without_a_shell(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(rcw.subprocess, "run", fake_run)
    assert rcw.run_argv(["gh", "run", "list"]).stdout == "[]"
    assert seen["argv"] == ["gh", "run", "list"]
    assert "shell" not in seen
    assert seen["timeout"] == rcw.COMMAND_TIMEOUT_SECONDS
    assert "shell=True" not in SCRIPT.read_text(encoding="utf-8")
