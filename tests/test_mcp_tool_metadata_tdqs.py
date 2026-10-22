"""TDQS metadata publishing: mcp_tool_metadata + _nonblocking_mcp_tool.

Covers (a) split_docstring, (b) a fresh FastMCP publishing per-parameter
descriptions, Args-free descriptions and derived ToolAnnotations through the
same wrapper shape as server._nonblocking_mcp_tool, (c) an explicit Field
description beating Args text, and (d) the live server.mcp contract
(non-empty, Args-free descriptions and aiworkhub_task_create's inputSchema
matching a direct SDK registration).
"""

import asyncio
import typing
from functools import wraps

import pytest

from aiworkhub.mcp_tool_metadata import (
    derive_annotations,
    prepare_registration,
    split_docstring,
)


def _register_via_wrapper(fresh, fn):
    """Replicate server._nonblocking_mcp_tool.decorate for a standalone FastMCP."""
    args, kwargs, signature = prepare_registration(fn, (), {})
    register = fresh.tool(*args, **kwargs)

    @wraps(fn)
    async def adapter(*a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    adapter.__signature__ = signature
    register(adapter)
    return fn


# --- (a) split_docstring ----------------------------------------------------


def test_split_docstring_args_and_returns():
    doc = """\
Do a thing.

Args:
    name (int): the name of the thing
        with a continuation line
    other: another parameter

Returns:
    the result
"""
    text, params = split_docstring(doc)
    assert params == {
        "name": "the name of the thing with a continuation line",
        "other": "another parameter",
    }
    assert "Args:" not in text
    assert "Returns:" in text
    assert "the result" in text


def test_split_docstring_missing_and_malformed():
    assert split_docstring("Just a summary.") == ("Just a summary.", {})
    text, params = split_docstring("Summary.\n\nArgs:\n    not an entry\n    nope\n")
    assert params == {}
    assert "Args:" not in text
    assert split_docstring(None) == ("", {})


def test_derive_annotations():
    assert derive_annotations("read_tool", "READ-ONLY: foo") == {"readOnlyHint": True}
    assert derive_annotations("x", "MANAGER READ: foo") == {"readOnlyHint": True}
    assert derive_annotations("x", "READ: foo") == {"readOnlyHint": True}
    assert derive_annotations("do_purge", "Delete.") == {
        "readOnlyHint": False,
        "destructiveHint": True,
    }
    assert derive_annotations("x", "DESTRUCTIVE: foo") == {
        "readOnlyHint": False,
        "destructiveHint": True,
    }
    assert derive_annotations("plain", "Hello.") is None


# --- (b) fresh FastMCP publishes descriptions and annotations ----------------


def test_fresh_fastmcp_publishes_metadata():
    pytest.importorskip("mcp")
    from mcp.server.fastmcp import FastMCP

    def read_tool(query: str, limit: int = 10) -> str:
        """READ-ONLY: search the index.

        Args:
            query: the search query text
            limit (int): maximum results, with a
                wrapped continuation
        """
        return "ok"

    def do_purge(target: str) -> str:
        """Delete a record.

        Args:
            target: what to delete
        """
        return "ok"

    fresh = FastMCP("t")
    for fn in (read_tool, do_purge):
        _register_via_wrapper(fresh, fn)

    tools = {t.name: t for t in asyncio.run(fresh.list_tools())}

    read = tools["read_tool"]
    assert read.inputSchema["properties"]["query"]["description"] == "the search query text"
    assert (
        read.inputSchema["properties"]["limit"]["description"]
        == "maximum results, with a wrapped continuation"
    )
    assert "Args:" not in read.description
    assert "search the index" in read.description
    assert read.annotations.readOnlyHint is True

    purge = tools["do_purge"]
    assert purge.inputSchema["properties"]["target"]["description"] == "what to delete"
    assert purge.annotations.readOnlyHint is False
    assert purge.annotations.destructiveHint is True


# --- (c) explicit Field description beats Args text --------------------------


def test_explicit_field_description_beats_args():
    pytest.importorskip("mcp")
    from typing import Annotated

    from mcp.server.fastmcp import FastMCP
    from pydantic import Field

    def explicit_tool(
        name: Annotated[str, Field(description="explicit description")],
        limit: int = 5,
    ) -> str:
        """Do a thing.

        Args:
            name: args-level description
            limit: limit description
        """
        return "ok"

    fresh = FastMCP("t")
    _register_via_wrapper(fresh, explicit_tool)
    tools = {t.name: t for t in asyncio.run(fresh.list_tools())}
    tool = tools["explicit_tool"]
    assert tool.inputSchema["properties"]["name"]["description"] == "explicit description"
    assert tool.inputSchema["properties"]["limit"]["description"] == "limit description"


# --- (d) live server.mcp contract --------------------------------------------


_DOCSTRING_PENDING: frozenset[str] = frozenset()  # shrink only; every published tool documents itself


def test_server_tools_publish_args_free_descriptions_and_match_sdk():
    pytest.importorskip("mcp")
    from aiworkhub import server

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert tools, "no tools registered"

    assert {
        name for name, tool in tools.items() if not (tool.description or "").strip()
    } <= _DOCSTRING_PENDING

    for name, tool in tools.items():
        for line in tool.description.splitlines():
            assert line.strip() != "Args:", f"{name}: description keeps an Args: line"

    from mcp.server.fastmcp import FastMCP

    direct = FastMCP("direct")
    direct.tool()(server.aiworkhub_task_create)
    direct_tools = {t.name: t for t in asyncio.run(direct.list_tools())}
    assert (
        tools["aiworkhub_task_create"].inputSchema
        == direct_tools["aiworkhub_task_create"].inputSchema
    )


def test_string_annotation_registers_without_forward_ref_error():
    pytest.importorskip("mcp")
    from mcp.server.fastmcp import FastMCP

    def string_annotated(mode: 'typing.Literal["a", "b"]') -> str:
        """Pick a mode."""
        return mode

    fresh = FastMCP("t")
    _register_via_wrapper(fresh, string_annotated)
    tools = {t.name: t for t in asyncio.run(fresh.list_tools())}
    assert "string_annotated" in tools


def test_string_annotation_with_args_publishes_description():
    pytest.importorskip("mcp")
    from mcp.server.fastmcp import FastMCP

    def documented_string_annotated(mode: 'typing.Literal["a", "b"]') -> str:
        """Pick a mode.

        Args:
            mode: which mode to use
        """
        return mode

    fresh = FastMCP("t")
    _register_via_wrapper(fresh, documented_string_annotated)
    tools = {t.name: t for t in asyncio.run(fresh.list_tools())}
    tool = tools["documented_string_annotated"]
    assert tool.inputSchema["properties"]["mode"]["description"] == "which mode to use"
