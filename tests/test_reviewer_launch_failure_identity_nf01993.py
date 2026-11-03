"""NF-2026-01993: a failed reviewer launch names its request, card, adapter and cause."""

from __future__ import annotations

import json

from aiworkhub import process_launcher

REQUEST_ID = "reserved-request-01993"
TASK_ID = "REVIEWER_NF01993"
IDENTITY = {
    "request_id": REQUEST_ID,
    "task_id": TASK_ID,
    "target_request_id": "target-request",
    "target_task_id": "target-task",
    "adapter_id": "codex_cli",
}


def _manager(tmp_path, monkeypatch, *, bind=None, claim=None, create=None):
    manager = process_launcher.ProcessManager(repo=tmp_path)
    calls: dict[str, list[str]] = {"mark": [], "terminal": [], "wake": []}
    card = {
        "task_id": TASK_ID,
        "read_only": True,
        "allowed_writes": [],
        "topic": "quality_review",
        "runner": "codex",
        "launch_request_id": REQUEST_ID,
    }
    monkeypatch.setattr(
        process_launcher.core,
        "create_task",
        lambda **_kw: create or {"ok": True, "receipt_state": "existing_identical"},
    )
    monkeypatch.setattr(
        process_launcher.core,
        "release_review_wake",
        lambda _task, reason: calls["wake"].append(reason),
    )
    monkeypatch.setattr(
        manager,
        "_show_task",
        lambda _task: {"returncode": 0, "stdout": json.dumps(card), "stderr": ""},
    )
    monkeypatch.setattr(
        process_launcher.task_engine,
        "claim_start_exact",
        lambda *_a, **_kw: claim or {"ok": True},
    )
    monkeypatch.setattr(
        process_launcher.task_engine, "trusted_claim_contention", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        process_launcher, "_committed_claim_card", lambda *_a, **_kw: {"claim_epoch": 3}
    )
    monkeypatch.setattr(manager, "_bind_reviewer_claim_epoch", bind or (lambda *_a: True))
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_launch_failed",
        lambda *_a, reason, **_kw: calls["mark"].append(reason) or {"ok": True},
    )
    monkeypatch.setattr(
        manager,
        "_terminalize_reviewer_attempt",
        lambda *_a, reason: calls["terminal"].append(reason),
    )
    return manager, calls


def _ensure(manager):
    return manager._ensure_quality_reviewer_card_bound(
        request_id=REQUEST_ID,
        target_request_id="target-request",
        target_task_id="target-task",
        reviewer_task_id=TASK_ID,
        runner="codex",
        adapter_id="codex_cli",
        lens="correctness",
        target_card={"adapter_id": "claude_cli"},
        terminalize_on_failure=True,
    )


def _assert_identity(reply):
    assert reply["ok"] is False
    for key, value in IDENTITY.items():
        assert reply.get(key) == value, key


def test_binding_exception_reaches_reply_and_release_reason(tmp_path, monkeypatch):
    def _raise(*_args):
        raise RuntimeError("db locked")

    manager, calls = _manager(tmp_path, monkeypatch, bind=_raise)
    reply = _ensure(manager)

    _assert_identity(reply)
    error = reply["error"]
    assert error.startswith("quality_review_claim_binding_failed")
    assert "RuntimeError" in error and "db locked" in error
    assert calls["mark"] == [error]
    assert calls["terminal"] == [error]


def test_binding_exception_text_is_truncated_to_500(tmp_path, monkeypatch):
    def _raise(*_args):
        raise RuntimeError("x" * 2000)

    manager, calls = _manager(tmp_path, monkeypatch, bind=_raise)
    reply = _ensure(manager)

    assert len(reply["error"]) == 500
    assert reply["error"].startswith("quality_review_claim_binding_failed:RuntimeError:")
    assert calls["mark"] == [reply["error"]]


def test_binding_false_return_names_binding_not_persisted(tmp_path, monkeypatch):
    manager, calls = _manager(tmp_path, monkeypatch, bind=lambda *_a: False)
    reply = _ensure(manager)

    _assert_identity(reply)
    expected = "quality_review_claim_binding_failed:binding_not_persisted"
    assert reply["error"] == expected
    assert calls["mark"] == [expected]


def test_create_failure_carries_identity(tmp_path, monkeypatch):
    manager, calls = _manager(
        tmp_path, monkeypatch, create={"ok": False, "error": "store down"}
    )
    reply = _ensure(manager)

    _assert_identity(reply)
    assert reply["error"] == "quality_review_task_create_failed:store down"
    assert calls["terminal"] == [reply["error"]]
    assert calls["wake"] == [reply["error"]]


def test_claim_failure_carries_identity(tmp_path, monkeypatch):
    manager, calls = _manager(
        tmp_path, monkeypatch, claim={"ok": False, "stderr": "claim refused"}
    )
    reply = _ensure(manager)

    _assert_identity(reply)
    assert reply["error"] == "quality_review_claim_failed:claim refused"
    assert calls["mark"] == []


def test_refusal_before_reservation_says_request_not_created(tmp_path, monkeypatch):
    manager = process_launcher.ProcessManager(repo=tmp_path)
    monkeypatch.setattr(
        manager,
        "_show_task",
        lambda _task: {"returncode": 1, "stdout": "", "stderr": "missing"},
    )
    monkeypatch.setattr(manager, "_live_reviewer_receipt", lambda *_a, **_kw: None)
    reserved: list[object] = []
    monkeypatch.setattr(
        manager,
        "_reserve_quality_reviewer_attempt",
        lambda **kw: reserved.append(kw) or {"ok": False},
    )

    reply = manager.launch_quality_reviewer(
        target_request_id="target-request",
        target_task_id="target-task",
        reviewer_task_id=TASK_ID,
        runner="codex",
        adapter_id="codex_cli",
        lens="correctness",
    )

    assert reply["ok"] is False
    assert reply["error"] == "quality_review_target_not_review_ready"
    assert reply["request_created"] is False
    assert reserved == []
