"""NF-2026-00973: MCP 2026-07-28 stateless core and the Tasks extension.

One owner for both stdio dispatchers -- ``server._stdio_dispatch`` (the
production stdlib backend) and ``stdio_fastmcp._dispatch`` (worker MCP) -- so
the two paths cannot drift.

Dual mode, backward compatible:

* A request whose ``_meta`` does not name
  ``io.modelcontextprotocol/protocolVersion`` (or names an initialize-era
  revision) takes the caller's legacy dispatcher unchanged; ``initialize``
  keeps negotiating only :data:`stdio_fastmcp.SUPPORTED_PROTOCOL_VERSIONS`.
* A request naming 2026-07-28 is served without ``initialize``; its result
  carries ``resultType`` and ``_meta`` serverInfo, and the methods that
  revision removed (``initialize``, ``ping``, ``logging/setLevel``) are
  refused. Any other named version is refused with -32022 and the supported
  list.
* ``server/discover`` answers in both modes.
* Tasks extension: only a 2026-07-28 request whose per-request
  clientCapabilities.extensions declares ``io.modelcontextprotocol/tasks``
  turns ``aiworkhub_agent_launch_task`` into a CreateTaskResult (taskId = the
  launch request_id) and may use ``tasks/get`` / ``tasks/cancel``.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import __version__
from .task_fsm import LAUNCHER_TERMINAL_SUBSTATUSES

STATELESS_PROTOCOL_VERSIONS = ("2026-07-28",)

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"

METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022

# How long a client may reuse a server/discover reply (5 min).
DISCOVER_TTL_MS = 300_000
# A launched worker's status stays readable from the process ledger; promise
# one day of task retention and a 5 s poll cadence.
TASK_TTL_MS = 86_400_000
TASK_POLL_INTERVAL_MS = 5_000

# Removed in 2026-07-28; still served on the initialize path.
LEGACY_ONLY_METHODS = frozenset(
    {"initialize", "notifications/initialized", "ping", "logging/setLevel"}
)

TASK_LAUNCH_TOOL = "aiworkhub_agent_launch_task"
TASK_STATUS_TOOL = "aiworkhub_agent_task_status"
TASK_CANCEL_TOOL = "aiworkhub_agent_cancel_task"
TASK_TOOLS = (TASK_LAUNCH_TOOL, TASK_STATUS_TOOL, TASK_CANCEL_TOOL)
TASK_CANCEL_REASON = "mcp_tasks_cancel"

# aiworkhub_agent_task_status lifecycle -> the spec's task states. ``state`` is
# the launcher's process state, whose vocabulary is owned by
# task_fsm.LAUNCHER_TERMINAL_SUBSTATUSES: every terminal member that is neither
# a success nor a cancellation is a failure (a crashed worker must never poll
# as working forever). Anything outside that vocabulary is still working.
TASK_STATE_COMPLETED = frozenset({"review_ready", "accepted"})
TASK_STATE_CANCELLED = frozenset({"cancelled"})
TASK_STATE_FAILED = LAUNCHER_TERMINAL_SUBSTATUSES - TASK_STATE_COMPLETED - TASK_STATE_CANCELLED
# Fallback when ``state`` is absent: the card's canonical task_card.status.
CARD_STATUS_COMPLETED = frozenset({"done"})
CARD_STATUS_FAILED = frozenset({"blocked", "failed"})
CARD_STATUS_CANCELLED = frozenset({"cancelled", "archived"})

_ECHO_MAX_LEN = 128

LegacyDispatch = Callable[[Any, dict[str, Any]], Any]
ErrorFactory = Callable[[int, str], Exception]
ToolAvailable = Callable[[str], bool]


def supported_protocol_versions() -> tuple[str, ...]:
    """Every revision this server speaks: initialize-era first, then stateless."""

    # Lazy: stdio_fastmcp imports this module at load time.
    from .stdio_fastmcp import SUPPORTED_PROTOCOL_VERSIONS

    return tuple(SUPPORTED_PROTOCOL_VERSIONS) + STATELESS_PROTOCOL_VERSIONS


def protocol_error(
    error_type: ErrorFactory, code: int, message: str, data: Any = None
) -> Exception:
    """Build the caller's JSON-RPC error, carrying optional ``data``."""

    exc = error_type(code, message)
    if data is not None:
        setattr(exc, "data", data)
    return exc


def error_object(exc: Any) -> dict[str, Any]:
    """The JSON-RPC ``error`` member for a dispatcher protocol error."""

    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    data = getattr(exc, "data", None)
    if data is not None:
        error["data"] = data
    return error


def server_info(server_name: str) -> dict[str, Any]:
    return {"name": server_name, "version": __version__}


def tasks_supported(tool_available: ToolAvailable) -> bool:
    return all(tool_available(name) for name in TASK_TOOLS)


def server_capabilities(tool_available: ToolAvailable) -> dict[str, Any]:
    capabilities: dict[str, Any] = {"tools": {}, "resources": {}}
    if tasks_supported(tool_available):
        capabilities["extensions"] = {TASKS_EXTENSION: {}}
    return capabilities


def discover_result(
    server_name: str, tool_available: ToolAvailable, instructions: str = ""
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "serverInfo": server_info(server_name),
        "capabilities": server_capabilities(tool_available),
        "supportedVersions": list(supported_protocol_versions()),
        "ttlMs": DISCOVER_TTL_MS,
        "cacheScope": "private",
    }
    if instructions:
        result["instructions"] = instructions
    return stamp_result(result, server_name)


def stamp_result(result: Any, server_name: str) -> Any:
    """Add ``resultType`` (default complete) and ``_meta`` serverInfo to a result."""

    if not isinstance(result, dict):
        return result
    stamped = dict(result)
    stamped.setdefault("resultType", "complete")
    meta = stamped.get("_meta")
    meta = dict(meta) if isinstance(meta, dict) else {}
    meta[META_SERVER_INFO] = server_info(server_name)
    stamped["_meta"] = meta
    return stamped


def _echo(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:_ECHO_MAX_LEN]


def declares_tasks(meta: Any) -> bool:
    """True only when per-request clientCapabilities.extensions declares tasks."""

    if not isinstance(meta, dict):
        return False
    capabilities = meta.get(META_CLIENT_CAPABILITIES)
    if not isinstance(capabilities, dict):
        return False
    extensions = capabilities.get("extensions")
    return isinstance(extensions, dict) and isinstance(extensions.get(TASKS_EXTENSION), dict)


def task_status_for(payload: dict[str, Any]) -> str:
    """Map an aiworkhub_agent_task_status payload onto a spec task state.

    ``state`` (the launcher's process state) decides when present; only when
    it is absent does the card's ``task_card.status`` decide. Anything not
    recognised as terminal is still working -- never a success.
    """

    state = str(payload.get("state") or "").strip()
    if state:
        if state in TASK_STATE_COMPLETED:
            return "completed"
        if state in TASK_STATE_CANCELLED:
            return "cancelled"
        if state in TASK_STATE_FAILED:
            return "failed"
        return "working"
    card = payload.get("task_card")
    card_status = str(card.get("status") or "").strip() if isinstance(card, dict) else ""
    if card_status in CARD_STATUS_COMPLETED:
        return "completed"
    if card_status in CARD_STATUS_CANCELLED:
        return "cancelled"
    if card_status in CARD_STATUS_FAILED:
        return "failed"
    return "working"


def tool_payload(result: Any) -> dict[str, Any] | None:
    """The JSON object a successful single-text CallToolResult carries, else None."""

    if not isinstance(result, dict) or result.get("isError"):
        return None
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return None
    item = content[0]
    if not isinstance(item, dict) or item.get("type") != "text":
        return None
    try:
        payload = json.loads(item.get("text"))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _tool_error_text(result: Any) -> str:
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            return _echo(content[0].get("text", ""))
    return "unreadable_tool_result"


def _launch_as_task(params: dict[str, Any], legacy: LegacyDispatch) -> Any:
    result = legacy("tools/call", params)
    payload = tool_payload(result)
    if payload is None or payload.get("ok") is False:
        return result  # a refused or failed launch is never a task
    task_id = payload.get("request_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return result
    created: dict[str, Any] = {
        "resultType": "task",
        "taskId": task_id,
        "status": "working",
        "ttlMs": TASK_TTL_MS,
        "pollIntervalMs": TASK_POLL_INTERVAL_MS,
    }
    state = payload.get("state")
    if isinstance(state, str) and state:
        created["statusMessage"] = state
    return created


def _task_id(params: dict[str, Any], error_type: ErrorFactory) -> str:
    task_id = params.get("taskId")
    if not isinstance(task_id, str) or not task_id.strip():
        raise protocol_error(error_type, INVALID_PARAMS, "invalid_params:taskId_required")
    return task_id


def _payload_refusal(payload: dict[str, Any], task_id: str, error_type: ErrorFactory) -> Exception:
    if payload.get("state") == "not_found":
        return protocol_error(error_type, INVALID_PARAMS, f"task_not_found:{_echo(task_id)}")
    reason = payload.get("error") or payload.get("reason") or payload.get("state") or "refused"
    return protocol_error(error_type, INVALID_PARAMS, f"task_refused:{_echo(reason)}")


def _tasks_get(task_id: str, legacy: LegacyDispatch, error_type: ErrorFactory) -> dict[str, Any]:
    reply = legacy("tools/call", {"name": TASK_STATUS_TOOL, "arguments": {"request_id": task_id}})
    payload = tool_payload(reply)
    if payload is None:
        raise protocol_error(
            error_type, INTERNAL_ERROR, f"task_status_unreadable:{_tool_error_text(reply)}"
        )
    if payload.get("ok") is False:
        raise _payload_refusal(payload, task_id, error_type)
    status = task_status_for(payload)
    result: dict[str, Any] = {
        "taskId": task_id,
        "status": status,
        "ttlMs": TASK_TTL_MS,
        "pollIntervalMs": TASK_POLL_INTERVAL_MS,
    }
    lifecycle = str(payload.get("state") or "")
    if lifecycle:
        result["statusMessage"] = lifecycle
    if status == "completed":
        result["result"] = {**reply, "resultType": "complete"}
    elif status == "failed":
        result["error"] = {
            "code": INTERNAL_ERROR,
            "message": f"task_failed:{_echo(lifecycle or 'unknown')}",
            "data": payload,
        }
    return result


def _tasks_cancel(task_id: str, legacy: LegacyDispatch, error_type: ErrorFactory) -> dict[str, Any]:
    reply = legacy(
        "tools/call",
        {"name": TASK_CANCEL_TOOL, "arguments": {"request_id": task_id, "reason": TASK_CANCEL_REASON}},
    )
    payload = tool_payload(reply)
    if payload is None:
        raise protocol_error(
            error_type, INTERNAL_ERROR, f"task_cancel_failed:{_tool_error_text(reply)}"
        )
    if payload.get("ok") is False:
        raise _payload_refusal(payload, task_id, error_type)
    return {}


def _tasks_method(
    method: str,
    params: dict[str, Any],
    meta: dict[str, Any],
    *,
    legacy: LegacyDispatch,
    error_type: ErrorFactory,
    tool_available: ToolAvailable,
) -> dict[str, Any]:
    if method not in ("tasks/get", "tasks/cancel", "tasks/update") or not tasks_supported(
        tool_available
    ):
        raise protocol_error(error_type, METHOD_NOT_FOUND, f"method_not_found:{_echo(method)}")
    if not declares_tasks(meta):
        raise protocol_error(
            error_type,
            MISSING_REQUIRED_CLIENT_CAPABILITY,
            f"missing_required_client_capability:{TASKS_EXTENSION}",
            {"requiredCapabilities": {"extensions": {TASKS_EXTENSION: {}}}},
        )
    if method == "tasks/update":
        raise protocol_error(
            error_type, INVALID_PARAMS, "tasks_update_unsupported:no_input_required_flow"
        )
    task_id = _task_id(params, error_type)
    if method == "tasks/get":
        return _tasks_get(task_id, legacy, error_type)
    return _tasks_cancel(task_id, legacy, error_type)


def dispatch(
    *,
    server_name: str,
    method: Any,
    params: dict[str, Any],
    legacy: LegacyDispatch,
    error_type: ErrorFactory,
    tool_available: ToolAvailable,
    instructions: str = "",
) -> Any:
    """Serve one validated request in initialize-era or 2026-07-28 mode.

    ``legacy(method, params)`` is the caller's unchanged pre-2026 dispatcher;
    ``error_type(code, message)`` builds the caller's JSON-RPC error.
    """

    if method == "server/discover":
        return discover_result(server_name, tool_available, instructions)
    meta = params.get("_meta")
    if not isinstance(meta, dict) or META_PROTOCOL_VERSION not in meta:
        return legacy(method, params)
    version = meta[META_PROTOCOL_VERSION]
    supported = supported_protocol_versions()
    if isinstance(version, str) and version in supported and version not in STATELESS_PROTOCOL_VERSIONS:
        return legacy(method, params)
    if not (isinstance(version, str) and version in STATELESS_PROTOCOL_VERSIONS):
        raise protocol_error(
            error_type,
            UNSUPPORTED_PROTOCOL_VERSION,
            f"unsupported_protocol_version:{_echo(version)}; supported: {', '.join(supported)}",
            {"supported": list(supported), "requested": _echo(version)},
        )
    if method in LEGACY_ONLY_METHODS:
        raise protocol_error(error_type, METHOD_NOT_FOUND, f"method_not_found:{method}")
    if isinstance(method, str) and method.startswith("tasks/"):
        result = _tasks_method(
            method,
            params,
            meta,
            legacy=legacy,
            error_type=error_type,
            tool_available=tool_available,
        )
    elif (
        method == "tools/call"
        and params.get("name") == TASK_LAUNCH_TOOL
        and declares_tasks(meta)
        and tasks_supported(tool_available)
    ):
        result = _launch_as_task(params, legacy)
    else:
        result = legacy(method, params)
    if method == "tools/list":
        # Lazy: stdio_fastmcp imports this module at load time. The cache hints
        # are 2026-07-28 fields, so only this stateless branch carries them.
        from .stdio_fastmcp import stateless_tools_list_envelope

        result = stateless_tools_list_envelope(result)
    return stamp_result(result, server_name)


__all__ = [
    "META_CLIENT_CAPABILITIES",
    "META_PROTOCOL_VERSION",
    "META_SERVER_INFO",
    "STATELESS_PROTOCOL_VERSIONS",
    "TASKS_EXTENSION",
    "UNSUPPORTED_PROTOCOL_VERSION",
    "discover_result",
    "dispatch",
    "error_object",
    "supported_protocol_versions",
    "task_status_for",
]
