"""Create-time task scope contract (NF-2026-02090, field reports NF-31/NF-19/NF-40).

Two refusals that launch used to discover one turn too late now happen when the
card is created (``core.create_task``, the allowed_writes amendment path and the
NeedFix preview/convert reason builder all call this module):

* A ``read_first``/``allowed_writes``/``required_outputs`` path inside a nested
  git repository is refused as ``task_path_in_nested_repository:<field>:<path>``.
  The boundary is decided by :func:`project_context._git_boundary_for_scope`, the
  same helper launch uses in ``resolve_task_repository_root``.
* ``forbidden`` and ``allowed_writes``/``required_outputs`` share ONE anchored
  grammar: a bare path names exactly that repo-root-relative path, and a glob
  segment never crosses ``/`` -- only an explicit ``**`` segment does. Text
  fields (objective, validation) are matched on whole path tokens, never on
  substrings, so forbidden ``CMakeLists.txt`` does not collide with a mention
  of ``third_party/CMakeLists.txt``.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

NESTED_REPOSITORY_FIELDS = ("read_first", "allowed_writes", "required_outputs")
PATH_CONFLICT_LIMIT = 32
_WILDCARDS = "*?["
# Characters that never belong to a repository path token in prose; ``:`` cuts
# a ``path:line`` reference, quotes/brackets cut markdown and code spans.
_TEXT_TOKEN_SPLIT_RE = re.compile(r"[\s`'\"(){}<>,;:|=]+")
_LEADING_DOT_SLASH_RE = re.compile(r"^(?:\./)+")


def _has_glob(value: str) -> bool:
    return any(ch in value for ch in _WILDCARDS)


def _literal_ends(segment: str) -> tuple[str, str]:
    """Literal text before the first and after the last wildcard of one segment."""
    first = min(segment.find(ch) for ch in _WILDCARDS if ch in segment)
    last = max((segment.rfind(ch) for ch in "*?]" if ch in segment), default=first)
    return segment[:first], segment[last + 1 :]


def _segments_may_overlap(left: str, right: str) -> bool:
    if left == right or fnmatch.fnmatchcase(left, right) or fnmatch.fnmatchcase(right, left):
        return True
    if not (_has_glob(left) and _has_glob(right)):
        return False
    left_head, left_tail = _literal_ends(left)
    right_head, right_tail = _literal_ends(right)
    return (left_head.startswith(right_head) or right_head.startswith(left_head)) and (
        left_tail.endswith(right_tail) or right_tail.endswith(left_tail)
    )


def anchored_paths_overlap(left: str, right: str) -> bool:
    """Whether two card path entries can name one repository path.

    A concrete side defers to ``core._paths_overlap`` (anchored via
    ``worker_workspace._matches``). Two ``**``-free globs overlap only segment
    by segment at the same depth, because a lone ``*`` never crosses ``/``. A
    ``**`` glob pair stays on the conservative ``core._paths_overlap`` answer.
    """
    from . import core

    if not (_has_glob(left) and _has_glob(right)):
        return core._paths_overlap(left, right, glob_aware=True)
    left_parts = left.rstrip("/").split("/")
    right_parts = right.rstrip("/").split("/")
    if "**" in left_parts or "**" in right_parts:
        return core._paths_overlap(left, right, glob_aware=True)
    return len(left_parts) == len(right_parts) and all(
        _segments_may_overlap(a, b) for a, b in zip(left_parts, right_parts, strict=True)
    )


def text_path_tokens(text: str) -> list[str]:
    """Whole path-like tokens of one prose field, URLs removed, in first-seen order."""
    from . import core

    stripped = core._OBJECTIVE_URL_RE.sub(" ", text.replace("\\", "/"))
    tokens: list[str] = []
    for raw in _TEXT_TOKEN_SPLIT_RE.split(stripped):
        token = _LEADING_DOT_SLASH_RE.sub("", raw.rstrip(".,!?"))
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _token_names(token: str, denied: str) -> bool:
    from . import worker_workspace

    if token == denied:
        return True
    if not _has_glob(denied):
        # A forbidden directory still covers the paths below it, by component.
        return token.startswith(denied.rstrip("/") + "/")
    try:
        return worker_workspace._matches(token, [denied])
    except worker_workspace.WorkspaceError:
        return False  # not a repository-relative path, so it names no repo path


def conflict_label(row: Mapping[str, str]) -> str:
    """``forbidden=<path>~<field>=<path>``: both colliding fields and both paths."""
    return f"forbidden={row['forbidden']}~{row['field']}={row['path']}"


def task_card_path_conflicts(card: Mapping[str, Any]) -> list[dict[str, str]]:
    """Bounded forbidden contradictions under the shared anchored grammar."""
    from . import core

    forbidden = [p for p in (core._task_contract_path(v) for v in card.get("forbidden") or []) if p]
    if not forbidden:
        return []
    conflicts: list[dict[str, str]] = []

    def add(row: dict[str, str]) -> bool:
        if row not in conflicts:
            conflicts.append(row)
        return len(conflicts) >= PATH_CONFLICT_LIMIT

    for field in ("allowed_writes", "required_outputs"):
        values = card.get(field) or []
        if not isinstance(values, list):
            continue
        for raw in values:
            declared = core._task_contract_path(raw)
            for denied in forbidden if declared else ():
                if anchored_paths_overlap(declared, denied) and add(
                    {"field": field, "path": declared, "forbidden": denied}
                ):
                    return conflicts
    text_fields = [("objective", str(card.get("objective") or ""))]
    text_fields.extend(("validation", str(value)) for value in card.get("validation") or [])
    for field, text in text_fields:
        for token in text_path_tokens(text):
            for denied in forbidden:
                if _token_names(token, denied) and add(
                    {"field": field, "path": token, "forbidden": denied}
                ):
                    return conflicts
    return conflicts


def path_contract_refusal(card: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``contradictory_task_path_contract`` lifecycle error for ``card``, or None."""
    from . import core

    conflicts = task_card_path_conflicts(card)
    if not conflicts:
        return None
    result = core._lifecycle_error("contradictory_task_path_contract", 2)
    result["conflicts"] = conflicts
    result["conflict_detail"] = [conflict_label(row) for row in conflicts]
    return result


def nested_repository_conflict(repo: Path, card: Mapping[str, Any]) -> tuple[str, str, Path] | None:
    """First ``(field, path, nested_root)`` whose scope sits in a nested git repository."""
    from . import core, project_context

    root = Path(repo).resolve()
    for field in NESTED_REPOSITORY_FIELDS:
        values = card.get(field) or []
        if not isinstance(values, list):
            continue
        for raw in values:
            value = raw.strip().replace("\\", "/") if isinstance(raw, str) else ""
            if field == "read_first" and value:
                match = core._READ_FIRST_ENTRY.match(value)
                value = match.group("path") if match else ""
            if not value:
                continue
            boundary = project_context._git_boundary_for_scope(root, value)
            if boundary is not None and boundary.resolve() != root:
                return field, value, boundary.resolve()
    return None


def nested_repository_refusal(repo: Path, card: Mapping[str, Any]) -> dict[str, Any] | None:
    """``task_path_in_nested_repository:<field>:<path>`` lifecycle error, or None."""
    from . import core

    found = nested_repository_conflict(repo, card)
    if found is None:
        return None
    field, path, boundary = found
    result = core._lifecycle_error(f"task_path_in_nested_repository:{field}:{path[:240]}", 2)
    result.update(
        field=field,
        path=path[:240],
        nested_repository=boundary.relative_to(Path(repo).resolve()).as_posix(),
    )
    return result


def creation_scope_refusal(repo: Path, card: Mapping[str, Any]) -> dict[str, Any] | None:
    """The first create-time scope refusal: forbidden contradictions, then nested repos."""
    return path_contract_refusal(card) or nested_repository_refusal(repo, card)


def creation_scope_reason(repo: Path, card: Mapping[str, Any]) -> str | None:
    """The same refusal as one reason string, for NeedFix preview/convert parity."""
    rows = task_card_path_conflicts(card)
    if rows:
        return "contradictory_task_path_contract:" + ";".join(conflict_label(r) for r in rows[:4])
    found = nested_repository_conflict(repo, card)
    if found is not None:
        return f"task_path_in_nested_repository:{found[0]}:{found[1][:240]}"
    return None
