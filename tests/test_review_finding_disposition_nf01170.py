"""NF-2026-01170: a manager-only, receipt-bound disposition of one reviewer finding.

A LOW/MEDIUM correctness or security finding becomes a ``refinement_required``
blocker. A manager who measured it to be a false positive can now record an
append-only ``dismissed`` receipt with counter-evidence; that lifts only that
finding's blocker, only for the exact candidate and reviewer receipt it was
bound to, and never for a blocking severity.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher  # noqa: E402
from aiworkhub import process_launcher_accept_review as accept_review  # noqa: E402
from aiworkhub import quality_calibration  # noqa: E402
from aiworkhub import quality_evidence as qe  # noqa: E402
from aiworkhub import review_orchestrator  # noqa: E402

CHANGED = {"src/aiworkhub/example.py": "a" * 64}
CANDIDATE = review_orchestrator.candidate_digest(CHANGED)
FINDING_1 = "reviewer:correctness:finding-1"
FINDING_2 = "reviewer:correctness:finding-2"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def _finding(finding_id: str, severity: str = qe.SEVERITY_LOW) -> dict[str, str]:
    return {
        "id": finding_id,
        "severity": severity,
        "summary": "reviewer claims a defect",
        "evidence": "fixture:claimed-defect",
    }


def _report(*findings: dict[str, str]) -> dict:
    return {
        "lens": qe.LENS_CORRECTNESS,
        "provider": "reviewer-b",
        "read_only": True,
        "can_mutate_repo": False,
        "findings": list(findings),
    }


def _fold(reports: list[dict], **kwargs) -> dict:
    return qe.fold_quality_verdict(
        [{"check_id": "tests", "kind": "test", "status": qe.STATUS_PASSED}],
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        reviewer_reports=reports,
        combined_tree_checks=[
            {"check_id": "combined", "kind": "test", "status": qe.STATUS_PASSED}
        ],
        worker_provider="worker-a",
        **kwargs,
    )


def _receipt(*findings: dict[str, str], reviewer_request_id: str = "review-req-1") -> dict:
    return {
        "schema_id": "aiworkhub.quality_review_receipt.v1",
        "packet_sha256": "b" * 64,
        "target": {"request_id": "target-request", "task_id": "TARGET", "claim_epoch": 3},
        "reviewer": {
            "request_id": reviewer_request_id,
            "task_id": "QUALITY_REVIEW_TARGET",
            "provider": "reviewer-b",
        },
        "report": {
            **_report(
                *[{**row, "disposition": "defect", "actionable": True} for row in findings]
            ),
        },
        "authority": {
            "process_identity_verified": True,
            "audit_verified": True,
            "terminal_state": "review_ready",
        },
        "submission_id": "c" * 64,
        "physical_submission_count": 1,
        "logical_submission_count": 1,
    }


def _target_card() -> dict:
    return {
        "task_id": "TARGET",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {
                    "task_id": "TARGET",
                    "request_id": "target-request",
                    "claim_epoch": 3,
                },
                "attempt_artifact_manifest": {"manifest_sha256": "d" * 64},
                "changed_path_hashes": dict(CHANGED),
            },
        },
    }


def _driver(tmp_path: Path) -> review_orchestrator.ReviewOrchestrator:
    return review_orchestrator.ReviewOrchestrator(
        SimpleNamespace(repo=tmp_path), db_path=tmp_path / "review.sqlite3"
    )


def _dispose(driver, receipt: dict, **overrides) -> dict:
    args = {
        "task_id": "TARGET",
        "request_id": "target-request",
        "reviewer_request_id": "review-req-1",
        "finding_id": FINDING_1,
        "disposition": "dismissed",
        "counter_evidence": "measured: the flagged branch is unreachable",
        "reason": "reviewer false positive",
        "actor": "codex:thread-exact",
        "reviewer_receipt": receipt,
        "candidate_sha256": CANDIDATE,
    }
    args.update(overrides)
    return driver.dispose_review_finding(**args)


# --------------------------------------------------------------------------
# the pure fold
# --------------------------------------------------------------------------
def test_low_correctness_finding_blocks_acceptance() -> None:
    verdict = _fold([_report(_finding("finding-1"))])

    assert verdict["passed"] is False
    assert f"refinement_required:{FINDING_1}" in verdict["blocking_evidence"]


def test_dismissal_lifts_only_that_finding_while_a_second_still_blocks() -> None:
    reports = [_report(_finding("finding-1"), _finding("finding-2"))]
    verdict = _fold(
        reports,
        finding_dispositions=[
            {"finding_id": FINDING_1, "disposition": "dismissed", "receipt_sha256": "e" * 64}
        ],
    )

    assert f"refinement_required:{FINDING_1}" not in verdict["blocking_evidence"]
    assert f"refinement_required:{FINDING_2}" in verdict["blocking_evidence"]
    assert verdict["passed"] is False
    assert verdict["manager_dismissed_findings"] == [
        {
            "finding_id": FINDING_1,
            "lens": qe.LENS_CORRECTNESS,
            "severity": qe.SEVERITY_LOW,
            "disposition": "dismissed",
            "classification": "manager_dismissed_reviewer_false_positive",
            "receipt_sha256": "e" * 64,
        }
    ]


def test_dismissing_the_only_finding_passes_the_fold() -> None:
    verdict = _fold(
        [_report(_finding("finding-1"))],
        finding_dispositions=[
            {"finding_id": FINDING_1, "disposition": "dismissed", "receipt_sha256": "e" * 64}
        ],
    )

    assert verdict["blocking_evidence"] == []
    assert verdict["passed"] is True


def test_confirmed_disposition_keeps_the_blocker() -> None:
    reports = [_report(_finding("finding-1"))]
    baseline = _fold(reports)
    confirmed = _fold(
        reports,
        finding_dispositions=[
            {"finding_id": FINDING_1, "disposition": "confirmed", "receipt_sha256": "e" * 64}
        ],
    )

    assert f"refinement_required:{FINDING_1}" in confirmed["blocking_evidence"]
    assert confirmed == baseline


def test_blocking_severity_is_never_suppressed_even_with_a_disposition_row() -> None:
    verdict = _fold(
        [_report(_finding("finding-1", qe.SEVERITY_HIGH))],
        finding_dispositions=[
            {"finding_id": FINDING_1, "disposition": "dismissed", "receipt_sha256": "e" * 64}
        ],
    )

    assert FINDING_1 in verdict["blocking_evidence"]
    assert verdict["passed"] is False
    assert "manager_dismissed_findings" not in verdict


def test_an_ambiguous_finding_id_across_reports_is_never_suppressed() -> None:
    verdict = _fold(
        [_report(_finding("finding-1")), _report(_finding("finding-1"))],
        finding_dispositions=[
            {"finding_id": FINDING_1, "disposition": "dismissed", "receipt_sha256": "e" * 64}
        ],
    )

    assert f"refinement_required:{FINDING_1}" in verdict["blocking_evidence"]


def test_fold_without_dispositions_is_identical_to_current_output() -> None:
    reports = [_report(_finding("finding-1"), _finding("finding-2", qe.SEVERITY_MEDIUM))]
    current = _fold(reports)

    for empty in (None, [], ()):
        folded = _fold(reports, finding_dispositions=empty)
        assert json.dumps(folded, sort_keys=True) == json.dumps(current, sort_keys=True)
    assert "manager_dismissed_findings" not in current
    calibration = quality_calibration.run_calibration()
    pinned = [
        case for case in calibration["cases"] if case["case_id"] == "reviewer_refinement"
    ]
    assert pinned and pinned[0]["calibrated"] is True


def test_reviewer_report_bytes_are_never_modified(tmp_path: Path) -> None:
    reports = [_report(_finding("finding-1"), _finding("finding-2"))]
    receipt = _receipt(_finding("finding-1"))
    before_reports = json.dumps(reports, sort_keys=True)
    before_receipt = json.dumps(receipt, sort_keys=True)
    snapshot = copy.deepcopy(receipt)

    _fold(
        reports,
        finding_dispositions=[
            {"finding_id": FINDING_1, "disposition": "dismissed", "receipt_sha256": "e" * 64}
        ],
    )
    assert _dispose(_driver(tmp_path), receipt)["ok"] is True

    assert json.dumps(reports, sort_keys=True) == before_reports
    assert json.dumps(receipt, sort_keys=True) == before_receipt
    assert receipt == snapshot


# --------------------------------------------------------------------------
# write-time refusals and the append-only receipt
# --------------------------------------------------------------------------
def test_dismissal_with_empty_counter_evidence_is_refused(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    receipt = _receipt(_finding("finding-1"))

    for blank in ("", "   \n\t"):
        assert _dispose(driver, receipt, counter_evidence=blank) == {
            "ok": False,
            "error": "review_finding_disposition_counter_evidence_required",
        }
    assert driver.review_finding_dispositions(
        task_id="TARGET", request_id="target-request"
    ) == []


def test_high_severity_finding_is_refused(tmp_path: Path) -> None:
    receipt = _receipt(_finding("finding-1", qe.SEVERITY_HIGH))

    assert _dispose(_driver(tmp_path), receipt) == {
        "ok": False,
        "error": "review_finding_disposition_blocking_severity",
    }


def test_unknown_finding_is_refused(tmp_path: Path) -> None:
    receipt = _receipt(_finding("finding-1"))

    assert _dispose(
        _driver(tmp_path), receipt, finding_id="reviewer:correctness:nope"
    ) == {"ok": False, "error": "review_finding_disposition_finding_unknown"}


def test_reviewer_not_bound_to_this_task_and_request_is_refused(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    receipt = _receipt(_finding("finding-1"))

    assert _dispose(driver, receipt, request_id="other-request") == {
        "ok": False,
        "error": "review_finding_disposition_reviewer_not_bound",
    }
    assert _dispose(driver, None) == {
        "ok": False,
        "error": "review_finding_disposition_reviewer_not_bound",
    }


def test_replay_is_idempotent_and_a_conflicting_disposition_is_refused(
    tmp_path: Path,
) -> None:
    driver = _driver(tmp_path)
    receipt = _receipt(_finding("finding-1"))

    first = _dispose(driver, receipt)
    assert first["ok"] is True and first["idempotent"] is False
    assert len(first["receipt_sha256"]) == 64
    assert first["candidate_sha256"] == CANDIDATE
    assert first["reviewer_receipt_sha256"] == accept_review._receipt_sha256(receipt)

    replay = _dispose(driver, receipt)
    assert replay["ok"] is True and replay["idempotent"] is True
    assert replay["receipt_sha256"] == first["receipt_sha256"]

    conflict = _dispose(driver, receipt, disposition="confirmed")
    assert conflict["ok"] is False
    assert conflict["error"] == "review_finding_disposition_conflict"

    stored = driver.review_finding_dispositions(
        task_id="TARGET", request_id="target-request"
    )
    assert [row["receipt_sha256"] for row in stored] == [first["receipt_sha256"]]
    assert stored[0]["disposition"] == "dismissed"


def _dispose_as(monkeypatch, route) -> dict:
    monkeypatch.setattr(process_launcher.core, "manager_bootstrap", lambda: route)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: True)
    return process_launcher.ProcessManager.dispose_review_finding(
        SimpleNamespace(repo=Path.cwd().resolve()),
        task_id="TARGET",
        request_id="target-request",
        reviewer_request_id="review-req-1",
        finding_id=FINDING_1,
        disposition="dismissed",
        counter_evidence="measured",
        reason="",
    )


def test_non_manager_caller_is_refused(monkeypatch) -> None:
    refused = {"ok": False, "error": "verified_manager_identity_required"}
    assert _dispose_as(monkeypatch, {"role": "worker"}) == refused
    # A non-dict route refuses instead of raising AttributeError.
    assert _dispose_as(monkeypatch, "not-a-route") == refused
    assert _dispose_as(monkeypatch, None) == refused
    # A manager route that names no repository is never compared with itself.
    assert _dispose_as(
        monkeypatch,
        {"role": "manager", "manager_route": {"provider": "codex", "thread_id": "t"}},
    ) == {"ok": False, "error": "manager_repository_mismatch"}


def test_process_manager_derives_binding_server_side(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    receipt = _receipt(_finding("finding-1"))
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
        review_orchestrator, "canonical_review_db", lambda _m: repo / "review.sqlite3"
    )
    monkeypatch.setattr(process_launcher, "_parse_card", lambda _raw, _tid: _target_card())
    monkeypatch.setattr(
        accept_review,
        "reviewer_evidence",
        lambda _self, _task, _request: [
            {"request_id": "review-req-1", "usable": True, "receipt": receipt}
        ],
    )
    manager = SimpleNamespace(repo=repo, _show_task=lambda _task_id: "{}")

    result = process_launcher.ProcessManager.dispose_review_finding(
        manager,
        task_id="TARGET",
        request_id="target-request",
        reviewer_request_id="review-req-1",
        finding_id=FINDING_1,
        disposition="dismissed",
        counter_evidence="measured: unreachable",
        reason="false positive",
    )

    assert result["ok"] is True
    assert result["actor"] == "codex:thread-exact"
    assert result["candidate_sha256"] == CANDIDATE
    assert result["reviewer_receipt_sha256"] == accept_review._receipt_sha256(receipt)


# --------------------------------------------------------------------------
# accept-time binding and the preview
# --------------------------------------------------------------------------
class _DispositionStore:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def review_finding_dispositions(self, task_id, request_id):
        return [dict(row) for row in self.rows]


def _stored(tmp_path: Path, receipt: dict, **overrides) -> dict:
    stored = _dispose(_driver(tmp_path), receipt, **overrides)
    assert stored["ok"] is True
    return stored


def test_disposition_bound_to_other_candidate_or_receipt_is_ignored_at_accept(
    tmp_path: Path,
) -> None:
    receipt = _receipt(_finding("finding-1"))
    row = _stored(tmp_path, receipt)
    digest = accept_review._receipt_sha256(receipt)
    store = _DispositionStore([row])

    def bound(candidate: str, receipt_digest: str) -> list[dict]:
        return accept_review.bound_finding_dispositions(
            store,
            "TARGET",
            "target-request",
            candidate_sha256=candidate,
            receipt_sha256_by_reviewer={"review-req-1": receipt_digest},
        )

    assert [item["finding_id"] for item in bound(CANDIDATE, digest)] == [FINDING_1]
    assert bound("f" * 64, digest) == []
    assert bound(CANDIDATE, "0" * 64) == []
    assert bound("", digest) == []

    # The fold therefore keeps the blocker for every unbound disposition.
    verdict = _fold(
        [_report(_finding("finding-1"))],
        finding_dispositions=bound(CANDIDATE, "0" * 64),
    )
    assert f"refinement_required:{FINDING_1}" in verdict["blocking_evidence"]


def test_accept_preview_fold_shows_each_disposition_and_lifts_only_dismissed(
    tmp_path: Path,
) -> None:
    receipt = _receipt(_finding("finding-1"), _finding("finding-2"))
    dismissed = _stored(tmp_path, receipt)
    confirmed = _stored(
        tmp_path, receipt, finding_id=FINDING_2, disposition="confirmed",
        counter_evidence="",
    )
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
    profile = qe.resolve_risk_profile(qe.RISK_MEDIUM)
    dispositions = accept_review.bound_finding_dispositions(
        _DispositionStore([dismissed, confirmed]),
        "TARGET",
        "target-request",
        candidate_sha256=accept_review._target_candidate_sha256(_target_card()),
        receipt_sha256_by_reviewer=accept_review._sealed_receipt_sha256(reviewers),
    )
    fold = accept_review.fold_accept_blockers(
        reviewers=reviewers,
        reviewer_request_ids=None,
        risk_profile=profile,
        terminal_substatus="review_ready",
        finding_dispositions=dispositions,
    )

    refinement = [row for row in fold["blockers"] if row["kind"] == "refinement_required"]
    assert [row["detail"] for row in refinement] == [FINDING_2]
    assert refinement[0]["disposition"] == "confirmed"
    assert refinement[0]["disposition_receipt_sha256"] == confirmed["receipt_sha256"]
    assert fold["finding_dispositions"] == [
        {
            "finding_id": FINDING_1,
            "reviewer_request_id": "review-req-1",
            "lens": qe.LENS_CORRECTNESS,
            "severity": qe.SEVERITY_LOW,
            "disposition": "dismissed",
            "receipt_sha256": dismissed["receipt_sha256"],
            "blocker_lifted": True,
        },
        {
            "finding_id": FINDING_2,
            "reviewer_request_id": "review-req-1",
            "lens": qe.LENS_CORRECTNESS,
            "severity": qe.SEVERITY_LOW,
            "disposition": "confirmed",
            "receipt_sha256": confirmed["receipt_sha256"],
            "blocker_lifted": False,
        },
    ]

    # Without dispositions the preview fold is exactly what it always was.
    plain = accept_review.fold_accept_blockers(
        reviewers=reviewers,
        reviewer_request_ids=None,
        risk_profile=profile,
        terminal_substatus="review_ready",
    )
    assert "finding_dispositions" not in plain
    assert [
        row["detail"]
        for row in plain["blockers"]
        if row["kind"] == "refinement_required"
    ] == [FINDING_1, FINDING_2]
    assert all("disposition" not in row for row in plain["blockers"])


# --------------------------------------------------------------------------
# the preview and the verdict share one lift rule
# --------------------------------------------------------------------------
_DISMISSED_ROW = {
    "reviewer_request_id": "review-req-1",
    "finding_id": FINDING_1,
    "disposition": "dismissed",
    "receipt_sha256": "e" * 64,
}


def _reviewer_row(receipt: dict, request_id: str = "review-req-1") -> dict:
    return {
        "task_id": "QUALITY_REVIEW_TARGET",
        "lens": qe.LENS_CORRECTNESS,
        "request_id": request_id,
        "state": "review_ready",
        "usable": True,
        "receipt": receipt,
    }


def _preview(reviewers: list[dict], dispositions: list[dict]) -> dict:
    return accept_review.fold_accept_blockers(
        reviewers=reviewers,
        reviewer_request_ids=None,
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        terminal_substatus="review_ready",
        finding_dispositions=dispositions,
    )


def _preview_refinements(fold: dict) -> list[str]:
    return [row["detail"] for row in fold["blockers"] if row["kind"] == "refinement_required"]


def test_shared_id_across_two_reports_is_kept_by_fold_and_preview() -> None:
    verdict = _fold(
        [_report(_finding("finding-1")), _report(_finding("finding-1"))],
        finding_dispositions=[_DISMISSED_ROW],
    )
    assert f"refinement_required:{FINDING_1}" in verdict["blocking_evidence"]

    reviewers = [
        _reviewer_row(_receipt(_finding("finding-1"))),
        _reviewer_row(
            _receipt(_finding("finding-1"), reviewer_request_id="review-req-2"),
            request_id="review-req-2",
        ),
    ]
    fold = _preview(reviewers, [_DISMISSED_ROW])
    assert _preview_refinements(fold) == [FINDING_1, FINDING_1]
    assert [row["blocker_lifted"] for row in fold["finding_dispositions"]] == [False]


def test_duplicate_id_inside_one_report_keeps_both_blockers_on_both_surfaces() -> None:
    verdict = _fold(
        [_report(_finding("finding-1"), _finding("finding-1"))],
        finding_dispositions=[_DISMISSED_ROW],
    )
    # The verdict de-duplicates blocker ids; neither finding is lifted.
    assert f"refinement_required:{FINDING_1}" in verdict["blocking_evidence"]
    assert verdict["passed"] is False
    assert "manager_dismissed_findings" not in verdict

    receipt = _receipt(_finding("finding-1"), _finding("finding-1"))
    fold = _preview([_reviewer_row(receipt)], [_DISMISSED_ROW])
    assert _preview_refinements(fold) == [FINDING_1, FINDING_1]
    assert all(row["blocker_lifted"] is False for row in fold["finding_dispositions"])


def test_dispose_on_a_duplicated_finding_id_is_refused(tmp_path: Path) -> None:
    receipt = _receipt(_finding("finding-1"), _finding("finding-1"))

    assert _dispose(_driver(tmp_path), receipt) == {
        "ok": False,
        "error": "review_finding_disposition_ambiguous",
    }


def test_dismissal_of_a_finding_that_is_not_a_blocker_is_refused(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    observation = _receipt(_finding("finding-1"))
    observation["report"]["findings"][0]["disposition"] = "observation"
    observation["report"]["findings"][0]["actionable"] = False
    other_lens = _receipt(_finding("finding-1"))
    other_lens["report"]["lens"] = qe.LENS_CODE_QUALITY
    other_id = f"reviewer:{qe.LENS_CODE_QUALITY}:finding-1"

    refused = {"ok": False, "error": "review_finding_disposition_not_a_blocker"}
    assert _dispose(driver, observation) == refused
    assert _dispose(driver, other_lens, finding_id=other_id) == refused
    assert driver.review_finding_dispositions(
        task_id="TARGET", request_id="target-request"
    ) == []

    # ``confirmed`` stays allowed for any finding.
    confirmed = _dispose(
        driver, other_lens, finding_id=other_id, disposition="confirmed",
        counter_evidence="",
    )
    assert confirmed["ok"] is True and confirmed["disposition"] == "confirmed"


def test_preview_blocker_lifted_follows_the_lens_condition() -> None:
    receipt = _receipt(_finding("finding-1"))
    receipt["report"]["lens"] = qe.LENS_CODE_QUALITY
    row = {**_DISMISSED_ROW, "finding_id": f"reviewer:{qe.LENS_CODE_QUALITY}:finding-1"}
    reviewer = {**_reviewer_row(receipt), "lens": qe.LENS_CODE_QUALITY}

    fold = _preview([reviewer], [row])
    assert [item["blocker_lifted"] for item in fold["finding_dispositions"]] == [False]


def test_preview_keys_dispositions_on_the_untruncated_finding_id() -> None:
    long_id = "x" * 320
    receipt = _receipt(_finding(long_id))
    finding_id = f"reviewer:{qe.LENS_CORRECTNESS}:{long_id}"
    fold = _preview(
        [_reviewer_row(receipt)],
        [{**_DISMISSED_ROW, "finding_id": finding_id[:300]}],
    )

    # A row naming the truncated id binds to nothing; the blocker detail alone
    # is truncated.
    assert "finding_dispositions" not in fold
    assert _preview_refinements(fold) == [finding_id[:300]]
