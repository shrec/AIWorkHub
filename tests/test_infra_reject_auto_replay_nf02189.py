"""NF-2026-02189: an infrastructure reject replays the retained candidate with no model run.

Every test drives the real ``aiworkhub_task_reject_review`` -> ``core.reject_review``
-> ``recover_blocked_rework`` path against a real task store and a retained
candidate on disk.  Only ``ProcessManager`` is faked: its replay lane has its own
suite, and what matters here is whether it is asked to launch and with what.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    accept_failure_cause,
    core,
    launch_replay_guard,
    process_launcher,
    process_launcher_accept_review,
    server,
    task_engine,
    task_store,
)
from aiworkhub.learning_commit import (  # noqa: E402
    INFRASTRUCTURE_FAILURE_CATEGORIES,
    FailureCategory,
)
from test_accept_failure_cause_nf02171 import (  # noqa: E402
    _BASE_FAILED_TRIAGE,
    _VALIDATION_ERROR,
)
from test_blocked_rework_recovery import (  # noqa: E402,F401  (the fixture is used by name)
    _NF1112_PATH,
    _NF1112_REQUEST,
    _nf1112_sealed_review,
    nf1112_coord,
)

_REPLAY_REQUEST = "e" * 32
_RUNNER = "claude_coding"
_TOPIC = "coding"
_STARTED = {
    "ok": True,
    "request_id": _REPLAY_REQUEST,
    "execution_mode": "validation_only_replay",
    "provider_launched": False,
}
_BOTH_DISPOSITIONS = pytest.mark.parametrize("to", ["blocked", "pending"])


class _Manager:
    """Stands in for ``ProcessManager`` and records what the handler asked of it."""

    def __init__(self) -> None:
        self.launch_calls: list[dict[str, Any]] = []
        self.order: list[str] = []
        self.on_launch: Callable[..., dict[str, Any]] = lambda **_kwargs: dict(_STARTED)

    def cancel_disposed_reviewer_processes(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.order.append("reviewer_cancellation")
        return {}

    def launch(self, **kwargs: Any) -> dict[str, Any]:
        self.order.append("launch")
        self.launch_calls.append(kwargs)
        return self.on_launch(**kwargs)


@pytest.fixture
def manager(nf1112_coord: Path, monkeypatch: pytest.MonkeyPatch) -> _Manager:
    fake = _Manager()
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)
    monkeypatch.setattr(
        process_launcher_accept_review, "review_finding_dispositions", lambda *_a, **_k: []
    )
    return fake


@pytest.fixture
def proven_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    proof = {"base_oid": "b" * 40, "head_oid": "c" * 40, "drifted_paths": [_NF1112_PATH]}
    monkeypatch.setattr(
        core.base_drift_proof_module, "base_drift_proof", lambda *_a, **_k: (proof, None)
    )


def _connect(repo: Path) -> sqlite3.Connection:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_event(repo: Path, task_id: str, event: str, payload: dict[str, Any]) -> None:
    conn = _connect(repo)
    try:
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, event, _RUNNER, json.dumps(payload), "2026-11-09T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _patch_card(repo: Path, task_id: str, **fields: Any) -> None:
    conn = _connect(repo)
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        card = {**json.loads(row["card_json"]), **fields}
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id)
        )
        conn.commit()
    finally:
        conn.close()


def _row(repo: Path, task_id: str) -> dict[str, Any]:
    conn = _connect(repo)
    try:
        row = conn.execute(
            "SELECT status, worker_status, claimed_by, claimed_at, started_at, completed_at, "
            "card_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row)


def _card(repo: Path, task_id: str) -> dict[str, Any]:
    return json.loads(_row(repo, task_id)["card_json"])


def _events(repo: Path, task_id: str, event: str) -> list[dict[str, Any]]:
    conn = _connect(repo)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event=? ORDER BY event_id",
            (task_id, event),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row["payload_json"]) for row in rows]


def _retained_review(repo: Path, task_id: str) -> dict[str, object]:
    """A review card with its sealed delta, candidate bytes and terminal event."""
    descriptor = _nf1112_sealed_review(repo, task_id)
    candidate = (
        repo / ".aiworkhub" / "runtime" / "worktrees" / _NF1112_REQUEST / "worktree"
    ).joinpath(*_NF1112_PATH.split("/"))
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"candidate\n")
    terminal = _card(repo, task_id)["terminal_review"]
    _insert_event(repo, task_id, "terminal_review", {**terminal, "task_id": task_id})
    return descriptor


def _reject(task_id: str, **kwargs: Any) -> dict[str, Any]:
    reply = server.aiworkhub_task_reject_review(task_id=task_id, reason="infrastructure", **kwargs)
    assert reply["ok"] is True, reply
    return reply


def _route_kwargs(repo: Path, task_id: str, route: str) -> tuple[dict[str, Any], str]:
    """The reject arguments for one route, and the category that route must record."""
    if route == "classify":
        _insert_event(
            repo,
            task_id,
            accept_failure_cause.EVENT,
            {
                "request_id": _NF1112_REQUEST,
                "error": _VALIDATION_ERROR,
                "triage": _BASE_FAILED_TRIAGE,
            },
        )
        return {}, "validation_environment"
    return {"failure_category": route}, route


def _snapshot_before_recovery(monkeypatch: pytest.MonkeyPatch, repo: Path) -> list[dict[str, Any]]:
    """Capture the row exactly as the reject committed it, before the replay touches it."""
    seen: list[dict[str, Any]] = []
    recover = core.recover_blocked_rework

    def spying(task_id: str, **kwargs: Any) -> dict[str, Any]:
        seen.append(_row(repo, task_id))
        return recover(task_id, **kwargs)

    monkeypatch.setattr(core, "recover_blocked_rework", spying)
    return seen


def _assert_as_the_reject_left_it(
    repo: Path, task_id: str, to: str, reject_left: list[dict[str, Any]]
) -> None:
    assert len(reject_left) == 1
    assert _row(repo, task_id) == reject_left[0]
    assert _row(repo, task_id)["status"] == to
    assert "validation_only_replay_authorization" not in _card(repo, task_id)


def test_replay_categories_are_the_two_infrastructure_categories() -> None:
    assert accept_failure_cause.INFRA_REJECT_REPLAY_CATEGORIES == {
        FailureCategory.BASE_DRIFT.value,
        FailureCategory.VALIDATION_ENVIRONMENT.value,
    }
    assert {FailureCategory.BASE_DRIFT, FailureCategory.VALIDATION_ENVIRONMENT} <= (
        INFRASTRUCTURE_FAILURE_CATEGORIES
    )


@_BOTH_DISPOSITIONS
@pytest.mark.parametrize("route", ["validation_environment", "base_drift", "classify"])
def test_infrastructure_reject_starts_one_provider_free_replay(
    nf1112_coord: Path, manager: _Manager, proven_drift: None, to: str, route: str
) -> None:
    task_id = f"NF02189_REPLAY_{to}_{route}".upper()
    _retained_review(nf1112_coord, task_id)
    kwargs, category = _route_kwargs(nf1112_coord, task_id, route)

    reply = _reject(task_id, to=to, **kwargs)

    assert reply["failure_category"] == category
    assert reply["automatic_validation_replay"] == {
        "attempted": True,
        "succeeded": True,
        "reason": f"validation_only_replay_started:{category}",
        "request_id": _REPLAY_REQUEST,
    }
    # Runner, topic and adapter are omitted, so launch derives them from the card.
    assert manager.launch_calls == [{"task_id": task_id}]

    card = _card(nf1112_coord, task_id)
    assert _row(nf1112_coord, task_id)["status"] == "pending"
    grant = launch_replay_guard.validation_only_replay_authorization(card, task_id)
    assert grant is not None
    assert grant["predecessor_request_id"] == _NF1112_REQUEST
    (recovery,) = _events(nf1112_coord, task_id, "blocked_rework_recovery")
    assert recovery["transition"] == f"{to}->pending"
    assert recovery["validation_only_replay"] is True
    assert recovery["infra_reject_replay"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"to": "pending"},
        {"to": "blocked"},
        {"to": "blocked", "failure_category": "provider_runtime"},
        {"to": "pending", "failure_category": "cancellation_or_timeout"},
        {"to": "archived"},
    ],
)
def test_every_other_reject_has_no_key_and_starts_nothing(
    nf1112_coord: Path, manager: _Manager, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, str]
) -> None:
    task_id = "NF02189_OTHER"
    _retained_review(nf1112_coord, task_id)

    def recovery_is_not_wanted(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a non-infrastructure reject must not recover")

    monkeypatch.setattr(core, "recover_blocked_rework", recovery_is_not_wanted)

    reply = _reject(task_id, **kwargs)

    assert "automatic_validation_replay" not in reply
    assert manager.launch_calls == []
    assert _events(nf1112_coord, task_id, "blocked_rework_recovery") == []
    pinned = _card(nf1112_coord, task_id).get("rejection_disposition") or {}
    assert "rejected_validation_only_replay" not in pinned


@_BOTH_DISPOSITIONS
@pytest.mark.parametrize("route", ["validation_environment", "base_drift", "classify"])
def test_rejecting_a_replay_does_not_replay_again(
    nf1112_coord: Path, manager: _Manager, proven_drift: None, to: str, route: str
) -> None:
    task_id = f"NF02189_ONE_SHOT_{to}_{route}".upper()
    _retained_review(nf1112_coord, task_id)
    _patch_card(
        nf1112_coord,
        task_id,
        validation_only_replay_authorization={
            "task_id": task_id,
            "request_id": _NF1112_REQUEST,
            "one_episode_binding": True,
        },
    )
    kwargs, category = _route_kwargs(nf1112_coord, task_id, route)

    reply = _reject(task_id, to=to, **kwargs)

    assert reply["failure_category"] == category

    assert reply["automatic_validation_replay"] == {
        "attempted": False,
        "succeeded": False,
        "reason": "rejected_request_was_validation_only_replay",
        "request_id": None,
    }
    assert manager.launch_calls == []
    assert _row(nf1112_coord, task_id)["status"] == to
    assert _events(nf1112_coord, task_id, "blocked_rework_recovery") == []
    assert _card(nf1112_coord, task_id)["rejection_disposition"][
        "rejected_validation_only_replay"
    ] is True


def test_a_grant_for_another_request_does_not_count_as_a_replay(
    nf1112_coord: Path, manager: _Manager
) -> None:
    task_id = "NF02189_STALE_GRANT"
    _retained_review(nf1112_coord, task_id)
    _patch_card(
        nf1112_coord,
        task_id,
        validation_only_replay_authorization={"task_id": task_id, "request_id": "f" * 32},
    )

    reply = _reject(task_id, to="pending", failure_category="validation_environment")

    assert reply["automatic_validation_replay"]["succeeded"] is True


@_BOTH_DISPOSITIONS
def test_a_card_with_no_retained_predecessor_stays_on_the_rework_path(
    nf1112_coord: Path, manager: _Manager, monkeypatch: pytest.MonkeyPatch, to: str
) -> None:
    task_id = f"NF02189_NO_PREDECESSOR_{to}".upper()
    descriptor = _retained_review(nf1112_coord, task_id)
    Path(str(descriptor["artifact_path"])).write_bytes(b"tampered\n")
    reject_left = _snapshot_before_recovery(monkeypatch, nf1112_coord)

    reply = _reject(task_id, to=to, failure_category="validation_environment")

    replay = reply["automatic_validation_replay"]
    assert replay["attempted"] is True
    assert replay["succeeded"] is False
    assert replay["request_id"] is None
    assert "validation_only_replay_missing_evidence" in replay["reason"]
    assert manager.launch_calls == []
    assert "rework_predecessor" not in _card(nf1112_coord, task_id)
    _assert_as_the_reject_left_it(nf1112_coord, task_id, to, reject_left)


@_BOTH_DISPOSITIONS
@pytest.mark.parametrize(
    "predecessor_fields",
    [{"request_id": ""}, {"changed_path_hashes": {}}],
    ids=["empty_request_id", "empty_hashes"],
)
def test_a_predecessor_without_identity_gets_no_replay_grant(
    nf1112_coord: Path, manager: _Manager, to: str, predecessor_fields: dict[str, Any]
) -> None:
    task_id = f"NF02189_THIN_{to}_{next(iter(predecessor_fields))}".upper()
    _retained_review(nf1112_coord, task_id)
    assert core.reject_review(
        task_id, "infrastructure", to=to, failure_category="validation_environment"
    )["ok"] is True
    predecessor = {**_card(nf1112_coord, task_id)["rework_predecessor"], **predecessor_fields}
    _patch_card(nf1112_coord, task_id, rework_predecessor=predecessor)
    before = _row(nf1112_coord, task_id)

    result = core.recover_blocked_rework(
        task_id, feedback_reason="replay", validation_only_replay=True, infra_reject_preimage_out={}
    )

    assert result["ok"] is False, result
    assert "validation_only_replay_missing_evidence" in result["stderr"]
    assert _row(nf1112_coord, task_id) == before


@_BOTH_DISPOSITIONS
def test_a_predecessor_from_an_earlier_episode_gets_no_replay_grant(
    nf1112_coord: Path, manager: _Manager, to: str
) -> None:
    task_id = f"NF02189_STALE_PREDECESSOR_{to}".upper()
    _retained_review(nf1112_coord, task_id)
    assert core.reject_review(
        task_id, "infrastructure", to=to, failure_category="validation_environment"
    )["ok"] is True
    predecessor = {**_card(nf1112_coord, task_id)["rework_predecessor"], "request_id": "8" * 32}
    _patch_card(nf1112_coord, task_id, rework_predecessor=predecessor)
    before = _row(nf1112_coord, task_id)

    result = core.recover_blocked_rework(
        task_id, feedback_reason="replay", validation_only_replay=True, infra_reject_preimage_out={}
    )

    assert result["ok"] is False, result
    assert "infra_reject_replay_predecessor_mismatch" in result["stderr"]
    assert _row(nf1112_coord, task_id) == before


def test_the_replay_flag_requires_validation_only_replay(
    nf1112_coord: Path, manager: _Manager
) -> None:
    task_id = "NF02189_FLAG_ALONE"
    _retained_review(nf1112_coord, task_id)
    assert core.reject_review(
        task_id, "infrastructure", to="pending", failure_category="validation_environment"
    )["ok"] is True

    result = core.recover_blocked_rework(
        task_id, feedback_reason="rework", infra_reject_preimage_out={}
    )

    assert result["ok"] is False, result
    assert "infra_reject_replay_requires_validation_only_replay" in result["stderr"]


def test_without_the_flag_a_pending_card_still_cannot_replay(
    nf1112_coord: Path, manager: _Manager
) -> None:
    task_id = "NF02189_NO_FLAG"
    _retained_review(nf1112_coord, task_id)
    assert core.reject_review(
        task_id, "infrastructure", to="pending", failure_category="validation_environment"
    )["ok"] is True

    result = core.recover_blocked_rework(
        task_id, feedback_reason="replay", validation_only_replay=True
    )

    assert result["ok"] is False, result
    assert "not_blocked:current=pending" in result["stderr"]


def test_the_flag_refuses_a_rejection_that_was_not_infrastructure(
    nf1112_coord: Path, manager: _Manager
) -> None:
    task_id = "NF02189_NOT_INFRA"
    _retained_review(nf1112_coord, task_id)
    assert core.reject_review(task_id, "real defect", to="pending")["ok"] is True

    result = core.recover_blocked_rework(
        task_id, feedback_reason="replay", validation_only_replay=True, infra_reject_preimage_out={}
    )

    assert result["ok"] is False, result
    assert "infra_reject_replay_rejection_not_infrastructure" in result["stderr"]


@_BOTH_DISPOSITIONS
def test_a_refused_launch_leaves_the_card_as_the_reject_left_it(
    nf1112_coord: Path, manager: _Manager, monkeypatch: pytest.MonkeyPatch, to: str
) -> None:
    task_id = f"NF02189_LAUNCH_REFUSED_{to}".upper()
    _retained_review(nf1112_coord, task_id)
    manager.on_launch = lambda **_kwargs: {"ok": False, "blocked_reason": "launch_disabled"}
    reject_left = _snapshot_before_recovery(monkeypatch, nf1112_coord)

    reply = _reject(task_id, to=to, failure_category="validation_environment")

    assert reply["automatic_validation_replay"] == {
        "attempted": True,
        "succeeded": False,
        "reason": "launch_refused:launch_disabled",
        "request_id": None,
    }
    _assert_as_the_reject_left_it(nf1112_coord, task_id, to, reject_left)
    (undone,) = _events(nf1112_coord, task_id, "infra_reject_replay_undone")
    assert undone["restored_status"] == to


@_BOTH_DISPOSITIONS
def test_a_launch_that_raises_leaves_the_card_as_the_reject_left_it(
    nf1112_coord: Path, manager: _Manager, monkeypatch: pytest.MonkeyPatch, to: str
) -> None:
    task_id = f"NF02189_LAUNCH_RAISES_{to}".upper()
    _retained_review(nf1112_coord, task_id)

    def explode(**_kwargs: Any) -> dict[str, Any]:
        raise OSError("sandbox unavailable")

    manager.on_launch = explode
    reject_left = _snapshot_before_recovery(monkeypatch, nf1112_coord)

    reply = _reject(task_id, to=to, failure_category="validation_environment")

    assert reply["automatic_validation_replay"]["attempted"] is True
    assert reply["automatic_validation_replay"]["succeeded"] is False
    assert reply["automatic_validation_replay"]["reason"] == "launch_raised:OSError"
    _assert_as_the_reject_left_it(nf1112_coord, task_id, to, reject_left)


@_BOTH_DISPOSITIONS
def test_a_replay_that_fails_after_its_claim_is_also_undone(
    nf1112_coord: Path, manager: _Manager, monkeypatch: pytest.MonkeyPatch, to: str
) -> None:
    task_id = f"NF02189_CLAIMED_THEN_FAILED_{to}".upper()
    _retained_review(nf1112_coord, task_id)

    def claim_then_fail(**_kwargs: Any) -> dict[str, Any]:
        claim = task_engine.claim_start_exact(
            nf1112_coord, task_id, _RUNNER, _TOPIC, request_id=_REPLAY_REQUEST
        )
        assert claim["ok"] is True, claim
        failed = task_engine.mark_launch_failed(
            nf1112_coord, task_id, _RUNNER, reason="workspace error", request_id=_REPLAY_REQUEST
        )
        assert failed["ok"] is True, failed
        return {
            "ok": False,
            "blocked_reason": "workspace error",
            "request_id": _REPLAY_REQUEST,
            "state": "launch_failed",
        }

    manager.on_launch = claim_then_fail
    reject_left = _snapshot_before_recovery(monkeypatch, nf1112_coord)

    reply = _reject(task_id, to=to, failure_category="validation_environment")

    assert reply["automatic_validation_replay"] == {
        "attempted": True,
        "succeeded": False,
        "reason": "launch_refused:workspace error",
        "request_id": _REPLAY_REQUEST,
    }
    _assert_as_the_reject_left_it(nf1112_coord, task_id, to, reject_left)


def test_a_card_another_actor_moved_on_is_not_restored(
    nf1112_coord: Path, manager: _Manager
) -> None:
    task_id = "NF02189_MOVED_ON"
    _retained_review(nf1112_coord, task_id)
    moved_on: dict[str, Any] = {}

    def claim_and_keep(**_kwargs: Any) -> dict[str, Any]:
        claim = task_engine.claim_start_exact(
            nf1112_coord, task_id, _RUNNER, _TOPIC, request_id=_REPLAY_REQUEST
        )
        assert claim["ok"] is True, claim
        moved_on.update(_row(nf1112_coord, task_id))
        return {"ok": False, "blocked_reason": "lost the race"}

    manager.on_launch = claim_and_keep

    reply = _reject(task_id, to="pending", failure_category="validation_environment")

    replay = reply["automatic_validation_replay"]
    assert replay["succeeded"] is False
    assert "restore_refused:" in replay["reason"]
    assert "infra_reject_undo_state_changed:processing/claimed" in replay["reason"]
    assert _row(nf1112_coord, task_id) == moved_on


def test_replay_runs_after_the_learning_closeout_and_reviewer_cancellation(
    nf1112_coord: Path, manager: _Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = "NF02189_ORDER"
    _retained_review(nf1112_coord, task_id)
    closeout = server._with_learning_closeout

    def recording(receipt: Any, **kwargs: Any) -> Any:
        manager.order.append("learning_closeout")
        return closeout(receipt, **kwargs)

    monkeypatch.setattr(server, "_with_learning_closeout", recording)

    _reject(task_id, to="pending", failure_category="validation_environment")

    assert manager.order == ["learning_closeout", "reviewer_cancellation", "launch"]


def test_a_failed_replay_skips_neither_step_before_it(
    nf1112_coord: Path, manager: _Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = "NF02189_ORDER_FAILED"
    _retained_review(nf1112_coord, task_id)
    manager.on_launch = lambda **_kwargs: {"ok": False, "blocked_reason": "launch_disabled"}
    closeout = server._with_learning_closeout

    def recording(receipt: Any, **kwargs: Any) -> Any:
        manager.order.append("learning_closeout")
        return closeout(receipt, **kwargs)

    monkeypatch.setattr(server, "_with_learning_closeout", recording)

    reply = _reject(task_id, to="blocked", failure_category="validation_environment")

    assert reply["ok"] is True
    assert "reviewer_process_cancellation" in reply
    assert manager.order == ["learning_closeout", "reviewer_cancellation", "launch"]


def test_a_launch_that_reports_a_provider_run_is_not_reported_as_a_replay(
    nf1112_coord: Path, manager: _Manager
) -> None:
    task_id = "NF02189_PROVIDER_RUN"
    _retained_review(nf1112_coord, task_id)
    manager.on_launch = lambda **_kwargs: {
        "ok": True, "request_id": _REPLAY_REQUEST, "provider_launched": True,
    }

    reply = _reject(task_id, to="pending", failure_category="validation_environment")

    assert reply["automatic_validation_replay"]["succeeded"] is False
    assert reply["automatic_validation_replay"]["attempted"] is True


def test_a_refused_recovery_does_not_put_a_secret_in_the_reason(
    nf1112_coord: Path, manager: _Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = "NF02189_SECRET_IN_STDERR"
    _retained_review(nf1112_coord, task_id)
    bearer = "ghpBearerSecretValue123"
    api_key = "sk-ProviderSecretKey12345678"

    def refused(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "stderr": f"recovery failed: Bearer {bearer} key {api_key}"}

    monkeypatch.setattr(core, "recover_blocked_rework", refused)

    reply = _reject(task_id, to="pending", failure_category="validation_environment")

    replay = reply["automatic_validation_replay"]
    assert replay["attempted"] is True
    assert replay["succeeded"] is False
    assert replay["reason"].startswith("recovery_refused:")
    assert bearer not in replay["reason"]
    assert api_key not in replay["reason"]
    assert manager.launch_calls == []
