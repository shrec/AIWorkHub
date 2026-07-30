from __future__ import annotations

import json
import os
import time
from pathlib import Path

from aiworkhub import shared_router, task_store


def _write_record(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_shared_router_lists_current_repo_and_preserves_repo_local_authority(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    repo_id = task_store.storage_readiness(repo).repo_id
    monkeypatch.setattr(shared_router.Path, "home", lambda: home)

    _write_record(
        shared_router.registry_dir(home) / f"{repo_id}.json",
        {
            "schema_id": shared_router.SCHEMA_ID,
            "repo_id": repo_id,
            "repo_name": "repo",
            "repo_root": str(repo),
            "window_id": "window_a",
            "extension_host_pid": 99999999,
            "selected_provider": "codex",
            "targets": {"codex": {"capability_state": "route_pending"}},
            "updated_at": "2026-07-24T00:00:00Z",
            "lease_expires_at": "2026-07-24T00:15:00Z",
        },
    )

    result = shared_router.list_known_repositories(current_root=repo, include_inactive=True)

    assert result["ok"] is True
    assert result["repositories"][0]["repo_id"] == repo_id
    assert result["repositories"][0]["current_repo"] is True
    assert result["repositories"][0]["selected_provider"] == "codex"
    assert result["repositories"][0]["repo_root"] == str(repo.resolve())


def test_shared_router_rejects_cross_repo_manifest_mismatch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    actual_repo_id = task_store.storage_readiness(repo).repo_id
    wrong_repo_id = "repo_" + ("0" * 32)
    assert wrong_repo_id != actual_repo_id
    monkeypatch.setattr(shared_router.Path, "home", lambda: home)

    _write_record(
        shared_router.registry_dir(home) / f"{wrong_repo_id}.json",
        {
            "schema_id": shared_router.SCHEMA_ID,
            "repo_id": wrong_repo_id,
            "repo_name": "repo",
            "repo_root": str(repo),
            "window_id": "window_wrong",
            "extension_host_pid": 99999999,
            "selected_provider": "codex",
            "targets": {},
            "updated_at": "2026-07-24T00:00:00Z",
            "lease_expires_at": "2026-07-24T00:15:00Z",
        },
    )

    result = shared_router.list_known_repositories(current_root=repo, include_inactive=True)

    assert result["repositories"] == []
    assert result["rejects"][0]["error"] == "manifest_repo_id_mismatch"
    assert result["rejects"][0]["repo_id"] == wrong_repo_id
    assert result["rejects"][0]["manifest_repo_id"] == actual_repo_id


def test_shared_router_marks_old_records_stale(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    repo_id = task_store.storage_readiness(repo).repo_id
    monkeypatch.setattr(shared_router.Path, "home", lambda: home)
    record = shared_router.registry_dir(home) / f"{repo_id}.json"
    _write_record(
        record,
        {
            "schema_id": shared_router.SCHEMA_ID,
            "repo_id": repo_id,
            "repo_name": "repo",
            "repo_root": str(repo),
            "window_id": "window_stale",
            "extension_host_pid": 99999999,
            "selected_provider": "claude",
            "targets": {},
            "updated_at": "2026-07-24T00:00:00Z",
            "lease_expires_at": "2026-07-24T00:15:00Z",
        },
    )
    old = time.time() - (shared_router.DEFAULT_TTL_SECONDS + 30)
    os.utime(record, (old, old))

    result = shared_router.list_known_repositories(current_root=repo, include_inactive=True)

    assert result["repositories"][0]["stale"] is True


def test_shared_router_hides_inactive_foreign_records_by_default(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    repo_id = task_store.storage_readiness(repo).repo_id
    monkeypatch.setattr(shared_router.Path, "home", lambda: home)
    record = shared_router.registry_dir(home) / f"{repo_id}.json"
    _write_record(
        record,
        {
            "schema_id": shared_router.SCHEMA_ID,
            "repo_id": repo_id,
            "repo_name": "repo",
            "repo_root": str(repo),
            "window_id": "window_dead",
            "extension_host_pid": 99999999,
            "selected_provider": "codex",
            "targets": {},
            "updated_at": "2026-07-24T00:00:00Z",
            "lease_expires_at": "2026-07-24T00:15:00Z",
        },
    )

    result = shared_router.list_known_repositories(current_root=tmp_path / "other")

    assert result["repositories"] == []
    assert result["inactive"][0]["repo_id"] == repo_id


def test_shared_router_resolves_exact_live_thread_without_caller_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    repo_id = task_store.storage_readiness(repo).repo_id
    monkeypatch.setattr(shared_router.Path, "home", lambda: home)
    _write_record(
        shared_router.registry_dir(home) / f"{repo_id}.json",
        {
            "schema_id": shared_router.SCHEMA_ID,
            "repo_id": repo_id,
            "repo_name": "repo",
            "repo_root": str(repo),
            "window_id": "window_exact",
            "extension_host_pid": os.getpid(),
            "selected_provider": "codex",
            "targets": {
                "codex": {
                    "route": {
                        "repo_id": repo_id,
                        "window_id": "window_exact",
                        "thread_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
                    }
                }
            },
            "updated_at": "2026-07-30T00:00:00Z",
            "lease_expires_at": "2026-07-30T00:15:00Z",
        },
    )

    result = shared_router.resolve_repository_route(
        provider="codex",
        thread_id="019f5097-6dbe-7172-870a-945afc5f3bfa",
        extension_host_pid=os.getpid(),
    )

    assert result["ok"] is True
    assert result["repo_id"] == repo_id
    assert result["repo_root"] == str(repo.resolve())


def test_shared_router_route_resolution_fails_closed_on_ambiguity(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(shared_router.Path, "home", lambda: home)
    thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    for name in ("alpha", "beta"):
        repo = tmp_path / name
        repo.mkdir()
        assert task_store.initialize_repository(repo)["ok"]
        repo_id = task_store.storage_readiness(repo).repo_id
        _write_record(
            shared_router.registry_dir(home) / f"{repo_id}.json",
            {
                "schema_id": shared_router.SCHEMA_ID,
                "repo_id": repo_id,
                "repo_name": name,
                "repo_root": str(repo),
                "window_id": f"window_{name}",
                "extension_host_pid": os.getpid(),
                "selected_provider": "codex",
                "targets": {"codex": {"route": {"repo_id": repo_id, "thread_id": thread_id}}},
                "updated_at": "2026-07-30T00:00:00Z",
                "lease_expires_at": "2026-07-30T00:15:00Z",
            },
        )

    result = shared_router.resolve_repository_route(provider="codex", thread_id=thread_id)

    assert result == {"ok": False, "error": "route_ambiguous", "matches": 2}
