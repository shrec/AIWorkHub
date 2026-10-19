"""Worker-MCP live-call completion gate, relocated from ``process_launcher``.

Moved verbatim under the module size ratchet; ``process_launcher`` re-exports
every name so ``process_launcher._worker_mcp_live_call_gate`` and the
context-section helpers resolve to these same objects.  This module must never
import ``process_launcher``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import repo_policy
from . import worker_ai_tools_mcp
from .process_launcher_evidence import (
    SOURCE_GRAPH_LIVE_CALLS_ALL_FAILED as _SOURCE_GRAPH_LIVE_CALLS_ALL_FAILED,
    is_rework_attempt as _is_rework_attempt,
    source_graph_orientation_voided as _source_graph_orientation_voided,
)
from .process_launcher_provider_failures import _project_context_receipt_from_output



# Sections the launcher can inject and that the worker MCP gate also accepts as
# a live tool. A section name outside this set is never credited.
_GATEABLE_CONTEXT_SECTIONS = frozenset(
    {"source_graph", "session_current_state", "ai_memory", "kb"}
)


def _executed_context_sections(context: dict[str, Any]) -> set[str]:
    """Gateable sections the launcher executed for this request without
    degradation.

    ``hit_count`` is deliberately NOT required to be > 0: an executed section
    that returned zero rows (e.g. AI Memory with no matches) is a valid, real
    result, never a "missing call" (B948/B951 regression). A degraded / stale /
    failed section is not credited, so a live recovery call is still required.
    """
    satisfied: set[str] = set()
    for section in context.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = str(section.get("name") or "")
        if name not in _GATEABLE_CONTEXT_SECTIONS:
            continue
        if not section.get("executed"):
            continue
        if str(section.get("degraded_reason") or "").strip():
            continue
        satisfied.add(name)
    return satisfied


def _coordinator_measured_zero_hit_sections(context: dict[str, Any]) -> set[str]:
    """Gateable sections the launcher executed, without degradation, whose
    canonical query measured exactly zero rows for this request.

    That zero is the coordinator's own measurement, taken before the worker
    started; no model text and no live re-call can change it.
    """
    measured: set[str] = set()
    for section in context.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = str(section.get("name") or "")
        if name not in _GATEABLE_CONTEXT_SECTIONS or not section.get("executed"):
            continue
        if str(section.get("degraded_reason") or "").strip():
            continue
        hit_count = section.get("hit_count")
        if type(hit_count) is int and hit_count == 0:
            measured.add(name)
    return measured


def _coordinator_bound_context_sections(metadata: dict[str, Any]) -> set[str] | None:
    """Sections the coordinator itself proves it injected into this request.

    The launcher recorded, at launch, the collected bundle's ``bundle_sha256``
    (``project_context``) and a delivery receipt naming the sha of the bundle
    it actually wrote into the prompt (``project_context_delivery``). When the
    delivery says ``injected`` and both shas agree, the bundle reached the
    worker by construction; the caller additionally requires the request's
    HMAC audit ledger to verify, which binds this request id. ``None`` means
    the launch record does not prove injection, so nothing is credited here.
    """
    context = metadata.get("project_context") or {}
    delivery = metadata.get("project_context_delivery") or {}
    if not isinstance(context, dict) or not isinstance(delivery, dict):
        return None
    bundle_sha256 = str(context.get("bundle_sha256") or "").strip().lower()
    delivered_sha256 = str(delivery.get("bundle_sha256") or "").strip().lower()
    if (
        delivery.get("injected") is not True
        or len(bundle_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in bundle_sha256)
        or delivered_sha256 != bundle_sha256
    ):
        return None
    return _executed_context_sections(context)


def _injected_context_satisfaction(
    metadata: dict[str, Any],
    request_id: str = "",
) -> tuple[bool, set[str]]:
    """Which required tools a VERIFIED injected project-context section already
    satisfies according to the worker-typed ``PROJECT_CONTEXT_RECEIPT`` line.

    Injection and a live worker call are ALTERNATIVE valid satisfaction sources
    for the same required tool -- the launcher already ran Session Manager / AI
    Memory / KB / Source Graph and injected their results with a hash receipt,
    so a worker need not re-run them by hand.

    This is the optional telemetry path: the primary acknowledgement is derived
    server-side by :func:`_coordinator_bound_context_sections` plus a verified
    audit ledger (``coordinator_prompt_binding``), because the receipt line
    only ever copied a sha the coordinator itself wrote into the prompt.  A
    receipt is credited here only when its ``bundle_sha256`` equals the stored
    one -- that sha binds repository/scope identity (the bundle embeds
    ``repo_identity.scope_root``) -- so a tampered, repo-mismatched, or
    unacknowledged receipt yields ``(False, set())`` from this path.  Sections
    are credited by :func:`_executed_context_sections`.
    """
    context = metadata.get("project_context") or {}
    if not isinstance(context, dict):
        return False, set()
    bundle_sha256 = str(context.get("bundle_sha256") or "").strip()
    stdout_path = str(metadata.get("stdout_path") or "").strip()
    if not bundle_sha256 or not stdout_path:
        return False, set()
    # Preserve the pre-existing bundle-bound acknowledgement contract for
    # Session Manager, AI Memory, and KB. Request binding is evaluated
    # separately at the Source Graph substitution boundary below.
    receipt = _project_context_receipt_from_output(
        Path(stdout_path),
        expected_bundle_sha256=bundle_sha256,
    )
    if not receipt.get("acknowledged"):
        return False, set()
    return True, _executed_context_sections(context)


def _decode_ledger_int(value: Any) -> int | None:
    """Decode one authenticated audit-ledger numeric field, or ``None``.

    A malformed number from the authenticated ledger is a named refusal, never
    an exception escaping the completion-gate boundary.  ``None`` (absent) reads
    as ``0`` to preserve the historic ``or 0`` semantics; any non-integral or
    non-numeric shape is reported as malformed.

    ``int()`` is the predicate, not ``str.isdigit()``: the latter admits shapes
    ``int()`` rejects (``'--5'``, superscript ``'²'``) and silently accepts
    non-ASCII digits (``'١٢٣'``) from an authenticated ledger.  We refuse
    anything but ASCII digits with an optional single leading ``-`` and let
    ``int()`` make the final decision, so no shape this guard admits can raise
    past this boundary.
    """

    if value is None:
        return 0
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        body = stripped[1:] if stripped.startswith("-") else stripped
        if not body.isascii() or not body.isdigit():
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _worker_mcp_live_call_gate(metadata: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Bounded, redacted B833 completion-gate summary.

    Gated for ``task_context_policy.task_type == "code"`` according to the
    repository's two explicit tool-policy switches.  A ``project_context``
    contract must be present to reach that value at all.
    Data-classification and research tasks -- and any task without a
    ``project_context`` contract -- are exempt and never blocked here; they
    keep whatever policy already applied to them (e.g. the immutable input
    shard for data tasks). Fails CLOSED on a gated task: a missing worker_mcp
    runtime record, an unreadable ledger, or zero verified live
    ``source_graph`` calls all resolve to ``satisfied: False``. Source Graph
    must be fresh and non-empty; Session Manager and requested Memory/KB
    sections must have a successful canonical call. A denied malformed or
    out-of-scope tool request remains visible as policy-warning telemetry but
    is not itself terminal once every required canonical call is satisfied:
    the denied request returned no data and granted no capability, so forcing
    a full worker rerun after recovery only discards valid work. A tampered or
    forged ledger line is dropped by ``verify_audit_ledger`` before it ever
    reaches this count, so a worker cannot satisfy the gate by writing text
    that merely looks like an audit entry.
    """
    inherited_gate_receipt = (metadata.get("worker_mcp") or {}).get(
        "inherited_predecessor_gate"
    )
    if metadata.get("execution_mode") == "validation_only_replay" and isinstance(
        inherited_gate_receipt, dict
    ):
        predecessor = metadata.get("rework_predecessor") or {}
        authorization = metadata.get("validation_only_replay_authorization") or {}
        bindings_match = (
            inherited_gate_receipt.get("task_id") == metadata.get("task_id")
            and inherited_gate_receipt.get("predecessor_request_id")
            == predecessor.get("request_id")
            == authorization.get("predecessor_request_id")
            and inherited_gate_receipt.get("changed_path_hashes")
            == predecessor.get("changed_path_hashes")
            == authorization.get("changed_path_hashes")
            and inherited_gate_receipt.get("next_claim_epoch")
            == metadata.get("claim_epoch")
            == authorization.get("next_claim_epoch")
            and (
                not authorization.get("request_id")
                or (
                    inherited_gate_receipt.get("request_id")
                    == authorization.get("request_id")
                    == metadata.get("request_id")
                    == request_id
                    and inherited_gate_receipt.get("repo")
                    == authorization.get("repo")
                    == (metadata.get("workspace") or {}).get("repo")
                )
            )
        )
        inherited_gate = inherited_gate_receipt.get("worker_mcp_gate")
        inherited_verification = (
            inherited_gate.get("verification")
            if isinstance(inherited_gate, dict)
            else None
        )
        if (
            not bindings_match
            or not isinstance(inherited_gate, dict)
            or inherited_gate.get("gated") is not True
            or inherited_gate.get("satisfied") is not True
            or not isinstance(inherited_verification, dict)
            or inherited_verification.get("ok") is not True
        ):
            return {
                "gated": True,
                "task_type": str(inherited_gate_receipt.get("task_type") or "code"),
                "required_tools": list(
                    inherited_gate_receipt.get("required_tools") or []
                ),
                "missing_tools": [],
                "satisfied": False,
                "reason": "validation_only_replay_predecessor_mcp_receipt_mismatch",
                "observation_only": False,
                "inherited_predecessor_evidence": True,
            }
        result = dict(inherited_gate)
        required_tools = list(result.get("required_tools") or [])
        result.update({
            "gated": True,
            "satisfied": True,
            "observation_only": False,
            "required_tools": required_tools,
            "missing_tools": [],
            "reason": "",
            "inherited_predecessor_evidence": True,
            "fresh_current_request_worker_calls": False,
            "satisfaction_by_tool": {
                tool: "authenticated_predecessor_gate" for tool in required_tools
            },
        })
        return result

    context_metadata = metadata.get("project_context") or {}
    task_type = str(
        (context_metadata.get("task_context_policy") or {}).get("task_type") or ""
    )
    context_required = context_metadata.get("required") is True
    worker_mcp_meta = metadata.get("worker_mcp") or {}
    sections = context_metadata.get("sections") or []
    tools_policy = dict(repo_policy.DEFAULT_POLICY["tools"])
    policy_error = ""
    authority_repo = worker_mcp_meta.get("authority_repo")
    if isinstance(authority_repo, str) and authority_repo.strip():
        try:
            tools_policy = dict(
                repo_policy.load_policy(Path(authority_repo))["tools"]
            )
        except repo_policy.RepoPolicyError as exc:
            policy_error = f"repo_policy_invalid:{exc}"
    # Sections the coordinator executed for this request and measured at zero
    # rows: a fact taken before the worker started, independent of any model
    # text or live re-call.  Degraded sections are never in this set.
    coordinator_zero_hit = (
        _coordinator_measured_zero_hit_sections(context_metadata)
        if isinstance(context_metadata, dict)
        else set()
    )
    gate_required_tools: list[str] = []
    exempted_tools: dict[str, str] = {}
    if task_type == "code" and tools_policy.get("source_graph_required_for_code"):
        gate_required_tools.append("source_graph")
    if (
        task_type == "code"
        and tools_policy.get("session_memory_kb_required_for_nontrivial")
    ):
        for section in sections:
            if not isinstance(section, dict) or not section.get("requested", True):
                continue
            name = str(section.get("name") or "")
            if (
                name in {"session_current_state", "ai_memory", "kb"}
                and name not in gate_required_tools
            ):
                if name == "session_current_state" and name in coordinator_zero_hit:
                    # The canonical session query IS the topic-scoped store:
                    # zero rows at launch means the store holds no document
                    # for this card's topic, so no live call can return
                    # anything and requiring one is ceremony.  Record why the
                    # tool is not required; a degraded section never reaches
                    # this branch and stays required.
                    exempted_tools[name] = "session_store_empty_for_topic_at_launch"
                    continue
                gate_required_tools.append(name)
    # An explicit required project-context contract is stronger than the
    # repository's generic code-task defaults.  Research/read-only cards use
    # this path too, so derive their blocking tools from the exact requested
    # sections instead of silently classifying them as observation-only.
    if context_required:
        for section in sections:
            if not isinstance(section, dict) or not section.get("requested", True):
                continue
            name = str(section.get("name") or "")
            if name in _GATEABLE_CONTEXT_SECTIONS and name not in gate_required_tools:
                gate_required_tools.append(name)
    gated = bool(gate_required_tools) and (task_type == "code" or context_required)
    gate_result: dict[str, Any] = {
        "gated": gated,
        "task_type": task_type,
        "project_context_required": context_required,
        "required_tools": gate_required_tools if gated else [],
        "exempted_tools": exempted_tools,
        "missing_tools": [],
        "satisfied": True,
        "reason": "",
        "satisfaction_by_tool": {},
        "injected_context_acknowledged": False,
        "injected_context_acknowledgement_source": "",
        "observation_only": not gated,
        "telemetry_observed": False,
        "telemetry_reason": "",
        "policy_warning": False,
        "policy_warning_count": 0,
        "warnings": [],
        "tools_policy": {
            "source_graph_required_for_code": bool(
                tools_policy.get("source_graph_required_for_code")
            ),
            "session_memory_kb_required_for_nontrivial": bool(
                tools_policy.get("session_memory_kb_required_for_nontrivial")
            ),
        },
    }
    # Receipt acknowledgement is evidence truth, not merely a code-task gate
    # implementation detail.  Keep it observable for exempt research and
    # data-classification tasks as well, while preserving their non-gated
    # completion semantics.
    injected_acknowledged, injected_tools = _injected_context_satisfaction(
        metadata, request_id
    )
    acknowledgement_source = "worker_receipt" if injected_acknowledged else ""
    gate_result["injected_context_acknowledged"] = injected_acknowledged
    gate_result["injected_context_acknowledgement_source"] = acknowledgement_source
    source_graph_injected_acknowledged = False
    context = metadata.get("project_context") or {}
    if injected_acknowledged and isinstance(context, dict):
        source_graph_receipt = _project_context_receipt_from_output(
            Path(str(metadata.get("stdout_path") or "")),
            expected_bundle_sha256=str(context.get("bundle_sha256") or ""),
            expected_request_id=request_id,
        )
        source_graph_injected_acknowledged = bool(
            source_graph_receipt.get("acknowledged")
        )
    if policy_error:
        gate_result["gated"] = True
        gate_result["satisfied"] = False
        gate_result["reason"] = policy_error
        return gate_result
    ledger_path = worker_mcp_meta.get("audit_ledger_path")
    key_path = worker_mcp_meta.get("audit_hmac_key_path")
    if not ledger_path or not key_path:
        gate_result["telemetry_reason"] = "worker_mcp_runtime_not_provisioned"
        if gated:
            gate_result["satisfied"] = False
            gate_result["reason"] = "worker_mcp_runtime_not_provisioned"
        return gate_result
    verification = worker_ai_tools_mcp.verify_audit_ledger(
        Path(str(ledger_path)),
        Path(str(key_path)),
        task_id=str(metadata["task_id"]),
        runner=str(metadata["runner"]),
        topic=str(metadata["topic"]),
        request_id=request_id,
    )
    # Bounded/redacted by construction: verify_audit_ledger never returns raw
    # paths, prompts, or database contents -- only counts and a short reason.
    gate_result["verification"] = {
        k: v for k, v in verification.items() if k != "schema_id"
    }
    gate_result["telemetry_observed"] = bool(verification.get("ok"))
    gate_result["telemetry_reason"] = str(verification.get("reason") or "")
    # Server-derived acknowledgement: the coordinator wrote the bundle into
    # this request's prompt (delivery receipt sha == collected bundle sha) and
    # the HMAC audit ledger verifies for this exact request id.  Both inputs
    # are coordinator facts; the worker-typed receipt line above only ever
    # copied a sha the coordinator itself rendered, so its absence or a typo
    # in it is telemetry, never grounds to discard verified work.
    if not injected_acknowledged and verification.get("ok") is True:
        bound_sections = _coordinator_bound_context_sections(metadata)
        if bound_sections is not None:
            injected_acknowledged = True
            injected_tools = bound_sections
            source_graph_injected_acknowledged = True
            acknowledgement_source = "coordinator_prompt_binding"
            gate_result["injected_context_acknowledged"] = True
            gate_result["injected_context_acknowledgement_source"] = (
                acknowledgement_source
            )
    # Authenticated-ledger numeric decoding fails closed with a named refusal:
    # a malformed count is never an exception escaping this gate boundary.
    policy_violations = _decode_ledger_int(verification.get("policy_violations"))
    live_source_graph_calls = _decode_ledger_int(
        verification.get("live_source_graph_calls")
    )
    successful_raw = verification.get("successful_call_count_by_tool")
    if successful_raw is None:
        successful_raw = {}
    successful: dict[str, int] = {}
    ledger_decode_failure = ""
    if policy_violations is None:
        ledger_decode_failure = "policy_violations"
    elif live_source_graph_calls is None:
        ledger_decode_failure = "live_source_graph_calls"
    elif not isinstance(successful_raw, dict):
        ledger_decode_failure = "successful_call_count_by_tool"
    else:
        for tool_name, raw_count in successful_raw.items():
            decoded_count = _decode_ledger_int(raw_count)
            if decoded_count is None:
                ledger_decode_failure = (
                    "successful_call_count_by_tool:" + str(tool_name)
                )
                break
            successful[str(tool_name)] = decoded_count
    if ledger_decode_failure:
        gate_result["gated"] = True
        gate_result["satisfied"] = False
        gate_result["reason"] = (
            "audit_ledger_numeric_decode_failed:" + ledger_decode_failure
        )
        return gate_result
    assert policy_violations is not None
    assert live_source_graph_calls is not None
    gate_result["policy_warning"] = policy_violations > 0
    gate_result["policy_warning_count"] = policy_violations
    if policy_violations:
        gate_result["warnings"] = [
            f"denied_aiworkhub_tool_requests_recovered:{policy_violations}"
        ]
    # A receipt that declares itself blocking is authority to refuse, not a
    # cosmetic flag.  Consult it directly so acceptance can never promote a
    # card over a self-declared-blocking audit receipt, and surface the exact
    # blocker so a manager sees why.  A field asserting authority it does not
    # have is worse than none.
    receipt_conformance = verification.get("receipt_conformance")
    if isinstance(receipt_conformance, dict) and receipt_conformance.get("blocking"):
        blockers = [
            str(item)
            for item in (receipt_conformance.get("blockers") or [])
            if str(item)
        ]
        gate_result["gated"] = True
        gate_result["satisfied"] = False
        gate_result["receipt_conformance_blocking"] = True
        gate_result["reason"] = (
            "receipt_conformance_blocking:" + ",".join(blockers)
            if blockers
            else "receipt_conformance_blocking"
        )
        return gate_result
    if not gated:
        return gate_result
    # ``successful`` was decoded above with fail-closed numeric handling.
    # The canonical supervisor-owned Source Graph query is initial-orientation
    # evidence once its exact bundle receipt is acknowledged. A later live,
    # authoritative worker call remains stronger evidence and takes precedence.
    satisfaction_by_tool: dict[str, str] = {}
    missing: list[str] = []
    stale: list[str] = []
    # Source Graph freshness is invocation truth: an authenticated, successful,
    # non-cached authoritative worker call satisfies continuous use even when
    # the bounded query returns zero rows. An acknowledged supervisor section
    # proves the same initial call was already executed before launch. Cached,
    # failed, degraded, non-authoritative, or unverified evidence is excluded
    # from injected_tools and remains fail-closed.
    successful_source_graph = _decode_ledger_int(successful.get("source_graph")) or 0
    failed_counts = verification.get("failed_call_count_by_tool")
    # A PRESENT but non-dict container is malformed ledger shape, not an absent
    # failure record: it voids orientation (None) instead of crediting it as 0.
    failed_source_graph = (
        _decode_ledger_int(failed_counts.get("source_graph"))
        if isinstance(failed_counts, dict)
        else _decode_ledger_int(None) if failed_counts is None else None
    )
    if live_source_graph_calls > 0:
        satisfaction_by_tool["source_graph"] = "live_worker_call"
    elif _source_graph_orientation_voided(
        failed_source_graph,
        live_calls=live_source_graph_calls,
        successful_calls=successful_source_graph,
    ):
        # NF-2026-01370: this worker DID call Source Graph live and every call
        # failed.  Injected orientation is the coordinator's own pre-launch
        # query, so it credits nothing here: the tool is missing and the typed
        # marker records why.
        satisfaction_by_tool["source_graph"] = _SOURCE_GRAPH_LIVE_CALLS_ALL_FAILED
        missing.append("source_graph")
    elif (
        verification.get("ok") is True
        and source_graph_injected_acknowledged
        and "source_graph" in injected_tools
    ):
        satisfaction_by_tool["source_graph"] = "supervisor_injected_orientation"
    elif successful_source_graph > 0:
        satisfaction_by_tool["source_graph"] = "stale_or_cached"
        stale.append("source_graph")
    else:
        missing.append("source_graph")
    rework_attempt = _is_rework_attempt(metadata)
    for tool in gate_required_tools:
        if tool == "source_graph":
            continue
        if int(successful.get(tool) or 0) > 0:
            satisfaction_by_tool[tool] = "live_worker_call"
        elif injected_acknowledged and tool in injected_tools:
            satisfaction_by_tool[tool] = (
                "coordinator_prompt_binding"
                if acknowledgement_source == "coordinator_prompt_binding"
                else "injected_receipt"
            )
        elif tool in coordinator_zero_hit:
            # The coordinator ran the canonical query for this request and
            # measured zero rows; that measurement is the answer, independent
            # of a model-typed receipt or a live re-call of the same query.
            satisfaction_by_tool[tool] = "coordinator_zero_hit_measurement"
        elif rework_attempt:
            # A rework is a validation-only replay of an already-green
            # predecessor delta; it structurally does not re-issue the context
            # tool calls the predecessor already made.  Honor the predecessor's
            # receipts here instead of discarding green work over a context
            # call this rework path never makes.  Source Graph freshness above
            # is still enforced per attempt.
            satisfaction_by_tool[tool] = "rework_predecessor_receipt"
        else:
            missing.append(tool)
    gate_result["missing_tools"] = missing
    gate_result["stale_tools"] = stale
    gate_result["satisfaction_by_tool"] = satisfaction_by_tool
    if not verification.get("ok") or missing or stale:
        gate_result["satisfied"] = False
        reasons: list[str] = []
        if missing:
            prefix = (
                "worker_mcp_required_tools_missing:"
                if context_required
                else "required_aiworkhub_mcp_calls_missing:"
            )
            reasons.append(prefix + ",".join(missing))
        if stale:
            reasons.append("source_graph_stale_or_cached:" + ",".join(stale))
        gate_result["reason"] = verification.get("reason") or "; ".join(reasons)
    return gate_result
