"""Private typed backends for process-specific platform behavior."""

from __future__ import annotations

import ctypes
import os
import subprocess
from typing import Any, Protocol, TypedDict, cast


class BackgroundProcessLaunchKwargs(TypedDict, total=False):
    creationflags: int
    startupinfo: Any
    start_new_session: bool


class ProcessBackend(Protocol):
    def background_launch_kwargs(self) -> BackgroundProcessLaunchKwargs: ...

    def process_is_alive(self, pid: int) -> bool: ...


class PosixProcessBackend:
    def background_launch_kwargs(self) -> BackgroundProcessLaunchKwargs:
        return {"start_new_session": True}

    def process_is_alive(self, pid: int) -> bool:
        if not _valid_pid(pid):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True


class WindowsProcessBackend:
    def background_launch_kwargs(self) -> BackgroundProcessLaunchKwargs:
        # These attributes do not exist on every host, so resolve them only when
        # the explicitly selected backend is Windows.
        startupinfo_type = getattr(subprocess, "STARTUPINFO")
        startupinfo = startupinfo_type()
        startupinfo.dwFlags |= cast(
            int, getattr(subprocess, "STARTF_USESHOWWINDOW")
        )
        startupinfo.wShowWindow = cast(int, getattr(subprocess, "SW_HIDE"))
        return {
            "creationflags": cast(int, getattr(subprocess, "CREATE_NO_WINDOW")),
            "startupinfo": startupinfo,
        }

    def process_is_alive(self, pid: int) -> bool:
        return windows_process_is_alive(pid)


def _valid_pid(pid: int) -> bool:
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


def _backend(platform_name: str) -> ProcessBackend:
    if platform_name == "nt":
        return WindowsProcessBackend()
    return PosixProcessBackend()


def background_process_launch_kwargs(
    platform_name: str | None = None,
) -> BackgroundProcessLaunchKwargs:
    selected_platform = os.name if platform_name is None else platform_name
    return _backend(selected_platform).background_launch_kwargs()


def process_is_alive(pid: int) -> bool:
    return _backend(os.name).process_is_alive(pid)


def windows_process_is_alive(pid: int) -> bool:
    if not _valid_pid(pid):
        return False

    win_dll = getattr(ctypes, "WinDLL")
    get_last_error = getattr(ctypes, "get_last_error")
    kernel32 = win_dll("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    open_process.restype = ctypes.c_void_p
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    get_exit_code.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int

    handle = open_process(0x1000, False, pid)
    if not handle:
        return bool(get_last_error() == 5)
    exit_code = ctypes.c_uint32()
    try:
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return False
        return bool(exit_code.value == 259)
    finally:
        close_handle(handle)
