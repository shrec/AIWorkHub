"""B107 packaging regression: collision_preflight must ship self-contained.

``cli_adapter_readonly_tool.collision_preflight`` used to obtain its card
classification helpers via
``from scripts.build_tasking_parallel_group_collision_guard_v1 import ...``.
That module is NOT packaged into the wheel or the VSIX runtime (only ``src/``
ships) and, in this repository, no longer exists anywhere -- so every
invocation on an installed/VSIX runtime raised
``ModuleNotFoundError: No module named 'scripts'``. The tool is registered into
the MCP server (``server.py``: ``cli_adapter_readonly_tool.register(mcp)``), so
this broke a live read-only tool for any repo lacking that file.

The fix reuses the production semantics that already ship in the package:
``task_store.canonical_status`` and the exact active-status bucket set the live
``core.collision_guard`` uses -- no ``scripts/`` import at all. These tests are
proper ``pytest`` tests (the historical coverage was a ``.sh`` script that CI
never runs), so the regression is caught in CI going forward.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import cli_adapter_readonly_tool as tool  # noqa: E402
from aiworkhub import task_store  # noqa: E402

_CARDS_ENV = "BITNN_TASK_CARDS_PATH"


def test_module_source_has_no_scripts_import() -> None:
    """Static guard: the unshippable ``scripts.`` import must never return.

    Checks real import STATEMENTS (line-leading), so an explanatory comment that
    names the old module for history is fine while a re-added lazy import fails.
    """
    src = (_SRC / "aiworkhub" / "cli_adapter_readonly_tool.py").read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in src.splitlines()
        if line.strip().startswith(("from scripts", "import scripts"))
    ]
    assert not offenders, f"unshippable scripts import present: {offenders}"


def test_helpers_resolve_without_scripts_and_use_production_semantics() -> None:
    active, canonical, load = tool._collision_guard_helpers()
    # canonical_status is the SAME production function the live collision guard
    # uses -- so the preflight can never drift from production.
    assert canonical is task_store.canonical_status
    assert set(active) == {"pending", "processing", "review", "blocked"}
    assert callable(load)


def test_preflight_no_cards_file_is_not_an_error(tmp_path, monkeypatch) -> None:
    """The historical failure was ModuleNotFoundError on invocation. A missing
    cards file must instead be a clean, read-only 'no active claims' answer."""
    monkeypatch.setenv(_CARDS_ENV, str(tmp_path / "does_not_exist.jsonl"))
    result = tool.collision_preflight(task_id="T1", runner="claude_opus48", topic="x")
    assert result["would_collide"] is False
    assert result["cards_source_exists"] is False
    assert result["active_card_count"] == 0
    assert result["read_only"] is True and result["mutated_state"] is False


def test_preflight_detects_task_and_runner_collisions(tmp_path, monkeypatch) -> None:
    cards = tmp_path / "cards.jsonl"
    cards.write_text(
        "\n".join(
            [
                json.dumps({"task_id": "T1", "runner": "claude_opus48", "status": "processing"}),
                json.dumps({"task_id": "T2", "runner": "claude_opus48", "status": "finished"}),
                json.dumps({"task_id": "T3", "runner": "claude_opus48", "status": "review"}),
                "   ",                       # blank -> skipped
                "{not valid json",          # malformed -> skipped
                json.dumps(["not", "dict"]),  # non-object -> skipped
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(_CARDS_ENV, str(cards))
    result = tool.collision_preflight(task_id="T1", runner="claude_opus48", topic="x")
    assert result["would_collide"] is True
    # T1(processing) + T3(review) are active; T2(finished) is excluded.
    assert result["active_card_count"] == 2
    assert result["matching_task_id_active_claims"] == 1
    assert result["same_runner_other_active_claims"] == 1
    assert "active_claim_exists_for_task_id" in result["collision_reasons"]
    assert "runner_already_claims_other_active_task" in result["collision_reasons"]


def test_preflight_no_collision_when_only_terminal_cards(tmp_path, monkeypatch) -> None:
    cards = tmp_path / "cards.jsonl"
    cards.write_text(
        "\n".join(
            [
                json.dumps({"task_id": "T1", "runner": "r", "status": "finished"}),
                json.dumps({"task_id": "T1", "runner": "r", "status": "archived", "archived_at": "2026-01-01"}),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(_CARDS_ENV, str(cards))
    result = tool.collision_preflight(task_id="T1", runner="r", topic="x")
    assert result["would_collide"] is False
    assert result["active_card_count"] == 0
