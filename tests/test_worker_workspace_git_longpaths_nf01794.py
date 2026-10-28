"""NF-2026-01794: every git spawn runs with ``core.longpaths=true``.

The nested union validation sandbox pushes worktree paths past Windows
MAX_PATH, and Windows git defaults to ``core.longpaths=false`` ("Filename too
long"). ``_git_environment`` injects the setting through ``GIT_CONFIG_*`` after
scrubbing every inherited ``GIT_*`` variable.
"""

from __future__ import annotations

import pytest

from aiworkhub import worker_workspace


def test_git_environment_enables_core_longpaths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)

    env = worker_workspace._git_environment()

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "core.longpaths"
    assert env["GIT_CONFIG_VALUE_0"] == "true"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_environment_still_scrubs_inherited_git_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    monkeypatch.setenv("GIT_CONFIG_KEY_2", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_2", "/tmp/hooks")
    monkeypatch.setenv("AIWORKHUB_NF01794_KEEP", "kept")

    env = worker_workspace._git_environment()

    assert "GIT_DIR" not in env
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert "GIT_CONFIG_KEY_2" not in env
    assert "GIT_CONFIG_VALUE_2" not in env
    assert env["AIWORKHUB_NF01794_KEEP"] == "kept"
    assert sorted(key for key in env if key.upper().startswith("GIT_")) == [
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_OPTIONAL_LOCKS",
        "GIT_TERMINAL_PROMPT",
    ]
