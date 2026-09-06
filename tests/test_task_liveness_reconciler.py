"""B412: token-free supervisor heartbeat, honest liveness derivation, and the
durable reconciler that finalizes an isolated worker even after the
launching MCP/VS Code/launcher process has disappeared.

Regression anchor: request ``d6b6e8ee4080420fb555692e741452bf`` exited
successfully at 2026-07-15T14:21:41Z but stayed ``processing`` until a
coordinator manually invoked reconciliation an hour later (15:21Z). The
structural gap: ``ProcessManager`` only re-scans persisted requests when a
NEW instance happens to be constructed -- nothing re-scans on its own. This
file exercises the fix: ``worker_supervisor.py``'s heartbeat, the derived
alive/quiet/unresponsive/lost states, and ``task_reconciler.py`` repeatedly
driving the EXISTING finalize path.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _ensure_deepseek_credentials_stub() -> None:
    """See test_process_launcher_security.py's identical helper: some
    isolated Task MCP worktrees are missing ``deepseek_credentials.py``
    entirely (an uncommitted file on the trusted host). Only installs a
    stub when the real module is genuinely unimportable."""
    import importlib
    import types

    try:
        importlib.import_module("aiworkhub.deepseek_credentials")
        return
    except ImportError:
        pass

    stub = types.ModuleType("aiworkhub.deepseek_credentials")

    class CredentialError(Exception):
        def __init__(self, reason: str = "deepseek_credential_stub_environment") -> None:
            super().__init__(reason)
            self.reason = reason

    def load_credential(repo=None):  # noqa: ANN001, ARG001
        raise CredentialError("deepseek_credential_stub_environment")

    def adapter_readiness(repo=None):  # noqa: ANN001, ARG001
        return {"ok": True, "readonly": True, "adapters": []}

    stub.CredentialError = CredentialError
    stub.load_credential = load_credential
    stub.adapter_readiness = adapter_readiness
    sys.modules["aiworkhub.deepseek_credentials"] = stub


_ensure_deepseek_credentials_stub()

from aiworkhub import process_launcher, task_reconciler, worker_supervisor  # noqa: E402
from aiworkhub import worker_workspace  # noqa: E402


def _chmod_blocked_by_sandbox() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as name:
        try:
            os.chmod(name, 0o700)
        except PermissionError:
            return True
    return False


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    """See test_process_launcher_security.py's identical fixture docstring:
    neutralizes ``os.chmod``/``os.fchmod`` ONLY when this exact sandbox
    genuinely rejects the bare syscall (probed once); a no-op elsewhere."""
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


def _spawn_sleeper(seconds: float = 30.0) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_dead(proc: subprocess.Popen, timeout: float = 5.0) -> bool:
    """Reap ``proc`` (a direct child of THIS test process) so a zombie entry
    can never be mistaken for "still alive" -- unlike production, where a
    terminated grandchild is reparented to init and reaped there, a test
    fixture's direct child stays a zombie (and ``os.kill(pid, 0)`` keeps
    succeeding) until its own parent calls ``wait()``."""
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _kill_if_alive(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


# --- worker_supervisor.py heartbeat -----------------------------------------


def test_heartbeat_refreshes_during_silent_child_and_tracks_output_activity(tmp_path, monkeypatch):
    """A monotonic heartbeat lands every ``heartbeat_interval_seconds`` while
    the child is silent, and ``last_output_change_epoch`` only advances once
    the child actually produces output -- output growth is activity
    evidence, never inferred progress.

    Runs the real ``supervise()`` on a background thread so this test can
    poll ``status_path`` concurrently. ``signal.signal`` only works on a
    process's main thread, so it is neutralized here -- this test exercises
    heartbeat/output-tracking, not SIGTERM/cancel handling (which the
    existing supervisor tests and ``ProcessManager``'s own cancel path
    already cover on the real main-thread entry point)."""
    monkeypatch.setattr(worker_supervisor.signal, "signal", lambda *a, **k: None)
    status_path = tmp_path / "status.json"
    cancel_path = tmp_path / "cancel.json"
    stdout_path = tmp_path / "out.log"
    stderr_path = tmp_path / "err.log"
    marker = tmp_path / "go"

    script = (
        "import time, sys, os\n"
        f"while not os.path.exists({str(marker)!r}):\n"
        "    time.sleep(0.02)\n"
        "sys.stdout.write('x' * 40)\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.6)\n"
    )
    spec = {
        "argv": [sys.executable, "-c", script],
        "cwd": "/",
        "timeout_seconds": 10,
        "status_path": str(status_path),
        "cancel_path": str(cancel_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "heartbeat_interval_seconds": 0.1,
    }

    import threading

    thread = threading.Thread(target=worker_supervisor.supervise, args=(spec,), daemon=True)
    thread.start()
    try:
        silent_snapshots = []
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and len(silent_snapshots) < 3:
            if status_path.is_file():
                try:
                    payload = json.loads(status_path.read_text())
                except json.JSONDecodeError:
                    payload = {}
                if payload.get("state") == "running" and payload.get("heartbeat_seq"):
                    if (
                        not silent_snapshots
                        or payload["heartbeat_seq"] != silent_snapshots[-1]["heartbeat_seq"]
                    ):
                        silent_snapshots.append(payload)
            time.sleep(0.08)

        assert len(silent_snapshots) >= 2, "expected multiple heartbeats while child was silent"
        seqs = [s["heartbeat_seq"] for s in silent_snapshots]
        assert seqs == sorted(seqs) and seqs[0] < seqs[-1], "heartbeat_seq must be monotonic"
        assert len({s["last_output_change_epoch"] for s in silent_snapshots}) == 1, (
            "no output changed yet -- last_output_change_epoch must stay fixed"
        )
        assert all(s.get("stdout_bytes", 0) == 0 for s in silent_snapshots)

        marker.write_text("go")
        deadline = time.monotonic() + 6.0
        activity_seen = False
        while time.monotonic() < deadline:
            payload = json.loads(status_path.read_text())
            if payload.get("stdout_bytes", 0) > 0:
                activity_seen = True
                assert payload["last_output_change_epoch"] > silent_snapshots[0]["last_output_change_epoch"]
                break
            time.sleep(0.05)
        assert activity_seen, "expected stdout activity to be observed after the marker was written"

        thread.join(timeout=10)
        final = json.loads(status_path.read_text())
        assert final["state"] == "exited"
        assert final["exit_code"] == 0
        assert final["supervisor_pid_start_ticks"] is not None
    finally:
        marker.write_text("go")
        thread.join(timeout=5)


def test_supervisor_status_read_secure_modes_and_symlink_rejection(tmp_path):
    """``read_supervisor_status`` fails closed (returns ``{}``) on a
    symlink, an insecure permission mode, and malformed JSON -- and accepts
    a genuinely owner-only 0600 regular file."""
    real = tmp_path / "status.json"
    payload = {"state": "running", "heartbeat_seq": 3}
    fd = os.open(real, os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    assert process_launcher.read_supervisor_status(real) == payload

    symlink = tmp_path / "status_symlink.json"
    symlink.symlink_to(real)
    assert process_launcher.read_supervisor_status(symlink) == {}

    insecure = tmp_path / "status_insecure.json"
    fd = os.open(insecure, os.O_CREAT | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    if os.name == "nt":
        assert process_launcher.read_supervisor_status(insecure) == payload
    else:
        assert process_launcher.read_supervisor_status(insecure) == {}

    malformed = tmp_path / "status_malformed.json"
    fd = os.open(malformed, os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("{not json")
    assert process_launcher.read_supervisor_status(malformed) == {}

    missing = tmp_path / "does_not_exist.json"
    assert process_launcher.read_supervisor_status(missing) == {}


def test_supervisor_status_read_rejects_foreign_owner(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("Windows ownership is ACL-based, not st_uid-based")
    real = tmp_path / "status.json"
    fd = os.open(real, os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"state": "running"}, fh)
    real_getuid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_getuid + 1)
    assert process_launcher.read_supervisor_status(real) == {}


# --- derive_liveness_state ---------------------------------------------------


def test_derive_liveness_state_alive_fresh_heartbeat_and_recent_activity():
    now = 1_000_000.0
    result = process_launcher.derive_liveness_state(
        now_epoch=now,
        supervisor_alive=True,
        heartbeat_at_epoch=now - 1.0,
        last_output_change_epoch=now - 2.0,
    )
    assert result["liveness_state"] == "alive"


def test_derive_liveness_state_quiet_is_not_a_failure():
    now = 1_000_000.0
    result = process_launcher.derive_liveness_state(
        now_epoch=now,
        supervisor_alive=True,
        heartbeat_at_epoch=now - 1.0,
        last_output_change_epoch=now - 5000.0,
        warning_seconds=1800.0,
        lease_seconds=60.0,
        grace_seconds=120.0,
    )
    assert result["liveness_state"] == "quiet"
    assert result["heartbeat_age_seconds"] == pytest.approx(1.0)


def test_derive_liveness_state_stale_heartbeat_becomes_unresponsive_then_lost():
    now = 1_000_000.0
    lease, grace = 60.0, 120.0
    unresponsive = process_launcher.derive_liveness_state(
        now_epoch=now,
        supervisor_alive=True,
        heartbeat_at_epoch=now - (lease + 10),
        last_output_change_epoch=now - (lease + 10),
        lease_seconds=lease,
        grace_seconds=grace,
    )
    assert unresponsive["liveness_state"] == "unresponsive"

    lost = process_launcher.derive_liveness_state(
        now_epoch=now,
        supervisor_alive=True,
        heartbeat_at_epoch=now - (lease + grace + 10),
        last_output_change_epoch=now - (lease + grace + 10),
        lease_seconds=lease,
        grace_seconds=grace,
    )
    assert lost["liveness_state"] == "lost"


def test_derive_liveness_state_exact_dead_pid_is_always_lost():
    result = process_launcher.derive_liveness_state(
        now_epoch=1_000_000.0,
        supervisor_alive=False,
        heartbeat_at_epoch=1_000_000.0 - 1.0,
        last_output_change_epoch=1_000_000.0 - 1.0,
    )
    assert result["liveness_state"] == "lost"


def test_derive_liveness_state_never_returns_a_percentage_or_correctness_claim():
    for supervisor_alive in (True, False):
        result = process_launcher.derive_liveness_state(
            now_epoch=1.0,
            supervisor_alive=supervisor_alive,
            heartbeat_at_epoch=None,
            last_output_change_epoch=None,
        )
        assert result["liveness_state"] in process_launcher.LIVENESS_STATES
        assert "percent" not in result and "progress" not in result and "correct" not in result


# --- PID-reuse refusal --------------------------------------------------------


def test_pid_matches_refuses_a_mismatched_start_tick_even_for_a_live_pid():
    own_pid = os.getpid()
    real_ticks = process_launcher._pid_start_ticks(own_pid)
    assert real_ticks is not None
    assert process_launcher._pid_matches(own_pid, real_ticks) is True
    # A live PID whose recorded start ticks do not match must never be
    # treated as the same process -- this is exactly PID-reuse refusal.
    assert process_launcher._pid_matches(own_pid, real_ticks + 1) is False


# --- ProcessManager._finalize_isolated_request liveness escalation ----------


def _card(task_id: str = "TASK_B412", runner: str = "claude_worker_b412") -> dict:
    return {
        "task_id": task_id,
        "runner": runner,
        "topic": "coding",
        "status": "processing",
        "worker_status": "in_progress",
        "claimed_by": runner,
        "review_requested_by": "",
        "allowed_writes": ["out/result.txt"],
    }


def _show(card: dict):
    def show(task_id: str) -> dict:
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    return show


def _collision(**_kwargs) -> dict:
    return {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""}


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)


def _build_manager(tmp_path: Path, card: dict) -> process_launcher.ProcessManager:
    return process_launcher.ProcessManager(
        repo=tmp_path / "repo",
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(card),
        collision_guard=_collision,
        adapter_builder=lambda **_k: SimpleNamespace(argv=[], cwd=str(tmp_path), launchable=True, reason=""),
        isolation_enabled=True,
    )


def _seed_request(
    manager: process_launcher.ProcessManager,
    tmp_path: Path,
    card: dict,
    *,
    request_id: str,
    supervisor_pid: int,
    supervisor_ticks,
    supervisor_status: dict,
) -> None:
    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    status_path = process_dir / f"{request_id}.supervisor.json"
    metadata_path = process_dir / f"{request_id}.request.json"
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    cancel_path = process_dir / f"{request_id}.cancel.json"
    for p in (stdout_path, stderr_path):
        os.close(os.open(p, os.O_CREAT | os.O_WRONLY, 0o600))
    _write_status(status_path, supervisor_status)

    workspace_metadata = {
        "request_id": request_id,
        "repo": str(tmp_path / "repo"),
        "path": str(tmp_path / "workspace" / request_id),
        "home": str(tmp_path / "home" / request_id),
        "allowed_writes": list(card["allowed_writes"]),
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    worker_workspace.write_json_0600(metadata_path, {
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "model": None,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(cancel_path),
        "metadata_path": str(metadata_path),
        "validation": [],
        "sandbox_backend": "landlock",
        "workspace": workspace_metadata,
    })
    manager._append_event({
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": supervisor_pid,
        "pid_start_ticks": supervisor_ticks,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
    })


def test_supervisor_unresponsive_beyond_grace_is_finalized_as_lost_and_kills_exact_process_group(
    tmp_path, monkeypatch,
):
    """A supervisor whose exact PID+start-tick identity still exists but
    whose heartbeat lease AND recovery grace have both elapsed is escalated
    to "lost", its exact process group (and its child's) is terminated, and
    the task is routed to review/blocked -- never left pending."""
    card = _card()
    fake_supervisor = _spawn_sleeper()
    fake_child = _spawn_sleeper()
    try:
        manager = _build_manager(tmp_path, card)
        release_calls = []
        def fake_terminal_failure(
            repo, task_id, runner, substatus, *, evidence=None, request_id=""
        ):
            release_calls.append((task_id, runner, substatus))
            card.update({"status": "blocked", "worker_status": substatus, "claimed_by": runner})
            return {"ok": True}
        monkeypatch.setattr(
            process_launcher.task_engine, "mark_terminal_failure", fake_terminal_failure,
        )
        now = time.time()
        stale_heartbeat = now - (
            process_launcher.heartbeat_lease_seconds()
            + process_launcher.lost_recovery_grace_seconds()
            + 5.0
        )
        _seed_request(
            manager, tmp_path, card,
            request_id="req-unresponsive-lost",
            supervisor_pid=fake_supervisor.pid,
            supervisor_ticks=process_launcher._pid_start_ticks(fake_supervisor.pid),
            supervisor_status={
                "state": "running",
                "child_pid": fake_child.pid,
                "child_pid_start_ticks": process_launcher._pid_start_ticks(fake_child.pid),
                "heartbeat_at_epoch": stale_heartbeat,
                "last_output_change_epoch": stale_heartbeat,
                "started_at_epoch": stale_heartbeat,
            },
        )

        result = manager._finalize_isolated_request("req-unresponsive-lost")

        assert result is not None
        assert result["state"] == "worker_failed"
        assert result["liveness_lost"] is True
        assert "liveness_lost" in result["error"]
        assert release_calls == [(card["task_id"], card["runner"], "worker_failed")]
        assert _wait_dead(fake_supervisor), "hung supervisor process group must be terminated"
        assert _wait_dead(fake_child), "orphaned child process group must be terminated too"
    finally:
        _kill_if_alive(fake_supervisor)
        _kill_if_alive(fake_child)


def test_fresh_heartbeat_without_meaningful_progress_remains_active(
    tmp_path, monkeypatch,
):
    card = _card(task_id="TASK_STALL", runner="deepseek_worker_stall")
    fake_supervisor = _spawn_sleeper()
    terminated: list[tuple[int, float]] = []
    release_evidence: list[dict] = []
    try:
        manager = _build_manager(tmp_path, card)

        def fake_terminal_failure(
            repo, task_id, runner, substatus, *, evidence=None, request_id=""
        ):
            release_evidence.append(dict(evidence or {}))
            card.update({"status": "blocked", "worker_status": substatus})
            return {"ok": True}

        monkeypatch.setattr(
            process_launcher.task_engine, "mark_terminal_failure", fake_terminal_failure,
        )
        monkeypatch.setattr(
            process_launcher,
            "_terminate_process_group",
            lambda pid, grace_seconds: terminated.append((pid, grace_seconds)),
        )
        now = time.time()
        ticks = process_launcher._pid_start_ticks(fake_supervisor.pid)
        _seed_request(
            manager, tmp_path, card,
            request_id="req-meaningful-stall",
            supervisor_pid=fake_supervisor.pid,
            supervisor_ticks=ticks,
            supervisor_status={
                "state": "running",
                "heartbeat_at_epoch": now,
                "heartbeat_seq": 42,
                "last_output_change_epoch": now,
                "last_meaningful_progress_epoch": now - 635,
                "last_meaningful_phase": "tool_turn",
                "last_progress_sequence": 99,
                "last_meaningful_progress_sequence": 7,
                "stdout_bytes": 120,
                "stderr_bytes": 0,
                "child_pid": 0,
            },
        )

        result = manager._finalize_isolated_request("req-meaningful-stall")

        assert result is None
        assert terminated == []
        assert release_evidence == []
        assert fake_supervisor.poll() is None
    finally:
        _kill_if_alive(fake_supervisor)


def test_stall_detection_never_terminates_a_recycled_pid(tmp_path, monkeypatch):
    card = _card(task_id="TASK_PID_REUSE", runner="deepseek_worker_pid")
    fake_supervisor = _spawn_sleeper()
    terminated: list[int] = []
    try:
        manager = _build_manager(tmp_path, card)
        monkeypatch.setattr(
            process_launcher.task_engine,
            "mark_terminal_failure",
            lambda *args, **kwargs: {"ok": True},
        )
        monkeypatch.setattr(
            process_launcher,
            "_terminate_process_group",
            lambda pid, grace_seconds: terminated.append(pid),
        )
        now = time.time()
        real_ticks = process_launcher._pid_start_ticks(fake_supervisor.pid)
        assert real_ticks is not None
        _seed_request(
            manager, tmp_path, card,
            request_id="req-recycled-pid",
            supervisor_pid=fake_supervisor.pid,
            supervisor_ticks=real_ticks + 1,
            supervisor_status={
                "state": "running",
                "heartbeat_at_epoch": now,
                "last_output_change_epoch": now,
                "last_meaningful_progress_epoch": now - 3600,
                "child_pid": 0,
            },
        )

        manager._finalize_isolated_request("req-recycled-pid")

        assert terminated == []
        assert fake_supervisor.poll() is None
    finally:
        _kill_if_alive(fake_supervisor)


def test_quiet_worker_with_fresh_heartbeat_remains_active(tmp_path, monkeypatch):
    card = _card(task_id="TASK_QUIET", runner="deepseek_worker_quiet")
    fake_supervisor = _spawn_sleeper()
    terminated: list[int] = []
    try:
        manager = _build_manager(tmp_path, card)
        monkeypatch.setattr(
            process_launcher,
            "_terminate_process_group",
            lambda pid, grace_seconds: terminated.append(pid),
        )
        now = time.time()
        _seed_request(
            manager, tmp_path, card,
            request_id="req-quiet-below-grace",
            supervisor_pid=fake_supervisor.pid,
            supervisor_ticks=process_launcher._pid_start_ticks(fake_supervisor.pid),
            supervisor_status={
                "state": "running",
                "heartbeat_at_epoch": now,
                "last_output_change_epoch": now,
                "last_meaningful_progress_epoch": now - 10,
                "last_meaningful_phase": "request_accepted",
                "child_pid": 0,
            },
        )

        assert manager._finalize_isolated_request("req-quiet-below-grace") is None
        assert terminated == []
        assert fake_supervisor.poll() is None
    finally:
        _kill_if_alive(fake_supervisor)


def test_meaningful_progress_sequence_is_backward_compatible():
    assert process_launcher._meaningful_progress_sequence({
        "last_progress_sequence": 7,
    }) == 7
    assert process_launcher._meaningful_progress_sequence({
        "last_progress_sequence": 99,
        "last_meaningful_progress_sequence": 7,
    }) == 7


def test_live_supervisor_before_first_status_write_is_not_failed(tmp_path, monkeypatch):
    """A reconciler racing the supervisor's first status write must leave the
    exact live process and its processing task untouched."""
    card = _card()
    fake_supervisor = _spawn_sleeper()
    try:
        manager = _build_manager(tmp_path, card)
        release_calls = []
        monkeypatch.setattr(
            process_launcher.core,
            "release_launch",
            lambda *args, **kwargs: release_calls.append((args, kwargs)) or {"ok": True},
        )
        request_id = "req-live-before-status"
        _seed_request(
            manager, tmp_path, card,
            request_id=request_id,
            supervisor_pid=fake_supervisor.pid,
            supervisor_ticks=process_launcher._pid_start_ticks(fake_supervisor.pid),
            supervisor_status={"state": "starting"},
        )
        (tmp_path / "processes" / f"{request_id}.supervisor.json").unlink()

        result = manager._finalize_isolated_request(request_id)

        assert result is None
        assert release_calls == []
        assert fake_supervisor.poll() is None
        assert card["status"] == "processing"
    finally:
        _kill_if_alive(fake_supervisor)


def test_supervisor_crash_with_surviving_child_is_terminated_and_never_left_pending(tmp_path, monkeypatch):
    """The supervisor process is already gone (exact PID+ticks no longer
    match) while its child is still running -- the orphaned child's exact
    process group is terminated and the task is routed to review, never
    silently left in pending."""
    card = _card()
    fake_child = _spawn_sleeper()
    try:
        manager = _build_manager(tmp_path, card)
        release_calls = []
        def fake_terminal_failure(
            repo, task_id, runner, substatus, *, evidence=None, request_id=""
        ):
            release_calls.append((task_id, runner, substatus))
            card.update({"status": "blocked", "worker_status": substatus, "claimed_by": runner})
            return {"ok": True}
        monkeypatch.setattr(
            process_launcher.task_engine, "mark_terminal_failure", fake_terminal_failure,
        )
        dead_pid = 2_147_483_000  # far beyond any realistic live PID in this sandbox
        _seed_request(
            manager, tmp_path, card,
            request_id="req-crash-surviving-child",
            supervisor_pid=dead_pid,
            supervisor_ticks=999_999_999,
            supervisor_status={
                "state": "running",
                "child_pid": fake_child.pid,
                "child_pid_start_ticks": process_launcher._pid_start_ticks(fake_child.pid),
                "heartbeat_at_epoch": time.time(),
                "last_output_change_epoch": time.time(),
                "started_at_epoch": time.time(),
            },
        )

        result = manager._finalize_isolated_request("req-crash-surviving-child")

        assert result is not None
        assert result["state"] == "worker_failed"
        assert _wait_dead(fake_child), "orphaned surviving child must be terminated"
        assert release_calls, "a crashed supervisor must still route to review, never silently drop"
        assert all(reason != "pending" for _, _, reason in release_calls)
    finally:
        _kill_if_alive(fake_child)


# --- durable reconciliation: the B411 regression itself ---------------------


def test_successful_exit_reconciled_after_launcher_disappearance_runs_each_step_exactly_once(
    tmp_path, monkeypatch,
):
    """The exact B411 regression: the supervisor already wrote a clean
    ``exited``/``exit_code=0`` status, but nothing re-scanned it because the
    launching process is gone. ``task_reconciler.run_scan`` (== the
    reconciler daemon's one iteration) must finalize it through scope
    validation -> validation commands -> promotion -> ``taskctl review``,
    each exactly once."""
    card = _card()
    card.update({"status": "processing", "worker_status": "in_progress"})
    manager = _build_manager(tmp_path, card)

    calls = {"enforce_scope": 0, "run_validations": 0, "promote": 0, "mark_review": 0}

    def fake_enforce_scope(workspace, **_kwargs):
        calls["enforce_scope"] += 1
        return ["out/result.txt"]

    def fake_run_validations(workspace, commands, **_kwargs):
        calls["run_validations"] += 1
        return [{"command": c, "returncode": 0} for c in commands]

    def fake_promote(workspace, changed):
        calls["promote"] += 1
        return list(changed)

    def fake_mark_review(repo, task_id, runner, substatus, *, evidence=None):
        calls["mark_review"] += 1
        card.update({"status": "review", "worker_status": "review", "review_requested_by": runner})
        return {"ok": True, "substatus": substatus, "evidence": evidence}

    monkeypatch.setattr(process_launcher, "enforce_scope", fake_enforce_scope)
    monkeypatch.setattr(process_launcher, "run_validations", fake_run_validations)
    monkeypatch.setattr(process_launcher, "promote", fake_promote)
    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_review", fake_mark_review)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: True)

    dead_pid = 2_147_483_001
    _seed_request(
        manager, tmp_path, card,
        request_id="req-exited-orphaned",
        supervisor_pid=dead_pid,
        supervisor_ticks=999_999_998,
        supervisor_status={
            "state": "exited",
            "exit_code": 0,
            "child_pid": dead_pid,
            "child_pid_start_ticks": 999_999_998,
            "started_at_epoch": time.time() - 120,
            "finished_at_epoch": time.time() - 60,
            "heartbeat_seq": 4,
            "heartbeat_at_epoch": time.time() - 60,
        },
    )

    result = task_reconciler.run_scan(manager)
    assert result["ok"] is True

    events = manager._request_events("req-exited-orphaned")
    latest = events[-1]
    assert latest["state"] == "review_ready"
    assert card["status"] == "review"
    # An empty validation contract cannot mutate the workspace, so the bounded
    # initial scope check is reused instead of spawning a redundant second Git
    # scan. The review transition runs exactly once; canonical promotion is
    # deferred to the coordinator's accept_review operation.
    assert calls == {"enforce_scope": 1, "run_validations": 0, "promote": 0, "mark_review": 1}

    # --- idempotent double scan: a second scan must be a total no-op ---
    result2 = task_reconciler.run_scan(manager)
    assert result2["ok"] is True
    assert calls == {"enforce_scope": 1, "run_validations": 0, "promote": 0, "mark_review": 1}
    events_after = manager._request_events("req-exited-orphaned")
    assert len(events_after) == len(events)


@pytest.mark.parametrize(
    "supervisor_state,expected_terminal",
    [
        ("timed_out", "worker_failed"),
        ("cancelled", "cancelled"),
        ("spawn_failed", "worker_failed"),
    ],
)
def test_non_exited_terminal_states_route_to_blocked_never_pending_and_enqueue_one_release(
    tmp_path, monkeypatch, supervisor_state, expected_terminal,
):
    card = _card()
    manager = _build_manager(tmp_path, card)
    release_calls = []

    def fake_release(
        repo, task_id, runner, substatus, *, evidence=None, request_id=""
    ):
        release_calls.append((task_id, runner, substatus))
        card.update({"status": "blocked", "worker_status": substatus, "claimed_by": runner})
        return {"ok": True}

    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_failure", fake_release)
    dead_pid = 2_147_483_010
    _seed_request(
        manager, tmp_path, card,
        request_id=f"req-{supervisor_state}",
        supervisor_pid=dead_pid,
        supervisor_ticks=999_999_997,
        supervisor_status={
            "state": supervisor_state,
            "exit_code": 1 if supervisor_state != "cancelled" else None,
            "started_at_epoch": time.time() - 30,
            "finished_at_epoch": time.time() - 5,
        },
    )

    result = manager._finalize_isolated_request(f"req-{supervisor_state}")

    assert result["state"] == expected_terminal
    assert len(release_calls) == 1
    assert release_calls[0][2] != "pending"
    assert card["status"] == "blocked"
    assert card["worker_status"] == expected_terminal

    # A duplicate scan must be a pure no-op: already terminal, nothing re-runs.
    again = manager._finalize_isolated_request(f"req-{supervisor_state}")
    assert again["state"] == expected_terminal
    assert len(release_calls) == 1


def test_structured_provider_auth_failure_blocks_without_review_candidate(
    tmp_path, monkeypatch,
):
    card = _card()
    manager = _build_manager(tmp_path, card)
    blocked_calls = []
    runtime_auth_failures = []

    def fake_launch_failed(repo, task_id, runner, *, reason, request_id=""):
        blocked_calls.append((task_id, runner, reason, request_id))
        card.update({"status": "blocked", "worker_status": "launch_failed"})
        return {"ok": True}

    monkeypatch.setattr(process_launcher.task_engine, "mark_launch_failed", fake_launch_failed)
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_review",
        lambda *a, **k: pytest.fail("provider auth failure must not enter review"),
    )
    monkeypatch.setattr(process_launcher, "cleanup_workspace", lambda *a, **k: None)
    monkeypatch.setattr(
        process_launcher.claude_auth,
        "record_runtime_auth_failure",
        lambda **kwargs: runtime_auth_failures.append(kwargs),
    )
    request_id = "req-provider-auth"
    _seed_request(
        manager,
        tmp_path,
        card,
        request_id=request_id,
        supervisor_pid=2_147_483_011,
        supervisor_ticks=999_999_996,
        supervisor_status={
            "state": "exited",
            "exit_code": 1,
            "started_at_epoch": time.time() - 5,
            "finished_at_epoch": time.time() - 1,
        },
    )
    stdout_path = tmp_path / "processes" / f"{request_id}.stdout.log"
    stdout_path.write_text(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 401,
                "terminal_reason": "api_error",
                "result": "secret-bearing provider text must not persist",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "launch_failed"
    assert result["workspace_retained"] is False
    # NF-2026-00326 inverted this assertion. A bare 401 with no body naming a
    # cause used to be reported as provider_authentication_failed, which sends
    # an operator to re-authenticate a credential that may be perfectly valid.
    # The refusal is now recorded honestly: the provider refused, and the
    # response did not say why.
    assert (
        result["error"]
        == "provider_refused:http_status=401:cause_not_distinguished_by_response"
    )
    assert "secret-bearing" not in json.dumps(result)
    assert blocked_calls == [
        (
            card["task_id"],
            card["runner"],
            "provider_refused:http_status=401:cause_not_distinguished_by_response",
            request_id,
        )
    ]
    assert runtime_auth_failures == [
        {"http_status": 401, "error_code": "", "session_id": ""}
    ]
    assert card["status"] == "blocked"
    assert manager.status(request_id)["liveness"] == {}


@pytest.mark.parametrize("status,worker_status", [("review", "review_ready"), ("finished", "done")])
def test_release_exact_is_idempotent_when_canonical_task_is_already_terminal(
    tmp_path, monkeypatch, status, worker_status,
):
    card = _card()
    card.update({"status": status, "worker_status": worker_status})
    manager = _build_manager(tmp_path, card)
    release_calls = []
    monkeypatch.setattr(
        process_launcher.core,
        "release_launch",
        lambda *args, **kwargs: release_calls.append((args, kwargs)),
    )

    result = manager._release_exact(
        {"task_id": card["task_id"], "runner": card["runner"]},
        "worker_failed",
    )

    assert result == {
        "ok": True,
        "idempotent_noop": True,
        "canonical_lifecycle": "review" if status == "review" else "finished",
    }
    assert release_calls == []


# --- task_reconciler.py CLI/daemon plumbing ---------------------------------


def _single_flight_service_process(repo, entered, release_fd):
    def _scan(*_args, **_kwargs):
        entered.send(os.getpid())
        os.read(release_fd, 1)
        return {"ok": True, "finalized": 0, "watched": 0}

    task_reconciler.run_scan = _scan
    service = task_reconciler.ReconcilerService(Path(repo), scan_interval_seconds=5)
    service.start()
    service._thread.join()


def _signal_wake_daemon_process(repo, entered):
    def _scan(*_args, **_kwargs):
        entered.send(os.getpid())
        return {"ok": True, "finalized": 0, "watched": 0}

    task_reconciler.run_scan = _scan
    task_reconciler.run_daemon(repo=Path(repo), scan_interval_seconds=3600)


@pytest.mark.skipif(os.name == "nt", reason="fork-based deterministic process authority test")
def test_two_services_single_flight_and_standby_takes_over_after_owner_exit(tmp_path):
    repo = tmp_path / "repo_process_authority"
    (repo / ".aiworkhub/runtime/process_logs").mkdir(parents=True)
    ctx = multiprocessing.get_context("fork")
    entered_reader, entered_writer = ctx.Pipe(duplex=False)
    release_reader, release_writer = os.pipe()
    processes = [
        ctx.Process(
            target=_single_flight_service_process,
            args=(repo, entered_writer, release_reader),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        assert entered_reader.poll(5)
        owner_pid = entered_reader.recv()
        assert not entered_reader.poll(0.5)
        # The authority is an OS lock, not a scan-long SQLite transaction:
        # ordinary manager/task-store reconciliation remains available.
        concurrent_result = process_launcher.ProcessManager(repo=repo).reconcile(
            include_gc=False
        )
        assert isinstance(concurrent_result, dict)
        owner = next(process for process in processes if process.pid == owner_pid)
        standby = next(process for process in processes if process.pid != owner_pid)
        assert owner.is_alive() and standby.is_alive()

        # Killing the exact holder releases the kernel lock. The passive MCP
        # service retries acquisition and becomes owner without intervention.
        # This assertion is about kernel-lock release, not graceful signal
        # handling (covered separately below). SIGKILL also prevents an
        # unrelated inherited test handler from keeping a fork child alive.
        owner.kill()
        owner.join(timeout=5)
        assert not owner.is_alive()
        assert entered_reader.poll(5)
        assert entered_reader.recv() == standby.pid
    finally:
        os.write(release_writer, b"x")
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        os.close(release_reader)
        os.close(release_writer)


@pytest.mark.skipif(os.name == "nt", reason="fork and SIGTERM regression")
def test_daemon_sigterm_wakes_long_wait_and_standby_takes_over(tmp_path):
    repo = tmp_path / "repo_signal_takeover"
    (repo / ".aiworkhub/runtime/process_logs").mkdir(parents=True)
    ctx = multiprocessing.get_context("fork")
    entered_reader, entered_writer = ctx.Pipe(duplex=False)
    owner = ctx.Process(target=_signal_wake_daemon_process, args=(repo, entered_writer))
    release_reader, release_writer = os.pipe()
    standby = ctx.Process(
        target=_single_flight_service_process,
        args=(repo, entered_writer, release_reader),
    )
    owner.start()
    try:
        assert entered_reader.poll(5)
        assert entered_reader.recv() == owner.pid
        standby.start()
        assert not entered_reader.poll(0.5)

        started = time.monotonic()
        os.kill(owner.pid, signal.SIGTERM)
        owner.join(timeout=3)
        assert not owner.is_alive(), "SIGTERM must wake the 3600-second wait"
        assert time.monotonic() - started < 3
        assert entered_reader.poll(5)
        assert entered_reader.recv() == standby.pid
    finally:
        os.write(release_writer, b"x")
        for process in (owner, standby):
            process.join(timeout=5)
            if process.is_alive():
                # A full-suite predecessor can leave the reconciler's graceful
                # SIGTERM handler installed in a fork child.  Test teardown is
                # final authority for these exact owned children, so use
                # SIGKILL just like the sibling single-flight regression.
                process.kill()
                process.join(timeout=5)
            assert not process.is_alive()
        os.close(release_reader)
        os.close(release_writer)


def test_single_instance_lock_rejects_a_concurrent_holder(tmp_path):
    lock_path = tmp_path / "reconciler.lock"
    with task_reconciler.single_instance_lock(lock_path):
        with pytest.raises(task_reconciler.ReconcilerLockHeld):
            with task_reconciler.single_instance_lock(lock_path):
                pass
    # Lock is released afterwards -- a fresh acquire must succeed.
    with task_reconciler.single_instance_lock(lock_path):
        pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory replacement regression")
def test_replaced_lock_parent_cannot_admit_a_second_process(tmp_path):
    repo = tmp_path / "repo"
    lock_path = repo / task_reconciler.LOCK_REL_PATH
    lock_path.parent.mkdir(parents=True)
    ctx = multiprocessing.get_context("fork")
    acquired_reader, acquired_writer = ctx.Pipe(duplex=False)
    release_reader, release_writer = os.pipe()

    def _hold_authority():
        with task_reconciler.single_instance_lock(lock_path):
            acquired_writer.send("owner")
            os.read(release_reader, 1)

    owner = ctx.Process(target=_hold_authority)
    owner.start()
    try:
        assert acquired_reader.poll(5)
        assert acquired_reader.recv() == "owner"
        displaced = lock_path.parent.with_name("locks-displaced")
        lock_path.parent.rename(displaced)
        lock_path.parent.mkdir()

        def _try_replacement():
            try:
                with task_reconciler.single_instance_lock(lock_path):
                    acquired_writer.send("entered")
            except task_reconciler.ReconcilerLockHeld:
                acquired_writer.send("held")

        contender = ctx.Process(target=_try_replacement)
        contender.start()
        contender.join(timeout=5)
        assert not contender.is_alive()
        assert acquired_reader.poll(2)
        assert acquired_reader.recv() == "held"
    finally:
        os.write(release_writer, b"x")
        owner.join(timeout=5)
        if owner.is_alive():
            owner.terminate()
            owner.join(timeout=5)
        os.close(release_reader)
        os.close(release_writer)

    with task_reconciler.single_instance_lock(lock_path):
        pass


def test_single_instance_lock_rejects_path_replaced_after_lock(tmp_path, monkeypatch):
    lock_path = tmp_path / "reconciler.lock"
    real_lock_fd = task_reconciler.lock_fd

    def _lock_then_replace(fd, *, blocking):
        real_lock_fd(fd, blocking=blocking)
        lock_path.unlink()
        replacement_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(replacement_fd)

    monkeypatch.setattr(task_reconciler, "lock_fd", _lock_then_replace)
    with pytest.raises(task_reconciler.ReconcilerLockUnsafe, match="lock_unsafe"):
        with task_reconciler.single_instance_lock(lock_path):
            pass

    monkeypatch.setattr(task_reconciler, "lock_fd", real_lock_fd)
    with task_reconciler.single_instance_lock(lock_path):
        pass


def test_single_instance_lock_rejects_hardlink_without_modifying_source(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("do-not-touch", encoding="utf-8")
    lock_path = tmp_path / "reconciler.lock"
    try:
        os.link(victim, lock_path)
    except (AttributeError, NotImplementedError, OSError) as exc:
        pytest.skip(f"os.link unsupported: {exc}")

    with pytest.raises(task_reconciler.ReconcilerLockUnsafe, match="lock_unsafe"):
        with task_reconciler.single_instance_lock(lock_path):
            pass
    assert victim.read_text(encoding="utf-8") == "do-not-touch"


def test_single_instance_lock_uses_platform_owner_predicate(tmp_path, monkeypatch):
    lock_path = tmp_path / "reconciler.lock"
    observed = []

    def reject_owner(metadata):
        observed.append(metadata.st_mode)
        return False

    monkeypatch.setattr(
        task_reconciler, "stat_owned_by_current_user", reject_owner
    )

    with pytest.raises(task_reconciler.ReconcilerLockUnsafe, match="lock_unsafe"):
        with task_reconciler.single_instance_lock(lock_path):
            pass

    assert observed


def test_single_instance_lock_rejects_symlink_without_nofollow_or_modifying_target(
    tmp_path, monkeypatch
):
    victim = tmp_path / "victim"
    victim.write_text("do-not-touch", encoding="utf-8")
    lock_path = tmp_path / "reconciler.lock"
    try:
        lock_path.symlink_to(victim)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink unsupported: {exc}")
    monkeypatch.delattr(task_reconciler.os, "O_NOFOLLOW", raising=False)

    with pytest.raises(task_reconciler.ReconcilerLockUnsafe, match="lock_unsafe"):
        with task_reconciler.single_instance_lock(lock_path):
            pass
    assert victim.read_text(encoding="utf-8") == "do-not-touch"


def test_unsafe_lock_path_is_unhealthy_not_standby(tmp_path, monkeypatch):
    repo = tmp_path / "repo_unsafe_authority"
    lock_path = repo / task_reconciler.LOCK_REL_PATH
    lock_path.parent.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("do-not-touch", encoding="utf-8")
    try:
        lock_path.symlink_to(victim)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink unsupported: {exc}")
    service = task_reconciler.ReconcilerService(repo, scan_interval_seconds=5)
    monkeypatch.setattr(
        service._stop_event,
        "wait",
        lambda _seconds: service._stop_event.set() or True,
    )

    service._loop()

    health = service.health()
    assert health["ok"] is False
    assert health["authority_state"] == "acquisition_failed"
    assert health["standby"] is False
    assert "lock_unsafe" in health["last_acquisition_error"]
    assert victim.read_text(encoding="utf-8") == "do-not-touch"


def test_write_status_rejects_substituted_mkstemp_path_and_preserves_status(
    tmp_path, monkeypatch
):
    task_reconciler.write_status(tmp_path, {"scan_finished_epoch": 123.0})
    target = task_reconciler.status_path(tmp_path)
    victim = tmp_path / "victim-status"
    victim.write_text("do-not-touch", encoding="utf-8")
    real_mkstemp = task_reconciler.tempfile.mkstemp
    attacked_paths = []

    def _substitute_created_path(*args, **kwargs):
        fd, created_path = real_mkstemp(*args, **kwargs)
        attacked_paths.append(created_path)
        os.unlink(created_path)
        try:
            os.link(victim, created_path)
        except (AttributeError, NotImplementedError, OSError) as exc:
            os.close(fd)
            pytest.skip(f"os.link unsupported: {exc}")
        return fd, created_path

    monkeypatch.setattr(task_reconciler.tempfile, "mkstemp", _substitute_created_path)
    task_reconciler.write_status(tmp_path, {"scan_finished_epoch": 456.0})

    assert victim.read_text(encoding="utf-8") == "do-not-touch"
    assert attacked_paths and not Path(attacked_paths[0]).exists()
    assert task_reconciler.read_status(tmp_path)["scan_finished_epoch"] == 123.0

    monkeypatch.setattr(task_reconciler.tempfile, "mkstemp", real_mkstemp)
    task_reconciler.write_status(tmp_path, {"scan_finished_epoch": 789.0})
    assert target.exists()
    assert task_reconciler.read_status(tmp_path)["scan_finished_epoch"] == 789.0


def test_run_once_cli_is_bounded_json_and_idempotent_on_an_empty_repo(tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / ".aiworkhub/runtime/process_logs").mkdir(parents=True)
    rc = task_reconciler.main(["run-once", "--repo", str(repo)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert "scanned_at" in payload

    rc2 = task_reconciler.main(["run-once", "--repo", str(repo)])
    assert rc2 == 0
    payload2 = json.loads(capsys.readouterr().out)
    assert payload2["ok"] is True


def test_status_cli_reports_lock_presence_read_only(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    rc = task_reconciler.main(["status", "--repo", str(repo)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["lock_present"] is False
    assert not (repo / task_reconciler.LOCK_REL_PATH).exists()


def test_read_status_rejects_symlink_and_insecure_mode(tmp_path):
    target = task_reconciler.status_path(tmp_path)
    target.parent.mkdir(parents=True)
    foreign = tmp_path / "foreign-status.json"
    foreign.write_text('{"ok": true}', encoding="utf-8")
    target.symlink_to(foreign)
    assert task_reconciler.read_status(tmp_path) == {}

    target.unlink()
    fd = os.open(target, os.O_CREAT | os.O_WRONLY, 0o666)
    os.write(fd, b'{"ok": true}')
    os.close(fd)
    assert task_reconciler.read_status(tmp_path) == {}


def test_failed_owner_pass_preserves_attempted_gc_mode(tmp_path, monkeypatch):
    repo = tmp_path / "repo_failed_gc"
    (repo / ".aiworkhub/runtime/process_logs").mkdir(parents=True)
    service = task_reconciler.ReconcilerService(repo, scan_interval_seconds=5)
    monkeypatch.setattr(
        task_reconciler,
        "run_scan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("scan failed")),
    )
    service._stop_event.set()
    service._stop_event.clear()
    monkeypatch.setattr(service._stop_event, "wait", lambda _seconds: True)

    service._run_as_owner(max_iterations=1)

    record = task_reconciler.read_status(repo)
    assert record["last_error"] == "RuntimeError:scan failed"
    assert record["gc_included"] is True
    assert record["last_completed_scan"]["gc_included"] is True


def test_daemon_runs_bounded_iterations_and_scans_repeatedly(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".aiworkhub/runtime/process_logs").mkdir(parents=True)
    scans = []
    rc = task_reconciler.run_daemon(
        repo=repo,
        scan_interval_seconds=5,
        max_iterations=3,
        on_scan=lambda result: scans.append(result),
    )
    assert rc == 0
    assert len(scans) == 3
    assert all(scan["ok"] for scan in scans)


def test_bounded_daemon_restores_process_signal_handlers(tmp_path):
    repo = tmp_path / "repo_signal_restore"
    (repo / ".aiworkhub/runtime/process_logs").mkdir(parents=True)
    before = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }

    assert task_reconciler.run_daemon(repo=repo, max_iterations=1) == 0

    assert signal.getsignal(signal.SIGTERM) is before[signal.SIGTERM]
    assert signal.getsignal(signal.SIGINT) is before[signal.SIGINT]


def test_daemon_lock_unsafe_fails_closed_without_scanning(tmp_path, monkeypatch):
    repo = tmp_path / "repo_unsafe_lock"
    (repo / ".aiworkhub/runtime/process_logs").mkdir(parents=True)
    scans = []

    def _unsafe(_lock_path):
        raise task_reconciler.ReconcilerLockUnsafe("reconciler_lock_unsafe:test")

    monkeypatch.setattr(task_reconciler, "single_instance_lock", _unsafe)
    monkeypatch.setattr(
        task_reconciler,
        "run_scan",
        lambda *_args, **_kwargs: scans.append(True),
    )

    rc = task_reconciler.run_daemon(repo=repo, on_scan=lambda result: scans.append(result))

    assert rc == 4
    assert scans == []
    record = task_reconciler.read_status(repo)
    assert record["authority_state"] == "acquisition_failed"
    assert record["acquisition_state"] == "failed"
    assert record["scan_in_progress"] is False
    assert record["owner_pid"] is None
    assert record["last_error"] == "reconciler_lock_unsafe:test"


def test_repo_bound_reconciler_service_is_idempotent_and_stoppable(tmp_path, monkeypatch):
    repo = tmp_path / "repo_service"
    (repo / ".aiworkhub/runtime/process_logs").mkdir(parents=True)
    scans = []
    monkeypatch.setattr(
        task_reconciler,
        "run_scan",
        lambda manager=None, repo=None, include_gc=True: scans.append(str(repo)) or {"ok": True},
    )
    service = task_reconciler.ensure_started(repo)
    again = task_reconciler.ensure_started(repo)
    assert service is again
    deadline = time.time() + 2
    while not scans and time.time() < deadline:
        time.sleep(0.01)
    assert scans == [str(repo.resolve())]
    assert service.health()["running"] is True
    assert task_reconciler.stop_reconciler(repo) is True
    assert task_reconciler.reconciler_health(repo)["running"] is False


def test_daemon_writes_lifecycle_transitions_with_writes_enabled(tmp_path, monkeypatch):
    """The reconciler daemon itself (not a never-shipped systemd unit file)
    is what must be able to write lifecycle transitions and see isolated
    worktrees: with AIWORKHUB_ALLOW_WRITES=1 set, a scan drives a terminal
    request through to review_ready exactly as task_reconciler.run_scan
    already proves above."""
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    card = _card()
    card.update({"status": "processing", "worker_status": "in_progress"})
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(
        process_launcher,
        "enforce_scope",
        lambda workspace, **_kwargs: ["out/result.txt"],
    )
    monkeypatch.setattr(
        process_launcher, "run_validations", lambda workspace, commands, **_kw: [
            {"command": c, "returncode": 0} for c in commands
        ],
    )
    monkeypatch.setattr(process_launcher, "promote", lambda workspace, changed: list(changed))
    monkeypatch.setattr(
        process_launcher.task_engine, "mark_terminal_review",
        lambda repo, task_id, runner, substatus, evidence=None: (
            card.update({"status": "review", "worker_status": "review", "review_requested_by": runner})
            or {"ok": True}
        ),
    )
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: True)

    dead_pid = 2_147_483_002
    _seed_request(
        manager, tmp_path, card,
        request_id="req-daemon-writes-lifecycle",
        supervisor_pid=dead_pid,
        supervisor_ticks=999_999_996,
        supervisor_status={
            "state": "exited",
            "exit_code": 0,
            "child_pid": dead_pid,
            "child_pid_start_ticks": 999_999_996,
            "started_at_epoch": time.time() - 60,
            "finished_at_epoch": time.time() - 10,
            "heartbeat_seq": 2,
            "heartbeat_at_epoch": time.time() - 10,
        },
    )

    result = task_reconciler.run_scan(manager)
    assert result["ok"] is True
    events = manager._request_events("req-daemon-writes-lifecycle")
    assert events[-1]["state"] == "review_ready"
    assert card["status"] == "review"


def _pid_evidence(
    verdict: process_launcher.PidIdentityVerdict,
) -> process_launcher.PidIdentityEvidence:
    return process_launcher.PidIdentityEvidence(
        verdict=verdict,
        pid=123,
        expected_start_ticks=456,
        observed_start_ticks=456 if verdict is process_launcher.PidIdentityVerdict.MATCH else None,
        attempts=1,
        operation="test",
    )


def test_persisted_watcher_bounds_unknown_and_cleans_up_without_finalizing(monkeypatch):
    identity_calls = []
    sleep_calls = []
    finalizer_calls = []

    def unknown_identity(pid, ticks):
        identity_calls.append((pid, ticks))
        return _pid_evidence(process_launcher.PidIdentityVerdict.UNKNOWN)

    monkeypatch.setattr(process_launcher, "_pid_identity_evidence", unknown_identity)
    monkeypatch.setattr(
        process_launcher.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )
    manager = SimpleNamespace(
        _lock=contextlib.nullcontext(),
        _watching={"request-unknown"},
        _finalize_after_process_exit=lambda request_id: finalizer_calls.append(request_id),
    )

    process_launcher.ProcessManager._watch_persisted_request(
        manager,
        "request-unknown",
        123,
        456,
    )

    limit = process_launcher._PERSISTED_WATCH_UNKNOWN_MAX_CONSECUTIVE
    assert identity_calls == [(123, 456)] * limit
    assert sleep_calls == [0.2] * (limit - 1)
    assert finalizer_calls == []
    assert manager._watching == set()


def test_persisted_watcher_match_resets_unknown_streak(monkeypatch):
    limit = process_launcher._PERSISTED_WATCH_UNKNOWN_MAX_CONSECUTIVE
    verdicts = iter(
        [process_launcher.PidIdentityVerdict.UNKNOWN] * (limit - 1)
        + [process_launcher.PidIdentityVerdict.MATCH]
        + [process_launcher.PidIdentityVerdict.UNKNOWN] * limit
    )
    sleep_calls = []
    finalizer_calls = []
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _pid_evidence(next(verdicts)),
    )
    monkeypatch.setattr(
        process_launcher.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )
    manager = SimpleNamespace(
        _lock=contextlib.nullcontext(),
        _watching={"request-reset"},
        _finalize_after_process_exit=lambda request_id: finalizer_calls.append(request_id),
    )

    process_launcher.ProcessManager._watch_persisted_request(
        manager,
        "request-reset",
        123,
        456,
    )

    assert len(sleep_calls) == (limit - 1) + 1 + (limit - 1)
    assert finalizer_calls == []
    assert manager._watching == set()


def test_persisted_watcher_finalizes_only_on_mismatch(monkeypatch):
    finalizer_calls = []
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _pid_evidence(process_launcher.PidIdentityVerdict.MISMATCH),
    )
    monkeypatch.setattr(
        process_launcher.time,
        "sleep",
        lambda _seconds: pytest.fail("MISMATCH must not sleep"),
    )
    manager = SimpleNamespace(
        _lock=contextlib.nullcontext(),
        _watching={"request-mismatch"},
        _finalize_after_process_exit=lambda request_id: finalizer_calls.append(request_id),
    )

    process_launcher.ProcessManager._watch_persisted_request(
        manager,
        "request-mismatch",
        123,
        456,
    )

    assert finalizer_calls == ["request-mismatch"]
    assert manager._watching == set()


def test_reconciler_retries_unknown_later_and_starts_exactly_one_watcher(monkeypatch):
    candidate = {
        "state": "running",
        "pid": 123,
        "pid_start_ticks": 456,
        "metadata_path": "metadata.json",
    }
    verdicts = iter(
        [
            process_launcher.PidIdentityVerdict.UNKNOWN,
            process_launcher.PidIdentityVerdict.MATCH,
            process_launcher.PidIdentityVerdict.MATCH,
        ]
    )
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _pid_evidence(next(verdicts)),
    )
    threads = []

    class FakeThread:
        def __init__(self, *, target, args, name, daemon):
            self.target = target
            self.args = args
            self.name = name
            self.daemon = daemon

        def start(self):
            threads.append(self)

    monkeypatch.setattr(process_launcher.threading, "Thread", FakeThread)
    finalizer_calls = []
    watch_target = lambda *_args: None
    manager = SimpleNamespace(
        _lock=contextlib.nullcontext(),
        _watching=set(),
        _live={},
        _latest_by_request=lambda: {"request-retry": candidate},
        _watch_persisted_request=watch_target,
        _finalize_after_process_exit=lambda request_id: finalizer_calls.append(request_id),
    )

    first = process_launcher.ProcessManager._reconcile_persisted_requests(manager)
    second = process_launcher.ProcessManager._reconcile_persisted_requests(manager)
    third = process_launcher.ProcessManager._reconcile_persisted_requests(manager)

    assert first == {"watched": 0, "finalized": 0}
    assert second == {"watched": 1, "finalized": 0}
    assert third == {"watched": 0, "finalized": 0}
    assert len(threads) == 1
    assert threads[0].target is watch_target
    assert threads[0].args == ("request-retry", 123, 456)
    assert manager._watching == {"request-retry"}
    assert finalizer_calls == []


@pytest.mark.parametrize("state", sorted(process_launcher.FINALIZATION_PENDING_STATES))
def test_reconciler_finalization_pending_bypasses_identity_and_finalizes_once(
    monkeypatch,
    state,
):
    candidate = {
        "state": state,
        "pid": 123,
        "pid_start_ticks": None,
    }

    def identity_must_not_run(_pid, _ticks):
        raise AssertionError("finalization-pending entries must bypass PID identity")

    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        identity_must_not_run,
    )
    finalizer_calls = []
    manager = SimpleNamespace(
        _latest_by_request=lambda: {"request-pending": candidate},
        _finalize_after_process_exit=lambda request_id: (
            finalizer_calls.append(request_id) or {"state": "review_ready"}
        ),
    )

    result = process_launcher.ProcessManager._reconcile_persisted_requests(manager)

    assert result == {"watched": 0, "finalized": 1}
    assert finalizer_calls == ["request-pending"]


def test_reconciler_active_unknown_retains_request_without_side_effects(monkeypatch):
    candidate = {
        "state": "running",
        "pid": 123,
        "pid_start_ticks": 456,
        "metadata_path": "metadata.json",
    }
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _pid_evidence(process_launcher.PidIdentityVerdict.UNKNOWN),
    )
    monkeypatch.setattr(
        process_launcher.threading,
        "Thread",
        lambda *_args, **_kwargs: pytest.fail("UNKNOWN must not start a watcher"),
    )

    def finalizer_must_not_run(_request_id):
        raise AssertionError("UNKNOWN must not enter finalization side effects")

    manager = SimpleNamespace(
        _watching=set(),
        _live={},
        _latest_by_request=lambda: {"request-active-unknown": candidate},
        _finalize_after_process_exit=finalizer_must_not_run,
    )

    result = process_launcher.ProcessManager._reconcile_persisted_requests(manager)

    assert result == {"watched": 0, "finalized": 0}
    assert candidate["state"] == "running"
    assert manager._watching == set()


def test_reconciler_active_mismatch_finalizes_once(monkeypatch):
    candidate = {
        "state": "running",
        "pid": 123,
        "pid_start_ticks": 456,
        "metadata_path": "metadata.json",
    }
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _pid_evidence(process_launcher.PidIdentityVerdict.MISMATCH),
    )
    finalizer_calls = []
    manager = SimpleNamespace(
        _watching=set(),
        _live={},
        _latest_by_request=lambda: {"request-active-mismatch": candidate},
        _finalize_after_process_exit=lambda request_id: (
            finalizer_calls.append(request_id) or {"state": "worker_failed"}
        ),
    )

    result = process_launcher.ProcessManager._reconcile_persisted_requests(manager)

    assert result == {"watched": 0, "finalized": 1}
    assert finalizer_calls == ["request-active-mismatch"]


@pytest.mark.parametrize("status_kind", ["active", "missing"])
def test_finalizer_pid_unknown_defers_without_side_effects_then_recovers(
    monkeypatch,
    tmp_path,
    status_kind,
):
    card = _card(task_id="TASK_W1_FINALIZE", runner="claude_worker_w1")
    manager = _build_manager(tmp_path, card)
    request_id = "request-w1-finalize"
    now = time.time()
    _seed_request(
        manager,
        tmp_path,
        card,
        request_id=request_id,
        supervisor_pid=123,
        supervisor_ticks=456,
        supervisor_status={
            "state": "running",
            "heartbeat_at_epoch": now,
            "last_output_change_epoch": now,
            "started_at_epoch": now,
        },
    )
    if status_kind == "missing":
        (tmp_path / "processes" / f"{request_id}.supervisor.json").unlink()
    verdict = {"value": process_launcher.PidIdentityVerdict.UNKNOWN}

    def identity(pid, _ticks):
        if int(pid or 0) == 123:
            return _pid_evidence(verdict["value"])
        return _pid_evidence(process_launcher.PidIdentityVerdict.MISMATCH)

    bridge_calls = []
    release_calls = []
    monkeypatch.setattr(process_launcher, "_pid_identity_evidence", identity)
    monkeypatch.setattr(
        manager,
        "_publish_bridge_cancellation_before_finalization",
        lambda request_id_arg, _live: bridge_calls.append(request_id_arg) or "",
    )
    monkeypatch.setattr(
        manager,
        "_terminal_failure_exact",
        lambda metadata, substatus, *, request_id, evidence: (
            release_calls.append((request_id, substatus)) or {"ok": True}
        ),
    )
    monkeypatch.setattr(
        manager,
        "_record_usage",
        lambda *_args, **_kwargs: ({}, False, ""),
    )
    monkeypatch.setattr(
        process_launcher,
        "_terminate_process_group",
        lambda *_args, **_kwargs: pytest.fail("UNKNOWN must not signal"),
    )
    monkeypatch.setattr(
        process_launcher,
        "cleanup_workspace",
        lambda *_args, **_kwargs: pytest.fail("UNKNOWN must retain workspace"),
    )
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        lambda *_args, **_kwargs: pytest.fail("UNKNOWN must not release/callback"),
    )
    before = manager._request_events(request_id)

    unknown = manager._finalize_after_process_exit(request_id)

    assert unknown["state"] == "running"
    assert unknown["reconciliation_deferred"] == "pid_identity_unknown"
    assert unknown["workspace_retained"] is True
    assert manager._request_events(request_id) == before
    assert bridge_calls == []
    assert release_calls == []
    with manager._request_lock(request_id, blocking=False):
        pass

    verdict["value"] = process_launcher.PidIdentityVerdict.MATCH
    matched = manager._finalize_after_process_exit(request_id)
    assert matched["state"] == "running"
    assert manager._request_events(request_id) == before
    assert bridge_calls == []
    assert release_calls == []

    verdict["value"] = process_launcher.PidIdentityVerdict.MISMATCH
    finalized = manager._finalize_after_process_exit(request_id)
    assert finalized["state"] == "worker_failed"
    assert "supervisor_incomplete" in finalized["error"]
    assert release_calls == [(request_id, "worker_failed")]
    assert bridge_calls == [request_id]
    assert manager._finalize_after_process_exit(request_id) == finalized
    assert release_calls == [(request_id, "worker_failed")]
    assert bridge_calls == [request_id]


def test_terminal_supervisor_status_bypasses_unknown_pid_and_finalizes_once(
    monkeypatch,
    tmp_path,
):
    card = _card(task_id="TASK_W1_TERMINAL", runner="deepseek_worker_w1")
    manager = _build_manager(tmp_path, card)
    request_id = "request-w1-terminal"
    _seed_request(
        manager,
        tmp_path,
        card,
        request_id=request_id,
        supervisor_pid=123,
        supervisor_ticks=456,
        supervisor_status={"state": "cancelled", "exit_code": 143},
    )
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _pid_evidence(
            process_launcher.PidIdentityVerdict.UNKNOWN
        ),
    )
    releases = []
    monkeypatch.setattr(
        manager,
        "_terminal_failure_exact",
        lambda metadata, substatus, *, request_id, evidence: (
            releases.append((request_id, substatus)) or {"ok": True}
        ),
    )
    monkeypatch.setattr(
        manager,
        "_record_usage",
        lambda *_args, **_kwargs: ({}, False, ""),
    )
    monkeypatch.setattr(
        process_launcher,
        "_terminate_process_group",
        lambda *_args, **_kwargs: pytest.fail("terminal artifact needs no signal"),
    )

    first = manager._finalize_after_process_exit(request_id)
    second = manager._finalize_after_process_exit(request_id)

    assert first["state"] == "cancelled"
    assert second == first
    assert releases == [(request_id, "cancelled")]


def test_liveness_terminal_mapping_ignores_env_and_keeps_candidate_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(
        worker_workspace,
        "verify_nested_landlock_authority_locator",
        lambda _path: None,
    )
    monkeypatch.setattr(
        worker_workspace,
        "verify_outer_validation_authority_file",
        lambda _path: None,
    )
    assert worker_workspace.authenticated_outer_validation_context() is None
    assert (
        process_launcher._terminal_state_for_workspace_error(
            worker_workspace.ValidationRunError(
                "validation_failed:python -m pytest:rc=2:stdout=:stderr=failed",
                [{"returncode": 2, "command": "python -m pytest"}],
            )
        )
        == "validation_failed"
    )
    assert (
        process_launcher._terminal_state_for_workspace_error(
            worker_workspace.ValidationRunError(
                "validation_failed:python -m pytest:rc=-15:stdout=:stderr=terminated",
                [{"returncode": -15, "command": "python -m pytest"}],
            )
        )
        == "validation_failed"
    )
    assert isinstance(_chmod_blocked_by_sandbox(), bool)
    assert worker_workspace.nested_sandbox_requires_host_boundary() is False


def test_live_provider_stays_non_terminal_past_600s_elapsed_quiet_and_stall(
    tmp_path, monkeypatch,
):
    card = _card(task_id="TASK_FAKE_CLOCK", runner="deepseek_worker_clock")
    fake_supervisor = _spawn_sleeper()
    terminated: list[int] = []
    clock = {"now": 1_700_000_000.0}
    try:
        manager = _build_manager(tmp_path, card)
        monkeypatch.setattr(process_launcher.time, "time", lambda: clock["now"])
        monkeypatch.setattr(
            process_launcher,
            "_terminate_process_group",
            lambda pid, grace_seconds: terminated.append(pid),
        )
        ticks = process_launcher._pid_start_ticks(fake_supervisor.pid)
        _seed_request(
            manager, tmp_path, card,
            request_id="req-fake-clock-600",
            supervisor_pid=fake_supervisor.pid,
            supervisor_ticks=ticks,
            supervisor_status={
                "state": "running",
                "heartbeat_at_epoch": clock["now"],
                "heartbeat_seq": 3,
                "last_output_change_epoch": clock["now"] - 600,
                "last_meaningful_progress_epoch": clock["now"] - 600,
                "last_meaningful_phase": "tool_turn",
                "child_pid": 0,
            },
        )

        first = manager._finalize_isolated_request("req-fake-clock-600")
        clock["now"] += 600.0
        status_path = tmp_path / "processes" / "req-fake-clock-600.supervisor.json"
        _write_status(status_path, {
            "state": "running",
            "heartbeat_at_epoch": clock["now"],
            "heartbeat_seq": 4,
            "last_output_change_epoch": clock["now"] - 1200,
            "last_meaningful_progress_epoch": clock["now"] - 1200,
            "last_meaningful_phase": "tool_turn",
            "child_pid": 0,
        })
        second = manager._finalize_isolated_request("req-fake-clock-600")

        assert first is None
        assert second is None
        assert terminated == []
        assert fake_supervisor.poll() is None
    finally:
        _kill_if_alive(fake_supervisor)


def test_legacy_supervisor_timed_out_is_not_authoritative_without_enforcement(
    tmp_path, monkeypatch,
):
    card = _card(task_id="TASK_LEGACY_TO", runner="deepseek_worker_legacy")
    manager = _build_manager(tmp_path, card)
    releases: list[tuple[str, str]] = []

    def fake_terminal_failure(
        repo, task_id, runner, substatus, *, evidence=None, request_id=""
    ):
        releases.append((task_id, substatus))
        card.update({"status": "blocked", "worker_status": substatus})
        return {"ok": True}

    monkeypatch.setattr(
        process_launcher.task_engine, "mark_terminal_failure", fake_terminal_failure,
    )
    _seed_request(
        manager, tmp_path, card,
        request_id="req-legacy-timed-out",
        supervisor_pid=999_999_999,
        supervisor_ticks=1,
        supervisor_status={
            "state": "timed_out",
            "exit_code": 124,
            "timeout_seconds": 30,
            "timeout_enforced": False,
        },
    )
    metadata_path = tmp_path / "processes" / "req-legacy-timed-out.request.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["timeout_seconds"] = 30
    payload["timeout_enforced"] = False
    worker_workspace.write_json_0600(metadata_path, payload)

    event = manager._finalize_isolated_request(
        "req-legacy-timed-out", supervisor_returncode=124
    )

    assert event is not None
    assert event["state"] == "worker_failed"
    assert event["state"] != "timed_out"
    assert releases == [(card["task_id"], "worker_failed")]


def test_reconciler_status_rejects_symlink_without_o_nofollow(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    spoof = tmp_path / "spoof.json"
    _write_status(spoof, {"scan_finished_epoch": time.time(), "ok": True})
    status = task_reconciler.status_path(repo)
    status.parent.mkdir(parents=True, exist_ok=True)
    status.symlink_to(spoof)
    monkeypatch.delattr(task_reconciler.os, "O_NOFOLLOW", raising=False)

    assert task_reconciler.read_status(repo) == {}


def test_stopped_owner_record_is_not_healthy_after_lock_release(tmp_path, monkeypatch):
    service = task_reconciler.ReconcilerService.__new__(task_reconciler.ReconcilerService)
    service.repo = tmp_path.resolve()
    service.scan_interval_seconds = task_reconciler.MIN_SCAN_INTERVAL_SECONDS
    service._manager = None
    service._stop_event = __import__("threading").Event()
    service._thread = None
    service._state_lock = __import__("threading").Lock()
    service._last_scan = {}
    service._last_error = ""
    service._pass_index = 0

    def one_scan(*_args, **_kwargs):
        service._stop_event.set()
        return {"ok": True, "finalized": 0, "watched": 0}

    monkeypatch.setattr(task_reconciler, "run_scan", one_scan)
    service.start()
    assert service._thread is not None
    service._thread.join(timeout=5)
    service.stop()

    record = task_reconciler.read_status(tmp_path)
    assert record["authority_state"] == "active_owner"
    health = task_reconciler.reconciler_health(tmp_path)
    assert health["ok"] is False
    assert health["durable_authority_live"] is False
    assert health["last_error"] == "reconciler_recorded_owner_not_live"


def _run_contending_cli_daemon(repo: str, entered, release_path: str) -> None:
    """Run the real CLI authority loop with an observable bounded scan."""
    from aiworkhub import task_reconciler as child_reconciler

    def observable_scan(manager, *, repo, include_gc=True):  # noqa: ARG001
        entered.send(os.getpid())
        deadline = time.monotonic() + 10
        while not Path(release_path).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return {"ok": True, "finalized": 0, "watched": 0}

    child_reconciler.run_scan = observable_scan
    raise SystemExit(child_reconciler.main([
        "daemon",
        "--repo",
        repo,
        "--scan-interval-seconds",
        str(child_reconciler.MIN_SCAN_INTERVAL_SECONDS),
        "--max-iterations",
        "1",
    ]))


def test_two_cli_daemons_standby_then_fail_over_without_overlapping_scans(tmp_path):
    """A contended CLI stays alive, then scans only after the owner exits."""
    ctx = multiprocessing.get_context("spawn")
    entered, child_entered = ctx.Pipe(duplex=False)
    release = tmp_path / "release-scan"
    args = (str(tmp_path), child_entered, str(release))
    owner = ctx.Process(target=_run_contending_cli_daemon, args=args)
    standby = ctx.Process(target=_run_contending_cli_daemon, args=args)
    owner.start()
    standby.start()
    try:
        assert entered.poll(10)
        first_pid = entered.recv()
        assert first_pid in {owner.pid, standby.pid}
        passive = standby if first_pid == owner.pid else owner
        active = owner if first_pid == owner.pid else standby

        assert not entered.poll(task_reconciler.AUTHORITY_RETRY_SECONDS * 2)
        assert passive.is_alive()

        active.kill()
        active.join(timeout=5)
        assert not active.is_alive()
        assert entered.poll(10)
        assert entered.recv() == passive.pid
        release.touch()
        passive.join(timeout=10)
        assert passive.exitcode == 0
    finally:
        release.touch(exist_ok=True)
        for process in (owner, standby):
            if process.is_alive():
                process.kill()
            process.join(timeout=5)
