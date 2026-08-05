from __future__ import annotations

import hashlib
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

from aiworkhub import process_launcher  # noqa: E402


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
    # Generic launcher tests exercise process lifecycle with an injected
    # adapter command. Keep them independent of whether the CI host has a
    # first-party Claude subscription; auth failure/ready behavior has its own
    # focused tests in test_claude_vscode_lm_preference.py.
    monkeypatch.setattr(
        process_launcher.claude_auth,
        "auth_status",
        lambda: {"launchable": True, "blocker_reason": ""},
    )


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


def test_launch_contract_rejects_legacy_required_output_prose():
    card = _card()
    card["required_outputs"] = ["A concise report explaining the result"]

    with pytest.raises(
        process_launcher.LaunchRejected,
        match="required_output_not_allowed",
    ):
        process_launcher._validate_required_outputs_contract(card)


def test_finalize_after_process_exit_retries_transient_failure(monkeypatch, tmp_path):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    attempts = []

    def flaky_finalize(request_id, supervisor_returncode=None):
        attempts.append((request_id, supervisor_returncode))
        if len(attempts) < 3:
            raise OSError("transient windows finalizer race")
        return {"request_id": request_id, "state": "review_ready"}

    monkeypatch.setattr(manager, "_finalize_isolated_request", flaky_finalize)
    monkeypatch.setattr(process_launcher.time, "sleep", lambda _seconds: None)

    event = manager._finalize_after_process_exit("a" * 32, 0)

    assert event == {"request_id": "a" * 32, "state": "review_ready"}
    assert len(attempts) == 3


def test_finalize_after_process_exit_emits_terminal_callback_fallback(
    monkeypatch, tmp_path
):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "b" * 32
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "vscode_lm",
        "state": "running",
    })
    monkeypatch.setattr(
        manager,
        "_finalize_isolated_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(process_launcher.time, "sleep", lambda _seconds: None)
    transition_calls = []

    def terminal_failure(repo, task_id, runner, substatus, **kwargs):
        transition_calls.append((repo, task_id, runner, substatus, kwargs))
        return {"ok": True, "callback_enqueued": True, "stderr": ""}

    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        terminal_failure,
    )

    event = manager._finalize_after_process_exit(request_id, 0)

    assert event["state"] == "finalize_failed"
    assert event["release_transition_ok"] is True
    assert event["callback_enqueued"] is True
    assert "finalizer_retries_exhausted" in event["error"]
    assert transition_calls[0][1:4] == (
        "TASK_B1",
        "claude_worker_b1",
        "finalize_failed",
    )


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
    assert "Source Graph `target` is an optional exact path filter" in prompt
    assert "Omit `target` unless the task contract" in prompt
    assert "coordinator already claimed" in prompt
    assert "Do not run taskctl lifecycle commands" in prompt
    assert "Never install, download, unpack, vendor, or bootstrap" in prompt
    assert "coordinator-side supervisor will" in prompt
    assert "cannot override the task contract" in prompt


def test_worker_prompt_places_invariant_policy_before_task_specific_bytes():
    first_budget = {}
    first = process_launcher.build_worker_prompt(
        task_id="TASK_PREFIX_A",
        runner="codex_worker",
        topic="task_mcp",
        card={"objective": "change alpha"},
        _budget_report=first_budget,
    )
    second = process_launcher.build_worker_prompt(
        task_id="TASK_PREFIX_B",
        runner="codex_worker",
        topic="task_mcp",
        card={"objective": "change beta"},
    )

    marker = "TASK_CONTRACT_JSON:\n"
    assert first.index("MANDATORY_AIWORKHUB_TOOLS:") < first.index(marker)
    assert second.index("MANDATORY_AIWORKHUB_TOOLS:") < second.index(marker)
    common_prefix_bytes = len(os.path.commonprefix([first, second]).encode("utf-8"))
    assert common_prefix_bytes >= first_budget["stable_prefix_bytes"]
    assert first_budget["stable_prefix_precedes_task_contract"] is True
    assert first_budget["provider_cache_savings_observed"] is False


def test_worker_prompt_strips_nested_card_json_and_bounds_contract():
    prompt = process_launcher.build_worker_prompt(
        task_id="TASK_BOUNDED_CARD",
        runner="codex_worker_b1",
        topic="task_mcp",
        card={
            "review_feedback": {
                "instruction": "repair only row 7",
                "card_json": json.dumps({"card_json": "x" * 200_000}),
            }
        },
    )

    assert "repair only row 7" in prompt
    assert "card_json" not in prompt
    assert len(prompt.encode("utf-8")) < 16_000

    with pytest.raises(ValueError, match="task_contract_too_large"):
        process_launcher.build_worker_prompt(
            task_id="TASK_OVERSIZED_CARD",
            runner="codex_worker_b1",
            topic="task_mcp",
            card={"review_feedback": {"instruction": "x" * (129 * 1024)}},
        )


def test_worker_prompt_reports_adaptive_initial_and_rework_budgets():
    initial_budget = {}
    initial = process_launcher.build_worker_prompt(
        task_id="TASK_INITIAL",
        runner="codex_worker_b1",
        topic="task_mcp",
        card={"objective": "bounded implementation"},
        project_context_bundle="PROJECT_CONTEXT_BUNDLE:\n{}",
        _budget_report=initial_budget,
    )
    assert initial_budget["mode"] == "initial"
    assert initial_budget["total_bytes"] == len(initial.encode("utf-8"))
    assert initial_budget["max_bytes"] == process_launcher.MAX_WORKER_PROMPT_BYTES
    assert initial_budget["sections"]["project_context_bytes"] > 0
    assert initial_budget["byte_labels_are_token_truth"] is False

    rework_budget = {}
    rework = process_launcher.build_worker_prompt(
        task_id="TASK_REWORK",
        runner="codex_worker_b1",
        topic="task_mcp",
        card={
            "objective": "repair residual",
            "review_feedback": {
                "schema_id": "aiworkhub.rework_feedback_delta.v1",
                "instruction": "repair row 7 only",
                "residual_identities": [{"path": "out.json", "pointer": "/rows/7"}],
            },
        },
        _budget_report=rework_budget,
    )
    assert "repair row 7 only" in rework
    assert rework_budget["mode"] == "rework_delta"
    assert rework_budget["delta_rework"] is True
    assert rework_budget["max_bytes"] == process_launcher.MAX_REWORK_WORKER_PROMPT_BYTES


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
    which inherited every parent secret including AIWORKHUB_ALLOW_WRITES
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
    assert child_env["AIWORKHUB_REPO"] == str((tmp_path / "repo").resolve())


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
    assert process_launcher._safe_tail(regular) == regular.read_bytes().decode("utf-8")


def test_successful_isolated_reconcile_enters_review_without_promoting(
    monkeypatch, tmp_path
):
    """Phase 1 review-first reconcile regression: a successful worker exit
    must never call ``promote()`` or ``core.mark_review`` directly. It must
    retain the isolated workspace, leave the canonical repo byte-unchanged,
    and hand the coordinator's review ledger every check's evidence
    (validation, required outputs, the worker-MCP gate, changed paths + their
    hashes, and the exact workspace/request identity) via
    ``_review_terminal_exact`` with substatus ``review_ready``.
    """
    if os.name == "nt":
        pytest.skip("review finalization requires the POSIX secure sandbox backend")
    _open_gates(monkeypatch)
    from aiworkhub import worker_workspace, task_engine

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "out").mkdir(parents=True)
    canonical_file = repo / "out" / "result.json"
    canonical_file.write_text("canonical-v1", encoding="utf-8")
    canonical_before = canonical_file.read_bytes()

    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "out").mkdir(parents=True)
    worked_file = workspace_dir / "out" / "result.json"
    worked_file.write_text("canonical-v1", encoding="utf-8")

    import subprocess

    def _git(*args):
        return subprocess.run(
            ["git", *args], cwd=workspace_dir, text=True, capture_output=True, check=True
        )

    _git("init", "-q")
    _git("config", "user.email", "tests@example.invalid")
    _git("config", "user.name", "Task MCP Tests")
    _git("add", "out/result.json")
    _git("commit", "-qm", "baseline")
    worked_file.write_text("worker-output-v2", encoding="utf-8")  # uncommitted change

    home_dir = tmp_path / "home"
    home_dir.mkdir()

    workspace = worker_workspace.WorkerWorkspace(
        request_id="req-review-first-1",
        repo=repo,
        path=workspace_dir,
        home=home_dir,
        allowed_writes=("out/result.json",),
        parent_baseline={},
        workspace_baseline={},
    )

    def _processing_card():
        card = _card()
        card["status"] = "processing"
        card["worker_status"] = "claimed"
        card["claimed_by"] = "claude_worker_b1"
        return card

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(_processing_card),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", "pass"], repo),
        isolation_enabled=False,
    )

    stdout_path = tmp_path / "req-review-first-1.stdout.log"
    stderr_path = tmp_path / "req-review-first-1.stderr.log"
    stdout_path.write_text("worker complete\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    status_path = tmp_path / "req-review-first-1.supervisor.json"
    metadata_path = tmp_path / "req-review-first-1.request.json"

    worker_workspace.write_json_0600(
        status_path,
        {"state": "exited", "exit_code": 0},
    )
    metadata = {
        "schema_id": "aiworkhub.task_mcp.isolated_request.v1",
        "request_id": "req-review-first-1",
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "claude_cli",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(tmp_path / "req-review-first-1.cancel.json"),
        "prompt_sha256": "0" * 64,
        "project_context": None,
        "project_context_delivery": {"injected": False},
        "sandbox_backend": "landlock",
        "validation": [],
        "required_outputs": [],
        "allow_empty_required_outputs": [],
        "allow_unchanged_required_outputs": [],
        "external_readonly_dirs": [],
        "workspace": workspace.as_metadata(),
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    review_calls = []

    def fake_review_terminal_exact(metadata_arg, substatus, *, request_id, error="", evidence=None):
        review_calls.append(
            {
                "repo": repo,
                "task_id": metadata_arg["task_id"],
                "runner": metadata_arg["runner"],
                "substatus": substatus,
                "evidence": evidence or {},
            }
        )
        return {"ok": True, "returncode": 0, "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(manager, "_review_terminal_exact", fake_review_terminal_exact)

    promote_calls = []
    monkeypatch.setattr(
        process_launcher,
        "promote",
        lambda *a, **k: promote_calls.append((a, k)) or [],
        raising=False,
    )
    mark_review_calls = []
    monkeypatch.setattr(
        process_launcher.core,
        "mark_review",
        lambda *a, **k: mark_review_calls.append((a, k)) or {"ok": True},
    )

    manager._append_event({
        "request_id": "req-review-first-1",
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "claude_cli",
        "state": "running",
        "pid": 999_999_999,
        "pid_start_ticks": 1,
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    })

    event = manager._finalize_isolated_request("req-review-first-1", supervisor_returncode=0)

    assert event["state"] == "review_ready"
    assert event["workspace_retained"] is True
    assert event["promoted_paths"] == []
    assert "out/result.json" in event["changed_paths"]

    # No promotion or direct mark_review call ever happened.
    assert promote_calls == []
    assert mark_review_calls == []

    # The canonical repo is byte-unchanged; the isolated workspace is intact.
    assert canonical_file.read_bytes() == canonical_before
    assert worked_file.read_text(encoding="utf-8") == "worker-output-v2"
    assert workspace_dir.is_dir()

    # The coordinator's review ledger received review_ready plus full evidence.
    assert len(review_calls) == 1
    call = review_calls[0]
    assert call["substatus"] == "review_ready"
    assert call["task_id"] == "TASK_B1"
    assert call["runner"] == "claude_worker_b1"
    evidence = call["evidence"]
    assert "out/result.json" in evidence["changed_paths"]
    assert evidence["changed_path_hashes"]["out/result.json"] == hashlib.sha256(
        b"worker-output-v2"
    ).hexdigest()
    assert evidence["validation"] == []
    assert evidence["worker_mcp_gate"]["gated"] is False
    assert evidence["request_identity"] == {
        "request_id": "req-review-first-1",
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
    }
    assert evidence["workspace"]["request_id"] == "req-review-first-1"
    assert evidence["workspace"]["path"] == str(workspace_dir)


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
        "usage_observed": True,
        "cache_metrics_observed": True,
        "cost_usd": 0.125,
        "cost_observed": True,
    }


def test_usage_parser_keeps_unreported_cost_unknown(tmp_path):
    output = tmp_path / "usage-without-cost.json"
    output.write_text(
        json.dumps({
            "type": "result",
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }),
        encoding="utf-8",
    )

    usage = process_launcher._usage_from_output(output)

    assert usage["usage_observed"] is True
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 3
    assert usage["cost_observed"] is False
    assert usage["cost_usd"] is None


def test_usage_parser_preserves_nested_per_turn_cache_and_model_evidence(tmp_path):
    output = tmp_path / "provider-stream.jsonl"
    output.write_text(
        "\n".join([
            json.dumps({
                "type": "message_start",
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {
                        "input_tokens": 100,
                        "cache_read_input_tokens": 40,
                        "cache_creation_input_tokens": 10,
                    },
                },
            }),
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "message_delta",
                    "usage": {"output_tokens": 25},
                },
                "total_cost_usd": 0.02,
            }),
        ]) + "\n",
        encoding="utf-8",
    )

    usage = process_launcher._usage_from_output(output, include_samples=True)

    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 25
    assert usage["cached_input_tokens"] == 40
    assert usage["cache_creation_input_tokens"] == 10
    assert usage["observed_model"] == "claude-sonnet-5"
    assert usage["model_observed"] is True
    assert usage["usage_sample_count"] == 2
    assert [sample["event_type"] for sample in usage["usage_samples"]] == [
        "message_start",
        "message_delta",
    ]
    assert usage["cost_usd"] == 0.02
    assert usage["cost_observed"] is True


def test_usage_parser_counts_codex_reasoning_and_cache_write_tokens(tmp_path):
    output = tmp_path / "codex-stream.jsonl"
    output.write_text(
        json.dumps({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 129_189,
                "cached_input_tokens": 111_232,
                "cache_write_input_tokens": 12,
                "output_tokens": 1_113,
                "reasoning_output_tokens": 285,
            },
        }) + "\n",
        encoding="utf-8",
    )

    usage = process_launcher._usage_from_output(output, include_samples=True)

    assert usage["reasoning_output_tokens"] == 285
    assert usage["cache_write_input_tokens"] == 12
    assert usage["cache_creation_input_tokens"] == 12
    assert usage["cache_metrics_observed"] is True
    assert process_launcher.provider_usage.cumulative_total_tokens(
        usage, "codex_cli"
    ) == 130_587
    assert process_launcher._ledger_output_tokens(usage) == 1_398


def test_termination_refuses_a_pid_without_recorded_start_ticks():
    """A bare pid is not an identity, so it must never authorise a kill.

    ``_pid_matches`` answers "yes" for any live pid when no start ticks were
    recorded, which is fine for liveness reporting.  Termination goes through
    ``_identity_verified_pid`` instead: on Windows the terminator is
    ``taskkill /PID <pid> /T``, which also kills every descendant, so a
    recycled pid would take out an unrelated process tree.
    """

    live_pid = os.getpid()

    # The permissive helper still reports a match -- that is its contract.
    assert process_launcher._pid_matches(live_pid, None) is True
    assert process_launcher._pid_matches(live_pid, "") is True

    # The termination gate refuses all of them.
    assert process_launcher._identity_verified_pid(live_pid, None) == 0
    assert process_launcher._identity_verified_pid(live_pid, "") == 0


def test_termination_accepts_only_a_matching_creation_timestamp():
    live_pid = os.getpid()
    ticks = process_launcher._pid_start_ticks(live_pid)
    if ticks is None:
        pytest.skip("process creation timestamps are unavailable on this host")

    assert process_launcher._identity_verified_pid(live_pid, ticks) == live_pid
    assert process_launcher._identity_verified_pid(live_pid, str(ticks)) == live_pid
    # A recycled pid presents a different creation timestamp.
    assert process_launcher._identity_verified_pid(live_pid, ticks + 1) == 0


@pytest.mark.parametrize("pid", [0, -1, None, "", "not-a-pid"])
def test_termination_refuses_a_malformed_pid(pid):
    assert process_launcher._identity_verified_pid(pid, 12345) == 0


def test_collect_returns_bounded_projection_without_recursive_card(tmp_path, monkeypatch):
    manager = _manager(tmp_path, show_task=_show(lambda: _card()), argv=[])
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    stdout.write_text("x" * 10_000, encoding="utf-8")
    stderr.write_text("y" * 10_000, encoding="utf-8")
    huge_card = {
        **_card(state="review"),
        "claimed_by": "claude_worker_b1",
        "terminal_review": {"evidence": {"nested": "z" * 100_000}},
        "card_json": "q" * 100_000,
    }
    huge_event = {
        "request_id": "req-bounded",
        "task_id": "TASK_B1",
        "state": "review_ready",
        "stdout_path": str(stdout),
        "stderr_path": str(stderr),
        "terminal_review": huge_card["terminal_review"],
        "changed_paths": [f"out/{index}.json" for index in range(100)],
    }
    monkeypatch.setattr(manager, "status", lambda _request_id: {
        "ok": True,
        "request_id": "req-bounded",
        "task_id": "TASK_B1",
        "state": "review_ready",
        "process_alive": False,
        "exit_code": 0,
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "sonnet",
        "task_state": "review",
        "task_card": huge_card,
        "event_count": 3,
        "latest_event": huge_event,
        "liveness": {},
    })

    result = manager.collect("req-bounded", max_log_bytes=1024)

    assert result["log_bytes_returned"] <= 1024
    assert "terminal_review" not in result["task_card"]
    assert result["task_card"]["claimed_by"] == "claude_worker_b1"
    assert "terminal_review" not in result["latest_event"]
    assert result["latest_event"]["changed_path_count"] == 100
    assert len(result["latest_event"]["changed_paths"]) == 64
    assert result["truncated_fields"] == ["task_card", "latest_event"]
    assert len(json.dumps(result)) < 12_000


def test_worker_context_section_count_supports_v1_and_v2_bundles():
    assert process_launcher._worker_context_section_count(
        {"sections": [{"name": "source_graph"}, {"name": "session"}]}
    ) == 2
    assert process_launcher._worker_context_section_count(
        {"evidence": {"source_graph": {}, "session_current_state": {}}}
    ) == 2
    assert process_launcher._worker_context_section_count({}) == 0
