"""Check or sync generated AIWorkHub tool-use policy blocks."""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

try:
    from aiworkhub import platform_io
except ImportError:
    platform_io = None


REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_SOURCE = Path("docs/AIWORKHUB_TOOL_USE_POLICY.md")
SCRIPT_PATH = Path("scripts/sync_aiworkhub_tool_policy.py")
HOST_FILES = (
    Path("AGENTS.md"),
    Path("CLAUDE.md"),
    Path(".github/copilot-instructions.md"),
)
START_MARKER = "<!-- AIWORKHUB_TOOL_USE_POLICY_START -->"
END_MARKER = "<!-- AIWORKHUB_TOOL_USE_POLICY_END -->"
START_MARKER_BYTES = START_MARKER.encode("utf-8")
END_MARKER_BYTES = END_MARKER.encode("utf-8")
WRITE_GATE_ENV = "AIWORKHUB_ALLOW_WRITES"
STAGED_NAME_PREFIX = "aiworkhub-policy-sync"
STAGED_NAME_ATTEMPTS = 128


@dataclass(frozen=True)
class PolicyBlock:
    start: int
    end: int
    inner: bytes


@dataclass(frozen=True)
class HostUpdate:
    root: Path
    relative_path: Path
    path: Path
    original: bytes
    updated: bytes
    mode: int
    original_identity: "FileIdentity"


@dataclass(frozen=True)
class FileSnapshot:
    data: bytes
    identity: "FileIdentity"


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    is_regular: bool
    nlink: int | None
    uid: int | None
    gid: int | None
    size: int
    digest: str


@dataclass(frozen=True)
class AuthenticatedPath:
    root: Path
    relative_path: Path
    path: Path
    parent_fd: int
    leaf_name: str
    purpose: str


StagedFileIdentity = FileIdentity


@dataclass(frozen=True)
class StagedUpdate:
    update: HostUpdate
    temp_path: Path
    temp_relative_path: Path
    temp_identity: StagedFileIdentity
    backup_path: Path
    backup_relative_path: Path
    backup_identity: StagedFileIdentity


@dataclass(frozen=True)
class BackupMovedUpdate:
    staged: StagedUpdate


@dataclass(frozen=True)
class ReplacementInstalledUpdate:
    staged: StagedUpdate
    installed_identity: FileIdentity


RollbackUpdate = BackupMovedUpdate | ReplacementInstalledUpdate


class PolicySyncError(Exception):
    """Raised when a policy file is missing, malformed, reordered, or drifted."""


def read_bytes(path: Path, relative_path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise PolicySyncError(f"{relative_path}: file is missing") from exc
    except OSError as exc:
        raise PolicySyncError(f"{relative_path}: cannot read file: {exc.strerror or exc}") from exc


def _reject_message(path: Path, purpose: str) -> str:
    if purpose == "staged":
        return f"{path}: refusing to authenticate staged symlink"
    return f"{path}: refusing to sync through symlink"


def _cannot_authenticate_message(path: Path, purpose: str, detail: str) -> str:
    if purpose == "staged":
        return f"{path}: cannot authenticate staged file: {detail}"
    return f"{path}: cannot authenticate file: {detail}"


def _validated_relative_path(relative_path: Path) -> Path:
    if relative_path.is_absolute() or not relative_path.parts:
        raise PolicySyncError(f"{relative_path}: path must be repository-relative")
    if any(part in ("", ".", "..") for part in relative_path.parts):
        raise PolicySyncError(f"{relative_path}: path must not escape the repository root")
    return relative_path


@contextmanager
def authenticated_path(
    root: Path,
    relative_path: Path,
    display_path: Path,
    purpose: str = "host",
) -> Iterator[AuthenticatedPath]:
    relative_path = _validated_relative_path(relative_path)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if no_follow is not None:
        root_flags |= no_follow
    try:
        parent_fd = os.open(root, root_flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PolicySyncError(_reject_message(display_path, purpose)) from exc
        raise PolicySyncError(
            _cannot_authenticate_message(display_path, purpose, exc.strerror or str(exc))
        ) from exc

    try:
        for component in relative_path.parts[:-1]:
            if no_follow is None:
                raise PolicySyncError(
                    f"{display_path}: cannot authenticate parent directory without an enforceable no-follow open primitive"
                )
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | no_follow,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PolicySyncError(_reject_message(display_path, purpose)) from exc
                if exc.errno == errno.ENOTDIR:
                    try:
                        component_stat = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                    except OSError:
                        pass
                    else:
                        if stat.S_ISLNK(component_stat.st_mode):
                            raise PolicySyncError(_reject_message(display_path, purpose)) from exc
                raise PolicySyncError(
                    _cannot_authenticate_message(display_path, purpose, exc.strerror or str(exc))
                ) from exc
            try:
                directory_stat = os.fstat(next_fd)
                if not stat.S_ISDIR(directory_stat.st_mode):
                    raise PolicySyncError(f"{display_path}: parent component is not a directory")
            except Exception:
                os.close(next_fd)
                raise
            os.close(parent_fd)
            parent_fd = next_fd

        yield AuthenticatedPath(
            root=root,
            relative_path=relative_path,
            path=root / relative_path,
            parent_fd=parent_fd,
            leaf_name=relative_path.name,
            purpose=purpose,
        )
    finally:
        os.close(parent_fd)


def _relative_from_path(path: Path) -> tuple[Path, Path]:
    absolute_path = path.absolute()
    parent = absolute_path.parent
    return parent, Path(absolute_path.name)


def platform_io_regular_file_snapshot(path: Path, relative_path: Path) -> FileSnapshot | None:
    if platform_io is None:
        return None
    read_snapshot = getattr(platform_io, "read_regular_file_snapshot", None)
    if read_snapshot is None:
        return None
    try:
        snapshot = read_snapshot(path, relative_path)
    except FileNotFoundError as exc:
        raise PolicySyncError(f"{relative_path}: file is missing") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PolicySyncError(f"{relative_path}: refusing to sync through symlink") from exc
        raise PolicySyncError(f"{relative_path}: cannot authenticate file: {exc.strerror or exc}") from exc
    if not isinstance(snapshot, FileSnapshot):
        raise PolicySyncError(f"{relative_path}: platform_io returned an invalid file snapshot")
    if not snapshot.identity.is_regular:
        raise PolicySyncError(f"{relative_path}: refusing to sync non-regular file")
    return snapshot


def read_regular_file_snapshot(path: Path, relative_path: Path, root: Path | None = None) -> FileSnapshot:
    if root is None:
        root, relative_path = _relative_from_path(path)

    with authenticated_path(root, relative_path, relative_path) as authenticated:
        platform_snapshot = platform_io_regular_file_snapshot(path, relative_path)
        if platform_snapshot is not None:
            return platform_snapshot

        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise PolicySyncError(
                f"{relative_path}: cannot authenticate file without an enforceable no-follow open primitive"
            )
        flags = os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(authenticated.leaf_name, flags, dir_fd=authenticated.parent_fd)
        except FileNotFoundError as exc:
            raise PolicySyncError(f"{relative_path}: file is missing") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PolicySyncError(f"{relative_path}: refusing to sync through symlink") from exc
            raise PolicySyncError(f"{relative_path}: cannot authenticate file: {exc.strerror or exc}") from exc

        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise PolicySyncError(f"{relative_path}: refusing to sync non-regular file")

            digest = hashlib.sha256()
            chunks = []
            with os.fdopen(fd, "rb") as host_file:
                for chunk in iter(lambda: host_file.read(1024 * 1024), b""):
                    chunks.append(chunk)
                    digest.update(chunk)
            data = b"".join(chunks)

            try:
                path_stat = os.stat(authenticated.leaf_name, dir_fd=authenticated.parent_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise PolicySyncError(f"{relative_path}: file was removed while authenticating") from exc
            except OSError as exc:
                raise PolicySyncError(f"{relative_path}: cannot inspect file: {exc.strerror or exc}") from exc
            if (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                raise PolicySyncError(f"{relative_path}: file identity changed while authenticating")

            return FileSnapshot(
                data=data,
                identity=FileIdentity(
                    device=file_stat.st_dev,
                    inode=file_stat.st_ino,
                    mode=file_stat.st_mode,
                    is_regular=True,
                    nlink=getattr(file_stat, "st_nlink", None),
                    uid=getattr(file_stat, "st_uid", None),
                    gid=getattr(file_stat, "st_gid", None),
                    size=file_stat.st_size,
                    digest=digest.hexdigest(),
                ),
            )
        except PolicySyncError:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        except OSError as exc:
            raise PolicySyncError(f"{relative_path}: cannot read file: {exc.strerror or exc}") from exc


def read_canonical(root: Path = REPO_ROOT) -> bytes:
    data = read_regular_file_snapshot(root / POLICY_SOURCE, POLICY_SOURCE, root).data
    if not data.endswith(b"\n"):
        raise PolicySyncError(f"{POLICY_SOURCE}: canonical policy source must end with newline")
    return data


def generated_block(canonical: bytes) -> bytes:
    return START_MARKER_BYTES + b"\n" + canonical + END_MARKER_BYTES


def extract_block(data: bytes, path: Path) -> PolicyBlock:
    start_count = data.count(START_MARKER_BYTES)
    end_count = data.count(END_MARKER_BYTES)
    if start_count != 1 or end_count != 1:
        raise PolicySyncError(
            f"{path}: expected one policy block, found "
            f"{start_count} start marker(s) and {end_count} end marker(s)"
        )

    start = data.index(START_MARKER_BYTES)
    inner_start = start + len(START_MARKER_BYTES)
    end = data.index(END_MARKER_BYTES)
    if end < inner_start:
        raise PolicySyncError(f"{path}: policy markers are reordered")
    if data[inner_start : inner_start + 1] == b"\n":
        inner_start += 1
    elif data[inner_start : inner_start + 2] == b"\r\n":
        inner_start += 2
    else:
        raise PolicySyncError(f"{path}: start marker must be followed by newline")
    return PolicyBlock(start=start, end=end + len(END_MARKER_BYTES), inner=data[inner_start:end])


def synced_text(data: bytes, canonical: bytes, path: Path) -> bytes:
    block = extract_block(data, path)
    return data[: block.start] + generated_block(canonical) + data[block.end :]


def check(root: Path = REPO_ROOT, host_files: Sequence[Path] = HOST_FILES) -> list[str]:
    try:
        canonical = read_canonical(root)
    except PolicySyncError as exc:
        return [str(exc)]

    errors = []
    for relative_path in host_files:
        path = root / relative_path
        try:
            data = read_regular_file_snapshot(path, relative_path, root).data
            block = extract_block(data, relative_path)
        except PolicySyncError as exc:
            errors.append(str(exc))
            continue
        if data[block.start : block.end] != generated_block(canonical):
            errors.append(f"{relative_path}: policy block differs from {POLICY_SOURCE}")
    return errors


def planned_updates(root: Path, canonical: bytes, host_files: Sequence[Path]) -> list[HostUpdate]:
    updates = []
    for relative_path in host_files:
        path = root / relative_path
        snapshot = read_regular_file_snapshot(path, relative_path, root)
        mode = snapshot.identity.mode & 0o7777
        updated = synced_text(snapshot.data, canonical, relative_path)
        updates.append(
            HostUpdate(
                root=root,
                relative_path=relative_path,
                path=path,
                original=snapshot.data,
                updated=updated,
                mode=mode,
                original_identity=snapshot.identity,
            )
        )
    return updates


def require_write_gate() -> None:
    if os.environ.get(WRITE_GATE_ENV) != "1":
        raise PolicySyncError(f"--sync requires {WRITE_GATE_ENV}=1")


def staged_path(path: Path, role: str) -> Path:
    return path.with_name(f".{path.name}.{STAGED_NAME_PREFIX}.{secrets.token_hex(16)}.{role}")


def open_new_staged_file(path: Path, flags: int, mode: int) -> int:
    root, relative_path = _relative_from_path(path)
    with authenticated_path(root, relative_path, path, "staged") as authenticated:
        fd = os.open(authenticated.leaf_name, flags, 0o600, dir_fd=authenticated.parent_fd)
    try:
        os.fchmod(fd, mode)
    except OSError:
        os.close(fd)
        try:
            authenticated_unlink(path)
        except OSError:
            pass
        raise
    return fd


def _fstat_identity(fd: int, digest: str) -> StagedFileIdentity:
    file_stat = os.fstat(fd)
    return FileIdentity(
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        mode=file_stat.st_mode,
        is_regular=stat.S_ISREG(file_stat.st_mode),
        nlink=getattr(file_stat, "st_nlink", None),
        uid=getattr(file_stat, "st_uid", None),
        gid=getattr(file_stat, "st_gid", None),
        size=file_stat.st_size,
        digest=digest,
    )


def platform_io_staged_file_snapshot(path: Path) -> FileSnapshot | None:
    if platform_io is None:
        return None
    read_snapshot = getattr(platform_io, "read_regular_file_snapshot", None)
    if read_snapshot is None:
        return None
    try:
        snapshot = read_snapshot(path, path)
    except FileNotFoundError as exc:
        raise PolicySyncError(f"{path}: staged file was removed before install") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PolicySyncError(f"{path}: refusing to authenticate staged symlink") from exc
        raise PolicySyncError(f"{path}: cannot authenticate staged file: {exc.strerror or exc}") from exc
    if not isinstance(snapshot, FileSnapshot):
        raise PolicySyncError(f"{path}: platform_io returned an invalid staged file snapshot")
    if not snapshot.identity.is_regular:
        raise PolicySyncError(f"{path}: staged path is not a regular file")
    return snapshot


def staged_file_identity(
    path: Path,
    root: Path | None = None,
    relative_path: Path | None = None,
) -> StagedFileIdentity:
    if root is None or relative_path is None:
        root, relative_path = _relative_from_path(path)
    with authenticated_path(root, relative_path, path, "staged") as authenticated:
        platform_snapshot = platform_io_staged_file_snapshot(path)
        if platform_snapshot is not None:
            return platform_snapshot.identity

        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise PolicySyncError(
                f"{path}: cannot authenticate staged file without an enforceable no-follow open primitive"
            )

        flags = os.O_RDONLY | no_follow
        try:
            fd = os.open(authenticated.leaf_name, flags, dir_fd=authenticated.parent_fd)
        except FileNotFoundError as exc:
            raise PolicySyncError(f"{path}: staged file was removed before install") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PolicySyncError(f"{path}: refusing to authenticate staged symlink") from exc
            raise PolicySyncError(f"{path}: cannot authenticate staged file: {exc.strerror or exc}") from exc

        try:
            file_stat = os.fstat(fd)
            digest = hashlib.sha256()
            with os.fdopen(fd, "rb") as staged_file:
                for chunk in iter(lambda: staged_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            try:
                path_stat = os.stat(authenticated.leaf_name, dir_fd=authenticated.parent_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise PolicySyncError(f"{path}: staged file was removed while authenticating") from exc
            except OSError as exc:
                raise PolicySyncError(f"{path}: cannot inspect staged file: {exc.strerror or exc}") from exc
            if (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                raise PolicySyncError(f"{path}: staged file identity changed while authenticating")
            return FileIdentity(
                device=file_stat.st_dev,
                inode=file_stat.st_ino,
                mode=file_stat.st_mode,
                is_regular=stat.S_ISREG(file_stat.st_mode),
                nlink=getattr(file_stat, "st_nlink", None),
                uid=getattr(file_stat, "st_uid", None),
                gid=getattr(file_stat, "st_gid", None),
                size=file_stat.st_size,
                digest=digest.hexdigest(),
            )
        except PolicySyncError:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        except OSError as exc:
            raise PolicySyncError(f"{path}: cannot read staged file: {exc.strerror or exc}") from exc


def verify_staged_identity(
    path: Path,
    expected: StagedFileIdentity,
    root: Path | None = None,
    relative_path: Path | None = None,
) -> None:
    current = staged_file_identity(path, root, relative_path)
    if current != expected:
        raise PolicySyncError(f"{path}: staged file was replaced or changed before install")
    if not current.is_regular:
        raise PolicySyncError(f"{path}: staged path is not a regular file")


def host_file_identity(path: Path, relative_path: Path, root: Path | None = None) -> FileIdentity:
    return read_regular_file_snapshot(path, relative_path, root).identity


def verify_host_identity(update: HostUpdate) -> None:
    current = host_file_identity(update.path, update.relative_path, update.root)
    if current != update.original_identity:
        raise PolicySyncError(f"{update.relative_path}: file changed after preflight; aborting without overwrite")


def create_private_staged_file(path: Path, data: bytes, mode: int) -> tuple[Path, StagedFileIdentity]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(STAGED_NAME_ATTEMPTS):
        temp_path = staged_path(path, "tmp")
        try:
            fd = open_new_staged_file(temp_path, flags, mode)
        except FileExistsError:
            continue
        except OSError as exc:
            raise PolicySyncError(
                f"{path}: cannot create private staged file: {exc.strerror or exc}"
            ) from exc

        try:
            with os.fdopen(fd, "wb") as staged_file:
                staged_file.write(data)
        except OSError as exc:
            cleanup_staged_best_effort([temp_path])
            raise PolicySyncError(f"{path}: cannot write private staged file: {exc.strerror or exc}") from exc
        return temp_path, staged_file_identity(temp_path)

    raise PolicySyncError(f"{path}: cannot create private staged file after repeated name collisions")


def reserve_private_backup_path(path: Path) -> tuple[Path, StagedFileIdentity]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(STAGED_NAME_ATTEMPTS):
        backup_path = staged_path(path, "bak")
        try:
            fd = open_new_staged_file(backup_path, flags, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise PolicySyncError(
                f"{path}: cannot reserve private backup file: {exc.strerror or exc}"
            ) from exc
        try:
            identity = _fstat_identity(fd, hashlib.sha256(b"").hexdigest())
        finally:
            os.close(fd)
        return backup_path, identity

    raise PolicySyncError(f"{path}: cannot reserve private backup file after repeated name collisions")


def staged_relative_path(update: HostUpdate, path: Path) -> Path:
    return update.relative_path.with_name(path.name)


def reject_symlink_host(update: HostUpdate) -> None:
    try:
        if update.path.is_symlink():
            raise PolicySyncError(f"{update.relative_path}: refusing to sync through symlink")
    except OSError as exc:
        raise PolicySyncError(f"{update.relative_path}: cannot inspect file: {exc.strerror or exc}") from exc


def authenticated_unlink(path: Path, root: Path | None = None, relative_path: Path | None = None) -> None:
    if root is None or relative_path is None:
        root, relative_path = _relative_from_path(path)
    with authenticated_path(root, relative_path, path, "staged") as authenticated:
        os.unlink(authenticated.leaf_name, dir_fd=authenticated.parent_fd)


def authenticated_replace(
    src_path: Path,
    src_relative_path: Path,
    dst_path: Path,
    dst_relative_path: Path,
    root: Path,
) -> None:
    with authenticated_path(root, src_relative_path, src_path, "staged") as src:
        with authenticated_path(root, dst_relative_path, dst_path, "host") as dst:
            os.replace(
                src.leaf_name,
                dst.leaf_name,
                src_dir_fd=src.parent_fd,
                dst_dir_fd=dst.parent_fd,
            )


def cleanup_staged(paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            authenticated_unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PolicySyncError(f"{path}: cannot remove staged file: {exc.strerror or exc}") from exc


def cleanup_staged_best_effort(paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            authenticated_unlink(path)
        except OSError:
            pass


def cleanup_authenticated_staged_best_effort(
    staged_artifacts: Sequence[tuple[Path, StagedFileIdentity, Path, Path]],
) -> None:
    for path, expected_identity, root, relative_path in staged_artifacts:
        try:
            current_identity = staged_file_identity(path, root, relative_path)
            if current_identity == expected_identity and current_identity.is_regular:
                authenticated_unlink(path, root, relative_path)
        except OSError:
            pass
        except PolicySyncError:
            pass


def staged_artifacts(staged_updates: Sequence[StagedUpdate]) -> list[tuple[Path, StagedFileIdentity, Path, Path]]:
    return [
        artifact
        for staged in staged_updates
        for artifact in (
            (staged.temp_path, staged.temp_identity, staged.update.root, staged.temp_relative_path),
            (staged.backup_path, staged.backup_identity, staged.update.root, staged.backup_relative_path),
        )
    ]


def restore_missing_host_from_backup(staged: StagedUpdate) -> None:
    with authenticated_path(staged.update.root, staged.update.relative_path, staged.update.relative_path) as host:
        try:
            current = os.stat(host.leaf_name, dir_fd=host.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PolicySyncError(
                f"{staged.update.relative_path}: cannot inspect rollback target: {exc.strerror or exc}"
            ) from exc
        else:
            if stat.S_ISLNK(current.st_mode):
                raise PolicySyncError(
                    f"{staged.update.relative_path}: rollback conflict; destination symlink appeared after sync failure"
                )
            raise PolicySyncError(
                f"{staged.update.relative_path}: rollback conflict; destination appeared after sync failure"
            )

    backup_identity = staged_file_identity(
        staged.backup_path,
        staged.update.root,
        staged.backup_relative_path,
    )
    if backup_identity != staged.update.original_identity:
        raise PolicySyncError(f"{staged.update.relative_path}: backup changed before rollback")
    if not backup_identity.is_regular:
        raise PolicySyncError(f"{staged.update.relative_path}: backup is not a regular file")
    try:
        with authenticated_path(staged.update.root, staged.backup_relative_path, staged.backup_path, "staged") as backup:
            with authenticated_path(staged.update.root, staged.update.relative_path, staged.update.relative_path) as host:
                os.link(
                    backup.leaf_name,
                    host.leaf_name,
                    src_dir_fd=backup.parent_fd,
                    dst_dir_fd=host.parent_fd,
                )
        authenticated_unlink(staged.backup_path, staged.update.root, staged.backup_relative_path)
    except FileExistsError as exc:
        raise PolicySyncError(
            f"{staged.update.relative_path}: rollback conflict; destination appeared after sync failure"
        ) from exc
    except OSError as exc:
        raise PolicySyncError(f"{staged.update.relative_path}: {exc.strerror or exc}") from exc


def rollback_applied(applied: Sequence[RollbackUpdate]) -> None:
    rollback_errors = []
    for applied_update in reversed(applied):
        staged = applied_update.staged
        try:
            if isinstance(applied_update, BackupMovedUpdate):
                restore_missing_host_from_backup(staged)
            else:
                current_identity = host_file_identity(staged.update.path, staged.update.relative_path, staged.update.root)
                if current_identity != applied_update.installed_identity:
                    rollback_errors.append(
                        f"{staged.update.relative_path}: rollback conflict; installed file changed after sync failure"
                    )
                    continue
                authenticated_replace(
                    staged.backup_path,
                    staged.backup_relative_path,
                    staged.update.path,
                    staged.update.relative_path,
                    staged.update.root,
                )
        except PolicySyncError as exc:
            rollback_errors.append(str(exc))
        except OSError as exc:
            rollback_errors.append(f"{staged.update.relative_path}: {exc.strerror or exc}")
    if rollback_errors:
        raise PolicySyncError("sync failed and rollback failed: " + "; ".join(rollback_errors))


def write_changed_updates(changed_updates: Sequence[HostUpdate]) -> None:
    staged_updates = []
    pending_staged_artifacts: list[tuple[Path, StagedFileIdentity, Path, Path]] = []

    try:
        for update in changed_updates:
            reject_symlink_host(update)
            temp_path, temp_identity = create_private_staged_file(update.path, update.updated, update.mode)
            temp_relative_path = staged_relative_path(update, temp_path)
            pending_staged_artifacts.append((temp_path, temp_identity, update.root, temp_relative_path))
            backup_path, backup_identity = reserve_private_backup_path(update.path)
            backup_relative_path = staged_relative_path(update, backup_path)
            pending_staged_artifacts.append((backup_path, backup_identity, update.root, backup_relative_path))
            staged_updates.append(
                StagedUpdate(
                    update=update,
                    temp_path=temp_path,
                    temp_relative_path=temp_relative_path,
                    temp_identity=temp_identity,
                    backup_path=backup_path,
                    backup_relative_path=backup_relative_path,
                    backup_identity=backup_identity,
                )
            )
            pending_staged_artifacts.clear()
    except (OSError, PolicySyncError) as exc:
        cleanup_authenticated_staged_best_effort(staged_artifacts(staged_updates) + pending_staged_artifacts)
        if isinstance(exc, PolicySyncError):
            raise
        raise PolicySyncError(exc.strerror or str(exc)) from exc

    rollback_updates: list[RollbackUpdate] = []
    try:
        for staged in staged_updates:
            reject_symlink_host(staged.update)
            verify_staged_identity(
                staged.backup_path,
                staged.backup_identity,
                staged.update.root,
                staged.backup_relative_path,
            )
            verify_staged_identity(
                staged.temp_path,
                staged.temp_identity,
                staged.update.root,
                staged.temp_relative_path,
            )
            verify_host_identity(staged.update)
            authenticated_replace(
                staged.update.path,
                staged.update.relative_path,
                staged.backup_path,
                staged.backup_relative_path,
                staged.update.root,
            )
            rollback_updates.append(BackupMovedUpdate(staged=staged))
            verify_staged_identity(
                staged.temp_path,
                staged.temp_identity,
                staged.update.root,
                staged.temp_relative_path,
            )
            authenticated_replace(
                staged.temp_path,
                staged.temp_relative_path,
                staged.update.path,
                staged.update.relative_path,
                staged.update.root,
            )
            rollback_updates[-1] = ReplacementInstalledUpdate(
                staged=staged,
                installed_identity=staged.temp_identity,
            )
    except (OSError, PolicySyncError) as exc:
        message = exc.strerror if isinstance(exc, OSError) else str(exc)
        try:
            rollback_applied(rollback_updates)
            cleanup_authenticated_staged_best_effort(staged_artifacts(staged_updates))
        except PolicySyncError as cleanup_exc:
            raise PolicySyncError(
                f"sync failed before all hosts were updated: {message or exc}; {cleanup_exc}"
            ) from exc
        raise PolicySyncError(f"sync failed before all hosts were updated: {message or exc}") from exc

    cleanup_authenticated_staged_best_effort(
        [
            (
                staged.backup_path,
                staged.update.original_identity,
                staged.update.root,
                staged.backup_relative_path,
            )
            for staged in staged_updates
        ]
    )


def sync(root: Path = REPO_ROOT, host_files: Sequence[Path] = HOST_FILES) -> list[Path]:
    canonical = read_canonical(root)
    updates = planned_updates(root, canonical, host_files)
    changed_updates = [update for update in updates if update.updated != update.original]
    if changed_updates:
        require_write_gate()
        write_changed_updates(changed_updates)
    return [update.relative_path for update in changed_updates]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify policy blocks without writing")
    mode.add_argument("--sync", action="store_true", help="rewrite policy blocks from the canonical source")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def print_errors(errors: Sequence[str]) -> None:
    print("AIWorkHub tool-use policy blocks are not synchronized:")
    for error in errors:
        print(f"- {error}")
    print(f"Run: {WRITE_GATE_ENV}=1 python {SCRIPT_PATH.as_posix()} --sync")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.sync:
        try:
            changed = sync(args.root)
        except PolicySyncError as exc:
            print_errors([str(exc)])
            return 1
        for path in changed:
            print(f"synced {path}")
        return 0

    errors = check(args.root)
    if errors:
        print_errors(errors)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
