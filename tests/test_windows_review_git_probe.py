from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from aiworkhub import worker_workspace


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _timed_out(workspace, phase: str) -> worker_workspace.GitCommandTimeout:
    return worker_workspace.GitCommandTimeout(
        phase=phase,
        argv=["git", "diff"],
        cwd=workspace.path,
        timeout=0.05,
        pid=1234,
        tree_terminated=True,
    )


def test_acceptance_scope_reads_detached_head_metadata_without_rev_parse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "review-metadata-head",
        {"allowed_writes": ["tracked.txt"]},
        "validation",
    )
    real_run = worker_workspace._run
    calls: list[tuple[str, ...]] = []

    def guarded_run(argv, **kwargs):
        calls.append(tuple(argv))
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            raise AssertionError("acceptance must not spawn git rev-parse HEAD")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(worker_workspace, "_run", guarded_run)
    try:
        started = time.monotonic()
        assert worker_workspace.enforce_scope(
            workspace,
            git_phase="review_acceptance",
            git_timeout=2.0,
        ) == []
        assert time.monotonic() - started < 5.0
        assert ("git", "rev-parse", "HEAD") not in calls
    finally:
        monkeypatch.setattr(worker_workspace, "_run", real_run)
        worker_workspace.cleanup_workspace(
            workspace.repo, workspace.path, workspace.home
        )


def test_review_git_timeout_is_structured_bounded_and_reaps_child(
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    with pytest.raises(worker_workspace.GitCommandTimeout) as caught:
        worker_workspace._run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout=0.05,
            phase="review_acceptance",
        )

    elapsed = time.monotonic() - started
    assert elapsed < 7.0
    assert str(caught.value).startswith("review_acceptance_git_probe_timeout:")
    assert caught.value.phase == "review_acceptance"
    assert caught.value.tree_terminated is True


def test_finalization_git_timeout_uses_distinct_taxonomy(tmp_path: Path) -> None:
    with pytest.raises(
        worker_workspace.GitCommandTimeout,
        match=r"^worker_finalization_timeout:",
    ):
        worker_workspace._run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout=0.05,
            phase="worker_finalization",
        )


def test_finalization_git_timeout_falls_back_to_complete_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    for name in ("deleted.txt", "rename-src.txt"):
        (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", "deleted.txt", "rename-src.txt")
    _git(repo, "commit", "-m", "more baseline")
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "manifest-fallback",
        {
            "allowed_writes": [
                "tracked.txt",
                "deleted.txt",
                "rename-src.txt",
                "rename-dst.txt",
                "new.txt",
            ]
        },
        "validation",
    )
    real_run = worker_workspace._run

    def blocked_git(argv, **kwargs):
        if argv[:2] == ["git", "diff"]:
            raise _timed_out(workspace, str(kwargs.get("phase") or ""))
        return real_run(argv, **kwargs)

    try:
        (workspace.path / "tracked.txt").write_text("modified\n", encoding="utf-8")
        (workspace.path / "deleted.txt").unlink()
        (workspace.path / "new.txt").write_text("new\n", encoding="utf-8")
        (workspace.path / "rename-src.txt").replace(
            workspace.path / "rename-dst.txt"
        )
        monkeypatch.setattr(worker_workspace, "_run", blocked_git)

        assert worker_workspace.changed_paths(
            workspace, git_phase="worker_finalization", git_timeout=0.05
        ) == [
            "deleted.txt",
            "new.txt",
            "rename-dst.txt",
            "rename-src.txt",
            "tracked.txt",
        ]
    finally:
        monkeypatch.setattr(worker_workspace, "_run", real_run)
        worker_workspace.cleanup_workspace(
            workspace.repo, workspace.path, workspace.home
        )


def test_manifest_fallback_fails_closed_for_out_of_scope_and_missing_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "manifest-scope",
        {"allowed_writes": ["tracked.txt"]},
        "validation",
    )
    real_run = worker_workspace._run

    def blocked_git(argv, **kwargs):
        if argv[:2] == ["git", "diff"]:
            raise _timed_out(workspace, str(kwargs.get("phase") or ""))
        return real_run(argv, **kwargs)

    try:
        (workspace.path / "outside.txt").write_text("forbidden\n", encoding="utf-8")
        monkeypatch.setattr(worker_workspace, "_run", blocked_git)
        with pytest.raises(worker_workspace.WorkspaceError, match="scope_violation:outside.txt"):
            worker_workspace.enforce_scope(
                workspace, git_phase="worker_finalization", git_timeout=0.05
            )
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="worker_finalization_git_fallback_failed:.*baseline_missing",
        ):
            worker_workspace.changed_paths(
                replace(workspace, tree_baseline=None),
                git_phase="worker_finalization",
                git_timeout=0.05,
            )
    finally:
        monkeypatch.setattr(worker_workspace, "_run", real_run)
        worker_workspace.cleanup_workspace(
            workspace.repo, workspace.path, workspace.home
        )
