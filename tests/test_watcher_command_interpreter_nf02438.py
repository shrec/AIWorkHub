"""NF-2026-02438: the published watcher command names the interpreter and PYTHONPATH.

A bare ``python -m aiworkhub.manager_callback_watch`` fails with
ModuleNotFoundError where ``aiworkhub`` lives only in the bundled runtime.  Every
published command comes from ``tool_guard_hook.manager_watch_command`` and uses
the running server's ``sys.executable`` plus a PYTHONPATH naming the directory
that contains the imported ``aiworkhub`` package.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import callback_store, core
from aiworkhub import tool_guard_hook as hook

_MODULE_ARGS = "-m aiworkhub.manager_callback_watch"


def _package_parent() -> str:
    import aiworkhub

    return Path(aiworkhub.__file__).resolve().parent.parent.as_posix()


def test_launcher_uses_running_interpreter_and_package_parent(tmp_path: Path) -> None:
    argv, env = hook.watcher_launcher(tmp_path)
    assert argv == [Path(sys.executable).as_posix(), "-m", "aiworkhub.manager_callback_watch"]
    assert env == {"PYTHONPATH": _package_parent()}


def test_command_is_an_env_prefix_then_the_interpreter(tmp_path: Path) -> None:
    command = hook.manager_watch_command(tmp_path, "sess-1")
    words = shlex.split(command)
    assert words[0] == f"PYTHONPATH={_package_parent()}"
    assert words[1] == Path(sys.executable).as_posix()
    assert words[2:4] == ["-m", "aiworkhub.manager_callback_watch"]
    assert words[words.index("--repo") + 1] == tmp_path.as_posix()
    assert words[words.index("--origin-thread-id") + 1] == "sess-1"
    assert words[words.index("--timeout") + 1] == "86400"


def test_paths_with_spaces_are_shell_quoted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "executable", "/opt/my python/bin/python")
    words = shlex.split(hook.manager_watch_command(tmp_path, "sess-1"))
    assert words[1] == "/opt/my python/bin/python"


def test_placeholder_rendering_keeps_the_documented_arguments() -> None:
    command = hook.manager_watch_command(None, None, provider=None, timeout=None)
    assert command.endswith(
        f"{_MODULE_ARGS} --repo <repo> --provider <provider> --origin-thread-id <manager session id>"
    )
    assert "--timeout" not in command


def test_every_published_text_uses_the_one_helper() -> None:
    placeholder = hook.manager_watch_command(None, None, provider=None, timeout=None)
    for text in (core.tool_guard_hook.claude_callback_contract(), callback_store.MANAGER_INBOX_REMEDIATION):
        assert f"`{placeholder}`" in text
        assert "`python -m" not in text
        assert "PYTHONPATH=" in text
        assert Path(sys.executable).as_posix() in text


@pytest.mark.skipif(os.name == "nt", reason="the env-prefix command form is POSIX shell syntax")
def test_published_command_runs_in_a_posix_shell_with_a_clean_environment(tmp_path: Path) -> None:
    command = hook.manager_watch_command(None, None, provider=None, timeout=None)
    words = shlex.split(command)
    assert words[0].startswith("PYTHONPATH=")
    # Substitute the placeholders with --help so only the import path is exercised.
    prefix = command.split(" --repo ", 1)[0]
    result = subprocess.run(
        ["/bin/sh", "-c", f"{prefix} --help"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()


def test_unresolvable_package_never_publishes_a_bare_failing_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delitem(sys.modules, "aiworkhub", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    text = hook.manager_watch_command(tmp_path, "sess-1")
    assert "-m aiworkhub.manager_callback_watch" not in text
    assert "PYTHONPATH" in text


def test_pythonpath_env_naming_the_runtime_is_used_when_package_is_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "aiworkhub").mkdir(parents=True)
    (runtime / "aiworkhub" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.delitem(sys.modules, "aiworkhub", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
    monkeypatch.setenv("PYTHONPATH", str(runtime))
    words = shlex.split(hook.manager_watch_command(tmp_path / "repo", "sess-1"))
    assert words[0] == f"PYTHONPATH={runtime.resolve().as_posix()}"
    assert words[2:4] == ["-m", "aiworkhub.manager_callback_watch"]


def test_produced_argv_and_env_import_the_watcher_in_a_clean_environment(tmp_path: Path) -> None:
    if importlib.util.find_spec("aiworkhub.manager_callback_watch") is None:
        pytest.skip("aiworkhub.manager_callback_watch is not checked out in this (sparse) tree")
    argv, env = hook.watcher_launcher(tmp_path)
    clean_env = dict(env)
    if os.name == "nt":
        clean_env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    result = subprocess.run(
        [*argv, "--help"],
        cwd=tmp_path,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "usage" in result.stdout.lower()
