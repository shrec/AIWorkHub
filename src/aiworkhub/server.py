from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import os
import sys
import threading
import types
import typing
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Literal

from . import roadmap_store, task_store
from .tool_recovery import unknown_tool_message

_MCP_SDK_AVAILABLE = True
try:
    if os.environ.get("AIWORKHUB_MCP_STDIO_BACKEND", "").strip().lower() == "stdlib":
        raise ModuleNotFoundError("AIWorkHub bounded stdlib MCP backend selected")
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:
    _MCP_SDK_AVAILABLE = False
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

    class _StdioTransportClosed(SystemExit):
        """The client closed stdout; no further JSON-RPC response is possible."""

    def _stdio_log(event: str, **fields: Any) -> None:
        """Write one bounded structured diagnostic without touching stdout."""

        record = {"component": "aiworkhub.mcp_stdio", "event": event}
        record.update({key: value for key, value in fields.items() if value is not None})
        try:
            sys.stderr.write(json.dumps(record, ensure_ascii=False, default=str)[:4000] + "\n")
            sys.stderr.flush()
        except (BrokenPipeError, OSError):
            pass

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

    def _stdio_json_schema_for_annotation(annotation: Any) -> dict[str, Any]:
        if annotation is Any:
            return {}
        origin = typing.get_origin(annotation)
        if origin is typing.Literal:
            values = list(typing.get_args(annotation))
            result: dict[str, Any] = {"enum": values}
            if values:
                result["type"] = _stdio_json_type(type(values[0]))
            return result
        if origin in (typing.Union, types.UnionType):
            args = [a for a in typing.get_args(annotation) if a is not type(None)]
            if len(args) == 1:
                return _stdio_json_schema_for_annotation(args[0])
        if origin in (list, tuple) or annotation in (list, tuple):
            annotation_args = typing.get_args(annotation)
            item_annotation = annotation_args[0] if annotation_args else Any
            return {
                "type": "array",
                "items": _stdio_json_schema_for_annotation(item_annotation),
            }
        if origin is dict or annotation is dict:
            annotation_args = typing.get_args(annotation)
            value_annotation = annotation_args[1] if len(annotation_args) == 2 else Any
            return {
                "type": "object",
                "additionalProperties": _stdio_json_schema_for_annotation(value_annotation),
            }
        return {"type": _stdio_json_type(annotation)}

    def _stdio_schema_for(func: Any) -> dict[str, Any]:
        try:
            sig = inspect.signature(func)
        except (TypeError, ValueError):
            return {"type": "object", "properties": {}, "additionalProperties": True}
        try:
            resolved_hints = typing.get_type_hints(func)
        except (NameError, TypeError):
            resolved_hints = {}
        properties: dict[str, Any] = {}
        required: list[str] = []
        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            properties[pname] = _stdio_json_schema_for_annotation(
                resolved_hints.get(pname, param.annotation)
            )
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
            raise _StdioProtocolError(-32602, unknown_tool_message(name, tools))
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

    _PROTOCOL_ALERT_RELATIVE_PARTS = (".aiworkhub", "runtime", "mcp_protocol_alerts.json")
    _PROTOCOL_ALERT_SCHEMA_ID = "aiworkhub.mcp_control_plane.protocol_alert.v1"
    _PROTOCOL_ALERT_FIELD_MAX_LEN = 200

    def _protocol_alert_repo_root() -> Path | None:
        raw = os.environ.get("AIWORKHUB_REPO_ROOT", "").strip()
        if not raw:
            return None
        try:
            root = Path(raw)
            return root if root.is_dir() else None
        except OSError:
            return None

    def _record_protocol_alert(*, method: Any, request_id: Any, reason: str) -> None:
        repo_root = _protocol_alert_repo_root()
        if repo_root is None:
            return
        try:
            path = repo_root.joinpath(*_PROTOCOL_ALERT_RELATIVE_PARTS)
            path.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                count = int(existing.get("count") or 0)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                count = 0
            record = {
                "schema_id": _PROTOCOL_ALERT_SCHEMA_ID,
                "count": count + 1,
                "latest": {
                    "method": str(method)[:_PROTOCOL_ALERT_FIELD_MAX_LEN] if method is not None else None,
                    "request_id": request_id
                    if request_id is None or isinstance(request_id, (str, int, float, bool))
                    else str(request_id)[:64],
                    "repo_identity": repo_root.name[:_PROTOCOL_ALERT_FIELD_MAX_LEN],
                    "boundary": "stdio_server",
                    "reason": reason[:_PROTOCOL_ALERT_FIELD_MAX_LEN],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
            tmp_path = path.with_name(path.name + ".tmp")
            tmp_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            os.replace(tmp_path, path)
        except OSError:
            pass

    def _validate_stdio_rpc_params(method: Any, params: Any, *, request_id: Any) -> dict[str, Any]:
        """Fail closed on non-object params before any child/RPC dispatch."""

        if params is None:
            return {}
        if isinstance(params, dict):
            return params
        reason = (
            "invalid_params:empty_string"
            if params == ""
            else f"invalid_params:non_object:{type(params).__name__}"
        )
        _record_protocol_alert(method=method, request_id=request_id, reason=reason)
        raise _StdioProtocolError(-32602, reason)

    def _stdio_dispatch(
        name: str,
        tools: dict[str, Any],
        method: Any,
        params: Any,
        instructions: str = "",
        request_id: Any = None,
    ) -> Any:
        params = _validate_stdio_rpc_params(method, params, request_id=request_id)
        if method == "initialize":
            result = {
                "protocolVersion": _FALLBACK_PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": name, "version": __version__},
            }
            if instructions:
                result["instructions"] = instructions
            return result
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return _stdio_tools_list(tools)
        if method == "tools/call":
            return _stdio_tools_call(tools, params)
        if method == "resources/list":
            return {"resources": []}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        raise _StdioProtocolError(-32601, f"method_not_found:{method}")

    def _stdio_write_message(stream: Any, message: dict[str, Any]) -> None:
        payload = json.dumps(
            message, ensure_ascii=False, default=str,
        ).encode("utf-8")
        if len(payload) > _FALLBACK_MAX_RESPONSE_BYTES:
            message = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32603, "message": "response_too_large"},
            }
            payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
        try:
            stream.write(payload + b"\n")
            stream.flush()
        except (BrokenPipeError, OSError) as exc:
            _stdio_log(
                "transport_closed",
                request_id=message.get("id"),
                error_type=type(exc).__name__,
            )
            raise _StdioTransportClosed(0) from exc

    def _run_stdio_fallback_server(
        name: str,
        tools: dict[str, Any],
        instructions: str = "",
    ) -> None:
        # Read from the binary buffer with an explicit limit.  Calling the
        # text wrapper's unbounded readline() and checking the length only
        # afterwards would already have allocated an arbitrarily large client
        # line.  The one-byte look-ahead detects overflow; the remainder is
        # drained in bounded chunks so the next request keeps its framing.
        stdin = sys.stdin.buffer
        # MCP stdio is a byte protocol.  Writing Unicode JSON through the
        # platform text wrapper makes transport correctness depend on the
        # host locale (for example Windows cp1251 cannot encode Georgian).
        # Always emit explicit UTF-8 bytes and bypass locale transcoding.
        stdout = sys.stdout.buffer
        write_lock = threading.Lock()
        transport_closed = threading.Event()

        def write(message: dict[str, Any]) -> None:
            if transport_closed.is_set():
                return
            try:
                with write_lock:
                    _stdio_write_message(stdout, message)
            except _StdioTransportClosed:
                transport_closed.set()

        def dispatch_request(message: dict[str, Any]) -> None:
            msg_id = message.get("id")
            method = message.get("method")
            params = message.get("params")
            try:
                result = _stdio_dispatch(name, tools, method, params, instructions, msg_id)
            except _StdioProtocolError as exc:
                write({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": exc.code, "message": exc.message},
                })
                return
            except Exception as exc:  # defensive: one request never kills the lane
                _stdio_log(
                    "request_failed",
                    request_id=msg_id,
                    method=method,
                    error_type=type(exc).__name__,
                )
                write({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {
                        "code": -32603,
                        "message": f"internal_error:{type(exc).__name__}",
                    },
                })
                return
            write({"jsonrpc": "2.0", "id": msg_id, "result": result})

        # Tool calls may legitimately wait on providers or repository locks.
        # Keep the reader and lightweight status calls serviceable while those
        # calls run, with capacity derived from this host and a conservative
        # upper bound. JSON-RPC permits responses to arrive out of order; the
        # immutable request id remains the exact correlation authority.
        max_workers = max(4, min(32, os.cpu_count() or 4))
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="aiworkhub-mcp-tool",
        )
        try:
            while not transport_closed.is_set():
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
                    write({
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
                    write({
                        "jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "parse_error"},
                    })
                    continue
                if not isinstance(message, dict):
                    write({
                        "jsonrpc": "2.0", "id": None,
                        "error": {"code": -32600, "message": "invalid_request"},
                    })
                    continue
                if "id" not in message:
                    # A JSON-RPC message with no id is a notification: no
                    # response, and side-effect free beyond lifecycle.  Anything
                    # that requires a response -- tools/call above all -- is a
                    # malformed request when it omits its id and must never reach
                    # _stdio_dispatch: under the stdlib backend an id-less
                    # tools/call for a launch tool would start a worker
                    # synchronously inside the read loop and return nothing,
                    # blocking every later request.  Dispatch only genuine
                    # notifications; refuse everything else with an explicit
                    # error so no launch runs.
                    method = message.get("method")
                    if not (isinstance(method, str) and method.startswith("notifications/")):
                        write({
                            "jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "id_required"},
                        })
                        continue
                    try:
                        _stdio_dispatch(
                            name,
                            tools,
                            method,
                            message.get("params"),
                            instructions,
                            None,
                        )
                    except Exception as exc:
                        _stdio_log(
                            "notification_failed",
                            method=method,
                            error_type=type(exc).__name__,
                        )
                    continue
                executor.submit(dispatch_request, message)
        finally:
            executor.shutdown(wait=True, cancel_futures=False)

    class FastMCP:  # type: ignore[no-redef]
        """Bounded stdlib MCP stdio server, used when the `mcp` package is absent."""

        def __init__(self, name: str, instructions: str | None = None):
            self.name = name
            self.instructions = str(instructions or "")
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
            _run_stdio_fallback_server(self.name, self._tools, self.instructions)

from . import __version__
from . import agent_tool_instruction_mcp
from . import cli_adapter_readonly_tool
from . import completion_inbox
from . import context_importer
from . import context_writes
from . import cost_ledger
from . import core
from . import dashboard_mcp_app
from . import deepseek_credentials
from . import evidence_instruments
from . import launch_queue_contract
from . import launch_queue_persist
from . import known_bug_scanner
from . import manager_ai_tools
from . import process_launcher
from . import review_summarizer
from . import stale_recovery
from . import task_engine
from . import task_plan
from . import task_reconciler
from . import terminal_log_retention
from . import worker_ai_tools_mcp
from . import dependency_autolaunch
from . import quality_evidence
from . import quality_calibration
from . import repo_policy
from . import shared_router


try:
    mcp = FastMCP(
        "AIWorkHub MCP",
        instructions=core.MCP_MANAGER_CONTRACT_BANNER,
    )
except TypeError:
    # Older MCP SDKs and deliberately minimal embedders may expose a
    # name-only constructor.  Keep startup fail-open; the identical mandatory
    # contract remains available through aiworkhub_manager_bootstrap.
    mcp = FastMCP("AIWorkHub MCP")


if _MCP_SDK_AVAILABLE:
    _sdk_tool_decorator = mcp.tool

    def _nonblocking_mcp_tool(*decorator_args: Any, **decorator_kwargs: Any):
        """Register sync MCP tools without blocking the SDK event loop.

        MCP's FastMCP currently awaits async functions but invokes synchronous
        functions inline. AIWorkHub tools are intentionally synchronous and a
        launch/finalization call may take minutes, so inline invocation makes
        unrelated status, dashboard and bootstrap requests time out behind it.
        Register an async adapter while returning the original function to
        preserve direct Python callers and tests.
        """

        register = _sdk_tool_decorator(*decorator_args, **decorator_kwargs)

        def decorate(function: Any) -> Any:
            if inspect.iscoroutinefunction(function):
                register(function)
                return function

            @wraps(function)
            async def run_in_worker_thread(*args: Any, **kwargs: Any) -> Any:
                return await asyncio.to_thread(function, *args, **kwargs)

            register(run_in_worker_thread)
            return function

        return decorate

    # All following @mcp.tool registrations use the non-blocking adapter.
    # The stdlib fallback has its own bounded executor above and keeps the
    # original synchronous registration path.
    mcp.tool = _nonblocking_mcp_tool  # type: ignore[method-assign]


_TASK_LIFECYCLE_WRITE_LOCK = threading.RLock()


def _serialize_task_lifecycle_write(function: Any) -> Any:
    """Serialize state-changing task operations within one MCP server.

    FastMCP may dispatch synchronous tools concurrently.  Keeping the complete
    task lifecycle mutation and its result construction under one re-entrant
    lock prevents overlapping create/claim/review/finalize calls from racing
    on a shared stdio session.  SQLite transactions remain the cross-process
    authority; this lock is the per-server ordering boundary.
    """

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _TASK_LIFECYCLE_WRITE_LOCK:
            return function(*args, **kwargs)

    return wrapped

RiskSignal = Literal[
    "public_api",
    "combined_change",
    "authority_boundary",
    "concurrency",
    "destructive_change",
    "schema_migration",
    "security_sensitive",
    "release",
]


@mcp.tool()
def aiworkhub_manager_bootstrap() -> dict[str, Any]:
    """CALL FIRST: mandatory repository manager contract and callback workflow."""

    return core.manager_bootstrap()


@mcp.tool()
def aiworkhub_manager_source_graph_query(
    mode: worker_ai_tools_mcp.SourceGraphMode,
    query: str,
    budget: int = 64,
    target: str | None = None,
    bundle_type: worker_ai_tools_mcp.SourceGraphBundleType = "explore",
    workflow_stage: worker_ai_tools_mcp.WorkflowStage = "unspecified",
) -> dict[str, Any]:
    """MANAGER CODE DISCOVERY: use first and continuously before built-in file search."""

    return manager_ai_tools.source_graph_query(
        mode=mode, query=query, budget=budget, target=target,
        bundle_type=bundle_type, workflow_stage=workflow_stage,
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
def aiworkhub_manager_ai_memory_get(key: str) -> dict[str, Any]:
    """MANAGER READ: exact active canonical AI Memory lookup."""

    return manager_ai_tools.ai_memory_get(key=key)


@mcp.tool()
def aiworkhub_manager_ai_memory_related(key: str) -> dict[str, Any]:
    """MANAGER READ: bounded related canonical AI Memory lookup."""

    return manager_ai_tools.ai_memory_related(key=key)


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
def aiworkhub_manager_context_graph_search(query: str, limit: int = 12) -> dict[str, Any]:
    """MANAGER READ: search exact transcript-backed Context Graph evidence."""

    return manager_ai_tools.context_graph_search(query=query, limit=limit)


@mcp.tool()
def aiworkhub_manager_context_graph_range(
    thread_id: str, around_event_id: int, before: int = 5, after: int = 5,
) -> dict[str, Any]:
    """MANAGER READ: retrieve an exact bounded transcript range around one event."""

    return manager_ai_tools.context_graph_range(
        thread_id=thread_id, around_event_id=around_event_id, before=before, after=after,
    )


@mcp.tool()
def aiworkhub_manager_context_graph_related(node_id: str, limit: int = 20) -> dict[str, Any]:
    """MANAGER READ: retrieve bounded deterministic relations for one graph node."""

    return manager_ai_tools.context_graph_related(node_id=node_id, limit=limit)


@mcp.tool()
def aiworkhub_manager_context_graph_event_write(
    role: str,
    event_type: str,
    content: str,
    source_ref: str,
    idempotency_key: str,
    task_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MANAGER WRITE: append one exact current-thread conversation event."""

    return manager_ai_tools.context_graph_event_write(
        role=role,
        event_type=event_type,
        content=content,
        source_ref=source_ref,
        idempotency_key=idempotency_key,
        task_id=task_id,
        metadata=metadata,
    )


@mcp.tool()
def aiworkhub_manager_context_graph_rebuild() -> dict[str, Any]:
    """MANAGER WRITE: rebuild derived graph state from the immutable ledger."""

    return manager_ai_tools.context_graph_rebuild()


@mcp.tool()
def aiworkhub_manager_workforce_catalog() -> dict[str, Any]:
    """MANAGER READ: configured models joined to observed canonical outcomes."""

    return manager_ai_tools.workforce_catalog_read()


@mcp.tool()
def aiworkhub_manager_workforce_rank(
    task_id: str,
    kinds: list[str],
    risk: str = "medium",
    context_tokens: int = 0,
    tool_needs: list[str] | None = None,
    quality_floor: float = 0.0,
) -> dict[str, Any]:
    """MANAGER READ: explainable cheapest-capable routing from current evidence."""

    return manager_ai_tools.workforce_rank(
        task_id=task_id,
        kinds=kinds,
        risk=risk,
        context_tokens=context_tokens,
        tool_needs=tool_needs,
        quality_floor=quality_floor,
    )


@mcp.tool()
def aiworkhub_manager_task_decomposition_preview(
    parent_task_id: str,
    objective: str,
    source_graph_receipt: dict[str, Any],
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    """MANAGER READ: validate a Source Graph-grounded child-DAG proposal.

    This operation never creates or launches tasks. The returned digest is an
    advisory proposal that requires a separate explicit manager decision.
    """

    return manager_ai_tools.task_decomposition_preview(
        parent_task_id=parent_task_id,
        objective=objective,
        source_graph_receipt=source_graph_receipt,
        children=children,
    )


@mcp.tool()
def aiworkhub_manager_session_write(
    action: context_writes.SessionAction,
    topic: str,
    content: str,
    idempotency_key: str,
    provenance: str,
) -> dict[str, Any]:
    """MANAGER WRITE: append one audited canonical Session Manager event."""

    return manager_ai_tools.session_write(
        action=action, topic=topic, content=content,
        idempotency_key=idempotency_key, provenance=provenance,
    )


@mcp.tool()
def aiworkhub_manager_ai_memory_write(
    action: context_writes.MemoryAction,
    key: str,
    idempotency_key: str,
    provenance: str,
    value: str = "",
    tags: str = "",
    scope: str = "project",
) -> dict[str, Any]:
    """MANAGER WRITE: remember, update, supersede, or archive canonical memory."""

    return manager_ai_tools.ai_memory_write(
        action=action, key=key, value=value, tags=tags, scope=scope,
        idempotency_key=idempotency_key, provenance=provenance,
    )


@mcp.tool()
def aiworkhub_manager_kb_write(
    action: context_writes.KbAction,
    key: str,
    idempotency_key: str,
    provenance: str,
    title: str = "",
    body: str = "",
    category: str = "",
    tags: str = "",
    source_refs: str = "",
    replacement_key: str = "",
) -> dict[str, Any]:
    """MANAGER WRITE: upsert, ingest, supersede, or archive canonical KB data."""

    return manager_ai_tools.kb_write(
        action=action, key=key, title=title, body=body, category=category,
        tags=tags, source_refs=source_refs, replacement_key=replacement_key,
        idempotency_key=idempotency_key, provenance=provenance,
    )


@mcp.tool()
def aiworkhub_manager_learning_commit(
    task_id: str,
    request_id: str,
    repo_area: str,
    outcome: str,
    evidence_ids: list[str],
    idempotency_key: str,
    provenance: str,
    root_cause_candidate: str = "",
    invariant_candidate: str = "",
    lesson_candidate: str = "",
    edge_candidates: list[dict[str, str]] | None = None,
    promote_ai_memory: bool = False,
    promote_context_graph: bool = False,
    promote_kb: bool = False,
) -> dict[str, Any]:
    """MANAGER WRITE: persist and project one verified Learning Commit."""

    return manager_ai_tools.learning_commit(
        task_id=task_id,
        request_id=request_id,
        repo_area=repo_area,
        outcome=outcome,
        evidence_ids=evidence_ids,
        idempotency_key=idempotency_key,
        provenance=provenance,
        root_cause_candidate=root_cause_candidate,
        invariant_candidate=invariant_candidate,
        lesson_candidate=lesson_candidate,
        edge_candidates=edge_candidates,
        promote_ai_memory=promote_ai_memory,
        promote_context_graph=promote_context_graph,
        promote_kb=promote_kb,
    )


@mcp.tool()
def aiworkhub_manager_needfix_markdown_preview(
    source_paths: list[str] | None = None,
    follow_links: bool = True,
) -> dict[str, Any]:
    """MANAGER READ: preview bounded captured NeedFix candidates from Markdown."""

    return manager_ai_tools.needfix_markdown_preview(
        source_paths=source_paths,
        follow_links=follow_links,
    )


@mcp.tool()
def aiworkhub_manager_needfix_markdown_commit(
    preview_id: str,
    source_paths: list[str] | None = None,
    follow_links: bool = True,
) -> dict[str, Any]:
    """MANAGER WRITE: commit an exact Markdown preview as captured items only."""

    return manager_ai_tools.needfix_markdown_commit(
        source_paths=source_paths,
        preview_id=preview_id,
        follow_links=follow_links,
    )


@mcp.tool()
def aiworkhub_manager_workforce_upsert(
    worker_id: str,
    adapter_id: str,
    model: str,
    provider: str,
    supports: list[str],
    tools: list[str] | None = None,
    enabled: bool = True,
    max_context_tokens: int = 0,
    max_risk: str = "medium",
    quality_ceiling: float = 1.0,
    manager_score_adjustment: float = 0.0,
) -> dict[str, Any]:
    """MANAGER WRITE: audited bounded upsert of one repository worker model."""

    return manager_ai_tools.workforce_upsert(worker={
        "worker_id": worker_id,
        "adapter_id": adapter_id,
        "model": model,
        "provider": provider,
        "supports": supports,
        "tools": tools or [],
        "enabled": enabled,
        "max_context_tokens": max_context_tokens,
        "max_risk": max_risk,
        "quality_ceiling": quality_ceiling,
        "manager_score_adjustment": manager_score_adjustment,
    })


@mcp.tool()
def aiworkhub_manager_context_import(
    component: context_importer.Component,
    operation: context_importer.Operation,
    source_path: str = "",
    idempotency_key: str = "",
    import_id: str = "",
    provenance: str = "",
    limit: int = 10_000,
) -> dict[str, Any]:
    """MANAGER: dry-run, apply or rollback one explicit repo-local legacy context store."""

    return manager_ai_tools.context_import(
        component=component,
        operation=operation,
        source_path=source_path,
        idempotency_key=idempotency_key,
        import_id=import_id,
        provenance=provenance,
        limit=limit,
    )


@mcp.tool()
def aiworkhub_manager_context_write_intents(request_id: str) -> dict[str, Any]:
    """MANAGER READ: inspect authenticated worker context-write proposals."""

    return process_launcher.default_manager().context_write_intents(request_id)


@mcp.tool()
def aiworkhub_manager_context_write_intent_dispose(
    request_id: str,
    intent_id: str,
    decision: Literal["accepted", "rejected"],
    reason: str,
) -> dict[str, Any]:
    """MANAGER WRITE: explicitly accept or reject one worker proposal."""

    return process_launcher.default_manager().dispose_context_write_intent(
        request_id, intent_id, decision=decision, reason=reason,
    )


@mcp.tool()
def aiworkhub_vscode_lm_worker_tool(
    request_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """PRIVATE BRIDGE: execute one active GLM call with worker-scoped audit authority."""

    return process_launcher.default_manager().invoke_vscode_lm_worker_tool(
        request_id=request_id,
        tool_name=tool_name,
        tool_input=tool_input,
    )


@mcp.tool()
def aiworkhub_router_known_repositories(limit: int = 64, include_inactive: bool = False) -> dict[str, Any]:
    """READ-ONLY: bounded shared registry of live AIWorkHub repository routes.

    This is discovery only.  Task storage and task mutation remain scoped to
    the current repository's own ``.aiworkhub`` MCP child.
    """

    return shared_router.list_known_repositories(
        current_root=core.repo_root(),
        limit=limit,
        include_inactive=include_inactive,
    )


@mcp.tool()
def aiworkhub_repo_list(limit: int = 64, include_inactive: bool = False) -> dict[str, Any]:
    """READ-ONLY: bounded live repository identities available to this manager."""

    return shared_router.list_known_repositories(
        current_root=core.repo_root(), limit=limit, include_inactive=include_inactive,
    )


@mcp.tool()
def aiworkhub_repo_current() -> dict[str, Any]:
    """READ-ONLY: exact current repository, binding source, and manager route."""

    return core.repository_current()


@mcp.tool()
def aiworkhub_repo_switch(repo_id: str) -> dict[str, Any]:
    """MANAGER OPERATION: atomically switch to one live manager-owned repo_id."""

    return core.repository_switch(repo_id)


@mcp.tool()
@_serialize_task_lifecycle_write
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
    allow_empty_required_outputs: list[str] | None = None,
    allow_unchanged_required_outputs: list[str] | None = None,
    validation: list[str] | None = None,
    priority: str = "normal",
    task_type: str = "code",
    depends_on: list[str] | None = None,
    read_first: list[str] | None = None,
    immutable_inputs: list[str] | None = None,
    read_only: bool = False,
    max_live_tokens: int | None = None,
    work_kind: str = "generic",
    validation_roles: list[str] | None = None,
    risk_tier: str | None = None,
) -> dict[str, Any]:
    """MANAGER WRITE: create one new canonical repo-local task card.

    The live manager session supplies callback provider/origin identity;
    callers cannot route a task into another chat, disable the mandatory
    review callback, or overwrite an existing id.
    ``depends_on`` (optional) names other task_ids in this repo that must
    finish before this card is Plan-DAG ready; omit it for the pre-DAG
    behavior.
    Tasks are uncapped by default. Never infer, estimate, or auto-assign
    ``max_live_tokens`` from task complexity, model, historical usage, or
    cost. Supply it only when the owner explicitly requests an exact cap or a
    repository policy pre-registers one. Process efficiency must come from
    focused context, bounded reads, minimal edits, fewer retries, and useful
    validation rather than from truncating work. An authorized cap is enforced
    only when the selected provider exposes structured usage while running;
    terminal-only usage remains posthoc evidence, never a false live-
    enforcement claim.
    Every writable task requires a non-empty ``required_outputs`` contract;
    it contains only repo-relative file paths or glob patterns covered by
    ``allowed_writes``. Human-readable outcome
    descriptions belong in ``acceptance``; mixing prose into
    ``required_outputs`` is rejected before a provider run is launched.
    ``allow_unchanged_required_outputs`` explicitly names required files that
    are evidence inputs/outputs permitted to remain byte-identical to both
    launch baselines; they are verified and hashed but never promoted.
    ``allow_empty_required_outputs`` is the equivalent explicit exception for
    deliberately zero-byte outputs. Neither exception is inferred.
    ``read_only=True`` is required for a task that intentionally declares
    both ``allowed_writes=[]`` and ``required_outputs=[]``. Read-only intent is
    never inferred from missing output authority.
    ``runner`` is the worker execution identity returned by
    ``aiworkhub_manager_workforce_rank.launch_contract.runner``. The exact
    value ``codex`` is reserved for the verified manager and is rejected for
    worker cards.
    ``work_kind`` is an explicit behavioral contract, never inferred from the
    title or worker prose. Specialized values require one ``validation_roles``
    entry per validation command: bugfix=reproduction+regression,
    refactor=parity, performance=baseline+delta, security=negative_fixture,
    and data_ml=schema+distribution. Missing roles are rejected before launch.
    ``risk_tier`` is optional explicit routing/economic evidence. It is never
    inferred from priority or prose; omitted historical cards remain unknown.
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
        allow_empty_required_outputs=allow_empty_required_outputs,
        allow_unchanged_required_outputs=allow_unchanged_required_outputs,
        validation=validation,
        priority=priority,
        task_type=task_type,
        depends_on=depends_on,
        read_first=read_first,
        immutable_inputs=immutable_inputs,
        read_only=read_only,
        max_live_tokens=max_live_tokens,
        work_kind=work_kind,
        validation_roles=validation_roles,
        risk_tier=risk_tier,
    )


def _compact_task_plan_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Project a full Plan-DAG snapshot through the canonical summariser.

    The projection is owned by ``task_plan.summarize_task_plan_snapshot`` so the
    MCP surface can never drift from the pure-plan field list.  A private field
    list here previously dropped every write-scope collision field
    (``global_collision_*``/``card_collision_*``), so the default receipt could
    report a card ready and collision-free while ``full=true`` reported it in a
    global collision -- the exact failure this tool exists to prevent, since a
    manager reads the default receipt before launching cards in parallel.

    ``omitted_fields`` is derived from the difference between the full snapshot
    and this projection, so it always names every field actually dropped instead
    of a hand-maintained literal that silently under-declares.  In particular a
    dropped ``dependency_resolution_errors`` (the archived-dependency-not-
    superseded reason) can no longer hide behind an empty ``invalid_depends_on``.
    """

    summary = task_plan.summarize_task_plan_snapshot(snapshot)
    summary["omitted_fields"] = sorted(
        key for key in snapshot if key not in summary
    )
    return summary


@mcp.tool()
def aiworkhub_task_plan_snapshot(full: bool = False) -> dict[str, Any]:
    """READ-ONLY: bounded Plan-DAG summary, or the complete DAG with ``full``.

    Default mode reports actionable lifecycle, ready work, blockers,
    write-scope overlaps, orphaned processing and DAG validity without
    repeating every finished card's dependency history.  Use ``full=true``
    for exact historical dependency/layer inspection.  This tool never claims
    work; ``aiworkhub_task_auto_pickup``/``_dryrun`` remain the claim surfaces.
    """

    snapshot = core.task_plan_snapshot()
    if full:
        return {
            **snapshot,
            "snapshot_mode": "full",
            "full_snapshot_available": True,
        }
    return _compact_task_plan_snapshot(snapshot)


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
@_serialize_task_lifecycle_write
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
@_serialize_task_lifecycle_write
def aiworkhub_task_mark_review(task_id: str) -> dict[str, Any]:
    """Write-gated: mark a worker task as ready for Codex review."""

    return core.mark_review(task_id=task_id)


@mcp.tool()
@_serialize_task_lifecycle_write
def aiworkhub_task_mark_done(task_id: str) -> dict[str, Any]:
    """Write-gated: finalize a reviewed task as done."""

    return core.mark_done(task_id=task_id)


@mcp.tool()
@_serialize_task_lifecycle_write
def aiworkhub_task_dependency_autolaunch_reconcile(capacity: int | None = None) -> dict[str, Any]:
    """MANAGER WRITE: bounded startup reconciliation for dependency-ready children."""

    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed"}
    return dependency_autolaunch.reconcile_startup(
        core.repo_root(), core.claim_start_exact, capacity=capacity
    )


@mcp.tool()
@_serialize_task_lifecycle_write
def aiworkhub_task_reject_review(
    task_id: str,
    reason: str,
    to: str = "pending",
    residual_identities: list[dict[str, str]] | None = None,
    predecessor_request_id: str | None = None,
) -> dict[str, Any]:
    """Write-gated Codex action: reject a reviewed task with exact feedback and
    an explicit disposition. ``to`` = pending (rework, default) | blocked |
    archived | superseded -- so a task whose real next step is a dependency-
    gated replacement is not silently requeued to pending.

    ``predecessor_request_id`` selects an exact retained review request as
    the rework workspace authority.  Omitted (None) defaults to the current
    review request.  An empty string fails closed.
    """

    kwargs: dict[str, Any] = {"task_id": task_id, "reason": reason, "to": to}
    # Preserve the established public call shape for callers that do not use
    # the optional typed residual contract.  This also keeps older embedded
    # runtimes/mocks compatible while newer callers can opt into the field.
    if residual_identities is not None:
        kwargs["residual_identities"] = residual_identities
    if predecessor_request_id is not None:
        kwargs["predecessor_request_id"] = predecessor_request_id
    return core.reject_review(**kwargs)


@mcp.tool()
@_serialize_task_lifecycle_write
def aiworkhub_task_recover_blocked_rework(
    task_id: str,
    feedback_reason: str = "",
    validation_only_replay: bool = False,
    clean_root_if_predecessor_missing: bool = False,
) -> dict[str, Any]:
    """Recover one exact blocked task through the canonical transaction."""

    return core.recover_blocked_rework(
        task_id,
        feedback_reason=feedback_reason,
        validation_only_replay=bool(validation_only_replay),
        clean_root_if_predecessor_missing=bool(
            clean_root_if_predecessor_missing
        ),
    )

@mcp.tool()
@_serialize_task_lifecycle_write
def aiworkhub_task_retry_terminal(
    task_id: str,
    request_id: str,
    terminal_substatus: str,
    reason: str = "",
) -> dict[str, Any]:
    """COORDINATOR WRITE: retry one exact operational terminal episode.

    Only blocked cancelled/timed-out/launch/worker/process/liveness failures
    are eligible.  The exact prior request id and substatus are mandatory;
    semantic validation/review failures remain on their established review
    workflow.  The same task ID is requeued and its append-only audit history
    is preserved.
    """

    return core.retry_terminal_task(
        task_id=task_id,
        request_id=request_id,
        terminal_substatus=terminal_substatus,
        reason=reason,
    )


@mcp.tool()
@_serialize_task_lifecycle_write
def aiworkhub_task_archive(
    task_id: str,
    reason: str = "",
) -> dict[str, Any]:
    """COORDINATOR WRITE: archive a stale non-processing task atomically
    (archived_at + card_json + an ``archived`` task_events row). Runs the full
    coordinator-capability write gate -- use this instead of patching SQLite.
    For an active/orphaned card use supersede."""

    return core.archive_task(task_id=task_id, reason=reason)


@mcp.tool()
@_serialize_task_lifecycle_write
def aiworkhub_task_supersede(
    task_id: str,
    reason: str = "",
    by: str = "",
) -> dict[str, Any]:
    """COORDINATOR WRITE: supersede a stale/active task with a replacement.
    Archives the card as ``superseded`` (allowed even from processing) and
    records the optional replacement task id via ``by``. Full coordinator-
    capability gate; atomic; no direct SQLite patching."""

    return core.supersede_task(task_id=task_id, reason=reason, by=by)


@mcp.tool()
@_serialize_task_lifecycle_write
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
@_serialize_task_lifecycle_write
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
    full: bool = False,
) -> dict[str, Any]:
    """READ-ONLY: bounded cost summary, with opt-in detailed dimensions.

    Default mode retains cost/cache truth plus provider, model and day
    aggregates.  Per-runner and per-topic maps are available with
    ``full=true``; task rows remain independently opt-in through
    ``include_tasks``.
    """

    result = cost_ledger.build_cost_ledger(
        runner=runner,
        topic=topic,
        status=status,
        include_tasks=include_tasks,
    )
    if full:
        return {
            **result,
            "snapshot_mode": "full",
            "full_snapshot_available": True,
        }

    aggregates = result.get("aggregates")
    aggregate_map = dict(aggregates) if isinstance(aggregates, Mapping) else {}
    summary_dimensions = ("by_model", "by_provider", "by_day")
    result["aggregates"] = {
        key: aggregate_map.get(key, {})
        for key in summary_dimensions
    }
    result.update({
        "snapshot_mode": "summary",
        "full_snapshot_available": True,
        # Derive the omission receipt from the real aggregate map instead of a
        # hand-maintained literal.  build_cost_ledger produces six dimensions
        # (by_topic/by_runner/by_model/by_provider/by_role/by_day); the summary
        # keeps three.  A hardcoded list named only by_topic/by_runner and so
        # dropped by_role -- worker vs reviewer spend -- with no trace.
        "omitted_dimensions": sorted(
            key for key in aggregate_map if key not in summary_dimensions
        ),
        "detail_request": {"full": True},
    })
    return result


# ---------------------------------------------------------------------------
# B275: read-only completion-inbox view combining review_queue, stale
# processing, runner-mismatch warnings, and latest validation facts into one
# tool for Codex orchestration. completion_inbox.py shells out only to the
# pre-existing read-only `core.list_tasks`/`core.show_task` (never a write
# command) -- see completion_inbox.py's module docstring for the full
# read-path/invariant note. Additive wiring only; no existing tool touched.
# ---------------------------------------------------------------------------


def _enrich_operational_failures_from_processes(
    operational_failures: Any, compact_processes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return enriched copies of ``operational_failures`` from process rows.

    Index ``compact_processes`` by exact non-empty ``request_id`` and, when a
    failure row carries that exact ``request_id``, copy the process ``error``
    when the failure has no ``operational_error`` and copy
    ``workspace_retained`` whenever the process row exposes it (including
    ``False``). Duplicate exact request rows resolve latest-wins by the last
    ``compact_processes`` occurrence. Resolution uses only the failure's
    ``request_id`` field -- never ``task_id`` and never ``launch_request_id``.
    """
    if not isinstance(operational_failures, list) or not operational_failures:
        return operational_failures
    latest_by_request: dict[str, dict[str, Any]] = {}
    for row in compact_processes:
        if not isinstance(row, Mapping):
            continue
        request_id = row.get("request_id")
        if isinstance(request_id, str) and request_id:
            latest_by_request[request_id] = row
    enriched: list[dict[str, Any]] = []
    for failure in operational_failures:
        if not isinstance(failure, Mapping):
            enriched.append(failure)
            continue
        failure_dict: dict[str, Any] = dict(failure)
        request_id = failure.get("request_id")
        match = (
            latest_by_request.get(request_id)
            if isinstance(request_id, str) and request_id
            else None
        )
        if match is None:
            enriched.append(failure_dict)
            continue
        enriched_failure = failure_dict
        if not enriched_failure.get("operational_error") and match.get("error"):
            enriched_failure["operational_error"] = match["error"]
        if "workspace_retained" in match:
            enriched_failure["workspace_retained"] = match["workspace_retained"]
        enriched.append(enriched_failure)
    return enriched


@mcp.tool()
def aiworkhub_completion_inbox(
    topic: str | None = None,
    limit: int = 200,
    stale_processing_hours: float = completion_inbox.DEFAULT_STALE_PROCESSING_HOURS,
) -> dict[str, Any]:
    """READ-ONLY: combined completion-inbox facts for Codex orchestration.

    Returns five facets in one call: ``review_queue`` (tasks awaiting Codex
    review), ``operational_failures`` (legacy finalizer failures eligible for
    retained retry), ``stale_processing`` (claimed tasks with no recent
    artifact/validation activity, default threshold 24h), ``runner_mismatch_warnings``
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
    process_report = process_launcher.default_manager().list_processes(limit=limit)
    compact_processes: list[dict[str, Any]] = []
    for row in process_report.get("processes", []):
        if not isinstance(row, Mapping):
            continue
        compact: dict[str, Any] = {}
        for key in (
            "request_id",
            "task_id",
            "runner",
            "topic",
            "adapter_id",
            "model",
            "state",
            "exit_code",
            "timestamp",
            "finished_at",
            "accepted",
            "review_ready",
            "liveness_lost",
            "workspace_retained",
        ):
            if key in row:
                compact[key] = row[key]
        if row.get("error"):
            compact["error"] = str(row["error"])[:500]
        usage = row.get("usage")
        if isinstance(usage, Mapping):
            compact["usage"] = {
                key: usage[key]
                for key in (
                    "usage_observed",
                    "input_tokens",
                    "output_tokens",
                    "visible_output_tokens",
                    "reasoning_output_tokens",
                    "cached_input_tokens",
                    "cache_creation_input_tokens",
                    "cache_write_input_tokens",
                    "observed_model",
                    "model_observed",
                    "cost_observed",
                    "cost_usd",
                )
                if key in usage
            }
        token_budget = row.get("token_budget")
        if isinstance(token_budget, Mapping):
            compact["token_budget"] = {
                key: token_budget[key]
                for key in (
                    "cap_tokens",
                    "accepted_total_tokens",
                    "cap_enforceable",
                    "telemetry_authority",
                )
                if key in token_budget
            }
        compact_processes.append(compact)
    result["agent_processes"] = {
        key: value
        for key, value in process_report.items()
        if key != "processes"
    }
    result["agent_processes"].update({
        "schema_id": "aiworkhub.completion_process_summary.v1",
        "processes": compact_processes,
        "detail_fields_omitted": True,
        "detail_tool": "aiworkhub_agent_task_status",
    })
    # Additive read-only adapter readiness (installed / credential_present /
    # endpoint / supported_models / launchable / exact blocker_reason) so Codex
    # sees whether a deepseek_copilot_cli launch is possible BEFORE claiming a
    # task. Exposed here (not as a new tool) to preserve the frozen v1 tool
    # contract; it never exposes the DeepSeek key contents or any hash of them.
    repo_root = core.repo_root()
    readiness = deepseek_credentials.adapter_readiness(repo=repo_root)
    # ``adapter_readiness`` preserves credential/endpoint compatibility fields,
    # but executable presence alone is not route launchability (notably for
    # native CLI adapters on Windows without AppContainer). Overlay the exact
    # canonical preflight verdict so this inbox cannot contradict Preflight.
    preflight = repo_policy.build_preflight(repo_root)
    route_truth = {
        str(item.get("adapter_id") or ""): item
        for item in preflight.get("providers", [])
        if isinstance(item, Mapping)
    }
    for adapter in readiness.get("adapters", []):
        if not isinstance(adapter, dict):
            continue
        route = route_truth.get(str(adapter.get("adapter_id") or ""))
        if route is None:
            continue
        adapter["launchable"] = bool(route.get("launchable"))
        adapter["status"] = str(route.get("status") or "unavailable")
        adapter["sandbox_backend"] = str(route.get("sandbox_backend") or "")
        adapter["blocker_reason"] = (
            ""
            if route.get("launchable")
            else str(route.get("reason") or adapter.get("blocker_reason") or "not_launchable")
        )[:300]
        adapter["launchability_authority"] = repo_policy.PREFLIGHT_SCHEMA_ID
    readiness["preflight_schema_id"] = preflight.get("schema_id")
    result["adapter_readiness"] = readiness
    result["operational_failures"] = _enrich_operational_failures_from_processes(
        result.get("operational_failures"), compact_processes
    )
    return result


@mcp.tool()
@_serialize_task_lifecycle_write
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
    Use the exact ``launch_contract`` returned by workforce ranking; the
    manager identity ``codex`` is never a worker runner.
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
@_serialize_task_lifecycle_write
def aiworkhub_quality_reviewer_launch(
    target_request_id: str,
    target_task_id: str,
    reviewer_task_id: str,
    runner: str,
    adapter_id: str,
    lens: Literal["correctness", "security", "code_quality"],
    model: str | None = None,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """DUAL-GATED: launch one independent anti-anchored quality reviewer."""

    return process_launcher.default_manager().launch_quality_reviewer(
        target_request_id=target_request_id,
        target_task_id=target_task_id,
        reviewer_task_id=reviewer_task_id,
        runner=runner,
        adapter_id=adapter_id,
        lens=lens,
        model=model,
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
@_serialize_task_lifecycle_write
def aiworkhub_agent_cancel_task(
    request_id: str,
    reason: str = "owner_cancelled",
) -> dict[str, Any]:
    """DUAL-GATED: terminate one exact process group recorded by this server."""

    return process_launcher.default_manager().cancel(request_id, reason=reason)


@mcp.tool()
@_serialize_task_lifecycle_write
def aiworkhub_agent_retry_finalization(
    request_id: str,
    task_id: str,
) -> dict[str, Any]:
    """COORDINATOR/WRITE-GATED: retry one retained finalization request.

    The provider/model is never launched again. The exact successful worker
    request, retained workspace, task identity and supervisor exit receipt are
    revalidated before deterministic validation/finalization is re-entered.
    A validation failure is eligible only when its durable reason is the
    operational ``validation_exec_scratch_unavailable`` condition; ordinary
    product/test failures remain non-retryable.
    """

    return process_launcher.default_manager().retry_finalization(
        request_id,
        task_id,
    )


@mcp.tool()
@_serialize_task_lifecycle_write
def aiworkhub_agent_accept_review(
    request_id: str,
    task_id: str,
    confirm_destructive_change: bool = False,
    requested_risk_tier: str = quality_evidence.RISK_LOW,
    risk_signals: list[RiskSignal] | None = None,
    reviewer_reports: list[dict[str, Any]] | None = None,
    reviewer_request_ids: list[str] | None = None,
    confirm_high_risk: bool = False,
) -> dict[str, Any]:
    """COORDINATOR/WRITE-GATED: accept one ``review_ready`` request, phase 2.

    Re-runs scope, required-output, and validation gates against the
    retained isolated worktree, compares every changed path's hash against
    the ones recorded when the request first reached review, and only then
    promotes into the canonical repo and finalizes the task as done. Any
    precondition or revalidation failure leaves the canonical repo untouched
    and the task in review. Idempotent: accepting the same already-finished
    request again returns ``already_accepted`` instead of re-promoting.
    Medium-and-higher risk profiles also validate a fresh canonical+candidate
    combined tree and require strict reviewer findings; high/critical review
    requires an explicit ``confirm_high_risk`` decision.
    """

    return process_launcher.default_manager().accept_review(
        request_id,
        task_id,
        confirm_destructive_change=confirm_destructive_change,
        requested_risk_tier=requested_risk_tier,
        risk_signals=risk_signals,
        reviewer_reports=reviewer_reports,
        reviewer_request_ids=reviewer_request_ids,
        confirm_high_risk=confirm_high_risk,
    )


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
def aiworkhub_dispatcher_watchdog() -> dict[str, Any]:
    """Detect and auto-recover a down-but-expected callback dispatcher.

    Intended for the VS Code extension's periodic refresh: when dispatch is
    expected for this process (extension-owned coordinator or verified Claude
    manager) but the dispatcher is unregistered/stopped, lost its repo_id, or
    reports a start error, this re-ensures it in place. A healthy dispatcher, a
    manager-inbox session, or a process where dispatch is not expected is left
    untouched. Accumulated durable-outbox callbacks replay automatically once
    the dispatcher is back."""

    return core.dispatcher_watchdog()


# ---------------------------------------------------------------------------
# 0.6.30: Source Graph automatic indexing lifecycle. Repo-bound (one daemon
# per canonical repository, no global cross-repo DB, no legacy AITools).
# Called by the VS Code extension after every MCP handshake (activation,
# tab-deserialization, reload) and before child termination/repo switch --
# same convention as the dispatcher tools above.
# ---------------------------------------------------------------------------


@mcp.tool()
def aiworkhub_source_graph_ensure_started() -> dict[str, Any]:
    """Idempotently ensure exactly one Source Graph indexing daemon is
    running for the active repository. Safe to call repeatedly (InitRepo,
    activation, tab-deserialization, reload) -- never starts a second
    thread and never fabricates a running daemon for an uninitialized
    repository."""

    return core.source_graph_ensure_started()


@mcp.tool()
def aiworkhub_source_graph_health() -> dict[str, Any]:
    """READ-ONLY: Source Graph indexing daemon health for the active
    repository -- indexing/ready/degraded status, last report/error/time."""

    return core.source_graph_health()


@mcp.tool()
def aiworkhub_source_graph_refresh_now() -> dict[str, Any]:
    """Force one bounded incremental (or first-ever full) Source Graph
    build now, non-overlapping with the periodic background refresh."""

    return core.source_graph_refresh_now()


@mcp.tool()
def aiworkhub_source_graph_stop() -> dict[str, Any]:
    """Stop and unregister the active repository's Source Graph indexing
    daemon, if any. Called on workspace/repository switch and extension
    deactivation so no cross-repository indexing thread survives."""

    return core.source_graph_stop()


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


# ---------------------------------------------------------------------------
# 0.6.30: Quality Evidence Engine -- read-only detection/profile/evidence
# tools plus one bounded run tool that only ever executes repo-declared
# argv-array commands (shell=False). Never installs tools; absence of an
# optional gate reports not_available, never a silent pass.
# ---------------------------------------------------------------------------


@mcp.tool()
def aiworkhub_quality_profile() -> dict[str, Any]:
    """READ-ONLY: zero-config detected languages/declared/installed tools."""

    return quality_evidence.build_zero_config_profile(core.repo_root())


@mcp.tool()
def aiworkhub_known_bug_scan(
    changed_paths: list[str],
    output_format: Literal["native", "sarif"] = "native",
) -> dict[str, Any]:
    """READ-ONLY: scan bounded changed paths for known bug patterns.

    Native and SARIF outputs preserve the same truth boundary: a static
    candidate is not reported as runtime-reproduced evidence.
    """

    report = known_bug_scanner.scan_changed_paths(core.repo_root(), changed_paths)
    return known_bug_scanner.to_sarif(report) if output_format == "sarif" else report


@mcp.tool()
def aiworkhub_eval_artifact_gate(
    changed_paths: list[str] | None = None,
) -> dict[str, Any]:
    """READ-ONLY: recompute registered eval verdicts from retained rows.

    A PASS over zero eligible rows or a summary/count/aggregate divergence is
    returned as blocking evidence. The tool never rewrites an artifact.
    """

    return quality_evidence.eval_artifact_gate.evaluate(
        core.repo_root(), changed_paths=changed_paths
    )


@mcp.tool()
def aiworkhub_contract_consistency_check() -> dict[str, Any]:
    """READ-ONLY: detect managed provider/worker instruction drift."""

    return evidence_instruments.contract_consistency_check(core.repo_root())


@mcp.tool()
def aiworkhub_source_graph_retrieval_eval() -> dict[str, Any]:
    """READ-ONLY: run registered precision@k/MRR cases through manager MCP wrapper."""

    return evidence_instruments.source_graph_retrieval_eval(
        core.repo_root(),
        query_fn=lambda **kwargs: manager_ai_tools.source_graph_query(
            **kwargs, compact_replay=False,
        ),
    )


@mcp.tool()
def aiworkhub_session_token_profile(task_id: str | None = None) -> dict[str, Any]:
    """READ-ONLY: observed provider tokens plus separately-labelled prompt bytes."""

    return evidence_instruments.session_token_profile(core.repo_root(), task_id=task_id)


@mcp.tool()
def aiworkhub_risk_mode_precision_bench() -> dict[str, Any]:
    """READ-ONLY: per-mode/language precision from adjudicated registered rows."""

    return evidence_instruments.risk_mode_precision_bench(core.repo_root())


@mcp.tool()
def aiworkhub_quality_gate_ratchet(
    violations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """READ-ONLY: report new quality identities and no-net-growth overflow."""

    return evidence_instruments.quality_gate_ratchet(
        core.repo_root(), violations=violations or []
    )


@mcp.tool()
def aiworkhub_prompt_bundle_ab_report() -> dict[str, Any]:
    """READ-ONLY: evaluate preregistered matched A/B provider-usage rows."""

    return evidence_instruments.prompt_bundle_ab_report(core.repo_root())


@mcp.tool()
def aiworkhub_suite_profile(
    check_id: str,
    repeats: int = 2,
) -> dict[str, Any]:
    """Execute one repo-declared quality check repeatedly; shell is never used."""

    descriptors = quality_evidence.declared_check_descriptors(core.repo_root())
    selected = next((row for row in descriptors if row.check_id == check_id), None)
    if selected is None:
        return {"ok": False, "error": "declared_check_not_found", "check_id": check_id}
    argv = [sys.executable if part == "{python}" else part for part in selected.command]
    return evidence_instruments.suite_profile(
        core.repo_root(), argv=argv, repeats=repeats
    )


@mcp.tool()
def aiworkhub_runtime_coverage_preview(artifact_path: str) -> dict[str, Any]:
    """READ-ONLY: parse a repo-local coverage.py JSON artifact."""

    try:
        return evidence_instruments.coverage_import_preview(
            core.repo_root(), artifact_path=artifact_path
        )
    except evidence_instruments.EvidenceInstrumentError as exc:
        return {"ok": False, "error": str(exc)[:240]}


@mcp.tool()
def aiworkhub_runtime_coverage_import(artifact_path: str) -> dict[str, Any]:
    """WRITE-GATED: atomically project repo-local coverage into Source Graph."""

    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed"}
    try:
        return evidence_instruments.coverage_import_apply(
            core.repo_root(), artifact_path=artifact_path
        )
    except evidence_instruments.EvidenceInstrumentError as exc:
        return {"ok": False, "error": str(exc)[:240]}


@mcp.tool()
def aiworkhub_quality_run_checks(changed_paths: list[str] | None = None) -> dict[str, Any]:
    """Run repo-declared .aiworkhub/quality.json build/test/lint/typecheck
    commands (argv arrays only, shell=False). Fails closed on malformed
    config. Diff/new-code-first: pass ``changed_paths`` to scope evidence."""

    root = core.repo_root()
    try:
        config_status = quality_evidence.repo_config_status(root)
        if config_status["status"] != "configured":
            return {
                "ok": False,
                "status": "unverified",
                "error": config_status["reason"],
                "schema_id": quality_evidence.SCHEMA_ID,
                "checks": [],
                "repository_quality_policy": config_status,
            }
        checks = quality_evidence.run_declared_checks(root, changed_paths=changed_paths)
    except quality_evidence.MalformedConfigError as exc:
        return {"ok": False, "error": str(exc), "schema_id": quality_evidence.SCHEMA_ID}
    return {
        "ok": True,
        "status": "verified" if all(c.status == quality_evidence.STATUS_PASSED for c in checks) else "unverified",
        "schema_id": quality_evidence.SCHEMA_ID,
        "checks": [c.to_dict() for c in checks],
        "repository_quality_policy": config_status,
    }


@mcp.tool()
def aiworkhub_quality_evidence_packet(changed_paths: list[str] | None = None) -> dict[str, Any]:
    """READ-ONLY: full canonical evidence packet -- profile, declared
    checks, risk-based optional-gate policy metadata (not_available !=
    passed), and the cross-provider read-only quality_reviewer contract."""

    return quality_evidence.build_evidence_packet(core.repo_root(), changed_paths=changed_paths)


@mcp.tool()
def aiworkhub_quality_calibration_report() -> dict[str, Any]:
    """READ-ONLY: run the deterministic positive/negative gate matrix."""

    return quality_calibration.run_calibration()


@mcp.tool()
def aiworkhub_environment_preflight(adapter_id: str | None = None) -> dict[str, Any]:
    """READ-ONLY: unified repository/policy/Source Graph/provider readiness.

    Provider access is reported as observed only when an adapter-specific
    bridge or credential readiness source proves it; installed CLI binaries
    alone remain ``installed_unverified_access``.
    """

    return repo_policy.build_preflight(core.repo_root(), adapter_id=adapter_id)


def _enforce_terminal_retention_safely(root: Path) -> None:
    """Apply retention without ever crashing or polluting MCP stdio startup."""

    try:
        terminal_log_retention.enforce(root)
    except Exception:
        # Retention remains observable through its preview/audit surfaces and
        # can be retried on the next startup.  A cleanup failure must never
        # make the task-control MCP unavailable.
        return


def _start_task_reconciler_safely(root: Path) -> None:
    """Start the task reconciler without ever making MCP unavailable.

    Runs on a daemon thread: reconciliation walks the task store and the
    retained workspaces, which is measured in tens of seconds on a large
    repository, and blocking the stdio thread for that long makes `initialize`
    miss the client's request deadline.  Failure stays diagnosable through the
    reconciler's own health surface and the next reload retries registration.
    """
    try:
        task_reconciler.ensure_started(root)
    except Exception:
        pass

def main() -> None:
    root = core.repo_root()
    # Additive schema migration for repositories created before NeedFix was
    # introduced. This runs before any read-only dashboard/list call so those
    # surfaces remain truthful and do not need hidden write side effects.
    try:
        task_store.ensure_needfix_storage(root)
    except Exception:
        # Keep MCP diagnostics alive if the auxiliary store cannot be opened;
        # the bounded NeedFix APIs will then return the concrete store error.
        pass
    try:
        roadmap_store.initialize_repository(root)
    except Exception:
        # Roadmap is additive; keep diagnostics/NeedFix/task control alive and
        # let the bounded Roadmap tools report the concrete storage failure.
        pass
    # Every independently launched manager MCP child owns its repository's
    # in-process Source Graph daemon.  Dashboard activation already calls the
    # public ensure tool, but Codex/Claude/Copilot may start this stdio server
    # without opening the dashboard.  Bootstrap the idempotent daemon here so
    # a fresh/reloaded chat never observes a permanently empty/stopped graph.
    # Uninitialized repositories remain a harmless no-op.
    try:
        core.source_graph_ensure_started()
    except Exception:
        # MCP must remain available so health/InitRepo can explain or repair
        # indexing; startup indexing failure must not kill the whole server.
        pass
    # Retention is repository policy, not a dashboard-only manual action.
    # Run it off the MCP stdio thread so startup remains responsive.
    threading.Thread(
        target=_enforce_terminal_retention_safely,
        args=(root,),
        name=f"aiworkhub-log-retention-{abs(hash(str(root))) & 0xffff:x}",
        daemon=True,
    ).start()
    # Reconciliation scans every task row and every retained workspace, so on a
    # large repository it takes tens of seconds.  Run it off the MCP stdio thread
    # for the same reason retention is: a synchronous call here delays the reply
    # to `initialize`, and a client whose request deadline is shorter than the
    # scan reports mcp_initialize_failed:mcp_request_timeout against a server
    # that is perfectly healthy.
    threading.Thread(
        target=_start_task_reconciler_safely,
        args=(root,),
        name=f"aiworkhub-task-reconciler-{abs(hash(str(root))) & 0xffff:x}",
        daemon=True,
    ).start()
    try:
        mcp.run()
    finally:
        task_reconciler.stop_reconciler(root)


# --- NeedFix MCP manager tools (additive; bounded, repo-scoped) ---


@mcp.tool()
def needfix_list(
    status: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
    order_by: str = "created_at",
    order_dir: str = "DESC",
) -> list[dict]:
    """List repo-scoped NeedFix entries (bounded)."""
    return core.needfix_list(
        status=status,
        kind=kind,
        severity=severity,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
        order_by=order_by,
        order_dir=order_dir,
    )


@mcp.tool()
def needfix_show(needfix_id: str) -> dict:
    """Show one NeedFix by id."""
    return core.needfix_show(needfix_id)


@mcp.tool()
def needfix_add(
    title: str,
    description: str,
    scope: str | None = None,
    provenance: dict | None = None,
    evidence: dict | None = None,
    status: str = "captured",
    kind: str = "other",
    severity: str = "medium",
    tags: list[str] | None = None,
    scope_files: list[str] | None = None,
    scope_symbols: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    readiness_score: int = 0,
) -> dict:
    """Manager-authority add of a NeedFix (dedupe-aware)."""
    return core.needfix_add(
        title=title,
        description=description,
        scope=scope,
        provenance=provenance,
        evidence=evidence,
        status=status,
        kind=kind,
        severity=severity,
        tags=tags,
        scope_files=scope_files,
        scope_symbols=scope_symbols,
        evidence_refs=evidence_refs,
        readiness_score=readiness_score,
    )


@mcp.tool()
def needfix_update(
    needfix_id: str,
    title: str | None = None,
    description: str | None = None,
    scope: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    tags: list[str] | None = None,
    scope_files: list[str] | None = None,
    scope_symbols: list[str] | None = None,
    evidence: dict | None = None,
    evidence_refs: list[str] | None = None,
    readiness_score: int | None = None,
) -> dict:
    """Manager update of mutable NeedFix fields."""
    return core.needfix_update(
        needfix_id,
        title=title,
        description=description,
        scope=scope,
        kind=kind,
        severity=severity,
        tags=tags,
        scope_files=scope_files,
        scope_symbols=scope_symbols,
        evidence=evidence,
        evidence_refs=evidence_refs,
        readiness_score=readiness_score,
    )


@mcp.tool()
def needfix_archive(needfix_id: str, reason: str | None = None) -> dict:
    """Explicitly archive (soft, audited, restorable) a NeedFix."""
    return core.needfix_archive(needfix_id, reason=reason)


@mcp.tool()
def needfix_restore(needfix_id: str, target_status: str = "captured") -> dict:
    """Explicitly restore an archived NeedFix."""
    return core.needfix_restore(needfix_id, target_status=target_status)


@mcp.tool()
def needfix_purge(needfix_id: str, audit_reason: str) -> dict:
    """Audited hard delete of an already-archived NeedFix."""
    return core.needfix_purge(needfix_id, audit_reason)


@mcp.tool()
def needfix_count(status: str | None = None, kind: str | None = None, severity: str | None = None) -> int:
    """Bounded count of NeedFix entries."""
    return core.needfix_count(status=status, kind=kind, severity=severity)


@mcp.tool()
def needfix_events(needfix_id: str, limit: int = 100) -> list[dict]:
    """List events for a NeedFix entry."""
    return core.needfix_events(needfix_id, limit=limit)


@mcp.tool()
def needfix_preview_convert(
    needfix_id: str, task_plan: dict[str, Any] | None = None
) -> dict:
    """Preview an explicit workforce-bound task conversion."""
    return core.needfix_preview_convert(needfix_id, task_plan=task_plan)


@mcp.tool()
def needfix_convert(
    needfix_id: str,
    task_plan: dict[str, Any] | None = None,
    plan_digest: str | None = None,
) -> dict:
    """Commit the exact previewed workforce-bound task conversion."""
    return core.needfix_convert(
        needfix_id, task_plan=task_plan, plan_digest=plan_digest
    )


@mcp.tool()
def needfix_link_existing_task(needfix_id: str, existing_task_id: str) -> dict:
    """Manager-only: atomically link a NeedFix to an existing, finished, accepted task."""
    return core.needfix_link_existing_task(needfix_id, existing_task_id)


@mcp.tool()
def needfix_reopen_superseded_task_link(needfix_id: str, reason: str) -> dict:
    """Manager-only: reopen a NeedFix whose exact linked task was superseded."""
    return core.needfix_reopen_superseded_task_link(needfix_id, reason)


# --- Roadmap MCP manager tools (repository-native; never launch workers) ---


@mcp.tool()
def roadmap_list(
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List bounded repository Roadmap outcomes."""
    return core.roadmap_list(
        status=status,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def roadmap_show(roadmap_id: str) -> dict:
    """Show one Roadmap outcome."""
    return core.roadmap_show(roadmap_id)


@mcp.tool()
def roadmap_events(roadmap_id: str, limit: int = 100) -> list[dict]:
    """List bounded audit events for one Roadmap outcome."""
    return core.roadmap_events(roadmap_id, limit=limit)


@mcp.tool()
def roadmap_snapshot(limit: int = 200, include_archived: bool = False) -> dict:
    """Join bounded Roadmap outcomes to canonical Task-DAG lifecycle."""
    return core.roadmap_snapshot(limit=limit, include_archived=include_archived)


@mcp.tool()
def roadmap_add(
    title: str,
    outcome: str,
    priority: str = "medium",
    milestone: str = "",
    acceptance: list[str] | None = None,
    needfix_ids: list[str] | None = None,
    depends_on: list[str] | None = None,
    provenance: dict | None = None,
    evidence_refs: list[str] | None = None,
) -> dict:
    """Manager-only Roadmap proposal; does not create or launch a task."""
    return core.roadmap_add(
        title,
        outcome,
        priority=priority,
        milestone=milestone,
        acceptance=acceptance,
        needfix_ids=needfix_ids,
        depends_on=depends_on,
        provenance=provenance,
        evidence_refs=evidence_refs,
    )


@mcp.tool()
def roadmap_transition(roadmap_id: str, target_status: str, reason: str) -> dict:
    """Manager-only guarded Roadmap lifecycle transition."""
    return core.roadmap_transition(roadmap_id, target_status, reason=reason)


@mcp.tool()
def roadmap_link_task(roadmap_id: str, task_id: str) -> dict:
    """Link one existing canonical task to a Roadmap outcome."""
    return core.roadmap_link_task(roadmap_id, task_id)


if __name__ == "__main__":
    main()
