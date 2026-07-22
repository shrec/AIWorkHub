from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import agent_tool_instructions, provider_tool_guards, task_store  # noqa: E402
from aiworkhub.runtime_adapters import CLAUDE_RAW_DISCOVERY_DENIES  # noqa: E402


def test_repository_guards_preserve_owner_content_and_are_idempotent(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# Owner policy\n", encoding="utf-8")
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(
        json.dumps({
            "model": "sonnet",
            "permissions": {
                "allow": ["Read", "Bash(python3 AITools/source_graph.py find x)"],
            },
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "python3 AITools/cgraph.py find x"}],
                }],
            },
        }),
        encoding="utf-8",
    )

    first = provider_tool_guards.apply_repository_guards(tmp_path)
    second = provider_tool_guards.apply_repository_guards(tmp_path)

    assert first["changed"]
    assert second["changed"] == []
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8").startswith("# Owner policy\n")
    for provider in agent_tool_instructions.PROVIDERS:
        text = (tmp_path / provider).read_text(encoding="utf-8")
        assert text.count(agent_tool_instructions.START) == 1
        assert text.count(agent_tool_instructions.END) == 1
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["model"] == "sonnet"
    assert settings["permissions"]["allow"] == ["Read"]
    assert tuple(settings["permissions"]["deny"]) == CLAUDE_RAW_DISCOVERY_DENIES
    assert "hooks" not in settings


def test_corrupt_claude_settings_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(provider_tool_guards.ProviderGuardError, match="claude_settings_invalid"):
        provider_tool_guards.apply_repository_guards(tmp_path)


def test_init_repo_installs_provider_guards(tmp_path: Path) -> None:
    result = task_store.initialize_repository(tmp_path)

    assert result["ok"] is True
    assert result["provider_guards"]["ok"] is True
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / "CLAUDE.md").is_file()
    assert (tmp_path / ".github" / "copilot-instructions.md").is_file()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert tuple(settings["permissions"]["deny"]) == CLAUDE_RAW_DISCOVERY_DENIES
