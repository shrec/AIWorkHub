"""NF-2026-00335 / AWH-OBS-012b regression: claim truth for the launch preflight.

TWO SURFACES, ONE STATE, OPPOSITE ANSWERS. The read-only launch preflight
(``cli_adapter_readonly_tool.collision_preflight``) counted every card in an
active STATUS bucket (pending/processing/review) as a held claim, so twelve
stale PENDING reviewer orphans made it report ``would_collide=True`` for EVERY
launch on the primary runner -- while the live write-scope guard
(``core._scan_aiworkhub_collisions``) reported the same queue collision-free.

A pending card holds nothing: no claim, no lock, no worker, no worktree. A claim
is HELD only when the store committed one (``claimed_by`` set by the exact-claim
path). These tests drive BOTH surfaces over ONE identical card set and prove:

  * the preflight counts only claims actually held, so a brand-new task_id whose
    runner holds no claim does not collide, and the two surfaces no longer
    contradict each other about what is held;
  * the returned value NAMES its runner-identity condition (``condition_kind`` /
    ``runner_busy``) distinctly from a write-scope/file collision;
  * the fail-closed UNRESOLVED semantics are unchanged (an unreadable configured
    source and an unavailable canonical store never degrade to a confident zero);
  * the live write-scope overlap detection is NOT weakened.

A test that exercises only the preflight cannot fail on this defect -- that shape
is why it shipped -- so the core contradiction tests drive both surfaces.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import cli_adapter_readonly_tool as tool  # noqa: E402
from aiworkhub import core  # noqa: E402

_CARDS_ENV = "AIWORKHUB_COLLISION_CARDS_PATH"
_LEGACY_ENV = "BITNN_TASK_CARDS_PATH"
_RUNNER = "claude_opus-5"


@pytest.fixture(autouse=True)
def _clear_card_env(monkeypatch) -> None:
    monkeypatch.delenv(_CARDS_ENV, raising=False)
    monkeypatch.delenv(_LEGACY_ENV, raising=False)


def _pending(task_id: str, writes: list[str]) -> dict:
    """A created-but-unlaunched card: pending + unclaimed, NO ``claimed_by``.

    Per the operating contract, creating a task leaves it pending and only an
    exact claim plus launch may make it processing -- so this holds nothing.
    """
    return {
        "task_id": task_id,
        "runner": _RUNNER,
        "topic": "task_queue",
        "status": "pending",
        "worker_status": "unclaimed",
        "allowed_writes": writes,
    }


def _processing(task_id: str, writes: list[str]) -> dict:
    """An exact claim the store COMMITTED: ``claimed_by`` set, worker claimed."""
    return {
        "task_id": task_id,
        "runner": _RUNNER,
        "topic": "task_queue",
        "status": "processing",
        "worker_status": "claimed",
        "claimed_by": _RUNNER,
        "claim_epoch": 1,
        "allowed_writes": writes,
    }


def _preflight_over(tmp_path, monkeypatch, cards: list[dict], *, task_id: str) -> dict:
    """Drive ``collision_preflight`` over an exact card set via the JSONL source."""
    src = tmp_path / "cards.jsonl"
    src.write_text("\n".join(json.dumps(c) for c in cards), encoding="utf-8")
    monkeypatch.setenv(_CARDS_ENV, str(src))
    return tool.collision_preflight(task_id=task_id, runner=_RUNNER)


def test_pending_cards_are_not_held_claims_both_surfaces_agree(tmp_path, monkeypatch) -> None:
    """ONE identical card set drives BOTH surfaces and they do not contradict
    each other about what is held. Twelve PENDING orphans share the runner with
    DISTINCT write scopes. The old preflight counted all twelve as held claims
    and said ``would_collide=True``; the live guard said collision_free -- the
    exact contradiction. Claim truth makes them agree: nothing is held."""
    cards = [_pending(f"PEND-{i}", [f"src/pkg/mod_{i}.py"]) for i in range(12)]

    preflight = _preflight_over(tmp_path, monkeypatch, cards, task_id="NF-WAVE-BRAND-NEW")
    scan = core._scan_aiworkhub_collisions(cards)

    # Claim truth: a pending card holds nothing, so nothing is held for the runner.
    assert preflight["same_runner_other_active_claims"] == 0
    assert preflight["matching_task_id_active_claims"] == 0
    assert preflight["would_collide"] is False
    # The two surfaces agree: neither reports anything held/colliding.
    assert scan["collision_free"] is True
    assert scan["collision_count"] == 0
    # active_card_count still reports active-bucket membership (informational);
    # it is simply no longer read as a held-claim count.
    assert preflight["active_card_count"] == 12


def test_objective_shape_counts_only_held_claims(tmp_path, monkeypatch) -> None:
    """The exact reproduced shape: 12 pending + 7 processing, all on
    ``claude_opus-5``, distinct scopes. BEFORE (status membership) the preflight
    counted 19 (``same_runner_other_active_claims``) and would_collide=True.
    AFTER (claim truth) only the 7 committed claims count."""
    pend = [_pending(f"PEND-{i}", [f"src/pkg/p_{i}.py"]) for i in range(12)]
    proc = [_processing(f"PROC-{i}", [f"src/pkg/q_{i}.py"]) for i in range(7)]
    cards = pend + proc

    preflight = _preflight_over(tmp_path, monkeypatch, cards, task_id="NF-WAVE-BRAND-NEW")

    held = [c for c in cards if str(c.get("claimed_by") or "")]
    assert len(held) == 7
    # Only the 7 held claims count -- not the 12 phantom pending orphans (19 - 12).
    assert preflight["same_runner_other_active_claims"] == 7
    assert preflight["same_runner_other_active_claims"] == len(held)
    # active_card_count is still the active bucket size (19); claim-truth is a
    # separate, correctly-smaller count.
    assert preflight["active_card_count"] == 19
    assert preflight["active_card_count"] != preflight["same_runner_other_active_claims"]
    # The live write-scope guard, over the SAME set, sees no file overlap.
    scan = core._scan_aiworkhub_collisions(cards)
    assert scan["collision_free"] is True
    assert scan["active_cards"] == 19


def test_returned_value_names_runner_identity_distinct_from_write_scope(tmp_path, monkeypatch) -> None:
    """A caller can tell a write-scope collision from a runner-identity condition
    from the returned value alone, without reading either implementation."""
    cards = [_processing("PROC-1", ["src/a.py"])]
    preflight = _preflight_over(tmp_path, monkeypatch, cards, task_id="BRAND-NEW")

    # The preflight names the question it answers: runner identity, not files.
    assert preflight["condition_kind"] == "runner_identity"
    # ``runner_busy`` is the field that says this runner merely already holds
    # another active claim -- a busy runner, NOT a file collision.
    assert preflight["runner_busy"] is True
    assert "runner_already_claims_other_active_task" in preflight["collision_reasons"]

    # The write-scope surface is a DIFFERENT question with a different shape: it
    # never carries the runner-identity naming, so the two cannot be conflated.
    scan = core._scan_aiworkhub_collisions(cards)
    assert scan["schema_id"] == "aiworkhub.task_collision_report.v1"
    assert "file_collisions" in scan
    assert "condition_kind" not in scan
    assert "runner_busy" not in scan


def test_unresolved_configured_source_unreadable_stays_unresolved(tmp_path, monkeypatch) -> None:
    """An EXPLICITLY configured JSONL source that cannot be read is UNRESOLVED --
    never a confident zero -- and that fail-closed shape is unchanged."""
    missing = tmp_path / "does_not_exist.jsonl"
    monkeypatch.setenv(_CARDS_ENV, str(missing))
    result = tool.collision_preflight(task_id="T1", runner=_RUNNER)
    assert result["cards_source_resolved"] is False
    assert result["would_collide"] is None
    assert result["active_card_count"] is None
    # The claim count is None, never a comfortable 0.
    assert result["same_runner_other_active_claims"] is None
    assert result["runner_busy"] is None
    assert result["condition_kind"] == "runner_identity"


def test_unresolved_canonical_store_unavailable_stays_unresolved(monkeypatch) -> None:
    """An unavailable canonical store (a deleted/locked DB raising a bare
    ``sqlite3.Error`` after the readiness check) stays UNRESOLVED, never a zero."""

    def _boom() -> list:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(core, "_active_cards_for_collision_guard", _boom)
    result = tool.collision_preflight(task_id="T1", runner=_RUNNER)
    assert result["cards_source_resolved"] is False
    assert result["would_collide"] is None
    assert result["active_card_count"] is None
    assert result["same_runner_other_active_claims"] is None
    assert result["cards_source"] == "canonical_task_store"


def test_live_guard_still_detects_genuine_write_scope_overlap(tmp_path, monkeypatch) -> None:
    """The live write-scope guard must still flag a genuine overlap between two
    running cards. This assertion FAILS if the overlap check is weakened."""
    a = _processing("PROC-A", ["src/aiworkhub/core.py"])
    b = _processing("PROC-B", ["src/aiworkhub/core.py"])

    scan = core._scan_aiworkhub_collisions([a, b])
    assert scan["collision_free"] is False
    assert scan["collision_count"] == 1
    fc = scan["file_collisions"][0]
    assert fc["file"] == "src/aiworkhub/core.py"
    assert fc["conflicting_tasks"] == ["PROC-A", "PROC-B"]

    # And the preflight, asked about a THIRD brand-new task on the same runner,
    # still reports the runner-identity condition for the two held claims.
    preflight = _preflight_over(tmp_path, monkeypatch, [a, b], task_id="PROC-C")
    assert preflight["same_runner_other_active_claims"] == 2
    assert preflight["runner_busy"] is True
