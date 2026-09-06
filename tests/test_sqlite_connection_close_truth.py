"""Truth test for the measured SQLite connection leak.

``sqlite3.Connection.__exit__`` commits or rolls back the open transaction; it
does NOT close the connection.  The nine ``with sqlite3.connect(...) as c:``
call sites in :mod:`aiworkhub.fresh_task_store` and
:mod:`aiworkhub.review_orchestrator` therefore leaked a live connection (with
its fd and page cache) past the block that appeared to scope it.

These tests inject a connection registry that only records a connection as
closed when ``.close()`` is actually called -- GC is deliberately kept out of
the picture by holding a strong reference to every opened connection.  On the
pre-fix code the registry keeps growing; wrapping each site in
``contextlib.closing`` drives it back to zero.
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

from aiworkhub import fresh_task_store, source_graph  # noqa: E402


class _ConnectionRegistry:
    """Track every opened connection and whether ``.close()`` was called."""

    def __init__(self) -> None:
        self._open: list[sqlite3.Connection] = []

    def install(
        self, monkeypatch, *, fail_execute_containing: str | None = None
    ) -> "_ConnectionRegistry":
        real_connect = sqlite3.connect
        registry = self

        class _TrackedConnection(sqlite3.Connection):
            def close(self) -> None:  # noqa: D401 - thin override
                registry._forget(self)
                super().close()

            def execute(self, sql, *args, **kwargs):
                if fail_execute_containing and fail_execute_containing in str(sql):
                    raise sqlite3.OperationalError(
                        f"simulated failure: {fail_execute_containing}"
                    )
                return super().execute(sql, *args, **kwargs)

        def _tracking_connect(*args, **kwargs):
            kwargs["factory"] = _TrackedConnection
            conn = real_connect(*args, **kwargs)
            registry._open.append(conn)  # strong ref: never GC'd out from under us
            return conn

        # fresh_task_store.sqlite3, review_orchestrator.sqlite3 and
        # source_graph.sqlite3 are the same module object, so patching
        # sqlite3.connect covers every call site.
        monkeypatch.setattr(sqlite3, "connect", _tracking_connect)
        return self

    def _forget(self, conn: sqlite3.Connection) -> None:
        try:
            self._open.remove(conn)
        except ValueError:
            pass

    def open_count(self) -> int:
        return len(self._open)


def _write_fresh_db(path: Path) -> None:
    """Build a valid empty canonical queue without going through the module."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(fresh_task_store._FRESH_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def test_repeated_reader_calls_do_not_grow_open_connections(monkeypatch, tmp_path):
    db = tmp_path / "queue.sqlite"
    _write_fresh_db(db)  # built before the registry is installed -> not counted

    registry = _ConnectionRegistry().install(monkeypatch)

    for _ in range(25):
        fresh_task_store.quick_check(db)
        fresh_task_store.schema_fingerprint(db)
        fresh_task_store.table_counts(db)
        fresh_task_store.empty_counts(db)

    leaked = registry.open_count()
    assert leaked == 0, f"{leaked} sqlite connection(s) left open by readers"


def test_sqlite_online_backup_leaves_no_open_connection(monkeypatch, tmp_path):
    source = tmp_path / "legacy.sqlite"
    _write_fresh_db(source)
    destination = tmp_path / "archive" / "legacy_archive.sqlite"

    registry = _ConnectionRegistry().install(monkeypatch)

    fresh_task_store.sqlite_online_backup(source, destination)

    # Both the source and the destination connection must be closed on return.
    leaked = registry.open_count()
    assert leaked == 0, f"{leaked} sqlite connection(s) left open after backup"
    assert destination.is_file()


def test_backup_output_is_a_valid_readable_copy(monkeypatch, tmp_path):
    source = tmp_path / "legacy.sqlite"
    _write_fresh_db(source)
    destination = tmp_path / "archive" / "legacy_archive.sqlite"

    fresh_task_store.sqlite_online_backup(source, destination)

    # No behaviour change: the archive is still a valid, quick-check-ok copy.
    assert fresh_task_store.quick_check(destination) == "ok"
    assert fresh_task_store.schema_fingerprint(destination) == (
        fresh_task_store.schema_fingerprint(source)
    )


def _seed_failed_build_staging(staging_path: Path, *, files_skipped: int = 3) -> None:
    """Populate a staging database with a ``last_build`` row worth persisting."""
    conn = source_graph.connect(staging_path)
    try:
        payload = json.dumps(
            {
                "finished_at": "2026-01-01T00:00:00+00:00",
                "build_revision": source_graph.BUILD_REVISION,
                "files_seen": 1,
                "files_skipped": files_skipped,
            }
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_build', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (payload,),
        )
        conn.commit()
    finally:
        conn.close()


def test_persist_failed_build_metadata_closes_both_connections_on_success_local(
    monkeypatch, tmp_path,
):
    """The staging read-only connection and canonical writable connection
    opened by ``_persist_failed_build_metadata`` must both be closed
    explicitly on the success path -- not left for garbage collection."""
    staging_path = tmp_path / "staging.sqlite"
    canonical_path = tmp_path / "canonical.sqlite"
    _seed_failed_build_staging(staging_path)

    registry = _ConnectionRegistry().install(monkeypatch)

    failure = source_graph.SourceGraphBuildFailedError("simulated_build_failure")
    source_graph._persist_failed_build_metadata(staging_path, canonical_path, failure)

    leaked = registry.open_count()
    assert leaked == 0, (
        f"{leaked} sqlite connection(s) left open by _persist_failed_build_metadata"
    )

    verify_conn = source_graph.connect(canonical_path, read_only=True)
    try:
        row = verify_conn.execute(
            "SELECT value FROM meta WHERE key='last_build'"
        ).fetchone()
    finally:
        verify_conn.close()
    assert row is not None
    stored = json.loads(str(row[0]))
    assert stored["status"] == "failed"
    assert "simulated_build_failure" in stored["failure_reason"]


def test_persist_failed_build_metadata_closes_connections_on_injected_exception_local(
    monkeypatch, tmp_path,
):
    """Even when the canonical write raises, both connections opened by
    ``_persist_failed_build_metadata`` must still be closed explicitly --
    proving the close is unconditional, not contingent on success."""
    staging_path = tmp_path / "staging.sqlite"
    canonical_path = tmp_path / "canonical.sqlite"
    _seed_failed_build_staging(staging_path)

    registry = _ConnectionRegistry().install(
        monkeypatch, fail_execute_containing="VALUES('last_build', ?)"
    )

    failure = source_graph.SourceGraphBuildFailedError("simulated_build_failure")
    with pytest.raises(sqlite3.OperationalError):
        source_graph._persist_failed_build_metadata(staging_path, canonical_path, failure)

    leaked = registry.open_count()
    assert leaked == 0, (
        f"{leaked} sqlite connection(s) left open after an injected exception in "
        "_persist_failed_build_metadata"
    )
