"""NF-2026-02439: honest callback review digest and review_phase.

* ``open_finding_count`` is never 0 for "unknown": stored reviewer reports with
  undisposed findings are counted even when the accept preview was not run, and
  a count that cannot be evaluated is ``None`` (with ``open_finding_ids`` None).
* A review_ready callback member carries ``review_phase``
  (``worker_done`` | ``review_complete``) and ``manager_ready`` derived from the
  card's ``manager_ready_receipt`` for the same request/epoch. The transition
  string stays ``review_ready``.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import completion_inbox, core, manager_callback_wait  # noqa: E402

REQUEST_ID = "R-PHASE-1"
TASK_ID = "T-PHASE-1"
UNEVALUATED = "accept_preview_not_run_in_callback_wait"


def _reports(count: int = 3) -> list[dict]:
    return [
        {
            "lens": "correctness",
            "reviewer_request_id": "RV-1",
            "findings": [
                {"id": f"F-{i}", "severity": "high", "disposition": "defect",
                 "category": "general", "summary": f"defect {i}",
                 "evidence": f"src/aiworkhub/mod_{i}.py:{i + 1}"}
                for i in range(count)
            ],
        }
    ]


def _card(*, reports: list[dict] | None = None, receipt: dict | None = None,
          request_id: str = REQUEST_ID) -> dict:
    card: dict = {
        "task_id": TASK_ID,
        "runner": "claude_coding",
        "topic": "coding",
        "status": "review",
        "claim_epoch": 1,
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {"request_id": request_id, "task_id": TASK_ID},
                "changed_paths": ["src/aiworkhub/mod_0.py"],
                "validation": [
                    {"declared_command": "pytest a", "returncode": 0, "stdout_tail": "ok"}
                ],
                "quality_gate": {
                    "passed": True,
                    "blocking_checks": [],
                    "review_risk_profile": {
                        "effective_tier": "medium",
                        "required_reviewer_lenses": ["correctness"],
                    },
                    "quality_verdict": (
                        {"reviewer_reports": reports} if reports is not None else {}
                    ),
                },
            },
        },
    }
    if receipt is not None:
        card["manager_ready_receipt"] = receipt
    return card


def _receipt(*, request_id: str = REQUEST_ID, claim_epoch: str = "1") -> dict:
    return {
        "manager_ready": {
            "schema_id": "aiworkhub.manager_ready_receipt.v1",
            "target_task_id": TASK_ID,
            "target_request_id": request_id,
            "claim_epoch": claim_epoch,
            "reviews": [],
        }
    }


def _patch_show(monkeypatch, card: dict) -> None:
    payload = {"returncode": 0, "stdout": json.dumps(card)}
    monkeypatch.setattr(core, "show_task", lambda _task_id: copy.deepcopy(payload))


def _member() -> dict:
    return {"task_id": TASK_ID, "state": "review_ready", "event_id": "e1",
            "request_id": REQUEST_ID}


def test_unevaluated_preview_still_counts_open_findings_from_stored_reports():
    digest = completion_inbox.review_digest(
        REQUEST_ID, card=_card(reports=_reports(3)), task_id=TASK_ID,
        unevaluated_reason=UNEVALUATED,
    )
    assert digest["verdict"] == "unevaluated"
    assert digest["unevaluated_reason"] == UNEVALUATED
    assert digest["open_finding_count"] == 3
    assert digest["open_finding_ids"] == [
        "reviewer:correctness:F-0",
        "reviewer:correctness:F-1",
        "reviewer:correctness:F-2",
    ]


def test_unevaluable_count_is_null_never_zero():
    digest = completion_inbox.review_digest(
        REQUEST_ID, card=_card(), task_id=TASK_ID, unevaluated_reason=UNEVALUATED
    )
    assert digest["verdict"] == "unevaluated"
    assert digest["open_finding_count"] is None
    assert digest["open_finding_ids"] is None
    assert digest["unevaluated_reason"] == UNEVALUATED


def test_evaluated_preview_with_no_findings_is_a_real_zero():
    digest = completion_inbox.review_digest(
        REQUEST_ID, card=_card(), task_id=TASK_ID, reviewer_reports=[],
        accept_preview={"evaluated": True, "blocked": False, "blockers": [],
                        "reviewer_request_ids": [], "per_lens": []},
    )
    assert digest["open_finding_count"] == 0
    assert digest["open_finding_ids"] == []


def test_member_digest_for_a_review_ready_callback_reports_the_stored_findings(monkeypatch):
    _patch_show(monkeypatch, _card(reports=_reports(3)))
    digest = manager_callback_wait._member_review_digest(_member())
    assert digest["open_finding_count"] == 3


def test_phase_is_worker_done_without_a_receipt(monkeypatch):
    _patch_show(monkeypatch, _card())
    assert manager_callback_wait._member_review_phase(_member()) == {
        "review_phase": "worker_done",
        "manager_ready": False,
    }


def test_phase_is_review_complete_with_a_receipt_for_the_same_request_and_epoch(monkeypatch):
    _patch_show(monkeypatch, _card(receipt=_receipt()))
    assert manager_callback_wait._member_review_phase(_member()) == {
        "review_phase": "review_complete",
        "manager_ready": True,
    }


def test_phase_ignores_a_receipt_for_another_request_or_epoch(monkeypatch):
    for receipt in (_receipt(request_id="R-OLD"), _receipt(claim_epoch="0")):
        _patch_show(monkeypatch, _card(receipt=receipt))
        assert manager_callback_wait._member_review_phase(_member()) == {
            "review_phase": "worker_done",
            "manager_ready": False,
        }
    # A receipt for the card's own request must still not satisfy a member that
    # names a different request.
    _patch_show(monkeypatch, _card(receipt=_receipt()))
    other = {**_member(), "request_id": "R-OTHER"}
    assert manager_callback_wait._member_review_phase(other)["manager_ready"] is False


def test_unreadable_card_is_not_ready(monkeypatch):
    monkeypatch.setattr(
        core, "show_task",
        lambda _task_id: {"returncode": 0, "stdout": f"Task not found: {TASK_ID}"},
    )
    assert manager_callback_wait._member_review_phase(_member()) == {
        "review_phase": "worker_done",
        "manager_ready": False,
    }


def test_two_missing_request_ids_are_not_a_match(monkeypatch):
    _patch_show(monkeypatch, _card(request_id="", receipt=_receipt(request_id="")))
    member = {**_member(), "request_id": ""}
    assert manager_callback_wait._member_review_phase(member) == {
        "review_phase": "worker_done",
        "manager_ready": False,
    }
    del member["request_id"]
    assert manager_callback_wait._member_review_phase(member)["manager_ready"] is False


def test_attach_sets_phase_on_review_ready_members_only_and_keeps_the_transition(monkeypatch):
    _patch_show(monkeypatch, _card(reports=_reports(3), receipt=_receipt()))
    members = [
        _member(),
        {"task_id": "B", "state": "worker_failed", "event_id": "e2", "request_id": "RB"},
    ]
    other_before = json.dumps(members[1], sort_keys=True)
    manager_callback_wait._attach_review_digests(members)
    assert members[0]["state"] == "review_ready"
    assert members[0]["review_phase"] == "review_complete"
    assert members[0]["manager_ready"] is True
    assert members[0]["review_digest"]["open_finding_count"] == 3
    assert json.dumps(members[1], sort_keys=True) == other_before


def test_a_failing_phase_lookup_is_fail_closed_and_non_fatal(monkeypatch):
    def boom(_entry):
        raise RuntimeError("boom")

    monkeypatch.setattr(manager_callback_wait, "_member_review_phase", boom)
    monkeypatch.setattr(manager_callback_wait, "_member_review_digest",
                        lambda _entry: {"ok": True})
    members = [_member()]
    manager_callback_wait._attach_review_digests(members)
    assert members[0]["review_phase"] == "worker_done"
    assert members[0]["manager_ready"] is False
    assert members[0]["review_digest"] == {"ok": True}
