from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_worker_mcp_starts_without_optional_mcp_package(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(ROOT / "src"),
        "AIWORKHUB_WORKER_MCP_TASK_ID": "FALLBACK_CANARY",
        "AIWORKHUB_WORKER_MCP_RUNNER": "claude_sonnet5",
        "AIWORKHUB_WORKER_MCP_TOPIC": "task_mcp",
        "AIWORKHUB_WORKER_MCP_REPO": str(tmp_path),
        "AIWORKHUB_WORKER_MCP_AUTHORITY_REPO": str(tmp_path),
    })
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "fallback-test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    completed = subprocess.run(
        [sys.executable, "-S", "-m", "aiworkhub.worker_ai_tools_mcp"],
        input="".join(json.dumps(item) + "\n" for item in requests),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
        check=True,
    )
    responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert responses[0]["result"]["serverInfo"]["name"] == "AIWorkHub Worker AI Tools"
    tools = {item["name"]: item for item in responses[1]["result"]["tools"]}
    assert set(tools) == {
        "aiworkhub_worker_source_graph_query",
        "aiworkhub_worker_session_current_state",
        "aiworkhub_worker_ai_memory_search",
        "aiworkhub_worker_kb_search",
        "aiworkhub_worker_kb_get",
        "aiworkhub_worker_kb_related",
    }
    source_schema = tools["aiworkhub_worker_source_graph_query"]["inputSchema"]
    assert source_schema["properties"]["budget"]["type"] == "integer"
    assert source_schema["properties"]["target"]["type"] == "string"
