"""Focused tests for pure runtime adapter command planning."""

from __future__ import annotations

import os
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import runtime_adapters  # noqa: E402


@pytest.fixture(autouse=True)
def _exercise_portable_adapter_planning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep command-shape tests independent of the executing host OS."""

    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)


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
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        "Read",
        "Write",
        "Edit",
        "Bash",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_source_graph_query",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_semantic_edit_prepare",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_semantic_edit_apply",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_session_current_state",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_ai_memory_search",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_ai_memory_get",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_ai_memory_related",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_kb_search",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_kb_get",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_kb_related",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_session_write_intent",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_ai_memory_write_intent",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_kb_write_intent",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_quality_review_submit",
        "--no-session-persistence",
        "--disallowedTools",
        *runtime_adapters.CLAUDE_RAW_DISCOVERY_DENIES,
        "--model",
        "claude-sonnet-current",
    ]


def test_claude_partial_stream_is_opt_in_for_explicit_live_budget(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "claude")
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(executable))

    compact = runtime_adapters.build_runtime_command("claude_cli", "Prompt", repo)
    live_budget = runtime_adapters.build_runtime_command(
        "claude_cli", "Prompt", repo, include_partial_messages=True
    )

    assert "--include-partial-messages" not in compact.argv
    assert "--include-partial-messages" in live_budget.argv
    assert "--cwd" not in compact.argv
    assert compact.cwd == str(repo.resolve())
    assert compact.executable == str(executable)
    assert compact.launchable is True
    assert compact.manual_only is False
    assert compact.validation_ok is True
    assert compact.validation_reason == ""

    with pytest.raises(FrozenInstanceError):
        compact.cwd = "/different"  # type: ignore[misc]


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
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        "Read",
        "Write",
        "Edit",
        "Bash",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_source_graph_query",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_semantic_edit_prepare",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_semantic_edit_apply",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_session_current_state",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_ai_memory_search",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_ai_memory_get",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_ai_memory_related",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_kb_search",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_kb_get",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_kb_related",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_session_write_intent",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_ai_memory_write_intent",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_kb_write_intent",
        "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_quality_review_submit",
        "--no-session-persistence",
        "--disallowedTools",
        *runtime_adapters.CLAUDE_RAW_DISCOVERY_DENIES,
    ]


def test_claude_raw_discovery_is_provider_denied(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "claude")
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(executable))

    plan = runtime_adapters.build_runtime_command("claude_cli", "Prompt", repo)

    start = plan.argv.index("--disallowedTools") + 1
    assert tuple(plan.argv[start:]) == runtime_adapters.CLAUDE_RAW_DISCOVERY_DENIES


def test_claude_worker_uses_bounded_noninteractive_permissions(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "claude")
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(executable))

    plan = runtime_adapters.build_runtime_command("claude_cli", "Prompt", repo)

    assert plan.argv[plan.argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--allowedTools" in plan.argv
    assert "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_source_graph_query" in plan.argv
    assert "--dangerously-skip-permissions" not in plan.argv
    assert "--allow-dangerously-skip-permissions" not in plan.argv


def test_claude_has_no_automatic_monetary_budget_cap(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "claude")
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(executable))

    plan = runtime_adapters.build_runtime_command("claude_cli", "Prompt", repo)
    assert "--max-budget-usd" not in plan.argv


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
    if kind == "not_executable" and os.name == "nt":
        pytest.skip("Windows has no POSIX executable mode bit")
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


def test_grok_kilo_runtime_plan_is_exact_and_preserves_prompt(tmp_path):
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    executable = _executable(tmp_path, "kilo")
    prompt = "Inspect one bounded target; keep $TOKEN literal თბილისი"

    plan = runtime_adapters.build_runtime_command(
        "grok_kilo_cli",
        prompt,
        repo,
        executable_overrides={"grok_kilo_cli": executable},
    )

    assert plan.launchable is True
    assert plan.argv == [
        str(executable),
        "run",
        "--pure",
        "--model",
        "xai/grok-4.6",
        "--format",
        "json",
        "--dir",
        str(repo.resolve()),
        "--auto",
        prompt,
    ]
    assert plan.argv.count(prompt) == 1
    assert not any("AUTH" in token or "XDG_" in token for token in plan.argv)


@pytest.mark.parametrize(
    "model",
    ["", "   ", "grok-4.6", "xai/grok-4", "openai/gpt-5", "xai/grok-4.6\x00"],
)
def test_grok_kilo_rejects_every_noncanonical_model(model, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "kilo")

    plan = runtime_adapters.build_runtime_command(
        "grok_kilo_cli",
        "Prompt",
        repo,
        model=model,
        executable_overrides={"grok_kilo_cli": executable},
    )

    assert plan.launchable is False
    assert plan.argv == []
    assert plan.validation_reason.startswith("unsupported_grok_kilo_model:")


def test_grok_kilo_registry_and_provider_family_are_explicit():
    assert "grok_kilo_cli" in runtime_adapters.SUPPORTED_ADAPTERS
    assert "grok_kilo_cli" in runtime_adapters.LOCAL_ADAPTERS
    assert runtime_adapters.ADAPTER_EXECUTABLES["grok_kilo_cli"] == "kilo"
    assert runtime_adapters.provider_for_adapter("grok_kilo_cli") == "xai"


def test_grok_kilo_preserves_windows_appcontainer_gate(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "kilo")
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: True)

    blocked = runtime_adapters.build_runtime_command(
        "grok_kilo_cli",
        "Prompt",
        repo,
        executable_overrides={"grok_kilo_cli": executable},
    )
    allowed = runtime_adapters.build_runtime_command(
        "grok_kilo_cli",
        "Prompt",
        repo,
        executable_overrides={"grok_kilo_cli": executable},
        outer_sandbox_backend="appcontainer",
    )

    assert blocked.launchable is False
    assert blocked.validation_reason == (
        runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER
    )
    assert allowed.launchable is True


def test_grok_kilo_discovers_newest_bounded_vscode_extension(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "extensions"
    older_bin = root / "kilocode.kilo-code-7.9.0" / "bin"
    newer_bin = root / "kilocode.kilo-code-7.10.0" / "bin"
    older_bin.mkdir(parents=True)
    newer_bin.mkdir(parents=True)
    older = _executable(older_bin, "kilo")
    newer = _executable(newer_bin, "kilo")
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        runtime_adapters, "_default_kilo_extension_roots", lambda: (root,)
    )

    plan = runtime_adapters.build_runtime_command("grok_kilo_cli", "Prompt", repo)

    assert older.exists()
    assert plan.executable == str(newer)
    assert plan.argv[0] == str(newer)


def test_grok_kilo_path_discovery_precedes_extension_fallback(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path_bin = tmp_path / "path"
    path_bin.mkdir()
    path_kilo = _executable(path_bin, "kilo")

    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: str(path_kilo))
    monkeypatch.setattr(
        runtime_adapters,
        "_default_kilo_extension_roots",
        lambda: (_ for _ in ()).throw(AssertionError("fallback must not run")),
    )

    plan = runtime_adapters.build_runtime_command("grok_kilo_cli", "Prompt", repo)
    assert plan.executable == str(path_kilo)


def test_grok_kilo_extension_symlink_is_rejected(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "extensions"
    real_bin = tmp_path / "real"
    real_bin.mkdir()
    real = _executable(real_bin, "kilo")
    link = root / "kilocode.kilo-code-9.0.0" / "bin" / "kilo"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        runtime_adapters, "_default_kilo_extension_roots", lambda: (root,)
    )

    plan = runtime_adapters.build_runtime_command("grok_kilo_cli", "Prompt", repo)
    assert plan.launchable is False
    assert plan.validation_reason == "executable not found: kilo"


def test_worker_temp_env_vars_is_the_frozen_temp_key_declaration() -> None:
    """NF430: the single declaration of which env keys carry a worker's TMPDIR.

    ``process_launcher.worker_launch_env`` overlays exactly these keys with the
    request-owned ``.aiworkhub/temp/worker/<request_id>`` authority, so their
    identity is contract.  This module still declares only inert naming data --
    never an environment mapping -- and the three names cover POSIX ``TMPDIR``
    plus the ``TMP``/``TEMP`` names Windows and macOS honour.
    """
    assert runtime_adapters.WORKER_TEMP_ENV_VARS == ("TMPDIR", "TMP", "TEMP")
    assert isinstance(runtime_adapters.WORKER_TEMP_ENV_VARS, tuple)
    assert set(runtime_adapters.WORKER_TEMP_ENV_VARS) == {"TMPDIR", "TMP", "TEMP"}
