"""Docstring-derived MCP tool metadata for Tool Definition Quality Score.

Parses the Google-style ``Args:``/``Parameters:`` section of a tool docstring
into per-parameter ``pydantic.Field`` descriptions and derives MCP
``ToolAnnotations`` hints, so the single registration choke point in
``server._nonblocking_mcp_tool`` can publish both without changing any tool's
docstring text.

Module scope imports only the stdlib; ``pydantic`` and ``mcp.types`` are
imported lazily so this module stays importable when the optional ``mcp`` SDK
(and its ``pydantic`` dependency) is absent.
"""

from __future__ import annotations

import inspect
import typing
from typing import Any

__all__ = ["split_docstring", "derive_annotations", "prepare_registration"]

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
    return "\n".join(kept), params


def derive_annotations(tool_name: str, description: str) -> dict[str, Any] | None:
    """Derive MCP ``ToolAnnotations`` hints from a tool name and description."""
    name = tool_name if isinstance(tool_name, str) else ""
    desc = description if isinstance(description, str) else ""
    first_line = desc.split("\n", 1)[0].strip() if desc else ""
    if first_line.startswith(("READ-ONLY", "MANAGER READ", "READ:")):
        return {"readOnlyHint": True}
    if first_line.startswith("DESTRUCTIVE") or any(
        token in name for token in ("_purge", "_delete", "_discard")
    ):
        return {"readOnlyHint": False, "destructiveHint": True}
    return None


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
