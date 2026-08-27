"""Claude manager-chat capture for the repository Context Graph.

The Codex manager transcript is projected by
:mod:`aiworkhub.manager_transcript_capture`, which consumes completed
``item/completed`` messages from a verified Codex App Server mux.  Claude Code
has no such mux; it persists an append-only JSONL session transcript instead.
This module is the Claude equivalent of that adapter: it projects only
completed final user/assistant messages from Claude transcript records into the
Context Graph, mirroring the Codex adapter's ``final_items`` contract.  It never
records reasoning, tool calls, tool results, sidechain (sub-agent) turns or
meta bookkeeping records, and it fails closed on any malformed record.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import capture_adapters, context_graph, feature_settings

_PROVIDER = "claude"
_EVENT_TYPE = "chat_message"
MAX_CAPTURE_CONTENT_BYTES = context_graph.MAX_CONTENT_BYTES


@dataclass(frozen=True)
class ClaudeChatMessage:
    session_id: str
    item_id: str
    role: str
    content: str
    occurred_at: str


def _token(value: Any, maximum: int = 256) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or "\x00" in text or len(text.encode("utf-8")) > maximum:
        return ""
    return text


def _message_text(content: Any) -> str:
    """Join the completed text blocks of a Claude message; ignore tool/other parts."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part).strip()


def extract_claude_completed_message(record: dict[str, Any]) -> ClaudeChatMessage | None:
    """Project one completed final user/assistant message; ignore every other record."""
    if not isinstance(record, dict):
        return None
    if record.get("isSidechain") or record.get("isMeta"):
        return None
    record_type = _token(record.get("type"), 64)
    if record_type not in ("user", "assistant"):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    role = _token(message.get("role"), 64)
    if role not in ("user", "assistant"):
        return None
    content = _message_text(message.get("content"))
    session_id = _token(record.get("sessionId"))
    item_id = _token(record.get("uuid"))
    if not session_id or not item_id or not content or "\x00" in content:
        return None
    if len(content.encode("utf-8")) > MAX_CAPTURE_CONTENT_BYTES:
        return None
    occurred_at = _token(record.get("timestamp"), 80)
    return ClaudeChatMessage(
        session_id=session_id,
        item_id=item_id,
        role=role,
        content=content,
        occurred_at=occurred_at,
    )


def capture_claude_message(repo: Path | str, message: ClaudeChatMessage) -> dict[str, Any]:
    """Append one completed Claude message to the Context Graph as a manager event."""
    digest = hashlib.sha256(
        f"{message.session_id}\0{message.item_id}".encode()
    ).hexdigest()
    return context_graph.append_event(
        repo,
        thread_id=message.session_id,
        session_id=message.session_id,
        provider=_PROVIDER,
        role=message.role,
        event_type=_EVENT_TYPE,
        content=message.content,
        source_ref=f"claude_transcript:{message.item_id}",
        idempotency_key=f"claude-item-{digest}",
        metadata={
            "manager_only": True,
            "session_id": message.session_id,
            "item_id": message.item_id,
        },
        occurred_at=message.occurred_at,
    )


# Bound the named-rejection detail so an adversarial transcript cannot grow the
# result without limit; the ``rejected`` count stays exact regardless.
MAX_REJECTED_DETAIL = 64


def capture_claude_records(
    repo: Path | str, records: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Project a stream of Claude transcript records; return bounded capture counts.

    ``skipped`` counts records that are not projectable manager messages (tool
    output, sidechain, meta) -- normal for any transcript.  ``rejected`` counts
    completed messages whose ``sessionId`` violates the Context Graph's strict
    token rule; each is skipped by name in ``rejected_records`` and the rest of
    the batch is still captured, so one malformed id can no longer raise
    mid-batch and discard every good record alongside it.  A visible ``rejected``
    count lets an operator tell a quiet repository from a rejected batch.
    """
    counts = {"seen": 0, "captured": 0, "idempotent": 0, "skipped": 0, "rejected": 0}
    rejected_records: list[dict[str, str]] = []
    if not feature_settings.enabled(repo, "context_graph"):
        return {
            **feature_settings.disabled_result("context_graph"),
            "provider": _PROVIDER,
            "rejected_records": rejected_records,
            **counts,
        }

    descriptor = capture_adapters.adapter(_PROVIDER) or {}
    if not capture_adapters.is_configured(_PROVIDER):
        # No capture path for this provider; report the state, capture nothing.
        return {
            "ok": True,
            "provider": _PROVIDER,
            "state": descriptor.get("state", "not_configured"),
            "rejected_records": rejected_records,
            **counts,
        }
    for record in records:
        counts["seen"] += 1
        message = extract_claude_completed_message(record)
        if message is None:
            counts["skipped"] += 1
            continue
        if not context_graph.is_valid_token(message.session_id):
            # Fail closed on the charset case: the strict token rule
            # append_event applies would raise and abort the whole batch, so
            # skip this record by name and keep capturing the rest.
            counts["rejected"] += 1
            if len(rejected_records) < MAX_REJECTED_DETAIL:
                rejected_records.append(
                    {
                        "item_id": message.item_id,
                        "session_id": message.session_id,
                        "reason": "invalid_session_id",
                    }
                )
            continue
        result = capture_claude_message(repo, message)
        if result.get("idempotent"):
            counts["idempotent"] += 1
        elif result.get("ok") and result.get("event_id"):
            counts["captured"] += 1
        else:
            counts["skipped"] += 1
    return {
        "ok": True,
        "provider": _PROVIDER,
        "state": descriptor.get("state", ""),
        "rejected_records": rejected_records,
        **counts,
    }


__all__ = [
    "ClaudeChatMessage",
    "capture_claude_message",
    "capture_claude_records",
    "extract_claude_completed_message",
]
