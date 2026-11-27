"""NF-2026-00979: a successor rotation runs one automatic continue turn; callbacks reach the successor.

The harness, the scripted backend and the compaction event are the owner's
(``test_manager_loop``); no provider, task store or MCP route is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from test_manager_loop import _COMPACTED, FakeBackend, Harness


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    made: list[Harness] = []

    def build(**options: Any) -> Harness:
        made.append(Harness(tmp_path, **options))
        return made[-1]

    yield build
    for harness in made:
        harness.orch.close()


def _script_compacting_turn(harness: Harness, handoff_step: Any) -> None:
    """The first turn compacts (rotates into a successor); the next provider call is the handoff."""
    harness.backends[0].script.append([
        _COMPACTED, {"type": "assistant_text", "payload": {"text": "done"}}, {"type": "turn_end", "payload": {}},
    ])
    harness.backends[0].script.append(handoff_step)


def test_a_successor_rotation_runs_one_continue_turn_worded_from_the_handoff(make) -> None:
    harness = make()
    harness.orch.start("fake", "model-a")
    _script_compacting_turn(harness, [
        {"type": "assistant_text", "payload": {"text": "HANDOFF-KEEP"}}, {"type": "turn_end", "payload": {}},
    ])

    result = harness.orch.send("hello")

    rotation = result["rotation"]
    successor = rotation["successor"]
    assert rotation["continue"]["ok"] is True
    assert successor.turn_count == 1
    assert harness.orch.session == successor
    (sent,) = harness.backends[1].messages
    assert "provider_compaction" in sent and "HANDOFF-KEEP" in sent
    continues = [e for e in harness.store.events(successor.session_id) if e["type"] == "continue"]
    assert len(continues) == 1
    assert "HANDOFF-KEEP" in continues[0]["payload"]["text"]


def test_a_mechanical_rotation_still_runs_the_continue_turn_worded_from_the_mechanical_handoff(make) -> None:
    harness = make()
    harness.orch.start("fake", "model-a")
    _script_compacting_turn(harness, RuntimeError("provider down"))

    rotation = harness.orch.send("hello")["rotation"]

    assert rotation["mechanical"] is True
    assert rotation["continue"]["ok"] is True
    (sent,) = harness.backends[1].messages
    assert "mechanical handoff" in sent and "Mechanical handoff for" in sent
    assert rotation["successor"].turn_count == 1


def test_a_callback_after_rotation_is_a_callback_turn_on_the_successor(make) -> None:
    harness = make()
    first = harness.orch.start("fake", "model-a")
    _script_compacting_turn(harness, [
        {"type": "assistant_text", "payload": {"text": "HANDOFF-KEEP"}}, {"type": "turn_end", "payload": {}},
    ])
    harness.orch.send("hello")
    successor = harness.orch.session
    assert successor is not None and successor.session_id != first.session_id

    result = harness.orch.wake({"task_id": "CARD-1", "state": "done"})

    assert result["ok"] is True
    assert result["session_id"] == successor.session_id
    callbacks = [e for e in harness.store.events(successor.session_id) if e["type"] == "callback"]
    assert len(callbacks) == 1
    assert callbacks[0]["payload"]["task_id"] == "CARD-1"
    assert harness.backends[1].messages[-1] == "callback: CARD-1 -> done"
    assert [e for e in harness.store.events(first.session_id) if e["type"] == "callback"] == []


def _compacting_step() -> list[Any]:
    return [_COMPACTED, {"type": "assistant_text", "payload": {"text": "done"}}, {"type": "turn_end", "payload": {}}]


def _handoff_step(text: str) -> list[Any]:
    return [{"type": "assistant_text", "payload": {"text": text}}, {"type": "turn_end", "payload": {}}]


class _ScriptedHarness(Harness):
    """Each backend the factory opens consumes the next scripted plan (its steps, in order)."""

    def __init__(self, root: Path, plans: list[list[Any]]) -> None:
        super().__init__(root)
        self.plans = list(plans)

    def factory(self, backend_id: str, model: str) -> FakeBackend:
        backend = super().factory(backend_id, model)
        if self.plans:
            backend.script.extend(self.plans.pop(0))
        return backend


def test_a_continue_turn_that_compacts_rotates_once_more_without_a_third_continue(tmp_path: Path) -> None:
    # start opens backend 0: its compacted turn and its handoff. Backend 1 is the successor: its
    # continue turn compacts too, so its handoff rotates into backend 2, which gets no continue.
    harness = _ScriptedHarness(tmp_path, [
        [_compacting_step(), _handoff_step("HANDOFF-ONE")],
        [_compacting_step(), _handoff_step("HANDOFF-TWO")],
        [],
    ])
    try:
        harness.orch.start("fake", "model-a")

        result = harness.orch.send("hello")

        outer = result["rotation"]
        nested = outer["continue"]["rotation"]
        assert nested is not None and nested["successor"] is not None
        assert nested["continue"] == {"ok": False, "skipped": "nested_rotation"}
        # The outer rotation names the successor it opened, which the nested rotation then closed.
        assert outer["successor"].session_id == nested["session"].session_id
        assert outer["successor"].session_id != nested["successor"].session_id
        assert len(harness.backends) == 3, "one nested rotation: a third successor, no third continue"
        assert harness.backends[2].messages == []
        assert harness.orch.session == nested["successor"]
    finally:
        harness.orch.close()
