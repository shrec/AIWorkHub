"""Isolated worker endpoint for the VS Code Language Model bridge."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .vscode_lm_bridge import EDIT_RESPONSE_SCHEMA_ID, RESPONSE_SCHEMA_ID


def _load_json(path: Path, *, max_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise RuntimeError("bridge_document_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("bridge_document_not_object")
    return value


def _strip_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        first_newline = value.find("\n")
        if first_newline >= 0:
            value = value[first_newline + 1 : -3].strip()
    return value


def _relative_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise RuntimeError("bridge_output_path_invalid")
    value = raw.strip().replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parts[0] == ".git":
        raise RuntimeError(f"bridge_output_path_escape:{value}")
    return path.as_posix()


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _write_atomic(workspace: Path, relative: str, content: str) -> None:
    target = (workspace / relative).resolve(strict=False)
    if target != workspace and workspace not in target.parents:
        raise RuntimeError(f"bridge_output_path_escape:{relative}")
    if target.is_symlink():
        raise RuntimeError(f"bridge_output_symlink:{relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        # ``mkstemp`` creates the file with owner-only permissions (0600).
        # Do not call fchmod here: isolated workers deliberately run under a
        # seccomp profile that rejects metadata-changing syscalls, including
        # fchmod.  The redundant chmod therefore made a valid GLM response
        # fail with EPERM before its first allowed output could be written.
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    finally:
        os.close(fd)
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def run(spec_path: Path) -> dict[str, Any]:
    spec = _load_json(spec_path)
    if spec.get("schema_id") != "aiworkhub.vscode_lm.worker_spec.v1":
        raise RuntimeError("bridge_worker_spec_schema_mismatch")
    workspace = Path(str(spec.get("workspace_path") or "")).resolve(strict=True)
    response_path = Path(str(spec.get("response_path") or ""))
    timeout_seconds = max(30, min(int(spec.get("timeout_seconds") or 7200), 86_400))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if response_path.is_file():
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("vscode_lm_response_timeout")

    response = _load_json(response_path)
    if response.get("schema_id") != RESPONSE_SCHEMA_ID:
        raise RuntimeError("vscode_lm_response_schema_mismatch")
    if response.get("request_id") != spec.get("request_id"):
        raise RuntimeError("vscode_lm_response_identity_mismatch")
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
    if not isinstance(edit, dict) or edit.get("schema_id") != EDIT_RESPONSE_SCHEMA_ID:
        raise RuntimeError("vscode_lm_edit_response_schema_mismatch")
    files = edit.get("files")
    if not isinstance(files, list):
        raise RuntimeError("vscode_lm_edit_response_files_invalid")
    allowed = [str(value) for value in spec.get("allowed_writes") or []]
    planned: list[tuple[str, str]] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise RuntimeError("vscode_lm_edit_response_file_invalid")
        relative = _relative_path(item.get("path"))
        if not _matches(relative, allowed):
            raise RuntimeError(f"vscode_lm_output_out_of_scope:{relative}")
        planned.append((relative, item["content"]))
    # Scope-validate the complete response before the first mutation.  This
    # prevents a mixed valid/invalid model response from partially applying.
    written: list[str] = []
    for relative, content in planned:
        _write_atomic(workspace, relative, content)
        written.append(relative)
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": str(edit.get("summary") or "GLM VS Code worker completed"),
        "model": response.get("model"),
        "changed_paths": sorted(set(written)),
        "project_context_receipt": str(spec.get("project_context_receipt") or ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--spec",
        default=str(Path.home() / ".aiworkhub_vscode_lm_worker.json"),
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


if __name__ == "__main__":
    raise SystemExit(main())
