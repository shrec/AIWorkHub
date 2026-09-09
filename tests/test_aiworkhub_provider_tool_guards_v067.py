from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import agent_tool_instructions, provider_tool_guards, task_store  # noqa: E402
from aiworkhub.runtime_adapters import (  # noqa: E402
    CLAUDE_RAW_DISCOVERY_DENIES,
    claude_disallowed_tools,
)


def _build_worker_deny() -> tuple[str, ...]:
    """The build worker's TREE deny, stated as the role rather than a literal.

    Derived from the settings producer, not the argv producer: the two are
    deliberately not identical any more (see
    ``test_the_launch_only_validation_deny_never_reaches_a_tracked_tree``).
    """

    return provider_tool_guards.claude_settings_deny(read_only=False)


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
    assert tuple(settings["permissions"]["deny"]) == _build_worker_deny()
    assert set(CLAUDE_RAW_DISCOVERY_DENIES) <= set(settings["permissions"]["deny"])
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
    assert tuple(settings["permissions"]["deny"]) == _build_worker_deny()


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

    assert tuple(_deny(root)) == _build_worker_deny()
    assert set(CLAUDE_RAW_DISCOVERY_DENIES) <= set(_deny(root))
    assert {"Grep", "Glob"} <= set(_deny(root))
    assert provider_tool_guards.claude_settings_deny(read_only=False) == (
        _build_worker_deny()
    )


def test_the_launch_only_editor_deny_never_reaches_a_tracked_tree(
    tmp_path: Path,
) -> None:
    """``Edit`` is denied at the LAUNCH, and must not be denied in the repo.

    This settings file is TRACKED, and a worker worktree checks out exactly
    these bytes -- but so does every human session and every interactive
    Claude Code session opened on the repository, none of whom are the build
    workers the rule aims at.  The argv carries the deny because that is where
    the role exists; the tree must not, for the same reason the raw validation
    spellings were subtracted before it.
    """
    from aiworkhub.runtime_adapters import CLAUDE_WORKER_RAW_EDITOR_DENIES

    root = tmp_path / "repo"
    root.mkdir()
    provider_tool_guards.apply_repository_guards(root)

    tree_deny = set(_deny(root))
    launch_deny = set(claude_disallowed_tools(read_only=False))

    assert set(CLAUDE_WORKER_RAW_EDITOR_DENIES) <= launch_deny
    assert not (set(CLAUDE_WORKER_RAW_EDITOR_DENIES) & tree_deny)
    assert "Edit" not in tree_deny
    # Nothing else moved: the tracked deny is still exactly raw discovery.
    assert tree_deny == set(CLAUDE_RAW_DISCOVERY_DENIES)


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


def test_the_settings_deny_and_the_argv_deny_cannot_disagree_about_a_role() -> None:
    """One producer for both enforcement surfaces, so a role cannot drift.

    Exactly TWO launch-only sets are subtracted on the settings side, and they
    are the only permitted difference: the raw validation spellings and the raw
    file editor.  Both are subtracted for the same reason -- this settings file
    is tracked, so a rule written there binds every human and interactive
    session on the repository, not the build workers it aims at.  Each is
    pinned by its own test below; this one pins that there is nothing ELSE.
    """
    from aiworkhub import runtime_adapters as ra

    launch_only = set(ra.CLAUDE_WORKER_VALIDATION_SHELL_DENIES) | set(
        ra.CLAUDE_WORKER_RAW_EDITOR_DENIES
    )
    for read_only in (True, False):
        settings = set(provider_tool_guards.claude_settings_deny(read_only=read_only))
        argv = set(ra.claude_disallowed_tools(read_only=read_only))
        assert settings <= argv
        assert argv - settings <= launch_only


def test_the_launch_only_validation_deny_never_reaches_a_tracked_tree(
    tmp_path: Path,
) -> None:
    """``Bash(pytest *)`` belongs to the launch argv, never to a settings file.

    Two measured facts put it there and keep it there.  Claude Bash rules are
    prefix matches, so ``Bash(pytest *)`` cannot match ``<python> -m pytest``,
    which is how every invocation in this repository is actually spelled -- the
    settings entry would buy nothing against the real form.  And
    ``.claude/settings.json`` is TRACKED: a worktree, a human session, another
    Claude Code session and the manager seat all inherit it, and none of them
    are the build worker the rule aims at.  So the argv carries it and the tree
    never does.
    """
    from aiworkhub import runtime_adapters as ra

    validation_denies = set(ra.CLAUDE_WORKER_VALIDATION_SHELL_DENIES)
    assert validation_denies == {"Bash(pytest *)", "Bash(ruff *)", "Bash(mypy *)"}

    # The launch argv carries them for a build worker.
    assert validation_denies <= set(ra.claude_disallowed_tools(read_only=False))

    # No settings surface does, for either role.
    assert not (validation_denies & set(
        provider_tool_guards.claude_settings_deny(read_only=False)
    ))
    assert not (validation_denies & set(
        provider_tool_guards.claude_settings_deny(read_only=True)
    ))

    root = tmp_path / "repo"
    root.mkdir()
    provider_tool_guards.apply_repository_guards(root)
    assert not (validation_denies & set(_deny(root)))

    worktree = _worktree_checkout_of_the_repository_settings(root, tmp_path)
    provider_tool_guards.apply_workspace_guards(worktree, read_only=False)
    assert not (validation_denies & set(_deny(worktree)))

    # A tree that inherited them from an earlier provisioning is repaired, not
    # left carrying a rule that belongs to the argv.
    settings_path = worktree / provider_tool_guards.CLAUDE_SETTINGS_REL
    stale = json.loads(settings_path.read_text(encoding="utf-8"))
    stale["permissions"]["deny"] = [*stale["permissions"]["deny"], "Bash(pytest *)"]
    settings_path.write_text(json.dumps(stale), encoding="utf-8")

    provider_tool_guards.apply_workspace_guards(worktree, read_only=False)

    assert "Bash(pytest *)" not in set(_deny(worktree))


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
