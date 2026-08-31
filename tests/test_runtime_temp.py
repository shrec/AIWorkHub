from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import runtime_temp, task_store, terminal_log_retention


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _write_manifest(
    directory: Path,
    request_id: str,
    *,
    pid: int,
    starttime: int,
    namespace: str = "worker",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / runtime_temp.OWNER_MANIFEST_NAME
    manifest.write_text(
        json.dumps({
            "schema_id": runtime_temp.OWNER_SCHEMA_ID,
            "request_id": request_id,
            "repo_id": runtime_temp._repo_fingerprint(Path("/unused/repo")),
            "pid": pid,
            "starttime": starttime,
            "created_at": "2026-01-01T00:00:00+00:00",
            "namespace": namespace,
        }),
        encoding="utf-8",
    )
    return directory


def test_temp_root_is_repo_local_and_fails_closed_on_symlink(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert runtime_temp.temp_root(repo) == (repo / ".aiworkhub" / "temp")

    # A real (non-symlink) hub and temp dir are acceptable.
    hub = repo / ".aiworkhub"
    hub.mkdir()
    (hub / "temp").mkdir()
    assert runtime_temp.temp_root(repo) == (repo / ".aiworkhub" / "temp")

    # A symlinked .aiworkhub hub must fail closed.
    shutil.rmtree(hub)
    os.symlink(tmp_path / "elsewhere", hub)
    with pytest.raises(runtime_temp.RuntimeTempError):
        runtime_temp.temp_root(repo)

    # A symlinked temp dir must fail closed too.
    hub.unlink()
    hub.mkdir()
    os.symlink(tmp_path / "elsewhere", hub / "temp")
    with pytest.raises(runtime_temp.RuntimeTempError):
        runtime_temp.temp_root(repo)


def test_request_dirs_are_namespaced_by_identity_and_repo(tmp_path: Path) -> None:
    repo_a = _repo(tmp_path)
    repo_b = tmp_path / "other"
    repo_b.mkdir()

    a = runtime_temp.request_dir(repo_a, "req-1", "validation")
    b = runtime_temp.request_dir(repo_b, "req-1", "validation")
    assert a == (repo_a / ".aiworkhub" / "temp" / "validation" / "req-1")
    assert b == (repo_b / ".aiworkhub" / "temp" / "validation" / "req-1")
    assert a != b  # two repositories never share mutable temp state

    with pytest.raises(runtime_temp.RuntimeTempError):
        runtime_temp.request_dir(repo_a, "../escape", "worker")


def test_provision_request_temp_layout_and_owner_manifest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    layout = runtime_temp.provision_request_temp(
        repo, "req-provision", namespace="worker"
    )

    assert layout.root == runtime_temp.request_dir(repo, "req-provision", "worker")
    for sub in ("home", "tmp", "mypy_cache", "stdio"):
        assert (layout.root / sub).is_dir()

    manifest = runtime_temp.read_owner_manifest(layout.root)
    assert manifest is not None
    assert manifest["request_id"] == "req-provision"
    assert manifest["pid"] == os.getpid()
    assert runtime_temp.owner_alive(manifest) is True

    # A second request shares no mutable state with the first.
    other = runtime_temp.provision_request_temp(repo, "req-other", namespace="worker")
    assert other.root != layout.root
    assert list(layout.root.iterdir())  # first request dir still intact and isolated


def test_read_owner_manifest_rejects_fifo_promptly(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo") or not hasattr(os, "fork"):
        pytest.skip("requires POSIX FIFO and fork support")

    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    os.mkfifo(owner_dir / runtime_temp.OWNER_MANIFEST_NAME)

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            result = runtime_temp.read_owner_manifest(owner_dir)
            os.write(write_fd, b"1" if result is None else b"0")
        finally:
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    deadline = time.monotonic() + 1
    pid = 0
    status = 0
    try:
        while time.monotonic() < deadline:
            pid, status = os.waitpid(child, os.WNOHANG)
            if pid:
                break
            time.sleep(0.01)
        assert pid == child
        assert os.waitstatus_to_exitcode(status) == 0
        assert os.read(read_fd, 1) == b"1"
    finally:
        os.close(read_fd)
        if pid == 0:
            try:
                os.kill(child, 9)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child, os.WNOHANG)
            except ChildProcessError:
                pass


def test_read_owner_manifest_rejects_special_symlink_and_oversized(
    tmp_path: Path,
) -> None:
    valid = _write_manifest(tmp_path / "valid", "valid", pid=111, starttime=111)
    assert runtime_temp.read_owner_manifest(valid)["request_id"] == "valid"

    symlinked = tmp_path / "symlinked"
    symlinked.mkdir()
    os.symlink(
        valid / runtime_temp.OWNER_MANIFEST_NAME,
        symlinked / runtime_temp.OWNER_MANIFEST_NAME,
    )
    assert runtime_temp.read_owner_manifest(symlinked) is None

    directory_manifest = tmp_path / "directory-manifest"
    (directory_manifest / runtime_temp.OWNER_MANIFEST_NAME).mkdir(parents=True)
    assert runtime_temp.read_owner_manifest(directory_manifest) is None

    oversized = tmp_path / "oversized"
    oversized.mkdir()
    (oversized / runtime_temp.OWNER_MANIFEST_NAME).write_text(
        "{" + " " * runtime_temp.MAX_MANIFEST_BYTES + "}",
        encoding="utf-8",
    )
    assert runtime_temp.read_owner_manifest(oversized) is None


def test_read_owner_manifest_rejects_lstat_open_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(tmp_path / "race", "race", pid=111, starttime=111)
    replacement = tmp_path / "replacement.json"
    replacement.write_text(
        json.dumps({
            "schema_id": runtime_temp.OWNER_SCHEMA_ID,
            "request_id": "replacement",
        }),
        encoding="utf-8",
    )
    original = owner_dir / runtime_temp.OWNER_MANIFEST_NAME
    assert original.stat().st_ino != replacement.stat().st_ino
    real_open = os.open

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        os.replace(replacement, owner_dir / runtime_temp.OWNER_MANIFEST_NAME)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: False)
    monkeypatch.setattr(runtime_temp.os, "open", swap_before_open)
    assert runtime_temp.read_owner_manifest(owner_dir) is None


@pytest.mark.skipif(
    os.name == "nt",
    reason="exercises POSIX fd/path replacement semantics; native Windows denies replacement by share mode",
)
def test_read_owner_manifest_rejects_post_read_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(
        tmp_path / "post-read-race", "race", pid=111, starttime=111
    )
    replacement_dir = _write_manifest(
        tmp_path / "post-read-replacement", "replacement", pid=222, starttime=222
    )
    original = owner_dir / runtime_temp.OWNER_MANIFEST_NAME
    replacement = replacement_dir / runtime_temp.OWNER_MANIFEST_NAME
    assert original.stat().st_ino != replacement.stat().st_ino
    real_read = os.read
    replaced = False

    def swap_after_read(fd: int, n: int) -> bytes:
        nonlocal replaced
        raw = real_read(fd, n)
        if not replaced:
            replaced = True
            os.replace(replacement, original)
        return raw

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: False)
    monkeypatch.setattr(runtime_temp.os, "read", swap_after_read)
    assert runtime_temp.read_owner_manifest(owner_dir) is None
    assert replaced is True


def _stat_with(
    info: os.stat_result,
    *,
    st_mode: int | None = None,
    st_ino: int | None = None,
    st_nlink: int | None = None,
    st_size: int | None = None,
) -> os.stat_result:
    values = list(info)
    if st_mode is not None:
        values[0] = st_mode
    if st_ino is not None:
        values[1] = st_ino
    if st_nlink is not None:
        values[3] = st_nlink
    if st_size is not None:
        values[6] = st_size
    return os.stat_result(values)


def _windows_metadata(
    size: int,
    *,
    last_write_time: int = 100,
    change_time: int = 200,
) -> runtime_temp._WindowsHandleMetadata:
    return runtime_temp._WindowsHandleMetadata(
        file_attributes=0x80,
        last_write_time=last_write_time,
        change_time=change_time,
        end_of_file=size,
    )


def test_read_owner_manifest_windows_identity_accepts_valid_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(
        tmp_path / "windows-valid", "windows-valid", pid=111, starttime=111
    )
    raw = (owner_dir / runtime_temp.OWNER_MANIFEST_NAME).read_bytes()
    identity = (10, (1 << 96) + 20)
    handle = 1234
    read_sizes = []

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: True)
    monkeypatch.setattr(runtime_temp, "_windows_path_identity", lambda path: identity)
    monkeypatch.setattr(
        runtime_temp, "_windows_open_manifest_handle", lambda path: handle
    )
    monkeypatch.setattr(
        runtime_temp, "_windows_handle_identity", lambda value: identity
    )
    monkeypatch.setattr(
        runtime_temp,
        "_windows_handle_metadata",
        lambda value: _windows_metadata(len(raw), change_time=0),
    )
    monkeypatch.setattr(runtime_temp, "_windows_close_handle", lambda value: None)

    def read_handle(value: int, size: int) -> bytes:
        assert value == handle
        read_sizes.append(size)
        return raw

    monkeypatch.setattr(runtime_temp, "_windows_read_handle", read_handle)

    manifest = runtime_temp.read_owner_manifest(owner_dir)
    assert manifest is not None
    assert manifest["request_id"] == "windows-valid"
    assert read_sizes == [runtime_temp.MAX_MANIFEST_BYTES + 1]


def test_windows_open_manifest_handle_denies_write_delete_sharing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / runtime_temp.OWNER_MANIFEST_NAME
    manifest.write_text("{}", encoding="utf-8")
    calls: list[tuple[str, int, int, int, int]] = []

    class CreateFileW:
        argtypes = None
        restype = None

        def __call__(
            self,
            path: str,
            access: int,
            share_mode: int,
            security: object,
            creation: int,
            flags: int,
            template: object,
        ) -> int:
            del security, template
            calls.append((path, access, share_mode, creation, flags))
            return 4321

    class Kernel32:
        pass

    kernel32 = Kernel32()
    kernel32.CreateFileW = CreateFileW()

    monkeypatch.setattr(
        runtime_temp.ctypes,
        "WinDLL",
        lambda name, use_last_error=True: kernel32,
        raising=False,
    )

    assert runtime_temp._windows_open_manifest_handle(manifest) == 4321
    assert calls == [
        (
            str(manifest),
            0x80000000,
            runtime_temp._WINDOWS_FILE_SHARE_READ,
            3,
            0x80 | 0x00200000,
        )
    ]


@pytest.mark.parametrize(
    ("failure_stage", "expected_trace"),
    [
        ("pre-lstat", ["pre-lstat"]),
        ("pre-identity", ["pre-lstat", "pre-identity"]),
        ("open", ["pre-lstat", "pre-identity", "open"]),
        (
            "opened-stat/type",
            ["pre-lstat", "pre-identity", "open", "opened-stat/type"],
        ),
        (
            "path-identity",
            [
                "pre-lstat",
                "pre-identity",
                "open",
                "opened-stat/type",
                "path-identity",
            ],
        ),
        (
            "read-entry",
            [
                "pre-lstat",
                "pre-identity",
                "open",
                "opened-stat/type",
                "path-identity",
                "read-entry",
            ],
        ),
    ],
)
def test_read_owner_manifest_windows_stage_bound_pre_read_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_trace: list[str],
) -> None:
    owner_dir = _write_manifest(
        tmp_path / f"windows-stage-{failure_stage}", "stage", pid=111, starttime=111
    )
    manifest_path = owner_dir / runtime_temp.OWNER_MANIFEST_NAME
    real_lstat = Path.lstat
    identity = (10, (1 << 80) + 20)
    handle = 1234
    trace: list[str] = []

    def lstat_gate(self: Path) -> os.stat_result:
        info = real_lstat(self)
        if self != manifest_path:
            return info
        if not trace:
            trace.append("pre-lstat")
            if failure_stage == "pre-lstat":
                return _stat_with(info, st_mode=(info.st_mode & ~0o170000) | 0o040000)
        return info

    path_identity_calls = 0

    def path_identity(path: Path) -> tuple[int, int] | None:
        nonlocal path_identity_calls
        path_identity_calls += 1
        trace.append("pre-identity" if path_identity_calls == 1 else "path-identity")
        if path_identity_calls == 1 and failure_stage == "pre-identity":
            return None
        if path_identity_calls > 1 and failure_stage == "path-identity":
            return None
        return identity

    def open_handle(path: Path) -> int | None:
        trace.append("open")
        return None if failure_stage == "open" else handle

    handle_identity_calls = 0

    def handle_identity(value: int) -> tuple[int, int] | None:
        nonlocal handle_identity_calls
        assert value == handle
        handle_identity_calls += 1
        if handle_identity_calls == 1:
            trace.append("opened-stat/type")
            if failure_stage == "opened-stat/type":
                return None
        return identity

    def read_handle(value: int, size: int) -> bytes | None:
        assert value == handle
        assert size == runtime_temp.MAX_MANIFEST_BYTES + 1
        trace.append("read-entry")
        return None if failure_stage == "read-entry" else manifest_path.read_bytes()

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: True)
    monkeypatch.setattr(runtime_temp.Path, "lstat", lstat_gate)
    monkeypatch.setattr(runtime_temp, "_windows_path_identity", path_identity)
    monkeypatch.setattr(runtime_temp, "_windows_open_manifest_handle", open_handle)
    monkeypatch.setattr(runtime_temp, "_windows_handle_identity", handle_identity)
    monkeypatch.setattr(
        runtime_temp,
        "_windows_handle_metadata",
        lambda value: _windows_metadata(manifest_path.stat().st_size),
    )
    monkeypatch.setattr(runtime_temp, "_windows_read_handle", read_handle)
    monkeypatch.setattr(runtime_temp, "_windows_close_handle", lambda value: None)

    assert runtime_temp.read_owner_manifest(owner_dir) is None
    assert trace == expected_trace


@pytest.mark.parametrize(
    "missing_stage", ["pre", "opened", "path", "zero-pre", "zero-opened", "zero-path"]
)
def test_read_owner_manifest_windows_fails_closed_for_missing_full_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_stage: str
) -> None:
    owner_dir = _write_manifest(
        tmp_path / f"windows-missing-{missing_stage}", "missing", pid=111, starttime=111
    )
    identity = (10, (1 << 72) + 20)
    zero_identity = (10, 0)
    handle = 1234
    path_calls = 0
    read_called = False

    def path_identity(path: Path) -> tuple[int, int] | None:
        nonlocal path_calls
        path_calls += 1
        if missing_stage == "pre" and path_calls == 1:
            return None
        if missing_stage == "path" and path_calls > 1:
            return None
        if missing_stage == "zero-pre" and path_calls == 1:
            return zero_identity
        if missing_stage == "zero-path" and path_calls > 1:
            return zero_identity
        return identity

    def handle_identity(value: int) -> tuple[int, int] | None:
        if missing_stage == "opened":
            return None
        if missing_stage == "zero-opened":
            return zero_identity
        return identity

    def read_handle(value: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return b"{}"

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: True)
    monkeypatch.setattr(runtime_temp, "_windows_path_identity", path_identity)
    monkeypatch.setattr(
        runtime_temp, "_windows_open_manifest_handle", lambda path: handle
    )
    monkeypatch.setattr(runtime_temp, "_windows_handle_identity", handle_identity)
    monkeypatch.setattr(runtime_temp, "_windows_read_handle", read_handle)
    monkeypatch.setattr(runtime_temp, "_windows_close_handle", lambda value: None)

    assert runtime_temp.read_owner_manifest(owner_dir) is None
    assert read_called is False


def test_read_owner_manifest_windows_rejects_replacement_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(
        tmp_path / "windows-before-open-race", "race", pid=111, starttime=111
    )
    replacement_dir = _write_manifest(
        tmp_path / "windows-before-open-replacement",
        "replacement",
        pid=222,
        starttime=222,
    )
    original = owner_dir / runtime_temp.OWNER_MANIFEST_NAME
    replacement = replacement_dir / runtime_temp.OWNER_MANIFEST_NAME
    original_identity = (10, (1 << 88) + 20)
    replacement_identity = (10, (1 << 88) + 21)
    handle = 1234
    replaced = False
    read_called = False

    def path_identity(path: Path) -> tuple[int, int]:
        nonlocal replaced
        if not replaced:
            replaced = True
            os.replace(replacement, original)
            return original_identity
        return replacement_identity

    def open_handle(path: Path) -> int:
        assert replaced is True
        return handle

    def read_handle(value: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return original.read_bytes()

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: True)
    monkeypatch.setattr(runtime_temp, "_windows_path_identity", path_identity)
    monkeypatch.setattr(runtime_temp, "_windows_open_manifest_handle", open_handle)
    monkeypatch.setattr(
        runtime_temp, "_windows_handle_identity", lambda value: replacement_identity
    )
    monkeypatch.setattr(
        runtime_temp,
        "_windows_handle_metadata",
        lambda value: _windows_metadata(original.stat().st_size),
    )
    monkeypatch.setattr(runtime_temp, "_windows_read_handle", read_handle)
    monkeypatch.setattr(runtime_temp, "_windows_close_handle", lambda value: None)

    assert runtime_temp.read_owner_manifest(owner_dir) is None
    assert replaced is True
    assert read_called is False


def test_read_owner_manifest_windows_reads_with_posix_metadata_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(
        tmp_path / "windows-pre-read-stable", "race", pid=111, starttime=111
    )
    replacement_dir = _write_manifest(
        tmp_path / "windows-pre-read-replacement", "replacement", pid=222, starttime=222
    )
    original = owner_dir / runtime_temp.OWNER_MANIFEST_NAME
    replacement = replacement_dir / runtime_temp.OWNER_MANIFEST_NAME
    original_identity = (10, (1 << 96) + 20)
    replacement_identity = (10, (1 << 96) + 21)
    real_lstat = Path.lstat
    replaced = False
    handle = 1234

    def path_identity(path: Path) -> tuple[int, int]:
        return replacement_identity if replaced else original_identity

    def lstat_with_windows_metadata(self: Path) -> os.stat_result:
        info = real_lstat(self)
        if self == original:
            return _stat_with(
                info, st_ino=20, st_nlink=23, st_size=info.st_size + 5
            )
        return info

    def swap_after_read(value: int, size: int) -> bytes:
        nonlocal replaced
        assert value == handle
        assert size == runtime_temp.MAX_MANIFEST_BYTES + 1
        raw = original.read_bytes()
        os.replace(replacement, original)
        replaced = True
        return raw

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: True)
    monkeypatch.setattr(runtime_temp, "_windows_path_identity", path_identity)
    monkeypatch.setattr(
        runtime_temp, "_windows_open_manifest_handle", lambda path: handle
    )
    monkeypatch.setattr(
        runtime_temp,
        "_windows_handle_identity",
        lambda value: original_identity,
    )
    monkeypatch.setattr(
        runtime_temp,
        "_windows_handle_metadata",
        lambda value: _windows_metadata(original.stat().st_size),
    )
    monkeypatch.setattr(runtime_temp.Path, "lstat", lstat_with_windows_metadata)
    monkeypatch.setattr(runtime_temp, "_windows_read_handle", swap_after_read)
    monkeypatch.setattr(runtime_temp, "_windows_close_handle", lambda value: None)

    assert runtime_temp.read_owner_manifest(owner_dir) is None
    assert replaced is True


def test_read_owner_manifest_windows_rejects_post_read_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(
        tmp_path / "windows-post-read-race", "race", pid=111, starttime=111
    )
    original = owner_dir / runtime_temp.OWNER_MANIFEST_NAME
    original_identity = (10, (1 << 80) + 20)
    replacement_identity = (10, (1 << 80) + 21)
    replaced = False
    handle = 1234

    def path_identity(path: Path) -> tuple[int, int]:
        return replacement_identity if replaced else original_identity

    def swap_after_read(value: int, size: int) -> bytes:
        nonlocal replaced
        assert value == handle
        assert size == runtime_temp.MAX_MANIFEST_BYTES + 1
        raw = original.read_bytes()
        replaced = True
        return raw

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: True)
    monkeypatch.setattr(runtime_temp, "_windows_path_identity", path_identity)
    monkeypatch.setattr(
        runtime_temp, "_windows_open_manifest_handle", lambda path: handle
    )
    monkeypatch.setattr(
        runtime_temp, "_windows_handle_identity", lambda value: original_identity
    )
    monkeypatch.setattr(
        runtime_temp,
        "_windows_handle_metadata",
        lambda value: _windows_metadata(original.stat().st_size),
    )
    monkeypatch.setattr(runtime_temp, "_windows_read_handle", swap_after_read)
    monkeypatch.setattr(runtime_temp, "_windows_close_handle", lambda value: None)

    assert runtime_temp.read_owner_manifest(owner_dir) is None
    assert replaced is True


def test_read_owner_manifest_windows_rejects_same_length_in_place_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(
        tmp_path / "windows-same-length-rewrite", "race", pid=111, starttime=111
    )
    original = owner_dir / runtime_temp.OWNER_MANIFEST_NAME
    raw = original.read_bytes()
    replacement = raw.replace(b'"race"', b'"swap"')
    assert len(replacement) == len(raw)
    identity = (10, (1 << 96) + 20)
    handle = 1234
    metadata_calls = 0

    def metadata(value: int) -> runtime_temp._WindowsHandleMetadata:
        nonlocal metadata_calls
        assert value == handle
        metadata_calls += 1
        return _windows_metadata(
            len(raw),
            change_time=200 if metadata_calls == 1 else 201,
        )

    def rewrite_during_read(value: int, size: int) -> bytes:
        assert value == handle
        assert size == runtime_temp.MAX_MANIFEST_BYTES + 1
        original.write_bytes(replacement)
        return raw

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: True)
    monkeypatch.setattr(runtime_temp, "_windows_path_identity", lambda path: identity)
    monkeypatch.setattr(
        runtime_temp, "_windows_open_manifest_handle", lambda path: handle
    )
    monkeypatch.setattr(runtime_temp, "_windows_handle_identity", lambda value: identity)
    monkeypatch.setattr(runtime_temp, "_windows_handle_metadata", metadata)
    monkeypatch.setattr(runtime_temp, "_windows_read_handle", rewrite_during_read)
    monkeypatch.setattr(runtime_temp, "_windows_close_handle", lambda value: None)

    assert runtime_temp.read_owner_manifest(owner_dir) is None
    assert original.read_bytes() == replacement
    assert metadata_calls == 2


@pytest.mark.parametrize("metadata_stage", ["pre-missing", "post-missing"])
def test_read_owner_manifest_windows_fails_closed_for_missing_handle_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata_stage: str
) -> None:
    owner_dir = _write_manifest(
        tmp_path / f"windows-metadata-{metadata_stage}", "metadata", pid=111, starttime=111
    )
    raw = (owner_dir / runtime_temp.OWNER_MANIFEST_NAME).read_bytes()
    identity = (10, (1 << 96) + 20)
    handle = 1234
    calls = 0

    def metadata(value: int) -> runtime_temp._WindowsHandleMetadata | None:
        nonlocal calls
        assert value == handle
        calls += 1
        if metadata_stage == "pre-missing" and calls == 1:
            return None
        if metadata_stage == "post-missing" and calls == 2:
            return None
        return _windows_metadata(len(raw))

    monkeypatch.setattr(runtime_temp, "_is_windows", lambda: True)
    monkeypatch.setattr(runtime_temp, "_windows_path_identity", lambda path: identity)
    monkeypatch.setattr(
        runtime_temp, "_windows_open_manifest_handle", lambda path: handle
    )
    monkeypatch.setattr(runtime_temp, "_windows_handle_identity", lambda value: identity)
    monkeypatch.setattr(runtime_temp, "_windows_handle_metadata", metadata)
    monkeypatch.setattr(runtime_temp, "_windows_read_handle", lambda value, size: raw)
    monkeypatch.setattr(runtime_temp, "_windows_close_handle", lambda value: None)

    assert runtime_temp.read_owner_manifest(owner_dir) is None
    assert calls == (1 if metadata_stage != "post-missing" else 2)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows file identity")
def test_read_owner_manifest_native_windows_rejects_post_read_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_dir = _write_manifest(
        tmp_path / "native-windows-post-read-race", "race", pid=111, starttime=111
    )
    replacement_dir = _write_manifest(
        tmp_path / "native-windows-post-read-replacement",
        "replacement",
        pid=222,
        starttime=222,
    )
    original = owner_dir / runtime_temp.OWNER_MANIFEST_NAME
    replacement = replacement_dir / runtime_temp.OWNER_MANIFEST_NAME
    original_bytes = original.read_bytes()
    replacement_bytes = replacement.read_bytes()
    real_read_handle = runtime_temp._windows_read_handle
    replace_error: OSError | None = None
    replace_attempted = False

    def swap_after_read(handle: int, size: int) -> bytes | None:
        nonlocal replace_attempted, replace_error
        raw = real_read_handle(handle, size)
        if raw is not None and not replace_attempted:
            replace_attempted = True
            try:
                os.replace(replacement, original)
            except OSError as exc:
                replace_error = exc
        return raw

    monkeypatch.setattr(runtime_temp, "_windows_read_handle", swap_after_read)

    manifest = runtime_temp.read_owner_manifest(owner_dir)
    assert manifest is not None
    assert manifest["request_id"] == "race"
    assert manifest["pid"] == 111
    assert replace_attempted is True
    assert isinstance(replace_error, PermissionError)
    assert original.read_bytes() == original_bytes
    assert replacement.read_bytes() == replacement_bytes


def test_owner_alive_live_dead_reused_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = {
        "schema_id": runtime_temp.OWNER_SCHEMA_ID,
        "request_id": "req-x",
        "pid": 111,
        "starttime": 111,
    }
    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: True)
    assert runtime_temp.owner_alive(manifest) is True

    # Dead PID with matching start identity -> dead.  The reuse check reads the
    # current start ticks through the single cross-platform seam
    # ``process_start_ticks`` (Linux /proc, Darwin sysctl, Windows
    # GetProcessTimes); mocking the Linux-only ``_proc_stat_starttime`` would
    # leave that seam live on macOS/Windows, where a fake low PID resolves to a
    # real or absent process and the assertion becomes platform-dependent.
    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: 111)
    assert runtime_temp.owner_alive(manifest) is False

    # Dead PID but start identity mismatch (PID reuse) -> fail closed (live).
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: 999)
    assert runtime_temp.owner_alive(manifest) is True

    # Unknown owner (no pid) -> never deleted.
    assert runtime_temp.owner_alive({"schema_id": runtime_temp.OWNER_SCHEMA_ID}) is True


def test_identify_dead_owner_dirs_skips_live_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    temp = runtime_temp.temp_root(repo)
    live = _write_manifest(
        temp / "validation" / "live", "live", pid=111, starttime=111, namespace="validation"
    )
    dead = _write_manifest(
        temp / "validation" / "dead", "dead", pid=222, starttime=222, namespace="validation"
    )
    unknown = temp / "validation" / "unknown"
    unknown.mkdir(parents=True)  # no owner manifest -> never deleted

    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: {111: True, 222: False}.get(pid))
    # Mock the cross-platform identity seam, not the Linux-only /proc reader, so
    # the reuse check is deterministic on Linux, macOS and Windows alike.
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: pid)

    dead_dirs = runtime_temp.identify_dead_owner_dirs(repo)
    paths = {Path(item["path"]) for item in dead_dirs}
    assert paths == {dead}
    assert live not in paths
    assert unknown not in paths


def test_dispose_request_temp_removes_exact_and_rejects_escape(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    layout = runtime_temp.provision_request_temp(repo, "req-dispose", namespace="worker")
    root = layout.root

    assert runtime_temp.dispose_request_temp(
        root, repo=repo, expected_request_id="req-dispose"
    ) is True
    assert not root.exists()

    # A path that escapes the repo temp authority must fail closed.
    outsider = tmp_path / "outside"
    outsider.mkdir()
    with pytest.raises(runtime_temp.RuntimeTempError):
        runtime_temp.dispose_request_temp(outsider, repo=repo)

    # Identity mismatch fails closed and leaves the directory in place.
    other = runtime_temp.provision_request_temp(repo, "req-keep", namespace="worker")
    with pytest.raises(runtime_temp.RuntimeTempError):
        runtime_temp.dispose_request_temp(
            other.root, repo=repo, expected_request_id="req-wrong"
        )
    assert other.root.exists()


def test_quota_is_bounded(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for index in range(3):
        runtime_temp.provision_request_temp(repo, f"req-{index}", namespace="worker")

    info = runtime_temp.quota(repo)
    assert info["dirs"] == 3
    assert info["bytes"] >= 0
    assert info["max_dirs"] > 0
    assert info["within_quota"] is True


def test_enforce_gc_removes_only_exact_dead_owner_temp_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    assert task_store.initialize_repository(repo)["ok"]

    temp = runtime_temp.temp_root(repo)
    dead = _write_manifest(
        temp / "validation" / "dead-gc",
        "dead-gc",
        pid=222,
        starttime=222,
        namespace="validation",
    )
    live = _write_manifest(
        temp / "validation" / "live-gc",
        "live-gc",
        pid=111,
        starttime=111,
        namespace="validation",
    )
    unknown = temp / "validation" / "unknown-gc"
    unknown.mkdir(parents=True)

    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: {111: True, 222: False}.get(pid))
    # Mock the cross-platform identity seam, not the Linux-only /proc reader, so
    # the GC reclaims only provably-dead owners identically on every platform.
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: pid)

    result = terminal_log_retention.enforce(repo)
    assert result["ok"] is True
    assert result["temp_gc_count"] == 1
    assert not dead.exists()
    assert live.exists()
    assert unknown.exists()


def test_now_utc_is_injectable_and_manifest_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retention/manifest timestamps come from the injectable clock, never from
    filesystem mtimes (which the outer validation sandbox denies mutating)."""
    frozen = datetime(2036, 5, 4, 3, 2, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime_temp, "now_utc", lambda: frozen)
    assert runtime_temp.now_utc() == frozen

    repo = _repo(tmp_path)
    layout = runtime_temp.provision_request_temp(repo, "req-clock", namespace="worker")
    manifest = runtime_temp.read_owner_manifest(layout.root)
    assert manifest is not None
    assert manifest["created_at"] == frozen.isoformat()


def test_worker_workspace_direct_script_resolves_sibling_runtime_temp() -> None:
    """worker_workspace.py is executed directly as the Landlock wrapper, so its
    bootstrap must resolve the authenticated sibling runtime_temp.py rather than
    a package-relative or installed fallback."""
    from aiworkhub import worker_workspace

    original_runtime_temp = sys.modules.get("aiworkhub.runtime_temp")
    ws_py = Path(worker_workspace.__file__).resolve()
    spec = importlib.util.spec_from_file_location("_direct_worker_workspace", ws_py)
    assert spec is not None and spec.loader is not None
    direct = importlib.util.module_from_spec(spec)
    sys.modules["_direct_worker_workspace"] = direct
    try:
        spec.loader.exec_module(direct)
        assert direct.runtime_temp is sys.modules["aiworkhub.runtime_temp"]
        assert Path(direct.runtime_temp.__file__).resolve() == ws_py.parent / "runtime_temp.py"
        assert direct.runtime_temp.TEMP_ROOT_ENV == "AIWORKHUB_TEMP_ROOT"
    finally:
        if original_runtime_temp is not None:
            sys.modules["aiworkhub.runtime_temp"] = original_runtime_temp
        else:
            sys.modules.pop("aiworkhub.runtime_temp", None)
        sys.modules.pop("_direct_worker_workspace", None)


class _BoundApi:
    def __init__(self, fn: object) -> None:
        self._fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args: object, **kwargs: object) -> object:
        return self._fn(*args, **kwargs)


def _handle_int(value: object) -> int:
    raw = getattr(value, "value", value)
    if raw is None:
        return 0
    return int(raw)


def _pack_file_id_both_dir_info(
    entries: list[tuple[str, int, int]],
) -> bytes:
    blobs: list[bytes] = []
    for index, (name, file_id, attrs) in enumerate(entries):
        encoded = name.encode("utf-16-le")
        used = runtime_temp._FILE_ID_BOTH_DIR_INFO_FILE_NAME + len(encoded)
        aligned = (used + 7) & ~7
        is_last = index == len(entries) - 1
        next_off = 0 if is_last else aligned
        header = bytearray(runtime_temp._FILE_ID_BOTH_DIR_INFO_FILE_NAME)
        header[0:4] = int(next_off).to_bytes(4, "little")
        header[56:60] = int(attrs).to_bytes(4, "little")
        header[60:64] = len(encoded).to_bytes(4, "little")
        header[96:104] = int(file_id).to_bytes(8, "little")
        payload = bytes(header) + encoded
        if not is_last:
            payload = payload + bytes(aligned - len(payload))
        blobs.append(payload)
    return b"".join(blobs)


class _FakeDirectoryKernel32:
    def __init__(
        self,
        *,
        handle: int,
        final_path: str,
        volume_serial: int,
        file_id: int,
        pages: list[bytes],
        attributes: int = 0x10,
        is_directory: bool = True,
        reparse: bool = False,
        fail_open: bool = False,
        invalid_handle: bool = False,
        fail_basic: bool = False,
        fail_standard: bool = False,
        fail_file_id: bool = False,
        zero_volume: bool = False,
        zero_file_id: bool = False,
        final_needed: int | None = None,
        final_written: int | None = None,
        enum_error_after: int | None = None,
        enum_error: int = 5,
        close_results: list[int] | None = None,
    ) -> None:
        self.handle = handle
        self.final_path = final_path
        self.volume_serial = volume_serial
        self.file_id = file_id
        self.pages = pages
        self.attributes = attributes
        self.is_directory = is_directory
        self.reparse = reparse
        self.fail_open = fail_open
        self.invalid_handle = invalid_handle
        self.fail_basic = fail_basic
        self.fail_standard = fail_standard
        self.fail_file_id = fail_file_id
        self.zero_volume = zero_volume
        self.zero_file_id = zero_file_id
        self.final_needed = final_needed
        self.final_written = final_written
        self.enum_error_after = enum_error_after
        self.enum_error = enum_error
        self.close_results = list(close_results or [])
        self.last_error = 0
        self.create_calls: list[tuple[str, int, int, int, int]] = []
        self.info_handles: list[int] = []
        self.dir_info_classes: list[int] = []
        self.final_handles: list[int] = []
        self.close_handles: list[int] = []
        self.set_file_info_calls = 0
        self._page_cursor = 0
        self.CreateFileW = _BoundApi(self._create)
        self.GetFileInformationByHandleEx = _BoundApi(self._info_ex)
        self.GetFinalPathNameByHandleW = _BoundApi(self._final)
        self.CloseHandle = _BoundApi(self._close)
        self.SetFileInformationByHandle = _BoundApi(self._set_info)

    def _create(
        self,
        path: object,
        access: object,
        share: object,
        security: object,
        creation: object,
        flags: object,
        template: object,
    ) -> int:
        del security, template
        self.create_calls.append(
            (str(path), int(access), int(share), int(creation), int(flags))
        )
        if self.fail_open:
            return 0
        if self.invalid_handle:
            return runtime_temp._windows_invalid_handle_value()
        return self.handle

    def _info_ex(
        self,
        handle: object,
        info_class: object,
        buf: object,
        size: object,
    ) -> int:
        ctypes = runtime_temp.ctypes
        ic = _handle_int(info_class)
        self.info_handles.append(_handle_int(handle))
        if ic == runtime_temp._WINDOWS_FILE_BASIC_INFO:
            if self.fail_basic:
                return 0
            attrs = self.attributes
            if self.reparse:
                attrs |= runtime_temp._WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
            blob = bytes(32) + int(attrs).to_bytes(4, "little") + bytes(4)
            ctypes.memmove(buf, blob, min(len(blob), _handle_int(size)))
            return 1
        if ic == runtime_temp._WINDOWS_FILE_STANDARD_INFO:
            if self.fail_standard:
                return 0
            directory = 1 if self.is_directory else 0
            blob = bytes(16) + (1).to_bytes(4, "little") + bytes([0, directory])
            ctypes.memmove(buf, blob, min(len(blob), _handle_int(size)))
            return 1
        if ic == runtime_temp._WINDOWS_FILE_ID_INFO:
            if self.fail_file_id:
                return 0
            serial = 0 if self.zero_volume else self.volume_serial
            fid = 0 if self.zero_file_id else self.file_id
            blob = int(serial).to_bytes(8, "little") + int(fid).to_bytes(16, "little")
            ctypes.memmove(buf, blob, min(len(blob), _handle_int(size)))
            return 1
        if ic in (
            runtime_temp._WINDOWS_FILE_ID_BOTH_DIRECTORY_INFO,
            runtime_temp._WINDOWS_FILE_ID_BOTH_DIRECTORY_RESTART_INFO,
        ):
            self.dir_info_classes.append(ic)
            if ic == runtime_temp._WINDOWS_FILE_ID_BOTH_DIRECTORY_RESTART_INFO:
                self._page_cursor = 0
            if (
                self.enum_error_after is not None
                and len(self.dir_info_classes) > self.enum_error_after
            ):
                self.last_error = self.enum_error
                return 0
            if self._page_cursor < len(self.pages):
                page = self.pages[self._page_cursor]
                self._page_cursor += 1
                ctypes.memmove(buf, page, min(len(page), _handle_int(size)))
                return 1
            self.last_error = runtime_temp._WINDOWS_ERROR_NO_MORE_FILES
            return 0
        self.last_error = 87
        return 0

    def _final(
        self,
        handle: object,
        buf: object,
        cch: object,
        flags: object,
    ) -> int:
        del flags
        ctypes = runtime_temp.ctypes
        self.final_handles.append(_handle_int(handle))
        path = self.final_path
        encoded = path.encode("utf-16-le", errors="surrogatepass")
        unit_count = len(encoded) // 2
        needed = unit_count + 1 if self.final_needed is None else int(self.final_needed)
        cch_i = _handle_int(cch)
        if buf in (None, 0) or cch_i == 0:
            return needed
        written = unit_count if self.final_written is None else int(self.final_written)
        if written > 0 and buf not in (None, 0) and cch_i > 0:
            payload = encoded + b"\x00\x00"
            ctypes.memmove(buf, payload, min(len(payload), cch_i * 2))
        return written

    def _close(self, handle: object) -> int:
        self.close_handles.append(_handle_int(handle))
        if self.close_results:
            return int(self.close_results.pop(0))
        return 1

    def _set_info(self, *args: object, **kwargs: object) -> int:
        del args, kwargs
        self.set_file_info_calls += 1
        raise AssertionError("SetFileInformationByHandle must not run")


_ENUM_ROOT = Path("C:/aiworkhub-enum-root")
_ENUM_FINAL = r"\\?\C:\aiworkhub-enum-root"
_WIDE_HANDLE = (1 << 32) + 99


def _install_directory_kernel32(
    monkeypatch: pytest.MonkeyPatch, kernel32: _FakeDirectoryKernel32
) -> None:
    monkeypatch.setattr(
        runtime_temp.ctypes,
        "WinDLL",
        lambda name, use_last_error=True: kernel32,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_temp.ctypes,
        "get_last_error",
        lambda: kernel32.last_error,
        raising=False,
    )


def _open_authority(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pages: list[bytes] | None = None,
    handle: int = _WIDE_HANDLE,
    close_results: list[int] | None = None,
    enum_error_after: int | None = None,
    enum_error: int = 5,
) -> tuple[runtime_temp.WindowsDirectoryAuthority, _FakeDirectoryKernel32]:
    kernel32 = _FakeDirectoryKernel32(
        handle=handle,
        final_path=_ENUM_FINAL,
        volume_serial=0xABCDEF12,
        file_id=(1 << 80) + 7,
        pages=list(pages or []),
        close_results=close_results,
        enum_error_after=enum_error_after,
        enum_error=enum_error,
    )
    _install_directory_kernel32(monkeypatch, kernel32)
    return runtime_temp.WindowsDirectoryAuthority(_ENUM_ROOT), kernel32

def test_windows_directory_authority_two_page_pagination_and_info_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page1 = _pack_file_id_both_dir_info([("page-one", 11, 0x80)])
    page2 = _pack_file_id_both_dir_info(
        [("page-two", 12, runtime_temp._WINDOWS_FILE_ATTRIBUTE_DIRECTORY)]
    )
    auth, kernel32 = _open_authority(monkeypatch, pages=[page1, page2])
    try:
        entries = auth.enumerate_entries()
        assert [entry.name for entry in entries] == ["page-one", "page-two"]
        assert entries[0].file_id == 11
        assert entries[0].is_directory is False
        assert entries[1].file_id == 12
        assert entries[1].is_directory is True
        assert kernel32.dir_info_classes == [
            runtime_temp._WINDOWS_FILE_ID_BOTH_DIRECTORY_RESTART_INFO,
            runtime_temp._WINDOWS_FILE_ID_BOTH_DIRECTORY_INFO,
            runtime_temp._WINDOWS_FILE_ID_BOTH_DIRECTORY_INFO,
        ]
        assert kernel32.set_file_info_calls == 0
        assert kernel32.create_calls == [
            (
                str(_ENUM_ROOT),
                runtime_temp._WINDOWS_FILE_LIST_DIRECTORY
                | runtime_temp._WINDOWS_FILE_READ_ATTRIBUTES,
                runtime_temp._WINDOWS_FILE_SHARE_READ
                | runtime_temp._WINDOWS_FILE_SHARE_WRITE,
                runtime_temp._WINDOWS_OPEN_EXISTING,
                runtime_temp._WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
                | runtime_temp._WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
            )
        ]
        share = kernel32.create_calls[0][2]
        assert share & 0x00000004 == 0
        assert auth.handle == _WIDE_HANDLE
        assert auth.volume_serial == 0xABCDEF12
        assert auth.file_id == (1 << 80) + 7
        assert set(kernel32.info_handles) == {_WIDE_HANDLE}
        assert kernel32.final_handles == [_WIDE_HANDLE, _WIDE_HANDLE]
    finally:
        auth.close()
    assert kernel32.close_handles == [_WIDE_HANDLE]


def test_windows_directory_authority_preserves_greater_than_32_bit_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth, kernel32 = _open_authority(monkeypatch, handle=_WIDE_HANDLE)
    auth.close()
    assert all(value == _WIDE_HANDLE for value in kernel32.info_handles)
    assert kernel32.close_handles == [_WIDE_HANDLE]
    assert _WIDE_HANDLE > 0xFFFFFFFF


def test_file_id_both_dir_info_unsigned_dot_exclusion_and_malformed() -> None:
    packed = _pack_file_id_both_dir_info(
        [
            (".", 1, runtime_temp._WINDOWS_FILE_ATTRIBUTE_DIRECTORY),
            ("..", 2, runtime_temp._WINDOWS_FILE_ATTRIBUTE_DIRECTORY),
            ("keep", 0x8000000000000001, 0x80),
        ]
    )
    parsed = runtime_temp._parse_file_id_both_dir_info(packed)
    assert parsed is not None
    assert [entry.name for entry in parsed] == ["keep"]
    assert parsed[0].file_id == 0x8000000000000001
    assert runtime_temp._parse_file_id_both_dir_info(b"\x00" * 50) is None
    header = bytearray(104)
    header[60:64] = (0x80000000).to_bytes(4, "little")
    header[96:104] = (3).to_bytes(8, "little")
    assert runtime_temp._parse_file_id_both_dir_info(bytes(header)) is None
    odd = bytearray(_pack_file_id_both_dir_info([("x", 4, 0x80)]))
    odd[60:64] = (1).to_bytes(4, "little")
    assert runtime_temp._parse_file_id_both_dir_info(bytes(odd)) is None
    unaligned = bytearray(104 + 2)
    unaligned[0:4] = (12).to_bytes(4, "little")
    unaligned[60:64] = (2).to_bytes(4, "little")
    unaligned[96:104] = (5).to_bytes(8, "little")
    unaligned[104:106] = "a".encode("utf-16-le")
    assert runtime_temp._parse_file_id_both_dir_info(bytes(unaligned)) is None
    zero_id = bytearray(_pack_file_id_both_dir_info([("z", 6, 0x80)]))
    zero_id[96:104] = (0).to_bytes(8, "little")
    assert runtime_temp._parse_file_id_both_dir_info(bytes(zero_id)) is None


def test_windows_directory_file_id_mapping_inequality_when_fields_drift() -> None:
    base = runtime_temp.WindowsDirectoryEntry("same", 9, 0x80, False)
    assert base != runtime_temp.WindowsDirectoryEntry("same", 10, 0x80, False)
    assert base != runtime_temp.WindowsDirectoryEntry("same", 9, 0x20, False)
    assert base != runtime_temp.WindowsDirectoryEntry("same", 9, 0x80, True)
    left = {entry.name: entry for entry in (base,)}
    right = {
        "same": runtime_temp.WindowsDirectoryEntry("same", 9, 0x80, True),
    }
    assert left != right


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"fail_open": True}, "open failed"),
        ({"invalid_handle": True}, "open failed"),
        ({"fail_basic": True}, "metadata unavailable"),
        ({"fail_standard": True}, "metadata unavailable"),
        ({"is_directory": False}, "not a directory"),
        ({"reparse": True}, "reparse"),
        ({"fail_file_id": True}, "FILE_ID_INFO unavailable"),
        ({"zero_volume": True}, "FILE_ID_INFO invalid"),
        ({"zero_file_id": True}, "FILE_ID_INFO invalid"),
        ({"final_needed": 0}, "final path unavailable"),
        ({"final_written": 0}, "final path unavailable"),
        ({"final_needed": 8, "final_written": 8}, "truncated"),
        ({"final_path": r"\\.\C:\aiworkhub-enum-root"}, "final path invalid"),
        ({"final_path": r"\\?\C:\other-root"}, "escapes root"),
    ],
)
def test_windows_directory_authority_failure_close_paths(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object], match: str
) -> None:
    kernel32 = _FakeDirectoryKernel32(
        handle=_WIDE_HANDLE,
        final_path=_ENUM_FINAL,
        volume_serial=9,
        file_id=9,
        pages=[],
    )
    for key, value in kwargs.items():
        setattr(kernel32, key, value)
    _install_directory_kernel32(monkeypatch, kernel32)
    with pytest.raises(runtime_temp.RuntimeTempError, match=match):
        runtime_temp.WindowsDirectoryAuthority(_ENUM_ROOT)
    if kwargs.get("fail_open") or kwargs.get("invalid_handle"):
        assert kernel32.close_handles == []
    else:
        assert kernel32.close_handles == [_WIDE_HANDLE]


def test_windows_directory_authority_path_and_containment_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(runtime_temp.RuntimeTempError, match="path invalid"):
        runtime_temp.WindowsDirectoryAuthority(Path(r"\\.\pipe\aiworkhub-not-a-dir"))
    kernel32 = _FakeDirectoryKernel32(
        handle=_WIDE_HANDLE,
        final_path=r"\\?\UNC\server\share\escaped",
        volume_serial=3,
        file_id=4,
        pages=[],
    )
    _install_directory_kernel32(monkeypatch, kernel32)
    with pytest.raises(runtime_temp.RuntimeTempError, match="escapes root"):
        runtime_temp.WindowsDirectoryAuthority(Path(r"\\server\share\root"))


def test_windows_directory_authority_final_path_decodes_non_bmp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    glyph = "\U0001F600"
    root = Path("C:/aiworkhub-enum-root/" + glyph)
    final = _ENUM_FINAL + "\\" + glyph
    kernel32 = _FakeDirectoryKernel32(
        handle=_WIDE_HANDLE,
        final_path=final,
        volume_serial=9,
        file_id=9,
        pages=[],
    )
    _install_directory_kernel32(monkeypatch, kernel32)
    auth = runtime_temp.WindowsDirectoryAuthority(root)
    try:
        decoded = runtime_temp.WindowsDirectoryAuthority._final_path(auth.handle)
        assert glyph in decoded
        assert all(not (0xD800 <= ord(ch) <= 0xDFFF) for ch in decoded)
    finally:
        auth.close()


def test_windows_directory_authority_final_path_rejects_malformed_utf16(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _FakeDirectoryKernel32(
        handle=_WIDE_HANDLE,
        final_path=_ENUM_FINAL + "\ud800",
        volume_serial=9,
        file_id=9,
        pages=[],
    )
    _install_directory_kernel32(monkeypatch, kernel32)
    with pytest.raises(runtime_temp.RuntimeTempError, match="final path invalid"):
        runtime_temp.WindowsDirectoryAuthority(_ENUM_ROOT)
    glyph = "\U0001F600"
    truncated = _ENUM_FINAL + "\\" + glyph
    kernel32 = _FakeDirectoryKernel32(
        handle=_WIDE_HANDLE,
        final_path=truncated,
        volume_serial=9,
        file_id=9,
        pages=[],
        final_written=len(truncated.encode("utf-16-le")) // 2 - 1,
    )
    _install_directory_kernel32(monkeypatch, kernel32)
    with pytest.raises(runtime_temp.RuntimeTempError, match="final path invalid"):
        runtime_temp.WindowsDirectoryAuthority(_ENUM_ROOT)


def test_windows_directory_authority_enum_nonterminal_error_drops_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page1 = _pack_file_id_both_dir_info([("kept-internal", 21, 0x80)])
    auth, kernel32 = _open_authority(
        monkeypatch, pages=[page1], enum_error_after=1, enum_error=5
    )
    with pytest.raises(runtime_temp.RuntimeTempError, match="enumeration failed"):
        auth.enumerate_entries()
    auth.close()
    assert kernel32.dir_info_classes[0] == (
        runtime_temp._WINDOWS_FILE_ID_BOTH_DIRECTORY_RESTART_INFO
    )


def test_windows_directory_authority_malformed_batch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth, _kernel32 = _open_authority(monkeypatch, pages=[b"\x00" * 40])
    with pytest.raises(runtime_temp.RuntimeTempError, match="malformed"):
        auth.enumerate_entries()
    auth.close()


def test_windows_directory_authority_checked_close_retry_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _pack_file_id_both_dir_info([("retryable", 31, 0x80)])
    auth, kernel32 = _open_authority(
        monkeypatch, pages=[page], close_results=[0, 1]
    )
    with pytest.raises(runtime_temp.RuntimeTempError, match="close failed"):
        auth.close()
    assert auth.closed is False
    retry_entries = auth.enumerate_entries()
    assert [entry.name for entry in retry_entries] == ["retryable"]
    assert kernel32.close_handles == [_WIDE_HANDLE]
    auth.close()
    assert auth.closed is True
    assert kernel32.close_handles == [_WIDE_HANDLE, _WIDE_HANDLE]
    auth.close()
    assert kernel32.close_handles == [_WIDE_HANDLE, _WIDE_HANDLE]
    nested, nested_kernel = _open_authority(
        monkeypatch, pages=[page], close_results=[0]
    )
    with pytest.raises(runtime_temp.RuntimeTempError, match="close failed"):
        with nested:
            pass
    assert nested.closed is False
    nested_kernel.close_results = [1]
    nested.close()
    assert nested.closed is True


def test_windows_close_handle_owner_manifest_remains_non_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CloseHandle:
        argtypes = None
        restype = None

        def __call__(self, handle: object) -> int:
            del handle
            return 0

    class Kernel32:
        pass

    kernel32 = Kernel32()
    kernel32.CloseHandle = CloseHandle()
    monkeypatch.setattr(
        runtime_temp.ctypes,
        "WinDLL",
        lambda name, use_last_error=True: kernel32,
        raising=False,
    )
    runtime_temp._windows_close_handle(_WIDE_HANDLE)


@pytest.mark.skipif(os.name != "nt", reason="native Windows directory canary")
def test_windows_directory_authority_native_file_id_canary(tmp_path: Path) -> None:
    root = tmp_path / "native-dir"
    root.mkdir()
    regular = root / "regular.txt"
    regular.write_text("payload", encoding="utf-8")
    child = root / "child_dir"
    child.mkdir()
    first = runtime_temp.WindowsDirectoryAuthority(root)
    second = runtime_temp.WindowsDirectoryAuthority(root)
    try:
        left = {entry.name: entry for entry in first.enumerate_entries()}
        right = {entry.name: entry for entry in second.enumerate_entries()}
        for name in ("regular.txt", "child_dir"):
            assert name in left and name in right
            assert left[name].file_id > 0
            assert left[name].file_id == right[name].file_id
            assert left[name].file_attributes == right[name].file_attributes
            assert left[name].is_directory == right[name].is_directory
        assert left["regular.txt"].is_directory is False
        assert left["child_dir"].is_directory is True
        assert first.volume_serial > 0
        assert first.file_id > 0
        assert first.volume_serial == second.volume_serial
        assert first.file_id == second.file_id
    finally:
        first.close()
        second.close()
    (root / "after-close.txt").write_text("writable", encoding="utf-8")
    assert (root / "after-close.txt").read_text(encoding="utf-8") == "writable"
