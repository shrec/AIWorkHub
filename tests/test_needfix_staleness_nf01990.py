"""NF-2026-01990: read-only NeedFix staleness sweep."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import needfix_staleness, needfix_store

_GIT = shutil.which("git")
needs_git = pytest.mark.skipif(_GIT is None, reason="git executable not on PATH")


@pytest.fixture
def repo_root() -> Path:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def init(repo_root: Path) -> Path:
    needfix_store.initialize_repository(repo_root)
    return repo_root


def _git(repo: Path, *args: str, date: datetime | None = None) -> str:
    env = dict(os.environ)
    if date is not None:
        stamp = date.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    result = subprocess.run(
        [
            "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "-c", "commit.gpgsign=false", "-C", str(repo), *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, *, date: datetime) -> str:
    _git(repo, "commit", "--allow-empty", "-q", "-m", message, date=date)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def git_repo(init: Path) -> Path:
    _git(init, "init", "-q")
    _commit(init, "initial", date=datetime(2001, 1, 1, tzinfo=timezone.utc))
    return init


def _add(repo: Path, title: str, **kw: Any) -> dict[str, Any]:
    return needfix_store.add_needfix(
        repo, title=title, description=f"{title} description", **kw
    )


def _by_id(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in report["rows"]}


@needs_git
def test_row_whose_only_scope_file_is_absent_is_archive_obsolete(git_repo: Path) -> None:
    (git_repo / "present.py").write_text("x = 1\n", encoding="utf-8")
    gone = _add(git_repo, "gone file", severity="high", scope_files=["src/gone.py"])
    partial = _add(
        git_repo, "partial file", severity="low", scope_files=["src/gone.py", "present.py"]
    )
    outside = _add(git_repo, "outside file", severity="low", scope_files=["../escape.py"])

    rows = _by_id(needfix_staleness.sweep(git_repo))

    assert rows[gone["id"]]["disposition"] == "archive_obsolete"
    assert rows[gone["id"]]["scope_files_missing"] == ["src/gone.py"]
    assert rows[partial["id"]]["disposition"] == "keep"
    assert rows[partial["id"]]["scope_files_missing"] == ["src/gone.py"]
    assert rows[outside["id"]]["disposition"] == "archive_obsolete"


@needs_git
def test_mention_after_creation_is_possibly_resolved_and_older_mention_ignored(
    git_repo: Path,
) -> None:
    named = _add(git_repo, "named later", severity="low")
    old_only = _add(git_repo, "named before", severity="low")
    created = datetime.fromisoformat(old_only["created_at"])
    _commit(git_repo, f"older note {old_only['id']}", date=created - timedelta(hours=2))
    sha = _commit(
        git_repo, f"fix the thing\n\nRefs {named['id']}", date=created + timedelta(hours=1)
    )

    report = needfix_staleness.sweep(git_repo)
    rows = _by_id(report)

    assert report["git_unavailable"] is False
    assert rows[named["id"]]["disposition"] == "possibly_resolved"
    assert rows[named["id"]]["mentioning_commits"] == [sha]
    assert rows[old_only["id"]]["mentioning_commits"] == []
    assert rows[old_only["id"]]["disposition"] == "keep"


@needs_git
def test_high_without_evidence_needs_triage_and_low_is_kept(git_repo: Path) -> None:
    high = _add(git_repo, "high item", severity="high")
    low = _add(git_repo, "low item", severity="low")

    report = needfix_staleness.sweep(git_repo)
    rows = _by_id(report)

    assert rows[high["id"]]["disposition"] == "needs_triage"
    assert rows[low["id"]]["disposition"] == "keep"
    assert [row["id"] for row in report["rows"]] == [high["id"], low["id"]]
    assert report["counts"] == {
        "archive_obsolete": 0, "possibly_resolved": 0, "needs_triage": 1, "keep": 1,
    }
    assert isinstance(rows[high["id"]]["age_days"], int)


@needs_git
def test_archived_rows_are_not_listed_and_accepted_rows_are_swept(git_repo: Path) -> None:
    captured = _add(git_repo, "still captured", severity="low")
    accepted = _add(git_repo, "already accepted", severity="high", status="accepted")
    archived = _add(git_repo, "to archive", severity="high")
    needfix_store.archive_needfix(git_repo, archived["id"], reason="test")

    report = needfix_staleness.sweep(git_repo)
    ids = {row["id"] for row in report["rows"]}

    assert ids == {captured["id"], accepted["id"]}
    assert archived["id"] not in ids
    assert report["captured_total"] == 1
    assert report["open_total"] == 2


def _snapshot(repo: Path) -> dict[str, tuple[str, str]]:
    rows = needfix_store.list_needfix(repo, include_archived=True, limit=500)
    return {row["id"]: (row["status"], row["updated_at"]) for row in rows}


@needs_git
def test_sweep_changes_no_needfix_row(git_repo: Path) -> None:
    named = _add(git_repo, "named", severity="high")
    _add(git_repo, "obsolete", severity="medium", scope_files=["nope.py"])
    _add(git_repo, "plain", severity="low")
    _commit(
        git_repo,
        f"Resolves: {named['id']}",
        date=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    before = _snapshot(git_repo)

    report = needfix_staleness.sweep(git_repo)

    assert report["captured_total"] == 3
    assert _snapshot(git_repo) == before


def test_repo_without_git_reports_git_unavailable(init: Path) -> None:
    row = _add(init, "no git here", severity="medium")

    report = needfix_staleness.sweep(init)

    assert report["git_unavailable"] is True
    assert _by_id(report)[row["id"]]["mentioning_commits"] == []
    assert report["schema_id"] == "aiworkhub.needfix_staleness_sweep.v1"


@needs_git
def test_cli_prints_json_whose_counts_match_sweep(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(git_repo, "cli high", severity="high")
    _add(git_repo, "cli low", severity="low")
    _add(git_repo, "cli gone", severity="low", scope_files=["missing.py"])
    expected = needfix_staleness.sweep(git_repo)

    assert needfix_staleness.main(["--repo", str(git_repo)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["counts"] == expected["counts"]
    assert printed["captured_total"] == expected["captured_total"]
    assert len(printed["rows"]) == 3

    assert (
        needfix_staleness.main(
            ["--repo", str(git_repo), "--disposition", "keep", "--limit", "5"]
        )
        == 0
    )
    narrowed = json.loads(capsys.readouterr().out)
    assert narrowed["counts"] == expected["counts"]
    assert [row["disposition"] for row in narrowed["rows"]] == ["keep"]


def test_sweep_covers_every_open_status(init: Path) -> None:
    captured = _add(init, "captured item", severity="low")
    triaged = _add(init, "triaged item", severity="low")
    needfix_store.triage_needfix(init, triaged["id"])
    accepted = _add(init, "accepted item", severity="low")
    needfix_store.triage_needfix(init, accepted["id"])
    needfix_store.accept_needfix(init, accepted["id"])

    report = needfix_staleness.sweep(init)
    rows = _by_id(report)

    assert rows[captured["id"]]["status"] == "captured"
    assert rows[triaged["id"]]["status"] == "triaged"
    assert rows[accepted["id"]]["status"] == "accepted"
    assert report["captured_total"] == 1
    assert report["open_total"] == 3
