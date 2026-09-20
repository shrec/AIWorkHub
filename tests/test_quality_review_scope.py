from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import quality_review_scope, source_graph


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_graph_scope_joins_candidate_symbols_callers_tests_and_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    (canonical / "src").mkdir(parents=True)
    (canonical / "tests").mkdir()
    (candidate / "src").mkdir(parents=True)
    source = (
        "def helper():\n"
        "    return 1\n\n"
        "def target():\n"
        "    return helper()\n\n"
        "def caller():\n"
        "    return target()\n"
    )
    (canonical / "src" / "mod.py").write_text(source, encoding="utf-8")
    (canonical / "tests" / "test_mod.py").write_text(
        "from src.mod import target\n\ndef test_target():\n    assert target() == 1\n",
        encoding="utf-8",
    )
    graph_db = tmp_path / "source-graph.sqlite"
    monkeypatch.setattr(source_graph, "resolve_db_path", lambda _root: graph_db)
    source_graph.build_index(canonical, db_path=graph_db, incremental=False)
    changed = source.replace("    return helper()", "    return helper() + 1")
    candidate_file = candidate / "src" / "mod.py"
    candidate_file.write_text(changed, encoding="utf-8")
    digest = _digest(candidate_file)

    scopes = quality_review_scope.build_scoped_audits(
        authority_repo=canonical,
        candidate_repo=candidate,
        task_id="TASK_1",
        packet_seed="request-1",
        created_at="2026-08-09T00:00:00Z",
        changed_path_hashes={"src/mod.py": digest},
        source_evidence={
            "src/mod.py": {
                "segments": [
                    {
                        "candidate_start_line": 4,
                        "candidate_end_line": 5,
                        "baseline_start_line": 4,
                        "baseline_end_line": 5,
                    }
                ]
            }
        },
        acceptance=["target remains callable"],
        forbidden_changes=["do not change caller contract"],
        required_outputs=["src/mod.py"],
        validation=[["python3", "-m", "pytest", "-q", "tests/test_mod.py"]],
        terminal_validation=[
            {
                "declared_command": "python3 -m pytest -q tests/test_mod.py",
                "returncode": 0,
            }
        ],
        lenses=["correctness"],
    )

    wrapped = scopes["correctness"]
    packet = wrapped["packet"]
    assert len(wrapped["fingerprint"]) == 64
    assert packet["review_lens"]["lens_kind"] == "correctness"
    assert packet["invariants"] == ["target remains callable"]
    assert packet["forbidden_changes"] == ["do not change caller contract"]
    assert {row["qualified_name"] for row in packet["target_symbols"]} >= {
        "src/mod.py.target"
    }
    assert any(
        "caller" in row["description"] for row in packet["impact_evidence"]
    )
    assert any(
        row["path"] == "tests/test_mod.py" for row in packet["test_evidence"]
    )
    assert any(
        row["evidence_level"] == "tested" for row in packet["test_evidence"]
    )
    assert packet["contract_evidence"][0]["path"] == "src/mod.py"
    assert packet["unknowns"] == []


def test_graph_scope_records_truthful_unknowns_without_canonical_graph(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "authority"
    candidate = tmp_path / "candidate"
    authority.mkdir()
    candidate.mkdir()
    path = candidate / "notes.md"
    path.write_text("changed\n", encoding="utf-8")

    scopes = quality_review_scope.build_scoped_audits(
        authority_repo=authority,
        candidate_repo=candidate,
        task_id="TASK_2",
        packet_seed="request-2",
        created_at="2026-08-09T00:00:00Z",
        changed_path_hashes={"notes.md": _digest(path)},
        source_evidence={"notes.md": {"segments": []}},
        lenses=["security"],
    )

    unknown_ids = {
        row["identity"] for row in scopes["security"]["packet"]["unknowns"]
    }
    assert "canonical-graph-unavailable" in unknown_ids
    assert "impact-unresolved" in unknown_ids
    assert "tests-unresolved" in unknown_ids
    assert scopes["security"]["packet"]["validation_expectations"][0][
        "identity"
    ] == "expect-candidate-integrity"


def test_executed_validation_evidence_names_the_last_line_it_printed(
    tmp_path: Path,
) -> None:
    """The one line a reviewer judging test adequacy actually needs.

    The finalizer retains a bounded ``stdout_tail``/``stderr_tail`` on every
    executed validation row; the last non-blank line of it is the pytest
    summary, the traceback tail or the linter count.  Reviewers used to re-run
    the command to obtain it (measured: 20 pytest re-runs and 85 sandbox probes
    per 1,024 reviewer runs).  ``stderr_tail`` is the fallback when the run
    printed nothing on stdout.
    """
    authority = tmp_path / "authority"
    candidate = tmp_path / "candidate"
    authority.mkdir()
    candidate.mkdir()
    path = candidate / "notes.md"
    path.write_text("changed\n", encoding="utf-8")

    scopes = quality_review_scope.build_scoped_audits(
        authority_repo=authority,
        candidate_repo=candidate,
        task_id="TASK_TAIL",
        packet_seed="request-tail",
        created_at="2026-09-08T00:00:00Z",
        changed_path_hashes={"notes.md": _digest(path)},
        source_evidence={"notes.md": {"segments": []}},
        terminal_validation=[
            {
                "declared_command": "python3 -m pytest -q",
                "returncode": 0,
                "stdout_tail": "collected 3 items\n3 passed in 3.01s\n\n",
            },
            {
                "declared_command": "ruff check .",
                "returncode": 0,
                "stdout_tail": "   \n",
                "stderr_tail": "warning: 1 rule deprecated\n",
            },
        ],
        lenses=["correctness"],
    )

    descriptions = [
        row["description"]
        for row in scopes["correctness"]["packet"]["test_evidence"]
    ]
    assert any(
        "last output line: 3 passed in 3.01s" in text for text in descriptions
    ), descriptions
    # Blank stdout falls through to the retained stderr tail rather than
    # reporting an empty last line.
    assert any(
        "last output line: warning: 1 rule deprecated" in text
        for text in descriptions
    ), descriptions


def test_executed_validation_evidence_stays_intact_without_any_output(
    tmp_path: Path,
) -> None:
    """A row with no retained tail keeps exactly the description it always had."""
    authority = tmp_path / "authority"
    candidate = tmp_path / "candidate"
    authority.mkdir()
    candidate.mkdir()
    path = candidate / "notes.md"
    path.write_text("changed\n", encoding="utf-8")

    scopes = quality_review_scope.build_scoped_audits(
        authority_repo=authority,
        candidate_repo=candidate,
        task_id="TASK_QUIET",
        packet_seed="request-quiet",
        created_at="2026-09-08T00:00:00Z",
        changed_path_hashes={"notes.md": _digest(path)},
        source_evidence={"notes.md": {"segments": []}},
        terminal_validation=[{"declared_command": "ruff check .", "returncode": 0}],
        lenses=["correctness"],
    )

    descriptions = [
        row["description"]
        for row in scopes["correctness"]["packet"]["test_evidence"]
    ]
    assert "Executed validation returned 0: ruff check ." in descriptions
    assert not any("last output line" in text for text in descriptions)


def test_graph_scope_uses_canonical_symbols_for_removed_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    (canonical / "removed.py").write_text(
        "def removed_symbol():\n    return 1\n",
        encoding="utf-8",
    )
    graph_db = tmp_path / "source-graph.sqlite"
    monkeypatch.setattr(source_graph, "resolve_db_path", lambda _root: graph_db)
    source_graph.build_index(canonical, db_path=graph_db, incremental=False)

    scopes = quality_review_scope.build_scoped_audits(
        authority_repo=canonical,
        candidate_repo=candidate,
        task_id="TASK_3",
        packet_seed="request-3",
        created_at="2026-08-10T00:00:00Z",
        changed_path_hashes={"removed.py": None},
        source_evidence={
            "removed.py": {
                "segments": [
                    {
                        "candidate_start_line": 0,
                        "candidate_end_line": 0,
                        "baseline_start_line": 1,
                        "baseline_end_line": 2,
                    }
                ]
            }
        },
        lenses=["correctness"],
    )

    packet = scopes["correctness"]["packet"]
    assert packet["changed_paths"][0]["change_kind"] == "removed"
    assert "removed.py.removed_symbol" in {
        row["qualified_name"] for row in packet["target_symbols"]
    }


_FIRST_HUNK = {
    "kind": "replace",
    "candidate_start_line": 8,
    "candidate_end_line": 14,
    "changed_start_line": 11,
    "changed_end_line": 11,
    "baseline_start_line": 11,
    "baseline_end_line": 11,
}
_LAST_HUNK = {
    "kind": "replace",
    "candidate_start_line": 998,
    "candidate_end_line": 1001,
    "changed_start_line": 1001,
    "changed_end_line": 1001,
    "baseline_start_line": 1001,
    "baseline_end_line": 1001,
}
_DELETED_DOOMED = {
    "kind": "delete",
    "candidate_start_line": 2,
    "candidate_end_line": 6,
    "changed_start_line": 5,
    "changed_end_line": 5,
    "baseline_start_line": 5,
    "baseline_end_line": 8,
}
_WITH_DOOMED = (
    "def keep():\n    return 1\n\n\n"
    "def doomed():\n    return 2\n\n\n"
    "def other():\n    return 3\n"
)
_WITHOUT_DOOMED = (
    "def keep():\n    return 1\n\n\n"
    "def other():\n    return 3\n"
)


def _index_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str],
) -> Path:
    canonical = tmp_path / "canonical"
    for relative, text in files.items():
        target = canonical / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    graph_db = tmp_path / "source-graph.sqlite"
    monkeypatch.setattr(source_graph, "resolve_db_path", lambda _root: graph_db)
    source_graph.build_index(canonical, db_path=graph_db, incremental=False)
    return canonical


def _candidate_file(tmp_path: Path, relative: str, text: str) -> tuple[Path, str]:
    candidate = tmp_path / "candidate"
    target = candidate / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return candidate, _digest(target)


def _bare_authority(tmp_path: Path) -> Path:
    authority = tmp_path / "authority"
    authority.mkdir()
    return authority


def _scope(
    authority: Path,
    candidate: Path,
    path: str,
    digest: str | None,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    scopes = quality_review_scope.build_scoped_audits(
        authority_repo=authority,
        candidate_repo=candidate,
        task_id="TASK_DELTA",
        packet_seed="request-delta",
        created_at="2026-09-20T00:00:00Z",
        changed_path_hashes={path: digest},
        source_evidence={path: evidence},
        lenses=["correctness"],
    )
    return scopes["correctness"]


def _targets(wrapped: dict[str, Any]) -> set[str]:
    return {row["qualified_name"] for row in wrapped["packet"]["target_symbols"]}


def _unknown_ids(wrapped: dict[str, Any]) -> set[str]:
    return {row["identity"] for row in wrapped["packet"]["unknowns"]}


def _impact_text(wrapped: dict[str, Any]) -> list[str]:
    return [row["description"] for row in wrapped["packet"]["impact_evidence"]]


def _packet_bytes(wrapped: dict[str, Any]) -> bytes:
    return json.dumps(
        wrapped["packet"], ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _adjacent_functions(b_value: int) -> str:
    return (
        "def a():\n    return 1\n\n\n"
        f"def b():\n    return {b_value}\n\n\n"
        "def c():\n    return 3\n"
    )


def _spread_module(first: str, last: str) -> str:
    lines = ["from src.util import first_callee, middle_callee, last_callee"]
    lines += [f"# padding {number}" for number in range(2, 10)]
    lines += ["def first():", f"    return {first}"]
    lines += [f"# padding {number}" for number in range(12, 500)]
    lines += ["def middle():", "    return middle_callee()"]
    lines += [f"# padding {number}" for number in range(502, 1000)]
    lines += ["def last():", f"    return {last}"]
    assert [lines[9], lines[499], lines[999]] == [
        "def first():",
        "def middle():",
        "def last():",
    ]
    return "\n".join(lines) + "\n"


def _distant_hunk_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, str]:
    canonical = _index_canonical(
        tmp_path,
        monkeypatch,
        {
            "src/util.py": (
                "def first_callee():\n    return 1\n\n\n"
                "def middle_callee():\n    return 2\n\n\n"
                "def last_callee():\n    return 3\n"
            ),
            "src/mod.py": _spread_module("first_callee()", "last_callee()"),
            "src/callers.py": (
                "from src.mod import first, last, middle\n\n\n"
                "def call_first():\n    return first()\n\n\n"
                "def call_middle():\n    return middle()\n\n\n"
                "def call_last():\n    return last()\n"
            ),
            "tests/test_mod.py": (
                "from src.mod import first, last\n\n\n"
                "def test_first():\n    assert first() == 1\n\n\n"
                "def test_last():\n    assert last() == 3\n"
            ),
        },
    )
    candidate, digest = _candidate_file(
        tmp_path, "src/mod.py", _spread_module("first_callee() + 1", "last_callee() + 1")
    )
    return canonical, candidate, digest


def test_distant_hunks_select_only_the_symbols_they_touch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical, candidate, digest = _distant_hunk_repo(tmp_path, monkeypatch)

    wrapped = _scope(
        canonical,
        candidate,
        "src/mod.py",
        digest,
        {"segments": [_FIRST_HUNK, _LAST_HUNK]},
    )

    packet = wrapped["packet"]
    assert _targets(wrapped) == {"src/mod.py.first", "src/mod.py.last"}
    impact = _impact_text(wrapped)
    for name in ("first_callee", "last_callee", "call_first", "call_last"):
        assert any(name in text for text in impact), (name, impact)
    assert not any("middle" in text for text in impact), impact
    assert any(row["path"] == "tests/test_mod.py" for row in packet["test_evidence"])
    assert packet["unknowns"] == []
    changed = packet["changed_paths"][0]
    assert (changed["line_start"], changed["line_end"]) == (8, 1001)


def test_measured_fixture_reports_scope_before_and_after_exact_segments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property,
) -> None:
    """Measured rows and packet bytes only; no token saving is claimed."""
    canonical, candidate, digest = _distant_hunk_repo(tmp_path, monkeypatch)
    hunks = [_FIRST_HUNK, _LAST_HUNK]
    # The removed selection window: one span from the first start to the last end.
    window = {
        "candidate_start_line": min(hunk["candidate_start_line"] for hunk in hunks),
        "candidate_end_line": max(hunk["candidate_end_line"] for hunk in hunks),
    }

    measured = {}
    for label, segments in (("before", [window]), ("after", hunks)):
        wrapped = _scope(
            canonical, candidate, "src/mod.py", digest, {"segments": segments}
        )
        measured[label] = {
            "target_symbols": len(wrapped["packet"]["target_symbols"]),
            "impact_rows": len(wrapped["packet"]["impact_evidence"]),
            "packet_bytes": len(_packet_bytes(wrapped)),
        }
        for metric, value in measured[label].items():
            record_property(f"{label}_{metric}", value)

    before, after = measured["before"], measured["after"]
    assert (before["target_symbols"], after["target_symbols"]) == (3, 2), measured
    assert after["impact_rows"] < before["impact_rows"], measured
    assert after["packet_bytes"] < before["packet_bytes"], measured


def test_segment_order_duplicates_and_adjacency_leave_the_packet_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical, candidate, digest = _distant_hunk_repo(tmp_path, monkeypatch)
    head = dict(_FIRST_HUNK, changed_start_line=10, changed_end_line=10)
    tail = dict(_FIRST_HUNK, changed_start_line=11, changed_end_line=11)
    whole = dict(_FIRST_HUNK, changed_start_line=10, changed_end_line=11)
    variants = [
        [whole, _LAST_HUNK],
        [_LAST_HUNK, whole],
        [head, tail, _LAST_HUNK],
        [tail, _LAST_HUNK, head, whole, whole],
    ]

    scopes = [
        _scope(canonical, candidate, "src/mod.py", digest, {"segments": variant})
        for variant in variants
    ]

    assert all(scope == scopes[0] for scope in scopes)
    assert scopes[0]["fingerprint"] == hashlib.sha256(_packet_bytes(scopes[0])).hexdigest()


def test_context_lines_around_a_hunk_do_not_select_the_neighbouring_symbol(
    tmp_path: Path,
) -> None:
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", _adjacent_functions(20))
    hunk = {
        "kind": "replace",
        "candidate_start_line": 3,
        "candidate_end_line": 9,
        "changed_start_line": 6,
        "changed_end_line": 6,
        "baseline_start_line": 6,
        "baseline_end_line": 6,
    }

    wrapped = _scope(
        _bare_authority(tmp_path), candidate, "src/mod.py", digest, {"segments": [hunk]}
    )

    assert _targets(wrapped) == {"src/mod.py.b"}


def test_changed_lines_outside_every_symbol_fall_back_to_the_module(
    tmp_path: Path,
) -> None:
    source = "def a():\n    return 1\n\n\n# notes\ndef b():\n    return 2\n"
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", source)
    hunks = [
        {"candidate_start_line": 2, "candidate_end_line": 2},
        {"candidate_start_line": 5, "candidate_end_line": 5},
    ]

    wrapped = _scope(
        _bare_authority(tmp_path), candidate, "src/mod.py", digest, {"segments": hunks}
    )

    assert _targets(wrapped) == {"src/mod.py.a", "src/mod.py"}


def test_late_hunk_edges_are_not_cut_by_the_row_bound(tmp_path: Path) -> None:
    calls = "".join(f"    call_{number}()\n" for number in range(70))
    source = f"def early():\n{calls}\n\ndef late():\n    return late_callee()\n"
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", source)
    hunk = {
        "candidate_start_line": 72,
        "candidate_end_line": 75,
        "changed_start_line": 75,
        "changed_end_line": 75,
    }

    wrapped = _scope(
        _bare_authority(tmp_path), candidate, "src/mod.py", digest, {"segments": [hunk]}
    )

    impact = _impact_text(wrapped)
    assert _targets(wrapped) == {"src/mod.py.late"}
    assert any("late_callee" in text for text in impact), impact
    assert not any("call_0" in text for text in impact), impact


def test_deletion_only_hunk_targets_the_canonical_symbol_it_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _index_canonical(
        tmp_path,
        monkeypatch,
        {
            "src/mod.py": _WITH_DOOMED,
            "src/user.py": (
                "from src.mod import doomed\n\n\ndef uses():\n    return doomed()\n"
            ),
        },
    )
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", _WITHOUT_DOOMED)

    wrapped = _scope(
        canonical, candidate, "src/mod.py", digest, {"segments": [_DELETED_DOOMED]}
    )

    assert _targets(wrapped) == {"src/mod.py.doomed"}
    assert any("src/user.py.uses" in text for text in _impact_text(wrapped))
    assert "deleted-symbols-unresolved" not in _unknown_ids(wrapped)


def test_deletion_only_hunk_without_canonical_graph_is_an_explicit_unknown(
    tmp_path: Path,
) -> None:
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", _WITHOUT_DOOMED)

    wrapped = _scope(
        _bare_authority(tmp_path),
        candidate,
        "src/mod.py",
        digest,
        {"segments": [_DELETED_DOOMED]},
    )

    assert {"deleted-symbols-unresolved", "canonical-graph-unavailable"} <= (
        _unknown_ids(wrapped)
    )
    assert _targets(wrapped) == {"src/mod.py"}


def test_deleted_lines_owned_by_no_canonical_symbol_are_an_explicit_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _index_canonical(
        tmp_path,
        monkeypatch,
        {"src/mod.py": "def keep():\n    return 1\n\n\ndef doomed():\n    return 2\n"},
    )
    candidate, digest = _candidate_file(
        tmp_path, "src/mod.py", "def keep():\n    return 1\ndef doomed():\n    return 2\n"
    )
    blank_lines = {
        "kind": "delete",
        "candidate_start_line": 1,
        "candidate_end_line": 4,
        "changed_start_line": 3,
        "changed_end_line": 3,
        "baseline_start_line": 3,
        "baseline_end_line": 4,
    }

    wrapped = _scope(
        canonical, candidate, "src/mod.py", digest, {"segments": [blank_lines]}
    )

    assert "deleted-symbols-unresolved" in _unknown_ids(wrapped)
    assert _targets(wrapped) == {"src/mod.py"}


def test_removed_file_without_hunk_rows_targets_every_canonical_symbol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _index_canonical(
        tmp_path,
        monkeypatch,
        {
            "removed.py": (
                "def gone_one():\n    return 1\n\n\ndef gone_two():\n    return 2\n"
            ),
            "keeper.py": (
                "from removed import gone_one, gone_two\n\n\n"
                "def keep():\n    return gone_one() + gone_two()\n"
            ),
        },
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()

    wrapped = _scope(canonical, candidate, "removed.py", None, {"segments": []})

    assert {"removed.py.gone_one", "removed.py.gone_two"} <= _targets(wrapped)
    assert any("keeper.py.keep" in text for text in _impact_text(wrapped))
    unresolved = {"changed-lines-unresolved", "deleted-symbols-unresolved"}
    assert not unresolved & _unknown_ids(wrapped)


def test_removed_file_without_canonical_graph_is_an_explicit_unknown(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()

    wrapped = _scope(
        _bare_authority(tmp_path), candidate, "removed.py", None, {"segments": []}
    )

    assert {"deleted-symbols-unresolved", "canonical-graph-unavailable"} <= (
        _unknown_ids(wrapped)
    )


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param({}, id="segments-key-missing"),
        pytest.param({"segments": None}, id="segments-none"),
        pytest.param({"segments": "6-6"}, id="segments-not-a-list"),
        pytest.param({"segments": []}, id="segments-empty"),
        pytest.param({"segments": [None, 7, "6-6"]}, id="rows-not-mappings"),
        pytest.param({"segments": [{}]}, id="row-without-bounds"),
        pytest.param(
            {"segments": [{"candidate_start_line": True, "candidate_end_line": True}]},
            id="boolean-bounds",
        ),
        pytest.param(
            {"segments": [{"candidate_start_line": "6", "candidate_end_line": "6"}]},
            id="string-bounds",
        ),
        pytest.param(
            {"segments": [{"candidate_start_line": 0, "candidate_end_line": 0}]},
            id="zero-bounds",
        ),
        pytest.param(
            {"segments": [{"candidate_start_line": -3, "candidate_end_line": 6}]},
            id="negative-bound",
        ),
        pytest.param(
            {"segments": [{"candidate_start_line": 9, "candidate_end_line": 6}]},
            id="inverted-range",
        ),
        pytest.param(
            {
                "segments": [
                    {
                        "kind": "delete",
                        "candidate_start_line": 3,
                        "candidate_end_line": 7,
                        "changed_start_line": 5,
                        "changed_end_line": 5,
                        "baseline_start_line": 0,
                        "baseline_end_line": 0,
                    }
                ]
            },
            id="deletion-without-baseline-lines",
        ),
        pytest.param(
            {
                "segments": [
                    {
                        "kind": "replace",
                        "baseline_start_line": 6,
                        "baseline_end_line": 6,
                    }
                ]
            },
            id="replacement-without-candidate-lines",
        ),
    ],
)
def test_missing_or_malformed_hunk_evidence_is_an_explicit_unknown(
    tmp_path: Path,
    evidence: dict[str, Any],
) -> None:
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", _adjacent_functions(20))

    wrapped = _scope(_bare_authority(tmp_path), candidate, "src/mod.py", digest, evidence)

    questions = {row["identity"]: row["question"] for row in wrapped["packet"]["unknowns"]}
    assert "src/mod.py" in questions["changed-lines-unresolved"]
    assert _targets(wrapped) == {"src/mod.py"}


def test_a_malformed_hunk_row_is_reported_and_never_widens_the_scope(
    tmp_path: Path,
) -> None:
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", _adjacent_functions(20))
    segments = [
        {"candidate_start_line": 6, "candidate_end_line": 6},
        {"candidate_start_line": "9", "candidate_end_line": 9},
    ]

    wrapped = _scope(
        _bare_authority(tmp_path), candidate, "src/mod.py", digest, {"segments": segments}
    )

    assert _targets(wrapped) == {"src/mod.py.b"}
    assert "changed-lines-unresolved" in _unknown_ids(wrapped)


def _diff_segments(
    baseline: str, candidate: str, context: int = 3
) -> list[dict[str, Any]]:
    """Hunk rows shaped like the coordinator's line-diff evidence for two texts."""
    old = baseline.splitlines(keepends=True)
    new = candidate.splitlines(keepends=True)
    opcodes = difflib.SequenceMatcher(None, old, new).get_opcodes()
    rows: list[dict[str, Any]] = []
    for index, (tag, old_start, old_end, new_start, new_end) in enumerate(opcodes):
        if tag == "equal":
            continue
        before = after = 0
        if index > 0 and opcodes[index - 1][0] == "equal":
            before = min(context, opcodes[index - 1][4] - opcodes[index - 1][3])
        if index + 1 < len(opcodes) and opcodes[index + 1][0] == "equal":
            after = min(context, opcodes[index + 1][4] - opcodes[index + 1][3])
        start = new_start - before + 1
        rows.append(
            {
                "kind": tag,
                "candidate_start_line": start,
                "candidate_end_line": max(start, new_end + after),
                "changed_start_line": new_start + 1,
                "changed_end_line": max(new_start + 1, new_end),
                "baseline_start_line": old_start + 1,
                "baseline_end_line": max(old_start + 1, old_end),
            }
        )
    return rows


_ADJACENT_BASELINE = (
    "def keep():\n    return 1\n"
    "def doomed():\n    return 2\n"
    "def other():\n    return 3\n"
)
_ADJACENT_CANDIDATE = (
    "def keep():\n    return 10\n"
    "def other():\n    return 3\n"
)
_RENAME_BASELINE = "def keep():\n    return 1\n\n\ndef doomed():\n    return 2\n"
_RENAME_CANDIDATE = _RENAME_BASELINE.replace("doomed", "renamed")
_DOOMED_USER = "from src.mod import doomed\n\n\ndef uses():\n    return doomed()\n"


def test_replace_hunk_that_swallows_a_function_targets_it_and_its_callers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _index_canonical(
        tmp_path,
        monkeypatch,
        {"src/mod.py": _ADJACENT_BASELINE, "src/user.py": _DOOMED_USER},
    )
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", _ADJACENT_CANDIDATE)
    segments = _diff_segments(_ADJACENT_BASELINE, _ADJACENT_CANDIDATE)
    # keep's edited line and the whole of doomed form one replace hunk.
    assert [row["kind"] for row in segments] == ["replace"]

    wrapped = _scope(canonical, candidate, "src/mod.py", digest, {"segments": segments})

    assert _targets(wrapped) == {"src/mod.py.keep", "src/mod.py.doomed"}
    assert any("src/user.py.uses" in text for text in _impact_text(wrapped))
    assert "deleted-symbols-unresolved" not in _unknown_ids(wrapped)


def test_replace_hunk_that_renames_a_function_surfaces_the_old_name_callers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _index_canonical(
        tmp_path,
        monkeypatch,
        {"src/mod.py": _RENAME_BASELINE, "src/user.py": _DOOMED_USER},
    )
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", _RENAME_CANDIDATE)
    segments = _diff_segments(_RENAME_BASELINE, _RENAME_CANDIDATE)
    assert [row["kind"] for row in segments] == ["replace"]

    wrapped = _scope(canonical, candidate, "src/mod.py", digest, {"segments": segments})

    assert _targets(wrapped) == {"src/mod.py.renamed", "src/mod.py.doomed"}
    assert any("src/user.py.uses" in text for text in _impact_text(wrapped))


def test_replace_hunk_on_module_level_lines_is_not_a_missing_symbol_alarm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A comment line is owned by no symbol, so nothing removed can be lost here.
    baseline = "import os\n\n# tuned per host\n\n\ndef keep():\n    return 1\n"
    candidate_text = baseline.replace("per host", "per fleet")
    canonical = _index_canonical(tmp_path, monkeypatch, {"src/mod.py": baseline})
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", candidate_text)
    segments = _diff_segments(baseline, candidate_text)
    assert [row["kind"] for row in segments] == ["replace"]

    wrapped = _scope(canonical, candidate, "src/mod.py", digest, {"segments": segments})

    assert _targets(wrapped) == {"src/mod.py"}
    assert not {"deleted-symbols-unresolved", "changed-lines-unresolved"} & (
        _unknown_ids(wrapped)
    )


def test_replace_hunk_without_canonical_graph_reports_removed_symbols_as_unknown(
    tmp_path: Path,
) -> None:
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", _ADJACENT_CANDIDATE)
    segments = _diff_segments(_ADJACENT_BASELINE, _ADJACENT_CANDIDATE)

    wrapped = _scope(
        _bare_authority(tmp_path), candidate, "src/mod.py", digest, {"segments": segments}
    )

    assert {"deleted-symbols-unresolved", "canonical-graph-unavailable"} <= (
        _unknown_ids(wrapped)
    )
    assert _targets(wrapped) == {"src/mod.py.keep"}


def test_insert_hunk_does_not_select_the_symbol_under_its_placeholder_baseline_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = "def a():\n    return 1\ndef b():\n    return 2\n"
    candidate_text = "def a():\n    return 1\ndef n():\n    return 9\ndef b():\n    return 2\n"
    canonical = _index_canonical(tmp_path, monkeypatch, {"src/mod.py": baseline})
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", candidate_text)
    segments = _diff_segments(baseline, candidate_text)
    # An insert removes nothing; its baseline range only marks where it landed.
    assert [row["kind"] for row in segments] == ["insert"]
    assert (segments[0]["baseline_start_line"], segments[0]["baseline_end_line"]) == (3, 3)

    wrapped = _scope(canonical, candidate, "src/mod.py", digest, {"segments": segments})

    assert _targets(wrapped) == {"src/mod.py.n"}


def test_replace_hunk_landing_outside_the_surviving_function_still_targets_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = "def s():\n    x = 1\n    return x\n\n\ndef t():\n    return 2\n"
    candidate_text = "def s():\n    x = 1\ny = 2\n\n\ndef t():\n    return 2\n"
    canonical = _index_canonical(tmp_path, monkeypatch, {"src/mod.py": baseline})
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", candidate_text)
    segments = _diff_segments(baseline, candidate_text)
    assert [row["kind"] for row in segments] == ["replace"]

    wrapped = _scope(canonical, candidate, "src/mod.py", digest, {"segments": segments})

    # `s` lost its return line to the module-level `y`; `t` is untouched.
    assert _targets(wrapped) == {"src/mod.py.y", "src/mod.py.s"}


def test_replaced_baseline_owners_are_order_and_duplicate_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = (
        "def keep():\n    return 1\n"
        "def doomed():\n    return 2\n\n\n"
        "def old_name():\n    return 3\n"
    )
    candidate_text = "def keep():\n    return 10\n\n\ndef new_name():\n    return 3\n"
    user = (
        "from src.mod import doomed, old_name\n\n\n"
        "def uses():\n    return doomed() + old_name()\n"
    )
    canonical = _index_canonical(
        tmp_path, monkeypatch, {"src/mod.py": baseline, "src/user.py": user}
    )
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", candidate_text)
    first, second = _diff_segments(baseline, candidate_text)
    variants = [[first, second], [second, first], [first, second, first, second]]

    scopes = [
        _scope(canonical, candidate, "src/mod.py", digest, {"segments": variant})
        for variant in variants
    ]

    assert all(scope == scopes[0] for scope in scopes)
    assert scopes[0]["fingerprint"] == hashlib.sha256(_packet_bytes(scopes[0])).hexdigest()
    assert _targets(scopes[0]) == {
        "src/mod.py.keep",
        "src/mod.py.doomed",
        "src/mod.py.old_name",
        "src/mod.py.new_name",
    }
    assert any("src/user.py.uses" in text for text in _impact_text(scopes[0]))


def test_a_replace_hunk_without_baseline_lines_is_reported_but_keeps_its_changed_scope(
    tmp_path: Path,
) -> None:
    candidate, digest = _candidate_file(tmp_path, "src/mod.py", _adjacent_functions(20))
    hunk = {
        "kind": "replace",
        "candidate_start_line": 6,
        "candidate_end_line": 6,
        "changed_start_line": 6,
        "changed_end_line": 6,
    }

    wrapped = _scope(
        _bare_authority(tmp_path), candidate, "src/mod.py", digest, {"segments": [hunk]}
    )

    assert _targets(wrapped) == {"src/mod.py.b"}
    assert "changed-lines-unresolved" in _unknown_ids(wrapped)
