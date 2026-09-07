"""Contract tests for the cross-process single-writer lease.

The decisive test here is :func:`test_lease_is_mutually_exclusive_across_processes`.
The measured contention this module exists for is cross-process (169 distinct
supervisor pids), so a lease that only excluded threads would be useless; that
test forks real OS processes and fails if the lease is not observed by both.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from aiworkhub import db_writer


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _connect(path: Path, busy_ms: int = 5000) -> sqlite3.Connection:
    """The same connection shape ``task_store._connect`` builds."""
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# mutual exclusion
# ---------------------------------------------------------------------------
def test_lease_is_mutually_exclusive_across_threads(tmp_path: Path) -> None:
    db = tmp_path / "q.sqlite"
    inside = []
    overlap = []

    def run() -> None:
        for _ in range(40):
            with db_writer.write_lease(db, timeout_s=10):
                inside.append(1)
                if len(inside) > 1:
                    overlap.append(1)
                time.sleep(0.001)
                inside.pop()

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlap == [], "two threads held the same write lease at once"


_CHILD = r"""
import os, sys, time
sys.path.insert(0, %(src)r)
from aiworkhub import db_writer

db = %(db)r
stamp = %(stamp)r
with db_writer.write_lease(db, timeout_s=30):
    # Record an interval; overlapping intervals prove the lease was not honoured.
    start = time.time()
    time.sleep(0.30)
    end = time.time()
with open(stamp, "a") as fh:
    fh.write("%%f %%f %%d\n" %% (start, end, os.getpid()))
"""


def test_lease_is_mutually_exclusive_across_processes(tmp_path: Path) -> None:
    """The measured failure is cross-process, so the lease must hold there."""
    if db_writer.backend() == "none":
        pytest.skip("no cross-process locking primitive on this host")

    db = tmp_path / "q.sqlite"
    stamp = tmp_path / "intervals.txt"
    src = str(Path(__file__).resolve().parents[1] / "src")
    script = tmp_path / "child.py"
    script.write_text(
        _CHILD % {"src": src, "db": str(db), "stamp": str(stamp)}, encoding="utf-8"
    )

    procs = [
        subprocess.Popen([sys.executable, str(script)])
        for _ in range(5)
    ]
    for p in procs:
        assert p.wait(timeout=120) == 0

    rows = [
        tuple(float(x) for x in line.split()[:2])
        for line in stamp.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 5
    rows.sort()
    for (start_a, end_a), (start_b, _end_b) in zip(rows, rows[1:]):
        assert end_a <= start_b + 1e-6, (
            "two OS processes held the write lease at the same time: "
            f"{end_a} overlaps {start_b}"
        )


# ---------------------------------------------------------------------------
# re-entrancy, timeout, error semantics
# ---------------------------------------------------------------------------
def test_nested_lease_on_same_path_does_not_deadlock(tmp_path: Path) -> None:
    db = tmp_path / "q.sqlite"
    with db_writer.write_lease(db, timeout_s=5) as outer:
        assert outer["reentrant"] is False
        with db_writer.write_lease(db, timeout_s=5) as inner:
            assert inner["reentrant"] is True


def test_timeout_raises_named_typed_error_and_never_proceeds(tmp_path: Path) -> None:
    db = tmp_path / "q.sqlite"
    released = threading.Event()
    holding = threading.Event()

    def hold() -> None:
        with db_writer.write_lease(db, timeout_s=10):
            holding.set()
            released.wait(timeout=10)

    t = threading.Thread(target=hold)
    t.start()
    assert holding.wait(timeout=10)
    try:
        with pytest.raises(db_writer.WriteLeaseTimeout) as excinfo:
            with db_writer.write_lease(db, timeout_s=0.05):
                pytest.fail("lease must not be granted while another holder has it")
    finally:
        released.set()
        t.join()

    err = excinfo.value
    assert err.retryable is True
    assert err.timeout_s == pytest.approx(0.05)
    assert "write_lease_timeout" in str(err)
    assert db.name in str(err)


def test_body_exception_propagates_unchanged_and_releases(tmp_path: Path) -> None:
    """A queue that swallows an error is worse than a lock."""
    db = tmp_path / "q.sqlite"
    sentinel = sqlite3.OperationalError("no such table: nope")

    with pytest.raises(sqlite3.OperationalError) as excinfo:
        with db_writer.write_lease(db, timeout_s=5):
            raise sentinel
    assert excinfo.value is sentinel

    # released despite the exception
    with db_writer.write_lease(db, timeout_s=1) as receipt:
        assert receipt["reentrant"] is False


# ---------------------------------------------------------------------------
# path handling
# ---------------------------------------------------------------------------
def test_lock_file_is_a_sidecar_and_never_the_database(tmp_path: Path) -> None:
    db = tmp_path / "task_queue.sqlite"
    lock = db_writer.lock_path_for(db)
    assert lock != db
    assert lock.name == "task_queue.sqlite.writer.lock"
    with db_writer.write_lease(db, timeout_s=5):
        assert lock.exists()
    assert not db.exists(), "the lease must not create or touch the database file"


def test_connection_db_path_resolves_the_main_database(tmp_path: Path) -> None:
    db = tmp_path / "q.sqlite"
    conn = _connect(db)
    try:
        assert Path(db_writer.connection_db_path(conn)).resolve() == db.resolve()
    finally:
        conn.close()


def test_memory_connection_is_skipped_not_failed() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with db_writer.lease_for_connection(conn) as receipt:
            assert receipt["skipped"] is True
    finally:
        conn.close()


def test_lease_for_connection_serializes_a_file_backed_connection(tmp_path: Path) -> None:
    db = tmp_path / "q.sqlite"
    conn = _connect(db)
    try:
        with db_writer.lease_for_connection(conn, timeout_s=5) as receipt:
            assert receipt["skipped"] is False
            assert Path(receipt["db_path"]).resolve() == db.resolve()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------
def test_stats_record_real_waiting(tmp_path: Path) -> None:
    db = tmp_path / "q.sqlite"
    db_writer.reset_stats()
    with db_writer.write_lease(db, timeout_s=5):
        pass
    snapshot = db_writer.stats()
    assert snapshot["acquired"] == 1.0
    assert snapshot["timeouts"] == 0.0
    assert snapshot["held_total_s"] >= 0.0


# ---------------------------------------------------------------------------
# the behaviour that motivated the module
# ---------------------------------------------------------------------------
def _claim_worker(idx: int, db: str, iters: int, leased: bool, busy_ms: int, q) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from aiworkhub import db_writer as dw

    locked = ok = 0

    def attempt() -> None:
        """One whole claim, connection lifecycle included.

        Open and close are inside the unit on purpose. ``PRAGMA
        journal_mode=WAL`` is a write, and closing the last connection can
        trigger a WAL checkpoint that also needs the write lock -- both raise
        "database is locked", so leaving either outside the lease would leave
        the class only half removed. ``task_engine.claim_start_exact`` takes
        the lease in the same order for the same reason.
        """
        conn = sqlite3.connect(db, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout={busy_ms}")
            conn.execute("PRAGMA journal_mode=WAL")
            row = conn.execute(
                "SELECT card_json FROM tasks WHERE task_id='t'"
            ).fetchone()
            raw = row["card_json"]
            card = json.loads(raw)
            card["n"] = int(card["n"]) + 1
            conn.execute(
                "UPDATE tasks SET card_json=? WHERE task_id='t' AND card_json=?",
                (json.dumps(card, sort_keys=True), raw),
            )
            conn.commit()
        finally:
            conn.close()

    for _ in range(iters):
        try:
            if leased:
                with dw.write_lease(db, timeout_s=60):
                    attempt()
            else:
                attempt()
            ok += 1
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                locked += 1
            else:
                raise
    q.put((ok, locked))


@pytest.mark.parametrize("leased", [False, True])
def test_lease_removes_the_database_is_locked_class(tmp_path: Path, leased: bool) -> None:
    """Under a busy_timeout shorter than the holder, the unleased path loses
    writes to ``database is locked``; the leased path must lose none."""
    db = tmp_path / "q.sqlite"
    conn = _connect(db)
    conn.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY, card_json TEXT)")
    conn.execute(
        "INSERT INTO tasks VALUES('t', ?)",
        (json.dumps({"n": 0, "pad": "x" * 200_000}, sort_keys=True),),
    )
    conn.commit()
    conn.close()

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [
        ctx.Process(target=_claim_worker, args=(i, str(db), 25, leased, 1, q))
        for i in range(6)
    ]
    for p in procs:
        p.start()
    results = [q.get(timeout=180) for _ in procs]
    for p in procs:
        p.join(timeout=60)

    locked = sum(r[1] for r in results)
    if leased:
        assert locked == 0, f"the lease must remove the lock class, saw {locked}"
    else:
        # Documents the baseline this module exists to remove. If this ever
        # stops failing, the scenario no longer reproduces contention and the
        # leased assertion above would be proving nothing.
        assert locked > 0, "baseline scenario no longer reproduces contention"
