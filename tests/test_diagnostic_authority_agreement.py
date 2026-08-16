"""Authority agreement: the read-only collision preflight and the canonical
collision guard must never disagree.

For the SAME canonical task-store state the preflight reports the SAME
active-card count as ``core.collision_guard``; and when the store is
unavailable BOTH decline rather than one of them inventing a confident zero.
This is the contract that stops a preflight from approving overlapping write
scopes that the canonical guard would block (0 active cards vs 14).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import cli_adapter_readonly_tool as tool  # noqa: E402
from aiworkhub import core  # noqa: E402


@pytest.fixture(autouse=True)
def _default_canonical_source(monkeypatch) -> None:
    # Exercise the DEFAULT canonical-store resolution, not a JSONL override.
    monkeypatch.delenv("AIWORKHUB_COLLISION_CARDS_PATH", raising=False)
    monkeypatch.delenv("BITNN_TASK_CARDS_PATH", raising=False)


def test_preflight_agrees_with_canonical_collision_guard() -> None:
    preflight = tool.collision_preflight(
        task_id="__authority_agreement_probe__", runner="__no_such_runner__", topic="x"
    )
    guard = core.collision_guard(print_json=False)

    if not preflight["cards_source_resolved"]:
        # Store unavailable: NEITHER invents a confident zero. The preflight is
        # UNRESOLVED (None, not 0) and the guard reports not-ok.
        assert preflight["active_card_count"] is None
        assert preflight["would_collide"] is None
        assert guard["ok"] is False
        return

    # Store ready: the preflight must resolve from the canonical store and its
    # active-card count must equal the guard's own active-card scan.
    assert preflight["cards_source"] == "canonical_task_store"
    guard_active = len(core._active_cards_for_collision_guard())
    assert preflight["active_card_count"] == guard_active
    # "Resolved" means the guard could READ its store, i.e. it did NOT hit the
    # store-unavailable path (the only branch that populates stderr). guard["ok"]
    # is the wrong signal: core.collision_guard returns ok=False whenever active
    # cards have overlapping allowed_writes, which is orthogonal to whether the
    # source resolved -- the old assertion passed only because this fixture has
    # no overlap.
    assert not guard["stderr"]


def test_unresolved_reason_never_leaks_a_filesystem_path() -> None:
    """On the canonical branch the honest 'why' must carry no absolute path,
    even though the underlying guard error string does."""
    preflight = tool.collision_preflight(task_id="T1", runner="r", topic="x")
    if preflight["cards_source_resolved"]:
        pytest.skip("canonical task store is provisioned in this environment")
    reason = preflight["unresolved_reason"]
    assert reason
    assert "/" not in reason and "\\" not in reason
