"""NF-2026-01618: restoring an inherited rework path to canonical bytes is not a change."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from aiworkhub import worker_workspace

CANONICAL = b"a { color: red; }\n"
PREDECESSOR = b"a { color: blue; margin: 0; }\n"
FURTHER = b"a { color: green; margin: 1px; padding: 2px; }\n"
REVERTED = "web/app.css"


class _State(NamedTuple):
    canonical: bytes | None
    predecessor: bytes | None
    current: bytes | None


# NF-2026-01614 epoch 2: the rework restored app.css to canonical; every case sits beside it.
REVERTED_STATE = _State(CANONICAL, PREDECESSOR, CANONICAL)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)


def _put(root: Path, relative: str, content: bytes | None) -> None:
    target = root / relative
    if content is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _init_repo(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir(parents=True)
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "AIWorkHub Test"),
        ("config", "core.autocrlf", "false"),
        ("config", "commit.gpgsign", "false"),
    ):
        _git(root, *args)
    for relative, content in files.items():
        _put(root, relative, content)
    if files:
        _git(root, "add", "--", *files)
    _git(root, "commit", "-q", "--allow-empty", "-m", "canonical")
    return root


def _hashes(root: Path, paths: dict[str, _State]) -> dict[str, str | None]:
    return {relative: worker_workspace._hash_path(root / relative) for relative in paths}


def _rework_workspace(
    tmp_path: Path, paths: dict[str, _State]
) -> worker_workspace.WorkerWorkspace:
    canonical = {
        relative: state.canonical
        for relative, state in paths.items()
        if state.canonical is not None
    }
    worktree = _init_repo(tmp_path / "worktree", canonical)
    parent_baseline = _hashes(worktree, paths)
    for relative, state in paths.items():
        if state.predecessor is not None:
            _put(worktree, relative, state.predecessor)
    workspace_baseline = _hashes(worktree, paths)
    for relative, state in paths.items():
        _put(worktree, relative, state.current)
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    return worker_workspace.WorkerWorkspace(
        request_id="1" * 32,
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=tuple(paths),
        parent_baseline=parent_baseline,
        workspace_baseline=workspace_baseline,
        inherited_rework_paths=tuple(
            relative for relative, state in paths.items() if state.predecessor is not None
        ),
    )


def test_inherited_path_restored_to_canonical_bytes_is_not_a_change(tmp_path: Path) -> None:
    workspace = _rework_workspace(tmp_path, {REVERTED: REVERTED_STATE})

    assert worker_workspace.changed_paths(workspace) == []


def test_inherited_path_left_at_predecessor_bytes_is_a_change(tmp_path: Path) -> None:
    workspace = _rework_workspace(
        tmp_path,
        {
            REVERTED: REVERTED_STATE,
            "web/kept.css": _State(CANONICAL, PREDECESSOR, PREDECESSOR),
        },
    )

    assert worker_workspace.changed_paths(workspace) == ["web/kept.css"]


def test_inherited_path_edited_beyond_predecessor_is_a_change(tmp_path: Path) -> None:
    workspace = _rework_workspace(
        tmp_path,
        {
            REVERTED: REVERTED_STATE,
            "web/edited.css": _State(CANONICAL, PREDECESSOR, FURTHER),
        },
    )

    assert worker_workspace.changed_paths(workspace) == ["web/edited.css"]


def test_non_inherited_path_edit_is_a_change(tmp_path: Path) -> None:
    workspace = _rework_workspace(
        tmp_path,
        {
            REVERTED: REVERTED_STATE,
            "web/plain.css": _State(CANONICAL, None, FURTHER),
        },
    )

    assert worker_workspace.changed_paths(workspace) == ["web/plain.css"]


def test_inherited_new_file_removed_by_rework_is_not_a_change(tmp_path: Path) -> None:
    workspace = _rework_workspace(
        tmp_path, {"web/new.css": _State(None, PREDECESSOR, None)}
    )

    assert worker_workspace.changed_paths(workspace) == []


def test_inherited_canonical_file_deleted_by_rework_is_a_change(tmp_path: Path) -> None:
    workspace = _rework_workspace(
        tmp_path, {"web/gone.css": _State(CANONICAL, PREDECESSOR, None)}
    )

    assert worker_workspace.changed_paths(workspace) == ["web/gone.css"]


def test_quality_review_workspace_accepts_rework_that_reverted_an_inherited_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured symptom: quality_review_candidate_mismatch:<reverted path>."""
    other = "web/other.css"
    allowed = [REVERTED, other]
    repo = _init_repo(tmp_path / "repo", {REVERTED: CANONICAL, other: CANONICAL})
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = worker_workspace.create_workspace(
        repo, "1" * 32, {"allowed_writes": allowed}, "validation"
    )
    successor = review = None
    try:
        _put(predecessor.path, REVERTED, PREDECESSOR)
        successor = worker_workspace.create_workspace(
            repo,
            "2" * 32,
            {
                "allowed_writes": allowed,
                "rework_predecessor": {
                    "schema_id": "aiworkhub.rework_predecessor.v1",
                    "request_id": "1" * 32,
                    "workspace": predecessor.as_metadata(),
                    "changed_path_hashes": {REVERTED: hashlib.sha256(PREDECESSOR).hexdigest()},
                },
            },
            "validation",
        )
        assert successor.inherited_rework_paths == (REVERTED,)
        assert (successor.path / REVERTED).read_bytes() == PREDECESSOR
        _put(successor.path, REVERTED, CANONICAL)
        _put(successor.path, other, FURTHER)

        review, evidence = worker_workspace.create_quality_review_workspace(
            successor, "3" * 32, worker_workspace.enforce_scope(successor), "validation"
        )

        assert evidence["candidate_paths"] == [other]
    finally:
        for workspace in (review, successor, predecessor):
            if workspace is not None:
                worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_manifest_fallback_judges_inherited_paths_against_canonical_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The GitCommandTimeout fallback must agree with the git branch (NF-2026-01618)."""
    kept, added = "web/kept.css", "web/new.css"
    allowed = [REVERTED, kept, added]
    predecessor_hash = hashlib.sha256(PREDECESSOR).hexdigest()
    repo = _init_repo(tmp_path / "repo", {REVERTED: CANONICAL, kept: CANONICAL})
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = worker_workspace.create_workspace(
        repo, "1" * 32, {"allowed_writes": allowed}, "validation"
    )
    successor = None
    try:
        for relative in allowed:
            _put(predecessor.path, relative, PREDECESSOR)
        successor = worker_workspace.create_workspace(
            repo,
            "2" * 32,
            {
                "allowed_writes": allowed,
                "rework_predecessor": {
                    "schema_id": "aiworkhub.rework_predecessor.v1",
                    "request_id": "1" * 32,
                    "workspace": predecessor.as_metadata(),
                    "changed_path_hashes": dict.fromkeys(allowed, predecessor_hash),
                },
            },
            "validation",
        )
        assert successor.inherited_rework_paths == tuple(sorted(allowed))
        _put(successor.path, REVERTED, CANONICAL)
        _put(successor.path, added, None)
        real_run = worker_workspace._run
        timeouts: list[list[str]] = []

        def blocked_git(argv, **kwargs):
            if argv[:2] != ["git", "diff"]:
                return real_run(argv, **kwargs)
            timeouts.append(argv)
            raise worker_workspace.GitCommandTimeout(
                phase=str(kwargs.get("phase") or ""),
                argv=argv,
                cwd=successor.path,
                timeout=0.05,
                pid=1234,
                tree_terminated=True,
            )

        with monkeypatch.context() as patch:
            patch.setattr(worker_workspace, "_run", blocked_git)
            changed = worker_workspace.changed_paths(successor)

        assert timeouts, "the git probe must time out so the manifest fallback runs"
        assert REVERTED not in changed
        assert added not in changed
        assert kept in changed
    finally:
        for workspace in (successor, predecessor):
            if workspace is not None:
                worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)
