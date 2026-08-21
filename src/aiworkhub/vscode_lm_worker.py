"""Isolated worker endpoint for the VS Code Language Model bridge."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import tempfile
import textwrap
import time
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, cast

from . import semantic_edit
from .vscode_lm_bridge import (
    EDIT_RESPONSE_SCHEMA_ID,
    EDIT_RESPONSE_SCHEMA_ID_V1,
    EDIT_RESPONSE_SCHEMA_ID_V2,
    ProgressFileSignature,
    ProgressReadTransientError,
    ProgressReceiptSecurityError,
    RESPONSE_SCHEMA_ID,
    progress_file_signature,
    read_progress_receipt_snapshot,
    read_terminal_decision,
)


MAX_V2_PATHS = 128
MAX_V2_REPLACEMENTS_PER_FILE = 256
MAX_V2_REPLACEMENT_BYTES = 2 * 1024 * 1024
MAX_V2_FILE_BYTES = 16 * 1024 * 1024
PROGRESS_READ_MAX_ATTEMPTS = 2
PROGRESS_READ_RETRY_SECONDS = 0.01


def _load_json(path: Path, *, max_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise RuntimeError("bridge_document_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("bridge_document_not_object")
    return value


def _strip_fence(text: str) -> str:
    value = text.strip()
    open_run = len(value) - len(value.lstrip("`"))
    close_run = len(value) - len(value.rstrip("`"))
    if open_run >= 3 and close_run >= 3 and open_run == close_run:
        first_newline = value.find("\n")
        if first_newline >= 0:
            value = value[first_newline + 1 : len(value) - close_run].strip()
    return value


def _relative_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise RuntimeError("bridge_output_path_invalid")
    value = raw.strip().replace("\\", "/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or any(part.lower() == ".git" for part in path.parts)
    ):
        raise RuntimeError(f"bridge_output_path_escape:{value}")
    return path.as_posix()


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


_FIDELITY_ANGLE_PLACEHOLDER = re.compile(
    r"^\s*<\s*(?:(?:code|implementation)|new\s+[\w.:-]+\s+code|"
    r"test\s+file\s+content|"
    r"(?:full|complete)\s+file\s+content|insert\s+[^>]+\s+here)\s*>\s*$",
    re.IGNORECASE,
)
_FIDELITY_PLACEHOLDER_ONLY = re.compile(
    r"^(?:(?:todo|fixme|xxx)(?:\s*[:\-].*)?|placeholder|"
    r"replacement\s+code\s+only|(?:test\s+)?file\s+content|"
    r"(?:implementation|code)\s+omitted(?:\s+for\s+brevity)?|"
    r"omitted\s+(?:code|for\s+brevity)|"
    r"(?:the\s+)?rest\s+of\s+(?:the\s+)?code(?:\s+unchanged)?|"
    r"rest\s+unchanged|remainder\s+unchanged|unchanged\s+code|"
    r"insert\s+code\s+here|your\s+code\s+here)"
    r"[.!;,\-:]*$",
    re.IGNORECASE,
)
_CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js",
    ".jsx", ".kt", ".php", ".py", ".pyi", ".rb", ".rs", ".swift", ".ts",
    ".tsx",
}


def _classification_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", normalized.strip()).casefold()


def _trim_outer_blank_lines(value: str) -> str:
    lines = value.splitlines()
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def _markdown_fence_body(value: str) -> str | None:
    lines = value.splitlines()
    if len(lines) < 2:
        return None
    opening = re.fullmatch(r" {0,3}(?P<fence>`{3,}|~{3,}).*", lines[0])
    if opening is None:
        return None
    fence = opening.group("fence")
    closing = re.fullmatch(
        rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*",
        lines[-1],
    )
    if closing is None:
        return None
    return _trim_outer_blank_lines("\n".join(lines[1:-1]))


def _classification_payload(text: str) -> tuple[str, bool, bool, bool]:
    marker = "\ue000"
    prepared = text.replace("…", marker)
    normalized_prepared = unicodedata.normalize("NFKC", prepared)
    compatibility_changed = normalized_prepared != prepared
    value = _trim_outer_blank_lines(normalized_prepared)
    wrapped = False
    remaining_unwrap_budget = len(value)
    while remaining_unwrap_budget > 0:
        next_value: str | None = None
        fence_body = _markdown_fence_body(value)
        if fence_body is not None:
            next_value = fence_body
        stripped = value.strip()
        c_block = re.fullmatch(r"/\*(?P<body>.*?)\*/", stripped, re.DOTALL)
        if c_block is not None:
            next_value = _trim_outer_blank_lines(
                "\n".join(
                    re.sub(r"^\s*\*+\s?", "", line)
                    for line in c_block.group("body").splitlines()
                )
            )
        html_block = re.fullmatch(r"<!--(?P<body>.*?)-->", stripped, re.DOTALL)
        if html_block is not None:
            next_value = _trim_outer_blank_lines(html_block.group("body"))
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        line_values: list[str] = []
        for line in lines:
            comment = re.fullmatch(r"(?:/{2,}|#+|--+|;+)[ \t]?(?P<body>.*)", line)
            if comment is None:
                line_values = []
                break
            line_values.append(comment.group("body"))
        if lines and line_values:
            next_value = _trim_outer_blank_lines("\n".join(line_values))
        if next_value is None:
            break
        reduction = len(value) - len(next_value)
        if reduction <= 0:
            break
        value = next_value
        remaining_unwrap_budget -= reduction
        wrapped = True
    unicode_ellipsis = value.strip() == marker
    return (
        _classification_text(value.replace(marker, "...")),
        wrapped,
        unicode_ellipsis,
        compatibility_changed,
    )


def _python_fragment_tree(text: str) -> ast.AST | None:
    source = textwrap.dedent(text)
    try:
        return ast.parse(source)
    except SyntaxError:
        try:
            wrapped = "def __aiworkhub_fragment__():\n" + textwrap.indent(source, "    ")
            return ast.parse(wrapped)
        except SyntaxError:
            return None


def _stub_statement(node: ast.AST) -> bool:
    if isinstance(node, ast.Pass):
        return True
    if (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and node.value.value is Ellipsis
    ):
        return True
    if isinstance(node, ast.Raise) and isinstance(node.exc, (ast.Name, ast.Call)):
        name = node.exc.id if isinstance(node.exc, ast.Name) else getattr(node.exc.func, "id", "")
        return name == "NotImplementedError"
    return False


def _stub_definition(node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return bool(body) and all(
        _stub_statement(child) or _stub_definition(child)
        for child in body
    )


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _base_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    return ""


def _stub_context(old_text: str, path: str) -> bool:
    if PurePosixPath(path).suffix.casefold() == ".pyi":
        return True
    tree = _python_fragment_tree(old_text)
    if tree is None:
        return False
    bodies: list[ast.stmt] = list(getattr(tree, "body", []))
    if (
        len(bodies) == 1
        and isinstance(bodies[0], ast.FunctionDef)
        and bodies[0].name == "__aiworkhub_fragment__"
    ):
        bodies = bodies[0].body
    significant = [
        node
        for node in bodies
        if not isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    if len(significant) == 1:
        node = significant[0]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            _decorator_name(decorator) in {"abstractmethod", "overload"}
            for decorator in node.decorator_list
        ):
            return True
        if (
            isinstance(node, ast.ClassDef)
            and any(_base_name(base) == "Protocol" for base in node.bases)
            and _stub_definition(node)
        ):
            return True
    return bool(significant) and all(
        _stub_statement(node) or _stub_definition(node)
        for node in significant
    )


def _pass_only(text: str) -> bool:
    tree = _python_fragment_tree(text)
    if tree is None:
        return False
    bodies: list[ast.stmt] = list(getattr(tree, "body", []))
    if len(bodies) == 1 and isinstance(bodies[0], ast.FunctionDef) and bodies[0].name == "__aiworkhub_fragment__":
        bodies = bodies[0].body
    return bool(bodies) and all(
        isinstance(node, ast.Pass)
        or (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))
        for node in bodies
    ) and any(isinstance(node, ast.Pass) for node in bodies)


def _meaningful_old_code(old_text: str, path: str) -> bool:
    if not old_text.strip() or _stub_context(old_text, path):
        return False
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix in {".py", ".pyi"}:
        tree = _python_fragment_tree(old_text)
        if tree is None:
            return False
        meaningful = (
            ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Call, ast.ClassDef,
            ast.For, ast.FunctionDef, ast.AsyncFunctionDef, ast.If, ast.Import,
            ast.ImportFrom, ast.Match, ast.Return, ast.Try, ast.While, ast.With,
            ast.Yield, ast.YieldFrom,
        )
        return any(isinstance(node, meaningful) for node in ast.walk(tree))
    if suffix not in _CODE_SUFFIXES:
        return False
    return bool(re.search(r"[{}();=]|\b(?:class|def|function|return|if|for|while|import|const|let|var)\b", old_text))


def _prose_not_code(new_text: str, path: str) -> bool:
    value = new_text.strip()
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", value)
    if len(words) < 2 or len(value) > 256 or re.search(r"[{};=]", value):
        return False
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix in {".py", ".pyi"} and _python_fragment_tree(value) is not None:
        return False
    if re.search(r"\b(?:return|raise|yield|import|from|class|def|if|for|while|const|let|var|function)\b", value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9\s.,'\"!?()\-]+", value))


def _fidelity_reject(reason: str, path: str, operation: str) -> None:
    raise RuntimeError(f"vscode_lm_edit_fidelity_rejected:{reason}:{path}:{operation}")


def _check_edit_fidelity(
    old_text: str,
    new_text: str,
    *,
    path: str,
    operation: str,
    create: bool = False,
) -> dict[str, Any]:
    """Reject deterministic non-code placeholders before any file mutation."""

    old_bytes = len(old_text.encode("utf-8"))
    new_bytes = len(new_text.encode("utf-8"))
    if operation.startswith("v3_range:") and new_text == "":
        return {
            "path": path,
            "operation": operation,
            "old_bytes": old_bytes,
            "new_bytes": 0,
        }
    normalized, wrapped, unicode_ellipsis, compatibility_changed = (
        _classification_payload(new_text)
    )
    old_normalized, _old_wrapped, _old_unicode_ellipsis, _old_compatibility_changed = (
        _classification_payload(old_text)
    )
    if create and not normalized:
        _fidelity_reject("empty_required_create", path, operation)
    if normalized == "..." and (
        create
        or unicode_ellipsis
        or compatibility_changed
        or wrapped
        or old_normalized == "..."
        or not _stub_context(old_text, path)
    ):
        _fidelity_reject("ellipsis_only", path, operation)
    if (
        _FIDELITY_PLACEHOLDER_ONLY.fullmatch(normalized)
        or _FIDELITY_ANGLE_PLACEHOLDER.fullmatch(normalized)
    ):
        _fidelity_reject("placeholder_phrase", path, operation)
    if (
        _pass_only(new_text)
        and _meaningful_old_code(old_text, path)
        and not _stub_context(old_text, path)
    ):
        _fidelity_reject("pass_only_nontrivial_replacement", path, operation)
    if (
        old_bytes >= 64
        and new_bytes <= max(24, int(old_bytes * 0.35))
        and _meaningful_old_code(old_text, path)
        and _prose_not_code(new_text, path)
    ):
        _fidelity_reject("destructive_non_code_shrink", path, operation)
    return {
        "path": path,
        "operation": operation,
        "old_bytes": old_bytes,
        "new_bytes": new_bytes,
    }


def _write_atomic(workspace: Path, relative: str, content: str) -> None:
    relative = _relative_path(relative)
    workspace = workspace.resolve()
    lexical_target = workspace / relative
    cursor = workspace
    for part in PurePosixPath(relative).parts:
        cursor /= part
        if cursor.is_symlink():
            raise RuntimeError(f"bridge_output_symlink:{relative}")
    target = lexical_target.resolve(strict=False)
    if target != workspace and workspace not in target.parents:
        raise RuntimeError(f"bridge_output_path_escape:{relative}")
    target.parent.mkdir(parents=True, exist_ok=True)

    existing_mode = None
    if target.exists():
        try:
            existing_mode = target.stat().st_mode & 0o777
        except OSError:
            existing_mode = None

    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline='', closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        closing_fd = fd
        fd = -1
        os.close(closing_fd)
        if existing_mode is not None:
            try:
                os.chmod(tmp_name, existing_mode)
            except OSError as exc:
                raise RuntimeError(
                    f"bridge_output_chmod_failed:{relative}"
                ) from exc
        os.replace(tmp_name, target)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _target_path(workspace: Path, relative: str) -> Path:
    workspace = workspace.resolve()
    cursor = workspace
    for part in PurePosixPath(relative).parts:
        cursor /= part
        if cursor.is_symlink():
            raise RuntimeError(f"bridge_output_symlink:{relative}")
    target = (workspace / relative).resolve(strict=False)
    if target != workspace and workspace not in target.parents:
        raise RuntimeError(f"bridge_output_path_escape:{relative}")
    return target


def _validate_allowed_path(raw_path: Any, allowed: list[str]) -> str:
    relative = _relative_path(raw_path)
    if not _matches(relative, allowed):
        raise RuntimeError(f"vscode_lm_output_out_of_scope:{relative}")
    return relative


def _required_create_paths(
    create_paths: set[str] | None,
    allowed: list[str],
) -> set[str]:
    return {
        _validate_allowed_path(value, allowed)
        for value in (create_paths or set())
    }


def _check_required_creates(
    required: set[str],
    contents: dict[str, str],
    *,
    operation: str,
) -> None:
    for relative in sorted(required):
        if relative not in contents:
            _fidelity_reject("missing_required_create", relative, operation)
        normalized, _wrapped, _unicode_ellipsis, _compatibility_changed = (
            _classification_payload(contents[relative])
        )
        if not normalized:
            _fidelity_reject("empty_required_create", relative, operation)


def _v1_planned_outputs(
    workspace: Path,
    edit: dict[str, Any],
    allowed: list[str],
    create_paths: set[str] | None = None,
) -> list[tuple[str, str]]:
    files = edit.get("files")
    if not isinstance(files, list):
        raise RuntimeError("vscode_lm_edit_response_files_invalid")
    planned: list[tuple[str, str]] = []
    seen: set[str] = set()
    required_creates = _required_create_paths(create_paths, allowed)
    create_contents: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise RuntimeError("vscode_lm_edit_response_file_invalid")
        relative = _validate_allowed_path(item.get("path"), allowed)
        if relative in seen:
            raise RuntimeError(f"vscode_lm_edit_response_duplicate_path:{relative}")
        seen.add(relative)
        target = _target_path(workspace, relative)
        old_text = ""
        if target.is_file() and not target.is_symlink():
            old_text = target.read_bytes().decode("utf-8", errors="replace")
        content = item["content"]
        is_create = relative in required_creates
        if is_create:
            create_contents[relative] = content
        _check_edit_fidelity(
            old_text,
            content,
            path=relative,
            operation="v1_file",
            create=is_create,
        )
        planned.append((relative, content))
    _check_required_creates(
        required_creates,
        create_contents,
        operation="v1_file",
    )
    return planned


def _require_sha256(value: Any, relative: str) -> str:
    digest = str(value or "")
    if (
        len(digest) != 64
        or digest.lower() != digest
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise RuntimeError(f"vscode_lm_edit_response_hash_invalid:{relative}")
    return digest


def _validate_v2_counts(edits: Any, creates: Any) -> None:
    if not isinstance(edits, list) or not isinstance(creates, list):
        raise RuntimeError("vscode_lm_edit_response_v2_shape_invalid")
    if len(edits) + len(creates) > MAX_V2_PATHS:
        raise RuntimeError("vscode_lm_edit_response_v2_path_count_exceeded")


def _v2_planned_outputs(
    workspace: Path,
    edit: dict[str, Any],
    allowed: list[str],
    create_paths: set[str] | None = None,
) -> list[tuple[str, str]]:
    edits = edit.get("edits", [])
    creates = edit.get("creates", [])
    _validate_v2_counts(edits, creates)
    planned: list[tuple[str, str]] = []
    seen: set[str] = set()
    required_creates = _required_create_paths(create_paths, allowed)
    create_contents: dict[str, str] = {}

    for item in edits:
        if not isinstance(item, dict):
            raise RuntimeError("vscode_lm_edit_response_edit_invalid")
        relative = _validate_allowed_path(item.get("path"), allowed)
        if relative in seen:
            raise RuntimeError(f"vscode_lm_edit_response_duplicate_path:{relative}")
        seen.add(relative)
        expected_hash = _require_sha256(item.get("current_sha256"), relative)
        replacements = item.get("replacements")
        if (
            not isinstance(replacements, list)
            or not replacements
            or len(replacements) > MAX_V2_REPLACEMENTS_PER_FILE
        ):
            raise RuntimeError(f"vscode_lm_edit_response_replacements_invalid:{relative}")
        target = _target_path(workspace, relative)
        if target.is_symlink() or not target.is_file():
            raise RuntimeError(f"vscode_lm_edit_response_edit_target_invalid:{relative}")
        current_bytes = target.read_bytes()
        actual_hash = hashlib.sha256(current_bytes).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"vscode_lm_edit_response_stale_hash:{relative}:"
                f"expected_sha256={expected_hash}:actual_sha256={actual_hash}"
            )
        try:
            current_text = current_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                f"vscode_lm_edit_response_current_utf8_invalid:{relative}"
            ) from exc
        next_text = current_text
        for replacement_index, replacement in enumerate(replacements):
            if not isinstance(replacement, dict):
                raise RuntimeError(
                    f"vscode_lm_edit_response_replacement_invalid:{relative}"
                )
            old = replacement.get("old")
            new = replacement.get("new")
            expected_count = replacement.get("expected_count")
            if (
                not isinstance(old, str)
                or not old
                or not isinstance(new, str)
                or not new
                or not isinstance(expected_count, int)
                or isinstance(expected_count, bool)
                or expected_count < 1
            ):
                raise RuntimeError(
                    f"vscode_lm_edit_response_replacement_invalid:{relative}"
                )
            if (
                len(old.encode("utf-8")) > MAX_V2_REPLACEMENT_BYTES
                or len(new.encode("utf-8")) > MAX_V2_REPLACEMENT_BYTES
            ):
                raise RuntimeError(
                    f"vscode_lm_edit_response_replacement_too_large:{relative}"
                )
            _check_edit_fidelity(
                old,
                new,
                path=relative,
                operation=f"v2_replacement:{replacement_index}",
            )
            actual_count = next_text.count(old)
            if actual_count != expected_count:
                old_bytes = old.encode("utf-8")
                raise RuntimeError(
                    f"vscode_lm_edit_response_replacement_count:{relative}:"
                    f"index={replacement_index}:actual={actual_count}:"
                    f"expected={expected_count}:old_sha256="
                    f"{hashlib.sha256(old_bytes).hexdigest()}:"
                    f"old_bytes={len(old_bytes)}"
                )
            next_text = next_text.replace(old, new)
            if len(next_text.encode("utf-8")) > MAX_V2_FILE_BYTES:
                raise RuntimeError(
                    f"vscode_lm_edit_response_file_too_large:{relative}"
                )
        planned.append((relative, next_text))

    for item in creates:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise RuntimeError("vscode_lm_edit_response_create_invalid")
        relative = _validate_allowed_path(item.get("path"), allowed)
        if relative in seen:
            raise RuntimeError(f"vscode_lm_edit_response_duplicate_path:{relative}")
        seen.add(relative)
        target = _target_path(workspace, relative)
        precreated_placeholder = (
            relative in (create_paths or set())
            and target.is_file()
            and not target.is_symlink()
            and target.stat().st_size == 0
        )
        if (target.exists() or target.is_symlink()) and not precreated_placeholder:
            raise RuntimeError(f"vscode_lm_edit_response_create_exists:{relative}")
        content = item["content"]
        create_contents[relative] = content
        if len(content.encode("utf-8")) > MAX_V2_FILE_BYTES:
            raise RuntimeError(f"vscode_lm_edit_response_file_too_large:{relative}")
        _check_edit_fidelity(
            "",
            content,
            path=relative,
            operation="v2_create",
            create=True,
        )
        planned.append((relative, content))
    _check_required_creates(
        required_creates,
        create_contents,
        operation="v2_create",
    )
    return planned


def _v3_planned_outputs(
    workspace: Path,
    edit: dict[str, Any],
    allowed: list[str],
    create_paths: set[str] | None = None,
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """Plan hash-bound line-range edits without requiring old/full-file output."""

    edits = edit.get("edits", [])
    creates = edit.get("creates", [])
    for key in ("deletes", "files", "replacements"):
        if key in edit and edit.get(key) is not None:
            raise RuntimeError(f"vscode_lm_edit_response_unsupported_top_level:{key}")
    _validate_v2_counts(edits, creates)
    planned: list[tuple[str, str]] = []
    metrics: list[dict[str, Any]] = []
    seen: set[str] = set()
    required_creates = _required_create_paths(create_paths, allowed)
    create_contents: dict[str, str] = {}

    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for idx, item in enumerate(edits):
        if not isinstance(item, dict):
            raise RuntimeError("vscode_lm_semantic_edit_invalid")
        relative = _validate_allowed_path(item.get("path"), allowed)
        expected_hash = _require_sha256(item.get("current_sha256"), relative)
        ranges = item.get("ranges")
        if not isinstance(ranges, list) or not ranges:
            raise RuntimeError(f"vscode_lm_semantic_edit_ranges_invalid:{relative}")
        range_sources = []
        for range_idx, range_item in enumerate(ranges):
            if not isinstance(range_item, dict):
                raise RuntimeError(f"vscode_lm_semantic_edit_ranges_invalid:{relative}")
            start = range_item.get("start_line")
            end = range_item.get("end_line")
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
            ):
                raise RuntimeError(f"vscode_lm_semantic_edit_ranges_invalid:{relative}")
            range_sources.append((start, end, idx, range_idx))
        if relative not in groups:
            groups[relative] = {
                "hash": expected_hash,
                "ranges": [],
                "range_sources": [],
                "first_idx": idx,
                "entry_count": 0,
            }
            order.append(relative)
            seen.add(relative)
        else:
            group = groups[relative]
            if group["hash"] != expected_hash:
                raise RuntimeError(
                    f"vscode_lm_edit_response_hash_conflict:{relative}:"
                    f"entry_index1={group['first_idx']}:entry_index2={idx}"
                )
        groups[relative]["ranges"].extend(ranges)
        groups[relative]["range_sources"].extend(range_sources)
        groups[relative]["entry_count"] += 1

    for relative in order:
        group = groups[relative]
        expected_hash = group["hash"]
        ranges = group["ranges"]
        ordered_sources = sorted(
            group["range_sources"],
            key=lambda value: (value[0], value[1], value[2], value[3]),
        )
        for previous, current in zip(ordered_sources, ordered_sources[1:]):
            if current[0] <= previous[1]:
                raise RuntimeError(
                    f"vscode_lm_semantic_edit_rejected:{relative}:"
                    f"entry_index1={previous[2]}:range_index1={previous[3]}:"
                    f"entry_index2={current[2]}:range_index2={current[3]}:"
                    f"semantic_edit_ranges_overlap:{previous[0]}-{previous[1]}:"
                    f"{current[0]}-{current[1]}"
        )
        target = _target_path(workspace, relative)
        if target.is_symlink() or not target.is_file():
            raise RuntimeError(
                f"vscode_lm_edit_response_edit_target_invalid:{relative}"
            )
        required_create = relative in required_creates
        current_bytes = target.read_bytes()
        if required_create and current_bytes:
            raise RuntimeError(f"vscode_lm_edit_response_create_exists:{relative}")
        actual_hash = hashlib.sha256(current_bytes).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"vscode_lm_edit_response_stale_hash:{relative}:"
                f"expected_sha256={expected_hash}:actual_sha256={actual_hash}"
            )
        try:
            current_text = current_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                f"vscode_lm_edit_response_current_utf8_invalid:{relative}"
            ) from exc
        try:
            next_text, edit_metrics = semantic_edit.apply_line_ranges(
                current_text, ranges
            )
        except semantic_edit.SemanticEditError as exc:
            first_idx = group["first_idx"]
            raise RuntimeError(
                f"vscode_lm_semantic_edit_rejected:{relative}:"
                f"entry_index1={first_idx}:{exc}"
            ) from exc
        current_lines = current_text.splitlines(keepends=True)
        for range_index, range_item in enumerate(ranges):
            start = range_item["start_line"]
            end = range_item["end_line"]
            old_fragment = (
                "" if not current_lines else "".join(current_lines[start - 1 : end])
            )
            _check_edit_fidelity(
                old_fragment,
                range_item["new"],
                path=relative,
                operation=f"v3_range:{range_index}",
            )
        next_bytes = next_text.encode("utf-8")
        if len(next_bytes) > MAX_V2_FILE_BYTES:
            raise RuntimeError(f"vscode_lm_edit_response_file_too_large:{relative}")
        if required_create:
            # The bridge stages declared new outputs as empty placeholders so
            # v3 can fill them with a bounded virtual-line edit.  That is a
            # create for contract/fidelity purposes even though the protocol
            # represents it in ``edits`` rather than ``creates``.
            _check_edit_fidelity(
                "",
                next_text,
                path=relative,
                operation="v3_create",
                create=True,
            )
            create_contents[relative] = next_text
        planned.append((relative, next_text))
        metric = {
            "path": relative,
            "file_bytes": len(current_bytes),
            "entry_index1": group["first_idx"],
            "entry_count": group["entry_count"],
            **edit_metrics,
        }
        if required_create:
            metric.update({
                "create": True,
                "replacement_bytes": len(next_bytes),
                "whole_file_output_required": True,
                "token_savings_claimed": False,
            })
        metrics.append(metric)

    for item in creates:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise RuntimeError("vscode_lm_edit_response_create_invalid")
        relative = _validate_allowed_path(item.get("path"), allowed)
        if relative in seen:
            raise RuntimeError(f"vscode_lm_edit_response_duplicate_path:{relative}")
        seen.add(relative)
        target = _target_path(workspace, relative)
        precreated_placeholder = (
            relative in (create_paths or set())
            and target.is_file()
            and not target.is_symlink()
            and target.stat().st_size == 0
        )
        if (target.exists() or target.is_symlink()) and not precreated_placeholder:
            raise RuntimeError(f"vscode_lm_edit_response_create_exists:{relative}")
        content = item["content"]
        create_contents[relative] = content
        if len(content.encode("utf-8")) > MAX_V2_FILE_BYTES:
            raise RuntimeError(f"vscode_lm_edit_response_file_too_large:{relative}")
        _check_edit_fidelity(
            "",
            content,
            path=relative,
            operation="v3_create",
            create=True,
        )
        planned.append((relative, content))
        metrics.append({
            "path": relative,
            "create": True,
            "replacement_bytes": len(content.encode("utf-8")),
            "whole_file_output_required": True,
            "token_savings_claimed": False,
        })
    _check_required_creates(
        required_creates,
        create_contents,
        operation="v3_create",
    )
    return planned, metrics


def _read_progress_with_retry(
    path: Path,
    request_id: str,
    repo_id: str,
    *,
    owner_uid: int | None = None,
    previous_sequence: int | None = None,
    defer_transient: bool,
) -> tuple[dict[str, Any], ProgressFileSignature | None]:
    for attempt in range(1, PROGRESS_READ_MAX_ATTEMPTS + 1):
        try:
            return cast(
                tuple[dict[str, Any], ProgressFileSignature | None],
                read_progress_receipt_snapshot(
                    path,
                    request_id,
                    repo_id,
                    owner_uid=owner_uid,
                    previous_sequence=previous_sequence,
                ),
            )
        except ProgressReadTransientError as exc:
            if attempt >= PROGRESS_READ_MAX_ATTEMPTS:
                if defer_transient:
                    return {}, None
                raise RuntimeError(
                    f"vscode_lm_progress_terminal_read_failed:{exc}"
                ) from exc
            time.sleep(PROGRESS_READ_RETRY_SECONDS)
    raise AssertionError("unreachable")


def run(spec_path: Path) -> dict[str, Any]:
    spec = _load_json(spec_path)
    if spec.get("schema_id") != "aiworkhub.vscode_lm.worker_spec.v1":
        raise RuntimeError("bridge_worker_spec_schema_mismatch")
    workspace = Path(str(spec.get("workspace_path") or "")).resolve(strict=True)
    response_path = Path(str(spec.get("response_path") or ""))
    terminal_decision_required = spec.get("terminal_decision_required") is True
    cancel_token = str(spec.get("cancel_token") or "")
    cancel_path = Path(str(spec.get("cancel_path") or response_path))
    if terminal_decision_required and cancel_path.resolve(strict=False) != response_path.resolve(
        strict=False
    ):
        raise RuntimeError("vscode_lm_terminal_decision_path_mismatch")
    progress_path_raw = str(spec.get("progress_path") or "")
    progress_path = Path(progress_path_raw) if progress_path_raw else None
    timeout_seconds = max(30, min(int(spec.get("timeout_seconds") or 7200), 86_400))
    deadline = time.monotonic() + timeout_seconds
    last_progress_sequence = 0
    last_progress_signature: ProgressFileSignature | None = None
    response: dict[str, Any] | None = None
    decision_action = ""
    while time.monotonic() < deadline:
        if terminal_decision_required:
            decision = read_terminal_decision(
                response_path,
                request_id=str(spec.get("request_id") or ""),
                repo_id=str(spec.get("repo_id") or ""),
                cancel_token=cancel_token,
            )
            if decision is not None:
                response, decision_action = decision
                break
        elif response_path.is_file():
            response = _load_json(response_path)
            break
        if progress_path is not None:
            try:
                progress_stat = progress_path.lstat()
                progress_signature = progress_file_signature(progress_stat)
            except OSError:
                progress_signature = None
            if progress_signature is not None and progress_signature != last_progress_signature:
                progress, validated_signature = _read_progress_with_retry(
                    progress_path,
                    str(spec.get("request_id") or ""),
                    str(spec.get("repo_id") or ""),
                    owner_uid=os.getuid() if hasattr(os, "getuid") else None,
                    previous_sequence=(last_progress_sequence or None),
                    defer_transient=True,
                )
                if progress:
                    last_progress_sequence = int(progress["sequence"])
                    progress_event = {
                        "type": "aiworkhub_progress",
                        "sequence": last_progress_sequence,
                        "phase": str(progress["phase"]),
                        "updated_at": str(progress["updated_at"]),
                    }
                    for key in (
                        "tool_name", "tool_state", "elapsed_ms", "error_code",
                        "timeout_phase", "timeout_ms",
                    ):
                        if key in progress:
                            progress_event[key] = progress[key]
                    print(
                        json.dumps(
                            progress_event,
                            ensure_ascii=True,
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    last_progress_signature = validated_signature
        time.sleep(0.1)
    else:
        raise RuntimeError("vscode_lm_response_timeout")

    if response is None:
        raise RuntimeError("vscode_lm_terminal_decision_missing")
    if response.get("schema_id") != RESPONSE_SCHEMA_ID:
        raise RuntimeError("vscode_lm_response_schema_mismatch")
    if response.get("request_id") != spec.get("request_id"):
        raise RuntimeError("vscode_lm_response_identity_mismatch")
    if terminal_decision_required:
        if response.get("repo_id") != spec.get("repo_id"):
            raise RuntimeError("vscode_lm_response_repo_identity_mismatch")
        if decision_action not in {"cancel", "response"}:
            raise RuntimeError("vscode_lm_terminal_decision_action_invalid")
    if progress_path is not None:
        _read_progress_with_retry(
            progress_path,
            str(spec.get("request_id") or ""),
            str(spec.get("repo_id") or ""),
            owner_uid=os.getuid() if hasattr(os, "getuid") else None,
            defer_transient=False,
        )
    if response.get("error"):
        diagnostics = response.get("diagnostics")
        detail = ""
        if isinstance(diagnostics, dict):
            bounded = {
                "protocol_preview": str(diagnostics.get("protocol_preview") or "")[:768],
                "turn_trace": list(diagnostics.get("turn_trace") or [])[-16:],
            }
            detail = f":diagnostics={json.dumps(bounded, ensure_ascii=True, separators=(',', ':'))[:2048]}"
        raise RuntimeError(f"vscode_lm_request_failed:{response.get('error')}{detail}")
    raw_text = str(response.get("text") or "")
    try:
        edit = json.loads(_strip_fence(raw_text))
    except json.JSONDecodeError as exc:
        raise RuntimeError("vscode_lm_edit_response_invalid_json") from exc
    if not isinstance(edit, dict) or edit.get("schema_id") not in {
        EDIT_RESPONSE_SCHEMA_ID,
        EDIT_RESPONSE_SCHEMA_ID_V2,
        EDIT_RESPONSE_SCHEMA_ID_V1,
    }:
        raise RuntimeError("vscode_lm_edit_response_schema_mismatch")
    allowed = [str(value) for value in spec.get("allowed_writes") or []]
    try:
        semantic_metrics: list[dict[str, Any]] = []
        create_paths = {
            str(value) for value in spec.get("create_paths") or [] if str(value)
        }
        if edit.get("schema_id") == EDIT_RESPONSE_SCHEMA_ID_V1:
            planned = _v1_planned_outputs(workspace, edit, allowed, create_paths)
        elif edit.get("schema_id") == EDIT_RESPONSE_SCHEMA_ID_V2:
            planned = _v2_planned_outputs(workspace, edit, allowed, create_paths)
        else:
            planned, semantic_metrics = _v3_planned_outputs(
                workspace, edit, allowed, create_paths
            )
    except RuntimeError as exc:
        response_bytes = raw_text.encode("utf-8")
        raise RuntimeError(
            f"{exc}:response_sha256={hashlib.sha256(response_bytes).hexdigest()}:"
            f"response_bytes={len(response_bytes)}"
        ) from exc
    # Scope-validate the complete response before the first mutation.  This
    # prevents a mixed valid/invalid model response from partially applying.
    # Compute expected hash/bytes for every planned content *before* any
    # write so that read-back verification never emits false evidence.
    expected_content: dict[str, tuple[bytes, str, int]] = {}
    for relative, content in planned:
        content_bytes = content.encode("utf-8")
        expected_content[relative] = (
            content_bytes,
            hashlib.sha256(content_bytes).hexdigest(),
            len(content_bytes),
        )
    written: list[str] = []
    final_hashes: dict[str, str] = {}
    final_sizes: dict[str, int] = {}
    for relative, content in planned:
        exp_bytes, exp_hash, exp_size = expected_content[relative]
        target = _target_path(workspace, relative)
        existing_bytes = target.read_bytes() if target.is_file() else None
        if existing_bytes == exp_bytes:
            readback_bytes = existing_bytes
        else:
            _write_atomic(workspace, relative, content)
            readback_bytes = target.read_bytes()
        if readback_bytes != exp_bytes:
            raise RuntimeError(
                f"vscode_lm_edit_response_write_readback_mismatch:"
                f"{relative}:expected_sha256={exp_hash}:"
                f"expected_bytes={exp_size}:"
                f"actual_sha256="
                f"{hashlib.sha256(readback_bytes).hexdigest()}:"
                f"actual_bytes={len(readback_bytes)}"
            )
        if existing_bytes != exp_bytes:
            written.append(relative)
        final_hashes[relative] = exp_hash
        final_sizes[relative] = exp_size
    written_set = set(written)
    for metric in semantic_metrics:
        metric_path = metric.get("path")
        if metric_path in final_hashes:
            metric["final_sha256"] = final_hashes[metric_path]
            metric["final_bytes"] = final_sizes[metric_path]
            metric["no_op"] = metric_path not in written_set
        metric["apply_surface"] = "vscode_lm_worker_provider_side"
        metric["mcp_receipt"] = None
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": str(edit.get("summary") or "GLM VS Code worker completed"),
        "model": response.get("model"),
        "changed_paths": sorted(set(written)),
        "edit_protocol": str(edit.get("schema_id") or ""),
        "semantic_edit_metrics": semantic_metrics,
        "project_context_receipt": str(spec.get("project_context_receipt") or ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--spec",
        default=str(_default_spec_path()),
    )
    args = parser.parse_args(argv)
    try:
        result = run(Path(args.spec))
    except Exception as exc:  # noqa: BLE001
        failure = {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "error": str(exc),
        }
        if isinstance(exc, ProgressReceiptSecurityError):
            failure["diagnostics"] = {"progress_security": exc.receipt}
        print(json.dumps(failure, ensure_ascii=True, sort_keys=True))
        return 1
    receipt = str(result.pop("project_context_receipt", "") or "").strip()
    if receipt:
        print(receipt)
    # Keep the stdio protocol independent of the Windows console code page.
    # A default cp1251/cp1252 stdout cannot encode otherwise-valid model text
    # such as arrows or CJK characters, while JSON escapes round-trip exactly.
    print(json.dumps(result, ensure_ascii=True))
    return 0


def _default_spec_path() -> Path:
    """Return a usable default even in a deliberately minimal child env.

    ``Path.home()`` raises on Windows when neither USERPROFILE nor a complete
    HOMEDRIVE/HOMEPATH pair is present.  The launcher normally supplies an
    explicit ``--spec`` path, so cwd is a safe last-resort default that also
    lets ``--help`` work in diagnostic subprocesses.
    """
    try:
        return Path.home() / ".aiworkhub_vscode_lm_worker.json"
    except RuntimeError:
        return Path.cwd() / ".aiworkhub_vscode_lm_worker.json"


if __name__ == "__main__":
    raise SystemExit(main())
