"""Reconcile the 989 rows the pre-NF-276 two-write archive left half-applied.

These rows carry a set ``archived_at`` (the archive provably happened -- the
audit trail holds the ``archived`` event) while their raw ``status`` column is
still non-terminal.  ``repair_archive_inconsistencies`` completes the archive
transition once, transactionally, under a preimage guard with a post-commit
re-read, and refuses to touch three protected classes:

* a row whose ``archived_at`` was set with no corroborating archive event
  (some non-archive path wrote it -- never assumed archived);
* a row a live process is still working on;
* a row whose worktree another active card pins as a rework predecessor.

The suite also proves the NF-276 prevention still holds, the repair is
idempotent, empty-``archived_at`` rows are never touched, and the read-only
report surfaces the three numbers -- non-terminal count, collision-guard holder
counts and pinned-worktree bytes -- before and after.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_store  # noqa: E402

_NOW = "2026-07-22T00:00:00+00:00"


def _insert(
    repo: Path,
    task_id: str,
    *,
    status: str = "pending",
    worker_status: str = "unclaimed",
    allowed_writes: list[str] | None = None,
    archived_at: str = "",
    request_id: str = "",
    rework_request_id: str = "",
    with_archive_event: bool = False,
    archive_event: str = "archived",
    card_extra: dict | None = None,
) -> None:
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    card: dict = {
        "task_id": task_id,
        "runner": "codex_worker_b891",
        "topic": "task_lifecycle",
        "allowed_writes": ["out.txt"] if allowed_writes is None else allowed_writes,
    }
    if request_id:
        card["request_id"] = request_id
    if rework_request_id:
        card["rework_predecessor"] = {"request_id": rework_request_id, "workspace": "wt"}
    if archive_event == "superseded" and with_archive_event:
        card["archive_operation"] = "superseded"
    if card_extra:
        card.update(card_extra)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, "
            "started_at, archived_at) "
            "VALUES (?, ?, 'task_lifecycle', ?, ?, '', '', ?, ?, ?, '', '', '', ?)",
            (task_id, "codex_worker_b891", status, worker_status, json.dumps(card),
             _NOW, _NOW, archived_at),
        )
        if with_archive_event:
            conn.execute(
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                "VALUES (?, ?, 'codex', '{}', ?)",
                (task_id, archive_event, _NOW),
            )
        conn.commit()
    finally:
        conn.close()


def _half_archived(repo: Path, task_id: str, **kwargs) -> None:
    """A genuine half-archived row: archived_at set, non-terminal status, and
    the archive audit event present."""
    kwargs.setdefault("status", "review")
    kwargs.setdefault("archived_at", "2026-07-27T10:29:22Z")
    kwargs.setdefault("with_archive_event", True)
    _insert(repo, task_id, **kwargs)


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


def _events(repo: Path, task_id: str) -> list[dict]:
    return task_store.get_task_events(repo, task_id)


class _ConnProxy:
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


def _patch_write_connection(monkeypatch, proxy_cls) -> None:
    real_connect = task_store._connect

    def fake_connect(path, *, readonly: bool = False):
        conn = real_connect(path, readonly=readonly)
        if readonly:
            return conn
        return proxy_cls(conn)

    monkeypatch.setattr(task_store, "_connect", fake_connect)


# --- reconciliation of genuine half-archived rows ---------------------------


def test_genuine_half_archived_row_is_reconciled_with_audit_and_reread(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _half_archived(repo, "ROW_REVIEW", status="review")

    result = task_store.repair_archive_inconsistencies(repo, actor="coordinator")

    assert result["repaired"] == ["ROW_REVIEW"]
    assert result["verified"] == ["ROW_REVIEW"]
    assert result["reverted"] == []
    assert result["skipped_conflict"] == []
    assert _row(repo, "ROW_REVIEW")["status"] == "archived"
    assert task_store.find_archive_inconsistencies(repo) == []

    repaired_events = [e for e in _events(repo, "ROW_REVIEW")
                       if e["event"] == "archive_inconsistency_repaired"]
    assert len(repaired_events) == 1
    payload = json.loads(repaired_events[0]["payload"])
    assert payload["task_id"] == "ROW_REVIEW"
    assert payload["from_status"] == "review"
    assert payload["to_status"] == "archived"
    assert payload["archived_at"] == "2026-07-27T10:29:22Z"
    assert payload["evidence"] == "archive_audit_event_present"


def test_superseded_archive_reconciles_to_superseded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _half_archived(repo, "ROW_SUPERSEDED", status="pending",
                   with_archive_event=True, archive_event="superseded")

    result = task_store.repair_archive_inconsistencies(repo)
    assert result["repaired"] == ["ROW_SUPERSEDED"]
    assert _row(repo, "ROW_SUPERSEDED")["status"] == "superseded"


def test_repair_is_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _half_archived(repo, "ROW_IDEMPOTENT", status="review")

    first = task_store.repair_archive_inconsistencies(repo)
    assert first["repaired"] == ["ROW_IDEMPOTENT"]

    second = task_store.repair_archive_inconsistencies(repo)
    assert second["repaired"] == []
    assert second["reconcilable"] == []
    assert second["count"] == 0
    # No second repair event was written.
    repaired_events = [e for e in _events(repo, "ROW_IDEMPOTENT")
                       if e["event"] == "archive_inconsistency_repaired"]
    assert len(repaired_events) == 1


# --- rows that must never be touched ----------------------------------------


def test_empty_archived_at_row_is_never_touched(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert(repo, "OPEN_BLOCKED", status="blocked", archived_at="")
    _insert(repo, "OPEN_PENDING", status="pending", archived_at="")
    _half_archived(repo, "ARCHIVED_ROW", status="review")

    report_before = task_store.archive_inconsistency_report(repo)
    assert report_before["genuinely_open_count"] == 2

    result = task_store.repair_archive_inconsistencies(repo)
    assert result["repaired"] == ["ARCHIVED_ROW"]

    assert _row(repo, "OPEN_BLOCKED")["status"] == "blocked"
    assert _row(repo, "OPEN_PENDING")["status"] == "pending"
    assert _row(repo, "OPEN_BLOCKED")["archived_at"] == ""
    # The genuinely-open rows survive unchanged.
    assert result["metrics_after"]["genuinely_open_count"] == 2


def test_archived_at_without_archive_event_is_excluded_and_listed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    # archived_at set by some non-archive path: no archive event in the trail.
    _insert(repo, "NO_EVENT_ROW", status="review", archived_at="2026-07-27T10:29:22Z",
            with_archive_event=False)

    detected = task_store.find_archive_inconsistencies(repo)
    assert detected[0]["task_id"] == "NO_EVENT_ROW"
    assert detected[0]["has_archive_event"] is False

    result = task_store.repair_archive_inconsistencies(repo)
    assert result["repaired"] == []
    assert result["excluded_no_archive_event"] == ["NO_EVENT_ROW"]
    # Untouched: the raw status column still disagrees, and the report still
    # lists it so an operator can decide what that non-archive write meant.
    assert _row(repo, "NO_EVENT_ROW")["status"] == "review"
    assert task_store.archive_inconsistency_report(repo)["count"] == 1


def test_live_process_row_is_excluded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _half_archived(repo, "LIVE_ROW", status="processing", worker_status="in_progress")
    _half_archived(repo, "IDLE_ROW", status="review")

    def is_live(task_id: str) -> bool:
        return task_id == "LIVE_ROW"

    result = task_store.repair_archive_inconsistencies(repo, is_task_live=is_live)
    assert result["excluded_live"] == ["LIVE_ROW"]
    assert result["repaired"] == ["IDLE_ROW"]
    assert _row(repo, "LIVE_ROW")["status"] == "processing"
    assert _row(repo, "IDLE_ROW")["status"] == "archived"


def test_rework_predecessor_pinned_row_is_excluded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    # PINNED is a half-archived predecessor; an active rework card pins it.
    _half_archived(repo, "PINNED_PREDECESSOR", status="blocked", request_id="req-pred-1")
    _insert(repo, "ACTIVE_REWORK", status="processing", worker_status="in_progress",
            rework_request_id="req-pred-1")
    _half_archived(repo, "UNPINNED", status="review", request_id="req-other")

    result = task_store.repair_archive_inconsistencies(repo)
    assert result["excluded_rework_pinned"] == ["PINNED_PREDECESSOR"]
    assert "UNPINNED" in result["repaired"]
    assert _row(repo, "PINNED_PREDECESSOR")["status"] == "blocked"
    assert _row(repo, "UNPINNED")["status"] == "archived"


# --- transactional safety ----------------------------------------------------


def test_row_changed_underneath_is_skipped_not_overwritten(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _half_archived(repo, "RACED_ROW", status="pending")

    class _RaceOnReconcile(_ConnProxy):
        raced = False

        def execute(self, sql: str, *args: object):
            stripped = sql.lstrip()
            if stripped.startswith("UPDATE tasks SET status=?") and not _RaceOnReconcile.raced:
                _RaceOnReconcile.raced = True
                # A concurrent writer moves the row out from under the preimage
                # guard before our guarded UPDATE runs.
                self._inner.execute(
                    "UPDATE tasks SET status='blocked' WHERE task_id='RACED_ROW'"
                )
            return self._inner.execute(sql, *args)

    _patch_write_connection(monkeypatch, _RaceOnReconcile)

    result = task_store.repair_archive_inconsistencies(repo)
    assert result["skipped_conflict"] == ["RACED_ROW"]
    assert result["repaired"] == []

    monkeypatch.undo()
    # Never overwritten to 'archived'; the concurrent value stands.
    assert _row(repo, "RACED_ROW")["status"] == "blocked"
    # No repair event was written for a skipped row.
    assert [e for e in _events(repo, "RACED_ROW")
            if e["event"] == "archive_inconsistency_repaired"] == []


# --- NF-276 prevention still holds ------------------------------------------


def test_nf276_failure_between_two_writes_cannot_half_archive(tmp_path: Path, monkeypatch) -> None:
    """A failure between the archive's two writes must leave the row wholly
    unarchived -- never ``archived_at`` set with a non-terminal status."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert(repo, "ATOMIC_ROW", status="pending")

    class _EventInsertFails(_ConnProxy):
        def execute(self, sql: str, *args: object):
            if sql.lstrip().startswith("INSERT INTO task_events"):
                raise sqlite3.OperationalError("injected failure between writes")
            return self._inner.execute(sql, *args)

    _patch_write_connection(monkeypatch, _EventInsertFails)
    ok, state = task_store.archive_task(repo, "ATOMIC_ROW", actor="codex")
    assert ok is False
    assert state.startswith("archive_write_failed")

    monkeypatch.undo()
    row = _row(repo, "ATOMIC_ROW")
    assert row["archived_at"] == ""
    assert row["status"] == "pending"
    # The invariant the repair depends on: no half-archived row was created.
    assert task_store.find_archive_inconsistencies(repo) == []


# --- operator-reachable report and the three numbers ------------------------


def test_report_lists_disagreeing_rows_with_count(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _half_archived(repo, "DIS_A", status="review")
    _half_archived(repo, "DIS_B", status="pending")
    _insert(repo, "OPEN", status="blocked", archived_at="")

    report = task_store.archive_inconsistency_report(repo)
    assert report["count"] == 2
    assert {r["task_id"] for r in report["rows"]} == {"DIS_A", "DIS_B"}
    assert report["by_status"] == {"review": 1, "pending": 1}
    assert report["genuinely_open_count"] == 1


def test_report_numbers_move_before_and_after_repair(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    core = "src/aiworkhub/core.py"
    ww = "src/aiworkhub/worker_workspace.py"
    # A genuinely-active card holding core.py (must remain a holder).
    _insert(repo, "REAL_ACTIVE", status="review", allowed_writes=[core])
    # Phantom half-archived holders of core.py and worker_workspace.py.
    _half_archived(repo, "PHANTOM_CORE", status="review", allowed_writes=[core])
    _half_archived(repo, "PHANTOM_WW", status="pending", allowed_writes=[ww])
    # A genuinely-open row that must survive untouched.
    _insert(repo, "OPEN_ONE", status="blocked", archived_at="")

    result = task_store.repair_archive_inconsistencies(repo)
    before = result["metrics_before"]
    after = result["metrics_after"]

    assert before["half_archived_count"] == 2
    assert after["half_archived_count"] == 0

    assert before["collision_holders"][core] == 2  # real + phantom
    assert after["collision_holders"][core] == 1   # phantom reconciled away
    assert before["collision_holders"][ww] == 1
    assert after["collision_holders"][ww] == 0

    assert before["pinned_worktree_bytes"] > 0
    assert after["pinned_worktree_bytes"] == 0

    # The genuinely-open count is the safety invariant: unchanged (the active
    # review card and the open blocked row, neither of them archived).
    assert before["genuinely_open_count"] == after["genuinely_open_count"] == 2

    assert set(result["repaired"]) == {"PHANTOM_CORE", "PHANTOM_WW"}
    assert _row(repo, "REAL_ACTIVE")["status"] == "review"
    assert _row(repo, "OPEN_ONE")["status"] == "blocked"


def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _half_archived(repo, "DRY_ROW", status="review")

    result = task_store.repair_archive_inconsistencies(repo, dry_run=True)
    assert result["dry_run"] is True
    assert result["repaired"] == []
    assert result["reconcilable"] == ["DRY_ROW"]
    assert _row(repo, "DRY_ROW")["status"] == "review"
    assert task_store.find_archive_inconsistencies(repo) != []
