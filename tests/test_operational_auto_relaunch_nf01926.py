"""NF-2026-01926: blocked cards with a known operational failure relaunch unattended.

The reconciler retries a blocked work card through ``core.retry_terminal_task``
with ``automatic=True`` and relaunches it through ``mgr.launch`` when
``stale_recovery._classify_blocked`` recommends ``relaunch``, the substatus is
in the automatic subset and ``core.automatic_terminal_retry_refusal`` is empty
-- at most ``OPERATIONAL_RETRY_BUDGET`` times per task per 24 h, counted from
durable ``retry_terminal`` events.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import core, task_reconciler, task_store


RUNNER = "worker_runner"
TOPIC = "operational_retry"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token_path = tmp_path / "coordinator.token"
    token_path.write_text("coordinator-token\n", encoding="utf-8")
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coordinator-token")
    return root


class _Mgr:
    def __init__(self, repo: Path, *, launch_error: Exception | None = None) -> None:
        self.repo = repo
        self.launches: list[dict[str, Any]] = []
        self.launch_error = launch_error

    def status(self, _request_id: str) -> dict[str, Any]:
        return {"ok": False, "state": "not_found"}

    def launch(self, **kwargs: Any) -> dict[str, Any]:
        self.launches.append(kwargs)
        if self.launch_error is not None:
            raise self.launch_error
        return {"ok": True, "request_id": f"relaunch-{len(self.launches)}"}


def _block(
    repo: Path,
    *,
    task_id: str,
    request_id: str,
    substatus: str = "process_lost",
    failure_class: str = "",
    topic: str = TOPIC,
) -> None:
    """Write (or rewrite) one blocked operational episode exactly as the launcher records it."""
    now = "2026-11-01T00:00:00+00:00"
    card = {
        "task_id": task_id,
        "runner": RUNNER,
        "topic": topic,
        "objective": "operational retry",
        "status": "blocked",
        "worker_status": substatus,
        "claimed_by": RUNNER,
        "claim_epoch": 3,
        "launch_request_id": request_id,
        "terminal_substatus": substatus,
        "terminal_outcome": substatus,
        "blocker_reason": f"{substatus}:exact",
        "blocked_at": now,
        "blocked_by": RUNNER,
        "terminal_failure": {
            "substatus": substatus,
            "evidence": {
                "request_id": request_id,
                "error": f"{substatus}:exact",
                **({"failure_class": failure_class} if failure_class else {}),
            },
        },
    }
    existing = task_store.get_task(repo, task_id)
    if existing and isinstance(existing.get("terminal_retry"), dict):
        card["terminal_retry"] = existing["terminal_retry"]
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tasks "
            "(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,claimed_by,claimed_at,started_at,completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, RUNNER, topic, "solo", "blocked", substatus, "normal",
                "operational retry", json.dumps(card, sort_keys=True), now, now,
                RUNNER, now, now, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _events(repo: Path, task_id: str, event: str) -> list[dict[str, Any]]:
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        rows = conn.execute(
            "SELECT runner, payload_json FROM task_events WHERE task_id=? AND event=? ORDER BY event_id",
            (task_id, event),
        ).fetchall()
    finally:
        conn.close()
    return [{"runner": runner, **json.loads(payload or "{}")} for runner, payload in rows]


def _card(repo: Path, task_id: str) -> dict[str, Any]:
    card = task_store.get_task(repo, task_id)
    assert card is not None
    return card


def _scan(mgr: _Mgr) -> dict[str, Any]:
    result = task_reconciler._scan_operational_retry_relaunch(mgr)  # noqa: SLF001
    assert result["ok"] is True
    return result


# (a) ---------------------------------------------------------------------------


def test_process_lost_without_class_is_retried_and_relaunched_unattended(repo: Path) -> None:
    _block(repo, task_id="TASK_OP_A", request_id="req-a-1")
    mgr = _Mgr(repo)

    result = _scan(mgr)

    assert result["relaunched"] == 1
    assert mgr.launches == [{"task_id": "TASK_OP_A", "runner": RUNNER, "topic": TOPIC}]
    card = _card(repo, "TASK_OP_A")
    assert task_store.canonical_status(card) == "pending"
    retry = card["terminal_retry"]
    assert retry["request_id"] == "req-a-1"
    assert retry["retry_of"] == "req-a-1"
    assert retry["terminal_substatus"] == "process_lost"
    assert retry["failure_class"] is None
    assert retry["actor"] == task_reconciler.OPERATIONAL_RETRY_ACTOR == "reconciler:auto"
    assert retry["automatic"] is True
    retry_events = _events(repo, "TASK_OP_A", "retry_terminal")
    assert len(retry_events) == 1 and retry_events[0]["actor"] == "reconciler:auto"
    informational = _events(repo, "TASK_OP_A", task_reconciler.OPERATIONAL_RETRY_EVENT)
    assert len(informational) == 1
    assert informational[0]["informational"] is True
    assert informational[0]["retry_of"] == "req-a-1"
    assert informational[0]["state"] == "relaunched"
    # Informational only: a kind distinct from every manager callback transition.
    assert task_reconciler.OPERATIONAL_RETRY_EVENT not in core.callback_store.CALLBACK_ELIGIBLE_TRANSITIONS


def test_run_scan_records_the_phase_and_its_timing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _ScanMgr(_Mgr):
        def reconcile(self, include_gc: bool = True) -> dict[str, Any]:
            return {"reconciled": []}

    for name in (
        "_scan_review_ready_recovery", "_scan_transient_retry_relaunch", "_scan_orphan_claims",
    ):
        monkeypatch.setattr(task_reconciler, name, lambda _mgr: {"ok": True, "state": "stub"})
    monkeypatch.setattr(task_reconciler, "_scan_wave_goal_bindings", lambda _repo: {"state": "stub"})
    monkeypatch.setattr(task_reconciler, "_scan_wave_completion", lambda _repo: {"state": "stub"})
    _block(repo, task_id="TASK_OP_SCAN", request_id="req-scan-1")

    result = task_reconciler.run_scan(_ScanMgr(repo), include_gc=False)

    assert result["operational_retry_relaunch"]["relaunched"] == 1
    assert "operational_retry_relaunch" in result["phase_durations_ms"]


# (b) ---------------------------------------------------------------------------


def test_third_failure_inside_the_window_stays_blocked_for_the_manager(repo: Path) -> None:
    mgr = _Mgr(repo)
    for attempt in (1, 2):
        _block(repo, task_id="TASK_OP_B", request_id=f"req-b-{attempt}")
        assert _scan(mgr)["relaunched"] == 1

    _block(repo, task_id="TASK_OP_B", request_id="req-b-3")
    before = _card(repo, "TASK_OP_B")
    result = _scan(mgr)

    assert result["relaunched"] == 0
    assert result["by_state"] == {"budget_exhausted": 1}
    assert len(mgr.launches) == 2
    assert _card(repo, "TASK_OP_B") == before
    assert task_store.canonical_status(before) == "blocked"
    assert len(_events(repo, "TASK_OP_B", "retry_terminal")) == 2
    assert len(_events(repo, "TASK_OP_B", task_reconciler.OPERATIONAL_RETRY_EVENT)) == 2


def test_budget_survives_a_restart_because_it_is_read_from_durable_events(repo: Path) -> None:
    for attempt in (1, 2):
        _block(repo, task_id="TASK_OP_R", request_id=f"req-r-{attempt}")
        assert _scan(_Mgr(repo))["relaunched"] == 1  # a fresh manager each pass
    _block(repo, task_id="TASK_OP_R", request_id="req-r-3")

    assert _scan(_Mgr(repo))["by_state"] == {"budget_exhausted": 1}


# (c) ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure_class", ["credential", "defect", "unknown", "transient", "not_a_placed_class"])
def test_refused_classes_stay_blocked_untouched(repo: Path, failure_class: str) -> None:
    _block(repo, task_id="TASK_OP_C", request_id="req-c-1", failure_class=failure_class)
    before = _card(repo, "TASK_OP_C")
    mgr = _Mgr(repo)

    result = _scan(mgr)

    assert result["by_state"] == {"refused": 1}
    assert result["attempted"] == 0
    assert mgr.launches == []
    assert _card(repo, "TASK_OP_C") == before
    assert _events(repo, "TASK_OP_C", "retry_terminal") == []
    assert _events(repo, "TASK_OP_C", task_reconciler.OPERATIONAL_RETRY_EVENT) == []


@pytest.mark.parametrize("substatus", ["cancelled", "timed_out", "validation_failed"])
def test_substatuses_outside_the_automatic_subset_are_untouched(repo: Path, substatus: str) -> None:
    _block(repo, task_id="TASK_OP_S", request_id="req-s-1", substatus=substatus)
    before = _card(repo, "TASK_OP_S")
    mgr = _Mgr(repo)

    result = _scan(mgr)

    assert result["attempted"] == 0
    assert mgr.launches == []
    assert _card(repo, "TASK_OP_S") == before


def test_worker_failed_qualifies_only_for_an_incomplete_supervisor(repo: Path) -> None:
    _block(repo, task_id="TASK_OP_W", request_id="req-w-1", substatus="worker_failed")
    mgr = _Mgr(repo)
    assert _scan(mgr)["by_state"] == {"skipped": 1}
    assert mgr.launches == []


# (d) ---------------------------------------------------------------------------


def test_quality_reviewer_cards_are_never_touched(repo: Path) -> None:
    _block(repo, task_id="QUALITY_REVIEW_TASK_X", request_id="req-q-1", topic="quality_review")
    before = _card(repo, "QUALITY_REVIEW_TASK_X")
    mgr = _Mgr(repo)

    result = _scan(mgr)

    assert result["attempted"] == 0
    assert mgr.launches == []
    assert _card(repo, "QUALITY_REVIEW_TASK_X") == before
    assert _events(repo, "QUALITY_REVIEW_TASK_X", "retry_terminal") == []


# (e) ---------------------------------------------------------------------------


def test_retry_error_leaves_the_card_blocked_and_is_reported(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block(repo, task_id="TASK_OP_E", request_id="req-e-1")
    before = _card(repo, "TASK_OP_E")
    monkeypatch.setattr(
        core, "retry_terminal_task", lambda *a, **k: {"ok": False, "stderr": "terminal_retry_boom"}
    )
    mgr = _Mgr(repo)

    result = _scan(mgr)

    assert result["errors"] == [
        {"task_id": "TASK_OP_E", "state": "retry_failed", "error": "terminal_retry_boom"}
    ]
    assert mgr.launches == []
    assert _card(repo, "TASK_OP_E") == before


def test_retry_raise_leaves_the_card_blocked_and_is_reported(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block(repo, task_id="TASK_OP_E2", request_id="req-e2-1")

    def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(core, "retry_terminal_task", _boom)

    result = _scan(_Mgr(repo))

    assert result["errors"][0]["state"] == "retry_failed"
    assert "retry_raised:RuntimeError" in result["errors"][0]["error"]
    assert task_store.canonical_status(_card(repo, "TASK_OP_E2")) == "blocked"


def test_launch_error_reblocks_the_card_and_is_reported(repo: Path) -> None:
    _block(repo, task_id="TASK_OP_L", request_id="req-l-1")
    mgr = _Mgr(repo, launch_error=RuntimeError("spawn"))

    result = _scan(mgr)

    assert len(mgr.launches) == 1
    assert result["errors"] == [
        {"task_id": "TASK_OP_L", "state": "launch_failed", "error": "launch_raised:RuntimeError"}
    ]
    assert result["results"][0]["reblock"] == "reblocked"
    card = _card(repo, "TASK_OP_L")
    assert task_store.canonical_status(card) == "blocked"
    assert card["terminal_substatus"] == "launch_failed"
    informational = _events(repo, "TASK_OP_L", task_reconciler.OPERATIONAL_RETRY_EVENT)
    assert [event["state"] for event in informational] == ["launch_failed"]


def test_unreadable_store_is_a_skipped_pass_not_a_raise(tmp_path: Path) -> None:
    result = task_reconciler._scan_operational_retry_relaunch(_Mgr(tmp_path / "missing"))  # noqa: SLF001
    assert result["ok"] is False and result["state"] == "skipped"
