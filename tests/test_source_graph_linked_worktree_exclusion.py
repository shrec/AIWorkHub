"""Linked git worktree exclusion in Source Graph discovery and admission."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.source_graph import (
    SourceGraphError,
    iter_source_files,
    load_ignore_policy,
)
from aiworkhub.source_graph_partition import _admits


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git_link(directory: Path, gitdir: Path) -> None:
    _write(directory / ".git", f"gitdir: {gitdir}\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _discovered(repo_root: Path) -> set[str]:
    return {
        path.relative_to(repo_root).as_posix()
        for path in iter_source_files(repo_root)
    }


def test_nested_linked_worktree_skipped_by_discovery(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "keep.py", "X = 1\n")
    worktree = tmp_path / ".kilo" / "worktrees" / "foremost-trip"
    _write(worktree / "dup.py", "X = 2\n")
    _git_link(worktree, tmp_path / ".git" / "worktrees" / "foremost-trip")
    assert sg._is_nested_linked_worktree_dir(tmp_path, worktree) is True
    assert _discovered(tmp_path) == {"pkg/keep.py"}


def test_submodule_style_git_link_stays_indexed(tmp_path: Path) -> None:
    submodule = tmp_path / "sub"
    _write(submodule / "mod.py", "X = 1\n")
    _git_link(submodule, tmp_path / ".git" / "modules" / "sub")
    assert sg._is_nested_linked_worktree_dir(tmp_path, submodule) is False
    assert _discovered(tmp_path) == {"sub/mod.py"}


def test_submodule_inside_linked_worktree_stays_indexed(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "plain" / "wtsub"
    _write(nested / "mod.py", "X = 1\n")
    _git_link(
        nested,
        tmp_path / ".git" / "worktrees" / "wt" / "modules" / "sub",
    )
    assert sg._is_nested_linked_worktree_dir(tmp_path, nested) is False
    assert _discovered(tmp_path) == {"plain/wtsub/mod.py"}


def test_repo_under_worktrees_directory_keeps_submodule(tmp_path: Path) -> None:
    repo_root = tmp_path / "worktrees" / "repo"
    submodule = repo_root / "sub"
    _write(submodule / "mod.py", "X = 1\n")
    _git_link(submodule, repo_root / ".git" / "modules" / "sub")
    assert sg._is_nested_linked_worktree_dir(repo_root, submodule) is False
    assert _discovered(repo_root) == {"sub/mod.py"}


def test_repo_root_that_is_a_linked_worktree_is_fully_indexed(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "checkout"
    _write(repo_root / "top.py", "X = 1\n")
    _write(repo_root / "pkg" / "deep.py", "X = 2\n")
    _git_link(repo_root, tmp_path / "main" / ".git" / "worktrees" / "checkout")
    assert sg._is_nested_linked_worktree_dir(repo_root, repo_root) is False
    assert _discovered(repo_root) == {"top.py", "pkg/deep.py"}


def test_malformed_git_link_never_raises_and_stays_indexed(
    tmp_path: Path,
) -> None:
    junk = tmp_path / "junk"
    _write(junk / "keep.py", "X = 1\n")
    _write(junk / ".git", "nothing here, no gitdir line at all\n")
    empty = tmp_path / "empty"
    _write(empty / "keep.py", "X = 2\n")
    _write(empty / ".git", "gitdir:   \n")
    assert sg._is_nested_linked_worktree_dir(tmp_path, junk) is False
    assert sg._is_nested_linked_worktree_dir(tmp_path, empty) is False
    assert _discovered(tmp_path) == {"junk/keep.py", "empty/keep.py"}


def test_single_file_resolution_refuses_nested_linked_worktree(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / ".kilo" / "worktrees" / "foremost-trip"
    target = worktree / "dup.py"
    _write(target, "X = 1\n")
    _git_link(worktree, tmp_path / ".git" / "worktrees" / "foremost-trip")
    with pytest.raises(SourceGraphError) as excinfo:
        sg.index_file(
            tmp_path,
            ".kilo/worktrees/foremost-trip/dup.py",
            _sha256(target),
        )
    assert "source_graph_single_file_excluded_dir" in str(excinfo.value)


def test_admits_keeps_two_positional_argument_contract(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "pkg" / "mod.py", "X = 1\n")
    policy = load_ignore_policy(tmp_path)
    assert _admits(policy, "pkg/mod.py") is True
    assert _admits(policy, "pkg/notes.txt") is False


def test_directory_becoming_linked_worktree_disappears_on_rebuild(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "top.py", "X = 1\n")
    nested = tmp_path / "vendored" / "trip"
    _write(nested / "deep.py", "X = 2\n")

    def build(database: Path) -> None:
        conn = sg.connect(database)
        conn.close()
        with sg.database_path_override(database):
            for path in iter_source_files(tmp_path):
                relative = path.relative_to(tmp_path).as_posix()
                sg.index_file(tmp_path, relative, _sha256(path))

    build(tmp_path / "first.db")
    assert _discovered(tmp_path) == {"top.py", "vendored/trip/deep.py"}

    _git_link(nested, tmp_path / ".git" / "worktrees" / "trip")
    assert sg._is_nested_linked_worktree_dir(tmp_path, nested) is True
    assert _discovered(tmp_path) == {"top.py"}

    build(tmp_path / "second.db")
    with pytest.raises(SourceGraphError):
        with sg.database_path_override(tmp_path / "refused.db"):
            sg.index_file(
                tmp_path,
                "vendored/trip/deep.py",
                _sha256(nested / "deep.py"),
            )
