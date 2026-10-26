"""NF-2026-01695: a crashed launcher's stacked sandbox-drive definition is
reclaimed by the next sandbox_drive, not leaked until sign-out.

Every push of ``_CtypesWin32Api.sandbox_drive`` leases its definition -- drive,
real root, and the owner's pid and start identity -- in a per-user store, and
``release_sandbox_drive`` deletes that lease after its pop.  The next
sandbox_drive, under the same session mutex, pops one definition for each lease
whose owner is dead or whose pid now names another process, and deletes it.

The kernel here is a fake that stacks DefineDosDeviceW definitions per letter,
as the real OS does.  Liveness and start identity are runtime_temp's, faked per
test, except in the one test that reads a real, exited process.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import aiworkhub.windows_appcontainer as wac
from aiworkhub import runtime_temp

_ROOT = r"d:\repo\.aiworkhub\runtime\worktrees"
_OTHER_ROOT = r"e:\work\other\.aiworkhub\runtime\worktrees"
_PUSH = 0x8  # DDD_NO_BROADCAST_SYSTEM
_POP = 0x8 | 0x2 | 0x4  # ... | DDD_REMOVE_DEFINITION | DDD_EXACT_MATCH_ON_REMOVE
_ME = os.getpid()
_MY_START = 1_000_001
_GONE, _GONE_START = 4242, 111


class _DosKernel:
    """kernel32's mutex and DOS-device calls, recorded.

    DefineDosDeviceW is a per-letter stack, as on the real OS: a plain define
    pushes, and one carrying DDD_REMOVE_DEFINITION pops its exact target or
    fails.  QueryDosDeviceW reports the whole stack, top first, as a multi-sz.
    """

    def __init__(self) -> None:
        self.devices: dict[str, list[str]] = {}
        self.calls: list[tuple[object, ...]] = []
        self.refuse_pops = False

    def CreateMutexW(self, _attributes, _owner, name):
        self.calls.append(("mutex", name))
        return 77

    def WaitForSingleObject(self, _handle, _wait_ms):
        return 0

    def ReleaseMutex(self, handle):
        self.calls.append(("release", handle))
        return 1

    def CloseHandle(self, _handle):
        return 1

    def GetLogicalDrives(self):
        return 0

    def QueryDosDeviceW(self, name, buffer, size):
        stack = self.devices.get(name) or []
        blob = "\0".join(reversed(stack)) + "\0\0"
        if not stack or len(blob) > size:
            return 0
        buffer[: len(blob)] = blob
        return len(blob)

    def DefineDosDeviceW(self, flags, name, target):
        self.calls.append(("define", flags, name, target))
        stack = self.devices.setdefault(name, [])
        definition = "\\??\\" + target
        if not flags & 0x2:
            stack.append(definition)
            return True
        if self.refuse_pops or definition not in stack:
            return False
        stack.remove(definition)
        return True

    def pops(self) -> list[tuple[object, ...]]:
        return [call[2:] for call in self.calls if call[:2] == ("define", _POP)]


@pytest.fixture
def processes(monkeypatch):
    """The live processes, pid -> start ticks (None: unreadable). Any other pid is dead."""
    live: dict[int, int | None] = {_ME: _MY_START}
    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: pid in live)
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: live.get(pid))
    return live


@pytest.fixture
def store(tmp_path):
    return wac._SandboxDriveLeases(tmp_path / "leases")


def _boundary(kernel, leases):
    """The real ctypes boundary over ``kernel``, keeping its leases in ``leases``."""
    api = wac._CtypesWin32Api.__new__(wac._CtypesWin32Api)
    api._kernel32 = kernel
    api._sandbox_drive_leases = leases
    return api


def _plant(directory: Path, data: bytes, name: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name or f"{os.urandom(16).hex()}.lease")
    path.write_bytes(data)
    return path


def _pushed(kernel, directory, drive, root, pid, start) -> Path:
    """A launcher's push and its lease, exactly as its sandbox_drive left them."""
    kernel.devices.setdefault(drive, []).append("\\??\\" + root)
    return _plant(directory, wac._DriveLease(drive, root, pid, start).encode())


def _lease_json(**fields) -> bytes:
    lease = {
        "schema": "aiworkhub.sandbox_drive_lease.v1",
        "drive": "Z:",
        "real_root": _ROOT,
        "pid": _GONE,
        "start_ticks": _GONE_START,
    }
    lease.update(fields)
    return json.dumps(lease).encode()


def test_the_lease_names_the_definition_and_its_owner():
    lease = wac._DriveLease("Z:", _ROOT, _GONE, _GONE_START)
    assert json.loads(lease.encode()) == json.loads(_lease_json())
    assert wac._DriveLease.decode(_lease_json()) == lease


def test_a_dead_launchers_push_is_reclaimed_by_the_next_sandbox_drive(store, processes):
    kernel = _DosKernel()
    left = _pushed(kernel, store.directory, "Z:", _ROOT, _GONE, _GONE_START)

    assert _boundary(kernel, store).sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops() == [("Z:", _ROOT)]
    assert not left.exists()
    # Reclaimed under the session mutex, before this launch's own push.
    pop = kernel.calls.index(("define", _POP, "Z:", _ROOT))
    assert kernel.calls[0] == ("mutex", wac._SANDBOX_DRIVE_MUTEX)
    assert pop < kernel.calls.index(("define", _PUSH, "Z:", _ROOT))
    assert pop < kernel.calls.index(("release", 77))
    assert kernel.devices["Z:"] == ["\\??\\" + _ROOT]
    (mine,) = store.directory.iterdir()
    assert wac._DriveLease.decode(mine.read_bytes()) == wac._DriveLease(
        "Z:", _ROOT, _ME, _MY_START
    )
    assert store.issues == []


def test_a_live_owner_is_never_popped(store, processes):
    kernel = _DosKernel()
    processes[_GONE] = _GONE_START  # same pid, same start: still running
    sibling = _pushed(kernel, store.directory, "Z:", _ROOT, _GONE, _GONE_START)

    assert _boundary(kernel, store).sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops() == []
    assert sibling.exists()
    assert len(kernel.devices["Z:"]) == 2


def test_a_reused_pid_counts_as_dead(store, processes):
    kernel = _DosKernel()
    processes[_GONE] = _GONE_START + 1  # the pid now names a later process
    left = _pushed(kernel, store.directory, "Z:", _ROOT, _GONE, _GONE_START)

    assert _boundary(kernel, store).sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops() == [("Z:", _ROOT)]
    assert not left.exists()
    assert kernel.devices["Z:"] == ["\\??\\" + _ROOT]


def test_an_unreadable_start_identity_is_never_proof_of_death(store, processes):
    kernel = _DosKernel()
    processes[_GONE] = None  # alive, but its start cannot be read
    kept = _pushed(kernel, store.directory, "Z:", _ROOT, _GONE, _GONE_START)

    assert _boundary(kernel, store).sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops() == []
    assert kept.exists()


def test_an_unanswerable_liveness_probe_keeps_the_lease(store, processes, monkeypatch):
    def unanswerable(_pid):
        raise OSError("liveness probe unavailable")

    monkeypatch.setattr(runtime_temp, "_pid_alive", unanswerable)
    kernel = _DosKernel()
    kept = _pushed(kernel, store.directory, "Z:", _ROOT, _GONE, _GONE_START)

    assert _boundary(kernel, store).sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops() == []
    assert kept.exists()
    assert any("liveness probe unavailable" in issue for issue in store.issues)


def test_a_real_exited_process_is_dead_and_this_one_alive(store):
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as child:
        try:
            start = runtime_temp.process_start_ticks(child.pid)
        finally:
            child.kill()
            child.wait()
    assert start
    kernel = _DosKernel()
    gone = _pushed(kernel, store.directory, "Z:", _ROOT, child.pid, start)
    me = _pushed(
        kernel, store.directory, "Y:", _OTHER_ROOT, _ME, runtime_temp.process_start_ticks(_ME)
    )

    assert _boundary(kernel, store).sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops() == [("Z:", _ROOT)]
    assert not gone.exists()
    assert me.exists()
    assert kernel.devices["Y:"] == ["\\??\\" + _OTHER_ROOT]


_MALFORMED_OR_FOREIGN = {
    "not_json": b"not json",
    "not_utf8": b"\xff\xfe\x00",
    "not_an_object": b"[]",
    "nested_too_deep": b"[" * 3000,
    "other_schema": _lease_json(schema="aiworkhub.sandbox_drive_lease.v0"),
    "missing_field": json.dumps(
        {k: v for k, v in json.loads(_lease_json()).items() if k != "start_ticks"}
    ).encode(),
    "extra_field": _lease_json(note="hello"),
    "system_drive": _lease_json(drive="C:"),
    "lowercase_drive": _lease_json(drive="z:"),
    "drive_without_colon": _lease_json(drive="Z"),
    "not_a_sandbox_root": _lease_json(real_root="c:\\windows"),
    "relative_root": _lease_json(real_root="repo\\.aiworkhub\\runtime\\worktrees"),
    "unc_root": _lease_json(real_root="\\\\server\\share\\.aiworkhub\\runtime\\worktrees"),
    "slashed_root": _lease_json(real_root="d:/repo/.aiworkhub/runtime/worktrees"),
    "pid_as_text": _lease_json(pid="4242"),
    "pid_as_bool": _lease_json(pid=True),
    "pid_zero": _lease_json(pid=0),
    "start_zero": _lease_json(start_ticks=0),
    "start_as_float": _lease_json(start_ticks=111.0),
    "oversized": _lease_json() + b" " * 5000,
}


@pytest.mark.parametrize("content", _MALFORMED_OR_FOREIGN.values(), ids=_MALFORMED_OR_FOREIGN)
def test_a_malformed_or_foreign_lease_is_skipped_without_a_pop(store, processes, content):
    kernel = _DosKernel()
    kernel.devices["Z:"] = ["\\??\\" + _ROOT]  # what a bad lease could aim at
    planted = _plant(store.directory, content)

    assert _boundary(kernel, store).sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops() == []
    assert planted.exists()
    assert len(kernel.devices["Z:"]) == 2
    assert any("malformed or foreign" in issue for issue in store.issues)


def test_entries_that_are_not_lease_files_are_skipped(store, processes):
    kernel = _DosKernel()
    kernel.devices["Z:"] = ["\\??\\" + _ROOT]
    misnamed = _plant(store.directory, _lease_json(), name="lease.json")
    folder = store.directory / f"{'b' * 32}.lease"
    folder.mkdir()

    assert _boundary(kernel, store).sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops() == []
    assert misnamed.exists()
    assert folder.is_dir()


def test_a_linked_lease_is_skipped(store, processes, tmp_path):
    kernel = _DosKernel()
    kernel.devices["Z:"] = ["\\??\\" + _ROOT]
    target = _plant(tmp_path / "elsewhere", _lease_json())
    store.directory.mkdir()
    link = store.directory / f"{'c' * 32}.lease"
    try:
        link.symlink_to(target)
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")

    assert _boundary(kernel, store).sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops() == []
    assert target.exists()


def test_release_deletes_its_own_lease_and_keeps_the_push_pop_pairing(store, processes):
    kernel = _DosKernel()
    api = _boundary(kernel, store)

    assert api.sandbox_drive(_ROOT) == "Z:"
    assert api.sandbox_drive(_ROOT) == "Z:"
    assert len(list(store.directory.iterdir())) == 2

    api.release_sandbox_drive("Z:", _ROOT)
    assert len(list(store.directory.iterdir())) == 1
    api.release_sandbox_drive("Z:", _ROOT)
    assert list(store.directory.iterdir()) == []

    assert kernel.devices["Z:"] == []
    defines = [call for call in kernel.calls if call[0] == "define"]
    assert defines == [("define", _PUSH, "Z:", _ROOT)] * 2 + [("define", _POP, "Z:", _ROOT)] * 2
    assert kernel.calls.count(("release", 77)) == 4
    assert store.issues == []


def test_a_failed_pop_keeps_its_lease_until_the_next_reconcile(store, processes):
    kernel = _DosKernel()
    api = _boundary(kernel, store)
    assert api.sandbox_drive(_ROOT) == "Z:"
    (lease,) = store.directory.iterdir()

    kernel.refuse_pops = True
    with pytest.raises(wac._Win32Failure):
        api.release_sandbox_drive("Z:", _ROOT)
    assert lease.exists()

    kernel.refuse_pops = False
    del processes[_ME]  # that launcher is gone now
    assert _boundary(kernel, wac._SandboxDriveLeases(store.directory)).sandbox_drive(_ROOT) == "Z:"
    assert not lease.exists()
    assert kernel.devices["Z:"] == ["\\??\\" + _ROOT]


def test_a_definition_already_gone_is_never_taken_from_a_live_owner(store, processes):
    """A dead lease whose definition this session no longer holds -- sign-out,
    reboot, another logon session, a crash right after a release's pop -- is
    deleted without a pop while a live owner holds the same root."""
    kernel = _DosKernel()
    processes[5151] = 222
    sibling = _pushed(kernel, store.directory, "Z:", _ROOT, 5151, 222)
    stale = _plant(store.directory, wac._DriveLease("Z:", _ROOT, _GONE, _GONE_START).encode())

    assert _boundary(kernel, store).sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops() == []
    assert not stale.exists()
    assert sibling.exists()
    assert len(kernel.devices["Z:"]) == 2


@pytest.mark.parametrize("missed", ["past_the_entry_limit", "unreadable"])
def test_a_scan_that_misses_a_live_lease_reclaims_nothing(store, processes, monkeypatch, missed):
    """A live owner's lease the scan did not read never lets a dead lease
    whose definition is already gone pop that owner's definition."""
    kernel = _DosKernel()
    processes[5151] = 222
    kernel.devices["Z:"] = ["\\??\\" + _ROOT]  # 5151's live push
    dead = wac._DriveLease("Z:", _ROOT, _GONE, _GONE_START).encode()
    stale = _plant(store.directory, dead, name="0" * 32 + ".lease")
    owner = wac._DriveLease("Z:", _ROOT, 5151, 222).encode()
    live = _plant(store.directory, owner, name="f" * 32 + ".lease")
    if missed == "past_the_entry_limit":
        monkeypatch.setattr(wac, "_DRIVE_LEASE_LIMIT", 1)
    else:
        read = wac._SandboxDriveLeases._read

        def unreadable(path):
            if path.name == live.name:
                raise PermissionError("sharing violation")
            return read(path)

        monkeypatch.setattr(wac._SandboxDriveLeases, "_read", staticmethod(unreadable))

    assert _boundary(kernel, store).sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops() == []
    assert stale.exists() and live.exists()
    assert len(kernel.devices["Z:"]) == 2
    assert any("reconcile deferred" in issue for issue in store.issues)


def test_a_failed_reclaim_pop_is_recorded_and_the_launch_goes_on(store, processes):
    kernel = _DosKernel()
    left = _pushed(kernel, store.directory, "Z:", _ROOT, _GONE, _GONE_START)
    kernel.refuse_pops = True

    assert _boundary(kernel, store).sandbox_drive(_ROOT) == "Z:"

    assert kernel.pops() == [("Z:", _ROOT)]
    assert not left.exists()
    assert any("pop Z:" in issue for issue in store.issues)


def test_an_unusable_store_never_blocks_the_launch(tmp_path, processes):
    blocker = tmp_path / "leases"
    blocker.write_text("a file where the store should be")
    leases = wac._SandboxDriveLeases(blocker)
    kernel = _DosKernel()

    assert _boundary(kernel, leases).sandbox_drive(_ROOT) == "Z:"

    assert kernel.devices["Z:"] == ["\\??\\" + _ROOT]
    assert len(leases.issues) == 2  # the reconcile skipped, the push not leased


def test_the_store_is_per_user_local_appdata(tmp_path, monkeypatch):
    # Never under tmp_path: a validation lane's temp may itself lie in a sandbox root.
    local = Path(tmp_path.anchor) / "Users" / "someone" / "AppData" / "Local"
    monkeypatch.setattr(wac, "resolve_local_appdata", lambda: str(local))

    leases = wac._SandboxDriveLeases.for_current_user()

    assert leases.directory == local / "AIWorkHub" / "sandbox-drive-leases"
    assert leases.issues == []


def test_without_a_per_user_store_the_drive_is_still_mapped(monkeypatch, processes):
    monkeypatch.setattr(wac, "resolve_local_appdata", lambda: "")
    leases = wac._SandboxDriveLeases.for_current_user()
    assert leases.directory is None
    kernel = _DosKernel()

    assert _boundary(kernel, leases).sandbox_drive(_ROOT) == "Z:"

    assert kernel.devices["Z:"] == ["\\??\\" + _ROOT]
    assert len(leases.issues) == 1


def test_a_store_inside_a_sandbox_root_is_refused(tmp_path, monkeypatch):
    sandbox = tmp_path / "repo" / ".aiworkhub" / "runtime" / "worktrees"
    local = sandbox / "request" / "home" / "AppData" / "Local"
    monkeypatch.setattr(wac, "resolve_local_appdata", lambda: str(local))

    leases = wac._SandboxDriveLeases.for_current_user()

    assert leases.directory is None
    assert len(leases.issues) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows inherits LocalAppData's DACL")
def test_the_store_and_its_leases_are_owner_only(store, processes):
    assert _boundary(_DosKernel(), store).sandbox_drive(_ROOT) == "Z:"

    (lease,) = store.directory.iterdir()
    assert stat.S_IMODE(store.directory.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(lease.stat().st_mode) & 0o077 == 0


def test_lease_issues_are_part_of_the_launch_evidence():
    leases = wac._SandboxDriveLeases(None, "sandbox drives are not leased: probe")
    launch = wac.AppContainerLaunch(
        pid=1,
        process_id=1,
        thread_id=2,
        container_name="aiworkhub.probe",
        container_sid="S-1-15-2-1",
        creation_identity="probe",
        command_line="probe",
        api=_boundary(_DosKernel(), leases),
        job=None,
        creation=wac._ProcessCreation(1, 2, None, None),
    )

    assert launch.cleanup_evidence()["sandbox_drive_lease_issues"] == [
        "sandbox drives are not leased: probe"
    ]
