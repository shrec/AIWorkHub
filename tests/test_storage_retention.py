from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from aiworkhub import storage_retention, task_store


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def retained(tmp_path: Path) -> dict[str, Path]:
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
    old = time.time() - 31 * 86400
    os.utime(entry, (old, old))
    return {"repo": repo, "base": base, "entry": entry}


def test_preview_is_repo_scoped_deterministic_and_side_effect_free(retained) -> None:
    first = storage_retention.preview(retained["repo"], base=retained["base"])
    second = storage_retention.preview(retained["repo"], base=retained["base"])

    assert first == second
    assert first["dry_run"] is True
    assert first["repository_scoped"] is True
    assert first["candidate_count"] == 1
    assert first["candidates"][0]["id"] == "request-safe"
    assert retained["entry"].is_dir()
    assert "base" not in first
    assert first["registration_health"]["ok"] is True
    assert first["registration_health"]["stale_candidate_count"] == 0


def test_preview_accounts_for_global_worktrees_runtime_and_legacy_logs(retained) -> None:
    foreign = retained["base"] / "unattributed-orphan"
    foreign.mkdir()
    (foreign / "payload.bin").write_bytes(b"x" * 4096)
    legacy = retained["repo"] / "logs" / "processes"
    legacy.mkdir(parents=True)
    (legacy / "old.stdout.log").write_bytes(b"l" * 2048)
    runtime = retained["repo"] / ".aiworkhub" / "runtime" / "extra"
    runtime.mkdir(parents=True)
    (runtime / "state.bin").write_bytes(b"r" * 1024)

    result = storage_retention.preview(retained["repo"], base=retained["base"])
    footprint = result["footprint"]

    assert footprint["global_worktree_bytes"] >= footprint["repository_worktree_bytes"] + 4096
    assert footprint["unattributed_or_foreign_worktree_bytes"] >= 4096
    assert footprint["legacy_log_bytes"] >= 2048
    assert footprint["canonical_runtime_bytes"] >= 1024
    assert result["current_bytes"] == footprint["observed_total_bytes"]


def test_quarantine_and_restore_roundtrip(retained) -> None:
    preview = storage_retention.preview(retained["repo"], base=retained["base"])

    moved = storage_retention.quarantine(
        retained["repo"],
        preview_digest=preview["preview_digest"],
        confirm=True,
        base=retained["base"],
    )

    assert moved["quarantined"] == 1
    assert not retained["entry"].exists()
    batches = storage_retention.list_batches(retained["repo"], base=retained["base"])
    assert batches["count"] == 1
    assert batches["batches"][0]["quarantined_count"] == 1

    restored = storage_retention.restore(
        retained["repo"],
        batch_id=moved["batch_id"],
        confirm=True,
        base=retained["base"],
    )
    assert restored["restored"] == 1
    assert retained["entry"].is_dir()


def test_stale_preview_and_early_purge_fail_closed(retained) -> None:
    preview = storage_retention.preview(retained["repo"], base=retained["base"])
    os.utime(retained["entry"], None)
    with pytest.raises(storage_retention.StorageRetentionError, match="retention_preview_stale"):
        storage_retention.quarantine(
            retained["repo"],
            preview_digest=preview["preview_digest"],
            confirm=True,
            base=retained["base"],
        )

    old = time.time() - 31 * 86400
    os.utime(retained["entry"], (old, old))
    fresh = storage_retention.preview(retained["repo"], base=retained["base"])
    moved = storage_retention.quarantine(
        retained["repo"], preview_digest=fresh["preview_digest"], confirm=True, base=retained["base"]
    )
    with pytest.raises(storage_retention.StorageRetentionError, match="retention_undo_window_active"):
        storage_retention.purge(
            retained["repo"], batch_id=moved["batch_id"], confirm=True, base=retained["base"]
        )


def test_purge_requires_expired_manifest_and_explicit_confirmation(retained) -> None:
    preview = storage_retention.preview(retained["repo"], base=retained["base"])
    moved = storage_retention.quarantine(
        retained["repo"], preview_digest=preview["preview_digest"], confirm=True, base=retained["base"]
    )
    batch = (
        retained["base"]
        / storage_retention.QUARANTINE_DIRNAME
        / task_store.storage_readiness(retained["repo"]).repo_id
        / moved["batch_id"]
    )
    manifest_path = batch / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["restore_deadline"] = "2000-01-01T00:00:00+00:00"
    storage_retention._atomic_json(manifest_path, manifest)

    with pytest.raises(storage_retention.StorageRetentionError, match="explicit_confirmation_required"):
        storage_retention.purge(
            retained["repo"], batch_id=moved["batch_id"], confirm=False, base=retained["base"]
        )
    result = storage_retention.purge(
        retained["repo"], batch_id=moved["batch_id"], confirm=True, base=retained["base"]
    )
    assert result["purged"] is True
    assert not batch.exists()


def test_stale_aiworkhub_registration_requires_digest_and_explicit_prune(retained) -> None:
    shutil.rmtree(retained["entry"])
    preview = storage_retention.preview(retained["repo"], base=retained["base"])
    registrations = preview["registration_health"]
    assert registrations["stale_candidate_count"] == 1
    assert registrations["foreign_stale_count"] == 0
    assert registrations["safe_to_prune"] is True
    assert registrations["stale_candidates"] == [
        {"id": "request-safe", "reason": "prunable"}
    ]

    with pytest.raises(storage_retention.StorageRetentionError, match="explicit_confirmation_required"):
        storage_retention.prune_stale_registrations(
            retained["repo"],
            preview_digest=registrations["preview_digest"],
            confirm=False,
            base=retained["base"],
        )
    with pytest.raises(storage_retention.StorageRetentionError, match="registration_preview_stale"):
        storage_retention.prune_stale_registrations(
            retained["repo"],
            preview_digest="0" * 64,
            confirm=True,
            base=retained["base"],
        )

    result = storage_retention.prune_stale_registrations(
        retained["repo"],
        preview_digest=registrations["preview_digest"],
        confirm=True,
        base=retained["base"],
    )
    assert result == {
        "ok": True,
        "pruned": 1,
        "ids": ["request-safe"],
        "no_op": False,
    }
    assert "request-safe" not in subprocess.run(
        ["git", "-C", str(retained["repo"]), "worktree", "list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_foreign_stale_registration_blocks_repository_wide_prune(retained, tmp_path: Path) -> None:
    outside = tmp_path / "foreign-worktree"
    _git(retained["repo"], "worktree", "add", "--detach", str(outside), "HEAD")
    shutil.rmtree(outside)
    registrations = storage_retention.preview(
        retained["repo"], base=retained["base"]
    )["registration_health"]
    assert registrations["foreign_stale_count"] == 1
    assert registrations["safe_to_prune"] is False
    with pytest.raises(
        storage_retention.StorageRetentionError,
        match="foreign_stale_registration_present",
    ):
        storage_retention.prune_stale_registrations(
            retained["repo"],
            preview_digest=registrations["preview_digest"],
            confirm=True,
            base=retained["base"],
        )


def test_registration_candidate_overflow_blocks_repository_wide_prune(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    base = tmp_path / "worktrees"
    root.mkdir()
    base.mkdir()
    monkeypatch.setattr(
        storage_retention.worktree_storage,
        "scan_worktree_registrations",
        lambda *_args: {
            "ok": True,
            "preview_digest": "d" * 64,
            "foreign_stale_count": 0,
            "candidate_overflow_count": 1,
            "stale_candidates": [{"id": "R000", "reason": "prunable"}],
        },
    )

    with pytest.raises(
        storage_retention.StorageRetentionError,
        match="registration_candidate_limit_exceeded",
    ):
        storage_retention.prune_stale_registrations(
            root,
            preview_digest="d" * 64,
            confirm=True,
            base=base,
        )
