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

from aiworkhub import review_lifecycle  # noqa: E402


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


def test_reserve_fails_closed_when_terminal_pending_action_is_missing(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
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

    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="descriptor_tamper"):
        review_lifecycle.reserve_next_action(
            db,
            owner="worker-terminal",
            lease_token="lease-terminal",
            now=NOW + timedelta(seconds=12),
        )


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
