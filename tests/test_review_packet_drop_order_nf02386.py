"""NF-2026-02386: review_packet compacts passing gate rows before reviewer evidence.

Passed destructive_diff rows and moved/ok citation_drift rows collapse to counts
first. Failed destructive diffs and broken citations are never removed by the
drop order. Reviewer findings and observations go last: their prose shrinks, but
no finding id and no dispose id is ever dropped.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import completion_inbox  # noqa: E402

from test_review_packet_bound_nf02361 import _card, _preview, _reports  # noqa: E402


def _destructive_rows(passed: int, failed: int = 0) -> list[dict[str, Any]]:
    rows = [
        {
            "check_id": f"destructive-{i:02d}",
            "status": "passed",
            "summary": "no baseline row was removed; every line of the table survives " * 3,
        }
        for i in range(passed)
    ]
    rows += [
        {
            "check_id": f"destructive-fail-{i:02d}",
            "status": "failed",
            "summary": "removed 12 rows from the baseline table without a migration",
        }
        for i in range(failed)
    ]
    return rows


def _citation_rows(moved: int, broken: int = 0) -> list[dict[str, Any]]:
    rows = [
        {
            "doc": f"docs/guide_{i:02d}.md",
            "doc_line": i + 1,
            "citation": f"src/aiworkhub/old_{i:02d}.py:{i + 40}",
            "replacement": f"src/aiworkhub/new_{i:02d}.py:{i + 40}",
            "status": "moved",
        }
        for i in range(moved)
    ]
    rows += [
        {
            "doc": "docs/broken.md",
            "doc_line": 9,
            "citation": "src/aiworkhub/gone.py:3",
            "replacement": "",
            "status": "broken",
        }
        for _ in range(broken)
    ]
    return rows


def _packet(max_bytes: int, *, passed: int = 0, failed: int = 0, moved: int = 0,
            broken: int = 0, count: int = 3, reports: Any = None) -> dict[str, Any]:
    card = _card()
    evidence = card["terminal_review"]["evidence"]
    evidence["destructive_diff_checks"] = _destructive_rows(passed, failed)
    evidence["quality_gate"]["review_ready_citation_drift"] = _citation_rows(moved, broken)
    return completion_inbox.review_packet(
        "R-1", card=card, task_id="T-1", accept_preview=_preview(),
        reviewer_reports=reports if reports is not None else _reports(count),
        max_bytes=max_bytes,
    )


def _measured(packet: dict[str, Any]) -> int:
    return len(json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _dispose_ids(packet: dict[str, Any]) -> list[str]:
    calls = packet["decision"]["dispose_calls"]
    if isinstance(calls, dict):
        return list(calls["finding_ids"])
    return [call["arguments"]["finding_id"] for call in calls]


def test_passing_rows_collapse_to_counts_and_every_finding_survives():
    packet = _packet(12000, passed=29, moved=10, count=3)
    assert packet["truncation"]["within_bound"] is True
    assert _measured(packet) <= 12000
    gates = packet["gates"]
    assert gates["destructive_diff_checks"] == []
    assert gates["destructive_diff_collapsed"] == {"passed": 29, "rows_omitted": 29}
    assert gates["citation_drift"] == []
    assert gates["citation_drift_collapsed"] == {"moved": 10, "rows_omitted": 10}
    assert [f["id"] for f in packet["review"]["findings"]] == ["F-00", "F-01", "F-02"]
    assert _dispose_ids(packet) == [f"reviewer:correctness:F-{i:02d}" for i in range(3)]


def test_thirty_eight_citation_rows_fit_the_small_bound():
    packet = _packet(5500, moved=38, count=3)
    assert packet["truncation"]["within_bound"] is True
    assert _measured(packet) <= 5500
    assert packet["gates"]["citation_drift_collapsed"]["moved"] == 38
    assert [f["id"] for f in packet["review"]["findings"]] == ["F-00", "F-01", "F-02"]


def test_thirty_eight_citation_rows_below_the_core_keep_every_id_and_name_the_section():
    packet = _packet(600, moved=38, count=3)
    assert packet["truncation"]["within_bound"] is False
    assert packet["truncation"]["irreducible_section"] in completion_inbox._PACKET_IRREDUCIBLE_SECTIONS
    assert [f["id"] for f in packet["review"]["findings"]] == ["F-00", "F-01", "F-02"]
    assert _dispose_ids(packet) == [f"reviewer:correctness:F-{i:02d}" for i in range(3)]


def test_failed_destructive_row_and_broken_citation_outlive_passed_and_moved_rows():
    packet = _packet(3000, passed=29, failed=1, moved=10, broken=1, count=3)
    gates = packet["gates"]
    assert gates["destructive_diff_collapsed"] == {"passed": 29, "rows_omitted": 29}
    assert gates["citation_drift_collapsed"] == {"moved": 10, "rows_omitted": 10}
    assert [row["check_id"] for row in gates["destructive_diff_checks"]] == [
        "destructive-fail-00"
    ]
    assert [row["status"] for row in gates["citation_drift"]] == ["broken"]


def test_findings_keep_every_id_and_dispose_id_when_the_bound_cannot_be_met():
    long_reports = [{
        "lens": "correctness",
        "reviewer_request_id": "RV-1",
        "findings": [
            {"id": f"F-{i:02d}", "severity": "medium", "disposition": "defect",
             "category": "general", "summary": "long prose " * 40,
             "evidence": "src/aiworkhub/mod.py:1 " * 20}
            for i in range(3)
        ],
    }]
    packet = _packet(1000, reports=long_reports)
    assert [f["id"] for f in packet["review"]["findings"]] == ["F-00", "F-01", "F-02"]
    assert all(f["summary"].endswith("…") for f in packet["review"]["findings"])
    assert _dispose_ids(packet) == [f"reviewer:correctness:F-{i:02d}" for i in range(3)]
    folded = packet["decision"]["dispose_calls"]
    assert folded["count"] == 3
    assert folded["omitted_finding_id_count"] == 0
    assert packet["truncation"]["within_bound"] is False
    assert packet["truncation"]["irreducible_section"] in completion_inbox._PACKET_IRREDUCIBLE_SECTIONS


def test_gate_collapse_comes_first_and_reviewer_evidence_last_in_the_declared_order():
    order = completion_inbox._PACKET_DROP_ORDER
    assert order[0] == "gate_rows_non_blocking"
    assert order[-2:] == ("reviewer_observations_prose", "reviewer_findings_prose")
    assert "reviewer_observations" not in order
    assert "reviewer_findings" not in order


def test_a_bound_below_the_core_names_the_section_that_could_not_shrink():
    packet = _packet(1000, count=3)
    assert packet["truncation"]["within_bound"] is False
    assert packet["truncation"]["irreducible_section"]
