"""NF-2026-01584: ACCESS_DENIED liveness only while the PID is enumerated."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from aiworkhub import _platform_process


class _FakeWindowsFunction:
    """Local ctypes-function stand-in with settable argtypes/restype."""

    def __init__(self, function):
        self.function = function
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.function(*args)


def _set_bytes_returned(target, value: int) -> None:
    holder = getattr(target, "_obj", target)
    holder.value = value


def _install_kernel32(monkeypatch, kernel32, last_error: int) -> None:
    monkeypatch.setattr(
        _platform_process.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel32,
        raising=False,
    )
    monkeypatch.setattr(
        _platform_process.ctypes, "get_last_error", lambda: last_error, raising=False
    )


def _fake_kernel32(open_result: int, enum_function) -> SimpleNamespace:
    return SimpleNamespace(
        OpenProcess=_FakeWindowsFunction(lambda *_args: open_result),
        GetExitCodeProcess=_FakeWindowsFunction(
            lambda *_args: pytest.fail("exit code")
        ),
        CloseHandle=_FakeWindowsFunction(lambda *_args: pytest.fail("close")),
        K32EnumProcesses=_FakeWindowsFunction(enum_function),
    )


def test_access_denied_pid_absent_from_enum_list_is_dead(monkeypatch):
    def fake_enum(array, _sizeof_array, bytes_returned):
        array[0] = 4
        array[1] = 8
        _set_bytes_returned(bytes_returned, 8)
        return 1

    kernel32 = _fake_kernel32(0, fake_enum)
    _install_kernel32(monkeypatch, kernel32, 5)
    assert _platform_process.windows_process_is_alive(42) is False
    assert len(kernel32.K32EnumProcesses.calls) == 1


def test_access_denied_pid_present_in_enum_list_is_alive(monkeypatch):
    def fake_enum(array, _sizeof_array, bytes_returned):
        array[0] = 4
        array[1] = 42
        _set_bytes_returned(bytes_returned, 8)
        return 1

    _install_kernel32(monkeypatch, _fake_kernel32(0, fake_enum), 5)
    assert _platform_process.windows_process_is_alive(42) is True


def test_access_denied_enum_failure_fails_closed_alive(monkeypatch):
    _install_kernel32(monkeypatch, _fake_kernel32(0, lambda *_args: 0), 5)
    assert _platform_process.windows_process_is_alive(42) is True


def test_access_denied_enum_exception_fails_closed_alive(monkeypatch):
    def fake_enum(*_args):
        raise OSError("K32EnumProcesses failed")

    _install_kernel32(monkeypatch, _fake_kernel32(0, fake_enum), 5)
    assert _platform_process.windows_process_is_alive(42) is True


def test_non_access_denied_error_skips_enumeration(monkeypatch):
    kernel32 = _fake_kernel32(0, lambda *_args: 1)
    _install_kernel32(monkeypatch, kernel32, 87)
    assert _platform_process.windows_process_is_alive(42) is False
    assert kernel32.K32EnumProcesses.calls == []


def test_enum_buffer_grows_until_partial_fill(monkeypatch):
    sizes = []

    def fake_enum(array, _sizeof_array, bytes_returned):
        sizes.append(len(array))
        if len(array) == 4096:
            _set_bytes_returned(bytes_returned, len(array) * 4)
            return 1
        array[0] = 4
        array[1] = 42
        _set_bytes_returned(bytes_returned, 8)
        return 1

    _install_kernel32(monkeypatch, _fake_kernel32(0, fake_enum), 5)
    assert _platform_process.windows_process_is_alive(42) is True
    assert sizes == [4096, 8192]


@pytest.mark.skipif(os.name != "nt", reason="windows-only real host check")
def test_windows_host_current_process_is_alive():
    assert _platform_process.windows_process_is_alive(os.getpid()) is True
