from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import repository_state as rs


def test_subst_mapping_contains_existing_and_missing_children(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    (root / ".aiworkhub").mkdir(parents=True)
    backing = tmp_path / "backing"
    (backing / ".aiworkhub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    real_resolve = Path.resolve

    def resolve(self: Path, strict: bool = False) -> Path:
        try:
            tail = self.relative_to(root)
        except ValueError:
            return real_resolve(self, strict=strict)
        if self.exists():
            return real_resolve(backing / tail, strict=strict)
        # Measured subst behavior: a missing path stays on the unresolved drive.
        return self

    monkeypatch.setattr(Path, "resolve", resolve)

    existing = rs._assert_relative_child(root, ".aiworkhub")
    assert existing == real_resolve(backing / ".aiworkhub")

    missing = rs._assert_relative_child(root, ".aiworkhub/sessions")
    assert missing == real_resolve(backing / ".aiworkhub") / "sessions"

    (root / "leak").mkdir()

    def resolve_leak(self: Path, strict: bool = False) -> Path:
        if self.name == "leak":
            return real_resolve(outside, strict=strict)
        return resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_leak)
    with pytest.raises(rs.PathEscapeError, match="path_outside_repository"):
        rs._assert_relative_child(root, "leak")


def test_escape_rejections_are_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(rs.PathEscapeError, match="path_escape"):
        rs._assert_relative_child(root, "../outside")
    with pytest.raises(rs.PathEscapeError, match="path_escape"):
        rs._assert_relative_child(root, root / "child")
    with pytest.raises(rs.PathEscapeError, match="path_escape"):
        rs._assert_relative_child(root, "a\x00b")
