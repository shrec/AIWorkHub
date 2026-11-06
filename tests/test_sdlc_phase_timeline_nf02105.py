"""NF-2026-02105: per-card phase timeline in sdlc_outcome_metrics."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aiworkhub import sdlc_outcome_metrics, sdlc_phase_timeline, task_engine, task_store

_REPO = "repo-one"
_T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _cold_ledger_scans(monkeypatch):
    monkeypatch.setattr(sdlc_outcome_metrics, "_segment_cache_path", lambda _log: None)


def _at(seconds: float) -> str:
    return (_T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _receipt(task_id: str, request_id: str) -> dict:
    hashes = {"fixed.txt": "a" * 64}
    unsigned = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": 1,
        "base_oid": "base",
        "promoted_paths": ["fixed.txt"],
        "changed_path_hashes": hashes,
        "attempt_artifact_manifest_id": "a" * 64,
        "repository_revision": "sha256:" + task_engine._canonical_json_hash({
            "base_oid": "base", "changed_path_hashes": hashes
        }),
    }
    return {**unsigned, "receipt_id": "sha256:" + task_engine._canonical_json_hash(unsigned)}


def _timeline_card(
    *, queue, worker, finalization, reviewers, manager_wait, accept, declared,
    neighbour=None, rerun=None, provisioning_ms=None, workspace_ms=None,
    manager_ready=True, reused_row=False,
):
    """Columns plus a card whose recorded timestamps give exactly the named phase durations."""

    claimed = queue
    worker_done = claimed + worker
    review_ready = worker_done + finalization
    last_reviewer = review_ready + reviewers
    manager_ready_at = last_reviewer + 10
    accept_started = manager_ready_at + manager_wait
    rows = [{"command": "pytest -q", "duration_seconds": declared}]
    if reused_row:
        rows.append({"command": "ruff", "duration_seconds": 999, "reused_from": "worker"})
    snapshot: dict = {"schema_id": "aiworkhub.full_validation_snapshot.v1"}
    if neighbour is not None:
        snapshot["neighbour_tests"] = {"duration_seconds": neighbour, "passed": True}
        if rerun is not None:
            snapshot["neighbour_tests"]["neighbour_flaky_rerun"] = {"duration_seconds": rerun}
    evidence: dict = {
        "finished_at": _at(worker_done),
        "validations": rows,
        "full_validation_snapshot": snapshot,
        "workspace": {"base_oid": "base"},
    }
    if workspace_ms is not None:
        evidence["workspace"]["provisioning_timings_ms"] = {"total": workspace_ms}
    if provisioning_ms is not None:
        evidence["provisioning_timings_ms"] = {"total": provisioning_ms, "seed_closure": 1.0}
    card = {
        "terminal_review": {"recorded_at": _at(review_ready), "evidence": evidence},
        "reviewer_receipts": [
            {"lens": "previous_round", "recorded_at": _at(review_ready - 5000)},
            {"lens": "correctness", "recorded_at": _at(review_ready + reviewers / 2)},
            {"lens": "security", "recorded_at": _at(last_reviewer)},
        ],
        "accept_evidence": {"started_at": _at(accept_started)},
        "accepted_at": _at(accept_started + accept),
    }
    if manager_ready:
        card["manager_ready_at"] = _at(manager_ready_at)
    return {
        "created_at": _at(0), "claimed_at": _at(claimed), "started_at": _at(claimed),
        "card": card,
    }


# A: 450 s validation (100 declared, 250+50 neighbour incl. flaky rerun, 50 provisioning).
# B: no neighbours selected, so neighbour time is a measured 0 rather than unknown.
# C: the NF-2026-02089-c1 shape (1821 s finalization) with manager_ready_at never recorded.
_CARDS = {
    "TASK-A": _timeline_card(
        queue=60, worker=600, finalization=1000, reviewers=900, manager_wait=120, accept=30,
        declared=100, neighbour=250, rerun=50, workspace_ms=50_000, reused_row=True,
    ),
    "TASK-B": _timeline_card(
        queue=120, worker=300, finalization=100, reviewers=600, manager_wait=240, accept=60,
        declared=50, provisioning_ms=20_000,
    ),
    "TASK-C": _timeline_card(
        queue=30, worker=1200, finalization=1821, reviewers=1200, manager_wait=300, accept=90,
        declared=200, neighbour=600, workspace_ms=30_600, manager_ready=False,
    ),
}


def _seed(root, cards):
    root.mkdir()
    db = root / "tasks.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(task_store.SCHEMA)
        task_store.ensure_event_indexes(conn)
        for index, (task_id, row) in enumerate(sorted(cards.items()), start=1):
            card = row["card"]
            conn.execute(
                "INSERT INTO tasks (task_id, card_json, created_at, updated_at, claimed_at, "
                "started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    card if isinstance(card, str) else json.dumps(card),
                    row["created_at"], row["created_at"], row["claimed_at"], row["started_at"],
                ),
            )
            payload = {
                "request_id": f"R-{task_id}",
                "accepted_outcome_receipt": _receipt(task_id, f"R-{task_id}"),
            }
            conn.execute(
                "INSERT INTO task_events(event_id, task_id, event, payload_json, created_at) "
                "VALUES (?, ?, 'accept_review', ?, ?)",
                (index, task_id, json.dumps(payload), _at(10_000 + index)),
            )
        conn.commit()
    finally:
        conn.close()
    return db


def _metrics(tmp_path, monkeypatch, cards, name="repo"):
    root = tmp_path / name
    db = _seed(root, cards)
    monkeypatch.setattr(
        task_store, "storage_readiness",
        lambda _root: SimpleNamespace(ready=True, reason="", canonical_db=db),
    )
    return sdlc_outcome_metrics.read_repository_metrics(root, repository_id=_REPO, limit=100)


def test_derive_card_phases_splits_seeded_timestamps_into_exact_durations():
    row = _CARDS["TASK-C"]
    evidence = row["card"]["terminal_review"]["evidence"]
    record = {
        "created_at": row["created_at"], "claimed_at": row["claimed_at"],
        "started_at": row["started_at"], "card_failure": None,
        "review_ready_at": row["card"]["terminal_review"]["recorded_at"],
        "worker_done_at": evidence["finished_at"],
        "validations": evidence["validations"],
        "snapshot_schema": evidence["full_validation_snapshot"]["schema_id"],
        "neighbour_present": True,
        "neighbour_seconds": 600,
        "workspace_provisioning_ms": 30_600,
        "reviewer_receipts": row["card"]["reviewer_receipts"],
        "accept_started_at": row["card"]["accept_evidence"]["started_at"],
        "accepted_at": row["card"]["accepted_at"],
    }
    derived = sdlc_phase_timeline.derive_card_phases(record)
    assert derived["phases"] == {
        "queue": 30.0, "worker": 1200.0, "validation": 830.6, "finalize_other": 990.4,
        "reviewers": 1200.0, "manager_wait": None, "accept": 90.0,
    }
    assert derived["validation_parts"] == {
        "declared": 200.0, "neighbour": 600.0, "provisioning": 30.6,
    }
    # A missing manager_ready_at is UNKNOWN with its cause, never a zero wait.
    assert derived["causes"] == {"manager_wait": "manager_ready_at_missing"}
    assert derived["total_seconds"] == 30 + 1200 + 1821 + 1200 + 10 + 300 + 90


def test_repository_metrics_carry_phase_median_p90_and_bounded_slowest_cards(
    tmp_path, monkeypatch
):
    result = _metrics(tmp_path, monkeypatch, _CARDS)
    timeline = result["phase_timeline"]
    assert timeline["schema_id"] == sdlc_phase_timeline.SCHEMA_ID
    assert (timeline["available"], timeline["unknown_reason"], timeline["cards"]) == (
        True, None, 3,
    )
    summary = {
        name: (stats["known"], stats["unknown"], stats["median_seconds"], stats["p90_seconds"])
        for name, stats in timeline["phases"].items()
    }
    assert summary == {
        "queue": (3, 0, 60.0, 120.0),
        "worker": (3, 0, 600.0, 1200.0),
        "validation": (3, 0, 450.0, 830.6),
        "finalize_other": (3, 0, 550.0, 990.4),
        "reviewers": (3, 0, 900.0, 1200.0),
        "manager_wait": (2, 1, 180.0, 240.0),
        "accept": (3, 0, 60.0, 90.0),
    }
    assert timeline["phases"]["manager_wait"]["unknown_causes"] == {
        "manager_ready_at_missing": 1,
    }
    assert timeline["unknowns"] == {"cards_with_unknown_phase": 1, "phase_values_unknown": 1}
    slowest = timeline["slowest_cards"]
    assert [entry["task_id"] for entry in slowest] == ["TASK-C", "TASK-A", "TASK-B"]
    assert [entry["total_seconds"] for entry in slowest] == [4651.0, 2720.0, 1430.0]
    assert slowest[0]["phases"]["manager_wait"] == sdlc_phase_timeline.UNKNOWN
    assert slowest[0]["phases"]["accept"] == 90.0
    assert slowest[1]["validation_parts"] == {
        "declared": 100.0, "neighbour": 300.0, "provisioning": 50.0,
    }
    assert slowest[2]["validation_parts"]["neighbour"] == 0.0
    assert (timeline["slowest_truncated"], timeline["unranked_total_unknown"]) == (False, 0)
    # The existing outcome metrics are unchanged by the new section.
    assert result["first_pass_acceptance"]["denominator"] == 3


def test_missing_validation_timing_is_unknown_and_never_folded_into_finalize_other(
    tmp_path, monkeypatch
):
    row = _timeline_card(
        queue=10, worker=20, finalization=30, reviewers=40, manager_wait=50, accept=60,
        declared=5, workspace_ms=1_000,
    )
    del row["card"]["terminal_review"]["evidence"]["full_validation_snapshot"]
    del row["card"]["reviewer_receipts"]
    timeline = _metrics(tmp_path, monkeypatch, {"TASK-D": row})["phase_timeline"]
    phases = timeline["phases"]
    assert phases["validation"]["unknown_causes"] == {"neighbour_timing_missing": 1}
    assert phases["finalize_other"]["unknown_causes"] == {"validation_unknown": 1}
    assert phases["reviewers"]["unknown_causes"] == {"last_reviewer_at_missing": 1}
    for name in ("validation", "finalize_other", "reviewers"):
        assert (phases[name]["known"], phases[name]["median_seconds"]) == (0, None)
    assert timeline["slowest_cards"][0]["validation_parts"]["neighbour"] == "UNKNOWN"
    assert phases["worker"]["median_seconds"] == 20.0


def test_reviewer_receipts_also_come_from_reviewer_attempt_rows():
    record = {
        "created_at": _at(0), "claimed_at": _at(1), "started_at": _at(1),
        "card_failure": None, "review_ready_at": _at(100),
    }
    evidence = sdlc_phase_timeline.PhaseEvidence({"T": record}, available=True)
    rows = [
        {"task_id": "T", "timestamp": _at(160), "usage": {"role": "reviewer"}},
        {"task_id": "T", "timestamp": _at(400), "usage": {"role": "worker"}},
        {"task_id": "T", "timestamp": _at(50), "usage": {"role": "reviewer"}},
    ]
    summary = sdlc_phase_timeline.summarize(evidence, ["T"], rows)
    assert summary["phases"]["reviewers"]["median_seconds"] == 60.0


def test_unreadable_store_and_malformed_cards_fail_closed_as_unknown(tmp_path, monkeypatch):
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()
    conn = sdlc_outcome_metrics.connect_readonly(bare)
    try:
        evidence = sdlc_phase_timeline.read_phase_evidence(conn, ["T"], chunk=2)
    finally:
        conn.close()
    assert evidence == sdlc_phase_timeline.PhaseEvidence(failure="store_error")
    unread = sdlc_phase_timeline.summarize(evidence, ["T", "U"])
    assert (unread["available"], unread["unknown_reason"]) == (False, "store_error")
    for stats in unread["phases"].values():
        assert (stats["known"], stats["unknown"], stats["median_seconds"]) == (0, 2, None)
        assert stats["unknown_causes"] == {"store_error": 2}
    assert unread["slowest_cards"] == [] and unread["unranked_total_unknown"] == 2

    malformed = dict(_CARDS["TASK-A"], card="{not json")
    timeline = _metrics(tmp_path, monkeypatch, {"TASK-A": malformed})["phase_timeline"]
    assert timeline["phases"]["queue"]["median_seconds"] == 60.0
    assert timeline["phases"]["accept"]["unknown_causes"] == {"card_malformed": 1}


def test_chunked_read_and_slowest_bound():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(task_store.SCHEMA)
        for index in range(5):
            row = _timeline_card(
                queue=index + 1, worker=10, finalization=10, reviewers=10, manager_wait=10,
                accept=10, declared=1, workspace_ms=1_000,
            )
            conn.execute(
                "INSERT INTO tasks (task_id, card_json, created_at, updated_at, claimed_at, "
                "started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (f"T{index}", json.dumps(row["card"]), row["created_at"], row["created_at"],
                 row["claimed_at"], row["started_at"]),
            )
        ids = [f"T{index}" for index in range(5)] + ["ABSENT"]
        evidence = sdlc_phase_timeline.read_phase_evidence(conn, ids, chunk=2)
    finally:
        conn.close()
    assert evidence.available and sorted(evidence.records) == ids[:5]
    summary = sdlc_phase_timeline.summarize(evidence, ids, slowest_limit=2)
    assert [entry["task_id"] for entry in summary["slowest_cards"]] == ["T4", "T3"]
    assert summary["slowest_truncated"] is True
    assert summary["phases"]["queue"]["unknown_causes"] == {"task_card_missing": 1}
    assert summary["unranked_total_unknown"] == 1


def test_aggregate_without_phase_evidence_reports_unknown_not_zero():
    result = sdlc_outcome_metrics.aggregate([], [], repository_id=_REPO)
    timeline = result["phase_timeline"]
    assert (timeline["available"], timeline["unknown_reason"]) == (
        False, "phase_evidence_unread",
    )
    assert timeline["cards"] == 0
