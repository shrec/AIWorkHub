"""Bounded request-owned visible tool journal; never completion authority.

Recognized credential keys, assignments, bearer/API tokens and URL credentials
are redacted again at relay. Arbitrary secrets cannot reliably be detected.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .platform_io import current_user_uid

ACTIVITY_SCHEMA = "aiworkhub.vscode_lm.activity.v1"
ACTIVITY_FILENAME = ".aiworkhub_vscode_lm_activity.jsonl"
MAX_JOURNAL_BYTES = 1024 * 1024
MAX_ROW_BYTES = 16 * 1024
MAX_PREVIEW_BYTES = 4096
_SECRET = r"authorization|api[_-]?key|token|secret|password|credential|cookie|prompt|model_context"
_KEY = re.compile(_SECRET, re.I)
_ASSIGNMENT = re.compile(
    rf"((?:{_SECRET})[\w-]*[\"']?\s*[:=]\s*)"
    r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s&,}]+)''', re.I
)
_ALLOWED = {
    "schema_id", "request_id", "repo_id", "sequence", "kind", "call_id", "tool_name",
    "tool_state", "tool_transport", "updated_at", "input_preview", "output_preview",
    "preview_truncated", "elapsed_ms", "error_code", "capture_status", "dropped_events",
    "capture_end", "redaction_coverage",
}


class ActivityCaptureError(RuntimeError):
    """Only the optional activity capture is rejected, never terminal authority."""


def _redact_json(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        return {key: "[redacted]" if _KEY.search(str(key)) else _redact_json(item, depth + 1)
                for key, item in list(value.items())[:40]}
    if isinstance(value, list):
        return [_redact_json(item, depth + 1) for item in value[:40]]
    return redact_text(value, parse_json=False) if isinstance(value, str) else value


def redact_text(value: str, *, parse_json: bool = True) -> str:
    """Recognized secrets only, before stdout; preserve valid bounded Unicode."""
    text = str(value)
    if parse_json and text.lstrip().startswith(("{", "[")):
        try:
            text = json.dumps(_redact_json(json.loads(text)), ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
    text = re.sub(r"\b(?:Bearer|Basic)\s+[^\s\"']+", "Bearer [redacted]", text, flags=re.I)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", text)
    text = re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@", r"\1[redacted]@", text)
    text = re.sub(
        r'''(--(?:authorization|api[_-]?key|token|secret|password|credential|cookie)(?:\s+|=))'''
        r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s]+)''',
        r"\1[redacted]", text, flags=re.I,
    )
    text = _ASSIGNMENT.sub(lambda match: match[1] + "[redacted]", text)
    return text.encode("utf-8", errors="replace")[:MAX_PREVIEW_BYTES].decode("utf-8", errors="ignore")


class ActivityReader:
    """Single reader cursor, bounded by the producer cap, replay/identity fail closed."""

    def __init__(self, path: Path, home: Path, request_id: str, repo_id: str) -> None:
        if path.absolute() != home.absolute() / ACTIVITY_FILENAME:
            raise ActivityCaptureError("vscode_lm_activity_path_invalid")
        if home.name != "home" or home.parent.name != request_id or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ActivityCaptureError("vscode_lm_activity_path_identity_invalid")
        self.path, self.home = path.absolute(), home.absolute()
        self.request_id, self.repo_id = request_id, repo_id
        self.offset = 0
        self.sequence = 0
        self.identity: tuple[int, int] | None = None
        self.availability = "unknown"
        self.capture_end = False
        self.dropped_events: int | None = None

    @staticmethod
    def _private(metadata: os.stat_result, *, directory: bool = False) -> None:
        uid = current_user_uid()
        regular = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
        if not regular or stat.S_ISLNK(metadata.st_mode) or (not directory and metadata.st_nlink != 1) or (os.name != "nt" and (
            metadata.st_mode & 0o077 or (uid is not None and metadata.st_uid != uid)
        )):
            raise ActivityCaptureError("vscode_lm_activity_owner_invalid")

    def _row(self, raw: bytes, sequence: int) -> dict[str, Any]:
        if len(raw) > MAX_ROW_BYTES:
            raise ActivityCaptureError("vscode_lm_activity_row_limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ActivityCaptureError("vscode_lm_activity_invalid_json") from exc
        if not isinstance(value, dict) or set(value) - _ALLOWED:
            raise ActivityCaptureError("vscode_lm_activity_metadata_invalid")
        # Validate every allowed field before membership, encoding or relay. Optional
        # fields stay optional for legacy journals; present values must be scalars.
        for key, item in value.items():
            expected = bool if key in {"capture_end", "preview_truncated"} else (
                int if key in {"sequence", "elapsed_ms", "dropped_events"} else str
            )
            if type(item) is not expected:
                raise ActivityCaptureError("vscode_lm_activity_metadata_invalid")
            if expected is str:
                try:
                    size = len(item.encode("utf-8"))
                except UnicodeError as exc:
                    raise ActivityCaptureError("vscode_lm_activity_unicode_invalid") from exc
                if size > MAX_PREVIEW_BYTES:
                    raise ActivityCaptureError("vscode_lm_activity_metadata_limit")
        for key, choices in (
            ("tool_transport", {"native", "emulated", "unknown"}),
            ("tool_state", {"started", "completed", "failed"}),
            ("capture_status", {"available", "limited", "unavailable"}),
        ):
            if key in value and value[key] not in choices:
                raise ActivityCaptureError("vscode_lm_activity_metadata_invalid")
        if not isinstance(value, dict) or set(value) - _ALLOWED or (
            value.get("schema_id") != ACTIVITY_SCHEMA or value.get("request_id") != self.request_id
            or value.get("repo_id") != self.repo_id or type(value.get("sequence")) is not int
            or value["sequence"] != sequence or value.get("kind") not in {"tool", "status"}
        ):
            raise ActivityCaptureError("vscode_lm_activity_identity_or_sequence_invalid")
        if value["kind"] == "tool":
            if value.get("tool_state") not in {"started", "completed", "failed"} or not (
                isinstance(value.get("call_id"), str) and 0 < len(value["call_id"]) <= 200
            ) or not isinstance(value.get("tool_name"), str) or len(value["tool_name"]) > 200:
                raise ActivityCaptureError("vscode_lm_activity_tool_invalid")
        elif value.get("capture_status") not in {"available", "limited", "unavailable"}:
            raise ActivityCaptureError("vscode_lm_activity_status_invalid")
        for key in ("dropped_events", "elapsed_ms"):
            if key in value and (type(value[key]) is not int or not 0 <= value[key] <= 86_400_000):
                raise ActivityCaptureError("vscode_lm_activity_count_invalid")
        for key in ("input_preview", "output_preview", "error_code"):
            if key in value:
                if not isinstance(value[key], str) or len(value[key].encode("utf-8")) > MAX_PREVIEW_BYTES:
                    raise ActivityCaptureError("vscode_lm_activity_preview_invalid")
                value[key] = redact_text(value[key])
        for key, item in value.items():
            if isinstance(item, str):
                value[key] = redact_text(item)
        if "capture_end" in value and type(value["capture_end"]) is not bool:
            raise ActivityCaptureError("vscode_lm_activity_end_invalid")
        if "preview_truncated" in value and type(value["preview_truncated"]) is not bool:
            raise ActivityCaptureError("vscode_lm_activity_preview_invalid")
        return {**value, "type": "aiworkhub_tool_activity"}

    def drain(self, *, final: bool = False) -> list[dict[str, Any]]:
        try:
            return self._drain(final=final)
        except OSError as exc:
            raise ActivityCaptureError("vscode_lm_activity_io_unavailable") from exc

    def _drain(self, *, final: bool) -> list[dict[str, Any]]:
        # Request ancestors may not redirect the owned home through a link/junction.
        for ancestor in (self.home, *self.home.parents):
            if ancestor.is_symlink() or ancestor.resolve() != ancestor:
                raise ActivityCaptureError("vscode_lm_activity_path_escape")
        self._private(self.home.lstat(), directory=True)
        try:
            initial = self.path.lstat()
        except FileNotFoundError:
            if self.identity is not None:
                raise ActivityCaptureError("vscode_lm_activity_identity_changed")
            return []
        self._private(initial)
        if initial.st_size > MAX_JOURNAL_BYTES or initial.st_size < self.offset:
            raise ActivityCaptureError("vscode_lm_activity_journal_limit_or_truncated")
        flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0)) | int(getattr(os, "O_BINARY", 0))
        fd = os.open(self.path, flags)
        try:
            opened = os.fstat(fd)
            self._private(opened)
            identity = (opened.st_dev, opened.st_ino)
            if identity != (initial.st_dev, initial.st_ino) or (
                self.identity is not None and identity != self.identity
            ):
                raise ActivityCaptureError("vscode_lm_activity_identity_changed")
            os.lseek(fd, self.offset, os.SEEK_SET)
            raw = bytearray()
            # IO reads release the GIL; this one request cursor is ordered by contract.
            while len(raw) <= MAX_JOURNAL_BYTES:
                chunk = os.read(fd, min(65536, MAX_JOURNAL_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(fd)
            named = self.path.lstat()
            self._private(after)
            self._private(named)
            if (named.st_dev, named.st_ino) != identity or after.st_size < initial.st_size:
                raise ActivityCaptureError("vscode_lm_activity_identity_changed")
            if after.st_size > MAX_JOURNAL_BYTES or self.offset + len(raw) > MAX_JOURNAL_BYTES:
                raise ActivityCaptureError("vscode_lm_activity_journal_limit")
        finally:
            os.close(fd)
        boundary = raw.rfind(b"\n") + 1
        if final and boundary != len(raw):
            raise ActivityCaptureError("vscode_lm_activity_incomplete")
        lines = bytes(raw[:boundary]).splitlines()
        # Validate the entire batch before exposing any foreign/replayed payload.
        events = [self._row(line, self.sequence + index + 1) for index, line in enumerate(lines)]
        self.identity = identity
        self.offset += boundary
        self.sequence += len(events)
        for event in events:
            if event["kind"] == "status":
                self.availability = event["capture_status"]
                self.capture_end = event.get("capture_end") is True
                self.dropped_events = event.get("dropped_events")
        return events
