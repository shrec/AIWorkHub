"""NF-2026-02057: review-chain verification re-hashes only what changed.

The reconciler, ``lifecycle_counts`` and the review drain all verify every
chain on every pass.  A per-process memo of exact row fingerprints lets an
unchanged chain skip re-canonicalization, while any rewritten, missing or
shortened row is still verified (and still fails closed) on the next pass.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import review_lifecycle  # noqa: E402


PACKET = "A" * 64
CANDIDATE = "b" * 64
NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_memo() -> Any:
    review_lifecycle.reset_chain_verify_memo()
    yield
    review_lifecycle.reset_chain_verify_memo()


class _Counters:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sha_calls = 0
        self.verified_indexes: list[int] = []
        real_sha = review_lifecycle._canonical_json_sha
        real_row = review_lifecycle._verify_action_row

        def counting_sha(payload: Any) -> tuple[str, str]:
            self.sha_calls += 1
            return real_sha(payload)

        def counting_row(conn: Any, row: Any, identity: Any, identity_sha256: str) -> Any:
            self.verified_indexes.append(int(row["action_index"]))
            return real_row(conn, row, identity, identity_sha256)

        monkeypatch.setattr(review_lifecycle, "_canonical_json_sha", counting_sha)
        monkeypatch.setattr(review_lifecycle, "_verify_action_row", counting_row)

    def reset(self) -> None:
        self.sha_calls = 0
        self.verified_indexes = []


def _chain(db: Path) -> review_lifecycle.ReviewChain:
    return review_lifecycle.create_or_replay_chain(
        db,
        target_task_id="TASK_MEMO",
        target_request_id="req-memo",
        claim_epoch="3",
        packet_sha256=PACKET,
        candidate_sha256=CANDIDATE,
        now=NOW,
    )


def _raw(db: Path, sql: str, params: tuple[Any, ...]) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_second_pass_over_unchanged_chain_does_not_rehash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    review_lifecycle.reset_chain_verify_memo()
    counters = _Counters(monkeypatch)

    first = review_lifecycle.lifecycle_counts(db)
    assert sorted(counters.verified_indexes) == list(range(len(review_lifecycle.PLAN)))
    assert counters.sha_calls > 0

    counters.reset()
    second = review_lifecycle.lifecycle_counts(db)
    assert second == first
    assert counters.verified_indexes == []
    assert counters.sha_calls == 0
    stats = review_lifecycle.chain_verify_memo_stats()
    assert stats["chain_hits"] >= 1
    assert stats["rows_skipped"] >= len(review_lifecycle.PLAN)


def test_advancing_one_action_verifies_only_that_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    review_lifecycle.lifecycle_counts(db)
    counters = _Counters(monkeypatch)

    # Make the first action "reserved" the way a file-level writer would, so
    # nothing but the next pass observes the change.
    _raw(
        db,
        "UPDATE review_action_outbox SET state='reserved', owner='w', lease_token='t', "
        "lease_expires_at=? WHERE action_index=0",
        ("2026-11-04T09:01:00.000000+00:00",),
    )
    counts = review_lifecycle.lifecycle_counts(db)
    assert counts["reserved"] == 1
    assert counters.verified_indexes == [0]

    counters.reset()
    review_lifecycle.lifecycle_counts(db)
    assert counters.verified_indexes == []


def test_mutated_middle_row_is_detected_on_next_pass(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    review_lifecycle.lifecycle_counts(db)
    review_lifecycle.lifecycle_counts(db)

    _raw(
        db,
        "UPDATE review_action_outbox SET descriptor_sha256=? WHERE action_index=5",
        ("0" * 64,),
    )
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="descriptor_tamper"):
        review_lifecycle.lifecycle_counts(db)
    # The failure dropped the memo; it still fails closed on every later pass.
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="descriptor_tamper"):
        review_lifecycle.lifecycle_counts(db)


def test_missing_row_and_rewritten_chain_row_still_fail_closed(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    review_lifecycle.lifecycle_counts(db)

    _raw(db, "UPDATE review_chains SET chain_identity_sha256=?", ("c" * 64,))
    with pytest.raises(review_lifecycle.ReviewLifecycleError):
        review_lifecycle.lifecycle_counts(db)

    other = tmp_path / "other.sqlite"
    _chain(other)
    review_lifecycle.lifecycle_counts(other)
    _raw(other, "DELETE FROM review_action_outbox WHERE action_index=11", ())
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="descriptor_tamper"):
        review_lifecycle.lifecycle_counts(other)


def test_retired_row_is_reverified_when_an_earlier_row_changes() -> None:
    memo = review_lifecycle._ChainVerifyMemo(8)
    digests = tuple(bytes([i]) * 32 for i in range(4))
    retired = (False, False, False, True)
    assert memo.rows_to_verify("x", 1, digests, retired) == [0, 1, 2, 3]
    memo.store("x", 1, digests)
    assert memo.rows_to_verify("x", 1, digests, retired) == []
    changed = (digests[0], b"\xff" * 32, digests[2], digests[3])
    assert memo.rows_to_verify("x", 1, changed, retired) == [1, 3]
    assert memo.rows_to_verify("x", 1, digests[:3], retired[:3]) == [0, 1, 2]


def test_memo_is_bounded() -> None:
    memo = review_lifecycle._ChainVerifyMemo(2)
    for chain_id in range(5):
        memo.store("x", chain_id, (b"\x00" * 32,))
    stats = memo.stats()
    assert stats["chains"] == 2
    assert stats["max_chains"] == 2
    assert memo.rows_to_verify("x", 0, (b"\x00" * 32,), (False,)) == [0]
    assert memo.rows_to_verify("x", 4, (b"\x00" * 32,), (False,)) == []
