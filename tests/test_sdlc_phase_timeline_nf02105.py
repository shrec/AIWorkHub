"""NF-2026-02105: per-card phase timeline in sdlc_outcome_metrics."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aiworkhub import (
    review_lifecycle,
    review_orchestrator,
    sdlc_outcome_metrics,
    sdlc_phase_timeline,
    task_engine,
    task_store,
)

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
    *, task_id, queue, worker, finalization, reviewers, manager_wait, accept, declared,
    neighbour=None, rerun=None, provisioning_ms=None, workspace_ms=None,
    manager_ready=True, ledger=True, reviewer_join=True, snapshot=True,
):
    """The real stored shape: compacted validation, a quality_gate snapshot, a ledger row.

    ``evidence.validation`` is compaction's identity stand-in, the per-command list and a
    card-side ``finished_at`` do not exist. This fixture omits ``accept_evidence.started_at``,
    so ``manager_wait`` and ``accept`` stay typed unknowns here.
    """

    claimed = queue
    worker_done = claimed + worker
    review_ready = worker_done + finalization
    last_reviewer = review_ready + reviewers
    manager_ready_at = last_reviewer + 10
    accepted_at = manager_ready_at + manager_wait + accept
    wall = declared + (neighbour or 0) + (rerun or 0)
    evidence: dict = {
        "validation": {
            "summarized": True, "bytes": 90_000, "sha256": "b" * 64,
            "durable_attempt_id": f"R-{task_id}", "durable_manifest_sha256": "c" * 64,
        },
        "workspace": {"base_oid": "base"},
    }
    if snapshot:
        full: dict = {"schema_id": "aiworkhub.full_validation_snapshot.v1"}
        if neighbour is not None:
            full["neighbour_tests"] = {"duration_seconds": neighbour, "passed": True}
            if rerun is not None:
                full["neighbour_tests"]["neighbour_flaky_rerun"] = {"duration_seconds": rerun}
        evidence["quality_gate"] = {"full_validation_snapshot": full}
    if workspace_ms is not None:
        evidence["workspace"]["provisioning_timings_ms"] = {"total": workspace_ms}
    if provisioning_ms is not None:
        evidence["provisioning_timings_ms"] = {"total": provisioning_ms, "seed_closure": 1.0}
    card = {
        "task_id": task_id,
        "accepted_request_id": f"R-{task_id}",
        "terminal_review": {"recorded_at": _at(review_ready), "evidence": evidence},
        "accepted_at": _at(accepted_at),
    }
    if manager_ready:
        card["manager_ready_at"] = _at(manager_ready_at)
    return {
        "created_at": _at(0), "claimed_at": _at(claimed), "started_at": _at(claimed),
        "card": card,
        "ledger": {
            "request_id": f"R-{task_id}", "task_id": task_id, "state": "review_ready",
            "finished_at": _at(review_ready),
            "finalization_duration_ms": finalization * 1000.0,
            "finalization_phase_durations_ms": {
                "workspace_scope": 12.5, "validation": wall * 1000.0,
                "evidence_and_transition": 40.0,
            },
        } if ledger else None,
        "reviewer_cards": [
            ("previous", _at(review_ready - 5000), f"R-prev-{task_id}"),
            ("correctness", _at(review_ready + reviewers / 2), f"R-corr-{task_id}"),
            ("security", _at(last_reviewer), f"R-sec-{task_id}"),
        ] if reviewer_join else [],
    }


# A: a 400 s validation wall (100 declared, 250+50 neighbour incl. flaky rerun).
# B: no neighbours selected, so neighbour time is a measured 0 rather than unknown.
# C: the NF-2026-02089-c1 shape (1821 s finalization) with manager_ready_at never recorded.
_CARDS = {
    "TASK-A": _timeline_card(
        task_id="TASK-A", queue=60, worker=600, finalization=1000, reviewers=900,
        manager_wait=120, accept=30, declared=100, neighbour=250, rerun=50, workspace_ms=50_000,
    ),
    "TASK-B": _timeline_card(
        task_id="TASK-B", queue=120, worker=300, finalization=100, reviewers=600,
        manager_wait=240, accept=60, declared=50, provisioning_ms=20_000,
    ),
    "TASK-C": _timeline_card(
        task_id="TASK-C", queue=30, worker=1200, finalization=1821, reviewers=1200,
        manager_wait=300, accept=90, declared=200, neighbour=600, workspace_ms=30_600,
        manager_ready=False,
    ),
}


def _insert_task(conn, task_id, card_json, created_at, claimed_at, started_at):
    conn.execute(
        "INSERT INTO tasks (task_id, card_json, created_at, updated_at, claimed_at, "
        "started_at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, card_json, created_at, created_at, claimed_at, started_at),
    )


def _seed_reviewers(conn, task_id, row):
    cursor = conn.execute(
        "INSERT INTO review_chains (target_task_id, target_request_id, claim_epoch, "
        "packet_sha256, candidate_sha256, chain_identity_json, chain_identity_sha256, "
        "created_at) VALUES (?, ?, '1', 'p', 'c', '{}', ?, ?)",
        (task_id, f"R-{task_id}", f"identity-{task_id}", row["created_at"]),
    )
    for attempt, (lens, recorded_at, request_id) in enumerate(row["reviewer_cards"], start=1):
        reviewer_task = f"REVIEW-{task_id}-{lens}"
        _insert_task(
            conn, reviewer_task, json.dumps({"terminal_review": {"recorded_at": recorded_at}}),
            row["created_at"], row["created_at"], row["created_at"],
        )
        conn.execute(
            "INSERT INTO review_orchestrator_route_attempts (chain_id, lens, attempt_index, "
            "reviewer_task_id, reviewer_request_id, runner, adapter_id, model, state) "
            "VALUES (?, ?, ?, ?, ?, 'runner', 'adapter', 'model', 'launched')",
            (cursor.lastrowid, lens, attempt, reviewer_task, request_id),
        )


def _seed(root, cards, *, reviewer_tables=True):
    root.mkdir()
    db = root / "tasks.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(task_store.SCHEMA)
        task_store.ensure_event_indexes(conn)
        if reviewer_tables:
            conn.executescript(review_lifecycle.SCHEMA)
            conn.execute(review_orchestrator.ROUTE_ATTEMPT_TABLE)
        for index, (task_id, row) in enumerate(sorted(cards.items()), start=1):
            card = row["card"]
            _insert_task(
                conn, task_id, card if isinstance(card, str) else json.dumps(card),
                row["created_at"], row["claimed_at"], row["started_at"],
            )
            if reviewer_tables:
                _seed_reviewers(conn, task_id, row)
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


def _write_ledger(root, cards):
    log = root.joinpath(*sdlc_outcome_metrics.PROCESS_LEDGER_REL)
    log.parent.mkdir(parents=True)
    rows = [row["ledger"] for _, row in sorted(cards.items()) if row.get("ledger")]
    log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _metrics(tmp_path, monkeypatch, cards, name="repo", *, reviewer_tables=True):
    root = tmp_path / name
    db = _seed(root, cards, reviewer_tables=reviewer_tables)
    _write_ledger(root, cards)
    monkeypatch.setattr(
        task_store, "storage_readiness",
        lambda _root: SimpleNamespace(ready=True, reason="", canonical_db=db),
    )
    return sdlc_outcome_metrics.read_repository_metrics(root, repository_id=_REPO, limit=100)


def test_a_card_that_still_has_per_command_rows_ignores_the_ledger_wall():
    record = {
        "created_at": _at(0), "claimed_at": _at(30), "started_at": _at(30),
        "card_failure": None,
        "review_ready_at": _at(3051),
        "worker_done_at": _at(1230),
        "validations": [
            {"command": "pytest -q", "duration_seconds": 200},
            {"command": "ruff", "duration_seconds": 999, "reused_from": "worker"},
        ],
        "ledger_validation_ms": 7_000_000.0,
        "snapshot_schema": "aiworkhub.full_validation_snapshot.v1",
        "neighbour_present": True,
        "neighbour_seconds": 600,
        "workspace_provisioning_ms": 30_600,
        "reviewer_receipts": [{"recorded_at": _at(3051 + 1200)}],
        "accept_started_at": _at(4561),
        "accepted_at": _at(4651),
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
    assert derived["total_seconds"] == 4651


def test_repository_metrics_carry_real_phase_seconds_from_the_stored_shape(
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
        "validation": (3, 0, 400.0, 800.0),
        "finalize_other": (3, 0, 600.0, 1021.0),
        "reviewers": (3, 0, 900.0, 1200.0),
        "manager_wait": (0, 3, None, None),
        "accept": (0, 3, None, None),
    }
    # accept_started_at is written nowhere, so these stay typed unknowns, never zero.
    assert timeline["phases"]["manager_wait"]["unknown_causes"] == {
        "accept_started_at_missing": 2, "manager_ready_at_missing": 1,
    }
    assert timeline["phases"]["accept"]["unknown_causes"] == {"accept_started_at_missing": 3}
    assert timeline["unknowns"] == {"cards_with_unknown_phase": 3, "phase_values_unknown": 6}
    slowest = timeline["slowest_cards"]
    assert [entry["task_id"] for entry in slowest] == ["TASK-C", "TASK-A", "TASK-B"]
    assert [entry["total_seconds"] for entry in slowest] == [4651.0, 2720.0, 1430.0]
    assert slowest[0]["phases"]["manager_wait"] == sdlc_phase_timeline.UNKNOWN
    assert slowest[0]["phases"]["accept"] == sdlc_phase_timeline.UNKNOWN
    # The ledger wall already contains the neighbour run, so it is not counted twice.
    assert slowest[1]["phases"]["validation"] == 400.0
    assert slowest[1]["validation_parts"] == {
        "declared": 100.0, "neighbour": 300.0, "provisioning": 50.0,
    }
    assert slowest[2]["validation_parts"] == {
        "declared": 50.0, "neighbour": 0.0, "provisioning": 20.0,
    }
    assert (timeline["slowest_truncated"], timeline["unranked_total_unknown"]) == (False, 0)
    # The existing outcome metrics are unchanged by the new section.
    assert result["first_pass_acceptance"]["denominator"] == 3


def test_no_ledger_row_snapshot_or_reviewer_join_keeps_every_phase_typed_unknown(
    tmp_path, monkeypatch
):
    row = _timeline_card(
        task_id="TASK-D", queue=10, worker=20, finalization=30, reviewers=40, manager_wait=50,
        accept=60, declared=5, workspace_ms=1_000, ledger=False, reviewer_join=False,
        snapshot=False,
    )
    timeline = _metrics(tmp_path, monkeypatch, {"TASK-D": row})["phase_timeline"]
    phases = timeline["phases"]
    assert phases["worker"]["unknown_causes"] == {"worker_done_at_missing": 1}
    assert phases["validation"]["unknown_causes"] == {"declared_timing_missing": 1}
    assert phases["finalize_other"]["unknown_causes"] == {"worker_done_at_missing": 1}
    assert phases["reviewers"]["unknown_causes"] == {"last_reviewer_at_missing": 1}
    for name in ("worker", "validation", "finalize_other", "reviewers"):
        assert (phases[name]["known"], phases[name]["median_seconds"]) == (0, None)
    parts = timeline["slowest_cards"][0]["validation_parts"]
    assert (parts["declared"], parts["neighbour"]) == ("UNKNOWN", "UNKNOWN")
    assert phases["queue"]["median_seconds"] == 10.0


def test_a_ledger_wall_without_a_snapshot_is_a_real_total_with_an_unknown_split(
    tmp_path, monkeypatch
):
    # Compaction may fold quality_gate as well, taking the snapshot with it.
    row = _timeline_card(
        task_id="TASK-E", queue=10, worker=20, finalization=30, reviewers=40, manager_wait=50,
        accept=60, declared=12, workspace_ms=1_000, snapshot=False,
    )
    timeline = _metrics(tmp_path, monkeypatch, {"TASK-E": row})["phase_timeline"]
    phases = timeline["phases"]
    assert phases["validation"]["median_seconds"] == 12.0
    assert phases["finalize_other"]["median_seconds"] == 18.0
    assert phases["worker"]["median_seconds"] == 20.0
    assert timeline["slowest_cards"][0]["validation_parts"] == {
        "declared": "UNKNOWN", "neighbour": "UNKNOWN", "provisioning": 1.0,
    }


def test_a_zero_ledger_timing_measured_nothing_and_stays_unknown(tmp_path, monkeypatch):
    row = _timeline_card(
        task_id="TASK-Z", queue=10, worker=20, finalization=30, reviewers=40, manager_wait=50,
        accept=60, declared=5, workspace_ms=1_000,
    )
    row["ledger"]["finalization_phase_durations_ms"]["validation"] = 0.0
    timeline = _metrics(tmp_path, monkeypatch, {"TASK-Z": row})["phase_timeline"]
    phases = timeline["phases"]
    assert phases["validation"]["unknown_causes"] == {"declared_timing_missing": 1}
    assert phases["finalize_other"]["unknown_causes"] == {"validation_unknown": 1}
    assert phases["worker"]["median_seconds"] == 20.0


def test_ledger_rows_of_another_request_or_task_are_not_used():
    record = {
        "created_at": _at(0), "claimed_at": _at(1), "started_at": _at(1),
        "card_failure": None, "review_ready_at": _at(100), "accepted_request_id": "R-T",
    }
    evidence = sdlc_phase_timeline.PhaseEvidence({"T": record}, available=True)
    timing = {
        "finished_at": _at(100), "finalization_duration_ms": 50_000.0, "validation_ms": 10_000.0,
    }
    summary = sdlc_phase_timeline.summarize(evidence, ["T"], [
        {"task_id": "T", "request_id": "R-older", **timing},
        {"task_id": "OTHER", "request_id": "R-T", **timing},
    ])
    assert summary["phases"]["worker"]["unknown_causes"] == {"worker_done_at_missing": 1}
    assert summary["phases"]["validation"]["unknown_causes"] == {"declared_timing_missing": 1}


def test_an_out_of_range_ledger_duration_stays_a_typed_unknown():
    record = {
        "created_at": _at(0), "claimed_at": _at(1), "started_at": _at(1),
        "card_failure": None, "review_ready_at": _at(100), "accepted_request_id": "R-T",
    }
    evidence = sdlc_phase_timeline.PhaseEvidence({"T": record}, available=True)
    summary = sdlc_phase_timeline.summarize(evidence, ["T"], [
        {"task_id": "T", "request_id": "R-T", "finished_at": _at(100),
         "finalization_duration_ms": 1e30},
    ])
    assert summary["phases"]["worker"]["unknown_causes"] == {"worker_done_at_missing": 1}


def test_a_retried_finalization_uses_the_newest_review_ready_row():
    record = {
        "created_at": _at(0), "claimed_at": _at(1), "started_at": _at(1),
        "card_failure": None, "review_ready_at": _at(200), "accepted_request_id": "R-T",
    }
    evidence = sdlc_phase_timeline.PhaseEvidence({"T": record}, available=True)
    row = {"task_id": "T", "request_id": "R-T"}
    summary = sdlc_phase_timeline.summarize(evidence, ["T"], [
        {**row, "finished_at": _at(200), "finalization_duration_ms": 100_000.0,
         "validation_ms": 40_000.0},
        {**row, "finished_at": _at(90), "finalization_duration_ms": 80_000.0,
         "validation_ms": 9_000.0},
    ])
    assert summary["phases"]["worker"]["median_seconds"] == 99.0
    assert summary["phases"]["validation"]["median_seconds"] == 40.0


def test_reviewer_time_is_attributed_to_the_target_through_the_chain_join(tmp_path, monkeypatch):
    row = _timeline_card(
        task_id="TASK-R", queue=10, worker=20, finalization=30, reviewers=40, manager_wait=50,
        accept=60, declared=5, workspace_ms=1_000,
    )
    ready = 10 + 20 + 30
    row["reviewer_cards"] = [
        # An earlier round's review of the same target is not this attempt's review.
        ("previous", _at(ready - 5000), "R-previous"),
        ("correctness", _at(ready + 15), "R-correctness"),
        ("security", _at(ready + 40), "R-security"),
        # Planned but never launched: no request id, so it never counts.
        ("planned", _at(ready + 9000), ""),
    ]
    timeline = _metrics(tmp_path, monkeypatch, {"TASK-R": row})["phase_timeline"]
    assert timeline["phases"]["reviewers"]["median_seconds"] == 40.0
    assert timeline["phases"]["reviewers"]["unknown"] == 0


def test_a_store_without_review_tables_keeps_reviewers_a_typed_unknown(tmp_path, monkeypatch):
    timeline = _metrics(
        tmp_path, monkeypatch, _CARDS, reviewer_tables=False
    )["phase_timeline"]
    assert timeline["available"] is True
    assert timeline["phases"]["reviewers"]["unknown_causes"] == {"last_reviewer_at_missing": 3}
    assert timeline["phases"]["reviewers"]["median_seconds"] is None
    assert timeline["phases"]["worker"]["known"] == 3


def test_a_reviewer_ledger_row_is_never_credited_to_its_own_card():
    record = {
        "created_at": _at(0), "claimed_at": _at(1), "started_at": _at(1),
        "card_failure": None, "review_ready_at": _at(100),
    }
    evidence = sdlc_phase_timeline.PhaseEvidence({"T": record}, available=True)
    rows = [{"task_id": "T", "timestamp": _at(160), "usage": {"role": "reviewer"}}]
    summary = sdlc_phase_timeline.summarize(evidence, ["T"], rows)
    assert summary["phases"]["reviewers"]["unknown_causes"] == {"last_reviewer_at_missing": 1}


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
                task_id=f"T{index}", queue=index + 1, worker=10, finalization=10, reviewers=10,
                manager_wait=10, accept=10, declared=1, workspace_ms=1_000,
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


_SLOT_NOTE = {
    "slots": 4, "waited_seconds": 1358.0, "acquired": True, "reason": "acquired_after_wait",
}


def _per_command_card(task_id, ledger_validation=None):
    """A card that kept its per-command rows; the wait sits outside their duration_seconds."""

    row = _timeline_card(
        task_id=task_id, queue=10, worker=100, finalization=2000, reviewers=40, manager_wait=50,
        accept=60, declared=100, neighbour=200, workspace_ms=1_000,
    )
    row["card"]["terminal_review"]["evidence"]["validations"] = [
        {"command": "pytest -q", "duration_seconds": 100},
    ]
    if ledger_validation is not None:
        row["ledger"]["validation"] = ledger_validation
    return row


def _slot_timeline(tmp_path, monkeypatch, row, name="repo"):
    return _metrics(tmp_path, monkeypatch, {"TASK-W": row}, name)["phase_timeline"]


def test_slot_wait_moves_from_finalize_other_into_validation(tmp_path, monkeypatch):
    before = _slot_timeline(
        tmp_path, monkeypatch, _per_command_card("TASK-W"), "before"
    )["slowest_cards"][0]
    assert before["phases"]["validation"] == 301.0
    assert before["phases"]["finalize_other"] == 1699.0

    noted = _per_command_card("TASK-W", [
        {"command": "pytest -q", "duration_seconds": 100, "host_slot_wait": _SLOT_NOTE},
    ])
    after = _slot_timeline(tmp_path, monkeypatch, noted, "after")["slowest_cards"][0]
    assert after["phases"]["validation"] == 1659.0
    assert after["phases"]["finalize_other"] == 341.0
    assert after["validation_parts"] == {
        "declared": 100.0, "neighbour": 200.0, "provisioning": 1.0, "slot_wait": 1358.0,
    }


def test_slot_wait_on_the_neighbour_path_is_not_counted_again(tmp_path, monkeypatch):
    row = _per_command_card("TASK-W", [{"command": "pytest -q", "duration_seconds": 100}])
    row["ledger"]["quality_gate"] = {"full_validation_snapshot": {"neighbour_tests": {
        "duration_seconds": 200,
        "host_slot_wait": _SLOT_NOTE,
        "validation": [{"command": "neighbours", "host_slot_wait": _SLOT_NOTE}],
    }}}
    assert "declared_slot_wait_seconds" not in sdlc_outcome_metrics._project_review_ready_row(
        row["ledger"]
    )
    card = _slot_timeline(tmp_path, monkeypatch, row)["slowest_cards"][0]
    assert card["phases"]["validation"] == 301.0
    assert "slot_wait" not in card["validation_parts"]


def test_slot_wait_of_one_batch_counts_once_and_separate_batches_add_up():
    project = sdlc_outcome_metrics._project_review_ready_row
    batch = [{"command": name, "host_slot_wait": dict(_SLOT_NOTE)} for name in "abc"]
    assert project({"validation": batch})["declared_slot_wait_seconds"] == 1358.0
    other = {"command": "d", "host_slot_wait": {**_SLOT_NOTE, "waited_seconds": 2.5}}
    assert project({"validation": [*batch, other]})["declared_slot_wait_seconds"] == 1360.5
    # A corrupt non-scalar field cannot break the once-per-batch key.
    odd = {"command": "e", "host_slot_wait": {**_SLOT_NOTE, "slots": [4]}}
    assert project({"validation": [odd, odd]})["declared_slot_wait_seconds"] == 1358.0


def test_slot_wait_absent_keeps_an_old_card_unchanged_and_known(tmp_path, monkeypatch):
    row = _per_command_card("TASK-W", [{"command": "pytest -q", "duration_seconds": 100}])
    assert "declared_slot_wait_seconds" not in sdlc_outcome_metrics._project_review_ready_row(
        row["ledger"]
    )
    timeline = _slot_timeline(tmp_path, monkeypatch, row)
    card = timeline["slowest_cards"][0]
    assert card["phases"]["validation"] == 301.0
    assert card["phases"]["finalize_other"] == 1699.0
    assert card["validation_parts"] == {
        "declared": 100.0, "neighbour": 200.0, "provisioning": 1.0,
    }
    for name in ("validation", "finalize_other"):
        assert timeline["phases"][name]["unknown"] == 0


@pytest.mark.parametrize(("waited", "declared"), [(400.0, 100.0), (5000.0, 0.0)])
def test_slot_wait_comes_out_of_declared_on_the_wall_path(
    tmp_path, monkeypatch, waited, declared
):
    row = _timeline_card(
        task_id="TASK-W", queue=10, worker=20, finalization=1000, reviewers=40, manager_wait=50,
        accept=60, declared=500, neighbour=200, workspace_ms=1_000,
    )
    row["ledger"]["validation"] = [
        {"command": "pytest -q", "host_slot_wait": {**_SLOT_NOTE, "waited_seconds": waited}},
    ]
    card = _slot_timeline(tmp_path, monkeypatch, row)["slowest_cards"][0]
    # The wall already contains the wait, so the phase total does not grow.
    assert card["phases"]["validation"] == 700.0
    assert card["phases"]["finalize_other"] == 300.0
    assert card["validation_parts"] == {
        "declared": declared, "neighbour": 200.0, "provisioning": 1.0, "slot_wait": waited,
    }


@pytest.mark.parametrize("row", [
    {"reused_from": "worker_validation_run", "host_slot_wait": _SLOT_NOTE},
    {"host_slot_wait": {**_SLOT_NOTE, "waited_seconds": -1}},
    {"host_slot_wait": {**_SLOT_NOTE, "waited_seconds": True}},
    {"host_slot_wait": {**_SLOT_NOTE, "waited_seconds": float("nan")}},
    {"host_slot_wait": "1358"},
    "not-a-row",
])
def test_slot_wait_ignores_reused_rows_and_malformed_notes(row):
    assert "declared_slot_wait_seconds" not in sdlc_outcome_metrics._project_review_ready_row(
        {"validation": [row]}
    )


def test_slot_wait_projection_invalidates_archived_segments():
    assert sdlc_outcome_metrics._SEGMENT_CACHE_VERSION >= 3
