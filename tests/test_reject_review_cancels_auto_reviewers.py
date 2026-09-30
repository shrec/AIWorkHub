import json

import pytest

from aiworkhub import core, process_launcher, server


def test_target_request_id_match_cancels_running_auto_reviewer_via_request_json_fallback(
    monkeypatch, tmp_path
):
    manager = object.__new__(process_launcher.ProcessManager)
    manager.process_dir = tmp_path
    latest = {
        "REQ_AUTO": {"task_id": "R_AUTO", "state": "running"},
    }
    monkeypatch.setattr(manager, "_latest_by_request_stable", lambda: (latest, ("stable",)))
    (tmp_path / "REQ_AUTO.request.json").write_text(
        json.dumps({"quality_review": {"target_request_id": "REQ_REJECTED"}}),
        encoding="utf-8",
    )
    calls = []

    def cancel(request_id, reason):
        calls.append((request_id, reason))
        return {"ok": True, "state": "cancelled"}

    monkeypatch.setattr(manager, "cancel", cancel)

    result = manager.cancel_disposed_reviewer_processes(
        [{"task_id": "R_OTHER_BOUND", "finished": True, "cleanup_error": ""}],
        rejected_request_id="REQ_REJECTED",
    )

    assert result["ok"] is True
    assert result["state"] == "completed"
    assert result["reviewer_task_ids"] == ["R_OTHER_BOUND"]
    assert calls == [("REQ_AUTO", "parent_candidate_rejected")]
    assert result["cancelled"] == [{
        "task_id": "R_AUTO",
        "request_id": "REQ_AUTO",
        "match": "target_request",
        "ok": True,
        "state": "cancelled",
        "blocked_reason": "",
    }]


def test_target_request_id_mismatch_leaves_reviewer_running(monkeypatch, tmp_path):
    manager = object.__new__(process_launcher.ProcessManager)
    manager.process_dir = tmp_path
    latest = {
        "REQ_OTHER": {
            "task_id": "R_OTHER",
            "state": "running",
            "quality_review": {"target_request_id": "REQ_DIFFERENT"},
        },
    }
    monkeypatch.setattr(manager, "_latest_by_request_stable", lambda: (latest, ("stable",)))
    monkeypatch.setattr(
        manager,
        "cancel",
        lambda *_args, **_kwargs: pytest.fail("mismatched target must not cancel"),
    )

    result = manager.cancel_disposed_reviewer_processes(
        [], rejected_request_id="REQ_REJECTED"
    )

    assert result["ok"] is True
    assert result["state"] == "completed"
    assert result["cancelled"] == []


def test_empty_reviewer_finalization_still_cancels_target_request_match(monkeypatch, tmp_path):
    manager = object.__new__(process_launcher.ProcessManager)
    manager.process_dir = tmp_path
    latest = {
        "REQ_AUTO2": {
            "task_id": "R_AUTO2",
            "state": "starting",
            "quality_review": {"target_request_id": "REQ_REJECTED"},
        },
    }
    monkeypatch.setattr(manager, "_latest_by_request_stable", lambda: (latest, ("stable",)))
    calls = []

    def cancel(request_id, reason):
        calls.append((request_id, reason))
        return {"ok": True, "state": "cancelled"}

    monkeypatch.setattr(manager, "cancel", cancel)

    result = manager.cancel_disposed_reviewer_processes(
        [], rejected_request_id="REQ_REJECTED"
    )

    assert result["ok"] is True
    assert result["state"] == "completed"
    assert result["reviewer_task_ids"] == []
    assert calls == [("REQ_AUTO2", "parent_candidate_rejected")]
    assert result["cancelled"] == [{
        "task_id": "R_AUTO2",
        "request_id": "REQ_AUTO2",
        "match": "target_request",
        "ok": True,
        "state": "cancelled",
        "blocked_reason": "",
    }]


def test_terminal_target_request_match_is_not_cancelled(monkeypatch, tmp_path):
    manager = object.__new__(process_launcher.ProcessManager)
    manager.process_dir = tmp_path
    latest = {
        "REQ_TERM": {
            "task_id": "R_TERM",
            "state": "review_ready",
            "quality_review": {"target_request_id": "REQ_REJECTED"},
        },
    }
    monkeypatch.setattr(manager, "_latest_by_request_stable", lambda: (latest, ("stable",)))
    monkeypatch.setattr(
        manager,
        "cancel",
        lambda *_args, **_kwargs: pytest.fail("terminal reviewer must not cancel"),
    )

    result = manager.cancel_disposed_reviewer_processes(
        [], rejected_request_id="REQ_REJECTED"
    )

    assert result["ok"] is True
    assert result["state"] == "completed"
    assert result["cancelled"] == []


def test_server_task_reject_review_forwards_rejected_request_id(monkeypatch):
    calls = []

    def reject(**kwargs):
        return {
            "ok": True,
            "reviewer_finalization": [],
            "stdout": json.dumps({"rejection_disposition": {"request_id": "REQ_REJECTED"}}),
            **kwargs,
        }

    class Manager:
        def cancel_disposed_reviewer_processes(self, rows, *, rejected_request_id=""):
            calls.append((rows, rejected_request_id))
            return {
                "schema_id": "aiworkhub.reviewer_process_cancellation.v1",
                "ok": True,
                "state": "completed",
                "reviewer_task_ids": [],
                "cancelled": [{
                    "task_id": "R_AUTO",
                    "request_id": "REQ_AUTO",
                    "match": "target_request",
                    "ok": True,
                    "state": "cancelled",
                    "blocked_reason": "",
                }],
            }

    monkeypatch.setattr(core, "reject_review", reject)
    monkeypatch.setattr(process_launcher, "default_manager", lambda: Manager())

    result = server.aiworkhub_task_reject_review("T_PARENT", "rework")

    assert calls == [([], "REQ_REJECTED")]
    assert result["reviewer_process_cancellation"]["cancelled"][0]["match"] == "target_request"
