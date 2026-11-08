"""NF-2026-02099: ``retry_finalization`` never holds the MCP call for the finalize.

Measured on needfix-NF-2026-02088: the tool call ran the retried finalization
synchronously, hung 1808 s until the client gave up, and the request then sat in
``finalizing`` with ``request_lock_busy`` and no callback. These tests pin the
fix on the real ``ProcessManager`` (the NF-2026-01030 harness, imported rather
than copied): admission and every refusal are still answered synchronously, an
admitted finalize runs detached behind a receipt, a duplicate call never queues
behind the request lock, a busy lock is refused at once, and a ``finalizing``
row whose finalizer is gone is reclaimed by liveness, not by waiting.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _path in (_SRC, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from aiworkhub import finalize_retry_dispatch, process_launcher  # noqa: E402
from test_retry_finalization_host_lane_nf01030 import (  # noqa: E402
    _REQUEST_ID,
    _TASK_ID,
    _harness,
    _isolate_runtime_state,
)

# Re-exported autouse fixture: the same isolated audit/toolchain state.
__all__ = ["_isolate_runtime_state"]

# Test-side bound on any wait for a detached thread; never a product deadline.
_BOUND_SECONDS = 30.0


def _wait_idle() -> bool:
    deadline = time.monotonic() + _BOUND_SECONDS
    while finalize_retry_dispatch.in_flight(_REQUEST_ID):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(finalize_retry_dispatch, "SETTLE_GRACE_SECONDS", 0.05)
    # A cold storage quick_check alone took 28 s in the release's Linux tree;
    # the product's 30 s admission wait is not what these tests measure.
    monkeypatch.setattr(finalize_retry_dispatch, "ADMISSION_WAIT_SECONDS", 300.0)
    yield
    assert _wait_idle(), "a dispatched retry outlived its test"


class _StalledFinalize:
    """A finalize that blocks until released, like the validation that hung."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[str, object]] = []

    def __call__(self, request_id: str, supervisor_returncode=None) -> dict:
        self.calls.append((request_id, supervisor_returncode))
        self.entered.set()
        self.release.wait(_BOUND_SECONDS)
        return {
            "request_id": request_id,
            "task_id": _TASK_ID,
            "state": "review_ready",
            "workspace_retained": True,
            "error": "",
        }


def _finalize_failed_harness(monkeypatch, tmp_path):
    return _harness(
        monkeypatch,
        tmp_path,
        terminal_state="finalize_failed",
        terminal_error="finalize_failed:validation_exec_unavailable",
    )


def _record_outcomes(monkeypatch) -> list[dict]:
    outcomes: list[dict] = []
    monkeypatch.setattr(
        process_launcher.validation_auto_rework,
        "after_finalize",
        lambda _manager, result: outcomes.append(dict(result)),
    )
    return outcomes


def _append_abandoned_finalizing_row(harness) -> None:
    metadata = harness.metadata_on_disk
    harness.manager._append_event({
        **harness.latest_event,
        "state": "finalizing",
        "error": "",
        "finalization_retry": True,
        "metadata_path": str(harness.metadata_path),
        "supervisor_status_path": metadata["supervisor_status_path"],
    })


def test_an_admitted_retry_returns_a_receipt_while_the_finalize_still_runs(monkeypatch, tmp_path):
    harness = _finalize_failed_harness(monkeypatch, tmp_path)
    stalled = _StalledFinalize()
    monkeypatch.setattr(harness.manager, "_finalize_isolated_request", stalled)
    outcomes = _record_outcomes(monkeypatch)
    try:
        result = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)

        assert result["ok"] is True, result
        assert result["deferred"] is True
        assert result["finalization_retry_dispatched"] is True
        assert result["already_in_flight"] is False
        assert result["state"] == "finalizing"
        assert result["provider_relaunched"] is False
        assert result["outcome_delivery"] == "terminal_review_callback"
        assert result["schema_id"] == finalize_retry_dispatch.RECEIPT_SCHEMA_ID
        assert stalled.entered.wait(_BOUND_SECONDS), "the admitted finalize never started"
        assert finalize_retry_dispatch.in_flight(_REQUEST_ID)
        assert harness.latest_event["state"] == "finalizing"
        assert len(harness.transitions) == 1
        assert outcomes == [], "the outcome was delivered before the finalize ended"
    finally:
        stalled.release.set()
    assert _wait_idle()
    assert stalled.calls == [(_REQUEST_ID, 0)]
    assert [outcome["state"] for outcome in outcomes] == ["review_ready"]
    assert outcomes[0]["ok"] is True


def test_a_second_retry_while_one_is_in_flight_gets_a_receipt_not_a_queue(monkeypatch, tmp_path):
    harness = _finalize_failed_harness(monkeypatch, tmp_path)
    stalled = _StalledFinalize()
    monkeypatch.setattr(harness.manager, "_finalize_isolated_request", stalled)
    _record_outcomes(monkeypatch)
    try:
        first = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)
        assert stalled.entered.wait(_BOUND_SECONDS)
        second = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)
    finally:
        stalled.release.set()
    assert first["deferred"] is True and first["already_in_flight"] is False
    assert second["ok"] is True and second["already_in_flight"] is True
    assert second["state"] == "finalizing"
    assert _wait_idle()
    assert len(stalled.calls) == 1 and len(harness.transitions) == 1


def test_a_finalize_that_settles_promptly_still_answers_with_its_outcome(monkeypatch, tmp_path):
    harness = _finalize_failed_harness(monkeypatch, tmp_path)
    monkeypatch.setattr(finalize_retry_dispatch, "SETTLE_GRACE_SECONDS", _BOUND_SECONDS)

    result = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)

    assert result["ok"] is True, result
    assert result["state"] == "review_ready"
    assert "deferred" not in result
    assert not finalize_retry_dispatch.in_flight(_REQUEST_ID)


def test_a_refusal_is_still_answered_synchronously_with_nothing_in_flight(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)

    result = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)

    assert result == {
        "ok": False,
        "request_id": _REQUEST_ID,
        "task_id": _TASK_ID,
        "error": "request_not_retryable_finalization_failure:validation_failed",
    }
    assert not finalize_retry_dispatch.in_flight(_REQUEST_ID)
    assert harness.transitions == [] and harness.finalized == []


def test_a_busy_request_lock_is_refused_at_once_never_waited_on(monkeypatch, tmp_path):
    harness = _finalize_failed_harness(monkeypatch, tmp_path)
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with harness.manager._request_lock(_REQUEST_ID):
            held.set()
            release.wait(_BOUND_SECONDS)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(_BOUND_SECONDS)
    try:
        started = time.monotonic()
        result = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(_BOUND_SECONDS)

    assert result["ok"] is False
    assert result["error"].startswith("finalization_retry_request_lock_busy:"), result
    # The old path polled the lock up to the shared advisory bound (20 s).
    assert elapsed < 10.0
    assert harness.transitions == [] and harness.finalized == []
    assert harness.latest_event["state"] == "finalize_failed"


def test_a_finalizing_row_whose_finalizer_is_gone_is_reclaimed_by_liveness(monkeypatch, tmp_path):
    harness = _finalize_failed_harness(monkeypatch, tmp_path)
    _append_abandoned_finalizing_row(harness)
    # Positive evidence of death: the section's exact pid/ticks match no process.
    _write_dead_finalizer_section(harness)
    monkeypatch.setattr(finalize_retry_dispatch, "SETTLE_GRACE_SECONDS", _BOUND_SECONDS)

    result = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)

    assert result["ok"] is True, result
    assert result["state"] == "review_ready"
    # The card already left finalize_failed with the abandoned attempt.
    assert harness.transitions == []
    [finalized] = harness.finalized
    assert finalized["event"]["state"] == "finalizing"
    assert finalized["event"]["finalization_retry_reclaimed_abandoned"] is True


def test_a_finalizing_row_with_a_live_finalizer_is_never_reclaimed(monkeypatch, tmp_path):
    ticks = process_launcher._pid_start_ticks(os.getpid())
    if ticks is None:
        pytest.skip("no exact process identity on this host: a finalizer heartbeat is never published")
    harness = _finalize_failed_harness(monkeypatch, tmp_path)
    _append_abandoned_finalizing_row(harness)
    status_path = Path(harness.metadata_on_disk["supervisor_status_path"])
    assert process_launcher.write_finalizer_heartbeat(status_path, {
        "pid": os.getpid(),
        "pid_start_ticks": ticks,
        "heartbeat_at_epoch": time.time(),
        "phase": "validating",
        "total": 4,
        "elapsed_seconds": 1.0,
    })

    result = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)

    assert result == {
        "ok": False,
        "request_id": _REQUEST_ID,
        "task_id": _TASK_ID,
        "error": "request_not_retryable_finalization_failure:finalizing",
    }
    assert harness.transitions == [] and harness.finalized == []


def _dead_pid() -> int:
    """The pid of a child that has already exited: positive evidence of death."""
    completed = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True, text=True, check=True, timeout=_BOUND_SECONDS,
    )
    return int(completed.stdout.strip())


def _write_dead_finalizer_section(harness) -> None:
    status_path = Path(harness.metadata_on_disk["supervisor_status_path"])
    assert process_launcher.write_finalizer_heartbeat(status_path, {
        "pid": _dead_pid(),
        "pid_start_ticks": 1,
        "heartbeat_at_epoch": time.time(),
        "phase": "validating",
        "total": 4,
        "elapsed_seconds": 1.0,
    })


def test_a_finalize_that_raises_after_admission_ends_finalize_failed_not_finalizing(monkeypatch, tmp_path):
    harness = _finalize_failed_harness(monkeypatch, tmp_path)
    terminal: list[tuple] = []
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        lambda *args, **kwargs: terminal.append((args, kwargs))
        or {"ok": True, "callback_enqueued": True},
    )

    def crash(request_id, supervisor_returncode=None):
        raise RuntimeError("validation host vanished")

    monkeypatch.setattr(harness.manager, "_finalize_isolated_request", crash)
    monkeypatch.setattr(finalize_retry_dispatch, "SETTLE_GRACE_SECONDS", _BOUND_SECONDS)

    result = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)

    assert result["ok"] is False
    assert result["state"] == "finalize_failed", result
    assert result["error"], result
    assert not finalize_retry_dispatch.in_flight(_REQUEST_ID)
    latest = harness.latest_event
    assert latest["state"] == "finalize_failed", latest
    assert latest["callback_enqueued"] is True
    [(args, kwargs)] = terminal
    assert args[3] == "finalize_failed" and kwargs["request_id"] == _REQUEST_ID
    assert kwargs["evidence"]["error"].startswith("finalization_retry_crashed:RuntimeError:")


def test_a_finalizing_row_without_a_finalizer_section_is_refused_as_liveness_unknown(monkeypatch, tmp_path):
    harness = _finalize_failed_harness(monkeypatch, tmp_path)
    _append_abandoned_finalizing_row(harness)
    before = harness.latest_event
    status_path = Path(harness.metadata_on_disk["supervisor_status_path"])
    assert process_launcher.FINALIZER_STATUS_SECTION not in process_launcher.read_supervisor_status(status_path)

    result = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)

    assert result == {
        "ok": False,
        "request_id": _REQUEST_ID,
        "task_id": _TASK_ID,
        "error": "finalization_retry_finalizer_liveness_unknown",
    }
    assert harness.transitions == [] and harness.finalized == []
    assert harness.latest_event == before


def test_a_duplicate_while_the_first_retry_awaits_admission_reports_admission_pending(monkeypatch, caplog):
    monkeypatch.setattr(finalize_retry_dispatch, "ADMISSION_WAIT_SECONDS", 0.05)
    caplog.set_level(logging.WARNING, logger=finalize_retry_dispatch.__name__)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def pending_admission() -> dict:
        calls.append(_REQUEST_ID)
        entered.set()
        release.wait(_BOUND_SECONDS)
        return {"ok": False, "request_id": _REQUEST_ID, "task_id": _TASK_ID, "error": "refused_late"}

    try:
        first = finalize_retry_dispatch.dispatch(_REQUEST_ID, _TASK_ID, pending_admission)
        assert entered.wait(_BOUND_SECONDS)
        second = finalize_retry_dispatch.dispatch(_REQUEST_ID, _TASK_ID, pending_admission)
    finally:
        release.set()

    assert first["state"] == "admission_pending" and first["already_in_flight"] is False
    assert second["state"] == "admission_pending" and second["already_in_flight"] is True
    assert first["outcome_delivery"] == second["outcome_delivery"] == "poll_task_status"
    assert _wait_idle()
    assert calls == [_REQUEST_ID]
    # The late refusal is logged even though it was never admitted.
    deadline = time.monotonic() + _BOUND_SECONDS
    while not any("refused_late" in r.getMessage() for r in caplog.records):
        assert time.monotonic() < deadline, "the late refusal was dropped"
        time.sleep(0.01)
    [record] = [r for r in caplog.records if "refused_late" in r.getMessage()]
    assert record.levelno == logging.WARNING and "admitted=False" in record.getMessage()


def test_admission_is_signalled_only_by_the_owning_dispatch_thread():
    finalize_retry_dispatch.mark_admitted(_REQUEST_ID)  # undispatched: a no-op
    assert not finalize_retry_dispatch.in_flight(_REQUEST_ID)

    result = finalize_retry_dispatch.dispatch(
        _REQUEST_ID, _TASK_ID, lambda: {"ok": False, "request_id": _REQUEST_ID, "error": "refused"}
    )

    assert result == {"ok": False, "request_id": _REQUEST_ID, "error": "refused"}
    assert not finalize_retry_dispatch.in_flight(_REQUEST_ID)


@pytest.mark.parametrize(
    ("make_exc", "reason"),
    [
        (lambda: process_launcher._BridgeCancellationDeferred("cancel pending"), "bridge_cancellation_pending"),
        (lambda: process_launcher._PidIdentityUnknownDeferred("pid_identity_unknown"), "pid_identity_unknown"),
        (lambda: process_launcher.AdvisoryLockTimeout("held", errno=None), "request_lock_busy"),
    ],
)
def test_a_deferred_finalize_after_admission_is_left_for_the_reconciler_not_terminalized(
    monkeypatch, tmp_path, make_exc, reason
):
    harness = _finalize_failed_harness(monkeypatch, tmp_path)
    terminal: list[tuple] = []
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        lambda *args, **kwargs: terminal.append((args, kwargs))
        or {"ok": True, "callback_enqueued": True},
    )
    outcomes = _record_outcomes(monkeypatch)

    def defer(request_id, supervisor_returncode=None):
        raise make_exc()

    monkeypatch.setattr(harness.manager, "_finalize_isolated_request", defer)
    monkeypatch.setattr(finalize_retry_dispatch, "SETTLE_GRACE_SECONDS", _BOUND_SECONDS)

    result = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)

    assert result["ok"] is False
    assert result["reconciliation_deferred"] == reason, result
    assert result["error"] == f"finalization_retry_deferred:{reason}"
    assert result["state"] == "finalizing"
    assert result["workspace_retained"] is True
    assert not finalize_retry_dispatch.in_flight(_REQUEST_ID)
    # A deferral proves nothing about the candidate: no terminal row, no
    # manager failure callback and no planned auto-return.
    assert terminal == []
    assert outcomes == []
    assert harness.latest_event["state"] == "finalizing"


@pytest.mark.parametrize("finalizer_pid", [0, -1])
def test_a_non_positive_finalizer_pid_is_never_evidence_of_death(monkeypatch, tmp_path, finalizer_pid):
    harness = _finalize_failed_harness(monkeypatch, tmp_path)
    _append_abandoned_finalizing_row(harness)
    status_path = Path(harness.metadata_on_disk["supervisor_status_path"])
    assert process_launcher.write_finalizer_heartbeat(status_path, {
        "pid": finalizer_pid,
        "pid_start_ticks": 1,
        "heartbeat_at_epoch": time.time() - 3600.0,
        "phase": "validating",
        "total": 4,
        "elapsed_seconds": 1.0,
    })
    before = harness.latest_event

    result = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)

    assert result == {
        "ok": False,
        "request_id": _REQUEST_ID,
        "task_id": _TASK_ID,
        "error": "finalization_retry_finalizer_liveness_unknown",
    }
    assert harness.transitions == [] and harness.finalized == []
    assert harness.latest_event == before


def test_a_duplicate_naming_another_task_is_refused_not_given_the_live_receipt(monkeypatch):
    monkeypatch.setattr(finalize_retry_dispatch, "ADMISSION_WAIT_SECONDS", 0.05)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def pending_admission() -> dict:
        calls.append(_REQUEST_ID)
        entered.set()
        release.wait(_BOUND_SECONDS)
        return {"ok": False, "request_id": _REQUEST_ID, "task_id": _TASK_ID, "error": "refused_late"}

    try:
        first = finalize_retry_dispatch.dispatch(_REQUEST_ID, _TASK_ID, pending_admission)
        assert entered.wait(_BOUND_SECONDS)
        mismatched = finalize_retry_dispatch.dispatch(_REQUEST_ID, "some-other-task", pending_admission)
        duplicate = finalize_retry_dispatch.dispatch(_REQUEST_ID, _TASK_ID, pending_admission)
    finally:
        release.set()

    assert first["task_id"] == _TASK_ID and first["already_in_flight"] is False
    assert mismatched["ok"] is False
    assert mismatched["task_id"] == "some-other-task"
    assert mismatched["error"] == (
        f"finalization_retry_task_identity_mismatch:in_flight_task_id={_TASK_ID}"
    )
    assert "schema_id" not in mismatched
    assert duplicate["ok"] is True and duplicate["already_in_flight"] is True
    assert duplicate["task_id"] == _TASK_ID
    assert _wait_idle()
    assert calls == [_REQUEST_ID]
