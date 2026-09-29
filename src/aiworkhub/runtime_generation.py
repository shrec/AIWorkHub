"""Which installed runtime generation this server runs from, and is it current.

NF-2026-01124: an installed server runs from
``<globalStorage>/runtime/generations/<generation>/runtime/aiworkhub/*.py``
and the extension points ``<globalStorage>/runtime/current.json`` at the
newest generation. A long-lived process started before an upgrade keeps
running the old code, so anything it owns exclusively (the reconciler lock)
must be able to notice that a newer generation is installed and yield.

Every answer fails closed to "not superseded": a dev checkout has no
generation, and an unreadable or invalid ``current.json`` names none.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

CURRENT_SCHEMA_ID = "aiworkhub.stable_runtime.v1"
MAX_CURRENT_BYTES = 64 * 1024
_GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _generation_root(own_path: Path | str | None) -> tuple[str, Path] | None:
    """(generation, <globalStorage>/runtime) for an installed module path."""

    path = Path(own_path if own_path is not None else __file__).resolve()
    parts = path.parts
    # .../runtime/generations/<generation>/runtime/aiworkhub/<module>.py
    if len(parts) < 7:
        return None
    if (
        parts[-2] != "aiworkhub"
        or parts[-3] != "runtime"
        or parts[-5] != "generations"
        or parts[-6] != "runtime"
    ):
        return None
    generation = parts[-4]
    if not _GENERATION_RE.match(generation):
        return None
    return generation, path.parents[4]


def own_generation(own_path: Path | str | None = None) -> str | None:
    """The generation this module was loaded from; None for a dev checkout."""

    root = _generation_root(own_path)
    return root[0] if root else None


def current_generation(own_path: Path | str | None = None) -> str | None:
    """The generation ``runtime/current.json`` names; None on any doubt."""

    root = _generation_root(own_path)
    if root is None:
        return None
    target = root[1] / "current.json"
    try:
        metadata = os.stat(target, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CURRENT_BYTES:
            return None
        with open(target, "rb") as stream:
            raw = stream.read(MAX_CURRENT_BYTES + 1)
        if len(raw) > MAX_CURRENT_BYTES:
            return None
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema_id") != CURRENT_SCHEMA_ID:
        return None
    generation = record.get("generation")
    if not isinstance(generation, str) or not _GENERATION_RE.match(generation):
        return None
    return generation


def generation_pair(own_path: Path | str | None = None) -> tuple[str | None, str | None]:
    """(own, current) generations; either may be None."""

    own = own_generation(own_path)
    return own, (current_generation(own_path) if own is not None else None)


def superseded(own_path: Path | str | None = None) -> bool:
    """True only when both generations are known and differ."""

    own, current = generation_pair(own_path)
    return own is not None and current is not None and own != current
