"""The agentic-CLI backend behind the manager loop's ``ManagerBackend`` protocol.

RM-2026-00067 phase 1, card 2. :mod:`aiworkhub.manager_loop` owns the
provider-neutral half -- the session record, the rehydration brief, the
orchestrator -- and launches nothing. This module is the other half: one
manager conversation carried by one agentic CLI (``claude_cli``, ``codex_cli``
or ``opencode_cli``) on the HOST. There is no AppContainer, no Landlock jail
and no worker MCP runtime here; those belong to the worker lane, which claims a
card and confines it. The manager is the operator's own seat.

Reused, found through Source Graph rather than respelled:

* :func:`aiworkhub.runtime_adapters.build_runtime_command` -- the per-adapter
  argv/cwd plan the worker lane already builds: ``--output-format stream-json``
  for Claude, ``exec --json`` for Codex, ``run --format json`` for OpenCode,
  non-interactive, with model selection and executable resolution.
* :func:`aiworkhub.runtime_adapters.classify_provider_outcome` -- the named
  verdict for a non-zero provider exit, carried into the ``error`` event.
* :func:`aiworkhub.context_capture._message_text` -- the existing projection of
  a Claude message's completed text blocks into one string.
* :func:`aiworkhub.workforce_catalog.catalog_declares_route` -- the existing
  workforce registry, which decides whether ``(backend_id, model)`` is a route
  this repository actually declares.
* :func:`aiworkhub.platform_io.terminate_process_tree` and
  :func:`aiworkhub.platform_io.is_windows` -- the process kill facade and the
  one platform predicate. Nothing here calls ``os.kill``; nothing runs through
  a shell; every launch is an argv list.

Deliberately NOT reused, with the reason:

* :func:`aiworkhub.provider_usage.read_provider_usage` summarizes a *completed*
  stdout log by path. A turn must emit events while the process still runs, so
  the terminal event's own provider-reported ``usage`` mapping is carried
  through unchanged instead of being re-derived after exit.
* :func:`aiworkhub.manager_transcript_capture.extract_codex_completed_message`
  parses the Codex App Server mux JSON-RPC protocol (``item/completed``), not
  the ``codex exec --json`` stream this backend reads.
* ``process_launcher.ProcessManager`` is worker-only: it claims a card and
  provisions a sandbox and a worker MCP runtime around every launch. A manager
  turn is none of those, so this module spawns its own child directly.

A line the translator does not recognize is skipped, never fatal. Each turn is
planned by :func:`runtime_adapters.build_manager_command`, the host-side plan:
a worker is confined, the owner's manager seat is not.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Sequence

from . import cli_model_discovery, context_capture, platform_io, runtime_adapters, workforce_catalog
from .manager_loop import ManagerLoopError, keep_tail

MANAGER_BACKEND_IDS: tuple[str, ...] = ("claude_cli", "codex_cli", "opencode_cli")
# The worker lane's own launch ceiling, reused as the manager turn default: a
# manager turn is one non-interactive CLI run of the same shape.
DEFAULT_TURN_TIMEOUT_SECONDS = 3600.0
MAX_ERROR_DETAIL_CHARS = 500
MAX_STDERR_CHARS = 2000
REAP_TIMEOUT_SECONDS = 5.0

# The conversation id spellings the three CLIs report; the first non-empty one
# seen in a turn is what the next turn resumes.
_CONVERSATION_KEYS = ("session_id", "sessionID", "thread_id", "threadId", "conversation_id")
# Claude's worker argv forbids session persistence, which is exactly what a
# multi-turn manager conversation needs, so this one token is dropped.
_CLAUDE_NO_PERSIST = "--no-session-persistence"
# Codex's ``--ephemeral`` likewise keeps no session, so ``exec resume`` would
# find nothing to resume (measured, Codex CLI 0.156.1).
_DROPPED_TOKENS: Mapping[str, frozenset[str]] = MappingProxyType(
    {"claude_cli": frozenset({_CLAUDE_NO_PERSIST}), "codex_cli": frozenset({"--ephemeral"})}
)
# Where each CLI's grammar allows its resume tokens: after the executable for
# Claude, after the ``exec``/``run`` subcommand for Codex and OpenCode.
_RESUME_GRAMMAR: Mapping[str, tuple[int, str]] = MappingProxyType(
    {"claude_cli": (1, "--resume"), "codex_cli": (2, "resume"), "opencode_cli": (2, "--session")}
)
# Only Claude has a verified argv spelling for an MCP config in this repository;
# OpenCode takes its config through OPENCODE_CONFIG_CONTENT instead, so a config
# asked for on another backend is refused rather than silently dropped.
_MCP_CONFIG_FLAGS: Mapping[str, str] = MappingProxyType({"claude_cli": "--mcp-config"})
_CODEX_TOOL_ITEMS = frozenset({"command_execution", "mcp_tool_call", "file_change", "web_search"})


def _turn_error(source: str, detail: str) -> dict[str, Any]:
    """One loop ``error`` event, bounded so a hostile provider cannot flood the log."""
    payload = {"source": source, "error": str(detail)[:MAX_ERROR_DETAIL_CHARS]}
    return {"type": "error", "payload": payload}


OUTPUT_TAIL_BYTES = 8 * 1024
_CLAUDE_COMMAND_TOOLS = frozenset({"Bash"})
_CLAUDE_EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write"})


@dataclasses.dataclass
class TurnContext:
    """What one turn's translator remembers between lines.

    ``calls`` pairs an open tool call with its result across lines. Claude's
    ``context_fill`` reads the LAST assistant call's usage and model, not the
    turn total, so both are kept here too, refreshed from every ``assistant`` line.
    """

    calls: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    last_usage: dict[str, Any] = dataclasses.field(default_factory=dict)
    model: str = ""
    steps: dict[str, Any] = dataclasses.field(default_factory=dict)


def _count(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else 0


def _usage(
    input_: Any, cache_read: Any, cache_write: Any, output: Any, window: Any,
    raw: Mapping[str, Any], *, fill: int | None = None,
) -> dict[str, Any]:
    """Provider usage in the one shape the console and rotation read (spec §4).

    ``fill`` is the already-summed numerator for ``context_fill``; ``None``
    means it cannot be computed (no window, or no per-call usage to read it
    from), never that it is zero.
    """
    counts = {
        "input": _count(input_), "cache_read": _count(cache_read),
        "cache_write": _count(cache_write), "output": _count(output),
    }
    size = _count(window)
    fill_ratio = round(fill / size, 4) if size and fill is not None else None
    return {**counts, "context_window": size or None, "context_fill": fill_ratio, "raw": dict(raw)}


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "") for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
    return "" if content is None else json.dumps(content, default=str)


def _command(
    call_id: str, command: str, *, status: str, exit_code: Any = None, output: str = "", cwd: str = "",
) -> dict[str, Any]:
    return {"type": "command", "payload": {
        "call_id": call_id, "command": command, "cwd": cwd, "status": status,
        "exit_code": exit_code if isinstance(exit_code, int) else None,
        "output_tail": keep_tail(output, OUTPUT_TAIL_BYTES), "output_bytes": len(output.encode("utf-8")),
    }}


def _file_change(call_id: str, path: str, pairs: list[tuple[str, str]], kind: str) -> dict[str, Any]:
    """One file change; each (before, after) pair becomes one hunk of a unified diff."""
    lines: list[str] = []
    added = removed = 0
    for before, after in pairs:
        hunk = list(difflib.unified_diff(
            before.splitlines(), after.splitlines(), f"a/{path}", f"b/{path}", lineterm="",
        ))
        body = hunk[2:]  # past the ---/+++ header; a content line "+++x" still counts as one added line
        added += sum(1 for line in body if line.startswith("+"))
        removed += sum(1 for line in body if line.startswith("-"))
        lines.extend(body if lines else hunk)
    return {"type": "file_change", "payload": {
        "call_id": call_id, "path": path, "kind": kind, "diff": "\n".join(lines),
        "added": added, "removed": removed,
    }}


def _tool_call(call_id: str, name: str, request: Any) -> dict[str, Any]:
    return {"type": "tool_call", "payload": {"call_id": call_id, "name": name, "input": request}}


def _tool_result(call_id: str, name: str, output: Any, failed: bool) -> dict[str, Any]:
    return {"type": "tool_result", "payload": {
        "call_id": call_id, "name": name, "output": output, "is_error": failed,
    }}


def _turn_end(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    """The turn's final event; ``usage`` appears only when the provider reported it."""
    return {"type": "turn_end", "payload": {"usage": dict(usage)} if usage else {}}


def _assistant_text(value: Any) -> list[dict[str, Any]]:
    text = str(value or "").strip()
    return [{"type": "assistant_text", "payload": {"text": text}}] if text else []


def _reasoning(value: Any) -> list[dict[str, Any]]:
    """One bounded model-reasoning event; thinking is evidence, not a secret."""
    text = str(value or "").strip()
    return [{"type": "reasoning", "payload": {"text": text}}] if text else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _claude_usage(event: Mapping[str, Any], context: TurnContext) -> dict[str, Any] | None:
    """Turn totals from ``result.usage``; ``context_fill`` from the last call instead (W1-P1b)."""
    usage = _mapping(event.get("usage"))
    if not usage:
        return None
    model_usage = _mapping(event.get("modelUsage"))
    if context.model and context.model in model_usage:
        window = _mapping(model_usage[context.model]).get("contextWindow")
    else:
        windows = [_count(_mapping(entry).get("contextWindow")) for entry in model_usage.values()]
        window = max(windows, default=0)
    last = context.last_usage
    fill = None
    if last:
        fill = (
            _count(last.get("input_tokens"))
            + _count(last.get("cache_read_input_tokens"))
            + _count(last.get("cache_creation_input_tokens"))
        )
    return _usage(
        usage.get("input_tokens"), usage.get("cache_read_input_tokens"),
        usage.get("cache_creation_input_tokens"), usage.get("output_tokens"), window, usage, fill=fill,
    )


def _claude_tool_use(block: Mapping[str, Any], context: TurnContext) -> list[dict[str, Any]]:
    call_id, name = str(block.get("id") or ""), str(block.get("name") or "")
    context.calls[call_id] = {"name": name, "input": block.get("input")}
    if name in _CLAUDE_COMMAND_TOOLS:
        return [_command(call_id, str(_mapping(block.get("input")).get("command") or ""), status="running")]
    if name in _CLAUDE_EDIT_TOOLS:
        return []  # shown once its result proves the write happened
    return [_tool_call(call_id, name, block.get("input"))]


def _claude_file_changes(
    call_id: str, name: str, request: Mapping[str, Any], output: str,
) -> list[dict[str, Any]]:
    path = str(request.get("file_path") or "")
    if name == "Write":
        kind = "add" if output.startswith("File created") else "update"
        return [_file_change(call_id, path, [("", str(request.get("content") or ""))], kind)]
    edits = request.get("edits") if name == "MultiEdit" else [request]
    pairs = [
        (str(e.get("old_string") or ""), str(e.get("new_string") or ""))
        for e in edits or [] if isinstance(e, Mapping)
    ]
    return [_file_change(call_id, path, pairs, "update")]


def _claude_tool_result(block: Mapping[str, Any], context: TurnContext) -> list[dict[str, Any]]:
    call_id = str(block.get("tool_use_id") or "")
    call = context.calls.pop(call_id, {"name": "", "input": None})
    name, request, failed = call["name"], _mapping(call["input"]), bool(block.get("is_error"))
    output = _result_text(block.get("content"))
    if name in _CLAUDE_COMMAND_TOOLS:
        status = "failed" if failed else "completed"
        return [_command(call_id, str(request.get("command") or ""), status=status, output=output)]
    if name in _CLAUDE_EDIT_TOOLS and not failed:
        return _claude_file_changes(call_id, name, request, output)
    shown = [_tool_call(call_id, name, call["input"])] if name in _CLAUDE_EDIT_TOOLS else []
    return [*shown, _tool_result(call_id, name, block.get("content"), failed)]


def _delta(kind: str, text: Any) -> list[dict[str, Any]]:
    """A streamed fragment for display only; the orchestrator never records it."""
    text = str(text or "")
    return [{"type": "delta", "payload": {"kind": kind, "text": text}}] if text else []


def _claude_delta(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    inner = _mapping(event.get("event"))
    if inner.get("type") != "content_block_delta":
        return []
    delta = _mapping(inner.get("delta"))
    if delta.get("type") == "text_delta":
        return _delta("text", delta.get("text"))
    if delta.get("type") == "thinking_delta":
        return _delta("reasoning", delta.get("thinking"))
    return []


def _claude_events(event: Mapping[str, Any], context: TurnContext) -> list[dict[str, Any]]:
    """Claude ``stream-json``: assistant/user content blocks, then the result event."""
    kind = str(event.get("type") or "")
    if kind == "stream_event":
        return _claude_delta(event)
    if kind == "result":
        return [_turn_end(_claude_usage(event, context))]
    message = _mapping(event.get("message"))
    if kind == "assistant":
        context.model = str(message.get("model") or context.model)
        usage = _mapping(message.get("usage"))
        if usage:
            context.last_usage = dict(usage)
    content = message.get("content")
    events = _assistant_text(context_capture._message_text(content))
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "tool_use":
            events.extend(_claude_tool_use(block, context))
        elif block.get("type") == "tool_result":
            events.extend(_claude_tool_result(block, context))
        elif block.get("type") == "thinking":
            events.extend(_reasoning(block.get("thinking")))
    return events


def _codex_usage(usage: Any) -> dict[str, Any] | None:
    usage = _mapping(usage)
    if not usage:
        return None
    cached = _count(usage.get("cached_input_tokens"))
    # Codex counts cached tokens inside input_tokens; the fixture confirms it when cached <= input.
    return _usage(
        max(_count(usage.get("input_tokens")) - cached, 0), cached,
        usage.get("cache_write_input_tokens"), usage.get("output_tokens"), 0, usage,
    )


def _codex_events(event: Mapping[str, Any], context: TurnContext) -> list[dict[str, Any]]:
    """Codex ``exec --json``: item events, then the completed turn's usage."""
    kind = str(event.get("type") or "")
    if kind == "turn.completed":
        return [_turn_end(_codex_usage(event.get("usage")))]
    if kind == "turn.failed":
        # NF-2026-01232: the provider's own reason, e.g. its HTTP 400 message.
        return [_turn_error("provider", str(_mapping(event.get("error")).get("message") or "turn_failed"))]
    item = _mapping(event.get("item"))
    item_type = str(item.get("type") or "")
    if not kind.startswith("item.") or not item_type:
        return []
    if item_type in ("agent_message", "agentMessage"):
        return _assistant_text(item.get("text"))
    call_id, done = str(item.get("id") or ""), kind == "item.completed"
    if item_type == "command_execution":
        code = item.get("exit_code") if done else None
        status = "running" if not done else ("completed" if code == 0 else "failed")
        return [_command(
            call_id, str(item.get("command") or ""), status=status, exit_code=code,
            output=str(item.get("aggregated_output") or ""),
        )]
    if item_type == "file_change":
        changes = item.get("changes") if done else None
        return [
            {"type": "file_change", "payload": {
                "call_id": call_id, "path": str(change.get("path") or ""),
                "kind": str(change.get("kind") or "update"), "diff": "", "added": 0, "removed": 0,
            }}
            for change in changes or [] if isinstance(change, Mapping)
        ]
    if item_type not in _CODEX_TOOL_ITEMS:
        return []
    if done:
        output = item.get("aggregated_output") or item.get("output") or item.get("result")
        return [_tool_result(call_id, item_type, output, str(item.get("status") or "") == "failed")]
    return [_tool_call(call_id, item_type, item.get("arguments"))]


def _opencode_call_id(part: Mapping[str, Any]) -> str:
    return str(part.get("id") or part.get("callID") or part.get("partID") or "")


def _opencode_command(
    call_id: str, request: Mapping[str, Any], state: Mapping[str, Any], status: str,
) -> dict[str, Any]:
    metadata = _mapping(state.get("metadata"))
    nested = _mapping(metadata.get("metadata"))
    code = nested.get("exit") if "exit" in nested else metadata.get("exit")
    code = code if isinstance(code, int) else None
    if status == "error":
        shown = "failed"
    elif status != "completed":
        shown = "running"
    elif code is not None and code != 0:
        shown = "failed"
    else:
        shown = "completed"
    return _command(
        call_id, str(request.get("command") or ""), status=shown, exit_code=code,
        output=str(state.get("output") or ""),
    )


def _opencode_tool(part: Mapping[str, Any], context: TurnContext) -> list[dict[str, Any]]:
    state = _mapping(part.get("state"))
    name = str(part.get("tool") or part.get("name") or state.get("title") or "tool")
    call_id = _opencode_call_id(part)
    status = str(state.get("status") or "")
    done, failed = status in ("completed", "error"), status == "error"
    request = _mapping(state.get("input") or part.get("input"))
    if name in ("shell", "bash"):
        return [_opencode_command(call_id, request, state, status)]
    if name in ("write", "edit") and status == "completed":
        path = str(request.get("path") or request.get("filePath") or "")
        if name == "write":
            output = str(state.get("output") or "")
            kind = "add" if output.startswith("Created") else "update"
            return [_file_change(call_id, path, [("", str(request.get("content") or ""))], kind)]
        pairs = [(str(request.get("oldString") or ""), str(request.get("newString") or ""))]
        return [_file_change(call_id, path, pairs, "update")]
    if not done:
        context.calls[call_id] = {"name": name}
        return [_tool_call(call_id, name, request or part.get("input"))]
    seen_running = context.calls.pop(call_id, None) is not None
    result = _tool_result(call_id, name, state.get("output"), failed)
    return [result] if seen_running else [_tool_call(call_id, name, request or part.get("input")), result]


def _opencode_events(event: Mapping[str, Any], context: TurnContext) -> list[dict[str, Any]]:
    """OpenCode ``run --format json`` and part updates: text, thinking, tools, then step finish."""
    kind = str(event.get("type") or "").strip().lower()
    part = _mapping(event.get("part"))
    part_type = str(part.get("type") or "").strip().lower().replace("-", "_")
    if kind.endswith("part.updated") or kind.endswith("part_updated"):
        kind = part_type
    elif part_type and kind not in ("text", "reasoning", "tool", "step_finish", "step_start"):
        kind = part_type
    if kind in ("step_finish", "stepfinish"):
        tokens = _mapping(part.get("tokens") or event.get("tokens"))
        if tokens:
            steps = context.steps
            if not steps:
                steps.update(count=0, input=0, cache_read=0, cache_write=0, output=0)
            cache = _mapping(tokens.get("cache"))
            steps["count"] += 1
            steps["input"] += _count(tokens.get("input"))
            steps["cache_read"] += _count(cache.get("read"))
            steps["cache_write"] += _count(cache.get("write"))
            steps["output"] += _count(tokens.get("output"))
            steps["last"] = dict(tokens)
        return []
    if kind == "text":
        return _assistant_text(part.get("text") or event.get("text"))
    if kind in ("reasoning", "thinking"):
        return _reasoning(part.get("text") or part.get("thinking") or event.get("text"))
    return _opencode_tool(part, context) if kind == "tool" else []


REASONING_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


# ponytail: per process and resets on server restart.
_CLAUDE_THINKING_DISPLAY_REJECTED = False


def manager_stream_tokens(backend_id: str, level: str = "") -> list[str]:
    """Flags that make a manager turn show thinking and honor a chosen depth.

    OpenCode hides thinking blocks unless ``--thinking`` is set. Claude's
    stream stays dark until partial messages are requested, and in ``-p
    stream-json`` mode it leaves the thinking display to the API default, which
    for claude-opus-5 is omitted: thinking blocks arrive with empty text
    (measured on CLI 2.1.280: 6 blocks, 0 chars; with ``--thinking-display
    summarized``: 5 blocks, 5995 chars). Effort tokens are added only for a
    level the CLI documents.

    When a claude turn's CLI rejects ``--thinking-display`` (see
    ``CliManagerBackend._turn``), this memo drops the flag for that one
    immediate retry and for every later turn in this process, while keeping
    ``--include-partial-messages`` and any ``--effort`` handling unchanged.
    """

    cleaned = str(level or "").strip().lower()
    if cleaned not in REASONING_LEVELS:
        cleaned = ""
    if backend_id == "opencode_cli":
        return ["--thinking"]
    if backend_id == "claude_cli":
        tokens = ["--include-partial-messages"]
        if not _CLAUDE_THINKING_DISPLAY_REJECTED:
            tokens += ["--thinking-display", "summarized"]
        if cleaned:
            tokens.extend(("--effort", cleaned))
        return tokens
    if backend_id == "codex_cli" and cleaned:
        return ["-c", f'model_reasoning_effort="{cleaned}"']
    return []


def apply_manager_stream_tokens(backend_id: str, argv: list[str], level: str = "") -> list[str]:
    tokens = manager_stream_tokens(backend_id, level)
    if not tokens:
        return list(argv)
    if backend_id == "opencode_cli" and argv:
        return [*argv[:-1], *tokens, argv[-1]]
    if backend_id == "codex_cli" and argv and argv[-1] == "-":
        return [*argv[:-1], *tokens, "-"]
    return [*argv, *tokens]


_TRANSLATORS: Mapping[str, Callable[[Mapping[str, Any], TurnContext], list[dict[str, Any]]]] = MappingProxyType(
    {
        "claude_cli": _claude_events,
        "codex_cli": _codex_events,
        "opencode_cli": _opencode_events,
    }
)


def flush(backend_id: str, context: TurnContext) -> list[dict[str, Any]]:
    """The turn's closing events a backend's own stream does not supply by itself.

    Only OpenCode needs this: its ``step_finish`` lines no longer carry their own
    ``turn_end`` (W1-P1b), so their usage is summed here into exactly one. Claude
    and Codex end their own turn with their own final event, so they get ``[]``.
    """
    if backend_id != "opencode_cli":
        return []
    steps = context.steps
    if not steps:
        return [_turn_end(None)]
    usage = _usage(
        steps["input"], steps["cache_read"], steps["cache_write"], steps["output"], 0,
        {"steps": steps["count"], "last": steps.get("last") or {}},
    )
    return [_turn_end(usage)]


def _provider_error(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """The provider's own failure report, or ``None`` when the event is not one."""
    if str(event.get("type") or "") != "error" and not event.get("is_error"):
        return None
    reported = event.get("error")
    if isinstance(reported, Mapping):
        # NF-2026-01232: the provider's message, not only its error class name.
        said = reported.get("message") or _mapping(reported.get("data")).get("message")
        detail = ": ".join(str(part) for part in (reported.get("name"), said) if part) or "provider_error"
    else:
        # A Claude ``result`` failure carries its text in ``result`` and the
        # subtype ``success``; that word is never a failure detail.
        texts = (event.get(key) for key in ("error", "result", "message"))
        detail = next((t for t in texts if isinstance(t, str) and t.strip()), None)
        subtype = event.get("subtype")
        if detail is None and isinstance(subtype, str) and subtype.strip():
            detail = subtype
        if detail is None or detail.strip().lower() == "success":
            detail = "provider_error"
    return _turn_error("provider", str(detail))


def translate(backend_id: str, event: Any, context: TurnContext | None = None) -> list[dict[str, Any]]:
    """One provider event as loop events; an unrecognized one yields nothing at all.

    ``context`` pairs a tool call with its result across lines; one turn shares one.
    """
    if not isinstance(event, Mapping):
        return []
    failure = _provider_error(event)
    if failure is not None:
        return [failure]
    translator = _TRANSLATORS.get(backend_id)
    return translator(event, context if context is not None else TurnContext()) if translator is not None else []


def conversation_id_of(event: Mapping[str, Any]) -> str:
    """The provider conversation id this event reports, or ``""`` when it reports none."""
    for key in _CONVERSATION_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _maybe_record_claude_resolution(
    backend_id: str, model: str, repo: Path, event: Mapping[str, Any]
) -> None:
    """Best effort: a resolved alias is worth recording, never worth failing a turn for."""

    if backend_id != "claude_cli" or model not in cli_model_discovery.CLAUDE_CLI_ALIASES:
        return
    if event.get("type") != "system" or event.get("subtype") != "init":
        return
    resolved = str(event.get("model") or "").strip()
    if not resolved:
        return
    try:
        cli_model_discovery.record_claude_resolution(repo, model, resolved)
    except Exception:  # noqa: BLE001 -- recording is observability, never a turn failure
        pass


def resume_argv(backend_id: str, argv: Sequence[str], conversation_id: str) -> list[str]:
    """Insert this CLI's resume tokens where its own grammar requires them."""
    tokens = list(argv)
    if not conversation_id or backend_id not in _RESUME_GRAMMAR:
        return tokens
    index, flag = _RESUME_GRAMMAR[backend_id]
    if backend_id == "codex_cli":
        tokens = _codex_resume_options(tokens)
    return tokens[:index] + [flag, conversation_id] + tokens[index:]


def _codex_resume_options(tokens: list[str]) -> list[str]:
    """``codex exec resume`` refuses ``-s`` and ``-C`` (measured, 0.156.1):
    the sandbox moves to its ``-c sandbox_mode=`` spelling, and ``-C`` is
    dropped because the turn already runs with the plan's cwd."""

    out: list[str] = []
    rest = iter(tokens)
    for token in rest:
        if token == "-s":
            out.extend(("-c", f'sandbox_mode="{next(rest, "")}"'))
        elif token == "-C":
            next(rest, None)
        else:
            out.append(token)
    return out


def _spawn_cli(
    argv: Sequence[str],
    cwd: str | None,
    stdin_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Start one non-interactive CLI turn in its own process group; argv list, no shell.

    ``env`` seat bindings MERGE over the inherited environment; ``None``
    inherits unchanged. A wholesale replace kills the child in milliseconds
    (measured: Bun/opencode dies on missing SystemRoot with no provider
    signal) -- the seat is the owner's own, so inheritance matches the
    claude seat; scoping lives in the MCP allowlists, not env scrubbing.
    With ``cwd`` given, ``PWD`` is bound to ``str(cwd)`` after the merge, so
    a child that resolves its directory from ``PWD`` works in ``cwd``.
    """
    grouping: dict[str, Any] = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if platform_io.is_windows()
        else {"start_new_session": True}
    )
    # The argv list comes from build_runtime_command; shell=False is the default
    # and is never overridden, so no provider text is ever interpreted as a command.
    child_env = dict(os.environ, **dict(env)) if env is not None else None
    if cwd is not None:
        # Popen(cwd=) leaves the inherited PWD stale; OpenCode takes its directory from PWD.
        child_env = {**(child_env if child_env is not None else os.environ), "PWD": str(cwd)}
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=child_env,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **grouping,
    )
    if stdin_text is not None:

        def _feed() -> None:
            try:
                process.stdin.write(stdin_text)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        threading.Thread(target=_feed, daemon=True).start()
    return process

def _decode(raw: str) -> Mapping[str, Any] | None:
    """One stdout line as a JSON object; anything else is skipped, never fatal."""
    line = raw.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _reap(process: Any) -> int:
    """Wait briefly for an exit code; an unreapable child reports -1, never hangs."""
    try:
        return int(process.wait(timeout=REAP_TIMEOUT_SECONDS))
    except (subprocess.TimeoutExpired, OSError, ValueError, TypeError):
        return -1


def _end_process(process: Any) -> None:
    """Stop one CLI process tree through the platform facade; never raises."""
    if process is None or process.poll() is not None:
        return
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0:
        platform_io.terminate_process_tree(pid)
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            return
    _reap(process)


class _StderrTail:
    """Drain the child's stderr on its own thread, bounded to the last lines.

    A CLI that fills its stderr pipe while the turn is still reading stdout
    would otherwise block forever, so the drain never waits on the reader.
    """

    MAX_LINES = 40

    def __init__(self, stream: Any) -> None:
        self._lines: list[str] = []
        self._thread: threading.Thread | None = None
        if stream is None:
            return
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream: Any) -> None:
        try:
            for line in stream:
                self._lines.append(line)
                del self._lines[: -self.MAX_LINES]
        except (OSError, ValueError):
            return

    def text(self) -> str:
        if self._thread is not None:
            self._thread.join(timeout=REAP_TIMEOUT_SECONDS)
        return "".join(self._lines)[-MAX_STDERR_CHARS:]


class CliManagerBackend:
    """One manager conversation on one agentic CLI, one non-interactive turn at a time.

    ``start`` only records the brief: spawning is deferred to the first ``send``
    so a started-but-silent session costs nothing. Each ``send`` runs one turn
    and yields its events as the lines arrive; the first turn carries the brief
    with the message, and every later turn resumes the conversation id captured
    from the stream. A turn that outruns ``timeout_seconds`` has its process
    killed and reports exactly one ``error`` event, as does a non-zero exit, and
    the backend stays closeable either way.
    """

    def __init__(
        self,
        backend_id: str,
        model: str,
        repo: Path | str,
        *,
        mcp_config_path: Path | str | None = None,
        timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
        plan_builder: Callable[..., Any] = runtime_adapters.build_manager_command,
        spawn: Callable[..., Any] = _spawn_cli,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        if backend_id not in MANAGER_BACKEND_IDS:
            raise ManagerLoopError(f"manager_backend_unsupported:{backend_id}")
        if mcp_config_path is not None and backend_id not in _MCP_CONFIG_FLAGS:
            raise ManagerLoopError(f"manager_backend_mcp_config_unsupported:{backend_id}")
        if not float(timeout_seconds) > 0:
            raise ValueError("timeout_seconds must be positive")
        self.backend_id = backend_id
        self.model = model
        self.repo = Path(repo)
        self.mcp_config_path = mcp_config_path
        self.timeout_seconds = float(timeout_seconds)
        self._plan_builder = plan_builder
        self._spawn = spawn
        # An empty mapping means "no seat bindings": inherit, never spawn
        # with a blank environment (a bare env kills the child in milliseconds
        # with no provider signal -- measured as worker_process_failure).
        self._extra_env = dict(extra_env) if extra_env else None
        self._brief = ""
        self._conversation_id = ""
        self.reasoning_level = ""
        # Stored image paths for the next turn only; the orchestrator sets them per send.
        self.images: tuple[str, ...] = ()
        self._process: Any = None
        self._slot = threading.Lock()
    @property
    def conversation_id(self) -> str:
        """The provider conversation later turns resume, or ``""`` before the first turn."""
        return self._conversation_id

    def is_running(self) -> bool:
        """Whether this backend currently owns a live CLI process."""
        process = self._process
        return process is not None and process.poll() is None

    def start(self, brief: str) -> str:
        """Record ``brief`` as the first turn's preamble; do not spawn anything yet."""
        self._brief = str(brief or "")
        return f"cli:{self.backend_id}:{self.model}"

    def send(self, message: str) -> Iterator[dict[str, Any]]:
        """Run one CLI turn for ``message`` and yield its events while it runs."""
        return self._turn(str(message))

    def close(self) -> None:
        """End the turn's process if one is alive; safe to call any number of times."""
        with self._slot:
            process, self._process = self._process, None
        _end_process(process)

    def argv_for(self, plan: Any) -> list[str]:
        """This turn's argv: the adapter plan, minus what a manager turn cannot keep."""
        dropped = _DROPPED_TOKENS.get(self.backend_id, frozenset())
        argv = [token for token in plan.argv if token not in dropped]
        flag = _MCP_CONFIG_FLAGS.get(self.backend_id)
        if flag is not None and self.mcp_config_path is not None:
            argv.extend((flag, str(self.mcp_config_path)))
        return resume_argv(self.backend_id, argv, self._conversation_id)

    def _turn(self, message: str) -> Iterator[dict[str, Any]]:
        images, self.images = tuple(self.images), ()
        if images and self.backend_id == "opencode_cli":
            yield _turn_error("images", f"manager_images_unsupported:{self.backend_id}")
            return
        # codex splits an --image value on ',' (measured on codex-cli 0.159.3).
        if self.backend_id == "codex_cli" and any("," in path for path in images):
            yield _turn_error("images", f"manager_image_path_unsupported:{self.backend_id}")
            return
        if images and self.backend_id == "claude_cli":
            # Claude takes no image flag in print mode; its Read tool opens the files.
            message = message + "\n\n" + "\n".join(f"Attached image: {path}" for path in images)
        resuming = bool(self._conversation_id)
        prompt = message if resuming else f"{self._brief}\n\n{message}".strip()
        plan = self._plan_builder(self.backend_id, prompt, self.repo, model=self.model)
        if not getattr(plan, "launchable", False) or not plan.argv:
            yield _turn_error("launch_plan", plan.validation_reason or "plan_not_launchable")
            return
        try:
            argv = apply_manager_stream_tokens(self.backend_id, self.argv_for(plan), self.reasoning_level)
            if images and self.backend_id == "codex_cli":
                # The attached --image=<path> form, before the positional prompt
                # (`-`): a bare `-i <path> -` swallows the `-` as one more image.
                argv = [*argv[:-1], *(f"--image={path}" for path in images), argv[-1]]
            cwd = plan.cwd
            stdin_text = getattr(plan, "stdin_text", None)
            if self._extra_env is None:
                process = self._spawn(argv, cwd, stdin_text)
            else:
                process = self._spawn(argv, cwd, stdin_text, dict(self._extra_env))
        except OSError as exc:
            yield _turn_error("spawn", f"{type(exc).__name__}: {exc}")
            return
        with self._slot:
            self._process = process
        expired = threading.Event()
        tail = _StderrTail(getattr(process, "stderr", None))

        def cut_off() -> None:
            expired.set()
            _end_process(process)

        watchdog = threading.Timer(self.timeout_seconds, cut_off)
        watchdog.start()
        context = TurnContext()
        saw_error = False
        streamed = False
        try:
            for raw in process.stdout or ():
                event = _decode(raw)
                if event is None:
                    continue
                self._conversation_id = self._conversation_id or conversation_id_of(event)
                _maybe_record_claude_resolution(self.backend_id, self.model, self.repo, event)
                for translated in translate(self.backend_id, event, context):
                    if translated["type"] == "error":
                        # NF-2026-01232: one failed turn is one error event; the
                        # provider's first report (Codex: error, then turn.failed) wins.
                        if saw_error:
                            continue
                        saw_error = True
                    streamed = True
                    yield translated
        finally:
            watchdog.cancel()
            code = _reap(process)
            stderr = tail.text()
            with self._slot:
                if self._process is process:
                    self._process = None
        if saw_error:
            return
        if (
            code != 0 and not streamed and not expired.is_set()
            and self.backend_id == "claude_cli" and "--thinking-display" in argv
            and "unknown option" in stderr.lower() and "--thinking-display" in stderr
        ):
            # NF-2026-01353: retry once; the memo drops the flag (images are already in message).
            global _CLAUDE_THINKING_DISPLAY_REJECTED
            _CLAUDE_THINKING_DISPLAY_REJECTED = True
            yield from self._turn(message)
            return
        if expired.is_set():
            yield _turn_error("timeout", f"manager_turn_timeout_seconds={self.timeout_seconds:g}")
        elif code != 0:
            outcome = runtime_adapters.classify_provider_outcome(exit_code=code, stderr=stderr)
            reason = str(outcome.get("reason") or f"exit_code={code}")
            # NF-2026-01232: with no provider error line, its stderr is its own text.
            said = str(outcome.get("provider_message") or "").strip()
            room = MAX_ERROR_DETAIL_CHARS - len(reason) - 2
            yield _turn_error(
                str(outcome.get("outcome") or "worker_failed"),
                f"{reason}: {said[-room:]}" if said and room > 0 else reason,
            )
        else:
            yield from flush(self.backend_id, context)


def _opencode_discovered_models() -> list[str]:
    """Bound ``opencode models`` probe; [] on any failure.

    Lives here, not in :mod:`cli_model_discovery`, because that module never
    spawns a process by contract -- every function there reads a file a CLI
    already wrote. Bounds mirror ``repo_policy._list_opencode_models``.
    """
    executable = shutil.which("opencode")
    if not executable:
        return []
    try:
        completed = subprocess.run(
            [executable, "models"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    payload = completed.stdout or b""
    if not payload:
        payload = completed.stderr or b""
    if len(payload) > 64 * 1024:
        return []
    return workforce_catalog.parse_opencode_models_output(payload)


def cli_discovers_model(repo: Path | str, backend_id: str, model: str) -> bool:
    """Whether the CLI itself offers ``model`` (see :mod:`cli_model_discovery`).

    The Manager picker lists these discovered models, so a start the static
    workforce registry does not declare is still a model the CLI can run.
    """

    if backend_id == "codex_cli":
        return any(entry["model"] == model for entry in cli_model_discovery.codex_models())
    if backend_id == "claude_cli":
        return any(entry["model"] == model for entry in cli_model_discovery.claude_models(repo))
    if backend_id == "opencode_cli":
        return model in _opencode_discovered_models()
    return False
def manager_backend_factory(
    repo: Path | str,
    *,
    mcp_config_path: Path | str | None = None,
    declares_route: Callable[..., bool] = workforce_catalog.catalog_declares_route,
    seat_env_provider: Callable[[str, str], Mapping[str, str] | None] | None = None,
    **options: Any,
) -> Callable[[str, str], CliManagerBackend]:
    """The ``backend_factory`` :class:`~aiworkhub.manager_loop.ManagerOrchestrator` takes.

    ``backend_id`` is resolved against the three manager CLIs and then against
    the repository's existing workforce registry, so a backend the catalog does
    not declare for this model is refused rather than launched. Every refusal is
    a named :class:`~aiworkhub.manager_loop.ManagerLoopError`.

    ``seat_env_provider``, when given, is called as
    ``provider(backend_id, model)`` at build time and its mapping becomes the
    seat's child-process environment (seat MCP bindings); ``None`` inherits,
    preserving the pre-seat behavior exactly.
    """

    root = Path(repo)

    def build(backend_id: str, model: str) -> CliManagerBackend:
        if backend_id not in MANAGER_BACKEND_IDS:
            raise ManagerLoopError(f"manager_backend_unsupported:{backend_id}")
        if not declares_route(root, backend_id, model) and not cli_discovers_model(
            root, backend_id, model
        ):
            raise ManagerLoopError(f"manager_backend_unavailable:{backend_id}:{model}")
        extra_env = seat_env_provider(backend_id, model) if seat_env_provider is not None else None
        return CliManagerBackend(
            backend_id, model, root, mcp_config_path=mcp_config_path, extra_env=extra_env, **options
        )

    return build


MANAGER_SEAT_RUNTIME_DIRNAME = "manager-seat"


def _write_0600(path: Path, data: bytes) -> None:
    """Write ``data`` owner-only, refusing symlinks (worker MCP writer shape)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def provision_manager_seat_env(
    repo: Path | str,
    backend_id: str,
    *,
    python_executable: str,
    package_import_root: Path | str,
) -> dict[str, str]:
    """Seat MCP bindings for one manager backend: config files plus child env.

    The seat identity binding (``AIWORKHUB_MANAGER_SEAT_BACKEND`` and
    ``AIWORKHUB_MANAGER_SEAT_TOKEN``) is minted once per backend into a
    0600 token file under ``.aiworkhub/runtime/manager-seat/`` and reused on
    every later provision call, so the env token and the file always agree.
    ``claude_cli`` needs no config file: it loads the project's ``.mcp.json``
    ``AIWorkHub`` server with ``cwd=repo``, and Claude passes its environment
    into that stdio server, so it returns exactly those two seat keys.
    ``codex_cli`` gets an isolated ``CODEX_HOME`` holding a manager
    ``config.toml`` (0600). ``opencode_cli`` gets the manager config through
    its config-content env (project config disabled). The embedded server env
    carries the repo binding plus the write gate (create needs it); launch is
    deliberately absent. Thread and episode identity is never written here:
    it flows from the launching gated child through process env inheritance,
    and a forged token fails closed against the 0600 seat token file.
    """
    if backend_id not in MANAGER_BACKEND_IDS:
        raise ManagerLoopError(f"manager_backend_unsupported:{backend_id}")
    root = Path(repo).resolve()
    seat_dir = root / ".aiworkhub" / "runtime" / MANAGER_SEAT_RUNTIME_DIRNAME
    token = _read_or_mint_manager_seat_token(seat_dir / f"seat-token-{backend_id}")
    server_env = {
        "AIWORKHUB_REPO": str(root),
        "AIWORKHUB_REPO_ROOT": str(root),
        "AIWORKHUB_ALLOW_WRITES": "1",
        "PYTHONPATH": str(package_import_root),
        "AIWORKHUB_MANAGER_SEAT_BACKEND": backend_id,
        "AIWORKHUB_MANAGER_SEAT_TOKEN": token,
    }
    if backend_id == "claude_cli":
        return {
            "AIWORKHUB_MANAGER_SEAT_BACKEND": backend_id,
            "AIWORKHUB_MANAGER_SEAT_TOKEN": token,
        }
    if backend_id == "codex_cli":
        codex_home = seat_dir / "codex-home"
        config_path = codex_home / "config.toml"
        toml_text = runtime_adapters.build_manager_codex_config_toml(
            python_executable=python_executable,
            launch_args=[],
            environment=server_env,
        )
        _write_0600(config_path, toml_text.encode("utf-8"))
        return {"CODEX_HOME": str(codex_home)}
    if backend_id == "opencode_cli":
        config = runtime_adapters.build_opencode_manager_mcp_config(
            [python_executable, "-m", "aiworkhub.server"],
            environment=server_env,
        )
        return {
            runtime_adapters.OPENCODE_WORKER_CONFIG_ENV: (
                runtime_adapters.serialize_opencode_manager_config(config)
            ),
            runtime_adapters.OPENCODE_DISABLE_PROJECT_CONFIG_ENV: "1",
        }
    raise ManagerLoopError(f"manager_seat_mcp_unsupported:{backend_id}")


def _read_or_mint_manager_seat_token(token_path: Path) -> str:
    """Return the backend's seat token, minting a 0600 file on first use."""
    import secrets

    try:
        existing = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if len(existing) == 64 and all(c in "0123456789abcdef" for c in existing):
        return existing
    token = secrets.token_hex(32)
    _write_0600(token_path, token.encode("utf-8"))
    return token
