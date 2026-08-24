"""Tests for the standalone, typed, immutable Repository Skill Registry foundation.

These tests cover the acceptance criteria for RM-2026-00021: deterministic
identity/version/digest, fail-closed validation, bounded deterministic ranking,
recurrence thresholding, negative safety evidence, immutability of historical
versions, and the manager-authority gate.
"""

from __future__ import annotations

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
