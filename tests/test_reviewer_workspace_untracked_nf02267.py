"""NF-2026-02267: the read-only reviewer workspace leaves owner scratch out.

The reviewer workspace overlaid every untracked non-ignored file of the live
canonical tree; a canonical tree with 7,952 untracked files (492 MB) made each
reviewer launch copy and hash all of them and outlive the launch owner before
the provider spawned. The reviewer now overlays the tracked canonical delta plus
only the untracked paths its candidate or read-only inputs name, and records how
many untracked paths it left out. The combined-tree helper is unchanged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aiworkhub import worker_workspace


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True)


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "AIWorkHub Test")
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "tracked.py").write_text("tracked = 1\n", encoding="utf-8")
    _git(repo, "add", "source.py", "tracked.py")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def test_review_workspace_leaves_unrelated_untracked_canonical_file_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    source = worker_workspace.create_workspace(
        repo,
        "1" * 32,
        {"allowed_writes": ["source.py"], "required_outputs": []},
        "validation",
    )
    review = None
    try:
        (source.path / "source.py").write_text("value = 2\n", encoding="utf-8")
        # Tracked canonical modification: still canonical content.
        (repo / "tracked.py").write_text("tracked = 2\n", encoding="utf-8")
        # Untracked canonical file no review input names: owner scratch.
        (repo / "scratch.txt").write_text("owner scratch\n", encoding="utf-8")

        review, evidence = worker_workspace.create_quality_review_workspace(
            source, "2" * 32, ["source.py"], "validation"
        )

        assert (review.path / "source.py").read_text(encoding="utf-8") == "value = 2\n"
        assert (review.path / "tracked.py").read_text(encoding="utf-8") == "tracked = 2\n"
        assert not (review.path / "scratch.txt").exists()
        assert evidence["candidate_paths"] == ["source.py"]
        assert evidence["canonical_delta_paths"] == ["tracked.py"]
        assert evidence["canonical_untracked_skipped"] == 1
        assert review.allowed_writes == ()
        assert worker_workspace.enforce_scope(review) == []
        # The combined-tree caller still sees the whole canonical delta.
        assert worker_workspace._canonical_worktree_delta_paths(repo) == [
            "scratch.txt",
            "tracked.py",
        ]
    finally:
        if review is not None:
            worker_workspace.cleanup_workspace(repo, review.path, review.home)
        worker_workspace.cleanup_workspace(repo, source.path, source.home)


def test_review_workspace_keeps_untracked_canonical_paths_the_review_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    source = worker_workspace.create_workspace(
        repo,
        "3" * 32,
        {"allowed_writes": ["source.py", "added.py"], "required_outputs": []},
        "validation",
    )
    review = None
    try:
        (source.path / "source.py").write_text("value = 2\n", encoding="utf-8")
        (source.path / "added.py").write_text("added = 2\n", encoding="utf-8")
        # An untracked canonical copy of a candidate path, an untracked
        # read-only input and one unrelated untracked file.
        (repo / "added.py").write_text("added = 1\n", encoding="utf-8")
        (repo / "notes.md").write_text("contract notes\n", encoding="utf-8")
        (repo / "scratch.txt").write_text("owner scratch\n", encoding="utf-8")

        review, evidence = worker_workspace.create_quality_review_workspace(
            source,
            "4" * 32,
            ["source.py", "added.py"],
            "validation",
            ["notes.md"],
        )

        assert (review.path / "added.py").read_text(encoding="utf-8") == "added = 2\n"
        assert (review.path / "notes.md").read_text(encoding="utf-8") == "contract notes\n"
        assert not (review.path / "scratch.txt").exists()
        assert evidence["canonical_delta_paths"] == ["added.py", "notes.md"]
        assert evidence["canonical_untracked_skipped"] == 1
        assert worker_workspace.enforce_scope(review) == []
    finally:
        if review is not None:
            worker_workspace.cleanup_workspace(repo, review.path, review.home)
        worker_workspace.cleanup_workspace(repo, source.path, source.home)
