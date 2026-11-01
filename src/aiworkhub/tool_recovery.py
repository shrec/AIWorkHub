"""Bounded recovery guidance for hallucinated MCP tool names."""

from __future__ import annotations

import difflib
import inspect
import json
import re
from collections.abc import Iterable
from typing import Any


MAX_REQUESTED_CHARS = 128
MAX_SUGGESTIONS = 3
MAX_PROFILES = 8
MAX_ACCEPTED_NAMES = 40
MAX_UNEXPECTED = 10
ARGUMENT_CUTOFF = 0.6
_ARGUMENT_ALIASES = (("limit", "budget"), ("id", "needfix_id"), ("nf_id", "needfix_id"))
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


def resolve_tool_arguments(
    function: Any,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Apply the two measured aliases, then name what a tool accepts.

    Returns ``(arguments, None)`` when every name is accepted, otherwise
    ``(arguments, message)`` where the message keeps the ``unexpected_arguments:``
    prefix. An alias is only mapped when the signature has the target and lacks
    the alias; alias plus target, or two aliases for one target, stay errors.
    """

    try:
        parameters = list(inspect.signature(function).parameters)
    except (TypeError, ValueError):
        return arguments, None
    accepted = set(parameters)
    claims: dict[str, list[str]] = {}
    for alias, target in _ARGUMENT_ALIASES:
        if alias in arguments and alias not in accepted and target in accepted:
            claims.setdefault(target, []).append(alias)
    resolved = dict(arguments)
    conflicts: list[str] = []
    for target, aliases in claims.items():
        if target in arguments or len(aliases) > 1:
            conflicts.append(
                f"{' and '.join(aliases)} map to {target}"
                + (" which was also passed" if target in arguments else "")
                + f"; pass only {target}"
            )
            continue
        resolved[target] = resolved.pop(aliases[0])
    unexpected = sorted(str(name) for name in resolved if name not in accepted)
    if not unexpected:
        return resolved, None
    shown_unexpected = [_bounded_name(name) for name in unexpected[:MAX_UNEXPECTED]]
    names = sorted(accepted)
    shown = ", ".join(names[:MAX_ACCEPTED_NAMES])
    if len(names) > MAX_ACCEPTED_NAMES:
        shown += f", ... (+{len(names) - MAX_ACCEPTED_NAMES} more)"
    candidates = [name for name in names if name not in resolved]
    hints = []
    for name in unexpected[:MAX_UNEXPECTED]:
        close = difflib.get_close_matches(
            name[:MAX_REQUESTED_CHARS], candidates, n=1, cutoff=ARGUMENT_CUTOFF
        )
        if close:
            hints.append(f"{_bounded_name(name)} -> {close[0]}")
    message = f"unexpected_arguments:{shown_unexpected}; accepted: {shown}"
    if hints:
        message += f"; did_you_mean: {', '.join(hints)}"
    if conflicts:
        message += f"; alias_conflict: {'; '.join(conflicts)}"
    return resolved, message


__all__ = ["resolve_tool_arguments", "unknown_tool_message"]
