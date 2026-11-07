"""NF-2026-01921: the bootstrap tool replies with one ToolSearch select line for the manager's standard tools."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_bootstrap(tmp_path, monkeypatch):
    """Serve manager_bootstrap without a repository, SQLite store or off-request-path sweep."""

    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        core,
        "_live_manager_identity",
        lambda: {"provider": "claude", "session_id": "nf01921-session", "window_id": "claude_vscode_1"},
    )
    monkeypatch.setattr(core, "_schedule_task_hygiene", lambda *, run_when_due: {"status": "stubbed"})
    monkeypatch.setattr(core, "_repository_binding_source", lambda: "stubbed")
    monkeypatch.setattr(core, "_bootstrap_task_health", lambda _root, _readiness: {"status": "stubbed"})
    monkeypatch.setattr(core, "_bootstrap_dispatcher", lambda _route, _provider: {"status": "stubbed"})
    monkeypatch.setattr(core, "_adopt_callbacks_on_bootstrap", lambda _route: {"status": "stubbed"})
    monkeypatch.setattr(core, "_active_manager_chat_record", lambda: None)
    monkeypatch.setattr(core, "_CONTRACT_DELIVERIES", {})


def _registered_manager_tools() -> set[str]:
    server = importlib.import_module("aiworkhub.server")  # the live MCP registry, read at call time
    return {tool.name for tool in asyncio.run(server.mcp.list_tools())}


def test_bootstrap_call_carries_a_select_line_of_registered_tools():
    reply = core.manager_bootstrap(bootstrap_call=True)

    select = reply["toolsearch_select"]
    assert select.startswith("select:")
    names = select.removeprefix("select:").split(",")
    assert names == list(core.MANAGER_TOOLSEARCH_SELECT_NAMES)
    assert len(names) == len(set(names))
    unregistered = set(names) - _registered_manager_tools()
    assert not unregistered, f"select names are not registered manager tools: {sorted(unregistered)}"
    assert "ToolSearch" in reply["toolsearch_hint"] and "compaction" in reply["toolsearch_hint"]


def test_suppressed_contract_reply_still_carries_the_select_line():
    first = core.manager_bootstrap(bootstrap_call=True)
    second = core.manager_bootstrap(bootstrap_call=True)

    assert first["contract_delivered"] is True
    assert second["contract_delivered"] is False
    assert second["toolsearch_select"] == first["toolsearch_select"]


def test_route_gate_call_carries_no_select_line():
    reply = core.manager_bootstrap()

    assert "toolsearch_select" not in reply
    assert "toolsearch_hint" not in reply
    assert reply["contract_delivery_reason"] == "route_gate_call"


def test_the_select_tuple_lives_in_an_import_free_owner_module():
    # The tuple is owned by the import-free hook module (copied standalone to .claude/hooks/);
    # the leaf and core only re-export that one object.
    leaf = importlib.import_module("aiworkhub.manager_toolsearch_names")
    owner = importlib.import_module("aiworkhub.tool_guard_hook")

    assert core.MANAGER_TOOLSEARCH_SELECT_NAMES is leaf.MANAGER_TOOLSEARCH_SELECT_NAMES
    assert leaf.MANAGER_TOOLSEARCH_SELECT_NAMES is owner.MANAGER_TOOLSEARCH_SELECT_NAMES
    leaf_source = Path(leaf.__file__).read_text(encoding="utf-8")
    assert "MANAGER_TOOLSEARCH_SELECT_NAMES:" not in leaf_source
    owner_source = Path(owner.__file__).read_text(encoding="utf-8")
    imports = [line for line in owner_source.splitlines() if line.lstrip().startswith(("import ", "from "))]
    assert not any("aiworkhub" in line for line in imports)
