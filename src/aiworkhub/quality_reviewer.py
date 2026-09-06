from __future__ import annotations

import ast
import builtins
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import sysconfig
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from .evidence_levels import EvidenceLevel
from .scoped_audit import ScopedAuditPacket, packet_fingerprint


PACKET_SCHEMA_ID = "aiworkhub.quality_review_packet.v1"
RECEIPT_SCHEMA_ID = "aiworkhub.quality_reviewer_receipt.v1"
MAX_PACKET_PATHS = 200
MAX_PACKET_CHECKS = 200
MAX_PACKET_COMMANDS = 100
MAX_TEXT_CHARS = 2_000
REVIEW_PACKET_FILE_ROOT_ENV = "AIWORKHUB_QUALITY_REVIEW_PACKET_ROOT"

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FINDING_CONFIDENCE = frozenset({"low", "medium", "high"})

# One canonical quality-review finding shape, shared by the reviewer prompt,
# the callable MCP tool schema, runtime normalization, the durable receipt and
# the coordinator finalization count.  Consumers (quality_evidence) require at
# least id/severity/disposition/summary/evidence plus the derived actionable
# and evidence_level fields; the input vocabulary is the strict subset of that
# shape a reviewer may supply.  Anything outside it is an undocumented alias
# and is rejected by name.
FINDING_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
FINDING_DISPOSITIONS = frozenset({"defect", "observation", "process_limit"})
OVERBUILD_FINDING_CATEGORIES = frozenset(
    {
        "duplicate_existing_symbol",
        "handrolled_standard_or_platform_capability",
        "unnecessary_abstraction",
        "excess_scope",
    }
)
FINDING_CATEGORIES = frozenset({"general"}) | OVERBUILD_FINDING_CATEGORIES


QualityReviewFinding = TypedDict(
    "QualityReviewFinding",
    {
        "severity": str,
        "summary": str,
        "evidence": str | Mapping[str, object],
        "id": NotRequired[str],
        "disposition": NotRequired[str],
        "confidence": NotRequired[str],
        "symbol": NotRequired[str],
        "claim": NotRequired[str],
        "reproduction": NotRequired[str],
        "required_validation": NotRequired[str],
        "evidence_level": NotRequired[str],
        "path": NotRequired[str],
        "line_start": NotRequired[int],
        "line_end": NotRequired[int],
        "check_id": NotRequired[str],
        "category": NotRequired[str],
        "replacement": NotRequired[str],
        "removable_surface": NotRequired[str],
    },
)
QualityReviewFinding.__doc__ = (
    "One canonical quality-review finding submitted by a reviewer. "
    "severity, summary and evidence are required; the remaining fields are optional."
)


QUALITY_REVIEW_FINDING_INPUT_REQUIRED_KEYS = frozenset(
    QualityReviewFinding.__required_keys__
)
QUALITY_REVIEW_FINDING_INPUT_KEYS = frozenset(QualityReviewFinding.__annotations__)
QUALITY_REVIEW_FINDING_INGRESS_KEYS = QUALITY_REVIEW_FINDING_INPUT_KEYS | frozenset(
    {"actionable", "evidence_reference"}
)
QUALITY_REVIEW_FINDING_REQUIRED_KEYS = frozenset(
    {
        "id",
        "severity",
        "disposition",
        "actionable",
        "category",
        "summary",
        "evidence",
        "confidence",
        "evidence_level",
        "symbol",
        "claim",
        "reproduction",
        "required_validation",
    }
)
QUALITY_REVIEW_FINDING_KEYS = QUALITY_REVIEW_FINDING_REQUIRED_KEYS | frozenset(
    {"evidence_reference", "replacement", "removable_surface"}
)
QUALITY_REVIEW_FINDING_SCHEMA_DOC = (
    "One finding object uses the canonical shape with required keys "
    "severity (critical|high|medium|low), summary and evidence; optional id "
    "(stable identifier, derived when omitted), disposition (defect default; "
    "observation and process_limit must be low severity), confidence "
    "(low|medium|high), symbol, claim, reproduction, required_validation and "
    "category. Category defaults to general; over-build categories are "
    "duplicate_existing_symbol, handrolled_standard_or_platform_capability, "
    "unnecessary_abstraction and excess_scope. "
    "Defects must cite an exact packet-permitted path and line "
    "(path/line_start/line_end) or a mechanical check_id. Evidence may be a "
    "string containing that exact reference or an object with exactly "
    "path/line_start/line_end (or check_id); the supervisor canonicalizes the "
    "object before validation. An actionable over-build finding must name "
    "concrete changed source lines from the candidate diff and a category-specific "
    "remedy: duplicate_existing_symbol and "
    "handrolled_standard_or_platform_capability require replacement (the "
    "pre-existing symbol, standard library or platform capability to use), while "
    "unnecessary_abstraction and excess_scope require removable_surface (the "
    "exact abstraction/scope surface to delete). Source Graph or diff evidence "
    "must prove the changed code and, for in-repo replacements, that the "
    "replacement is not newly introduced by the candidate diff. Over-build "
    "reports must be disposition=defect/actionable; observations, process "
    "limits, check-only evidence and test-target evidence are rejected. Raw line "
    "count, token count and aesthetic preference are not failures. Correctness, "
    "security, portability, accessibility and explicit task requirements remain "
    "higher-priority review obligations and must not be downgraded for "
    "minimality. The tool derives actionable, evidence_level and "
    "evidence_reference; do not invent keys outside this shape. An empty "
    "findings list is valid."
)
QUALITY_REVIEW_SUBMIT_TOOL_DESCRIPTION = (
    "Submit findings for the exact coordinator-bound review packet. "
    + QUALITY_REVIEW_FINDING_SCHEMA_DOC
)


class ReviewerEvidenceError(ValueError):
    """Raised when reviewer evidence is not bound to its exact execution."""


def _identity(value: object, field: str) -> str:
    normalized = str(value or "")
    if not _IDENTITY_RE.fullmatch(normalized):
        raise ReviewerEvidenceError(f"invalid_{field}")
    return normalized


def _bounded_strings(values: Iterable[object], *, limit: int) -> list[str]:
    rows = [str(value)[:MAX_TEXT_CHARS] for value in values]
    if len(rows) > limit:
        raise ReviewerEvidenceError("review_packet_overflow")
    return rows


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_review_packet(
    *,
    request_id: str,
    task_id: str,
    claim_epoch: int,
    worker_provider: str,
    changed_path_hashes: Mapping[str, str],
    objective: str = "",
    acceptance: Iterable[object] = (),
    required_outputs: Iterable[object] = (),
    validation: Iterable[object] = (),
    terminal_validation: Iterable[object] = (),
    mechanical_checks: Iterable[Mapping[str, Any]] = (),
    combined_tree_checks: Iterable[Mapping[str, Any]] = (),
    source_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    scoped_audits: Mapping[str, Mapping[str, Any] | ScopedAuditPacket] | None = None,
) -> dict[str, Any]:
    """Build the only evidence packet an independent reviewer may receive.

    The packet is intentionally anti-anchored: it contains the objective
    contract and deterministic evidence, never the worker's explanation,
    self-verdict, final response, chain of thought, or reviewer suggestions.
    """

    if not isinstance(claim_epoch, int) or claim_epoch < 1:
        raise ReviewerEvidenceError("invalid_claim_epoch")
    if not isinstance(changed_path_hashes, Mapping) or not changed_path_hashes:
        raise ReviewerEvidenceError("changed_path_hashes_missing")
    if len(changed_path_hashes) > MAX_PACKET_PATHS:
        raise ReviewerEvidenceError("review_packet_overflow")
    path_rows: list[dict[str, Any]] = []
    for path, digest in sorted(changed_path_hashes.items()):
        normalized_path = str(path)
        normalized_digest = None if digest is None else str(digest)
        if (
            not normalized_path
            or normalized_path.startswith("/")
            or ".." in normalized_path.split("/")
        ):
            raise ReviewerEvidenceError("invalid_changed_path")
        if normalized_digest is not None and not _SHA256_RE.fullmatch(
            normalized_digest
        ):
            raise ReviewerEvidenceError("invalid_changed_path_hash")
        path_rows.append({"path": normalized_path, "sha256": normalized_digest})

    def checks(values: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
        rows = list(values)
        if len(rows) > MAX_PACKET_CHECKS:
            raise ReviewerEvidenceError("review_packet_overflow")
        result: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ReviewerEvidenceError("invalid_mechanical_check")
            check_id = str(row.get("check_id") or "")
            status = str(row.get("status") or "")
            kind = str(row.get("kind") or "")
            if not check_id or not status or not kind:
                raise ReviewerEvidenceError("invalid_mechanical_check")
            result.append(
                {
                    "check_id": check_id[:200],
                    "kind": kind[:100],
                    "status": status[:100],
                    "provenance": str(row.get("provenance") or "")[:MAX_TEXT_CHARS],
                }
            )
        return result

    def terminal_validations(values: Iterable[object]) -> list[dict[str, Any]]:
        rows = list(values)
        if len(rows) > MAX_PACKET_COMMANDS:
            raise ReviewerEvidenceError("review_packet_overflow")
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ReviewerEvidenceError("invalid_terminal_validation")
            executed = row.get("executed_argv") or row.get("argv") or []
            if not isinstance(executed, (list, tuple)):
                raise ReviewerEvidenceError("invalid_terminal_validation")
            returncode = row.get("returncode")
            if not isinstance(returncode, int) or isinstance(returncode, bool):
                raise ReviewerEvidenceError("invalid_terminal_validation")
            result.append(
                {
                    "declared_command": str(
                        row.get("declared_command") or row.get("command") or ""
                    )[:MAX_TEXT_CHARS],
                    "executed_argv": [
                        str(item)[:MAX_TEXT_CHARS]
                        for item in list(executed)[:MAX_PACKET_COMMANDS]
                    ],
                    "returncode": returncode,
                    "stdout_truncated": bool(row.get("stdout_truncated")),
                    "stderr_truncated": bool(row.get("stderr_truncated")),
                }
            )
        return result

    candidate: dict[str, Any] = {"changed_paths": path_rows}
    if source_evidence is not None:
        candidate["source_evidence"] = _source_evidence_rows(source_evidence, path_rows)
    if scoped_audits is not None:
        candidate["scoped_audits"] = _scoped_audit_rows(
            scoped_audits,
            task_id=task_id,
            changed_paths={row["path"] for row in path_rows},
        )
    body = {
        "schema_id": PACKET_SCHEMA_ID,
        "target": {
            "request_id": _identity(request_id, "request_id"),
            "task_id": _identity(task_id, "task_id"),
            "claim_epoch": claim_epoch,
            "worker_provider": _identity(worker_provider, "worker_provider"),
        },
        "contract": {
            "objective": str(objective)[:MAX_TEXT_CHARS],
            "acceptance": _bounded_strings(acceptance, limit=MAX_PACKET_COMMANDS),
            "required_outputs": _bounded_strings(required_outputs, limit=MAX_PACKET_COMMANDS),
            "validation": _bounded_strings(validation, limit=MAX_PACKET_COMMANDS),
        },
        "terminal_validation": terminal_validations(terminal_validation),
        "candidate": candidate,
        "mechanical_checks": checks(mechanical_checks),
        "combined_tree_checks": checks(combined_tree_checks),
    }
    return {**body, "packet_sha256": _canonical_digest(body)}


def _has_symlink_component(base: Path, relative: str) -> bool:
    """Return True when any path component under ``base`` is a symlink.

    ``Path.resolve`` follows symlinks, so a containment/is_file check run
    after resolving can never reject a symlink whose target stays inside the
    repository.  Each unresolved component is checked with ``lstat`` (via
    ``Path.is_symlink``) before any resolution.
    """

    current = base
    for part in relative.split("/"):
        if not part:
            continue
        current = current / part
        if current.is_symlink():
            return True
    return False


def verify_review_packet_candidate(
    packet_path: Path,
    repo: Path,
    *,
    max_packet_bytes: int = 256 * 1024,
) -> dict[str, Any]:
    """Verify an on-disk review packet and its exact candidate file bytes.

    This is the bounded, fail-closed verifier the reviewer Source Graph prewarm
    consumes before any index build.  It re-checks the packet schema, canonical
    digest, target identity, changed-path shape and containment, and re-hashes
    every candidate file byte so a stale or tampered packet can never authorize
    an index.  Raises ``ReviewerEvidenceError`` with a stable ``quality_review_*``
    code on any mismatch.
    """

    try:
        if packet_path.is_symlink() or not packet_path.is_file():
            raise ReviewerEvidenceError("quality_review_packet_invalid")
        if packet_path.stat().st_size > max_packet_bytes:
            raise ReviewerEvidenceError("quality_review_packet_too_large")
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except ReviewerEvidenceError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewerEvidenceError("quality_review_packet_unreadable") from exc
    if not isinstance(packet, dict) or packet.get("schema_id") != PACKET_SCHEMA_ID:
        raise ReviewerEvidenceError("quality_review_packet_schema_mismatch")
    packet_sha256 = str(packet.get("packet_sha256") or "")
    body = {key: value for key, value in packet.items() if key != "packet_sha256"}
    if not _SHA256_RE.fullmatch(packet_sha256) or _canonical_digest(body) != packet_sha256:
        raise ReviewerEvidenceError("quality_review_packet_digest_invalid")
    target = packet.get("target") or {}
    target_request_id = str(target.get("request_id") or "")
    target_task_id = str(target.get("task_id") or "")
    if not target_request_id or not target_task_id:
        raise ReviewerEvidenceError("quality_review_packet_target_invalid")
    candidate_section = packet.get("candidate") or {}
    rows = candidate_section.get("changed_paths") or []
    if not isinstance(rows, list) or not rows:
        raise ReviewerEvidenceError("quality_review_candidate_paths_missing")
    source_evidence = candidate_section.get("source_evidence") or []
    source_by_path: dict[str, Mapping[str, Any]] = {}
    if isinstance(source_evidence, list):
        for evidence_row in source_evidence:
            if isinstance(evidence_row, Mapping):
                source_by_path[str(evidence_row.get("path") or "")] = evidence_row
    resolved_repo = repo.resolve()
    verified_paths: list[dict[str, str | None]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReviewerEvidenceError("quality_review_candidate_path_invalid")
        relative = str(row.get("path") or "")
        expected_raw = row.get("sha256")
        expected = str(expected_raw or "") if expected_raw is not None else None
        if not relative or relative.startswith("/") or ".." in relative.split("/"):
            raise ReviewerEvidenceError("quality_review_candidate_path_invalid")
        candidate = resolved_repo / relative
        if _has_symlink_component(resolved_repo, relative):
            raise ReviewerEvidenceError("quality_review_candidate_path_symlink")
        candidate = candidate.resolve()
        if not candidate.is_relative_to(resolved_repo):
            raise ReviewerEvidenceError("quality_review_candidate_path_invalid")
        if expected is None:
            evidence_row = source_by_path.get(relative)
            if (
                not isinstance(evidence_row, Mapping)
                or evidence_row.get("candidate_sha256") is not None
                or evidence_row.get("excerpt") != ""
                or evidence_row.get("excerpt_bytes") != 0
                or evidence_row.get("source_bytes") != 0
                or evidence_row.get("truncated") is not False
                or evidence_row.get("segments") != []
                or evidence_row.get("omission_reason") != "candidate_deleted_or_non_file"
                or candidate.is_file()
            ):
                raise ReviewerEvidenceError("quality_review_candidate_omission_invalid")
            verified_paths.append({"path": relative, "sha256": None})
            continue
        if not _SHA256_RE.fullmatch(expected):
            raise ReviewerEvidenceError("quality_review_candidate_hash_invalid")
        if not candidate.is_file():
            raise ReviewerEvidenceError("quality_review_candidate_path_missing")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
            raise ReviewerEvidenceError("quality_review_candidate_hash_mismatch")
        verified_paths.append({"path": relative, "sha256": expected})
    return {
        "packet_sha256": packet_sha256,
        "target_request_id": target_request_id,
        "target_task_id": target_task_id,
        "changed_paths": verified_paths,
    }


def verify_reviewer_receipt(
    receipt: Mapping[str, Any],
    *,
    packet: Mapping[str, Any],
    expected_reviewer_request_id: str,
    expected_reviewer_task_id: str,
    observed_provider: str,
    observed_terminal_state: str,
    audit_verified: bool,
) -> dict[str, Any]:
    """Verify one process-observed reviewer receipt against an exact packet.

    ``observed_provider`` and ``observed_terminal_state`` must come from the
    launch/process registry, while ``audit_verified`` must come from the
    authenticated worker-tool ledger. They are deliberately separate from
    the model-submitted receipt so a provider string in JSON proves nothing.
    """

    if not isinstance(receipt, Mapping):
        raise ReviewerEvidenceError("reviewer_receipt_not_object")
    if receipt.get("schema_id") != RECEIPT_SCHEMA_ID:
        raise ReviewerEvidenceError("reviewer_receipt_schema_mismatch")
    packet_body = {k: v for k, v in packet.items() if k != "packet_sha256"}
    packet_digest = str(packet.get("packet_sha256") or "")
    if not _SHA256_RE.fullmatch(packet_digest) or _canonical_digest(packet_body) != packet_digest:
        raise ReviewerEvidenceError("review_packet_digest_invalid")
    target = packet.get("target")
    receipt_target = receipt.get("target")
    reviewer = receipt.get("reviewer")
    report = receipt.get("report")
    if (
        not isinstance(target, Mapping)
        or not isinstance(receipt_target, Mapping)
        or not isinstance(reviewer, Mapping)
        or not isinstance(report, Mapping)
    ):
        raise ReviewerEvidenceError("reviewer_receipt_shape_invalid")
    for field in ("request_id", "task_id", "claim_epoch"):
        if receipt_target.get(field) != target.get(field):
            raise ReviewerEvidenceError(f"reviewer_target_{field}_mismatch")
    if receipt.get("packet_sha256") != packet_digest:
        raise ReviewerEvidenceError("reviewer_packet_digest_mismatch")
    if reviewer.get("request_id") != expected_reviewer_request_id:
        raise ReviewerEvidenceError("reviewer_request_identity_mismatch")
    if reviewer.get("task_id") != expected_reviewer_task_id:
        raise ReviewerEvidenceError("reviewer_task_identity_mismatch")
    if reviewer.get("provider") != observed_provider:
        raise ReviewerEvidenceError("reviewer_provider_spoofed")
    if observed_terminal_state != "review_ready":
        raise ReviewerEvidenceError("reviewer_terminal_state_invalid")
    if not audit_verified:
        raise ReviewerEvidenceError("reviewer_audit_unverified")
    if report.get("read_only") is not True or report.get("can_mutate_repo") is not False:
        raise ReviewerEvidenceError("reviewer_report_not_read_only")
    return {
        "schema_id": RECEIPT_SCHEMA_ID,
        "packet_sha256": packet_digest,
        "target": dict(receipt_target),
        "reviewer": {
            "request_id": expected_reviewer_request_id,
            "task_id": expected_reviewer_task_id,
            "provider": observed_provider,
        },
        "report": dict(report),
        "authority": {
            "process_identity_verified": True,
            "audit_verified": True,
            "terminal_state": observed_terminal_state,
        },
    }


# Work the system has already done deterministically, which reviewers were
# nonetheless redoing by hand. Measured on one real review (request fb150636):
# 38 model turns and 17 tool calls, of which only 4 were reading the candidate.
# The other 13 were scaffolding -- ``sha256sum`` over paths whose digests were
# already in the packet, and four consecutive attempts to re-run a pytest
# command whose exact argv, returncode and output the packet already carried,
# three of them failing only because the reviewer was hunting for an interpreter.
#
# Naming what is settled moves that work off the model without weakening the
# review: the reviewer still reads the candidate and still judges it, but it
# stops re-deriving facts the supervisor established and recorded.
_ALREADY_ESTABLISHED_MECHANICALLY = (
    "The packet is deterministic evidence the supervisor already produced. Do "
    "NOT re-derive any of it:\n"
    "- candidate file digests are recorded per changed path; do not run "
    "sha256sum or any hashing command to confirm them.\n"
    "- every declared validation was already executed; the packet carries its "
    "exact argv, returncode and bounded stdout/stderr under terminal_validation "
    "and mechanical_checks. Do not re-run pytest, lint or any validation "
    "command, and do not go looking for an interpreter.\n"
    "- combined-tree checks are recorded the same way.\n"
    "Read the candidate and judge it. Spend your turns on the code, not on "
    "reproducing receipts you were handed.\n"
)


def build_review_prompt(
    packet: Mapping[str, Any],
    *,
    lens: str,
    submit_tool_name: str = "aiworkhub_worker_quality_review_submit",
    packet_file: str | None = None,
    packet_root: Path | str | None = None,
    max_inline_bytes: int = 96 * 1024,
) -> str:
    """Render a bounded independent-review prompt from packet facts only.

    When *packet_file* is provided the serialised packet is written to that
    path and the prompt references it via a worker-scoped file read instead of
    embedding the full JSON inline.  This avoids E2BIG on native CLI adapters
    where large quality-review payloads would otherwise be passed through argv.
    *max_inline_bytes* guards the inline fallback when *packet_file* is None.
    *packet_root* is coordinator-owned write authority, independent of the
    destination path. It overrides the worker-environment fallback without
    changing process-global state, so concurrent managers keep separate roots.
    """

    if lens not in {"correctness", "security", "code_quality"}:
        raise ReviewerEvidenceError("invalid_reviewer_lens")
    packet_body = {k: v for k, v in packet.items() if k != "packet_sha256"}
    packet_digest = str(packet.get("packet_sha256") or "")
    if not _SHA256_RE.fullmatch(packet_digest) or _canonical_digest(packet_body) != packet_digest:
        raise ReviewerEvidenceError("review_packet_digest_invalid")
    scoped_audits = (packet.get("candidate") or {}).get("scoped_audits")
    active_scope = None
    if scoped_audits is not None:
        if not isinstance(scoped_audits, Mapping) or lens not in scoped_audits:
            raise ReviewerEvidenceError("review_scope_lens_missing")
        active_scope = scoped_audits[lens]
        if not isinstance(active_scope, Mapping):
            raise ReviewerEvidenceError("review_scope_invalid")
    encoded = json.dumps(packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    scope_instruction = (
        "Use only the active graph-scoped audit entry for this lens as the "
        "primary behavior boundary; treat its known_unknowns as explicit limits.\n"
        if active_scope is not None
        else ""
    )
    active_scope_evidence = (
        "ACTIVE_SCOPED_AUDIT: "
        + json.dumps(
            active_scope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
        if active_scope is not None
        else ""
    )
    use_file_transport = (
        packet_file is not None and len(encoded.encode("utf-8")) > max_inline_bytes
    )
    if use_file_transport and packet_file is not None:
        _write_review_packet_file(Path(packet_file), encoded, packet_root=packet_root)
    packet_evidence = (
        f"QUALITY_REVIEW_PACKET_FILE: {packet_file}\n"
        f"PACKET_SHA256: {packet_digest}\n"
        "The packet file has been written with the canonical serialized packet "
        "whose packet_sha256 is shown above. Call "
        "aiworkhub_worker_quality_review_packet_read with no arguments before "
        "reviewing; the returned packet is the authoritative evidence for this "
        "review. Do not supply a path or identity to that tool.\n"
        if use_file_transport
        else f"QUALITY_REVIEW_PACKET: {encoded}\n"
    )
    # Reconciliation of a real contradiction: runtime_adapters grants the
    # reviewer ``aiworkhub_worker_quality_review_submit`` in its allowedTools,
    # yet this prompt bans invoking any submission tool.  The prompt ban is
    # authoritative -- the reviewer emits exactly one JSON report as its final
    # text and the SUPERVISOR (quality_review_ingest.supervisor_ingest) derives
    # all task/request/claim/target/reviewer/packet identity and submits it.
    # The allowedTools grant is retained only as a tolerated fallback: if a
    # reviewer self-submits despite the ban, that payload lands in the audit
    # ledger and supervisor_ingest's explicit-receipt path reconciles it (dedup
    # or conflict), never letting reviewer-asserted identity through.  The tool
    # name is deliberately kept out of the returned prompt so the reviewer is
    # not nudged toward it.
    return (
        "You are an independent, strictly read-only quality reviewer.\n"
        f"Review lens: {lens}.\n"
        "Inspect the exact candidate workspace and the deterministic packet below. "
        "You are intentionally not given the worker's rationale, self-verdict, or final answer. "
        "Do not write, edit, format, or delete repository files.\n"
        f"{scope_instruction}"
        f"{_ALREADY_ESTABLISHED_MECHANICALLY}"
        "Report only concrete items supported by file/line or check evidence. "
        f"{QUALITY_REVIEW_FINDING_SCHEMA_DOC}\n"
        f"{active_scope_evidence}"
        "Finish with exactly one JSON object and no surrounding prose, using "
        f'{{"lens":"{lens}","findings":[...]}}. The supervisor derives all '
        "task, request, claim, target, reviewer, and packet identity and durably "
        "submits the report. Do not invoke lifecycle or submission tools.\n"
        f"{packet_evidence}"
    )


def _derive_finding_id(
    *,
    lens: str,
    index: int,
    severity: str,
    disposition: str,
    summary: str,
    evidence: str,
) -> str:
    """Derive a stable finding identifier when the reviewer omits ``id``."""
    digest = hashlib.sha256(
        f"{severity}\x00{disposition}\x00{summary}\x00{evidence}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{lens}-{index}-{digest}"


def normalize_packet_findings(
    packet: Mapping[str, Any],
    *,
    lens: str,
    findings: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bind reviewer findings to exact paths/checks in one scoped packet.

    A model cannot promote its own evidence level.  The strongest level this
    boundary assigns is ``static_evidence`` for an exact packet-permitted
    path/line or mechanical check.  Reproduction/tested levels require later
    manager-observed execution and therefore cannot originate here.
    """

    candidate = packet.get("candidate")
    if not isinstance(candidate, Mapping):
        raise ReviewerEvidenceError("review_scope_missing")
    scoped = candidate.get("scoped_audits")
    if not isinstance(scoped, Mapping) or lens not in scoped:
        raise ReviewerEvidenceError("review_scope_lens_missing")
    wrapper = scoped[lens]
    if not isinstance(wrapper, Mapping) or not isinstance(wrapper.get("packet"), Mapping):
        raise ReviewerEvidenceError("review_scope_invalid")
    scope = wrapper["packet"]
    permitted_paths: set[str] = {
        str(row.get("path") or "")
        for row in scope.get("changed_paths") or []
        if isinstance(row, Mapping)
    }
    for section in ("impact_evidence", "test_evidence", "contract_evidence"):
        permitted_paths.update(
            str(row.get("path") or "")
            for row in scope.get(section) or []
            if isinstance(row, Mapping)
        )
    permitted_paths.discard("")
    permitted_checks = {
        str(row.get("check_id") or "")
        for section in ("mechanical_checks", "combined_tree_checks")
        for row in packet.get(section) or []
        if isinstance(row, Mapping)
    }
    permitted_checks.discard("")
    permitted_symbols = {
        str(row.get("qualified_name") or "")
        for row in scope.get("target_symbols") or []
        if isinstance(row, Mapping)
    }
    permitted_symbols.discard("")
    changed_paths = {
        str(row.get("path") or "")
        for row in (candidate.get("changed_paths") or [])
        if isinstance(row, Mapping)
    }
    changed_paths.discard("")
    changed_source_lines = _candidate_changed_source_lines(candidate)
    authorized_repo_replacements = _authorized_overbuild_replacements(
        scope,
        changed_paths=changed_paths,
        changed_source_lines=changed_source_lines,
    )

    rows = list(findings)
    if len(rows) > 100:
        raise ReviewerEvidenceError("review_findings_overflow")
    normalized: list[dict[str, Any]] = []
    for index, finding in enumerate(rows):
        if not isinstance(finding, Mapping):
            raise ReviewerEvidenceError(f"review_finding_{index}_not_object")
        unknown = set(finding) - QUALITY_REVIEW_FINDING_INGRESS_KEYS
        if unknown:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_unknown_key:{','.join(sorted(unknown))}"
            )
        finding, supplied_reference = _canonicalize_ingress_finding(
            finding, index=index
        )
        finding = _canonicalize_structured_evidence(finding, index=index)
        severity_raw = finding.get("severity")
        if severity_raw is None:
            raise ReviewerEvidenceError(f"review_finding_{index}_severity_missing")
        severity = str(severity_raw).strip()
        if severity not in FINDING_SEVERITIES:
            raise ReviewerEvidenceError(f"review_finding_{index}_severity_invalid")
        disposition = str(finding.get("disposition") or "defect").strip()
        if disposition not in FINDING_DISPOSITIONS:
            raise ReviewerEvidenceError(f"review_finding_{index}_disposition_invalid")
        if disposition != "defect" and severity != "low":
            raise ReviewerEvidenceError(
                f"review_finding_{index}_nondefect_severity_must_be_low"
            )
        category = str(finding.get("category") or "general").strip()
        if category not in FINDING_CATEGORIES:
            raise ReviewerEvidenceError(f"review_finding_{index}_category_invalid")
        if "summary" not in finding:
            raise ReviewerEvidenceError(f"review_finding_{index}_summary_missing")
        if "evidence" not in finding:
            raise ReviewerEvidenceError(f"review_finding_{index}_evidence_missing")
        summary = str(finding["summary"] or "").strip()
        evidence = str(finding["evidence"] or "").strip()
        if not summary or not evidence:
            raise ReviewerEvidenceError(f"review_finding_{index}_text_missing")

        evidence_reference = _validate_evidence_reference(
            supplied_reference,
            index=index,
            permitted_paths=permitted_paths,
            permitted_checks=permitted_checks,
            allow_test_target=True,
        )
        explicit_path = str(finding.get("path") or "")
        explicit_check = str(finding.get("check_id") or "")
        explicit_line_start = finding.get("line_start")
        explicit_line_end = finding.get("line_end")
        if supplied_reference is not None and (
            explicit_path
            or explicit_check
            or explicit_line_start not in (None, "")
            or explicit_line_end not in (None, "")
        ):
            raise ReviewerEvidenceError(
                f"review_finding_{index}_evidence_reference_conflict"
            )
        if evidence_reference is None and explicit_path:
            if explicit_path not in permitted_paths:
                raise ReviewerEvidenceError(
                    f"review_finding_{index}_path_out_of_scope"
                )
            line_start = finding.get("line_start")
            line_end = finding.get("line_end", line_start)
            if (
                not isinstance(line_start, int)
                or isinstance(line_start, bool)
                or line_start < 1
                or not isinstance(line_end, int)
                or isinstance(line_end, bool)
                or line_end < line_start
            ):
                raise ReviewerEvidenceError(
                    f"review_finding_{index}_line_invalid"
                )
            evidence_reference = {
                "kind": "source",
                "path": explicit_path,
                "line_start": line_start,
                "line_end": line_end,
            }
        elif evidence_reference is None and explicit_check:
            if explicit_check not in permitted_checks:
                raise ReviewerEvidenceError(
                    f"review_finding_{index}_check_out_of_scope"
                )
            evidence_reference = {"kind": "check", "check_id": explicit_check}
        elif evidence_reference is None:
            for path in sorted(permitted_paths, key=lambda value: (-len(value), value)):
                match = _source_citation_re(path).search(evidence)
                if match:
                    start = int(match.group(1))
                    end = int(match.group(2) or start)
                    evidence_reference = {
                        "kind": "source",
                        "path": path,
                        "line_start": start,
                        "line_end": end,
                    }
                    break
                if f"{path}::" in evidence:
                    evidence_reference = {
                        "kind": "test_target",
                        "path": path,
                    }
                    break
            if evidence_reference is None:
                for check_id in sorted(permitted_checks):
                    if check_id and _token_re(check_id).search(evidence):
                        evidence_reference = {
                            "kind": "check",
                            "check_id": check_id,
                        }
                        break

        if evidence_reference is not None:
            _validate_evidence_text_agrees_with_reference(
                evidence,
                evidence_reference=evidence_reference,
                index=index,
                permitted_paths=permitted_paths,
                permitted_checks=permitted_checks,
            )

        actionable = disposition == "defect"
        if actionable and evidence_reference is None:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_exact_evidence_required"
            )
        if actionable and evidence_reference is not None and evidence_reference.get("kind") == "test_target":
            raise ReviewerEvidenceError(
                f"review_finding_{index}_exact_evidence_required"
            )
        replacement = str(finding.get("replacement") or "").strip()[:MAX_TEXT_CHARS]
        removable_surface = str(
            finding.get("removable_surface") or ""
        ).strip()[:MAX_TEXT_CHARS]
        if category in OVERBUILD_FINDING_CATEGORIES:
            _validate_overbuild_finding(
                index=index,
                category=category,
                disposition=disposition,
                evidence_reference=evidence_reference,
                replacement=replacement,
                removable_surface=removable_surface,
                authorized_repo_replacements=authorized_repo_replacements,
                changed_paths=changed_paths,
                changed_source_lines=changed_source_lines,
            )
        derived_level = (
            EvidenceLevel.STATIC_EVIDENCE
            if evidence_reference is not None
            else EvidenceLevel.OBSERVATION
        )
        requested_raw = finding.get("evidence_level")
        if requested_raw in (None, ""):
            evidence_level = derived_level
        else:
            try:
                requested = (
                    requested_raw
                    if isinstance(requested_raw, EvidenceLevel)
                    else EvidenceLevel[str(requested_raw).strip().upper()]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ReviewerEvidenceError(
                    f"review_finding_{index}_evidence_level_invalid"
                ) from exc
            evidence_level = min(requested, derived_level)
        if severity in {"critical", "high"} and evidence_level < EvidenceLevel.STATIC_EVIDENCE:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_blocker_evidence_insufficient"
            )

        confidence = str(finding.get("confidence") or "")
        if not confidence:
            confidence = "medium" if evidence_reference is not None else "low"
        if confidence not in _FINDING_CONFIDENCE:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_confidence_invalid"
            )
        symbol = str(finding.get("symbol") or "")[:MAX_TEXT_CHARS]
        if symbol and symbol not in permitted_symbols:
            symbol = ""
        finding_id = str(finding.get("id") or "").strip()
        if not finding_id:
            finding_id = _derive_finding_id(
                lens=lens,
                index=index,
                severity=severity,
                disposition=disposition,
                summary=summary,
                evidence=evidence,
            )
        elif len(finding_id) > 200:
            finding_id = finding_id[:200]
        normalized_finding: dict[str, Any] = {
            "id": finding_id,
            "severity": severity,
            "disposition": disposition,
            "actionable": actionable,
            "category": category,
            "summary": summary[:MAX_TEXT_CHARS],
            "evidence": evidence[:MAX_TEXT_CHARS],
            "confidence": confidence,
            "evidence_level": evidence_level.name.lower(),
            "symbol": symbol,
            "claim": str(finding.get("claim") or summary)[:MAX_TEXT_CHARS],
            "reproduction": str(finding.get("reproduction") or "")[:MAX_TEXT_CHARS],
            "required_validation": str(
                finding.get("required_validation")
                or (
                    "manager must independently validate this finding"
                    if actionable
                    else ""
                )
            )[:MAX_TEXT_CHARS],
        }
        if replacement:
            normalized_finding["replacement"] = replacement
        if removable_surface:
            normalized_finding["removable_surface"] = removable_surface
        if evidence_reference is not None:
            normalized_finding["evidence_reference"] = evidence_reference
        normalized.append(normalized_finding)
    return normalized


def _canonicalize_ingress_finding(
    finding: Mapping[str, Any], *, index: int
) -> tuple[dict[str, Any], object]:
    """Strip derived authority and retain a canonical reference for validation."""

    canonical = dict(finding)
    canonical.pop("actionable", None)
    supplied_reference = canonical.pop("evidence_reference", None)
    if supplied_reference is not None and not isinstance(supplied_reference, Mapping):
        raise ReviewerEvidenceError(
            f"review_finding_{index}_evidence_reference_invalid"
        )
    return canonical, supplied_reference


def _validate_evidence_reference(
    raw: object,
    *,
    index: int,
    permitted_paths: set[str],
    permitted_checks: set[str],
    allow_test_target: bool,
) -> dict[str, Any] | None:
    """Revalidate one supervisor-shaped reference against packet authority."""

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ReviewerEvidenceError(f"review_finding_{index}_evidence_reference_invalid")
    kind = raw.get("kind")
    if kind == "source":
        allowed = {"kind", "path", "line_start", "line_end"}
        if set(raw) != allowed:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_evidence_reference_invalid"
            )
        path = raw.get("path")
        line_start = raw.get("line_start")
        line_end = raw.get("line_end")
        if not isinstance(path, str) or not path:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_evidence_reference_invalid"
            )
        if path not in permitted_paths:
            raise ReviewerEvidenceError(f"review_finding_{index}_path_out_of_scope")
        if (
            not isinstance(line_start, int)
            or isinstance(line_start, bool)
            or line_start < 1
            or not isinstance(line_end, int)
            or isinstance(line_end, bool)
            or line_end < line_start
        ):
            raise ReviewerEvidenceError(f"review_finding_{index}_line_invalid")
        return {
            "kind": "source",
            "path": path,
            "line_start": line_start,
            "line_end": line_end,
        }
    if kind == "check":
        if set(raw) != {"kind", "check_id"}:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_evidence_reference_invalid"
            )
        check_id = raw.get("check_id")
        if not isinstance(check_id, str) or not check_id:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_evidence_reference_invalid"
            )
        if check_id not in permitted_checks:
            raise ReviewerEvidenceError(f"review_finding_{index}_check_out_of_scope")
        return {"kind": "check", "check_id": check_id}
    if kind == "test_target":
        if not allow_test_target:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_evidence_reference_invalid"
            )
        if set(raw) != {"kind", "path"}:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_evidence_reference_invalid"
            )
        path = raw.get("path")
        if not isinstance(path, str) or not path:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_evidence_reference_invalid"
            )
        if path not in permitted_paths:
            raise ReviewerEvidenceError(f"review_finding_{index}_path_out_of_scope")
        return {"kind": "test_target", "path": path}
    raise ReviewerEvidenceError(f"review_finding_{index}_evidence_reference_invalid")


def _validate_evidence_text_agrees_with_reference(
    evidence: str,
    *,
    evidence_reference: Mapping[str, Any],
    index: int,
    permitted_paths: set[str],
    permitted_checks: set[str],
) -> None:
    """Reject canonical reports whose evidence text names a different identity."""

    claimed = _evidence_text_reference(
        evidence, permitted_paths=permitted_paths, permitted_checks=permitted_checks
    )
    if claimed is not None and claimed != evidence_reference:
        raise ReviewerEvidenceError(
            f"review_finding_{index}_evidence_reference_conflict"
        )


def _evidence_text_reference(
    evidence: str,
    *,
    permitted_paths: set[str],
    permitted_checks: set[str],
) -> dict[str, Any] | None:
    for path in sorted(permitted_paths, key=lambda value: (-len(value), value)):
        match = _source_citation_re(path).search(evidence)
        if match:
            start = int(match.group(1))
            end = int(match.group(2) or start)
            return {
                "kind": "source",
                "path": path,
                "line_start": start,
                "line_end": end,
            }
        if f"{path}::" in evidence:
            return {
                "kind": "test_target",
                "path": path,
            }
    generic_source = re.search(
        r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+"
        r"\.[A-Za-z0-9_.-]+:(\d+)(?:-(\d+))?(?![A-Za-z0-9_-])",
        evidence,
    )
    if generic_source:
        return {"kind": "source", "path": "", "line_start": 0, "line_end": 0}
    for check_id in sorted(permitted_checks):
        if check_id and _token_re(check_id).search(evidence):
            return {"kind": "check", "check_id": check_id}
    return None


def _token_re(value: str) -> re.Pattern[str]:
    """Match an exact evidence token without binding superstrings."""

    return re.compile(
        rf"(?<![A-Za-z0-9_.:-]){re.escape(value)}(?![A-Za-z0-9_.:-])"
    )


def _source_citation_re(path: str) -> re.Pattern[str]:
    """Match ``path:line`` or ``path:start-end`` with a hard end boundary."""

    return re.compile(
        rf"(?<![A-Za-z0-9_.-]){re.escape(path)}:(\d+)(?:-(\d+))?"
        r"(?![A-Za-z0-9_-])"
    )


def _candidate_changed_source_lines(
    candidate: Mapping[str, Any],
) -> dict[str, list[tuple[int, int]]]:
    """Return changed candidate diff line spans from packet source evidence."""

    spans: dict[str, list[tuple[int, int]]] = {}
    for row in candidate.get("source_evidence") or []:
        if not isinstance(row, Mapping):
            continue
        path = str(row.get("path") or "")
        if not path:
            continue
        for segment in row.get("segments") or []:
            if not isinstance(segment, Mapping):
                continue
            start = segment.get("changed_start_line")
            end = segment.get("changed_end_line")
            if (
                isinstance(start, int)
                and not isinstance(start, bool)
                and isinstance(end, int)
                and not isinstance(end, bool)
                and start >= 1
                and end >= start
            ):
                spans.setdefault(path, []).append((start, end))
    return spans


def _line_proves_preexisting_source(
    row: Mapping[str, Any],
    *,
    changed_paths: set[str],
    changed_source_lines: Mapping[str, list[tuple[int, int]]],
) -> bool:
    """Return True only for replacement rows bound outside candidate-added code."""

    path = str(row.get("path") or row.get("file_path") or "").strip()
    if not path:
        return False
    if path not in changed_paths:
        return True
    line = row.get("line") or row.get("line_start") or row.get("start_line")
    if not isinstance(line, int) or isinstance(line, bool):
        return False
    spans = changed_source_lines.get(path) or []
    if not spans:
        return False
    return not any(span_start <= line <= span_end for span_start, span_end in spans)


def _authorized_overbuild_replacements(
    scope: Mapping[str, Any],
    *,
    changed_paths: set[str],
    changed_source_lines: Mapping[str, list[tuple[int, int]]],
) -> set[str]:
    """Collect replacement names proven outside the candidate diff."""

    replacements: set[str] = set()
    for row in scope.get("target_symbols") or []:
        if not isinstance(row, Mapping) or not _line_proves_preexisting_source(
            row,
            changed_paths=changed_paths,
            changed_source_lines=changed_source_lines,
        ):
            continue
        for key in ("qualified_name", "symbol", "name"):
            value = str(row.get(key) or "").strip()
            if value:
                replacements.add(value)
    for section in ("impact_evidence", "contract_evidence"):
        for row in scope.get(section) or []:
            if not isinstance(row, Mapping) or not _line_proves_preexisting_source(
                row,
                changed_paths=changed_paths,
                changed_source_lines=changed_source_lines,
            ):
                continue
            for key in ("qualified_name", "symbol", "reference", "replacement"):
                value = str(row.get(key) or "").strip()
                if value:
                    replacements.add(value)
    return replacements


def _changed_line_reference(
    value: str,
    *,
    changed_paths: set[str],
    changed_source_lines: Mapping[str, list[tuple[int, int]]],
) -> dict[str, Any] | None:
    for path in sorted(changed_paths, key=lambda item: (-len(item), item)):
        match = re.fullmatch(rf"{re.escape(path)}:(\d+)(?:-(\d+))?", value)
        if not match:
            continue
        start = int(match.group(1))
        end = int(match.group(2) or start)
        spans = changed_source_lines.get(path) or []
        if any(span_start <= start and end <= span_end for span_start, span_end in spans):
            return {
                "kind": "source",
                "path": path,
                "line_start": start,
                "line_end": end,
            }
    return None


def _write_review_packet_file(
    packet_path: Path, encoded: str, *, packet_root: Path | str | None = None,
) -> None:
    """Write a canonical packet via an exclusive private staging file."""

    payload = encoded.encode("utf-8")
    root_raw = (
        packet_root
        if packet_root is not None
        else os.environ.get(REVIEW_PACKET_FILE_ROOT_ENV)
    )
    if not root_raw:
        raise ReviewerEvidenceError("review_packet_file_root_missing")
    runtime_root = Path(root_raw)
    try:
        if runtime_root.is_symlink() or not runtime_root.is_dir():
            raise ReviewerEvidenceError("review_packet_file_root_invalid")
        runtime_root = runtime_root.resolve()
    except OSError as exc:
        raise ReviewerEvidenceError("review_packet_file_root_invalid") from exc
    if not packet_path.is_absolute():
        packet_path = runtime_root / packet_path
    else:
        try:
            if not packet_path.resolve(strict=False).is_relative_to(runtime_root):
                raise ReviewerEvidenceError("review_packet_file_outside_root")
        except OSError as exc:
            raise ReviewerEvidenceError("review_packet_file_outside_root") from exc
    parent = packet_path.parent
    tmp_path: Path | None = None
    fd = -1
    try:
        if parent.is_symlink() or not parent.is_dir():
            raise ReviewerEvidenceError("review_packet_file_unwritable")
        parent = parent.resolve()
        if not parent.is_relative_to(runtime_root):
            raise ReviewerEvidenceError("review_packet_file_outside_root")
        relative_parent = parent.relative_to(runtime_root).as_posix()
        if relative_parent != "." and _has_symlink_component(runtime_root, relative_parent):
            raise ReviewerEvidenceError("review_packet_file_symlink")
        packet_path = parent / packet_path.name
        parent_before = parent.stat()
        if packet_path.is_symlink():
            raise ReviewerEvidenceError("review_packet_file_symlink")
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{packet_path.name}.",
            suffix=".tmp",
            dir=parent,
        )
        tmp_path = Path(tmp_name)
        if tmp_path.is_symlink():
            raise ReviewerEvidenceError("review_packet_file_symlink")
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        parent_after = parent.stat()
        if (
            parent_before.st_dev != parent_after.st_dev
            or parent_before.st_ino != parent_after.st_ino
            or not parent.resolve().is_relative_to(runtime_root)
        ):
            raise ReviewerEvidenceError("review_packet_file_identity_changed")
        if packet_path.is_symlink():
            raise ReviewerEvidenceError("review_packet_file_symlink")
        os.replace(tmp_path, packet_path)
        tmp_path = None
        parent_after_replace = parent.stat()
        if (
            parent_before.st_dev != parent_after_replace.st_dev
            or parent_before.st_ino != parent_after_replace.st_ino
            or not parent.resolve().is_relative_to(runtime_root)
        ):
            raise ReviewerEvidenceError("review_packet_file_identity_changed")
        if packet_path.is_symlink() or packet_path.read_bytes() != payload:
            raise ReviewerEvidenceError("review_packet_file_digest_invalid")
    except ReviewerEvidenceError:
        raise
    except OSError as exc:
        raise ReviewerEvidenceError("review_packet_file_unwritable") from exc
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _stdlib_search_roots() -> tuple[Path, ...]:
    roots = {
        sysconfig.get_path(name)
        for name in ("stdlib", "platstdlib")
        if sysconfig.get_path(name)
    }
    dynload = Path(sysconfig.get_path("stdlib")) / "lib-dynload"
    if dynload.is_dir():
        roots.add(str(dynload))
    return tuple(Path(root).resolve() for root in roots)


def _dotted_identifier_parts(name: str) -> list[str] | None:
    if "\x00" in name or "/" in name or "\\" in name:
        return None
    parts = name.split(".")
    if not parts or any(not part.isidentifier() for part in parts):
        return None
    return parts


def _contained_stdlib_path(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root):
        return None
    return resolved


def _platform_python_module_path(module: str) -> Path | None:
    parts = _dotted_identifier_parts(f"{module}.__sentinel__")
    if parts is None:
        return None
    parts = parts[:-1]
    root = parts[0]
    if root not in sys.stdlib_module_names and root not in sys.builtin_module_names:
        return None
    for search_root in _stdlib_search_roots():
        current = search_root
        module_file: Path | None = None
        for index, part in enumerate(parts):
            file_candidate = current / f"{part}.py"
            package_candidate = current / part / "__init__.py"
            if file_candidate.is_file():
                module_file = _contained_stdlib_path(file_candidate, search_root)
                if module_file is None:
                    return None
                if index == len(parts) - 1:
                    return module_file
                break
            if package_candidate.is_file():
                module_file = _contained_stdlib_path(package_candidate, search_root)
                if module_file is None:
                    return None
                current = current / part
                if index == len(parts) - 1:
                    return module_file
                continue
            break
    return None


def _stdlib_python_tree(module: str) -> ast.Module | None:
    path = _platform_python_module_path(module)
    if path is None:
        return None
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _interpreter_install_roots() -> tuple[Path, ...]:
    roots: set[Path] = set()
    for name in ("purelib", "platlib"):
        value = sysconfig.get_path(name)
        if not value:
            continue
        try:
            root = Path(value).resolve()
        except OSError:
            continue
        if root.is_dir():
            roots.add(root)
    return tuple(sorted(roots))


def _typeshed_stdlib_roots() -> tuple[Path, ...]:
    roots: set[Path] = set()
    for install_root in _interpreter_install_roots():
        candidate = install_root / "mypy" / "typeshed" / "stdlib"
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved.is_relative_to(install_root):
            roots.add(resolved)
    return tuple(sorted(roots))


def _contained_typeshed_stub_path(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root):
        return None
    return resolved


def _typeshed_stub_path(module: str) -> Path | None:
    parts = _dotted_identifier_parts(f"{module}.__sentinel__")
    if parts is None:
        return None
    module_parts = parts[:-1]
    for root in _typeshed_stdlib_roots():
        file_candidate = root.joinpath(*module_parts).with_suffix(".pyi")
        if file_candidate.is_file():
            stub = _contained_typeshed_stub_path(file_candidate, root)
            if stub is not None:
                return stub
        package_candidate = root.joinpath(*module_parts, "__init__.pyi")
        if package_candidate.is_file():
            stub = _contained_typeshed_stub_path(package_candidate, root)
            if stub is not None:
                return stub
    return None


def _typeshed_tree(module: str) -> ast.Module | None:
    path = _typeshed_stub_path(module)
    if path is None:
        return None
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _module_scope_nodes(nodes: list[ast.stmt]) -> Iterable[ast.stmt]:
    for node in nodes:
        yield node
        if isinstance(node, ast.If | ast.Try):
            yield from _module_scope_nodes(list(node.body))
            yield from _module_scope_nodes(list(node.orelse))
            if isinstance(node, ast.Try):
                for handler in node.handlers:
                    yield from _module_scope_nodes(list(handler.body))


def _module_tree_defines(
    tree: ast.Module | None, symbol: str, *, depth: int = 0
) -> bool:
    if tree is None or depth > 4:
        return False

    for node in _module_scope_nodes(tree.body):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name == symbol:
                return True
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == symbol
                for target in targets
            ):
                return True
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*" and node.module:
                    if _module_tree_defines(
                        _typeshed_tree(node.module),
                        symbol,
                        depth=depth + 1,
                    ):
                        return True
                    continue
                if (alias.asname or alias.name).split(".", 1)[0] == symbol:
                    return True
    return False


def _module_tree_class_defines(
    tree: ast.Module | None,
    class_name: str,
    member: str,
    *,
    depth: int = 0,
) -> bool:
    if tree is None or depth > 4:
        return False

    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                if child.name == member:
                    return True
            if isinstance(child, ast.Assign | ast.AnnAssign):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                if any(
                    isinstance(target, ast.Name) and target.id == member
                    for target in targets
                ):
                    return True
            if isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if (alias.asname or alias.name).split(".", 1)[0] == member:
                        return True
    return False


def _spec_is_platform_module(spec: importlib.machinery.ModuleSpec) -> bool:
    origin = spec.origin
    if origin in {"built-in", "frozen"}:
        return True
    if not origin:
        return False
    try:
        origin_path = Path(origin).resolve()
    except OSError:
        return False
    return any(
        origin_path == root or origin_path.is_relative_to(root)
        for root in _stdlib_search_roots()
    )


def _platform_module_spec(module: str) -> importlib.machinery.ModuleSpec | None:
    parts = _dotted_identifier_parts(f"{module}.__sentinel__")
    if parts is None:
        return None
    root = parts[0]
    if root not in sys.stdlib_module_names and root not in sys.builtin_module_names:
        return None
    spec = importlib.machinery.BuiltinImporter.find_spec(module)
    for search_root in _stdlib_search_roots():
        if spec is not None:
            break
        spec = importlib.machinery.PathFinder.find_spec(module, [str(search_root)])
    if spec is None or not _spec_is_platform_module(spec):
        return None
    return spec


def _spec_can_be_loaded_without_python_top_level_code(
    spec: importlib.machinery.ModuleSpec,
) -> bool:
    if spec.origin in {"built-in", "frozen"}:
        return True
    origin = spec.origin or ""
    return any(
        origin.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES
    )


def _loaded_platform_module(module: str) -> Any | None:
    spec = _platform_module_spec(module)
    if spec is None or spec.loader is None:
        return None
    if not _spec_can_be_loaded_without_python_top_level_code(spec):
        return None
    loaded = sys.modules.get(module)
    loaded_spec = getattr(loaded, "__spec__", None)
    if loaded is not None and isinstance(
        loaded_spec, importlib.machinery.ModuleSpec
    ) and _spec_is_platform_module(loaded_spec):
        return loaded
    try:
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
    except Exception:
        return None
    return loaded


def _platform_module_runtime_has_attr(module: str, *attrs: str) -> bool:
    if any(attr.startswith("__") and attr.endswith("__") for attr in attrs):
        return False
    current = _loaded_platform_module(module)
    if current is None:
        return False
    for attr in attrs:
        if not hasattr(current, attr):
            return False
        current = getattr(current, attr)
    return True


def _platform_module_has_attr(module: str, symbol: str) -> bool:
    return (
        _module_tree_defines(_stdlib_python_tree(module), symbol)
        or _module_tree_defines(_typeshed_tree(module), symbol)
        or _platform_module_runtime_has_attr(module, symbol)
    )


def _platform_module_class_has_attr(module: str, class_name: str, symbol: str) -> bool:
    return (
        _module_tree_class_defines(_stdlib_python_tree(module), class_name, symbol)
        or _module_tree_class_defines(_typeshed_tree(module), class_name, symbol)
        or _platform_module_runtime_has_attr(module, class_name, symbol)
    )


def _resolved_import_from_module(module: str, node: ast.ImportFrom) -> str | None:
    if not node.level:
        return node.module or None
    spec = _platform_module_spec(module)
    package = (
        module
        if spec is not None and spec.submodule_search_locations is not None
        else module.rpartition(".")[0]
    )
    if not package:
        return None
    relative_name = "." * node.level + (node.module or "")
    try:
        return importlib.util.resolve_name(relative_name, package)
    except (ImportError, ValueError):
        return None


def _stdlib_python_module_defines(
    module: str,
    symbol: str,
    *,
    depth: int = 0,
    seen: frozenset[str] = frozenset(),
) -> bool:
    """Resolve a stdlib definition through bounded, non-executing re-exports."""

    if depth > 4 or module in seen:
        return False
    tree = _stdlib_python_tree(module)
    if tree is None:
        return False
    if _module_tree_defines(tree, symbol):
        return True

    next_seen = seen | {module}
    for node in _module_scope_nodes(tree.body):
        if not isinstance(node, ast.ImportFrom) or not any(
            alias.name == "*" for alias in node.names
        ):
            continue
        target = _resolved_import_from_module(module, node)
        if target and _stdlib_python_module_defines(
            target,
            symbol,
            depth=depth + 1,
            seen=next_seen,
        ):
            return True
    return False


def _stdlib_alias_target(module: str, symbol: str) -> str | None:
    tree = _stdlib_python_tree(module)
    if tree is None:
        return None
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                imports[bound] = alias.name
                if bound == symbol:
                    return alias.name
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound = alias.asname or alias.name
                imports[bound] = f"{node.module}.{alias.name}"
                if bound == symbol:
                    return f"{node.module}.{alias.name}"
                if alias.name == "*":
                    star_target = _resolved_import_from_module(module, node)
                    if star_target and _stdlib_python_module_defines(
                        star_target, symbol
                    ):
                        return f"{star_target}.{symbol}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value = node.value
            if not isinstance(value, ast.Name) or value.id not in imports:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol:
                    return imports[value.id]
    return None


def _platform_module_exists(module: str) -> bool:
    return (
        _platform_module_spec(module) is not None
        or _platform_python_module_path(module) is not None
        or _typeshed_stub_path(module) is not None
    )


def _builtin_class_has_attr(class_name: str, symbol: str) -> bool:
    if symbol.startswith("__") and symbol.endswith("__"):
        return False
    owner = getattr(builtins, class_name, None)
    return isinstance(owner, type) and hasattr(owner, symbol)


def _is_platform_capability_parts(parts: list[str], *, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if len(parts) == 1:
        return parts[0] in dir(builtins) or _module_tree_defines(
            _typeshed_tree("builtins"), parts[0]
        )
    if len(parts) < 2:
        return False
    if parts[0] == "builtins" and len(parts) == 2:
        return _is_platform_capability_parts([parts[1]], depth=depth + 1)
    if parts[0] == "builtins" and len(parts) == 3:
        return _builtin_class_has_attr(parts[1], parts[2]) or _module_tree_class_defines(
            _typeshed_tree("builtins"), parts[1], parts[2]
        )
    if len(parts) == 2 and _is_platform_capability_parts([parts[0]], depth=depth + 1):
        return _builtin_class_has_attr(parts[0], parts[1]) or _module_tree_class_defines(
            _typeshed_tree("builtins"), parts[0], parts[1]
        )
    for module_end in range(len(parts) - 1, 0, -1):
        module = ".".join(parts[:module_end])
        if not _platform_module_exists(module):
            continue
        symbol = parts[module_end]
        if module_end == len(parts) - 1:
            return _stdlib_python_module_defines(
                module, symbol
            ) or _platform_module_has_attr(module, symbol)
        child_module = ".".join(parts[: module_end + 1])
        if _platform_module_exists(child_module):
            if _is_platform_capability_parts(parts[: module_end + 1], depth=depth + 1):
                return True
            return _is_platform_capability_parts(
                [child_module, *parts[module_end + 1 :]],
                depth=depth + 1,
            )
        alias_target = _stdlib_alias_target(module, symbol)
        if alias_target is not None:
            return _is_platform_capability_parts(
                [*alias_target.split("."), *parts[module_end + 1 :]],
                depth=depth + 1,
            )
        if len(parts) == module_end + 2 and _platform_module_class_has_attr(
            module, symbol, parts[module_end + 1]
        ):
            return True
        return False
    return False


def _is_importable_platform_capability(name: str) -> bool:
    """Check replacements against stdlib/platform authority without sys.path lookup."""

    parts = _dotted_identifier_parts(name)
    if parts is None:
        return False
    return _is_platform_capability_parts(parts)


_REPLACEMENT_CATEGORIES = frozenset(
    {"duplicate_existing_symbol", "handrolled_standard_or_platform_capability"}
)
_REMOVABLE_SURFACE_CATEGORIES = frozenset({"unnecessary_abstraction", "excess_scope"})


def _validate_overbuild_finding(
    *,
    index: int,
    category: str,
    disposition: str,
    evidence_reference: Mapping[str, Any] | None,
    replacement: str,
    removable_surface: str,
    authorized_repo_replacements: set[str],
    changed_paths: set[str],
    changed_source_lines: Mapping[str, list[tuple[int, int]]],
) -> None:
    if disposition != "defect":
        raise ReviewerEvidenceError(
            f"review_finding_{index}_overbuild_must_be_defect"
        )
    if evidence_reference is None:
        raise ReviewerEvidenceError(
            f"review_finding_{index}_overbuild_exact_evidence_required"
        )
    if evidence_reference.get("kind") != "source":
        raise ReviewerEvidenceError(
            f"review_finding_{index}_overbuild_source_evidence_required"
        )
    path = str(evidence_reference.get("path") or "")
    if path not in changed_paths:
        raise ReviewerEvidenceError(
            f"review_finding_{index}_overbuild_changed_source_required"
        )
    start = evidence_reference.get("line_start")
    end = evidence_reference.get("line_end")
    spans = changed_source_lines.get(path) or []
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not any(
            span_start <= start and end <= span_end
            for span_start, span_end in spans
        )
    ):
        raise ReviewerEvidenceError(
            f"review_finding_{index}_overbuild_changed_source_required"
        )
    if category in _REPLACEMENT_CATEGORIES:
        if not replacement or removable_surface:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_overbuild_replacement_required"
            )
        if category == "duplicate_existing_symbol":
            if replacement not in authorized_repo_replacements:
                raise ReviewerEvidenceError(
                    f"review_finding_{index}_overbuild_replacement_unbound"
                )
            return
        if not _is_importable_platform_capability(replacement):
            raise ReviewerEvidenceError(
                f"review_finding_{index}_overbuild_replacement_unbound"
            )
        return
    if category in _REMOVABLE_SURFACE_CATEGORIES:
        if not removable_surface or replacement:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_overbuild_removable_surface_required"
            )
        if _changed_line_reference(
            removable_surface,
            changed_paths=changed_paths,
            changed_source_lines=changed_source_lines,
        ) is None:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_overbuild_removable_surface_unbound"
            )


def _canonicalize_structured_evidence(
    finding: Mapping[str, Any], *, index: int
) -> dict[str, Any]:
    """Convert one exact structured evidence reference to canonical input.
    Reviewer providers commonly render a file/line citation as an ``evidence``
    object even when the prompt requests sibling fields.  This conversion is
    deliberately narrow: it accepts only the canonical source/check keys,
    rejects conflicts with sibling fields, and leaves packet-scope validation
    to ``normalize_packet_findings``.
    """

    canonical = dict(finding)
    raw = canonical.get("evidence")
    if not isinstance(raw, Mapping):
        return canonical
    source_keys = {"path", "line_start", "line_end"}
    check_keys = {"check_id"}
    raw_keys = set(raw)
    if raw_keys == source_keys:
        path = raw.get("path")
        check_id = None
    elif raw_keys == check_keys:
        path = None
        check_id = raw.get("check_id")
    else:
        raise ReviewerEvidenceError(
            f"review_finding_{index}_structured_evidence_invalid"
        )
    if bool(path) == bool(check_id):
        raise ReviewerEvidenceError(
            f"review_finding_{index}_structured_evidence_invalid"
        )
    sibling_source = any(canonical.get(key) not in (None, "") for key in source_keys)
    sibling_check = canonical.get("check_id") not in (None, "")
    if path is None and sibling_source:
        raise ReviewerEvidenceError(
            f"review_finding_{index}_structured_evidence_conflict:source"
        )
    if check_id is None and sibling_check:
        raise ReviewerEvidenceError(
            f"review_finding_{index}_structured_evidence_conflict:check_id"
        )
    transferred = (
        ("path", path),
        ("line_start", raw.get("line_start")),
        ("line_end", raw.get("line_end")),
        ("check_id", check_id),
    )
    for key, value in transferred:
        if value is None:
            continue
        existing = canonical.get(key)
        if existing not in (None, "") and existing != value:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_structured_evidence_conflict:{key}"
            )
        canonical[key] = value
    if path:
        start = raw.get("line_start")
        end = raw.get("line_end", start)
        canonical["evidence"] = (
            f"{path}:{start}-{end}" if end != start else f"{path}:{start}"
        )
    else:
        canonical["evidence"] = str(check_id)
    return canonical


def _scoped_audit_rows(
    values: Mapping[str, Mapping[str, Any] | ScopedAuditPacket],
    *,
    task_id: str,
    changed_paths: set[str],
) -> dict[str, dict[str, Any]]:
    """Validate and canonically embed one scoped packet per reviewer lens."""

    if not isinstance(values, Mapping) or not values:
        raise ReviewerEvidenceError("review_scope_missing")
    result: dict[str, dict[str, Any]] = {}
    for lens, value in sorted(values.items()):
        if lens not in {"correctness", "security", "code_quality"}:
            raise ReviewerEvidenceError("review_scope_lens_invalid")
        payload: object
        if isinstance(value, ScopedAuditPacket):
            payload = value.as_json()
            fingerprint = packet_fingerprint(value)
            schema_id = "aiworkhub.scoped_audit.v1"
            wrapper_known_unknowns = None
        elif isinstance(value, Mapping):
            payload = value.get("packet")
            fingerprint = str(value.get("fingerprint") or "")
            schema_id = str(value.get("schema_id") or "")
            wrapper_known_unknowns = value.get("known_unknowns")
        else:
            raise ReviewerEvidenceError("review_scope_invalid")
        if schema_id != "aiworkhub.scoped_audit.v1" or not isinstance(payload, Mapping):
            raise ReviewerEvidenceError("review_scope_schema_invalid")
        if not _SHA256_RE.fullmatch(fingerprint):
            raise ReviewerEvidenceError("review_scope_fingerprint_invalid")
        calculated = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if calculated != fingerprint:
            raise ReviewerEvidenceError("review_scope_fingerprint_invalid")
        if payload.get("task_id") != task_id:
            raise ReviewerEvidenceError("review_scope_task_mismatch")
        review_lens = payload.get("review_lens")
        if not isinstance(review_lens, Mapping) or review_lens.get("lens_kind") != lens:
            raise ReviewerEvidenceError("review_scope_lens_mismatch")
        scope_paths = {
            str(row.get("path") or "")
            for row in payload.get("changed_paths") or []
            if isinstance(row, Mapping)
        }
        if scope_paths != changed_paths:
            raise ReviewerEvidenceError("review_scope_changed_paths_mismatch")
        known_unknowns_source = payload.get("known_unknowns")
        if known_unknowns_source is None:
            known_unknowns_source = []
        if not isinstance(known_unknowns_source, list):
            raise ReviewerEvidenceError("review_scope_known_unknowns_invalid")
        if wrapper_known_unknowns is not None and wrapper_known_unknowns != known_unknowns_source:
            raise ReviewerEvidenceError("review_scope_known_unknowns_mismatch")
        known_unknowns = [
            str(item)[:MAX_TEXT_CHARS] for item in known_unknowns_source
        ]
        result[lens] = {
            "schema_id": schema_id,
            "fingerprint": fingerprint,
            "known_unknowns": known_unknowns,
            "packet": dict(payload),
        }
    return result


MAX_SOURCE_EVIDENCE_CHARS = 8_000
MAX_SOURCE_EVIDENCE_TOTAL_CHARS = 120_000
MAX_SOURCE_EVIDENCE_SEGMENTS = 200


def _source_evidence_rows(
    source_evidence: Mapping[str, Mapping[str, Any]],
    path_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate bounded source evidence bound one-to-one to changed paths."""

    def bounded_int(value: int | float | str | bytes | bytearray | None, field: str) -> int:
        if isinstance(value, bool):
            raise ReviewerEvidenceError("invalid_candidate_source_evidence")
        try:
            normalized = int(value or 0)
        except (TypeError, ValueError) as exc:
            raise ReviewerEvidenceError("invalid_candidate_source_evidence") from exc
        if normalized < 0:
            raise ReviewerEvidenceError("invalid_candidate_source_evidence")
        return normalized

    def segment_rows(values: object) -> list[dict[str, Any]]:
        if values is None:
            return []
        if not isinstance(values, list) or len(values) > MAX_SOURCE_EVIDENCE_SEGMENTS:
            raise ReviewerEvidenceError("invalid_candidate_source_evidence")
        result: list[dict[str, Any]] = []
        for segment in values:
            if not isinstance(segment, Mapping):
                raise ReviewerEvidenceError("invalid_candidate_source_evidence")
            kind = str(segment.get("kind") or "")
            if kind not in {"replace", "insert", "delete"}:
                raise ReviewerEvidenceError("invalid_candidate_source_evidence")
            result.append(
                {
                    "kind": kind,
                    "candidate_start_line": bounded_int(
                        segment.get("candidate_start_line"), "candidate_start_line"
                    ),
                    "candidate_end_line": bounded_int(
                        segment.get("candidate_end_line"), "candidate_end_line"
                    ),
                    "changed_start_line": bounded_int(
                        segment.get("changed_start_line"), "changed_start_line"
                    ),
                    "changed_end_line": bounded_int(
                        segment.get("changed_end_line"), "changed_end_line"
                    ),
                    "baseline_start_line": bounded_int(
                        segment.get("baseline_start_line"), "baseline_start_line"
                    ),
                    "baseline_end_line": bounded_int(
                        segment.get("baseline_end_line"), "baseline_end_line"
                    ),
                    "excerpt_bytes": bounded_int(
                        segment.get("excerpt_bytes"), "excerpt_bytes"
                    ),
                    "truncated": bool(segment.get("truncated")),
                }
            )
        return result

    if not isinstance(source_evidence, Mapping) or not source_evidence:
        raise ReviewerEvidenceError("candidate_source_evidence_missing")
    expected = {row["path"]: row["sha256"] for row in path_rows}
    if set(source_evidence) != set(expected):
        raise ReviewerEvidenceError("candidate_source_evidence_path_mismatch")
    rows: list[dict[str, Any]] = []
    total = 0
    for path in sorted(expected):
        row = source_evidence[path]
        if not isinstance(row, Mapping):
            raise ReviewerEvidenceError("invalid_candidate_source_evidence")
        expected_digest = expected[path]
        digest = row.get("candidate_sha256")
        if expected_digest is None:
            if digest is not None:
                raise ReviewerEvidenceError("candidate_source_evidence_hash_mismatch")
        else:
            digest = str(digest or "")
            if not _SHA256_RE.fullmatch(digest) or digest != expected_digest:
                raise ReviewerEvidenceError("candidate_source_evidence_hash_mismatch")
        excerpt = row.get("excerpt")
        if not isinstance(excerpt, str):
            raise ReviewerEvidenceError("invalid_candidate_source_evidence")
        if len(excerpt) > MAX_SOURCE_EVIDENCE_CHARS:
            raise ReviewerEvidenceError("review_packet_overflow")
        total += len(excerpt)
        if total > MAX_SOURCE_EVIDENCE_TOTAL_CHARS:
            raise ReviewerEvidenceError("review_packet_overflow")
        source_bytes = bounded_int(row.get("source_bytes"), "source_bytes")
        excerpt_bytes = bounded_int(row.get("excerpt_bytes"), "excerpt_bytes")
        if excerpt_bytes > max(source_bytes, len(excerpt.encode("utf-8"))):
            raise ReviewerEvidenceError("invalid_candidate_source_evidence")
        segments = segment_rows(row.get("segments"))
        omission_reason = str(row.get("omission_reason") or "")[:MAX_TEXT_CHARS]
        if omission_reason.startswith("changed_hunks_omitted:"):
            try:
                omitted_hunks = int(omission_reason.split(":", 1)[1])
            except (TypeError, ValueError) as exc:
                raise ReviewerEvidenceError(
                    "invalid_candidate_source_evidence"
                ) from exc
            if omitted_hunks < 1 or sum(
                1 for segment in segments if segment["truncated"]
            ) < omitted_hunks:
                raise ReviewerEvidenceError("invalid_candidate_source_evidence")
        if not excerpt and not omission_reason and not segments:
            raise ReviewerEvidenceError("invalid_candidate_source_evidence")
        result = {
            "path": path,
            "candidate_sha256": digest,
            "excerpt": excerpt,
            "excerpt_bytes": excerpt_bytes,
            "source_bytes": source_bytes,
            "truncated": bool(row.get("truncated")),
            "segments": segments,
        }
        if omission_reason:
            result["omission_reason"] = omission_reason
        baseline_omission = str(
            row.get("baseline_omission_reason") or ""
        )[:MAX_TEXT_CHARS]
        if baseline_omission:
            result["baseline_omission_reason"] = baseline_omission
        rows.append(result)
    return rows


__all__ = [
    "PACKET_SCHEMA_ID",
    "RECEIPT_SCHEMA_ID",
    "MAX_SOURCE_EVIDENCE_CHARS",
    "MAX_SOURCE_EVIDENCE_TOTAL_CHARS",
    "FINDING_SEVERITIES",
    "FINDING_DISPOSITIONS",
    "FINDING_CATEGORIES",
    "OVERBUILD_FINDING_CATEGORIES",
    "QualityReviewFinding",
    "QUALITY_REVIEW_FINDING_INPUT_KEYS",
    "QUALITY_REVIEW_FINDING_INPUT_REQUIRED_KEYS",
    "QUALITY_REVIEW_FINDING_REQUIRED_KEYS",
    "QUALITY_REVIEW_FINDING_KEYS",
    "QUALITY_REVIEW_FINDING_SCHEMA_DOC",
    "QUALITY_REVIEW_SUBMIT_TOOL_DESCRIPTION",
    "ReviewerEvidenceError",
    "build_review_packet",
    "build_review_prompt",
    "normalize_packet_findings",
    "verify_review_packet_candidate",
    "verify_reviewer_receipt",
]
