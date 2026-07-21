"""Pure AIWorkHub workforce routing policy.

This module ranks already-declared worker candidates.  It never mutates task
queues, never launches a process, and never invents cost or outcome evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


TASK_KINDS: tuple[str, ...] = (
    "mechanical",
    "code",
    "research",
    "linguistic",
    "review",
)
SUPPORTED_MODELS: tuple[str, ...] = (
    "gpt-5.5",
    "deepseek-v4-pro",
    "glm-5.2",
    "claude",
)
SUPPORTED_ADAPTERS: tuple[str, ...] = (
    "codex_cli",
    "deepseek_copilot_cli",
    "glm_cli",
    "claude_cli",
    "manual",
)
RISK_ORDER: Mapping[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}
CONSERVATIVE_PRIORS: Mapping[str, float] = {
    "accepted_rate": 0.50,
    "review_ready_rate": 0.50,
    "validation_failure_rate": 0.25,
    "p50_latency_seconds": 3600.0,
    "p95_latency_seconds": 14_400.0,
    "cost_usd_per_1k_tokens": 99.0,
    "estimated_tokens": 100_000.0,
}


def _clean_token(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _clean_set(values: Iterable[str] | None) -> frozenset[str]:
    return frozenset(_clean_token(value) for value in (values or ()) if str(value or "").strip())


def _risk_value(value: str) -> int:
    return RISK_ORDER.get(_clean_token(value), RISK_ORDER["critical"])


def _bounded_rate(value: float | None, default: float) -> float:
    if value is None:
        return default
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _bounded_positive(value: float | None, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed >= 0.0 else default


@dataclass(frozen=True, slots=True)
class TaskRequirements:
    """Bounded requirements for one task allocation decision."""

    task_id: str
    repo_id: str
    kinds: frozenset[str]
    risk: str = "medium"
    context_tokens: int = 0
    tool_needs: frozenset[str] = field(default_factory=frozenset)
    quality_floor: float = 0.0
    deadline_seconds: float | None = None
    owner_model_pin: str | None = None
    coordinator_model_pin: str | None = None
    no_providers: frozenset[str] = field(default_factory=frozenset)
    allow_parallel: bool = False

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        repo_id: str,
        kinds: Iterable[str],
        risk: str = "medium",
        context_tokens: int = 0,
        tool_needs: Iterable[str] | None = None,
        quality_floor: float = 0.0,
        deadline_seconds: float | None = None,
        owner_model_pin: str | None = None,
        coordinator_model_pin: str | None = None,
        no_providers: Iterable[str] | None = None,
        allow_parallel: bool = False,
    ) -> "TaskRequirements":
        cleaned_kinds = _clean_set(kinds)
        if not task_id or not str(task_id).strip():
            raise ValueError("task_id is required")
        if not repo_id or not str(repo_id).strip():
            raise ValueError("repo_id is required")
        if not cleaned_kinds:
            raise ValueError("at least one task kind is required")
        unknown = cleaned_kinds - set(TASK_KINDS)
        if unknown:
            raise ValueError(f"unsupported task kinds: {sorted(unknown)}")
        return cls(
            task_id=str(task_id).strip(),
            repo_id=str(repo_id).strip(),
            kinds=cleaned_kinds,
            risk=_clean_token(risk),
            context_tokens=max(0, int(context_tokens or 0)),
            tool_needs=_clean_set(tool_needs),
            quality_floor=_bounded_rate(quality_floor, 0.0),
            deadline_seconds=deadline_seconds,
            owner_model_pin=owner_model_pin.strip() if isinstance(owner_model_pin, str) and owner_model_pin.strip() else None,
            coordinator_model_pin=coordinator_model_pin.strip() if isinstance(coordinator_model_pin, str) and coordinator_model_pin.strip() else None,
            no_providers=_clean_set(no_providers),
            allow_parallel=bool(allow_parallel),
        )

    @property
    def effective_model_pin(self) -> str | None:
        """Owner and coordinator pins are authoritative; owner is stricter."""
        return self.owner_model_pin or self.coordinator_model_pin

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repo_id": self.repo_id,
            "kinds": sorted(self.kinds),
            "risk": self.risk,
            "context_tokens": self.context_tokens,
            "tool_needs": sorted(self.tool_needs),
            "quality_floor": self.quality_floor,
            "deadline_seconds": self.deadline_seconds,
            "owner_model_pin": self.owner_model_pin,
            "coordinator_model_pin": self.coordinator_model_pin,
            "effective_model_pin": self.effective_model_pin,
            "no_providers": sorted(self.no_providers),
            "allow_parallel": self.allow_parallel,
        }


@dataclass(frozen=True, slots=True)
class OutcomeEvidence:
    """Observed outcome, latency, token, and cost evidence for one worker."""

    accepted_rate: float | None = None
    review_ready_rate: float | None = None
    validation_failure_rate: float | None = None
    p50_latency_seconds: float | None = None
    p95_latency_seconds: float | None = None
    cost_usd_per_1k_tokens: float | None = None
    estimated_tokens: int | None = None
    sample_count: int = 0

    def normalized(self) -> tuple[dict[str, float], dict[str, str]]:
        values = {
            "accepted_rate": _bounded_rate(
                self.accepted_rate, CONSERVATIVE_PRIORS["accepted_rate"]
            ),
            "review_ready_rate": _bounded_rate(
                self.review_ready_rate, CONSERVATIVE_PRIORS["review_ready_rate"]
            ),
            "validation_failure_rate": _bounded_rate(
                self.validation_failure_rate,
                CONSERVATIVE_PRIORS["validation_failure_rate"],
            ),
            "p50_latency_seconds": _bounded_positive(
                self.p50_latency_seconds,
                CONSERVATIVE_PRIORS["p50_latency_seconds"],
            ),
            "p95_latency_seconds": _bounded_positive(
                self.p95_latency_seconds,
                CONSERVATIVE_PRIORS["p95_latency_seconds"],
            ),
            "cost_usd_per_1k_tokens": _bounded_positive(
                self.cost_usd_per_1k_tokens,
                CONSERVATIVE_PRIORS["cost_usd_per_1k_tokens"],
            ),
            "estimated_tokens": _bounded_positive(
                float(self.estimated_tokens) if self.estimated_tokens is not None else None,
                CONSERVATIVE_PRIORS["estimated_tokens"],
            ),
        }
        sources = {
            key: (
                "observed"
                if getattr(self, key if key != "estimated_tokens" else "estimated_tokens") is not None
                else "conservative_prior"
            )
            for key in values
        }
        return values, sources

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted_rate": self.accepted_rate,
            "review_ready_rate": self.review_ready_rate,
            "validation_failure_rate": self.validation_failure_rate,
            "p50_latency_seconds": self.p50_latency_seconds,
            "p95_latency_seconds": self.p95_latency_seconds,
            "cost_usd_per_1k_tokens": self.cost_usd_per_1k_tokens,
            "estimated_tokens": self.estimated_tokens,
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True, slots=True)
class WorkerCapability:
    """Declared capabilities and current availability for a worker/model."""

    worker_id: str
    adapter_id: str
    model: str
    provider: str
    supports: frozenset[str]
    tools: frozenset[str] = field(default_factory=frozenset)
    max_context_tokens: int = 0
    max_risk: str = "medium"
    quality_ceiling: float = 1.0
    available: bool = True
    credential_ok: bool = True
    quota_available: bool = True
    no_provider_override: bool = False
    evidence: OutcomeEvidence = field(default_factory=OutcomeEvidence)

    @classmethod
    def build(
        cls,
        *,
        worker_id: str,
        adapter_id: str,
        model: str,
        provider: str,
        supports: Iterable[str],
        tools: Iterable[str] | None = None,
        max_context_tokens: int = 0,
        max_risk: str = "medium",
        quality_ceiling: float = 1.0,
        available: bool = True,
        credential_ok: bool = True,
        quota_available: bool = True,
        no_provider_override: bool = False,
        evidence: OutcomeEvidence | None = None,
    ) -> "WorkerCapability":
        cleaned_supports = _clean_set(supports)
        unknown = cleaned_supports - set(TASK_KINDS)
        if unknown:
            raise ValueError(f"unsupported worker capabilities: {sorted(unknown)}")
        if _clean_token(adapter_id).replace("-", "_") not in SUPPORTED_ADAPTERS:
            raise ValueError(f"unsupported adapter_id: {adapter_id}")
        return cls(
            worker_id=str(worker_id).strip(),
            adapter_id=_clean_token(adapter_id).replace("-", "_"),
            model=str(model).strip(),
            provider=_clean_token(provider),
            supports=cleaned_supports,
            tools=_clean_set(tools),
            max_context_tokens=max(0, int(max_context_tokens or 0)),
            max_risk=_clean_token(max_risk),
            quality_ceiling=_bounded_rate(quality_ceiling, 1.0),
            available=bool(available),
            credential_ok=bool(credential_ok),
            quota_available=bool(quota_available),
            no_provider_override=bool(no_provider_override),
            evidence=evidence or OutcomeEvidence(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "adapter_id": self.adapter_id,
            "model": self.model,
            "provider": self.provider,
            "supports": sorted(self.supports),
            "tools": sorted(self.tools),
            "max_context_tokens": self.max_context_tokens,
            "max_risk": self.max_risk,
            "quality_ceiling": self.quality_ceiling,
            "available": self.available,
            "credential_ok": self.credential_ok,
            "quota_available": self.quota_available,
            "no_provider_override": self.no_provider_override,
            "evidence": self.evidence.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    worker_id: str
    adapter_id: str
    model: str
    provider: str
    compatible: bool
    excluded: bool
    exclusion_reasons: tuple[str, ...]
    score_components: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "adapter_id": self.adapter_id,
            "model": self.model,
            "provider": self.provider,
            "compatible": self.compatible,
            "excluded": self.excluded,
            "exclusion_reasons": list(self.exclusion_reasons),
            "score_components": dict(self.score_components),
        }


@dataclass(frozen=True, slots=True)
class WorkforceDecision:
    """Explainable, deterministic routing decision."""

    repo_id: str
    task_id: str
    selected_worker_id: str | None
    selected_adapter_id: str | None
    selected_model: str | None
    selected_provider: str | None
    fallback_chain: tuple[str, ...]
    candidates: tuple[CandidateRecord, ...]
    policy: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "task_id": self.task_id,
            "selected_worker_id": self.selected_worker_id,
            "selected_adapter_id": self.selected_adapter_id,
            "selected_model": self.selected_model,
            "selected_provider": self.selected_provider,
            "fallback_chain": list(self.fallback_chain),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "policy": dict(self.policy),
        }


def _exclusion_reasons(task: TaskRequirements, worker: WorkerCapability) -> list[str]:
    reasons: list[str] = []
    pin = task.effective_model_pin
    if pin and worker.model != pin:
        reasons.append(f"authoritative_model_pin_mismatch:{pin}")
    if worker.provider in task.no_providers or worker.no_provider_override:
        reasons.append(f"provider_forbidden:{worker.provider}")
    if not worker.available:
        reasons.append("worker_unavailable")
    if not worker.credential_ok:
        reasons.append("credentials_missing")
    if not worker.quota_available:
        reasons.append("quota_unavailable")
    if not task.kinds.issubset(worker.supports):
        missing = sorted(task.kinds - worker.supports)
        reasons.append(f"missing_capability:{','.join(missing)}")
    if not task.tool_needs.issubset(worker.tools):
        missing = sorted(task.tool_needs - worker.tools)
        reasons.append(f"missing_tools:{','.join(missing)}")
    if task.context_tokens and worker.max_context_tokens < task.context_tokens:
        reasons.append("context_too_large")
    if _risk_value(worker.max_risk) < _risk_value(task.risk):
        reasons.append("risk_exceeds_worker_limit")
    if worker.quality_ceiling < task.quality_floor:
        reasons.append("quality_ceiling_below_floor")
    return reasons


def _score_components(task: TaskRequirements, worker: WorkerCapability) -> dict[str, Any]:
    values, sources = worker.evidence.normalized()
    observed_success = min(values["accepted_rate"], values["review_ready_rate"])
    effective_success = max(0.0, observed_success - values["validation_failure_rate"])
    estimated_cost = round(
        values["cost_usd_per_1k_tokens"] * values["estimated_tokens"] / 1000.0,
        6,
    )
    deadline_penalty = 0
    if task.deadline_seconds is not None and values["p95_latency_seconds"] > task.deadline_seconds:
        deadline_penalty = 1
    return {
        "accepted_rate": values["accepted_rate"],
        "review_ready_rate": values["review_ready_rate"],
        "validation_failure_rate": values["validation_failure_rate"],
        "effective_success_rate": round(effective_success, 6),
        "p50_latency_seconds": values["p50_latency_seconds"],
        "p95_latency_seconds": values["p95_latency_seconds"],
        "cost_usd_per_1k_tokens": values["cost_usd_per_1k_tokens"],
        "estimated_tokens": int(values["estimated_tokens"]),
        "estimated_cost_usd": estimated_cost,
        "quality_floor": task.quality_floor,
        "meets_quality_floor": effective_success >= task.quality_floor,
        "deadline_penalty": deadline_penalty,
        "sample_count": worker.evidence.sample_count,
        "evidence_sources": sources,
    }


def _candidate_sort_key(candidate: CandidateRecord) -> tuple[Any, ...]:
    components = candidate.score_components
    return (
        int(candidate.excluded),
        components.get("deadline_penalty", 0),
        components.get("estimated_cost_usd", CONSERVATIVE_PRIORS["cost_usd_per_1k_tokens"]),
        -components.get("effective_success_rate", 0.0),
        components.get("validation_failure_rate", 1.0),
        components.get("p50_latency_seconds", CONSERVATIVE_PRIORS["p50_latency_seconds"]),
        components.get("p95_latency_seconds", CONSERVATIVE_PRIORS["p95_latency_seconds"]),
        candidate.provider,
        candidate.model,
        candidate.worker_id,
    )


def rank_workforce(
    task: TaskRequirements,
    workers: Sequence[WorkerCapability],
) -> WorkforceDecision:
    """Rank compatible workers and select the cheapest capable available one."""
    records: list[CandidateRecord] = []
    for worker in workers:
        reasons = _exclusion_reasons(task, worker)
        components = _score_components(task, worker)
        if not components["meets_quality_floor"]:
            reasons.append("observed_or_prior_quality_below_floor")
        records.append(
            CandidateRecord(
                worker_id=worker.worker_id,
                adapter_id=worker.adapter_id,
                model=worker.model,
                provider=worker.provider,
                compatible=not reasons,
                excluded=bool(reasons),
                exclusion_reasons=tuple(reasons),
                score_components=components,
            )
        )
    ordered = tuple(sorted(records, key=_candidate_sort_key))
    compatible = tuple(candidate for candidate in ordered if not candidate.excluded)
    selected = compatible[0] if compatible else None
    return WorkforceDecision(
        repo_id=task.repo_id,
        task_id=task.task_id,
        selected_worker_id=selected.worker_id if selected else None,
        selected_adapter_id=selected.adapter_id if selected else None,
        selected_model=selected.model if selected else None,
        selected_provider=selected.provider if selected else None,
        fallback_chain=tuple(candidate.worker_id for candidate in compatible),
        candidates=ordered,
        policy={
            "mode": "capability_cost_outcome_task_allocation",
            "pure_policy_only": True,
            "queue_mutation": False,
            "worker_launch": False,
            "duplicate_accepted_work": "prohibited",
            "parallel_heterogeneous_shards": "allowed_only_for_disjoint_inputs",
            "failed_residuals": "escalate_residual_only",
            "selection_order": [
                "authoritative pins/provider constraints",
                "availability/credentials/quota",
                "capability/tool/context/risk/quality compatibility",
                "cheapest estimated cost",
                "higher effective accepted/review-ready outcome",
                "lower validation failure",
                "lower latency",
                "stable lexical tie-break",
            ],
            "conservative_priors": dict(CONSERVATIVE_PRIORS),
        },
    )


def select_worker(
    task: TaskRequirements,
    workers: Sequence[WorkerCapability],
) -> WorkforceDecision:
    """Compatibility alias for callers that need a single decision record."""
    return rank_workforce(task, workers)


def plan_parallel_shards(
    task: TaskRequirements,
    shard_inputs: Sequence[Mapping[str, Any]],
    workers: Sequence[WorkerCapability],
) -> dict[str, Any]:
    """Purely describe parallel shard routing without launching workers.

    Duplicate shard ids or duplicate accepted source ids are rejected up front.
    """
    seen_shards: set[str] = set()
    seen_sources: set[str] = set()
    decisions: list[dict[str, Any]] = []
    violations: list[str] = []
    for shard in shard_inputs:
        shard_id = str(shard.get("shard_id") or "").strip()
        source_ids = {str(value).strip() for value in shard.get("source_ids", []) if str(value).strip()}
        if not shard_id:
            violations.append("missing_shard_id")
            continue
        if shard_id in seen_shards:
            violations.append(f"duplicate_shard:{shard_id}")
        overlap = sorted(seen_sources & source_ids)
        if overlap:
            violations.append(f"duplicate_source_ids:{','.join(overlap)}")
        seen_shards.add(shard_id)
        seen_sources.update(source_ids)
        shard_task = TaskRequirements(
            task_id=f"{task.task_id}:{shard_id}",
            repo_id=task.repo_id,
            kinds=task.kinds,
            risk=task.risk,
            context_tokens=task.context_tokens,
            tool_needs=task.tool_needs,
            quality_floor=task.quality_floor,
            deadline_seconds=task.deadline_seconds,
            owner_model_pin=task.owner_model_pin,
            coordinator_model_pin=task.coordinator_model_pin,
            no_providers=task.no_providers,
            allow_parallel=task.allow_parallel,
        )
        decisions.append(rank_workforce(shard_task, workers).as_dict())
    return {
        "repo_id": task.repo_id,
        "task_id": task.task_id,
        "parallel_permitted": bool(task.allow_parallel and not violations),
        "violations": violations,
        "decisions": decisions if task.allow_parallel and not violations else [],
        "policy": {
            "inputs_must_be_disjoint": True,
            "duplicate_accepted_work": "prohibited",
            "queue_mutation": False,
            "worker_launch": False,
        },
    }


__all__ = [
    "CONSERVATIVE_PRIORS",
    "SUPPORTED_ADAPTERS",
    "SUPPORTED_MODELS",
    "TASK_KINDS",
    "CandidateRecord",
    "OutcomeEvidence",
    "TaskRequirements",
    "WorkerCapability",
    "WorkforceDecision",
    "plan_parallel_shards",
    "rank_workforce",
    "select_worker",
]
