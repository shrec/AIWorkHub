"""fix #5: heartbeat-lost / stall recovery must be reachable for a wedged but
still-alive supervisor.

The passive PID watcher only finalized once the PID+start-ticks identity stopped
matching, so a heartbeat-lost or stalled worker stayed "processing" forever.
On a MATCH verdict, when the candidate has a supervisor_status_path and is not
already tracked, the durable escalation path now gets a chance to run while the
process still exists; a terminal verdict counts as finalized, otherwise the scan
falls through to the passive watcher unchanged.

These tests drive the public ``reconcile()`` sequence against a real live
process rather than calling the private finalizer directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher, worker_workspace  # noqa: E402

pytestmark = pytest.mark.skipif(
    __import__("os").name == "nt", reason="POSIX proc-identity / process groups"
)


def _card(state: str = "processing") -> dict:
    return {
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "status": state,
        "worker_status": "unclaimed",
        "claimed_by": "",
        "allowed_writes": ["out/result.json"],
        "priority": "high",
    }


def _show(task_id: str):
    return {"returncode": 0, "stdout": json.dumps(_card()), "stderr": ""}


def _manager(tmp_path: Path) -> process_launcher.ProcessManager:
    repo = tmp_path / "repo"
    repo.mkdir()
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show,
        isolation_enabled=False,
    )


def _live_child():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    ticks = process_launcher._pid_start_ticks(child.pid)
    return child, ticks


def _stage_running_request(manager, tmp_path, *, status: dict, pid: int, ticks):
    request_id = "9" * 32
    worktree = tmp_path / "ws"
    home = tmp_path / "ws-home"
    worktree.mkdir()
    home.mkdir()
    workspace = process_launcher.WorkerWorkspace(
        request_id=request_id,
        repo=manager.repo,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    stdout_path = tmp_path / f"{request_id}.stdout.log"
    stderr_path = tmp_path / f"{request_id}.stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    status_path = manager.process_dir / f"{request_id}.supervisor.json"
    metadata_path = manager.process_dir / f"{request_id}.request.json"
    worker_workspace.write_json_0600(status_path, status)
    metadata = {
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "claude_cli",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(tmp_path / f"{request_id}.cancel.json"),
        "workspace": workspace.as_metadata(),
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "claude_cli",
        "state": "running",
        "pid": pid,
        "pid_start_ticks": ticks,
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    })
    return request_id


def test_reconcile_recovers_wedged_but_alive_supervisor(monkeypatch, tmp_path):
    child, ticks = _live_child()
    if ticks is None:
        child.terminate()
        pytest.skip("proc start ticks unavailable")
    try:
        manager = _manager(tmp_path)
        monkeypatch.setenv(process_launcher.STALL_GRACE_ENV, "30")
        now = time.time()
        # Fresh heartbeat (so liveness is "alive", not "lost"), but the last
        # meaningful progress is long past: a wedged-but-alive worker.
        request_id = _stage_running_request(
            manager,
            tmp_path,
            status={
                "state": "running",
                "heartbeat_at_epoch": now,
                "last_output_change_epoch": now,
                "last_meaningful_progress_epoch": now - 100_000,
            },
            pid=child.pid,
            ticks=ticks,
        )

        terminate_calls: list[int] = []
        watch_calls: list[tuple] = []
        monkeypatch.setattr(
            process_launcher,
            "_terminate_process_group",
            lambda pid, **_k: terminate_calls.append(pid),
        )
        monkeypatch.setattr(
            process_launcher, "_declared_failure_denominators", lambda *a, **k: {}
        )
        monkeypatch.setattr(
            manager, "_terminal_failure_exact", lambda *a, **k: {"ok": True, "stderr": ""}
        )
        monkeypatch.setattr(manager, "_persist_attempt_artifacts", lambda *a, **k: None)
        monkeypatch.setattr(manager, "_record_usage", lambda *a, **k: ({}, False, ""))
        monkeypatch.setattr(manager, "_gc_finalized_workspaces", lambda: {})
        monkeypatch.setattr(
            manager,
            "_watch_persisted_request",
            lambda *a, **k: watch_calls.append(a),
        )

        result = manager.reconcile()

        latest = manager._request_events(request_id)[-1]
        assert latest["state"] == "worker_failed"
        assert latest["stall_detected"] is True
        assert result.get("finalized", 0) >= 1
        # The durable escalation path finalized it while alive; the passive
        # watcher was never needed for this wedged worker.
        assert watch_calls == []
        assert child.pid in terminate_calls
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_reconcile_leaves_healthy_worker_untouched(monkeypatch, tmp_path):
    child, ticks = _live_child()
    if ticks is None:
        child.terminate()
        pytest.skip("proc start ticks unavailable")
    try:
        manager = _manager(tmp_path)
        now = time.time()
        # Fresh heartbeat AND fresh meaningful progress: a healthy worker.
        request_id = _stage_running_request(
            manager,
            tmp_path,
            status={
                "state": "running",
                "heartbeat_at_epoch": now,
                "last_output_change_epoch": now,
                "last_meaningful_progress_epoch": now,
            },
            pid=child.pid,
            ticks=ticks,
        )

        watch_calls: list[tuple] = []
        monkeypatch.setattr(manager, "_gc_finalized_workspaces", lambda: {})
        monkeypatch.setattr(
            manager,
            "_watch_persisted_request",
            lambda *a, **k: watch_calls.append(a),
        )
        # A healthy worker must produce no durable finalization side effect.
        monkeypatch.setattr(
            manager,
            "_terminal_failure_exact",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("healthy worker must not be terminalized")
            ),
        )
        monkeypatch.setattr(
            manager,
            "_record_usage",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("healthy worker must not record usage")
            ),
        )

        result = manager.reconcile()

        # No terminal event: the request is still "running".
        assert manager._request_events(request_id)[-1]["state"] == "running"
        assert result.get("finalized", 0) == 0
        # It fell through to the passive watcher unchanged.
        assert len(watch_calls) == 1
        assert watch_calls[0][0] == request_id
    finally:
        child.terminate()
        child.wait(timeout=5)
