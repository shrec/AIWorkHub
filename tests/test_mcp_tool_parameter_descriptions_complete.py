"""Glama TDQS: every parameter of every published MCP tool carries a description.

A parameter reaches ``tools/list`` with a description only through an ``Args:``
entry in the tool docstring (``mcp_tool_metadata.split_docstring``) or an
explicit ``Field``/hint description, so this fails the moment a tool, or a new
parameter on an existing tool, ships undocumented.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

_SRC = Path(__file__).resolve().parents[1] / "src"
_PAYLOAD_PREFIX = "TDQS_INPUT_SCHEMAS="

# Some tools register only when the write gate is open at import time, so the child opens it.
_CHILD = """
try:
    import sitecustomize  # noqa: F401 - the AppContainer lane shim must still load
except ImportError:
    pass
import asyncio
import json
from aiworkhub import server

tools = asyncio.run(server.mcp.list_tools())
print(PREFIX + json.dumps({tool.name: [tool.description, tool.inputSchema] for tool in tools}))
"""


def _undescribed(schemas: dict[str, dict]) -> list[str]:
    return sorted(
        f"{name}.{param}"
        for name, schema in schemas.items()
        for param, spec in (schema.get("properties") or {}).items()
        if not str((spec or {}).get("description") or "").strip()
    )


def _report(undescribed: list[str]) -> str:
    return f"{len(undescribed)} parameters publish no description:\n" + "\n".join(undescribed)


def _publish_defects(listing: dict[str, list]) -> list[str]:
    """Descriptions ending in whitespace and properties publishing ``examples == []``."""
    defects = [
        f"{name}: description ends in whitespace"
        for name, (description, _) in listing.items()
        if (description or "") != (description or "").rstrip()
    ]
    defects += [
        f"{name}.{param}: examples == []"
        for name, (_, schema) in listing.items()
        for param, spec in ((schema or {}).get("properties") or {}).items()
        if isinstance(spec, dict) and spec.get("examples") == []
    ]
    return sorted(defects)


def test_every_published_parameter_has_a_description() -> None:
    from aiworkhub import server

    tools = asyncio.run(server.mcp.list_tools())
    assert tools, "no tools registered"
    undescribed = _undescribed({tool.name: tool.inputSchema for tool in tools})
    assert not undescribed, _report(undescribed)


def test_every_write_gated_parameter_has_a_description() -> None:
    from aiworkhub import server

    registered = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    env = dict(
        os.environ,
        AIWORKHUB_ALLOW_WRITES="1",
        PYTHONPATH=os.pathsep.join([str(_SRC), *inherited]),
    )
    completed = subprocess.run(
        [sys.executable, "-c", f"PREFIX = {_PAYLOAD_PREFIX!r}\n{_CHILD}"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    lines = [line for line in completed.stdout.splitlines() if line.startswith(_PAYLOAD_PREFIX)]
    assert len(lines) == 1, completed.stdout[-2000:]
    payload = json.loads(lines[0][len(_PAYLOAD_PREFIX):])
    assert registered <= set(payload)
    undescribed = _undescribed({name: schema for name, (_, schema) in payload.items()})
    assert not undescribed, _report(undescribed)
    defects = _publish_defects(payload)
    assert not defects, "\n".join(defects)


def test_published_tools_have_no_trailing_whitespace_or_empty_examples() -> None:
    from aiworkhub import server

    tools = asyncio.run(server.mcp.list_tools())
    assert tools, "no tools registered"
    defects = _publish_defects({tool.name: [tool.description, tool.inputSchema] for tool in tools})
    assert not defects, "\n".join(defects)
