from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aiworkhub import process_event_ledger, terminal_failure_classification
from aiworkhub import process_launcher as pl
from aiworkhub.process_launcher import ProcessManager

_SUPERVISOR_ERROR = "AppContainerError:profile_creation_failed: hr=0x8000ffff"


def _spawn_failed_manager_and_metadata(
    tmp_path: Path, *, task_id: str, request_id: str, supervisor_status: dict,
) -> tuple[ProcessManager, Path, Path]:
    """Minimal isolated-finalizer fixture, mirroring the real supervisor-status
    read path: a request whose supervisor wrote a terminal ``state``/``error``
    before the worker ever produced output.
    """
    process_dir = tmp_path / "processes"
    process_dir.mkdir(exist_ok=True)
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor-status.json"

    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    workspace_repo = tmp_path / "repo"
    workspace_path = workspace_repo / "worktree"
    workspace_home = workspace_repo / "home"
    workspace_path.mkdir(parents=True, exist_ok=True)
    workspace_home.mkdir(parents=True, exist_ok=True)

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
        "topic": "nf-2026-01136",
        "adapter_id": "claude_cli",
        "model": "claude-sonnet",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "workspace": {
            "request_id": request_id,
            "repo": str(workspace_repo),
            "path": str(workspace_path),
            "home": str(workspace_home),
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
            "topic": "nf-2026-01136",
            "claimed_by": "worker",
            "claim_epoch": 1,
            "review_requested_by": "worker",
        },
    )
    return manager, metadata_path, status_path


def test_real_finalizer_propagates_spawn_failed_supervisor_cause(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF-2026-01136: a supervisor.json recording ``state=spawn_failed`` with a
    non-empty error must reach the terminal process event as a
    ``sandbox_spawn_failed`` terminal_reason carrying that exact bounded cause,
    not ``terminal_reason_missing``.
    """
    task_id = "NF_2026_01136_SPAWN_FAILED"
    request_id = "cc" * 16
    manager, metadata_path, status_path = _spawn_failed_manager_and_metadata(
        tmp_path,
        task_id=task_id,
        request_id=request_id,
        supervisor_status={"state": "spawn_failed", "error": _SUPERVISOR_ERROR},
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "nf-2026-01136",
            "adapter_id": "claude_cli",
            "model": "claude-sonnet",
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
    monkeypatch.setattr(
        pl.ProcessManager, "_exact_claim_state", lambda self, *_a, **_k: "processing",
    )
    monkeypatch.setattr(pl.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(
        pl.task_store, "mark_transient_retry",
        lambda *_a, **_k: (False, "not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_terminal_failure_exact", lambda self, *_a, **_k: {"ok": True},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_review_terminal_exact", lambda self, *_a, **_k: {"ok": True},
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
    assert event["error"] == _SUPERVISOR_ERROR
    terminal_reason = event["terminal_reason"]
    assert terminal_reason["code"] == "sandbox_spawn_failed"
    assert terminal_reason["message"] == _SUPERVISOR_ERROR
    assert terminal_reason["missing_cause"] is False

    category = terminal_failure_classification.disposition_for_reason(terminal_reason["code"])
    assert category != terminal_failure_classification.FAILURE_CLASS_UNKNOWN


def test_canonical_terminal_reason_builds_sandbox_spawn_failed_from_supervisor_status(
    tmp_path: Path,
) -> None:
    """Focused companion: exercise the shared ledger point directly with the
    exact shape process_launcher now emits for a spawn_failed supervisor
    status (state normalized to ``worker_failed``, the bounded supervisor
    error surfaced verbatim, and the caller-asserted ``sandbox_spawn_failed``
    code hint), and assert the persisted code/message/category.
    """
    ledger_path = tmp_path / "events.jsonl"
    persisted = process_event_ledger.append_event(
        ledger_path,
        {
            "request_id": "dd" * 16,
            "task_id": "NF_2026_01136_LEDGER",
            "runner": "worker",
            "state": "worker_failed",
            "error": _SUPERVISOR_ERROR,
            "terminal_reason": {"code": "sandbox_spawn_failed"},
        },
    )

    terminal_reason = persisted["terminal_reason"]
    assert terminal_reason["code"] == "sandbox_spawn_failed"
    assert terminal_reason["message"] == _SUPERVISOR_ERROR
    assert terminal_reason["missing_cause"] is False

    category = terminal_failure_classification.disposition_for_reason(terminal_reason["code"])
    assert category != terminal_failure_classification.FAILURE_CLASS_UNKNOWN


def test_supervisor_spawn_failure_cause_only_fires_for_spawn_failed_with_error() -> None:
    """A genuine spawn_failed error is carried verbatim; every other
    supervisor_state, or an empty/missing error, yields no cause to propagate."""
    cause = terminal_failure_classification.supervisor_spawn_failure_cause(
        "spawn_failed", {"error": _SUPERVISOR_ERROR},
    )
    assert cause == _SUPERVISOR_ERROR
    assert terminal_failure_classification.supervisor_spawn_failure_cause(
        "spawn_failed", {"error": ""},
    ) is None
    assert terminal_failure_classification.supervisor_spawn_failure_cause(
        "spawn_failed", {"error": None},
    ) is None
    assert terminal_failure_classification.supervisor_spawn_failure_cause(
        "spawn_failed", None,
    ) is None
    assert terminal_failure_classification.supervisor_spawn_failure_cause(
        "exited", {"error": _SUPERVISOR_ERROR},
    ) is None
    assert terminal_failure_classification.supervisor_spawn_failure_cause(
        "supervisor_error", {"error": _SUPERVISOR_ERROR},
    ) is None


def test_supervisor_spawn_failure_cause_refuses_unallowlisted_shapes() -> None:
    """NF-2026-01136 rework (security): the guard is a whole-string allowlist,
    not a blocklist -- anything that is not the fixed
    ``SomeError:some_code[: hr=0x...]`` shape is withheld, including
    token/credential-shaped text, a stringified argv, a credentialed URL, a
    non-string ``error`` field, and an oversized value (withheld quickly: the
    length check runs before any regex scan)."""
    for error in (
        "sk-ant-api03-" + "x" * 40,
        "Bearer abcdefghijklmnop",
        "['claude', '--api-key', 'sk-ant-xyz']",
        "https://user:pw@host/x",
        "A" * 5_000_000,
    ):
        assert terminal_failure_classification.supervisor_spawn_failure_cause(
            "spawn_failed", {"error": error},
        ) is None
    for supervisor_status in (
        {"error": {"a": 1}},
        {"error": ["--api-key", "sk"]},
    ):
        assert terminal_failure_classification.supervisor_spawn_failure_cause(
            "spawn_failed", supervisor_status,
        ) is None


def test_supervisor_spawn_failure_cause_allows_keyword_bearing_allowlisted_shape() -> None:
    """A message containing a credential-sounding word (``Token``) still
    passes when its overall shape is the fixed allowlisted spawn-error shape
    -- the allowlist judges shape only, never keywords."""
    cause = terminal_failure_classification.supervisor_spawn_failure_cause(
        "spawn_failed",
        {"error": "OpenProcessTokenError:access_denied: hr=0x80070005"},
    )
    assert cause == "OpenProcessTokenError:access_denied: hr=0x80070005"


def test_real_finalizer_withholds_refused_spawn_cause_but_still_classifies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF-2026-01136 rework (security): when the supervisor's own error text
    is not the allowlisted spawn-error shape, the finalizer still classifies
    the terminal event as ``sandbox_spawn_failed`` (ledger taxonomy
    ``sandbox_spawn_failure``) via the fixed closed-vocabulary fallback -- but
    the refused text itself never reaches the serialized event.
    """
    task_id = "NF_2026_01136_SPAWN_FAILED_REFUSED"
    request_id = "dd" * 16
    secret = "token=sk-ant-XXXXXXXXXXXXXXXXXXXXXXXX"
    manager, metadata_path, status_path = _spawn_failed_manager_and_metadata(
        tmp_path,
        task_id=task_id,
        request_id=request_id,
        supervisor_status={"state": "spawn_failed", "error": secret},
    )
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "nf-2026-01136",
            "adapter_id": "claude_cli",
            "model": "claude-sonnet",
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
    monkeypatch.setattr(
        pl.ProcessManager, "_exact_claim_state", lambda self, *_a, **_k: "processing",
    )
    monkeypatch.setattr(pl.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(
        pl.task_store, "mark_transient_retry",
        lambda *_a, **_k: (False, "not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_terminal_failure_exact", lambda self, *_a, **_k: {"ok": True},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_review_terminal_exact", lambda self, *_a, **_k: {"ok": True},
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
    assert event["error"] == "supervisor_spawn_failed"
    terminal_reason = event["terminal_reason"]
    assert terminal_reason["code"] == "sandbox_spawn_failed"
    assert terminal_reason["taxonomy"] == "sandbox_spawn_failure"
    assert terminal_reason["missing_cause"] is False
    assert secret not in json.dumps(event)
