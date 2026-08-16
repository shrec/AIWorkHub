"""Atomicity and write-truth guarantees for the canonical task store.

These tests pin the four archive/terminalization invariants that a stuck row
previously violated:

* an archive is a single atomic transition -- forcing a failure part-way
  through leaves the row wholly unarchived, never half-archived;
* a status change re-reads the row after commit and reports what is actually
  stored, so a write that did not persist reports failure rather than the
  value it intended to write;
* rows already left in the impossible ``archived_at``-set / non-terminal
  state are detected and explicitly, loggably repaired;
* a forced terminalization is terminal -- calling it again does not retry or
  re-transition the request.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_store  # noqa: E402


def _insert_task(repo: Path, task_id: str, *, status: str) -> None:
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-07-22T00:00:00+00:00"
    card = {
        "task_id": task_id,
        "runner": "codex_worker_b891",
        "topic": "task_lifecycle",
        "allowed_writes": ["out.txt"],
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at) "
            "VALUES (?, ?, 'task_lifecycle', ?, ?, '', '', ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                "codex_worker_b891",
                status,
                "claimed" if status == "processing" else "unclaimed",
                json.dumps(card),
                now,
                now,
                "codex_worker_b891" if status == "processing" else "",
                now if status == "processing" else "",
                now if status == "processing" else "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row(repo: Path, task_id: str) -> dict[str, str]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, worker_status, archived_at FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return {key: str(row[key] or "") for key in ("status", "worker_status", "archived_at")}


class _ConnProxy:
    """Delegate every connection call to a real connection, letting a subclass
    intercept one specific SQL statement to inject a failure or a no-op."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner

    def execute(self, sql: str, *args: object):
        return self._inner.execute(sql, *args)

    def commit(self) -> None:
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()

    def close(self) -> None:
        self._inner.close()


def _patch_write_connection(monkeypatch: pytest.MonkeyPatch, proxy_cls) -> None:
    real_connect = task_store._connect

    def fake_connect(path, *, readonly: bool = False):
        conn = real_connect(path, readonly=readonly)
        if readonly:
            return conn
        return proxy_cls(conn)

    monkeypatch.setattr(task_store, "_connect", fake_connect)


def test_archive_transition_is_atomic_and_leaves_row_wholly_unarchived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force a failure part-way through the archive (after ``archived_at`` and
    ``status`` are written in-transaction, before commit) and assert the row is
    left wholly unarchived rather than half-archived."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_ATOMIC_ARCHIVE", status="pending")

    class _EventInsertFails(_ConnProxy):
        def execute(self, sql: str, *args: object):
            if sql.lstrip().startswith("INSERT INTO task_events"):
                raise sqlite3.OperationalError("injected task_events failure")
            return self._inner.execute(sql, *args)

    _patch_write_connection(monkeypatch, _EventInsertFails)

    ok, state = task_store.archive_task(repo, "TASK_ATOMIC_ARCHIVE", actor="codex")
    assert ok is False
    assert state.startswith("archive_write_failed")

    monkeypatch.undo()
    row = _row(repo, "TASK_ATOMIC_ARCHIVE")
    assert row["archived_at"] == ""
    assert row["status"] == "pending"
    assert task_store.list_tasks(repo, status="archived") == []
    assert task_store.list_tasks(repo, status="pending")[0]["task_id"] == "TASK_ATOMIC_ARCHIVE"


def test_archive_reports_failure_when_write_did_not_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model the supersede-lied defect: the write path returns without actually
    writing the intended columns.  The post-commit re-read must catch this and
    report failure rather than the value it intended to write."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_WRITE_TRUTH", status="pending")

    class _ArchiveUpdateSwallowed(_ConnProxy):
        def execute(self, sql: str, *args: object):
            if sql.lstrip().startswith("UPDATE tasks SET archived_at="):
                # Match one row (rowcount == 1) but never write archived_at/status.
                return self._inner.execute(
                    "UPDATE tasks SET updated_at=updated_at WHERE task_id IS NOT NULL"
                )
            return self._inner.execute(sql, *args)

    _patch_write_connection(monkeypatch, _ArchiveUpdateSwallowed)

    ok, state = task_store.archive_task(repo, "TASK_WRITE_TRUTH", actor="codex")
    assert (ok, state) == (False, "archive_not_persisted")

    monkeypatch.undo()
    row = _row(repo, "TASK_WRITE_TRUTH")
    assert row["archived_at"] == ""
    assert row["status"] == "pending"


def test_archive_persists_terminal_status_atomically(tmp_path: Path) -> None:
    """The happy path writes ``archived_at`` and the terminal ``status`` column
    together, so no row is ever left archived-but-non-terminal."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_ARCHIVE_OK", status="pending")

    ok, state = task_store.archive_task(repo, "TASK_ARCHIVE_OK", actor="codex", reason="cleanup")
    assert (ok, state) == (True, "archived")

    row = _row(repo, "TASK_ARCHIVE_OK")
    assert row["archived_at"] != ""
    assert row["status"] == "archived"
    assert task_store.find_archive_inconsistencies(repo) == []


def test_detects_and_repairs_half_applied_archive_rows(tmp_path: Path) -> None:
    """A row with ``archived_at`` set while ``status`` is still ``pending`` is
    detected and explicitly, loggably repaired; ``dry_run`` detects without
    writing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_HALF_ARCHIVED", status="pending")
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET archived_at='2026-08-15T13:13:00+00:00' WHERE task_id=?",
            ("TASK_HALF_ARCHIVED",),
        )
        conn.commit()
    finally:
        conn.close()

    detected = task_store.find_archive_inconsistencies(repo)
    assert [item["task_id"] for item in detected] == ["TASK_HALF_ARCHIVED"]
    assert detected[0]["status"] == "pending"

    dry = task_store.repair_archive_inconsistencies(repo, dry_run=True)
    assert dry["repaired"] == [] and dry["count"] == 1
    assert _row(repo, "TASK_HALF_ARCHIVED")["status"] == "pending"

    result = task_store.repair_archive_inconsistencies(repo, actor="coordinator")
    assert result["repaired"] == ["TASK_HALF_ARCHIVED"]
    assert _row(repo, "TASK_HALF_ARCHIVED")["status"] == "archived"
    assert task_store.find_archive_inconsistencies(repo) == []

    events = [event["event"] for event in task_store.get_task_events(repo, "TASK_HALF_ARCHIVED")]
    assert "archive_inconsistency_repaired" in events


def test_force_terminalize_is_terminal_and_does_not_retry_again(tmp_path: Path) -> None:
    """A stuck request can be forced to a terminal state carrying the reason;
    a second call does not retry or re-transition it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_STUCK", status="pending")

    ok, state = task_store.force_terminalize(
        repo, "TASK_STUCK", reason="finalizer_retries_exhausted", actor="manager"
    )
    assert (ok, state) == (True, "blocked")

    detail = task_store.get_task(repo, "TASK_STUCK")
    assert detail is not None
    assert detail["status"] == "blocked"
    assert detail["blocker_reason"] == "finalizer_retries_exhausted"
    assert detail["terminal_failure"]["reason"] == "finalizer_retries_exhausted"

    def _forced_events() -> int:
        return sum(
            1
            for event in task_store.get_task_events(repo, "TASK_STUCK")
            if event["event"] == "force_terminalized"
        )

    assert _forced_events() == 1

    # Exhausted means terminal: a second attempt is a no-op, not another retry.
    ok2, state2 = task_store.force_terminalize(
        repo, "TASK_STUCK", reason="finalizer_retries_exhausted", actor="manager"
    )
    assert (ok2, state2) == (True, "already_terminal")
    assert _forced_events() == 1

    # The finalizer retry path also refuses a force-terminalized request.
    ok3, _state3 = task_store.retry_finalize_failed(
        repo, "TASK_STUCK", runner="codex_worker_b891", request_id="req-x"
    )
    assert ok3 is False
