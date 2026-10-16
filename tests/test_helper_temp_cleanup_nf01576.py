"""NF-2026-01576: read-only helper-temp leaves and aged awh-helper scratch sweeps."""

import os
import stat
import time
from pathlib import Path

from aiworkhub import storage_retention, windows_appcontainer, worker_workspace


def _read_only_leaf(tmp_path: Path, parent: str) -> Path:
    leaf = tmp_path / parent / "abcdef0123"
    nested = leaf / "sub" / "pytest-of-x"
    nested.mkdir(parents=True)
    (nested / "f.txt").write_text("x")
    os.chmod(nested / "f.txt", stat.S_IREAD)
    return leaf


def test_discard_helper_temp_removes_read_only_leaf(tmp_path: Path) -> None:
    leaf = _read_only_leaf(tmp_path, "awh-helper")

    windows_appcontainer._discard_helper_temp(str(leaf))

    assert not leaf.exists()


def test_discard_helper_temp_keeps_leaf_outside_helper_parent(tmp_path: Path) -> None:
    leaf = _read_only_leaf(tmp_path, "not-helper")

    try:
        windows_appcontainer._discard_helper_temp(str(leaf))
        assert leaf.is_dir()
    finally:
        os.chmod(leaf / "sub" / "pytest-of-x" / "f.txt", stat.S_IWRITE)


def test_discard_helper_temp_swallows_removal_failure(
    tmp_path: Path, monkeypatch
) -> None:
    leaf = tmp_path / "awh-helper" / "abcdef0123"
    leaf.mkdir(parents=True)

    def fail(_root: Path) -> None:
        raise worker_workspace.WorkspaceError("workspace_cleanup_remove_failed")

    monkeypatch.setattr(worker_workspace, "_rmtree_workspace_owned", fail)

    windows_appcontainer._discard_helper_temp(str(leaf))

    assert leaf.is_dir()


def test_janitor_scratch_sweeps_aged_awh_helper_leaf(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    container = root / ".aiworkhub" / "runtime" / "worktrees" / "awh-helper"
    aged = container / "fedcba9876"
    young = container / "0123456789"
    aged.mkdir(parents=True)
    young.mkdir(parents=True)
    (aged / "f.txt").write_text("x")
    (young / "f.txt").write_text("x")

    now = time.time()
    old = now - 49 * 3600
    # _janitor_mtime takes the newest mtime of the leaf and its direct children,
    # so the aged leaf's child file must be aged too.
    os.utime(aged / "f.txt", (old, old))
    os.chmod(aged / "f.txt", stat.S_IREAD)
    os.utime(aged, (old, old))
    os.utime(young, (now - 3600, now - 3600))

    result = storage_retention._janitor_scratch(root, now)

    assert result["removed"] == 1
    assert not aged.exists()
    assert young.exists()
