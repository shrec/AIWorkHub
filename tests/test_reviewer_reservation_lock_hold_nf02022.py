"""NF-2026-02022: a lost reservation hand-off never re-folds the whole ledger
while the cross-process registry lock is held.

Every ``_proven_reservation_snapshot`` caller goes through
``ProcessManager._decide_on_proven_snapshot``: on a lost hand-off it leaves the
lock, re-snapshots with it released and re-enters, bounded, then fails closed
exactly as before.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from aiworkhub import process_launcher

_REVIEWER_TASK = "REVIEWER_NF02022"


def _manager(tmp_path, monkeypatch):
    manager = process_launcher.ProcessManager(repo=tmp_path)
    monkeypatch.setattr(
        process_launcher.review_orchestrator,
        "canonical_review_db",
        lambda _manager: None,
    )
    return manager


def _sibling(index: int) -> dict[str, Any]:
    return {
        "request_id": f"sibling-{index}",
        "task_id": f"SIBLING_{index}",
        "runner": "codex",
        "topic": "code",
        "adapter_id": "codex_cli",
        "state": "completed",
    }


class _Spy:
    """Track registry-lock ownership and every stable fold."""

    def __init__(self, manager, *, sibling_appends: int) -> None:
        self.manager = manager
        self.held = False
        self.stable_calls: list[bool] = []  # held-at-call per fold
        self.sibling_appends = sibling_appends
        self._orig_lock = manager._registry_lock
        self._orig_stable = manager._latest_by_request_stable
        manager._registry_lock = self._lock
        manager._latest_by_request_stable = self._stable

    @contextmanager
    def _lock(self):
        with self._orig_lock():
            self.held = True
            try:
                yield
            finally:
                self.held = False

    def _stable(self):
        self.stable_calls.append(self.held)
        snapshot = self._orig_stable()
        if self.sibling_appends > 0:
            # A sibling appends between this snapshot and lock acquisition:
            # the hand-off this caller is about to make is lost.
            self.sibling_appends -= 1
            self.manager._append_event(_sibling(len(self.stable_calls)))
        return snapshot


def _reserve(manager) -> dict[str, Any]:
    return manager._reserve_quality_reviewer_attempt(
        reviewer_task_id=_REVIEWER_TASK,
        runner="codex",
        adapter_id="codex_cli",
        target_request_id="target-request",
        target_task_id="TARGET_TASK",
        lens="correctness",
        model=None,
        timeout_seconds=600,
    )


def _reviewer_rows(manager) -> list[dict[str, Any]]:
    return [
        event for event in manager._latest_by_request().values()
        if event.get("task_id") == _REVIEWER_TASK
    ]


def test_lost_hand_off_re_snapshots_outside_the_lock_and_reserves_once(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    manager._append_event(_sibling(0))
    spy = _Spy(manager, sibling_appends=1)

    receipt = _reserve(manager)

    assert receipt["ok"] is True, receipt
    assert receipt["state"] == "starting"
    # One outside snapshot, one re-snapshot after the lost hand-off; NEITHER
    # ran while the registry lock was held.
    assert spy.stable_calls == [False, False]
    rows = _reviewer_rows(manager)
    assert len(rows) == 1
    assert rows[0]["request_id"] == receipt["request_id"]
    assert rows[0]["state"] == "starting"


def test_in_lock_decision_uses_generation_proven_under_the_lock(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    manager._append_event(_sibling(0))
    spy = _Spy(manager, sibling_appends=0)
    seen: list[tuple[Any, Any]] = []
    reconcile = manager._reconcile_expired_starting_reservations

    def _spy_reconcile(proven, *args, **kwargs):
        assert spy.held is True
        seen.append((proven[1], manager._ledger_generation()))
        return reconcile(proven, *args, **kwargs)

    monkeypatch.setattr(
        manager, "_reconcile_expired_starting_reservations", _spy_reconcile
    )

    receipt = _reserve(manager)

    # Unchanged ledger: today's cheap path, one outside fold, same receipt.
    assert receipt["ok"] is True and receipt["already_reserved"] is False
    assert spy.stable_calls == [False]
    assert len(seen) == 1
    proven_generation, generation_under_lock = seen[0]
    assert proven_generation is not None
    assert proven_generation == generation_under_lock
    assert len(_reviewer_rows(manager)) == 1


def test_churning_ledger_fails_closed_after_bounded_rounds(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    manager._append_event(_sibling(0))
    spy = _Spy(manager, sibling_appends=10_000)

    receipt = _reserve(manager)

    assert receipt == {"ok": False, "error": "ledger_snapshot_unproven"}
    rounds = process_launcher.ProcessManager._RESERVATION_RESNAPSHOT_ROUNDS
    assert spy.stable_calls == [False] * rounds
    assert _reviewer_rows(manager) == []


def test_other_reservation_callers_share_the_out_of_lock_helper(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    request_id = "bind-me"
    manager._append_event({
        "request_id": request_id,
        "task_id": _REVIEWER_TASK,
        "runner": "codex",
        "topic": "quality_review",
        "adapter_id": "codex_cli",
        "state": "starting",
        "reservation_expires_at_epoch": 4_000_000_000.0,
    })
    helper_calls: list[str] = []
    helper = manager._decide_on_proven_snapshot

    def _spy_helper(snapshot, decide, **kwargs):
        helper_calls.append(decide.__qualname__)
        return helper(snapshot, decide, **kwargs)

    monkeypatch.setattr(manager, "_decide_on_proven_snapshot", _spy_helper)
    spy = _Spy(manager, sibling_appends=1)

    assert manager._bind_reviewer_claim_epoch(request_id, 7) is True

    assert spy.stable_calls == [False, False]
    assert helper_calls == [
        "ProcessManager._bind_reviewer_claim_epoch.<locals>._decide"
    ]
    bound = manager._latest_by_request()[request_id]
    # The ledger projects a bound claim back onto ``starting``; the bound
    # epoch is the durable proof the decision ran.
    assert bound["reviewer_claim_epoch"] == 7

    # A churning ledger fails closed for this caller too, still with no fold
    # under the lock.
    spy.stable_calls.clear()
    spy.sibling_appends = 10_000
    assert manager._reviewer_attach_provider_identity(
        request_id, pid=4242, pid_start_ticks=1
    ) is False
    assert spy.stable_calls and not any(spy.stable_calls)
    assert helper_calls[-1] == (
        "ProcessManager._reviewer_attach_provider_identity.<locals>._decide"
    )
