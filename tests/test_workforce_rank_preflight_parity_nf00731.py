"""NF-2026-00731 parity: workforce_router and provider_route_contracts preflight
decide every route-contract capability from one vocabulary.

Before this fix, ``workforce_router._exclusion_reasons`` decided tool needs
against ``worker.tools`` (the catalog's declared tool names), while
``aiworkhub_environment_preflight`` decided from
``provider_route_contracts.adapter_can_complete``.  The two could disagree in
either direction whenever a catalog entry spelled a tool hyphenated,
snake_case, or omitted it.  These tests call both real functions directly so a
reintroduced vocabulary split fails here, not in a live run.
"""

from __future__ import annotations

import itertools

import pytest

from aiworkhub import provider_route_contracts
from aiworkhub.workforce_router import (
    SUPPORTED_ADAPTERS,
    TaskRequirements,
    WorkerCapability,
    rank_workforce,
)


def _task(tool_need: str) -> TaskRequirements:
    return TaskRequirements.build(
        task_id="NF00731",
        repo_id="repo-nf00731",
        kinds={"code"},
        tool_needs={tool_need},
    )


def _worker(adapter_id: str, *, tools: frozenset[str] = frozenset()) -> WorkerCapability:
    return WorkerCapability.build(
        worker_id=f"worker-{adapter_id}",
        adapter_id=adapter_id,
        model=adapter_id,
        provider="test-provider",
        supports={"code"},
        tools=tools,
        max_context_tokens=1_000_000,
        max_risk="critical",
        quality_ceiling=1.0,
    )


def _not_declared_reason(capability: str) -> str:
    return f"{provider_route_contracts.REASON_NOT_DECLARED}:{capability}"


@pytest.mark.parametrize(
    "adapter_id,capability",
    list(itertools.product(SUPPORTED_ADAPTERS, provider_route_contracts.CAPABILITY_VOCABULARY)),
)
def test_rank_exclusion_matches_preflight_capability_verdict(adapter_id: str, capability: str) -> None:
    preflight_supported = provider_route_contracts.adapter_can_complete(adapter_id, capability)
    decision = rank_workforce(_task(capability), [_worker(adapter_id)])
    candidate = decision.candidates[0]
    assert candidate.excluded == (not preflight_supported), candidate.exclusion_reasons


@pytest.mark.parametrize("adapter_id", ["grok_kilo_cli", "opencode_cli"])
def test_rank_excludes_structurally_unsupported_cli_routes(adapter_id: str) -> None:
    task = TaskRequirements.build(
        task_id="NF00731-structural",
        repo_id="repo-nf00731",
        kinds={"code"},
        tool_needs={"source-graph", "semantic-edit"},
    )
    decision = rank_workforce(task, [_worker(adapter_id)])
    candidate = decision.candidates[0]
    assert candidate.excluded
    assert _not_declared_reason(provider_route_contracts.CAPABILITY_SOURCE_GRAPH_QUERY) in candidate.exclusion_reasons
    assert _not_declared_reason(provider_route_contracts.CAPABILITY_WORKER_SEMANTIC_EDIT) in candidate.exclusion_reasons
    assert decision.selected_worker_id is None


@pytest.mark.parametrize(
    "need",
    ["source-graph-query", "source_graph_query", "worker-semantic-edit", "worker_semantic_edit"],
)
def test_contract_supported_route_not_excluded_regardless_of_catalog_spelling(need: str) -> None:
    # glm_vscode_lm's declared tools list omits the capability entirely; the
    # route contract alone decides a need that is in its vocabulary.
    decision = rank_workforce(
        _task(need), [_worker("glm_vscode_lm", tools=frozenset({"unrelated-tool"}))]
    )
    candidate = decision.candidates[0]
    assert not candidate.excluded
    assert decision.selected_worker_id == candidate.worker_id


def test_hyphen_and_snake_case_tool_needs_produce_identical_rank_results() -> None:
    worker = _worker("glm_vscode_lm")
    hyphenated = rank_workforce(_task("source-graph"), [worker])
    snake_cased = rank_workforce(_task("source_graph"), [worker])
    assert hyphenated.candidates[0].excluded == snake_cased.candidates[0].excluded
    assert hyphenated.candidates[0].exclusion_reasons == snake_cased.candidates[0].exclusion_reasons
