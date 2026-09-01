from __future__ import annotations

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

from aiworkhub import core, process_launcher, server, task_store  # noqa: E402

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
