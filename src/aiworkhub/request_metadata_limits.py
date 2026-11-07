"""One size bound for a coordinator-written ``{request_id}.request.json``.

NF-2026-02154: the readers each carried their own hand-written 1-2 MiB cap,
none of them measured, so a ~5k-file repository (request.json ~2.4 MB) broke
rework at crash-retry and would have broken the next reader in turn.  Every
reader of that file now uses :data:`REQUEST_METADATA_MAX_BYTES`, and the
launch writer refuses an oversize payload with a named error before the file
is written, so an oversize request fails at launch instead of at a later
rework, crash-retry or accept.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .worker_workspace import WorkspaceError

# request.json is written only by the coordinator into its own 0700 process
# directory, so this is a memory-safety ceiling for the readers, not a guess at
# what a request needs.  Today a sparse worktree costs about 460 B per file in
# the metadata, so 64 MiB holds roughly 140k files.
REQUEST_METADATA_MAX_BYTES = 64 * 1024 * 1024


def check_request_metadata_bytes(size: int) -> None:
    """Refuse an encoded request.json larger than the shared ceiling."""

    if size > REQUEST_METADATA_MAX_BYTES:
        raise WorkspaceError(
            f"request_metadata_too_large:{size}>{REQUEST_METADATA_MAX_BYTES}"
        )


def check_request_metadata_payload(payload: Mapping[str, Any]) -> None:
    """Measure ``payload`` exactly as ``write_json_0600`` encodes it, then check.

    The encoding is pinned to ``worker_workspace.write_json_0600`` by
    ``tests/test_request_metadata_limits_nf02154.py``.
    """

    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    check_request_metadata_bytes(len(encoded))
