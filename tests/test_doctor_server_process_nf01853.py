from __future__ import annotations

import ctypes
import os
from types import SimpleNamespace

from aiworkhub import doctor, platform_io, runtime_generation

KEYS = {"rss_bytes", "peak_rss_bytes", "private_bytes"}


def test_process_memory_bytes_on_this_host():
    memory = platform_io.process_memory_bytes()
    assert set(memory) == KEYS
    assert isinstance(memory["peak_rss_bytes"], int) and memory["peak_rss_bytes"] > 0
    # Only Windows and Linux report a current RSS; other POSIX reports the peak alone.
    if platform_io.is_windows() or platform_io.is_linux():
        assert isinstance(memory["rss_bytes"], int) and memory["rss_bytes"] > 0
        assert memory["peak_rss_bytes"] >= memory["rss_bytes"]


def test_process_memory_bytes_never_raises(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(platform_io, "is_windows", boom)
    assert platform_io.process_memory_bytes() == dict.fromkeys(KEYS)


def test_process_memory_declares_argtypes_and_restype(monkeypatch):
    recorded: dict[str, object] = {}

    class _Proc:
        argtypes = None
        restype = None

        def __call__(self, *_args: object) -> int:
            recorded["argtypes"] = self.argtypes
            recorded["restype"] = self.restype
            return 0

    kernel32 = SimpleNamespace(
        GetCurrentProcess=_Proc(), K32GetProcessMemoryInfo=_Proc()
    )
    monkeypatch.setattr(platform_io, "is_windows", lambda _name=None: True)
    monkeypatch.setattr(
        platform_io.ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False
    )

    assert platform_io.process_memory_bytes() == dict.fromkeys(KEYS)
    assert recorded["restype"] is ctypes.c_int
    argtypes = recorded["argtypes"]
    assert argtypes is not None and len(argtypes) == 3
    assert kernel32.GetCurrentProcess.restype is ctypes.c_void_p


def test_server_process_shape():
    info = doctor.server_process()
    assert info["pid"] == os.getpid()
    assert isinstance(info["python_allocated_blocks"], int)
    assert KEYS <= set(info)
    assert "runtime_generation" in info and "superseded" in info


def test_server_process_survives_runtime_generation_failure(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime_generation, "own_generation", boom)
    monkeypatch.setattr(runtime_generation, "superseded", boom)
    info = doctor.server_process()
    assert info["runtime_generation"] is None
    assert info["superseded"] is None


def test_doctor_reply_keeps_keys_and_adds_server_process():
    from aiworkhub import server

    reply = server.aiworkhub_doctor_diagnose()
    assert {"schema_id", "verdict", "problems", "probes"} <= set(reply)
    assert reply["server_process"]["pid"] == os.getpid()
