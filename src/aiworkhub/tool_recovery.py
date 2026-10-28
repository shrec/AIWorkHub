"""Bounded recovery guidance for hallucinated MCP tool names."""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Iterable
from typing import Any


MAX_REQUESTED_CHARS = 128
MAX_SUGGESTIONS = 3
MAX_PROFILES = 8
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.:/-]+")


def _bounded_name(value: Any) -> str:
    text = str(value or "")[:MAX_REQUESTED_CHARS]
    match = _SAFE_NAME.fullmatch(text)
    return text if match else "<invalid>"


def unknown_tool_message(
    requested: Any,
    registered: Iterable[str],
    *,
    active_profile: str | None = None,
    exposing_profiles: Iterable[str] = (),
) -> str:
    """Return deterministic, token-bounded guidance without executing aliases.

    With the ``active_profile`` a known tool was called on and the
    ``exposing_profiles`` that do serve it, the guidance names both instead of
    the generic unknown-tool recovery.
    """

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
    payload: dict[str, Any] = {
        "error": "unknown_tool",
        "requested": name,
        "retryable": True,
        "recovery": "call tools/list and retry with one exact registered name",
        "suggestions": suggestions,
    }
    exposed = [
        profile
        for profile in dict.fromkeys(_bounded_name(raw) for raw in exposing_profiles)
        if profile != "<invalid>"
    ][:MAX_PROFILES]
    if active_profile is not None and exposed:
        active = _bounded_name(active_profile)
        payload["reason"] = "not_in_active_profile"
        payload["active_profile"] = active
        payload["exposed_in"] = exposed
        payload["recovery"] = (
            f"{name} is not exposed on the {active} profile; profiles exposing it: "
            f"{', '.join(exposed)}. Select one with AIWORKHUB_MCP_SURFACE, or call "
            f"tools/list and retry with one exact name the {active} profile lists"
        )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


__all__ = ["unknown_tool_message"]
