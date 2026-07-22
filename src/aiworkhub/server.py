from __future__ import annotations

import inspect
import json
import sys
import types
import typing
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:
    # ------------------------------------------------------------------
    # Bounded, dependency-free MCP stdio fallback.
    #
    # Used automatically whenever the optional `mcp` PyPI package is not
    # importable -- exactly the situation a VS Code-bundled extension-local
    # Python runtime hits when it runs with user/site-packages disabled and
    # no network install. Implements the subset of the MCP stdio wire
    # protocol this server's callers actually use (initialize /
    # notifications/initialized / tools/list / tools/call) as
    # newline-delimited JSON-RPC 2.0 over stdio -- the same framing the real
    # `mcp` package and this repo's own stdio clients already speak, so the
    # extension host and mcp_stdio_client_smoke.py-style clients need no
    # changes to talk to it.
    #
    # Fail-closed by construction: fixed request/response byte caps,
    # structured JSON-RPC errors on any malformed/oversized/unknown input,
    # and tool dispatch only through the fixed name->callable map each
    # `@mcp.tool()` decorator populated at import time -- never
    # eval/exec/dynamic import driven by client-supplied data.
    # ------------------------------------------------------------------

    _FALLBACK_PROTOCOL_VERSION = "2024-11-05"
    _FALLBACK_MAX_LINE_BYTES = 8 * 1024 * 1024
    _FALLBACK_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

    class _StdioProtocolError(Exception):
        """A structured JSON-RPC error to report back to the client."""

        def __init__(self, code: int, message: str):
            super().__init__(message)
            self.code = code
            self.message = message

    def _stdio_json_type(annotation: Any) -> str:
        if annotation is inspect.Signature.empty:
            return "string"
        origin = typing.get_origin(annotation)
        if origin in (typing.Union, types.UnionType):
            args = [a for a in typing.get_args(annotation) if a is not type(None)]
            return _stdio_json_type(args[0]) if len(args) == 1 else "string"
        if origin in (list, tuple):
            return "array"
        if origin is dict:
            return "object"
        mapping = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
        return mapping.get(annotation, "string")

    def _stdio_schema_for(func: Any) -> dict[str, Any]:
        try:
            sig = inspect.signature(func)
        except (TypeError, ValueError):
            return {"type": "object", "properties": {}, "additionalProperties": True}
        properties: dict[str, Any] = {}
        required: list[str] = []
        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            properties[pname] = {"type": _stdio_json_type(param.annotation)}
            if param.default is inspect.Signature.empty:
                required.append(pname)
        schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            schema["required"] = required
        return schema

    def _stdio_tools_list(tools: dict[str, Any]) -> dict[str, Any]:
        entries = []
        for name, func in tools.items():
            doc = inspect.getdoc(func) or ""
            entries.append({
                "name": name,
                "description": doc.strip().splitlines()[0] if doc.strip() else "",
                "inputSchema": _stdio_schema_for(func),
            })
        return {"tools": entries}

    def _stdio_tools_call(tools: dict[str, Any], params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise _StdioProtocolError(-32602, "invalid_params")
        name = params.get("name")
        if not isinstance(name, str) or name not in tools:
            raise _StdioProtocolError(-32602, f"unknown_tool:{name!r}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise _StdioProtocolError(-32602, "arguments_must_be_object")
        func = tools[name]
        try:
            allowed = set(inspect.signature(func).parameters.keys())
        except (TypeError, ValueError):
            allowed = set(arguments.keys())
        unexpected = sorted(set(arguments.keys()) - allowed)
        if unexpected:
            raise _StdioProtocolError(-32602, f"unexpected_arguments:{unexpected}")
        try:
            result = func(**arguments)
        except Exception as exc:  # tool-level failure -> MCP error content, not a crash
            return {"content": [{"type": "text", "text": str(exc)[:2000]}], "isError": True}
        structured = result if isinstance(result, dict) else {"value": result}
        text = json.dumps(result, ensure_ascii=False, default=str)
        return {"content": [{"type": "text", "text": text}], "structuredContent": structured}

    def _stdio_dispatch(name: str, tools: dict[str, Any], method: Any, params: Any) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": _FALLBACK_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": name, "version": __version__},
            }
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return _stdio_tools_list(tools)
        if method == "tools/call":
            return _stdio_tools_call(tools, params)
        raise _StdioProtocolError(-32601, f"method_not_found:{method}")

    def _stdio_write_message(stream: Any, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, default=str)
        if len(payload.encode("utf-8")) > _FALLBACK_MAX_RESPONSE_BYTES:
            message = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32603, "message": "response_too_large"},
            }
            payload = json.dumps(message, ensure_ascii=False)
        stream.write(payload + "\n")
        stream.flush()

    def _run_stdio_fallback_server(name: str, tools: dict[str, Any]) -> None:
        # Read from the binary buffer with an explicit limit.  Calling the
        # text wrapper's unbounded readline() and checking the length only
        # afterwards would already have allocated an arbitrarily large client
        # line.  The one-byte look-ahead detects overflow; the remainder is
        # drained in bounded chunks so the next request keeps its framing.
        stdin = sys.stdin.buffer
        stdout = sys.stdout
        while True:
            line = stdin.readline(_FALLBACK_MAX_LINE_BYTES + 1)
            if line == b"":
                return
            oversized = len(line) > _FALLBACK_MAX_LINE_BYTES
            if oversized and not line.endswith(b"\n"):
                while True:
                    remainder = stdin.readline(_FALLBACK_MAX_LINE_BYTES + 1)
                    if remainder == b"" or remainder.endswith(b"\n"):
                        break
            if oversized:
                _stdio_write_message(stdout, {
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "request_too_large"},
                })
                continue
            line = line.rstrip(b"\r\n")
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                _stdio_write_message(stdout, {
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse_error"},
                })
                continue
            if not isinstance(message, dict):
                _stdio_write_message(stdout, {
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "invalid_request"},
                })
                continue
            has_id = "id" in message
            msg_id = message.get("id")
            method = message.get("method")
            params = message.get("params") if message.get("params") is not None else {}
            try:
                result = _stdio_dispatch(name, tools, method, params)
            except _StdioProtocolError as exc:
                if has_id:
                    _stdio_write_message(stdout, {
                        "jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": exc.code, "message": exc.message},
                    })
                continue
            except Exception as exc:  # defensive: never let one bad request kill the loop
                if has_id:
                    _stdio_write_message(stdout, {
                        "jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32603, "message": f"internal_error:{type(exc).__name__}"},
                    })
                continue
            if has_id:
                _stdio_write_message(stdout, {"jsonrpc": "2.0", "id": msg_id, "result": result})

    class FastMCP:  # type: ignore[no-redef]
        """Bounded stdlib MCP stdio server, used when the `mcp` package is absent."""

        def __init__(self, name: str):
            self.name = name
            self._tools: dict[str, Any] = {}

        def tool(self, *args: Any, **kwargs: Any):
            def decorate(func: Any) -> Any:
                tool_name = str(kwargs.get("name") or func.__name__)
                self._tools[tool_name] = func
                return func

            return decorate

        @property
        def registered_tools(self) -> list[str]:
            return list(self._tools.keys())

        def run(self) -> None:
            _run_stdio_fallback_server(self.name, self._tools)

from . import __version__
from . import agent_tool_instruction_mcp
from . import cli_adapter_readonly_tool
from . import completion_inbox
from . import cost_ledger
from . import core
from . import dashboard_mcp_app
from . import deepseek_credentials
from . import launch_queue_contract
from . import launch_queue_persist
from . import manager_ai_tools
from . import process_launcher
from . import review_summarizer
from . import stale_recovery
from . import task_engine


mcp = FastMCP("AIWorkHub MCP")


@mcp.tool()
def aiworkhub_manager_bootstrap() -> dict[str, Any]:
    """READ-ONLY: explain this repository's manager workflow and callback lanes."""

    return core.manager_bootstrap()


@mcp.tool()
def aiworkhub_manager_source_graph_query(
    mode: str,
    query: str,
    budget: int = 64,
    target: str | None = None,
    bundle_type: str = "explore",
) -> dict[str, Any]:
    """MANAGER READ: bounded canonical Source Graph query for this repository."""

    return manager_ai_tools.source_graph_query(
        mode=mode, query=query, budget=budget, target=target, bundle_type=bundle_type
    )


@mcp.tool()
def aiworkhub_manager_session_current_state(
    topic: str = "management", limit: int = 12
) -> dict[str, Any]:
    """MANAGER READ: bounded canonical Session Manager current state."""

    return manager_ai_tools.session_current_state(topic=topic, limit=limit)


@mcp.tool()
def aiworkhub_manager_ai_memory_search(query: str, limit: int = 8) -> dict[str, Any]:
    """MANAGER READ: bounded canonical AI Memory search."""

    return manager_ai_tools.ai_memory_search(query=query, limit=limit)


@mcp.tool()
def aiworkhub_manager_kb_search(query: str, limit: int = 8) -> dict[str, Any]:
    """MANAGER READ: bounded canonical KB search."""

    return manager_ai_tools.kb_search(query=query, limit=limit)


@mcp.tool()
def aiworkhub_manager_kb_get(key: str) -> dict[str, Any]:
    """MANAGER READ: exact canonical KB entry lookup."""

    return manager_ai_tools.kb_get(key=key)


@mcp.tool()
def aiworkhub_manager_kb_related(key: str) -> dict[str, Any]:
    """MANAGER READ: bounded canonical KB relation lookup."""

    return manager_ai_tools.kb_related(key=key)


@mcp.tool()
def aiworkhub_task_create(
    task_id: str,
    title: str,
    runner: str,
    topic: str,
    objective: str,
    acceptance: list[str],
    allowed_writes: list[str],
    forbidden: list[str] | None = None,
    required_outputs: list[str] | None = None,
    validation: list[str] | None = None,
    priority: str = "normal",
    callback_required: bool = True,
    task_type: str = "code",
) -> dict[str, Any]:
    """MANAGER WRITE: create one new canonical repo-local task card.

    The live manager session supplies callback provider/origin identity;
    callers cannot route a task into another chat or overwrite an existing id.
    """

    return core.create_task(
        task_id=task_id,
        title=title,
        runner=runner,
        topic=topic,
        objective=objective,
        acceptance=acceptance,
        allowed_writes=allowed_writes,
        forbidden=forbidden,
        required_outputs=required_outputs,
        validation=validation,
        priority=priority,
        callback_required=callback_required,
        task_type=task_type,
    )


@mcp.tool()
def aiworkhub_task_health() -> dict[str, Any]:
    """Check parent AIWorkHub task queue health and write-gate state."""

    result = core.health()
    result["server_version"] = __version__
    return result


@mcp.tool()
def aiworkhub_task_review_queue() -> dict[str, Any]:
    """List tasks waiting for Codex review."""

    return core.review_queue()


@mcp.tool()
def aiworkhub_task_list(status: str = "pending", topic: str | None = None, limit: int = 80) -> dict[str, Any]:
    """List task cards by status and optional topic."""

    return core.list_tasks(status=status, topic=topic, limit=limit)


@mcp.tool()
def aiworkhub_task_show(task_id: str) -> dict[str, Any]:
    """Show a single task card by task_id."""

    return core.show_task(task_id)


@mcp.tool()
def aiworkhub_task_pending_for_runner(runner: str, topic: str | None = None) -> dict[str, Any]:
    """Return pending cards for a specific runner as raw output plus parsed JSONL rows."""

    return core.pending_for_runner(runner=runner, topic=topic)


@mcp.tool()
def aiworkhub_task_auto_pickup(runner: str, topic: str | None = None) -> dict[str, Any]:
    """Write-gated: claim and start the next task for a runner.

    ``core.auto_pickup``'s public schema has no ``task_id`` parameter, so a
    card-scoped one-off runner/topic (an identity not on the static
    ``core.RUNNER_TOPIC_ALLOWLIST``, authorized only to claim the exact
    pending card that already names it -- see
    ``core._check_card_scoped_write_authority``) always failed
    ``card_scoped_task_id_required`` here, even though claim-start is
    already the intended, narrower authority for exactly this identity and
    a caller with only ``(runner, topic)`` structurally cannot supply the
    missing ``task_id`` to satisfy it. When (and only when) that exact
    denial reason comes back, this resolves the single eligible pending
    task_id itself -- the same read-only candidate ``aiworkhub_task_auto_pickup_dryrun``
    already reports -- and claims it through claim-start, the authority path
    this identity already qualifies for. No runner/topic allowlist, batch
    guard, or write gate is loosened: an ineligible identity still gets
    ``card_scoped_task_id_required`` (now with zero eligible candidates) or
    whatever card-scoped denial claim-start already returns.
    """

    result = core.auto_pickup(runner=runner, topic=topic)
    if result.get("ok") or "card_scoped_task_id_required" not in str(result.get("stderr") or ""):
        return result
    if topic is None:
        return result
    dryrun = core.auto_pickup_dryrun(runner=runner, topic=topic)
    candidate_task_id = str(dryrun.get("would_claim_task_id") or "")
    if not candidate_task_id:
        return result
    return task_engine.claim_start_exact(core.repo_root(), candidate_task_id, runner, topic)


@mcp.tool()
def aiworkhub_task_auto_pickup_dryrun(runner: str, topic: str | None = None) -> dict[str, Any]:
    """READ-ONLY: preview which task auto_pickup WOULD claim for a runner/topic.

    Reports the candidate task_id, a compact card, and runner/topic filtering
    counts WITHOUT mutating the parent queue and WITHOUT touching the write gate.
    Never invokes the write-gated auto-pickup command; uses only the read-only
    `export` path. The real claim path (aiworkhub_task_auto_pickup) stays behind
    AIWORKHUB_ALLOW_WRITES; this preview provides no alternate write path
    and cannot bypass that gate.
    """

    return core.auto_pickup_dryrun(runner=runner, topic=topic)


@mcp.tool()
def aiworkhub_task_mark_review(task_id: str) -> dict[str, Any]:
    """Write-gated: mark a worker task as ready for Codex review."""

    return core.mark_review(task_id=task_id)


@mcp.tool()
def aiworkhub_task_mark_done(task_id: str) -> dict[str, Any]:
    """Write-gated: finalize a reviewed task as done."""

    return core.mark_done(task_id=task_id)


@mcp.tool()
def aiworkhub_task_reject_review(
    task_id: str,
    reason: str,
) -> dict[str, Any]:
    """Write-gated Codex action: requeue a reviewed task with exact feedback."""

    return core.reject_review(task_id=task_id, reason=reason)


@mcp.tool()
def aiworkhub_manager_task_archive(
    task_id: str,
    reason: str = "",
) -> dict[str, Any]:
    """MANAGER WRITE: archive a non-processing card without deleting audit history."""

    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed", "task_id": task_id}
    return task_engine.archive_task(
        core.repo_root(),
        task_id,
        actor=core.CODEX_RUNNER,
        reason=reason,
        supersede=False,
    )


@mcp.tool()
def aiworkhub_manager_task_supersede(
    task_id: str,
    reason: str = "",
) -> dict[str, Any]:
    """MANAGER WRITE: supersede an orphaned active card while preserving audit history."""

    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed", "task_id": task_id}
    return task_engine.archive_task(
        core.repo_root(),
        task_id,
        actor=core.CODEX_RUNNER,
        reason=reason,
        supersede=True,
    )


@mcp.tool()
def aiworkhub_task_collision_guard(print_json: bool = True) -> dict[str, Any]:
    """Run the task collision guard for active cards."""

    return core.collision_guard(print_json=print_json)


@mcp.tool()
def aiworkhub_task_usage_report(
    runner: str | None = None,
    topic: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Return task usage report, optionally filtered."""

    return core.usage_report(runner=runner, topic=topic, status=status)


@mcp.tool()
def aiworkhub_task_export_jsonl() -> dict[str, Any]:
    """Write-gated: export SQLite task queue back to JSONL manifest."""

    return core.export_jsonl()


@mcp.tool()
def aiworkhub_task_audit_log_read(max_entries: int = 100) -> dict[str, Any]:
    """Read-only: inspect write-gate audit log summaries.

    Returns counts by tool/action and the last N audit entries.
    Never writes, never enables writes, never launches processes.
    All authority flags are preserved false.
    """

    return core.read_audit_log(max_entries=max_entries)


@mcp.tool()
def aiworkhub_task_review_summarize(
    task_ids: list[str] | None = None,
    batch_label: str | None = None,
) -> dict[str, Any]:
    """READ-ONLY: Inspect review queue, group tasks by topic/runner, return Codex review checklist.

    Calls the B110 review_summarizer core (read-only contract).
    Returns grouped tasks, validation commands, allowed_writes counts,
    and a Codex-ready review checklist shape.

    FROZEN INPUT SCHEMA — no write-gate toggle, no mutation parameters.
    Never calls taskctl done/review/start/auto-pickup/add-card.
    Never mutates queue or audit state.
    Never launches agents or model processes.
    Writes remain default-off regardless of input values.
    """

    result = review_summarizer.summarize_review_queue()
    if task_ids:
        filtered_checklist = [
            c for c in result.get("codex_review_checklist", [])
            if c.get("task_id") in task_ids
        ]
        result["codex_review_checklist"] = filtered_checklist
        result["filtered_by_task_ids"] = True
        result["requested_task_ids"] = task_ids
    if batch_label:
        result["batch_label"] = batch_label
    result["server_tool"] = "aiworkhub_task_review_summarize"
    result["contract"] = "B111_v1_readonly_server_wiring"
    return result


@mcp.tool()
def aiworkhub_task_codex_handoff(
    task_ids: list[str] | None = None,
    batch_label: str | None = None,
) -> dict[str, Any]:
    """READ-ONLY: Codex-ready HANDOFF report for review-queue tasks.

    For each task waiting on Codex review, summarizes task_id, runner, topic,
    allowed_writes, validation commands, derived risks, commit-hygiene status,
    and a scoped stage/finalize command list — WITHOUT mutating the parent
    task queue.

    FROZEN READ-ONLY CONTRACT — no write-gate toggle, no mutation parameters.
    Never calls taskctl done/review/start/auto-pickup/add-card.
    Never mutates queue or audit state. Never launches agents or processes.
    Writes remain default-off regardless of input values.
    """

    result = review_summarizer.build_codex_handoff_report(
        task_ids=task_ids,
        batch_label=batch_label,
    )
    result["server_tool"] = "aiworkhub_task_codex_handoff"
    return result


@mcp.tool()
def aiworkhub_task_codex_handoff_markdown(
    task_ids: list[str] | None = None,
    batch_label: str | None = None,
) -> dict[str, Any]:
    """READ-ONLY: Codex-ready HANDOFF report rendered as compact Markdown.

    Builds the same B116 handoff report as ``aiworkhub_task_codex_handoff`` (via
    the identical read-only ``review-queue`` + ``show`` calls, never a write
    command) and adds a deterministic ``markdown`` field — a compact review
    packet Codex can read directly instead of manually parsing the JSON.

    FROZEN READ-ONLY CONTRACT — no write-gate toggle, no mutation parameters.
    Never calls taskctl done/review/start/auto-pickup/add-card.
    Never mutates queue or audit state. Never launches agents or processes.
    Writes remain default-off regardless of input values.
    """

    result = review_summarizer.build_codex_handoff_markdown_report(
        task_ids=task_ids,
        batch_label=batch_label,
    )
    result["server_tool"] = "aiworkhub_task_codex_handoff_markdown"
    return result


@mcp.tool()
def aiworkhub_supervisor_loop_status(
    task_id: str,
    runner: str | None = None,
    topic: str | None = None,
    supervisor_request_id: str | None = None,
    previous_snapshot: dict[str, Any] | None = None,
    reported_validation_verdict: str | None = None,
) -> dict[str, Any]:
    """READ-ONLY: derive supervisor_state + error taxonomy for a task_id.

    Implements the B08 contract's 7-step derivation over the LIVE queue
    (task_mcp_supervisor_loop_status_tool_b08_v1.json): fetch_status via
    core.show_task, then runner_topic_mismatch / missing_artifact /
    collision / stale_task / failed_validation checks, falling through to a
    read-only lifecycle_state -> supervisor_state mapping when none trip.

    FROZEN READ-ONLY CONTRACT -- no write-gate toggle, no mutation
    parameters. Never calls taskctl done/review/start/auto-pickup/add-card.
    Never mutates queue or audit state. Never launches agents or processes.
    Writes remain default-off regardless of input values.
    """

    return core.supervisor_loop_status(
        task_id=task_id,
        runner=runner,
        topic=topic,
        supervisor_request_id=supervisor_request_id,
        previous_snapshot=previous_snapshot,
        reported_validation_verdict=reported_validation_verdict,
    )


# B107: wire the read-only CLI adapter plan/audit/report tools (B106 contract).
# register() only binds three already read-only functions -- it appends no
# audit entry, mutates no queue state, and launches no process; the write
# gate stays off by default and this call cannot turn it on.
cli_adapter_readonly_tool.register(mcp)


# ---------------------------------------------------------------------------
# B615: native VS Code dashboard tools. These three MCP methods delegate to
# the existing read-only dashboard builders and expose no mutation primitive.
# ---------------------------------------------------------------------------
dashboard_mcp_app.register(mcp)


# ---------------------------------------------------------------------------
# B821: AIWorkHub agent tool-instruction activation (read-only-first MCP tools).
# Binds to the server's active core.repo_root(); never accepts a caller-selected
# repository path.  Write mode requires both the explicit write flag AND the
# parent write gate (AIWORKHUB_ALLOW_WRITES=1).
# ---------------------------------------------------------------------------
agent_tool_instruction_mcp.register(mcp)


# ---------------------------------------------------------------------------
# B252: read-only launch-queue contract/persist views (B119 module pair).
#
# launch_queue_contract.py and launch_queue_persist.py are NOT in this task's
# allowed_writes, so they stay untouched; these three tools import and call
# their existing pure functions directly, mirroring the B106
# cli_adapter_readonly_tool pattern (validated dry-run view + read-only audit
# summary) without adding a register()-style helper to either source module.
#
# Hard invariants preserved unchanged:
#   * launch_queue_contract.launch_enabled() stays hardcoded False and
#     LAUNCH_IMPLEMENTED stays a constant False -- neither is env-toggled.
#   * describe_intent is always called with audit=False (the only other value,
#     True, is refused by the contract itself with LaunchDisabledError).
#   * audit_summary_readonly delegates to launch_queue_persist.read_persisted_log,
#     a read-only, non-creating file read -- it never calls persist_transition
#     or persist_intent (the only write path in that module), so this tool
#     writes nothing regardless of AIWORKHUB_ALLOW_WRITES.
# ---------------------------------------------------------------------------

def _launch_queue_authority_flags() -> dict[str, bool]:
    """Read-only authority flags for the B252 launch-queue tools.

    Every flag stays False; ``write_gate_enabled`` mirrors the parent
    write-gate env state for information only -- none of these three tools
    write anything themselves, so the gate value never grants a write path
    here.
    """

    return {
        "process_launch": False,
        "process_launch_authority": False,
        "agent_launch": False,
        "shell_invocation": False,
        "queue_write": False,
        "audit_write": False,
        "workflow_switch": launch_queue_contract.WORKFLOW_SWITCH_ENABLED,
        "write_gate_enabled": core.writes_allowed(),
    }


@mcp.tool()
def aiworkhub_launch_queue_describe_readonly(
    task_id: str,
    runner: str,
    topic: str,
    adapter_id: str,
    argv_template: list[str] | None = None,
    priority: str = "normal",
) -> dict[str, Any]:
    """READ-ONLY: describe a launch INTENT via the disabled B119 launch_queue_contract.

    Builds a pure ``LaunchRequest`` (``enqueue_intent``) and returns
    ``describe_intent``'s dry-run intent + gate evaluation as data.
    ``describe_intent`` is always invoked with ``audit=False`` here (the only
    other value the contract accepts, ``True``, raises ``LaunchDisabledError``
    rather than writing anything -- this tool never passes it). ``launch_enabled()``
    stays hardcoded False and ``LAUNCH_IMPLEMENTED`` stays False regardless of
    any env flag; no launcher exists in the code path this tool reaches, so it
    can never start a process.

    FROZEN READ-ONLY CONTRACT -- no write-gate toggle, no audit parameter.
    Never mutates queue or audit state. Never launches agents or processes.
    """

    request = launch_queue_contract.enqueue_intent(
        task_id=task_id,
        runner=runner,
        topic=topic,
        adapter_id=adapter_id,
        argv_template=argv_template,
        priority=priority,
    )
    result = launch_queue_contract.describe_intent(request, audit=False)
    result["readonly"] = True
    result["authority_flags"].update(_launch_queue_authority_flags())
    result["server_tool"] = "aiworkhub_launch_queue_describe_readonly"
    return result


@mcp.tool()
def aiworkhub_launch_queue_evaluate_readonly(
    task_id: str,
    runner: str,
    topic: str,
    adapter_id: str,
    argv_template: list[str] | None = None,
    priority: str = "normal",
) -> dict[str, Any]:
    """READ-ONLY: evaluate a launch request via the disabled B119 launch_queue_contract.

    Builds a pure ``LaunchRequest`` (``enqueue_intent``) and returns
    ``evaluate_launch``'s gate-evaluation decision. ``permitted`` is always
    False: this contract has no implemented launcher, so the request stays
    ``blocked_launch_disabled`` regardless of env gate state. Writes nothing:
    no persistence call is made and no process is launched.

    FROZEN READ-ONLY CONTRACT -- no write-gate toggle, no mutation parameters.
    Never mutates queue or audit state. Never launches agents or processes.
    """

    request = launch_queue_contract.enqueue_intent(
        task_id=task_id,
        runner=runner,
        topic=topic,
        adapter_id=adapter_id,
        argv_template=argv_template,
        priority=priority,
    )
    decision = launch_queue_contract.evaluate_launch(request).as_dict()
    decision["readonly"] = True
    decision["authority_flags"] = _launch_queue_authority_flags()
    decision["server_tool"] = "aiworkhub_launch_queue_evaluate_readonly"
    return decision


@mcp.tool()
def aiworkhub_launch_queue_audit_summary_readonly(max_entries: int = 100) -> dict[str, Any]:
    """READ-ONLY: summarize the append-only B119 launch-queue audit JSONL log.

    Delegates to ``launch_queue_persist.read_persisted_log``, which only reads
    the log file (plain ``open(..., "r")``, and reports ``log_exists=False``
    without creating it when absent) and never writes, never enables writes,
    never launches a process. Same shape as ``aiworkhub_task_audit_log_read``:
    counts by decision/to_state plus the last N entries, and reports whether
    every persisted entry so far is ``blocked_launch_disabled`` -- the expected
    state while ``AIWORKHUB_ALLOW_LAUNCH`` / ``_ALLOW_WRITES`` stay unset
    (the default).

    FROZEN READ-ONLY CONTRACT -- no write-gate toggle, no mutation parameters.
    Never mutates queue or audit state. Never launches agents or processes.
    """

    result = launch_queue_persist.read_persisted_log(max_entries=max_entries)
    result["readonly"] = True
    result["authority_flags"] = _launch_queue_authority_flags()
    result["server_tool"] = "aiworkhub_launch_queue_audit_summary_readonly"
    return result


@mcp.tool()
def aiworkhub_task_queue_request(
    task_id: str,
    runner: str,
    topic: str,
    adapter_id: str,
    model: str | None = None,
    owner_prompt: str = "",
    argv_template: list[str] | None = None,
    priority: str = "normal",
    stale_timeout_seconds: int = 7200,
    requested_at: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """WRITE-GATED: append a disabled launch-queue request.

    This is the B286 MVP queue-request tool. It never launches a process:
    ``LAUNCH_IMPLEMENTED`` remains False in launch_queue_contract. With
    ``AIWORKHUB_ALLOW_WRITES`` unset, it appends nothing. With the write
    gate open, it appends one existing launch-queue audit record unless the
    request is idempotent or a different runner already owns an open request
    for the same task_id.
    """

    return core.queue_request(
        task_id=task_id,
        runner=runner,
        topic=topic,
        adapter_id=adapter_id,
        model=model,
        owner_prompt=owner_prompt,
        argv_template=argv_template,
        priority=priority,
        stale_timeout_seconds=stale_timeout_seconds,
        requested_at=requested_at,
        request_id=request_id,
    )


@mcp.tool()
def aiworkhub_task_stale_recovery_recommend(
    topic: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """READ-ONLY: recommend recovery actions for stale processing tasks.

    Produces recommendations only. It never calls taskctl write commands and
    never kills a process.
    """

    return stale_recovery.build_recovery_actions(topic=topic, limit=limit)


@mcp.tool()
def aiworkhub_task_cost_ledger(
    runner: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    include_tasks: bool = False,
) -> dict[str, Any]:
    """READ-ONLY: aggregate task usage and launch-log cost evidence."""

    return cost_ledger.build_cost_ledger(
        runner=runner,
        topic=topic,
        status=status,
        include_tasks=include_tasks,
    )


# ---------------------------------------------------------------------------
# B275: read-only completion-inbox view combining review_queue, stale
# processing, runner-mismatch warnings, and latest validation facts into one
# tool for Codex orchestration. completion_inbox.py shells out only to the
# pre-existing read-only `core.list_tasks`/`core.show_task` (never a write
# command) -- see completion_inbox.py's module docstring for the full
# read-path/invariant note. Additive wiring only; no existing tool touched.
# ---------------------------------------------------------------------------

@mcp.tool()
def aiworkhub_completion_inbox(
    topic: str | None = None,
    limit: int = 200,
    stale_processing_hours: float = completion_inbox.DEFAULT_STALE_PROCESSING_HOURS,
) -> dict[str, Any]:
    """READ-ONLY: combined completion-inbox facts for Codex orchestration.

    Returns four facets in one call: ``review_queue`` (tasks awaiting Codex
    review), ``stale_processing`` (claimed tasks with no recent artifact/
    validation activity, default threshold 24h), ``runner_mismatch_warnings``
    (batch-token mismatches between a runner name and the task_id it
    claimed -- a local pure replica of
    ``AITools/taskctl.py::_runner_task_batch_mismatch``), and
    ``latest_validation_facts`` (the most recently recorded validation_status/
    validation_error/blocker_reason per task).

    FROZEN READ-ONLY CONTRACT -- no write-gate toggle, no mutation
    parameters. Only issues `taskctl list`/`taskctl show` (both read-only).
    Never calls taskctl done/review/start/auto-pickup/add-card/export-jsonl.
    Never mutates queue or audit state. Never launches agents or processes
    beyond the existing `taskctl.py` read-only child-process calls.
    """

    result = completion_inbox.build_completion_inbox(
        topic=topic,
        limit=limit,
        stale_processing_hours=stale_processing_hours,
    )
    # Additive process evidence: taskctl remains lifecycle authority, while
    # launcher exits/timeouts are visible in the same polling call Codex uses
    # for review-ready work.
    result["agent_processes"] = process_launcher.default_manager().list_processes(
        limit=limit
    )
    # Additive read-only adapter readiness (installed / credential_present /
    # endpoint / supported_models / launchable / exact blocker_reason) so Codex
    # sees whether a deepseek_copilot_cli launch is possible BEFORE claiming a
    # task. Exposed here (not as a new tool) to preserve the frozen v1 tool
    # contract; it never exposes the DeepSeek key contents or any hash of them.
    result["adapter_readiness"] = deepseek_credentials.adapter_readiness(
        repo=core.repo_root()
    )
    return result


@mcp.tool()
def aiworkhub_agent_launch_task(
    task_id: str,
    runner: str,
    topic: str,
    adapter_id: str,
    model: str | None = None,
    owner_prompt: str = "",
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    """DUAL-GATED: launch one exact local Claude/Codex worker process.

    Requires both AIWORKHUB_ALLOW_LAUNCH=1 and
    AIWORKHUB_ALLOW_WRITES=1. The task card, runner, topic, pending state,
    allowed-write scope, collision guard, adapter, and process limit are all
    validated before a shell-free child process can start.
    """

    core.scrub_coordinator_capability_from_environment()
    return process_launcher.default_manager().launch(
        task_id=task_id,
        runner=runner,
        topic=topic,
        adapter_id=adapter_id,
        model=model,
        owner_prompt=owner_prompt,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def aiworkhub_agent_task_status(request_id: str) -> dict[str, Any]:
    """READ-ONLY: inspect one launched process and its authoritative task card."""

    return process_launcher.default_manager().status(request_id)


@mcp.tool()
def aiworkhub_agent_collect_result(
    request_id: str,
    max_log_bytes: int = process_launcher.MAX_LOG_TAIL_BYTES,
) -> dict[str, Any]:
    """READ-ONLY: collect bounded worker output and review-readiness evidence."""

    return process_launcher.default_manager().collect(request_id, max_log_bytes=max_log_bytes)


@mcp.tool()
def aiworkhub_agent_cancel_task(
    request_id: str,
    reason: str = "owner_cancelled",
) -> dict[str, Any]:
    """DUAL-GATED: terminate one exact process group recorded by this server."""

    return process_launcher.default_manager().cancel(request_id, reason=reason)


@mcp.tool()
def aiworkhub_agent_list_processes(limit: int = 100) -> dict[str, Any]:
    """READ-ONLY: list latest process states for dashboard and orchestration."""

    return process_launcher.default_manager().list_processes(limit=limit)


@mcp.tool()
def aiworkhub_dispatcher_ensure_started() -> dict[str, Any]:
    """Idempotently ensure exactly one callback dispatcher is running for
    the active repository, bound to the currently-selected coordinator
    target (B857). Called by the VS Code extension after every MCP
    handshake (activation, tab-deserialization, reload) and after an
    explicit coordinator-target switch -- never starts a second thread and
    never marks an uninitialized repository as degraded."""

    return core.dispatcher_ensure_started()


@mcp.tool()
def aiworkhub_dispatcher_health() -> dict[str, Any]:
    """READ-ONLY: dispatcher running/stopped, selected coordinator target,
    repository identity, pending callback count, and last delivery/error."""

    return core.dispatcher_health()


@mcp.tool()
def aiworkhub_dispatcher_stop() -> dict[str, Any]:
    """Stop and unregister the active repository's callback dispatcher, if
    any. Called on workspace/repository switch and extension deactivation
    so no cross-repository read or delivery can happen afterward."""

    return core.dispatcher_stop()


@mcp.tool()
def aiworkhub_claude_callback_wait(timeout_seconds: int = 240) -> dict[str, Any]:
    """BLOCKING READ/CLAIM: wait for a callback for this verified Claude
    Code manager session.  Call ``aiworkhub_claude_callback_ack`` immediately
    after receipt; an unacknowledged lease is retried, never lost."""

    return core.claude_callback_wait(timeout_seconds)


@mcp.tool()
def aiworkhub_claude_callback_ack(batch_id: str, lease_id: str) -> dict[str, Any]:
    """WRITE: acknowledge one exact callback batch returned to this same
    verified Claude manager session."""

    return core.claude_callback_ack(batch_id, lease_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
