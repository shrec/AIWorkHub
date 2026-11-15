"""NF-2026-02273: opt-in bounded live-heap census in doctor server_process."""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from aiworkhub import doctor

FIVE_MB = 5 * 1024 * 1024
CONTENT_MARKER = "NF02273-CONTENT-MARKER-"

# Module globals so the census reaches them through this module's namespace dict.
_BIG_BYTES: bytes | None = None
_MARKED_TEXT: str | None = None


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
    base = doctor.diagnose({"probe": lambda: {"ok": True}}, heap_census=True)
    census = base["server_process"]["heap_census"]
    without_largest = dict(base)
    without_largest["server_process"] = dict(base["server_process"])
    without_largest["server_process"]["heap_census"] = {
        **census,
        "largest_objects": [],
        "largest_objects_dropped": True,
    }
    # Room for everything except the largest-objects list (plus slack for gc/timing jitter).
    limit = doctor._encoded_size(without_largest) + 256
    assert doctor._encoded_size(base) > limit
    monkeypatch.setattr(doctor, "REPLY_MAX_BYTES", limit)
    reply = doctor.diagnose({"probe": lambda: {"ok": True}}, heap_census=True)
    fitted = reply["server_process"]["heap_census"]
    assert fitted["largest_objects"] == []
    assert fitted["largest_objects_dropped"] is True
    assert fitted["top_types"]
    assert "top_types_dropped" not in fitted
    assert "summaries_dropped" not in reply

    monkeypatch.setattr(doctor, "REPLY_MAX_BYTES", 1)
    tiny = doctor.diagnose({"probe": lambda: {"ok": True}}, heap_census=True)
    dropped = tiny["server_process"]["heap_census"]
    assert dropped["largest_objects"] == [] and dropped["top_types"] == []
    assert dropped["largest_objects_dropped"] is True
    assert dropped["top_types_dropped"] is True


def test_mcp_tool_exposes_heap_census_defaulting_false() -> None:
    from aiworkhub import server

    parameter = inspect.signature(server.aiworkhub_doctor_diagnose).parameters["heap_census"]
    assert parameter.default is False
    assert (server.aiworkhub_doctor_diagnose.__doc__ or "").lstrip().startswith("READ-ONLY:")
