"""NF-2026-02275 / NF-2026-02276: a dead finalizer ends finalization explicitly.

Field case: worker_exit normal, supervisor_alive false, child_alive false,
phase validating, elapsed 2007 s while the heartbeat age stayed 8 s. Nothing
could settle the request, cancel refused it as ``finalization_pending`` and
``merge_risks`` kept listing the closed card as an active writer.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, finalize_retry_dispatch, process_launcher, task_store  # noqa: E402
from aiworkhub.worker_workspace import MAX_VALIDATION_SECONDS  # noqa: E402

SUPERVISOR = (4101, 11)
CHILD = (4102, 12)
FINALIZER = (4103, 13)
RID, TID, RUNNER = "e842ffc99bca4d919da8b0ebcf25413c", "remove-custom-events-wave2", "codex_a"


def _manager(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "process_log.jsonl",
        process_dir=tmp_path / "proc",
        isolation_enabled=False,
    )


def _alive(monkeypatch, *identities):
    live = set(identities)
    monkeypatch.setattr(
        process_launcher, "_pid_matches", lambda pid, ticks: (pid, ticks) in live
    )


def _finalizer_section(*, beat=None, elapsed=10.0, total=2, phase=None):
    section = {
        "pid": FINALIZER[0],
        "pid_start_ticks": FINALIZER[1],
        "heartbeat_at_epoch": time.time() if beat is None else beat,
        "phase": process_launcher.FINALIZER_VALIDATING_PHASE if phase is None else phase,
        "total": total,
        "elapsed_seconds": elapsed,
    }
    return section


def _seed(mgr, tmp_path: Path, *, state="finalizing", finalizer=None, supervisor_state="exited"):
    status_path = tmp_path / "supervisor_status.json"
    record = {
        "state": supervisor_state,
        "exit_code": 0,
        "child_pid": CHILD[0],
        "child_pid_start_ticks": CHILD[1],
        "heartbeat_at_epoch": time.time() - 2000,
    }
    if finalizer is not None:
        record[process_launcher.FINALIZER_STATUS_SECTION] = finalizer
    process_launcher.write_json_0600(status_path, record)
    mgr._append_event({
        "request_id": RID,
        "task_id": TID,
        "runner": RUNNER,
        "topic": "finalization",
        "state": "running",
        "pid": SUPERVISOR[0],
        "pid_start_ticks": SUPERVISOR[1],
        "supervisor_status_path": str(status_path),
    })
    mgr._append_event({"request_id": RID, "task_id": TID, "runner": RUNNER, "state": state})
    return status_path


@pytest.fixture(autouse=True)
def _writes_allowed(monkeypatch):
    """NF-2026-02417: a write-gated process never decides a dead finalizer."""
    monkeypatch.setattr(core, "writes_allowed", lambda: True)


@pytest.fixture()
def terminal_calls(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake(repo, task_id, runner, substatus, *, evidence=None, request_id=""):
        calls.append((substatus, dict(evidence or {})))
        return {"ok": True, "callback_enqueued": True}

    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_failure", fake)
    return calls


def _latest(mgr) -> dict:
    return mgr._latest_by_request()[RID]


# --------------------------------------------------------------------------- #
# The verdict: positive evidence only.
# --------------------------------------------------------------------------- #

def test_fresh_heartbeat_with_dead_supervisor_child_and_finalizer_is_finalizer_dead(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=_finalizer_section(beat=time.time()))
    _alive(monkeypatch)  # nothing matches: the beat was written by a process now gone

    latest = finalize_retry_dispatch._pending_latest(mgr, RID)
    assert finalize_retry_dispatch.dead_finalizer_reason(latest) == "finalize_failed:finalizer_dead"


def test_live_finalizer_past_the_validation_deadline_is_validation_hung(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    total = 2
    elapsed = total * MAX_VALIDATION_SECONDS + finalize_retry_dispatch.VALIDATION_DEADLINE_GRACE_SECONDS + 5
    _seed(mgr, tmp_path, finalizer=_finalizer_section(elapsed=float(elapsed), total=total))
    _alive(monkeypatch, FINALIZER)  # alive and beating: the beat cannot extend the deadline

    latest = finalize_retry_dispatch._pending_latest(mgr, RID)
    assert finalize_retry_dispatch.dead_finalizer_reason(latest) == "validation_failed:validation_hung"


@pytest.mark.parametrize("alive", [SUPERVISOR, CHILD])
def test_a_live_supervisor_or_child_is_never_ended(tmp_path, monkeypatch, alive):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=_finalizer_section())
    _alive(monkeypatch, alive)

    latest = finalize_retry_dispatch._pending_latest(mgr, RID)
    assert finalize_retry_dispatch.dead_finalizer_reason(latest) == ""


def test_missing_finalizer_section_is_unknown_not_dead(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=None)
    _alive(monkeypatch)

    latest = finalize_retry_dispatch._pending_latest(mgr, RID)
    assert finalize_retry_dispatch.dead_finalizer_reason(latest) == ""


# --------------------------------------------------------------------------- #
# _reconcile_persisted_requests ends it on the next pass.
# --------------------------------------------------------------------------- #

def test_reconcile_ends_a_dead_finalizer_with_an_explicit_reason(tmp_path, monkeypatch, terminal_calls):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=_finalizer_section(elapsed=2007.0))
    _alive(monkeypatch)

    def must_not_refinalize(*_a, **_k):
        raise AssertionError("a provably dead finalizer is ended, not re-run")

    monkeypatch.setattr(mgr, "_finalize_after_process_exit", must_not_refinalize)
    # The retained candidate is re-finalized through the retry path; here it
    # is refused (no retained workspace), so the end is recorded explicitly.
    monkeypatch.setattr(mgr, "retry_finalization", lambda *_a, **_k: {
        "ok": False, "error": "finalization_retry_workspace_missing",
    })

    stats = mgr._reconcile_persisted_requests()

    assert stats["finalized"] == 1
    latest = _latest(mgr)
    assert latest["state"] == "finalize_failed"
    assert latest["state"] in process_launcher.TERMINAL_PROCESS_STATES
    assert latest["finalization_end_reason"] == "finalize_failed:finalizer_dead"
    assert latest["release_transition_ok"] is True
    assert [call[0] for call in terminal_calls] == ["finalize_failed"]
    assert terminal_calls[0][1]["error"] == "finalize_failed:finalizer_dead"
    # Terminal: the next pass has nothing left to do.
    assert mgr._reconcile_persisted_requests()["finalized"] == 0


def test_reconcile_ends_a_hung_validation_without_the_request_lock(tmp_path, monkeypatch, terminal_calls):
    mgr = _manager(tmp_path)
    elapsed = 2 * MAX_VALIDATION_SECONDS + finalize_retry_dispatch.VALIDATION_DEADLINE_GRACE_SECONDS + 5
    _seed(mgr, tmp_path, finalizer=_finalizer_section(elapsed=float(elapsed), total=2))
    _alive(monkeypatch, FINALIZER)
    monkeypatch.setattr(mgr, "_finalize_after_process_exit", lambda *_a, **_k: None)

    with mgr._request_lock(RID):  # the hung finalizer still owns the lock
        mgr._reconcile_persisted_requests()

    latest = _latest(mgr)
    assert latest["state"] == "finalize_failed"
    assert latest["finalization_end_reason"] == "validation_failed:validation_hung"
    assert terminal_calls[0][1]["error"] == "validation_failed:validation_hung"


def test_reconcile_leaves_a_live_finalizer_to_its_own_finalization(tmp_path, monkeypatch, terminal_calls):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=_finalizer_section())
    _alive(monkeypatch, FINALIZER)
    seen: list[str] = []
    monkeypatch.setattr(
        mgr, "_finalize_after_process_exit", lambda request_id, *a, **k: seen.append(request_id)
    )

    mgr._reconcile_persisted_requests()

    assert seen == [RID]
    assert _latest(mgr)["state"] == "finalizing"
    assert terminal_calls == []


def test_a_late_write_of_the_fenced_finalizer_is_ignored(tmp_path, monkeypatch, terminal_calls):
    mgr = _manager(tmp_path)
    elapsed = 2 * MAX_VALIDATION_SECONDS + finalize_retry_dispatch.VALIDATION_DEADLINE_GRACE_SECONDS + 5
    _seed(mgr, tmp_path, finalizer=_finalizer_section(elapsed=float(elapsed), total=2))
    _alive(monkeypatch, FINALIZER)  # hung: alive, past the validation deadline
    assert finalize_retry_dispatch.end_if_finalizer_dead(mgr, RID)["state"] == "finalize_failed"

    late_epoch = finalize_retry_dispatch.finalizer_claim_epoch(mgr._request_events(RID)[:2])
    with process_launcher._FinalizerHeartbeat(RID, claim_epoch=late_epoch):  # the late finalizer
        late = mgr._append_event({"request_id": RID, "task_id": TID, "state": "review_ready"})
    assert late["finalizer_fenced"] is True
    assert _latest(mgr)["state"] == "finalize_failed"

    # Any other writer is untouched by the fence.
    other = mgr._append_event({"request_id": RID, "task_id": TID, "state": "finalize_failed",
                               "workspace_disposition": "retained_in_place"})
    assert "finalizer_fenced" not in other


# --------------------------------------------------------------------------- #
# agent_cancel_task in FINALIZATION_PENDING_STATES (NF-2026-02276).
# --------------------------------------------------------------------------- #

def _cancel_ready(mgr, monkeypatch):
    monkeypatch.setattr(
        mgr, "_publish_bridge_cancellation_before_finalization", lambda *_a, **_k: ""
    )


def test_cancel_of_a_dead_finalizer_records_cancelled_and_fences(tmp_path, monkeypatch, terminal_calls):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=_finalizer_section())
    _alive(monkeypatch)
    _cancel_ready(mgr, monkeypatch)

    result = mgr.cancel(RID, reason="manager_cancel")

    assert result["ok"] is True
    assert result["state"] == "cancelled"
    assert result["cancelled_from_state"] == "finalizing"
    latest = _latest(mgr)
    assert latest["state"] == "cancelled"
    assert "manager_cancel" in latest["reason"]
    assert [call[0] for call in terminal_calls] == ["cancelled"]
    assert finalize_retry_dispatch.finalizer_fence_path(mgr, RID).is_file()
    fenced_epoch = finalize_retry_dispatch.stored_fence_epoch(mgr, RID)
    with process_launcher._FinalizerHeartbeat(RID, claim_epoch=fenced_epoch):
        assert mgr._append_event({"request_id": RID, "state": "review_ready"})["finalizer_fenced"]
    assert _latest(mgr)["state"] == "cancelled"


def test_cancel_of_a_live_finalizer_still_refuses_and_names_its_pid(tmp_path, monkeypatch, terminal_calls):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=_finalizer_section())
    _alive(monkeypatch, FINALIZER)
    _cancel_ready(mgr, monkeypatch)

    result = mgr.cancel(RID)

    assert result["ok"] is False
    assert result["blocked_reason"] == "finalization_pending"
    assert result["finalizer_alive"] is True
    assert result["finalizer_pid"] == FINALIZER[0]
    assert _latest(mgr)["state"] == "finalizing"
    assert terminal_calls == []
    assert not finalize_retry_dispatch.finalizer_fence_path(mgr, RID).exists()


def test_cancel_refuses_while_the_request_lock_is_held(tmp_path, monkeypatch, terminal_calls):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, state="reconcile_pending", finalizer=None)
    _alive(monkeypatch)
    _cancel_ready(mgr, monkeypatch)
    monkeypatch.setattr(
        mgr, "_request_lock",
        lambda *_a, **_k: (_ for _ in ()).throw(process_launcher.AdvisoryLockTimeout("busy")),
    )

    result = mgr.cancel(RID)

    assert result["ok"] is False
    assert result["blocked_reason"] == "finalization_pending"
    assert _latest(mgr)["state"] == "reconcile_pending"
    assert terminal_calls == []


# --------------------------------------------------------------------------- #
# merge_risks never lists a card closed by cancel or supersede.
# --------------------------------------------------------------------------- #

def _insert_card(repo, task_id, *, status, worker_status, allowed_writes, **extra):
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-11-15T00:00:00+00:00"
    card = {
        "task_id": task_id, "runner": "codex_a", "topic": "coding",
        "allowed_writes": allowed_writes, "depends_on": [], "priority": "",
        "status": status, "worker_status": worker_status, **extra,
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, archived_at) "
            "VALUES (?, 'codex_a', 'coding', ?, ?, '', '', ?, ?, ?, '')",
            (task_id, status, worker_status, json.dumps(card), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def test_merge_risks_skip_superseded_and_cancelled_processing_cards(tmp_path, monkeypatch):
    repo = tmp_path / "canon"
    repo.mkdir()
    task_store.initialize_repository(repo)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    repo = core.repo_root()
    for task_id, status in (("superseded-card", "superseded"), ("cancelled-card", "cancelled"),
                            ("live-card", "processing")):
        _insert_card(repo, task_id, status=status, worker_status="claimed",
                     allowed_writes=["shared.py"], launch_request_id=f"request-{task_id}")
    _insert_card(repo, "candidate", status="pending", worker_status="unclaimed",
                 allowed_writes=["shared.py"])

    # The stale worker_status still projects both closed cards as processing.
    guard = json.loads(core.launch_collision_guard(task_id="candidate", print_json=True)["stdout"])
    assert {risk["task_id"] for risk in guard["merge_risks"]} >= {"superseded-card", "cancelled-card"}

    launcher = SimpleNamespace(
        repo=repo,
        _collision_guard=lambda task_id, print_json: core.launch_collision_guard(
            task_id=task_id, print_json=print_json
        ),
    )
    risks = process_launcher.ProcessManager._launch_merge_risks(launcher, "candidate")

    assert [risk["task_id"] for risk in risks] == ["live-card"]
    assert risks[0]["paths"] == ["shared.py"]


def test_closed_by_disposal_never_relaxes_the_live_archive_guard():
    live = {"status": "processing", "worker_status": "claimed"}
    assert task_store.closed_by_disposal(live) is False
    assert task_store.canonical_status(live) == "processing"
    assert task_store.closed_by_disposal({"status": "superseded", "worker_status": "claimed"})
    assert task_store.closed_by_disposal({"archived_at": "2026-11-15T00:00:00+00:00"})


# --------------------------------------------------------------------------- #
# Rework: fail-closed cancel, dead validation command, re-finalize, epoch fence.
# --------------------------------------------------------------------------- #

def test_a_cancel_refused_while_the_supervisor_is_alive(tmp_path, monkeypatch, terminal_calls):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=None)  # no finalizer section: only the supervisor decides
    _alive(monkeypatch, SUPERVISOR)
    _cancel_ready(mgr, monkeypatch)

    result = mgr.cancel(RID)

    assert result["ok"] is False
    assert result["blocked_reason"] == "finalization_pending"
    assert _latest(mgr)["state"] == "finalizing"
    assert terminal_calls == []
    assert not finalize_retry_dispatch.finalizer_fence_path(mgr, RID).exists()


@pytest.mark.parametrize("status", [None, {}])
def test_b_cancel_refused_when_the_status_is_unreadable(tmp_path, monkeypatch, terminal_calls, status):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=_finalizer_section())
    _alive(monkeypatch)
    _cancel_ready(mgr, monkeypatch)
    monkeypatch.setattr(process_launcher, "read_supervisor_status", lambda _path: status)

    result = mgr.cancel(RID)  # never an AttributeError on a None status

    assert result["ok"] is False
    assert result["blocked_reason"] == "finalization_pending"
    assert result["finalizer_alive"] is False
    assert terminal_calls == []
    assert not finalize_retry_dispatch.finalizer_fence_path(mgr, RID).exists()


def test_d_finalizer_dead_with_a_passing_sealed_diff_ends_in_review(tmp_path, monkeypatch, terminal_calls):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=_finalizer_section())
    _alive(monkeypatch)
    retried: list[tuple[str, str, int]] = []

    def retry(request_id, task_id, *, validation_lane=""):
        # The real retry path, reduced to its contract: admitted under a fresh
        # epoch, the sealed diff validates and the card goes to review.
        epoch = finalize_retry_dispatch.fresh_finalizer_epoch(mgr, request_id)
        retried.append((request_id, task_id, epoch))
        with process_launcher._FinalizerHeartbeat(request_id, claim_epoch=epoch):
            mgr._append_event({"request_id": request_id, "task_id": task_id,
                               "state": "finalizing", "finalizer_claim_epoch": epoch})
            mgr._append_event({"request_id": request_id, "task_id": task_id, "state": "review_ready"})
        return {"ok": True, "request_id": request_id, "state": "review_ready"}

    monkeypatch.setattr(mgr, "retry_finalization", retry)

    result = finalize_retry_dispatch.end_if_finalizer_dead(mgr, RID)

    assert result["state"] == "review_ready"
    assert result["finalization_end_reason"] == "finalize_failed:finalizer_dead"
    assert retried and retried[0][:2] == (RID, TID)
    assert retried[0][2] == finalize_retry_dispatch.stored_fence_epoch(mgr, RID) + 1
    assert _latest(mgr)["state"] == "review_ready"
    assert terminal_calls == []  # never recorded finalize_failed


def test_d_a_refused_refinalization_falls_back_to_finalize_failed(tmp_path, monkeypatch, terminal_calls):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=_finalizer_section())
    _alive(monkeypatch)
    monkeypatch.setattr(mgr, "retry_finalization", lambda *_a, **_k: {
        "ok": False, "error": "finalization_retry_metadata_invalid",
    })

    result = finalize_retry_dispatch.end_if_finalizer_dead(mgr, RID)

    assert result["state"] == "finalize_failed"
    assert _latest(mgr)["finalization_end_reason"] == "finalize_failed:finalizer_dead"
    assert [call[0] for call in terminal_calls] == ["finalize_failed"]


def test_e_a_fence_of_epoch_n_never_drops_epoch_n_plus_one(tmp_path, monkeypatch, terminal_calls):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=_finalizer_section())
    latest = finalize_retry_dispatch._pending_latest(mgr, RID)
    finalize_retry_dispatch.fence_finalizer(mgr, RID, "finalize_failed:finalizer_dead", latest)
    fenced_epoch = finalize_retry_dispatch.stored_fence_epoch(mgr, RID)
    assert type(fenced_epoch) is int
    assert finalize_retry_dispatch.finalizer_fenced(mgr, RID, fenced_epoch) is True
    assert finalize_retry_dispatch.finalizer_fenced(mgr, RID, fenced_epoch + 1) is False
    assert finalize_retry_dispatch.finalizer_fenced(mgr, RID, None) is False
    assert finalize_retry_dispatch.fresh_finalizer_epoch(mgr, RID) == fenced_epoch + 1

    with process_launcher._FinalizerHeartbeat(RID, claim_epoch=fenced_epoch + 1):
        written = mgr._append_event({"request_id": RID, "task_id": TID, "state": "review_ready"})
    assert "finalizer_fenced" not in written
    assert _latest(mgr)["state"] == "review_ready"

    # The fenced finalizer can transition no card, review or failure.
    metadata = {"task_id": TID, "runner": RUNNER}
    reviews: list[str] = []
    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_review",
                        lambda *a, **k: reviews.append("review") or {"ok": True})
    with process_launcher._FinalizerHeartbeat(RID, claim_epoch=fenced_epoch):
        review = mgr._review_terminal_exact(metadata, "review_ready", request_id=RID)
        failure = mgr._terminal_failure_exact(metadata, "finalize_failed", request_id=RID, evidence={})
        recorded = finalize_retry_dispatch.record_finalizer_failed(
            mgr, RID, 0, {"task_id": TID, "runner": RUNNER}, "late", 0, time.monotonic()
        )
    assert review["finalizer_fenced"] is True and review["ok"] is False
    assert failure["finalizer_fenced"] is True and failure["ok"] is False
    assert recorded["finalizer_fenced"] is True
    assert reviews == [] and terminal_calls == []
    assert _latest(mgr)["state"] == "review_ready"


def test_f_cancel_with_a_failed_release_returns_not_ok(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    _seed(mgr, tmp_path, finalizer=_finalizer_section())
    _alive(monkeypatch)
    _cancel_ready(mgr, monkeypatch)
    monkeypatch.setattr(
        process_launcher.task_engine, "mark_terminal_failure",
        lambda *_a, **_k: {"ok": False, "stderr": "illegal_transition:archived->failed"},
    )

    result = mgr.cancel(RID, reason="manager_cancel")

    assert result["ok"] is False
    assert result["release_transition_ok"] is False
    assert "illegal_transition" in result["stderr"]
    assert result.get("state") != "cancelled"
    assert _latest(mgr)["state"] == "finalizing"


# --------------------------------------------------------------------------- #
# Rework: an unreadable fence record is no fence.
# --------------------------------------------------------------------------- #

def test_i_unreadable_fence_record_is_not_fenced_and_never_raises(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    monkeypatch.setattr(process_launcher, "read_supervisor_status", lambda _path: None)

    assert finalize_retry_dispatch.stored_fence_epoch(mgr, RID) is None
    with process_launcher._FinalizerHeartbeat(RID, claim_epoch=1):
        assert finalize_retry_dispatch.active_finalizer_fenced(mgr, RID) is False
