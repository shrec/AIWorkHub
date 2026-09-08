"""Canonical ctypes layouts for the Win32 Job Object limit structures.

These declarations are import-safe on every platform.  Windows scalar widths
are explicit so their ABI can be validated on Linux; only ``SIZE_T`` and
``ULONG_PTR`` follow the process pointer width.
"""

from __future__ import annotations

import ctypes

DWORD = ctypes.c_uint32
LARGE_INTEGER = ctypes.c_int64
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", LARGE_INTEGER),
        ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


__all__ = [
    "IO_COUNTERS",
    "JOBOBJECT_BASIC_LIMIT_INFORMATION",
    "JOBOBJECT_EXTENDED_LIMIT_INFORMATION",
]
