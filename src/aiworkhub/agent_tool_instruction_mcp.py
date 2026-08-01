"""B821 MCP tool surface for AIWorkHub agent tool-instruction activation.

Bounded, dry-run-first, idempotent managed-block writes for three known
providers (AGENTS.md, CLAUDE.md, .github/copilot-instructions.md). Generated
content is limited to Task MCP, Source Graph, Session Manager, AI Memory and KB
tool-use policy — no domain/security/general coding-policy import.

Hard invariants:
  * READ-ONLY by default; writes require explicit ``write=True`` AND the parent
    write gate (``core.writes_allowed()``).
  * Repository root is bounded: must exist, must be a directory, no path traversal.
  * Only the three known provider paths are writable; every other path is refused.
  * Uses the existing managed-marker/idempotence implementation from
    ``agent_tool_instructions``; preserves owner-authored text outside the
    managed block.
  * No shell execution, no free-form template execution, no arbitrary path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from aiworkhub import agent_tool_instructions as instr
from aiworkhub import core


# ---------------------------------------------------------------------------
# Authority constants
# ---------------------------------------------------------------------------

READONLY: bool = True
InstructionTarget = Literal[
    "AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md",
]

MCP_TOOL_NAMES: tuple[str, ...] = (
    "aiworkhub_agent_tool_instruction_preview",
    "aiworkhub_agent_tool_instruction_inspect",
    "aiworkhub_agent_tool_instruction_apply",
)


def _authority_flags() -> dict[str, bool]:
    """Read-only authority flags. Write stays False by default."""
    return {
        "write_enabled": core.writes_allowed(),
        "readonly": READONLY,
        "process_launch": False,
        "process_launch_authority": False,
        "shell_invocation": False,
        "agent_launch": False,
        "template_execution": False,
        "domain_policy_generation": False,
    }


# ---------------------------------------------------------------------------
# Path safety (bounded repository root, known providers only)
# ---------------------------------------------------------------------------

def _repo_root(raw: str | os.PathLike[str] | None) -> Path:
    """Resolve and validate repository root; fail-closed on every violation."""
    root = Path(raw or ".").expanduser().resolve()
    if not root.exists():
        raise ValueError(f"repo_root_does_not_exist:{root}")
    if not root.is_dir():
        raise ValueError(f"repo_root_is_not_directory:{root}")
    return root


def _provider_path(repo_root: Path, provider: str) -> Path:
    """Map a known provider name to its exact relative path; refuse everything else."""
    if provider not in instr.PROVIDERS:
        raise ValueError(
            f"unsupported_provider:{provider}; known={list(instr.PROVIDERS)}"
        )
    resolved = (repo_root / provider).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        raise ValueError(
            f"provider_path_escapes_repo_root: provider={provider} resolved={resolved} root={repo_root}"
        )
    return resolved


def _read_owner(repo_root: Path, provider: str) -> str:
    """Read the current provider file text; return '' when the file doesn't exist."""
    path = _provider_path(repo_root, provider)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_plan(repo_root: Path, provider: str) -> dict[str, Any]:
    """Build and return an inert apply plan."""
    owner = _read_owner(repo_root, provider)
    return instr.build_apply_plan(provider, owner)


# ---------------------------------------------------------------------------
# Read-only MCP tool functions
# ---------------------------------------------------------------------------

def preview(
    *,
    repo_root: str,
    provider: str | None = None,
) -> dict[str, Any]:
    """READ-ONLY: dry-run preview for one or all providers.

    Returns the inert apply plan(s) plus unified diffs WITHOUT mutating any
    repository file.  Defaults to all three providers when ``provider`` is
    omitted.
    """
    root = _repo_root(repo_root)
    providers = (provider,) if provider else instr.PROVIDERS

    plans: dict[str, Any] = {}
    for p in providers:
        _provider_path(root, p)  # raises on escape / unknown
        plan = _write_plan(root, p)
        diff = instr.diff_projection(p, _read_owner(root, p))
        plans[p] = {
            "ok": plan["ok"],
            "reason": plan["reason"],
            "inspection": plan["inspection"],
            "diff": diff,
        }

    return {
        "ok": True,
        "mode": "dry_run_preview",
        "dry_run": True,
        "write_performed": False,
        "mutated_state": False,
        "repo_root": str(root),
        "providers": plans,
        "authority_flags": _authority_flags(),
    }


def inspect_document(
    *,
    repo_root: str,
    provider: str,
) -> dict[str, Any]:
    """READ-ONLY: inspect managed markers in one provider file.

    Returns marker counts and managed-block validity without mutating anything.
    Refuses unknown or path-escaping ``provider`` values.
    """
    root = _repo_root(repo_root)
    path = _provider_path(root, provider)
    text = _read_owner(root, provider)
    inspection = instr.inspect_document(text)
    return {
        "ok": True,
        "mode": "readonly_inspect",
        "dry_run": True,
        "write_performed": False,
        "mutated_state": False,
        "repo_root": str(root),
        "provider": provider,
        "provider_path": str(path),
        "file_exists": path.exists(),
        "inspection": inspection,
        "authority_flags": _authority_flags(),
    }


def apply(
    *,
    repo_root: str,
    provider: str,
    write: bool = False,
) -> dict[str, Any]:
    """Activate the agent tool-use policy block for one provider.

    Read-only dry-run by default.  Only writes when ``write=True`` AND the
    parent write gate (``core.writes_allowed()``) is enabled.  Uses the existing
    managed-marker/idempotence path so a second write is a no-op and owner text
    outside the managed block is never altered.
    """
    root = _repo_root(repo_root)
    path = _provider_path(root, provider)
    owner = _read_owner(root, provider)
    plan = instr.build_apply_plan(provider, owner)

    result: dict[str, Any] = {
        "ok": plan["ok"],
        "mode": "dry_run" if not write else "apply",
        "dry_run": not write,
        "write_performed": False,
        "provider": provider,
        "provider_path": str(path),
        "repo_root": str(root),
        "reason": plan["reason"],
        "inspection": plan["inspection"],
        "authority_flags": _authority_flags(),
    }

    if not plan["ok"]:
        result["error"] = "managed_block_marker_corrupt_or_duplicate"
        return result

    if not write:
        result["diff"] = instr.diff_projection(provider, owner)
        result["would_change"] = plan["planned_text"] != plan["current_text"]
        return result

    if not core.writes_allowed():
        result["ok"] = False
        result["error"] = "write_gate_disabled"
        result["mode"] = "rejected"
        return result

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan["planned_text"], encoding="utf-8")
    result["write_performed"] = True
    result["written_bytes"] = len(plan["planned_text"].encode("utf-8"))
    result["dry_run"] = False
    return result


# ---------------------------------------------------------------------------
# MCP-bound tool functions (bind to server's active repo_root, reject caller path)
# ---------------------------------------------------------------------------

def _mcp_preview(provider: InstructionTarget | None = None) -> dict[str, Any]:
    """MCP-bound: preview using the server's active repository root.

    Never accepts a caller-selected ``repo_root``; always binds to
    ``core.repo_root()``.  The CLI may still target an owner-selected
    repository via ``--repo``, but the MCP surface is pinned to the
    server's active repository. ``provider`` is an instruction-file target,
    not a model provider; its schema enumerates all valid filenames.
    """
    return preview(repo_root=str(core.repo_root()), provider=provider)


def _mcp_inspect(provider: InstructionTarget) -> dict[str, Any]:
    """MCP-bound: inspect using the server's active repository root."""
    return inspect_document(repo_root=str(core.repo_root()), provider=provider)


def _mcp_apply(provider: InstructionTarget, write: bool = False) -> dict[str, Any]:
    """MCP-bound: apply using the server's active repository root."""
    return apply(repo_root=str(core.repo_root()), provider=provider, write=write)


# ---------------------------------------------------------------------------
# MCP tool registration map
# ---------------------------------------------------------------------------

MCP_TOOLS: dict[str, Any] = {
    "aiworkhub_agent_tool_instruction_preview": _mcp_preview,
    "aiworkhub_agent_tool_instruction_inspect": _mcp_inspect,
    "aiworkhub_agent_tool_instruction_apply": _mcp_apply,
}


def register(mcp: Any) -> tuple[str, ...]:
    """Register the three read-only-first tools on a FastMCP-like server.

    All MCP tools bind to the server's active ``core.repo_root()`` and
    reject caller-selected repository paths.  The CLI may still target an
    owner-selected repository via ``--repo``.

    Kept out of ``server.py`` so the server wiring is a one-liner.  All tools
    are read-only by default; ``apply`` gates writes behind both the explicit
    ``write`` flag AND the parent write gate.
    """
    for name, fn in MCP_TOOLS.items():
        mcp.tool(name=name)(fn)
    return MCP_TOOL_NAMES
