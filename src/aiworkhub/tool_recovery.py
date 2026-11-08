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
# NF-2026-01940: ``successor_task_id`` is the name the supersede continuation
# reply and every other successor-task surface use for what
# ``aiworkhub_task_supersede`` takes as ``by``.
_ARGUMENT_ALIASES = (
    ("limit", "budget"),
    ("id", "needfix_id"),
    ("nf_id", "needfix_id"),
    ("successor_task_id", "by"),
)
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


def _signature_parameters(function: Any) -> list[inspect.Parameter] | None:
    try:
        return list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        return None


def _accepted_text(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_ACCEPTED_NAMES])
    if len(names) > MAX_ACCEPTED_NAMES:
        shown += f", ... (+{len(names) - MAX_ACCEPTED_NAMES} more)"
    return shown


def _missing_required(
    parameters: list[inspect.Parameter], resolved: dict[str, Any]
) -> list[str]:
    """Every parameter without a default, other than *args/**kwargs, absent from ``resolved``."""

    return [
        parameter.name
        for parameter in parameters
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        and parameter.name not in resolved
    ]


def _missing_text(missing: list[str]) -> str:
    return f"[{_accepted_text(missing)}]"


def resolve_tool_arguments(
    function: Any,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Apply the measured aliases, then name what a tool accepts.

    Returns ``(arguments, None)`` when every name is accepted, otherwise
    ``(arguments, message)`` where the message keeps the ``unexpected_arguments:``
    prefix and, when required parameters are also absent, ends with
    ``missing_required:``. An alias is only mapped when the signature has the
    target and lacks the alias; alias plus target, or two aliases for one
    target, stay errors. A call that only misses required parameters returns
    ``None``; ``missing_required_refusal`` names those.
    """

    signature = _signature_parameters(function)
    if signature is None:
        return arguments, None
    parameters = [parameter.name for parameter in signature]
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
    candidates = [name for name in names if name not in resolved]
    hints = []
    for name in unexpected[:MAX_UNEXPECTED]:
        close = difflib.get_close_matches(
            name[:MAX_REQUESTED_CHARS], candidates, n=1, cutoff=ARGUMENT_CUTOFF
        )
        if close:
            hints.append(f"{_bounded_name(name)} -> {close[0]}")
    message = f"unexpected_arguments:{shown_unexpected}; accepted: {_accepted_text(names)}"
    if hints:
        message += f"; did_you_mean: {', '.join(hints)}"
    if conflicts:
        message += f"; alias_conflict: {'; '.join(conflicts)}"
    missing = _missing_required(signature, resolved)
    if missing:
        message += f"; missing_required: {_missing_text(missing)}"
    return resolved, message


def missing_required_refusal(
    function: Any,
    resolved: dict[str, Any],
    original: dict[str, Any],
) -> str | None:
    """Name every missing required parameter in one message, or return ``None``.

    ``resolved`` is what ``resolve_tool_arguments`` returned and ``original``
    what the caller sent; keys of ``original`` that it rewrote to a target are
    reported as ``alias -> target`` so the model sees which alias was applied.
    """

    signature = _signature_parameters(function)
    if signature is None:
        return None
    missing = _missing_required(signature, resolved)
    if not missing:
        return None
    names = sorted(
        parameter.name for parameter in signature
        if parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
    )
    message = f"missing_required_arguments:{_missing_text(missing)}; accepted: {_accepted_text(names)}"
    applied = [
        f"{alias} -> {target}"
        for alias, target in _ARGUMENT_ALIASES
        if alias in original
        and alias not in resolved
        and target in resolved
        and target not in original
    ]
    if applied:
        message += f"; aliases_applied: {', '.join(applied)}"
    return message


__all__ = ["missing_required_refusal", "resolve_tool_arguments", "unknown_tool_message"]
