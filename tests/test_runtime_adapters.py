"""Focused tests for pure runtime adapter command planning."""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import runtime_adapters  # noqa: E402


def _executable(tmp_path: Path, name: str) -> Path:
    executable = tmp_path / name
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable.resolve()


def test_claude_argv_is_current_noninteractive_shape(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "claude")
    calls = []

    def fake_which(binary):
        calls.append(binary)
        return str(executable)

    monkeypatch.setattr(runtime_adapters.shutil, "which", fake_which)
    plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        "Implement the focused change",
        repo,
        model="claude-sonnet-current",
    )

    assert calls == ["claude"]
    assert plan.argv == [
        str(executable),
        "-p",
        "Implement the focused change",
        "--output-format",
        "json",
        "--permission-mode",
        "auto",
        "--no-session-persistence",
        "--model",
        "claude-sonnet-current",
    ]
    assert "--cwd" not in plan.argv
    assert plan.cwd == str(repo.resolve())
    assert plan.executable == str(executable)
    assert plan.launchable is True
    assert plan.manual_only is False
    assert plan.validation_ok is True
    assert plan.validation_reason == ""

    with pytest.raises(FrozenInstanceError):
        plan.cwd = "/different"  # type: ignore[misc]


def test_claude_omits_model_when_not_requested(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "claude")
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(executable))

    plan = runtime_adapters.build_runtime_command("claude_cli", "Prompt", repo)

    assert plan.argv == [
        str(executable),
        "-p",
        "Prompt",
        "--output-format",
        "json",
        "--permission-mode",
        "auto",
        "--no-session-persistence",
    ]


def test_codex_argv_preserves_spaces_and_unicode(monkeypatch, tmp_path):
    repo = tmp_path / "repo with spaces თბილისი"
    repo.mkdir()
    executable = _executable(tmp_path, "codex")
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(executable))
    prompt = "შეასწორე café 東京; keep $TOKEN literal"

    plan = runtime_adapters.build_runtime_command(
        "codex_cli",
        prompt,
        repo,
        model="gpt model Ω",
    )

    assert plan.argv == [
        str(executable),
        "exec",
        "--json",
        "--ephemeral",
        "-s",
        "workspace-write",
        "-C",
        str(repo.resolve()),
        "--model",
        "gpt model Ω",
        prompt,
    ]
    assert plan.argv[-1] == prompt
    assert plan.cwd == str(repo.resolve())
    assert plan.launchable is True


def test_codex_inner_sandbox_can_be_disabled_only_by_explicit_outer_sandbox_env(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "codex")
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(executable))
    monkeypatch.setenv(
        runtime_adapters.CODEX_INNER_SANDBOX_MODE_ENV,
        "danger-full-access",
    )

    plan = runtime_adapters.build_runtime_command("codex_cli", "prompt", repo)

    assert plan.launchable is True
    assert plan.argv[plan.argv.index("-s") + 1] == "danger-full-access"


def test_invalid_adapter_is_rejected_without_discovery(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def unexpected_which(_):
        raise AssertionError("unsupported adapters must not trigger discovery")

    monkeypatch.setattr(runtime_adapters.shutil, "which", unexpected_which)
    plan = runtime_adapters.build_runtime_command("shell_cli", "Prompt", repo)

    assert plan.adapter_id == "shell_cli"
    assert plan.argv == []
    assert plan.executable is None
    assert plan.launchable is False
    assert plan.manual_only is False
    assert plan.validation_ok is False
    assert plan.validation_reason == "unsupported adapter"


def test_missing_repo_is_rejected_before_discovery(monkeypatch, tmp_path):
    def unexpected_which(_):
        raise AssertionError("an invalid cwd must stop executable discovery")

    monkeypatch.setattr(runtime_adapters.shutil, "which", unexpected_which)
    plan = runtime_adapters.build_runtime_command(
        "claude_cli", "Prompt", tmp_path / "missing"
    )

    assert plan.argv == []
    assert plan.cwd is None
    assert plan.launchable is False
    assert plan.validation_ok is False
    assert "repo path" in plan.validation_reason


@pytest.mark.parametrize("prompt", ["", "  \t\n"])
def test_prompt_must_be_nonempty(prompt, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    plan = runtime_adapters.build_runtime_command("codex_cli", prompt, repo)

    assert plan.argv == []
    assert plan.validation_ok is False
    assert plan.validation_reason == "prompt must be a nonempty string"


def test_deepseek_is_clear_manual_only_status(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def unexpected_which(_):
        raise AssertionError("manual-only adapters have no executable")

    monkeypatch.setattr(runtime_adapters.shutil, "which", unexpected_which)
    plan = runtime_adapters.build_runtime_command(
        "deepseek_manual", "Review this task manually", repo
    )

    assert plan.adapter_id == "deepseek_manual"
    assert plan.argv == []
    assert plan.cwd == str(repo.resolve())
    assert plan.executable is None
    assert plan.launchable is False
    assert plan.manual_only is True
    assert plan.validation_ok is True
    assert "manual-only" in plan.validation_reason
    assert plan.as_dict() == {
        "adapter_id": "deepseek_manual",
        "argv": [],
        "cwd": str(repo.resolve()),
        "executable": None,
        "launchable": False,
        "manual_only": True,
        "validation_ok": True,
        "validation_reason": "manual-only adapter; no local command is available",
    }


def test_absolute_executable_override_bypasses_which(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    override = _executable(tmp_path, "custom claude")

    def unexpected_which(_):
        raise AssertionError("an explicit override must take precedence")

    monkeypatch.setattr(runtime_adapters.shutil, "which", unexpected_which)
    plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        "Prompt",
        repo,
        executable_overrides={"claude_cli": override},
    )

    assert plan.executable == str(override)
    assert plan.argv[0] == str(override)
    assert plan.launchable is True


@pytest.mark.parametrize("kind", ["relative", "missing", "directory", "not_executable"])
def test_unsafe_executable_overrides_are_rejected(kind, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    if kind == "relative":
        override = Path("relative/claude")
    elif kind == "missing":
        override = tmp_path / "missing-claude"
    elif kind == "directory":
        override = tmp_path / "bin-dir"
        override.mkdir()
    else:
        override = tmp_path / "claude-no-exec"
        override.write_text("not executable", encoding="utf-8")
        override.chmod(0o644)

    plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        "Prompt",
        repo,
        executable_overrides={"claude_cli": override},
    )

    assert plan.argv == []
    assert plan.executable is None
    assert plan.launchable is False
    assert plan.validation_ok is False
    assert "override" in plan.validation_reason


def test_unsupported_override_key_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "claude")

    plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        "Prompt",
        repo,
        executable_overrides={"unknown_cli": executable},
    )

    assert plan.argv == []
    assert plan.validation_ok is False
    assert plan.validation_reason == "executable overrides contain an unsupported adapter key"


def test_missing_discovered_executable_is_not_launchable(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: None)

    plan = runtime_adapters.build_runtime_command("codex_cli", "Prompt", repo)

    assert plan.argv == []
    assert plan.executable is None
    assert plan.launchable is False
    assert plan.validation_ok is False
    assert plan.validation_reason == "executable not found: codex"


def test_module_has_no_process_execution_dependency():
    assert "subprocess" not in runtime_adapters.__dict__
