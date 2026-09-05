"""Deliver a quality-review packet as inline content and gate reviewer launch.

This module fixes the assurance-delivery defect where a reviewer received its
candidate only as a filesystem path to ``quality_review_packet.json``.  A
reviewer on an in-process ``vscode_lm`` adapter has no file-read tool of any
kind and therefore could never open that packet, so its lens stayed
unsatisfiable while a green worker suite was blocked from acceptance.

Everything here is pure: it performs no process launch and no repository
mutation.  The functions:

* deliver the packet as content in the reviewer prompt
  (:func:`deliver_packet_content` / :func:`build_reviewer_prompt_with_content`)
  while preserving the exact ``packet_sha256`` contract, keeping the on-disk
  path only as a convenience for adapters that *do* have a file-read tool;
* assemble the reviewer prompt for the exact adapter that will run it
  (:func:`assemble_reviewer_prompt`) -- the single seam the reviewer launch
  path uses so a blind adapter is handed content and a sighted one keeps its
  path transport;
* decide when a reviewer may keep inspecting rather than being forced to submit
  after a single zero-hit Source Graph query
  (:func:`reviewer_may_query_source_graph_again` / :func:`reviewer_submit_forced`);
* choose whether a reviewer's Source Graph is scoped to its own worktree or to
  the parent repository when the worktree index has no rows
  (:func:`choose_reviewer_source_graph_scope`);
* refuse the launch immediately, naming the reason per provider, when no
  independent *sighted* reviewer exists for the target
  (:func:`assess_reviewer_availability`); and
* record the best available *independence rung* for a review
  (:func:`resolve_independence_rung`) so a single-provider installation still
  completes a review and every acceptance names exactly how independent it was.

The 0.9.74 blind-reviewer gate in :mod:`aiworkhub.quality_evidence` is not
weakened here: a report whose only finding is ``process_limit`` still fails its
lens.  Content delivery makes blind adapters able to *see*; it never accepts an
uninspected review or a same-provider review.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import quality_reviewer, runtime_adapters
from .quality_reviewer import ReviewerEvidenceError

DELIVERY_SCHEMA_ID = "aiworkhub.quality_review_packet_delivery.v1"
AVAILABILITY_SCHEMA_ID = "aiworkhub.quality_review_reviewer_availability.v1"
SCOPE_SCHEMA_ID = "aiworkhub.quality_review_source_graph_scope.v1"
INDEPENDENCE_SCHEMA_ID = "aiworkhub.quality_review_independence.v1"

DEFAULT_SUBMIT_TOOL = "aiworkhub_worker_quality_review_submit"

# Inline transport marker emitted by ``quality_reviewer.build_review_prompt``
# when the packet content is embedded rather than referenced by path.
_INLINE_PACKET_PREFIX = "QUALITY_REVIEW_PACKET: "
_CONVENIENCE_PREFIX = "QUALITY_REVIEW_PACKET_FILE_CONVENIENCE: "

# A reviewer must be allowed at least this many Source Graph inspection queries
# before submission can be forced; one zero-hit orientation query is never
# enough to end the inspection phase.  A hard maximum keeps the phase bounded.
REVIEWER_MIN_INSPECTION_QUERIES = 2
REVIEWER_MAX_INSPECTION_QUERIES = 8

# Reviewer provider availability, observed from the launch/provider layer.
AVAILABILITY_AVAILABLE = "available"
AVAILABILITY_PROVIDER_REFUSED = "provider_refused"
AVAILABILITY_QUOTA_UNOBSERVED = "quota_unobserved"

# Per-provider assessment reasons.
REASON_AVAILABLE = "available_independent_sighted"
# Retained for compatibility only.  ``assess_reviewer_availability`` no longer
# emits this: sharing the worker's provider is not a blocking reason, because
# independence is a recorded ladder (:func:`resolve_independence_rung`), not a
# vendor check.  A same-provider reviewer completes at the fresh-context rung.
REASON_SAME_PROVIDER = "same_provider_not_independent"
REASON_PROVIDER_REFUSED = "provider_refused"
REASON_QUOTA_UNOBSERVED = "quota_unobserved"
REASON_BLIND_NO_FILE_READ = "blind_adapter_no_file_read"

REFUSAL_NO_INDEPENDENT_SIGHTED = "no_independent_sighted_reviewer_available"

# Review independence ladder.
#
# Multi-model routing exists so work can be sent to a model by cost and
# difficulty; it was never a requirement that one *vendor* review another.  A
# user who has only one provider must still get a real independent review, so
# independence is a recorded ladder, best rung first, never a hard refusal:
#
#     cross_provider  ->  cross_model_same_provider  ->  same_model_fresh_context
#
# What actually makes a review independent -- the anti-anchored packet, the
# sealed candidate, the separate read-only process and the authenticated
# packet_sha256-bound submission -- holds on every rung and is unchanged by this
# classification; vendor identity never was one of them.
RUNG_CROSS_PROVIDER = "cross_provider"
RUNG_CROSS_MODEL_SAME_PROVIDER = "cross_model_same_provider"
RUNG_SAME_MODEL_FRESH_CONTEXT = "same_model_fresh_context"

# Best rung first: the ladder is walked top-to-bottom and the first rung whose
# condition holds is the one recorded.
INDEPENDENCE_LADDER = (
    RUNG_CROSS_PROVIDER,
    RUNG_CROSS_MODEL_SAME_PROVIDER,
    RUNG_SAME_MODEL_FRESH_CONTEXT,
)


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _verify_packet_digest(packet: Mapping[str, Any]) -> str:
    """Return the packet's ``packet_sha256`` after re-deriving and matching it.

    Fail-closed: the digest is recomputed over the packet body (every key except
    ``packet_sha256``) exactly as :func:`quality_reviewer.build_review_packet`
    computed it, so delivery can never change or forge the contract.
    """

    if not isinstance(packet, Mapping):
        raise ReviewerEvidenceError("review_packet_not_object")
    stored = str(packet.get("packet_sha256") or "")
    body = {key: value for key, value in packet.items() if key != "packet_sha256"}
    recomputed = hashlib.sha256(_canonical_bytes(body)).hexdigest()
    if not stored or stored != recomputed:
        raise ReviewerEvidenceError("review_packet_digest_invalid")
    return stored


def deliver_packet_content(
    packet: Mapping[str, Any], *, packet_path: str | None = None
) -> dict[str, Any]:
    """Package the review packet for delivery as content, not as a path.

    The returned ``packet_content`` is the authoritative, self-contained
    evidence a reviewer needs with no file-read tool.  ``packet_path`` is kept
    only as a convenience for reviewers that can read files.  ``packet_sha256``
    is the unchanged contract digest.
    """

    digest = _verify_packet_digest(packet)
    content = json.dumps(
        packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return {
        "schema_id": DELIVERY_SCHEMA_ID,
        "packet_sha256": digest,
        "packet_content": content,
        "packet_path": str(packet_path) if packet_path is not None else None,
        "requires_file_read": False,
    }


def capability_set_has_file_read(tool_names: Iterable[str]) -> bool:
    """True when a reviewer's exact tool capability set includes a file read."""

    return runtime_adapters.capability_set_has_file_read(tool_names)


def build_reviewer_prompt_with_content(
    packet: Mapping[str, Any],
    *,
    lens: str,
    reviewer_tool_names: Iterable[str] = (),
    submit_tool_name: str = DEFAULT_SUBMIT_TOOL,
    packet_path: str | None = None,
) -> str:
    """Render a reviewer prompt that always embeds the packet content inline.

    Unlike a path-only delivery, the returned prompt carries the full packet
    JSON, so a reviewer with no file-read tool can still inspect every candidate
    path/line and mechanical check.  When the reviewer *does* have a file-read
    tool and ``packet_path`` is provided, an explicitly non-authoritative
    convenience reference to the identical on-disk packet is appended.
    """

    # ``packet_file=None`` forces the inline transport in build_review_prompt,
    # which also re-validates the packet digest before embedding it.
    prompt = quality_reviewer.build_review_prompt(
        packet,
        lens=lens,
        submit_tool_name=submit_tool_name,
        packet_file=None,
    )
    if packet_path is not None and capability_set_has_file_read(reviewer_tool_names):
        prompt += (
            f"{_CONVENIENCE_PREFIX}{packet_path}\n"
            "The identical packet content is also written to this path for "
            "reviewers that have a file-read tool; the inline packet above is "
            "authoritative and requires no file read.\n"
        )
    return prompt


def assemble_reviewer_prompt(
    packet: Mapping[str, Any],
    *,
    lens: str,
    adapter_id: str,
    submit_tool_name: str = DEFAULT_SUBMIT_TOOL,
    packet_path: str | None = None,
    packet_root: Path | str | None = None,
    adapter_fallback_used: bool = False,
) -> str:
    """Assemble the reviewer prompt for the exact adapter that will run it.

    This is the single prompt-assembly seam used by the reviewer launch path in
    :mod:`aiworkhub.process_launcher`.  A *sighted* adapter -- one whose reviewer
    is handed a worker file-read tool -- keeps the path-referenced transport,
    which avoids passing a large packet through argv on native CLI adapters.  A
    *blind* adapter -- notably the in-process ``vscode_lm`` routes, which hand
    the model no file-read tool of any kind -- receives the packet content
    inline so it can still cite an exact packet-permitted path and line.  A
    blind reviewer is never handed only an unreadable path.

    ``packet_root`` comes from the coordinator's verified worker runtime directory;
    it is not inferred from ``packet_path`` or asserted by the reviewer.
    """

    if runtime_adapters.adapter_provides_file_read(
        adapter_id, adapter_fallback_used=adapter_fallback_used
    ):
        return quality_reviewer.build_review_prompt(
            packet,
            lens=lens,
            submit_tool_name=submit_tool_name,
            packet_file=packet_path,
            packet_root=packet_root,
        )
    return build_reviewer_prompt_with_content(
        packet,
        lens=lens,
        reviewer_tool_names=(),
        submit_tool_name=submit_tool_name,
        packet_path=packet_path,
    )


def extract_inline_packet(prompt: str) -> dict[str, Any] | None:
    """Recover the inline packet a blind reviewer reads straight from its prompt.

    Returns the parsed packet dict, or ``None`` when the prompt carries no
    inline packet.  This performs no file I/O -- it is the exact path a reviewer
    with no file-read tool uses to reach the candidate content.
    """

    for line in prompt.splitlines():
        if line.startswith(_INLINE_PACKET_PREFIX):
            encoded = line[len(_INLINE_PACKET_PREFIX):]
            try:
                parsed = json.loads(encoded)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def reviewer_may_query_source_graph_again(
    prior_query_count: int, *, prior_hit_total: int = 0
) -> bool:
    """Whether a reviewer may issue a further Source Graph query.

    A single zero-hit orientation query must never end the inspection phase, so
    the reviewer is always allowed at least ``REVIEWER_MIN_INSPECTION_QUERIES``
    queries.  Past that minimum the reviewer may keep querying only while it has
    still found nothing, and never beyond ``REVIEWER_MAX_INSPECTION_QUERIES``.
    """

    count = max(0, int(prior_query_count))
    if count >= REVIEWER_MAX_INSPECTION_QUERIES:
        return False
    if count < REVIEWER_MIN_INSPECTION_QUERIES:
        return True
    return int(prior_hit_total) <= 0


def reviewer_submit_forced(
    prior_query_count: int, *, prior_hit_total: int = 0
) -> bool:
    """Inverse of :func:`reviewer_may_query_source_graph_again`."""

    return not reviewer_may_query_source_graph_again(
        prior_query_count, prior_hit_total=prior_hit_total
    )


def _baseline_paths(workspace_baseline: Iterable[Any]) -> list[str]:
    paths: list[str] = []
    for row in workspace_baseline or ():
        if isinstance(row, Mapping):
            value = str(row.get("path") or "")
        else:
            value = str(row or "")
        if value:
            paths.append(value)
    return paths


def choose_reviewer_source_graph_scope(
    *,
    worktree_evidence: Mapping[str, Any] | None,
    workspace_baseline: Iterable[Any] = (),
    parent_repo: str | None = None,
) -> dict[str, Any]:
    """Decide whether the reviewer Source Graph uses the worktree or the parent.

    A zero-row worktree index must mean *nothing matched*, never *nothing
    exists*: when the worktree index reports no rows but ``workspace_baseline``
    lists candidate files, the reviewer Source Graph is scoped to the parent
    repository instead.  The returned record always states which scope was
    chosen and why.
    """

    counts = worktree_evidence or {}
    entity_rows = int(counts.get("entity_rows") or 0)
    edge_rows = int(counts.get("edge_rows") or 0)
    file_rows = int(counts.get("file_rows") or 0)
    baseline = _baseline_paths(workspace_baseline)
    evidence_counts = {
        "entity_rows": entity_rows,
        "edge_rows": edge_rows,
        "file_rows": file_rows,
    }
    if entity_rows > 0 or edge_rows > 0 or file_rows > 0:
        return {
            "schema_id": SCOPE_SCHEMA_ID,
            "scope": "reviewer_worktree",
            "reason": "reviewer_worktree_indexed",
            "why": (
                "the reviewer worktree index returned non-zero Source Graph "
                "evidence for its candidate files"
            ),
            "evidence_counts": evidence_counts,
            "baseline_file_count": len(baseline),
        }
    if baseline and parent_repo:
        return {
            "schema_id": SCOPE_SCHEMA_ID,
            "scope": "parent_repository",
            "parent_repo": str(parent_repo),
            "reason": "reviewer_worktree_unindexed_baseline_present",
            "why": (
                "the reviewer worktree index returned zero rows while its "
                "workspace_baseline lists candidate files, so a zero hit would "
                "otherwise falsely read as nothing exists; the reviewer Source "
                "Graph is scoped to the parent repository instead"
            ),
            "evidence_counts": evidence_counts,
            "baseline_file_count": len(baseline),
        }
    return {
        "schema_id": SCOPE_SCHEMA_ID,
        "scope": "unavailable",
        "reason": (
            "reviewer_worktree_unindexed_no_parent_scope"
            if baseline
            else "reviewer_worktree_unindexed_empty_baseline"
        ),
        "why": (
            "the reviewer worktree index returned zero rows and no parent "
            "repository scope was available to fall back to"
        ),
        "evidence_counts": evidence_counts,
        "baseline_file_count": len(baseline),
    }


def _candidate_can_inspect(
    candidate: Mapping[str, Any], *, packet_delivered_as_content: bool
) -> bool:
    if packet_delivered_as_content:
        return True
    explicit = candidate.get("file_read")
    if isinstance(explicit, bool):
        return explicit
    adapter_id = str(candidate.get("adapter_id") or "")
    fallback = bool(candidate.get("adapter_fallback_used"))
    return runtime_adapters.adapter_provides_file_read(
        adapter_id, adapter_fallback_used=fallback
    )


def assess_reviewer_availability(
    *,
    worker_provider: str,
    candidates: Sequence[Mapping[str, Any]],
    packet_delivered_as_content: bool,
) -> dict[str, Any]:
    """Refuse -- with a per-provider reason -- when no reviewer can actually see.

    Reviewer independence is a recorded *ladder* (:func:`resolve_independence_rung`),
    not a vendor check: multi-model routing exists to send work to a model by
    cost and difficulty, and a single-provider installation must still get a real
    review rather than a refusal.  So a reviewer is usable when it is (1)
    *available* (the provider did not refuse and its quota was observed) and (2)
    *sighted*, i.e. it can inspect the candidate.  A reviewer that shares the
    worker's provider is **not** blocked here -- its independence is recorded as
    the ``same_model_fresh_context`` / ``cross_model_same_provider`` rung, and the
    real independence invariants (the anti-anchored packet, the sealed candidate,
    the separate read-only process and the authenticated ``packet_sha256``-bound
    submission) hold on every rung.  Content delivery makes an otherwise-blind
    adapter sighted; it is never used to accept a refused reviewer.  When no
    candidate is both available and sighted, ``can_launch`` is False and
    ``refusal_reason`` is set, so the launch fails immediately instead of
    producing an uninspected review.
    """

    reasons_by_provider: dict[str, str] = {}
    viable: list[str] = []
    for candidate in candidates or ():
        provider = str(candidate.get("provider") or "")
        if not provider:
            continue
        availability = str(candidate.get("availability") or AVAILABILITY_AVAILABLE)
        # Provider identity is deliberately not consulted: a same-provider
        # reviewer is independent by the ladder, not refused here.
        if availability == AVAILABILITY_PROVIDER_REFUSED:
            reason = REASON_PROVIDER_REFUSED
        elif availability == AVAILABILITY_QUOTA_UNOBSERVED:
            reason = REASON_QUOTA_UNOBSERVED
        elif not _candidate_can_inspect(
            candidate, packet_delivered_as_content=packet_delivered_as_content
        ):
            reason = REASON_BLIND_NO_FILE_READ
        else:
            reason = REASON_AVAILABLE
            viable.append(provider)
        # First decisive reason per provider wins, but an ``available`` verdict
        # always upgrades a previously recorded blocking reason for it.
        if provider not in reasons_by_provider or reason == REASON_AVAILABLE:
            reasons_by_provider[provider] = reason
    viable = list(dict.fromkeys(viable))
    can_launch = bool(viable)
    return {
        "schema_id": AVAILABILITY_SCHEMA_ID,
        "worker_provider": worker_provider,
        "packet_delivered_as_content": bool(packet_delivered_as_content),
        "can_launch": can_launch,
        "viable_reviewers": viable,
        "reasons_by_provider": reasons_by_provider,
        "refusal_reason": None if can_launch else REFUSAL_NO_INDEPENDENT_SIGHTED,
    }


def resolve_independence_rung(
    *,
    worker_provider: str,
    reviewer_provider: str,
    worker_model: str = "",
    reviewer_model: str = "",
) -> dict[str, Any]:
    """Record the best available independence rung for this review.

    The ladder degrades ``cross_provider`` -> ``cross_model_same_provider`` ->
    ``same_model_fresh_context`` and never refuses: a single-provider,
    single-model installation still reaches ``same_model_fresh_context`` and can
    complete a review.  The returned record names the rung so it can be written
    onto the acceptance evidence, and every rung still relies on the sealed,
    anti-anchored packet reviewed by a separate read-only process that submits
    through the authenticated ``packet_sha256``-bound tool.
    """

    wp, rp = str(worker_provider or ""), str(reviewer_provider or "")
    wm, rm = str(worker_model or ""), str(reviewer_model or "")
    if rp != wp:
        rung = RUNG_CROSS_PROVIDER
    elif rm != wm:
        rung = RUNG_CROSS_MODEL_SAME_PROVIDER
    else:
        rung = RUNG_SAME_MODEL_FRESH_CONTEXT
    return {
        "schema_id": INDEPENDENCE_SCHEMA_ID,
        "rung": rung,
        "rung_index": INDEPENDENCE_LADDER.index(rung),
        "worker_provider": wp,
        "reviewer_provider": rp,
        "worker_model": wm,
        "reviewer_model": rm,
        "cross_provider": rp != wp,
        # Same provider *and* same model is still independent, but only because
        # the reviewer runs in a separate read-only process with a fresh
        # context against the sealed, anti-anchored packet.
        "fresh_context_required": rung == RUNG_SAME_MODEL_FRESH_CONTEXT,
    }


def independence_acceptance_line(rung_record: Mapping[str, Any] | str) -> str:
    """Render the acceptance-evidence line that records the independence rung.

    Every acceptance names its rung so an auditor sees exactly how independent
    the review was.
    """

    if isinstance(rung_record, Mapping):
        rung = str(rung_record.get("rung") or "")
    else:
        rung = str(rung_record or "")
    if rung not in INDEPENDENCE_LADDER:
        raise ReviewerEvidenceError("invalid_independence_rung")
    return f"Independent review recorded at rung: {rung}"


# ---------------------------------------------------------------------------
# Reviewer Source Graph prewarm error classification.
#
# Prewarm prebuilds the reviewer's candidate Source Graph overlay so its queries
# are fast.  It is an *optimisation* and never a correctness precondition: the
# sealed review packet already carries every candidate's content, so a reviewer
# can always fall back to inspecting the packet alone.  Therefore a path Source
# Graph *deliberately does not index* must never prevent a review.
#
# ``worker_ai_tools_mcp.prewarm_quality_review_source_graph`` surfaces such a
# path as a ``WorkerToolError`` wrapping a ``source_graph.SourceGraphError``
# whose code names why the single file was refused.  Exactly three of those
# codes mean "this file is one Source Graph is designed not to index":
#
#   * ``source_graph_single_file_excluded_glob``  -- caught by an exclude glob
#     (for example an ``eval/`` artifact or other generated evidence);
#   * ``source_graph_single_file_excluded_dir``   -- lives under an excluded
#     directory;
#   * ``source_graph_single_file_unsupported_extension`` -- its extension has no
#     language representation at all, so it can never be indexed.
#
# These are *tolerated*: the launcher skips the path, records why, and still
# launches the reviewer, which proceeds on the sealed packet alone.
#
# Every OTHER prewarm failure stays loud and refuses the launch, so real
# breakage is never swallowed:
#   * a hash mismatch / missing-expected-hash means the candidate on disk does
#     not match the sealed packet -- an integrity violation;
#   * ``unreadable`` / ``path_mismatch`` means an indexable file could not be
#     read or extracted -- genuine breakage on a file that SHOULD index;
#   * path-safety codes (absolute / traversal / symlink / outside_repository /
#     null_byte / not_string / unresolvable) mean a malformed or hostile
#     candidate path;
#   * a wrapped ``sqlite3.Error`` / ``OSError`` (clone/backup I/O) is
#     infrastructure breakage.
# None of those are deliberate exclusions, so none are tolerated here.
PREWARM_SKIP_SCHEMA_ID = "aiworkhub.quality_review_prewarm_skip.v1"

PREWARM_TOLERATED_EXCLUSION_CONDITIONS = (
    "source_graph_single_file_excluded_glob",
    "source_graph_single_file_excluded_dir",
    "source_graph_single_file_unsupported_extension",
)

# ``worker_ai_tools_mcp.prewarm_quality_review_source_graph`` renders a wrapped
# prewarm failure with a FIXED structure (worker_ai_tools_mcp.py:973-976):
#
#     "quality_review_candidate_source_graph_prewarm_error:"
#     + type(exc).__name__ + ":" + str(exc)[:240]
#
# So, reading left to right after the constant prefix, the message is exactly
# ``<type_name>:<str(exc)>``.  ``type(exc).__name__`` is a Python identifier and
# can NEVER contain a ``:``; for a ``source_graph.SourceGraphError`` it is exactly
# ``SourceGraphError`` and ``str(exc)`` is that error's own ``<code>:<detail>``
# message whose leading segment is the deliberate, machine-set condition code.
# Therefore the whole message decomposes into positional fields separated by the
# first two colons after the prefix:
#
#     <prefix> : <type_name> : <code> : <detail...>
#        fixed      slot 0      slot 1   worker-influenced, path-bearing
#
# The decision is read from the SLOTS, never by searching the text.  ``<detail>``
# is worker-influenced (it can embed the candidate path, including a literal
# ``SourceGraphError:source_graph_single_file_excluded_glob``), so it must never
# be scanned; the ``[:240]`` truncation only ever clips ``<detail>`` and cannot
# move ``<type_name>`` or ``<code>`` out of their slots.
PREWARM_CANDIDATE_ERROR_PREFIX = (
    "quality_review_candidate_source_graph_prewarm_error:"
)
PREWARM_SOURCE_GRAPH_TYPE_NAME = "SourceGraphError"


def _prewarm_tolerated_exclusion_code(text: str) -> str | None:
    """Return the tolerated exclusion code, read from its field, or ``None``.

    The verdict is anchored to the message's fixed structure, not to any
    substring search.  We require the exact
    ``quality_review_candidate_source_graph_prewarm_error:`` prefix, then read
    the two positional fields that follow it -- ``<type_name>`` (slot 0) and
    ``<code>`` (slot 1) -- because the wrapper always emits
    ``<prefix>:<type(exc).__name__>:<str(exc)>`` and an exception type name
    cannot contain a ``:``.  Tolerance requires BOTH that slot 0 is exactly
    ``SourceGraphError`` (so a wrapped ``OSError`` / ``sqlite3.Error`` /
    ``WorkerToolError`` can never qualify, whatever its message mentions) AND
    that slot 1 *equals* an allowlisted exclusion code.  Everything after the
    second colon is the worker-influenced ``<detail>`` -- it can embed the
    candidate path, including a literal ``SourceGraphError:...excluded_glob`` --
    and is never consulted, so it cannot forge a tolerated verdict.  A
    genuine-failure code (``..._unreadable``, ``..._hash_mismatch``,
    ``..._traversal`` ...) occupies slot 1 and is refused.
    """

    if not text.startswith(PREWARM_CANDIDATE_ERROR_PREFIX):
        # Not a wrapped candidate prewarm error at all (for example a direct
        # ``quality_review_candidate_path_escapes_workspace`` WorkerToolError, or
        # an empty message): fail closed, it is not a deliberate exclusion.
        return None
    # Split the post-prefix body on its first two colons only.  ``<detail>`` (the
    # remainder) is deliberately left un-split and never inspected.
    fields = text[len(PREWARM_CANDIDATE_ERROR_PREFIX):].split(":", 2)
    if len(fields) < 2:
        # No code slot present (for example a type name with no ``str(exc)``):
        # nothing to tolerate.
        return None
    type_name = fields[0].strip()
    code_segment = fields[1].strip()
    if type_name != PREWARM_SOURCE_GRAPH_TYPE_NAME:
        # A ``sqlite3.Error`` / ``OSError`` / ``WorkerToolError`` folded into the
        # same prefix carries its OWN type name in slot 0, not ``SourceGraphError``
        # -- infrastructure/contract breakage on an indexable file, kept loud even
        # if its ``<detail>`` path happens to mention an excluded-glob token.
        return None
    if code_segment in PREWARM_TOLERATED_EXCLUSION_CONDITIONS:
        return code_segment
    return None


def classify_prewarm_error(message: str) -> dict[str, Any]:
    """Decide whether a reviewer prewarm failure is a tolerable exclusion.

    ``tolerated`` is True only for the deliberate-exclusion conditions in
    :data:`PREWARM_TOLERATED_EXCLUSION_CONDITIONS`; for those the launcher skips
    the path, records ``reason``, and still launches the reviewer.  For every
    other failure ``tolerated`` is False and the launch is refused loudly, so a
    real indexing failure on a file that should be indexable is never swallowed.

    Classification is fail-closed and read from the message's fixed positional
    structure (``<prefix>:<type_name>:<code>:<detail>``), never by searching the
    text.  ``<detail>`` is worker-influenced (it can embed the candidate path,
    including a literal ``SourceGraphError:source_graph_single_file_excluded_glob``),
    so a genuine indexing failure whose path merely contains that token -- for
    example a wrapped ``OSError`` at ``eval/SourceGraphError:``
    ``source_graph_single_file_excluded_glob/module.py``, or a
    ``source_graph_single_file_unreadable`` on such a path -- must still refuse.
    :func:`_prewarm_tolerated_exclusion_code` guarantees this by reading the type
    name from slot 0 and the code from slot 1 and requiring BOTH that slot 0 is
    exactly ``SourceGraphError`` AND that slot 1 *equals* an allowlisted exclusion
    code; the path-bearing ``<detail>`` is never consulted.  A ``sqlite3.Error`` /
    ``OSError`` folded into the same
    ``quality_review_candidate_source_graph_prewarm_error`` prefix carries its own
    type name in slot 0, not ``SourceGraphError``, so it can never be tolerated and
    stays loud.
    """

    text = str(message or "")
    condition = _prewarm_tolerated_exclusion_code(text)
    if condition is not None:
        return {
            "schema_id": PREWARM_SKIP_SCHEMA_ID,
            "tolerated": True,
            "condition": condition,
            "reason": (
                "reviewer_source_graph_prewarm_skipped_excluded_path:"
                f"{condition}: Source Graph deliberately does not index this "
                "path, so prewarm skipped it and the candidate Source Graph "
                "overlay was not built; the reviewer proceeds on the sealed "
                "review packet alone -- which already carries the candidate "
                "content -- and still launches. detail=" + text[:200]
            ),
        }
    return {
        "schema_id": PREWARM_SKIP_SCHEMA_ID,
        "tolerated": False,
        "condition": None,
        "reason": (
            "reviewer_source_graph_prewarm_failed_indexable_target: a real "
            "indexing failure on a file that should be indexable is not a "
            "deliberate exclusion and is not swallowed. detail=" + text[:200]
        ),
    }


# ---------------------------------------------------------------------------
# Reviewer prewarm/launch truth: read-only open, bounded wait, reservation
# expiry, preparation-phase stall, and the review_ready launch gate.
#
# Measured wedge (six concurrent reviewers): all six stuck in
# ``reviewer_source_graph_prewarm_started`` with ``pid`` null, ``process_alive``
# False, the preparation heartbeat frozen at launch time, and
# ``reservation_expires_at_epoch`` already past with nothing reclaimed, while
# Source Graph health showed a ``writable_open_recover`` phase failing on
# ``database is locked``.  Three independent defects, each addressed by a pure
# decision function below so the launch path can be truthful instead of hanging.
# ---------------------------------------------------------------------------
REVIEWER_PREWARM_OPEN_SCHEMA_ID = "aiworkhub.quality_review_prewarm_open.v1"
REVIEWER_PREWARM_WAIT_SCHEMA_ID = "aiworkhub.quality_review_prewarm_wait.v1"
REVIEWER_RESERVATION_SCHEMA_ID = "aiworkhub.quality_review_reservation.v1"
REVIEWER_PREP_STALL_SCHEMA_ID = "aiworkhub.quality_review_preparation_stall.v1"
REVIEWER_LAUNCH_TARGET_SCHEMA_ID = "aiworkhub.quality_review_launch_target.v1"

REVIEWER_PREWARM_OPEN_MODE_READONLY = "read_only"

# A reviewer prewarm that cannot acquire its read-only index handle within this
# many seconds fails the launch with a named reason instead of hanging.
REVIEWER_PREWARM_DEADLINE_SECONDS_DEFAULT = 10.0

# A preparation-phase heartbeat older than this is a stall even though no
# process exists yet.
REVIEWER_PREP_HEARTBEAT_STALL_SECONDS_DEFAULT = 20.0

REVIEWER_TARGET_REVIEW_READY = "review_ready"


def reviewer_is_read_only(card: Mapping[str, Any] | None) -> bool:
    """True when a reviewer card declares no writes and forbids repository write.

    A quality reviewer's contract is read-only: its ``allowed_writes`` is empty
    and its ``forbidden`` list names ``repository_write``.  This is used to
    confirm a prewarm open plan is consistent with the card it was handed.
    """

    if not isinstance(card, Mapping):
        return False
    allowed = card.get("allowed_writes")
    forbidden = card.get("forbidden")
    no_writes = not allowed
    forbids_write = False
    if isinstance(forbidden, (list, tuple)):
        forbids_write = any("repository_write" in str(item) for item in forbidden)
    return bool(no_writes and forbids_write)


def reviewer_prewarm_open_plan(
    *, reviewer_card: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """State that the reviewer Source Graph prewarm opens the index READ-ONLY.

    A quality reviewer is read-only by contract, so prewarming its candidate
    overlay never needs -- and must never take -- a WRITABLE open of the Source
    Graph index.  The measured wedge (six reviewers stuck in
    ``writable_open_recover`` on ``database is locked``) came from taking a
    writer lock the reviewer's contract never justified.  This plan makes the
    read-only open explicit so the prewarm cannot contend for the writer lock.
    ``contract_consistent`` is False only if a supplied card contradicts the
    read-only contract; the open mode is read-only regardless.
    """

    consistent = reviewer_card is None or reviewer_is_read_only(reviewer_card)
    return {
        "schema_id": REVIEWER_PREWARM_OPEN_SCHEMA_ID,
        "open_mode": REVIEWER_PREWARM_OPEN_MODE_READONLY,
        "writable": False,
        "requires_writer_lock": False,
        "contract_consistent": bool(consistent),
        "reason": "reviewer_read_only_by_contract",
        "why": (
            "a quality reviewer declares no allowed_writes and forbids "
            "repository_write, so its Source Graph prewarm takes a read-only "
            "index open and never contends for the writable/writer lock"
        ),
    }


def evaluate_prewarm_wait(
    *,
    elapsed_seconds: float,
    acquired: bool,
    deadline_seconds: float = REVIEWER_PREWARM_DEADLINE_SECONDS_DEFAULT,
    phase: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Decide whether a bounded reviewer prewarm succeeds, waits, or fails launch.

    ``acquired`` short-circuits to success.  Otherwise a prewarm that has run
    for at least ``deadline_seconds`` without acquiring its read-only index
    handle FAILS the launch (``launch_failed`` True) with a named reason that
    carries the last observed phase/error, instead of hanging indefinitely.
    Within the deadline it is still waiting and the launch is not yet failed.
    """

    elapsed = max(0.0, float(elapsed_seconds))
    deadline = max(0.0, float(deadline_seconds))
    if acquired:
        return {
            "schema_id": REVIEWER_PREWARM_WAIT_SCHEMA_ID,
            "state": "acquired",
            "ok": True,
            "launch_failed": False,
            "timed_out": False,
            "elapsed_seconds": elapsed,
            "deadline_seconds": deadline,
            "reason": "reviewer_source_graph_prewarm_acquired",
        }
    if elapsed >= deadline:
        detail = str(error or phase or "").strip()[:200]
        reason = (
            "reviewer_source_graph_prewarm_deadline_exceeded:"
            f"elapsed={elapsed:.3f}s>=deadline={deadline:.3f}s"
        )
        if detail:
            reason = f"{reason}:{detail}"
        return {
            "schema_id": REVIEWER_PREWARM_WAIT_SCHEMA_ID,
            "state": "deadline_exceeded",
            "ok": False,
            "launch_failed": True,
            "timed_out": True,
            "elapsed_seconds": elapsed,
            "deadline_seconds": deadline,
            "reason": reason,
        }
    return {
        "schema_id": REVIEWER_PREWARM_WAIT_SCHEMA_ID,
        "state": "waiting",
        "ok": False,
        "launch_failed": False,
        "timed_out": False,
        "elapsed_seconds": elapsed,
        "deadline_seconds": deadline,
        "reason": "reviewer_source_graph_prewarm_in_progress",
    }


def evaluate_reservation(
    *,
    reservation_expires_at_epoch: float,
    now_epoch: float,
    slot_id: str = "",
    attempt_id: str = "",
) -> dict[str, Any]:
    """An expired reservation terminates the attempt and frees its slot.

    Given the reservation's absolute expiry epoch and the current epoch, an
    already-past expiry means the attempt is abandoned: ``terminate_attempt``
    and ``free_slot`` are both True with a named reason -- rather than the
    measured failure where a past ``reservation_expires_at_epoch`` reclaimed
    nothing and the slot stayed wedged.  An unexpired reservation is active.
    """

    expires = float(reservation_expires_at_epoch)
    now = float(now_epoch)
    remaining = expires - now
    expired = remaining <= 0.0
    if expired:
        reason = f"reviewer_reservation_expired:expired_{-remaining:.3f}s_ago"
    else:
        reason = f"reviewer_reservation_active:{remaining:.3f}s_remaining"
    return {
        "schema_id": REVIEWER_RESERVATION_SCHEMA_ID,
        "expired": bool(expired),
        "terminate_attempt": bool(expired),
        "free_slot": bool(expired),
        "slot_id": str(slot_id),
        "attempt_id": str(attempt_id),
        "seconds_remaining": remaining,
        "reason": reason,
    }


def detect_preparation_stall(
    *,
    phase: str,
    heartbeat_epoch: float,
    now_epoch: float,
    pid: int | None = None,
    process_alive: bool = False,
    stall_after_seconds: float = REVIEWER_PREP_HEARTBEAT_STALL_SECONDS_DEFAULT,
) -> dict[str, Any]:
    """A frozen preparation heartbeat is a stall even with no process yet.

    During preparation (e.g. ``reviewer_source_graph_prewarm_started``) there is
    no spawned process -- ``pid`` is None and ``process_alive`` is False -- yet
    the attempt still emits a preparation heartbeat.  Stall detection that only
    watches a RUNNING process is blind to this phase, which is exactly how the
    measured frozen preparation heartbeat went unreclaimed.  This watches the
    heartbeat age directly, independent of any process, so a frozen preparation
    heartbeat is a detectable stall.
    """

    now = float(now_epoch)
    heartbeat = float(heartbeat_epoch)
    age = now - heartbeat
    bound = max(0.0, float(stall_after_seconds))
    no_process = pid is None or not process_alive
    stalled = age >= bound
    if stalled:
        reason = (
            "reviewer_preparation_heartbeat_frozen:"
            f"phase={str(phase or '')}:age={age:.3f}s>=bound={bound:.3f}s:"
            f"no_process={no_process}"
        )
    else:
        reason = "reviewer_preparation_heartbeat_fresh"
    return {
        "schema_id": REVIEWER_PREP_STALL_SCHEMA_ID,
        "stalled": bool(stalled),
        "in_preparation": bool(no_process),
        "watches_process": False,
        "heartbeat_age_seconds": age,
        "stall_after_seconds": bound,
        "phase": str(phase or ""),
        "pid": pid,
        "process_alive": bool(process_alive),
        "reason": reason,
    }


def _target_review_substatus(target_card: Mapping[str, Any] | None) -> str:
    if not isinstance(target_card, Mapping):
        return ""
    terminal_review = target_card.get("terminal_review")
    if isinstance(terminal_review, Mapping):
        return str(terminal_review.get("substatus") or "")
    return ""


def assess_reviewer_launch_target(
    *,
    target_card: Mapping[str, Any] | None = None,
    target_substatus: str = "",
) -> dict[str, Any]:
    """Refuse a quality reviewer launch whose target is not ``review_ready``.

    The reviewer resolves its target; if that target is not ``review_ready`` the
    reviewer refuses and dies.  Returning ``ok`` from the launch and letting the
    reviewer silently disappear leaves the caller believing a review is running.
    This gate makes the launch FAIL at launch (``can_launch`` False,
    ``fails_at_launch`` True), naming the observed target state, so the caller
    learns immediately instead of waiting on a review that never starts.
    """

    substatus = str(target_substatus or "")
    if not substatus and target_card is not None:
        substatus = _target_review_substatus(target_card)
    normalized = substatus.strip().lower()
    ready = normalized == REVIEWER_TARGET_REVIEW_READY
    if ready:
        reason = "reviewer_target_review_ready"
    else:
        reason = f"reviewer_target_not_review_ready:{normalized or '<none>'}"
    return {
        "schema_id": REVIEWER_LAUNCH_TARGET_SCHEMA_ID,
        "can_launch": bool(ready),
        "fails_at_launch": not ready,
        "target_substatus": normalized,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Candidate reachability gate (NF-2026-00304).
#
# All three quality lenses once passed a candidate whose new code had no caller
# anywhere: the card existed to add code that no production path ever reached,
# and every lens stayed green because reachability is not a judgement a lens
# makes -- a test can call a function directly and pass while nothing in
# production ever does.  This is the gate's most expensive blind spot, because
# everything else it checks is downstream of the code actually running.
#
# So reachability is decided HERE, mechanically, from edges Source Graph
# ALREADY carries -- its ``calls`` and reference edges -- rather than from a new
# static analyser.  For every symbol a candidate ADDED or MODIFIED, we establish
# whether a path exists to it from an entry point the repository already
# recognises.
#
# The result is REPORTED, never silently enforced.  Some additions are
# legitimately unreferenced -- a new public API, a CLI entry point, a test
# helper -- so an unreachable addition is NAMED as a non-blocking finding a
# manager disposes of (``disposition="observation"``, ``blocking=False``),
# rather than either failing the card outright (a gate that cries wolf gets
# disabled) or, as before, saying nothing at all.
# ---------------------------------------------------------------------------
REACHABILITY_SCHEMA_ID = "aiworkhub.quality_review_reachability.v1"

# An unreachable addition is surfaced for a manager to dispose of; it never
# blocks the card on its own.  ``observation`` matches the quality-evidence
# finding-disposition vocabulary (reported, non-blocking).
REACHABILITY_DISPOSITION = "observation"
REACHABILITY_GATE_ACTION = "surface_for_manager_disposition"

CHANGE_ADDED = "added"
CHANGE_MODIFIED = "modified"


def _reachability_symbol_identity(record: Any) -> tuple[str, str, str]:
    """Normalise one added/modified symbol record to ``(symbol, file, change)``."""

    if isinstance(record, Mapping):
        symbol = str(
            record.get("qualname")
            or record.get("symbol")
            or record.get("name")
            or ""
        )
        file = str(record.get("file") or record.get("file_path") or "")
        change = str(
            record.get("change") or record.get("change_kind") or CHANGE_ADDED
        ).lower()
    else:
        symbol, file, change = str(record or ""), "", CHANGE_ADDED
    if change not in (CHANGE_ADDED, CHANGE_MODIFIED):
        change = CHANGE_ADDED
    return symbol, file, change


def _reachability_edge_endpoints(edge: Any) -> tuple[str, str]:
    """Normalise a Source Graph call/reference edge to ``(src, dst)``.

    An edge means ``src`` calls or references ``dst`` -- so reachability flows
    from ``src`` to ``dst``.  Both the raw Source Graph edge shape
    (``src_qualname``/``dst_qualname``) and a caller/callee mapping are accepted.
    """

    if isinstance(edge, Mapping):
        src = str(
            edge.get("src")
            or edge.get("caller")
            or edge.get("src_qualname")
            or edge.get("from")
            or ""
        )
        dst = str(
            edge.get("dst")
            or edge.get("callee")
            or edge.get("dst_qualname")
            or edge.get("to")
            or edge.get("name")
            or ""
        )
    elif isinstance(edge, (tuple, list)) and len(edge) >= 2:
        src, dst = str(edge[0] or ""), str(edge[1] or "")
    else:
        src, dst = "", ""
    return src, dst


def _reachable_from_entry_points(
    entry_points: Iterable[str], edges: Iterable[Any]
) -> set[str]:
    """Set of symbols reachable by following call/reference edges from entries."""

    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        src, dst = _reachability_edge_endpoints(edge)
        if src and dst:
            adjacency.setdefault(src, set()).add(dst)
    reached = {str(ep) for ep in entry_points if str(ep)}
    frontier = list(reached)
    while frontier:
        node = frontier.pop()
        for nxt in adjacency.get(node, ()):  # node (src) -> nxt (dst)
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)
    return reached


def analyze_candidate_reachability(
    *,
    changed_symbols: Iterable[Any],
    call_edges: Iterable[Any] = (),
    reference_edges: Iterable[Any] = (),
    entry_points: Iterable[str] = (),
) -> dict[str, Any]:
    """Report every candidate addition no recognised entry point can reach.

    For each symbol the candidate ADDED or MODIFIED, a path is sought from an
    entry point the repository already recognises, following the ``calls`` and
    reference edges Source Graph already carries (no new analyser).  A symbol
    with such a path stays quiet; a symbol without one is NAMED as a finding.

    The gate never fails the card on an unreachable addition: some additions are
    legitimately unreferenced -- a new public API, a CLI entry point, a test
    helper -- so each unreachable addition is emitted as a non-blocking finding
    (``disposition="observation"``, ``blocking=False``,
    ``gate_action="surface_for_manager_disposition"``) that names the exact
    symbol, its file and whether it was added or modified, for a manager to
    dispose of.  ``all_reachable`` is True and ``findings`` empty when every
    candidate symbol is reached.
    """

    entry = [str(ep) for ep in (entry_points or ()) if str(ep)]
    edges = list(call_edges or ()) + list(reference_edges or ())
    reached = _reachable_from_entry_points(entry, edges)

    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in changed_symbols or ():
        symbol, file, change = _reachability_symbol_identity(record)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        if symbol in reached:
            continue
        findings.append(
            {
                "schema_id": REACHABILITY_SCHEMA_ID,
                "symbol": symbol,
                "file": file,
                "change": change,
                "disposition": REACHABILITY_DISPOSITION,
                "blocking": False,
                "reason": (
                    f"unreachable_candidate_{change}:{symbol} has no path from "
                    f"any of the {len(entry)} entry point(s) the repository "
                    "already recognises; a manager must dispose of this finding "
                    "-- it may be a legitimately unreferenced addition such as a "
                    "new public API, a CLI entry point or a test helper, or it "
                    "may be code the card added that nothing ever reaches"
                ),
            }
        )
    findings.sort(key=lambda item: (item["file"], item["symbol"]))
    return {
        "schema_id": REACHABILITY_SCHEMA_ID,
        "entry_point_count": len(entry),
        "changed_symbol_count": len(seen),
        "all_reachable": not findings,
        "blocking": False,
        "gate_action": REACHABILITY_GATE_ACTION,
        "unreachable_additions": [item["symbol"] for item in findings],
        "findings": findings,
    }


__all__ = [
    "DELIVERY_SCHEMA_ID",
    "PREWARM_SKIP_SCHEMA_ID",
    "PREWARM_TOLERATED_EXCLUSION_CONDITIONS",
    "classify_prewarm_error",
    "AVAILABILITY_SCHEMA_ID",
    "SCOPE_SCHEMA_ID",
    "INDEPENDENCE_SCHEMA_ID",
    "DEFAULT_SUBMIT_TOOL",
    "REVIEWER_MIN_INSPECTION_QUERIES",
    "REVIEWER_MAX_INSPECTION_QUERIES",
    "AVAILABILITY_AVAILABLE",
    "AVAILABILITY_PROVIDER_REFUSED",
    "AVAILABILITY_QUOTA_UNOBSERVED",
    "REASON_AVAILABLE",
    "REASON_SAME_PROVIDER",
    "REASON_PROVIDER_REFUSED",
    "REASON_QUOTA_UNOBSERVED",
    "REASON_BLIND_NO_FILE_READ",
    "REFUSAL_NO_INDEPENDENT_SIGHTED",
    "RUNG_CROSS_PROVIDER",
    "RUNG_CROSS_MODEL_SAME_PROVIDER",
    "RUNG_SAME_MODEL_FRESH_CONTEXT",
    "INDEPENDENCE_LADDER",
    "deliver_packet_content",
    "capability_set_has_file_read",
    "build_reviewer_prompt_with_content",
    "assemble_reviewer_prompt",
    "extract_inline_packet",
    "reviewer_may_query_source_graph_again",
    "reviewer_submit_forced",
    "choose_reviewer_source_graph_scope",
    "assess_reviewer_availability",
    "resolve_independence_rung",
    "independence_acceptance_line",
    "REVIEWER_PREWARM_OPEN_SCHEMA_ID",
    "REVIEWER_PREWARM_WAIT_SCHEMA_ID",
    "REVIEWER_RESERVATION_SCHEMA_ID",
    "REVIEWER_PREP_STALL_SCHEMA_ID",
    "REVIEWER_LAUNCH_TARGET_SCHEMA_ID",
    "REVIEWER_PREWARM_OPEN_MODE_READONLY",
    "REVIEWER_PREWARM_DEADLINE_SECONDS_DEFAULT",
    "REVIEWER_PREP_HEARTBEAT_STALL_SECONDS_DEFAULT",
    "REVIEWER_TARGET_REVIEW_READY",
    "reviewer_is_read_only",
    "reviewer_prewarm_open_plan",
    "evaluate_prewarm_wait",
    "evaluate_reservation",
    "detect_preparation_stall",
    "assess_reviewer_launch_target",
    "REACHABILITY_SCHEMA_ID",
    "REACHABILITY_DISPOSITION",
    "REACHABILITY_GATE_ACTION",
    "CHANGE_ADDED",
    "CHANGE_MODIFIED",
    "analyze_candidate_reachability",
]
