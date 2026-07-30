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


def test_preview_keeps_last_ten_and_protects_nonterminal_task(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(11):
        _run(repo, f"{index + 1:032x}", "TASK_DONE", age_days=20 + index)
    _run(repo, "f" * 32, "TASK_REVIEW", age_days=100)

    result = terminal_log_retention.preview(repo)

    assert result["dry_run"] is True
    assert result["repository_scoped"] is True
    assert result["candidate_count"] == 1
    assert result["candidates"][0]["request_id"] == f"{11:032x}"
    assert result["protected_count"] == 11
    assert (repo / terminal_log_retention.PROCESS_LOG_RELATIVE_PATH).is_file()


def test_terminal_log_quarantine_restore_and_explicit_purge_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(11):
        _run(repo, f"{index + 1:032x}", "TASK_DONE", age_days=20 + index)
    preview = terminal_log_retention.preview(repo)

    moved = terminal_log_retention.quarantine(
        repo, preview_digest=preview["preview_digest"], confirm=True
    )

    assert moved["quarantined"] == 4
    request_id = preview["candidates"][0]["request_id"]
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    assert not (process_root / f"{request_id}.stdout.log").exists()
    assert (repo / terminal_log_retention.PROCESS_LOG_RELATIVE_PATH).is_file()
    batches = terminal_log_retention.list_batches(repo)
    assert batches["count"] == 1

    with pytest.raises(terminal_log_retention.TerminalLogRetentionError, match="retention_undo_window_active"):
        terminal_log_retention.purge(repo, batch_id=moved["batch_id"], confirm=True)
    restored = terminal_log_retention.restore(repo, batch_id=moved["batch_id"], confirm=True)
    assert restored["restored"] == 4
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
