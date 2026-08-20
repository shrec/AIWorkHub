from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from aiworkhub import storage_observability, task_store, terminal_log_retention


def _write_terminal_batch(
    repo: Path,
    batch_id: str,
    *,
    item_states: tuple[str, ...],
    deadline: str,
    payload: tuple[tuple[str, str], ...] = (),
) -> None:
    """Materialise one terminal-log quarantine batch for the observability path.

    ``payload`` writes real files regardless of what the manifest records, so a
    test can build a batch that is past its deadline yet still physically holds
    files -- ``purge_eligible`` but never reaped by ``purge_empty_batches``.
    """
    repo_id = task_store.storage_readiness(repo).repo_id
    batch = repo / terminal_log_retention.QUARANTINE_RELATIVE_PATH / batch_id
    batch.mkdir(parents=True)
    items = []
    for index, state in enumerate(item_states):
        request_id = f"{index:032x}"
        (batch / request_id).mkdir()
        items.append({
            "request_id": request_id,
            "state": state,
            "files": [{"name": f"{request_id}.stdout.log", "size_bytes": 0, "mtime_ns": 1}],
        })
    for relpath, content in payload:
        target = batch / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    manifest = {
        "schema_id": terminal_log_retention.SCHEMA_ID,
        "repo_id": repo_id,
        "batch_id": batch_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "restore_deadline": deadline,
        "preview_digest": "0" * 64,
        "status": "empty",
        "items": items,
        "quarantined_files": 0,
        "quarantined_bytes": 0,
    }
    (batch / terminal_log_retention.MANIFEST_NAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )


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
    # The configured worker root is nested below .aiworkhub/runtime. It is
    # measured by the worktree inventory and must not be walked or counted a
    # second time as repository data.
    nested_worker = data / "runtime" / "worktrees" / "foreign" / "worktree"
    nested_worker.mkdir(parents=True)
    (nested_worker / "payload.bin").write_bytes(b"w" * 512)
    monkeypatch.setattr(
        storage_observability.storage_retention,
        "configured_worktree_root",
        lambda _repo: data / "runtime" / "worktrees",
    )
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
    assert result["components"] == [
        {"id": "runtime", "bytes": 0, "files": 0},
        {"id": "state.db", "bytes": 128, "files": 1},
    ]
    assert result["retention_preview"]["dry_run"] is True
    assert result["retention_preview"]["repository_scoped"] is True
    assert result["retention_preview"]["eligible_bytes"] == 64
    assert result["retention_preview"]["registrations"]["registered_count"] == 3
    assert result["retention_preview"]["registrations"]["stale_candidate_count"] == 1
    assert "protected" not in result["terminal_log_retention"]
    assert "candidates" not in result["terminal_log_retention"]
    assert result["disk_total_bytes"] > 0
    assert result["disk_free_bytes"] > 0
    assert result["readonly"] is True
    assert "repo" not in result and "base" not in result


def test_storage_snapshot_reports_a_bound_for_every_accumulating_store(tmp_path, monkeypatch):
    # Each store must answer three questions in the report: what bounds it, what
    # happens at the bound, and what an operator does when the automatic path
    # cannot act. A store that cannot answer all three is unbounded.
    repo = tmp_path / "repo"
    (repo / ".aiworkhub").mkdir(parents=True)
    monkeypatch.setattr(
        storage_observability.worktree_storage,
        "scan_worktrees",
        lambda *_args, **_kwargs: {
            "summary": {"total_bytes": 256, "count": 2, "removable_safe_bytes": 64}
        },
    )
    monkeypatch.setattr(
        storage_observability.worktree_storage,
        "scan_worktree_registrations",
        lambda _root: {
            "ok": True,
            "registered_count": 0,
            "aiworkhub_registered_count": 0,
            "stale_candidate_count": 0,
            "candidate_overflow_count": 0,
            "foreign_stale_count": 0,
            "safe_to_prune": True,
            "preview_digest": "d" * 64,
        },
    )
    storage_observability._reset_cache_for_tests()

    bounds = _wait_ready(repo)["storage_bounds"]

    for store in (
        "worktrees",
        "terminal_log_quarantine",
        "process_logs",
        "attempt_artifacts",
        "runtime_generations",
    ):
        assert bounds[store]["bounds"], store
        assert bounds[store]["at_bound"], store
        assert bounds[store]["operator_action"], store
    # The worktree footprint once NF-2026-00286's pins release is stated.
    assert "projected_bytes_after_pins_release" in bounds["worktrees"]
    assert bounds["worktrees"]["pinned_predecessor_bytes"] == 0


def test_empty_purge_eligible_count_reflects_only_the_collector_reap(tmp_path, monkeypatch):
    # The surfaced empty-purge count must equal what ``purge_empty_batches`` will
    # actually collect, never the wider ``purge_eligible`` population. A batch past
    # its deadline that still holds files is ``purge_eligible`` yet that collector
    # never takes it, so summing ``purge_eligible`` would over-promise (2 here);
    # the count reads ``reapable_empty`` and reports 1.
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    # Genuinely empty: record-empty AND disk-empty -> the collector reaps it.
    _write_terminal_batch(
        repo, "l20260816T101142-000000000001",
        item_states=("skipped_identity_changed",) * 2,
        deadline="2040-01-01T00:00:00+00:00",
    )
    # Expired but still full: past deadline, record claims a quarantined item whose
    # file is physically present -> purge_eligible, but never reapable_empty.
    _write_terminal_batch(
        repo, "l20260816T101142-000000000002",
        item_states=("quarantined",),
        deadline="2020-01-01T00:00:00+00:00",
        payload=((f"{0:032x}/{0:032x}.stdout.log", "held"),),
    )
    monkeypatch.setattr(
        storage_observability.worktree_storage,
        "scan_worktrees",
        lambda *_args, **_kwargs: {
            "summary": {"total_bytes": 0, "count": 0, "removable_safe_bytes": 0}
        },
    )
    monkeypatch.setattr(
        storage_observability.worktree_storage,
        "scan_worktree_registrations",
        lambda _root: {
            "ok": True,
            "registered_count": 0,
            "aiworkhub_registered_count": 0,
            "stale_candidate_count": 0,
            "candidate_overflow_count": 0,
            "foreign_stale_count": 0,
            "safe_to_prune": True,
            "preview_digest": "e" * 64,
        },
    )
    storage_observability._reset_cache_for_tests()

    result = _wait_ready(repo)
    terminal_bounds = result["storage_bounds"]["terminal_log_quarantine"]

    # Both batches are purge_eligible, so the pre-fix count would have read 2.
    eligible = [
        row for row in result["terminal_log_quarantine_batches"]
        if row.get("purge_eligible")
    ]
    assert len(eligible) == 2
    # But only the genuinely-empty one is what the named collector drains.
    assert terminal_bounds["empty_purge_eligible_count"] == 1


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


def test_storage_snapshot_survives_runtime_cache_reset(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    measured = {
        "scan_status": "ready",
        "scanned_at": "2026-08-20T00:00:00+00:00",
        "managed_total_bytes": 123456,
        "repo_data_bytes": 123,
        "repo_data_files": 4,
        "worker_tree_bytes": 120000,
        "worker_tree_count": 3,
        "quarantine_bytes": 0,
        "safe_reclaimable_bytes": 1000,
        "components": [],
        "retention_preview": {},
        "quarantine_batches": [],
        "terminal_log_retention": {
            "protected": [{"request_id": "never-transport-this-row"}],
            "protected_count": 1,
        },
        "terminal_log_quarantine_batches": [],
        "task_retention": {},
        "task_retention_batches": [],
        "errors": [],
        # A persisted payload is never allowed to override live response authority.
        "readonly": False,
        "disk_total_bytes": 0,
    }
    storage_observability._persist(repo.resolve(), measured)
    storage_observability._reset_cache_for_tests()
    monkeypatch.setattr(
        storage_observability,
        "_measure",
        lambda _root: (_ for _ in ()).throw(AssertionError("fresh cache rescanned")),
    )

    started = time.monotonic()
    result = storage_observability.snapshot(repo)

    assert time.monotonic() - started < 0.1
    assert result["scan_status"] == "ready"
    assert result["managed_total_bytes"] == 123456
    assert result["scanned_at"] == "2026-08-20T00:00:00+00:00"
    assert result["readonly"] is True
    assert result["disk_total_bytes"] > 0
    assert result["terminal_log_retention"] == {"protected_count": 1}


def test_stale_persisted_storage_snapshot_is_visible_during_refresh(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    payload = {
        "scan_status": "ready",
        "scanned_at": "2026-08-19T00:00:00+00:00",
        "managed_total_bytes": 654321,
        "errors": [],
    }
    cache_path = repo / storage_observability.PERSISTED_CACHE_RELATIVE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({
            "schema_id": storage_observability.PERSISTED_CACHE_SCHEMA,
            "repo_binding": storage_observability._repo_binding(repo.resolve()),
            "completed_at_epoch": time.time()
            - storage_observability.SCAN_TTL_SECONDS
            - 1,
            "payload": payload,
        }),
        encoding="utf-8",
    )
    release = threading.Event()

    def slow_refresh(_root: Path) -> dict:
        release.wait(1.0)
        return {**payload, "managed_total_bytes": 777777}

    monkeypatch.setattr(storage_observability, "_measure", slow_refresh)
    storage_observability._reset_cache_for_tests()
    try:
        started = time.monotonic()
        result = storage_observability.snapshot(repo)

        assert time.monotonic() - started < 0.1
        assert result["scan_status"] == "refreshing"
        assert result["managed_total_bytes"] == 654321
    finally:
        release.set()
        _wait_ready(repo)


def test_foreign_or_malformed_storage_cache_is_ignored(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    cache_path = repo / storage_observability.PERSISTED_CACHE_RELATIVE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({
            "schema_id": storage_observability.PERSISTED_CACHE_SCHEMA,
            "repo_binding": "0" * 64,
            "completed_at_epoch": time.time(),
            "payload": {
                "scan_status": "ready",
                "managed_total_bytes": 999999,
                "errors": [],
            },
        }),
        encoding="utf-8",
    )
    release = threading.Event()

    def measured_after_release(_root: Path) -> dict:
        release.wait(1.0)
        return {"scan_status": "ready", "managed_total_bytes": 1, "errors": []}

    monkeypatch.setattr(
        storage_observability,
        "_measure",
        measured_after_release,
    )
    storage_observability._reset_cache_for_tests()
    try:
        result = storage_observability.snapshot(repo)
        assert result["scan_status"] == "scanning"
        assert result["managed_total_bytes"] == 0
    finally:
        release.set()
        _wait_ready(repo)


def test_storage_invalidation_keeps_last_good_visible_while_refreshing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    payload = {
        "scan_status": "ready",
        "scanned_at": "2026-08-20T00:00:00+00:00",
        "managed_total_bytes": 222222,
        "errors": [],
    }
    storage_observability._persist(repo.resolve(), payload)
    storage_observability._reset_cache_for_tests()
    assert storage_observability.snapshot(repo)["scan_status"] == "ready"
    release = threading.Event()

    def refreshed_after_release(_root: Path) -> dict:
        release.wait(1.0)
        return {**payload, "managed_total_bytes": 333333}

    monkeypatch.setattr(storage_observability, "_measure", refreshed_after_release)
    storage_observability.invalidate(repo)
    try:
        result = storage_observability.snapshot(repo)
        assert result["scan_status"] == "refreshing"
        assert result["managed_total_bytes"] == 222222
    finally:
        release.set()
        assert _wait_ready(repo)["managed_total_bytes"] == 333333
