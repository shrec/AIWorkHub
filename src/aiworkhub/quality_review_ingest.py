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
    final_excerpt: str = ""


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


MAX_FINAL_EXCERPT_CHARS = 500


def _report_from_value(value: Any) -> dict[str, Any] | None:
    """Validate one already-parsed JSON value as a single review report."""
    if not isinstance(value, dict):
        raise ReviewProtocolError(
            "multiple_report_objects" if isinstance(value, list) else "malformed_structured_output"
        )
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


def _iter_balanced_objects(text: str) -> Iterable[str]:
    """Yield each top-level balanced ``{...}`` region, string-aware.

    Braces are balanced only once inside an object, so quotes and braces in the
    surrounding prose -- including a lone unmatched quote -- never derail the
    scan.  This locates the report whether it is bare, wrapped in prose, or
    fenced inside a ```json block.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if depth == 0:
            if char == "{":
                depth = 1
                start = index
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start : index + 1]
                start = -1


def _last_report_object(text: str) -> dict[str, Any] | None:
    """Return the last balanced JSON object in ``text`` that is a report."""
    found: dict[str, Any] | None = None
    for candidate in _iter_balanced_objects(text):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        report = _report_from_value(value)
        if report is not None:
            found = report
    return found


def _bounded_final_excerpt(text: str) -> str:
    """Collapse whitespace and cap the reviewer's final text for a reason."""
    collapsed = " ".join(text.split())
    if len(collapsed) > MAX_FINAL_EXCERPT_CHARS:
        return collapsed[:MAX_FINAL_EXCERPT_CHARS] + "..."
    return collapsed


def _report_from_text(text: str) -> dict[str, Any] | None:
    """Extract one review report from a provider final message.

    The reviewer prompt asks for exactly one bare JSON object, so a strict
    whole-message parse runs first and preserves the exact multiple/malformed
    diagnostics for a well-behaved reviewer.  A capable reviewer instead often
    ends with helpful prose and wraps the report in a fenced ```json block or
    inline braces; that final is now accepted by taking the last balanced JSON
    object carrying the report keys rather than being silently dropped.
    """
    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        report = _last_report_object(stripped)
        if report is not None:
            return report
        if stripped.startswith(("{", "[")):
            raise ReviewProtocolError("malformed_structured_output") from None
        return None
    return _report_from_value(value)


def extract_structured_final(events: Iterable[str], *, expected_lens: str) -> IngestResult:
    """Extract exactly one report from bounded JSONL provider events."""
    reports: list[dict[str, Any]] = []
    final_count = 0
    last_final_text = ""
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
        last_final_text = text
        report = _report_from_text(text)
        if report is not None:
            reports.append(report)
    if len(reports) > 1:
        raise ReviewProtocolError("multiple_structured_finals")
    if not reports:
        return IngestResult(
            "unstructured_final" if final_count else "missing_final",
            None,
            final_excerpt=_bounded_final_excerpt(last_final_text),
        )
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
        # Fail closed with the reviewer's actual final output, never a bare
        # submission count.  The SUPERVISOR -- never the reviewer -- derives
        # identity and submits, so a missing report here means nothing durable
        # was produced; the finalizer must see WHY (an unparseable prose final
        # or a genuinely absent final), not just "submission_count:0".
        raise ReviewProtocolError(_no_report_reason(result))
    if submit is None:
        return IngestResult(result.status, report)
    submit(report)
    return IngestResult("submitted", report, submitted=True)


def _no_report_reason(result: IngestResult) -> str:
    """Explain an empty ingest with a bounded excerpt of the reviewer output."""
    if result.status == "unstructured_final":
        excerpt = result.final_excerpt or "(empty final message)"
        return f"no_report_in_final:{excerpt}"
    return "no_provider_final"


def _normalize_review_finding_aliases(report: Mapping[str, Any]) -> dict[str, Any]:
    """Translate only supported provider aliases without changing review authority."""
    normalized_report = dict(report)
    normalized_findings: list[Any] = []
    for index, raw_finding in enumerate(report.get("findings") or []):
        if not isinstance(raw_finding, Mapping):
            normalized_findings.append(raw_finding)
            continue
        finding = dict(raw_finding)
        if "actionable" in finding:
            if type(finding["actionable"]) is not bool:
                raise ReviewProtocolError(
                    f"structured_report_invalid:review_finding_{index}_actionable_invalid"
                )
            finding.pop("actionable")
        if "evidence_reference" in finding:
            raw_reference = finding.pop("evidence_reference")
            if not isinstance(raw_reference, Mapping):
                raise ReviewProtocolError(
                    f"structured_report_invalid:review_finding_{index}_evidence_reference_invalid"
                )
            reference = dict(raw_reference)
            allowed = {"path", "line_start", "line_end", "check_id"}
            if any(not isinstance(key, str) for key in reference) or set(reference) - allowed:
                raise ReviewProtocolError(
                    f"structured_report_invalid:review_finding_{index}_evidence_reference_invalid"
                )
            path = reference.get("path")
            check_id = reference.get("check_id")
            if bool(path) == bool(check_id):
                raise ReviewProtocolError(
                    f"structured_report_invalid:review_finding_{index}_evidence_reference_invalid"
                )
            for key in ("path", "check_id"):
                value = reference.get(key)
                if value is not None and (
                    not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096
                ):
                    raise ReviewProtocolError(
                        f"structured_report_invalid:review_finding_{index}_evidence_reference_invalid"
                    )
            for key in ("line_start", "line_end"):
                value = reference.get(key)
                if value is not None and (
                    type(value) is not int or value < 1 or value > 1_000_000
                ):
                    raise ReviewProtocolError(
                        f"structured_report_invalid:review_finding_{index}_evidence_reference_invalid"
                    )
            for key, value in reference.items():
                existing = finding.get(key)
                if existing not in (None, "") and existing != value:
                    raise ReviewProtocolError(
                        f"structured_report_invalid:review_finding_{index}_evidence_reference_conflict:{key}"
                    )
                finding[key] = value
            if "evidence" not in finding:
                finding["evidence"] = reference
        normalized_findings.append(finding)
    normalized_report["findings"] = normalized_findings
    return normalized_report


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
            normalized_report = _normalize_review_finding_aliases(report)
            findings = quality_reviewer.normalize_packet_findings(
                packet, lens=expected_lens,
                findings=list(normalized_report.get("findings") or []),
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
