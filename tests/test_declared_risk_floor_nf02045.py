"""NF-2026-02045: a declared risk tier is an upward floor, never lost.

A card created with ``risk_tier="high"`` whose changed paths carry no security
marker derived only ``medium`` at review_ready and at accept, so the security
lens was never required. The declaration now folds upward at both points; a
card whose tier was derived (``risk_tier_origin="derived"``) gets no new floor,
and a lower requested tier can never lower the declared one.
"""

from __future__ import annotations

import pytest

from aiworkhub import process_launcher_accept_review as accept_review
from aiworkhub import quality_evidence as qe

# A combined code change with no path marker: derives medium, no security lens.
PATHS = ["src/app/service.py", "src/app/models.py"]


def _card(**extra) -> dict:
    card = {"task_id": "T-1", "task_type": "code", "validation": ["pytest -q"]}
    card.update(extra)
    return card


def _derived_only_tier() -> str:
    signals = qe.derive_risk_signals(_card(), PATHS)
    return qe.resolve_risk_profile(qe.RISK_LOW, signals=signals)["effective_tier"]


def _sealed(card: dict, observation: dict) -> dict:
    card = dict(card)
    card["terminal_review"] = {
        "evidence": {"quality_gate": {"review_risk_profile": observation}}
    }
    return card


def _accept_lenses(card: dict, requested) -> tuple[str, list[str]]:
    tier = accept_review.effective_requested_risk_tier(card, requested)
    signals = qe.derive_risk_signals(card, PATHS)
    profile = qe.resolve_risk_profile(tier, signals=signals)
    return str(profile["effective_tier"]), list(profile["required_reviewer_lenses"])


def test_precondition_paths_derive_medium_without_security():
    assert _derived_only_tier() == qe.RISK_MEDIUM
    observation = qe.review_ready_risk_observation(_card(), PATHS)
    assert "security" not in observation["required_reviewer_lenses"]


def test_declared_high_survives_review_ready_and_accept():
    card = _card(risk_tier="high", risk_tier_origin="declared")
    observation = qe.review_ready_risk_observation(card, PATHS)
    assert observation["error"] == ""
    assert observation["effective_tier"] == qe.RISK_HIGH
    assert "security" in observation["required_reviewer_lenses"]

    tier, lenses = _accept_lenses(_sealed(card, observation), None)
    assert tier == qe.RISK_HIGH
    assert "security" in lenses


def test_declared_high_survives_accept_even_from_a_medium_seal():
    """A card sealed before the fix carries a medium observation."""
    card = _card(risk_tier="high", risk_tier_origin="declared")
    stale = qe.review_ready_risk_observation(_card(), PATHS)
    assert stale["effective_tier"] == qe.RISK_MEDIUM
    tier, lenses = _accept_lenses(_sealed(card, stale), None)
    assert tier == qe.RISK_HIGH
    assert "security" in lenses


def test_card_without_origin_is_read_back_as_declared():
    card = _card(risk_tier="high")
    assert qe.declared_risk_tier_floor(card) == qe.RISK_HIGH
    assert qe.review_ready_risk_observation(card, PATHS)["effective_tier"] == (
        qe.RISK_HIGH
    )


def test_derived_origin_gets_no_new_floor():
    card = _card(risk_tier="high", risk_tier_origin="derived")
    assert qe.declared_risk_tier_floor(card) == ""
    observation = qe.review_ready_risk_observation(card, PATHS)
    assert observation["effective_tier"] == qe.RISK_MEDIUM
    assert "security" not in observation["required_reviewer_lenses"]
    tier, lenses = _accept_lenses(_sealed(card, observation), None)
    assert tier == qe.RISK_MEDIUM
    assert "security" not in lenses


@pytest.mark.parametrize("requested", [qe.RISK_LOW, qe.RISK_MEDIUM])
def test_lower_requested_tier_cannot_lower_the_declared_one(requested):
    card = _card(risk_tier="high", risk_tier_origin="declared")
    observation = qe.review_ready_risk_observation(
        card, PATHS, requested_risk_tier=requested
    )
    assert observation["effective_tier"] == qe.RISK_HIGH
    tier, lenses = _accept_lenses(_sealed(card, observation), requested)
    assert tier == qe.RISK_HIGH
    assert "security" in lenses
    # Unsealed card: the declaration alone still floors the accept fold.
    assert accept_review.effective_requested_risk_tier(card, requested) == (
        qe.RISK_HIGH
    )


def test_declared_floor_never_lowers_a_higher_tier():
    card = _card(risk_tier="low", risk_tier_origin="declared")
    assert accept_review.effective_requested_risk_tier(card, "critical") == (
        "critical"
    )
    sealed = _sealed(card, {"effective_tier": qe.RISK_HIGH, "error": ""})
    assert accept_review.effective_requested_risk_tier(sealed, None) == qe.RISK_HIGH
    observation = qe.review_ready_risk_observation(card, PATHS)
    assert observation["effective_tier"] == qe.RISK_MEDIUM


@pytest.mark.parametrize("stored", ["bogus", "", None, 7])
def test_unknown_stored_tier_is_ignored_not_raised(stored):
    card = _card(risk_tier=stored, risk_tier_origin="declared")
    assert qe.declared_risk_tier_floor(card) == ""
    observation = qe.review_ready_risk_observation(card, PATHS)
    assert observation["error"] == ""
    assert observation["effective_tier"] == qe.RISK_MEDIUM
    assert accept_review.effective_requested_risk_tier(card, None) == qe.RISK_LOW


def test_unknown_requested_tier_still_fails_closed():
    card = _card(risk_tier="high", risk_tier_origin="declared")
    observation = qe.review_ready_risk_observation(
        card, PATHS, requested_risk_tier="not-a-tier"
    )
    assert observation["error"].startswith("MalformedConfigError:")
    assert accept_review.effective_requested_risk_tier(card, "not-a-tier") == (
        "not-a-tier"
    )
