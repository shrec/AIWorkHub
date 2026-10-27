"""NF-2026-01746: candidate source evidence never exceeds the reviewer's segment bound."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import process_launcher, quality_reviewer

ProcessManager = process_launcher.ProcessManager
PATH = "src/big.py"


def _evidence(tmp_path: Path, baseline: str, candidate: str) -> dict[str, dict[str, Any]]:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    for root, text in ((repo, baseline), (workspace, candidate)):
        target = root / PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    manager = SimpleNamespace(
        repo=str(repo),
        _QUALITY_REVIEW_SOURCE_CONTEXT_LINES=ProcessManager._QUALITY_REVIEW_SOURCE_CONTEXT_LINES,
        _QUALITY_REVIEW_SOURCE_MAX_BYTES=ProcessManager._QUALITY_REVIEW_SOURCE_MAX_BYTES,
        _QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES=ProcessManager._QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES,
    )
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return ProcessManager._quality_review_source_evidence(
        manager, SimpleNamespace(path=str(workspace)), {PATH: digest}
    )


def _every_nth_changed(total: int, step: int) -> tuple[str, str]:
    baseline = "".join(f"line {number}\n" for number in range(1, total + 1))
    candidate = "".join(
        f"changed {number}\n" if number % step == 0 else f"line {number}\n"
        for number in range(1, total + 1)
    )
    return baseline, candidate


def _validated(evidence: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows = quality_reviewer._source_evidence_rows(
        evidence, [{"path": PATH, "sha256": evidence[PATH]["candidate_sha256"]}]
    )
    assert len(rows) == 1
    return rows[0]


def test_producer_uses_the_reviewer_owned_bound() -> None:
    assert process_launcher.quality_reviewer is quality_reviewer
    assert quality_reviewer.MAX_SOURCE_EVIDENCE_SEGMENTS == 200


def test_measured_2000_line_every_fifth_line_shape_is_folded_and_accepted(
    tmp_path: Path,
) -> None:
    cap = quality_reviewer.MAX_SOURCE_EVIDENCE_SEGMENTS
    baseline, candidate = _every_nth_changed(2000, 5)
    evidence = _evidence(tmp_path, baseline, candidate)
    row = evidence[PATH]
    segments = row["segments"]

    # 400 replace hunks before the fix; never more than the reviewer's bound now.
    assert len(segments) == cap
    folded = segments[-1]
    context = min(ProcessManager._QUALITY_REVIEW_SOURCE_CONTEXT_LINES, 4)
    first_folded_line = 5 * cap
    assert folded == {
        "kind": "replace",
        "candidate_start_line": first_folded_line - context,
        "candidate_end_line": 2000,
        "changed_start_line": first_folded_line,
        "changed_end_line": 2000,
        "baseline_start_line": first_folded_line,
        "baseline_end_line": 2000,
        "excerpt_bytes": 0,
        "truncated": True,
    }
    assert all(segment["changed_end_line"] < first_folded_line for segment in segments[:-1])
    assert row["diff_complete"] is False
    assert row["truncated"] is True
    omitted = int(row["omission_reason"].split(":", 1)[1])
    assert row["omission_reason"] == f"changed_hunks_omitted:{omitted}"
    assert omitted == sum(1 for segment in segments if segment["truncated"])

    validated = _validated(evidence)
    assert validated["segments"] == segments
    assert validated["diff_complete"] is False
    assert validated["truncated"] is True
    assert validated["omission_reason"] == row["omission_reason"]
    assert validated["excerpt"] == row["excerpt"]


def test_folded_mixed_kinds_become_one_replace_spanning_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quality_reviewer, "MAX_SOURCE_EVIDENCE_SEGMENTS", 2)
    baseline = "".join(f"keep {n}\n" for n in range(1, 31))
    lines = baseline.splitlines(keepends=True)
    candidate_lines = list(lines)
    candidate_lines[4] = "edited 5\n"  # replace (kept in slot 1)
    candidate_lines.insert(15, "inserted\n")  # insert (first folded)
    del candidate_lines[26]  # delete (last folded)
    evidence = _evidence(tmp_path, baseline, "".join(candidate_lines))
    segments = evidence[PATH]["segments"]
    assert [segment["kind"] for segment in segments] == ["replace", "replace"]
    assert segments[0]["truncated"] is False
    folded = segments[1]
    assert folded["truncated"] is True and folded["excerpt_bytes"] == 0
    assert folded["changed_start_line"] == 16
    assert folded["baseline_start_line"] == 16
    assert folded["baseline_end_line"] == 26
    assert folded["changed_end_line"] >= folded["changed_start_line"]
    assert evidence[PATH]["omission_reason"] == "changed_hunks_omitted:1"
    assert "inserted" not in evidence[PATH]["excerpt"]


def test_diff_at_the_bound_is_byte_identical_to_the_unbounded_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = quality_reviewer.MAX_SOURCE_EVIDENCE_SEGMENTS
    baseline, candidate = _every_nth_changed(5 * cap, 5)
    bounded = _evidence(tmp_path / "bounded", baseline, candidate)
    assert len(bounded[PATH]["segments"]) == cap
    monkeypatch.setattr(quality_reviewer, "MAX_SOURCE_EVIDENCE_SEGMENTS", cap * 10)
    unbounded = _evidence(tmp_path / "unbounded", baseline, candidate)
    assert json.dumps(bounded, sort_keys=True) == json.dumps(unbounded, sort_keys=True)
    _validated(bounded)


def test_small_diff_evidence_is_unchanged(tmp_path: Path) -> None:
    assert ProcessManager._QUALITY_REVIEW_SOURCE_CONTEXT_LINES >= 1
    evidence = _evidence(tmp_path, "a\nb\nc\n", "a\nB\nc\n")
    header = f"@@ path:{json.dumps(PATH)} candidate:1-3 change:2-2 baseline:2-2 replace @@\n"
    excerpt = header + " a\n-b\n+B\n c\n"
    assert evidence[PATH] == {
        "candidate_sha256": hashlib.sha256(b"a\nB\nc\n").hexdigest(),
        "excerpt": excerpt,
        "excerpt_bytes": len(excerpt.encode("utf-8")),
        "source_bytes": 6,
        "truncated": False,
        "diff_complete": True,
        "segments": [
            {
                "kind": "replace",
                "candidate_start_line": 1,
                "candidate_end_line": 3,
                "changed_start_line": 2,
                "changed_end_line": 2,
                "baseline_start_line": 2,
                "baseline_end_line": 2,
                "excerpt_bytes": len(excerpt.encode("utf-8")),
                "truncated": False,
            }
        ],
    }
    _validated(evidence)
