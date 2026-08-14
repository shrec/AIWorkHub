from __future__ import annotations

import hashlib
import json
import re
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


QualityReviewFinding = TypedDict(
    "QualityReviewFinding",
    {
        "severity": str,
        "summary": str,
        "evidence": str,
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
QUALITY_REVIEW_FINDING_REQUIRED_KEYS = frozenset(
    {
        "id",
        "severity",
        "disposition",
        "actionable",
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
    {"evidence_reference"}
)
QUALITY_REVIEW_FINDING_SCHEMA_DOC = (
    "One finding object uses the canonical shape with required keys "
    "severity (critical|high|medium|low), summary and evidence; optional id "
    "(stable identifier, derived when omitted), disposition (defect default; "
    "observation and process_limit must be low severity), confidence "
    "(low|medium|high), symbol, claim, reproduction and required_validation. "
    "Defects must cite an exact packet-permitted path and line "
    "(path/line_start/line_end) or a mechanical check_id. The tool derives "
    "actionable, evidence_level and evidence_reference; do not invent keys "
    "outside this shape. An empty findings list is valid."
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
    rows = (packet.get("candidate") or {}).get("changed_paths") or []
    if not isinstance(rows, list) or not rows:
        raise ReviewerEvidenceError("quality_review_candidate_paths_missing")
    resolved_repo = repo.resolve()
    verified_paths: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReviewerEvidenceError("quality_review_candidate_path_invalid")
        relative = str(row.get("path") or "")
        expected = str(row.get("sha256") or "")
        if not relative or relative.startswith("/") or ".." in relative.split("/"):
            raise ReviewerEvidenceError("quality_review_candidate_path_invalid")
        candidate = resolved_repo / relative
        if _has_symlink_component(resolved_repo, relative):
            raise ReviewerEvidenceError("quality_review_candidate_path_symlink")
        candidate = candidate.resolve()
        if not candidate.is_relative_to(resolved_repo) or not candidate.is_file():
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
    if not all(isinstance(value, Mapping) for value in (target, receipt_target, reviewer, report)):
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


def build_review_prompt(
    packet: Mapping[str, Any],
    *,
    lens: str,
    submit_tool_name: str = "aiworkhub_worker_quality_review_submit",
    packet_file: str | None = None,
    max_inline_bytes: int = 96 * 1024,
) -> str:
    """Render a bounded independent-review prompt from packet facts only.

    When *packet_file* is provided the serialised packet is written to that
    path and the prompt references it via a worker-scoped file read instead of
    embedding the full JSON inline.  This avoids E2BIG on native CLI adapters
    where large quality-review payloads would otherwise be passed through argv.
    *max_inline_bytes* guards the inline fallback when *packet_file* is None.
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
    use_file_transport = (
        packet_file is not None and len(encoded.encode("utf-8")) > max_inline_bytes
    )
    if use_file_transport:
        import os as _os
        if not _os.path.isfile(packet_file):
            raise ReviewerEvidenceError("review_packet_file_missing")
    packet_evidence = (
        f"QUALITY_REVIEW_PACKET_FILE: {packet_file}\n"
        f"PACKET_SHA256: {packet_digest}\n"
        "Read the packet file first with a worker file-read tool; its "
        f"contents are the authoritative evidence for this review.\n"
        if use_file_transport
        else f"QUALITY_REVIEW_PACKET: {encoded}\n"
    )
    return (
        "You are an independent, strictly read-only quality reviewer.\n"
        f"Review lens: {lens}.\n"
        "Inspect the exact candidate workspace and the deterministic packet below. "
        "You are intentionally not given the worker's rationale, self-verdict, or final answer. "
        "Do not write, edit, format, or delete repository files.\n"
        f"{scope_instruction}"
        "Report only concrete items supported by file/line or check evidence. "
        f"{QUALITY_REVIEW_FINDING_SCHEMA_DOC}\n"
        f"Before finishing, call {submit_tool_name} exactly once with "
        f'packet_sha256="{packet_digest}", lens="{lens}", and your findings array.\n'
        "The tool call is the authoritative submission; prose is not evidence.\n"
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

    rows = list(findings)
    if len(rows) > 100:
        raise ReviewerEvidenceError("review_findings_overflow")
    normalized: list[dict[str, Any]] = []
    for index, finding in enumerate(rows):
        if not isinstance(finding, Mapping):
            raise ReviewerEvidenceError(f"review_finding_{index}_not_object")
        unknown = set(finding) - QUALITY_REVIEW_FINDING_INPUT_KEYS
        if unknown:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_unknown_key:{','.join(sorted(unknown))}"
            )
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
        if "summary" not in finding:
            raise ReviewerEvidenceError(f"review_finding_{index}_summary_missing")
        if "evidence" not in finding:
            raise ReviewerEvidenceError(f"review_finding_{index}_evidence_missing")
        summary = str(finding["summary"] or "").strip()
        evidence = str(finding["evidence"] or "").strip()
        if not summary or not evidence:
            raise ReviewerEvidenceError(f"review_finding_{index}_text_missing")

        evidence_reference: dict[str, Any] | None = None
        explicit_path = str(finding.get("path") or "")
        explicit_check = str(finding.get("check_id") or "")
        if explicit_path:
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
        elif explicit_check:
            if explicit_check not in permitted_checks:
                raise ReviewerEvidenceError(
                    f"review_finding_{index}_check_out_of_scope"
                )
            evidence_reference = {"kind": "check", "check_id": explicit_check}
        else:
            for path in sorted(permitted_paths, key=lambda value: (-len(value), value)):
                match = re.search(
                    rf"(?<![A-Za-z0-9_.-]){re.escape(path)}:(\d+)(?:-(\d+))?",
                    evidence,
                )
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
                    if check_id and check_id in evidence:
                        evidence_reference = {
                            "kind": "check",
                            "check_id": check_id,
                        }
                        break

        actionable = disposition == "defect"
        if actionable and evidence_reference is None:
            raise ReviewerEvidenceError(
                f"review_finding_{index}_exact_evidence_required"
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
        if evidence_reference is not None:
            normalized_finding["evidence_reference"] = evidence_reference
        normalized.append(normalized_finding)
    return normalized


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
        if isinstance(value, ScopedAuditPacket):
            payload = value.as_json()
            fingerprint = packet_fingerprint(value)
            schema_id = "aiworkhub.scoped_audit.v1"
        elif isinstance(value, Mapping):
            payload = value.get("packet")
            fingerprint = str(value.get("fingerprint") or "")
            schema_id = str(value.get("schema_id") or "")
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
        result[lens] = {
            "schema_id": schema_id,
            "fingerprint": fingerprint,
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

    def bounded_int(value: object, field: str) -> int:
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
