"""Scope that shares import-closure path memos across the rows of one preflight."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from typing import Any

# NF-2026-00630: the filesystem verdicts of one import-closure run (init-file
# states, candidate verdicts, lstat probes, resolved paths, first symlinks) are
# a function of the tree alone, so the rows of one preflight may share them.
# The value is ``None`` outside a scope, else a dict keyed by resolved repo
# string.  A ContextVar is empty in another thread, which then falls back to
# per-call dicts.
CLOSURE_PROBE_SCOPE: contextvars.ContextVar[dict[str, tuple[Any, ...]] | None] = (
    contextvars.ContextVar("aiworkhub_closure_probe_scope", default=None)
)


@contextlib.contextmanager
def shared_closure_probes() -> Iterator[None]:
    """Share closure path memos across the calls made inside one scope.

    Nothing outlives the ``with`` block: the dict is dropped on exit, so a tree
    changed afterwards is seen afresh by the next scope and by unscoped calls.
    """

    token = CLOSURE_PROBE_SCOPE.set({})
    try:
        yield
    finally:
        CLOSURE_PROBE_SCOPE.reset(token)
