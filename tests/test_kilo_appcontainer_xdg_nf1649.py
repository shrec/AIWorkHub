from __future__ import annotations

import os
from pathlib import Path

from aiworkhub import process_launcher
from aiworkhub.windows_appcontainer import request_scoped_grants

_XDG_KEYS = (
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
)
_KILO_BUN_KEYS = (*_XDG_KEYS, "BUN_TMPDIR")


def _grant_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def test_kilo_appcontainer_xdg_and_bun_tmpdir_are_precreated_and_modify_granted(
    tmp_path: Path,
) -> None:
    """NF-2026-01649: Bun realpath() EPERM target is XDG_STATE_HOME.

    Measured cause: kilo AppContainer handed Bun XDG_STATE_HOME as isolated
    HOME\\.local\\state (the path Bun realpaths at startup). That leaf was
    mkdir'd, but sibling XDG data/cache/config dirs were not pre-created and
    were omitted from the request-scoped modify grant plan; BUN_TMPDIR was
    also unset, unlike the working opencode_cli launch.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    request = tmp_path / ".aiworkhub" / "runtime" / "worktrees" / "req-nf1649"
    home = request / "home"
    worktree = request / "worktree"
    home.mkdir(parents=True)
    worktree.mkdir()

    env = process_launcher.worker_launch_env(
        "grok_kilo_cli",
        repo=repo,
        request_id="req-nf1649",
        home=home,
        sandbox_backend="windows_appcontainer",
    )
    opencode = process_launcher.worker_launch_env(
        "opencode_cli",
        repo=repo,
        request_id="req-nf1649-oc",
        home=home,
        sandbox_backend="windows_appcontainer",
    )

    home_root = home.resolve()
    assert env["BUN_TMPDIR"] == env["TMPDIR"]
    assert env["BUN_TMPDIR"] == opencode["BUN_TMPDIR"] == opencode["TMPDIR"]
    assert Path(env["BUN_TMPDIR"]) == home_root / "tmp"

    grants = request_scoped_grants(env, str(worktree))
    modify = {
        _grant_key(grant.path) for grant in grants if grant.access == "modify"
    }
    for key in _KILO_BUN_KEYS:
        path = Path(env[key])
        assert path.is_dir(), key
        assert path == path.resolve(), key
        assert home_root in path.parents, key
        assert _grant_key(str(path)) in modify, key


def test_non_appcontainer_and_non_kilo_launch_envs_are_unchanged(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "plain-home"

    kilo_plain = process_launcher.worker_launch_env(
        "grok_kilo_cli",
        repo=repo,
        request_id="req-kilo-plain",
        home=home,
        sandbox_backend=None,
    )
    assert "BUN_TMPDIR" not in kilo_plain
    assert Path(kilo_plain["XDG_STATE_HOME"]) == (
        Path(kilo_plain["HOME"]) / ".local" / "state"
    )
    assert not Path(kilo_plain["XDG_STATE_HOME"]).exists()
    assert not Path(kilo_plain["XDG_CACHE_HOME"]).exists()
    assert not Path(kilo_plain["XDG_DATA_HOME"]).exists()
    assert not Path(kilo_plain["XDG_CONFIG_HOME"]).exists()

    claude = process_launcher.worker_launch_env(
        "claude_cli",
        repo=repo,
        request_id="req-claude-ac",
        home=home,
        sandbox_backend="windows_appcontainer",
    )
    assert not any(key.startswith("XDG_") for key in claude)
    assert "BUN_TMPDIR" not in claude

    opencode = process_launcher.worker_launch_env(
        "opencode_cli",
        repo=repo,
        request_id="req-opencode-ac",
        home=home,
        sandbox_backend="windows_appcontainer",
    )
    assert opencode["BUN_TMPDIR"] == opencode["TMPDIR"]
    assert not any(key.startswith("XDG_") for key in opencode)
