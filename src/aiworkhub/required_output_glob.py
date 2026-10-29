from __future__ import annotations

from collections.abc import Collection, Iterable

GLOB_SIBLING_UNCHANGED_REASON = "glob_sibling_unchanged"


def split_glob_unchanged(
    unchanged: Iterable[str], changed: Collection[str]
) -> tuple[list[str], list[str]]:
    """Return ``(failing, allowed)`` for the unchanged matches of one glob pattern.

    A glob pattern is satisfied when at least one of its matches changed; the
    remaining unchanged matches are then allowed siblings. When no match
    changed, every unchanged match fails as an exact path would.
    """
    paths = list(unchanged)
    if changed:
        return [], paths
    return paths, []
