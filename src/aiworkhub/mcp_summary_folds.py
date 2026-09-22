"""Compact "summary" folds for oversized manager/worker MCP tool responses.

Mirrors the existing pattern in ``core.summarize_evidence`` /
``core.CARD_EVIDENCE_PATHS`` / ``core._fold_evidence``: a named heavy field is
folded to ``{summarized, bytes, sha256}`` plus the handful of facts a
reviewer actually reads, and every other field is returned exact. Kept in its
own module -- rather than growing ``server.py`` or ``core.py`` -- per the
module size ratchet in tests/test_module_size_ratchet.py.
"""

from __future__ import annotations

from typing import Any

from . import core

_REWORK_OUTCOME_REASON = {
    "delta_reused": "sealed delta artifact reused",
    "pinned_no_delta": "predecessor pinned without a reusable delta artifact",
    "no_changes": "predecessor recorded no changed paths",
}

# Static, author-written contract text that repeats on every read of a task's
# process status -- a caller re-reading status already has it from the
# create/launch call that started the run. Folded to identity here;
# task_id/title/status and the other lifecycle fields are left exact.
_CARD_STATIC_CONTRACT_KEYS = (
    "objective",
    "acceptance",
    "validation",
    "read_first",
    "template_provenance",
    "project_context",
    "forbidden",
    "required_outputs",
    "mandatory_changed_outputs",
)


def _fold_to_identity(value: Any) -> Any:
    """Fold ``value`` to ``{summarized, bytes, sha256}``; pass scalars through."""

    if value is None or isinstance(value, (int, float, bool)):
        return value
    size, digest = core._evidence_identity(value)
    return {"summarized": True, "bytes": size, "sha256": digest}


def fold_terminal_failure(value: Any) -> Any:
    """Fold a card's ``terminal_failure`` to the facts a reviewer reads."""

    if not isinstance(value, dict):
        return value
    size, digest = core._evidence_identity(value)
    evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
    return {
        "summarized": True,
        "bytes": size,
        "sha256": digest,
        "substatus": value.get("substatus"),
        "reason": value.get("reason"),
        "category": evidence.get("failure_class") or value.get("category"),
        "first_error": core._evidence_text(
            value.get("error"),
            evidence.get("error"),
            evidence.get("stderr_tail"),
            evidence.get("stdout_tail"),
        ),
    }


def fold_rework_predecessor(value: Any) -> Any:
    """Fold a card's ``rework_predecessor`` to the facts a reviewer reads."""

    if not isinstance(value, dict):
        return value
    size, digest = core._evidence_identity(value)
    if value.get("rework_delta") is not None:
        outcome = "delta_reused"
    elif value.get("changed_path_hashes"):
        outcome = "pinned_no_delta"
    else:
        outcome = "no_changes"
    return {
        "summarized": True,
        "bytes": size,
        "sha256": digest,
        "task_id": value.get("task_id"),
        "request_id": value.get("request_id"),
        "outcome": outcome,
        "reason": _REWORK_OUTCOME_REASON[outcome],
    }


_KNOWN_TERMINAL_REVIEW_EVIDENCE_KEYS = frozenset(
    {
        "validation",
        "quality_gate",
        "quality_review",
        "quality_review_receipt",
        "worker_mcp_gate",
        "workspace",
    }
)


def _fold_blob_generic(value: Any) -> Any:
    """Fold an unlisted evidence/telemetry blob to identity plus any verdict-like signal."""

    if not isinstance(value, (dict, list)):
        return value
    size, digest = core._evidence_identity(value)
    folded: dict[str, Any] = {"summarized": True, "bytes": size, "sha256": digest}
    if isinstance(value, dict):
        for key in ("verdict", "status", "measured", "evidence_observed"):
            if key in value:
                folded[key] = value[key]
    return folded


def fold_terminal_review_evidence_residual(card: Any) -> Any:
    """Fold every ``terminal_review.evidence`` sub-key CARD_EVIDENCE_PATHS doesn't list."""

    if not isinstance(card, dict):
        return card
    terminal_review = card.get("terminal_review")
    if not isinstance(terminal_review, dict):
        return card
    evidence = terminal_review.get("evidence")
    if not isinstance(evidence, dict):
        return card
    residual_keys = [key for key in evidence if key not in _KNOWN_TERMINAL_REVIEW_EVIDENCE_KEYS]
    if not residual_keys:
        return card
    folded_evidence = dict(evidence)
    for key in residual_keys:
        folded_evidence[key] = _fold_blob_generic(folded_evidence[key])
    result = dict(card)
    result["terminal_review"] = {**terminal_review, "evidence": folded_evidence}
    return result


def fold_task_card_facts(card: Any) -> Any:
    """Fold ``terminal_failure``/``rework_predecessor``/expanded_contract.

    Shared by ``aiworkhub_task_show`` and ``aiworkhub_agent_task_status``:
    these are outcome/provenance byproducts, never what a manager is reading
    the card in order to learn.
    """

    if not isinstance(card, dict):
        return card
    result = dict(card)
    if isinstance(result.get("terminal_failure"), dict):
        result["terminal_failure"] = fold_terminal_failure(result["terminal_failure"])
    if isinstance(result.get("rework_predecessor"), dict):
        result["rework_predecessor"] = fold_rework_predecessor(result["rework_predecessor"])
    provenance = result.get("template_provenance")
    if isinstance(provenance, dict) and "expanded_contract" in provenance:
        folded_provenance = dict(provenance)
        folded_provenance["expanded_contract"] = _fold_to_identity(provenance["expanded_contract"])
        result["template_provenance"] = folded_provenance
    return fold_terminal_review_evidence_residual(result)


def fold_task_card_contract(card: Any) -> Any:
    """Fold a card's static contract text to identity; keep lifecycle fields."""

    if not isinstance(card, dict):
        return card
    result = dict(card)
    for key in _CARD_STATIC_CONTRACT_KEYS:
        if key in result and result[key]:
            result[key] = _fold_to_identity(result[key])
    allowed_writes = result.get("allowed_writes")
    if isinstance(allowed_writes, list):
        result["allowed_writes"] = {"summarized": True, "count": len(allowed_writes)}
    return result


def fold_task_card_for_process_status(card: Any) -> Any:
    """Full task_card fold for ``aiworkhub_agent_task_status``'s summary."""

    return fold_task_card_contract(fold_task_card_facts(card))


def _compact_provider_status_map(items: Any, *, adapter_id: str | None) -> dict[str, Any]:
    """Fold provider rows to a flat {adapter_id: status} map."""

    if not isinstance(items, list):
        return items
    status_by_id = {
        str(item.get("adapter_id")): item.get("status")
        for item in items if isinstance(item, dict)
    }
    if adapter_id:
        return {key: value for key, value in status_by_id.items() if key == adapter_id}
    return status_by_id


def _compact_adapter_observability_row(item: Any) -> Any:
    """One compact row per adapter: identity, reachability and quota facts only."""

    if not isinstance(item, dict):
        return item
    return {
        "adapter_id": item.get("adapter_id"),
        "installed": item.get("installed"),
        "reachable": item.get("reachable"),
        "access_observed": item.get("access_observed"),
        "quota_observable": item.get("quota_observable"),
        "reason": item.get("quota_observability_reason"),
    }


def _compact_route_family_row(family: str, contract: Any) -> dict[str, Any]:
    """One compact row per route family: identity, verification status, supported-capability count."""

    if not isinstance(contract, dict):
        return {"family": family, "status": None, "count": 0}
    capabilities = contract.get("capabilities")
    supported_count = 0
    if isinstance(capabilities, dict):
        supported_count = sum(
            1 for record in capabilities.values()
            if isinstance(record, dict) and record.get("state") == "supported"
        )
    return {
        "family": family,
        "status": contract.get("last_verification"),
        "count": supported_count,
    }


_ID_LIST_PREVIEW = 5


def _compact_capability_route_map(value: Any) -> Any:
    """Fold each capability's route/exclusion list to a count plus a first few adapter ids."""

    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for capability, entries in value.items():
        if isinstance(entries, list):
            ids = [
                str(item.get("adapter_id")) if isinstance(item, dict) else str(item)
                for item in entries
            ]
            result[capability] = {
                "count": len(entries),
                "adapter_ids": ids[:_ID_LIST_PREVIEW],
            }
        else:
            result[capability] = entries
    return result


def _compact_route_list(value: Any) -> Any:
    """Fold a flat route list to a count plus a first few adapter ids."""

    if not isinstance(value, list):
        return value
    ids = [str(item.get("adapter_id")) for item in value if isinstance(item, dict)]
    return {"count": len(value), "adapter_ids": ids[:_ID_LIST_PREVIEW]}


_POLICY_VERDICT_KEYS = (
    "valid",
    "configured",
    "error",
    "model_settings_valid",
    "model_settings_error",
)
_WORKER_FINALIZATION_STATE_KEYS = ("ok", "status", "reason", "phase")


def fold_preflight_summary(report: Any, *, adapter_id: str | None) -> Any:
    """Fold ``repo_policy.build_preflight``'s output for a compact summary."""

    if not isinstance(report, dict):
        return report
    result = dict(report)
    source_graph = result.get("source_graph")
    if isinstance(source_graph, dict):
        folded_source_graph = dict(source_graph)
        refresh_job = folded_source_graph.get("refresh_job")
        if isinstance(refresh_job, dict) and "report" in refresh_job:
            folded_refresh_job = dict(refresh_job)
            folded_refresh_job["report"] = _fold_to_identity(refresh_job["report"])
            folded_source_graph["refresh_job"] = folded_refresh_job
        result["source_graph"] = folded_source_graph
    providers = result.get("providers")
    if isinstance(providers, list):
        result["providers"] = _compact_provider_status_map(providers, adapter_id=adapter_id)
    observability = result.get("provider_observability")
    if isinstance(observability, dict):
        folded_observability = dict(observability)
        for key in ("adapters", "providers"):
            rows = folded_observability.get(key)
            if isinstance(rows, list):
                rows = [
                    _compact_adapter_observability_row(item) if isinstance(item, dict) else item
                    for item in rows
                ]
                if adapter_id:
                    rows = [
                        row for row in rows
                        if isinstance(row, dict) and row.get("adapter_id") == adapter_id
                    ]
                folded_observability[key] = rows
        result["provider_observability"] = folded_observability
    route_contracts = result.get("provider_route_contracts")
    if isinstance(route_contracts, dict) and isinstance(route_contracts.get("route_families"), dict):
        folded_route_contracts = dict(route_contracts)
        folded_route_contracts["route_families"] = [
            _compact_route_family_row(family, contract)
            for family, contract in route_contracts["route_families"].items()
        ]
        result["provider_route_contracts"] = folded_route_contracts
    provider_summary = result.get("provider_summary")
    if isinstance(provider_summary, dict):
        folded_provider_summary = dict(provider_summary)
        if isinstance(folded_provider_summary.get("capability_exclusions"), dict):
            folded_provider_summary["capability_exclusions"] = _compact_capability_route_map(
                folded_provider_summary["capability_exclusions"]
            )
        if isinstance(folded_provider_summary.get("capability_launchable_routes"), dict):
            folded_provider_summary["capability_launchable_routes"] = _compact_capability_route_map(
                folded_provider_summary["capability_launchable_routes"]
            )
        if isinstance(folded_provider_summary.get("unavailable_routes"), list):
            folded_provider_summary["unavailable_routes"] = _compact_route_list(
                folded_provider_summary["unavailable_routes"]
            )
        if "route_status_questions" in folded_provider_summary:
            folded_provider_summary["route_status_questions"] = _fold_to_identity(
                folded_provider_summary["route_status_questions"]
            )
        result["provider_summary"] = folded_provider_summary
    policy = result.get("policy")
    if isinstance(policy, dict):
        result["policy"] = {key: policy.get(key) for key in _POLICY_VERDICT_KEYS}
    worker_finalization = result.get("worker_finalization")
    if isinstance(worker_finalization, dict):
        result["worker_finalization"] = {
            key: worker_finalization[key]
            for key in _WORKER_FINALIZATION_STATE_KEYS
            if key in worker_finalization
        }
    return result


_WORKFORCE_RANK_FULL_ROW_PREVIEW = 3
_WORKFORCE_RANK_EXCLUDED_ID_PREVIEW = 10


def _compact_compatible_candidate_row(item: dict[str, Any]) -> dict[str, Any]:
    """Reduce a lower-ranked compatible candidate to identity plus one score."""

    components = item.get("score_components")
    score = (
        components.get("evidence_weighted_success_rate")
        if isinstance(components, dict)
        else None
    )
    return {
        "worker_id": item.get("worker_id"),
        "adapter_id": item.get("adapter_id"),
        "model": item.get("model"),
        "score": score,
    }


def _compact_excluded_candidate_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "worker_id": item.get("worker_id"),
        "adapter_id": item.get("adapter_id"),
        "model": item.get("model"),
    }


def fold_workforce_rank_summary(result: Any) -> Any:
    """Fold ``manager_ai_tools.workforce_rank``'s output for a compact summary."""

    if not isinstance(result, dict):
        return result
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        return result
    folded = dict(result)
    compatible: list[Any] = []
    excluded: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if item.get("excluded"):
            reasons = [str(reason) for reason in (item.get("exclusion_reasons") or [])]
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            excluded.append(item)
        else:
            compatible.append(item)
    folded["candidates"] = [
        item if index < _WORKFORCE_RANK_FULL_ROW_PREVIEW else _compact_compatible_candidate_row(item)
        for index, item in enumerate(compatible)
    ]
    folded["excluded_candidates"] = [
        _compact_excluded_candidate_row(item)
        for item in excluded[:_WORKFORCE_RANK_EXCLUDED_ID_PREVIEW]
    ]
    folded["excluded_candidates_by_reason"] = reason_counts
    return folded


def fold_latest_event_for_process_status(event: Any) -> Any:
    """Fold usage samples and other large event blocks for agent_task_status's summary."""

    if not isinstance(event, dict):
        return event
    result = dict(event)
    usage = result.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("usage_samples"), list):
        samples = usage["usage_samples"]
        size, digest = core._evidence_identity(samples)
        folded_usage = dict(usage)
        folded_usage["usage_samples"] = {
            "summarized": True,
            "count": len(samples),
            "bytes": size,
            "sha256": digest,
        }
        result["usage"] = folded_usage
    for key in ("semantic_edit_coverage", "evidence_record", "read_efficiency"):
        value = result.get(key)
        if isinstance(value, (dict, list)) and value:
            result[key] = _fold_blob_generic(value)
    return result
