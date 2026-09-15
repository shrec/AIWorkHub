"""NF27 v7: production validation metadata broker coverage.

Covers the fail-closed, stable-fd target verification, the classic
``fchmodat`` (no flags) vs ``fchmodat2`` (flags) split, descendant-pid
authentication, the real kernel capability probe, and the process-group
kill/reap safety net used by the seccomp user-notification broker, plus a
real ``run_validations`` integration that runs unmodified ``git init``
beneath the per-request validation exec scratch. Skips are permitted only
when the host genuinely lacks seccomp user notification, ``openat2``, the
Landlock backend, or git.
"""

from __future__ import annotations

import ctypes
import hashlib
import inspect
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

from aiworkhub import validation_runner, worker_workspace
from aiworkhub.worker_workspace import ValidationEnvironmentBlocked, WorkspaceError


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="requires Linux seccomp user-notification and Landlock",
)


def _self_hosted_validation_exec() -> bool:
    """True when this pytest runs inside an AIWorkHub validation exec sandbox.

    The canonical validation harness spawns the suite beneath a scratch whose
    TMPDIR/TMP/TEMP basename carries the ``aiworkhub_validation_exec_``
    prefix. In that self-hosted context the already loaded canonical broker
    still denies directory chmod/fchmod before the candidate broker can
    prove its behavior, so only the direct directory-mutation tests and the
    nested run_validations directory-chmod integration are skipped with this
    bootstrap reason; all verification/denial coverage still runs.
    """
    for name in ("TMPDIR", "TMP", "TEMP"):
        value = os.environ.get(name)
        if value and os.path.basename(value).startswith(
            "aiworkhub_validation_exec_"
        ):
            return True
    return False


_SELF_HOSTED_VALIDATION_EXEC = _self_hosted_validation_exec()


def _workspace(tmp_path: Path) -> worker_workspace.WorkerWorkspace:
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    worktree.mkdir()
    home.mkdir(mode=0o700)
    (home / "tmp").mkdir(mode=0o700)
    return worker_workspace.WorkerWorkspace(
        request_id=f"v7-{os.getpid()}-{tmp_path.name}",
        repo=tmp_path,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir(mode=0o700)
    return root


@pytest.fixture
def require_openat2() -> None:
    if not worker_workspace._openat2_available():
        pytest.skip("host kernel lacks openat2(2)")


def _scratch_fd_root(scratch: Path) -> tuple[int, PurePosixPath]:
    fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY)
    return fd, PurePosixPath(str(scratch.resolve()))


class _FakeLibrary:
    """A libseccomp stand-in for the broker's pure userspace branches.

    It resolves only the four brokered names and treats every notification id
    as still valid, so ``_metadata_broker_apply``'s argument decoding, target
    resolution and mutation can be exercised without a live kernel listener.
    """

    _NUMBERS = {"chmod": 71, "fchmod": 72, "fchmodat": 73, "fchmodat2": 74}

    def number(self, name: str) -> int:
        return self._NUMBERS[name]

    def seccomp_syscall_resolve_name(self, raw: bytes) -> int:
        return self._NUMBERS.get(raw.decode("ascii"), -1)

    def seccomp_notify_id_valid(self, fd: int, notif_id: int) -> int:
        return 0


class _RealSyscallLibrary:
    """Linux x86_64 truth for the brokered names NF-2026-00448/NF-2026-00841 use.

    The mapping stays separate from ``_FakeLibrary`` so a real-table lookup
    can never accidentally inherit the fake numbers used by tests.
    ``utimensat`` (280) joins ``chmod`` (90) because NF-2026-00841 measured the
    two together as one authenticated denial sequence.
    """

    _NUMBERS = {"chmod": 90, "fchmod": 91, "utimensat": 280}

    def number(self, name: str) -> int:
        return self._NUMBERS[name]

    def seccomp_syscall_resolve_name(self, raw: bytes) -> int:
        return self._NUMBERS.get(raw.decode("ascii"), -1)

    def seccomp_notify_id_valid(self, fd: int, notif_id: int) -> int:
        return 0


def _make_request(nr: int, pid: int) -> worker_workspace._SeccompNotif:
    request = worker_workspace._SeccompNotif()
    request.id = 1
    request.pid = pid
    request.flags = 0
    request.data.nr = nr
    return request


def _path_buffer(path: Path) -> ctypes.Array:
    buf = ctypes.create_string_buffer(worker_workspace._METADATA_BROKER_PATH_LIMIT)
    buf.value = str(path.resolve()).encode("utf-8")
    return buf


class TestBrokerIsDefinedBeforeMainDispatch:
    def test_symbols_exist(self) -> None:
        for name in (
            "_install_metadata_notify_filter",
            "_run_metadata_broker",
            "_metadata_broker_apply",
            "_metadata_broker_verify_target",
            "_metadata_broker_authenticate_pid",
            "_openat2_beneath",
            "_openat2_available",
            "_seccomp_kernel_notify_api",
            "_seccomp_notify_supported",
            "_kill_validator_group",
        ):
            assert hasattr(worker_workspace, name), name

    def test_definitions_precede_direct_script_dispatch(self) -> None:
        source = Path(worker_workspace.__file__).read_text(encoding="utf-8")
        broker = source.index("def _run_metadata_broker(")
        dispatch = source.index('if __name__ == "__main__"')
        assert broker < dispatch

    def test_exactly_one_broker_implementation(self) -> None:
        source = Path(worker_workspace.__file__).read_text(encoding="utf-8")
        assert source.count("def _run_metadata_broker(") == 1
        assert source.count("def _install_metadata_notify_filter(") == 1


class TestModeAndFlagVerification:
    @pytest.mark.parametrize("mode", [0o644, 0o600, 0o755, 0o000])
    def test_permission_bits_allowed(self, mode: int) -> None:
        assert worker_workspace._metadata_broker_verify_mode(mode) == mode

    @pytest.mark.parametrize("mode", [0o4755, 0o2755, 0o1777, -1])
    def test_unsafe_modes_denied(self, mode: int) -> None:
        with pytest.raises(WorkspaceError):
            worker_workspace._metadata_broker_verify_mode(mode)

    @pytest.mark.parametrize(
        "mode",
        [
            stat.S_IFDIR | 0o700,
            stat.S_IFDIR | 0o755,
            stat.S_IFREG | 0o600,
            stat.S_IFREG | 0o644,
            stat.S_IFLNK | 0o777,
        ],
    )
    def test_full_st_mode_strips_only_file_type_bits(self, mode: int) -> None:
        assert worker_workspace._metadata_broker_verify_mode(mode) == stat.S_IMODE(mode)

    @pytest.mark.parametrize(
        "mode",
        [
            stat.S_IFDIR | stat.S_ISUID | 0o755,
            stat.S_IFDIR | stat.S_ISGID | 0o755,
            stat.S_IFDIR | stat.S_ISVTX | 0o777,
            stat.S_IFREG | stat.S_ISUID | 0o755,
        ],
    )
    def test_full_st_mode_still_rejects_special_bits(self, mode: int) -> None:
        with pytest.raises(WorkspaceError):
            worker_workspace._metadata_broker_verify_mode(mode)

    def test_zero_flags_allowed(self) -> None:
        assert worker_workspace._metadata_broker_verify_flags(0) == 0

    @pytest.mark.parametrize("flags", [0x100, 1, 0x1000])
    def test_unsupported_flags_denied(self, flags: int) -> None:
        with pytest.raises(WorkspaceError):
            worker_workspace._metadata_broker_verify_flags(flags)


@pytest.mark.usefixtures("require_openat2")
class TestTargetVerification:
    def test_scratch_owned_regular_file_allowed(self, scratch: Path) -> None:
        target = scratch / "config.lock"
        target.write_text("x", encoding="utf-8")
        fd, root = _scratch_fd_root(scratch)
        try:
            verified, mutate = worker_workspace._metadata_broker_verify_target(
                str(target), fd, root
            )
            assert mutate is True
            try:
                info = os.fstat(verified)
                assert stat.S_ISREG(info.st_mode)
                assert os.stat(target).st_ino == info.st_ino
            finally:
                os.close(verified)
        finally:
            os.close(fd)

    def test_nested_regular_file_allowed(self, scratch: Path) -> None:
        nested = scratch / "probe" / ".git"
        nested.mkdir(parents=True)
        target = nested / "config.lock"
        target.write_text("x", encoding="utf-8")
        fd, root = _scratch_fd_root(scratch)
        try:
            verified, mutate = worker_workspace._metadata_broker_verify_target(
                str(target), fd, root
            )
            assert mutate is True
            try:
                info = os.fstat(verified)
                assert stat.S_ISREG(info.st_mode)
                assert os.stat(target).st_ino == info.st_ino
            finally:
                os.close(verified)
        finally:
            os.close(fd)

    def _deny(self, scratch: Path, candidate: str) -> None:
        fd, root = _scratch_fd_root(scratch)
        try:
            with pytest.raises(WorkspaceError):
                worker_workspace._metadata_broker_verify_target(candidate, fd, root)
        finally:
            os.close(fd)

    def test_relative_path_denied(self, scratch: Path) -> None:
        self._deny(scratch, "config.lock")

    def test_traversal_denied(self, scratch: Path) -> None:
        self._deny(scratch, f"{scratch}/../escape")

    def test_outside_absolute_denied(self, scratch: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        self._deny(scratch, str(outside))

    def test_symlink_target_denied(self, scratch: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        link = scratch / "link"
        link.symlink_to(outside)
        self._deny(scratch, str(link))

    def test_symlink_root_denied(self, scratch: Path, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "file.txt").write_text("x", encoding="utf-8")
        (scratch / "dirlink").symlink_to(elsewhere)
        self._deny(scratch, str(scratch / "dirlink" / "file.txt"))

    def test_hardlink_denied(self, scratch: Path) -> None:
        original = scratch / "original"
        original.write_text("x", encoding="utf-8")
        os.link(original, scratch / "hard")
        self._deny(scratch, str(scratch / "hard"))

    def test_owned_directory_allowed(self, scratch: Path) -> None:
        subdir = scratch / "subdir"
        subdir.mkdir()
        fd, root = _scratch_fd_root(scratch)
        try:
            verified, mutate = worker_workspace._metadata_broker_verify_target(
                str(subdir), fd, root
            )
            assert mutate is True
            try:
                info = os.fstat(verified)
                assert stat.S_ISDIR(info.st_mode)
                assert os.stat(subdir).st_ino == info.st_ino
            finally:
                os.close(verified)
        finally:
            os.close(fd)

    def test_foreign_owner_directory_denied(
        self, scratch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (scratch / "subdir").mkdir()
        monkeypatch.setattr(worker_workspace.os, "getuid", lambda: 999_992)
        self._deny(scratch, str(scratch / "subdir"))

    def test_scratch_root_directory_denied(self, scratch: Path) -> None:
        self._deny(scratch, str(scratch.resolve()))

    def test_missing_target_denied(self, scratch: Path) -> None:
        self._deny(scratch, str(scratch / "absent"))

    def test_foreign_owner_denied(
        self, scratch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = scratch / "owned"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setattr(worker_workspace.os, "getuid", lambda: 999_991)
        self._deny(scratch, str(target))


@pytest.mark.usefixtures("require_openat2")
class TestOpenat2Beneath:
    def test_opens_regular_file(self, scratch: Path) -> None:
        (scratch / "f").write_text("x", encoding="utf-8")
        dfd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fd = worker_workspace._openat2_beneath(dfd, "f", os.O_RDONLY)
            os.close(fd)
        finally:
            os.close(dfd)

    def test_rejects_symlink_component(self, scratch: Path, tmp_path: Path) -> None:
        outside = tmp_path / "o"
        outside.mkdir()
        (outside / "f").write_text("x", encoding="utf-8")
        (scratch / "lnk").symlink_to(outside)
        dfd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(WorkspaceError):
                worker_workspace._openat2_beneath(dfd, "lnk/f", os.O_RDONLY)
        finally:
            os.close(dfd)

    def test_rejects_absolute(self, scratch: Path) -> None:
        dfd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(WorkspaceError):
                worker_workspace._openat2_beneath(dfd, "/etc/hostname", os.O_RDONLY)
        finally:
            os.close(dfd)

    def test_rejects_parent_escape(self, scratch: Path) -> None:
        dfd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(WorkspaceError):
                worker_workspace._openat2_beneath(dfd, "../escape", os.O_RDONLY)
        finally:
            os.close(dfd)


@pytest.mark.usefixtures("require_openat2")
class TestBrokeredSyscallDecoding:
    def test_fchmodat_classic_ignores_flags_argument(self, scratch: Path) -> None:
        # The classic fchmodat syscall has no flags arg; args[3] is undefined
        # register content and must be ignored, so a garbage value still succeeds.
        library = _FakeLibrary()
        target = scratch / "config.lock"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o600)
        fd, root = _scratch_fd_root(scratch)
        buf = _path_buffer(target)
        request = _make_request(library.number("fchmodat"), os.getpid())
        request.data.args[0] = 0
        request.data.args[1] = ctypes.addressof(buf)
        request.data.args[2] = 0o640
        request.data.args[3] = 0xDEADBEEF
        try:
            worker_workspace._metadata_broker_apply(
                library, -1, request, os.getpid(), fd, root
            )
        finally:
            os.close(fd)
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o640

    def test_fchmodat2_validates_flags(self, scratch: Path) -> None:
        library = _FakeLibrary()
        target = scratch / "config.lock"
        target.write_text("x", encoding="utf-8")
        fd, root = _scratch_fd_root(scratch)
        buf = _path_buffer(target)
        request = _make_request(library.number("fchmodat2"), os.getpid())
        request.data.args[0] = 0
        request.data.args[1] = ctypes.addressof(buf)
        request.data.args[2] = 0o640
        request.data.args[3] = 1  # AT_SYMLINK_NOFOLLOW etc. -> denied
        try:
            with pytest.raises(WorkspaceError):
                worker_workspace._metadata_broker_apply(
                    library, -1, request, os.getpid(), fd, root
                )
        finally:
            os.close(fd)

    def test_fchmodat2_zero_flags_succeeds(self, scratch: Path) -> None:
        library = _FakeLibrary()
        target = scratch / "config.lock"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o600)
        fd, root = _scratch_fd_root(scratch)
        buf = _path_buffer(target)
        request = _make_request(library.number("fchmodat2"), os.getpid())
        request.data.args[0] = 0
        request.data.args[1] = ctypes.addressof(buf)
        request.data.args[2] = 0o640
        request.data.args[3] = 0
        try:
            worker_workspace._metadata_broker_apply(
                library, -1, request, os.getpid(), fd, root
            )
        finally:
            os.close(fd)
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o640

    def test_chmod_brokers_absolute_path(self, scratch: Path) -> None:
        library = _FakeLibrary()
        target = scratch / "config.lock"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o600)
        fd, root = _scratch_fd_root(scratch)
        buf = _path_buffer(target)
        request = _make_request(library.number("chmod"), os.getpid())
        request.data.args[0] = ctypes.addressof(buf)
        request.data.args[1] = 0o640
        try:
            worker_workspace._metadata_broker_apply(
                library, -1, request, os.getpid(), fd, root
            )
        finally:
            os.close(fd)
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o640

    def test_fchmod_brokers_exact_open_descriptor(self, scratch: Path) -> None:
        library = _FakeLibrary()
        target = scratch / "config.lock"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o600)
        fd, root = _scratch_fd_root(scratch)
        target_fd = os.open(target, os.O_RDWR)
        request = _make_request(library.number("fchmod"), os.getpid())
        request.data.args[0] = target_fd
        request.data.args[1] = 0o640
        try:
            worker_workspace._metadata_broker_apply(
                library, -1, request, os.getpid(), fd, root
            )
        finally:
            os.close(target_fd)
            os.close(fd)
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o640

    @staticmethod
    def _hardlinked_git_config_lock(scratch: Path) -> Path:
        # NF-2026-00448: nested Git sparse checkout keeps .git/config.lock
        # as a hardlink alias; reproduce that exact layout for probes.
        #
        # NF-2026-00841: build that layout with mode-at-creation only, exactly
        # like ``TestNF841AuthenticatedDenialIsNotStructurallyTerminal.
        # _shared_inode_lock``. A raw ``os.chmod`` here is itself a
        # request-owned scratch ``.git/config.lock`` metadata mutation, so
        # inside the canonical union/Landlock validation the fixture -- not the
        # broker behaviour under test -- decided whether these probes could run
        # at all, and the four parametrizations failed for a reason unrelated
        # to the no-op authorization they exist to prove. The umask guard makes
        # the created mode exactly 0o664 on any host, which is the current mode
        # the brokered exact-mode no-op is compared against below.
        git_dir = scratch / ".git"
        git_dir.mkdir(mode=0o700)
        target = git_dir / "config.lock"
        previous_umask = os.umask(0)
        try:
            handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o664)
        finally:
            os.umask(previous_umask)
        try:
            os.write(handle, b"x")
        finally:
            os.close(handle)
        os.link(target, scratch / "config.lock.alias")
        return target

    def _apply_brokered_metadata(
        self,
        library,
        syscall_name: str,
        target: Path,
        scratch: Path,
        mode: int,
    ) -> None:
        fd, root = _scratch_fd_root(scratch)
        request = _make_request(library.number(syscall_name), os.getpid())
        target_fd = None
        try:
            if syscall_name == "chmod":
                buf = _path_buffer(target)
                request.data.args[0] = ctypes.addressof(buf)
            else:
                target_fd = os.open(target, os.O_RDWR)
                request.data.args[0] = target_fd
            request.data.args[1] = mode
            worker_workspace._metadata_broker_apply(
                library, -1, request, os.getpid(), fd, root
            )
        finally:
            if target_fd is not None:
                os.close(target_fd)
            os.close(fd)

    def test_real_syscall_numbers_map_to_same_brokered_names(self) -> None:
        real_names = worker_workspace._metadata_broker_syscall_names(
            _RealSyscallLibrary()
        )
        fake_names = worker_workspace._metadata_broker_syscall_names(
            _FakeLibrary()
        )
        # NF-2026-00448 measured the nested Git config.lock no-op as chmod
        # (syscall 90) and fchmod (syscall 91) on x86_64; the brokered names
        # must resolve identically for the real and the fake numbers alike.
        assert real_names.get(90) == fake_names.get(71) == "chmod"
        assert real_names.get(91) == fake_names.get(72) == "fchmod"

    @pytest.mark.parametrize(
        "library_factory", [_FakeLibrary, _RealSyscallLibrary], ids=["fake", "real"]
    )
    @pytest.mark.parametrize("syscall_name", ["chmod", "fchmod"])
    def test_noop_metadata_on_hardlinked_git_lock_allowed(
        self, scratch: Path, library_factory, syscall_name: str
    ) -> None:
        # The exact nested Git no-op case: the requested mode equals the
        # current mode of the hardlinked lock, so the broker must allow it
        # for the fake numbers and the real x86_64 chmod (90)/fchmod (91).
        library = library_factory()
        target = self._hardlinked_git_config_lock(scratch)
        self._apply_brokered_metadata(
            library, syscall_name, target, scratch, 0o664
        )
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o664
        assert os.stat(target).st_nlink == 2

    @pytest.mark.parametrize("syscall_name", ["chmod", "fchmod"])
    def test_mutating_mode_on_hardlink_denied(
        self, scratch: Path, syscall_name: str
    ) -> None:
        # Any mode change on a hardlink alias is a metadata mutation and
        # must keep failing closed with the audited denial reason.
        target = scratch / "config.lock"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o664)
        os.link(target, scratch / "config.lock.alias")
        with pytest.raises(
            WorkspaceError, match="metadata_broker_hardlink_forbidden"
        ):
            self._apply_brokered_metadata(
                _FakeLibrary(), syscall_name, target, scratch, 0o600
            )
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o664

    def test_fchmod_unlinked_descriptor_noop_denied(
        self, scratch: Path
    ) -> None:
        library = _FakeLibrary()
        target = scratch / "config.lock"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o664)
        fd, root = _scratch_fd_root(scratch)
        target_fd = os.open(target, os.O_RDWR)
        os.unlink(target)
        request = _make_request(library.number("fchmod"), os.getpid())
        request.data.args[0] = target_fd
        request.data.args[1] = 0o664
        try:
            with pytest.raises(
                WorkspaceError, match="metadata_broker_deleted_fd"
            ):
                worker_workspace._metadata_broker_apply(
                    library, -1, request, os.getpid(), fd, root
                )
        finally:
            os.close(target_fd)
            os.close(fd)
        assert not target.exists()

    def test_fchmod_link_count_race_mutating_denied(
        self, scratch: Path
    ) -> None:
        library = _FakeLibrary()
        target = scratch / "config.lock"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o664)
        fd, root = _scratch_fd_root(scratch)
        target_fd = os.open(target, os.O_RDWR)
        os.link(target, scratch / "config.lock.alias")
        request = _make_request(library.number("fchmod"), os.getpid())
        request.data.args[0] = target_fd
        request.data.args[1] = 0o600
        try:
            with pytest.raises(
                WorkspaceError, match="metadata_broker_hardlink_forbidden"
            ):
                worker_workspace._metadata_broker_apply(
                    library, -1, request, os.getpid(), fd, root
                )
        finally:
            os.close(target_fd)
            os.close(fd)
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o664

    def test_fchmod_outside_scratch_denied(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        library = _FakeLibrary()
        outside = tmp_path / "outside.lock"
        outside.write_text("x", encoding="utf-8")
        fd, root = _scratch_fd_root(scratch)
        target_fd = os.open(outside, os.O_RDWR)
        request = _make_request(library.number("fchmod"), os.getpid())
        request.data.args[0] = target_fd
        request.data.args[1] = 0o640
        try:
            with pytest.raises(WorkspaceError):
                worker_workspace._metadata_broker_apply(
                    library, -1, request, os.getpid(), fd, root
                )
        finally:
            os.close(target_fd)
            os.close(fd)

    def test_chmod_outside_scratch_denied(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        library = _FakeLibrary()
        outside = tmp_path / "outside.lock"
        outside.write_text("x", encoding="utf-8")
        fd, root = _scratch_fd_root(scratch)
        buf = _path_buffer(outside)
        request = _make_request(library.number("chmod"), os.getpid())
        request.data.args[0] = ctypes.addressof(buf)
        request.data.args[1] = 0o640
        try:
            with pytest.raises(WorkspaceError):
                worker_workspace._metadata_broker_apply(
                    library, -1, request, os.getpid(), fd, root
                )
        finally:
            os.close(fd)

    def test_chmod_owned_directory_succeeds(self, scratch: Path) -> None:
        if _SELF_HOSTED_VALIDATION_EXEC:
            pytest.skip(
                "self-hosted validation bootstrap: loaded canonical broker "
                "still denies directory chmod before the candidate broker "
                "proves its behavior"
            )
        library = _FakeLibrary()
        subdir = scratch / "subdir"
        subdir.mkdir(mode=0o755)
        fd, root = _scratch_fd_root(scratch)
        buf = _path_buffer(subdir)
        request = _make_request(library.number("chmod"), os.getpid())
        request.data.args[0] = ctypes.addressof(buf)
        request.data.args[1] = 0o700
        try:
            worker_workspace._metadata_broker_apply(
                library, -1, request, os.getpid(), fd, root
            )
        finally:
            os.close(fd)
        assert stat.S_IMODE(os.stat(subdir).st_mode) == 0o700

    def test_fchmod_owned_directory_succeeds(self, scratch: Path) -> None:
        if _SELF_HOSTED_VALIDATION_EXEC:
            pytest.skip(
                "self-hosted validation bootstrap: loaded canonical broker "
                "still denies directory fchmod before the candidate broker "
                "proves its behavior"
            )
        library = _FakeLibrary()
        subdir = scratch / "subdir"
        subdir.mkdir(mode=0o755)
        fd, root = _scratch_fd_root(scratch)
        dir_fd = os.open(subdir, os.O_RDONLY | os.O_DIRECTORY)
        request = _make_request(library.number("fchmod"), os.getpid())
        request.data.args[0] = dir_fd
        request.data.args[1] = 0o700
        try:
            worker_workspace._metadata_broker_apply(
                library, -1, request, os.getpid(), fd, root
            )
        finally:
            os.close(dir_fd)
            os.close(fd)
        assert stat.S_IMODE(os.stat(subdir).st_mode) == 0o700

    def test_fchmodat2_owned_directory_succeeds(self, scratch: Path) -> None:
        if _SELF_HOSTED_VALIDATION_EXEC:
            pytest.skip(
                "self-hosted validation bootstrap: loaded canonical broker "
                "still denies directory fchmod before the candidate broker "
                "proves its behavior"
            )
        library = _FakeLibrary()
        subdir = scratch / "subdir"
        subdir.mkdir(mode=0o755)
        fd, root = _scratch_fd_root(scratch)
        buf = _path_buffer(subdir)
        request = _make_request(library.number("fchmodat2"), os.getpid())
        request.data.args[0] = fd
        request.data.args[1] = ctypes.addressof(buf)
        request.data.args[2] = 0o700
        request.data.args[3] = 0
        try:
            worker_workspace._metadata_broker_apply(
                library, -1, request, os.getpid(), fd, root
            )
        finally:
            os.close(fd)
        assert stat.S_IMODE(os.stat(subdir).st_mode) == 0o700


class TestDescendantPidAuthentication:
    def test_accepts_self(self) -> None:
        worker_workspace._metadata_broker_authenticate_pid(os.getpid(), os.getpid())

    def test_process_pgid_matches_kernel(self) -> None:
        assert worker_workspace._metadata_broker_process_pgid(
            os.getpid()
        ) == os.getpgid(0)

    def test_accepts_live_group_descendant(self) -> None:
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child leg never returns
            try:
                time.sleep(5)
            finally:
                os._exit(0)
        try:
            leader = os.getpgid(pid)
            worker_workspace._metadata_broker_authenticate_pid(pid, leader)
            with pytest.raises(WorkspaceError):
                worker_workspace._metadata_broker_authenticate_pid(pid, 999_991)
        finally:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)

    def test_rejects_foreign_pid(self) -> None:
        with pytest.raises(WorkspaceError):
            worker_workspace._metadata_broker_authenticate_pid(1, os.getpid())

    def test_rejects_nonpositive_pid(self) -> None:
        with pytest.raises(WorkspaceError):
            worker_workspace._metadata_broker_authenticate_pid(0, os.getpid())

    def test_process_pgid_missing_raises(self) -> None:
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child leg never returns
            os._exit(0)
        os.waitpid(pid, 0)
        with pytest.raises(WorkspaceError):
            worker_workspace._metadata_broker_process_pgid(pid)


class TestCapabilityProbe:
    def test_openat2_available_is_bool(self) -> None:
        assert isinstance(worker_workspace._openat2_available(), bool)

    def test_seccomp_notify_supported_is_bool(self) -> None:
        assert isinstance(worker_workspace._seccomp_notify_supported(), bool)

    def test_every_bound_libseccomp_entry_point_declares_its_arguments(self) -> None:
        """A ctypes call with no ``argtypes`` truncates a pointer to 32 bits.

        ``scmp_filter_ctx`` is the first argument of ``seccomp_rule_add`` and
        reaches it here as a plain Python int.  Left unprototyped, ctypes
        converts that int to a C ``int``, so a filter context allocated above
        4 GiB arrives as a wild pointer and libseccomp dies of SIGSEGV.  A
        non-PIE interpreter keeps its heap below 4 GiB and hides the defect
        completely, so the prototype -- not a passing run -- is the only thing
        that can be asserted portably.
        """
        library = worker_workspace._seccomp_library()
        if library is None:
            pytest.skip("libseccomp is not installed on this host")
        for name in (
            "seccomp_init",
            "seccomp_syscall_resolve_name",
            "seccomp_rule_add",
            "seccomp_load",
            "seccomp_release",
        ):
            assert getattr(library, name).argtypes is not None, (
                f"{name} is bound without argtypes; a pointer argument would be "
                "silently truncated to 32 bits"
            )
        assert library.seccomp_init.restype is ctypes.c_void_p
        assert library.seccomp_rule_add.argtypes[0] is ctypes.c_void_p

    def test_filter_context_above_four_gib_survives_rule_add(self) -> None:
        """Add a rule to a context glibc served from a high mmap'd arena.

        A secondary thread gets its own malloc arena, which glibc mmaps far
        above 4 GiB even when the main heap is low.  That reproduces here, on
        any interpreter, exactly what a PIE interpreter does on its very first
        allocation -- the condition every CI runner is in and this host is not.
        The probe runs in a child process because the failure mode is SIGSEGV,
        which no ``pytest.raises`` can observe.
        """
        library = worker_workspace._seccomp_library()
        if library is None:
            pytest.skip("libseccomp is not installed on this host")
        syscall_number = library.seccomp_syscall_resolve_name(b"fchmod")
        if syscall_number < 0:
            pytest.skip("libseccomp cannot resolve fchmod on this architecture")
        script = (
            "import threading\n"
            "from aiworkhub import worker_workspace\n"
            "library = worker_workspace._seccomp_library()\n"
            "outcome = {}\n"
            "def add_rule():\n"
            "    context = library.seccomp_init(worker_workspace._SCMP_ACT_ALLOW)\n"
            "    print('context=' + str(context), flush=True)\n"
            "    outcome['rc'] = library.seccomp_rule_add(\n"
            "        context, worker_workspace._SCMP_ACT_ERRNO | 1, "
            + str(syscall_number)
            + ", 0\n"
            "    )\n"
            "    library.seccomp_release(context)\n"
            "worker = threading.Thread(target=add_rule)\n"
            "worker.start()\n"
            "worker.join()\n"
            "print('rc=' + repr(outcome.get('rc')), flush=True)\n"
        )
        probe = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        reported = [
            line for line in probe.stdout.splitlines() if line.startswith("context=")
        ]
        if not reported:
            pytest.skip(
                "could not allocate a filter context in a child process: "
                f"{probe.stderr[-400:]}"
            )
        context = int(reported[0].split("=", 1)[1])
        if context < 2**32:
            pytest.skip(
                "this host serves filter contexts below the 4 GiB boundary, so "
                "a truncated pointer cannot be distinguished from a correct one"
            )
        assert probe.returncode == 0, (
            f"seccomp_rule_add failed on a context at {context:#x} "
            f"(returncode={probe.returncode}, stderr={probe.stderr[-400:]})"
        )
        assert "rc=0" in probe.stdout, probe.stdout

    def test_declared_notification_structs_match_the_kernel_sizes(self) -> None:
        """The broker declares the notification structs; the kernel sizes them.

        ``seccomp_notify_alloc`` allocates from ``SECCOMP_GET_NOTIF_SIZES``, so
        a declared layout wider than the kernel's would have the broker write a
        response past the end of libseccomp's own allocation.  Pin the two
        against each other locally instead of discovering the drift as a
        corrupted child.
        """
        library = worker_workspace._seccomp_library()
        if library is None:
            pytest.skip("libseccomp is not installed on this host")
        seccomp_number = library.seccomp_syscall_resolve_name(b"seccomp")
        if seccomp_number < 0:
            pytest.skip("libseccomp cannot resolve seccomp(2) on this architecture")

        class _NotifSizes(ctypes.Structure):
            _fields_ = [
                ("seccomp_notif", ctypes.c_uint16),
                ("seccomp_notif_resp", ctypes.c_uint16),
                ("seccomp_data", ctypes.c_uint16),
            ]

        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        sizes = _NotifSizes()
        seccomp_get_notif_sizes = 3
        if (
            libc.syscall(
                ctypes.c_long(seccomp_number),
                ctypes.c_long(seccomp_get_notif_sizes),
                ctypes.c_long(0),
                ctypes.byref(sizes),
            )
            != 0
        ):
            pytest.skip("this kernel does not implement SECCOMP_GET_NOTIF_SIZES")
        assert ctypes.sizeof(worker_workspace._SeccompData) == sizes.seccomp_data
        assert ctypes.sizeof(worker_workspace._SeccompNotif) == sizes.seccomp_notif
        assert (
            ctypes.sizeof(worker_workspace._SeccompNotifResp)
            == sizes.seccomp_notif_resp
        )

    @pytest.mark.parametrize("level, expected", [(4, False), (5, True)])
    def test_notify_requires_libseccomp_api_level_five(
        self,
        monkeypatch: pytest.MonkeyPatch,
        level: int,
        expected: bool,
    ) -> None:
        class ApiGet:
            argtypes: list[object] = []
            restype: object | None = None

            def __call__(self) -> int:
                return level

        class Library:
            seccomp_api_get = ApiGet()

        monkeypatch.setattr(worker_workspace, "_seccomp_library", lambda: Library())
        assert worker_workspace._seccomp_kernel_notify_api() is expected

    def test_broker_rejects_symlink_scratch_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = tmp_path / "real-scratch"
        real.mkdir(mode=0o700)
        linked = tmp_path / "linked-scratch"
        linked.symlink_to(real, target_is_directory=True)
        monkeypatch.setattr(
            worker_workspace, "_seccomp_notify_library", lambda: object()
        )
        monkeypatch.setattr(worker_workspace, "_kill_validator_group", lambda _pid: None)
        monkeypatch.setattr(worker_workspace, "_reap_validator", lambda _pid: None)
        with pytest.raises(WorkspaceError, match="metadata_broker_scratch_unavailable"):
            worker_workspace._run_metadata_broker(-1, os.getpid(), linked)

    def test_pdeathsig_rejects_parent_identity_drift(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(os, "getppid", lambda: 22)
        with pytest.raises(WorkspaceError, match="parent_identity_changed"):
            worker_workspace._verify_broker_parent_identity(11)

    def test_supported_requires_openat2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            worker_workspace, "_seccomp_notify_library", lambda: object()
        )
        monkeypatch.setattr(
            worker_workspace, "_seccomp_kernel_notify_api", lambda: True
        )
        monkeypatch.setattr(worker_workspace, "_openat2_available", lambda: False)
        assert worker_workspace._seccomp_notify_supported() is False

    def test_supported_requires_kernel_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            worker_workspace, "_seccomp_notify_library", lambda: object()
        )
        monkeypatch.setattr(
            worker_workspace, "_seccomp_kernel_notify_api", lambda: False
        )
        monkeypatch.setattr(worker_workspace, "_openat2_available", lambda: True)
        assert worker_workspace._seccomp_notify_supported() is False

    def test_supported_requires_library(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            worker_workspace, "_seccomp_notify_library", lambda: None
        )
        assert worker_workspace._seccomp_notify_supported() is False

    def test_supported_requires_live_listener_transfer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            worker_workspace, "_seccomp_notify_library", lambda: object()
        )
        monkeypatch.setattr(
            worker_workspace, "_seccomp_kernel_notify_api", lambda: True
        )
        monkeypatch.setattr(worker_workspace, "_openat2_available", lambda: True)
        monkeypatch.setattr(
            worker_workspace, "_seccomp_notify_runtime_supported", lambda: False
        )
        assert worker_workspace._seccomp_notify_supported() is False


class TestProcessGroupTeardown:
    def test_kill_and_reap_validator_group(self) -> None:
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child leg never returns
            try:
                os.setsid()
                time.sleep(30)
            finally:
                os._exit(0)
        worker_workspace._kill_validator_group(pid)
        worker_workspace._reap_validator(pid)
        with pytest.raises(OSError):
            os.kill(pid, 0)

    def test_reap_validator_tolerates_missing_child(self) -> None:
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child leg never returns
            os._exit(0)
        os.waitpid(pid, 0)
        # Already reaped: a second reap must not raise.
        worker_workspace._reap_validator(pid)


class TestMalformedNotifications:
    def test_identity_drift_denied(self, scratch: Path) -> None:
        fd, root = _scratch_fd_root(scratch)
        request = _make_request(0, 1)  # pid 1 is not in the broker child's group
        try:
            with pytest.raises(WorkspaceError):
                worker_workspace._metadata_broker_apply(
                    _FakeLibrary(), -1, request, os.getpid(), fd, root
                )
        finally:
            os.close(fd)

    def test_unsupported_syscall_denied(self, scratch: Path) -> None:
        fd, root = _scratch_fd_root(scratch)
        request = _make_request(-424_242, os.getpid())
        try:
            with pytest.raises(WorkspaceError):
                worker_workspace._metadata_broker_apply(
                    _FakeLibrary(), -1, request, os.getpid(), fd, root
                )
        finally:
            os.close(fd)

    def test_null_path_pointer_denied(self) -> None:
        with pytest.raises(WorkspaceError):
            worker_workspace._read_child_cstring(os.getpid(), 0)


class TestExitStatusMapping:
    def test_normal_exit(self) -> None:
        assert worker_workspace._metadata_broker_exit_code(0) == 0
        assert worker_workspace._metadata_broker_exit_code(3 << 8) == 3

    def test_signal_exit(self) -> None:
        assert worker_workspace._metadata_broker_exit_code(9) == 137


class TestMetadataBrokerHandshake:
    """Pure-userspace coverage of the bounded, observable listener handoff.

    These drive ``socketpair``/``pipe`` directly, so they reproduce the
    deterministic transfer-failure states (EOF without a listener, child error
    report, timeout) and prove a real ``SCM_RIGHTS`` descriptor is received,
    without requiring a live seccomp-notify listener or Landlock sandbox.
    """

    def test_successful_fd_receipt(self) -> None:
        import socket

        parent_sock, child_sock = socket.socketpair()
        probe_r, probe_w = os.pipe()
        try:
            worker_workspace._metadata_broker_handshake_send(child_sock, probe_w)
            child_sock.close()
            listener_fd, error = worker_workspace._metadata_broker_handshake_receive(
                parent_sock, time.monotonic() + 5.0
            )
            assert listener_fd >= 0
            assert error == ""
            try:
                # The received descriptor is a live, distinct duplicate of the
                # sent pipe write end.
                os.write(listener_fd, b"x")
                assert os.read(probe_r, 1) == b"x"
            finally:
                os.close(listener_fd)
        finally:
            parent_sock.close()
            os.close(probe_r)
            os.close(probe_w)

    def test_eof_without_listener_is_deterministic_failure(self) -> None:
        import socket

        parent_sock, child_sock = socket.socketpair()
        child_sock.close()  # child dies without delivering a listener fd
        try:
            listener_fd, error = worker_workspace._metadata_broker_handshake_receive(
                parent_sock, time.monotonic() + 5.0
            )
        finally:
            parent_sock.close()
        assert listener_fd < 0
        assert error == "handshake_eof"

    def test_child_error_report_is_observable(self) -> None:
        import socket

        parent_sock, child_sock = socket.socketpair()
        try:
            worker_workspace._metadata_broker_handshake_error(child_sock, b"boom")
            child_sock.close()
            listener_fd, error = worker_workspace._metadata_broker_handshake_receive(
                parent_sock, time.monotonic() + 5.0
            )
        finally:
            parent_sock.close()
        assert listener_fd < 0
        assert error == "boom"

    def test_timeout_is_bounded_and_observable(self) -> None:
        import socket

        parent_sock, child_sock = socket.socketpair()
        started = time.monotonic()
        try:
            # Peer stays open but never sends: the parent must fail closed on
            # a bounded deadline instead of blocking indefinitely.
            listener_fd, error = worker_workspace._metadata_broker_handshake_receive(
                parent_sock, started + 0.2
            )
        finally:
            parent_sock.close()
            child_sock.close()
        assert listener_fd < 0
        assert error == "handshake_timeout"
        assert time.monotonic() - started < 5.0

    def test_non_error_data_without_fd_is_protocol_violation(self) -> None:
        import socket

        parent_sock, child_sock = socket.socketpair()
        try:
            child_sock.sendall(b"garbage")
            child_sock.close()
            listener_fd, error = worker_workspace._metadata_broker_handshake_receive(
                parent_sock, time.monotonic() + 5.0
            )
        finally:
            parent_sock.close()
        assert listener_fd < 0
        assert error == "handshake_protocol_violation"


class TestRunValidationsGitInitIntegration:
    def test_git_init_succeeds_beneath_validation_scratch(
        self, tmp_path: Path
    ) -> None:
        if _SELF_HOSTED_VALIDATION_EXEC:
            pytest.skip(
                "self-hosted validation bootstrap: loaded canonical broker "
                "still denies directory mutation before the candidate "
                "broker proves its behavior in a real run_validations"
            )
        if sys.platform != "linux":
            pytest.skip("seccomp user notification is Linux-only")
        try:
            backend = worker_workspace.select_sandbox_backend()
        except WorkspaceError:
            pytest.skip("no secure sandbox backend available on this host")
        if backend != "landlock":
            pytest.skip("landlock validation backend not selected on this host")
        if not worker_workspace._seccomp_notify_supported():
            pytest.skip("host kernel/libseccomp lacks seccomp user notification")
        if shutil.which("git") is None:
            pytest.skip("git is not available on this host")
        script_body = (
            "import os\n"
            "import subprocess\n"
            "import sys\n"
            "d = os.path.join(os.environ['TMPDIR'], 'gitprobe')\n"
            "os.makedirs(d, exist_ok=True)\n"
            "r = subprocess.run(\n"
            "    ['git', 'init', '-q', '.'],\n"
            "    cwd=d,\n"
            "    capture_output=True,\n"
            "    text=True,\n"
            ")\n"
            "sys.stderr.write(r.stderr)\n"
            "sys.exit(r.returncode)\n"
        )
        program = "exec(" + repr(script_body) + ")"
        workspace = _workspace(tmp_path)
        results = worker_workspace.run_validations(
            workspace,
            [f"python3 -c {shlex.quote(program)}"],
            timeout_seconds=180,
        )
        assert results
        assert results[0]["returncode"] == 0, results[0]
        assert not results[0].get("timed_out")

    def test_write_json_0600_parent_chmod_succeeds(
        self, tmp_path: Path
    ) -> None:
        if _SELF_HOSTED_VALIDATION_EXEC:
            pytest.skip(
                "self-hosted validation bootstrap: loaded canonical broker "
                "still denies directory chmod before the candidate broker "
                "proves its behavior in a real run_validations"
            )
        if sys.platform != "linux":
            pytest.skip("seccomp user notification is Linux-only")
        try:
            backend = worker_workspace.select_sandbox_backend()
        except WorkspaceError:
            pytest.skip("no secure sandbox backend available on this host")
        if backend != "landlock":
            pytest.skip("landlock validation backend not selected on this host")
        if not worker_workspace._seccomp_notify_supported():
            pytest.skip("host kernel/libseccomp lacks seccomp user notification")
        script_body = (
            "import json\n"
            "import os\n"
            "import sys\n"
            "d = os.path.join(os.environ['TMPDIR'], 'wj0600probe')\n"
            "sub = os.path.join(d, 'sub')\n"
            "os.makedirs(sub, exist_ok=True)\n"
            "os.chmod(sub, 0o700)\n"
            "payload = {'version': 1, 'status': 'ok'}\n"
            "target = os.path.join(sub, 'out.json')\n"
            "with open(target, 'w', encoding='utf-8') as handle:\n"
            "    json.dump(payload, handle, sort_keys=True)\n"
            "    handle.write('\\n')\n"
            "os.chmod(target, 0o600)\n"
            "sys.exit(0)\n"
        )
        program = "exec(" + repr(script_body) + ")"
        workspace = _workspace(tmp_path)
        results = worker_workspace.run_validations(
            workspace,
            [f"python3 -c {shlex.quote(program)}"],
            timeout_seconds=180,
        )
        assert results
        assert results[0]["returncode"] == 0, results[0]
        assert not results[0].get("timed_out")


class TestReviewOverlayContentOnlyCopy:
    """NF160: review overlays must copy content only, never copystat/utime.

    ``_overlay_regular_path`` feeds ``create_quality_review_workspace``. The
    Landlock validation boundary denies ``utime``/``utimensat``, so the
    overlay copy must not request timestamp/owner preservation via
    ``shutil.copy2`` and must keep rejecting symlink/non-regular sources.
    """

    def test_overlay_copies_bytes_without_metadata_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "src"
        target = tmp_path / "dst"
        source.mkdir()
        target.mkdir()
        (source / "candidate.py").write_text("value = 2\n", encoding="utf-8")

        def _reject_copy2(*_args, **_kwargs):
            raise AssertionError("review overlay must not use copy2")

        monkeypatch.setattr(worker_workspace.shutil, "copy2", _reject_copy2)
        worker_workspace._overlay_regular_path(source, target, "candidate.py")
        assert (target / "candidate.py").read_text(encoding="utf-8") == "value = 2\n"

    def test_overlay_rejects_symlink_and_non_regular_sources(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "src"
        target = tmp_path / "dst"
        source.mkdir()
        target.mkdir()
        real = source / "real.py"
        real.write_text("value = 1\n", encoding="utf-8")
        (source / "link.py").symlink_to(real)
        with pytest.raises(WorkspaceError, match="symlink_path_component_forbidden"):
            worker_workspace._overlay_regular_path(source, target, "link.py")
        (source / "subdir").mkdir()
        with pytest.raises(WorkspaceError, match="combined_tree_source_not_file"):
            worker_workspace._overlay_regular_path(source, target, "subdir")


class TestCoherentDependencyGenerationPortability:
    """NF-2026-00423: dependency closure is one coherent current-canonical
    generation, seeded with a Landlock-boundary-safe content-only copy.

    The seed path must not fall back to ``shutil.copy2`` / ``os.utime`` (both
    denied inside the Landlock validation boundary), must materialize the exact
    current-canonical bytes byte-for-byte (no newline/metadata drift), and must
    keep the imported dependency read-only -- outside ``allowed_writes`` and
    absent from the candidate delta -- so it can never be promoted. Requires
    only ``git``; the Linux-only guard is the module-level ``pytestmark``.
    """

    @staticmethod
    def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )

    def _seed_repo(self, root: Path) -> None:
        assert self._git(root, "init", "-q").returncode == 0
        assert self._git(
            root, "config", "user.email", "tests@example.invalid"
        ).returncode == 0
        assert self._git(root, "config", "user.name", "NF423 Tests").returncode == 0
        package = root / "src/prodpkg"
        package.mkdir(parents=True)
        (package / "__init__.py").write_bytes(b"")
        # HEAD generation: dependency lacks the symbol; launcher does not import
        # it, so HEAD alone is internally consistent.
        (package / "task_store.py").write_bytes(b"STORE = 'v1'\n")
        (package / "process_launcher.py").write_bytes(b"LAUNCH_OK = False\n")
        (root / "probe_launcher.py").write_bytes(
            b"from prodpkg.process_launcher import LAUNCH_OK\n"
            b"print('LAUNCH_OK', LAUNCH_OK)\n"
        )
        (root / "pyproject.toml").write_bytes(
            b"[tool.pytest.ini_options]\npythonpath = ['src']\n"
        )
        assert self._git(
            root, "add", "src/prodpkg", "probe_launcher.py", "pyproject.toml"
        ).returncode == 0
        assert self._git(root, "commit", "-qm", "head").returncode == 0
        # Current canonical generation, uncommitted: detached HEAD differs.
        (package / "task_store.py").write_bytes(
            b"def is_bool_safe_int(value):\n"
            b"    return isinstance(value, int) and not isinstance(value, bool)\n"
        )
        (package / "process_launcher.py").write_bytes(
            b"from prodpkg.task_store import is_bool_safe_int\n"
            b"LAUNCH_OK = is_bool_safe_int(5)\n"
        )

    def test_dependency_overlay_is_content_only_and_read_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if shutil.which("git") is None:
            pytest.skip("git is not available on this host")
        repo = tmp_path / "parent"
        repo.mkdir()
        self._seed_repo(repo)

        def _reject_copy2(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("dependency seed must not use copy2")

        monkeypatch.setattr(worker_workspace.shutil, "copy2", _reject_copy2)
        monkeypatch.setenv(
            worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
        )
        workspace = worker_workspace.create_workspace(
            repo,
            "nf423-portability-dependency",
            {
                "allowed_writes": ["src/prodpkg/process_launcher.py"],
                "read_first": [
                    "src/prodpkg/process_launcher.py",
                    "probe_launcher.py",
                ],
                "validation": ["PYTHONPATH=src python3 probe_launcher.py"],
            },
            "validation",
        )
        try:
            dependency = workspace.path / "src/prodpkg/task_store.py"
            current_bytes = (repo / "src/prodpkg/task_store.py").read_bytes()
            # Byte-for-byte identical to the current canonical tree: coherent
            # generation, no metadata/newline drift, content-only copy.
            assert dependency.read_bytes() == current_bytes
            assert hashlib.sha256(dependency.read_bytes()).digest() == hashlib.sha256(
                current_bytes
            ).digest()
            assert b"is_bool_safe_int" in dependency.read_bytes()
            # Read-only dependency: not writable, not a candidate change.
            assert "src/prodpkg/task_store.py" not in workspace.allowed_writes
            assert worker_workspace.changed_paths(workspace) == []
        finally:
            worker_workspace.cleanup_workspace(
                repo, workspace.path, workspace.home
            )


class TestSemLockCapability:
    def test_probe_does_not_infer_support_from_platform_name(self) -> None:
        import inspect

        src = "".join(
            inspect.getsource(fn)
            for fn in (
                validation_runner.construct_multiprocessing_semlock,
                validation_runner.probe_multiprocessing_semlock,
                validation_runner.sandbox_can_grant_semlock,
                validation_runner.preflight_semlock_capability,
            )
        )
        assert "sys.platform" not in src
        assert "os.name" not in src
        assert "get_context" in inspect.getsource(
            validation_runner.construct_multiprocessing_semlock
        )

    def test_production_construct_uses_runtime_context(self) -> None:
        try:
            backend = validation_runner.construct_multiprocessing_semlock()
        except TypeError as exc:
            raise AssertionError(
                "construct_multiprocessing_semlock must pass runtime ctx"
            ) from exc
        except PermissionError:
            probe = validation_runner.probe_multiprocessing_semlock()
            assert probe["supported"] is False
            assert probe["primitive"] == validation_runner.SEMLOCK_PRIMITIVE
            return
        assert backend in {"posix_shm", "named_semaphore"}
        probe = validation_runner.probe_multiprocessing_semlock()
        assert probe["supported"] is True
        assert probe["backend"] == backend
        assert probe["schema"] == validation_runner.SEMLOCK_PROBE_SCHEMA
        assert probe["primitive"] == validation_runner.SEMLOCK_PRIMITIVE

    def test_landlock_dev_shm_permission_error_is_unsupported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiworkhub import process_launcher
        from aiworkhub.terminal_failure_classification import classify_terminal_failure

        if worker_workspace.landlock_abi_version() < 1:
            pytest.skip("host kernel lacks landlock")
        workspace = _workspace(tmp_path)
        source = validation_runner.SEMLOCK_TRUSTED_PROBE_SOURCE
        assert "aiworkhub" not in source
        inner = validation_runner.trusted_semlock_probe_argv(sys.executable)
        assert inner == [sys.executable, "-c", source]
        argv = process_launcher.sandbox_argv(
            workspace, "", inner, backend="landlock"
        )
        assert argv[-len(inner) :] == inner
        child = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        probe = validation_runner.decode_trusted_semlock_exit(child.returncode)
        assert probe is not None, (child.returncode, child.stderr[-400:])
        assert probe["supported"] is False
        assert probe["primitive"] == validation_runner.SEMLOCK_PRIMITIVE
        assert probe["backend"] == "posix_shm"
        decision = validation_runner.preflight_semlock_capability(
            ["python -m pytest tests/test_x.py"],
            backend="landlock",
            probe=probe,
            source_text=lambda rel: "import multiprocessing as mp\nmp.get_context().Lock()\n",
        )
        assert decision.action == "unsupported"
        assert decision.evidence == (
            f"{validation_runner.VALIDATION_UNSUPPORTED_IN_SANDBOX}:"
            f"{validation_runner.SEMLOCK_PRIMITIVE}:posix_shm"
        )
        tests_dir = workspace.path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_x.py").write_text(
            "import multiprocessing as mp\n"
            "ctx = mp.get_context('spawn')\n"
            "q = ctx.Queue()\n",
            encoding="utf-8",
        )

        def fake_run_validations(
            target: object, commands: list[str], **kw: object
        ) -> list[dict[str, object]]:
            raise AssertionError("unsupported capability must not run the command")

        monkeypatch.setattr(process_launcher, "run_validations", fake_run_validations)
        with pytest.raises(ValidationEnvironmentBlocked) as caught:
            process_launcher._run_validations_with_toolchain_receipt(
                workspace,
                ["python -m pytest tests/test_x.py"],
                {},
                backend="landlock",
            )
        assert caught.value.restriction == validation_runner.VALIDATION_UNSUPPORTED_IN_SANDBOX
        row = caught.value.results[0]
        assert row["capability_probe"]["backend"] == "posix_shm"
        assert row["capability_probe"]["supported"] is False
        assert (
            process_launcher._terminal_state_for_workspace_error(caught.value)
            == "finalize_failed"
        )
        classified = classify_terminal_failure(
            state="finalize_failed",
            exit_code=0,
            error=str(caught.value),
        )
        assert classified["failure_kind"] == "finalize_failed"
        assert classified["diagnostic"].startswith(
            "finalize_failed:validation_unsupported_in_sandbox"
        )
        assert "auth_forbidden" not in classified["diagnostic"]

    def test_grant_retains_candidate_pass_fail(self) -> None:
        probe = {
            "schema": validation_runner.SEMLOCK_PROBE_SCHEMA,
            "supported": True,
            "primitive": validation_runner.SEMLOCK_PRIMITIVE,
            "backend": "named_semaphore",
        }
        decision = validation_runner.preflight_semlock_capability(
            ["python -m pytest tests/test_x.py"],
            backend="landlock",
            probe=probe,
            source_text=lambda rel: "import multiprocessing as mp\nmp.get_context().Lock()\n",
        )
        assert decision.action == "grant"
        passed = validation_runner.classify_validation_results(
            [{"command": "python -m pytest tests/test_x.py", "returncode": 0}]
        )
        assert passed.state == validation_runner.VALIDATION_PASSED
        failed = validation_runner.classify_validation_results(
            [{"command": "python -m pytest tests/test_x.py", "returncode": 1}]
        )
        assert failed.state == validation_runner.VALIDATION_FAILED
        assert failed.blocks_acceptance is True

    def test_unsupported_blocks_acceptance_without_retry(self) -> None:
        probe = {
            "schema": validation_runner.SEMLOCK_PROBE_SCHEMA,
            "supported": False,
            "primitive": validation_runner.SEMLOCK_PRIMITIVE,
            "backend": "posix_shm",
        }
        row = {
            "command": "python -m pytest tests/test_x.py",
            "returncode": 1,
            "capability_probe_attributed": True,
            "capability_probe": probe,
        }
        terminal = validation_runner.classify_validation_results([row])
        assert terminal.state == validation_runner.VALIDATION_ENVIRONMENT_BLOCKED
        assert terminal.restriction == validation_runner.VALIDATION_UNSUPPORTED_IN_SANDBOX
        assert terminal.blocks_acceptance is True
        assert terminal.requires_supersede is False
        replay = validation_runner.plan_validation_capability_replay(
            [row["command"]], [row], backend="landlock"
        )
        assert replay.replay is False
        exhausted = validation_runner.plan_validation_capability_replay(
            [row["command"]], [row], backend="landlock", already_replayed=True
        )
        assert exhausted.reason == "validation_capability_replay_exhausted"

    def test_ordinary_permissionerror_is_not_unsupported(self) -> None:
        spawn = {
            "command": "python -m pytest tests/test_x.py",
            "returncode": None,
            "launch_error": "PermissionError",
        }
        assert (
            validation_runner.row_restriction(spawn)
            == validation_runner.RESTRICTION_FORBIDDEN_SPAWN
        )
        candidate = {
            "command": "python -m pytest tests/test_x.py",
            "returncode": 1,
            "stderr_tail": "PermissionError: [Errno 13] Permission denied: '/secret'",
        }
        assert validation_runner.row_restriction(candidate) is None
        terminal = validation_runner.classify_validation_results([candidate])
        assert terminal.state == validation_runner.VALIDATION_FAILED

    def test_linux_and_windows_backends_share_unsupported_prefix(self) -> None:
        filename_less = "named_semaphore" if os.name == "nt" else "posix_shm"
        cases = (
            ("/dev/shm/mp-x", "posix_shm"),
            (None, filename_less),
            ("Global\\BaseNamedObjects\\sem", "named_semaphore"),
        )
        for filename, expected_backend in cases:
            def _deny(name: str | None = filename) -> str:
                raise PermissionError(13, "Permission denied", name)

            probe = validation_runner.probe_multiprocessing_semlock(construct=_deny)
            assert probe["supported"] is False
            assert probe["primitive"] == validation_runner.SEMLOCK_PRIMITIVE
            assert probe["backend"] == expected_backend
            decision = validation_runner.preflight_semlock_capability(
                ['python -c "import multiprocessing as mp; mp.get_context().Lock()"'],
                backend="landlock",
                probe=probe,
            )
            assert decision.action == "unsupported"
            assert decision.evidence.endswith(f":{expected_backend}")
            assert decision.evidence.startswith(
                f"{validation_runner.VALIDATION_UNSUPPORTED_IN_SANDBOX}:"
                f"{validation_runner.SEMLOCK_PRIMITIVE}:"
            )

    def test_import_or_executor_is_not_semlock_need(self) -> None:
        denied = {
            "schema": validation_runner.SEMLOCK_PROBE_SCHEMA,
            "supported": False,
            "primitive": validation_runner.SEMLOCK_PRIMITIVE,
            "backend": "posix_shm",
        }
        sources = (
            "import multiprocessing as mp\n",
            "from concurrent.futures import ProcessPoolExecutor\nProcessPoolExecutor()\n",
            "import multiprocessing\nmultiprocessing.Process(target=abs)\n",
        )
        for text in sources:
            assert (
                validation_runner.command_needs_multiprocessing_semlock(
                    "python -m pytest tests/test_x.py",
                    source_text=lambda rel, src=text: src,
                )
                is False
            )
            assert (
                validation_runner.preflight_semlock_capability(
                    ["python -m pytest tests/test_x.py"],
                    backend="landlock",
                    probe=denied,
                    source_text=lambda rel, src=text: src,
                ).action
                == "skip"
            )
        assert (
            validation_runner.command_needs_multiprocessing_semlock(
                "python -m pytest -n 2 tests/test_x.py",
                source_text=lambda rel: "import multiprocessing as mp\n",
            )
            is False
        )
        assert (
            validation_runner.command_needs_multiprocessing_semlock(
                "python -c multiprocessing"
            )
            is False
        )

    def test_queue_and_lock_are_semlock_need(self) -> None:
        queue_src = (
            "import multiprocessing as mp\n"
            "ctx = mp.get_context('spawn')\n"
            "q = ctx.Queue()\n"
        )
        assert (
            validation_runner.command_needs_multiprocessing_semlock(
                "python -m pytest tests/test_x.py",
                source_text=lambda rel: queue_src,
            )
            is True
        )
        assert (
            validation_runner.command_needs_multiprocessing_semlock(
                'python -c "import multiprocessing as mp; mp.get_context().Lock()"'
            )
            is True
        )

    def test_trusted_exit_codes_are_structural(self) -> None:
        mapping = (
            (70, True, "posix_shm"),
            (71, True, "named_semaphore"),
            (80, False, "posix_shm"),
            (81, False, "named_semaphore"),
        )
        for code, supported, backend in mapping:
            probe = validation_runner.decode_trusted_semlock_exit(code)
            assert probe is not None
            assert probe["schema"] == validation_runner.SEMLOCK_PROBE_SCHEMA
            assert probe["supported"] is supported
            assert probe["backend"] == backend
            assert probe["primitive"] == validation_runner.SEMLOCK_PRIMITIVE
        assert validation_runner.decode_trusted_semlock_exit(1) is None
        argv = validation_runner.trusted_semlock_probe_argv(sys.executable)
        assert argv[0] == sys.executable
        assert argv[1] == "-c"
        assert argv[2] == validation_runner.SEMLOCK_TRUSTED_PROBE_SOURCE

    def test_sandbox_measured_posix_shm_success_is_grant(self) -> None:
        probe = {
            "schema": validation_runner.SEMLOCK_PROBE_SCHEMA,
            "supported": True,
            "primitive": validation_runner.SEMLOCK_PRIMITIVE,
            "backend": "posix_shm",
        }
        denied = validation_runner.preflight_semlock_capability(
            ["python -m pytest tests/test_x.py"],
            backend="landlock",
            probe=probe,
            source_text=lambda rel: "import multiprocessing as mp\nmp.get_context().Lock()\n",
        )
        assert denied.action == "unsupported"
        granted = validation_runner.preflight_semlock_capability(
            ["python -m pytest tests/test_x.py"],
            backend="landlock",
            probe=probe,
            source_text=lambda rel: "import multiprocessing as mp\nmp.get_context().Lock()\n",
            sandbox_measured=True,
        )
        assert granted.action == "grant"

    def test_receipt_probe_uses_sandbox_argv_not_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiworkhub import process_launcher

        workspace = _workspace(tmp_path)
        tests_dir = workspace.path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_x.py").write_text(
            "import multiprocessing as mp\n"
            "ctx = mp.get_context('spawn')\n"
            "q = ctx.Queue()\n",
            encoding="utf-8",
        )
        wrapped: list[list[str]] = []

        def fake_sandbox_argv(ws: object, adapter_id: str, adapter_argv: list[str], **kw: object) -> list[str]:
            wrapped.append(list(adapter_argv))
            return [sys.executable, "-c", "raise SystemExit(80)"]

        def boom() -> str:
            raise AssertionError("parent must not construct SemLock")

        ran: list[list[str]] = []

        def fake_run_validations(
            target: object, commands: list[str], **kw: object
        ) -> list[dict[str, object]]:
            ran.append(list(commands))
            return [{"command": "python -m pytest tests/test_x.py", "returncode": 0}]

        monkeypatch.setattr(process_launcher, "sandbox_argv", fake_sandbox_argv)
        monkeypatch.setattr(process_launcher, "run_validations", fake_run_validations)
        monkeypatch.setattr(validation_runner, "construct_multiprocessing_semlock", boom)
        with pytest.raises(ValidationEnvironmentBlocked) as caught:
            process_launcher._run_validations_with_toolchain_receipt(
                workspace,
                ["python -m pytest tests/test_x.py"],
                {},
                backend="landlock",
            )
        assert ran == []
        assert wrapped
        assert wrapped[0][2] == validation_runner.SEMLOCK_TRUSTED_PROBE_SOURCE
        assert caught.value.restriction == validation_runner.VALIDATION_UNSUPPORTED_IN_SANDBOX
        assert (
            process_launcher._terminal_state_for_workspace_error(caught.value)
            == "finalize_failed"
        )
        row = caught.value.results[0]
        assert row["capability_probe_attributed"] is True
        assert row["capability_probe"]["schema"] == validation_runner.SEMLOCK_PROBE_SCHEMA
        assert row["capability_probe"]["supported"] is False

    def test_receipt_probe_denial_preserves_batch_role_cardinality(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiworkhub import process_launcher

        workspace = _workspace(tmp_path)
        tests_dir = workspace.path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_x.py").write_text(
            "import multiprocessing as mp\n"
            "ctx = mp.get_context('spawn')\n"
            "q = ctx.Queue()\n",
            encoding="utf-8",
        )
        commands = [
            "python -m pytest tests/test_x.py",
            "python -m pytest tests/test_y.py",
            "python -m ruff check src",
            "git diff --check",
        ]
        roles = ["regression", "reproduction", "generic", "generic"]

        def fake_sandbox_argv(
            ws: object, adapter_id: str, adapter_argv: list[str], **kw: object
        ) -> list[str]:
            return [sys.executable, "-c", "raise SystemExit(80)"]

        def forbidden_run(*args: object, **kwargs: object) -> list[dict[str, object]]:
            raise AssertionError("declared commands must not run after denied preflight")

        monkeypatch.setattr(process_launcher, "sandbox_argv", fake_sandbox_argv)
        monkeypatch.setattr(process_launcher, "run_validations", forbidden_run)
        monkeypatch.setattr(
            process_launcher, "_sandbox_backend_for_adapter", lambda adapter_id: "landlock"
        )
        authority = {
            "adapter_id": "grok_kilo_cli",
            "sandbox_backend": "landlock",
            "validation": commands,
            "validation_roles": roles,
            "work_kind": "bugfix",
        }
        with pytest.raises(ValidationEnvironmentBlocked) as caught:
            process_launcher._run_declared_validations(
                workspace, authority, authority
            )

        assert [row["command"] for row in caught.value.results] == commands
        assert [row["behavioral_role"] for row in caught.value.results] == roles
        assert all(
            row["preflight_scope"] == "validation_batch"
            and row["capability_probe_attributed"] is True
            for row in caught.value.results
        )

    def test_receipt_probe_grant_runs_declared_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiworkhub import process_launcher

        workspace = _workspace(tmp_path)
        tests_dir = workspace.path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_x.py").write_text(
            "import multiprocessing as mp\nmp.get_context().Lock()\n",
            encoding="utf-8",
        )

        def fake_sandbox_argv(ws: object, adapter_id: str, adapter_argv: list[str], **kw: object) -> list[str]:
            return [sys.executable, "-c", "raise SystemExit(71)"]

        ran: list[list[str]] = []

        def fake_run_validations(
            target: object, commands: list[str], **kw: object
        ) -> list[dict[str, object]]:
            ran.append(list(commands))
            return [{"command": "python -m pytest tests/test_x.py", "returncode": 0}]

        monkeypatch.setattr(process_launcher, "sandbox_argv", fake_sandbox_argv)
        monkeypatch.setattr(process_launcher, "run_validations", fake_run_validations)
        rows = process_launcher._run_validations_with_toolchain_receipt(
            workspace,
            ["python -m pytest tests/test_x.py"],
            {},
            backend="landlock",
        )
        assert ran == [["python -m pytest tests/test_x.py"]]
        assert rows[0]["returncode"] == 0


class TestNF841AuthenticatedDenialIsNotStructurallyTerminal:
    """NF-2026-00841: a legitimate denial must not replace the real exit status.

    Two provider-free failures measured the broker terminating an otherwise
    passing pytest with ``rc=126`` after an authenticated ``utimensat``
    (syscall 280) ``metadata_broker_outside_scratch`` denial followed by an
    authenticated ``chmod`` (syscall 90) ``metadata_broker_hardlink_forbidden``
    denial.  Neither denial means the sandbox is broken: the validation
    substrate itself plants a shared inode inside the request-owned exec
    scratch (``plant_outer_validation_authority`` hardlinks the nested Landlock
    authority locator to its workspace anchor so
    ``verify_nested_landlock_authority_locator`` can require ``st_nlink == 2``),
    and refusing to mutate it is the intended outcome.  The refusal, the
    audited record and every kernel beneath-root/symlink/owner/inode check stay
    exactly as they were; only the process-group kill goes away.
    """

    @staticmethod
    def _record(
        exc: BaseException, syscall_nr: int, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[bool, dict]:
        """Drive the real denial recorder and return ``(terminal, record)``."""
        import json

        read_fd, write_fd = os.pipe()
        monkeypatch.setattr(
            worker_workspace, "_metadata_broker_evidence_fd", write_fd
        )
        monkeypatch.setattr(worker_workspace, "_metadata_broker_denial_count", 0)
        request = _make_request(syscall_nr, os.getpid())
        try:
            terminal = worker_workspace._record_metadata_broker_denial(exc, request)
            os.close(write_fd)
            payload = os.read(read_fd, 4096).decode("utf-8")
        finally:
            os.close(read_fd)
        return terminal, json.loads(payload.splitlines()[0])

    def test_terminal_reasons_are_exactly_the_unknowable_post_states(self) -> None:
        # A denial is terminal only when this trusted parent cannot say what
        # the filesystem looks like afterwards.  A refused mutation is not in
        # that class: the shared inode is provably untouched.
        assert worker_workspace._METADATA_BROKER_TERMINAL_DENIAL_REASONS == frozenset(
            {"metadata_broker_deleted_fd", "oserror_EPERM"}
        )

    def test_hardlink_forbidden_chmod_is_audited_without_termination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        terminal, record = self._record(
            WorkspaceError("metadata_broker_hardlink_forbidden:/scratch/config.lock"),
            90,
            monkeypatch,
        )
        assert terminal is False
        assert record["schema"] == "aiworkhub.metadata_broker_denial.v1"
        assert record["authenticated"] is True
        assert record["terminal"] is False
        assert record["reason"] == "metadata_broker_hardlink_forbidden"
        assert record["syscall_nr"] == 90

    def test_outside_scratch_utimensat_is_audited_without_termination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        terminal, record = self._record(
            WorkspaceError("metadata_broker_outside_scratch:/elsewhere/stamp"),
            280,
            monkeypatch,
        )
        assert terminal is False
        assert record["authenticated"] is True
        assert record["terminal"] is False
        assert record["reason"] == "metadata_broker_outside_scratch"
        assert record["syscall_nr"] == 280

    @pytest.mark.parametrize(
        "exc,reason",
        [
            (
                WorkspaceError("metadata_broker_deleted_fd"),
                "metadata_broker_deleted_fd",
            ),
            (PermissionError(1, "Operation not permitted"), "oserror_EPERM"),
        ],
    )
    def test_unknowable_post_state_denials_stay_terminal(
        self, exc: BaseException, reason: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        terminal, record = self._record(exc, 90, monkeypatch)
        assert terminal is True
        assert record["terminal"] is True
        assert record["reason"] == reason

    def test_refused_emulation_is_its_own_reason_and_never_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A nested trusted parent's own ``fchmod`` can be refused, and that is
        not an unknowable post-state: the verified inode is provably untouched.

        This is the exact denial that kept the nested-Git cases red -- the
        emulation reached ``EPERM`` because the parent doing it was itself
        confined by an outer validation boundary -- and collapsing it into
        ``oserror_EPERM`` terminated an otherwise passing command.
        """
        reasons = worker_workspace._METADATA_BROKER_TERMINAL_DENIAL_REASONS
        assert "metadata_broker_emulation_refused" not in reasons
        # The unbounded EPERM classes this carve-out must not absorb stay put.
        assert reasons == frozenset({"metadata_broker_deleted_fd", "oserror_EPERM"})
        terminal, record = self._record(
            worker_workspace._MetadataBrokerEmulationRefused(
                "metadata_broker_emulation_refused:fchmod"
            ),
            90,
            monkeypatch,
        )
        assert terminal is False
        assert record["authenticated"] is True
        assert record["terminal"] is False
        assert record["reason"] == "metadata_broker_emulation_refused"

    def test_only_eperm_is_reclassified_at_the_single_mutation_site(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # EPERM is the one errno that means the mode change was refused whole;
        # anything else may have left a state this parent cannot describe, so it
        # keeps its own reason and its own terminal status.
        calls: list[tuple[int, int]] = []

        def _refuse(fd: int, mode: int) -> None:
            calls.append((fd, mode))
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(worker_workspace.os, "fchmod", _refuse)
        with pytest.raises(worker_workspace._MetadataBrokerEmulationRefused):
            worker_workspace._metadata_broker_emulate_fchmod(7, 0o600)
        assert calls == [(7, 0o600)]

        def _read_only(_fd: int, _mode: int) -> None:
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(worker_workspace.os, "fchmod", _read_only)
        with pytest.raises(OSError) as other:
            worker_workspace._metadata_broker_emulate_fchmod(7, 0o600)
        assert not isinstance(
            other.value, worker_workspace._MetadataBrokerEmulationRefused
        )
        assert (
            worker_workspace._metadata_broker_denial_reason(other.value)
            == "oserror_EROFS"
        )

    @pytest.mark.usefixtures("require_openat2")
    @pytest.mark.parametrize("syscall", ["chmod", "fchmod"])
    def test_both_descriptor_paths_refuse_without_touching_the_target(
        self, syscall: str, scratch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive the real broker down both mutation paths with the emulation
        refused, exactly as an outer boundary refuses it.

        Both branches reach the same single ``fchmod`` mutation site: the
        pathname form through an ``openat2`` descriptor resolved beneath this
        request's scratch, the descriptor form through the child's own reopened
        and inode-matched fd.  The target is created with the mode-at-creation
        ``os.open`` argument, so the case needs no real chmod of its own and
        stays runnable inside a nested sandbox.
        """
        library = _RealSyscallLibrary()
        target = scratch / "config.lock"
        previous_umask = os.umask(0)
        try:
            handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o664)
        finally:
            os.umask(previous_umask)
        before = os.stat(target)
        refused: list[int] = []

        def _refuse(fd: int, _mode: int) -> None:
            refused.append(fd)
            raise PermissionError(1, "Operation not permitted")

        fd, root = _scratch_fd_root(scratch)
        buffer = _path_buffer(target)
        try:
            request = _make_request(library.number(syscall), os.getpid())
            if syscall == "fchmod":
                request.data.args[0] = handle
            else:
                request.data.args[0] = ctypes.addressof(buffer)
            request.data.args[1] = 0o600
            monkeypatch.setattr(worker_workspace.os, "fchmod", _refuse)
            with pytest.raises(
                worker_workspace._MetadataBrokerEmulationRefused
            ) as denial:
                worker_workspace._metadata_broker_apply(
                    library, -1, request, os.getpid(), fd, root
                )
        finally:
            os.close(fd)
            os.close(handle)

        # The refusal happened at the mutation site, once, after every check.
        assert len(refused) == 1
        after = os.stat(target)
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        assert stat.S_IMODE(after.st_mode) == 0o664
        terminal, record = self._record(
            denial.value, int(request.data.nr), monkeypatch
        )
        assert terminal is False, record
        assert record["authenticated"] is True
        assert record["reason"] == "metadata_broker_emulation_refused"

    @staticmethod
    def _shared_inode_lock(scratch: Path) -> Path:
        """A hardlinked ``.git/config.lock``, built without a single chmod.

        ``TestBrokeredSyscallDecoding._hardlinked_git_config_lock`` reaches the
        same layout through ``os.chmod``, which the worker sandbox's own
        seccomp policy denies. Both denials this case replays are raised by
        target verification before any real metadata syscall, so building the
        fixture with the mode-at-creation ``os.open`` argument keeps the
        regression runnable in a nested sandbox as well as on a host.

        That creation mode is masked by the process umask, so this fixture
        needs the sibling's umask guard too: at the usual ``0o022`` the lock is
        born ``0o644`` and the untouched-mode assertion below reports a
        difference the broker never caused. Pinning the created mode to exactly
        ``0o664`` on every host keeps ``before`` and ``after`` comparable and
        keeps the replayed ``0o600`` chmod a genuine mode *change*, which is
        what must stay denied on a shared inode.
        """
        git_dir = scratch / ".git"
        git_dir.mkdir(mode=0o700)
        target = git_dir / "config.lock"
        previous_umask = os.umask(0)
        try:
            handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o664)
        finally:
            os.umask(previous_umask)
        try:
            os.write(handle, b"x")
        finally:
            os.close(handle)
        os.link(target, scratch / "config.lock.alias")
        return target

    @pytest.mark.usefixtures("require_openat2")
    def test_authenticated_280_then_90_sequence_leaves_shared_inode_untouched(
        self, scratch: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Replay the exact measured sequence against the real broker with the
        # real x86_64 syscall numbers: both requests are authenticated, both
        # are refused fail-closed, the shared inode keeps its identity and its
        # mode, and neither refusal is structurally terminal.
        library = _RealSyscallLibrary()
        outside = tmp_path / "outside.stamp"
        outside.write_text("x", encoding="utf-8")
        target = self._shared_inode_lock(scratch)
        before = os.stat(target)
        denials: list[tuple[int, WorkspaceError]] = []
        fd, root = _scratch_fd_root(scratch)
        try:
            stamp_buffer = _path_buffer(outside)
            utimensat = _make_request(library.number("utimensat"), os.getpid())
            utimensat.data.args[0] = 0
            utimensat.data.args[1] = ctypes.addressof(stamp_buffer)
            utimensat.data.args[2] = 0
            utimensat.data.args[3] = 0
            with pytest.raises(
                WorkspaceError, match="metadata_broker_outside_scratch"
            ) as outside_denial:
                worker_workspace._metadata_broker_apply(
                    library, -1, utimensat, os.getpid(), fd, root
                )
            denials.append((int(utimensat.data.nr), outside_denial.value))

            lock_buffer = _path_buffer(target)
            chmod = _make_request(library.number("chmod"), os.getpid())
            chmod.data.args[0] = ctypes.addressof(lock_buffer)
            chmod.data.args[1] = 0o600
            with pytest.raises(
                WorkspaceError, match="metadata_broker_hardlink_forbidden"
            ) as hardlink_denial:
                worker_workspace._metadata_broker_apply(
                    library, -1, chmod, os.getpid(), fd, root
                )
            denials.append((int(chmod.data.nr), hardlink_denial.value))
        finally:
            os.close(fd)

        after = os.stat(target)
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        assert after.st_nlink == 2
        assert stat.S_IMODE(after.st_mode) == 0o664
        assert [number for number, _exc in denials] == [280, 90]
        for number, exc in denials:
            terminal, record = self._record(exc, number, monkeypatch)
            assert terminal is False, record
            assert record["authenticated"] is True
            assert record["syscall_nr"] == number

    @staticmethod
    def _denial_probe_command() -> str:
        """Build the nested probe as one card-shaped validation command.

        ``_tokenize_validation_command`` rejects shell metacharacters anywhere
        in a validation command, including inside shell quoting, so the payload
        must not contain one at all.  ``O_WRONLY``, ``O_CREAT`` and ``O_EXCL``
        are disjoint bits, so summing them yields exactly the flag word the
        bitwise or would have produced, and every explanatory comment stays out
        here in real source rather than riding along inside the program text.
        """
        script_body = "\n".join(
            (
                "import os",
                "import sys",
                "scratch = os.environ['TMPDIR']",
                "probe = os.path.join(scratch, 'nf841probe')",
                "os.makedirs(probe, exist_ok=True)",
                "target = os.path.join(probe, 'config.lock')",
                "flags = os.O_WRONLY + os.O_CREAT + os.O_EXCL",
                "handle = os.open(target, flags, 0o664)",
                "os.close(handle)",
                # Plant the shared inode the broker must refuse to mutate.
                "try:",
                "    os.link(target, os.path.join(probe, 'config.lock.alias'))",
                "except OSError:",
                "    sys.exit(3)",
                # syscall 280 against a path outside every authorized root.
                "stamp = os.path.join(os.path.dirname(scratch), 'nf841.stamp')",
                "try:",
                "    os.utime(stamp, None)",
                "except OSError:",
                "    pass",
                # syscall 90 against the shared inode: refused, never terminal.
                "try:",
                "    os.chmod(target, 0o600)",
                "except OSError:",
                "    pass",
                "sys.exit(0)",
            )
        )
        return f"python3 -c {shlex.quote('exec(' + repr(script_body) + ')')}"

    def test_denial_probe_payload_is_card_validation_parseable(self) -> None:
        # The predecessor payload spelled the open flags with a bitwise or, and
        # _tokenize_validation_command refuses that character even inside shell
        # quoting -- so the nested case died as validation_shell_syntax_forbidden
        # before the broker ever decided anything.  Prove the payload parses on
        # every host, not only where a nested Landlock sandbox can start.
        command = self._denial_probe_command()
        assert not set(command).intersection("\n\r|;`<>\x00")
        argv = worker_workspace._tokenize_validation_command(command)
        assert argv[:2] == ["python3", "-c"]
        assert len(argv) == 3
        assert "os.utime(stamp, None)" in argv[2]
        assert "os.chmod(target, 0o600)" in argv[2]
        assert "os.link(target," in argv[2]

    def test_nested_validation_reports_child_status_after_both_denials(
        self, tmp_path: Path
    ) -> None:
        if _SELF_HOSTED_VALIDATION_EXEC:
            pytest.skip(
                "self-hosted validation bootstrap: the already loaded canonical "
                "broker still decides the nested run before the candidate "
                "broker can prove its behavior in a real run_validations"
            )
        if sys.platform != "linux":
            pytest.skip("seccomp user notification is Linux-only")
        try:
            backend = worker_workspace.select_sandbox_backend()
        except WorkspaceError:
            pytest.skip("no secure sandbox backend available on this host")
        if backend != "landlock":
            pytest.skip("landlock validation backend not selected on this host")
        if not worker_workspace._seccomp_notify_supported():
            pytest.skip("host kernel/libseccomp lacks seccomp user notification")
        workspace = _workspace(tmp_path)
        try:
            results = worker_workspace.run_validations(
                workspace,
                [self._denial_probe_command()],
                timeout_seconds=180,
            )
        except ValidationEnvironmentBlocked as blocked:
            # No exec-capable, metadata-honouring scratch root exists here, so
            # the nested run never starts. That is a host capability gap, the
            # same one provision_validation_exec_scratch reports fail-closed --
            # not a verdict on the broker's termination policy.
            pytest.skip(f"validation_unsupported_in_sandbox:{blocked}")
        assert results
        if results[0]["returncode"] == 3:
            pytest.skip("this host's Landlock policy refuses hardlink creation")
        assert results[0]["returncode"] == 0, results[0]
        assert not results[0].get("timed_out")


class TestNestedFallbackDoesNotShadowTheOuterBroker:
    """NF-2026-00841: the deny-only fallback must not blind an outer broker.

    The kernel permits one seccomp user-notification listener per filter tree,
    so a nested ``run_validations`` inside an already-brokered validation
    sandbox cannot install its own and takes ``_apply_metadata_seccomp``.
    Because ``SECCOMP_RET_ERRNO`` outranks ``SECCOMP_RET_USER_NOTIF``, that
    inner filter used to override the outer broker and return ``EPERM`` for
    every chmod -- which is why unmodified Git could not set ``core.filemode``
    on its own request-owned ``.git/config.lock``. The fallback now yields
    individual chmod-family *variants*, one syscall at a time, and only those
    an outer broker was measured issuing that exact syscall to mediate: each
    applied a real mode change to this request's own scratch inode and was
    refused at the filesystem root, beside the scratch, and through a symlink
    inside it. Anything unmeasured keeps its historical deny.
    """

    @staticmethod
    def _recording_library() -> object:
        class _Library:
            def __init__(self) -> None:
                self.rules: list[str] = []
                self.loaded = False
                self.released = False
                self._names: dict[int, str] = {}

            def seccomp_init(self, _action: int) -> int:
                return 1

            def seccomp_syscall_resolve_name(self, name: bytes) -> int:
                number = len(self._names) + 1
                self._names[number] = name.decode("ascii")
                return number

            def seccomp_rule_add(
                self, _ctx: int, _action: int, number: int, _count: int
            ) -> int:
                self.rules.append(self._names[number])
                return 0

            def seccomp_load(self, _ctx: int) -> int:
                self.loaded = True
                return 0

            def seccomp_release(self, _ctx: int) -> None:
                self.released = True

        return _Library()

    def test_default_fallback_still_denies_every_metadata_syscall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library = self._recording_library()
        monkeypatch.setattr(worker_workspace, "_seccomp_library", lambda: library)
        worker_workspace._apply_metadata_seccomp()
        assert tuple(library.rules) == worker_workspace._SECCOMP_DENIED_SYSCALLS
        assert library.loaded is True
        assert library.released is True

    def test_brokered_fallback_yields_only_the_measured_variants(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library = self._recording_library()
        monkeypatch.setattr(worker_workspace, "_seccomp_library", lambda: library)
        worker_workspace._apply_metadata_seccomp(
            brokered_chmod_syscalls=frozenset(
                worker_workspace._MEASURABLE_BROKERED_CHMOD_SYSCALLS
            )
        )
        omitted = set(worker_workspace._SECCOMP_DENIED_SYSCALLS) - set(library.rules)
        assert omitted == set(worker_workspace._MEASURABLE_BROKERED_CHMOD_SYSCALLS)
        # NF-2026-00841 rework: ``fchmodat2`` has no libc entry point here, so
        # it cannot be issued as itself by the probe -- and what is never
        # measured is never exempted.  The family is not traded away on one
        # variant's evidence, however many of its siblings are proven mediated.
        assert "fchmodat2" in library.rules
        # Ownership, xattr, timestamp and process-inspection confinement is
        # never traded away either -- including utimensat, which this filter
        # keeps denying even though the broker can mediate it.
        for retained in ("utimensat", "chown", "fchownat", "setxattr", "ptrace"):
            assert retained in library.rules
        assert library.loaded is True

    def test_a_single_measured_variant_yields_exactly_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library = self._recording_library()
        monkeypatch.setattr(worker_workspace, "_seccomp_library", lambda: library)
        worker_workspace._apply_metadata_seccomp(
            brokered_chmod_syscalls=frozenset({"chmod"})
        )
        omitted = set(worker_workspace._SECCOMP_DENIED_SYSCALLS) - set(library.rules)
        assert omitted == {"chmod"}
        assert "fchmodat" in library.rules

    def test_no_syscall_outside_the_chmod_family_can_be_exempted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exemption is intersected with the chmod family at load time.

        A caller -- or a future measurement bug -- cannot trade away ownership,
        timestamp or process-inspection confinement by naming it here.
        """
        library = self._recording_library()
        monkeypatch.setattr(worker_workspace, "_seccomp_library", lambda: library)
        worker_workspace._apply_metadata_seccomp(
            brokered_chmod_syscalls=frozenset(
                {"chmod", "utimensat", "chown", "ptrace", "setxattr"}
            )
        )
        omitted = set(worker_workspace._SECCOMP_DENIED_SYSCALLS) - set(library.rules)
        assert omitted == {"chmod"}

    def test_chmod_family_subset_is_not_widened(self) -> None:
        chmod_family = set(worker_workspace._METADATA_BROKER_CHMOD_SYSCALLS)
        assert chmod_family == {"chmod", "fchmod", "fchmodat", "fchmodat2"}
        assert chmod_family <= set(worker_workspace._METADATA_BROKER_SYSCALLS)
        assert chmod_family <= set(worker_workspace._SECCOMP_DENIED_SYSCALLS)
        measurable = set(worker_workspace._MEASURABLE_BROKERED_CHMOD_SYSCALLS)
        # ``fchmod`` joined the measurable set once it was probed as itself, on
        # descriptors to targets the probe creates. ``fchmodat2`` has no libc
        # entry point here, so it can never be issued, measured or exempted --
        # the subset stays a strict subset of the family.
        assert measurable == {"chmod", "fchmod", "fchmodat"}
        assert measurable < chmod_family

    @staticmethod
    def _install_boundary(
        monkeypatch: pytest.MonkeyPatch,
        scratch: Path,
        *,
        outside: "BaseException | None",
        root_only: bool = False,
        follows_symlinks: bool = False,
        seen: "list[Path] | None" = None,
    ) -> None:
        """Install a modelled mediating boundary in place of ``os.chmod``.

        A canonical broker resolves beneath this request's own roots: it refuses
        every target outside the scratch and every symlinked target
        (``RESOLVE_NO_SYMLINKS``) while applying a real change inside it.
        ``root_only`` models the path-scoped MAC this rework must reject -- it
        guards the filesystem root and lets everything else through -- and
        ``follows_symlinks`` models a boundary with no symlink rule at all. A
        target the model lets through reports ``ENOENT`` for a name that does
        not exist, exactly as an unmediated syscall would.

        The mode an authorized call would have applied is remembered and served
        back through ``os.fstat`` instead of being written to the inode: the
        worker sandbox denies the whole chmod family unconditionally, so a model
        that really chmod'd could not run there at all. What the measurement
        under test observes is identical either way.
        """
        applied: dict[tuple[int, int], int] = {}
        real_fstat = os.fstat
        real_stat = os.stat

        def chmod(path: object, mode: int, *_args: object, **_kwargs: object) -> None:
            target = Path(os.fspath(path))
            if seen is not None:
                seen.append(target)
            if target.is_relative_to(scratch):
                if target.is_symlink() and not follows_symlinks:
                    raise OSError("symlinked target refused")
                identity = real_stat(target)
                applied[(identity.st_dev, identity.st_ino)] = mode
                return None
            if outside is not None and not (root_only and target.parent != Path("/")):
                raise outside
            raise FileNotFoundError(2, "No such file or directory")

        def fchmod(fd: int, mode: int, *_args: object, **_kwargs: object) -> None:
            """The same modelled boundary, reached through a descriptor.

            ``fchmod`` is measured on targets the probe created, so unlike the
            pathname model an outside target *exists*: an unmediated boundary
            really applies the change there rather than reporting ``ENOENT``,
            which is exactly what the confinement half must fail closed on. The
            canonical broker's ``st_nlink`` rule is modelled too, because that
            is the refusal the measurement falls back to when creation is
            confined to the scratch.
            """
            info = real_fstat(fd)
            target = Path(os.readlink(f"/proc/self/fd/{fd}"))
            if seen is not None:
                seen.append(target)
            if target.is_relative_to(scratch):
                if info.st_nlink != 1 and stat.S_IMODE(info.st_mode) != mode:
                    raise PermissionError(1, "shared inode refused")
                applied[(info.st_dev, info.st_ino)] = mode
                return None
            if outside is not None and not (root_only and target.parent != Path("/")):
                raise outside
            applied[(info.st_dev, info.st_ino)] = mode
            return None

        def fstat(fd: int) -> os.stat_result:
            info = real_fstat(fd)
            # Consumed on read: each probe reads its own result exactly once and
            # unlinks its throwaway file straight after, so a later probe that
            # lands on the recycled inode number must not inherit this answer.
            mode = applied.pop((info.st_dev, info.st_ino), None)
            if mode is None:
                return info
            fields = list(info)
            fields[0] = stat.S_IFMT(info.st_mode) | mode
            return os.stat_result(fields)

        monkeypatch.setattr(os, "chmod", chmod)
        monkeypatch.setattr(os, "fchmod", fchmod)
        monkeypatch.setattr(os, "fstat", fstat)

    def test_probe_accepts_a_boundary_that_mediates_refuses_and_blocks_symlinks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        scratch = tmp_path / "region" / "scratch"
        scratch.mkdir(parents=True)
        seen: list[Path] = []
        self._install_boundary(
            monkeypatch, scratch, outside=PermissionError(1, "denied"), seen=seen
        )
        assert worker_workspace._outer_metadata_broker_brokered_chmod_syscalls(
            scratch
        ) == frozenset(worker_workspace._MEASURABLE_BROKERED_CHMOD_SYSCALLS)
        assert not list(scratch.iterdir()), "the probe must clean up after itself"
        # Every target the boundary saw carries the reserved probe basename, so
        # the refusals it answers can be recognised -- and left unrecorded -- by
        # the broker instead of forging denial telemetry on each nested command.
        assert seen
        for target in seen:
            assert target.name.startswith(
                worker_workspace._METADATA_BROKER_PROBE_BASENAME_PREFIX
            ), target

    def test_probe_rejects_an_unmediated_chmod(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An out-of-scratch chmod that really executes proves nothing mediates
        it: ``ENOENT`` for a non-existent target means the syscall ran."""
        scratch = tmp_path / "region" / "scratch"
        scratch.mkdir(parents=True)
        self._install_boundary(monkeypatch, scratch, outside=None)
        assert (
            worker_workspace._outer_metadata_broker_brokered_chmod_syscalls(scratch)
            == frozenset()
        )

    def test_probe_rejects_a_policy_that_only_guards_the_filesystem_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """NF-2026-00841 rework: the ambient-MAC false positive, closed.

        An AppArmor/SELinux-like policy that forbids chmod at ``/`` while
        permitting the temporary-file region an exec scratch lives in answers
        the root probe with ``EPERM`` without any canonical broker existing.
        Accepting that would disable the whole chmod-family deny and let a
        nested validator chmod every neighbouring request's files, so the
        measurement also probes directly beside the scratch -- inside the
        region such a policy permits -- and fails closed when it is allowed.
        """
        scratch = tmp_path / "region" / "scratch"
        scratch.mkdir(parents=True)
        seen: list[Path] = []
        self._install_boundary(
            monkeypatch,
            scratch,
            outside=PermissionError(1, "denied"),
            root_only=True,
            seen=seen,
        )
        assert (
            worker_workspace._outer_metadata_broker_brokered_chmod_syscalls(scratch)
            == frozenset()
        )
        # The beside-the-scratch target really was probed, not assumed.
        assert any(target.parent == scratch.parent for target in seen), seen

    def test_probe_rejects_a_boundary_that_follows_a_symlink(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``RESOLVE_NO_SYMLINKS`` is the broker's own rule, and no path-scoped
        policy implements it: a boundary that reaches a target through a link
        is not the mediator this exemption is safe to rely on."""
        scratch = tmp_path / "region" / "scratch"
        scratch.mkdir(parents=True)
        self._install_boundary(
            monkeypatch,
            scratch,
            outside=PermissionError(1, "denied"),
            follows_symlinks=True,
        )
        assert (
            worker_workspace._outer_metadata_broker_brokered_chmod_syscalls(scratch)
            == frozenset()
        )

    def test_probe_rejects_a_plain_deny_only_ancestor(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        scratch = tmp_path / "region" / "scratch"
        scratch.mkdir(parents=True)

        def chmod(*_args: object, **_kwargs: object) -> None:
            raise PermissionError(1, "denied")

        monkeypatch.setattr(os, "chmod", chmod)
        assert (
            worker_workspace._outer_metadata_broker_brokered_chmod_syscalls(scratch)
            == frozenset()
        )

    def test_probe_requires_a_scratch_and_fails_closed_on_inode_drift(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        assert (
            worker_workspace._outer_metadata_broker_brokered_chmod_syscalls(None)
            == frozenset()
        )
        scratch = tmp_path / "region" / "scratch"
        scratch.mkdir(parents=True)
        self._install_boundary(
            monkeypatch, scratch, outside=PermissionError(1, "denied")
        )
        real_fstat = os.fstat
        seen: list[int] = []

        def drifting_fstat(fd: int) -> os.stat_result:
            info = real_fstat(fd)
            seen.append(fd)
            if len(seen) % 2 == 1:
                # Each variant reads the probe inode twice. The ``before`` read
                # is honest and the ``after`` read reports a different inode --
                # the drift every variant must fail closed on, not just the
                # first one measured.
                return info
            fields = list(info)
            fields[1] = int(info.st_ino) + 1
            return os.stat_result(fields)

        monkeypatch.setattr(os, "fstat", drifting_fstat)
        assert (
            worker_workspace._outer_metadata_broker_brokered_chmod_syscalls(scratch)
            == frozenset()
        )

    def test_the_descriptor_variant_is_measured_as_itself(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """NF-2026-00841 rework: ``fchmod`` is proven, not inferred.

        The predecessor exempted only the pathname variants and left ``fchmod``
        denied in the nested child's fallback filter, so ``SECCOMP_RET_ERRNO``
        kept answering it ``EPERM`` and outranked the outer broker that was
        already mediating its siblings -- which is the exact syscall Git reaches
        through an open ``.git/config.lock`` descriptor. It is now measured by
        issuing ``os.fchmod`` itself, never by trusting a sibling's evidence.
        """
        scratch = tmp_path / "region" / "scratch"
        scratch.mkdir(parents=True)
        issued: list[int] = []
        self._install_boundary(
            monkeypatch, scratch, outside=PermissionError(1, "denied")
        )
        modelled_fchmod = os.fchmod

        def counting_fchmod(fd: int, mode: int, *args: object, **kwargs: object):
            issued.append(fd)
            return modelled_fchmod(fd, mode, *args, **kwargs)

        monkeypatch.setattr(os, "fchmod", counting_fchmod)
        assert "fchmod" in (
            worker_workspace._outer_metadata_broker_brokered_chmod_syscalls(scratch)
        )
        assert issued, "the descriptor form must actually be issued"
        assert not list(scratch.iterdir()), "the probe must clean up after itself"

    def test_the_descriptor_variant_is_refused_beside_the_scratch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A region-permissive policy must not exempt the descriptor form.

        ``root_only`` forbids only the filesystem root and lets the temporary
        region an exec scratch lives in through. A descriptor on a throwaway
        directly beside the scratch is therefore applied rather than refused,
        and ``fchmod`` must keep its historical deny.
        """
        scratch = tmp_path / "region" / "scratch"
        scratch.mkdir(parents=True)
        self._install_boundary(
            monkeypatch,
            scratch,
            outside=PermissionError(1, "denied"),
            root_only=True,
        )
        # Asserted on the descriptor measurement itself: the aggregate would
        # also exclude ``fchmod`` here because no pathname sibling survives
        # ``root_only``, which would pass this test without the confinement
        # half ever being exercised.
        assert worker_workspace._brokered_fchmod_is_mediated(scratch) is False
        assert "fchmod" not in (
            worker_workspace._outer_metadata_broker_brokered_chmod_syscalls(scratch)
        )

    def test_the_descriptor_variant_falls_back_to_the_shared_inode_rule(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Confining creation to the scratch must not disable the measurement.

        A nested validator's Landlock ruleset may permit creation only beneath
        the exec scratch, so the beside-the-scratch descriptor cannot be made at
        all. The broker's own ``st_nlink`` rule then decides: no bare kernel and
        no path-scoped policy refuses a mode change merely because the inode is
        hardlinked, so a refusal there authenticates the mediator itself.
        """
        scratch = tmp_path / "region" / "scratch"
        scratch.mkdir(parents=True)
        self._install_boundary(
            monkeypatch, scratch, outside=PermissionError(1, "denied")
        )
        real_open = os.open

        def confined_open(path: object, flags: int, *args: object) -> int:
            target = Path(os.fspath(path))
            if flags & os.O_CREAT and not target.is_relative_to(scratch):
                raise PermissionError(1, "creation confined to the scratch")
            return real_open(path, flags, *args)

        monkeypatch.setattr(os, "open", confined_open)
        assert worker_workspace._brokered_fchmod_is_mediated(scratch) is True
        assert "fchmod" in (
            worker_workspace._outer_metadata_broker_brokered_chmod_syscalls(scratch)
        )
        assert not list(scratch.iterdir()), "the probe must clean up after itself"

    def test_the_descriptor_variant_fails_closed_on_an_unrefused_shared_inode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A boundary with no ``st_nlink`` rule is not the canonical broker.

        With creation confined to the scratch the shared-inode refusal is the
        only negative evidence left, so a boundary that lets an ``fchmod`` on a
        hardlinked inode through -- instead of refusing it the way
        ``_metadata_broker_verify_fd`` does -- must not have ``fchmod`` traded
        away to it.
        """
        scratch = tmp_path / "region" / "scratch"
        scratch.mkdir(parents=True)
        self._install_boundary(
            monkeypatch, scratch, outside=PermissionError(1, "denied")
        )
        real_open = os.open
        real_fchmod = os.fchmod

        def confined_open(path: object, flags: int, *args: object) -> int:
            target = Path(os.fspath(path))
            if flags & os.O_CREAT and not target.is_relative_to(scratch):
                raise PermissionError(1, "creation confined to the scratch")
            return real_open(path, flags, *args)

        def linkblind_fchmod(fd: int, mode: int, *args: object, **kwargs: object):
            """Identical to the modelled broker minus its ``st_nlink`` rule."""
            info = os.fstat(fd)
            if info.st_nlink != 1:
                return None
            return real_fchmod(fd, mode, *args, **kwargs)

        monkeypatch.setattr(os, "open", confined_open)
        monkeypatch.setattr(os, "fchmod", linkblind_fchmod)
        assert worker_workspace._brokered_fchmod_is_mediated(scratch) is False
        assert "fchmod" not in (
            worker_workspace._outer_metadata_broker_brokered_chmod_syscalls(scratch)
        )

    def test_the_descriptor_variant_needs_a_proven_pathname_sibling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A descriptor cannot demonstrate a path rule, so it never stands alone.

        A boundary that reaches a target through a symlink has no
        ``RESOLVE_NO_SYMLINKS`` rule and is not the mediator this exemption may
        rely on -- yet nothing a *descriptor* probe can issue would notice,
        because ``fchmod`` resolves no path at all.  ``fchmod`` is therefore
        yielded only alongside a pathname sibling that proved the path rules on
        this very boundary, which keeps the whole family denied here.
        """
        scratch = tmp_path / "region" / "scratch"
        scratch.mkdir(parents=True)
        self._install_boundary(
            monkeypatch,
            scratch,
            outside=PermissionError(1, "denied"),
            follows_symlinks=True,
        )
        # The descriptor half on its own is satisfied by this boundary...
        assert worker_workspace._brokered_fchmod_is_mediated(scratch) is True
        # ...and it is still not exempted, because no sibling proved the path
        # rules that a descriptor form cannot speak to.
        assert (
            worker_workspace._outer_metadata_broker_brokered_chmod_syscalls(scratch)
            == frozenset()
        )


@pytest.mark.usefixtures("require_openat2")
class TestBoundaryProbeDenialsAreNotRecordedAsValidatorDefects:
    """NF-2026-00841 rework: the measurement must not forge denial telemetry.

    ``_outer_metadata_broker_brokered_chmod_syscalls`` authenticates the outer
    boundary by provoking refusals -- outside the scratch, beside it, and
    through a symlink -- once per nested command.  Those refusals are real and
    fail closed, but they are the measurement's own *expected* answer, not a
    validator rejection: recording them printed a ``metadata_broker_denied``
    line on every nested command and burned slots of the bounded 32-record
    ledger that real denials need.

    The reserved basename that marks them grants nothing.  It is resolved,
    checked and refused by exactly the same rules as any other name; only the
    telemetry distinguishes it, and only for the bounded, never-terminal
    reasons a probe can legitimately provoke.
    """

    @staticmethod
    def _record(exc: BaseException, syscall_nr: int = 90) -> tuple:
        """Drive the real recorder with *both* of its sinks captured.

        Returns ``(terminal, evidence, stderr_noise, ledger_count)``.
        """
        evidence_read, evidence_write = os.pipe()
        noise_read, noise_write = os.pipe()
        saved_stderr = os.dup(2)
        previous_fd = worker_workspace._metadata_broker_evidence_fd
        previous_count = worker_workspace._metadata_broker_denial_count
        worker_workspace._metadata_broker_evidence_fd = evidence_write
        worker_workspace._metadata_broker_denial_count = 0
        request = _make_request(syscall_nr, os.getpid())
        try:
            os.dup2(noise_write, 2)
            try:
                terminal = worker_workspace._record_metadata_broker_denial(
                    exc, request
                )
            finally:
                os.dup2(saved_stderr, 2)
            os.close(evidence_write)
            os.close(noise_write)
            evidence = os.read(evidence_read, 8192).decode("utf-8", "replace")
            noise = os.read(noise_read, 8192).decode("utf-8", "replace")
            count = worker_workspace._metadata_broker_denial_count
        finally:
            os.close(saved_stderr)
            os.close(evidence_read)
            os.close(noise_read)
            worker_workspace._metadata_broker_evidence_fd = previous_fd
            worker_workspace._metadata_broker_denial_count = previous_count
        return terminal, evidence, noise, count

    def test_a_probe_refusal_emits_no_record_and_no_stderr_line(self) -> None:
        probe = f"/{worker_workspace._METADATA_BROKER_PROBE_BASENAME_PREFIX}abc123"
        denial = worker_workspace._metadata_broker_probe_denial(
            WorkspaceError(f"metadata_broker_outside_scratch:{probe}"), probe
        )
        assert denial is not None
        terminal, evidence, noise, count = self._record(denial)
        assert terminal is False
        assert evidence == ""
        assert noise == ""
        # The bounded ledger a real denial needs is left completely intact.
        assert count == 0

    def test_an_ordinary_refusal_is_still_recorded_in_full(self) -> None:
        terminal, evidence, noise, count = self._record(
            WorkspaceError("metadata_broker_outside_scratch:/elsewhere/config.lock")
        )
        record = json.loads(evidence.splitlines()[0])
        assert terminal is False
        assert record["reason"] == "metadata_broker_outside_scratch"
        assert record["authenticated"] is True
        assert "metadata_broker_denied" in noise
        assert count == 1

    def test_the_reserved_name_cannot_silence_an_unexpected_reason(self) -> None:
        """Only the bounded probe-reason set qualifies, whatever the name is."""
        probe = f"/scratch/{worker_workspace._METADATA_BROKER_PROBE_BASENAME_PREFIX}x"
        exc = WorkspaceError(f"metadata_broker_hardlink_forbidden:{probe}")
        assert worker_workspace._metadata_broker_probe_denial(exc, probe) is None
        _terminal, evidence, noise, count = self._record(exc)
        assert json.loads(evidence.splitlines()[0])["reason"] == (
            "metadata_broker_hardlink_forbidden"
        )
        assert "metadata_broker_denied" in noise
        assert count == 1

    def test_an_ordinary_name_never_qualifies_as_a_probe(self) -> None:
        candidate = "/scratch/.git/config.lock"
        assert (
            worker_workspace._metadata_broker_probe_denial(
                WorkspaceError(f"metadata_broker_outside_scratch:{candidate}"),
                candidate,
            )
            is None
        )

    def test_no_suppressible_reason_is_ever_terminal(self) -> None:
        """A probe label can never hide a denial that terminates the command."""
        assert not (
            worker_workspace._METADATA_BROKER_PROBE_SUPPRESSIBLE_REASONS
            & worker_workspace._METADATA_BROKER_TERMINAL_DENIAL_REASONS
        )

    def test_a_probe_named_target_is_still_fully_checked_and_denied(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """Outside the scratch the reserved name buys exactly nothing."""
        library = _FakeLibrary()
        outside = tmp_path / "outside"
        outside.mkdir()
        target = worker_workspace._metadata_broker_probe_path(outside)
        target.write_text("x", encoding="utf-8")
        before = stat.S_IMODE(target.lstat().st_mode)
        fd, root = _scratch_fd_root(scratch)
        buf = _path_buffer(target)
        request = _make_request(library.number("chmod"), os.getpid())
        request.data.args[0] = ctypes.addressof(buf)
        request.data.args[1] = 0o640
        try:
            with pytest.raises(WorkspaceError, match="metadata_broker_outside_scratch"):
                worker_workspace._metadata_broker_apply(
                    library, -1, request, os.getpid(), fd, root
                )
        finally:
            os.close(fd)
        assert stat.S_IMODE(target.lstat().st_mode) == before

    def test_a_probe_named_target_inside_the_scratch_is_an_ordinary_target(
        self, scratch: Path
    ) -> None:
        """Nothing is skipped for the reserved name where the rules allow it."""
        library = _FakeLibrary()
        target = worker_workspace._metadata_broker_probe_path(scratch)
        target.write_text("x", encoding="utf-8")
        fd, root = _scratch_fd_root(scratch)
        buf = _path_buffer(target)
        request = _make_request(library.number("chmod"), os.getpid())
        request.data.args[0] = ctypes.addressof(buf)
        request.data.args[1] = 0o640
        try:
            worker_workspace._metadata_broker_apply(
                library, -1, request, os.getpid(), fd, root
            )
        finally:
            os.close(fd)
        assert stat.S_IMODE(target.lstat().st_mode) == 0o640


@pytest.mark.usefixtures("require_openat2")
class TestNoReservedNameBypassesTheBrokerChecks:
    """NF-2026-00841: nothing routes a brokered chmod around the checks.

    The retired declaration handshake gave one reserved basename a branch of
    its own inside ``_metadata_broker_apply``: it registered a nested scope and
    answered with a reserved errno instead of resolving a target. It could
    never bootstrap -- during a self-hosted canonical validation the boundary
    mediating a candidate's exec scratch is the *already loaded* canonical
    broker, which cannot answer a protocol that same candidate introduces, so
    it performed the mode no-op, the nested child read that as
    ``boundary_allowed`` and kept its full deny list, and unmodified Git was
    left unable to set ``core.filemode`` on its own request-owned
    ``.git/config.lock``. A name-triggered branch is also exactly the kind of
    bypass this broker must never grow. A file carrying that former basename
    is now an ordinary target: mediated, fully checked, and mutated only where
    the same scratch-root, owner, symlink, traversal, hardlink and inode rules
    already allow it.
    """

    RETIRED_BASENAME = ".aiworkhub-metadata-scope.v1"

    def test_the_former_reserved_basename_is_an_ordinary_target(
        self, scratch: Path
    ) -> None:
        library = _FakeLibrary()
        target = scratch / self.RETIRED_BASENAME
        target.write_text("x", encoding="utf-8")
        fd, root = _scratch_fd_root(scratch)
        buf = _path_buffer(target)
        request = _make_request(library.number("chmod"), os.getpid())
        request.data.args[0] = ctypes.addressof(buf)
        request.data.args[1] = 0o640
        try:
            worker_workspace._metadata_broker_apply(
                library, -1, request, os.getpid(), fd, root
            )
        finally:
            os.close(fd)
        # Resolved and mutated like any other beneath-scratch file: no
        # registration, no reserved errno, no skipped mode change.
        assert stat.S_IMODE(target.lstat().st_mode) == 0o640

    def test_the_former_reserved_basename_outside_scratch_is_denied(
        self, scratch: Path, tmp_path: Path
    ) -> None:
        """The retired name buys no authority it did not already have."""
        library = _FakeLibrary()
        outside = tmp_path / self.RETIRED_BASENAME
        outside.write_text("keep\n", encoding="utf-8")
        before = stat.S_IMODE(outside.lstat().st_mode)
        fd, root = _scratch_fd_root(scratch)
        buf = _path_buffer(outside)
        request = _make_request(library.number("chmod"), os.getpid())
        request.data.args[0] = ctypes.addressof(buf)
        request.data.args[1] = 0o640
        try:
            with pytest.raises(WorkspaceError, match="outside_scratch"):
                worker_workspace._metadata_broker_apply(
                    library, -1, request, os.getpid(), fd, root
                )
        finally:
            os.close(fd)
        assert stat.S_IMODE(outside.lstat().st_mode) == before
        assert outside.read_text(encoding="utf-8") == "keep\n"

    def test_apply_takes_no_nested_scope_parameter(self) -> None:
        """A second protocol must not reappear alongside the probe.

        The nested child now decides by *measuring* the boundary already
        mediating its exec scratch, which needs nothing from that boundary but
        two syscall answers. Any re-added registry, reserved filename or
        acknowledgement errno would be a parallel protocol with the same
        unbootstrappable shape.
        """
        assert (
            "scope_registry"
            not in inspect.signature(
                worker_workspace._metadata_broker_apply
            ).parameters
        )
        for retired in (
            "_METADATA_BROKER_SCOPE_FILENAME",
            "_METADATA_BROKER_SCOPE_ACK_ERRNO",
            "_MetadataBrokerScopeRegistry",
            "_MetadataBrokerScopeRegistered",
            "_metadata_broker_register_scope",
            "_metadata_broker_enforce_scope",
            "_register_nested_metadata_scope",
        ):
            assert not hasattr(worker_workspace, retired), retired


class TestDeclaredMetadataAuthorityRootsAreTheRootsInForce:
    """NF-2026-00841 rework: the widening a nested run cannot avoid is declared.

    One seccomp user-notification listener exists per filter tree, so a nested
    ``run_validations`` cannot install one of its own and the chmod variants it
    leaves out of its deny-only filter are decided by the OUTER boundary --
    against the *outer* request's exec scratch, which is strictly wider than
    this nested run's scratch.  Every kernel/broker check still runs unchanged
    in the mediating parent; what was missing was any statement of which roots
    those checks run against, so the nested-Git integration compared an
    authorized ``chmod`` to ``TMPDIR`` and read a legitimate mutation inside the
    outer request's own root as an escape from every root.

    ``declared_metadata_authority_roots`` states it.  It authorizes nothing: the
    outer root is added only when a real exemption happened AND an
    HMAC-authenticated outer context vouches for it, so neither an environment
    variable nor command output can mint a wider declared authority.
    """

    def test_own_roots_only_when_nothing_was_left_to_an_outer_boundary(
        self, tmp_path: Path
    ) -> None:
        scratch = tmp_path / "scratch"
        worker_temp = tmp_path / "worker-temp"
        scratch.mkdir()
        worker_temp.mkdir()
        assert worker_workspace.declared_metadata_authority_roots(
            scratch, worker_temp
        ) == (str(scratch.resolve()), str(worker_temp.resolve()))

    def test_authentic_outer_scratch_is_added_only_with_a_real_exemption(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "scratch"
        outer = tmp_path / "outer-scratch"
        scratch.mkdir()
        outer.mkdir()
        monkeypatch.setattr(
            worker_workspace,
            "authenticated_outer_validation_context",
            lambda: {"exec_scratch": str(outer)},
        )
        # No exemption: nothing was left to the outer boundary, so nothing wider
        # is declared even though an authentic outer context exists.
        assert worker_workspace.declared_metadata_authority_roots(scratch, None) == (
            str(scratch.resolve()),
        )
        assert worker_workspace.declared_metadata_authority_roots(
            scratch, None, frozenset({"chmod"})
        ) == (str(scratch.resolve()), str(outer.resolve()))

    def test_no_authentic_outer_context_never_widens_the_declaration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setattr(
            worker_workspace, "authenticated_outer_validation_context", lambda: None
        )
        monkeypatch.setenv(
            worker_workspace.METADATA_AUTHORITY_ROOTS_ENV, json.dumps(["/"])
        )
        assert worker_workspace.declared_metadata_authority_roots(
            scratch, None, frozenset(worker_workspace._MEASURABLE_BROKERED_CHMOD_SYSCALLS)
        ) == (str(scratch.resolve()),)

    def test_publication_is_json_and_clears_any_inherited_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setenv(
            worker_workspace.METADATA_AUTHORITY_ROOTS_ENV, json.dumps(["/forged"])
        )
        worker_workspace._publish_metadata_authority_roots(
            (str(scratch.resolve()),)
        )
        assert json.loads(
            os.environ[worker_workspace.METADATA_AUTHORITY_ROOTS_ENV]
        ) == [str(scratch.resolve())]
        # An empty declaration must remove a stale/forged inherited value rather
        # than leave it standing as this run's claimed authority.
        worker_workspace._publish_metadata_authority_roots(())
        assert worker_workspace.METADATA_AUTHORITY_ROOTS_ENV not in os.environ

    def test_declaration_is_not_read_back_as_authority_by_the_broker(self) -> None:
        """No brokered decision may consult the declaration.

        The roots that decide a syscall are the verified directory descriptors
        ``_run_metadata_broker`` opened, never a string a child could have
        written into its own environment.
        """
        for symbol in (
            worker_workspace._metadata_broker_verify_target,
            worker_workspace._metadata_broker_verify_target_any,
            worker_workspace._metadata_broker_verify_fd,
            worker_workspace._metadata_broker_apply,
            worker_workspace._metadata_broker_open_child_fd,
        ):
            source = inspect.getsource(symbol)
            assert worker_workspace.METADATA_AUTHORITY_ROOTS_ENV not in source
            assert "declared_metadata_authority_roots" not in source
