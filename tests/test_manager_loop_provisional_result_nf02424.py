"""NF-2026-02424: a claude provisional ``result`` does not end a Manager Chat turn.

The claude CLI reports a ``result`` whose usage is all zero with
``iterations: []`` (an injected task-notification) and then continues the same
turn with more assistant text and a real ``result``. That provisional result
must not arm the exit grace: the child is not killed early, the full text and
the real usage reach the turn, and one turn still has one terminal event
(NF-2026-01232). ``usage: {}`` keeps the existing exit-grace behaviour.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import manager_loop_backends as mlb  # noqa: E402


def _owner() -> ModuleType:
    """The FakeCli fixtures' one owner module, imported rather than copied."""
    for name in ("test_manager_loop_backends", "tests.test_manager_loop_backends"):
        module = sys.modules.get(name)
        if module is not None:
            return module
    path = Path(__file__).with_name("test_manager_loop_backends.py")
    spec = importlib.util.spec_from_file_location("test_manager_loop_backends", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_manager_loop_backends"] = module
    spec.loader.exec_module(module)
    return module


_T = _owner()
FakeCli, backend, drain, kinds = _T.FakeCli, _T.backend, _T.drain, _T.kinds
_TERMINAL_KINDS = {"turn_end", "error", "turn_stopped"}

PROVISIONAL = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "",
    "usage": {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "iterations": [],
    },
}
REAL = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "part two",
    "usage": {
        "input_tokens": 12,
        "output_tokens": 34,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 6,
        "iterations": [{"input_tokens": 12, "output_tokens": 34}],
    },
}


def _text(text: str) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _terminals(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] in _TERMINAL_KINDS]


def test_only_the_exact_zero_usage_iterations_shape_is_provisional():
    assert mlb._provisional_claude_result("claude_cli", PROVISIONAL)
    assert not mlb._provisional_claude_result("claude_cli", {"type": "result", "usage": {}})
    assert not mlb._provisional_claude_result("claude_cli", REAL)
    no_iterations = {k: v for k, v in PROVISIONAL["usage"].items() if k != "iterations"}
    assert not mlb._provisional_claude_result("claude_cli", {"type": "result", "usage": no_iterations})
    some_iterations = {**PROVISIONAL["usage"], "iterations": [{"input_tokens": 0}]}
    assert not mlb._provisional_claude_result("claude_cli", {"type": "result", "usage": some_iterations})
    one_count = {**PROVISIONAL["usage"], "output_tokens": 1}
    assert not mlb._provisional_claude_result("claude_cli", {"type": "result", "usage": one_count})
    missing_count = {k: v for k, v in PROVISIONAL["usage"].items() if k != "cache_read_input_tokens"}
    assert not mlb._provisional_claude_result("claude_cli", {"type": "result", "usage": missing_count})
    assert not mlb._provisional_claude_result("codex_cli", PROVISIONAL)
    assert not mlb._provisional_claude_result("claude_cli", {**PROVISIONAL, "type": "assistant"})


def test_a_provisional_result_then_a_delay_past_the_grace_keeps_the_whole_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(mlb, "TERMINAL_EXIT_GRACE_SECONDS", 0.3)
    fake = FakeCli(tmp_path, [[_text("part one"), PROVISIONAL, "@sleep:1.2", _text("part two"), REAL]])
    cli = backend(fake, timeout_seconds=30.0)
    cli.start("brief")

    events = drain(cli.send("go"))

    assert kinds(events) == ["assistant_text", "assistant_text", "turn_end"]
    assert [e["payload"]["text"] for e in events if e["type"] == "assistant_text"] == [
        "part one", "part two",
    ]
    usage = events[-1]["payload"]["usage"]
    assert (usage["input"], usage["output"], usage["cache_read"], usage["cache_write"]) == (12, 34, 5, 6)
    assert fake.children[-1].poll() == 0, "the child exited on its own, it was not killed early"
    cli.close()


def test_a_provisional_only_turn_reaching_eof_emits_the_provisional_once(tmp_path: Path):
    fake = FakeCli(tmp_path, [[_text("only"), PROVISIONAL]])
    cli = backend(fake)
    cli.start("brief")

    events = drain(cli.send("go"))

    assert kinds(events) == ["assistant_text", "turn_end"]
    assert len(_terminals(events)) == 1
    usage = events[-1]["payload"]["usage"]
    assert (usage["input"], usage["output"]) == (0, 0)
    cli.close()


def test_a_provisional_then_an_error_exit_emits_only_the_error(tmp_path: Path):
    fake = FakeCli(tmp_path, [[_text("partial"), PROVISIONAL]], exit_code=3)
    cli = backend(fake)
    cli.start("brief")

    events = drain(cli.send("go"))

    assert [event["type"] for event in _terminals(events)] == ["error"]
    assert kinds(events) == ["assistant_text", "error"]
    cli.close()


def test_a_provisional_then_silence_expiry_emits_the_provisional_once(tmp_path: Path):
    # As in the NF-2026-02337 timeout test: spawn returns only once "@ready" is
    # flushed, so the 0.5 s window opens with the lines already pending and the
    # hang after the provisional is what expires it (not a slow child start).
    fake = FakeCli(
        tmp_path, [[_text("stalled"), PROVISIONAL, "@ready", "@hang"]], hold_spawn_until_ready=True
    )
    cli = backend(fake, timeout_seconds=0.5)
    cli.start("brief")

    began = time.monotonic()
    events = drain(cli.send("go"))

    assert time.monotonic() - began < 20.0
    assert kinds(events) == ["assistant_text", "turn_end"]
    assert not cli.is_running()
    assert fake.children[-1].poll() is not None, "the stalled child is ended, not leaked"
    cli.close()


def test_a_provisional_then_a_provider_error_emits_only_the_error(tmp_path: Path):
    failed = {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"}
    fake = FakeCli(tmp_path, [[PROVISIONAL, _text("more"), failed]])
    cli = backend(fake)
    cli.start("brief")

    events = drain(cli.send("go"))

    assert [event["type"] for event in _terminals(events)] == ["error"]
    assert events[-1]["payload"]["source"] == "provider"
    cli.close()


def test_an_empty_usage_result_still_ends_at_the_exit_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(mlb, "TERMINAL_EXIT_GRACE_SECONDS", 0.3)
    fake = FakeCli(tmp_path, [[_text("done"), {"type": "result", "usage": {}}, "@hang"]])
    cli = backend(fake, timeout_seconds=60.0)
    cli.start("brief")

    began = time.monotonic()
    events = drain(cli.send("go"))

    assert time.monotonic() - began < 20.0
    assert kinds(events) == ["assistant_text", "turn_end"]
    assert fake.children[-1].poll() is not None
    cli.close()
