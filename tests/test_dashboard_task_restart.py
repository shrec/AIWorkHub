"""Tests for the dashboard one-click task restart backend (NF-2026-01481).

Each test monkeypatches the canonical ``core``/``task_store`` functions and a
fake ``process_launcher`` manager, so none of this ever touches a real task
store or spawns a process.
"""

from __future__ import annotations

from typing import Any

import pytest

from aiworkhub import core, dashboard_mcp_app, process_launcher, storage_observability, task_store


class _FakeManager:
    def __init__(self, *, alive: bool) -> None:
        self._alive = alive
        self.status_calls: list[str] = []
        self.launch_calls: list[dict[str, Any]] = []

    def status(self, request_id: str) -> dict[str, Any]:
        self.status_calls.append(request_id)
        return {"ok": True, "process_alive": self._alive}

    def launch(self, **kwargs: Any) -> dict[str, Any]:
        self.launch_calls.append(kwargs)
        return {
            "ok": True,
            "request_id": "req-restart-1",
            "state": "launched",
            "blocked_reason": None,
        }


def _task(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": "needfix-NF-TEST-0001",
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": None,
        "launch_request_id": "req-prior-1",
    }
    base.update(overrides)
    return base


def _stub_task_lookup(monkeypatch: pytest.MonkeyPatch, task: dict[str, Any]) -> None:
    monkeypatch.setattr(core, "repo_root", lambda: "/repo")
    monkeypatch.setattr(task_store, "get_task", lambda root, task_id: task)
    monkeypatch.setattr(storage_observability, "invalidate", lambda root: None)


def _install_fake_manager(monkeypatch: pytest.MonkeyPatch, *, alive: bool) -> _FakeManager:
    manager = _FakeManager(alive=alive)
    monkeypatch.setattr(process_launcher, "default_manager", lambda: manager)
    return manager


def test_tool_registered_in_task_retention_write_tools() -> None:
    assert (
        dashboard_mcp_app.TASK_RETENTION_WRITE_TOOLS["aiworkhub_dashboard_task_restart"]
        is dashboard_mcp_app.task_restart_view
    )


def test_confirm_gate_required() -> None:
    response = dashboard_mcp_app.task_restart_view("needfix-NF-TEST-0001", confirm=False)
    assert response["ok"] is False
    assert response["error"] == "task_restart_confirmation_required"
    assert response["task_id"] == "needfix-NF-TEST-0001"
    assert response["server_tool"] == "aiworkhub_dashboard_task_restart"


def test_invalid_task_id_refused() -> None:
    response = dashboard_mcp_app.task_restart_view("", confirm=True)
    assert response["ok"] is False
    assert response["error"] == "invalid_task_id"


def test_task_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "repo_root", lambda: "/repo")
    monkeypatch.setattr(task_store, "get_task", lambda root, task_id: None)

    response = dashboard_mcp_app.task_restart_view("needfix-NF-TEST-0001", confirm=True)

    assert response["ok"] is False
    assert response["error"] == "task_not_found"


def test_pending_unclaimed_launches_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(status="pending", worker_status="unclaimed")
    monkeypatch.setattr(task_store, "canonical_status", lambda t: "pending")
    _stub_task_lookup(monkeypatch, task)
    manager = _install_fake_manager(monkeypatch, alive=False)

    response = dashboard_mcp_app.task_restart_view(task["task_id"], confirm=True)

    assert response["ok"] is True
    assert response["path"] == "launch"
    assert manager.launch_calls == [{"task_id": task["task_id"]}]
    assert response["launch"]["request_id"] == "req-restart-1"
    assert response["steps"][-1]["step"] == "launch"


def test_blocked_operational_substatus_retries_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(status="blocked", worker_status="timed_out", launch_request_id="req-prior-7")
    monkeypatch.setattr(task_store, "canonical_status", lambda t: "blocked")
    _stub_task_lookup(monkeypatch, task)
    manager = _install_fake_manager(monkeypatch, alive=False)

    calls: list[dict[str, Any]] = []

    def fake_retry_terminal_task(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(core, "retry_terminal_task", fake_retry_terminal_task)

    response = dashboard_mcp_app.task_restart_view(
        task["task_id"], reason="owner retry", confirm=True
    )

    assert response["ok"] is True
    assert response["path"] == "retry_terminal"
    assert calls == [
        {
            "task_id": task["task_id"],
            "request_id": "req-prior-7",
            "terminal_substatus": "timed_out",
            "reason": "owner retry",
        }
    ]
    assert manager.launch_calls == [{"task_id": task["task_id"]}]


def test_blocked_retry_terminal_failure_never_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(status="blocked", worker_status="cancelled")
    monkeypatch.setattr(task_store, "canonical_status", lambda t: "blocked")
    _stub_task_lookup(monkeypatch, task)
    manager = _install_fake_manager(monkeypatch, alive=False)

    monkeypatch.setattr(
        core, "retry_terminal_task", lambda **kwargs: {"ok": False, "stderr": "boom"}
    )

    response = dashboard_mcp_app.task_restart_view(task["task_id"], confirm=True)

    assert response["ok"] is False
    assert response["error"] == "retry_terminal_failed"
    assert response["steps"][-1]["ok"] is False
    assert manager.launch_calls == []


def test_blocked_non_operational_recovers_rework(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(status="blocked", worker_status="blocked_scope_rejected")
    monkeypatch.setattr(task_store, "canonical_status", lambda t: "blocked")
    _stub_task_lookup(monkeypatch, task)
    manager = _install_fake_manager(monkeypatch, alive=False)

    calls: list[dict[str, Any]] = []

    def fake_recover_blocked_rework(task_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"task_id": task_id, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(core, "recover_blocked_rework", fake_recover_blocked_rework)

    response = dashboard_mcp_app.task_restart_view(
        task["task_id"], reason="owner rework", confirm=True
    )

    assert response["ok"] is True
    assert response["path"] == "recover_blocked_rework"
    assert calls == [{"task_id": task["task_id"], "feedback_reason": "owner rework"}]
    assert manager.launch_calls == [{"task_id": task["task_id"]}]


def test_processing_alive_refuses_task_running(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(
        status="processing",
        worker_status="claimed",
        claimed_by="worker-7",
        launch_request_id="req-live-1",
    )
    monkeypatch.setattr(task_store, "canonical_status", lambda t: "processing")
    _stub_task_lookup(monkeypatch, task)
    manager = _install_fake_manager(monkeypatch, alive=True)

    response = dashboard_mcp_app.task_restart_view(task["task_id"], confirm=True)

    assert response["ok"] is False
    assert response["error"] == "task_running"
    assert manager.status_calls == ["req-live-1"]
    assert manager.launch_calls == []


def test_processing_dead_reconciles_then_recovers_and_relaunches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(
        status="processing",
        worker_status="claimed",
        claimed_by="worker-7",
        launch_request_id="req-dead-1",
        claim_epoch=3,
    )
    monkeypatch.setattr(task_store, "canonical_status", lambda t: "processing")
    _stub_task_lookup(monkeypatch, task)
    manager = _install_fake_manager(monkeypatch, alive=False)

    recovered_task = _task(
        status="blocked", worker_status="timed_out", launch_request_id="req-dead-1"
    )
    get_task_calls: list[str] = []

    def fake_get_task(root: str, task_id: str) -> dict[str, Any]:
        get_task_calls.append(task_id)
        return task if len(get_task_calls) == 1 else recovered_task

    monkeypatch.setattr(task_store, "get_task", fake_get_task)

    reconcile_calls: list[tuple[Any, ...]] = []

    def fake_reconcile_dead_processing_task(
        task_id: str, request_id: str, claim_epoch: int
    ) -> dict[str, Any]:
        reconcile_calls.append((task_id, request_id, claim_epoch))
        return {"ok": True}

    monkeypatch.setattr(
        core, "reconcile_dead_processing_task", fake_reconcile_dead_processing_task
    )

    retry_calls: list[dict[str, Any]] = []

    def fake_retry_terminal_task(**kwargs: Any) -> dict[str, Any]:
        retry_calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(core, "retry_terminal_task", fake_retry_terminal_task)

    response = dashboard_mcp_app.task_restart_view(
        task["task_id"], reason="owner recovers orphan", confirm=True
    )

    assert response["ok"] is True
    assert response["path"] == "reconcile_dead_processing+retry_terminal"
    assert reconcile_calls == [(task["task_id"], "req-dead-1", 3)]
    assert len(get_task_calls) == 2
    assert retry_calls == [
        {
            "task_id": task["task_id"],
            "request_id": "req-dead-1",
            "terminal_substatus": "timed_out",
            "reason": "owner recovers orphan",
        }
    ]
    assert manager.status_calls == ["req-dead-1"]
    assert manager.launch_calls == [{"task_id": task["task_id"]}]


def test_processing_dead_reconcile_failure_stops_before_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(
        status="processing",
        worker_status="claimed",
        claimed_by="worker-7",
        launch_request_id="req-dead-2",
        claim_epoch=5,
    )
    monkeypatch.setattr(task_store, "canonical_status", lambda t: "processing")
    _stub_task_lookup(monkeypatch, task)
    manager = _install_fake_manager(monkeypatch, alive=False)

    monkeypatch.setattr(
        core,
        "reconcile_dead_processing_task",
        lambda *a, **k: {"ok": False, "stderr": "dead_claim_mismatch"},
    )

    def _fail_if_called(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("must not be called")

    monkeypatch.setattr(core, "retry_terminal_task", _fail_if_called)
    monkeypatch.setattr(core, "recover_blocked_rework", _fail_if_called)

    response = dashboard_mcp_app.task_restart_view(
        task["task_id"], reason="owner recovers orphan", confirm=True
    )

    assert response["ok"] is False
    assert response["error"] == "reconcile_dead_processing_failed"
    assert manager.launch_calls == []


def test_processing_reconcile_then_taskstoreerror_still_invalidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(
        status="processing",
        worker_status="claimed",
        claimed_by="worker-7",
        launch_request_id="req-dead-3",
        claim_epoch=2,
    )
    monkeypatch.setattr(task_store, "canonical_status", lambda t: "processing")
    _stub_task_lookup(monkeypatch, task)
    manager = _install_fake_manager(monkeypatch, alive=False)

    monkeypatch.setattr(
        core, "reconcile_dead_processing_task", lambda *a, **k: {"ok": True}
    )

    get_task_calls: list[str] = []

    def fake_get_task(root: str, task_id: str) -> dict[str, Any]:
        get_task_calls.append(task_id)
        if len(get_task_calls) == 1:
            return task
        raise task_store.TaskStoreError("boom")

    monkeypatch.setattr(task_store, "get_task", fake_get_task)

    invalidate_calls: list[str] = []
    monkeypatch.setattr(
        storage_observability, "invalidate", lambda root: invalidate_calls.append(root)
    )

    response = dashboard_mcp_app.task_restart_view(
        task["task_id"], reason="owner recovers orphan", confirm=True
    )

    assert response["ok"] is False
    assert len(get_task_calls) == 2
    assert manager.launch_calls == []
    assert invalidate_calls == ["/repo"]


@pytest.mark.parametrize("status", ["review", "finished", "archived", "superseded"])
def test_non_restartable_statuses_refused(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    task = _task(status=status, worker_status=status)
    monkeypatch.setattr(task_store, "canonical_status", lambda t: status)
    _stub_task_lookup(monkeypatch, task)
    manager = _install_fake_manager(monkeypatch, alive=False)

    response = dashboard_mcp_app.task_restart_view(task["task_id"], confirm=True)

    assert response["ok"] is False
    assert response["error"] == f"not_restartable:{status}"
    assert manager.launch_calls == []


def test_pending_claimed_refuses_without_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(status="pending", worker_status="claimed", claimed_by="worker-3")
    monkeypatch.setattr(task_store, "canonical_status", lambda t: "pending")
    _stub_task_lookup(monkeypatch, task)
    manager = _install_fake_manager(monkeypatch, alive=False)

    response = dashboard_mcp_app.task_restart_view(task["task_id"], confirm=True)

    assert response["ok"] is False
    assert response["error"] == "task_claimed"
    assert manager.launch_calls == []


def test_launch_non_dict_result_reports_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(status="pending", worker_status="unclaimed")
    monkeypatch.setattr(task_store, "canonical_status", lambda t: "pending")
    _stub_task_lookup(monkeypatch, task)

    class _NoneLaunchManager:
        def launch(self, **kwargs: Any) -> Any:
            return None

    monkeypatch.setattr(process_launcher, "default_manager", lambda: _NoneLaunchManager())

    response = dashboard_mcp_app.task_restart_view(task["task_id"], confirm=True)

    assert response["ok"] is False


def test_launch_dict_without_ok_key_reports_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(status="pending", worker_status="unclaimed")
    monkeypatch.setattr(task_store, "canonical_status", lambda t: "pending")
    _stub_task_lookup(monkeypatch, task)

    class _NoOkKeyManager:
        def launch(self, **kwargs: Any) -> dict[str, Any]:
            return {"request_id": "req-restart-2", "state": "launched"}

    monkeypatch.setattr(process_launcher, "default_manager", lambda: _NoOkKeyManager())

    response = dashboard_mcp_app.task_restart_view(task["task_id"], confirm=True)

    assert response["ok"] is False


def test_launch_refusal_error_propagates_to_response(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(status="pending", worker_status="unclaimed")
    monkeypatch.setattr(task_store, "canonical_status", lambda t: "pending")
    _stub_task_lookup(monkeypatch, task)

    class _RefusedManager:
        def launch(self, **kwargs: Any) -> dict[str, Any]:
            return {"ok": False, "error": "x_refused"}

    monkeypatch.setattr(process_launcher, "default_manager", lambda: _RefusedManager())

    response = dashboard_mcp_app.task_restart_view(task["task_id"], confirm=True)

    assert response["ok"] is False
    assert response["error"] == "x_refused"
    assert response["steps"][-1]["error"] == "x_refused"
