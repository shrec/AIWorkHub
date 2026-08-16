"""Cross-platform process-identity contract for ``process_start_ticks``.

A PID alone is never an identity -- the operating system recycles PIDs, and a
recycled PID read as a live worker makes the launcher believe a dead worker is
running or reap a process it does not own.  The process *start time* is what
makes the (pid, start_ticks) pair unique.  This module pins that property on
the running platform and pins the fail-closed behaviour every caller must show
when a platform genuinely cannot supply a start time.

The identity primitive is unified: ``runtime_temp.process_start_ticks`` is the
sole implementation and both ``process_launcher._pid_start_ticks`` and
``worker_supervisor._pid_start_ticks`` delegate to it, so the launcher, the
standalone supervisor, and the temp-owner GC can never disagree.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher, runtime_temp, worker_supervisor  # noqa: E402


def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_running_platform_supplies_a_real_identity_not_none():
    """On Linux, Darwin, and Windows the identity is a real value, never None.

    This is the core Darwin regression: ``sysctl kern.proc.pid`` must expose a
    start time so ``process_start_ticks`` stops returning None on every call.
    """
    ticks = runtime_temp.process_start_ticks(os.getpid())
    assert ticks is not None
    assert isinstance(ticks, int)
    # The two delegators must return the identical value from the one source.
    assert process_launcher._pid_start_ticks(os.getpid()) == ticks
    assert worker_supervisor._pid_start_ticks(os.getpid()) == ticks


def test_non_positive_pid_has_no_identity():
    assert runtime_temp.process_start_ticks(0) is None
    assert runtime_temp.process_start_ticks(-1) is None
    assert process_launcher._pid_start_ticks(0) is None


def test_two_distinct_live_processes_never_share_one_identity():
    """Two genuinely-live processes must not collapse to the same identity."""
    first = _spawn_sleeper()
    second = _spawn_sleeper()
    try:
        assert first.pid != second.pid
        first_ticks = runtime_temp.process_start_ticks(first.pid)
        second_ticks = runtime_temp.process_start_ticks(second.pid)
        assert first_ticks is not None
        assert second_ticks is not None
        # Identity is the (pid, start_ticks) pair; distinct live processes
        # never produce the same pair.
        assert (first.pid, first_ticks) != (second.pid, second_ticks)
    finally:
        for child in (first, second):
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)


def test_reused_pid_with_a_different_start_time_is_not_the_same_process():
    """A live PID carrying a foreign start time must be rejected, not matched.

    This is exactly the PID-reuse hole the start time closes: same PID, but the
    recorded creation stamp no longer matches, so it is a different process.
    """
    real = runtime_temp.process_start_ticks(os.getpid())
    assert real is not None

    # Same live PID, wrong (stale) start ticks -> not the recorded process.
    assert process_launcher._pid_matches(os.getpid(), real + 999_999) is False
    stale = process_launcher._pid_identity_evidence(os.getpid(), real + 999_999)
    assert stale.verdict is process_launcher.PidIdentityVerdict.MISMATCH

    # Same live PID, matching start ticks -> the recorded process.
    assert process_launcher._pid_matches(os.getpid(), real) is True
    fresh = process_launcher._pid_identity_evidence(os.getpid(), real)
    assert fresh.verdict is process_launcher.PidIdentityVerdict.MATCH


def test_absent_identity_is_a_legible_refusal_never_a_typeerror(monkeypatch):
    """A platform that cannot supply a start time must fail closed and legible.

    Every caller of the primitive in both modules must behave correctly when
    the identity is absent: no ``NoneType + int`` TypeError, an explicit
    "unknown" verdict, a refusal to act on the bare PID, and a note of the
    protection that is lost -- never a match and never a crash.
    """
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: None)
    monkeypatch.setattr(process_launcher.runtime_temp, "process_start_ticks", lambda pid: None)
    monkeypatch.setattr(worker_supervisor, "process_start_ticks", lambda pid: None)

    pid = os.getpid()

    # The delegators degrade to None rather than raising.
    assert process_launcher._pid_start_ticks(pid) is None
    assert worker_supervisor._pid_start_ticks(pid) is None

    # Evidence is UNKNOWN with a named, legible cause -- not a TypeError.
    evidence = process_launcher._pid_identity_evidence(pid, 12_345)
    assert evidence.verdict is process_launcher.PidIdentityVerdict.UNKNOWN
    assert evidence.operation == "_pid_start_ticks"
    assert evidence.exception == "StartTicksUnavailable"

    # Termination refuses to act on a PID it cannot positively identify.
    assert process_launcher._identity_verified_pid(pid, 12_345) == 0

    # A live PID with unknown ticks is *not* proven dead (fail closed).
    assert process_launcher._process_proven_dead(pid, 12_345) is False

    # Bare-liveness reporting is still allowed when no ticks were recorded,
    # but a recorded-yet-unverifiable tick refuses rather than adds.
    assert process_launcher._pid_matches(pid, None) is True
    assert process_launcher._pid_matches(pid, 12_345) is False


def test_darwin_kinfo_parse_is_dependency_free_and_bounded():
    """The Darwin byte parse is pure and testable on any platform.

    ``kp_proc.p_starttime`` is a ``struct timeval`` (int64 tv_sec, int32
    tv_usec) at offset 0 of ``kinfo_proc``; only that 12-byte prefix is read.
    """
    tv_sec = 1_600_000_000
    tv_usec = 123_456
    raw = tv_sec.to_bytes(8, "little", signed=True) + tv_usec.to_bytes(4, "little", signed=True)
    raw += b"\x00\x00\x00\x00"  # timeval tail padding, ignored
    assert runtime_temp._darwin_start_ticks_from_kinfo(raw) == tv_sec * 1_000_000 + tv_usec

    # Too short, implausible microseconds, and non-positive seconds all refuse.
    assert runtime_temp._darwin_start_ticks_from_kinfo(b"\x00" * 11) is None
    bad_usec = (10).to_bytes(8, "little") + (2_000_000).to_bytes(4, "little")
    assert runtime_temp._darwin_start_ticks_from_kinfo(bad_usec) is None
    assert runtime_temp._darwin_start_ticks_from_kinfo(b"\x00" * 16) is None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc field-22 path")
def test_linux_proc_field_22_reading_is_unchanged():
    """The Linux path still reads ``/proc/<pid>/stat`` field 22, untouched."""
    pid = os.getpid()
    ticks = runtime_temp._proc_stat_starttime(pid)
    assert ticks is not None
    # process_start_ticks routes Linux through the same field-22 reader.
    assert runtime_temp.process_start_ticks(pid) == ticks
    # Independent read of field 22 (starttime) confirms the parse.
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    _head, _sep, tail = raw.rpartition(")")
    assert int(tail.split()[19]) == ticks


def test_identity_primitive_is_unified_behind_one_implementation():
    """Both delegators resolve to the single ``runtime_temp`` implementation."""
    # The supervisor imported the very same function object.
    assert worker_supervisor.process_start_ticks is runtime_temp.process_start_ticks
    # The launcher looks the function up on runtime_temp at call time.
    assert process_launcher.runtime_temp is runtime_temp


def test_owner_alive_fails_closed_when_identity_is_absent(monkeypatch):
    """The temp-owner GC caller must never reclaim an unidentifiable owner.

    ``owner_alive`` is the consumer that turns the identity primitive into a
    delete decision.  Absence of identity is not evidence of death: a manifest
    carrying no usable PID, a zeroed PID, or a garbage PID must all answer
    *alive* so the runtime directory is never reclaimed, and must never raise a
    TypeError.  This pins the fail-*closed* direction independently of the
    ``_pid_alive`` probe (which is forced dead here to prove the short-circuit).
    """
    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: False)

    # No PID key at all, an explicit zero PID, and a non-integer PID all
    # fail closed to alive rather than raising or reclaiming.
    assert runtime_temp.owner_alive({"schema_id": runtime_temp.OWNER_SCHEMA_ID}) is True
    assert runtime_temp.owner_alive({"pid": 0, "starttime": 0}) is True
    assert runtime_temp.owner_alive({"pid": "not-a-pid"}) is True


def test_owner_alive_reused_pid_is_not_the_recorded_owner(monkeypatch):
    """A dead PID reclaims only when its start identity still matches.

    Same PID, different recorded start time means the OS recycled the PID onto
    a different process, so the directory is foreign and must survive; a
    matching start time is the only case that reclaims.
    """
    manifest = {"schema_id": runtime_temp.OWNER_SCHEMA_ID, "pid": 4242, "starttime": 111}
    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: False)

    # Reused PID: current start ticks differ -> foreign -> alive (kept).
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: 999)
    assert runtime_temp.owner_alive(manifest) is True

    # Same PID, matching start ticks -> the recorded owner is truly dead.
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: 111)
    assert runtime_temp.owner_alive(manifest) is False
