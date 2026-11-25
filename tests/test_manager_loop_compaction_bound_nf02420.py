"""NF-2026-02420: Manager Chat rotates before the provider's observed auto-compaction point.

The Claude CLI reports ``context_window`` 1000000 but compacts on its own near 188K tokens,
so ``context_fill`` never reaches ``rotate_fraction``. Once a ``provider_compacted`` event
names that point (``pre_tokens``), the loop treats it as the effective window and rotates
proactively, labelled ``provider_compaction_bound``.
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from aiworkhub import manager_loop as ml

from test_manager_loop import Harness, _used

PRE_TOKENS = 188_000
WINDOW = 1_000_000


def _compacted(pre_tokens: Any = PRE_TOKENS) -> dict[str, Any]:
    return {"type": "provider_compacted", "payload": {"trigger": "auto", "pre_tokens": pre_tokens}}


def _at(tokens: int) -> list[dict[str, Any]]:
    """A replying turn whose measured context is exactly ``tokens`` of the 1M window."""
    return _used(input=1, output=1, context_window=WINDOW, context_fill=tokens / WINDOW)


def _threshold(harness: Harness) -> int:
    return math.ceil(harness.orch.rotate_fraction * PRE_TOKENS)


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    made: list[Harness] = []

    def build(**options: Any) -> Harness:
        made.append(Harness(tmp_path, **options))
        return made[-1]

    yield build
    for harness in made:
        harness.orch.close()


def _compact_and_rotate(harness: Harness) -> ml.ManagerSession:
    """Run one completed turn on which the provider compacted; return the successor."""
    first = harness.orch.start("fake", "model-a")
    harness.backends[0].script.append(
        [_compacted(), *_used(input=1, output=1, context_window=WINDOW, context_fill=0.15)]
    )
    result = harness.orch.send("hello")
    assert result["rotation_basis"] == "provider_compaction"
    rotation = result["rotation"]
    assert rotation is not None and rotation["session"].session_id == first.session_id
    successor = harness.orch.session
    assert successor is not None and successor.session_id != first.session_id
    return successor


def test_the_successor_carries_the_bound_and_rotates_before_the_provider_compacts(make) -> None:
    harness = make()
    successor = _compact_and_rotate(harness)
    threshold = _threshold(harness)

    assert successor.provider_compact_tokens == PRE_TOKENS
    saved = {item.session_id: item for item in harness.store.sessions()}
    assert saved[successor.session_id].provider_compact_tokens == PRE_TOKENS

    backend = harness.backends[1]
    backend.script.extend([_at(90_000), _at(threshold - 1_000), _at(threshold)])
    start = harness.orch.send("one")
    below = harness.orch.send("two")
    crossed = harness.orch.send("three")

    # The provider still reports a window five times larger than where it compacts.
    assert start["context_fill"] < harness.orch.rotate_fraction
    assert crossed["context_fill"] < harness.orch.rotate_fraction
    assert (start["rotation"], below["rotation"]) == (None, None)
    assert below["rotation_basis"] == "provider_context_fill"
    assert crossed["context_tokens"] == threshold
    assert crossed["rotation_basis"] == "provider_compaction_bound"
    rotation = crossed["rotation"]
    assert rotation is not None and rotation["session"].session_id == successor.session_id
    assert "context_threshold" in backend.messages[-1]
    requests = [e for e in harness.store.events(successor.session_id) if e["type"] == "handoff_request"]
    assert [e["payload"]["reason"] for e in requests] == ["context_threshold"]
    # The next successor keeps the bound too.
    assert harness.orch.session.provider_compact_tokens == PRE_TOKENS


def test_a_new_loop_seeds_the_bound_from_a_compaction_already_in_the_store(make) -> None:
    earlier = make(prefix="a")
    _compact_and_rotate(earlier)
    earlier.orch.close()
    # Records written before this field existed carry no bound; only the event log does.
    for item in earlier.store.sessions():
        earlier.store.save(
            dataclasses.replace(item, provider_compact_tokens=None, provider_compact_window=None)
        )
    assert earlier.store.latest_compaction_pre_tokens(WINDOW) == PRE_TOKENS
    # A bound belongs to the window it was observed under; another window gets none.
    assert earlier.store.latest_compaction_pre_tokens(400_000) is None
    assert earlier.store.latest_compaction_pre_tokens(None) is None

    harness = make(prefix="b")
    session = harness.orch.start("fake", "model-a")

    # The chain opens without scanning the store; its first turn seeds the bound.
    assert session.provider_compact_tokens is None
    harness.backends[0].script.append(_at(_threshold(harness)))
    result = harness.orch.send("hello")
    assert result["rotation_basis"] == "provider_compaction_bound"
    assert result["rotation"] is not None
    assert result["rotation"]["session"].session_id == session.session_id


def test_a_session_without_a_bound_seeds_it_once_on_its_first_turn(make) -> None:
    earlier = make(prefix="a")
    _compact_and_rotate(earlier)
    active = earlier.orch.session
    assert active is not None
    # The live record lost its bound (as a pre-upgrade record would have none).
    earlier.orch._session = dataclasses.replace(
        active, provider_compact_tokens=None, provider_compact_window=None
    )
    # The seed is keyed by the window this turn reports.
    earlier.backends[1].script.append(_at(90_000))

    result = earlier.orch.send("hello")

    assert result["rotation"] is None
    assert earlier.orch.session.provider_compact_tokens == PRE_TOKENS
    assert earlier.orch.session.provider_compact_window == WINDOW


def test_without_any_compaction_the_rotation_decisions_are_unchanged(make) -> None:
    harness = make()
    session = harness.orch.start("fake", "model-a")
    assert session.provider_compact_tokens is None
    harness.backends[0].script.extend([_at(90_000), _at(160_000), _at(185_000)])

    results = [harness.orch.send(text) for text in ("one", "two", "three")]

    assert [r["rotation"] for r in results] == [None, None, None]
    assert {r["rotation_basis"] for r in results} == {"provider_context_fill"}
    assert harness.orch.session.session_id == session.session_id
    assert harness.orch.session.provider_compact_tokens is None
    assert harness.store.latest_compaction_pre_tokens(WINDOW) is None


def test_the_growth_ceiling_still_fires_without_a_compaction_bound(make) -> None:
    harness = make()
    session = harness.orch.start("fake", "model-a")
    harness.backends[0].script.extend([_at(90_000), _at(190_000)])

    harness.orch.send("one")
    result = harness.orch.send("two")

    assert result["rotation_basis"] == "absolute_token_ceiling"
    assert result["rotation"]["session"].session_id == session.session_id


@pytest.mark.parametrize("pre_tokens", [0, -5, True, "188000", 1.5, None])
def test_an_invalid_pre_tokens_never_becomes_a_bound(make, pre_tokens: Any) -> None:
    harness = make()
    harness.orch.start("fake", "model-a")
    harness.backends[0].script.append(
        [_compacted(pre_tokens), *_used(input=1, output=1, context_window=WINDOW, context_fill=0.15)]
    )

    result = harness.orch.send("hello")

    # The compaction itself still rotates (NF-2026-02329); it just names no bound.
    assert result["rotation_basis"] == "provider_compaction"
    assert harness.orch.session.provider_compact_tokens is None


def test_a_failed_turn_past_the_bound_does_not_rotate(make) -> None:
    harness = make()
    successor = _compact_and_rotate(harness)
    usage = {"input": 1, "output": 1, "context_window": WINDOW, "context_fill": _threshold(harness) / WINDOW}
    harness.backends[1].script.append(
        [{"type": "turn_end", "payload": {"usage": usage}}, RuntimeError("usage limit reached")]
    )

    failed = harness.orch.send("work")

    assert failed["ok"] is False and not failed["reply"]
    assert failed["rotation"] is None
    assert harness.orch.session.session_id == successor.session_id
    assert harness.orch.session.status == "active"


def test_the_bound_round_trips_through_the_session_record() -> None:
    session = ml.ManagerSession(
        session_id="a-session-0001", repo_id="r", backend_id="b", model="m",
        status="active", created_at="t", provider_compact_tokens=PRE_TOKENS,
        provider_compact_window=WINDOW,
    )
    assert ml.ManagerSession.from_json(session.to_json()) == session
    legacy = {
        k: v for k, v in session.to_json().items()
        if k not in ("provider_compact_tokens", "provider_compact_window")
    }
    restored = ml.ManagerSession.from_json(legacy)
    assert (restored.provider_compact_tokens, restored.provider_compact_window) == (None, None)


def test_the_bound_applies_only_under_the_window_it_was_observed_with(make) -> None:
    harness = make()
    successor = _compact_and_rotate(harness)
    assert (successor.provider_compact_tokens, successor.provider_compact_window) == (PRE_TOKENS, WINDOW)
    tokens = math.ceil(0.9 * PRE_TOKENS)
    assert tokens >= _threshold(harness)
    other = 400_000
    backend = harness.backends[1]
    backend.script.extend([
        _used(input=1, output=1, context_window=other, context_fill=tokens / other),
        _at(tokens),
    ])

    elsewhere = harness.orch.send("one")

    # Another provider's window: its compaction point says nothing about this one.
    assert elsewhere["context_tokens"] == tokens
    assert elsewhere["context_fill"] < harness.orch.rotate_fraction
    assert elsewhere["rotation"] is None
    assert elsewhere["rotation_basis"] == "provider_context_fill"
    assert harness.orch.session.session_id == successor.session_id
    # The pair survives the foreign-window turn unchanged.
    assert harness.orch.session.provider_compact_tokens == PRE_TOKENS
    assert harness.orch.session.provider_compact_window == WINDOW

    same = harness.orch.send("two")

    assert same["context_tokens"] == tokens
    assert same["rotation_basis"] == "provider_compaction_bound"
    assert same["rotation"] is not None
    assert same["rotation"]["session"].session_id == successor.session_id


def test_a_new_chain_scans_the_store_once_when_it_holds_no_compaction(make, monkeypatch) -> None:
    harness = make()
    calls: list[int | None] = []
    original = harness.store.latest_compaction_pre_tokens

    def counted(window: int | None) -> int | None:
        calls.append(window)
        return original(window)

    monkeypatch.setattr(harness.store, "latest_compaction_pre_tokens", counted)
    session = harness.orch.start("fake", "model-a")
    assert calls == []
    harness.backends[0].script.extend([_at(90_000), _at(100_000), _at(110_000)])

    results = [harness.orch.send(text) for text in ("one", "two", "three")]

    assert [r["rotation"] for r in results] == [None, None, None]
    assert harness.orch.session.session_id == session.session_id
    assert harness.orch.session.provider_compact_tokens is None
    assert calls == [WINDOW]


def test_a_first_turn_without_a_window_leaves_the_seed_for_the_next_turn(make) -> None:
    earlier = make(prefix="a")
    _compact_and_rotate(earlier)
    earlier.orch.close()
    for item in earlier.store.sessions():
        earlier.store.save(
            dataclasses.replace(item, provider_compact_tokens=None, provider_compact_window=None)
        )
    harness = make(prefix="b")
    session = harness.orch.start("fake", "model-a")
    harness.backends[0].script.extend([_used(input=1, output=1), _at(_threshold(harness))])

    windowless = harness.orch.send("one")
    crossed = harness.orch.send("two")

    assert windowless["rotation"] is None
    assert crossed["rotation_basis"] == "provider_compaction_bound"
    assert crossed["rotation"]["session"].session_id == session.session_id
