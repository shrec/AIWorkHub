"""NF-2026-01929: the _latest_by_request projection fits the cache bound.

Rows carrying a large ``validation`` payload pushed the replace-fold projection
past ``_LATEST_EVENT_CACHE_MAX_BYTES``, so it was never retained and every call
was a cold replay. ``_latest_by_request`` now drops the heavy fields no reader
uses; these tests pin that it is retained and that every other field matches a
cold replay.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_event_ledger as ledger  # noqa: E402
from aiworkhub import process_launcher  # noqa: E402

_ROWS = 40
_VALIDATION = [{"command": "pytest", "stdout": "v" * 8192}]
_DROPPED = process_launcher._LATEST_BY_REQUEST_DROP_FIELDS


@pytest.fixture(autouse=True)
def _clean_cache():
    ledger.reset_latest_event_cache()
    yield
    ledger.reset_latest_event_cache()


def _corpus(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for index in range(_ROWS):
            rows = [
                {
                    "request_id": f"request-{index}",
                    "task_id": f"task-{index % 7}",
                    "state": "running",
                    "quality_review_receipt": {"verdict": f"ok-{index}"},
                    "worker_mcp_gate": {"gated": True, "satisfied": True},
                },
                {
                    "request_id": f"request-{index}",
                    "state": "review_ready",
                    "validation": _VALIDATION,
                    "usage": {"input_tokens": index},
                    "token_budget": {"limit": index},
                    "accepted_outcome_receipt": {"id": index},
                    "quality_review_receipt": {"verdict": f"ok-{index}"},
                    "worker_mcp_gate": {"gated": True, "satisfied": True},
                },
            ]
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")


def _manager(path: Path) -> SimpleNamespace:
    return SimpleNamespace(process_log_path=path)


def _cold(path: Path) -> dict:
    return ledger.latest_events(
        path,
        key_field="request_id",
        skip_event_kinds=(process_launcher.RUNTIME_NOTICE_EVENT_KIND,),
        replace=True,
    )


def test_projection_is_retained_and_second_call_is_a_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    full = _cold(path)
    full_bytes = ledger.latest_event_cache_stats()["estimated_bytes"]
    ledger.reset_latest_event_cache()

    ledger.reset_latest_event_cache()
    monkeypatch.setattr(ledger, "_LATEST_EVENT_CACHE_MAX_BYTES", full_bytes // 2)
    assert _cold(path) == full
    assert ledger.latest_event_cache_stats()["entries"] == 0
    ledger.reset_latest_event_cache()

    manager = _manager(path)
    first = process_launcher.ProcessManager._latest_by_request(manager)
    stats = ledger.latest_event_cache_stats()
    assert stats["entries"] == 1
    assert stats["estimated_bytes"] <= full_bytes // 2

    rebuilds: list[int] = []
    real_rebuild = ledger._rebuild_latest_events
    monkeypatch.setattr(
        ledger,
        "_rebuild_latest_events",
        lambda *a, **k: rebuilds.append(1) or real_rebuild(*a, **k),
    )
    second = process_launcher.ProcessManager._latest_by_request(manager)
    assert rebuilds == []
    assert second == first


def test_every_kept_field_equals_a_cold_replay(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    cold = _cold(path)
    ledger.reset_latest_event_cache()
    projected = process_launcher.ProcessManager._latest_by_request(_manager(path))

    assert projected.keys() == cold.keys()
    for request_id, row in cold.items():
        expected = {k: v for k, v in row.items() if k not in _DROPPED}
        assert projected[request_id] == expected
        assert projected[request_id]["quality_review_receipt"]
        assert projected[request_id]["worker_mcp_gate"]
        assert not set(_DROPPED) & projected[request_id].keys()
