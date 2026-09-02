"""Exact process identity: is THIS pid still the process we launched?

Extracted from ``process_launcher`` under the module-size ratchet, which is
descending by design: a 14,675-line module cannot absorb another fix, and this
region is one whole subject rather than a convenient slice. PID reuse is the
question it answers -- a live pid whose recorded start ticks differ is a
DIFFERENT process, and saying so is what keeps a reconciler from declaring a
stranger dead.

Every verdict is fail-closed: UNKNOWN when identity cannot be decided, never a
guess. Callers defer on UNKNOWN, so a wrong MATCH strands work and a wrong
MISMATCH kills a live worker -- both worse than waiting.

``process_launcher`` re-exports these names, so existing callers and tests that
reach for ``process_launcher._pid_identity_evidence`` keep resolving here.
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from . import runtime_temp
from .platform_io import process_is_alive


# Liveness is one function, imported -- never a private copy. A POSIX branch
# that read EPERM as DEAD declared a worker under another uid dead and
# terminalized it, while the Windows branch read access-denied as ALIVE.
# ``platform_io.process_is_alive`` gives every entry point the same honest
# answer (EPERM means alive).
_pid_alive = process_is_alive


def _pid_start_ticks(pid: int) -> int | None:
    """Read the canonical creation timestamp; ``None`` means unknown."""
    return runtime_temp.process_start_ticks(pid)


def _pid_matches(pid: int, expected_start_ticks: Any) -> bool:
    if not _pid_alive(pid):
        return False
    if expected_start_ticks in (None, ""):
        return True
    try:
        expected = int(expected_start_ticks)
    except (TypeError, ValueError):
        return False
    return _pid_start_ticks(pid) == expected


class PidIdentityVerdict(Enum):
    """Truthful result of an exact PID plus creation-time identity probe."""

    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PidIdentityEvidence:
    """Immutable evidence captured at the process-identity boundary."""

    verdict: PidIdentityVerdict
    pid: int | None
    expected_start_ticks: int | None
    observed_start_ticks: int | None
    attempts: int
    operation: str
    winerror: int | None = None
    exception: str = ""


_PID_IDENTITY_MAX_ATTEMPTS = 3
_PID_IDENTITY_RETRY_DELAY_SECONDS = 0.01
_WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WINDOWS_ERROR_INVALID_PARAMETER = 87


def _windows_pid_identity_once(
    pid: int,
    expected_start_ticks: int,
    *,
    attempt: int,
) -> PidIdentityEvidence:
    """Perform one Windows identity probe and capture failure provenance."""

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    try:
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        getattr(ctypes, "set_last_error")(0)
        handle = kernel32.OpenProcess(
            _WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror is None:
            winerror = int(getattr(ctypes, "get_last_error")()) or None
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.UNKNOWN,
            pid=pid,
            expected_start_ticks=expected_start_ticks,
            observed_start_ticks=None,
            attempts=attempt,
            operation="OpenProcess",
            winerror=winerror,
            exception=type(exc).__name__,
        )

    if not handle:
        winerror = int(getattr(ctypes, "get_last_error")()) or None
        absent = winerror == _WINDOWS_ERROR_INVALID_PARAMETER
        return PidIdentityEvidence(
            verdict=(
                PidIdentityVerdict.MISMATCH
                if absent
                else PidIdentityVerdict.UNKNOWN
            ),
            pid=pid,
            expected_start_ticks=expected_start_ticks,
            observed_start_ticks=None,
            attempts=attempt,
            operation="OpenProcess",
            winerror=winerror,
            exception="ProcessAbsent" if absent else "OpenProcessFailed",
        )

    creation = _FileTime()
    exit_time = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    try:
        try:
            getattr(ctypes, "set_last_error")(0)
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if winerror is None:
                winerror = int(getattr(ctypes, "get_last_error")()) or None
            return PidIdentityEvidence(
                verdict=PidIdentityVerdict.UNKNOWN,
                pid=pid,
                expected_start_ticks=expected_start_ticks,
                observed_start_ticks=None,
                attempts=attempt,
                operation="GetProcessTimes",
                winerror=winerror,
                exception=type(exc).__name__,
            )
        if not ok:
            winerror = int(getattr(ctypes, "get_last_error")()) or None
            return PidIdentityEvidence(
                verdict=PidIdentityVerdict.UNKNOWN,
                pid=pid,
                expected_start_ticks=expected_start_ticks,
                observed_start_ticks=None,
                attempts=attempt,
                operation="GetProcessTimes",
                winerror=winerror,
                exception="GetProcessTimesFailed",
            )
        observed = (int(creation.high) << 32) | int(creation.low)
        return PidIdentityEvidence(
            verdict=(
                PidIdentityVerdict.MATCH
                if observed == expected_start_ticks
                else PidIdentityVerdict.MISMATCH
            ),
            pid=pid,
            expected_start_ticks=expected_start_ticks,
            observed_start_ticks=observed,
            attempts=attempt,
            operation="GetProcessTimes",
        )
    finally:
        kernel32.CloseHandle(handle)


def _pid_identity_evidence(pid: Any, expected_start_ticks: Any) -> PidIdentityEvidence:
    """Return bounded, fail-closed PID identity evidence on every platform."""

    try:
        numeric_pid = int(pid or 0)
    except (TypeError, ValueError) as exc:
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.UNKNOWN,
            pid=None,
            expected_start_ticks=None,
            observed_start_ticks=None,
            attempts=0,
            operation="parse_pid",
            exception=type(exc).__name__,
        )
    if numeric_pid <= 0:
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.MISMATCH,
            pid=numeric_pid,
            expected_start_ticks=None,
            observed_start_ticks=None,
            attempts=0,
            operation="pid_absent",
            exception="NonPositivePid",
        )
    if expected_start_ticks in (None, ""):
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.UNKNOWN,
            pid=numeric_pid,
            expected_start_ticks=None,
            observed_start_ticks=None,
            attempts=0,
            operation="parse_expected_start_ticks",
            exception="ExpectedStartTicksMissing",
        )
    try:
        expected = int(expected_start_ticks)
    except (TypeError, ValueError) as exc:
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.UNKNOWN,
            pid=numeric_pid,
            expected_start_ticks=None,
            observed_start_ticks=None,
            attempts=0,
            operation="parse_expected_start_ticks",
            exception=type(exc).__name__,
        )

    if os.name == "nt":
        evidence: PidIdentityEvidence | None = None
        for attempt in range(1, _PID_IDENTITY_MAX_ATTEMPTS + 1):
            evidence = _windows_pid_identity_once(
                numeric_pid,
                expected,
                attempt=attempt,
            )
            if evidence.verdict is not PidIdentityVerdict.UNKNOWN:
                return evidence
            if attempt < _PID_IDENTITY_MAX_ATTEMPTS:
                time.sleep(_PID_IDENTITY_RETRY_DELAY_SECONDS)
        assert evidence is not None
        return evidence

    try:
        os.kill(numeric_pid, 0)
    except ProcessLookupError as exc:
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.MISMATCH,
            pid=numeric_pid,
            expected_start_ticks=expected,
            observed_start_ticks=None,
            attempts=1,
            operation="kill_zero",
            exception=type(exc).__name__,
        )
    except PermissionError as exc:
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.UNKNOWN,
            pid=numeric_pid,
            expected_start_ticks=expected,
            observed_start_ticks=None,
            attempts=1,
            operation="kill_zero",
            exception=type(exc).__name__,
        )
    observed = _pid_start_ticks(numeric_pid)
    if observed is None:
        return PidIdentityEvidence(
            verdict=PidIdentityVerdict.UNKNOWN,
            pid=numeric_pid,
            expected_start_ticks=expected,
            observed_start_ticks=None,
            attempts=1,
            operation="_pid_start_ticks",
            exception="StartTicksUnavailable",
        )
    return PidIdentityEvidence(
        verdict=(
            PidIdentityVerdict.MATCH
            if observed == expected
            else PidIdentityVerdict.MISMATCH
        ),
        pid=numeric_pid,
        expected_start_ticks=expected,
        observed_start_ticks=observed,
        attempts=1,
        operation="_pid_start_ticks",
    )
