from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from geoai_task_mcp import core, process_launcher, server  # noqa: E402


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

    launched = server.geoai_agent_launch_task(
        "T1", "claude_t1", "task_mcp", "claude_cli", model="sonnet"
    )
    assert launched["request_id"] == "r1"
    assert server.geoai_agent_task_status("r1")["state"] == "running"
    assert server.geoai_agent_collect_result("r1", 4096)["review_ready"] is True
    assert server.geoai_agent_cancel_task("r1", "test")["state"] == "cancelled"
    assert server.geoai_agent_list_processes(20)["processes"] == []

    assert [name for name, _ in fake.calls] == ["launch", "status", "collect", "cancel", "list"]


def test_launch_scrubs_coordinator_capability_before_manager_call(monkeypatch, tmp_path):
    fake = _FakeManager()
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)
    token_file = tmp_path / "coordinator.token"
    token_file.write_text("server-only-capability", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv(core.COORDINATOR_TOKEN_ENV, "server-only-capability")
    monkeypatch.setenv(core.COORDINATOR_TOKEN_FILE_ENV, str(token_file))

    result = server.geoai_agent_launch_task(
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

    server.geoai_task_mark_review("T1")
    server.geoai_task_mark_done("T1")
    server.geoai_task_reject_review("T1", "repair this")

    assert calls == [
        ("review", {"task_id": "T1"}),
        ("done", {"task_id": "T1"}),
        (
            "reject",
            {"task_id": "T1", "reason": "repair this"},
        ),
    ]


def test_real_core_lifecycle_calls_scope_identity_and_capability(monkeypatch, tmp_path):
    runner = "claude_task_mcp_runtime_wiring"
    topic = "task_mcp"
    task_id = "RUNTIME_WIRING_TASK"
    token = "runtime-wiring-capability"
    token_file = tmp_path / "coordinator.token"
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("GEOAI_TASK_MCP_ALLOW_WRITES", "1")
    monkeypatch.setenv(core.COORDINATOR_TOKEN_ENV, token)
    monkeypatch.setenv(core.COORDINATOR_TOKEN_FILE_ENV, str(token_file))
    monkeypatch.setattr(core, "require_repo", lambda: tmp_path)
    monkeypatch.setattr(
        core,
        "show_task",
        lambda _task_id: {
            "ok": True,
            "returncode": 0,
            "stdout": (
                '{"task_id":"RUNTIME_WIRING_TASK","runner":"'
                + runner
                + '","claimed_by":"'
                + runner
                + '","topic":"task_mcp","status":"processing"}'
            ),
            "stderr": "",
        },
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs, dict(os.environ)))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(core.subprocess, "run", fake_run)

    assert core.claim_start_exact(task_id, runner, topic)["ok"] is True
    assert core.mark_review(task_id, runner=runner, topic=topic)["ok"] is True
    assert core.mark_done(task_id, topic=topic)["ok"] is True
    assert core.reject_review(task_id, "repair", topic=topic)["ok"] is True
    assert core.release_launch(task_id, runner, "spawn failed", topic=topic)["ok"] is True

    commands = [call[0][2:] for call in calls]
    assert commands == [
        ["claim-start", task_id, "--runner", runner, "--topic", topic],
        ["review", task_id, "--runner", runner, "--topic", topic],
        ["done", task_id, "--runner", "codex", "--topic", topic],
        [
            "reject-review",
            task_id,
            "--runner",
            "codex",
            "--topic",
            topic,
            "--reason",
            "repair",
        ],
        [
            "release-launch",
            task_id,
            "--runner",
            "codex",
            "--topic",
            topic,
            "--claimed-by",
            runner,
            "--reason",
            "spawn failed",
        ],
    ]
    for _, kwargs, parent_env in calls[:2]:
        assert "env" not in kwargs
        assert core.COORDINATOR_TOKEN_ENV not in parent_env
        assert core.COORDINATOR_TOKEN_FILE_ENV not in parent_env
    for _, kwargs, parent_env in calls[2:]:
        child_env = kwargs["env"]
        assert child_env[core.COORDINATOR_TOKEN_ENV] == token
        assert child_env[core.COORDINATOR_TOKEN_FILE_ENV] == str(token_file)
        assert core.COORDINATOR_TOKEN_ENV not in parent_env
        assert core.COORDINATOR_TOKEN_FILE_ENV not in parent_env


def test_coordinator_token_is_scrubbed_before_any_submodule_regardless_of_import_order(
    tmp_path,
):
    """B314_F002 regression: the coordinator token pop used to live as
    core.py's own module-level side effect, so a caller importing a
    *different* submodule first (dashboard, worker_workspace, ...) could in
    principle run that submodule's top-level code -- and any raw
    os.environ.copy() in it -- before the secret was ever popped. The scrub
    now runs in geoai_task_mcp/__init__.py, which Python always finishes
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
        "from geoai_task_mcp import worker_workspace; "
        "assert 'BITNN_TASKCTL_COORDINATOR_TOKEN' not in os.environ; "
        "assert 'BITNN_TASKCTL_COORDINATOR_TOKEN_FILE' not in os.environ; "
        "from geoai_task_mcp import core; "
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

    monkeypatch.setenv("GEOAI_TASK_MCP_ALLOW_WRITES", "1")
    with pytest.raises(ValueError, match="coordinator capability may only"):
        core.run_taskctl(
            ["review", "T1", "--runner", "codex"],
            allow_write=True,
            runner="codex",
            coordinator_capability=True,
        )
