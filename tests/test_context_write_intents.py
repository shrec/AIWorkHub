from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import context_write_intents, core, process_launcher, storage_registry, task_store
from aiworkhub import worker_ai_tools_mcp as worker_tools


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _ctx(tmp_path: Path, repo: Path) -> worker_tools.WorkerToolContext:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    ledger = runtime / "audit.jsonl"
    ledger.write_text("", encoding="utf-8")
    key = runtime / "audit.key"
    key.write_bytes(b"k" * 32)
    return worker_tools.WorkerToolContext(
        task_id="TASK_INTENT_1",
        runner="worker_intent_1",
        topic="context_intents",
        request_id="a" * 32,
        repo=tmp_path,
        authority_repo=repo,
        source_graph_targets=(),
        session_topic="AIWorkHub context lifecycle",
        audit_ledger_path=ledger,
        audit_hmac_key_path=key,
    )


def _read(ctx: worker_tools.WorkerToolContext) -> list[dict]:
    return context_write_intents.read_verified_intents(
        ledger_path=ctx.audit_ledger_path,
        key_path=ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
        authority_repo=ctx.authority_repo,
    )


def _append_session(ctx: worker_tools.WorkerToolContext, index: int) -> dict:
    return context_write_intents.append_intent(
        ledger_path=ctx.audit_ledger_path,
        key_path=ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
        authority_repo=ctx.authority_repo,
        component="session",
        action="checkpoint",
        payload={"topic": ctx.session_topic, "content": f"capacity checkpoint {index}"},
        idempotency_key=f"session:capacity:{index:04d}",
        provenance="capacity evidence",
    )


def test_worker_proposal_never_mutates_canonical_context_until_manager_accepts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ctx = _ctx(tmp_path, repo)

    proposed = worker_tools.ai_memory_write_intent(
        ctx,
        action="remember",
        key="decision.worker.intent",
        value="manager must accept",
        tags="context,intent",
        idempotency_key="memory:worker:intent:0001",
        provenance="worker task evidence",
    )
    assert proposed["ok"] is True
    assert proposed["status"] == "pending_manager_review"

    memory_db = storage_registry.resolve_database_path(
        storage_registry.load_storage_registry(repo), "memory",
    )
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0

    intents = _read(ctx)
    assert len(intents) == 1
    task_db = storage_registry.resolve_database_path(
        storage_registry.load_storage_registry(repo), "task_queue",
    )
    assert context_write_intents.decisions(repo, request_id=ctx.request_id) == {}
    with sqlite3.connect(task_db) as con:
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_write_intent_decisions'"
        ).fetchone() is None
    applied = context_write_intents.apply_accepted_intent(
        repo,
        intent=intents[0],
        manager_provider="codex",
        manager_session_id="thread-manager-1",
    )
    decision = context_write_intents.record_decision(
        repo,
        intent=intents[0],
        decision="accepted",
        reason="verified task evidence",
        manager_provider="codex",
        manager_session_id="thread-manager-1",
        result=applied,
    )
    assert decision["ok"] is True
    assert decision["decision"] == "accepted"
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT value FROM memories WHERE key='decision.worker.intent'").fetchone()[0] == "manager must accept"


def test_intents_are_hmac_verified_deduplicated_and_decisions_are_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ctx = _ctx(tmp_path, repo)
    kwargs = dict(
        action="checkpoint",
        content="bounded checkpoint",
        idempotency_key="session:worker:intent:0001",
        provenance="worker evidence",
    )
    first = worker_tools.session_write_intent(ctx, **kwargs)
    second = worker_tools.session_write_intent(ctx, **kwargs)
    assert first["intent_id"] == second["intent_id"]
    intents = _read(ctx)
    assert len(intents) == 1

    rejected = context_write_intents.record_decision(
        repo,
        intent=intents[0],
        decision="rejected",
        reason="not durable project knowledge",
        manager_provider="codex",
        manager_session_id="thread-manager-1",
        result={"ok": True, "applied": False},
    )
    repeated = context_write_intents.record_decision(
        repo,
        intent=intents[0],
        decision="rejected",
        reason="not durable project knowledge",
        manager_provider="codex",
        manager_session_id="thread-manager-1",
        result={"ok": True, "applied": False},
    )
    assert rejected["idempotent"] is False
    assert repeated["idempotent"] is True
    with pytest.raises(context_write_intents.ContextWriteIntentError, match="intent_already_disposed"):
        context_write_intents.record_decision(
            repo,
            intent=intents[0],
            decision="accepted",
            reason="conflicting decision",
            manager_provider="codex",
            manager_session_id="thread-manager-1",
            result={"ok": True},
        )

    rows = ctx.audit_ledger_path.read_text(encoding="utf-8").splitlines()
    forged = json.loads(rows[0])
    forged["payload"]["content"] = "tampered"
    ctx.audit_ledger_path.write_text(json.dumps(forged) + "\n", encoding="utf-8")
    assert _read(ctx) == []
    with pytest.raises(
        context_write_intents.ContextWriteIntentError,
        match="intent_ledger_hmac_invalid",
    ):
        _append_session(ctx, 1)
    assert len(ctx.audit_ledger_path.read_text(encoding="utf-8").splitlines()) == 1


def test_registered_worker_write_intents_expose_no_identity_or_path_override(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ctx = _ctx(tmp_path, repo)

    class FakeMcp:
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}

        def tool(self, *, name: str, description: str | None = None):
            def decorator(fn):
                self.registered[name] = fn
                return fn
            return decorator

    fake = FakeMcp()
    worker_tools.register_tools(fake, ctx)
    for name in (
        "aiworkhub_worker_session_write_intent",
        "aiworkhub_worker_ai_memory_write_intent",
        "aiworkhub_worker_kb_write_intent",
    ):
        assert name in fake.registered
        params = set(__import__("inspect").signature(fake.registered[name]).parameters)
        assert not params.intersection({"repo", "repo_root", "task_id", "runner", "topic", "request_id", "db_path"})


def test_capacity_overflow_preserves_oldest_retry_and_manager_disposition(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ctx = _ctx(tmp_path, repo)

    submitted = [
        _append_session(ctx, index)
        for index in range(context_write_intents.MAX_INTENTS_PER_REQUEST)
    ]
    assert all(row["idempotent"] is False for row in submitted)
    visible = _read(ctx)
    assert len(visible) == context_write_intents.MAX_INTENTS_PER_REQUEST
    assert [row["intent_id"] for row in visible] == [row["intent_id"] for row in submitted]

    with pytest.raises(
        context_write_intents.ContextWriteIntentError,
        match="intent_capacity_exceeded",
    ):
        _append_session(ctx, context_write_intents.MAX_INTENTS_PER_REQUEST)
    assert len(ctx.audit_ledger_path.read_text(encoding="utf-8").splitlines()) == len(submitted)

    oldest_retry = _append_session(ctx, 0)
    assert oldest_retry["intent_id"] == submitted[0]["intent_id"]
    assert oldest_retry["idempotent"] is True
    assert len(ctx.audit_ledger_path.read_text(encoding="utf-8").splitlines()) == len(submitted)

    decision = context_write_intents.record_decision(
        repo,
        intent=visible[0],
        decision="accepted",
        reason="oldest remains manager visible",
        manager_provider="codex",
        manager_session_id="thread-manager-1",
        result={"ok": True, "applied": False},
    )
    assert decision["decision"] == "accepted"


def test_concurrent_idempotent_appends_create_one_physical_capacity_record(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ctx = _ctx(tmp_path, repo)

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(_append_session, ctx, 7) for _ in range(16)]
        results = [future.result() for future in as_completed(futures)]

    assert {result["intent_id"] for result in results} == {results[0]["intent_id"]}
    assert sum(result["idempotent"] is False for result in results) == 1
    assert sum(result["idempotent"] is True for result in results) == 15
    assert len(ctx.audit_ledger_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(_read(ctx)) == 1


def test_concurrent_distinct_capacity_appends_do_not_oversubscribe(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ctx = _ctx(tmp_path, repo)
    for index in range(context_write_intents.MAX_INTENTS_PER_REQUEST - 1):
        _append_session(ctx, index)

    def attempt(index: int) -> tuple[str, dict | str]:
        try:
            return "ok", _append_session(ctx, index)
        except context_write_intents.ContextWriteIntentError as exc:
            return "err", str(exc)

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(attempt, 100 + index) for index in range(12)]
        results = [future.result() for future in as_completed(futures)]

    successes = [result for status, result in results if status == "ok"]
    errors = [result for status, result in results if status == "err"]
    assert len(successes) == 1
    assert all(
        isinstance(result, dict) and result["idempotent"] is False
        for result in successes
    )
    assert errors == ["intent_capacity_exceeded"] * 11
    assert len(_read(ctx)) == context_write_intents.MAX_INTENTS_PER_REQUEST
    assert len(ctx.audit_ledger_path.read_text(encoding="utf-8").splitlines()) == (
        context_write_intents.MAX_INTENTS_PER_REQUEST
    )


def test_verified_manager_can_inspect_and_accept_exact_request_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    ctx = _ctx(tmp_path, repo)
    submitted = worker_tools.session_write_intent(
        ctx,
        action="checkpoint",
        content="worker checkpoint requiring manager arbitration",
        idempotency_key="session:worker:manager:0001",
        provenance="review evidence",
    )
    assert submitted["ok"] is True

    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    metadata_path = process_dir / f"{ctx.request_id}.request.json"
    metadata_path.write_text(
        json.dumps({
            "request_id": ctx.request_id,
            "task_id": ctx.task_id,
            "runner": ctx.runner,
            "topic": ctx.topic,
            "worker_mcp": {
                "authority_repo": str(repo),
                "audit_ledger_path": str(ctx.audit_ledger_path),
                "audit_hmac_key_path": str(ctx.audit_hmac_key_path),
            },
        }),
        encoding="utf-8",
    )
    card = {
        "task_id": ctx.task_id,
        "status": "review",
        "worker_status": "review_ready",
        "runner": ctx.runner,
        "topic": ctx.topic,
        "claimed_by": ctx.runner,
    }
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "processes.jsonl",
        process_dir=process_dir,
        show_task=lambda _task_id: {"returncode": 0, "stdout": json.dumps(card), "stderr": ""},
        isolation_enabled=False,
    )
    manager._append_event({
        "request_id": ctx.request_id,
        "task_id": ctx.task_id,
        "runner": ctx.runner,
        "topic": ctx.topic,
        "state": "review_ready",
        "metadata_path": str(metadata_path),
    })
    route = {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo": str(repo),
        "manager_route": {
            "provider": "codex",
            "session_id": "thread-manager-1",
            "thread_id": "thread-manager-1",
        },
    }
    monkeypatch.setattr(core, "manager_bootstrap", lambda: route)
    monkeypatch.setattr(core, "writes_allowed", lambda: True)

    inbox = manager.context_write_intents(ctx.request_id)
    assert inbox["ok"] is True
    assert inbox["counts"] == {"total": 1, "pending": 1, "accepted": 0, "rejected": 0}
    disposed = manager.dispose_context_write_intent(
        ctx.request_id,
        submitted["intent_id"],
        decision="accepted",
        reason="verified durable checkpoint",
    )
    assert disposed["ok"] is True
    assert disposed["decision"] == "accepted"
    assert manager.context_write_intents(ctx.request_id)["counts"]["pending"] == 0

    transcript_db = storage_registry.resolve_database_path(
        storage_registry.load_storage_registry(repo), "transcript",
    )
    with sqlite3.connect(transcript_db) as con:
        row = con.execute("SELECT kind,content FROM documents").fetchone()
    assert row == ("checkpoint", "worker checkpoint requiring manager arbitration")
