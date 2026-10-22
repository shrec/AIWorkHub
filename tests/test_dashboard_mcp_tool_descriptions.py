"""TDQS docstring contract tests for dashboard_mcp_app MCP tools."""

from __future__ import annotations

import inspect
from typing import Any

from aiworkhub import dashboard_mcp_app
from aiworkhub.mcp_tool_metadata import derive_annotations, split_docstring

VALID_PREFIXES = ("READ-ONLY:", "USER WRITE:", "DESTRUCTIVE:")
MAX_DOCSTRING_WORDS = 160


class _RecordingMcp:
    """Minimal FastMCP-compatible recorder capturing registered (name, fn) pairs."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str):
        def _decorator(fn: Any) -> Any:
            assert name not in self.tools, f"duplicate tool registration: {name}"
            self.tools[name] = fn
            return fn

        return _decorator


def _published_dashboard_tools() -> dict[str, Any]:
    recorder = _RecordingMcp()
    returned_names = dashboard_mcp_app.register(recorder)
    assert returned_names, "register() returned no tool names"
    assert set(returned_names) == set(recorder.tools), (
        "register() return tuple does not match registered tool names"
    )
    assert len(returned_names) == len(recorder.tools)
    return recorder.tools


def test_every_published_dashboard_tool_has_tdqs_complete_docstring() -> None:
    tools = _published_dashboard_tools()
    assert len(tools) >= 44

    for name, fn in tools.items():
        doc = inspect.getdoc(fn)
        assert doc, f"{name} ({fn.__name__}) is missing a docstring"
        assert doc.isascii(), f"{name} docstring must be plain ASCII"

        words = doc.split()
        assert len(words) <= MAX_DOCSTRING_WORDS, (
            f"{name} docstring has {len(words)} words (max {MAX_DOCSTRING_WORDS})"
        )

        lines = doc.splitlines()
        first_line = lines[0].strip()
        assert first_line.startswith(VALID_PREFIXES), (
            f"{name} first line must start with one of {VALID_PREFIXES}, got: {first_line!r}"
        )
        for prefix in VALID_PREFIXES:
            if first_line.startswith(prefix):
                purpose = first_line[len(prefix) :].strip()
                assert purpose, f"{name} is missing a purpose sentence after {prefix}"
                break

        if "_purge" in name:
            assert first_line.startswith("DESTRUCTIVE:"), (
                f"purge tool {name} must start with 'DESTRUCTIVE:', got: {first_line!r}"
            )
            annotations = derive_annotations(name, doc)
            assert annotations == {"readOnlyHint": False, "destructiveHint": True}
        elif first_line.startswith("READ-ONLY:"):
            assert derive_annotations(name, doc) == {"readOnlyHint": True}

        cleaned, parsed_args = split_docstring(doc)
        assert "Args:" not in cleaned, f"{name} cleaned description still contains Args:"
        body_lines = [
            line.strip()
            for line in cleaned.splitlines()[1:]
            if line.strip()
        ]
        assert body_lines, (
            f"{name} must include a usage/behavior paragraph after the first line"
        )

        sig_params = list(inspect.signature(fn).parameters)
        if sig_params:
            assert any(line.strip() == "Args:" for line in lines), (
                f"{name} takes parameters {sig_params} but has no Args: section"
            )
            assert set(parsed_args) == set(sig_params), (
                f"{name} Args: keys {sorted(parsed_args)} do not match "
                f"signature parameters {sig_params}"
            )
            for param_name in sig_params:
                param_desc = parsed_args[param_name].strip()
                assert param_desc, (
                    f"{name} parameter {param_name!r} has an empty Args description"
                )
        else:
            assert not any(line.strip() == "Args:" for line in lines), (
                f"{name} takes no parameters and must omit Args:"
            )
            assert parsed_args == {}
