"""NF-2026-01716: review_packet lists the reviewers' defect findings and lenses."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import completion_inbox, process_launcher_accept_review  # noqa: E402


def _card() -> dict:
    return {
        "task_id": "T-NF",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "status": "review",
        "risk_tier": "high",
        "claim_epoch": 4,
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {"request_id": "R-NF", "task_id": "T-NF"},
                "quality_gate": {
                    "passed": True,
                    "quality_verdict": {
                        "lenses": [
                            {"lens": "correctness", "status": "skipped",
                             "finding_ids": [], "observation_ids": []},
                        ],
                        "refine_required": False,
                    },
                },
            },
        },
        "manager_ready_receipt": {
            "manager_ready": {
                "schema_id": "aiworkhub.manager_ready_receipt.v1",
                "target_task_id": "T-NF",
                "target_request_id": "R-NF",
                "claim_epoch": "4",
                "lenses": ["correctness"],
                "reviews": [],
            }
        },
    }


def _report() -> dict:
    return {
        "lens": "correctness",
        "reviewer_request_id": "rq-c",
        "findings": [
            {"id": "F-1", "severity": "medium", "disposition": "defect",
             "category": "general", "summary": "off by one",
             "evidence": "src/aiworkhub/service.py:42 loop bound"},
            {"id": "O-1", "severity": "low", "disposition": "observation",
             "category": "general", "summary": "style", "evidence": ""},
        ],
    }


_PREVIEW = {
    "evaluated": True,
    "blocked": True,
    "blockers": [],
    "reviewer_request_ids": ["rq-c"],
    "per_lens": [
        {"lens": "correctness", "task_id": "T-REV", "request_id": "rq-c",
         "state": "review_ready", "usable": True, "running": False,
         "required": True, "selected": True, "has_verified_report": True},
    ],
}


def test_packet_lists_reviewer_defects_with_dispose_ids_and_live_lenses():
    packet = completion_inbox.review_packet(
        "R-NF", card=_card(), task_id="T-NF",
        accept_preview=_PREVIEW, reviewer_reports=[_report()],
    )
    review = packet["review"]
    assert len(review["findings"]) == 1
    finding = review["findings"][0]
    assert finding["reviewer_request_id"] == "rq-c"
    assert finding["dispose_finding_id"] == "reviewer:correctness:F-1"
    assert review["observations"][0]["dispose_finding_id"] == "reviewer:correctness:O-1"
    assert review["lenses"] == [
        {"lens": "correctness", "state": "review_ready", "usable": True,
         "required": True, "selected": True, "finding_count": 1,
         "observation_count": 1}
    ]


def test_packet_without_preview_per_lens_keeps_verdict_lenses():
    for preview in (None, {"evaluated": True, "reviewer_request_ids": []}):
        packet = completion_inbox.review_packet(
            "R-NF", card=_card(), task_id="T-NF", accept_preview=preview
        )
        assert packet["review"]["lenses"] == [
            {"lens": "correctness", "status": "skipped", "finding_count": 0,
             "observation_count": 0, "independence_rung": ""}
        ]
        assert packet["review"]["findings"] == []


def _rows() -> list[dict]:
    good = {"request_id": "rq-c", "usable": True, "receipt": {"report": _report()}}
    return [
        good,
        {**good, "request_id": "rq-unselected"},
        {**good, "request_id": "rq-unusable", "usable": False},
        {"request_id": "rq-noreceipt", "usable": True, "receipt": None},
        {"request_id": "rq-noreport", "usable": True, "receipt": {"report": "x"}},
    ]


def test_preview_reviewer_reports_keeps_only_usable_selected_reports(monkeypatch):
    monkeypatch.setattr(
        process_launcher_accept_review, "reviewer_evidence", lambda *_a: _rows()
    )
    ids = ["rq-c", "rq-unusable", "rq-noreceipt", "rq-noreport"]
    reports = process_launcher_accept_review.preview_reviewer_reports(
        object(), "T-NF", "R-NF", ids
    )
    assert [r["reviewer_request_id"] for r in reports] == ["rq-c"]
    assert reports[0]["lens"] == "correctness"


def test_preview_reviewer_reports_never_raises(monkeypatch):
    def boom(*_args):
        raise RuntimeError("ledger gone")

    monkeypatch.setattr(process_launcher_accept_review, "reviewer_evidence", boom)
    assert process_launcher_accept_review.preview_reviewer_reports(
        object(), "T-NF", "R-NF", ["rq-c"]
    ) == []


def test_packet_tolerates_non_list_findings_and_dispose_id_matches_display_id():
    long_id = "F-" + "x" * 300
    report = {**_report(), "findings": [{**_report()["findings"][0], "id": long_id}]}
    malformed = {"lens": "security", "reviewer_request_id": "rq-s", "findings": 7}
    packet = completion_inbox.review_packet(
        "R-NF", card=_card(), task_id="T-NF",
        accept_preview=_PREVIEW, reviewer_reports=[report, malformed],
    )
    finding = packet["review"]["findings"][0]
    assert finding["dispose_finding_id"] == f"reviewer:correctness:{finding['id']}"
    assert packet["review"]["lenses"][0]["finding_count"] == 1
