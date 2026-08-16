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
    assert set(active) == {"pending", "processing", "review"}
    assert callable(load)


def test_preflight_explicit_missing_source_is_unresolved_not_a_confident_zero(
    tmp_path, monkeypatch
) -> None:
    """An EXPLICITLY configured card source that does not exist is UNRESOLVED,
    never a confident 'zero active claims, safe to proceed'.

    This is the contract this card exists to establish. When an operator points
    ``BITNN_TASK_CARDS_PATH`` (or ``AIWORKHUB_COLLISION_CARDS_PATH``) at a file
    that is absent, the honest answer is 'I could not resolve my source', not
    '0 active cards'. A preflight that approves overlapping write scopes because
    it could not find its own source is the original defect -- it is how a
    collision guard reported 0 active cards while the canonical guard saw 14.
    """
    missing = tmp_path / "does_not_exist.jsonl"
    monkeypatch.setenv(_CARDS_ENV, str(missing))
    result = tool.collision_preflight(task_id="T1", runner="claude_opus48", topic="x")
    # Unresolved shape -- None, not 0/False, so it can never read as "safe".
    assert result["cards_source_resolved"] is False
    assert result["active_card_count"] is None
    assert result["would_collide"] is None
    # unresolved_reason names WHY (the env var), and leaks no absolute path.
    assert result["unresolved_reason"]
    assert str(missing) not in result["unresolved_reason"]
    assert "does_not_exist.jsonl" not in result["unresolved_reason"]
    # The WHOLE unresolved structure is path-free: cards_source names the env
    # var beside the reason, it does not print the (absolute) path either.
    assert str(missing) not in result["cards_source"]
    assert "does_not_exist.jsonl" not in result["cards_source"]
    # Still strictly read-only regardless of resolution outcome.
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
                json.dumps({"task_id": "T2", "runner": "r", "status": "blocked"}),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(_CARDS_ENV, str(cards))
    result = tool.collision_preflight(task_id="T1", runner="r", topic="x")
    assert result["would_collide"] is False
    assert result["active_card_count"] == 0
