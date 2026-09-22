"""The host-side manager launch plan (NF-2026-00963).

A worker CLI stays confined on Windows; the owner's manager seat is planned
by :func:`aiworkhub.runtime_adapters.build_manager_command` and is not.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from aiworkhub import manager_loop_backends, runtime_adapters


def test_worker_command_still_refuses_a_native_cli_on_windows(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: True)
    plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        "p",
        tmp_path,
        executable_overrides={"claude_cli": sys.executable},
    )
    assert plan.launchable is False
    assert (
        plan.validation_reason
        == "windows_native_cli_requires_appcontainer_sandbox"
    )


def test_manager_command_is_launchable_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: True)
    plan = runtime_adapters.build_manager_command(
        "claude_cli",
        "p",
        tmp_path,
        executable_overrides={"claude_cli": sys.executable},
    )
    assert plan.launchable is True
    assert plan.argv


def test_worker_and_manager_argv_are_equal_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
    # The same model on both sides: a manager plan that dropped it would differ.
    worker_plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        "p",
        tmp_path,
        model="claude-opus-5",
        executable_overrides={"claude_cli": sys.executable},
    )
    manager_plan = runtime_adapters.build_manager_command(
        "claude_cli",
        "p",
        tmp_path,
        model="claude-opus-5",
        executable_overrides={"claude_cli": sys.executable},
    )
    assert "claude-opus-5" in manager_plan.argv
    assert manager_plan.argv == worker_plan.argv


def test_manager_command_refuses_an_unknown_adapter(tmp_path):
    plan = runtime_adapters.build_manager_command("no_such_adapter", "p", tmp_path)
    assert plan.launchable is False


def test_cli_manager_backend_defaults_to_the_manager_command(tmp_path):
    backend = manager_loop_backends.CliManagerBackend("claude_cli", "m", tmp_path)
    assert backend._plan_builder is runtime_adapters.build_manager_command


def test_build_manager_command_is_referenced_only_by_its_two_modules():
    allowed = {"runtime_adapters.py", "manager_loop_backends.py"}
    package_dir = Path(runtime_adapters.__file__).parent
    for path in sorted(package_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        identifiers = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
        if "build_manager_command" in identifiers:
            assert path.name in allowed
