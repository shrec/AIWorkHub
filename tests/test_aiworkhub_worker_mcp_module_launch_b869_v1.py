"""B869: real stdio subprocess launch proof for the worker AI-tools MCP server.

Before this repair every generated Claude/Copilot/Codex config launched this
server as ``command=<python>`` ``args=[<worker_ai_tools_mcp.py path>]`` --
executing the file directly as ``__main__``. This module's own
``from .repository_state import RepositoryStateError`` is a *relative*
import, which raises ``ImportError: attempted relative import with no known
parent package`` before ``ctx`` is even bound or a single tool registered.
No unit test caught this because every prior B833/B834 test drove the
Python functions in-process; none ever spawned the generated
command/args/env as a real OS subprocess. This test closes that gap: it
reads back the exact generated adapter config, spawns EXACTLY that
command/args/env as a real subprocess over OS stdio pipes, and drives it
through a real ``mcp.ClientSession`` (initialize -> tools/list -> tools/call)
-- the same path a live Claude/Codex/Copilot session uses.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import get_default_environment, stdio_client  # noqa: E402

from aiworkhub import repository_state  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as w  # noqa: E402

REQUEST_TIMEOUT = timedelta(seconds=30)


def _write_tool(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _authority_repo(tmp_path: Path) -> Path:
    """Same fake-authority-repo shape as the B833/B834 fixtures: real
    non-empty legacy databases plus a bootstrapped manifest/registry plus
    fixed-argv, fake ``AITools/*.py`` stand-ins -- enough for one live,
    bounded read-only tool call over the real stdio pipe."""
    repo = tmp_path / "authority_repo"
    (repo / "AITools" / "ai_memory").mkdir(parents=True)
    for relative in (
        "AITools/session.db", "AITools/transcript_graph.db",
        "AITools/ai_memory/ai_memory.db", "AITools/kb.db",
    ):
        (repo / relative).write_bytes(b"SQLite format 3\x00fake-non-empty-authority-db")
    repository_state.bootstrap_repository(repo)
    source_graph_db = repo / ".aiworkhub" / "source_graph" / "source_graph.sqlite"
    source_graph_db.parent.mkdir(parents=True, exist_ok=True)
    source_graph_db.write_bytes(b"SQLite format 3\x00fake-non-empty-source-graph-db")
    _write_tool(
        repo / "AITools/transcript_graph.py",
        "import json, sys\n"
        "print(json.dumps({'state': 'partial_state', 'evidence': [{'id': 1}], 'topic': sys.argv[2]}))\n",
    )
    _write_tool(
        repo / "AITools/ai_memory/ai_memory.py",
        "import json\nprint(json.dumps({'count': 1, 'results': [{'key': 'ctx', 'value': 'bounded'}]}))\n",
    )
    _write_tool(
        repo / "AITools/kb.py",
        "import sys\nprint('[module] bounded KB context over real stdio pipe')\n",
    )
    return repo


def _server_params(config_path: Path) -> StdioServerParameters:
    """Read back the exact generated adapter config (the same artifact a
    real Claude session launches via --mcp-config) and spawn precisely that
    command/args/env -- never a hand-written shortcut argv."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    server = config["mcpServers"][w.SERVER_NAME]
    env = get_default_environment()
    env.update(server["env"])
    return StdioServerParameters(command=server["command"], args=server["args"], env=env)


async def _run_handshake(params: StdioServerParameters) -> dict:
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=REQUEST_TIMEOUT) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            call = await session.call_tool(
                "aiworkhub_worker_kb_search", {"query": "module launch smoke"},
                read_timeout_seconds=REQUEST_TIMEOUT,
            )
            return {"tool_names": names, "call_is_error": call.isError, "call": call}


def test_worker_mcp_server_launches_as_a_package_module_and_serves_tools_over_real_stdio(
    tmp_path: Path,
) -> None:
    authority_repo = _authority_repo(tmp_path)
    home = tmp_path / "home"
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id="req_b869", task_id="TASK_B869", runner="claude_b869",
        topic="task_mcp", repo=authority_repo, authority_repo=authority_repo,
        source_graph_targets=[], session_topic="AIWorkHub worker MCP module launch B869",
        package_import_root=_SRC,
    )

    params = _server_params(runtime.claude_mcp_config_path)
    # Never the bare relative-importing file: args[0] is the module flag and
    # the last arg is a dotted module name, not an existing file path.
    assert params.args[0] == "-m"
    assert not Path(params.args[-1]).exists()
    assert params.args[-1] == "aiworkhub.worker_ai_tools_mcp"
    assert Path(params.env["PYTHONPATH"]).is_dir()

    result = asyncio.run(_run_handshake(params))
    assert result["tool_names"] == set(w.MCP_TOOL_NAMES)
    assert len(w.MCP_TOOL_NAMES) == 8
    assert result["call_is_error"] is not True


def test_worker_mcp_server_copilot_and_codex_configs_also_launch_as_module(tmp_path: Path) -> None:
    authority_repo = _authority_repo(tmp_path)
    home = tmp_path / "home"
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id="req_b869b", task_id="TASK_B869", runner="claude_b869",
        topic="task_mcp", repo=authority_repo, authority_repo=authority_repo,
        source_graph_targets=[], session_topic="AIWorkHub worker MCP module launch B869",
        package_import_root=_SRC,
    )
    copilot_params = _server_params(runtime.copilot_mcp_config_path)
    assert copilot_params.args == ["-m", "aiworkhub.worker_ai_tools_mcp"]

    codex_toml = runtime.codex_config_toml_path.read_text(encoding="utf-8")
    assert 'args = ["-m", "aiworkhub.worker_ai_tools_mcp"]' in codex_toml
    assert f'{w.ENV_PYTHONPATH} = ' in codex_toml


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
