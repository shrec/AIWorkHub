"""Bounded Linux ``utimensat`` timestamp argument decoding for the metadata broker."""

from __future__ import annotations

import ctypes
import errno
import os
import struct

from aiworkhub.platform_io import is_linux


UTIME_NOW = 1_073_741_823
UTIME_OMIT = 1_073_741_822
TIMESPEC_PAIR_SIZE = 32
AT_EMPTY_PATH = 0x1000


class TimestampArgumentError(ValueError):
    """The caller supplied a timestamp representation the broker cannot emulate."""


def decode_utimensat_timespec(raw: bytes | None) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Validate and decode one snapshotted Linux 64-bit ABI timespec pair.

    This broker supports the Linux ABIs whose ``time_t`` and ``long`` are both
    signed 64-bit values (including x86-64 and arm64).  The decoded values are
    deliberately not resolved against a userspace stat snapshot: UTIME_NOW and
    UTIME_OMIT must reach the kernel unchanged to retain atomic/no-op semantics.
    """
    if not is_linux() or ctypes.sizeof(ctypes.c_long) != 8:
        raise TimestampArgumentError("metadata_broker_unsupported_timespec_abi")
    if raw is None:
        return None
    if len(raw) != TIMESPEC_PAIR_SIZE:
        raise TimestampArgumentError("metadata_broker_timespec_size")
    values = struct.unpack("=qqqq", raw)
    result: list[tuple[int, int]] = []
    for seconds, nanoseconds in zip(values[::2], values[1::2], strict=True):
        if nanoseconds not in (UTIME_NOW, UTIME_OMIT) and not (
            0 <= nanoseconds < 1_000_000_000
        ):
            raise TimestampArgumentError("metadata_broker_invalid_nanoseconds")
        result.append((seconds, nanoseconds))
    return result[0], result[1]


def apply_utimensat_fd(fd: int, raw: bytes | None) -> None:
    """Apply a validated snapshot to an authenticated fd with kernel semantics."""
    decoded = decode_utimensat_timespec(raw)
    argument = None
    pair = None
    if decoded is not None:
        class _Timespec(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

        pair = (_Timespec * 2)(*(_Timespec(*value) for value in decoded))
        argument = ctypes.cast(pair, ctypes.c_void_p)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.futimens.argtypes = [ctypes.c_int, ctypes.c_void_p]
    libc.futimens.restype = ctypes.c_int
    ctypes.set_errno(0)
    if libc.futimens(fd, argument) != 0:
        error = ctypes.get_errno() or errno.EINVAL
        raise OSError(error, os.strerror(error))
