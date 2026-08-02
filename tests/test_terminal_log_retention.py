from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from aiworkhub import task_store, terminal_log_retention


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    readiness = task_store.storage_readiness(repo)
    now = "2026-01-01T00:00:00+00:00"
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at,completed_at,origin_thread_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("TASK_DONE", "runner", "topic", "finished", "done", "{}", now, now, now, "thread"),
        )
        conn.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at,origin_thread_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("TASK_REVIEW", "runner", "topic", "review", "review", "{}", now, now, "thread"),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


def _run(repo: Path, request_id: str, task_id: str, *, age_days: int = 20) -> None:
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    process_root.mkdir(parents=True, exist_ok=True)
    old = time.time() - age_days * 86400
    for suffix in terminal_log_retention._OWNED_SUFFIXES:
        path = process_root / f"{request_id}{suffix}"
        path.write_text(f"{request_id}\n", encoding="utf-8")
        os.utime(path, (old, old))
    ledger = repo / terminal_log_retention.PROCESS_LOG_RELATIVE_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "request_id": request_id,
            "task_id": task_id,
            "state": "exited",
            "finished_at": "2026-01-01T00:00:00+00:00",
        }) + "\n")


def test_preview_expires_all_old_finished_runs_and_protects_nonterminal_task(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(11):
        _run(repo, f"{index + 1:032x}", "TASK_DONE", age_days=20 + index)
    _run(repo, "f" * 32, "TASK_REVIEW", age_days=100)

    result = terminal_log_retention.preview(repo)

    assert result["dry_run"] is True
    assert result["repository_scoped"] is True
    assert result["candidate_count"] == 11
    assert {row["request_id"] for row in result["candidates"]} == {
        f"{index + 1:032x}" for index in range(11)
    }
    assert result["protected_count"] == 1
    assert (repo / terminal_log_retention.PROCESS_LOG_RELATIVE_PATH).is_file()


def test_preview_is_paginated_but_digest_covers_full_candidate_set(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(75):
        _run(repo, f"{index + 1:032x}", "TASK_DONE", age_days=20)

    first = terminal_log_retention.preview(repo, limit=20)
    second = terminal_log_retention.preview(repo, cursor=20, limit=20)
    summary = terminal_log_retention.preview(repo, include_candidates=False)

    assert first["candidate_count"] == 75
    assert first["candidate_total"] == 75
    assert first["returned_count"] == 20
    assert first["next_cursor"] == 20
    assert second["cursor"] == 20
    assert second["returned_count"] == 20
    assert first["preview_digest"] == second["preview_digest"]
    assert "candidates" not in summary
    assert summary["candidate_total"] == 75


def test_policy_enforcement_automatically_quarantines_expired_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    request_id = "1" * 32
    _run(repo, request_id, "TASK_DONE", age_days=20)

    result = terminal_log_retention.enforce(repo)

    assert result["status"] == "completed"
    assert result["quarantined_files"] == 4
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    assert not (process_root / f"{request_id}.stdout.log").exists()
    assert terminal_log_retention.list_batches(repo)["count"] == 1


def test_preview_accounts_for_orphan_request_files_and_legacy_store(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    orphan_id = "a" * 32
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    process_root.mkdir(parents=True, exist_ok=True)
    orphan = process_root / f"{orphan_id}.stdout.log"
    orphan.write_bytes(b"o" * 3072)
    legacy = repo / terminal_log_retention.LEGACY_PROCESS_FILES_RELATIVE_PATH
    legacy.mkdir(parents=True)
    (legacy / "old.stdout.log").write_bytes(b"l" * 4096)

    result = terminal_log_retention.preview(repo)

    assert result["orphan_file_count"] == 1
    assert result["orphan_file_bytes"] == 3072
    assert result["legacy_current_bytes"] >= 4096
    assert result["legacy_status"] == "present_unmanaged"
    assert result["current_bytes"] >= 7168
    assert result["protected_count"] == 2


def test_preview_expires_aged_orphan_files_without_touching_recent_orphans(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    process_root.mkdir(parents=True, exist_ok=True)
    old_id = "b" * 32
    recent_id = "c" * 32
    old_path = process_root / f"{old_id}.stdout.log"
    recent_path = process_root / f"{recent_id}.stderr.log"
    old_path.write_bytes(b"old")
    recent_path.write_bytes(b"recent")
    old = time.time() - 20 * 86400
    os.utime(old_path, (old, old))

    result = terminal_log_retention.preview(repo)

    assert result["candidate_count"] == 1
    assert result["candidates"][0]["request_id"] == old_id
    assert result["candidates"][0]["state"] == "orphaned"
    assert recent_id not in {row["request_id"] for row in result["candidates"]}
    assert result["protected_count"] == 1


def test_aged_legacy_store_quarantine_and_restore_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    legacy = repo / "logs" / "processes"
    legacy.mkdir(parents=True)
    payload = legacy / "old.stdout.log"
    payload.write_bytes(b"legacy" * 1024)
    old = time.time() - 20 * 86400
    os.utime(payload, (old, old))

    preview = terminal_log_retention.preview(repo)
    assert preview["legacy_candidate"]["size_bytes"] == payload.stat().st_size
    moved = terminal_log_retention.quarantine(
        repo,
        preview_digest=preview["preview_digest"],
        confirm=True,
    )

    assert moved["no_op"] is False
    assert moved["bytes"] == len(b"legacy" * 1024)
    assert not (repo / "logs").exists()
    restored = terminal_log_retention.restore(
        repo,
        batch_id=moved["batch_id"],
        confirm=True,
    )
    assert restored["restored"] == 1
    assert payload.read_bytes() == b"legacy" * 1024


def test_terminal_log_quarantine_restore_and_explicit_purge_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(11):
        _run(repo, f"{index + 1:032x}", "TASK_DONE", age_days=20 + index)
    preview = terminal_log_retention.preview(repo)

    moved = terminal_log_retention.quarantine(
        repo, preview_digest=preview["preview_digest"], confirm=True
    )

    assert moved["quarantined"] == 44
    request_id = preview["candidates"][0]["request_id"]
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    assert not (process_root / f"{request_id}.stdout.log").exists()
    assert (repo / terminal_log_retention.PROCESS_LOG_RELATIVE_PATH).is_file()
    batches = terminal_log_retention.list_batches(repo)
    assert batches["count"] == 1

    with pytest.raises(terminal_log_retention.TerminalLogRetentionError, match="retention_undo_window_active"):
        terminal_log_retention.purge(repo, batch_id=moved["batch_id"], confirm=True)
    restored = terminal_log_retention.restore(repo, batch_id=moved["batch_id"], confirm=True)
    assert restored["restored"] == 44
    assert (process_root / f"{request_id}.stdout.log").is_file()


def test_stale_preview_and_symlink_swap_fail_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(11):
        _run(repo, f"{index + 1:032x}", "TASK_DONE", age_days=20 + index)
    preview = terminal_log_retention.preview(repo)
    request_id = preview["candidates"][0]["request_id"]
    target = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH / f"{request_id}.stdout.log"
    target.unlink()
    target.symlink_to(repo / terminal_log_retention.PROCESS_LOG_RELATIVE_PATH)

    with pytest.raises(terminal_log_retention.TerminalLogRetentionError, match="terminal_log_preview_stale"):
        terminal_log_retention.quarantine(
            repo, preview_digest=preview["preview_digest"], confirm=True
        )
