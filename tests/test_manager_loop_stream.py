from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub.manager_loop_backends import (  # noqa: E402
    apply_manager_stream_tokens,
    translate,
)


def test_opencode_part_update_becomes_visible_thinking_and_tool() -> None:
    thinking = translate(
        "opencode_cli",
        {
            "type": "message.part.updated",
            "part": {"type": "reasoning", "text": "checking the route"},
        },
    )
    tool = translate(
        "opencode_cli",
        {
            "type": "message.part.updated",
            "part": {"type": "tool", "tool": "read", "state": {"status": "running", "input": {"path": "a.py"}}},
        },
    )
    assert thinking == [{"type": "reasoning", "payload": {"text": "checking the route"}}]
    assert tool[0]["type"] == "tool_call"
    assert tool[0]["payload"]["name"] == "read"


def test_reasoning_depth_is_only_a_documented_flag() -> None:
    assert apply_manager_stream_tokens("opencode_cli", ["opencode", "run", "hello"], "high") == [
        "opencode",
        "run",
        "--thinking",
        "hello",
    ]
    assert "--effort" in apply_manager_stream_tokens("claude_cli", ["claude", "-p"], "max")
    assert "max" in apply_manager_stream_tokens("claude_cli", ["claude", "-p"], "max")
    assert apply_manager_stream_tokens("claude_cli", ["claude", "-p"], "nope") == [
        "claude",
        "-p",
        "--include-partial-messages",
        # Without an explicit display claude-opus-5 streams thinking blocks with
        # empty text in -p stream-json mode (measured on CLI 2.1.280: 6 blocks,
        # 0 chars; with summarized: 5 blocks, 5995 chars).
        "--thinking-display",
        "summarized",
    ]
    codex = apply_manager_stream_tokens("codex_cli", ["codex", "exec", "-"], "high")
    assert codex[-3] == "-c"
    assert codex[-2] == 'model_reasoning_effort="high"'
    assert codex[-1] == "-"


def test_claude_stream_deltas_become_transient_delta_items() -> None:
    text = {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}}}
    think = {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hm"}}}
    other = {"type": "stream_event", "event": {"type": "message_start"}}
    assert translate("claude_cli", text) == [{"type": "delta", "payload": {"kind": "text", "text": "Hel"}}]
    assert translate("claude_cli", think) == [{"type": "delta", "payload": {"kind": "reasoning", "text": "hm"}}]
    assert translate("claude_cli", other) == []
