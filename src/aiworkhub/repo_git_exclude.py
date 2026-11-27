"""Keep AIWorkHub host-local files out of a hosted repository's git index.

NF-2026-01013: AIWorkHub creates ``.aiworkhub/``, ``.scratch/``, MCP configs
and agent-instruction files inside the repository it hosts. With no ignore
entry a plain ``git add -A`` published sqlite databases and Windows host paths
to a public repository.

:func:`ensure_host_local_excludes` appends ONE marked block to
``<git-common-dir>/info/exclude``. That file is local to the clone (never
committed, never shared) and git applies it only to UNTRACKED paths, so a
repository that deliberately commits one of these files keeps tracking it and
committing a new one stays an explicit ``git add -f``. The tracked
``.gitignore`` is never touched.

Stdlib and file-only: no git subprocess, so it is safe on the bootstrap path.
The function never raises; every outcome is a receipt with a ``state``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

BLOCK_BEGIN = "# >>> aiworkhub host-local (managed)"
BLOCK_END = "# <<< aiworkhub"

HOST_LOCAL_PATTERNS: tuple[str, ...] = (
    "/.aiworkhub/",
    "/.scratch/",
    "/.mcp.json",
    "/.vscode/mcp.json",
    "/AGENTS.md",
    "/CLAUDE.md",
    "/.github/copilot-instructions.md",
)

# A ``.git`` file / ``commondir`` file is one short line; anything larger is not one.
_POINTER_FILE_MAX_BYTES = 64 * 1024
# info/exclude is hand-edited text; refuse to rewrite something implausibly large.
_EXCLUDE_MAX_BYTES = 4 * 1024 * 1024


def managed_block() -> str:
    """The exact block text (with trailing newline) this module owns."""
    return "\n".join((BLOCK_BEGIN, *HOST_LOCAL_PATTERNS, BLOCK_END)) + "\n"


def _read_pointer(path: Path) -> str:
    if path.stat().st_size > _POINTER_FILE_MAX_BYTES:
        raise ValueError(f"pointer_file_too_large:{path.name}")
    return path.read_text(encoding="utf-8", errors="strict").strip()


def resolve_git_common_dir(repo_root: Path) -> tuple[Path | None, str]:
    """Return ``(common_git_dir, reason)``; the dir is None when there is none.

    ``.git`` may be a directory (normal clone) or a ``gitdir: <path>`` file
    (linked worktree / submodule). A linked worktree's private git dir names
    its shared repository in ``commondir``; info/exclude is read from there.
    """
    dot_git = Path(repo_root) / ".git"
    if dot_git.is_dir():
        git_dir = dot_git
    elif dot_git.is_file():
        text = _read_pointer(dot_git)
        if not text.startswith("gitdir:"):
            return None, "dot_git_file_without_gitdir_line"
        target = Path(text[len("gitdir:"):].strip())
        git_dir = target if target.is_absolute() else (dot_git.parent / target)
        if not git_dir.is_dir():
            return None, "gitdir_target_missing"
    else:
        return None, "no_git_dir"
    commondir = git_dir / "commondir"
    if commondir.is_file():
        common = Path(_read_pointer(commondir))
        common_dir = common if common.is_absolute() else (git_dir / common)
        if common_dir.is_dir():
            return Path(os.path.normpath(common_dir)), ""
        return None, "commondir_target_missing"
    return Path(os.path.normpath(git_dir)), ""


def _merge(existing: str) -> str:
    """Return ``existing`` with exactly one current managed block."""
    block = managed_block()
    lines = existing.splitlines(keepends=True)
    begin = next((i for i, line in enumerate(lines) if line.rstrip("\r\n") == BLOCK_BEGIN), None)
    if begin is not None:
        end = next(
            (i for i in range(begin + 1, len(lines)) if lines[i].rstrip("\r\n") == BLOCK_END),
            None,
        )
        if end is not None:
            return "".join(lines[:begin]) + block + "".join(lines[end + 1:])
        # An unterminated block: drop only the stray marker, keep the user's rules.
        existing = "".join(lines[:begin] + lines[begin + 1:])
    prefix = existing
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + block


def ensure_host_local_excludes(repo_root: Path | str) -> dict[str, Any]:
    """Idempotently write the managed block into the repo's info/exclude.

    ``state`` is one of ``written`` (block added), ``updated`` (an older block
    replaced), ``unchanged`` (already current; file not rewritten),
    ``no_git_dir`` (not a git checkout: nothing to do) or ``error`` (with a
    ``reason`` naming the cause). Never raises into the caller.
    """
    receipt: dict[str, Any] = {"state": "error", "reason": "", "exclude_path": ""}
    try:
        common_dir, reason = resolve_git_common_dir(Path(repo_root))
        if common_dir is None:
            receipt["state"] = "no_git_dir" if reason == "no_git_dir" else "error"
            receipt["reason"] = reason
            return receipt
        exclude = common_dir / "info" / "exclude"
        receipt["exclude_path"] = str(exclude)
        existing = ""
        if exclude.is_file():
            if exclude.stat().st_size > _EXCLUDE_MAX_BYTES:
                receipt["reason"] = f"info_exclude_too_large:limit_{_EXCLUDE_MAX_BYTES}_bytes"
                return receipt
            with exclude.open("r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
                existing = fh.read()
        merged = _merge(existing)
        if merged == existing:
            receipt["state"] = "unchanged"
            return receipt
        had_block = any(line.rstrip("\r\n") == BLOCK_BEGIN for line in existing.splitlines())
        exclude.parent.mkdir(parents=True, exist_ok=True)
        tmp = exclude.with_name(f"exclude.aiworkhub-{os.getpid()}-{os.urandom(4).hex()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
                fh.write(merged)
            os.replace(tmp, exclude)
        finally:
            if tmp.exists():
                tmp.unlink()
        receipt["state"] = "updated" if had_block else "written"
        return receipt
    except Exception as exc:  # noqa: BLE001 -- the caller (bootstrap) must stay available
        receipt["state"] = "error"
        receipt["reason"] = f"{type(exc).__name__}:{exc}"[:240]
        return receipt


def bootstrap_receipt(repo_root: Path | str) -> dict[str, str]:
    """The small ``{state, reason}`` key manager_bootstrap surfaces. Never raises."""
    try:
        receipt = ensure_host_local_excludes(repo_root)
        return {
            "state": str(receipt.get("state") or "error"),
            "reason": str(receipt.get("reason") or ""),
        }
    except Exception as exc:  # noqa: BLE001 -- bootstrap must remain available
        return {"state": "error", "reason": type(exc).__name__}
