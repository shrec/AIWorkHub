"""NF-2026-02388: automatic reviewers must not be wasted.

Three downstream incidents, each reproduced through fakes only (no provider
CLI, no live manager loop):

(a) automatic seeding launched a reviewer for a lens that already had a
    manual or verified reviewer for the same target request;
(b) an automatic reviewer was routed to an adapter that cannot be pinned
    (``deepseek_v4-pro``) and only failed after it was launched;
(c) an automatic reviewer ran against a candidate the manager had already
    rejected, including one launched in the window before the cancel sweep.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import review_orchestrator as ro

PACKET = "a" * 64
CANDIDATE = "b" * 64
IDENTITY = {
    "target_task_id": "T_TARGET",
    "target_request_id": "REQ_TARGET",
    "claim_epoch": "3",
    "packet_sha256": PACKET,
    "candidate_sha256": CANDIDATE,
}
LENS = "correctness"


def _review_ready_status(state: str = "review_ready") -> dict[str, Any]:
    ready = state == "review_ready"
    return {
        "ok": True,
        "state": state,
        "task_card": {
            "task_id": IDENTITY["target_task_id"],
            "status": "review" if ready else "pending",
            "worker_status": "review" if ready else "pending",
            "terminal_substatus": "review_ready" if ready else "",
        },
    }


@pytest.fixture
def resolved_identity(monkeypatch):
    monkeypatch.setattr(ro, "resolve_target_identity", lambda _status: {
        "identity": dict(IDENTITY),
        "conflict": "",
        "identity_source": "terminal_review_evidence",
        "workspace_identity": "",
    })


# --- (a) seeding treats a manual / verified reviewer as coverage -------------


class _RecoveryManager:
    def __init__(self, repo, card, existing):
        self.repo = repo
        self._card = card
        self._existing = existing
        self.lookups: list[dict[str, str]] = []
        self.events: list[dict[str, Any]] = []

    def status(self, request_id):
        assert request_id == IDENTITY["target_request_id"]
        return {"ok": True, "state": "review_ready", "task_card": self._card}

    def existing_lens_reviewer(self, **kwargs):
        self.lookups.append(dict(kwargs))
        return self._existing

    def _append_event(self, event):
        self.events.append(dict(event))


def _run_recovery(monkeypatch, tmp_path, existing):
    card = {
        "task_id": IDENTITY["target_task_id"],
        "topic": "code",
        "terminal_substatus": "review_ready",
        "terminal_review": {"evidence": {
            "request_identity": {"request_id": IDENTITY["target_request_id"]},
            "changed_path_hashes": {"src/a.py": "c" * 64},
        }},
    }
    manager = _RecoveryManager(tmp_path, card, existing)
    registered: list[Any] = []
    monkeypatch.setattr(ro.task_store, "list_review_queue_cards", lambda *_a, **_k: [card])
    monkeypatch.setattr(ro.task_store, "manager_ready_marker", lambda _card: None)
    # This chain has no hashed child of its own on any attempt slot.
    monkeypatch.setattr(ro.task_store, "get_task", lambda *_a, **_k: None)
    monkeypatch.setattr(ro.review_lifecycle, "_chain_identity", lambda **kw: dict(kw))
    monkeypatch.setattr(ro, "resolve_target_identity", lambda _status: {
        "identity": dict(IDENTITY),
        "conflict": "",
        "identity_source": "terminal_review_evidence",
        "workspace_identity": "",
    })
    monkeypatch.setattr(ro, "candidate_registration", lambda **_kw: {
        "candidate_sha256": CANDIDATE,
        "required_reviewer_lenses": [LENS],
    })
    monkeypatch.setattr(
        ro, "register_candidate",
        lambda _manager, **kw: registered.append(kw) or {"ok": True},
    )
    result = ro.recover_review_ready_targets(manager, db_path=tmp_path / "lifecycle.db")
    return result, registered, manager


def test_seeding_skips_lens_already_held_by_manual_or_verified_reviewer(
    monkeypatch, tmp_path,
):
    manual = {
        "task_id": "MANUAL_REVIEWER", "request_id": "REQ_MANUAL",
        "runner": "claude_opus-5", "adapter_id": "claude_cli",
        "model": "claude-opus-5", "state": "sealed",
    }
    result, registered, manager = _run_recovery(monkeypatch, tmp_path, manual)

    assert registered == [], "a lens with a manual/verified reviewer was re-seeded"
    assert result["review_recovery_reasons"] == {"already_present": 1}
    assert manager.lookups == [{
        "target_task_id": IDENTITY["target_task_id"],
        "target_request_id": IDENTITY["target_request_id"],
        "claim_epoch": IDENTITY["claim_epoch"],
        "lens": LENS,
    }]


def test_seeding_still_ensures_lens_with_no_reviewer_of_any_origin(monkeypatch, tmp_path):
    result, registered, _manager = _run_recovery(monkeypatch, tmp_path, None)

    assert len(registered) == 1
    assert result["review_recovery_ensured"] == 1


# --- (b) an unpinnable / unusable route is skipped before launch -------------


def test_unpinnable_reviewer_route_is_refused_with_reason():
    refusal = ro.reviewer_route_adapter_refusal(
        {"runner": "deepseek_v4-pro", "adapter_id": "deepseek_api", "model": "v4-pro"}
    )
    assert refusal.startswith("adapter_unpinnable:runner_adapter_mismatch:")
    assert "runner=deepseek_v4-pro" in refusal and "adapter=deepseek_api" in refusal
    # A claude runner owns canonical rows, so only an adapter with its own row pins.
    assert ro.reviewer_route_adapter_refusal(
        {"runner": "claude_opus-5", "adapter_id": "vscode_lm", "model": "claude-opus-5"}
    ).startswith("adapter_unpinnable:workforce_route_absent:")
    assert ro.reviewer_route_adapter_refusal(
        {"runner": "claude_opus-5", "adapter_id": "claude_cli", "model": "claude-opus-5"}
    ) == ""


def test_route_selection_skips_unpinnable_route_before_policy(tmp_path):
    route = {"runner": "deepseek_v4-pro", "adapter_id": "deepseek_api", "model": "v4-pro"}
    refusal = ro._review_route_refusal(
        tmp_path, route, ro._review_route_identity(route), {},
        frozenset(), frozenset(),
    )
    assert refusal.startswith("adapter_unpinnable:")


def test_custom_selector_route_that_cannot_pin_is_unavailable_with_skip(tmp_path):
    orchestrator = object.__new__(ro.ReviewOrchestrator)
    orchestrator.manager = SimpleNamespace(repo=tmp_path)
    orchestrator.route_selector = lambda *_args: {
        "runner": "deepseek_v4-pro", "adapter_id": "deepseek_api", "model": "v4-pro",
    }
    with pytest.raises(ro.ReviewRouteUnavailable) as raised:
        orchestrator._select_attempt_route("R1", LENS, frozenset())
    assert [row["skip"].split(":", 1)[0] for row in raised.value.skipped] == [
        "adapter_unpinnable"
    ]


class _LaunchManager:
    def __init__(self, repo, statuses):
        self.repo = repo
        self._statuses = list(statuses)
        self.launches: list[dict[str, Any]] = []
        self.cancels: list[tuple[str, str]] = []

    def status(self, request_id):
        assert request_id == IDENTITY["target_request_id"]
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]

    def launch_quality_reviewer(self, **kwargs):
        self.launches.append(dict(kwargs))
        return {
            "ok": True, "request_id": "REQ_AUTO_REVIEWER",
            "task_id": kwargs["reviewer_task_id"], "runner": kwargs["runner"],
            "adapter_id": kwargs["adapter_id"], "model": kwargs["model"],
            "state": "starting",
        }

    def cancel(self, request_id, reason):
        self.cancels.append((request_id, reason))
        return {"ok": True, "state": "cancelled"}


def _orchestrator(monkeypatch, manager):
    orchestrator = object.__new__(ro.ReviewOrchestrator)
    orchestrator.manager = manager
    retired: list[str] = []
    monkeypatch.setattr(
        orchestrator, "_retire_route_attempt",
        lambda _action, _attempt, reason: retired.append(reason) or True,
    )
    monkeypatch.setattr(
        orchestrator, "_bind_route_attempt_request", lambda *_a, **_k: True,
    )
    action = SimpleNamespace(
        action_id=1, chain_id=1, lens=LENS, action_type="launch",
        descriptor={"chain_identity": dict(IDENTITY)},
    )
    return orchestrator, action, retired


def _attempt(runner, adapter_id, model):
    return {
        "attempt_index": 1, "state": "planned", "failure_reason": "",
        "reviewer_task_id": ro.ReviewOrchestrator._reviewer_task_id(IDENTITY, LENS),
        "reviewer_request_id": "",
        "runner": runner, "adapter_id": adapter_id, "model": model,
    }


def test_unpinnable_attempt_is_retired_with_reason_and_never_launched(
    monkeypatch, tmp_path, resolved_identity,
):
    manager = _LaunchManager(tmp_path, [_review_ready_status()])
    orchestrator, action, retired = _orchestrator(monkeypatch, manager)

    result = orchestrator._launch_route_attempt(
        action, _attempt("deepseek_v4-pro", "deepseek_api", "v4-pro"),
    )

    assert manager.launches == [], "a reviewer was started on an unpinnable adapter"
    assert len(retired) == 1 and retired[0].startswith("adapter_unpinnable:")
    assert ro.reviewer_route_start_failed(retired[0])
    assert result["_route_terminal_failure"] == retired[0]
    assert result["_route_dispatch"]["relaunch_distinct_route"] is True


# --- (c) no reviewer for a rejected / superseded candidate -------------------


def test_no_automatic_reviewer_starts_for_rejected_target(
    monkeypatch, tmp_path, resolved_identity,
):
    manager = _LaunchManager(tmp_path, [_review_ready_status("rejected")])
    orchestrator, action, _retired = _orchestrator(monkeypatch, manager)

    with pytest.raises(ro._DeferredLaunch) as raised:
        orchestrator._launch_route_attempt(
            action, _attempt("claude_haiku", "claude_cli", "claude-haiku-4.5"),
        )

    assert manager.launches == [], "a reviewer was started on a rejected candidate"
    assert raised.value.reason == "target_not_review_ready:rejected"


def test_no_automatic_reviewer_starts_after_claim_epoch_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(ro, "resolve_target_identity", lambda _status: {
        "identity": {**IDENTITY, "claim_epoch": "4"},
        "conflict": "", "identity_source": "terminal_review_evidence",
        "workspace_identity": "",
    })
    manager = _LaunchManager(tmp_path, [_review_ready_status()])
    orchestrator, action, _retired = _orchestrator(monkeypatch, manager)

    with pytest.raises(ro._DeferredLaunch) as raised:
        orchestrator._launch_route_attempt(
            action, _attempt("claude_haiku", "claude_cli", "claude-haiku-4.5"),
        )

    assert manager.launches == []
    assert raised.value.reason == "target_claim_epoch_superseded:4"


def test_reviewer_launched_in_reject_window_is_cancelled(
    monkeypatch, tmp_path, resolved_identity,
):
    # review_ready when the launch is gated, rejected by the time it returns:
    # the reject's cancel sweep ran before this reviewer's row existed.
    manager = _LaunchManager(
        tmp_path, [_review_ready_status(), _review_ready_status("rejected")],
    )
    orchestrator, action, _retired = _orchestrator(monkeypatch, manager)

    with pytest.raises(ro._DeferredLaunch) as raised:
        orchestrator._launch_route_attempt(
            action, _attempt("claude_haiku", "claude_cli", "claude-haiku-4.5"),
        )

    assert len(manager.launches) == 1
    assert manager.cancels == [("REQ_AUTO_REVIEWER", "parent_candidate_rejected")]
    assert raised.value.receipt["cancelled_reviewer_request_id"] == "REQ_AUTO_REVIEWER"
    assert raised.value.receipt["cancel_state"] == "cancelled"


def test_review_ready_target_still_launches_and_is_not_cancelled(
    monkeypatch, tmp_path, resolved_identity,
):
    manager = _LaunchManager(tmp_path, [_review_ready_status()])
    orchestrator, action, retired = _orchestrator(monkeypatch, manager)

    result = orchestrator._launch_route_attempt(
        action, _attempt("claude_haiku", "claude_cli", "claude-haiku-4.5"),
    )

    assert len(manager.launches) == 1
    assert manager.cancels == [] and retired == []
    assert result["request_id"] == "REQ_AUTO_REVIEWER"


# --- rework: a launched reviewer is always bound; transient reads never cancel --


class _ScriptedLaunchManager(_LaunchManager):
    """Status replies scripted per call; an Exception entry is raised."""

    def __init__(self, repo, statuses, *, cancel_error: Exception | None = None):
        super().__init__(repo, statuses)
        self._cancel_error = cancel_error
        self.order: list[str] = []

    def status(self, request_id):
        reply = super().status(request_id)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def launch_quality_reviewer(self, **kwargs):
        self.order.append("launch")
        return super().launch_quality_reviewer(**kwargs)

    def cancel(self, request_id, reason):
        self.order.append("cancel")
        self.cancels.append((request_id, reason))
        if self._cancel_error is not None:
            raise self._cancel_error
        return {"ok": True, "state": "cancelled"}


def _recording_bind(monkeypatch, orchestrator, manager):
    binds: list[str] = []

    def bind(_action, _attempt, request_id):
        manager.order.append("bind")
        binds.append(request_id)
        return True

    monkeypatch.setattr(orchestrator, "_bind_route_attempt_request", bind)
    return binds


def test_reject_window_cancel_failure_still_binds_launched_reviewer(
    monkeypatch, tmp_path, resolved_identity,
):
    manager = _ScriptedLaunchManager(
        tmp_path, [_review_ready_status(), _review_ready_status("rejected")],
        cancel_error=RuntimeError("cancel backend down"),
    )
    orchestrator, action, _retired = _orchestrator(monkeypatch, manager)
    binds = _recording_bind(monkeypatch, orchestrator, manager)

    with pytest.raises(ro._DeferredLaunch) as raised:
        orchestrator._launch_route_attempt(
            action, _attempt("claude_haiku", "claude_cli", "claude-haiku-4.5"),
        )

    assert binds == ["REQ_AUTO_REVIEWER"], "a live reviewer was left unbound"
    assert manager.order == ["launch", "bind", "cancel"]
    assert raised.value.reason == "target_not_review_ready:rejected"
    assert raised.value.receipt["cancel_state"] == "cancel_failed:RuntimeError"
    assert raised.value.receipt["cancelled_reviewer_request_id"] == "REQ_AUTO_REVIEWER"


def test_post_launch_status_read_failure_does_not_cancel(
    monkeypatch, tmp_path, resolved_identity,
):
    manager = _ScriptedLaunchManager(
        tmp_path,
        [_review_ready_status(), OSError("status store busy"), _review_ready_status()],
    )
    orchestrator, action, retired = _orchestrator(monkeypatch, manager)
    binds = _recording_bind(monkeypatch, orchestrator, manager)

    result = orchestrator._launch_route_attempt(
        action, _attempt("claude_haiku", "claude_cli", "claude-haiku-4.5"),
    )

    assert manager.cancels == [], "a transient status fault cancelled a reviewer"
    assert binds == ["REQ_AUTO_REVIEWER"] and retired == []
    assert result["request_id"] == "REQ_AUTO_REVIEWER"


def test_pre_launch_status_read_failure_still_defers(
    monkeypatch, tmp_path, resolved_identity,
):
    manager = _ScriptedLaunchManager(
        tmp_path, [OSError("status store busy"), _review_ready_status()],
    )
    orchestrator, action, _retired = _orchestrator(monkeypatch, manager)

    with pytest.raises(ro._DeferredLaunch) as raised:
        orchestrator._launch_route_attempt(
            action, _attempt("claude_haiku", "claude_cli", "claude-haiku-4.5"),
        )

    assert manager.launches == []
    assert raised.value.reason == "target_status_unavailable:OSError"


def test_review_ready_target_without_resolvable_identity_never_launches(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(ro, "resolve_target_identity", lambda _status: {
        "identity": {}, "conflict": "", "identity_source": "",
        "workspace_identity": "",
    })
    manager = _LaunchManager(tmp_path, [_review_ready_status()])
    orchestrator, action, _retired = _orchestrator(monkeypatch, manager)

    with pytest.raises(ro._DeferredLaunch) as raised:
        orchestrator._launch_route_attempt(
            action, _attempt("claude_haiku", "claude_cli", "claude-haiku-4.5"),
        )

    assert manager.launches == [], "an unresolved target identity launched a reviewer"
    assert raised.value.reason == "target_identity_unavailable"


def test_reject_window_cancel_runs_when_bind_is_not_durable(
    monkeypatch, tmp_path, resolved_identity,
):
    # A retry of this still-unbound attempt meets the pre-launch gate, which
    # defers before it could reconcile the reviewer: it is cancelled now.
    manager = _ScriptedLaunchManager(
        tmp_path, [_review_ready_status(), _review_ready_status("rejected")],
    )
    orchestrator, action, _retired = _orchestrator(monkeypatch, manager)

    def bind(_action, _attempt, _request_id):
        manager.order.append("bind")
        return False

    monkeypatch.setattr(orchestrator, "_bind_route_attempt_request", bind)

    with pytest.raises(ro._DeferredLaunch) as raised:
        orchestrator._launch_route_attempt(
            action, _attempt("claude_haiku", "claude_cli", "claude-haiku-4.5"),
        )

    assert manager.order == ["launch", "bind", "cancel"]
    assert manager.cancels == [("REQ_AUTO_REVIEWER", "parent_candidate_rejected")]
    assert raised.value.reason == "target_not_review_ready:rejected"
    assert raised.value.receipt["cancel_state"] == "cancelled"


def test_bind_not_durable_on_review_ready_target_stays_pending_uncancelled(
    monkeypatch, tmp_path, resolved_identity,
):
    manager = _ScriptedLaunchManager(tmp_path, [_review_ready_status()])
    orchestrator, action, retired = _orchestrator(monkeypatch, manager)
    monkeypatch.setattr(
        orchestrator, "_bind_route_attempt_request", lambda *_a, **_k: False,
    )

    result = orchestrator._launch_route_attempt(
        action, _attempt("claude_haiku", "claude_cli", "claude-haiku-4.5"),
    )

    assert result is None
    assert manager.cancels == [] and retired == []
