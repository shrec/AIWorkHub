"""B435: compact callback prompt rendering tests.

Single-task: exactly ``Task MCP: <task_id> → <state>``.
Batch: exactly ``Task MCP: <N> tasks terminal → inspect review queue``.

Validation gates (durable delivery) unchanged.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aiworkhub.callback_bridge import (
    CALLBACK_ELIGIBLE_STATES,
    build_batch_callback_prompt,
    build_callback_prompt,
)


# ---------------------------------------------------------------------------
# Single-task compact format
# ---------------------------------------------------------------------------

def test_single_task_compact_format_exact():
    """Single task must render exactly 'Task MCP: TASK_ID → state'."""
    prompt = build_callback_prompt("TASK_001", "review_ready", event_id="ev1", request_id="r1")
    assert prompt == "Task MCP: TASK_001 → review_ready"


def test_single_task_compact_format_various_states():
    """Every eligible terminal state produces exact compact form."""
    for state in sorted(CALLBACK_ELIGIBLE_STATES):
        prompt = build_callback_prompt("DEEPSEEK_X_V1", state)
        assert prompt == f"Task MCP: DEEPSEEK_X_V1 → {state}"


def test_single_task_no_old_boilerplate():
    """Compact prompt must not contain old verbose boilerplate."""
    prompt = build_callback_prompt("TASK_A", "blocked")
    for banned in (
        "coordinator should inspect",
        "No worker output",
        "failure details",
        "logs",
        "artifacts",
        "objective text",
        "The AIWorkHub MCP worker",
        "perform coordinator review/rebase",
    ):
        assert banned not in prompt, f"boilerplate leaked: {banned!r}"


# ---------------------------------------------------------------------------
# Batch compact format
# ---------------------------------------------------------------------------

def test_batch_compact_format_exact_single():
    """A single-member batch preserves the useful exact task identity."""
    members = [{"task_id": "TASK_001", "state": "review_ready", "event_id": "e1", "request_id": "r1"}]
    prompt = build_batch_callback_prompt(members)
    assert prompt == "Task MCP: TASK_001 → review_ready"


def test_batch_compact_format_exact_multi():
    """Multi-member batch: 'Task MCP: N tasks terminal → inspect review queue'."""
    members = [
        {"task_id": "TASK_001", "state": "review_ready", "event_id": "e1", "request_id": "r1"},
        {"task_id": "TASK_002", "state": "blocked", "event_id": "e2", "request_id": "r2"},
        {"task_id": "TASK_003", "state": "cancelled", "event_id": "e3", "request_id": "r3"},
    ]
    prompt = build_batch_callback_prompt(members)
    assert prompt == "Task MCP: 3 tasks terminal → inspect review queue"


def test_batch_compact_format_large_count():
    """Batch count is a plain integer, not padded or truncated."""
    members = [{"task_id": f"TK_{i:04d}", "state": "review_ready"} for i in range(42)]
    prompt = build_batch_callback_prompt(members)
    assert prompt == "Task MCP: 42 tasks terminal → inspect review queue"


def test_batch_no_old_boilerplate():
    """Batch prompt must not contain old verbose boilerplate."""
    members = [{"task_id": "TK1", "state": "review_ready"}, {"task_id": "TK2", "state": "blocked"}]
    prompt = build_batch_callback_prompt(members)
    for banned in (
        "coordinator should inspect",
        "No worker output",
        "failure details",
        "logs",
        "artifacts",
        "objective text",
        "does not need a separate wake",
        "The AIWorkHub MCP worker",
    ):
        assert banned not in prompt, f"boilerplate leaked: {banned!r}"


# ---------------------------------------------------------------------------
# Validation gates preserved (durable delivery unchanged)
# ---------------------------------------------------------------------------

def test_single_task_validation_rejects_control_characters():
    with pytest.raises(ValueError):
        build_callback_prompt("TASK\n001", "review_ready")
    with pytest.raises(ValueError):
        build_callback_prompt("TASK_001", "review\rready")
    with pytest.raises(ValueError):
        build_callback_prompt("TASK_001", "review_ready", event_id="e\t1")


def test_single_task_validation_rejects_nonterminal_state():
    for bad_state in ("pending", "processing", "done", "reject"):
        with pytest.raises(ValueError, match="invalid callback terminal state"):
            build_callback_prompt("TASK_001", bad_state)


def test_single_task_validation_rejects_invalid_task_id():
    with pytest.raises(ValueError, match="invalid callback task_id"):
        build_callback_prompt("TASK 001 inject", "review_ready")
    with pytest.raises(ValueError, match="invalid callback task_id"):
        build_callback_prompt("", "review_ready")


def test_batch_validation_rejects_invalid_member():
    """Batch still validates every member -- durable gate unchanged."""
    members = [
        {"task_id": "TK1", "state": "review_ready"},
        {"task_id": "TK2", "state": "processing"},  # non-terminal
    ]
    with pytest.raises(ValueError, match="invalid callback terminal state"):
        build_batch_callback_prompt(members)


def test_batch_validation_rejects_empty():
    with pytest.raises(ValueError, match="requires at least one member"):
        build_batch_callback_prompt([])


# ---------------------------------------------------------------------------
# Unicode / unusual task_id safety
# ---------------------------------------------------------------------------

def test_unicode_task_id_safely_rejected():
    """Georgian/Unicode chars are rejected by the regex gate, no crash."""
    with pytest.raises(ValueError):
        build_callback_prompt("დავალება_001", "review_ready")


def test_unicode_state_safely_rejected():
    """Unicode state is rejected (not in CALLBACK_ELIGIBLE_STATES)."""
    with pytest.raises(ValueError):
        build_callback_prompt("TASK_001", "მზადაა")


def test_batch_unicode_member_safely_rejected():
    members = [{"task_id": "valid_1", "state": "review_ready"}, {"task_id": "დავ_2", "state": "review_ready"}]
    with pytest.raises(ValueError, match="invalid callback task_id"):
        build_batch_callback_prompt(members)


# ---------------------------------------------------------------------------
# Function signatures: no new untrusted parameters
# ---------------------------------------------------------------------------

def test_single_task_signature_only_validated_params():
    sig = inspect.signature(build_callback_prompt)
    allowed = {"task_id", "state", "event_id", "request_id"}
    assert set(sig.parameters) == allowed


def test_batch_signature_only_members_param():
    sig = inspect.signature(build_batch_callback_prompt)
    assert set(sig.parameters) == {"members"}
