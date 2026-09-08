from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path

import pytest

from aiworkhub import process_launcher as pl
from aiworkhub import terminal_failure_classification
from aiworkhub.process_launcher import ProcessManager


def test_collect_rehydrates_request_truth_after_bounded_gc_event(tmp_path: Path) -> None:
    task_id = "TERMINAL_COLLECT_TRUTH"
    request_id = "a" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    metadata_path = process_dir / f"{request_id}.request.json"
    stdout_path.write_bytes(b"exact provider error\n")
    stderr_path.write_bytes(b"exact stderr\n")
    metadata_path.write_text("{}", encoding="utf-8")
    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "blocked",
            "worker_status": "worker_failed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001 - exact event-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 123,
            "pid_start_ticks": 456,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metadata_path": str(metadata_path),
        }
    )
    manager._append_event(  # noqa: SLF001
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "worker_failed",
            "exit_code": 1,
            "error": "vscode_lm_edit_response_stale_hash:src/app.py",
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metadata_path": str(metadata_path),
        }
    )
    manager._append_event(  # noqa: SLF001
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "worker_failed",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)

    assert result["state"] == "worker_failed"
    assert result["adapter_id"] == "deepseek_vscode_lm"
    assert result["model"] == "deepseek-v4-pro"
    assert result["exit_code"] == 1
    assert result["latest_event"]["error"] == (
        "vscode_lm_edit_response_stale_hash:src/app.py"
    )
    assert result["latest_event"]["metadata_path"] == str(metadata_path)
    assert result["stdout_tail"] == "exact provider error\n"
    assert result["stderr_tail"] == "exact stderr\n"


def test_collect_preserves_failure_kind_and_diagnostic_across_gc_overlay(
    tmp_path: Path,
) -> None:
    task_id = "TERMINAL_COLLECT_FAILURE_KIND"
    request_id = "b" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "blocked",
            "worker_status": "worker_failed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001 - exact event-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "worker_failed",
            "exit_code": 1,
            "error": "vscode_lm_edit_response_stale_hash:src/app.py",
            "failure_kind": "worker_failed",
            "diagnostic": "worker_failed:unclassified:exit_code=1",
        }
    )
    # A later workspace-GC/retention overlay event intentionally carries only
    # a small lifecycle delta -- it must never clobber the classified truth
    # computed once at terminal finalization.
    manager._append_event(  # noqa: SLF001
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "worker_failed",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)

    assert result["latest_event"]["failure_kind"] == "worker_failed"
    assert result["latest_event"]["diagnostic"] == (
        "worker_failed:unclassified:exit_code=1"
    )
    assert result["latest_event"]["model"] == "deepseek-v4-pro"
    assert result["latest_event"]["exit_code"] == 1
    assert result["latest_event"]["error"] == (
        "vscode_lm_edit_response_stale_hash:src/app.py"
    )


def test_status_preserves_failure_kind_and_diagnostic_across_gc_overlay(
    tmp_path: Path,
) -> None:
    task_id = "TERMINAL_STATUS_FAILURE_KIND"
    request_id = "c" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "blocked",
            "worker_status": "timed_out",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "timed_out",
            "error": "worker_timed_out:timeout_seconds=1800:exit_code=None",
            "failure_kind": "timeout_stall",
            "diagnostic": "timeout_stall:unclassified",
        }
    )
    # A retention/GC overlay row carries only a small lifecycle delta and must
    # never resurrect or blank out the exact prior terminal classification.
    manager._append_event(  # noqa: SLF001
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "timed_out",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    status = manager.status(request_id)

    assert status["latest_event"]["failure_kind"] == "timeout_stall"
    assert status["latest_event"]["diagnostic"] == "timeout_stall:unclassified"


def test_real_finalizer_prefers_provider_log_diagnostic_over_generic_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework: the real finalizer, not a hand-built event,
    must read the actual stdout/stderr tail exactly once and classify from it
    a more specific closed-vocabulary code than the generic
    ``worker_failed:unclassified`` the supervisor-wrapper error alone would
    produce -- while the raw provider text itself never reaches durable
    diagnostic evidence -- and that persisted truth must survive collect/status
    through a later GC/retention overlay event.
    """
    task_id = "TERMINAL_FINALIZER_E2E"
    request_id = "d" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor-status.json"

    provider_diagnostic = "RuntimeError: actionable_provider_diagnostic_missing_output"
    stdout_path.write_text("Starting worker...\n", encoding="utf-8")
    stderr_path.write_text(
        "Traceback (most recent call last):\n" + provider_diagnostic + "\n",
        encoding="utf-8",
    )
    # read_supervisor_status fails closed on group/other-readable bits, and
    # chmod is unavailable in a sandboxed worker -- an owner-only umask at
    # creation time satisfies the same owner-only invariant without it.
    previous_umask = os.umask(0o077)
    try:
        status_path.write_text(
            json.dumps({"state": "exited", "exit_code": 1, "error": ""}),
            encoding="utf-8",
        )
    finally:
        os.umask(previous_umask)

    metadata = {
        "task_id": task_id,
        "runner": "worker",
        "topic": "truth",
        "adapter_id": "deepseek_vscode_lm",
        "model": "deepseek-v4-pro",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "workspace": {
            "request_id": request_id,
            "repo": str(tmp_path / "repo"),
            "path": str(tmp_path / "repo" / "worktree"),
            "home": str(tmp_path / "repo" / "home"),
            "allowed_writes": [],
            "parent_baseline": {},
        },
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "processing",
            "worker_status": "claimed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    # Everything downstream of the state/exit_code/log-evidence resolution
    # under test (task-store transition, usage ledger, attempt artifacts) is
    # neutralized so this exercises the real finalizer's classification path
    # without needing a live task database or workspace tree.
    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "DELTA_RETAINING_TERMINAL_STATES", frozenset())
    monkeypatch.setattr(pl, "_provider_auth_failure_from_output", lambda _path: None)
    monkeypatch.setattr(
        pl.task_engine, "mark_terminal_failure",
        lambda *_a, **_k: {"ok": True, "callback_enqueued": False},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "worker_failed"
    assert event["failure_kind"] == "worker_failed"
    # A specific closed-vocabulary code was picked from the real stderr tail
    # ("RuntimeError" -> runtime_error), never the generic supervisor-wrapper
    # unclassified verdict the bare error string alone would have produced --
    # and none of the raw provider text is copied into durable diagnostic.
    assert event["diagnostic"] == "worker_failed:runtime_error:exit_code=1"
    assert provider_diagnostic not in event["diagnostic"]
    assert "supervisor_state=exited" not in event["diagnostic"]

    manager._append_event(  # noqa: SLF001 - GC/retention overlay, no reclassification
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "worker_failed",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"]["failure_kind"] == "worker_failed"
    assert result["latest_event"]["diagnostic"] == "worker_failed:runtime_error:exit_code=1"
    assert provider_diagnostic not in result["latest_event"]["diagnostic"]

    status = manager.status(request_id)
    assert status["latest_event"]["failure_kind"] == "worker_failed"
    assert status["latest_event"]["diagnostic"] == "worker_failed:runtime_error:exit_code=1"
    assert provider_diagnostic not in status["latest_event"]["diagnostic"]


def test_real_finalizer_persists_launch_failed_for_malformed_status_and_provider_auth_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework: a malformed/incomplete supervisor status (a
    terminal ``exited`` record carrying no ``exit_code``) combined with a
    genuine provider auth-refusal detected from the worker's own output must
    still persist a non-null ``failure_kind`` -- exit_code=None must never
    fall through to the no-failure default, which is indistinguishable from
    success -- and that verdict must survive collect/status through a later
    GC/retention overlay event.
    """
    task_id = "TERMINAL_FINALIZER_LAUNCH_FAILED"
    request_id = "f" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor-status.json"

    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    # read_supervisor_status fails closed on group/other-readable bits, and
    # chmod is unavailable in a sandboxed worker -- an owner-only umask at
    # creation time satisfies the same owner-only invariant without it.
    previous_umask = os.umask(0o077)
    try:
        status_path.write_text(
            json.dumps({"state": "exited", "exit_code": None, "error": ""}),
            encoding="utf-8",
        )
    finally:
        os.umask(previous_umask)

    metadata = {
        "task_id": task_id,
        "runner": "worker",
        "topic": "truth",
        "adapter_id": "deepseek_vscode_lm",
        "model": "deepseek-v4-pro",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "workspace": {
            "request_id": request_id,
            "repo": str(tmp_path / "repo"),
            "path": str(tmp_path / "repo" / "worktree"),
            "home": str(tmp_path / "repo" / "home"),
            "allowed_writes": [],
            "parent_baseline": {},
        },
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "processing",
            "worker_status": "claimed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    stable_reason = "provider_refused:http_status=401:cause_not_distinguished_by_response"

    # Everything downstream of the state/exit_code/auth-evidence resolution
    # under test (task-store transition, workspace cleanup, usage ledger,
    # attempt artifacts) is neutralized so this exercises the real
    # finalizer's launch_failed classification wiring without needing a live
    # task database or workspace tree.
    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "DELTA_RETAINING_TERMINAL_STATES", frozenset())
    monkeypatch.setattr(
        pl,
        "_provider_auth_failure_from_output",
        lambda _path: {
            "schema_id": "aiworkhub.provider_launch_failure.v1",
            "reason": stable_reason,
            "refusal_kind": "cause_not_distinguished",
            "recoverable": False,
            "http_status": 401,
        },
    )
    monkeypatch.setattr(pl, "cleanup_workspace", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pl.task_engine, "mark_launch_failed",
        lambda *_a, **_k: {"ok": True, "callback_enqueued": False},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    # The launcher-owned reason string is closed vocabulary (an HTTP status it
    # observed plus its own "cause_not_distinguished" term), so it is safe to
    # classify into a matching closed-vocabulary code -- never copied verbatim.
    expected_diagnostic = "launch_failed:auth_cause_not_distinguished:http_status=401"
    assert event["state"] == "launch_failed"
    assert event["exit_code"] is None
    assert event["failure_kind"] == "launch_failed"
    assert event["diagnostic"] == expected_diagnostic

    manager._append_event(  # noqa: SLF001 - GC/retention overlay, no reclassification
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "launch_failed",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"]["failure_kind"] == "launch_failed"
    assert result["latest_event"]["diagnostic"] == expected_diagnostic

    status = manager.status(request_id)
    assert status["latest_event"]["failure_kind"] == "launch_failed"
    assert status["latest_event"]["diagnostic"] == expected_diagnostic


def test_collect_and_status_never_leak_pem_body_straddling_the_tail_read_boundary(
    tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework BLOCKING fix: a private key whose BEGIN marker
    falls before the classifier's bounded tail-read window (so only the body
    + END marker are inside it) must never leak raw body bytes into durable
    diagnostic evidence -- at classification time, and through every
    lifecycle surface (collect/status) that later rehydrates it across a
    GC/retention overlay event.
    """
    task_id = "TERMINAL_COLLECT_PEM_STRADDLE"
    request_id = "9" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    stderr_path = process_dir / f"{request_id}.stderr.log"

    body_line = "SECRETKEYMATERIAL_LINE_0123456789ABCDEF\n"
    pem_block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        + body_line * 200
        + "-----END RSA PRIVATE KEY-----\n"
    )
    stderr_path.write_text(pem_block + "SAFE_TRAILING_TEXT\n", encoding="utf-8")

    classified = terminal_failure_classification.classify_terminal_failure_from_paths(
        state="worker_failed",
        exit_code=1,
        error="worker_failed:supervisor_state=exited:exit_code=1",
        stdout_path=None,
        stderr_path=stderr_path,
    )
    assert "SECRETKEYMATERIAL" not in classified["diagnostic"]
    assert "-----BEGIN" not in classified["diagnostic"]
    assert classified["diagnostic"] == "worker_failed:unclassified:exit_code=1"

    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "blocked",
            "worker_status": "worker_failed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "worker_failed",
            "exit_code": 1,
            "error": "worker_failed:supervisor_state=exited:exit_code=1",
            **classified,
        }
    )
    # A later workspace-GC/retention overlay event must never resurrect the
    # raw log content or reclassify -- it only ever carries a lifecycle delta.
    manager._append_event(  # noqa: SLF001
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "worker_failed",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert "SECRETKEYMATERIAL" not in result["latest_event"]["diagnostic"]

    status = manager.status(request_id)
    assert "SECRETKEYMATERIAL" not in status["latest_event"]["diagnostic"]


def test_real_finalizer_persists_timeout_stall_for_liveness_lost_supervisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework: a genuinely hung supervisor -- heartbeat lease
    AND bounded recovery grace both exceeded while the exact supervisor PID
    still exists -- must persist ``failure_kind == timeout_stall`` with a
    bounded diagnostic. ``supervisor_state`` itself never leaves
    "starting"/"running" for this case, so the finalizer's ``terminal_state``
    stays the generic ``worker_failed`` lifecycle value; only the
    classification input must carry the real liveness verdict.
    """
    task_id = "TERMINAL_FINALIZER_LIVENESS_LOST"
    request_id = "e" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor-status.json"

    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    real_pid = os.getpid()
    real_ticks = pl._pid_start_ticks(real_pid)  # noqa: SLF001 - exact identity fixture
    stale_epoch = time.time() - 100_000.0

    # read_supervisor_status fails closed on group/other-readable bits, and
    # chmod is unavailable in a sandboxed worker -- an owner-only umask at
    # creation time satisfies the same owner-only invariant without it.
    previous_umask = os.umask(0o077)
    try:
        status_path.write_text(
            json.dumps({
                "state": "running",
                "heartbeat_at_epoch": stale_epoch,
                "last_output_change_epoch": stale_epoch,
                "exit_code": None,
                "error": "",
            }),
            encoding="utf-8",
        )
    finally:
        os.umask(previous_umask)

    metadata = {
        "task_id": task_id,
        "runner": "worker",
        "topic": "truth",
        "adapter_id": "deepseek_vscode_lm",
        "model": "deepseek-v4-pro",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "workspace": {
            "request_id": request_id,
            "repo": str(tmp_path / "repo"),
            "path": str(tmp_path / "repo" / "worktree"),
            "home": str(tmp_path / "repo" / "home"),
            "allowed_writes": [],
            "parent_baseline": {},
        },
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "processing",
            "worker_status": "claimed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": real_pid,
            "pid_start_ticks": real_ticks,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    # Everything downstream of the liveness/classification resolution under
    # test (task-store transition, usage ledger, attempt artifacts, and the
    # exact-PID termination side effect this hung-supervisor path triggers)
    # is neutralized so this exercises the real finalizer's liveness-to-
    # classification wiring without terminating this test's own process
    # group or needing a live task database/workspace tree.
    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "DELTA_RETAINING_TERMINAL_STATES", frozenset())
    monkeypatch.setattr(pl, "_provider_auth_failure_from_output", lambda _path: None)
    monkeypatch.setattr(pl, "_terminate_process_group", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pl.task_engine, "mark_terminal_failure",
        lambda *_a, **_k: {"ok": True, "callback_enqueued": False},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "worker_failed"
    assert event["liveness_lost"] is True
    assert event["failure_kind"] == "timeout_stall"
    assert event["diagnostic"].startswith("timeout_stall:liveness_lost")
    assert "heartbeat_lease_and_recovery_grace_exceeded" not in event["diagnostic"]

    manager._append_event(  # noqa: SLF001 - GC/retention overlay, no reclassification
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "worker_failed",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"]["failure_kind"] == "timeout_stall"
    assert result["latest_event"]["diagnostic"] == event["diagnostic"]
    assert "heartbeat_lease_and_recovery_grace_exceeded" not in (
        result["latest_event"]["diagnostic"]
    )

    status = manager.status(request_id)
    assert status["latest_event"]["failure_kind"] == "timeout_stall"
    assert status["latest_event"]["diagnostic"] == event["diagnostic"]
    assert "heartbeat_lease_and_recovery_grace_exceeded" not in (
        status["latest_event"]["diagnostic"]
    )


def test_collect_and_status_preserve_terminal_error_through_real_overlay_error(
    tmp_path: Path,
) -> None:
    """Blocking regression: a real GC/retention overlay (cleanup_failed,
    missing_reclaimed, quarantine_failed, ...) carries its own non-null
    ``error`` describing the retention operation, not the process failure.
    That operational error must never clobber the original terminal
    ``error`` -- it must surface separately as ``retention_error``.
    """
    task_id = "TERMINAL_OVERLAY_ERROR_IDENTITY"
    request_id = "7" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "blocked",
            "worker_status": "worker_failed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    terminal_error = "vscode_lm_edit_response_stale_hash:src/app.py"
    manager._append_event(  # noqa: SLF001 - exact event-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "worker_failed",
            "exit_code": 1,
            "error": terminal_error,
            "failure_kind": "worker_failed",
            "diagnostic": "worker_failed:unclassified:exit_code=1",
        }
    )
    # A real retention overlay row -- shaped like ProcessManager._retention_event's
    # cleanup_failed branch -- carries its own operational error and no
    # failure_kind of its own.
    retention_error = "cleanup_failed:OSError(28, 'No space left on device')"
    manager._append_event(  # noqa: SLF001
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "worker_failed",
            "error": retention_error,
            "workspace_gc": False,
            "workspace_disposition": "retained_in_place",
            "workspace_retained": True,
        }
    )

    status = manager.status(request_id)
    assert status["latest_event"]["failure_kind"] == "worker_failed"
    assert status["latest_event"]["diagnostic"] == (
        "worker_failed:unclassified:exit_code=1"
    )
    assert status["latest_event"]["error"] == terminal_error
    assert status["latest_event"]["retention_error"] == retention_error

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"]["failure_kind"] == "worker_failed"
    assert result["latest_event"]["error"] == terminal_error
    assert result["latest_event"]["retention_error"] == retention_error


class _DirectMonitorFakeProcess:
    """Minimal ``Popen``-shaped stand-in for the direct/non-isolated monitor."""

    def __init__(self, returncode: int) -> None:
        self.pid = 4242
        self._returncode = returncode

    def wait(self) -> int:
        return self._returncode

    def poll(self) -> int:
        return self._returncode


def _direct_monitor_live(
    request_id: str, task_id: str, returncode: int, stdout_path: Path, stderr_path: Path,
) -> "pl._LiveProcess":
    return pl._LiveProcess(
        request_id=request_id,
        task_id=task_id,
        runner="worker",
        topic="truth",
        adapter_id="claude_cli",
        model="claude-test",
        process=_DirectMonitorFakeProcess(returncode),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        started_at=pl._utcnow(),
        timeout_seconds=1800,
        isolated=False,
    )


def test_direct_monitor_persists_failure_kind_for_isolation_disabled_nonzero_exit(
    tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework: the non-isolated/direct monitor path
    (``isolation_enabled=False``) must route a nonzero-exit worker through the
    same terminal_failure_classification authority as the isolated finalizer,
    persisting a stable failure_kind/diagnostic -- not just the raw exit_code
    -- and that verdict must survive collect/status through a later
    GC/retention overlay event. ``review_ready`` and ``cancelled`` outcomes on
    the same path must never gain a failure_kind.
    """
    task_id = "TERMINAL_DIRECT_MONITOR_NONZERO_EXIT"
    request_id = "3" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text(
        "Traceback (most recent call last):\nRuntimeError: boom\n", encoding="utf-8",
    )

    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "blocked",
            "worker_status": "worker_failed",
            "runner": "worker",
            "topic": "truth",
        },
    )

    manager._monitor_direct_for_tests(  # noqa: SLF001 - exact direct-monitor regression
        _direct_monitor_live(request_id, task_id, 1, stdout_path, stderr_path)
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["state"] == "exited"
    assert result["exit_code"] == 1
    assert result["latest_event"]["failure_kind"] == "nonzero_exit"
    expected_diagnostic = "nonzero_exit:runtime_error:exit_code=1"
    assert result["latest_event"]["diagnostic"] == expected_diagnostic

    manager._append_event(  # noqa: SLF001 - GC/retention overlay, no reclassification
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "claude_cli",
            "state": "exited",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"]["failure_kind"] == "nonzero_exit"
    assert result["latest_event"]["diagnostic"] == expected_diagnostic

    status = manager.status(request_id)
    assert status["latest_event"]["failure_kind"] == "nonzero_exit"
    assert status["latest_event"]["diagnostic"] == expected_diagnostic


def test_direct_monitor_never_assigns_failure_kind_to_review_ready_or_cancelled(
    tmp_path: Path,
) -> None:
    """Companion regression: a clean exit that reaches review, and a
    cancelled request, must both stay failure-free on the same direct-monitor
    path that now classifies nonzero exits.
    """
    process_dir = tmp_path / "processes"
    process_dir.mkdir()

    review_task_id = "TERMINAL_DIRECT_MONITOR_REVIEW_READY"
    review_request_id = "4" * 32
    review_stdout = process_dir / f"{review_request_id}.stdout.log"
    review_stderr = process_dir / f"{review_request_id}.stderr.log"
    review_stdout.write_text("", encoding="utf-8")
    review_stderr.write_text("", encoding="utf-8")
    review_manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "review_events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "returncode": 0,
            "stdout": json.dumps({
                "task_id": review_task_id,
                "status": "review",
                "worker_status": "exited",
                "runner": "worker",
                "topic": "truth",
            }),
            "stderr": "",
        },
    )
    review_manager._monitor_direct_for_tests(  # noqa: SLF001
        _direct_monitor_live(
            review_request_id, review_task_id, 0, review_stdout, review_stderr
        )
    )
    review_result = review_manager.collect(review_request_id, max_log_bytes=4096)
    assert review_result["state"] == "review_ready"
    assert review_result["latest_event"].get("failure_kind") is None

    cancelled_task_id = "TERMINAL_DIRECT_MONITOR_CANCELLED"
    cancelled_request_id = "5" * 32
    cancelled_stdout = process_dir / f"{cancelled_request_id}.stdout.log"
    cancelled_stderr = process_dir / f"{cancelled_request_id}.stderr.log"
    cancelled_stdout.write_text("", encoding="utf-8")
    cancelled_stderr.write_text("", encoding="utf-8")
    cancelled_manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "cancelled_events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": cancelled_task_id,
            "status": "blocked",
            "worker_status": "cancelled",
            "runner": "worker",
            "topic": "truth",
        },
    )
    cancelled_manager._cancelled.add(cancelled_request_id)  # noqa: SLF001
    cancelled_manager._monitor_direct_for_tests(  # noqa: SLF001
        _direct_monitor_live(
            cancelled_request_id, cancelled_task_id, 1, cancelled_stdout, cancelled_stderr
        )
    )
    cancelled_result = cancelled_manager.collect(cancelled_request_id, max_log_bytes=4096)
    assert cancelled_result["state"] == "cancelled"
    assert cancelled_result["latest_event"].get("failure_kind") is None


_TASK_ENGINE_SECRET_FRAGMENTS = (
    "abcd1234efgh5678ijkl",
    "abcXYZ789secret",
    "hunter2plain",
    "SECRETKEYMATERIAL",
    "-----BEGIN",
)

_TASK_ENGINE_SECRET_SHAPED_ERROR = (
    "Authorization: Bearer abcd1234efgh5678ijkl "
    '{"password": "hunter2plain"} '
    "{'authorization': 'Bearer abcXYZ789secret'} "
    "-----BEGIN RSA PRIVATE KEY-----\nSECRETKEYMATERIAL\n-----END RSA PRIVATE KEY-----"
)


def test_real_finalizer_never_persists_secret_shaped_error_to_mark_terminal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework BLOCKING security fix: a supervisor-status
    ``error`` field shaped like a live credential (Bearer header, quoted
    JSON, Python repr, and a PEM private key, all at once) must never reach
    the durable task-engine surface (``task_engine.mark_terminal_failure``'s
    ``evidence``) even though the process-event ledger's failure_kind/
    diagnostic were already safe -- both durable surfaces for the identical
    finalization event must share the one closed-vocabulary authority.
    """
    task_id = "TERMINAL_FINALIZER_TASK_ENGINE_REDACTION"
    request_id = "1" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor-status.json"

    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    previous_umask = os.umask(0o077)
    try:
        status_path.write_text(
            json.dumps(
                {"state": "exited", "exit_code": 1, "error": _TASK_ENGINE_SECRET_SHAPED_ERROR}
            ),
            encoding="utf-8",
        )
    finally:
        os.umask(previous_umask)

    metadata = {
        "task_id": task_id,
        "runner": "worker",
        "topic": "truth",
        "adapter_id": "deepseek_vscode_lm",
        "model": "deepseek-v4-pro",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "workspace": {
            "request_id": request_id,
            "repo": str(tmp_path / "repo"),
            "path": str(tmp_path / "repo" / "worktree"),
            "home": str(tmp_path / "repo" / "home"),
            "allowed_writes": [],
            "parent_baseline": {},
        },
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "processing",
            "worker_status": "claimed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    captured = {}

    def _capture_mark_terminal_failure(*_a, evidence=None, **_k):
        captured["evidence"] = evidence
        return {"ok": True, "callback_enqueued": False}

    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "DELTA_RETAINING_TERMINAL_STATES", frozenset())
    monkeypatch.setattr(pl, "_provider_auth_failure_from_output", lambda _path: None)
    monkeypatch.setattr(pl.task_engine, "mark_terminal_failure", _capture_mark_terminal_failure)
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "worker_failed"
    assert event["failure_kind"] == "worker_failed"
    evidence = captured["evidence"]
    assert evidence is not None
    serialized = json.dumps(evidence, default=str)
    for fragment in _TASK_ENGINE_SECRET_FRAGMENTS:
        assert fragment not in serialized
    assert evidence["error"] == event["diagnostic"]


def test_real_finalizer_never_persists_secret_shaped_reason_to_mark_launch_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework BLOCKING security fix: whatever text a
    provider-refusal reason carries, ``task_engine.mark_launch_failed``'s
    ``reason`` must be the same closed-vocabulary diagnostic persisted to the
    process-event ledger for this finalization event, never the raw text --
    proven with Bearer/JSON/repr/PEM credential-shaped content standing in
    for that reason.
    """
    task_id = "TERMINAL_FINALIZER_LAUNCH_FAILED_REDACTION"
    request_id = "2" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor-status.json"

    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    previous_umask = os.umask(0o077)
    try:
        status_path.write_text(
            json.dumps({"state": "exited", "exit_code": None, "error": ""}),
            encoding="utf-8",
        )
    finally:
        os.umask(previous_umask)

    metadata = {
        "task_id": task_id,
        "runner": "worker",
        "topic": "truth",
        "adapter_id": "deepseek_vscode_lm",
        "model": "deepseek-v4-pro",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "workspace": {
            "request_id": request_id,
            "repo": str(tmp_path / "repo"),
            "path": str(tmp_path / "repo" / "worktree"),
            "home": str(tmp_path / "repo" / "home"),
            "allowed_writes": [],
            "parent_baseline": {},
        },
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "processing",
            "worker_status": "claimed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    captured = {}

    def _capture_mark_launch_failed(*_a, reason=None, **_k):
        captured["reason"] = reason
        return {"ok": True, "callback_enqueued": False}

    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "DELTA_RETAINING_TERMINAL_STATES", frozenset())
    monkeypatch.setattr(
        pl,
        "_provider_auth_failure_from_output",
        lambda _path: {
            "schema_id": "aiworkhub.provider_launch_failure.v1",
            "reason": _TASK_ENGINE_SECRET_SHAPED_ERROR,
            "refusal_kind": "cause_not_distinguished",
            "recoverable": False,
            "http_status": 401,
        },
    )
    monkeypatch.setattr(pl, "cleanup_workspace", lambda *_a, **_k: None)
    monkeypatch.setattr(pl.task_engine, "mark_launch_failed", _capture_mark_launch_failed)
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "launch_failed"
    assert event["failure_kind"] == "launch_failed"
    reason = captured["reason"]
    assert reason is not None
    for fragment in _TASK_ENGINE_SECRET_FRAGMENTS:
        assert fragment not in reason
    assert reason == event["diagnostic"]


def test_real_finalizer_persists_failure_kind_for_output_budget_exceeded_with_no_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework BLOCKING fix: the real finalizer's
    ``output_budget_exceeded`` supervisor state never resolves an
    ``exit_code`` (the supervisor cuts the worker off, it does not wait for a
    process exit), so this terminal outcome must still persist a nonempty
    ``failure_kind``/bounded diagnostic instead of falling through to the
    no-failure default -- and that verdict must survive collect/status
    through a later GC/retention overlay event.
    """
    task_id = "TERMINAL_FINALIZER_OUTPUT_BUDGET_EXCEEDED"
    request_id = "6" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor-status.json"

    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    previous_umask = os.umask(0o077)
    try:
        status_path.write_text(
            json.dumps({
                "state": "output_budget_exceeded",
                "exit_code": None,
                "error": "",
                "output_budget": {"cap_bytes": 1_000_000, "observed_bytes": 1_200_000},
            }),
            encoding="utf-8",
        )
    finally:
        os.umask(previous_umask)

    metadata = {
        "task_id": task_id,
        "runner": "worker",
        "topic": "truth",
        "adapter_id": "deepseek_vscode_lm",
        "model": "deepseek-v4-pro",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "workspace": {
            "request_id": request_id,
            "repo": str(tmp_path / "repo"),
            "path": str(tmp_path / "repo" / "worktree"),
            "home": str(tmp_path / "repo" / "home"),
            "allowed_writes": [],
            "parent_baseline": {},
        },
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "processing",
            "worker_status": "claimed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "DELTA_RETAINING_TERMINAL_STATES", frozenset())
    monkeypatch.setattr(
        pl.task_engine, "mark_terminal_failure",
        lambda *_a, **_k: {"ok": True, "callback_enqueued": False},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "output_budget_exceeded"
    assert event["exit_code"] is None
    assert event["failure_kind"] == "output_budget_exceeded"
    assert event["diagnostic"] == "output_budget_exceeded:unclassified"

    manager._append_event(  # noqa: SLF001 - GC/retention overlay, no reclassification
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "output_budget_exceeded",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"]["failure_kind"] == "output_budget_exceeded"
    assert result["latest_event"]["diagnostic"] == "output_budget_exceeded:unclassified"

    status = manager.status(request_id)
    assert status["latest_event"]["failure_kind"] == "output_budget_exceeded"
    assert status["latest_event"]["diagnostic"] == "output_budget_exceeded:unclassified"


_PROCESS_EVENT_SECRET_FRAGMENTS = (
    "abcd1234efgh5678ijkl",
    "abcXYZ789secret",
    "hunter2plain",
    "SECRETKEYMATERIAL",
    "-----BEGIN",
    "AKIAABCDEFGHIJKLMNOP",
    "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
)

_PROCESS_EVENT_SECRET_SHAPED_ERROR = (
    "Authorization: Bearer abcd1234efgh5678ijkl "
    '{"password": "hunter2plain"} '
    "{'authorization': 'Bearer abcXYZ789secret'} "
    "aws_access_key_id=AKIAABCDEFGHIJKLMNOP "
    "aws_secret_access_key=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY "
    "-----BEGIN RSA PRIVATE KEY-----\nSECRETKEYMATERIAL\n-----END RSA PRIVATE KEY-----"
)


def test_real_finalizer_never_persists_secret_shaped_error_to_process_event_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework-of-rework BLOCKING security fix: the
    process-event ledger's own durable ``error`` -- the exact value
    collect/status expose as ``latest_event["error"]`` -- must never copy the
    raw supervisor-status ``error`` text, even though the sibling task-engine
    evidence and ``diagnostic`` were already safe. A classified terminal
    failure's public ``error`` must equal the same closed-vocabulary
    ``diagnostic`` computed for this exact event, proven with Bearer, quoted
    JSON, Python repr, AWS-style, and PEM credential shapes all at once, and
    that safe value must survive collect/status through a later GC/retention
    overlay event unchanged.
    """
    task_id = "TERMINAL_FINALIZER_EVENT_LEDGER_REDACTION"
    request_id = "8" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor-status.json"

    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    previous_umask = os.umask(0o077)
    try:
        status_path.write_text(
            json.dumps(
                {"state": "exited", "exit_code": 1, "error": _PROCESS_EVENT_SECRET_SHAPED_ERROR}
            ),
            encoding="utf-8",
        )
    finally:
        os.umask(previous_umask)

    metadata = {
        "task_id": task_id,
        "runner": "worker",
        "topic": "truth",
        "adapter_id": "deepseek_vscode_lm",
        "model": "deepseek-v4-pro",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "workspace": {
            "request_id": request_id,
            "repo": str(tmp_path / "repo"),
            "path": str(tmp_path / "repo" / "worktree"),
            "home": str(tmp_path / "repo" / "home"),
            "allowed_writes": [],
            "parent_baseline": {},
        },
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "processing",
            "worker_status": "claimed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "DELTA_RETAINING_TERMINAL_STATES", frozenset())
    monkeypatch.setattr(pl, "_provider_auth_failure_from_output", lambda _path: None)
    monkeypatch.setattr(
        pl.task_engine, "mark_terminal_failure",
        lambda *_a, **_k: {"ok": True, "callback_enqueued": False},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "worker_failed"
    assert event["failure_kind"] == "worker_failed"
    assert event["error"] == event["diagnostic"]
    serialized = json.dumps(event, default=str)
    for fragment in _PROCESS_EVENT_SECRET_FRAGMENTS:
        assert fragment not in serialized

    manager._append_event(  # noqa: SLF001 - GC/retention overlay, no reclassification
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "worker_failed",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"]["failure_kind"] == "worker_failed"
    assert result["latest_event"]["error"] == event["diagnostic"]
    collect_serialized = json.dumps(result, default=str)
    for fragment in _PROCESS_EVENT_SECRET_FRAGMENTS:
        assert fragment not in collect_serialized

    status = manager.status(request_id)
    assert status["latest_event"]["failure_kind"] == "worker_failed"
    assert status["latest_event"]["error"] == event["diagnostic"]
    status_serialized = json.dumps(status, default=str)
    for fragment in _PROCESS_EVENT_SECRET_FRAGMENTS:
        assert fragment not in status_serialized


def _isolated_finalizer_manager_and_metadata(
    tmp_path: Path, *, task_id: str, request_id: str, supervisor_status: dict, extra_metadata: dict | None = None,
) -> tuple[ProcessManager, Path, Path]:
    process_dir = tmp_path / "processes"
    process_dir.mkdir(exist_ok=True)
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor-status.json"

    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    previous_umask = os.umask(0o077)
    try:
        status_path.write_text(json.dumps(supervisor_status), encoding="utf-8")
    finally:
        os.umask(previous_umask)

    metadata = {
        "request_id": request_id,
        "claim_epoch": 1,
        "task_id": task_id,
        "runner": "worker",
        "topic": "truth",
        "adapter_id": "deepseek_vscode_lm",
        "model": "deepseek-v4-pro",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "workspace": {
            "request_id": request_id,
            "repo": str(tmp_path / "repo"),
            "path": str(tmp_path / "repo" / "worktree"),
            "home": str(tmp_path / "repo" / "home"),
            "allowed_writes": [],
            "parent_baseline": {},
        },
        **(extra_metadata or {}),
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "processing",
            "worker_status": "claimed",
            "runner": "worker",
            "topic": "truth",
            "claimed_by": "worker",
            "claim_epoch": 1,
            "review_requested_by": "worker",
        },
    )
    return manager, metadata_path, status_path


@pytest.mark.parametrize("running_state", ["running", "cancel_requested"])
def test_real_finalizer_never_persists_secret_shaped_error_for_cancelled_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, running_state: str,
) -> None:
    """NF-2026-00622 V7 rework-of-rework BLOCKING security fix: cancellation
    correctly carries no failure_kind/diagnostic, but a pre-existing
    secret-shaped ``supervisor_status.error`` was still reaching the durable,
    public process-event ``error`` field because safe replacement was
    conditional on failure_kind. Content sanitation must be unconditional --
    proven for both cancellation entry points (a supervisor-reported
    ``cancelled`` state and a durable ``cancel_requested`` manager intent),
    with a Bearer/JSON/repr/AWS/PEM payload all at once, and that safe value
    must survive collect/status through a later GC/retention overlay event.
    """
    task_id = "TERMINAL_FINALIZER_CANCELLED_REDACTION"
    request_id = ("c" if running_state == "running" else "d") * 32
    supervisor_state = "cancelled" if running_state == "running" else "exited"
    manager, metadata_path, status_path = _isolated_finalizer_manager_and_metadata(
        tmp_path,
        task_id=task_id,
        request_id=request_id,
        supervisor_status={
            "state": supervisor_state,
            "exit_code": 137,
            "error": _PROCESS_EVENT_SECRET_SHAPED_ERROR,
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": running_state,
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(manager.process_dir / f"{request_id}.stdout.log"),
            "stderr_path": str(manager.process_dir / f"{request_id}.stderr.log"),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "DELTA_RETAINING_TERMINAL_STATES", frozenset())
    monkeypatch.setattr(pl, "_provider_auth_failure_from_output", lambda _path: None)
    monkeypatch.setattr(
        pl.task_engine, "mark_terminal_failure",
        lambda *_a, **_k: {"ok": True, "callback_enqueued": False},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "cancelled"
    assert event["failure_kind"] is None
    assert event["diagnostic"] == ""
    error_value = event["error"]
    assert error_value != _PROCESS_EVENT_SECRET_SHAPED_ERROR
    serialized = json.dumps(event, default=str)
    for fragment in _PROCESS_EVENT_SECRET_FRAGMENTS:
        assert fragment not in serialized

    manager._append_event(  # noqa: SLF001 - GC/retention overlay, no reclassification
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "cancelled",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"].get("failure_kind") is None
    assert result["latest_event"]["error"] == error_value
    collect_serialized = json.dumps(result, default=str)
    for fragment in _PROCESS_EVENT_SECRET_FRAGMENTS:
        assert fragment not in collect_serialized

    status = manager.status(request_id)
    assert status["latest_event"].get("failure_kind") is None
    assert status["latest_event"]["error"] == error_value
    status_serialized = json.dumps(status, default=str)
    for fragment in _PROCESS_EVENT_SECRET_FRAGMENTS:
        assert fragment not in status_serialized


def test_real_finalizer_never_persists_secret_shaped_error_for_quality_review_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework-of-rework BLOCKING security fix companion: a
    successful quality-review outcome (``review_ready``, no failure_kind of
    its own) with a pre-existing secret-shaped ``supervisor_status.error``
    must also never persist that raw text into the durable, public
    process-event ``error`` field -- content sanitation applies to every
    terminal outcome, not only genuine failures and not only cancellation.
    """
    task_id = "TERMINAL_FINALIZER_REVIEW_READY_REDACTION"
    request_id = "0" * 32
    manager, metadata_path, status_path = _isolated_finalizer_manager_and_metadata(
        tmp_path,
        task_id=task_id,
        request_id=request_id,
        supervisor_status={
            "state": "exited",
            "exit_code": 0,
            "error": _PROCESS_EVENT_SECRET_SHAPED_ERROR,
        },
        extra_metadata={
            "quality_review": {"packet_path": str(tmp_path / "unused_packet.json")},
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(manager.process_dir / f"{request_id}.stdout.log"),
            "stderr_path": str(manager.process_dir / f"{request_id}.stderr.log"),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "_provider_auth_failure_from_output", lambda _path: None)
    monkeypatch.setattr(pl.ProcessManager, "_exact_claim_state", lambda self, *_a, **_k: "processing")
    monkeypatch.setattr(pl.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(pl, "enforce_scope", lambda *_a, **_k: [])
    monkeypatch.setattr(
        pl, "_verified_quality_review_receipt", lambda *_a, **_k: {"reviewer": {}, "report": {}},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_review_terminal_exact", lambda self, *_a, **_k: {"ok": True},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_canonical_outcome_evidence", lambda self, *_a, **_k: {},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "review_ready"
    assert event["failure_kind"] is None
    assert event["diagnostic"] == ""
    assert event["error"] != _PROCESS_EVENT_SECRET_SHAPED_ERROR
    serialized = json.dumps(event, default=str)
    for fragment in _PROCESS_EVENT_SECRET_FRAGMENTS:
        assert fragment not in serialized


# NF-2026-00622 V7 rework-of-rework: boundary-hardening finding. The isolated
# finalizer trusted supervisor_status["exit_code"] as if it were always a
# real int and passed it straight into diagnostic/error formatting and the
# durable exit_code field -- a secret-bearing string (or a bool/float/
# container/huge-int shape) reached those public surfaces unchanged. Every
# shape here is untrusted JSON the malformed/hostile supervisor-status file
# can legally carry.
_MALICIOUS_EXIT_CODE_SHAPES = (
    "Authorization: Bearer abcd1234efgh5678ijkl",
    '{"password": "hunter2plain"}',
    True,
    False,
    1.5,
    {"authorization": "Bearer abcXYZ789secret"},
    ["Authorization: Bearer abcd1234efgh5678ijkl"],
    10**20,
    -(10**20),
)


@pytest.mark.parametrize("malicious_exit_code", _MALICIOUS_EXIT_CODE_SHAPES)
def test_real_finalizer_normalizes_a_malicious_supervisor_status_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, malicious_exit_code: object,
) -> None:
    """NF-2026-00622 V7 rework-of-rework BLOCKING security fix: a secret-
    bearing string, a bool, a float, a dict/list, or a huge int in
    ``supervisor_status["exit_code"]`` must never reach the durable public
    ``exit_code`` field or diagnostic/error formatting as attacker content --
    every shape normalizes to ``None``, which this finalizer's own state
    machine (unable to prove a clean ``exit_code == 0`` exit) then routes to
    a deterministic ``worker_failed`` verdict, and that verdict must survive
    collect/status through a later GC/retention overlay event.
    """
    task_id = "TERMINAL_FINALIZER_EXIT_CODE_BOUNDARY"
    request_id = "b" * 32
    manager, metadata_path, status_path = _isolated_finalizer_manager_and_metadata(
        tmp_path,
        task_id=task_id,
        request_id=request_id,
        supervisor_status={
            "state": "exited",
            "exit_code": malicious_exit_code,
            "error": "",
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(manager.process_dir / f"{request_id}.stdout.log"),
            "stderr_path": str(manager.process_dir / f"{request_id}.stderr.log"),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "DELTA_RETAINING_TERMINAL_STATES", frozenset())
    monkeypatch.setattr(pl, "_provider_auth_failure_from_output", lambda _path: None)
    monkeypatch.setattr(
        pl.task_engine, "mark_terminal_failure",
        lambda *_a, **_k: {"ok": True, "callback_enqueued": False},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    # A malicious exit_code can never be mistaken for the literal 0 that
    # alone would mean success, so this deterministically fails closed --
    # identically across every invalid shape.
    assert event["state"] == "worker_failed"
    assert event["exit_code"] is None
    assert event["failure_kind"] == "worker_failed"
    assert event["diagnostic"] == "worker_failed:unclassified"
    serialized = json.dumps(event, default=str)
    assert "Bearer" not in serialized
    assert "hunter2plain" not in serialized
    assert "abcXYZ789secret" not in serialized

    manager._append_event(  # noqa: SLF001 - GC/retention overlay, no reclassification
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "worker_failed",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"]["failure_kind"] == "worker_failed"
    assert result["latest_event"]["diagnostic"] == "worker_failed:unclassified"
    assert result["latest_event"]["exit_code"] is None

    status = manager.status(request_id)
    assert status["latest_event"]["failure_kind"] == "worker_failed"
    assert status["latest_event"]["diagnostic"] == "worker_failed:unclassified"
    assert status["latest_event"]["exit_code"] is None


def test_real_finalizer_keeps_correct_semantics_for_a_valid_zero_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Control case for the boundary-hardening fix above: a genuine ``0``
    must still normalize to itself and be recognized as a clean exit, proving
    the strict-int normalization does not regress real success semantics.
    """
    task_id = "TERMINAL_FINALIZER_EXIT_CODE_BOUNDARY_VALID"
    request_id = "9" * 32
    manager, metadata_path, status_path = _isolated_finalizer_manager_and_metadata(
        tmp_path,
        task_id=task_id,
        request_id=request_id,
        supervisor_status={"state": "exited", "exit_code": 0, "error": ""},
        extra_metadata={
            "quality_review": {"packet_path": str(tmp_path / "unused_packet.json")},
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(manager.process_dir / f"{request_id}.stdout.log"),
            "stderr_path": str(manager.process_dir / f"{request_id}.stderr.log"),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "_provider_auth_failure_from_output", lambda _path: None)
    monkeypatch.setattr(pl.ProcessManager, "_exact_claim_state", lambda self, *_a, **_k: "processing")
    monkeypatch.setattr(pl.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(pl, "enforce_scope", lambda *_a, **_k: [])
    monkeypatch.setattr(
        pl, "_verified_quality_review_receipt", lambda *_a, **_k: {"reviewer": {}, "report": {}},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_review_terminal_exact", lambda self, *_a, **_k: {"ok": True},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_canonical_outcome_evidence", lambda self, *_a, **_k: {},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "review_ready"
    assert event["exit_code"] == 0
    assert event["failure_kind"] is None
    assert event["diagnostic"] == ""


def test_real_finalizer_derives_fresh_authority_after_quality_review_transition_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework (temporal-drift fix): a quality-review outcome
    that passes review scoring but then fails its durable review-ready
    transition must downgrade to ``review_pending`` -- and the authority on
    that final event must be derived fresh from the settled
    ``review_pending``/``review_transition_failed`` outcome, never the stale
    no-failure authority computed earlier for the (never-settled) exited/
    review_ready classification.
    """
    task_id = "TERMINAL_FINALIZER_QUALITY_REVIEW_TRANSITION_FAILED"
    request_id = "cc" * 16
    manager, metadata_path, status_path = _isolated_finalizer_manager_and_metadata(
        tmp_path,
        task_id=task_id,
        request_id=request_id,
        supervisor_status={"state": "exited", "exit_code": 0, "error": ""},
        extra_metadata={
            "quality_review": {"packet_path": str(tmp_path / "unused_packet.json")},
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(manager.process_dir / f"{request_id}.stdout.log"),
            "stderr_path": str(manager.process_dir / f"{request_id}.stderr.log"),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "_provider_auth_failure_from_output", lambda _path: None)
    monkeypatch.setattr(pl.ProcessManager, "_exact_claim_state", lambda self, *_a, **_k: "processing")
    monkeypatch.setattr(pl.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(pl, "enforce_scope", lambda *_a, **_k: [])
    monkeypatch.setattr(
        pl, "_verified_quality_review_receipt", lambda *_a, **_k: {"reviewer": {}, "report": {}},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_review_terminal_exact",
        lambda self, *_a, **_k: {"ok": False, "stderr": "db_write_conflict:planned_maintenance"},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_canonical_outcome_evidence", lambda self, *_a, **_k: {},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "review_pending"
    assert event["exit_code"] == 0
    # The stale pre-pipeline no-failure classification (review_ready, exit 0,
    # error="") would have persisted an empty error here; the fresh, live
    # derivation must instead reflect the settled review_transition_failed
    # outcome.
    assert event["failure_kind"] is None
    assert event["diagnostic"] == ""
    assert event["error"] == "review_pending:unclassified:exit_code=0"


def test_real_finalizer_derives_fresh_authority_after_worker_candidate_review_transition_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Companion regression for the normal (non-quality-review) worker-
    candidate review pipeline: the same review_transition_failed ->
    review_pending downgrade must derive its authority fresh from the settled
    outcome, not the stale no-failure authority computed for the
    never-settled exited/review_ready classification.
    """
    task_id = "TERMINAL_FINALIZER_CANDIDATE_REVIEW_TRANSITION_FAILED"
    request_id = "dd" * 16
    manager, metadata_path, status_path = _isolated_finalizer_manager_and_metadata(
        tmp_path,
        task_id=task_id,
        request_id=request_id,
        supervisor_status={"state": "exited", "exit_code": 0, "error": ""},
    )
    candidate = tmp_path / "repo/worktree/src/app.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("VALUE = 1\n", encoding="utf-8")
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(manager.process_dir / f"{request_id}.stdout.log"),
            "stderr_path": str(manager.process_dir / f"{request_id}.stderr.log"),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "_provider_auth_failure_from_output", lambda _path: None)
    monkeypatch.setattr(pl.ProcessManager, "_exact_claim_state", lambda self, *_a, **_k: "processing")
    monkeypatch.setattr(pl.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(pl, "enforce_scope", lambda *_a, **_k: ["src/app.py"])
    monkeypatch.setattr(pl, "validate_residual_contract", lambda *_a, **_k: [])
    monkeypatch.setattr(pl, "validate_required_outputs", lambda *_a, **_k: [])
    monkeypatch.setattr(pl, "_worker_mcp_live_call_gate", lambda *_a, **_k: {"gated": False})
    monkeypatch.setattr(pl, "_run_full_snapshot_validations", lambda *_a, **_k: ([], None))
    monkeypatch.setattr(pl, "_enforce_behavioral_gate", lambda *_a, **_k: None)
    monkeypatch.setattr(pl, "_changed_path_hashes", lambda *_a, **_k: {})
    monkeypatch.setattr(
        pl.quality_evidence, "run_completion_quality_gate",
        lambda *_a, **_k: {"passed": True, "blocking_checks": []},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_review_terminal_exact",
        lambda self, *_a, **_k: {"ok": False, "stderr": "db_write_conflict:planned_maintenance"},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_canonical_outcome_evidence", lambda self, *_a, **_k: {},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "review_pending"
    assert event["exit_code"] == 0
    assert event["failure_kind"] is None
    assert event["diagnostic"] == ""
    assert event["error"] == "review_pending:unclassified:exit_code=0"


def test_real_finalizer_settles_authority_after_a_validation_failure_from_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework-of-rework-of-rework BLOCKING correctness fix: a
    clean exited/0 provider exit classifies as no-failure, but a required-
    output validation failure discovered afterward inside the isolated
    finalizer's own review pipeline is a real terminal failure. The
    authoritative failure_kind/diagnostic/error must be recomputed from the
    settled ``validation_failed`` outcome, not the stale pre-pipeline
    no-failure classification, and must survive collect/status through a
    later GC/retention overlay event.
    """
    task_id = "TERMINAL_FINALIZER_VALIDATION_FAILED"
    request_id = "aa" * 16
    manager, metadata_path, status_path = _isolated_finalizer_manager_and_metadata(
        tmp_path,
        task_id=task_id,
        request_id=request_id,
        supervisor_status={"state": "exited", "exit_code": 0, "error": ""},
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(manager.process_dir / f"{request_id}.stdout.log"),
            "stderr_path": str(manager.process_dir / f"{request_id}.stderr.log"),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl.ProcessManager, "_exact_claim_state", lambda self, *_a, **_k: "processing")
    monkeypatch.setattr(pl.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(pl, "enforce_scope", lambda *_a, **_k: [])
    monkeypatch.setattr(pl, "validate_residual_contract", lambda *_a, **_k: [])
    monkeypatch.setattr(
        pl, "validate_required_outputs",
        lambda *_a, **_k: (_ for _ in ()).throw(
            pl.WorkspaceError("required_output_missing:required.txt")
        ),
    )
    monkeypatch.setattr(pl, "_retained_candidate_identity_evidence", lambda *_a, **_k: {})
    monkeypatch.setattr(pl, "_terminal_rework_delta_evidence", lambda *_a, **_k: None)
    monkeypatch.setattr(pl.ProcessManager, "_terminal_failure_exact", lambda self, *_a, **_k: {"ok": True})
    monkeypatch.setattr(pl.ProcessManager, "_review_terminal_exact", lambda self, *_a, **_k: {"ok": True})
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "validation_failed"
    assert event["exit_code"] == 0
    assert event["failure_kind"] == "validation_failed"
    # NF-622 vocabulary: the classifier now names this cause. The old
    # `unclassified` value was the defect this module exists to remove -- a
    # minted validation constant recorded as "no idea". The test's purpose
    # (one settled authority triple, never recomputed by the GC overlay)
    # is unchanged and is asserted below.
    assert event["diagnostic"] == "validation_failed:required_output_missing:exit_code=0"
    assert event["error"] == event["diagnostic"]

    manager._append_event(  # noqa: SLF001 - GC/retention overlay, no reclassification
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "validation_failed",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"]["failure_kind"] == "validation_failed"
    assert result["latest_event"]["diagnostic"] == event["diagnostic"]

    status = manager.status(request_id)
    assert status["latest_event"]["failure_kind"] == "validation_failed"
    assert status["latest_event"]["diagnostic"] == event["diagnostic"]


def test_real_finalizer_settles_authority_after_a_finalize_failure_from_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Companion regression: an unrecognized workspace error raised while
    reconciling a clean exited/0 exit maps to ``finalize_failed`` (the
    fallback of ``_terminal_state_for_workspace_error``), and that settled
    outcome -- not the stale pre-pipeline no-failure classification -- must
    be what failure_kind/diagnostic/error and every later collect/status
    rehydration agree on.
    """
    task_id = "TERMINAL_FINALIZER_FINALIZE_FAILED"
    request_id = "bb" * 16
    manager, metadata_path, status_path = _isolated_finalizer_manager_and_metadata(
        tmp_path,
        task_id=task_id,
        request_id=request_id,
        supervisor_status={"state": "exited", "exit_code": 0, "error": ""},
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(manager.process_dir / f"{request_id}.stdout.log"),
            "stderr_path": str(manager.process_dir / f"{request_id}.stderr.log"),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )

    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl.ProcessManager, "_exact_claim_state", lambda self, *_a, **_k: "processing")
    monkeypatch.setattr(pl.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(
        pl, "enforce_scope",
        lambda *_a, **_k: (_ for _ in ()).throw(
            pl.WorkspaceError("workspace_git_status_unavailable")
        ),
    )
    monkeypatch.setattr(pl, "_retained_candidate_identity_evidence", lambda *_a, **_k: {})
    monkeypatch.setattr(pl, "_terminal_rework_delta_evidence", lambda *_a, **_k: None)
    monkeypatch.setattr(pl.ProcessManager, "_terminal_failure_exact", lambda self, *_a, **_k: {"ok": True})
    monkeypatch.setattr(pl.ProcessManager, "_review_terminal_exact", lambda self, *_a, **_k: {"ok": True})
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event["state"] == "finalize_failed"
    assert event["exit_code"] == 0
    assert event["failure_kind"] == "finalize_failed"
    assert event["diagnostic"] == "finalize_failed:unclassified:exit_code=0"
    assert event["error"] == event["diagnostic"]

    manager._append_event(  # noqa: SLF001 - GC/retention overlay, no reclassification
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "finalize_failed",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"]["failure_kind"] == "finalize_failed"
    assert result["latest_event"]["diagnostic"] == event["diagnostic"]

    status = manager.status(request_id)
    assert status["latest_event"]["failure_kind"] == "finalize_failed"
    assert status["latest_event"]["diagnostic"] == event["diagnostic"]


# NF-2026-00622 V7 rework-of-rework-of-rework: manager-validation regression.
# ``_event_identity`` used ``event.get(key) is not None`` to decide whether to
# merge a key, which cannot tell "this event never had the key" apart from
# "this event explicitly carries the key with value None" -- both read back
# as None from ``.get``. A terminal event that explicitly normalizes
# ``exit_code`` to ``None`` (an invalid/malicious supervisor-status shape)
# must have that exact ``None`` survive a later sparse GC/retention overlay
# event that simply omits ``exit_code`` entirely, not have it silently drop
# out of the merged identity dict.
def test_event_identity_preserves_explicit_none_key_across_key_absent_overlay() -> None:
    terminal_event = {
        "request_id": "e" * 32,
        "task_id": "TERMINAL_EVENT_IDENTITY_PRESENCE",
        "runner": "worker",
        "topic": "truth",
        "adapter_id": "deepseek_vscode_lm",
        "model": "deepseek-v4-pro",
        "exit_code": None,
        "failure_kind": "worker_failed",
        "diagnostic": "worker_failed:unclassified",
        "error": None,
    }
    sparse_overlay_event = {
        "request_id": "e" * 32,
        "task_id": "TERMINAL_EVENT_IDENTITY_PRESENCE",
        "runner": "worker",
        "topic": "truth",
        "adapter_id": "deepseek_vscode_lm",
        "workspace_gc": True,
        "workspace_retained": False,
    }

    merged = pl.ProcessManager._event_identity([terminal_event, sparse_overlay_event])  # noqa: SLF001

    assert "exit_code" in merged
    assert merged["exit_code"] is None
    assert merged["failure_kind"] == "worker_failed"
    assert merged["diagnostic"] == "worker_failed:unclassified"
    assert merged["model"] == "deepseek-v4-pro"


def test_event_identity_key_absent_from_first_event_never_materializes() -> None:
    """Control case: a key genuinely never carried by any event must stay
    entirely absent from the merged identity, not silently become a present
    ``None`` -- presence-sensitivity must not manufacture keys out of thin
    air, only preserve ones an event actually declared.
    """
    events = [
        {"request_id": "f" * 32, "task_id": "T", "runner": "worker", "topic": "truth"},
        {"request_id": "f" * 32, "task_id": "T", "runner": "worker", "topic": "truth",
         "workspace_gc": True},
    ]

    merged = pl.ProcessManager._event_identity(events)  # noqa: SLF001

    assert "exit_code" not in merged
    assert "model" not in merged


def test_event_identity_later_explicit_value_overrides_earlier_explicit_none() -> None:
    events = [
        {"request_id": "g" * 32, "task_id": "T", "runner": "worker", "topic": "truth",
         "exit_code": None},
        {"request_id": "g" * 32, "task_id": "T", "runner": "worker", "topic": "truth",
         "exit_code": 0},
    ]

    merged = pl.ProcessManager._event_identity(events)  # noqa: SLF001

    assert merged["exit_code"] == 0


# NF-2026-00622 V7 rework (correctness fix): ``_event_identity`` gated the
# authoritative branch on ``event.get("failure_kind") is not None``, which
# cannot distinguish "no failure_kind key at all" (a sparse GC/retention/
# disposal overlay) from "failure_kind explicitly recorded as None" (a real
# review_ready/success terminal row). Both read back as None from ``.get``,
# so a later authoritative success fell into the sparse-overlay elif branch
# and its own error='' was stashed as retention_error instead of clearing the
# earlier failure's error -- resurrecting stale failure truth. The fix keys
# the authoritative branch on ``"failure_kind" in event`` (presence), which
# is the exact marker every real GC/retention/disposal overlay omits.
def test_event_identity_later_authoritative_success_clears_prior_failure_error() -> None:
    events = [
        {
            "request_id": "j" * 32, "task_id": "T", "runner": "worker", "topic": "truth",
            "failure_kind": "worker_failed", "diagnostic": "worker_failed:unclassified",
            "error": "worker_failed:unclassified",
        },
        {
            "request_id": "j" * 32, "task_id": "T", "runner": "worker", "topic": "truth",
            "failure_kind": None, "diagnostic": "", "error": "",
        },
    ]

    merged = pl.ProcessManager._event_identity(events)  # noqa: SLF001

    assert merged["failure_kind"] is None
    assert merged["diagnostic"] == ""
    assert merged["error"] == ""
    assert "retention_error" not in merged


def test_event_identity_sparse_overlay_absent_error_never_touches_prior_error() -> None:
    """Control case: a sparse GC/retention/disposal overlay row that omits
    ``error`` entirely (the common case -- a clean GC pass) must leave the
    prior authoritative error/failure_kind completely untouched. Absence is
    neutral, never a signal to clear.
    """
    events = [
        {
            "request_id": "k" * 32, "task_id": "T", "runner": "worker", "topic": "truth",
            "failure_kind": "worker_failed", "diagnostic": "worker_failed:unclassified",
            "error": "worker_failed:unclassified",
        },
        {
            "request_id": "k" * 32, "task_id": "T", "runner": "worker", "topic": "truth",
            "workspace_gc": True,
        },
    ]

    merged = pl.ProcessManager._event_identity(events)  # noqa: SLF001

    assert merged["failure_kind"] == "worker_failed"
    assert merged["error"] == "worker_failed:unclassified"
    assert "retention_error" not in merged


def test_status_and_collect_clear_stale_failure_after_authoritative_retry_success(
    tmp_path: Path,
) -> None:
    """Blocking regression for the manager's exact reviewed lines: a failed
    first attempt (real failure_kind/diagnostic/error) followed by a later
    authoritative retry-finalization success (an explicit failure_kind=None/
    diagnostic=""/error="" terminal row, shaped exactly like
    ``**terminal_failure_authority``) must clear the stale failure truth on
    both status() and collect(), not resurrect it as a stale error/verdict.
    """
    task_id = "TERMINAL_RETRY_SUCCESS_CLEARS_FAILURE"
    request_id = "a1" * 16
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "review",
            "worker_status": "exited",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001 - first attempt: real terminal failure
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "worker_failed",
            "exit_code": 1,
            "error": "worker_failed:unclassified:exit_code=1",
            "failure_kind": "worker_failed",
            "diagnostic": "worker_failed:unclassified:exit_code=1",
        }
    )
    manager._append_event(  # noqa: SLF001 - later authoritative retry success
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "review_ready",
            "exit_code": 0,
            "failure_kind": None,
            "diagnostic": "",
            "error": "",
        }
    )

    status = manager.status(request_id)
    assert status["latest_event"]["failure_kind"] is None
    assert status["latest_event"]["diagnostic"] == ""
    assert status["latest_event"]["error"] == ""
    assert "retention_error" not in status["latest_event"]
    assert status["latest_event"]["exit_code"] == 0

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"].get("failure_kind") is None
    assert result["latest_event"]["error"] == ""


def test_real_finalizer_metadata_invalid_persists_finalize_failed_classification(
    tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework: fix for the flagged review finding. The
    metadata-parse early return inside ``_finalize_isolated_request`` used to
    hand-build a ``finalize_failed`` event carrying a raw
    ``metadata_invalid:{exc}`` ``error`` string and no failure_kind/diagnostic
    at all, returning before the isolated finalizer's own local
    ``_settle_terminal_failure_authority`` closure was ever defined --
    skipping classification/sanitation entirely. A genuinely corrupt
    ``*.request.json`` metadata file must now still yield a stable,
    closed-vocabulary failure_kind/diagnostic, and that verdict must survive
    collect/status through a later GC/retention overlay event.
    """
    task_id = "TERMINAL_FINALIZER_METADATA_INVALID"
    request_id = "m" * 32
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    metadata_path = process_dir / f"{request_id}.request.json"
    # Deliberately malformed JSON -- never a valid metadata document. A proven
    # dead sentinel PID (999999999) matches the convention every other
    # finalizer test in this module uses to bypass liveness gating.
    metadata_path.write_text("{not valid json", encoding="utf-8")

    manager = ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        isolation_enabled=False,
        show_task=lambda _task_id: {
            "task_id": task_id,
            "status": "processing",
            "worker_status": "claimed",
            "runner": "worker",
            "topic": "truth",
        },
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "metadata_path": str(metadata_path),
        }
    )

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert event is not None
    assert event["state"] == "finalize_failed"
    assert event["failure_kind"] == "finalize_failed"
    assert event["diagnostic"].startswith("finalize_failed:")
    assert event["error"] == event["diagnostic"]

    manager._append_event(  # noqa: SLF001 - GC/retention overlay, no reclassification
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "state": "finalize_failed",
            "workspace_gc": True,
            "workspace_retained": False,
        }
    )

    result = manager.collect(request_id, max_log_bytes=4096)
    assert result["latest_event"]["failure_kind"] == "finalize_failed"
    assert result["latest_event"]["diagnostic"] == event["diagnostic"]

    status = manager.status(request_id)
    assert status["latest_event"]["failure_kind"] == "finalize_failed"
    assert status["latest_event"]["diagnostic"] == event["diagnostic"]


# NF-2026-00622 V7 rework: structural regression for the flagged review
# finding. A metadata-parse early return once hand-built a ``finalize_failed``
# event (a literal terminal-failure ``state`` string) with a raw,
# unclassified ``error`` -- skipping
# ``terminal_failure_classification.terminal_event_authority`` entirely. This
# is a bounded source assertion, not a functional test: every dict literal in
# process_launcher.py that assigns a closed-vocabulary terminal-failure state
# as a literal string must also spread in a call to that one shared authority
# function within the same literal, so this exact bypass class cannot
# silently reappear at a new call site.
_TERMINAL_FAILURE_STATE_LITERALS = frozenset({
    "worker_failed", "launch_failed", "timed_out", "liveness_lost",
    "output_budget_exceeded", "exited_without_review", "validation_failed",
    "finalize_failed", "scope_rejected", "promotion_conflict", "finalize_abandoned",
})


def _spreads_terminal_event_authority(dict_node: "ast.Dict") -> bool:
    for key, value in zip(dict_node.keys, dict_node.values):
        if (
            key is None
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "terminal_event_authority"
        ):
            return True
    return False


def test_no_terminal_failure_state_literal_is_appended_outside_the_shared_authority() -> None:
    source_path = Path(__file__).parents[1] / "src" / "aiworkhub" / "process_launcher.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    violations: list[int] = []
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        literal_state = next(
            (
                value.value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant)
                and key.value == "state"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value in _TERMINAL_FAILURE_STATE_LITERALS
            ),
            None,
        )
        if literal_state is None:
            continue
        checked += 1
        if not _spreads_terminal_event_authority(node):
            violations.append(node.lineno)

    assert checked > 0, "expected at least one literal terminal-failure state to check"
    assert violations == [], (
        "terminal-failure state literal(s) appended without "
        f"terminal_failure_classification.terminal_event_authority at line(s): {violations}"
    )


def test_finalize_isolated_request_never_caches_terminal_failure_authority_across_a_mutation() -> None:
    """Structural regression for the reviewed correctness finding: authority
    was cached at one point and only *selectively* recomputed, so a later
    review-transition failure branch (quality-review or normal worker-
    candidate review, both downgrading ``review_ready`` -> ``review_pending``)
    could set ``terminal_state``/``error`` without ever recomputing, and the
    final event spread the stale, pre-mutation authority. The fix: no call
    site may store ``_settle_terminal_failure_authority()``'s result in a name
    that is read again later -- every site either uses the call expression
    directly, or is the single final derivation immediately before the
    terminal event's construction, which must run after every
    ``terminal_state`` mutation in the function.
    """
    source_path = Path(__file__).parents[1] / "src" / "aiworkhub" / "process_launcher.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_finalize_isolated_request"
    )

    def _is_settle_call(node: "ast.AST") -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_settle_terminal_failure_authority"
        )

    cached_names: set[str] = set()
    final_derivation_lines: list[int] = []
    for node in ast.walk(target):
        if isinstance(node, ast.Assign) and _is_settle_call(node.value):
            for assign_target in node.targets:
                if isinstance(assign_target, ast.Name):
                    cached_names.add(assign_target.id)
                    if assign_target.id == "final_terminal_failure_authority":
                        final_derivation_lines.append(node.lineno)

    # Exactly one cached name is allowed: the final, immediately-consumed
    # derivation right before the terminal event's construction. Any other
    # cached name reintroduces the temporal-drift bug this rework fixed.
    assert cached_names == {"final_terminal_failure_authority"}, (
        "unexpected cached _settle_terminal_failure_authority() assignment(s): "
        f"{cached_names}"
    )
    assert len(final_derivation_lines) == 1
    final_derivation_line = final_derivation_lines[0]

    mutation_lines = [
        node.lineno for node in ast.walk(target)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "terminal_state" for t in node.targets)
    ]
    assert mutation_lines, "expected at least one terminal_state mutation to check"
    assert all(line < final_derivation_line for line in mutation_lines), (
        "a terminal_state mutation occurs at or after the final "
        "_settle_terminal_failure_authority() derivation, which can no longer "
        "observe it"
    )
