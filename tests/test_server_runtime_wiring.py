from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, process_launcher, server, task_engine, task_store  # noqa: E402

_NOW = "2026-07-20T00:00:00+00:00"


def _init_lifecycle_repo(tmp_path: Path) -> Path:
    root = tmp_path / "lifecycle_repo"
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


def _insert_lifecycle_task(root: Path, task_id: str, runner: str, topic: str) -> None:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, origin_thread_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, runner, topic, "solo", "pending", "unclaimed", "normal", "objective",
             json.dumps({"origin_thread_id": "runtime-wiring-thread"}), _NOW, _NOW, None,
             "runtime-wiring-thread"),
        )
        conn.commit()
    finally:
        conn.close()


def _lifecycle_row(root: Path, task_id: str) -> sqlite3.Row:
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None, task_id
    return row


class _FakeManager:
    def __init__(self):
        self.calls = []
        self.launch_environment = {}
        self.list_payload = {"ok": True, "total_requests": 0, "processes": []}

    def launch(self, **kwargs):
        self.launch_environment = dict(os.environ)
        self.calls.append(("launch", kwargs))
        return {"ok": True, "request_id": "r1", **kwargs}

    def status(self, request_id):
        self.calls.append(("status", {"request_id": request_id}))
        return {"ok": True, "request_id": request_id, "state": "running"}

    def collect(self, request_id, max_log_bytes):
        self.calls.append(("collect", {"request_id": request_id, "max_log_bytes": max_log_bytes}))
        return {"ok": True, "request_id": request_id, "review_ready": True}

    def cancel(self, request_id, reason):
        self.calls.append(("cancel", {"request_id": request_id, "reason": reason}))
        return {"ok": True, "request_id": request_id, "state": "cancelled"}

    def retry_finalization(self, request_id, task_id):
        self.calls.append(("retry_finalization", {
            "request_id": request_id,
            "task_id": task_id,
        }))
        return {
            "ok": True,
            "request_id": request_id,
            "task_id": task_id,
            "state": "review_ready",
            "provider_relaunched": False,
        }

    def list_processes(self, limit):
        self.calls.append(("list", {"limit": limit}))
        return self.list_payload


def test_runtime_tools_delegate_to_single_manager(monkeypatch):
    fake = _FakeManager()
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)

    launched = server.aiworkhub_agent_launch_task(
        "T1", "claude_t1", "task_mcp", "claude_cli", model="sonnet"
    )
    assert launched["request_id"] == "r1"
    assert server.aiworkhub_agent_task_status("r1")["state"] == "running"
    assert server.aiworkhub_agent_collect_result("r1", 4096)["review_ready"] is True
    assert server.aiworkhub_agent_cancel_task("r1", "test")["state"] == "cancelled"
    assert server.aiworkhub_agent_retry_finalization("r1", "T1")[
        "provider_relaunched"
    ] is False
    summary = server.aiworkhub_agent_list_processes(20)
    assert summary["detail"] == "summary"
    assert summary["scanned_count"] == 0

    assert [name for name, _ in fake.calls] == [
        "launch",
        "status",
        "collect",
        "cancel",
        "retry_finalization",
        "list",
    ]


def test_read_only_mcp_does_not_take_task_reconciler_ownership(
    tmp_path, monkeypatch
):
    starts = []
    monkeypatch.setattr(core, "writes_allowed", lambda: False)
    monkeypatch.setattr(
        server.task_reconciler, "ensure_started", lambda root: starts.append(root)
    )

    server._start_task_reconciler_safely(tmp_path)

    assert starts == []


def test_write_enabled_mcp_starts_task_reconciler(tmp_path, monkeypatch):
    starts = []
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(
        server.task_reconciler, "ensure_started", lambda root: starts.append(root)
    )

    server._start_task_reconciler_safely(tmp_path)

    assert starts == [tmp_path]


def test_launch_rebinds_stale_validation_replay_once(monkeypatch):
    fake = _FakeManager()
    launch_results = iter(
        [
            {
                "ok": False,
                "state": "blocked",
                "blocked_reason": "validation_only_replay_predecessor_mismatch",
            },
            {"ok": True, "request_id": "r2", "state": "running"},
        ]
    )

    def launch(**kwargs):
        fake.calls.append(("launch", kwargs))
        return next(launch_results)

    recoveries = []

    def recover(task_id, **kwargs):
        recoveries.append((task_id, kwargs))
        return {"ok": True}

    fake.launch = launch
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)
    monkeypatch.setattr(core, "recover_blocked_rework", recover)

    result = server.aiworkhub_agent_launch_task(
        "T1", "claude_t1", "task_mcp", "claude_cli", model="sonnet"
    )

    assert result["ok"] is True
    assert result["request_id"] == "r2"
    assert result["automatic_validation_replay_recovery"] == {
        "attempted": True,
        "succeeded": True,
        "reason": "validation_only_replay_predecessor_mismatch",
    }
    assert len([call for call in fake.calls if call[0] == "launch"]) == 2
    assert recoveries == [
        (
            "T1",
            {
                "feedback_reason": (
                    "Automatic one-shot rebind after authenticated validation-only "
                    "replay launch blocker: validation_only_replay_predecessor_mismatch"
                ),
                "validation_only_replay": True,
            },
        )
    ]


def test_list_processes_summary_is_bounded_deterministic_and_truthful(monkeypatch):
    fake = _FakeManager()
    rows = [
        {
            "request_id": f"request-{index:03d}",
            "state": ("running", "review_ready", "blocked")[index % 3],
            "terminal_substatus": (
                "" if index % 3 == 0 else ("review_ready" if index % 2 else "validation_failed")
            ),
            "timestamp": f"2026-08-11T{index // 60:02d}:{index % 60:02d}:00+00:00",
            "logs": "ლ" * 4000,
            "workspace_baseline": {"files": ["x" * 1000]},
            "tree_baseline": {"paths": ["y" * 1000]},
            "validation": {"output": "z" * 1000},
            "usage": {"payload": "u" * 1000},
        }
        for index in range(100)
    ]
    fake.list_payload = {
        "ok": True,
        "launch_implemented": True,
        "launch_enabled": True,
        "active_in_memory": 34,
        "concurrency_limit": 8,
        "total_requests": 125,
        "processes": rows,
    }
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)

    first = server.aiworkhub_agent_list_processes()
    second_fake = _FakeManager()
    second_fake.list_payload = fake.list_payload
    monkeypatch.setattr(process_launcher, "default_manager", lambda: second_fake)
    second = server.aiworkhub_agent_list_processes()

    assert first == second
    assert len(json.dumps(first, ensure_ascii=False).encode("utf-8")) <= 4096
    assert first == {
        "ok": True,
        "detail": "summary",
        "requested_count": 100,
        "scanned_count": 100,
        "total_count": 125,
        "returned_count": 0,
        "truncated": True,
        "full_detail_available": True,
        "state_counts": {"blocked": 33, "review_ready": 33, "running": 34},
        "terminal_substatus_counts": {"review_ready": 33, "validation_failed": 33},
        "timing": {
            "newest_timestamp": "2026-08-11T01:39:00+00:00",
            "oldest_timestamp": "2026-08-11T00:00:00+00:00",
        },
    }
    assert fake.calls == [("list", {"limit": 100})]
    assert second_fake.calls == [("list", {"limit": 100})]
    assert not ({"processes", "logs", "workspace_baseline", "tree_baseline", "validation", "usage"} & first.keys())


def test_list_processes_summary_compares_aware_timestamps_by_instant(monkeypatch):
    fake = _FakeManager()
    fake.list_payload = {
        "ok": True,
        "total_requests": 4,
        "processes": [
            {"timestamp": "2026-08-11T01:00:00+00:00"},
            {"timestamp": "2026-08-11T02:00:00+02:00"},
            {"timestamp": "9999-malformed"},
            {"timestamp": "2099-01-01T00:00:00"},
        ],
    }
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)

    summary = server.aiworkhub_agent_list_processes()

    assert summary["timing"] == {
        "newest_timestamp": "2026-08-11T01:00:00+00:00",
        "oldest_timestamp": "2026-08-11T02:00:00+02:00",
    }
    assert fake.calls == [("list", {"limit": 100})]


def test_list_processes_summary_bounds_adversarial_aggregate_values(monkeypatch):
    fake = _FakeManager()
    fake.list_payload = {
        "ok": True,
        "total_requests": 100,
        "processes": [
            {
                "state": f"state-{index:03d}-" + ("界" * 2000),
                "terminal_substatus": f"substatus-{index:03d}-" + ("ლ" * 2000),
                "timestamp": f"timestamp-{index:03d}-" + ("🕰" * 2000),
            }
            for index in range(100)
        ],
    }
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)

    summary = server.aiworkhub_agent_list_processes()
    encoded = json.dumps(summary, ensure_ascii=False).encode("utf-8")

    assert len(encoded) <= 4096
    assert fake.calls == [("list", {"limit": 100})]
    for field in ("state_counts", "terminal_substatus_counts"):
        aggregate = summary[field]
        assert len(aggregate["values"]) == 6
        assert sum(item["count"] for item in aggregate["values"]) == 6
        assert aggregate["overflow"]["distinct_count"] == 94
        assert aggregate["overflow"]["occurrence_count"] == 94
        assert len(aggregate["overflow"]["all_counts_sha256"]) == 64
    assert summary["timing"]["newest_timestamp"]["utf8_bytes"] > 4096
    assert summary["timing"]["oldest_timestamp"]["utf8_bytes"] > 4096
    assert "界" * 100 not in encoded.decode("utf-8")
    assert "ლ" * 100 not in encoded.decode("utf-8")


def test_list_processes_full_is_exact_legacy_payload(monkeypatch):
    fake = _FakeManager()
    legacy = {"ok": True, "total_requests": 1, "processes": [{"logs": "kept"}]}
    fake.list_payload = legacy
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)

    assert server.aiworkhub_agent_list_processes(7, detail="full") is legacy
    assert fake.calls == [("list", {"limit": 7})]


@pytest.mark.parametrize("detail", ["FULL", "", "records"])
def test_list_processes_rejects_invalid_detail_before_manager(monkeypatch, detail):
    fake = _FakeManager()
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)

    with pytest.raises(ValueError, match="invalid_detail"):
        server.aiworkhub_agent_list_processes(detail=detail)
    assert fake.calls == []


@pytest.mark.parametrize("limit", [0, 1001, True, 1.5, "10"])
def test_list_processes_rejects_invalid_limit_before_manager(monkeypatch, limit):
    fake = _FakeManager()
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)

    with pytest.raises(ValueError, match="invalid_limit"):
        server.aiworkhub_agent_list_processes(limit=limit)
    assert fake.calls == []


def test_launch_scrubs_coordinator_capability_before_manager_call(monkeypatch, tmp_path):
    fake = _FakeManager()
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)
    token_file = tmp_path / "coordinator.token"
    token_file.write_text("server-only-capability", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv(core.COORDINATOR_TOKEN_ENV, "server-only-capability")
    monkeypatch.setenv(core.COORDINATOR_TOKEN_FILE_ENV, str(token_file))

    result = server.aiworkhub_agent_launch_task(
        "T1", "claude_t1", "task_mcp", "claude_cli"
    )

    assert result["ok"] is True
    assert core.COORDINATOR_TOKEN_ENV not in fake.launch_environment
    assert core.COORDINATOR_TOKEN_FILE_ENV not in fake.launch_environment
    assert core.COORDINATOR_TOKEN_ENV not in os.environ
    assert core.COORDINATOR_TOKEN_FILE_ENV not in os.environ


def test_server_lifecycle_tools_preserve_public_schema(monkeypatch):
    calls = []

    def record(name):
        def invoke(**kwargs):
            calls.append((name, kwargs))
            return {"ok": True, **kwargs}

        return invoke

    monkeypatch.setattr(core, "mark_review", record("review"))
    monkeypatch.setattr(core, "mark_done", record("done"))
    monkeypatch.setattr(core, "reject_review", record("reject"))

    server.aiworkhub_task_mark_review("T1")
    server.aiworkhub_task_mark_done("T1")
    server.aiworkhub_task_reject_review("T1", "repair this")

    assert calls == [
        ("review", {"task_id": "T1"}),
        ("done", {"task_id": "T1"}),
        (
            "reject",
            {"task_id": "T1", "reason": "repair this", "to": "pending"},
        ),
    ]


def test_server_recover_blocked_rework_forwards_public_schema(monkeypatch):
    calls = []

    def recover(
        task_id,
        *,
        feedback_reason="",
        validation_only_replay=False,
        clean_root_if_predecessor_missing=False,
    ):
        calls.append(
            (
                task_id,
                feedback_reason,
                validation_only_replay,
                clean_root_if_predecessor_missing,
            )
        )
        return {"ok": True, "task_id": task_id}

    monkeypatch.setattr(core, "recover_blocked_rework", recover)

    result = server.aiworkhub_task_recover_blocked_rework("T_BLOCKED", "focused repair")

    assert result == {"ok": True, "task_id": "T_BLOCKED"}
    assert calls == [("T_BLOCKED", "focused repair", False, False)]


def test_server_reroute_launch_identity_forwards_public_schema(monkeypatch):
    calls = []

    def reroute(**kwargs):
        calls.append(kwargs)
        return {"ok": True, **kwargs}

    monkeypatch.setattr(core, "reroute_launch_identity", reroute)

    result = server.aiworkhub_task_reroute_launch_identity(
        "T_REROUTE",
        from_runner="codex_gpt-5.3-codex-spark",
        to_runner="codex_gpt-5.5",
        to_adapter_id="codex_cli",
        to_model="gpt-5.5",
        reason="operational retry route repaired",
        topic="nf460_reroute_mcp_wiring",
    )

    assert result == {
        "ok": True,
        "task_id": "T_REROUTE",
        "from_runner": "codex_gpt-5.3-codex-spark",
        "to_runner": "codex_gpt-5.5",
        "to_adapter_id": "codex_cli",
        "to_model": "gpt-5.5",
        "reason": "operational retry route repaired",
        "topic": "nf460_reroute_mcp_wiring",
    }
    assert calls == [
        {
            "task_id": "T_REROUTE",
            "from_runner": "codex_gpt-5.3-codex-spark",
            "to_runner": "codex_gpt-5.5",
            "to_adapter_id": "codex_cli",
            "to_model": "gpt-5.5",
            "reason": "operational retry route repaired",
            "topic": "nf460_reroute_mcp_wiring",
        }
    ]


def test_core_recover_blocked_rework_uses_canonical_gate_and_transaction(monkeypatch):
    calls = []
    card = {"task_id": "T_BLOCKED", "topic": "blocked_rework"}
    monkeypatch.setattr(core, "_live_card", lambda task_id: (card, None))

    def gate(action, **kwargs):
        calls.append(("gate", action, kwargs))
        return None

    def recover(
        root,
        task_id,
        *,
        actor,
        feedback_reason,
        validation_only_replay=False,
        clean_root_if_predecessor_missing=False,
    ):
        calls.append(
            (
                "recover",
                root,
                task_id,
                actor,
                feedback_reason,
                validation_only_replay,
                clean_root_if_predecessor_missing,
            )
        )
        return True, "recovered"

    monkeypatch.setattr(core, "_canonical_write_gate", gate)
    monkeypatch.setattr(task_store, "recover_blocked_rework", recover)
    monkeypatch.setattr(task_store, "get_task", lambda root, task_id: card)
    monkeypatch.setattr(core, "_reconcile_retained_workspaces", lambda result: result)

    result = core.recover_blocked_rework(
        "T_BLOCKED", feedback_reason=" focused repair ", topic="blocked_rework"
    )

    assert result["ok"] is True
    assert calls[0] == (
        "gate",
        "recover-blocked-rework",
        {
            "runner": core.CODEX_RUNNER,
            "topic": "blocked_rework",
            "coordinator_capability": True,
            "task_id": "T_BLOCKED",
        },
    )
    assert calls[1][0:3] == ("recover", core.repo_root(), "T_BLOCKED")
    assert calls[1][3:] == (core.CODEX_RUNNER, "focused repair", False, False)


def test_recover_blocked_rework_is_a_codex_coordinator_action():
    assert core.check_runner_topic_allowlist(
        core.CODEX_RUNNER,
        "blocked_rework",
        "recover-blocked-rework",
    ) == {"allowed": True, "reason": "codex_wildcard_topic_allowed"}


def test_core_recover_blocked_rework_topic_mismatch_fails_before_write(monkeypatch):
    monkeypatch.setattr(
        core,
        "_live_card",
        lambda task_id: ({"task_id": task_id, "topic": "expected"}, None),
    )
    monkeypatch.setattr(
        core,
        "_canonical_write_gate",
        lambda *args, **kwargs: pytest.fail("write gate must not run on topic mismatch"),
    )

    result = core.recover_blocked_rework("T_BLOCKED", topic="wrong")

    assert result["ok"] is False
    assert "topic mismatch" in result["stderr"]


def test_server_reject_review_passes_predecessor_request_id(monkeypatch):
    """The server MCP tool forwards predecessor_request_id to core.reject_review
    when provided, and omits it when None (safe default)."""
    calls = []

    def record(name):
        def invoke(**kwargs):
            calls.append((name, kwargs))
            return {"ok": True, **kwargs}

        return invoke

    monkeypatch.setattr(core, "mark_review", record("review"))
    monkeypatch.setattr(core, "mark_done", record("done"))
    monkeypatch.setattr(core, "reject_review", record("reject"))

    # With explicit predecessor
    server.aiworkhub_task_reject_review(
        "T_EXPL", "repair", to="pending", predecessor_request_id="req-A"
    )
    # Without predecessor (None, the default)
    server.aiworkhub_task_reject_review("T_DEF", "repair")

    assert ("reject", {
        "task_id": "T_EXPL",
        "reason": "repair",
        "to": "pending",
        "predecessor_request_id": "req-A",
    }) in calls
    assert ("reject", {
        "task_id": "T_DEF",
        "reason": "repair",
        "to": "pending",
    }) in calls
    # None must not leak as a kwarg
    for _, kwargs in calls:
        if kwargs.get("task_id") == "T_DEF":
            assert "predecessor_request_id" not in kwargs


def test_server_reject_review_passes_explicit_infrastructure_category(monkeypatch):
    calls = []

    def reject(**kwargs):
        calls.append(kwargs)
        return {"ok": True, **kwargs}

    monkeypatch.setattr(core, "reject_review", reject)

    server.aiworkhub_task_reject_review(
        "T_MECHANICAL",
        "review route failed",
        to="blocked",
        failure_category="provider_runtime",
    )

    assert calls == [{
        "task_id": "T_MECHANICAL",
        "reason": "review route failed",
        "to": "blocked",
        "failure_category": "provider_runtime",
    }]


def test_server_reject_review_cancels_only_core_disposed_reviewer_processes(monkeypatch):
    reviewer_rows = [
        {"task_id": "R_EXACT", "finished": True, "cleanup_error": ""},
    ]

    def reject(**kwargs):
        return {"ok": True, "reviewer_finalization": reviewer_rows, **kwargs}

    class Manager:
        def cancel_disposed_reviewer_processes(self, rows):
            assert rows is reviewer_rows
            return {
                "schema_id": "aiworkhub.reviewer_process_cancellation.v1",
                "ok": True,
                "state": "completed",
                "reviewer_task_ids": ["R_EXACT"],
                "cancelled": [],
            }

    monkeypatch.setattr(core, "reject_review", reject)
    monkeypatch.setattr(process_launcher, "default_manager", lambda: Manager())

    result = server.aiworkhub_task_reject_review("T_PARENT", "rework")

    assert result["reviewer_process_cancellation"]["ok"] is True
    assert result["reviewer_process_cancellation"]["reviewer_task_ids"] == ["R_EXACT"]

def test_real_core_lifecycle_calls_scope_identity_and_capability(monkeypatch, tmp_path):
    """B857: rebased to the canonical in-process engine (task_store) --
    these lifecycle calls resolve directly against the repo-local
    ``.aiworkhub/tasking/task_queue.sqlite``, never a subprocess/taskctl.py
    shell-out (that expectation predates the B852 canonical engine)."""
    runner = "claude_task_mcp_runtime_wiring"
    topic = "task_mcp"
    token = "runtime-wiring-capability"
    token_file = tmp_path / "coordinator.token"
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(0o600)

    root = _init_lifecycle_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setenv(core.COORDINATOR_TOKEN_ENV, token)
    monkeypatch.setenv(core.COORDINATOR_TOKEN_FILE_ENV, str(token_file))

    # claim-start -> review -> done, all against one task's real card row.
    done_task_id = "RUNTIME_WIRING_TASK_DONE"
    _insert_lifecycle_task(root, done_task_id, runner, topic)

    claimed = core.claim_start_exact(done_task_id, runner, topic)
    assert claimed["ok"] is True, claimed
    assert _lifecycle_row(root, done_task_id)["worker_status"] == "claimed"

    reviewed = core.mark_review(done_task_id, runner=runner, topic=topic)
    assert reviewed["ok"] is True, reviewed
    assert _lifecycle_row(root, done_task_id)["worker_status"] == "review"

    done = core.mark_done(done_task_id, topic=topic)
    assert done["ok"] is True, done
    row = _lifecycle_row(root, done_task_id)
    assert row["worker_status"] == "done"
    assert row["status"] == "finished"

    # reject-review requires its own claim-start -> review precondition, so
    # it is exercised on a second task rather than chained onto the
    # already-finished one above.
    reject_task_id = "RUNTIME_WIRING_TASK_REJECT"
    _insert_lifecycle_task(root, reject_task_id, runner, topic)
    assert core.claim_start_exact(reject_task_id, runner, topic)["ok"] is True
    assert core.mark_review(reject_task_id, runner=runner, topic=topic)["ok"] is True
    rejected = core.reject_review(reject_task_id, "repair", topic=topic)
    assert rejected["ok"] is True, rejected
    row = _lifecycle_row(root, reject_task_id)
    assert row["worker_status"] == "unclaimed"
    assert row["status"] == "pending"

    # A failed launch/finalization still requires manager review.  The exact
    # processing owner and terminal reason are preserved; only an explicit
    # manager reject-review may return the card to pending.
    release_task_id = "RUNTIME_WIRING_TASK_RELEASE"
    _insert_lifecycle_task(root, release_task_id, runner, topic)
    assert core.claim_start_exact(release_task_id, runner, topic)["ok"] is True
    released = core.release_launch(release_task_id, runner, "spawn failed", topic=topic)
    assert released["ok"] is True, released
    row = _lifecycle_row(root, release_task_id)
    assert row["worker_status"] == "review"
    assert row["status"] == "review"
    card = task_store.get_task(root, release_task_id)
    assert card is not None
    assert card["terminal_outcome"] == "spawn failed"
    assert released["callback_enqueued"] is True

    # mark_done/reject_review/release_launch are coordinator-only (require
    # the scrubbed coordinator token); claim_start_exact/mark_review are
    # card-scoped to the exact runner/topic instead. Neither path ever
    # shells out to a subprocess -- there is no `core.subprocess.run` call
    # left to observe in any of the four calls above.


def test_coordinator_token_is_scrubbed_before_any_submodule_regardless_of_import_order(
    tmp_path,
):
    """B314_F002 regression: the coordinator token pop used to live as
    core.py's own module-level side effect, so a caller importing a
    *different* submodule first (dashboard, worker_workspace, ...) could in
    principle run that submodule's top-level code -- and any raw
    os.environ.copy() in it -- before the secret was ever popped. The scrub
    now runs in aiworkhub/__init__.py, which Python always finishes
    executing before ANY submodule of the package is imported, so this must
    hold no matter which submodule is imported first.

    Runs in a fresh subprocess (not this test process) because the scrub is
    idempotent-after-first-import within one interpreter -- only a fresh
    process proves the ordering guarantee rather than reusing an
    already-scrubbed sys.modules cache from an earlier test in this file.
    """
    script = (
        "import os, sys; "
        f"sys.path.insert(0, {str(_SRC)!r}); "
        "from aiworkhub import worker_workspace; "
        "assert 'BITNN_TASKCTL_COORDINATOR_TOKEN' not in os.environ; "
        "assert 'BITNN_TASKCTL_COORDINATOR_TOKEN_FILE' not in os.environ; "
        "from aiworkhub import core; "
        "assert core.coordinator_config() == ('leak-order-test', ''); "
        "print('SCRUBBED_BEFORE_SUBMODULE_IMPORT_OK')"
    )
    env = dict(os.environ)
    env["BITNN_TASKCTL_COORDINATOR_TOKEN"] = "leak-order-test"
    env.pop("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCRUBBED_BEFORE_SUBMODULE_IMPORT_OK" in result.stdout


def test_write_command_classification_and_capability_scope(monkeypatch):
    expected_writes = {
        "add-card",
        "auto-pickup",
        "claim-start",
        "done",
        "export-jsonl",
        "import-jsonl",
        "init-db",
        "owner-review-recover",
        "pickup",
        "recover-stale",
        "reject-review",
        "release-launch",
        "review",
        "stage",
        "start",
        "unstick-pending",
        "usage",
    }
    assert expected_writes <= core.WRITE_COMMANDS
    for command in expected_writes:
        assert core._is_write_command([command]) is True
    for command in ("list", "show", "review-queue", "usage-report", "verify"):
        assert core._is_write_command([command]) is False

    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    with pytest.raises(ValueError, match="coordinator capability may only"):
        core.run_taskctl(
            ["review", "T1", "--runner", "codex"],
            allow_write=True,
            runner="codex",
            coordinator_capability=True,
        )


def test_source_graph_retrieval_eval_wires_real_canonical_acceptance_authority(
    monkeypatch, tmp_path,
):
    """NF-2026-00864: the production MCP path must build a real,
    repository-bound external_qualification.canonical_acceptance_authority
    per declared case attempt -- not a test-only stand-in -- so that
    accepted_outcome_coverage stops being a permanently-pending constant. A
    garbage/tampered receipt is refused by the real canonical authority; a
    task with no canonical evidence in the store stays pending; neither
    corrupts the other's measurement."""

    root = _init_lifecycle_repo(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: root)

    runner = "claude_task_mcp_runtime_wiring"
    topic = "task_mcp"
    real_task_id = "RUNTIME_WIRING_RETRIEVAL_TASK"
    _insert_lifecycle_task(root, real_task_id, runner, topic)

    registry = root / ".aiworkhub" / "source-graph-retrieval-eval.json"
    registry.write_text(json.dumps({
        "cases": [
            {
                "id": "tampered",
                "query": "symbol", "mode": "focus", "k": 2,
                "expected_paths": ["src/right.py"],
                "accepted_outcome_task_id": real_task_id,
                "accepted_outcome_request_id": "req_tampered_1",
                "accepted_outcome_receipt": {"schema_id": "forged"},
            },
            {
                "id": "missing_task",
                "query": "symbol", "mode": "focus", "k": 2,
                "expected_paths": ["src/right.py"],
                "accepted_outcome_task_id": "RUNTIME_WIRING_RETRIEVAL_TASK_MISSING",
                "accepted_outcome_request_id": "req_missing_1",
                "accepted_outcome_receipt": {"schema_id": "aiworkhub.accepted_outcome_receipt.v1"},
            },
        ],
    }), encoding="utf-8")

    calls = []
    real_authority = server.external_qualification.canonical_acceptance_authority

    def spy_authority(repo, card, *, task_id, request_id):
        calls.append((Path(repo), dict(card) if isinstance(card, dict) else card, task_id, request_id))
        return real_authority(repo, card, task_id=task_id, request_id=request_id)

    monkeypatch.setattr(server.external_qualification, "canonical_acceptance_authority", spy_authority)
    monkeypatch.setattr(
        server.manager_ai_tools, "source_graph_query",
        lambda **_kwargs: {
            "ok": True,
            "content": json.dumps({"ranked_symbols": [{"file_path": "src/right.py"}]}),
        },
    )

    report = server.aiworkhub_source_graph_retrieval_eval()

    # The real authority was invoked exactly once, with the real repo root
    # and the real card task_store just stored for the one case whose task
    # actually exists -- never a fixture double, and never invoked for the
    # case whose task cannot be found.
    assert len(calls) == 1
    called_repo, called_card, called_task_id, called_request_id = calls[0]
    assert called_repo == root
    assert called_task_id == real_task_id
    assert called_request_id == "req_tampered_1"
    assert called_card["task_id"] == real_task_id

    by_id = {row["id"]: row for row in report["cases"]}
    assert by_id["tampered"]["accepted_outcome_status"] == "refused"
    assert by_id["tampered"]["accepted_outcome_observed"] is False
    assert by_id["tampered"]["accepted_outcome_reason"]

    assert by_id["missing_task"]["accepted_outcome_status"] == "pending"
    assert by_id["missing_task"]["accepted_outcome_observed"] is False

    # Coverage is real: the one definitively-evaluated (refused) case drives
    # it to 0.0 -- never a hardcoded floor -- and the pending case is
    # excluded rather than silently counted as a failure.
    assert report["accepted_outcome_coverage"] == 0.0
    assert report["accepted_outcome_measurement_pending"] is False


def test_source_graph_retrieval_eval_accepted_outcome_through_the_real_canonical_authority(
    monkeypatch, tmp_path,
):
    """NF-2026-00864-r1: the wiring test above proves refusal and a missing
    task both flow through the real canonical authority, but neither of its
    cases ever authenticates -- so accepted_outcome_coverage has never been
    observed leaving the permanently-pending state on an ordinary run. Build
    one genuinely accepted attempt exactly the way a real acceptance would
    produce it: real promoted bytes on disk, a card whose
    terminal_review.evidence seals their hashes, and a receipt recomputed
    with task_engine's own canonical digest -- then drive it through the
    real, unstubbed external_qualification.canonical_acceptance_authority
    and assert it authenticates."""

    root = _init_lifecycle_repo(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: root)

    runner = "claude_task_mcp_runtime_wiring"
    topic = "task_mcp"
    task_id = "RUNTIME_WIRING_RETRIEVAL_ACCEPTED_TASK"
    request_id = "req_accepted_1"

    promoted_relative = "accepted_case_evidence.txt"
    (root / promoted_relative).write_text("genuinely accepted evidence\n", encoding="utf-8")
    promoted_hash = hashlib.sha256((root / promoted_relative).read_bytes()).hexdigest()

    base_oid = "b" * 40
    claim_epoch = 3
    manifest = {"schema_id": "aiworkhub.attempt_artifact_manifest.v1", "entries": []}
    manifest_id = task_engine._canonical_json_hash(manifest)
    changed_path_hashes = {promoted_relative: promoted_hash}

    card_json = {
        "claim_epoch": claim_epoch,
        "terminal_review": {
            "evidence": {
                "changed_paths": [promoted_relative],
                "changed_path_hashes": changed_path_hashes,
                "attempt_artifact_manifest": manifest,
                "workspace": {"base_oid": base_oid},
            },
        },
    }

    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, origin_thread_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, runner, topic, "solo", "accepted", "accepted", "normal", "objective",
             json.dumps(card_json), _NOW, _NOW, None, "runtime-wiring-thread"),
        )
        conn.commit()
    finally:
        conn.close()

    # Recomputed the same way task_engine._validate_accepted_outcome_receipt
    # recomputes it -- this is what makes the receipt genuine rather than
    # hand-waved, since a hand-picked digest would simply be refused.
    revision = "sha256:" + task_engine._canonical_json_hash({
        "base_oid": base_oid, "changed_path_hashes": changed_path_hashes,
    })
    unsigned_receipt = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "base_oid": base_oid,
        "promoted_paths": [promoted_relative],
        "changed_path_hashes": changed_path_hashes,
        "attempt_artifact_manifest_id": manifest_id,
        "repository_revision": revision,
    }
    receipt_id = "sha256:" + task_engine._canonical_json_hash(unsigned_receipt)
    receipt = {**unsigned_receipt, "receipt_id": receipt_id}

    registry = root / ".aiworkhub" / "source-graph-retrieval-eval.json"
    registry.write_text(json.dumps({
        "cases": [{
            "id": "accepted",
            "query": "symbol", "mode": "focus", "k": 2,
            "expected_paths": ["src/right.py"],
            "accepted_outcome_task_id": task_id,
            "accepted_outcome_request_id": request_id,
            "accepted_outcome_receipt": receipt,
        }],
    }), encoding="utf-8")

    # external_qualification.canonical_acceptance_authority is deliberately
    # left unpatched here -- the point of this test is that the real
    # authority, not a spy or a stand-in, authenticates the receipt.
    monkeypatch.setattr(
        server.manager_ai_tools, "source_graph_query",
        lambda **_kwargs: {
            "ok": True,
            "content": json.dumps({"ranked_symbols": [{"file_path": "src/right.py"}]}),
        },
    )

    report = server.aiworkhub_source_graph_retrieval_eval()

    row = report["cases"][0]
    assert row["accepted_outcome_status"] == "accepted"
    assert row["accepted_outcome_observed"] is True
    assert row["accepted_outcome_reason"] == ""

    # Coverage is a real, observed fraction on an ordinary run -- not a
    # hardcoded floor and no longer permanently pending.
    assert report["accepted_outcome_coverage"] == 1.0
    assert report["accepted_outcome_measurement_pending"] is False
    assert report["accepted_outcome_claims_declared"] == 1
    assert report["accepted_outcome_measurement_pending_reason"] is None


def test_source_graph_retrieval_eval_same_task_tampered_field_stays_refused(
    monkeypatch, tmp_path,
):
    """A same-task/same-request receipt that tampers base_oid must stay
    refused through the real MCP path; claim_epoch-only staleness against
    authenticated current-card evidence stays pending."""

    root = _init_lifecycle_repo(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: root)

    runner = "claude_task_mcp_runtime_wiring"
    topic = "task_mcp"
    promoted_relative = "accepted_case_evidence.txt"
    (root / promoted_relative).write_text("genuinely accepted evidence\n", encoding="utf-8")
    promoted_hash = hashlib.sha256((root / promoted_relative).read_bytes()).hexdigest()
    base_oid = "b" * 40
    manifest = {"schema_id": "aiworkhub.attempt_artifact_manifest.v1", "entries": []}
    manifest_id = task_engine._canonical_json_hash(manifest)
    changed_path_hashes = {promoted_relative: promoted_hash}
    revision = "sha256:" + task_engine._canonical_json_hash({
        "base_oid": base_oid, "changed_path_hashes": changed_path_hashes,
    })

    def _insert(task_id: str, claim_epoch: int) -> None:
        card_json = {
            "claim_epoch": claim_epoch,
            "terminal_review": {
                "evidence": {
                    "changed_paths": [promoted_relative],
                    "changed_path_hashes": changed_path_hashes,
                    "attempt_artifact_manifest": manifest,
                    "workspace": {"base_oid": base_oid},
                },
            },
        }
        readiness = task_store.storage_readiness(root)
        conn = sqlite3.connect(readiness.canonical_db)
        try:
            conn.execute(
                "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
                "objective, card_json, created_at, updated_at, claimed_by, origin_thread_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, runner, topic, "solo", "accepted", "accepted", "normal", "objective",
                 json.dumps(card_json), _NOW, _NOW, None, "runtime-wiring-thread"),
            )
            conn.commit()
        finally:
            conn.close()

    def _receipt(task_id: str, request_id: str, claim_epoch: int, receipt_base_oid: str) -> dict:
        unsigned = {
            "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
            "task_id": task_id,
            "request_id": request_id,
            "claim_epoch": claim_epoch,
            "base_oid": receipt_base_oid,
            "promoted_paths": [promoted_relative],
            "changed_path_hashes": changed_path_hashes,
            "attempt_artifact_manifest_id": manifest_id,
            "repository_revision": revision,
        }
        return {
            **unsigned,
            "receipt_id": "sha256:" + task_engine._canonical_json_hash(unsigned),
        }

    stale_task = "RUNTIME_WIRING_RETRIEVAL_STALE_EPOCH_TASK"
    tamper_task = "RUNTIME_WIRING_RETRIEVAL_TAMPERED_OID_TASK"
    _insert(stale_task, 4)
    _insert(tamper_task, 3)

    registry = root / ".aiworkhub" / "source-graph-retrieval-eval.json"
    registry.write_text(json.dumps({
        "cases": [
            {
                "id": "stale_claim_epoch",
                "query": "symbol", "mode": "focus", "k": 2,
                "expected_paths": ["src/right.py"],
                "accepted_outcome_task_id": stale_task,
                "accepted_outcome_request_id": "req_stale_1",
                "accepted_outcome_receipt": _receipt(
                    stale_task, "req_stale_1", 3, base_oid,
                ),
            },
            {
                "id": "tampered_base_oid",
                "query": "symbol", "mode": "focus", "k": 2,
                "expected_paths": ["src/right.py"],
                "accepted_outcome_task_id": tamper_task,
                "accepted_outcome_request_id": "req_tamper_1",
                "accepted_outcome_receipt": _receipt(
                    tamper_task, "req_tamper_1", 3, "c" * 40,
                ),
            },
        ],
    }), encoding="utf-8")

    monkeypatch.setattr(
        server.manager_ai_tools, "source_graph_query",
        lambda **_kwargs: {
            "ok": True,
            "content": json.dumps({"ranked_symbols": [{"file_path": "src/right.py"}]}),
        },
    )

    report = server.aiworkhub_source_graph_retrieval_eval()
    by_id = {row["id"]: row for row in report["cases"]}
    assert by_id["stale_claim_epoch"]["accepted_outcome_status"] == "pending"
    assert by_id["stale_claim_epoch"]["accepted_outcome_reason"] == (
        "accepted_outcome_receipt_identity_mismatch"
    )
    assert by_id["tampered_base_oid"]["accepted_outcome_status"] == "refused"
    assert by_id["tampered_base_oid"]["accepted_outcome_reason"] == (
        "accepted_outcome_receipt_identity_mismatch"
    )
    assert report["accepted_outcome_coverage"] == 0.0
    assert report["accepted_outcome_measurement_pending"] is False
