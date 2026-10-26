from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiworkhub.storage_registry import PathEscapeError, _ensure_relative_safe


def _try_symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")


def test_root_reached_through_remapped_ancestor_resolves(tmp_path, monkeypatch):
    literal = tmp_path / "literal"
    mapped = tmp_path / "mapped"
    (mapped / ".aiworkhub").mkdir(parents=True)
    real_resolve = Path.resolve

    def fake_resolve(self, strict=False):
        if self == literal or literal in self.parents:
            return real_resolve(mapped / self.relative_to(literal), strict=strict)
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    resolved = _ensure_relative_safe(literal, ".aiworkhub/kb/kb.sqlite", "canonical_durable_path")
    assert resolved == real_resolve(mapped) / ".aiworkhub" / "kb" / "kb.sqlite"
    assert _ensure_relative_safe(literal, ".", "root") == real_resolve(mapped)


def test_root_reached_through_real_linked_ancestor_resolves(tmp_path):
    real_parent = tmp_path / "real_parent"
    (real_parent / "repo" / ".aiworkhub").mkdir(parents=True)
    link = tmp_path / "link"
    _try_symlink(link, real_parent)

    resolved = _ensure_relative_safe(link / "repo", ".aiworkhub/kb/kb.sqlite", "canonical_durable_path")
    assert resolved == (real_parent / "repo").resolve() / ".aiworkhub" / "kb" / "kb.sqlite"


def test_symlinked_hub_dir_is_still_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _try_symlink(repo / ".aiworkhub", outside)

    with pytest.raises(PathEscapeError, match="storage_registry_symlink"):
        _ensure_relative_safe(repo, ".aiworkhub/kb/kb.sqlite", "canonical_durable_path")


def test_symlinked_base_is_refused(tmp_path):
    real_repo = tmp_path / "real_repo"
    real_repo.mkdir()
    link = tmp_path / "repo_link"
    _try_symlink(link, real_repo)

    with pytest.raises(PathEscapeError, match="storage_registry_symlink"):
        _ensure_relative_safe(link, ".aiworkhub/kb/kb.sqlite", "canonical_durable_path")


def test_dotdot_absolute_and_nul_are_still_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for value in ("../escape.sqlite", ".aiworkhub/../../escape.sqlite", str(tmp_path / "abs.sqlite"), "a\x00b"):
        with pytest.raises(PathEscapeError, match="storage_registry_path_escape"):
            _ensure_relative_safe(repo, value, "canonical_durable_path")
