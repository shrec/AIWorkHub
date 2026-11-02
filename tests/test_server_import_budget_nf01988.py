"""NF-2026-01988: ``import aiworkhub.server`` stays cheap and keeps its tool surface.

Measured before the fix: ``python -X importtime -c "import aiworkhub.server"``
took 8.2-13.0 s idle and executed 197 aiworkhub modules; under xdist load the
MCP initialize took 51.6 s against the client's 30 s connect timeout. The
server now defers every implementation module it does not need to register its
tools. These tests run the import in fresh interpreters so module creep fails CI.
"""

from __future__ import annotations

import ast
import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
SERVER_PATH = SRC_ROOT / "aiworkhub" / "server.py"

# Executed aiworkhub modules (the package included) after a bare
# ``import aiworkhub.server``. Measured 2026-11-03: 44 (before NF-2026-01988: 197).
EAGER_AIWORKHUB_MODULE_CEILING = 50
SURFACES = ("manager", "worker", "dashboard", "all")
CHILD_TIMEOUT_SECONDS = 600

_COUNT_CHILD = r"""
import importlib.util, json, sys
lazy_type = importlib.util._LazyModule

def executed():
    return sorted(
        name for name, module in list(sys.modules.items())
        if (name == "aiworkhub" or name.startswith("aiworkhub."))
        and module is not None and not isinstance(module, lazy_type)
    )

import aiworkhub.server as server
after_import = executed()
launcher = sys.modules["aiworkhub.process_launcher"]
launcher_deferred = isinstance(launcher, lazy_type)
shared_lock = (
    object.__getattribute__(launcher, "__spec__").loader_state.get("lock")
    is server._DEFERRED_EXECUTION_LOCK
) if launcher_deferred else None
attribute_is_canonical = server.process_launcher is launcher
mirrored_default = server.process_launcher.MAX_LOG_TAIL_BYTES
from aiworkhub import worker_ai_tools_mcp
print(json.dumps({
    "executed": after_import,
    "launcher_deferred": launcher_deferred,
    "shared_lock": shared_lock,
    "attribute_is_canonical": attribute_is_canonical,
    "launcher_executed_on_use": not isinstance(launcher, lazy_type),
    "mirrored_default_matches": mirrored_default == server._COLLECT_RESULT_MAX_LOG_TAIL_BYTES,
    "task_store_is_canonical": server.task_store is sys.modules["aiworkhub.task_store"],
    "reexports_resolve": (
        server.validate_provider_call_id is worker_ai_tools_mcp.validate_provider_call_id
        and server.validate_provenance is worker_ai_tools_mcp.validate_provenance
    ),
    "finder_removed": server._deferred_import_finder not in sys.meta_path,
}))
"""

# argv: mode ("lazy" or "eager"), path of server.py. In eager mode every module
# server.py imports at module level is executed first with the normal import
# machinery, which reproduces the pre-NF-2026-01988 import exactly.
_SURFACE_CHILD = r"""
import ast, asyncio, importlib, json, sys
mode, server_path = sys.argv[1], sys.argv[2]

def module_level_imports(body):
    for node in body:
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                yield "aiworkhub." + node.module
            else:
                for alias in node.names:
                    yield "aiworkhub." + alias.name
        for field in ("body", "orelse", "finalbody"):
            if isinstance(node, (ast.If, ast.Try)):
                yield from module_level_imports(getattr(node, field, []))
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                yield from module_level_imports(handler.body)

if mode == "eager":
    with open(server_path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for name in sorted(set(module_level_imports(tree.body))):
        try:
            importlib.import_module(name)
        except ModuleNotFoundError:
            pass  # a name bound from the package itself, e.g. __version__

import aiworkhub.server as server
surface = server.mcp_surface()
if server._MCP_SDK_AVAILABLE:
    tools = [
        tool.model_dump(mode="json", exclude_none=True)
        for tool in asyncio.run(server.mcp.list_tools())
    ]
else:
    tools = server._stdio_tools_list(server.mcp._tools)["tools"]
tools.sort(key=lambda tool: tool["name"])
print(json.dumps({
    "surface": surface,
    "tools": tools,
    "registry_listed": all(server.mcp_tool_listed(tool["name"], surface) for tool in tools),
}, sort_keys=True))
"""


def _child_env(surface: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(SRC_ROOT), env.get("PYTHONPATH", "")) if part
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONSTARTUP", None)
    if surface is not None:
        env["AIWORKHUB_MCP_SURFACE"] = surface
    return env


def _run_child(code: str, *args: str, surface: str | None = None) -> dict:
    # stdin=DEVNULL: a child inheriting a pipe stdin can hang at startup on Windows.
    completed = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=str(REPO_ROOT),
        env=_child_env(surface),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, f"child printed nothing; stderr: {completed.stderr[-4000:]}"
    return json.loads(lines[-1])


def test_bare_import_stays_within_the_module_budget() -> None:
    report = _run_child(_COUNT_CHILD)

    executed = report["executed"]
    assert "aiworkhub.server" in executed
    assert len(executed) <= EAGER_AIWORKHUB_MODULE_CEILING, (
        f"import aiworkhub.server executed {len(executed)} aiworkhub modules "
        f"(ceiling {EAGER_AIWORKHUB_MODULE_CEILING}): {executed}"
    )
    assert report["launcher_deferred"] is True
    assert report["shared_lock"] is True
    assert report["attribute_is_canonical"] is True
    assert report["launcher_executed_on_use"] is True
    assert report["mirrored_default_matches"] is True
    assert report["task_store_is_canonical"] is True
    assert report["reexports_resolve"] is True
    assert report["finder_removed"] is True


def test_published_tools_match_the_eager_import_on_every_surface() -> None:
    jobs = [(mode, surface) for surface in SURFACES for mode in ("lazy", "eager")]
    workers = max(1, min(len(jobs), os.cpu_count() or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            job: pool.submit(_run_child, _SURFACE_CHILD, job[0], str(SERVER_PATH), surface=job[1])
            for job in jobs
        }
        results = {job: future.result() for job, future in futures.items()}

    for surface in SURFACES:
        lazy = results[("lazy", surface)]
        eager = results[("eager", surface)]
        assert lazy["surface"] == eager["surface"] == surface
        # Worker tools are served by the worker MCP server, so this server's
        # worker surface may publish nothing; equality below still pins it.
        if surface != "worker":
            assert lazy["tools"], f"surface {surface} published no tools"
        assert eager["registry_listed"] is True
        assert lazy["registry_listed"] is True
        assert [tool["name"] for tool in lazy["tools"]] == [
            tool["name"] for tool in eager["tools"]
        ], surface
        assert lazy["tools"] == eager["tools"], surface


def test_server_module_attributes_are_the_canonical_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiworkhub import server

    canonical = sys.modules["aiworkhub.task_store"]
    assert server.task_store is canonical
    monkeypatch.setattr(server.task_store, "_nf01988_probe", "patched", raising=False)
    assert canonical._nf01988_probe == "patched"
    with pytest.raises(AttributeError):
        getattr(server, "_nf01988_no_such_attribute")


def test_mirrored_tool_defaults_equal_their_owners() -> None:
    import inspect

    from aiworkhub import process_launcher, server

    assert server._COLLECT_RESULT_MAX_LOG_TAIL_BYTES == process_launcher.MAX_LOG_TAIL_BYTES
    default = inspect.signature(server.aiworkhub_agent_collect_result).parameters["max_log_bytes"].default
    assert default == process_launcher.MAX_LOG_TAIL_BYTES


def test_no_tool_default_executes_process_launcher() -> None:
    """A default is evaluated at import, so naming process_launcher there undoes the deferral."""

    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    defaults = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                for sub in ast.walk(default):
                    if (
                        isinstance(sub, ast.Attribute)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "process_launcher"
                    ):
                        defaults.append(node.name)
    assert defaults == [], defaults
