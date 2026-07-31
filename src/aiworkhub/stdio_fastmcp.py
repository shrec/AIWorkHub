"""Small dependency-free FastMCP-compatible stdio server.

The packaged VS Code runtime deliberately does not depend on site packages.
Worker-side MCP servers therefore use this bounded JSON-RPC implementation
when the optional :mod:`mcp` package is unavailable.
"""

from __future__ import annotations

import inspect
import json
import sys
import types
import typing
from typing import Any

from . import __version__


PROTOCOL_VERSION = "2024-11-05"
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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
        raise ProtocolError(-32602, f"unknown_tool:{name!r}")
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


def _dispatch(server_name: str, tools: dict[str, Any], method: Any, params: Any) -> Any:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
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
    raise ProtocolError(-32601, f"method_not_found:{method}")


def _write(message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False, default=str)
    if len(payload.encode("utf-8")) > MAX_RESPONSE_BYTES:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {"code": -32603, "message": "response_too_large"},
        })
    sys.stdout.write(payload + "\n")
    sys.stdout.flush()


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
            result = _dispatch(server_name, tools, request.get("method"), request.get("params") or {})
            _write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except ProtocolError as exc:
            _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:
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
