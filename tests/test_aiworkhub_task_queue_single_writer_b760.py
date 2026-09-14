"""NF-2026-00760: one path-keyed writer lease for canonical task_queue.sqlite."""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import (
    callback_store,
    db_writer,
    dependency_autolaunch,
    review_lifecycle,
    review_orchestrator,
    task_engine,
    task_store,
)

_SRC = Path(__file__).resolve().parents[1] / "src"
_PACKET = "a" * 64
_CANDIDATE = "b" * 64
_LEASED_CONNECT_OWNERS = {
    "callback_store.py": "open_db",
    "review_lifecycle.py": "_connect",
    "review_orchestrator.py": "_side_table_connection",
    "core.py": "_canonical_connect",
    "dependency_autolaunch.py": "_write_connection",
}


def _is_sqlite_connect(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "connect"
        and isinstance(func.value, ast.Name)
        and func.value.id == "sqlite3"
    )


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str:
    best = ""
    best_line = -1
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= target.lineno <= end and node.lineno >= best_line:
            best = node.name
            best_line = node.lineno
    return best


def test_sqlite_connect_only_inside_leased_factories() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "aiworkhub"
    for filename, owner in _LEASED_CONNECT_OWNERS.items():
        path = root / filename
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not _is_sqlite_connect(node):
                continue
            assert _enclosing_function(tree, node) == owner, (
                f"{filename}:{node.lineno} sqlite3.connect bypasses {owner}"
            )
        assert "write_lease" in text



def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    return repo

def _db(repo: Path) -> Path:
    return Path(task_store.storage_readiness(repo).canonical_db)


def test_nested_module_lease_is_reentrant(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    db = _db(repo)
    conn = callback_store.open_db(db)
    try:
        callback_store.init_db(conn)
        with db_writer.write_lease(db, timeout_s=5) as inner:
            assert inner["reentrant"] is True
    finally:
        conn.close()


def test_pure_review_reads_do_not_acquire_writer_lease(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    db = _db(repo)
    chain = review_lifecycle.create_or_replay_chain(
        db,
        target_task_id="TASK_READ",
        target_request_id="req-read",
        claim_epoch="1",
        packet_sha256=_PACKET,
        candidate_sha256=_CANDIDATE,
        now=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    db_writer.reset_stats()
    actions = review_lifecycle.actions_for_chain(db, chain.chain_id)
    receipts = review_lifecycle.completed_receipts_for_chain(db, chain.chain_id)
    listed = task_store.list_tasks(repo)
    assert len(actions) == len(review_lifecycle.PLAN)
    assert receipts == ()
    assert listed == []
    assert db_writer.stats().get("acquired", 0.0) == 0.0


def test_distinct_databases_are_not_serialized_together(tmp_path: Path) -> None:
    first = tmp_path / "a.sqlite"
    second = tmp_path / "b.sqlite"
    inside: list[Path] = []
    overlap: list[int] = []
    barrier = threading.Barrier(2)

    def hold(path: Path) -> None:
        with db_writer.write_lease(path, timeout_s=5):
            inside.append(path)
            barrier.wait()
            if len(inside) == 2:
                overlap.append(1)
            barrier.wait()
            inside.remove(path)

    threads = [
        threading.Thread(target=hold, args=(first,)),
        threading.Thread(target=hold, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert overlap


def test_launch_does_not_run_under_writer_lease(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    db = _db(repo)
    now = datetime.now(timezone.utc).isoformat()
    parent = {
        "task_id": "TASK_PARENT",
        "runner": "codex",
        "topic": "task_mcp",
        "status": "success",
        "worker_status": "accepted",
        "depends_on": [],
    }
    child = {
        "task_id": "TASK_CHILD",
        "runner": "codex",
        "topic": "task_mcp",
        "status": "pending",
        "worker_status": "unclaimed",
        "depends_on": ["TASK_PARENT"],
        "origin_thread_id": "thread-child",
        "coordinator_provider": "codex",
    }
    conn = sqlite3.connect(str(db))
    try:
        for card, status, worker in (
            (parent, "success", "accepted"),
            (child, "pending", "unclaimed"),
        ):
            conn.execute(
                "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
                "card_json, created_at, updated_at, origin_thread_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    card["task_id"],
                    card["runner"],
                    card["topic"],
                    status,
                    worker,
                    json.dumps(card),
                    now,
                    now,
                    card.get("origin_thread_id", ""),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    depth: list[int] = []
    real = db_writer.write_lease

    @contextmanager
    def wrapped(db_path, *, timeout_s=None):
        depth.append(1)
        try:
            with real(db_path, timeout_s=timeout_s) as receipt:
                yield receipt
        finally:
            depth.pop()

    def launch(task_id: str, runner: str, topic: str, request_id: str) -> dict[str, object]:
        assert depth == [], "writer lease held across launch"
        return {"ok": False, "stderr": "capacity"}

    db_writer.write_lease = wrapped  # type: ignore[method-assign]
    try:
        outcome = dependency_autolaunch.reconcile(repo, trigger_task_id="TASK_PARENT", launch=launch)
    finally:
        db_writer.write_lease = real  # type: ignore[method-assign]
    assert outcome["ok"] is True
    assert outcome["delayed"]


_CHILD = r"""
import json, sys
sys.path.insert(0, %(src)r)
from datetime import datetime, timezone
from pathlib import Path
from aiworkhub import callback_store, review_lifecycle, review_orchestrator, task_store

db = Path(%(db)r)
stamp = Path(%(stamp)r)
idx = %(idx)d
errors = []
created = 0
enqueued = 0
try:
    for n in range(8):
        task_id = f"TASK_W{idx}_{n}"
        review_lifecycle.create_or_replay_chain(
            db,
            target_task_id=task_id,
            target_request_id=f"req-{idx}-{n}",
            claim_epoch=str(idx),
            packet_sha256="a" * 64,
            candidate_sha256="b" * 64,
            now=datetime(2026, 9, 10, tzinfo=timezone.utc),
        )
        created += 1
        conn = callback_store.open_db(db)
        try:
            callback_store.init_db(conn)
            now = callback_store.utc_now()
            card = {
                "task_id": task_id,
                "status": "review",
                "worker_status": "review",
                "origin_thread_id": f"thread-{idx}",
                "claim_epoch": 0,
            }
            conn.execute(
                "INSERT OR IGNORE INTO tasks(task_id, runner, topic, status, worker_status, "
                "priority, objective, card_json, created_at, updated_at, origin_thread_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id, "codex", "task_mcp", "review", "review", "high", "mix",
                    json.dumps(card), now, now, f"thread-{idx}",
                ),
            )
            conn.commit()
            if callback_store.enqueue_callback(
                conn, task_id, f"thread-{idx}", "review_ready",
                provider="codex", episode_id="0",
            ):
                enqueued += 1
        finally:
            conn.close()
        chain = review_lifecycle.create_or_replay_chain(
            db,
            target_task_id=task_id,
            target_request_id=f"req-{idx}-{n}",
            claim_epoch=str(idx),
            packet_sha256="a" * 64,
            candidate_sha256="b" * 64,
        )
        review_orchestrator.bind_lens_plan(
            db, chain_id=chain.chain_id, lenses=("correctness",),
        )
        review_orchestrator.required_lenses(db, chain.chain_id)
        task_store.list_tasks(Path(%(repo)r), limit=20)
except Exception as exc:
    errors.append(f"{type(exc).__name__}:{exc}")
stamp.write_text(json.dumps({"created": created, "enqueued": enqueued, "errors": errors}), encoding="utf-8")
"""


def test_multiprocess_mixed_mutations_have_zero_lock_failures(tmp_path: Path) -> None:
    if db_writer.backend() == "none":
        pytest.skip("no cross-process locking primitive on this host")
    repo = _init_repo(tmp_path)
    db = _db(repo)
    workers = 4
    scripts = []
    stamps = []
    for idx in range(workers):
        stamp = tmp_path / f"stamp-{idx}.json"
        script = tmp_path / f"child-{idx}.py"
        script.write_text(
            _CHILD
            % {
                "src": str(_SRC),
                "db": str(db),
                "stamp": str(stamp),
                "idx": idx,
                "repo": str(repo),
            },
            encoding="utf-8",
        )
        scripts.append(script)
        stamps.append(stamp)
    procs = [subprocess.Popen([sys.executable, str(script)]) for script in scripts]
    for proc in procs:
        assert proc.wait(timeout=120) == 0
    created = 0
    enqueued = 0
    for stamp in stamps:
        payload = json.loads(stamp.read_text(encoding="utf-8"))
        assert payload["errors"] == [], payload["errors"]
        created += int(payload["created"])
        enqueued += int(payload["enqueued"])
    conn = callback_store.open_db(db)
    try:
        chains = conn.execute("SELECT COUNT(*) FROM review_chains").fetchone()[0]
        outbox = conn.execute("SELECT COUNT(*) FROM callback_outbox").fetchone()[0]
    finally:
        conn.close()
    assert chains == created
    assert outbox == enqueued
    assert created == workers * 8


# --------------------------------------------------------------------------
# NF-2026-00846: the launch path's own writers
#
# NF-2026-00760 leased the stores that open their own connections. It did not
# reach ``task_store._connect``, which is where ``task_engine`` opens the six
# claim/settlement writers an ``aiworkhub_agent_launch_task`` traverses
# (``record_launch_blocker``, ``mark_launch_failed``, ``mark_terminal_failure``,
# ``mark_review_workspace_missing``, ``accept_review``,
# ``disposition_reviewer_children``) -- so concurrent isolated workers still
# raced into ``PRAGMA journal_mode=WAL`` and the claim CAS unserialized, and a
# loser surfaced a raw ``database is locked`` BEFORE any provider was launched.
# --------------------------------------------------------------------------

_NF846_RUNNER = "codex"
_NF846_TOPIC = "task_mcp"
_NF846_REQUEST = "nf846" + "0" * 27


def _open_write_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the write gate. It has its own tests; this file is about
    serialization, and a gate denial would answer before SQLite is touched."""
    monkeypatch.setattr(
        task_engine.core, "_canonical_write_gate", lambda *a, **k: None
    )


def _seed_pending_task(db: Path, task_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    card = {
        "task_id": task_id,
        "runner": _NF846_RUNNER,
        "topic": _NF846_TOPIC,
        "status": "pending",
        "worker_status": "unclaimed",
    }
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, origin_thread_id) "
            "VALUES (?,?,?,'pending','unclaimed','high','nf846',?,?,?,'')",
            (task_id, _NF846_RUNNER, _NF846_TOPIC, json.dumps(card, sort_keys=True), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _claim_facts(db: Path, task_id: str) -> dict[str, object]:
    """Durable identity of the claim, read outside every writer."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, worker_status, claimed_by, card_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND event='claim_start'",
            (task_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    card = json.loads(row["card_json"] or "{}") if row is not None else {}
    return {
        "status": None if row is None else str(row["status"]),
        "worker_status": None if row is None else str(row["worker_status"]),
        "claimed_by": None if row is None else str(row["claimed_by"] or ""),
        "claim_epoch": card.get("claim_epoch"),
        "launch_request_id": card.get("launch_request_id"),
        "claim_start_events": int(events),
    }


def _await_flag(flag: Path, *, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if flag.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"child never signalled {flag.name}")


_UNLEASED_HOLDER = r"""
import sqlite3, time
from pathlib import Path

conn = sqlite3.connect(%(db)r, timeout=0.0)
conn.execute("PRAGMA busy_timeout=0")
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE tasks SET updated_at=updated_at WHERE task_id=%(task)r")
Path(%(ready)r).write_text("held", encoding="utf-8")
time.sleep(%(hold)f)
conn.rollback()
conn.close()
"""


_LEASED_HOLDER = r"""
import sys, time
sys.path.insert(0, %(src)r)
from pathlib import Path
from aiworkhub import task_store

conn = task_store._connect(Path(%(db)r))
try:
    conn.execute("UPDATE tasks SET updated_at=updated_at WHERE task_id=%(task)r")
    Path(%(ready)r).write_text("held", encoding="utf-8")
    time.sleep(%(hold)f)
    conn.rollback()
finally:
    conn.close()
"""


def _spawn(tmp_path: Path, name: str, source: str) -> subprocess.Popen:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return subprocess.Popen([sys.executable, str(script)])


def test_unleased_writer_reproduces_the_raw_database_is_locked_failure(
    tmp_path: Path,
) -> None:
    """The 0.11.36 failure, reproduced exactly.

    This is the pre-fix shape: a write-capable connection to the canonical
    queue opened and used WITHOUT the cross-process lease while a second
    process holds the write lock. It must still fail, otherwise the leased
    assertions below would be proving nothing about a scenario that no longer
    produces contention.
    """
    repo = _init_repo(tmp_path)
    db = _db(repo)
    task_id = "TASK_NF846_RAW"
    _seed_pending_task(db, task_id)
    ready = tmp_path / "raw-held.flag"
    holder = _spawn(
        tmp_path,
        "raw_holder.py",
        _UNLEASED_HOLDER
        % {"db": str(db), "task": task_id, "ready": str(ready), "hold": 3.0},
    )
    try:
        _await_flag(ready)
        raw = sqlite3.connect(str(db), timeout=0.0)
        try:
            raw.execute("PRAGMA busy_timeout=100")
            with pytest.raises(sqlite3.OperationalError) as err:
                raw.execute("BEGIN IMMEDIATE")
                raw.execute(
                    "UPDATE tasks SET updated_at='x' WHERE task_id=?", (task_id,)
                )
            assert "locked" in str(err.value).lower()
        finally:
            raw.close()
    finally:
        assert holder.wait(timeout=120) == 0
    # Nothing durable happened: the loser produced an exception, not a claim.
    assert _claim_facts(db, task_id)["claim_start_events"] == 0


def test_leased_launch_claim_survives_the_same_contention_with_one_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same bounded scenario, now serialized: no raw lock, one claim identity.

    The competing process holds a real write transaction on the canonical
    queue for longer than any ``busy_timeout`` would tolerate. Because it
    obtained that connection through the leased ``task_store._connect``, the
    launch-path claim QUEUES behind it instead of racing it, and then commits
    exactly one claim epoch bound to exactly one request id.
    """
    repo = _init_repo(tmp_path)
    db = _db(repo)
    task_id = "TASK_NF846_LEASED"
    _seed_pending_task(db, task_id)
    _open_write_gate(monkeypatch)
    ready = tmp_path / "leased-held.flag"
    holder = _spawn(
        tmp_path,
        "leased_holder.py",
        _LEASED_HOLDER
        % {
            "src": str(_SRC),
            "db": str(db),
            "task": task_id,
            "ready": str(ready),
            "hold": 1.5,
        },
    )
    try:
        _await_flag(ready)
        result = task_engine.claim_start_exact(
            repo, task_id, _NF846_RUNNER, _NF846_TOPIC, request_id=_NF846_REQUEST
        )
    finally:
        assert holder.wait(timeout=120) == 0

    assert result["ok"] is True, result.get("stderr")
    assert "locked" not in str(result.get("stderr") or "").lower()
    facts = _claim_facts(db, task_id)
    assert facts["status"] == "processing"
    assert facts["worker_status"] == "claimed"
    assert facts["claimed_by"] == _NF846_RUNNER
    assert facts["claim_epoch"] == 1
    assert facts["launch_request_id"] == _NF846_REQUEST
    assert facts["claim_start_events"] == 1

    # Re-presenting the SAME request reconciles onto the committed claim; it
    # never takes a second epoch, which is what makes the retry below safe.
    replay = task_engine.claim_start_exact(
        repo, task_id, _NF846_RUNNER, _NF846_TOPIC, request_id=_NF846_REQUEST
    )
    assert replay["ok"] is True
    assert replay.get("claim_reconciled") is True
    assert _claim_facts(db, task_id) == facts


_CONCURRENT_CLAIM = r"""
import json, sys
sys.path.insert(0, %(src)r)
from pathlib import Path
from aiworkhub import core, task_engine

core._canonical_write_gate = lambda *a, **k: None
stamp = Path(%(stamp)r)
payload = {"error": "", "ok": None, "stderr": "", "reconciled": None}
try:
    result = task_engine.claim_start_exact(
        Path(%(repo)r), %(task)r, %(runner)r, %(topic)r, request_id=%(request)r,
    )
    payload["ok"] = bool(result.get("ok"))
    payload["stderr"] = str(result.get("stderr") or "")
    payload["reconciled"] = bool(result.get("claim_reconciled"))
except BaseException as exc:
    payload["error"] = f"{type(exc).__name__}:{exc}"
stamp.write_text(json.dumps(payload), encoding="utf-8")
"""


def test_concurrent_isolated_workers_produce_no_raw_lock_and_one_claim(
    tmp_path: Path,
) -> None:
    """Four separate OS processes claim the same task with the same request id.

    This is the collision-free relaunch shape that produced the outage: every
    worker is a distinct supervisor process, so nothing in-process could have
    made them take turns. The lease must leave exactly one claim epoch, one
    ``claim_start`` event and one request identity, and no worker may see a
    raw lock error.
    """
    if db_writer.backend() == "none":
        pytest.skip("no cross-process locking primitive on this host")
    repo = _init_repo(tmp_path)
    db = _db(repo)
    task_id = "TASK_NF846_CONCURRENT"
    _seed_pending_task(db, task_id)
    workers = 4
    stamps = [tmp_path / f"claim-{idx}.json" for idx in range(workers)]
    procs = [
        _spawn(
            tmp_path,
            f"claimer-{idx}.py",
            _CONCURRENT_CLAIM
            % {
                "src": str(_SRC),
                "repo": str(repo),
                "task": task_id,
                "runner": _NF846_RUNNER,
                "topic": _NF846_TOPIC,
                "request": _NF846_REQUEST,
                "stamp": str(stamp),
            },
        )
        for idx, stamp in enumerate(stamps)
    ]
    for proc in procs:
        assert proc.wait(timeout=180) == 0

    payloads = [json.loads(stamp.read_text(encoding="utf-8")) for stamp in stamps]
    for payload in payloads:
        assert payload["error"] == "", payload["error"]
        assert payload["ok"] is True, payload["stderr"]
        assert "locked" not in payload["stderr"].lower()
    # One fresh claim, the rest reconciled onto it -- never a second epoch.
    assert sum(1 for p in payloads if not p["reconciled"]) == 1

    facts = _claim_facts(db, task_id)
    assert facts["claim_start_events"] == 1
    assert facts["claim_epoch"] == 1
    assert facts["launch_request_id"] == _NF846_REQUEST


def test_pre_provider_contention_yields_a_typed_receipt_not_a_raw_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged holder is bounded, typed and durable -- and launches nothing."""
    repo = _init_repo(tmp_path)
    db = _db(repo)
    task_id = "TASK_NF846_WEDGED"
    _seed_pending_task(db, task_id)
    _open_write_gate(monkeypatch)
    attempts: list[str] = []

    def always_contended(db_path, *, timeout_s=None):  # type: ignore[no-untyped-def]
        attempts.append(str(db_path))
        raise db_writer.WriteLeaseTimeout(str(db_path), 30.0, 30.0)

    monkeypatch.setattr(db_writer, "write_lease", always_contended)
    result = task_engine.claim_start_exact(
        repo, task_id, _NF846_RUNNER, _NF846_TOPIC, request_id=_NF846_REQUEST
    )

    assert result["ok"] is False
    assert result["stderr"].startswith("task_queue_write_contention:")
    assert f"request_id={_NF846_REQUEST}" in result["stderr"]
    receipt = result["task_queue_contention"]
    assert receipt["schema_id"] == task_engine.CLAIM_CONTENTION_SCHEMA
    assert receipt["task_id"] == task_id
    assert receipt["request_id"] == _NF846_REQUEST
    assert receipt["error_class"] == "WriteLeaseTimeout"
    assert receipt["provider_launched"] is False
    assert receipt["retryable"] is True
    # Bounded, not an unbounded retry, and the task is untouched.
    assert len(attempts) == task_engine.CLAIM_CONTENTION_ATTEMPTS
    assert set(attempts) == {str(db)}
    facts = _claim_facts(db, task_id)
    assert facts["status"] == "pending"
    assert facts["worker_status"] == "unclaimed"
    assert facts["claim_start_events"] == 0


def test_bounded_retry_converges_on_exactly_one_claim_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient contended attempt is replayed under the SAME request id."""
    repo = _init_repo(tmp_path)
    db = _db(repo)
    task_id = "TASK_NF846_TRANSIENT"
    _seed_pending_task(db, task_id)
    _open_write_gate(monkeypatch)
    real = db_writer.write_lease
    calls: list[int] = []

    def flaky(db_path, *, timeout_s=None):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) == 1:
            raise db_writer.WriteLeaseTimeout(str(db_path), 0.0, 0.0)
        return real(db_path, timeout_s=timeout_s)

    monkeypatch.setattr(db_writer, "write_lease", flaky)
    result = task_engine.claim_start_exact(
        repo, task_id, _NF846_RUNNER, _NF846_TOPIC, request_id=_NF846_REQUEST
    )

    assert result["ok"] is True, result.get("stderr")
    assert len(calls) > 1
    facts = _claim_facts(db, task_id)
    assert facts["claim_epoch"] == 1
    assert facts["claim_start_events"] == 1
    assert facts["launch_request_id"] == _NF846_REQUEST


def test_a_real_sqlite_fault_is_never_retried_or_reclassified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only contention is mechanical. A genuine fault must keep failing."""
    repo = _init_repo(tmp_path)
    db = _db(repo)
    task_id = "TASK_NF846_FAULT"
    _seed_pending_task(db, task_id)
    _open_write_gate(monkeypatch)
    calls: list[int] = []
    real_connect = task_store._connect

    def broken(path, **kwargs):  # type: ignore[no-untyped-def]
        # Readiness reads must keep working: only the WRITER faults here.
        if kwargs.get("readonly"):
            return real_connect(path, **kwargs)
        calls.append(1)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(task_store, "_connect", broken)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        task_engine.claim_start_exact(
            repo, task_id, _NF846_RUNNER, _NF846_TOPIC, request_id=_NF846_REQUEST
        )
    assert len(calls) == 1
    # A failed attempt must not strand the lease it took. The probe runs on
    # another THREAD because the lease is re-entrant within one thread.
    released: list[bool] = []

    def probe() -> None:
        try:
            with db_writer.write_lease(db, timeout_s=1.0):
                released.append(True)
        except db_writer.WriteLeaseTimeout:
            released.append(False)

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(timeout=30)
    assert released == [True]


def test_write_capable_task_store_connections_hold_the_lease(tmp_path: Path) -> None:
    """The closure itself: opening a writer takes the lease, closing releases it.

    The probe runs on another THREAD because the lease is deliberately
    re-entrant within one (process, thread, path).
    """
    repo = _init_repo(tmp_path)
    db = _db(repo)
    db_writer.reset_stats()
    conn = task_store._connect(db)
    outcome: list[bool] = []

    def probe() -> None:
        try:
            with db_writer.write_lease(db, timeout_s=0.2):
                outcome.append(True)
        except db_writer.WriteLeaseTimeout:
            outcome.append(False)

    try:
        assert db_writer.stats()["acquired"] >= 1.0
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=30)
        assert outcome == [False]
    finally:
        conn.close()
    with db_writer.write_lease(db, timeout_s=5) as receipt:
        assert receipt["reentrant"] is False


def test_readonly_task_store_connections_are_never_serialized(tmp_path: Path) -> None:
    """WAL readers must stay concurrent with the single writer."""
    repo = _init_repo(tmp_path)
    db = _db(repo)
    db_writer.reset_stats()
    conn = task_store._connect(db, readonly=True)
    outcome: list[bool] = []

    def probe() -> None:
        with db_writer.write_lease(db, timeout_s=5):
            outcome.append(True)

    try:
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        assert db_writer.stats().get("acquired", 0.0) == 0.0
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=30)
        assert outcome == [True]
    finally:
        conn.close()


def test_every_launch_settlement_path_runs_under_the_writer_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The six ``task_engine`` writers a launch reaches, each proven leased.

    These opened ``task_store._connect`` directly and so were the writers
    NF-2026-00760's lease never covered.
    """
    repo = _init_repo(tmp_path)
    db = _db(repo)
    task_id = "TASK_NF846_SETTLE"
    _seed_pending_task(db, task_id)
    _open_write_gate(monkeypatch)
    leases: list[str] = []
    real = db_writer.write_lease

    @contextmanager
    def traced(db_path, *, timeout_s=None):  # type: ignore[no-untyped-def]
        leases.append(str(Path(db_path).resolve()))
        with real(db_path, timeout_s=timeout_s) as receipt:
            yield receipt

    monkeypatch.setattr(db_writer, "write_lease", traced)
    settlements = {
        "record_launch_blocker": lambda: task_engine.record_launch_blocker(
            repo,
            task_id,
            _NF846_RUNNER,
            _NF846_TOPIC,
            adapter_id="codex_cli",
            reason="nf846_probe",
            request_id=_NF846_REQUEST,
        ),
        "mark_launch_failed": lambda: task_engine.mark_launch_failed(
            repo, task_id, _NF846_RUNNER, reason="nf846_probe"
        ),
        "mark_terminal_failure": lambda: task_engine.mark_terminal_failure(
            repo, task_id, _NF846_RUNNER, "worker_failed"
        ),
        "mark_review_workspace_missing": lambda: task_engine.mark_review_workspace_missing(
            repo, task_id, _NF846_RUNNER, _NF846_REQUEST, reason="nf846_probe"
        ),
        "accept_review": lambda: task_engine.accept_review(
            repo,
            task_id,
            runner=_NF846_RUNNER,
            topic=_NF846_TOPIC,
            request_id=_NF846_REQUEST,
        ),
        "disposition_reviewer_children": lambda: task_engine.disposition_reviewer_children(
            repo,
            task_id,
            verified_reviewer_task_ids=[],
            parent_request_id=_NF846_REQUEST,
        ),
    }
    expected = str(Path(db).resolve())
    for name, call in settlements.items():
        leases.clear()
        call()
        assert leases, f"{name} opened a canonical writer without the lease"
        assert set(leases) == {expected}, f"{name} leased {set(leases)}"
