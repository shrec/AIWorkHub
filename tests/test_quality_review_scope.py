from __future__ import annotations

import hashlib
from pathlib import Path

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
