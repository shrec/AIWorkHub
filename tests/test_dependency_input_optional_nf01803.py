"""NF-01803: a dependency-derived immutable input that is absent is optional."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher, worker_workspace  # noqa: E402

_FILTERS = "UnitTests/UnitTests.vcxproj.filters"


def _tree(tmp_path: Path, *relatives: str) -> None:
    for relative in relatives:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")


def _card(**extra: object) -> dict:
    card = {
        "immutable_inputs": ["src/a.py", _FILTERS],
        "dependency_materialized_inputs": [_FILTERS],
    }
    card.update(extra)
    return card


def test_absent_derived_input_is_skipped(tmp_path: Path) -> None:
    _tree(tmp_path, "src/a.py")
    result = worker_workspace._exact_required_card_inputs(tmp_path, _card(), ())
    assert result == ("src/a.py",)


def test_present_derived_input_is_still_returned(tmp_path: Path) -> None:
    _tree(tmp_path, "src/a.py", _FILTERS)
    result = worker_workspace._exact_required_card_inputs(tmp_path, _card(), ())
    assert result == tuple(sorted(("src/a.py", _FILTERS)))


def test_card_declared_missing_input_still_fails_closed(tmp_path: Path) -> None:
    _tree(tmp_path, "src/a.py")
    card = _card(dependency_materialized_inputs=[])
    message = (
        "workspace_required_input_missing:field=immutable_inputs:index=1:path="
        + _FILTERS
    )
    with pytest.raises(worker_workspace.WorkspaceError, match=re.escape(message)):
        worker_workspace._exact_required_card_inputs(tmp_path, card, ())


def test_dependency_composition_does_not_block_launch(tmp_path: Path) -> None:
    dep = {
        "task_id": "DEP",
        "allowed_writes": ["out/made.txt", _FILTERS],
        "required_outputs": ["out/made.txt"],
    }
    mgr = process_launcher.ProcessManager(
        repo=tmp_path,
        process_dir=tmp_path / "proc",
        show_task=lambda task_id: {
            "ok": True,
            "returncode": 0,
            "stdout": json.dumps(dep),
            "stderr": "",
        },
        isolation_enabled=False,
    )
    _tree(tmp_path, "src/a.py", "out/made.txt")
    child = {
        "task_id": "CHILD",
        "depends_on": ["DEP"],
        "immutable_inputs": ["src/a.py"],
        "allowed_writes": ["out/final.txt"],
    }
    enriched = mgr._with_dependency_inputs(child)
    assert _FILTERS in enriched["dependency_materialized_inputs"]
    result = worker_workspace._exact_required_card_inputs(tmp_path, enriched, ())
    assert result == ("out/made.txt", "src/a.py")
