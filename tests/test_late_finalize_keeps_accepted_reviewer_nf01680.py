"""NF-2026-01680: a late finalize event never voids an already-accepted reviewer.

Field case: a quality reviewer request was accepted (its card holds the
accepted ``quality_review`` receipt); a late retries-exhausted finalize then
found the card no longer processing and appended ``finalize_abandoned`` over
the decided tail. The accept lens resolved the reviewer from that newest row,
counted the lens unusable and forced a paid re-review.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    finalize_retry_dispatch,
    process_launcher,
    process_launcher_accept_review,
    task_store,
)

RID, TID, RUNNER = "5f0c1e9a2b7d4c3e8a6f1b2c3d4e5f60", "QR-CORRECTNESS", "reviewer"


def _manager(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "process_log.jsonl",
        process_dir=tmp_path / "proc",
        isolation_enabled=False,
    )


def _seed(mgr, *tail: dict) -> None:
    mgr._append_event({
        "request_id": RID, "task_id": TID, "runner": RUNNER,
        "topic": "quality_review", "state": "running",
    })
    for row in tail:
        mgr._append_event({"request_id": RID, "task_id": TID, "runner": RUNNER, **row})


@pytest.fixture()
def terminal_calls(monkeypatch):
    calls: list[str] = []

    def fake(repo, task_id, runner, substatus, *, evidence=None, request_id=""):
        calls.append(substatus)
        # The field case: the card already left processing.
        return {"ok": False, "stderr": "illegal_transition:archived->failed"}

    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_failure", fake)
    return calls


def _states(mgr) -> list[str]:
    return [str(event.get("state") or "") for event in mgr._request_events(RID)]


# --------------------------------------------------------------------------- #
# Reproduction: the retries-exhausted finalize path over an accepted reviewer.
# --------------------------------------------------------------------------- #

def test_reproduction_exhausted_finalize_appends_nothing_over_accepted_reviewer(
    tmp_path, monkeypatch, terminal_calls
):
    mgr = _manager(tmp_path)
    _seed(mgr, {"state": "accepted", "accepted": True})

    def crash(*_a, **_k):
        raise RuntimeError("late_finalize_boom")

    monkeypatch.setattr(mgr, "_finalize_isolated_request", crash)
    monkeypatch.setattr(process_launcher.time, "sleep", lambda _s: None)

    result = mgr._finalize_after_process_exit(RID, 0)

    assert result["skipped_decided_tail"] is True
    assert result["recorded"] is False
    assert result["state"] == "accepted"
    assert "finalize_abandoned" not in _states(mgr)
    assert _states(mgr)[-1] == "accepted"
    assert terminal_calls == []  # no card move, so no callback either


def test_reproduction_recorder_skips_a_decided_review_ready_tail(
    tmp_path, terminal_calls
):
    mgr = _manager(tmp_path)
    _seed(mgr, {"state": "review_ready"})

    result = finalize_retry_dispatch.record_finalizer_failed(
        mgr, RID, 0, {"task_id": TID, "runner": RUNNER},
        "finalizer_retries_exhausted:x", 3, time.monotonic(),
    )

    assert result["skipped_decided_tail"] is True
    assert _states(mgr)[-1] == "review_ready"
    assert terminal_calls == []


# --------------------------------------------------------------------------- #
# Regression: undecided tails still settle; the shared predicate is unchanged.
# --------------------------------------------------------------------------- #

def test_regression_undecided_tail_still_records_finalize_abandoned(
    tmp_path, terminal_calls
):
    mgr = _manager(tmp_path)
    _seed(mgr, {"state": "finalizing"})

    result = finalize_retry_dispatch.record_finalizer_failed(
        mgr, RID, 0, {"task_id": TID, "runner": RUNNER},
        "finalizer_retries_exhausted:x", 3, time.monotonic(),
    )

    assert "skipped_decided_tail" not in result
    assert terminal_calls == ["finalize_failed"]
    assert _states(mgr)[-1] == result["state"]
    assert result["state"] in {"finalize_abandoned", "reconcile_pending"}


def test_regression_exited_tail_still_moves_the_card(
    tmp_path, monkeypatch, terminal_calls
):
    # A terminal-but-not-successful tail (exited) is not a decided success:
    # the exhausted finalize must still try to move the card.
    mgr = _manager(tmp_path)
    _seed(mgr, {"state": "exited"})

    def crash(*_a, **_k):
        raise RuntimeError("late_finalize_boom")

    monkeypatch.setattr(mgr, "_finalize_isolated_request", crash)
    monkeypatch.setattr(process_launcher.time, "sleep", lambda _s: None)

    result = mgr._finalize_after_process_exit(RID, 0)

    assert terminal_calls == ["finalize_failed"]
    assert not result.get("skipped_decided_tail")

    direct = finalize_retry_dispatch.record_finalizer_failed(
        mgr, RID, 0, {"task_id": TID, "runner": RUNNER},
        "finalizer_retries_exhausted:x", 3, time.monotonic(),
    )
    assert not direct.get("skipped_decided_tail")


def test_regression_nf01800_moot_bridge_cancel_keeps_a_decided_tail(
    tmp_path, monkeypatch
):
    mgr = _manager(tmp_path)
    _seed(mgr, {"state": "accepted", "accepted": True})
    monkeypatch.setattr(process_launcher.time, "sleep", lambda _s: None)

    def refuse(*_a, **_k):
        raise RuntimeError("bridge_home_removed")

    monkeypatch.setattr(mgr, "_bridge_request_for_cancellation", refuse)
    monkeypatch.setattr(
        mgr, "_bridge_cancel_moot_home_removed", lambda *_a, **_k: "home_removed"
    )

    with pytest.raises(process_launcher._BridgeCancellationDeferred):
        mgr._publish_bridge_cancellation_before_finalization(RID)

    assert _states(mgr)[-1] == "accepted"
    assert "finalize_abandoned" not in _states(mgr)


# --------------------------------------------------------------------------- #
# Regression: the accept lens resolver reads the success, not the late row.
# --------------------------------------------------------------------------- #

def _reviewer_card(task_id: str, lens: str) -> dict:
    return {
        "task_id": task_id,
        "topic": "quality_review",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "quality_review": {
                    "lens": lens,
                    "packet_sha256": "d" * 64,
                    "target_task_id": "T-1",
                    "target_request_id": "R-1",
                },
            },
        },
    }


def _store_with_reviewers(tmp_path: Path, cards: list[dict]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        for card in cards:
            conn.execute(
                "INSERT INTO tasks (task_id, runner, topic, mode, status, "
                "worker_status, priority, objective, card_json, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    card["task_id"], "reviewer", "quality_review", "auto", "review",
                    "done", 5, "review", json.dumps(card),
                    "2026-09-08T00:00:00+00:00", "2026-09-08T00:00:00+00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return repo


class _LedgerManager:
    """The ledger reads ``reviewer_evidence`` uses; no live reviewers."""

    def __init__(self, repo: Path, ledgers: dict[str, list[dict]]) -> None:
        self.repo = repo
        self._ledgers = ledgers

    def _latest_by_request(self) -> dict[str, dict]:
        return {rid: events[-1] for rid, events in self._ledgers.items()}

    def _request_events(self, request_id: str) -> list[dict]:
        return list(self._ledgers.get(request_id, []))


def _row(task_id: str, state: str, finished_at: str, **extra) -> dict:
    return {"task_id": task_id, "state": state, "finished_at": finished_at, **extra}


@pytest.mark.parametrize("success", ["accepted", "review_ready"])
@pytest.mark.parametrize("late", ["finalize_abandoned", "finalize_failed"])
def test_regression_late_finalize_row_keeps_reviewer_usable(tmp_path, success, late):
    repo = _store_with_reviewers(tmp_path, [_reviewer_card("QR-CORRECTNESS", "correctness")])
    manager = _LedgerManager(repo, {
        "rq-correctness": [
            _row("QR-CORRECTNESS", "running", ""),
            _row("QR-CORRECTNESS", success, "2026-09-08T01:00:00+00:00", accepted=True),
            _row("QR-CORRECTNESS", late, "2026-09-08T02:00:00+00:00"),
        ],
    })

    rows = process_launcher_accept_review.reviewer_evidence(manager, "T-1", "R-1")

    assert [(r["request_id"], r["state"], r["usable"]) for r in rows] == [
        ("rq-correctness", success, True),
    ]
    assert process_launcher_accept_review.bound_reviewer_request_ids(
        manager, "T-1", "R-1"
    ) == ["rq-correctness"]


def test_regression_reviewer_that_only_ever_failed_stays_unusable(tmp_path):
    repo = _store_with_reviewers(tmp_path, [_reviewer_card("QR-CORRECTNESS", "correctness")])
    manager = _LedgerManager(repo, {
        "rq-correctness": [
            _row("QR-CORRECTNESS", "running", ""),
            _row("QR-CORRECTNESS", "finalizing", ""),
            _row("QR-CORRECTNESS", "finalize_abandoned", "2026-09-08T02:00:00+00:00"),
        ],
    })

    rows = process_launcher_accept_review.reviewer_evidence(manager, "T-1", "R-1")

    assert [(r["request_id"], r["state"], r["usable"]) for r in rows] == [
        ("rq-correctness", "finalize_abandoned", False),
    ]


def test_regression_unreadable_ledger_keeps_the_late_row_unusable(tmp_path):
    repo = _store_with_reviewers(tmp_path, [_reviewer_card("QR-CORRECTNESS", "correctness")])

    class _Unreadable(_LedgerManager):
        def _request_events(self, request_id: str) -> list[dict]:
            raise OSError("ledger_unreadable")

    manager = _Unreadable(repo, {
        "rq-correctness": [
            _row("QR-CORRECTNESS", "accepted", "2026-09-08T01:00:00+00:00", accepted=True),
            _row("QR-CORRECTNESS", "finalize_abandoned", "2026-09-08T02:00:00+00:00"),
        ],
    })

    rows = process_launcher_accept_review.reviewer_evidence(manager, "T-1", "R-1")

    assert [(r["state"], r["usable"]) for r in rows] == [("finalize_abandoned", False)]


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["running", "accepted", "finalize_abandoned"], "accepted"),
        (["running", "review_ready", "finalize_failed"], "review_ready"),
        (["running", "finalize_abandoned"], "finalize_abandoned"),
        (["accepted", "cancelled"], "cancelled"),
        (["accepted"], "accepted"),
    ],
)
def test_regression_reviewer_decided_event_used_by_accept(states, expected):
    events = [{"state": state} for state in states]
    assert (
        process_launcher_accept_review._reviewer_decided_event(events)["state"]
        == expected
    )


def test_regression_cancel_between_success_and_late_finalize_stays_unusable(tmp_path):
    repo = _store_with_reviewers(tmp_path, [_reviewer_card("QR-CORRECTNESS", "correctness")])
    ledger = [
        _row("QR-CORRECTNESS", "review_ready", "2026-09-08T01:00:00+00:00"),
        _row("QR-CORRECTNESS", "cancelled", "2026-09-08T01:30:00+00:00"),
        _row("QR-CORRECTNESS", "finalize_abandoned", "2026-09-08T02:00:00+00:00"),
    ]
    manager = _LedgerManager(repo, {"rq-correctness": ledger})

    rows = process_launcher_accept_review.reviewer_evidence(manager, "T-1", "R-1")

    assert [(r["request_id"], r["usable"]) for r in rows] == [("rq-correctness", False)]
    assert rows[0]["state"] not in {"review_ready", "accepted"}
    decided = process_launcher_accept_review._reviewer_decided_event(ledger)
    assert decided["state"] not in {"review_ready", "accepted"}
    assert decided["state"] == "finalize_abandoned"


def test_regression_decided_row_supplies_state_and_finished_at(tmp_path):
    repo = _store_with_reviewers(tmp_path, [_reviewer_card("QR-CORRECTNESS", "correctness")])
    manager = _LedgerManager(repo, {
        "rq-correctness": [
            _row("QR-CORRECTNESS", "accepted", "2026-09-08T01:00:00+00:00", accepted=True),
            _row("QR-CORRECTNESS", "finalize_abandoned", "2026-09-08T02:00:00+00:00"),
        ],
    })

    rows = process_launcher_accept_review.reviewer_evidence(manager, "T-1", "R-1")

    assert [(r["state"], r["usable"]) for r in rows] == [("accepted", True)]

    # finished_at comes from the decided row (amendment item 3): an older
    # success whose late finalize row is newer must not outrank a genuinely
    # newer success of the same reviewer task.
    manager = _LedgerManager(repo, {
        "rq-old": [
            _row("QR-CORRECTNESS", "accepted", "2026-09-08T01:00:00+00:00", accepted=True),
            _row("QR-CORRECTNESS", "finalize_abandoned", "2026-09-08T05:00:00+00:00"),
        ],
        "rq-new": [
            _row("QR-CORRECTNESS", "accepted", "2026-09-08T03:00:00+00:00", accepted=True),
        ],
    })
    rows = process_launcher_accept_review.reviewer_evidence(manager, "T-1", "R-1")
    assert [(r["request_id"], r["usable"]) for r in rows] == [("rq-new", True)]
