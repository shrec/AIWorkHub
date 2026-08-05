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
