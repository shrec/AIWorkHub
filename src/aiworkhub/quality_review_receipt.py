"""Authenticity of a quality-review receipt: its exact shape, and its proof.

A reviewer process returns one JSON receipt.  Before any finalization may rest
on it, three separate things have to be true, and this module owns all three:

* the receipt matches the exact production read-only schema -- an exhaustive
  key set at every level, lowercase 64-hex packet and submission hashes,
  bool-safe claim epoch and submission counts, verified authority and a
  ``review_ready`` terminal state (``_enforce_quality_review_receipt_schema``);
* the reviewer's retained workspace is provably read-only and empty, so the
  reviewer cannot have written the repository it was judging
  (``_enforce_readonly_retained_workspace``);
* exactly one authenticated logical submission exists for the reviewer
  process, produced by an independent provider against the immutable target
  packet (``_verified_quality_review_receipt``).

Split out of ``process_launcher`` unchanged.  The rules live here so the
launcher keeps process lifecycle evidence and this module keeps the receipt's
schema and authority; every check, message and value is exactly as it was.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from . import quality_review
from . import quality_review_ingest
from . import quality_reviewer
from . import task_store
from . import worker_ai_tools_mcp
from .worker_workspace import WorkerWorkspace, WorkspaceError


_QUALITY_REVIEW_RECEIPT_TOP_KEYS = frozenset(
    {
        "schema_id",
        "packet_sha256",
        "target",
        "reviewer",
        "report",
        "authority",
        "submission_id",
        "physical_submission_count",
        "logical_submission_count",
    }
)
_QUALITY_REVIEW_TARGET_KEYS = frozenset({"request_id", "task_id", "claim_epoch"})
_QUALITY_REVIEW_REVIEWER_KEYS = frozenset({"request_id", "task_id", "provider"})
_QUALITY_REVIEW_REPORT_KEYS = frozenset(
    {"lens", "provider", "read_only", "can_mutate_repo", "findings"}
)
_QUALITY_REVIEW_AUTHORITY_KEYS = frozenset(
    {"process_identity_verified", "audit_verified", "terminal_state"}
)
# The receipt's findings are produced by quality_evidence.normalize_reviewer_reports,
# which unconditionally emits the canonical finding schema's own required fields
# plus the derived ``actionable`` flag, and carries every key the schema marks
# OPTIONAL through only when the reviewer actually supplied it.  Requiring the
# reviewer boundary's full emit-set (QUALITY_REVIEW_FINDING_REQUIRED_KEYS)
# therefore demanded keys the receipt producer never guaranteed.  Measured
# against the durable receipt store, that floor admitted 33 of 1902 real
# findings (1%) and refused 1732 for a missing ``category`` alone -- discarding
# a fully paid-for reviewer run at the last step of finalization.
#
# The floor is DERIVED from the authoritative vocabularies rather than restated,
# so it cannot drift from them: the canonical required emit-set minus everything
# the canonical finding schema declares optional.  This widens presence only to
# what the canonical normalizer provably emits; the ceiling is unchanged, so an
# unknown key is still refused, and severity/disposition/actionable stay
# mandatory by value immediately below, where an absent key fails closed.
_QUALITY_REVIEW_FINDING_RECEIPT_REQUIRED_KEYS = (
    quality_reviewer.QUALITY_REVIEW_FINDING_REQUIRED_KEYS
    - (
        quality_reviewer.QUALITY_REVIEW_FINDING_INPUT_KEYS
        - quality_reviewer.QUALITY_REVIEW_FINDING_INPUT_REQUIRED_KEYS
    )
)
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


# One authority for the bool-safe integer rule, owned by the store that binds
# the claim epochs it guards.  A private copy here would silently stop matching
# the store's rule and admit an epoch the store would reject -- visible only as
# a terminal transition bound to the wrong episode.
_is_bool_safe_int = task_store.is_bool_safe_int


def _is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and _SHA256_HEX_RE.fullmatch(value) is not None


def _enforce_quality_review_receipt_schema(
    receipt: dict[str, Any], observed_provider: str
) -> dict[str, Any]:
    """Reject any deviation from the exact production read-only receipt shape."""
    if set(receipt) != _QUALITY_REVIEW_RECEIPT_TOP_KEYS:
        raise WorkspaceError("quality_review_receipt_top_level_keys_invalid")
    if receipt.get("schema_id") != quality_reviewer.RECEIPT_SCHEMA_ID:
        raise WorkspaceError("quality_review_receipt_schema_mismatch")
    packet_sha256 = receipt.get("packet_sha256")
    submission_id = receipt.get("submission_id")
    if not _is_sha256_hex(packet_sha256):
        raise WorkspaceError("quality_review_packet_sha256_invalid")
    if not _is_sha256_hex(submission_id):
        raise WorkspaceError("quality_review_submission_id_invalid")
    target = receipt.get("target")
    reviewer = receipt.get("reviewer")
    report = receipt.get("report")
    authority = receipt.get("authority")
    if not (
        isinstance(target, dict)
        and isinstance(reviewer, dict)
        and isinstance(report, dict)
        and isinstance(authority, dict)
    ):
        raise WorkspaceError("quality_review_receipt_shape_invalid")
    if set(target) != _QUALITY_REVIEW_TARGET_KEYS:
        raise WorkspaceError("quality_review_target_keys_invalid")
    if set(reviewer) != _QUALITY_REVIEW_REVIEWER_KEYS:
        raise WorkspaceError("quality_review_reviewer_keys_invalid")
    if set(report) != _QUALITY_REVIEW_REPORT_KEYS:
        raise WorkspaceError("quality_review_report_keys_invalid")
    if set(authority) != _QUALITY_REVIEW_AUTHORITY_KEYS:
        raise WorkspaceError("quality_review_authority_keys_invalid")
    claim_epoch = target.get("claim_epoch")
    if not _is_bool_safe_int(claim_epoch):
        raise WorkspaceError("quality_review_claim_epoch_invalid")
    if str(reviewer.get("provider") or "") != observed_provider:
        raise WorkspaceError("quality_review_reviewer_provider_mismatch")
    if str(report.get("provider") or "") != observed_provider:
        raise WorkspaceError("quality_review_report_provider_mismatch")
    findings = report.get("findings")
    if not isinstance(findings, list):
        raise WorkspaceError("quality_review_report_findings_invalid")
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise WorkspaceError(f"quality_review_finding_{index}_invalid")
        finding_keys = set(finding)
        if not (
            _QUALITY_REVIEW_FINDING_RECEIPT_REQUIRED_KEYS <= finding_keys
            <= quality_reviewer.QUALITY_REVIEW_FINDING_KEYS
        ):
            raise WorkspaceError(f"quality_review_finding_{index}_keys_invalid")
        if str(finding.get("severity") or "") not in quality_reviewer.FINDING_SEVERITIES:
            raise WorkspaceError(f"quality_review_finding_{index}_severity_invalid")
        if (
            str(finding.get("disposition") or "")
            not in quality_reviewer.FINDING_DISPOSITIONS
        ):
            raise WorkspaceError(f"quality_review_finding_{index}_disposition_invalid")
        if finding.get("actionable") is not (finding.get("disposition") == "defect"):
            raise WorkspaceError(f"quality_review_finding_{index}_actionable_invalid")
    if authority.get("process_identity_verified") is not True:
        raise WorkspaceError("quality_review_authority_process_identity_invalid")
    if authority.get("audit_verified") is not True:
        raise WorkspaceError("quality_review_authority_audit_invalid")
    if authority.get("terminal_state") != "review_ready":
        raise WorkspaceError("quality_review_authority_terminal_state_invalid")
    if report.get("read_only") is not True or report.get("can_mutate_repo") is not False:
        raise WorkspaceError("quality_review_report_not_read_only")
    physical_submission_count = receipt.get("physical_submission_count")
    logical_submission_count = receipt.get("logical_submission_count")
    if (
        not _is_bool_safe_int(physical_submission_count)
        or physical_submission_count != 1
    ):
        raise WorkspaceError("quality_review_physical_submission_count_invalid")
    if not _is_bool_safe_int(logical_submission_count) or logical_submission_count != 1:
        raise WorkspaceError("quality_review_logical_submission_count_invalid")
    return receipt


def _verified_quality_review_receipt(
    metadata: dict[str, Any],
    workspace: WorkerWorkspace,
    request_id: str,
) -> dict[str, Any]:
    """Resolve exactly one authenticated logical submission for a reviewer process."""

    binding = metadata.get("quality_review")
    if not isinstance(binding, dict):
        raise WorkspaceError("quality_review_binding_missing")
    packet_path_raw = binding.get("packet_path")
    if not isinstance(packet_path_raw, str) or not packet_path_raw:
        raise WorkspaceError("quality_review_packet_path_missing")
    packet_path = Path(packet_path_raw).resolve()
    try:
        packet_path.relative_to(workspace.home.resolve())
    except ValueError as exc:
        raise WorkspaceError("quality_review_packet_outside_home") from exc
    try:
        if packet_path.is_symlink() or not packet_path.is_file():
            raise WorkspaceError("quality_review_packet_invalid")
        if packet_path.stat().st_size > worker_ai_tools_mcp.MAX_QUALITY_REVIEW_PACKET_BYTES:
            raise WorkspaceError("quality_review_packet_too_large")
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("quality_review_packet_unreadable") from exc
    expected_lens = str(binding.get("lens") or "")
    try:
        verification, payloads = quality_review_ingest.supervisor_ingest(
            metadata=metadata,
            workspace=workspace,
            packet=packet,
            packet_path=packet_path,
            request_id=request_id,
            expected_lens=expected_lens,
        )
    except quality_review_ingest.ReviewProtocolError as exc:
        raise WorkspaceError(str(exc)) from exc
    if len(payloads) != 1:
        raise WorkspaceError(f"quality_review_submission_count:{len(payloads)}")
    receipt_payload = payloads[0]
    observed_provider = str(metadata.get("adapter_id") or "")
    target = packet.get("target") if isinstance(packet, dict) else None
    if not isinstance(target, dict):
        raise WorkspaceError("quality_review_packet_target_missing")
    worker_provider_name = str(target.get("worker_provider") or "")
    rung_record = quality_review.resolve_independence_rung(
        worker_provider=worker_provider_name,
        reviewer_provider=observed_provider,
        worker_model=worker_provider_name,
        reviewer_model=observed_provider,
    )
    if rung_record["rung"] not in quality_review.INDEPENDENCE_LADDER:
        raise WorkspaceError(
            "quality_review_provider_not_independent:"
            f"worker_provider={worker_provider_name},"
            f"reviewer_provider={observed_provider}"
        )
    receipt = json.loads(json.dumps(receipt_payload, ensure_ascii=False))
    reviewer = receipt.get("reviewer")
    report = receipt.get("report")
    if not isinstance(reviewer, dict) or not isinstance(report, dict):
        raise WorkspaceError("quality_review_receipt_shape_invalid")
    reviewer["provider"] = observed_provider
    report["provider"] = observed_provider
    entries_tampered = verification.get("entries_tampered")
    if not _is_bool_safe_int(entries_tampered):
        raise WorkspaceError("quality_review_audit_entries_tampered_invalid")
    audit_verified = bool(verification.get("ok")) and entries_tampered == 0
    try:
        verified = quality_reviewer.verify_reviewer_receipt(
            receipt,
            packet=packet,
            expected_reviewer_request_id=request_id,
            expected_reviewer_task_id=str(metadata.get("task_id") or ""),
            observed_provider=observed_provider,
            observed_terminal_state="review_ready",
            audit_verified=audit_verified,
        )
    except quality_reviewer.ReviewerEvidenceError as exc:
        raise WorkspaceError(f"quality_review_receipt_invalid:{exc}") from exc
    if str((verified.get("report") or {}).get("lens") or "") != expected_lens:
        raise WorkspaceError("quality_review_lens_mismatch")
    verified["submission_id"] = hashlib.sha256(
        json.dumps(
            receipt_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    verified["physical_submission_count"] = 1
    verified["logical_submission_count"] = 1
    return _enforce_quality_review_receipt_schema(verified, observed_provider)
