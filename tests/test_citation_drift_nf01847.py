from __future__ import annotations

import subprocess
from pathlib import Path

from aiworkhub import completion_inbox, quality_evidence

OLD_LINES = [f"line_{n} = {n}" for n in range(1, 21)]


def _git_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "--", *files], check=True)
    return root


def _pair(tmp_path: Path, new_lines: list[str], doc: str, extra=None):
    old = "\n".join(OLD_LINES) + "\n"
    baseline = tmp_path / "base"
    baseline.mkdir()
    (baseline / "src").mkdir()
    (baseline / "src" / "m.py").write_text(old, encoding="utf-8")
    files = {"src/m.py": "\n".join(new_lines) + "\n", "docs/d.md": doc}
    files.update(extra or {})
    candidate = _git_repo(tmp_path / "cand", files)
    return baseline, candidate


def _inserted() -> list[str]:
    return ["new_a", "new_b", "new_c", *OLD_LINES]


def test_inserted_lines_above_citation_yield_moved_row(tmp_path):
    baseline, candidate = _pair(tmp_path, _inserted(), "See src/m.py:10 for it.\n")
    rows = quality_evidence.run_citation_drift_checks(
        baseline, candidate, changed_paths=["src/m.py"]
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "moved"
    assert row["replacement"] == "src/m.py:13"
    assert (row["doc"], row["doc_line"], row["old_line"], row["new_line"]) == (
        "docs/d.md", 1, 10, 13,
    )
    assert row["target_path"] == "src/m.py"
    assert row["citation"] == "src/m.py:10"


def test_edited_cited_line_is_target_changed(tmp_path):
    new = list(OLD_LINES)
    new[9] = "line_10 = 'edited'"
    baseline, candidate = _pair(tmp_path, new, "See `src/m.py:10`.\n")
    rows = quality_evidence.run_citation_drift_checks(
        baseline, candidate, changed_paths=["src/m.py"]
    )
    assert [(r["status"], r["replacement"]) for r in rows] == [("target_changed", None)]


def test_range_citation_renumbers_both_ends(tmp_path):
    baseline, candidate = _pair(tmp_path, _inserted(), "Range src/m.py:5-8 here.\n")
    rows = quality_evidence.run_citation_drift_checks(
        baseline, candidate, changed_paths=["src/m.py"]
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "moved"
    assert rows[0]["replacement"] == "src/m.py:8-11"


def test_unchanged_citation_and_unchanged_file_yield_no_row(tmp_path):
    new = list(OLD_LINES) + ["appended"]
    baseline, candidate = _pair(
        tmp_path, new, "Stable src/m.py:10 and other src/n.py:3.\n",
        extra={"src/n.py": "x\ny\nz\n"},
    )
    (baseline / "src" / "n.py").write_text("x\ny\nz\n", encoding="utf-8")
    rows = quality_evidence.run_citation_drift_checks(
        baseline, candidate, changed_paths=["src/m.py", "src/n.py"]
    )
    assert rows == []


def test_path_prefix_collision_does_not_match(tmp_path):
    baseline, candidate = _pair(tmp_path, _inserted(), "Other other_src/m.py:10 only.\n")
    rows = quality_evidence.run_citation_drift_checks(
        baseline, candidate, changed_paths=["src/m.py"]
    )
    assert rows == []


def test_added_and_markdown_paths_are_skipped(tmp_path):
    baseline, candidate = _pair(tmp_path, _inserted(), "src/m.py:10 docs/d.md:1\n")
    rows = quality_evidence.run_citation_drift_checks(
        baseline, candidate, changed_paths=["docs/d.md", "src/added.py"]
    )
    assert rows == []


def test_non_git_candidate_returns_empty(tmp_path):
    baseline = tmp_path / "base"
    candidate = tmp_path / "cand"
    for root, lines in ((baseline, OLD_LINES), (candidate, _inserted())):
        (root / "src").mkdir(parents=True)
        (root / "src" / "m.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (candidate / "d.md").write_text("src/m.py:10\n", encoding="utf-8")
    assert quality_evidence.run_citation_drift_checks(
        baseline, candidate, changed_paths=["src/m.py"]
    ) == []


def _gate(tmp_path: Path, name: str, doc: str):
    sub = tmp_path / name
    sub.mkdir()
    baseline, candidate = _pair(sub, _inserted(), doc)
    return quality_evidence.run_review_ready_quality_gate(
        candidate,
        changed_paths=["src/m.py"],
        canonical_repo=baseline,
    )


def test_review_ready_gate_stores_rows_without_changing_verdict(tmp_path):
    with_doc = _gate(tmp_path, "a", "See src/m.py:10.\n")
    without_doc = _gate(tmp_path, "b", "Nothing cited.\n")
    assert [r["replacement"] for r in with_doc["review_ready_citation_drift"]] == [
        "src/m.py:13"
    ]
    assert without_doc["review_ready_citation_drift"] == []
    assert with_doc["passed"] == without_doc["passed"]
    assert with_doc.get("blocking_checks") == without_doc.get("blocking_checks")
    assert with_doc["review_risk_profile"]["effective_tier"] == (
        without_doc["review_risk_profile"]["effective_tier"]
    )
    assert with_doc["review_ready_destructive_diff_blockers"] == (
        without_doc["review_ready_destructive_diff_blockers"]
    )


def test_completion_inbox_packet_carries_citation_drift(tmp_path):
    gate = _gate(tmp_path, "a", "See src/m.py:10.\n")
    card = {
        "task_id": "t1",
        "status": "review_ready",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {"quality_gate": gate, "changed_paths": ["src/m.py"]},
        },
    }
    packet = completion_inbox.review_packet("r1", card=card)
    rows = packet["gates"]["citation_drift"]
    assert rows == [
        {
            "doc": "docs/d.md",
            "doc_line": 1,
            "citation": "src/m.py:10",
            "replacement": "src/m.py:13",
            "status": "moved",
        }
    ]
