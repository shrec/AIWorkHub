"""NF-2026-01574: dead vscode_lm request whose bridge-cancel home is gone.

The reconciler must close such a request as a terminal
``finalize_abandoned`` carrying a moot bridge-cancel reason, while every
still-relevant case -- a live child, a present home, or a processing card
bound to the request -- keeps the fail-closed ``reconcile_pending``
deferral.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aiworkhub import process_launcher, worker_workspace


REQUEST_ID = "d" * 32
TASK_ID = "NF01574_RECONCILE_ORPHAN_HOME_GONE"
SUPERVISOR_PID = 454545
SUPERVISOR_TICKS = 23
DEAD_CHILD_PID = 454547


def _pid_evidence(
    verdict: process_launcher.PidIdentityVerdict,
) -> process_launcher.PidIdentityEvidence:
    return process_launcher.PidIdentityEvidence(
        verdict=verdict,
        pid=SUPERVISOR_PID,
        expected_start_ticks=SUPERVISOR_TICKS,
        observed_start_ticks=0,
        attempts=1,
        operation="nf01574_orphan_home_gone",
    )


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    card: dict,
    child_pid: int = DEAD_CHILD_PID,
    home_exists: bool = False,
    cancel_raises: bool = False,
    verdict: process_launcher.PidIdentityVerdict = (
        process_launcher.PidIdentityVerdict.MISMATCH
    ),
) -> process_launcher.ProcessManager:
    """Build the exact persisted shape the reconciler recovers from."""
    bridge = process_launcher.vscode_lm_bridge
    monkeypatch.setenv(bridge.BRIDGE_ROOT_ENV, str(tmp_path / "bridge"))
    workspace_root = tmp_path / REQUEST_ID
    home = workspace_root / "home"
    worktree = workspace_root / "worktree"
    worktree.mkdir(parents=True)
    if home_exists:
        home.mkdir(parents=True)
    repo_id = "repo_" + "e" * 32
    cancel_token = "f" * 64
    request_path = tmp_path / "bridge" / "requests" / repo_id / f"{REQUEST_ID}.json"
    response_path = home / ".aiworkhub_vscode_lm_response.json"
    worker_spec_path = home / ".aiworkhub_vscode_lm_worker.json"
    bridge._atomic_json(  # noqa: SLF001 - exact persisted bridge receipt
        request_path,
        {
            "schema_id": bridge.REQUEST_SCHEMA_ID,
            "request_id": REQUEST_ID,
            "repo_id": repo_id,
            "cancel_token": cancel_token,
        },
    )
    request = bridge.BridgeRequest(
        request_id=REQUEST_ID,
        repo_id=repo_id,
        request_path=request_path,
        response_path=response_path,
        worker_spec_path=worker_spec_path,
        cancel_path=response_path,
        cancel_token=cancel_token,
    )
    claim_path = Path(f"{request_path}.claim-window_dead")
    request_path.rename(claim_path)

    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    metadata_path = process_dir / f"{REQUEST_ID}.request.json"
    status_path = process_dir / f"{REQUEST_ID}.status.json"
    cancel_path = process_dir / f"{REQUEST_ID}.cancel.json"
    process_launcher.write_json_0600(
        status_path,
        {
            "state": "exited",
            "exit_code": 0,
            "child_pid": child_pid,
            "heartbeat_at_epoch": 1.0,
        },
    )
    workspace = worker_workspace.WorkerWorkspace(
        request_id=REQUEST_ID,
        repo=tmp_path / "repo",
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    process_launcher.write_json_0600(
        metadata_path,
        {
            "request_id": REQUEST_ID,
            "adapter_id": "vscode_lm",
            "cancel_path": str(cancel_path),
            "supervisor_status_path": str(status_path),
            "workspace": workspace.as_metadata(),
            "vscode_lm_bridge": bridge.bridge_request_metadata(request),
        },
    )
    manager = process_launcher.ProcessManager(
        repo=tmp_path / "repo",
        process_log_path=tmp_path / "events.jsonl",
        process_dir=process_dir,
        show_task=lambda _task_id: {"returncode": 0, "stdout": json.dumps(card)},
    )
    manager._append_event(  # noqa: SLF001 - persisted reconcile setup
        {
            "request_id": REQUEST_ID,
            "task_id": TASK_ID,
            "runner": "worker",
            "topic": "bridge-orphan",
            "adapter_id": "vscode_lm",
            "state": "running",
            "pid": SUPERVISOR_PID,
            "pid_start_ticks": SUPERVISOR_TICKS,
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _pid_evidence(verdict),
    )
    if cancel_raises:

        def _raise_oserror(_request):
            raise OSError("bridge cancel surface gone")

        monkeypatch.setattr(bridge, "cancel_request", _raise_oserror)
    return manager


def _rows(manager: process_launcher.ProcessManager) -> list[dict]:
    return manager._request_events(REQUEST_ID)  # noqa: SLF001


def _deferred_row(manager: process_launcher.ProcessManager) -> dict:
    pending = [
        row for row in _rows(manager) if row.get("state") == "reconcile_pending"
    ]
    assert pending, _rows(manager)
    return pending[-1]


@pytest.mark.parametrize("card_status", ["finished", "archived"])
def test_dead_request_with_removed_home_finalizes_as_abandoned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    card_status: str,
) -> None:
    manager = _setup(
        monkeypatch,
        tmp_path,
        card={"task_id": TASK_ID, "status": card_status},
    )

    sweep = manager._reconcile_persisted_requests  # noqa: SLF001
    assert sweep() == {"watched": 0, "finalized": 1}

    rows = _rows(manager)
    abandoned = [row for row in rows if row.get("state") == "finalize_abandoned"]
    assert len(abandoned) == 1
    assert abandoned[0]["bridge_cancel_moot"] == "bridge_cancel_moot_home_removed"
    assert abandoned[0]["bridge_provider_may_be_active"] is False
    assert [row for row in rows if row.get("state") == "reconcile_pending"] == []
    assert sweep() == {"watched": 0, "finalized": 0}
    assert _rows(manager) == rows


def test_cancel_reports_the_terminal_abandoned_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = _setup(
        monkeypatch,
        tmp_path,
        card={"task_id": TASK_ID, "status": "finished"},
    )

    result = manager.cancel(REQUEST_ID)

    assert _rows(manager)[-1]["state"] == "finalize_abandoned"
    assert result["ok"] is True
    assert result["state"] == "finalize_abandoned"


def test_live_child_defers_on_failed_bridge_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = _setup(
        monkeypatch,
        tmp_path,
        card={"task_id": TASK_ID, "status": "finished"},
        child_pid=os.getpid(),
    )

    sweep = manager._reconcile_persisted_requests  # noqa: SLF001
    assert sweep() == {"watched": 0, "finalized": 0}

    deferred = _deferred_row(manager)
    assert deferred["bridge_provider_may_be_active"] is True
    assert deferred["reconciliation_deferred"] == "bridge_cancel_publication_failed"


def test_moot_helper_returns_none_while_identity_still_matches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = _setup(
        monkeypatch,
        tmp_path,
        card={"task_id": TASK_ID, "status": "finished"},
        verdict=process_launcher.PidIdentityVerdict.MATCH,
    )
    events = _rows(manager)
    latest = {**manager._event_identity(events), **events[-1]}  # noqa: SLF001
    status = {
        "request_id": REQUEST_ID,
        "state": latest.get("state"),
        "adapter_id": latest.get("adapter_id"),
        "latest_event": latest,
    }

    moot = manager._bridge_cancel_moot_home_removed(  # noqa: SLF001
        REQUEST_ID, events, latest, None, status
    )
    assert moot is None


def test_cancel_failure_with_present_home_still_defers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = _setup(
        monkeypatch,
        tmp_path,
        card={"task_id": TASK_ID, "status": "finished"},
        home_exists=True,
        cancel_raises=True,
    )

    sweep = manager._reconcile_persisted_requests  # noqa: SLF001
    assert sweep() == {"watched": 0, "finalized": 0}

    deferred = _deferred_row(manager)
    assert deferred["bridge_provider_may_be_active"] is True
    assert deferred["reconciliation_deferred"] == "bridge_cancel_publication_failed"


def test_processing_card_bound_to_request_still_defers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = _setup(
        monkeypatch,
        tmp_path,
        card={
            "task_id": TASK_ID,
            "status": "processing",
            "launch_request_id": REQUEST_ID,
        },
    )

    sweep = manager._reconcile_persisted_requests  # noqa: SLF001
    assert sweep() == {"watched": 0, "finalized": 0}

    deferred = _deferred_row(manager)
    assert deferred["bridge_provider_may_be_active"] is True
    assert deferred["reconciliation_deferred"] == "bridge_cancel_publication_failed"
