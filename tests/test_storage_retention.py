from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from aiworkhub import storage_retention, task_store, worktree_storage

# terminal_runs_days defaults to 30; 31 real days pushes a worktree past the
# default policy threshold without ever touching its on-disk mtime. Age is
# injected via an explicit ``now`` rather than ``os.utime``: workers run
# under a landlock sandbox that forbids changing filesystem mtimes.
_AGED_NOW_OFFSET_DAYS = 31


def _aged_now() -> float:
    return time.time() + _AGED_NOW_OFFSET_DAYS * 86400


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
    return {"repo": repo, "base": base, "entry": entry}


def test_preview_is_repo_scoped_deterministic_and_side_effect_free(retained) -> None:
    aged_now = _aged_now()
    first = storage_retention.preview(retained["repo"], base=retained["base"], now=aged_now)
    second = storage_retention.preview(retained["repo"], base=retained["base"], now=aged_now)

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
    aged_now = _aged_now()
    preview = storage_retention.preview(retained["repo"], base=retained["base"], now=aged_now)

    moved = storage_retention.quarantine(
        retained["repo"],
        preview_digest=preview["preview_digest"],
        confirm=True,
        base=retained["base"],
        now=aged_now,
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
    aged_now = _aged_now()
    preview = storage_retention.preview(retained["repo"], base=retained["base"], now=aged_now)
    # Invalidate the prior scan's identity without touching mtimes directly
    # (landlock forbids os.utime): writing a new file into the entry bumps
    # its real mtime as a normal side effect of the write, which changes
    # modified_at_epoch/size_bytes and therefore the preview digest.
    (retained["entry"] / "touched.txt").write_text("x", encoding="utf-8")
    with pytest.raises(storage_retention.StorageRetentionError, match="retention_preview_stale"):
        storage_retention.quarantine(
            retained["repo"],
            preview_digest=preview["preview_digest"],
            confirm=True,
            base=retained["base"],
            now=aged_now,
        )

    fresh = storage_retention.preview(retained["repo"], base=retained["base"], now=aged_now)
    moved = storage_retention.quarantine(
        retained["repo"],
        preview_digest=fresh["preview_digest"],
        confirm=True,
        base=retained["base"],
        now=aged_now,
    )
    with pytest.raises(storage_retention.StorageRetentionError, match="retention_undo_window_active"):
        storage_retention.purge(
            retained["repo"], batch_id=moved["batch_id"], confirm=True, base=retained["base"]
        )


def test_purge_requires_expired_manifest_and_explicit_confirmation(retained) -> None:
    aged_now = _aged_now()
    preview = storage_retention.preview(retained["repo"], base=retained["base"], now=aged_now)
    moved = storage_retention.quarantine(
        retained["repo"],
        preview_digest=preview["preview_digest"],
        confirm=True,
        base=retained["base"],
        now=aged_now,
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


# --- AWH-OBS-013: empty worktree quarantine batches -------------------------
# The terminal-log quarantine already self-reaps an empty batch and offers a
# purge_empty_batches reaper (see tests/test_terminal_log_retention_empty_batches.py).
# The worktree quarantine here previously did neither: an all-skipped batch was
# left on disk holding a live seven-day deadline, and there was no reaper for it.
# These prove both halves are now closed for the worktree subsystem too.


def _repo_id(repo: Path) -> str:
    return task_store.storage_readiness(repo).repo_id


def _write_empty_batch(
    base: Path, repo_id: str, batch_id: str, *, item_states, with_payload: bool = False
) -> Path:
    """Materialise a worktree quarantine batch directory with a valid manifest.

    A ``quarantined`` item gets a real file under ``<batch>/<id>/worktree`` so a
    test can prove a content-bearing batch is never reaped; every skipped item
    leaves no directory, mirroring a batch that moved nothing.
    """
    qroot = base / storage_retention.QUARANTINE_DIRNAME / repo_id
    qroot.mkdir(parents=True, exist_ok=True)
    batch = qroot / batch_id
    batch.mkdir()
    items = []
    for index, state in enumerate(item_states):
        item_id = f"request-{index:03d}"
        if with_payload and state == "quarantined":
            (batch / item_id / "worktree").mkdir(parents=True)
            (batch / item_id / "worktree" / "f.txt").write_text("x", encoding="utf-8")
        items.append({
            "id": item_id,
            "head": "0" * 40,
            "size_bytes": 1,
            "modified_at_epoch": 1,
            "state": state,
        })
    manifest = {
        "schema_id": storage_retention.SCHEMA_ID,
        "repo_id": repo_id,
        "batch_id": batch_id,
        "created_at": "2035-01-01T00:00:00+00:00",
        # A deadline far in the future: any reap is because the batch is empty,
        # not because the undo window expired.
        "restore_deadline": "2040-01-01T00:00:00+00:00",
        "preview_digest": "0" * 64,
        "status": "empty" if all(s in storage_retention._EMPTY_BATCH_ITEM_STATES for s in item_states) else "quarantined",
        "items": items,
        "quarantined_count": sum(1 for s in item_states if s == "quarantined"),
        "quarantined_bytes": sum(1 for s in item_states if s == "quarantined"),
    }
    storage_retention._atomic_json(batch / storage_retention.MANIFEST_NAME, manifest)
    return batch


def test_quarantine_self_reaps_a_batch_that_can_never_hold_anything(
    retained, monkeypatch
) -> None:
    # A batch whose sole candidate is skipped during the move (its git identity
    # changed after the digest was reconfirmed) moves nothing. It must be reaped
    # at the source, not left behind empty holding a live undo window.
    aged_now = _aged_now()
    preview = storage_retention.preview(retained["repo"], base=retained["base"], now=aged_now)
    assert preview["candidate_count"] == 1

    real_state = worktree_storage._worktree_git_state

    # quarantine reconfirms the digest by re-running the FULL footprint
    # measurement -- a fresh single-flight walk, because the preview() above
    # already completed and evicted its own. Both that re-measurement and the
    # preview() above resolve git state on the OFF-thread daemon walk (thread
    # name "aiworkhub-retention-preview"); the sole MAIN-thread _worktree_git_state
    # call is quarantine's move-loop identity recheck (storage_retention.py:1282).
    # So discriminate by thread, never by a call counter: a counter miscounts
    # because the head is read once per measurement (here: the explicit preview,
    # then quarantine's re-measurement) BEFORE the move loop is ever reached, so a
    # ">= 2nd call" rule corrupts quarantine's digest reconfirmation and raises
    # retention_preview_stale instead of exercising the self-reap. Threading it
    # keeps the head stable through both measurements (digest reconfirms clean)
    # and changes it only at the move -- exactly "identity changed between the
    # digest snapshot and the move", so the sole candidate becomes
    # skipped_git_state_changed, nothing moves, and the empty batch self-reaps.
    def _flaky_state(worktree_dir):
        state = dict(real_state(worktree_dir))
        on_measurement_thread = (
            threading.current_thread().name == "aiworkhub-retention-preview"
        )
        if not on_measurement_thread and state.get("head"):
            head = state["head"]
            state["head"] = ("b" if head[0] != "b" else "c") + head[1:]
        return state

    monkeypatch.setattr(worktree_storage, "_worktree_git_state", _flaky_state)

    result = storage_retention.quarantine(
        retained["repo"],
        preview_digest=preview["preview_digest"],
        confirm=True,
        base=retained["base"],
        now=aged_now,
    )

    assert result["no_op"] is True
    assert result["batch_id"] == ""
    assert result["quarantined"] == 0
    # No empty batch was left on disk and none is listed.
    assert storage_retention.list_batches(retained["repo"], base=retained["base"])["count"] == 0
    qroot = retained["base"] / storage_retention.QUARANTINE_DIRNAME
    assert not qroot.exists() or list(qroot.rglob(storage_retention.MANIFEST_NAME)) == []


def test_purge_empty_batches_reaps_preexisting_empty_but_spares_content(retained) -> None:
    repo_id = _repo_id(retained["repo"])
    empty = _write_empty_batch(
        retained["base"], repo_id, "q20260816T101142-000000000001",
        item_states=("skipped_identity_changed", "skipped_git_state_changed"),
    )
    full = _write_empty_batch(
        retained["base"], repo_id, "q20260816T101142-0000000000ff",
        item_states=("quarantined",), with_payload=True,
    )
    # Surfaced as reapable_empty vs not, even though both deadlines are live.
    listed = {b["batch_id"]: b for b in storage_retention.list_batches(
        retained["repo"], base=retained["base"])["batches"]}
    assert listed[empty.name]["reapable_empty"] is True
    assert listed[full.name]["reapable_empty"] is False

    result = storage_retention.purge_empty_batches(
        retained["repo"], confirm=True, base=retained["base"]
    )
    assert result["batch_ids"] == [empty.name]
    assert not empty.exists()
    assert full.exists()  # a content-bearing batch is never touched by the reaper


def test_purge_empty_batches_requires_confirmation(retained) -> None:
    with pytest.raises(storage_retention.StorageRetentionError, match="explicit_confirmation_required"):
        storage_retention.purge_empty_batches(
            retained["repo"], confirm=False, base=retained["base"]
        )


def test_purge_reaps_reapable_empty_batch_inside_live_undo_window(retained) -> None:
    # An empty batch holds nothing to restore, so an explicit purge reaps it even
    # before its deadline -- while a content-bearing batch still fails closed
    # (covered by test_stale_preview_and_early_purge_fail_closed).
    repo_id = _repo_id(retained["repo"])
    empty = _write_empty_batch(
        retained["base"], repo_id, "q20260816T101142-000000000002",
        item_states=("skipped_identity_changed",),
    )
    result = storage_retention.purge(
        retained["repo"], batch_id=empty.name, confirm=True, base=retained["base"]
    )
    assert result["purged"] is True
    assert not empty.exists()


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
