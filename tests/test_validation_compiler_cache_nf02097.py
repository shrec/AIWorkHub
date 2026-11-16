"""NF-2026-02097: validation lanes share one content-addressed compiler cache."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import validation_compiler_cache as vcc
from aiworkhub.worker_workspace import VSCODE_LM_IN_PROCESS_BACKEND, WINDOWS_APPCONTAINER_BACKEND

BASE_A = "a" * 40
BASE_B = "b" * 64


def _which(bin_dir: Path, *present: str):
    def fake(name: str, path: str | None = None) -> str | None:
        return str(bin_dir / name) if name in present else None

    return fake


def _cache_root(repo: Path) -> Path:
    return repo / ".aiworkhub" / "runtime" / "compiler-cache"


def _env(
    repo: Path, workspace: Path, *present: str, base_oid: str | None = BASE_A, **kwargs
) -> dict[str, str]:
    return vcc.compiler_cache_env(
        repo,
        VSCODE_LM_IN_PROCESS_BACKEND,
        base_oid,
        workspace,
        which=_which(repo.parent / "bin", *present),
        **kwargs,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def test_reproduction_ccache_env_points_at_in_repo_cache_with_cmake_launchers(
    repo: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "worktree-a"

    env = _env(repo, workspace, "ccache", "sccache")

    assert Path(env["CCACHE_DIR"]) == _cache_root(repo) / BASE_A / "ccache"
    assert Path(env["CCACHE_DIR"]).is_dir()
    assert env["CCACHE_BASEDIR"] == str(workspace)
    assert env["CCACHE_NOHASHDIR"] == "1"
    assert env["CCACHE_MAXSIZE"] == vcc.COMPILER_CACHE_MAX_SIZE
    launcher = str(tmp_path / "bin" / "ccache")
    assert env["CMAKE_C_COMPILER_LAUNCHER"] == launcher
    assert env["CMAKE_CXX_COMPILER_LAUNCHER"] == launcher
    assert Path(env["CMAKE_C_COMPILER_LAUNCHER"]).is_absolute()
    assert Path(env["CMAKE_CXX_COMPILER_LAUNCHER"]).is_absolute()
    assert not any(key.startswith("SCCACHE") or key == "RUSTC_WRAPPER" for key in env)


def test_reproduction_two_workspace_roots_share_one_cache_dir(repo: Path, tmp_path: Path) -> None:
    first = _env(repo, tmp_path / "worktree-a", "ccache")
    second = _env(repo, tmp_path / "worktree-b", "ccache")

    assert first["CCACHE_DIR"] == second["CCACHE_DIR"]
    assert first["CCACHE_BASEDIR"] != second["CCACHE_BASEDIR"]


def test_reproduction_different_base_oids_get_different_cache_dirs(
    repo: Path, tmp_path: Path
) -> None:
    first = _env(repo, tmp_path / "w", "ccache", base_oid=BASE_A)
    second = _env(repo, tmp_path / "w", "ccache", base_oid=BASE_B)

    assert first["CCACHE_DIR"] != second["CCACHE_DIR"]
    assert Path(second["CCACHE_DIR"]) == _cache_root(repo) / BASE_B / "ccache"


def test_regression_no_cache_tool_returns_empty_env(repo: Path, tmp_path: Path) -> None:
    assert _env(repo, tmp_path / "w") == {}
    assert not (repo / ".aiworkhub").exists()


def test_regression_sccache_alone_is_not_used(repo: Path, tmp_path: Path) -> None:
    assert _env(repo, tmp_path / "w", "sccache") == {}
    assert not (repo / ".aiworkhub").exists()


def test_regression_relative_launcher_returns_empty_env(repo: Path, tmp_path: Path) -> None:
    env = vcc.compiler_cache_env(
        repo,
        VSCODE_LM_IN_PROCESS_BACKEND,
        BASE_A,
        tmp_path / "w",
        which=lambda name, path=None: "ccache",
    )

    assert env == {}
    assert not (repo / ".aiworkhub").exists()


@pytest.mark.parametrize("base_oid", [None, "", "HEAD", "a" * 39, "../" + "a" * 37, "A" * 40])
def test_regression_unknown_base_oid_returns_empty_env(
    repo: Path, tmp_path: Path, base_oid: str | None
) -> None:
    assert _env(repo, tmp_path / "w", "ccache", base_oid=base_oid) == {}
    assert not (repo / ".aiworkhub").exists()


def test_regression_sandboxed_backend_without_writable_grant_returns_empty_env(
    repo: Path, tmp_path: Path
) -> None:
    for backend in ("landlock", "bubblewrap", WINDOWS_APPCONTAINER_BACKEND):
        env = vcc.compiler_cache_env(
            repo, backend, BASE_A, tmp_path / "w", which=_which(tmp_path / "bin", "ccache")
        )
        assert env == {}
    assert not (repo / ".aiworkhub").exists()


def test_regression_symlinked_cache_dir_returns_empty_env(repo: Path, tmp_path: Path) -> None:
    runtime = repo / ".aiworkhub" / "runtime"
    runtime.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (runtime / "compiler-cache").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable on this host: {exc}")

    assert _env(repo, tmp_path / "w", "ccache") == {}
    assert not (outside / BASE_A).exists()


def _fake_lstat(attributes_for):
    real_lstat = os.lstat

    def fake(path):
        info = real_lstat(path)
        fields = {"st_mode": info.st_mode}
        attributes = attributes_for(Path(path))
        if attributes is not None:
            fields["st_file_attributes"] = attributes
        return SimpleNamespace(**fields)

    return fake


def _remove_is_junction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(type(tmp_path), "is_junction", None, raising=False)
    monkeypatch.setattr(vcc, "_WINDOWS", True)


def test_regression_junction_fallback_uses_reparse_point_attribute(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remove_is_junction(monkeypatch, tmp_path)
    reparse = stat.FILE_ATTRIBUTE_REPARSE_POINT
    monkeypatch.setattr(
        vcc,
        "_lstat",
        _fake_lstat(lambda p: reparse if p.name == "compiler-cache" else 0),
    )

    assert _env(repo, tmp_path / "w", "ccache") == {}
    assert not (_cache_root(repo) / BASE_A).exists()


def test_regression_junction_fallback_admits_real_directories(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remove_is_junction(monkeypatch, tmp_path)
    monkeypatch.setattr(vcc, "_lstat", _fake_lstat(lambda p: 0))

    env = _env(repo, tmp_path / "w", "ccache")

    assert Path(env["CCACHE_DIR"]) == _cache_root(repo) / BASE_A / "ccache"


def test_regression_no_junction_probe_at_all_refuses(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remove_is_junction(monkeypatch, tmp_path)
    monkeypatch.setattr(vcc, "_lstat", _fake_lstat(lambda p: None))
    (repo / ".aiworkhub").mkdir()

    assert _env(repo, tmp_path / "w", "ccache") == {}
    assert not (repo / ".aiworkhub" / "runtime").exists()


def test_regression_non_directory_cache_path_returns_empty_env(
    repo: Path, tmp_path: Path
) -> None:
    runtime = repo / ".aiworkhub" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "compiler-cache").write_text("not a directory", encoding="utf-8")

    assert _env(repo, tmp_path / "w", "ccache") == {}


def test_regression_unwritable_cache_dir_returns_empty_env(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vcc.os, "access", lambda *args, **kwargs: False)

    assert _env(repo, tmp_path / "w", "ccache") == {}


def test_regression_card_declared_ccache_dir_is_not_overridden(
    repo: Path, tmp_path: Path
) -> None:
    declared = {"CCACHE_DIR": "card-owned", "CMAKE_C_COMPILER_LAUNCHER": "card-launcher"}

    env = _env(repo, tmp_path / "w", "ccache", declared_env=declared)

    assert "CCACHE_DIR" not in env
    assert "CMAKE_C_COMPILER_LAUNCHER" not in env
    assert env["CMAKE_CXX_COMPILER_LAUNCHER"] == str(tmp_path / "bin" / "ccache")
    assert {**env, **declared}["CCACHE_DIR"] == "card-owned"


def test_regression_which_searches_declared_path(repo: Path, tmp_path: Path) -> None:
    seen: list[str | None] = []

    def which(name: str, path: str | None = None) -> str | None:
        seen.append(path)
        return None

    vcc.compiler_cache_env(
        repo,
        VSCODE_LM_IN_PROCESS_BACKEND,
        BASE_A,
        tmp_path / "w",
        {"PATH": os.pathsep.join(["a", "b"])},
        which=which,
    )

    assert seen == [os.pathsep.join(["a", "b"])]


def test_regression_stale_sibling_bases_are_pruned_fresh_ones_kept(
    repo: Path, tmp_path: Path
) -> None:
    root = _cache_root(repo)
    stale, fresh, other = root / BASE_B, root / ("c" * 40), root / "not-a-commit"
    for path in (stale, fresh, other):
        (path / "ccache").mkdir(parents=True)
    old = os.stat(stale).st_mtime - vcc.COMPILER_CACHE_STALE_SECONDS - 60
    for path in (stale, other):
        os.utime(path, (old, old))

    env = _env(repo, tmp_path / "w", "ccache")

    assert Path(env["CCACHE_DIR"]).is_dir()
    assert not stale.exists()
    assert fresh.is_dir() and other.is_dir()
