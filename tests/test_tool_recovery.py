from __future__ import annotations

import json

from aiworkhub.tool_recovery import unknown_tool_message


def test_unknown_tool_guidance_is_bounded_deterministic_and_non_executing() -> None:
    tools = [
        "aiworkhub_manager_source_graph_query",
        "aiworkhub_manager_session_current_state",
        "aiworkhub_dashboard_health",
    ]
    first = unknown_tool_message("aiworkhub_dashboard_healt", tools)
    second = unknown_tool_message("aiworkhub_dashboard_healt", reversed(tools))
    assert first == second
    payload = json.loads(first)
    assert payload["error"] == "unknown_tool"
    assert payload["requested"] == "aiworkhub_dashboard_healt"
    assert payload["retryable"] is True
    assert payload["recovery"] == (
        "call tools/list and retry with one exact registered name"
    )
    assert payload["suggestions"][0] == "aiworkhub_dashboard_health"
    assert len(payload["suggestions"]) <= 3


def test_unknown_tool_guidance_redacts_malformed_or_oversized_names() -> None:
    payload = json.loads(unknown_tool_message("secret value\n" * 30, ["safe_tool"]))
    assert payload["requested"] == "<invalid>"
    assert payload["suggestions"] == []
    assert len(json.dumps(payload)) < 500


def test_off_profile_guidance_names_the_active_and_exposing_profiles() -> None:
    listed = ["aiworkhub_dashboard_snapshot", "aiworkhub_task_list"]
    message = unknown_tool_message(
        "aiworkhub_dashboard_health",
        listed,
        active_profile="manager",
        exposing_profiles=("dashboard", "all", "dashboard"),
    )
    payload = json.loads(message)
    assert payload["error"] == "unknown_tool"
    assert payload["reason"] == "not_in_active_profile"
    assert payload["requested"] == "aiworkhub_dashboard_health"
    assert payload["active_profile"] == "manager"
    assert payload["exposed_in"] == ["dashboard", "all"]
    assert payload["recovery"] == (
        "aiworkhub_dashboard_health is not exposed on the manager profile; profiles "
        "exposing it: dashboard, all. Select one with AIWORKHUB_MCP_SURFACE, or call "
        "tools/list and retry with one exact name the manager profile lists"
    )
    assert payload["suggestions"][0] == "aiworkhub_dashboard_snapshot"
    # Without exposing profiles (a name nothing serves) the generic guidance is unchanged.
    assert unknown_tool_message("nope", listed, active_profile="manager") == unknown_tool_message("nope", listed)
    bounded = json.loads(unknown_tool_message(
        "x", [], active_profile="bad profile\n", exposing_profiles=[f"p{i}" for i in range(50)] + ["a b"],
    ))
    assert bounded["active_profile"] == "<invalid>"
    assert bounded["exposed_in"] == [f"p{i}" for i in range(8)]
