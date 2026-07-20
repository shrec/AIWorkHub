from __future__ import annotations

from geoai_task_mcp.workforce_router import (
    OutcomeEvidence,
    TaskRequirements,
    WorkerCapability,
    plan_parallel_shards,
    select_worker,
)


def _task(**overrides):
    values = {
        "task_id": "TASK_B813",
        "repo_id": "repo-alpha",
        "kinds": {"code"},
        "risk": "medium",
        "context_tokens": 80_000,
        "tool_needs": {"filesystem"},
        "quality_floor": 0.60,
    }
    values.update(overrides)
    return TaskRequirements.build(**values)


def _worker(
    worker_id: str,
    model: str,
    provider: str,
    *,
    adapter_id: str = "codex_cli",
    cost: float | None = None,
    accepted: float | None = None,
    review_ready: float | None = None,
    validation_failure: float | None = None,
    p50: float | None = None,
    p95: float | None = None,
    available: bool = True,
    credential_ok: bool = True,
    quota_available: bool = True,
    supports: set[str] | None = None,
    tools: set[str] | None = None,
    max_context_tokens: int = 160_000,
    max_risk: str = "high",
    quality_ceiling: float = 1.0,
):
    return WorkerCapability.build(
        worker_id=worker_id,
        adapter_id=adapter_id,
        model=model,
        provider=provider,
        supports=supports or {"mechanical", "code", "review"},
        tools=tools or {"filesystem"},
        max_context_tokens=max_context_tokens,
        max_risk=max_risk,
        quality_ceiling=quality_ceiling,
        available=available,
        credential_ok=credential_ok,
        quota_available=quota_available,
        evidence=OutcomeEvidence(
            accepted_rate=accepted,
            review_ready_rate=review_ready,
            validation_failure_rate=validation_failure,
            p50_latency_seconds=p50,
            p95_latency_seconds=p95,
            cost_usd_per_1k_tokens=cost,
            estimated_tokens=80_000,
            sample_count=12,
        ),
    )


def test_selects_cheapest_capable_model_without_claude_default():
    decision = select_worker(
        _task(),
        [
            _worker("claude", "claude", "anthropic", adapter_id="claude_cli", cost=9.0, accepted=0.95, review_ready=0.95, validation_failure=0.02),
            _worker("gpt55", "gpt-5.5", "openai", cost=6.0, accepted=0.90, review_ready=0.88, validation_failure=0.03),
            _worker("deepseek", "deepseek-v4-pro", "deepseek", adapter_id="deepseek_copilot_cli", cost=1.0, accepted=0.82, review_ready=0.80, validation_failure=0.05),
            _worker("glm", "glm-5.2", "zhipu", adapter_id="glm_cli", cost=2.0, accepted=0.85, review_ready=0.84, validation_failure=0.04),
        ],
    )

    assert decision.selected_worker_id == "deepseek"
    assert decision.selected_adapter_id == "deepseek_copilot_cli"
    assert decision.selected_model == "deepseek-v4-pro"
    assert decision.fallback_chain == ("deepseek", "glm", "gpt55", "claude")
    assert decision.as_dict()["repo_id"] == "repo-alpha"


def test_missing_evidence_uses_labeled_conservative_priors_and_can_fail_floor():
    decision = select_worker(
        _task(quality_floor=0.51),
        [_worker("unknown", "glm-5.2", "zhipu", adapter_id="glm_cli")],
    )

    candidate = decision.candidates[0].as_dict()
    assert decision.selected_worker_id is None
    assert "observed_or_prior_quality_below_floor" in candidate["exclusion_reasons"]
    assert candidate["score_components"]["evidence_sources"]["accepted_rate"] == "conservative_prior"
    assert candidate["score_components"]["evidence_sources"]["cost_usd_per_1k_tokens"] == "conservative_prior"
    assert candidate["score_components"]["cost_usd_per_1k_tokens"] == 99.0


def test_authoritative_owner_pin_and_no_provider_constraints_are_enforced():
    task = _task(owner_model_pin="gpt-5.5", no_providers={"anthropic"})
    decision = select_worker(
        task,
        [
            _worker("claude", "claude", "anthropic", adapter_id="claude_cli", cost=0.1, accepted=1.0, review_ready=1.0, validation_failure=0.0),
            _worker("deepseek", "deepseek-v4-pro", "deepseek", adapter_id="deepseek_copilot_cli", cost=0.1, accepted=1.0, review_ready=1.0, validation_failure=0.0),
            _worker("gpt55", "gpt-5.5", "openai", cost=5.0, accepted=0.95, review_ready=0.92, validation_failure=0.01),
        ],
    )

    assert decision.selected_worker_id == "gpt55"
    exclusions = {candidate.worker_id: candidate.exclusion_reasons for candidate in decision.candidates}
    assert "provider_forbidden:anthropic" in exclusions["claude"]
    assert "authoritative_model_pin_mismatch:gpt-5.5" in exclusions["deepseek"]


def test_unavailable_credentials_or_quota_are_never_selected():
    decision = select_worker(
        _task(),
        [
            _worker("no-creds", "deepseek-v4-pro", "deepseek", adapter_id="deepseek_copilot_cli", cost=0.1, accepted=1.0, review_ready=1.0, validation_failure=0.0, credential_ok=False),
            _worker("no-quota", "glm-5.2", "zhipu", adapter_id="glm_cli", cost=0.2, accepted=1.0, review_ready=1.0, validation_failure=0.0, quota_available=False),
            _worker("offline", "claude", "anthropic", adapter_id="claude_cli", cost=0.3, accepted=1.0, review_ready=1.0, validation_failure=0.0, available=False),
            _worker("ok", "gpt-5.5", "openai", cost=9.0, accepted=0.9, review_ready=0.9, validation_failure=0.02),
        ],
    )

    assert decision.selected_worker_id == "ok"
    reasons = {candidate.worker_id: candidate.exclusion_reasons for candidate in decision.candidates}
    assert "credentials_missing" in reasons["no-creds"]
    assert "quota_unavailable" in reasons["no-quota"]
    assert "worker_unavailable" in reasons["offline"]


def test_capability_context_tool_risk_and_quality_filters_are_explainable():
    task = _task(kinds={"code", "research"}, risk="high", context_tokens=200_000, tool_needs={"filesystem", "web"}, quality_floor=0.75)
    decision = select_worker(
        task,
        [
            _worker("missing-research", "gpt-5.5", "openai", supports={"code"}, tools={"filesystem", "web"}, accepted=0.9, review_ready=0.9, validation_failure=0.0),
            _worker("too-small", "claude", "anthropic", adapter_id="claude_cli", supports={"code", "research"}, tools={"filesystem", "web"}, max_context_tokens=100_000, accepted=0.9, review_ready=0.9, validation_failure=0.0),
            _worker("no-web", "glm-5.2", "zhipu", adapter_id="glm_cli", supports={"code", "research"}, tools={"filesystem"}, max_context_tokens=300_000, accepted=0.9, review_ready=0.9, validation_failure=0.0),
            _worker("low-risk", "deepseek-v4-pro", "deepseek", adapter_id="deepseek_copilot_cli", supports={"code", "research"}, tools={"filesystem", "web"}, max_context_tokens=300_000, max_risk="medium", accepted=0.9, review_ready=0.9, validation_failure=0.0),
            _worker("low-quality", "gpt-5.5-mini", "openai", supports={"code", "research"}, tools={"filesystem", "web"}, max_context_tokens=300_000, quality_ceiling=0.7, accepted=0.9, review_ready=0.9, validation_failure=0.0),
        ],
    )

    assert decision.selected_worker_id is None
    reasons = {candidate.worker_id: candidate.exclusion_reasons for candidate in decision.candidates}
    assert "missing_capability:research" in reasons["missing-research"]
    assert "context_too_large" in reasons["too-small"]
    assert "missing_tools:web" in reasons["no-web"]
    assert "risk_exceeds_worker_limit" in reasons["low-risk"]
    assert "quality_ceiling_below_floor" in reasons["low-quality"]


def test_parallel_shards_require_disjoint_inputs_and_do_not_launch_or_mutate():
    task = _task(allow_parallel=True, quality_floor=0.6)
    workers = [
        _worker("deepseek", "deepseek-v4-pro", "deepseek", adapter_id="deepseek_copilot_cli", cost=1.0, accepted=0.9, review_ready=0.9, validation_failure=0.02),
        _worker("gpt55", "gpt-5.5", "openai", cost=3.0, accepted=0.9, review_ready=0.9, validation_failure=0.02),
    ]

    ok = plan_parallel_shards(
        task,
        [
            {"shard_id": "a", "source_ids": ["1", "2"]},
            {"shard_id": "b", "source_ids": ["3"]},
        ],
        workers,
    )
    assert ok["parallel_permitted"] is True
    assert [item["selected_worker_id"] for item in ok["decisions"]] == ["deepseek", "deepseek"]
    assert ok["policy"]["worker_launch"] is False
    assert ok["policy"]["queue_mutation"] is False

    duplicate = plan_parallel_shards(
        task,
        [
            {"shard_id": "a", "source_ids": ["1", "2"]},
            {"shard_id": "a", "source_ids": ["2", "3"]},
        ],
        workers,
    )
    assert duplicate["parallel_permitted"] is False
    assert duplicate["decisions"] == []
    assert "duplicate_shard:a" in duplicate["violations"]
    assert "duplicate_source_ids:2" in duplicate["violations"]


def test_decision_record_declares_pure_policy_and_residual_escalation():
    decision = select_worker(
        _task(),
        [_worker("deepseek", "deepseek-v4-pro", "deepseek", adapter_id="deepseek_copilot_cli", cost=1.0, accepted=0.9, review_ready=0.9, validation_failure=0.02)],
    )
    policy = decision.as_dict()["policy"]

    assert policy["pure_policy_only"] is True
    assert policy["queue_mutation"] is False
    assert policy["worker_launch"] is False
    assert policy["duplicate_accepted_work"] == "prohibited"
    assert policy["failed_residuals"] == "escalate_residual_only"
