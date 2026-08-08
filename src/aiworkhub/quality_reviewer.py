from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any


PACKET_SCHEMA_ID = "aiworkhub.quality_review_packet.v1"
RECEIPT_SCHEMA_ID = "aiworkhub.quality_reviewer_receipt.v1"
MAX_PACKET_PATHS = 200
MAX_PACKET_CHECKS = 200
MAX_PACKET_COMMANDS = 100
MAX_TEXT_CHARS = 2_000

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
) -> str:
    """Render a bounded independent-review prompt from packet facts only."""

    if lens not in {"correctness", "security", "code_quality"}:
        raise ReviewerEvidenceError("invalid_reviewer_lens")
    packet_body = {k: v for k, v in packet.items() if k != "packet_sha256"}
    packet_digest = str(packet.get("packet_sha256") or "")
    if not _SHA256_RE.fullmatch(packet_digest) or _canonical_digest(packet_body) != packet_digest:
        raise ReviewerEvidenceError("review_packet_digest_invalid")
    encoded = json.dumps(packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (
        "You are an independent, strictly read-only quality reviewer.\n"
        f"Review lens: {lens}.\n"
        "Inspect the exact candidate workspace and the deterministic packet below. "
        "You are intentionally not given the worker's rationale, self-verdict, or final answer. "
        "Do not write, edit, format, or delete repository files.\n"
        "Report only concrete findings supported by file/line or check evidence. "
        "Use severity critical, high, medium, or low. An empty findings list is valid.\n"
        f"Before finishing, call {submit_tool_name} exactly once with "
        f'packet_sha256="{packet_digest}", lens="{lens}", and your findings array.\n'
        "The tool call is the authoritative submission; prose is not evidence.\n"
        f"QUALITY_REVIEW_PACKET: {encoded}\n"
    )


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
    "ReviewerEvidenceError",
    "build_review_packet",
    "build_review_prompt",
    "verify_reviewer_receipt",
]
