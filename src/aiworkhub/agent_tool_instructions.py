"""Compact, deterministic AI tool-use instruction projections.

This module is pure: it renders and plans managed-block edits but never reads
or writes repository files.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Literal


CANONICAL_MAX_BYTES = 3800
PROJECTION_MAX_BYTES = 4000
START = "<!-- AIWORKHUB_TOOL_USE_POLICY_START -->"
END = "<!-- AIWORKHUB_TOOL_USE_POLICY_END -->"
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
    context_graph: tuple[str, ...]
    memory: tuple[str, ...]
    kb: tuple[str, ...]
    finish: str


POLICY = ToolPolicy(
    title="AIWorkHub MCP tool-use policy",
    order=(
        "validate the injected AIWorkHub Task MCP receipt, identity and scope",
        "consume and acknowledge the injected project-context receipt",
        "manager uses aiworkhub_manager_source_graph_query; worker uses aiworkhub_worker_source_graph_query",
        "manager uses aiworkhub_manager_session_current_state; worker uses aiworkhub_worker_session_current_state",
        "manager uses aiworkhub_manager_ai_memory_search; worker uses aiworkhub_worker_ai_memory_search",
        "manager uses aiworkhub_manager_kb_search/get/related; worker uses aiworkhub_worker_kb_search/get/related",
        "manager uses aiworkhub_manager_context_graph_search, aiworkhub_manager_context_graph_range and aiworkhub_manager_context_graph_related when enabled; workers never access Context Graph",
        "execute exact card action and validation",
    ),
    adaptive=(
        "Role-specific AIWorkHub MCP tools are mandatory for managers and workers; legacy AITools scripts/databases are not model interfaces.",
        "Task MCP receipt is always required; Source Graph is required for code tasks.",
        "Session Manager, AI Memory and KB run only when the card requests them or the task is non-trivial.",
        "Workers submit durable context changes only through the session/AI Memory/KB write-intent tools; a verified manager accepts or rejects each intent before canonical apply. Never write context databases directly.",
        "Do not make empty irrelevant calls to satisfy ceremony.",
    ),
    source_graph=(
        "When source_graph_required is true, stop if its bundle is unavailable, empty, stale or unacknowledged.",
        "Never use grep, rg, find, tree, broad cat/sed or recursive listing while Source Graph can index/process the target.",
        "A bounded exact-target fallback is allowed only after Source Graph reports that target unsupported or unindexed; record that reason.",
        "Re-query whenever the active symbol, dependency boundary, failure hypothesis, edit scope or validation target materially changes.",
        "Set workflow_stage on every Source Graph call: orientation, implementation, validation, review or rework; never relabel old calls after the fact.",
        "Start with focus/slice; escalate from returned evidence to context/calls/trace, impact, testmap/coverage and then a typed bundle only when needed.",
        "Use body for an exact symbol and bodygrep for indexed literal/body text; refresh once before any recorded bounded fallback.",
        "After Source Graph finds an exact target, prefer body/file preview; otherwise use a bounded read and never reread an unchanged range.",
        "Final receipts distinguish injected, live, zero-hit and cache-hit calls plus modes and fallbacks; one preflight query is not continuous use.",
    ),
    validation=(
        "Exact validation/build/test commands named by the card are allowed.",
        "Exact known-path reads from the card or Source Graph are allowed; they are not broad discovery.",
    ),
    session=(
        "Recover current state before non-trivial assumptions and preserve the returned session identity in the handoff.",
        "Never store secrets or fabricate session evidence.",
    ),
    context_graph=(
        "Manager-only when enabled: search for non-trivial continuation, compaction/handoff recovery or prior-conversation facts; use range/related only from returned evidence.",
        "Workers never query or write Context Graph; durable context uses Session/AI Memory/KB write intents.",
        "Disabled or zero-hit is not failure; no empty ceremonial calls.",
    ),
    memory=(
        "After session recovery, issue one bounded task-specific query.",
        "Reuse returned durable decisions/lessons.",
        "Do not query legacy memory files directly.",
    ),
    kb=(
        "Query authoritative project contracts/docs for unresolved factual context and preserve source identity.",
        "After a zero hit, do not repeat the query unless task scope changes.",
    ),
    finish="Stop at Codex review.",
)


# B833: informational cross-reference only. Deliberately NOT part of
# ``POLICY`` -- adding it there would change ``render_canonical()`` /
# ``render_projection()`` byte output, which is pinned exactly by
# the pinned agent-tool activation evaluation. Kept here so callers/tests
# have one canonical place to
# check the dynamic worker MCP tool names against the policy module without
# hand-duplicating the list.
WORKER_MCP_TOOL_NAMES: tuple[str, ...] = (
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_semantic_edit_prepare",
    "aiworkhub_worker_semantic_edit_apply",
    "aiworkhub_worker_session_current_state",
    "aiworkhub_worker_ai_memory_search",
    "aiworkhub_worker_ai_memory_get",
    "aiworkhub_worker_ai_memory_related",
    "aiworkhub_worker_kb_search",
    "aiworkhub_worker_kb_get",
    "aiworkhub_worker_kb_related",
    "aiworkhub_worker_session_write_intent",
    "aiworkhub_worker_ai_memory_write_intent",
    "aiworkhub_worker_kb_write_intent",
    "aiworkhub_worker_quality_review_submit",
)

MANAGER_CONTEXT_GRAPH_TOOL_NAMES: tuple[str, ...] = (
    "aiworkhub_manager_context_graph_search",
    "aiworkhub_manager_context_graph_range",
    "aiworkhub_manager_context_graph_related",
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
        "Manager Context Graph:",
        *[f"- {item}" for item in policy.context_graph],
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
    "MANAGER_CONTEXT_GRAPH_TOOL_NAMES",
    "POLICY",
    "PROJECTION_MAX_BYTES",
    "PROVIDERS",
    "START",
    "WORKER_MCP_TOOL_NAMES",
    "ToolPolicy",
    "build_apply_plan",
    "diff_projection",
    "inspect_document",
    "render_all",
    "render_canonical",
    "render_projection",
]
