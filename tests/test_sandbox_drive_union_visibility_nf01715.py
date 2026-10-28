"""NF-2026-01715: a concurrent launcher's reconcile must never unmap a live
validation's sandbox drive.

Measured (requests 65ae8628a37240b3b0b326eb082db63a, 95ee60dd7e5248a2b9ea737c1d8cf47b):
xdist gw0 collected from ``Z:\\union2_...\\worktree`` while the other workers'
``os.stat`` of the same path raised WinError 3, the run sat beside another
card's validation, and afterwards no mapping was left.  Every request, union
tree included, lives under the one sandbox root, so every launch stacks an
identical definition on the same letter.  ``_SandboxDriveLeases.reconcile``
popped one whenever this session's definitions outnumbered its *store's* live
leases -- but a push leased in another store, or not leased at all, is in the
first count and not the second.  Reclaiming a dead lease whose definition was
already gone therefore took a live launch's definition; the letter vanished
(or was handed to another root) when the last counted owner released it,
while the uncounted validation's children were still running.

Fakes only: no DefineDosDeviceW, runs on any host.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import windows_appcontainer as wac

_ROOT = "c:\\hub\\.aiworkhub\\runtime\\worktrees"
_OTHER_ROOT = "d:\\other\\.aiworkhub\\runtime\\worktrees"
_ME = os.getpid()
_MY_START = 1000
_GONE = 4242
_GONE_START = 7


def _target(root: str) -> str:
    return "\\??\\" + root


class _SessionKernel:
    """One logon session's kernel32, as far as sandbox drives touch it.

    Named mutexes are refcounted objects that die with their last handle,
    and creating an existing one reports ERROR_ALREADY_EXISTS.  DefineDosDeviceW
    is a per-letter stack: a plain define pushes, a remove pops its exact
    target or fails.  QueryDosDeviceW reports the whole stack, top first.
    """

    def __init__(self) -> None:
        self.devices: dict[str, list[str]] = {}
        self.objects: dict[str, int] = {}
        self.handles: dict[int, str] = {}
        self.pops: list[tuple[str, str]] = []
        self.last_error = 0
        self.refuse_use_mutex = False
        self._next = 100

    def CreateMutexW(self, _attributes, _owner, name):
        if self.refuse_use_mutex and name.startswith(wac._SANDBOX_DRIVE_USE_PREFIX):
            self.last_error = 5
            return 0
        self.last_error = wac._ERROR_ALREADY_EXISTS if self.objects.get(name) else 0
        self.objects[name] = self.objects.get(name, 0) + 1
        self._next += 1
        self.handles[self._next] = name
        return self._next

    def WaitForSingleObject(self, _handle, _wait_ms):
        return 0

    def ReleaseMutex(self, _handle):
        return 1

    def CloseHandle(self, handle):
        name = self.handles.pop(handle)
        self.objects[name] -= 1
        if not self.objects[name]:
            del self.objects[name]
        return 1

    def GetLogicalDrives(self):
        return 0

    def QueryDosDeviceW(self, name, buffer, size):
        stack = self.devices.get(name) or []
        blob = "\0".join(reversed(stack)) + "\0\0"
        if not stack or len(blob) > size:
            self.last_error = 2
            return 0
        buffer[: len(blob)] = blob
        return len(blob)

    def DefineDosDeviceW(self, flags, name, target):
        stack = self.devices.setdefault(name, [])
        definition = _target(target)
        if not flags & 0x2:
            stack.append(definition)
            return True
        if definition not in stack:
            self.last_error = 2
            return False
        stack.remove(definition)
        self.pops.append((name, target))
        return True


@pytest.fixture
def kernel(monkeypatch) -> _SessionKernel:
    kernel = _SessionKernel()
    monkeypatch.setattr(wac, "_last_win_error", lambda: kernel.last_error)
    alive = {_ME: _MY_START}
    monkeypatch.setattr(
        wac,
        "_runtime_temp",
        lambda: SimpleNamespace(
            process_start_ticks=lambda pid: alive.get(pid),
            _recorded_process_alive=lambda pid, ticks: alive.get(pid) == ticks,
        ),
    )
    return kernel


def _boundary(kernel: _SessionKernel, leases: wac._SandboxDriveLeases):
    api = wac._CtypesWin32Api.__new__(wac._CtypesWin32Api)
    api._kernel32 = kernel
    api._sandbox_drive_leases = leases
    return api


def _ghost(directory: Path, drive: str, root: str) -> Path:
    """A dead launcher's lease whose definition its release already popped."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{secrets.token_hex(16)}.lease"
    path.write_bytes(wac._DriveLease(drive, root, _GONE, _GONE_START).encode())
    return path


@pytest.fixture(params=["leased_in_another_store", "store_unavailable"])
def validation_leases(request, tmp_path) -> wac._SandboxDriveLeases:
    """The running validation's own store: one the reconciler cannot see."""
    if request.param == "store_unavailable":
        return wac._SandboxDriveLeases(None, "sandbox drives are not leased: test")
    return wac._SandboxDriveLeases(tmp_path / "validation-store")


def test_a_concurrent_reconcile_keeps_the_running_validations_drive(
    kernel, validation_leases, tmp_path
):
    store = wac._SandboxDriveLeases(tmp_path / "launcher-store")
    ghost = _ghost(store.directory, "Z:", _ROOT)
    validation = _boundary(kernel, validation_leases)
    assert validation.sandbox_drive(_ROOT) == "Z:"

    # Another card's launch, beside it: its reconcile finds the ghost.
    beside = _boundary(kernel, store)
    assert beside.sandbox_drive(_ROOT) == "Z:"
    assert kernel.devices["Z:"] == [_target(_ROOT)] * 2
    assert kernel.pops == []
    assert ghost.exists()

    beside.release_sandbox_drive("Z:", _ROOT)
    # The validation's command and every xdist worker still see their root.
    assert kernel.devices["Z:"] == [_target(_ROOT)]

    validation.release_sandbox_drive("Z:", _ROOT)
    assert kernel.devices["Z:"] == []
    assert kernel.objects == {}
    assert validation.sandbox_drive_lease_issues == validation_leases.issues

    # The first launch that finds the pair unused reclaims the ghost; its
    # definition is long gone, so nothing is popped.
    later = _boundary(kernel, store)
    assert later.sandbox_drive(_ROOT) == "Z:"
    assert not ghost.exists()
    assert kernel.pops == [("Z:", _ROOT), ("Z:", _ROOT)]
    assert kernel.devices["Z:"] == [_target(_ROOT)]
    assert store.issues == []


def test_the_letter_is_never_handed_to_another_root_under_a_live_validation(
    kernel, validation_leases, tmp_path
):
    store = wac._SandboxDriveLeases(tmp_path / "launcher-store")
    ghost = _ghost(store.directory, "Z:", _ROOT)
    validation = _boundary(kernel, validation_leases)
    assert validation.sandbox_drive(_ROOT) == "Z:"

    other = _boundary(kernel, store)
    assert other.sandbox_drive(_OTHER_ROOT) == "Y:"

    assert kernel.devices["Z:"] == [_target(_ROOT)]
    assert kernel.devices["Y:"] == [_target(_OTHER_ROOT)]
    assert ghost.exists()


def test_a_dead_launchers_push_is_still_reclaimed_when_nothing_uses_it(kernel, tmp_path):
    store = wac._SandboxDriveLeases(tmp_path / "launcher-store")
    ghost = _ghost(store.directory, "Z:", _ROOT)
    kernel.devices["Z:"] = [_target(_ROOT)]  # the dead launcher's own push

    launch = _boundary(kernel, store)
    assert launch.sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops == [("Z:", _ROOT)]
    assert not ghost.exists()
    assert kernel.devices["Z:"] == [_target(_ROOT)]
    launch.release_sandbox_drive("Z:", _ROOT)
    assert kernel.devices["Z:"] == []
    assert kernel.objects == {}
    assert store.issues == []


def test_a_use_probe_that_cannot_answer_keeps_the_lease(kernel, tmp_path):
    store = wac._SandboxDriveLeases(tmp_path / "launcher-store")
    ghost = _ghost(store.directory, "Z:", _ROOT)
    kernel.devices["Z:"] = [_target(_ROOT)]
    kernel.refuse_use_mutex = True

    launch = _boundary(kernel, store)
    assert launch.sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops == []
    assert ghost.exists()
    assert kernel.devices["Z:"] == [_target(_ROOT)] * 2
    assert any("use not held" in issue for issue in store.issues)


def test_a_push_whose_hold_failed_never_closes_a_live_pushs_handle(kernel, tmp_path):
    launch = _boundary(kernel, wac._SandboxDriveLeases(tmp_path / "launcher-store"))
    kernel.refuse_use_mutex = True
    assert launch.sandbox_drive(_ROOT) == "Z:"  # this push holds no use mutex
    kernel.refuse_use_mutex = False
    assert launch.sandbox_drive(_ROOT) == "Z:"

    # Whichever push releases first, the one still live keeps the pair in use.
    launch.release_sandbox_drive("Z:", _ROOT)
    assert kernel.objects == {wac._sandbox_drive_use_name("Z:", _ROOT): 1}
    launch.release_sandbox_drive("Z:", _ROOT)
    assert kernel.objects == {}
    assert kernel.devices["Z:"] == []
