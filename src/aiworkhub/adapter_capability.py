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


def reject_incapable(card: Mapping[str, Any], candidate: str, rejections: list[str]) -> bool:
    """Append ``<candidate>:capability:<reason>`` and return True when the card is blocked."""
    reason = card_capability_blocker(card)
    if not reason:
        return False
    rejections.append(f"{candidate}:capability:{reason}")
    return True
