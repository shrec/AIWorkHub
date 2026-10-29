"""NF-2026-01839: evidence-mode task_show dedupes repeated subtrees losslessly."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import mcp_summary_folds as folds  # noqa: E402
from aiworkhub import server  # noqa: E402


def _big(tag: str = "a", n: int = 3000) -> dict:
    return {"sources": [tag * n], "kind": "authority"}


def _envelope(card: dict) -> dict:
    return {"ok": True, "stdout": json.dumps(card), "task_card": card}


def _card() -> dict:
    return {
        "task_id": "T",
        "validation": [
            {"python_candidate_authority": _big(), "n": 1},
            {"python_candidate_authority": _big(), "n": 2},
            {"python_candidate_authority": _big(), "n": 3},
        ],
    }


def test_repeated_subtree_is_replaced_and_restored(monkeypatch) -> None:
    card = _card()
    deduped = folds.dedupe_repeated_subtrees(card)
    assert len(json.dumps(deduped)) < len(json.dumps(card)) // 2
    assert deduped["validation"][0]["python_candidate_authority"] == _big()
    ref = deduped["validation"][1]["python_candidate_authority"]
    assert ref["same_as"] == "$.validation[0].python_candidate_authority"
    assert set(ref) == {"same_as", "sha256", "bytes"}
    assert folds.expand_same_as(deduped) == card

    monkeypatch.setattr(server.core, "show_task", lambda task_id, full=False: _envelope(card))
    evidence = server.aiworkhub_task_show("T", detail="evidence")
    full = server.aiworkhub_task_show("T", detail="full")
    assert len(evidence["stdout"]) < len(json.dumps(card))
    assert folds.expand_same_as(json.loads(evidence["stdout"])) == card
    assert full == _envelope(card)


def test_subtrees_below_threshold_are_never_replaced() -> None:
    small = {"x": "y" * 100}
    card = {"a": small, "b": small, "c": [small, small]}
    assert folds.dedupe_repeated_subtrees(card) == card
    assert folds.dedupe_repeated_subtrees(card, min_bytes=10) != card


def test_nested_repeat_inside_first_occurrence_stays_whole() -> None:
    inner = _big("i", 2500)
    outer = {"inner": inner, "inner_again": _big("i", 2500)}
    card = {"first": outer, "second": json.loads(json.dumps(outer))}
    deduped = folds.dedupe_repeated_subtrees(card)
    assert deduped["first"]["inner"] == inner
    assert deduped["first"]["inner_again"]["same_as"] == "$.first.inner"
    assert deduped["second"]["same_as"] == "$.first"
    assert folds.expand_same_as(deduped) == card


def test_input_is_not_mutated() -> None:
    card = _card()
    snapshot = json.dumps(card, sort_keys=True)
    folds.dedupe_repeated_subtrees(card)
    assert json.dumps(card, sort_keys=True) == snapshot


def test_round_trip_with_mixed_list_and_dict_positions() -> None:
    a, b = _big("a"), _big("b")
    card = {
        "list": [a, b, a, [a, b], {"k": b}],
        "dict": {"a": a, "odd key": b, "nested": {"again": a}},
        "top": [[a], [a]],
        "scalar": "z" * 5000,
        "dup_scalar": "z" * 5000,
    }
    deduped = folds.dedupe_repeated_subtrees(card)
    assert deduped["dup_scalar"] == card["dup_scalar"]
    assert deduped["dict"]["odd key"]["same_as"] == "$.list[1]"
    assert folds.expand_same_as(json.loads(json.dumps(deduped))) == card


def test_three_copies_in_one_list_round_trip() -> None:
    a = _big("a")
    card = {"x": [a, a, a]}
    deduped = folds.dedupe_repeated_subtrees(card)
    assert deduped["x"][1]["same_as"] == "$.x[0]"
    assert deduped["x"][2]["same_as"] == "$.x[0]"
    assert folds.expand_same_as(json.loads(json.dumps(deduped))) == card
