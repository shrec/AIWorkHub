"""NF-2026-01721: per-adapter capability table for the indexed-file Read/Write ban."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import runtime_adapters  # noqa: E402

_ENFORCED = {
    "claude_cli",
    "opencode_cli",
    "vscode_lm",
    "glm_vscode_lm",
    "deepseek_vscode_lm",
}
_COPILOT = ("deepseek_copilot_cli", "glm_copilot_cli")
_NATIVE_FILE_TOOL_NAMES = frozenset(
    {"read", "view", "write", "edit", "create", "patch", "multiedit", "str_replace_editor"}
)


@pytest.fixture(autouse=True)
def _portable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)


@pytest.mark.parametrize("adapter_id", runtime_adapters.SUPPORTED_ADAPTERS)
def test_every_supported_adapter_has_an_explicit_row(adapter_id: str) -> None:
    assert adapter_id in runtime_adapters._INDEXED_FILE_RW_ENFORCEMENT_REASONS
    fact = runtime_adapters.indexed_file_rw_enforcement_fact(adapter_id)
    assert fact["adapter_id"] == adapter_id
    assert fact["enforced"] is (adapter_id in _ENFORCED)
    assert (adapter_id in runtime_adapters.INDEXED_FILE_RW_ENFORCING_ADAPTERS) is fact["enforced"]
    if fact["enforced"]:
        assert fact["mechanism"] and not fact["gap"]
    else:
        assert fact["gap"].startswith("gap:") and not fact["mechanism"]


def test_unknown_adapter_is_not_enforced() -> None:
    fact = runtime_adapters.indexed_file_rw_enforcement_fact("some_future_cli")
    assert fact["enforced"] is False
    assert fact["gap"] == runtime_adapters.INDEXED_FILE_RW_ENFORCEMENT_UNVERIFIED
    assert fact["mechanism"] == ""


def test_table_has_no_rows_for_unsupported_adapters() -> None:
    assert set(runtime_adapters._INDEXED_FILE_RW_ENFORCEMENT_REASONS) <= set(
        runtime_adapters.SUPPORTED_ADAPTERS
    )


@pytest.mark.parametrize("adapter_id", runtime_adapters.SUPPORTED_ADAPTERS)
def test_launch_record_fact_carries_the_indexed_file_row(adapter_id: str) -> None:
    fact = runtime_adapters.raw_discovery_enforcement_fact(adapter_id)
    assert fact["indexed_file_rw"] == runtime_adapters.indexed_file_rw_enforcement_fact(adapter_id)


def test_opencode_contract_denies_every_native_read_and_write_tool() -> None:
    contract = runtime_adapters.opencode_worker_permission_contract()
    assert contract["*"] == runtime_adapters.OPENCODE_PERMISSION_DENY
    for tool in ("read", "edit", "write", "patch", "multiedit"):
        assert tool in runtime_adapters.OPENCODE_DENIED_BUILTIN_TOOLS
        assert contract[tool] == runtime_adapters.OPENCODE_PERMISSION_DENY
        assert runtime_adapters.opencode_tool_is_allowed(tool) is False
    allowed = {
        name
        for name, action in contract.items()
        if action == runtime_adapters.OPENCODE_PERMISSION_ALLOW
    }
    assert allowed
    assert all(name.startswith(f"{runtime_adapters.OPENCODE_WORKER_MCP_SERVER}_") for name in allowed)


def test_opencode_generated_config_denies_native_read_and_write() -> None:
    config = runtime_adapters.build_opencode_worker_mcp_config(
        ("/usr/bin/python3", "-m", "aiworkhub.worker_ai_tools_mcp")
    )
    permission = config["permission"]
    assert permission["*"] == runtime_adapters.OPENCODE_PERMISSION_DENY
    for tool in ("read", "edit", "write", "patch", "multiedit"):
        assert permission[tool] == runtime_adapters.OPENCODE_PERMISSION_DENY
    assert runtime_adapters.indexed_file_rw_enforcement_fact("opencode_cli")["enforced"] is True


@pytest.mark.parametrize("adapter_id", _COPILOT)
def test_copilot_argv_matches_the_recorded_decision(adapter_id: str, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = runtime_adapters.build_runtime_command(
        adapter_id,
        "Prompt",
        repo,
        executable_overrides={adapter_id: Path(sys.executable)},
    )
    assert plan.launchable is True
    prefix = "--excluded-tools="
    excluded = {
        name.strip().lower()
        for token in plan.argv
        if token.startswith(prefix)
        for name in token[len(prefix) :].split(",")
    }
    assert excluded == set(runtime_adapters.COPILOT_RAW_DISCOVERY_EXCLUDED_TOOLS)
    excludes_native_files = bool(excluded & _NATIVE_FILE_TOOL_NAMES)
    fact = runtime_adapters.indexed_file_rw_enforcement_fact(adapter_id)
    assert fact["enforced"] is excludes_native_files
    if not excludes_native_files:
        assert fact["gap"].startswith("gap:copilot")
        assert "no new-file tool" in fact["gap"]
