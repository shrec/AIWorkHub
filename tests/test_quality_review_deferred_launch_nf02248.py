"""NF-2026-02248 (NF-2026-00034): deferred quality reviewer launch.

A reviewer launch deferred for capacity waits in ``launch_admission`` for up
to its own configured ceiling, longer than the 300 s launch-owner ceiling.  It
used to end as a silent pid-null ``quality_review_launch_timeout`` with no
spawn attempt.  It also lost its adapter on the deferred path when the caller
passed none, which recorded a false ``cross_provider`` rung on a
single-provider install.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import process_launcher, quality_review

REVIEWER_TASK = "TASK_REVIEWER_NF02248"
TARGET_TASK = "TASK_TARGET_NF02248"
TARGET_REQUEST = "a" * 32


def _manager(tmp_path: Path) -> process_launcher.ProcessManager:
    repo = tmp_path / "repo"
    repo.mkdir()
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=lambda task_id: {"ok": True, "stdout": "{}"},
        isolation_enabled=False,
    )


def _reserve(manager: process_launcher.ProcessManager, request_id: str) -> None:
    manager._append_event({
        "request_id": request_id,
        "task_id": REVIEWER_TASK,
        "runner": "claude_worker_b1",
        "topic": "quality_review",
        "adapter_id": "claude_cli",
        "state": "starting",
        "reservation_expires_at_epoch": time.time() + 600,
        "owner_pid": os.getpid(),
        "owner_pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
    })


def _stub_preparation(
    manager: process_launcher.ProcessManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = {
        "worker_adapter_id": "claude_cli",
        "packet": {},
        "workspace": SimpleNamespace(as_metadata=lambda: {}),
        "changed_hashes": {"src/x.py": "0" * 64},
        "read_only_input_paths": [],
    }
    monkeypatch.setattr(
        manager,
        "_prepared_quality_review",
        lambda *args, **kwargs: {"ok": True, "prepared": prepared},
    )
    monkeypatch.setattr(
        process_launcher.quality_reviewer,
        "build_lens_packet",
        lambda packet, *, lens: {"target": {"claim_epoch": 1}, "lens": lens},
    )


def _capture_terminal(
    manager: process_launcher.ProcessManager, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    terminal: list[dict[str, Any]] = []

    def _terminalize(request_id, task_id, runner, adapter_id, *, reason):
        terminal.append({
            "request_id": request_id,
            "adapter_id": adapter_id,
            "reason": reason,
        })

    monkeypatch.setattr(manager, "_terminalize_reviewer_attempt", _terminalize)
    return terminal


def _run(manager: process_launcher.ProcessManager, request_id: str, adapter_id: str) -> None:
    manager._launch_reserved_quality_reviewer(
        request_id=request_id,
        target_request_id=TARGET_REQUEST,
        target_task_id=TARGET_TASK,
        reviewer_task_id=REVIEWER_TASK,
        runner="claude_worker_b1",
        adapter_id=adapter_id,
        lens="correctness",
        model=None,
        timeout_seconds=1800,
    )


def test_capacity_deferred_reviewer_spawns_instead_of_pid_null_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bounded admission wait longer than the owner ceiling still spawns."""

    manager = _manager(tmp_path)
    request_id = "b" * 32
    _reserve(manager, request_id)
    _stub_preparation(manager, monkeypatch)
    terminal = _capture_terminal(manager, monkeypatch)
    monkeypatch.setattr(
        process_launcher.ProcessManager, "_QUALITY_REVIEW_LAUNCH_OWNER_SECONDS", 0.05
    )
    monkeypatch.setenv("AIWORKHUB_LAUNCH_ADMISSION_WAIT_SECONDS", "30")

    def _slow_admission(*, kind, probe, wait_seconds, **_kwargs):
        # Capacity frees only after the owner outlived its ceiling and
        # observed this live wait (bounded so a regression fails, not hangs).
        assert kind == "reviewer"
        assert wait_seconds == 30.0
        started = time.monotonic()
        deadline = started + 20.0
        log = tmp_path / "events.jsonl"
        while time.monotonic() < deadline:
            if '"admission_waiting"' in log.read_text():
                break
            time.sleep(0.02)
        return {"admit": True, "waited_seconds": time.monotonic() - started}

    monkeypatch.setattr(
        process_launcher.launch_admission, "await_memory_admission", _slow_admission
    )
    spawned: list[dict[str, Any]] = []

    def _launch_task(**kwargs):
        admission = manager._await_reviewer_admission(lambda: {"admit": True})
        assert admission["admit"] is True
        spawned.append(kwargs)
        return {"ok": True, "request_id": kwargs["reserved_request_id"], "pid": 4242}

    monkeypatch.setattr(manager, "launch_task", _launch_task)

    _run(manager, request_id, "claude_cli")

    assert terminal == [], terminal
    assert len(spawned) == 1
    assert spawned[0]["adapter_id"] == "claude_cli"
    assert spawned[0]["quality_review_binding"]["adapter_id"] == "claude_cli"
    # The owner heartbeated the reservation while admission was pending, so
    # expiry reconciliation keeps reading it as live.
    latest = manager._latest_by_request()[request_id]
    assert latest["state"] == "starting"
    assert latest["adapter_id"] == "claude_cli"
    phases = [
        json.loads(line).get("preparation_phase")
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert "admission_waiting" in phases
    # The wait registration is released on exit (bounded state).
    assert process_launcher.ProcessManager._REVIEWER_ADMISSION_WAIT_WINDOWS == {}
    assert process_launcher.ProcessManager._REVIEWER_ADMISSION_ENDED_AT == {}


def test_stalled_reviewer_timeout_carries_adapter_and_attempt_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real pre-provider stall is still terminalized, but diagnosably."""

    manager = _manager(tmp_path)
    request_id = "c" * 32
    _reserve(manager, request_id)
    _stub_preparation(manager, monkeypatch)
    terminal = _capture_terminal(manager, monkeypatch)
    monkeypatch.setattr(
        process_launcher.ProcessManager, "_QUALITY_REVIEW_LAUNCH_OWNER_SECONDS", 0.05
    )
    release = threading.Event()

    def _stalled_launch_task(**kwargs):
        release.wait(5)
        return {"ok": False, "error": "released"}

    monkeypatch.setattr(manager, "launch_task", _stalled_launch_task)
    try:
        _run(manager, request_id, "claude_cli")
    finally:
        release.set()

    assert len(terminal) == 1
    assert terminal[0]["adapter_id"] == "claude_cli"
    # The reason stays the exact token every consumer matches.
    assert terminal[0]["reason"] == "quality_review_launch_timeout"
    details = [
        event.get("preparation_detail")
        for event in (
            json.loads(line)
            for line in (tmp_path / "events.jsonl").read_text().splitlines()
            if line.strip()
        )
        if event.get("preparation_phase") == "launch_timeout_evidence"
    ]
    assert len(details) == 1
    evidence = json.loads(details[0])
    assert evidence["adapter_id"] == "claude_cli"
    assert evidence["provider_spawn_committed"] is False
    assert evidence["retryable"] is True
    assert evidence["last_phase"] == "isolated_launch_started"


def test_admission_ending_late_in_join_slice_gets_fresh_spawn_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission completing near a slice end still leaves a full spawn ceiling.

    Post-admission work longer than the rest of the current join slice (but
    shorter than one owner ceiling) must spawn, not terminalize as a timeout.
    """

    manager = _manager(tmp_path)
    request_id = "e" * 32
    _reserve(manager, request_id)
    _stub_preparation(manager, monkeypatch)
    terminal = _capture_terminal(manager, monkeypatch)
    ceiling = 1.0
    monkeypatch.setattr(
        process_launcher.ProcessManager, "_QUALITY_REVIEW_LAUNCH_OWNER_SECONDS", ceiling
    )
    monkeypatch.setenv("AIWORKHUB_LAUNCH_ADMISSION_WAIT_SECONDS", "30")

    def _late_admission(*, kind, probe, wait_seconds, **_kwargs):
        # Admission completes just before the first join slice ends.
        time.sleep(ceiling * 0.85)
        return {"admit": True, "waited_seconds": ceiling * 0.85}

    monkeypatch.setattr(
        process_launcher.launch_admission, "await_memory_admission", _late_admission
    )
    spawned: list[dict[str, Any]] = []

    def _launch_task(**kwargs):
        admission = manager._await_reviewer_admission(lambda: {"admit": True})
        assert admission["admit"] is True
        # Post-admission work outlasts the slice remainder, not a ceiling.
        time.sleep(ceiling * 0.5)
        spawned.append(kwargs)
        return {"ok": True, "request_id": kwargs["reserved_request_id"], "pid": 4244}

    monkeypatch.setattr(manager, "launch_task", _launch_task)

    _run(manager, request_id, "claude_cli")

    assert terminal == [], terminal
    assert len(spawned) == 1
    assert spawned[0]["adapter_id"] == "claude_cli"
    assert process_launcher.ProcessManager._REVIEWER_ADMISSION_WAIT_WINDOWS == {}
    assert process_launcher.ProcessManager._REVIEWER_ADMISSION_ENDED_AT == {}


def test_deferred_launch_keeps_reserved_adapter_on_single_provider_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty caller adapter is recovered from the sealed reservation.

    On a single-provider install (worker and reviewer both on claude) the
    reviewer launches at a same-provider rung; no cross_provider rung is
    required, and none is falsely recorded from a lost adapter.
    """

    manager = _manager(tmp_path)
    request_id = "d" * 32
    _reserve(manager, request_id)
    _stub_preparation(manager, monkeypatch)
    terminal = _capture_terminal(manager, monkeypatch)
    spawned: list[dict[str, Any]] = []

    def _launch_task(**kwargs):
        spawned.append(kwargs)
        return {"ok": True, "request_id": kwargs["reserved_request_id"], "pid": 4243}

    monkeypatch.setattr(manager, "launch_task", _launch_task)

    _run(manager, request_id, "")

    assert terminal == [], terminal
    assert len(spawned) == 1
    assert spawned[0]["adapter_id"] == "claude_cli"
    binding = spawned[0]["quality_review_binding"]
    assert binding["adapter_id"] == "claude_cli"
    rung = binding["independence"]["rung"]
    assert rung != quality_review.RUNG_CROSS_PROVIDER
    assert rung in quality_review.INDEPENDENCE_LADDER
    assert binding["independence"]["cross_provider"] is False
