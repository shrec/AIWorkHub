"""NF-2026-00261: read-only SQLite URIs must be built with proper escaping.

Building a read-only URI by raw f-string -- ``f"file:{path}?mode=ro"`` -- is
not a read-only open: SQLite treats ``#`` as a fragment delimiter, so a
repository path containing ``#`` swallows the whole ``?mode=ro`` query. The
database then opens READ-WRITE with create-if-missing and targets a DIFFERENT
file than the caller named. The canonical ``sqlite_readonly.connect_readonly``
helper percent-encodes the path (``#`` -> ``%23``) and issues ``PRAGMA
query_only=ON``.

The in-scope production read-only opens (dashboard_mcp_app.py) route through
that helper. Out-of-scope sites are named in the worker report.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import sqlite_readonly  # noqa: E402


def _make_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
    finally:
        conn.close()


def test_connect_readonly_survives_hash_in_repository_path(tmp_path):
    db = tmp_path / "repo#123" / "data.sqlite"
    _make_db(db)

    conn = sqlite_readonly.connect_readonly(db)
    try:
        # The '#' was percent-encoded, so the query survived and the open
        # targeted the exact file the caller named.
        assert conn.execute("SELECT x FROM t").fetchone()[0] == 42
        # ... and it is genuinely read-only (fail-closed second guarantee).
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (1)")
    finally:
        conn.close()


def test_raw_fstring_uri_is_broken_by_hash_the_helper_avoids(tmp_path):
    """Documents the exact defect the helper fixes: a raw f-string URI with a
    '#' in the path loses ``?mode=ro`` and opens read-write against the wrong
    file."""
    db = tmp_path / "repo#456" / "data.sqlite"
    _make_db(db)

    raw = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        # A true mode=ro open would raise here; instead the '#'-truncated URI
        # opened a different, writable database, so the write succeeds.
        raw.execute("CREATE TABLE broken(x)")
        raw.commit()
    finally:
        raw.close()
    # The caller's real database was never touched by that write.
    verify = sqlite_readonly.connect_readonly(db)
    try:
        tables = {
            row[0]
            for row in verify.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        verify.close()
    assert "broken" not in tables
    assert "t" in tables


def test_dashboard_readonly_opens_use_canonical_helper():
    src = (SRC / "aiworkhub" / "dashboard_mcp_app.py").read_text(encoding="utf-8")
    # Every in-scope read-only open was migrated to the canonical helper...
    assert src.count("sqlite_readonly.connect_readonly(") == 3
    # ... and no bare f-string read-only URI construction remains.
    assert '.as_uri()}?mode=ro"' not in src
