"""NF-2026-02289: bounded read-only native_memory section in doctor server_process."""

from __future__ import annotations

import ctypes
import sqlite3
import sys
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
}
SQLITE_KEYS = ("sqlite_memory_used", "sqlite_memory_highwater")
WINDOWS_KEYS = ("private_bytes", "peak_working_set_bytes")


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
        else:
            assert set(value) == {"error"} and isinstance(value["error"], str)
    assert _is_int(native["live_sqlite_connections"])
    assert native["python_allocated_blocks"] > 0
    if sys.platform == "win32":
        assert all(_is_int(native[key]) and native[key] > 0 for key in WINDOWS_KEYS)
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
