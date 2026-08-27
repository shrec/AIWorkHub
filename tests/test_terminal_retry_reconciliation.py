from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import core, process_launcher, task_store


@pytest.fixture
def coordinator_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token_path = tmp_path / "coordinator.token"
    token_path.write_text("coordinator-token\n", encoding="utf-8")
    os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coordinator-token")
    return repo


def _insert_blocked(
    repo: Path,
    *,
    task_id: str,
    request_id: str,
    substatus: str,
) -> None:
    readiness = task_store.storage_readiness(repo)
    now = "2026-08-03T00:00:00+00:00"
    card = {
        "task_id": task_id,
        "runner": "worker_runner",
        "topic": "terminal_retry",
        "objective": "retry exact operational failure",
        "status": "blocked",
        "worker_status": substatus,
        "claimed_by": "worker_runner",
        "claim_epoch": 7,
        "launch_request_id": request_id,
        "terminal_substatus": substatus,
        "terminal_outcome": substatus,
        "blocker_reason": f"{substatus}:exact",
        "blocked_at": now,
        "blocked_by": "worker_runner",
        "terminal_failure": {
            "substatus": substatus,
            "evidence": {"request_id": request_id, "error": f"{substatus}:exact"},
        },
        "review_feedback": {"schema_id": "aiworkhub.rework_feedback_delta.v1"},
        "rework_predecessor": {"schema_id": "aiworkhub.rework_predecessor.v1"},
    }
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,claimed_by,claimed_at,started_at,completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                "worker_runner",
                "terminal_retry",
                "solo",
                "blocked",
                substatus,
                "normal",
                "retry exact operational failure",
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                now,
                now,
                "worker_runner",
                now,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row(repo: Path, task_id: str) -> sqlite3.Row:
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


@pytest.mark.parametrize(
    "substatus",
    [
        "cancelled",
        "timed_out",
        "output_budget_exceeded",
        "launch_failed",
        "worker_failed",
        "finalize_failed",
        "process_lost",
        "liveness_lost",
    ],
)
def test_retry_terminal_requeues_only_exact_operational_episode(
    coordinator_repo: Path, substatus: str
) -> None:
    task_id = f"RETRY_{substatus.upper()}"
    request_id = (substatus[0] * 32)[:32]
    _insert_blocked(
        coordinator_repo,
        task_id=task_id,
        request_id=request_id,
        substatus=substatus,
    )

    result = core.retry_terminal_task(task_id, request_id, substatus, "route repaired")

    assert result["ok"] is True, result
    row = _row(coordinator_repo, task_id)
    assert row["status"] == "pending"
    assert row["worker_status"] == "unclaimed"
    assert row["claimed_by"] is None
    assert row["claimed_at"] is None
    assert row["started_at"] is None
    assert row["completed_at"] is None
    card = json.loads(row["card_json"])
    assert card["claim_epoch"] == 7
    assert card["review_feedback"]["schema_id"].endswith(".v1")
    assert card["rework_predecessor"]["schema_id"].endswith(".v1")
    assert card["terminal_retry"]["request_id"] == request_id
    assert card["terminal_retry"]["terminal_substatus"] == substatus
    for cleared in (
        "launch_request_id",
        "terminal_failure",
        "terminal_substatus",
        "terminal_outcome",
        "blocker_reason",
        "blocked_at",
        "blocked_by",
    ):
        assert cleared not in card

    repeated = core.retry_terminal_task(task_id, request_id, substatus, "route repaired")
    assert repeated["ok"] is True
    assert repeated["idempotent"] is True


def test_retry_terminal_rejects_wrong_request_without_mutation(
    coordinator_repo: Path,
) -> None:
    _insert_blocked(
        coordinator_repo,
        task_id="RETRY_WRONG_REQUEST",
        request_id="a" * 32,
        substatus="worker_failed",
    )

    result = core.retry_terminal_task(
        "RETRY_WRONG_REQUEST", "b" * 32, "worker_failed"
    )

    assert result["ok"] is False
    assert "request_mismatch" in result["stderr"]
    row = _row(coordinator_repo, "RETRY_WRONG_REQUEST")
    assert row["status"] == "blocked"
    assert row["worker_status"] == "worker_failed"


@pytest.mark.parametrize("substatus", ["validation_failed", "scope_rejected", "review_ready"])
def test_retry_terminal_rejects_semantic_or_review_outcomes(
    coordinator_repo: Path, substatus: str
) -> None:
    task_id = f"RETRY_FORBIDDEN_{substatus.upper()}"
    _insert_blocked(
        coordinator_repo,
        task_id=task_id,
        request_id="c" * 32,
        substatus=substatus,
    )

    result = core.retry_terminal_task(task_id, "c" * 32, substatus)

    assert result["ok"] is False
    assert "substatus_not_operational" in result["stderr"]
    assert _row(coordinator_repo, task_id)["status"] == "blocked"


def _insert_pending_reroutable(
    repo: Path,
    *,
    task_id: str,
    runner: str = "claude_sonnet-4.6",
    topic: str = "terminal_retry",
    status: str = "pending",
    worker_status: str = "unclaimed",
    claimed_by: str | None = None,
    terminal_retry: dict | None = "default",  # type: ignore[assignment]
    rework_predecessor: dict | None = None,
    risk_tier: str | None = None,
) -> None:
    readiness = task_store.storage_readiness(repo)
    now = "2026-08-03T00:00:00+00:00"
    if terminal_retry == "default":
        terminal_retry = {
            "schema_id": "aiworkhub.terminal_retry.v1",
            "request_id": "r" * 32,
            "terminal_substatus": "worker_failed",
            "reason": "route repaired",
            "retried_at": now,
        }
    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "objective": "retry exact operational failure",
        "status": status,
        "worker_status": worker_status,
        "claimed_by": claimed_by,
        "allowed_writes": ["out/result.json"],
        "forbidden": ["do not modify core.py"],
        "required_outputs": ["out/result.json"],
        "validation": ["python -m pytest tests/test_process_launcher.py"],
        "template_id": "nf398-terminal-retry",
        "template_provenance": {"schema_id": "aiworkhub.template_provenance.v1"},
        "history": [{"event": "terminal_retry_requeued"}],
    }
    if terminal_retry is not None:
        card["terminal_retry"] = terminal_retry
    if rework_predecessor is not None:
        card["rework_predecessor"] = rework_predecessor
    if risk_tier is not None:
        card["risk_tier"] = risk_tier
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,claimed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                runner,
                topic,
                "solo",
                status,
                worker_status,
                "normal",
                "retry exact operational failure",
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                now,
                now,
                claimed_by,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_reroute_launch_identity_repairs_invalid_pinned_runner(
    coordinator_repo: Path,
) -> None:
    task_id = "REROUTE_OK"
    _insert_pending_reroutable(coordinator_repo, task_id=task_id, runner="claude_sonnet-4.6")

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
        reason="route repaired",
    )

    assert result["ok"] is True, result
    assert result["to_model"] == "claude-sonnet-5"
    row = _row(coordinator_repo, task_id)
    assert row["runner"] == "claude_sonnet-5"
    assert row["status"] == "pending"
    assert row["worker_status"] == "unclaimed"
    card = json.loads(row["card_json"])
    assert card["task_id"] == task_id
    assert card["topic"] == "terminal_retry"
    assert card["terminal_retry"]["request_id"] == "r" * 32
    receipt = card["identity_reroute"]
    assert receipt["schema_id"] == "aiworkhub.identity_reroute.v1"
    assert receipt["from_runner"] == "claude_sonnet-4.6"
    assert receipt["to_runner"] == "claude_sonnet-5"
    assert receipt["to_model"] == "claude-sonnet-5"


def test_terminal_retry_spark_card_requires_explicit_native_codex_reroute(
    coordinator_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "NF398_SPARK_RETRY"
    topic = "nf460_reroute_mcp_wiring"
    scope = ["out/result.json"]
    _insert_pending_reroutable(
        coordinator_repo,
        task_id=task_id,
        runner="codex_gpt-5.3-codex-spark",
        topic=topic,
        terminal_retry={
            "schema_id": "aiworkhub.terminal_retry.v1",
            "request_id": "9" * 32,
            "terminal_substatus": "worker_failed",
            "reason": "native route unavailable",
            "retried_at": "2026-08-03T00:00:00+00:00",
        },
        risk_tier="critical",
    )
    before = _row(coordinator_repo, task_id)
    before_card = json.loads(before["card_json"])
    before_card["allowed_writes"] = scope
    before_card["required_outputs"] = scope
    readiness = task_store.storage_readiness(coordinator_repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(before_card, ensure_ascii=False, sort_keys=True), task_id),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setattr(
        process_launcher.project_context,
        "collect_project_context",
        lambda *_: None,
    )
    manager = process_launcher.ProcessManager(
        repo=coordinator_repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=lambda requested: {
            "returncode": 0,
            "stdout": json.dumps(task_store.get_task(coordinator_repo, requested)),
            "stderr": "",
        },
        collision_guard=lambda **_: {"returncode": 0, "stdout": "{}", "stderr": ""},
        adapter_builder=lambda **_: SimpleNamespace(
            argv=[sys.executable, "-c", "pass"],
            cwd=str(coordinator_repo),
            launchable=True,
            reason="",
        ),
        isolation_enabled=False,
    )

    rejected = manager.launch(
        task_id=task_id,
        runner="codex_gpt-5.5",
        topic=topic,
        adapter_id="codex_cli",
        model="gpt-5.5",
        timeout_seconds=30,
    )

    assert rejected["ok"] is False
    assert "runner_mismatch:codex_gpt-5.3-codex-spark" in rejected["blocked_reason"]
    row = _row(coordinator_repo, task_id)
    assert row["runner"] == "codex_gpt-5.3-codex-spark"
    card = json.loads(row["card_json"])
    assert card["task_id"] == task_id
    assert card["topic"] == topic
    assert card["allowed_writes"] == scope
    assert card["template_id"] == "nf398-terminal-retry"
    assert card["history"] == [{"event": "terminal_retry_requeued"}]
    assert "identity_reroute" not in card

    rerouted = core.reroute_launch_identity(
        task_id,
        from_runner="codex_gpt-5.3-codex-spark",
        to_runner="codex_gpt-5.5",
        to_adapter_id="codex_cli",
        to_model="gpt-5.5",
        reason="explicit native route repair",
        topic=topic,
    )

    assert rerouted["ok"] is True, rerouted
    row = _row(coordinator_repo, task_id)
    assert row["runner"] == "codex_gpt-5.5"
    card = json.loads(row["card_json"])
    assert card["task_id"] == task_id
    assert card["topic"] == topic
    assert card["allowed_writes"] == scope
    assert card["required_outputs"] == scope
    assert card["template_id"] == "nf398-terminal-retry"
    assert card["history"] == [{"event": "terminal_retry_requeued"}]
    receipt = card["identity_reroute"]
    assert receipt["schema_id"] == "aiworkhub.identity_reroute.v1"
    assert receipt["from_runner"] == "codex_gpt-5.3-codex-spark"
    assert receipt["to_runner"] == "codex_gpt-5.5"
    assert receipt["to_adapter_id"] == "codex_cli"
    assert receipt["to_model"] == "gpt-5.5"
    assert len(json.dumps(receipt, ensure_ascii=False).encode("utf-8")) < 1024

    launched = manager.launch(
        task_id=task_id,
        runner="codex_gpt-5.5",
        topic=topic,
        adapter_id="codex_cli",
        model="gpt-5.5",
        timeout_seconds=30,
    )

    assert launched["ok"] is True, launched
    assert launched["runner"] == "codex_gpt-5.5"
    assert launched["adapter_id"] == "codex_cli"
    assert launched["model"] == "gpt-5.5"


def test_reroute_launch_identity_rejects_claimed_task(coordinator_repo: Path) -> None:
    task_id = "REROUTE_CLAIMED"
    _insert_pending_reroutable(
        coordinator_repo,
        task_id=task_id,
        status="processing",
        worker_status="claimed",
        claimed_by="claude_sonnet-4.6",
    )

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
    )

    assert result["ok"] is False
    assert "reroute_not_pending_unclaimed" in result["stderr"]


def test_reroute_launch_identity_requires_terminal_retry_provenance(
    coordinator_repo: Path,
) -> None:
    task_id = "REROUTE_NO_PROVENANCE"
    _insert_pending_reroutable(coordinator_repo, task_id=task_id, terminal_retry=None)

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
    )

    assert result["ok"] is False
    assert "reroute_requires_terminal_retry_provenance" in result["stderr"]


@pytest.mark.parametrize(
    "terminal_retry",
    [
        {},
        {
            "schema_id": "aiworkhub.terminal_retry.v0",
            "request_id": "r" * 32,
            "terminal_substatus": "worker_failed",
        },
        {
            "schema_id": "aiworkhub.terminal_retry.v1",
            "request_id": "",
            "terminal_substatus": "worker_failed",
        },
        {
            "schema_id": "aiworkhub.terminal_retry.v1",
            "request_id": "r" * 121,
            "terminal_substatus": "worker_failed",
        },
        {
            "schema_id": "aiworkhub.terminal_retry.v1",
            "request_id": "r" * 32,
            "terminal_substatus": "validation_failed",
        },
    ],
)
def test_reroute_launch_identity_rejects_malformed_or_nonoperational_retry(
    coordinator_repo: Path,
    terminal_retry: dict,
) -> None:
    task_id = "REROUTE_BAD_PROVENANCE"
    _insert_pending_reroutable(
        coordinator_repo,
        task_id=task_id,
        terminal_retry=terminal_retry,
    )

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
    )

    assert result["ok"] is False
    assert "reroute_requires_terminal_retry_provenance" in result["stderr"]
    row = _row(coordinator_repo, task_id)
    assert row["runner"] == "claude_sonnet-4.6"
    assert json.loads(row["card_json"])["terminal_retry"] == terminal_retry


def test_reroute_launch_identity_rejects_retained_candidate_delta(
    coordinator_repo: Path,
) -> None:
    task_id = "REROUTE_RETAINED_DELTA"
    _insert_pending_reroutable(
        coordinator_repo,
        task_id=task_id,
        rework_predecessor={
            "schema_id": "aiworkhub.rework_predecessor.v1",
            "changed_path_hashes": {"out/result.py": "deadbeef"},
        },
    )

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
    )

    assert result["ok"] is False
    assert "reroute_retained_candidate_delta" in result["stderr"]
    assert _row(coordinator_repo, task_id)["runner"] == "claude_sonnet-4.6"


def test_reroute_launch_identity_allows_stub_rework_predecessor_without_delta(
    coordinator_repo: Path,
) -> None:
    task_id = "REROUTE_STUB_PREDECESSOR"
    _insert_pending_reroutable(
        coordinator_repo,
        task_id=task_id,
        rework_predecessor={"schema_id": "aiworkhub.rework_predecessor.v1"},
    )

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
    )

    assert result["ok"] is True, result


@pytest.mark.parametrize(
    ("to_runner", "to_adapter_id", "to_model", "risk_tier"),
    [
        ("claude_haiku-4.5", "claude_cli", "haiku", "high"),  # insufficient risk
        ("claude_sonnet-4.6", "claude_cli", "claude-sonnet-4.6", None),  # route absent
        ("made_up_runner", "claude_cli", "made_up_model", None),  # arbitrary identity
        ("claude_sonnet-5", "codex_cli", "sonnet", None),  # wrong adapter
    ],
)
def test_reroute_launch_identity_fails_closed_for_bad_targets(
    coordinator_repo: Path,
    to_runner: str,
    to_adapter_id: str,
    to_model: str,
    risk_tier: str | None,
) -> None:
    task_id = f"REROUTE_BAD_{to_runner}_{to_adapter_id}"
    _insert_pending_reroutable(coordinator_repo, task_id=task_id, risk_tier=risk_tier)

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner=to_runner,
        to_adapter_id=to_adapter_id,
        to_model=to_model,
    )

    assert result["ok"] is False
    assert "reroute_target_rejected" in result["stderr"]
    assert _row(coordinator_repo, task_id)["runner"] == "claude_sonnet-4.6"


def test_reroute_launch_identity_rejects_stale_from_identity(
    coordinator_repo: Path,
) -> None:
    task_id = "REROUTE_STALE_FROM"
    _insert_pending_reroutable(coordinator_repo, task_id=task_id, runner="claude_sonnet-4.6")

    result = core.reroute_launch_identity(
        task_id,
        from_runner="a_different_pinned_runner",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
    )

    assert result["ok"] is False
    assert "reroute_from_identity_mismatch" in result["stderr"]


def test_reroute_launch_identity_fails_closed_on_concurrent_mutation(
    coordinator_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compare-and-swap race: the live card read by ``reroute_launch_identity``
    is stale by the time it commits, because a concurrent writer already
    mutated the row underneath it."""
    task_id = "REROUTE_RACE"
    _insert_pending_reroutable(coordinator_repo, task_id=task_id, runner="claude_sonnet-4.6")

    stale_card, error = core._live_card(task_id)
    assert error is None

    readiness = task_store.storage_readiness(coordinator_repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "UPDATE tasks SET updated_at=? WHERE task_id=?",
            ("2026-08-03T00:00:01+00:00", task_id),
        )
        mutated = dict(stale_card)
        mutated["reason_for_race"] = "concurrent-writer-touched-this-row"
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(mutated, ensure_ascii=False, sort_keys=True), task_id),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(core, "_live_card", lambda _task_id: (stale_card, None))

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
    )

    assert result["ok"] is False
    assert "reroute_transition_conflict" in result["stderr"]
    assert _row(coordinator_repo, task_id)["runner"] == "claude_sonnet-4.6"
