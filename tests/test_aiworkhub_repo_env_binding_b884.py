from __future__ import annotations

import importlib

import pytest

from aiworkhub import core
from aiworkhub import callback_bridge


def test_core_repo_root_prefers_canonical_root_and_accepts_matching_legacy_symlink(
    tmp_path, monkeypatch
):
    real = tmp_path / "real_repo"
    real.mkdir()
    link = tmp_path / "repo_link"
    link.symlink_to(real, target_is_directory=True)

    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(real))
    monkeypatch.setenv("AIWORKHUB_REPO", str(link))

    assert core.repo_root() == real.resolve()


def test_core_repo_root_refuses_mismatched_legacy_binding(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical_repo"
    legacy = tmp_path / "stale_legacy_repo"
    canonical.mkdir()
    legacy.mkdir()

    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(canonical))
    monkeypatch.setenv("AIWORKHUB_REPO", str(legacy))

    with pytest.raises(RuntimeError, match="repo_root_env_mismatch"):
        core.repo_root()


def test_callback_bridge_uses_same_canonical_repo_binding(tmp_path, monkeypatch):
    real = tmp_path / "real_repo"
    real.mkdir()
    link = tmp_path / "repo_link"
    link.symlink_to(real, target_is_directory=True)

    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(real))
    monkeypatch.setenv("AIWORKHUB_REPO", str(link))

    # CALLBACK_CWD is computed at import time; reload proves new bridge
    # processes bind to the canonical root instead of a stale legacy alias.
    module = importlib.reload(importlib.import_module("aiworkhub.callback_bridge"))
    assert module.CALLBACK_CWD == str(real.resolve())


def test_callback_bridge_refuses_mismatched_legacy_binding(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical_repo"
    legacy = tmp_path / "stale_legacy_repo"
    canonical.mkdir()
    legacy.mkdir()

    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(canonical))
    monkeypatch.setenv("AIWORKHUB_REPO", str(legacy))

    with pytest.raises(RuntimeError, match="repo_root_env_mismatch"):
        importlib.reload(importlib.import_module("aiworkhub.callback_bridge"))

    monkeypatch.setenv("AIWORKHUB_REPO", str(canonical))
    importlib.reload(importlib.import_module("aiworkhub.callback_bridge"))
