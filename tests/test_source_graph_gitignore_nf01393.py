"""NF-2026-01393: Source Graph file enumeration honours ``.gitignore``.

Covers: ignored directories/globs excluded from ``iter_source_files`` and a
built index; a tracked file force-added despite matching an ignore pattern
stays indexed; the single-file index route refuses an ignored path; a file
that becomes ignored after a first build is removed on the next refresh;
a non-git directory or a failing git call keeps today's enumeration and
records that ``.gitignore`` was not applied.

Run: python3 -m pytest -q tests/test_source_graph_gitignore_nf01393.py
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _git_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)
    bootstrap_repository(root, repo_name=name)
    return root


def _plain_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _indexed_files(repo: Path) -> set[str]:
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        return {str(row["file_path"]) for row in conn.execute("SELECT file_path FROM files")}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_gitignored_directory_and_glob_excluded_from_enumeration_and_index(tmp_path):
    repo = _git_repo(tmp_path, "repo_dir_glob")
    _write(repo / ".gitignore", "ResearchDocs/Extracted/\n*.generated.py\n")
    _write(repo / "src" / "live.py", "def live():\n    return 1\n")
    _write(repo / "ResearchDocs" / "Extracted" / "dump.py", "def dumped():\n    return 1\n")
    _write(repo / "src" / "thing.generated.py", "def generated():\n    return 1\n")

    rels = {p.relative_to(repo).as_posix() for p in sg.iter_source_files(repo)}
    assert rels == {"src/live.py"}

    report = sg.build_index(repo, incremental=False)
    assert report.gitignore_applied is True
    assert report.gitignore_skip_reason == ""
    assert _indexed_files(repo) == {"src/live.py"}


def test_force_added_tracked_file_stays_indexed_despite_gitignore(tmp_path):
    repo = _git_repo(tmp_path, "repo_force_add")
    _write(repo / ".gitignore", "vendor/\n")
    _write(repo / "vendor" / "shipped.py", "def shipped():\n    return 1\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-f", "vendor/shipped.py"],
        check=True, capture_output=True,
    )
    _write(repo / "src" / "normal.py", "def normal():\n    return 1\n")

    rels = {p.relative_to(repo).as_posix() for p in sg.iter_source_files(repo)}
    assert rels == {"vendor/shipped.py", "src/normal.py"}

    report = sg.build_index(repo, incremental=False)
    assert report.gitignore_applied is True
    assert _indexed_files(repo) == {"vendor/shipped.py", "src/normal.py"}


def test_index_file_refuses_gitignored_path(tmp_path):
    repo = _git_repo(tmp_path, "repo_single_file")
    _write(repo / ".gitignore", "build_out/\n")
    source = "def built():\n    return 1\n"
    _write(repo / "build_out" / "thing.py", source)
    digest = _sha256(source)

    with pytest.raises(sg.SourceGraphError, match="gitignored"):
        sg.index_file(repo, "build_out/thing.py", digest)


def test_build_removes_file_that_becomes_gitignored(tmp_path):
    repo = _git_repo(tmp_path, "repo_becomes_ignored")
    _write(repo / "src" / "keep.py", "def keep():\n    return 1\n")
    _write(repo / "src" / "drop.py", "def drop():\n    return 1\n")
    first = sg.build_index(repo, incremental=True)
    assert first.files_seen == 2
    assert _indexed_files(repo) == {"src/keep.py", "src/drop.py"}

    _write(repo / ".gitignore", "src/drop.py\n")
    second = sg.build_index(repo, incremental=True)
    assert second.files_removed == 1
    assert _indexed_files(repo) == {"src/keep.py"}


def test_non_git_directory_keeps_enumeration_and_records_not_applied(tmp_path):
    repo = _plain_repo(tmp_path, "plain_repo")
    _write(repo / "src" / "only.py", "def only():\n    return 1\n")

    scan = sg._scan_git_ignored_paths(repo)
    assert scan.applied is False
    assert scan.skip_reason

    rels = {p.relative_to(repo).as_posix() for p in sg.iter_source_files(repo)}
    assert rels == {"src/only.py"}

    report = sg.build_index(repo, incremental=False)
    assert report.gitignore_applied is False
    assert report.gitignore_skip_reason
    assert report.files_seen == 1
    assert _indexed_files(repo) == {"src/only.py"}


def test_failing_git_call_keeps_enumeration_and_records_not_applied(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, "repo_git_fails")
    _write(repo / "src" / "only.py", "def only():\n    return 1\n")

    def _boom(*args, **kwargs):
        raise OSError("git executable not found")

    monkeypatch.setattr(sg.subprocess, "run", _boom)

    scan = sg._scan_git_ignored_paths(repo)
    assert scan.applied is False
    assert "git executable not found" in scan.skip_reason

    rels = {p.relative_to(repo).as_posix() for p in sg.iter_source_files(repo)}
    assert rels == {"src/only.py"}
