"""NF-2026-02053: MCP start lists tools cheaply and defers the cleanup sweep.

Before the fix every stdio ``tools/list`` re-derived each tool schema with two
``typing.get_type_hints`` calls, and ``main()`` started the repository cleanup
sweep concurrently with ``initialize``, so the sweep's import and directory walk
held the GIL the stdio reader needed. These tests drive the real stdlib
transport in a fresh interpreter and assert structure, never wall-clock time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Executed aiworkhub modules (the package included) once the stdlib transport
# has answered initialize and the first tools/list. Measured 2026-11-04: 54,
# one of them the child's stand-in aiworkhub.storage_retention.
STARTUP_AIWORKHUB_MODULE_CEILING = 60
CHILD_TIMEOUT_SECONDS = 600
_PREFIX = "NF02053_PAYLOAD="

_CHILD = r"""
try:
    import sitecustomize  # noqa: F401 - -S skips site; the AppContainer lane shim must still load
except ImportError:
    pass
import collections, importlib.util, io, json, sys, threading, types, typing
from pathlib import Path

lazy_type = importlib.util._LazyModule
repo_root = Path(sys.argv[1])

def executed():
    return sorted(
        name for name, module in list(sys.modules.items())
        if (name == "aiworkhub" or name.startswith("aiworkhub."))
        and module is not None and not isinstance(module, lazy_type)
    )

import aiworkhub
import aiworkhub.server as server
assert not server._MCP_SDK_AVAILABLE

hint_calls = collections.Counter()
real_get_type_hints = typing.get_type_hints

def counting_get_type_hints(obj, *args, **kwargs):
    hint_calls[getattr(obj, "__qualname__", repr(obj))] += 1
    return real_get_type_hints(obj, *args, **kwargs)

typing.get_type_hints = counting_get_type_hints

stdout_buffer = io.BytesIO()
sweeps = []

def record_sweep(root, **kwargs):
    # Snapshot what the client had already received when the sweep was asked for.
    sweeps.append({
        "root": str(root),
        "tools_list_written": b'"id": 2' in stdout_buffer.getvalue(),
        "served_flag": server._FIRST_TOOLS_LIST_SERVED.is_set(),
    })
    return True

fake_retention = types.ModuleType("aiworkhub.storage_retention")
fake_retention.schedule_repository_cleanup = record_sweep
sys.modules["aiworkhub.storage_retention"] = fake_retention
aiworkhub.storage_retention = fake_retention

requests = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "nf02053", "version": "0"},
    }},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
]
stdin_bytes = b"".join(json.dumps(r).encode("utf-8") + b"\n" for r in requests)

class _Stream:
    def __init__(self, buffer):
        self.buffer = buffer

# The same gate thread main() starts, before the transport reads anything.
gate = threading.Thread(target=server._enforce_terminal_retention_safely, args=(repo_root,), daemon=True)
gate.start()
sweeps_before_serving = len(sweeps)

real_stdin, real_stdout = sys.stdin, sys.stdout
sys.stdin, sys.stdout = _Stream(io.BytesIO(stdin_bytes)), _Stream(stdout_buffer)
try:
    server._run_stdio_fallback_server(server.mcp.name, server.mcp._tools, server.mcp.instructions)
finally:
    sys.stdin, sys.stdout = real_stdin, real_stdout

after_first_list = executed()
launcher = sys.modules.get("aiworkhub.process_launcher")
launcher_deferred = isinstance(launcher, lazy_type)
first_hint_calls = dict(hint_calls)
first_stats = dict(server._STDIO_TOOLS_LIST_STATS)

replies = {}
for line in stdout_buffer.getvalue().splitlines():
    message = json.loads(line)
    replies[message.get("id")] = message
first_tools = replies[2]["result"]["tools"]

second_tools = server._stdio_tools_list(server.mcp._tools)["tools"]
second_tools[0]["name"] = "mutated-by-caller"
second_tools[0]["inputSchema"]["mutated"] = True
third_tools = server._stdio_tools_list(server.mcp._tools)["tools"]
relist_stats = dict(server._STDIO_TOOLS_LIST_STATS)

# A profile change between two listings is seen at once: only schemas are cached.
from types import MappingProxyType
from aiworkhub import mcp_tool_metadata
surface = server.mcp_surface()
hidden = first_tools[0]["name"]
previous_profile = mcp_tool_metadata.MCP_TOOL_PROFILES.get(hidden)
mcp_tool_metadata.MCP_TOOL_PROFILES[hidden] = MappingProxyType({surface: "callable"})
try:
    profiled_tools = server._stdio_tools_list(server.mcp._tools)["tools"]
finally:
    if previous_profile is None:
        del mcp_tool_metadata.MCP_TOOL_PROFILES[hidden]
    else:
        mcp_tool_metadata.MCP_TOOL_PROFILES[hidden] = previous_profile

gate.join(timeout=60)

print(PREFIX + json.dumps({
    "executed": after_first_list,
    "launcher_deferred": launcher_deferred,
    "initialize_ok": "result" in replies.get(1, {}),
    "tool_count": len(first_tools),
    "first_hint_calls": first_hint_calls,
    "hint_calls_after_relist": dict(hint_calls),
    "first_stats": first_stats,
    "relist_stats": relist_stats,
    "final_stats": dict(server._STDIO_TOOLS_LIST_STATS),
    "relist_identical": first_tools == third_tools,
    "hidden": hidden,
    "first_names": [tool["name"] for tool in first_tools],
    "profiled_names": [tool["name"] for tool in profiled_tools],
    "profiled_identical_rest": profiled_tools == first_tools[1:],
    "sweeps_before_serving": sweeps_before_serving,
    "sweeps": sweeps,
    "gate_alive": gate.is_alive(),
}))
"""


def _child_env(repo: Path) -> dict[str, str]:
    # Extend PYTHONPATH, never replace it (the AppContainer lane keeps its shim
    # there), but drop entries carrying the SDK so -S really selects stdlib.
    inherited = [
        part
        for part in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if part and not (Path(part, "mcp").is_dir() or Path(part, "pydantic").is_dir())
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SRC_ROOT), *inherited])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["AIWORKHUB_MCP_STDIO_BACKEND"] = "stdlib"
    env["AIWORKHUB_REPO_ROOT"] = str(repo)
    env["AIWORKHUB_REPO"] = str(repo)
    env.pop("PYTHONSTARTUP", None)
    env.pop("AIWORKHUB_MCP_SURFACE", None)
    return env


@pytest.fixture(scope="module")
def startup_report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    repo = tmp_path_factory.mktemp("nf02053_repo")
    completed = subprocess.run(
        [sys.executable, "-S", "-c", _CHILD.replace("PREFIX", repr(_PREFIX)), str(repo)],
        cwd=str(REPO_ROOT),
        env=_child_env(repo),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    payloads = [line for line in completed.stdout.splitlines() if line.startswith(_PREFIX)]
    assert payloads, f"child printed no payload; stderr: {completed.stderr[-4000:]}"
    return json.loads(payloads[-1][len(_PREFIX):])


def test_first_tools_list_stays_within_the_module_budget(startup_report: dict) -> None:
    executed = startup_report["executed"]
    assert startup_report["initialize_ok"] is True
    assert startup_report["tool_count"] > 0
    assert "aiworkhub.server" in executed
    assert len(executed) <= STARTUP_AIWORKHUB_MODULE_CEILING, (
        f"initialize + first tools/list executed {len(executed)} aiworkhub modules "
        f"(ceiling {STARTUP_AIWORKHUB_MODULE_CEILING}): {executed}"
    )
    # Evaluating tool type hints must not execute what NF-2026-01988 defers.
    assert startup_report["launcher_deferred"] is True
    assert "aiworkhub.process_launcher" not in executed


def test_tool_hints_are_evaluated_once_and_relists_hit_the_cache(startup_report: dict) -> None:
    first = startup_report["first_hint_calls"]
    assert first, "the first tools/list evaluated no type hints"
    assert max(first.values()) == 1, {k: v for k, v in first.items() if v > 1}
    # Two more listings: no further evaluation, every schema served from the cache.
    assert startup_report["hint_calls_after_relist"] == first
    count = startup_report["tool_count"]
    first_stats = startup_report["first_stats"]
    assert first_stats["hits"] + first_stats["misses"] == count
    assert first_stats["misses"] >= 1
    assert startup_report["relist_stats"] == {
        "hits": first_stats["hits"] + 2 * count,
        "misses": first_stats["misses"],
    }
    # A caller mutating its copy (entry or schema) never reaches the cache.
    assert startup_report["relist_identical"] is True


def test_profile_change_between_listings_is_reflected_while_schemas_hit(startup_report: dict) -> None:
    hidden = startup_report["hidden"]
    assert hidden in startup_report["first_names"]
    assert startup_report["profiled_names"] == [
        name for name in startup_report["first_names"] if name != hidden
    ]
    assert startup_report["profiled_identical_rest"] is True
    relist, final = startup_report["relist_stats"], startup_report["final_stats"]
    # The re-filtered listing built no schema: each remaining one was a hit.
    assert final["misses"] == relist["misses"]
    assert final["hits"] == relist["hits"] + startup_report["tool_count"] - 1


def test_no_sweep_is_scheduled_before_the_first_tools_list_is_answered(startup_report: dict) -> None:
    assert startup_report["sweeps_before_serving"] == 0
    sweeps = startup_report["sweeps"]
    assert len(sweeps) == 1, sweeps
    assert sweeps[0]["tools_list_written"] is True
    assert sweeps[0]["served_flag"] is True
    assert startup_report["gate_alive"] is False


def _isolated_gate(monkeypatch: pytest.MonkeyPatch):
    from aiworkhub import server, storage_retention

    served = threading.Event()
    cancelled = threading.Event()
    monkeypatch.setattr(server, "_FIRST_TOOLS_LIST_SERVED", served)
    monkeypatch.setattr(server, "_STARTUP_SWEEP_CANCELLED", cancelled)
    scheduled: list[Path] = []
    monkeypatch.setattr(
        storage_retention,
        "schedule_repository_cleanup",
        lambda root: scheduled.append(root) or True,
    )
    return server, served, cancelled, scheduled


def test_shutdown_cancels_a_pending_startup_sweep(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server, served, cancelled, scheduled = _isolated_gate(monkeypatch)
    cancelled.set()
    served.set()  # what main()'s finally does

    server._enforce_terminal_retention_safely(tmp_path)

    assert scheduled == []


def test_startup_sweep_runs_once_the_first_listing_was_served(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server, served, _cancelled, scheduled = _isolated_gate(monkeypatch)
    server._mark_first_tools_list_served()
    assert served.is_set()

    server._enforce_terminal_retention_safely(tmp_path)

    assert scheduled == [tmp_path]


def test_startup_sweep_is_bounded_when_no_listing_is_ever_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An SDK transport never reports tools/list; the delay is bounded, not infinite.
    server, served, _cancelled, scheduled = _isolated_gate(monkeypatch)
    monkeypatch.setattr(server, "_STARTUP_SWEEP_MAX_DELAY_SECONDS", 0.0)

    server._enforce_terminal_retention_safely(tmp_path)

    assert not served.is_set()
    assert scheduled == [tmp_path]
