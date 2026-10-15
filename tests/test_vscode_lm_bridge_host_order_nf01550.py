from __future__ import annotations

import time
from pathlib import Path

import pytest

from aiworkhub import repository_state, task_store, vscode_lm_bridge


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    repo_id = repository_state.inspect_repository(repo).manifest.repo_id
    return root, repo, repo_id


def _write_host(root: Path, repo_id: str, window_id: str) -> Path:
    path = root / "hosts" / repo_id / f"{window_id}.json"
    vscode_lm_bridge._atomic_json(  # noqa: SLF001 - contract-level test
        path,
        {
            "schema_id": vscode_lm_bridge.HOST_SCHEMA_ID,
            "repo_id": repo_id,
            "window_id": window_id,
            "models": ["glm-5.2"],
            "model_metadata": [
                {
                    "canonical": "glm-5.2",
                    "id": "glm-5.2",
                    "family": "glm-5.2",
                    "access_state": "granted",
                }
            ],
            "permission_granted": True,
            "extension_host_pid": 4242,
            "extension_version": "0.9.test",
        },
    )
    return path


def test_live_heartbeat_is_measured_despite_dead_window_files_beyond_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo, repo_id = _setup(tmp_path, monkeypatch)
    dead = [_write_host(root, repo_id, f"window_{index:02d}") for index in range(40)]
    dead_newest = max(path.stat().st_mtime for path in dead)
    live = _write_host(root, repo_id, "window_zz")
    # Keep the TTL boundary well clear of bridge_readiness's 1 ms age rounding.
    for _ in range(40):
        if live.stat().st_mtime - dead_newest >= 0.01:
            break
        time.sleep(0.05)
        live = _write_host(root, repo_id, "window_zz")
    live_mtime = live.stat().st_mtime
    gap = live_mtime - dead_newest
    assert gap >= 0.01

    now = live_mtime + 1
    monkeypatch.setattr(vscode_lm_bridge, "HOST_TTL_SECONDS", 1 + gap / 2)
    real_glob = Path.glob

    def name_ordered_glob(self: Path, pattern: str, *args: object, **kwargs: object):
        # Pin the live file past the cap on every filesystem, not only name-ordered NTFS.
        return iter(sorted(real_glob(self, pattern, *args, **kwargs), key=lambda p: p.name))

    monkeypatch.setattr(Path, "glob", name_ordered_glob)
    result = vscode_lm_bridge.bridge_readiness(repo, model=None, now=now)

    assert result["launchable"] is True
    assert result["blocker_reason"] == ""
    assert result["live_host_count"] == 1
    assert result["window_id"] == "window_zz"
    assert result["stale_host_count"] == 31
    assert all(path.exists() for path in dead)


def test_heartbeats_older_than_prune_window_are_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo, repo_id = _setup(tmp_path, monkeypatch)
    paths = [_write_host(root, repo_id, f"window_{index:02d}") for index in range(3)]

    kept = vscode_lm_bridge.bridge_readiness(
        repo,
        model=None,
        now=time.time() + vscode_lm_bridge.HOST_PRUNE_SECONDS - 10,
    )
    assert all(path.exists() for path in paths)
    assert kept["blocker_reason"] == "vscode_lm_host_stale"

    pruned = vscode_lm_bridge.bridge_readiness(
        repo,
        model=None,
        now=time.time() + vscode_lm_bridge.HOST_PRUNE_SECONDS + 10,
    )
    assert not any(path.exists() for path in paths)
    assert pruned["launchable"] is False
    assert pruned["blocker_reason"] == "vscode_lm_host_unavailable"
    again = vscode_lm_bridge.bridge_readiness(repo, model=None)
    assert again["blocker_reason"] == "vscode_lm_host_unavailable"


def test_unremovable_heartbeat_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo, repo_id = _setup(tmp_path, monkeypatch)
    paths = [_write_host(root, repo_id, f"window_{index:02d}") for index in range(3)]
    attempts: list[Path] = []

    def denied(self: Path, *_args: object, **_kwargs: object) -> None:
        attempts.append(self)
        raise PermissionError("sandbox denies unlink")

    monkeypatch.setattr(Path, "unlink", denied)
    result = vscode_lm_bridge.bridge_readiness(
        repo,
        model=None,
        now=time.time() + vscode_lm_bridge.HOST_PRUNE_SECONDS + 10,
    )

    assert len(attempts) == 3
    assert all(path.exists() for path in paths)
    assert result["launchable"] is False
    assert result["blocker_reason"] == "vscode_lm_host_unavailable"


def test_prune_skips_heartbeats_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo, repo_id = _setup(tmp_path, monkeypatch)
    path = _write_host(root, repo_id, "window_00")
    attempts: list[Path] = []
    monkeypatch.setattr(vscode_lm_bridge, "stat_owned_by_current_user", lambda _stat: False)
    monkeypatch.setattr(Path, "unlink", lambda self, *_a, **_k: attempts.append(self))

    vscode_lm_bridge.bridge_readiness(
        repo,
        model=None,
        now=time.time() + vscode_lm_bridge.HOST_PRUNE_SECONDS + 10,
    )

    assert attempts == []
    assert path.exists()
