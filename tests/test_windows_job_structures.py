"""Platform-neutral ABI and consumer checks for Windows Job Objects."""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

from aiworkhub import app_server_mux, windows_appcontainer, worker_supervisor
from aiworkhub.windows_job_structures import (
    IO_COUNTERS,
    JOBOBJECT_BASIC_LIMIT_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
)

KILL_ON_JOB_CLOSE = 0x00002000


def _field_names(structure):
    return [name for name, _ctype in structure._fields_]


def test_job_object_layout_field_order_widths_offsets_and_alignment():
    assert _field_names(IO_COUNTERS) == [
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    ]
    assert all(ctypes.sizeof(kind) == 8 for _name, kind in IO_COUNTERS._fields_)
    assert _field_names(JOBOBJECT_BASIC_LIMIT_INFORMATION) == [
        "PerProcessUserTimeLimit", "PerJobUserTimeLimit", "LimitFlags",
        "MinimumWorkingSetSize", "MaximumWorkingSetSize", "ActiveProcessLimit",
        "Affinity", "PriorityClass", "SchedulingClass",
    ]
    basic_types = dict(JOBOBJECT_BASIC_LIMIT_INFORMATION._fields_)
    assert ctypes.sizeof(basic_types["PerProcessUserTimeLimit"]) == 8
    assert ctypes.sizeof(basic_types["PerJobUserTimeLimit"]) == 8
    for name in ("LimitFlags", "ActiveProcessLimit", "PriorityClass", "SchedulingClass"):
        assert ctypes.sizeof(basic_types[name]) == 4
    for name in ("MinimumWorkingSetSize", "MaximumWorkingSetSize", "Affinity"):
        assert ctypes.sizeof(basic_types[name]) == ctypes.sizeof(ctypes.c_void_p)

    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.alignment(IO_COUNTERS) == 8
    assert ctypes.sizeof(IO_COUNTERS) == 48
    if pointer_size == 8:
        assert ctypes.sizeof(JOBOBJECT_BASIC_LIMIT_INFORMATION) == 64
        assert [getattr(JOBOBJECT_BASIC_LIMIT_INFORMATION, name).offset for name in _field_names(JOBOBJECT_BASIC_LIMIT_INFORMATION)] == [0, 8, 16, 24, 32, 40, 48, 56, 60]
        assert ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144
        assert [getattr(JOBOBJECT_EXTENDED_LIMIT_INFORMATION, name).offset for name in _field_names(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)] == [0, 64, 112, 120, 128, 136]
    assert ctypes.alignment(JOBOBJECT_BASIC_LIMIT_INFORMATION) == 8
    assert ctypes.alignment(JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 8


class _Function:
    def __init__(self, result=1):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class _Kernel32:
    def __init__(self):
        self.CreateJobObjectW = _Function(71)
        self.SetInformationJobObject = _Function(1)
        self.AssignProcessToJobObject = _Function(1)
        self.CloseHandle = _Function(1)


def _assert_job_buffer(call):
    handle, info_class, buffer, size = call
    assert handle
    assert info_class == 9
    assert size == ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
    assert type(buffer._obj) is JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    assert buffer._obj.BasicLimitInformation.LimitFlags == KILL_ON_JOB_CLOSE


def test_appcontainer_uses_canonical_job_buffer():
    kernel32 = _Kernel32()
    api = object.__new__(windows_appcontainer._CtypesWin32Api)
    api._kernel32 = kernel32
    api.configure_job_object(71)
    _assert_job_buffer(kernel32.SetInformationJobObject.calls[0])


def test_mux_uses_canonical_job_buffer_and_assigns_child(monkeypatch):
    kernel32 = _Kernel32()
    monkeypatch.setattr(app_server_mux.os, "name", "nt")
    monkeypatch.setattr(app_server_mux.ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    monkeypatch.setattr(app_server_mux, "_close_windows_handle", lambda _handle: None)
    handle = app_server_mux._bind_child_lifetime_to_this_process(SimpleNamespace(_handle=83))
    assert handle == 71
    _assert_job_buffer(kernel32.SetInformationJobObject.calls[0])
    assert int(kernel32.AssignProcessToJobObject.calls[0][1].value) == 83


def test_supervisor_uses_canonical_job_buffer_assigns_and_closes(monkeypatch):
    kernel32 = _Kernel32()
    monkeypatch.setattr(worker_supervisor.ctypes, "WinDLL", lambda *_a, **_k: kernel32, raising=False)
    job = worker_supervisor._WindowsKillOnCloseJob()
    _assert_job_buffer(kernel32.SetInformationJobObject.calls[0])
    job.assign(SimpleNamespace(_handle=83))
    assert kernel32.AssignProcessToJobObject.calls[0] == (71, 83)
    job.close()
    assert kernel32.CloseHandle.calls == [(71,)]
    assert job._handle is None
