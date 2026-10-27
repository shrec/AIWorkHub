from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import repository_state as rs


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    (root / ".aiworkhub").mkdir(parents=True)
    backing = tmp_path / "backing"
    (backing / ".aiworkhub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    return root, backing, outside


def test_root_remapped_while_child_resolve_stays_on_subst_drive(tmp_path: Path, monkeypatch) -> None:
    root, backing, _ = _layout(tmp_path)
    real_resolve = Path.resolve

    def resolve(self: Path, strict: bool = False) -> Path:
        if self == root:
            return real_resolve(backing, strict=strict)
        try:
            self.relative_to(root)
        except ValueError:
            return real_resolve(self, strict=strict)
        # The child's handle open was denied: it stays on the unresolved drive.
        return self

    monkeypatch.setattr(Path, "resolve", resolve)

    existing = rs._assert_relative_child(root, ".aiworkhub")
    assert existing == real_resolve(backing) / ".aiworkhub"

    missing = rs._assert_relative_child(root, ".aiworkhub/sessions")
    assert missing == real_resolve(backing) / ".aiworkhub" / "sessions"


def test_root_resolve_stays_on_subst_drive_while_child_remaps(tmp_path: Path, monkeypatch) -> None:
    root, backing, _ = _layout(tmp_path)
    real_resolve = Path.resolve

    def resolve(self: Path, strict: bool = False) -> Path:
        if self == root:
            # The root's handle open was denied: it stays on the unresolved drive.
            return self
        try:
            tail = self.relative_to(root)
        except ValueError:
            return real_resolve(self, strict=strict)
        return real_resolve(backing / tail, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)

    existing = rs._assert_relative_child(root, ".aiworkhub")
    assert existing == real_resolve(backing / ".aiworkhub")

    missing = rs._assert_relative_child(root, ".aiworkhub/sessions")
    assert missing == real_resolve(backing / ".aiworkhub") / "sessions"


@pytest.mark.parametrize("root_remapped", [True, False])
def test_child_resolving_to_unrelated_directory_is_refused(
    tmp_path: Path, monkeypatch, root_remapped: bool
) -> None:
    root, backing, outside = _layout(tmp_path)
    (root / "leak").mkdir()
    real_resolve = Path.resolve

    def resolve(self: Path, strict: bool = False) -> Path:
        if self == root:
            return real_resolve(backing, strict=strict) if root_remapped else self
        if self.name == "leak":
            # A junction/reparse redirect lands outside the repository.
            return real_resolve(outside, strict=strict)
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)

    with pytest.raises(rs.PathEscapeError, match="path_outside_repository"):
        rs._assert_relative_child(root, "leak")
    with pytest.raises(rs.PathEscapeError, match="path_outside_repository"):
        rs._assert_relative_child(root, "leak/child")


def test_reparse_component_blocks_the_mismatched_basis_fallback(tmp_path: Path, monkeypatch) -> None:
    root, backing, _ = _layout(tmp_path)
    real_resolve = Path.resolve

    def resolve(self: Path, strict: bool = False) -> Path:
        if self == root:
            return self
        try:
            tail = self.relative_to(root)
        except ValueError:
            return real_resolve(self, strict=strict)
        return real_resolve(backing / tail, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(rs, "_has_reparse_component", lambda _root, _rel: True)

    with pytest.raises(rs.PathEscapeError, match="path_outside_repository"):
        rs._assert_relative_child(root, ".aiworkhub")


def test_has_reparse_component_on_plain_directories(tmp_path: Path) -> None:
    root, _, _ = _layout(tmp_path)
    assert rs._has_reparse_component(root, Path(".aiworkhub")) is False
    assert rs._has_reparse_component(root, Path(".aiworkhub/missing/deeper")) is False
