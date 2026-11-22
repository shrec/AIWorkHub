"""NF-2026-02370: citation drift in a doc outside allowed_writes is not actionable.

The review-ready gate counted every drifted Markdown citation, including ones
in docs the card could never edit. Rows are now partitioned by the card's own
``allowed_writes`` with the worker write gate's matcher: rows whose citing doc
is outside the scope go to ``review_ready_citation_drift_outside_scope`` (and
``gates.citation_drift_outside_scope`` in the packet) and never count toward
the actionable rows, blockers or verdict. In-scope rows are exactly today's.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aiworkhub import completion_inbox, quality_evidence

OLD_LINES = [f"line_{n} = {n}" for n in range(1, 21)]
NEW_LINES = ["new_a", "new_b", "new_c", *OLD_LINES]
DOCS = {"docs/in.md": "See src/m.py:10 here.\n", "notes/out.md": "Also src/m.py:10.\n"}


def _pair(tmp_path: Path, docs: dict[str, str]) -> tuple[Path, Path]:
    baseline = tmp_path / "base"
    (baseline / "src").mkdir(parents=True)
    (baseline / "src" / "m.py").write_text("\n".join(OLD_LINES) + "\n", encoding="utf-8")
    candidate = tmp_path / "cand"
    files = {"src/m.py": "\n".join(NEW_LINES) + "\n", **docs}
    for relative, text in files.items():
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(candidate)], check=True)
    subprocess.run(["git", "-C", str(candidate), "add", "--", *files], check=True)
    return baseline, candidate


def _gate(tmp_path: Path, name: str, *, docs=DOCS, card=None) -> dict:
    sub = tmp_path / name
    sub.mkdir()
    baseline, candidate = _pair(sub, docs)
    return quality_evidence.run_review_ready_quality_gate(
        candidate, card=card, changed_paths=["src/m.py"], canonical_repo=baseline
    )


def _card(allowed_writes) -> dict:
    return {"task_id": "t1", "allowed_writes": allowed_writes}


@pytest.fixture(autouse=True)
def _no_live_ci(monkeypatch):
    monkeypatch.setattr(
        completion_inbox.ci_status,
        "base_ci_section",
        lambda *_a, **_k: {"state": "unavailable", "reason": "stubbed", "warnings": []},
    )


def test_outside_scope_row_is_split_off_and_in_scope_row_is_unchanged(tmp_path):
    unscoped = _gate(tmp_path, "a")
    scoped = _gate(tmp_path, "b", card=_card(["src/m.py", "docs/in.md"]))
    # Without a scope both rows stay actionable, exactly as before.
    assert [r["doc"] for r in unscoped["review_ready_citation_drift"]] == [
        "docs/in.md", "notes/out.md",
    ]
    assert unscoped["review_ready_citation_drift_outside_scope"] == []
    assert scoped["review_ready_citation_drift"] == [
        row for row in unscoped["review_ready_citation_drift"] if row["doc"] == "docs/in.md"
    ]
    assert scoped["review_ready_citation_drift_outside_scope"] == [
        row for row in unscoped["review_ready_citation_drift"] if row["doc"] == "notes/out.md"
    ]


def test_outside_scope_rows_never_touch_count_blockers_or_verdict(tmp_path):
    scope = _card(["src/m.py", "docs/in.md"])
    both = _gate(tmp_path, "a", card=scope)
    in_only = _gate(tmp_path, "b", docs={"docs/in.md": DOCS["docs/in.md"]}, card=scope)
    assert len(both["review_ready_citation_drift"]) == len(
        in_only["review_ready_citation_drift"]
    ) == 1
    assert both["passed"] == in_only["passed"]
    assert both.get("blocking_checks") == in_only.get("blocking_checks")
    assert both["review_ready_destructive_diff_blockers"] == (
        in_only["review_ready_destructive_diff_blockers"]
    )
    assert both["review_risk_profile"]["effective_tier"] == (
        in_only["review_risk_profile"]["effective_tier"]
    )


def test_a_glob_scope_uses_the_write_gate_matcher(tmp_path):
    # ``docs/*.md`` names docs/in.md; a lone ``*`` never crosses ``/``.
    gate = _gate(
        tmp_path, "a",
        docs={**DOCS, "docs/sub/deep.md": "src/m.py:10\n"},
        card=_card(["src/m.py", "docs/*.md"]),
    )
    assert [r["doc"] for r in gate["review_ready_citation_drift"]] == ["docs/in.md"]
    assert sorted(r["doc"] for r in gate["review_ready_citation_drift_outside_scope"]) == [
        "docs/sub/deep.md", "notes/out.md",
    ]


def _packet(gate: dict, allowed_writes) -> dict:
    card = {
        "task_id": "t1",
        "status": "review_ready",
        "allowed_writes": allowed_writes,
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {"quality_gate": gate, "changed_paths": ["src/m.py"]},
        },
    }
    return completion_inbox.review_packet("r1", card=card)


_IN_ROW = {
    "doc": "docs/in.md", "doc_line": 1, "citation": "src/m.py:10",
    "replacement": "src/m.py:13", "status": "moved",
}
_OUT_ROW = {**_IN_ROW, "doc": "notes/out.md"}


def test_packet_reports_outside_rows_only_under_outside_scope(tmp_path):
    scope = ["src/m.py", "docs/in.md"]
    packet = _packet(_gate(tmp_path, "a", card=_card(scope)), scope)
    gates = packet["gates"]
    assert gates["citation_drift"] == [_IN_ROW]
    assert gates["citation_drift_outside_scope"] == [_OUT_ROW]
    assert gates["quality_gate_blockers"] == []


def test_packet_repartitions_a_gate_recorded_before_the_split(tmp_path):
    legacy = _gate(tmp_path, "a")
    legacy.pop("review_ready_citation_drift_outside_scope")
    packet = _packet(legacy, ["src/m.py", "docs/in.md"])
    assert packet["gates"]["citation_drift"] == [_IN_ROW]
    assert packet["gates"]["citation_drift_outside_scope"] == [_OUT_ROW]


def test_packet_without_outside_rows_has_no_outside_field(tmp_path):
    packet = _packet(_gate(tmp_path, "a"), [])
    assert packet["gates"]["citation_drift"] == [_IN_ROW, _OUT_ROW]
    assert "citation_drift_outside_scope" not in packet["gates"]


def test_partition_fails_closed_toward_in_scope():
    rows = [
        {"doc": "docs/in.md"},
        {"doc": "../escape.md"},
        {"doc": ""},
        {"citation": "no doc"},
        {"doc": "notes/out.md"},
    ]
    inside, outside = quality_evidence.partition_citation_drift_by_scope(
        rows, ["docs/in.md"]
    )
    assert inside == rows[:4]
    assert outside == [rows[4]]
    assert quality_evidence.partition_citation_drift_by_scope(rows, None) == (rows, [])
    assert quality_evidence.partition_citation_drift_by_scope(rows, []) == (rows, [])
    assert quality_evidence.partition_citation_drift_by_scope(rows, "docs/in.md") == (
        rows[:4], [rows[4]],
    )
