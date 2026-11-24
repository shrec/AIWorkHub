"""NF-2026-02407: arena cap and per-call heap trim on the mcp-SDK backend too.

The FastMCP (SDK) backend caps glibc arenas once at server start and schedules
the coalesced heap trim after every tool call from its single registration
wrapper; the heap census names the serving backend, the arena-cap outcome and
the trim state; a tool call that raises VmHWM by >= 256 MiB logs one line.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import doctor, platform_io

TESTS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TESTS_ROOT.parent
SRC_ROOT = REPO_ROOT / "src"
CHILD_TIMEOUT_SECONDS = 600
_PREFIX = "NF02407_PAYLOAD="
_MIB = 1024 * 1024


def _reset() -> None:
    platform_io._HEAP_TRIM_STATE.update({
        "probed": False,
        "handle": None,
        "libc": None,
        "last": None,
        "timer": None,
        "arena_cap": None,
        "count": 0,
    })
    platform_io._SERVING_HEAP_STATE.update({"backend": None, "arena_cap": None})


@pytest.fixture(autouse=True)
def _fresh_state() -> Any:
    _reset()
    yield
    _reset()


class _FakeLibc:
    def __init__(self) -> None:
        self.trims: list[int] = []

        def malloc_trim(pad: int) -> int:
            self.trims.append(pad)
            return 1

        self.malloc_trim = malloc_trim


def _fake_glibc(monkeypatch: pytest.MonkeyPatch) -> _FakeLibc:
    libc = _FakeLibc()
    monkeypatch.setattr(platform_io, "is_linux", lambda platform_name=None: True)
    monkeypatch.setattr(os, "confstr", lambda name: "glibc 2.35", raising=False)
    monkeypatch.setattr(platform_io.ctypes, "CDLL", lambda name: libc)
    return libc


def _fake_proc_peaks(monkeypatch: pytest.MonkeyPatch, peaks: list[int | None]) -> None:
    """Fake the /proc/self/status reader: one VmHWM value per read, in order."""

    def memory() -> dict[str, int | None]:
        return {"rss_bytes": None, "peak_rss_bytes": peaks.pop(0), "private_bytes": None}

    monkeypatch.setattr(platform_io, "is_linux", lambda platform_name=None: True)
    monkeypatch.setattr(platform_io, "process_memory_bytes", memory)


def _peak_lines(stderr: str) -> list[dict[str, Any]]:
    records = []
    for line in stderr.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("event") == "tool_peak_rss_growth":
            records.append(record)
    return records


# ---------------------------------------------------------------- start / census


def test_start_serving_heap_policy_caps_once_and_records_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    outcome = {"available": True, "capped": True, "limit": 2}

    def cap(limit: int = 2) -> dict[str, Any]:
        calls.append(limit)
        return dict(outcome)

    monkeypatch.setattr(platform_io, "cap_malloc_arenas", cap)
    assert platform_io.start_serving_heap_policy("mcp_sdk") == outcome
    assert calls == [2]
    state = platform_io.serving_heap_state()
    assert state["mcp_backend"] == "mcp_sdk"
    assert state["arena_cap"] == outcome
    assert "serving_reason" not in state


def test_start_serving_heap_policy_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(limit: int = 2) -> dict[str, Any]:
        raise RuntimeError("mallopt exploded")

    monkeypatch.setattr(platform_io, "cap_malloc_arenas", boom)
    result = platform_io.start_serving_heap_policy("stdlib")
    assert result == {"available": False, "capped": False, "reason": "cap_failed:RuntimeError"}
    assert platform_io.serving_heap_state()["arena_cap"] == result


def test_doctor_heap_census_reports_backend_arena_cap_and_trim_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = _fake_glibc(monkeypatch)
    outcome = {"available": True, "capped": True, "limit": 2}
    monkeypatch.setattr(platform_io, "cap_malloc_arenas", lambda limit=2: dict(outcome))
    # The object census itself is covered by NF-2026-02273; keep this one cheap.
    monkeypatch.setattr(doctor, "_heap_census", lambda limit: {"stub": True})
    monkeypatch.setattr(doctor, "_native_memory", lambda: {"reasons": {}})

    platform_io.start_serving_heap_policy("mcp_sdk")
    assert platform_io.schedule_heap_trim()["trimmed"] is True
    assert libc.trims == [0]

    reply = doctor.server_process(heap_census=True)
    assert reply["mcp_backend"] == "mcp_sdk"
    assert reply["arena_cap"] == outcome
    trim = reply["heap_trim"]
    assert set(trim) == {
        "count", "last_trim_monotonic", "seconds_since_last_trim", "timer_armed",
    }
    assert trim["count"] == 1
    assert trim["last_trim_monotonic"] is not None
    assert trim["timer_armed"] is False
    json.dumps(reply)  # the census stays JSON-serialisable

    # The default (non-census) reply is unchanged.
    plain = doctor.server_process()
    assert "mcp_backend" not in plain and "heap_trim" not in plain


def test_doctor_heap_census_names_an_unrecorded_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_heap_census", lambda limit: {"stub": True})
    monkeypatch.setattr(doctor, "_native_memory", lambda: {"reasons": {}})
    reply = doctor.server_process(heap_census=True)
    # Unknown is reported as unknown, never as a backend.
    assert reply["mcp_backend"] is None
    assert reply["arena_cap"] is None
    assert reply["serving_reason"] == "mcp_server_start_not_recorded"
    assert reply["heap_trim"]["count"] == 0


# ---------------------------------------------------------------- peak growth


def test_peak_growth_at_threshold_logs_one_bounded_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    threshold = platform_io.PEAK_RSS_GROWTH_LOG_BYTES
    assert threshold == 256 * _MIB
    _fake_proc_peaks(monkeypatch, [1024 * _MIB, 1024 * _MIB + threshold])
    before = platform_io.linux_peak_rss_bytes()
    long_name = "nf02407_tool_" + "x" * 500
    assert platform_io.note_tool_call_peak(long_name, before) == threshold
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = _peak_lines(captured.err)
    assert len(lines) == 1
    assert lines[0]["tool"] == long_name[:128]
    assert lines[0]["delta_bytes"] == threshold
    assert lines[0]["delta_mib"] == 256


def test_peak_growth_below_threshold_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    threshold = platform_io.PEAK_RSS_GROWTH_LOG_BYTES
    _fake_proc_peaks(monkeypatch, [2048 * _MIB, 2048 * _MIB + threshold - 1])
    before = platform_io.linux_peak_rss_bytes()
    assert platform_io.note_tool_call_peak("small", before) == threshold - 1
    assert _peak_lines(capsys.readouterr().err) == []


def test_peak_growth_is_a_noop_off_linux(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden() -> dict[str, int | None]:
        raise AssertionError("/proc must not be read off Linux")

    monkeypatch.setattr(platform_io, "is_linux", lambda platform_name=None: False)
    monkeypatch.setattr(platform_io, "process_memory_bytes", forbidden)
    assert platform_io.linux_peak_rss_bytes() is None
    assert platform_io.note_tool_call_peak("tool", None) is None
    assert platform_io.note_tool_call_peak("tool", 5) is None
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_peak_reader_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> dict[str, int | None]:
        raise OSError("proc unreadable")

    monkeypatch.setattr(platform_io, "is_linux", lambda platform_name=None: True)
    monkeypatch.setattr(platform_io, "process_memory_bytes", broken)
    assert platform_io.linux_peak_rss_bytes() is None
    assert platform_io.note_tool_call_peak("tool", 1024) is None


# ---------------------------------------------------------------- SDK backend


_CHILD = r"""
import asyncio, io, json, sys

import aiworkhub.server as server
assert server._MCP_SDK_AVAILABLE, "the SDK backend was not selected"
assert server._MCP_BACKEND_NAME == "mcp_sdk"
from aiworkhub import platform_io

MIB = 1024 * 1024
events = []

def cap(limit=2):
    events.append(["cap"])
    return {"available": False, "capped": False, "reason": "nf02407_fake"}

def schedule():
    events.append(["trim"])
    return {"available": False, "trimmed": False, "scheduled": False}

# Faked /proc reader: VmHWM before/after the first two calls; None afterwards.
peaks = [1024 * MIB, 1324 * MIB, 1324 * MIB, 1325 * MIB]

def memory():
    value = peaks.pop(0) if peaks else None
    return {"rss_bytes": None, "peak_rss_bytes": value, "private_bytes": None}

platform_io.cap_malloc_arenas = cap
platform_io.schedule_heap_trim = schedule
platform_io.is_linux = lambda platform_name=None: True
platform_io.process_memory_bytes = memory

class Stop(Exception):
    pass

def detach():
    events.append(["detach"])
    raise Stop

server._detach_inheritable_stdin = detach
try:
    server.main()
except Stop:
    pass

# Register probes through the one FastMCP registration wrapper.
server.mcp_tool_callable = lambda name, surface=None: True

def nf02407_probe(label: str = "x") -> dict:
    "NF-2026-02407 synchronous probe tool."
    events.append(["call", label])
    return {"ok": True, "label": label}

async def nf02407_async_probe() -> dict:
    "NF-2026-02407 asynchronous probe tool."
    events.append(["async_call"])
    return {"ok": True}

server.mcp.tool(name="nf02407_probe")(nf02407_probe)
server.mcp.tool(name="nf02407_async_probe")(nf02407_async_probe)
sync_tool = server.mcp._tool_manager.get_tool("nf02407_probe")
async_tool = server.mcp._tool_manager.get_tool("nf02407_async_probe")

captured = io.StringIO()
real_stderr = sys.stderr
sys.stderr = captured
results = []
try:
    results.append(asyncio.run(sync_tool.run({"label": "a"})))
    results.append(asyncio.run(sync_tool.run({"label": "b"})))
    results.append(asyncio.run(async_tool.run({})))

    # Every new path fails inside the wrapper: the reply is still the tool's own.
    def schedule_raises():
        events.append(["trim_raised"])
        raise RuntimeError("trim exploded")

    def memory_raises():
        raise OSError("proc unreadable")

    platform_io.schedule_heap_trim = schedule_raises
    platform_io.process_memory_bytes = memory_raises
    results.append(asyncio.run(sync_tool.run({"label": "c"})))
finally:
    sys.stderr = real_stderr

peak_lines = []
for line in captured.getvalue().splitlines():
    try:
        record = json.loads(line)
    except ValueError:
        continue
    if isinstance(record, dict) and record.get("event") == "tool_peak_rss_growth":
        peak_lines.append(record)

print(PREFIX + json.dumps({
    "events": events,
    "results": results,
    "peak_lines": peak_lines,
    "serving": platform_io.serving_heap_state(),
}, default=str))
"""


def _sdk_child_env(repo: Path) -> dict[str, str]:
    # Extend PYTHONPATH, never replace it (the AppContainer lane keeps its shim there).
    inherited = [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SRC_ROOT), *inherited])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["AIWORKHUB_MCP_STDIO_BACKEND"] = "sdk"
    env["AIWORKHUB_ALLOW_WRITES"] = "0"
    env["AIWORKHUB_REPO_ROOT"] = str(repo)
    env["AIWORKHUB_REPO"] = str(repo)
    env.pop("PYTHONSTARTUP", None)
    env.pop("AIWORKHUB_MCP_SURFACE", None)
    return env


def test_sdk_server_caps_arenas_once_and_trims_after_each_tool_call(tmp_path: Path) -> None:
    if importlib.util.find_spec("mcp") is None:
        pytest.skip("mcp SDK not importable: the FastMCP backend cannot be selected here")
    repo = tmp_path / "repo"
    repo.mkdir()
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD.replace("PREFIX", repr(_PREFIX)), str(repo)],
        cwd=str(REPO_ROOT),
        env=_sdk_child_env(repo),
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
    report = json.loads(payloads[-1][len(_PREFIX):])

    # Arenas are capped exactly once, first thing in main(); every tool call --
    # sync on its worker thread, async on the loop -- schedules one trim after it ran.
    assert report["events"] == [
        ["cap"],
        ["detach"],
        ["call", "a"],
        ["trim"],
        ["call", "b"],
        ["trim"],
        ["async_call"],
        ["trim"],
        ["call", "c"],
        ["trim_raised"],
    ]
    # The failing trim and /proc reader never reached the reply.
    assert [result["ok"] for result in report["results"]] == [True, True, True, True]
    assert report["results"][3]["label"] == "c"
    # Only the call that raised VmHWM by 300 MiB logged, once, naming the tool.
    assert len(report["peak_lines"]) == 1
    line = report["peak_lines"][0]
    assert line["tool"] == "nf02407_probe"
    assert line["delta_bytes"] == 300 * _MIB
    assert line["peak_rss_bytes"] == 1324 * _MIB
    # The census attributes this process to the SDK backend and its cap outcome.
    assert report["serving"]["mcp_backend"] == "mcp_sdk"
    assert report["serving"]["arena_cap"] == {
        "available": False, "capped": False, "reason": "nf02407_fake",
    }
