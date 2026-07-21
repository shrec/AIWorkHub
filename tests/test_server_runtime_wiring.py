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
            "objective, card_json, created_at, updated_at, claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, runner, topic, "solo", "pending", "unclaimed", "normal", "objective",
             json.dumps({}), _NOW, _NOW, None),
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

    def list_processes(self, limit):
        self.calls.append(("list", {"limit": limit}))
        return {"ok": True, "processes": []}


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
    assert server.aiworkhub_agent_list_processes(20)["processes"] == []

    assert [name for name, _ in fake.calls] == ["launch", "status", "collect", "cancel", "list"]


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
            {"task_id": "T1", "reason": "repair this"},
        ),
    ]


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

    # release-launch requires its own claim-start precondition (the exact
    # processing owner), so it is exercised on a third task.
    release_task_id = "RUNTIME_WIRING_TASK_RELEASE"
    _insert_lifecycle_task(root, release_task_id, runner, topic)
    assert core.claim_start_exact(release_task_id, runner, topic)["ok"] is True
    released = core.release_launch(release_task_id, runner, "spawn failed", topic=topic)
    assert released["ok"] is True, released
    row = _lifecycle_row(root, release_task_id)
    assert row["worker_status"] == "unclaimed"
    assert row["status"] == "pending"

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
