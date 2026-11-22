"""The applier layer: verify the whole-file preimage, then mutate the tree.

``semantic_edit`` deliberately stops short of writing. Its docstring names the
split: that module verifies the range-fragment preimage, and "the applier layer
(above this module) enforces the whole-file preimage before it mutates the
tree". That layer existed only inside the worker MCP session, so it was
available to a worker and to nobody else.

The manager has no semantic-edit tool at all, which is why manager-side
corrections were being made by whole-string rewrites with no hash binding: the
one instrument that makes a small edit verifiable was locked to one caller.
Here it is one function with one definition, so the worker session and a
manager both get the same guarantees rather than the manager getting none.
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from . import semantic_edit
from .platform_io import chmod_fd, posix_path_modes_supported


def replace_prepared_range(
    root: Path,
    target: semantic_edit.PreparedLineTarget,
    new: str,
    *,
    allowed_writes: Iterable[str],
) -> tuple[str, dict[str, Any]]:
    """Re-verify a prepared target and durably replace exactly its lines.

    The file and the fragment are re-hashed here rather than trusted from
    prepare time, so a file that moved underneath the edit is refused instead
    of silently overwritten.

    The replacement is atomic and never changes the destination's mode
    (NF-2026-01970, NF-2026-02365). The temp is created with the original
    mode, so the normal path needs no chmod syscall, and the temp is
    installed by ``os.replace`` only when its permission bits match the
    original's. When they differ -- a umask, a sandbox that forbids
    ``fchmod`` -- the new bytes are written in place into the original
    inode, which keeps its mode and owner. When neither path can keep the
    mode the edit is refused with ``semantic_edit_mode_not_preserved`` and
    the original bytes are left untouched; a mode is never changed silently.

    Raises ``SemanticEditError`` or ``OSError``; the caller decides how a
    refusal is reported.

    The whole read-verify-write runs with ``root`` and every directory down
    to the file held (:func:`semantic_edit.held_path`), and the file itself
    held while it is read: a writer of the tree cannot swap any of them for
    a junction or link after the checks, so the write lands where the
    checks looked.  The file is released only for the replace, which swaps
    the directory entry and never follows a link.
    """

    relative = semantic_edit.normalize_relative_path(target.path)
    if not semantic_edit.path_is_allowed(relative, allowed_writes):
        raise semantic_edit.SemanticEditError(f"semantic_edit_path_not_allowed:{relative}")
    with semantic_edit.held_path(root, relative, include_file=False):
        with semantic_edit.held_path(root, relative):
            file_path = semantic_edit.resolve_existing_file(root, target.path)
            original_mode = os.stat(file_path).st_mode & 0o7777
            data, current_text = semantic_edit.read_utf8_file(file_path, target.path)
            current_sha256 = hashlib.sha256(data).hexdigest()
            start_line, end_line = target.start_line, target.end_line
            if current_sha256 != target.current_sha256:
                # NF-2026-01976: an earlier apply in this file may only have
                # shifted the prepared bytes; find them or refuse stale.
                start_line, end_line = semantic_edit.relocated_range(current_text, target)
            current = semantic_edit.prepare_line_target(
                root,
                path=target.path,
                start_line=start_line,
                end_line=end_line,
                allowed_writes=allowed_writes,
            )
            if current.current_sha256 != current_sha256:
                raise semantic_edit.SemanticEditError(
                    f"semantic_edit_stale_file:{target.path}"
                )
            if current.fragment_sha256 != target.fragment_sha256:
                raise semantic_edit.SemanticEditError(
                    f"semantic_edit_stale_fragment:{target.path}"
                )

        next_text, metrics = semantic_edit.apply_line_ranges(
            current_text,
            [{
                "start_line": start_line,
                "end_line": end_line,
                "new": new,
                "fragment_sha256": target.fragment_sha256,
            }],
        )
        if (start_line, end_line) != (target.start_line, target.end_line):
            metrics["relocated_from"] = [target.start_line, target.end_line]
            metrics["relocated_to"] = [start_line, end_line]
        regression = semantic_edit.syntax_regression(relative, current_text, next_text)
        if regression:
            raise semantic_edit.SemanticEditError(regression)
        payload = next_text.encode("utf-8")
        # NF-2026-02365: the temp is created with the original mode, so no
        # chmod syscall is needed on the normal path; a chmod a sandbox denies
        # can no longer leave a 0600 file behind under a successful apply.
        temp_path, fd = _create_temp_with_mode(file_path, original_mode)
        try:
            try:
                with os.fdopen(fd, "wb", closefd=False) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if not _fd_mode_matches(fd, original_mode):
                    # The umask narrowed the creation mode; one descriptor
                    # chmod may still fix it where the host allows it.
                    try:
                        chmod_fd(fd, original_mode)
                    except OSError:
                        pass
                replace_keeps_mode = _fd_mode_matches(fd, original_mode)
            finally:
                os.close(fd)
            metrics["original_mode"] = original_mode
            if replace_keeps_mode:
                os.replace(temp_path, file_path)
                metrics["mode_write_path"] = "replace"
            else:
                # Installing the temp would change the mode: rewrite the
                # original inode instead, which keeps its mode and owner.
                try:
                    _write_in_place(file_path, payload, data, original_mode)
                except OSError as exc:
                    raise semantic_edit.SemanticEditError(
                        f"semantic_edit_mode_not_preserved:{relative}"
                    ) from exc
                metrics["mode_write_path"] = "in_place"
            metrics["mode_restored"] = True
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
    return next_text, metrics


_TEMP_NAME_ATTEMPTS = 16


def _comparable_mode(mode: int) -> int:
    """The permission bits this host can represent and therefore must keep.

    POSIX keeps all of ``0o7777``. Windows reports only the read-only
    attribute through ``S_IWRITE``; its execute bits are derived from the
    file extension by ``os.stat`` and never appear on a descriptor, so they
    carry no information an edit could lose.
    """

    if posix_path_modes_supported():
        return mode & 0o7777
    return mode & stat.S_IWRITE


def _fd_mode_matches(fd: int, mode: int) -> bool:
    """Whether the open file ``fd`` carries exactly the permission bits ``mode``."""

    return _comparable_mode(os.fstat(fd).st_mode) == _comparable_mode(mode)


def _create_temp_with_mode(file_path: Path, mode: int) -> tuple[Path, int]:
    """Create a uniquely named sibling of ``file_path`` with ``mode`` set at creation.

    ``os.open`` with ``O_EXCL`` (never following a link) replaces ``mkstemp``,
    whose fixed 0600 needed a chmod afterwards. The name is random and retried
    a bounded number of times; exhaustion is refused with a named reason.
    """

    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    for _attempt in range(_TEMP_NAME_ATTEMPTS):
        candidate = file_path.parent / f".{file_path.name}.aiworkhub-{secrets.token_hex(8)}"
        try:
            return candidate, os.open(candidate, flags, mode & 0o7777)
        except FileExistsError:
            continue
    raise semantic_edit.SemanticEditError(
        f"semantic_edit_temp_name_exhausted:{file_path.name}:{_TEMP_NAME_ATTEMPTS}"
    )


def _write_in_place(file_path: Path, payload: bytes, original: bytes, mode: int) -> None:
    """Rewrite ``file_path``'s own inode with ``payload``, keeping mode and owner.

    Used only when installing a temp would change the mode. Raises ``OSError``
    when the file cannot be opened, does not carry ``mode``, or cannot be
    written; a failed write is rolled back to ``original`` before re-raising,
    so a refusal never leaves half-written bytes.
    """

    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(file_path, flags)
    try:
        if not _fd_mode_matches(fd, mode):
            raise PermissionError(
                errno.EPERM, "semantic_edit_mode_not_preserved", str(file_path)
            )
        with os.fdopen(fd, "r+b", closefd=False) as handle:
            try:
                handle.write(payload)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                try:
                    handle.seek(0)
                    handle.write(original)
                    handle.truncate()
                    handle.flush()
                    os.fsync(handle.fileno())
                except OSError:
                    pass
                raise
    finally:
        os.close(fd)


def create_target(root: Path, path: str, *, allowed_writes: Iterable[str]) -> str:
    """Check that ``path`` may be created and return it normalized.

    A create gets the containment an edit gets -- a repo-relative path with no
    ``..`` or drive (:func:`semantic_edit.normalize_relative_path`), covered by
    ``allowed_writes``, under no symlink or junction -- plus the one check an
    edit never needs: nothing exists at the path yet.  Creates nothing; raises
    ``SemanticEditError`` with a stable reason.
    """

    relative = semantic_edit.normalize_relative_path(path)
    if not semantic_edit.path_is_allowed(relative, allowed_writes):
        raise semantic_edit.SemanticEditError(f"semantic_edit_path_not_allowed:{relative}")
    root = root.resolve()
    cursor = root
    for part in PurePosixPath(relative).parts:
        cursor /= part
        # The same lstat-first refusal resolve_existing_file applies (NF-2026-01077).
        if cursor.is_symlink() or cursor.is_junction():
            raise semantic_edit.SemanticEditError(f"semantic_edit_symlink_forbidden:{relative}")
    if os.path.lexists(cursor):
        raise semantic_edit.SemanticEditError(f"semantic_edit_create_target_exists:{relative}")
    parent = cursor.parent
    while parent != root and not os.path.lexists(parent):
        parent = parent.parent
    if not parent.is_dir():
        raise semantic_edit.SemanticEditError(f"semantic_edit_target_invalid:{relative}")
    if root not in cursor.resolve(strict=False).parents:
        raise semantic_edit.SemanticEditError(f"semantic_edit_path_escape:{relative}")
    return relative


def create_new_file(
    root: Path,
    path: str,
    content: str,
    *,
    allowed_writes: Iterable[str],
) -> tuple[str, dict[str, Any]]:
    """Create ``path`` holding exactly ``content`` and never overwrite anything.

    :func:`create_target` runs first, then the directories down to the file
    are held (:func:`semantic_edit.held_path`, missing ones made) while the
    file is opened ``O_EXCL``: a path that appeared after the check is refused
    with the same ``semantic_edit_create_target_exists`` reason instead of
    being replaced.  The bytes written are ``content`` encoded as UTF-8, with
    no newline translation.  Returns ``(content, metrics)`` shaped like
    :func:`replace_prepared_range`'s, with ``created`` set.
    """

    relative = create_target(root, path, allowed_writes=allowed_writes)
    if not isinstance(content, str):
        raise semantic_edit.SemanticEditError("semantic_edit_replacement_invalid:0")
    data = content.encode("utf-8")
    if len(data) > semantic_edit.MAX_FILE_BYTES:
        raise semantic_edit.SemanticEditError("semantic_edit_result_too_large")
    regression = semantic_edit.syntax_regression(relative, "", content)
    if regression:
        raise semantic_edit.SemanticEditError(regression)
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    with semantic_edit.held_path(
        root, relative, include_file=False, create_directories=True
    ) as file_path:
        try:
            fd = os.open(file_path, flags, 0o666)
        except FileExistsError as exc:
            raise semantic_edit.SemanticEditError(
                f"semantic_edit_create_target_exists:{relative}"
            ) from exc
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            # A file this call created and could not fill is not left behind.
            try:
                os.unlink(file_path)
            except OSError:
                pass
            raise
    return content, {
        "created": True,
        "range_count": 1,
        "old_region_bytes": 0,
        "replacement_bytes": len(data),
        "model_reemitted_old_bytes": 0,
        "file_bytes": len(data),
        "whole_file_output_required": False,
        "token_savings_claimed": False,
        "preimage_verified": True,
    }
