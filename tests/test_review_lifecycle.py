from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import review_lifecycle, review_orchestrator  # noqa: E402


PACKET = "A" * 64
CANDIDATE = "b" * 64
NOW = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)


def _chain(db: Path) -> review_lifecycle.ReviewChain:
    return review_lifecycle.create_or_replay_chain(
        db,
        target_task_id="TASK_TARGET",
        target_request_id="req-target",
        claim_epoch="7",
        packet_sha256=PACKET,
        candidate_sha256=CANDIDATE,
        now=NOW,
    )


def _reserve(db: Path, *, now: datetime = NOW) -> review_lifecycle.ReviewAction:
    action = review_lifecycle.reserve_next_action(
        db, owner="worker-a", lease_token="lease-a", now=now, lease_seconds=60
    )
    assert action is not None
    return action


def _tamper(db: Path, column: str, value: object, *, action_index: int = 0) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            f"UPDATE review_action_outbox SET {column}=? WHERE action_index=?",
            (value, action_index),
        )
        conn.commit()
    finally:
        conn.close()


def _tamper_chain_digest(db: Path, column: str, value: object) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(f"UPDATE review_chains SET {column}=?", (value,))
        conn.commit()
    finally:
        conn.close()


def _delete_action(db: Path, action_index: int) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "DELETE FROM review_action_outbox WHERE action_index=?",
            (action_index,),
        )
        conn.commit()
    finally:
        conn.close()


def test_unique_episode_replay_returns_same_chain_and_drift_conflicts(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    first = _chain(db)
    replay = review_lifecycle.create_or_replay_chain(
        db,
        target_task_id="TASK_TARGET",
        target_request_id="req-target",
        claim_epoch=7,
        packet_sha256=PACKET.lower(),
        candidate_sha256=CANDIDATE.upper(),
        now=NOW,
    )

    assert replay.chain_id == first.chain_id
    assert replay.chain_identity_sha256 == first.chain_identity_sha256
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="chain_identity_conflict"):
        review_lifecycle.create_or_replay_chain(
            db,
            target_task_id="TASK_TARGET",
            target_request_id="req-target",
            claim_epoch=7,
            packet_sha256="c" * 64,
            candidate_sha256=CANDIDATE,
            now=NOW,
        )


def test_digest_inputs_must_be_exact_sha256_hex(tmp_path: Path) -> None:
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="invalid_packet_sha256"):
        review_lifecycle.create_or_replay_chain(
            tmp_path / "task.sqlite",
            target_task_id="TASK_TARGET",
            target_request_id="req-target",
            claim_epoch=7,
            packet_sha256="g" * 64,
            candidate_sha256=CANDIDATE,
        )


@pytest.mark.parametrize(
    ("column", "tampered_value", "expected"),
    [
        ("packet_sha256", PACKET.upper(), "stored_packet_sha256_tamper"),
        ("packet_sha256", f"{PACKET.lower()} ", "stored_packet_sha256_tamper"),
        ("candidate_sha256", CANDIDATE.upper(), "stored_candidate_sha256_tamper"),
        ("candidate_sha256", f" {CANDIDATE.lower()}", "stored_candidate_sha256_tamper"),
    ],
)
def test_stored_chain_digests_must_be_exact_lowercase_hex_for_all_entrypoints(
    tmp_path: Path,
    column: str,
    tampered_value: str,
    expected: str,
) -> None:
    def create_or_replay(db: Path, chain_id: int) -> None:
        del chain_id
        review_lifecycle.create_or_replay_chain(
            db,
            target_task_id="TASK_TARGET",
            target_request_id="req-target",
            claim_epoch="7",
            packet_sha256=PACKET,
            candidate_sha256=CANDIDATE,
            now=NOW,
        )

    entrypoints: tuple[Callable[[Path, int], object], ...] = (
        lambda db, chain_id: review_lifecycle.actions_for_chain(db, chain_id),
        lambda db, _chain_id: review_lifecycle.reserve_next_action(
            db, owner="worker-a", lease_token="lease-a", now=NOW
        ),
        create_or_replay,
    )
    for index, entrypoint in enumerate(entrypoints):
        db = tmp_path / f"{column}_{index}_{len(tampered_value)}.sqlite"
        chain = _chain(db)
        _tamper_chain_digest(db, column, tampered_value)
        with pytest.raises(review_lifecycle.ReviewLifecycleError, match=expected):
            entrypoint(db, chain.chain_id)


def test_canonical_twelve_action_order(tmp_path: Path) -> None:
    actions = _chain(tmp_path / "task.sqlite").actions

    assert [(a.phase, a.action_type, a.lens) for a in actions] == [
        ("correctness", "launch", "correctness"),
        ("correctness", "accept", "correctness"),
        ("correctness", "archive", "correctness"),
        ("security", "launch", "security"),
        ("security", "accept", "security"),
        ("security", "archive", "security"),
        ("code_quality", "launch", "code_quality"),
        ("code_quality", "accept", "code_quality"),
        ("code_quality", "archive", "code_quality"),
        ("target", "target_accept", ""),
        ("target", "target_archive", ""),
        ("needfix", "needfix_close", ""),
    ]
    assert [a.action_index for a in actions] == list(range(12))
    assert actions[-1].phase == "needfix"
    assert actions[-1].action_type == "needfix_close"


def test_two_connection_concurrency_blocks_behind_reserved_head_action(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    results: list[int | None] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def reserve(owner: str) -> None:
        try:
            barrier.wait()
            action = review_lifecycle.reserve_next_action(
                db, owner=owner, lease_token=f"{owner}-token", now=NOW, lease_seconds=60
            )
            results.append(None if action is None else action.action_index)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reserve, args=(f"worker-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    ordered_results = sorted(results, key=lambda value: -1 if value is None else value)
    assert ordered_results == [None, 0]

    first = next(row for row in review_lifecycle.rows_for_test(db) if row["action_index"] == 0)
    assert review_lifecycle.complete_action(
        db,
        action_id=int(first["action_id"]),
        owner=str(first["owner"]),
        lease_token=str(first["lease_token"]),
        receipt={"ok": True},
        now=NOW + timedelta(seconds=1),
    )
    second = review_lifecycle.reserve_next_action(
        db, owner="worker-after", lease_token="worker-after-token", now=NOW, lease_seconds=60
    )
    assert second is not None
    assert second.action_index == 1


def test_expired_reclaim_is_deterministic_and_malformed_time_fails(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    first = _reserve(db, now=NOW)
    reclaimed = review_lifecycle.reserve_next_action(
        db,
        owner="worker-b",
        lease_token="lease-b",
        now=NOW + timedelta(seconds=61),
        lease_seconds=60,
    )
    assert reclaimed is not None
    assert reclaimed.action_id == first.action_id

    _tamper(db, "lease_expires_at", "2026-08-29T18:00:00Z")
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="malformed_lease_expires_at"):
        review_lifecycle.reserve_next_action(
            db, owner="worker-c", lease_token="lease-c", now=NOW + timedelta(seconds=122)
        )


def test_stale_owner_cannot_complete_and_exact_completion_replay_conflicts(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    action = _reserve(db, now=NOW)
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="stale_owner"):
        review_lifecycle.complete_action(
            db,
            action_id=action.action_id,
            owner="worker-a",
            lease_token="lease-a",
            receipt={"ok": True},
            now=NOW + timedelta(seconds=61),
        )

    action = review_lifecycle.reserve_next_action(
        db,
        owner="worker-b",
        lease_token="lease-b",
        now=NOW + timedelta(seconds=62),
        lease_seconds=60,
    )
    assert action is not None
    assert review_lifecycle.complete_action(
        db,
        action_id=action.action_id,
        owner="worker-b",
        lease_token="lease-b",
        receipt={"ok": True},
        now=NOW + timedelta(seconds=63),
    )
    assert review_lifecycle.complete_action(
        db,
        action_id=action.action_id,
        owner="worker-b",
        lease_token="lease-b",
        receipt={"ok": True},
        now=NOW + timedelta(seconds=64),
    )
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="completion_conflict"):
        review_lifecycle.complete_action(
            db,
            action_id=action.action_id,
            owner="worker-b",
            lease_token="lease-b",
            receipt={"ok": False},
            now=NOW + timedelta(seconds=65),
        )


def test_completed_action_is_sqlite_immutable_after_exact_replay(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    action = _reserve(db, now=NOW)
    receipt = {"ok": True, "scenario": "immutable"}
    assert review_lifecycle.complete_action(
        db,
        action_id=action.action_id,
        owner="worker-a",
        lease_token="lease-a",
        receipt=receipt,
        now=NOW + timedelta(seconds=1),
    )
    assert review_lifecycle.complete_action(
        db,
        action_id=action.action_id,
        owner="worker-a",
        lease_token="lease-a",
        receipt=receipt,
        now=NOW + timedelta(seconds=2),
    )

    replacement_json = json.dumps(
        {"ok": True, "scenario": "immutable", "tampered": True},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    replacement_sha = hashlib.sha256(replacement_json.encode("utf-8")).hexdigest()
    row = next(row for row in review_lifecycle.rows_for_test(db) if row["action_index"] == 0)
    replacement_commitment = hashlib.sha256(
        json.dumps(
            {
                "schema_id": "aiworkhub.review_completion_receipt_commitment.v1",
                "action_id": str(row["action_id"]),
                "chain_id": str(row["chain_id"]),
                "action_index": str(row["action_index"]),
                "descriptor_sha256": str(row["descriptor_sha256"]),
                "receipt_json": replacement_json,
                "receipt_sha256": replacement_sha,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()

    statements = [
        (
            "UPDATE review_action_outbox SET receipt_json=?, receipt_sha256=?, "
            "receipt_commitment_sha256=? WHERE action_id=?",
            (replacement_json, replacement_sha, replacement_commitment, action.action_id),
        ),
        (
            "UPDATE review_action_outbox SET state='reserved' WHERE action_id=?",
            (action.action_id,),
        ),
        (
            "UPDATE review_action_outbox SET descriptor_json='{}' WHERE action_id=?",
            (action.action_id,),
        ),
        ("DELETE FROM review_action_outbox WHERE action_id=?", (action.action_id,)),
    ]
    for sql, parameters in statements:
        conn = sqlite3.connect(db)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="completed_action_immutable"):
                conn.execute(sql, parameters)
                conn.commit()
        finally:
            conn.close()

    assert review_lifecycle.complete_action(
        db,
        action_id=action.action_id,
        owner="worker-a",
        lease_token="lease-a",
        receipt=receipt,
        now=NOW + timedelta(seconds=3),
    )


def test_descriptor_column_and_digest_tamper_matrix(tmp_path: Path) -> None:
    columns = [
        ("action_type", "accept"),
        ("phase", "security"),
        ("lens", "security"),
        ("target_task_id", "OTHER"),
        ("target_request_id", "other-req"),
        ("claim_epoch", "8"),
        ("descriptor_json", "{}"),
        ("descriptor_sha256", "0" * 64),
    ]
    for column, value in columns:
        db = tmp_path / f"{column}.sqlite"
        _chain(db)
        _tamper(db, column, value)
        with pytest.raises(review_lifecycle.ReviewLifecycleError, match="descriptor_tamper"):
            review_lifecycle.reserve_next_action(
                db, owner="worker-a", lease_token="lease-a", now=NOW
            )


def test_malformed_state_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    _tamper(db, "state", "leased")
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="malformed_state"):
        review_lifecycle.reserve_next_action(
            db, owner="worker-a", lease_token="lease-a", now=NOW
        )


def test_reserve_returns_none_and_actions_for_chain_fails_closed_when_terminal_action_is_missing(
    tmp_path: Path,
) -> None:
    """A drained chain has no pending/reserved row left for reserve to scan.

    Bounded reservation only verifies chains it actually touches, so a
    deleted terminal row in an otherwise-quiescent chain is no longer caught
    by reserve itself -- it is still caught by whole-chain verification.
    """
    db = tmp_path / "task.sqlite"
    chain = _chain(db)
    for _index in range(11):
        action = review_lifecycle.reserve_next_action(
            db,
            owner=f"worker-{_index}",
            lease_token=f"lease-{_index}",
            now=NOW + timedelta(seconds=_index),
        )
        assert action is not None
        assert action.action_index == _index
        assert review_lifecycle.complete_action(
            db,
            action_id=action.action_id,
            owner=f"worker-{_index}",
            lease_token=f"lease-{_index}",
            receipt={"ok": True, "action": _index},
            now=NOW + timedelta(seconds=_index, microseconds=1),
        )
    _delete_action(db, 11)

    assert review_lifecycle.reserve_next_action(
        db,
        owner="worker-terminal",
        lease_token="lease-terminal",
        now=NOW + timedelta(seconds=12),
    ) is None
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="descriptor_tamper"):
        review_lifecycle.actions_for_chain(db, chain.chain_id)


def test_reserve_fails_closed_when_reserved_action_is_missing(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    action = _reserve(db)
    _delete_action(db, action.action_index)

    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="descriptor_tamper"):
        review_lifecycle.reserve_next_action(
            db,
            owner="worker-b",
            lease_token="lease-b",
            now=NOW + timedelta(seconds=61),
        )


def test_complete_fails_closed_when_sibling_pending_action_is_missing(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    action = _reserve(db)
    _delete_action(db, 11)

    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="descriptor_tamper"):
        review_lifecycle.complete_action(
            db,
            action_id=action.action_id,
            owner="worker-a",
            lease_token="lease-a",
            receipt={"ok": True},
            now=NOW + timedelta(seconds=1),
        )


def test_complete_fails_closed_when_sibling_completed_action_is_missing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    first = _reserve(db)
    assert review_lifecycle.complete_action(
        db,
        action_id=first.action_id,
        owner="worker-a",
        lease_token="lease-a",
        receipt={"ok": True},
        now=NOW + timedelta(seconds=1),
    )
    second = review_lifecycle.reserve_next_action(
        db,
        owner="worker-b",
        lease_token="lease-b",
        now=NOW + timedelta(seconds=2),
    )
    assert second is not None
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TRIGGER trg_review_action_completed_no_delete")
        conn.execute("DELETE FROM review_action_outbox WHERE action_index=0")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="descriptor_tamper"):
        review_lifecycle.complete_action(
            db,
            action_id=second.action_id,
            owner="worker-b",
            lease_token="lease-b",
            receipt={"ok": True},
            now=NOW + timedelta(seconds=3),
        )


def test_cas_loss_simulation_raises_typed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    original_connect = review_lifecycle._connect

    def connect(path: str | Path) -> "_CasLossConnection":
        return _CasLossConnection(original_connect(path))

    monkeypatch.setattr(review_lifecycle, "_connect", connect)
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="cas_lost"):
        review_lifecycle.reserve_next_action(
            db, owner="worker-a", lease_token="lease-a", now=NOW
        )


def test_reserve_exact_preimage_cas_detects_between_read_and_write_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    original_connect = review_lifecycle._connect

    def connect(path: str | Path) -> "_PreimageDriftConnection":
        return _PreimageDriftConnection(
            original_connect(path),
            before_update_sql=(
                "UPDATE review_action_outbox SET updated_at=? WHERE action_index=0"
            ),
            before_update_parameters=(
                (NOW + timedelta(microseconds=1)).isoformat(timespec="microseconds"),
            ),
        )

    monkeypatch.setattr(review_lifecycle, "_connect", connect)
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="cas_lost"):
        review_lifecycle.reserve_next_action(db, owner="worker-a", lease_token="lease-a", now=NOW)


def test_complete_exact_preimage_cas_detects_between_read_and_write_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    action = _reserve(db)
    original_connect = review_lifecycle._connect

    def connect(path: str | Path) -> "_PreimageDriftConnection":
        return _PreimageDriftConnection(
            original_connect(path),
            before_update_sql=(
                "UPDATE review_action_outbox SET failure_reason='late drift' "
                "WHERE action_id=?"
            ),
            before_update_parameters=(action.action_id,),
        )

    monkeypatch.setattr(review_lifecycle, "_connect", connect)
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="cas_lost"):
        review_lifecycle.complete_action(
            db,
            action_id=action.action_id,
            owner="worker-a",
            lease_token="lease-a",
            receipt={"ok": True},
            now=NOW + timedelta(seconds=1),
        )


class _CasLossConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor | "_ZeroRowCursor":
        cursor = self._connection.execute(sql, parameters)
        if sql.startswith("UPDATE review_action_outbox SET state='reserved'"):
            return _ZeroRowCursor(cursor)
        return cursor

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


class _ZeroRowCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._cursor, name)


class _PreimageDriftConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        before_update_sql: str,
        before_update_parameters: object = (),
    ) -> None:
        self._connection = connection
        self._before_update_sql = before_update_sql
        self._before_update_parameters = before_update_parameters
        self._drifted = False

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        if sql.startswith("UPDATE review_action_outbox SET state=") and not self._drifted:
            self._drifted = True
            self._connection.execute(self._before_update_sql, self._before_update_parameters)
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


def test_restart_reauthenticates_stored_chain(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    chain = _chain(db)

    restarted = review_lifecycle.actions_for_chain(db, chain.chain_id)

    assert [action.descriptor_sha256 for action in restarted] == [
        action.descriptor_sha256 for action in chain.actions
    ]


def test_pending_is_split_into_parked_and_reservable(tmp_path: Path) -> None:
    """A queue depth that counts work nobody will ever do is not a depth.

    An action whose chain already holds a failed action can never be reserved:
    every later action waits on the earlier one completing. Measured on this
    repository: 1,847 pending, 1,389 of them parked behind 129 failed chains.
    Reported as one number, 'pending' read as a backlog that would be worked
    off, and it never would be.
    """
    db = tmp_path / "review.sqlite"
    live = review_lifecycle.create_or_replay_chain(
        db, target_task_id="LIVE", target_request_id="req-live",
        claim_epoch=1, packet_sha256="a" * 64, candidate_sha256="b" * 64,
        now=NOW,
    )
    parked = review_lifecycle.create_or_replay_chain(
        db, target_task_id="PARKED", target_request_id="req-parked",
        claim_epoch=1, packet_sha256="c" * 64, candidate_sha256="d" * 64,
        now=NOW,
    )
    assert live.chain_id != parked.chain_id

    token = "t" * 32
    action = review_lifecycle.reserve_next_action(
        db, owner="probe", lease_token=token, now=NOW, lease_seconds=60,
    )
    assert action is not None
    review_lifecycle.fail_action(
        db, action_id=action.action_id, owner="probe", lease_token=token,
        reason="RuntimeError:deliberate", now=NOW,
    )

    counts = review_lifecycle.lifecycle_counts(db)
    assert counts["failed"] == 1
    assert counts["pending_parked"] + counts["pending_reservable"] == counts["pending"]
    assert counts["pending_parked"] > 0, "the failed chain's remaining actions are parked"
    assert counts["pending_reservable"] > 0, "the untouched chain is still reservable"


def test_reconcile_retires_pending_descendants_of_a_failed_action_and_blocks_their_reservation(
    tmp_path: Path,
) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    action = _reserve(db)
    assert action.action_index == 0
    review_lifecycle.fail_action(
        db, action_id=action.action_id, owner="worker-a", lease_token="lease-a",
        reason="boom", now=NOW,
    )

    result = review_lifecycle.reconcile_dead_chains(db, now=NOW)
    assert result["retired"] == 11
    assert result["examined_failed"] == 1

    rows = {row["action_index"]: row for row in review_lifecycle.rows_for_test(db)}
    for index in range(1, 12):
        assert rows[index]["state"] == "retired"
        assert rows[index]["retired_due_to_action_id"] == str(action.action_id)

    counts = review_lifecycle.lifecycle_counts(db)
    assert counts["failed"] == 1
    assert counts["retired"] == 11
    assert counts["pending"] == 0

    assert review_lifecycle.reserve_next_action(
        db, owner="worker-b", lease_token="lease-b", now=NOW,
    ) is None


def test_reconcile_dead_chains_is_idempotent_across_repeated_passes(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    action = _reserve(db)
    review_lifecycle.fail_action(
        db, action_id=action.action_id, owner="worker-a", lease_token="lease-a",
        reason="boom", now=NOW,
    )

    first = review_lifecycle.reconcile_dead_chains(db, now=NOW)
    assert first["retired"] == 11

    second = review_lifecycle.reconcile_dead_chains(db, now=NOW)
    assert second["retired"] == 0
    assert second["examined_failed"] == 1

    third = review_lifecycle.reconcile_dead_chains(db, now=NOW)
    assert third["retired"] == 0

    counts = review_lifecycle.lifecycle_counts(db)
    assert counts["retired"] == 11
    assert counts["pending"] == 0


def test_reconcile_dead_chains_is_bounded_per_pass_and_progresses_deterministically(
    tmp_path: Path,
) -> None:
    db = tmp_path / "task.sqlite"
    for index in range(5):
        review_lifecycle.create_or_replay_chain(
            db,
            target_task_id=f"BOUND-{index}",
            target_request_id=f"req-bound-{index}",
            claim_epoch=1,
            packet_sha256=f"{index % 10}" * 64,
            candidate_sha256=f"{(index + 1) % 10}" * 64,
            now=NOW,
        )
        head = review_lifecycle.reserve_next_action(
            db, owner=f"worker-{index}", lease_token=f"lease-{index}",
            now=NOW, lease_seconds=60,
        )
        assert head is not None
        review_lifecycle.fail_action(
            db, action_id=head.action_id, owner=f"worker-{index}",
            lease_token=f"lease-{index}", reason="boom", now=NOW,
        )

    first = review_lifecycle.reconcile_dead_chains(db, now=NOW, batch_limit=2)
    assert first["examined_failed"] == 2
    assert first["retired"] == 22

    second = review_lifecycle.reconcile_dead_chains(db, now=NOW, batch_limit=2)
    assert second["examined_failed"] == 2
    assert second["retired"] == 22

    third = review_lifecycle.reconcile_dead_chains(db, now=NOW, batch_limit=2)
    assert third["examined_failed"] == 1
    assert third["retired"] == 11

    fourth = review_lifecycle.reconcile_dead_chains(db, now=NOW, batch_limit=2)
    assert fourth["wrapped"] == 1
    assert fourth["examined_failed"] == 2
    assert fourth["retired"] == 0

    counts = review_lifecycle.lifecycle_counts(db)
    assert counts["failed"] == 5
    assert counts["retired"] == 55
    assert counts["pending"] == 0


def test_reconcile_dead_chains_fails_closed_when_a_descendant_descriptor_is_tampered(
    tmp_path: Path,
) -> None:
    """``reconcile_dead_chains`` must authenticate every failed row and every
    descendant it retires through ``_verify_chain_row``/``_verify_action_row``
    before mutating it, exactly like ``reserve_next_action`` and
    ``complete_action`` already do. A tampered descendant descriptor must
    abort the whole bounded pass rather than being silently marked retired.
    """
    db = tmp_path / "task.sqlite"
    _chain(db)
    action = _reserve(db)
    assert action.action_index == 0
    review_lifecycle.fail_action(
        db, action_id=action.action_id, owner="worker-a", lease_token="lease-a",
        reason="boom", now=NOW,
    )
    _tamper(db, "descriptor_json", "{}", action_index=1)

    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="descriptor_tamper"):
        review_lifecycle.reconcile_dead_chains(db, now=NOW)

    rows = {row["action_index"]: row for row in review_lifecycle.rows_for_test(db)}
    assert rows[1]["state"] == "pending"
    for index in range(2, 12):
        assert rows[index]["state"] == "pending"


def test_sustained_new_pending_arrivals_cannot_starve_a_redeferred_low_action_id(
    tmp_path: Path,
) -> None:
    """A cursor that only advances forward and only wraps once a forward scan
    comes back completely empty is defeated by sustained arrivals: a steady
    stream of ever-newer pending rows keeps the forward scan non-empty
    forever, so a lease ``defer_action`` returns to ``pending`` at a low
    ``action_id`` is skipped on every call and never reserved again. The
    pending scan must instead bound each round to a snapshot high-water mark
    taken at the round's start, so new arrivals during a round cannot extend
    it and the round -- and so the reset row -- completes within a bounded
    number of calls no matter how many new rows keep arriving.
    """
    db = tmp_path / "task.sqlite"
    stuck = _chain(db)
    first = review_lifecycle.reserve_next_action(
        db, owner="worker-stuck", lease_token="lease-stuck", now=NOW, lease_seconds=60,
    )
    assert first is not None
    assert first.chain_id == stuck.chain_id
    assert first.action_index == 0
    assert review_lifecycle.defer_action(
        db, action_id=first.action_id, owner="worker-stuck",
        lease_token="lease-stuck", now=NOW,
    )

    found = None
    for wave in range(8):
        for filler in range(5):
            review_lifecycle.create_or_replay_chain(
                db,
                target_task_id=f"ARRIVAL-{wave}-{filler}",
                target_request_id=f"req-arrival-{wave}-{filler}",
                claim_epoch=1,
                packet_sha256=f"{(wave * 5 + filler) % 10}" * 64,
                candidate_sha256=f"{(wave * 5 + filler + 1) % 10}" * 64,
                now=NOW,
            )
        action = review_lifecycle.reserve_next_action(
            db, owner=f"worker-wave-{wave}", lease_token=f"lease-wave-{wave}", now=NOW,
        )
        assert action is not None
        if action.chain_id == stuck.chain_id and action.action_index == 0:
            found = action
            break

    assert found is not None, "sustained new arrivals starved the re-deferred low action_id"


def test_retired_evidence_fails_closed_for_malformed_cross_chain_later_and_nonfailed_causes(
    tmp_path: Path,
) -> None:
    db = tmp_path / "task.sqlite"
    chain_a = _chain(db)
    chain_b = review_lifecycle.create_or_replay_chain(
        db, target_task_id="OTHER", target_request_id="req-other", claim_epoch=1,
        packet_sha256="c" * 64, candidate_sha256="d" * 64, now=NOW,
    )
    head_a = review_lifecycle.reserve_next_action(
        db, owner="worker-a", lease_token="lease-a", now=NOW, lease_seconds=60,
    )
    assert head_a is not None
    review_lifecycle.fail_action(
        db, action_id=head_a.action_id, owner="worker-a", lease_token="lease-a",
        reason="boom", now=NOW,
    )
    review_lifecycle.reconcile_dead_chains(db, now=NOW)

    retired_row = next(
        row for row in review_lifecycle.rows_for_test(db)
        if row["chain_id"] == chain_a.chain_id and row["action_index"] == 1
    )
    valid_cause = retired_row["retired_due_to_action_id"]

    def set_cause(value: object) -> None:
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE review_action_outbox SET retired_due_to_action_id=? "
                "WHERE action_id=?",
                (value, retired_row["action_id"]),
            )
            conn.commit()
        finally:
            conn.close()

    # malformed: not a digit at all.
    set_cause("not-a-number")
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="retirement_evidence_invalid"):
        review_lifecycle.actions_for_chain(db, chain_a.chain_id)

    # nonfailed: points at a retired sibling, not a failed row.
    sibling = next(
        row for row in review_lifecycle.rows_for_test(db)
        if row["chain_id"] == chain_a.chain_id and row["action_index"] == 2
    )
    set_cause(str(sibling["action_id"]))
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="retirement_evidence_invalid"):
        review_lifecycle.actions_for_chain(db, chain_a.chain_id)

    # cross-chain: points at a real failed action in a different chain.
    head_b = review_lifecycle.reserve_next_action(
        db, owner="worker-b", lease_token="lease-b", now=NOW, lease_seconds=60,
    )
    assert head_b is not None
    assert head_b.chain_id == chain_b.chain_id
    review_lifecycle.fail_action(
        db, action_id=head_b.action_id, owner="worker-b", lease_token="lease-b",
        reason="boom-b", now=NOW,
    )
    set_cause(str(head_b.action_id))
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="retirement_evidence_invalid"):
        review_lifecycle.actions_for_chain(db, chain_a.chain_id)

    # later: points at a same-chain row whose action_index is not earlier.
    later_row = next(
        row for row in review_lifecycle.rows_for_test(db)
        if row["chain_id"] == chain_a.chain_id and row["action_index"] == 3
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE review_action_outbox SET state='failed', owner='forced', "
            "lease_token='forced-token', lease_expires_at=?, completed_at=?, "
            "failure_reason='forced', retired_due_to_action_id='' WHERE action_id=?",
            (
                NOW.isoformat(timespec="microseconds"),
                NOW.isoformat(timespec="microseconds"),
                later_row["action_id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    set_cause(str(later_row["action_id"]))
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="retirement_evidence_invalid"):
        review_lifecycle.actions_for_chain(db, chain_a.chain_id)

    # restore the original exact same-chain earlier failed reference.
    set_cause(valid_cause)
    review_lifecycle.actions_for_chain(db, chain_a.chain_id)


def test_more_than_256_pending_rows_cannot_starve_an_expired_reserved_lease(
    tmp_path: Path,
) -> None:
    db = tmp_path / "task.sqlite"
    for index in range(260):
        review_lifecycle.create_or_replay_chain(
            db,
            target_task_id=f"FILLER-{index}",
            target_request_id=f"req-filler-{index}",
            claim_epoch=1,
            packet_sha256=f"{index % 10}" * 64,
            candidate_sha256=f"{(index + 1) % 10}" * 64,
            now=NOW,
        )
    target = review_lifecycle.create_or_replay_chain(
        db, target_task_id="TARGET", target_request_id="req-target-late", claim_epoch=1,
        packet_sha256="e" * 64, candidate_sha256="f" * 64, now=NOW,
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE review_action_outbox SET state='reserved', owner='stuck-owner', "
            "lease_token='stuck-token', lease_expires_at=? "
            "WHERE chain_id=? AND action_index=0",
            (
                (NOW - timedelta(seconds=1)).isoformat(timespec="microseconds"),
                target.chain_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    reclaimed = review_lifecycle.reserve_next_action(
        db, owner="worker-new", lease_token="lease-new", now=NOW, lease_seconds=60,
    )
    assert reclaimed is not None
    assert reclaimed.chain_id == target.chain_id
    assert reclaimed.action_index == 0


def test_more_than_256_unexpired_reserved_rows_cannot_starve_a_later_expired_lease(
    tmp_path: Path,
) -> None:
    """The reserved bucket must reach an expired lease through the
    ``state, lease_expires_at`` index, not by scanning ``action_id`` order.

    300 chains reserve their head action with a live, unexpired lease --
    more than ``RESERVE_SCAN_LIMIT`` -- all created (and so all lower
    ``action_id``) before one more chain whose head is genuinely expired. A
    plain ``ORDER BY action_id LIMIT 256`` scan of the reserved bucket would
    fill its whole window with the live rows and never reach the expired one;
    every descendant of every chain here is also blocked behind its own
    unreserved head, so nothing in the pending bucket can paper over that
    failure either.
    """
    db = tmp_path / "task.sqlite"
    live_chain_ids = []
    for index in range(300):
        chain = review_lifecycle.create_or_replay_chain(
            db,
            target_task_id=f"LIVE-RESERVED-{index}",
            target_request_id=f"req-live-reserved-{index}",
            claim_epoch=1,
            packet_sha256=f"{index % 10}" * 64,
            candidate_sha256=f"{(index + 1) % 10}" * 64,
            now=NOW,
        )
        live_chain_ids.append(chain.chain_id)
    target = review_lifecycle.create_or_replay_chain(
        db, target_task_id="LATE-EXPIRED", target_request_id="req-late-expired",
        claim_epoch=1, packet_sha256="e" * 64, candidate_sha256="f" * 64, now=NOW,
    )
    conn = sqlite3.connect(db)
    try:
        for index, chain_id in enumerate(live_chain_ids):
            conn.execute(
                "UPDATE review_action_outbox SET state='reserved', "
                f"owner='live-owner-{index}', lease_token='live-token-{index}', "
                "lease_expires_at=? WHERE chain_id=? AND action_index=0",
                (
                    (NOW + timedelta(seconds=3600)).isoformat(timespec="microseconds"),
                    chain_id,
                ),
            )
        conn.execute(
            "UPDATE review_action_outbox SET state='reserved', owner='stuck-owner', "
            "lease_token='stuck-token', lease_expires_at=? "
            "WHERE chain_id=? AND action_index=0",
            (
                (NOW - timedelta(seconds=1)).isoformat(timespec="microseconds"),
                target.chain_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    reclaimed = review_lifecycle.reserve_next_action(
        db, owner="worker-new", lease_token="lease-new", now=NOW, lease_seconds=60,
    )
    assert reclaimed is not None
    assert reclaimed.chain_id == target.chain_id
    assert reclaimed.action_index == 0


def test_more_than_256_blocked_pending_descendants_do_not_starve_a_later_ready_head(
    tmp_path: Path,
) -> None:
    """A fixed ``ORDER BY action_id LIMIT 256`` pending scan restarted from
    the top on every call can fill its whole window with pending rows
    blocked behind an earlier same-chain failure, and never reach a later
    chain's immediately-reservable head. The pending scan must instead make
    bounded forward progress via a persistent indexed keyset cursor that
    wraps once exhausted, so repeated calls eventually reach it.
    """
    db = tmp_path / "task.sqlite"
    chain_ids = []
    for index in range(24):
        chain = review_lifecycle.create_or_replay_chain(
            db,
            target_task_id=f"BLOCK-{index}",
            target_request_id=f"req-block-{index}",
            claim_epoch=1,
            packet_sha256=f"{index % 10}" * 64,
            candidate_sha256=f"{(index + 1) % 10}" * 64,
            now=NOW,
        )
        chain_ids.append(chain.chain_id)
    conn = sqlite3.connect(db)
    try:
        for chain_id in chain_ids:
            conn.execute(
                "UPDATE review_action_outbox SET state='failed', owner='forced', "
                "lease_token='forced-token', lease_expires_at=?, completed_at=?, "
                "failure_reason='boom' WHERE chain_id=? AND action_index=0",
                (
                    NOW.isoformat(timespec="microseconds"),
                    NOW.isoformat(timespec="microseconds"),
                    chain_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    # 24 failed heads leave 24 * 11 = 264 same-chain-blocked pending
    # descendants -- more than the 256-row scan window -- all with a lower
    # action_id than the later chain created below.
    target = review_lifecycle.create_or_replay_chain(
        db, target_task_id="LATE-TARGET", target_request_id="req-late-target",
        claim_epoch=1, packet_sha256="e" * 64, candidate_sha256="f" * 64, now=NOW,
    )

    found = None
    for attempt in range(3):
        action = review_lifecycle.reserve_next_action(
            db, owner=f"worker-{attempt}", lease_token=f"lease-{attempt}",
            now=NOW, lease_seconds=60,
        )
        if action is not None:
            found = action
            break
    assert found is not None
    assert found.chain_id == target.chain_id
    assert found.action_index == 0


def test_large_table_pending_reserved_failed_paths_use_indexed_state_search(
    tmp_path: Path,
) -> None:
    """Large-table EXPLAIN QUERY PLAN proves the reserve/reconcile hot paths
    do bounded indexed work, not a full-table scan.

    ``state NOT IN (...)`` cannot use ``idx_review_action_outbox_state`` the
    way an exact ``state=?`` predicate can, so on a large table it falls back
    to ``SCAN review_action_outbox`` -- unbounded work hidden behind a
    ``LIMIT`` -- and it also treats any unrecognized state as reservable.
    Every query below must instead resolve to an indexed ``SEARCH`` with no
    fallback ``TEMP B-TREE`` sort, for every state bucket the reserve and
    reconcile paths actually query.
    """
    db = tmp_path / "large.sqlite"
    for index in range(300):
        review_lifecycle.create_or_replay_chain(
            db,
            target_task_id=f"PLAN-{index}",
            target_request_id=f"req-plan-{index}",
            claim_epoch=1,
            packet_sha256=f"{index % 10}" * 64,
            candidate_sha256=f"{(index + 1) % 10}" * 64,
            now=NOW,
        )

    now_text = NOW.isoformat(timespec="microseconds")
    queries = [
        "SELECT * FROM review_action_outbox WHERE state='pending' "
        "ORDER BY action_id LIMIT 256",
        "SELECT * FROM review_action_outbox WHERE state='pending' "
        "AND action_id > 0 ORDER BY action_id LIMIT 256",
        "SELECT * FROM review_action_outbox WHERE state='reserved' "
        f"AND lease_expires_at<='{now_text}' "
        "ORDER BY lease_expires_at, action_id LIMIT 256",
        "SELECT action_id, chain_id, action_index FROM review_action_outbox "
        "WHERE state='failed' AND action_id > 0 ORDER BY action_id LIMIT 256",
        "SELECT action_id, chain_id, action_index FROM review_action_outbox "
        "WHERE state='failed' ORDER BY action_id LIMIT 256",
    ]
    conn = sqlite3.connect(db)
    try:
        for query in queries:
            plan_text = " | ".join(
                str(row) for row in conn.execute("EXPLAIN QUERY PLAN " + query)
            )
            assert "SCAN" not in plan_text, plan_text
            assert "TEMP B-TREE" not in plan_text, plan_text
            assert "idx_review_action_outbox_state" in plan_text, plan_text
    finally:
        conn.close()


class _RecordingManager:
    """Minimal ``review_orchestrator.Manager`` that must not be asked to launch."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.events: list[dict] = []

    def _append_event(self, event: dict) -> None:
        self.events.append(dict(event))

    def launch_quality_reviewer(self, **kwargs: object) -> dict:
        raise AssertionError("launch_quality_reviewer must not run with max_actions=0")

    def accept_review(self, request_id: str, task_id: str, **kwargs: object) -> dict:
        raise AssertionError("accept_review must not run with max_actions=0")

    def reject_review(self, task_id: str, reason: str, *, to: str = "pending") -> dict:
        raise AssertionError("reject_review must not run with max_actions=0")

    def status(self, request_id: str) -> dict:
        raise AssertionError("status must not run with max_actions=0")


def test_orchestrator_drain_reconciles_dead_chain_descendants_before_reserving(
    tmp_path: Path,
) -> None:
    """``ReviewOrchestrator.drain`` must retire a failed chain's descendants
    even when ``max_actions=0`` leaves no room to reserve or launch anything,
    proving the bounded reconciliation pass is wired ahead of the reservation
    loop rather than folded into it.
    """
    db = tmp_path / "task.sqlite"
    _chain(db)
    leaked = _reserve(db)
    assert leaked.action_index == 0
    assert review_lifecycle.fail_action(
        db,
        action_id=leaked.action_id,
        owner="worker-a",
        lease_token="lease-a",
        reason="boom",
        now=NOW,
    )
    manager = _RecordingManager(tmp_path)
    orchestrator = review_orchestrator.ReviewOrchestrator(manager, db_path=db)
    result = orchestrator.drain(max_actions=0, now=NOW + timedelta(seconds=1))
    assert result.attempted == 0
    counts = review_lifecycle.lifecycle_counts(db)
    assert counts["failed"] == 1
    assert counts["retired"] == len(review_lifecycle.PLAN) - 1
    assert counts["pending"] == 0
    assert counts["pending_parked"] == 0
    assert manager.events == []


def test_orchestrator_mechanical_failure_reason_still_short_circuits_review() -> None:
    """The mechanical short-circuit this rework must preserve still fires only
    on a positive, in-epoch measured failure and fails closed on everything
    else -- unrelated to, and unaffected by, the new reconciliation wiring.
    """
    card = {
        "terminal_review": {
            "deterministic_verification": {
                "applicable": True,
                "pass": False,
                "claim_epoch": "7",
                "evidence_verdict": {
                    "nothing_measured": False,
                    "failed_validation_count": 2,
                    "missing_required_output_count": 0,
                },
            }
        }
    }
    assert review_orchestrator.mechanical_failure_reason(card, "7") == (
        "mechanically_failing_candidate:failed_validation_count=2,"
        "missing_required_output_count=0"
    )
    assert review_orchestrator.mechanical_failure_reason(card, "8") == ""
    stale_evidence = {
        "terminal_review": {
            "deterministic_verification": {
                "applicable": True,
                "pass": False,
                "claim_epoch": "7",
                "evidence_verdict": {"nothing_measured": True},
            }
        }
    }
    assert review_orchestrator.mechanical_failure_reason(stale_evidence, "7") == ""
    assert review_orchestrator.mechanical_failure_reason({}, "7") == ""


def test_orchestrator_routing_catalog_cache_reset_still_clears_memoised_entries() -> None:
    """``reset_routing_catalog_cache`` this rework must preserve still empties
    the per-pass memoisation dict ``select_reviewer_route`` relies on.
    """
    review_orchestrator._ROUTING_CATALOG_CACHE["test-repo"] = (0.0, {"workers": ["w"]})
    review_orchestrator.reset_routing_catalog_cache()
    assert review_orchestrator._ROUTING_CATALOG_CACHE == {}
