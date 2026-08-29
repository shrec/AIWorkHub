"""Bounded, fail-closed ingestion of provider-owned quality-review finals."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_EVENT_BYTES = 1_048_576
MAX_EVENTS = 4096
REPORT_KEYS = frozenset({"lens", "findings"})


class ReviewProtocolError(RuntimeError):
    """A stable protocol failure, distinct from provider/infrastructure failure."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(f"review_protocol:{category}")


@dataclass(frozen=True)
class IngestResult:
    status: str
    report: dict[str, Any] | None
    submitted: bool = False
    deduplicated: bool = False


def provider_final_text(event: Mapping[str, Any]) -> str:
    """Mirror the launcher's allowlist of final/assistant event shapes."""
    event_type = event.get("type")
    if event_type == "result":
        if event.get("is_error") is True or event.get("subtype") == "error":
            return ""
        value = event.get("result")
        return value.strip() if isinstance(value, str) else ""
    if event_type == "item.completed":
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") == "agent_message":
            value = item.get("text")
            return value.strip() if isinstance(value, str) else ""
        return ""
    if event_type in {"assistant.message", "assistant_message"}:
        data = event.get("data")
        value = data.get("content") if isinstance(data, Mapping) else None
        return value.strip() if isinstance(value, str) else ""
    if event_type == "text":
        part = event.get("part")
        if isinstance(part, Mapping) and part.get("type") == "text":
            value = part.get("text")
            return value.strip() if isinstance(value, str) else ""
    return ""


def _report_from_text(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        if text.lstrip().startswith(("{", "[")):
            raise ReviewProtocolError("malformed_structured_output") from None
        return None
    if not isinstance(value, dict):
        raise ReviewProtocolError("multiple_report_objects" if isinstance(value, list) else "malformed_structured_output")
    report: Any = value.get("report", value)
    if not isinstance(report, dict) or not REPORT_KEYS <= report.keys():
        return None
    if isinstance(value.get("report"), dict) and any(
        isinstance(v, dict) and REPORT_KEYS <= v.keys()
        for key, v in value.items() if key != "report"
    ):
        raise ReviewProtocolError("multiple_report_objects")
    if not isinstance(report.get("lens"), str) or not isinstance(report.get("findings"), list):
        raise ReviewProtocolError("malformed_structured_output")
    return dict(report)


def extract_structured_final(events: Iterable[str], *, expected_lens: str) -> IngestResult:
    """Extract exactly one report from bounded JSONL provider events."""
    reports: list[dict[str, Any]] = []
    final_count = 0
    for count, raw in enumerate(events, 1):
        if count > MAX_EVENTS or len(raw.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ReviewProtocolError("provider_events_oversized")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        text = provider_final_text(event)
        if not text:
            continue
        final_count += 1
        report = _report_from_text(text)
        if report is not None:
            reports.append(report)
    if len(reports) > 1:
        raise ReviewProtocolError("multiple_structured_finals")
    if not reports:
        return IngestResult("unstructured_final" if final_count else "missing_final", None)
    report = reports[0]
    if report["lens"] != expected_lens:
        raise ReviewProtocolError("lens_mismatch")
    return IngestResult("structured_final", report)


def ingest_structured_final(
    events: Iterable[str], *, expected_lens: str,
    explicit_report: Mapping[str, Any] | None = None,
    submit: Callable[[dict[str, Any]], Any] | None = None,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> IngestResult:
    """Apply legacy compatibility, logical dedup, and supervisor submission."""
    result = extract_structured_final(events, expected_lens=expected_lens)
    report = result.report
    if report is not None and normalize is not None:
        report = normalize(report)
    if explicit_report is not None:
        authoritative = dict(explicit_report)
        if report is None:
            return IngestResult("explicit_only", authoritative, deduplicated=True)
        if authoritative != report:
            raise ReviewProtocolError("explicit_submission_conflict")
        return IngestResult("deduplicated", report, deduplicated=True)
    if report is None:
        return result
    if submit is None:
        return IngestResult(result.status, report)
    submit(report)
    return IngestResult("submitted", report, submitted=True)


def supervisor_ingest(
    *, metadata: Mapping[str, Any], workspace: Any, packet: Mapping[str, Any],
    packet_path: Path, request_id: str, expected_lens: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Reconcile provider final output with the process-bound durable audit."""
    from . import quality_reviewer, worker_ai_tools_mcp

    worker_meta = metadata.get("worker_mcp") or {}
    ledger = Path(str(worker_meta.get("audit_ledger_path") or ""))
    key = Path(str(worker_meta.get("audit_hmac_key_path") or ""))

    def verify() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        audit = worker_ai_tools_mcp.verify_audit_ledger(
            ledger, key, task_id=str(metadata.get("task_id") or ""),
            runner=str(metadata.get("runner") or ""),
            topic=str(metadata.get("topic") or ""), request_id=request_id,
        )
        return audit, [p for p in audit.get("verified_payloads") or [] if isinstance(p, dict)]

    _, payloads = verify()
    if len(payloads) > 1:
        raise ReviewProtocolError(f"submission_count:{len(payloads)}")
    explicit = None
    if payloads:
        report = payloads[0].get("report")
        if not isinstance(report, dict):
            raise ReviewProtocolError("explicit_receipt_shape_invalid")
        explicit = {"lens": report.get("lens"), "findings": report.get("findings")}
    stdout = Path(str(metadata.get("stdout_path") or ""))
    valid_stdout = (
        stdout.is_absolute() and stdout.name == f"{request_id}.stdout.log"
        and not stdout.is_symlink() and stdout.is_file()
    )
    if not valid_stdout and not payloads:
        raise ReviewProtocolError("provider_events_unavailable")
    events: Iterable[str] = () if not valid_stdout else stdout.open(encoding="utf-8")

    def normalize(report: dict[str, Any]) -> dict[str, Any]:
        try:
            findings = quality_reviewer.normalize_packet_findings(
                packet, lens=expected_lens, findings=list(report.get("findings") or []),
            )
        except quality_reviewer.ReviewerEvidenceError as exc:
            raise ReviewProtocolError(f"structured_report_invalid:{exc}") from exc
        return {"lens": expected_lens, "findings": findings}

    def submit(report: dict[str, Any]) -> None:
        ctx = worker_ai_tools_mcp.WorkerToolContext(
            task_id=str(metadata.get("task_id") or ""), runner=str(metadata.get("runner") or ""),
            topic=str(metadata.get("topic") or ""), request_id=request_id,
            repo=workspace.repo, authority_repo=workspace.repo, source_graph_targets=(),
            session_topic=str(metadata.get("topic") or ""), audit_ledger_path=ledger,
            audit_hmac_key_path=key, quality_review_packet_path=packet_path,
            allowed_writes=(), provenance="live", _supervisor_owned=True,
        )
        result = worker_ai_tools_mcp.quality_review_submit(
            ctx, packet_sha256=str(packet.get("packet_sha256") or ""),
            lens=expected_lens, findings=list(report.get("findings") or []),
        )
        if not result.get("ok"):
            raise ReviewProtocolError(str(result.get("reason") or "internal_submission_failed"))

    try:
        ingest_structured_final(
            events, expected_lens=expected_lens, explicit_report=explicit,
            submit=submit, normalize=normalize,
        )
    except (UnicodeDecodeError, OSError) as exc:
        raise ReviewProtocolError("provider_events_unreadable") from exc
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            close()
    return verify()
