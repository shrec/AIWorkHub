"""Compact "summary" folds for oversized manager/worker MCP tool responses.

Mirrors the existing pattern in ``core.summarize_evidence`` /
``core.CARD_EVIDENCE_PATHS`` / ``core._fold_evidence``: a named heavy field is
folded to ``{summarized, bytes, sha256}`` plus the handful of facts a
reviewer actually reads, and every other field is returned exact. Kept in its
own module -- rather than growing ``server.py`` or ``core.py`` -- per the
module size ratchet in tests/test_module_size_ratchet.py.
"""

from __future__ import annotations

import hashlib
import json
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


# Scalar members a folded Source Graph health block keeps exact, so a manager
# polling health/refresh_now still reads index_quality.degraded,
# integrity.status and recommendation_resolvability.status without the body.
_SOURCE_GRAPH_HEALTH_KEPT_MEMBERS = ("status", "state", "degraded", "ok")


def fold_source_graph_health_summary(payload: Any) -> Any:
    """Project a Source Graph health/refresh_now payload to its top-level facts.

    Top-level scalars stay exact; every other top-level value folds to
    ``{summarized, bytes, sha256}``, and a folded dict also keeps its own scalar
    status/state/degraded/ok members and its ``degraded_reasons`` list.
    """

    if not isinstance(payload, dict):
        return payload
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            result[key] = value
            continue
        folded = _fold_to_identity(value)
        if isinstance(value, dict):
            for member in _SOURCE_GRAPH_HEALTH_KEPT_MEMBERS:
                kept = value.get(member)
                if member in value and (kept is None or isinstance(kept, (str, int, float, bool))):
                    folded[member] = kept
            if isinstance(value.get("degraded_reasons"), list):
                folded["degraded_reasons"] = value["degraded_reasons"]
        result[key] = folded
    return result


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
    """Fold contract text and repeated retry/reroute prose, not lifecycle facts."""
    result = fold_task_card_contract(fold_task_card_facts(card))
    if not isinstance(result, dict):
        return result
    minimality = result.get("minimality_contract")
    if minimality:
        result["minimality_contract"] = _fold_to_identity(minimality)
    for key in ("identity_reroute", "terminal_retry"):
        value = result.get(key)
        if not isinstance(value, dict):
            continue
        provenance = dict(value)
        if provenance.get("reason"):
            provenance["reason"] = _fold_to_identity(provenance["reason"])
        authorization = provenance.get("operational_provider_authorization")
        if isinstance(authorization, dict) and authorization.get("reason"):
            authorization = dict(authorization)
            authorization["reason"] = _fold_to_identity(authorization["reason"])
            provenance["operational_provider_authorization"] = authorization
        result[key] = provenance
    return result


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
    # Core has already computed these summaries' identity. Drop only the redundant
    # field-name inventory; retain its original digest, bytes and every fact.
    for key in ("project_context", "worker_mcp_gate", "token_budget", "quality_gate"):
        value = result.get(key)
        if isinstance(value, dict) and value.get("summarized") is True and "keys" in value:
            result[key] = {name: fact for name, fact in value.items() if name != "keys"}
    # Named receipts contain repeatable manifests/registration, not live state.
    # Keep explicit unknown/observed flags, errors and operational facts visible.
    for key, facts in (
        ("review_automation", ("state", "chain_identity_sha256")),
        ("attempt_artifact_manifest", ("attempt_id", "artifact_count", "verified")),
        ("project_context_delivery", ("injected", "section_count", "bundle_bytes")),
    ):
        value = result.get(key)
        if not isinstance(value, dict) or not value or value.get("summarized") is True:
            continue
        receipt = _fold_to_identity(value)
        for name, fact in value.items():
            if (name in facts or isinstance(fact, bool) or fact is None
                    or name in ("status", "verdict", "reason", "error", "unknown_reason",
                                "telemetry_reason", "evidence_observed")):
                receipt[name] = fact
        result[key] = receipt
    return result


def _scalar_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _canonical_texts(root: Any) -> dict[int, str]:
    """Canonical JSON text of every dict/list node, composed bottom-up (O(n))."""

    texts: dict[int, str] = {}

    def visit(node: Any) -> str:
        if isinstance(node, dict):
            items = sorted(((str(k), v) for k, v in node.items()), key=lambda kv: kv[0])
            text = "{" + ",".join(f"{_scalar_text(k)}:{visit(v)}" for k, v in items) + "}"
        elif isinstance(node, list):
            text = "[" + ",".join(visit(v) for v in node) + "]"
        else:
            return _scalar_text(node)
        texts[id(node)] = text
        return text

    visit(root)
    return texts


def _child_path(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    text = str(key)
    if text.isidentifier():
        return f"{path}.{text}"
    return f"{path}[{json.dumps(text, ensure_ascii=False)}]"


def dedupe_repeated_subtrees(value: Any, min_bytes: int = 2048) -> Any:
    """Replace repeated dict/list subtrees >= ``min_bytes`` with ``same_as`` refs.

    Lossless: ``expand_same_as`` restores the input. The first occurrence in
    depth-first document order is kept whole; later identical copies become
    ``{"same_as": path, "sha256", "bytes"}``. The input is not mutated.
    """

    texts = _canonical_texts(value)
    seen: dict[str, str] = {}

    def walk(node: Any, path: str) -> Any:
        text = texts.get(id(node))
        if text is None:
            return node
        encoded = text.encode("utf-8")
        if len(encoded) >= min_bytes:
            earlier = seen.get(text)
            if earlier is not None:
                return {
                    "same_as": earlier,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "bytes": len(encoded),
                }
            seen[text] = path
        if isinstance(node, dict):
            return {k: walk(v, _child_path(path, k)) for k, v in node.items()}
        return [walk(v, _child_path(path, i)) for i, v in enumerate(node)]

    return walk(value, "$")


def _is_ref(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and set(node) == {"same_as", "sha256", "bytes"}
        and isinstance(node["same_as"], str)
    )


def _resolve_path(root: Any, path: str) -> Any:
    node = root
    i = 1
    while i < len(path):
        if path[i] == ".":
            j = i + 1
            while j < len(path) and path[j] not in ".[":
                j += 1
            node = node[path[i + 1 : j]]
            i = j
        elif path[i + 1] == '"':
            key, end = json.JSONDecoder().raw_decode(path, i + 1)
            node = node[key]
            i = end + 1
        else:
            j = path.index("]", i)
            node = node[int(path[i + 1 : j])]
            i = j + 1
    return node


def expand_same_as(value: Any) -> Any:
    """Inverse of ``dedupe_repeated_subtrees``: restore the exact original value.

    Each ref names an earlier, non-ancestor path of the deduped input, so it is
    resolved against the input and the target is expanded recursively.
    """

    def build(node: Any, path: str) -> Any:
        if _is_ref(node):
            ref = node["same_as"]
            return build(_resolve_path(value, ref), ref)
        if isinstance(node, dict):
            return {k: build(v, _child_path(path, k)) for k, v in node.items()}
        if isinstance(node, list):
            return [build(v, _child_path(path, i)) for i, v in enumerate(node)]
        return node

    return build(value, "$")
