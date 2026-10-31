"""NF-2026-01898: the write lease names its holder so a timeout is diagnosable."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from aiworkhub import db_writer

_CHILD = """
import os, sys
from aiworkhub import db_writer
with db_writer.write_lease(sys.argv[1]):
    print(os.getpid(), flush=True)
    sys.stdin.readline()
"""


def _db(tmp_path: Path) -> Path:
    return tmp_path / "task_queue.sqlite"


def test_timeout_names_the_holding_process(tmp_path: Path) -> None:
    db = _db(tmp_path)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(db)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    holder_pid = 0
    try:
        assert child.stdout is not None
        # A venv launcher can sit between Popen and the interpreter, so the
        # child reports the pid that actually holds the lease.
        holder_pid = int(child.stdout.readline().strip())
        with pytest.raises(db_writer.WriteLeaseTimeout) as excinfo:
            with db_writer.write_lease(db, timeout_s=0.5):
                pass
        message = str(excinfo.value)
        assert f"holder_pid={holder_pid}" in message
        assert "callsite=" in message
        assert re.search(r":held_ms=\d+:", message)
        assert excinfo.value.holder is not None
        assert excinfo.value.holder["pid"] == holder_pid
        assert excinfo.value.retryable is True
    finally:
        if child.stdin is not None:
            child.stdin.close()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            if holder_pid and holder_pid != child.pid:
                try:
                    os.kill(holder_pid, signal.SIGTERM)
                except OSError:
                    pass
            child.wait()
        if child.stdout is not None:
            child.stdout.close()


def test_sidecar_removed_after_normal_exit(tmp_path: Path) -> None:
    db = _db(tmp_path)
    holder = db_writer.holder_path_for(db)
    with db_writer.write_lease(db):
        assert holder.exists()
    assert not holder.exists()


def test_timeout_without_sidecar_keeps_the_old_format(tmp_path: Path) -> None:
    db = _db(tmp_path)
    errors: list[BaseException] = []

    def contender() -> None:
        try:
            with db_writer.write_lease(db, timeout_s=0.2):
                pass
        except BaseException as exc:  # noqa: BLE001 -- collected for the assertion
            errors.append(exc)

    with db_writer.write_lease(db):
        db_writer.holder_path_for(db).unlink()
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(10)

    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, db_writer.WriteLeaseTimeout)
    assert error.holder is None
    assert ":holder_pid" not in str(error)
    assert re.fullmatch(
        r"write_lease_timeout:task_queue\.sqlite:waited_ms=\d+:timeout_ms=200",
        str(error),
    )
    assert str(db_writer.WriteLeaseTimeout(str(db), 0.5, 30.0)) == (
        "write_lease_timeout:task_queue.sqlite:waited_ms=500:timeout_ms=30000"
    )


def test_reentrant_lease_leaves_the_sidecar_until_the_outer_exits(tmp_path: Path) -> None:
    db = _db(tmp_path)
    holder = db_writer.holder_path_for(db)
    with db_writer.write_lease(db):
        with db_writer.write_lease(db) as inner:
            assert inner["reentrant"] is True
            assert holder.exists()
        assert holder.exists()
    assert not holder.exists()


def test_unparseable_sidecar_never_masks_the_timeout(tmp_path: Path) -> None:
    # Deeply nested JSON makes json.loads raise RecursionError, not ValueError;
    # the best-effort holder read must still yield None so the caller raises
    # WriteLeaseTimeout instead of an unrelated exception.
    db = _db(tmp_path)
    db_writer.holder_path_for(db).write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    assert db_writer._read_holder(db) is None
