from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import needfix_likely_fixed, needfix_store


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _commit(repo: Path, filename: str, content: str, message: str, *, when: str) -> str:
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    env_date = when
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "commit",
            "-m",
            message,
            "--date",
            env_date,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_DATE": env_date,
            "GIT_COMMITTER_DATE": env_date,
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": __import__("os").environ.get("HOME", ""),
        },
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@example.com")
    _git(repo, "config", "user.name", "A")
    _commit(repo, "README.md", "init", "init", when="2020-01-01T00:00:00")
    return repo


def _add_row(repo: Path, *, title: str, scope_files: list[str], created_at: str) -> str:
    row = needfix_store.add_needfix(
        repo,
        title=title,
        description="d",
        scope_files=scope_files,
    )
    needfix_id = row["id"]
    conn = needfix_store._connect(repo)
    try:
        conn.execute(
            "UPDATE needfix SET created_at = ? WHERE id = ?", (created_at, needfix_id)
        )
    finally:
        conn.close()
    return needfix_id


def test_named_in_commit_signal(git_repo: Path) -> None:
    row_id = _add_row(
        git_repo, title="t1", scope_files=[], created_at="2021-01-01T00:00:00+00:00"
    )
    _commit(
        git_repo,
        "fix.py",
        "pass",
        f"Fix issue, resolves {row_id} in prose",
        when="2021-02-01T00:00:00",
    )
    result = needfix_likely_fixed.build_candidates(git_repo)
    by_id = {c["id"]: c for c in result["candidates"]}
    assert by_id[row_id]["signal"] == "named_in_commit"
    assert result["summary"]["named_in_commit"] == 1


def test_all_scope_files_touched_signal(git_repo: Path) -> None:
    row_id = _add_row(
        git_repo,
        title="t2",
        scope_files=["a.py", "b.py"],
        created_at="2021-01-01T00:00:00+00:00",
    )
    repo = git_repo
    (repo / "a.py").write_text("x", encoding="utf-8")
    (repo / "b.py").write_text("y", encoding="utf-8")
    _git(repo, "add", "a.py", "b.py")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "touch both", "--date", "2021-02-01T00:00:00"],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_DATE": "2021-02-01T00:00:00",
            "GIT_COMMITTER_DATE": "2021-02-01T00:00:00",
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": __import__("os").environ.get("HOME", ""),
        },
    )
    result = needfix_likely_fixed.build_candidates(repo)
    by_id = {c["id"]: c for c in result["candidates"]}
    assert by_id[row_id]["signal"] == "all_scope_files_touched"


def test_some_scope_files_touched_signal(git_repo: Path) -> None:
    row_id = _add_row(
        git_repo,
        title="t3",
        scope_files=["c.py", "d.py"],
        created_at="2021-01-01T00:00:00+00:00",
    )
    _commit(git_repo, "c.py", "x", "touch one", when="2021-02-01T00:00:00")
    result = needfix_likely_fixed.build_candidates(git_repo)
    by_id = {c["id"]: c for c in result["candidates"]}
    assert by_id[row_id]["signal"] == "some_scope_files_touched"


def test_no_signal_row(git_repo: Path) -> None:
    row_id = _add_row(
        git_repo, title="t4", scope_files=[], created_at="2021-01-01T00:00:00+00:00"
    )
    result = needfix_likely_fixed.build_candidates(git_repo)
    by_id = {c["id"]: c for c in result["candidates"]}
    assert by_id[row_id]["signal"] == "no_signal"


def test_single_git_invocation_regardless_of_row_count(git_repo: Path, monkeypatch) -> None:
    for i in range(5):
        _add_row(
            git_repo,
            title=f"row{i}",
            scope_files=[f"f{i}.py"],
            created_at="2021-01-01T00:00:00+00:00",
        )
    calls = []
    orig_run = subprocess.run

    def spy(*args, **kwargs):
        calls.append(args)
        return orig_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    needfix_likely_fixed.build_candidates(git_repo)
    assert len(calls) == 1


def test_commits_before_created_at_never_count(git_repo: Path) -> None:
    _commit(git_repo, "old.py", "x", "old touch", when="2020-06-01T00:00:00")
    row_id = _add_row(
        git_repo,
        title="t5",
        scope_files=["old.py"],
        created_at="2021-01-01T00:00:00+00:00",
    )
    result = needfix_likely_fixed.build_candidates(git_repo)
    by_id = {c["id"]: c for c in result["candidates"]}
    assert by_id[row_id]["signal"] == "no_signal"


def test_store_is_unchanged_after_run(git_repo: Path) -> None:
    _add_row(
        git_repo, title="t6", scope_files=["z.py"], created_at="2021-01-01T00:00:00+00:00"
    )
    db_path = needfix_store._db_path(git_repo)
    before = db_path.read_bytes()
    needfix_likely_fixed.build_candidates(git_repo)
    after = db_path.read_bytes()
    assert before == after


def test_cli_json_output(git_repo: Path, capsys) -> None:
    _add_row(
        git_repo, title="t7", scope_files=[], created_at="2021-01-01T00:00:00+00:00"
    )
    exit_code = needfix_likely_fixed.main(["--repo", str(git_repo), "--json"])
    assert exit_code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "candidates" in payload and "summary" in payload


def test_body_trailer_match_yields_named_in_commit(git_repo: Path) -> None:
    row_id = _add_row(
        git_repo, title="t8", scope_files=[], created_at="2021-01-01T00:00:00+00:00"
    )
    _commit(
        git_repo,
        "fix2.py",
        "pass",
        f"Fix something\n\nResolves: {row_id}",
        when="2021-02-01T00:00:00",
    )
    result = needfix_likely_fixed.build_candidates(git_repo)
    by_id = {c["id"]: c for c in result["candidates"]}
    assert by_id[row_id]["signal"] == "named_in_commit"


def test_prefix_id_does_not_match(git_repo: Path) -> None:
    row_id = _add_row(
        git_repo, title="t9", scope_files=[], created_at="2021-01-01T00:00:00+00:00"
    )
    _commit(
        git_repo,
        "fix3.py",
        "pass",
        f"Mentions {row_id}0 which is a longer id",
        when="2021-02-01T00:00:00",
    )
    result = needfix_likely_fixed.build_candidates(git_repo)
    by_id = {c["id"]: c for c in result["candidates"]}
    assert by_id[row_id]["signal"] == "no_signal"


def test_non_ascii_scope_file_path_matches(git_repo: Path) -> None:
    row_id = _add_row(
        git_repo,
        title="t10",
        scope_files=["caf\u00e9.py"],
        created_at="2021-01-01T00:00:00+00:00",
    )
    _commit(git_repo, "caf\u00e9.py", "x", "touch unicode file", when="2021-02-01T00:00:00")
    result = needfix_likely_fixed.build_candidates(git_repo)
    by_id = {c["id"]: c for c in result["candidates"]}
    assert by_id[row_id]["signal"] == "all_scope_files_touched"
