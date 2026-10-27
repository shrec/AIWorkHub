"""NF-2026-00804: retryable claim contention defers, every other failure fails closed.

``task_engine.claim_start_exact`` converts bounded pre-provider writer
contention into a typed ``task_queue_contention`` receipt. Both launcher claim
callers -- the quality-reviewer binder and the validation-only replay -- must
defer on exactly that receipt under the same request identity, and keep
failing closed on anything else.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import db_writer, process_launcher, task_engine, task_store

_RECOVERY_PATH = Path(__file__).with_name(
    "test_quality_reviewer_starting_reservation_recovery.py"
)
_spec = importlib.util.spec_from_file_location("_nf804_recovery", _RECOVERY_PATH)
assert _spec is not None and _spec.loader is not None
_recovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_recovery)

RUNNER = "codex"
TOPIC = "quality_review"
CONTENTION_REASON = "quality_review_claim_write_contention"


def _always_contended(db_path, *, timeout_s=None):  # type: ignore[no-untyped-def]
    raise db_writer.WriteLeaseTimeout(str(db_path), 30.0, 30.0)


def _contended_claim(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Route the canonical claim through a wedged writer lease."""
    real_claim = task_engine.claim_start_exact
    results: list[dict] = []

    def contended(*args, **kwargs):  # type: ignore[no-untyped-def]
        with pytest.MonkeyPatch.context() as lease_patch:
            lease_patch.setattr(db_writer, "write_lease", _always_contended)
            result = real_claim(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(task_engine, "claim_start_exact", contended)
    return results


def _real_contention(tmp_path: Path, task_id: str, request_id: str) -> dict:
    with pytest.MonkeyPatch.context() as lease_patch:
        lease_patch.setattr(db_writer, "write_lease", _always_contended)
        result = task_engine.claim_start_exact(
            tmp_path, task_id, RUNNER, TOPIC, request_id=request_id
        )
    assert result["ok"] is False
    assert result["task_queue_contention"]["retryable"] is True
    return result


def _mutated(result: dict, mutation: str) -> dict:
    mutated = copy.deepcopy(result)
    receipt = mutated["task_queue_contention"]
    if mutation == "no_receipt":
        mutated.pop("task_queue_contention")
    elif mutation == "not_retryable":
        receipt["retryable"] = False
    elif mutation == "retryable_not_bool":
        receipt["retryable"] = "true"
    elif mutation == "provider_launched":
        receipt["provider_launched"] = True
    elif mutation == "wrong_schema":
        receipt["schema_id"] = "aiworkhub.task_queue.write_contention.v0"
    elif mutation == "other_request":
        receipt["request_id"] = "another-request"
    else:  # pragma: no cover -- parametrization guard
        raise AssertionError(mutation)
    return mutated


FAIL_CLOSED_MUTATIONS = (
    "no_receipt",
    "not_retryable",
    "retryable_not_bool",
    "provider_launched",
    "wrong_schema",
    "other_request",
)


def _seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_id: str) -> None:
    _recovery._seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )


def _assert_pending_unclaimed(tmp_path: Path, task_id: str) -> None:
    card = task_store.get_task(tmp_path, task_id)
    assert card is not None
    assert card["status"] == "pending"
    assert card["worker_status"] == "unclaimed"
    assert not card.get("launch_request_id")


def _predicate_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    shared = task_engine.trusted_claim_contention

    def spy(result, *, task_id, request_id):  # type: ignore[no-untyped-def]
        calls.append(request_id)
        return shared(result, task_id=task_id, request_id=request_id)

    monkeypatch.setattr(task_engine, "trusted_claim_contention", spy)
    return calls


# --------------------------------------------------------------------------
# The shared predicate


def test_predicate_accepts_only_the_exact_trusted_receipt(tmp_path, monkeypatch):
    task_id = "NF804_PREDICATE"
    request_id = "nf804-predicate"
    _seeded(tmp_path, monkeypatch, task_id)
    result = _real_contention(tmp_path, task_id, request_id)

    receipt = task_engine.trusted_claim_contention(
        result, task_id=task_id, request_id=request_id
    )
    assert receipt == result["task_queue_contention"]
    assert receipt["schema_id"] == task_engine.CLAIM_CONTENTION_SCHEMA
    assert task_engine.trusted_claim_contention(
        result, task_id="OTHER_TASK", request_id=request_id
    ) is None
    assert task_engine.trusted_claim_contention(
        {**result, "ok": True}, task_id=task_id, request_id=request_id
    ) is None
    for mutation in FAIL_CLOSED_MUTATIONS:
        assert task_engine.trusted_claim_contention(
            _mutated(result, mutation), task_id=task_id, request_id=request_id
        ) is None, mutation
    # The predicate is owned by task_engine; the launcher keeps no copy.
    assert not hasattr(process_launcher, "trusted_claim_contention")
    assert task_engine.contention_deferral(None) is None


# --------------------------------------------------------------------------
# Caller 1: the quality-reviewer claim binder


def _reviewer_manager(tmp_path, monkeypatch, task_id: str, request_id: str):
    _seeded(tmp_path, monkeypatch, task_id)
    manager = _recovery._manager_for_periodic_scan(tmp_path, monkeypatch)
    starting = _recovery._starting(
        request_id, task_id=task_id, deadline=time.time() + 60.0
    )
    starting.pop("reviewer_claim_epoch")
    manager._append_event(starting)
    created: list[str] = []
    monkeypatch.setattr(
        process_launcher.core,
        "create_task",
        lambda **kwargs: (
            created.append(kwargs["task_id"])
            or {"ok": True, "receipt_state": "existing_identical"}
        ),
    )

    def show_task(requested: str) -> dict:
        card = (
            {"task_id": requested, "adapter_id": "claude_cli"}
            if requested != task_id
            else {
                **(task_store.get_task(tmp_path, task_id) or {}),
                "read_only": True,
                "allowed_writes": [],
            }
        )
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    monkeypatch.setattr(manager, "_show_task", show_task)
    terminalized: list[str] = []
    monkeypatch.setattr(
        manager,
        "_terminalize_reviewer_attempt",
        lambda *_args, **kwargs: terminalized.append(str(kwargs.get("reason"))),
    )
    return manager, created, terminalized


def _bind(manager, task_id: str, request_id: str) -> dict:
    return manager._ensure_quality_reviewer_card_bound(
        request_id=request_id,
        target_request_id="target-request",
        target_task_id="target-task",
        reviewer_task_id=task_id,
        runner=RUNNER,
        adapter_id="codex_cli",
        lens="correctness",
        target_card={"adapter_id": "claude_cli"},
        terminalize_on_failure=True,
    )


def test_reviewer_contention_retains_reservation_and_starts_nothing(
    tmp_path, monkeypatch
):
    task_id = "NF804_REVIEWER_CONTENDED"
    request_id = "nf804-reviewer-contended"
    manager, created, terminalized = _reviewer_manager(
        tmp_path, monkeypatch, task_id, request_id
    )
    claims = _contended_claim(monkeypatch)
    predicate_calls = _predicate_spy(monkeypatch)
    launched: list[dict] = []
    monkeypatch.setattr(
        manager, "_live_reviewer_receipt", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        process_launcher.quality_review,
        "assess_reviewer_launch_target",
        lambda **_kwargs: {"can_launch": True},
    )
    monkeypatch.setattr(
        manager,
        "_reserve_quality_reviewer_attempt",
        lambda **_kwargs: {"ok": True, "request_id": request_id},
    )
    monkeypatch.setattr(
        manager,
        "_launch_reserved_quality_reviewer",
        lambda **kwargs: launched.append(kwargs),
    )

    # The launch refuses a target that is not one exact ledger request of the
    # target task before reserving anything (NF-2026-01748).
    manager._append_event({
        "request_id": "target-request", "task_id": "target-task",
        "runner": RUNNER, "state": "review_ready",
    })
    result = manager.launch_quality_reviewer(
        target_request_id="target-request",
        target_task_id="target-task",
        reviewer_task_id=task_id,
        runner=RUNNER,
        adapter_id="codex_cli",
        lens="correctness",
    )

    assert len(claims) == 1
    assert predicate_calls == [request_id]
    assert result["ok"] is False
    assert result["error"].startswith(
        "quality_review_claim_failed:task_queue_write_contention:"
    )
    assert result["recovery_state"] == "starting_reservation_retained"
    assert result["claim_recovery_bound"] is True
    receipt = result["task_queue_contention"]
    assert receipt == claims[0]["task_queue_contention"]
    assert receipt["schema_id"] == task_engine.CLAIM_CONTENTION_SCHEMA
    assert receipt["request_id"] == request_id
    assert receipt["retryable"] is True
    assert receipt["provider_launched"] is False
    # Never terminalized, never launched, no second reviewer card.
    assert terminalized == []
    assert launched == []
    assert created == [task_id]
    latest = manager._latest_by_request()[request_id]
    assert latest["state"] == "starting"
    # Nothing committed, so the reservation is not labelled commit-ambiguous.
    assert latest["claim_recovery_state"] == "claim_write_contention_deferred"
    assert latest["claim_recovery_state"] == task_engine.CLAIM_CONTENTION_RECOVERY_STATE
    assert latest["claim_recovery_reason"] == CONTENTION_REASON
    assert "reviewer_claim_epoch" not in latest
    assert not any(
        event.get("state") in {"blocked", "launch_failed", "failed"}
        for event in manager._request_events(request_id)
    )
    _assert_pending_unclaimed(tmp_path, task_id)


def test_reviewer_contention_retry_reuses_the_same_request_identity(
    tmp_path, monkeypatch
):
    task_id = "NF804_REVIEWER_RETRY"
    request_id = "nf804-reviewer-retry"
    manager, _created, terminalized = _reviewer_manager(
        tmp_path, monkeypatch, task_id, request_id
    )
    real_claim = task_engine.claim_start_exact
    _contended_claim(monkeypatch)

    deferred = _bind(manager, task_id, request_id)
    assert deferred["recovery_state"] == "starting_reservation_retained"
    _assert_pending_unclaimed(tmp_path, task_id)

    monkeypatch.setattr(task_engine, "claim_start_exact", real_claim)
    retried = _bind(manager, task_id, request_id)

    assert retried["ok"] is True, retried
    assert terminalized == []
    card = task_store.get_task(tmp_path, task_id)
    assert card is not None
    assert card["status"] == "processing"
    assert card["launch_request_id"] == request_id
    assert card["claim_epoch"] == 1
    latest = manager._latest_by_request()[request_id]
    assert latest["reviewer_claim_epoch"] == 1


@pytest.mark.parametrize("mutation", FAIL_CLOSED_MUTATIONS)
def test_reviewer_untrusted_claim_failure_still_fails_closed(
    tmp_path, monkeypatch, mutation
):
    task_id = f"NF804_REVIEWER_{mutation.upper()}"
    request_id = f"nf804-reviewer-{mutation.replace('_', '-')}"
    manager, _created, terminalized = _reviewer_manager(
        tmp_path, monkeypatch, task_id, request_id
    )
    claim = _mutated(_real_contention(tmp_path, task_id, request_id), mutation)
    monkeypatch.setattr(
        task_engine, "claim_start_exact", lambda *_args, **_kwargs: claim
    )

    result = _bind(manager, task_id, request_id)

    reason = "quality_review_claim_failed:" + claim["stderr"][:500]
    assert result == {"ok": False, "error": reason}
    assert terminalized == [reason]
    latest = manager._latest_by_request()[request_id]
    assert "claim_recovery_state" not in latest


@pytest.mark.parametrize("failure", ["returns_false", "raises"])
def test_reviewer_contention_that_cannot_be_retained_fails_closed(
    tmp_path, monkeypatch, failure
):
    task_id = f"NF804_REVIEWER_UNRETAINED_{failure.upper()}"
    request_id = f"nf804-reviewer-unretained-{failure.replace('_', '-')}"
    manager, _created, terminalized = _reviewer_manager(
        tmp_path, monkeypatch, task_id, request_id
    )
    claims = _contended_claim(monkeypatch)

    def unretained(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        if failure == "raises":
            raise OSError("registry unavailable")
        return False

    monkeypatch.setattr(
        manager, "_retain_ambiguous_reviewer_claim_for_reconciliation", unretained
    )

    result = _bind(manager, task_id, request_id)

    reason = "quality_review_claim_failed:" + claims[0]["stderr"][:500]
    # Neither retained nor left dangling: the attempt is terminalized.
    assert result == {"ok": False, "error": reason}
    assert terminalized == [reason]


def test_contention_after_ambiguous_mark_records_the_contention_reason(
    tmp_path, monkeypatch
):
    task_id = "NF804_REVIEWER_REMARK"
    request_id = "nf804-reviewer-remark"
    manager, _created, terminalized = _reviewer_manager(
        tmp_path, monkeypatch, task_id, request_id
    )
    assert manager._retain_ambiguous_reviewer_claim_for_reconciliation(
        request_id, task_id, RUNNER
    )
    assert (
        manager._latest_by_request()[request_id]["claim_recovery_state"]
        == "claim_commit_ambiguous"
    )
    _contended_claim(monkeypatch)

    result = _bind(manager, task_id, request_id)

    assert result["recovery_state"] == "starting_reservation_retained"
    assert terminalized == []
    latest = manager._latest_by_request()[request_id]
    assert latest["state"] == "starting"
    assert latest["claim_recovery_state"] == task_engine.CLAIM_CONTENTION_RECOVERY_STATE
    assert latest["claim_recovery_reason"] == CONTENTION_REASON


def test_reaper_closes_expired_contention_reservation_and_keeps_card_pending(
    tmp_path, monkeypatch
):
    """An expired contention-retained reservation never fails its reviewer.

    The card stayed pending/unclaimed, so ``_recover_ambiguous_reviewer_claims``
    has nothing to bind; once the lease expires the reaper must release the
    reservation (ledger only) and leave the card for the next seeding pass.
    """

    task_id = "NF804_REVIEWER_REAPED"
    request_id = "nf804-reviewer-reaped"
    manager, _created, terminalized = _reviewer_manager(
        tmp_path, monkeypatch, task_id, request_id
    )
    real_claim = task_engine.claim_start_exact
    _contended_claim(monkeypatch)
    assert _bind(manager, task_id, request_id)["claim_recovery_bound"] is True
    monkeypatch.setattr(task_engine, "claim_start_exact", real_claim)
    # Until its lease expires the deferred reservation still blocks duplicates.
    assert request_id in manager._active_request_ids()

    expired = dict(manager._latest_by_request()[request_id])
    expired["reservation_expires_at_epoch"] = time.time() - 1.0
    manager._append_event(expired)
    assert request_id not in manager._active_request_ids()
    monkeypatch.setattr(
        process_launcher.task_store,
        "enqueue_terminal_callback",
        lambda *_args, **_kwargs: True,
    )

    scan = _recovery.task_reconciler.run_scan(
        manager, repo=tmp_path, include_gc=False
    )
    second = _recovery.task_reconciler.run_scan(
        manager, repo=tmp_path, include_gc=False
    )

    assert scan["reservations_retired"] == 1
    assert second["reservations_retired"] == 0
    assert terminalized == []
    latest = manager._latest_by_request()[request_id]
    assert latest["state"] == "blocked"
    assert latest["blocked_reason"] == "reservation_expired"
    # Ledger-only release: no settlement intent could touch the card.
    assert "terminal_intent" not in latest
    assert not manager._reviewer_terminal_intent_path(request_id).exists()
    assert request_id not in manager._active_request_ids()
    _assert_pending_unclaimed(tmp_path, task_id)
    card = task_store.get_task(tmp_path, task_id)
    assert card is not None
    assert not card.get("terminal_retry")
    # The lens can be relaunched under a fresh request identity.
    relaunched = task_engine.claim_start_exact(
        tmp_path, task_id, RUNNER, TOPIC, request_id="nf804-reviewer-relaunch"
    )
    assert relaunched["ok"] is True


# --------------------------------------------------------------------------
# Caller 2: the validation-only replay claim


def _replay_manager(tmp_path, monkeypatch, task_id: str):
    _seeded(tmp_path, monkeypatch, task_id)
    manager = process_launcher.ProcessManager(repo=tmp_path)
    workspace = SimpleNamespace(
        repo=tmp_path, path=tmp_path / "replay-worktree", home=tmp_path / "replay-home"
    )
    cleaned: list[object] = []
    released: list[dict] = []
    monkeypatch.setattr(
        process_launcher, "create_workspace", lambda *_args, **_kwargs: workspace
    )
    monkeypatch.setattr(
        process_launcher,
        "cleanup_workspace",
        lambda *args, **_kwargs: cleaned.append(args),
    )
    monkeypatch.setattr(
        process_launcher,
        "build_residual_contract_manifest",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(process_launcher, "_path_manifest", lambda *_a, **_k: {})
    monkeypatch.setattr(
        manager,
        "_validation_replay_predecessor_mcp_receipt",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        task_engine,
        "mark_launch_failed",
        lambda *_args, **kwargs: released.append(kwargs) or {"ok": True},
    )
    return manager, cleaned, released


def _replay(manager, task_id: str, request_id: str) -> dict:
    return manager._launch_validation_only_replay(
        task_id=task_id,
        runner=RUNNER,
        topic=TOPIC,
        adapter_id="codex_cli",
        model=None,
        timeout_seconds=30,
        card={"task_id": task_id, "request_id": request_id},
        authorization={"task_id": task_id},
        request_id=request_id,
    )


def test_replay_contention_is_a_typed_deferral_that_keeps_the_card(
    tmp_path, monkeypatch
):
    task_id = "NF804_REPLAY_CONTENDED"
    request_id = "nf804-replay-contended"
    manager, cleaned, released = _replay_manager(tmp_path, monkeypatch, task_id)
    real_claim = task_engine.claim_start_exact
    claims = _contended_claim(monkeypatch)
    predicate_calls = _predicate_spy(monkeypatch)

    result = _replay(manager, task_id, request_id)

    assert len(claims) == 1
    assert predicate_calls == [request_id]
    assert result["ok"] is False
    assert result["outcome"] == "deferred"
    assert result["deferred"] is True
    assert result["retryable"] is True
    assert result["terminal"] is False
    assert result["provider_launched"] is False
    assert result["request_id"] == request_id
    assert result["task_queue_contention"] == claims[0]["task_queue_contention"]
    assert result["task_queue_contention"]["request_id"] == request_id
    # The deferral is durable: the persisted latest event carries it, not
    # only the in-memory reply.
    latest = manager._latest_by_request()[request_id]
    assert latest["state"] == "blocked"
    assert latest["outcome"] == "deferred"
    assert latest["deferred"] is True
    assert latest["retryable"] is True
    assert latest["terminal"] is False
    assert latest["provider_launched"] is False
    assert latest["task_queue_contention"] == claims[0]["task_queue_contention"]
    # Nothing was claimed, so nothing is released or terminalized; the card
    # (and the predecessor candidate it authorizes) stays pending.
    assert released == []
    _assert_pending_unclaimed(tmp_path, task_id)
    # The per-request seed is freed so the same identity can be re-seeded.
    assert len(cleaned) == 1

    monkeypatch.setattr(task_engine, "claim_start_exact", real_claim)
    retried = task_engine.claim_start_exact(
        tmp_path, task_id, RUNNER, TOPIC, request_id=request_id
    )
    assert retried["ok"] is True
    card = task_store.get_task(tmp_path, task_id)
    assert card is not None
    assert card["launch_request_id"] == request_id
    assert card["claim_epoch"] == 1


@pytest.mark.parametrize("mutation", FAIL_CLOSED_MUTATIONS)
def test_replay_untrusted_claim_failure_still_fails_closed(
    tmp_path, monkeypatch, mutation
):
    task_id = f"NF804_REPLAY_{mutation.upper()}"
    request_id = f"nf804-replay-{mutation.replace('_', '-')}"
    manager, _cleaned, released = _replay_manager(tmp_path, monkeypatch, task_id)
    claim = _mutated(_real_contention(tmp_path, task_id, request_id), mutation)
    monkeypatch.setattr(
        task_engine, "claim_start_exact", lambda *_args, **_kwargs: claim
    )

    result = _replay(manager, task_id, request_id)

    assert result["ok"] is False
    assert result["state"] == "blocked"
    assert result["blocked_reason"] == (
        "claim_start_failed:" + claim["stderr"][:300]
    )
    latest = manager._latest_by_request()[request_id]
    for key in ("outcome", "deferred", "retryable", "terminal", "task_queue_contention"):
        assert key not in result, key
        assert key not in latest, key
    assert released == []
