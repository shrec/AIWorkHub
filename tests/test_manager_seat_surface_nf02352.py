"""NF-2026-02352: a Manager Chat seat serves the manager MCP profile, never the inherited dashboard one.

The Manager Chat loop runs inside the dashboard MCP child (AIWORKHUB_MCP_SURFACE=dashboard).
``_spawn_cli`` merges the seat env over that environment, so the seat env itself must pin the
manager surface. Fakes only: no provider CLI and no MCP client is ever started here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import manager_loop_backends as mlb  # noqa: E402
from aiworkhub import runtime_adapters, server  # noqa: E402

_SEAT_KEYS = ("AIWORKHUB_MANAGER_SEAT_BACKEND", "AIWORKHUB_MANAGER_SEAT_TOKEN")
_MANAGER_TOOLS = (
    "aiworkhub_manager_bootstrap",
    "aiworkhub_manager_source_graph_query",
    "aiworkhub_review_packet",
)


def _provision(repo: Path, backend_id: str) -> dict[str, str]:
    return mlb.provision_manager_seat_env(
        repo,
        backend_id,
        python_executable=sys.executable,
        package_import_root=str(_SRC),
    )


def _spawned_child_env(monkeypatch: pytest.MonkeyPatch, cwd: Path, env: dict[str, str]) -> dict[str, str]:
    """Run the shipped ``_spawn_cli`` merge against a fake Popen and return the child env."""
    captured: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            captured["argv"] = list(argv)
            captured["env"] = kwargs.get("env")

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(mlb.subprocess, "Popen", _FakePopen)
    mlb._spawn_cli(["claude", "-p", "ask"], str(cwd), env=env)
    child_env = captured["env"]
    assert isinstance(child_env, dict), "the seat child must get an explicit merged env"
    return child_env


def test_claude_seat_child_gets_the_manager_surface_over_an_inherited_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv(server.MCP_SURFACE_ENV, "dashboard")
    seat_env = _provision(tmp_path, "claude_cli")

    child_env = _spawned_child_env(monkeypatch, tmp_path, seat_env)

    assert child_env[server.MCP_SURFACE_ENV] == server.MCP_SURFACE_MANAGER
    for key in _SEAT_KEYS:
        assert child_env[key] == seat_env[key]
    assert child_env["AIWORKHUB_MANAGER_SEAT_BACKEND"] == "claude_cli"


def test_the_pinned_surface_serves_the_manager_profile_with_bootstrap_discovery_and_review(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(server.MCP_SURFACE_ENV, "dashboard")
    child_env = _spawned_child_env(monkeypatch, tmp_path, _provision(tmp_path, "claude_cli"))

    # The seat's stdio server inherits exactly this value from the claude child.
    monkeypatch.setenv(server.MCP_SURFACE_ENV, child_env[server.MCP_SURFACE_ENV])
    assert server.mcp_surface() == server.MCP_SURFACE_MANAGER
    for name in _MANAGER_TOOLS:
        assert server.mcp_tool_callable(name), name
        assert server.mcp_tool_callable(name, server.MCP_SURFACE_MANAGER), name


def test_codex_seat_config_pins_the_manager_surface_in_the_server_env(tmp_path, monkeypatch):
    tomllib = pytest.importorskip("tomllib")
    monkeypatch.setenv(server.MCP_SURFACE_ENV, "dashboard")
    env = _provision(tmp_path, "codex_cli")

    assert set(env) == {"CODEX_HOME"}
    config_path = Path(env["CODEX_HOME"]) / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    server_env = config["mcp_servers"]["AIWorkHub"]["env"]
    assert server_env[server.MCP_SURFACE_ENV] == server.MCP_SURFACE_MANAGER
    assert server_env["AIWORKHUB_MANAGER_SEAT_BACKEND"] == "codex_cli"
    assert "AIWORKHUB_MANAGER_SEAT_TOKEN" in server_env


def _seat_env_blocks(value: Any) -> list[dict[str, Any]]:
    """Every mapping in the opencode config that carries the seat token binding."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if "AIWORKHUB_MANAGER_SEAT_TOKEN" in value:
            found.append(value)
        for child in value.values():
            found.extend(_seat_env_blocks(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_seat_env_blocks(child))
    return found


def test_opencode_seat_config_pins_the_manager_surface_in_the_server_env(tmp_path, monkeypatch):
    monkeypatch.setenv(server.MCP_SURFACE_ENV, "dashboard")
    env = _provision(tmp_path, "opencode_cli")

    config = json.loads(env[runtime_adapters.OPENCODE_WORKER_CONFIG_ENV])
    blocks = _seat_env_blocks(config)
    assert blocks, "the opencode seat config must carry the seat server env"
    for block in blocks:
        assert block[server.MCP_SURFACE_ENV] == server.MCP_SURFACE_MANAGER
        assert block["AIWORKHUB_MANAGER_SEAT_BACKEND"] == "opencode_cli"


def test_the_seat_allowlist_accepts_the_surface_key_and_still_refuses_others():
    checked = runtime_adapters._manager_seat_mcp_environment(
        {server.MCP_SURFACE_ENV: server.MCP_SURFACE_MANAGER, "AIWORKHUB_REPO": "r"}
    )
    assert checked[server.MCP_SURFACE_ENV] == server.MCP_SURFACE_MANAGER
    with pytest.raises(ValueError, match="manager_seat_mcp_environment_invalid"):
        runtime_adapters._manager_seat_mcp_environment({"AIWORKHUB_WORKER_TOKEN": "x"})
