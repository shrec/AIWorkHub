"""NF-2026-02418: continuing an already-continued card numbers on the chain root."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import capped_continuation  # noqa: E402


@pytest.fixture
def existing(monkeypatch):
    ids: set[str] = set()
    monkeypatch.setattr(capped_continuation, "_task_exists", lambda task_id: task_id in ids)
    return ids


def test_continuing_a_continuation_takes_the_next_number_on_the_root(existing):
    existing.update({"X", "X-c1"})
    assert capped_continuation.successor_task_id("X-c1") == "X-c2"


def test_continuing_a_continuation_skips_existing_numbers(existing):
    existing.update({"X", "X-c1", "X-c2"})
    assert capped_continuation.successor_task_id("X-c1") == "X-c3"


def test_root_id_keeps_the_smallest_free_suffix(existing):
    assert capped_continuation.successor_task_id("X") == "X-c1"
    existing.update({"X", "X-c1"})
    assert capped_continuation.successor_task_id("X") == "X-c2"


def test_explicit_by_is_returned_unchanged(existing):
    assert capped_continuation.successor_task_id("X-c1", "custom-id") == "custom-id"
    assert capped_continuation.successor_task_id("X-c1", "  spaced  ") == "spaced"


def test_never_chains_the_suffix(existing):
    existing.update({"X", "X-c1"})
    assert capped_continuation.successor_task_id("X-c1") != "X-c1-c1"
