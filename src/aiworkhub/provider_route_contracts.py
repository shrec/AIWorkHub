"""Protocol-first registry of what each provider route can actually complete.

A launch route advertises two very different things, and AIWorkHub used to
conflate them:

* whether the route can be *started* here -- a binary resolves, a credential
  file exists, an editor host answers and consent was granted; and
* whether the route can *complete a given unit of work* -- for example carry a
  reviewer's evidence read and its report back into the canonical store.

``repo_policy.build_preflight`` measured only the first and published it as
``launchable`` / ``ready_unverified``.  For the editor-hosted routes every
signal behind that verdict (``bridge_module_present``, ``host_count``,
``access_observed``) is a fact about the bridge being present and consented
to; none is a fact about a round trip completing.  A router reading
``launchable`` as a blanket yes could therefore spend a whole card on a route
that cannot finish the job (NF-2026-00669).

This module is the registry half of RM-2026-00033 phase 1.  It is keyed on the
ROUTE FAMILY -- the protocol a route speaks -- not on a model or adapter name.
``glm_vscode_lm`` and ``deepseek_vscode_lm`` are two models over one editor
bridge; they share a transport, a tool-dispatch allowlist and an event
envelope, so a capability statement true of one is true of the other, and a
new model added to that family inherits the contract with no code change.

Fail-closed is the whole point
------------------------------
The defect being closed is a *bridge* fact standing in for a *round-trip*
fact.  Replacing it with a *registry* fact standing in for a round-trip fact
would be the same defect wearing a different hat.  So every capability record
carries the class of evidence behind it, and:

* ``supported`` is returned ONLY for a capability explicitly declared with a
  real evidence class;
* an undeclared capability, an unknown family and an unknown capability name
  are all ``unknown`` -- never ``supported``, and never silently ``false``
  either, because "we never measured it" and "we measured it and it does not
  work" are different facts that a router must be able to tell apart;
* ``route_can_complete`` is true for ``supported`` alone, so a caller that
  reads it fails closed on everything else.

This mirrors the repository doctrine ``unmeasured_must_not_read_as_measured_empty``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from . import runtime_adapters

PROVIDER_ROUTE_CONTRACT_SCHEMA_ID = "aiworkhub.provider_route_contract.v1"

# ── Capability states ──────────────────────────────────────────────────────
# Three values, not a boolean.  A boolean cannot distinguish "measured and it
# does not work" from "never measured", and that distinction is the entire
# subject of NF-2026-00669.
CAPABILITY_SUPPORTED = "supported"
CAPABILITY_UNSUPPORTED = "unsupported"
CAPABILITY_UNKNOWN = "unknown"

# ── Evidence classes, weakest to strongest ─────────────────────────────────
# The class travels with every record so a reader can weigh the claim instead
# of trusting the verdict.  ``observed_round_trip`` is the only class that
# means a real end-to-end exchange completed; nothing in this repository has
# earned it yet, and pretending otherwise is the defect this registry exists
# to prevent.
EVIDENCE_UNVERIFIED = "unverified"
EVIDENCE_DECLARED_FROM_DOCUMENTATION = "declared_from_documentation"
EVIDENCE_DECLARED_FROM_CODE_PATH = "declared_from_code_path"
EVIDENCE_OBSERVED_ROUND_TRIP = "observed_round_trip"

_EVIDENCE_CLASSES = frozenset(
    {
        EVIDENCE_UNVERIFIED,
        EVIDENCE_DECLARED_FROM_DOCUMENTATION,
        EVIDENCE_DECLARED_FROM_CODE_PATH,
        EVIDENCE_OBSERVED_ROUND_TRIP,
    }
)

# Explicit stand-in for a field whose true value is not known here.  An empty
# string would read as "measured and empty"; this reads as what it is.
UNKNOWN_VALUE = "unknown"

# Verification vocabulary for the registry's own documentation provenance.
VERIFICATION_NEVER_RUN = "never_verified"

# ── Capability vocabulary ──────────────────────────────────────────────────
# Named units of work a route either can or cannot complete.  Deliberately
# small: every entry below is backed by a code path someone read.
CAPABILITY_REVIEWER_SUBMIT = "reviewer_submit"
CAPABILITY_REVIEWER_PACKET_READ = "reviewer_packet_read"
CAPABILITY_WORKER_SEMANTIC_EDIT = "worker_semantic_edit"
CAPABILITY_SOURCE_GRAPH_QUERY = "source_graph_query"

CAPABILITY_VOCABULARY: tuple[str, ...] = (
    CAPABILITY_REVIEWER_SUBMIT,
    CAPABILITY_REVIEWER_PACKET_READ,
    CAPABILITY_WORKER_SEMANTIC_EDIT,
    CAPABILITY_SOURCE_GRAPH_QUERY,
)

# ── Exact reasons ──────────────────────────────────────────────────────────
# Snake_case reason tokens in the vocabulary the preflight surface already
# speaks, so an exclusion reads the same way an ``access_unavailable`` or a
# ``quota_unobserved`` one does.
REASON_NOT_DECLARED = "capability_not_declared_for_route_family"
REASON_FAMILY_UNKNOWN = "route_family_unknown_capability_undetermined"
REASON_CAPABILITY_UNKNOWN = "capability_not_in_registry_vocabulary"
REASON_NO_OBSERVED_ROUND_TRIP = "no_observed_review_round_trip_on_this_route_family"
REASON_BRIDGE_TOOL_NOT_ALLOWED = "worker_bridge_tool_not_allowed"


@dataclass(frozen=True)
class CapabilityRecord:
    """One capability statement plus the evidence that backs it."""

    capability: str
    state: str
    evidence_class: str
    evidence: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "state": self.state,
            "evidence_class": self.evidence_class,
            "evidence": self.evidence,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RouteContract:
    """The contract one protocol family offers, shared by every model on it."""

    route_family: str
    transport: str
    protocol: str
    protocol_version: str
    model_families: tuple[str, ...]
    documentation_urls: tuple[str, ...]
    documentation_retrieved_at: str
    documentation_digest: str
    last_verification: str
    capabilities: Mapping[str, CapabilityRecord]

    def as_dict(self) -> dict[str, Any]:
        return {
            "route_family": self.route_family,
            "transport": self.transport,
            "protocol": self.protocol,
            "protocol_version": self.protocol_version,
            "model_families": list(self.model_families),
            "documentation_urls": list(self.documentation_urls),
            "documentation_retrieved_at": self.documentation_retrieved_at,
            "documentation_digest": self.documentation_digest,
            "last_verification": self.last_verification,
            "capabilities": {
                name: record.as_dict()
                for name, record in sorted(self.capabilities.items())
            },
        }


def _record(
    capability: str,
    state: str,
    evidence_class: str,
    evidence: str = "",
    reason: str = "",
) -> CapabilityRecord:
    return CapabilityRecord(
        capability=capability,
        state=state,
        evidence_class=evidence_class,
        evidence=evidence[:200],
        reason=reason[:200],
    )


# ---------------------------------------------------------------------------
# The contracts.
#
# Every ``supported`` and every ``unsupported`` below cites the exact code path
# that was read to justify it.  Everything else is ``unknown`` -- including
# capabilities on families nobody has measured -- because an honest gap is the
# only thing that keeps this registry better than the bridge-presence signal it
# replaces.
# ---------------------------------------------------------------------------

# The editor bridge dispatches a fixed tool allowlist; anything outside it
# returns ``worker_bridge_tool_not_allowed``.  That allowlist is the ground
# truth for what an editor-hosted worker can and cannot do.
#
# The citation names the FUNCTION, not a line range.  It used to say
# ``process_launcher.py:12093-12172`` and it moved three times in one day --
# every unrelated insertion above the dispatch chain re-broke
# ``test_declared_code_path_claims_match_the_bridge_allowlist``, which is the
# same silent-rot failure mode the test exists to prevent, just inverted: a
# loud failure that says nothing about the fact being claimed.  The test
# already resolves this function by AST name; a symbolic citation is the fact
# it was actually asserting, so the span is now derived on both sides and
# there is nothing left to drift.
_EDITOR_BRIDGE_DISPATCH = (
    "src/aiworkhub/process_launcher.py::invoke_vscode_lm_worker_tool"
)

_EDITOR_VSCODE_LM_CONTRACT = RouteContract(
    route_family=runtime_adapters.ROUTE_FAMILY_EDITOR_VSCODE_LM,
    transport="editor_hosted_coordinator_bridge",
    protocol="vscode_language_model_api",
    # The editor's own API version is not observable from this control plane.
    protocol_version=UNKNOWN_VALUE,
    model_families=("glm", "deepseek", "editor_default"),
    documentation_urls=(
        "https://code.visualstudio.com/api/extension-guides/language-model",
    ),
    documentation_retrieved_at=UNKNOWN_VALUE,
    documentation_digest=UNKNOWN_VALUE,
    last_verification=VERIFICATION_NEVER_RUN,
    capabilities=MappingProxyType(
        {
            # MEASURED AND POSITIVE, as of a86934d.  The bridge dispatch
            # allowlist now carries ``aiworkhub_worker_quality_review_packet_read``,
            # so a reviewer on this family can read the file-transport packet
            # its own prompt tells it to fetch (quality_reviewer.py:578-582).
            #
            # This record was ``unsupported`` for about an hour and became
            # false the moment that allowlist changed, which is the standing
            # hazard of the whole EVIDENCE_DECLARED_FROM_CODE_PATH class: a
            # claim ABOUT a code path, restated by hand, drifts silently when
            # that path moves.  ``test_declared_code_path_claims_match_the_bridge
            # _allowlist`` derives the allowlist from the source and fails on
            # divergence, so this record cannot rot again without a red test.
            CAPABILITY_REVIEWER_PACKET_READ: _record(
                CAPABILITY_REVIEWER_PACKET_READ,
                CAPABILITY_SUPPORTED,
                EVIDENCE_DECLARED_FROM_CODE_PATH,
                evidence=_EDITOR_BRIDGE_DISPATCH,
            ),
            # NOT MEASURED.  Note carefully what is and is not known here.
            # The bridge DOES dispatch ``aiworkhub_worker_quality_review_submit``
            # (process_launcher.py:11983-11992), so the common claim that "the
            # vscode_lm transport does not carry submit" is false at the
            # dispatcher.  But that tool is not the canonical path: the reviewer
            # prompt bans every submission tool and the supervisor ingests the
            # reviewer's final assistant TEXT instead
            # (quality_reviewer.py:586-596).  Whether this family emits a final
            # event in the shape ``quality_review_ingest.provider_final_text``
            # accepts has never been observed end to end.  So the state is
            # unknown -- not true, and not false either.
            CAPABILITY_REVIEWER_SUBMIT: _record(
                CAPABILITY_REVIEWER_SUBMIT,
                CAPABILITY_UNKNOWN,
                EVIDENCE_UNVERIFIED,
                evidence="src/aiworkhub/quality_reviewer.py:586-596",
                reason=REASON_NO_OBSERVED_ROUND_TRIP,
            ),
            CAPABILITY_WORKER_SEMANTIC_EDIT: _record(
                CAPABILITY_WORKER_SEMANTIC_EDIT,
                CAPABILITY_SUPPORTED,
                EVIDENCE_DECLARED_FROM_CODE_PATH,
                evidence="src/aiworkhub/process_launcher.py:11574-11584",
            ),
            CAPABILITY_SOURCE_GRAPH_QUERY: _record(
                CAPABILITY_SOURCE_GRAPH_QUERY,
                CAPABILITY_SUPPORTED,
                EVIDENCE_DECLARED_FROM_CODE_PATH,
                evidence="src/aiworkhub/process_launcher.py:11572-11573",
            ),
        }
    ),
)

# The Claude CLI reviewer is granted its review tools explicitly, and the
# ingest allowlist understands its ``result`` final-event shape.
_CLAUDE_CLI_CONTRACT = RouteContract(
    route_family=runtime_adapters.ROUTE_FAMILY_CLAUDE_CLI,
    transport="native_cli_stdio_stream_json",
    protocol="claude_cli_stream_json",
    protocol_version=UNKNOWN_VALUE,
    model_families=("claude",),
    documentation_urls=(
        "https://docs.claude.com/en/docs/claude-code/cli-reference",
    ),
    documentation_retrieved_at=UNKNOWN_VALUE,
    documentation_digest=UNKNOWN_VALUE,
    last_verification=VERIFICATION_NEVER_RUN,
    capabilities=MappingProxyType(
        {
            CAPABILITY_REVIEWER_PACKET_READ: _record(
                CAPABILITY_REVIEWER_PACKET_READ,
                CAPABILITY_SUPPORTED,
                EVIDENCE_DECLARED_FROM_CODE_PATH,
                evidence="src/aiworkhub/runtime_adapters.py:813",
            ),
            CAPABILITY_REVIEWER_SUBMIT: _record(
                CAPABILITY_REVIEWER_SUBMIT,
                CAPABILITY_SUPPORTED,
                EVIDENCE_DECLARED_FROM_CODE_PATH,
                # Both routes exist for this family: the reviewer holds the
                # submit tool, and its ``result`` final event is in the ingest
                # allowlist so the supervisor path works too.
                evidence="src/aiworkhub/quality_review_ingest.py:35-40",
            ),
            CAPABILITY_WORKER_SEMANTIC_EDIT: _record(
                CAPABILITY_WORKER_SEMANTIC_EDIT,
                CAPABILITY_SUPPORTED,
                EVIDENCE_DECLARED_FROM_CODE_PATH,
                evidence="src/aiworkhub/runtime_adapters.py:800-806",
            ),
            CAPABILITY_SOURCE_GRAPH_QUERY: _record(
                CAPABILITY_SOURCE_GRAPH_QUERY,
                CAPABILITY_SUPPORTED,
                EVIDENCE_DECLARED_FROM_CODE_PATH,
                evidence="src/aiworkhub/runtime_adapters.py:784",
            ),
        }
    ),
)

_CODEX_CLI_CONTRACT = RouteContract(
    route_family=runtime_adapters.ROUTE_FAMILY_CODEX_CLI,
    transport="native_cli_stdio_jsonl",
    protocol="codex_cli_jsonl_items",
    protocol_version=UNKNOWN_VALUE,
    model_families=("gpt", "codex"),
    documentation_urls=("https://developers.openai.com/codex/cli/",),
    documentation_retrieved_at=UNKNOWN_VALUE,
    documentation_digest=UNKNOWN_VALUE,
    last_verification=VERIFICATION_NEVER_RUN,
    capabilities=MappingProxyType(
        {
            # The supervisor ingest path understands this family's
            # ``item.completed`` / ``agent_message`` final-event shape, which is
            # the canonical way a reviewer's report reaches the store.
            CAPABILITY_REVIEWER_SUBMIT: _record(
                CAPABILITY_REVIEWER_SUBMIT,
                CAPABILITY_SUPPORTED,
                EVIDENCE_DECLARED_FROM_CODE_PATH,
                evidence="src/aiworkhub/quality_review_ingest.py:41-45",
            ),
            # Not measured: this family does not use ``claude_allowed_tools``
            # and no code path here declares its review tool grant.
            CAPABILITY_REVIEWER_PACKET_READ: _record(
                CAPABILITY_REVIEWER_PACKET_READ,
                CAPABILITY_UNKNOWN,
                EVIDENCE_UNVERIFIED,
                reason=REASON_NOT_DECLARED,
            ),
        }
    ),
)

# Nothing about these two families has been measured for any capability in the
# vocabulary.  They are present so the registry inventories every catalog route
# rather than only the interesting ones -- and every lookup against them
# returns ``unknown``, which is the honest answer.
_COPILOT_BYOK_CLI_CONTRACT = RouteContract(
    route_family=runtime_adapters.ROUTE_FAMILY_COPILOT_BYOK_CLI,
    transport="native_cli_stdio_byok",
    protocol="openai_compatible_chat_completions",
    protocol_version=UNKNOWN_VALUE,
    model_families=("glm", "deepseek"),
    documentation_urls=(
        "https://docs.github.com/en/copilot/concepts/agents/about-copilot-cli",
    ),
    documentation_retrieved_at=UNKNOWN_VALUE,
    documentation_digest=UNKNOWN_VALUE,
    last_verification=VERIFICATION_NEVER_RUN,
    capabilities=MappingProxyType({}),
)

_KILO_XAI_CLI_CONTRACT = RouteContract(
    route_family=runtime_adapters.ROUTE_FAMILY_KILO_XAI_CLI,
    transport="native_cli_stdio",
    protocol="kilo_cli",
    protocol_version=UNKNOWN_VALUE,
    model_families=("grok",),
    documentation_urls=("https://kilocode.ai/docs",),
    documentation_retrieved_at=UNKNOWN_VALUE,
    documentation_digest=UNKNOWN_VALUE,
    last_verification=VERIFICATION_NEVER_RUN,
    capabilities=MappingProxyType({}),
)

ROUTE_CONTRACTS: Mapping[str, RouteContract] = MappingProxyType(
    {
        contract.route_family: contract
        for contract in (
            _EDITOR_VSCODE_LM_CONTRACT,
            _CLAUDE_CLI_CONTRACT,
            _CODEX_CLI_CONTRACT,
            _COPILOT_BYOK_CLI_CONTRACT,
            _KILO_XAI_CLI_CONTRACT,
        )
    }
)


def route_family_for_adapter(adapter_id: str) -> str:
    """Protocol family for one adapter id, or ``unknown``."""

    return runtime_adapters.route_family(adapter_id)


def capability_record(route_family: str, capability: str) -> CapabilityRecord:
    """Return one capability statement, ALWAYS -- unknown rather than absent.

    Returning a record for every question (including nonsense ones) is what
    lets callers treat "undeclared" and "declared unsupported" uniformly
    without any of them having to remember to handle ``None``.
    """

    if capability not in CAPABILITY_VOCABULARY:
        return _record(
            str(capability)[:128],
            CAPABILITY_UNKNOWN,
            EVIDENCE_UNVERIFIED,
            reason=REASON_CAPABILITY_UNKNOWN,
        )
    contract = ROUTE_CONTRACTS.get(route_family)
    if contract is None:
        return _record(
            capability,
            CAPABILITY_UNKNOWN,
            EVIDENCE_UNVERIFIED,
            reason=REASON_FAMILY_UNKNOWN,
        )
    declared = contract.capabilities.get(capability)
    if declared is None:
        return _record(
            capability,
            CAPABILITY_UNKNOWN,
            EVIDENCE_UNVERIFIED,
            reason=REASON_NOT_DECLARED,
        )
    return declared


def adapter_capability_record(adapter_id: str, capability: str) -> CapabilityRecord:
    """Capability statement for an adapter, resolved through its family."""

    return capability_record(route_family_for_adapter(adapter_id), capability)


def route_can_complete(route_family: str, capability: str) -> bool:
    """True ONLY for an explicitly supported capability.

    This is the fail-closed reader: ``unknown`` and ``unsupported`` are both
    false, so a caller cannot accidentally inherit a yes from silence.  Callers
    that must distinguish the two read ``capability_record`` instead.
    """

    return capability_record(route_family, capability).state == CAPABILITY_SUPPORTED


def adapter_can_complete(adapter_id: str, capability: str) -> bool:
    """``route_can_complete`` for one adapter id."""

    return route_can_complete(route_family_for_adapter(adapter_id), capability)


def describe_adapter_capabilities(adapter_id: str) -> dict[str, Any]:
    """Every vocabulary capability for one adapter, with evidence."""

    family = route_family_for_adapter(adapter_id)
    contract = ROUTE_CONTRACTS.get(family)
    return {
        "schema_id": PROVIDER_ROUTE_CONTRACT_SCHEMA_ID,
        "adapter_id": adapter_id,
        "route_family": family,
        "contract_declared": contract is not None,
        "transport": contract.transport if contract else UNKNOWN_VALUE,
        "protocol": contract.protocol if contract else UNKNOWN_VALUE,
        "protocol_version": contract.protocol_version if contract else UNKNOWN_VALUE,
        "capabilities": {
            capability: capability_record(family, capability).as_dict()
            for capability in CAPABILITY_VOCABULARY
        },
    }


def capability_exclusions(
    adapter_ids: tuple[str, ...] | list[str], capability: str
) -> list[dict[str, Any]]:
    """Routes that must NOT be selected for ``capability``, with exact reasons.

    An excluded route is named with the state that excluded it, so a reader can
    tell a measured "this transport cannot do it" from an unmeasured "nobody
    has ever checked" -- the two call for different fixes.
    """

    exclusions: list[dict[str, Any]] = []
    for adapter_id in adapter_ids:
        record = adapter_capability_record(adapter_id, capability)
        if record.state == CAPABILITY_SUPPORTED:
            continue
        exclusions.append(
            {
                "adapter_id": str(adapter_id)[:128],
                "route_family": route_family_for_adapter(adapter_id),
                "capability": capability,
                "state": record.state,
                "evidence_class": record.evidence_class,
                "reason": record.reason,
                "evidence": record.evidence,
            }
        )
    return exclusions


def registry_report() -> dict[str, Any]:
    """The whole registry, one serializable object."""

    return {
        "schema_id": PROVIDER_ROUTE_CONTRACT_SCHEMA_ID,
        "capability_vocabulary": list(CAPABILITY_VOCABULARY),
        "evidence_classes": sorted(_EVIDENCE_CLASSES),
        "route_families": {
            family: contract.as_dict()
            for family, contract in sorted(ROUTE_CONTRACTS.items())
        },
        "adapters": {
            adapter_id: route_family_for_adapter(adapter_id)
            for adapter_id in runtime_adapters.LOCAL_ADAPTERS
        },
        "note": (
            "capability state is supported only where an evidence class backs "
            "it; undeclared and unknown routes read as unknown, never as "
            "supported and never as measured-empty"
        ),
    }


__all__ = [
    "CAPABILITY_REVIEWER_PACKET_READ",
    "CAPABILITY_REVIEWER_SUBMIT",
    "CAPABILITY_SOURCE_GRAPH_QUERY",
    "CAPABILITY_SUPPORTED",
    "CAPABILITY_UNKNOWN",
    "CAPABILITY_UNSUPPORTED",
    "CAPABILITY_VOCABULARY",
    "CAPABILITY_WORKER_SEMANTIC_EDIT",
    "EVIDENCE_DECLARED_FROM_CODE_PATH",
    "EVIDENCE_DECLARED_FROM_DOCUMENTATION",
    "EVIDENCE_OBSERVED_ROUND_TRIP",
    "EVIDENCE_UNVERIFIED",
    "PROVIDER_ROUTE_CONTRACT_SCHEMA_ID",
    "REASON_BRIDGE_TOOL_NOT_ALLOWED",
    "REASON_CAPABILITY_UNKNOWN",
    "REASON_FAMILY_UNKNOWN",
    "REASON_NOT_DECLARED",
    "REASON_NO_OBSERVED_ROUND_TRIP",
    "ROUTE_CONTRACTS",
    "UNKNOWN_VALUE",
    "VERIFICATION_NEVER_RUN",
    "CapabilityRecord",
    "RouteContract",
    "adapter_can_complete",
    "adapter_capability_record",
    "capability_exclusions",
    "capability_record",
    "describe_adapter_capabilities",
    "registry_report",
    "route_can_complete",
    "route_family_for_adapter",
]
