from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "aiworkhub"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_canonical_package_and_public_entrypoints() -> None:
    pyproject = _read("pyproject.toml")
    assert 'name = "aiworkhub"' in pyproject
    assert 'aiworkhub = "aiworkhub.server:main"' in pyproject
    assert 'awh = "aiworkhub.server:main"' in pyproject
    assert "aiworkhub-dashboard" in pyproject
    assert (SRC / "__init__.py").is_file()
    assert not (ROOT / "src" / ("geo" + "ai_task_mcp")).exists()


def test_server_and_worker_tools_use_aiworkhub_namespace() -> None:
    server = _read("src/aiworkhub/server.py")
    worker = _read("src/aiworkhub/worker_ai_tools_mcp.py")
    assert 'FastMCP("AIWorkHub MCP")' in server
    assert "def aiworkhub_task_health" in server
    assert "def aiworkhub_agent_launch_task" in server
    assert 'SERVER_NAME = "aiworkhub_worker_ai_tools"' in worker
    for old in ("geo" + "ai_task_", "geo" + "ai_agent_", "GEO" + "AI_"):
        assert old not in server
        assert old not in worker


def test_environment_state_and_instruction_policy_are_canonical() -> None:
    repository_state = _read("src/aiworkhub/repository_state.py")
    instructions = _read("src/aiworkhub/agent_tool_instructions.py")
    assert 'HUB_DIRNAME = ".aiworkhub"' in repository_state
    assert "AIWORKHUB_REPO_ROOT" in repository_state
    assert "AIWorkHub MCP tool-use policy" in instructions
    assert "Source Graph is required for code tasks." in instructions
    assert "Stop at Codex review." in instructions
    assert "aiworkhub_worker_source_graph_query" in instructions


def test_readme_and_migration_manifest_are_release_facing() -> None:
    readme = _read("README.md")
    manifest = json.loads(_read("eval/aiworkhub_canonical_rebrand_b837_v1.json"))
    assert readme.startswith("# AIWorkHub")
    assert "local-first control plane for multi-model software development" in readme
    assert "docs/assets/aiworkhub-hero.svg" in readme
    assert (ROOT / "docs" / "assets" / "aiworkhub-hero.svg").is_file()
    assert (ROOT / "docs" / "BRAND.md").is_file()
    assert manifest["canonical"]["display_name"] == "AIWorkHub"
    assert manifest["canonical"]["abbreviation"] == "AWH"
    assert manifest["canonical"]["python_package"] == "aiworkhub"
    assert manifest["scan_evidence"]["case_insensitive_old_brand_occurrences"] == 0
