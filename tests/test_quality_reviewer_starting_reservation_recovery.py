from __future__ import annotations

import json
import os
import threading
import time

import pytest

from aiworkhub import process_launcher, task_engine, task_reconciler, task_store


def _manager_for_periodic_scan(tmp_path, monkeypatch):
    manager = process_launcher.ProcessManager(repo=tmp_path)
    monkeypatch.setattr(manager, "_reconcile_persisted_requests", lambda: {"watched": 0, "finalized": 0})
    monkeypatch.setattr(
        manager,
        "_gc_finalized_workspaces",
        lambda: {"gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0},
    )
    monkeypatch.setattr(
        process_launcher.review_orchestrator,
        "canonical_review_db",
        lambda _manager: None,
    )
    return manager


def _starting(
    request_id: str,
    *,
    deadline: float,
    pid: int = 0,
    task_id: str | None = None,
    claim_epoch: int = 1,
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "task_id": task_id or f"reviewer-{request_id}",
        "runner": "codex",
        "topic": "quality_review",
        "adapter_id": "codex_cli",
        "state": "starting",
        "pid": pid,
        "reviewer_claim_epoch": claim_epoch,
        "reservation_expires_at_epoch": deadline,
    }


def _seed_pending_reviewer(tmp_path, task_id: str) -> None:
    task_store.initialize_repository(tmp_path)
    with pytest.MonkeyPatch.context() as seed_patch:
        seed_patch.setattr(task_engine.core, "repo_root", lambda: tmp_path)
        seed_patch.setattr(
            task_engine.core,
            "_claude_manager_identity",
            lambda: {"thread_id": "test", "provider": "claude"},
        )
        seed_patch.setattr(
            task_engine.core, "_canonical_write_gate", lambda *_args: None
        )
        seed_patch.setattr(
            task_engine.core,
            "_verify_coordinator_capability",
            lambda *_args: (True, ""),
        )
        seed_patch.setattr(task_engine.core, "CODEX_RUNNER", "test-manager")
        created = task_engine.core.create_task(
            task_id=task_id,
            title="Reviewer recovery fixture",
            runner="codex",
            topic="quality_review",
            objective="Exercise reviewer reservation recovery.",
            acceptance=["reservation recovery is deterministic"],
            allowed_writes=[],
            callback_required=False,
            read_only=True,
        )
    assert created["ok"] is True


def _assert_nonregular_intent_is_contained(tmp_path, monkeypatch, intent_path) -> None:
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    store_calls: list[str] = []
    callback_calls: list[str] = []
    monkeypatch.setattr(
        process_launcher.task_store,
        "mark_terminal_failure",
        lambda *_args, **_kwargs: store_calls.append("store"),
    )
    monkeypatch.setattr(
        process_launcher.task_store,
        "enqueue_terminal_callback",
        lambda *_args, **_kwargs: callback_calls.append("callback"),
    )

    started = time.monotonic()
    assert manager._settle_reviewer_terminal_intents() == 0
    assert time.monotonic() - started < 1.0

    audit = process_launcher.reviewer_terminal_intent_diagnostic_path(
        manager.process_log_path
    )
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["intent_file"] == intent_path.name
    assert records[0]["reason"] == "path_identity_mismatch"
    assert records[0]["sha256"] == ""
    assert -1 <= records[0]["bytes"] < 4096
    assert store_calls == []
    assert callback_calls == []


def test_symlink_terminal_intent_diagnostic_never_follows_subject(tmp_path, monkeypatch):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    target = tmp_path / "outside-intent-target"
    target.write_text("sensitive target bytes", encoding="utf-8")
    intent = manager.process_dir / (
        "symlink" + manager._REVIEWER_TERMINAL_INTENT_SUFFIX
    )
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.symlink_to(target)

    _assert_nonregular_intent_is_contained(tmp_path, monkeypatch, intent)


def test_fifo_terminal_intent_diagnostic_never_blocks_on_subject(tmp_path, monkeypatch):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    intent = manager.process_dir / ("fifo" + manager._REVIEWER_TERMINAL_INTENT_SUFFIX)
    intent.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(intent)

    _assert_nonregular_intent_is_contained(tmp_path, monkeypatch, intent)


def test_periodic_scan_retires_expired_pid_null_reservation_once(tmp_path, monkeypatch):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    manager._append_event(_starting("lost-ack", deadline=time.time() - 1.0))
    settled: list[str] = []
    monkeypatch.setattr(
        manager,
        "_settle_reviewer_terminal_intents_contained",
        lambda: settled.append("settled") or 1,
    )

    first = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    second = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)

    latest = manager._latest_by_request()["lost-ack"]
    assert first["reservations_retired"] == 1
    assert second["reservations_retired"] == 0
    assert latest["state"] == "blocked"
    assert latest["blocked_reason"] == "reservation_expired"
    assert latest["terminal_intent"] == "recorded"
    assert settled == ["settled", "settled"]


def test_periodic_scan_contains_malformed_pid_and_retires_later_expired(
    tmp_path, monkeypatch
):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    malformed = _starting("malformed-pid", deadline=time.time() - 1.0)
    malformed["pid"] = "not-a-pid"
    manager._append_event(malformed)
    manager._append_event(_starting("later-expired", deadline=time.time() - 1.0))
    monkeypatch.setattr(
        manager,
        "_settle_reviewer_terminal_intents_contained",
        lambda: 1,
    )

    result = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)

    latest = manager._latest_by_request()
    assert result["reservations_retired"] == 1
    assert latest["malformed-pid"]["state"] == "starting"
    assert latest["later-expired"]["state"] == "blocked"
    assert latest["later-expired"]["blocked_reason"] == "reservation_expired"


def test_periodic_scan_legacy_reservation_uses_ledger_only_retirement(
    tmp_path, monkeypatch
):
    task_id = "REVIEWER_PRECLAIM_LEGACY"
    _seed_pending_reviewer(tmp_path, task_id)
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    legacy = _starting("legacy", task_id=task_id, deadline=time.time() - 1.0)
    legacy.pop("reviewer_claim_epoch")
    manager._append_event(legacy)
    settled: list[str] = []
    monkeypatch.setattr(
        manager,
        "_settle_reviewer_terminal_intents_contained",
        lambda: settled.append("settled") or 0,
    )

    first = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    second = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)

    latest = manager._latest_by_request()["legacy"]
    assert first["reservations_retired"] == 1
    assert second["reservations_retired"] == 0
    assert latest["state"] == "blocked"
    assert latest["blocked_reason"] == "reservation_expired"
    assert "terminal_intent" not in latest
    assert settled == ["settled", "settled"]


def test_periodic_scan_recovers_crash_immediately_after_claim_commit(
    tmp_path, monkeypatch
):
    task_id = "REVIEWER_IMMEDIATE_POST_CLAIM_CRASH"
    request_id = "immediate-post-claim-crash"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    reservation = _starting(
        request_id, task_id=task_id, deadline=time.time() - 1.0
    )
    reservation.pop("reviewer_claim_epoch")
    manager._append_event(reservation)

    claimed = task_engine.claim_start_exact(
        tmp_path, task_id, "codex", "quality_review", request_id
    )
    assert claimed["ok"] is True
    claimed_card = task_store.get_task(tmp_path, task_id)
    assert claimed_card is not None

    first = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    after_first = task_store.get_task(tmp_path, task_id)
    events_after_first = manager._request_events(request_id)
    second = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)

    assert first["reservations_retired"] == 1
    assert first["terminal_intents_settled"] == 1
    assert second["reservations_retired"] == 0
    assert second["terminal_intents_settled"] == 0
    assert after_first is not None
    assert after_first["status"] == "pending"
    assert after_first["worker_status"] == "unclaimed"
    assert any(
        event.get("reviewer_claim_epoch") == claimed_card["claim_epoch"]
        for event in events_after_first
    )
    assert sum(event.get("state") == "blocked" for event in events_after_first) == 1


def test_existing_terminal_intent_symlink_swap_fails_closed(tmp_path, monkeypatch):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    event = _starting("intent-symlink-swap", deadline=time.time() - 1.0)
    target = tmp_path / "foreign-intent"
    target.write_text("{}", encoding="utf-8")
    intent = manager._reviewer_terminal_intent_path("intent-symlink-swap")
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.symlink_to(target)

    assert (
        manager._record_reviewer_terminal_intent(
            "intent-symlink-swap", event, "reservation_expired"
        )
        == "record_failed"
    )
    assert target.read_text(encoding="utf-8") == "{}"


def test_periodic_scan_preserves_unexpired_and_ambiguous_identity(tmp_path, monkeypatch):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    manager._append_event(_starting("new", deadline=time.time() + 60.0))
    manager._append_event(_starting("ambiguous", deadline=time.time() - 1.0, pid=999_999_999))
    ambiguous = process_launcher._pid_identity_evidence(os.getpid(), None)
    assert ambiguous.verdict is process_launcher.PidIdentityVerdict.UNKNOWN
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: ambiguous,
    )

    result = manager.reconcile(include_gc=False)

    latest = manager._latest_by_request()
    assert result["reservations_retired"] == 0
    assert latest["new"]["state"] == "starting"
    assert latest["ambiguous"]["state"] == "starting"


def test_periodic_scan_preserves_future_pid_mismatch_task_store_claim(
    tmp_path, monkeypatch
):
    task_id = "REVIEWER_FUTURE_PID_MISMATCH"
    request_id = "future-pid-mismatch"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )
    claimed = task_engine.claim_start_exact(
        tmp_path, task_id, "codex", "quality_review", request_id
    )
    assert claimed["ok"] is True
    claimed_card = task_store.get_task(tmp_path, task_id)
    assert claimed_card is not None

    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    manager._append_event(
        _starting(
            request_id,
            task_id=task_id,
            claim_epoch=int(claimed_card["claim_epoch"]),
            deadline=time.time() + 60.0,
            pid=999_999_999,
        )
    )
    mismatch = type(
        "MismatchEvidence",
        (),
        {"verdict": process_launcher.PidIdentityVerdict.MISMATCH},
    )()
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: mismatch,
    )

    result = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)

    assert result["reservations_retired"] == 0
    assert result["terminal_intents_settled"] == 0
    latest = manager._latest_by_request()[request_id]
    assert latest["state"] == "starting"
    recovered = task_store.get_task(tmp_path, task_id)
    assert recovered is not None
    assert recovered["status"] == "processing"
    assert recovered["worker_status"] == "claimed"


def test_periodic_scan_settles_expired_pid_mismatch_task_store_claim_once(
    tmp_path, monkeypatch
):
    task_id = "REVIEWER_EXPIRED_PID_MISMATCH"
    request_id = "expired-pid-mismatch"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )
    claimed = task_engine.claim_start_exact(
        tmp_path, task_id, "codex", "quality_review", request_id
    )
    assert claimed["ok"] is True
    claimed_card = task_store.get_task(tmp_path, task_id)
    assert claimed_card is not None
    claim_epoch = int(claimed_card["claim_epoch"])

    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    manager._append_event(
        _starting(
            request_id,
            task_id=task_id,
            claim_epoch=claim_epoch,
            deadline=time.time() - 1.0,
            pid=999_999_999,
        )
    )
    mismatch = type(
        "MismatchEvidence",
        (),
        {"verdict": process_launcher.PidIdentityVerdict.MISMATCH},
    )()
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: mismatch,
    )
    callbacks: list[tuple[str, int]] = []
    monkeypatch.setattr(
        process_launcher.task_store,
        "enqueue_terminal_callback",
        lambda _repo, _task_id, *, claim_epoch, **_kwargs: (
            callbacks.append((_task_id, int(claim_epoch))) or True
        ),
    )

    first = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    after_first = task_store.get_task(tmp_path, task_id)
    second = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)

    assert first["reservations_retired"] == 1
    assert first["terminal_intents_settled"] == 1
    assert second["reservations_retired"] == 0
    assert second["terminal_intents_settled"] == 0
    latest = manager._latest_by_request()[request_id]
    assert latest["state"] == "blocked"
    assert latest["blocked_reason"] == "reservation_process_false"
    assert latest["terminal_intent"] == "recorded"
    assert after_first is not None
    assert after_first["status"] == "pending"
    assert after_first["worker_status"] == "unclaimed"
    assert task_store.get_task(tmp_path, task_id) == after_first
    assert callbacks == [(task_id, claim_epoch)]


def test_periodic_scan_requires_valid_expired_lease_before_pid_null_retirement(
    tmp_path, monkeypatch
):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    future_stalled = _starting("future-stalled", deadline=time.time() + 60.0)
    future_stalled.update(
        preparation_phase="source_graph",
        preparation_heartbeat_epoch=1.0,
    )
    manager._append_event(future_stalled)

    invalid_deadlines: dict[str, object] = {
        "zero": 0.0,
        "malformed": "not-a-deadline",
        "nan": float("nan"),
        "positive-infinity": float("inf"),
        "negative-infinity": float("-inf"),
    }
    missing = _starting("missing", deadline=1.0)
    missing.pop("reservation_expires_at_epoch")
    manager._append_event(missing)
    for request_id, deadline in invalid_deadlines.items():
        manager._append_event(_starting(request_id, deadline=deadline))

    # Proves one malformed reservation is contained and does not abort the pass:
    # a later reservation with valid expired authority is still retired.
    manager._append_event(_starting("valid-expired", deadline=time.time() - 1.0))

    result = manager.reconcile(include_gc=False)

    latest = manager._latest_by_request()
    assert result["reservations_retired"] == 1
    preserved = ["future-stalled", "missing", *invalid_deadlines]
    assert all(latest[request_id]["state"] == "starting" for request_id in preserved)
    assert latest["valid-expired"]["state"] == "blocked"
    assert latest["valid-expired"]["blocked_reason"] == "reservation_expired"


def test_periodic_scan_releases_exact_real_task_store_claim_once(tmp_path, monkeypatch):
    task_id = "REVIEWER_STARTING_EXPIRED"
    request_id = "release"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None)
    claimed = task_engine.claim_start_exact(
        tmp_path, task_id, "codex", "quality_review", request_id
    )
    assert claimed["ok"] is True
    claimed_card = task_store.get_task(tmp_path, task_id)
    assert claimed_card is not None
    claim_epoch = int(claimed_card["claim_epoch"])

    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    expired_stalled = _starting(
            request_id,
            task_id=task_id,
            claim_epoch=claim_epoch,
            deadline=time.time() - 1.0,
        )
    expired_stalled.update(
        preparation_phase="source_graph",
        preparation_heartbeat_epoch=1.0,
    )
    manager._append_event(expired_stalled)
    callbacks: list[tuple[str, int]] = []
    monkeypatch.setattr(
        process_launcher.task_store,
        "enqueue_terminal_callback",
        lambda _repo, _task_id, *, claim_epoch, **_kwargs: (
            callbacks.append((_task_id, int(claim_epoch))) or True
        ),
    )

    first = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    after_first = task_store.get_task(tmp_path, task_id)

    assert first["reservations_retired"] == 1
    assert first["terminal_intents_settled"] == 1
    latest = manager._latest_by_request()[request_id]
    assert latest["terminal_intent"] == "recorded"
    assert latest["blocked_reason"].startswith(
        "preparation_heartbeat_stalled:source_graph:"
    )
    assert after_first is not None
    assert after_first["status"] == "pending"
    assert after_first["worker_status"] == "unclaimed"
    assert after_first["claimed_by"] in (None, "")

    reclaimed = task_engine.claim_start_exact(
        tmp_path, task_id, "codex", "quality_review", request_id + "-retry"
    )
    assert reclaimed["ok"] is True
    before_second = task_store.get_task(tmp_path, task_id)
    second = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    after_second = task_store.get_task(tmp_path, task_id)

    assert second["reservations_retired"] == 0
    assert second["terminal_intents_settled"] == 0
    assert before_second is not None
    assert before_second["status"] == "processing"
    assert before_second["worker_status"] == "claimed"
    assert after_second == before_second
    assert callbacks == [(task_id, claim_epoch)]


def test_direct_reconciliation_cannot_enable_unexpired_admission_recovery(
    tmp_path, monkeypatch
):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    event = _starting("generic", deadline=time.time() + 60.0)
    event.update(
        preparation_phase="source_graph",
        preparation_heartbeat_epoch=1.0,
    )
    manager._append_event(event)

    result = manager._reconcile_expired_starting_reservations(
        _admission_recovery_authority=True
    )

    assert result == 0
    assert manager._latest_by_request()["generic"]["state"] == "starting"


@pytest.mark.parametrize("identity_field", ["pid", "provider_pid", "owner_pid"])
@pytest.mark.parametrize(
    "identity_value", ["not-a-pid", -1, float("nan"), float("inf")]
)
def test_active_count_preserves_ambiguous_durable_pid_authority(
    tmp_path, monkeypatch, identity_field, identity_value
):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    request_id = f"ambiguous-{identity_field}"
    event = _starting(request_id, deadline=time.time() - 1.0)
    if identity_field in {"provider_pid", "owner_pid"}:
        event.update(state="provider_spawn_committed", provider_pid=0, owner_pid=0)
    event[identity_field] = identity_value

    assert manager._active_request_ids({request_id: event}) == {request_id}
    assert manager._active_count({request_id: event}) == 1


def test_admission_recovery_preserves_unexpired_stalled_preparation(
    tmp_path, monkeypatch
):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    request_id = "unexpired-stalled-admission"
    event = _starting(request_id, deadline=time.time() + 60.0)
    event.update(
        preparation_phase="source_graph",
        preparation_heartbeat_epoch=1.0,
    )
    manager._append_event(event)
    snapshot = manager._latest_by_request_stable()

    with manager._lock, manager._registry_lock():
        proven = manager._resolved_reservation_snapshot(snapshot)
        assert proven is not None
        retired = manager._reconcile_expired_starting_reservations(
            proven,
            resolved=True,
            _admission_recovery_authority=(
                process_launcher._LAUNCH_RESERVATION_ADMISSION_RECOVERY
            ),
        )

    assert retired == 0
    assert manager._latest_by_request()[request_id]["state"] == "starting"


def test_periodic_scan_preserves_spawn_commit_appended_after_snapshot(
    tmp_path, monkeypatch
):
    task_id = "REVIEWER_SPAWN_COMMIT_RACE"
    request_id = "spawn-commit-race"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )
    claimed = task_engine.claim_start_exact(
        tmp_path, task_id, "codex", "quality_review", request_id
    )
    assert claimed["ok"] is True
    claimed_card = task_store.get_task(tmp_path, task_id)
    assert claimed_card is not None
    claim_epoch = int(claimed_card["claim_epoch"])

    reconciler = _manager_for_periodic_scan(tmp_path, monkeypatch)
    launcher = process_launcher.ProcessManager(repo=tmp_path)
    reconciler._append_event(
        _starting(
            request_id,
            task_id=task_id,
            claim_epoch=claim_epoch,
            deadline=time.time() - 1.0,
        )
    )
    snapshot_taken = threading.Event()
    allow_reconcile = threading.Event()
    original_snapshot = reconciler._latest_by_request_stable

    def paused_snapshot():
        snapshot = original_snapshot()
        snapshot_taken.set()
        assert allow_reconcile.wait(timeout=5.0)
        return snapshot

    monkeypatch.setattr(reconciler, "_latest_by_request_stable", paused_snapshot)
    callbacks: list[tuple[str, int]] = []
    monkeypatch.setattr(
        process_launcher.task_store,
        "enqueue_terminal_callback",
        lambda _repo, _task_id, *, claim_epoch, **_kwargs: (
            callbacks.append((_task_id, int(claim_epoch))) or True
        ),
    )
    result: dict[str, object] = {}

    def scan() -> None:
        result.update(
            task_reconciler.run_scan(reconciler, repo=tmp_path, include_gc=False)
        )

    thread = threading.Thread(target=scan)
    thread.start()
    assert snapshot_taken.wait(timeout=5.0)
    assert launcher._reviewer_spawn_transition(
        request_id, reviewer_claim_epoch=claim_epoch
    )
    allow_reconcile.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    events = reconciler._request_events(request_id)
    assert result["reservations_retired"] == 0
    assert events[-1]["state"] == "provider_spawn_committed"
    assert not any(event.get("state") == "blocked" for event in events)
    assert not reconciler._reviewer_terminal_intent_path(request_id).exists()
    assert callbacks == []
    assert task_store.get_task(tmp_path, task_id) == claimed_card


def test_post_claim_pre_spawn_gap_persists_exact_epoch_and_recovers(
    tmp_path, monkeypatch
):
    task_id = "REVIEWER_POST_CLAIM_GAP"
    request_id = "post-claim-gap"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    reservation = _starting(
        request_id,
        task_id=task_id,
        deadline=time.time() - 1.0,
    )
    reservation.pop("reviewer_claim_epoch")
    manager._append_event(reservation)
    claim = task_engine.claim_start_exact(
        tmp_path, task_id, "codex", "quality_review", request_id
    )
    claimed_card = task_store.get_task(tmp_path, task_id)
    assert claim["ok"] is True
    assert claimed_card is not None
    claim_epoch = int(claimed_card["claim_epoch"])

    # This binding is the durable boundary immediately following claim commit;
    # the simulated launcher dies before verification, preparation, or spawn.
    assert manager._bind_reviewer_claim_epoch(request_id, claim_epoch)
    bound = manager._latest_by_request()[request_id]
    assert bound["reviewer_claim_epoch"] == claim_epoch
    assert bound["state"] == "starting"
    assert bound["claim_binding_state"] == "reviewer_claim_bound"
    events_after_binding = manager._request_events(request_id)
    assert sum(event.get("state") == "starting" for event in events_after_binding) == 1
    assert events_after_binding[-1]["state"] == "reviewer_claim_bound"

    # A lost acknowledgement can replay the exact binding without adding a
    # second durable row or manufacturing another reservation event.
    assert manager._bind_reviewer_claim_epoch(request_id, claim_epoch)
    assert manager._request_events(request_id) == events_after_binding

    scan = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    recovered = task_store.get_task(tmp_path, task_id)
    assert scan["reservations_retired"] == 1
    assert scan["terminal_intents_settled"] == 1
    assert recovered is not None
    assert recovered["status"] == "pending"
    assert recovered["worker_status"] == "unclaimed"
    retry = task_engine.claim_start_exact(
        tmp_path, task_id, "codex", "quality_review", request_id + "-retry"
    )
    assert retry["ok"] is True


@pytest.mark.parametrize(
    ("verdict", "retired"),
    [
        (process_launcher.PidIdentityVerdict.MATCH, 0),
        (process_launcher.PidIdentityVerdict.UNKNOWN, 0),
        (process_launcher.PidIdentityVerdict.MISMATCH, 1),
    ],
)
def test_non_prewarm_pid_null_preparation_authenticates_owner(
    tmp_path, monkeypatch, verdict, retired
):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    event = _starting("generic-owner", deadline=time.time() - 1.0)
    event.update(
        preparation_phase="workspace_materialization",
        preparation_heartbeat_epoch=time.time(),
        owner_pid=os.getpid(),
        owner_pid_start_ticks=123,
    )
    manager._append_event(event)
    evidence = type("OwnerEvidence", (), {"verdict": verdict})()
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: evidence,
    )

    result = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)

    assert result["reservations_retired"] == retired
    expected_state = "blocked" if retired else "starting"
    assert manager._latest_by_request()["generic-owner"]["state"] == expected_state


def test_binding_and_canonical_release_failure_retains_reapable_reservation(
    tmp_path, monkeypatch
):
    task_id = "REVIEWER_BIND_AND_RELEASE_FAILURE"
    request_id = "bind-and-release-failure"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    starting = _starting(
        request_id,
        task_id=task_id,
        deadline=time.time() - 1.0,
    )
    starting.pop("reviewer_claim_epoch")
    manager._append_event(starting)
    monkeypatch.setattr(
        process_launcher.core,
        "create_task",
        lambda **_kwargs: {"ok": True, "receipt_state": "existing_identical"},
    )
    monkeypatch.setattr(
        manager,
        "_show_task",
        lambda _task_id: {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    **(task_store.get_task(tmp_path, task_id) or {}),
                    "read_only": True,
                    "allowed_writes": [],
                }
            ),
            "stderr": "",
        },
    )
    monkeypatch.setattr(manager, "_bind_reviewer_claim_epoch", lambda *_args: False)
    canonical_mark_launch_failed = task_engine.mark_launch_failed
    monkeypatch.setattr(
        task_engine,
        "mark_launch_failed",
        lambda *_args, **_kwargs: {"ok": False, "error": "store_locked"},
    )

    failed = manager._ensure_quality_reviewer_card_bound(
        request_id=request_id,
        target_request_id="target-request",
        target_task_id="target-task",
        reviewer_task_id=task_id,
        runner="codex",
        adapter_id="codex_cli",
        lens="correctness",
        target_card={"adapter_id": "claude_cli"},
        terminalize_on_failure=True,
    )

    claimed = task_store.get_task(tmp_path, task_id)
    latest = manager._latest_by_request()[request_id]
    assert failed["ok"] is False
    assert failed["recovery_state"] == "starting_reservation_retained"
    assert failed["release_error"] == "store_locked"
    assert claimed is not None
    assert claimed["status"] == "processing"
    assert latest["state"] == "starting"
    claim_epoch = int(claimed["claim_epoch"])
    assert failed["claim_recovery_bound"] is True
    assert latest["reviewer_claim_epoch"] == claim_epoch
    assert latest["claim_binding_state"] == "reviewer_claim_bound"
    assert latest["claim_recovery_reason"] == (
        "quality_review_claim_binding_and_release_failed"
    )

    callbacks: list[tuple[str, int]] = []
    monkeypatch.setattr(
        process_launcher.task_store,
        "enqueue_terminal_callback",
        lambda _repo, _task_id, *, claim_epoch, **_kwargs: (
            callbacks.append((_task_id, int(claim_epoch))) or True
        ),
    )

    monkeypatch.setattr(task_engine, "mark_launch_failed", canonical_mark_launch_failed)
    scan = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    recovered = task_store.get_task(tmp_path, task_id)

    assert scan["reservations_retired"] == 1
    assert scan["terminal_intents_settled"] == 1
    assert recovered is not None
    assert recovered["status"] == "pending"
    assert recovered["worker_status"] == "unclaimed"
    assert callbacks == [(task_id, claim_epoch)]


def test_committed_claim_with_malformed_receipt_recovers_canonical_epoch(
    tmp_path, monkeypatch
):
    task_id = "REVIEWER_MALFORMED_COMMITTED_RECEIPT"
    request_id = "malformed-committed-receipt"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    starting = _starting(request_id, task_id=task_id, deadline=time.time() + 60.0)
    starting.pop("reviewer_claim_epoch")
    manager._append_event(starting)
    monkeypatch.setattr(
        process_launcher.core,
        "create_task",
        lambda **_kwargs: {"ok": True, "receipt_state": "existing_identical"},
    )
    monkeypatch.setattr(
        manager,
        "_show_task",
        lambda _task_id: {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    **(task_store.get_task(tmp_path, task_id) or {}),
                    "read_only": True,
                    "allowed_writes": [],
                }
            ),
            "stderr": "",
        },
    )
    canonical_claim = task_engine.claim_start_exact

    def committed_with_malformed_receipt(*args, **kwargs):
        result = canonical_claim(*args, **kwargs)
        assert result["ok"] is True
        return {**result, "stdout": "{malformed"}

    monkeypatch.setattr(
        task_engine, "claim_start_exact", committed_with_malformed_receipt
    )

    result = manager._ensure_quality_reviewer_card_bound(
        request_id=request_id,
        target_request_id="target-request",
        target_task_id="target-task",
        reviewer_task_id=task_id,
        runner="codex",
        adapter_id="codex_cli",
        lens="correctness",
        target_card={"adapter_id": "claude_cli"},
        terminalize_on_failure=True,
    )

    claimed = task_store.get_task(tmp_path, task_id)
    latest = manager._latest_by_request()[request_id]
    assert result["ok"] is True
    assert claimed is not None
    assert claimed["status"] == "processing"
    assert latest["state"] == "starting"
    assert latest["reviewer_claim_epoch"] == claimed["claim_epoch"]
    assert not any(
        event.get("request_id") == request_id and event.get("state") == "blocked"
        for event in manager._request_events(request_id)
    )


def test_double_transient_claim_result_is_recovered_by_one_periodic_scan(
    tmp_path, monkeypatch
):
    task_id = "REVIEWER_DOUBLE_TRANSIENT"
    request_id = "double-transient"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    starting = _starting(request_id, task_id=task_id, deadline=time.time() - 1.0)
    starting.pop("reviewer_claim_epoch")
    manager._append_event(starting)
    monkeypatch.setattr(
        process_launcher.core,
        "create_task",
        lambda **_kwargs: {"ok": True, "receipt_state": "existing_identical"},
    )
    canonical_claim = task_engine.claim_start_exact

    def malformed_committed_claim(*args, **kwargs):
        result = canonical_claim(*args, **kwargs)
        assert result["ok"] is True
        return {**result, "stdout": "{malformed"}

    monkeypatch.setattr(task_engine, "claim_start_exact", malformed_committed_claim)
    show_calls = 0

    def transient_second_show(_task_id):
        nonlocal show_calls
        show_calls += 1
        if show_calls > 1:
            raise process_launcher.LaunchRejected("store_locked")
        card = task_store.get_task(tmp_path, task_id) or {}
        return {
            "returncode": 0,
            "stdout": json.dumps({**card, "read_only": True, "allowed_writes": []}),
            "stderr": "",
        }

    monkeypatch.setattr(manager, "_show_task", transient_second_show)
    failed = manager._ensure_quality_reviewer_card_bound(
        request_id=request_id,
        target_request_id="target-request",
        target_task_id="target-task",
        reviewer_task_id=task_id,
        runner="codex",
        adapter_id="codex_cli",
        lens="correctness",
        target_card={"adapter_id": "claude_cli"},
        terminalize_on_failure=True,
    )

    retained = manager._latest_by_request()[request_id]
    assert failed["recovery_state"] == "starting_reservation_retained"
    assert failed["claim_recovery_bound"] is True
    assert retained["claim_recovery_state"] == "claim_commit_ambiguous"
    assert "reviewer_claim_epoch" not in retained

    callbacks: list[tuple[str, int]] = []
    monkeypatch.setattr(
        process_launcher.task_store,
        "enqueue_terminal_callback",
        lambda _repo, _task_id, *, claim_epoch, **_kwargs: (
            callbacks.append((_task_id, int(claim_epoch))) or True
        ),
    )
    first = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    second = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)

    recovered = task_store.get_task(tmp_path, task_id)
    assert first["ambiguous_claims_recovered"] == 1
    assert first["reservations_retired"] == 1
    assert first["terminal_intents_settled"] == 1
    assert second["ambiguous_claims_recovered"] == 0
    assert second["reservations_retired"] == 0
    assert recovered is not None
    assert recovered["status"] == "pending"
    assert recovered["worker_status"] == "unclaimed"
    assert len(callbacks) == 1


def test_ambiguous_claim_recovery_preserves_mismatched_or_newer_claim(
    tmp_path, monkeypatch
):
    task_id = "REVIEWER_AMBIGUOUS_MISMATCH"
    request_id = "ambiguous-mismatch"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )
    claimed = task_engine.claim_start_exact(
        tmp_path, task_id, "codex", "quality_review", "newer-request"
    )
    assert claimed["ok"] is True
    claimed_card = task_store.get_task(tmp_path, task_id)
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    event = _starting(request_id, task_id=task_id, deadline=time.time() - 1.0)
    event.pop("reviewer_claim_epoch")
    event["claim_recovery_state"] = "claim_commit_ambiguous"
    manager._append_event(event)

    first = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    second = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)

    latest = manager._latest_by_request()[request_id]
    assert first["ambiguous_claims_recovered"] == 0
    assert second["ambiguous_claims_recovered"] == 0
    assert first["reservations_retired"] == 0
    assert second["reservations_retired"] == 0
    assert latest["state"] == "starting"
    assert "reviewer_claim_epoch" not in latest
    assert task_store.get_task(tmp_path, task_id) == claimed_card


@pytest.mark.parametrize("identity_field", ["provider_pid", "owner_pid"])
def test_committed_malformed_process_identity_preserves_reservation(
    tmp_path, monkeypatch, identity_field
):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    request_id = f"malformed-{identity_field}"
    task_id = f"REVIEWER_{identity_field.upper()}"
    event = _starting(request_id, task_id=task_id, deadline=time.time() + 60.0)
    event.update(
        state="provider_spawn_committed",
        provider_pid=0,
        owner_pid=0,
    )
    event[identity_field] = "not-a-pid"

    receipt = manager._live_reviewer_receipt(
        task_id, latest={request_id: event}
    )

    assert receipt is not None
    assert receipt["request_id"] == request_id


def test_retry_preserves_matching_starting_reservation_with_malformed_lease(
    tmp_path, monkeypatch
):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    request_id = "malformed-lease-retry"
    task_id = "REVIEWER_MALFORMED_LEASE_RETRY"
    event = _starting(request_id, task_id=task_id, deadline="not-a-deadline")
    event["quality_review_attempt"] = {
        "target_request_id": "target-request",
        "target_task_id": "target-task",
        "lens": "correctness",
    }
    manager._append_event(event)

    receipt = manager._live_reviewer_receipt(
        task_id,
        manager._latest_by_request(),
        target_request_id="target-request",
        target_task_id="target-task",
        lens="correctness",
    )

    assert receipt is not None
    assert receipt["ok"] is True
    assert receipt["already_reserved"] is True
    assert receipt["request_id"] == request_id
    assert receipt["pid"] == 0
    events = manager._request_events(request_id)
    assert len(events) == 1
    assert events[0]["state"] == "starting"
    assert events[0]["reservation_expires_at_epoch"] == "not-a-deadline"


def test_unrelated_admission_contains_malformed_starting_lease(
    tmp_path, monkeypatch
):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    malformed = _starting("malformed-existing", deadline="not-a-deadline")
    unrelated = {
        "request_id": "unrelated-launch",
        "state": "finished",
        "pid": 0,
    }

    active = manager._active_request_ids(
        {
            "malformed-existing": malformed,
            "unrelated-launch": unrelated,
        }
    )

    # Admission fails closed on ambiguous authority without allowing an
    # unrelated launch attempt to crash on the malformed durable value.
    assert active == {"malformed-existing"}


def test_terminal_intent_write_failure_keeps_claim_bound_starting_retryable(
    tmp_path, monkeypatch
):
    task_id = "REVIEWER_TERMINAL_INTENT_RETRY"
    request_id = "terminal-intent-retry"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )
    claimed = task_engine.claim_start_exact(
        tmp_path, task_id, "codex", "quality_review", request_id
    )
    claimed_card = task_store.get_task(tmp_path, task_id)
    assert claimed["ok"] is True
    assert claimed_card is not None

    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    manager._append_event(
        _starting(
            request_id,
            task_id=task_id,
            claim_epoch=int(claimed_card["claim_epoch"]),
            deadline=time.time() - 1.0,
        )
    )
    original_record = manager._record_reviewer_terminal_intent
    monkeypatch.setattr(
        manager,
        "_record_reviewer_terminal_intent",
        lambda *_args, **_kwargs: "record_failed",
    )

    manager._terminalize_reviewer_attempt(
        request_id,
        task_id,
        "codex",
        "codex_cli",
        reason="launch_failed_after_claim",
    )

    latest = manager._latest_by_request()[request_id]
    assert latest["state"] == "starting"
    assert latest["reviewer_claim_epoch"] == claimed_card["claim_epoch"]
    assert task_store.get_task(tmp_path, task_id) == claimed_card
    assert not any(
        event.get("state") == "blocked"
        for event in manager._request_events(request_id)
    )

    monkeypatch.setattr(manager, "_record_reviewer_terminal_intent", original_record)
    scan = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    recovered = task_store.get_task(tmp_path, task_id)

    assert scan["reservations_retired"] == 1
    assert scan["terminal_intents_settled"] == 1
    assert manager._latest_by_request()[request_id]["state"] == "blocked"
    assert recovered is not None
    assert recovered["status"] == "pending"
    assert recovered["worker_status"] == "unclaimed"


@pytest.mark.parametrize(
    "existing_intent",
    [
        "{malformed",
        json.dumps(
            {
                "schema_id": process_launcher.REVIEWER_TERMINAL_INTENT_SCHEMA_ID,
                "request_id": "foreign-request",
                "task_id": "REVIEWER_EXISTING_INTENT",
                "runner": "codex",
                "reviewer_claim_epoch": 1,
                "substatus": "reviewer_reservation_retirement_intent",
                "blocked_reason": "launch_failed_after_claim",
            }
        ),
        json.dumps(
            {
                "schema_id": process_launcher.REVIEWER_TERMINAL_INTENT_SCHEMA_ID,
                "request_id": "existing-intent",
                "task_id": "REVIEWER_EXISTING_INTENT",
                "runner": "codex",
                "reviewer_claim_epoch": 999,
                "substatus": "reviewer_reservation_retirement_intent",
                "blocked_reason": "launch_failed_after_claim",
            }
        ),
    ],
    ids=["malformed", "foreign", "mismatched"],
)
def test_untrusted_existing_terminal_intent_cannot_authorize_terminalization(
    tmp_path, monkeypatch, existing_intent
):
    task_id = "REVIEWER_EXISTING_INTENT"
    request_id = "existing-intent"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )
    claimed = task_engine.claim_start_exact(
        tmp_path, task_id, "codex", "quality_review", request_id
    )
    claimed_card = task_store.get_task(tmp_path, task_id)
    assert claimed["ok"] is True
    assert claimed_card is not None

    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    manager._append_event(
        _starting(
            request_id,
            task_id=task_id,
            claim_epoch=int(claimed_card["claim_epoch"]),
            deadline=time.time() - 1.0,
        )
    )
    intent_path = manager._reviewer_terminal_intent_path(request_id)
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    intent_path.write_text(existing_intent, encoding="utf-8")

    manager._terminalize_reviewer_attempt(
        request_id,
        task_id,
        "codex",
        "codex_cli",
        reason="launch_failed_after_claim",
    )

    latest = manager._latest_by_request()[request_id]
    assert latest["state"] == "starting"
    assert task_store.get_task(tmp_path, task_id) == claimed_card
    assert not any(
        event.get("state") == "blocked"
        for event in manager._request_events(request_id)
    )


def test_misnamed_terminal_intent_has_no_settlement_authority(tmp_path, monkeypatch):
    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    request_id = "foreign-payload-request"
    payload = {
        "schema_id": process_launcher.REVIEWER_TERMINAL_INTENT_SCHEMA_ID,
        "request_id": request_id,
        "task_id": "FOREIGN_PAYLOAD_TASK",
        "runner": "codex",
        "reviewer_claim_epoch": 1,
        "substatus": "reviewer_reservation_retirement_intent",
        "blocked_reason": "reservation_expired",
    }
    canonical = manager._reviewer_terminal_intent_path(request_id)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    forged = canonical.with_name(
        "0" * 64 + manager._REVIEWER_TERMINAL_INTENT_SUFFIX
    )
    forged.write_text(json.dumps(payload), encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        task_store,
        "mark_terminal_failure",
        lambda *_args, **_kwargs: calls.append("terminal") or (True, "blocked"),
    )
    monkeypatch.setattr(
        task_store,
        "enqueue_terminal_callback",
        lambda *_args, **_kwargs: calls.append("callback") or True,
    )
    monkeypatch.setattr(
        task_engine.core,
        "retry_terminal_task",
        lambda *_args, **_kwargs: calls.append("retry") or {"ok": True},
    )

    assert manager._settle_reviewer_terminal_intents() == 0
    assert manager._settle_reviewer_terminal_intents() == 0
    assert calls == []
    assert forged.exists()
    assert not canonical.exists()
def test_post_claim_launch_failure_release_error_is_reaped_exactly_once(
    tmp_path, monkeypatch
):
    task_id = "REVIEWER_POST_CLAIM_RELEASE_ERROR"
    request_id = "post-claim-release-error"
    _seed_pending_reviewer(tmp_path, task_id)
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *_args, **_kwargs: None
    )
    claimed = task_engine.claim_start_exact(
        tmp_path, task_id, "codex", "quality_review", request_id
    )
    claimed_card = task_store.get_task(tmp_path, task_id)
    assert claimed["ok"] is True
    assert claimed_card is not None
    claim_epoch = int(claimed_card["claim_epoch"])

    manager = _manager_for_periodic_scan(tmp_path, monkeypatch)
    manager._append_event(
        _starting(
            request_id,
            task_id=task_id,
            claim_epoch=claim_epoch,
            deadline=time.time() - 1.0,
        )
    )
    monkeypatch.setattr(
        task_engine,
        "mark_launch_failed",
        lambda *_args, **_kwargs: {"ok": False, "error": "store_locked"},
    )

    released, retained = (
        manager._release_or_retain_reviewer_claim_after_launch_failure(
            request_id=request_id,
            task_id=task_id,
            runner="codex",
            reviewer_claim_epoch=claim_epoch,
            reason="post_claim_pre_spawn_error",
        )
    )

    latest = manager._latest_by_request()[request_id]
    assert released == {"ok": False, "error": "store_locked"}
    assert retained is True
    assert latest["state"] == "starting"
    assert latest["reviewer_claim_epoch"] == claim_epoch
    assert not any(
        event.get("state") == "launch_failed"
        for event in manager._request_events(request_id)
    )

    first = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    second = task_reconciler.run_scan(manager, repo=tmp_path, include_gc=False)
    recovered = task_store.get_task(tmp_path, task_id)

    assert first["reservations_retired"] == 1
    assert first["terminal_intents_settled"] == 1
    assert second["reservations_retired"] == 0
    assert second["terminal_intents_settled"] == 0
    assert recovered is not None
    assert recovered["status"] == "pending"
    assert recovered["worker_status"] == "unclaimed"
