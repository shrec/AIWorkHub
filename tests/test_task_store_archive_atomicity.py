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


def _insert_task(
    repo: Path, task_id: str, *, status: str, worker_status: str | None = None
) -> None:
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
                worker_status
                if worker_status is not None
                else ("claimed" if status == "processing" else "unclaimed"),
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

    def __getattr__(self, name: str):
        # ``in_transaction`` in particular: the leased write paths ask the real
        # connection whether a transaction is already open before issuing
        # BEGIN IMMEDIATE, so a proxy that answered for itself would report the
        # wrong transaction state to the code under test.
        return getattr(self._inner, name)


def _patch_write_connection(monkeypatch: pytest.MonkeyPatch, proxy_cls) -> None:
    real_connect = task_store._connect

    def fake_connect(path, *, readonly: bool = False, **kwargs):
        # ``explicit_txn`` must be forwarded, not dropped: the leased paths open
        # their connection with it and state their own transaction boundary.
        conn = real_connect(path, readonly=readonly, **kwargs)
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
        # A genuine half-archived row still bears the archive audit event: the
        # pre-NF-276 bug lost only the terminal status column write, not the
        # event.  Reconciliation trusts that event as proof the archive happened.
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES ('TASK_HALF_ARCHIVED', 'archived', 'codex', '{}', "
            "'2026-08-15T13:13:00+00:00')"
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


# --- restore is the exact inverse of archive --------------------------------


def _card_json(repo: Path, task_id: str) -> str:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        return str(conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()[0])
    finally:
        conn.close()


def _newest_event_payload(repo: Path, task_id: str, events: tuple[str, ...]) -> dict:
    # ``get_task_events`` orders event_id DESC, so index 0 is the newest.
    matched = [
        event
        for event in task_store.get_task_events(repo, task_id)
        if event["event"] in events
    ]
    assert matched, f"no {events} event for {task_id}"
    return json.loads(matched[0]["payload"])


def _archive_payload(repo: Path, task_id: str) -> dict:
    return _newest_event_payload(repo, task_id, ("archived", "superseded"))


def _restored_payload(repo: Path, task_id: str) -> dict:
    return _newest_event_payload(repo, task_id, ("restored",))

def test_archive_records_the_preimage_in_the_same_transaction(tmp_path: Path) -> None:
    """The archive event carries the columns the archive is about to overwrite."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_PREIMAGE", status="review", worker_status="review")

    assert task_store.archive_task(repo, "TASK_PREIMAGE", actor="codex") == (True, "archived")

    assert _archive_payload(repo, "TASK_PREIMAGE")["preimage"] == {
        "status": "review",
        "worker_status": "review",
    }


def test_restore_round_trips_status_and_worker_status(tmp_path: Path) -> None:
    """Archive then restore returns the row to the columns it started with.

    Reversing ``archived_at`` alone was the defect: it left ``status='archived'``
    on a row that is no longer archived.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_ROUNDTRIP", status="pending", worker_status="unclaimed")
    before = _row(repo, "TASK_ROUNDTRIP")

    assert task_store.archive_task(repo, "TASK_ROUNDTRIP", actor="codex") == (True, "archived")
    assert _row(repo, "TASK_ROUNDTRIP")["status"] == "archived"

    ok, state = task_store.restore_task(repo, "TASK_ROUNDTRIP", actor="dashboard")
    assert (ok, state) == (True, "restored")

    after = _row(repo, "TASK_ROUNDTRIP")
    assert after == before
    assert after["archived_at"] == ""
    assert after["status"] == "pending"
    assert after["worker_status"] == "unclaimed"

    payload = _restored_payload(repo, "TASK_ROUNDTRIP")
    assert payload["preimage_source"] == "recorded_preimage"
    assert payload["restored_status"] == "pending"
    assert payload["prior_status"] == "archived"

    # The card's own status agrees with the column it mirrors.
    card = task_store.get_task(repo, "TASK_ROUNDTRIP")
    assert card["status"] == "pending"
    assert "archived_at" not in json.loads(_card_json(repo, "TASK_ROUNDTRIP"))


def test_restore_leaves_no_row_the_inverse_scan_can_find(tmp_path: Path) -> None:
    """The regression itself: after a restore neither scan direction fires."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_CLEAN", status="review", worker_status="review")

    task_store.archive_task(repo, "TASK_CLEAN", actor="codex")
    task_store.restore_task(repo, "TASK_CLEAN", actor="dashboard")

    assert task_store.find_archive_inconsistencies(repo) == []


def test_restore_of_a_pre_fix_row_derives_status_and_says_so(tmp_path: Path) -> None:
    """A row archived before the preimage existed is restored truthfully.

    Its prior ``status`` was never recorded, so it is not invented: the raw
    column is set to this module's own projection of the un-archived row, and
    the event labels the weaker source.  ``worker_status`` needs no preimage --
    ``archive_task`` never overwrote it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_LEGACY", status="review", worker_status="done")
    task_store.archive_task(repo, "TASK_LEGACY", actor="codex")

    # Rewrite history to the pre-fix shape: an archive event with no preimage.
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE task_events SET payload_json='{\"reason\": \"\"}' "
            "WHERE task_id=? AND event='archived'",
            ("TASK_LEGACY",),
        )
        conn.commit()
    finally:
        conn.close()
    assert "preimage" not in _archive_payload(repo, "TASK_LEGACY")

    ok, state = task_store.restore_task(repo, "TASK_LEGACY", actor="dashboard")
    assert (ok, state) == (True, "restored")

    row = _row(repo, "TASK_LEGACY")
    assert row["archived_at"] == ""
    # worker_status='done' projects as 'finished'; the column now agrees with
    # the projection every surface already reports, instead of contradicting it.
    assert row["status"] == "finished"
    assert row["worker_status"] == "done"
    assert task_store.canonical_status(row) == "finished"

    payload = _restored_payload(repo, "TASK_LEGACY")
    assert payload["preimage_source"] == "derived_canonical"

    # And the derived row is not itself drift.
    assert task_store.find_archive_inconsistencies(repo) == []


def test_restore_is_guarded_and_reports_what_is_stored(tmp_path: Path) -> None:
    """A concurrent writer that moves the row underneath the restore wins."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_CONFLICT", status="pending", worker_status="unclaimed")
    task_store.archive_task(repo, "TASK_CONFLICT", actor="codex")

    real_connect = task_store._connect

    class _MovesRowFirst:
        """Changes ``status`` after the preimage is read, before the UPDATE."""

        def __init__(self, inner):
            self._inner = inner
            self._moved = False

        def execute(self, sql: str, *args):
            if sql.lstrip().startswith("UPDATE tasks SET archived_at=''") and not self._moved:
                self._moved = True
                self._inner.execute(
                    "UPDATE tasks SET status='superseded' WHERE task_id='TASK_CONFLICT'"
                )
            return self._inner.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def fake_connect(path, *, readonly: bool = False, **kwargs):
        conn = real_connect(path, readonly=readonly, **kwargs)
        return conn if readonly else _MovesRowFirst(conn)

    original = task_store._connect
    task_store._connect = fake_connect
    try:
        ok, state = task_store.restore_task(repo, "TASK_CONFLICT", actor="dashboard")
    finally:
        task_store._connect = original

    assert (ok, state) == (False, "restore_write_conflict")
    # The guard refused, so the row is still exactly archived -- never half-done.
    row = _row(repo, "TASK_CONFLICT")
    assert row["archived_at"] != ""


# --- the inverse scan makes the leaked class measurable ---------------------


def test_inverse_scan_finds_half_restored_rows(tmp_path: Path) -> None:
    """``status`` archive-written while ``archived_at`` is empty is now detected."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_STUCK_ARCHIVED", status="review", worker_status="done")
    task_store.archive_task(repo, "TASK_STUCK_ARCHIVED", actor="codex")

    # Reproduce exactly what the old restore_task left behind: archived_at
    # cleared, status column still 'archived'.
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET archived_at='' WHERE task_id=?", ("TASK_STUCK_ARCHIVED",)
        )
        conn.commit()
    finally:
        conn.close()

    # Every surface says the card is fine ...
    assert task_store.canonical_status(_row(repo, "TASK_STUCK_ARCHIVED")) == "finished"

    # ... and the scan now names it anyway.
    detected = task_store.find_archive_inconsistencies(repo)
    assert [(item["task_id"], item["kind"]) for item in detected] == [
        ("TASK_STUCK_ARCHIVED", "half_restored")
    ]
    assert detected[0]["has_archive_event"] is True
    assert detected[0]["status"] == "archived"
    assert detected[0]["worker_status"] == "done"


def test_inverse_scan_ignores_ordinary_finished_and_open_rows(tmp_path: Path) -> None:
    """The scan keys on the two archive-WRITTEN statuses, not every terminal one.

    ``_ARCHIVE_TERMINAL_STATUSES`` also contains ``finished``; keying the
    inverse direction on that set would flag every completed card in the
    repository as drift.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "DONE_ROW", status="finished", worker_status="done")
    _insert_task(repo, "COMPLETED_ROW", status="completed", worker_status="done")
    _insert_task(repo, "OPEN_ROW", status="pending", worker_status="unclaimed")
    _insert_task(repo, "REVIEW_ROW", status="review", worker_status="review")

    assert task_store.find_archive_inconsistencies(repo) == []


def test_reviewer_child_superseded_rows_are_separated_by_the_archive_event(
    tmp_path: Path,
) -> None:
    """The discriminator that keeps the real class from drowning.

    ``reviewer_child_superseded`` writes ``status='superseded'`` with no
    ``archived_at`` and no archive event by design.  It matches the inverse
    predicate but is not a half-undone archive, and ``has_archive_event``
    separates the two without a second scan.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "REVIEWER_CHILD", status="superseded", worker_status="superseded")
    _insert_task(repo, "REAL_LEAK", status="review", worker_status="done")
    task_store.archive_task(repo, "REAL_LEAK", actor="codex")
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE tasks SET archived_at='' WHERE task_id=?", ("REAL_LEAK",))
        conn.commit()
    finally:
        conn.close()

    detected = task_store.find_archive_inconsistencies(repo)
    assert {item["task_id"] for item in detected} == {"REVIEWER_CHILD", "REAL_LEAK"}
    by_id = {item["task_id"]: item for item in detected}
    assert by_id["REAL_LEAK"]["has_archive_event"] is True
    assert by_id["REVIEWER_CHILD"]["has_archive_event"] is False


def test_repair_never_touches_the_inverse_class_but_names_it(tmp_path: Path) -> None:
    """The repair knows how to finish an archive, not how to undo one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "HALF_RESTORED", status="review", worker_status="done")
    task_store.archive_task(repo, "HALF_RESTORED", actor="codex")
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE tasks SET archived_at='' WHERE task_id=?", ("HALF_RESTORED",))
        conn.commit()
    finally:
        conn.close()

    result = task_store.repair_archive_inconsistencies(repo, actor="coordinator")

    assert result["repaired"] == []
    assert result["skipped_conflict"] == []
    assert result["count"] == 0
    assert result["observed_half_restored"] == ["HALF_RESTORED"]
    # Untouched.
    assert _row(repo, "HALF_RESTORED")["status"] == "archived"
