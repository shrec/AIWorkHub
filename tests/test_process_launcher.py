from __future__ import annotations

import errno
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    launch_zero_delta,
    needfix_store,
    platform_io,
    process_launcher,
    process_launcher_acceptance,
    task_store,
    task_templates,
    toolchain_authority,
    worker_ai_tools_mcp,
    worker_workspace,
)


class _RejectingToolchainAuthority:
    def __init__(self) -> None:
        self.repairs = 0

    def evaluate(self, _card):
        missing = (toolchain_authority.MissingRequirement("module", "pytest"),)
        return toolchain_authority.AuthoritySnapshot(
            schema_id=toolchain_authority.SCHEMA_ID,
            repository="/repo",
            path="",
            executables=(),
            modules=(),
            repository_fingerprint="0" * 64,
            missing=missing,
            digest="1" * 64,
        )

    def repair(self, _snapshot):
        self.repairs += 1
        return False


def _linked_needfix(repo: Path, task_id: str) -> str:
    record = needfix_store.add_needfix(
        repo, title="accepted closure", description="d", status="accepted"
    )
    needfix_store.convert_needfix(
        repo,
        record["id"],
        lambda _card: {"ok": True, "task_id": task_id},
    )
    return str(record["id"])


def _accepted_needfix_card(
    task_id: str, request_id: str, status: str = "finished"
) -> dict:
    card = _card(task_id, status)
    card["accepted_request_id"] = request_id
    return card


def test_accept_review_needfix_restart_reconciles_pending_closure_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = "needfix-task"
    request_id = "accepted-request"
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _accepted_needfix_card(task_id, request_id)),
        argv=[sys.executable, "-c", "pass"],
    )
    needfix_id = _linked_needfix(manager.repo, task_id)
    real_close = needfix_store.close_for_accepted_task
    calls = 0

    def fail_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("injected closure persistence failure")
        return real_close(*args, **kwargs)

    monkeypatch.setattr(needfix_store, "close_for_accepted_task", fail_first)
    pending = manager._close_accepted_task_needfix(task_id, request_id)

    assert pending["state"] == "pending_recovery"
    assert pending["accepted_request_id"] == request_id
    assert needfix_store.get_needfix(manager.repo, needfix_id)["status"] == "task_created"

    restarted = process_launcher.ProcessManager(
        repo=manager.repo,
        process_log_path=manager.process_log_path,
        process_dir=manager.process_dir,
        show_task=_show(lambda: _accepted_needfix_card(task_id, request_id)),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", "pass"], manager.repo),
        isolation_enabled=False,
    )
    assert needfix_store.get_needfix(manager.repo, needfix_id)["status"] == "resolved"
    assert (
        needfix_store.list_needfix(
            manager.repo,
            active_only=True,
            get_task_fn=lambda _task_id: None,
            canonical_status_fn=lambda _card: "",
        )
        == []
    )

    process_launcher.ProcessManager(
        repo=manager.repo,
        process_log_path=manager.process_log_path,
        process_dir=manager.process_dir,
        show_task=_show(lambda: _accepted_needfix_card(task_id, request_id)),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", "pass"], manager.repo),
        isolation_enabled=False,
    )
    conn = needfix_store._connect(manager.repo)
    try:
        closure_events = conn.execute(
            "SELECT COUNT(*) FROM needfix_events "
            "WHERE needfix_id = ? AND event = 'accepted_task_closed'",
            (needfix_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert closure_events == 1
    reconciled = [
        event
        for event in restarted._events()
        if event.get("state") == "needfix_closure_reconciled"
    ]
    assert len(reconciled) == 1
    assert reconciled[0]["needfix_closure"]["closure_id"] == pending["closure_id"]


@pytest.mark.parametrize("status", ["pending", "blocked", "review", "rejected"])
def test_accept_review_needfix_reconciliation_ignores_nonaccepted_tasks(
    tmp_path: Path, status: str
) -> None:
    task_id = f"task-{status}"
    request_id = "request-nonaccepted"
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _accepted_needfix_card(task_id, request_id, status)),
        argv=[sys.executable, "-c", "pass"],
    )
    needfix_id = _linked_needfix(manager.repo, task_id)
    manager._append_event({
        "request_id": request_id,
        "task_id": task_id,
        "state": "needfix_closure_pending",
    })

    process_launcher.ProcessManager(
        repo=manager.repo,
        process_log_path=manager.process_log_path,
        process_dir=manager.process_dir,
        show_task=_show(lambda: _accepted_needfix_card(task_id, request_id, status)),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", "pass"], manager.repo),
        isolation_enabled=False,
    )

    assert needfix_store.get_needfix(manager.repo, needfix_id)["status"] == "task_created"


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


def test_memory_admission_rejects_insufficient_available_memory(monkeypatch) -> None:
    monkeypatch.setattr(
        process_launcher,
        "_available_memory_bytes",
        lambda: process_launcher.MEMORY_LAUNCH_REQUIRED_BYTES - 1,
    )
    verdict = process_launcher._memory_launch_admission()
    assert verdict["admit"] is False
    assert verdict["retryable"] is True
    assert verdict["reason"] == "memory_capacity_insufficient"


def test_memory_admission_accepts_sufficient_available_memory(monkeypatch) -> None:
    monkeypatch.setattr(
        process_launcher,
        "_available_memory_bytes",
        lambda: process_launcher.MEMORY_LAUNCH_REQUIRED_BYTES,
    )
    verdict = process_launcher._memory_launch_admission()
    assert verdict["admit"] is True
    assert verdict["reason"] == "memory_capacity_available"


def test_memory_admission_fails_closed_when_probe_fails(monkeypatch) -> None:
    monkeypatch.setattr(process_launcher, "_available_memory_bytes", lambda: None)
    verdict = process_launcher._memory_launch_admission()
    assert verdict["admit"] is False
    assert verdict["retryable"] is True
    assert verdict["reason"] == "memory_probe_unavailable"


def test_memory_denial_precedes_launch_side_effects(monkeypatch, tmp_path: Path) -> None:
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager.isolation_enabled = True
    monkeypatch.setattr(
        process_launcher,
        "_available_memory_bytes",
        lambda: process_launcher.MEMORY_LAUNCH_REQUIRED_BYTES - 1,
    )

    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
    )

    assert result["ok"] is False
    reason = result["blocked_reason"]
    assert reason.startswith("memory_launch_capacity_denied:")
    assert '"retryable":true' in reason
    assert '"reason":"memory_capacity_insufficient"' in reason
    assert manager._live == {}
    assert not manager.process_dir.exists()
    event = json.loads(manager.process_log_path.read_text(encoding="utf-8"))
    assert event["state"] == "blocked"
    assert event["blocked_reason"] == reason


def _canonical_claimed_task_repo(
    tmp_path: Path,
    *,
    task_id: str = "TASK_USAGE",
    runner: str = "claude_worker_b1",
    request_id: str = "a" * 32,
    claim_epoch: int = 3,
) -> Path:
    repo = tmp_path / f"repo-{task_id.lower()}"
    repo.mkdir()
    task_store.initialize_repository(repo)
    now = "2026-09-01T00:00:00+00:00"
    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": "task_mcp",
        "status": "processing",
        "worker_status": "claimed",
        "claimed_by": runner,
        "launch_request_id": request_id,
        "claim_epoch": claim_epoch,
    }
    conn = task_store._connect(task_store.canonical_db_path(repo))
    try:
        conn.execute(
            "INSERT INTO tasks("
            "task_id, runner, topic, mode, status, "
            "worker_status, priority, objective, card_json, created_at, "
            "updated_at, claimed_by, claimed_at, started_at, completed_at, "
            "origin_thread_id, archived_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                runner,
                "task_mcp",
                "",
                "processing",
                "claimed",
                "high",
                "",
                json.dumps(card, sort_keys=True),
                now,
                now,
                runner,
                now,
                now,
                "",
                "",
                "",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


def test_launch_reservation_refreshes_snapshot_after_lock_handoff_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    stale_generation = ("stale",)
    fresh_generation = ("fresh",)
    snapshots = iter(
        [
            ({"stale-request": {"state": "finished"}}, stale_generation),
            ({"fresh-request": {"state": "finished"}}, fresh_generation),
        ]
    )
    snapshot_calls = 0

    def stable_snapshot():
        nonlocal snapshot_calls
        snapshot_calls += 1
        return next(snapshots)

    monkeypatch.setattr(manager, "_latest_by_request_stable", stable_snapshot)
    monkeypatch.setattr(manager, "_ledger_generation", lambda: fresh_generation)

    with manager._launch_reservation(
        {
            "request_id": "req-ledger-handoff-race",
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "adapter_id": "claude_cli",
        }
    ):
        pass

    assert snapshot_calls == 2
    assert manager._events()[-1]["state"] == "starting"


def test_grok_kilo_provider_preflight_resolves_only_supported_model(tmp_path) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )

    provider_env, model = manager._resolve_provider_env("grok_kilo_cli", None)
    assert provider_env is None
    assert model == "xai/grok-4.6"

    with pytest.raises(
        process_launcher.LaunchRejected,
        match="grok_kilo_model_rejected:unsupported_grok_kilo_model",
    ):
        manager._resolve_provider_env("grok_kilo_cli", "xai/grok-4.5")


def test_grok_kilo_readonly_canary_text_result_is_meaningful(tmp_path) -> None:
    canary = json.dumps(
        {
            "request_id": "debf2f43a57c4abe89b66ef887216f6f",
            "gates": ["source_graph", "session", "ai_memory", "kb"],
            "changed_paths": [],
        }
    )
    stdout = tmp_path / "grok-kilo-canary.ndjson"
    payload = "\n".join(
        [
            json.dumps({"type": "session.init", "subtype": "kilo"}),
            json.dumps(
                {"type": "tool", "part": {"type": "function_call", "name": "graph"}}
            ),
            json.dumps(
                {"type": "reasoning", "part": {"type": "reasoning", "text": "x"}}
            ),
            json.dumps({"type": "text", "part": {"type": "text", "text": "   "}}),
            "not-json",
            json.dumps({"type": "text", "part": {"type": "text", "text": canary}}),
        ]
    ) + "\n"
    stdout.write_text(payload, encoding="utf-8")

    evidence = process_launcher._readonly_research_result_evidence(stdout)

    assert evidence["meaningful_output"] is True
    assert evidence["result_event_count"] == 1
    assert evidence["result_chars"] == len(canary)
    assert evidence["reason"] == ""
    assert evidence["sha256"] == hashlib.sha256(stdout.read_bytes()).hexdigest()


def test_grok_kilo_readonly_chatter_without_text_part_stays_missing(tmp_path) -> None:
    stdout = tmp_path / "grok-kilo-chatter.ndjson"
    stdout.write_text(
        "\n".join(
            [
                json.dumps({"type": "session.init", "kilo": True}),
                json.dumps({"type": "tool", "part": {"type": "function_call"}}),
                json.dumps(
                    {"type": "reasoning", "part": {"type": "reasoning", "text": "x"}}
                ),
                json.dumps({"type": "text", "part": {"type": "text", "text": ""}}),
                json.dumps({"type": "text", "part": "not-a-dict"}),
                json.dumps(
                    {"type": "text", "part": {"type": "text", "text": ["list"]}}
                ),
                json.dumps(
                    {"type": "text", "text": "top-level text without part envelope"}
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "text", "part": {"type": "text", "text": "x"}},
                    }
                ),
                json.dumps({"type": "error", "message": "kilo failed"}),
                "{broken",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    evidence = process_launcher._readonly_research_result_evidence(stdout)

    assert evidence["meaningful_output"] is False
    assert evidence["reason"] == "research_result_missing"
    assert evidence["result_event_count"] == 0


def test_grok_kilo_text_part_extraction_is_structurally_exact() -> None:
    extract = process_launcher._research_result_text
    assert extract({"type": "text", "part": {"type": "text", "text": " ok "}}) == "ok"
    assert extract({"type": "text", "part": {"type": "text", "text": "   "}}) == ""
    assert extract({"type": "text", "part": {"type": "text"}}) == ""
    assert extract({"type": "text", "part": {"type": "text", "text": 7}}) == ""
    assert extract({"type": "text", "part": {"type": "reasoning", "text": "x"}}) == ""
    assert extract({"type": "reasoning", "part": {"type": "text", "text": "x"}}) == ""

    # Existing provider result shapes stay intact.
    assert extract({"type": "result", "result": "claude final"}) == "claude final"
    assert extract({"type": "result", "is_error": True, "result": "x"}) == ""
    assert extract(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "codex"}}
    ) == "codex"
    assert extract(
        {"type": "assistant.message", "data": {"content": "vscode"}}
    ) == "vscode"


def test_grok_kilo_child_env_is_request_local_and_secret_free(tmp_path) -> None:
    home = tmp_path / "isolated-home"
    env = process_launcher.sanitized_env("grok_kilo_cli", home=home)

    assert env["HOME"] == str(home.resolve())
    assert env["XDG_DATA_HOME"] == str(home.resolve() / ".local" / "share")
    assert env["XDG_CONFIG_HOME"] == str(home.resolve() / ".config")
    assert env["XDG_CACHE_HOME"] == str(home.resolve() / ".cache")
    assert not any("TOKEN" in key or "API_KEY" in key for key in env)


def test_grok_kilo_launch_projects_auth_before_worker_runtime_registration(
    monkeypatch, tmp_path
) -> None:
    _open_gates(monkeypatch)
    secret = "xai-secret-must-never-appear"
    source = tmp_path / "coordinator-auth.json"
    source.write_text(
        json.dumps(
            {
                "xai": {"type": "oauth", "refresh": secret},
                "unrelated": {"token": "must-be-stripped"},
            }
        ),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    worktree = tmp_path / "request" / "worktree"
    home = tmp_path / "request" / "home"
    for directory in (repo, worktree, home):
        directory.mkdir(parents=True)
    card = {
        "task_id": "TASK_GROK",
        "runner": "grok_runner",
        "topic": "code",
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": "",
        "allowed_writes": ["out/result.json"],
        "priority": "high",
    }
    workspace = process_launcher.WorkerWorkspace(
        request_id="placeholder",
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=("out/result.json",),
        parent_baseline={"out/result.json": None},
        workspace_baseline={"out/result.json": None},
    )
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(lambda: card),
        collision_guard=_collision,
        adapter_builder=_plan(["kilo"], repo),
    )
    monkeypatch.setattr(manager, "_preflight_card", lambda *a, **k: dict(card))
    monkeypatch.setattr(
        process_launcher, "_launch_project_context", lambda *a, **k: None
    )
    monkeypatch.setattr(
        process_launcher, "_sandbox_backend_for_adapter", lambda _adapter: "landlock"
    )
    monkeypatch.setattr(
        process_launcher.kilo_auth,
        "resolve_kilo_auth_source",
        lambda **_kwargs: source,
    )
    monkeypatch.setattr(
        process_launcher,
        "create_workspace",
        lambda _repo, request_id, _card, _adapter: process_launcher.replace(
            workspace, request_id=request_id
        ),
    )
    monkeypatch.setattr(
        process_launcher, "build_residual_contract_manifest", lambda *a, **k: []
    )
    order: list[str] = []

    def stop_after_projection(*_args, **_kwargs):
        projected = json.loads(
            (home / ".local" / "share" / "kilo" / "auth.json").read_text(
                encoding="utf-8"
            )
        )
        assert list(projected) == ["xai"]
        assert projected["xai"]["refresh"] == secret
        order.append("projected-before-runtime")
        raise RuntimeError("stop-after-kilo-projection")

    monkeypatch.setattr(
        process_launcher,
        "_provision_worker_mcp_runtime_for_authority",
        stop_after_projection,
    )

    result = manager._launch_isolated(
        task_id="TASK_GROK",
        runner="grok_runner",
        topic="code",
        adapter_id="grok_kilo_cli",
        model="xai/grok-4.6",
        owner_prompt="",
        timeout_seconds=30,
    )

    assert order == ["projected-before-runtime"]
    assert result["ok"] is False
    serialized = json.dumps(result, sort_keys=True)
    assert secret not in serialized
    assert str(source) not in serialized


def _claim_receipt(*, epoch: object = 2, request_id: str = "req-claim") -> dict:
    return {
        "ok": True,
        "returncode": 0,
        "stdout": json.dumps(
            {
                "task_id": "TASK-CLAIM",
                "runner": "runner-claim",
                "topic": "topic-claim",
                "launch_request_id": request_id,
                "claim_epoch": epoch,
            }
        ),
    }


def test_committed_claim_card_binds_exact_current_epoch() -> None:
    card = process_launcher._committed_claim_card(
        _claim_receipt(),
        request_id="req-claim",
        task_id="TASK-CLAIM",
        runner="runner-claim",
        topic="topic-claim",
    )
    assert card["claim_epoch"] == 2
    assert card["launch_request_id"] == "req-claim"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"epoch": True}, "claim_epoch"),
        ({"epoch": 0}, "claim_epoch"),
        ({"request_id": "wrong"}, "launch_request_id"),
    ],
)
def test_committed_claim_card_rejects_tampered_identity(
    mutation: dict[str, object], reason: str
) -> None:
    receipt = _claim_receipt(
        epoch=mutation.get("epoch", 2),
        request_id=str(mutation.get("request_id", "req-claim")),
    )
    with pytest.raises(process_launcher.LaunchRejected, match=reason):
        process_launcher._committed_claim_card(
            receipt,
            request_id="req-claim",
            task_id="TASK-CLAIM",
            runner="runner-claim",
            topic="topic-claim",
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


def test_request_events_cache_is_request_scoped_and_invalidates_on_append(
    tmp_path,
    monkeypatch,
):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    first_id = "1" * 32
    second_id = "2" * 32
    manager._append_event({"request_id": first_id, "state": "running"})
    manager._append_event({"request_id": second_id, "state": "running"})

    scans = 0
    original_events = manager._events

    def counted_events():
        nonlocal scans
        scans += 1
        return original_events()

    monkeypatch.setattr(manager, "_events", counted_events)

    assert manager._request_events(first_id)[-1]["state"] == "running"
    assert manager._request_events(first_id)[-1]["state"] == "running"
    assert scans == 1

    # A new request must never reuse another request's empty projection merely
    # because the durable ledger fingerprint is unchanged.
    assert manager._request_events(second_id)[-1]["state"] == "running"
    assert manager._request_events(second_id)[-1]["request_id"] == second_id
    assert manager._request_events(first_id)[-1]["request_id"] == first_id
    assert scans == 2

    manager._append_event({"request_id": first_id, "state": "review_ready"})
    assert manager._request_events(first_id)[-1]["state"] == "review_ready"
    assert scans == 3


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


def test_launch_contract_accepts_authenticated_empty_mandatory_change_set():
    expanded = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["src/app.py"],
        test_paths=["tests/test_app.py"],
    )
    card = {
        **expanded,
        "template_provenance": task_templates.template_provenance_payload(
            expanded, classification_reason="explicit_template"
        ),
    }
    assert card["required_outputs"] == []

    process_launcher._validate_required_outputs_contract(card)

    card["template_provenance"] = {
        **card["template_provenance"],
        "expanded_contract_digest": "0" * 64,
    }
    with pytest.raises(
        process_launcher.LaunchRejected, match="required_outputs_invalid"
    ):
        process_launcher._validate_required_outputs_contract(card)


def test_quality_review_launch_requires_exact_packet_binding() -> None:
    with pytest.raises(
        process_launcher.LaunchRejected,
        match="quality_review_binding_required",
    ):
        process_launcher._enforce_quality_review_launch_binding(
            "quality_review", None
        )

    with pytest.raises(
        process_launcher.LaunchRejected,
        match="quality_review_binding_topic_mismatch",
    ):
        process_launcher._enforce_quality_review_launch_binding(
            "task_mcp", {"packet": {}}
        )

    process_launcher._enforce_quality_review_launch_binding(
        "quality_review", {"packet": {}}
    )


def test_rework_overlay_materialization_uses_same_task_distinct_request(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "successor" / "worktree"
    home = tmp_path / "successor" / "home"
    repo.mkdir()
    worktree.mkdir(parents=True)
    home.mkdir(parents=True)
    candidate = worktree / "src" / "service.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"def repaired():\n    return True\n")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    workspace = process_launcher.WorkerWorkspace(
        request_id="5" * 32,
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=("src/service.py",),
        parent_baseline={"src/service.py": None},
        workspace_baseline={"src/service.py": digest},
        inherited_rework_paths=("src/service.py",),
    )

    path, packet = process_launcher._materialize_worker_rework_overlay(
        workspace,
        task_id="TASK_SAME",
        card={
            "rework_predecessor": {
                "request_id": "6" * 32,
                "changed_path_hashes": {"src/service.py": digest},
            }
        },
    )

    assert path is not None and path.is_file()
    assert packet is not None
    assert packet["successor_task_id"] == "TASK_SAME"
    assert packet["predecessor_task_id"] == "TASK_SAME"
    assert packet["successor_request_id"] == "5" * 32
    assert packet["predecessor_request_id"] == "6" * 32
    assert json.loads(path.read_text(encoding="utf-8")) == packet


def test_crash_retry_packet_reuses_bounded_failure_evidence_without_stale_tree(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    process_dir = repo / ".aiworkhub" / "runtime" / "process_logs" / "processes"
    worktree = tmp_path / "successor" / "worktree"
    home = tmp_path / "successor" / "home"
    repo.mkdir()
    process_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    home.mkdir(parents=True)
    workspace = process_launcher.WorkerWorkspace(
        request_id="5" * 32,
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=("src/service.py",),
        parent_baseline={"src/service.py": None},
        workspace_baseline={"src/service.py": "a" * 64},
        inherited_rework_paths=("src/service.py",),
    )
    predecessor = "6" * 32
    process_launcher.write_json_0600(
        process_dir / f"{predecessor}.request.json",
        {
            "request_id": predecessor,
            "task_id": "TASK_SAME",
            "workspace": {"repo": str(repo)},
        },
    )
    process_launcher.write_json_0600(
        process_dir / f"{predecessor}.supervisor.json",
        {"state": "supervisor_error", "exit_code": 126, "error": "bridge crashed"},
    )
    (process_dir / f"{predecessor}.stdout.log").write_text(
        "useful-step\n" + ("x" * 9000), encoding="utf-8"
    )
    (process_dir / f"{predecessor}.stderr.log").write_text(
        "exact-provider-error\n", encoding="utf-8"
    )
    process_launcher.attempt_artifacts.persist_json_bundle(
        process_dir / "attempt-artifacts" / predecessor,
        attempt_id=predecessor,
        payloads={
            "metadata": {"request_id": predecessor},
            "diff": {"changed_paths": ["src/service.py"]},
            "validation": {
                "checks": [{
                    "returncode": 1,
                    "argv": ["pytest", "tests/test_service.py"],
                    "stderr_tail": "AssertionError: expected true",
                }]
            },
            "usage": {"usage_observed": False},
            "review": {"target_state": "validation_failed"},
        },
    )
    overlay = {
        "predecessor_request_id": predecessor,
        "predecessor_task_id": "TASK_SAME",
        "canonical_digest": "b" * 64,
    }

    path, packet = process_launcher._materialize_crash_retry_packet(
        process_dir,
        workspace,
        task_id="TASK_SAME",
        card={"rework_predecessor": {"request_id": predecessor}},
        rework_overlay_packet=overlay,
    )

    assert path is not None and path.is_file()
    assert packet is not None
    assert packet["predecessor_state"] == "supervisor_error"
    assert packet["predecessor_exit_code"] == 126
    assert "exact-provider-error" in packet["stderr_tail"]
    assert len(packet["stdout_tail"].encode("utf-8")) <= 4096
    assert packet["rework_overlay_sha256"] == "b" * 64
    assert packet["validation_failure_delta"]["failure_count"] == 1
    assert packet["validation_failure_delta"]["receipts"][0][
        "failure_class"
    ] == "test_failure"
    assert len(packet["validation_manifest_sha256"]) == 64
    assert packet["stale_worktree_bytes_authoritative"] is False
    assert packet["canonical_reread_savings_claimed"] is False
    assert "path" not in packet and "home" not in packet
    prompt = process_launcher.build_worker_prompt(
        task_id="TASK_SAME",
        runner="claude_worker_b1",
        topic="task_mcp",
        card={"task_id": "TASK_SAME", "rework_predecessor": {}},
        crash_retry_packet=packet,
    )
    assert prompt.count("CRASH_RETRY_PACKET_JSON:") == 1
    # Later mutations of the old log cannot alter the request-private packet.
    (process_dir / f"{predecessor}.stderr.log").write_text(
        "stale-later-data", encoding="utf-8"
    )
    assert "stale-later-data" not in path.read_text(encoding="utf-8")


def test_crash_retry_packet_rejects_cross_task_predecessor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    process_dir = tmp_path / "processes"
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    for directory in (repo, process_dir, worktree, home):
        directory.mkdir(parents=True)
    workspace = process_launcher.WorkerWorkspace(
        request_id="5" * 32,
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
        inherited_rework_paths=("src/service.py",),
    )

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="crash_retry_predecessor_identity_mismatch",
    ):
        process_launcher._materialize_crash_retry_packet(
            process_dir,
            workspace,
            task_id="TASK_SAME",
            card={
                "rework_predecessor": {
                    "request_id": "6" * 32,
                    "task_id": "OTHER_TASK",
                }
            },
            rework_overlay_packet={
                "predecessor_request_id": "6" * 32,
                "predecessor_task_id": "OTHER_TASK",
            },
        )


def test_finalize_after_process_exit_retries_transient_failure(monkeypatch, tmp_path):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "a" * 32
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "state": "running",
        "metadata_path": str(tmp_path / "metadata.json"),
    })
    attempts = []

    def flaky_finalize(request_id, supervisor_returncode=None, *, lock_blocking=True):
        attempts.append((request_id, supervisor_returncode))
        assert lock_blocking is True
        assert manager._request_events(request_id)[-1]["state"] == "running"
        if len(attempts) < 3:
            raise OSError("transient windows finalizer race")
        return {"request_id": request_id, "state": "review_ready"}

    monkeypatch.setattr(manager, "_finalize_isolated_request", flaky_finalize)
    monkeypatch.setattr(process_launcher.time, "sleep", lambda _seconds: None)

    event = manager._finalize_after_process_exit(request_id, 0)

    assert event == {"request_id": request_id, "state": "review_ready"}
    assert len(attempts) == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows lock timeout regression")
def test_duplicate_finalizer_lock_contention_defers_for_owner(
    monkeypatch, tmp_path
):
    owner_manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    duplicate_manager = process_launcher.ProcessManager(
        repo=owner_manager.repo,
        process_log_path=owner_manager.process_log_path,
        process_dir=owner_manager.process_dir,
        isolation_enabled=False,
    )
    request_id = "c" * 32
    owner_manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "state": "running",
        "metadata_path": str(tmp_path / "metadata.json"),
    })
    owner_entered = threading.Event()
    release_owner = threading.Event()
    review_calls: list[str] = []
    owner_errors: list[BaseException] = []

    def run_owner():
        try:
            # Hold the real request lock until the duplicate has exceeded the
            # configured Windows contention timeout. This models validation
            # and evidence work that legitimately takes longer than 20s
            # without making the test wait 20 wall-clock seconds.
            with owner_manager._request_lock(request_id):
                owner_entered.set()
                assert release_owner.wait(timeout=5.0)
                review_calls.append(request_id)
                owner_manager._append_event({
                    "request_id": request_id,
                    "task_id": "TASK_B1",
                    "runner": "claude_worker_b1",
                    "topic": "task_mcp",
                    "state": "review_ready",
                })
        except BaseException as exc:  # surfaced on the main test thread below
            owner_errors.append(exc)

    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_MAX_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_POLL_SECONDS", 0.001)
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("lock contention must not terminalize the task")
        ),
    )

    owner = threading.Thread(target=run_owner, name="finalizer-owner")
    owner.start()
    assert owner_entered.wait(timeout=5.0)
    try:
        duplicate = duplicate_manager._finalize_after_process_exit(request_id, 0)
        assert duplicate is not None
        assert duplicate["state"] == "running"
        assert duplicate["reconciliation_deferred"] == "request_lock_busy"
        assert duplicate["workspace_retained"] is True
        assert owner_manager._request_events(request_id)[-1]["state"] == "running"
    finally:
        release_owner.set()
        owner.join(timeout=5.0)

    assert not owner.is_alive()
    assert owner_errors == []
    assert review_calls == [request_id]
    states = [
        event["state"] for event in owner_manager._request_events(request_id)
    ]
    assert states.count("review_ready") == 1
    assert "finalize_failed" not in states


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
        # This test covers the generic terminal-callback fallback. Bridge
        # routes have a separate fail-closed cancellation publication gate.
        "adapter_id": "claude_cli",
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
    assert event["finalization_duration_ms"] >= 0
    assert event["release_transition_ok"] is True
    assert event["callback_enqueued"] is True
    assert "finalizer_retries_exhausted" in event["error"]
    assert transition_calls[0][1:4] == (
        "TASK_B1",
        "claude_worker_b1",
        "finalize_failed",
    )


@pytest.mark.parametrize(
    ("terminal_state", "terminal_error"),
    [
        ("finalize_failed", ""),
        ("release_pending", "terminal_failure_transition_failed:sqlite_busy"),
        (
            "validation_failed",
            "validation_exec_scratch_unavailable:C:\\Temp:noexec",
        ),
    ],
)
def test_retry_finalization_reuses_retained_workspace_without_provider(
    monkeypatch, tmp_path, terminal_state, terminal_error
):
    _open_gates(monkeypatch)
    from aiworkhub import worker_workspace

    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="review")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "d" * 32
    worktree_root = tmp_path / "worktrees"
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(worktree_root))
    workspace_path = worktree_root / request_id / "worktree"
    home_path = worktree_root / request_id / "home"
    workspace_path.parent.mkdir(parents=True)
    workspace_path.mkdir()
    home_path.mkdir()
    workspace = worker_workspace.WorkerWorkspace(
        request_id=request_id,
        repo=manager.repo,
        path=workspace_path,
        home=home_path,
        allowed_writes=("out/result.json",),
        parent_baseline={},
        workspace_baseline={},
    )
    status_path = manager.process_dir / f"{request_id}.supervisor.json"
    metadata_path = manager.process_dir / f"{request_id}.request.json"
    worker_workspace.write_json_0600(
        status_path, {"state": "exited", "exit_code": 0}
    )
    worker_workspace.write_json_0600(
        metadata_path,
        {
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "adapter_id": "vscode_lm",
            "sandbox_backend": "vscode_lm_in_process",
            "supervisor_status_path": str(status_path),
            "workspace": workspace.as_metadata(),
        },
    )
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "vscode_lm",
        "state": terminal_state,
        "error": terminal_error,
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
        "workspace_retained": True,
    })
    transitions = []
    monkeypatch.setattr(
        process_launcher.task_engine,
        "retry_finalize_failed",
        lambda *args, **kwargs: transitions.append((args, kwargs)) or {
            "ok": True,
            "stderr": "",
        },
    )

    def finalize(request_id_arg, supervisor_returncode=None):
        latest = manager._request_events(request_id_arg)[-1]
        assert latest["state"] == "finalizing"
        assert latest["finalization_retry"] is True
        assert latest["finalization_retry_provider_launched"] is False
        assert supervisor_returncode == 0
        return {
            "request_id": request_id_arg,
            "task_id": "TASK_B1",
            "state": "review_ready",
            "workspace_retained": True,
            "error": "",
        }

    monkeypatch.setattr(manager, "_finalize_isolated_request", finalize)

    result = manager.retry_finalization(request_id, "TASK_B1")

    assert result["ok"] is True, result
    assert result["state"] == "review_ready"
    assert result["provider_relaunched"] is False
    if terminal_state == "release_pending":
        assert transitions == []
    else:
        assert transitions and transitions[0][0][1:4] == (
            "TASK_B1",
            "claude_worker_b1",
            request_id,
        )


def test_retry_finalization_rejects_product_validation_failure(monkeypatch, tmp_path):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="blocked")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "e" * 32
    manager._append_event(
        {
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "state": "validation_failed",
            "error": "validation_failed:python3 -m pytest:rc=1",
            "workspace_retained": True,
        }
    )

    result = manager.retry_finalization(request_id, "TASK_B1")

    assert result["ok"] is False
    assert result["error"] == (
        "request_not_retryable_finalization_failure:validation_failed"
    )


def test_environment_blocked_validation_is_never_reported_as_validation_failed():
    """NF-2026-00271: a recoverable environment/sandbox restriction must route
    to the retryable ``finalize_failed`` bucket, never to the acceptance-blocking
    ``validation_failed`` (the candidate did not fail its gate)."""
    from aiworkhub import worker_workspace

    env_blocked = worker_workspace.ValidationEnvironmentBlocked(
        "validation_environment_blocked:missing_package:pytest:"
        "restrictions=missing_package:stderr=",
        [],
        restriction="missing_package",
        restrictions=("missing_package",),
    )
    assert (
        process_launcher._terminal_state_for_workspace_error(env_blocked)
        == "finalize_failed"
    )

    genuine = worker_workspace.ValidationRunError(
        "validation_failed:pytest:rc=1:stdout=:stderr=", []
    )
    assert (
        process_launcher._terminal_state_for_workspace_error(genuine)
        == "validation_failed"
    )

    # Environment/sandbox restrictions must never become ``validation_failed``:
    # an absent executable, an unavailable pytest runtime, an unprovisionable
    # exec scratch, and a missing validator package are all recoverable.
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("validation_executable_unavailable:pytest")
    ) == "finalize_failed"
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("validation_pytest_runtime_missing_pytest:/x")
    ) == "finalize_failed"
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError(
            "validation_exec_scratch_unavailable:request-home:noexec"
        )
    ) == "finalize_failed"
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("unsupported_sandbox_backend:bogus")
    ) == "finalize_failed"

    # Genuine candidate gate failures keep the acceptance-blocking state.
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("required_output_missing:out/result.json")
    ) == "validation_failed"
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("quality_gate_failed:coverage")
    ) == "validation_failed"

    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("scope_violation:out/result.json")
    ) == "scope_rejected"
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("promotion_scope:out")
    ) == "promotion_conflict"
    assert process_launcher._terminal_state_for_workspace_error(
        process_launcher.WorkspaceError("some_unexpected_error")
    ) == "finalize_failed"


def test_candidate_contract_validation_tokens_are_acceptance_blocking():
    """NF-2026-00271 (rework): deterministic candidate/card validation defects
    must stay acceptance-blocking ``validation_failed``. Only genuine
    environment/sandbox restrictions may reach the retryable ``finalize_failed``
    bucket; a catch-all there would let a provider-free ``retry_finalization``
    loop re-run a defect the candidate itself authored."""
    candidate_contract_tokens = (
        "validation_route_adapter_missing",
        "validation_commands_invalid",
        "validation_command_invalid",
        "validation_receipt_count_mismatch",
        "validation_failure_delta_too_large",
        "validation_command_limit_exceeded:9",
        "validation_cwd_not_directory:sub",
        "validation_pythonpath_not_directory:src",
        # Additional deterministic defects from the same declared-command
        # parsing and contract-shape surface.
        "validation_route_backend_mismatch:expected=landlock:recorded=bwrap",
        "validation_shell_syntax_forbidden:cd sub && pytest|tee",
        "validation_pythonpath_empty",
        "validation_cd_prefix_malformed",
        "invalid_validation_command",
    )
    for token in candidate_contract_tokens:
        assert (
            process_launcher._terminal_state_for_workspace_error(
                process_launcher.WorkspaceError(token)
            )
            == "validation_failed"
        ), token

    # The narrow environment/sandbox allowlist is unchanged: the same classifier
    # still routes a missing executable / runtime / scratch / backend to the
    # retryable bucket, never to ``validation_failed``.
    for token in (
        "validation_executable_unavailable:pytest",
        "validation_pytest_runtime_unavailable:/x",
        "validation_pytest_runtime_missing_pytest:/x",
        "validation_exec_scratch_unavailable:request-home:noexec",
        "unsupported_sandbox_backend:bogus",
        "validation_unsupported_in_sandbox:secure_sandbox_unavailable",
    ):
        assert (
            process_launcher._terminal_state_for_workspace_error(
                process_launcher.WorkspaceError(token)
            )
            == "finalize_failed"
        ), token


def test_validation_security_refusals_are_acceptance_blocking():
    """NF-2026-00271 (rework): security refusals and candidate validation
    defects must stay acceptance-blocking ``validation_failed``. The exact-token
    allowlist -- not a broad ``validation_executable_`` /
    ``validation_pytest_runtime_`` family prefix -- is the only route to the
    recoverable ``finalize_failed`` bucket, so a world-writable or
    untrusted-owner validator binary/runtime-root, a symlink-forbidden pytest
    runtime, or an unapproved/non-executable validator fails closed instead of
    failing open."""
    security_refusal_tokens = (
        "validation_executable_world_writable:/x",
        "validation_executable_untrusted_owner:/x",
        "validation_executable_runtime_root_world_writable:/x",
        "validation_executable_runtime_root_untrusted_owner:/x",
        "validation_executable_untrusted_runtime_root:/x",
        "validation_executable_not_approved:pylint",
        "validation_executable_not_executable:/x",
        "validation_pytest_runtime_world_writable:/x",
        "validation_pytest_runtime_untrusted_owner:/x",
        "validation_pytest_runtime_symlink_forbidden:/x",
    )
    for token in security_refusal_tokens:
        assert (
            process_launcher._terminal_state_for_workspace_error(
                process_launcher.WorkspaceError(token)
            )
            == "validation_failed"
        ), token


def test_validation_recoverable_environment_tokens_are_exact():
    """NF-2026-00271 (rework): only the six colon-terminated recoverable tokens
    route to the retryable ``finalize_failed`` bucket. A broad family prefix
    would fail-open and reclassify a security refusal as recoverable."""
    recoverable_tokens = (
        "validation_executable_unavailable:pytest",
        "validation_pytest_runtime_unavailable:/x",
        "validation_pytest_runtime_missing_pytest:/x",
        "validation_exec_scratch_unavailable:request-home:noexec",
        "unsupported_sandbox_backend:bogus",
        "validation_unsupported_in_sandbox:secure_sandbox_unavailable",
    )
    for token in recoverable_tokens:
        assert (
            process_launcher._terminal_state_for_workspace_error(
                process_launcher.WorkspaceError(token)
            )
            == "finalize_failed"
        ), token


def test_run_declared_validations_preserves_environment_blocked_subtype(monkeypatch):
    """The finalizer seam must keep ``ValidationEnvironmentBlocked`` (with its
    ``terminal_state``/``restriction``/``recoverable`` flags) so routing can
    tell an environment block apart from a genuine candidate failure."""
    from aiworkhub import worker_workspace

    raised = worker_workspace.ValidationEnvironmentBlocked(
        "validation_environment_blocked:missing_package:pytest:"
        "restrictions=missing_package:stderr=",
        [{
            "command": "pytest",
            "returncode": 1,
            "failure_receipt": {"failure_class": "absent_validator_module"},
        }],
        restriction="missing_package",
        restrictions=("missing_package",),
    )

    monkeypatch.setattr(
        process_launcher.quality_evidence,
        "normalize_behavioral_contract",
        lambda work_kind, commands, roles: ("code", ["gate"]),
    )
    monkeypatch.setattr(process_launcher, "_validation_route_kwargs", lambda meta: {})

    def _raise(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(process_launcher, "run_validations", _raise)

    with pytest.raises(worker_workspace.ValidationEnvironmentBlocked) as caught:
        process_launcher._run_declared_validations(
            object(),
            {"validation": ["pytest"], "work_kind": "code"},
            {"adapter_id": "claude_cli"},
        )

    exc = caught.value
    assert isinstance(exc, worker_workspace.ValidationEnvironmentBlocked)
    assert exc.terminal_state == "validation_environment_blocked"
    assert exc.recoverable is True
    assert exc.requires_supersede is False
    assert exc.restriction == "missing_package"
    assert exc.restrictions == ("missing_package",)
    assert exc.results[0]["behavioral_role"] == "gate"
    assert exc.results[0]["failure_receipt"]["failure_class"] == "absent_validator_module"


def test_run_declared_validations_replays_authenticated_structural_denial_once(
    monkeypatch, tmp_path
):
    from aiworkhub import worker_workspace

    workspace = SimpleNamespace(
        request_id="req-structural",
        path=tmp_path / "worktree",
        repo=tmp_path / "repo",
    )
    metadata = {
        "adapter_id": "codex_cli",
        "request_id": workspace.request_id,
        "task_id": "task-structural",
        "workspace": {
            "request_id": workspace.request_id,
            "path": str(workspace.path),
            "repo": str(workspace.repo),
        },
    }
    denied_row = {
        "command": "pytest",
        "returncode": 1,
        "metadata_broker_denial_attributed": True,
        "metadata_broker_denials": [{
            "schema": "aiworkhub.metadata_broker_denial.v1",
            "authenticated": True,
            "terminal": True,
            "reason": "metadata_broker_hardlink_forbidden",
            "syscall_nr": 90,
        }],
    }
    raised = worker_workspace.ValidationEnvironmentBlocked(
        "validation_environment_blocked:metadata_broker_denial",
        [denied_row],
        restriction="metadata_broker_denial",
        restrictions=("metadata_broker_denial",),
    )
    calls = []

    monkeypatch.setattr(
        process_launcher.quality_evidence,
        "normalize_behavioral_contract",
        lambda work_kind, commands, roles: ("code", ["gate"]),
    )
    monkeypatch.setattr(
        process_launcher,
        "_validation_route_kwargs",
        lambda _meta: {"backend": "landlock"},
    )

    def _run(candidate_workspace, commands, **route):
        calls.append((candidate_workspace, list(commands), dict(route)))
        if len(calls) == 1:
            raise raised
        return [{"command": "pytest", "returncode": 0}]

    monkeypatch.setattr(process_launcher, "run_validations", _run)

    rows = process_launcher._run_declared_validations(
        workspace,
        {"validation": ["pytest"], "work_kind": "code"},
        metadata,
    )

    assert len(calls) == 2
    assert calls[0][0] is calls[1][0] is workspace
    assert calls[0][1] == calls[1][1] == ["pytest"]
    assert calls[0][2] == {"backend": "landlock"}
    assert calls[1][2] == {
        "backend": "landlock",
        "outer_validation_authority": True,
    }
    assert rows[0]["behavioral_role"] == "gate"
    receipt = rows[0]["validation_capability_replay"]
    assert receipt["attempt"] == 1
    assert receipt["profile"] == "metadata_isolated_v1"
    assert receipt["capabilities"] == ["hardlink"]
    assert receipt["request_identity"]["request_id"] == workspace.request_id
    assert receipt["original_denial"][0]["metadata_broker_denial_attributed"] is True


def test_validation_capability_replay_rejects_workspace_identity_mismatch(
    monkeypatch, tmp_path
):
    from aiworkhub import worker_workspace

    workspace = SimpleNamespace(
        request_id="req-identity",
        path=tmp_path / "worktree",
        repo=tmp_path / "repo",
    )
    row = {
        "command": "pytest",
        "returncode": 1,
        "metadata_broker_denial_attributed": True,
        "metadata_broker_denials": [{
            "schema": "aiworkhub.metadata_broker_denial.v1",
            "authenticated": True,
            "terminal": True,
            "reason": "fchmodat denied",
            "syscall_nr": 268,
        }],
    }
    raised = worker_workspace.ValidationEnvironmentBlocked(
        "validation_environment_blocked:metadata_broker_denial",
        [row],
        restriction="metadata_broker_denial",
        restrictions=("metadata_broker_denial",),
    )
    calls = 0

    monkeypatch.setattr(
        process_launcher.quality_evidence,
        "normalize_behavioral_contract",
        lambda work_kind, commands, roles: ("code", ["gate"]),
    )
    monkeypatch.setattr(
        process_launcher,
        "_validation_route_kwargs",
        lambda _meta: {"backend": "landlock"},
    )

    def _run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise raised

    monkeypatch.setattr(process_launcher, "run_validations", _run)

    with pytest.raises(worker_workspace.ValidationRunError) as caught:
        process_launcher._run_declared_validations(
            workspace,
            {"validation": ["pytest"], "work_kind": "code"},
            {
                "adapter_id": "codex_cli",
                "request_id": workspace.request_id,
                "task_id": "task-identity",
                "workspace": {
                    "request_id": workspace.request_id,
                    "path": str(tmp_path / "wrong-worktree"),
                    "repo": str(workspace.repo),
                },
            },
        )

    assert calls == 1
    assert str(caught.value) == (
        "validation_failed:validation_capability_replay_identity_mismatch"
    )


def test_run_declared_validations_keeps_genuine_failure_as_validation_run_error(monkeypatch):
    """A real gate failure must stay ``ValidationRunError``/``validation_failed``
    through the finalizer seam -- it is never weakened into an environment
    block (NF-WAVE-SANDBOX-TRUTH)."""
    from aiworkhub import worker_workspace

    raised = worker_workspace.ValidationRunError(
        "validation_failed:pytest:rc=1:stdout=:stderr=",
        [{"command": "pytest", "returncode": 1}],
    )

    monkeypatch.setattr(
        process_launcher.quality_evidence,
        "normalize_behavioral_contract",
        lambda work_kind, commands, roles: ("code", ["gate"]),
    )
    monkeypatch.setattr(process_launcher, "_validation_route_kwargs", lambda meta: {})

    def _raise(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(process_launcher, "run_validations", _raise)

    with pytest.raises(worker_workspace.ValidationRunError) as caught:
        process_launcher._run_declared_validations(
            object(),
            {"validation": ["pytest"], "work_kind": "code"},
            {"adapter_id": "claude_cli"},
        )

    exc = caught.value
    assert not isinstance(exc, worker_workspace.ValidationEnvironmentBlocked)
    assert exc.terminal_state == "validation_failed"
    assert exc.requires_supersede is True
    assert exc.results[0]["behavioral_role"] == "gate"


# RM43: schema-role mypy comparison is supervisor-owned. The candidate row
# remains a truthful nonzero receipt even when its normalized diagnostic
# multiset is fully covered by the pinned baseline.
def _baseline_diagnostic_validation_row(
    lines: list[str], *, role: str = "schema"
) -> dict:
    return {
        "command": ".venv/bin/python -m mypy src",
        "declared_command": ".venv/bin/python -m mypy src",
        "argv": [".venv/bin/python", "-m", "mypy", "src"],
        "declared_argv": [".venv/bin/python", "-m", "mypy", "src"],
        "executed_argv": [".venv/bin/python", "-m", "mypy", "src"],
        "interpreter_authority": {"path": ".venv/bin/python"},
        "sandbox_backend": "landlock",
        "execution_boundary": "os_sandbox",
        "cwd": None,
        "env_override": None,
        "timeout_seconds": 30,
        "returncode": 1,
        "timed_out": False,
        "stdout_tail": "\n".join(lines + ["Found %d errors" % len(lines)]),
        "stderr_tail": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
        "behavioral_role": role,
    }


def _baseline_diagnostic_validation_workspace(tmp_path: Path):
    path = tmp_path / "candidate"
    home = tmp_path / "home"
    path.mkdir()
    home.mkdir()
    return process_launcher.WorkerWorkspace(
        request_id="candidate",
        repo=tmp_path,
        path=path,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
        base_oid="a" * 40,
    )


def _run_baseline_diagnostic_validation_compare(
    monkeypatch, tmp_path, candidate, baseline
):
    workspace = _baseline_diagnostic_validation_workspace(tmp_path)
    monkeypatch.setattr(
        process_launcher, "create_workspace", lambda *_args, **_kwargs: workspace
    )
    monkeypatch.setattr(
        process_launcher, "cleanup_workspace", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        process_launcher, "run_validations", lambda *_args, **_kwargs: [baseline]
    )
    monkeypatch.setattr(
        process_launcher,
        "_validation_route_kwargs",
        lambda metadata: {
            "adapter_id": metadata["adapter_id"],
            "backend": metadata["sandbox_backend"],
        },
    )
    return process_launcher._compare_schema_mypy_baseline(
        workspace,
        {"allowed_writes": [], "read_first": []},
        {"adapter_id": "claude_cli", "sandbox_backend": "landlock"},
        [candidate],
    )


def test_baseline_diagnostic_validation_identical_nine_diagnostics(
    monkeypatch, tmp_path
):
    lines = [
        f"src/a.py:{index}: error: bad {index}  [arg-type]"
        for index in range(1, 10)
    ]
    rows = _run_baseline_diagnostic_validation_compare(
        monkeypatch,
        tmp_path,
        _baseline_diagnostic_validation_row(lines),
        _baseline_diagnostic_validation_row(lines),
    )
    assert rows[0]["returncode"] == 1
    assert rows[0]["baseline_comparison"]["outcome"] == (
        "baseline_no_new_diagnostics"
    )
    assert rows[0]["baseline_comparison"]["candidate_count"] == 9


def test_baseline_diagnostic_validation_rejects_one_new_diagnostic(
    monkeypatch, tmp_path
):
    candidate = _baseline_diagnostic_validation_row(
        [
            "src/a.py:1: error: old  [arg-type]",
            "src/a.py:2: error: new  [arg-type]",
        ]
    )
    baseline = _baseline_diagnostic_validation_row(
        ["src/a.py:8: error: old  [arg-type]"]
    )
    with pytest.raises(
        process_launcher.WorkspaceError, match="baseline_mypy_new_diagnostics"
    ):
        _run_baseline_diagnostic_validation_compare(
            monkeypatch, tmp_path, candidate, baseline
        )


def test_baseline_diagnostic_validation_rejects_multiplicity_increase(
    monkeypatch, tmp_path
):
    line = "src/a.py:1: error: duplicated  [arg-type]"
    with pytest.raises(
        process_launcher.WorkspaceError, match="baseline_mypy_new_diagnostics"
    ):
        _run_baseline_diagnostic_validation_compare(
            monkeypatch,
            tmp_path,
            _baseline_diagnostic_validation_row([line, line]),
            _baseline_diagnostic_validation_row([line]),
        )


def test_baseline_diagnostic_validation_ignores_line_shift_and_dot_prefix(
    monkeypatch, tmp_path
):
    _run_baseline_diagnostic_validation_compare(
        monkeypatch,
        tmp_path,
        _baseline_diagnostic_validation_row(
            ["./src/a.py:99:4: error: stable  [arg-type]"]
        ),
        _baseline_diagnostic_validation_row(
            ["src/a.py:2: error: stable  [arg-type]"]
        ),
    )


def test_baseline_diagnostic_validation_requires_base_oid(monkeypatch, tmp_path):
    workspace = process_launcher.replace(
        _baseline_diagnostic_validation_workspace(tmp_path), base_oid=None
    )
    with pytest.raises(
        process_launcher.WorkspaceError, match="baseline_base_oid_missing"
    ):
        process_launcher._compare_schema_mypy_baseline(
            workspace,
            {"allowed_writes": []},
            {"adapter_id": "claude_cli"},
            [
                _baseline_diagnostic_validation_row(
                    ["src/a.py:1: error: x  [arg-type]"]
                )
            ],
        )


def test_baseline_diagnostic_validation_rejects_baseline_execution_failure(
    monkeypatch, tmp_path
):
    candidate = _baseline_diagnostic_validation_row(
        ["src/a.py:1: error: x  [arg-type]"]
    )
    failed = _baseline_diagnostic_validation_row([])
    failed["timed_out"] = True
    with pytest.raises(
        process_launcher.WorkspaceError,
        match="baseline_mypy_candidate_not_comparable",
    ):
        _run_baseline_diagnostic_validation_compare(
            monkeypatch, tmp_path, candidate, failed
        )


def test_baseline_diagnostic_validation_rejects_authority_mismatch(
    monkeypatch, tmp_path
):
    candidate = _baseline_diagnostic_validation_row(
        ["src/a.py:1: error: x  [arg-type]"]
    )
    baseline = _baseline_diagnostic_validation_row(
        ["src/a.py:1: error: x  [arg-type]"]
    )
    baseline["sandbox_backend"] = "bubblewrap"
    with pytest.raises(
        process_launcher.WorkspaceError,
        match="baseline_validation_authority_mismatch",
    ):
        _run_baseline_diagnostic_validation_compare(
            monkeypatch, tmp_path, candidate, baseline
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "-q", "-k", "mypy"],
        ["python", "fake_mypy.py"],
        ["ruff", "-m", "mypy"],
        ["ruff", "check", "mypy"],
    ],
)
def test_baseline_diagnostic_validation_rejects_mypy_lookalike_command(
    tmp_path, argv
):
    row = _baseline_diagnostic_validation_row(
        ["src/a.py:1: error: x  [arg-type]"]
    )
    row["argv"] = row["executed_argv"] = argv
    with pytest.raises(
        process_launcher.WorkspaceError, match="baseline_comparison_ineligible"
    ):
        process_launcher._compare_schema_mypy_baseline(
            _baseline_diagnostic_validation_workspace(tmp_path),
            {"allowed_writes": []},
            {"adapter_id": "claude_cli"},
            [row],
        )


def test_baseline_diagnostic_validation_identical_pytest_failure_remains_red(
    tmp_path,
):
    pytest_row = _baseline_diagnostic_validation_row(
        ["src/a.py:1: error: x  [arg-type]"], role="test"
    )
    pytest_row["command"] = pytest_row["declared_command"] = "pytest -q"
    pytest_row["argv"] = pytest_row["executed_argv"] = ["pytest", "-q"]
    with pytest.raises(
        process_launcher.WorkspaceError, match="baseline_comparison_ineligible"
    ):
        process_launcher._compare_schema_mypy_baseline(
            _baseline_diagnostic_validation_workspace(tmp_path),
            {"allowed_writes": []},
            {"adapter_id": "claude_cli"},
            [pytest_row],
        )


def test_baseline_diagnostic_validation_rejects_unexpected_mypy_returncode(
    monkeypatch, tmp_path
):
    candidate = _baseline_diagnostic_validation_row(
        ["src/a.py:1: error: x  [arg-type]"]
    )
    baseline = _baseline_diagnostic_validation_row(
        ["src/a.py:1: error: x  [arg-type]"]
    )
    baseline["returncode"] = 2
    with pytest.raises(
        process_launcher.WorkspaceError,
        match="baseline_mypy_candidate_not_comparable",
    ):
        _run_baseline_diagnostic_validation_compare(
            monkeypatch, tmp_path, candidate, baseline
        )


def test_run_declared_validations_accepts_only_baselined_mypy_failure(
    monkeypatch, tmp_path
):
    candidate_mypy = _baseline_diagnostic_validation_row(
        ["src/a.py:20: error: existing  [arg-type]"]
    )
    passing_pytest = {
        "command": "pytest -q",
        "declared_command": "pytest -q",
        "argv": ["pytest", "-q"],
        "executed_argv": ["pytest", "-q"],
        "returncode": 0,
        "timed_out": False,
    }
    baseline_mypy = _baseline_diagnostic_validation_row(
        ["src/a.py:2: error: existing  [arg-type]"]
    )
    workspace = _baseline_diagnostic_validation_workspace(tmp_path)
    calls = 0

    def run(_workspace, commands, **_kwargs):
        nonlocal calls
        calls += 1
        if len(commands) == 2:
            raise process_launcher.ValidationRunError(
                "candidate failed", [candidate_mypy, passing_pytest]
            )
        raise process_launcher.ValidationRunError("baseline failed", [baseline_mypy])

    monkeypatch.setattr(process_launcher, "run_validations", run)
    monkeypatch.setattr(
        process_launcher, "create_workspace", lambda *_args, **_kwargs: workspace
    )
    monkeypatch.setattr(
        process_launcher, "cleanup_workspace", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        process_launcher,
        "_validation_route_kwargs",
        lambda metadata: {
            "adapter_id": metadata["adapter_id"],
            "backend": metadata["sandbox_backend"],
        },
    )
    rows = process_launcher._run_declared_validations(
        workspace,
        {
            "work_kind": "generic",
            "validation": ["python -m mypy src/a.py", "pytest -q"],
            "validation_roles": ["schema", "regression"],
            "allowed_writes": [],
        },
        {"adapter_id": "claude_cli", "sandbox_backend": "landlock"},
    )
    assert calls == 2
    assert len(rows) == 2
    assert rows[0]["baseline_comparison"]["outcome"] == (
        "baseline_no_new_diagnostics"
    )
    assert rows[1]["returncode"] == 0


def test_run_declared_validations_keeps_mixed_mypy_and_pytest_failures_red(
    monkeypatch, tmp_path
):
    candidate_mypy = _baseline_diagnostic_validation_row(
        ["src/a.py:20: error: existing  [arg-type]"]
    )
    failed_pytest = {
        "command": "pytest -q",
        "declared_command": "pytest -q",
        "argv": ["pytest", "-q"],
        "executed_argv": ["pytest", "-q"],
        "returncode": 1,
        "timed_out": False,
    }

    def run(*_args, **_kwargs):
        raise process_launcher.ValidationRunError(
            "candidate failed", [candidate_mypy, failed_pytest]
        )

    monkeypatch.setattr(process_launcher, "run_validations", run)
    monkeypatch.setattr(
        process_launcher,
        "_validation_route_kwargs",
        lambda metadata: {
            "adapter_id": metadata["adapter_id"],
            "backend": metadata["sandbox_backend"],
        },
    )
    with pytest.raises(
        process_launcher.ValidationRunError,
        match="baseline_comparison_failed:baseline_comparison_ineligible",
    ):
        process_launcher._run_declared_validations(
            _baseline_diagnostic_validation_workspace(tmp_path),
            {
                "work_kind": "generic",
                "validation": ["python -m mypy src/a.py", "pytest -q"],
                "validation_roles": ["schema", "regression"],
                "allowed_writes": [],
            },
            {"adapter_id": "claude_cli", "sandbox_backend": "landlock"},
        )


def test_reconcile_defers_live_windows_pid_without_start_ticks(monkeypatch, tmp_path):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "c" * 32
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "state": "running",
        "pid": 4242,
        "pid_start_ticks": None,
        "metadata_path": str(tmp_path / "metadata.json"),
    })
    watched = []
    monkeypatch.setattr(manager, "_watch_persisted_request", lambda *args: watched.append(args))
    monkeypatch.setattr(
        manager,
        "_finalize_after_process_exit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("live pid finalized")),
    )

    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(process_launcher.threading, "Thread", ImmediateThread)

    result = manager._reconcile_persisted_requests()

    assert result == {"watched": 0, "finalized": 0}
    assert watched == []


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


def test_coordinator_runner_is_never_accepted_as_worker_identity() -> None:
    with pytest.raises(
        process_launcher.LaunchRejected,
        match="coordinator_runner_cannot_launch_worker",
    ):
        process_launcher._validate_adapter_identity("codex", "glm_vscode_lm")


def test_workforce_identity_rejects_absent_literal_model() -> None:
    with pytest.raises(process_launcher.LaunchRejected, match="workforce_model_mismatch"):
        process_launcher.validate_workforce_identity(
            "claude_sonnet-5", "claude_cli", "claude-sonnet-4.6"
        )


def test_workforce_identity_rejects_mismatched_runner_and_model() -> None:
    with pytest.raises(process_launcher.LaunchRejected, match="workforce_route_absent"):
        process_launcher.validate_workforce_identity(
            "claude_sonnet-4.6", "claude_cli", "claude-haiku-4.5"
        )


def test_workforce_identity_accepts_valid_alias_for_high_risk_task() -> None:
    canonical = process_launcher.validate_workforce_identity(
        "claude_sonnet-5", "claude_cli", "sonnet", risk_tier="high"
    )
    assert canonical == "claude-sonnet-5"


def test_workforce_identity_rejects_risk_incapable_route() -> None:
    with pytest.raises(
        process_launcher.LaunchRejected, match="workforce_route_risk_incapable"
    ):
        process_launcher.validate_workforce_identity(
            "claude_haiku-4.5", "claude_cli", "haiku", risk_tier="critical"
        )


def test_workforce_identity_rejects_disabled_route(monkeypatch: pytest.MonkeyPatch) -> None:
    disabled = dict(process_launcher._CANONICAL_WORKFORCE[("claude_sonnet-5", "claude_cli")])
    disabled["enabled"] = False
    monkeypatch.setitem(
        process_launcher._CANONICAL_WORKFORCE, ("claude_sonnet-5", "claude_cli"), disabled
    )
    with pytest.raises(process_launcher.LaunchRejected, match="workforce_route_disabled"):
        process_launcher.validate_workforce_identity("claude_sonnet-5", "claude_cli", "sonnet")


def test_workforce_identity_rejects_unavailable_route(monkeypatch: pytest.MonkeyPatch) -> None:
    unavailable = dict(process_launcher._CANONICAL_WORKFORCE[("claude_sonnet-5", "claude_cli")])
    unavailable["available"] = False
    monkeypatch.setitem(
        process_launcher._CANONICAL_WORKFORCE, ("claude_sonnet-5", "claude_cli"), unavailable
    )
    with pytest.raises(process_launcher.LaunchRejected, match="workforce_route_unavailable"):
        process_launcher.validate_workforce_identity("claude_sonnet-5", "claude_cli", "sonnet")


def test_workforce_identity_is_scoped_to_claude_runner_family() -> None:
    # A non-claude runner family (grok, glm, deepseek, codex, copilot) keeps its
    # existing, unchanged identity handling -- this repository's canonical
    # workforce table only ever governs the claude_* family.
    assert (
        process_launcher.validate_workforce_identity(
            "grok_runner", "grok_kilo_cli", "xai/grok-4.6"
        )
        == "xai/grok-4.6"
    )


def test_workforce_identity_accepts_canonical_native_codex_route() -> None:
    canonical = process_launcher.validate_workforce_identity(
        "codex_gpt-5.5", "codex_cli", "gpt-5.5", risk_tier="critical"
    )
    assert canonical == "gpt-5.5"


def test_workforce_identity_rejects_native_codex_route_model_mismatch() -> None:
    with pytest.raises(process_launcher.LaunchRejected, match="workforce_model_mismatch"):
        process_launcher.validate_workforce_identity(
            "codex_gpt-5.5", "codex_cli", "gpt-5.4", risk_tier="high"
        )


def test_workforce_identity_keeps_non_table_codex_route_unchanged() -> None:
    assert (
        process_launcher.validate_workforce_identity(
            "codex_gpt-5.4", "codex_cli", "gpt-5.4", risk_tier="critical"
        )
        == "gpt-5.4"
    )


def test_workforce_identity_skips_unpinned_model() -> None:
    # A launch that never pins a model is unaffected: existing exact-match
    # launches without an explicit model keep their prior behavior unchanged.
    assert (
        process_launcher.validate_workforce_identity("claude_worker_b1", "claude_cli", None)
        is None
    )


def test_launch_rejects_workforce_absent_model_before_provider_spawn(
    tmp_path, monkeypatch,
) -> None:
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: {**_card(), "runner": "claude_sonnet-5"}),
        argv=[sys.executable, "-c", "pass"],
    )
    monkeypatch.setattr(
        manager,
        "_resolve_provider_env",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("provider credentials must not be resolved")
        ),
    )

    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_sonnet-5",
        topic="task_mcp",
        adapter_id="claude_cli",
        model="claude-sonnet-4.6",
        timeout_seconds=30,
    )

    assert result["ok"] is False
    assert "workforce_model_mismatch" in result["blocked_reason"]


def test_launch_rejects_mismatched_runner_and_model_before_provider_spawn(
    tmp_path, monkeypatch,
) -> None:
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: {**_card(), "runner": "claude_sonnet-4.6"}),
        argv=[sys.executable, "-c", "pass"],
    )
    monkeypatch.setattr(
        manager,
        "_resolve_provider_env",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("provider credentials must not be resolved")
        ),
    )

    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_sonnet-4.6",
        topic="task_mcp",
        adapter_id="claude_cli",
        model="claude-haiku-4.5",
        timeout_seconds=30,
    )

    assert result["ok"] is False
    assert "workforce_route_absent" in result["blocked_reason"]


def test_launch_accepts_valid_canonical_tuple_for_high_risk_task(
    tmp_path, monkeypatch,
) -> None:
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(
            lambda: {**_card(), "runner": "claude_sonnet-5", "risk_tier": "high"}
        ),
        argv=[sys.executable, "-c", "print('claimed only')"],
    )

    launched = manager.launch(
        task_id="TASK_B1",
        runner="claude_sonnet-5",
        topic="task_mcp",
        adapter_id="claude_cli",
        model="sonnet",
        timeout_seconds=30,
    )

    assert launched["ok"] is True
    result = _wait_terminal(manager, launched["request_id"])
    assert result["state"] == "exited_without_review"


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
    monkeypatch.setattr(
        process_launcher.runtime_adapters, "_is_windows_host", lambda: False
    )
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
    monkeypatch.setattr(
        process_launcher,
        "_validation_route_kwargs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty validation must not resolve a sandbox route")
        ),
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
    assert event["review_automation"]["state"] == "pending"
    assert event["review_automation"]["error"].startswith("KeyError:")
    assert event["workspace_retained"] is True
    assert event["promoted_paths"] == []
    assert event["finalization_duration_ms"] >= 0
    phase_durations = event["finalization_phase_durations_ms"]
    assert 0.0 <= phase_durations["validation"] < 5.0
    assert phase_durations["workspace_scope"] >= 0.0
    assert phase_durations["evidence_and_transition"] >= 0.0
    assert sum(phase_durations.values()) <= event["finalization_duration_ms"] + 1.0
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
    artifact_receipt = evidence["attempt_artifact_manifest"]
    assert artifact_receipt["verified"] is True
    assert event["attempt_artifact_manifest"] == artifact_receipt
    verification = process_launcher.attempt_artifacts.verify_json_bundle(
        Path(artifact_receipt["manifest_path"]).parent
    )
    assert verification["attempt_id"] == "req-review-first-1"
    assert verification["roles"] == [
        "diff",
        "metadata",
        "review",
        "usage",
        "validation",
    ]
    evidence_record = process_launcher.evidence_levels.validate_evidence_record(
        evidence["evidence_record"]
    )
    assert (
        evidence_record.evidence_level
        == process_launcher.evidence_levels.EvidenceLevel.STATIC_EVIDENCE
    )


def test_reconcile_retries_durable_review_automation_after_restart(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    repo.mkdir()
    kwargs = dict(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        isolation_enabled=False,
    )
    review_db = tmp_path / "canonical-task-queue.sqlite"
    monkeypatch.setattr(
        process_launcher.review_orchestrator,
        "canonical_review_db",
        lambda _manager: review_db,
    )
    first = process_launcher.ProcessManager(**kwargs)
    registration = {
        "target_task_id": "TASK_B1",
        "target_request_id": "req-review-first-1",
        "claim_epoch": "1",
        "packet_sha256": "a" * 64,
        "candidate_sha256": "b" * 64,
    }
    first._append_event({
        "request_id": "req-review-first-1",
        "task_id": "TASK_B1",
        "state": "review_ready",
        "review_automation": {"state": "pending", "registration": registration},
    })
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_reconcile_persisted_requests",
        lambda self: {"watched": 0, "finalized": 0},
    )
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_gc_finalized_workspaces",
        lambda self: {"gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0},
    )
    calls = []
    monkeypatch.setattr(
        process_launcher.review_orchestrator,
        "register_candidate",
        lambda manager, **values: calls.append((manager, values)) or object(),
    )

    restarted = process_launcher.ProcessManager(**kwargs)
    result = restarted.reconcile()
    assert result["automation_retried"] == 1
    assert result["automation_seeded"] == 1
    assert result["automation_failed"] == 0
    assert calls[0][1]["registration"] == registration
    latest = restarted._latest_by_request()["req-review-first-1"]
    assert latest["state"] == "review_ready"
    assert latest["review_automation"]["state"] == "seeded"

    replay = process_launcher.ProcessManager(**kwargs).reconcile()
    assert replay["automation_retried"] == 0
    assert len(calls) == 1

    monkeypatch.setattr(
        process_launcher.review_orchestrator,
        "register_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("store unavailable")),
    )
    restarted._append_event({
        "request_id": "req-review-unavailable-1",
        "task_id": "TASK_B2",
        "state": "review_ready",
        "review_automation": {
            "state": "pending",
            "registration": {**registration, "target_task_id": "TASK_B2",
                             "target_request_id": "req-review-unavailable-1"},
        },
    })
    unavailable = process_launcher.ProcessManager(**kwargs).reconcile()
    assert unavailable["automation_retried"] == 1
    assert unavailable["automation_seeded"] == 0
    assert unavailable["automation_failed"] == 1
    assert unavailable["automation_failures"] == [{
        "request_id": "req-review-unavailable-1",
        "error": "OSError:store unavailable",
    }]
    unavailable_manager = process_launcher.ProcessManager(**kwargs)
    failed_event = unavailable_manager._latest_by_request()["req-review-unavailable-1"]
    assert failed_event["state"] == "review_ready"
    assert failed_event["review_automation"]["state"] == "pending"
    assert failed_event["review_automation"]["error"] == "OSError:store unavailable"


def test_reconcile_contains_unready_review_store_without_second_database(
    monkeypatch, tmp_path
):
    manager = process_launcher.ProcessManager(
        repo=tmp_path / "missing-repository-authority",
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        isolation_enabled=False,
    )
    monkeypatch.setattr(
        manager, "_reconcile_persisted_requests",
        lambda: {"watched": 0, "finalized": 0},
    )
    monkeypatch.setattr(
        manager, "_gc_finalized_workspaces",
        lambda: {"gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0},
    )

    result = manager.reconcile()

    assert result["automation_unavailable"] is True
    assert result["attempted"] == 0
    assert not hasattr(manager, "_review_lifecycle_db")
    assert not (tmp_path / "processes" / "review_lifecycle.sqlite").exists()


def test_quality_reviewer_finalization_seals_attempt_bundle(
    monkeypatch, tmp_path
):
    if os.name == "nt":
        pytest.skip("review finalization requires the POSIX secure sandbox backend")
    _open_gates(monkeypatch)
    from aiworkhub import worker_workspace

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace_dir = tmp_path / "review-workspace"
    workspace_dir.mkdir()
    marker = workspace_dir / "README.md"
    marker.write_text("review target\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=workspace_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=workspace_dir,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Task MCP Tests"],
        cwd=workspace_dir,
        check=True,
    )
    subprocess.run(["git", "add", "README.md"], cwd=workspace_dir, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "baseline"], cwd=workspace_dir, check=True
    )
    home = tmp_path / "review-home"
    home.mkdir()
    request_id = "req-quality-artifacts-1"
    workspace = worker_workspace.WorkerWorkspace(
        request_id=request_id,
        repo=repo,
        path=workspace_dir,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )

    def processing_card():
        card = _card()
        card.update({
            "status": "processing",
            "worker_status": "claimed",
            "claimed_by": "claude_worker_b1",
            "topic": "task_mcp",
        })
        return card

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(processing_card),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", "pass"], repo),
        isolation_enabled=False,
    )
    stdout_path = tmp_path / f"{request_id}.stdout.log"
    stderr_path = tmp_path / f"{request_id}.stderr.log"
    status_path = tmp_path / f"{request_id}.supervisor.json"
    metadata_path = tmp_path / f"{request_id}.request.json"
    stdout_path.write_text("review complete\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    worker_workspace.write_json_0600(
        status_path, {"state": "exited", "exit_code": 0}
    )
    metadata = {
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "claude-sonnet",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "sandbox_backend": "landlock",
        "quality_review": {
            "target_request_id": "target-request",
            "target_task_id": "TARGET_TASK",
            "lens": "correctness",
        },
        "workspace": workspace.as_metadata(),
    }
    worker_workspace.write_json_0600(metadata_path, metadata)
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": 999_999_999,
        "pid_start_ticks": 1,
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
    })
    verified_receipt = {
        "schema_id": "aiworkhub.quality_reviewer_receipt.v1",
        "report": {"lens": "correctness", "findings": []},
    }
    monkeypatch.setattr(
        process_launcher,
        "_verified_quality_review_receipt",
        lambda *_args: verified_receipt,
    )
    review_calls = []
    monkeypatch.setattr(
        manager,
        "_review_terminal_exact",
        lambda _metadata, substatus, **kwargs: (
            review_calls.append((substatus, kwargs["evidence"]))
            or {"ok": True}
        ),
    )

    event = manager._finalize_isolated_request(request_id, supervisor_returncode=0)

    assert event["state"] == "review_ready"
    assert len(review_calls) == 1
    receipt = review_calls[0][1]["attempt_artifact_manifest"]
    assert receipt["verified"] is True
    assert event["attempt_artifact_manifest"] == receipt
    assert process_launcher.attempt_artifacts.verify_json_bundle(
        Path(receipt["manifest_path"]).parent
    )["verified"] is True
    assert review_calls[0][1]["evidence_record"]["evidence_level"] == (
        "static_evidence"
    )


def test_empty_declared_validation_skips_route_and_scratch(monkeypatch, tmp_path):
    workspace = SimpleNamespace(path=tmp_path, home=tmp_path)
    monkeypatch.setattr(
        process_launcher,
        "_validation_route_kwargs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty validation must not resolve a route")
        ),
    )
    monkeypatch.setattr(
        process_launcher,
        "run_validations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty validation must not enter the executor")
        ),
    )

    assert process_launcher._run_declared_validations(
        workspace,
        {"validation": []},
        {"adapter_id": "vscode_lm", "sandbox_backend": "deterministic_validation"},
    ) == []


def test_validation_only_replay_skips_bridge_cancellation_only_without_provider():
    assert process_launcher._requires_bridge_cancellation(
        {
            "execution_mode": "validation_only_replay",
            "provider_launched": False,
            "adapter_id": "deepseek_vscode_lm",
        }
    ) is False
    assert process_launcher._requires_bridge_cancellation(
        {
            "execution_mode": "validation_only_replay",
            "provider_launched": True,
            "adapter_id": "deepseek_vscode_lm",
        }
    ) is True
    assert process_launcher._requires_bridge_cancellation(
        {
            "execution_mode": "provider_worker",
            "provider_launched": False,
            "adapter_id": "deepseek_vscode_lm",
        }
    ) is True


def test_finalize_isolated_request_validation_only_replay_authorization(
    monkeypatch, tmp_path
):
    """NF50 Phase B regression at the real worker finalization callsite
    (``_finalize_isolated_request``'s ``validate_required_outputs`` call):
    an unchanged *inherited* predecessor required output only reaches
    review when the immutable ``metadata`` snapshot carries a Phase A
    ``validation_only_replay_authorization`` whose exact task, coordinator
    actor, rework predecessor request, claim epoch, and pinned raw SHA-256
    all match this exact episode. Without it, the ordinary
    ``required_output_unchanged`` failure still applies.
    """
    if os.name == "nt":
        pytest.skip("review finalization requires the POSIX secure sandbox backend")
    _open_gates(monkeypatch)
    from aiworkhub import worker_workspace

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "out").mkdir(parents=True)
    canonical_file = repo / "out" / "result.json"
    canonical_file.write_text("canonical-v1", encoding="utf-8")

    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "out").mkdir(parents=True)
    worked_file = workspace_dir / "out" / "result.json"
    # Byte-identical to canonical: nothing changed within this episode, and
    # the inherited predecessor content itself matches canonical too, so
    # this is genuinely a validation-only replay, not a real delta.
    worked_file.write_text("canonical-v1", encoding="utf-8")
    baseline_hash = worker_workspace._hash_path(worked_file)
    raw_sha256 = hashlib.sha256(worked_file.read_bytes()).hexdigest()

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

    home_dir = tmp_path / "home"
    home_dir.mkdir()

    workspace = worker_workspace.WorkerWorkspace(
        request_id="req-replay-1",
        repo=repo,
        path=workspace_dir,
        home=home_dir,
        allowed_writes=("out/result.json",),
        parent_baseline={"out/result.json": baseline_hash},
        workspace_baseline={"out/result.json": baseline_hash},
        inherited_rework_paths=("out/result.json",),
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
    monkeypatch.setattr(
        process_launcher, "promote", lambda *a, **k: [], raising=False
    )
    monkeypatch.setattr(
        process_launcher.core, "mark_review", lambda *a, **k: {"ok": True}
    )

    review_calls = []

    def fake_review_terminal_exact(metadata_arg, substatus, *, request_id, error="", evidence=None):
        review_calls.append(
            {
                "request_id": request_id,
                "task_id": metadata_arg["task_id"],
                "substatus": substatus,
                "evidence": evidence or {},
            }
        )
        return {"ok": True, "returncode": 0, "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(manager, "_review_terminal_exact", fake_review_terminal_exact)

    def _run(request_id: str, authorization: dict | None):
        stdout_path = tmp_path / f"{request_id}.stdout.log"
        stderr_path = tmp_path / f"{request_id}.stderr.log"
        stdout_path.write_text("worker complete\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        status_path = tmp_path / f"{request_id}.supervisor.json"
        metadata_path = tmp_path / f"{request_id}.request.json"
        worker_workspace.write_json_0600(
            status_path, {"state": "exited", "exit_code": 0}
        )
        metadata = {
            "schema_id": "aiworkhub.task_mcp.isolated_request.v1",
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "adapter_id": "claude_cli",
            "model": "claude_cli",
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "supervisor_status_path": str(status_path),
            "cancel_path": str(tmp_path / f"{request_id}.cancel.json"),
            "prompt_sha256": "0" * 64,
            "project_context": None,
            "project_context_delivery": {"injected": False},
            "sandbox_backend": "landlock",
            "validation": [],
            "required_outputs": ["out/result.json"],
            "allow_empty_required_outputs": [],
            "allow_unchanged_required_outputs": [],
            "external_readonly_dirs": [],
            "workspace": workspace.as_metadata(),
            "claim_epoch": 3,
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "predecessor-1",
                "changed_path_hashes": {"out/result.json": raw_sha256},
            },
            "validation_only_replay_authorization": authorization,
        }
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        manager._append_event({
            "request_id": request_id,
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
        return manager._finalize_isolated_request(request_id, supervisor_returncode=0)

    # Missing authorization: the ordinary required_output_unchanged failure
    # still applies (this must never silently pass).
    unauthorized_event = _run("req-replay-unauthorized", None)
    assert unauthorized_event["state"] == "validation_failed"
    assert '"unchanged_mandatory_outputs":["out/result.json"]' in unauthorized_event[
        "error"
    ]
    assert unauthorized_event["attempt_artifact_manifest"]["verified"] is True
    assert process_launcher.attempt_artifacts.verify_json_bundle(
        Path(unauthorized_event["attempt_artifact_manifest"]["manifest_path"]).parent
    )["attempt_id"] == "req-replay-unauthorized"

    # A wrong claim epoch (stale/replayed episode) fails closed the same way.
    stale_authorization = {
        "task_id": "TASK_B1",
        "actor": process_launcher.core.CODEX_RUNNER,
        "predecessor_request_id": "predecessor-1",
        "changed_path_hashes": {"out/result.json": raw_sha256},
        "authorized_at": "2026-08-07T00:00:00+00:00",
        "next_claim_epoch": 99,
        "one_episode_binding": True,
    }
    stale_event = _run("req-replay-stale-epoch", stale_authorization)
    assert stale_event["state"] == "validation_failed"
    assert '"unchanged_mandatory_outputs":["out/result.json"]' in stale_event["error"]

    # Exact matching authorization: reaches review with structured replay
    # evidence attached, and manager acceptance/promotion gates untouched.
    authorization = {**stale_authorization, "next_claim_epoch": 3}
    authorized_event = _run("req-replay-authorized", authorization)
    assert authorized_event["state"] == "review_ready"
    assert authorized_event["changed_paths"] == []

    call = next(c for c in review_calls if c["request_id"] == "req-replay-authorized")
    assert call["substatus"] == "review_ready"
    evidence = call["evidence"]
    record = evidence["required_outputs"][0]
    assert record["unchanged_allowed"] is True
    assert record["replay_evidence"]["sha256"] == raw_sha256
    assert record["replay_evidence"]["claim_epoch"] == 3
    assert evidence["validation_only_replay"] == [record["replay_evidence"]]


def test_validation_only_replay_authorization_fails_closed_before_launch():
    digest = "a" * 64
    card = {
        "task_id": "TASK_REPLAY",
        "claim_epoch": 4,
        "required_outputs": ["out/result.json"],
        "validation": ["python3 -m py_compile out/result.json"],
        "rework_predecessor": {
            "request_id": "predecessor-4",
            "changed_path_hashes": {"out/result.json": digest},
        },
        "validation_only_replay_authorization": {
            "task_id": "TASK_REPLAY",
            "actor": process_launcher.core.CODEX_RUNNER,
            "predecessor_request_id": "predecessor-4",
            "changed_path_hashes": {"out/result.json": digest},
            "next_claim_epoch": 4,
            "one_episode_binding": True,
        },
    }
    exact = process_launcher._validation_only_replay_authorization(
        card, "TASK_REPLAY"
    )
    assert exact is not card["validation_only_replay_authorization"]
    assert exact["changed_path_hashes"] == {"out/result.json": digest}

    no_validation = json.loads(json.dumps(card))
    no_validation["validation"] = []
    exact_no_validation = process_launcher._validation_only_replay_authorization(
        no_validation, "TASK_REPLAY"
    )
    assert exact_no_validation["changed_path_hashes"] == {
        "out/result.json": digest
    }

    no_required_outputs = json.loads(json.dumps(card))
    no_required_outputs["required_outputs"] = []
    exact_no_required_outputs = (
        process_launcher._validation_only_replay_authorization(
            no_required_outputs, "TASK_REPLAY"
        )
    )
    assert exact_no_required_outputs["changed_path_hashes"] == {
        "out/result.json": digest
    }

    stale = json.loads(json.dumps(card))
    stale["validation_only_replay_authorization"]["next_claim_epoch"] = 3
    with pytest.raises(
        process_launcher.LaunchRejected,
        match="validation_only_replay_claim_epoch_mismatch",
    ):
        process_launcher._validation_only_replay_authorization(
            stale, "TASK_REPLAY"
        )

    forged = json.loads(json.dumps(card))
    forged["validation_only_replay_authorization"]["actor"] = "worker"
    with pytest.raises(
        process_launcher.LaunchRejected,
        match="validation_only_replay_actor_mismatch",
    ):
        process_launcher._validation_only_replay_authorization(
            forged, "TASK_REPLAY"
        )


def test_validation_only_replay_code_task_requires_satisfied_exact_predecessor_mcp_gate(
    monkeypatch, tmp_path
):
    card = {
        "task_id": "TASK_REPLAY",
        "project_context": {"task_type": "code", "required": True},
    }
    authorization = {
        "predecessor_request_id": "predecessor-7",
        "changed_path_hashes": {"out/result.py": "b" * 64},
        "next_claim_epoch": 7,
    }
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(task_id="TASK_REPLAY")),
        argv=[sys.executable, "-c", "pass"],
    )
    monkeypatch.setattr(
        process_launcher,
        "create_workspace",
        lambda *a, **k: pytest.fail("workspace must not be created"),
    )

    with pytest.raises(
        process_launcher.LaunchRejected,
        match="validation_only_replay_predecessor_terminal_event_missing",
    ):
        manager._launch_validation_only_replay(
            task_id="TASK_REPLAY",
            runner="claude_worker_b1",
            topic="task_mcp",
            adapter_id="claude_cli",
            model=None,
            timeout_seconds=30,
            card=card,
            authorization=authorization,
        )

    manager._append_event({
        "request_id": "predecessor-7",
        "task_id": "TASK_REPLAY",
        "state": "validation_failed",
        "worker_mcp_gate": {
            "gated": True,
            "satisfied": False,
            "required_tools": ["source_graph", "session_current_state"],
            "reason": "worker_mcp_required_tools_missing:session_current_state",
            "verification": {"ok": True},
        },
    })
    with pytest.raises(
        process_launcher.LaunchRejected,
        match="validation_only_replay_predecessor_worker_mcp_gate_unsatisfied",
    ):
        manager._validation_replay_predecessor_mcp_receipt(
            card, authorization, "TASK_REPLAY"
        )


def test_validation_only_replay_inherits_authenticated_predecessor_mcp_truth(tmp_path):
    card = {
        "task_id": "TASK_REPLAY",
        "project_context": {"task_type": "code", "required": True},
    }
    authorization = {
        "predecessor_request_id": "predecessor-green",
        "changed_path_hashes": {"out/result.py": "c" * 64},
        "next_claim_epoch": 8,
    }
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(task_id="TASK_REPLAY")),
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event({
        "request_id": "predecessor-green",
        "task_id": "TASK_REPLAY",
        "state": "review_ready",
        "worker_mcp_gate": {
            "gated": True,
            "task_type": "code",
            "project_context_required": True,
            "required_tools": [
                "source_graph",
                "session_current_state",
                "ai_memory",
                "kb",
            ],
            "satisfied": True,
            "verification": {"ok": True, "verified_entries": 4},
        },
    })
    receipt = manager._validation_replay_predecessor_mcp_receipt(
        card, authorization, "TASK_REPLAY"
    )
    metadata = {
        "execution_mode": "validation_only_replay",
        "request_id": "replay-8",
        "task_id": "TASK_REPLAY",
        "claim_epoch": 8,
        "rework_predecessor": {
            "request_id": "predecessor-green",
            "changed_path_hashes": {"out/result.py": "c" * 64},
        },
        "validation_only_replay_authorization": authorization,
        "worker_mcp": {"inherited_predecessor_gate": receipt},
    }

    gate = process_launcher._worker_mcp_live_call_gate(metadata, "replay-8")
    assert gate["satisfied"] is True
    assert gate["required_tools"] == [
        "source_graph",
        "session_current_state",
        "ai_memory",
        "kb",
    ]
    assert gate["fresh_current_request_worker_calls"] is False
    assert set(gate["satisfaction_by_tool"].values()) == {
        "authenticated_predecessor_gate"
    }

    metadata["rework_predecessor"]["changed_path_hashes"] = {
        "out/result.py": "d" * 64
    }
    assert process_launcher._worker_mcp_live_call_gate(metadata, "replay-8")[
        "satisfied"
    ] is False


def test_isolated_validation_only_replay_never_resolves_or_starts_provider(
    monkeypatch, tmp_path
):
    _open_gates(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace_dir = tmp_path / "workspace"
    home_dir = tmp_path / "home"
    workspace_dir.mkdir()
    home_dir.mkdir()
    digest = "b" * 64
    card = {
        **_card(task_id="TASK_REPLAY"),
        "claim_epoch": 7,
        "validation": ["python3 -m py_compile out/result.py"],
        "required_outputs": ["out/result.py"],
        "rework_predecessor": {
            "request_id": "predecessor-7",
            "changed_path_hashes": {"out/result.py": digest},
        },
        "validation_only_replay_authorization": {
            "task_id": "TASK_REPLAY",
            "actor": process_launcher.core.CODEX_RUNNER,
            "predecessor_request_id": "predecessor-7",
            "changed_path_hashes": {"out/result.py": digest},
            "next_claim_epoch": 7,
            "one_episode_binding": True,
        },
    }
    workspace = process_launcher.WorkerWorkspace(
        request_id="placeholder",
        repo=repo,
        path=workspace_dir,
        home=home_dir,
        allowed_writes=("out/result.py",),
        parent_baseline={"out/result.py": None},
        workspace_baseline={"out/result.py": digest},
        inherited_rework_paths=("out/result.py",),
    )
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(lambda: card),
        collision_guard=_collision,
        adapter_builder=lambda **_: (_ for _ in ()).throw(
            AssertionError("provider adapter plan must not be built")
        ),
    )
    monkeypatch.setattr(manager, "_preflight_card", lambda *a, **k: dict(card))
    monkeypatch.setattr(
        manager,
        "_resolve_provider_env",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("provider credentials must not be resolved")
        ),
    )
    monkeypatch.setattr(
        manager,
        "_popen",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("provider/supervisor process must not start")
        ),
    )
    monkeypatch.setattr(
        process_launcher,
        "create_workspace",
        lambda repo_arg, request_id, card_arg, adapter_id: process_launcher.replace(
            workspace, request_id=request_id
        ),
    )
    monkeypatch.setattr(
        process_launcher,
        "build_residual_contract_manifest",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        process_launcher.task_engine,
        "claim_start_exact",
        lambda *a, **k: {
            "ok": True,
            "returncode": 0,
            "stdout": json.dumps(
                {
                    **card,
                    "task_id": a[1],
                    "runner": a[2],
                    "topic": a[3],
                    "launch_request_id": k["request_id"],
                    "claim_epoch": 7,
                }
            ),
        },
    )
    finalized = []
    monkeypatch.setattr(
        manager,
        "_finalize_isolated_request",
        lambda request_id, supervisor_returncode=None: finalized.append(
            (request_id, supervisor_returncode)
        ),
    )

    result = manager.launch(
        task_id="TASK_REPLAY",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        model="claude-sonnet-5",
        timeout_seconds=30,
    )

    assert result["ok"] is True
    assert result["state"] == "running"
    assert result["terminal"] is False
    assert result["execution_mode"] == "validation_only_replay"
    assert result["provider_launched"] is False
    assert result["pid"] is None
    launch_event = next(
        event
        for event in manager._events()
        if event.get("request_id") == result["request_id"]
        and event.get("state") == "running"
    )
    assert launch_event["execution_mode"] == "validation_only_replay"
    assert launch_event["provider_launched"] is False
    assert not list((tmp_path / "processes").glob("*.supervisor-spec.json"))
    deadline = time.monotonic() + 2
    while not finalized and time.monotonic() < deadline:
        time.sleep(0.01)
    assert finalized == [(result["request_id"], 0)]


def test_provider_free_replay_usage_is_labeled_without_fabricated_observation(
    monkeypatch, tmp_path
):
    output = tmp_path / "empty-provider-output.jsonl"
    output.write_text("", encoding="utf-8")
    card = _card()
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: card),
        argv=[sys.executable, "-c", "pass"],
    )
    monkeypatch.setattr(
        process_launcher.core,
        "run_taskctl",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("usage recording must not use taskctl")
        ),
    )
    usage, recorded, error = manager._record_usage(
        "request-replay",
        card["task_id"],
        card["runner"],
        "claude_cli",
        "claude-sonnet-5",
        output,
        topic=card["topic"],
        execution_mode="validation_only_replay",
    )

    assert recorded is False
    assert error == "claim_authority_unavailable"
    assert usage["provider_launched"] is False
    assert usage["usage_observed"] is False
    assert usage["telemetry_reason"] == "provider_not_invoked_deterministic_replay"


def test_append_live_usage_event_requires_exact_current_claim(tmp_path):
    request_id = "b" * 32
    repo = _canonical_claimed_task_repo(tmp_path, request_id=request_id, claim_epoch=7)
    payload = {
        "model": "claude-sonnet-5",
        "requested_model": "claude-sonnet-5",
        "observed_model": "",
        "role": "worker",
        "provider": "claude",
        "input_tokens": 11,
        "output_tokens": 5,
        "total_tokens": 16,
        "visible_output_tokens": 5,
        "reasoning_output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "telemetry_reason": "",
        "cost_usd": 0.0,
        "usage_observed": True,
        "model_observed": False,
        "cache_metrics_observed": False,
        "cost_observed": False,
    }

    assert task_store.append_live_usage_event(
        repo,
        "TASK_USAGE",
        "claude_worker_b1",
        request_id=request_id,
        claimed_by="claude_worker_b1",
        claim_epoch=7,
        payload=payload,
    ) == (True, "recorded")
    assert task_store.append_live_usage_event(
        repo,
        "TASK_USAGE",
        "claude_worker_b1",
        request_id=request_id,
        claimed_by="claude_worker_b1",
        claim_epoch=7,
        payload=payload,
    ) == (True, "already_recorded")

    events = task_store.list_usage_events(repo)
    assert len(events) == 1
    assert events[0]["source"] == "task_mcp_launcher"
    assert events[0]["note"] == f"task_mcp_request:{request_id}"
    assert events[0]["request_id"] == request_id
    assert events[0]["claim_epoch"] == 7
    assert events[0]["role"] == "worker"
    assert events[0]["model"] == "claude-sonnet-5"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"claim_epoch": True}, "usage_live_claim_epoch_invalid"),
        ({"claim_epoch": 6}, "claim_epoch_mismatch"),
        ({"claimed_by": "other"}, "claimed_by_mismatch"),
        ({"runner": "other"}, "runner_mismatch"),
        ({"request_id": "c" * 32}, "request_id_mismatch"),
        (
            {"payload": {"request_id": "g" * 32}},
            "usage_live_request_spoof",
        ),
        ({"payload": {"source": "terminal_log_backfill"}}, "usage_live_source_spoof"),
        ({"payload": {"note": "task_mcp_request:" + "d" * 32}}, "usage_live_note_spoof"),
        # `review` and the other states one claim reaches after its attempt ran
        # are now accountable -- refusing them discarded the cost of every
        # worker that succeeded. A CLOSED record still refuses: those are not
        # states this claim can be in, they are records a coordinator finished
        # with. See tests/test_spend_is_recorded_for_work_that_succeeded.py.
        (
            {"sql": "UPDATE tasks SET archived_at='2026-09-02T00:00:00+00:00'"},
            "lifecycle_mismatch:archived",
        ),
        (
            {"sql": "UPDATE tasks SET status='superseded', worker_status='superseded'"},
            "lifecycle_mismatch:superseded",
        ),
    ],
)
def test_append_live_usage_event_fails_closed_on_authority_mismatch(
    tmp_path, mutation, expected
):
    request_id = "e" * 32
    repo = _canonical_claimed_task_repo(tmp_path, request_id=request_id, claim_epoch=7)
    if mutation.get("sql"):
        conn = task_store._connect(task_store.canonical_db_path(repo))
        try:
            conn.execute(str(mutation["sql"]))
            conn.commit()
        finally:
            conn.close()
    payload = {"model": "claude-sonnet-5", **mutation.get("payload", {})}

    recorded, reason = task_store.append_live_usage_event(
        repo,
        "TASK_USAGE",
        str(mutation.get("runner", "claude_worker_b1")),
        request_id=str(mutation.get("request_id", request_id)),
        claimed_by=str(mutation.get("claimed_by", "claude_worker_b1")),
        claim_epoch=mutation.get("claim_epoch", 7),
        payload=payload,
    )

    assert recorded is False
    assert reason == expected
    assert task_store.list_usage_events(repo) == []


def test_record_usage_direct_harness_has_no_authoritative_live_ledger_call(
    monkeypatch, tmp_path
):
    output = tmp_path / "usage.jsonl"
    output.write_text("", encoding="utf-8")
    card = _card()
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: card),
        argv=[sys.executable, "-c", "pass"],
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("direct harness must not append live usage")

    monkeypatch.setattr(task_store, "append_live_usage_event", forbidden)
    usage, recorded, error = manager._record_usage(
        "request-direct",
        card["task_id"],
        card["runner"],
        "claude_cli",
        "claude-sonnet-5",
        output,
        topic=card["topic"],
    )

    assert recorded is False
    assert error == "claim_authority_unavailable"
    assert usage["provider_launched"] is True
    assert usage["role"] == "worker"


def _spoofed_usage_record_card(
    request_id: str,
    *,
    task_id: str = "TASK_USAGE",
    runner: str = "claude_worker_b1",
) -> dict:
    card = _card(task_id, "processing")
    card.update({
        "worker_status": "claimed",
        "claimed_by": runner,
        "launch_request_id": request_id,
        "usage_records": [
            {
                "source": "task_mcp_launcher",
                "note": f"task_mcp_request:{request_id}",
            }
        ],
    })
    return card


@pytest.mark.parametrize(
    ("claim_authority", "expected_error"),
    [
        (None, "claim_authority_unavailable"),
        (
            {
                "request_id": "wrong-request",
                "claimed_by": "claude_worker_b1",
                "claim_epoch": 9,
            },
            "claim_authority_request_mismatch",
        ),
        (
            {
                "request_id": "f" * 32,
                "claimed_by": "other-worker",
                "claim_epoch": 9,
            },
            "claim_authority_claimed_by_mismatch",
        ),
        (
            {
                "request_id": "f" * 32,
                "claimed_by": "claude_worker_b1",
                "claim_epoch": 0,
            },
            "claim_authority_claim_epoch_invalid",
        ),
    ],
)
def test_record_usage_ignores_spoofed_card_usage_records_before_claim_authority(
    monkeypatch, tmp_path, claim_authority, expected_error
):
    output = tmp_path / "usage.jsonl"
    output.write_text("", encoding="utf-8")
    request_id = "f" * 32
    card = _spoofed_usage_record_card(request_id)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: card),
        argv=[sys.executable, "-c", "pass"],
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid claim authority must not reach live usage store")

    monkeypatch.setattr(task_store, "append_live_usage_event", forbidden)
    usage, recorded, error = manager._record_usage(
        request_id,
        card["task_id"],
        card["runner"],
        "claude_cli",
        "claude-sonnet-5",
        output,
        topic=card["topic"],
        claim_authority=claim_authority,
    )

    assert recorded is False
    assert error == expected_error
    assert usage["role"] == "worker"


def test_record_usage_forwards_exact_isolated_claim_epoch(monkeypatch, tmp_path):
    output = tmp_path / "usage.jsonl"
    output.write_text("", encoding="utf-8")
    card = _card("TASK_USAGE", "processing")
    card.update({
        "worker_status": "claimed",
        "claimed_by": card["runner"],
        "launch_request_id": "f" * 32,
        "claim_epoch": 9,
        "usage_records": [
            {
                "source": "task_mcp_launcher",
                "note": f"task_mcp_request:{'f' * 32}",
            }
        ],
    })
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: card),
        argv=[sys.executable, "-c", "pass"],
    )
    captured = {}

    def fake_append(repo, task_id, runner, **kwargs):
        captured.update({"repo": repo, "task_id": task_id, "runner": runner, **kwargs})
        return True, "recorded"

    monkeypatch.setattr(task_store, "append_live_usage_event", fake_append)
    usage, recorded, error = manager._record_usage(
        "f" * 32,
        "TASK_USAGE",
        card["runner"],
        "claude_cli",
        "claude-sonnet-5",
        output,
        topic=card["topic"],
        claim_authority={
            "request_id": "f" * 32,
            "claimed_by": card["runner"],
            "claim_epoch": 9,
        },
    )

    assert recorded is True
    assert error == ""
    assert usage["role"] == "worker"
    assert captured["request_id"] == "f" * 32
    assert captured["claimed_by"] == card["runner"]
    assert captured["claim_epoch"] == 9
    assert captured["payload"]["role"] == "worker"
    assert captured["payload"]["model"] == "claude-sonnet-5"


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


def test_vscode_lm_usage_records_explicit_provider_api_unavailability(
    tmp_path, monkeypatch,
):
    output = tmp_path / "vscode-lm-result.jsonl"
    output.write_text(
        json.dumps({
            "type": "result",
            "subtype": "success",
            "model": {"id": "glm-5.2"},
            "result": "completed without provider usage metadata",
        }) + "\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_append(repo, task_id, runner, **kwargs):
        captured.update({"repo": repo, "task_id": task_id, "runner": runner, **kwargs})
        return True, "recorded"

    monkeypatch.setattr(task_store, "append_live_usage_event", fake_append)
    manager = process_launcher.ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        isolation_enabled=False,
    )

    usage, recorded, error = manager._record_usage(
        "a" * 32,
        "TASK_USAGE",
        "glm_worker",
        "glm_vscode_lm",
        "glm-5.2",
        output,
        topic="code",
        claim_authority={
            "request_id": "a" * 32,
            "claimed_by": "glm_worker",
            "claim_epoch": 1,
        },
    )

    assert recorded is True
    assert error == ""
    assert usage["usage_observed"] is False
    assert usage["telemetry_reason"] == "provider_api_usage_unavailable"
    assert captured["payload"]["telemetry_reason"] == "provider_api_usage_unavailable"
    assert captured["payload"]["provider"] == "glm_vscode_lm"


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


def test_quality_review_card_is_readonly_and_quality_review_topic():
    quality_card = {
        "topic": "quality_review",
        "project_context": {"task_type": "research"},
        "read_only": True,
        "allowed_writes": [],
        "required_outputs": [],
    }
    assert process_launcher._card_is_readonly_quality_review(quality_card) is True

    impl_card = {
        "topic": "task_mcp",
        "project_context": {"task_type": "code"},
        "read_only": False,
        "allowed_writes": ["out.txt"],
        "required_outputs": ["out.txt"],
    }
    assert process_launcher._card_is_readonly_quality_review(impl_card) is False


def test_quality_review_card_identification_rejects_mutation():
    card = {
        "topic": "quality_review",
        "project_context": {"task_type": "research"},
        "read_only": False,
        "allowed_writes": ["some_file"],
        "required_outputs": [],
    }
    assert process_launcher._card_is_readonly_quality_review(card) is False


def _w1_pid_evidence(
    verdict: process_launcher.PidIdentityVerdict,
) -> process_launcher.PidIdentityEvidence:
    return process_launcher.PidIdentityEvidence(
        verdict=verdict,
        pid=123,
        expected_start_ticks=456,
        observed_start_ticks=(
            456 if verdict is process_launcher.PidIdentityVerdict.MATCH else None
        ),
        attempts=1,
        operation="test",
    )


def test_status_pid_identity_unknown_defers_without_mutation_then_mismatch_finalizes(
    monkeypatch,
    tmp_path,
):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "status-pid-identity"
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": "running",
        "pid": 123,
        "pid_start_ticks": 456,
        "metadata_path": str(tmp_path / "request.json"),
    })
    verdict = {"value": process_launcher.PidIdentityVerdict.UNKNOWN}
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _w1_pid_evidence(verdict["value"]),
    )
    monkeypatch.setattr(process_launcher, "_pid_matches", lambda *_args: False)
    finalizer_calls = []

    def finalize(request_id_arg, *, lock_blocking=True):
        finalizer_calls.append((request_id_arg, lock_blocking))
        manager._append_event({
            "request_id": request_id_arg,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "state": "worker_failed",
        })

    monkeypatch.setattr(manager, "_finalize_after_process_exit", finalize)
    before = manager._request_events(request_id)

    unknown = manager.status(request_id)

    assert unknown["state"] == "running"
    assert unknown["latest_event"]["reconciliation_deferred"] == "pid_identity_unknown"
    assert manager._request_events(request_id) == before
    assert finalizer_calls == []

    verdict["value"] = process_launcher.PidIdentityVerdict.MATCH
    assert manager.status(request_id)["state"] == "running"
    assert finalizer_calls == []

    verdict["value"] = process_launcher.PidIdentityVerdict.MISMATCH
    assert manager.status(request_id)["state"] == "worker_failed"
    assert finalizer_calls == [(request_id, False)]
    assert manager.status(request_id)["state"] == "worker_failed"
    assert finalizer_calls == [(request_id, False)]


def test_cancel_pid_identity_tri_state_and_bridge_completion_order(
    monkeypatch,
    tmp_path,
):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    verdicts = {}
    ordering = []
    bridge_results = {}
    signals = []
    finalizer_calls = []

    def seed(request_id):
        metadata_path = tmp_path / f"{request_id}.json"
        cancel_path = tmp_path / f"{request_id}.cancel.json"
        metadata_path.write_text(
            json.dumps({"cancel_path": str(cancel_path)}), encoding="utf-8"
        )
        manager._append_event({
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "adapter_id": "claude_cli",
            "state": "running",
            "pid": 123,
            "pid_start_ticks": 456,
            "metadata_path": str(metadata_path),
        })
        return cancel_path

    def bridge(request_id, _live):
        ordering.append((request_id, "bridge"))
        return bridge_results.get(request_id, "")

    def identity(_pid, _ticks):
        request_id = ordering[-1][0]
        ordering.append((request_id, "identity"))
        return _w1_pid_evidence(verdicts[request_id])

    def finalize(request_id):
        ordering.append((request_id, "finalize"))
        finalizer_calls.append(request_id)
        manager._append_event({
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "state": "worker_failed",
        })

    monkeypatch.setattr(
        manager, "_publish_bridge_cancellation_before_finalization", bridge
    )
    monkeypatch.setattr(process_launcher, "_pid_identity_evidence", identity)
    monkeypatch.setattr(manager, "_finalize_after_process_exit", finalize)
    monkeypatch.setattr(
        process_launcher.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    unknown_id = "cancel-unknown"
    unknown_cancel_path = seed(unknown_id)
    verdicts[unknown_id] = process_launcher.PidIdentityVerdict.UNKNOWN
    before = manager._request_events(unknown_id)
    unknown = manager.cancel(unknown_id)
    assert unknown["blocked_reason"] == "pid_identity_unknown"
    assert manager._request_events(unknown_id) == before
    assert not unknown_cancel_path.exists()
    assert signals == []
    assert finalizer_calls == []
    assert ordering == [(unknown_id, "bridge"), (unknown_id, "identity")]

    completed_id = "cancel-completed"
    completed_path = seed(completed_id)
    bridge_results[completed_id] = "completed"
    completed = manager.cancel(completed_id)
    assert completed["completion_won"] is True
    assert not completed_path.exists()
    assert (completed_id, "identity") not in ordering

    match_id = "cancel-match"
    match_path = seed(match_id)
    verdicts[match_id] = process_launcher.PidIdentityVerdict.MATCH
    matched = manager.cancel(match_id)
    assert matched["state"] == "cancel_requested"
    assert json.loads(match_path.read_text(encoding="utf-8"))["request_id"] == match_id
    assert signals == [(123, signal.SIGTERM)]
    assert ordering[-2:] == [(match_id, "bridge"), (match_id, "identity")]

    mismatch_id = "cancel-mismatch"
    mismatch_path = seed(mismatch_id)
    verdicts[mismatch_id] = process_launcher.PidIdentityVerdict.MISMATCH
    mismatched = manager.cancel(mismatch_id)
    assert mismatched["state"] == "worker_failed"
    assert not mismatch_path.exists()
    assert finalizer_calls == [mismatch_id]
    assert ordering[-3:] == [
        (mismatch_id, "bridge"),
        (mismatch_id, "identity"),
        (mismatch_id, "finalize"),
    ]


_W2_ADMISSION_CASES = [
    pytest.param(process_launcher.PidIdentityVerdict.MATCH, 456, True, id="match"),
    pytest.param(process_launcher.PidIdentityVerdict.UNKNOWN, 456, True, id="unknown"),
    pytest.param(process_launcher.PidIdentityVerdict.MISMATCH, 456, False, id="mismatch"),
    pytest.param(process_launcher.PidIdentityVerdict.UNKNOWN, None, True, id="missing-ticks"),
    pytest.param(
        process_launcher.PidIdentityVerdict.UNKNOWN,
        "malformed-ticks",
        True,
        id="malformed-ticks",
    ),
]


def _seed_w2_persisted_request(
    manager,
    *,
    request_id,
    task_id,
    ticks,
    state="running",
    pid=123,
):
    manager._append_event({
        "request_id": request_id,
        "task_id": task_id,
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": state,
        "pid": pid,
        "pid_start_ticks": ticks,
        "metadata_path": "persisted-request.json",
    })


def _forbid_w2_admission_side_effects(monkeypatch, manager):
    calls = []

    def forbidden(name):
        def record(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"admission triggered forbidden side effect: {name}")

        return record

    monkeypatch.setattr(
        manager,
        "_finalize_after_process_exit",
        forbidden("terminal_event"),
    )
    monkeypatch.setattr(manager, "_release_exact", forbidden("release"))
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_terminal_failure",
        forbidden("callback"),
    )
    monkeypatch.setattr(
        process_launcher,
        "cleanup_workspace",
        forbidden("gc"),
    )
    return calls


@pytest.mark.parametrize("verdict,ticks,expected_active", _W2_ADMISSION_CASES)
def test_active_request_ids_pid_identity_admission_matrix(
    monkeypatch,
    tmp_path,
    verdict,
    ticks,
    expected_active,
):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "w2-capacity-persisted"
    _seed_w2_persisted_request(
        manager,
        request_id=request_id,
        task_id="OTHER_TASK_B1",
        ticks=ticks,
    )
    before = manager._request_events(request_id)
    identity_calls = []

    def identity(pid, expected_ticks):
        identity_calls.append((pid, expected_ticks))
        return _w1_pid_evidence(verdict)

    monkeypatch.setattr(process_launcher, "_pid_identity_evidence", identity)
    monkeypatch.setattr(
        process_launcher,
        "_pid_matches",
        lambda *_args: pytest.fail("admission must not use reporting-only _pid_matches"),
    )
    side_effects = _forbid_w2_admission_side_effects(monkeypatch, manager)

    active = manager._active_request_ids()

    assert (request_id in active) is expected_active
    assert manager._active_count() == int(expected_active)
    assert identity_calls == [(123, ticks), (123, ticks)]
    assert manager._request_events(request_id) == before
    assert side_effects == []


@pytest.mark.parametrize("verdict,ticks,expected_blocked", _W2_ADMISSION_CASES)
def test_assert_no_duplicate_task_pid_identity_admission_matrix(
    monkeypatch,
    tmp_path,
    verdict,
    ticks,
    expected_blocked,
):
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="processing")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "w2-assert-duplicate"
    _seed_w2_persisted_request(
        manager,
        request_id=request_id,
        task_id="TASK_B1",
        ticks=ticks,
    )
    before = manager._request_events(request_id)
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _w1_pid_evidence(verdict),
    )
    monkeypatch.setattr(
        process_launcher,
        "_pid_matches",
        lambda *_args: pytest.fail("admission must not use reporting-only _pid_matches"),
    )
    side_effects = _forbid_w2_admission_side_effects(monkeypatch, manager)

    if expected_blocked:
        with pytest.raises(
            process_launcher.LaunchRejected,
            match=f"duplicate_persisted_task:{request_id}",
        ):
            manager._assert_no_duplicate_task("TASK_B1")
    else:
        manager._assert_no_duplicate_task("TASK_B1")

    assert manager._request_events(request_id) == before
    assert side_effects == []


@pytest.mark.parametrize("verdict,ticks,expected_blocked", _W2_ADMISSION_CASES)
def test_direct_launch_duplicate_pid_identity_admission_matrix(
    monkeypatch,
    tmp_path,
    verdict,
    ticks,
    expected_blocked,
):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "w2-direct-duplicate"
    _seed_w2_persisted_request(
        manager,
        request_id=request_id,
        task_id="TASK_B1",
        ticks=ticks,
    )
    before = manager._request_events(request_id)
    monkeypatch.setattr(manager, "_active_count", lambda: 0)
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _w1_pid_evidence(verdict),
    )
    monkeypatch.setattr(
        process_launcher,
        "_pid_matches",
        lambda *_args: pytest.fail("admission must not use reporting-only _pid_matches"),
    )
    side_effects = (
        _forbid_w2_admission_side_effects(monkeypatch, manager)
        if expected_blocked
        else []
    )

    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )

    assert result["ok"] is not expected_blocked
    if expected_blocked:
        assert result["blocked_reason"] == f"duplicate_persisted_task:{request_id}"
    else:
        _wait_terminal(manager, result["request_id"])
    assert manager._request_events(request_id) == before
    assert side_effects == []


@pytest.mark.parametrize(
    "verdict,expected_blocked",
    [
        pytest.param(process_launcher.PidIdentityVerdict.MATCH, True, id="match"),
        pytest.param(
            process_launcher.PidIdentityVerdict.UNKNOWN,
            True,
            id="unknown",
        ),
        pytest.param(
            process_launcher.PidIdentityVerdict.MISMATCH,
            False,
            id="mismatch",
        ),
    ],
)
def test_direct_launch_cancel_requested_pid_identity_admission_matrix(
    monkeypatch,
    tmp_path,
    verdict,
    expected_blocked,
):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "w2-direct-cancel-requested"
    _seed_w2_persisted_request(
        manager,
        request_id=request_id,
        task_id="TASK_B1",
        ticks=456,
        state="cancel_requested",
    )
    before = manager._request_events(request_id)
    monkeypatch.setattr(manager, "_active_count", lambda: 0)
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _w1_pid_evidence(verdict),
    )
    monkeypatch.setattr(
        process_launcher,
        "_pid_matches",
        lambda *_args: pytest.fail("admission must not use reporting-only _pid_matches"),
    )
    side_effects = (
        _forbid_w2_admission_side_effects(monkeypatch, manager)
        if expected_blocked
        else []
    )

    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )

    assert result["ok"] is not expected_blocked
    if expected_blocked:
        assert result["blocked_reason"] == f"duplicate_persisted_task:{request_id}"
    else:
        _wait_terminal(manager, result["request_id"])
    assert manager._request_events(request_id) == before
    assert side_effects == []


def test_missing_pid_is_mismatch_inactive_and_allows_direct_launch(
    monkeypatch,
    tmp_path,
):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "w2-direct-missing-pid"
    _seed_w2_persisted_request(
        manager,
        request_id=request_id,
        task_id="TASK_B1",
        ticks=None,
        state="cancel_requested",
        pid=0,
    )
    before = manager._request_events(request_id)

    identity = process_launcher._pid_identity_evidence(0, None)
    assert identity.verdict is process_launcher.PidIdentityVerdict.MISMATCH
    assert request_id not in manager._active_request_ids()
    manager._assert_no_duplicate_task("TASK_B1")

    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )

    assert result["ok"] is True
    _wait_terminal(manager, result["request_id"])
    assert manager._request_events(request_id) == before


def test_unknown_duplicate_recovery_allows_direct_launch_after_mismatch(
    monkeypatch,
    tmp_path,
):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    persisted_id = "w2-direct-recovery"
    _seed_w2_persisted_request(
        manager,
        request_id=persisted_id,
        task_id="TASK_B1",
        ticks=None,
    )
    before = manager._request_events(persisted_id)
    verdict = {"value": process_launcher.PidIdentityVerdict.UNKNOWN}
    monkeypatch.setattr(manager, "_active_count", lambda: 0)
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda _pid, _ticks: _w1_pid_evidence(verdict["value"]),
    )

    blocked = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert blocked["blocked_reason"] == f"duplicate_persisted_task:{persisted_id}"
    assert manager._request_events(persisted_id) == before

    verdict["value"] = process_launcher.PidIdentityVerdict.MISMATCH
    recovered = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )

    assert recovered["ok"] is True
    _wait_terminal(manager, recovered["request_id"])
    assert manager._request_events(persisted_id) == before


# ---------------------------------------------------------------------------
# NF129: rework_overlay / request_scoped_predecessor live Source Graph gate
# ---------------------------------------------------------------------------

import hmac as _hmac_mod  # noqa: E402


def _sign_entry(entry: dict, key: bytes) -> str:
    """HMAC-sign a ledger entry dict, returning the complete JSON line."""
    canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    digest = _hmac_mod.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    signed = {**entry, "hmac_sha256": digest}
    return json.dumps(signed, ensure_ascii=False, sort_keys=True) + "\n"


_ENTRY_SCHEMA = worker_ai_tools_mcp.AUDIT_ENTRY_SCHEMA_ID


def _rework_entry(**overrides: object) -> dict:
    defaults: dict[str, object] = {
        "schema_id": _ENTRY_SCHEMA,
        "timestamp": "2026-08-11T01:00:00+00:00",
        "task_id": "TASK_NF129",
        "runner": "test_runner",
        "topic": "nf129_topic",
        "request_id": "req-nf129",
        "tool": "source_graph",
        "ok": True,
        "cache_hit": False,
        "hit_count": 3,
        "bytes_returned": 500,
        "violation": "",
        "authority_source": "rework_overlay",
        "authority_state": "request_scoped_predecessor",
        "authority_repo": "/test/repo",
        "provider_call_id": "pci_rework_overlay_1",
        "provenance": "live",
    }
    return {**defaults, **overrides}


def test_rework_overlay_counts_as_live_source_graph_call(tmp_path: Path) -> None:
    """authority_source=rework_overlay + authority_state=request_scoped_predecessor
    must be counted as an authoritative live Source Graph call after HMAC +
    identity checks pass."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    entry = _rework_entry()
    line = _sign_entry(entry, key)

    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(line, encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["entries_verified"] == 1
    assert result["entries_tampered"] == 0
    assert result["live_source_graph_calls"] == 1
    assert result["fresh_source_graph_calls"] == 1
    assert (
        "source_graph:rework_overlay:request_scoped_predecessor:/test/repo"
        in result["authority_index_identity"]
    )


def test_rework_worktree_overlay_counts_as_live_source_graph_call(
    tmp_path: Path,
) -> None:
    """The worker query surface relabels an applied predecessor overlay as
    request_scoped_worktree; that authenticated authority is live too."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    entry = _rework_entry(authority_state="request_scoped_worktree")
    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(_sign_entry(entry, key), encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["entries_verified"] == 1
    assert result["live_source_graph_calls"] == 1
    assert result["fresh_source_graph_calls"] == 1
    assert (
        "source_graph:rework_overlay:request_scoped_worktree:/test/repo"
        in result["authority_index_identity"]
    )


def test_rework_overlay_rejected_with_wrong_hmac(tmp_path: Path) -> None:
    """A rework_overlay entry with a tampered/forged HMAC must be dropped,
    not counted as live."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    entry = _rework_entry()
    line = _sign_entry(entry, key)

    # Forge: sign with a different key
    wrong_key = os.urandom(32)
    forged_line = _sign_entry(entry, wrong_key)

    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(forged_line, encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["entries_verified"] == 0
    assert result["entries_tampered"] == 1
    assert result["live_source_graph_calls"] == 0


def test_rework_overlay_rejected_with_wrong_identity(tmp_path: Path) -> None:
    """A rework_overlay entry with a mismatched task_id/runner/topic/request_id
    must not count as live."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    entry = _rework_entry(task_id="TASK_OTHER")
    line = _sign_entry(entry, key)

    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(line, encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["entries_verified"] == 0
    assert result["live_source_graph_calls"] == 0


def test_rework_overlay_not_live_when_cache_hit(tmp_path: Path) -> None:
    """A cached rework_overlay source_graph call is fresh telemetry but not
    live -- the completion gate requires a non-cached hit."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    entry = _rework_entry(cache_hit=True)
    line = _sign_entry(entry, key)

    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(line, encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["live_source_graph_calls"] == 0
    assert result["fresh_source_graph_calls"] == 0


def test_rework_overlay_zero_hit_is_still_a_live_invocation(tmp_path: Path) -> None:
    """A fresh authenticated zero-hit overlay call is live invocation truth;
    evidence usefulness remains visible in the zero-hit counters."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    entry = _rework_entry(hit_count=0)
    line = _sign_entry(entry, key)

    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(line, encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["live_source_graph_calls"] == 1
    assert result["fresh_source_graph_calls"] == 1
    assert result["source_graph_zero_hit_calls"] == 1


def test_rework_overlay_authority_label_remains_distinct(tmp_path: Path) -> None:
    """rework_overlay must appear under its own authority label in the index,
    never conflated with canonical or candidate_overlay."""
    key = os.urandom(32)
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(key)

    rework = _rework_entry()
    canonical = _rework_entry(
        authority_source="canonical",
        authority_state="sole_authority",
    )
    candidate = _rework_entry(
        authority_source="candidate_overlay",
        authority_state="quality_review_readonly",
    )

    lines = "".join(_sign_entry(e, key) for e in (rework, canonical, candidate))
    ledger_path = tmp_path / "audit.jsonl"
    ledger_path.write_text(lines, encoding="utf-8")

    result = worker_ai_tools_mcp.verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="TASK_NF129",
        runner="test_runner",
        topic="nf129_topic",
        request_id="req-nf129",
    )
    assert result["ok"] is True
    assert result["live_source_graph_calls"] == 3
    authority = set(result["authority_index_identity"])
    assert "source_graph:rework_overlay:request_scoped_predecessor:/test/repo" in authority
    assert "source_graph:canonical:sole_authority:/test/repo" in authority
    assert "source_graph:candidate_overlay:quality_review_readonly:/test/repo" in authority
    # Three distinct labels, no conflation
    assert len(authority) == 3


# ---------------------------------------------------------------------------
# NF-2026-00118 / NF-2026-00131: quality-review lifecycle bootstrap regressions
# ---------------------------------------------------------------------------


def _sealed_reviewer_receipt(
    packet_sha256: str | None = None,
    reviewer_request_id: str = "rev-req-001",
    reviewer_task_id: str = "rev-task-001",
    target_request_id: str = "tgt-req-001",
    target_task_id: str = "tgt-task-001",
    provider: str = "deepseek_vscode_lm",
    claim_epoch: int = 1,
) -> dict:
    if packet_sha256 is None:
        packet_sha256 = hashlib.sha256(b"packet-body").hexdigest()
    from aiworkhub import quality_reviewer as _qr

    return {
        "schema_id": _qr.RECEIPT_SCHEMA_ID,
        "packet_sha256": packet_sha256,
        "target": {
            "request_id": target_request_id,
            "task_id": target_task_id,
            "claim_epoch": claim_epoch,
        },
        "reviewer": {
            "request_id": reviewer_request_id,
            "task_id": reviewer_task_id,
            "provider": provider,
        },
        "report": {
            "lens": "correctness",
            "provider": provider,
            "read_only": True,
            "can_mutate_repo": False,
            "findings": [],
        },
        "authority": {
            "process_identity_verified": True,
            "audit_verified": True,
            "terminal_state": "review_ready",
        },
        "submission_id": hashlib.sha256(b"sealed-submission").hexdigest(),
        "physical_submission_count": 1,
        "logical_submission_count": 1,
    }


def _accepted_latest_event(
    receipt: dict, reviewer_task_id: str = "rev-task-001", adapter_id: str = "deepseek_vscode_lm",
) -> dict:
    return {
        "state": "accepted",
        "accepted": True,
        "task_id": reviewer_task_id,
        "adapter_id": adapter_id,
        "quality_review_receipt": receipt,
    }


def _quality_review_workspace_metadata() -> dict:
    return {
        "request_id": "rev-req-001",
        "repo": "/tmp/quality-review-repo",
        "path": "/tmp/quality-review-workspace",
        "home": "/tmp/quality-review-home",
        "allowed_writes": [],
        "parent_baseline": {},
        "workspace_baseline": {},
        "inherited_rework_paths": [],
    }


def _accepted_card(
    receipt: dict,
    reviewer_request_id: str = "rev-req-001",
    reviewer_task_id: str = "rev-task-001",
    topic: str = "quality_review",
    claim_epoch: int = 1,
) -> dict:
    return {
        "task_id": reviewer_task_id,
        "accepted_request_id": reviewer_request_id,
        "topic": topic,
        "status": "finished",
        "allowed_writes": [],
        "terminal_review": {
            "evidence": {
                "quality_review_receipt": receipt,
                "quality_review": {
                    "target_claim_epoch": claim_epoch,
                    "adapter_id": "deepseek_vscode_lm",
                },
                "changed_paths": [],
                "changed_path_hashes": {},
                "workspace": _quality_review_workspace_metadata(),
            },
        },
        "accept_evidence": {"quality_review_receipt": receipt},
    }


def test_sealed_reviewer_receipt_survives_empty_workspace_hashes() -> None:
    """A sealed read-only reviewer receipt remains consumable after workspace
    cleanup when changed_paths=[] and canonical_delta_paths=[] because the
    receipt identity is self-contained in the event/card/receipt triple and
    does not depend on workspace-side hash files."""
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)

    result = process_launcher._verified_accepted_quality_review_receipt(
        latest=latest,
        card=card,
        reviewer_request_id="rev-req-001",
        target_request_id="tgt-req-001",
        target_task_id="tgt-task-001",
    )

    assert result["schema_id"] == receipt["schema_id"]
    assert result["packet_sha256"] == receipt["packet_sha256"]
    assert result["report"]["read_only"] is True
    assert result["report"]["can_mutate_repo"] is False
    assert result["authority"]["process_identity_verified"] is True
    # The receipt is valid with no workspace hashes — the canonical_delta_paths
    # and changed_paths are [] for a read-only sealed receipt.
    assert result["target"]["request_id"] == "tgt-req-001"
    assert result["target"]["task_id"] == "tgt-task-001"


def test_sealed_reviewer_receipt_rejects_event_not_accepted() -> None:
    """A reviewer event that was never accepted must be rejected even when the
    receipt payload is otherwise well-formed."""
    receipt = _sealed_reviewer_receipt()
    latest = {
        "state": "review_ready",
        "accepted": False,
        "task_id": "rev-task-001",
        "quality_review_receipt": receipt,
    }
    card = _accepted_card(receipt)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_reviewer_accepted_event_invalid",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_identity_mismatch() -> None:
    """A receipt whose task/request identity doesn't match the card must be
    rejected — sealed does not mean unverified."""
    receipt = _sealed_reviewer_receipt(
        reviewer_request_id="rev-req-001",
        target_request_id="tgt-req-001",
    )
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)
    # Card has a different request_id than what we pass as target
    card["accepted_request_id"] = "rev-req-001"

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_reviewer_accepted_target_mismatch",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="wrong-target-req",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_wrong_topic() -> None:
    """A card marked with a non-quality_review topic cannot satisfy the sealed
    receipt acceptance check."""
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt, topic="task_mcp")

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_reviewer_accepted_topic_mismatch",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_readonly_no_paths_no_hashes_with_receipt() -> None:
    """A retained read-only reviewer receipt with typed-empty changed_paths
    (list), changed_path_hashes (dict) and empty allowed_writes remains
    consumable after reload because its identity is self-contained."""
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)

    result = process_launcher._verified_accepted_quality_review_receipt(
        latest=latest,
        card=card,
        reviewer_request_id="rev-req-001",
        target_request_id="tgt-req-001",
        target_task_id="tgt-task-001",
    )

    assert result["schema_id"] == receipt["schema_id"]
    assert result["packet_sha256"] == receipt["packet_sha256"]
    assert result["submission_id"] == receipt["submission_id"]
    assert result["target"]["claim_epoch"] == 1
    assert result["physical_submission_count"] == 1
    assert result["logical_submission_count"] == 1


def test_sealed_reviewer_receipt_rejects_nonempty_changed_paths() -> None:
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)
    card["terminal_review"]["evidence"]["changed_paths"] = ["src/x.py"]

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_changed_paths_not_empty",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_nonempty_changed_path_hashes() -> None:
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)
    card["terminal_review"]["evidence"]["changed_path_hashes"] = {
        "src/x.py": hashlib.sha256(b"x").hexdigest()
    }

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_changed_path_hashes_not_empty",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_nonempty_workspace_allowed_writes() -> None:
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)
    card["terminal_review"]["evidence"]["workspace"]["allowed_writes"] = ["out/x.py"]

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_workspace_allowed_writes_not_empty",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_bool_claim_epoch() -> None:
    receipt = _sealed_reviewer_receipt(claim_epoch=True)  # type: ignore[arg-type]
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_claim_epoch_invalid",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_bool_submission_count() -> None:
    receipt = _sealed_reviewer_receipt()
    receipt["physical_submission_count"] = True
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_physical_submission_count_invalid",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_uppercase_packet_sha256() -> None:
    receipt = _sealed_reviewer_receipt()
    receipt["packet_sha256"] = receipt["packet_sha256"].upper()
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_packet_sha256_invalid",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_provider_mismatch() -> None:
    receipt = _sealed_reviewer_receipt(provider="other_provider")
    latest = _accepted_latest_event(receipt, adapter_id="deepseek_vscode_lm")
    card = _accepted_card(receipt)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_reviewer_provider_mismatch",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_claim_epoch_binding_mismatch() -> None:
    receipt = _sealed_reviewer_receipt(claim_epoch=7)
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt, claim_epoch=8)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_reviewer_claim_epoch_binding_mismatch",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_adapter_binding_mismatch() -> None:
    receipt = _sealed_reviewer_receipt()
    latest = _accepted_latest_event(receipt, adapter_id="deepseek_vscode_lm")
    card = _accepted_card(receipt)
    card["terminal_review"]["evidence"]["quality_review"]["adapter_id"] = "other_adapter"

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_reviewer_adapter_binding_mismatch",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_sealed_reviewer_receipt_rejects_missing_submission_counts() -> None:
    receipt = _sealed_reviewer_receipt()
    receipt.pop("submission_id")
    latest = _accepted_latest_event(receipt)
    card = _accepted_card(receipt)

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_receipt_top_level_keys_invalid",
    ):
        process_launcher._verified_accepted_quality_review_receipt(
            latest=latest, card=card,
            reviewer_request_id="rev-req-001",
            target_request_id="tgt-req-001",
            target_task_id="tgt-task-001",
        )


def test_quality_review_receipt_schema_rejects_string_claim_epoch() -> None:
    receipt = _sealed_reviewer_receipt()
    receipt["target"]["claim_epoch"] = "1"

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_claim_epoch_invalid",
    ):
        process_launcher._enforce_quality_review_receipt_schema(
            receipt, "deepseek_vscode_lm"
        )


def test_quality_review_receipt_schema_rejects_missing_findings() -> None:
    receipt = _sealed_reviewer_receipt()
    receipt["report"].pop("findings")

    with pytest.raises(
        process_launcher.WorkspaceError,
        match="quality_review_report_keys_invalid",
    ):
        process_launcher._enforce_quality_review_receipt_schema(
            receipt, "deepseek_vscode_lm"
        )


def test_native_cli_large_packet_uses_file_transport_avoiding_argv_e2big(
    tmp_path: Path,
) -> None:
    """Large quality-review packets (≥ 150 KB serialised) must be routed
    through file/stdin transport, never through argv, to avoid E2BIG on
    native CLI adapters."""
    from aiworkhub import quality_reviewer as _qr

    # Bounded path count (≤ MAX_PACKET_PATHS) with large mechanical_checks
    # provenance strings to push the serialised packet above 150 KB without
    # exceeding any production packet limit.
    large_changed_path_hashes = {
        f"src/large_module_{i:04d}.py": hashlib.sha256(
            f"content-{i}".encode("utf-8")
        ).hexdigest()
        for i in range(150)
    }
    packet = _qr.build_review_packet(
        request_id="req-e2big-001",
        task_id="task-e2big-001",
        claim_epoch=1,
        worker_provider="deepseek_vscode_lm",
        changed_path_hashes=large_changed_path_hashes,
        acceptance=["Packets >= 150 KB must avoid argv."],
        mechanical_checks=[
            {
                "check_id": f"ck-{j:04d}",
                "kind": "lint",
                "status": "ok",
                "provenance": "X" * 1900,
            }
            for j in range(70)
        ],
    )
    encoded = json.dumps(packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert len(encoded.encode("utf-8")) > 150_000, (
        f"Packet must be > 150 KB to trigger E2BIG gate; got {len(encoded.encode('utf-8'))} bytes"
    )

    packet_file = tmp_path / "large_packet.json"
    packet_file.write_text(encoded, encoding="utf-8")

    prompt = _qr.build_review_prompt(
        packet,
        lens="correctness",
        submit_tool_name="aiworkhub_worker_quality_review_submit",
        packet_file=str(packet_file),
        max_inline_bytes=96 * 1024,
    )
    assert "QUALITY_REVIEW_PACKET_FILE:" in prompt
    assert "QUALITY_REVIEW_PACKET:" not in prompt
    assert "aiworkhub_worker_quality_review_packet_read" in prompt


def test_bounded_review_submit_fails_closed_without_receipt_schema() -> None:
    """A receipt payload missing the schema_id or with a wrong schema must be
    rejected — the protocol fails closed rather than fabricating findings."""
    from aiworkhub import quality_reviewer as _qr

    packet = _qr.build_review_packet(
        request_id="req-bounded-001",
        task_id="task-bounded-001",
        claim_epoch=1,
        worker_provider="deepseek_vscode_lm",
        changed_path_hashes={"src/module.py": "a" * 64},
    )
    # No schema_id at all
    with pytest.raises(_qr.ReviewerEvidenceError):
        _qr.verify_reviewer_receipt(
            receipt={"report": {"lens": "correctness", "findings": []}},
            packet=packet,
            expected_reviewer_request_id="rev-req-001",
            expected_reviewer_task_id="rev-task-001",
            observed_provider="deepseek_vscode_lm",
            observed_terminal_state="review_ready",
            audit_verified=True,
        )

    # Wrong schema
    with pytest.raises(_qr.ReviewerEvidenceError):
        _qr.verify_reviewer_receipt(
            receipt={"schema_id": "aiworkhub.wrong_schema.v1", "report": {}},
            packet=packet,
            expected_reviewer_request_id="rev-req-001",
            expected_reviewer_task_id="rev-task-001",
            observed_provider="deepseek_vscode_lm",
            observed_terminal_state="review_ready",
            audit_verified=True,
        )


def test_bounded_review_submit_rejects_provider_spoof_in_receipt() -> None:
    """A receipt's self-reported provider must match the process-observed
    provider. A provider string in JSON proves nothing — the launcher
    independently records the actual adapter_id."""
    from aiworkhub import quality_reviewer as _qr

    receipt = _sealed_reviewer_receipt(provider="attacker_provider")
    packet = _qr.build_review_packet(
        request_id="req-provider-001",
        task_id="task-provider-001",
        claim_epoch=1,
        worker_provider="deepseek_vscode_lm",
        changed_path_hashes={"src/module.py": "a" * 64},
    )
    # Build a receipt with the right shape but spoofed provider
    shaped_receipt = {
        "schema_id": _qr.RECEIPT_SCHEMA_ID,
        "packet_sha256": packet["packet_sha256"],
        "target": {
            "request_id": "req-provider-001",
            "task_id": "task-provider-001",
            "claim_epoch": 1,
        },
        "reviewer": {
            "request_id": "rev-req-001",
            "task_id": "rev-task-001",
            "provider": "attacker_provider",
        },
        "report": {
            "lens": "correctness",
            "read_only": True,
            "can_mutate_repo": False,
            "findings": [],
        },
        "authority": {
            "process_identity_verified": True,
            "audit_verified": True,
            "terminal_state": "review_ready",
        },
    }
    with pytest.raises(
        _qr.ReviewerEvidenceError,
        match="reviewer_provider_spoofed",
    ):
        _qr.verify_reviewer_receipt(
            receipt=shaped_receipt,
            packet=packet,
            expected_reviewer_request_id="rev-req-001",
            expected_reviewer_task_id="rev-task-001",
            observed_provider="deepseek_vscode_lm",
            observed_terminal_state="review_ready",
            audit_verified=True,
        )


def _reserve_starting(
    manager,
    request_id: str,
    phase: str | None = None,
    *,
    expires_at_epoch: float | None = None,
) -> None:
    event = {
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "quality_review",
        "adapter_id": "claude_cli",
        "state": "starting",
        "reservation_expires_at_epoch": (
            time.time() + 600
            if expires_at_epoch is None
            else expires_at_epoch
        ),
        "owner_pid": os.getpid(),
        "owner_pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
    }
    if phase is not None:
        event["preparation_phase"] = phase
    manager._append_event(event)


def test_quality_review_prewarm_liveness_tracks_started_phase(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "req-prewarm-live"
    _reserve_starting(
        manager, request_id, "reviewer_source_graph_prewarm_started"
    )

    assert manager._reviewer_source_graph_prewarm_live(request_id) is True

    manager._publish_reviewer_progress(
        request_id, "reviewer_source_graph_prewarm_complete"
    )
    assert manager._reviewer_source_graph_prewarm_live(request_id) is False


def test_quality_review_launch_owner_join_keeps_live_prewarm(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "req-prewarm-live"
    _reserve_starting(
        manager, request_id, "reviewer_source_graph_prewarm_started"
    )
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_QUALITY_REVIEW_LAUNCH_OWNER_SECONDS",
        0.05,
    )

    def owner() -> None:
        time.sleep(0.25)

    launcher = threading.Thread(target=owner)
    launcher.start()

    outcome = manager._reviewer_launch_owner_join(launcher, request_id)
    launcher.join(timeout=5)

    assert outcome == "completed"


def test_quality_review_launch_owner_join_timeouts_without_live_prewarm(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "req-prewarm-stale"
    _reserve_starting(manager, request_id, "packet_prepared")
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_QUALITY_REVIEW_LAUNCH_OWNER_SECONDS",
        0.05,
    )

    stop = threading.Event()

    def owner() -> None:
        while not stop.is_set():
            time.sleep(0.01)

    launcher = threading.Thread(target=owner, daemon=True)
    launcher.start()

    outcome = manager._reviewer_launch_owner_join(launcher, request_id)
    stop.set()
    launcher.join(timeout=5)

    assert outcome == "timeout"


def test_quality_review_prewarm_reconciliation_defers_live_owned_prewarm(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    live_request = "req-live-prewarm-expired"
    _reserve_starting(
        manager,
        live_request,
        "reviewer_source_graph_prewarm_started",
        expires_at_epoch=time.time() - 60,
    )
    unrelated_request = "req-unrelated-stale"
    manager._append_event({
        "request_id": unrelated_request,
        "task_id": "TASK_UNRELATED",
        "runner": "claude_worker_b1",
        "topic": "quality_review",
        "adapter_id": "claude_cli",
        "state": "starting",
        "reservation_expires_at_epoch": time.time() - 60,
    })

    reconciled = manager._reconcile_expired_starting_reservations()

    assert reconciled == 1
    live_latest = manager._latest_by_request()[live_request]
    assert live_latest.get("state") == "starting"
    assert (
        live_latest.get("preparation_phase")
        == "reviewer_source_graph_prewarm_started"
    )
    unrelated_latest = manager._latest_by_request()[unrelated_request]
    assert unrelated_latest.get("state") == "blocked"
    assert unrelated_latest.get("blocked_reason") == "reservation_expired"


def test_quality_review_prewarm_reconciliation_fails_closed_without_live_owner(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    cases = {
        "req-dead-owner": {
            "owner_pid": 2**22 + 12345,
            "owner_pid_start_ticks": 1,
        },
        "req-mismatched-owner": {
            "owner_pid": os.getpid(),
            "owner_pid_start_ticks": 1,
        },
        "req-missing-owner": {},
    }
    for request_id, extra in cases.items():
        event = {
            "request_id": request_id,
            "task_id": "TASK_B1",
            "runner": "claude_worker_b1",
            "topic": "quality_review",
            "adapter_id": "claude_cli",
            "state": "starting",
            "reservation_expires_at_epoch": time.time() - 60,
            "preparation_phase": "reviewer_source_graph_prewarm_started",
        }
        event.update(extra)
        manager._append_event(event)

    reconciled = manager._reconcile_expired_starting_reservations()

    assert reconciled == 3
    for request_id in cases:
        latest = manager._latest_by_request()[request_id]
        assert latest.get("state") == "blocked"
        assert latest.get("blocked_reason") == "reservation_expired"


def test_quality_review_prewarm_live_fails_closed_on_unknown_identity(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "req-prewarm-unknown"
    # A positive owner pid whose start-ticks are missing yields UNKNOWN
    # identity evidence.  Live prewarm ownership requires an exact MATCH, so
    # UNKNOWN must fail closed rather than be treated as a live owned build.
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "quality_review",
        "adapter_id": "claude_cli",
        "state": "starting",
        "reservation_expires_at_epoch": time.time() - 60,
        "preparation_phase": "reviewer_source_graph_prewarm_started",
        "owner_pid": os.getpid(),
        "owner_pid_start_ticks": None,
    })

    assert manager._reviewer_source_graph_prewarm_live(request_id) is False

    reconciled = manager._reconcile_expired_starting_reservations()

    assert reconciled == 1
    latest = manager._latest_by_request()[request_id]
    assert latest.get("state") == "blocked"
    assert latest.get("blocked_reason") == "reservation_expired"


def _quality_review_card(task_id: str = "TASK_REVIEW_1") -> dict:
    return {
        "task_id": task_id,
        "runner": "claude_worker_reviewer",
        "topic": "quality_review",
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": "",
        "allowed_writes": [],
        "read_only": True,
        "priority": "high",
    }


def _reviewer_launch_setup(tmp_path: Path, monkeypatch):
    """Shared scaffolding for the real-``_launch_isolated`` reviewer ordering
    tests below.  Mocks only what a synthetic reviewer launch cannot
    reasonably exercise in a unit test -- the task-engine claim, and the
    git-backed candidate-overlay worktree diffing inside
    ``create_quality_review_workspace`` -- while calling the actual
    ``ProcessManager._launch_isolated`` method, so the ordering it enforces
    (workspace+packet creation, then authority verification, then prewarm,
    then runtime/provider registration) is exercised for real rather than
    reimplemented in the test.
    """

    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _quality_review_card()),
        argv=[sys.executable, "-c", "pass"],
    )
    monkeypatch.setattr(
        process_launcher.task_engine, "claim_start_exact",
        lambda *a, **k: {"ok": True},
    )
    monkeypatch.setattr(
        process_launcher, "_task_authority_repo", lambda repo, card: repo.resolve()
    )
    # The reviewer ordering under test is workspace+packet creation, then
    # authority, then prewarm, then runtime/provider registration -- it is not
    # host-sandbox selection.  ``_launch_isolated`` resolves the OS sandbox
    # backend before that ordering, and ``select_sandbox_backend`` legitimately
    # raises on a host with no bubblewrap/landlock (e.g. macOS CI), aborting the
    # launch before the ordering runs and leaving ``order`` empty.  Pin a fixed
    # backend -- an orthogonal dependency like the mocks above -- so the ordering
    # is exercised identically on every platform, exactly as it already is on
    # Linux where a real sandbox is present.
    monkeypatch.setattr(
        process_launcher, "_sandbox_backend_for_adapter", lambda adapter_id: "bubblewrap"
    )
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    candidate_home = tmp_path / "candidate_home"
    (candidate_home / "task_mcp_worker_runtime").mkdir(parents=True)
    fake_workspace = process_launcher.WorkerWorkspace(
        request_id="c" * 32,
        repo=candidate_dir,
        path=candidate_dir,
        home=candidate_home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    monkeypatch.setattr(
        process_launcher, "create_quality_review_workspace",
        lambda *a, **k: (fake_workspace, {"schema_id": "fake.v1"}),
    )
    binding = {
        "target_request_id": "target-request-1",
        "target_task_id": "TARGET_TASK_1",
        "target_claim_epoch": 1,
        "adapter_id": "claude_cli",
        "source_workspace": fake_workspace.as_metadata(),
        "candidate_paths": ["module.py"],
        "packet": {"packet_sha256": "a" * 64},
        "lens": "correctness",
    }
    return manager, binding


def test_quality_review_launch_isolated_orders_authority_prewarm_registration(
    monkeypatch, tmp_path,
):
    manager, binding = _reviewer_launch_setup(tmp_path, monkeypatch)
    order: list[str] = []

    def fake_authority(authority_repo):
        order.append("authority")
        return worker_ai_tools_mcp.AuthorityBinding(
            db_path=tmp_path / "canonical.sqlite",
            authority_source="canonical",
            authority_state="sole_authority",
            authority_repo=authority_repo,
        )

    def fake_prewarm(*_args, **_kwargs):
        order.append("prewarm")
        return {"ok": True, "built": True}

    def fake_registration(*_args, **_kwargs):
        order.append("registration")
        raise RuntimeError(
            "stop-after-registration: real subprocess spawn is out of scope for this ordering test"
        )

    monkeypatch.setattr(
        worker_ai_tools_mcp, "verify_quality_review_prewarm_authority", fake_authority
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp, "prewarm_quality_review_source_graph", fake_prewarm
    )
    monkeypatch.setattr(
        process_launcher, "_provision_worker_mcp_runtime_for_authority", fake_registration
    )

    manager._launch_isolated(
        task_id="TASK_REVIEW_1",
        runner="claude_worker_reviewer",
        topic="quality_review",
        adapter_id="claude_cli",
        model=None,
        owner_prompt="",
        timeout_seconds=30,
        quality_review_binding=binding,
    )

    assert order == ["authority", "prewarm", "registration"]


def test_quality_review_launch_isolated_fails_closed_before_prewarm_and_registration(
    monkeypatch, tmp_path,
):
    manager, binding = _reviewer_launch_setup(tmp_path, monkeypatch)
    order: list[str] = []

    def fail_authority(authority_repo):
        order.append("authority")
        raise worker_ai_tools_mcp.WorkerToolError(
            "authority_component_not_canonical_active:source_graph.source_graph:shadow"
        )

    def fake_prewarm(*_args, **_kwargs):
        order.append("prewarm")
        return {"ok": True, "built": True}

    def fake_registration(*_args, **_kwargs):
        order.append("registration")
        raise RuntimeError("registration must never run after a failed authority check")

    monkeypatch.setattr(
        worker_ai_tools_mcp, "verify_quality_review_prewarm_authority", fail_authority
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp, "prewarm_quality_review_source_graph", fake_prewarm
    )
    monkeypatch.setattr(
        process_launcher, "_provision_worker_mcp_runtime_for_authority", fake_registration
    )

    result = manager._launch_isolated(
        task_id="TASK_REVIEW_1",
        runner="claude_worker_reviewer",
        topic="quality_review",
        adapter_id="claude_cli",
        model=None,
        owner_prompt="",
        timeout_seconds=30,
        quality_review_binding=binding,
    )

    assert order == ["authority"]
    assert result.get("ok") is False
    assert "quality_review_source_graph_authority_unverified" in str(
        result.get("blocked_reason") or ""
    )


def test_quality_review_launch_isolated_classifies_prewarm_data_failure_truthfully(
    monkeypatch, tmp_path,
):
    """A Source Graph contract/data failure surfaced from the prewarm call
    (as ``worker_ai_tools_mcp.prewarm_quality_review_source_graph`` now always
    raises ``WorkerToolError`` for its own clone/backup/index/schema
    failures) must be classified as a truthful, expected
    ``quality_review_source_graph_prewarm_failed`` launch block -- never
    folded into the generic ``unexpected_launch_error`` bucket reserved for
    real provider-launch anomalies -- and must never reach runtime/provider
    registration.
    """

    manager, binding = _reviewer_launch_setup(tmp_path, monkeypatch)
    order: list[str] = []

    def fake_authority(authority_repo):
        order.append("authority")
        return SimpleNamespace(
            authority_source="canonical", authority_state="sole_authority",
        )

    def fail_prewarm(*_args, **_kwargs):
        order.append("prewarm")
        raise worker_ai_tools_mcp.WorkerToolError(
            "quality_review_candidate_source_graph_prewarm_error:"
            "OperationalError:disk I/O error"
        )

    def fake_registration(*_args, **_kwargs):
        order.append("registration")
        raise RuntimeError("registration must never run after a failed prewarm")

    monkeypatch.setattr(
        worker_ai_tools_mcp, "verify_quality_review_prewarm_authority", fake_authority
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp, "prewarm_quality_review_source_graph", fail_prewarm
    )
    monkeypatch.setattr(
        process_launcher, "_provision_worker_mcp_runtime_for_authority", fake_registration
    )

    result = manager._launch_isolated(
        task_id="TASK_REVIEW_1",
        runner="claude_worker_reviewer",
        topic="quality_review",
        adapter_id="claude_cli",
        model=None,
        owner_prompt="",
        timeout_seconds=30,
        quality_review_binding=binding,
    )

    assert order == ["authority", "prewarm"]
    assert result.get("ok") is False
    reason = str(result.get("blocked_reason") or "")
    assert reason.startswith(
        "quality_review_source_graph_prewarm_failed:"
        "quality_review_candidate_source_graph_prewarm_error:"
    )
    assert "unexpected_launch_error" not in reason
    assert "diagnostic" not in result


def test_crash_retry_packet_carries_unsanitized_diagnostics_and_hashes_delivered(
    tmp_path: Path,
) -> None:
    """fix #4: the predecessor diagnostics are embedded in a JSON packet, so
    JSON encoding already neutralises every metacharacter. The HTML-oriented
    live-output sanitiser must not run: it escaped and redacted bytes the
    successor needs verbatim, and the tail hashes were computed over the
    unsanitised bytes, so the corruption was undetectable. Carry the bytes
    unescaped/unredacted and hash exactly the bytes delivered."""

    repo = tmp_path / "repo"
    process_dir = tmp_path / "processes"
    worktree = tmp_path / "succ" / "worktree"
    home = tmp_path / "succ" / "home"
    for directory in (repo, process_dir, worktree, home):
        directory.mkdir(parents=True)
    workspace = process_launcher.WorkerWorkspace(
        request_id="7" * 32,
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    predecessor = "8" * 32
    process_launcher.write_json_0600(
        process_dir / f"{predecessor}.request.json",
        {"request_id": predecessor, "task_id": "TASK_SAME", "workspace": {"repo": str(repo)}},
    )
    process_launcher.write_json_0600(
        process_dir / f"{predecessor}.supervisor.json",
        {"state": "supervisor_error", "exit_code": 1, "error": "boom"},
    )
    # HTML metacharacters plus a long opaque token the live-output sanitiser
    # would escape/redact. Kept short so the whole stream is the delivered tail.
    long_token = "SECRET" + "x" * 80
    stdout_text = f"<step> a & b {long_token}\n"
    stderr_text = f'trace <b>"boom"</b> {long_token}\n'
    (process_dir / f"{predecessor}.stdout.log").write_text(stdout_text, encoding="utf-8")
    (process_dir / f"{predecessor}.stderr.log").write_text(stderr_text, encoding="utf-8")
    overlay = {
        "predecessor_request_id": predecessor,
        "predecessor_task_id": "TASK_SAME",
        "canonical_digest": "c" * 64,
    }

    _path, packet = process_launcher._materialize_crash_retry_packet(
        process_dir,
        workspace,
        task_id="TASK_SAME",
        card={"rework_predecessor": {"request_id": predecessor}},
        rework_overlay_packet=overlay,
    )

    assert packet is not None
    # Verbatim: no HTML escaping and no long-token redaction.
    assert packet["stdout_tail"] == stdout_text
    assert packet["stderr_tail"] == stderr_text
    assert long_token in packet["stdout_tail"]
    assert "&amp;" not in packet["stdout_tail"]
    assert "&quot;" not in packet["stderr_tail"]
    # The tail hashes cover exactly the bytes delivered in the packet.
    assert (
        packet["stdout_tail_sha256"]
        == hashlib.sha256(packet["stdout_tail"].encode("utf-8")).hexdigest()
    )
    assert (
        packet["stderr_tail_sha256"]
        == hashlib.sha256(packet["stderr_tail"].encode("utf-8")).hexdigest()
    )


def _finalize_retry_manager(tmp_path: Path, *, retained_delta: bool = False):
    from aiworkhub import worker_workspace

    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card(state="review")),
        argv=[sys.executable, "-c", "pass"],
    )
    request_id = "f" * 32
    worktree = tmp_path / "ws"
    home = tmp_path / "ws-home"
    worktree.mkdir()
    home.mkdir()
    workspace = process_launcher.WorkerWorkspace(
        request_id=request_id,
        repo=manager.repo,
        path=worktree,
        home=home,
        allowed_writes=(("changed.py",) if retained_delta else ()),
        parent_baseline=({"changed.py": None} if retained_delta else {}),
        workspace_baseline=({"changed.py": None} if retained_delta else {}),
        base_oid="b" * 40,
    )
    if retained_delta:
        (worktree / "changed.py").write_bytes(b"retained worker edit")
    stdout_path = tmp_path / f"{request_id}.stdout.log"
    stderr_path = tmp_path / f"{request_id}.stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    status_path = manager.process_dir / f"{request_id}.supervisor.json"
    metadata_path = manager.process_dir / f"{request_id}.request.json"
    # A non-exited terminal state takes the light failure finalization path,
    # which still runs the shared usage-recording block being exercised here.
    worker_workspace.write_json_0600(status_path, {"state": "cancelled"})
    metadata = {
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "claude_cli",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(tmp_path / f"{request_id}.cancel.json"),
        "workspace": workspace.as_metadata(),
        "claim_epoch": 2,
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    return manager, request_id, metadata_path, status_path


def test_process_manager_worker_failed_retains_scoped_delta_without_outputs(
    monkeypatch, tmp_path
):
    from aiworkhub import worker_workspace

    manager, request_id, metadata_path, status_path = _finalize_retry_manager(
        tmp_path, retained_delta=True
    )
    worker_workspace.write_json_0600(
        status_path, {"state": "supervisor_error", "error": "provider quota refusal"}
    )
    manager._append_event({
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "state": "finalizing",
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
        "pid": 999_999_999,
        "pid_start_ticks": 1,
    })
    captured: dict = {}
    monkeypatch.setattr(process_launcher, "enforce_scope", lambda *_a, **_k: ["changed.py"])
    monkeypatch.setattr(manager, "_exact_claim_state", lambda _metadata: "processing")
    monkeypatch.setattr(
        process_launcher,
        "_terminal_rework_delta_evidence",
        lambda *_a, **_k: {"schema_id": "aiworkhub.rework_delta_descriptor.v1", "sealed": True},
    )
    monkeypatch.setattr(manager, "_persist_attempt_artifacts", lambda *_a, **_k: None)

    def terminal_failure(_metadata, state, *, evidence, **_kwargs):
        captured.update(evidence)
        assert state == "worker_failed"
        return {"ok": True, "stderr": ""}

    monkeypatch.setattr(manager, "_terminal_failure_exact", terminal_failure)
    event = manager._finalize_isolated_request(request_id, 1)

    digest = hashlib.sha256(b"retained worker edit").hexdigest()
    assert event["state"] == "worker_failed"
    assert captured["changed_paths"] == ["changed.py"]
    assert captured["changed_path_hashes"] == {"changed.py": digest}
    assert captured["python_candidate_authority"]["sources"] == [
        {"path": "changed.py", "state": "added", "bytes_sha256": digest}
    ]
    assert captured["rework_delta"]["sealed"] is True


def test_release_pending_retry_records_provider_token_spend(monkeypatch, tmp_path):
    """fix #8: a release_pending predecessor is a finalization-pending state
    that never recorded provider spend. On the retry the spend must be recorded,
    not lost by reusing an empty prior-usage record."""

    manager, request_id, metadata_path, status_path = _finalize_retry_manager(tmp_path)
    # A release_pending predecessor carried NO recorded usage, then a retry
    # appended its ``finalizing`` event (as retry_finalization does).
    common = {
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
        "pid": 999_999_999,
        "pid_start_ticks": 1,
    }
    manager._append_event({**common, "state": "release_pending"})
    manager._append_event(
        {**common, "state": "finalizing", "finalization_retry": True}
    )

    recorded: list[tuple[str, dict]] = []

    def fake_record_usage(request_id_arg, *_args, **kwargs):
        recorded.append((request_id_arg, dict(kwargs)))
        return {"input_tokens": 123, "output_tokens": 45}, True, ""

    monkeypatch.setattr(manager, "_record_usage", fake_record_usage)
    monkeypatch.setattr(
        manager, "_terminal_failure_exact", lambda *a, **k: {"ok": True, "stderr": ""}
    )
    monkeypatch.setattr(manager, "_persist_attempt_artifacts", lambda *a, **k: None)

    event = manager._finalize_isolated_request(request_id, 0)

    assert recorded == [
        (
            request_id,
            {
                "topic": "task_mcp",
                "execution_mode": "",
                "claim_authority": {
                    "request_id": request_id,
                    "claimed_by": "claude_worker_b1",
                    "claim_epoch": 2,
                },
            },
        )
    ]
    assert event["usage_recorded"] is True
    assert event["usage"]["input_tokens"] == 123


def test_finalization_retry_reuses_prior_recorded_usage(monkeypatch, tmp_path):
    """A retry whose predecessor already recorded usage must reuse it (no
    double count), never re-record."""

    manager, request_id, metadata_path, status_path = _finalize_retry_manager(tmp_path)
    common = {
        "request_id": request_id,
        "task_id": "TASK_B1",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
        "pid": 999_999_999,
        "pid_start_ticks": 1,
    }
    manager._append_event(
        {
            **common,
            "state": "finalize_failed",
            "usage": {"input_tokens": 7},
            "usage_recorded": True,
        }
    )
    manager._append_event(
        {**common, "state": "finalizing", "finalization_retry": True}
    )

    monkeypatch.setattr(
        manager,
        "_record_usage",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not re-record already-recorded usage")
        ),
    )
    monkeypatch.setattr(
        manager, "_terminal_failure_exact", lambda *a, **k: {"ok": True, "stderr": ""}
    )
    monkeypatch.setattr(manager, "_persist_attempt_artifacts", lambda *a, **k: None)

    event = manager._finalize_isolated_request(request_id, 0)

    assert event["usage_recorded"] is True
    assert event["usage"]["input_tokens"] == 7


def test_terminal_rework_delta_evidence_seals_changed_and_deleted_paths(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "changed.py").write_bytes(b"changed\n")
    workspace = SimpleNamespace(repo=repo, path=worktree)
    captured = {}

    monkeypatch.setattr(
        process_launcher._worker_workspace,
        "configured_runtime_root",
        lambda authority_repo: authority_repo / ".aiworkhub" / "runtime",
    )

    def seal(authority_repo, task_id, request_id, claim_epoch, entries, artifact_dir):
        captured.update(
            authority_repo=authority_repo,
            task_id=task_id,
            request_id=request_id,
            claim_epoch=claim_epoch,
            entries=list(entries),
            artifact_dir=artifact_dir,
        )
        return {"path": str(artifact_dir / "packet.json"), "digest": "a" * 64}

    monkeypatch.setattr(
        process_launcher._worker_workspace, "seal_rework_delta_artifact", seal
    )

    evidence = process_launcher._terminal_rework_delta_evidence(
        workspace,
        {"task_id": "TASK-DELTA", "claim_epoch": 3},
        "a" * 32,
        ["src/changed.py", "src/deleted.py"],
    )

    assert evidence == {
        "schema_id": "aiworkhub.rework_delta_descriptor.v1",
        "sealed": True,
        "authority_repo": str(repo.resolve()),
        "task_id": "TASK-DELTA",
        "request_id": "a" * 32,
        "claim_epoch": 3,
        "artifact_path": str(repo / ".aiworkhub/runtime/rework_deltas/packet.json"),
        "artifact_sha256": "a" * 64,
    }
    assert captured["authority_repo"] == repo.resolve()
    assert captured["entries"] == [
        ("src/changed.py", b"changed\n"),
        ("src/deleted.py", None),
    ]
    assert captured["artifact_dir"] == repo / ".aiworkhub/runtime/rework_deltas"


@pytest.mark.parametrize("claim_epoch", [True, 0, "1", None])
def test_terminal_rework_delta_evidence_rejects_invalid_claim_epoch(
    monkeypatch, tmp_path, claim_epoch
):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    (worktree / "changed.py").write_bytes(b"changed")
    monkeypatch.setattr(
        process_launcher._worker_workspace,
        "seal_rework_delta_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid identity must not seal")
        ),
    )

    evidence = process_launcher._terminal_rework_delta_evidence(
        SimpleNamespace(repo=repo, path=worktree),
        {"task_id": "TASK-DELTA", "claim_epoch": claim_epoch},
        "b" * 32,
        ["changed.py"],
    )

    assert evidence == {
        "schema_id": "aiworkhub.rework_delta_seal.v1",
        "sealed": False,
        "reason": "rework_delta_identity_invalid",
    }


def test_terminal_rework_delta_evidence_reports_seal_failure_without_descriptor(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    (worktree / "changed.py").write_bytes(b"changed")
    monkeypatch.setattr(
        process_launcher._worker_workspace,
        "configured_runtime_root",
        lambda authority_repo: authority_repo / ".aiworkhub" / "runtime",
    )
    monkeypatch.setattr(
        process_launcher._worker_workspace,
        "seal_rework_delta_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            process_launcher.WorkspaceError("synthetic_failure")
        ),
    )

    evidence = process_launcher._terminal_rework_delta_evidence(
        SimpleNamespace(repo=repo, path=worktree),
        {"task_id": "TASK-DELTA", "claim_epoch": 2},
        "c" * 32,
        ["changed.py"],
    )

    assert evidence["sealed"] is False
    assert evidence["reason"] == "rework_delta_seal_failed:synthetic_failure"
    assert "artifact_path" not in evidence


@pytest.mark.parametrize("terminal_state", ["worker_failed", "finalize_failed"])
def test_terminal_failure_retained_candidate_is_independent_of_required_outputs(
    tmp_path: Path, terminal_state: str
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    candidate = worktree / "changed.py"
    candidate.write_bytes(b"candidate")
    workspace = SimpleNamespace(
        path=worktree,
        repo=repo,
        allowed_writes=("changed.py",),
        parent_baseline={"changed.py": None},
        base_oid="b" * 40,
        as_metadata=lambda: {
            "request_id": "d" * 32,
            "repo": str(repo),
            "path": str(worktree),
            "allowed_writes": ["changed.py"],
            "parent_baseline": {"changed.py": None},
            "base_oid": "b" * 40,
        },
    )
    evidence = process_launcher._retained_candidate_identity_evidence(
        workspace,
        {
            "task_id": "TASK-RETAINED",
            "runner": "worker",
            "topic": "code",
            "claim_epoch": 2,
            "required_outputs": [],
        },
        "d" * 32,
        ["changed.py"],
        terminal_state,
    )
    assert evidence["changed_path_hashes"] == {
        "changed.py": hashlib.sha256(b"candidate").hexdigest()
    }
    assert evidence["python_candidate_authority"]["sources"] == [
        {
            "path": "changed.py",
            "state": "added",
            "bytes_sha256": hashlib.sha256(b"candidate").hexdigest(),
        }
    ]


# ---------------------------------------------------------------------------
# NF-2026-00401: reconciliation records terminal intent under the registry
# lock and settles it against the task store with the lock released.
# ---------------------------------------------------------------------------


def _committed_reviewer_event(
    request_id: str,
    *,
    reviewer_claim_epoch: int | None = 5,
    task_id: str = "TASK_B1",
) -> dict:
    """A committed reviewer attempt whose owner identity is a proven mismatch."""

    event = {
        "request_id": request_id,
        "task_id": task_id,
        "runner": "claude_worker_b1",
        "topic": "quality_review",
        "adapter_id": "claude_cli",
        "state": "provider_spawn_committed",
        "reservation_expires_at_epoch": time.time() + 600,
        "owner_pid": os.getpid(),
        "owner_pid_start_ticks": 1,
    }
    if reviewer_claim_epoch is not None:
        event["reviewer_claim_epoch"] = reviewer_claim_epoch
    return event


def test_reconciliation_reaches_no_task_store_under_the_registry_lock(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event(_committed_reviewer_event("req-no-store"))

    def forbidden(*_args, **_kwargs):
        pytest.fail("reconciliation must not touch the task store under the lock")

    for name in ("mark_terminal_failure", "get_task", "_require_ready", "_connect"):
        monkeypatch.setattr(process_launcher.task_store, name, forbidden)

    assert manager._reconcile_expired_starting_reservations() == 1
    latest = manager._latest_by_request()["req-no-store"]
    assert latest["state"] == "blocked"
    assert latest["blocked_reason"] == "provider_spawn_committed_owner_dead"
    assert latest["terminal_intent"] == "recorded"
    assert manager._reviewer_terminal_intent_path("req-no-store").is_file()


def _intent_diagnostics(manager) -> list[dict]:
    """Every operator-visible line recorded for an unsettleable intent."""

    path = process_launcher.reviewer_terminal_intent_diagnostic_path(
        manager.process_log_path
    )
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_terminal_intent_without_bindable_identity_is_never_acted_on(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event(_committed_reviewer_event("req-bound", reviewer_claim_epoch=6))
    assert manager._reconcile_expired_starting_reservations() == 1

    intent_path = manager._reviewer_terminal_intent_path("req-bound")
    payload = json.loads(intent_path.read_text(encoding="utf-8"))
    payload["reviewer_claim_epoch"] = None
    intent_path.write_text(json.dumps(payload), encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        pytest.fail("an unbindable intent must never reach the task store")

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", forbidden
    )
    monkeypatch.setattr(
        process_launcher.task_store, "enqueue_terminal_callback", forbidden
    )

    ledger_before = len(manager._events())
    assert manager._settle_reviewer_terminal_intents() == 0
    # It is retained, not deleted: a corrupt intent is evidence, not garbage.
    assert intent_path.is_file()

    # ...but silent evidence strands the card with nothing saying why, so the
    # diagnostic ledger names it exactly once.
    diagnostics = _intent_diagnostics(manager)
    assert len(diagnostics) == 1
    assert diagnostics[0]["reason"] == "identity_unbindable"
    assert diagnostics[0]["intent_file"] == intent_path.name
    assert diagnostics[0]["schema_id"] == (
        process_launcher.REVIEWER_TERMINAL_INTENT_DIAGNOSTIC_SCHEMA_ID
    )
    assert diagnostics[0]["bytes"] == intent_path.stat().st_size

    # Reporting it must not re-enter settlement: no process-ledger event, and
    # therefore no second reconcile pass and no callback.
    assert len(manager._events()) == ledger_before
    for _ in range(3):
        assert manager._settle_reviewer_terminal_intents() == 0
    assert len(_intent_diagnostics(manager)) == 1


def test_foreign_intent_files_are_ignored(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager.process_dir.mkdir(parents=True, exist_ok=True)
    foreign = manager.process_dir / (
        "deadbeef" + process_launcher.ProcessManager._REVIEWER_TERMINAL_INTENT_SUFFIX
    )
    foreign.write_text(json.dumps({"schema_id": "someone.else.v1"}), encoding="utf-8")
    unreadable = manager.process_dir / (
        "beefdead" + process_launcher.ProcessManager._REVIEWER_TERMINAL_INTENT_SUFFIX
    )
    unreadable.write_text("{not json at all", encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        pytest.fail("a foreign schema must never drive a terminal transition")

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", forbidden
    )
    monkeypatch.setattr(
        process_launcher.task_store, "enqueue_terminal_callback", forbidden
    )

    assert manager._settle_reviewer_terminal_intents() == 0
    assert foreign.is_file()
    assert unreadable.is_file()

    reasons = {
        record["intent_file"]: record["reason"]
        for record in _intent_diagnostics(manager)
    }
    assert reasons == {
        foreign.name: "foreign_schema",
        unreadable.name: "unparseable_json",
    }
    # Each file is reported once no matter how many passes run over it.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert len(_intent_diagnostics(manager)) == 2


def test_unreadable_intent_bytes_are_named_once_and_never_confused_with_a_race(
    tmp_path: Path, monkeypatch
) -> None:
    # ``FileNotFoundError`` on this read means the settler that won the intent
    # retired it underneath us -- the benign race the design is built around,
    # and rightly silent.  Any OTHER read failure leaves the intent sitting
    # there unsettleable, and reading it as that same race stranded the card
    # with nothing anywhere saying why.
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event(
        _committed_reviewer_event("req-unreadable", reviewer_claim_epoch=4)
    )
    assert manager._reconcile_expired_starting_reservations() == 1
    intent_path = manager._reviewer_terminal_intent_path("req-unreadable")
    assert intent_path.is_file()

    def forbidden(*_args, **_kwargs):
        pytest.fail("an unreadable intent must never reach the task store")

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", forbidden
    )
    monkeypatch.setattr(
        process_launcher.task_store, "enqueue_terminal_callback", forbidden
    )

    # Injected rather than produced with chmod: chmod's effect on a read
    # depends on the filesystem and on whether the suite runs as root, so it
    # cannot state deterministically which branch is under test.
    injected: list[OSError] = [FileNotFoundError(errno.ENOENT, "already retired")]
    exact_read_text = Path.read_text

    def refusing_read_text(self, *args, **kwargs):
        if self.name == intent_path.name:
            raise injected[0]
        return exact_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refusing_read_text)

    # The winner already retired it: nothing to report.
    for _ in range(2):
        assert manager._settle_reviewer_terminal_intents() == 0
    assert _intent_diagnostics(manager) == []

    # The intent is still THERE and unreadable, which no pass can settle.
    injected[0] = OSError(errno.EIO, "injected unreadable intent")
    for _ in range(3):
        assert manager._settle_reviewer_terminal_intents() == 0

    monkeypatch.undo()
    # Retained, never deleted: the intent is the only thing that brings a
    # later pass back to this claim once the bytes are readable again.
    assert intent_path.is_file()
    diagnostics = _intent_diagnostics(manager)
    assert len(diagnostics) == 1
    assert diagnostics[0]["reason"] == "unreadable_bytes"
    assert diagnostics[0]["intent_file"] == intent_path.name
    assert diagnostics[0]["schema_id"] == (
        process_launcher.REVIEWER_TERMINAL_INTENT_DIAGNOSTIC_SCHEMA_ID
    )


def test_unsettleable_intent_diagnostics_are_bounded_per_pass(
    tmp_path: Path,
) -> None:
    # An operator who leaves a pile of corrupt intents in place must not have
    # the diagnostic ledger grow without bound in a single pass -- but nothing
    # may be silently dropped either, so later passes report the remainder.
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager.process_dir.mkdir(parents=True, exist_ok=True)
    cap = process_launcher.ProcessManager._TERMINAL_INTENT_DIAGNOSTICS_PER_PASS
    total = cap + 3
    for index in range(total):
        path = manager.process_dir / (
            f"{index:08x}"
            + process_launcher.ProcessManager._REVIEWER_TERMINAL_INTENT_SUFFIX
        )
        path.write_text("{ truncated", encoding="utf-8")

    assert manager._settle_reviewer_terminal_intents() == 0
    assert len(_intent_diagnostics(manager)) == cap

    assert manager._settle_reviewer_terminal_intents() == 0
    assert len(_intent_diagnostics(manager)) == total

    # Every intent is now accounted for exactly once and nothing repeats.
    files = [record["intent_file"] for record in _intent_diagnostics(manager)]
    assert len(set(files)) == total
    assert manager._settle_reviewer_terminal_intents() == 0
    assert len(_intent_diagnostics(manager)) == total


def test_terminal_intent_resolution_classifies_store_outcomes() -> None:
    resolved = process_launcher.ProcessManager._terminal_intent_is_resolved
    # Outcomes proving no future pass could move this exact claim.
    assert resolved("task_not_found") is True
    assert resolved("runner_mismatch") is True
    assert resolved("claim_owner_mismatch") is True
    assert resolved("launch_request_mismatch") is True
    assert resolved("expected_claim_epoch_invalid") is True
    assert resolved("claim_epoch_mismatch:expected=3:current=4") is True
    assert resolved("not_processing:current=blocked") is True
    assert resolved("unsupported_terminal_failure:nonsense") is True
    # A CAS conflict is transient and must keep the intent for the next pass.
    assert resolved("terminal_failure_transition_conflict") is False
    assert resolved("blocked") is False


def test_terminal_intent_resolution_reads_the_stores_own_vocabulary() -> None:
    # The launcher must not keep a private copy of these states: a copy stops
    # matching the day the store grows a new fail-closed outcome, and the
    # intent would then be retried forever against a card it can never move.
    store = process_launcher.task_store
    resolved = process_launcher.ProcessManager._terminal_intent_is_resolved
    for state in store.TERMINAL_FAILURE_FINAL_STATES:
        assert resolved(state) is True
    for prefix in store.TERMINAL_FAILURE_FINAL_STATE_PREFIXES:
        assert resolved(prefix + "observed") is True
    assert store.terminal_failure_state_is_final("") is False
    assert store.terminal_failure_state_is_final(None) is False


def _pending_intent(tmp_path: Path, request_id: str, claim_epoch: int = 4):
    """A manager holding exactly one durable, bindable terminal intent."""

    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event(
        _committed_reviewer_event(request_id, reviewer_claim_epoch=claim_epoch)
    )
    assert manager._reconcile_expired_starting_reservations() == 1
    intent_path = manager._reviewer_terminal_intent_path(request_id)
    assert intent_path.is_file()
    return manager, intent_path


_FINAL_REFUSALS = sorted(
    process_launcher.task_store.TERMINAL_FAILURE_FINAL_STATES
) + [
    prefix + "observed"
    for prefix in process_launcher.task_store.TERMINAL_FAILURE_FINAL_STATE_PREFIXES
]


@pytest.mark.parametrize("final_state", _FINAL_REFUSALS)
def test_final_refusal_never_retires_an_intent_silently(
    tmp_path: Path, monkeypatch, final_state: str
) -> None:
    # Across the WHOLE final-state vocabulary: a refusal that is not this
    # intent's own completed transition retires the ticket having moved no
    # card at all.  Retiring it silently is indistinguishable from a
    # settlement that worked, so every one of these must leave evidence.
    manager, intent_path = _pending_intent(tmp_path, "req-final")

    monkeypatch.setattr(
        process_launcher.task_store,
        "mark_terminal_failure",
        lambda *_a, **_k: (False, final_state),
    )
    monkeypatch.setattr(
        process_launcher.task_store,
        "terminal_failure_already_applied",
        lambda *_a, **_k: False,
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("a refusal that moved no card owes no callback")

    monkeypatch.setattr(
        process_launcher.task_store, "enqueue_terminal_callback", forbidden
    )

    assert manager._settle_reviewer_terminal_intents() == 0
    # Spent: no later pass could ever move this claim, so the ticket goes...
    assert not intent_path.exists()
    # ...but never before the refusal is on the record, exactly once.
    diagnostics = _intent_diagnostics(manager)
    assert len(diagnostics) == 1
    assert diagnostics[0]["reason"] == f"final_refusal:{final_state}"
    assert diagnostics[0]["intent_file"] == intent_path.name
    assert diagnostics[0]["schema_id"] == (
        process_launcher.REVIEWER_TERMINAL_INTENT_DIAGNOSTIC_SCHEMA_ID
    )
    # Retiring the ticket retires its markers too: nothing is left behind to
    # suppress the diagnostic if the same request ever recurs.
    assert not manager._terminal_intent_diagnosed_marker(intent_path).exists()
    assert not manager._terminal_intent_retired_marker(intent_path).exists()
    # Nothing re-enters settlement and nothing repeats.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert len(_intent_diagnostics(manager)) == 1


def test_final_refusal_retains_the_intent_when_evidence_cannot_be_written(
    tmp_path: Path, monkeypatch
) -> None:
    # Never silent-drop: when the refusal cannot be recorded the ticket is
    # retained so a later pass reports AND retires it, rather than the intent
    # disappearing with nothing anywhere saying it ever existed.
    manager, intent_path = _pending_intent(tmp_path, "req-unwritable")

    monkeypatch.setattr(
        process_launcher.task_store,
        "mark_terminal_failure",
        lambda *_a, **_k: (False, "task_not_found"),
    )
    monkeypatch.setattr(
        process_launcher.task_store,
        "terminal_failure_already_applied",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_append_intent_diagnostic",
        lambda self, record: False,
    )

    assert manager._settle_reviewer_terminal_intents() == 0
    assert intent_path.is_file()
    assert _intent_diagnostics(manager) == []
    # The failed claim was given back, so it cannot suppress a later report.
    assert not manager._terminal_intent_retired_marker(intent_path).exists()

    # A later pass, with the ledger writable again, reports and then retires.
    monkeypatch.undo()
    monkeypatch.setattr(
        process_launcher.task_store,
        "mark_terminal_failure",
        lambda *_a, **_k: (False, "task_not_found"),
    )
    monkeypatch.setattr(
        process_launcher.task_store,
        "terminal_failure_already_applied",
        lambda *_a, **_k: False,
    )
    assert manager._settle_reviewer_terminal_intents() == 0
    assert not intent_path.exists()
    reasons = [record["reason"] for record in _intent_diagnostics(manager)]
    assert reasons == ["final_refusal:task_not_found"]


_ABSENT = object()

# Every shape that leaves a proven-dead reservation impossible to bind to an
# exact task/request/claim epoch.  Each one must fail closed AND say so once.
_IDENTITY_INCOMPLETE_EVENTS = {
    "epoch_missing": {"reviewer_claim_epoch": _ABSENT},
    "epoch_zero": {"reviewer_claim_epoch": 0},
    "epoch_negative": {"reviewer_claim_epoch": -3},
    # ``bool`` is an ``int`` subclass, so a bare isinstance check would accept
    # these and bind a transition to epoch 1 or 0.
    "epoch_bool_true": {"reviewer_claim_epoch": True},
    "epoch_bool_false": {"reviewer_claim_epoch": False},
    "epoch_string": {"reviewer_claim_epoch": "7"},
    "epoch_float": {"reviewer_claim_epoch": 7.0},
    "task_id_missing": {"task_id": _ABSENT},
    "task_id_blank": {"task_id": "   "},
    "runner_missing": {"runner": _ABSENT},
    "runner_blank": {"runner": ""},
}


def _incomplete_identity_event(request_id: str, label: str) -> dict:
    event = _committed_reviewer_event(request_id)
    for key, value in _IDENTITY_INCOMPLETE_EVENTS[label].items():
        if value is _ABSENT:
            event.pop(key, None)
        else:
            event[key] = value
    return event


@pytest.mark.parametrize("label", sorted(_IDENTITY_INCOMPLETE_EVENTS))
def test_identity_incomplete_committed_reservation_is_reported_exactly_once(
    tmp_path: Path, monkeypatch, label: str
) -> None:
    # A proven-dead committed reservation that cannot be bound is never
    # terminalized -- correctly -- but that refusal is re-derived from the same
    # unchanged row on every pass.  Without a diagnostic it is silent forever.
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event(_incomplete_identity_event("req-incomplete", label))

    def forbidden(*_args, **_kwargs):
        pytest.fail("an unbindable reservation must never reach the task store")

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", forbidden
    )
    monkeypatch.setattr(
        process_launcher.task_store, "enqueue_terminal_callback", forbidden
    )

    intent_path = manager._reviewer_terminal_intent_path("req-incomplete")
    ledger_before = len(manager._events())

    # Fails closed: nothing terminalized, no intent recorded, no ledger event.
    assert manager._reconcile_expired_starting_reservations() == 0
    assert not intent_path.exists()
    assert len(manager._events()) == ledger_before

    # ...but the stranded reservation is now named, exactly once.
    diagnostics = _intent_diagnostics(manager)
    assert len(diagnostics) == 1
    assert diagnostics[0]["reason"] == (
        "identity_incomplete:provider_spawn_committed_owner_dead"
    )
    assert diagnostics[0]["intent_file"] == intent_path.name
    assert diagnostics[0]["schema_id"] == (
        process_launcher.REVIEWER_TERMINAL_INTENT_DIAGNOSTIC_SCHEMA_ID
    )

    # Re-evaluated on every pass, reported once: the bound is what makes the
    # diagnostic safe to emit from a loop that never converges.
    for _ in range(5):
        assert manager._reconcile_expired_starting_reservations() == 0
    assert len(_intent_diagnostics(manager)) == 1
    assert len(manager._events()) == ledger_before
    assert not intent_path.exists()


def test_identity_incomplete_diagnostic_separates_distinct_episodes(
    tmp_path: Path,
) -> None:
    # Keying the marker on the request alone would hide a genuinely different
    # episode -- a re-claimed card arriving with a new, still-malformed
    # identity -- behind the first line ever written for that request.
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event(_incomplete_identity_event("req-episodes", "epoch_zero"))
    assert manager._reconcile_expired_starting_reservations() == 0
    assert len(_intent_diagnostics(manager)) == 1

    # The same episode again stays silent...
    assert manager._reconcile_expired_starting_reservations() == 0
    assert len(_intent_diagnostics(manager)) == 1

    # ...while a new identity for the same request earns its own line.
    manager._append_event(_incomplete_identity_event("req-episodes", "epoch_string"))
    assert manager._reconcile_expired_starting_reservations() == 0
    assert len(_intent_diagnostics(manager)) == 2
    assert manager._reconcile_expired_starting_reservations() == 0
    assert len(_intent_diagnostics(manager)) == 2


def test_identity_incomplete_diagnostics_are_bounded_per_reconcile_pass(
    tmp_path: Path, monkeypatch
) -> None:
    # The O_EXCL marker bounds one EPISODE for all time, but a single
    # reconciliation pass can meet arbitrarily many distinct unbindable rows,
    # and each first sighting costs a marker plus a ledger line.  Without a
    # per-pass ceiling one pass emits the whole pile -- so the pass is bounded,
    # and the remainder is retried rather than dropped.
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    cap = process_launcher.ProcessManager._TERMINAL_INTENT_DIAGNOSTICS_PER_PASS
    total = cap + 3
    for index in range(total):
        manager._append_event(
            _committed_reviewer_event(
                f"req-flood-{index:02d}",
                reviewer_claim_epoch=None,
                task_id=f"TASK_FLOOD_{index:02d}",
            )
        )

    def forbidden(*_args, **_kwargs):
        pytest.fail("an unbindable reservation must never reach the task store")

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", forbidden
    )
    monkeypatch.setattr(
        process_launcher.task_store, "enqueue_terminal_callback", forbidden
    )

    ledger_before = len(manager._events())

    # Fails closed for every row, and reports at most ``cap`` of them.
    assert manager._reconcile_expired_starting_reservations() == 0
    assert len(_intent_diagnostics(manager)) == cap
    assert len(manager._events()) == ledger_before

    # An unreported episode kept no marker, so the next pass names the rest
    # instead of them being silently dropped.
    assert manager._reconcile_expired_starting_reservations() == 0
    assert len(_intent_diagnostics(manager)) == total

    # Exactly once per episode: every request appears on the record once, and
    # further passes -- which re-derive the same refusal forever -- add nothing.
    files = [record["intent_file"] for record in _intent_diagnostics(manager)]
    assert len(set(files)) == total
    for _ in range(3):
        assert manager._reconcile_expired_starting_reservations() == 0
    assert len(_intent_diagnostics(manager)) == total
    assert len(manager._events()) == ledger_before
    for index in range(total):
        assert not manager._reviewer_terminal_intent_path(
            f"req-flood-{index:02d}"
        ).exists()


def test_bindable_committed_reservation_records_no_identity_diagnostic(
    tmp_path: Path,
) -> None:
    # The diagnostic names a permanent stranding, so a reservation that
    # terminalizes normally must not produce one: an operator paged for a
    # healthy transition learns to ignore the ledger.
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event(
        _committed_reviewer_event("req-bindable", reviewer_claim_epoch=4)
    )
    assert manager._reconcile_expired_starting_reservations() == 1
    assert manager._reviewer_terminal_intent_path("req-bindable").is_file()
    assert _intent_diagnostics(manager) == []


def test_intent_substatus_outside_store_vocabulary_never_reaches_the_store(
    tmp_path: Path, monkeypatch
) -> None:
    # A substatus the store would refuse is a malformed intent, not a claim to
    # act on.  Validating it here keeps it off SQLite entirely and routes it to
    # the retention path instead of letting a final
    # ``unsupported_terminal_failure`` retire a card that never moved.
    manager, intent_path = _pending_intent(tmp_path, "req-substatus")
    payload = json.loads(intent_path.read_text(encoding="utf-8"))
    payload["substatus"] = "not_a_real_substatus"
    intent_path.write_text(json.dumps(payload), encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        pytest.fail("a malformed substatus must never reach the task store")

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", forbidden
    )
    monkeypatch.setattr(
        process_launcher.task_store, "enqueue_terminal_callback", forbidden
    )

    assert manager._settle_reviewer_terminal_intents() == 0
    # Retained as evidence, exactly like an unbindable identity.
    assert intent_path.is_file()
    diagnostics = _intent_diagnostics(manager)
    assert len(diagnostics) == 1
    # ...but under its OWN reason.  The identity here bound perfectly and only
    # the substatus is unusable, so reusing ``identity_unbindable`` would send
    # an operator to repair task/request/claim-epoch fields that are correct.
    assert diagnostics[0]["reason"] == "substatus_unsupported"
    assert diagnostics[0]["intent_file"] == intent_path.name
    for _ in range(3):
        assert manager._settle_reviewer_terminal_intents() == 0
    assert len(_intent_diagnostics(manager)) == 1


@pytest.mark.parametrize(
    "substatus", sorted(process_launcher.task_store.MARK_TERMINAL_FAILURE_SUBSTATUSES)
)
def test_every_declared_substatus_still_reaches_the_store(
    tmp_path: Path, monkeypatch, substatus: str
) -> None:
    # The new guard must gate malformed values only: every substatus the store
    # itself declares has to pass straight through to the transition.
    manager, intent_path = _pending_intent(tmp_path, "req-vocab")
    payload = json.loads(intent_path.read_text(encoding="utf-8"))
    payload["substatus"] = substatus
    intent_path.write_text(json.dumps(payload), encoding="utf-8")

    seen: list[str] = []

    def mark(*_args, **kwargs):
        seen.append(kwargs["substatus"])
        return True, "blocked"

    monkeypatch.setattr(process_launcher.task_store, "mark_terminal_failure", mark)
    monkeypatch.setattr(
        process_launcher.task_store,
        "enqueue_terminal_callback",
        lambda *_a, **_k: True,
    )

    assert manager._settle_reviewer_terminal_intents() == 1
    assert seen == [substatus]
    assert not intent_path.exists()
    assert _intent_diagnostics(manager) == []


def test_contained_settlement_failure_is_named_once_per_exception_type(
    tmp_path: Path, monkeypatch
) -> None:
    # The contained catch must keep returning 0 -- the caller's launch outcome
    # may not be replaced by an unrelated reservation error -- but 0 is also
    # what "nothing was pending" returns, so a settler that can never run
    # would otherwise look idle forever.
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager.process_dir.mkdir(parents=True, exist_ok=True)

    def boom(self):
        raise RuntimeError("settler is broken")

    monkeypatch.setattr(
        process_launcher.ProcessManager, "_settle_reviewer_terminal_intents", boom
    )
    # Repeated passes must not grow the ledger while the fault persists.
    for _ in range(3):
        assert manager._settle_reviewer_terminal_intents_contained() == 0
    diagnostics = _intent_diagnostics(manager)
    assert len(diagnostics) == 1
    assert diagnostics[0]["reason"] == "settlement_pass_failed:RuntimeError"
    assert diagnostics[0]["intent_file"] == ""
    assert diagnostics[0]["schema_id"] == (
        process_launcher.REVIEWER_TERMINAL_INTENT_DIAGNOSTIC_SCHEMA_ID
    )

    # A genuinely different fault is still worth one line of its own.
    def other(self):
        raise ValueError("a different fault")

    monkeypatch.setattr(
        process_launcher.ProcessManager, "_settle_reviewer_terminal_intents", other
    )
    assert manager._settle_reviewer_terminal_intents_contained() == 0
    assert {record["reason"] for record in _intent_diagnostics(manager)} == {
        "settlement_pass_failed:RuntimeError",
        "settlement_pass_failed:ValueError",
    }


def test_bool_safe_int_rule_has_exactly_one_authority() -> None:
    # Two copies of this rule drift, and the day they disagree a truthy flag
    # binds a terminal transition or its callback to episode 1.
    assert (
        process_launcher._is_bool_safe_int
        is process_launcher.task_store.is_bool_safe_int
    )
    predicate = process_launcher._is_bool_safe_int
    assert predicate(1) is True
    assert predicate(0) is True
    assert predicate(True) is False
    assert predicate(False) is False
    assert predicate("1") is False
    assert predicate(None) is False


def test_settled_terminal_callback_routes_through_the_public_store_api(
    tmp_path: Path, monkeypatch
) -> None:
    # Settlement owns no second copy of the callback-store access and no
    # private wrapper around it: it hands the store the exact episode it just
    # transitioned and lets the store bind the transition and the thread.
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event(
        _committed_reviewer_event("req-callback", reviewer_claim_epoch=9)
    )
    assert manager._reconcile_expired_starting_reservations() == 1

    calls: list[dict] = []

    def record(root, task_id, **kwargs):
        calls.append({"root": root, "task_id": task_id, **kwargs})
        return True

    def forbidden(*_args, **_kwargs):
        pytest.fail("the launcher must not reach the callback database itself")

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure",
        lambda *_a, **_k: (True, "blocked"),
    )
    monkeypatch.setattr(process_launcher.task_store, "enqueue_terminal_callback", record)
    for name in ("get_task", "_require_ready", "_connect"):
        monkeypatch.setattr(process_launcher.task_store, name, forbidden)

    assert manager._settle_reviewer_terminal_intents() == 1
    assert calls == [{
        "root": manager.repo,
        "task_id": "TASK_B1",
        "substatus": "liveness_lost",
        "request_id": "req-callback",
        "claim_epoch": 9,
    }]
    assert not hasattr(manager, "_enqueue_terminal_callback")


def test_terminal_callback_authority_contains_an_unavailable_store(
    tmp_path: Path,
) -> None:
    # Containment lives in the one authority rather than in each caller: a
    # store that cannot even be opened is reported as "not enqueued" instead of
    # raising over the terminal transition that already succeeded.
    assert process_launcher.task_store.enqueue_terminal_callback(
        tmp_path,
        "TASK_B1",
        substatus="liveness_lost",
        request_id="req-callback",
        claim_epoch=9,
    ) is False


def test_terminal_callback_authority_contains_a_locked_store(
    tmp_path: Path, monkeypatch
) -> None:
    # ``database is locked`` is the ordinary shape of a contended store, and it
    # is a ``sqlite3.Error`` rather than a ``TaskStoreError``.  Letting it
    # escape would make one contended card abort the settlement of every other
    # intent, so it is contained here as "not written yet".
    store = process_launcher.task_store

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "get_task", locked)
    assert store.enqueue_terminal_callback(
        tmp_path,
        "TASK_B1",
        substatus="liveness_lost",
        request_id="req-callback",
        claim_epoch=9,
    ) is False


def _ready_store_with_card(
    root: Path,
    *,
    provider: str = "claude",
    origin_thread_id: str = "thread-42",
    claim_epoch: int = 9,
) -> Path:
    """One initialized canonical store holding exactly one terminalized card.

    The containment regressions above deliberately never reach a real store,
    so the *success* arm of the callback authority -- the single writer of the
    manager wake a settled terminal intent owes -- was never executed by a
    test.  Seeding the card directly is the smallest fixture that gets there:
    the creation API derives identity from a live manager route this suite has
    no business standing up.
    """
    store = process_launcher.task_store
    assert store.initialize_repository(root)["ok"] is True
    db = Path(store.storage_readiness(root).canonical_db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status,"
            " card_json, created_at, updated_at, origin_thread_id)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "TASK_B1",
                "claude_worker_b1",
                "task_mcp",
                "blocked",
                "liveness_lost",
                json.dumps(
                    {"coordinator_provider": provider, "claim_epoch": claim_epoch}
                ),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                origin_thread_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _outbox_rows(db: Path) -> list[dict]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM callback_outbox")]
    finally:
        conn.close()


def test_terminal_callback_authority_writes_exactly_one_bound_row(tmp_path: Path) -> None:
    # Every field this row binds is what routes the wake: the transition tells
    # the manager what happened, and provider/thread/episode/request tell it
    # whose episode it happened to.  A silently unbound field is a callback
    # delivered about somebody else's work, so they are asserted exactly.
    db = _ready_store_with_card(tmp_path)
    store = process_launcher.task_store

    assert store.enqueue_terminal_callback(
        tmp_path,
        "TASK_B1",
        substatus="liveness_lost",
        request_id="req-callback",
        claim_epoch=9,
    ) is True

    rows = _outbox_rows(db)
    assert len(rows) == 1
    assert {
        key: rows[0][key]
        for key in (
            "task_id", "provider", "origin_thread_id",
            "transition", "episode_id", "request_id", "state",
        )
    } == {
        "task_id": "TASK_B1",
        "provider": "claude",
        "origin_thread_id": "thread-42",
        "transition": "blocked",
        "episode_id": "9",
        "request_id": "req-callback",
        "state": "pending",
    }

    # A retried settlement of the same durable intent is owed nothing more.
    # ``False`` here is "already written", and the outbox must be untouched --
    # a second pending row is a second wake for one terminal outcome.
    assert store.enqueue_terminal_callback(
        tmp_path,
        "TASK_B1",
        substatus="liveness_lost",
        request_id="req-callback",
        claim_epoch=9,
    ) is False
    assert _outbox_rows(db) == rows


@pytest.mark.parametrize(
    ("provider", "origin_thread_id", "card_claim_epoch"),
    [
        ("claude", "", 9),
        ("gemini", "thread-42", 9),
        ("claude", "thread-42", 10),
    ],
    ids=["route_missing", "provider_unknown", "episode_mismatch"],
)
def test_terminal_callback_authority_writes_nothing_it_cannot_bind(
    tmp_path: Path, provider: str, origin_thread_id: str, card_claim_epoch: int
) -> None:
    # An unroutable or foreign identity fails closed as "not enqueued" and
    # leaves no row behind: a callback nobody can be sure belongs to this
    # episode is worse than the callback that was never written.
    db = _ready_store_with_card(
        tmp_path,
        provider=provider,
        origin_thread_id=origin_thread_id,
        claim_epoch=card_claim_epoch,
    )
    assert process_launcher.task_store.enqueue_terminal_callback(
        tmp_path,
        "TASK_B1",
        substatus="liveness_lost",
        request_id="req-callback",
        claim_epoch=9,
    ) is False
    assert _outbox_rows(db) == []


def test_terminal_callback_authority_never_wakes_a_manager_with_review_ready(
    tmp_path: Path,
) -> None:
    # A substatus this layer does not recognize must resolve to the ``blocked``
    # failure class a manager has to look at, never to ``review_ready`` -- that
    # would announce unreviewed, failed work as ready to inspect.
    db = _ready_store_with_card(tmp_path)
    assert process_launcher.task_store.enqueue_terminal_callback(
        tmp_path,
        "TASK_B1",
        substatus="not_a_declared_substatus",
        request_id="req-callback",
        claim_epoch=9,
    ) is True
    rows = _outbox_rows(db)
    assert len(rows) == 1
    assert rows[0]["transition"] == "blocked"


def _ready_store_with_settled_card(
    root: Path,
    *,
    request_id: str,
    claim_epoch: int = 9,
    card_claim_epoch: int | None = None,
    provider: str = "claude",
    origin_thread_id: str = "thread-42",
    status: str = "blocked",
    worker_status: str = "liveness_lost",
    claimed_by: str = "",
    launch_request_id: str = "",
) -> Path:
    """An initialized store whose card already records THIS terminal failure.

    That is exactly what a settler crash leaves behind: the transition is
    durable on the card, so every later ``mark_terminal_failure`` can only
    refuse the retry as already applied, and the one callback the intent still
    owes is all that is left to settle.

    ``card_claim_epoch`` (with ``status``/``worker_status``) models the card
    DRIFTING past the episode its recorded failure names -- a recovery that
    re-claimed and released it while the crashed settler still owed its
    callback.  ``claimed_by``/``launch_request_id`` carry that drift all the
    way into a live LATER claim, which is what makes the store refuse the
    retried transition for the epoch rather than for the status.
    ``provider``/``origin_thread_id`` model a callback identity the callback
    store will never route.
    """

    current_epoch = claim_epoch if card_claim_epoch is None else card_claim_epoch
    db = _ready_store_with_card(
        root,
        provider=provider,
        origin_thread_id=origin_thread_id,
        claim_epoch=current_epoch,
    )
    card: dict = {
        "coordinator_provider": provider,
        "claim_epoch": current_epoch,
        "terminal_failure": {
            "runner": "claude_worker_b1",
            "substatus": "liveness_lost",
            "claim_epoch": claim_epoch,
            "evidence": {"request_id": request_id},
        },
    }
    if launch_request_id:
        card["launch_request_id"] = launch_request_id
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE tasks SET status=?, worker_status=?, claimed_by=?, card_json=?"
            " WHERE task_id=?",
            (
                status,
                worker_status,
                claimed_by or None,
                json.dumps(card),
                "TASK_B1",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _crashed_settler(tmp_path: Path, request_id: str) -> tuple:
    """Compose the real settler with a real store in the exact crash window.

    The callback is durably enqueued and the intent that owes it was never
    retired -- the state the process is in when it dies between those two
    steps.
    """

    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    db = _ready_store_with_settled_card(manager.repo, request_id=request_id)
    manager._append_event(
        _committed_reviewer_event(request_id, reviewer_claim_epoch=9)
    )
    assert manager._reconcile_expired_starting_reservations() == 1
    intent_path = manager._reviewer_terminal_intent_path(request_id)
    assert intent_path.is_file()
    assert process_launcher.task_store.enqueue_terminal_callback(
        manager.repo,
        "TASK_B1",
        substatus="liveness_lost",
        request_id=request_id,
        claim_epoch=9,
    ) is True
    return manager, db, intent_path


def test_an_already_durable_callback_retires_its_intent_and_adds_no_row(
    tmp_path: Path,
) -> None:
    # The exactly-once gap this closes.  ``enqueue_terminal_callback`` answers
    # False for a duplicate exactly as it does for an unwritable store, so a
    # settler that crashed after enqueueing meets that same refusal on every
    # later scan.  Reading it as "not written yet" strands this intent -- and
    # the processing claim behind it -- forever, which is why the scan below
    # has to retire it while leaving the outbox untouched.
    manager, db, intent_path = _crashed_settler(tmp_path, "req-durable")
    durable = _outbox_rows(db)
    assert len(durable) == 1
    assert durable[0]["request_id"] == "req-durable"
    assert durable[0]["state"] == "pending"

    assert manager._settle_reviewer_terminal_intents() == 1
    assert not intent_path.exists()
    # One terminal outcome owes one manager wake: a second pending row here
    # would be a second wake for the same episode.
    assert _outbox_rows(db) == durable

    # Repeated scans have nothing left to settle and still add nothing.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert _outbox_rows(db) == durable


def test_concurrent_scans_retire_one_durable_intent_exactly_once(
    tmp_path: Path,
) -> None:
    # The same crash window, reached by four scans at once: the ticket is
    # claimed under one request lock, so exactly one of them may report a
    # settlement and none of them may write a second row.
    manager, db, intent_path = _crashed_settler(tmp_path, "req-durable-race")
    durable = _outbox_rows(db)
    assert len(durable) == 1

    start = threading.Barrier(4)
    settled: list[int] = []
    guard = threading.Lock()

    def scan() -> None:
        start.wait(timeout=30)
        result = manager._settle_reviewer_terminal_intents()
        with guard:
            settled.append(result)

    threads = [threading.Thread(target=scan) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)

    assert sum(settled) == 1, settled
    assert not intent_path.exists()
    assert _outbox_rows(db) == durable


def test_the_durable_callback_proof_binds_this_exact_identity(
    tmp_path: Path,
) -> None:
    # The proof is what retires a ticket, so it must never answer for anybody
    # else's callback.  The exact identity the intent bound is proven; a
    # foreign episode or transition fails closed and keeps its own intent for
    # a pass that really is owed one.
    db = _ready_store_with_settled_card(tmp_path, request_id="req-durable")
    durable = process_launcher.task_store.terminal_callback_already_durable
    assert process_launcher.task_store.enqueue_terminal_callback(
        tmp_path,
        "TASK_B1",
        substatus="liveness_lost",
        request_id="req-durable",
        claim_epoch=9,
    ) is True
    assert len(_outbox_rows(db)) == 1

    assert durable(
        tmp_path,
        "TASK_B1",
        substatus="liveness_lost",
        request_id="req-durable",
        claim_epoch=9,
    ) is True
    for foreign in (
        {"request_id": "req-durable", "claim_epoch": 8, "substatus": "liveness_lost"},
        {"request_id": "req-durable", "claim_epoch": 9, "substatus": "exited"},
        {"request_id": "req-durable", "claim_epoch": True, "substatus": "liveness_lost"},
    ):
        assert durable(tmp_path, "TASK_B1", **foreign) is False
        assert durable(tmp_path, "TASK_OTHER", **foreign) is False
    # The one field the proof deliberately does NOT narrow on is request_id,
    # because the store's dedup key does not carry it either: the row above
    # occupies (task, provider, route, transition, episode) for good, so a
    # sibling request in this same episode can never enqueue again.  Proving
    # a narrower identity than the enqueue refuses on is what turns that
    # permanent refusal into an endless retry, so the two agree exactly.
    assert durable(
        tmp_path,
        "TASK_B1",
        substatus="liveness_lost",
        request_id="req-other",
        claim_epoch=9,
    ) is True
    # Aligning on request_id weakens nothing else: a foreign TASK still fails
    # closed on the same call, and every other component of the key is exact.
    assert durable(
        tmp_path,
        "TASK_OTHER",
        substatus="liveness_lost",
        request_id="req-other",
        claim_epoch=9,
    ) is False
    # Proving nothing must never be mistaken for proving something, so an
    # unreadable store raises here instead of resolving to "already sent".
    with pytest.raises(process_launcher.task_store.TaskStoreError):
        durable(
            tmp_path / "not-a-repository",
            "TASK_B1",
            substatus="liveness_lost",
            request_id="req-durable",
            claim_epoch=9,
        )


def _dead_letter_the_only_row(db: Path, *, last_error: str) -> None:
    """Move the single outbox row to ``dead_letter`` with an exact reason."""

    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE callback_outbox SET state='dead_letter', last_error=?",
            (last_error,),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_dead_lettered_callback_settles_once_instead_of_retrying_forever(
    tmp_path: Path,
) -> None:
    # A row that really was enqueued, became deliverable work, and only later
    # exhausted delivery still occupies the store's state-agnostic dedup key
    # forever.  So the enqueue refuses permanently -- and if the durable proof
    # refused with it, this intent (and the processing claim behind it) would
    # retry on every pass for the rest of time.  One bounded truthful
    # settlement, no duplicate row, no silent retry.
    manager, db, intent_path = _crashed_settler(tmp_path, "req-deadletter")
    enqueued = _outbox_rows(db)
    assert len(enqueued) == 1
    assert enqueued[0]["state"] == "pending"

    _dead_letter_the_only_row(db, last_error="delivery_exhausted:http_502")

    # The enqueue is refused for good: the key is taken and state is not part
    # of it, so no later pass could ever produce a replacement row.
    assert process_launcher.task_store.enqueue_terminal_callback(
        manager.repo,
        "TASK_B1",
        substatus="liveness_lost",
        request_id="req-deadletter",
        claim_epoch=9,
    ) is False
    assert len(_outbox_rows(db)) == 1

    assert manager._settle_reviewer_terminal_intents() == 1
    assert not intent_path.exists()
    after = _outbox_rows(db)
    assert len(after) == 1, "settling must never add a second row for one episode"
    assert after[0]["state"] == "dead_letter"

    # And the retired ticket brings no later pass back to this claim.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert _outbox_rows(db) == after


def test_a_never_deliverable_dead_letter_never_retires_an_intent(
    tmp_path: Path,
) -> None:
    # The OTHER row wearing ``dead_letter``: the one ``enqueue_callback``
    # writes INSTEAD of enqueuing, when it rejects the row as malformed.  That
    # callback never became deliverable work, so retiring an intent against it
    # would drop the manager wake in total silence.  It names itself in
    # ``last_error`` and the proof refuses it.
    manager, db, intent_path = _crashed_settler(tmp_path, "req-malformed")
    assert len(_outbox_rows(db)) == 1

    _dead_letter_the_only_row(
        db,
        last_error=(
            process_launcher.task_store
            .TERMINAL_CALLBACK_NEVER_DELIVERABLE_ERROR_PREFIX
            + "request_id_too_long"
        ),
    )

    assert process_launcher.task_store.terminal_callback_already_durable(
        manager.repo,
        "TASK_B1",
        substatus="liveness_lost",
        request_id="req-malformed",
        claim_epoch=9,
    ) is False
    # Nothing is settled and the ticket is kept, so the wake stays owed and
    # visible instead of being retired against a row that never carried it.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert intent_path.is_file()
    assert len(_outbox_rows(db)) == 1


def _ready_store_with_processing_card(
    root: Path,
    *,
    request_id: str,
    claim_epoch: int = 9,
    provider: str = "claude",
    origin_thread_id: str = "thread-42",
) -> Path:
    """A real claimed card every transition guard has to be crossed against.

    ``mark_terminal_failure`` checks the runner, the canonical ``processing``
    status, the claim owner, the card's ``launch_request_id`` and its
    ``claim_epoch`` against this exact row.  The other real-store fixtures
    here start from a card that is ALREADY terminalized, so those guards are
    only ever met by their refusal arm; this one is what a settlement has to
    pass through on the way to a first, successful transition.
    """

    store = process_launcher.task_store
    assert store.initialize_repository(root)["ok"] is True
    db = Path(store.storage_readiness(root).canonical_db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status,"
            " card_json, created_at, updated_at, origin_thread_id, claimed_by)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "TASK_B1",
                "claude_worker_b1",
                "task_mcp",
                "processing",
                "running",
                json.dumps({
                    "coordinator_provider": provider,
                    "claim_epoch": claim_epoch,
                    "launch_request_id": request_id,
                }),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                origin_thread_id,
                "claude_worker_b1",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_a_real_store_settlement_crosses_every_transition_guard(
    tmp_path: Path,
) -> None:
    # End to end with NOTHING stubbed: a live processing claim, the real
    # transition, and the real enqueue.  The happy path has to cross runner,
    # claim owner, claim epoch and launch_request identity against one actual
    # row, and land exactly one manager wake naming the exact episode.
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    db = _ready_store_with_processing_card(manager.repo, request_id="req-happy")
    manager._append_event(
        _committed_reviewer_event("req-happy", reviewer_claim_epoch=9)
    )
    assert manager._reconcile_expired_starting_reservations() == 1
    intent_path = manager._reviewer_terminal_intent_path("req-happy")
    assert intent_path.is_file()

    assert manager._settle_reviewer_terminal_intents() == 1
    assert not intent_path.exists()

    card = process_launcher.task_store.get_task(manager.repo, "TASK_B1") or {}
    assert card["status"] == "blocked"
    recorded = card["terminal_failure"]
    assert recorded["substatus"] == "liveness_lost"
    assert recorded["runner"] == "claude_worker_b1"
    assert recorded["claim_epoch"] == 9
    assert recorded["evidence"]["request_id"] == "req-happy"
    assert recorded["evidence"]["reviewer_claim_epoch"] == 9

    rows = _outbox_rows(db)
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"
    assert rows[0]["task_id"] == "TASK_B1"
    assert rows[0]["provider"] == "claude"
    assert rows[0]["origin_thread_id"] == "thread-42"
    assert rows[0]["request_id"] == "req-happy"
    assert str(rows[0]["episode_id"]) == "9"

    # Exactly once: the ticket is retired, so a repeated pass settles nothing
    # and the manager is never woken twice for one terminal outcome.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert _outbox_rows(db) == rows


_APPLIED_INTENT = {
    "runner": "claude_worker_b1",
    "substatus": "liveness_lost",
    "request_id": "req-callback",
    "claim_epoch": 9,
}


def test_terminal_failure_already_applied_matches_only_this_exact_intent(
    tmp_path: Path, monkeypatch
) -> None:
    # Post-transition recovery must never fabricate a callback for somebody
    # else's terminal outcome: the recorded failure has to name this runner,
    # this substatus, this episode and this request.
    store = process_launcher.task_store
    recorded = {
        "runner": "claude_worker_b1",
        "substatus": "liveness_lost",
        "claim_epoch": 9,
        "evidence": {"request_id": "req-callback"},
    }
    card: dict = {"claim_epoch": 9, "terminal_failure": recorded}
    monkeypatch.setattr(store, "get_task", lambda *_a, **_k: card)

    applied = store.terminal_failure_already_applied
    state = "not_processing:current=blocked"
    assert applied(tmp_path, "TASK_B1", state, **_APPLIED_INTENT) is True

    for key, foreign in (
        ("runner", "someone_else"),
        ("substatus", "worker_failed"),
        ("claim_epoch", 10),
    ):
        card["terminal_failure"] = {**recorded, key: foreign}
        assert applied(tmp_path, "TASK_B1", state, **_APPLIED_INTENT) is False

    # A recorded ``True`` must not be read as episode 1 by the comparison.
    card["terminal_failure"] = {**recorded, "claim_epoch": True}
    assert applied(
        tmp_path, "TASK_B1", state, **{**_APPLIED_INTENT, "claim_epoch": 1}
    ) is False
    card["terminal_failure"] = {
        **recorded, "evidence": {"request_id": "another-request"},
    }
    assert applied(tmp_path, "TASK_B1", state, **_APPLIED_INTENT) is False

    # A card that records no terminal failure at all never counts as ours.
    card["terminal_failure"] = None
    assert applied(tmp_path, "TASK_B1", state, **_APPLIED_INTENT) is False


def test_terminal_failure_already_applied_reads_no_card_for_other_refusals(
    tmp_path: Path, monkeypatch
) -> None:
    # A refusal only says why THIS attempt was rejected now.  The ones a
    # recover/re-claim can produce are decided from the card; the rest are
    # pre-store or not-found guards that prove no attempt of this caller's ever
    # reached the card, so they are answered without opening the store at all.
    store = process_launcher.task_store

    def forbidden(*_args, **_kwargs):
        pytest.fail("a refusal that proves nothing landed must not read a card")

    monkeypatch.setattr(store, "get_task", forbidden)
    applied = store.terminal_failure_already_applied
    for state in (
        "task_not_found",
        "expected_claim_epoch_invalid",
        "unsupported_terminal_failure:not_a_real_substatus",
        "terminal_failure_transition_conflict",
        "blocked",
        "",
    ):
        assert applied(tmp_path, "TASK_B1", state, **_APPLIED_INTENT) is False

    # ``True`` is an ``int`` subclass; it must never bind episode 1.  The epoch
    # is validated before any card read, so this holds for every refusal that
    # would otherwise open the store.
    for state in (
        "not_processing:current=blocked",
        "claim_epoch_mismatch:expected=9:current=10",
        "claim_owner_mismatch",
    ):
        for bad in (True, False, "9", 9.0):
            assert applied(
                tmp_path, "TASK_B1", state, **{**_APPLIED_INTENT, "claim_epoch": bad}
            ) is False


@pytest.mark.parametrize(
    "state",
    [
        "not_processing:current=blocked",
        "claim_epoch_mismatch:expected=9:current=11",
        "claim_owner_mismatch",
        "runner_mismatch",
        "launch_request_mismatch",
        "launch_request_id_required",
    ],
)
def test_every_reclaim_refusal_is_decided_by_the_card_not_the_string(
    tmp_path: Path, monkeypatch, state: str
) -> None:
    # The crash window is always the same one: the transition is durable and
    # the callback it owes is not.  Which refusal the retry meets depends only
    # on how far the card moved on -- to a later episode, another owner, a new
    # launch request.  Reading any of them as "nothing landed" retires an owed
    # manager wake, so all of them consult the card, and the card's own record
    # is what decides.
    store = process_launcher.task_store
    recorded = {
        "runner": "claude_worker_b1",
        "substatus": "liveness_lost",
        "claim_epoch": 9,
        "evidence": {"request_id": "req-callback"},
    }
    # The card has since been re-claimed into a later episode; the record it
    # still carries names THIS caller's episode 9.
    card: dict = {"claim_epoch": 11, "terminal_failure": recorded}
    monkeypatch.setattr(store, "get_task", lambda *_a, **_k: card)

    applied = store.terminal_failure_already_applied
    assert applied(tmp_path, "TASK_B1", state, **_APPLIED_INTENT) is True

    # A record naming the LATER episode is somebody else's outcome, never this
    # caller's, so the same refusal now answers False and the intent is spent.
    card["terminal_failure"] = {**recorded, "claim_epoch": 11}
    assert applied(tmp_path, "TASK_B1", state, **_APPLIED_INTENT) is False
    card["terminal_failure"] = None
    assert applied(tmp_path, "TASK_B1", state, **_APPLIED_INTENT) is False


def test_mark_terminal_failure_rejects_a_non_integer_claim_epoch(
    tmp_path: Path,
) -> None:
    # ``True`` is an ``int`` subclass; accepting it would let a truthy value
    # masquerade as claim epoch 1 and terminalize the wrong claim.  The guard
    # fails closed before any store access, so no repository is required.
    for bad in (True, False, "3", 3.0):
        ok, state = process_launcher.task_store.mark_terminal_failure(
            tmp_path,
            "TASK_B1",
            runner="claude_worker_b1",
            substatus="liveness_lost",
            claim_epoch=bad,
        )
        assert ok is False
        assert state == "expected_claim_epoch_invalid"


def test_mark_terminal_failure_rejects_an_unsupported_substatus(
    tmp_path: Path,
) -> None:
    ok, state = process_launcher.task_store.mark_terminal_failure(
        tmp_path,
        "TASK_B1",
        runner="claude_worker_b1",
        substatus="not_a_real_substatus",
        claim_epoch=3,
    )
    assert ok is False
    assert state == "unsupported_terminal_failure:not_a_real_substatus"


def _reclaimed_settled_store(root: Path, *, request_id: str) -> Path:
    """The crash window, met again after the card was recovered and re-claimed.

    The settler's own transition is still recorded against episode 9, and the
    card has since moved on to episode 11 and been released back to pending.
    That drift is the whole point: the callback is still owed for episode 9
    and nothing on the current card names it.
    """

    return _ready_store_with_settled_card(
        root,
        request_id=request_id,
        claim_epoch=9,
        card_claim_epoch=11,
        status="pending",
        worker_status="unclaimed",
    )


def test_a_recovered_and_reclaimed_card_still_settles_its_recorded_episode(
    tmp_path: Path,
) -> None:
    # Epoch drift after recover/re-claim.  The card proves this settler's own
    # transition landed, so the callback stays owed -- but binding the enqueue
    # and the durable proof to the card's CURRENT epoch refuses both forever,
    # and the intent (with the claim behind it) can never retire.
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    db = _reclaimed_settled_store(manager.repo, request_id="req-reclaimed")
    manager._append_event(
        _committed_reviewer_event("req-reclaimed", reviewer_claim_epoch=9)
    )
    assert manager._reconcile_expired_starting_reservations() == 1
    intent_path = manager._reviewer_terminal_intent_path("req-reclaimed")
    assert intent_path.is_file()

    store = process_launcher.task_store
    # The retry is refused because the card is no longer this settler's to
    # move, and the card itself is what proves the transition already landed.
    ok, state = store.mark_terminal_failure(
        manager.repo,
        "TASK_B1",
        runner="claude_worker_b1",
        substatus="liveness_lost",
        request_id="req-reclaimed",
        claim_epoch=9,
    )
    assert ok is False
    assert state.startswith("not_processing:")
    assert store.terminal_failure_already_applied(
        manager.repo,
        "TASK_B1",
        state,
        runner="claude_worker_b1",
        substatus="liveness_lost",
        request_id="req-reclaimed",
        claim_epoch=9,
    ) is True

    assert manager._settle_reviewer_terminal_intents() == 1
    assert not intent_path.exists()
    rows = _outbox_rows(db)
    assert len(rows) == 1
    # The wake names the episode the transition moved, never the later claim.
    assert rows[0]["episode_id"] == "9"
    assert rows[0]["request_id"] == "req-reclaimed"
    assert rows[0]["state"] == "pending"

    # Nothing is left to settle, and no second wake for one terminal outcome.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert _outbox_rows(db) == rows


def _reclaimed_processing_store(root: Path, *, request_id: str) -> Path:
    """The crash window met again after the card was re-claimed and is LIVE.

    The settler's own transition is still recorded against episode 9, and the
    card has since been recovered into episode 11 and re-claimed by the same
    runner under a new launch request.  Nothing about the STATUS refuses the
    retry any more, so the store refuses it for the epoch instead -- which is
    the refusal a settler must not read as "this transition never happened".
    """

    return _ready_store_with_settled_card(
        root,
        request_id=request_id,
        claim_epoch=9,
        card_claim_epoch=11,
        status="processing",
        worker_status="claimed",
        claimed_by="claude_worker_b1",
        launch_request_id=request_id,
    )


def test_a_reclaim_before_the_callback_never_retires_the_wake_it_owes(
    tmp_path: Path,
) -> None:
    # The exact hidden re-claim.  This settler's transition landed and it died
    # owing one callback; by the time it runs again the card is live under a
    # LATER episode, so the retry is refused with ``claim_epoch_mismatch``
    # rather than ``not_processing``.  Deciding from the refusal string retires
    # the ticket, and the manager is never woken about work that really did
    # terminate.
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    db = _reclaimed_processing_store(manager.repo, request_id="req-reclaim-live")
    manager._append_event(
        _committed_reviewer_event("req-reclaim-live", reviewer_claim_epoch=9)
    )
    assert manager._reconcile_expired_starting_reservations() == 1
    intent_path = manager._reviewer_terminal_intent_path("req-reclaim-live")
    assert intent_path.is_file()

    store = process_launcher.task_store
    ok, state = store.mark_terminal_failure(
        manager.repo,
        "TASK_B1",
        runner="claude_worker_b1",
        substatus="liveness_lost",
        request_id="req-reclaim-live",
        claim_epoch=9,
    )
    assert ok is False
    assert state.startswith("claim_epoch_mismatch:")
    # The CARD, not the refusal string, is what proves the transition landed.
    assert store.terminal_failure_already_applied(
        manager.repo,
        "TASK_B1",
        state,
        runner="claude_worker_b1",
        substatus="liveness_lost",
        request_id="req-reclaim-live",
        claim_epoch=9,
    ) is True

    assert manager._settle_reviewer_terminal_intents() == 1
    assert not intent_path.exists()
    rows = _outbox_rows(db)
    assert len(rows) == 1
    # The wake names episode 9 -- the one the transition actually moved --
    # never the live episode 11 that re-claimed the card afterwards.
    assert rows[0]["episode_id"] == "9"
    assert rows[0]["request_id"] == "req-reclaim-live"
    assert rows[0]["state"] == "pending"

    # Idempotent: a second scan owes nothing and adds no second wake, and the
    # live later claim is left exactly where its own owner put it.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert _outbox_rows(db) == rows
    reclaimed = store.get_task(manager.repo, "TASK_B1") or {}
    assert reclaimed.get("claim_epoch") == 11


def test_a_recorded_episode_settles_only_the_intent_that_recorded_it(
    tmp_path: Path,
) -> None:
    # The recorded episode is a second authority, so it must be exactly as
    # narrow as the card's own evidence: a foreign request, a foreign episode
    # or a foreign substatus may never settle -- or prove -- a callback for
    # somebody else's terminal outcome just because the card drifted.
    db = _reclaimed_settled_store(tmp_path, request_id="req-reclaimed")
    store = process_launcher.task_store

    for foreign in (
        {"request_id": "req-other", "claim_epoch": 9, "substatus": "liveness_lost"},
        {"request_id": "req-reclaimed", "claim_epoch": 8, "substatus": "liveness_lost"},
        {"request_id": "req-reclaimed", "claim_epoch": 9, "substatus": "exited"},
    ):
        assert store.enqueue_terminal_callback(tmp_path, "TASK_B1", **foreign) is False
        assert store.terminal_callback_already_durable(
            tmp_path, "TASK_B1", **foreign
        ) is False
    assert _outbox_rows(db) == []

    exact = {
        "substatus": "liveness_lost",
        "request_id": "req-reclaimed",
        "claim_epoch": 9,
    }
    assert store.enqueue_terminal_callback(tmp_path, "TASK_B1", **exact) is True
    rows = _outbox_rows(db)
    assert len(rows) == 1
    assert rows[0]["episode_id"] == "9"
    assert store.terminal_callback_already_durable(
        tmp_path, "TASK_B1", **exact
    ) is True
    # And the retry of that exact settlement is still a no-op, not a second row.
    assert store.enqueue_terminal_callback(tmp_path, "TASK_B1", **exact) is False
    assert _outbox_rows(db) == rows


@pytest.mark.parametrize(
    ("provider", "origin_thread_id", "reason"),
    [
        ("claude", "", "origin_thread_unbound"),
        ("gemini", "thread-42", "provider_unroutable"),
    ],
    ids=["empty_origin", "unknown_provider"],
)
def test_an_unroutable_callback_identity_retires_with_a_named_disposition(
    tmp_path: Path, provider: str, origin_thread_id: str, reason: str
) -> None:
    # The third world behind ``enqueue_terminal_callback``'s ``False``.  The
    # terminal failure is committed and the callback identity is one the store
    # refuses structurally, so neither the enqueue nor the durable proof can
    # EVER succeed.  Reading that as "not written yet" retries the same refusal
    # on every scan for the life of the process and never retires the intent.
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: _card()),
        argv=[sys.executable, "-c", "pass"],
    )
    db = _ready_store_with_settled_card(
        manager.repo,
        request_id="req-unroutable",
        provider=provider,
        origin_thread_id=origin_thread_id,
    )
    manager._append_event(
        _committed_reviewer_event("req-unroutable", reviewer_claim_epoch=9)
    )
    assert manager._reconcile_expired_starting_reservations() == 1
    intent_path = manager._reviewer_terminal_intent_path("req-unroutable")
    assert intent_path.is_file()

    assert (
        process_launcher.task_store.terminal_callback_identity_unroutable(
            manager.repo, "TASK_B1", substatus="liveness_lost"
        )
        == reason
    )

    assert manager._settle_reviewer_terminal_intents() == 1
    assert not intent_path.exists()
    # The manager wake really is lost, so the disposition is named rather than
    # the ticket being retired in silence.
    assert [record["reason"] for record in _intent_diagnostics(manager)] == [
        f"callback_unroutable:{reason}"
    ]
    assert _outbox_rows(db) == []

    # And no later scan comes back to it: the silent infinite retry is gone.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert len(_intent_diagnostics(manager)) == 1
    assert _outbox_rows(db) == []


def test_a_routable_identity_never_takes_the_unroutable_disposition(
    tmp_path: Path,
) -> None:
    # The escape hatch has to stay shut for every ordinary card, or a merely
    # contended store would retire an intent whose callback is still owed.
    store = process_launcher.task_store
    _ready_store_with_settled_card(tmp_path, request_id="req-routable")
    assert store.terminal_callback_identity_unroutable(
        tmp_path, "TASK_B1", substatus="liveness_lost"
    ) == ""
    # Proving nothing must never be mistaken for proving "can never route", so
    # an unreadable store raises here instead of resolving to a retirement.
    with pytest.raises(store.TaskStoreError):
        store.terminal_callback_identity_unroutable(
            tmp_path / "not-a-repository", "TASK_B1", substatus="liveness_lost"
        )


# ── NF430 worker temp launch env ───────────────────────────────────────────


def test_worker_launch_env_routes_tmpdir_at_request_owned_authority(tmp_path):
    """NF430: worker_launch_env overlays TMPDIR/TMP/TEMP at the request-owned
    ``.aiworkhub/temp/worker/<request_id>/tmp`` authority -- provisioned 0700
    and outside the candidate worktree -- while preserving the sanitized env.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    request_id = "req-nf430-a1b2c3"
    env = process_launcher.worker_launch_env(
        "claude_cli", repo=repo, request_id=request_id
    )
    tmp = Path(env["TMPDIR"])
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"]
    # The exact request-owned repository-local authority, never shared /tmp.
    assert tmp.name == "tmp"
    assert tmp.parent.name == request_id
    parts = tmp.parts
    assert ".aiworkhub" in parts and "temp" in parts and "worker" in parts
    assert "worktree" not in parts
    # Provisioned before spawn: a real 0700 directory the child can write to.
    assert tmp.is_dir()
    assert tmp.stat().st_mode & 0o777 == 0o700
    # The sanitized allowlist is untouched: a request-scoped HOME survives and
    # launch/credential authority never leaks into the child.
    assert env["HOME"]
    assert "AIWORKHUB_ALLOW_LAUNCH" not in env
    assert "AIWORKHUB_ALLOW_WRITES" not in env


def test_worker_launch_env_is_collision_free_and_applies_provider_env(tmp_path):
    """NF430: distinct requests get distinct temp roots, and the explicit BYOK
    provider env is still merged exactly as sanitized_env does."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env_a = process_launcher.worker_launch_env(
        "claude_cli", repo=repo, request_id="req-a"
    )
    env_b = process_launcher.worker_launch_env(
        "claude_cli", repo=repo, request_id="req-b"
    )
    assert env_a["TMPDIR"] != env_b["TMPDIR"]
    assert "req-a" in env_a["TMPDIR"] and "req-b" in env_b["TMPDIR"]
    env_p = process_launcher.worker_launch_env(
        "deepseek_copilot_cli",
        repo=repo,
        request_id="req-provider",
        provider_env={"COPILOT_PROVIDER_API_KEY": "byok-secret"},
    )
    assert env_p["COPILOT_PROVIDER_API_KEY"] == "byok-secret"
    # The provider env never shadows the request-owned temp keys.
    assert Path(env_p["TMPDIR"]).parent.name == "req-provider"


def _quality_reviewer_launch_harness(tmp_path, monkeypatch):
    cards: dict[str, dict] = {}
    order: list[str] = []
    started: list[object] = []

    def show(task_id: str):
        stored = cards.get(str(task_id))
        if isinstance(stored, dict) and stored.get("topic") == "quality_review":
            return {
                "returncode": 0,
                "stdout": json.dumps(stored),
                "stderr": "",
            }
        card = {
            "task_id": task_id,
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "status": "review",
            "worker_status": "review",
            "terminal_review": {"substatus": "review_ready"},
            "allowed_writes": [],
            "priority": "high",
        }
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    manager = _manager(
        tmp_path,
        show_task=show,
        argv=[sys.executable, "-c", "pass"],
    )
    monkeypatch.setattr(
        process_launcher.quality_review,
        "assess_reviewer_launch_target",
        lambda **_k: {
            "can_launch": True,
            "fails_at_launch": False,
            "reason": "reviewer_target_review_ready",
            "target_substatus": "review_ready",
        },
    )

    def fake_create(**kwargs):
        order.append("create")
        card = {
            "task_id": kwargs["task_id"],
            "runner": kwargs["runner"],
            "topic": kwargs["topic"],
            "read_only": kwargs.get("read_only", False),
            "allowed_writes": list(kwargs.get("allowed_writes") or []),
            "status": "pending",
            "worker_status": "unclaimed",
        }
        cards[str(kwargs["task_id"])] = card
        return {"ok": True, "created": True, "task_id": kwargs["task_id"]}

    def fake_claim(repo, task_id, runner, topic, request_id=""):
        order.append("claim")
        existing = cards.get(str(task_id), {})
        card = {
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "launch_request_id": request_id,
            "claim_epoch": 1,
            "status": "processing",
            "worker_status": "claimed",
            "claimed_by": runner,
            "read_only": existing.get("read_only", True),
            "allowed_writes": list(existing.get("allowed_writes") or []),
        }
        cards[str(task_id)] = card
        return {
            "ok": True,
            "returncode": 0,
            "stdout": json.dumps(card),
            "stderr": "",
        }

    class FakeThread:
        def __init__(self, target=None, kwargs=None, name=None, daemon=None):
            self.target = target
            self.kwargs = kwargs or {}
            self.name = name
            started.append(self)

        def start(self):
            order.append("thread")

    monkeypatch.setattr(process_launcher.core, "create_task", fake_create)
    monkeypatch.setattr(process_launcher.task_engine, "claim_start_exact", fake_claim)
    monkeypatch.setattr(process_launcher.threading, "Thread", FakeThread)
    return manager, cards, order, started


def test_quality_reviewer_launch_ack_requires_visible_claimed_card(
    tmp_path, monkeypatch,
):
    manager, cards, order, started = _quality_reviewer_launch_harness(
        tmp_path, monkeypatch
    )
    receipt = manager.launch_quality_reviewer(
        target_request_id="target-req-1",
        target_task_id="TARGET_TASK_1",
        reviewer_task_id="REVIEWER_TASK_1",
        runner="claude_worker_reviewer",
        adapter_id="claude_cli",
        lens="correctness",
    )
    assert receipt["ok"] is True
    assert receipt["request_id"]
    assert receipt["task_id"] == "REVIEWER_TASK_1"
    assert receipt["launch_request_id"] == receipt["request_id"]
    assert cards["REVIEWER_TASK_1"]["launch_request_id"] == receipt["request_id"]
    assert cards["REVIEWER_TASK_1"]["status"] == "processing"
    assert cards["REVIEWER_TASK_1"]["worker_status"] == "claimed"
    assert order[:2] == ["create", "claim"]
    assert order.index("create") < order.index("thread")
    assert len(started) == 1
    latest = manager._latest_by_request()[receipt["request_id"]]
    assert latest["task_id"] == "REVIEWER_TASK_1"
    assert latest["state"] == "starting"


def test_quality_reviewer_launch_create_failure_terminalizes_without_ack(
    tmp_path, monkeypatch,
):
    manager, _cards, order, started = _quality_reviewer_launch_harness(
        tmp_path, monkeypatch
    )

    def fail_create(**_kwargs):
        order.append("create")
        return {"ok": False, "stderr": "manager_identity_required:task_create"}

    monkeypatch.setattr(process_launcher.core, "create_task", fail_create)
    receipt = manager.launch_quality_reviewer(
        target_request_id="target-req-1",
        target_task_id="TARGET_TASK_1",
        reviewer_task_id="REVIEWER_TASK_1",
        runner="claude_worker_reviewer",
        adapter_id="claude_cli",
        lens="correctness",
    )
    assert receipt["ok"] is False
    assert str(receipt.get("error") or "").startswith(
        "quality_review_task_create_failed:"
    )
    assert "thread" not in order
    assert started == []
    blocked = [
        event
        for event in manager._latest_by_request().values()
        if event.get("state") == "blocked"
    ]
    assert len(blocked) == 1
    assert "quality_review_task_create_failed" in str(
        blocked[0].get("blocked_reason") or ""
    )


def test_quality_reviewer_launch_claim_failure_releases_reservation(
    tmp_path, monkeypatch,
):
    manager, _cards, order, started = _quality_reviewer_launch_harness(
        tmp_path, monkeypatch
    )

    def fail_claim(*_a, **_k):
        order.append("claim")
        return {
            "ok": False,
            "returncode": 1,
            "stdout": "",
            "stderr": "claim_conflict:task_id=REVIEWER_TASK_1",
        }

    monkeypatch.setattr(process_launcher.task_engine, "claim_start_exact", fail_claim)
    receipt = manager.launch_quality_reviewer(
        target_request_id="target-req-1",
        target_task_id="TARGET_TASK_1",
        reviewer_task_id="REVIEWER_TASK_1",
        runner="claude_worker_reviewer",
        adapter_id="claude_cli",
        lens="correctness",
    )
    assert receipt["ok"] is False
    assert str(receipt.get("error") or "").startswith("quality_review_claim_failed:")
    assert "thread" not in order
    assert started == []
    blocked = [
        event
        for event in manager._latest_by_request().values()
        if event.get("state") == "blocked"
    ]
    assert len(blocked) == 1
    assert "quality_review_claim_failed" in str(blocked[0].get("blocked_reason") or "")


def test_quality_reviewer_launch_concurrent_same_id_reconciles_one_request(
    tmp_path, monkeypatch,
):
    real_thread = threading.Thread
    manager, cards, _order, started = _quality_reviewer_launch_harness(
        tmp_path, monkeypatch
    )
    barrier = threading.Barrier(2)
    receipts: list[dict | None] = [None, None]

    def run(index: int) -> None:
        barrier.wait()
        receipts[index] = manager.launch_quality_reviewer(
            target_request_id="target-req-1",
            target_task_id="TARGET_TASK_1",
            reviewer_task_id="REVIEWER_TASK_1",
            runner="claude_worker_reviewer",
            adapter_id="claude_cli",
            lens="correctness",
        )

    workers = [
        real_thread(target=run, args=(0,)),
        real_thread(target=run, args=(1,)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    first, second = receipts
    assert first is not None and second is not None
    assert first["ok"] is True and second["ok"] is True
    assert first["request_id"] == second["request_id"]
    assert first["task_id"] == second["task_id"] == "REVIEWER_TASK_1"
    assert cards["REVIEWER_TASK_1"]["launch_request_id"] == first["request_id"]
    assert len(started) == 1
    starting = [
        event
        for event in manager._latest_by_request().values()
        if event.get("task_id") == "REVIEWER_TASK_1" and event.get("state") == "starting"
    ]
    assert len(starting) == 1


def test_quality_reviewer_launch_existing_identical_rereads_safe_card(
    tmp_path, monkeypatch,
):
    manager, cards, order, started = _quality_reviewer_launch_harness(
        tmp_path, monkeypatch
    )
    cards["REVIEWER_TASK_1"] = {
        "task_id": "REVIEWER_TASK_1",
        "runner": "claude_worker_reviewer",
        "topic": "quality_review",
        "read_only": True,
        "allowed_writes": [],
        "status": "pending",
        "worker_status": "unclaimed",
    }

    def existing_create(**_kwargs):
        order.append("create")
        return {
            "ok": False,
            "receipt_state": "existing_identical",
            "reconciled": True,
        }

    monkeypatch.setattr(process_launcher.core, "create_task", existing_create)
    receipt = manager.launch_quality_reviewer(
        target_request_id="target-req-1",
        target_task_id="TARGET_TASK_1",
        reviewer_task_id="REVIEWER_TASK_1",
        runner="claude_worker_reviewer",
        adapter_id="claude_cli",
        lens="correctness",
    )
    assert receipt["ok"] is True
    assert receipt["task_id"] == "REVIEWER_TASK_1"
    assert cards["REVIEWER_TASK_1"]["launch_request_id"] == receipt["request_id"]
    assert order[:2] == ["create", "claim"]
    assert order.index("create") < order.index("thread")
    assert len(started) == 1


def test_quality_reviewer_launch_task_omits_none_reserved_request_id(
    tmp_path, monkeypatch,
):
    _open_gates(monkeypatch)
    seen: list[dict] = []

    def isolated_without_reserved(**kwargs):
        seen.append(kwargs)
        assert "reserved_request_id" not in kwargs
        return {"ok": True, "state": "running", "request_id": "compat-req"}

    manager = _manager(
        tmp_path,
        show_task=_show(_card),
        argv=[sys.executable, "-c", "pass"],
    )
    manager.isolation_enabled = True
    monkeypatch.setattr(manager, "_launch_isolated", isolated_without_reserved)
    result = manager.launch(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
    )
    assert result["ok"] is True
    assert seen and "reserved_request_id" not in seen[0]


def test_quality_reviewer_preflight_omits_none_reserved_request_id(
    tmp_path, monkeypatch,
):
    _open_gates(monkeypatch)
    manager = _manager(
        tmp_path,
        show_task=_show(_card),
        argv=[sys.executable, "-c", "pass"],
    )

    def preflight_without_reserved(task_id, runner, topic, adapter_id):
        raise process_launcher.LaunchRejected("stop-after-compat-preflight")

    monkeypatch.setattr(manager, "_preflight_card", preflight_without_reserved)
    monkeypatch.setattr(
        manager,
        "_blocked",
        lambda task_id, runner, topic, adapter_id, reason, **_k: {
            "ok": False,
            "blocked_reason": reason,
            "task_id": task_id,
        },
    )
    result = manager._launch_isolated(
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        model=None,
        owner_prompt="",
        timeout_seconds=30,
    )
    assert result.get("ok") is False
    assert "stop-after-compat-preflight" in str(result.get("blocked_reason") or "")


def test_quality_reviewer_reserved_launch_handoff_skips_second_claim(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(process_launcher, "chmod_fd", lambda *_a, **_k: None)
    monkeypatch.setattr(process_launcher, "chmod_path", lambda *_a, **_k: None)
    monkeypatch.setattr(os, "chmod", lambda *_a, **_k: None)

    def append_without_chmod(path, event, **_k):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    monkeypatch.setattr(
        process_launcher.process_event_ledger, "append_event", append_without_chmod
    )
    manager, cards, _order, started = _quality_reviewer_launch_harness(
        tmp_path, monkeypatch
    )
    claim_calls: list[str] = []

    def fake_claim(repo, task_id, runner, topic, request_id=""):
        claim_calls.append(str(request_id))
        existing = cards.get(str(task_id), {})
        if existing.get("status") == "processing":

            return {
                "ok": False,
                "returncode": 1,
                "stdout": "",
                "stderr": "card_scoped_claim_start_ineligible:processing",

            }
        card = {
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "launch_request_id": request_id,
            "claim_epoch": 1,
            "status": "processing",
            "worker_status": "claimed",
            "claimed_by": runner,
            "read_only": existing.get("read_only", True),
            "allowed_writes": list(existing.get("allowed_writes") or []),
        }
        cards[str(task_id)] = card
        return {
            "ok": True,
            "returncode": 0,
            "stdout": json.dumps(card),
            "stderr": "",
        }

    monkeypatch.setattr(process_launcher.task_engine, "claim_start_exact", fake_claim)
    receipt = manager.launch_quality_reviewer(
        target_request_id="target-req-1",
        target_task_id="TARGET_TASK_1",
        reviewer_task_id="REVIEWER_TASK_1",
        runner="claude_worker_reviewer",
        adapter_id="claude_cli",
        lens="correctness",
    )
    assert receipt["ok"] is True
    bound_id = str(receipt["request_id"])
    assert cards["REVIEWER_TASK_1"]["launch_request_id"] == bound_id
    assert len(claim_calls) == 1
    owner = started[0]
    assert owner.target == manager._launch_reserved_quality_reviewer

    _open_gates(monkeypatch)
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    candidate_home = tmp_path / "candidate_home"
    (candidate_home / "task_mcp_worker_runtime").mkdir(parents=True)
    fake_workspace = process_launcher.WorkerWorkspace(
        request_id="c" * 32,
        repo=candidate_dir,
        path=candidate_dir,
        home=candidate_home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    packet = {
        "packet_sha256": "a" * 64,
        "target": {"claim_epoch": 1},
        "lens": "correctness",
    }
    read_only_input_paths = ["README.md", "docs/SOURCE_GRAPH.md"]

    def fake_prep(*_a, **_k):
        return {
            "ok": True,
            "prepared": {
                "worker_adapter_id": "claude_cli",
                "workspace": fake_workspace,
                "changed_hashes": {"module.py": "h"},
                "read_only_input_paths": read_only_input_paths,
                "packet": packet,
            },
        }

    monkeypatch.setattr(manager, "_prepared_quality_review", fake_prep)
    popen_calls: list[object] = []

    def fake_popen(*_a, **_k):
        popen_calls.append(True)
        return SimpleNamespace(pid=4242, poll=lambda: None)

    monkeypatch.setattr(manager, "_popen", fake_popen)
    spawn_calls: list[str] = []

    def fake_spawn(request_id, binding=None, **_k):
        spawn_calls.append(str(request_id))
        assert binding is not None
        assert binding.get("read_only_input_paths") == read_only_input_paths
        return True

    monkeypatch.setattr(manager, "_reviewer_spawn_transition", fake_spawn)
    launch_kwargs_seen: list[dict] = []
    launch_receipts: list[dict] = []

    def wrapped_launch_task(**kwargs):
        launch_kwargs_seen.append(dict(kwargs))
        reserved = kwargs.get("reserved_request_id")
        card = manager._preflight_card(
            kwargs["task_id"],
            kwargs["runner"],
            kwargs["topic"],
            kwargs["adapter_id"],
            reserved_request_id=reserved,
        )
        assert process_launcher.core._lifecycle_state(card) == "processing"
        assert card.get("launch_request_id") == reserved
        quality_review_binding = kwargs.get("quality_review_binding")
        assert quality_review_binding is not None
        assert quality_review_binding.get("read_only_input_paths") == read_only_input_paths
        manager._reviewer_spawn_transition(
            reserved, binding=quality_review_binding
        )
        process = manager._popen([sys.executable, "-c", "pass"])
        result = {
            "ok": True,
            "request_id": reserved,
            "task_id": kwargs["task_id"],
            "state": "running",
            "pid": process.pid,
        }
        launch_receipts.append(result)
        return result

    monkeypatch.setattr(manager, "launch_task", wrapped_launch_task)

    class ExecThread:
        def __init__(self, target=None, kwargs=None, name=None, daemon=None, args=()):
            self.target = target
            self.kwargs = kwargs or {}
            self.args = args
            self.name = name or ""

        def start(self):
            if str(self.name).startswith("aiworkhub-task-"):
                return
            if self.target is not None:
                self.target(*self.args, **self.kwargs)

        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(process_launcher.threading, "Thread", ExecThread)
    owner.target(**owner.kwargs)
    assert len(claim_calls) == 1
    assert launch_kwargs_seen
    assert launch_kwargs_seen[0].get("reserved_request_id") == bound_id
    assert launch_kwargs_seen[0].get("task_id") == "REVIEWER_TASK_1"
    assert launch_receipts and launch_receipts[0].get("ok") is True
    assert launch_receipts[0].get("request_id") == bound_id
    assert spawn_calls == [bound_id]
    assert popen_calls == [True]


def test_quality_reviewer_supervisor_handoff_uses_existing_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    monkeypatch.setattr(
        process_launcher, "__file__", str(overlay / "process_launcher.py")
    )
    host_root = tmp_path / "host_package"
    canonical = host_root / "aiworkhub" / "worker_supervisor.py"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("def main():\n    pass\n# --spec\n", encoding="utf-8")
    monkeypatch.setattr(
        process_launcher.worker_ai_tools_mcp,
        "resolve_host_package_import_root",
        lambda: host_root,
    )
    script = process_launcher._worker_supervisor_script()
    assert script == canonical
    assert script.is_file()
    assert script.name == "worker_supervisor.py"
    assert not (overlay / "worker_supervisor.py").exists()
    text = script.read_text(encoding="utf-8")
    assert "--spec" in text
    assert "def main" in text


def test_quality_reviewer_supervisor_handoff_rejects_missing_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    monkeypatch.setattr(
        process_launcher, "__file__", str(overlay / "process_launcher.py")
    )
    monkeypatch.setattr(
        process_launcher.worker_ai_tools_mcp,
        "resolve_host_package_import_root",
        lambda: tmp_path / "missing_host_package",
    )
    monkeypatch.setattr(process_launcher.sys, "path", [])
    with pytest.raises(
        process_launcher.LaunchRejected, match="worker_supervisor_script_missing"
    ):
        process_launcher._worker_supervisor_script()


# --- NF-2026-00456: scope (allowed_writes/read_first) vs. explicit
# mandatory-change (required_outputs) is authenticated separately, and every
# observed required-output mismatch is retained as a distinct structured
# diagnostic instead of failing closed on the first one. -------------------


def test_expand_template_scope_is_not_implicit_mandatory_change() -> None:
    expanded = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["a.py", "b.py"],
        test_paths=["tests/test_a.py"],
    )
    assert expanded["allowed_writes"] == ["a.py", "b.py", "tests/test_a.py"]
    assert expanded["required_outputs"] == []


def test_expand_template_mandatory_changed_output_must_be_in_scope() -> None:
    with pytest.raises(
        task_templates.TaskTemplateError,
        match="mandatory_changed_output_out_of_scope",
    ):
        task_templates.expand_template(
            "implementation_with_tests",
            production_paths=["a.py"],
            test_paths=["tests/test_a.py"],
            mandatory_changed_outputs=["not_in_scope.py"],
        )


def test_expand_template_explicit_mandatory_changed_output_is_narrow() -> None:
    expanded = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["a.py", "b.py"],
        test_paths=["tests/test_a.py"],
        mandatory_changed_outputs=["a.py"],
    )
    assert expanded["required_outputs"] == ["a.py"]
    assert expanded["allowed_writes"] == ["a.py", "b.py", "tests/test_a.py"]


def _seeded_workspace(
    tmp_path: Path,
    *,
    files: dict[str, bytes],
    baselines: dict[str, str],
    allowed_writes: tuple[str, ...] | None = None,
) -> process_launcher.WorkerWorkspace:
    """Build an isolated ``WorkerWorkspace`` fixture; never a live task store."""
    repo = tmp_path / "repo"
    worktree = tmp_path / "request" / "worktree"
    home = tmp_path / "request" / "home"
    for directory in (repo, worktree, home):
        directory.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    normalized_baselines = {}
    for relative, digest in baselines.items():
        if digest.startswith("file:"):
            normalized_baselines[relative] = digest
            continue
        mode = (worktree / relative).stat().st_mode & 0o777
        normalized_baselines[relative] = f"file:{mode:o}:{digest}"
    return process_launcher.WorkerWorkspace(
        request_id="req",
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=allowed_writes or tuple(files),
        parent_baseline=normalized_baselines,
        workspace_baseline=normalized_baselines,
    )


def test_validate_required_outputs_unrelated_authorized_output_survives_unchanged(
    tmp_path: Path,
) -> None:
    """Reproduce the two-file-delta-terminalized-on-an-unrelated-unchanged-
    authorized-output shape: a real delta at a.py/b.py must not be killed by
    an untouched, merely-authorized c.py that was never declared mandatory."""
    workspace = _seeded_workspace(
        tmp_path,
        files={"a.py": b"changed-a", "b.py": b"changed-b", "c.py": b"unchanged"},
        baselines={"c.py": hashlib.sha256(b"unchanged").hexdigest()},
    )
    expanded = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["a.py", "b.py", "c.py"],
        test_paths=["tests/test_x.py"],
        mandatory_changed_outputs=["a.py", "b.py"],
    )
    assert expanded["allowed_writes"] == ["a.py", "b.py", "c.py", "tests/test_x.py"]
    records = process_launcher._fallback_validate_required_outputs(
        workspace, expanded["required_outputs"]
    )
    assert {record["path"] for record in records} == {"a.py", "b.py"}


def test_validate_required_outputs_zero_edits_yield_no_mandatory_records(
    tmp_path: Path,
) -> None:
    """Reproduce the had-no-edits-but-terminalized-as-required_output_unchanged
    shape: with no explicit mandatory-change declaration, an all-unchanged
    workspace validates cleanly here; overall no-op detection is a separate
    gate, not a false single-file required_output_unchanged."""
    workspace = _seeded_workspace(
        tmp_path,
        files={"process_launcher.py": b"unchanged-content"},
        baselines={
            "process_launcher.py": hashlib.sha256(b"unchanged-content").hexdigest()
        },
    )
    expanded = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["process_launcher.py"],
        test_paths=["tests/test_x.py"],
    )
    assert expanded["required_outputs"] == []
    records = process_launcher._fallback_validate_required_outputs(
        workspace, expanded["required_outputs"]
    )
    assert records == []


def test_validate_required_outputs_explicit_mandatory_output_still_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = _seeded_workspace(
        tmp_path,
        files={"a.py": b"same"},
        baselines={"a.py": hashlib.sha256(b"same").hexdigest()},
    )
    expanded = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["a.py"],
        test_paths=["tests/test_x.py"],
        mandatory_changed_outputs=["a.py"],
    )
    assert expanded["required_outputs"] == ["a.py"]
    with pytest.raises(process_launcher.WorkspaceError, match="required_output_mismatch"):
        process_launcher._fallback_validate_required_outputs(
            workspace, expanded["required_outputs"]
        )


def test_validate_required_outputs_retains_all_distinct_mismatch_categories(
    tmp_path: Path,
) -> None:
    workspace = _seeded_workspace(
        tmp_path,
        files={"mandatory.py": b"same-content", "empty.py": b""},
        baselines={"mandatory.py": hashlib.sha256(b"same-content").hexdigest()},
        allowed_writes=("missing.py", "mandatory.py", "empty.py"),
    )
    with pytest.raises(process_launcher.WorkspaceError) as excinfo:
        process_launcher.validate_required_outputs(
            workspace, ["missing.py", "mandatory.py", "empty.py"]
        )
    message = str(excinfo.value)
    assert message.startswith("required_output_mismatch:")
    diagnostics = json.loads(message.split(":", 1)[1])
    assert diagnostics["missing_required_artifacts"] == ["missing.py"]
    assert diagnostics["unchanged_mandatory_outputs"] == ["mandatory.py"]
    assert diagnostics["scope_violations"] == [
        {"path": "empty.py", "reason": "required_output_zero_bytes"}
    ]
    assert diagnostics["primary_validation_result"] == []


@pytest.mark.parametrize(
    ("production_paths", "test_paths", "mandatory_changed_outputs", "files", "unchanged_baselines"),
    [
        (
            ["src/app_container.py", "src/app_container_config.py"],
            ["tests/test_app_container.py"],
            ["src/app_container.py"],
            {
                "src/app_container.py": b"changed-fix",
                "src/app_container_config.py": b"unchanged-config",
            },
            {"src/app_container_config.py": b"unchanged-config"},
        ),
        (
            ["src/model_settings_modal.py", "src/model_settings.css"],
            ["tests/test_model_settings_modal.py"],
            ["src/model_settings_modal.py"],
            {
                "src/model_settings_modal.py": b"changed-modal",
                "src/model_settings.css": b"unchanged-css",
                "tests/test_model_settings_modal.py": b"unchanged-test",
            },
            {
                "src/model_settings.css": b"unchanged-css",
                "tests/test_model_settings_modal.py": b"unchanged-test",
            },
        ),
    ],
    ids=["appcontainer_style", "model_settings_style"],
)
def test_optional_authorized_helper_unchanged_does_not_fail_finalization(
    tmp_path: Path,
    production_paths: list[str],
    test_paths: list[str],
    mandatory_changed_outputs: list[str],
    files: dict[str, bytes],
    unchanged_baselines: dict[str, bytes],
) -> None:
    """End-to-end AppContainer/Model-Settings regression: the worker changes
    only the declared mandatory file; every other authorized-but-optional
    production/test path stays byte-identical, and finalization must observe
    only the explicitly declared mandatory change, not the full authorized
    write scope."""
    expanded = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=production_paths,
        test_paths=test_paths,
        mandatory_changed_outputs=mandatory_changed_outputs,
    )
    workspace = _seeded_workspace(
        tmp_path,
        files=files,
        baselines={
            relative: hashlib.sha256(content).hexdigest()
            for relative, content in unchanged_baselines.items()
        },
        allowed_writes=tuple(expanded["allowed_writes"]),
    )
    records = process_launcher.validate_required_outputs(
        workspace, expanded["required_outputs"]
    )
    assert {record["path"] for record in records} == set(mandatory_changed_outputs)


def test_validate_required_outputs_existing_mandatory_file_edited_from_old_baseline(
    tmp_path: Path,
) -> None:
    """Reproduce the primary real-world accepted path: the mandatory output
    already existed before the change (a real pre-edit baseline hash), and
    the worker edited its bytes. This must validate cleanly, distinct from
    the no-baseline (newly created file) and unchanged-baseline (no-op)
    shapes already covered above."""
    workspace = _seeded_workspace(
        tmp_path,
        files={"src/app_container.py": b"changed-fix"},
        baselines={"src/app_container.py": hashlib.sha256(b"original-fix").hexdigest()},
    )
    expanded = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["src/app_container.py"],
        test_paths=["tests/test_app_container.py"],
        mandatory_changed_outputs=["src/app_container.py"],
    )
    records = process_launcher.validate_required_outputs(
        workspace, expanded["required_outputs"]
    )
    assert {record["path"] for record in records} == {"src/app_container.py"}


def test_appcontainer_style_explicit_mandatory_output_unchanged_fails_closed_with_evidence(
    tmp_path: Path,
) -> None:
    """When the explicitly declared mandatory output is left unchanged,
    finalization must still fail closed, and the diagnostics must retain the
    primary validation result (records that did pass) alongside the
    mismatch, never discarding it."""
    expanded = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["src/app_container.py", "src/app_container_config.py"],
        test_paths=["tests/test_app_container.py"],
        mandatory_changed_outputs=["src/app_container.py"],
    )
    workspace = _seeded_workspace(
        tmp_path,
        files={
            "src/app_container.py": b"unchanged-fix",
            "src/app_container_config.py": b"unchanged-config",
        },
        baselines={
            "src/app_container.py": hashlib.sha256(b"unchanged-fix").hexdigest(),
            "src/app_container_config.py": hashlib.sha256(
                b"unchanged-config"
            ).hexdigest(),
        },
        allowed_writes=tuple(expanded["allowed_writes"]),
    )
    with pytest.raises(
        process_launcher.WorkspaceError, match="required_output_mismatch"
    ) as excinfo:
        process_launcher.validate_required_outputs(
            workspace, expanded["required_outputs"]
        )
    diagnostics = json.loads(str(excinfo.value).split(":", 1)[1])
    assert diagnostics["unchanged_mandatory_outputs"] == ["src/app_container.py"]
    assert diagnostics["primary_validation_result"] == []


def test_quality_review_packet_binding_carries_explicit_target_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = "e" * 32
    manager = _manager(
        tmp_path,
        show_task=lambda _task_id: {"returncode": 1, "stdout": "", "stderr": ""},
        argv=[sys.executable, "-c", "pass"],
    )
    repo = manager.repo
    workspace_path = tmp_path / "worktrees" / request_id / "worktree"
    home = tmp_path / "worktrees" / request_id / "home"
    workspace_path.mkdir(parents=True)
    home.mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    (repo / "docs" / "SOURCE_GRAPH.md").write_text("graph\n", encoding="utf-8")
    (workspace_path / "src").mkdir()
    candidate = workspace_path / "src" / "changed.py"
    candidate.write_text("value = 2\n", encoding="utf-8")
    changed_hashes = {
        "src/changed.py": hashlib.sha256(candidate.read_bytes()).hexdigest()
    }
    workspace = process_launcher.WorkerWorkspace(
        request_id=request_id,
        repo=repo,
        path=workspace_path,
        home=home,
        allowed_writes=("src/changed.py",),
        parent_baseline={},
        workspace_baseline={},
    )
    card = _card(task_id="TARGET_TASK", state="review")
    card.update(
        {
            "claim_epoch": 1,
            "allowed_writes": ["src/changed.py"],
            "read_first": ["README.md", "src/changed.py"],
            "immutable_inputs": ["docs/SOURCE_GRAPH.md"],
            "terminal_review": {
                "evidence": {
                    "workspace": workspace.as_metadata(),
                    "changed_path_hashes": changed_hashes,
                    "quality_gate": {"checks": []},
                    "validation": [],
                }
            },
        }
    )
    manager._show_task = _show(lambda: card)
    manager._append_event(
        {
            "request_id": request_id,
            "task_id": "TARGET_TASK",
            "runner": "worker",
            "topic": "code",
            "adapter_id": "worker_adapter",
            "state": "review_ready",
        }
    )
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setattr(
        manager,
        "_quality_review_source_evidence",
        lambda *_args, **_kwargs: {
            "src/changed.py": {"sha256": changed_hashes["src/changed.py"]}
        },
    )
    scoped_kwargs = {}
    monkeypatch.setattr(
        process_launcher.quality_review_scope,
        "build_scoped_audits",
        lambda **kwargs: scoped_kwargs.update(
            {**kwargs, "source_evidence": dict(kwargs["source_evidence"])}
        )
        or {},
    )
    packet_kwargs = {}
    monkeypatch.setattr(
        process_launcher.quality_reviewer,
        "build_review_packet",
        lambda **kwargs: packet_kwargs.update(kwargs)
        or {
            "schema_id": "aiworkhub.quality_review.packet.v1",
            "contract": {},
            "candidate": {},
            "packet_sha256": "a" * 64,
        },
    )

    result = manager._build_quality_review_packet(request_id, "TARGET_TASK")

    assert result["ok"] is True, result
    prepared = result["prepared"]
    assert prepared["read_only_input_paths"] == [
        "README.md",
        "docs/SOURCE_GRAPH.md",
    ]
    assert set(scoped_kwargs["source_evidence"]) == {"src/changed.py"}
    assert set(packet_kwargs["source_evidence"]) == {"src/changed.py"}
    assert prepared["packet"]["contract"]["immutable_inputs"] == [
        "docs/SOURCE_GRAPH.md"
    ]
    packet_body = {
        key: value
        for key, value in prepared["packet"].items()
        if key != "packet_sha256"
    }
    assert prepared["packet"]["packet_sha256"] == (
        process_launcher.quality_reviewer._canonical_digest(packet_body)
    )


def test_verified_quality_review_receipt_ingests_jsonl_once_across_retry(
    tmp_path: Path,
) -> None:
    from test_quality_reviewer_contract import _packet, _worker_context

    packet = _packet()
    packet_path = tmp_path / "review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _worker_context(tmp_path, packet_path)
    stdout_path = tmp_path / f"{ctx.request_id}.stdout.log"
    stdout_path.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(
                        {"lens": "correctness", "findings": []},
                        separators=(",", ":"),
                    ),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    metadata = {
        "task_id": ctx.task_id,
        "runner": ctx.runner,
        "topic": ctx.topic,
        "adapter_id": "claude_cli",
        "stdout_path": str(stdout_path),
        "worker_mcp": {
            "audit_ledger_path": str(ctx.audit_ledger_path),
            "audit_hmac_key_path": str(ctx.audit_hmac_key_path),
        },
        "quality_review": {
            "packet_path": str(packet_path),
            "lens": "correctness",
        },
    }
    workspace = SimpleNamespace(repo=tmp_path, home=tmp_path)

    first = process_launcher._verified_quality_review_receipt(
        metadata, workspace, ctx.request_id
    )
    retried = process_launcher._verified_quality_review_receipt(
        metadata, workspace, ctx.request_id
    )

    assert first["submission_id"] == retried["submission_id"]
    assert first["physical_submission_count"] == 1
    assert first["logical_submission_count"] == 1
    ledger_lines = ctx.audit_ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(ledger_lines) == 1
    authenticated_entry = json.loads(ledger_lines[0])
    assert authenticated_entry["provenance"] == "live"
    assert authenticated_entry["authority_source"] == "supervisor"


def test_accepted_outcome_receipt_binds_promoted_repository_bytes(tmp_path: Path) -> None:
    (tmp_path / "result.txt").write_bytes(b"canonical-result\n")
    digest = hashlib.sha256(b"canonical-result\n").hexdigest()

    receipt = process_launcher._accepted_outcome_receipt(
        tmp_path,
        task_id="TASK_RECEIPT",
        request_id="request-receipt",
        claim_epoch=3,
        base_oid="base-oid",
        promoted_paths=["result.txt"],
        changed_path_hashes={"result.txt": digest},
        attempt_artifact_manifest={"entries": []},
    )

    assert receipt["promoted_paths"] == ["result.txt"]
    assert receipt["changed_path_hashes"] == {"result.txt": digest}
    assert receipt["repository_revision"].startswith("sha256:")
    assert receipt["receipt_id"].startswith("sha256:")


def test_accepted_outcome_receipt_rejects_post_promotion_byte_mismatch(
    tmp_path: Path,
) -> None:
    (tmp_path / "result.txt").write_bytes(b"forged\n")

    with pytest.raises(process_launcher.WorkspaceError, match="candidate_mismatch"):
        process_launcher._accepted_outcome_receipt(
            tmp_path,
            task_id="TASK_RECEIPT",
            request_id="request-receipt",
            claim_epoch=3,
            base_oid="base-oid",
            promoted_paths=["result.txt"],
            changed_path_hashes={"result.txt": "0" * 64},
            attempt_artifact_manifest={"entries": []},
        )


def test_acceptance_helpers_preserve_alias_and_identical_retry_receipt(
    tmp_path: Path,
) -> None:
    assert (
        process_launcher._accepted_outcome_receipt
        is process_launcher_acceptance.accepted_outcome_receipt
    )
    receipt = {"receipt_id": "sha256:persisted"}
    card = {
        "status": "finished",
        "accepted_request_id": "request-1",
        "accept_evidence": {"accepted_outcome_receipt": receipt},
    }
    closed: list[tuple[str, str]] = []

    result = process_launcher_acceptance.finished_acceptance_result(
        tmp_path,
        card,
        task_id="TASK_ACCEPTED",
        request_id="request-1",
        canonical_status=lambda value: str(value["status"]),
        close_needfix=lambda task_id, request_id: (
            closed.append((task_id, request_id)) or {"state": "closed"}
        ),
    )

    assert result is not None
    assert result["accepted_outcome_receipt"] is receipt
    assert result["already_accepted"] is True
    assert closed == [("TASK_ACCEPTED", "request-1")]


def test_acceptance_helper_hides_receipt_from_different_request(tmp_path: Path) -> None:
    result = process_launcher_acceptance.finished_acceptance_result(
        tmp_path,
        {
            "status": "finished",
            "accepted_request_id": "request-1",
            "accept_evidence": {"accepted_outcome_receipt": {"receipt_id": "secret"}},
        },
        task_id="TASK_ACCEPTED",
        request_id="request-2",
        canonical_status=lambda value: str(value["status"]),
        close_needfix=lambda _task_id, _request_id: pytest.fail("must not close"),
    )

    assert result is not None
    assert result["ok"] is False
    assert result["accepted_outcome_receipt"] is None
    assert result["error"] == "task_already_finished_by_other_request"


def _nf523_parity_mypy_row(**overrides):
    row = {
        "command": ".venv/bin/python -m mypy src/aiworkhub/example.py",
        "declared_command": ".venv/bin/python -m mypy src/aiworkhub/example.py",
        "argv": [".venv/bin/python", "-m", "mypy", "src/aiworkhub/example.py"],
        "executed_argv": [
            ".venv/bin/python",
            "-m",
            "mypy",
            "src/aiworkhub/example.py",
        ],
        "returncode": 1,
        "timed_out": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "behavioral_role": "parity",
        "stdout_tail": (
            "src/aiworkhub/example.py:12:8: "
            "error: incompatible argument [call-arg]\n"
        ),
        "stderr_tail": "",
    }
    row.update(overrides)
    return row


_NF523_EXTRA_LINE = (
    "src/aiworkhub/example.py:40:9: "
    "error: unsupported operand [operator]\n"
)


def _nf523_baseline_compare(baseline_row):
    from types import SimpleNamespace

    from aiworkhub.process_launcher_validation import compare_schema_mypy_baseline

    workspace = SimpleNamespace(
        repo="/repos/example",
        path="/worktrees/candidate",
        home="/homes/candidate",
        base_oid="f" * 40,
    )

    def create_workspace(repo, name, card, adapter_id, **kwargs):
        return SimpleNamespace(
            repo=repo,
            path=f"/worktrees/{name}",
            home=f"/homes/{name}",
            base_oid=kwargs.get("pinned_base_oid"),
        )

    def cleanup_workspace(repo, path, home):
        return None

    def run_validations(target, commands, **route):
        assert commands == [baseline_row["declared_command"]]
        return [dict(baseline_row)]

    def compare(candidate):
        return compare_schema_mypy_baseline(
            workspace,
            {"task_id": "AIWORKHUB_NF525"},
            {"adapter_id": "adapter-x"},
            candidate,
            create_workspace=create_workspace,
            cleanup_workspace=cleanup_workspace,
            run_validations=run_validations,
            route_resolver=lambda metadata: {"backend": "deterministic_validation"},
        )

    return compare


def test_parity_schema_mypy_baseline_equal_diagnostics_passes():
    compare = _nf523_baseline_compare(_nf523_parity_mypy_row())
    row = _nf523_parity_mypy_row(behavioral_role="PARITY")
    assert compare([row]) == [row]
    evidence = row["baseline_comparison"]
    assert evidence["schema_id"] == "aiworkhub.baseline_comparison.v1"
    assert evidence["outcome"] == "baseline_no_new_diagnostics"
    assert evidence["new_diagnostics"] == []
    assert evidence["candidate_count"] == evidence["baseline_count"] == 1


def test_parity_schema_mypy_baseline_subset_diagnostics_passes():
    subset = _nf523_parity_mypy_row()
    superset = _nf523_parity_mypy_row(
        stdout_tail=subset["stdout_tail"] + _NF523_EXTRA_LINE
    )
    compare = _nf523_baseline_compare(superset)
    row = _nf523_parity_mypy_row()
    assert compare([row]) == [row]
    evidence = row["baseline_comparison"]
    assert evidence["schema_id"] == "aiworkhub.baseline_comparison.v1"
    assert evidence["outcome"] == "baseline_no_new_diagnostics"
    assert evidence["candidate_count"] == 1
    assert evidence["baseline_count"] == 2


def test_parity_schema_mypy_baseline_new_diagnostics_fails_closed():
    from aiworkhub.worker_workspace import WorkspaceError

    baseline = _nf523_parity_mypy_row()
    candidate = _nf523_parity_mypy_row(
        stdout_tail=baseline["stdout_tail"] + _NF523_EXTRA_LINE
    )
    compare = _nf523_baseline_compare(baseline)
    with pytest.raises(WorkspaceError, match="baseline_mypy_new_diagnostics"):
        compare([candidate])
    evidence = candidate["baseline_comparison"]
    assert evidence["outcome"] == "baseline_new_diagnostics"
    assert evidence["new_diagnostics"] == [
        ["src/aiworkhub/example.py", "operator", "unsupported operand"]
    ]


@pytest.mark.parametrize("role", ["regression", "reproduction", "delta"])
def test_validation_role_parity_gate_rejects_non_parity_roles(role):
    from aiworkhub.worker_workspace import WorkspaceError

    compare = _nf523_baseline_compare(_nf523_parity_mypy_row())
    row = _nf523_parity_mypy_row(behavioral_role=role)
    with pytest.raises(WorkspaceError, match="baseline_comparison_ineligible"):
        compare([row])
    assert "baseline_comparison" not in row


def test_validation_role_parity_gate_rejects_non_mypy_command():
    from aiworkhub.worker_workspace import WorkspaceError

    compare = _nf523_baseline_compare(_nf523_parity_mypy_row())
    row = _nf523_parity_mypy_row(
        command=".venv/bin/python -m pytest -q tests",
        declared_command=".venv/bin/python -m pytest -q tests",
        argv=[".venv/bin/python", "-m", "pytest", "-q", "tests"],
        executed_argv=[".venv/bin/python", "-m", "pytest", "-q", "tests"],
    )
    with pytest.raises(WorkspaceError, match="baseline_comparison_ineligible"):
        compare([row])
    assert "baseline_comparison" not in row


def test_baseline_comparison_parity_identity_mismatch_fails_closed():
    from aiworkhub.worker_workspace import WorkspaceError

    drifted = _nf523_parity_mypy_row(
        executed_argv=[
            "/usr/bin/python3.11",
            "-m",
            "mypy",
            "src/aiworkhub/example.py",
        ]
    )
    compare = _nf523_baseline_compare(drifted)
    row = _nf523_parity_mypy_row()
    with pytest.raises(WorkspaceError, match="baseline_validation_authority_mismatch"):
        compare([row])


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"timed_out": True}, "baseline_mypy_candidate_not_comparable"),
        ({"stdout_truncated": True}, "baseline_mypy_output_truncated"),
    ],
    ids=["timeout", "stdout_truncated"],
)
def test_parity_schema_mypy_baseline_non_comparable_fails_closed(overrides, error):
    from aiworkhub.worker_workspace import WorkspaceError

    compare = _nf523_baseline_compare(_nf523_parity_mypy_row())
    row = _nf523_parity_mypy_row(**overrides)
    with pytest.raises(WorkspaceError, match=error):
        compare([row])


# --- NF-2026-00548 (audit M3): in-run zero-delta tripwire and the
# identical-relaunch guard. Both are named, deterministic platform mechanics:
# the tripwire only ever appends a notice, and the guard only ever refuses a
# launch whose every input is provably identical to a recorded failure. ------


_NF548_SEED = b"seed-content\n"
_NF548_ALLOWED_WRITE = "out/result.json"


def _nf548_workspace(
    tmp_path: Path, *, content: bytes = _NF548_SEED
) -> process_launcher.WorkerWorkspace:
    """A provisioned workspace whose baseline describes ``_NF548_SEED``."""
    return _seeded_workspace(
        tmp_path,
        files={_NF548_ALLOWED_WRITE: content},
        baselines={_NF548_ALLOWED_WRITE: hashlib.sha256(_NF548_SEED).hexdigest()},
        allowed_writes=(_NF548_ALLOWED_WRITE,),
    )


class _NF548Process:
    """A worker that times out ``timeouts`` times before exiting cleanly."""

    def __init__(self, timeouts: int = 0) -> None:
        self.pid = 4242
        self.waits = 0
        self.terminated = False
        self.killed = False
        self._timeouts = timeouts

    def wait(self, timeout=None):
        self.waits += 1
        if self.waits <= self._timeouts:
            raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout or 0)
        return 0

    def poll(self):
        return 0 if self.waits > self._timeouts else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _nf548_live(
    tmp_path: Path,
    manager: process_launcher.ProcessManager,
    workspace: process_launcher.WorkerWorkspace,
    *,
    process: object | None = None,
    timeout_seconds: int = 600,
    read_only: bool = False,
    allow_unchanged: tuple[str, ...] = (),
) -> process_launcher._LiveProcess:
    request_id = "nf548request"
    manager.process_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = manager.process_dir / f"{request_id}.request.json"
    metadata_path.write_text(
        json.dumps({
            "request_id": request_id,
            "workspace": workspace.as_metadata(),
            "required_outputs": [_NF548_ALLOWED_WRITE],
            "read_only": read_only,
            "allow_unchanged_required_outputs": list(allow_unchanged),
        }),
        encoding="utf-8",
    )
    return process_launcher._LiveProcess(
        request_id=request_id,
        task_id="TASK_B1",
        runner="claude_worker_b1",
        topic="task_mcp",
        adapter_id="claude_cli",
        model="claude-opus-5",
        process=process if process is not None else _NF548Process(),
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        started_at="2026-09-01T00:00:00+00:00",
        timeout_seconds=timeout_seconds,
        isolated=True,
        metadata_path=metadata_path,
    )


def _nf548_notices(manager: process_launcher.ProcessManager) -> list[dict]:
    return [
        event
        for event in manager._events()
        if event.get("notice") == process_launcher.ZERO_DELTA_NOTICE
    ]


def test_zero_delta_tripwire_emits_exactly_one_notice_per_empty_run(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(_card),
        argv=[sys.executable, "-c", "pass"],
    )
    live = _nf548_live(tmp_path, manager, _nf548_workspace(tmp_path))
    manager._append_event({
        "request_id": live.request_id,
        "task_id": live.task_id,
        "state": "running",
        "pid": live.process.pid,
    })

    first = manager._maybe_emit_zero_delta_notice(live, elapsed_seconds=420.0)
    assert first is not None
    assert first["notice"] == "zero_required_output_delta_warning"
    assert first["event_kind"] == process_launcher.RUNTIME_NOTICE_EVENT_KIND
    assert first["request_id"] == live.request_id
    assert first["required_outputs"] == [_NF548_ALLOWED_WRITE]
    assert first["allowed_writes"] == [_NF548_ALLOWED_WRITE]
    assert first["changed_allowed_writes"] == []
    assert first["elapsed_share"] == 0.7
    assert first["notice_after_seconds"] == 300.0
    assert first["enforced"] is False
    # A notice carries no lifecycle state at all, so nothing can read it as one.
    assert "state" not in first

    assert manager._maybe_emit_zero_delta_notice(live, elapsed_seconds=500.0) is None
    assert manager._maybe_emit_zero_delta_notice(live, elapsed_seconds=590.0) is None
    assert len(_nf548_notices(manager)) == 1

    # Lifecycle semantics are untouched: the request's state row is still the
    # one the supervisor published, not the notice.
    assert manager._latest_by_request()[live.request_id]["state"] == "running"


def test_zero_delta_tripwire_stays_silent_when_an_allowed_write_changed(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(_card),
        argv=[sys.executable, "-c", "pass"],
    )
    workspace = _nf548_workspace(tmp_path, content=b"the worker actually wrote\n")
    assert process_launcher.changed_allowed_write_paths(workspace) == [
        _NF548_ALLOWED_WRITE
    ]
    live = _nf548_live(tmp_path, manager, workspace)

    assert manager._maybe_emit_zero_delta_notice(live, elapsed_seconds=599.0) is None
    assert live.zero_delta_tripwire_settled is True
    assert _nf548_notices(manager) == []


@pytest.mark.parametrize(
    "exemption",
    [{"read_only": True}, {"allow_unchanged": (_NF548_ALLOWED_WRITE,)}],
    ids=["read_only", "allow_unchanged_required_outputs"],
)
def test_zero_delta_tripwire_never_fires_for_exempt_cards(
    tmp_path: Path, exemption: dict
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(_card),
        argv=[sys.executable, "-c", "pass"],
    )
    live = _nf548_live(tmp_path, manager, _nf548_workspace(tmp_path), **exemption)

    assert manager._maybe_emit_zero_delta_notice(live, elapsed_seconds=10_000.0) is None
    assert _nf548_notices(manager) == []


def test_zero_delta_tripwire_holds_until_the_configured_share_elapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(_card),
        argv=[sys.executable, "-c", "pass"],
    )
    live = _nf548_live(tmp_path, manager, _nf548_workspace(tmp_path))

    assert process_launcher.zero_delta_notice_after_seconds(600) == 300.0
    assert manager._maybe_emit_zero_delta_notice(live, elapsed_seconds=299.0) is None
    assert live.zero_delta_tripwire_settled is False
    assert _nf548_notices(manager) == []

    monkeypatch.setenv(process_launcher.ZERO_DELTA_ELAPSED_SHARE_ENV, "0.9")
    assert process_launcher.zero_delta_elapsed_share() == 0.9
    assert process_launcher.zero_delta_notice_after_seconds(600) == 540.0
    assert manager._maybe_emit_zero_delta_notice(live, elapsed_seconds=400.0) is None
    assert _nf548_notices(manager) == []

    emitted = manager._maybe_emit_zero_delta_notice(live, elapsed_seconds=560.0)
    assert emitted is not None
    assert emitted["notice_after_seconds"] == 540.0


@pytest.mark.parametrize(
    "raw", ["", "   ", "not-a-number", "0", "-0.5", "1.5"], ids=range(6)
)
def test_zero_delta_elapsed_share_falls_back_to_the_bounded_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(process_launcher.ZERO_DELTA_ELAPSED_SHARE_ENV, raw)
    assert (
        process_launcher.zero_delta_elapsed_share()
        == process_launcher.ZERO_DELTA_DEFAULT_ELAPSED_SHARE
    )


def test_zero_delta_notice_bounds_hold_for_short_and_long_ceilings() -> None:
    assert process_launcher.zero_delta_notice_after_seconds(60) == (
        process_launcher.ZERO_DELTA_MIN_SECONDS
    )
    assert process_launcher.zero_delta_notice_after_seconds(86_400) == (
        process_launcher.ZERO_DELTA_MAX_SECONDS
    )
    assert process_launcher.zero_delta_notice_after_seconds("nonsense") == (
        process_launcher.ZERO_DELTA_MIN_SECONDS
    )


def test_zero_delta_monitor_wait_notices_once_without_killing_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(
        tmp_path,
        show_task=_show(_card),
        argv=[sys.executable, "-c", "pass"],
    )
    process = _NF548Process(timeouts=3)
    live = _nf548_live(tmp_path, manager, _nf548_workspace(tmp_path), process=process)
    monkeypatch.setattr(
        launch_zero_delta, "zero_delta_notice_after_seconds", lambda _timeout: 0.0
    )

    manager._await_exit_watching_zero_delta(live)

    assert process.waits == 4
    assert process.poll() == 0
    assert process.terminated is False and process.killed is False
    assert len(_nf548_notices(manager)) == 1


def test_zero_delta_monitor_wait_supports_a_process_without_timeout(
    tmp_path: Path,
) -> None:
    """A process-like object whose ``wait`` takes no timeout still exits."""

    manager = _manager(
        tmp_path,
        show_task=_show(_card),
        argv=[sys.executable, "-c", "pass"],
    )
    waits: list[int] = []

    def wait() -> int:
        waits.append(1)
        return 0

    legacy = SimpleNamespace(pid=99, wait=wait, poll=lambda: 0)
    live = _nf548_live(tmp_path, manager, _nf548_workspace(tmp_path), process=legacy)

    manager._await_exit_watching_zero_delta(live)

    assert waits == [1]
    assert _nf548_notices(manager) == []


def _nf548_failed_card(**overrides) -> dict:
    """A card carrying a recorded terminal failure pinned to its own content."""
    card = _card()
    card["objective"] = "NF548 identical relaunch guard"
    card["review_feedback"] = [{"reason": "validation_failed"}]
    card.update(overrides)
    card["terminal_failure"] = {
        "request_id": "predecessor-request-1",
        "error": "WorkspaceError: required_output_mismatch on every file",
        "runner": card["runner"],
        "adapter_id": "claude_cli",
        "card_content_sha256": process_launcher.card_content_identity(card),
        "review_feedback_identity": process_launcher.review_feedback_identity(card),
        "recorded_at": "2026-09-01T10:00:00+00:00",
    }
    return card


def _nf548_preflight(
    tmp_path: Path, card: dict, *, adapter_id: str = "claude_cli"
) -> dict:
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: card),
        argv=[sys.executable, "-c", "pass"],
    )
    return manager._preflight_card(
        card["task_id"], card["runner"], card["topic"], adapter_id
    )


def test_identical_relaunch_is_refused_with_a_named_deterministic_reason(
    tmp_path: Path,
) -> None:
    card = _nf548_failed_card()
    expected_hash = process_launcher.bounded_error_hash(
        card["terminal_failure"]["error"]
    )
    assert len(expected_hash) == process_launcher.TERMINAL_ERROR_HASH_HEX_CHARS

    with pytest.raises(process_launcher.LaunchRejected) as excinfo:
        _nf548_preflight(tmp_path, card)

    reason = str(excinfo.value)
    assert reason == (
        f"identical_relaunch_blocked:predecessor-request-1:{expected_hash}"
    )
    # The refusal is pure: the guard runs before anything is claimed.
    assert card["status"] == "pending"
    assert card["worker_status"] == "unclaimed"


def test_identical_relaunch_refusal_is_the_same_reason_every_time() -> None:
    card = _nf548_failed_card()
    first = process_launcher.identical_relaunch_refusal(
        card, runner=card["runner"], adapter_id="claude_cli"
    )
    second = process_launcher.identical_relaunch_refusal(
        dict(card), runner=card["runner"], adapter_id="claude_cli"
    )
    assert first == second and first.startswith("identical_relaunch_blocked:")


def test_identical_relaunch_guard_accepts_a_recorded_error_hash(tmp_path: Path) -> None:
    card = _nf548_failed_card()
    card["terminal_failure"]["error_hash"] = "0123456789abcdef"
    card["terminal_failure"].pop("error")

    with pytest.raises(
        process_launcher.LaunchRejected,
        match="identical_relaunch_blocked:predecessor-request-1:0123456789abcdef",
    ):
        _nf548_preflight(tmp_path, card)


def _nf548_terminal_retry_card() -> dict:
    card = _nf548_failed_card()
    card["terminal_retry"] = {
        "predecessor_request_id": "predecessor-request-1",
        "recorded_at": "2026-09-01T10:00:17+00:00",
    }
    return card


def _nf548_changed_feedback_card() -> dict:
    card = _nf548_failed_card()
    card["review_feedback"] = [{"reason": "scope_violation"}]
    return card


def _nf548_changed_runner_card() -> dict:
    card = _nf548_failed_card()
    card["terminal_failure"]["runner"] = "claude_worker_other"
    return card


def _nf548_changed_adapter_card() -> dict:
    card = _nf548_failed_card()
    card["terminal_failure"]["adapter_id"] = "codex_cli"
    return card


def _nf548_changed_content_card() -> dict:
    card = _nf548_failed_card()
    card["objective"] = "NF548 identical relaunch guard, now with a new objective"
    return card


@pytest.mark.parametrize(
    "card_fn",
    [
        _nf548_terminal_retry_card,
        _nf548_changed_feedback_card,
        _nf548_changed_runner_card,
        _nf548_changed_adapter_card,
        _nf548_changed_content_card,
    ],
    ids=[
        "explicit_terminal_retry",
        "changed_review_feedback_reason",
        "changed_runner",
        "changed_adapter",
        "changed_card_content",
    ],
)
def test_identical_relaunch_guard_permits_every_changed_input(
    tmp_path: Path, card_fn
) -> None:
    card = card_fn()
    assert (
        process_launcher.identical_relaunch_refusal(
            card, runner=card["runner"], adapter_id="claude_cli"
        )
        == ""
    )
    assert _nf548_preflight(tmp_path, card)["task_id"] == card["task_id"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"request_id": ""},
        {"error": "   "},
        {"card_content_sha256": ""},
        {"review_feedback_identity": ""},
    ],
    ids=["no_predecessor", "no_error_text", "unpinned_card", "unpinned_feedback"],
)
def test_identical_relaunch_guard_fails_open_on_unpinned_evidence(
    tmp_path: Path, mutation: dict
) -> None:
    card = _nf548_failed_card()
    card["terminal_failure"].update(mutation)
    assert (
        process_launcher.identical_relaunch_refusal(
            card, runner=card["runner"], adapter_id="claude_cli"
        )
        == ""
    )
    assert _nf548_preflight(tmp_path, card)["task_id"] == card["task_id"]


def test_identical_relaunch_guard_reads_the_latest_recorded_failure() -> None:
    card = _nf548_failed_card()
    latest = dict(card["terminal_failure"])
    latest["request_id"] = "predecessor-request-2"
    latest["recorded_at"] = "2026-09-01T11:00:00+00:00"
    card["terminal_failure"] = [dict(card["terminal_failure"]), latest]

    refusal = process_launcher.identical_relaunch_refusal(
        card, runner=card["runner"], adapter_id="claude_cli"
    )
    assert refusal.split(":")[1] == "predecessor-request-2"


def test_identical_relaunch_guard_ignores_launches_without_a_failure_record(
    tmp_path: Path,
) -> None:
    card = _card()
    assert (
        process_launcher.identical_relaunch_refusal(
            card, runner=card["runner"], adapter_id="claude_cli"
        )
        == ""
    )
    assert _nf548_preflight(tmp_path, card)["task_id"] == card["task_id"]


def test_card_content_identity_ignores_non_contract_bookkeeping() -> None:
    card = _nf548_failed_card()
    identity = process_launcher.card_content_identity(card)
    noisy = dict(card)
    noisy["updated_at"] = "2026-09-01T12:00:00+00:00"
    noisy["card_json"] = json.dumps(card)
    noisy["terminal_failure"] = {"request_id": "unrelated"}
    assert process_launcher.card_content_identity(noisy) == identity

    changed = dict(card)
    changed["allowed_writes"] = ["out/other.json"]
    assert process_launcher.card_content_identity(changed) != identity


def test_review_feedback_identity_tracks_only_the_reason() -> None:
    base = {"review_feedback": [{"reason": "validation_failed", "detail": "one"}]}
    same_reason = {"review_feedback": [{"reason": "validation_failed", "detail": "two"}]}
    other_reason = {"review_feedback": [{"reason": "scope_violation"}]}
    assert process_launcher.review_feedback_identity(
        base
    ) == process_launcher.review_feedback_identity(same_reason)
    assert process_launcher.review_feedback_identity(
        base
    ) != process_launcher.review_feedback_identity(other_reason)
    assert process_launcher.review_feedback_identity(
        {}
    ) == process_launcher.review_feedback_identity({"review_feedback": None})


def test_bounded_error_hash_is_whitespace_stable_and_bounded() -> None:
    assert process_launcher.bounded_error_hash("  a   b\n") == (
        process_launcher.bounded_error_hash("a b")
    )
    assert process_launcher.bounded_error_hash("") == ""
    assert process_launcher.bounded_error_hash(None) == ""
    assert len(process_launcher.bounded_error_hash("boom")) == (
        process_launcher.TERMINAL_ERROR_HASH_HEX_CHARS
    )


# ── worker validation affordances (sandbox parity) ────────────────────────


def _host_interpreter_is_advertisable() -> str:
    """Why this host's own interpreter cannot back the affordance, or ""."""
    resolved = Path(sys.executable).resolve(strict=True)
    info = resolved.stat()
    if platform_io.posix_path_modes_supported(os.name) and stat.S_IMODE(info.st_mode) & 0o002:
        return f"host interpreter {resolved} is world-writable"
    if os.name != "nt" and info.st_uid != os.getuid() and info.st_uid != 0:
        return f"host interpreter {resolved} is owned by uid {info.st_uid}"
    return ""


def _fake_canonical_venv(repo: Path) -> None:
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    # Production shape: the venv python is a symlink chain ending at the
    # coordinator's own interpreter, which is the only authenticated escape.
    (venv_bin / "python").symlink_to(Path(sys.executable).resolve())
    for name in ("ruff", "mypy"):
        tool = venv_bin / name
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="posix venv layout")
def test_worker_launch_env_exposes_canonical_validation_affordances(tmp_path):
    # The affordance is only exposed when the interpreter behind it passes the
    # same verification the finalization validator applies. A GitHub-hosted
    # runner installs a WORLD-WRITABLE Python, which that verification refuses
    # on purpose, so this contract cannot be demonstrated there -- and the test
    # asserted it anyway, failing in CI for a day with nothing but KeyError
    # because worker_validation_affordance_env swallows the reason by design.
    unusable = _host_interpreter_is_advertisable()
    if unusable:
        pytest.skip(f"affordance cannot be demonstrated here: {unusable}")
    repo = tmp_path / "repo"
    repo.mkdir()
    _fake_canonical_venv(repo)
    # Verify first, so a host that refuses names the guard instead of a KeyError.
    worker_workspace._verify_validation_interpreter(
        repo / ".venv" / "bin" / "python",
        repo,
        authenticated_external_endpoint=Path(sys.executable).resolve(strict=True),
    )
    env = process_launcher.worker_launch_env(
        "claude_cli", repo=repo, request_id="req-affordance"
    )
    assert env["AIWORKHUB_CANONICAL_PYTHON"] == str(repo / ".venv" / "bin" / "python")
    assert env["AIWORKHUB_CANONICAL_RUFF"] == str(repo / ".venv" / "bin" / "ruff")
    assert env["AIWORKHUB_CANONICAL_MYPY"] == str(repo / ".venv" / "bin" / "mypy")
    # Caches land inside the request-owned writable temp, never the worktree.
    assert env["RUFF_CACHE_DIR"] == str(Path(env["TMPDIR"]) / "ruff-cache")
    assert env["MYPY_CACHE_DIR"] == str(Path(env["TMPDIR"]) / "mypy-cache")
    assert env["PYTEST_ADDOPTS"] == "-p no:cacheprovider"
    # The sanitized PATH is untouched: the affordance is an explicit variable,
    # never a PATH widening.
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"


@pytest.mark.skipif(os.name == "nt", reason="posix venv layout")
def test_worker_launch_env_spells_affordances_for_bubblewrap_alias(tmp_path):
    unusable = _host_interpreter_is_advertisable()
    if unusable:
        pytest.skip(f"affordance cannot be demonstrated here: {unusable}")
    repo = tmp_path / "repo"
    repo.mkdir()
    _fake_canonical_venv(repo)
    env = process_launcher.worker_launch_env(
        "claude_cli",
        repo=repo,
        request_id="req-bwrap",
        sandbox_backend="bubblewrap",
    )
    assert env["AIWORKHUB_CANONICAL_PYTHON"] == "/authority-repo/.venv/bin/python"
    assert env["AIWORKHUB_CANONICAL_RUFF"] == "/authority-repo/.venv/bin/ruff"


@pytest.mark.skipif(os.name == "nt", reason="posix venv layout")
def test_worker_launch_env_omits_missing_or_untrusted_affordances(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # No .venv at all: nothing is advertised, launch is not a failure, and the
    # cache routing still applies.
    env = process_launcher.worker_launch_env(
        "claude_cli", repo=repo, request_id="req-novenv"
    )
    assert "AIWORKHUB_CANONICAL_PYTHON" not in env
    assert "AIWORKHUB_CANONICAL_RUFF" not in env
    assert env["PYTEST_ADDOPTS"] == "-p no:cacheprovider"
    # A ruff that escapes the venv root by symlink is untrusted and omitted.
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    outside = tmp_path / "outside-ruff"
    outside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside.chmod(0o755)
    (venv_bin / "ruff").symlink_to(outside)
    env = process_launcher.worker_launch_env(
        "claude_cli", repo=repo, request_id="req-escape"
    )
    assert "AIWORKHUB_CANONICAL_RUFF" not in env


def test_worker_runtime_policy_names_sandbox_validation_facts():
    from aiworkhub import agent_tool_instructions

    policy = agent_tool_instructions.render_worker_runtime_policy()
    assert "SANDBOX_VALIDATION_FACTS" in policy
    assert "$AIWORKHUB_CANONICAL_PYTHON" in policy
    assert "validation_unsupported_in_sandbox:" in policy
    assert "never stub a denied call" in policy


def test_status_names_why_the_task_card_was_not_read(monkeypatch, tmp_path):
    """A card that was never read must not report as a card that is absent.

    Measured 2026-09-02 on AIWORKHUB_01082_REVIEW_CORRECTNESS_V1: the reservation
    landed at 22:17:24, the store claimed the card at 22:17:39, and this surface
    still answered ``task_state="unknown"`` with an empty ``task_card`` -- the
    same verdict it gives for a card that genuinely does not exist. Keeping the
    read bounded during preparation is right; reporting it as ignorance is not.
    """

    reads: list[str] = []

    def show(task_id):
        reads.append(task_id)
        return _show(
            lambda: _card(task_id="TASK_CARD_READ", state="processing")
        )(task_id)

    manager = _manager(
        tmp_path,
        show_task=show,
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event({
        "request_id": "status-card-read",
        "task_id": "TASK_CARD_READ",
        "runner": "claude_worker_b1",
        "topic": "quality_review",
        "adapter_id": "claude_cli",
        "state": "starting",
    })

    deferred = manager.status("status-card-read")

    # The bounded read is preserved: the store is not touched at all.
    assert reads == []
    assert deferred["task_card"] is None
    assert deferred["task_state"] == "unknown"
    assert deferred["task_card_read"] == "deferred_pid_null_starting_reservation"

    manager._append_event({
        "request_id": "status-card-read",
        "task_id": "TASK_CARD_READ",
        "runner": "claude_worker_b1",
        "state": "running",
        "pid": 4321,
        "pid_start_ticks": 99,
    })

    live = manager.status("status-card-read")

    assert reads == ["TASK_CARD_READ"]
    assert live["task_card_read"] == "read"
    assert live["task_state"] != "unknown"

    # collect() is the surface a manager actually reads. A reason that stops at
    # status() is a reason nobody sees -- which is how the confusion happened.
    assert manager.collect("status-card-read")["task_card_read"] == "read"


def test_status_distinguishes_a_failed_card_read_from_an_absent_card(
    monkeypatch,
    tmp_path,
):
    """``read_failed`` and ``no card`` are different facts; the bare except hid both."""

    def show(_task_id):
        raise RuntimeError("store unavailable")

    manager = _manager(
        tmp_path,
        show_task=show,
        argv=[sys.executable, "-c", "pass"],
    )
    manager._append_event({
        "request_id": "status-card-failed",
        "task_id": "TASK_CARD_FAILED",
        "runner": "claude_worker_b1",
        "state": "running",
        "pid": 4322,
        "pid_start_ticks": 98,
    })

    result = manager.status("status-card-failed")

    assert result["task_card"] is None
    assert result["task_state"] == "unknown"
    assert result["task_card_read"] == "read_failed:RuntimeError"


def test_validation_capability_preflight_is_complete_sorted_and_read_only(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        lambda argv, repo: (["/definitely/missing-validator", *argv[1:]], ()),
    )
    card = {
        "allowed_writes": [],
        "validation": ["tool --version", "cd missing-cwd && tool check"],
    }

    observed = worker_workspace.preflight_validation_capabilities(tmp_path, card)

    assert observed == tuple(sorted(observed))
    assert observed == (
        "cwd:missing-cwd",
        "executable:/definitely/missing-validator",
    )
    assert list(tmp_path.iterdir()) == []


def test_validation_capability_preflight_rejects_before_launch_side_effects(
    monkeypatch, tmp_path
):
    card = _card()
    card["validation"] = ["missing-validator check"]
    monkeypatch.setattr(
        worker_workspace,
        "preflight_validation_capabilities",
        lambda repo, observed: ("executable:/missing-validator",),
    )
    manager = _manager(
        tmp_path,
        show_task=_show(lambda: card),
        argv=[sys.executable, "-c", "pass"],
    )

    with pytest.raises(
        process_launcher.LaunchRejected,
        match=r'^task_contract_unwinnable:\["executable:/missing-validator"\]$',
    ):
        manager._preflight_card(
            card["task_id"], card["runner"], card["topic"], "claude_cli"
        )

    assert card["status"] == "pending"
    assert card["worker_status"] == "unclaimed"
    assert not manager.process_log_path.exists()
    assert not manager.process_dir.exists()


def test_process_manager_consumes_authority_before_collision_or_provider_launch(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    authority = _RejectingToolchainAuthority()
    side_effects: list[str] = []
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(lambda: _card()),
        collision_guard=lambda **_kwargs: side_effects.append("collision") or {},
        adapter_builder=lambda **_kwargs: side_effects.append("provider"),
        isolation_enabled=False,
        toolchain_authority=authority,
    )

    with pytest.raises(
        process_launcher.LaunchRejected,
        match=r'^task_contract_unwinnable:\["module:pytest"\]$',
    ):
        manager._preflight_card(
            "TASK_B1", "claude_worker_b1", "task_mcp", "claude_cli"
        )

    assert authority.repairs == 1
    assert side_effects == []
    assert not (tmp_path / "events.jsonl").exists()


@pytest.mark.parametrize(
    ("command", "expected_module"),
    [
        ("python3 -m pytest -q tests/test_process_launcher.py", "pytest"),
        ("python3 -m ruff check src", "ruff"),
        ("python3 -m mypy src", "mypy"),
        ("python3 -m coverage run -m pytest", "coverage"),
        ("node tools/check.js", None),
        ("npm test", None),
    ],
)
def test_validation_capability_preflight_accepts_supported_command_forms(
    monkeypatch, tmp_path, command, expected_module
):
    probed_modules = []
    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_roots",
        lambda argv, repo: ([sys.executable, *argv[1:]], ()),
    )
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_root_supplies_validator_module",
        lambda executable, module: probed_modules.append(module) or True,
    )
    monkeypatch.setattr(
        worker_workspace, "_declared_workspace_seed_closure", lambda *args: ((), (), ())
    )

    assert worker_workspace.preflight_validation_capabilities(
        tmp_path, {"allowed_writes": [], "validation": [command]}
    ) == ()
    assert probed_modules == ([] if expected_module is None else [expected_module])
