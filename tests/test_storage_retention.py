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
    manifest.pop("deadline_authentication", None)
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


def _finish_accepted_card(
    repo: Path, task_id: str, request_id: str, *, predecessor: str = "", idle: bool = True
) -> None:
    import sqlite3
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    card: dict[str, object] = {"accepted_request_id": request_id}
    if predecessor:
        card["rework_predecessor"] = {"request_id": predecessor}
    connection = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        connection.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "claude", "storage", "finished", "unclaimed", json.dumps(card), now, now),
        )
        connection.commit()
    finally:
        connection.close()
    if idle:
        _write_idle_process_identity(repo, request_id)
        if predecessor:
            _write_idle_process_identity(repo, predecessor)


def _write_idle_process_identity(repo: Path, request_id: str) -> None:
    from aiworkhub import process_event_ledger, process_launcher

    log_path = repo / process_launcher.PROCESS_LOG_DEFAULT_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process_event_ledger.append_event(
        log_path,
        {
            "pid": 1_000_000_001,
            "pid_start_ticks": 1,
            "request_id": request_id,
            "timestamp": "2026-01-01T00:00:00+00:00",
        },
    )


def _accepted_evidence(
    task_id: str, request_id: str, *, predecessor: str | None = ""
) -> dict[str, str]:
    from aiworkhub import task_retention

    predecessor_request_id = "" if predecessor is None else str(predecessor).strip()
    return {
        "schema_id": task_retention.ACCEPTED_CLEANUP_EVIDENCE_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "predecessor_request_id": predecessor_request_id,
        "canonical_digest": task_retention.canonical_acceptance_digest(
            accept_evidence={},
            accepted_request_id=request_id,
            predecessor_request_id=predecessor_request_id,
            request_id=request_id,
            status="finished",
            task_id=task_id,
        ),
    }


def test_cleanup_accepted_artifacts_removes_ephemeral_keeps_receipts(retained) -> None:
    from aiworkhub import task_retention

    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    (entry / "manifest.json").write_text('{"schema_id":"kept"}', encoding="utf-8")
    (entry / "receipt.json").write_text('{"ok":true}', encoding="utf-8")
    logs = entry / "logs"
    logs.mkdir()
    (logs / "out.log").write_text("ephemeral\n", encoding="utf-8")
    _finish_accepted_card(repo, "TASK-SAFE", "request-safe")

    first = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_evidence("TASK-SAFE", "request-safe"),
        base=base,
    )
    second = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_evidence("TASK-SAFE", "request-safe"),
        base=base,
    )

    assert first["ok"] is True
    assert first["schema_id"] == storage_retention.ARTIFACT_GC_SCHEMA_ID
    assert first["status"] == "cleaned"
    assert first["replayed"] is False
    assert "worktree" in first["removed"]
    assert "logs" in first["removed"]
    assert "manifest.json" in first["preserved"]
    assert "receipt.json" in first["preserved"]
    assert not (entry / "worktree").exists()
    assert not logs.exists()
    assert (entry / "manifest.json").is_file()
    assert (entry / "receipt.json").is_file()
    assert (entry / "artifact-gc.receipt.json").is_file()
    assert second["ok"] is True
    assert second["replayed"] is True
    assert second["receipt_digest"] == first["receipt_digest"]
    verdict = task_retention.validate_accepted_cleanup_evidence(
        repo,
        _accepted_evidence("TASK-SAFE", "request-safe"),
    )
    assert verdict["ok"] is True


def test_cleanup_accepted_artifacts_resumes_from_phase_evidence(retained) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    (entry / "scratch.bin").write_bytes(b"tmp")
    _finish_accepted_card(repo, "TASK-RESUME", "request-safe")
    evidence = _accepted_evidence("TASK-RESUME", "request-safe")
    phase_path = (
        repo / ".aiworkhub" / "runtime" / "storage" / "artifact-gc" / "request-safe.json"
    )
    phase_path.parent.mkdir(parents=True, exist_ok=True)
    phase_path.write_text(
        json.dumps(
            {
                "canonical_digest": evidence["canonical_digest"],
                "ephemeral": ["scratch.bin", "worktree"],
                "phase": "inventoried",
                "preserved": [],
                "request_id": "request-safe",
                "schema_id": storage_retention.ARTIFACT_GC_PHASE_SCHEMA_ID,
                "task_id": "TASK-RESUME",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)

    assert result["ok"] is True
    assert result["status"] == "cleaned"
    assert "scratch.bin" in result["removed"]
    assert not (entry / "scratch.bin").exists()
    assert not (entry / "worktree").exists()


def test_cleanup_accepted_artifacts_fail_closed_unknown_and_unresolved(retained) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    _finish_accepted_card(repo, "TASK-KNOWN", "request-safe")
    missing = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_evidence("TASK-MISSING", "request-safe"),
        base=base,
    )
    unknown = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence={
            "schema_id": "aiworkhub.accepted_cleanup_evidence.v1",
            "task_id": "TASK-KNOWN",
            "request_id": "request-safe",
            "canonical_digest": "0" * 64,
        },
        base=base,
    )

    assert missing["ok"] is False
    assert missing["status"] == "failed_closed"
    assert missing["reason"] == "unresolved_task"
    assert missing["deleted"] is False
    assert unknown["ok"] is False
    assert unknown["reason"] == "unknown_identity"
    assert unknown["deleted"] is False
    assert entry.is_dir()
    assert (entry / "worktree").is_dir()


def test_cleanup_registered_worktree_prunes_git_registration(retained) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    checkout = entry / "worktree"
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(checkout) in listed
    _finish_accepted_card(repo, "TASK-REG", "request-safe")

    result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_evidence("TASK-REG", "request-safe"),
        base=base,
    )

    assert result["ok"] is True
    assert "worktree" in result["removed"]
    assert not checkout.exists()
    after = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(checkout) not in after


def test_cleanup_resumes_from_quarantined_phase(retained) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    (entry / "scratch.bin").write_bytes(b"tmp")
    _finish_accepted_card(repo, "TASK-Q", "request-safe")
    evidence = _accepted_evidence("TASK-Q", "request-safe")
    qroot = storage_retention._ensure_quarantine_root(repo, base)
    batch_id = "agc20260101T000000-request-s"
    batch = qroot / batch_id
    batch.mkdir(parents=True)
    shutil.move(str(entry), str(batch / "request-safe"))
    phase_path = repo / ".aiworkhub" / "runtime" / "storage" / "artifact-gc" / "request-safe.json"
    phase_path.parent.mkdir(parents=True, exist_ok=True)
    phase_path.write_text(
        json.dumps(
            {
                "batch_id": batch_id,
                "canonical_digest": evidence["canonical_digest"],
                "ephemeral": ["scratch.bin", "worktree"],
                "phase": "quarantined",
                "preserved": [],
                "removed": [],
                "request_id": "request-safe",
                "retry_evidence": {
                    "canonical_digest": evidence["canonical_digest"],
                    "error": "simulated",
                    "phase": "inventoried",
                    "request_id": "request-safe",
                    "retryable": True,
                    "schema_id": storage_retention.ARTIFACT_GC_PHASE_SCHEMA_ID,
                    "task_id": "TASK-Q",
                },
                "schema_id": storage_retention.ARTIFACT_GC_PHASE_SCHEMA_ID,
                "task_id": "TASK-Q",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)

    restored = base / "request-safe"
    assert result["ok"] is True
    assert result["status"] == "cleaned"
    assert restored.is_dir()
    assert not (restored / "scratch.bin").exists()
    assert not (restored / "worktree").exists()
    assert (restored / "artifact-gc.receipt.json").is_file()


def _worktree_list(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_cleanup_missing_dir_stale_registration_unregisters_only_owned(retained) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    checkout = entry / "worktree"
    other_entry = base / "request-other"
    other = other_entry / "worktree"
    other_entry.mkdir()
    _git(repo, "worktree", "add", "--detach", str(other), "HEAD")
    shutil.rmtree(checkout)
    listed = _worktree_list(repo)
    assert str(checkout) in listed
    assert str(other) in listed
    _finish_accepted_card(repo, "TASK-STALE", "request-safe")
    evidence = _accepted_evidence("TASK-STALE", "request-safe")
    phase_path = (
        repo / ".aiworkhub" / "runtime" / "storage" / "artifact-gc" / "request-safe.json"
    )
    phase_path.parent.mkdir(parents=True, exist_ok=True)
    phase_path.write_text(
        json.dumps(
            {
                "canonical_digest": evidence["canonical_digest"],
                "ephemeral": ["worktree"],
                "phase": "inventoried",
                "preserved": [],
                "request_id": "request-safe",
                "schema_id": storage_retention.ARTIFACT_GC_PHASE_SCHEMA_ID,
                "task_id": "TASK-STALE",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = storage_retention.cleanup_accepted_artifacts(
        repo, evidence=evidence, base=base
    )

    assert result["ok"] is True
    assert result["schema_id"] == storage_retention.ARTIFACT_GC_SCHEMA_ID
    assert "worktree" in result["removed"]
    after = _worktree_list(repo)
    assert str(checkout) not in after
    assert str(other) in after
    assert other.is_dir()
    assert not checkout.exists()


def test_cleanup_fail_closed_absent_process_ledger(retained) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    _finish_accepted_card(repo, "TASK-NO-LEDGER", "request-safe", idle=False)

    result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_evidence("TASK-NO-LEDGER", "request-safe"),
        base=base,
    )

    assert result["ok"] is False
    assert result["status"] == "failed_closed"
    assert result["reason"] == "ambiguous_ownership"
    assert result["deleted"] is False
    assert result["ephemeral"] == []
    assert result["preserved"] == []
    assert result["removed"] == []
    assert result["predecessor_unpinned"] == ""
    assert entry.is_dir()
    assert (entry / "worktree").is_dir()


def test_cleanup_partial_delete_quarantines_and_replays_inventory(retained, monkeypatch) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    (entry / "manifest.json").write_text('{"schema_id":"kept"}', encoding="utf-8")
    logs = entry / "logs"
    logs.mkdir()
    (logs / "out.log").write_text("ephemeral\n", encoding="utf-8")
    _finish_accepted_card(repo, "TASK-PARTIAL", "request-safe")
    evidence = _accepted_evidence("TASK-PARTIAL", "request-safe")

    def _boom(repo_root, checkout):
        raise OSError("partial-delete")

    monkeypatch.setattr(storage_retention, "_remove_registered_checkout", _boom)
    first = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    monkeypatch.undo()

    assert first["ok"] is False
    assert first["status"] == "quarantined"
    assert first["reason"] == "cleanup_failed"
    assert first["deleted"] is False
    assert "logs" in first["removed"]
    assert "worktree" in first["ephemeral"]
    assert "manifest.json" in first["preserved"]
    assert first["predecessor_unpinned"] == ""
    assert not logs.exists() or not (base / "request-safe" / "logs").exists()

    second = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    restored = base / "request-safe"
    assert second["ok"] is True
    assert second["replayed"] is False
    assert set(first["removed"]).issubset(second["removed"])
    assert "logs" in second["removed"]
    assert "worktree" in second["removed"]
    assert "manifest.json" in second["preserved"]
    assert second["predecessor_unpinned"] == ""
    assert not (restored / "worktree").exists()
    assert (restored / "manifest.json").is_file()

    third = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    assert third["ok"] is True
    assert third["replayed"] is True
    assert set(first["removed"]).issubset(third["removed"])
    assert "logs" in third["removed"]
    assert "worktree" in third["removed"]
    assert "manifest.json" in third["preserved"]
    assert third["predecessor_unpinned"] == ""
    assert third["receipt_digest"] == second["receipt_digest"]


def test_cleanup_syncs_phase_after_predecessor_unpin(retained, monkeypatch) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    (entry / "manifest.json").write_text('{"schema_id":"kept"}', encoding="utf-8")
    _finish_accepted_card(repo, "TASK-UNPIN-SYNC", "request-safe", predecessor="pred-ok")
    evidence = _accepted_evidence("TASK-UNPIN-SYNC", "request-safe", predecessor="pred-ok")

    def _boom(*_args, **_kwargs):
        raise OSError("complete-after-unpin")

    monkeypatch.setattr(storage_retention, "_complete_artifact_gc", _boom)
    first = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    monkeypatch.undo()

    assert first["ok"] is False
    assert first["status"] == "quarantined"
    assert first["retry_evidence"]["phase"] == "predecessor_unpinned"
    assert first["predecessor_unpinned"] == "pred-ok"
    phase_path = repo / ".aiworkhub" / "runtime" / "storage" / "artifact-gc" / "request-safe.json"
    stored = json.loads(phase_path.read_text(encoding="utf-8"))
    assert stored["phase"] == "quarantined"
    assert stored["predecessor_unpinned"] == "pred-ok"
    assert stored["retry_evidence"]["phase"] == "predecessor_unpinned"

    second = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    assert second["ok"] is True
    assert second["replayed"] is False
    assert second["predecessor_unpinned"] == "pred-ok"
    third = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    assert third["ok"] is True
    assert third["replayed"] is True
    assert third["receipt_digest"] == second["receipt_digest"]


def test_cleanup_resumes_after_crash_following_predecessor_row_clear(
    retained, monkeypatch
) -> None:
    import sqlite3

    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    (entry / "manifest.json").write_text('{"schema_id":"kept"}', encoding="utf-8")
    _finish_accepted_card(repo, "TASK-UNPIN-CRASH", "request-safe", predecessor="pred-ok")
    evidence = _accepted_evidence("TASK-UNPIN-CRASH", "request-safe", predecessor="pred-ok")
    real_clear = storage_retention._clear_predecessor_pin

    def _crash_after_clear(*args, **kwargs):
        real_clear(*args, **kwargs)
        raise OSError("crash-after-row-clear")

    monkeypatch.setattr(storage_retention, "_clear_predecessor_pin", _crash_after_clear)
    first = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    monkeypatch.undo()

    assert first["ok"] is False
    assert first["deleted"] is False
    connection = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        card = json.loads(
            connection.execute(
                "SELECT card_json FROM tasks WHERE task_id=?",
                ("TASK-UNPIN-CRASH",),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert "rework_predecessor" not in card
    phase_path = repo / ".aiworkhub" / "runtime" / "storage" / "artifact-gc" / "request-safe.json"
    stored = json.loads(phase_path.read_text(encoding="utf-8"))
    retry_phase = stored.get("phase")
    if retry_phase == "quarantined":
        retry_phase = (stored.get("retry_evidence") or {}).get("phase")
    assert retry_phase == "predecessor_unpin_intent"
    assert stored["predecessor_unpinned"] == "pred-ok"

    second = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    assert second["ok"] is True
    assert second["replayed"] is False
    assert second["predecessor_unpinned"] == "pred-ok"
    third = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    assert third["ok"] is True
    assert third["replayed"] is True
    assert third["receipt_digest"] == second["receipt_digest"]
    assert third["predecessor_unpinned"] == "pred-ok"

def test_cleanup_partial_quarantine_persist_fail_closed_and_replays(retained, monkeypatch) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    (entry / "manifest.json").write_text('{"schema_id":"kept"}', encoding="utf-8")
    logs = entry / "logs"
    logs.mkdir()
    (logs / "out.log").write_text("ephemeral\n", encoding="utf-8")
    _finish_accepted_card(repo, "TASK-QPERSIST", "request-safe")
    evidence = _accepted_evidence("TASK-QPERSIST", "request-safe")
    real_write = storage_retention._write_artifact_gc_phase

    def _boom_checkout(repo_root, checkout):
        raise OSError("partial-delete")

    def _boom_phase(path, payload):
        if payload.get("phase") == "quarantined":
            raise OSError("phase-persist")
        return real_write(path, payload)

    monkeypatch.setattr(storage_retention, "_remove_registered_checkout", _boom_checkout)
    monkeypatch.setattr(storage_retention, "_write_artifact_gc_phase", _boom_phase)
    first = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    monkeypatch.undo()

    assert first["ok"] is False
    assert first["status"] == "failed_closed"
    assert first["reason"] == "cleanup_failed"
    assert first["deleted"] is False
    assert first["retry_evidence"]["quarantine_failed"] is True
    assert first["retry_evidence"].get("restore_failed") is not True
    restored = base / "request-safe"
    assert restored.is_dir()
    assert (restored / "manifest.json").is_file()
    phase_path = repo / ".aiworkhub" / "runtime" / "storage" / "artifact-gc" / "request-safe.json"
    stored = json.loads(phase_path.read_text(encoding="utf-8"))
    assert stored["phase"] != "quarantined"

    second = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    assert second["ok"] is True
    assert second["replayed"] is False
    assert "worktree" in second["removed"]
    assert "manifest.json" in second["preserved"]
    assert not (restored / "worktree").exists()
    assert (restored / "manifest.json").is_file()
    third = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    assert third["ok"] is True
    assert third["replayed"] is True
    assert third["receipt_digest"] == second["receipt_digest"]


def test_cleanup_invalid_predecessor_fail_closed_no_quarantine_loop(retained) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    (entry / "manifest.json").write_text('{"schema_id":"kept"}', encoding="utf-8")
    _finish_accepted_card(repo, "TASK-BADPRED", "request-safe", predecessor="bad id")
    evidence = _accepted_evidence("TASK-BADPRED", "request-safe", predecessor="bad id")
    qroot = storage_retention._ensure_quarantine_root(repo, base)

    first = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    second = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)

    assert first["ok"] is False
    assert first["status"] == "failed_closed"
    assert first["reason"] == "predecessor_identity_invalid"
    assert first["deleted"] is False
    assert first["predecessor_unpinned"] == ""
    assert second == first
    assert (base / "request-safe").is_dir()
    assert (base / "request-safe" / "manifest.json").is_file()
    phase_path = repo / ".aiworkhub" / "runtime" / "storage" / "artifact-gc" / "request-safe.json"
    stored = json.loads(phase_path.read_text(encoding="utf-8"))
    assert stored["phase"] == "ephemeral_removed"
    assert list(qroot.iterdir()) == []


def test_cleanup_quarantine_audit_fault_fail_closed_and_replays(retained, monkeypatch) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    (entry / "manifest.json").write_text('{"schema_id":"kept"}', encoding="utf-8")
    logs = entry / "logs"
    logs.mkdir()
    (logs / "out.log").write_text("ephemeral\n", encoding="utf-8")
    _finish_accepted_card(repo, "TASK-QAUDIT", "request-safe")
    evidence = _accepted_evidence("TASK-QAUDIT", "request-safe")
    qroot = storage_retention._ensure_quarantine_root(repo, base)

    def _boom_checkout(repo_root, checkout):
        raise OSError("partial-delete")

    def _boom_audit(*_args, **_kwargs):
        raise OSError("quarantine-audit")

    monkeypatch.setattr(storage_retention, "_remove_registered_checkout", _boom_checkout)
    monkeypatch.setattr(storage_retention, "_append_audit", _boom_audit)
    first = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    monkeypatch.undo()

    assert first["ok"] is False
    assert first["status"] == "failed_closed"
    assert first["reason"] == "quarantine_audit_failed"
    assert first["deleted"] is False
    assert first["batch_id"]
    assert first["retry_evidence"]["batch_id"] == first["batch_id"]
    assert first["retry_evidence"]["quarantine_failed"] is True
    assert first["retry_evidence"]["retryable"] is True
    assert not (base / "request-safe").exists()
    quarantined = qroot / first["batch_id"] / "request-safe"
    assert quarantined.is_dir()
    assert (quarantined / "manifest.json").is_file()
    phase_path = repo / ".aiworkhub" / "runtime" / "storage" / "artifact-gc" / "request-safe.json"
    stored = json.loads(phase_path.read_text(encoding="utf-8"))
    assert stored["phase"] == "quarantined"
    assert stored["batch_id"] == first["batch_id"]
    assert stored["retry_evidence"]["retryable"] is True
    audit_path = repo / storage_retention.AUDIT_RELATIVE_PATH
    assert not audit_path.is_file() or "artifact_gc_quarantined" not in audit_path.read_text(
        encoding="utf-8"
    )

    second = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    restored = base / "request-safe"
    assert second["ok"] is True
    assert second["replayed"] is False
    assert "worktree" in second["removed"]
    assert "manifest.json" in second["preserved"]
    assert not (restored / "worktree").exists()
    assert (restored / "manifest.json").is_file()

    third = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    assert third["ok"] is True
    assert third["replayed"] is True
    assert third["receipt_digest"] == second["receipt_digest"]


def test_cleanup_predecessor_substitution_fail_closed(retained) -> None:
    import sqlite3
    from aiworkhub import task_retention

    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    (entry / "manifest.json").write_text('{"schema_id":"kept"}', encoding="utf-8")
    _finish_accepted_card(repo, "TASK-SUB", "request-safe")
    digest_empty = task_retention.canonical_acceptance_digest(
        accept_evidence={},
        accepted_request_id="request-safe",
        predecessor_request_id="",
        request_id="request-safe",
        status="finished",
        task_id="TASK-SUB",
    )
    digest_none = task_retention.canonical_acceptance_digest(
        accept_evidence={},
        accepted_request_id="request-safe",
        predecessor_request_id=None,
        request_id="request-safe",
        status="finished",
        task_id="TASK-SUB",
    )
    digest_omitted = task_retention.canonical_acceptance_digest(
        accept_evidence={},
        accepted_request_id="request-safe",
        request_id="request-safe",
        status="finished",
        task_id="TASK-SUB",
    )
    assert digest_empty == digest_none == digest_omitted
    base_evidence = {
        "schema_id": task_retention.ACCEPTED_CLEANUP_EVIDENCE_SCHEMA,
        "task_id": "TASK-SUB",
        "request_id": "request-safe",
        "canonical_digest": digest_empty,
    }
    assert task_retention.validate_accepted_cleanup_evidence(repo, base_evidence)["ok"] is True
    assert (
        task_retention.validate_accepted_cleanup_evidence(
            repo, {**base_evidence, "predecessor_request_id": None}
        )["ok"]
        is True
    )
    assert (
        task_retention.validate_accepted_cleanup_evidence(
            repo, {**base_evidence, "predecessor_request_id": ""}
        )["ok"]
        is True
    )

    substituted = base / "pred-sub"
    substituted.mkdir()
    (substituted / "keep.bin").write_bytes(b"live")
    _write_idle_process_identity(repo, "pred-sub")
    connection = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        row = connection.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", ("TASK-SUB",)
        ).fetchone()
        card = json.loads(row[0])
        card["rework_predecessor"] = {"request_id": "pred-sub"}
        connection.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card), "TASK-SUB"),
        )
        connection.commit()
    finally:
        connection.close()

    result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_evidence("TASK-SUB", "request-safe"),
        base=base,
    )
    assert result["ok"] is False
    assert result["deleted"] is False
    assert result["reason"] == "unknown_identity"
    assert entry.is_dir()
    assert (entry / "worktree").is_dir()
    assert substituted.is_dir()
    assert (substituted / "keep.bin").is_file()


def test_cleanup_quarantine_resume_deduplicates_removed_names(retained) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    (entry / "manifest.json").write_text('{"schema_id":"kept"}', encoding="utf-8")
    _finish_accepted_card(repo, "TASK-DEDUPE", "request-safe")
    evidence = _accepted_evidence("TASK-DEDUPE", "request-safe")
    phase_path = (
        repo / ".aiworkhub" / "runtime" / "storage" / "artifact-gc" / "request-safe.json"
    )
    phase_path.parent.mkdir(parents=True, exist_ok=True)
    phase_path.write_text(
        json.dumps(
            {
                "canonical_digest": evidence["canonical_digest"],
                "ephemeral": ["logs", "scratch.bin", "worktree"],
                "phase": "inventoried",
                "preserved": ["manifest.json"],
                "removed": ["logs"],
                "request_id": "request-safe",
                "schema_id": storage_retention.ARTIFACT_GC_PHASE_SCHEMA_ID,
                "task_id": "TASK-DEDUPE",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)

    assert result["ok"] is True
    assert result["removed"] == ["logs", "worktree"]
    assert result["removed"].count("logs") == 1
    assert "scratch.bin" not in result["removed"]
    assert not (entry / "worktree").exists()
    assert (entry / "manifest.json").is_file()


def test_cleanup_completed_replay_fail_closed_on_receipt_digest_mismatch(retained) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    (entry / "manifest.json").write_text('{"schema_id":"kept"}', encoding="utf-8")
    _finish_accepted_card(repo, "TASK-REPLAY-DIGEST", "request-safe")
    evidence = _accepted_evidence("TASK-REPLAY-DIGEST", "request-safe")
    first = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)
    assert first["ok"] is True
    assert first["replayed"] is False
    phase_path = (
        repo / ".aiworkhub" / "runtime" / "storage" / "artifact-gc" / "request-safe.json"
    )
    stored = json.loads(phase_path.read_text(encoding="utf-8"))
    stored["receipt"]["receipt_digest"] = "0" * 64
    phase_path.write_text(json.dumps(stored, sort_keys=True) + "\n", encoding="utf-8")

    second = storage_retention.cleanup_accepted_artifacts(repo, evidence=evidence, base=base)

    assert second["ok"] is False
    assert second["deleted"] is False
    assert second["replayed"] is False
    assert second["reason"] == "unknown_identity"
    assert (entry / "manifest.json").is_file()
    assert (entry / "artifact-gc.receipt.json").is_file()


def test_cleanup_accepted_artifacts_accepts_colon_bearing_task_id(retained) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    task_id = "needfix:NF-2026-00385"
    (entry / "manifest.json").write_text('{"schema_id":"kept"}', encoding="utf-8")
    (entry / "receipt.json").write_text('{"ok":true}', encoding="utf-8")
    _finish_accepted_card(repo, task_id, "request-safe")

    result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_evidence(task_id, "request-safe"),
        base=base,
    )

    assert result["ok"] is True
    assert result["task_id"] == task_id
    assert result["status"] == "cleaned"
    assert not (entry / "worktree").exists()
    assert (entry / "manifest.json").is_file()
    assert (entry / "receipt.json").is_file()


def test_cleanup_accepted_artifacts_accepts_max_length_task_id(retained) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    task_id = "T" + ("a" * 255)
    (entry / "manifest.json").write_text('{"schema_id":"kept"}', encoding="utf-8")
    _finish_accepted_card(repo, task_id, "request-safe")

    result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_evidence(task_id, "request-safe"),
        base=base,
    )

    assert result["ok"] is True
    assert result["task_id"] == task_id
    assert result["status"] == "cleaned"
    assert not (entry / "worktree").exists()
    assert (entry / "manifest.json").is_file()


def test_cleanup_accepted_artifacts_rejects_malformed_task_id(retained) -> None:
    repo, base, entry = retained["repo"], retained["base"], retained["entry"]
    _finish_accepted_card(repo, "TASK-KNOWN", "request-safe")
    result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_evidence("T" + ("a" * 256), "request-safe"),
        base=base,
    )

    assert result["ok"] is False
    assert result["status"] == "failed_closed"
    assert result["reason"] == "unknown_identity"
    assert result["deleted"] is False
    assert entry.is_dir()
    assert (entry / "worktree").is_dir()
