"""Tests for the standalone, typed, immutable Repository Skill Registry foundation.

These tests cover the acceptance criteria for RM-2026-00021: deterministic
identity/version/digest, fail-closed validation, bounded deterministic ranking,
recurrence thresholding, negative safety evidence, immutability of historical
versions, and the manager-authority gate.
"""

from __future__ import annotations

import json

import pytest

import aiworkhub.skill_registry as sr

WORKER = sr.Authority(sr.AuthorityRole.WORKER, actor_id="worker-1", token="worker-token")
MANAGER = sr.Authority(sr.AuthorityRole.MANAGER, actor_id="manager-1", token="manager-secret")
MANAGER_NO_TOKEN = sr.Authority(sr.AuthorityRole.MANAGER, actor_id="manager-1", token="")

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


def raw_data(**overrides):
    data = dict(BASE_DATA)
    data.update(overrides)
    return data


def base_record(**overrides):
    return sr.SkillRecord.from_mapping(raw_data(**overrides))


def assert_fails(code, fn):
    with pytest.raises(sr.SkillRegistryError) as excinfo:
        fn()
    assert excinfo.value.code == code


def evidence_record(source, outcome, actor_id=None, authority="worker"):
    return sr.EvidenceRecord(
        source=source,
        outcome=sr.EvidenceOutcome(outcome),
        authority=sr.AuthorityRole(authority),
        actor_id=source if actor_id is None else actor_id,
    )


def with_evidence(record, entries):
    evidence = tuple(evidence_record(**entry) for entry in entries)
    accepted = sum(1 for item in evidence if item.outcome is sr.EvidenceOutcome.ACCEPTED)
    negative = sum(1 for item in evidence if item.outcome is sr.EvidenceOutcome.NEGATIVE)
    return sr.replace(record, evidence=evidence, accepted_count=accepted, negative_count=negative)


def _record(identity, task_family="z", path="z", risk="low", stage="z", accepted=0):
    record = base_record(
        identity=identity, task_family=task_family, path_or_symbol=path,
        risk=risk, stage=stage,
    )
    for index in range(accepted):
        record = sr.replace(
            record,
            evidence=record.evidence
            + (evidence_record(f"src-{index}", "accepted", actor_id=f"actor-{index}"),),
            accepted_count=accepted,
        )
    return record


def propose_with_accepted_evidence(registry, identity="commit-msg-check", version="1.0.0"):
    """Propose evidence-free, then append accepted evidence from distinct actors."""
    registry.propose(base_record(identity=identity, version=version), WORKER)
    for actor_id in ("actor-a", "actor-b"):
        actor = sr.Authority(sr.AuthorityRole.WORKER, actor_id=actor_id, token=f"tok-{actor_id}")
        registry.add_evidence(
            identity,
            version,
            {"source": f"src-{actor_id}", "outcome": "accepted"},
            actor,
        )
    return registry.get(identity, version)


# ---------------------------------------------------------------------------
# Determinism: identity, version, digest, normalized payload
# ---------------------------------------------------------------------------


def test_digest_is_deterministic_across_mapping_order():
    ordered = sr.SkillRecord.from_mapping(BASE_DATA)
    reversed_keys = list(reversed(list(BASE_DATA.keys())))
    rebuilt = {key: BASE_DATA[key] for key in reversed_keys}
    assert sr.skill_digest(ordered) == sr.skill_digest(sr.SkillRecord.from_mapping(rebuilt))
    assert sr.canonical_payload(ordered) == sr.canonical_payload(
        sr.SkillRecord.from_mapping(rebuilt)
    )


def test_canonical_json_sorts_mapping_keys():
    assert sr.canonical_json({"b": 1, "a": [2, 3]}) == sr.canonical_json({"a": [2, 3], "b": 1})


def test_evidence_actor_id_in_canonical_but_not_in_skill_digest():
    # Records differing only by evidence actor_id canonicalize differently ...
    record_a = with_evidence(
        base_record(), [{"source": "s1", "outcome": "accepted", "actor_id": "actor-a"}]
    )
    record_b = with_evidence(
        base_record(), [{"source": "s1", "outcome": "accepted", "actor_id": "actor-b"}]
    )
    assert sr.canonical_json(record_a.evidence[0]) != sr.canonical_json(record_b.evidence[0])
    # ... but the skill content digest intentionally excludes evidence entirely.
    assert sr.skill_digest(record_a) == sr.skill_digest(record_b)


def test_normalized_payload_is_order_independent():
    first = sr.normalize(
        {"identity": "a", "version": "1.0.0", "scope": "repository",
         "task_family": "t", "path_or_symbol": "p", "risk": "low",
         "stage": "s", "triggers": ["x", "y"], "confidence": 0.5}
    )
    second = sr.normalize(
        {"confidence": 0.5, "triggers": ["x", "y"], "stage": "s", "risk": "low",
         "path_or_symbol": "p", "task_family": "t", "scope": "repository",
         "version": "1.0.0", "identity": "a"}
    )
    assert first == second
    assert isinstance(first["triggers"], tuple)


# ---------------------------------------------------------------------------
# Fail-closed validation
# ---------------------------------------------------------------------------


def test_unknown_key_fails_closed():
    assert_fails("skill_registry.unknown_key", lambda: sr.normalize(raw_data(extra=1)))


def test_missing_field_fails_closed():
    data = {key: value for key, value in BASE_DATA.items() if key != "identity"}
    assert_fails("skill_registry.missing_field", lambda: sr.normalize(data))


@pytest.mark.parametrize("identity", ["Bad", "1bad", "", "a" * 129, "with space"])
def test_malformed_identity_rejected(identity):
    assert_fails(
        "skill_registry.invalid_identity",
        lambda identity=identity: sr.normalize(raw_data(identity=identity)),
    )


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3.4", "", "one.two.three"])
def test_malformed_version_rejected(version):
    assert_fails(
        "skill_registry.invalid_version",
        lambda version=version: sr.normalize(raw_data(version=version)),
    )


@pytest.mark.parametrize("path", ["/abs", "C:\\x", "a/../b", "..", ".", "a\x00b"])
def test_unsafe_path_rejected(path):
    assert_fails(
        "skill_registry.unsafe_path",
        lambda path=path: sr.normalize(raw_data(path_or_symbol=path)),
    )


def test_invalid_scope_and_risk_rejected():
    assert_fails("skill_registry.invalid_scope", lambda: sr.normalize(raw_data(scope="nope")))
    assert_fails("skill_registry.invalid_risk", lambda: sr.normalize(raw_data(risk="nope")))


def test_bool_as_int_counter_rejected():
    assert_fails(
        "skill_registry.invalid_counter",
        lambda: sr.normalize(raw_data(accepted_count=True)),
    )
    assert_fails(
        "skill_registry.invalid_counter",
        lambda: sr.normalize(raw_data(negative_count=-1)),
    )


def test_counter_mismatch_rejected():
    data = dict(BASE_DATA)
    data.update(
        evidence=[
            {"source": "s", "outcome": "accepted", "authority": "worker", "actor_id": "a1"}
        ],
        accepted_count=2,
    )
    assert_fails("skill_registry.counter_mismatch", lambda: sr.normalize(data))


@pytest.mark.parametrize(
    "confidence",
    [float("nan"), float("inf"), float("-inf"), -1.0, 1.5, 2.0],
)
def test_non_finite_or_out_of_range_confidence_rejected(confidence):
    assert_fails(
        "skill_registry.invalid_confidence",
        lambda confidence=confidence: sr.normalize(raw_data(confidence=confidence)),
    )


@pytest.mark.parametrize(
    "triggers,avoid_rules",
    [
        (["x", "x"], []),
        (["x", "!x"], []),
        (["x"], ["x"]),
        (["x"], ["!x"]),
    ],
)
def test_contradictory_triggers_rejected(triggers, avoid_rules):
    assert_fails(
        "skill_registry.contradictory_triggers",
        lambda: sr.normalize(raw_data(triggers=triggers, avoid_rules=avoid_rules)),
    )


def test_lifecycle_transition_table():
    assert sr.transition_allowed("proposed", "active")
    assert not sr.transition_allowed("proposed", "retired")
    assert sr.transition_allowed("active", "retired")
    assert not sr.transition_allowed("active", "proposed")
    assert not sr.transition_allowed("retired", "active")
    assert not sr.transition_allowed("retired", "proposed")
    # Self-transitions are fail-closed (no state change).
    assert not sr.transition_allowed("proposed", "proposed")
    assert not sr.transition_allowed("active", "active")
    assert not sr.transition_allowed("retired", "retired")


# ---------------------------------------------------------------------------
# Deterministic bounded ranking
# ---------------------------------------------------------------------------


def test_ranking_is_deterministic_and_bounded():
    high = _record("high", risk="critical")
    low = _record("low", risk="low", accepted=3)
    medium = _record("medium", risk="medium")
    ranked = sr.rank([low, medium, high])
    assert [r.identity for r in ranked] == ["high", "medium", "low"]
    assert sr.rank([high, medium, low]) == ranked
    assert sr.rank([low, medium, high], limit=2) == ranked[:2]
    assert sr.rank([low, medium, high], limit=0) == []
    assert sr.rank([], limit=5) == []


def test_ranking_tie_break_is_lexical():
    first = _record("alpha", task_family="same", path="same", risk="low", stage="same")
    second = _record("beta", task_family="same", path="same", risk="low", stage="same")
    assert [r.identity for r in sr.rank([second, first])] == ["alpha", "beta"]


def test_ranking_rejects_invalid_limit():
    record = _record("x")
    assert_fails("skill_registry.invalid_value", lambda: sr.rank([record], limit=-1))
    assert_fails("skill_registry.invalid_type", lambda: sr.rank([record], limit=True))
    assert_fails("skill_registry.invalid_type", lambda: sr.rank([record], limit=1.5))
    assert_fails("skill_registry.invalid_type", lambda: sr.rank("not-an-iterable"))
    assert_fails("skill_registry.invalid_type", lambda: sr.rank([record, "bad"]))


def test_rank_is_bounded_by_default_and_clamps_above_cap():
    records = [base_record(identity=f"skill{i:04d}") for i in range(5)]
    ranked = sr.rank(records)
    assert len(ranked) == 5
    assert sr.rank(records, limit=None) == ranked
    assert sr.rank(records, limit=0) == []
    assert sr.rank(records, limit=sr.MAX_RANK_LIMIT + 1) == ranked


def test_rank_is_bounded_on_large_iterables():
    def make(count):
        for index in range(count):
            yield base_record(identity=f"skill{index:05d}")

    assert len(sr.rank(make(sr.MAX_RANK_LIMIT + 1))) == sr.MAX_RANK_LIMIT
    assert (
        len(sr.rank(make(sr.MAX_RANK_LIMIT + 1), limit=sr.MAX_RANK_LIMIT + 5))
        == sr.MAX_RANK_LIMIT
    )


def test_rank_retains_at_most_cap_candidates_by_construction():
    total = sr.MAX_RANK_LIMIT + 1000
    ranked = sr.rank((base_record(identity=f"skill{index:06d}") for index in range(total)))
    assert len(ranked) == sr.MAX_RANK_LIMIT


def test_rank_selects_global_top_items_from_a_stream():
    # The single highest-ranked candidate appears last in the stream; a first-N
    # truncation would drop it, so this asserts genuine top-N selection rather
    # than truncation of the head of the stream.
    def make():
        for index in range(sr.MAX_RANK_LIMIT):
            yield base_record(identity=f"low{index:06d}", risk="low")
        yield base_record(identity="critical-last", risk="critical")

    ranked = sr.rank(make())
    assert len(ranked) == sr.MAX_RANK_LIMIT
    assert ranked[0].identity == "critical-last"
    assert all(item.identity != "critical-last" for item in ranked[1:])


# ---------------------------------------------------------------------------
# Recurrence threshold and negative safety evidence
# ---------------------------------------------------------------------------


def test_one_success_does_not_activate():
    record = with_evidence(base_record(), [{"source": "s1", "outcome": "accepted"}])
    decision = sr.can_activate(record, 2)
    assert not decision.allowed
    assert decision.reason_code == "skill_registry.insufficient_evidence"


def test_one_actor_with_two_labels_is_not_independent():
    # A single provenance identity cannot forge independence by choosing two
    # different free-text source labels.
    record = with_evidence(
        base_record(),
        [
            {"source": "s1", "outcome": "accepted", "actor_id": "actor-a"},
            {"source": "s2", "outcome": "accepted", "actor_id": "actor-a"},
        ],
    )
    assert sr.independent_accepted_evidence_count(record) == 1
    assert not sr.can_activate(record, 2).allowed


def test_two_distinct_actors_can_activate():
    record = with_evidence(
        base_record(),
        [
            {"source": "s1", "outcome": "accepted", "actor_id": "actor-a"},
            {"source": "s2", "outcome": "accepted", "actor_id": "actor-b"},
        ],
    )
    assert sr.independent_accepted_evidence_count(record) == 2
    assert sr.can_activate(record, 2).allowed


def test_evidence_without_provenance_identity_is_rejected():
    data = raw_data(
        evidence=[{"source": "s1", "outcome": "accepted", "authority": "worker"}],
        accepted_count=1,
    )
    assert_fails("skill_registry.invalid_evidence", lambda: sr.normalize(data))


@pytest.mark.parametrize("actor_id", ["Bad", "with space", "a" * 129])
def test_evidence_actor_id_shape_validated(actor_id):
    data = raw_data(
        evidence=[
            {"source": "s", "outcome": "accepted", "authority": "worker", "actor_id": actor_id}
        ],
        accepted_count=1,
    )
    assert_fails("skill_registry.invalid_evidence", lambda: sr.normalize(data))


def test_unresolved_negative_evidence_blocks_activation():
    record = with_evidence(
        base_record(),
        [
            {"source": "s1", "outcome": "accepted"},
            {"source": "s2", "outcome": "accepted"},
            {"source": "s3", "outcome": "negative"},
        ],
    )
    decision = sr.can_activate(record, 2)
    assert not decision.allowed
    assert decision.reason_code == "skill_registry.unresolved_negative_evidence"


def test_resolved_negative_evidence_does_not_block():
    record = with_evidence(
        base_record(),
        [
            {"source": "s1", "outcome": "accepted"},
            {"source": "s2", "outcome": "accepted"},
            {"source": "s3", "outcome": "negative"},
        ],
    )
    resolved = sr.replace(
        record,
        evidence=tuple(
            sr.replace(item, resolved=True)
            if item.outcome is sr.EvidenceOutcome.NEGATIVE
            else item
            for item in record.evidence
        ),
    )
    assert sr.unresolved_negative_evidence(resolved) == ()
    assert sr.can_activate(resolved, 2).allowed


def test_append_cannot_pre_resolve_negative_evidence():
    registry = sr.SkillRegistry()
    registry.propose(base_record(), WORKER)
    for actor_id in ("actor-a", "actor-b"):
        actor = sr.Authority(sr.AuthorityRole.WORKER, actor_id=actor_id, token=f"tok-{actor_id}")
        registry.add_evidence(
            "commit-msg-check",
            "1.0.0",
            {"source": f"src-{actor_id}", "outcome": "accepted"},
            actor,
        )

    # A worker cannot pre-resolve a negative by submitting resolved=True (mapping).
    neg_worker = sr.Authority(sr.AuthorityRole.WORKER, actor_id="neg-worker", token="t")
    updated = registry.add_evidence(
        "commit-msg-check",
        "1.0.0",
        {"source": "src-neg-1", "outcome": "negative", "resolved": True},
        neg_worker,
    )
    assert updated.evidence[-1].outcome is sr.EvidenceOutcome.NEGATIVE
    assert updated.evidence[-1].resolved is False
    assert_fails(
        "skill_registry.unresolved_negative_evidence",
        lambda: registry.activate("commit-msg-check", "1.0.0", MANAGER),
    )

    # The same holds for a prebuilt EvidenceRecord input.
    updated = registry.add_evidence(
        "commit-msg-check",
        "1.0.0",
        sr.EvidenceRecord(
            source="src-neg-2",
            outcome=sr.EvidenceOutcome.NEGATIVE,
            authority=sr.AuthorityRole.WORKER,
            actor_id="neg-worker",
            resolved=True,
        ),
        neg_worker,
    )
    assert updated.evidence[-1].resolved is False
    assert_fails(
        "skill_registry.unresolved_negative_evidence",
        lambda: registry.activate("commit-msg-check", "1.0.0", MANAGER),
    )

    # Even a manager append cannot pre-resolve a negative.
    updated = registry.add_evidence(
        "commit-msg-check",
        "1.0.0",
        {"source": "src-neg-3", "outcome": "negative", "resolved": True},
        MANAGER,
    )
    assert updated.evidence[-1].resolved is False
    assert_fails(
        "skill_registry.unresolved_negative_evidence",
        lambda: registry.activate("commit-msg-check", "1.0.0", MANAGER),
    )

    # Only the explicit manager resolution transition clears the negatives.
    resolved = registry.mark_negative_resolved("commit-msg-check", "1.0.0", MANAGER)
    assert all(
        item.resolved for item in resolved.evidence if item.outcome is sr.EvidenceOutcome.NEGATIVE
    )
    activated = registry.activate("commit-msg-check", "1.0.0", MANAGER)
    assert activated.lifecycle_state is sr.LifecycleState.ACTIVE


def test_min_accepted_evidence_is_configurable_but_at_least_two():
    record = with_evidence(base_record(), [{"source": "s1", "outcome": "accepted"}])
    assert sr.can_activate(record, 2).allowed is False
    assert sr.can_activate(record, 3).allowed is False
    assert_fails(
        "skill_registry.invalid_value", lambda: sr.SkillRegistry(min_accepted_evidence=1)
    )
    assert_fails(
        "skill_registry.invalid_type", lambda: sr.SkillRegistry(min_accepted_evidence=True)
    )


def test_can_activate_enforces_min_accepted_evidence_contract():
    record = with_evidence(base_record(), [{"source": "s1", "outcome": "accepted"}])
    assert_fails("skill_registry.invalid_value", lambda: sr.can_activate(record, 1))
    assert_fails("skill_registry.invalid_type", lambda: sr.can_activate(record, True))
    assert_fails("skill_registry.invalid_type", lambda: sr.can_activate(record, 2.5))


def test_can_activate_decision_is_bool_safe():
    enough = with_evidence(
        base_record(),
        [
            {"source": "s1", "outcome": "accepted", "actor_id": "actor-a"},
            {"source": "s2", "outcome": "accepted", "actor_id": "actor-b"},
        ],
    )
    assert bool(sr.can_activate(enough, 2)) is True
    assert bool(sr.can_activate(base_record(), 2)) is False


# ---------------------------------------------------------------------------
# Provenance binding and secret hygiene
# ---------------------------------------------------------------------------


def test_add_evidence_binds_authority_and_provenance_identity():
    registry = sr.SkillRegistry()
    registry.propose(base_record(), WORKER)
    updated = registry.add_evidence(
        "commit-msg-check",
        "1.0.0",
        {"source": "s1", "outcome": "accepted", "authority": "manager", "actor_id": "forged"},
        WORKER,
    )
    entry = updated.evidence[-1]
    assert entry.authority is sr.AuthorityRole.WORKER
    assert entry.actor_id == WORKER.actor_id


def test_evidence_records_never_persist_the_secret_token():
    secret = "manager-secret-token"
    registry = sr.SkillRegistry()
    registry.propose(base_record(), WORKER)
    manager = sr.Authority(sr.AuthorityRole.MANAGER, actor_id="manager-1", token=secret)
    updated = registry.add_evidence(
        "commit-msg-check", "1.0.0", {"source": "s1", "outcome": "accepted"}, manager
    )
    entry = updated.evidence[-1]
    assert entry.actor_id == "manager-1"
    assert not hasattr(entry, "token")
    assert secret not in str(entry)


def test_add_evidence_rejects_actor_without_provenance_identity():
    registry = sr.SkillRegistry()
    registry.propose(base_record(), WORKER)
    anonymous = sr.Authority(sr.AuthorityRole.WORKER, actor_id="", token="t")
    assert_fails(
        "skill_registry.unauthorized",
        lambda: registry.add_evidence(
            "commit-msg-check", "1.0.0", {"source": "s1", "outcome": "accepted"}, anonymous
        ),
    )


@pytest.mark.parametrize("actor_id", ["Bad", "with space", "-lead", "1lead", "a" * 129])
def test_authority_actor_id_shape_validated(actor_id):
    authority = sr.Authority(sr.AuthorityRole.WORKER, actor_id=actor_id, token="t")
    registry = sr.SkillRegistry()
    registry.propose(base_record(), WORKER)
    assert_fails(
        "skill_registry.unauthorized",
        lambda: registry.add_evidence(
            "commit-msg-check", "1.0.0", {"source": "s", "outcome": "accepted"}, authority
        ),
    )


# ---------------------------------------------------------------------------
# Immutability of historical versions
# ---------------------------------------------------------------------------


def test_identical_content_cannot_be_re_registered():
    registry = sr.SkillRegistry()
    registry.propose(base_record(), WORKER)
    assert_fails(
        "skill_registry.immutable_digest",
        lambda: registry.propose(base_record(), WORKER),
    )


def test_existing_identity_version_cannot_be_overwritten():
    registry = sr.SkillRegistry()
    registry.propose(base_record(), WORKER)
    assert_fails(
        "skill_registry.immutable_identity",
        lambda: registry.propose(base_record(stage="pre-edit"), WORKER),
    )


def test_digest_is_stable_across_runtime_state_changes():
    registry = sr.SkillRegistry()
    proposed = registry.propose(base_record(), WORKER)
    digest = sr.skill_digest(proposed)
    worker2 = sr.Authority(sr.AuthorityRole.WORKER, actor_id="worker-2", token="w2")
    registry.add_evidence("commit-msg-check", "1.0.0", evidence_record("s1", "accepted"), WORKER)
    registry.add_evidence("commit-msg-check", "1.0.0", evidence_record("s2", "accepted"), worker2)
    updated = registry.activate("commit-msg-check", "1.0.0", MANAGER)
    assert sr.skill_digest(updated) == digest
    assert proposed.lifecycle_state is sr.LifecycleState.PROPOSED
    assert proposed.evidence == ()


def test_promote_creates_new_global_version_and_leaves_source_unchanged():
    registry = sr.SkillRegistry()
    propose_with_accepted_evidence(registry)
    source = registry.activate("commit-msg-check", "1.0.0", MANAGER)
    source_digest = sr.skill_digest(source)

    promoted = registry.promote("commit-msg-check", "1.0.0", "1.1.0", MANAGER)

    # The source record is untouched: same content, digest, and scope.
    original = registry.get("commit-msg-check", "1.0.0")
    assert original.scope is sr.SkillScope.REPOSITORY
    assert original.version == "1.0.0"
    assert sr.skill_digest(original) == source_digest

    # A new, distinct, active global version is registered atomically.
    assert promoted is registry.get("commit-msg-check", "1.1.0")
    assert promoted.scope is sr.SkillScope.GLOBAL
    assert promoted.version == "1.1.0"
    assert promoted.lifecycle_state is sr.LifecycleState.ACTIVE
    assert sr.skill_digest(promoted) != source_digest

    # The digest index still binds the old digest to the old version.
    assert registry._digest_index[source_digest] == ("commit-msg-check", "1.0.0")
    assert registry._digest_index[sr.skill_digest(promoted)] == ("commit-msg-check", "1.1.0")


def test_promote_rejects_version_and_digest_collisions():
    registry = sr.SkillRegistry()
    propose_with_accepted_evidence(registry)
    registry.activate("commit-msg-check", "1.0.0", MANAGER)

    # A distinct proposed record already occupies the target (identity, version).
    registry.propose(base_record(version="1.2.0", stage="pre-edit"), WORKER)
    assert_fails(
        "skill_registry.immutable_identity",
        lambda: registry.promote("commit-msg-check", "1.0.0", "1.2.0", MANAGER),
    )

    # First promotion to a fresh version succeeds.
    registry.promote("commit-msg-check", "1.0.0", "1.1.0", MANAGER)
    # Re-promoting to the same target version reproduces an identical global
    # digest, which is rejected.
    assert_fails(
        "skill_registry.immutable_digest",
        lambda: registry.promote("commit-msg-check", "1.0.0", "1.1.0", MANAGER),
    )
    # In-place promotion (same version) is rejected.
    assert_fails(
        "skill_registry.immutable_identity",
        lambda: registry.promote("commit-msg-check", "1.0.0", "1.0.0", MANAGER),
    )
    # A malformed target version is rejected.
    assert_fails(
        "skill_registry.invalid_version",
        lambda: registry.promote("commit-msg-check", "1.0.0", "not-a-version", MANAGER),
    )


# ---------------------------------------------------------------------------
# Manager-authority gate
# ---------------------------------------------------------------------------


def test_worker_cannot_activate_retire_or_promote():
    registry = sr.SkillRegistry()
    propose_with_accepted_evidence(registry)
    assert_fails(
        "skill_registry.unauthorized",
        lambda: registry.activate("commit-msg-check", "1.0.0", WORKER),
    )
    assert_fails(
        "skill_registry.unauthorized",
        lambda: registry.activate("commit-msg-check", "1.0.0", MANAGER_NO_TOKEN),
    )
    assert_fails(
        "skill_registry.unauthorized",
        lambda: registry.retire("commit-msg-check", "1.0.0", WORKER),
    )
    assert_fails(
        "skill_registry.unauthorized",
        lambda: registry.promote("commit-msg-check", "1.0.0", "1.1.0", WORKER),
    )
    assert_fails(
        "skill_registry.unauthorized",
        lambda: registry.mark_negative_resolved("commit-msg-check", "1.0.0", WORKER),
    )


def test_manager_with_token_can_activate():
    registry = sr.SkillRegistry()
    propose_with_accepted_evidence(registry)
    activated = registry.activate("commit-msg-check", "1.0.0", MANAGER)
    assert activated.lifecycle_state is sr.LifecycleState.ACTIVE


def test_manager_cannot_activate_without_evidence():
    registry = sr.SkillRegistry()
    registry.propose(base_record(), WORKER)
    assert_fails(
        "skill_registry.insufficient_evidence",
        lambda: registry.activate("commit-msg-check", "1.0.0", MANAGER),
    )


def test_activation_requires_proposed_state():
    record = with_evidence(
        base_record(lifecycle_state="active"),
        [{"source": "s1", "outcome": "accepted"}, {"source": "s2", "outcome": "accepted"}],
    )
    decision = sr.can_activate(record, 2)
    assert not decision.allowed
    assert decision.reason_code == "skill_registry.invalid_transition"


def test_promotion_requires_active_repository_skill():
    assert sr.can_promote(base_record(lifecycle_state="active")).allowed
    proposed = sr.can_promote(base_record(lifecycle_state="proposed"))
    assert proposed.reason_code == "skill_registry.invalid_transition"
    global_skill = sr.can_promote(base_record(scope="global", lifecycle_state="active"))
    assert global_skill.reason_code == "skill_registry.invalid_scope"


def test_retire_requires_active_skill():
    assert sr.can_retire(base_record(lifecycle_state="active")).allowed
    proposed = sr.can_retire(base_record(lifecycle_state="proposed"))
    assert proposed.reason_code == "skill_registry.invalid_transition"


def test_promotion_and_retirement_roundtrip():
    registry = sr.SkillRegistry()
    propose_with_accepted_evidence(registry)
    registry.activate("commit-msg-check", "1.0.0", MANAGER)
    promoted = registry.promote("commit-msg-check", "1.0.0", "1.1.0", MANAGER)
    assert promoted.scope is sr.SkillScope.GLOBAL
    assert promoted.version == "1.1.0"
    assert registry.get("commit-msg-check", "1.0.0").scope is sr.SkillScope.REPOSITORY
    retired = registry.retire("commit-msg-check", "1.0.0", MANAGER)
    assert retired.lifecycle_state is sr.LifecycleState.RETIRED


# ---------------------------------------------------------------------------
# Registry iteration and bounded selection
# ---------------------------------------------------------------------------


def test_registry_is_iterable_and_bounded_selection():
    registry = sr.SkillRegistry()
    for identity in ["b", "a", "c"]:
        registry.propose(base_record(identity=identity, task_family="t"), WORKER)
    assert len(registry) == 3
    assert len(registry.records()) == 3
    assert [r.identity for r in sr.rank(registry)] == ["a", "b", "c"]
    assert len(sr.rank(registry, limit=2)) == 2
    assert registry.get("a", "1.0.0") is not None
    assert registry.get("missing", "1.0.0") is None
    assert ("a", "1.0.0") in registry
    assert ("missing", "1.0.0") not in registry


def _active(**overrides):
    return base_record(lifecycle_state="active", **overrides)


def _select_context(**overrides):
    data = {
        "task_family": "commit",
        "path_or_symbol": "src/aiworkhub/skill_registry.py",
        "risk": "medium",
        "stage": "post-edit",
        "triggers": ["commit"],
    }
    data.update(overrides)
    return data


def test_select_positive_active_match():
    record = _active()
    receipt = sr.select([record], _select_context())
    assert len(receipt.selected) == 1
    item = receipt.selected[0]
    assert item.identity == "commit-msg-check"
    assert item.version == "1.0.0"
    assert item.digest == sr.skill_digest(record)
    assert "lifecycle:active" in item.reasons
    assert "task_family:exact" in item.reasons
    assert "path_or_symbol:exact" in item.reasons


def test_select_zero_match_is_empty():
    record = _active(task_family="review")
    receipt = sr.select([record], _select_context())
    assert receipt.selected == ()
    payload = receipt.as_mapping()
    assert set(payload) == {"selected", "context", "context_seal"}
    assert payload["selected"] == []
    assert payload["context"]["task_family"] == "commit"
    assert payload["context"]["path_or_symbol"] == "src/aiworkhub/skill_registry.py"
    assert payload["context_seal"] == sr._receipt_context_seal(receipt.context, receipt.selected)
    assert payload["context_seal"]


def test_select_excludes_proposed_and_retired():
    proposed = base_record(lifecycle_state="proposed")
    retired = base_record(identity="retired-skill", lifecycle_state="retired")
    active = _active(identity="active-skill")
    receipt = sr.select([proposed, retired, active], _select_context())
    assert [item.identity for item in receipt.selected] == ["active-skill"]


def test_select_exact_versus_wildcard():
    exact = _active(identity="exact-path")
    wild = _active(identity="wild-path", path_or_symbol="*")
    prefix = _active(identity="prefix-path", path_or_symbol="src/aiworkhub/*")
    other = _active(identity="other-path", path_or_symbol="src/other.py")
    substr = _active(identity="substr-path", path_or_symbol="skill_registry")
    family_wild = _active(identity="wild-family", task_family="*")
    trigger_wild = _active(identity="wild-trigger", triggers=["*"])
    receipt = sr.select(
        [exact, wild, prefix, other, substr, family_wild, trigger_wild],
        _select_context(),
    )
    assert [item.identity for item in receipt.selected] == [
        "wild-family",
        "wild-path",
        "prefix-path",
        "exact-path",
        "wild-trigger",
    ]
    kinds = {item.identity: item.reasons for item in receipt.selected}
    assert "path_or_symbol:wildcard" in kinds["wild-path"]
    assert "path_or_symbol:wildcard" in kinds["prefix-path"]
    assert "path_or_symbol:exact" in kinds["exact-path"]
    assert "task_family:wildcard" in kinds["wild-family"]
    assert "triggers:wildcard" in kinds["wild-trigger"]


def test_select_rejects_substring_authority():
    record = _active()
    assert sr.select([record], _select_context(task_family="commit-msg")).selected == ()
    assert sr.select([record], _select_context(triggers=["commit-msg"])).selected == ()
    assert sr.select([record], _select_context(path_or_symbol="src/aiworkhub")).selected == ()
    constrained = _active(identity="py-only", applicability=["python"])
    assert sr.select([constrained], _select_context()).selected == ()
    assert [
        item.identity
        for item in sr.select([constrained], _select_context(applicability=["python"])).selected
    ] == ["py-only"]
    assert sr.select([constrained], _select_context(applicability=["rust"])).selected == ()


def test_select_is_order_independent_and_tie_stable():
    alpha = _active(
        identity="alpha", task_family="t", path_or_symbol="p", risk="low", stage="s", triggers=()
    )
    beta = _active(
        identity="beta", task_family="t", path_or_symbol="p", risk="low", stage="s", triggers=()
    )
    ctx = _select_context(task_family="t", path_or_symbol="p", risk="low", stage="s", triggers=())
    first = sr.select([beta, alpha], ctx)
    second = sr.select([alpha, beta], ctx)
    assert [item.identity for item in first.selected] == ["alpha", "beta"]
    assert first.selected == second.selected
    assert first.as_mapping() == second.as_mapping()


def test_select_enforces_positive_limit_and_clamps():
    records = [
        _active(
            identity=f"skill{index:02d}",
            task_family="t",
            path_or_symbol="p",
            risk="low",
            stage="s",
            triggers=(),
        )
        for index in range(5)
    ]
    ctx = _select_context(task_family="t", path_or_symbol="p", risk="low", stage="s", triggers=())
    assert [item.identity for item in sr.select(records, ctx, limit=2).selected] == [
        "skill00",
        "skill01",
    ]
    assert len(sr.select(records, ctx, limit=sr.MAX_SELECT_LIMIT + 5).selected) == 5
    assert_fails("skill_registry.invalid_value", lambda: sr.select(records, ctx, limit=0))
    assert_fails("skill_registry.invalid_value", lambda: sr.select(records, ctx, limit=-1))
    assert_fails("skill_registry.invalid_type", lambda: sr.select(records, ctx, limit=True))
    assert_fails("skill_registry.invalid_type", lambda: sr.select(records, ctx, limit=1.5))
    assert_fails("skill_registry.invalid_type", lambda: sr.select(records, ctx, limit=None))


def test_select_malformed_inputs_fail_closed():
    record = _active()
    ctx = _select_context()
    assert_fails("skill_registry.invalid_type", lambda: sr.select("nope", ctx))
    assert_fails("skill_registry.invalid_type", lambda: sr.select([record, "bad"], ctx))
    assert_fails("skill_registry.invalid_type", lambda: sr.select([record], "nope"))
    assert_fails(
        "skill_registry.unknown_key",
        lambda: sr.select([record], {**ctx, "token": "secret"}),
    )
    assert_fails(
        "skill_registry.missing_field",
        lambda: sr.select([record], {"task_family": "commit"}),
    )
    assert_fails(
        "skill_registry.unsafe_path",
        lambda: sr.select([record], _select_context(path_or_symbol="/etc/passwd")),
    )
    assert_fails(
        "skill_registry.unsafe_path",
        lambda: sr.select([record], _select_context(path_or_symbol="C:\\Windows")),
    )


def test_select_receipt_redacts_bodies_secrets_and_host_paths():
    record = _active(
        procedure_steps=["run /home/secret/tool --token leaked-secret-value"],
        preferred_tools=["/usr/bin/leaked"],
        avoid_rules=["never print leaked-secret-value"],
    )
    receipt = sr.select([record], _select_context())
    payload = sr.canonical_json(receipt.as_mapping())
    text = str(receipt) + payload
    assert "leaked-secret-value" not in text
    assert "/home/secret" not in text
    assert "procedure" not in payload
    row = receipt.as_mapping()["selected"][0]
    assert set(row) == {"identity", "version", "digest", "reasons"}
    assert row["identity"] == "commit-msg-check"
    assert row["digest"] == sr.skill_digest(record)
    assert all(":" in token for token in row["reasons"])


def test_select_does_not_mutate_registry_state():
    registry = sr.SkillRegistry()
    propose_with_accepted_evidence(registry)
    active = registry.activate("commit-msg-check", "1.0.0", MANAGER)
    before = registry.records()
    before_state = (
        active.lifecycle_state,
        active.accepted_count,
        active.negative_count,
        active.evidence,
        sr.skill_digest(active),
    )
    receipt = registry.select(_select_context())
    after = registry.records()
    assert before == after
    assert all(left is right for left, right in zip(before, after))
    current = registry.get("commit-msg-check", "1.0.0")
    assert current is active
    assert (
        current.lifecycle_state,
        current.accepted_count,
        current.negative_count,
        current.evidence,
        sr.skill_digest(current),
    ) == before_state
    assert [item.identity for item in receipt.selected] == ["commit-msg-check"]
    assert sr.select(registry, _select_context()).selected == receipt.selected



def _packet_pair():
    alpha = _active(
        identity="alpha",
        task_family="t",
        path_or_symbol="p",
        risk="low",
        stage="s",
        triggers=(),
        applicability=("repo",),
        procedure_steps=("inspect diff",),
        avoid_rules=("do not rewrite history",),
        preferred_tools=("git",),
    )
    beta = _active(
        identity="beta",
        task_family="t",
        path_or_symbol="p",
        risk="low",
        stage="s",
        triggers=(),
        applicability=("repo",),
        procedure_steps=("inspect status",),
        avoid_rules=("do not rewrite history",),
        preferred_tools=("git",),
    )
    ctx = _select_context(
        task_family="t",
        path_or_symbol="p",
        risk="low",
        stage="s",
        triggers=(),
        applicability=("repo",),
    )
    return alpha, beta, ctx


_PACKET_REASONS = (
    "lifecycle:active",
    "task_family:exact",
    "path_or_symbol:exact",
    "risk:exact",
    "stage:exact",
    "triggers:exact",
    "applicability:unconstrained",
)
_PACKET_EMITTED_FIELDS = (
    "identity",
    "version",
    "digest",
    "reasons",
    "applicability",
    "procedure_steps",
    "avoid_rules",
    "preferred_tools",
)
_PACKET_LIST_FIELDS = (
    "reasons",
    "applicability",
    "procedure_steps",
    "avoid_rules",
    "preferred_tools",
)


def _record_select_context(record):
    return {
        "task_family": record.task_family,
        "path_or_symbol": record.path_or_symbol,
        "risk": record.risk,
        "stage": record.stage,
        "triggers": record.triggers,
        "applicability": record.applicability,
    }


def _bound_receipt(record, reasons=_PACKET_REASONS, context=None):
    if context is None:
        context = _record_select_context(record)
    selected = (
        sr.SkillSelection(
            identity=record.identity,
            version=record.version,
            digest=sr.skill_digest(record),
            reasons=reasons,
        ),
    )
    return sr.SkillSelectionReceipt(
        selected=selected,
        context=context,
        context_seal=sr._receipt_context_seal(context, selected),
    )


def _packet_candidates_and_receipt(field, text):
    if field == "reasons":
        record = _active()
        return [record], _bound_receipt(record, (text,))
    record = _active(**{field: (text,)})
    return [record], _bound_receipt(record)


def test_runtime_packet_positive_and_exact_keys():
    record = _active(
        applicability=("commit-hook",),
        procedure_steps=("check message length",),
        avoid_rules=("do not rewrite history",),
        preferred_tools=("git",),
    )
    record = with_evidence(
        record,
        [{"source": "src-a", "outcome": "accepted", "actor_id": "actor-a"}],
    )
    receipt = sr.select([record], _select_context(applicability=["commit-hook"]))
    packet = sr.build_runtime_packet([record], receipt)
    payload = packet.as_mapping()
    assert packet.version == sr.RUNTIME_PACKET_VERSION
    assert set(payload) == {"version", "skills"}
    assert len(payload["skills"]) == 1
    row = payload["skills"][0]
    assert set(row) == {
        "identity",
        "version",
        "digest",
        "reasons",
        "applicability",
        "procedure_steps",
        "avoid_rules",
        "preferred_tools",
    }
    assert row["identity"] == "commit-msg-check"
    assert row["version"] == "1.0.0"
    assert row["digest"] == sr.skill_digest(record)
    assert row["applicability"] == ["commit-hook"]
    assert row["procedure_steps"] == ["check message length"]
    assert row["avoid_rules"] == ["do not rewrite history"]
    assert row["preferred_tools"] == ["git"]
    text = sr.canonical_json(payload)
    assert "evidence" not in text
    assert "accepted_count" not in text
    assert "negative_count" not in text
    assert "actor-a" not in text
    assert "src/aiworkhub/skill_registry.py" not in text
    assert "lifecycle" not in "".join(row.keys())


def test_runtime_packet_empty_is_truthful_and_versioned():
    record = _active()
    receipt = sr.select([record], _select_context(task_family="other"))
    packet = sr.build_runtime_packet([record], receipt)
    assert packet.skills == ()
    assert packet.as_mapping() == {"version": sr.RUNTIME_PACKET_VERSION, "skills": []}
    empty = sr.build_runtime_packet([], sr.SkillSelectionReceipt())
    assert empty.as_mapping() == packet.as_mapping()


def test_runtime_packet_order_follows_receipt_not_candidates():
    alpha, beta, ctx = _packet_pair()
    receipt = sr.select([beta, alpha], ctx)
    first = sr.build_runtime_packet([beta, alpha], receipt)
    second = sr.build_runtime_packet([alpha, beta], receipt)
    assert [row.identity for row in first.skills] == ["alpha", "beta"]
    assert [row.identity for row in second.skills] == ["alpha", "beta"]
    assert first.as_mapping() == second.as_mapping()
    assert first.skills[0].digest == sr.skill_digest(alpha)
    assert first.skills[1].digest == sr.skill_digest(beta)


def test_runtime_packet_binds_exact_digest():
    record = _active()
    receipt = sr.select([record], _select_context())
    item = receipt.selected[0]
    spoofed = sr.SkillSelectionReceipt(
        selected=(
            sr.SkillSelection(
                identity=item.identity,
                version=item.version,
                digest="0" * 64,
                reasons=item.reasons,
            ),
        )
    )
    assert_fails(
        "skill_registry.digest_mismatch",
        lambda: sr.build_runtime_packet([record], spoofed),
    )


def test_runtime_packet_rejects_lifecycle_duplicate_missing_and_spoof():
    alpha, beta, ctx = _packet_pair()
    receipt = sr.select([alpha, beta], ctx)
    proposed = base_record(
        identity="alpha", task_family="t", path_or_symbol="p", risk="low", stage="s", triggers=()
    )
    retired = base_record(
        identity="alpha",
        task_family="t",
        path_or_symbol="p",
        risk="low",
        stage="s",
        triggers=(),
        lifecycle_state="retired",
    )
    assert_fails(
        "skill_registry.invalid_lifecycle",
        lambda: sr.build_runtime_packet([proposed], _bound_receipt(proposed)),
    )
    assert_fails(
        "skill_registry.invalid_lifecycle",
        lambda: sr.build_runtime_packet([retired], _bound_receipt(retired)),
    )
    assert_fails(
        "skill_registry.duplicate_selection",
        lambda: sr.build_runtime_packet([alpha, alpha], receipt),
    )
    assert_fails(
        "skill_registry.duplicate_selection",
        lambda: sr.build_runtime_packet(
            [alpha],
            sr.SkillSelectionReceipt(selected=(receipt.selected[0], receipt.selected[0])),
        ),
    )
    assert_fails(
        "skill_registry.not_found",
        lambda: sr.build_runtime_packet([beta], receipt),
    )
    spoofed = sr.SkillSelectionReceipt(selected=(receipt.selected[1], receipt.selected[0]))
    assert_fails(
        "skill_registry.selection_spoofed",
        lambda: sr.build_runtime_packet([alpha, beta], spoofed),
    )

    class ExtraReceipt(sr.SkillSelectionReceipt):
        pass

    assert_fails(
        "skill_registry.receipt_extended",
        lambda: sr.build_runtime_packet([alpha, beta], ExtraReceipt(selected=receipt.selected)),
    )
    assert_fails(
        "skill_registry.invalid_type",
        lambda: sr.build_runtime_packet([alpha], receipt.as_mapping()),
    )


def test_runtime_packet_enforces_all_bounds_and_malformed_limits():
    alpha, beta, ctx = _packet_pair()
    receipt = sr.select([alpha, beta], ctx)
    assert_fails(
        "skill_registry.packet_limit",
        lambda: sr.build_runtime_packet([alpha, beta], receipt, max_selected=1),
    )
    assert_fails(
        "skill_registry.packet_limit",
        lambda: sr.build_runtime_packet([alpha, beta], receipt, max_list_items=1),
    )
    assert_fails(
        "skill_registry.packet_limit",
        lambda: sr.build_runtime_packet([alpha, beta], receipt, max_string_bytes=3),
    )
    assert_fails(
        "skill_registry.packet_limit",
        lambda: sr.build_runtime_packet([alpha, beta], receipt, max_packet_bytes=10),
    )
    assert_fails(
        "skill_registry.invalid_type",
        lambda: sr.build_runtime_packet([alpha], receipt, max_selected=True),
    )
    assert_fails(
        "skill_registry.invalid_type",
        lambda: sr.build_runtime_packet([alpha], receipt, max_list_items=1.5),
    )
    assert_fails(
        "skill_registry.invalid_type",
        lambda: sr.build_runtime_packet([alpha], receipt, max_string_bytes=None),
    )
    assert_fails(
        "skill_registry.invalid_value",
        lambda: sr.build_runtime_packet([alpha], receipt, max_packet_bytes=0),
    )
    assert_fails(
        "skill_registry.invalid_value",
        lambda: sr.build_runtime_packet([alpha], receipt, max_selected=-1),
    )


def test_runtime_packet_rejects_secrets_paths_and_controls():
    cases = (
        ("skill_registry.secret_rejected", {"procedure_steps": ["Authorization: Bearer leakedtoken"]}),
        ("skill_registry.secret_rejected", {"procedure_steps": ["Bearer leakedtoken"]}),
        ("skill_registry.secret_rejected", {"avoid_rules": ["-----BEGIN PRIVATE KEY-----"]}),
        ("skill_registry.secret_rejected", {"preferred_tools": ["token=leakedvalue"]}),
        ("skill_registry.secret_rejected", {"applicability": ["secret=leakedvalue"]}),
        ("skill_registry.unsafe_path", {"procedure_steps": ["run /usr/bin/tool"]}),
        ("skill_registry.unsafe_path", {"preferred_tools": [r"C:\Windows\tool.exe"]}),
        ("skill_registry.invalid_value", {"avoid_rules": ["do not use \x00 null"]}),
        ("skill_registry.invalid_value", {"procedure_steps": ["line\nbreak"]}),
    )
    for code, overrides in cases:
        record = _active(**overrides)
        assert_fails(code, lambda record=record: sr.build_runtime_packet([record], _bound_receipt(record)))


def test_runtime_packet_rejects_prefixed_secrets_and_file_uris_on_every_field():
    payloads = (
        ("skill_registry.secret_rejected", "OPENAI_API_KEY=supersecret"),
        ("skill_registry.secret_rejected", "AWS_SECRET_ACCESS_KEY=supersecret"),
        ("skill_registry.secret_rejected", "GH_TOKEN=supersecret"),
        ("skill_registry.secret_rejected", "DB_PASSWORD=supersecret"),
        ("skill_registry.unsafe_path", "file:///etc/passwd"),
        ("skill_registry.unsafe_path", "FILE://localhost/etc/passwd"),
        ("skill_registry.unsafe_path", r"\\server\share\secret"),
        ("skill_registry.unsafe_path", r"\\.\pipe\secret"),
        ("skill_registry.unsafe_path", r"\\?\C:\Windows\system32"),
        ("skill_registry.unsafe_path", "//./COM1"),
        ("skill_registry.unsafe_path", "//?/C:/Windows"),
    )
    for field in _PACKET_EMITTED_FIELDS:
        for code, text in payloads:
            if field in _PACKET_LIST_FIELDS:
                candidates, receipt = _packet_candidates_and_receipt(field, text)
                assert_fails(
                    code,
                    lambda candidates=candidates, receipt=receipt: sr.build_runtime_packet(
                        candidates, receipt
                    ),
                )
            else:
                assert_fails(
                    code,
                    lambda field=field, text=text: sr._bounded_packet_scalar(text, field, 256),
                )
    record = _active(procedure_steps=("set mode=strict", "retries=3"))
    packet = sr.build_runtime_packet([record], _bound_receipt(record))
    assert packet.skills[0].procedure_steps == ("set mode=strict", "retries=3")


def test_runtime_packet_rejects_secret_assign_stem_swallow_bypasses():
    cases = (
        "SECRET_KEY=leakedvalue",
        "secret_key=leakedvalue",
        "export SECRET_KEY=leakedvalue",
        "token_id=leakedvalue",
        "password_hash=leakedvalue",
    )
    for text in cases:
        record = _active(procedure_steps=(text,))
        assert_fails(
            "skill_registry.secret_rejected",
            lambda record=record: sr.build_runtime_packet([record], _bound_receipt(record)),
        )


def test_runtime_packet_rejects_structural_assignment_secret_keys():
    payloads = (
        "SecretKey=leakedvalue",
        "secretKey=leakedvalue",
        "secrets=leakedvalue",
        "tokenId=leakedvalue",
        "tokenValue=leakedvalue",
        "passwordHash=leakedvalue",
    )
    for field in _PACKET_EMITTED_FIELDS:
        for text in payloads:
            if field in _PACKET_LIST_FIELDS:
                candidates, receipt = _packet_candidates_and_receipt(field, text)
                assert_fails(
                    "skill_registry.secret_rejected",
                    lambda candidates=candidates, receipt=receipt: sr.build_runtime_packet(
                        candidates, receipt
                    ),
                )
            else:
                assert_fails(
                    "skill_registry.secret_rejected",
                    lambda field=field, text=text: sr._bounded_packet_scalar(text, field, 256),
                )
    allowed = _active(
        procedure_steps=(
            "mention tokenId as a field name",
            "set mode=strict",
            "retries=3",
        )
    )
    packet = sr.build_runtime_packet([allowed], _bound_receipt(allowed))
    assert packet.skills[0].procedure_steps == (
        "mention tokenId as a field name",
        "set mode=strict",
        "retries=3",
    )


def test_runtime_packet_rejects_fused_lowercase_secret_compounds():
    assert sr._has_secret_assignment("apikey=leak")
    assert sr._has_secret_assignment("accesskey=leak")
    assert sr._has_secret_assignment("privatekey=leak")
    assert sr._has_secret_assignment("secretkey=leak")
    assert sr._has_secret_assignment("authtoken=leak")
    assert sr._has_secret_assignment("apiKey=leak")
    assert sr._has_secret_assignment("accessKey=leak")
    assert sr._has_secret_assignment("privateKey=leak")
    assert sr._has_secret_assignment("tokenid=leak")
    assert sr._has_secret_assignment("tokenvalue=leak")
    assert sr._has_secret_assignment("passwordhash=leak")
    assert sr._has_secret_assignment("secretaccesskey=leak")
    assert sr._has_secret_assignment("awssecretaccesskey=leak")
    assert not sr._has_secret_assignment("monkey=banana")
    assert not sr._has_secret_assignment("keyboard=qwerty")
    assert not sr._has_secret_assignment("hockey=puck")
    assert not sr._has_secret_assignment("tokenizer=bert")
    assert not sr._has_secret_assignment("mode=strict")
    assert not sr._has_secret_assignment("mention apikey in docs")
    rejected = (
        "apikey=leak",
        "accesskey=leak",
        "privatekey=leak",
        "secretkey=leak",
        "authkey=leak",
        "authtoken=leak",
        "accesstoken=leak",
        "apitoken=leak",
        "bearertoken=leak",
        "clientsecret=leak",
        "apisecret=leak",
        "apiKey=leak",
        "accessKey=leak",
        "privateKey=leak",
        "secretKey=leak",
        "authToken=leak",
        "api_key=leak",
        "access_key=leak",
        "private_key=leak",
        "secret_key=leak",
        "auth_token=leak",
        "api.key=leak",
        "access.key=leak",
        "private.key=leak",
        "api-key=leak",
        "access-key=leak",
        "private-key=leak",
        "auth-token=leak",
        "tokenid=leak",
        "tokenvalue=leak",
        "passwordhash=leak",
        "secretaccesskey=leak",
        "awssecretaccesskey=leak",
    )
    for field in _PACKET_EMITTED_FIELDS:
        for text in rejected:
            if field in _PACKET_LIST_FIELDS:
                candidates, receipt = _packet_candidates_and_receipt(field, text)
                assert_fails(
                    "skill_registry.secret_rejected",
                    lambda candidates=candidates, receipt=receipt: sr.build_runtime_packet(
                        candidates, receipt
                    ),
                )
            else:
                assert_fails(
                    "skill_registry.secret_rejected",
                    lambda field=field, text=text: sr._bounded_packet_scalar(text, field, 256),
                )
    allowed = _active(
        procedure_steps=(
            "monkey=banana",
            "keyboard=qwerty",
            "hockey=puck",
            "tokenizer=bert",
            "mode=strict",
            "mention apikey in docs",
            "discuss accesskey naming",
            "privatekey is a field name",
        )
    )
    packet = sr.build_runtime_packet([allowed], _bound_receipt(allowed))
    assert packet.skills[0].procedure_steps == (
        "monkey=banana",
        "keyboard=qwerty",
        "hockey=puck",
        "tokenizer=bert",
        "mode=strict",
        "mention apikey in docs",
        "discuss accesskey naming",
        "privatekey is a field name",
    )


def _secret_family_surface_keys() -> tuple[str, ...]:
    families = (
        ("api", "key"),
        ("access", "key"),
        ("access", "id"),
        ("private", "key"),
        ("secret", "key"),
        ("secret", "access", "key"),
        ("auth", "token"),
        ("auth", "key"),
        ("bearer", "token"),
        ("client", "secret"),
        ("token", "id"),
        ("token", "value"),
        ("password", "hash"),
        ("credential",),
        ("client", "credential"),
        ("aws", "secret", "access", "key"),
        ("aws", "access", "key"),
        ("aws", "access", "key", "id"),
    )
    keys: list[str] = []
    for parts in families:
        fused = "".join(parts)
        camel = parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])
        pascal = "".join(part[:1].upper() + part[1:] for part in parts)
        keys.extend(
            (
                fused,
                camel,
                pascal,
                "_".join(parts),
                ".".join(parts),
                "-".join(parts),
            )
        )
    return tuple(keys)


def test_runtime_packet_rejects_secret_key_family_surfaces():
    for key in _secret_family_surface_keys():
        text = f"{key}=leak"
        assert sr._has_secret_assignment(text), text
        for field in _PACKET_EMITTED_FIELDS:
            if field in _PACKET_LIST_FIELDS:
                candidates, receipt = _packet_candidates_and_receipt(field, text)
                assert_fails(
                    "skill_registry.secret_rejected",
                    lambda candidates=candidates, receipt=receipt: sr.build_runtime_packet(
                        candidates, receipt
                    ),
                )
            else:
                assert_fails(
                    "skill_registry.secret_rejected",
                    lambda field=field, text=text: sr._bounded_packet_scalar(text, field, 256),
                )
    benign = (
        "monkey=banana",
        "keyboard=qwerty",
        "hockey=puck",
        "tokenizer=bert",
        "mode=strict",
        "mention tokenId as a field name",
        "mention apikey in docs",
    )
    for text in benign:
        assert not sr._has_secret_assignment(text), text
    allowed = _active(procedure_steps=benign)
    packet = sr.build_runtime_packet([allowed], _bound_receipt(allowed))
    assert packet.skills[0].procedure_steps == benign


def test_runtime_packet_rejects_bearer_colon_form():
    record = _active(procedure_steps=("Bearer: leakedtoken",))
    assert_fails(
        "skill_registry.secret_rejected",
        lambda: sr.build_runtime_packet([record], _bound_receipt(record)),
    )


def test_runtime_packet_rejects_bearer_assignment_forms():
    payloads = (
        "bearer=leakedtoken",
        "Bearer=leakedtoken",
        "BEARER=leakedtoken",
    )
    for field in _PACKET_EMITTED_FIELDS:
        for text in payloads:
            if field in _PACKET_LIST_FIELDS:
                candidates, receipt = _packet_candidates_and_receipt(field, text)
                assert_fails(
                    "skill_registry.secret_rejected",
                    lambda candidates=candidates, receipt=receipt: sr.build_runtime_packet(
                        candidates, receipt
                    ),
                )
            else:
                assert_fails(
                    "skill_registry.secret_rejected",
                    lambda field=field, text=text: sr._bounded_packet_scalar(text, field, 256),
                )


def test_runtime_packet_rejects_pgp_private_key_block():
    payloads = (
        "-----BEGIN PGP PRIVATE KEY BLOCK-----",
        "-----begin pgp private key block-----",
    )
    for text in payloads:
        for field in _PACKET_LIST_FIELDS:
            candidates, receipt = _packet_candidates_and_receipt(field, text)
            assert_fails(
                "skill_registry.secret_rejected",
                lambda candidates=candidates, receipt=receipt: sr.build_runtime_packet(
                    candidates, receipt
                ),
            )


def test_runtime_packet_rejects_assignment_style_private_and_cloud_keys():
    cases = (
        "private_key=MIIEowIBAAKCAQEA",
        "secret.key=leakedvalue",
        "aws_access_key_id=AKIALEAKED",
        "AWS_ACCESS_KEY_ID=AKIALEAKED",
    )
    for text in cases:
        for field in _PACKET_LIST_FIELDS:
            candidates, receipt = _packet_candidates_and_receipt(field, text)
            assert_fails(
                "skill_registry.secret_rejected",
                lambda candidates=candidates, receipt=receipt: sr.build_runtime_packet(
                    candidates, receipt
                ),
            )
    allowed = _active(
        procedure_steps=(
            "rotate credentials without embedding values",
            "mention aws_access_key_id only as a field name",
            "never paste a private_key block",
            "secret.key names are not credentials",
        )
    )
    packet = sr.build_runtime_packet([allowed], _bound_receipt(allowed))
    assert packet.skills[0].procedure_steps == allowed.procedure_steps


def test_select_to_build_rejects_duplicate_identity_version_candidates():
    record = _active()
    duplicate = _active()
    candidates = [record, duplicate]
    assert_fails(
        "skill_registry.duplicate_selection",
        lambda: sr.select(candidates, _select_context()),
    )
    receipt = sr.select([record], _select_context())
    assert_fails(
        "skill_registry.duplicate_selection",
        lambda: sr.build_runtime_packet(candidates, receipt),
    )


def test_runtime_packet_rejects_windows_root_relative_path_not_relative_tools():
    record = _active(procedure_steps=(r"\Windows\system32\cmd.exe",))
    assert_fails(
        "skill_registry.unsafe_path",
        lambda: sr.build_runtime_packet([record], _bound_receipt(record)),
    )
    allowed = _active(preferred_tools=("git", "rg", r"tools\rg"))
    packet = sr.build_runtime_packet([allowed], _bound_receipt(allowed))
    assert packet.skills[0].preferred_tools == ("git", "rg", r"tools\rg")


def test_runtime_packet_rejects_punctuated_and_encoded_posix_paths():
    payloads = (
        "/'etc/passwd",
        '/"etc/passwd',
        "/%2e%2e/etc/passwd",
    )
    for field in _PACKET_EMITTED_FIELDS:
        for text in payloads:
            if field in _PACKET_LIST_FIELDS:
                candidates, receipt = _packet_candidates_and_receipt(field, text)
                assert_fails(
                    "skill_registry.unsafe_path",
                    lambda candidates=candidates, receipt=receipt: sr.build_runtime_packet(
                        candidates, receipt
                    ),
                )
            else:
                assert_fails(
                    "skill_registry.unsafe_path",
                    lambda field=field, text=text: sr._bounded_packet_scalar(text, field, 256),
                )
    allowed = _active(
        procedure_steps=("docs/'notes.md'", 'refs/"safe"', "refs/%2e%2e/safe"),
        preferred_tools=("git", "tools/rg"),
    )
    packet = sr.build_runtime_packet([allowed], _bound_receipt(allowed))
    assert packet.skills[0].procedure_steps == allowed.procedure_steps
    assert packet.skills[0].preferred_tools == allowed.preferred_tools


@pytest.mark.parametrize(
    ("text", "rejected"),
    (
        ("/'etc/passwd", True),
        ('/"etc/passwd', True),
        ("/%2e%2e/etc/passwd", True),
        (r"\Windows\system32\cmd.exe", True),
        ("/@etc/passwd", True),
        ("/$HOME/.ssh", True),
        (r"\@Windows\system32", True),
        ("tools/rg", False),
        (r"tools\rg", False),
        ("use git/rg and docs/notes", False),
    ),
)
def test_runtime_packet_generalized_absolute_path_matrix(text, rejected):
    assert bool(sr._INSTRUCTION_ABS_PATH_RE.search(text)) is rejected
    record = _active(procedure_steps=(text,))
    if rejected:
        assert_fails(
            "skill_registry.unsafe_path",
            lambda: sr.build_runtime_packet([record], _bound_receipt(record)),
        )
        return
    packet = sr.build_runtime_packet([record], _bound_receipt(record))
    assert packet.skills[0].procedure_steps == (text,)


def test_runtime_packet_rejects_invalid_reason_grammar():
    record = _active()
    valid = _PACKET_REASONS
    packet = sr.build_runtime_packet([record], _bound_receipt(record, valid))
    assert packet.skills[0].reasons == valid
    cases = (
        ("please follow these arbitrary instructions",),
        valid[:1],
        valid[:4],
        valid + ("task_family:exact",),
        (valid[0], valid[2], valid[1], *valid[3:]),
        (*valid[:2], "unknown_field:exact", *valid[3:]),
        (*valid[:1], "task_family:fuzzy", *valid[2:]),
        (*valid[:5], "triggers:maybe", valid[6]),
        ("lifecycle:retired", *valid[1:]),
        ("lifecycle:active", "task_family:exact", "task_family:exact", *valid[3:]),
    )
    for reasons in cases:
        assert_fails(
            "skill_registry.invalid_value",
            lambda reasons=reasons: sr.build_runtime_packet([record], _bound_receipt(record, reasons)),
        )


def test_runtime_packet_rejects_quoted_secrets_punctuated_paths_controls_and_forged_reasons():
    payloads = (
        ("skill_registry.secret_rejected", '{"api_key":"leakedvalue"}'),
        ("skill_registry.secret_rejected", '{"api_key": "leakedvalue"}'),
        ("skill_registry.secret_rejected", "'api_key': 'leakedvalue'"),
        ("skill_registry.unsafe_path", "`/etc/passwd`"),
        ("skill_registry.unsafe_path", "(/tmp/secret)"),
        ("skill_registry.unsafe_path", r"[C:\Windows\system32]"),
        ("skill_registry.unsafe_path", "`file:///etc/passwd`"),
        ("skill_registry.invalid_value", "line\u2028break"),
        ("skill_registry.invalid_value", "line\u2029break"),
        ("skill_registry.invalid_value", "c1\u0085next"),
        ("skill_registry.invalid_value", "bidi\u202eoverride"),
        ("skill_registry.invalid_value", "bidi\u2066isolate"),
    )
    for field in _PACKET_EMITTED_FIELDS:
        for code, text in payloads:
            if field in _PACKET_LIST_FIELDS:
                candidates, receipt = _packet_candidates_and_receipt(field, text)
                assert_fails(
                    code,
                    lambda candidates=candidates, receipt=receipt: sr.build_runtime_packet(
                        candidates, receipt
                    ),
                )
            else:
                assert_fails(
                    code,
                    lambda field=field, text=text: sr._bounded_packet_scalar(text, field, 256),
                )
    record = _active()
    forged = (
        "lifecycle:active",
        "task_family:wildcard",
        "path_or_symbol:wildcard",
        "risk:wildcard",
        "stage:wildcard",
        "triggers:wildcard",
        "applicability:wildcard",
    )
    assert_fails(
        "skill_registry.invalid_value",
        lambda: sr.build_runtime_packet([record], _bound_receipt(record, forged)),
    )
    mixed = (*_PACKET_REASONS[:1], "task_family:wildcard", *_PACKET_REASONS[2:])
    assert_fails(
        "skill_registry.invalid_value",
        lambda: sr.build_runtime_packet([record], _bound_receipt(record, mixed)),
    )


def test_runtime_packet_rejects_camelcase_secret_access_key_assignments():
    payloads = (
        '{"secretAccessKey":"leakedvalue"}',
        '{"awsSecretAccessKey":"leakedvalue"}',
    )
    for text in payloads:
        record = _active(procedure_steps=(text,))
        assert_fails(
            "skill_registry.secret_rejected",
            lambda record=record: sr.build_runtime_packet([record], _bound_receipt(record)),
        )


def test_runtime_packet_rejects_format_char_split_secret_tokens():
    payloads = (
        "pass\u200bword=leakedvalue",
        "Bea\u200brer leakedtoken",
        "-----BEGIN PRI\u200bVATE KEY-----",
        "pass\u00adword=leakedvalue",
        "Bea\u00adrer leakedtoken",
        "-----BEGIN PRI\u00adVATE KEY-----",
        "pass\u200cword=leakedvalue",
        "pass\u200dword=leakedvalue",
        "pass\u2060word=leakedvalue",
        "pass\ufeffword=leakedvalue",
    )
    for field in _PACKET_EMITTED_FIELDS:
        for text in payloads:
            if field in _PACKET_LIST_FIELDS:
                candidates, receipt = _packet_candidates_and_receipt(field, text)
                assert_fails(
                    "skill_registry.invalid_value",
                    lambda candidates=candidates, receipt=receipt: sr.build_runtime_packet(
                        candidates, receipt
                    ),
                )
            else:
                assert_fails(
                    "skill_registry.invalid_value",
                    lambda field=field, text=text: sr._bounded_packet_scalar(text, field, 256),
                )


def test_runtime_packet_rejects_unicode_whitespace_and_fullwidth_assignment_delimiters():
    payloads = (
        "password\u00a0=leakedvalue",
        "password=\u00a0leakedvalue",
        "api_key\uff1aleakedvalue",
    )
    for text in payloads:
        assert sr._has_secret_assignment(text), text
    for field in _PACKET_EMITTED_FIELDS:
        for text in payloads:
            if field in _PACKET_LIST_FIELDS:
                candidates, receipt = _packet_candidates_and_receipt(field, text)
                assert_fails(
                    "skill_registry.secret_rejected",
                    lambda candidates=candidates, receipt=receipt: sr.build_runtime_packet(
                        candidates, receipt
                    ),
                )
            else:
                assert_fails(
                    "skill_registry.secret_rejected",
                    lambda field=field, text=text: sr._bounded_packet_scalar(text, field, 256),
                )


def test_runtime_packet_canonical_bytes_are_stable():
    alpha, beta, ctx = _packet_pair()
    receipt_beta_first = sr.select([beta, alpha], ctx)
    receipt_alpha_first = sr.select([alpha, beta], ctx)
    from_beta_first = sr.build_runtime_packet([beta, alpha], receipt_beta_first)
    from_alpha_first = sr.build_runtime_packet([alpha, beta], receipt_alpha_first)
    expected_reasons = [
        "lifecycle:active",
        "task_family:exact",
        "path_or_symbol:exact",
        "risk:exact",
        "stage:exact",
        "triggers:unconstrained",
        "applicability:exact",
    ]

    def _expected_row(record, procedure_steps):
        return {
            "preferred_tools": ["git"],
            "avoid_rules": ["do not rewrite history"],
            "procedure_steps": list(procedure_steps),
            "applicability": ["repo"],
            "reasons": list(expected_reasons),
            "digest": sr.skill_digest(record),
            "version": record.version,
            "identity": record.identity,
        }

    expected_skills_first_order = {
        "skills": [
            _expected_row(alpha, ("inspect diff",)),
            _expected_row(beta, ("inspect status",)),
        ],
        "version": sr.RUNTIME_PACKET_VERSION,
    }
    expected_skills_second_order = {
        "version": sr.RUNTIME_PACKET_VERSION,
        "skills": [
            {
                "identity": alpha.identity,
                "version": alpha.version,
                "digest": sr.skill_digest(alpha),
                "reasons": list(expected_reasons),
                "applicability": ["repo"],
                "procedure_steps": ["inspect diff"],
                "avoid_rules": ["do not rewrite history"],
                "preferred_tools": ["git"],
            },
            {
                "identity": beta.identity,
                "version": beta.version,
                "digest": sr.skill_digest(beta),
                "reasons": list(expected_reasons),
                "applicability": ["repo"],
                "procedure_steps": ["inspect status"],
                "avoid_rules": ["do not rewrite history"],
                "preferred_tools": ["git"],
            },
        ],
    }
    expected_bytes = json.dumps(
        expected_skills_first_order, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    reordered_bytes = json.dumps(
        expected_skills_second_order, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    assert expected_bytes == reordered_bytes
    assert sr.canonical_json(from_beta_first).encode("utf-8") == expected_bytes
    assert sr.canonical_json(from_alpha_first).encode("utf-8") == expected_bytes
    assert sr.canonical_json(from_beta_first.as_mapping()).encode("utf-8") == expected_bytes


def test_runtime_packet_does_not_mutate_inputs_or_registry():
    registry = sr.SkillRegistry()
    propose_with_accepted_evidence(registry)
    active = registry.activate("commit-msg-check", "1.0.0", MANAGER)
    receipt = registry.select(_select_context())
    before_records = registry.records()
    before_selected = receipt.selected
    before_steps = active.procedure_steps
    packet = sr.build_runtime_packet(registry, receipt)
    again = registry.build_runtime_packet(receipt)
    assert registry.records() == before_records
    assert all(left is right for left, right in zip(registry.records(), before_records))
    assert receipt.selected is before_selected
    assert active.procedure_steps is before_steps
    assert registry.get("commit-msg-check", "1.0.0") is active
    assert packet.as_mapping() == again.as_mapping()
    assert [row.identity for row in packet.skills] == ["commit-msg-check"]


def test_runtime_packet_round_trips_select_reason_kinds():
    exact = _active()
    exact_receipt = sr.select([exact], _select_context())
    exact_packet = sr.build_runtime_packet([exact], exact_receipt)
    assert "task_family:exact" in exact_packet.skills[0].reasons
    assert "path_or_symbol:exact" in exact_packet.skills[0].reasons
    assert exact_packet.skills[0].reasons == exact_receipt.selected[0].reasons

    wild = _active()
    wild_receipt = sr.select([wild], _select_context(task_family="*"))
    assert "task_family:wildcard" in wild_receipt.selected[0].reasons
    wild_packet = sr.build_runtime_packet([wild], wild_receipt)
    assert wild_packet.skills[0].reasons == wild_receipt.selected[0].reasons

    prefix = _active(path_or_symbol="src/aiworkhub/*")
    prefix_receipt = sr.select([prefix], _select_context())
    assert "path_or_symbol:wildcard" in prefix_receipt.selected[0].reasons
    prefix_packet = sr.build_runtime_packet([prefix], prefix_receipt)
    assert prefix_packet.skills[0].reasons == prefix_receipt.selected[0].reasons

    open_record = _active(triggers=(), applicability=())
    open_receipt = sr.select([open_record], _select_context(triggers=(), applicability=()))
    assert "triggers:unconstrained" in open_receipt.selected[0].reasons
    assert "applicability:unconstrained" in open_receipt.selected[0].reasons
    open_packet = sr.build_runtime_packet([open_record], open_receipt)
    assert open_packet.skills[0].reasons == open_receipt.selected[0].reasons


def test_runtime_packet_rejects_mutated_reasons_or_bound_context():
    record = _active()
    receipt = sr.select([record], _select_context(task_family="*"))
    item = receipt.selected[0]
    assert item.digest == sr.skill_digest(record)
    assert "task_family:wildcard" in item.reasons
    mixed = (*item.reasons[:1], "task_family:exact", *item.reasons[2:])
    mutated_reasons = sr.replace(receipt, selected=(sr.replace(item, reasons=mixed),))
    assert mutated_reasons.selected[0].digest == item.digest
    assert_fails(
        "skill_registry.invalid_value",
        lambda: sr.build_runtime_packet([record], mutated_reasons),
    )
    mutated_context = sr.replace(
        receipt,
        context={**dict(receipt.context), "task_family": record.task_family},
    )
    assert mutated_context.selected[0].digest == item.digest
    assert_fails(
        "skill_registry.invalid_value",
        lambda: sr.build_runtime_packet([record], mutated_context),
    )
    forged = sr.replace(
        receipt,
        context={**dict(receipt.context), "task_family": record.task_family},
        selected=(sr.replace(item, reasons=mixed),),
    )
    assert forged.selected[0].digest == item.digest
    assert forged.context_seal == receipt.context_seal
    assert_fails(
        "skill_registry.invalid_value",
        lambda: sr.build_runtime_packet([record], forged),
    )
    assert_fails(
        "skill_registry.invalid_value",
        lambda: sr.build_runtime_packet(
            [record], sr.replace(receipt, context_seal="0" * 64)
        ),
    )
    assert_fails(
        "skill_registry.invalid_value",
        lambda: sr.build_runtime_packet([record], sr.replace(receipt, context_seal="")),
    )
    assert_fails(
        "skill_registry.invalid_value",
        lambda: sr.build_runtime_packet(
            [record], sr.replace(receipt, context_seal=receipt.context_seal + "ff")
        ),
    )
    assert_fails(
        "skill_registry.receipt_extended",
        lambda: sr.build_runtime_packet(
            [record],
            sr.replace(
                receipt,
                context={**dict(receipt.context), "context_seal": receipt.context_seal},
            ),
        ),
    )
    manual = sr.SkillSelectionReceipt(selected=receipt.selected, context=receipt.context)
    assert manual.context_seal == ""
    assert_fails(
        "skill_registry.invalid_value",
        lambda: sr.build_runtime_packet([record], manual),
    )
    empty = sr.SkillSelectionReceipt()
    assert empty.as_mapping() == {"selected": [], "context": {}, "context_seal": ""}
    assert sr.build_runtime_packet([], empty).skills == ()
    with pytest.raises(TypeError):
        empty.context["task_family"] = "commit"


def test_selection_receipt_empty_context_is_immutable():
    empty = sr.SkillSelectionReceipt()
    assert empty.as_mapping() == {"selected": [], "context": {}, "context_seal": ""}
    assert not isinstance(empty.context, dict)
    with pytest.raises(TypeError):
        empty.context["task_family"] = "commit"


def test_propose_requires_proposed_state():
    registry = sr.SkillRegistry()
    assert_fails(
        "skill_registry.invalid_transition",
        lambda: registry.propose(base_record(lifecycle_state="active"), WORKER),
    )


def test_propose_rejects_prepopulated_evidence_and_counters():
    registry = sr.SkillRegistry()
    forged = with_evidence(
        base_record(),
        [{"source": "s1", "outcome": "accepted", "actor_id": "actor-a"}],
    )
    assert_fails(
        "skill_registry.proposal_evidence_forbidden",
        lambda: registry.propose(forged, WORKER),
    )
    assert_fails(
        "skill_registry.counter_mismatch",
        lambda: registry.propose(sr.replace(base_record(), accepted_count=1), WORKER),
    )
    assert registry.propose(base_record(), WORKER).evidence == ()


# ---------------------------------------------------------------------------
# Fail-closed validation of crafted (directly constructed) records
# ---------------------------------------------------------------------------


def _bad_record(**overrides):
    fields = dict(
        identity="ok-skill",
        version="1.0.0",
        scope=sr.SkillScope.REPOSITORY,
        task_family="t",
        path_or_symbol="p",
        risk=sr.RiskLevel.LOW,
        stage="s",
        triggers=("x",),
        confidence=0.5,
    )
    fields.update(overrides)
    return sr.SkillRecord(**fields)


def test_crafted_invalid_record_fails_closed_at_public_boundaries():
    bad_identity = _bad_record(identity="Bad")
    assert_fails("skill_registry.invalid_identity", lambda: sr.can_activate(bad_identity, 2))
    assert_fails("skill_registry.invalid_identity", lambda: sr.skill_digest(bad_identity))
    assert_fails("skill_registry.invalid_identity", lambda: sr.rank([bad_identity]))

    bad_confidence = _bad_record(confidence=float("nan"))
    assert_fails("skill_registry.invalid_confidence", lambda: sr.skill_digest(bad_confidence))
    assert_fails("skill_registry.invalid_confidence", lambda: sr.can_promote(bad_confidence))

    bad_risk = _bad_record(risk="garbage")
    assert_fails("skill_registry.invalid_risk", lambda: sr.rank([bad_risk]))

    bad_triggers = _bad_record(triggers="not-a-tuple")
    assert_fails("skill_registry.invalid_type", lambda: sr.can_retire(bad_triggers))

    bad_evidence = _bad_record(evidence=[{"source": "s"}])
    assert_fails(
        "skill_registry.invalid_evidence",
        lambda: sr.unresolved_negative_evidence(bad_evidence),
    )


@pytest.mark.parametrize(
    "field",
    ["triggers", "applicability", "procedure_steps", "avoid_rules", "preferred_tools"],
)
def test_crafted_string_container_fails_closed_as_invalid_type(field):
    # A crafted malformed container (a str instead of a tuple) must not be
    # silently coerced into a list of characters by validate_record; the
    # dedicated tuple validator owns type checking and reports invalid_type,
    # never a downstream contradictory_triggers.
    record = _bad_record(**{field: "not-a-tuple"})
    assert_fails("skill_registry.invalid_type", lambda: sr.validate_record(record))
    assert_fails("skill_registry.invalid_type", lambda: sr.skill_digest(record))


def test_crafted_string_evidence_fails_closed_as_invalid_type():
    # A crafted evidence field that is a str (instead of a tuple) is rejected as
    # invalid_type by the evidence coercer rather than being char-split.
    record = _bad_record(evidence="not-a-list")
    assert_fails("skill_registry.invalid_type", lambda: sr.validate_record(record))
    assert_fails("skill_registry.invalid_type", lambda: sr.skill_digest(record))
