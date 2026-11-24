"""NF-2026-02396: per-finding diff context for the review packet.

Pure, read-only and total. For each reviewer finding that names a path, the
section carries the hunk of the request's retained worktree that holds the
finding's line, or the nearest hunk when the line sits outside every hunk.
It runs one ``git diff`` per distinct path and never writes to the worktree or
its index (no ``git add -N``, no checkout): the worktree is hash-sealed for
review. ``--no-optional-locks`` also stops git from refreshing the index.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
from typing import Any

# Cap on one hunk's UTF-8 bytes, and on the summed size of the section's items.
HUNK_MAX_BYTES = 1500
SECTION_MAX_BYTES = 8000
GIT_TIMEOUT_SECONDS = 20
# An untracked file shows this many lines either side of the finding line.
NEW_FILE_CONTEXT_LINES = 15
PATH_TEXT_CHARS = 200

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def build_section(findings: list[Any], worktree: Path) -> dict[str, Any]:
    """The ``finding_diff_context`` section. Never raises."""
    try:
        return _build_section(findings, Path(worktree))
    except Exception as exc:  # noqa: BLE001 -- advisory evidence must not break the packet
        return {"items": [], "unavailable_reason": f"section_error:{type(exc).__name__}"}


def _build_section(findings: Any, worktree: Path) -> dict[str, Any]:
    if not worktree.is_dir():
        return {"items": [], "unavailable_reason": "worktree_absent"}
    root = worktree.resolve()
    rows = list(findings) if isinstance(findings, list) else []
    distinct = list(dict.fromkeys(row["path"] for row in rows if _has_path(row)))
    loaded: dict[str, dict[str, Any]] = {}
    if distinct:
        workers = max(1, min(len(distinct), (os.cpu_count() or 2) // 2))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda path: _load_path(root, path), distinct))
        loaded = dict(zip(distinct, results))
    items: list[dict[str, Any]] = []
    used = 0
    omitted = 0
    for row in rows:
        item = _row_item(row, root, loaded)
        if item is None:
            continue
        size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if omitted or used + size > SECTION_MAX_BYTES:
            omitted += 1
            continue
        items.append(item)
        used += size
    return {"items": items, "items_omitted": omitted, "truncated": omitted > 0}


def _has_path(row: Any) -> bool:
    return isinstance(row, dict) and isinstance(row.get("path"), str) and bool(row["path"].strip())


def _row_item(row: Any, root: Path, loaded: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return {"finding_id": None, "path": None, "line": None, "unavailable_reason": "invalid_row"}
    if not _has_path(row):
        return None
    path = row["path"]
    line = _as_line(row.get("line"))
    base = {"finding_id": _finding_id(row), "path": path[:PATH_TEXT_CHARS], "line": line}
    got = loaded[path]
    if "reason" in got:
        return {**base, "unavailable_reason": got["reason"]}
    if got.get("new_file"):
        try:
            header, text = _file_window(root / path, line)
        except OSError as exc:
            return {**base, "unavailable_reason": f"file_unreadable:{type(exc).__name__}"}
        text, truncated = _cap_hunk(text)
        return {**base, "kind": "new_file_window", "hunk_header": header,
                "hunk": text, "truncated": truncated}
    hunk = _pick_hunk(got["hunks"], line)
    text, truncated = _cap_hunk(hunk["text"])
    return {**base, "kind": "diff_hunk", "hunk_header": hunk["header"],
            "hunk": text, "truncated": truncated}


def _finding_id(row: dict[str, Any]) -> str | None:
    value = row.get("dispose_finding_id") or row.get("id")
    return None if value in (None, "") else str(value)[:PATH_TEXT_CHARS]


def _as_line(value: Any) -> int | None:
    """A 1-based line number, or None. Missing and non-int values are None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdecimal():
        number = int(value.strip())
    else:
        return None
    return number if number >= 1 else None


def _is_outside(root: Path, path: str) -> bool:
    """True for an absolute path or one that resolves outside ``root``."""
    if Path(path).is_absolute() or PureWindowsPath(path).anchor:
        return True
    return not (root / path).resolve().is_relative_to(root)


def _load_path(root: Path, path: str) -> dict[str, Any]:
    """One git call for one distinct path: hunks, an untracked file, or a named reason. Never raises."""
    try:
        if _is_outside(root, path):
            return {"reason": "path_outside_worktree"}
        if not (root / path).is_file():
            return {"reason": "path_not_in_worktree"}
    except (OSError, ValueError) as exc:
        return {"reason": f"path_invalid:{type(exc).__name__}"}
    argv = [
        "git", "--no-optional-locks", "-C", str(root),
        "diff", "--no-color", "--no-ext-diff", "-U3", "HEAD", "--", path,
    ]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 -- a missing or failing git is a named reason
        return {"reason": f"git_diff_failed:{type(exc).__name__}"}
    if completed.returncode != 0:
        return {"reason": f"git_diff_failed:{completed.returncode}"}
    diff = completed.stdout.decode("utf-8", errors="replace")
    hunks = _parse_hunks(diff)
    if hunks:
        return {"hunks": hunks}
    if diff.strip():
        return {"reason": "diff_without_hunk"}
    # An empty diff for a file that exists in the worktree: untracked (or unchanged).
    return {"new_file": True}


def _parse_hunks(diff: str) -> list[dict[str, Any]]:
    """Each ``@@`` hunk with its new-side range; the body is the lines after the header."""
    parsed: list[tuple[str, int, int, list[str]]] = []
    for line in diff.splitlines():
        match = _HUNK_HEADER_RE.match(line)
        if match:
            count = match.group(2)
            parsed.append((line, int(match.group(1)), int(count) if count is not None else 1, []))
        elif parsed:
            parsed[-1][3].append(line)
    return [
        {"header": header, "start": start, "count": count, "text": "\n".join(body)}
        for header, start, count, body in parsed
    ]


def _distance(hunk: dict[str, Any], line: int) -> int:
    low = hunk["start"]
    high = low + max(hunk["count"], 1) - 1
    if line < low:
        return low - line
    if line > high:
        return line - high
    return 0


def _pick_hunk(hunks: list[dict[str, Any]], line: int | None) -> dict[str, Any]:
    """The hunk containing ``line``, else the nearest; the first hunk when there is no line."""
    if line is None:
        return hunks[0]
    return min(hunks, key=lambda hunk: _distance(hunk, line))


def _file_window(target: Path, line: int | None) -> tuple[str, str]:
    """Numbered lines around ``line`` of an untracked file, or its head when there is no line."""
    if line is None:
        first, last = 1, 2 * NEW_FILE_CONTEXT_LINES + 1
    else:
        first = max(1, line - NEW_FILE_CONTEXT_LINES)
        last = line + NEW_FILE_CONTEXT_LINES
    shown: list[str] = []
    with target.open("r", encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle, start=1):
            if number > last:
                break
            if number >= first:
                text = raw.rstrip("\r\n")
                shown.append(f"{number}: {text}")
    return f"@@ new_file {first},{last} @@", "\n".join(shown)


def _cap_hunk(text: str) -> tuple[str, bool]:
    from .core import _bounded_utf8_prefix

    return _bounded_utf8_prefix(text, HUNK_MAX_BYTES)
