"""NF-2026-01828: a successful accept records a system disposition per finding.

Every finding of a verified reviewer report that no manager decided gets one
``system:accept`` row bound to the accept receipt and the candidate digest:
``fixed_by_correction`` for a manager-confirmed finding the accept's correction
repaired, ``accepted_nonblocking`` for the rest. Manager rows are never
overwritten, the manager tool still refuses both system values, and the blocker
gate is unchanged.
"""

from __future__ import annotations

import importlib.util
import inspect
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher_accept_review as accept_review  # noqa: E402
from aiworkhub import quality_evidence as qe  # noqa: E402
from aiworkhub import review_orchestrator  # noqa: E402

_BASE_SPEC = importlib.util.spec_from_file_location(
    "_nf01170_disposition_fixtures_nf01828",
    Path(__file__).with_name("test_review_finding_disposition_nf01170.py"),
)
assert _BASE_SPEC is not None and _BASE_SPEC.loader is not None
base = importlib.util.module_from_spec(_BASE_SPEC)
_BASE_SPEC.loader.exec_module(base)

FINDING_1 = base.FINDING_1
FINDING_2 = base.FINDING_2
FINDING_3 = "reviewer:correctness:finding-3"
ACCEPT_RECEIPT = "a" * 64
CURRENT = "review_orchestrator_finding_dispositions"
HISTORY = "review_orchestrator_finding_disposition_history"


def _table(db: Path, table: str) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is None:
            return []
        return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def _by_finding(db: Path) -> dict[str, sqlite3.Row]:
    return {row["finding_id"]: row for row in _table(db, CURRENT)}


def _entry(receipt: dict, finding_id: str, *, corrected: bool = False) -> dict:
    return {
        "reviewer_task_id": "QUALITY_REVIEW_TARGET",
        "reviewer_request_id": "review-req-1",
        "reviewer_receipt_sha256": accept_review._receipt_sha256(receipt),
        "finding_id": finding_id,
        "lens": qe.LENS_CORRECTNESS,
        "severity": qe.SEVERITY_LOW,
        "corrected": corrected,
    }


def _record(driver, entries: list[dict], **overrides) -> dict:
    args = {
        "task_id": "TARGET",
        "request_id": "target-request",
        "candidate_sha256": base.CANDIDATE,
        "accept_receipt_sha256": ACCEPT_RECEIPT,
        "findings": entries,
    }
    args.update(overrides)
    return driver.record_accept_system_dispositions(**args)


def _three(tmp_path: Path):
    driver = base._driver(tmp_path)
    receipt = base._receipt(
        base._finding("finding-1"), base._finding("finding-2"), base._finding("finding-3")
    )
    return driver, receipt, tmp_path / "review.sqlite3"


# --------------------------------------------------------------------------
# what is recorded
# --------------------------------------------------------------------------
def test_every_undecided_finding_gets_one_bound_system_disposition(tmp_path: Path) -> None:
    driver, receipt, db = _three(tmp_path)
    entries = [_entry(receipt, f) for f in (FINDING_1, FINDING_2, FINDING_3)]

    result = _record(driver, entries)

    assert result["ok"] is True
    assert result["counts"] == {"accepted_nonblocking": 3, "fixed_by_correction": 0}
    assert result["already_recorded"] == 0
    rows = _by_finding(db)
    assert sorted(rows) == [FINDING_1, FINDING_2, FINDING_3]
    for row in rows.values():
        assert row["disposition"] == "accepted_nonblocking"
        assert row["actor"] == "system:accept"
        assert row["candidate_sha256"] == base.CANDIDATE
        assert row["reviewer_receipt_sha256"] == accept_review._receipt_sha256(receipt)
    stored = driver.review_finding_dispositions(task_id="TARGET", request_id="target-request")
    assert len(stored) == 3
    assert {row["accept_receipt_sha256"] for row in stored} == {ACCEPT_RECEIPT}


def test_confirmed_finding_repaired_by_correction_is_fixed_by_correction(
    tmp_path: Path,
) -> None:
    driver, receipt, db = _three(tmp_path)
    confirmed = base._dispose(
        driver, receipt, finding_id=FINDING_2, disposition="confirmed", counter_evidence=""
    )
    assert confirmed["ok"] is True

    result = _record(
        driver,
        [
            _entry(receipt, FINDING_2, corrected=True),
            _entry(receipt, FINDING_3),
        ],
    )

    assert result["counts"] == {"accepted_nonblocking": 1, "fixed_by_correction": 1}
    rows = _by_finding(db)
    assert rows[FINDING_2]["disposition"] == "fixed_by_correction"
    assert rows[FINDING_2]["actor"] == "system:accept"
    assert rows[FINDING_3]["disposition"] == "accepted_nonblocking"
    # Still exactly one current row per finding, and the manager's confirmed
    # receipt survives verbatim in the append-only history.
    assert len(_table(db, CURRENT)) == 2
    history = _table(db, HISTORY)
    assert [row["receipt_sha256"] for row in history] == [confirmed["receipt_sha256"]]
    assert history[0]["disposition"] == "confirmed"
    assert history[0]["actor"] == "codex:thread-exact"
    assert history[0]["superseded_by_receipt_sha256"] == rows[FINDING_2]["receipt_sha256"]
    stored = {
        row["finding_id"]: row
        for row in driver.review_finding_dispositions(
            task_id="TARGET", request_id="target-request"
        )
    }
    assert stored[FINDING_2]["prior_disposition"] == "confirmed"
    assert stored[FINDING_2]["supersedes_receipt_sha256"] == confirmed["receipt_sha256"]


def test_existing_manager_dispositions_are_never_overwritten(tmp_path: Path) -> None:
    driver, receipt, db = _three(tmp_path)
    dismissed = base._dispose(driver, receipt)
    confirmed = base._dispose(
        driver, receipt, finding_id=FINDING_2, disposition="confirmed", counter_evidence=""
    )
    before = {fid: tuple(row) for fid, row in _by_finding(db).items()}

    # FINDING_1 is dismissed (even if a correction named it), FINDING_2 is
    # confirmed but no correction repaired it.
    result = _record(
        driver,
        [
            _entry(receipt, FINDING_1, corrected=True),
            _entry(receipt, FINDING_2),
            _entry(receipt, FINDING_3),
        ],
    )

    assert result["counts"] == {"accepted_nonblocking": 1, "fixed_by_correction": 0}
    assert result["already_recorded"] == 2
    after = _by_finding(db)
    assert tuple(after[FINDING_1]) == before[FINDING_1]
    assert tuple(after[FINDING_2]) == before[FINDING_2]
    assert after[FINDING_1]["receipt_sha256"] == dismissed["receipt_sha256"]
    assert after[FINDING_2]["receipt_sha256"] == confirmed["receipt_sha256"]
    assert _table(db, HISTORY) == []


def test_a_confirmation_bound_to_other_bytes_is_not_superseded(tmp_path: Path) -> None:
    driver, receipt, db = _three(tmp_path)
    base._dispose(
        driver, receipt, finding_id=FINDING_2, disposition="confirmed", counter_evidence=""
    )
    before = tuple(_by_finding(db)[FINDING_2])

    result = _record(
        driver, [_entry(receipt, FINDING_2, corrected=True)], candidate_sha256="f" * 64
    )

    assert result["ok"] is True
    assert result["counts"] == {"accepted_nonblocking": 0, "fixed_by_correction": 0}
    assert tuple(_by_finding(db)[FINDING_2]) == before
    assert _table(db, HISTORY) == []


def test_replayed_accept_does_not_duplicate_rows(tmp_path: Path) -> None:
    driver, receipt, db = _three(tmp_path)
    base._dispose(
        driver, receipt, finding_id=FINDING_2, disposition="confirmed", counter_evidence=""
    )
    entries = [
        _entry(receipt, FINDING_1),
        _entry(receipt, FINDING_2, corrected=True),
        _entry(receipt, FINDING_3),
    ]

    first = _record(driver, entries)
    snapshot = [tuple(row) for row in _table(db, CURRENT)]
    history = [tuple(row) for row in _table(db, HISTORY)]
    second = _record(driver, entries)

    assert first["counts"] == {"accepted_nonblocking": 2, "fixed_by_correction": 1}
    assert second["ok"] is True
    assert second["counts"] == {"accepted_nonblocking": 0, "fixed_by_correction": 0}
    assert second["already_recorded"] == 3
    assert [tuple(row) for row in _table(db, CURRENT)] == snapshot
    assert [tuple(row) for row in _table(db, HISTORY)] == history


def test_an_unbound_entry_rolls_the_whole_batch_back(tmp_path: Path) -> None:
    driver, receipt, db = _three(tmp_path)
    bad = {**_entry(receipt, FINDING_3), "reviewer_receipt_sha256": ""}

    result = _record(driver, [_entry(receipt, FINDING_1), bad])

    assert result == {"ok": False, "error": "system_disposition_finding_unbound"}
    assert _table(db, CURRENT) == []


def test_candidate_and_accept_receipt_must_be_digests(tmp_path: Path) -> None:
    driver, receipt, db = _three(tmp_path)
    entries = [_entry(receipt, FINDING_1)]

    assert _record(driver, entries, candidate_sha256="")["error"] == (
        "system_disposition_candidate_unavailable"
    )
    assert _record(driver, entries, accept_receipt_sha256="nope")["error"] == (
        "system_disposition_accept_receipt_invalid"
    )
    assert _table(db, CURRENT) == []


# --------------------------------------------------------------------------
# the manager surface and the gate are unchanged
# --------------------------------------------------------------------------
def test_manager_tool_still_refuses_the_system_dispositions(tmp_path: Path) -> None:
    driver = base._driver(tmp_path)
    receipt = base._receipt(base._finding("finding-1"))

    assert review_orchestrator.FINDING_DISPOSITIONS == {"confirmed", "dismissed"}
    assert review_orchestrator.SYSTEM_FINDING_DISPOSITIONS == {
        "fixed_by_correction",
        "accepted_nonblocking",
    }
    assert not (
        review_orchestrator.FINDING_DISPOSITIONS
        & review_orchestrator.SYSTEM_FINDING_DISPOSITIONS
    )
    for system_value in sorted(review_orchestrator.SYSTEM_FINDING_DISPOSITIONS):
        assert base._dispose(driver, receipt, disposition=system_value) == {
            "ok": False,
            "error": "review_finding_disposition_invalid",
        }
    assert driver.review_finding_dispositions(
        task_id="TARGET", request_id="target-request"
    ) == []


def _fold_for(receipt: dict, dispositions: list[dict]) -> dict:
    reviewers = [
        {
            "task_id": "QUALITY_REVIEW_TARGET",
            "lens": qe.LENS_CORRECTNESS,
            "request_id": "review-req-1",
            "state": "review_ready",
            "usable": True,
            "receipt": receipt,
        }
    ]
    return accept_review.fold_accept_blockers(
        reviewers=reviewers,
        reviewer_request_ids=None,
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        terminal_substatus="review_ready",
        finding_dispositions=dispositions,
    )


def test_blocker_gate_is_unchanged_by_system_rows(tmp_path: Path) -> None:
    driver = base._driver(tmp_path)
    receipt = base._receipt(
        base._finding("finding-1"), base._finding("finding-2", qe.SEVERITY_HIGH)
    )

    def blockers() -> list[str]:
        stored = driver.review_finding_dispositions(
            task_id="TARGET", request_id="target-request"
        )
        bound = accept_review.bound_finding_dispositions(
            base._DispositionStore(stored),
            "TARGET",
            "target-request",
            candidate_sha256=accept_review._target_candidate_sha256(base._target_card()),
            receipt_sha256_by_reviewer={
                "review-req-1": accept_review._receipt_sha256(receipt)
            },
        )
        fold = _fold_for(receipt, bound)
        return sorted(
            row["detail"] for row in fold["blockers"] if row["kind"] == "refinement_required"
        )

    assert blockers() == [FINDING_1, FINDING_2]
    # A system row is written only by a successful accept; even if one existed
    # it would lift nothing, so an undisposed blocking finding still blocks.
    _record(driver, [_entry(receipt, FINDING_1), _entry(receipt, FINDING_2)])
    assert blockers() == [FINDING_1, FINDING_2]


# --------------------------------------------------------------------------
# the accept-side helper and its ordering
# --------------------------------------------------------------------------
def _helper(tmp_path: Path, monkeypatch, receipt: dict, **overrides) -> dict:
    db = tmp_path / "review.sqlite3"
    monkeypatch.setattr(review_orchestrator, "canonical_review_db", lambda _m: db)
    digest = accept_review._receipt_sha256(receipt)
    args = {
        "task_id": "TARGET",
        "request_id": "target-request",
        "card": base._target_card(),
        "reviewer_ids": ["review-req-1"],
        "reviewer_reports": [receipt["report"]],
        "reviewer_tasks": [("QUALITY_REVIEW_TARGET", None, True)],
        "receipt_sha256_by_reviewer": {"review-req-1": digest},
        "correction_record": None,
        "accepted_outcome_receipt": {"receipt_id": "sha256:" + ACCEPT_RECEIPT},
    }
    args.update(overrides)
    return accept_review.record_accept_finding_dispositions(
        SimpleNamespace(repo=tmp_path), **args
    )


def test_accept_helper_covers_every_reported_finding(tmp_path: Path, monkeypatch) -> None:
    driver, receipt, db = _three(tmp_path)
    base._dispose(
        driver, receipt, finding_id=FINDING_2, disposition="confirmed", counter_evidence=""
    )
    base._dispose(driver, receipt)

    result = _helper(
        tmp_path, monkeypatch, receipt, correction_record={"finding_ids": [FINDING_2]}
    )

    assert result["ok"] is True
    assert result["counts"] == {"accepted_nonblocking": 1, "fixed_by_correction": 1}
    assert result["accept_receipt_sha256"] == ACCEPT_RECEIPT
    assert result["candidate_sha256"] == base.CANDIDATE
    rows = _by_finding(db)
    assert {fid: row["disposition"] for fid, row in rows.items()} == {
        FINDING_1: "dismissed",
        FINDING_2: "fixed_by_correction",
        FINDING_3: "accepted_nonblocking",
    }
    assert _helper(
        tmp_path, monkeypatch, receipt, correction_record={"finding_ids": [FINDING_2]}
    )["counts"] == {"accepted_nonblocking": 0, "fixed_by_correction": 0}
    assert len(_table(db, CURRENT)) == 3


def test_accept_helper_failure_records_nothing_and_never_raises(
    tmp_path: Path, monkeypatch
) -> None:
    receipt = base._receipt(base._finding("finding-1"))
    db = tmp_path / "review.sqlite3"

    no_receipt = _helper(
        tmp_path, monkeypatch, receipt, accepted_outcome_receipt={"receipt_id": ""}
    )
    assert no_receipt == {
        "ok": False,
        "error": "system_disposition_accept_receipt_invalid",
    }
    no_digest = _helper(
        tmp_path, monkeypatch, receipt, receipt_sha256_by_reviewer={}
    )
    assert no_digest["ok"] is False
    assert _table(db, CURRENT) == []

    monkeypatch.setattr(review_orchestrator, "canonical_review_db", lambda _m: None)
    unavailable = accept_review.record_accept_finding_dispositions(
        SimpleNamespace(repo=tmp_path),
        task_id="TARGET",
        request_id="target-request",
        card=base._target_card(),
        reviewer_ids=["review-req-1"],
        reviewer_reports=[receipt["report"]],
        reviewer_tasks=[("QUALITY_REVIEW_TARGET", None, True)],
        receipt_sha256_by_reviewer={},
        correction_record=None,
        accepted_outcome_receipt={"receipt_id": "sha256:" + ACCEPT_RECEIPT},
    )
    assert unavailable == {"ok": False, "error": "review_storage_unavailable"}


def test_accept_helper_misaligned_inputs_fail_into_the_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    receipt = base._receipt(base._finding("finding-1"))
    db = tmp_path / "review.sqlite3"

    result = _helper(
        tmp_path,
        monkeypatch,
        receipt,
        reviewer_tasks=[
            ("QUALITY_REVIEW_TARGET", None, True),
            ("QUALITY_REVIEW_EXTRA", None, True),
        ],
    )
    assert result == {"ok": False, "error": "system_disposition_failed:ValueError"}

    unpackable = _helper(tmp_path, monkeypatch, receipt, reviewer_tasks=[None])
    assert unpackable == {"ok": False, "error": "system_disposition_failed:TypeError"}
    assert _table(db, CURRENT) == []


def test_accept_records_dispositions_only_after_the_accept_landed() -> None:
    source = inspect.getsource(accept_review.accept_review)
    call = source.index("record_accept_finding_dispositions(")

    assert source.index("task_engine.accept_review(") < call
    assert source.index("promotion_finalize_failed:") < call
    assert '"auto_finding_dispositions": auto_finding_dispositions' in source
