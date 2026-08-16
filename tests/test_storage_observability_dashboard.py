from __future__ import annotations

import time
from pathlib import Path

from aiworkhub import storage_observability


def _wait_ready(repo: Path, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    result = storage_observability.snapshot(repo)
    while result["scan_status"] in {"scanning", "refreshing"} and time.monotonic() < deadline:
        time.sleep(0.01)
        result = storage_observability.snapshot(repo)
    return result


def test_storage_snapshot_counts_repo_data_and_never_follows_symlinks(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    data = repo / ".aiworkhub"
    data.mkdir(parents=True)
    (data / "state.db").write_bytes(b"a" * 128)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"b" * 4096)
    (data / "outside-link").symlink_to(outside)
    monkeypatch.setattr(
        storage_observability.worktree_storage,
        "scan_worktrees",
        # scan_worktrees is reached through storage_retention.repo_storage_footprint,
        # which passes `base` positionally; a keyword-only stub raises TypeError
        # before the stub body runs and the test then asserts on the wrong error.
        lambda *_args, **_kwargs: {
            "summary": {"total_bytes": 256, "count": 2, "removable_safe_bytes": 64}
        },
    )
    monkeypatch.setattr(
        storage_observability.worktree_storage,
        "scan_worktree_registrations",
        lambda _root: {
            "ok": True,
            "registered_count": 3,
            "aiworkhub_registered_count": 2,
            "stale_candidate_count": 1,
            "candidate_overflow_count": 0,
            "foreign_stale_count": 0,
            "safe_to_prune": True,
            "preview_digest": "c" * 64,
        },
    )
    storage_observability._reset_cache_for_tests()

    result = _wait_ready(repo)

    assert result["scan_status"] == "ready"
    assert result["repo_data_bytes"] == 128
    assert result["repo_data_files"] == 1
    assert result["worker_tree_bytes"] == 256
    assert result["worker_tree_count"] == 2
    assert result["safe_reclaimable_bytes"] == 64
    assert result["managed_total_bytes"] == 384
    assert result["components"] == [{"id": "state.db", "bytes": 128, "files": 1}]
    assert result["retention_preview"]["dry_run"] is True
    assert result["retention_preview"]["repository_scoped"] is True
    assert result["retention_preview"]["eligible_bytes"] == 64
    assert result["retention_preview"]["registrations"]["registered_count"] == 3
    assert result["retention_preview"]["registrations"]["stale_candidate_count"] == 1
    assert result["disk_total_bytes"] > 0
    assert result["disk_free_bytes"] > 0
    assert result["readonly"] is True
    assert "repo" not in result and "base" not in result


def test_storage_snapshot_flags_material_unattributed_storage(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    gib = 1024 ** 3
    material_footprint = {
        "base": tmp_path,
        "scan": {
            "summary": {"total_bytes": 5 * gib, "count": 13},
            "worktrees": [],
            "base": str(tmp_path),
        },
        "observed_total_bytes": 21 * gib,
        "repository_worktree_bytes": 5 * gib,
        "global_worktree_bytes": 21 * gib,
        "unattributed_or_foreign_worktree_bytes": 16 * gib,
        "repository_worktree_count": 13,
        "global_worktree_count": 143,
        "unattributed_or_foreign_worktree_count": 130,
        "canonical_runtime_bytes": 0,
        "legacy_log_bytes": 0,
        "legacy_log_status": "absent_or_empty",
    }
    monkeypatch.setattr(
        storage_observability.storage_retention,
        "repo_storage_footprint",
        lambda *_args, **_kwargs: material_footprint,
    )
    monkeypatch.setattr(
        storage_observability.worktree_storage,
        "scan_worktree_registrations",
        lambda _root: {
            "ok": True,
            "registered_count": 143,
            "aiworkhub_registered_count": 13,
            "stale_candidate_count": 0,
            "candidate_overflow_count": 0,
            "foreign_stale_count": 0,
            "safe_to_prune": False,
            "preview_digest": "f" * 64,
        },
    )
    storage_observability._reset_cache_for_tests()

    result = _wait_ready(repo)

    alert = result["retention_preview"]["unattributed_alert"]
    assert alert["material"] is True
    assert alert["count"] == 130
    assert alert["bytes"] == 16 * gib
    # A short candidate list must not read as clean beside the stranded footprint.
    assert result["retention_preview"]["eligible_count"] == 0
    assert "recover_stranded_worktrees" in alert["message"]


def test_storage_snapshot_returns_immediately_while_slow_scan_runs(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def slow_scan(*_args, **_kwargs):
        time.sleep(0.15)
        return {"summary": {"total_bytes": 1, "count": 1, "removable_safe_bytes": 0}}

    monkeypatch.setattr(storage_observability.worktree_storage, "scan_worktrees", slow_scan)
    storage_observability._reset_cache_for_tests()
    started = time.monotonic()

    first = storage_observability.snapshot(repo)

    assert time.monotonic() - started < 0.1
    assert first["scan_status"] == "scanning"
    assert _wait_ready(repo)["scan_status"] == "ready"


def test_storage_scan_failure_is_bounded_and_does_not_break_capacity(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        storage_observability.worktree_storage,
        "scan_worktrees",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("secret details")),
    )
    storage_observability._reset_cache_for_tests()

    result = _wait_ready(repo)

    assert result["scan_status"] == "error"
    assert result["errors"] == ["storage_scan_failed:RuntimeError"]
    assert result["disk_total_bytes"] > 0
