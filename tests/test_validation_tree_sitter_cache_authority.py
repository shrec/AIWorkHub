from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiworkhub import worker_workspace as ww


TREE_SITTER_CACHE_ENV = "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"


def _seed_js_ts_cache(root: Path) -> None:
    libs = root / "tree-sitter-language-pack" / "v1.20.0" / "libs"
    libs.mkdir(parents=True)
    suffix = ".dll" if os.name == "nt" else ".so"
    (libs / f"tree_sitter_javascript{suffix}").write_bytes(b"javascript")
    (libs / f"tree_sitter_typescript{suffix}").write_bytes(b"typescript")


@pytest.mark.skipif(os.name != "nt", reason="Windows cache discovery contract")
def test_sanitized_env_preserves_only_validated_tree_sitter_cache_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_app_data = tmp_path / "local"
    _seed_js_ts_cache(local_app_data)
    private_home = tmp_path / "private-home"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv(TREE_SITTER_CACHE_ENV, raising=False)

    env = ww.sanitized_env("validation", home=private_home)

    assert env["HOME"] == str(private_home.resolve())
    assert env["USERPROFILE"] == str(private_home.resolve())
    assert "APPDATA" not in env
    assert "LOCALAPPDATA" not in env
    assert env[TREE_SITTER_CACHE_ENV] == str(local_app_data.resolve())


@pytest.mark.skipif(os.name != "nt", reason="Windows cache discovery contract")
@pytest.mark.parametrize("shape", ["missing", "empty", "reparse"])
def test_sanitized_env_rejects_untrusted_tree_sitter_cache_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    local_app_data = tmp_path / "local"
    local_app_data.mkdir()
    pack = local_app_data / "tree-sitter-language-pack"
    if shape == "empty":
        (pack / "v1.20.0" / "libs").mkdir(parents=True)
    elif shape == "reparse":
        target = tmp_path / "outside"
        _seed_js_ts_cache(target)
        try:
            pack.symlink_to(target / "tree-sitter-language-pack", target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable on this Windows host")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv(TREE_SITTER_CACHE_ENV, str(local_app_data))

    env = ww.sanitized_env("validation", home=tmp_path / "private-home")

    assert TREE_SITTER_CACHE_ENV not in env
