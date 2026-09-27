from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import repository_state as repo_state
from aiworkhub import tree_sitter_cache as tsc
from aiworkhub import windows_appcontainer as appcontainer
from aiworkhub import worker_workspace as ww


TREE_SITTER_CACHE_ENV = "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"
SUFFIX = ".dll" if os.name == "nt" else ".so"
GRAMMARS = ("javascript", "typescript")


def _seed_js_ts_cache(root: Path, grammars: tuple[str, ...] = GRAMMARS) -> Path:
    libs = root / "tree-sitter-language-pack" / "v1.20.0" / "libs"
    libs.mkdir(parents=True)
    for grammar in grammars:
        (libs / f"tree_sitter_{grammar}{SUFFIX}").write_bytes(grammar.encode() * 64)
    return libs


def _mirrored_libs(repo: Path) -> Path:
    return tsc.repo_mirror_root(repo) / "tree-sitter-language-pack" / "v1.20.0" / "libs"


def _explicit_source(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv(TREE_SITTER_CACHE_ENV, str(root))


def test_repo_mirror_root_sits_beside_worktrees(tmp_path: Path) -> None:
    assert tsc.repo_mirror_root(tmp_path) == (
        tmp_path.resolve() / ".aiworkhub" / "runtime" / "tree-sitter-cache"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows cache discovery contract")
def test_sanitized_env_forwards_repo_mirror_never_validation_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_app_data = tmp_path / "local"
    source_libs = _seed_js_ts_cache(local_app_data)
    private_home = tmp_path / "private-home"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv(TREE_SITTER_CACHE_ENV, raising=False)

    env = ww.sanitized_env("validation", home=private_home, repo=repo)

    assert env["HOME"] == str(private_home.resolve())
    assert env["USERPROFILE"] == str(private_home.resolve())
    assert "APPDATA" not in env
    assert "LOCALAPPDATA" not in env
    assert env[TREE_SITTER_CACHE_ENV] == str(tsc.repo_mirror_root(repo))
    assert not Path(env[TREE_SITTER_CACHE_ENV]).is_relative_to(private_home.resolve())
    assert not (private_home / "tree-sitter-cache").exists()
    mirrored = _mirrored_libs(repo)
    for grammar in GRAMMARS:
        name = f"tree_sitter_{grammar}{SUFFIX}"
        assert (mirrored / name).read_bytes() == (source_libs / name).read_bytes()


def test_sanitized_env_mirror_is_repo_local_on_every_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _seed_js_ts_cache(source)
    _explicit_source(monkeypatch, source)
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()

    env = ww.sanitized_env("validation", home=home, repo=repo)

    assert env[TREE_SITTER_CACHE_ENV] == str(tsc.repo_mirror_root(repo))
    assert not Path(env[TREE_SITTER_CACHE_ENV]).is_relative_to(home.resolve())
    assert not (home / "tree-sitter-cache").exists()


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

    env = ww.sanitized_env("validation", home=tmp_path / "private-home", repo=tmp_path)

    assert TREE_SITTER_CACHE_ENV not in env


def test_source_missing_typescript_forwards_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _seed_js_ts_cache(source, ("javascript",))
    _explicit_source(monkeypatch, source)
    repo = tmp_path / "repo"
    repo.mkdir()

    env = ww.sanitized_env("validation", home=tmp_path / "home", repo=repo)

    assert TREE_SITTER_CACHE_ENV not in env
    assert not tsc.repo_mirror_root(repo).exists()
    assert tsc.mirror_tree_sitter_cache(source, tmp_path / "direct") is None


def test_unwritable_destination_falls_back_to_trusted_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _seed_js_ts_cache(source)
    _explicit_source(monkeypatch, source)
    repo = tmp_path / "repo"
    mirror = tsc.repo_mirror_root(repo)
    mirror.parent.mkdir(parents=True)
    # A regular file where the mirror directory must go makes mkdir fail.
    mirror.write_bytes(b"not a directory")

    env = ww.sanitized_env("validation", home=tmp_path / "home", repo=repo)

    assert env[TREE_SITTER_CACHE_ENV] == str(source.resolve())
    assert tsc.mirror_tree_sitter_cache(source, mirror) is None


def test_mirror_copies_only_grammar_library_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    libs = _seed_js_ts_cache(source)
    (libs / "nested").mkdir()
    (libs / "nested" / f"tree_sitter_python{SUFFIX}").write_bytes(b"python")
    (libs / "README.txt").write_bytes(b"notes")
    (libs / f"tree_sitter_empty{SUFFIX}").write_bytes(b"")
    version = libs.parent
    (version / "bundles").mkdir()
    (version / "bundles" / f"tree_sitter_go{SUFFIX}").write_bytes(b"go")
    (version / "manifest.json").write_text("{}", encoding="utf-8")
    destination = tsc.repo_mirror_root(tmp_path / "repo")

    mirrored = tsc.mirror_tree_sitter_cache(source, destination)

    assert mirrored == destination.resolve()
    mirrored_version = destination / "tree-sitter-language-pack" / "v1.20.0"
    assert sorted(p.name for p in mirrored_version.iterdir()) == ["libs"]
    assert sorted(p.name for p in (mirrored_version / "libs").iterdir()) == sorted(
        f"tree_sitter_{grammar}{SUFFIX}" for grammar in GRAMMARS
    )


def test_second_mirror_with_unchanged_sources_copies_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _seed_js_ts_cache(source)
    destination = tsc.repo_mirror_root(tmp_path / "repo")
    assert tsc.mirror_tree_sitter_cache(source, destination) == destination.resolve()
    libs = destination / "tree-sitter-language-pack" / "v1.20.0" / "libs"
    before = {p.name: p.stat().st_mtime_ns for p in libs.iterdir()}

    def _no_copy(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unchanged grammar library was copied again")

    monkeypatch.setattr(tsc.shutil, "copyfile", _no_copy)

    assert tsc.mirror_tree_sitter_cache(source, destination) == destination.resolve()
    assert {p.name: p.stat().st_mtime_ns for p in libs.iterdir()} == before


def test_changed_source_is_swapped_in_without_leftover_temporaries(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source_libs = _seed_js_ts_cache(source)
    destination = tsc.repo_mirror_root(tmp_path / "repo")
    assert tsc.mirror_tree_sitter_cache(source, destination) == destination.resolve()
    name = f"tree_sitter_typescript{SUFFIX}"
    (source_libs / name).write_bytes(b"updated typescript grammar" * 8)

    assert tsc.mirror_tree_sitter_cache(source, destination) == destination.resolve()
    libs = destination / "tree-sitter-language-pack" / "v1.20.0" / "libs"
    assert (libs / name).read_bytes() == (source_libs / name).read_bytes()
    assert sorted(p.name for p in libs.iterdir()) == sorted(
        f"tree_sitter_{grammar}{SUFFIX}" for grammar in GRAMMARS
    )


def test_home_none_and_non_validation_adapters_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _seed_js_ts_cache(source)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.delenv(TREE_SITTER_CACHE_ENV, raising=False)
    baseline = {
        adapter: ww.sanitized_env(adapter, home=home)
        for adapter in ("claude_cli", "codex_cli", "opencode_cli")
    }
    monkeypatch.setenv(TREE_SITTER_CACHE_ENV, str(source))

    for adapter, expected in baseline.items():
        assert ww.sanitized_env(adapter, home=home) == expected
        assert ww.sanitized_env(adapter, home=home, repo=repo) == expected
    assert not tsc.repo_mirror_root(repo).exists()
    env = ww.sanitized_env("validation", repo=repo)
    assert env[TREE_SITTER_CACHE_ENV] == str(source.resolve())
    assert not tsc.repo_mirror_root(repo).exists()


def test_mirror_refuses_link_planted_at_nested_libs(
    tmp_path: Path, make_symlink: object
) -> None:
    source = tmp_path / "source"
    _seed_js_ts_cache(source)
    destination = tsc.repo_mirror_root(tmp_path / "repo")
    version = destination / "tree-sitter-language-pack" / "v1.20.0"
    version.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(version / "libs"))
    else:
        make_symlink(outside, version / "libs")  # type: ignore[operator]

    assert tsc.mirror_tree_sitter_cache(source, destination) is None
    assert list(outside.iterdir()) == []


def test_grammars_split_across_versions_are_not_parser_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    pack = source / "tree-sitter-language-pack"
    for version, grammar in (("v1.19.0", "javascript"), ("v1.20.0", "typescript")):
        libs = pack / version / "libs"
        libs.mkdir(parents=True)
        (libs / f"tree_sitter_{grammar}{SUFFIX}").write_bytes(grammar.encode() * 64)
    _explicit_source(monkeypatch, source)

    assert tsc.trusted_tree_sitter_cache_root() is None


def test_mirror_read_grants_only_for_the_forwarded_mirror(tmp_path: Path) -> None:
    mirror = str(tsc.repo_mirror_root(tmp_path))

    assert tsc.mirror_read_grants(tmp_path, {TREE_SITTER_CACHE_ENV: mirror}) == [
        appcontainer.ContainerGrant(mirror, "read_execute", persistent=True)
    ]
    assert tsc.mirror_read_grants(tmp_path, {}) == []
    assert tsc.mirror_read_grants(
        tmp_path, {TREE_SITTER_CACHE_ENV: str(tmp_path / "elsewhere")}
    ) == []


class _LaunchCaptured(Exception):
    pass


def test_appcontainer_validation_launch_passes_the_mirror_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = "a" * 32
    request_root = tmp_path / ".aiworkhub" / "runtime" / "worktrees" / request_id
    worktree, home = request_root / "worktree", request_root / "home"
    worktree.mkdir(parents=True)
    home.mkdir()
    workspace = SimpleNamespace(
        repo=tmp_path, path=worktree, home=home, request_id=request_id
    )
    mirror = str(tsc.repo_mirror_root(tmp_path))
    captured: list[appcontainer.AppContainerRequest] = []

    def _fake_launch(request: appcontainer.AppContainerRequest) -> None:
        captured.append(request)
        raise _LaunchCaptured

    monkeypatch.setattr(
        repo_state,
        "inspect_repository",
        lambda _repo: SimpleNamespace(manifest=SimpleNamespace(repo_id="repo_test")),
    )
    monkeypatch.setattr(appcontainer, "request_scoped_grants", lambda _env: [])
    monkeypatch.setattr(appcontainer, "python_read_grants", lambda *_a, **_k: [])
    monkeypatch.setattr(appcontainer, "native_handle", lambda fd: fd)
    monkeypatch.setattr(appcontainer, "launch_appcontainer", _fake_launch)

    with pytest.raises(_LaunchCaptured):
        ww._run_appcontainer_validation(
            ["tool.exe", "--version"], workspace=workspace, adapter_id="claude_cli",
            cwd=worktree, env={"HOME": str(home), TREE_SITTER_CACHE_ENV: mirror},
            timeout_seconds=30,
        )

    grant = appcontainer.ContainerGrant(mirror, "read_execute", persistent=True)
    assert list(captured[0].filesystem_grants).count(grant) == 1
