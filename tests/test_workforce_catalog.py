from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from aiworkhub import workforce_catalog, workforce_router


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
    assert outcomes["attempt_count"] == 3
    assert outcomes["retry_count"] == 1
    assert outcomes["accepted_rate"] == 0.5
    assert outcomes["review_ready_rate"] == 1.0
    assert outcomes["validation_failure_rate"] == 0.5
    assert outcomes["cost_usd_per_1k_tokens"] == 0.1
    assert snapshot["summary"]["unattributed_process_rows"] == 1
    assert snapshot["truth_contract"]["provider_quota_fabricated"] is False
    untouched = next(item for item in snapshot["workers"] if item["worker_id"] == "glm-5.2")
    assert untouched["observed_score"] is None
    assert untouched["outcomes"]["evidence_source"] == "conservative_prior"
    assert untouched["availability_observed"] is False


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
    by_id = {item["worker_id"]: item for item in decision["candidates"]}
    assert by_id["b"]["score_components"]["manager_adjusted_success_rate"] > by_id["a"]["score_components"]["manager_adjusted_success_rate"]
