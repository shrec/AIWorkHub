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
