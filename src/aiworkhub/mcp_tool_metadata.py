"""Docstring-derived MCP tool metadata for Tool Definition Quality Score.

Parses the Google-style ``Args:``/``Parameters:`` section of a tool docstring
into per-parameter ``pydantic.Field`` descriptions and derives MCP
``ToolAnnotations`` hints, so the single registration choke point in
``server._nonblocking_mcp_tool`` can publish both without changing any tool's
docstring text.

It also owns the declarative MCP tool profile table (RM-2026-00078):
``MCP_TOOL_PROFILES`` maps each tool name to the profiles that expose it and
how (``listed`` or ``callable``); ``server.mcp_tool_callable`` and
``server.mcp_tool_listed`` read it through ``tool_profile_visibility``.

Module scope imports only the stdlib; ``pydantic`` and ``mcp.types`` are
imported lazily so this module stays importable when the optional ``mcp`` SDK
(and its ``pydantic`` dependency) is absent.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

__all__ = [
    "split_docstring",
    "derive_annotations",
    "prepare_registration",
    "MCP_TOOL_PROFILES",
    "tool_profile_visibility",
    "exposing_profiles",
]

_SECTION_HEADERS = ("Returns:", "Raises:", "Example:", "Examples:", "Note:")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _split_entry(stripped: str) -> tuple[str | None, str]:
    """Split one ``name: text`` / ``name (type): text`` entry line."""
    colon = stripped.find(":")
    if colon <= 0:
        return None, ""
    name = stripped[:colon].strip()
    text = stripped[colon + 1:].strip()
    if not name:
        return None, ""
    if name.endswith(")"):
        open_idx = name.rfind("(")
        if open_idx > 0:
            name = name[:open_idx].strip()
    if not name:
        return None, ""
    return name, text


def _consume_args(
    lines: list[str], start: int, header_indent: int
) -> tuple[dict[str, str], int]:
    """Parse entries until a dedent to header level or another section header."""
    params: dict[str, str] = {}
    i = start
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if _indent(line) <= header_indent or stripped in _SECTION_HEADERS:
            break
        name, text = _split_entry(stripped)
        if name is None:
            i += 1
            continue
        entry_indent = _indent(line)
        parts = [text] if text else []
        j = i + 1
        while j < n:
            nxt = lines[j]
            if not nxt.strip():
                break
            if _indent(nxt) <= entry_indent or nxt.strip() in _SECTION_HEADERS:
                break
            parts.append(nxt.strip())
            j += 1
        params[name] = " ".join(part for part in parts if part)
        i = j
    return params, i


def split_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """Split a Google-style docstring into ``(cleaned text, Args map)``.

    Returns the ``inspect.cleandoc`` text with any ``Args:``/``Parameters:``
    block removed (``Returns``/``Raises``/``Examples``/``Notes`` kept in order)
    plus a map of documented parameter name to its joined description text.
    Never raises: a missing or malformed docstring yields ``(cleaned, {})``.
    """
    if doc is None:
        return "", {}
    if not isinstance(doc, str):
        try:
            doc = str(doc)
        except Exception:
            return "", {}
    try:
        cleaned = inspect.cleandoc(doc)
    except Exception:
        cleaned = doc
    if not cleaned:
        return cleaned, {}
    lines = cleaned.split("\n")
    kept: list[str] = []
    params: dict[str, str] = {}
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip() in ("Args:", "Parameters:"):
            params, i = _consume_args(lines, i + 1, _indent(line))
            continue
        kept.append(line)
        i += 1
    return "\n".join(kept).rstrip(), params


_READ_ONLY_PREFIXES = ("READ-ONLY", "MANAGER READ", "READ:")
_DESTRUCTIVE_TOKENS = ("_purge", "_delete", "_discard")

# Last name segment of a tool that only reads state when no docstring prefix decides.
_READ_VERBS = frozenset({
    "list", "show", "get", "search", "status", "health", "count", "events", "snapshot",
    "preview", "readonly", "query", "inspect", "report", "related", "range", "catalog",
    "rank", "profile", "packet", "summary", "usage", "ledger", "queue", "diagnose", "read",
    "evaluate", "describe", "dryrun", "check", "bench", "current", "inbox", "peers",
    "recommend", "metrics", "eval", "gate", "scan", "guard", "wait", "digest",
})

# Trailing name segment(s) of a state-changing tool that converges on the same state
# when repeated with the same arguments (create-or-update, start-if-stopped, set-to-value).
_IDEMPOTENT_SUFFIXES = (
    "upsert", "ensure", "ensure_started", "stop", "set", "update", "rename", "restore",
    "archive", "link",
)

# Only WorkHub Mail reaches another machine (paired LAN peers).
_OPEN_WORLD_TOKENS = ("hub_mail_send", "hub_mail_pair_", "hub_mail_listener")


def derive_annotations(tool_name: str, description: str) -> dict[str, Any]:
    """Derive MCP ``ToolAnnotations`` hints from a tool name and description.

    Always returns a full hint dict with ``readOnlyHint`` set. A READ-ONLY /
    MANAGER READ / READ: first line wins over the name; then a DESTRUCTIVE first
    line or a ``_purge``/``_delete``/``_discard`` name; then a read verb as the
    last name segment (or ``_readonly`` in the name). Anything else, including
    an unknown name, is a non-destructive write.
    """
    name = tool_name if isinstance(tool_name, str) else ""
    desc = description if isinstance(description, str) else ""
    first_line = desc.split("\n", 1)[0].strip() if desc else ""
    open_world = any(token in name for token in _OPEN_WORLD_TOKENS)
    if first_line.startswith(_READ_ONLY_PREFIXES):
        return {"readOnlyHint": True, "openWorldHint": open_world}
    if first_line.startswith("DESTRUCTIVE") or any(token in name for token in _DESTRUCTIVE_TOKENS):
        return {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": open_world,
        }
    if name and (name.rsplit("_", 1)[-1] in _READ_VERBS or "_readonly" in name):
        return {"readOnlyHint": True, "openWorldHint": open_world}
    hints: dict[str, Any] = {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": open_world,
    }
    if name and any(name == suffix or name.endswith("_" + suffix) for suffix in _IDEMPOTENT_SUFFIXES):
        hints["idempotentHint"] = True
    return hints


def _has_field_description(annotation: Any) -> bool:
    """True when ``annotation`` is ``Annotated[..., FieldInfo(description=...)]``."""
    try:
        if typing.get_origin(annotation) is not typing.Annotated:
            return False
        for meta in typing.get_args(annotation)[1:]:
            if meta is not None and getattr(meta, "description", None):
                return True
    except Exception:
        return False
    return False


def _build_signature(function: Any, params: dict[str, str]) -> "inspect.Signature | None":
    try:
        sig = inspect.signature(function, eval_str=True)
    except (TypeError, ValueError, NameError):
        return None
    if not params:
        return sig
    try:
        from pydantic import Field
    except ImportError:
        return sig
    new_params = []
    for pname, param in sig.parameters.items():
        text = params.get(pname)
        if text is None:
            new_params.append(param)
            continue
        annotation = param.annotation
        if isinstance(annotation, str):
            try:
                hints = typing.get_type_hints(function, include_extras=True)
            except Exception:
                hints = {}
            annotation = hints.get(pname, annotation)
        if _has_field_description(annotation):
            new_params.append(param)
            continue
        new_params.append(
            param.replace(annotation=typing.Annotated[annotation, Field(description=text)])
        )
    return sig.replace(parameters=new_params)


def prepare_registration(
    function: Any, decorator_args: tuple, decorator_kwargs: dict
) -> tuple[list, dict, inspect.Signature]:
    """Build ``(args, kwargs, signature)`` for one MCP tool registration.

    ``args``/``kwargs`` are the (possibly augmented) ``mcp.tool(...)`` call
    arguments: the cleaned docstring becomes ``description`` unless one was
    given, and ``derive_annotations`` supplies ``mcp.types.ToolAnnotations``
    unless ``annotations`` was given (skipped when the SDK import fails). The
    returned signature wraps each documented parameter in
    ``Annotated[<annotation>, pydantic.Field(description=text)]`` while leaving
    an already-``Annotated`` ``FieldInfo`` parameter byte-identical.
    """
    args = list(decorator_args)
    kwargs = dict(decorator_kwargs)

    if "name" in kwargs:
        tool_name = kwargs["name"]
    elif args and isinstance(args[0], str):
        tool_name = args[0]
    else:
        tool_name = getattr(function, "__name__", "")

    doc = getattr(function, "__doc__", None)
    cleaned, params = split_docstring(doc)

    if "description" not in kwargs:
        kwargs["description"] = cleaned
    description = kwargs.get("description", cleaned)

    if "annotations" not in kwargs:
        derived = derive_annotations(tool_name, description)
        if derived is not None:
            try:
                from mcp.types import ToolAnnotations
            except ImportError:
                pass
            else:
                kwargs["annotations"] = ToolAnnotations(**derived)

    signature = _build_signature(function, params)
    return args, kwargs, signature


# MCP tool profiles (RM-2026-00078). AIWORKHUB_MCP_SURFACE selects one profile.
# ``all`` is implicit: every registered tool, listed. Each other profile exposes
# only the tools MCP_TOOL_PROFILES names for it, ``listed`` (in tools/list and
# callable) or ``callable`` (callable by exact name, absent from tools/list).
MCP_PROFILE_ALL = "all"
MCP_PROFILE_MANAGER = "manager"
MCP_PROFILE_WORKER = "worker"
MCP_PROFILE_DASHBOARD = "dashboard"
MCP_PROFILES = (MCP_PROFILE_MANAGER, MCP_PROFILE_WORKER, MCP_PROFILE_DASHBOARD)
MCP_SURFACES = (MCP_PROFILE_ALL, *MCP_PROFILES)

MCP_VISIBILITY_LISTED = "listed"
MCP_VISIBILITY_CALLABLE = "callable"
MCP_VISIBILITIES = (MCP_VISIBILITY_LISTED, MCP_VISIBILITY_CALLABLE)

MCP_DASHBOARD_TOOL_PREFIX = "aiworkhub_dashboard_"

_MANAGER = MappingProxyType({MCP_PROFILE_MANAGER: MCP_VISIBILITY_LISTED})
_DASHBOARD = MappingProxyType({MCP_PROFILE_DASHBOARD: MCP_VISIBILITY_LISTED})
_MANAGER_DASHBOARD = MappingProxyType({
    MCP_PROFILE_MANAGER: MCP_VISIBILITY_LISTED,
    MCP_PROFILE_DASHBOARD: MCP_VISIBILITY_LISTED,
})
_WORKER = MappingProxyType({MCP_PROFILE_WORKER: MCP_VISIBILITY_LISTED})

# Tool name -> {profile: visibility}, in registration order. ``manager`` is exactly
# the pre-table manager surface: every server tool except the 47 webview-only
# dashboard tools. ``dashboard`` is every dashboard_mcp_app tool plus every tool
# the VS Code extension's dashboard code calls (its *_TOOLS maps, *_TOOL constants
# and literal callTool names). ``worker`` is the worker MCP server's tools
# (worker_ai_tools_mcp.MCP_TOOL_NAMES). tests/test_mcp_tool_profiles_rm00078.py
# pins every one of those sources against this table.
MCP_TOOL_PROFILES: dict[str, Mapping[str, str]] = {
    "aiworkhub_manager_bootstrap": _MANAGER,
    "aiworkhub_manager_semantic_edit_prepare": _MANAGER,
    "aiworkhub_manager_semantic_edit_apply": _MANAGER,
    "aiworkhub_manager_source_graph_query": _MANAGER,
    "aiworkhub_manager_session_current_state": _MANAGER,
    "aiworkhub_manager_ai_memory_search": _MANAGER,
    "aiworkhub_manager_ai_memory_get": _MANAGER,
    "aiworkhub_manager_ai_memory_related": _MANAGER,
    "aiworkhub_manager_ai_memory_review": _MANAGER,
    "aiworkhub_manager_kb_search": _MANAGER,
    "aiworkhub_manager_kb_get": _MANAGER,
    "aiworkhub_manager_kb_related": _MANAGER,
    "aiworkhub_manager_context_graph_search": _MANAGER,
    "aiworkhub_manager_context_graph_range": _MANAGER,
    "aiworkhub_manager_context_graph_related": _MANAGER,
    "aiworkhub_manager_context_graph_event_write": _MANAGER,
    "aiworkhub_manager_context_graph_rebuild": _MANAGER,
    "aiworkhub_manager_workforce_catalog": _MANAGER,
    "aiworkhub_manager_workforce_rank": _MANAGER,
    "aiworkhub_manager_task_decomposition_preview": _MANAGER,
    "aiworkhub_manager_session_write": _MANAGER,
    "aiworkhub_manager_ai_memory_write": _MANAGER,
    "aiworkhub_manager_kb_write": _MANAGER,
    "aiworkhub_manager_learning_commit": _MANAGER,
    "aiworkhub_manager_needfix_markdown_preview": _MANAGER,
    "aiworkhub_manager_needfix_markdown_commit": _MANAGER,
    "aiworkhub_manager_workforce_upsert": _MANAGER,
    "aiworkhub_manager_context_import": _MANAGER,
    "aiworkhub_manager_context_write_intents": _MANAGER,
    "aiworkhub_manager_context_write_intent_dispose": _MANAGER,
    "aiworkhub_manager_skill_propose": _MANAGER,
    "aiworkhub_manager_skill_add_evidence": _MANAGER,
    "aiworkhub_manager_skill_usage": _MANAGER,
    "aiworkhub_manager_skill_activate": _MANAGER,
    "aiworkhub_manager_skill_mine": _MANAGER,
    "aiworkhub_manager_skill_retirement_report": _MANAGER,
    "aiworkhub_manager_skill_evidence_sweep": _MANAGER,
    "aiworkhub_manager_loop_start": _MANAGER_DASHBOARD,
    "aiworkhub_manager_loop_ensure": _MANAGER_DASHBOARD,
    "aiworkhub_manager_loop_restore": _MANAGER_DASHBOARD,
    "aiworkhub_manager_loop_rename": _MANAGER_DASHBOARD,
    "aiworkhub_manager_loop_discard": _MANAGER_DASHBOARD,
    "aiworkhub_manager_loop_continue": _MANAGER_DASHBOARD,
    "aiworkhub_manager_loop_new": _MANAGER_DASHBOARD,
    "aiworkhub_manager_loop_send": _MANAGER_DASHBOARD,
    "aiworkhub_manager_loop_rotate": _MANAGER_DASHBOARD,
    "aiworkhub_manager_loop_status": _MANAGER_DASHBOARD,
    "aiworkhub_manager_loop_events": _MANAGER_DASHBOARD,
    "aiworkhub_manager_loop_close": _MANAGER_DASHBOARD,
    "aiworkhub_manager_loop_stop": _MANAGER_DASHBOARD,
    "aiworkhub_manager_recipe_register": _MANAGER,
    "aiworkhub_manager_recipe_seed_canonical": _MANAGER,
    "aiworkhub_manager_recipe_seed_plan": _MANAGER,
    "aiworkhub_manager_recipe_list": _MANAGER,
    "aiworkhub_manager_recipe_usage": _MANAGER,
    "aiworkhub_manager_recipe_show": _MANAGER,
    "aiworkhub_manager_recipe_run": _MANAGER,
    "aiworkhub_manager_hub_mail_send": _MANAGER,
    "aiworkhub_manager_hub_mail_inbox": _MANAGER,
    "aiworkhub_manager_hub_mail_show": _MANAGER,
    "aiworkhub_manager_hub_mail_archive": _MANAGER,
    "aiworkhub_manager_hub_mail_pair_begin": _MANAGER,
    "aiworkhub_manager_hub_mail_pair_complete": _MANAGER,
    "aiworkhub_manager_hub_mail_peers": _MANAGER,
    "aiworkhub_manager_hub_mail_listener": _MANAGER,
    "aiworkhub_vscode_lm_worker_tool": _MANAGER_DASHBOARD,
    "aiworkhub_router_known_repositories": _MANAGER,
    "aiworkhub_repo_list": _MANAGER,
    "aiworkhub_repo_current": _MANAGER,
    "aiworkhub_repo_switch": _MANAGER,
    "aiworkhub_task_create": _MANAGER,
    "aiworkhub_task_create_from_template": _MANAGER,
    "aiworkhub_task_template_list": _MANAGER,
    "aiworkhub_task_template_show": _MANAGER,
    "aiworkhub_task_plan_snapshot": _MANAGER,
    "aiworkhub_task_health": _MANAGER,
    "aiworkhub_task_review_queue": _MANAGER,
    "aiworkhub_task_list": _MANAGER,
    "aiworkhub_task_show": _MANAGER,
    "aiworkhub_task_pending_for_runner": _MANAGER,
    "aiworkhub_task_auto_pickup": _MANAGER,
    "aiworkhub_task_auto_pickup_dryrun": _MANAGER,
    "aiworkhub_task_mark_review": _MANAGER,
    "aiworkhub_task_mark_done": _MANAGER,
    "aiworkhub_task_dependency_autolaunch_reconcile": _MANAGER,
    "aiworkhub_task_reject_review": _MANAGER,
    "aiworkhub_task_recover_blocked_rework": _MANAGER,
    "aiworkhub_task_retry_terminal": _MANAGER,
    "aiworkhub_task_reroute_launch_identity": _MANAGER,
    "aiworkhub_task_archive": _MANAGER,
    "aiworkhub_manager_reconcile_dead_processing": _MANAGER,
    "aiworkhub_task_supersede": _MANAGER,
    "aiworkhub_manager_task_archive": _MANAGER,
    "aiworkhub_manager_task_supersede": _MANAGER,
    "aiworkhub_task_collision_guard": _MANAGER,
    "aiworkhub_task_usage_report": _MANAGER,
    "aiworkhub_task_export_jsonl": _MANAGER,
    "aiworkhub_task_audit_log_read": _MANAGER,
    "aiworkhub_task_review_summarize": _MANAGER,
    "aiworkhub_task_codex_handoff": _MANAGER,
    "aiworkhub_task_codex_handoff_markdown": _MANAGER,
    "aiworkhub_supervisor_loop_status": _MANAGER,
    "aiworkhub_cli_adapter_plan_readonly": _MANAGER,
    "aiworkhub_cli_adapter_audit_summary_readonly": _MANAGER,
    "aiworkhub_cli_adapter_report_readonly": _MANAGER,
    "aiworkhub_dashboard_snapshot": _MANAGER_DASHBOARD,
    "aiworkhub_dashboard_task_detail": _DASHBOARD,
    "aiworkhub_dashboard_health": _DASHBOARD,
    "aiworkhub_dashboard_task_live_output": _DASHBOARD,
    "aiworkhub_dashboard_memory": _DASHBOARD,
    "aiworkhub_dashboard_sessions": _DASHBOARD,
    "aiworkhub_dashboard_kb": _DASHBOARD,
    "aiworkhub_dashboard_skills": _DASHBOARD,
    "aiworkhub_dashboard_needfix_list": _DASHBOARD,
    "aiworkhub_dashboard_needfix_detail": _DASHBOARD,
    "aiworkhub_dashboard_needfix_convert_preview": _DASHBOARD,
    "aiworkhub_dashboard_needfix_capture": _DASHBOARD,
    "aiworkhub_dashboard_needfix_update": _DASHBOARD,
    "aiworkhub_dashboard_needfix_transition": _MANAGER_DASHBOARD,
    "aiworkhub_dashboard_needfix_archive": _DASHBOARD,
    "aiworkhub_dashboard_needfix_restore": _DASHBOARD,
    "aiworkhub_dashboard_needfix_purge": _DASHBOARD,
    "aiworkhub_dashboard_needfix_convert_commit": _DASHBOARD,
    "aiworkhub_dashboard_needfix_link_existing_task": _DASHBOARD,
    "aiworkhub_dashboard_needfix_reopen_superseded_task_link": _DASHBOARD,
    "aiworkhub_dashboard_roadmap_list": _DASHBOARD,
    "aiworkhub_dashboard_roadmap_detail": _DASHBOARD,
    "aiworkhub_dashboard_hub_mail": _DASHBOARD,
    "aiworkhub_dashboard_hub_mail_detail": _DASHBOARD,
    "aiworkhub_dashboard_hub_mail_listener": _DASHBOARD,
    "aiworkhub_dashboard_hub_mail_pair_begin": _DASHBOARD,
    "aiworkhub_dashboard_hub_mail_pair_complete": _DASHBOARD,
    "aiworkhub_dashboard_settings": _DASHBOARD,
    "aiworkhub_dashboard_settings_update": _DASHBOARD,
    "aiworkhub_dashboard_model_settings_update": _DASHBOARD,
    "aiworkhub_dashboard_source_graph_settings_update": _DASHBOARD,
    "aiworkhub_dashboard_storage_retention_preview": _DASHBOARD,
    "aiworkhub_dashboard_storage_quarantine": _DASHBOARD,
    "aiworkhub_dashboard_storage_registration_prune": _DASHBOARD,
    "aiworkhub_dashboard_storage_restore": _DASHBOARD,
    "aiworkhub_dashboard_storage_purge": _DASHBOARD,
    "aiworkhub_dashboard_terminal_log_retention_preview": _DASHBOARD,
    "aiworkhub_dashboard_terminal_log_usage_backfill": _DASHBOARD,
    "aiworkhub_dashboard_terminal_log_quarantine": _DASHBOARD,
    "aiworkhub_dashboard_terminal_log_restore": _DASHBOARD,
    "aiworkhub_dashboard_terminal_log_purge": _DASHBOARD,
    "aiworkhub_dashboard_task_retention_preview": _DASHBOARD,
    "aiworkhub_dashboard_task_archive": _DASHBOARD,
    "aiworkhub_dashboard_task_restore": _DASHBOARD,
    "aiworkhub_dashboard_task_restart": _DASHBOARD,
    "aiworkhub_dashboard_task_quarantine": _DASHBOARD,
    "aiworkhub_dashboard_task_quarantine_restore": _DASHBOARD,
    "aiworkhub_dashboard_task_quarantine_purge": _DASHBOARD,
    "aiworkhub_dashboard_initialize": _DASHBOARD,
    "aiworkhub_agent_tool_instruction_preview": _MANAGER,
    "aiworkhub_agent_tool_instruction_inspect": _MANAGER,
    "aiworkhub_agent_tool_instruction_apply": _MANAGER,
    "aiworkhub_launch_queue_describe_readonly": _MANAGER,
    "aiworkhub_launch_queue_evaluate_readonly": _MANAGER,
    "aiworkhub_launch_queue_audit_summary_readonly": _MANAGER,
    "aiworkhub_task_queue_request": _MANAGER,
    "aiworkhub_task_stale_recovery_recommend": _MANAGER,
    "aiworkhub_task_cost_ledger": _MANAGER,
    "aiworkhub_completion_inbox": _MANAGER,
    "aiworkhub_review_packet": _MANAGER,
    "aiworkhub_agent_launch_task": _MANAGER,
    "aiworkhub_quality_reviewer_launch": _MANAGER,
    "aiworkhub_agent_task_status": _MANAGER,
    "aiworkhub_agent_collect_result": _MANAGER,
    "aiworkhub_agent_cancel_task": _MANAGER,
    "aiworkhub_agent_retry_finalization": _MANAGER,
    "aiworkhub_manager_review_hold_resolve": _MANAGER,
    "aiworkhub_manager_review_finding_dispose": _MANAGER,
    "aiworkhub_agent_accept_preview": _MANAGER,
    "aiworkhub_agent_accept_review": _MANAGER,
    "aiworkhub_agent_list_processes": _MANAGER,
    "aiworkhub_dispatcher_ensure_started": _MANAGER_DASHBOARD,
    "aiworkhub_dispatcher_health": _MANAGER_DASHBOARD,
    "aiworkhub_doctor_diagnose": _MANAGER,
    "aiworkhub_dispatcher_stop": _MANAGER_DASHBOARD,
    "aiworkhub_dispatcher_watchdog": _MANAGER,
    "aiworkhub_source_graph_ensure_started": _MANAGER_DASHBOARD,
    "aiworkhub_source_graph_health": _MANAGER_DASHBOARD,
    "aiworkhub_source_graph_refresh_now": _MANAGER,
    "aiworkhub_source_graph_stop": _MANAGER_DASHBOARD,
    "aiworkhub_claude_callback_wait": _MANAGER,
    "aiworkhub_claude_callback_ack": _MANAGER,
    "aiworkhub_quality_profile": _MANAGER,
    "aiworkhub_known_bug_scan": _MANAGER,
    "aiworkhub_eval_artifact_gate": _MANAGER,
    "aiworkhub_contract_consistency_check": _MANAGER,
    "aiworkhub_source_graph_retrieval_eval": _MANAGER,
    "aiworkhub_session_token_profile": _MANAGER,
    "aiworkhub_risk_mode_precision_bench": _MANAGER,
    "aiworkhub_quality_gate_ratchet": _MANAGER,
    "aiworkhub_prompt_bundle_ab_report": _MANAGER,
    "aiworkhub_suite_profile": _MANAGER,
    "aiworkhub_runtime_coverage_preview": _MANAGER,
    "aiworkhub_runtime_coverage_import": _MANAGER,
    "aiworkhub_quality_run_checks": _MANAGER,
    "aiworkhub_quality_evidence_packet": _MANAGER,
    "aiworkhub_quality_calibration_report": _MANAGER,
    "aiworkhub_environment_preflight": _MANAGER,
    "aiworkhub_storage_recover_stranded_worktrees": _MANAGER,
    "needfix_list": _MANAGER,
    "needfix_show": _MANAGER,
    "aiworkhub_manager_needfix_draft": _MANAGER,
    "needfix_add": _MANAGER,
    "needfix_update": _MANAGER,
    "needfix_archive": _MANAGER,
    "needfix_restore": _MANAGER,
    "needfix_purge": _MANAGER,
    "needfix_count": _MANAGER,
    "needfix_events": _MANAGER,
    "aiworkhub_manager_sdlc_outcome_metrics": _MANAGER,
    "aiworkhub_manager_sdlc_case_create": _MANAGER,
    "aiworkhub_manager_sdlc_stage_record": _MANAGER,
    "aiworkhub_manager_sdlc_case_get": _MANAGER,
    "aiworkhub_manager_sdlc_stage_packet": _MANAGER,
    "aiworkhub_manager_sdlc_case_create_for_task": _MANAGER,
    "aiworkhub_manager_sdlc_case_for_task": _MANAGER,
    "needfix_preview_convert": _MANAGER,
    "needfix_convert": _MANAGER,
    "needfix_link_existing_task": _MANAGER,
    "needfix_reopen_superseded_task_link": _MANAGER,
    "roadmap_list": _MANAGER,
    "roadmap_show": _MANAGER,
    "roadmap_events": _MANAGER,
    "roadmap_snapshot": _MANAGER,
    "roadmap_add": _MANAGER,
    "roadmap_transition": _MANAGER,
    "roadmap_link_task": _MANAGER,
    "aiworkhub_worker_source_graph_query": _WORKER,
    "aiworkhub_worker_semantic_edit_prepare": _WORKER,
    "aiworkhub_worker_semantic_edit_apply": _WORKER,
    "aiworkhub_worker_semantic_edit_exception_declare": _WORKER,
    "aiworkhub_worker_session_current_state": _WORKER,
    "aiworkhub_worker_ai_memory_search": _WORKER,
    "aiworkhub_worker_ai_memory_get": _WORKER,
    "aiworkhub_worker_ai_memory_related": _WORKER,
    "aiworkhub_worker_kb_search": _WORKER,
    "aiworkhub_worker_kb_get": _WORKER,
    "aiworkhub_worker_kb_related": _WORKER,
    "aiworkhub_worker_session_write_intent": _WORKER,
    "aiworkhub_worker_ai_memory_write_intent": _WORKER,
    "aiworkhub_worker_kb_write_intent": _WORKER,
    "aiworkhub_worker_quality_review_packet_read": _WORKER,
    "aiworkhub_worker_quality_review_submit": _WORKER,
    "aiworkhub_worker_validation_run": _WORKER,
    "aiworkhub_worker_validation_output_page": _WORKER,
    "aiworkhub_worker_exit_preflight": _WORKER,
}


def tool_profile_visibility(name: Any, profile: str) -> str | None:
    """Return how ``profile`` exposes tool ``name``: listed, callable, or None.

    ``all`` lists every name. A name in MCP_TOOL_PROFILES is exposed only where
    its entry says. A name the table does not know (a tool registered without an
    entry, or a test's synthetic registry) keeps the pre-table manager rule --
    exposed on ``manager`` unless it is an ``aiworkhub_dashboard_*`` name -- so a
    new tool never silently leaves the manager surface; ``worker`` and
    ``dashboard`` expose only what the table names. A non-string name, which can
    be neither registered nor called, stays exposed on ``manager`` as before.
    An unknown profile exposes nothing.
    """
    if profile == MCP_PROFILE_ALL:
        return MCP_VISIBILITY_LISTED
    if not isinstance(name, str):
        return MCP_VISIBILITY_LISTED if profile == MCP_PROFILE_MANAGER else None
    entry = MCP_TOOL_PROFILES.get(name)
    if entry is not None:
        return entry.get(profile)
    if profile == MCP_PROFILE_MANAGER and not name.startswith(MCP_DASHBOARD_TOOL_PREFIX):
        return MCP_VISIBILITY_LISTED
    return None


def exposing_profiles(name: Any) -> tuple[str, ...]:
    """Every profile that lists or calls ``name``, in MCP_PROFILES order, then ``all``."""
    named = [profile for profile in MCP_PROFILES if tool_profile_visibility(name, profile)]
    return (*named, MCP_PROFILE_ALL)
