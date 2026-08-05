"""Bounded recovery guidance for hallucinated MCP tool names."""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Iterable
from typing import Any


MAX_REQUESTED_CHARS = 128
MAX_SUGGESTIONS = 3
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.:/-]+")


def _bounded_name(value: Any) -> str:
    text = str(value or "")[:MAX_REQUESTED_CHARS]
    match = _SAFE_NAME.fullmatch(text)
    return text if match else "<invalid>"


def unknown_tool_message(requested: Any, registered: Iterable[str]) -> str:
    """Return deterministic, token-bounded guidance without executing aliases."""

    name = _bounded_name(requested)
    candidates = sorted({
        candidate
        for raw in registered
        if (candidate := _bounded_name(raw)) != "<invalid>"
    })
    suggestions = difflib.get_close_matches(
        name,
        candidates,
        n=MAX_SUGGESTIONS,
        cutoff=0.45,
    )
    payload = {
        "error": "unknown_tool",
        "requested": name,
        "retryable": True,
        "recovery": "call tools/list and retry with one exact registered name",
        "suggestions": suggestions,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


__all__ = ["unknown_tool_message"]
