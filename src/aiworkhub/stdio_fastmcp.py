"""Small dependency-free FastMCP-compatible stdio server.

The packaged VS Code runtime deliberately does not depend on site packages.
Worker-side MCP servers therefore use this bounded JSON-RPC implementation
when the optional :mod:`mcp` package is unavailable.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import types
import typing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .tool_recovery import unknown_tool_message


PROTOCOL_VERSION = "2024-11-05"
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TransportClosed(SystemExit):
    """The MCP client closed stdout, so the server must stop cleanly."""


def _stderr_event(event: str, **fields: Any) -> None:
    record = {"component": "aiworkhub.worker_mcp_stdio", "event": event}
    record.update({key: value for key, value in fields.items() if value is not None})
    try:
        sys.stderr.write(json.dumps(record, ensure_ascii=False, default=str)[:4000] + "\n")
        sys.stderr.flush()
    except (BrokenPipeError, OSError):
        pass


def _json_type(annotation: Any) -> str:
    if annotation is inspect.Signature.empty:
        return "string"
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        return _json_type(args[0]) if len(args) == 1 else "string"
    if origin in (list, tuple):
        return "array"
    if origin is dict:
        return "object"
    return {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }.get(annotation, "string")


def _json_schema_for_annotation(annotation: Any) -> dict[str, Any]:
    """Return the bounded JSON-schema fragment used by ``tools/list``.

    ``Literal`` support is important for model-facing contracts: an opaque
    string makes agents guess accepted operation names and waste tool calls.
    """

    if annotation is Any:
        return {}
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        values = list(typing.get_args(annotation))
        schema: dict[str, Any] = {"enum": values}
        if values:
            schema["type"] = _json_type(type(values[0]))
        return schema
    if origin in (typing.Union, types.UnionType):
        args = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return _json_schema_for_annotation(args[0])
    if origin in (list, tuple) or annotation in (list, tuple):
        args = typing.get_args(annotation)
        item_annotation = args[0] if args else Any
        return {
            "type": "array",
            "items": _json_schema_for_annotation(item_annotation),
        }
    if origin is dict or annotation is dict:
        args = typing.get_args(annotation)
        value_annotation = args[1] if len(args) == 2 else Any
        return {
            "type": "object",
            "additionalProperties": _json_schema_for_annotation(value_annotation),
        }
    return {"type": _json_type(annotation)}


def _schema_for(func: Any) -> dict[str, Any]:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}, "additionalProperties": True}
    try:
        resolved_hints = typing.get_type_hints(func)
    except (NameError, TypeError):
        resolved_hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if name == "self":
            continue
        properties[name] = _json_schema_for_annotation(
            resolved_hints.get(name, parameter.annotation)
        )
        if parameter.default is inspect.Signature.empty:
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _tool_result(tools: dict[str, Any], params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ProtocolError(-32602, "invalid_params")
    name = params.get("name")
    if not isinstance(name, str) or name not in tools:
        raise ProtocolError(-32602, unknown_tool_message(name, tools))
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ProtocolError(-32602, "arguments_must_be_object")
    function = tools[name]
    try:
        allowed = set(inspect.signature(function).parameters)
    except (TypeError, ValueError):
        allowed = set(arguments)
    unexpected = sorted(set(arguments) - allowed)
    if unexpected:
        raise ProtocolError(-32602, f"unexpected_arguments:{unexpected}")
    try:
        value = function(**arguments)
    except Exception as exc:  # one tool failure must not terminate the server
        return {"content": [{"type": "text", "text": str(exc)[:2000]}], "isError": True}
    structured = value if isinstance(value, dict) else {"value": value}
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}],
        "structuredContent": structured,
    }


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
                "boundary": "stdio_fastmcp",
                "reason": reason[:_PROTOCOL_ALERT_FIELD_MAX_LEN],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        pass


def _validate_rpc_params(method: Any, params: Any, *, request_id: Any) -> dict[str, Any]:
    """Fail closed on non-object params before any dispatch reaches a tool."""

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
    raise ProtocolError(-32602, reason)


def _dispatch(
    server_name: str, tools: dict[str, Any], method: Any, params: Any, request_id: Any = None
) -> Any:
    params = _validate_rpc_params(method, params, request_id=request_id)
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": server_name, "version": __version__},
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {}
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": name,
                    "description": (inspect.getdoc(function) or "").strip().splitlines()[0]
                    if (inspect.getdoc(function) or "").strip()
                    else "",
                    "inputSchema": _schema_for(function),
                }
                for name, function in tools.items()
            ]
        }
    if method == "tools/call":
        return _tool_result(tools, params)
    if method == "resources/list":
        return {"resources": []}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    raise ProtocolError(-32601, f"method_not_found:{method}")


def _write(message: dict[str, Any]) -> None:
    payload = json.dumps(
        message, ensure_ascii=False, default=str,
    ).encode("utf-8")
    if len(payload) > MAX_RESPONSE_BYTES:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {"code": -32603, "message": "response_too_large"},
        }).encode("utf-8")
    try:
        # JSON-RPC stdio is UTF-8 bytes, independent of the Windows console
        # code page or PYTHONIOENCODING inherited by the worker process.
        sys.stdout.buffer.write(payload + b"\n")
        sys.stdout.buffer.flush()
    except (BrokenPipeError, OSError) as exc:
        _stderr_event(
            "transport_closed",
            request_id=message.get("id"),
            error_type=type(exc).__name__,
        )
        raise TransportClosed(0) from exc


def _run(server_name: str, tools: dict[str, Any]) -> None:
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline(MAX_LINE_BYTES + 1)
        if line == b"":
            return
        oversized = len(line) > MAX_LINE_BYTES
        if oversized and not line.endswith(b"\n"):
            while True:
                remainder = stdin.readline(MAX_LINE_BYTES + 1)
                if remainder == b"" or remainder.endswith(b"\n"):
                    break
        if oversized:
            _write({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "request_too_large"}})
            continue
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("object_required")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse_error"}})
            continue
        if "id" not in request:
            continue
        request_id = request.get("id")
        try:
            result = _dispatch(server_name, tools, request.get("method"), request.get("params"), request_id)
            _write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except ProtocolError as exc:
            _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:
            _stderr_event(
                "request_failed",
                request_id=request_id,
                method=request.get("method"),
                error_type=type(exc).__name__,
            )
            _write({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": f"internal_error:{type(exc).__name__}"},
            })


class FallbackFastMCP:
    """FastMCP's decorator/run subset backed only by the standard library."""

    def __init__(self, name: str):
        self.name = name
        self._tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):
        del args

        def decorate(function: Any) -> Any:
            self._tools[str(kwargs.get("name") or function.__name__)] = function
            return function

        return decorate

    @property
    def registered_tools(self) -> list[str]:
        return list(self._tools)

    def run(self) -> None:
        _run(self.name, self._tools)


__all__ = ["FallbackFastMCP"]
