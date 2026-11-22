"""NF-2026-02344: the review_ready gate inherits the canonical policy.

A sparse worker tree omits ``.aiworkhub/quality.json`` on disk. Unless the
candidate changes that policy, the review_ready gate must load the declared
checks from the canonical repository -- exactly as accept does -- so a
candidate that violates one is refused before paid review, not first at accept.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

qe = importlib.import_module("aiworkhub.quality_evidence")

FAILING_CHECK_ID = "declared-invariants"
CANDIDATE_CHECK_ID = "candidate-own-check"


def _write_policy(root: Path, check_id: str, exit_code: int) -> None:
    config = root / ".aiworkhub" / "quality.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "id": check_id,
                        "kind": "test",
                        "command": [
                            sys.executable,
                            "-c",
                            f"raise SystemExit({exit_code})",
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    canonical = tmp_path / "canonical"
    (canonical / "src").mkdir(parents=True)
    (canonical / "src" / "service.py").write_text("x = 1\n", encoding="utf-8")
    _write_policy(canonical, FAILING_CHECK_ID, 1)
    candidate = tmp_path / "candidate"
    (candidate / "src").mkdir(parents=True)
    (candidate / "src" / "service.py").write_text("x = 2\n", encoding="utf-8")
    return canonical, candidate


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _check_ids(gate: dict) -> set[str]:
    return {str(row.get("check_id")) for row in gate.get("checks") or []}


def test_sparse_candidate_fails_canonical_declared_check_at_review_ready(tmp_path):
    canonical, candidate = _trees(tmp_path)
    assert not (candidate / ".aiworkhub" / "quality.json").exists()
    before = _snapshot(candidate)

    gate = qe.run_review_ready_quality_gate(
        candidate, changed_paths=["src/service.py"], canonical_repo=canonical
    )

    assert gate["passed"] is False
    assert FAILING_CHECK_ID in (gate.get("blocking_checks") or [])
    assert gate["review_ready_policy_source"] == "canonical_inherited"
    # The policy is read from canonical; nothing is written into the candidate.
    assert _snapshot(candidate) == before


def test_without_canonical_repo_keeps_candidate_policy(tmp_path):
    _canonical, candidate = _trees(tmp_path)

    gate = qe.run_review_ready_quality_gate(candidate, changed_paths=["src/service.py"])

    assert FAILING_CHECK_ID not in _check_ids(gate)
    assert FAILING_CHECK_ID not in (gate.get("blocking_checks") or [])
    assert gate["review_ready_policy_source"] == "candidate"


def test_candidate_that_changes_policy_reads_its_own(tmp_path):
    canonical, candidate = _trees(tmp_path)
    _write_policy(candidate, CANDIDATE_CHECK_ID, 0)
    before = _snapshot(candidate)

    gate = qe.run_review_ready_quality_gate(
        candidate,
        changed_paths=[".aiworkhub/quality.json", "src/service.py"],
        canonical_repo=canonical,
    )

    assert gate["review_ready_policy_source"] == "candidate"
    ids = _check_ids(gate)
    assert CANDIDATE_CHECK_ID in ids
    assert FAILING_CHECK_ID not in ids
    assert FAILING_CHECK_ID not in (gate.get("blocking_checks") or [])
    assert _snapshot(candidate) == before
