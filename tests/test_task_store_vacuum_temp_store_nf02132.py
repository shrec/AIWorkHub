"""NF-2026-02132: the one-time incremental VACUUM builds its copy on disk.

``PRAGMA temp_store=MEMORY`` on every task-store connection makes VACUUM build
a full temporary copy of the database in process RAM. The conversion must
switch the connection to file-backed temp storage before VACUUM runs.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from aiworkhub import task_store
from aiworkhub import task_store_compaction as compaction


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _auto_vacuum(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
    finally:
        conn.close()


def test_incremental_vacuum_switches_temp_store_to_file_before_vacuum(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo = _repo(tmp_path)
    db = task_store.canonical_db_path(repo)
    assert _auto_vacuum(db) != 2

    executed: list[str] = []
    original = task_store._write_connection

    @contextlib.contextmanager
    def traced(*args: Any, **kwargs: Any) -> Iterator[sqlite3.Connection]:
        with original(*args, **kwargs) as conn:
            conn.set_trace_callback(executed.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(task_store, "_write_connection", traced)

    result = compaction._ensure_incremental_vacuum(db)

    assert result["converted"] is True
    assert result["auto_vacuum"] == "incremental"
    assert _auto_vacuum(db) == 2

    statements = ["".join(sql.split()).upper() for sql in executed]
    temp_store = [i for i, sql in enumerate(statements) if sql == "PRAGMATEMP_STORE=FILE"]
    vacuum = [i for i, sql in enumerate(statements) if sql == "VACUUM"]
    assert temp_store, statements
    assert vacuum, statements
    assert temp_store[0] < vacuum[0], statements
