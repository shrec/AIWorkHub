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
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, cast

from . import semantic_edit
from .platform_io import current_user_uid
from .vscode_lm_activity import ActivityCaptureError, ActivityReader
from .runtime_adapters import EDITOR_REQUESTED_MODEL_RE
from .semantic_edit import coerce_protocol_line
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

# ---------------------------------------------------------------------------
# NF-2026-01036 part A: a provider credit/balance refusal is a ROUTE fact.
#
# Measured: the extension host answered a turn with the provider's own
# ``error`` field ``You've reached your monthly credit limit. Please enable
# additional paid credits or wait until your credits reset on October 1, 2026
# at 4:00 AM.``, which this worker re-raised as
# ``vscode_lm_request_failed:<sentence>`` and the launcher filed as
# ``worker_failed``/``unknown`` -- so the route circuit never learned it and
# every unpinned launch picked the same exhausted route again.
#
# Only the HOST-OWNED ``response["error"]`` field is ever matched here, and only
# when the WHOLE field is the provider sentence (anchored at both ends): an
# extension protocol failure is prefixed with its own ``vscode_lm_*`` code, and
# worker/model prose never reaches this function at all.  The result is a
# structured ``provider_error`` (the ``workforce_catalog`` sealed shape) that the
# launcher re-validates before sealing it onto the card.
VSCODE_LM_PROVIDER_BALANCE_EXHAUSTED = "vscode_lm_provider_balance_exhausted"
PROVIDER_ROUTE_ERROR_SCHEMA_ID = "aiworkhub.provider_route_error.v1"
PROVIDER_ERROR_SOURCE = "vscode_lm_extension_response"
_CREDIT_LIMIT_RE = re.compile(
    r"You(?:'|’)ve reached your (?:monthly )?credit limit\."
    r"(?P<rest>[^\r\n]{0,400})"
)
_CREDIT_RESET_RE = re.compile(
    r"credits reset on (?P<when>[A-Z][a-z]{2,8} \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M)\.?\s*\Z"
)
# The provider prints the reset as a wall-clock time WITHOUT a zone.  The
# conservative reading is the LATEST instant that wall time can denote, i.e.
# the westernmost civil offset UTC-12:00: the route is then never re-admitted
# before the provider could actually have reset, only (at most ~26 h) after.
_RESET_ASSUMED_UTC_OFFSET = timedelta(hours=-12)


class VscodeLmProviderRefusal(RuntimeError):
    """A structured provider refusal read from the host-owned response field."""

    def __init__(self, provider_error: dict[str, Any]) -> None:
        super().__init__(VSCODE_LM_PROVIDER_BALANCE_EXHAUSTED)
        self.provider_error = provider_error


def _credit_reset_iso(rest: str) -> str:
    match = _CREDIT_RESET_RE.search(rest)
    if match is None:
        return ""
    try:
        wall = datetime.strptime(match.group("when"), "%B %d, %Y at %I:%M %p")
    except ValueError:
        return ""
    local = wall.replace(tzinfo=timezone(_RESET_ASSUMED_UTC_OFFSET))
    return local.astimezone(timezone.utc).isoformat()


def provider_balance_error(response_error: Any) -> dict[str, Any] | None:
    """Return the structured provider error for a credit-limit refusal, or None.

    ``response_error`` must be the extension host's own ``response["error"]``
    value.  The whole field must be the provider's credit-limit sentence.
    """

    if not isinstance(response_error, str):
        return None
    text = response_error.strip()
    match = _CREDIT_LIMIT_RE.fullmatch(text)
    if match is None:
        return None
    reset_at = _credit_reset_iso(match.group("rest"))
    sealed: dict[str, Any] = {
        "schema_id": PROVIDER_ROUTE_ERROR_SCHEMA_ID,
        "owner": "provider",
        "sealed": True,
        "source": PROVIDER_ERROR_SOURCE,
        "code": "insufficient_balance",
        "http_status": 402,
        "detail": text[:300],
    }
    if reset_at:
        sealed["reset_at"] = reset_at
        sealed["reset_timezone_assumed"] = "UTC-12:00"
    return sealed


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


REASONING_CONTEXT_ATTEMPT_SCHEMA_ID = "aiworkhub.reasoning_context_attempt.v1"
_ATTEMPT_MAX_BYTES = 4096
_ATTEMPT_MAX_SEND_TURNS = 64
# The host reports capacity as a JavaScript number, so it is bounded to a safe integer.
_ATTEMPT_MAX_CAPACITY_TOKENS = 2**53 - 1
_ATTEMPT_IDENTITY_RE = re.compile(r"[ -~]{1,128}")
_ATTEMPT_LABEL_RE = re.compile(r"[ -~]{0,128}")
_ATTEMPT_OPTION_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/ -]{0,63}")
_ATTEMPT_FIELDS = (
    "schema_id",
    "request_id",
    "repo_id",
    "requested_model",
    "host_model",
    "requested_profile",
    "send_state",
    "send_turn_count",
    "provider_request_acknowledged",
    "option_status",
    "option_key",
    "option_value",
    "context_capacity_tokens",
    "context_capacity_source",
    "provider_internal_state",
    "unknown_reason",
)
_ATTEMPT_HOST_MODEL_FIELDS = ("id", "family", "name", "vendor", "version")
_ATTEMPT_VOCABULARY_FIELDS = (
    "requested_profile",
    "send_state",
    "option_status",
    "context_capacity_source",
    "provider_internal_state",
)
_ATTEMPT_PROFILES = frozenset({"canonical_medium_high", "canonical_high", "canonical_maximum"})
_ATTEMPT_SEND_STATES = frozenset({"sent", "not_sent"})
_ATTEMPT_OPTION_STATUSES = frozenset({
    "applied",
    "unsupported",
    "provider_default",
    "unverifiable",
    "capability_ceiling",
    "unknown",
    "not_sent",
})
_ATTEMPT_CAPACITY_SOURCES = frozenset({"model.maxInputTokens", "request.model_context", "unknown"})
_ATTEMPT_HOST_UNKNOWN_REASONS = frozenset({
    "option_shape_unrecognized",
    "sent_option_disagrees_with_effort_status",
    "option_changed_between_turns",
    "send_turn_count_out_of_bounds",
    "receipt_recorder_error",
})
_ATTEMPT_WORKER_UNKNOWN_REASONS = frozenset({
    "receipt_absent",
    "receipt_malformed",
    "receipt_oversized",
    "receipt_schema_mismatch",
    "receipt_identity_mismatch",
    "receipt_vocabulary_invalid",
    "receipt_bounds_invalid",
    "receipt_inconsistent",
})


def _attempt_text(value: object, pattern: re.Pattern[str]) -> str | None:
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    return None


def _attempt_unknown(spec: dict[str, Any], reason: str) -> dict[str, Any]:
    """Worker-authored receipt that claims only the identity pinned in the spec."""
    return {
        "schema_id": REASONING_CONTEXT_ATTEMPT_SCHEMA_ID,
        "request_id": _attempt_text(spec.get("request_id"), _ATTEMPT_IDENTITY_RE),
        "repo_id": _attempt_text(spec.get("repo_id"), _ATTEMPT_IDENTITY_RE),
        "requested_model": _attempt_text(spec.get("model"), EDITOR_REQUESTED_MODEL_RE),
        "host_model": None,
        "requested_profile": None,
        "send_state": "unknown",
        "send_turn_count": None,
        "provider_request_acknowledged": None,
        "option_status": "unknown",
        "option_key": None,
        "option_value": None,
        "context_capacity_tokens": None,
        "context_capacity_source": "unknown",
        "provider_internal_state": "unknown",
        "unknown_reason": reason,
    }


def _attempt_refusal(raw: object, spec: dict[str, Any]) -> str | None:
    """Typed reason a host receipt must not be surfaced as evidence, else None."""
    if not isinstance(raw, dict):
        return "receipt_malformed"
    try:
        encoded = json.dumps(raw, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return "receipt_malformed"
    if len(encoded) > _ATTEMPT_MAX_BYTES:
        return "receipt_oversized"
    if "schema_id" in raw and raw["schema_id"] != REASONING_CONTEXT_ATTEMPT_SCHEMA_ID:
        return "receipt_schema_mismatch"
    host_model = raw.get("host_model")
    if set(raw) != set(_ATTEMPT_FIELDS) or not isinstance(host_model, dict):
        return "receipt_malformed"
    unknown_reason = raw["unknown_reason"]
    if (
        set(host_model) != set(_ATTEMPT_HOST_MODEL_FIELDS)
        or any(value is not None and not isinstance(value, str) for value in host_model.values())
        or not all(isinstance(raw[key], str) for key in _ATTEMPT_VOCABULARY_FIELDS)
        or not isinstance(raw["provider_request_acknowledged"], bool)
        or not (unknown_reason is None or isinstance(unknown_reason, str))
    ):
        return "receipt_malformed"
    pinned = {
        "request_id": _attempt_text(spec.get("request_id"), _ATTEMPT_IDENTITY_RE),
        "repo_id": _attempt_text(spec.get("repo_id"), _ATTEMPT_IDENTITY_RE),
        "requested_model": _attempt_text(spec.get("model"), EDITOR_REQUESTED_MODEL_RE),
    }
    if any(value is None or raw[key] != value for key, value in pinned.items()):
        return "receipt_identity_mismatch"
    if (
        raw["requested_profile"] not in _ATTEMPT_PROFILES
        or raw["send_state"] not in _ATTEMPT_SEND_STATES
        or raw["option_status"] not in _ATTEMPT_OPTION_STATUSES
        or raw["context_capacity_source"] not in _ATTEMPT_CAPACITY_SOURCES
        or raw["provider_internal_state"] != "unknown"
        or (unknown_reason is not None and unknown_reason not in _ATTEMPT_HOST_UNKNOWN_REASONS)
    ):
        return "receipt_vocabulary_invalid"
    count = raw["send_turn_count"]
    tokens = raw["context_capacity_tokens"]
    if (
        type(count) is not int
        or not 0 <= count <= _ATTEMPT_MAX_SEND_TURNS
        or (
            tokens is not None
            and (type(tokens) is not int or not 1 <= tokens <= _ATTEMPT_MAX_CAPACITY_TOKENS)
        )
        or any(
            value is not None and _attempt_text(value, _ATTEMPT_OPTION_TOKEN_RE) is None
            for value in (raw["option_key"], raw["option_value"])
        )
        or any(
            value is not None and _attempt_text(value, _ATTEMPT_LABEL_RE) is None
            for value in host_model.values()
        )
    ):
        return "receipt_bounds_invalid"
    # run() only reaches this on success, which proves an acknowledged send happened.
    applied = raw["option_status"] == "applied"
    if (
        raw["send_state"] != "sent"
        or count < 1
        or raw["provider_request_acknowledged"] is not True
        or raw["option_status"] == "not_sent"
        or (raw["option_key"] is not None) != applied
        or (raw["option_value"] is not None) != applied
        or (raw["option_status"] == "unknown") != (unknown_reason is not None)
        or (tokens is None) != (raw["context_capacity_source"] == "unknown")
    ):
        return "receipt_inconsistent"
    return None


def _reasoning_context_attempt_result(
    response: dict[str, Any], spec: dict[str, Any],
) -> dict[str, Any]:
    """Verified host receipt rebuilt from scalars, or a typed unknown; never raises."""
    raw = response.get("reasoning_context_attempt")
    reason = "receipt_absent" if raw is None else _attempt_refusal(raw, spec)
    if reason is not None:
        return _attempt_unknown(spec, reason)
    verified = {key: raw[key] for key in _ATTEMPT_FIELDS}
    verified["host_model"] = {key: raw["host_model"][key] for key in _ATTEMPT_HOST_MODEL_FIELDS}
    return verified


_TOOL_REQUEST_SCHEMA_ID = "aiworkhub.vscode_lm.tool_request.v1"
_RANGE_PROTOCOL_VERBS = frozenset({"replace_range", "edit", "v3_range"})
_CREATE_PROTOCOL_VERBS = frozenset({"create", "v3_create"})
_STAGE_TOOL_NAMES = frozenset({
    "aiworkhub_manager_semantic_edit_stage",
    "aiworkhub_worker_semantic_edit_stage",
})


def _protocol_kind(item: dict[str, Any]) -> str:
    """Map operation/action aliases onto range or create, or a conflict."""

    kinds: list[str] = []
    for key in ("operation", "action"):
        raw = item.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        verb = raw.strip()
        if verb in _RANGE_PROTOCOL_VERBS:
            kinds.append("range")
        elif verb in _CREATE_PROTOCOL_VERBS:
            kinds.append("create")
        else:
            return "unknown"
    if not kinds:
        return ""
    if len(set(kinds)) != 1:
        return "conflict"
    return kinds[0]


def _protocol_path(item: dict[str, Any]) -> str | None:
    for key in ("path", "file_path"):
        raw = item.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw
    return None


def _coerce_protocol_range(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    if "start_line" in normalized:
        normalized["start_line"] = coerce_protocol_line(normalized.get("start_line"))
    if "end_line" in normalized:
        normalized["end_line"] = coerce_protocol_line(normalized.get("end_line"))
    return normalized


def _flat_protocol_range(item: dict[str, Any]) -> dict[str, Any] | None:
    if "start_line" not in item or "end_line" not in item:
        return None
    if not isinstance(item.get("new"), str):
        return None
    range_item: dict[str, Any] = {
        "start_line": coerce_protocol_line(item.get("start_line")),
        "end_line": coerce_protocol_line(item.get("end_line")),
        "new": item["new"],
    }
    if "preserve_trailing_newline" in item:
        range_item["preserve_trailing_newline"] = item["preserve_trailing_newline"]
    if isinstance(item.get("fragment_sha256"), str):
        range_item["fragment_sha256"] = item["fragment_sha256"]
    return range_item


def _normalize_protocol_edit_item(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Turn one staged edit into a v3 edit or create entry.

    ``action`` and ``operation`` are the same verb.  ``file_path`` is ``path``.
    A flat ``start_line``/``end_line``/``new`` stage becomes ``ranges``.  The
    caller still verifies the file hash; this function never invents one.
    """

    kind = _protocol_kind(item)
    path = _protocol_path(item)
    ranges = item.get("ranges")
    if isinstance(ranges, list) and kind != "create":
        edit = dict(item)
        if path is not None:
            edit["path"] = path
        edit["ranges"] = [
            _coerce_protocol_range(range_item) if isinstance(range_item, dict) else range_item
            for range_item in ranges
        ]
        return "edit", edit
    if kind == "create" and path is not None:
        content = item.get("content")
        if not isinstance(content, str):
            content = item.get("new")
        if isinstance(content, str):
            return "create", {"path": path, "content": content}
    if kind in {"", "range"} and path is not None:
        flat = _flat_protocol_range(item)
        if flat is not None:
            edit: dict[str, Any] = {"path": path, "ranges": [flat]}
            if "current_sha256" in item:
                edit["current_sha256"] = item["current_sha256"]
            return "edit", edit
    edit = dict(item)
    if path is not None:
        edit["path"] = path
    return "edit", edit


def _looks_like_flat_stage(item: dict[str, Any]) -> bool:
    if item.get("replacements") is not None or item.get("files") is not None:
        return False
    if isinstance(item.get("ranges"), list):
        return False
    kind, entry = _normalize_protocol_edit_item(item)
    if kind == "create":
        return True
    return isinstance(entry.get("ranges"), list) and bool(entry["ranges"])


def _normalize_staged_final_envelope(edit: dict[str, Any]) -> dict[str, Any]:
    """Make a valid staged edit a v3 final envelope.

    Text-protocol workers send ``action`` where the final schema wants
    ``operation``-shaped ranges, and line numbers as strings.  That mismatch
    is not a hash failure: hashes, out-of-file ranges, and overlaps stay
    fail-closed after this shape normalization.  A valid stage must not die
    as ``final_edit_invalid``.
    """

    if not isinstance(edit, dict):
        return edit
    payload = edit
    if edit.get("schema_id") == _TOOL_REQUEST_SCHEMA_ID:
        inner = edit.get("input")
        name = edit.get("name")
        if not isinstance(inner, dict):
            return edit
        named_stage = isinstance(name, str) and name in _STAGE_TOOL_NAMES
        if not named_stage and not _looks_like_flat_stage(inner):
            return edit
        payload = dict(inner)
        if not isinstance(payload.get("summary"), str) and isinstance(edit.get("summary"), str):
            payload["summary"] = edit["summary"]
    schema = payload.get("schema_id")
    if schema in {EDIT_RESPONSE_SCHEMA_ID_V1, EDIT_RESPONSE_SCHEMA_ID_V2}:
        return payload
    edits = payload.get("edits")
    if not isinstance(edits, list):
        if not _looks_like_flat_stage(payload):
            return payload
        kind, entry = _normalize_protocol_edit_item(payload)
        summary = payload.get("summary") if isinstance(payload.get("summary"), str) else "staged semantic edit"
        if kind == "create":
            return {
                "schema_id": EDIT_RESPONSE_SCHEMA_ID,
                "summary": summary,
                "edits": [],
                "creates": [entry],
            }
        return {
            "schema_id": EDIT_RESPONSE_SCHEMA_ID,
            "summary": summary,
            "edits": [entry],
            "creates": [],
        }
    next_edits: list[Any] = []
    extra_creates: list[dict[str, Any]] = []
    for item in edits:
        if not isinstance(item, dict):
            next_edits.append(item)
            continue
        kind, entry = _normalize_protocol_edit_item(item)
        if kind == "create":
            extra_creates.append(entry)
            continue
        next_edits.append(entry)
    creates = payload.get("creates")
    next_creates = list(creates) if isinstance(creates, list) else []
    next_creates.extend(extra_creates)
    normalized = dict(payload)
    normalized["edits"] = next_edits
    normalized["creates"] = next_creates
    if normalized.get("schema_id") not in {
        EDIT_RESPONSE_SCHEMA_ID,
        EDIT_RESPONSE_SCHEMA_ID_V1,
        EDIT_RESPONSE_SCHEMA_ID_V2,
    }:
        normalized["schema_id"] = EDIT_RESPONSE_SCHEMA_ID
    return normalized


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
    activity_reader = None
    capture_rejected = False
    if spec.get("activity_capture") is True and spec.get("activity_path"):
        try:
            activity_home = Path(str(spec.get("workspace_home") or "")).absolute()
            if (activity_home != spec_path.absolute().parent
                    or activity_home != response_path.absolute().parent
                    or activity_home.parent != workspace.parent):
                raise ActivityCaptureError("vscode_lm_activity_attachment_mismatch")
            activity_reader = ActivityReader(
                Path(str(spec["activity_path"])), activity_home,
                str(spec.get("request_id") or ""), str(spec.get("repo_id") or ""),
            )
        except ActivityCaptureError:
            capture_rejected = True

    def capture_status() -> str:
        if capture_rejected or (response is not None
                and response.get("activity_capture_status") == "unavailable"):
            return "unavailable"
        return activity_reader.availability if activity_reader else "unknown"

    def drain_activity(*, final: bool = False) -> None:
        nonlocal activity_reader, capture_rejected
        rejected_now = False
        if activity_reader is not None:
            try:
                activities = activity_reader.drain(final=final)
            except ActivityCaptureError:
                # Optional capture fails closed on payload, NOT on terminal authority.
                activity_reader = None
                capture_rejected = rejected_now = True
            else:
                for activity in activities:
                    print(json.dumps(activity, ensure_ascii=True, sort_keys=True), flush=True)
        if final or rejected_now:
            print(json.dumps({
                "type": "aiworkhub_tool_activity", "kind": "status",
                "capture_status": capture_status(),
                "request_id": str(spec.get("request_id") or ""),
                "repo_id": str(spec.get("repo_id") or ""),
                "final_drain": final,
                "rejected_capture": capture_rejected,
                "capture_end": activity_reader.capture_end if activity_reader else False,
                "dropped_events": activity_reader.dropped_events if activity_reader else None,
            }, ensure_ascii=True, sort_keys=True), flush=True)

    while time.monotonic() < deadline:
        drain_activity()
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
                    owner_uid=current_user_uid(),
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
        drain_activity(final=True)
        raise RuntimeError("vscode_lm_response_timeout")

    drain_activity(final=True)
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
            owner_uid=current_user_uid(),
            defer_transient=False,
        )
    if response.get("error"):
        # The host-owned field alone -- never ``text`` or any model output.
        balance_error = provider_balance_error(response.get("error"))
        if balance_error is not None:
            raise VscodeLmProviderRefusal(balance_error)
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
    if isinstance(edit, dict):
        edit = _normalize_staged_final_envelope(edit)
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
        # Existing files are edited through the coordinator's authenticated
        # worker prepare/apply session before this final handoff. Python may
        # materialize new-file exceptions only, never unaudited replacements.
        for relative, _content in planned:
            if relative not in create_paths and (workspace / relative).exists():
                raise RuntimeError(
                    f"vscode_lm_existing_edit_requires_authenticated_apply:{relative}"
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
        "reasoning_context_attempt": _reasoning_context_attempt_result(response, spec),
        "visible_tool_activity_status": capture_status(),
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
        if isinstance(exc, VscodeLmProviderRefusal):
            failure["provider_error"] = exc.provider_error
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
