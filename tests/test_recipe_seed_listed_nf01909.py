from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import server  # noqa: E402


def test_seed_canonical_is_listed_on_manager() -> None:
    assert server.mcp_tool_listed("aiworkhub_manager_recipe_seed_canonical", "manager") is True


def test_seed_canonical_is_not_callable_on_worker() -> None:
    assert server.mcp_tool_callable("aiworkhub_manager_recipe_seed_canonical", "worker") is False


def test_recipe_register_stays_hidden_on_manager() -> None:
    assert server.mcp_tool_listed("aiworkhub_manager_recipe_register", "manager") is False
