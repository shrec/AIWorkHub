"""NF-2026-01682: a verified manager may re-dispose a reviewer finding.

A confirmation made in error can become ``dismissed`` and the reverse, but
only with a non-empty reason. The current row is updated in place to the new
receipt, and the superseded receipt is first copied verbatim into an
append-only history table in the same transaction. Once the target is
accepted, every disposition is frozen.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher  # noqa: E402
from aiworkhub import process_launcher_accept_review as accept_review  # noqa: E402
from aiworkhub import review_orchestrator  # noqa: E402

_BASE_SPEC = importlib.util.spec_from_file_location(
    "_nf01170_disposition_fixtures",
    Path(__file__).with_name("test_review_finding_disposition_nf01170.py"),
)
assert _BASE_SPEC is not None and _BASE_SPEC.loader is not None
base = importlib.util.module_from_spec(_BASE_SPEC)
_BASE_SPEC.loader.exec_module(base)

FINDING_1 = base.FINDING_1
CURRENT = "review_orchestrator_finding_dispositions"
HISTORY = "review_orchestrator_finding_disposition_history"
COLUMNS = review_orchestrator._FINDING_DISPOSITION_COLUMNS


def _rows(db: Path, table: str, columns: str = COLUMNS) -> list[tuple]:
    with closing(sqlite3.connect(db)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is None:
            return []
        order = "history_id" if table == HISTORY else "finding_id"
        return conn.execute(f"SELECT {columns} FROM {table} ORDER BY {order}").fetchall()


def _db(tmp_path: Path) -> Path:
    return tmp_path / "review.sqlite3"


def _confirm(driver, receipt: dict, **overrides) -> dict:
    args = {"disposition": "confirmed", "counter_evidence": "", "reason": ""}
    args.update(overrides)
    return base._dispose(driver, receipt, **args)


def _preview_after(driver, receipt: dict) -> dict:
    """The accept_preview fold over what the store holds now."""
    rows = driver.review_finding_dispositions(task_id="TARGET", request_id="target-request")
    reviewers = [base._reviewer_row(receipt)]
    bound = accept_review.bound_finding_dispositions(
        base._DispositionStore(rows),
        "TARGET",
        "target-request",
        candidate_sha256=accept_review._target_candidate_sha256(base._target_card()),
        receipt_sha256_by_reviewer=accept_review._sealed_receipt_sha256(reviewers),
    )
    return base._preview(reviewers, bound)


# --------------------------------------------------------------------------
# re-disposition in both directions
# --------------------------------------------------------------------------
def test_confirmed_then_dismissed_with_reason_lifts_the_blocker(tmp_path: Path) -> None:
    driver = base._driver(tmp_path)
    receipt = base._receipt(base._finding("finding-1"))

    confirmed = _confirm(driver, receipt)
    assert confirmed["ok"] is True
    assert base._preview_refinements(_preview_after(driver, receipt)) == [FINDING_1]

    dismissed = base._dispose(driver, receipt, reason="the confirmation was made in error")
    assert dismissed["ok"] is True and dismissed["idempotent"] is False
    assert dismissed["disposition"] == "dismissed"
    assert dismissed["prior_disposition"] == "confirmed"
    assert dismissed["supersedes_receipt_sha256"] == confirmed["receipt_sha256"]
    assert dismissed["receipt_sha256"] != confirmed["receipt_sha256"]

    fold = _preview_after(driver, receipt)
    assert base._preview_refinements(fold) == []
    assert fold["finding_dispositions"] == [
        {
            "finding_id": FINDING_1,
            "reviewer_request_id": "review-req-1",
            "lens": base.qe.LENS_CORRECTNESS,
            "severity": base.qe.SEVERITY_LOW,
            "disposition": "dismissed",
            "receipt_sha256": dismissed["receipt_sha256"],
            "blocker_lifted": True,
        }
    ]
    # The current table still holds exactly one row for the finding.
    stored = driver.review_finding_dispositions(task_id="TARGET", request_id="target-request")
    assert [row["receipt_sha256"] for row in stored] == [dismissed["receipt_sha256"]]


def test_dismissed_then_confirmed_restores_the_blocker(tmp_path: Path) -> None:
    driver = base._driver(tmp_path)
    receipt = base._receipt(base._finding("finding-1"))

    dismissed = base._dispose(driver, receipt)
    assert dismissed["ok"] is True
    assert base._preview_refinements(_preview_after(driver, receipt)) == []

    confirmed = _confirm(driver, receipt, reason="re-measured: the branch is reachable")
    assert confirmed["ok"] is True
    assert confirmed["prior_disposition"] == "dismissed"
    assert confirmed["supersedes_receipt_sha256"] == dismissed["receipt_sha256"]

    fold = _preview_after(driver, receipt)
    refinement = [row for row in fold["blockers"] if row["kind"] == "refinement_required"]
    assert [row["detail"] for row in refinement] == [FINDING_1]
    assert refinement[0]["disposition"] == "confirmed"
    assert refinement[0]["disposition_receipt_sha256"] == confirmed["receipt_sha256"]


@pytest.mark.parametrize("blank", ["", "   \n\t"])
def test_redisposition_without_reason_is_refused_and_the_row_is_unchanged(
    tmp_path: Path, blank: str
) -> None:
    driver = base._driver(tmp_path)
    receipt = base._receipt(base._finding("finding-1"))
    confirmed = _confirm(driver, receipt)
    before = _rows(_db(tmp_path), CURRENT)

    refused = base._dispose(driver, receipt, reason=blank)

    assert refused == {
        "ok": False,
        "error": "review_finding_disposition_conflict",
        "receipt_sha256": confirmed["receipt_sha256"],
        "detail": "redisposition_reason_required",
    }
    assert _rows(_db(tmp_path), CURRENT) == before
    assert _rows(_db(tmp_path), HISTORY) == []


def test_redisposition_still_passes_every_existing_gate(tmp_path: Path) -> None:
    driver = base._driver(tmp_path)
    receipt = base._receipt(base._finding("finding-1"))
    _confirm(driver, receipt)
    before = _rows(_db(tmp_path), CURRENT)

    assert base._dispose(driver, receipt, counter_evidence="", reason="why") == {
        "ok": False,
        "error": "review_finding_disposition_counter_evidence_required",
    }
    assert base._dispose(driver, receipt, candidate_sha256="", reason="why") == {
        "ok": False,
        "error": "review_finding_disposition_candidate_unavailable",
    }
    assert _rows(_db(tmp_path), CURRENT) == before
    assert _rows(_db(tmp_path), HISTORY) == []


# --------------------------------------------------------------------------
# the append-only history
# --------------------------------------------------------------------------
def test_each_redisposition_appends_one_verbatim_hash_verifiable_history_row(
    tmp_path: Path,
) -> None:
    driver = base._driver(tmp_path)
    receipt = base._receipt(base._finding("finding-1"))
    db = _db(tmp_path)

    first = _confirm(driver, receipt)
    first_row = _rows(db, CURRENT)
    second = base._dispose(driver, receipt, reason="confirmed in error")
    second_row = _rows(db, CURRENT)
    assert len(_rows(db, HISTORY)) == 1
    third = _confirm(driver, receipt, reason="dismissed in error")

    history = _rows(db, HISTORY, f"{COLUMNS},superseded_by_receipt_sha256")
    assert [row[:-1] for row in history] == first_row + second_row
    assert [row[-1] for row in history] == [second["receipt_sha256"], third["receipt_sha256"]]
    assert [row[0] for row in history] == [first["receipt_sha256"], second["receipt_sha256"]]
    for row in history:
        receipt_json = row[COLUMNS.split(",").index("receipt_json")]
        assert review_orchestrator.canonical_digest(json.loads(receipt_json)) == row[0]

    current = _rows(db, CURRENT)
    assert len(current) == 1 and current[0][0] == third["receipt_sha256"]
    assert third["supersedes_receipt_sha256"] == second["receipt_sha256"]


def test_history_rows_are_never_updated_or_deleted(tmp_path: Path) -> None:
    driver = base._driver(tmp_path)
    receipt = base._receipt(base._finding("finding-1"))
    _confirm(driver, receipt)
    assert base._dispose(driver, receipt, reason="confirmed in error")["ok"] is True
    before = _rows(_db(tmp_path), HISTORY)

    with closing(sqlite3.connect(_db(tmp_path))) as conn:
        for statement in (
            f"UPDATE {HISTORY} SET disposition='dismissed'",
            f"DELETE FROM {HISTORY}",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(statement)
    assert _rows(_db(tmp_path), HISTORY) == before


def test_identical_replay_is_idempotent_and_writes_no_history(tmp_path: Path) -> None:
    driver = base._driver(tmp_path)
    receipt = base._receipt(base._finding("finding-1"))
    db = _db(tmp_path)

    first = _confirm(driver, receipt)
    replay = _confirm(driver, receipt)
    assert replay["ok"] is True and replay["idempotent"] is True
    assert replay["receipt_sha256"] == first["receipt_sha256"]
    assert _rows(db, HISTORY) == []

    # A replay of the re-disposition itself is idempotent as well.
    moved = base._dispose(driver, receipt, reason="confirmed in error")
    moved_again = base._dispose(driver, receipt, reason="confirmed in error")
    assert moved_again["ok"] is True and moved_again["idempotent"] is True
    assert moved_again["receipt_sha256"] == moved["receipt_sha256"]
    assert moved_again["prior_disposition"] == "confirmed"
    assert len(_rows(db, HISTORY)) == 1


# --------------------------------------------------------------------------
# frozen after accept
# --------------------------------------------------------------------------
def _manager_dispose(monkeypatch, repo: Path, card: dict, receipt: dict) -> dict:
    monkeypatch.setattr(
        process_launcher.core,
        "manager_bootstrap",
        lambda: {
            "role": "manager",
            "repo": str(repo),
            "manager_route": {"provider": "codex", "thread_id": "thread-exact"},
        },
    )
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(
        review_orchestrator, "canonical_review_db", lambda _m: _db(repo)
    )
    monkeypatch.setattr(process_launcher, "_parse_card", lambda _raw, _tid: card)
    monkeypatch.setattr(
        accept_review,
        "reviewer_evidence",
        lambda _self, _task, _request: [
            {"request_id": "review-req-1", "usable": True, "receipt": receipt}
        ],
    )
    return process_launcher.ProcessManager.dispose_review_finding(
        SimpleNamespace(repo=repo, _show_task=lambda _task_id: "{}"),
        task_id="TARGET",
        request_id="target-request",
        reviewer_request_id="review-req-1",
        finding_id=FINDING_1,
        disposition="dismissed",
        counter_evidence="measured: unreachable",
        reason="the confirmation was made in error",
    )


@pytest.mark.parametrize(
    "accepted",
    [{"status": "finished"}, {"worker_status": "done"}],
    ids=["status_finished", "worker_status_done"],
)
def test_dispose_after_the_target_is_accepted_is_refused_frozen(
    monkeypatch, tmp_path: Path, accepted: dict
) -> None:
    repo = tmp_path.resolve()
    receipt = base._receipt(base._finding("finding-1"))
    _confirm(base._driver(repo), receipt)
    before = _rows(_db(repo), CURRENT)
    card = {**base._target_card(), **accepted}

    result = _manager_dispose(monkeypatch, repo, card, receipt)

    assert result["ok"] is False
    assert result["error"] == "review_finding_disposition_frozen_after_accept"
    assert _rows(_db(repo), CURRENT) == before
    assert _rows(_db(repo), HISTORY) == []


def test_dispose_before_accept_still_redisposes_through_the_manager(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path.resolve()
    receipt = base._receipt(base._finding("finding-1"))
    confirmed = _confirm(base._driver(repo), receipt)

    result = _manager_dispose(
        monkeypatch, repo, {**base._target_card(), "status": "review"}, receipt
    )

    assert result["ok"] is True
    assert result["prior_disposition"] == "confirmed"
    assert result["supersedes_receipt_sha256"] == confirmed["receipt_sha256"]
    assert len(_rows(_db(repo), HISTORY)) == 1
