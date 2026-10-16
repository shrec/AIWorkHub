"""Windows hardlink + read-only removal coverage for NF-2026-01583."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from aiworkhub.worker_workspace import (
    _rmtree_workspace_owned,
    _windows_delete_ignoring_readonly,
)


def _readonly_attribute_present(path: Path) -> bool:
    """Return True when path carries FILE_ATTRIBUTE_READONLY on Windows."""
    attributes = os.lstat(path).st_file_attributes
    return bool(getattr(stat, "FILE_ATTRIBUTE_READONLY", 0) & attributes)


@pytest.mark.skipif(os.name != "nt", reason="hardlink/read-only removal is Windows-specific")
def test_rmtree_deletes_readonly_shared_inode_inside_tree_and_keeps_outside_link(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    outside = tmp_path / "outside"
    tree.mkdir()
    inside = tree / "f"
    inside.write_bytes(b"payload-bytes")
    os.link(inside, outside)
    os.chmod(inside, stat.S_IREAD)
    assert os.lstat(inside).st_nlink == 2

    try:
        _rmtree_workspace_owned(tree)
        assert not tree.exists()
        assert outside.exists()
        assert outside.read_bytes() == b"payload-bytes"
        outside_info = os.lstat(outside)
        assert outside_info.st_nlink == 1
        assert _readonly_attribute_present(outside)
    finally:
        if outside.exists():
            os.chmod(outside, stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="read-only removal regression is Windows-specific")
def test_rmtree_removes_a_readonly_unshared_file(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    target = tree / "f"
    target.write_bytes(b"regression")
    os.chmod(target, stat.S_IREAD)
    assert os.lstat(target).st_nlink == 1

    try:
        _rmtree_workspace_owned(tree)
        assert not tree.exists()
    finally:
        if target.exists():
            os.chmod(target, stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="helper is a Windows-only code path")
def test_windows_delete_ignoring_readonly_missing_path_is_false(tmp_path: Path) -> None:
    missing = str(tmp_path / "does-not-exist")
    assert os.path.exists(missing) is False
    assert _windows_delete_ignoring_readonly(missing) is False


def test_windows_delete_ignoring_readonly_is_noop_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    assert os.name != "nt"
    assert _windows_delete_ignoring_readonly("any-target") is False
