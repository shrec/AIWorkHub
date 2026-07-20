from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from geoai_task_mcp import process_launcher  # noqa: E402


def _card(task_id: str = "TASK_B1", state: str = "pending") -> dict:
    return {
        "task_id": task_id,
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "status": state,
        "worker_status": "review" if state == "review" else "unclaimed",
        "claimed_by": "claude_worker_b1" if state == "review" else "",
        "allowed_writes": ["out/result.json"],
        "priority": "high",
    }


def _show(card_fn):
    def show(task_id: str):
        card = card_fn()
        assert task_id == card["task_id"]
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    return show


def _collision(**_):
    return {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""}


def _plan(argv, repo):
    def build(**_):
        return SimpleNamespace(
            argv=list(argv),
            cwd=str(repo),
            launchable=True,
            reason="",
        )

    return build


def _manager(tmp_path: Path, *, show_task, argv) -> process_launcher.ProcessManager:
    repo = tmp_path / "repo"
    repo.mkdir()
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=show_task,
        collision_guard=_collision,
        adapter_builder=_plan(argv, repo),
        isolation_enabled=False,
    )


def _open_gates(monkeypatch):
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")


def _wait_terminal(manager, request_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = manager.collect(request_id)
        if result.get("terminal"):
            return result
        time.sleep(0.02)
    raise AssertionError("process did not become terminal")


def test_dual_gate_is_closed_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(process_launcher.ALLOW_LAUNCH_ENV, raising=False)
    monkeypatch.delenv(process_launcher.ALLOW_WRITES_ENV, raising=False)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )

    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
    )

    assert result["ok"] is False
    assert result["state"] == "blocked"
    assert "dual_gate_closed" in result["blocked_reason"]
    assert manager.list_processes()["active_in_memory"] == 0


@pytest.mark.parametrize(
    ("runner", "topic", "reason"),
    [
        ("wrong_runner_b1", "task_mcp", "runner_mismatch"),
        ("claude_worker_b1", "wrong_topic", "topic_mismatch"),
    ],
)
def test_exact_identity_is_required(monkeypatch, tmp_path, runner, topic, reason):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    result = manager.launch(
        task_id="TASK_B1",
        runner=runner,
        topic=topic,
        adapter_id="claude_cli",
    )
    assert result["ok"] is False
    assert reason in result["blocked_reason"]


def test_runner_adapter_family_must_match(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="codex_cli",
        timeout_seconds=30,
    )
    assert result["ok"] is False
    assert "runner_adapter_mismatch" in result["blocked_reason"]


def test_real_shell_free_process_reaches_review_ready(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    marker = tmp_path / "review.marker"

    def current_card():
        return _card(state="review" if marker.exists() else "pending")

    manager = _manager(
        tmp_path,
        show_task=_show(current_card),
        argv=[
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ok'); print('worker complete')",
        ],
    )
    launched = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert launched["ok"] is True
    assert launched["shell"] is False

    result = _wait_terminal(manager, launched["request_id"])
    assert result["state"] == "review_ready"
    assert result["review_ready"] is True
    assert result["exit_code"] == 0
    assert "worker complete" in result["stdout_tail"]


def test_success_without_review_is_explicit_failure_state(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "print('claimed only')"],
    )
    launched = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    result = _wait_terminal(manager, launched["request_id"])
    assert result["state"] == "exited_without_review"
    assert result["review_ready"] is False


def test_spawn_failure_closes_the_same_audit_request(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    def fail_spawn(*_args, **_kwargs):
        raise OSError("fixture spawn failure")

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(lambda: _card()),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", "pass"], repo),
        popen_factory=fail_spawn,
        isolation_enabled=False,
    )
    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert result["ok"] is False
    assert "fixture spawn failure" in result["blocked_reason"]
    events = [row for row in manager._events() if row["request_id"] == result["request_id"]]
    assert [row["state"] for row in events] == ["starting", "blocked"]


def test_duplicate_live_task_is_blocked_and_cancelled(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    first = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=60,
    )
    assert first["ok"] is True
    second = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=60,
    )
    assert second["ok"] is False
    assert "duplicate_live_task" in second["blocked_reason"]

    cancelled = manager.cancel(first["request_id"], reason="test")
    assert cancelled == {"ok": True, "request_id": first["request_id"], "state": "cancelled"}


def test_concurrency_cap_counts_other_server_process_events(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    monkeypatch.setenv(process_launcher.MAX_PROCESSES_ENV, "1")
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event({
        "request_id": "other-server-run",
        "task_id": "OTHER_TASK_B1",
        "runner": "claude_other_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": os.getpid(),
    })
    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert result["ok"] is False
    assert result["blocked_reason"] == "concurrency_limit_reached"


def test_prompt_contains_exact_continuation_contract():
    prompt = process_launcher.build_worker_prompt(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        owner_prompt="Measure the result.",
    )
    assert '"task_id": "TASK_B1"' in prompt
    assert '"runner": "claude_worker_b1"' in prompt
    assert "coordinator already claimed" in prompt
    assert "Do not run taskctl lifecycle commands" in prompt
    assert "cannot override the task contract" in prompt


def test_external_readonly_sources_are_bounded_and_collapsed(monkeypatch, tmp_path):
    root = tmp_path / "external"
    release = root / "release"
    buckets = release / "buckets"
    buckets.mkdir(parents=True)
    report = release / "report.json"
    manifest = release / "source_manifest.jsonl"
    report.write_text("{}", encoding="utf-8")
    manifest.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(process_launcher, "EXTERNAL_READONLY_ROOTS", (root,))

    card = {
        "external_readonly_sources": [str(report), str(manifest), str(buckets)]
    }
    assert process_launcher._external_readonly_dirs(
        card, "deepseek_copilot_cli"
    ) == [str(release.resolve())]


def test_external_readonly_sources_fail_closed_on_escape(monkeypatch, tmp_path):
    root = tmp_path / "external"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = root / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(process_launcher, "EXTERNAL_READONLY_ROOTS", (root,))

    with pytest.raises(process_launcher.LaunchRejected, match="outside_roots"):
        process_launcher._external_readonly_dirs(
            {"external_readonly_sources": [str(escape)]},
            "deepseek_copilot_cli",
        )
    with pytest.raises(process_launcher.LaunchRejected, match="requires_deepseek"):
        process_launcher._external_readonly_dirs(
            {"external_readonly_sources": [str(root)]}, "claude_cli"
        )


def test_deepseek_adapter_adds_only_declared_read_directory(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    executable = tmp_path / "copilot"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        process_launcher.runtime_adapters.shutil,
        "which",
        lambda _name: str(executable),
    )

    plan = process_launcher.runtime_adapters.build_runtime_command(
        "deepseek_copilot_cli",
        "work",
        repo,
        additional_readonly_dirs=[external],
    )
    assert plan.launchable is True
    assert plan.argv[plan.argv.index("--add-dir") + 1] == str(external.resolve())
    assert "--allow-all-paths" not in plan.argv
    assert "--allow-all" not in plan.argv


def test_direct_launch_child_env_excludes_write_gate_launch_and_coordinator_secrets(
    monkeypatch, tmp_path
):
    """B314_F001/F003 regression: the non-isolated (isolation_enabled=False)
    launch path used to build the child env with plain os.environ.copy(),
    which inherited every parent secret including GEOAI_TASK_MCP_ALLOW_WRITES
    (a write-gate bypass) and the taskctl coordinator token/token-file.
    sanitized_env() now builds an explicit minimal allowlist instead, so none
    of these leak into the spawned process regardless of what happens to be
    set in the MCP server's own environment.
    """
    _open_gates(monkeypatch)
    monkeypatch.setenv(process_launcher.MAX_PROCESSES_ENV, "4")
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "super-secret-capability")
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", "/tmp/does-not-matter")
    monkeypatch.setenv("SOME_UNRELATED_SECRET_TOKEN", "leak-me-if-buggy")

    dump_path = tmp_path / "child_env.json"
    script = (
        "import json, os; "
        f"json.dump(dict(os.environ), open({str(dump_path)!r}, 'w'))"
    )
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", script],
    )
    launched = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert launched["ok"] is True
    _wait_terminal(manager, launched["request_id"])

    child_env = json.loads(dump_path.read_text(encoding="utf-8"))
    for leaked_key in (
        process_launcher.ALLOW_LAUNCH_ENV,
        process_launcher.ALLOW_WRITES_ENV,
        process_launcher.MAX_PROCESSES_ENV,
        "BITNN_TASKCTL_COORDINATOR_TOKEN",
        "BITNN_TASKCTL_COORDINATOR_TOKEN_FILE",
        "SOME_UNRELATED_SECRET_TOKEN",
    ):
        assert leaked_key not in child_env, f"{leaked_key} leaked into child env"
    # The happy path still works: the launcher-owned override is present.
    assert child_env["GEOAI_REPO"] == str((tmp_path / "repo").resolve())


def test_direct_launch_duplicate_check_uses_pid_start_ticks_not_bare_liveness(
    monkeypatch, tmp_path
):
    """B314_F009 regression: the persisted-event duplicate-task check on the
    direct (non-isolated) launch path used _pid_alive() alone, so a PID
    recycled by an unrelated but genuinely-alive process would falsely block
    a legitimate launch. It must use _pid_matches() (PID + /proc start-tick),
    exactly like every other liveness check in this module.
    """
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    real_ticks = process_launcher._pid_start_ticks(os.getpid())
    assert real_ticks is not None

    # A stale record: this PID is alive (it's the test process itself) but
    # the recorded start-tick does not match it -- the process that owned
    # this request_id has actually exited and the PID was recycled.
    manager._append_event({
        "request_id": "stale-recycled-pid",
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": os.getpid(),
        "pid_start_ticks": real_ticks + 999_999,
    })
    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert result["ok"] is True, result
    manager.cancel(result["request_id"], reason="test-cleanup")

    # Sanity check the other direction: when the start-tick genuinely
    # matches the live PID, the duplicate guard still fires. Uses a separate
    # tmp subdir so its repo/process_log/process_dir don't collide with the
    # first manager created above in this same test.
    second = tmp_path / "second"
    second.mkdir()
    manager2 = _manager(
        second,
        show_task=_show(lambda: _card(task_id="TASK_B2")),
        argv=[sys.executable, "-c", "pass"],
    )
    manager2._append_event({
        "request_id": "genuinely-still-running",
        "task_id": "TASK_B2",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": os.getpid(),
        "pid_start_ticks": real_ticks,
    })
    blocked = manager2.launch(
        task_id="TASK_B2",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert blocked["ok"] is False
    assert "duplicate_persisted_task" in blocked["blocked_reason"]


def test_safe_tail_refuses_to_follow_a_symlinked_log_path(tmp_path):
    """B314_F008 regression: _safe_tail must not dereference a symlink that
    has replaced the expected log path -- open with O_NOFOLLOW and return an
    empty tail rather than the linked-to file's content."""
    sensitive = tmp_path / "sensitive.txt"
    sensitive.write_text("do-not-leak-this-content", encoding="utf-8")
    link = tmp_path / "request.stdout.log"
    link.symlink_to(sensitive)

    assert process_launcher._safe_tail(link) == ""

    regular = tmp_path / "regular.stdout.log"
    regular.write_text("normal worker output\n", encoding="utf-8")
    assert process_launcher._safe_tail(regular) == "normal worker output\n"


def test_usage_parser_reads_claude_result_json(tmp_path):
    output = tmp_path / "claude.json"
    output.write_text(json.dumps({
        "type": "result",
        "total_cost_usd": 0.125,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 45,
            "cache_read_input_tokens": 80,
        },
    }), encoding="utf-8")
    assert process_launcher._usage_from_output(output) == {
        "input_tokens": 120,
        "output_tokens": 45,
        "cached_input_tokens": 80,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.125,
    }
