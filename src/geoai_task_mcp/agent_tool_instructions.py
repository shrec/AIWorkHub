"""Compact, deterministic AI tool-use instruction projections.

This module is pure: it renders and plans managed-block edits but never reads
or writes repository files.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Literal


CANONICAL_MAX_BYTES = 2200
PROJECTION_MAX_BYTES = 3000
START = "<!-- GEOAI_TASK_MCP_TOOL_USE_POLICY_START -->"
END = "<!-- GEOAI_TASK_MCP_TOOL_USE_POLICY_END -->"
PROVIDERS = ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md")

Provider = Literal["AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md"]


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Structured policy source used by every provider projection."""

    title: str
    order: tuple[str, ...]
    adaptive: tuple[str, ...]
    source_graph: tuple[str, ...]
    validation: tuple[str, ...]
    session: tuple[str, ...]
    memory: tuple[str, ...]
    kb: tuple[str, ...]
    finish: str


POLICY = ToolPolicy(
    title="GeoAI Task MCP tool-use policy",
    order=(
        "validate Task MCP card identity/scope",
        "consume and acknowledge injected project-context receipt",
        "use Source Graph for code discovery, slicing, impact and exact read targets",
        "use Session Manager current state for non-trivial continuity",
        "query targeted AI Memory",
        "query targeted KB",
        "execute exact card action and validation",
    ),
    adaptive=(
        "Task MCP receipt is always required.",
        "Source Graph is required for code tasks.",
        "Session Manager, AI Memory and KB run only when the card requests them or the task is non-trivial.",
        "Do not make empty irrelevant calls to satisfy ceremony.",
    ),
    source_graph=(
        "When source_graph_required is true, stop if its bundle is unavailable, empty, stale or unacknowledged.",
        "Do not fall back to grep, rg, find, tree, broad cat/sed, recursive listing or manual full-repository parsing for repository discovery.",
    ),
    validation=(
        "Exact validation/build/test commands named by the card are allowed.",
        "Exact known-path reads from the card or Source Graph are allowed; they are not broad discovery.",
    ),
    session=(
        "Bootstrap current state before non-trivial assumptions.",
        "Checkpoint meaningful start, edit, decision, blocker, validation and handoff events.",
        "Do not checkpoint tiny reads; never store secrets.",
    ),
    memory=(
        "After session recovery, issue one bounded task-specific query.",
        "Reuse returned durable decisions/lessons.",
        "Write only new durable knowledge; no raw logs, prompts, credentials or duplicate session state.",
    ),
    kb=(
        "Query authoritative project contracts/docs for unresolved factual context and preserve source identity.",
        "After a zero hit, do not repeat the query unless task scope changes.",
    ),
    finish="Stop at Codex review.",
)


def _lines(policy: ToolPolicy = POLICY) -> list[str]:
    return [
        f"# {policy.title}",
        "Order:",
        *[f"{idx}. {step}." for idx, step in enumerate(policy.order, start=1)],
        "Adaptive use:",
        *[f"- {item}" for item in policy.adaptive],
        "Source Graph gate:",
        *[f"- {item}" for item in policy.source_graph],
        "Exact-command exception:",
        *[f"- {item}" for item in policy.validation],
        "Session Manager:",
        *[f"- {item}" for item in policy.session],
        "AI Memory:",
        *[f"- {item}" for item in policy.memory],
        "KB:",
        *[f"- {item}" for item in policy.kb],
        policy.finish,
    ]


def _assert_size(label: str, text: str, limit: int) -> None:
    size = len(text.encode("utf-8"))
    if size > limit:
        raise ValueError(f"{label}_too_large:{size}>{limit}")


def render_canonical(policy: ToolPolicy = POLICY) -> str:
    """Render the compact canonical policy, capped by contract."""

    text = "\n".join(_lines(policy)).strip() + "\n"
    _assert_size("canonical_policy", text, CANONICAL_MAX_BYTES)
    return text


def render_projection(provider: Provider, policy: ToolPolicy = POLICY) -> str:
    """Render one deterministic provider projection from the same policy."""

    if provider not in PROVIDERS:
        raise ValueError(f"unsupported_provider:{provider}")
    body = render_canonical(policy)
    text = f"{START}\nTarget: {provider}\n{body}{END}\n"
    _assert_size(f"projection:{provider}", text, PROJECTION_MAX_BYTES)
    return text


def render_all(policy: ToolPolicy = POLICY) -> dict[str, str]:
    return {provider: render_projection(provider, policy) for provider in PROVIDERS}


def inspect_document(text: str) -> dict[str, Any]:
    """Inspect managed markers without mutating owner text."""

    starts = text.count(START)
    ends = text.count(END)
    valid = starts == 1 and ends == 1 and text.find(START) < text.find(END)
    return {
        "start_count": starts,
        "end_count": ends,
        "managed": valid,
        "fail_closed": (starts != ends) or starts > 1 or ends > 1 or (starts == 1 and text.find(START) > text.find(END)),
    }


def _replace_block(text: str, block: str) -> str:
    start = text.find(START)
    end = text.find(END)
    if start < 0:
        return text.rstrip() + ("\n\n" if text.strip() else "") + block
    end += len(END)
    return text[:start] + block.rstrip() + text[end:]


def build_apply_plan(provider: Provider, owner_text: str, policy: ToolPolicy = POLICY) -> dict[str, Any]:
    """Return a non-mutating managed-block apply plan."""

    block = render_projection(provider, policy)
    inspection = inspect_document(owner_text)
    if inspection["fail_closed"]:
        return {
            "ok": False,
            "reason": "managed_block_marker_corrupt_or_duplicate",
            "provider": provider,
            "current_text": owner_text,
            "planned_text": owner_text,
            "inspection": inspection,
        }
    planned = _replace_block(owner_text, block)
    return {
        "ok": True,
        "reason": "replace_managed_block" if inspection["managed"] else "append_managed_block",
        "provider": provider,
        "current_text": owner_text,
        "planned_text": planned,
        "inspection": inspection,
    }


def diff_projection(provider: Provider, owner_text: str, policy: ToolPolicy = POLICY) -> str:
    """Return a unified diff for the inert apply plan."""

    plan = build_apply_plan(provider, owner_text, policy)
    return "".join(
        difflib.unified_diff(
            owner_text.splitlines(keepends=True),
            str(plan["planned_text"]).splitlines(keepends=True),
            fromfile=f"a/{provider}",
            tofile=f"b/{provider}",
        )
    )


__all__ = [
    "CANONICAL_MAX_BYTES",
    "END",
    "POLICY",
    "PROJECTION_MAX_BYTES",
    "PROVIDERS",
    "START",
    "ToolPolicy",
    "build_apply_plan",
    "diff_projection",
    "inspect_document",
    "render_all",
    "render_canonical",
    "render_projection",
]
