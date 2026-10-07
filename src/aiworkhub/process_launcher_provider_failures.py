"""Provider-output failure classifiers for launched task workers.

Each classifier reads a worker's bounded stdout JSONL and, trusting only
provider-owned machine fields (never model prose), returns a sealed refusal
record: an authentication/quota/rate refusal, a route-level model rejection,
a VS Code LM response timeout, or a VS Code LM balance exhaustion. Moved out
of ``process_launcher`` unchanged; ``process_launcher`` re-imports every name
so existing call sites and ``process_launcher._name`` references still resolve.
The worker's PROJECT_CONTEXT_RECEIPT parser moved here the same way: it reads
the same bounded stdout through the same provider-authenticated envelopes.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import project_context, runtime_adapters, terminal_failure_classification

MAX_RECEIPT_SCAN_BYTES = 2 * 1024 * 1024


def _read_byte_range(path: Path, offset: int, length: int) -> str:
    """Read exactly ``length`` bytes starting at ``offset`` from ``path``,
    O_NOFOLLOW-guarded like ``_safe_tail``. Returns ``""`` on any OS error
    (missing file, symlink, permission) -- fails closed, never raises."""
    if length <= 0:
        return ""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return ""
    try:
        with os.fdopen(fd, "rb") as fh:
            fh.seek(max(0, offset))
            return fh.read(length).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _is_bounded_machine_code(value: str) -> bool:
    """Return whether ``value`` is a short ASCII machine identifier, not prose."""

    if not value or len(value) > 128 or not value[0].isalnum():
        return False
    return all(
        char.isascii() and (char.isalnum() or char in "._:/-")
        for char in value
    )


def _bounded_response_body_machine_code(body_text: object) -> str | None:
    """Extract only a bounded code/type from a small JSON response body."""

    if not isinstance(body_text, str) or not body_text or len(body_text) > 8192:
        return None
    try:
        body = json.loads(body_text)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    candidates: list[object] = []
    nested_error = body.get("error")
    if isinstance(nested_error, dict):
        candidates.extend((nested_error.get("code"), nested_error.get("type")))
    candidates.extend((body.get("code"), body.get("type")))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        code = candidate.strip().lower()
        if _is_bounded_machine_code(code):
            return code
    return None


def _opencode_provider_refusal_from_event(
    event: dict[str, Any],
) -> dict[str, Any] | None:
    """Seal a provider-owned OpenCode APIError without retaining its raw body."""

    if event.get("type") != "error":
        return None
    error = event.get("error")
    if not isinstance(error, dict) or error.get("name") != "APIError":
        return None
    data = error.get("data")
    if not isinstance(data, dict):
        return None
    raw_status = data.get("statusCode")
    if (
        not isinstance(raw_status, int)
        or isinstance(raw_status, bool)
        or not 400 <= raw_status <= 599
    ):
        return None
    machine_code = _bounded_response_body_machine_code(data.get("responseBody"))
    if machine_code is None:
        return None
    typed_spending_limit = machine_code == "personal-team-blocked:spending-limit"
    if not (
        typed_spending_limit
        or runtime_adapters.provider_body_names_cause(machine_code)
    ):
        return None
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1,
        message=f"http_status={raw_status}",
        machine_code=machine_code,
    )
    if outcome.get("outcome") != runtime_adapters.OUTCOME_PROVIDER_REFUSED:
        return None
    return {
        "schema_id": "aiworkhub.provider_launch_failure.v1",
        "reason": str(outcome.get("reason") or "provider_refused"),
        "refusal_kind": str(outcome.get("refusal_kind") or ""),
        "recoverable": bool(outcome.get("recoverable")),
        "http_status": raw_status,
        "error_code": machine_code,
    }


def _provider_auth_failure_from_output(path: Path) -> dict[str, Any] | None:
    """Return a bounded, body-classified provider-refusal record, no secret text.

    Only provider-owned JSONL fields are authoritative. Model prose and raw
    error bodies are deliberately ignored so an agent cannot spoof a launch
    failure or leak credentials into durable task state.

    When a provider-owned ``api_error`` names an HTTP refusal status, its own
    status and machine error code -- never model prose -- are handed to
    ``runtime_adapters.classify_provider_outcome`` so the recorded reason is
    derived from the response body at the boundary where it is still in hand.
    A quota or rate refusal is therefore named as such instead of collapsing
    into ``worker_failed`` downstream (NF-2026-00275), and a bare 401/403 whose
    body distinguishes nothing is recorded as ``cause_not_distinguished`` rather
    than guessed as an authentication failure (NF-2026-00326). The detection was
    widened from 401/403 alone to every refusal status/code so the quota case
    that item one measured is no longer lost before classification.
    """

    try:
        st = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
        return None
    size = int(st.st_size)
    if size <= MAX_RECEIPT_SCAN_BYTES:
        text = _read_byte_range(path, 0, size)
    else:
        half = MAX_RECEIPT_SCAN_BYTES // 2
        text = _read_byte_range(path, 0, half) + "\n" + _read_byte_range(
            path, size - half, half
        )
    # Provider-owned HTTP refusal statuses: authentication (401/403), payment
    # required / balance (402) and rate/quota (429). 5xx is left to the worker
    # path unchanged -- a transient upstream outage is not a launch refusal here.
    # The status set and the machine-code vocabulary are OWNED by
    # ``runtime_adapters`` and reused here so the gate that forwards a body and
    # the classifier that names it can never drift onto different statuses or
    # token forms again (NF-2026-00275 rework: a forwarded 402 that the
    # classifier could not name collapsed back into ``worker_failed``).
    refusal_statuses = runtime_adapters.PROVIDER_REFUSAL_STATUSES
    for raw_line in text.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        opencode_refusal = _opencode_provider_refusal_from_event(event)
        if opencode_refusal is not None:
            return opencode_refusal
        raw_status = event.get("error_status", event.get("api_error_status"))
        status = raw_status if isinstance(raw_status, int) and not isinstance(raw_status, bool) else 0
        raw_error = event.get("error")
        error_code = ""
        if isinstance(raw_error, str):
            candidate_code = raw_error.strip().lower()
            if _is_bounded_machine_code(candidate_code) and (
                candidate_code
                in {"authentication_failed", "unauthorized", "invalid_api_key"}
                or runtime_adapters.provider_body_names_cause(candidate_code)
            ):
                error_code = candidate_code
        subtype = str(event.get("subtype") or "").strip().lower()
        structured_auth_error = error_code in {
            "authentication_failed",
            "unauthorized",
            "invalid_api_key",
        }
        # A provider-owned refusal is present when the status is a refusal code
        # OR the machine error code itself names a quota/rate/credential cause.
        status_refusal = status in refusal_statuses
        code_refusal = structured_auth_error or runtime_adapters.provider_body_names_cause(
            error_code
        )
        structured_result_error = (
            event.get("type") == "result"
            and event.get("is_error") is True
            and str(event.get("terminal_reason") or "").strip().lower() == "api_error"
            and (status_refusal or code_refusal)
        )
        structured_retry_error = (
            event.get("type") == "system"
            and subtype == "api_retry"
            and (status_refusal or code_refusal)
        )
        if not (structured_result_error or structured_retry_error):
            continue
        # Hand the provider's OWN status and machine error code to the classifier
        # -- never the ``result``/message prose, which an agent could author.
        provider_body = f"http_status={status} {error_code}".strip()
        outcome = runtime_adapters.classify_provider_outcome(
            exit_code=1, message=provider_body
        )
        if outcome.get("outcome") != runtime_adapters.OUTCOME_PROVIDER_REFUSED:
            # The launch-time detector ESTABLISHED a provider refusal above -- a
            # refusal status, or a body/machine code that named an auth cause --
            # yet the classifier could not name WHICH cause from the forwarded
            # status and code alone (e.g. a status-less generic ``unauthorized``:
            # http_status=0 carrying no distinguishing token).  Returning the
            # classifier's ``worker_failed`` verdict here would record a provider
            # refusal that the detector matched BECAUSE it named an auth cause as
            # a worker crash -- exactly the NF-2026-00275 invariant this card
            # exists to hold, and specifically the dead-credential case where an
            # operator must re-authenticate and would instead be told their code
            # failed.  The honest verdict is that a refusal occurred whose cause
            # the response did not distinguish, so emit ``cause_not_distinguished``
            # -- the same reason the classifier uses for a bare 401 -- rather than
            # collapse back onto the worker path.  ``structured_auth_error`` and
            # the classifier's cause vocabulary are two lists that legitimately
            # disagree about a status-less ``unauthorized`` (the detector treats
            # it as an auth signal; the classifier excludes it because it names
            # nothing); this branch reconciles that disagreement honestly instead
            # of letting a matched refusal fall through to ``worker_failed``.
            return {
                "schema_id": "aiworkhub.provider_launch_failure.v1",
                "reason": (
                    f"provider_refused:http_status={status}"
                    ":cause_not_distinguished_by_response"
                ),
                "refusal_kind": runtime_adapters.REFUSAL_CAUSE_NOT_DISTINGUISHED,
                "recoverable": False,
                "http_status": status,
            }
        return {
            "schema_id": "aiworkhub.provider_launch_failure.v1",
            "reason": str(outcome.get("reason") or "provider_refused"),
            "refusal_kind": str(outcome.get("refusal_kind") or ""),
            "recoverable": bool(outcome.get("recoverable")),
            "http_status": status,
            "error_code": error_code,
            "session_id": str(
                event.get("session_id")
                or event.get("sessionId")
                or event.get("provider_session_id")
                or ""
            ),
        }
    return None


# Provider-owned envelope shapes that name the MODEL as the thing that does not
# exist for this account.  Kept exact: a bare 400 is usually the caller's
# payload, so a status alone never qualifies.
_MODEL_REJECTION_STATUSES: frozenset[int] = frozenset({400, 404})
_MODEL_REJECTION_ERROR_TYPES: frozenset[str] = frozenset({
    "invalid_request_error", "not_found_error", "invalid_model",
})
_MODEL_REJECTION_ERROR_CODES: frozenset[str] = frozenset({
    "model_not_found", "model_not_supported",
    "unknown_model", "model_not_available",
})


def _provider_model_rejection_from_output(
    path: Path, requested_model: str
) -> dict[str, Any] | None:
    """Return a sealed record when the provider refused the ROUTE, not the work.

    NF-2026-00655 measured 103 launches on ``codex_cli``/``gpt-5.4`` returning
    0 accepts and 0 rejects, 13 of them byte-identical: 725 bytes of stdout and
    143 of stderr carrying

        {"type":"error","status":400,"error":{"type":"invalid_request_error",
         "message":"The 'gpt-5.4' model is not supported when using Codex with
         a ChatGPT account."}}

    ``_provider_auth_failure_from_output`` does not see it -- the envelope is a
    top-level ``error`` object rather than a ``result``/``system`` event, and
    400 is not a refusal status -- so it collapsed into
    ``worker_failed:supervisor_state=exited:exit_code=1`` and killed the CARD
    while leaving the route ready for the next 102 launches.

    Two facts must both hold before this is called a route failure, and
    together they make it unspoofable by model prose: the envelope must be the
    provider's own error object with a model-shaped error type or code, and its
    message must name the exact model THIS launch pinned.  A worker that echoes
    someone else's error text cannot satisfy the second, and a genuine bad
    request about anything other than the model cannot satisfy the first.
    """

    model = str(requested_model or "").strip()
    if not model:
        return None
    try:
        st = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
        return None
    size = int(st.st_size)
    if size <= MAX_RECEIPT_SCAN_BYTES:
        text = _read_byte_range(path, 0, size)
    else:
        half = MAX_RECEIPT_SCAN_BYTES // 2
        text = _read_byte_range(path, 0, half) + "\n" + _read_byte_range(
            path, size - half, half
        )
    for raw_line in text.splitlines():
        try:
            outer = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(outer, dict):
            continue
        # THE ENVELOPE IS NESTED, AND READING ONLY THE OUTER LINE MATCHED
        # NOTHING.  The Codex CLI forwards the upstream provider's own error
        # body as a QUOTED JSON STRING inside ``message``, so the outer line
        # carries no ``status`` and no ``error`` object at all.  Replayed
        # against the 13 byte-identical 725-byte ``gpt-5.4`` logs this seal was
        # written for, the outer-line-only scan returned ``None`` on every one
        # of them: ``status: 400``, ``error.type: invalid_request_error`` and
        # the message naming the pinned model all live in the nested body.
        #
        # The anti-forgery anchor is unchanged.  A candidate must still be a
        # provider ``error`` envelope with a model-shaped type or code, and its
        # message must still name the exact model THIS launch pinned, so worker
        # prose still cannot mint a route failure.  The unwrapping is shared
        # with ``terminal_failure_classification`` so the boundary detector and
        # the disposition classifier can never disagree about what the provider
        # sent (R4/NF-2026-00646).
        #
        # THE MESSAGE IS UNWRAPPED ONLY FOR A PROVIDER-OWNED ENVELOPE.  An
        # ``assistant`` line's ``message`` is the model's own content, so
        # unwrapping one would let a worker mint this seal by printing the body
        # -- ``test_worker_prose_cannot_forge_a_route_failure`` states exactly
        # that case, and an unrestricted unwrap regressed it.
        nested: tuple[dict[str, Any], ...] = ()
        if (
            str(outer.get("type") or "").strip().lower()
            in terminal_failure_classification.PROVIDER_OWNED_MESSAGE_TYPES
        ):
            nested = tuple(
                terminal_failure_classification.embedded_provider_objects(
                    outer.get("message")
                )
            )
        for event in (outer, *nested):
            if str(event.get("type") or "").strip().lower() != "error":
                continue
            body = event.get("error")
            if not isinstance(body, dict):
                continue
            error_type = str(body.get("type") or "").strip().lower()
            error_code = str(body.get("code") or "").strip().lower()
            raw_status = event.get("status", event.get("error_status"))
            status = (
                raw_status
                if isinstance(raw_status, int) and not isinstance(raw_status, bool)
                else 0
            )
            # NF-2026-01374: OpenCode emits a provider-owned top-level error
            # envelope whose error.type is exactly ``provider.no-route`` and
            # which carries no HTTP status.  Accept that one statusless case;
            # every other error type keeps the existing {400,404} status gate
            # and the existing model-shaped error-type/code sets entirely
            # unchanged, and the message must still name the exact model THIS
            # launch pinned so worker prose cannot mint it.  A carried
            # non-integer status is retained as unsealed evidence, never erased
            # into a statusless seal.
            no_route = error_type == "provider.no-route" and (
                (raw_status is None and event.get("error_status") is None)
                or status in _MODEL_REJECTION_STATUSES
            )
            if status not in _MODEL_REJECTION_STATUSES and not no_route:
                continue
            if (
                error_type not in _MODEL_REJECTION_ERROR_TYPES
                and error_code not in _MODEL_REJECTION_ERROR_CODES
                and not no_route
            ):
                continue
            message = str(body.get("message") or "")
            names_model = (
                re.search(
                    r"(?<![\w./-])" + re.escape(model) + r"(?![\w./-])",
                    message,
                )
                is not None
                if no_route
                else error_code in _MODEL_REJECTION_ERROR_CODES or model in message
            )
            if not names_model:
                continue
            sealed = {
                "schema_id": "aiworkhub.provider_route_error.v1",
                "owner": "provider",
                "sealed": True,
                "code": (
                    "model_not_available"
                    if no_route
                    else (
                        error_code
                        if error_code in _MODEL_REJECTION_ERROR_CODES
                        else "model_not_supported"
                    )
                ),
                "http_status": status,
                "model": model,
                "detail": message[:300],
            }
            return {
                "schema_id": "aiworkhub.provider_launch_failure.v1",
                "reason": f"provider_route_model_unavailable:model={model}",
                "refusal_kind": "model_not_found",
                "recoverable": False,
                "http_status": status,
                "error_code": str(sealed["code"]),
                "session_id": "",
                "provider_error": sealed,
            }
    return None


def _provider_timeout_failure_from_output(path: Path) -> dict[str, Any] | None:
    """Return exact structured VS Code LM timeout evidence.

    The editor bridge owns its response deadline.  It may exit immediately
    before the outer supervisor's matching deadline, leaving the supervisor
    with the otherwise ambiguous pair ``state=exited, exit_code=1``.  Trust
    only the bridge's machine-generated result envelope; never classify model
    prose containing the word ``timeout`` as lifecycle evidence.
    """

    try:
        st = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
        return None
    size = int(st.st_size)
    if size <= MAX_RECEIPT_SCAN_BYTES:
        text = _read_byte_range(path, 0, size)
    else:
        half = MAX_RECEIPT_SCAN_BYTES // 2
        text = _read_byte_range(path, 0, half) + "\n" + _read_byte_range(
            path, size - half, half
        )
    for raw_line in text.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if (
            event.get("type") == "result"
            and event.get("is_error") is True
            and str(event.get("subtype") or "").strip().lower() == "error"
            and str(event.get("error") or "").strip() == "vscode_lm_response_timeout"
        ):
            return {
                "schema_id": "aiworkhub.provider_timeout_failure.v1",
                "reason": "vscode_lm_response_timeout",
            }
    return None


# ``vscode_lm_worker``'s own structured provider refusal (NF-2026-01036), named
# rather than imported so this module keeps its import graph unchanged.
_VSCODE_LM_BALANCE_EXHAUSTED = "vscode_lm_provider_balance_exhausted"
_VSCODE_LM_PROVIDER_ERROR_SOURCE = "vscode_lm_extension_response"
_VSCODE_LM_BALANCE_CODES: frozenset[str] = frozenset({
    "insufficient_balance", "quota_exhausted",
})


def _vscode_lm_balance_failure_from_output(path: Path) -> dict[str, Any] | None:
    """Seal a VS Code LM credit/balance refusal the worker read from the host.

    Trusted only from the worker's machine-generated terminal envelope --
    ``type=result``, ``is_error``, the exact ``vscode_lm_provider_balance_
    exhausted`` error constant, and a structured ``provider_error`` whose
    source is the extension host's response field.  No prose anywhere in the
    stream is scanned for balance/quota wording, so a model that prints the
    provider's credit-limit sentence cannot mint this seal.  Every sealed field
    is re-minted here from a closed vocabulary or a parsed, bounded value.
    """

    try:
        st = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
        return None
    size = int(st.st_size)
    if size <= MAX_RECEIPT_SCAN_BYTES:
        text = _read_byte_range(path, 0, size)
    else:
        half = MAX_RECEIPT_SCAN_BYTES // 2
        text = _read_byte_range(path, 0, half) + "\n" + _read_byte_range(
            path, size - half, half
        )
    for raw_line in text.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if not (
            event.get("type") == "result"
            and event.get("is_error") is True
            and str(event.get("subtype") or "").strip().lower() == "error"
            and str(event.get("error") or "").strip() == _VSCODE_LM_BALANCE_EXHAUSTED
        ):
            continue
        body = event.get("provider_error")
        if not isinstance(body, dict):
            continue
        if (
            body.get("owner") != "provider"
            or body.get("sealed") is not True
            or body.get("source") != _VSCODE_LM_PROVIDER_ERROR_SOURCE
        ):
            continue
        code = str(body.get("code") or "").strip().lower()
        if code not in _VSCODE_LM_BALANCE_CODES:
            continue
        reset_at = ""
        raw_reset = body.get("reset_at")
        if isinstance(raw_reset, str) and 0 < len(raw_reset) <= 64:
            try:
                parsed = datetime.fromisoformat(raw_reset.strip().replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None and parsed.tzinfo is not None:
                reset_at = parsed.astimezone(timezone.utc).isoformat()
        sealed: dict[str, Any] = {
            "schema_id": "aiworkhub.provider_route_error.v1",
            "owner": "provider",
            "sealed": True,
            "source": _VSCODE_LM_PROVIDER_ERROR_SOURCE,
            "code": next(item for item in _VSCODE_LM_BALANCE_CODES if item == code),
            "http_status": 402,
            "detail": str(body.get("detail") or "")[:300],
        }
        if reset_at:
            sealed["reset_at"] = reset_at
            if body.get("reset_timezone_assumed") == "UTC-12:00":
                sealed["reset_timezone_assumed"] = "UTC-12:00"
        return {
            "schema_id": "aiworkhub.provider_launch_failure.v1",
            "reason": (
                "provider_refused_balance_exhausted_recoverable_after_reported_window"
                if reset_at
                else "provider_refused_balance_exhausted"
            ),
            "refusal_kind": "balance_exhausted",
            "recoverable": bool(reset_at),
            "http_status": 402,
            "error_code": str(sealed["code"]),
            "session_id": "",
            "provider_error": sealed,
        }
    return None


def _receipt_text_candidates(raw_line: str) -> list[str]:
    """Return only provider-authenticated assistant-output payloads.

    The worker prompt contains the complete acknowledgement template.  Raw
    stdout therefore has no acknowledgement authority: a provider that echoes
    its input would otherwise replay that template verbatim.  Supported JSONL
    adapters bind assistant text to a typed output envelope at the
    process/adapter boundary.
    """
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, dict):
        return []

    candidates: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    item = event.get("item")
    if (
        isinstance(item, dict)
        and str(item.get("type") or "") == "agent_message"
    ):
        add(item.get("text"))  # Codex JSONL assistant output

    data = event.get("data")
    if (
        isinstance(data, dict)
        and str(event.get("type") or "") == "assistant.message"
    ):
        add(data.get("content"))  # DeepSeek/Copilot assistant output

    message = event.get("message")
    if (
        isinstance(message, dict)
        and str(message.get("role") or "") == "assistant"
    ):
        content = message.get("content")
        if isinstance(content, list):
            for block in content[:32]:
                if isinstance(block, dict):
                    add(block.get("text"))  # Claude stream-json
        else:
            add(content)
    return candidates[:40]


def _project_context_receipt_from_output(
    path: Path,
    *,
    expected_bundle_sha256: str = "",
    expected_request_id: str = "",
) -> dict[str, Any]:
    result = {
        "schema_id": project_context.RECEIPT_SCHEMA_ID,
        "acknowledged": False,
        "bundle_sha256": "",
        "prompt_sha256": "",
        "request_id": "",
        "section_count": 0,
        "reason": "receipt_not_found",
    }
    # A receipt is normally emitted near the beginning of a streaming JSONL
    # run.  Reading only the final 16 KiB loses it as soon as tool results make
    # the stream larger (the 0.6.11 live canary produced 135 KiB).  Scan a
    # bounded whole log; for unusually large logs keep symmetric head/tail
    # windows so early receipts and late adapter summaries remain visible.
    try:
        size = path.stat().st_size if path.is_file() and not path.is_symlink() else 0
    except OSError:
        size = 0
    if size <= MAX_RECEIPT_SCAN_BYTES:
        text = _read_byte_range(path, 0, size)
    else:
        half = MAX_RECEIPT_SCAN_BYTES // 2
        text = _read_byte_range(path, 0, half) + "\n" + _read_byte_range(path, size - half, half)
    prefix = "PROJECT_CONTEXT_RECEIPT:"
    expected = expected_bundle_sha256.strip().lower()
    expected_request = expected_request_id.strip()
    for line in reversed(text.splitlines()):
        for candidate in _receipt_text_candidates(line):
            marker = candidate.rfind(prefix)
            if marker >= 0:
                candidate = candidate[marker + len(prefix):].strip()
            try:
                value, _end = json.JSONDecoder().raw_decode(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict) or value.get("schema_id") != project_context.RECEIPT_SCHEMA_ID:
                continue
            bundle_sha = str(value.get("bundle_sha256") or "").strip().lower()
            receipt_request = str(value.get("request_id") or "").strip()
            section_raw = value.get("section_count") or 0
            section_count = int(section_raw) if str(section_raw).isdigit() else 0
            valid_sha = len(bundle_sha) == 64 and all(ch in "0123456789abcdef" for ch in bundle_sha)
            matches = not expected or bundle_sha == expected
            request_matches = not expected_request or receipt_request == expected_request
            acknowledged = (
                value.get("acknowledged") is True
                and valid_sha
                and matches
                and request_matches
                and section_count > 0
            )
            reason = str(value.get("reason") or "")[:160]
            if not valid_sha:
                reason = "receipt_bundle_sha256_invalid"
            elif not matches:
                reason = "receipt_bundle_sha256_mismatch"
            elif not request_matches:
                reason = "receipt_request_id_mismatch"
            elif section_count <= 0:
                reason = "receipt_section_count_invalid"
            return {
                "schema_id": project_context.RECEIPT_SCHEMA_ID,
                "acknowledged": acknowledged,
                "bundle_sha256": bundle_sha[:80],
                "prompt_sha256": str(value.get("prompt_sha256") or "")[:80],
                "request_id": receipt_request[:160],
                "section_count": section_count,
                "reason": reason,
            }
    return result


def _coordinator_bound_context_ack(
    context_ack: dict[str, Any],
    worker_mcp_gate: Mapping[str, Any] | None,
    metadata: Mapping[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """Report the gate's coordinator-bound acknowledgement (NF-2026-01395).

    A worker told not to print the receipt (claude_cli) leaves none in stdout,
    so the stdout scan says ``receipt_not_found`` although the worker MCP gate
    already acknowledged the injected bundle from coordinator facts.  The
    evidence then reports that server-derived acknowledgement.
    """
    source = (worker_mcp_gate or {}).get("injected_context_acknowledgement_source")
    # Only an absent receipt is upgraded; a found but unverifiable receipt keeps
    # its specific failure reason and observed digest.
    if context_ack.get("reason") != "receipt_not_found" or source != "coordinator_prompt_binding":
        return context_ack
    context = metadata.get("project_context")
    delivery = metadata.get("project_context_delivery")
    bundle_sha = str(context.get("bundle_sha256") or "") if isinstance(context, Mapping) else ""
    section_count = delivery.get("section_count") if isinstance(delivery, Mapping) else None
    # The receipt path's invariants: a 64-hex digest and a positive section count.
    if not (
        len(bundle_sha) == 64
        and all(ch in "0123456789abcdef" for ch in bundle_sha)
        and type(section_count) is int
        and section_count > 0
    ):
        return context_ack
    return {
        **context_ack,
        "acknowledged": True,
        "bundle_sha256": bundle_sha,
        "request_id": request_id[:160],
        "section_count": section_count,
        "reason": "coordinator_prompt_binding",
    }
