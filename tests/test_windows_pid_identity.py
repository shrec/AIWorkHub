"""Focused Windows PID identity evidence and fail-closed routing tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any, Callable, TypeVar

import pytest

from aiworkhub import process_identity, process_launcher


_T = TypeVar("_T")


class _FakeFunction:
    def __init__(self, callback: Any) -> None:
        self._callback = callback
        self.argtypes: list[object] = []
        self.restype: object = None

    def __call__(self, *args: object) -> Any:
        return self._callback(*args)


class _FakeKernel32:
    def __init__(
        self,
        *,
        open_results: list[tuple[int, int]],
        times_results: list[tuple[bool, int, int]],
        set_error: Callable[[int], None],
    ) -> None:
        self._open_results = list(open_results)
        self._times_results = list(times_results)
        self._set_error = set_error
        self.open_calls = 0
        self.times_calls = 0
        self.closed_handles: list[int] = []
        self.OpenProcess = _FakeFunction(self._open_process)
        self.GetProcessTimes = _FakeFunction(self._get_process_times)
        self.CloseHandle = _FakeFunction(self._close_handle)

    @staticmethod
    def _next_result(results: list[_T], index: int) -> _T:
        if not results:
            raise AssertionError("fake result sequence is empty")
        return results[min(index, len(results) - 1)]

    def _open_process(self, _access: object, _inherit: object, _pid: object) -> int:
        handle, winerror = self._next_result(self._open_results, self.open_calls)
        self.open_calls += 1
        self._set_error(winerror)
        return handle

    def _get_process_times(
        self,
        _handle: object,
        creation: object,
        _exit_time: object,
        _kernel: object,
        _user: object,
    ) -> int:
        ok, winerror, ticks = self._next_result(
            self._times_results,
            self.times_calls,
        )
        self.times_calls += 1
        self._set_error(winerror)
        if ok:
            creation._obj.low = ticks & 0xFFFFFFFF  # type: ignore[attr-defined]
            creation._obj.high = ticks >> 32  # type: ignore[attr-defined]
        return int(ok)

    def _close_handle(self, handle: object) -> int:
        self.closed_handles.append(int(handle))
        return 1


def _install_windows_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    open_results: list[tuple[int, int]],
    times_results: list[tuple[bool, int, int]] | None = None,
) -> tuple[_FakeKernel32, list[float]]:
    last_error = {"value": 0}
    sleep_calls: list[float] = []

    def set_last_error(value: int) -> None:
        last_error["value"] = int(value)

    kernel32 = _FakeKernel32(
        open_results=open_results,
        times_results=times_results or [],
        set_error=set_last_error,
    )
    monkeypatch.setattr(process_identity.os, "name", "nt")
    monkeypatch.setattr(
        process_launcher.ctypes,
        "WinDLL",
        lambda _name, use_last_error=True: kernel32,
        raising=False,
    )
    monkeypatch.setattr(
        process_launcher.ctypes, "set_last_error", set_last_error, raising=False,
    )
    monkeypatch.setattr(
        process_launcher.ctypes,
        "get_last_error",
        lambda: last_error["value"],
        raising=False,
    )
    monkeypatch.setattr(
        process_launcher.time,
        "sleep",
        lambda seconds: sleep_calls.append(float(seconds)),
    )
    return kernel32, sleep_calls


def test_verdict_is_exact_tri_state_and_evidence_is_immutable() -> None:
    assert [item.name for item in process_identity.PidIdentityVerdict] == [
        "MATCH",
        "MISMATCH",
        "UNKNOWN",
    ]
    evidence = process_identity.PidIdentityEvidence(
        verdict=process_identity.PidIdentityVerdict.UNKNOWN,
        pid=1,
        expected_start_ticks=2,
        observed_start_ticks=None,
        attempts=1,
        operation="OpenProcess",
    )
    with pytest.raises(FrozenInstanceError):
        evidence.attempts = 2  # type: ignore[misc]


def test_equal_creation_time_is_match_and_handle_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = 0x123456789
    kernel32, sleeps = _install_windows_probe(
        monkeypatch,
        open_results=[(101, 0)],
        times_results=[(True, 0, ticks)],
    )

    evidence = process_launcher._pid_identity_evidence(44, ticks)

    assert evidence.verdict is process_identity.PidIdentityVerdict.MATCH
    assert evidence.observed_start_ticks == ticks
    assert evidence.operation == "GetProcessTimes"
    assert evidence.attempts == 1
    assert evidence.winerror is None
    assert kernel32.closed_handles == [101]
    assert sleeps == []


def test_unequal_creation_time_is_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32, _sleeps = _install_windows_probe(
        monkeypatch,
        open_results=[(102, 0)],
        times_results=[(True, 0, 222)],
    )

    evidence = process_launcher._pid_identity_evidence(44, 111)

    assert evidence.verdict is process_identity.PidIdentityVerdict.MISMATCH
    assert evidence.observed_start_ticks == 222
    assert evidence.attempts == 1
    assert kernel32.closed_handles == [102]


def test_explicit_absence_is_mismatch_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32, sleeps = _install_windows_probe(
        monkeypatch,
        open_results=[(0, process_identity._WINDOWS_ERROR_INVALID_PARAMETER)],
    )

    evidence = process_launcher._pid_identity_evidence(44, 111)

    assert evidence.verdict is process_identity.PidIdentityVerdict.MISMATCH
    assert evidence.operation == "OpenProcess"
    assert evidence.winerror == process_identity._WINDOWS_ERROR_INVALID_PARAMETER
    assert evidence.exception == "ProcessAbsent"
    assert evidence.attempts == 1
    assert kernel32.closed_handles == []
    assert sleeps == []


@pytest.mark.parametrize("winerror", [5, 8, 1234])
def test_access_denied_resource_and_unclassified_open_errors_are_unknown(
    monkeypatch: pytest.MonkeyPatch,
    winerror: int,
) -> None:
    kernel32, sleeps = _install_windows_probe(
        monkeypatch,
        open_results=[(0, winerror)],
    )

    evidence = process_launcher._pid_identity_evidence(44, 111)

    assert evidence.verdict is process_identity.PidIdentityVerdict.UNKNOWN
    assert evidence.operation == "OpenProcess"
    assert evidence.winerror == winerror
    assert evidence.exception == "OpenProcessFailed"
    assert evidence.attempts == process_identity._PID_IDENTITY_MAX_ATTEMPTS
    assert kernel32.open_calls == process_identity._PID_IDENTITY_MAX_ATTEMPTS
    assert kernel32.closed_handles == []
    assert len(sleeps) == process_identity._PID_IDENTITY_MAX_ATTEMPTS - 1


def test_get_process_times_failure_is_unknown_and_closes_every_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32, sleeps = _install_windows_probe(
        monkeypatch,
        open_results=[(103, 0)],
        times_results=[(False, 6, 0)],
    )

    evidence = process_launcher._pid_identity_evidence(44, 111)

    assert evidence.verdict is process_identity.PidIdentityVerdict.UNKNOWN
    assert evidence.operation == "GetProcessTimes"
    assert evidence.winerror == 6
    assert evidence.exception == "GetProcessTimesFailed"
    assert evidence.attempts == process_identity._PID_IDENTITY_MAX_ATTEMPTS
    assert kernel32.closed_handles == [103, 103, 103]
    assert len(sleeps) == process_identity._PID_IDENTITY_MAX_ATTEMPTS - 1


def test_missing_and_malformed_expected_ticks_are_unknown() -> None:
    missing = process_launcher._pid_identity_evidence(44, None)
    malformed = process_launcher._pid_identity_evidence(44, "not-an-integer")

    assert missing.verdict is process_identity.PidIdentityVerdict.UNKNOWN
    assert missing.operation == "parse_expected_start_ticks"
    assert missing.attempts == 0
    assert malformed.verdict is process_identity.PidIdentityVerdict.UNKNOWN
    assert malformed.operation == "parse_expected_start_ticks"
    assert malformed.exception == "ValueError"


def test_transient_open_failure_then_match_uses_bounded_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = 444
    kernel32, sleeps = _install_windows_probe(
        monkeypatch,
        open_results=[(0, 5), (104, 0)],
        times_results=[(True, 0, ticks)],
    )

    evidence = process_launcher._pid_identity_evidence(44, ticks)

    assert evidence.verdict is process_identity.PidIdentityVerdict.MATCH
    assert evidence.attempts == 2
    assert kernel32.open_calls == 2
    assert kernel32.closed_handles == [104]
    assert sleeps == [process_identity._PID_IDENTITY_RETRY_DELAY_SECONDS]


def test_only_verified_and_proven_dead_helpers_route_through_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def result(verdict: process_identity.PidIdentityVerdict):
        return process_identity.PidIdentityEvidence(
            verdict=verdict,
            pid=44,
            expected_start_ticks=111,
            observed_start_ticks=111,
            attempts=1,
            operation="test",
        )

    monkeypatch.setattr(
        process_identity,
        "_pid_identity_evidence",
        lambda _pid, _ticks: result(process_identity.PidIdentityVerdict.MATCH),
    )
    assert process_launcher._identity_verified_pid(44, 111) == 44
    assert process_launcher._process_proven_dead(44, 111) is False

    monkeypatch.setattr(
        process_identity,
        "_pid_identity_evidence",
        lambda _pid, _ticks: result(process_identity.PidIdentityVerdict.UNKNOWN),
    )
    assert process_launcher._identity_verified_pid(44, 111) == 0
    assert process_launcher._process_proven_dead(44, 111) is False

    monkeypatch.setattr(
        process_identity,
        "_pid_identity_evidence",
        lambda _pid, _ticks: result(process_identity.PidIdentityVerdict.MISMATCH),
    )
    assert process_launcher._identity_verified_pid(44, 111) == 0
    assert process_launcher._process_proven_dead(44, 111) is True


def test_legacy_pid_matches_behavior_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_identity, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(process_identity, "_pid_start_ticks", lambda _pid: 111)

    assert process_launcher._pid_matches(44, 111) is True
    assert process_launcher._pid_matches(44, None) is True
    assert process_launcher._pid_matches(44, "malformed") is False

    monkeypatch.setattr(process_identity, "_pid_alive", lambda _pid: False)
    assert process_launcher._pid_matches(44, 111) is False


def test_the_launcher_re_exports_the_same_objects_identity_lives_in() -> None:
    """The extraction must be transparent to every existing caller.

    Re-export is not a copy: these must be the SAME objects, or a monkeypatch
    applied to one module would silently miss the code the other runs -- which
    is exactly how eight tests broke when the extraction first landed.
    """
    for name in (
        "PidIdentityEvidence",
        "PidIdentityVerdict",
        "_identity_verified_pid",
        "_pid_alive",
        "_pid_identity_evidence",
        "_pid_matches",
        "_pid_start_ticks",
        "_process_proven_dead",
    ):
        assert getattr(process_launcher, name) is getattr(process_identity, name), name
