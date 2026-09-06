from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import process_launcher, storage_retention, task_store


def test_terminal_hints_coalesce_without_running_scan_inline(monkeypatch, tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def cleanup(repo_root: Path, *, base=None, now=None):
        entered.set()
        release.wait(2)
        return {"ok": True, "scanned": 0}

    monkeypatch.setattr(storage_retention, "run_repository_cleanup", cleanup)
    assert storage_retention.schedule_repository_cleanup(tmp_path) is True
    assert entered.wait(1)
    assert storage_retention.schedule_repository_cleanup(tmp_path) is False
    release.set()


def test_cleanup_single_flight_uses_platform_lock_not_sqlite(
    monkeypatch, tmp_path: Path
) -> None:
    entered = threading.Event()
    lock_calls: list[tuple[int, bool]] = []
    unlock_calls: list[int] = []

    monkeypatch.setattr(
        storage_retention.sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("cleanup lock must not open SQLite"),
    )
    monkeypatch.setattr(
        storage_retention,
        "lock_fd",
        lambda fd, *, blocking: lock_calls.append((fd, blocking)),
    )
    monkeypatch.setattr(
        storage_retention,
        "unlock_fd",
        lambda fd: unlock_calls.append(fd),
    )
    monkeypatch.setattr(
        storage_retention,
        "run_repository_cleanup",
        lambda repo_root, *, base=None: entered.set() or {"ok": True},
    )

    assert storage_retention.schedule_repository_cleanup(tmp_path) is True
    assert entered.wait(1)
    for _ in range(100):
        if not storage_retention.repository_cleanup_status(tmp_path)["running"]:
            break
        threading.Event().wait(0.01)

    assert lock_calls and lock_calls[0][1] is False
    assert unlock_calls == [lock_calls[0][0]]


def test_manifest_authentication_detects_deadline_tampering(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(storage_retention, "_repo_id", lambda _root: "repo-test")
    manifest = {
        "schema_id": storage_retention.SCHEMA_ID,
        "repo_id": "repo-test",
        "batch_id": "q20260905T203912-49829298592d",
        "created_at": "2026-09-05T20:39:12+00:00",
        "restore_deadline": "2026-09-12T20:39:12+00:00",
        "preview_digest": "49829298592d",
    }
    manifest["deadline_authentication"] = storage_retention._manifest_authentication(
        tmp_path, manifest
    )
    assert storage_retention._authenticated_manifest(tmp_path, manifest)
    manifest["restore_deadline"] = "2026-09-04T20:39:12+00:00"
    assert not storage_retention._authenticated_manifest(tmp_path, manifest)


def test_public_purge_rejects_tampered_authenticated_deadline(
    monkeypatch, tmp_path: Path
) -> None:
    batch_id = "q20260905T203912-49829298592d"
    batch = tmp_path / batch_id
    batch.mkdir()
    evidence = batch / "evidence"
    evidence.write_text("restorable", encoding="utf-8")
    monkeypatch.setattr(storage_retention, "_repo_id", lambda _root: "repo-test")
    manifest = {
        "schema_id": storage_retention.SCHEMA_ID,
        "repo_id": "repo-test",
        "batch_id": batch_id,
        "created_at": "2026-09-05T20:39:12+00:00",
        "restore_deadline": "2099-09-12T20:39:12+00:00",
        "preview_digest": "49829298592d",
        "items": [{"quarantine_path": "evidence"}],
    }
    manifest["deadline_authentication"] = storage_retention._manifest_authentication(
        tmp_path, manifest
    )
    manifest["restore_deadline"] = "2020-09-04T20:39:12+00:00"
    monkeypatch.setattr(storage_retention, "_verified_batch", lambda *args: batch)
    monkeypatch.setattr(storage_retention, "_load_manifest", lambda *args: manifest)

    with pytest.raises(
        storage_retention.StorageRetentionError,
        match="retention_deadline_authentication_invalid",
    ):
        storage_retention.purge(tmp_path, batch_id=batch_id, confirm=True, base=tmp_path)

    assert evidence.read_text(encoding="utf-8") == "restorable"


def test_cleanup_telemetry_is_bounded_when_no_candidates(monkeypatch, tmp_path: Path) -> None:
    protected = [{"reason": f"reason-{index}"} for index in range(50)]
    monkeypatch.setattr(
        storage_retention,
        "preview",
        lambda *args, **kwargs: {
            "complete": True,
            "candidates": [],
            "protected": protected,
            "preview_digest": "digest",
        },
    )
    monkeypatch.setattr(storage_retention, "_iter_batch_rows", lambda *args: iter(()))
    monkeypatch.setattr(storage_retention, "_repo_id", lambda root: "repo-test")
    result = storage_retention.run_repository_cleanup(tmp_path)
    assert result["scanned"] == 50
    assert result["protected"] == 50
    assert len(result["protected_reasons"]) == 20


def test_cleanup_quarantines_candidates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        storage_retention,
        "preview",
        lambda *args, **kwargs: {
            "complete": True,
            "candidates": [{"path": "one"}],
            "protected": [],
            "preview_digest": "digest",
        },
    )
    monkeypatch.setattr(
        storage_retention,
        "quarantine",
        lambda *args, **kwargs: {"quarantined": 1, "bytes": 123},
    )
    monkeypatch.setattr(storage_retention, "_iter_batch_rows", lambda *args: iter(()))
    monkeypatch.setattr(storage_retention, "_repo_id", lambda root: "repo-test")
    result = storage_retention._run_repository_cleanup(tmp_path, now=1.0)
    assert (result["quarantined"], result["bytes_moved"]) == (1, 123)


def test_auto_purge_requires_authentication_and_uses_injected_now(
    monkeypatch, tmp_path: Path
) -> None:
    deadline = "2026-09-05T20:39:12+00:00"
    now = datetime(2026, 9, 6, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(
        storage_retention,
        "preview",
        lambda *args, **kwargs: {
            "complete": True,
            "candidates": [],
            "protected": [],
            "preview_digest": "digest",
        },
    )
    monkeypatch.setattr(
        storage_retention,
        "_iter_batch_rows",
        lambda *args: iter(
            [{"batch_id": "q20260905T203912-49829298592d", "restore_deadline": deadline}]
        ),
    )
    monkeypatch.setattr(storage_retention, "configured_worktree_root", lambda root: tmp_path)
    monkeypatch.setattr(storage_retention, "_verified_batch", lambda *args: tmp_path)
    monkeypatch.setattr(storage_retention, "_repo_id", lambda root: "repo-test")
    manifest = {"restore_deadline": deadline}
    monkeypatch.setattr(storage_retention, "_load_manifest", lambda *args: manifest)
    purged: list[datetime] = []
    monkeypatch.setattr(
        storage_retention,
        "_purge_batch",
        lambda *args, **kwargs: purged.append(kwargs["current"]) or {"bytes": 7},
    )

    monkeypatch.setattr(storage_retention, "_authenticated_manifest", lambda *args: False)
    assert storage_retention._run_repository_cleanup(tmp_path, now=now)[
        "expired_batches_purged"
    ] == 0
    assert purged == []

    monkeypatch.setattr(storage_retention, "_authenticated_manifest", lambda *args: True)
    result = storage_retention._run_repository_cleanup(tmp_path, now=now)
    assert result["expired_batches_purged"] == 1
    assert result["bytes_freed"] == 7
    assert purged == [datetime.fromtimestamp(now, timezone.utc)]


def test_public_purge_rejects_caller_controlled_clock(tmp_path: Path) -> None:
    batch = tmp_path / "q20260905T203912-49829298592d"
    batch.mkdir()
    evidence = batch / "evidence"
    evidence.write_text("restorable", encoding="utf-8")
    with pytest.raises(TypeError, match="unexpected keyword argument 'now'"):
        storage_retention.purge(
            tmp_path,
            batch_id="q20260905T203912-49829298592d",
            confirm=True,
            now=datetime(2999, 1, 1, tzinfo=timezone.utc).timestamp(),
        )
    assert evidence.read_text(encoding="utf-8") == "restorable"


def test_public_cleanup_rejects_caller_controlled_clock(tmp_path: Path) -> None:
    batch = tmp_path / "q20260905T203912-49829298592d"
    batch.mkdir()
    evidence = batch / "evidence"
    evidence.write_text("restorable", encoding="utf-8")
    with pytest.raises(TypeError, match="unexpected keyword argument 'now'"):
        storage_retention.run_repository_cleanup(
            tmp_path,
            now=datetime(2999, 1, 1, tzinfo=timezone.utc).timestamp(),
        )
    assert evidence.read_text(encoding="utf-8") == "restorable"


def test_cleanup_streams_batches_beyond_public_preview_cap(
    monkeypatch, tmp_path: Path
) -> None:
    expired_id = "q20200101T000000-aaaaaaaaaaaa"
    future_deadline = "2099-01-01T00:00:00+00:00"
    expired_deadline = "2020-01-02T00:00:00+00:00"
    batch_ids = [expired_id] + [
        f"q20981231T23{index // 60:02d}{index % 60:02d}-{index:012x}"
        for index in range(101)
    ]
    for batch_id in batch_ids:
        (tmp_path / batch_id).mkdir()

    monkeypatch.setattr(
        storage_retention,
        "preview",
        lambda *args, **kwargs: {
            "complete": True,
            "candidates": [],
            "protected": [],
            "preview_digest": "digest",
        },
    )
    monkeypatch.setattr(storage_retention, "_read_quarantine_root", lambda *args: tmp_path)
    monkeypatch.setattr(storage_retention, "_repo_id", lambda root: "repo-test")

    def load_manifest(path: Path, repo_id: str):
        batch_id = path.parent.name
        return {
            "batch_id": batch_id,
            "created_at": "2020-01-01T00:00:00+00:00",
            "restore_deadline": (
                expired_deadline if batch_id == expired_id else future_deadline
            ),
            "status": "quarantined",
            "items": [],
        }

    monkeypatch.setattr(storage_retention, "_load_manifest", load_manifest)
    monkeypatch.setattr(storage_retention, "_authenticated_manifest", lambda *args: True)
    monkeypatch.setattr(
        storage_retention,
        "_verified_batch",
        lambda root, base, batch_id: tmp_path / batch_id,
    )
    purged: list[str] = []
    monkeypatch.setattr(
        storage_retention,
        "_purge_batch",
        lambda *args, **kwargs: purged.append(kwargs["batch_id"]) or {"bytes": 9},
    )

    public = storage_retention.list_batches(tmp_path, base=tmp_path)
    assert public["count"] == 100
    assert expired_id not in {row["batch_id"] for row in public["batches"]}

    now = datetime(2026, 9, 6, tzinfo=timezone.utc).timestamp()
    result = storage_retention._run_repository_cleanup(
        tmp_path, base=tmp_path, now=now
    )
    assert purged == [expired_id]
    assert result["expired_batches_purged"] == 1
    assert result["bytes_freed"] == 9
    assert result["next_deadline"] == future_deadline


def test_cleanup_reschedules_next_deadline_and_exposes_status(
    monkeypatch, tmp_path: Path
) -> None:
    deadline = "2099-01-01T00:00:00+00:00"
    scheduled: list[str] = []
    monkeypatch.setattr(
        storage_retention,
        "run_repository_cleanup",
        lambda *args, **kwargs: {"ok": True, "next_deadline": deadline},
    )
    monkeypatch.setattr(
        storage_retention,
        "_schedule_deadline_wakeup",
        lambda root, *, base, deadline: scheduled.append(deadline),
    )
    assert storage_retention.schedule_repository_cleanup(tmp_path)
    for _ in range(100):
        if scheduled:
            break
        threading.Event().wait(0.01)
    assert scheduled == [deadline]
    status = storage_retention.repository_cleanup_status(tmp_path)
    assert status["running"] is False
    assert status["next_deadline"] == deadline
    assert status["last_run"]["ok"] is True


def test_process_wide_manager_startup_schedules_one_recovery_sweep(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    scheduled: list[Path] = []
    monkeypatch.setattr(
        storage_retention,
        "schedule_repository_cleanup",
        lambda repo: scheduled.append(repo) or True,
    )
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_reconcile_pending_needfix_closures",
        lambda self: None,
    )
    monkeypatch.setattr(process_launcher, "_DEFAULT_MANAGER", None)

    process_launcher.default_manager()
    process_launcher.default_manager()

    assert scheduled == [repo.resolve()]


def test_manual_purge_remains_compatible_with_legacy_manifest(
    monkeypatch, tmp_path: Path
) -> None:
    batch = tmp_path / "q20200101T000000-aaaaaaaaaaaa"
    batch.mkdir()
    manifest = {
        "restore_deadline": "2020-01-02T00:00:00+00:00",
        "quarantined_bytes": 11,
    }
    monkeypatch.setattr(storage_retention, "configured_worktree_root", lambda root: tmp_path)
    monkeypatch.setattr(storage_retention, "_verified_batch", lambda *args: batch)
    monkeypatch.setattr(storage_retention, "_repo_id", lambda root: "repo-test")
    monkeypatch.setattr(storage_retention, "_load_manifest", lambda *args: manifest)
    monkeypatch.setattr(storage_retention, "_append_audit", lambda *args: None)
    monkeypatch.setattr(storage_retention.worktree_storage, "_git", lambda *args: None)
    result = storage_retention.purge(
        tmp_path, batch_id=batch.name, confirm=True
    )
    assert result["bytes"] == 11
    assert not batch.exists()
