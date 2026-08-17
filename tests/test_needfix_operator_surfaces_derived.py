"""The three operator surfaces read the DERIVED NeedFix set through the real doors.

These tests drive the exact entry points production uses -- ``core.needfix_list``,
``core.needfix_count`` and ``dashboard._build_needfix_snapshot`` -- with NO hooks
passed by hand, against a real canonical task store. The pre-existing tests passed
the task-store hooks directly and so never caught that these three surfaces still
called the raw underived store functions; a rejected record resurfaced and a
landed one read as open. These go through the same door an operator does:
``core.needfix_list``/``count`` via the canonical ``AIWORKHUB_REPO_ROOT`` binding,
and the dashboard snapshot via its repository root.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from aiworkhub import core, dashboard, needfix_store, task_store


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> Path:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Drive core.repo_root() through the canonical env binding exactly as the
        # MCP process does -- no monkeypatching of core internals or hook passing.
        monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
        monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
        yield root


def _insert_task(repo: Path, task_id: str, status: str) -> None:
    db = task_store.canonical_db_path(repo)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, status, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _link(repo: Path, title: str, task_id: str) -> str:
    """Capture a NeedFix and point its converted_task_id at ``task_id``."""
    rec = needfix_store.capture_proposal(repo, title=title, description=title)
    conn = sqlite3.connect(str(needfix_store._db_path(repo)))
    try:
        conn.execute(
            "UPDATE needfix SET converted_task_id = ?, status = 'task_created' "
            "WHERE id = ?",
            (task_id, rec["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return rec["id"]


class TestOperatorSurfacesDeriveByDefault:
    """A landed (finished-card) record is hidden on every operator-facing path."""

    def test_core_needfix_list_hides_a_finished_linked_record(self, repo: Path):
        task_store.initialize_repository(repo)
        _insert_task(repo, "T-fin", "finished")
        landed = _link(repo, "already-landed", "T-fin")
        live = needfix_store.capture_proposal(
            repo, title="live-problem", description="still open"
        )["id"]

        rows = core.needfix_list()

        ids = {row["id"] for row in rows}
        assert landed not in ids  # the landed record does NOT resurface
        assert live in ids
        # Derivation ran through the real door without anyone passing hooks.
        assert rows.derived is True
        assert rows.underived_reason is None

    def test_core_needfix_count_agrees_with_the_list(self, repo: Path):
        task_store.initialize_repository(repo)
        _insert_task(repo, "T-fin", "finished")
        _link(repo, "already-landed", "T-fin")
        needfix_store.capture_proposal(repo, title="live-problem", description="open")

        rows = core.needfix_list()
        count = core.needfix_count()

        assert count.derived is True
        assert count.underived_reason is None
        # The count and the list describe the same derived active set.
        assert count == len(rows) == 1

    def test_dashboard_snapshot_hides_finished_and_states_derived(self, repo: Path):
        task_store.initialize_repository(repo)
        _insert_task(repo, "T-fin", "finished")
        landed = _link(repo, "already-landed", "T-fin")
        needfix_store.capture_proposal(repo, title="live-problem", description="open")

        snapshot = dashboard._build_needfix_snapshot(repo)

        assert snapshot["available"] is True
        assert snapshot["derived"] is True
        assert snapshot["underived_reason"] is None
        ids = {item["id"] for item in snapshot["items"]}
        assert landed not in ids
        assert snapshot["total"] == 1
        assert snapshot["open"] == 1


class TestOperatorSurfacesMarkUnderived:
    """No ready task store -> the caller is TOLD, not shown stale rows as truth."""

    def test_core_list_and_count_are_marked_underived_without_task_store(
        self, repo: Path
    ):
        # NeedFix store only; no canonical task store to derive linked-card state.
        needfix_store.initialize_repository(repo)
        needfix_store.capture_proposal(repo, title="orphan", description="orphan")

        rows = core.needfix_list()
        assert rows.derived is False
        assert rows.underived_reason  # bounded reason, not silence
        assert len(rows) == 1  # raw rows still surfaced, not swallowed

        count = core.needfix_count()
        assert count.derived is False
        assert count.underived_reason

    def test_dashboard_snapshot_is_marked_underived_without_task_store(
        self, repo: Path
    ):
        needfix_store.initialize_repository(repo)
        needfix_store.capture_proposal(repo, title="orphan", description="orphan")

        snapshot = dashboard._build_needfix_snapshot(repo)
        assert snapshot["available"] is True
        assert snapshot["derived"] is False
        assert snapshot["underived_reason"]
        # The raw rows are still shown so the operator is not left blind.
        assert snapshot["total"] == 1
