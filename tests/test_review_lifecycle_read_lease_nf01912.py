"""NF-2026-01912: read-only review projections must not hold the write lease."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from aiworkhub import db_writer, review_lifecycle

_SHA = "a" * 64


def _store(tmp_path: Path) -> Path:
    db = tmp_path / "task_queue.sqlite"
    review_lifecycle.create_or_replay_chain(
        db,
        target_task_id="task-1",
        target_request_id="req-1",
        claim_epoch=1,
        packet_sha256=_SHA,
        candidate_sha256="b" * 64,
    )
    return db


def _lease_refused(*_args, **_kwargs):
    raise AssertionError("write_lease must not be taken by a read-only projection")


def test_projections_take_no_write_lease(tmp_path, monkeypatch) -> None:
    db = _store(tmp_path)
    counts = review_lifecycle.lifecycle_counts(db)
    drain = review_lifecycle.drain_projection(db)
    rows = review_lifecycle.rows_for_test(db)
    failures = review_lifecycle.terminal_route_binding_failures(db)

    monkeypatch.setattr(db_writer, "write_lease", _lease_refused)

    assert review_lifecycle.lifecycle_counts(db) == counts
    assert review_lifecycle.drain_projection(db) == drain
    assert review_lifecycle.rows_for_test(db) == rows
    assert review_lifecycle.terminal_route_binding_failures(db) == failures


def test_lifecycle_counts_does_not_wait_for_another_holder(tmp_path) -> None:
    db = _store(tmp_path)
    expected = review_lifecycle.lifecycle_counts(db)
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with db_writer.write_lease(db):
            held.set()
            release.wait(30)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    try:
        assert held.wait(10)
        started = time.monotonic()
        assert review_lifecycle.lifecycle_counts(db) == expected
        review_lifecycle.drain_projection(db)
        assert time.monotonic() - started < 5.0
    finally:
        release.set()
        thread.join(10)


def test_tampered_chain_still_raises(tmp_path) -> None:
    db = _store(tmp_path)
    raw = sqlite3.connect(str(db))
    try:
        raw.execute(
            "UPDATE review_action_outbox SET descriptor_json = descriptor_json || ' '"
        )
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(review_lifecycle.ReviewLifecycleError):
        review_lifecycle.lifecycle_counts(db)
    with pytest.raises(review_lifecycle.ReviewLifecycleError):
        review_lifecycle.drain_projection(db)
