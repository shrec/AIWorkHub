"""B952: dependency artifact materialization.

A dependent task's isolated worktree must see its ``depends_on`` dependencies'
promoted (accepted-but-not-yet-committed) outputs.
``ProcessManager._with_dependency_inputs`` resolves each dependency's declared
write scope (``allowed_writes`` + any ``required_outputs``) and merges it into
the dependent card's ``immutable_inputs`` -- so ``create_workspace`` seeds those
paths from the canonical working tree (where ``promote`` writes them,
uncommitted) and the B919 input-drift manifest covers them. This is the fix for
the measured defect where a completed dependency's artifact never reached a
dependent's worktree (a finished B948 output unseen by B951).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher  # noqa: E402


def _envelopes(cards: dict[str, dict]):
    """An injectable ``show_task`` returning a ``task_engine``-shaped envelope."""
    def show_task(task_id: str) -> dict:
        card = cards.get(task_id)
        if card is None:
            return {"ok": False, "returncode": 1, "stdout": "", "stderr": "not_found"}
        return {"ok": True, "returncode": 0, "stdout": json.dumps(card), "stderr": ""}
    return show_task


def _manager(tmp_path: Path, cards: dict[str, dict]) -> process_launcher.ProcessManager:
    return process_launcher.ProcessManager(
        repo=tmp_path, process_dir=tmp_path / "proc",
        show_task=_envelopes(cards), isolation_enabled=False,
    )


def test_dependency_outputs_merged_into_immutable_inputs(tmp_path: Path) -> None:
    dep = {
        "task_id": "DEP_B948",
        "allowed_writes": ["out/report.json", "out/data/*.csv"],
        "required_outputs": ["out/report.json"],
    }
    mgr = _manager(tmp_path, {"DEP_B948": dep})
    child = {
        "task_id": "CHILD_B951", "depends_on": ["DEP_B948"],
        "immutable_inputs": ["docs/schema.json"], "allowed_writes": ["out/final.txt"],
    }
    enriched = mgr._with_dependency_inputs(child)
    # Existing immutable_inputs first, dep outputs appended (required_outputs is
    # a subset of allowed_writes so out/report.json is added exactly once).
    assert enriched["immutable_inputs"] == [
        "docs/schema.json", "out/report.json", "out/data/*.csv",
    ]
    assert enriched["dependency_materialized_inputs"] == ["out/report.json", "out/data/*.csv"]
    # The original card is never mutated in place.
    assert child["immutable_inputs"] == ["docs/schema.json"]


def test_no_depends_on_returns_card_unchanged(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, {})
    card = {"task_id": "T", "immutable_inputs": ["a"], "allowed_writes": ["b"]}
    assert mgr._with_dependency_inputs(card) is card


def test_dep_output_already_declared_is_not_duplicated(tmp_path: Path) -> None:
    dep = {"task_id": "DEP", "allowed_writes": ["out/x.json", "out/y.json"]}
    mgr = _manager(tmp_path, {"DEP": dep})
    # out/x.json is already an immutable input; out/y.json is the child's OWN
    # write scope -- neither should be re-added.
    child = {
        "task_id": "C", "depends_on": ["DEP"],
        "immutable_inputs": ["out/x.json"], "allowed_writes": ["out/y.json"],
    }
    enriched = mgr._with_dependency_inputs(child)
    assert enriched is child  # nothing added -> same object, no audit field
    assert "dependency_materialized_inputs" not in enriched


def test_missing_dependency_is_skipped_not_fatal(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, {})  # dependency not present in the store
    child = {"task_id": "C", "depends_on": ["DEP_MISSING"], "allowed_writes": ["out/z"]}
    assert mgr._with_dependency_inputs(child) is child


def test_multiple_deps_deduped_order_preserving(tmp_path: Path) -> None:
    cards = {
        "D1": {"task_id": "D1", "allowed_writes": ["out/a.json", "shared/common.bin"]},
        "D2": {"task_id": "D2", "allowed_writes": ["shared/common.bin", "out/b.json"]},
    }
    mgr = _manager(tmp_path, cards)
    child = {"task_id": "C", "depends_on": ["D1", "D2"], "allowed_writes": ["out/final"]}
    enriched = mgr._with_dependency_inputs(child)
    assert enriched["immutable_inputs"] == ["out/a.json", "shared/common.bin", "out/b.json"]


def test_load_dependency_card_envelope_parsing(tmp_path: Path) -> None:
    cards = {"OK": {"task_id": "OK", "allowed_writes": ["x"]}}
    mgr = _manager(tmp_path, cards)
    assert mgr._load_dependency_card("OK") == {"task_id": "OK", "allowed_writes": ["x"]}
    assert mgr._load_dependency_card("MISSING") == {}


def test_load_dependency_card_tolerates_bad_envelopes(tmp_path: Path) -> None:
    table = {
        "OK_BAD_JSON": {"ok": True, "returncode": 0, "stdout": "{not json"},
        "NONZERO": {"ok": False, "returncode": 2, "stdout": "{}"},
        "NON_DICT": {"ok": True, "returncode": 0, "stdout": "[1,2,3]"},
    }

    def bad_show(task_id: str):
        if task_id == "RAISES":
            raise RuntimeError("boom")
        return table.get(task_id, {"ok": False, "returncode": 1, "stdout": ""})

    mgr = process_launcher.ProcessManager(
        repo=tmp_path, process_dir=tmp_path / "proc", show_task=bad_show, isolation_enabled=False,
    )
    assert mgr._load_dependency_card("OK_BAD_JSON") == {}
    assert mgr._load_dependency_card("NONZERO") == {}
    assert mgr._load_dependency_card("NON_DICT") == {}
    assert mgr._load_dependency_card("RAISES") == {}
