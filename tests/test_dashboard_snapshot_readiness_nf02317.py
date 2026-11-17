"""NF-2026-02317: the dashboard read path uses the lite storage readiness mode.

A cold dashboard snapshot spent 7.9 s in PRAGMA quick_check on every reader
respawn. The dashboard must still report a ready repository without running
that integrity scan; the scan belongs to the MCP server or doctor.
"""

from __future__ import annotations

from pathlib import Path

from aiworkhub import dashboard, task_store


def _ready_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    readiness = task_store.storage_readiness(repo)
    assert readiness.ready, readiness.reason
    task_store.reset_storage_readiness_cache()
    return repo


def test_dashboard_storage_readiness_never_runs_quick_check_on_ready_repo(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _ready_repo(tmp_path)
    original_quick_check = task_store._cached_quick_check
    calls: list[tuple] = []

    def _record_quick_check(*args, **kwargs):
        calls.append(args)
        return original_quick_check(*args, **kwargs)

    monkeypatch.setattr(task_store, "_cached_quick_check", _record_quick_check)

    readiness = dashboard.DashboardProvider(repo_root=repo).get_storage_readiness()

    assert readiness.ready, readiness.reason
    assert calls == [], "dashboard read path must not run PRAGMA quick_check"

    # Control: the full readiness check reaches the same spy, so the empty
    # record above is evidence of the lite mode and not of an unwired spy.
    task_store.reset_storage_readiness_cache()
    full = task_store.storage_readiness(repo)
    assert full.ready, full.reason
    assert calls, "full storage_readiness must reach the quick_check spy"
