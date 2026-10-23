"""An outvoted lone negative must not permanently veto a well-supported skill.

``blocking_negative_evidence`` tolerates an unresolved negative only when
independent acceptance overwhelms it, and every gate that blocks on negatives
reads that one predicate. These tests pin the measured case, each floor, the
unchanged reason codes, and the append order that "newest" rests on.
"""

from __future__ import annotations

import pytest

import aiworkhub.dashboard as dashboard
import aiworkhub.skill_miner as skill_miner
import aiworkhub.skill_registry as sr
from aiworkhub import skill_registry_store as store

ACTIVE = sr.LifecycleState.ACTIVE
PROPOSED = sr.LifecycleState.PROPOSED

NEGATIVE_CODE = "skill_registry.unresolved_negative_evidence"
INSUFFICIENT_CODE = "skill_registry.insufficient_evidence"
NEGATIVE_REASON = "unresolved_negative_evidence"

WORKER = sr.Authority(sr.AuthorityRole.WORKER, actor_id="worker-1", token="worker-token")
MANAGER = sr.Authority(sr.AuthorityRole.MANAGER, actor_id="manager-1", token="manager-secret")

BASE_DATA = {
    "identity": "commit-msg-check",
    "version": "1.0.0",
    "scope": "repository",
    "task_family": "commit",
    "path_or_symbol": "src/aiworkhub/skill_registry.py",
    "risk": "medium",
    "stage": "post-edit",
    "triggers": ["commit"],
    "confidence": 0.9,
}

WILDCARD_CONTEXT = {
    "task_family": "*",
    "path_or_symbol": "*",
    "risk": "*",
    "stage": "*",
    "triggers": ["*"],
    "applicability": ["*"],
}

TOLERATED = {
    "at_the_count_floor": {"accepted": 10, "negatives": 1},
    "at_the_share_floor": {"accepted": 90, "negatives": 10},
    "negative_between_acceptances": {"accepted": 20, "negatives": 1, "position": "middle"},
    "two_independent_actors": {"accepted": 20, "negatives": 1, "actors": 2},
    "resolved_row_is_newest": {"accepted": 20, "negatives": 1, "resolved_tail": True},
}

BLOCKED = {
    "below_the_count_floor": (
        {"accepted": 9, "negatives": 1},
        NEGATIVE_CODE,
    ),
    "below_the_share_floor": (
        {"accepted": 10, "negatives": 2},
        NEGATIVE_CODE,
    ),
    "just_below_the_share_floor": (
        {"accepted": 89, "negatives": 10},
        NEGATIVE_CODE,
    ),
    "newest_row_is_the_negative": (
        {"accepted": 20, "negatives": 1, "position": "last"},
        NEGATIVE_CODE,
    ),
    "single_accepted_actor": (
        {"accepted": 20, "negatives": 1, "actors": 1},
        INSUFFICIENT_CODE,
    ),
    "one_actor_spelled_two_ways": (
        {
            "accepted": 20,
            "negatives": 1,
            "actors": ("manager.claude.7e6e8a47", "claude_manager_7e6e8a47"),
        },
        INSUFFICIENT_CODE,
    ),
}


def row(outcome, actor_id, source, resolved=False):
    return sr.EvidenceRecord(
        source=source,
        outcome=sr.EvidenceOutcome(outcome),
        authority=sr.AuthorityRole.WORKER,
        actor_id=actor_id,
        resolved=resolved,
    )


def make_record(
    accepted,
    negatives=1,
    *,
    actors=8,
    position="first",
    resolved_tail=False,
    lifecycle=ACTIVE,
):
    """Spread ``accepted`` rows over ``actors`` (a count or explicit ids); place the negatives."""
    ids = [f"actor-{n}" for n in range(actors)] if isinstance(actors, int) else list(actors)
    rows = [row("accepted", ids[n % len(ids)], f"ok-{n}") for n in range(accepted)]
    cut = {"first": 0, "middle": accepted // 2, "last": accepted}[position]
    rows[cut:cut] = [row("negative", "actor-neg", f"incident-{n}") for n in range(negatives)]
    if resolved_tail:
        rows.append(row("negative", "actor-neg", "incident-old", resolved=True))
    return sr.SkillRecord.from_mapping(
        {**BASE_DATA, "evidence": rows, "lifecycle_state": lifecycle}
    )


def proposed_and_active(**shape):
    return make_record(lifecycle=PROPOSED, **shape), make_record(lifecycle=ACTIVE, **shape)


def registry_with(evidence):
    """Append ``(source, outcome, actor_id)`` rows through the real write path, in order."""
    registry = sr.SkillRegistry(min_accepted_evidence=2)
    registry.propose(sr.SkillRecord.from_mapping(BASE_DATA), WORKER)
    for source, outcome, actor_id in evidence:
        actor = sr.Authority(sr.AuthorityRole.WORKER, actor_id=actor_id, token=f"tok-{actor_id}")
        registry.add_evidence(
            BASE_DATA["identity"],
            BASE_DATA["version"],
            {"source": source, "outcome": outcome},
            actor,
        )
    return registry


def accepted_rows(count):
    return [(f"ok-{n}", "accepted", f"actor-{n % 8}") for n in range(count)]


def test_measured_case_88_accepted_1_negative_8_actors_is_not_blocked():
    proposed, active = proposed_and_active(accepted=88, negatives=1, actors=8)

    assert sr.independent_accepted_evidence_count(active) == 8
    assert sr.blocking_negative_evidence(active) == ()
    decision = sr.can_activate(proposed, 2)
    assert (decision.allowed, decision.reason_code) == (True, "skill_registry.ok")
    assert store.activation_supported(active, 2) is True
    assert skill_miner.injectability(active) == (True, "")
    # With the negative gate silent, the next gate in line speaks instead.
    assert skill_miner.injectability(proposed) == (
        False,
        "lifecycle_state_is_proposed_not_active",
    )
    # Tolerating is not forgetting: the row is still stored and still reported.
    assert len(sr.unresolved_negative_evidence(active)) == 1
    assert active.negative_count == 1


@pytest.mark.parametrize("shape", list(TOLERATED.values()), ids=list(TOLERATED))
def test_tolerated_shapes_pass_every_gate(shape):
    proposed, active = proposed_and_active(**shape)

    assert sr.blocking_negative_evidence(active) == ()
    assert sr.can_activate(proposed, 2).allowed is True
    assert store.activation_supported(active, 2) is True
    assert skill_miner.injectability(active) == (True, "")
    assert len(sr.unresolved_negative_evidence(active)) == shape["negatives"]


@pytest.mark.parametrize(
    ("shape", "activate_code"), list(BLOCKED.values()), ids=list(BLOCKED)
)
def test_blocked_shapes_stay_blocked_at_every_gate(shape, activate_code):
    proposed, active = proposed_and_active(**shape)

    blocking = sr.blocking_negative_evidence(active)
    assert blocking
    assert blocking == sr.unresolved_negative_evidence(active)
    decision = sr.can_activate(proposed, 2)
    assert (decision.allowed, decision.reason_code) == (False, activate_code)
    assert store.activation_supported(active, 2) is False
    assert skill_miner.injectability(active) == (False, NEGATIVE_REASON)


def test_blocked_activation_keeps_its_exact_reason_code_and_message():
    proposed = make_record(9, 1, lifecycle=PROPOSED)

    assert sr.can_activate(proposed, 2) == sr.TransitionDecision(
        allowed=False,
        reason_code="skill_registry.unresolved_negative_evidence",
        reason="unresolved negative evidence from 'actor-neg'",
    )


@pytest.mark.parametrize("accepted", [0, 1, 2, 20])
def test_zero_negatives_report_nothing_blocking(accepted):
    record = make_record(accepted, 0, actors=2)

    assert sr.unresolved_negative_evidence(record) == ()
    assert sr.blocking_negative_evidence(record) == ()


def test_zero_negatives_leave_only_the_actor_floor_in_charge():
    two_proposed, two_active = proposed_and_active(accepted=2, negatives=0, actors=2)
    one_proposed, one_active = proposed_and_active(accepted=20, negatives=0, actors=1)

    assert sr.can_activate(two_proposed, 2).allowed is True
    assert store.activation_supported(two_active, 2) is True
    assert skill_miner.injectability(two_active) == (True, "")
    assert sr.can_activate(one_proposed, 2).reason_code == INSUFFICIENT_CODE
    assert store.activation_supported(one_active, 2) is False
    assert skill_miner.injectability(one_active) == (
        False,
        "activation_evidence_below_two_distinct_actors",
    )


def test_a_fresh_negative_after_a_tolerated_one_blocks_again():
    tolerated = make_record(88, 1)
    fresh = sr.replace(
        tolerated,
        evidence=tolerated.evidence + (row("negative", "actor-neg", "incident-new"),),
        negative_count=2,
    )

    assert sr.blocking_negative_evidence(tolerated) == ()
    assert len(sr.blocking_negative_evidence(fresh)) == 2
    assert store.activation_supported(fresh, 2) is False
    assert skill_miner.injectability(fresh) == (False, NEGATIVE_REASON)


def test_activation_flow_tolerates_an_earlier_negative():
    registry = registry_with([("incident", "negative", "actor-neg"), *accepted_rows(20)])

    activated = registry.activate(BASE_DATA["identity"], BASE_DATA["version"], MANAGER)

    assert activated.lifecycle_state is ACTIVE


def test_activation_flow_refuses_a_newest_negative_until_a_manager_resolves_it():
    registry = registry_with([*accepted_rows(20), ("incident", "negative", "actor-neg")])
    identity, version = BASE_DATA["identity"], BASE_DATA["version"]

    with pytest.raises(sr.SkillRegistryError) as excinfo:
        registry.activate(identity, version, MANAGER)
    assert excinfo.value.code == NEGATIVE_CODE

    registry.resolve_negative_evidence(identity, version, "incident", "fixed upstream", MANAGER)
    assert registry.activate(identity, version, MANAGER).lifecycle_state is ACTIVE


def test_dashboard_gate_agrees_with_the_miner_with_and_without_it(monkeypatch):
    outvoted = make_record(88, 1)
    blocked = make_record(9, 1)

    assert dashboard._skill_injectability(sr, outvoted) == (True, "")
    assert dashboard._skill_injectability(sr, blocked) == (False, NEGATIVE_REASON)

    monkeypatch.setattr(dashboard, "_load_skill_miner", lambda: None)
    assert dashboard._skill_injectability(sr, outvoted) == (True, "")
    assert dashboard._skill_injectability(sr, blocked) == (False, NEGATIVE_REASON)


@pytest.mark.parametrize(
    ("accepted", "stays_active"), [(88, True), (9, False)], ids=["outvoted", "count_floor"]
)
def test_a_stored_active_record_follows_the_negative_gate(tmp_path, accepted, stays_active):
    record = make_record(accepted, 1)
    store.put_record(tmp_path, record)

    loaded = store.load_registry(tmp_path)

    state = loaded.get(record.identity, record.version).lifecycle_state
    assert (state is ACTIVE) is stays_active
    assert len(loaded.select(WILDCARD_CONTEXT).selected) == int(stays_active)
    assert store.audit_active_records(tmp_path)[0]["verified"] is stays_active


def test_tolerance_floors_are_exported_with_their_documented_values():
    assert sr.NEGATIVE_TOLERANCE_MIN_ACCEPTED == 10
    assert sr.NEGATIVE_TOLERANCE_MIN_SHARE == 0.9
    assert sr.NEGATIVE_TOLERANCE_MIN_ACTORS == 2
    for name in (
        "blocking_negative_evidence",
        "NEGATIVE_TOLERANCE_MIN_ACCEPTED",
        "NEGATIVE_TOLERANCE_MIN_SHARE",
        "NEGATIVE_TOLERANCE_MIN_ACTORS",
    ):
        assert name in sr.__all__
