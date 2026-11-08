"""Pure capability check: does a card's validation need a native toolchain or shell?

NF-2026-02087: ``derive_launch_identity`` auto-picked vscode_lm (the first
pinnable adapter) for cards whose validation runs cmake/nmake; the vscode_lm
lane cannot run native toolchains. This module is consulted BEFORE bridge
readiness so such cards fall through to claude_cli instead.
"""

from __future__ import annotations

import os
import shlex
from typing import Any, Mapping

NATIVE_TOOLCHAIN_EXECUTABLES = frozenset(
    {
        "cmake",
        "ctest",
        "cpack",
        "make",
        "gmake",
        "nmake",
        "ninja",
        "msbuild",
        "cl",
        "gcc",
        "g++",
        "clang",
        "clang++",
        "cargo",
        "rustc",
        "go",
        "npm",
        "npx",
        "node",
        "yarn",
        "pnpm",
        "gradle",
        "gradlew",
        "mvn",
        "dotnet",
        "meson",
        "bazel",
    }
)

SHELL_EXECUTABLES = frozenset({"bash", "sh", "zsh", "pwsh", "powershell", "cmd"})

_STRIP_SUFFIXES = (".exe", ".cmd", ".bat", ".com")


def _normalise_token(token: str) -> str:
    name = os.path.basename(token.replace("\\", "/")).casefold()
    for suffix in _STRIP_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _tokenise(command: str) -> list[str]:
    # Backslashes first: posix shlex would otherwise eat Windows path
    # separators (``C:\tools\NMake.exe`` -> ``C:toolsNMake.exe``).
    command = command.replace("\\", "/")
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def card_capability_blocker(card: Mapping[str, Any]) -> str:
    """Return "" or a short reason the card's validation cannot run on vscode_lm."""
    validation = card.get("validation")
    if not isinstance(validation, list):
        return ""
    for command in validation:
        if not isinstance(command, str):
            continue
        tokens = _tokenise(command)
        if not tokens:
            continue
        for token in tokens:
            name = _normalise_token(token)
            if name in NATIVE_TOOLCHAIN_EXECUTABLES:
                return f"native_toolchain:{name}"
        argv0_index = 0
        while argv0_index < len(tokens) and "=" in tokens[argv0_index] and not tokens[argv0_index].startswith("-"):
            argv0_index += 1
        if argv0_index < len(tokens):
            argv0_name = _normalise_token(tokens[argv0_index])
            if argv0_name in SHELL_EXECUTABLES:
                return f"shell:{argv0_name}"
    return ""


VALIDATION_FAILED = "validation_failed"
# NF-2026-02159: validation_auto_rework relaunches on the SAME adapter, so the
# second validation_failed in a row is the point where the same route is no
# longer expected to behave differently.
REPEAT_VALIDATION_FAILURES = 2
# NF-2026-02146: vscode_lm stages one semantic edit per file; a second stage on
# the same file is refused and a retry on vscode_lm fails identically.
STAGING_FAILURE_MARKERS = ("vscode_lm_semantic_edit_stage_required", "coordinates_shifted")
_AUTO_RETURN_HEADER = "validation_auto_rework signature="
_AUTO_RETURN_KEY = "auto_return="
_TEXT_LIMIT = 2000


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _flat(value: Any) -> str:
    """Bounded text of a recorded cause: a string, or a mapping's error/reason fields."""
    if isinstance(value, str):
        return value[:_TEXT_LIMIT]
    if isinstance(value, Mapping):
        return " ".join(
            str(value.get(key))[:_TEXT_LIMIT]
            for key in ("substatus", "error", "reason", "code")
            if isinstance(value.get(key), str)
        )
    return ""


def _first(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _auto_return_count(card: Mapping[str, Any], evidence: Mapping[str, Any]) -> int:
    """Validation failures the card's own rework records attribute to one adapter."""
    counts = [1]
    record = _mapping(evidence.get("validation_auto_rework"))
    prior = record.get("prior_auto_returns")
    if isinstance(prior, int) and not isinstance(prior, bool) and prior >= 0:
        counts.append(prior + 1)
    feedback = card.get("review_feedback")
    text = _first(feedback.get("instruction") if isinstance(feedback, Mapping) else feedback)
    if text.startswith(_AUTO_RETURN_HEADER):
        header = text.split("\n", 1)[0]
        _, _, tail = header.partition(_AUTO_RETURN_KEY)
        digits = tail.split(None, 1)[0] if tail.strip() else ""
        if digits.isdigit():
            counts.append(int(digits))
    return max(counts)


def repeat_failure_cause(card: Mapping[str, Any], candidate: str) -> str:
    """Return "" or why the card's own attempt history rules a vscode_lm candidate out.

    Pure: reads only the card mapping (terminal review, rework predecessor and
    the validation_auto_rework header) and never the store. The validation
    branch is card-wide and sticky: the auto_return count never resets when the
    adapter changes, so a repeatedly failing card is never moved back to
    vscode_lm. The staging branch only applies when the last attempt ran on
    this same candidate.
    """
    review = _mapping(card.get("terminal_review"))
    evidence = _mapping(review.get("evidence"))
    predecessor = _mapping(card.get("rework_predecessor"))
    substatus = _first(
        review.get("substatus"), card.get("terminal_substatus"), predecessor.get("terminal_substatus")
    )
    if substatus == VALIDATION_FAILED:
        if _auto_return_count(card, evidence) >= REPEAT_VALIDATION_FAILURES:
            return "repeated_validation_failure"
        return ""
    adapter = _first(
        evidence.get("adapter_id"),
        _mapping(evidence.get("validation_auto_rework")).get("adapter_id"),
        card.get("adapter_id"),
    )
    if adapter != candidate:
        return ""
    # ponytail: decided from the last attempt only (no per-attempt adapter history on the card); make it sticky when one exists.
    if substatus.startswith("worker_failed"):
        detail = " ".join(
            _flat(value)
            for value in (
                substatus,
                review,
                evidence,
                card.get("terminal_failure"),
                card.get("terminal_review_reason"),
                card.get("launch_error"),
                card.get("blocker_reason"),
            )
        )
        if any(marker in detail for marker in STAGING_FAILURE_MARKERS):
            return "vscode_lm_staging_failure"
    return ""


def reject_incapable(card: Mapping[str, Any], candidate: str, rejections: list[str]) -> bool:
    """Append ``<candidate>:<cause>`` and return True when the card rules the candidate out.

    Causes: ``capability:<reason>`` (validation needs a native toolchain or
    shell), ``repeated_validation_failure`` (card-wide count of validation
    failures, whichever adapter ran last) and ``vscode_lm_staging_failure``
    (the card's last attempt on this same vscode_lm-family adapter).
    """
    reason = card_capability_blocker(card)
    if reason:
        rejections.append(f"{candidate}:capability:{reason}")
        return True
    if "vscode_lm" in candidate:
        cause = repeat_failure_cause(card, candidate)
        if cause:
            rejections.append(f"{candidate}:{cause}")
            return True
    return False
