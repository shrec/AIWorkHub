from __future__ import annotations

import ctypes
import os
import shutil
import struct
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

from aiworkhub import worker_workspace
from aiworkhub.validation_metadata_timestamps import (
    TIMESPEC_PAIR_SIZE,
    UTIME_NOW,
    UTIME_OMIT,
    TimestampArgumentError,
    apply_utimensat_fd,
    decode_utimensat_timespec,
)


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or ctypes.sizeof(ctypes.c_long) != 8,
    reason="requires the Linux 64-bit-long utimensat ABI",
)


class Library:
    stale = False

    def seccomp_syscall_resolve_name(self, raw: bytes) -> int:
        return {"utimensat": 280}.get(raw.decode(), -1)

    def seccomp_notify_id_valid(self, _fd: int, _id: int) -> int:
        return -1 if self.stale else 0


def _request(
    path_buffer: ctypes.Array[ctypes.c_char] | None,
    times_buffer: ctypes.Array[ctypes.c_char] | None,
    *,
    dirfd: int = -100,
    flags: int = 0,
) -> worker_workspace._SeccompNotif:
    value = worker_workspace._SeccompNotif()
    value.id = 7
    value.pid = os.getpid()
    value.data.nr = 280
    value.data.args[0] = ctypes.c_uint32(dirfd).value
    value.data.args[1] = 0 if path_buffer is None else ctypes.addressof(path_buffer)
    value.data.args[2] = 0 if times_buffer is None else ctypes.addressof(times_buffer)
    value.data.args[3] = flags
    return value


def _times(values: tuple[int, int, int, int]) -> ctypes.Array[ctypes.c_char]:
    return ctypes.create_string_buffer(struct.pack("=qqqq", *values))


def _apply(
    root: Path,
    target: Path,
    values: tuple[int, int, int, int] | None,
    *,
    by_fd: bool = False,
    library: Library | None = None,
    flags: int = 0,
) -> None:
    path_buffer = None if by_fd else ctypes.create_string_buffer(str(target.resolve()).encode())
    times_buffer = None if values is None else _times(values)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    target_fd = os.open(target, os.O_RDONLY) if by_fd else -1
    try:
        request = _request(
            path_buffer,
            times_buffer,
            dirfd=target_fd if by_fd else -100,
            flags=flags,
        )
        worker_workspace._metadata_broker_apply(
            library or Library(),
            -1,
            request,
            os.getpid(),
            root_fd,
            PurePosixPath(str(root.resolve())),
        )
    finally:
        if target_fd >= 0:
            os.close(target_fd)
        os.close(root_fd)


@pytest.fixture
def owned(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "scratch"
    root.mkdir()
    target = root / "owned"
    target.write_text("x", encoding="utf-8")
    return root, target


@pytest.mark.parametrize("by_fd", [False, True])
def test_exact_timestamps_on_path_and_descriptor(
    owned: tuple[Path, Path], by_fd: bool
) -> None:
    root, target = owned
    _apply(root, target, (11, 3, 22, 4), by_fd=by_fd)
    assert (target.stat().st_atime_ns, target.stat().st_mtime_ns) == (
        11_000_000_003,
        22_000_000_004,
    )


def test_kernel_now_omit_and_both_omit_semantics(owned: tuple[Path, Path]) -> None:
    root, target = owned
    os.utime(target, ns=(10_000_000_001, 20_000_000_002))
    before = target.stat()
    before_control = root / "before-now"
    after_control = root / "after-now"
    before_control.write_text("x", encoding="utf-8")
    after_control.write_text("x", encoding="utf-8")
    before_fd = os.open(before_control, os.O_RDONLY)
    after_fd = os.open(after_control, os.O_RDONLY)
    try:
        os.utime(before_fd, None)
        lower = os.fstat(before_fd).st_mtime_ns
        _apply(root, target, (0, UTIME_OMIT, 0, UTIME_NOW))
        os.utime(after_fd, None)
        upper = os.fstat(after_fd).st_mtime_ns
    finally:
        os.close(after_fd)
        os.close(before_fd)
    mixed = target.stat()
    assert mixed.st_atime_ns == before.st_atime_ns
    assert lower <= mixed.st_mtime_ns <= upper
    before_noop = target.stat()
    time.sleep(0.01)
    _apply(root, target, (0, UTIME_OMIT, 0, UTIME_OMIT), by_fd=True)
    after_noop = target.stat()
    assert (
        after_noop.st_atime_ns,
        after_noop.st_mtime_ns,
        after_noop.st_ctime_ns,
    ) == (
        before_noop.st_atime_ns,
        before_noop.st_mtime_ns,
        before_noop.st_ctime_ns,
    )


def test_null_times_means_both_now(owned: tuple[Path, Path]) -> None:
    root, target = owned
    before_control = root / "before-now"
    after_control = root / "after-now"
    before_control.write_text("x", encoding="utf-8")
    after_control.write_text("x", encoding="utf-8")
    before_fd = os.open(before_control, os.O_RDONLY)
    after_fd = os.open(after_control, os.O_RDONLY)
    try:
        os.utime(before_fd, None)
        lower = os.fstat(before_fd).st_mtime_ns
        _apply(root, target, None, by_fd=True)
        os.utime(after_fd, None)
        upper = os.fstat(after_fd).st_mtime_ns
    finally:
        os.close(after_fd)
        os.close(before_fd)
    info = target.stat()
    assert lower <= info.st_atime_ns <= upper
    assert lower <= info.st_mtime_ns <= upper


def test_linux_64_bit_timespec_abi_is_explicit() -> None:
    assert TIMESPEC_PAIR_SIZE == struct.calcsize("=qqqq") == 32
    assert ctypes.sizeof(ctypes.c_long) == 8
    assert decode_utimensat_timespec(_times((1, 2, 3, 4)).raw[:32]) == (
        (1, 2),
        (3, 4),
    )


@pytest.mark.parametrize("nsec", [-1, 1_000_000_000, UTIME_NOW + 1])
def test_invalid_nanoseconds_rejected(nsec: int) -> None:
    with pytest.raises(TimestampArgumentError):
        decode_utimensat_timespec(struct.pack("=qqqq", 0, nsec, 0, 0))


def test_invalid_size_rejected() -> None:
    with pytest.raises(TimestampArgumentError, match="timespec_size"):
        decode_utimensat_timespec(b"x" * (TIMESPEC_PAIR_SIZE - 1))


def test_invalid_timestamp_dispatch_leaves_target_unchanged(
    owned: tuple[Path, Path]
) -> None:
    root, target = owned
    before = _stamp(target)
    with pytest.raises(worker_workspace.WorkspaceError, match="invalid_nanoseconds"):
        _apply(root, target, (1, -1, 2, 0))
    assert _stamp(target) == before


def test_flags_rejected_without_changes(owned: tuple[Path, Path]) -> None:
    root, target = owned
    before = target.stat()
    with pytest.raises(worker_workspace.WorkspaceError, match="unsupported_flags"):
        _apply(root, target, (1, 0, 2, 0), flags=1)
    assert _stamp(target) == _stamp_info(before)


def _stamp(path: Path) -> tuple[int, int, int]:
    return _stamp_info(path.stat())


def _stamp_info(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_atime_ns, info.st_mtime_ns, info.st_ctime_ns


def test_ungranted_path_symlink_and_hardlink_are_unchanged(
    owned: tuple[Path, Path], tmp_path: Path
) -> None:
    root, target = owned
    outside = tmp_path / "outside"
    outside.write_text("o", encoding="utf-8")
    link = root / "link"
    link.symlink_to(outside)
    hard = root / "hard"
    os.link(target, hard)
    for candidate in (outside, link, hard):
        before = _stamp(candidate)
        with pytest.raises(worker_workspace.WorkspaceError):
            _apply(root, candidate, (1, 0, 2, 0))
        assert _stamp(candidate) == before


def test_foreign_owner_is_unchanged(
    owned: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, target = owned
    before = _stamp(target)
    monkeypatch.setattr(worker_workspace.os, "getuid", lambda: 999_991)
    with pytest.raises(worker_workspace.WorkspaceError, match="foreign_owner"):
        _apply(root, target, (1, 0, 2, 0))
    assert _stamp(target) == before


def test_deleted_descriptor_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    target = root / "owned"
    target.write_text("x", encoding="utf-8")
    target_fd = os.open(target, os.O_RDONLY)
    target.unlink()
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    times_buffer = _times((1, 0, 2, 0))
    try:
        with pytest.raises(worker_workspace.WorkspaceError, match="deleted_fd"):
            worker_workspace._metadata_broker_apply(
                Library(),
                -1,
                _request(None, times_buffer, dirfd=target_fd),
                os.getpid(),
                root_fd,
                PurePosixPath(str(root.resolve())),
            )
    finally:
        os.close(root_fd)
        os.close(target_fd)


def test_swapped_descriptor_reopen_is_rejected(
    owned: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, target = owned
    other = tmp_path / "other"
    other.write_text("y", encoding="utf-8")
    real_open = worker_workspace.os.open

    def swapped_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if str(path).startswith(f"/proc/{os.getpid()}/fd/"):
            return real_open(other, flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(worker_workspace.os, "open", swapped_open)
    before = _stamp(target)
    with pytest.raises(worker_workspace.WorkspaceError, match="fd_inode_drift"):
        _apply(root, target, (1, 0, 2, 0), by_fd=True)
    assert _stamp(target) == before


def test_stale_notification_and_unauthenticated_caller_are_unchanged(
    owned: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, target = owned
    before = _stamp(target)
    library = Library()
    library.stale = True
    with pytest.raises(worker_workspace.WorkspaceError, match="notification_stale"):
        _apply(root, target, (1, 0, 2, 0), library=library)
    assert _stamp(target) == before
    monkeypatch.setattr(
        worker_workspace,
        "_metadata_broker_authenticate_pid",
        lambda _pid, _child: (_ for _ in ()).throw(
            worker_workspace.WorkspaceError("metadata_broker_foreign_pid")
        ),
    )
    with pytest.raises(worker_workspace.WorkspaceError, match="foreign_pid"):
        _apply(root, target, (1, 0, 2, 0))
    assert _stamp(target) == before


def test_apply_helper_operates_on_real_descriptor(owned: tuple[Path, Path]) -> None:
    _root, target = owned
    fd = os.open(target, os.O_RDONLY)
    try:
        apply_utimensat_fd(fd, struct.pack("=qqqq", 5, 6, 7, 8))
    finally:
        os.close(fd)
    assert (target.stat().st_atime_ns, target.stat().st_mtime_ns) == (
        5_000_000_006,
        7_000_000_008,
    )


def test_real_node_and_python_operations_through_validation_broker(
    tmp_path: Path,
) -> None:
    assert shutil.which("node") is not None
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    worktree.mkdir()
    home.mkdir(mode=0o700)
    (home / "tmp").mkdir(mode=0o700)
    python_script = worktree / "timestamp_python.py"
    python_script.write_text(
        "import os\n"
        "p = os.path.join(os.environ['TMPDIR'], 'python')\n"
        "open(p, 'w').close()\n"
        "os.utime(p, (11, 22))\n"
        "s = os.stat(p)\n"
        "assert int(s.st_atime) == 11 and int(s.st_mtime) == 22\n",
        encoding="utf-8",
    )
    node_script = worktree / "timestamp_node.js"
    node_script.write_text(
        "const fs = require('fs');\n"
        "const p = process.env.TMPDIR + '/node';\n"
        "fs.writeFileSync(p, '');\n"
        "fs.utimesSync(p, 33, 44);\n"
        "const s = fs.statSync(p);\n"
        "if (s.atimeMs !== 33000 || s.mtimeMs !== 44000) process.exit(2);\n",
        encoding="utf-8",
    )
    workspace = worker_workspace.WorkerWorkspace(
        request_id=f"timestamp-{os.getpid()}",
        repo=tmp_path,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    results = worker_workspace.run_validations(
        workspace,
        [f"python3 {python_script}", f"node {node_script}"],
        timeout_seconds=180,
    )
    assert [row["returncode"] for row in results] == [0, 0], results
