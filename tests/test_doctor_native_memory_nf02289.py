"""NF-2026-02289: bounded read-only native_memory section in doctor server_process."""

from __future__ import annotations

import ctypes
import sqlite3
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import doctor

KEYS = {
    "sqlite_memory_used",
    "sqlite_memory_highwater",
    "live_sqlite_connections",
    "python_allocated_blocks",
    "private_bytes",
    "peak_working_set_bytes",
    "vm_rss_bytes",
    "vm_swap_bytes",
    "rss_anon_bytes",
    "mallinfo2",
}
SQLITE_KEYS = ("sqlite_memory_used", "sqlite_memory_highwater")
WINDOWS_KEYS = ("private_bytes", "peak_working_set_bytes")
LINUX_KEYS = ("vm_rss_bytes", "vm_swap_bytes", "rss_anon_bytes", "mallinfo2")


@pytest.fixture(autouse=True)
def _cheap_census(monkeypatch: pytest.MonkeyPatch) -> None:
    # The native section is independent of the census; keep the census tiny.
    monkeypatch.setattr(doctor, "HEAP_CENSUS_MAX_OBJECTS", 10)


def _native() -> dict[str, Any]:
    reply = doctor.server_process(heap_census=True)
    native = reply["native_memory"]
    assert "error" not in native, native
    return native


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def test_section_shape() -> None:
    native = _native()
    assert set(native) == KEYS | {"reasons"}
    reasons = native["reasons"]
    for key in KEYS:
        value = native[key]
        if _is_int(value):
            assert value >= 0
            assert key not in reasons
        elif value is None:
            assert isinstance(reasons[key], str) and reasons[key]
        elif key == "mallinfo2" and "error" not in value:
            assert set(value) == set(doctor.MALLINFO2_FIELDS)
            assert all(_is_int(field) for field in value.values())
        else:
            assert set(value) == {"error"} and isinstance(value["error"], str)
    assert _is_int(native["live_sqlite_connections"])
    assert native["python_allocated_blocks"] > 0
    if sys.platform == "win32":
        assert all(_is_int(native[key]) and native[key] > 0 for key in WINDOWS_KEYS)
        assert all(native[key] is None for key in LINUX_KEYS)
        assert all(reasons[key] == "linux_only" for key in LINUX_KEYS)
    else:
        assert all(native[key] is None for key in WINDOWS_KEYS)
        assert all(reasons[key] == "windows_only" for key in WINDOWS_KEYS)


def test_live_sqlite_connections_increases_by_one() -> None:
    before = _native()["live_sqlite_connections"]
    connection = sqlite3.connect(":memory:")
    try:
        after = _native()["live_sqlite_connections"]
    finally:
        connection.close()
    assert after == before + 1


def test_ctypes_resolution_failure_yields_null_with_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("simulated resolution failure")

    monkeypatch.setattr(ctypes, "CDLL", broken)
    native = _native()
    for key in SQLITE_KEYS:
        assert native[key] is None
        assert native["reasons"][key] == "sqlite_library_unresolved:OSError"
    # Unrelated probes are unaffected.
    assert _is_int(native["live_sqlite_connections"])
    assert _is_int(native["python_allocated_blocks"])


def test_unexpected_probe_failure_reports_error_type(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> int:
        raise ValueError("secret detail must not leak")

    monkeypatch.setattr(doctor, "_sqlite_memory_used", boom)
    native = _native()
    assert native["sqlite_memory_used"] == {"error": "ValueError"}
    assert "secret detail" not in repr(native)
    assert native["sqlite_memory_highwater"] != {"error": "ValueError"}


def test_highwater_probe_does_not_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[int, ...]]] = []

    def record(symbol: str, *args: int) -> int:
        calls.append((symbol, args))
        return 1

    monkeypatch.setattr(doctor, "_sqlite_counter", record)
    native = _native()
    assert native["sqlite_memory_highwater"] == 1
    assert ("sqlite3_memory_highwater", (0,)) in calls


def test_default_server_process_is_unchanged() -> None:
    default = doctor.server_process()
    assert "native_memory" not in default
    assert "heap_census" not in default
    assert set(default) == set(doctor.server_process(heap_census=False))


# NF-2026-02331: Linux /proc/self/status counters and glibc mallinfo2.
PROC_STATUS_TEXT = (
    "Name:\tpython\n"
    "VmRSS:\t  2048 kB\n"
    "RssAnon:\t  1024 kB\n"
    "VmSwap:\t     3 kB\n"
)


class _FakeLibc:
    def __init__(self) -> None:
        def mallinfo2() -> Any:
            return SimpleNamespace(
                arena=10, ordblks=1, smblks=0, hblks=1, hblkhd=20, usmblks=0,
                fsmblks=0, uordblks=30, fordblks=40, keepcost=50,
            )

        self.mallinfo2 = mallinfo2


def test_linux_glibc_probes_report_ints_and_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform_io, "is_linux", lambda: True)
    monkeypatch.setattr(doctor, "_read_proc_status", lambda: PROC_STATUS_TEXT)
    monkeypatch.setattr(doctor, "_glibc_library", _FakeLibc)
    native = doctor._native_memory()
    assert native["vm_rss_bytes"] == 2048 * 1024
    assert native["rss_anon_bytes"] == 1024 * 1024
    assert native["vm_swap_bytes"] == 3 * 1024
    assert native["mallinfo2"] == {
        "arena": 10,
        "hblkhd": 20,
        "uordblks": 30,
        "fordblks": 40,
        "keepcost": 50,
    }
    for key in LINUX_KEYS:
        assert key not in native["reasons"]


def test_off_linux_probes_are_null_with_named_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform_io, "is_linux", lambda: False)
    native = doctor._native_memory()
    for key in LINUX_KEYS:
        assert native[key] is None
        assert native["reasons"][key] == "linux_only"
    assert _is_int(native["python_allocated_blocks"])


def test_non_glibc_and_missing_mallinfo2_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform_io, "is_linux", lambda: True)
    monkeypatch.setattr(doctor, "_read_proc_status", lambda: PROC_STATUS_TEXT)
    monkeypatch.setattr(doctor.os, "confstr", lambda _name: "musl 1.2", raising=False)
    native = doctor._native_memory()
    assert native["mallinfo2"] is None
    assert native["reasons"]["mallinfo2"] == "glibc_only"
    assert native["vm_rss_bytes"] == 2048 * 1024

    monkeypatch.setattr(doctor, "_glibc_library", lambda: SimpleNamespace())
    native = doctor._native_memory()
    assert native["mallinfo2"] is None
    assert native["reasons"]["mallinfo2"] == "mallinfo2_missing"


def test_proc_status_failures_stay_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable() -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(doctor.platform_io, "is_linux", lambda: True)
    monkeypatch.setattr(doctor, "_read_proc_status", unreadable)
    monkeypatch.setattr(doctor, "_glibc_library", _FakeLibc)
    native = doctor._native_memory()
    for key in ("vm_rss_bytes", "vm_swap_bytes", "rss_anon_bytes"):
        assert native[key] is None
        assert native["reasons"][key] == "proc_status_unreadable:PermissionError"
    assert isinstance(native["mallinfo2"], dict)

    monkeypatch.setattr(doctor, "_read_proc_status", lambda: "VmRSS:\tlots kB\n")
    native = doctor._native_memory()
    assert native["reasons"]["vm_rss_bytes"] == "proc_status_unparsed:VmRSS"
    assert native["reasons"]["vm_swap_bytes"] == "proc_status_field_missing:VmSwap"
