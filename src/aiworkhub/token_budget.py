from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


SCHEMA_ID = "aiworkhub.token_budget.kernel.v1"
EVIDENCE_SCHEMA_ID = "aiworkhub.token_budget.supervisor_evidence.v1"
MAX_REPORT_ID_BYTES = 128
REPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class TelemetryAuthority(str, Enum):
    ENFORCED_LIVE = "enforced_live"
    POSTHOC_ONLY = "posthoc_only"
    TELEMETRY_UNAVAILABLE = "telemetry_unavailable"


class SampleKind(str, Enum):
    CUMULATIVE = "cumulative"
    DELTA = "delta"


class SampleOutcome(str, Enum):
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    REPLAY_IGNORED = "replay_ignored"
    REPLAY_CONFLICT = "replay_conflict"


@dataclass(frozen=True)
class TokenSample:
    report_id: str
    authority: TelemetryAuthority | str
    kind: SampleKind | str
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    observed_at: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class ReportRecord:
    fingerprint: str
    outcome: SampleOutcome
    accepted_delta: int | None
    reason: str


@dataclass(frozen=True)
class TokenBudgetState:
    cap_tokens: int | None = None
    accepted_total_tokens: int = 0
    enforceable_live_tokens: int = 0
    max_cumulative_total_tokens: int = 0
    reports: dict[str, ReportRecord] = field(default_factory=dict)


@dataclass(frozen=True)
class TokenBudgetDecision:
    state: TokenBudgetState
    outcome: SampleOutcome
    reason: str
    report_id: str
    accepted_delta_tokens: int | None
    accepted_total_tokens: int
    enforceable_live_tokens: int
    cap_tokens: int | None
    cap_crossed: bool
    cap_enforceable: bool
    evidence: dict[str, Any]


def _coerce_authority(value: TelemetryAuthority | str) -> TelemetryAuthority:
    if isinstance(value, TelemetryAuthority):
        return value
    return TelemetryAuthority(str(value))


def _coerce_kind(value: SampleKind | str) -> SampleKind:
    return value if isinstance(value, SampleKind) else SampleKind(str(value))


def _enum_value(value: Enum | str) -> str:
    return str(value.value if isinstance(value, Enum) else value)


def telemetry_authority_rank(authority: TelemetryAuthority | str) -> int:
    order = {
        TelemetryAuthority.TELEMETRY_UNAVAILABLE: 0,
        TelemetryAuthority.POSTHOC_ONLY: 1,
        TelemetryAuthority.ENFORCED_LIVE: 2,
    }
    return order[_coerce_authority(authority)]


def _valid_report_id(report_id: str) -> bool:
    try:
        encoded = report_id.encode("ascii")
    except UnicodeEncodeError:
        return False
    return (
        0 < len(encoded) <= MAX_REPORT_ID_BYTES
        and REPORT_ID_RE.match(report_id) is not None
    )


def _sample_fingerprint(sample: TokenSample) -> str:
    payload = {
        "authority": _enum_value(sample.authority),
        "input_tokens": sample.input_tokens,
        "kind": _enum_value(sample.kind),
        "observed_at": sample.observed_at,
        "output_tokens": sample.output_tokens,
        "report_id": sample.report_id,
        "source": sample.source,
        "total_tokens": sample.total_tokens,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _token_total(sample: TokenSample) -> int | None:
    if sample.total_tokens is not None:
        return sample.total_tokens
    if sample.input_tokens is None and sample.output_tokens is None:
        return None
    return int(sample.input_tokens or 0) + int(sample.output_tokens or 0)


def _record_decision(
    state: TokenBudgetState,
    sample: TokenSample,
    *,
    outcome: SampleOutcome,
    reason: str,
    accepted_delta: int | None,
    cap_crossed: bool,
    cap_enforceable: bool,
    next_state: TokenBudgetState | None = None,
) -> TokenBudgetDecision:
    final_state = next_state or state
    evidence = {
        "schema_id": SCHEMA_ID,
        "report_id": sample.report_id,
        "outcome": outcome.value,
        "reason": reason,
        "telemetry_authority": _enum_value(sample.authority),
        "sample_kind": _enum_value(sample.kind),
        "accepted_delta_tokens": accepted_delta,
        "accepted_total_tokens": final_state.accepted_total_tokens,
        "enforceable_live_tokens": final_state.enforceable_live_tokens,
        "cap_tokens": final_state.cap_tokens,
        "cap_crossed": cap_crossed,
        "cap_enforceable": cap_enforceable,
        "enforcing": False,
        "cost_usd": None,
    }
    return TokenBudgetDecision(
        state=final_state,
        outcome=outcome,
        reason=reason,
        report_id=sample.report_id,
        accepted_delta_tokens=accepted_delta,
        accepted_total_tokens=final_state.accepted_total_tokens,
        enforceable_live_tokens=final_state.enforceable_live_tokens,
        cap_tokens=final_state.cap_tokens,
        cap_crossed=cap_crossed,
        cap_enforceable=cap_enforceable,
        evidence=evidence,
    )


def consume_sample(state: TokenBudgetState, sample: TokenSample) -> TokenBudgetDecision:
    fingerprint = _sample_fingerprint(sample)
    if sample.report_id in state.reports:
        previous = state.reports[sample.report_id]
        outcome = (
            SampleOutcome.REPLAY_IGNORED
            if fingerprint == previous.fingerprint
            else SampleOutcome.REPLAY_CONFLICT
        )
        reason = (
            "report_id_replay"
            if outcome == SampleOutcome.REPLAY_IGNORED
            else "report_id_conflict"
        )
        return _record_decision(
            state,
            sample,
            outcome=outcome,
            reason=reason,
            accepted_delta=None,
            cap_crossed=False,
            cap_enforceable=False,
        )

    if not _valid_report_id(sample.report_id):
        record = ReportRecord(
            fingerprint, SampleOutcome.IGNORED, None, "invalid_report_id"
        )
        return _record_decision(
            replace(state, reports={**state.reports, sample.report_id: record}),
            sample,
            outcome=SampleOutcome.IGNORED,
            reason="invalid_report_id",
            accepted_delta=None,
            cap_crossed=False,
            cap_enforceable=False,
        )

    try:
        sample = replace(
            sample,
            authority=_coerce_authority(sample.authority),
            kind=_coerce_kind(sample.kind),
        )
    except ValueError:
        record = ReportRecord(
            fingerprint, SampleOutcome.IGNORED, None, "invalid_sample_metadata"
        )
        return _record_decision(
            replace(state, reports={**state.reports, sample.report_id: record}),
            sample,
            outcome=SampleOutcome.IGNORED,
            reason="invalid_sample_metadata",
            accepted_delta=None,
            cap_crossed=False,
            cap_enforceable=False,
        )

    if sample.authority is TelemetryAuthority.TELEMETRY_UNAVAILABLE:
        record = ReportRecord(
            fingerprint, SampleOutcome.IGNORED, None, "telemetry_unavailable"
        )
        return _record_decision(
            replace(state, reports={**state.reports, sample.report_id: record}),
            sample,
            outcome=SampleOutcome.IGNORED,
            reason="telemetry_unavailable",
            accepted_delta=None,
            cap_crossed=False,
            cap_enforceable=False,
        )

    total = _token_total(sample)
    if total is None or total < 0:
        record = ReportRecord(
            fingerprint, SampleOutcome.IGNORED, None, "invalid_token_total"
        )
        return _record_decision(
            replace(state, reports={**state.reports, sample.report_id: record}),
            sample,
            outcome=SampleOutcome.IGNORED,
            reason="invalid_token_total",
            accepted_delta=None,
            cap_crossed=False,
            cap_enforceable=False,
        )

    if sample.kind is SampleKind.CUMULATIVE:
        if total < state.max_cumulative_total_tokens:
            record = ReportRecord(
                fingerprint, SampleOutcome.IGNORED, None, "cumulative_regression"
            )
            return _record_decision(
                replace(state, reports={**state.reports, sample.report_id: record}),
                sample,
                outcome=SampleOutcome.IGNORED,
                reason="cumulative_regression",
                accepted_delta=None,
                cap_crossed=False,
                cap_enforceable=False,
            )
        accepted_delta = max(0, total - state.accepted_total_tokens)
        next_max_cumulative = total
    else:
        accepted_delta = total
        next_max_cumulative = state.max_cumulative_total_tokens

    previous_live = state.enforceable_live_tokens
    next_live = previous_live
    if sample.authority is TelemetryAuthority.ENFORCED_LIVE:
        if sample.kind is SampleKind.CUMULATIVE:
            next_live = max(previous_live, total)
        else:
            next_live += accepted_delta
    next_total = state.accepted_total_tokens + accepted_delta
    cap_crossed = (
        state.cap_tokens is not None
        and previous_live < state.cap_tokens
        and next_live >= state.cap_tokens
    )
    # Observed usage is telemetry only. Crossing a legacy cap is recorded as a
    # diagnostic but never authorizes enforcement, so no sample is enforceable.
    cap_enforceable = False
    record = ReportRecord(
        fingerprint, SampleOutcome.ACCEPTED, accepted_delta, "accepted"
    )
    next_state = replace(
        state,
        accepted_total_tokens=next_total,
        enforceable_live_tokens=next_live,
        max_cumulative_total_tokens=next_max_cumulative,
        reports={**state.reports, sample.report_id: record},
    )
    return _record_decision(
        state,
        sample,
        outcome=SampleOutcome.ACCEPTED,
        reason="accepted",
        accepted_delta=accepted_delta,
        cap_crossed=bool(cap_crossed),
        cap_enforceable=bool(cap_enforceable),
        next_state=next_state,
    )


def supervisor_evidence(
    state: TokenBudgetState,
    decisions: list[TokenBudgetDecision],
    *,
    subject: str,
) -> dict[str, Any]:
    events = [decision.evidence for decision in decisions]
    encoded = json.dumps(events, sort_keys=True, separators=(",", ":")).encode("utf-8")
    authorities = [
        str(event.get("telemetry_authority") or "") for event in events
    ]
    if TelemetryAuthority.ENFORCED_LIVE.value in authorities:
        telemetry_authority = TelemetryAuthority.ENFORCED_LIVE.value
    elif TelemetryAuthority.POSTHOC_ONLY.value in authorities:
        telemetry_authority = TelemetryAuthority.POSTHOC_ONLY.value
    else:
        telemetry_authority = TelemetryAuthority.TELEMETRY_UNAVAILABLE.value
    return {
        "schema_id": EVIDENCE_SCHEMA_ID,
        "subject": subject,
        "cost_usd": None,
        "cap_tokens": state.cap_tokens,
        "accepted_total_tokens": state.accepted_total_tokens,
        "enforceable_live_tokens": state.enforceable_live_tokens,
        "report_count": len(state.reports),
        "telemetry_authority": telemetry_authority,
        "telemetry_observed": bool(events),
        "telemetry_reason": "" if events else "no_provider_usage_report_observed",
        "cap_enforceable": any(
            bool(event.get("cap_enforceable")) for event in events
        ),
        "enforcing": False,
        "event_sha256": hashlib.sha256(encoded).hexdigest(),
        "events": events,
    }
