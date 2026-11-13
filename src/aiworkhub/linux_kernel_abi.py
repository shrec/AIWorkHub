"""Dependency-free Linux kernel ABI ctypes structures (Landlock, openat2, seccomp)."""

from __future__ import annotations

import ctypes

__all__ = [
    "LANDLOCK_ACCESS_FS_READ_DIR",
    "LANDLOCK_ACCESS_FS_READ_FILE",
    "LANDLOCK_CREATE_RULESET_VERSION",
    "LANDLOCK_RULE_PATH_BENEATH",
    "PR_SET_NO_NEW_PRIVS",
    "SYS_LANDLOCK_ADD_RULE",
    "SYS_LANDLOCK_CREATE_RULESET",
    "SYS_LANDLOCK_RESTRICT_SELF",
    "_LandlockPathBeneathAttr",
    "_LandlockRulesetAttr",
    "_OpenHow",
    "_SeccompData",
    "_SeccompNotif",
    "_SeccompNotifResp",
]

# Landlock syscall numbers are identical on every Linux architecture (unified
# syscall table since 5.13), and the access bits are the uapi values.
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
PR_SET_NO_NEW_PRIVS = 38


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


class _SeccompData(ctypes.Structure):
    _fields_ = [
        ("nr", ctypes.c_int32),
        ("arch", ctypes.c_uint32),
        ("instruction_pointer", ctypes.c_uint64),
        ("args", ctypes.c_uint64 * 6),
    ]


class _SeccompNotif(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint64),
        ("pid", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("data", _SeccompData),
    ]


class _SeccompNotifResp(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint64),
        ("val", ctypes.c_int64),
        ("error", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
    ]
