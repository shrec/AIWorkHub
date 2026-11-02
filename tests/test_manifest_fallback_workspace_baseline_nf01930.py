from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from aiworkhub import worker_workspace
from aiworkhub.worker_workspace import GitCommandTimeout, WorkerWorkspace


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "tester")
    (repo / "a.txt").write_text("canon-a", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _make_workspace(repo: Path) -> WorkerWorkspace:
    card = {
        "allowed_writes": ["a.txt"],
        "required_outputs": [],
        "read_first": [],
        "immutable_inputs": [],
    }
    return worker_workspace.create_workspace(repo, "req-nf01930", card, "test-adapter")


def test_manifest_fallback_honours_reseeded_workspace_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NF-2026-01930: the Git-timeout manifest fallback must agree with the
    Git path for a workspace whose workspace_baseline was re-seeded AFTER
    overlays, exactly as create_quality_review_workspace does. tree_baseline
    is captured at workspace creation, BEFORE the overlay; workspace_baseline
    is re-seeded afterward to the overlaid bytes, so the two baselines differ
    for an overlaid path. Only workspace_baseline governs changed_paths.
    """
    repo = _init_repo(tmp_path)
    workspace = _make_workspace(repo)

    # Simulate create_quality_review_workspace's overlay step: a candidate
    # path is overlaid with new bytes, then workspace_baseline is re-seeded
    # to those overlaid bytes (6437-6447 reconciliation judges against this
    # re-seeded baseline, not the pre-overlay tree_baseline).
    (workspace.path / "a.txt").write_text("overlay-a", encoding="utf-8")
    reseeded_baseline = {
        "a.txt": worker_workspace._hash_path(workspace.path / "a.txt"),
    }
    workspace = replace(workspace, workspace_baseline=reseeded_baseline)

    real_run = worker_workspace._run

    def timeout_on_diff(argv, **kwargs):
        if len(argv) > 1 and argv[1] == "diff":
            raise GitCommandTimeout(
                phase="workspace_scope",
                argv=list(argv),
                cwd=Path("."),
                timeout=1.0,
                pid=0,
                tree_terminated=True,
            )
        return real_run(argv, **kwargs)

    def _assert_git_and_manifest_agree(expected: list[str]) -> None:
        git_result = worker_workspace.changed_paths(workspace)
        manifest_result = worker_workspace._manifest_changed_paths(
            workspace, git_phase="workspace_scope"
        )
        assert git_result == manifest_result == expected

    # Case 1: untouched overlay -> both report [] (bytes match the re-seeded
    # workspace_baseline, even though they differ from tree_baseline).
    _assert_git_and_manifest_agree([])

    # Case 2: edit the overlaid path -> both report exactly that path.
    (workspace.path / "a.txt").write_text("edited-a", encoding="utf-8")
    _assert_git_and_manifest_agree(["a.txt"])

    # The Git-timeout route reaches the manifest fallback with the same answer.
    with monkeypatch.context() as patch:
        patch.setattr(worker_workspace, "_run", timeout_on_diff)
        assert worker_workspace.changed_paths(workspace) == ["a.txt"]

    # Case 3: revert to the pre-overlay tree_baseline bytes, which still
    # differ from the re-seeded workspace_baseline hash -> still reported,
    # because workspace_baseline (not tree_baseline) is authoritative.
    (workspace.path / "a.txt").write_text("canon-a", encoding="utf-8")
    _assert_git_and_manifest_agree(["a.txt"])
