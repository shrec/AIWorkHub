"""Name the hashes behind a promotion conflict (NF-2026-01960).

A ``promotion_conflict`` refusal used to name only the path, so a manager who
had edited a promoted file in the canonical tree before accept had to find the
mismatch by hand.  Every promotion-conflict raise now appends one fixed tail::

    <code>:<path>[:conflicts=<n>]:expected_sha256=<t>:observed_sha256=<t>:candidate_sha256=<t>

``expected`` is the recorded parent baseline, ``observed`` the current
canonical bytes and ``candidate`` the worker bytes.  A token is a 64-hex sha256
of the file bytes, ``none`` for an absent path, ``symlink-<sha256 of target>``
for a symlink, or ``invalid`` for a value of no known shape -- so an
interpolated value can never forge a later field.  The prefix and path stay
first, so prefix mapping to ``promotion_conflict`` is unchanged.

This module is dependency-free: ``worker_workspace`` imports it lazily inside
``promote`` and ``process_launcher_validation`` owns no second prefix list.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

# The single definition of the error prefixes that map to ``promotion_conflict``.
PROMOTION_CONFLICT_PREFIXES: tuple[str, ...] = (
    "parent_changed",
    "promotion_scope",
    "promotion_merge",
)
# Raised by preflight checks before any canonical byte is written, so an accept
# can still answer with a structured refusal and leave the task in review.
PRE_WRITE_CONFLICT_PREFIXES: tuple[str, ...] = (
    "parent_changed_since_launch:",
    "promotion_merge_",
)
MAX_CONFLICT_ENTRIES = 20

_HEX64 = re.compile(r"[0-9a-f]{64}")
_TOKEN = r"(?:none|invalid|symlink-[0-9a-f]{64}|[0-9a-f]{64})"
_DETAIL_RE = re.compile(
    r"(?P<code>[a-z_]+):(?P<path>.+?)(?::conflicts=-?\d+)?"
    rf":expected_sha256=(?P<expected>{_TOKEN})"
    rf":observed_sha256=(?P<observed>{_TOKEN})"
    rf":candidate_sha256=(?P<candidate>{_TOKEN})"
)


def hash_token(value: str | None) -> str:
    """Render one ``_hash_path`` value (or raw sha256 hex) as a tail token."""
    if value is None:
        return "none"
    text = str(value)
    if text.startswith("file:"):
        digest = text.rsplit(":", 1)[1]
        return digest if _HEX64.fullmatch(digest) else "invalid"
    if text.startswith("symlink:"):
        target = text[len("symlink:"):].encode("utf-8", "surrogateescape")
        return "symlink-" + hashlib.sha256(target).hexdigest()
    return text if _HEX64.fullmatch(text) else "invalid"


def conflict_detail(
    expected: str | None, observed: str | None, candidate: str | None
) -> str:
    """Return the ``:expected_sha256=...:observed_sha256=...:candidate_sha256=...`` tail."""
    return (
        f":expected_sha256={hash_token(expected)}"
        f":observed_sha256={hash_token(observed)}"
        f":candidate_sha256={hash_token(candidate)}"
    )


def conflict_entries(errors: Iterable[str]) -> dict[str, Any]:
    """Bounded ``{path, expected_sha256, observed_sha256, candidate_sha256}`` list.

    Only full-match tails are parsed; anything else contributes no entry, so
    the caller keeps the full error string as the authoritative text.
    """
    entries: list[dict[str, str]] = []
    total = 0
    for error in errors:
        match = _DETAIL_RE.fullmatch(str(error))
        if match is None:
            continue
        total += 1
        if len(entries) < MAX_CONFLICT_ENTRIES:
            entries.append(
                {
                    "path": match["path"],
                    "expected_sha256": match["expected"],
                    "observed_sha256": match["observed"],
                    "candidate_sha256": match["candidate"],
                }
            )
    return {
        "promotion_conflicts": entries,
        "promotion_conflicts_truncated": total - len(entries),
    }


def promotion_conflict_reply(error: str) -> dict[str, Any] | None:
    """Manager-facing accept refusal for a pre-write promotion conflict.

    Returns ``None`` for any other error (including a mid-write
    ``parent_changed_during_promotion``) so the caller re-raises unchanged.
    """
    text = str(error)
    if not text.startswith(PRE_WRITE_CONFLICT_PREFIXES):
        return None
    return {
        "error": text,
        "error_class": "promotion_conflict",
        **conflict_entries([text]),
    }
