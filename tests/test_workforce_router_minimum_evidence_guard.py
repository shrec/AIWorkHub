"""Minimum-evidence guard and sort-order contract for workforce_router.

Before this file nothing in tests/ referenced ``_candidate_sort_key``, so the
order that decides which model does the work was unasserted.  Two defects were
reachable through that gap:

* ``OutcomeEvidence.normalized()`` displaced the conservative prior whenever an
  observed value was ``not None``, never consulting ``sample_count`` -- so a
  worker with a 2-for-2 record outranked every worker held at the prior.
* ``sample_count`` appeared nowhere in the sort tuple, so p50 latency was the
  first key able to separate candidates whose outcome evidence was thin, and a
  fast producer of unusable output won by being fast.

Each test below pins one half of the repair and fails if the guard, the
constant, or the key order is moved.
"""

from __future__ import annotations

import pytest

from aiworkhub.workforce_router import (
    CONSERVATIVE_PRIORS,
    MIN_OUTCOME_SAMPLES,
    OUTCOME_PRIOR_STRENGTH,
    SAMPLE_GATED_EVIDENCE_FIELDS,
    OutcomeEvidence,
    TaskRequirements,
    WorkerCapability,
    _candidate_sort_key,
    rank_workforce,
    select_worker,
)


def _task(**overrides):
    values = {
        "task_id": "TASK_EVIDENCE_GUARD",
        "repo_id": "repo-alpha",
        "kinds": {"code"},
        "risk": "medium",
        "context_tokens": 0,
        "tool_needs": {"filesystem"},
        "quality_floor": 0.0,
    }
    values.update(overrides)
    return TaskRequirements.build(**values)


def _worker(
    worker_id: str,
    *,
    model: str | None = None,
    provider: str = "openai",
    adapter_id: str = "codex_cli",
    sample_count: int = 0,
    accepted: float | None = None,
    review_ready: float | None = None,
    validation_failure: float | None = None,
    p50: float | None = None,
    p95: float | None = None,
    cost: float | None = None,
    tool_discipline: float | None = None,
    manager_score_adjustment: float = 0.0,
):
    return WorkerCapability.build(
        worker_id=worker_id,
        adapter_id=adapter_id,
        model=model or worker_id,
        provider=provider,
        supports={"code", "review", "mechanical"},
        tools={"filesystem"},
        max_context_tokens=160_000,
        max_risk="high",
        quality_ceiling=1.0,
        available=True,
        credential_ok=True,
        quota_available=True,
        manager_score_adjustment=manager_score_adjustment,
        evidence=OutcomeEvidence(
            accepted_rate=accepted,
            review_ready_rate=review_ready,
            validation_failure_rate=validation_failure,
            p50_latency_seconds=p50,
            p95_latency_seconds=p95,
            cost_usd_per_1k_tokens=cost,
            estimated_tokens=80_000,
            tool_discipline_score=tool_discipline,
            sample_count=sample_count,
        ),
    )


def _components(worker: WorkerCapability, task=None):
    decision = rank_workforce(task or _task(), [worker])
    return decision.candidates[0].score_components


# --------------------------------------------------------------------------
# 1. The floor itself
# --------------------------------------------------------------------------


def test_below_floor_observed_rate_never_displaces_the_conservative_prior():
    """A 2-for-2 record is consistent with a 35% model and must not be trusted.

    This is the live defect: on 2026-09-07 the repository's own catalog gave
    gpt-5.5 accepted=1.0/review_ready=1.0/validation_failure=0.0 over exactly
    two decided outcomes, which produced manager_adjusted_success_rate == 1.0
    and won the whole route against six candidates held at the prior.
    """
    thin = _worker(
        "two-of-two",
        sample_count=2,
        accepted=1.0,
        review_ready=1.0,
        validation_failure=0.0,
        p50=37.5,
        p95=37.6,
    )
    components = _components(thin)

    assert components["accepted_rate"] == CONSERVATIVE_PRIORS["accepted_rate"]
    assert components["review_ready_rate"] == CONSERVATIVE_PRIORS["review_ready_rate"]
    assert (
        components["validation_failure_rate"]
        == CONSERVATIVE_PRIORS["validation_failure_rate"]
    )
    assert components["p50_latency_seconds"] == CONSERVATIVE_PRIORS["p50_latency_seconds"]
    assert components["p95_latency_seconds"] == CONSERVATIVE_PRIORS["p95_latency_seconds"]
    assert components["manager_adjusted_success_rate"] == 0.25
    assert components["outcome_evidence_admissible"] is False
    assert components["outcome_evidence_weight"] == 0.0


def test_withheld_evidence_is_labelled_apart_from_absent_evidence():
    """"too little to trust" must stay distinguishable from "nothing at all"."""
    withheld = _components(
        _worker("withheld", sample_count=2, accepted=1.0, p50=37.5)
    )["evidence_sources"]
    absent = _components(_worker("absent", sample_count=0))["evidence_sources"]

    assert withheld["accepted_rate"] == "insufficient_samples"
    assert withheld["p50_latency_seconds"] == "insufficient_samples"
    assert absent["accepted_rate"] == "conservative_prior"
    assert absent["p50_latency_seconds"] == "conservative_prior"


@pytest.mark.parametrize(
    ("sample_count", "admissible"),
    [(0, False), (1, False), (4, False), (5, True), (12, True), (322, True)],
)
def test_the_floor_sits_exactly_at_min_outcome_samples(sample_count, admissible):
    """n=4 gives one-sided binomial p=0.0625 and cannot reject the 0.50 prior;
    n=5 gives p=0.03125 and can.  The constant must sit on that boundary."""
    assert MIN_OUTCOME_SAMPLES == 5
    components = _components(
        _worker("boundary", sample_count=sample_count, accepted=0.9, review_ready=0.9)
    )
    assert components["outcome_evidence_admissible"] is admissible
    assert (components["accepted_rate"] == 0.9) is admissible


def test_the_floor_is_a_named_constant_reported_with_every_decision():
    assert _components(_worker("any"))["min_outcome_samples"] == MIN_OUTCOME_SAMPLES


# --------------------------------------------------------------------------
# 2. Evidence weighting, and its place ahead of latency
# --------------------------------------------------------------------------


def test_admissible_evidence_is_shrunk_toward_the_prior_by_sample_count():
    """Weight is n/(n+OUTCOME_PRIOR_STRENGTH): 0.5 at the floor, never 1.0."""
    at_floor = _components(
        _worker("floor", sample_count=5, accepted=1.0, review_ready=1.0, validation_failure=0.0)
    )
    deep = _components(
        _worker("deep", sample_count=95, accepted=1.0, review_ready=1.0, validation_failure=0.0)
    )

    assert at_floor["outcome_evidence_weight"] == pytest.approx(
        5 / (5 + OUTCOME_PRIOR_STRENGTH)
    )
    # prior_success is 0.50 - 0.25 = 0.25; effective_success is 1.0.
    assert at_floor["evidence_weighted_success_rate"] == pytest.approx(0.25 + 0.5 * 0.75)
    assert deep["evidence_weighted_success_rate"] == pytest.approx(0.25 + 0.95 * 0.75)
    assert deep["evidence_weighted_success_rate"] < 1.0
    assert (
        deep["evidence_weighted_success_rate"]
        > at_floor["evidence_weighted_success_rate"]
    )


def test_a_fast_worker_with_no_outcomes_loses_to_a_measured_slow_one():
    """The audit's latency race, reproduced with the repository's own numbers.

    glm-5 carries a measured p50 of 115.32s over ZERO decided outcomes while
    every other candidate is held at the 3600s prior.  Under the old tuple p50
    was the first key that separated anything, so the zero-sample worker won.
    """
    fast_but_unknown = _worker("aaa-fast", provider="aaa", sample_count=0, p50=115.32, p95=115.32)
    slow_but_measured = _worker(
        "zzz-measured",
        provider="zzz",
        sample_count=12,
        accepted=0.5,
        review_ready=0.5,
        validation_failure=0.25,
    )

    decision = select_worker(_task(), [fast_but_unknown, slow_but_measured])

    # Quality is a genuine tie here (both resolve to 0.25), so the decision
    # turns on evidence volume -- not on the unbacked latency measurement.
    assert decision.selected_worker_id == "zzz-measured"
    assert [c.worker_id for c in decision.candidates][0] == "zzz-measured"


def test_evidence_weighted_quality_outranks_latency_outright():
    fast_and_poor = _worker(
        "fast-poor",
        provider="aaa",
        sample_count=40,
        accepted=0.2,
        review_ready=0.2,
        validation_failure=0.05,
        p50=30.0,
        p95=40.0,
    )
    slow_and_good = _worker(
        "slow-good",
        provider="zzz",
        sample_count=40,
        accepted=0.9,
        review_ready=0.9,
        validation_failure=0.02,
        p50=3000.0,
        p95=9000.0,
    )

    decision = select_worker(_task(), [fast_and_poor, slow_and_good])
    assert decision.selected_worker_id == "slow-good"


def test_sample_count_is_consulted_by_the_sort_key():
    """The exhaustive-scan finding: sample_count occurred nowhere in the tuple."""
    task = _task()
    known = rank_workforce(task, [_worker("known", sample_count=40)]).candidates[0]
    unknown = rank_workforce(task, [_worker("known", sample_count=0)]).candidates[0]

    assert _candidate_sort_key(known) != _candidate_sort_key(unknown)
    assert -40 in _candidate_sort_key(known)


def test_evidence_volume_is_ranked_before_latency_in_the_tuple():
    """Position matters, not just presence: the sample-count key must sit ahead
    of both latency keys or a thin-but-fast candidate wins again."""
    candidate = rank_workforce(
        _task(), [_worker("probe", sample_count=40, p50=123.0, p95=456.0)]
    ).candidates[0]
    key = list(_candidate_sort_key(candidate))

    # sample_count 40 is admissible, so 123.0/456.0 are the real observed
    # latencies sitting in the tuple -- and evidence volume still precedes them.
    assert key.index(-40) < key.index(123.0)
    assert key.index(-40) < key.index(456.0)


def test_evidence_volume_breaks_a_quality_tie_before_latency_does():
    """Behavioural counterpart to the positional assertion above.

    Both candidates carry admissible evidence AND a real measured latency, and
    both resolve to exactly the prior-equivalent 0.25 quality, so the decision
    turns solely on whether evidence volume or speed is consulted first.
    """
    thin_and_fast = _worker(
        "aaa-thin-fast",
        provider="aaa",
        sample_count=5,
        accepted=0.5,
        review_ready=0.5,
        validation_failure=0.25,
        p50=100.0,
        p95=200.0,
    )
    deep_and_slow = _worker(
        "zzz-deep-slow",
        provider="zzz",
        sample_count=100,
        accepted=0.5,
        review_ready=0.5,
        validation_failure=0.25,
        p50=3000.0,
        p95=9000.0,
    )

    decision = select_worker(_task(), [thin_and_fast, deep_and_slow])

    assert (
        thin_and_fast.evidence.accepted_rate == deep_and_slow.evidence.accepted_rate
    ), "the tie this test depends on must be a real one"
    assert decision.selected_worker_id == "zzz-deep-slow"


def test_a_thin_perfect_record_loses_to_a_deep_strong_one():
    """Shrinkage, not just the floor: 5/5 is admissible but still not 1.0.

    Without weighting, a 5-for-5 record scores a flat 1.0 and beats a 200-task
    worker at 0.80.  Shrunk toward the prior it scores 0.625 against 0.787 and
    correctly loses.
    """
    thin_perfect = _worker(
        "aaa-thin-perfect",
        provider="aaa",
        sample_count=5,
        accepted=1.0,
        review_ready=1.0,
        validation_failure=0.0,
    )
    deep_strong = _worker(
        "zzz-deep-strong",
        provider="zzz",
        sample_count=200,
        accepted=0.8,
        review_ready=0.8,
        validation_failure=0.0,
    )

    decision = select_worker(_task(), [thin_perfect, deep_strong])
    by_id = {c.worker_id: c.score_components for c in decision.candidates}

    assert by_id["aaa-thin-perfect"]["manager_adjusted_success_rate"] == 1.0
    assert by_id["zzz-deep-strong"]["manager_adjusted_success_rate"] == 0.8
    assert (
        by_id["aaa-thin-perfect"]["evidence_weighted_success_rate"]
        < by_id["zzz-deep-strong"]["evidence_weighted_success_rate"]
    )
    assert decision.selected_worker_id == "zzz-deep-strong"


def test_evidence_weighted_quality_is_ranked_before_latency_in_the_tuple():
    candidate = rank_workforce(
        _task(),
        [_worker("probe", sample_count=40, accepted=0.9, review_ready=0.9, validation_failure=0.02)],
    ).candidates[0]
    key = list(_candidate_sort_key(candidate))
    components = candidate.score_components

    weighted = -components["evidence_weighted_success_rate"]
    assert key.index(weighted) < key.index(CONSERVATIVE_PRIORS["p50_latency_seconds"])


# --------------------------------------------------------------------------
# 3. What the guard must NOT touch
# --------------------------------------------------------------------------


def test_cost_and_tool_discipline_evidence_are_not_sample_gated():
    """A single billed run reports a true rate; it is not a sample statistic."""
    components = _components(
        _worker("billed", sample_count=1, cost=2.0, tool_discipline=88.0)
    )

    assert components["cost_usd_per_1k_tokens"] == 2.0
    assert components["cost_known"] is True
    assert components["tool_discipline_score"] == 88.0
    assert components["evidence_sources"]["tool_discipline_score"] == "observed"
    assert "cost_usd_per_1k_tokens" not in SAMPLE_GATED_EVIDENCE_FIELDS
    assert "tool_discipline_score" not in SAMPLE_GATED_EVIDENCE_FIELDS


def test_manager_adjustment_is_an_override_and_is_never_diluted_by_sample_count():
    thin = _components(_worker("thin", sample_count=0, manager_score_adjustment=10.0))
    deep = _components(_worker("deep", sample_count=400, manager_score_adjustment=10.0))

    # Both start from the same prior-derived 0.25 baseline and both receive the
    # full +0.10; the adjustment is a human override, not a sample statistic.
    assert thin["evidence_weighted_success_rate"] == pytest.approx(0.35)
    assert deep["evidence_weighted_success_rate"] == pytest.approx(0.35)


def test_unknown_cost_stays_a_deliberate_no_op_rather_than_a_free_candidate():
    """Finding 3 check: the null-cost half was already handled deliberately.

    When cost is unknown fleet-wide both cost keys are uniform across every
    candidate, so they contribute nothing -- exactly what skipping them would
    do.  When one candidate DOES have a cost, the unknown one still sorts
    behind it as (1, inf) and can never look free.
    """
    task = _task()
    priced = rank_workforce(task, [_worker("priced", cost=1.0, sample_count=40)]).candidates[0]
    unpriced = rank_workforce(task, [_worker("unpriced", cost=None, sample_count=40)]).candidates[0]

    assert _candidate_sort_key(priced)[2:4] == (0, pytest.approx(80.0))
    assert _candidate_sort_key(unpriced)[2:4] == (1, float("inf"))
    assert unpriced.score_components["evidence_sources"]["cost_usd_per_1k_tokens"] == "unknown"

    # Fleet-wide unknown: the two cost keys are identical for everyone, so they
    # cannot be what collapses the field.
    fleet = rank_workforce(
        task,
        [_worker(f"w{i}", provider=f"p{i}", cost=None, sample_count=40) for i in range(4)],
    )
    cost_keys = {_candidate_sort_key(c)[2:4] for c in fleet.candidates}
    assert cost_keys == {(1, float("inf"))}
