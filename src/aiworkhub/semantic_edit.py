"""Deterministic, bounded source-fragment editing.

The model selects a small line range from Source Graph evidence and emits only
the replacement text.  ``prepare_line_target`` binds both the whole-file and the
range-fragment sha256 of that range.  Verification is split across two layers:
the applier layer (above this module) enforces the whole-file preimage before it
mutates the tree, while this module verifies the range-fragment preimage on each
edit.  ``apply_line_ranges`` is handed ``(current_text, ranges)`` and no
whole-file hash, so it cannot check the whole-file preimage here: it compares the
fragment hash when one is supplied, refuses on a mismatch, and -- when no hash is
supplied -- applies the range but reports in the returned accounting how many
ranges went unverified, so an unchecked write is never mistaken for a checked
one.  This module deliberately makes no token-savings claim: it reports bytes
exposed and bytes emitted so an ablation can measure provider-level effects
separately.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import shutil
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Iterator

from .platform_io import ReparsePointRefused, pinned_paths


MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_FRAGMENT_BYTES = 256 * 1024
MAX_REPLACEMENT_BYTES = 2 * 1024 * 1024
MAX_RANGES_PER_FILE = 256
_BACKSLASH = "\\"
_ESCAPE_LEVEL_CHANGE_DEFAULT: ContextVar[bool] = ContextVar(
    "aiworkhub_semantic_edit_allow_escape_level_change", default=False
)


@contextmanager
def escape_level_change_allowed(allowed: bool) -> Iterator[None]:
    """Default ``allow_escape_level_change`` for ranges that do not set it.

    The applier layer builds the range item itself, so an apply surface that
    received the caller's flag scopes it here instead of widening that layer.
    """
    token = _ESCAPE_LEVEL_CHANGE_DEFAULT.set(allowed)
    try:
        yield
    finally:
        _ESCAPE_LEVEL_CHANGE_DEFAULT.reset(token)


class SemanticEditError(RuntimeError):
    """Fail-closed semantic edit contract violation."""


class SemanticEditAnchorMismatch(SemanticEditError):
    """Content anchors matched neither the requested range nor one unique shift.

    ``str()`` is the stable ``semantic_edit_anchor_mismatch:<path>`` reason;
    ``detail`` holds what the caller needs to name the intended range: the
    requested range's actual first and last line, its numbered surroundings,
    the ranges where both anchors do match and the next call to make.
    """

    def __init__(self, path: str, detail: dict[str, Any]) -> None:
        super().__init__(f"semantic_edit_anchor_mismatch:{path}")
        self.detail = detail


class SemanticEditEdgeDropped(SemanticEditError):
    """``new`` skipped the head or tail of the prepared range it replaces.

    ``new`` replaces the WHOLE prepared range, so an old line above (head) or
    below (tail) the first/last line the model re-emitted is deleted.
    ``str()`` is ``semantic_edit_range_edge_dropped:<index>:<head|tail>``;
    ``detail`` carries the numbered dropped lines and the next call.
    """

    def __init__(
        self, index: int, edge: str, dropped_lines: list[str], dropped_count: int
    ) -> None:
        super().__init__(f"semantic_edit_range_edge_dropped:{index}:{edge}")
        self.detail = {
            "range_index": index,
            "edge": edge,
            "dropped_lines": dropped_lines,
            "dropped_line_count": dropped_count,
            "next_call": (
                "`new` replaces the whole prepared range, so re-emit the"
                " dropped lines in `new`, or prepare only the lines you mean"
                " to delete and apply new=''. To insert, prepare the line you"
                " insert after and emit it first."
            ),
        }


class SemanticEditEscapeLevelChanged(SemanticEditError):
    """A re-emitted line came back with a different backslash level."""

    def __init__(self, index: int, lines: list[dict[str, Any]]) -> None:
        super().__init__(f"semantic_edit_escape_level_changed:{index}")
        self.detail = {
            "range_index": index,
            "lines": lines,
            "next_call": (
                "Re-emit those lines with the original backslashes. For an"
                " intentional regex/escape fix, pass allow_escape_level_change"
                "=true on apply."
            ),
        }


@dataclass(frozen=True, slots=True)
class PreparedLineTarget:
    path: str
    start_line: int
    end_line: int
    current_sha256: str
    fragment_sha256: str
    fragment: str
    file_bytes: int
    fragment_bytes: int
    # The ``(start_line, end_line)`` the caller asked for when its content
    # anchors moved the range; ``None`` when it was bound where it was asked.
    anchor_relocated_from: tuple[int, int] | None = None

    def receipt(
        self, *, target_id: str = "", include_fragment: bool = True,
    ) -> dict[str, Any]:
        """The hash-bound receipt; ``include_fragment=False`` omits the text.

        Everything ``apply`` verifies (``current_sha256``, ``fragment_sha256``,
        the range) is present either way; a hash-only receipt says so with
        ``fragment_omitted`` so the caller never mistakes it for an empty
        fragment.
        """
        payload: dict[str, Any] = {
            "schema_id": "aiworkhub.semantic_edit_target.v1",
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "current_sha256": self.current_sha256,
            "fragment_sha256": self.fragment_sha256,
            "file_bytes": self.file_bytes,
            "fragment_bytes": self.fragment_bytes,
            "whole_file_bytes_not_returned_by_tool": max(
                self.file_bytes - self.fragment_bytes, 0
            ),
            "token_savings_claimed": False,
        }
        if include_fragment:
            payload["fragment"] = self.fragment
        else:
            payload["fragment_omitted"] = True
        if self.anchor_relocated_from is not None:
            payload["anchor_relocated_from"] = {
                "start_line": self.anchor_relocated_from[0],
                "end_line": self.anchor_relocated_from[1],
            }
        if target_id:
            payload["target_id"] = target_id
        return payload


def normalize_relative_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise SemanticEditError("semantic_edit_path_invalid")
    value = raw.strip().replace("\\", "/")
    windows = PureWindowsPath(raw.strip())
    if windows.drive or windows.anchor:
        raise SemanticEditError(f"semantic_edit_path_escape:{value}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or path.parts[0] == ".git"
    ):
        raise SemanticEditError(f"semantic_edit_path_escape:{value}")
    return path.as_posix()


def path_is_allowed(relative: str, patterns: Iterable[str] | None) -> bool:
    # A missing allowlist (``None``) or an empty one authorizes nothing rather
    # than raising ``TypeError`` on ``for ... in None``: callers that carry an
    # optional allowlist can ask the question without first guarding for it.
    if not patterns:
        return False
    return any(
        relative == str(pattern).replace("\\", "/")
        or fnmatch.fnmatchcase(relative, str(pattern).replace("\\", "/"))
        for pattern in patterns
    )


def resolve_existing_file(root: Path, relative: str) -> Path:
    root = root.resolve()
    cursor = root
    for part in PurePosixPath(relative).parts:
        cursor /= part
        # A junction is no symlink and resolve() may not traverse one (NF-2026-01077): refuse it
        # from lstat first.  Path.is_junction is 3.12 API; requires-python is >=3.12: no fallback.
        if cursor.is_symlink() or cursor.is_junction():
            raise SemanticEditError(f"semantic_edit_symlink_forbidden:{relative}")
    try:
        target = (root / relative).resolve(strict=True)
    except OSError as exc:
        raise SemanticEditError(f"semantic_edit_target_missing:{relative}") from exc
    if root not in target.parents or not target.is_file():
        raise SemanticEditError(f"semantic_edit_target_invalid:{relative}")
    return target


@contextmanager
def held_path(
    root: Path,
    relative: str,
    *,
    include_file: bool = True,
    create_directories: bool = False,
) -> Iterator[Path]:
    """Hold ``root``, every directory down to ``relative`` and -- with
    ``include_file`` -- the file itself for the duration of the block
    (:func:`platform_io.pinned_paths`), and yield the file's path.

    Checking a path and then using it is a race whenever someone else can
    write the tree -- on Windows a worker with modify rights can swap a
    directory for a junction between the two.  Held, no component is or can
    become a link, or be renamed or removed, so a path used inside the block
    lands where it was checked.  ``create_directories`` makes each missing
    directory before holding it.  A file must be released before it is
    replaced, so a writer holds the directories only.
    """

    root = root.resolve()
    parts = PurePosixPath(relative).parts
    with ExitStack() as stack:
        directory = root
        try:
            stack.enter_context(pinned_paths([root]))
            for part in parts[:-1]:
                directory = directory / part
                if create_directories:
                    directory.mkdir(exist_ok=True)
                stack.enter_context(pinned_paths([directory]))
            if include_file:
                stack.enter_context(pinned_paths([directory / parts[-1]]))
        except ReparsePointRefused as exc:
            raise SemanticEditError(f"semantic_edit_symlink_forbidden:{relative}") from exc
        except OSError as exc:
            raise SemanticEditError(f"semantic_edit_target_missing:{relative}") from exc
        yield directory / parts[-1]


def read_utf8_file(target: Path, relative: str) -> tuple[bytes, str]:
    data = target.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise SemanticEditError(f"semantic_edit_file_too_large:{relative}")
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SemanticEditError(f"semantic_edit_utf8_required:{relative}") from exc


def coerce_protocol_line(value: object) -> object:
    """Return an int for a decimal line number, otherwise the original value.

    The package's single owner for this coercion: the VS Code LM worker's
    patch-protocol normaliser imports this name rather than restating the body
    (NF-2026-01022), so a bridge and the editor agree on what ``"55"`` means.

    Text-protocol bridges emit Source Graph line numbers as JSON strings
    (``"55"``).  A boolean is an int subclass and must stay a boolean so the
    fail-closed check below still rejects it.  Non-decimal text is left
    unchanged and rejected as ``semantic_edit_line_range_invalid``.
    """

    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return value


def _validated_line_bounds(start_line: object, end_line: object) -> tuple[int, int]:
    """Accept an inclusive 1-line range (``start_line == end_line``).

    ``end_line < start_line`` is invalid.  Equality is one existing line,
    ``lines[start - 1:end]``, not an empty exclusive span.
    """

    start = coerce_protocol_line(start_line)
    end = coerce_protocol_line(end_line)
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 1
        or end < start
    ):
        raise SemanticEditError("semantic_edit_line_range_invalid")
    return start, end


# A line is what ``\n`` ends, the way Source Graph, git and editors number them
# (``source_graph_diff._split_lines`` states the same rule; a test pins the two
# together).  ``str.splitlines`` also breaks on a bare CR, \v, \f, \x1c-\x1e,
# U+0085, U+2028 and U+2029, so a range taken from any of those tools selected
# and replaced the wrong fragment of a file holding one of them (NF-2026-01077).
_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+\Z")


def _split_lines(text: str) -> list[str]:
    """Split ``text`` on ``\\n`` only, keeping line ends.

    ``"".join`` of the result is ``text`` and empty text has no lines.  Every
    line split in this module goes through here.
    """
    return _LINE_RE.findall(text)


def _line_slice(text: str, start_line: object, end_line: object) -> tuple[list[str], str]:
    start_line, end_line = _validated_line_bounds(start_line, end_line)
    lines = _split_lines(text)
    # An existing zero-byte file has one deterministic virtual insertion
    # point.  VS Code LM workers receive such files as trusted placeholders
    # for declared required outputs; treating ``1:1`` as a zero-byte region
    # lets the model emit only the new content while preserving the normal
    # path, full-file-hash, fragment-hash and idempotency gates.  No other
    # out-of-bounds range is relaxed.
    if not lines and start_line == 1 and end_line == 1:
        return lines, ""
    if not lines or end_line > len(lines):
        raise SemanticEditError(
            f"semantic_edit_line_range_out_of_bounds:{start_line}:{end_line}:{len(lines)}"
        )
    return lines, "".join(lines[start_line - 1 : end_line])


# NF-2026-01533: a model derives line numbers by counting un-numbered Source
# Graph text, so a range a few lines off was bound and applied silently (GLM 5.3
# in EntryLink: shifts 0/-2/-1/-1 with the right replacement text).  Content
# anchors -- the text of the first and last line the caller means to replace --
# verify the range, move it by one nearby unique shift, or refuse it.
_ANCHOR_SHIFT_LIMIT = 16
_ANCHOR_CONTEXT_LINES = 3
_ANCHOR_CONTEXT_MAX_ROWS = 60
_ANCHOR_MAX_CANDIDATES = 5
# A refusal shows at most this many characters of any one line, so a minified
# or generated file cannot turn the 60-row window into megabytes of reply.
_ANCHOR_SHOWN_CHARS = 240


def _anchor_text(line: str) -> str:
    """``line`` as anchors compare it: stripped, without a UTF-8 BOM on line 1."""
    return line.lstrip("\ufeff").strip()


def _anchor_shown(line: str) -> str:
    """``line`` as a refusal displays it: no BOM, bounded length."""
    text = _line_content(line).lstrip("\ufeff")
    if len(text) <= _ANCHOR_SHOWN_CHARS:
        return text
    return text[:_ANCHOR_SHOWN_CHARS] + f"...[truncated {len(text)} chars]"


def _validated_anchors(
    relative: str, expected_first_line: object, expected_last_line: object,
    *, single_line: bool,
) -> tuple[str, str] | None:
    """Return both stripped anchors, or ``None`` when neither was given.

    On a multi-line range one anchor alone cannot tell a shifted range from a
    resized one, so it is refused, naming the missing field, rather than
    half-checked.  On a single-line range the first and last line are the same
    line, so one anchor is complete and stands for both (NF-2026-01743).
    """
    if expected_first_line is None and expected_last_line is None:
        return None
    if expected_first_line is None or expected_last_line is None:
        if not single_line:
            missing = "expected_first_line" if expected_first_line is None else "expected_last_line"
            raise SemanticEditError(f"semantic_edit_anchor_incomplete:{relative}:missing={missing}")
        expected_first_line = expected_last_line = (
            expected_last_line if expected_first_line is None else expected_first_line
        )
    if not isinstance(expected_first_line, str) or not isinstance(expected_last_line, str):
        raise SemanticEditError(f"semantic_edit_anchor_invalid:{relative}")
    return _anchor_text(expected_first_line), _anchor_text(expected_last_line)


def _anchored_range(
    relative: str,
    lines: list[str],
    start_line: int,
    end_line: int,
    first: str,
    last: str,
) -> tuple[int, int]:
    """Return the range whose first and last line strip to ``first`` and ``last``.

    The requested range wins when it matches.  Otherwise the same span is tried
    1..``_ANCHOR_SHIFT_LIMIT`` lines away, nearest first (``-d`` then ``+d``),
    and the range moves only when exactly one shift matches and the anchors are
    not both empty: blank lines identify nothing.  Anything else raises
    :class:`SemanticEditAnchorMismatch` and binds nothing.
    """
    lines = lines or [""]  # the empty file's one virtual line (``_line_slice``)
    span = end_line - start_line

    def anchored(start: int) -> bool:
        end = start + span
        return (
            start >= 1
            and end <= len(lines)
            and _anchor_text(lines[start - 1]) == first
            and _anchor_text(lines[end - 1]) == last
        )

    if anchored(start_line):
        return start_line, end_line
    if first or last:
        shifted = [
            start_line + offset
            for distance in range(1, _ANCHOR_SHIFT_LIMIT + 1)
            for offset in (-distance, distance)
            if anchored(start_line + offset)
        ]
        if len(shifted) == 1:
            return shifted[0], shifted[0] + span
    candidates = sorted(
        (start for start in range(1, len(lines) - span + 1) if anchored(start)),
        key=lambda start: (abs(start - start_line), start),
    )[:_ANCHOR_MAX_CANDIDATES]
    first_row = max(1, start_line - _ANCHOR_CONTEXT_LINES)
    last_row = min(
        len(lines),
        end_line + _ANCHOR_CONTEXT_LINES,
        first_row + _ANCHOR_CONTEXT_MAX_ROWS - 1,
    )
    raise SemanticEditAnchorMismatch(relative, {
        "actual_first_line": _anchor_shown(lines[start_line - 1]),
        "actual_last_line": _anchor_shown(lines[end_line - 1]),
        "numbered_context": [
            f"{number}\t{_anchor_shown(lines[number - 1])}"
            for number in range(first_row, last_row + 1)
        ],
        "candidates": [
            {"start_line": start, "end_line": start + span} for start in candidates
        ],
        "next_call": (
            "Choose the intended range from candidates or numbered_context and"
            " call semantic_edit_prepare again with its start_line and end_line"
            " and with expected_first_line and expected_last_line set to the"
            " exact text of its first and last line."
        ),
    })


def prepare_line_target(
    root: Path,
    *,
    path: str,
    start_line: int,
    end_line: int,
    allowed_writes: Iterable[str],
    expected_first_line: str | None = None,
    expected_last_line: str | None = None,
) -> PreparedLineTarget:
    """Bind one line range by its whole-file and fragment sha256.

    Without anchors the range is bound exactly as requested.  With both, it is
    first checked against them and kept, relocated or refused
    (:func:`_anchored_range`); a relocated target records the requested range
    in ``anchor_relocated_from``.
    """
    relative = normalize_relative_path(path)
    if not path_is_allowed(relative, allowed_writes):
        raise SemanticEditError(f"semantic_edit_path_not_allowed:{relative}")
    start_line, end_line = _validated_line_bounds(start_line, end_line)
    anchors = _validated_anchors(
        relative, expected_first_line, expected_last_line,
        single_line=start_line == end_line,
    )
    resolve_existing_file(root, relative)
    with held_path(root, relative):
        target = resolve_existing_file(root, relative)
        data, text = read_utf8_file(target, relative)
    lines, fragment = _line_slice(text, start_line, end_line)
    relocated_from: tuple[int, int] | None = None
    if anchors is not None:
        located = _anchored_range(relative, lines, start_line, end_line, *anchors)
        if located != (start_line, end_line):
            relocated_from = (start_line, end_line)
            start_line, end_line = located
            _lines, fragment = _line_slice(text, start_line, end_line)
    fragment_data = fragment.encode("utf-8")
    if len(fragment_data) > MAX_FRAGMENT_BYTES:
        raise SemanticEditError(f"semantic_edit_fragment_too_large:{relative}")
    return PreparedLineTarget(
        path=relative,
        start_line=start_line,
        end_line=end_line,
        current_sha256=hashlib.sha256(data).hexdigest(),
        fragment_sha256=hashlib.sha256(fragment_data).hexdigest(),
        fragment=fragment,
        file_bytes=len(data),
        fragment_bytes=len(fragment_data),
        anchor_relocated_from=relocated_from,
    )


def _line_terminator(fragment: str) -> str:
    """Return the terminator ``fragment`` ends with: ``"\\r\\n"``, ``"\\n"`` or ``""``.

    Lines are numbered on ``\\n`` only (:func:`_split_lines`), so a line ends with
    LF, or with CRLF whose CR belongs to the terminator.  A bare CR, ``\\f``,
    U+0085, U+2028 and the other characters ``str.splitlines`` would break on are
    line content: they end nothing, so a fragment that ends with one has no
    terminator to restore.
    """
    if fragment.endswith("\r\n"):
        return "\r\n"
    return "\n" if fragment.endswith("\n") else ""


def _trailing_blank_line_count(text: str) -> int:
    """Count consecutive trailing lines that are only a terminator.

    ``_split_lines`` numbers lines on ``\\n`` only, so a blank line is exactly
    ``"\\n"`` or ``"\\r\\n"``.  Whitespace-only lines carry content and end
    nothing on their own, so they are not counted here.
    """
    count = 0
    for line in reversed(_split_lines(text)):
        if line in ("\n", "\r\n"):
            count += 1
        else:
            break
    return count


def _line_content(line: str) -> str:
    """``line`` without the terminator :func:`_line_terminator` finds."""
    return line[: len(line) - len(_line_terminator(line))]


def _escape_level_changes(old: str, new: str) -> list[tuple[int, str]]:
    old_lines = [_line_content(line) for line in _split_lines(old)]
    new_lines = [_line_content(line) for line in _split_lines(new)]
    exact = set(old_lines)
    lost: dict[str, int] = {}
    for i, o in enumerate(old_lines):
        key = o.replace(_BACKSLASH * 2, _BACKSLASH)
        if key not in lost:
            lost[key] = i
    by_text: dict[str, int] = {}
    for i, o in enumerate(old_lines):
        if o not in by_text:
            by_text[o] = i
    changes: list[tuple[int, str]] = []
    for n in new_lines:
        if n in exact:
            continue
        if n in lost:
            changes.append((lost[n], "lost"))
            continue
        collapsed = n.replace(_BACKSLASH * 2, _BACKSLASH)
        if collapsed in by_text:
            changes.append((by_text[collapsed], "gained"))
    return changes


_EDGE_DROP_LISTED_MAX = 50
_EDGE_DROP_LINE_CHARS = 200


def _edge_dropped_indexes(old_c: list[str], new_c: list[str]) -> list[int]:
    """Old indexes the head of ``new`` skipped over, or ``[]``.

    ``new`` skips the head when its first non-blank line is not the first
    non-blank old line but equals a later old line ``k``: the model started
    inside the range, so every old line above ``k`` is deleted.  Only lines
    whose text appears nowhere in ``new`` count as dropped.
    """
    first_new = next((text for text in new_c if text), None)
    first_old = next((i for i, text in enumerate(old_c) if text), None)
    if first_new is None or first_old is None or first_new == old_c[first_old]:
        return []
    k = next(
        (i for i in range(first_old + 1, len(old_c)) if old_c[i] == first_new), None
    )
    if k is None:
        return []
    kept = set(new_c)
    return [i for i in range(k) if old_c[i] and old_c[i] not in kept]


def _raise_if_edge_dropped(index: int, start: int, old: str, new: str) -> None:
    old_raw = [_line_content(line) for line in _split_lines(old)]
    old_c = [line.strip() for line in old_raw]
    new_c = [_line_content(line).strip() for line in _split_lines(new)]
    last = len(old_c) - 1
    for edge in ("head", "tail"):
        if edge == "head":
            dropped = _edge_dropped_indexes(old_c, new_c)
        else:
            dropped = sorted(
                last - i for i in _edge_dropped_indexes(old_c[::-1], new_c[::-1])
            )
        if dropped:
            raise SemanticEditEdgeDropped(
                index,
                edge,
                [
                    f"{start + i}\t{old_raw[i][:_EDGE_DROP_LINE_CHARS]}"
                    for i in dropped[:_EDGE_DROP_LISTED_MAX]
                ],
                len(dropped),
            )


def apply_line_ranges(
    current_text: str,
    ranges: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    if not ranges or len(ranges) > MAX_RANGES_PER_FILE:
        raise SemanticEditError("semantic_edit_ranges_invalid")
    lines = _split_lines(current_text)
    normalized: list[tuple[int, int, str, int, int, bool]] = []
    trailing_blank_lines_dropped: list[dict[str, Any]] = []
    escape_level_changed_lines: list[dict[str, Any]] = []
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            raise SemanticEditError(f"semantic_edit_range_invalid:{index}")
        start, end = _validated_line_bounds(
            item.get("start_line"), item.get("end_line")
        )
        replacement = item.get("new")
        if not isinstance(replacement, str):
            raise SemanticEditError(f"semantic_edit_replacement_invalid:{index}")
        replacement_bytes = len(replacement.encode("utf-8"))
        if replacement_bytes > MAX_REPLACEMENT_BYTES:
            raise SemanticEditError(f"semantic_edit_replacement_too_large:{index}")
        _unused, old = _line_slice(current_text, start, end)
        old_bytes = old.encode("utf-8")
        # The range-fragment preimage is verified only when a hash is supplied,
        # and a mismatch always raises.  A missing hash (``None``, ``""`` or an
        # absent key) is NOT silently treated as verified: the range is applied
        # but recorded as unverified, and the accounting below reports how many
        # ranges skipped the check so a caller can never mistake an unchecked
        # write for a checked one.  (This layer receives ``(current_text,
        # ranges)`` and no whole-file hash, so it cannot verify the whole-file
        # preimage -- that gate lives in the applier layer, above this one.)
        expected_fragment_sha256 = item.get("fragment_sha256")
        if expected_fragment_sha256 in (None, ""):
            preimage_verified = False
        elif (
            not isinstance(expected_fragment_sha256, str)
            or hashlib.sha256(old_bytes).hexdigest() != expected_fragment_sha256
        ):
            raise SemanticEditError(f"semantic_edit_fragment_hash_mismatch:{index}")
        else:
            preimage_verified = True
        preserve = item.get("preserve_trailing_newline", True)
        if not isinstance(preserve, bool):
            raise SemanticEditError(f"semantic_edit_newline_policy_invalid:{index}")
        # Complete the replacement with the terminator ``old`` ended with, exactly
        # when it does not itself end a ``\n``-numbered line: a bare CR ends
        # nothing, so ``B\r`` over an LF line becomes ``B\r\n``.
        if preserve and replacement and not replacement.endswith("\n"):
            replacement += _line_terminator(old)
        old_blank = _trailing_blank_line_count(old)
        new_blank = _trailing_blank_line_count(replacement)
        if new_blank < old_blank:
            trailing_blank_lines_dropped.append(
                {
                    "range_index": index,
                    "start_line": start,
                    "end_line": end,
                    "old_trailing_blank_lines": old_blank,
                    "new_trailing_blank_lines": new_blank,
                }
            )
        _raise_if_edge_dropped(index, start, old, replacement)
        range_escape_changes = _escape_level_changes(old, replacement)
        allow_escape_level_change = item.get(
            "allow_escape_level_change", _ESCAPE_LEVEL_CHANGE_DEFAULT.get()
        )
        if not isinstance(allow_escape_level_change, bool):
            raise SemanticEditError(f"semantic_edit_escape_policy_invalid:{index}")
        if range_escape_changes and not allow_escape_level_change:
            raise SemanticEditEscapeLevelChanged(
                index,
                [
                    {"old_line": start + _offset, "direction": _direction}
                    for _offset, _direction in range_escape_changes
                ],
            )
        for _offset, _direction in range_escape_changes:
            escape_level_changed_lines.append(
                {"range_index": index, "old_line": start + _offset, "direction": _direction}
            )
        normalized.append(
            (
                start,
                end,
                replacement,
                len(old_bytes),
                len(replacement.encode("utf-8")),
                preimage_verified,
            )
        )

    ordered = sorted(normalized, key=lambda value: (value[0], value[1]))
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] <= previous[1]:
            raise SemanticEditError(
                f"semantic_edit_ranges_overlap:{previous[0]}-{previous[1]}:{current[0]}-{current[1]}"
            )

    for start, end, replacement, _old_bytes, _new_bytes, _verified in reversed(ordered):
        lines[start - 1 : end] = [replacement]
    next_text = "".join(lines)
    next_bytes = len(next_text.encode("utf-8"))
    if next_bytes > MAX_FILE_BYTES:
        raise SemanticEditError("semantic_edit_result_too_large")
    old_region_bytes = sum(item[3] for item in normalized)
    replacement_bytes = sum(item[4] for item in normalized)
    unverified_range_count = sum(1 for item in normalized if not item[5])
    return next_text, {
        "range_count": len(normalized),
        "old_region_bytes": old_region_bytes,
        "replacement_bytes": replacement_bytes,
        "model_reemitted_old_bytes": 0,
        "whole_file_output_required": False,
        "token_savings_claimed": False,
        "preimage_verified_range_count": len(normalized) - unverified_range_count,
        "preimage_unverified_range_count": unverified_range_count,
        "preimage_verified": unverified_range_count == 0,
        "trailing_blank_lines_dropped": trailing_blank_lines_dropped,
        "escape_level_changed_lines": escape_level_changed_lines,
    }


def syntax_regression(path: str, before: str, after: str) -> str:
    """Regression-only guard: refuse a replacement that breaks parsing.

    Returns ``""`` when there is no regression, otherwise
    ``"semantic_edit_syntax_regression:<path>:<lineno>:<msg> -- ..."``.
    ``msg`` is bounded to 200 chars and never carries file content.
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        try:
            compile(before, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            return ""
        try:
            compile(after, path, "exec", dont_inherit=True)
        except SyntaxError as exc:
            lineno = exc.lineno or 0
            msg = str(exc.msg or exc)[:200].replace("\n", " ").replace("\r", " ")
            return (
                f"semantic_edit_syntax_regression:{path}:{lineno}:{msg}"
                " -- a replacement must re-emit every line of the prepared"
                " range it keeps; omitted lines are deleted"
            )
        except ValueError as exc:
            msg = str(exc)[:200].replace("\n", " ").replace("\r", " ")
            return (
                f"semantic_edit_syntax_regression:{path}:0:{msg}"
                " -- a replacement must re-emit every line of the prepared"
                " range it keeps; omitted lines are deleted"
            )
        return ""
    if suffix in (".js", ".cjs", ".mjs"):
        node = shutil.which("node")
        if not node:
            return ""
        try:
            with tempfile.TemporaryDirectory(prefix="aiworkhub-syntax-") as tmp:
                before_file = Path(tmp) / f"before{suffix}"
                after_file = Path(tmp) / f"after{suffix}"
                before_file.write_text(before, encoding="utf-8")
                after_file.write_text(after, encoding="utf-8")
                before_proc = subprocess.run(
                    [node, "--check", str(before_file)],
                    capture_output=True,
                    timeout=10,
                    shell=False,
                )
                if before_proc.returncode != 0:
                    return ""
                after_proc = subprocess.run(
                    [node, "--check", str(after_file)],
                    capture_output=True,
                    timeout=10,
                    shell=False,
                )
                if after_proc.returncode == 0:
                    return ""
                stderr = after_proc.stderr
                if isinstance(stderr, bytes):
                    stderr_text = stderr.decode("utf-8", errors="replace")
                else:
                    stderr_text = str(stderr)
                lineno = 0
                marker = str(after_file)
                idx = stderr_text.find(marker)
                haystack = stderr_text[idx + len(marker):] if idx >= 0 else ""
                match = re.search(r":(\d+)", haystack)
                if match:
                    try:
                        lineno = int(match.group(1))
                    except ValueError:
                        lineno = 0
                first_line = ""
                for line in _split_lines(stderr_text):
                    if line.strip():
                        first_line = line.strip()
                        break
                if marker in first_line:
                    first_line = first_line.replace(marker, "").strip()
                    first_line = first_line.lstrip(": ").strip()
                msg = first_line[:200].replace("\r", " ")
                if not msg:
                    msg = "node --check failed"
                return (
                    f"semantic_edit_syntax_regression:{path}:{lineno}:{msg}"
                    " -- a replacement must re-emit every line of the prepared"
                    " range it keeps; omitted lines are deleted"
                )
        except (subprocess.TimeoutExpired, OSError):
            return ""
    return ""
