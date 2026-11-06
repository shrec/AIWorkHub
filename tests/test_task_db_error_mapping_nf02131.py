"""NF-2026-02131: a transient task-DB storage error must be named as one.

Measured (Linux field report, AIWorkHub 0.12.90):

1. ``task_store._schema_ok`` caught ``sqlite3.DatabaseError`` on the read-only
   open and returned False, so a ``disk I/O error`` became readiness reason
   ``canonical_schema_incomplete`` -- the exact string the extension maps to
   "storage upgrade required" and an Initialize button on a live DB.
2. ``core._live_card`` dropped ``show_task``'s stderr, so
   ``_check_card_scoped_write_authority`` reported ``card_scoped_task_unresolved``
   both for "no such card" and for "the task DB could not be read".
3. ``card_scoped_task_unresolved`` is deterministic, so the reconciler held the
   card until its row changed: a storage blip stranded it.
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

from aiworkhub import core, dependency_autolaunch, task_store  # noqa: E402

NOW = "2026-11-07T00:00:00+00:00"
RUNNER = "claude_opus-5"
TOPIC = "task_mcp"
FIELD_REASON = (
    "collision_guard_failed:launch_blocker_record_failed:runner/topic allowlist denied: "
    "card_scoped_task_lookup_failed:canonical_db_unavailable:SQLITE_IOERR"
)


class _DiskIoError(sqlite3.OperationalError):
    """An OperationalError carrying the name Python 3.11+ attaches."""

    sqlite_errorname = "SQLITE_IOERR"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    task_store.reset_storage_readiness_cache()
    yield root
    task_store.reset_storage_readiness_cache()


def _insert_task(root: Path, task_id: str) -> None:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    card_json = json.dumps({"runner": RUNNER, "topic": TOPIC}, ensure_ascii=False)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, RUNNER, TOPIC, "solo", "pending", "unclaimed", "normal",
             "objective", card_json, NOW, NOW, ""),
        )
        conn.commit()
    finally:
        conn.close()


def _raise_on_readonly_open(exc: BaseException):
    def _connect_readonly(*_args, **_kwargs):
        raise exc

    return _connect_readonly


# ---------------------------------------------------------------------------
# 1. Readiness: I/O error vs. a genuinely incomplete schema.
# ---------------------------------------------------------------------------


def test_readonly_open_io_error_is_canonical_db_unavailable(repo, monkeypatch):
    monkeypatch.setattr(
        task_store,
        "connect_readonly",
        _raise_on_readonly_open(sqlite3.OperationalError("disk I/O error")),
    )
    readiness = task_store.storage_readiness(repo)
    assert not readiness.ready
    assert readiness.reason.startswith("canonical_db_unavailable:"), readiness.reason
    assert readiness.reason != "canonical_schema_incomplete"
    assert "disk I/O error" in readiness.reason
    assert len(readiness.reason) <= len("canonical_db_unavailable:") + 120


def test_unavailable_detail_prefers_sqlite_errorname(repo, monkeypatch):
    monkeypatch.setattr(
        task_store, "connect_readonly", _raise_on_readonly_open(_DiskIoError("disk I/O error"))
    )
    readiness = task_store.storage_readiness(repo)
    assert readiness.reason == "canonical_db_unavailable:SQLITE_IOERR"


def test_unavailable_detail_is_truncated(repo, monkeypatch):
    monkeypatch.setattr(
        task_store,
        "connect_readonly",
        _raise_on_readonly_open(sqlite3.OperationalError("x" * 5000)),
    )
    readiness = task_store.storage_readiness(repo)
    assert readiness.reason.startswith("canonical_db_unavailable:")
    assert len(readiness.reason) == len("canonical_db_unavailable:") + 120


def test_unavailable_verdict_is_not_cached(repo, monkeypatch):
    assert task_store.storage_readiness(repo).ready
    task_store.reset_storage_readiness_cache()
    with monkeypatch.context() as patch:
        patch.setattr(
            task_store, "connect_readonly", _raise_on_readonly_open(_DiskIoError("disk I/O error"))
        )
        assert task_store.storage_readiness(repo).reason == "canonical_db_unavailable:SQLITE_IOERR"
    # Storage recovered: the very next call re-checks and is ready.
    assert task_store.storage_readiness(repo).ready


def test_db_missing_a_required_table_is_still_schema_incomplete(repo):
    readiness = task_store.storage_readiness(repo)
    assert readiness.ready, readiness.reason
    dropped = task_store.REQUIRED_TABLES[-1]
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(f"DROP TABLE {dropped}")
        conn.commit()
    finally:
        conn.close()
    task_store.reset_storage_readiness_cache()
    assert task_store.storage_readiness(repo).reason == "canonical_schema_incomplete"


# ---------------------------------------------------------------------------
# 2. Card-scoped authority: read failure vs. absent card.
# ---------------------------------------------------------------------------


def test_show_task_failure_is_card_scoped_task_lookup_failed(monkeypatch):
    stderr = "canonical_db_unavailable:SQLITE_IOERR"

    def failing_show_task(task_id, *, full=True):
        return {"ok": False, "returncode": 1, "command": ["show", task_id], "stdout": "", "stderr": stderr}

    monkeypatch.setattr(core, "show_task", failing_show_task)
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF02131_A"], RUNNER, TOPIC
    )
    assert decision["allowed"] is False
    assert decision["reason"] == f"card_scoped_task_lookup_failed:{stderr}"
    assert decision["detail"] == stderr


def test_show_task_stderr_is_bounded(monkeypatch):
    def failing_show_task(task_id, *, full=True):
        return {"ok": False, "returncode": 1, "command": [], "stdout": "", "stderr": "e" * 5000}

    monkeypatch.setattr(core, "show_task", failing_show_task)
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF02131_B"], RUNNER, TOPIC
    )
    assert decision["reason"].startswith("card_scoped_task_lookup_failed:")
    assert len(decision["detail"]) <= 240


def test_live_card_error_keeps_show_task_stderr_and_exit(monkeypatch):
    def failing_show_task(task_id, *, full=True):
        return {"ok": False, "returncode": 3, "command": [], "stdout": "", "stderr": "database is locked"}

    monkeypatch.setattr(core, "show_task", failing_show_task)
    card, error = core._live_card("TASK_NF02131_C")
    assert card is None
    assert error is not None
    assert error["returncode"] == 3
    assert "database is locked" in error["stderr"]


def test_real_storage_error_reaches_the_authority_reason(repo, monkeypatch):
    _insert_task(repo, "TASK_NF02131_D")
    task_store.reset_storage_readiness_cache()
    monkeypatch.setattr(
        task_store, "connect_readonly", _raise_on_readonly_open(_DiskIoError("disk I/O error"))
    )
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF02131_D"], RUNNER, TOPIC
    )
    assert decision["allowed"] is False
    assert decision["reason"].startswith("card_scoped_task_lookup_failed:")
    assert "canonical_db_unavailable:SQLITE_IOERR" in decision["reason"]


def test_missing_card_is_still_card_scoped_task_unresolved(repo):
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF02131_NEVER_INSERTED"], RUNNER, TOPIC
    )
    assert decision == {"allowed": False, "reason": "card_scoped_task_unresolved"}


def test_present_card_still_resolves(repo):
    _insert_task(repo, "TASK_NF02131_E")
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF02131_E"], RUNNER, TOPIC
    )
    assert decision == {"allowed": True, "reason": "card_scoped_launch_blocker_allowed"}


# ---------------------------------------------------------------------------
# 3. Reconciler classification: storage failures back off, never hold.
# ---------------------------------------------------------------------------


def test_field_reason_with_lookup_failure_is_transient():
    assert dependency_autolaunch.classify_denial(
        "collision_guard_failed:launch_blocker_record_failed:runner/topic allowlist denied: "
        "card_scoped_task_lookup_failed:..."
    ) == "transient"
    assert dependency_autolaunch.classify_denial(FIELD_REASON) == "transient"


def test_storage_reasons_are_disclaimed_transient():
    for reason in ("card_scoped_task_lookup_failed", "canonical_db_unavailable"):
        assert reason in dependency_autolaunch.TRANSIENT_DENIAL_REASONS
        assert reason not in dependency_autolaunch.DETERMINISTIC_DENIAL_REASONS
        assert dependency_autolaunch.classify_denial(reason) == "transient"


def test_absent_card_reason_stays_deterministic():
    assert dependency_autolaunch.classify_denial(
        "runner/topic allowlist denied: card_scoped_task_unresolved"
    ) == "deterministic"
