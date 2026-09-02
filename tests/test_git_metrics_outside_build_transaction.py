"""NF-2026-00570: the 90-day git walk must not run inside the index write
transaction, and an unchanged HEAD must do no walk at all.

``_build_index_locked`` used to call ``materialize_git_metrics`` at the tail of
its ``with conn:`` write transaction, so a ``git log --numstat`` over the whole
repository (measured 3+ seconds, ~94%% of a build) ran while the connection and
its writer lock were held. The fix moves the git subprocess before the
transaction opens and applies the result with SQLite-only work inside it.

These tests pin that contract:

  * REPRODUCTION -- no git subprocess runs while the write transaction holds
    the connection (fails on the pre-fix code, where it ran inside ``with
    conn:``), and an unchanged HEAD performs no git walk.
  * The materialised metrics stay byte-identical for the same commit.
  * A repository without git history still indexes.
  * A git failure/timeout degrades to absent metrics, never a failed build.

Run: python3 -m pytest -q tests/test_git_metrics_outside_build_transaction.py
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import source_graph_insights as sginsights
from aiworkhub.repository_state import bootstrap_repository


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

HEAD = "0123456789abcdef0123456789abcdef01234567"

# Two authors touch m.py; one author touches other.py. Chosen so every metric
# column carries a distinct, hand-verifiable value.
NUMSTAT = (
    "@@Alice\n"
    "10\t2\tm.py\n"
    "@@Bob\n"
    "5\t0\tm.py\n"
    "3\t1\tother.py\n"
)

_GIT_LOG_CMD = [
    "git", "log", "--since=90 days", "--format=@@%an", "--numstat", "--", ".",
]


def _new_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _seed_repo(repo: Path) -> None:
    (repo / ".git").mkdir(exist_ok=True)
    _write(repo / "m.py", "def m():\n    return 1\n")
    _write(repo / "other.py", "def other():\n    return 2\n")


def _make_fake_git(calls, *, head=HEAD, head_rc=0, log_stdout=NUMSTAT,
                   log_rc=0, log_raises=False):
    """subprocess.run replacement recording every git invocation."""

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, head_rc, stdout=head + "\n", stderr="")
        if cmd[:2] == ["git", "log"]:
            if log_raises:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 8))
            return subprocess.CompletedProcess(cmd, log_rc, stdout=log_stdout, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


def _file_history_row(repo: Path, file_path: str):
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        row = conn.execute(
            "SELECT commit_touches_90d, lines_added_90d, lines_deleted_90d, "
            "authors_90d, primary_author_90d, evidence FROM file_history "
            "WHERE file_path=?",
            (file_path,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _write_transaction_held(db_path: Path) -> bool:
    """True iff another connection holds the SQLite write lock right now.

    A separate connection attempts ``BEGIN IMMEDIATE`` with no busy wait: while
    the build's ``with conn:`` transaction holds the RESERVED lock the attempt
    is refused (``database is locked``). A plain reader's SHARED lock does not
    trip it, so this fires only for an open writer transaction.
    """

    probe = sqlite3.connect(str(db_path), timeout=0)
    try:
        probe.execute("PRAGMA busy_timeout=0")
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        probe.close()


# ---------------------------------------------------------------------------
# REPRODUCTION -- the walk must not run under the writer lock.
# ---------------------------------------------------------------------------

def test_git_subprocess_never_runs_inside_write_transaction(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "txn")
    _seed_repo(repo)
    db_path = sg.resolve_db_path(repo)

    calls: list[list[str]] = []
    held_during_git: list[bool] = []
    fake = _make_fake_git(calls)

    def probing_run(cmd, *args, **kwargs):
        if cmd[:1] == ["git"]:
            held_during_git.append(_write_transaction_held(db_path))
        return fake(cmd, *args, **kwargs)

    monkeypatch.setattr(sginsights.subprocess, "run", probing_run)
    sg.build_index(repo, incremental=False)

    assert calls, "the build must invoke git so the walk is exercised"
    # Pre-fix, the walk ran inside ``with conn:`` and the writer lock was held
    # for at least one git call; post-fix, none of them see a held transaction.
    assert held_during_git and not any(held_during_git), (
        "no git subprocess may run while the index write transaction is held"
    )


def test_unchanged_head_performs_no_git_walk(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "cached")
    _seed_repo(repo)

    first: list[list[str]] = []
    monkeypatch.setattr(sginsights.subprocess, "run", _make_fake_git(first))
    sg.build_index(repo, incremental=False)
    assert _GIT_LOG_CMD in first, "the first build must actually walk git"

    # HEAD unchanged and nothing on disk changed: no walk may happen again.
    second: list[list[str]] = []
    monkeypatch.setattr(sginsights.subprocess, "run", _make_fake_git(second))
    sg.build_index(repo, incremental=True)
    assert not any(c[:2] == ["git", "log"] for c in second), (
        "an unchanged HEAD must perform no git walk at all"
    )


# ---------------------------------------------------------------------------
# Regressions.
# ---------------------------------------------------------------------------

def test_materialised_metrics_are_byte_identical(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "parity")
    _seed_repo(repo)
    monkeypatch.setattr(sginsights.subprocess, "run", _make_fake_git([]))
    sg.build_index(repo, incremental=False)

    assert _file_history_row(repo, "m.py") == {
        "commit_touches_90d": 2, "lines_added_90d": 15, "lines_deleted_90d": 2,
        "authors_90d": 2, "primary_author_90d": "Alice",
        "evidence": "git_commit_file_touches",
    }
    assert _file_history_row(repo, "other.py") == {
        "commit_touches_90d": 1, "lines_added_90d": 3, "lines_deleted_90d": 1,
        "authors_90d": 1, "primary_author_90d": "Bob",
        "evidence": "git_commit_file_touches",
    }


def test_repository_without_git_history_still_indexes(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "nogit")
    _write(repo / "m.py", "def m():\n    return 1\n")  # no .git directory

    calls: list[list[str]] = []
    monkeypatch.setattr(sginsights.subprocess, "run", _make_fake_git(calls))
    report = sg.build_index(repo, incremental=False)

    assert report.files_changed >= 1, "a repo without git history still indexes"
    assert calls == [], "a non-git repo must invoke no git subprocess"
    assert _file_history_row(repo, "m.py") is None


@pytest.mark.parametrize("failure", [{"log_raises": True}, {"log_rc": 128}])
def test_git_failure_degrades_to_absent_metrics(tmp_path, monkeypatch, failure):
    repo = _new_repo(tmp_path, "gitfail")
    _seed_repo(repo)
    monkeypatch.setattr(
        sginsights.subprocess, "run", _make_fake_git([], **failure),
    )
    report = sg.build_index(repo, incremental=False)

    assert report.files_changed >= 1, "a git failure must not fail the build"
    assert _file_history_row(repo, "m.py") is None, "metrics degrade to absent"
