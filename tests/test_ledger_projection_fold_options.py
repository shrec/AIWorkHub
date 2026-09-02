"""NF-2026-00561: the launcher re-parsed the whole ledger on every call.

``ProcessManager._latest_by_request`` folded a raw ``iter_events`` pass, so every
one of its ~20 call sites re-read every segment. Measured on this host's ledger
(12 segments, 557.7 MB, 56459 rows):

    uncached iter_events fold   2.190 s
    cached append-aware fold    0.007 s   (325x, byte-identical result)

The cached projection already existed but folded with different semantics: it
MERGES rows per key and keeps every row, while the launcher REPLACES the row per
request and must drop runtime notices -- a notice landing in that map would erase
the ``state`` every reconciler reads.

Rather than keep a second hand-written fold, the projection takes the two
options. These tests pin that the options are honoured, that they are part of
the cache identity (so one view can never be served the other's answer), and
that the cached fold is result-identical to a full uncached pass.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_event_ledger as ledger  # noqa: E402

NOTICE = "runtime_notice"


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _reference_fold(path: Path) -> dict[str, dict]:
    """The launcher's original hand-written fold, kept as the oracle."""
    latest: dict[str, dict] = {}
    for event in ledger.iter_events(path):
        if str(event.get("event_kind") or "") == NOTICE:
            continue
        request_id = str(event.get("request_id") or "")
        if request_id:
            latest[request_id] = event
    return latest


def _corpus(path: Path) -> None:
    _write(
        path,
        [
            {"request_id": "a", "state": "starting", "pid": 1},
            {"request_id": "b", "state": "starting", "pid": 2},
            {"request_id": "a", "state": "running"},
            {"request_id": "a", "event_kind": NOTICE, "state": "observation"},
            {"request_id": "b", "state": "review_ready", "exit_code": 0},
        ],
    )


def test_replace_fold_matches_the_original_hand_written_fold(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger._LATEST_EVENT_CACHE.clear()

    cached = ledger.latest_events(
        path, key_field="request_id", skip_event_kinds=(NOTICE,), replace=True
    )
    assert cached == _reference_fold(path)


def test_replace_drops_the_previous_row_rather_than_overlaying_it(
    tmp_path: Path,
) -> None:
    """`replace` is the launcher's semantics: the newest row IS the state."""
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger._LATEST_EVENT_CACHE.clear()

    replaced = ledger.latest_events(
        path, key_field="request_id", skip_event_kinds=(NOTICE,), replace=True
    )
    merged = ledger.latest_events(path, key_field="request_id")

    # "a" started with a pid and then reported running WITHOUT one.
    assert "pid" not in replaced["a"], "replace must not carry the old pid forward"
    assert merged["a"]["pid"] == 1, "the merge view still overlays"


def test_a_runtime_notice_never_becomes_the_state(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger._LATEST_EVENT_CACHE.clear()

    latest = ledger.latest_events(
        path, key_field="request_id", skip_event_kinds=(NOTICE,), replace=True
    )
    assert latest["a"]["state"] == "running"
    assert latest["a"].get("event_kind") != NOTICE


def test_fold_options_are_part_of_the_cache_identity(tmp_path: Path) -> None:
    """One view must never be served the other view's cached answer."""
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger._LATEST_EVENT_CACHE.clear()

    merged = ledger.latest_events(path, key_field="request_id")
    replaced = ledger.latest_events(
        path, key_field="request_id", skip_event_kinds=(NOTICE,), replace=True
    )
    assert merged != replaced
    # Asking again in the other order must still give each its own answer.
    assert ledger.latest_events(path, key_field="request_id") == merged
    assert (
        ledger.latest_events(
            path, key_field="request_id", skip_event_kinds=(NOTICE,), replace=True
        )
        == replaced
    )


def test_incremental_append_keeps_the_fold_options(tmp_path: Path) -> None:
    """The append-only fast path must not silently revert to a merge fold."""
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger._LATEST_EVENT_CACHE.clear()

    first = ledger.latest_events(
        path, key_field="request_id", skip_event_kinds=(NOTICE,), replace=True
    )
    assert first["b"]["state"] == "review_ready"

    _write(
        path,
        [
            {"request_id": "b", "event_kind": NOTICE, "state": "observation"},
            {"request_id": "b", "state": "finished"},
        ],
    )

    second = ledger.latest_events(
        path, key_field="request_id", skip_event_kinds=(NOTICE,), replace=True
    )
    assert second == _reference_fold(path)
    assert second["b"]["state"] == "finished"
    assert "exit_code" not in second["b"], "replace must not carry the old row forward"


def test_skip_event_kinds_order_does_not_split_the_cache(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    _corpus(path)
    ledger._LATEST_EVENT_CACHE.clear()

    ledger.latest_events(path, skip_event_kinds=(NOTICE, "other"), replace=True)
    before = len(ledger._LATEST_EVENT_CACHE)
    ledger.latest_events(path, skip_event_kinds=("other", NOTICE), replace=True)
    assert len(ledger._LATEST_EVENT_CACHE) == before
