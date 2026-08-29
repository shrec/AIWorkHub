"""Strict, immutable contracts for untrusted provider responses."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import NoReturn


class ProviderResponseContractError(ValueError):
    """A stable, machine-readable provider response validation failure."""

    def __init__(self, *, reason: str, category: str) -> None:
        self.reason = reason
        self.category = category
        super().__init__(f"{category}:{reason}")


class ProviderEventType(str, Enum):
    """Provider-independent event discriminators."""

    ASSISTANT_TEXT = "ASSISTANT_TEXT"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


_KNOWN_EVENT_TYPES = {
    "assistant_text": ProviderEventType.ASSISTANT_TEXT,
    "tool_call": ProviderEventType.TOOL_CALL,
    "tool_result": ProviderEventType.TOOL_RESULT,
    "error": ProviderEventType.ERROR,
}


def _invalid(category: str, reason: str) -> NoReturn:
    raise ProviderResponseContractError(category=category, reason=reason)


def _freeze_json(value: object, *, category: str, reason: str) -> object:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError:
            _invalid(category, reason)
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _invalid(category, reason)
        return value
    if isinstance(value, Mapping):
        detached: dict[str, object] = {}
        for key, member in value.items():
            if not isinstance(key, str):
                _invalid(category, reason)
            detached[key] = _freeze_json(member, category=category, reason=reason)
        return MappingProxyType(detached)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(member, category=category, reason=reason)
            for member in value
        )
    _invalid(category, reason)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(member) for key, member in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(member) for member in value]
    return value


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    """One detached provider event, including its exact provider discriminator."""

    type: ProviderEventType
    raw_type: str
    data: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """A fully validated response with a stable canonical representation."""

    events: tuple[ProviderEvent, ...]
    capabilities: tuple[str, ...]
    verified: bool
    canonical_bytes: bytes
    digest: str


def _normalize_capabilities(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(
        value, Iterable
    ):
        _invalid("capabilities", "invalid_capabilities")
    try:
        members = tuple(value)
    except Exception:
        _invalid("capabilities", "invalid_capabilities")
    if any(
        not isinstance(member, str) or not member or not member.strip()
        for member in members
    ):
        _invalid("capabilities", "invalid_capabilities")
    try:
        for member in members:
            member.encode("utf-8")
    except UnicodeError:
        _invalid("capabilities", "invalid_capabilities")
    return tuple(sorted(set(members)))


def _normalize_event(value: object) -> ProviderEvent:
    if not isinstance(value, Mapping) or set(value) != {"type", "data"}:
        _invalid("event", "invalid_event")
    raw_type = value["type"]
    if not isinstance(raw_type, str) or not raw_type or not raw_type.strip():
        _invalid("event_type", "invalid_event_type")
    try:
        raw_type.encode("utf-8")
    except UnicodeError:
        _invalid("event_type", "invalid_event_type")
    raw_data = value["data"]
    if not isinstance(raw_data, Mapping):
        _invalid("event_data", "invalid_event_data")
    frozen_data = _freeze_json(
        raw_data, category="event_data", reason="invalid_event_data"
    )
    if not isinstance(frozen_data, Mapping):  # pragma: no cover - guarded above
        _invalid("event_data", "invalid_event_data")
    return ProviderEvent(
        type=_KNOWN_EVENT_TYPES.get(raw_type, ProviderEventType.UNKNOWN),
        raw_type=raw_type,
        data=frozen_data,
    )


def _normalize_provider_response(payload: object) -> ProviderResponse:
    if not isinstance(payload, Mapping) or set(payload) != {
        "events",
        "capabilities",
        "verified",
    }:
        _invalid("response", "invalid_response")

    raw_events = payload["events"]
    if (
        isinstance(raw_events, (str, bytes, bytearray, Mapping))
        or not isinstance(raw_events, Iterable)
    ):
        _invalid("events", "invalid_events")
    try:
        events = tuple(_normalize_event(event) for event in raw_events)
    except ProviderResponseContractError:
        raise
    except Exception:
        _invalid("events", "invalid_events")
    if not events:
        _invalid("events", "invalid_events")

    capabilities = _normalize_capabilities(payload["capabilities"])
    verified = payload["verified"]
    if type(verified) is not bool:
        _invalid("verified", "invalid_verified")

    canonical_value = {
        "capabilities": list(capabilities),
        "events": [
            {
                "data": _thaw_json(event.data),
                "raw_type": event.raw_type,
                "type": event.type.value,
            }
            for event in events
        ],
        "verified": verified,
    }
    canonical_bytes = json.dumps(
        canonical_value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ProviderResponse(
        events=events,
        capabilities=capabilities,
        verified=verified,
        canonical_bytes=canonical_bytes,
        digest=hashlib.sha256(canonical_bytes).hexdigest(),
    )


def normalize_provider_response(payload: object) -> ProviderResponse:
    """Validate and detach an untrusted provider response, failing closed."""

    try:
        return _normalize_provider_response(payload)
    except ProviderResponseContractError:
        raise
    except Exception as exc:
        raise ProviderResponseContractError(
            category="response", reason="invalid_response"
        ) from exc


__all__ = [
    "ProviderEvent",
    "ProviderEventType",
    "ProviderResponse",
    "ProviderResponseContractError",
    "normalize_provider_response",
]
