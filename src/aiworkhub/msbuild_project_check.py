"""Structural check for changed MSBuild project files (NF-2026-02000).

A worker edit to a ``.vcxproj`` dropped a ``ResourceCompile`` item and
duplicated ``ClInclude`` entries, and the builtin gate passed it. For one
changed project path this module reports a failure when:

(a) the candidate is not well-formed XML (stdlib ``xml.etree``);
(b) an ``ItemGroup`` item ``(type, Include)`` present in the committed base
    (``git show HEAD:<relpath>``, the base the builtin checks already use) is
    missing from the candidate; a path absent from HEAD has no base;
(c) the same ``(type, Include)`` pair appears more than once in the candidate.

Include values compare as MSBuild does: case-insensitively, with ``/`` and
``\\`` treated alike. Stdlib only; no toolchain; one file per call.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

PROJECT_SUFFIXES: tuple[str, ...] = (
    ".vcxproj",
    ".vcxproj.filters",
    ".csproj",
    ".vbproj",
    ".fsproj",
    ".props",
    ".targets",
)
MAX_PROJECT_BYTES = 16 * 1024 * 1024
MAX_REPORTED_ITEMS = 20


ItemKey = tuple[str, str]
ConditionContext = tuple[tuple[str, str], ...]
ProjectItem = tuple[str, str, str, ConditionContext]


class _BaseUnavailable(Exception):
    """The committed base could not be read; the check fails closed."""


def is_project_path(relative: str) -> bool:
    """True for a path whose name ends in an MSBuild project suffix."""
    name = str(relative).replace("\\", "/").rsplit("/", 1)[-1].lower()
    return any(name.endswith(suffix) for suffix in PROJECT_SUFFIXES)


def normalize_include(value: str) -> str:
    """MSBuild item identity: case-insensitive, ``/`` equals ``\\``."""
    return value.strip().replace("/", "\\").casefold()


def _local(tag: object) -> str:
    text = tag if isinstance(tag, str) else ""
    return text.rsplit("}", 1)[-1]


def _parse(raw: bytes) -> ET.Element:
    if len(raw) > MAX_PROJECT_BYTES:
        raise ValueError(f"project file exceeds {MAX_PROJECT_BYTES} bytes")
    if b"<!ENTITY" in raw or b"<\x00!\x00E\x00N\x00T\x00I\x00T\x00Y" in raw:
        raise ValueError("entity declarations are not allowed in project files")
    return ET.fromstring(raw)


def normalize_condition(value: str | None) -> str:
    """Condition identity: stripped, casefolded, whitespace collapsed; missing is ``""``."""
    return " ".join((value or "").split()).casefold()


def project_items(root_element: ET.Element) -> list[ProjectItem]:
    """Every ``(item type, normalized Include, raw Include, condition context)``.

    The condition context is the ``(local tag, normalized Condition)`` of every
    ancestor ``When``/``Otherwise``, then of the ``ItemGroup``, then of the item
    itself. A semicolon list in one Include attribute contributes one item per
    entry, as MSBuild expands it. ElementTree has no parent pointers, so the
    walk carries the context down; an explicit stack keeps deep nesting from
    exhausting the interpreter recursion limit. Items keep document order.
    """
    items: list[ProjectItem] = []
    stack: list[tuple[ET.Element, ConditionContext]] = [(root_element, ())]
    while stack:
        element, context = stack.pop()
        tag = _local(element.tag)
        if tag == "ItemGroup":
            group_context = context + ((tag, normalize_condition(element.get("Condition"))),)
            for child in element:
                include = child.get("Include")
                if include is None:
                    continue
                item_type = _local(child.tag)
                item_context = group_context + (
                    (item_type, normalize_condition(child.get("Condition"))),
                )
                for part in include.split(";"):
                    if part.strip():
                        items.append(
                            (item_type, normalize_include(part), part.strip(), item_context)
                        )
            continue
        if tag in ("When", "Otherwise"):
            context = context + ((tag, normalize_condition(element.get("Condition"))),)
        stack.extend((child, context) for child in reversed(list(element)))
    return items


def _git(root: Path, args: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        timeout=timeout_seconds, shell=False,
    )


def _unborn_head(root: Path, timeout_seconds: int) -> bool:
    """True only when HEAD is a symbolic ref to a branch with no commit yet."""
    symbolic = _git(root, ["symbolic-ref", "-q", "HEAD"], timeout_seconds)
    if symbolic.returncode != 0:
        return False
    ref = symbolic.stdout.decode("utf-8", "replace").strip()
    if not ref:
        return False
    probe = _git(root, ["rev-parse", "--verify", "-q", ref], timeout_seconds)
    return probe.returncode != 0


def read_base(root: Path, relative: str, *, timeout_seconds: int) -> bytes | None:
    """Committed HEAD bytes for ``relative``; ``None`` when the path has no base.

    No base means: the root is not a git checkout, the repository has no
    commit yet (unborn HEAD), or HEAD does not contain the path. Any other git
    failure, including an unresolvable HEAD, raises ``_BaseUnavailable`` so
    the caller fails closed.
    """
    if not (root / ".git").exists():
        return None
    posix = Path(relative).as_posix()
    try:
        head = _git(root, ["rev-parse", "--verify", "-q", "HEAD^{commit}"], timeout_seconds)
        if head.returncode != 0:
            if _unborn_head(root, timeout_seconds):
                return None
            raise _BaseUnavailable(
                head.stderr.decode("utf-8", "replace").strip() or "HEAD unresolvable"
            )
        listed = _git(root, ["ls-tree", "-z", "--name-only", "HEAD", "--", posix], timeout_seconds)
        if listed.returncode != 0:
            raise _BaseUnavailable(listed.stderr.decode("utf-8", "replace").strip() or "git ls-tree failed")
        if not listed.stdout.strip(b"\x00"):
            return None
        shown = _git(root, ["show", f"HEAD:{posix}"], timeout_seconds)
        if shown.returncode != 0:
            raise _BaseUnavailable(shown.stderr.decode("utf-8", "replace").strip() or "git show failed")
        return shown.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _BaseUnavailable(str(exc)) from exc


def _describe(keys: list[ItemKey], display: dict[ItemKey, str]) -> list[str]:
    return [f"{key[0]} Include={display[key]!r}" for key in keys[:MAX_REPORTED_ITEMS]]


def check_project_file(
    root: Path, relative: str, *, timeout_seconds: int = 60
) -> tuple[bool, str, str]:
    """Return ``(passed, summary, error)`` for one changed project file."""
    label = f"msbuild project {relative}"
    try:
        candidate_root = _parse((root / relative).read_bytes())
    except (OSError, ValueError, ET.ParseError) as exc:
        error = f"not well-formed XML: {exc}"
        return False, f"{label}: {error}", error
    candidate = project_items(candidate_root)
    display: dict[ItemKey, str] = {}
    for item_type, key, raw, _context in candidate:
        display.setdefault((item_type, key), raw)
    # Per-configuration items are valid MSBuild: the same Include under a
    # different Condition (item, ItemGroup or When/Otherwise branch) is not a
    # duplicate, so the duplicate key carries the condition context.
    counts = Counter((item_type, key, context) for item_type, key, _raw, context in candidate)
    duplicates = sorted({(k[0], k[1]) for k, n in counts.items() if n > 1})

    problems: list[str] = []
    errors: list[str] = []
    if duplicates:
        problems.append("duplicate items: " + "; ".join(_describe(duplicates, display)))
        errors.append("duplicate_project_item")

    base_note = "base: none (new project file)"
    try:
        base_raw = read_base(root, relative, timeout_seconds=timeout_seconds)
    except _BaseUnavailable as exc:
        error = f"base_unreadable: {exc}"
        return False, f"{label}: {error}", error
    if base_raw is not None:
        try:
            base_items = project_items(_parse(base_raw))
        except (ValueError, ET.ParseError) as exc:
            base_note = f"base: not well-formed ({exc}); missing-item comparison not applicable"
        else:
            base_note = "base: HEAD"
            base_display: dict[ItemKey, str] = {}
            for item_type, key, raw, _context in base_items:
                base_display.setdefault((item_type, key), raw)
            # Keyed by (type, Include) only: an edited Condition is not a loss.
            missing = sorted(set(base_display) - set(display))
            if missing:
                problems.append(
                    "items missing versus base: " + "; ".join(_describe(missing, base_display))
                )
                errors.append("missing_project_item")
    if problems:
        error = ",".join(errors)
        return False, f"{label}: " + " | ".join(problems), error
    return True, f"{label}: well-formed, {len(display)} unique items, {base_note}", ""
