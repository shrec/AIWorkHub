"""NF-2026-01741: review_packet builds the packet for every review_ready card."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import completion_inbox  # noqa: E402

_PENDING_KEYS = {
    "manager_readiness",
    "outstanding_accept_blockers",
    "outstanding_accept_blockers_evaluated",
}


def _card(*, ready: bool = True, substatus: str = "review_ready") -> dict:
    card = {
        "task_id": "T-NF",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "status": "review",
        "claim_epoch": 4,
        "terminal_review": {
            "substatus": substatus,
            "evidence": {
                "request_identity": {"request_id": "R-NF", "task_id": "T-NF"},
                "changed_paths": ["src/aiworkhub/completion_inbox.py"],
                "quality_gate": {"passed": True, "quality_verdict": {"lenses": []}},
            },
        },
    }
    if ready:
        card["manager_ready_receipt"] = {
            "manager_ready": {
                "schema_id": "aiworkhub.manager_ready_receipt.v1",
                "target_task_id": "T-NF",
                "target_request_id": "R-NF",
                "claim_epoch": "4",
                "lenses": ["correctness"],
                "reviews": [],
            }
        }
    return card


_PREVIEW = {
    "evaluated": True,
    "blocked": True,
    "blockers": [
        {"kind": "reviewer_running", "error": "correctness reviewer still running"},
        {"kind": "refinement_required", "error": "F-1 open"},
    ],
    "reviewer_request_ids": ["rq-c"],
}


def test_review_ready_without_marker_returns_full_packet_pending_with_blockers():
    packet = completion_inbox.review_packet(
        "R-NF", card=_card(ready=False), task_id="T-NF", accept_preview=_PREVIEW
    )
    assert packet["ok"] is True
    assert "error" not in packet
    assert packet["manager_readiness"] == "pending"
    assert packet["manager_ready"] is None
    # The blockers are the accept fold's own rows, not a second vocabulary.
    assert packet["outstanding_accept_blockers"] == [
        {"kind": "reviewer_running", "error": "correctness reviewer still running"},
        {"kind": "refinement_required", "error": "F-1 open"},
    ]
    assert packet["outstanding_accept_blockers"] == packet["accept_preview"]["blockers"]
    assert packet["outstanding_accept_blockers_evaluated"] is True
    # The full packet is built.
    for section in ("identity", "status", "gates", "review", "diff", "accept_preview"):
        assert section in packet
    assert packet["status"]["terminal_substatus"] == "review_ready"
    assert packet["diff"]["changed_paths"] == ["src/aiworkhub/completion_inbox.py"]


def test_pending_without_preview_names_the_blockers_unevaluated():
    packet = completion_inbox.review_packet(
        "R-NF", card=_card(ready=False), task_id="T-NF"
    )
    assert packet["ok"] is True
    assert packet["manager_readiness"] == "pending"
    assert packet["outstanding_accept_blockers"] == []
    assert packet["outstanding_accept_blockers_evaluated"] is False


def test_review_ready_with_marker_packet_is_unchanged():
    card = _card()
    packet = completion_inbox.review_packet(
        "R-NF", card=card, task_id="T-NF", accept_preview=_PREVIEW
    )
    assert packet["ok"] is True
    assert not (_PENDING_KEYS & set(packet))
    assert packet["manager_ready"] == card["manager_ready_receipt"]["manager_ready"]
    again = completion_inbox.review_packet(
        "R-NF", card=copy.deepcopy(card), task_id="T-NF", accept_preview=_PREVIEW
    )
    assert json.dumps(packet, sort_keys=True) == json.dumps(again, sort_keys=True)


def test_card_not_review_ready_keeps_todays_behaviour():
    packet = completion_inbox.review_packet(
        "R-NF", card=_card(ready=False, substatus="blocked"), task_id="T-NF"
    )
    assert not (_PENDING_KEYS & set(packet))
    assert packet["manager_ready"] == {"schema_id": "aiworkhub.manager_ready_legacy.v1"}
    refused = completion_inbox.review_packet("R-NF")
    assert refused["ok"] is False
    assert refused["error"] == "task_id_required_without_card"
