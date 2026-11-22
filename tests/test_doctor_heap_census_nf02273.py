"""NF-2026-02273: opt-in bounded live-heap census in doctor server_process."""

from __future__ import annotations

import inspect
import json
import sys
from typing import Any

import pytest

from aiworkhub import doctor

FIVE_MB = 5 * 1024 * 1024
CONTENT_MARKER = "NF02273-CONTENT-MARKER-"
PAYLOAD_COUNT = 50
PAYLOAD_SIZE = 200 * 1024

# Module globals so the census reaches them through this module's namespace dict.
_BIG_BYTES: bytes | None = None
_MARKED_TEXT: str | None = None
_PLANTED: list[bytes] | None = None


def _census(**kwargs: Any) -> dict[str, Any]:
    reply = doctor.server_process(heap_census=True, **kwargs)
    census = reply["heap_census"]
    assert "error" not in census, census
    return census


def test_default_server_process_shape_is_unchanged() -> None:
    default = doctor.server_process()
    explicit = doctor.server_process(heap_census=False)
    assert "heap_census" not in default
    assert set(default) == set(explicit)
    assert {
        "pid",
        "python_allocated_blocks",
        "runtime_generation",
        "superseded",
    } <= set(default)


def test_default_diagnose_reply_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "task_db_holders", lambda: {})
    reply = doctor.diagnose({"probe": lambda: {"ok": True}}, timeout_seconds=5.0)
    assert set(reply) == {"schema_id", "verdict", "problems", "probes", "task_db_holders"}


def test_census_shape_and_five_mb_bytes_in_largest_objects() -> None:
    global _BIG_BYTES
    _BIG_BYTES = bytes(FIVE_MB)
    try:
        census = _census()
    finally:
        _BIG_BYTES = None
    assert {
        "top_types",
        "largest_objects",
        "gc_counts",
        "census_seconds",
        "census_truncated",
    } <= set(census)
    assert census["census_truncated"] is False
    assert isinstance(census["census_seconds"], float) and census["census_seconds"] >= 0
    assert set(census["gc_counts"]) == {"before_collect", "collected", "after_collect"}
    assert len(census["gc_counts"]["before_collect"]) == 3
    assert isinstance(census["gc_counts"]["collected"], int)

    top = census["top_types"]
    assert 0 < len(top) <= doctor.HEAP_CENSUS_TOP_TYPES
    counts = [entry["count"] for entry in top]
    assert counts == sorted(counts, reverse=True)
    for entry in top:
        assert set(entry) == {"type", "count", "shallow_bytes"}
        assert isinstance(entry["type"], str)

    largest = census["largest_objects"]
    assert 0 < len(largest) <= doctor.HEAP_CENSUS_LARGEST_OBJECTS
    sizes = [entry["shallow_bytes"] for entry in largest]
    assert sizes == sorted(sizes, reverse=True)
    hits = [
        entry
        for entry in largest
        if entry["type"] == "bytes" and entry["len"] == FIVE_MB
    ]
    assert len(hits) == 1, largest
    hit = hits[0]
    assert hit["shallow_bytes"] >= FIVE_MB
    assert hit["referrer"] == "module"
    assert hit["referrer_name"] == __name__
    for entry in largest:
        assert set(entry) == {"type", "len", "shallow_bytes", "referrer", "referrer_name"}


def test_census_never_includes_object_contents() -> None:
    global _MARKED_TEXT
    _MARKED_TEXT = CONTENT_MARKER * 100_000
    try:
        census = _census()
    finally:
        _MARKED_TEXT = None
    assert any(
        entry["type"] == "str" and entry["len"] == len(CONTENT_MARKER) * 100_000
        for entry in census["largest_objects"]
    )
    assert CONTENT_MARKER not in json.dumps(census)


def test_object_cap_reports_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "HEAP_CENSUS_MAX_OBJECTS", 10)
    census = _census()
    assert census["census_truncated"] is True
    assert census["objects_scanned"] == 10
    assert sum(entry["count"] for entry in census["top_types"]) <= 10


def test_diagnose_with_census_attaches_server_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "task_db_holders", lambda: {})
    monkeypatch.setattr(doctor, "REPLY_MAX_BYTES", 1 << 20)
    reply = doctor.diagnose({"probe": lambda: {"ok": True}}, heap_census=True)
    census = reply["server_process"]["heap_census"]
    assert census["largest_objects"] and census["top_types"]
    assert "largest_objects_dropped" not in census
    assert len(json.dumps(reply, separators=(",", ":"), sort_keys=True)) <= 1 << 20


def test_diagnose_drops_largest_objects_first_when_oversized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "task_db_holders", lambda: {})
    monkeypatch.setattr(doctor, "REPLY_MAX_BYTES", 1 << 20)
    base = doctor.diagnose({"probe": lambda: {"ok": True}}, heap_census=True)
    census = base["server_process"]["heap_census"]
    native = base["server_process"]["native_memory"]
    without_largest = dict(base)
    without_largest["server_process"] = dict(base["server_process"])
    # The optional sections (payload holders, mallinfo2) always give way before largest_objects.
    without_largest["server_process"]["heap_census"] = {
        **census,
        "largest_objects": [],
        "largest_objects_dropped": True,
        "untracked_payload": {
            **census["untracked_payload"],
            "top_holders": [],
            "top_holders_dropped": True,
        },
    }
    if isinstance(native.get("mallinfo2"), dict):
        without_largest["server_process"]["native_memory"] = {
            **native,
            "mallinfo2": None,
            "reasons": {**native["reasons"], "mallinfo2": "dropped_to_fit_reply"},
        }
    # Room for everything except the largest-objects list (plus slack for gc/timing jitter).
    limit = doctor._encoded_size(without_largest) + 256
    assert doctor._encoded_size(base) > limit
    monkeypatch.setattr(doctor, "REPLY_MAX_BYTES", limit)
    reply = doctor.diagnose({"probe": lambda: {"ok": True}}, heap_census=True)
    fitted = reply["server_process"]["heap_census"]
    assert fitted["largest_objects"] == []
    assert fitted["largest_objects_dropped"] is True
    assert fitted["untracked_payload"]["top_holders_dropped"] is True
    assert fitted["top_types"]
    assert "top_types_dropped" not in fitted
    assert "summaries_dropped" not in reply

    monkeypatch.setattr(doctor, "REPLY_MAX_BYTES", 1)
    tiny = doctor.diagnose({"probe": lambda: {"ok": True}}, heap_census=True)
    dropped = tiny["server_process"]["heap_census"]
    assert dropped["largest_objects"] == [] and dropped["top_types"] == []
    assert dropped["largest_objects_dropped"] is True
    assert dropped["top_types_dropped"] is True
    assert dropped["untracked_payload"]["top_holders"] == []
    assert dropped["untracked_payload"]["top_holders_dropped"] is True
    assert tiny["server_process"]["native_memory"]["mallinfo2"] is None


def test_mcp_tool_exposes_heap_census_defaulting_false() -> None:
    from aiworkhub import server

    parameter = inspect.signature(server.aiworkhub_doctor_diagnose).parameters["heap_census"]
    assert parameter.default is False
    assert (server.aiworkhub_doctor_diagnose.__doc__ or "").lstrip().startswith("READ-ONLY:")


HOLDER_KEYS = {"type", "len", "payload_bytes", "referrer", "referrer_name"}


def test_untracked_payload_totals_and_top_holder() -> None:
    """NF-2026-02331: a module-level list of 50 x 200 KB bytes shows in totals and holders."""
    global _PLANTED
    _PLANTED = [bytes([index]) * PAYLOAD_SIZE for index in range(PAYLOAD_COUNT)]
    try:
        census = _census()
    finally:
        _PLANTED = None
    planted = PAYLOAD_COUNT * PAYLOAD_SIZE
    payload = census["untracked_payload"]
    assert set(payload["types"]) == {"str", "bytes", "bytearray"}
    for entry in payload["types"].values():
        assert set(entry) == {"count", "shallow_bytes"}
    assert payload["types"]["bytes"]["count"] >= PAYLOAD_COUNT
    assert payload["types"]["bytes"]["shallow_bytes"] >= planted
    assert payload["total_bytes"] >= planted
    assert payload["payloads_truncated"] is False

    holders = payload["top_holders"]
    assert 0 < len(holders) <= doctor.HEAP_CENSUS_LARGEST_OBJECTS
    held = [entry["payload_bytes"] for entry in holders]
    assert held == sorted(held, reverse=True)
    for entry in holders:
        assert set(entry) == HOLDER_KEYS
    hits = [entry for entry in holders if entry["type"] == "list" and entry["len"] == PAYLOAD_COUNT]
    assert len(hits) == 1, holders
    assert hits[0]["payload_bytes"] >= planted
    assert hits[0]["referrer"] == "module"
    assert hits[0]["referrer_name"] == __name__


def test_payload_reached_from_two_containers_is_counted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = bytes([7]) * PAYLOAD_SIZE
    first = [shared]
    second = [shared, shared]
    monkeypatch.setattr(doctor.gc, "get_objects", lambda: [first, second])
    payload = doctor._heap_census(doctor.HEAP_CENSUS_MAX_OBJECTS)["untracked_payload"]
    size = sys.getsizeof(shared)
    assert payload["types"]["bytes"] == {"count": 1, "shallow_bytes": size}
    assert payload["types"]["str"] == {"count": 0, "shallow_bytes": 0}
    assert payload["total_bytes"] == size
    by_len = {entry["len"]: entry["payload_bytes"] for entry in payload["top_holders"]}
    assert by_len == {1: size, 2: 2 * size}


def test_payload_totals_are_bounded_by_max_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    planted = [bytes([index]) * 64 for index in range(5)]
    monkeypatch.setattr(doctor.gc, "get_objects", lambda: [planted])
    census = doctor._heap_census(2)
    payload = census["untracked_payload"]
    assert census["objects_scanned"] == 1
    assert payload["types"]["bytes"]["count"] == 2
    assert payload["payloads_truncated"] is True
    # The holder still reports every payload byte it directly references.
    assert payload["top_holders"][0]["payload_bytes"] == sum(sys.getsizeof(b) for b in planted)


def test_diagnose_drops_top_holders_before_largest_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "task_db_holders", lambda: {})
    monkeypatch.setattr(doctor, "REPLY_MAX_BYTES", 1 << 20)
    base = doctor.diagnose({"probe": lambda: {"ok": True}}, heap_census=True)
    census = base["server_process"]["heap_census"]
    assert census["untracked_payload"]["top_holders"]
    without_holders = dict(base)
    without_holders["server_process"] = dict(base["server_process"])
    without_holders["server_process"]["heap_census"] = {
        **census,
        "untracked_payload": {
            **census["untracked_payload"],
            "top_holders": [],
            "top_holders_dropped": True,
        },
    }
    limit = doctor._encoded_size(without_holders) + 256
    assert doctor._encoded_size(base) > limit
    monkeypatch.setattr(doctor, "REPLY_MAX_BYTES", limit)
    reply = doctor.diagnose({"probe": lambda: {"ok": True}}, heap_census=True)
    fitted = reply["server_process"]["heap_census"]
    assert fitted["untracked_payload"]["top_holders"] == []
    assert fitted["untracked_payload"]["top_holders_dropped"] is True
    assert fitted["untracked_payload"]["types"]["bytes"]["count"] >= 0
    assert fitted["largest_objects"] and fitted["top_types"]
    assert "largest_objects_dropped" not in fitted
    assert "summaries_dropped" not in reply


def test_census_resolves_owners_without_get_referrers(monkeypatch: pytest.MonkeyPatch) -> None:
    """NF-2026-02334: gc.get_referrers walks the whole heap per object; never call it."""

    def refuse(*_objects: Any) -> list[Any]:
        raise AssertionError("gc.get_referrers must not be called by the heap census")

    monkeypatch.setattr(doctor.gc, "get_referrers", refuse)
    global _BIG_BYTES
    _BIG_BYTES = bytes(FIVE_MB)
    try:
        census = _census()
    finally:
        _BIG_BYTES = None
    assert census["objects_skipped"] >= 0
    hits = [
        entry
        for entry in census["largest_objects"]
        if entry["type"] == "bytes" and entry["len"] == FIVE_MB
    ]
    assert len(hits) == 1, census["largest_objects"]
    assert hits[0]["referrer"] == "module"
    assert hits[0]["referrer_name"] == __name__


def test_register_owner_skips_the_shared_builtins_dict() -> None:
    """NF-2026-02334 review: every function references one builtins dict; register it never."""
    owners: dict[int, tuple[str, str]] = {}
    namespaces: list[dict[Any, Any]] = []
    shared = _census.__builtins__
    for fn in (_census, test_census_resolves_owners_without_get_referrers, _census):
        doctor._register_owner(fn, owners, namespaces)
    assert id(shared) not in owners
    assert all(namespace is not shared for namespace in namespaces)
    assert len({id(namespace) for namespace in namespaces}) == len(namespaces)
