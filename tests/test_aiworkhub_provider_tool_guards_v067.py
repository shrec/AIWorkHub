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


# ---------------------------------------------------------------------------
# The project deny is ROLE-AWARE, or a reviewer's native-search grant is inert.
#
# runtime_adapters grants Grep/Glob to a read-only reviewer through
# --allowedTools. But the reviewer runs in a git worktree of THIS repository, so
# it checks out the tracked .claude/settings.json, whose permissions.deny lists
# Grep and Glob -- and a project deny beats an argv allow. Measured over 242
# claude reviewer runs: 1,080 denied Bash calls, 25% of every reviewer Bash
# call, in 181 of them.
# ---------------------------------------------------------------------------


def _deny(root: Path) -> list[str]:
    settings = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    return list(settings["permissions"]["deny"])


def _worktree_checkout_of_the_repository_settings(root: Path, tmp_path: Path) -> Path:
    """A worktree whose settings file is the repository's tracked bytes."""
    worktree = tmp_path / "worktree"
    (worktree / ".claude").mkdir(parents=True)
    (worktree / ".claude" / "settings.json").write_text(
        (root / ".claude" / "settings.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return worktree


def test_a_build_worker_tree_keeps_the_whole_raw_discovery_deny(tmp_path: Path) -> None:
    """Source Graph is the build worker's discovery path; nothing here weakens it."""
    root = tmp_path / "repo"
    root.mkdir()
    provider_tool_guards.apply_repository_guards(root)

    assert tuple(_deny(root)) == CLAUDE_RAW_DISCOVERY_DENIES
    assert {"Grep", "Glob"} <= set(_deny(root))
    assert provider_tool_guards.claude_settings_deny(read_only=False) == (
        CLAUDE_RAW_DISCOVERY_DENIES
    )


def test_a_reviewer_worktree_does_not_inherit_the_native_search_deny(
    tmp_path: Path,
) -> None:
    """The measured defect: the grant was real and the inherited deny killed it."""
    root = tmp_path / "repo"
    root.mkdir()
    provider_tool_guards.apply_repository_guards(root)
    worktree = _worktree_checkout_of_the_repository_settings(root, tmp_path)

    before = _deny(worktree)
    assert {"Grep", "Glob"} <= set(before), before

    result = provider_tool_guards.apply_workspace_guards(worktree, read_only=True)

    after = _deny(worktree)
    assert result["ok"] is True
    assert result["changed"] == [".claude/settings.json"]
    assert not ({"Grep", "Glob"} & set(after)), after
    # The bounded native tools are the substitute; an unbounded shell scan is not.
    assert {"Bash(grep *)", "Bash(rg *)", "Bash(find *)", "Bash(tree *)"} <= set(after)
    # And the canonical repository is untouched: only the worktree changed role.
    assert {"Grep", "Glob"} <= set(_deny(root))


def test_the_settings_deny_and_the_argv_deny_cannot_disagree() -> None:
    """One producer for both enforcement surfaces, so a role cannot drift."""
    from aiworkhub import runtime_adapters as ra

    for read_only in (True, False):
        assert provider_tool_guards.claude_settings_deny(read_only=read_only) == tuple(
            ra.claude_disallowed_tools(read_only=read_only)
        )


def test_a_build_worker_worktree_still_gets_the_full_deny(tmp_path: Path) -> None:
    """The role is a parameter, and the writing role is the default."""
    root = tmp_path / "repo"
    root.mkdir()
    provider_tool_guards.apply_repository_guards(root)
    worktree = _worktree_checkout_of_the_repository_settings(root, tmp_path)

    result = provider_tool_guards.apply_workspace_guards(worktree, read_only=False)

    assert result["changed"] == []  # already correct for this role
    assert {"Grep", "Glob"} <= set(_deny(worktree))


def test_the_reviewer_rewrite_is_idempotent_and_names_its_baseline_path(
    tmp_path: Path,
) -> None:
    """A caller must be able to fold the rewritten path into its own baseline."""
    root = tmp_path / "repo"
    root.mkdir()
    provider_tool_guards.apply_repository_guards(root)
    worktree = _worktree_checkout_of_the_repository_settings(root, tmp_path)

    first = provider_tool_guards.apply_workspace_guards(worktree, read_only=True)
    second = provider_tool_guards.apply_workspace_guards(worktree, read_only=True)

    assert first["changed"] == [".claude/settings.json"]
    assert second["changed"] == []
    assert first["baseline_paths"] == [".claude/settings.json"]
    assert second["baseline_paths"] == [".claude/settings.json"]


def test_an_owner_deny_is_never_dropped_by_the_role_rewrite(tmp_path: Path) -> None:
    """Only the entries the role surrenders are removed; owner policy survives."""
    worktree = tmp_path / "worktree"
    (worktree / ".claude").mkdir(parents=True)
    (worktree / ".claude" / "settings.json").write_text(
        json.dumps({
            "model": "sonnet",
            "permissions": {"deny": ["Grep", "Glob", "Bash(curl *)", "WebFetch"]},
        }),
        encoding="utf-8",
    )

    provider_tool_guards.apply_workspace_guards(worktree, read_only=True)

    settings = json.loads((worktree / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["model"] == "sonnet"
    assert "Bash(curl *)" in settings["permissions"]["deny"]
    assert "WebFetch" in settings["permissions"]["deny"]
    assert not ({"Grep", "Glob"} & set(settings["permissions"]["deny"]))
