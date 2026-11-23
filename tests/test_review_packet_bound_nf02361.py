"""NF-2026-02361: ``review_packet(max_bytes=N)`` honours N.

The decision section is never dropped (NF-2026-01925), and its
``dispose_calls`` list grows by one prefilled dict per undisposed finding, so
a review with many findings overshot every small bound and reported
``within_bound: false`` about itself. Once every evidence section is gone and
the packet is still over the bound, the per-finding calls now fold into
``{count, finding_ids, omitted_finding_id_count, template}``; accept and
reject stay whole. A packet that already fits is untouched, byte for byte.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import completion_inbox, server  # noqa: E402

FINDING_COUNT = 34
_STUB_CI = {"state": "unavailable", "reason": "stubbed", "warnings": []}

# sha256 of the small fixture's packet as the pre-fix module encoded it
# (computed against the base-commit completion_inbox on the same fixture).
SMALL_PACKET_SHA256 = "4c8b5f18fe5fa9da8d5b51835949a8072763768c038ec8d041fb1900714df060"


def _card() -> dict:
    return {
        "task_id": "T-1",
        "runner": "claude_coding",
        "topic": "coding",
        "status": "review",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {"request_id": "R-1", "task_id": "T-1"},
                "changed_paths": [f"src/aiworkhub/mod_{i}.py" for i in range(40)],
                "changed_path_hashes": {"src/aiworkhub/mod_0.py": "a" * 64},
                "validation": [
                    {"declared_command": "pytest tests/test_mod.py",
                     "returncode": 0, "stdout_tail": "x" * 1900},
                ],
                "quality_gate": {
                    "passed": True,
                    "blocking_checks": [],
                    "review_risk_profile": {
                        "effective_tier": "medium",
                        "required_reviewer_lenses": ["correctness"],
                    },
                    "checks": [
                        {"check_id": f"c{i}", "kind": "lint", "status": "passed",
                         "command": "ruff", "summary": "ok"}
                        for i in range(20)
                    ],
                    "quality_verdict": {},
                },
            },
        },
    }


def _reports(count: int) -> list[dict]:
    return [
        {
            "lens": "correctness",
            "reviewer_request_id": "RV-1",
            "findings": [
                {"id": f"F-{i:02d}", "severity": "medium", "disposition": "defect",
                 "category": "general",
                 "summary": f"finding {i:02d}: the bounded path misses one case",
                 "evidence": f"src/aiworkhub/mod_{i}.py:{i + 10}"}
                for i in range(count)
            ],
        }
    ]


def _preview() -> dict:
    return {
        "evaluated": True,
        "blocked": True,
        "blockers": [
            {"kind": "required_reviewer_missing",
             "error": "required_reviewer_missing:security"},
        ],
        "reviewer_request_ids": ["RV-1"],
        "reviewer_request_id_source": "bound_children",
        "per_lens": [{"lens": "correctness", "task_id": "TR-1", "request_id": "RV-1",
                      "state": "review_ready", "usable": True, "required": True,
                      "selected": True}],
        # Present, so review_packet never asks the live CI.
        "base_ci": dict(_STUB_CI),
    }


def _packet(count: int = FINDING_COUNT, **kwargs) -> dict:
    return completion_inbox.review_packet(
        "R-1", card=_card(), task_id="T-1", accept_preview=_preview(),
        reviewer_reports=_reports(count), **kwargs,
    )


def _expected_ids(count: int = FINDING_COUNT) -> list[str]:
    return [f"reviewer:correctness:F-{i:02d}" for i in range(count)]


def _sha256(packet: dict) -> str:
    return hashlib.sha256(
        json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_the_fixture_really_overshoots_the_small_bounds():
    full = _packet()
    calls = full["decision"]["dispose_calls"]
    assert isinstance(calls, list) and len(calls) == FINDING_COUNT
    assert [c["arguments"]["finding_id"] for c in calls] == _expected_ids()
    assert full["encoded_bytes"] > 5000
    assert "compacted_sections" not in full["truncation"]


@pytest.mark.parametrize("max_bytes", [4000, 5000])
def test_small_bounds_are_honoured_by_compacting_dispose_calls(max_bytes):
    packet = _packet(max_bytes=max_bytes)
    # Every finding id is kept, so the 34 findings' prose cannot fit these bounds.
    assert packet["truncation"]["within_bound"] is False
    assert packet["truncation"]["irreducible_section"] in completion_inbox._PACKET_IRREDUCIBLE_SECTIONS
    assert packet["truncation"]["compacted_sections"] == ["decision.dispose_calls"]
    folded = packet["decision"]["dispose_calls"]
    assert folded["compacted"] is True
    assert folded["tool"] == "aiworkhub_manager_review_finding_dispose"
    assert folded["count"] == FINDING_COUNT
    kept = folded["finding_ids"]
    assert kept, folded
    assert kept == _expected_ids()[: len(kept)]
    assert folded["omitted_finding_id_count"] == FINDING_COUNT - len(kept)
    # Evidence went first, in the declared order, before any compaction.
    dropped = packet["truncation"]["dropped_sections"]
    assert dropped == [s for s in completion_inbox._PACKET_DROP_ORDER if s in dropped]
    assert "reviewer_findings" not in dropped


def test_the_larger_bound_keeps_every_finding_id_in_order():
    folded = _packet(max_bytes=5000)["decision"]["dispose_calls"]
    assert folded["finding_ids"] == _expected_ids()
    assert folded["omitted_finding_id_count"] == 0


def test_a_bound_below_the_full_id_list_keeps_every_id_and_names_the_section():
    roomy = _packet(max_bytes=5000)
    ids_bytes = len(json.dumps(_expected_ids(), separators=(",", ":")).encode("utf-8"))
    max_bytes = roomy["encoded_bytes"] - ids_bytes // 2
    packet = _packet(max_bytes=max_bytes)
    folded = packet["decision"]["dispose_calls"]
    assert folded["count"] == FINDING_COUNT
    assert folded["finding_ids"] == _expected_ids()
    assert folded["omitted_finding_id_count"] == 0
    # The 5000 bound already applied every reduction, so a lower one cannot fit.
    assert packet["truncation"]["within_bound"] is False
    assert packet["truncation"]["irreducible_section"] in completion_inbox._PACKET_IRREDUCIBLE_SECTIONS


@pytest.mark.parametrize("max_bytes", [1000, 4000, 5000])
def test_accept_and_reject_arguments_are_never_compacted(max_bytes):
    full = _packet()["decision"]
    packet = _packet(max_bytes=max_bytes)
    decision = packet["decision"]
    assert decision["accept_call"] == full["accept_call"]
    assert decision["reject_call"] == full["reject_call"]
    rest = {k: v for k, v in decision.items() if k != "dispose_calls"}
    assert rest == {k: v for k, v in full.items() if k != "dispose_calls"}


def test_an_unreachable_bound_is_reported_honestly():
    packet = _packet(max_bytes=1000)
    assert packet["truncation"]["within_bound"] is False
    assert packet["encoded_bytes"] > 1000
    assert packet["truncation"]["irreducible_section"] in completion_inbox._PACKET_IRREDUCIBLE_SECTIONS
    folded = packet["decision"]["dispose_calls"]
    assert folded["count"] == FINDING_COUNT
    assert folded["finding_ids"] == _expected_ids()
    assert folded["omitted_finding_id_count"] == 0


def test_the_template_is_a_dispose_argument_dict_without_finding_id():
    folded = _packet(max_bytes=4000)["decision"]["dispose_calls"]
    template = folded["template"]
    assert template == {
        "task_id": "T-1", "request_id": "R-1",
        "reviewer_request_id": "RV-1", "disposition": "",
    }
    params = inspect.signature(server.aiworkhub_manager_review_finding_dispose).parameters
    assert set(template) | {"finding_id"} <= set(params)
    assert folded["reviewer_request_ids"] == ["RV-1"]


def test_a_packet_that_fits_after_drops_is_not_compacted():
    full = _packet()
    packet = _packet(max_bytes=full["encoded_bytes"] - 1500)
    assert packet["truncation"]["within_bound"] is True
    assert packet["truncation"]["dropped_sections"]
    assert "compacted_sections" not in packet["truncation"]
    assert packet["decision"] == full["decision"]


def test_a_packet_already_within_the_bound_is_byte_identical():
    packet = _packet(count=2)
    assert packet["truncation"] == {
        "dropped_sections": [], "max_bytes": completion_inbox.REVIEW_PACKET_MAX_BYTES,
        "within_bound": True,
    }
    assert isinstance(packet["decision"]["dispose_calls"], list)
    assert _sha256(packet) == SMALL_PACKET_SHA256


def test_findings_of_several_reviewers_keep_their_reviewer_mapping():
    reports = _reports(FINDING_COUNT)
    reports.append({
        "lens": "security",
        "reviewer_request_id": "RV-2",
        "findings": [
            {"id": f"S-{i:02d}", "severity": "medium", "disposition": "defect",
             "category": "general", "summary": f"security finding {i:02d}",
             "evidence": f"src/aiworkhub/mod_{i}.py:{i + 20}"}
            for i in range(4)
        ],
    })
    preview = _preview()
    preview["reviewer_request_ids"] = ["RV-1", "RV-2"]
    preview["per_lens"].append({"lens": "security", "task_id": "TR-2", "request_id": "RV-2",
                                "state": "review_ready", "usable": True, "required": True,
                                "selected": True})
    packet = completion_inbox.review_packet(
        "R-1", card=_card(), task_id="T-1", accept_preview=preview,
        reviewer_reports=reports, max_bytes=5600,
    )
    # 38 findings keep every id; their prose alone exceeds this bound.
    assert packet["truncation"]["within_bound"] is False
    assert packet["truncation"]["irreducible_section"] in completion_inbox._PACKET_IRREDUCIBLE_SECTIONS
    folded = packet["decision"]["dispose_calls"]
    assert folded["template"]["reviewer_request_id"] == ""
    reviewers = folded["reviewer_request_ids"]
    assert sorted(reviewers) == ["RV-1", "RV-2"]
    kept = folded["finding_ids"]
    index = folded["finding_reviewer_index"]
    assert len(index) == len(kept)
    assert any(f.startswith("reviewer:security:") for f in kept), kept
    for finding_id, position in zip(kept, index):
        expected = "RV-2" if finding_id.startswith("reviewer:security:") else "RV-1"
        assert reviewers[position] == expected, (finding_id, position)
