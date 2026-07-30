from __future__ import annotations

import json
import os
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
