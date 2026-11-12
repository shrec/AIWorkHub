"""NF-2026-02195: manager identity stamping never runs a cold PRAGMA quick_check.

Measured: ``retry_finalization`` admission (30 s bound) spent 28 s once per
process in ``core._codex_shared_repo_route_manager_identity`` ->
``task_store.storage_readiness`` -> ``_cached_quick_check`` -> ``quick_check``.
Identity only needs the route DB present and openable, so it asks for
``storage_readiness(root, integrity_check=False)``; every default caller keeps
the full integrity scan.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, task_store  # noqa: E402


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    task_store.reset_storage_readiness_cache()
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.delenv("AIWORKHUB_WINDOW_ID", raising=False)
    yield root
    task_store.reset_storage_readiness_cache()


@pytest.fixture
def integrity_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real_cached = task_store._cached_quick_check

    def recording_cached(path: Path) -> str:
        calls.append(str(path))
        return real_cached(path)

    monkeypatch.setattr(task_store, "_cached_quick_check", recording_cached)
    return calls


def _forbid_quick_check(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(path: Path) -> str:
        raise AssertionError(f"identity stamping ran PRAGMA quick_check on {path}")

    monkeypatch.setattr(task_store, "quick_check", refuse)


def _shared_route(repo_id: str):
    def list_known_repositories(*, current_root, limit=32, include_inactive=False):
        return {
            "ok": True,
            "repositories": [{
                "repo_id": repo_id,
                "current_repo": True,
                "extension_host_alive": True,
                "stale": False,
                "selected_provider": "codex",
                "window_id": "window_live",
                "targets": {"codex": {
                    "capability_state": "route_pending",
                    "route": {"repo_id": repo_id, "window_id": "window_live",
                              "thread_id": "", "session_id": "episode_live"},
                }},
            }],
        }

    return list_known_repositories


def test_identity_stamping_never_calls_quick_check(repo, integrity_calls, monkeypatch):
    _forbid_quick_check(monkeypatch)
    repo_id = task_store.inspect_repository(repo).manifest.repo_id
    monkeypatch.setattr(core.shared_router, "list_known_repositories", _shared_route(repo_id))
    monkeypatch.setattr(core.manager_route_probe, "claude_ancestry_present", lambda: False)

    identity = core._codex_shared_repo_route_manager_identity()

    assert integrity_calls == []
    assert identity == {
        "provider": "codex",
        "session_id": "episode_live",
        "thread_id": "",
        "window_id": "window_live",
        "callback_supported": "false",
        "route_state": "route_pending",
    }


def test_lite_readiness_skips_quick_check_and_is_never_memoized(repo, integrity_calls, monkeypatch):
    real_quick_check = task_store.quick_check
    _forbid_quick_check(monkeypatch)

    lite = task_store.storage_readiness(repo, integrity_check=False)
    again = task_store.storage_readiness(repo, integrity_check=False)

    assert lite.ready and again.ready
    assert lite.repo_id == task_store.inspect_repository(repo).manifest.repo_id
    assert integrity_calls == []

    # A lite verdict must not satisfy a later default (integrity-checked) caller.
    calls: list[str] = []

    def recording_quick_check(path: Path) -> str:
        calls.append(str(path))
        return real_quick_check(path)

    monkeypatch.setattr(task_store, "quick_check", recording_quick_check)
    assert task_store.storage_readiness(repo).ready
    assert len(integrity_calls) == 1
    assert len(calls) == 1


def test_default_storage_readiness_still_runs_quick_check(repo, integrity_calls, monkeypatch):
    calls: list[str] = []

    def failing_quick_check(path: Path) -> str:
        calls.append(str(path))
        return "row 1 missing from index"

    monkeypatch.setattr(task_store, "quick_check", failing_quick_check)

    readiness = task_store.storage_readiness(repo)

    assert len(integrity_calls) == 1
    assert len(calls) == 1
    assert not readiness.ready
    assert readiness.reason.startswith("quick_check_failed:")
