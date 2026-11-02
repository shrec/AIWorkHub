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

import os
import tempfile
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

    The replacement is atomic, and the destination's real mode is captured
    before the swap and restored after: ``mkstemp`` creates 0600 and
    ``os.replace`` carries that onto the destination, which would otherwise
    rewrite an executable 0755 script down to 0600 on every apply. Restoring
    the mode is best effort, because some sandboxed filesystems forbid chmod
    and the content edit must still land atomically there.

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
            current = semantic_edit.prepare_line_target(
                root,
                path=target.path,
                start_line=target.start_line,
                end_line=target.end_line,
                allowed_writes=allowed_writes,
            )
            if current.current_sha256 != target.current_sha256:
                raise semantic_edit.SemanticEditError(
                    f"semantic_edit_stale_file:{target.path}"
                )
            if current.fragment_sha256 != target.fragment_sha256:
                raise semantic_edit.SemanticEditError(
                    f"semantic_edit_stale_fragment:{target.path}"
                )

            file_path = semantic_edit.resolve_existing_file(root, target.path)
            original_mode = os.stat(file_path).st_mode & 0o7777
            _data, current_text = semantic_edit.read_utf8_file(file_path, target.path)
        next_text, metrics = semantic_edit.apply_line_ranges(
            current_text,
            [{
                "start_line": target.start_line,
                "end_line": target.end_line,
                "new": new,
                "fragment_sha256": target.fragment_sha256,
            }],
        )
        regression = semantic_edit.syntax_regression(relative, current_text, next_text)
        if regression:
            raise semantic_edit.SemanticEditError(regression)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{file_path.name}.aiworkhub-", dir=file_path.parent
        )
        mode_restored = False
        try:
            try:
                chmod_fd(fd, original_mode)
                mode_restored = posix_path_modes_supported()
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8", newline="", closefd=False) as handle:
                handle.write(next_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.close(fd)
            fd = -1
            if original_mode == 0o600:
                mode_restored = True
            else:
                try:
                    os.chmod(temp_name, original_mode)
                    mode_restored = True
                except OSError:
                    pass
            metrics["mode_restored"] = mode_restored
            metrics["original_mode"] = original_mode
            os.replace(temp_name, file_path)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    return next_text, metrics


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
