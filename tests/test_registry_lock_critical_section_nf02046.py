"""NF-2026-02046: the process-registry lock critical sections stay short.

``_reserve_quality_reviewer_attempt`` used to load task cards (unbound-claim
resolution and the lens-reuse rule) while holding ``_registry_lock``; one hold
was measured at 20.8 s and parallel reviewer launches waited out the 20 s
advisory-lock bound.  The store reads now run with the lock released, the
critical section only verifies the ledger rows they were taken over, and
contention is a retryable deferral.  The finalizer's former unnamed
``evidence_and_transition`` residual is split into named sub-phases.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

import pytest

from aiworkhub import process_launcher
from aiworkhub import reviewer_reservation_recovery as recovery

_TARGET_TASK = "TARGET_TASK_NF02046"
_TARGET_REQUEST = "target-request-nf02046"


def _manager(tmp_path, monkeypatch):
    manager = process_launcher.ProcessManager(repo=tmp_path)
    monkeypatch.setattr(
        process_launcher.review_orchestrator,
        "canonical_review_db",
        lambda _manager: None,
    )
    monkeypatch.setattr(process_launcher, "_configured_limit", lambda: 16)
    return manager


def _reserve(manager, *, reviewer_task_id: str, lens: str) -> dict[str, Any]:
    return manager._reserve_quality_reviewer_attempt(
        reviewer_task_id=reviewer_task_id,
        runner="codex",
        adapter_id="codex_cli",
        target_request_id=_TARGET_REQUEST,
        target_task_id=_TARGET_TASK,
        lens=lens,
        model=None,
        timeout_seconds=600,
    )


def _rows(manager, task_id: str) -> list[dict[str, Any]]:
    return [
        event for event in manager._latest_by_request().values()
        if event.get("task_id") == task_id
    ]


class _LockSpy:
    """Record registry-lock ownership and every hold duration."""

    def __init__(self, manager) -> None:
        self.held = False
        self.holds: list[float] = []
        self._orig = manager._registry_lock
        manager._registry_lock = self._lock

    @contextmanager
    def _lock(self):
        with self._orig():
            self.held = True
            started = time.monotonic()
            try:
                yield
            finally:
                self.holds.append(time.monotonic() - started)
                self.held = False


def test_interleaved_reservations_for_one_target_both_reach_starting(
    tmp_path, monkeypatch
):
    first = _manager(tmp_path, monkeypatch)
    second = process_launcher.ProcessManager(repo=tmp_path)
    receipts: dict[str, dict[str, Any]] = {}
    original_stable = first._latest_by_request_stable
    interleaved = {"done": False}

    def _stable_then_interleave():
        snapshot = original_stable()
        if not interleaved["done"]:
            # The second server reserves for the SAME target between the
            # first server's outside snapshot and its lock acquisition.
            interleaved["done"] = True
            receipts["second"] = _reserve(
                second, reviewer_task_id="REVIEWER_SECURITY", lens="security"
            )
        return snapshot

    first._latest_by_request_stable = _stable_then_interleave

    receipts["first"] = _reserve(
        first, reviewer_task_id="REVIEWER_CORRECTNESS", lens="correctness"
    )

    assert interleaved["done"] is True
    for name in ("first", "second"):
        receipt = receipts[name]
        assert receipt["ok"] is True, (name, receipt)
        assert receipt["state"] == "starting", (name, receipt)
    assert receipts["first"]["request_id"] != receipts["second"]["request_id"]
    for task_id in ("REVIEWER_CORRECTNESS", "REVIEWER_SECURITY"):
        rows = _rows(first, task_id)
        assert len(rows) == 1, (task_id, rows)
        assert rows[0]["state"] == "starting"


def test_slow_store_read_runs_outside_the_registry_lock(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    # A finished reviewer already sealed to this target and lens, so the reuse
    # rule has to load its card -- the read that used to run under the lock.
    manager._append_event({
        "request_id": "prior-reviewer-request",
        "task_id": "PRIOR_REVIEWER",
        "runner": "codex",
        "topic": "quality_review",
        "adapter_id": "codex_cli",
        "state": "completed",
        "quality_review_attempt": {
            "target_request_id": _TARGET_REQUEST,
            "target_task_id": _TARGET_TASK,
            "lens": "correctness",
        },
    })
    spy = _LockSpy(manager)
    store_reads: list[bool] = []

    def _slow_get_task(_repo, _task_id):
        store_reads.append(spy.held)
        time.sleep(1.2)
        return None

    monkeypatch.setattr(process_launcher.task_store, "get_task", _slow_get_task)

    receipt = _reserve(manager, reviewer_task_id="REVIEWER_NEW", lens="correctness")

    assert receipt["ok"] is True, receipt
    assert receipt["state"] == "starting"
    assert store_reads, "the reuse rule must have read the prior reviewer card"
    assert not any(store_reads), "a task-store read ran under the registry lock"
    assert spy.holds and max(spy.holds) < 1.0, spy.holds


def test_registry_lock_contention_is_a_retryable_deferral(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)

    @contextmanager
    def _contended():
        raise process_launcher.AdvisoryLockTimeout("contended", errno=None)
        yield  # pragma: no cover

    manager._registry_lock = _contended

    receipt = _reserve(manager, reviewer_task_id="REVIEWER_CONTENDED", lens="security")

    assert receipt["ok"] is False
    assert receipt["error"] == "ledger_snapshot_unproven"
    assert receipt["cause"] == "registry_lock_timeout"
    assert receipt["retryable"] is True
    assert receipt["deferred"] is True
    assert receipt["request_created"] is False
    assert "launch_failed" not in str(receipt)
    assert _rows(manager, "REVIEWER_CONTENDED") == []


def test_finalization_phases_name_the_former_residual():
    clock = process_launcher._FinalizationPhaseClock()
    # A 20 s wait inside the terminal transition is attributed to it.
    clock.add("terminal_transition", time.monotonic() - 20.0)
    assert clock.call("usage_record", lambda value: value, 7) == 7

    durations = process_launcher._finalization_phase_durations(
        total_ms=20_050.0, validation_ms=10.0, scope_ms=5.0, clock=clock
    )

    for phase in process_launcher.FINALIZATION_EVIDENCE_SUBPHASES:
        assert phase in durations
    assert durations["terminal_transition"] >= 20_000.0
    assert durations["evidence_and_transition"] < 1_000.0
    assert durations["workspace_scope"] == 5.0
    assert durations["validation"] == 10.0
    assert sum(durations.values()) <= 20_050.0 + 1.0
    with pytest.raises(KeyError):
        clock.add("not_a_phase", time.monotonic())


def _sealed_row(request_id: str, *, task_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "task_id": task_id,
        "runner": "codex",
        "topic": "quality_review",
        "adapter_id": "codex_cli",
        "state": "completed",
        "quality_review_attempt": {
            "target_request_id": _TARGET_REQUEST,
            "target_task_id": _TARGET_TASK,
            "lens": "correctness",
        },
        **extra,
    }


def _expired_unbound_row(request_id: str, *, task_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "task_id": task_id,
        "runner": "codex",
        "topic": "quality_review",
        "adapter_id": "codex_cli",
        "state": "starting",
        "reviewer_claim_epoch": None,
        "reservation_expires_at_epoch": time.time() - 600.0,
        **extra,
    }


def _hook_prefetch(monkeypatch, after_each) -> list[int]:
    """Run ``after_each(manager, round)`` between each lock-free prefetch and the lock."""

    rounds: list[int] = []
    original = recovery._prefetch_quality_reviewer_reuse

    def _wrapped(manager, latest, **kwargs):
        result = original(manager, latest, **kwargs)
        rounds.append(len(rounds) + 1)
        after_each(manager, rounds[-1])
        return result

    monkeypatch.setattr(recovery, "_prefetch_quality_reviewer_reuse", _wrapped)
    return rounds


def _stub_exact_resolution(manager, request_id: str, epoch: int = 7) -> None:
    manager._resolve_unbound_reviewer_claims = lambda latest: (
        {request_id: ("exact", epoch)}
        if request_id in latest and latest[request_id].get("reviewer_claim_epoch") is None
        else {}
    )


def _spy_appends(manager) -> list[dict[str, Any]]:
    appended: list[dict[str, Any]] = []
    original = manager._append_event

    def _append(event):
        appended.append(dict(event))
        return original(event)

    manager._append_event = _append
    return appended


@pytest.mark.parametrize("row_changed", [False, True])
def test_prefetched_claim_resolution_binds_only_its_exact_row(
    tmp_path, monkeypatch, row_changed
):
    manager = _manager(tmp_path, monkeypatch)
    # Unrelated to the reserved target/lens, so the reuse inputs never change
    # and exactly one round runs: only the fingerprint decides.
    manager._append_event(_expired_unbound_row("unbound-request", task_id="OTHER_REVIEWER"))
    _stub_exact_resolution(manager, "unbound-request")

    def _change_row(mgr, _round):
        if row_changed:
            mgr._append_event(_expired_unbound_row(
                "unbound-request", task_id="OTHER_REVIEWER", note="changed after prefetch"
            ))

    rounds = _hook_prefetch(monkeypatch, _change_row)
    appended = _spy_appends(manager)

    receipt = _reserve(manager, reviewer_task_id="REVIEWER_NEW", lens="correctness")

    assert receipt["ok"] is True, receipt
    assert rounds == [1]
    bound = [
        event for event in appended
        if event.get("request_id") == "unbound-request"
        and event.get("claim_binding_state") == "reviewer_claim_bound"
    ]
    if row_changed:
        assert bound == [], "a resolution computed over a stale row was applied"
    else:
        assert len(bound) == 1 and bound[0]["reviewer_claim_epoch"] == 7, appended


def test_ledger_append_between_prefetch_and_lock_reruns_exactly_once(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)

    def _append_same_lens(mgr, round_no):
        if round_no == 1:
            mgr._append_event(_sealed_row("racing-request", task_id="RACING_REVIEWER"))

    rounds = _hook_prefetch(monkeypatch, _append_same_lens)

    receipt = _reserve(manager, reviewer_task_id="REVIEWER_NEW", lens="correctness")

    assert rounds == [1, 2]
    assert receipt["ok"] is True, receipt
    assert receipt["state"] == "starting"
    assert len(_rows(manager, "REVIEWER_NEW")) == 1


def test_reconcile_binding_a_relevant_reviewer_reruns_the_reuse_reads(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    # A relevant (same target and lens) unbound reviewer that the critical
    # section's reconcile binds: the inputs are compared AFTER it, so the
    # prefetched reuse answer is discarded and re-read once.
    manager._append_event(_expired_unbound_row(
        "relevant-request", task_id="RELEVANT_REVIEWER",
        quality_review_attempt={
            "target_request_id": _TARGET_REQUEST,
            "target_task_id": _TARGET_TASK,
            "lens": "correctness",
        },
    ))
    _stub_exact_resolution(manager, "relevant-request")
    rounds = _hook_prefetch(monkeypatch, lambda _mgr, _round: None)

    receipt = _reserve(manager, reviewer_task_id="REVIEWER_NEW", lens="correctness")

    assert rounds == [1, 2]
    assert receipt["ok"] is True, receipt
    assert receipt["state"] == "starting"


def test_reuse_inputs_changing_every_round_is_a_retryable_deferral(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)

    def _append_every_round(mgr, round_no):
        mgr._append_event(_sealed_row(f"racing-{round_no}", task_id=f"RACING_{round_no}"))

    rounds = _hook_prefetch(monkeypatch, _append_every_round)

    receipt = _reserve(manager, reviewer_task_id="REVIEWER_NEW", lens="correctness")

    assert len(rounds) == manager._RESERVATION_RESNAPSHOT_ROUNDS
    assert receipt == recovery._registry_lock_contended_deferral(
        "lens_reuse_inputs_changed"
    )
    assert receipt["retryable"] is True and receipt["deferred"] is True
    assert "launch_failed" not in str(receipt)
    assert not [
        event for event in manager._latest_by_request().values()
        if event.get("state") == "starting"
    ]
    assert _rows(manager, "REVIEWER_NEW") == []
