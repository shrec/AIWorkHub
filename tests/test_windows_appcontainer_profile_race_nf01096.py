"""Regression tests for the CreateAppContainerProfile race in derive_identity.

A concurrent reviewer launch can race CreateAppContainerProfile for the same
profile name; the loser previously received a non-ALREADY_EXISTS HRESULT and
failed outright instead of deriving the winner's identity. These tests drive
``_CtypesWin32Api.derive_identity`` against a fake ``_userenv`` so they run on
any OS without touching real Win32 APIs.
"""

from __future__ import annotations

import pytest

from aiworkhub import windows_appcontainer as wac


class _FakeUserenv:
    """Fake replacement for the userenv DLL boundary."""

    def __init__(self, create_results, derive_results=()):
        self._create_results = list(create_results)
        self._derive_results = list(derive_results)
        self.create_calls = 0
        self.derive_calls = 0

    def CreateAppContainerProfile(
        self, name, display_name, description, reserved, count, sid_ptr
    ):
        self.create_calls += 1
        return self._create_results.pop(0)

    def DeriveAppContainerSidFromAppContainerName(self, name, sid_ptr):
        self.derive_calls += 1
        return self._derive_results.pop(0)


class _FakeApi:
    """Stand-in for ``self`` inside derive_identity: no real Win32 calls."""

    def __init__(self, userenv):
        self._userenv = userenv

    def _sid_to_string(self, sid):
        return "S-1-15-2-fake"


def _patch_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(
        wac.time, "sleep", lambda seconds: calls.append(seconds)
    )
    return calls


def test_already_exists_path_is_unchanged(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    userenv = _FakeUserenv(
        create_results=[wac._HRESULT_ALREADY_EXISTS], derive_results=[0]
    )
    api = _FakeApi(userenv)

    identity = wac._CtypesWin32Api.derive_identity(api, "name", "disp", "desc")

    assert identity.created_profile is False
    assert identity.sid_string == "S-1-15-2-fake"
    assert userenv.create_calls == 1
    assert userenv.derive_calls == 1
    assert sleeps == []


def test_success_path_is_unchanged(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    userenv = _FakeUserenv(create_results=[0])
    api = _FakeApi(userenv)

    identity = wac._CtypesWin32Api.derive_identity(api, "name", "disp", "desc")

    assert identity.created_profile is True
    assert userenv.create_calls == 1
    assert userenv.derive_calls == 0
    assert sleeps == []


def test_race_then_derive_success_returns_created_false(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    userenv = _FakeUserenv(create_results=[-1], derive_results=[0])
    api = _FakeApi(userenv)

    identity = wac._CtypesWin32Api.derive_identity(api, "name", "disp", "desc")

    assert identity.created_profile is False
    assert identity.sid_string == "S-1-15-2-fake"
    assert userenv.create_calls == 1
    assert userenv.derive_calls == 1
    assert sleeps == []


def test_transient_failure_then_success_returns_created_true(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    userenv = _FakeUserenv(create_results=[-1, 0], derive_results=[-10])
    api = _FakeApi(userenv)

    identity = wac._CtypesWin32Api.derive_identity(api, "name", "disp", "desc")

    assert identity.created_profile is True
    assert userenv.create_calls == 2
    assert userenv.derive_calls == 1
    assert len(sleeps) == 1
    assert all(0.05 <= s <= 0.2 for s in sleeps)


def test_persistent_failure_raises_original_create_hr_after_three_attempts(
    monkeypatch,
):
    sleeps = _patch_sleep(monkeypatch)
    userenv = _FakeUserenv(
        create_results=[-1, -2, -3], derive_results=[-10, -20, -30]
    )
    api = _FakeApi(userenv)

    with pytest.raises(wac._Win32Failure) as excinfo:
        wac._CtypesWin32Api.derive_identity(api, "name", "disp", "desc")

    failure = excinfo.value
    assert failure.operation == "create_appcontainer_profile"
    assert failure.win_error == (-1 & 0xFFFF)
    assert userenv.create_calls == 3
    assert userenv.derive_calls == 3
    assert len(sleeps) == 2
    assert all(0.05 <= s <= 0.2 for s in sleeps)
