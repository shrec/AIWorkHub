"""NF-2026-02130: a read-only open must never drop this process's SQLite locks.

POSIX fcntl locks belong to the (process, inode) pair: closing ANY descriptor
to a file releases every lock the process holds on it. SQLite's unix VFS only
protects its own descriptors (it defers their close while a lock is held).
``connect_readonly`` used to read the database header with a plain ``open()``
on every call; that ``close()`` silently dropped the SHARED lock every live
SQLite connection in the same process held on the database. A peer process
that then opened and closed a connection believed it was the last one, took
EXCLUSIVE, checkpointed and DELETED ``-wal``/``-shm`` under the still-open
connections: ``disk I/O error``, or writes into an unlinked WAL nobody else
can see. Windows locks per handle, which is why it never reproduced there.

Two tests pin the fix:

1. A spy (every OS): with ``builtins.open``, ``io.open`` and ``os.open``
   wrapped, ``connect_readonly`` on a live WAL database -- and the failure-path
   sidecar classification -- open none of the database, ``-wal`` or ``-shm``.
2. The reported reproduction (POSIX only): a holder connection in WAL with a
   committed row, ``connect_readonly`` + read + close in the same process, a
   peer process opens/reads/closes, and then ``-wal``/``-shm`` must still exist
   and a second holder commit must be visible to a fresh peer connection.
   A control leg proves the harness detects the defect: a plain ``open()``
   of the database in the holder process makes the peer delete the sidecars.
"""

from __future__ import annotations

import builtins
import io
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import sqlite_readonly  # noqa: E402
from aiworkhub.sqlite_readonly import connect_readonly  # noqa: E402

# A peer process: open, read every row, close. Closing is the step that, when
# it can take EXCLUSIVE, checkpoints and deletes the WAL and wal-index.
_PEER_SCRIPT = (
    "import sqlite3, sys\n"
    "con = sqlite3.connect(sys.argv[1], timeout=30)\n"
    "try:\n"
    "    rows = con.execute('SELECT v FROM t ORDER BY v').fetchall()\n"
    "finally:\n"
    "    con.close()\n"
    "print(','.join(str(row[0]) for row in rows))\n"
)
_PEER_TIMEOUT_SECONDS = 120


def _sidecars(db: Path) -> tuple[Path, Path]:
    return db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")


@contextmanager
def _wal_holder(db: Path) -> Iterator[sqlite3.Connection]:
    """A live in-process WAL connection with one committed row."""

    holder = sqlite3.connect(str(db), timeout=30)
    try:
        assert holder.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        holder.execute("CREATE TABLE t(v INTEGER)")
        holder.execute("INSERT INTO t(v) VALUES (1)")
        holder.commit()
        yield holder
    finally:
        holder.close()


def _peer_read(db: Path) -> list[int]:
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _PEER_SCRIPT, str(db)],
        capture_output=True,
        text=True,
        timeout=_PEER_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    text = completed.stdout.strip()
    return [int(value) for value in text.split(",")] if text else []


def _normalise(target: object) -> str | None:
    if isinstance(target, int):
        return None  # an fd, not a path
    try:
        raw = os.fsdecode(os.fspath(target))  # type: ignore[arg-type]
    except TypeError:
        return None
    return os.path.normcase(os.path.realpath(raw))


@contextmanager
def _open_spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record every path opened via ``builtins.open``/``io.open``/``os.open``."""

    seen: list[str] = []
    real_builtin_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open

    def spy_builtin_open(file, *args, **kwargs):
        name = _normalise(file)
        if name is not None:
            seen.append(name)
        return real_builtin_open(file, *args, **kwargs)

    def spy_io_open(file, *args, **kwargs):
        name = _normalise(file)
        if name is not None:
            seen.append(name)
        return real_io_open(file, *args, **kwargs)

    def spy_os_open(path, *args, **kwargs):
        name = _normalise(path)
        if name is not None:
            seen.append(name)
        return real_os_open(path, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(builtins, "open", spy_builtin_open)
        mp.setattr(io, "open", spy_io_open)
        mp.setattr(os, "open", spy_os_open)
        yield seen


def _protected(db: Path) -> set[str]:
    return {_normalise(path) for path in (db, *_sidecars(db))}  # type: ignore[misc]


def test_nf02130_connect_readonly_opens_no_live_sqlite_file_outside_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "task.db"
    canary = tmp_path / "canary.txt"
    canary.write_text("spy self-check", encoding="utf-8")

    with _wal_holder(db):
        wal, shm = _sidecars(db)
        assert wal.is_file() and shm.is_file(), "holder must keep a live WAL"
        protected = _protected(db)

        with _open_spy(monkeypatch) as seen:
            # The spy must actually see opens, or an empty list proves nothing.
            with open(canary, encoding="utf-8") as handle:
                handle.read()
            with io.open(canary, encoding="utf-8") as handle:
                handle.read()
            os.close(os.open(canary, os.O_RDONLY))
            assert seen.count(_normalise(canary)) == 3

            con = connect_readonly(db)
            try:
                assert sqlite_readonly.fallback_mode(con) is None
                assert con.execute("SELECT v FROM t").fetchall() == [(1,)]
            finally:
                con.close()

            # The failure-path classification must not open the sidecars on
            # POSIX either: it runs while the same live connections exist.
            # Windows keeps its open() probe (locks are per handle there).
            if os.name != "nt":
                assert sqlite_readonly._sidecar_state_unwritable(db) is False

        assert sqlite_readonly._sidecar_state_unwritable(db) is False
        opened = sorted(set(seen) & protected)
        assert opened == [], f"opened a live SQLite file outside SQLite: {opened}"


@pytest.mark.skipif(os.name == "nt", reason="fcntl lock semantics are POSIX-only")
def test_nf02130_readonly_open_keeps_holder_locks_so_peer_cannot_delete_wal(
    tmp_path: Path,
) -> None:
    db = tmp_path / "task.db"
    wal, shm = _sidecars(db)

    with _wal_holder(db) as holder:
        con = connect_readonly(db)
        try:
            assert con.execute("SELECT v FROM t ORDER BY v").fetchall() == [(1,)]
        finally:
            con.close()

        assert _peer_read(db) == [1]
        assert wal.exists(), "peer deleted -wal under a live holder connection"
        assert shm.exists(), "peer deleted -shm under a live holder connection"

        holder.execute("INSERT INTO t(v) VALUES (2)")
        holder.commit()
        assert _peer_read(db) == [1, 2], "holder commit diverged from the shared WAL"


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="control relies on Linux SQLite deleting the WAL on last close "
    "(some macOS builds persist it); the reproduction above runs on all POSIX",
)
def test_nf02130_control_plain_open_in_holder_process_lets_peer_delete_wal(
    tmp_path: Path,
) -> None:
    """The defect the fix removes, reproduced so the test above is not vacuous."""

    db = tmp_path / "task.db"
    wal, shm = _sidecars(db)

    with _wal_holder(db):
        with open(db, "rb") as handle:  # what _is_wal_mode used to do
            handle.read(20)

        assert _peer_read(db) == [1]
        assert not wal.exists() and not shm.exists(), (
            "plain open()/close() no longer drops fcntl locks here; the "
            "NF-2026-02130 reproduction above would not detect a regression"
        )
