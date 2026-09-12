"""OpenCode 1.18.27 runtime adapter foundation: resolution, identity, deny."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import runtime_adapters  # noqa: E402


@pytest.fixture(autouse=True)
def _portable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
    monkeypatch.setattr(runtime_adapters, "_is_linux_host", lambda: True)


def _executable(_tmp_path: Path, _name: str) -> Path:
    return Path(sys.executable).resolve()


def test_existing_adapter_identities_are_unchanged() -> None:
    assert runtime_adapters.SUPPORTED_ADAPTERS[:8] == (
        "vscode_lm",
        "claude_cli",
        "codex_cli",
        "deepseek_copilot_cli",
        "deepseek_vscode_lm",
        "glm_copilot_cli",
        "glm_vscode_lm",
        "grok_kilo_cli",
    )
    assert runtime_adapters.SUPPORTED_ADAPTERS[-1] == "deepseek_manual"
    assert runtime_adapters.OPENCODE_CLI_ADAPTER == "opencode_cli"
    assert runtime_adapters.ADAPTER_EXECUTABLES["claude_cli"] == "claude"
    assert runtime_adapters.ADAPTER_EXECUTABLES["grok_kilo_cli"] == "kilo"
    assert runtime_adapters.ADAPTER_EXECUTABLES["opencode_cli"] == "opencode"


def test_opencode_argv_is_format_json_and_exact_provider_model(tmp_path: Path) -> None:
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    executable = _executable(tmp_path, "opencode")
    prompt = "Inspect one bounded target; keep $TOKEN literal თბილისი"
    model = "anthropic/claude-sonnet-4"

    plan = runtime_adapters.build_runtime_command(
        runtime_adapters.OPENCODE_CLI_ADAPTER,
        prompt,
        repo,
        model=model,
        executable_overrides={runtime_adapters.OPENCODE_CLI_ADAPTER: executable},
    )

    assert plan.launchable is True
    assert plan.argv == [
        str(executable),
        "run",
        "--format",
        "json",
        "--model",
        model,
        prompt,
    ]
    assert plan.argv.count(prompt) == 1
    assert not any("AUTH" in token or "KEY" in token for token in plan.argv)


def test_opencode_preserves_unknown_provider_model_identity(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "opencode")
    model = "acme/mod-1.18.27"

    plan = runtime_adapters.build_runtime_command(
        runtime_adapters.OPENCODE_CLI_ADAPTER,
        "Prompt",
        repo,
        model=model,
        executable_overrides={runtime_adapters.OPENCODE_CLI_ADAPTER: executable},
    )

    assert plan.launchable is True
    assert plan.argv[plan.argv.index("--model") + 1] == model


@pytest.mark.parametrize(
    "model",
    [None, "", "claude-sonnet-4", "openai/", "/gpt-4", "openai /gpt-4", "xai"],
)
def test_opencode_model_without_provider_identity_fails_closed(
    tmp_path: Path, model: str | None
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "opencode")
    plan = runtime_adapters.build_runtime_command(
        runtime_adapters.OPENCODE_CLI_ADAPTER,
        "Prompt",
        repo,
        model=model,
        executable_overrides={runtime_adapters.OPENCODE_CLI_ADAPTER: executable},
    )
    assert plan.launchable is False
    assert plan.argv == []
    assert plan.validation_reason.startswith("unsupported_opencode_model:") or (
        plan.validation_reason == "model must be a nonempty string when provided"
    )


def test_opencode_windows_resolution_fails_closed_without_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: True)
    called = []

    def _which(binary: str) -> str | None:
        called.append(binary)
        return str(tmp_path / "opencode.exe")

    monkeypatch.setattr(runtime_adapters.shutil, "which", _which)
    resolution = runtime_adapters.resolve_executable(
        runtime_adapters.OPENCODE_CLI_ADAPTER,
        executable_overrides={
            runtime_adapters.OPENCODE_CLI_ADAPTER: _executable(tmp_path, "opencode")
        },
    )
    assert resolution.ok is False
    assert resolution.executable is None
    assert resolution.reason == runtime_adapters.OPENCODE_WINDOWS_RESOLUTION_FAIL_CLOSED
    assert called == []


def test_opencode_linux_snap_bin_is_used_when_path_lookup_misses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snap = _executable(tmp_path, "opencode")
    monkeypatch.setattr(runtime_adapters, "OPENCODE_SNAP_BIN", str(snap))
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _binary: None)
    resolution = runtime_adapters.resolve_executable(runtime_adapters.OPENCODE_CLI_ADAPTER)
    assert resolution.ok is True
    assert resolution.executable == str(snap)


def test_opencode_linux_snap_bin_preserves_wrapper_when_symlink_target_is_snap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snap_target = tmp_path / "usr" / "bin" / "snap"
    snap_target.parent.mkdir(parents=True)
    snap_target.symlink_to(Path(sys.executable).resolve())
    wrapper = tmp_path / "snap" / "bin" / "opencode"
    wrapper.parent.mkdir(parents=True)
    wrapper.symlink_to(snap_target)
    monkeypatch.setattr(runtime_adapters, "OPENCODE_SNAP_BIN", str(wrapper))
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _binary: None)
    resolution = runtime_adapters.resolve_executable(runtime_adapters.OPENCODE_CLI_ADAPTER)
    assert resolution.ok is True
    assert resolution.executable == str(wrapper)
    assert resolution.executable != str(wrapper.resolve(strict=True))


def test_opencode_path_discovery_preserves_snap_wrapper_when_symlink_target_is_snap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snap_target = tmp_path / "usr" / "bin" / "snap"
    snap_target.parent.mkdir(parents=True)
    snap_target.symlink_to(Path(sys.executable).resolve())
    wrapper = tmp_path / "snap" / "bin" / "opencode"
    wrapper.parent.mkdir(parents=True)
    wrapper.symlink_to(snap_target)
    monkeypatch.setattr(runtime_adapters, "OPENCODE_SNAP_BIN", str(tmp_path / "unused-snap"))
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _binary: str(wrapper))
    resolution = runtime_adapters.resolve_executable(runtime_adapters.OPENCODE_CLI_ADAPTER)
    assert resolution.ok is True
    assert resolution.executable == str(wrapper)
    assert resolution.executable != str(wrapper.resolve(strict=True))


def test_opencode_permission_default_deny_blocks_builtins_and_unknown() -> None:
    denied = (
        "read",
        "edit",
        "bash",
        "task",
        "skill",
        "lsp",
        "websearch",
        "future_unknown_tool",
    )
    for tool in denied:
        assert runtime_adapters.opencode_permission_action(tool) == (
            runtime_adapters.OPENCODE_PERMISSION_DENY
        )
        assert runtime_adapters.opencode_tool_is_allowed(tool) is False


def test_opencode_permission_allows_only_worker_mcp_namespace() -> None:
    permission = runtime_adapters.opencode_worker_permission_contract()
    allow_names = {
        name
        for name, action in permission.items()
        if action == runtime_adapters.OPENCODE_PERMISSION_ALLOW
    }
    expected = {
        runtime_adapters.opencode_mcp_tool_name(mcp_tool)
        for mcp_tool in runtime_adapters.OPENCODE_WORKER_MCP_TOOLS
    }
    assert allow_names == expected
    assert all("*" not in name and "?" not in name for name in allow_names)
    for name in expected:
        assert runtime_adapters.opencode_tool_is_allowed(name) is True
        assert runtime_adapters.opencode_permission_action(name) == (
            runtime_adapters.OPENCODE_PERMISSION_ALLOW
        )
    denied = (
        "aiworkhub_manager_bootstrap",
        "aiworkhub_worker_ai_tools_quality_review_submit",
        "aiworkhub_worker_ai_tools_aiworkhub_worker_quality_review_submit",
        "aiworkhub_worker_ai_tools_aiworkhub_worker_quality_review_packet_read",
        "aiworkhub_worker_ai_tools_aiworkhub_worker_undeclared",
    )
    for name in denied:
        assert runtime_adapters.opencode_tool_is_allowed(name) is False
        assert runtime_adapters.opencode_permission_action(name) == (
            runtime_adapters.OPENCODE_PERMISSION_DENY
        )


def test_opencode_worker_mcp_config_is_request_local_and_secret_free() -> None:
    command = ("/usr/bin/python3", "-m", "aiworkhub.worker_ai_tools_mcp")
    config = runtime_adapters.build_opencode_worker_mcp_config(command)
    permission = config["permission"]
    assert permission["*"] == runtime_adapters.OPENCODE_PERMISSION_DENY
    for mcp_tool in runtime_adapters.OPENCODE_WORKER_MCP_TOOLS:
        name = runtime_adapters.opencode_mcp_tool_name(mcp_tool)
        assert permission[name] == runtime_adapters.OPENCODE_PERMISSION_ALLOW
    server = config["mcp"][runtime_adapters.OPENCODE_WORKER_MCP_SERVER]
    assert server["type"] == "local"
    assert server["command"] == list(command)
    assert server["enabled"] is True
    assert "environment" not in server
    assert "headers" not in server
    assert "oauth" not in server
    dumped = repr(config)
    assert "SECRET" not in dumped
    assert "API_KEY" not in dumped
    assert "TOKEN" not in dumped


def test_opencode_worker_mcp_config_rejects_shell_string_command() -> None:
    with pytest.raises(ValueError, match="opencode_mcp_command_must_be_argv"):
        runtime_adapters.build_opencode_worker_mcp_config("python -m aiworkhub")
