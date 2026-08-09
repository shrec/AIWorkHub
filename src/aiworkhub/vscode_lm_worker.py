"""Isolated worker endpoint for the VS Code Language Model bridge."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from . import semantic_edit
from .vscode_lm_bridge import (
    EDIT_RESPONSE_SCHEMA_ID,
    EDIT_RESPONSE_SCHEMA_ID_V1,
    EDIT_RESPONSE_SCHEMA_ID_V2,
    RESPONSE_SCHEMA_ID,
    read_progress_receipt,
)


MAX_V2_PATHS = 128
MAX_V2_REPLACEMENTS_PER_FILE = 256
MAX_V2_REPLACEMENT_BYTES = 2 * 1024 * 1024
MAX_V2_FILE_BYTES = 16 * 1024 * 1024


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


_FIDELITY_WORD_MARKERS = re.compile(
    r"(?<![\w])(?:todo|fixme|xxx|placeholder)(?![\w])",
    re.IGNORECASE,
)
_FIDELITY_ANGLE_PLACEHOLDER = re.compile(
    r"^\s*<\s*(?:new\s+[\w.:-]+\s+code|test\s+file\s+content|"
    r"(?:full|complete)\s+file\s+content|insert\s+[^>]+\s+here)\s*>\s*$",
    re.IGNORECASE,
)
_FIDELITY_PHRASE_MARKERS = (
    "implementation omitted",
    "omitted for brevity",
    "rest of the code",
    "rest unchanged",
    "remainder unchanged",
    "unchanged code",
    "insert code here",
    "your code here",
)


def _check_edit_fidelity(
    old_text: str,
    new_text: str,
    *,
    path: str,
    operation: str,
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
    lowered = new_text.casefold()
    marker = _FIDELITY_WORD_MARKERS.search(new_text)
    phrase = next(
        (value for value in _FIDELITY_PHRASE_MARKERS if value in lowered),
        "",
    )
    if marker is not None or phrase or _FIDELITY_ANGLE_PLACEHOLDER.fullmatch(new_text):
        reason = "explicit_non_substantive_marker"
    elif (
        old_bytes >= 2048
        and new_bytes <= 64
        and not any(char.isalnum() for char in new_text)
    ):
        reason = "suspicious_shrink_without_alphanumeric_substance"
    else:
        return {
            "path": path,
            "operation": operation,
            "old_bytes": old_bytes,
            "new_bytes": new_bytes,
        }
    raise RuntimeError(
        "vscode_lm_edit_fidelity_rejected:"
        f"{reason}:{path}:{operation}:old_bytes={old_bytes}:new_bytes={new_bytes}"
    )


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


def _v1_planned_outputs(
    workspace: Path,
    edit: dict[str, Any],
    allowed: list[str],
) -> list[tuple[str, str]]:
    files = edit.get("files")
    if not isinstance(files, list):
        raise RuntimeError("vscode_lm_edit_response_files_invalid")
    planned: list[tuple[str, str]] = []
    seen: set[str] = set()
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
        _check_edit_fidelity(
            old_text,
            content,
            path=relative,
            operation="v1_file",
        )
        planned.append((relative, content))
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
        if hashlib.sha256(current_bytes).hexdigest() != expected_hash:
            raise RuntimeError(f"vscode_lm_edit_response_stale_hash:{relative}")
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
        if len(content.encode("utf-8")) > MAX_V2_FILE_BYTES:
            raise RuntimeError(f"vscode_lm_edit_response_file_too_large:{relative}")
        _check_edit_fidelity("", content, path=relative, operation="v2_create")
        planned.append((relative, content))
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
        current_bytes = target.read_bytes()
        if hashlib.sha256(current_bytes).hexdigest() != expected_hash:
            raise RuntimeError(f"vscode_lm_edit_response_stale_hash:{relative}")
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
        planned.append((relative, next_text))
        metrics.append({
            "path": relative,
            "file_bytes": len(current_bytes),
            "entry_index1": group["first_idx"],
            "entry_count": group["entry_count"],
            **edit_metrics,
        })

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
        if len(content.encode("utf-8")) > MAX_V2_FILE_BYTES:
            raise RuntimeError(f"vscode_lm_edit_response_file_too_large:{relative}")
        _check_edit_fidelity("", content, path=relative, operation="v3_create")
        planned.append((relative, content))
        metrics.append({
            "path": relative,
            "create": True,
            "replacement_bytes": len(content.encode("utf-8")),
            "whole_file_output_required": True,
            "token_savings_claimed": False,
        })
    return planned, metrics


def run(spec_path: Path) -> dict[str, Any]:
    spec = _load_json(spec_path)
    if spec.get("schema_id") != "aiworkhub.vscode_lm.worker_spec.v1":
        raise RuntimeError("bridge_worker_spec_schema_mismatch")
    workspace = Path(str(spec.get("workspace_path") or "")).resolve(strict=True)
    response_path = Path(str(spec.get("response_path") or ""))
    progress_path_raw = str(spec.get("progress_path") or "")
    progress_path = Path(progress_path_raw) if progress_path_raw else None
    timeout_seconds = max(30, min(int(spec.get("timeout_seconds") or 7200), 86_400))
    deadline = time.monotonic() + timeout_seconds
    last_progress_sequence = 0
    last_progress_signature: tuple[int, int] | None = None
    while time.monotonic() < deadline:
        if response_path.is_file():
            break
        if progress_path is not None and (
            progress_path.exists() or progress_path.is_symlink()
        ):
            try:
                progress_stat = progress_path.lstat()
                progress_signature = (progress_stat.st_mtime_ns, progress_stat.st_size)
            except OSError:
                progress_signature = None
            if progress_signature is not None and progress_signature != last_progress_signature:
                progress = read_progress_receipt(
                    progress_path,
                    str(spec.get("request_id") or ""),
                    str(spec.get("repo_id") or ""),
                    owner_uid=os.getuid() if hasattr(os, "getuid") else None,
                    previous_sequence=(last_progress_sequence or None),
                )
                if progress:
                    last_progress_sequence = int(progress["sequence"])
                    print(
                        json.dumps(
                            {
                                "type": "aiworkhub_progress",
                                "sequence": last_progress_sequence,
                                "phase": str(progress["phase"]),
                                "updated_at": str(progress["updated_at"]),
                            },
                            ensure_ascii=True,
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                last_progress_signature = progress_signature
        time.sleep(0.1)
    else:
        raise RuntimeError("vscode_lm_response_timeout")

    response = _load_json(response_path)
    if response.get("schema_id") != RESPONSE_SCHEMA_ID:
        raise RuntimeError("vscode_lm_response_schema_mismatch")
    if response.get("request_id") != spec.get("request_id"):
        raise RuntimeError("vscode_lm_response_identity_mismatch")
    if progress_path is not None:
        read_progress_receipt(
            progress_path,
            str(spec.get("request_id") or ""),
            str(spec.get("repo_id") or ""),
            owner_uid=os.getuid() if hasattr(os, "getuid") else None,
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
        if edit.get("schema_id") == EDIT_RESPONSE_SCHEMA_ID_V1:
            planned = _v1_planned_outputs(workspace, edit, allowed)
        elif edit.get("schema_id") == EDIT_RESPONSE_SCHEMA_ID_V2:
            create_paths = {
                str(value) for value in spec.get("create_paths") or [] if str(value)
            }
            planned = _v2_planned_outputs(workspace, edit, allowed, create_paths)
        else:
            create_paths = {
                str(value) for value in spec.get("create_paths") or [] if str(value)
            }
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
        _write_atomic(workspace, relative, content)
        readback_bytes = _target_path(workspace, relative).read_bytes()
        exp_bytes, exp_hash, exp_size = expected_content[relative]
        if readback_bytes != exp_bytes:
            raise RuntimeError(
                f"vscode_lm_edit_response_write_readback_mismatch:"
                f"{relative}:expected_sha256={exp_hash}:"
                f"expected_bytes={exp_size}:"
                f"actual_sha256="
                f"{hashlib.sha256(readback_bytes).hexdigest()}:"
                f"actual_bytes={len(readback_bytes)}"
            )
        written.append(relative)
        final_hashes[relative] = exp_hash
        final_sizes[relative] = exp_size
    for metric in semantic_metrics:
        metric_path = metric.get("path")
        if metric_path in final_hashes:
            metric["final_sha256"] = final_hashes[metric_path]
            metric["final_bytes"] = final_sizes[metric_path]
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
        print(json.dumps({"type": "result", "subtype": "error", "is_error": True, "error": str(exc)}))
        return 1
    receipt = str(result.pop("project_context_receipt", "") or "").strip()
    if receipt:
        print(receipt)
    print(json.dumps(result, ensure_ascii=False))
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
