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
from pathlib import Path
from typing import Any, Iterable

from . import semantic_edit


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
    """

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
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{file_path.name}.aiworkhub-", dir=file_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="", closefd=False) as handle:
            handle.write(next_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(fd)
        fd = -1
        if original_mode != 0o600:
            try:
                os.chmod(temp_name, original_mode)
            except OSError:
                pass
        os.replace(temp_name, file_path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return next_text, metrics
