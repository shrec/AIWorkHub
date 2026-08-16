"""Empty terminal-log quarantine batches must never accumulate.

Two independently reported defects are covered here (verified against the
canonical repository on Linux, where 675 of 696 live batches were empty and not
one of those empty batches held any file on disk):

1. A batch that moves nothing must not be left behind holding a full seven-day
   restore deadline -- an empty batch has nothing to restore, so the deadline
   protects nothing while the entry accrues forever on the storage panel.
2. The sweep is invoked once per launched MCP child process (a background
   thread in ``server.main``), so several concurrent passes can run within one
   startup window.  No pass may leave an empty batch behind, and repeated
   passes over a store with nothing eligible must not grow the batch count.

Emptiness is defined narrowly and safely: a batch is reapable before its
deadline *only* when every item was skipped before its move
(``skipped_identity_changed``) so no file sits in the batch.  A batch holding
items in any content-bearing or already-acted state -- ``quarantined``,
``restored`` or ``restore_conflict`` -- keeps its full undo window and its
on-disk files are never removed inside it.  ``enforce`` reaps only batches whose
deadline has genuinely expired; a live empty batch is made *eligible* and
surfaced, and its actual release is left to an explicit operator purge.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import task_store, terminal_log_retention


# A fixed far-future reference clock, matching the sibling suite: every
# just-written file (real mtime) is already older than ``logs_days`` without
# mutating filesystem timestamps (os.utime is denied in the validation sandbox).
_FROZEN_NOW = datetime(2035, 1, 1, tzinfo=timezone.utc)
# A restore deadline well past the frozen clock: the undo window is still
# nominally active, so any reap of such a batch is because it is empty, not
# because the deadline expired.
_FUTURE_DEADLINE = "2040-01-01T00:00:00+00:00"
# A deadline already behind the frozen clock: the undo window has genuinely
# expired, so enforce may sweep the batch as before.
_EXPIRED_DEADLINE = "2030-01-01T00:00:00+00:00"

# Item states that leave a file physically inside the batch directory.  Only
# these should ever block an early purge or be preserved across one.
_HOLDS_FILE = frozenset({"quarantined", "restore_conflict"})


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
        conn.commit()
    finally:
        conn.close()
    return repo


def _run(repo: Path, request_id: str, task_id: str) -> None:
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    process_root.mkdir(parents=True, exist_ok=True)
    for suffix in terminal_log_retention._OWNED_SUFFIXES:
        (process_root / f"{request_id}{suffix}").write_text(f"{request_id}\n", encoding="utf-8")
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


def _write_batch(
    repo: Path,
    batch_id: str,
    *,
    status: str,
    deadline: str,
    item_states: tuple[str, ...],
    legacy_state: str | None = None,
) -> str:
    """Materialise a batch directory with a valid, identity-matching manifest.

    One request subdir is created per item.  States that physically hold a file
    inside the batch (``quarantined``, ``restore_conflict``) get a real file
    written, so a test can prove those files are never removed inside the undo
    window; every other state leaves the subdir empty, mirroring a batch that
    moved nothing (skipped) or already moved its content back out (restored).
    """

    repo_id = task_store.storage_readiness(repo).repo_id
    batch = repo / terminal_log_retention.QUARANTINE_RELATIVE_PATH / batch_id
    batch.mkdir(parents=True)
    items: list[dict[str, object]] = []
    for index, state in enumerate(item_states):
        request_id = f"{index:032x}"
        (batch / request_id).mkdir()
        name = f"{request_id}.stdout.log"
        if state in _HOLDS_FILE:
            (batch / request_id / name).write_text("x", encoding="utf-8")
        items.append({
            "request_id": request_id,
            "state": state,
            "files": [{"name": name, "size_bytes": 1, "mtime_ns": 1}],
        })
    legacy: dict[str, object] | None = None
    if legacy_state is not None:
        if legacy_state in _HOLDS_FILE:
            (batch / "legacy-logs").mkdir()
            (batch / "legacy-logs" / "old.log").write_text("x", encoding="utf-8")
        legacy = {
            "file_count": 1 if legacy_state in _HOLDS_FILE else 0,
            "size_bytes": 1 if legacy_state in _HOLDS_FILE else 0,
            "newest_mtime_ns": 1,
            "state": legacy_state,
        }
    held = sum(1 for s in item_states if s in _HOLDS_FILE) + (
        1 if legacy_state in _HOLDS_FILE else 0
    )
    manifest = {
        "schema_id": terminal_log_retention.SCHEMA_ID,
        "repo_id": repo_id,
        "batch_id": batch_id,
        "created_at": "2035-01-01T00:00:00+00:00",
        "restore_deadline": deadline,
        "preview_digest": "0" * 64,
        "status": status,
        "items": items,
        "legacy_store": legacy,
        "quarantined_files": held,
        "quarantined_bytes": held,
    }
    (batch / terminal_log_retention.MANIFEST_NAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return batch_id


def _files_in_batch(repo: Path, batch_id: str) -> set[str]:
    """Every non-manifest regular file physically present under the batch."""

    batch = repo / terminal_log_retention.QUARANTINE_RELATIVE_PATH / batch_id
    return {
        str(path.relative_to(batch))
        for path in batch.rglob("*")
        if path.is_file() and path.name != terminal_log_retention.MANIFEST_NAME
    }


def test_enforce_with_nothing_eligible_opens_no_batch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = terminal_log_retention.enforce(repo)

    assert result["status"] == "completed"
    assert result["quarantined_files"] == 0
    assert result["batch_id"] == ""
    assert terminal_log_retention.list_batches(repo)["count"] == 0


def test_repeated_sweeps_within_one_startup_do_not_accumulate_batches(tmp_path: Path) -> None:
    # Several passes run within a single startup window (one per launched MCP
    # child).  With nothing eligible, none of them may open a batch.
    repo = _repo(tmp_path)

    for _ in range(5):
        terminal_log_retention.enforce(repo)

    assert terminal_log_retention.list_batches(repo)["count"] == 0


def test_quarantine_reaps_an_empty_batch_it_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reproduce the cross-process TOCTOU: the digest is validated while the
    # files are present, but a concurrent sweep moves them before this pass
    # performs its own move, so nothing is quarantined.  The batch this pass
    # opened must be reaped immediately rather than left behind empty.
    repo = _repo(tmp_path)
    _run(repo, "1" * 32, "TASK_DONE")
    preview = terminal_log_retention.preview(repo)
    assert preview["candidate_count"] == 1
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH

    real_payload = terminal_log_retention._candidate_payload

    def _racing_payload(root: Path) -> dict[str, object]:
        payload = real_payload(root)
        for item in payload.get("candidates", []):
            for entry in item.get("files", []):
                (process_root / entry["name"]).unlink()
        return payload

    monkeypatch.setattr(terminal_log_retention, "_candidate_payload", _racing_payload)

    result = terminal_log_retention.quarantine(
        repo, preview_digest=preview["preview_digest"], confirm=True
    )

    assert result["quarantined"] == 0
    assert result["no_op"] is True
    assert result["batch_id"] == ""
    assert terminal_log_retention.list_batches(repo)["count"] == 0


def test_empty_batch_of_skipped_items_is_reapable_but_enforce_leaves_it(tmp_path: Path) -> None:
    # The exact shape observed on the canonical store: status "empty", three
    # skipped items, no file on disk, and a still-live seven-day deadline.
    repo = _repo(tmp_path)
    batch_id = _write_batch(
        repo, "l20260816T101142-000000000001", status="empty",
        deadline=_FUTURE_DEADLINE,
        item_states=("skipped_identity_changed",) * 3,
    )
    assert _files_in_batch(repo, batch_id) == set()

    # It is surfaced to the operator as reapable even though the deadline is
    # still live, because it holds nothing to restore.
    listed = terminal_log_retention.list_batches(repo)["batches"][0]
    assert listed["purge_eligible"] is True

    # enforce must NOT auto-purge it (a stated forbidden): the operator decides.
    result = terminal_log_retention.enforce(repo)
    assert result["purged_batches"] == 0
    assert terminal_log_retention.list_batches(repo)["count"] == 1

    # An explicit operator purge reaps it inside the live window.
    purged = terminal_log_retention.purge(repo, batch_id=batch_id, confirm=True)
    assert purged["purged"] is True
    assert terminal_log_retention.list_batches(repo)["count"] == 0


@pytest.mark.parametrize("state", ["quarantined", "restored", "restore_conflict"])
def test_batch_holding_items_in_any_state_keeps_its_deadline(
    tmp_path: Path, state: str
) -> None:
    # Any item in a non-skipped state means the batch is not empty.  It keeps
    # its full restore deadline, refuses an early purge, and -- crucially for a
    # restore_conflict, whose files are still physically present -- never loses
    # a file inside the undo window.
    repo = _repo(tmp_path)
    batch_id = _write_batch(
        repo, "l20260816T101142-0000000000ff", status="quarantined",
        deadline=_FUTURE_DEADLINE, item_states=(state,),
    )
    before = _files_in_batch(repo, batch_id)

    listed = terminal_log_retention.list_batches(repo)["batches"][0]
    assert listed["purge_eligible"] is False

    # enforce leaves a live batch that holds items untouched.
    assert terminal_log_retention.enforce(repo)["purged_batches"] == 0

    with pytest.raises(
        terminal_log_retention.TerminalLogRetentionError,
        match="retention_undo_window_active",
    ):
        terminal_log_retention.purge(repo, batch_id=batch_id, confirm=True)

    assert _files_in_batch(repo, batch_id) == before
    assert terminal_log_retention.list_batches(repo)["count"] == 1


def test_mixed_batch_with_one_held_item_is_not_reapable(tmp_path: Path) -> None:
    # Guards against the over-broad generalisation "no quarantined items => reap":
    # a batch with one quarantined item among skipped ones still holds a file and
    # must keep its deadline.
    repo = _repo(tmp_path)
    batch_id = _write_batch(
        repo, "l20260816T101142-0000000000aa", status="quarantined",
        deadline=_FUTURE_DEADLINE,
        item_states=("skipped_identity_changed", "quarantined", "skipped_identity_changed"),
    )
    before = _files_in_batch(repo, batch_id)
    assert before  # the quarantined item's file is physically present

    assert terminal_log_retention.list_batches(repo)["batches"][0]["purge_eligible"] is False
    with pytest.raises(
        terminal_log_retention.TerminalLogRetentionError,
        match="retention_undo_window_active",
    ):
        terminal_log_retention.purge(repo, batch_id=batch_id, confirm=True)
    assert _files_in_batch(repo, batch_id) == before


def test_batch_with_quarantined_legacy_store_keeps_its_deadline(tmp_path: Path) -> None:
    # A batch whose only content is a quarantined legacy ``logs/`` store still
    # holds files and is not empty.
    repo = _repo(tmp_path)
    batch_id = _write_batch(
        repo, "l20260816T101142-0000000000bb", status="quarantined",
        deadline=_FUTURE_DEADLINE, item_states=(), legacy_state="quarantined",
    )
    before = _files_in_batch(repo, batch_id)
    assert before

    assert terminal_log_retention.list_batches(repo)["batches"][0]["purge_eligible"] is False
    with pytest.raises(
        terminal_log_retention.TerminalLogRetentionError,
        match="retention_undo_window_active",
    ):
        terminal_log_retention.purge(repo, batch_id=batch_id, confirm=True)
    assert _files_in_batch(repo, batch_id) == before


def test_enforce_still_sweeps_a_genuinely_expired_batch(tmp_path: Path) -> None:
    # The normal expiry path is intact: a batch whose undo window has actually
    # passed is still reaped by enforce, empty or not.
    repo = _repo(tmp_path)
    _write_batch(
        repo, "l20260816T101142-0000000000cc", status="quarantined",
        deadline=_EXPIRED_DEADLINE, item_states=("quarantined",),
    )

    result = terminal_log_retention.enforce(repo)

    assert result["purged_batches"] == 1
    assert terminal_log_retention.list_batches(repo)["count"] == 0
