"""Source Graph ``calls`` mode must answer about the symbol it was asked about.

Before NF-2026-00861 ``calls`` built its edge rows from the scoped FILE list
alone: ``calls(query="review_feedback_identity", target="src/aiworkhub/
task_store.py")`` returned every call recorded anywhere in that file, in line
order, and any other word returned the byte-identical rows -- both under
``scope="query_matches"``. These tests pin the three truthful outcomes:
an exact symbol, an ambiguous name, and an unresolved query.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("calls_query_truth") / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="calls_query_truth")
    # One exactly-named subject, one unrelated neighbour in the SAME file (the
    # file-level answer used to mix the two), and a caller in another file.
    _write(root / "pkg" / "target.py", (
        "def review_feedback_identity(card):\n"
        "    return _reason(card)\n"
        "\n"
        "\n"
        "def _reason(card):\n"
        "    return str(card)\n"
        "\n"
        "\n"
        "def unrelated_neighbour():\n"
        "    return _reason(\"unrelated\")\n"
    ))
    _write(root / "pkg" / "caller.py", (
        "from pkg.target import review_feedback_identity\n"
        "\n"
        "\n"
        "def use_identity(card):\n"
        "    return review_feedback_identity(card)\n"
    ))
    # Two distinct symbols sharing one name, so an ambiguous lookup has a real
    # population to be ambiguous about.
    _write(root / "pkg" / "twin_a.py", (
        "def shared_name():\n"
        "    return \"a\"\n"
    ))
    _write(root / "pkg" / "twin_b.py", (
        "def shared_name():\n"
        "    return \"b\"\n"
    ))
    sg.build_index(root, incremental=False)
    return root


def _calls(repo: Path, query: str, *, target: str | None = "pkg/target.py") -> dict:
    return sg.analytics_query(repo, "calls", query, 64, target=target)


def _edges(payload: dict) -> list[dict]:
    return list(payload["outgoing_calls"]) + list(payload["incoming_calls"])


def test_exact_query_symbol_returns_only_that_symbols_edges(repo: Path) -> None:
    payload = _calls(repo, "review_feedback_identity")

    assert payload["scope"] == "query_symbol_matches"
    resolved = payload["query_symbol"]["resolved"]
    assert resolved.endswith("review_feedback_identity")
    assert payload["query_symbol"]["resolution"] == "exact_symbol_match"
    assert payload["query_symbol"]["file_path"] == "pkg/target.py"

    # Every outgoing edge is one this symbol makes -- not one its file makes.
    assert payload["outgoing_calls"]
    assert {row["caller_symbol"] for row in payload["outgoing_calls"]} == {resolved}
    assert {row["callee_symbol"] for row in payload["outgoing_calls"]} == {"_reason"}
    # The same file's unrelated neighbour also calls ``_reason``; the file-level
    # answer returned that edge too, and it must no longer appear here.
    assert not any(
        "unrelated_neighbour" in str(row["caller_symbol"])
        for row in _edges(payload)
    )


def test_exact_query_symbol_keeps_canonical_callers_in_other_files(repo: Path) -> None:
    payload = _calls(repo, "review_feedback_identity")

    # A caller lives outside the target file. Confining incoming edges to the
    # target scope would drop exactly the evidence the question asks for.
    assert any(
        row["caller_file"] == "pkg/caller.py"
        and "use_identity" in str(row["caller_symbol"])
        for row in payload["incoming_calls"]
    )
    assert all(
        str(row["callee_symbol"]) == "review_feedback_identity"
        for row in payload["incoming_calls"]
    )


def test_unrelated_query_cannot_reuse_the_edges_under_query_matches(repo: Path) -> None:
    resolved = _calls(repo, "review_feedback_identity")
    unrelated = _calls(repo, "zzqqxx_not_a_symbol_here")

    assert unrelated["scope"] != "query_matches"
    assert unrelated["scope"] == "file_level_call_edges"
    assert unrelated["query_symbol"]["resolved"] is None
    assert unrelated["query_symbol"]["resolution"] == "no_exact_symbol_in_scope"
    assert json.dumps(_edges(resolved), sort_keys=True) != json.dumps(
        _edges(unrelated), sort_keys=True
    )


def test_unresolved_query_still_reports_labelled_file_level_evidence(repo: Path) -> None:
    payload = _calls(repo, "zzqqxx_not_a_symbol_here")

    # Truthfully labelled file-level evidence, not a claimed query match and
    # not a fabricated emptiness: the file really does record these calls.
    assert payload["scope"] == "file_level_call_edges"
    assert payload["files"] == ["pkg/target.py"]
    assert _edges(payload)
    assert payload["coverage"]["returned"] == len(_edges(payload))


def test_multi_word_query_is_unresolvable_rather_than_guessed(repo: Path) -> None:
    payload = _calls(repo, "review feedback identity")

    assert payload["scope"] == "file_level_call_edges"
    assert payload["query_symbol"]["resolution"] == "unresolvable_query"


def test_ambiguous_query_symbol_is_labelled_and_claims_no_edges(repo: Path) -> None:
    payload = _calls(repo, "shared_name", target="pkg")

    assert payload["scope"] == "query_symbol_ambiguous"
    assert payload["query_symbol"]["resolved"] is None
    assert payload["outgoing_calls"] == []
    assert payload["incoming_calls"] == []
    candidates = payload["query_symbol"]["candidates"]
    assert {candidate.rsplit("/", 1)[-1] for candidate in candidates} == {
        "twin_a.py.shared_name", "twin_b.py.shared_name",
    }


def test_out_of_scope_symbol_does_not_resolve_against_another_file(repo: Path) -> None:
    # ``use_identity`` is real, but not in this target. Resolving it anyway
    # would let an exact name silently widen the scope it was asked under.
    payload = _calls(repo, "use_identity", target="pkg/target.py")

    assert payload["scope"] == "file_level_call_edges"
    assert payload["query_symbol"]["resolution"] == "no_exact_symbol_in_scope"


def test_unscoped_exact_query_symbol_still_binds_to_that_symbol(repo: Path) -> None:
    payload = _calls(repo, "review_feedback_identity", target=None)

    assert payload["scope"] == "query_symbol_matches"
    assert payload["query_symbol"]["file_path"] == "pkg/target.py"
    assert {row["caller_symbol"] for row in payload["outgoing_calls"]} == {
        payload["query_symbol"]["resolved"]
    }
