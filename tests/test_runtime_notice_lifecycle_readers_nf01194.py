"""Tests for NF-2026-01194: a trailing advisory ``runtime_notice`` row must
never strand a lifecycle reader that used to read ``events[-1]``.

The launcher's zero-delta tripwire appends an advisory row with no ``state``
after ten minutes without a required-output delta (see
``RUNTIME_NOTICE_EVENT_KIND`` in ``src/aiworkhub/launch_zero_delta.py``).
Several ``ProcessManager`` readers took lifecycle state from the raw tail of
a request's event history, so once that advisory row landed, ``status()``
lost the running state and liveness, and the vscode_lm worker-tool bridge
refused every call with ``worker_bridge_request_not_active`` -- even though
the worker was alive and authorized moments before. The fix reads the last
LIFECYCLE row (``launch_zero_delta.split_lifecycle_tail``) instead of
``events[-1]``; these tests pin that an advisory tail can neither erase
lifecycle state nor re-authorize/re-admit a finished request.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    launch_zero_delta,
    process_launcher,
    runtime_adapters,
    worker_workspace,
)

# The vscode_lm bridge requires `re.fullmatch(r"[a-f0-9]{32}", request_id)`,
# so (unlike other fixtures in this suite) this id must be pure hex.
REQUEST_ID = "01194" + "0" * 27
TASK_ID = "TASK_NF01194"


def _card() -> dict:
    return {
        "task_id": TASK_ID,
        "runner": "claude_worker",
        "topic": "coding",
        "status": "processing",
        "worker_status": "in_progress",
        "claimed_by": "claude_worker",
        "review_requested_by": "",
        "allowed_writes": ["out/result.txt"],
    }


def _manager(tmp_path: Path, card: dict) -> process_launcher.ProcessManager:
    return process_launcher.ProcessManager(
        repo=tmp_path / "repo",
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=lambda _t: {"returncode": 0, "stdout": json.dumps(card), "stderr": ""},
        collision_guard=lambda **_k: {
            "returncode": 0, "stdout": '{"collision_free":true}', "stderr": "",
        },
        adapter_builder=lambda **_k: SimpleNamespace(
            argv=[], cwd=str(tmp_path), launchable=True, reason="",
        ),
        isolation_enabled=True,
    )


def _seed_lifecycle(
    manager,
    tmp_path: Path,
    card: dict,
    *,
    pid: int,
    ticks,
    state: str,
    adapter_id: str = "claude_cli",
) -> Path:
    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    status_path = process_dir / f"{REQUEST_ID}.supervisor.json"
    metadata_path = process_dir / f"{REQUEST_ID}.request.json"
    stdout_path = process_dir / f"{REQUEST_ID}.stdout.log"
    stderr_path = process_dir / f"{REQUEST_ID}.stderr.log"
    for p in (stdout_path, stderr_path):
        os.close(os.open(p, os.O_CREAT | os.O_WRONLY, 0o600))
    worker_workspace.write_json_0600(status_path, {"state": "running"})

    worker_workspace.write_json_0600(metadata_path, {
        "request_id": REQUEST_ID,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": adapter_id,
        "model": None,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(process_dir / f"{REQUEST_ID}.cancel.json"),
        "metadata_path": str(metadata_path),
        "validation": [],
        "sandbox_backend": "landlock",
        "workspace": {
            "request_id": REQUEST_ID,
            "repo": str(tmp_path / "repo"),
            "path": str(tmp_path / "workspace" / REQUEST_ID),
            "home": str(tmp_path / "home" / REQUEST_ID),
            "allowed_writes": list(card["allowed_writes"]),
            "parent_baseline": {},
            "workspace_baseline": {},
        },
    })
    manager._append_event({
        "request_id": REQUEST_ID,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": adapter_id,
        "state": state,
        "pid": pid,
        "pid_start_ticks": ticks,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
    })
    return metadata_path


def _append_notice(
    manager, metadata_path: Path, *, pid: int, adapter_id: str = "claude_cli",
) -> None:
    """The exact advisory row the launcher writes -- pid, but no start ticks, no state."""
    manager._append_event({
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "runner": "claude_worker",
        "topic": "coding",
        "adapter_id": adapter_id,
        "event_kind": process_launcher.RUNTIME_NOTICE_EVENT_KIND,
        "notice": "zero_required_output_delta_warning",
        "pid": pid,
        "metadata_path": str(metadata_path),
        "elapsed_seconds": 603.7,
    })


# ---------------------------------------------------------------------------
# split_lifecycle_tail -- direct unit coverage of the shared helper
# ---------------------------------------------------------------------------

def test_split_lifecycle_tail_skips_a_trailing_notice():
    lifecycle_row = {"request_id": "r", "state": "running"}
    notice_row = {
        "request_id": "r", "event_kind": launch_zero_delta.RUNTIME_NOTICE_EVENT_KIND,
    }
    lifecycle, advisory = launch_zero_delta.split_lifecycle_tail(
        [lifecycle_row, notice_row]
    )
    assert lifecycle is lifecycle_row
    assert advisory is notice_row


def test_split_lifecycle_tail_empty_list_is_safe():
    lifecycle, advisory = launch_zero_delta.split_lifecycle_tail([])
    assert lifecycle == {}
    assert advisory is None


def test_split_lifecycle_tail_notice_only_is_safe():
    notice_row = {
        "request_id": "r", "event_kind": launch_zero_delta.RUNTIME_NOTICE_EVENT_KIND,
    }
    lifecycle, advisory = launch_zero_delta.split_lifecycle_tail([notice_row])
    assert lifecycle == {}
    assert advisory is notice_row


def test_split_lifecycle_tail_finds_the_last_of_each_independently():
    first_lifecycle = {"request_id": "r", "state": "starting"}
    notice_row = {
        "request_id": "r", "event_kind": launch_zero_delta.RUNTIME_NOTICE_EVENT_KIND,
    }
    second_lifecycle = {"request_id": "r", "state": "exited"}
    lifecycle, advisory = launch_zero_delta.split_lifecycle_tail(
        [first_lifecycle, notice_row, second_lifecycle]
    )
    assert lifecycle is second_lifecycle
    assert advisory is notice_row


# ---------------------------------------------------------------------------
# Acceptance 1 + 2: [running lifecycle row with pid+ticks, runtime_notice row]
# ---------------------------------------------------------------------------

def test_status_reports_running_lifecycle_state_despite_trailing_notice(
    tmp_path, monkeypatch,
):
    card = _card()
    manager = _manager(tmp_path, card)
    metadata_path = _seed_lifecycle(
        manager, tmp_path, card, pid=999_000, ticks=4242, state="running",
    )
    _append_notice(manager, metadata_path, pid=999_000)
    # The fake pid/ticks never match a real OS process; mock the liveness
    # primitive so `process_alive` exercises the lifecycle-row-driven branch,
    # and no-op the reconciliation finalizer this test does not model.
    monkeypatch.setattr(process_launcher, "_pid_matches", lambda *_a, **_k: True)
    monkeypatch.setattr(manager, "_finalize_after_process_exit", lambda *_a, **_k: None)

    result = manager.status(REQUEST_ID)

    assert result["state"] == "running"
    assert result["process_alive"] is True
    assert result["liveness"] != {}
    assert result["latest_event"].get("event_kind") != process_launcher.RUNTIME_NOTICE_EVENT_KIND
    assert result["latest_event"].get("state") == "running"
    assert result["runtime_notice"] is not None
    assert result["runtime_notice"].get("notice") == "zero_required_output_delta_warning"


def test_bridge_authorizes_the_call_despite_trailing_notice(tmp_path, monkeypatch):
    card = _card()
    manager = _manager(tmp_path, card)
    metadata_path = _seed_lifecycle(
        manager, tmp_path, card, pid=999_001, ticks=4243, state="running",
        adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
    )
    _append_notice(
        manager, metadata_path, pid=999_001, adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
    )
    # Isolate the authorization gate: stop deterministically right after it,
    # before the HMAC/audit dispatch machinery this test does not model.
    monkeypatch.setattr(manager, "_metadata_from_events", lambda *_a, **_k: None)

    result = manager.invoke_vscode_lm_worker_tool(REQUEST_ID, "some_tool", {})

    assert result.get("reason") != "worker_bridge_request_not_active"
    assert result == {"ok": False, "reason": "worker_bridge_metadata_invalid"}


# ---------------------------------------------------------------------------
# Negative: a terminal lifecycle row followed by a notice never re-authorizes
# ---------------------------------------------------------------------------

def test_a_terminal_lifecycle_row_is_not_revived_by_a_trailing_notice(tmp_path):
    card = _card()
    manager = _manager(tmp_path, card)
    metadata_path = _seed_lifecycle(
        manager, tmp_path, card, pid=999_002, ticks=4244, state="exited",
        adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
    )
    _append_notice(
        manager, metadata_path, pid=999_002, adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
    )

    status_result = manager.status(REQUEST_ID)
    bridge_result = manager.invoke_vscode_lm_worker_tool(REQUEST_ID, "some_tool", {})

    assert status_result["state"] == "exited"
    assert bridge_result == {"ok": False, "reason": "worker_bridge_request_not_active"}


# ---------------------------------------------------------------------------
# Negative: a ledger of only advisory rows never authorizes
# ---------------------------------------------------------------------------

def test_only_advisory_rows_never_authorize_the_bridge(tmp_path):
    card = _card()
    manager = _manager(tmp_path, card)
    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    manager._append_event({
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "runner": "claude_worker",
        "topic": "coding",
        "adapter_id": runtime_adapters.VSCODE_LM_ADAPTER,
        "event_kind": process_launcher.RUNTIME_NOTICE_EVENT_KIND,
        "notice": "zero_required_output_delta_warning",
        "pid": 999_003,
        "metadata_path": str(process_dir / f"{REQUEST_ID}.request.json"),
        "elapsed_seconds": 603.7,
    })

    result = manager.invoke_vscode_lm_worker_tool(REQUEST_ID, "some_tool", {})

    assert result["ok"] is False
    assert result["reason"] == "worker_bridge_request_not_active"
