"""Deterministic, bounded source-fragment editing.

The model selects a small line range from Source Graph evidence and emits only
the replacement text.  The trusted local runtime verifies the complete file
preimage, applies non-overlapping ranges, and returns accounting facts.  This
module deliberately makes no token-savings claim: it reports bytes exposed and
bytes emitted so an ablation can measure provider-level effects separately.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_FRAGMENT_BYTES = 256 * 1024
MAX_REPLACEMENT_BYTES = 2 * 1024 * 1024
MAX_RANGES_PER_FILE = 256


class SemanticEditError(RuntimeError):
    """Fail-closed semantic edit contract violation."""


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

    def receipt(self, *, target_id: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_id": "aiworkhub.semantic_edit_target.v1",
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "current_sha256": self.current_sha256,
            "fragment_sha256": self.fragment_sha256,
            "fragment": self.fragment,
            "file_bytes": self.file_bytes,
            "fragment_bytes": self.fragment_bytes,
            "whole_file_bytes_not_returned_by_tool": max(
                self.file_bytes - self.fragment_bytes, 0
            ),
            "token_savings_claimed": False,
        }
        if target_id:
            payload["target_id"] = target_id
        return payload


def normalize_relative_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise SemanticEditError("semantic_edit_path_invalid")
    value = raw.strip().replace("\\", "/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or path.parts[0] == ".git"
    ):
        raise SemanticEditError(f"semantic_edit_path_escape:{value}")
    return path.as_posix()


def path_is_allowed(relative: str, patterns: Iterable[str]) -> bool:
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
        if cursor.is_symlink():
            raise SemanticEditError(f"semantic_edit_symlink_forbidden:{relative}")
    try:
        target = (root / relative).resolve(strict=True)
    except OSError as exc:
        raise SemanticEditError(f"semantic_edit_target_missing:{relative}") from exc
    if root not in target.parents or not target.is_file():
        raise SemanticEditError(f"semantic_edit_target_invalid:{relative}")
    return target


def read_utf8_file(target: Path, relative: str) -> tuple[bytes, str]:
    data = target.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise SemanticEditError(f"semantic_edit_file_too_large:{relative}")
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SemanticEditError(f"semantic_edit_utf8_required:{relative}") from exc


def _line_slice(text: str, start_line: int, end_line: int) -> tuple[list[str], str]:
    if (
        not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or not isinstance(end_line, int)
        or isinstance(end_line, bool)
        or start_line < 1
        or end_line < start_line
    ):
        raise SemanticEditError("semantic_edit_line_range_invalid")
    lines = text.splitlines(keepends=True)
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


def prepare_line_target(
    root: Path,
    *,
    path: str,
    start_line: int,
    end_line: int,
    allowed_writes: Iterable[str],
) -> PreparedLineTarget:
    relative = normalize_relative_path(path)
    if not path_is_allowed(relative, allowed_writes):
        raise SemanticEditError(f"semantic_edit_path_not_allowed:{relative}")
    target = resolve_existing_file(root, relative)
    data, text = read_utf8_file(target, relative)
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
    )


def apply_line_ranges(
    current_text: str,
    ranges: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    if not ranges or len(ranges) > MAX_RANGES_PER_FILE:
        raise SemanticEditError("semantic_edit_ranges_invalid")
    lines = current_text.splitlines(keepends=True)
    normalized: list[tuple[int, int, str, int, int]] = []
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            raise SemanticEditError(f"semantic_edit_range_invalid:{index}")
        start = item.get("start_line")
        end = item.get("end_line")
        replacement = item.get("new")
        if not isinstance(replacement, str):
            raise SemanticEditError(f"semantic_edit_replacement_invalid:{index}")
        replacement_bytes = len(replacement.encode("utf-8"))
        if replacement_bytes > MAX_REPLACEMENT_BYTES:
            raise SemanticEditError(f"semantic_edit_replacement_too_large:{index}")
        _unused, old = _line_slice(current_text, start, end)
        old_bytes = old.encode("utf-8")
        expected_fragment_sha256 = item.get("fragment_sha256")
        if expected_fragment_sha256 not in (None, "") and (
            not isinstance(expected_fragment_sha256, str)
            or hashlib.sha256(old_bytes).hexdigest() != expected_fragment_sha256
        ):
            raise SemanticEditError(f"semantic_edit_fragment_hash_mismatch:{index}")
        preserve = item.get("preserve_trailing_newline", True)
        if not isinstance(preserve, bool):
            raise SemanticEditError(f"semantic_edit_newline_policy_invalid:{index}")
        if preserve and replacement and old.endswith("\r\n") and not replacement.endswith(("\n", "\r")):
            replacement += "\r\n"
        elif preserve and replacement and old.endswith("\n") and not replacement.endswith(("\n", "\r")):
            replacement += "\n"
        normalized.append((start, end, replacement, len(old_bytes), len(replacement.encode("utf-8"))))

    ordered = sorted(normalized, key=lambda value: (value[0], value[1]))
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] <= previous[1]:
            raise SemanticEditError(
                f"semantic_edit_ranges_overlap:{previous[0]}-{previous[1]}:{current[0]}-{current[1]}"
            )

    for start, end, replacement, _old_bytes, _new_bytes in reversed(ordered):
        lines[start - 1 : end] = [replacement]
    next_text = "".join(lines)
    next_bytes = len(next_text.encode("utf-8"))
    if next_bytes > MAX_FILE_BYTES:
        raise SemanticEditError("semantic_edit_result_too_large")
    old_region_bytes = sum(item[3] for item in normalized)
    replacement_bytes = sum(item[4] for item in normalized)
    return next_text, {
        "range_count": len(normalized),
        "old_region_bytes": old_region_bytes,
        "replacement_bytes": replacement_bytes,
        "model_reemitted_old_bytes": 0,
        "whole_file_output_required": False,
        "token_savings_claimed": False,
    }
