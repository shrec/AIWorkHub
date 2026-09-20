"""Deterministic, read-only attempt trajectory export for one request_id.

Composes existing evidence -- the canonical task card, its audit event
history, the process lifecycle ledger, attempt artifact bundles and recorded
usage -- into one canonical JSON document describing what happened to a
single attempt. Accepted, rejected and failed attempts all use the exact same
top-level schema: evidence that was never recorded is reported as an explicit
``UNKNOWN``/absent marker, never guessed and never defaulted to success or
zero. Nothing here writes to any store; every function is a pure read.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from functools import partial
from pathlib import Path
from typing import Any, Callable

from . import attempt_artifacts, process_event_ledger, task_engine, task_store

SCHEMA_ID = "aiworkhub.attempt_trajectory_export.v1"
UNKNOWN = "UNKNOWN"

# Matches task_engine._validate_accepted_outcome_receipt(repo, card, task_id,
# request_id, receipt) with `repo` pre-bound by the caller (see
# export_attempt_trajectory). Default export binds that live authority;
# callers may pass an explicit opt-in callback. A receipt's own self-digest
# proves internal consistency, never that it was canonically issued.
AcceptedOutcomeAuthority = Callable[
    [dict, str, str, dict], tuple
]

_PROCESS_EVENTS_RELATIVE_PATH = Path(".aiworkhub/runtime/process_logs/process_events.jsonl")
_ATTEMPT_ARTIFACT_BUNDLE_ROOT = Path(".aiworkhub/runtime/process_logs/processes/attempt-artifacts")

_MAX_LEDGER_EVENTS = 500
_MAX_TASK_EVENTS = 200
_MAX_USAGE_ROWS = 200
_MAX_RAW_LEDGER_EVENTS_READ = 10_000

# Redaction is by exact field identity, never by content sniffing, so the same
# named field always redacts the same way and an unlisted field is never
# silently dropped or altered.
_REDACTED_FIELD_NAMES = frozenset({
    "packet", "api_key", "apikey", "secret", "token", "password",
    "authorization", "cookie", "access_token", "refresh_token",
    "private_key", "client_secret", "session_token",
})


class AttemptTrajectoryExportError(ValueError):
    """Base class for a refused attempt trajectory export. Always fail closed."""


class IdentityMismatchError(AttemptTrajectoryExportError):
    """A receipt, artifact bundle or event does not bind the requested identity."""


class DuplicateSequenceError(AttemptTrajectoryExportError):
    """Two ledger events for the same request declare the same explicit sequence."""


class ArtifactDigestMismatchError(AttemptTrajectoryExportError):
    """The attempt artifact bundle failed byte-for-byte re-verification."""


class ContradictoryTerminalDecisionError(AttemptTrajectoryExportError):
    """More than one disjoint terminal outcome is evidenced for one request."""


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def to_canonical_json(result: dict[str, Any]) -> str:
    """Return the exact deterministic JSON bytes an export must reproduce."""
    return _canonical_json_bytes(result).decode("utf-8")


def _is_redacted_key(key: str) -> bool:
    return key.strip().lower() in _REDACTED_FIELD_NAMES


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Replace known-sensitive field values with a deterministic digest.

    Depth is bounded so a hostile or accidentally self-referential-looking
    payload cannot make redaction recurse without limit.
    """
    if _depth > 12:
        return {"redacted": True, "sha256": _digest(value), "reason": "max_depth_exceeded"}
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_redacted_key(key):
                result[key] = {"redacted": True, "sha256": _digest(item)}
            else:
                result[key] = redact(item, _depth=_depth + 1)
        return result
    if isinstance(value, list):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value


def _bounded(items: list[Any], limit: int) -> dict[str, Any]:
    kept = items[:limit]
    omitted = items[limit:]
    return {
        "items": kept,
        "omitted_count": len(omitted),
        "omitted_sha256": _digest(omitted) if omitted else None,
    }


def _is_valid_numeric_field(row: dict[str, Any], field: str) -> bool:
    """True only when the field is present as an actual, non-boolean number.

    A missing/None value, or a boolean masquerading as 0/1, must never be
    treated as valid measured evidence for aggregation.
    """
    value = row.get(field)
    return isinstance(value, (int, float)) and not isinstance(value, bool)

def _extract_request_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("request_id") or payload.get("attempt_id")
    if direct:
        return str(direct)
    identity = payload.get("request_identity")
    if isinstance(identity, dict) and identity.get("request_id"):
        return str(identity["request_id"])
    note = str(payload.get("note") or "")
    prefix = "task_mcp_request:"
    if note.startswith(prefix):
        return note[len(prefix):]
    return ""


def _authenticate_accepted_outcome_receipt(receipt: Any) -> bool:
    """Recompute the receipt's own self-digest; never trust it unverified."""
    if not isinstance(receipt, dict):
        return False
    receipt_id = receipt.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id.startswith("sha256:"):
        return False
    body = {key: value for key, value in receipt.items() if key != "receipt_id"}
    try:
        expected = "sha256:" + _digest(body)
    except (TypeError, ValueError):
        return False
    return receipt_id == expected


def _terminal_review_request_id(card: dict[str, Any]) -> str:
    terminal_review = card.get("terminal_review")
    if not isinstance(terminal_review, dict):
        return ""
    evidence = terminal_review.get("evidence")
    if not isinstance(evidence, dict):
        return ""
    identity = evidence.get("request_identity")
    if not isinstance(identity, dict):
        return ""
    return str(identity.get("request_id") or "")


def _parse_task_event_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("payload")
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _validations_from_artifacts(artifact_section: dict[str, Any]) -> dict[str, Any]:
    payload = artifact_section.get("roles", {}).get("validation")
    if not isinstance(payload, dict):
        return {"state": UNKNOWN, "reason": "no_validation_artifact", "checks": []}
    return {
        "state": "recorded",
        "reason": None,
        "checks": payload.get("checks", []),
        "passed": payload.get("passed", UNKNOWN),
    }


def _reviews_from_evidence(
    artifact_section: dict[str, Any], card: dict[str, Any], request_id: str
) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    review_payload = artifact_section.get("roles", {}).get("review")
    if isinstance(review_payload, dict):
        reviews.append({"source": "attempt_artifact_bundle", "payload": review_payload})
    if _terminal_review_request_id(card) == request_id:
        terminal_review = card.get("terminal_review")
        evidence = terminal_review.get("evidence") if isinstance(terminal_review, dict) else None
        if isinstance(evidence, dict):
            for key in ("quality_review_receipt", "research_result"):
                if key in evidence:
                    reviews.append({
                        "source": f"task_card_terminal_review:{key}",
                        "payload": redact(evidence[key]),
                    })
    return reviews


def build_attempt_trajectory(
    *,
    task_id: str,
    request_id: str,
    repository_id: str = UNKNOWN,
    card: dict[str, Any] | None = None,
    task_events: list[dict[str, Any]] | None = None,
    ledger_events: list[dict[str, Any]] | None = None,
    usage_rows: list[dict[str, Any]] | None = None,
    manager_decision: dict[str, str] | None = None,
    artifact_bundle: dict[str, Any] | None = None,
    accepted_outcome_authority: AcceptedOutcomeAuthority | None = None,
) -> dict[str, Any]:
    """Build the canonical trajectory export from already-fetched evidence.

    This is the pure core: it performs no I/O, so every refusal path
    (identity mismatch, duplicate sequence, artifact tamper, contradictory
    terminal decisions) is exercised deterministically from plain data.
    ``export_attempt_trajectory`` below is the I/O-performing orchestrator
    that gathers this evidence from the canonical stores.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise AttemptTrajectoryExportError("task_id must be a non-empty string")
    if not isinstance(request_id, str) or not request_id.strip():
        raise AttemptTrajectoryExportError("request_id must be a non-empty string")

    card = card or {}
    usage_rows = usage_rows or []

    # --- artifact bundle identity + evidence --------------------------------
    artifact_section: dict[str, Any] = {
        "state": "absent",
        "reason": "no_attempt_artifact_bundle_found",
        "verification": None,
        "roles": {},
    }
    if artifact_bundle is not None:
        verification = artifact_bundle.get("verification")
        if verification is not None and str(verification.get("attempt_id") or "") != request_id:
            raise IdentityMismatchError(
                "identity_mismatch: attempt artifact bundle attempt_id "
                f"{verification.get('attempt_id')!r} does not match request_id {request_id!r}"
            )
        payloads = artifact_bundle.get("payloads") or {}
        artifact_section = {
            "state": "verified" if verification is not None else "unverified",
            "reason": None,
            "verification": verification,
            "roles": {role: redact(payload) for role, payload in sorted(payloads.items())},
        }

    # --- request-scoped process ledger events -------------------------------
    scoped_ledger_events = [
        redact(dict(row))
        for row in (ledger_events or [])
        if str(row.get("request_id") or "") == request_id
    ]
    seen_sequences: dict[int, int] = {}
    for index, row in enumerate(scoped_ledger_events):
        seq = row.get("seq")
        if seq is None or isinstance(seq, bool) or not isinstance(seq, int):
            continue
        if seq in seen_sequences:
            raise DuplicateSequenceError(
                f"duplicate_sequence: ledger events {seen_sequences[seq]} and {index} "
                f"both declare seq={seq} for request_id {request_id!r}"
            )
        seen_sequences[seq] = index
    has_failure_event = any(bool(row.get("terminal_reason")) for row in scoped_ledger_events)

    # --- request-scoped task audit events ------------------------------------
    scoped_task_events: list[dict[str, Any]] = []
    for row in (task_events or []):
        payload = _parse_task_event_payload(row)
        if _extract_request_id(payload) != request_id and _extract_request_id(row) != request_id:
            continue
        scoped_task_events.append({
            "event": str(row.get("event") or ""),
            "runner": str(row.get("runner") or ""),
            "created_at": str(row.get("created_at") or ""),
            "payload": redact(payload),
        })
    # accept_review/reject_review are manager audit events, not terminal
    # authority: accept_review never grants "accepted" by itself (see the
    # canonical-authority gate below), so recording both alongside a
    # non-accepted attempt is not inherently contradictory.
    has_reject_event = any(row["event"] == "reject_review" for row in scoped_task_events)

    # --- accepted-outcome receipt authentication ----------------------------
    accept_evidence = card.get("accept_evidence")
    receipt = (
        accept_evidence.get("accepted_outcome_receipt")
        if isinstance(accept_evidence, dict)
        else None
    )
    accepted_request_id = str(card.get("accepted_request_id") or "")
    receipt_signal = False
    authenticated_receipt: dict[str, Any] | None = None
    if receipt is not None:
        if not _authenticate_accepted_outcome_receipt(receipt):
            raise IdentityMismatchError(
                "identity_mismatch: accepted_outcome_receipt failed self-authentication"
            )
        if receipt.get("schema_id") != task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA:
            raise IdentityMismatchError(
                "identity_mismatch: accepted_outcome_receipt has an unrecognised schema_id"
            )
        receipt_task_id = str(receipt.get("task_id") or "")
        receipt_request_id = str(receipt.get("request_id") or "")
        if receipt_request_id == request_id:
            if receipt_task_id != task_id or accepted_request_id != request_id:
                raise IdentityMismatchError(
                    "identity_mismatch: accepted_outcome_receipt does not bind the "
                    f"requested task_id {task_id!r}/request_id {request_id!r}"
                )
            # Self-authentication only proves the receipt is internally
            # consistent -- a forger can self-compute a valid digest over
            # fabricated content. Only a bound canonical authority (the
            # repository-aware wrapper around task_engine's own
            # sealed-evidence validator) may grant the "accepted" signal.
            # Absent that authority the receipt stays evidenced-but-unproven
            # and the outcome remains UNKNOWN rather than accepted.
            if accepted_outcome_authority is not None:
                authenticated, reason = accepted_outcome_authority(
                    card, task_id, request_id, receipt
                )
                if authenticated is None:
                    raise IdentityMismatchError(
                        "identity_mismatch: accepted_outcome_receipt failed canonical "
                        f"authority validation ({reason})"
                    )
                authenticated_receipt = authenticated
                receipt_signal = True
        elif accepted_request_id == request_id:
            raise IdentityMismatchError(
                "identity_mismatch: task card accepted_request_id "
                f"{accepted_request_id!r} conflicts with the bound receipt's own "
                f"request_id {receipt_request_id!r}"
            )

    # --- resolve one terminal outcome from disjoint signals ------------------
    signals: set[str] = set()
    if receipt_signal:
        signals.add("accepted")
    if has_reject_event:
        signals.add("rejected")
    if has_failure_event:
        signals.add("failed")
    if len(signals) > 1:
        raise ContradictoryTerminalDecisionError(
            f"contradictory_terminal_decision: multiple terminal signals {sorted(signals)!r} "
            f"for request_id {request_id!r}"
        )
    outcome_state = next(iter(signals), "unknown")
    outcome_reason = {
        "accepted": "accepted_outcome_receipt_validated_by_canonical_authority",
        "rejected": "reject_review_event",
        "failed": "process_ledger_terminal_failure_event",
        "unknown": "no_terminal_signal_evidenced_for_request",
    }[outcome_state]

    # --- manager decision (task-level; scoped only when demonstrably tied) --
    manager_decision_section = {
        "decision": UNKNOWN,
        "event": UNKNOWN,
        "created_at": UNKNOWN,
        "request_scoped": False,
    }
    if isinstance(manager_decision, dict) and manager_decision:
        manager_decision_section = {
            "decision": str(manager_decision.get("decision") or UNKNOWN),
            "event": str(manager_decision.get("event") or UNKNOWN),
            "created_at": str(manager_decision.get("created_at") or UNKNOWN),
            "request_scoped": _terminal_review_request_id(card) == request_id,
        }

    # --- usage / cost, matched strictly by request identity ------------------
    matched_usage = [row for row in usage_rows if _extract_request_id(row) == request_id]
    if not matched_usage:
        usage_section: dict[str, Any] = {
            "state": UNKNOWN,
            "reason": "no_usage_evidence_for_request",
            "matched_records": 0,
            "total_tokens": UNKNOWN,
            "cost_usd": UNKNOWN,
        }
    else:
        usage_observed_all = all(bool(row.get("usage_observed")) for row in matched_usage)
        cost_known_all = all(bool(row.get("cost_known")) for row in matched_usage)
        tokens_measured = usage_observed_all and all(
            _is_valid_numeric_field(row, "total_tokens") for row in matched_usage
        )
        cost_measured = cost_known_all and all(
            _is_valid_numeric_field(row, "cost_usd") for row in matched_usage
        )
        usage_section = {
            "state": "measured" if tokens_measured and cost_measured else "partially_unknown",
            "reason": (
                None
                if tokens_measured and cost_measured
                else "one_or_more_matched_records_unmeasured"
            ),
            "matched_records": len(matched_usage),
            "total_tokens": (
                sum(row.get("total_tokens") for row in matched_usage) if tokens_measured else UNKNOWN
            ),
            "cost_usd": (
                round(sum(row.get("cost_usd") for row in matched_usage), 6)
                if cost_measured
                else UNKNOWN
            ),
        }

    bounded_ledger = _bounded(scoped_ledger_events, _MAX_LEDGER_EVENTS)
    bounded_task_events = _bounded(scoped_task_events, _MAX_TASK_EVENTS)
    bounded_usage = _bounded(matched_usage, _MAX_USAGE_ROWS)

    ordered_events = sorted(
        [{"source": "process_ledger", **row} for row in bounded_ledger["items"]]
        + [{"source": "task_event_ledger", **row} for row in bounded_task_events["items"]],
        key=lambda row: (str(row.get("timestamp") or row.get("created_at") or ""), row["source"]),
    )

    return {
        "schema_id": SCHEMA_ID,
        "repository_id": str(repository_id) if repository_id else UNKNOWN,
        "task_id": task_id,
        "request_id": request_id,
        "runner": str(card.get("runner") or UNKNOWN),
        "topic": str(card.get("topic") or UNKNOWN),
        "task_status": str(card.get("status") or UNKNOWN),
        "outcome": {
            "state": outcome_state,
            "reason": outcome_reason,
            "accepted_outcome_receipt": redact(authenticated_receipt) if receipt_signal else None,
        },
        "events": ordered_events,
        "events_bounds": {
            "ledger_events_omitted_count": bounded_ledger["omitted_count"],
            "ledger_events_omitted_sha256": bounded_ledger["omitted_sha256"],
            "task_events_omitted_count": bounded_task_events["omitted_count"],
            "task_events_omitted_sha256": bounded_task_events["omitted_sha256"],
        },
        "artifacts": artifact_section,
        "validations": _validations_from_artifacts(artifact_section),
        "reviews": _reviews_from_evidence(artifact_section, card, request_id),
        "manager_decision": manager_decision_section,
        "usage": {
            **usage_section,
            "rows_omitted_count": bounded_usage["omitted_count"],
            "rows_omitted_sha256": bounded_usage["omitted_sha256"],
        },
    }


def export_attempt_trajectory(
    repo: str | Path,
    *,
    task_id: str,
    request_id: str,
    process_events_path: str | Path | None = None,
    attempt_artifact_bundle_dir: str | Path | None = None,
    manager_decisions: dict[str, dict[str, str]] | None = None,
    usage_rows: list[dict[str, Any]] | None = None,
    accepted_outcome_authority: AcceptedOutcomeAuthority | None = None,
) -> dict[str, Any]:
    """Compose one deterministic, read-only attempt trajectory export.

    Every read is bounded and best-effort: a missing ledger file or a missing
    artifact bundle degrades the corresponding section to UNKNOWN/absent
    rather than raising, but a *present and tampered* artifact bundle or a
    contradictory terminal decision still fails closed via the named errors
    above. Nothing here mutates any store.

    ``manager_decisions`` and ``usage_rows`` are whole-store snapshots
    (``task_store.latest_manager_decisions`` / ``list_usage_events``). A
    caller exporting many trajectories from the same store snapshot -- for
    example a corpus builder iterating over thousands of cards -- should
    fetch each once and pass it in here, rather than let every call re-run
    its own whole-table query; that per-call re-fetch is what turns an
    N-card rebuild into O(N) whole-store scans. Omitting either argument
    preserves the original single-call behavior of fetching it fresh.

    ``accepted_outcome_authority`` is opt-in. When omitted, export binds
    ``task_engine._validate_accepted_outcome_receipt`` so current-byte
    mismatch still refuses accepted. Pass a callback to use a different
    sealed-evidence authority without changing that default.
    """
    repo_path = Path(repo)
    ledger_path = (
        Path(process_events_path)
        if process_events_path is not None
        else repo_path / _PROCESS_EVENTS_RELATIVE_PATH
    )
    bundle_dir = (
        Path(attempt_artifact_bundle_dir)
        if attempt_artifact_bundle_dir is not None
        else repo_path / _ATTEMPT_ARTIFACT_BUNDLE_ROOT / request_id
    )

    try:
        card = task_store.get_task(repo_path, task_id)
    except task_store.TaskStoreError:
        card = None
    try:
        task_events = task_store.get_task_events(repo_path, task_id, limit=500)
    except task_store.TaskStoreError:
        task_events = []
    if usage_rows is None:
        try:
            usage_rows = task_store.list_usage_events(repo_path, limit=10_000)
        except task_store.TaskStoreError:
            usage_rows = []
    if manager_decisions is None:
        try:
            manager_decision = task_store.latest_manager_decisions(repo_path).get(task_id)
        except task_store.TaskStoreError:
            manager_decision = None
    else:
        manager_decision = manager_decisions.get(task_id)

    ledger_events = (
        list(
            itertools.islice(
                process_event_ledger.iter_events(ledger_path), _MAX_RAW_LEDGER_EVENTS_READ
            )
        )
        if ledger_path.is_file()
        else []
    )

    artifact_bundle: dict[str, Any] | None = None
    if bundle_dir.is_dir() and not bundle_dir.is_symlink():
        try:
            verification = attempt_artifacts.verify_json_bundle(bundle_dir)
        except attempt_artifacts.InvalidArtifactError as exc:
            raise ArtifactDigestMismatchError(f"artifact_digest_mismatch: {exc}") from exc
        except attempt_artifacts.InvalidManifestError as exc:
            raise ArtifactDigestMismatchError(f"artifact_manifest_invalid: {exc}") from exc
        manifest_text = (bundle_dir / attempt_artifacts.MANIFEST_FILENAME).read_text(
            encoding="utf-8"
        )
        manifest = attempt_artifacts.parse_manifest_json(manifest_text)
        payloads: dict[str, Any] = {}
        for entry in manifest.artifacts:
            if not entry.present:
                continue
            raw = (bundle_dir / entry.path).read_bytes()
            try:
                payloads[entry.role] = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payloads[entry.role] = {"unparseable_bytes_sha256": hashlib.sha256(raw).hexdigest()}
        artifact_bundle = {"verification": verification, "payloads": payloads}

    if accepted_outcome_authority is None:
        accepted_outcome_authority = partial(
            task_engine._validate_accepted_outcome_receipt, repo_path,
        )
    return build_attempt_trajectory(
        task_id=task_id,
        request_id=request_id,
        repository_id=str(repo_path.resolve()),
        card=card,
        task_events=task_events,
        ledger_events=ledger_events,
        usage_rows=usage_rows,
        manager_decision=manager_decision,
        artifact_bundle=artifact_bundle,
        accepted_outcome_authority=accepted_outcome_authority,
    )


__all__ = [
    "SCHEMA_ID",
    "UNKNOWN",
    "AttemptTrajectoryExportError",
    "IdentityMismatchError",
    "DuplicateSequenceError",
    "ArtifactDigestMismatchError",
    "ContradictoryTerminalDecisionError",
    "redact",
    "to_canonical_json",
    "build_attempt_trajectory",
    "export_attempt_trajectory",
]
