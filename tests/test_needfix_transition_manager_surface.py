"""The manager MCP surface lists no aiworkhub_dashboard_* tool; needfix_transition replaces the last one.

The dashboard runs in its own process, so the LLM-facing manager profile no longer keeps
aiworkhub_dashboard_snapshot or aiworkhub_dashboard_needfix_transition. NeedFix triage/accept
moves to the manager tool ``needfix_transition``, which runs the same transition path as the
dashboard tool (dashboard_mcp_app.needfix_transition_view): same arguments, confirm gate,
refusals and receipt; only ``server_tool`` names the manager tool. The dashboard profile and
the webview's tool names are unchanged.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, dashboard_mcp_app, mcp_tool_metadata, needfix_store, server  # noqa: E402

_MANAGER_TOOL = "needfix_transition"
_DASHBOARD_TRANSITION = "aiworkhub_dashboard_needfix_transition"
_DASHBOARD_SNAPSHOT = "aiworkhub_dashboard_snapshot"
_PREFIX = mcp_tool_metadata.MCP_DASHBOARD_TOOL_PREFIX
# Per-row values that differ between two otherwise identical items.
_VOLATILE_KEYS = frozenset({"id", "needfix_id", "updated_at", "created_at", "event_id", "title", "description"})


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    needfix_store.initialize_repository(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda *args, **kwargs: tmp_path)
    return tmp_path


def _twin_items(repo: Path, status: str = "captured", tag: str = "a") -> tuple[str, str]:
    # Distinct text: the store folds an identical capture into the existing row.
    first = needfix_store.add_needfix(repo, title=f"twin {tag} one", description=f"{tag} first body", status=status)
    second = needfix_store.add_needfix(repo, title=f"twin {tag} two", description=f"{tag} second body", status=status)
    assert first["id"] != second["id"]
    return first["id"], second["id"]


def _normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("<volatile>" if key in _VOLATILE_KEYS else _normalized(item))
            for key, item in value.items()
            if key != "server_tool"
        }
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    return value


def _both(manager_id: str, dashboard_id: str, **arguments: Any) -> tuple[dict, dict]:
    manager = server.needfix_transition(needfix_id=manager_id, **arguments)
    dashboard = dashboard_mcp_app.needfix_transition_view(needfix_id=dashboard_id, **arguments)
    return manager, dashboard


# --- profile table and listing ---------------------------------------------------


def test_manager_profile_lists_no_dashboard_tool_and_lists_needfix_transition(monkeypatch) -> None:
    manager_rows = {
        name
        for name, entry in mcp_tool_metadata.MCP_TOOL_PROFILES.items()
        if mcp_tool_metadata.MCP_PROFILE_MANAGER in entry
    }
    assert manager_rows, "an empty manager profile makes the prefix check vacuous"
    assert sorted(name for name in manager_rows if name.startswith(_PREFIX)) == []
    assert server.MANAGER_SURFACE_DASHBOARD_KEEP == frozenset()
    assert server.MANAGER_SURFACE_DASHBOARD_KEEP_REASONS == {}
    monkeypatch.setenv(server.MCP_SURFACE_ENV, "manager")
    assert server.mcp_tool_listed(_MANAGER_TOOL) is True
    for name in (_DASHBOARD_TRANSITION, _DASHBOARD_SNAPSHOT):
        assert server.mcp_tool_callable(name) is False, name
        assert server.mcp_tool_listed(name) is False, name


def test_needfix_transition_has_the_needfix_update_profile() -> None:
    profiles = mcp_tool_metadata.MCP_TOOL_PROFILES
    assert dict(profiles[_MANAGER_TOOL]) == dict(profiles["needfix_update"]) == {"manager": "listed"}
    # Registration order: the new row sits right after needfix_update.
    names = list(profiles)
    assert names.index(_MANAGER_TOOL) == names.index("needfix_update") + 1


def test_dashboard_profile_still_lists_both_dashboard_tools_unchanged() -> None:
    for name in (_DASHBOARD_TRANSITION, _DASHBOARD_SNAPSHOT):
        assert dict(mcp_tool_metadata.MCP_TOOL_PROFILES[name]) == {"dashboard": "listed"}, name
        assert server.mcp_tool_listed(name, "dashboard") is True, name
        assert server.mcp_tool_listed(name, "all") is True, name
    assert server.mcp_tool_callable(_MANAGER_TOOL, "dashboard") is False
    # The webview keeps calling the same names, bound to the same view functions.
    assert dashboard_mcp_app.NEEDFIX_WRITE_TOOLS[_DASHBOARD_TRANSITION] is dashboard_mcp_app.needfix_transition_view
    assert _DASHBOARD_SNAPSHOT in dashboard_mcp_app.READONLY_TOOLS


def test_manager_tool_takes_the_dashboard_arguments_and_names_no_dashboard_tool() -> None:
    manager = inspect.signature(server.needfix_transition).parameters
    dashboard = inspect.signature(dashboard_mcp_app.needfix_transition_view).parameters
    assert list(manager) == list(dashboard)
    assert {name: param.default for name, param in manager.items()} == {
        name: param.default for name, param in dashboard.items()
    }
    cleaned, documented = mcp_tool_metadata.split_docstring(server.needfix_transition.__doc__)
    assert set(documented) == set(manager)
    assert _PREFIX not in cleaned
    assert "needfix_update" in cleaned and "needfix_archive" in cleaned


# --- same transition path, result and refusals -------------------------------------


def test_triage_then_accept_matches_the_dashboard_tool(repo: Path) -> None:
    manager_id, dashboard_id = _twin_items(repo)
    for action, before, after in (("triage", "captured", "triaged"), ("accept", "triaged", "accepted")):
        manager, dashboard = _both(manager_id, dashboard_id, action=action, confirm=True)
        assert manager["ok"] is True, manager
        assert manager["server_tool"] == _MANAGER_TOOL
        assert dashboard["server_tool"] == _DASHBOARD_TRANSITION
        assert manager["authority"] == dashboard["authority"] == "storage_write"
        assert (manager["status_before"], manager["status_after"]) == (before, after)
        assert manager["id"] == manager_id
        assert list(manager) == list(dashboard)
        assert _normalized(manager) == _normalized(dashboard)
    assert needfix_store.get_needfix(repo, manager_id)["status"] == "accepted"
    assert needfix_store.get_needfix(repo, dashboard_id)["status"] == "accepted"


def test_promote_to_accepted_matches_the_dashboard_tool(repo: Path) -> None:
    manager_id, dashboard_id = _twin_items(repo)
    manager, dashboard = _both(
        manager_id, dashboard_id, action="triage", promote_to="accepted", readiness_score=70, confirm=True
    )
    assert manager["ok"] is True and manager["status_after"] == "accepted", manager
    assert manager["promote_to"] == "accepted" and manager["readiness_score"] == 70
    assert [event["status_after"] for event in manager["events"]] == ["triaged", "accepted"]
    assert _normalized(manager) == _normalized(dashboard)


@pytest.mark.parametrize(
    ("action", "after"), [("reject", "rejected"), ("defer", "deferred")],
)
def test_other_transitions_match_the_dashboard_tool(repo: Path, action: str, after: str) -> None:
    manager_id, dashboard_id = _twin_items(repo, status="triaged")
    manager, dashboard = _both(manager_id, dashboard_id, action=action, reason="not now", confirm=True)
    assert manager["ok"] is True and manager["status_after"] == after, manager
    assert _normalized(manager) == _normalized(dashboard)


def test_bulk_ids_match_the_dashboard_tool(repo: Path) -> None:
    manager_ids = list(_twin_items(repo))
    dashboard_ids = list(_twin_items(repo, tag="b"))
    assert not set(manager_ids) & set(dashboard_ids)
    manager = server.needfix_transition(needfix_ids=manager_ids, action="triage", confirm=True)
    dashboard = dashboard_mcp_app.needfix_transition_view(needfix_ids=dashboard_ids, action="triage", confirm=True)
    assert manager["succeeded"] == 2 and manager["failed"] == 0, manager
    assert manager["server_tool"] == _MANAGER_TOOL
    assert [entry["needfix_id"] for entry in manager["results"]] == manager_ids
    assert _normalized(manager) == _normalized(dashboard)


@pytest.mark.parametrize("allow_writes", ["1", None])
@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"action": "triage"}, "needfix_transition_confirmation_required"),
        ({"action": "triage", "confirm": "yes"}, "needfix_transition_confirmation_required"),
        ({"action": "teleport", "confirm": True}, "invalid_needfix_transition"),
        ({"action": "accept", "promote_to": "accepted", "confirm": True}, "promote_to_requires_triage_action"),
        ({"action": "triage", "promote_to": "resolved", "confirm": True}, "invalid_promote_to"),
        ({"action": "accept", "evidence": {"k": 1}, "confirm": True}, "evidence_requires_resolve_verified"),
    ],
)
def test_refusals_match_the_dashboard_tool_and_write_nothing(
    repo: Path, monkeypatch, allow_writes: str | None, arguments: dict, error: str
) -> None:
    if allow_writes is None:
        monkeypatch.delenv("AIWORKHUB_ALLOW_WRITES", raising=False)
    else:
        monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", allow_writes)
    manager_id, dashboard_id = _twin_items(repo)
    manager, dashboard = _both(manager_id, dashboard_id, **arguments)
    assert manager["ok"] is False and manager["error"] == error, manager
    assert manager["server_tool"] == _MANAGER_TOOL
    assert _normalized(manager) == _normalized(dashboard)
    assert needfix_store.get_needfix(repo, manager_id)["status"] == "captured"


def test_store_refusal_matches_the_dashboard_tool(repo: Path) -> None:
    # captured -> accepted skips triage: the store refuses, both tools report the same bounded error.
    manager_id, dashboard_id = _twin_items(repo)
    manager, dashboard = _both(manager_id, dashboard_id, action="accept", confirm=True)
    assert manager["ok"] is False and manager["error"], manager
    assert manager["error"].replace(manager_id, "<id>") == dashboard["error"].replace(dashboard_id, "<id>")
    assert set(manager) == set(dashboard)


def test_manager_tool_forwards_every_argument_to_the_dashboard_path(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_view(**kwargs: Any) -> dict:
        calls.append(kwargs)
        return {"ok": True, "server_tool": _DASHBOARD_TRANSITION, "authority": "storage_write"}

    monkeypatch.setattr(dashboard_mcp_app, "needfix_transition_view", fake_view)
    arguments = {
        "needfix_id": "NF-2026-00001",
        "action": "resolve_verified",
        "reason": "verified",
        "readiness_score": 100,
        "duplicate_parent_id": "",
        "confirm": True,
        "include_item": True,
        "promote_to": "",
        "needfix_ids": None,
        "evidence": {"k": 1},
        "evidence_refs": ["ref"],
    }
    result = server.needfix_transition(**arguments)
    assert calls == [arguments]
    assert result == {"ok": True, "server_tool": _MANAGER_TOOL, "authority": "storage_write"}


# --- conversion refusal names the manager tool -----------------------------------


@pytest.mark.parametrize(("status", "action"), [("captured", "triage"), ("triaged", "accept")])
def test_conversion_refusal_and_next_action_name_the_manager_tool(repo: Path, status: str, action: str) -> None:
    assert needfix_store.TRANSITION_TOOL == server.NEEDFIX_TRANSITION_TOOL == _MANAGER_TOOL
    rec = needfix_store.add_needfix(repo, title="t", description="d", status=status)
    preview = needfix_store.preview_convert(
        repo, rec["id"],
        {"runner": "claude", "topic": "needfix", "allowed_writes": ["src/x.py"], "required_outputs": ["src/x.py"]},
    )
    assert preview["next_action"] == {"tool": _MANAGER_TOOL, "action": action}
    reason = needfix_store._conversion_refusal_reason(rec["id"], status)
    assert f"call {_MANAGER_TOOL} action={action}" in reason
    assert _PREFIX not in reason and _PREFIX not in preview["refusal_reason"]
    assert server.mcp_tool_listed(preview["next_action"]["tool"], "manager") is True
