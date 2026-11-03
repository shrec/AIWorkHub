"""NF-2026-01994: a large changed file keeps the nearest neighbourhood and its
truncation is a sampling record, not a blind-review trigger."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import quality_review_scope, quality_reviewer, scoped_audit, source_graph

FAIL_CLOSED = (
    "Fail closed on unknowns: a non-empty known_unknowns list, or missing or "
    "stale changed-segment evidence for any changed path, must be escalated as "
    "a process_limit finding and can never support a clean result.\n"
)
_OUTSIDE_CALLERS = 80
_TARGET_LINE = 201


def _big_module(value: int) -> str:
    lines = ["def far_caller():", "    return target()"]
    lines += [f"# padding {number}" for number in range(3, _TARGET_LINE)]
    lines += ["def target():", f"    return {value}"]
    lines += ["def near_caller():", "    return target()"]
    lines += [f"# padding {number}" for number in range(_TARGET_LINE + 4, 400)]
    lines += ["def tail():", "    return 0"]
    assert lines[_TARGET_LINE - 1] == "def target():"
    return "\n".join(lines) + "\n"


def _outside_callers() -> str:
    rows = ["from src.big import target", ""]
    for index in range(_OUTSIDE_CALLERS):
        rows += [f"def caller_{index:03d}():", "    return target()", ""]
    return "\n".join(rows)


def _hunks() -> list[dict[str, Any]]:
    hunk = {
        "kind": "replace",
        "candidate_start_line": _TARGET_LINE + 1,
        "candidate_end_line": _TARGET_LINE + 1,
        "changed_start_line": _TARGET_LINE + 1,
        "changed_end_line": _TARGET_LINE + 1,
        "baseline_start_line": _TARGET_LINE + 1,
        "baseline_end_line": _TARGET_LINE + 1,
    }
    tail = dict(
        hunk,
        candidate_start_line=399,
        candidate_end_line=400,
        changed_start_line=400,
        changed_end_line=400,
        baseline_start_line=400,
        baseline_end_line=400,
    )
    return [hunk, tail]


def _large_file_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> dict[str, dict[str, Any]]:
    canonical = tmp_path / "canonical"
    (canonical / "src").mkdir(parents=True)
    (canonical / "src" / "big.py").write_text(_big_module(1), encoding="utf-8")
    # Sorts before src/big.py: the old file_path order kept only these callers.
    (canonical / "src" / "aaa_callers.py").write_text(
        _outside_callers(), encoding="utf-8"
    )
    graph_db = tmp_path / "source-graph.sqlite"
    monkeypatch.setattr(source_graph, "resolve_db_path", lambda _root: graph_db)
    source_graph.build_index(canonical, db_path=graph_db, incremental=False)
    candidate = tmp_path / "candidate"
    (candidate / "src").mkdir(parents=True)
    changed = candidate / "src" / "big.py"
    changed.write_text(_big_module(2), encoding="utf-8")
    digest = hashlib.sha256(changed.read_bytes()).hexdigest()
    return quality_review_scope.build_scoped_audits(
        authority_repo=canonical,
        candidate_repo=candidate,
        task_id="TASK_NF01994",
        packet_seed="request-nf01994",
        created_at="2026-11-03T00:00:00Z",
        changed_path_hashes={"src/big.py": digest},
        source_evidence={"src/big.py": {"segments": _hunks()}},
        lenses=["correctness"],
        terminal_validation=[{"declared_command": "pytest -q", "returncode": 0}],
        **kwargs,
    )


def _caller_paths(packet: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (row["path"], row["description"])
        for row in packet["impact_evidence"]
        if row["evidence_kind"] == "callers"
    ]


def test_overflowing_callers_keep_changed_file_callers_nearest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scopes = _large_file_scope(tmp_path, monkeypatch)
    packet = scopes["correctness"]["packet"]

    callers = _caller_paths(packet)
    text = "\n".join(description for _, description in callers)
    # The old file_path,line order filled every row with src/aaa_callers.py
    # (80 callers) and dropped both callers that live in the changed file.
    assert "src/big.py.near_caller" in text, text
    assert "src/big.py.far_caller" in text, text
    outside = [path for path, _ in callers if path == "src/aaa_callers.py"]
    assert 0 < len(outside) < _OUTSIDE_CALLERS
    # The remainder keeps the existing deterministic file_path,line order.
    assert "caller_000" in text
    assert f"caller_{_OUTSIDE_CALLERS - 1:03d}" not in text

    unknowns = {row["identity"]: row for row in packet["unknowns"]}
    sample = unknowns["scope-rows-truncated-caller_edges"]
    assert "nearest-ranked" in sample["why_relevant"]
    assert "Source Graph" in sample["why_relevant"]
    assert "collection=caller_edges; dropped=" in sample["why_relevant"]
    assert quality_review_scope.is_sampling_unknown(sample["identity"])
    assert quality_review_scope.scope_unknowns_are_sampling_only(scopes["correctness"])

    again = _large_file_scope(tmp_path / "again", monkeypatch)
    assert again["correctness"]["fingerprint"] == scopes["correctness"]["fingerprint"]


def test_executed_validation_is_never_dropped_for_canonical_test_targets(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "authority"
    candidate = tmp_path / "candidate"
    authority.mkdir()
    candidate.mkdir()
    hashes: dict[str, str | None] = {}
    for index in range(70):
        name = f"tests_test_z_{index:03d}.md"
        (candidate / name).write_text(f"changed {index}\n", encoding="utf-8")
        hashes[name] = hashlib.sha256((candidate / name).read_bytes()).hexdigest()

    scopes = quality_review_scope.build_scoped_audits(
        authority_repo=authority,
        candidate_repo=candidate,
        task_id="TASK_EXEC",
        packet_seed="request-exec",
        created_at="2026-11-03T00:00:00Z",
        changed_path_hashes=hashes,
        source_evidence={name: {"segments": []} for name in hashes},
        terminal_validation=[{"declared_command": "pytest -q", "returncode": 0}],
        lenses=["correctness"],
    )

    rows = scopes["correctness"]["packet"]["test_evidence"]
    assert len(rows) == scoped_audit.MAX_COLLECTION_SIZE
    executed = [
        row
        for row in rows
        if row["description"] == "Executed validation returned 0: pytest -q"
    ]
    assert len(executed) == 1
    assert executed[0]["evidence_level"] == "tested"


@pytest.mark.parametrize(
    "identity, sampling",
    [
        ("scope-rows-truncated-caller_edges", True),
        ("scope-rows-truncated-impact_evidence", True),
        ("scope-rows-truncated-test_evidence", True),
        ("scope-rows-truncated-unknowns", False),
        ("scope-rows-truncated-target_symbols", False),
        ("scope-rows-truncated-terminal_validation", False),
        ("scope-rows-truncated-edges", False),
        ("changed-lines-unresolved", False),
        ("canonical-graph-unavailable", False),
        (None, False),
    ],
)
def test_only_neighbourhood_truncation_is_a_sampling_record(
    identity: object, sampling: bool
) -> None:
    assert quality_review_scope.is_sampling_unknown(identity) is sampling


def _prompt(audit: dict[str, Any]) -> str:
    source = "changed code"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    packet = quality_reviewer.build_review_packet(
        request_id="req1",
        task_id="TASK_NF01994",
        claim_epoch=1,
        worker_provider="adapter-a",
        changed_path_hashes={"src/big.py": digest},
        objective="review the large-file change",
        acceptance=["callers stay correct"],
        required_outputs=["src/big.py"],
        source_evidence={
            "src/big.py": {
                "candidate_sha256": digest,
                "excerpt": source,
                "excerpt_bytes": len(source),
                "source_bytes": len(source),
                "truncated": False,
                "segments": [
                    dict(row, excerpt_bytes=len(source), truncated=False)
                    for row in _hunks()
                ],
            }
        },
        scoped_audits={"correctness": audit},
    )
    lens_packet = quality_reviewer.build_lens_packet(packet, lens="correctness")
    return quality_reviewer.build_review_prompt(lens_packet, lens="correctness")


def test_sampling_only_unknowns_are_reported_as_an_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = _large_file_scope(tmp_path, monkeypatch)["correctness"]
    assert {row["identity"] for row in audit["packet"]["unknowns"]} == {
        "scope-rows-truncated-caller_edges",
        "scope-rows-truncated-impact_evidence",
    }

    prompt = _prompt(audit)

    assert "Fail closed on unknowns" not in prompt
    assert "known_unknowns list" not in prompt
    assert "sampling record" in prompt
    assert "low-severity observation, never as a process_limit finding" in prompt


def test_a_non_sampling_unknown_keeps_the_fail_closed_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 70 declared validations overflow validation_expectations: not a sample.
    audit = _large_file_scope(
        tmp_path,
        monkeypatch,
        validation=[["python", f"check_{index}.py"] for index in range(70)],
    )["correctness"]
    identities = {row["identity"] for row in audit["packet"]["unknowns"]}
    assert "scope-rows-truncated-validation_expectations" in identities
    assert not quality_review_scope.scope_unknowns_are_sampling_only(audit)

    prompt = _prompt(audit)

    assert FAIL_CLOSED in prompt
    assert "Report that sampling as a low-severity observation" not in prompt


def test_caller_rank_without_changed_spans_is_a_valid_order_key() -> None:
    rank_sql, params = quality_review_scope._caller_rank_sql({})
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE edges(file_path TEXT, line INTEGER)")
    conn.execute("INSERT INTO edges VALUES ('a.py', 1)")
    # A bare integer ORDER BY term is a column index: "ORDER BY 0" raises out of range.
    rows = conn.execute(f"SELECT * FROM edges ORDER BY {rank_sql},file_path", params).fetchall()
    assert rows == [("a.py", 1)]
