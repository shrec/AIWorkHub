"""NF-2026-00350: the locking/liveness/group-kill contract, proven on POSIX.

Three defects lived under one "same contract on every platform" docstring, and
their recoveries were written for an OS we do not run on:

* A1 -- ``lock_fd(blocking=True)`` waited forever on POSIX (``flock(LOCK_EX)``)
  while Windows raised ``AdvisoryLockTimeout`` after a bounded wait. The careful
  ``request_lock_busy`` recovery in ``process_launcher`` was therefore
  UNREACHABLE on Linux; contention parked a monitor thread instead.
* A4 -- liveness had three answers to one question. ``EPERM`` (a signal we are
  not permitted to send) means the process EXISTS, yet the launcher and the
  shared router read it as DEAD and terminalized a live worker.
* A3 -- the group terminator signalled the whole process group but then checked
  only the leader, so a child outliving its leader skipped SIGKILL escalation.

These regressions drive each defect on POSIX -- not behind a Windows-only
marker. They are the platform we actually run on.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import platform_io, process_launcher, runtime_temp, shared_router  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX flock / os.kill / process-group semantics",
)


def _chmod_blocked_by_sandbox() -> bool:
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as name:
        try:
            os.chmod(name, 0o700)
        except PermissionError:
            return True
    return False


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    """See test_task_liveness_reconciler.py's identical fixture: neutralize the
    bare ``os.chmod``/``os.fchmod`` syscalls ONLY when this exact sandbox
    genuinely rejects them (probed once); a no-op on a normal host."""
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


# --- A1: the POSIX blocking lock is bounded, and its recovery is reachable ----


def test_the_wait_bound_is_one_shared_value_not_a_windows_only_constant():
    """A single neutral bound is used by both platforms; the old Windows-named
    constant is gone, so no second constant can drift beside it."""
    assert isinstance(platform_io.ADVISORY_LOCK_MAX_WAIT_SECONDS, float)
    assert not hasattr(platform_io, "WINDOWS_LOCK_MAX_WAIT_SECONDS")
    assert not hasattr(platform_io, "WINDOWS_LOCK_POLL_SECONDS")


def test_posix_blocking_lock_times_out_instead_of_waiting_forever(tmp_path, monkeypatch):
    """A real cross-process holder must make the contender raise
    ``AdvisoryLockTimeout`` within the shared bound -- not block forever."""
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_MAX_WAIT_SECONDS", 0.3)

    lock_path = tmp_path / "shared.lock"
    holder_src = textwrap.dedent(
        f"""
        import os, sys, time
        sys.path.insert(0, {str(_SRC)!r})
        from aiworkhub import platform_io
        fd = os.open({str(lock_path)!r}, os.O_CREAT | os.O_RDWR, 0o600)
        platform_io.lock_fd(fd, blocking=True)
        print("HELD", flush=True)
        time.sleep(30)
        """
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_src], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout.readline().strip() == "HELD"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            started = time.monotonic()
            with pytest.raises(platform_io.AdvisoryLockTimeout):
                platform_io.lock_fd(fd, blocking=True)
            waited = time.monotonic() - started
        finally:
            os.close(fd)
        assert waited < 10, f"blocking lock must stop meaning forever, waited {waited:.2f}s"
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def _minimal_manager(tmp_path: Path) -> process_launcher.ProcessManager:
    repo = tmp_path / "repo"
    repo.mkdir()
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=lambda task_id: {"returncode": 0, "stdout": "{}", "stderr": ""},
        isolation_enabled=False,
    )


def test_advisory_lock_recovery_is_reachable_on_posix(tmp_path, monkeypatch):
    """Driving the contended request-lock path on POSIX must reach the recovery
    that previously only Windows could hit: a deferred, non-destructive
    ``request_lock_busy`` outcome that retains the workspace."""
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_MAX_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_POLL_SECONDS", 0.005)
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        lambda *a, **k: pytest.fail("lock contention must not terminalize the task"),
    )

    manager = _minimal_manager(tmp_path)
    request_id = "a" * 32
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_LOCK",
        "runner": "claude_worker_lock",
        "topic": "task_mcp",
        "state": "running",
        "pid": 123,
        "pid_start_ticks": 456,
        "metadata_path": str(tmp_path / "metadata.json"),
    })

    # Hold the one writer boundary, then drive a duplicate finalizer into it.
    with manager._request_lock(request_id):
        deferred = manager._finalize_after_process_exit(request_id, 0)

    assert deferred is not None
    assert deferred["state"] == "running"
    assert deferred["reconciliation_deferred"] == "request_lock_busy"
    assert deferred["workspace_retained"] is True
    # The durable card was not moved by the contended attempt.
    assert manager._request_events(request_id)[-1]["state"] == "running"


# --- A4: liveness is one function, and EPERM reads as ALIVE everywhere --------


def test_pid_one_is_reported_alive_from_every_entry_point():
    """PID 1 is unambiguously alive. Signalling it is refused with EPERM, and
    every liveness entry point must read that as ALIVE, never dead."""
    assert platform_io.process_is_alive(1) is True
    assert process_launcher._pid_alive(1) is True
    assert runtime_temp._pid_alive(1) is True


def test_eperm_reads_as_alive_from_every_entry_point(monkeypatch):
    """A pid whose signal is refused with EPERM exists, so it is ALIVE."""
    def refuse(_pid, _sig):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(platform_io.os, "kill", refuse)

    assert platform_io.process_is_alive(4321) is True
    assert process_launcher._pid_alive(4321) is True
    assert runtime_temp._pid_alive(4321) is True


def test_liveness_is_one_function_and_no_module_keeps_a_private_copy():
    """Every probe imports the single ``platform_io.process_is_alive``; the
    source of each former owner keeps no private ``_pid_alive`` definition, so a
    re-introduced copy fails this contract test."""
    assert process_launcher._pid_alive is platform_io.process_is_alive
    assert runtime_temp._pid_alive is platform_io.process_is_alive
    assert shared_router.process_is_alive is platform_io.process_is_alive

    for module in (process_launcher, runtime_temp, shared_router):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "def _pid_alive(" not in source, (
            f"{module.__name__} re-introduced a private liveness probe"
        )


def test_shared_router_reports_a_live_foreign_uid_process_as_alive(monkeypatch):
    """The shared route registry used to read EPERM as dead, declaring a live
    extension host (under another uid) dead. It now routes through the one
    function, so EPERM is alive."""
    def refuse(_pid, _sig):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(platform_io.os, "kill", refuse)
    assert shared_router.process_is_alive(4321) is True


# --- boundary robustness: one malformed record must not hide every route ------


def _write_route_record(reg_dir: Path, repo_id: str, *, extension_host_pid) -> Path:
    """Materialize a route record that passes schema + manifest binding, so the
    only variable under test is the ``extension_host_pid`` value on line 90."""
    root = reg_dir.parent / repo_id
    manifest = root / shared_router.repository_state.PROJECT_MANIFEST_REL
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"repo_id": repo_id}), encoding="utf-8")
    record = reg_dir / f"{repo_id}.json"
    record.write_text(
        json.dumps({
            "schema_id": shared_router.SCHEMA_ID,
            "repo_id": repo_id,
            "repo_root": str(root),
            "extension_host_pid": extension_host_pid,
        }),
        encoding="utf-8",
    )
    return record


def test_non_numeric_extension_host_pid_is_rejected_not_raised(tmp_path, monkeypatch):
    """A registry record with a non-numeric ``extension_host_pid`` otherwise
    passes schema and manifest binding. The boundary whose whole job is to
    summarize an invalid record as a REJECT must not raise on the tenth field
    when it defends the other nine -- one malformed record cannot take down the
    whole route-resolution listing and hide every other repository's route."""
    reg_dir = tmp_path / "router" / "repos"
    reg_dir.mkdir(parents=True)
    good_id = "repo_" + "a" * 32
    bad_id = "repo_" + "b" * 32
    _write_route_record(reg_dir, good_id, extension_host_pid=4321)
    _write_route_record(reg_dir, bad_id, extension_host_pid="not-a-pid")
    monkeypatch.setattr(shared_router, "registry_dir", lambda home=None: reg_dir)

    # Must not raise ValueError while parsing the malformed record.
    listing = shared_router.list_known_repositories(include_inactive=True)

    assert listing["ok"] is True
    seen = {r["repo_id"] for r in listing["repositories"] + listing["inactive"]}
    assert good_id in seen, "the valid route survives the malformed neighbour"
    reject = next(r for r in listing["rejects"] if r.get("repo_id") == bad_id)
    assert reject["error"] == "extension_host_pid_invalid"


# --- A3: the group terminator verifies the group it signalled ----------------


def test_group_terminator_escalates_when_a_child_outlives_its_leader(monkeypatch):
    """The leader is dead, but a child keeps the process GROUP alive. Because
    the terminator now verifies the group (not just the leader), the early
    return is refused and the SIGKILL escalation still runs."""
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(process_launcher.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    # Leader dead -- if the check still gated on the leader it would return early.
    monkeypatch.setattr(process_launcher, "_pid_alive", lambda _pid: False)
    # ...but a surviving child keeps the group alive through the whole grace.
    monkeypatch.setattr(process_launcher, "_process_group_alive", lambda _pgid: True)
    clock = iter([0.0, 0.0, 0.2, 0.2, 0.2])
    monkeypatch.setattr(process_launcher.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(process_launcher.time, "sleep", lambda _s: None)

    process_launcher._terminate_process_group(4321, grace_seconds=0.1)

    sent = [sig for _, sig in signals]
    assert signal.SIGTERM in sent, "the group is signalled first"
    assert signal.SIGKILL in sent, "a surviving child must force SIGKILL escalation"
    assert signals[-1] == (4321, signal.SIGKILL)


def test_process_group_alive_reads_the_group_not_the_leader(monkeypatch):
    """The group probe answers on ``killpg(pgid, 0)``: ESRCH is empty (dead),
    EPERM is a member we may not signal (alive)."""
    monkeypatch.setattr(
        process_launcher.os,
        "killpg",
        lambda _p, _s: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert process_launcher._process_group_alive(4321) is False

    monkeypatch.setattr(
        process_launcher.os,
        "killpg",
        lambda _p, _s: (_ for _ in ()).throw(PermissionError(errno.EPERM, "denied")),
    )
    assert process_launcher._process_group_alive(4321) is True
