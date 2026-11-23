"""NF-2026-02273: trim freed glibc heap after every MCP request.

``schedule_heap_trim`` is a trailing-edge coalescing trim for the hot request
path, ``cap_malloc_arenas`` caps glibc arenas before the tool threads start,
the stdlib MCP transport calls both, and the single-threaded dashboard reader
trims synchronously after each reply.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from aiworkhub import dashboard_reader, platform_io

TESTS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TESTS_ROOT.parent
CHILD_TIMEOUT_SECONDS = 600
_PREFIX = "NF02273_PAYLOAD="


def _reset() -> None:
    platform_io._HEAP_TRIM_STATE.update({
        "probed": False,
        "handle": None,
        "libc": None,
        "last": None,
        "timer": None,
        "arena_cap": None,
    })


@pytest.fixture(autouse=True)
def _fresh_trim_state() -> Any:
    _reset()
    yield
    _reset()


class _FakeLibc:
    def __init__(self) -> None:
        self.trims: list[int] = []
        self.mallopts: list[tuple[int, int]] = []
        self.loads = 0

        def malloc_trim(pad: int) -> int:
            self.trims.append(pad)
            return 1

        def mallopt(param: int, value: int) -> int:
            self.mallopts.append((param, value))
            return 1

        self.malloc_trim = malloc_trim
        self.mallopt = mallopt


def _fake_glibc(monkeypatch: pytest.MonkeyPatch) -> _FakeLibc:
    libc = _FakeLibc()

    def load(name: str) -> _FakeLibc:
        assert name == "libc.so.6"
        libc.loads += 1
        return libc

    monkeypatch.setattr(platform_io, "is_linux", lambda platform_name=None: True)
    monkeypatch.setattr(os, "confstr", lambda name: "glibc 2.35", raising=False)
    monkeypatch.setattr(platform_io.ctypes, "CDLL", load)
    return libc


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    clock = {"now": 100.0}
    monkeypatch.setattr(platform_io, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    return clock


class _FakeTimer:
    def __init__(self, built: list[_FakeTimer], interval: float, function: Callable[..., Any],
                 args: tuple[Any, ...] = ()) -> None:
        self.interval = interval
        self.function = function
        self.args = args
        self.daemon = False
        self.started = False
        built.append(self)

    def start(self) -> None:
        self.started = True

    def fire(self) -> None:
        self.function(*self.args)


def _fake_timers(monkeypatch: pytest.MonkeyPatch) -> list[_FakeTimer]:
    built: list[_FakeTimer] = []

    def factory(interval: float, function: Callable[..., Any], args: tuple[Any, ...] = ()) -> _FakeTimer:
        return _FakeTimer(built, interval, function, args)

    monkeypatch.setattr(platform_io, "_HEAP_TRIM_TIMER", factory)
    return built


def test_schedule_trims_inline_then_arms_one_coalescing_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    libc = _fake_glibc(monkeypatch)
    clock = _fake_clock(monkeypatch)
    timers = _fake_timers(monkeypatch)
    interval = platform_io.HEAP_TRIM_MIN_INTERVAL_SECONDS

    first = platform_io.schedule_heap_trim()
    assert first == {"available": True, "trimmed": True, "scheduled": False}
    assert libc.trims == [0]
    assert timers == []

    clock["now"] += 1.0
    second = platform_io.schedule_heap_trim()
    assert second["scheduled"] is True and second["trimmed"] is False
    assert len(timers) == 1
    (timer,) = timers
    assert timer.started is True and timer.daemon is True
    assert timer.interval == pytest.approx(interval - 1.0)
    assert platform_io._HEAP_TRIM_STATE["timer"] is timer

    third = platform_io.schedule_heap_trim()
    assert third["coalesced"] is True
    assert third["trimmed"] is False and third["scheduled"] is False
    assert len(timers) == 1
    assert libc.trims == [0]

    # Fired early relative to the inline trim: still trims (no rate limit).
    clock["now"] += 0.5
    timer.fire()
    assert libc.trims == [0, 0]
    assert platform_io._HEAP_TRIM_STATE["timer"] is None
    assert platform_io._HEAP_TRIM_STATE["last"] == clock["now"]

    # The slot is free again: a later call inside the interval arms a new timer.
    clock["now"] += 0.5
    assert platform_io.schedule_heap_trim()["scheduled"] is True
    assert len(timers) == 2
    assert libc.loads == 1


def test_schedule_trims_inline_once_the_interval_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    libc = _fake_glibc(monkeypatch)
    clock = _fake_clock(monkeypatch)
    timers = _fake_timers(monkeypatch)
    platform_io.schedule_heap_trim()
    clock["now"] += platform_io.HEAP_TRIM_MIN_INTERVAL_SECONDS + 0.1
    assert platform_io.schedule_heap_trim()["trimmed"] is True
    assert libc.trims == [0, 0]
    assert timers == []


def test_schedule_off_glibc_is_a_no_op_without_a_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_io, "is_linux", lambda platform_name=None: False)

    def no_timer(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no timer may be built off glibc")

    monkeypatch.setattr(platform_io, "_HEAP_TRIM_TIMER", no_timer)
    for _ in range(3):
        result = platform_io.schedule_heap_trim()
        assert result["available"] is False
        assert result["scheduled"] is False
    assert platform_io._HEAP_TRIM_STATE["timer"] is None


def test_schedule_never_raises_when_the_timer_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_glibc(monkeypatch)
    clock = _fake_clock(monkeypatch)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(platform_io, "_HEAP_TRIM_TIMER", broken)
    platform_io.schedule_heap_trim()
    clock["now"] += 1.0
    result = platform_io.schedule_heap_trim()
    assert result["available"] is False
    assert result["reason"].startswith("schedule_failed:")
    assert platform_io._HEAP_TRIM_STATE["timer"] is None


def test_cap_malloc_arenas_calls_mallopt_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    libc = _fake_glibc(monkeypatch)
    monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
    first = platform_io.cap_malloc_arenas()
    assert first == {"available": True, "capped": True, "limit": 2}
    assert platform_io.cap_malloc_arenas() == first
    assert platform_io.cap_malloc_arenas() == first
    assert libc.mallopts == [(-8, 2)]
    assert libc.loads == 1


def test_cap_malloc_arenas_defers_to_the_operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    libc = _fake_glibc(monkeypatch)
    monkeypatch.setenv("MALLOC_ARENA_MAX", "4")
    result = platform_io.cap_malloc_arenas()
    assert result["capped"] is False
    assert result["skipped"] == "malloc_arena_max_env_set"
    assert libc.mallopts == []


def test_cap_malloc_arenas_off_glibc_and_on_load_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
    monkeypatch.setattr(platform_io, "is_linux", lambda platform_name=None: False)
    assert platform_io.cap_malloc_arenas()["available"] is False

    _reset()
    monkeypatch.setattr(platform_io, "is_linux", lambda platform_name=None: True)
    monkeypatch.setattr(os, "confstr", lambda name: "glibc 2.35", raising=False)

    def broken(name: str) -> Any:
        raise OSError("libc.so.6 missing")

    monkeypatch.setattr(platform_io.ctypes, "CDLL", broken)
    result = platform_io.cap_malloc_arenas()
    assert result["available"] is False and result["capped"] is False


def test_cap_malloc_arenas_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    libc = _fake_glibc(monkeypatch)
    monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)

    def boom(param: int, value: int) -> int:
        raise RuntimeError("mallopt exploded")

    libc.mallopt = boom  # type: ignore[assignment]
    result = platform_io.cap_malloc_arenas()
    assert result["available"] is False
    assert result["reason"].startswith("cap_failed:")


class _RecordingStream(io.BytesIO):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def write(self, data: Any) -> int:
        self._events.append("write")
        return super().write(data)


def _leaked_threads(before: set[threading.Thread]) -> list[threading.Thread]:
    """Threads started after ``before`` was taken that are still alive (identity, not a count)."""
    return [thread for thread in threading.enumerate() if thread not in before and thread.is_alive()]


def test_dashboard_reader_trims_synchronously_after_each_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(dashboard_reader, "drain_deferred_work", lambda: events.append("drain"))

    def trim() -> dict[str, Any]:
        events.append("trim")
        return {"available": False, "trimmed": False}

    def no_schedule() -> dict[str, Any]:
        raise AssertionError("the dashboard reader must not schedule a timer trim")

    monkeypatch.setattr(platform_io, "trim_process_heap", trim)
    monkeypatch.setattr(platform_io, "schedule_heap_trim", no_schedule)
    threads_before = set(threading.enumerate())
    stdout = _RecordingStream(events)
    code = dashboard_reader._serve_loop(io.BytesIO(b"{bad\n\n[1]\n"), stdout, {})
    assert code == 0
    assert events == ["write", "drain", "trim", "write", "drain", "trim"]
    assert len(stdout.getvalue().splitlines()) == 2
    leaked = _leaked_threads(threads_before)
    assert leaked == [], leaked


_CHILD = r"""
try:
    import sitecustomize  # noqa: F401 - -S skips site; the AppContainer lane shim must still load
except ImportError:
    pass
import concurrent.futures, io, json, sys

import aiworkhub.server as server
assert not server._MCP_SDK_AVAILABLE
from aiworkhub import platform_io

events = []
stdout_buffer = io.BytesIO()

def written_lines():
    return stdout_buffer.getvalue().count(b"\n")

def cap(limit=2):
    events.append(["cap"])
    return {"available": False, "capped": False}

def schedule():
    events.append(["trim", written_lines()])
    return {"available": False, "trimmed": False, "scheduled": False}

platform_io.cap_malloc_arenas = cap
platform_io.schedule_heap_trim = schedule

class InlineExecutor:
    def __init__(self, *args, **kwargs):
        events.append(["executor", kwargs.get("thread_name_prefix")])

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def shutdown(self, wait=True, cancel_futures=False):
        events.append(["shutdown"])

concurrent.futures.ThreadPoolExecutor = InlineExecutor

requests = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "nf02273", "version": "0"},
    }},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "nf02273/no_such_method"},
]
stdin_bytes = b"".join(json.dumps(r).encode("utf-8") + b"\n" for r in requests)

class _Stream:
    def __init__(self, buffer):
        self.buffer = buffer

real_stdin, real_stdout = sys.stdin, sys.stdout
sys.stdin, sys.stdout = _Stream(io.BytesIO(stdin_bytes)), _Stream(stdout_buffer)
try:
    server._run_stdio_fallback_server(server.mcp.name, server.mcp._tools, server.mcp.instructions)
finally:
    sys.stdin, sys.stdout = real_stdin, real_stdout

reply_ids = [json.loads(line).get("id") for line in stdout_buffer.getvalue().splitlines()]
print(PREFIX + json.dumps({"events": events, "reply_ids": reply_ids}))
"""


def _startup_child_env(repo: Path) -> dict[str, str]:
    # Reuse the owner of the stdlib-backend child environment (NF-2026-02053).
    spec = importlib.util.spec_from_file_location(
        "_nf02273_startup_owner", TESTS_ROOT / "test_mcp_startup_nf02053.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env: dict[str, str] = module._child_env(repo)
    return env


def test_stdlib_server_caps_arenas_first_and_trims_after_each_response(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    completed = subprocess.run(
        [sys.executable, "-S", "-c", _CHILD.replace("PREFIX", repr(_PREFIX)), str(repo)],
        cwd=str(REPO_ROOT),
        env=_startup_child_env(repo),
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
    assert report["reply_ids"] == [1, 2]
    # Arenas are capped before the tool executor exists; each answered request
    # trims once, after its response line was written; notifications do not.
    assert report["events"] == [
        ["cap"],
        ["executor", "aiworkhub-mcp-tool"],
        ["trim", 1],
        ["trim", 2],
        ["shutdown"],
    ]
