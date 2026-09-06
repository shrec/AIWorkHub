from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import task_store, terminal_log_retention


# A fixed far-future reference clock. Retention-age cutoffs derive from
# ``terminal_log_retention.now_utc()``, so freezing it here makes every
# just-written file (real mtime) already older than ``logs_days`` without ever
# mutating filesystem timestamps (os.utime is denied in the outer validation
# sandbox).
_FROZEN_NOW = datetime(2035, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _freeze_retention_clock(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _run(repo: Path, request_id: str, task_id: str) -> None:
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    process_root.mkdir(parents=True, exist_ok=True)
    for suffix in terminal_log_retention._OWNED_SUFFIXES:
        path = process_root / f"{request_id}{suffix}"
        path.write_text(f"{request_id}\n", encoding="utf-8")
    ledger = repo / terminal_log_retention.PROCESS_LOG_RELATIVE_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "request_id": request_id,
            "task_id": task_id,
            "runner": "runner",
            "topic": "topic",
            "adapter_id": "codex_cli",
            "state": "exited",
            "finished_at": "2026-01-01T00:00:00+00:00",
        }) + "\n")
    ok, reason = task_store.append_usage_capture_event(
        repo,
        task_id,
        "runner",
        {
            "source": "task_mcp_launcher",
            "note": f"task_mcp_request:{request_id}",
            "usage_observed": False,
            "telemetry_reason": "provider_usage_report_not_observed",
        },
    )
    assert ok, reason


def test_preview_expires_all_old_finished_runs_and_protects_nonterminal_task(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(11):
        _run(repo, f"{index + 1:032x}", "TASK_DONE")
    _run(repo, "f" * 32, "TASK_REVIEW")

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
        _run(repo, f"{index + 1:032x}", "TASK_DONE")

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


def test_latest_rows_uses_incremental_ledger_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    ledger = repo / terminal_log_retention.PROCESS_LOG_RELATIVE_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{}\n", encoding="utf-8")
    request_id = "a" * 32
    projected = {
        request_id: {"request_id": request_id, "state": "exited"},
        "not-a-request": {"request_id": "not-a-request", "state": "exited"},
    }
    monkeypatch.setattr(
        terminal_log_retention.process_event_ledger,
        "latest_events",
        lambda _path: projected,
    )
    monkeypatch.setattr(
        terminal_log_retention.process_event_ledger,
        "iter_events",
        lambda _path: (_ for _ in ()).throw(AssertionError("full replay used")),
    )

    assert terminal_log_retention._latest_rows(repo) == {
        request_id: projected[request_id]
    }


def test_policy_enforcement_automatically_quarantines_expired_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    request_id = "1" * 32
    _run(repo, request_id, "TASK_DONE")

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


def test_preview_expresses_orphan_age_without_mtime_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    process_root.mkdir(parents=True, exist_ok=True)
    orphan_id = "b" * 32
    orphan_path = process_root / f"{orphan_id}.stdout.log"
    orphan_path.write_bytes(b"old")

    # An aged orphan (mtime well before the frozen cutoff) is still protected,
    # reported with the usage-capture-missing reason.
    aged = terminal_log_retention.preview(repo)
    assert aged["candidate_count"] == 0
    assert {row["reason"] for row in aged["protected"]} == {
        "usage_capture_receipt_missing"
    }

    # A recent orphan (clock pinned at real write time) is protected with the
    # retention-age-not-met reason instead.
    monkeypatch.setattr(
        terminal_log_retention, "now_utc", lambda: datetime.now(timezone.utc)
    )
    recent = terminal_log_retention.preview(repo)
    assert recent["candidate_count"] == 0
    assert {row["reason"] for row in recent["protected"]} == {
        "orphan_retention_age_not_met"
    }


def test_aged_legacy_store_is_protected_without_usage_receipts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    legacy = repo / "logs" / "processes"
    legacy.mkdir(parents=True)
    payload = legacy / "old.stdout.log"
    payload.write_bytes(b"legacy" * 1024)

    preview = terminal_log_retention.preview(repo)
    assert preview["legacy_candidate"] is None
    assert preview["candidate_count"] == 0
    assert preview["protected_count"] == 1
    assert payload.read_bytes() == b"legacy" * 1024


def test_usage_capture_backfill_is_idempotent_and_unblocks_retention(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    request_id = "d" * 32
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    process_root.mkdir(parents=True, exist_ok=True)
    for suffix in terminal_log_retention._OWNED_SUFFIXES:
        path = process_root / f"{request_id}{suffix}"
        path.write_text(
            '{"type":"result","usage":{"input_tokens":7,"output_tokens":3}}\n'
            if suffix == ".stdout.log" else "run\n",
            encoding="utf-8",
        )
    ledger = repo / terminal_log_retention.PROCESS_LOG_RELATIVE_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({
        "request_id": request_id,
        "task_id": "TASK_DONE",
        "runner": "runner",
        "topic": "topic",
        "adapter_id": "codex_cli",
        "model": "codex",
        "state": "exited",
    }) + "\n", encoding="utf-8")

    before = terminal_log_retention.preview(repo)
    assert before["candidate_count"] == 0
    assert before["protected_count"] == 1

    first = terminal_log_retention.backfill_usage_capture(repo, confirm=True)
    second = terminal_log_retention.backfill_usage_capture(repo, confirm=True)
    assert first["recorded"] == 1
    assert second["recorded"] == 0
    assert second["already_recorded"] == 1

    after = terminal_log_retention.preview(repo)
    assert after["candidate_count"] == 1
    usage = task_store.list_usage_events(repo)
    assert usage[0]["total_tokens"] == 10


def test_terminal_log_quarantine_restore_and_explicit_purge_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(11):
        _run(repo, f"{index + 1:032x}", "TASK_DONE")
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


def test_enforce_purges_expired_batch_beyond_bounded_ui_window(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    quarantine = repo / terminal_log_retention.QUARANTINE_RELATIVE_PATH
    quarantine.mkdir(parents=True, exist_ok=True)
    repo_id = terminal_log_retention._repo_id(repo)

    def write_batch(batch_id: str, deadline: str) -> Path:
        batch = quarantine / batch_id
        batch.mkdir()
        (batch / "payload.log").write_text("x", encoding="utf-8")
        terminal_log_retention._atomic_json(
            batch / terminal_log_retention.MANIFEST_NAME,
            {
                "schema_id": terminal_log_retention.SCHEMA_ID,
                "repo_id": repo_id,
                "batch_id": batch_id,
                "created_at": "2034-01-01T00:00:00+00:00",
                "restore_deadline": deadline,
                "status": "quarantined",
                "quarantined_bytes": 1,
                "items": [
                    {
                        "request_id": "a" * 32,
                        "state": "quarantined",
                        "files": [],
                    }
                ],
            },
        )
        return batch

    expired = write_batch(
        "l20200101T000000-000000000000", "2030-01-01T00:00:00+00:00"
    )
    for index in range(100):
        write_batch(
            f"l20340101T000000-{index:012x}", "2040-01-01T00:00:00+00:00"
        )

    # The operator-facing listing is deliberately bounded and cannot see the
    # older entry. Automatic enforcement must still scan every deadline.
    listed = terminal_log_retention.list_batches(repo)
    assert listed["count"] == 100
    assert all(row["batch_id"] != expired.name for row in listed["batches"])

    result = terminal_log_retention.enforce(repo)

    assert result["purged_batches"] == 1
    assert result["purged_bytes"] == 1
    assert result["next_deadline"] == "2040-01-01T00:00:00+00:00"
    assert not expired.exists()


def test_stale_preview_and_symlink_swap_fail_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(11):
        _run(repo, f"{index + 1:032x}", "TASK_DONE")
    preview = terminal_log_retention.preview(repo)
    request_id = preview["candidates"][0]["request_id"]
    target = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH / f"{request_id}.stdout.log"
    target.unlink()
    target.symlink_to(repo / terminal_log_retention.PROCESS_LOG_RELATIVE_PATH)

    with pytest.raises(terminal_log_retention.TerminalLogRetentionError, match="terminal_log_preview_stale"):
        terminal_log_retention.quarantine(
            repo, preview_digest=preview["preview_digest"], confirm=True
        )
