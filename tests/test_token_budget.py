from aiworkhub.token_budget import (
    SampleKind,
    SampleOutcome,
    TelemetryAuthority,
    TokenBudgetState,
    TokenSample,
    consume_sample,
    supervisor_evidence,
    telemetry_authority_rank,
)


def test_authority_ordering_and_only_live_crossing_is_enforceable() -> None:
    assert telemetry_authority_rank(TelemetryAuthority.TELEMETRY_UNAVAILABLE) == 0
    assert telemetry_authority_rank(TelemetryAuthority.POSTHOC_ONLY) == 1
    assert telemetry_authority_rank(TelemetryAuthority.ENFORCED_LIVE) == 2

    state = TokenBudgetState(cap_tokens=100)
    posthoc = consume_sample(
        state,
        TokenSample(
            report_id="posthoc-1",
            authority=TelemetryAuthority.POSTHOC_ONLY,
            kind=SampleKind.DELTA,
            total_tokens=150,
        ),
    )

    assert posthoc.outcome is SampleOutcome.ACCEPTED
    assert posthoc.accepted_total_tokens == 150
    assert posthoc.enforceable_live_tokens == 0
    assert posthoc.cap_crossed is False
    assert posthoc.cap_enforceable is False

    live = consume_sample(
        posthoc.state,
        TokenSample(
            report_id="live-1",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.DELTA,
            total_tokens=100,
        ),
    )

    assert live.accepted_total_tokens == 250
    assert live.enforceable_live_tokens == 100
    assert live.cap_crossed is True
    assert live.cap_enforceable is True


def test_later_live_cumulative_report_can_authoritatively_prove_cap_crossing() -> None:
    state = TokenBudgetState(cap_tokens=100)
    posthoc = consume_sample(
        state,
        TokenSample(
            report_id="posthoc-cumulative",
            authority=TelemetryAuthority.POSTHOC_ONLY,
            kind=SampleKind.CUMULATIVE,
            total_tokens=150,
        ),
    )
    live = consume_sample(
        posthoc.state,
        TokenSample(
            report_id="live-cumulative",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.CUMULATIVE,
            total_tokens=175,
        ),
    )

    assert posthoc.cap_enforceable is False
    assert live.accepted_delta_tokens == 25
    assert live.accepted_total_tokens == 175
    assert live.enforceable_live_tokens == 175
    assert live.cap_crossed is True
    assert live.cap_enforceable is True


def test_cumulative_and_delta_samples_do_not_double_count_or_regress() -> None:
    state = TokenBudgetState()
    first = consume_sample(
        state,
        TokenSample(
            report_id="cum-1",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.CUMULATIVE,
            total_tokens=100,
        ),
    )
    second = consume_sample(
        first.state,
        TokenSample(
            report_id="cum-2",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.CUMULATIVE,
            total_tokens=125,
        ),
    )
    delta = consume_sample(
        second.state,
        TokenSample(
            report_id="delta-1",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.DELTA,
            total_tokens=10,
        ),
    )
    regression = consume_sample(
        delta.state,
        TokenSample(
            report_id="cum-3",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.CUMULATIVE,
            total_tokens=120,
        ),
    )

    assert first.accepted_delta_tokens == 100
    assert second.accepted_delta_tokens == 25
    assert delta.accepted_delta_tokens == 10
    assert delta.accepted_total_tokens == 135
    assert regression.outcome is SampleOutcome.IGNORED
    assert regression.reason == "cumulative_regression"
    assert regression.accepted_total_tokens == 135


def test_mixed_cumulative_delta_cumulative_only_accepts_unseen_attempt_total() -> None:
    state = TokenBudgetState(cap_tokens=125)
    first = consume_sample(
        state,
        TokenSample(
            report_id="cum-1",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.CUMULATIVE,
            total_tokens=100,
        ),
    )
    delta = consume_sample(
        first.state,
        TokenSample(
            report_id="delta-1",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.DELTA,
            total_tokens=10,
        ),
    )
    cumulative = consume_sample(
        delta.state,
        TokenSample(
            report_id="cum-2",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.CUMULATIVE,
            total_tokens=125,
        ),
    )
    overlapping = consume_sample(
        cumulative.state,
        TokenSample(
            report_id="cum-3",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.CUMULATIVE,
            total_tokens=125,
        ),
    )

    assert cumulative.accepted_delta_tokens == 15
    assert cumulative.accepted_total_tokens == 125
    assert cumulative.enforceable_live_tokens == 125
    assert cumulative.cap_crossed is True
    assert cumulative.cap_enforceable is True
    assert overlapping.accepted_delta_tokens == 0
    assert overlapping.accepted_total_tokens == 125
    assert overlapping.cap_enforceable is False


def test_unknown_sample_metadata_fails_closed_and_consumes_report_id() -> None:
    state = TokenBudgetState(cap_tokens=1)
    invalid = consume_sample(
        state,
        TokenSample(
            report_id="invalid-metadata",
            authority="future_live",
            kind="absolute",
            total_tokens=999,
        ),
    )
    same_replay = consume_sample(
        invalid.state,
        TokenSample(
            report_id="invalid-metadata",
            authority="future_live",
            kind="absolute",
            total_tokens=999,
        ),
    )
    conflict = consume_sample(
        invalid.state,
        TokenSample(
            report_id="invalid-metadata",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.DELTA,
            total_tokens=1,
        ),
    )

    assert invalid.outcome is SampleOutcome.IGNORED
    assert invalid.reason == "invalid_sample_metadata"
    assert invalid.accepted_total_tokens == 0
    assert invalid.cap_enforceable is False
    assert invalid.evidence["telemetry_authority"] == "future_live"
    assert invalid.evidence["sample_kind"] == "absolute"
    assert same_replay.outcome is SampleOutcome.REPLAY_IGNORED
    assert conflict.outcome is SampleOutcome.REPLAY_CONFLICT
    assert conflict.accepted_total_tokens == 0


def test_report_ids_are_single_use_for_accepted_and_ignored_samples() -> None:
    state = TokenBudgetState()
    accepted = consume_sample(
        state,
        TokenSample(
            report_id="fixed-id",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.DELTA,
            total_tokens=10,
        ),
    )
    same_replay = consume_sample(
        accepted.state,
        TokenSample(
            report_id="fixed-id",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.DELTA,
            total_tokens=10,
        ),
    )
    conflict = consume_sample(
        accepted.state,
        TokenSample(
            report_id="fixed-id",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.DELTA,
            total_tokens=11,
        ),
    )
    ignored = consume_sample(
        accepted.state,
        TokenSample(
            report_id="missing-telemetry",
            authority=TelemetryAuthority.TELEMETRY_UNAVAILABLE,
            kind=SampleKind.DELTA,
        ),
    )
    ignored_conflict = consume_sample(
        ignored.state,
        TokenSample(
            report_id="missing-telemetry",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.DELTA,
            total_tokens=99,
        ),
    )

    assert same_replay.outcome is SampleOutcome.REPLAY_IGNORED
    assert same_replay.accepted_total_tokens == 10
    assert conflict.outcome is SampleOutcome.REPLAY_CONFLICT
    assert conflict.accepted_total_tokens == 10
    assert ignored.outcome is SampleOutcome.IGNORED
    assert ignored.reason == "telemetry_unavailable"
    assert ignored_conflict.outcome is SampleOutcome.REPLAY_CONFLICT
    assert ignored_conflict.accepted_total_tokens == 10


def test_bounded_report_ids_are_consumed_even_when_invalid() -> None:
    invalid_id = "x" * 129
    state = TokenBudgetState()
    ignored = consume_sample(
        state,
        TokenSample(
            report_id=invalid_id,
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.DELTA,
            total_tokens=1,
        ),
    )
    replay = consume_sample(
        ignored.state,
        TokenSample(
            report_id=invalid_id,
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.DELTA,
            total_tokens=2,
        ),
    )

    assert ignored.outcome is SampleOutcome.IGNORED
    assert ignored.reason == "invalid_report_id"
    assert invalid_id in ignored.state.reports
    assert replay.outcome is SampleOutcome.REPLAY_CONFLICT


def test_unavailable_telemetry_does_not_fabricate_zero_tokens_or_dollars() -> None:
    decision = consume_sample(
        TokenBudgetState(cap_tokens=10),
        TokenSample(
            report_id="unavailable-1",
            authority=TelemetryAuthority.TELEMETRY_UNAVAILABLE,
            kind=SampleKind.CUMULATIVE,
        ),
    )

    assert decision.outcome is SampleOutcome.IGNORED
    assert decision.accepted_delta_tokens is None
    assert decision.evidence["accepted_delta_tokens"] is None
    assert decision.evidence["cost_usd"] is None
    assert decision.cap_enforceable is False


def test_supervisor_evidence_is_immutable_and_contains_no_dollars() -> None:
    first = consume_sample(
        TokenBudgetState(cap_tokens=5),
        TokenSample(
            report_id="live-1",
            authority=TelemetryAuthority.ENFORCED_LIVE,
            kind=SampleKind.DELTA,
            total_tokens=5,
        ),
    )
    evidence = supervisor_evidence(first.state, [first], subject="worker-request")

    assert evidence["schema_id"] == "aiworkhub.token_budget.supervisor_evidence.v1"
    assert evidence["subject"] == "worker-request"
    assert evidence["event_sha256"]
    assert evidence["cost_usd"] is None
    assert evidence["events"][0]["cap_enforceable"] is True
