"""Manager seat MCP contracts: the Task MCP surface a Manager Chat seat holds.

A seat drafts cards and reads the control plane; it never launches workers,
never finalizes review, and never edits the tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import runtime_adapters as ra


def test_manager_seat_contract_requires_the_card_opening_core() -> None:
    with pytest.raises(ra.OpenCodeWorkerConfigError) as exc:
        ra.require_manager_seat_tool_contract(["aiworkhub_manager_bootstrap"])
    assert "aiworkhub_task_create" in str(exc.value)


def test_manager_seat_codex_tools_include_card_opening_and_discovery() -> None:
    tools = ra.resolve_manager_seat_codex_tools()
    for name in (
        "aiworkhub_manager_bootstrap",
        "aiworkhub_manager_source_graph_query",
        "aiworkhub_task_create",
        "aiworkhub_task_create_from_template",
    ):
        assert name in tools
    for banned in (
        "aiworkhub_agent_launch_task",
        "aiworkhub_task_mark_done",
        "aiworkhub_task_reject_review",
        "aiworkhub_manager_semantic_edit_apply",
        "aiworkhub_manager_loop_send",
    ):
        assert banned not in tools


def test_manager_codex_config_toml_names_the_seat_server_and_tools() -> None:
    text = ra.build_manager_codex_config_toml(
        python_executable=sys.executable,
        launch_args=[],
        environment={"AIWORKHUB_REPO": "D:\\r", "AIWORKHUB_ALLOW_WRITES": "1"},
    )
    assert "[mcp_servers.AIWorkHub]" in text
    assert "aiworkhub_task_create" in text
    assert "aiworkhub_agent_launch_task" not in text
    assert 'AIWORKHUB_REPO = "D:\\\\r"' in text
    assert text.endswith("\n")


def test_opencode_manager_contract_denies_everything_but_the_seat() -> None:
    permission = ra.opencode_manager_permission_contract()
    assert permission["*"] == ra.OPENCODE_PERMISSION_DENY
    assert permission["read"] == ra.OPENCODE_PERMISSION_DENY
    assert permission["awh_aiworkhub_task_create"] == ra.OPENCODE_PERMISSION_ALLOW
    assert "awh_aiworkhub_agent_launch_task" not in permission
    assert "awh_aiworkhub_task_mark_done" not in permission
    assert "awh_aiworkhub_manager_semantic_edit_apply" not in permission
    assert "awh_aiworkhub_manager_loop_send" not in permission


def test_opencode_manager_config_build_validate_roundtrip() -> None:
    config = ra.build_opencode_manager_mcp_config(
        [sys.executable, "-m", "aiworkhub.server"],
        environment={"AIWORKHUB_REPO": "D:\\r"},
    )
    assert ra.validate_opencode_manager_config(config) is config


def test_opencode_manager_config_rejects_a_widened_permission() -> None:
    config = ra.build_opencode_manager_mcp_config([sys.executable, "-m", "aiworkhub.server"])
    config["permission"]["awh_aiworkhub_agent_launch_task"] = ra.OPENCODE_PERMISSION_ALLOW
    with pytest.raises(ra.OpenCodeWorkerConfigError):
        ra.validate_opencode_manager_config(config)
