"""A worker that goes quiet must not become a worker that can never finish.

The launcher appends a ``runtime_notice`` row when a request produces no
required-output delta for ten minutes. It is advisory: it says nothing about
the process lifecycle and, unlike every lifecycle row, it carries ``pid``
without ``pid_start_ticks``.

Both finalizers read PID identity from ``events[-1]``. Once a notice existed it
WAS the last row, so identity became "pid present, start ticks unknown" --
which is UNKNOWN, and UNKNOWN defers rather than finalizes, correctly, because
an ambiguous PID must never be declared dead. The two rules were each right and
together they were a trap: the longer a worker worked quietly, the more certain
its notice, and the more permanent its deferral.

Measured on this repository: request 92279514599a47e2a6c9dcdaf86f53fb exited
cleanly at 11:28:03 with its work written and its own tests green, earned a
notice at 11:25:41, and was still ``processing`` half an hour later. Deriving
identity from the merged request history instead of the tail row finalized it
in seven seconds.

Identity belongs to the REQUEST, not to whichever row happens to be last.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher, worker_workspace  # noqa: E402

REQUEST_ID = "advisory0000000000000000000000ab"


def _card() -> dict:
    return {
        "task_id": "TASK_ADVISORY_ROW",
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
        collision_guard=lambda **_k: {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""},
        adapter_builder=lambda **_k: SimpleNamespace(argv=[], cwd=str(tmp_path), launchable=True, reason=""),
        isolation_enabled=True,
    )


def _seed(manager, tmp_path: Path, card: dict, *, pid: int, ticks) -> Path:
    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    status_path = process_dir / f"{REQUEST_ID}.supervisor.json"
    metadata_path = process_dir / f"{REQUEST_ID}.request.json"
    stdout_path = process_dir / f"{REQUEST_ID}.stdout.log"
    stderr_path = process_dir / f"{REQUEST_ID}.stderr.log"
    for p in (stdout_path, stderr_path):
        os.close(os.open(p, os.O_CREAT | os.O_WRONLY, 0o600))
    status_path.write_text(json.dumps({"state": "exited", "exit_code": 0}), encoding="utf-8")

    worker_workspace.write_json_0600(metadata_path, {
        "request_id": REQUEST_ID,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
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
    lifecycle = {
        "request_id": REQUEST_ID,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": pid,
        "pid_start_ticks": ticks,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
    }
    manager._append_event(lifecycle)
    return metadata_path


def _append_notice(manager, metadata_path: Path, pid: int) -> None:
    """The exact advisory row the launcher writes -- pid, but no start ticks."""
    manager._append_event({
        "request_id": REQUEST_ID,
        "task_id": "TASK_ADVISORY_ROW",
        "runner": "claude_worker",
        "topic": "coding",
        "adapter_id": "claude_cli",
        "event_kind": process_launcher.RUNTIME_NOTICE_EVENT_KIND,
        "notice": "zero_required_output_delta_warning",
        "pid": pid,
        "metadata_path": str(metadata_path),
        "elapsed_seconds": 603.7,
    })


def test_the_advisory_row_really_does_omit_the_start_ticks(tmp_path):
    """Pin the premise: if notices ever carry ticks, this whole trap changes."""
    card = _card()
    manager = _manager(tmp_path, card)
    metadata_path = _seed(manager, tmp_path, card, pid=999_000, ticks=4242)
    _append_notice(manager, metadata_path, pid=999_000)

    tail = manager._request_events(REQUEST_ID)[-1]
    assert tail["event_kind"] == process_launcher.RUNTIME_NOTICE_EVENT_KIND
    assert "pid" in tail
    assert "pid_start_ticks" not in tail


def test_identity_read_from_the_tail_alone_is_undecidable(tmp_path):
    """The bug, stated as a measurement rather than a story."""
    card = _card()
    manager = _manager(tmp_path, card)
    metadata_path = _seed(manager, tmp_path, card, pid=999_000, ticks=4242)
    _append_notice(manager, metadata_path, pid=999_000)

    events = manager._request_events(REQUEST_ID)
    tail_only = process_launcher._pid_identity_evidence(
        events[-1].get("pid"), events[-1].get("pid_start_ticks")
    )
    merged = manager._event_identity(events)
    from_history = process_launcher._pid_identity_evidence(
        merged.get("pid"), merged.get("pid_start_ticks")
    )
    assert tail_only.verdict is process_launcher.PidIdentityVerdict.UNKNOWN
    assert from_history.verdict is process_launcher.PidIdentityVerdict.MISMATCH


def test_a_quiet_worker_that_exited_is_still_finalized(tmp_path, monkeypatch):
    """The behaviour that was lost: an exited worker reaches a terminal state."""
    card = _card()
    manager = _manager(tmp_path, card)
    metadata_path = _seed(manager, tmp_path, card, pid=999_000, ticks=4242)
    _append_notice(manager, metadata_path, pid=999_000)

    monkeypatch.setattr(manager, "_record_usage", lambda *_a, **_k: ({}, False, ""))
    monkeypatch.setattr(
        manager, "_terminal_failure_exact",
        lambda *_a, **_k: {"ok": True},
    )
    monkeypatch.setattr(
        process_launcher.task_engine, "mark_terminal_failure",
        lambda *_a, **_k: {"ok": True},
    )

    event = manager._finalize_after_process_exit(REQUEST_ID)

    assert event is not None
    assert event.get("reconciliation_deferred") != "pid_identity_unknown", (
        "an exited worker with a runtime notice must not defer forever"
    )


def test_a_genuinely_ambiguous_identity_still_defers(tmp_path):
    """The deferral rule itself was never wrong -- do not delete it.

    With no start ticks anywhere in the request's history, the PID really is
    undecidable and finalizing would risk declaring a live process dead.
    """
    card = _card()
    manager = _manager(tmp_path, card)
    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    manager._append_event({
        "request_id": REQUEST_ID,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "state": "running",
        "pid": os.getpid(),          # live pid, unknown provenance
    })
    events = manager._request_events(REQUEST_ID)
    merged = manager._event_identity(events)
    verdict = process_launcher._pid_identity_evidence(
        merged.get("pid"), merged.get("pid_start_ticks")
    ).verdict
    assert verdict is process_launcher.PidIdentityVerdict.UNKNOWN
