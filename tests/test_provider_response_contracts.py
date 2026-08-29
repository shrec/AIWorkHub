from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator

import pytest

from aiworkhub.provider_response_contracts import (
    ProviderEventType,
    ProviderResponseContractError,
    normalize_provider_response,
)


def response(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "events": [{"type": "assistant_text", "data": {"text": "hello"}}],
        "capabilities": ["chat", "tools"],
        "verified": True,
    }
    value.update(overrides)
    return value


def test_known_event_is_normalized_without_flattening() -> None:
    normalized = normalize_provider_response(response())

    assert normalized.events[0].type is ProviderEventType.ASSISTANT_TEXT
    assert normalized.events[0].raw_type == "assistant_text"
    assert normalized.events[0].data["text"] == "hello"


def test_unknown_event_preserves_detached_recursive_raw_value() -> None:
    raw = {"nested": [{"value": 1}], "tags": ["a", "b"]}
    normalized = normalize_provider_response(
        response(events=[{"type": "future.delta", "data": raw}])
    )

    event = normalized.events[0]
    assert event.type is ProviderEventType.UNKNOWN
    assert event.raw_type == "future.delta"
    assert event.data["nested"][0]["value"] == 1
    raw["nested"][0]["value"] = 99
    raw["tags"].append("c")
    assert event.data["nested"][0]["value"] == 1
    assert event.data["tags"] == ("a", "b")
    with pytest.raises(TypeError):
        event.data["new"] = "mutation"
    with pytest.raises(TypeError):
        event.data["nested"][0]["value"] = 2
    with pytest.raises(AttributeError):
        event.data["tags"].append("mutation")


@pytest.mark.parametrize(
    "capabilities",
    ["chat", b"chat", bytearray(b"chat"), {"chat": True}, [""], ["  "], [1]],
)
def test_capabilities_fail_closed(capabilities: object) -> None:
    with pytest.raises(ProviderResponseContractError) as caught:
        normalize_provider_response(response(capabilities=capabilities))

    assert caught.value.category == "capabilities"
    assert caught.value.reason == "invalid_capabilities"


def test_capabilities_accept_non_string_iterable_and_are_canonicalized() -> None:
    normalized = normalize_provider_response(
        response(capabilities=(item for item in ["tools", "chat", "tools"]))
    )

    assert normalized.capabilities == ("chat", "tools")


@pytest.mark.parametrize("verified", [0, 1, "true", None, [], object()])
def test_verified_requires_an_actual_bool(verified: object) -> None:
    with pytest.raises(ProviderResponseContractError) as caught:
        normalize_provider_response(response(verified=verified))

    assert caught.value.category == "verified"
    assert caught.value.reason == "invalid_verified"


def test_canonical_serialization_and_digest_are_deterministic() -> None:
    left = response(
        events=[{"data": {"z": 2, "a": 1}, "type": "tool_call"}],
        capabilities={"tools", "chat"},
    )
    right = response(
        capabilities=["chat", "tools"],
        events=[{"type": "tool_call", "data": {"a": 1, "z": 2}}],
    )

    first = normalize_provider_response(left)
    second = normalize_provider_response(right)
    assert first.canonical_bytes == second.canonical_bytes
    assert first.digest == second.digest
    assert first.digest == hashlib.sha256(first.canonical_bytes).hexdigest()
    assert json.loads(first.canonical_bytes)["events"][0]["data"] == {
        "a": 1,
        "z": 2,
    }


def test_throwing_iterable_and_non_utf8_string_raise_typed_error() -> None:
    class ThrowingIterable:
        def __iter__(self) -> Iterator[object]:
            raise OSError("provider iterator failed")

    with pytest.raises(ProviderResponseContractError):
        normalize_provider_response(response(capabilities=ThrowingIterable()))
    with pytest.raises(ProviderResponseContractError):
        normalize_provider_response(
            response(events=[{"type": "future", "data": {"text": "\ud800"}}])
        )


@pytest.mark.parametrize(
    ("payload", "category", "reason"),
    [
        (None, "response", "invalid_response"),
        ([], "response", "invalid_response"),
        (
            {"events": [], "capabilities": [], "verified": True},
            "events",
            "invalid_events",
        ),
        (response(events=["bad"]), "event", "invalid_event"),
        (
            response(events=[{"type": "x", "data": []}]),
            "event_data",
            "invalid_event_data",
        ),
        (
            response(events=[{"type": 7, "data": {}}]),
            "event_type",
            "invalid_event_type",
        ),
    ],
)
def test_malformed_inputs_raise_one_typed_contract_error(
    payload: object, category: str, reason: str
) -> None:
    with pytest.raises(ProviderResponseContractError) as caught:
        normalize_provider_response(payload)

    assert caught.value.category == category
    assert caught.value.reason == reason
