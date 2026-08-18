"""Retention must tell the truth about what it moved, freed and lost.

Each test here fails without the matching fix in this wave:

* NF-2026-00296 -- an interrupted quarantine (files moved, per-item manifest
  commit not yet written) is recoverable from its manifest, and two stage-and-move
  passes never collide on one staging directory.
* NF-2026-00286 -- the two dead paths (``orphan_candidates`` and
  ``legacy_candidate``) are removed, not wired: an aged legacy ``logs/`` store and
  an orphan (no ledger row) both stay fail-closed protected and are never
  quarantine candidates, so the removed paths had no reachable case. (These are
  dead-code removals with no behavioural delta; the tests guard that the cases the
  paths pretended to cover stay protected.)
* NF-2026-00287 -- ``purge`` reports the bytes actually reclaimed from disk for a
  batch whose files exist on disk but are unclaimed in the record.
* NF-2026-00162 -- ``storage_retention.restore`` names an absent retained
  workspace instead of surfacing it as a registration/identity (hash) drift.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import storage_retention, task_store, terminal_log_retention


# A far-future reference clock: every just-written file (real mtime) is already
# older than ``logs_days`` without mutating filesystem timestamps (os.utime is
# denied in the validation sandbox).
_FROZEN_NOW = datetime(2035, 1, 1, tzinfo=timezone.utc)
_FUTURE_DEADLINE = "2040-01-01T00:00:00+00:00"
_EXPIRED_DEADLINE = "2030-01-01T00:00:00+00:00"


@pytest.fixture()
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_log_retention, "now_utc", lambda: _FROZEN_NOW)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    readiness = task_store.storage_readiness(repo)
    now = "2026-01-01T00:00:00+00:00"
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,"
            "created_at,updated_at,completed_at,origin_thread_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("TASK_DONE", "runner", "topic", "finished", "done", "{}", now, now, now, "thread"),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


def _owned_names(request_id: str) -> list[str]:
    return [f"{request_id}{suffix}" for suffix in terminal_log_retention._OWNED_SUFFIXES]


# --------------------------------------------------------------------------
# NF-2026-00296 -- interrupted quarantine recoverable from its manifest
# --------------------------------------------------------------------------


def test_interrupted_quarantine_is_recoverable_from_manifest(
    tmp_path: Path, frozen_clock: None
) -> None:
    # Reproduce the exact interruption: ``quarantine`` moves an item's files into
    # the batch, then commits ``state="quarantined"`` in a following manifest
    # write. A crash between the move and that commit leaves the files physically
    # in the batch while the item still reads ``planned``. restore must recover
    # them from the manifest rather than stranding them forever.
    repo = _repo(tmp_path)
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    process_root.mkdir(parents=True, exist_ok=True)
    request_id = "1" * 32
    batch_id = "l20350101T000000-0000000000a1"
    batch = repo / terminal_log_retention.QUARANTINE_RELATIVE_PATH / batch_id
    (batch / request_id).mkdir(parents=True)
    files = []
    for name in _owned_names(request_id):
        (batch / request_id / name).write_text(name, encoding="utf-8")
        files.append({"name": name, "size_bytes": len(name), "mtime_ns": 1})
    manifest = {
        "schema_id": terminal_log_retention.SCHEMA_ID,
        "repo_id": task_store.storage_readiness(repo).repo_id,
        "batch_id": batch_id,
        "created_at": "2035-01-01T00:00:00+00:00",
        "restore_deadline": _FUTURE_DEADLINE,
        "preview_digest": "0" * 64,
        # The item is still "planned": its files moved, but the commit that would
        # flip it to "quarantined" never landed.
        "status": "quarantining",
        "items": [{"request_id": request_id, "state": "planned", "files": files}],
        "quarantined_files": 0,
        "quarantined_bytes": 0,
    }
    (batch / terminal_log_retention.MANIFEST_NAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    result = terminal_log_retention.restore(repo, batch_id=batch_id, confirm=True)

    assert result["restored"] == len(files)
    for name in _owned_names(request_id):
        assert (process_root / name).is_file()
        assert not (batch / request_id / name).exists()
    reloaded = json.loads((batch / terminal_log_retention.MANIFEST_NAME).read_text())
    assert reloaded["items"][0]["state"] == "restored"


def test_two_stage_and_move_passes_get_unique_staging_directories(
    tmp_path: Path, frozen_clock: None
) -> None:
    # Two enforce passes in separate processes can observe the same stray within
    # the same second. With a digest-derived id both computed the SAME batch id
    # and the second collided on ``batch.mkdir``. A per-batch unique id gives each
    # pass its own directory.
    repo = _repo(tmp_path)

    def _stage_one() -> dict[str, object]:
        stray = tmp_path / "stray"
        stray.mkdir()
        (stray / "payload.bin").write_bytes(b"x" * 1024)
        return terminal_log_retention._stage_reconcile_batch(
            repo,
            [{"name": "stray", "path": str(stray), "bytes": 1024, "reason": "test"}],
            action="reconcile_unclaimed",
        )

    first = _stage_one()
    second = _stage_one()

    assert first["count"] == 1 and second["count"] == 1
    assert first["batch_id"] and second["batch_id"]
    assert first["batch_id"] != second["batch_id"]
    assert terminal_log_retention.list_batches(repo)["count"] == 2


# --------------------------------------------------------------------------
# NF-2026-00286 -- the two dead paths: legacy wired, orphan removed
# --------------------------------------------------------------------------


def _write_legacy_logs(repo: Path) -> int:
    legacy = repo / "logs" / "processes"
    legacy.mkdir(parents=True)
    payload = b"l" * 4096
    (legacy / "old.stdout.log").write_bytes(payload)
    return len(payload)


def test_aged_legacy_logs_stay_protected_never_a_candidate(
    tmp_path: Path, frozen_clock: None
) -> None:
    # ``legacy_candidate`` is removed, not wired: an aged legacy ``logs/`` store is
    # always protected (fail closed) and never a quarantine candidate, so the dead
    # ``if legacy_candidate is not None`` digest/accumulator consumers had no
    # reachable case. Wiring it would have widened the eventual-deletion path and
    # contradicted the committed ``test_aged_legacy_store_is_protected_without_
    # usage_receipts``.
    repo = _repo(tmp_path)
    _write_legacy_logs(repo)

    preview = terminal_log_retention.preview(repo)

    assert preview["legacy_candidate"] is None
    assert preview["candidate_count"] == 0
    assert preview["legacy_status"] == "present_unmanaged"
    assert any(item["request_id"] == "legacy_logs" for item in preview["protected"])
    # A preview never moves the store.
    assert (repo / "logs" / "processes" / "old.stdout.log").is_file()


def test_orphan_files_stay_protected_never_a_candidate(
    tmp_path: Path, frozen_clock: None
) -> None:
    # The removed ``orphan_candidates`` path had no reachable case: an owned file
    # set with no ledger row cannot be proved to belong to a finished/archived
    # task, so it always fails closed to ``protected`` and is never swept.
    repo = _repo(tmp_path)
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    process_root.mkdir(parents=True, exist_ok=True)
    orphan_id = "a" * 32
    for name in _owned_names(orphan_id):
        (process_root / name).write_text(name, encoding="utf-8")

    preview = terminal_log_retention.preview(repo)

    assert all(item["request_id"] != orphan_id for item in preview["candidates"])
    assert any(item["request_id"] == orphan_id for item in preview["protected"])


# --------------------------------------------------------------------------
# NF-2026-00287 -- purge reports bytes actually reclaimed from disk
# --------------------------------------------------------------------------


def test_purge_reports_bytes_reclaimed_for_unclaimed_batch(
    tmp_path: Path, frozen_clock: None
) -> None:
    # The stranded shape: the record says empty (every item skipped,
    # quarantined_bytes 0) while the directory physically holds real bytes. purge
    # must report what it truly removes from disk, not the record's zero.
    repo = _repo(tmp_path)
    request_id = "2" * 32
    batch_id = "l20350101T000000-0000000000b2"
    batch = repo / terminal_log_retention.QUARANTINE_RELATIVE_PATH / batch_id
    (batch / request_id).mkdir(parents=True)
    payload = b"g" * 9000
    (batch / request_id / f"{request_id}.stdout.log").write_bytes(payload)
    manifest = {
        "schema_id": terminal_log_retention.SCHEMA_ID,
        "repo_id": task_store.storage_readiness(repo).repo_id,
        "batch_id": batch_id,
        "created_at": "2035-01-01T00:00:00+00:00",
        # Deadline already expired, so purge is permitted; the record reads empty
        # (skipped item, zero bytes) yet the directory holds the payload above.
        "restore_deadline": _EXPIRED_DEADLINE,
        "preview_digest": "0" * 64,
        "status": "empty",
        "items": [
            {
                "request_id": request_id,
                "state": "skipped_identity_changed",
                "files": [{"name": f"{request_id}.stdout.log", "size_bytes": 1, "mtime_ns": 1}],
            }
        ],
        "quarantined_files": 0,
        "quarantined_bytes": 0,
    }
    (batch / terminal_log_retention.MANIFEST_NAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    result = terminal_log_retention.purge(repo, batch_id=batch_id, confirm=True)

    assert result["purged"] is True
    assert result["bytes"] == len(payload)
    assert not batch.exists()


# --------------------------------------------------------------------------
# NF-2026-00162 -- an absent retained workspace is named, not hash drift
# --------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def retained(tmp_path: Path) -> dict[str, Path]:
    if shutil.which("git") is None:
        pytest.skip("git executable not available in sandbox")
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    base = tmp_path / "worktrees"
    base.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_path, "clone", str(remote), str(repo))
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "origin", "HEAD:refs/heads/main")
    _git(repo, "fetch", "origin")
    assert task_store.initialize_repository(repo)["ok"]
    entry = base / "request-safe"
    worktree = entry / "worktree"
    entry.mkdir()
    _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    return {"repo": repo, "base": base, "entry": entry}


def test_restore_names_absent_retained_workspace_not_hash_drift(retained) -> None:
    aged_now = time.time() + 31 * 86400
    preview = storage_retention.preview(retained["repo"], base=retained["base"], now=aged_now)
    moved = storage_retention.quarantine(
        retained["repo"],
        preview_digest=preview["preview_digest"],
        confirm=True,
        base=retained["base"],
        now=aged_now,
    )
    assert moved["quarantined"] == 1

    repo_id = task_store.storage_readiness(retained["repo"]).repo_id
    batch = (
        retained["base"]
        / storage_retention.QUARANTINE_DIRNAME
        / repo_id
        / moved["batch_id"]
    )
    # The retained workspace itself was swept while quarantined: only its inner
    # checkout is gone, so restore moves an entry back that has no workspace.
    shutil.rmtree(batch / "request-safe" / "worktree")

    with pytest.raises(
        storage_retention.StorageRetentionError,
        match="retention_restore_workspace_absent:request-safe",
    ):
        storage_retention.restore(
            retained["repo"],
            batch_id=moved["batch_id"],
            confirm=True,
            base=retained["base"],
        )
