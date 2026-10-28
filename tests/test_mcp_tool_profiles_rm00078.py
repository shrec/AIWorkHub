"""RM-2026-00078 step 1: one declarative MCP tool profile table, a listed/callable split, the default surface unchanged."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import dashboard_mcp_app, mcp_tool_metadata, server, task_store  # noqa: E402
from aiworkhub.tool_recovery import unknown_tool_message  # noqa: E402

_PAYLOAD_PREFIX = "RM00078_PAYLOAD="
_METADATA_PY = _SRC / "aiworkhub" / "mcp_tool_metadata.py"
_WORKER_MCP_PY = _SRC / "aiworkhub" / "worker_ai_tools_mcp.py"
_EXTENSION_JS = _ROOT / "vscode-extension" / "extension.js"
_DASHBOARD = "aiworkhub_dashboard_"
_KEEP = {"aiworkhub_dashboard_needfix_transition", "aiworkhub_dashboard_snapshot"}
_WRITE_GATED = {
    "aiworkhub_task_auto_pickup",
    "aiworkhub_task_mark_review",
    "aiworkhub_task_mark_done",
    "aiworkhub_task_export_jsonl",
}
_CALLABLE_ONLY = "aiworkhub_task_template_list"
_HIDDEN_PROBE = "aiworkhub_dashboard_health"
_MANAGER_PROBE = "aiworkhub_task_list"
_NEVER_EXISTED = "aiworkhub_no_such_tool_rm00078"
_WORKER_ONLY = "aiworkhub_worker_source_graph_query"

# Measured on base 739a7c70: tools/list counts before the profile table existed.
_PRE_TABLE_ALL_COUNT = 232
_PRE_TABLE_ALL_READ_ONLY_COUNT = 228
_PRE_TABLE_MANAGER_COUNT = 185
# The 47 webview-only dashboard tools the pre-table manager surface hid.
_PRE_TABLE_MANAGER_HIDDEN = frozenset({
    "aiworkhub_dashboard_task_detail",
    "aiworkhub_dashboard_health",
    "aiworkhub_dashboard_task_live_output",
    "aiworkhub_dashboard_memory",
    "aiworkhub_dashboard_sessions",
    "aiworkhub_dashboard_kb",
    "aiworkhub_dashboard_skills",
    "aiworkhub_dashboard_needfix_list",
    "aiworkhub_dashboard_needfix_detail",
    "aiworkhub_dashboard_needfix_convert_preview",
    "aiworkhub_dashboard_needfix_capture",
    "aiworkhub_dashboard_needfix_update",
    "aiworkhub_dashboard_needfix_archive",
    "aiworkhub_dashboard_needfix_restore",
    "aiworkhub_dashboard_needfix_purge",
    "aiworkhub_dashboard_needfix_convert_commit",
    "aiworkhub_dashboard_needfix_link_existing_task",
    "aiworkhub_dashboard_needfix_reopen_superseded_task_link",
    "aiworkhub_dashboard_roadmap_list",
    "aiworkhub_dashboard_roadmap_detail",
    "aiworkhub_dashboard_hub_mail",
    "aiworkhub_dashboard_hub_mail_detail",
    "aiworkhub_dashboard_hub_mail_listener",
    "aiworkhub_dashboard_hub_mail_pair_begin",
    "aiworkhub_dashboard_hub_mail_pair_complete",
    "aiworkhub_dashboard_settings",
    "aiworkhub_dashboard_settings_update",
    "aiworkhub_dashboard_model_settings_update",
    "aiworkhub_dashboard_source_graph_settings_update",
    "aiworkhub_dashboard_storage_retention_preview",
    "aiworkhub_dashboard_storage_quarantine",
    "aiworkhub_dashboard_storage_registration_prune",
    "aiworkhub_dashboard_storage_restore",
    "aiworkhub_dashboard_storage_purge",
    "aiworkhub_dashboard_terminal_log_retention_preview",
    "aiworkhub_dashboard_terminal_log_usage_backfill",
    "aiworkhub_dashboard_terminal_log_quarantine",
    "aiworkhub_dashboard_terminal_log_restore",
    "aiworkhub_dashboard_terminal_log_purge",
    "aiworkhub_dashboard_task_retention_preview",
    "aiworkhub_dashboard_task_archive",
    "aiworkhub_dashboard_task_restore",
    "aiworkhub_dashboard_task_restart",
    "aiworkhub_dashboard_task_quarantine",
    "aiworkhub_dashboard_task_quarantine_restore",
    "aiworkhub_dashboard_task_quarantine_purge",
    "aiworkhub_dashboard_initialize",
})

_CHILD = r"""
try:
    import sitecustomize  # noqa: F401 - -S skips site; the AppContainer lane shim must still load
except ImportError:
    pass
import json
import os
from types import MappingProxyType
from aiworkhub import mcp_tool_metadata, server

ARGS = json.loads(os.environ["RM00078_ARGS"])
assert not server._MCP_SDK_AVAILABLE
real = server.mcp._tools


def rpc(method, params=None):
    try:
        return {"result": server._stdio_dispatch("AIWorkHub MCP", real, method, params, "", 1)}
    except server._StdioProtocolError as exc:
        return {"error": {"code": exc.code, "message": exc.message}}


def listed():
    return [entry["name"] for entry in rpc("tools/list")["result"]["tools"]]


report = {"surface": server.mcp_surface(), "registered": list(real), "listed": listed(), "probes": {}}
for name in ARGS.get("probes", []):
    report["probes"][name] = rpc("tools/call", {"name": name, "arguments": {}})
callable_only = ARGS.get("callable_only")
if callable_only:
    entry = {report["surface"]: mcp_tool_metadata.MCP_VISIBILITY_CALLABLE}
    mcp_tool_metadata.MCP_TOOL_PROFILES[callable_only] = MappingProxyType(entry)
    report["callable_only"] = {
        "listed": listed(),
        "call": rpc("tools/call", {"name": callable_only, "arguments": {}}),
    }
print(PREFIX + json.dumps(report))
"""

_RUNS = {
    "unset": {"surface": None, "writes": True},
    "unset_read_only": {"surface": None, "writes": False},
    "all": {"surface": "all", "writes": True},
    "manager": {
        "surface": "manager", "writes": True,
        "args": {"probes": [_HIDDEN_PROBE, _NEVER_EXISTED, _WORKER_ONLY], "callable_only": _CALLABLE_ONLY},
    },
    "unknown": {"surface": "Bogus-Profile", "writes": True},
    "worker": {"surface": "worker", "writes": True, "args": {"probes": [_MANAGER_PROBE]}},
    "dashboard": {"surface": "dashboard", "writes": True, "args": {"probes": [_MANAGER_PROBE]}},
}


def _run_child(repo_root: Path, surface: str | None, writes: bool, args: dict) -> dict:
    # Extend PYTHONPATH, never replace it, and drop the SDK entries -S could not hide.
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    inherited = [p for p in inherited if not (Path(p, "mcp").is_dir() or Path(p, "pydantic").is_dir())]
    env = dict(
        os.environ,
        AIWORKHUB_MCP_STDIO_BACKEND="stdlib",
        AIWORKHUB_REPO_ROOT=str(repo_root),
        AIWORKHUB_REPO=str(repo_root),
        RM00078_ARGS=json.dumps(args),
        PYTHONPATH=os.pathsep.join([str(_SRC), *inherited]),
    )
    for key in ("AIWORKHUB_MCP_SURFACE", "AIWORKHUB_ALLOW_WRITES"):
        env.pop(key, None)
    if surface is not None:
        env["AIWORKHUB_MCP_SURFACE"] = surface
    if writes:
        env["AIWORKHUB_ALLOW_WRITES"] = "1"
    script = f"PREFIX = {_PAYLOAD_PREFIX!r}\n{_CHILD}"
    completed = subprocess.run(
        [sys.executable, "-S", "-c", script], env=env, capture_output=True, text=True, timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    lines = completed.stdout.splitlines()
    assert len(lines) == 1 and lines[0].startswith(_PAYLOAD_PREFIX), completed.stdout[-2000:]
    report = json.loads(lines[0][len(_PAYLOAD_PREFIX):])
    report["stderr_surface_lines"] = [
        line for line in completed.stderr.splitlines() if line.startswith("aiworkhub.mcp_surface:")
    ]
    return report


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("rm00078_repo")
    assert task_store.initialize_repository(root)["ok"]
    with ThreadPoolExecutor(max_workers=max(1, min(len(_RUNS), os.cpu_count() or 1))) as pool:
        futures = {
            label: pool.submit(_run_child, root, spec["surface"], spec["writes"], spec.get("args", {}))
            for label, spec in _RUNS.items()
        }
        return {label: future.result() for label, future in futures.items()}


def _profile_names(profile: str) -> set:
    return {name for name, entry in mcp_tool_metadata.MCP_TOOL_PROFILES.items() if profile in entry}


def _pre_table_manager_rule(name: str) -> bool:
    return not (name.startswith(_DASHBOARD) and name not in _KEEP)


def _error(reply: dict) -> dict:
    assert "result" not in reply, reply
    return reply["error"]


def _literal_assignment(path: Path, target: str) -> ast.expr:
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == target:
            return node.value
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == target for t in node.targets):
            return node.value
    raise AssertionError(f"{target} is not assigned at module level in {path}")


class _Collector:
    def __init__(self) -> None:
        self.names: list[str] = []

    def tool(self, name: str):
        def decorate(function):
            self.names.append(name)
            return function

        return decorate


def _dashboard_app_tool_names() -> set:
    collector = _Collector()
    returned = dashboard_mcp_app.register(collector)
    assert sorted(returned) == sorted(collector.names)
    return set(collector.names)


_TOOL_NAME = r"(?:aiworkhub|needfix|roadmap)_[a-z0-9_]+"


def _extension_dashboard_tool_names() -> set:
    """Tool names the extension's dashboard code calls: its *_TOOLS maps, *_TOOL constants, literal callTool names.

    The VSCODE_LM_* constants and the VSCODE_LM_PRIVATE_TOOLS array describe the language-model
    bridge's own tools, not dashboard calls.
    """
    assert _EXTENSION_JS.is_file(), f"missing {_EXTENSION_JS}: the dashboard profile cannot be checked"
    text = _EXTENSION_JS.read_text(encoding="utf-8")
    names: set = set()
    for block in re.finditer(r"const [A-Z0-9_]+_TOOLS = Object\.freeze\(\{(.*?)\}\);", text, re.S):
        names |= set(re.findall(rf'"({_TOOL_NAME})"', block.group(1)))
    for const in re.finditer(rf'const ([A-Z0-9_]+)_TOOL = "({_TOOL_NAME})";', text):
        if not const.group(1).startswith("VSCODE_LM_"):
            names.add(const.group(2))
    names |= set(re.findall(rf'(?:callTool|_callToolRaw)\(\s*"({_TOOL_NAME})"', text))
    return names


def test_profile_table_is_one_literal_and_the_module_scans_nothing() -> None:
    table = _literal_assignment(_METADATA_PY, "MCP_TOOL_PROFILES")
    assert isinstance(table, ast.Dict)
    keys = [key.value for key in table.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)]
    assert len(keys) == len(table.keys) == len(set(keys)) == len(mcp_tool_metadata.MCP_TOOL_PROFILES)
    assert {getattr(value, "id", None) for value in table.values} <= {
        "_MANAGER", "_DASHBOARD", "_MANAGER_DASHBOARD", "_WORKER",
    }
    module_imports = set()
    for node in ast.parse(_METADATA_PY.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import):
            module_imports |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            module_imports.add(node.module)
    assert module_imports == {"__future__", "inspect", "typing", "collections.abc", "types"}
    for name, entry in mcp_tool_metadata.MCP_TOOL_PROFILES.items():
        assert entry and set(entry) <= set(mcp_tool_metadata.MCP_PROFILES), name
        # Step 1 lists every entry; ``callable`` is infrastructure for a later step.
        assert set(entry.values()) == {mcp_tool_metadata.MCP_VISIBILITY_LISTED}, name
    assert mcp_tool_metadata.MCP_SURFACES == ("all", "manager", "worker", "dashboard")
    assert server.MCP_SURFACES is mcp_tool_metadata.MCP_SURFACES
    assert server.MCP_DASHBOARD_TOOL_PREFIX == mcp_tool_metadata.MCP_DASHBOARD_TOOL_PREFIX == _DASHBOARD


def test_unset_and_all_list_exactly_the_pre_table_tools(runs: dict) -> None:
    unset, explicit, read_only = runs["unset"], runs["all"], runs["unset_read_only"]
    assert unset["surface"] == explicit["surface"] == read_only["surface"] == "all"
    assert unset["listed"] == unset["registered"] == explicit["listed"] == explicit["registered"]
    assert len(unset["listed"]) == len(set(unset["listed"])) == _PRE_TABLE_ALL_COUNT
    assert read_only["listed"] == read_only["registered"]
    assert len(read_only["listed"]) == _PRE_TABLE_ALL_READ_ONLY_COUNT
    assert read_only["listed"] == [name for name in unset["listed"] if name not in _WRITE_GATED]
    # No real tool relies on the table's fallback rule for unknown names.
    assert set(unset["registered"]) <= set(mcp_tool_metadata.MCP_TOOL_PROFILES)


def test_manager_lists_exactly_the_pre_table_manager_tools(runs: dict) -> None:
    full, manager = runs["unset"]["listed"], runs["manager"]
    assert manager["surface"] == "manager"
    assert len(manager["listed"]) == _PRE_TABLE_MANAGER_COUNT
    assert manager["listed"] == manager["registered"]
    assert manager["listed"] == [name for name in full if _pre_table_manager_rule(name)]
    assert manager["listed"] == [name for name in full if name not in _PRE_TABLE_MANAGER_HIDDEN]
    assert {name for name in full if not _pre_table_manager_rule(name)} == _PRE_TABLE_MANAGER_HIDDEN
    assert _profile_names("manager") == set(full) - _PRE_TABLE_MANAGER_HIDDEN
    assert {name for name in _profile_names("manager") if name.startswith(_DASHBOARD)} == set(
        server.MANAGER_SURFACE_DASHBOARD_KEEP
    ) == _KEEP


def test_unknown_surface_serves_manager_and_reports_once_on_stderr(runs: dict) -> None:
    unknown = runs["unknown"]
    assert unknown["surface"] == "manager"
    assert unknown["listed"] == runs["manager"]["listed"]
    [line] = unknown["stderr_surface_lines"]
    assert "AIWORKHUB_MCP_SURFACE='bogus-profile'" in line and "'manager'" in line
    for label in ("unset", "all", "manager", "worker", "dashboard"):
        assert runs[label]["stderr_surface_lines"] == [], label


def test_unknown_surface_value_writes_exactly_one_stderr_line_in_process(monkeypatch, capsys) -> None:
    monkeypatch.setattr(server, "_unknown_surfaces_reported", set())
    monkeypatch.setenv(server.MCP_SURFACE_ENV, " Bogus-Profile ")
    assert [server.mcp_surface() for _ in range(3)] == ["manager"] * 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "aiworkhub.mcp_surface: unknown AIWORKHUB_MCP_SURFACE='bogus-profile'; using 'manager' "
        "(accepted: all, dashboard, manager, worker)"
    ]
    for value, expected in ((None, "all"), ("", "all"), ("ALL", "all"), ("worker", "worker"), ("dashboard", "dashboard")):
        if value is None:
            monkeypatch.delenv(server.MCP_SURFACE_ENV, raising=False)
        else:
            monkeypatch.setenv(server.MCP_SURFACE_ENV, value)
        assert server.mcp_surface() == expected
    assert capsys.readouterr() == ("", "")


def test_worker_profile_is_exactly_the_worker_tools(runs: dict) -> None:
    worker_names = set(ast.literal_eval(_literal_assignment(_WORKER_MCP_PY, "MCP_TOOL_NAMES")))
    assert worker_names and all(name.startswith("aiworkhub_worker_") for name in worker_names)
    assert _profile_names("worker") == worker_names
    assert {name for name in mcp_tool_metadata.MCP_TOOL_PROFILES if name.startswith("aiworkhub_worker_")} == worker_names
    assert all(set(mcp_tool_metadata.MCP_TOOL_PROFILES[name]) == {"worker"} for name in worker_names)
    worker = runs["worker"]
    assert worker["surface"] == "worker"
    full = runs["unset"]["listed"]
    assert worker["listed"] == worker["registered"] == [name for name in full if name.startswith("aiworkhub_worker_")]


def test_dashboard_profile_is_the_dashboard_app_plus_every_extension_called_tool(runs: dict) -> None:
    app_names = _dashboard_app_tool_names()
    extension_names = _extension_dashboard_tool_names()
    assert len(app_names) == 49 and all(name.startswith(_DASHBOARD) for name in app_names)
    assert extension_names
    dashboard = _profile_names("dashboard")
    missing = sorted(extension_names - dashboard)
    assert missing == [], f"extension-called tools absent from the dashboard profile: {missing}"
    assert app_names <= dashboard
    assert dashboard == app_names | extension_names
    run = runs["dashboard"]
    assert run["surface"] == "dashboard"
    full = runs["unset"]["listed"]
    assert run["listed"] == run["registered"] == [name for name in full if name in dashboard]


def test_callable_entry_is_callable_by_exact_name_but_absent_from_tools_list(monkeypatch, runs: dict) -> None:
    monkeypatch.setitem(
        mcp_tool_metadata.MCP_TOOL_PROFILES, _CALLABLE_ONLY, MappingProxyType({"manager": "callable"})
    )
    assert server.mcp_tool_callable(_CALLABLE_ONLY, "manager") is True
    assert server.mcp_tool_listed(_CALLABLE_ONLY, "manager") is False
    assert server.mcp_tool_listed(_CALLABLE_ONLY, "all") is True
    assert server.mcp_tool_callable(_CALLABLE_ONLY, "dashboard") is False
    manager = runs["manager"]
    assert _CALLABLE_ONLY in manager["listed"]
    patched = manager["callable_only"]
    assert patched["listed"] == [name for name in manager["listed"] if name != _CALLABLE_ONLY]
    result = patched["call"]["result"]
    assert list(result) == ["content"]
    assert json.loads(result["content"][0]["text"])["schema_id"] == "aiworkhub.task_template_list.v1"


@pytest.mark.skipif(not server._MCP_SDK_AVAILABLE, reason="mcp SDK is not importable in this process")
def test_sdk_tools_list_paths_publish_only_listed_tools(monkeypatch) -> None:
    from mcp import types

    def via_handler() -> list:
        reply = asyncio.run(server._lowlevel_server.request_handlers[types.ListToolsRequest](None))
        return [tool.name for tool in reply.root.tools]

    monkeypatch.delenv(server.MCP_SURFACE_ENV, raising=False)
    everything = [tool.name for tool in asyncio.run(server.mcp.list_tools())]
    assert everything == via_handler() == [tool.name for tool in server.mcp._tool_manager.list_tools()]
    monkeypatch.setitem(
        mcp_tool_metadata.MCP_TOOL_PROFILES, _CALLABLE_ONLY, MappingProxyType({"manager": "callable"})
    )
    monkeypatch.setenv(server.MCP_SURFACE_ENV, "manager")
    expected = [name for name in everything if server.mcp_tool_listed(name, "manager")]
    assert _CALLABLE_ONLY not in expected and _HIDDEN_PROBE not in expected
    assert [tool.name for tool in asyncio.run(server.mcp.list_tools())] == expected
    assert via_handler() == expected
    assert server.mcp._tool_manager.get_tool(_CALLABLE_ONLY) is not None


def test_off_profile_calls_name_the_active_and_exposing_profiles(runs: dict) -> None:
    manager = runs["manager"]
    hidden = _error(manager["probes"][_HIDDEN_PROBE])
    assert hidden == {
        "code": -32602,
        "message": unknown_tool_message(
            _HIDDEN_PROBE, manager["listed"], active_profile="manager", exposing_profiles=("dashboard", "all"),
        ),
    }
    payload = json.loads(hidden["message"])
    assert payload["active_profile"] == "manager" and payload["exposed_in"] == ["dashboard", "all"]
    assert _error(manager["probes"][_NEVER_EXISTED])["message"] == unknown_tool_message(
        _NEVER_EXISTED, manager["listed"]
    )
    for label in ("worker", "dashboard"):
        run = runs[label]
        refusal = json.loads(_error(run["probes"][_MANAGER_PROBE])["message"])
        assert refusal["active_profile"] == label and refusal["exposed_in"] == ["manager", "all"], label
        assert refusal["reason"] == "not_in_active_profile" and refusal["requested"] == _MANAGER_PROBE


def test_unknown_tool_message_helper_distinguishes_known_and_unknown_names() -> None:
    tools = {"aiworkhub_task_list": object(), "aiworkhub_dashboard_health": object(), "synthetic_plain": object()}
    listed = ["aiworkhub_task_list", "synthetic_plain"]
    known = json.loads(server.mcp_unknown_tool_message("aiworkhub_dashboard_health", tools, "manager"))
    assert known["exposed_in"] == ["dashboard", "all"]
    # Worker-only names live on the worker MCP server: no profile here serves them.
    assert server.mcp_unknown_tool_message("aiworkhub_worker_exit_preflight", tools, "manager") == (
        unknown_tool_message("aiworkhub_worker_exit_preflight", listed)
    )
    for name in ("aiworkhub_no_such_tool", None, 5):
        assert server.mcp_unknown_tool_message(name, tools, "manager") == unknown_tool_message(name, listed)
    assert server.mcp_unknown_tool_message("aiworkhub_task_list", tools, "manager") == unknown_tool_message(
        "aiworkhub_task_list", listed
    )


def test_write_gate_skipped_tool_gets_the_generic_refusal(monkeypatch) -> None:
    # Skipped while writes are off, the tool is served by no profile: a surface switch cannot help.
    name = "aiworkhub_task_mark_done"
    assert name in mcp_tool_metadata.MCP_TOOL_PROFILES
    tools = {"aiworkhub_dashboard_health": object(), "aiworkhub_task_list": object()}
    monkeypatch.setattr(server, "_WRITE_GATE_SKIPPED", {name})
    for surface in ("dashboard", "worker", "manager"):
        listed = [candidate for candidate in tools if server.mcp_tool_listed(candidate, surface)]
        assert server.mcp_unknown_tool_message(name, tools, surface) == unknown_tool_message(name, listed)
    monkeypatch.setattr(server, "_WRITE_GATE_SKIPPED", set())
    assert json.loads(server.mcp_unknown_tool_message(name, tools, "dashboard"))["exposed_in"] == ["manager", "all"]


def test_manager_call_to_a_worker_only_tool_gets_the_generic_refusal(runs: dict) -> None:
    manager = runs["manager"]
    assert _WORKER_ONLY in mcp_tool_metadata.MCP_TOOL_PROFILES and _WORKER_ONLY not in manager["registered"]
    assert _error(manager["probes"][_WORKER_ONLY]) == {
        "code": -32602,
        "message": unknown_tool_message(_WORKER_ONLY, manager["listed"]),
    }


def test_alternating_unknown_surface_values_report_each_value_once(monkeypatch, capsys) -> None:
    monkeypatch.setattr(server, "_unknown_surfaces_reported", set())
    for _ in range(3):
        for value in ("bogus-one", "bogus-two"):
            monkeypatch.setenv(server.MCP_SURFACE_ENV, value)
            assert server.mcp_surface() == "manager"
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 2
    assert "AIWORKHUB_MCP_SURFACE='bogus-one'" in lines[0] and "AIWORKHUB_MCP_SURFACE='bogus-two'" in lines[1]


def test_manager_dashboard_keep_set_matches_the_profile_table() -> None:
    assert {
        name
        for name, entry in mcp_tool_metadata.MCP_TOOL_PROFILES.items()
        if name.startswith(mcp_tool_metadata.MCP_DASHBOARD_TOOL_PREFIX) and "manager" in entry
    } == server.MANAGER_SURFACE_DASHBOARD_KEEP
