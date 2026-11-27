"""NF-2026-00856: retry a locked review transition instead of discarding a
completed worker run.

(a) locked twice then ok -> one ok result after 3 calls.
(b) a non-lock failure -> exactly 1 call, result returned unchanged.
(c) budget exhausted -> the last failed result is returned, attempts bounded
    (injected fake clock and sleep).
(d) the real process_launcher finalize sites, driven through a stub
    ``_review_terminal_exact`` that fails ``database is locked`` once and then
    succeeds, settle on ``review_ready`` without a provider relaunch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import test_terminal_collect_truth as _truth
from aiworkhub import process_launcher as pl
from aiworkhub import review_transition_retry as rtr

_LOCKED = {"ok": False, "stderr": "sqlite3.OperationalError: database is locked"}


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


def _scripted(results: list[dict[str, Any]]) -> tuple[list[int], Any]:
    calls: list[int] = []

    def call() -> dict[str, Any]:
        calls.append(1)
        return results[min(len(calls), len(results)) - 1]

    return calls, call


def test_a_locked_twice_then_ok_returns_ok_after_three_calls() -> None:
    fake = _FakeClock()
    ok = {"ok": True, "idempotent_noop": True}
    calls, call = _scripted(
        [_LOCKED, {"ok": False, "stdout": "Database Is Busy"}, ok],
    )

    result = rtr.retry_transient_transition(call, sleep=fake.sleep, clock=fake.clock)

    assert result is ok
    assert len(calls) == 3
    assert len(fake.sleeps) == 2


def test_b_non_lock_failure_is_not_retried_and_returned_unchanged() -> None:
    fake = _FakeClock()
    failure = {"ok": False, "stderr": "db_write_conflict:planned_maintenance"}
    calls, call = _scripted([failure, {"ok": True}])

    result = rtr.retry_transient_transition(call, sleep=fake.sleep, clock=fake.clock)

    assert result is failure
    assert result == {"ok": False, "stderr": "db_write_conflict:planned_maintenance"}
    assert len(calls) == 1
    assert fake.sleeps == []


def test_b_success_is_returned_at_once() -> None:
    fake = _FakeClock()
    calls, call = _scripted([{"ok": True}])

    assert rtr.retry_transient_transition(call, sleep=fake.sleep, clock=fake.clock) == {"ok": True}
    assert len(calls) == 1


def test_c_budget_exhausted_returns_last_failure_with_bounded_attempts() -> None:
    fake = _FakeClock()
    failures = [
        {"ok": False, "stderr": f"database is locked (attempt {index})"} for index in range(500)
    ]
    calls, call = _scripted(failures)
    budget_s = 5.0

    result = rtr.retry_transient_transition(
        call, sleep=fake.sleep, clock=fake.clock, budget_s=budget_s, rng=lambda: 1.0,
    )

    assert result is failures[len(calls) - 1]
    assert result["ok"] is False
    # Bounded by the wall-clock budget, never by running the script dry.
    assert 2 <= len(calls) < 50
    assert sum(fake.sleeps) <= budget_s + 1e-9
    assert fake.now - 1000.0 <= budget_s + 1e-9
    assert all(delay <= rtr.MAX_DELAY_S for delay in fake.sleeps)


def test_c_zero_budget_never_retries() -> None:
    fake = _FakeClock()
    calls, call = _scripted([_LOCKED, {"ok": True}])

    result = rtr.retry_transient_transition(
        call, sleep=fake.sleep, clock=fake.clock, budget_s=0.0,
    )

    assert result is _LOCKED
    assert len(calls) == 1


def test_c_stuck_clock_is_still_bounded() -> None:
    calls, call = _scripted([_LOCKED])

    result = rtr.retry_transient_transition(
        call, sleep=lambda _s: None, clock=lambda: 0.0, budget_s=10.0,
    )

    assert result is _LOCKED
    assert len(calls) == rtr.MAX_ATTEMPTS_CEILING


def _locked_once_then_ok(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def review_terminal_exact(self: Any, _metadata: Any, state: str, **_kwargs: Any) -> dict:
        calls.append(state)
        if len(calls) == 1:
            return dict(_LOCKED)
        return {"ok": True}

    monkeypatch.setattr(pl.ProcessManager, "_review_terminal_exact", review_terminal_exact)
    return calls


def _running_event(manager: Any, request_id: str, task_id: str, metadata_path: Path,
                   status_path: Path) -> None:
    manager._append_event(  # noqa: SLF001 - exact finalizer-lineage regression
        {
            "request_id": request_id,
            "task_id": task_id,
            "runner": "worker",
            "topic": "truth",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "running",
            "pid": 999999999,
            "pid_start_ticks": 1,
            "stdout_path": str(manager.process_dir / f"{request_id}.stdout.log"),
            "stderr_path": str(manager.process_dir / f"{request_id}.stderr.log"),
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
        }
    )


def _common_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pl, "_requires_bridge_cancellation", lambda _metadata: False)
    monkeypatch.setattr(pl, "_provider_auth_failure_from_output", lambda _path: None)
    monkeypatch.setattr(pl.ProcessManager, "_exact_claim_state", lambda self, *_a, **_k: "processing")
    monkeypatch.setattr(pl.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(
        pl.ProcessManager, "_canonical_outcome_evidence", lambda self, *_a, **_k: {},
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_record_usage",
        lambda self, *_a, **_k: ({}, False, "usage_not_under_test"),
    )
    monkeypatch.setattr(
        pl.ProcessManager, "_persist_attempt_artifacts",
        lambda self, *_a, **_k: None,
    )


def test_d_quality_review_finalize_retries_locked_transition_to_review_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    task_id = "NF00856_QUALITY_REVIEW_LOCKED_ONCE"
    request_id = "ee" * 16
    manager, metadata_path, status_path = _truth._isolated_finalizer_manager_and_metadata(  # noqa: SLF001
        tmp_path,
        task_id=task_id,
        request_id=request_id,
        supervisor_status={"state": "exited", "exit_code": 0, "error": ""},
        extra_metadata={
            "quality_review": {"packet_path": str(tmp_path / "unused_packet.json")},
        },
    )
    _running_event(manager, request_id, task_id, metadata_path, status_path)
    _common_stubs(monkeypatch)
    monkeypatch.setattr(pl, "enforce_scope", lambda *_a, **_k: [])
    monkeypatch.setattr(
        pl, "_verified_quality_review_receipt", lambda *_a, **_k: {"reviewer": {}, "report": {}},
    )
    calls = _locked_once_then_ok(monkeypatch)

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert calls == ["review_ready", "review_ready"]
    assert event["state"] == "review_ready"
    assert event["exit_code"] == 0
    assert event["failure_kind"] is None
    assert "review_transition_failed" not in str(event.get("error") or "")


def test_d_worker_candidate_finalize_retries_locked_transition_to_review_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    task_id = "NF00856_CANDIDATE_LOCKED_ONCE"
    request_id = "ff" * 16
    manager, metadata_path, status_path = _truth._isolated_finalizer_manager_and_metadata(  # noqa: SLF001
        tmp_path,
        task_id=task_id,
        request_id=request_id,
        supervisor_status={"state": "exited", "exit_code": 0, "error": ""},
    )
    candidate = tmp_path / "repo/worktree/src/app.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("VALUE = 1\n", encoding="utf-8")
    _running_event(manager, request_id, task_id, metadata_path, status_path)
    _common_stubs(monkeypatch)
    monkeypatch.setattr(pl, "enforce_scope", lambda *_a, **_k: ["src/app.py"])
    monkeypatch.setattr(pl, "validate_residual_contract", lambda *_a, **_k: [])
    monkeypatch.setattr(pl, "validate_required_outputs", lambda *_a, **_k: [])
    monkeypatch.setattr(pl, "_worker_mcp_live_call_gate", lambda *_a, **_k: {"gated": False})
    monkeypatch.setattr(pl, "_run_full_snapshot_validations", lambda *_a, **_k: ([], None))
    monkeypatch.setattr(pl, "_enforce_behavioral_gate", lambda *_a, **_k: None)
    monkeypatch.setattr(pl, "_changed_path_hashes", lambda *_a, **_k: {})
    monkeypatch.setattr(
        pl.quality_evidence, "run_completion_quality_gate",
        lambda *_a, **_k: {"passed": True, "blocking_checks": []},
    )
    monkeypatch.setattr(
        "aiworkhub.successful_rework_recovery.successful_candidate_evidence",
        lambda _workspace, _metadata, _request_id, changed: (
            sorted(changed), {path: "0" * 64 for path in changed}, {},
        ),
    )
    calls = _locked_once_then_ok(monkeypatch)

    event = manager._finalize_isolated_request(request_id)  # noqa: SLF001

    assert calls == ["review_ready", "review_ready"]
    assert event["state"] == "review_ready"
    assert event["exit_code"] == 0
    assert event["failure_kind"] is None
    assert "review_transition_failed" not in str(event.get("error") or "")
