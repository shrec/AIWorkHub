from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from aiworkhub import (
    core,
    learning_commit,
    model_settings,
    process_launcher,
    provider_route_contracts,
    repo_policy,
    runner_topic_policy,
    workforce_catalog,
    workforce_router,
)


def test_catalog_atomic_write_skips_redundant_chmod_when_already_private(
    tmp_path, monkeypatch,
):
    root = _root(tmp_path)

    def denied(*_args, **_kwargs):
        raise PermissionError("sandbox denies chmod")

    monkeypatch.setattr(workforce_catalog.os, "chmod", denied)
    path, created = workforce_catalog.ensure_catalog(root)

    assert created is True
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    else:
        assert path.is_file()


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


def _preflight() -> dict:
    return {
        "providers": [
            {
                "adapter_id": adapter,
                "launchable": True,
                "status": "ready",
            }
            for adapter in {
                "claude_cli",
                "codex_cli",
                "deepseek_copilot_cli",
                "glm_vscode_lm",
            }
        ]
    }


def test_default_catalog_contains_current_declared_workforce_and_is_idempotent(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path, created = workforce_catalog.ensure_catalog(root)
    assert created is True
    catalog = workforce_catalog.load_catalog(root)
    models = {item["model"] for item in catalog["workers"]}
    assert {"haiku", "sonnet", "opus", "gpt-5.5", "gpt-5.3-codex-spark", "deepseek-v4-pro", "deepseek-v4-flash", "glm-5.2"}.issubset(models)
    assert "opus-4.8" not in models
    expected_tools = ["filesystem", "source-graph", "session-manager", "ai-memory", "kb", "semantic-edit"]
    for worker_id in ("deepseek-v4-pro", "deepseek-v4-flash", "glm-5.2"):
        worker = next(item for item in catalog["workers"] if item["worker_id"] == worker_id)
        assert worker["tools"] == expected_tools
    deepseek = next(item for item in catalog["workers"] if item["worker_id"] == "deepseek-v4-pro")
    assert deepseek["adapter_id"] == "deepseek_vscode_lm"
    same, created_again = workforce_catalog.ensure_catalog(root)
    assert same == path
    assert created_again is False
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_catalog_rejects_invalid_revision_without_leaking_value_error() -> None:
    value = {
        "schema_id": workforce_catalog.SCHEMA_ID,
        "revision": "not-an-integer",
        "workers": [],
    }
    try:
        workforce_catalog.validate_catalog(value)
    except workforce_catalog.WorkforceCatalogError as exc:
        assert str(exc) == "catalog_revision_invalid"
    else:  # pragma: no cover - explicit assertion without pytest dependency
        raise AssertionError("invalid catalog revision was accepted")


def test_manager_upsert_is_bounded_audited_and_preserves_other_workers(tmp_path: Path) -> None:
    root = _root(tmp_path)
    result = workforce_catalog.upsert_worker(
        root,
        {
            "worker_id": "glm-5.2",
            "adapter_id": "glm_vscode_lm",
            "model": "glm-5.2",
            "provider": "zhipu",
            "enabled": True,
            "supports": ["mechanical", "code", "research", "review"],
            "tools": ["filesystem", "source-graph"],
            "max_context_tokens": 1_000_000,
            "max_risk": "high",
            "quality_ceiling": 0.96,
            "manager_score_adjustment": 4.0,
        },
        actor={"role": "manager", "provider": "codex", "actor_id": "private-thread-1234567890"},
    )
    assert result["action"] == "updated"
    catalog = workforce_catalog.load_catalog(root)
    assert len(catalog["workers"]) == len(workforce_catalog.DEFAULT_WORKERS)
    glm = next(item for item in catalog["workers"] if item["worker_id"] == "glm-5.2")
    assert glm["manager_score_adjustment"] == 4.0
    audit = (root / workforce_catalog.AUDIT_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "private-thread-1234567890" not in audit
    assert "glm-5.2" in audit


def test_catalog_scores_only_attributed_canonical_outcomes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    cards = [
        {"task_id": "T1", "status": "finished", "terminal_substatus": "review_ready"},
        {"task_id": "T2", "status": "review", "terminal_substatus": "validation_failed"},
    ]
    processes = [
        {"request_id": "r1", "task_id": "T1", "adapter_id": "deepseek_vscode_lm", "model": "deepseek-v4-pro", "started_at": "2026-07-30T10:00:00+00:00", "finished_at": "2026-07-30T10:01:00+00:00", "total_tokens": 1000, "cost_usd": 0.10},
        {"request_id": "r1-retry", "task_id": "T1", "adapter_id": "deepseek_vscode_lm", "model": "deepseek-v4-pro", "started_at": "2026-07-30T10:02:00+00:00", "finished_at": "2026-07-30T10:04:00+00:00", "total_tokens": 1000, "cost_usd": 0.10},
        {"request_id": "r2", "task_id": "T2", "adapter_id": "deepseek_vscode_lm", "model": "deepseek-v4-pro", "started_at": "2026-07-30T10:00:00+00:00", "finished_at": "2026-07-30T10:03:00+00:00", "total_tokens": 2000, "cost_usd": 0.20},
        {"request_id": "unknown", "task_id": "T3", "adapter_id": "unknown", "model": "unknown"},
    ]
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=cards,
        process_rows=processes,
        preflight=_preflight(),
    )
    deepseek = next(item for item in snapshot["workers"] if item["worker_id"] == "deepseek-v4-pro")
    outcomes = deepseek["outcomes"]
    assert outcomes["sample_count"] == 2
    assert outcomes["attempted_task_count"] == 2
    assert outcomes["infrastructure_failure_count"] == 0
    assert outcomes["attempt_count"] == 3
    assert outcomes["retry_count"] == 1
    assert outcomes["accepted_rate"] == 0.5
    assert outcomes["review_ready_rate"] == 1.0
    assert outcomes["validation_failure_rate"] == 0.5
    assert outcomes["cost_usd_per_1k_tokens"] == 0.1
    assert snapshot["summary"]["unattributed_process_rows"] == 1
    assert snapshot["summary"]["unattributed_missing_model_rows"] == 0
    assert snapshot["summary"]["unattributed_unknown_adapter_or_model_rows"] == 1
    assert snapshot["truth_contract"]["provider_quota_fabricated"] is False
    untouched = next(item for item in snapshot["workers"] if item["worker_id"] == "glm-5.2")
    assert untouched["observed_score"] is None
    assert untouched["outcomes"]["evidence_source"] == "conservative_prior"
    assert untouched["availability_observed"] is False


def test_catalog_uses_canonical_taxonomy_for_infrastructure_substatuses(
    tmp_path: Path,
) -> None:
    """workforce_catalog and learning_commit must never disagree about which
    terminal substatuses are infrastructure failures -- so this exercises a
    substatus (``process_lost``) that only the shared canonical taxonomy
    (not workforce_catalog's old private, narrower set) recognizes.
    """
    assert "process_lost" in learning_commit.INFRASTRUCTURE_TERMINAL_SUBSTATUSES
    root = _root(tmp_path)
    cards = [
        {"task_id": "T1", "status": "blocked", "terminal_substatus": "process_lost"},
        {"task_id": "T2", "status": "finished", "terminal_substatus": "review_ready"},
    ]
    processes = [
        {"request_id": "r1", "task_id": "T1", "adapter_id": "glm_vscode_lm", "model": "glm-5.2"},
        {"request_id": "r2", "task_id": "T2", "adapter_id": "glm_vscode_lm", "model": "glm-5.2"},
    ]
    snapshot = workforce_catalog.build_catalog(
        root, cards=cards, process_rows=processes, preflight=_preflight()
    )
    glm = next(item for item in snapshot["workers"] if item["worker_id"] == "glm-5.2")
    assert glm["outcomes"]["sample_count"] == 1
    assert glm["outcomes"]["attempted_task_count"] == 2
    assert glm["outcomes"]["infrastructure_failure_count"] == 1
    assert glm["outcomes"]["accepted_rate"] == 1.0


def test_infrastructure_failures_do_not_poison_model_quality_evidence(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    cards = [
        {"task_id": "infra", "status": "blocked", "terminal_substatus": "launch_failed"},
        {"task_id": "quality", "status": "finished", "terminal_substatus": "review_ready"},
    ]
    processes = [
        {"request_id": "ri", "task_id": "infra", "adapter_id": "glm_vscode_lm", "model": "glm-5.2"},
        {"request_id": "rq", "task_id": "quality", "adapter_id": "glm_vscode_lm", "model": "glm-5.2"},
    ]

    snapshot = workforce_catalog.build_catalog(
        root, cards=cards, process_rows=processes, preflight=_preflight()
    )
    glm = next(item for item in snapshot["workers"] if item["worker_id"] == "glm-5.2")

    assert glm["outcomes"]["sample_count"] == 1
    assert glm["outcomes"]["attempted_task_count"] == 2
    assert glm["outcomes"]["infrastructure_failure_count"] == 1
    assert glm["outcomes"]["accepted_rate"] == 1.0
    assert glm["outcomes"]["review_ready_rate"] == 1.0
    assert glm["outcomes"]["validation_failure_rate"] == 0.0
    assert glm["observed_score"] == 100.0


def test_sealed_dependency_diagnostic_excludes_card_via_canonical_failure_category(
    tmp_path: Path,
) -> None:
    """Infrastructure exclusion must consume the canonical FailureCategory
    grouping (``learning_commit.INFRASTRUCTURE_FAILURE_CATEGORIES``) through
    the same ``core.classify_terminal_disposition`` the learning-commit path
    uses -- not merely a private terminal_substatus allowlist. A card whose
    terminal_substatus looks candidate-code-shaped (``review_ready``) but
    carries a provider-sealed dependency/route diagnostic is still an
    infrastructure failure and must not poison the model's quality rate.
    """
    root = _root(tmp_path)
    cards = [
        {
            "task_id": "T1",
            "status": "blocked",
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {"provider_error": {
                    "owner": "provider", "sealed": True, "code": "route_unavailable",
                }},
            },
        },
        {"task_id": "T2", "status": "finished", "terminal_substatus": "review_ready"},
    ]
    processes = [
        {"request_id": "r1", "task_id": "T1", "adapter_id": "glm_vscode_lm", "model": "glm-5.2"},
        {"request_id": "r2", "task_id": "T2", "adapter_id": "glm_vscode_lm", "model": "glm-5.2"},
    ]
    snapshot = workforce_catalog.build_catalog(
        root, cards=cards, process_rows=processes, preflight=_preflight()
    )
    glm = next(item for item in snapshot["workers"] if item["worker_id"] == "glm-5.2")
    assert glm["outcomes"]["sample_count"] == 1
    assert glm["outcomes"]["attempted_task_count"] == 2
    assert glm["outcomes"]["infrastructure_failure_count"] == 1
    assert glm["outcomes"]["accepted_rate"] == 1.0


def test_failure_rate_stays_aligned_when_canonical_taxonomy_changes(
    tmp_path: Path, monkeypatch,
) -> None:
    """workforce_catalog's ``validation_failure_rate`` must be derived only
    through the shared canonical classifier
    (``core.classify_terminal_disposition`` +
    ``learning_commit.CODE_QUALITY_FAILURE_CATEGORIES``), never a private
    literal duplicated in workforce_catalog.py. Prove it by widening what the
    canonical taxonomy classifies as CANDIDATE_CODE for a substatus the old
    hardcoded set never recognized, and confirming the catalog's failure rate
    reacts -- with zero changes to workforce_catalog.py itself.
    """
    root = _root(tmp_path)
    cards = [
        {"task_id": "T1", "status": "review", "terminal_substatus": "custom_new_failure_substatus"},
    ]
    processes = [
        {"request_id": "r1", "task_id": "T1", "adapter_id": "glm_vscode_lm", "model": "glm-5.2"},
    ]

    before = workforce_catalog.build_catalog(
        root, cards=cards, process_rows=processes, preflight=_preflight()
    )
    glm_before = next(item for item in before["workers"] if item["worker_id"] == "glm-5.2")
    assert glm_before["outcomes"]["validation_failure_rate"] == 0.0

    real_classify = core.classify_terminal_disposition

    def widened_classify(card):
        if isinstance(card, dict) and card.get("terminal_substatus") == "custom_new_failure_substatus":
            return learning_commit.FailureCategory.CANDIDATE_CODE
        return real_classify(card)

    monkeypatch.setattr(workforce_catalog.core, "classify_terminal_disposition", widened_classify)

    after = workforce_catalog.build_catalog(
        root, cards=cards, process_rows=processes, preflight=_preflight()
    )
    glm_after = next(item for item in after["workers"] if item["worker_id"] == "glm-5.2")
    assert glm_after["outcomes"]["validation_failure_rate"] == 1.0


def test_repository_model_policy_removes_disabled_routes_from_ranking(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    model_settings.update(
        root,
        provider="zhipu",
        enabled=False,
        expected_revision=0,
    )

    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[], preflight=_preflight()
    )
    glm = next(item for item in snapshot["workers"] if item["worker_id"] == "glm-5.2")
    assert glm["policy_enabled"] is False
    assert glm["enabled"] is False
    assert glm["available"] is False
    assert snapshot["truth_contract"]["repository_model_policy_enforced"] is True

    task = workforce_router.TaskRequirements.build(
        task_id="T-policy",
        repo_id="repo",
        kinds=["code"],
        tool_needs=["filesystem"],
    )
    decision = workforce_catalog.rank_task(root, task, catalog=snapshot)
    assert decision["selected_worker_id"] != "glm-5.2"
    glm_candidate = next(
        item for item in decision["candidates"] if item["worker_id"] == "glm-5.2"
    )
    assert glm_candidate["excluded"] is True
    assert "worker_unavailable" in glm_candidate["exclusion_reasons"]


def test_copilot_policy_disables_every_editor_hosted_worker_route(tmp_path: Path) -> None:
    root = _root(tmp_path)
    model_settings.update(
        root,
        provider="copilot",
        enabled=False,
        expected_revision=0,
    )
    preflight = _preflight()
    for row in preflight["providers"]:
        if row["adapter_id"] == "glm_vscode_lm":
            row["observed_models"] = ["glm-5.2", "glm-5.3"]

    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[], preflight=preflight
    )
    editor_rows = [
        row for row in snapshot["workers"]
        if row.get("policy_provider") == "copilot"
    ]
    assert editor_rows
    assert all(row["policy_adapter"] == "vscode_lm" for row in editor_rows)
    assert all(row["policy_enabled"] is False for row in editor_rows)
    assert all(row["available"] is False for row in editor_rows)


def test_copilot_exact_model_switch_does_not_disable_sibling_model(tmp_path: Path) -> None:
    root = _root(tmp_path)
    model_settings.update(
        root,
        provider="copilot",
        adapter="vscode_lm",
        model="glm-5.3",
        enabled=False,
        expected_revision=0,
    )
    preflight = _preflight()
    for row in preflight["providers"]:
        if row["adapter_id"] == "glm_vscode_lm":
            row["observed_models"] = ["glm-5.2", "glm-5.3"]

    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[], preflight=preflight
    )
    by_model = {
        row["model"]: row
        for row in snapshot["workers"]
        if row.get("policy_provider") == "copilot"
        and row["model"] in {"glm-5.2", "glm-5.3"}
    }
    assert by_model["glm-5.2"]["launch_eligible"] is True
    # The sibling model is untouched by the exact-model switch: it is
    # startable, its circuit is closed, and it has no failures, so it is
    # available.  Availability never waited on an observed success.
    assert by_model["glm-5.2"]["available"] is True
    assert by_model["glm-5.2"]["route_health"]["state"] == "closed"
    assert by_model["glm-5.2"]["route_health"]["failure_kind"] == ""
    assert by_model["glm-5.2"]["readiness_status"] == "ready"
    assert by_model["glm-5.3"]["available"] is False


def test_explicit_disabled_discovered_model_overrides_family_projection(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    path, _ = workforce_catalog.ensure_catalog(root)
    catalog = workforce_catalog.load_catalog(root)
    seed = next(
        row for row in catalog["workers"] if row["worker_id"] == "glm-5.2"
    )
    concrete = dict(seed, worker_id="glm-5.3", model="glm-5.3", enabled=False)
    catalog["workers"] = [seed, concrete]
    path.write_text(json.dumps(catalog) + "\n", encoding="utf-8")
    preflight = {
        "providers": [{
            "adapter_id": "glm_vscode_lm",
            "launchable": True,
            "status": "ready",
            "observed_models": ["glm-5.2", "glm-5.3"],
        }]
    }

    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[], preflight=preflight
    )
    relevant = [
        row
        for row in snapshot["workers"]
        if row["adapter_id"] == "glm_vscode_lm"
        and row["model"] in {"glm-5.2", "glm-5.3"}
    ]
    identities = [(row["worker_id"], row["model"]) for row in relevant]
    assert identities == [("glm-5.2", "glm-5.2"), ("glm-5.3", "glm-5.3")]
    assert len(identities) == len(set(identities))
    disabled = next(row for row in relevant if row["model"] == "glm-5.3")
    assert disabled["enabled"] is False
    assert disabled["available"] is False

    task = workforce_router.TaskRequirements.build(
        task_id="T-disabled-discovered",
        repo_id="repo",
        kinds=["code"],
        tool_needs=["filesystem"],
    )
    decision = workforce_catalog.rank_task(root, task, catalog=snapshot)
    candidates = [
        row for row in decision["candidates"] if row["worker_id"] == "glm-5.3"
    ]
    assert len(candidates) == 1
    assert candidates[0]["excluded"] is True
    assert decision["selected_worker_id"] != "glm-5.3"
    # The healthy sibling may well win a contract -- it is startable and its
    # circuit is closed.  The invariant under test is only that an explicitly
    # DISABLED discovered model never does.
    contract = decision["launch_contract"]
    assert contract is None or contract["model"] != "glm-5.3"


def test_explicit_enabled_discovered_model_stays_authoritative_and_dynamic() -> None:
    seed = dict(
        next(
            row
            for row in workforce_catalog.DEFAULT_WORKERS
            if row["worker_id"] == "glm-5.2"
            and row["adapter_id"] == "glm_vscode_lm"
            and row["model"] == "glm-5.2"
        ),
        manager_score_adjustment=1.0,
    )
    concrete = dict(
        seed,
        worker_id="glm-5.3",
        model="glm-5.3",
        manager_score_adjustment=7.0,
    )
    expanded = workforce_catalog._expand_discovered_workers(
        [seed, concrete],
        {"glm_vscode_lm": {"observed_models": ["glm-5.2", "glm-5.3", "glm-5.4"]}},
    )

    assert [(row["worker_id"], row["model"]) for row in expanded] == [
        ("glm-5.2", "glm-5.2"),
        ("glm-5.4", "glm-5.4"),
        ("glm-5.3", "glm-5.3"),
    ]
    by_model = {row["model"]: row for row in expanded}
    assert by_model["glm-5.3"]["manager_score_adjustment"] == 7.0
    assert by_model["glm-5.4"]["manager_score_adjustment"] == 1.0


def test_rank_task_uses_manager_adjustment_without_fabricating_outcomes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    workers = [
        {
            "worker_id": "a", "adapter_id": "codex_cli", "model": "a", "provider": "openai",
            "enabled": True, "supports": ["code"], "tools": ["filesystem"], "max_context_tokens": 1000,
            "max_risk": "high", "quality_ceiling": 1.0, "manager_score_adjustment": -5.0,
            "available": True, "outcomes": {"sample_count": 0},
        },
        {
            "worker_id": "b", "adapter_id": "codex_cli", "model": "b", "provider": "openai",
            "enabled": True, "supports": ["code"], "tools": ["filesystem"], "max_context_tokens": 1000,
            "max_risk": "high", "quality_ceiling": 1.0, "manager_score_adjustment": 5.0,
            "available": True, "outcomes": {"sample_count": 0},
        },
    ]
    task = workforce_router.TaskRequirements.build(
        task_id="T", repo_id="repo", kinds=["code"], tool_needs=["filesystem"]
    )
    decision = workforce_catalog.rank_task(root, task, catalog={"workers": workers})
    assert decision["selected_worker_id"] == "b"
    assert decision["selected_execution_runner"] == "codex_b"
    assert decision["launch_contract"] == {
        "runner": "codex_b",
        "adapter_id": "codex_cli",
        "model": "b",
        "task_id": "T",
        "identity_rule": "use_same_runner_for_task_create_and_agent_launch_task",
    }
    by_id = {item["worker_id"]: item for item in decision["candidates"]}
    assert by_id["a"]["execution_runner"] == "codex_a"
    assert by_id["b"]["execution_runner"] == "codex_b"
    assert by_id["b"]["score_components"]["manager_adjusted_success_rate"] > by_id["a"]["score_components"]["manager_adjusted_success_rate"]


def test_economic_advisory_never_changes_selected_worker(tmp_path: Path) -> None:
    root = _root(tmp_path)
    workers = [
        {
            "worker_id": "selected-by-existing-policy",
            "adapter_id": "codex_cli",
            "model": "model-a",
            "provider": "openai",
            "enabled": True,
            "supports": ["code"],
            "tools": ["filesystem"],
            "max_context_tokens": 1000,
            "max_risk": "high",
            "quality_ceiling": 1.0,
            "manager_score_adjustment": 0.0,
            "available": True,
            "outcomes": {
                "sample_count": 5,
                "accepted_rate": 1.0,
                "review_ready_rate": 1.0,
                "validation_failure_rate": 0.0,
                "cost_usd_per_1k_tokens": 0.1,
                "estimated_tokens_per_attempt": 1000,
            },
            "cost_per_accepted_outcome": {"code": {"medium": {
                "state": "MEASURED",
                "matched_decided_tasks": 5,
                "accepted_outcomes": 5,
                "cost_coverage": 1.0,
                "cost_per_accepted_outcome_usd": 5.0,
            }}},
        },
        {
            "worker_id": "economic-advisory-only",
            "adapter_id": "codex_cli",
            "model": "model-b",
            "provider": "openai",
            "enabled": True,
            "supports": ["code"],
            "tools": ["filesystem"],
            "max_context_tokens": 1000,
            "max_risk": "high",
            "quality_ceiling": 1.0,
            "manager_score_adjustment": 0.0,
            "available": True,
            "outcomes": {
                "sample_count": 5,
                "accepted_rate": 1.0,
                "review_ready_rate": 1.0,
                "validation_failure_rate": 0.0,
                "cost_usd_per_1k_tokens": 0.2,
                "estimated_tokens_per_attempt": 1000,
            },
            "cost_per_accepted_outcome": {"code": {"medium": {
                "state": "MEASURED",
                "matched_decided_tasks": 5,
                "accepted_outcomes": 5,
                "cost_coverage": 1.0,
                "cost_per_accepted_outcome_usd": 1.0,
            }}},
        },
    ]
    task = workforce_router.TaskRequirements.build(
        task_id="economic-advisory",
        repo_id="repo",
        kinds=["code"],
        tool_needs=["filesystem"],
    )

    decision = workforce_catalog.rank_task(
        root, task, catalog={"workers": workers}
    )

    assert decision["selected_worker_id"] == "selected-by-existing-policy"
    assert decision["economic_advisory"]["recommended_worker_id"] == "economic-advisory-only"
    assert decision["economic_advisory"]["automatic_selection_changed"] is False
    assert decision["economic_advisory"]["shadow_eligible"] is False


def test_economic_advisory_excludes_unknown_cost(tmp_path: Path) -> None:
    root = _root(tmp_path)
    workers = [{
        "worker_id": "unknown-cost",
        "adapter_id": "codex_cli",
        "model": "unknown-model",
        "provider": "openai",
        "enabled": True,
        "supports": ["code"],
        "tools": ["filesystem"],
        "max_context_tokens": 1000,
        "max_risk": "high",
        "quality_ceiling": 1.0,
        "manager_score_adjustment": 0.0,
        "available": True,
        "outcomes": {"sample_count": 1},
        "cost_per_accepted_outcome": {"code": {"medium": {
            "state": "UNKNOWN",
            "matched_decided_tasks": 1,
            "accepted_outcomes": 1,
            "cost_coverage": 0.0,
            "cost_per_accepted_outcome_usd": None,
        }}},
    }]
    task = workforce_router.TaskRequirements.build(
        task_id="unknown-advisory",
        repo_id="repo",
        kinds=["code"],
        tool_needs=["filesystem"],
    )

    decision = workforce_catalog.rank_task(
        root, task, catalog={"workers": workers}
    )

    assert decision["economic_advisory"]["recommended_worker_id"] is None
    assert decision["economic_advisory"]["comparable_candidates"] == 0


def test_execution_runner_is_stable_and_never_uses_manager_identity() -> None:
    assert workforce_catalog.execution_runner("glm-5.2", "glm_vscode_lm") == "glm_5.2"
    assert workforce_catalog.execution_runner("deepseek-v4-pro", "deepseek_vscode_lm") == "deepseek_v4-pro"
    assert workforce_catalog.execution_runner("gpt-5.5", "codex_cli") == "codex_gpt-5.5"
    assert workforce_catalog.execution_runner("any", "vscode_lm") == "copilot_any"
    assert workforce_catalog.execution_runner("grok-4.6", "grok_kilo_cli") == "grok_4.6"


def _roles_worker_row(worker_id: str, adapter_id: str, model: str, **extra) -> dict:
    worker = {
        "worker_id": worker_id,
        "adapter_id": adapter_id,
        "model": model,
        "provider": "zhipu",
        "enabled": True,
        "supports": ["mechanical", "code", "research", "review"],
        "tools": [],
        "max_context_tokens": 128_000,
        "max_risk": "medium",
        "quality_ceiling": 0.9,
        "manager_score_adjustment": 0.0,
    }
    worker.update(extra)
    return worker


def _validated_role_rows(*workers: dict) -> list:
    catalog = {
        "schema_id": workforce_catalog.SCHEMA_ID,
        "revision": 1,
        "workers": list(workers),
    }
    return workforce_catalog.validate_catalog(catalog)["workers"]


def test_legacy_rows_get_safe_role_defaults() -> None:
    row = _validated_role_rows(
        _roles_worker_row("glm-5.2", "glm_vscode_lm", "glm-5.2")
    )[0]
    assert row["manager"] is True
    assert row["implementation_worker"] is True
    assert row["reviewer"] is False


def test_explicit_role_booleans_are_preserved_for_ordinary_routes() -> None:
    row = _validated_role_rows(
        _roles_worker_row(
            "glm-5.2",
            "glm_vscode_lm",
            "glm-5.2",
            manager=False,
            implementation_worker=False,
            reviewer=True,
        )
    )[0]
    assert row["manager"] is False
    assert row["implementation_worker"] is False
    assert row["reviewer"] is True


def test_non_boolean_role_values_fail_closed_with_stable_error() -> None:
    for field, bad in (
        ("manager", "yes"),
        ("implementation_worker", 1),
        ("reviewer", None),
    ):
        try:
            _validated_role_rows(
                _roles_worker_row(
                    "glm-5.2", "glm_vscode_lm", "glm-5.2", **{field: bad}
                )
            )
        except workforce_catalog.WorkforceCatalogError as exc:
            assert str(exc) == "worker_roles_invalid"
        else:  # pragma: no cover - explicit assertion without pytest dependency
            raise AssertionError(f"non-boolean {field} role value was accepted")


def test_codex_identities_are_always_manager_only() -> None:
    rows = _validated_role_rows(
        _roles_worker_row(
            "gpt-5.5",
            "codex_cli",
            "gpt-5.5",
            manager=False,
            implementation_worker=True,
            reviewer=True,
        ),
        _roles_worker_row(
            "codex",
            "codex_cli",
            "gpt-5.5",
            manager=False,
            implementation_worker=True,
            reviewer=True,
        ),
    )
    assert [row["worker_id"] for row in rows] == ["gpt-5.5", "codex"]
    for row in rows:
        assert row["manager"] is True
        assert row["implementation_worker"] is False
        assert row["reviewer"] is False


def test_default_catalog_declares_exact_grok_kilo_route() -> None:
    worker = next(
        row
        for row in workforce_catalog.DEFAULT_WORKERS
        if row["worker_id"] == "grok-4.6"
    )
    assert worker["adapter_id"] == "grok_kilo_cli"
    assert worker["model"] == "xai/grok-4.6"
    assert worker["provider"] == "xai"


def test_existing_catalog_gains_grok_without_overwriting_repository_choices(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    legacy = workforce_catalog._default_catalog()
    legacy["workers"] = [
        row for row in legacy["workers"] if row["worker_id"] != "grok-4.6"
    ]
    legacy["workers"][0]["enabled"] = False
    path = workforce_catalog.catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = workforce_catalog.load_catalog(root)

    assert loaded["configured"] is True
    assert next(
        row for row in loaded["workers"] if row["worker_id"] == legacy["workers"][0]["worker_id"]
    )["enabled"] is False
    grok = next(row for row in loaded["workers"] if row["worker_id"] == "grok-4.6")
    assert grok["adapter_id"] == "grok_kilo_cli"
    assert grok["enabled"] is True


def test_deepseek_uses_launchable_copilot_fallback_without_identity_drift(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={
            "providers": [
                {"adapter_id": "deepseek_vscode_lm", "launchable": False, "status": "not_visible"},
                {"adapter_id": "deepseek_copilot_cli", "launchable": True, "status": "ready", "access_observed": True},
            ]
        },
    )
    worker = next(row for row in snapshot["workers"] if row["worker_id"] == "deepseek-v4-pro")
    assert worker["launch_eligible"] is True
    assert worker["available"] is True
    assert worker["route_health"]["state"] == "closed"
    assert worker["readiness_status"] == "ready"
    assert worker["adapter_id"] == "deepseek_vscode_lm"
    assert worker["effective_adapter_id"] == "deepseek_copilot_cli"
    assert worker["adapter_fallback_used"] is True
    assert worker["provider"] == "deepseek"
    assert worker["model"] == "deepseek-v4-pro"


def test_glm_uses_launchable_copilot_fallback(tmp_path: Path) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={
            "providers": [
                {"adapter_id": "glm_vscode_lm", "launchable": False, "status": "not_visible"},
                {"adapter_id": "glm_copilot_cli", "launchable": True, "status": "ready", "access_observed": True},
            ]
        },
    )
    worker = next(row for row in snapshot["workers"] if row["worker_id"] == "glm-5.2")
    assert worker["launch_eligible"] is True
    assert worker["available"] is True
    assert worker["route_health"]["state"] == "closed"
    assert worker["readiness_status"] == "ready"
    assert worker["effective_adapter_id"] == "glm_copilot_cli"
    assert worker["adapter_fallback_used"] is True


def test_first_party_claude_never_falls_back_to_editor_authorization(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={
            "providers": [
                {"adapter_id": "claude_cli", "launchable": False, "status": "not_installed"},
                {
                    "adapter_id": "vscode_lm",
                    "launchable": True,
                    "status": "ready",
                    "access_observed": True,
                    "observed_models": ["claude-sonnet-5", "gpt-5.5"],
                },
            ]
        },
    )
    sonnet = next(row for row in snapshot["workers"] if row["worker_id"] == "claude-sonnet-5")
    haiku = next(row for row in snapshot["workers"] if row["worker_id"] == "claude-haiku")
    assert sonnet["available"] is False
    assert sonnet["effective_adapter_id"] == "claude_cli"
    assert sonnet["adapter_fallback_used"] is False
    assert haiku["available"] is False


def test_rank_task_does_not_substitute_editor_auth_for_first_party_claude(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={
            "providers": [
                {"adapter_id": "claude_cli", "launchable": False, "status": "not_installed"},
                {
                    "adapter_id": "vscode_lm",
                    "launchable": True,
                    "status": "ready",
                    "access_observed": True,
                    "observed_models": ["claude-sonnet-5"],
                },
            ]
        },
    )
    task = workforce_router.TaskRequirements.build(
        task_id="T-editor",
        repo_id="repo",
        kinds=["linguistic"],
        risk="high",
        owner_model_pin="sonnet",
        tool_needs=["source-graph"],
    )
    decision = workforce_catalog.rank_task(root, task, catalog=snapshot)
    assert decision["selected_worker_id"] is None
    assert decision["selected_adapter_id"] is None


def test_successful_attributed_outcome_establishes_access_observation(tmp_path: Path) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[{"task_id": "T1", "status": "finished", "terminal_substatus": "review_ready"}],
        process_rows=[{
            "request_id": "r1", "task_id": "T1", "adapter_id": "codex_cli",
            "model": "gpt-5.5", "total_tokens": 500,
        }],
        preflight={"providers": [{"adapter_id": "codex_cli", "launchable": True, "status": "ready"}]},
    )
    worker = next(row for row in snapshot["workers"] if row["worker_id"] == "gpt-5.5")
    assert worker["outcomes"]["sample_count"] == 1
    assert worker["availability_observation"]["historical_quality_cards"] == 1
    assert worker["availability_observation"]["proves_round_trip"] is False
    assert worker["availability_observed"] is False
    assert worker["round_trip_observed"] == "unknown"


def test_codex_historical_success_cannot_override_unverified_current_model_access(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[{
            "task_id": "T1",
            "status": "finished",
            "terminal_substatus": "review_ready",
        }],
        process_rows=[{
            "request_id": "r1",
            "task_id": "T1",
            "adapter_id": "codex_cli",
            "model": "gpt-5.3-codex",
        }],
        preflight={
            "providers": [{
                "adapter_id": "codex_cli",
                "launchable": True,
                "access_observed": False,
                "status": "installed_unverified_access",
            }]
        },
    )

    worker = next(
        row for row in snapshot["workers"]
        if row["worker_id"] == "gpt-5.3-codex"
    )
    assert worker["outcomes"]["sample_count"] == 1
    assert worker["availability_observed"] is False
    assert worker["round_trip_observed"] == "unknown"
    assert worker["available"] is False
    assert worker["readiness_status"] == "model_access_unverified"

    task = workforce_router.TaskRequirements.build(
        task_id="T-codex-unverified",
        repo_id="repo",
        kinds=["code"],
        risk="high",
        owner_model_pin="gpt-5.3-codex",
        tool_needs=["source-graph"],
    )
    decision = workforce_catalog.rank_task(root, task, catalog=snapshot)
    assert decision["selected_worker_id"] is None
    assert decision["launch_contract"] is None


def test_codex_worker_requires_current_exact_model_capability_receipt(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={
            "providers": [{
                "adapter_id": "codex_cli",
                "launchable": True,
                "access_observed": True,
                "observed_models": ["gpt-5.5"],
                "status": "ready",
            }]
        },
    )

    supported = next(
        row for row in snapshot["workers"] if row["worker_id"] == "gpt-5.5"
    )
    unsupported = next(
        row for row in snapshot["workers"]
        if row["worker_id"] == "gpt-5.3-codex"
    )
    assert supported["available"] is True
    assert unsupported["available"] is False


def test_codex_capability_model_ids_are_matched_exactly(tmp_path: Path) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={"providers": [{
            "adapter_id": "codex_cli",
            "launchable": True,
            "access_observed": True,
            "observed_models": ["openai/gpt-5.5"],
            "status": "ready_unverified",
        }]},
    )
    worker = next(
        row for row in snapshot["workers"] if row["worker_id"] == "gpt-5.5"
    )
    assert worker["available"] is False


def test_disabled_copilot_policy_never_becomes_codex_effective_fallback(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    model_settings.update(
        root, provider="copilot", enabled=False, expected_revision=0
    )
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={"providers": [
            {
                "adapter_id": "codex_cli",
                "launchable": False,
                "access_observed": False,
                "status": "access_unavailable",
            },
            {
                "adapter_id": "vscode_lm",
                "launchable": True,
                "access_observed": True,
                "observed_models": ["gpt-5.5"],
                "status": "ready_unverified",
            },
        ]},
    )
    worker = next(
        row for row in snapshot["workers"] if row["worker_id"] == "gpt-5.5"
    )
    assert worker["effective_adapter_id"] == "codex_cli"
    assert worker["adapter_fallback_used"] is False
    assert worker["available"] is False


def test_disabled_copilot_parent_wins_over_enabled_exact_fallback_model(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    model_settings.update(
        root, provider="copilot", enabled=False, expected_revision=0
    )
    model_settings.update(
        root,
        provider="copilot",
        adapter="vscode_lm",
        model="gpt-5.5",
        enabled=True,
        expected_revision=1,
    )
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={"providers": [
            {"adapter_id": "codex_cli", "launchable": False},
            {
                "adapter_id": "vscode_lm",
                "launchable": True,
                "access_observed": True,
                "observed_models": ["gpt-5.5"],
                "status": "ready_unverified",
            },
        ]},
    )
    worker = next(
        row for row in snapshot["workers"] if row["worker_id"] == "gpt-5.5"
    )
    assert worker["effective_adapter_id"] == "codex_cli"
    assert worker["available"] is False


def test_explicitly_enabled_copilot_codex_fallback_remains_labeled(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    model_settings.update(
        root, provider="copilot", enabled=True, expected_revision=0
    )
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={"providers": [
            {
                "adapter_id": "codex_cli",
                "launchable": False,
                "access_observed": False,
                "status": "access_unavailable",
            },
            {
                "adapter_id": "vscode_lm",
                "launchable": True,
                "access_observed": True,
                "observed_models": ["gpt-5.5"],
                "status": "ready_unverified",
            },
        ]},
    )
    worker = next(
        row for row in snapshot["workers"] if row["worker_id"] == "gpt-5.5"
    )
    assert worker["effective_adapter_id"] == "vscode_lm"
    assert worker["adapter_fallback_used"] is True
    assert worker["policy_provider"] == "copilot"
    assert worker["available"] is True


def test_disabled_copilot_concrete_model_from_catalog_is_not_selectable(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    model_settings.update(
        root,
        provider="copilot",
        adapter="vscode_lm",
        model="gpt-5.5",
        enabled=False,
        expected_revision=0,
    )
    preflight = {"providers": [
        {
            "adapter_id": "codex_cli",
            "launchable": False,
            "status": "access_unavailable",
        },
        {
            "adapter_id": "vscode_lm",
            "launchable": True,
            "access_observed": True,
            "observed_models": ["gpt-5.5"],
            "provider_observed_models": ["gpt-5.5"],
            "status": "ready_unverified",
        },
    ]}

    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[], preflight=preflight
    )
    worker = next(
        row for row in snapshot["workers"] if row["worker_id"] == "gpt-5.5"
    )
    assert worker["policy_provider"] == "openai"
    assert worker["policy_adapter"] == "codex_cli"
    assert worker["effective_adapter_id"] == "codex_cli"
    assert worker["available"] is False

    task = workforce_router.TaskRequirements.build(
        task_id="T-disabled-copilot-model",
        repo_id="repo",
        kinds=["code"],
        risk="critical",
        owner_model_pin="gpt-5.5",
        tool_needs=["source-graph"],
    )
    decision = workforce_catalog.rank_task(root, task, catalog=snapshot)
    assert decision["selected_worker_id"] is None
    assert decision["launch_contract"] is None


def test_enabled_deepseek_copilot_route_survives_disabled_copilot_catalog_model(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    model_settings.update(
        root,
        provider="copilot",
        adapter="vscode_lm",
        model="deepseek-v4-pro",
        enabled=False,
        expected_revision=0,
    )
    model_settings.update(
        root,
        provider="deepseek",
        adapter="deepseek_copilot_cli",
        model="deepseek-v4-pro",
        enabled=True,
        expected_revision=1,
    )

    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={"providers": [
            {
                "adapter_id": "deepseek_vscode_lm",
                "launchable": False,
                "status": "repository_model_policy_disabled",
            },
            {
                "adapter_id": "vscode_lm",
                "launchable": True,
                "access_observed": True,
                "observed_models": [],
                "provider_observed_models": ["deepseek-v4-pro"],
                "status": "repository_model_policy_disabled",
            },
            {
                "adapter_id": "deepseek_copilot_cli",
                "launchable": True,
                "access_observed": True,
                "status": "ready_unverified",
            },
        ]},
    )
    worker = next(
        row for row in snapshot["workers"] if row["worker_id"] == "deepseek-v4-pro"
    )
    assert worker["effective_adapter_id"] == "deepseek_copilot_cli"
    assert worker["policy_provider"] == "deepseek"
    assert worker["policy_enabled"] is True
    assert worker["launch_eligible"] is True
    assert worker["available"] is True
    assert worker["route_health"]["state"] == "closed"
    assert worker["readiness_status"] == "ready_unverified"


def test_canonical_usage_rows_supply_tokens_and_labeled_unknown_cost(tmp_path: Path) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[{"task_id": "T1", "status": "finished", "terminal_substatus": "review_ready"}],
        process_rows=[{
            "request_id": "r1", "task_id": "T1", "runner": "codex_runner",
            "adapter_id": "codex_cli", "model": "gpt-5.5",
        }],
        usage_rows=[{
            "task_id": "T1", "runner": "codex_runner", "model": "gpt-5.5",
            "total_tokens": 1702755, "cost_usd": 0.0, "cost_known": False,
        }],
        preflight={"providers": [{"adapter_id": "codex_cli", "launchable": True, "status": "ready"}]},
    )
    worker = next(row for row in snapshot["workers"] if row["worker_id"] == "gpt-5.5")
    assert worker["outcomes"]["total_tokens"] == 1702755
    assert worker["outcomes"]["cost_usd"] is None
    assert worker["outcomes"]["tokens_with_unknown_cost"] == 1702755


def test_effective_cost_rate_excludes_tokens_with_unknown_cost(tmp_path: Path) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[
            {"task_id": "T1", "status": "finished", "terminal_substatus": "review_ready"},
            {"task_id": "T2", "status": "finished", "terminal_substatus": "review_ready"},
        ],
        process_rows=[
            {
                "request_id": "r1", "task_id": "T1", "runner": "codex_runner",
                "adapter_id": "codex_cli", "model": "gpt-5.5",
            },
            {
                "request_id": "r2", "task_id": "T2", "runner": "codex_runner",
                "adapter_id": "codex_cli", "model": "gpt-5.5",
            },
        ],
        usage_rows=[
            {
                "task_id": "T1", "runner": "codex_runner", "model": "gpt-5.5",
                "total_tokens": 1_000, "cost_usd": 1.0, "cost_known": True,
            },
            {
                "task_id": "T2", "runner": "codex_runner", "model": "gpt-5.5",
                "total_tokens": 9_000, "cost_usd": 0.0, "cost_known": False,
            },
        ],
        preflight={
            "providers": [{
                "adapter_id": "codex_cli", "launchable": True, "status": "ready",
            }],
        },
    )

    worker = next(row for row in snapshot["workers"] if row["worker_id"] == "gpt-5.5")
    outcomes = worker["outcomes"]
    assert outcomes["cost_known_records"] == 1
    assert outcomes["cost_unknown_records"] == 1
    assert outcomes["tokens_with_known_cost"] == 1_000
    assert outcomes["tokens_with_unknown_cost"] == 9_000
    assert outcomes["cost_usd_per_1k_tokens"] == 1.0


def test_missing_process_identity_is_recovered_only_from_canonical_terminal_evidence(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[{
            "task_id": "T1",
            "runner": "deepseek_runner",
            "status": "finished",
            "terminal_substatus": "review_ready",
            "terminal_review": {"evidence": {
                "model": "deepseek-v4-pro",
                "adapter_id": "deepseek_copilot_cli",
            }},
        }],
        process_rows=[{"request_id": "r1", "task_id": "T1"}],
        preflight={"providers": [{
            "adapter_id": "deepseek_copilot_cli", "launchable": True, "status": "ready"
        }]},
    )
    worker = next(row for row in snapshot["workers"] if row["worker_id"] == "deepseek-v4-pro")
    assert worker["outcomes"]["sample_count"] == 1
    assert snapshot["summary"]["process_identity_recovered_rows"] == 1
    assert snapshot["summary"]["unattributed_process_rows"] == 0


def _route_failure_row(
    *, request_id: str, model: str, state: str, error: str, epoch: float,
) -> dict:
    return {
        "request_id": request_id,
        "task_id": f"task-{request_id}",
        "adapter_id": "deepseek_vscode_lm",
        "model": model,
        "state": state,
        "error": error,
        "finished_at": datetime.fromtimestamp(
            epoch, tz=timezone.utc
        ).isoformat(),
    }


def _deepseek_preflight() -> dict:
    return {"providers": [{
        "adapter_id": "deepseek_vscode_lm",
        "launchable": True,
        "status": "ready",
        "observed_models": ["deepseek-v4-pro", "deepseek-v4-flash"],
    }]}


def test_route_circuit_is_exact_adapter_model_and_never_shared_mcp(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    now = 2_000_000_000.0
    failures = [
        _route_failure_row(
            request_id="failure-1", model="deepseek-v4-pro",
            state="worker_failed", error="mcp_request_timeout",
            epoch=now - 20,
        ),
        _route_failure_row(
            request_id="failure-2", model="deepseek-v4-pro",
            state="worker_failed", error="no_terminal_event",
            epoch=now - 10,
        ),
    ]

    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=failures,
        preflight=_deepseek_preflight(), now_epoch=now,
    )
    pro = next(
        row for row in snapshot["workers"]
        if row["worker_id"] == "deepseek-v4-pro"
    )
    flash = next(
        row for row in snapshot["workers"]
        if row["worker_id"] == "deepseek-v4-flash"
    )

    assert pro["available"] is False
    assert pro["readiness_status"] == "route_circuit_open"
    assert pro["route_health"]["state"] == "open"
    assert pro["route_health"]["consecutive_failures"] == 2
    assert pro["route_health"]["scope"] == "exact_adapter_and_model"
    assert pro["route_health"]["mcp_control_plane_affected"] is False
    assert flash["launch_eligible"] is True
    assert flash["available"] is True
    assert flash["route_health"]["state"] == "closed"

    task = workforce_router.TaskRequirements.build(
        task_id="route-local-fallback",
        repo_id="repo",
        kinds=["mechanical", "code"],
        risk="medium",
        tool_needs=["source-graph"],
    )
    decision = workforce_catalog.rank_task(root, task, catalog=snapshot)
    # The tripped route is excluded.  Its healthy sibling is not: an open
    # circuit on one exact route must never starve the whole workforce.
    assert decision["selected_worker_id"] != "deepseek-v4-pro"
    pro_candidate = next(
        item for item in decision["candidates"]
        if item["worker_id"] == "deepseek-v4-pro"
    )
    assert pro_candidate["excluded"] is True
    assert "worker_unavailable" in pro_candidate["exclusion_reasons"]


def test_route_success_resets_transient_circuit_and_auth_circuit_half_opens(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    now = 2_000_000_000.0
    rows = [
        _route_failure_row(
            request_id="old-1", model="deepseek-v4-pro",
            state="worker_failed", error="mcp_request_timeout",
            epoch=now - 30,
        ),
        _route_failure_row(
            request_id="old-2", model="deepseek-v4-pro",
            state="timed_out", error="provider_timeout",
            epoch=now - 20,
        ),
        _route_failure_row(
            request_id="success", model="deepseek-v4-pro",
            state="validation_failed", error="downstream test failed",
            epoch=now - 10,
        ),
    ]
    reset = workforce_catalog.build_catalog(
        root, cards=[], process_rows=rows,
        preflight=_deepseek_preflight(), now_epoch=now,
    )
    pro = next(
        row for row in reset["workers"]
        if row["worker_id"] == "deepseek-v4-pro"
    )
    assert pro["available"] is True
    assert pro["route_health"]["state"] == "closed"
    assert pro["route_health"]["consecutive_failures"] == 0

    # Only a sealed, provider-owned auth error half-opens after cooldown; the
    # free-form error string is never classified.
    auth_failure = [_sealed_route_row(
        request_id="auth", model="deepseek-v4-pro",
        code="invalid_grant", http_status=401, epoch=now - 700,
    )]
    cooled = workforce_catalog.build_catalog(
        root, cards=[], process_rows=auth_failure,
        preflight=_deepseek_preflight(), now_epoch=now,
    )
    pro = next(
        row for row in cooled["workers"]
        if row["worker_id"] == "deepseek-v4-pro"
    )
    assert pro["launch_eligible"] is True
    # Half-open is the breaker's retry state: the cooldown elapsed, so the
    # route may be tried again and is available.
    assert pro["available"] is True
    assert pro["route_health"]["state"] == "half_open"
    assert pro["route_health"]["failure_kind"] == "auth"


def _sealed_route_row(
    *, request_id: str, model: str, epoch: float, code: str = "",
    http_status=None, owner: str = "provider", sealed: bool = True,
    state: str = "worker_failed",
) -> dict:
    provider_error: dict = {"owner": owner, "sealed": sealed}
    if code:
        provider_error["code"] = code
    if http_status is not None:
        provider_error["http_status"] = http_status
    return {
        "request_id": request_id,
        "task_id": f"task-{request_id}",
        "adapter_id": "deepseek_vscode_lm",
        "model": model,
        "state": state,
        "provider_error": provider_error,
        "finished_at": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(),
    }


def test_authenticated_http_402_quota_opens_exact_route_after_one_failure(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    now = 2_000_000_000.0
    rows = [_sealed_route_row(
        request_id="quota", model="deepseek-v4-pro",
        code="insufficient_balance", http_status=402, epoch=now - 15,
    )]

    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=rows,
        preflight=_deepseek_preflight(), now_epoch=now,
    )
    pro = next(
        row for row in snapshot["workers"] if row["worker_id"] == "deepseek-v4-pro"
    )
    flash = next(
        row for row in snapshot["workers"] if row["worker_id"] == "deepseek-v4-flash"
    )

    assert pro["available"] is False
    assert pro["readiness_status"] == "route_circuit_open"
    assert pro["route_health"]["state"] == "open"
    assert pro["route_health"]["failure_kind"] == "quota"
    assert pro["route_health"]["consecutive_failures"] == 1
    assert pro["route_health"]["threshold"] == 1
    assert pro["route_health"]["scope"] == "exact_adapter_and_model"
    assert pro["route_health"]["mcp_control_plane_affected"] is False
    # Sibling model on the same adapter stays healthy and rankable.
    assert flash["launch_eligible"] is True
    assert flash["available"] is True
    assert flash["route_health"]["state"] == "closed"

    task = workforce_router.TaskRequirements.build(
        task_id="quota-route-local-fallback",
        repo_id="repo",
        kinds=["mechanical", "code"],
        risk="medium",
        tool_needs=["source-graph"],
    )
    decision = workforce_catalog.rank_task(root, task, catalog=snapshot)
    # The quota-failed route is excluded.  Its healthy sibling is not: one
    # route tripping must never starve the whole workforce.
    assert decision["selected_worker_id"] != "deepseek-v4-pro"
    pro_candidate = next(
        item for item in decision["candidates"]
        if item["worker_id"] == "deepseek-v4-pro"
    )
    assert pro_candidate["excluded"] is True
    assert "worker_unavailable" in pro_candidate["exclusion_reasons"]


def test_invalid_grant_and_unknown_refresh_token_open_auth_route_after_one(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    now = 2_000_000_000.0
    for code in ("invalid_grant", "unknown_refresh_token"):
        rows = [_sealed_route_row(
            request_id=code, model="deepseek-v4-pro", code=code, epoch=now - 15,
        )]
        snapshot = workforce_catalog.build_catalog(
            root, cards=[], process_rows=rows,
            preflight=_deepseek_preflight(), now_epoch=now,
        )
        pro = next(
            row for row in snapshot["workers"]
            if row["worker_id"] == "deepseek-v4-pro"
        )
        assert pro["available"] is False, code
        assert pro["route_health"]["state"] == "open", code
        assert pro["route_health"]["failure_kind"] == "auth", code
        assert pro["route_health"]["consecutive_failures"] == 1, code
        assert pro["route_health"]["threshold"] == 1, code


def test_only_sealed_provider_errors_classify_not_prose_or_spoofing(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    now = 2_000_000_000.0
    # 1) Free-form prose carrying the quota/auth substrings but no sealed
    #    provider error must never trip the circuit.
    prose = [_route_failure_row(
        request_id="prose", model="deepseek-v4-pro", state="worker_failed",
        error="insufficient_balance http_status=402 invalid_grant",
        epoch=now - 15,
    )]
    # 2) A structured error the model attributes to itself (not sealed by the
    #    provider transport) is equally untrusted.
    spoofed = [_sealed_route_row(
        request_id="spoof", model="deepseek-v4-pro",
        code="insufficient_balance", http_status=402, epoch=now - 15,
        owner="model", sealed=False,
    )]
    for rows, label in ((prose, "prose"), (spoofed, "spoofed")):
        snapshot = workforce_catalog.build_catalog(
            root, cards=[], process_rows=rows,
            preflight=_deepseek_preflight(), now_epoch=now,
        )
        pro = next(
            row for row in snapshot["workers"]
            if row["worker_id"] == "deepseek-v4-pro"
        )
        assert pro["launch_eligible"] is True, label
        assert pro["available"] is True, label
        assert pro["route_health"]["state"] == "closed", label
        assert pro["route_health"]["failure_kind"] == "", label
        assert pro["route_health"]["consecutive_failures"] == 0, label


def test_validation_finalize_and_review_ready_do_not_penalize_route(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    now = 2_000_000_000.0
    rows = [
        _route_failure_row(
            request_id="v", model="deepseek-v4-pro", state="validation_failed",
            error="downstream test failed", epoch=now - 30,
        ),
        _route_failure_row(
            request_id="f", model="deepseek-v4-pro", state="finalize_failed",
            error="finalize step failed", epoch=now - 20,
        ),
        _route_failure_row(
            request_id="r", model="deepseek-v4-pro", state="review_ready",
            error="", epoch=now - 10,
        ),
    ]
    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=rows,
        preflight=_deepseek_preflight(), now_epoch=now,
    )
    pro = next(
        row for row in snapshot["workers"] if row["worker_id"] == "deepseek-v4-pro"
    )
    assert pro["available"] is True
    assert pro["route_health"]["state"] == "closed"
    assert pro["route_health"]["consecutive_failures"] == 0
    assert pro["route_health"]["failure_kind"] == ""


def test_authenticated_success_deterministically_closes_open_quota_circuit(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    now = 2_000_000_000.0
    rows = [
        _sealed_route_row(
            request_id="quota", model="deepseek-v4-pro",
            code="quota_exhausted", http_status=402, epoch=now - 120,
        ),
        _route_failure_row(
            request_id="recovered", model="deepseek-v4-pro",
            state="review_ready", error="", epoch=now - 10,
        ),
    ]
    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=rows,
        preflight=_deepseek_preflight(), now_epoch=now,
    )
    pro = next(
        row for row in snapshot["workers"] if row["worker_id"] == "deepseek-v4-pro"
    )
    assert pro["available"] is True
    assert pro["route_health"]["state"] == "closed"
    assert pro["route_health"]["failure_kind"] == ""
    assert pro["route_health"]["consecutive_failures"] == 0


def test_unsealed_auth_error_prose_never_opens_route_circuit(
    tmp_path: Path,
) -> None:
    # Free-form error text carrying auth markers (including HTTP 401/403 and
    # mixed spoof prose) must never classify: 'auth' is single-failure, so a
    # spoofed string could otherwise open the exact route after one message.
    root = _root(tmp_path)
    now = 2_000_000_000.0
    for error in (
        "unauthorized",
        "invalid_api_key",
        "authentication_failed http_status=401",
        "authorization_failed http_status=403",
        "sorry, the model returned: 401 unauthorized invalid_api_key invalid_grant",
    ):
        rows = [_route_failure_row(
            request_id="spoof", model="deepseek-v4-pro",
            state="worker_failed", error=error, epoch=now - 15,
        )]
        snapshot = workforce_catalog.build_catalog(
            root, cards=[], process_rows=rows,
            preflight=_deepseek_preflight(), now_epoch=now,
        )
        pro = next(
            row for row in snapshot["workers"]
            if row["worker_id"] == "deepseek-v4-pro"
        )
        assert pro["launch_eligible"] is True, error
        # Prose never trips a circuit, so nothing was measured failing and
        # the route stays available on the unmeasured case.
        assert pro["available"] is True, error
        assert pro["route_health"]["state"] == "closed", error
        assert pro["route_health"]["failure_kind"] == "", error
        assert pro["route_health"]["consecutive_failures"] == 0, error


def test_sealed_auth_401_403_open_only_exact_route_and_sealed_success_recovers(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    now = 2_000_000_000.0
    for http_status, code in ((401, ""), (403, ""), (None, "unauthorized")):
        rows = [_sealed_route_row(
            request_id="auth", model="deepseek-v4-pro",
            code=code, http_status=http_status, epoch=now - 15,
        )]
        snapshot = workforce_catalog.build_catalog(
            root, cards=[], process_rows=rows,
            preflight=_deepseek_preflight(), now_epoch=now,
        )
        pro = next(
            row for row in snapshot["workers"]
            if row["worker_id"] == "deepseek-v4-pro"
        )
        flash = next(
            row for row in snapshot["workers"]
            if row["worker_id"] == "deepseek-v4-flash"
        )
        assert pro["available"] is False, (http_status, code)
        assert pro["route_health"]["state"] == "open", (http_status, code)
        assert pro["route_health"]["failure_kind"] == "auth", (http_status, code)
        assert pro["route_health"]["consecutive_failures"] == 1, (http_status, code)
        assert pro["route_health"]["threshold"] == 1, (http_status, code)
        # Sibling model and the shared control plane stay healthy.
        assert flash["launch_eligible"] is True, (http_status, code)
        # The sibling has no failures of its own, so its circuit is closed
        # and it stays available -- isolation is per exact route.
        assert flash["available"] is True, (http_status, code)
        assert flash["route_health"]["state"] == "closed", (http_status, code)
        assert flash["route_health"]["failure_kind"] == "", (http_status, code)
        assert pro["route_health"]["mcp_control_plane_affected"] is False

    # A later sealed authenticated success deterministically closes the circuit.
    recovered = [
        _sealed_route_row(
            request_id="auth", model="deepseek-v4-pro",
            http_status=401, epoch=now - 120,
        ),
        _route_failure_row(
            request_id="ok", model="deepseek-v4-pro",
            state="review_ready", error="", epoch=now - 10,
        ),
    ]
    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=recovered,
        preflight=_deepseek_preflight(), now_epoch=now,
    )
    pro = next(
        row for row in snapshot["workers"]
        if row["worker_id"] == "deepseek-v4-pro"
    )
    assert pro["available"] is True
    assert pro["route_health"]["state"] == "closed"
    assert pro["route_health"]["failure_kind"] == ""
    assert pro["route_health"]["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# NF-2026-00549 slice A: one canonical runner-id grammar shared with
# runner_topic_policy, workforce-upsert normalization, and a catalog-to-launcher
# route parity regression.
# ---------------------------------------------------------------------------


def _opus_worker(**overrides) -> dict:
    worker = {
        "adapter_id": "claude_cli",
        "model": "opus",
        "provider": "anthropic",
        "enabled": True,
        "supports": ["code", "research", "linguistic", "review"],
        "tools": ["filesystem", "source-graph"],
        "max_context_tokens": 1_000_000,
        "max_risk": "critical",
        "quality_ceiling": 1.0,
        "manager_score_adjustment": 3.0,
    }
    worker.update(overrides)
    return worker


def test_all_registered_catalog_runner_ids_are_byte_stable_under_grammar() -> None:
    registered = [
        workforce_catalog.execution_runner(item["worker_id"], item["adapter_id"])
        for item in workforce_catalog.DEFAULT_WORKERS
    ]
    # No two registered runner ids share a fold key, and each resolves to
    # itself byte-for-byte -- no existing enabled route changes identity.
    assert len(registered) == len(set(registered))
    for runner in registered:
        assert runner_topic_policy.canonical_runner_id(runner, registered) == runner


def test_upsert_normalizes_variant_runner_spelling_onto_registered_id(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    workforce_catalog.ensure_catalog(root)
    count = len(workforce_catalog.load_catalog(root)["workers"])

    result = workforce_catalog.upsert_worker(
        root,
        _opus_worker(worker_id="claude_opus_5"),  # underscore variant
        actor={"role": "manager", "provider": "codex", "actor_id": "x"},
    )

    assert result["action"] == "updated"
    assert result["worker_id"] == "claude-opus-5"
    catalog = workforce_catalog.load_catalog(root)
    worker_ids = {item["worker_id"] for item in catalog["workers"]}
    assert "claude_opus_5" not in worker_ids  # variant spelling never persisted
    assert len(catalog["workers"]) == count  # no divergent second identity
    opus = next(
        item for item in catalog["workers"] if item["worker_id"] == "claude-opus-5"
    )
    assert opus["manager_score_adjustment"] == 3.0


def test_upsert_rejects_unresolvable_runner_variant_identity_conflict(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    workforce_catalog.ensure_catalog(root)
    try:
        # Folds onto registered claude_opus-5 but pins a conflicting model.
        workforce_catalog.upsert_worker(
            root,
            _opus_worker(worker_id="claude_opus_5", model="sonnet"),
            actor={"role": "manager", "provider": "codex", "actor_id": "x"},
        )
    except workforce_catalog.WorkforceCatalogError as exc:
        assert str(exc) == "runner_id_variant_identity_conflict"
    else:  # pragma: no cover - explicit assertion without pytest dependency
        raise AssertionError("conflicting runner-id variant was accepted")


def test_every_enabled_catalog_worker_resolves_a_launcher_route(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[], preflight=_preflight()
    )
    # Read the launcher route authority read-only.
    routes = process_launcher._CANONICAL_WORKFORCE
    enabled = [row for row in snapshot["workers"] if row["enabled"]]
    registered = [row["execution_runner"] for row in enabled]
    governed = 0
    for row in enabled:
        runner = row["execution_runner"]
        adapter = row["effective_adapter_id"]
        # Identity is byte-stable and accepted by the launcher.
        assert (
            runner_topic_policy.canonical_runner_id(runner, registered) == runner
        )
        assert (
            process_launcher.validate_workforce_identity(runner, adapter, None)
            is None
        )
        # The launcher governs the claude_ family: every enabled claude-family
        # worker must have a launcher-authority row, so an enabled claude model
        # that would die with workforce_route_absent (the measured F5/NF-549
        # failure on claude_opus-5) fails this test instead of being skipped.
        if runner.startswith("claude_"):
            assert (runner, adapter) in routes, f"launcher route absent: {runner}"
        # Where the launcher governs an exact route, the enabled model must
        # resolve with its pinned catalog model and risk tier.
        if (runner, adapter) in routes:
            governed += 1
            resolved = process_launcher.validate_workforce_identity(
                runner, adapter, row["model"], risk_tier=row["max_risk"]
            )
            assert resolved == routes[(runner, adapter)]["model"]
    assert governed >= 3  # claude_opus-5, claude_sonnet-5, codex_gpt-5.5 minimum


def test_launcher_route_absent_for_an_enabled_model_fails_the_suite() -> None:
    # The exact regression shape: a claude-family enabled model whose canonical
    # runner id is missing from the launcher authority must raise
    # workforce_route_absent, so the parity guard above can never pass silently.
    assert ("claude_ghost-9", "claude_cli") not in process_launcher._CANONICAL_WORKFORCE
    try:
        process_launcher.validate_workforce_identity(
            "claude_ghost-9", "claude_cli", "ghost-9"
        )
    except process_launcher.LaunchRejected as exc:
        assert str(exc).startswith("workforce_route_absent:")
    else:  # pragma: no cover - explicit assertion without pytest dependency
        raise AssertionError("missing launcher route did not raise route-absent")


# ---------------------------------------------------------------------------
# One question, one predicate (NF-2026-00669).
#
# Two surfaces publish a verdict about the same route.  Preflight answers "can
# this route be STARTED here?"; the catalog answers "has a round trip been
# OBSERVED on it?".  Both answers can be true at once, and the owner reported
# twice that they read as a contradiction because neither said which question
# it had answered and because the catalog called a route with 53 decided tasks
# "unobserved".  These tests hold both halves of that fix.
# ---------------------------------------------------------------------------


def _glm_route_economics(*, matched: int, accepted: int) -> dict:
    """A cost-ledger view carrying decided outcomes for the live GLM route."""
    return {
        "routes": {
            "glm-5.2": {
                "glm_vscode_lm": {
                    "code": {
                        "unknown": {
                            "matched_decided_tasks": matched,
                            "accepted_outcomes": accepted,
                            "state": "UNKNOWN",
                        }
                    }
                }
            }
        }
    }


def test_route_with_decided_history_is_never_published_as_never_observed(
    tmp_path: Path,
) -> None:
    """The owner's exact case: 53 decided tasks reported as `route_unobserved`."""
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight=_preflight(),
        cost_per_accepted_outcome=_glm_route_economics(matched=53, accepted=23),
        now_epoch=2_000_000_000.0,
    )
    glm = next(row for row in snapshot["workers"] if row["worker_id"] == "glm-5.2")

    # The verdict on the ROUND TRIP stays negative -- nothing recent proves
    # the route still works -- but it must not claim the route was never
    # observed, and it must not make the route unavailable either.
    assert glm["available"] is True

    observation = glm["route_observation"]
    assert observation["reason"] == repo_policy.ROUTE_OBSERVATION_OUTSIDE_WINDOW
    assert observation["prior_observation_count"] == 53
    assert (
        observation["evidence_class"]
        == provider_route_contracts.EVIDENCE_OBSERVED_ROUND_TRIP
    )
    # Measured-but-stale is `unknown`, never `supported` and never
    # `unsupported`: nothing measured this route failing.
    assert observation["state"] == provider_route_contracts.CAPABILITY_UNKNOWN
    assert snapshot["summary"]["decided_history_outside_observation_window"] >= 1


def test_route_with_no_history_stays_unobserved_and_never_borrows_evidence(
    tmp_path: Path,
) -> None:
    """Fail-closed in the other direction: unmeasured must stay unmeasured."""
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight=_preflight(),
        now_epoch=2_000_000_000.0,
    )
    glm = next(row for row in snapshot["workers"] if row["worker_id"] == "glm-5.2")

    # Never run is UNKNOWN, not bad: the route stays available so it can earn
    # its first observation.  A gate that needs a success to open can never
    # open on a fresh install.
    assert glm["available"] is True
    assert glm["route_health"]["state"] == "closed"
    observation = glm["route_observation"]
    assert observation["reason"] == repo_policy.ROUTE_OBSERVATION_NEVER_RECORDED
    assert observation["prior_observation_count"] == 0
    assert (
        observation["evidence_class"] == provider_route_contracts.EVIDENCE_UNVERIFIED
    )
    assert observation["state"] == provider_route_contracts.CAPABILITY_UNKNOWN


def test_a_stale_terminal_execution_still_counts_as_a_prior_observation(
    tmp_path: Path,
) -> None:
    """A success older than the window is evidence; only its recency lapsed."""
    root = _root(tmp_path)
    now = 2_000_000_000.0

    def success_at(epoch: float) -> dict:
        return {
            "request_id": "ok-1",
            "task_id": "task-ok-1",
            "adapter_id": "deepseek_vscode_lm",
            "model": "deepseek-v4-pro",
            "state": "accepted",
            "error": "",
            "finished_at": datetime.fromtimestamp(
                epoch, tz=timezone.utc
            ).isoformat(),
        }

    fresh = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[success_at(now - 60)],
        preflight=_deepseek_preflight(), now_epoch=now,
    )
    pro = next(
        row for row in fresh["workers"] if row["worker_id"] == "deepseek-v4-pro"
    )
    assert pro["available"] is True
    assert pro["availability_reason"] == ""
    assert (
        pro["route_observation"]["state"]
        == provider_route_contracts.CAPABILITY_SUPPORTED
    )
    assert pro["route_observation"]["reason"] == repo_policy.ROUTE_OBSERVATION_IN_WINDOW

    stale_epoch = now - workforce_catalog.ROUTE_CIRCUIT_LOOKBACK_SECONDS - 60
    stale = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[success_at(stale_epoch)],
        preflight=_deepseek_preflight(), now_epoch=now,
    )
    stale_pro = next(
        row for row in stale["workers"] if row["worker_id"] == "deepseek-v4-pro"
    )
    # `supported` must decay out of the window -- an old success cannot keep
    # asserting the route works today -- but decay is not a failure, so the
    # route stays available.  This is the owner's second case: a week away
    # must not make a working route unavailable on Monday.
    assert stale_pro["available"] is True
    assert stale_pro["route_health"]["state"] == "closed"
    assert (
        stale_pro["route_observation"]["state"]
        == provider_route_contracts.CAPABILITY_UNKNOWN
    )
    # ...and the route was observed, so it is not reported as never run.
    assert (
        stale_pro["route_observation"]["reason"]
        == repo_policy.ROUTE_OBSERVATION_OUTSIDE_WINDOW
    )
    assert stale_pro["route_observation"]["prior_observation_count"] == 1
    assert (
        stale_pro["route_observation"]["evidence_class"]
        == provider_route_contracts.EVIDENCE_OBSERVED_ROUND_TRIP
    )


def test_every_unavailable_route_names_an_exact_actionable_blocker(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={
            "providers": [
                {
                    "adapter_id": "claude_cli",
                    "launchable": False,
                    "status": "not_installed",
                }
            ]
        },
    )
    assert snapshot["workers"]
    for row in snapshot["workers"]:
        if row["available"]:
            assert row["availability_reason"] == "", row["worker_id"]
        else:
            # `available=false` is never a bare no.
            assert row["availability_reason"], row["worker_id"]

    opus = next(
        row for row in snapshot["workers"] if row["worker_id"] == "claude-opus-5"
    )
    assert opus["available"] is False
    # The blocker belongs to the OTHER surface, so this row quotes that
    # surface's question and its own status instead of inventing a second
    # word for the same fact.  This is what stops the two contradicting.
    assert opus["availability_reason"] == (
        f"{repo_policy.ROUTE_QUESTION_STARTABLE}:not_installed"
    )


def test_the_two_surfaces_answer_named_and_different_questions(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    # Real preflight on whatever host runs this: the question tokens are host
    # independent even though the readiness verdicts are not, so nothing here
    # forces a platform.
    preflight = repo_policy.build_preflight(root)
    assert preflight["providers"]
    for row in preflight["providers"]:
        assert row["route_question"] == repo_policy.ROUTE_QUESTION_STARTABLE

    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[], preflight=_preflight()
    )
    assert snapshot["workers"]
    for row in snapshot["workers"]:
        assert row["route_question"] == repo_policy.ROUTE_QUESTION_ROUND_TRIP_OBSERVED
        assert row["route_observation"]["question"] == row["route_question"]

    # The two surfaces must not be answering the same question -- if they
    # were, one of them would be redundant and they could genuinely conflict.
    assert (
        repo_policy.ROUTE_QUESTION_STARTABLE
        != repo_policy.ROUTE_QUESTION_ROUND_TRIP_OBSERVED
    )
    assert (
        snapshot["truth_contract"]["route_question"]
        == repo_policy.ROUTE_QUESTION_ROUND_TRIP_OBSERVED
    )
    assert (
        snapshot["truth_contract"]["startability_question_answered_by"]
        == "repo_policy.build_preflight"
    )


def test_preflight_declares_both_questions_and_disclaims_the_one_it_cannot_answer(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    questions = repo_policy.build_preflight(root)["provider_summary"][
        "route_status_questions"
    ]
    assert questions["answered_here"]["question"] == (
        repo_policy.ROUTE_QUESTION_STARTABLE
    )
    assert questions["answered_by_workforce_catalog"]["question"] == (
        repo_policy.ROUTE_QUESTION_ROUND_TRIP_OBSERVED
    )
    # A reader of preflight alone is told, on the surface itself, that
    # `launchable` is not availability.
    assert "never completed" not in questions["answered_here"]["asserts"]
    assert questions["answered_here"]["does_not_assert"]
    assert questions["answered_by_workforce_catalog"]["does_not_assert"]


def test_rank_task_carries_the_exact_reason_an_excluded_route_was_dropped(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    now = 2_000_000_000.0
    # A route with 53 decided tasks that is ALSO failing right now.  Only the
    # measured failures exclude it; the decided history travels with the
    # decision so a reader can see the route is not a stranger, merely sick.
    failing = [
        {
            "request_id": f"glm-fail-{index}",
            "task_id": f"T-glm-fail-{index}",
            "adapter_id": "glm_vscode_lm",
            "model": "glm-5.2",
            "state": "launch_failed",
            "finished_at": datetime.fromtimestamp(
                now - 60 - index, tz=timezone.utc
            ).isoformat(),
        }
        for index in range(2)
    ]
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=failing,
        preflight=_preflight(),
        cost_per_accepted_outcome=_glm_route_economics(matched=53, accepted=23),
        now_epoch=now,
    )
    task = workforce_router.TaskRequirements.build(
        task_id="reason-propagation",
        repo_id="repo",
        kinds=["code"],
        risk="medium",
        tool_needs=["source-graph"],
    )
    decision = workforce_catalog.rank_task(root, task, catalog=snapshot)
    glm = next(
        item for item in decision["candidates"] if item["worker_id"] == "glm-5.2"
    )
    assert glm["excluded"] is True
    assert "worker_unavailable" in glm["exclusion_reasons"]
    # The router only knows "unavailable"; the catalog knows why.  A reader of
    # the decision must not have to go back to the catalog to find out.
    assert glm["availability_reason"].startswith(
        repo_policy.ROUTE_OBSERVATION_CIRCUIT_OPEN
    )
    assert glm["route_observation"]["prior_observation_count"] == 53


def test_availability_predicate_says_which_questions_decided_the_verdict(
    tmp_path: Path,
) -> None:
    """`available` is a conjunction; the row must say which questions it used.

    The conjunction is now the SAME two questions for every route.  It used
    to include the round-trip question for two provider families only, which
    is what made a fresh install unable to start them at all.
    """
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[], preflight=_preflight()
    )
    by_id = {row["worker_id"]: row for row in snapshot["workers"]}

    for worker_id in ("glm-5.2", "claude-opus-5"):
        row = by_id[worker_id]
        assert row["availability_predicate"] == [
            repo_policy.ROUTE_QUESTION_STARTABLE,
            repo_policy.ROUTE_QUESTION_FAILURE_CIRCUIT_CLOSED,
        ], worker_id
        assert row["available"] is row["launch_eligible"], worker_id

    # The switch that carried the old per-provider asymmetry is gone, not
    # merely unread: a dead gate is one edit away from being re-armed.
    assert not hasattr(workforce_catalog, "_OBSERVATION_GATED_PROVIDERS")
    assert all("observation_gated_route" not in row for row in snapshot["workers"])

    for row in snapshot["workers"]:
        # Whatever the conjunction, the round-trip fact is reported for every
        # row, so no route is ever silently unmeasured.
        assert row["route_observation"]["question"] == (
            repo_policy.ROUTE_QUESTION_ROUND_TRIP_OBSERVED
        )
        assert row["route_observation"]["state"] in {
            provider_route_contracts.CAPABILITY_SUPPORTED,
            provider_route_contracts.CAPABILITY_UNSUPPORTED,
            provider_route_contracts.CAPABILITY_UNKNOWN,
        }


# ---------------------------------------------------------------------------
# NF-2026-00672.  Two defects in one expression.
#
# 1. Availability required a terminal success inside a 24h window for two
#    provider families, which is a gate that can never open on a fresh
#    install and that closes again after a week away.
# 2. "Has this route EVER run?" was answered from the process log, which this
#    repository retains for 7 days and which held 78 minutes of history when
#    the defect was measured -- so every route reported that no terminal
#    execution was ever recorded while the usage ledger held 143 records for
#    the same runner.
# ---------------------------------------------------------------------------


def _decided_card(
    *, task_id: str, adapter_id: str, model: str, status: str = "finished",
) -> dict:
    """A card as the RETAINED store holds it: no live process row survives."""
    return {
        "task_id": task_id,
        "status": status,
        "terminal_substatus": "review_ready",
        "terminal_review": {
            "evidence": {"adapter_id": adapter_id, "model": model},
        },
    }


def test_a_route_with_no_history_at_all_is_available(tmp_path: Path) -> None:
    """The owner's first case: what happens the first time someone works?

    A fresh install has no history by construction.  If availability requires
    an observed success, the route is never available, so it is never
    launched, so it never earns the success -- the gate is self-locking.
    """
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[], usage_rows=[],
        preflight=_preflight(), now_epoch=2_000_000_000.0,
    )
    for worker_id in ("glm-5.2", "deepseek-v4-pro", "claude-opus-5"):
        row = next(
            item for item in snapshot["workers"]
            if item["worker_id"] == worker_id
        )
        assert row["available"] is row["launch_eligible"], worker_id
        assert row["route_health"]["state"] == "closed", worker_id
        assert row["route_health"]["consecutive_failures"] == 0, worker_id
        # The absence is reported honestly, and as UNKNOWN -- never as a
        # measured negative.  Nobody measured this route failing.
        observation = row["route_observation"]
        assert observation["prior_observation_count"] == 0, worker_id
        assert observation["reason"] == (
            repo_policy.ROUTE_OBSERVATION_NEVER_RECORDED
        ), worker_id
        assert observation["state"] == (
            provider_route_contracts.CAPABILITY_UNKNOWN
        ), worker_id

    glm = next(
        item for item in snapshot["workers"] if item["worker_id"] == "glm-5.2"
    )
    assert glm["available"] is True
    task = workforce_router.TaskRequirements.build(
        task_id="T-first-ever-run",
        repo_id="repo",
        kinds=["code"],
        risk="high",
        owner_model_pin="glm-5.2",
        tool_needs=["source-graph"],
    )
    decision = workforce_catalog.rank_task(root, task, catalog=snapshot)
    assert decision["selected_worker_id"] == "glm-5.2"


def test_a_route_whose_only_history_is_older_than_the_window_is_available(
    tmp_path: Path,
) -> None:
    """The owner's second case: coming back after a week away.

    The window is the circuit breaker's, and a breaker trips on failures.  A
    success ageing out of it is not a failure; it is the same route, quieter.
    """
    root = _root(tmp_path)
    now = 2_000_000_000.0
    stale_epoch = now - workforce_catalog.ROUTE_CIRCUIT_LOOKBACK_SECONDS * 7
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[{
            "request_id": "week-old-success",
            "task_id": "T-week-old",
            "adapter_id": "glm_vscode_lm",
            "model": "glm-5.2",
            "state": "accepted",
            "finished_at": datetime.fromtimestamp(
                stale_epoch, tz=timezone.utc
            ).isoformat(),
        }],
        preflight=_preflight(),
        now_epoch=now,
    )
    glm = next(
        row for row in snapshot["workers"] if row["worker_id"] == "glm-5.2"
    )
    assert glm["available"] is True
    assert glm["route_health"]["state"] == "closed"
    assert glm["route_health"]["failure_kind"] == ""
    observation = glm["route_observation"]
    assert observation["reason"] == repo_policy.ROUTE_OBSERVATION_OUTSIDE_WINDOW
    assert observation["state"] == provider_route_contracts.CAPABILITY_UNKNOWN
    assert observation["prior_observation_count"] == 1
    assert snapshot["summary"]["decided_history_outside_observation_window"] >= 1


def test_prior_observations_survive_a_process_log_that_aged_out(
    tmp_path: Path,
) -> None:
    """NF-2026-00672 exactly: no process rows, 205 decided cards, count 205.

    The process log is passed EMPTY, which is what a 7-day log holding 78
    minutes looks like to this join.  Reading the count from that log is what
    published `no_terminal_execution_ever_recorded` for a route with a
    hundreds-strong decided history.
    """
    root = _root(tmp_path)
    cards = [
        _decided_card(
            task_id=f"T-glm-{index}",
            adapter_id="glm_vscode_lm",
            model="glm-5.2",
        )
        for index in range(205)
    ]
    snapshot = workforce_catalog.build_catalog(
        root, cards=cards, process_rows=[], usage_rows=[],
        preflight=_preflight(), now_epoch=2_000_000_000.0,
    )
    glm = next(
        row for row in snapshot["workers"] if row["worker_id"] == "glm-5.2"
    )
    observation = glm["route_observation"]
    assert observation["prior_observation_count"] == 205
    assert observation["reason"] == repo_policy.ROUTE_OBSERVATION_OUTSIDE_WINDOW
    assert (
        observation["evidence_class"]
        == provider_route_contracts.EVIDENCE_OBSERVED_ROUND_TRIP
    )
    # The number is auditable: it says which retained ledger it came from,
    # and that it did NOT come from the process log.
    sources = observation["prior_observation_sources"]
    assert sources["retained_decided_task_cards"] == 205
    assert sources["process_log_cards"] == 0
    assert sources["process_log_terminal_events"] == 0


def test_prior_observations_reach_the_retained_usage_ledger(
    tmp_path: Path,
) -> None:
    """The ledger `aiworkhub_task_usage_report` reads is reachable here too.

    Its rows key on `requested_model`, which is the catalog's own model name,
    so the join needs no runner translation -- and must not attempt one: the
    runner column is many-to-one against models.
    """
    root = _root(tmp_path)
    usage = [
        {
            "task_id": f"T-usage-{index}",
            "runner": "glm" if index % 2 else "glm_5.2",
            "adapter_id": "glm_vscode_lm",
            "requested_model": "glm-5.2",
            "model": "customendpoint/glm-5.2/glm-5.2/GLM-5.2/1.0.0",
        }
        for index in range(143)
    ]
    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[], usage_rows=usage,
        preflight=_preflight(), now_epoch=2_000_000_000.0,
    )
    glm = next(
        row for row in snapshot["workers"] if row["worker_id"] == "glm-5.2"
    )
    observation = glm["route_observation"]
    assert observation["prior_observation_count"] == 143
    assert observation["prior_observation_sources"]["retained_usage_records"] == 143
    # Two different runner spellings, one route: keying on runner would have
    # split this history in half.
    assert {row["runner"] for row in usage} == {"glm", "glm_5.2"}


def test_a_record_whose_route_cannot_be_resolved_is_never_attributed(
    tmp_path: Path,
) -> None:
    """Fail closed on the OTHER side: unknown is unknown, not this route.

    `deepseek_copilot` and `codex` are provider-family spellings the ledgers
    really contain.  They name a vendor, not one of the two routes a DeepSeek
    worker can take, so folding them in would credit `deepseek_vscode_lm`
    with `deepseek_copilot_cli`'s history.
    """
    root = _root(tmp_path)
    unresolvable = [
        {
            "task_id": "T-vendor-only",
            "runner": "deepseek",
            "provider": "deepseek_copilot",
            "requested_model": "deepseek-v4-pro",
        },
        {
            "task_id": "T-no-model",
            "runner": "glm_5.2",
            "adapter_id": "glm_vscode_lm",
            "requested_model": "",
            "model": "",
        },
    ]
    index = workforce_catalog.retained_route_observations(
        [
            _decided_card(
                task_id="T-card-no-model", adapter_id="claude_cli", model=""
            )
        ],
        unresolvable,
    )
    # The guard lives in the identity function itself, so it holds for every
    # caller: a vendor-family spelling resolves to NO adapter, a declared one
    # resolves to itself.  Normalizing `deepseek_copilot` onto either
    # DeepSeek route would credit one transport with the other's history.
    assert workforce_catalog.route_evidence_identity(
        "", "deepseek-v4-pro", adapter_fallback="deepseek_copilot"
    ) == ("", "deepseek-v4-pro")
    assert workforce_catalog.route_evidence_identity(
        "", "deepseek-v4-pro", adapter_fallback="deepseek_vscode_lm"
    ) == ("deepseek_vscode_lm", "deepseek-v4-pro")
    assert workforce_catalog.route_evidence_identity(
        "glm_vscode_lm", "glm-5.2"
    ) == ("glm_vscode_lm", "glm-5.2")
    assert index["unresolved_usage_records"] == 2
    assert index["unresolved_decided_cards"] == 1
    assert index["usage_records"] == {}
    assert index["decided_tasks"] == {}

    snapshot = workforce_catalog.build_catalog(
        root, cards=[], process_rows=[], usage_rows=unresolvable,
        preflight=_preflight(), now_epoch=2_000_000_000.0,
    )
    for worker_id in ("deepseek-v4-pro", "glm-5.2"):
        row = next(
            item for item in snapshot["workers"]
            if item["worker_id"] == worker_id
        )
        assert row["route_observation"]["prior_observation_count"] == 0, worker_id
        assert row["route_observation"]["reason"] == (
            repo_policy.ROUTE_OBSERVATION_NEVER_RECORDED
        ), worker_id


def test_history_is_attributed_to_the_exact_route_not_the_sibling_adapter(
    tmp_path: Path,
) -> None:
    """One model, two transports, two histories -- kept apart.

    `deepseek-v4-pro` runs over the editor bridge and over the BYOK Copilot
    CLI.  They are different authorizations, and one's history is not
    evidence about the other.
    """
    root = _root(tmp_path)
    cards = (
        [
            _decided_card(
                task_id=f"T-bridge-{i}",
                adapter_id="deepseek_vscode_lm",
                model="deepseek-v4-pro",
            )
            for i in range(68)
        ]
        + [
            _decided_card(
                task_id=f"T-byok-{i}",
                adapter_id="deepseek_copilot_cli",
                model="deepseek-v4-pro",
            )
            for i in range(274)
        ]
    )
    bridge_only = {
        "providers": [
            {"adapter_id": "deepseek_vscode_lm", "launchable": True, "status": "ready"},
        ]
    }
    snapshot = workforce_catalog.build_catalog(
        root, cards=cards, process_rows=[], usage_rows=[],
        preflight=bridge_only, now_epoch=2_000_000_000.0,
    )
    pro = next(
        row for row in snapshot["workers"]
        if row["worker_id"] == "deepseek-v4-pro"
    )
    assert pro["effective_adapter_id"] == "deepseek_vscode_lm"
    # 68, not 342: the BYOK route's 274 belong to the BYOK route.
    assert pro["route_observation"]["prior_observation_count"] == 68


def test_claude_cli_alias_is_translated_by_the_one_existing_alias_table(
    tmp_path: Path,
) -> None:
    """The catalog says `sonnet`; the ledgers say `claude-sonnet-5`.

    That translation already exists once, in `_EDITOR_MODEL_ALIASES`.  A
    second table here is how the two vocabularies drifted apart.
    """
    assert "claude-sonnet-5" in workforce_catalog.route_model_identities("sonnet")
    assert workforce_catalog.route_model_identities("sonnet") >= {
        "sonnet", *workforce_catalog._EDITOR_MODEL_ALIASES["sonnet"]
    }

    root = _root(tmp_path)
    cards = [
        _decided_card(
            task_id="T-alias-cli", adapter_id="claude_cli", model="sonnet"
        ),
        _decided_card(
            task_id="T-alias-ledger",
            adapter_id="claude_cli",
            model="claude-sonnet-5",
        ),
    ]
    snapshot = workforce_catalog.build_catalog(
        root, cards=cards, process_rows=[], usage_rows=[],
        preflight=_preflight(), now_epoch=2_000_000_000.0,
    )
    sonnet = next(
        row for row in snapshot["workers"]
        if row["worker_id"] == "claude-sonnet-5"
    )
    assert sonnet["route_observation"]["prior_observation_count"] == 2


def test_route_observation_verdict_reports_where_its_count_came_from() -> None:
    verdict = repo_policy.route_observation_verdict(
        observed_in_window=False,
        prior_observation_count=205,
        observation_window_seconds=86_400.0,
        evidence_sources={"retained_decided_task_cards": 205, "process_log_cards": 0},
    )
    assert verdict["prior_observation_count"] == 205
    assert verdict["prior_observation_sources"] == {
        "retained_decided_task_cards": 205,
        "process_log_cards": 0,
    }
    assert verdict["reason"] == repo_policy.ROUTE_OBSERVATION_OUTSIDE_WINDOW
    # Omitting the breakdown is allowed and reports an empty mapping, never a
    # fabricated attribution.
    assert repo_policy.route_observation_verdict(
        observed_in_window=False,
        prior_observation_count=0,
        observation_window_seconds=86_400.0,
    )["prior_observation_sources"] == {}


def test_access_observation_names_which_evidence_made_it_true(tmp_path: Path) -> None:
    """Access-probe facts must not read as current round-trip observation.

    Measured 2026-09-08 on a Windows operator's 0.11.8 report: the same row
    said ``route_observation.state = unknown`` with
    ``no_terminal_execution_inside_observation_window`` and, two fields later,
    ``availability_observed = true``. The compatibility boolean is now current
    in-window round-trip success only; the access record names a different
    question and never claims to prove a round trip.
    """
    root = _root(tmp_path)
    preflight = {"providers": [
        {"adapter_id": "codex_cli", "launchable": True, "status": "ready"}
    ]}

    def _observation(snapshot: dict, worker_id: str = "gpt-5.5") -> dict:
        row = next(r for r in snapshot["workers"] if r["worker_id"] == worker_id)
        observation = row["availability_observation"]
        for other in snapshot["workers"]:
            record = other["availability_observation"]
            assert record["question"] == repo_policy.ROUTE_QUESTION_ACCESS_PROBE_OBSERVED
            assert record["question"] != repo_policy.ROUTE_QUESTION_ROUND_TRIP_OBSERVED
            assert record["windowed"] is False
            assert record["proves_round_trip"] is False
            assert other["availability_observed"] is (other["round_trip_observed"] is True)
            assert other["readiness"]["proves_round_trip"] is False
        return observation

    quiet = _observation(
        workforce_catalog.build_catalog(
            root, cards=[], process_rows=[], preflight={"providers": []}
        )
    )
    assert quiet["basis"] == "none"
    assert quiet["access_probe_observed"] is False
    assert quiet["historical_quality_cards"] == 0

    seen_snapshot = workforce_catalog.build_catalog(
        root,
        cards=[{"task_id": "T1", "status": "finished",
                "terminal_substatus": "review_ready"}],
        process_rows=[{
            "request_id": "r1", "task_id": "T1", "adapter_id": "codex_cli",
            "model": "gpt-5.5", "total_tokens": 500,
        }],
        preflight=preflight,
    )
    seen = _observation(seen_snapshot)
    assert seen["basis"] == "historical_quality_cards"
    assert seen["historical_quality_cards"] == 1
    row = next(r for r in seen_snapshot["workers"] if r["worker_id"] == "gpt-5.5")
    assert row["route_question"] == repo_policy.ROUTE_QUESTION_ROUND_TRIP_OBSERVED
    assert row["availability_observation"]["question"] != row["route_question"]
    assert row["availability_observed"] is False
    assert row["round_trip_observed"] == "unknown"
    assert row["historical_route_observation"]["recorded"] is True
    assert row["historical_route_observation"]["in_current_window"] is False
