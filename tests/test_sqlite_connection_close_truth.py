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

import sqlite3
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import fresh_task_store  # noqa: E402


class _ConnectionRegistry:
    """Track every opened connection and whether ``.close()`` was called."""

    def __init__(self) -> None:
        self._open: list[sqlite3.Connection] = []

    def install(self, monkeypatch) -> "_ConnectionRegistry":
        real_connect = sqlite3.connect
        registry = self

        class _TrackedConnection(sqlite3.Connection):
            def close(self) -> None:  # noqa: D401 - thin override
                registry._forget(self)
                super().close()

        def _tracking_connect(*args, **kwargs):
            kwargs["factory"] = _TrackedConnection
            conn = real_connect(*args, **kwargs)
            registry._open.append(conn)  # strong ref: never GC'd out from under us
            return conn

        # fresh_task_store.sqlite3 and review_orchestrator.sqlite3 are the same
        # module object, so patching sqlite3.connect covers every call site.
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
