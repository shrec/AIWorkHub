"""Check or sync generated AIWorkHub tool-use policy blocks."""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
import secrets
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from aiworkhub import agent_tool_instructions

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
    # Windows has no dir_fd-based directory descriptor at all (see
    # platform_io.directory_descriptor_backend): ``parent_fd`` there is a
    # harmless ``-1`` sentinel, never a usable POSIX fd. The immediate parent
    # directory is instead pinned by this identity-authenticated, non-reparse
    # Windows HANDLE (``platform_io.OwnedWindowsHandle``), which every
    # Windows-side caller of ``authenticated_path`` uses instead of
    # ``parent_fd``. ``None`` on POSIX.
    windows_parent_handle: object | None = None


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


def _windows_directory_chain_primitives_available() -> bool:
    """Whether this host can authenticate a directory walk the Windows way.

    Distinct from ``platform_io is not None``: a caller (or a test double) may
    provide ``platform_io`` with only file-level helpers -- e.g. a fake
    ``read_regular_file_snapshot`` -- without the HANDLE-based directory
    primitives a *parent* authentication actually needs. Checking the exact
    attributes this module calls keeps the "no enforceable no-follow open
    primitive" fail-closed path honest about what it actually verified.
    """

    return (
        platform_io is not None
        and getattr(platform_io, "open_windows_root_directory_handle", None) is not None
        and getattr(platform_io, "open_windows_relative_child_directory", None) is not None
    )


def _authenticate_windows_directory_chain(
    root: Path,
    relative_path: Path,
    display_path: Path,
    purpose: str,
) -> "object":
    """Windows counterpart of the POSIX ``O_NOFOLLOW``/``dir_fd`` walk above.

    ``os.open()`` cannot open a directory at all on Windows (it always raises
    ``PermissionError``, regardless of flags) and ``os.supports_dir_fd`` is
    empty there, so there is no dir-fd chain to build. Instead this resolves
    the root and every intermediate component as a raw, identity-authenticated
    Windows HANDLE via ``platform_io``'s ``NtCreateFile``-based primitives,
    which reject a symlink/junction at any component atomically (the reparse
    point itself is opened, never its target) -- the same guarantee
    ``O_NOFOLLOW`` gives POSIX, raised through the same ``errno.ELOOP`` so this
    function's callers need only one branch to recognize it.
    """

    if not _windows_directory_chain_primitives_available():
        raise PolicySyncError(
            f"{display_path}: cannot authenticate parent directory without an enforceable no-follow open primitive"
        )
    try:
        handle = platform_io.open_windows_root_directory_handle(root)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PolicySyncError(_reject_message(display_path, purpose)) from exc
        raise PolicySyncError(
            _cannot_authenticate_message(display_path, purpose, exc.strerror or str(exc))
        ) from exc
    try:
        for component in relative_path.parts[:-1]:
            try:
                next_handle = platform_io.open_windows_relative_child_directory(handle.value, component)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PolicySyncError(_reject_message(display_path, purpose)) from exc
                raise PolicySyncError(
                    _cannot_authenticate_message(display_path, purpose, exc.strerror or str(exc))
                ) from exc
            handle.close()
            handle = next_handle
        return handle
    except BaseException:
        handle.close()
        raise


@contextmanager
def authenticated_path(
    root: Path,
    relative_path: Path,
    display_path: Path,
    purpose: str = "host",
) -> Iterator[AuthenticatedPath]:
    relative_path = _validated_relative_path(relative_path)

    if os.name == "nt":
        handle = _authenticate_windows_directory_chain(root, relative_path, display_path, purpose)
        try:
            yield AuthenticatedPath(
                root=root,
                relative_path=relative_path,
                path=root / relative_path,
                parent_fd=-1,
                leaf_name=relative_path.name,
                purpose=purpose,
                windows_parent_handle=handle,
            )
        finally:
            handle.close()
        return

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
            windows_parent_handle=None,
        )
    finally:
        os.close(parent_fd)


def _relative_from_path(path: Path) -> tuple[Path, Path]:
    absolute_path = path.absolute()
    parent = absolute_path.parent
    return parent, Path(absolute_path.name)


def _open_authenticated_leaf_for_read(authenticated: AuthenticatedPath, relative_path: Path) -> int:
    """Open the already-authenticated leaf for reading; caller owns the fd.

    Tries the portable ``dir_fd``-relative open first -- the real mechanism on
    POSIX. Windows defines neither ``O_DIRECTORY`` nor ``O_NOFOLLOW`` and
    ``os.supports_dir_fd`` is empty there, so ``os.open`` raises
    ``NotImplementedError`` immediately (before touching the filesystem) for
    any non-``None`` ``dir_fd`` -- a harmless, side-effect-free probe -- and
    this falls back to ``platform_io``'s HANDLE-relative primitive, which
    reuses the very same parent HANDLE :func:`authenticated_path` already
    authenticated.
    """

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is not None:
        flags = os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0)
        try:
            return os.open(authenticated.leaf_name, flags, dir_fd=authenticated.parent_fd)
        except NotImplementedError:
            pass
        except FileNotFoundError as exc:
            raise PolicySyncError(f"{relative_path}: file is missing") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PolicySyncError(f"{relative_path}: refusing to sync through symlink") from exc
            raise PolicySyncError(f"{relative_path}: cannot authenticate file: {exc.strerror or exc}") from exc

    if authenticated.windows_parent_handle is None:
        raise PolicySyncError(
            f"{relative_path}: cannot authenticate file without an enforceable no-follow open primitive"
        )
    try:
        return platform_io.open_windows_relative_regular_file_descriptor(
            authenticated.windows_parent_handle.value, authenticated.leaf_name
        )
    except FileNotFoundError as exc:
        raise PolicySyncError(f"{relative_path}: file is missing") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PolicySyncError(f"{relative_path}: refusing to sync through symlink") from exc
        raise PolicySyncError(f"{relative_path}: cannot authenticate file: {exc.strerror or exc}") from exc


def _verify_authenticated_leaf_identity_unchanged(
    authenticated: AuthenticatedPath,
    relative_path: Path,
    file_stat: os.stat_result,
) -> None:
    """Re-authenticate the leaf's identity after reading its content.

    The read itself came from a pinned descriptor/HANDLE that cannot drift,
    but this closes the same window the POSIX ``dir_fd`` re-``stat`` always
    closed: a same-name replace that lands *during* the read must still be
    caught rather than silently accepted as the file's content. Same
    probe-then-native-fallback shape as :func:`_open_authenticated_leaf_for_read`.
    """

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is not None:
        try:
            path_stat = os.stat(authenticated.leaf_name, dir_fd=authenticated.parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise PolicySyncError(f"{relative_path}: file was removed while authenticating") from exc
        except OSError as exc:
            raise PolicySyncError(f"{relative_path}: cannot inspect file: {exc.strerror or exc}") from exc
        if (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise PolicySyncError(f"{relative_path}: file identity changed while authenticating")
        return

    # Windows: no_follow is unavailable. Still attempt the same dir_fd-relative
    # probe purely for cross-platform code-path symmetry -- os.stat() there
    # raises NotImplementedError immediately for any non-None dir_fd, before
    # touching the filesystem, so this is side-effect free -- then fall back to
    # a HANDLE-relative reopen-and-compare using the same parent HANDLE
    # authenticated_path already pinned.
    try:
        os.stat(authenticated.leaf_name, dir_fd=authenticated.parent_fd, follow_symlinks=False)
    except NotImplementedError:
        pass

    if authenticated.windows_parent_handle is None:
        return
    try:
        second_fd = platform_io.open_windows_relative_regular_file_descriptor(
            authenticated.windows_parent_handle.value, authenticated.leaf_name
        )
    except FileNotFoundError as exc:
        raise PolicySyncError(f"{relative_path}: file was removed while authenticating") from exc
    except OSError as exc:
        raise PolicySyncError(f"{relative_path}: cannot inspect file: {exc.strerror or exc}") from exc
    try:
        path_stat = os.fstat(second_fd)
    finally:
        os.close(second_fd)
    if (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        raise PolicySyncError(f"{relative_path}: file identity changed while authenticating")


def _read_authenticated_regular_file(authenticated: AuthenticatedPath, relative_path: Path) -> FileSnapshot:
    """Read, hash and re-verify the leaf :func:`authenticated_path` pinned.

    Shared by :func:`read_regular_file_snapshot` (host/policy-source reads,
    which need the bytes) and :func:`staged_file_identity` (which only needs
    the identity) so both platforms' leaf-open logic lives in exactly one
    place.
    """

    fd = _open_authenticated_leaf_for_read(authenticated, relative_path)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PolicySyncError(f"{relative_path}: refusing to sync non-regular file")

        digest = hashlib.sha256()
        chunks = []
        with os.fdopen(fd, "rb") as source_file:
            for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                chunks.append(chunk)
                digest.update(chunk)
        data = b"".join(chunks)

        _verify_authenticated_leaf_identity_unchanged(authenticated, relative_path, file_stat)

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


def read_regular_file_snapshot(path: Path, relative_path: Path, root: Path | None = None) -> FileSnapshot:
    if root is None:
        root, relative_path = _relative_from_path(path)

    with authenticated_path(root, relative_path, relative_path) as authenticated:
        return _read_authenticated_regular_file(authenticated, relative_path)


def read_canonical(root: Path = REPO_ROOT) -> bytes:
    data = read_regular_file_snapshot(root / POLICY_SOURCE, POLICY_SOURCE, root).data
    if not data.endswith(b"\n"):
        raise PolicySyncError(f"{POLICY_SOURCE}: canonical policy source must end with newline")
    return data


def generated_block(canonical: bytes, provider: Path | str | None = None) -> bytes:
    provider_name = Path(provider).as_posix() if provider is not None else ""
    if provider_name in agent_tool_instructions.PROVIDERS:
        return agent_tool_instructions.render_projection(provider_name).rstrip("\n").encode(
            "utf-8"
        )
    return START_MARKER_BYTES + b"\n" + canonical + END_MARKER_BYTES


def extract_block(data: bytes, path: Path) -> PolicyBlock:
    # ``.as_posix()``, not the bare Path (whose ``str()`` uses the host's
    # native separator): these messages are compared against literal,
    # slash-spelled expectations regardless of platform.
    display = path.as_posix()
    start_count = data.count(START_MARKER_BYTES)
    end_count = data.count(END_MARKER_BYTES)
    if start_count != 1 or end_count != 1:
        raise PolicySyncError(
            f"{display}: expected one policy block, found "
            f"{start_count} start marker(s) and {end_count} end marker(s)"
        )

    start = data.index(START_MARKER_BYTES)
    inner_start = start + len(START_MARKER_BYTES)
    end = data.index(END_MARKER_BYTES)
    if end < inner_start:
        raise PolicySyncError(f"{display}: policy markers are reordered")
    if data[inner_start : inner_start + 1] == b"\n":
        inner_start += 1
    elif data[inner_start : inner_start + 2] == b"\r\n":
        inner_start += 2
    else:
        raise PolicySyncError(f"{display}: start marker must be followed by newline")
    return PolicyBlock(start=start, end=end + len(END_MARKER_BYTES), inner=data[inner_start:end])


def rendered_canonical() -> bytes:
    return agent_tool_instructions.render_canonical().encode("utf-8")


def canonical_source_error(canonical: bytes) -> str | None:
    if canonical == rendered_canonical():
        return None
    return f"{POLICY_SOURCE}: canonical policy source differs from agent_tool_instructions.render_canonical()"


def outside_scan(
    data: bytes, block: PolicyBlock, generated: bytes, path: Path
) -> agent_tool_instructions.OutsideScan:
    """Scan the host text around the block for copies of the rendered policy.

    The block replacer only rewrites between the markers, so a copy of the
    policy that once lived above the START marker survived every sync and
    every --check (CLAUDE.md carried 1,775 B of it). The scan is shared with
    the MCP apply plan in ``agent_tool_instructions``.

    ``generated`` is this host's own block, but the scan matches against every
    projection's rules: AGENTS.md then kept 1,018 B of the CLAUDE.md manager
    startup rules above its marker, renamed "Kilo", and a scan that knew only
    AGENTS.md's preamble-free block called them owner prose. A verbatim copy is
    reported and stripped by --sync; a copy whose wording differs is reported
    and left exactly as it is.
    """

    try:
        before = data[: block.start].decode("utf-8")
        after = data[block.end :].decode("utf-8")
        rendered = generated.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicySyncError(f"{path}: host text around the policy block is not UTF-8: {exc}") from exc
    return agent_tool_instructions.scan_outside_block(before, after, rendered)


def duplicate_error(path: Path, scan: agent_tool_instructions.OutsideScan) -> str | None:
    if scan.drifted:
        first = scan.drifted[0]
        shown = first if len(first) <= 60 else first[:57] + "..."
        return (
            f"{path}: {len(scan.drifted)} drifted policy line(s) outside the managed block; "
            f"remove or restore them by hand, the sync will not guess (first: {shown!r})"
        )
    if scan.verbatim:
        return f"{path}: {len(scan.verbatim)} policy line(s) duplicated outside the managed block"
    return None


def synced_text(data: bytes, canonical: bytes, path: Path) -> bytes:
    block = extract_block(data, path)
    generated = generated_block(canonical, path)
    scan = outside_scan(data, block, generated, path)
    if scan.fail_closed:
        raise PolicySyncError(duplicate_error(path, scan) or f"{path}: drifted policy text outside the managed block")
    return scan.before.encode("utf-8") + generated + scan.after.encode("utf-8")


def check(root: Path = REPO_ROOT, host_files: Sequence[Path] = HOST_FILES) -> list[str]:
    try:
        canonical = read_canonical(root)
    except PolicySyncError as exc:
        return [str(exc)]

    errors = []
    source_error = canonical_source_error(canonical)
    if source_error is not None:
        errors.append(source_error)
    for relative_path in host_files:
        path = root / relative_path
        try:
            data = read_regular_file_snapshot(path, relative_path, root).data
            block = extract_block(data, relative_path)
        except PolicySyncError as exc:
            errors.append(str(exc))
            continue
        generated = generated_block(canonical, relative_path)
        if data[block.start : block.end] != generated:
            errors.append(f"{relative_path}: policy block differs from {POLICY_SOURCE}")
        try:
            scan = outside_scan(data, block, generated, relative_path)
        except PolicySyncError as exc:
            errors.append(str(exc))
            continue
        outside_error = duplicate_error(relative_path, scan)
        if outside_error is not None:
            errors.append(outside_error)
    return errors


def planned_updates(root: Path, canonical: bytes, host_files: Sequence[Path]) -> list[HostUpdate]:
    updates = []
    if canonical_source_error(canonical) is not None:
        # The module POLICY is the source of truth; the docs copy is one more
        # projection and is regenerated through the same staged write path.
        snapshot = read_regular_file_snapshot(root / POLICY_SOURCE, POLICY_SOURCE, root)
        updates.append(
            HostUpdate(
                root=root,
                relative_path=POLICY_SOURCE,
                path=root / POLICY_SOURCE,
                original=snapshot.data,
                updated=rendered_canonical(),
                mode=snapshot.identity.mode & 0o7777,
                original_identity=snapshot.identity,
            )
        )
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


def _create_authenticated_staged_leaf(authenticated: AuthenticatedPath, flags: int) -> int:
    """Exclusively create the staged leaf; caller owns the returned fd.

    Every caller of :func:`open_new_staged_file` passes
    ``O_WRONLY | O_CREAT | O_EXCL`` (optionally ``O_NOFOLLOW``): the file must
    not already exist under any form -- regular file, directory, or
    symlink/reparse point. Same probe-then-native-fallback shape as
    :func:`_open_authenticated_leaf_for_read`: the portable ``dir_fd`` create
    is tried first and raises ``NotImplementedError`` immediately on Windows
    (no filesystem access attempted), falling back to
    :func:`platform_io.create_windows_relative_regular_file_descriptor`, whose
    ``FILE_CREATE`` disposition gives the same "fail if anything is already
    there" guarantee.
    """

    try:
        return os.open(authenticated.leaf_name, flags, 0o600, dir_fd=authenticated.parent_fd)
    except NotImplementedError:
        pass
    return platform_io.create_windows_relative_regular_file_descriptor(
        authenticated.windows_parent_handle.value, authenticated.leaf_name
    )


def open_new_staged_file(path: Path, flags: int, mode: int) -> int:
    root, relative_path = _relative_from_path(path)
    with authenticated_path(root, relative_path, path, "staged") as authenticated:
        fd = _create_authenticated_staged_leaf(authenticated, flags)
    if not hasattr(os, "fchmod"):
        # Windows has no POSIX mode bits to set (see
        # platform_io.chmod_fd): the secure O_EXCL creation above is the
        # authority there and this becomes a documented no-op, exactly like
        # chmod_fd's own Windows branch.
        return fd
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


def _open_authenticated_staged_leaf(authenticated: AuthenticatedPath, path: Path) -> int:
    """``_open_authenticated_leaf_for_read``'s counterpart, staged messages."""

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is not None:
        flags = os.O_RDONLY | no_follow
        try:
            return os.open(authenticated.leaf_name, flags, dir_fd=authenticated.parent_fd)
        except NotImplementedError:
            pass
        except FileNotFoundError as exc:
            raise PolicySyncError(f"{path}: staged file was removed before install") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PolicySyncError(f"{path}: refusing to authenticate staged symlink") from exc
            raise PolicySyncError(f"{path}: cannot authenticate staged file: {exc.strerror or exc}") from exc

    if authenticated.windows_parent_handle is None:
        raise PolicySyncError(
            f"{path}: cannot authenticate staged file without an enforceable no-follow open primitive"
        )
    try:
        return platform_io.open_windows_relative_regular_file_descriptor(
            authenticated.windows_parent_handle.value, authenticated.leaf_name
        )
    except FileNotFoundError as exc:
        raise PolicySyncError(f"{path}: staged file was removed before install") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PolicySyncError(f"{path}: refusing to authenticate staged symlink") from exc
        raise PolicySyncError(f"{path}: cannot authenticate staged file: {exc.strerror or exc}") from exc


def _verify_authenticated_staged_leaf_identity_unchanged(
    authenticated: AuthenticatedPath,
    path: Path,
    file_stat: os.stat_result,
) -> None:
    """``_verify_authenticated_leaf_identity_unchanged``'s counterpart, staged messages."""

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is not None:
        try:
            path_stat = os.stat(authenticated.leaf_name, dir_fd=authenticated.parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise PolicySyncError(f"{path}: staged file was removed while authenticating") from exc
        except OSError as exc:
            raise PolicySyncError(f"{path}: cannot inspect staged file: {exc.strerror or exc}") from exc
        if (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise PolicySyncError(f"{path}: staged file identity changed while authenticating")
        return

    try:
        os.stat(authenticated.leaf_name, dir_fd=authenticated.parent_fd, follow_symlinks=False)
    except NotImplementedError:
        pass

    if authenticated.windows_parent_handle is None:
        return
    try:
        second_fd = platform_io.open_windows_relative_regular_file_descriptor(
            authenticated.windows_parent_handle.value, authenticated.leaf_name
        )
    except FileNotFoundError as exc:
        raise PolicySyncError(f"{path}: staged file was removed while authenticating") from exc
    except OSError as exc:
        raise PolicySyncError(f"{path}: cannot inspect staged file: {exc.strerror or exc}") from exc
    try:
        path_stat = os.fstat(second_fd)
    finally:
        os.close(second_fd)
    if (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        raise PolicySyncError(f"{path}: staged file identity changed while authenticating")


def staged_file_identity(
    path: Path,
    root: Path | None = None,
    relative_path: Path | None = None,
) -> StagedFileIdentity:
    if root is None or relative_path is None:
        root, relative_path = _relative_from_path(path)
    with authenticated_path(root, relative_path, path, "staged") as authenticated:
        fd = _open_authenticated_staged_leaf(authenticated, path)
        try:
            file_stat = os.fstat(fd)
            digest = hashlib.sha256()
            with os.fdopen(fd, "rb") as staged_file:
                for chunk in iter(lambda: staged_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            _verify_authenticated_staged_leaf_identity_unchanged(authenticated, path, file_stat)
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
        try:
            os.unlink(authenticated.leaf_name, dir_fd=authenticated.parent_fd)
            return
        except NotImplementedError:
            pass
        platform_io.delete_windows_relative_child(
            authenticated.windows_parent_handle.value, authenticated.leaf_name
        )


def authenticated_replace(
    src_path: Path,
    src_relative_path: Path,
    dst_path: Path,
    dst_relative_path: Path,
    root: Path,
) -> None:
    with authenticated_path(root, src_relative_path, src_path, "staged") as src:
        with authenticated_path(root, dst_relative_path, dst_path, "host") as dst:
            # Portable dir_fd-relative replace tried first (the real
            # mechanism on POSIX); Windows' os.replace() does not even accept
            # src_dir_fd/dst_dir_fd (TypeError, raised before any filesystem
            # access), so this falls back to platform_io's HANDLE-to-HANDLE
            # rename, which reuses the exact parent HANDLEs already
            # authenticated above instead of re-resolving either pathname.
            try:
                os.replace(
                    src.leaf_name,
                    dst.leaf_name,
                    src_dir_fd=src.parent_fd,
                    dst_dir_fd=dst.parent_fd,
                )
                return
            except (NotImplementedError, TypeError):
                pass
            platform_io.rename_windows_relative_child(
                src.windows_parent_handle.value,
                src.leaf_name,
                dst.windows_parent_handle.value,
                dst.leaf_name,
                replace_if_exists=True,
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


def _check_windows_rollback_target_missing(
    parent_handle_value: int, leaf_name: str, relative_path: Path
) -> None:
    """Windows counterpart of the ``os.stat(dir_fd=...)`` missing-target probe.

    ``open_windows_relative_child_disposition`` opens either a file or a
    directory generically and refuses a reparse point, so one call answers
    both "does anything already exist here" and "is it a symlink" the same way
    the POSIX ``lstat`` did -- without ever resolving a full pathname a second
    time.
    """

    try:
        authority = platform_io.open_windows_relative_child_disposition(parent_handle_value, leaf_name)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PolicySyncError(
                f"{relative_path}: rollback conflict; destination symlink appeared after sync failure"
            ) from exc
        raise PolicySyncError(
            f"{relative_path}: cannot inspect rollback target: {exc.strerror or exc}"
        ) from exc
    authority.close()
    raise PolicySyncError(
        f"{relative_path}: rollback conflict; destination appeared after sync failure"
    )


def restore_missing_host_from_backup(staged: StagedUpdate) -> None:
    with authenticated_path(staged.update.root, staged.update.relative_path, staged.update.relative_path) as host:
        try:
            current = os.stat(host.leaf_name, dir_fd=host.parent_fd, follow_symlinks=False)
        except NotImplementedError:
            _check_windows_rollback_target_missing(
                host.windows_parent_handle.value, host.leaf_name, staged.update.relative_path
            )
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
                try:
                    os.link(
                        backup.leaf_name,
                        host.leaf_name,
                        src_dir_fd=backup.parent_fd,
                        dst_dir_fd=host.parent_fd,
                    )
                except NotImplementedError:
                    platform_io.link_windows_relative_child(
                        backup.windows_parent_handle.value,
                        backup.leaf_name,
                        host.windows_parent_handle.value,
                        host.leaf_name,
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
