from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import (
    core,
    process_launcher,
    task_store,
    terminal_failure_classification,
    toolchain_authority,
    workforce_catalog,
    worker_workspace,
)


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
    failure_class: str = "",
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
            "evidence": {
                "request_id": request_id,
                "error": f"{substatus}:exact",
                # Absent unless a test asks for one: the launcher writes this
                # key only where the classifier actually ran, and "absent" is
                # a different fact from any class it could have placed.
                **({"failure_class": failure_class} if failure_class else {}),
            },
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
    card_overrides: dict | None = None,
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
    if card_overrides is not None:
        card.update(card_overrides)
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


def _retained_predecessor(
    repo: Path,
    *,
    task_id: str,
    request_id: str = "a" * 32,
    content: bytes = b"retained candidate\n",
) -> dict:
    workspace_root = worker_workspace.configured_worktree_root(repo) / request_id
    worktree = workspace_root / "worktree"
    home = workspace_root / "home"
    output = worktree / "out" / "result.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    changed_digest = hashlib.sha256(content).hexdigest()

    artifact_bytes = b"{}"
    artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
    artifact_path = (
        worker_workspace.configured_runtime_root(repo)
        / "rework_deltas"
        / f"{artifact_digest}.json"
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(artifact_bytes)
    claim_epoch = 1
    return {
        "schema_id": "aiworkhub.rework_predecessor.v1",
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "changed_path_hashes": {"out/result.json": changed_digest},
        "workspace": {
            "request_id": request_id,
            "repo": str(repo),
            "path": str(worktree),
            "home": str(home),
            "allowed_writes": ["out/result.json"],
            "parent_baseline": {},
            "workspace_baseline": {},
            "base_oid": "b" * 40,
        },
        "rework_delta": {
            "schema_id": "aiworkhub.rework_delta_descriptor.v1",
            "sealed": True,
            "authority_repo": str(repo.resolve()),
            "task_id": task_id,
            "request_id": request_id,
            "claim_epoch": claim_epoch,
            "artifact_path": str(artifact_path),
            "artifact_sha256": artifact_digest,
        },
        "delta_artifact": {
            "path": str(artifact_path),
            "digest": artifact_digest,
        },
    }


def _manager_rejection_fields(predecessor: dict) -> dict:
    pinned_at = "2026-08-03T00:01:00+00:00"
    request_id = predecessor["request_id"]
    instruction = "repair the rejected candidate through another provider"
    predecessor["pinned_at"] = pinned_at
    return {
        "claim_epoch": predecessor["claim_epoch"],
        "rejection_disposition": {
            "schema_id": "aiworkhub.rejection_disposition.v1",
            "failure_category": "candidate_code",
            "request_id": request_id,
            "to": "pending",
            "pinned_at": pinned_at,
        },
        "review_feedback": {
            "schema_id": "aiworkhub.rework_feedback_delta.v1",
            "instruction": instruction,
            "reason_identity": {
                "bytes": len(instruction.encode("utf-8")),
                "sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
                "truncated": False,
            },
            "predecessor_request_id": request_id,
            "predecessor_changed_paths": sorted(
                predecessor["changed_path_hashes"]
            ),
            "residual_identities": [],
        },
    }


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
        # This test owns identity rerouting, not validation capability
        # discovery.  Its synthetic repository intentionally has no Python
        # project or test tree, so inject an available authority snapshot.
        toolchain_authority=toolchain_authority.ToolchainAuthority(
            coordinator_repo, capability_probe=lambda *_: (),
        ),
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


def test_reroute_launch_identity_uses_current_manager_rejection_over_stale_retry(
    coordinator_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "REROUTE_REJECTED_GLM_TO_DEEPSEEK"
    predecessor = _retained_predecessor(coordinator_repo, task_id=task_id)
    manager_fields = _manager_rejection_fields(predecessor)
    monkeypatch.setattr(
        worker_workspace,
        "changed_paths",
        lambda _workspace, **_kwargs: ["out/result.json"],
    )
    monkeypatch.setattr(
        workforce_catalog,
        "build_catalog",
        lambda _repo: {
            "workers": [{
                "execution_runner": "deepseek_v4-pro",
                "effective_adapter_id": "deepseek_vscode_lm",
                "model": "deepseek-v4-pro",
                "enabled": True,
                "launch_eligible": True,
                "available": True,
                "max_risk": "critical",
            }]
        },
    )
    _insert_pending_reroutable(
        coordinator_repo,
        task_id=task_id,
        runner="glm_5.3",
        rework_predecessor=predecessor,
        risk_tier="high",
        card_overrides=manager_fields,
    )

    result = core.reroute_launch_identity(
        task_id,
        from_runner="glm_5.3",
        to_runner="deepseek_v4-pro",
        to_adapter_id="deepseek_vscode_lm",
        to_model="deepseek-v4-pro",
    )

    assert result["ok"] is True, result
    row = _row(coordinator_repo, task_id)
    assert row["runner"] == "deepseek_v4-pro"
    card = json.loads(row["card_json"])
    assert card["terminal_retry"]["request_id"] == "r" * 32
    assert card["review_feedback"] == manager_fields["review_feedback"]
    assert card["rework_predecessor"] == predecessor
    authorization = card["identity_reroute"][
        "manager_rejection_authorization"
    ]
    assert authorization["task_id"] == task_id
    assert authorization["request_id"] == predecessor["request_id"]
    assert authorization["claim_epoch"] == predecessor["claim_epoch"]
    assert result["manager_rejection_authorization"] == authorization


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("missing", "reroute_manager_rejection_provenance_missing"),
        ("stale", "reroute_manager_rejection_identity_mismatch"),
        ("cross_task", "reroute_manager_rejection_identity_mismatch"),
        ("unsealed", "reroute_retained_candidate_delta_unverified"),
        ("tampered", "reroute_retained_candidate_hash_mismatch"),
    ],
)
def test_reroute_launch_identity_rejects_invalid_manager_rework_provenance(
    coordinator_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_error: str,
) -> None:
    task_id = f"REROUTE_REJECTED_BAD_{mutation.upper()}"
    predecessor = _retained_predecessor(coordinator_repo, task_id=task_id)
    manager_fields = _manager_rejection_fields(predecessor)
    if mutation == "missing":
        manager_fields.pop("rejection_disposition")
    elif mutation == "stale":
        manager_fields["rejection_disposition"]["request_id"] = "c" * 32
    elif mutation == "cross_task":
        predecessor["task_id"] = "OTHER_TASK"
        predecessor["rework_delta"]["task_id"] = "OTHER_TASK"
    elif mutation == "unsealed":
        predecessor["rework_delta"]["sealed"] = False
    elif mutation == "tampered":
        Path(predecessor["workspace"]["path"], "out/result.json").write_text(
            "tampered\n", encoding="utf-8"
        )
    monkeypatch.setattr(
        worker_workspace,
        "changed_paths",
        lambda _workspace, **_kwargs: ["out/result.json"],
    )
    _insert_pending_reroutable(
        coordinator_repo,
        task_id=task_id,
        runner="glm_5.3",
        rework_predecessor=predecessor,
        card_overrides=manager_fields,
    )

    result = core.reroute_launch_identity(
        task_id,
        from_runner="glm_5.3",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="claude-sonnet-5",
    )

    assert result["ok"] is False
    assert expected_error in result["stderr"]
    assert _row(coordinator_repo, task_id)["runner"] == "glm_5.3"


def test_reroute_launch_identity_preserves_retained_candidate_delta(
    coordinator_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "REROUTE_RETAINED_DELTA"
    predecessor = _retained_predecessor(coordinator_repo, task_id=task_id)
    monkeypatch.setattr(
        worker_workspace,
        "changed_paths",
        lambda _workspace, **_kwargs: ["out/result.json"],
    )
    _insert_pending_reroutable(
        coordinator_repo,
        task_id=task_id,
        rework_predecessor=predecessor,
    )

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
    )

    assert result["ok"] is True, result
    row = _row(coordinator_repo, task_id)
    assert row["runner"] == "claude_sonnet-5"
    card = json.loads(row["card_json"])
    assert card["rework_predecessor"] == predecessor
    receipt = card["identity_reroute"]
    assert receipt["retained_candidate_preserved"] is True
    assert receipt["retained_predecessor_request_id"] == "a" * 32
    assert receipt["retained_base_oid"] == "b" * 40
    assert receipt["retained_claim_epoch"] == 1
    assert receipt["retained_changed_path_count"] == 1
    expected_digest = hashlib.sha256(
        json.dumps(
            predecessor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert receipt["retained_predecessor_sha256"] == expected_digest


def test_reroute_launch_identity_uses_sealed_delta_after_worktree_retention(
    coordinator_repo: Path,
) -> None:
    task_id = "REROUTE_RETAINED_SEALED_DELTA"
    content = b"retained candidate\n"
    predecessor = _retained_predecessor(
        coordinator_repo, task_id=task_id, content=content
    )
    artifact = worker_workspace.seal_rework_delta_artifact(
        authority_repo=coordinator_repo,
        task_id=task_id,
        request_id=predecessor["request_id"],
        claim_epoch=predecessor["claim_epoch"],
        file_entries=[("out/result.json", content)],
        artifact_dir=(
            worker_workspace.configured_runtime_root(coordinator_repo)
            / "rework_deltas"
        ),
    )
    predecessor["delta_artifact"] = artifact
    predecessor["rework_delta"].update(
        artifact_path=artifact["path"], artifact_sha256=artifact["digest"]
    )
    worktree = Path(predecessor["workspace"]["path"])
    (worktree / "out/result.json").unlink()
    (worktree / "out").rmdir()
    worktree.rmdir()
    _insert_pending_reroutable(
        coordinator_repo, task_id=task_id, rework_predecessor=predecessor
    )

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
    )

    assert result["ok"] is True, result
    row = _row(coordinator_repo, task_id)
    assert row["runner"] == "claude_sonnet-5"
    card = json.loads(row["card_json"])
    assert card["rework_predecessor"] == predecessor


def test_reroute_launch_identity_rejects_tampered_retained_bytes(
    coordinator_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "REROUTE_RETAINED_TAMPERED"
    predecessor = _retained_predecessor(coordinator_repo, task_id=task_id)
    Path(predecessor["workspace"]["path"], "out/result.json").write_text(
        "tampered\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        worker_workspace,
        "changed_paths",
        lambda _workspace, **_kwargs: ["out/result.json"],
    )
    _insert_pending_reroutable(
        coordinator_repo, task_id=task_id, rework_predecessor=predecessor
    )

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
    )

    assert result["ok"] is False
    assert "reroute_retained_candidate_hash_mismatch" in result["stderr"]
    assert _row(coordinator_repo, task_id)["runner"] == "claude_sonnet-4.6"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("task_id", "OTHER_TASK", "identity_mismatch"),
        ("claim_epoch", 2, "delta_unverified"),
    ],
)
def test_reroute_launch_identity_rejects_retained_identity_drift(
    coordinator_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    reason: str,
) -> None:
    task_id = f"REROUTE_RETAINED_{field.upper()}"
    predecessor = _retained_predecessor(coordinator_repo, task_id=task_id)
    predecessor[field] = value
    monkeypatch.setattr(
        worker_workspace,
        "changed_paths",
        lambda _workspace, **_kwargs: ["out/result.json"],
    )
    _insert_pending_reroutable(
        coordinator_repo, task_id=task_id, rework_predecessor=predecessor
    )

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner="claude_sonnet-5",
        to_adapter_id="claude_cli",
        to_model="sonnet",
    )

    assert result["ok"] is False
    assert f"reroute_retained_candidate_{reason}" in result["stderr"]
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


def test_reroute_launch_identity_accepts_available_catalog_route(
    coordinator_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "REROUTE_CATALOG_DEEPSEEK"
    _insert_pending_reroutable(
        coordinator_repo, task_id=task_id, risk_tier="high"
    )
    monkeypatch.setattr(
        workforce_catalog,
        "build_catalog",
        lambda _repo: {
            "workers": [
                {
                    "execution_runner": "deepseek_v4-pro",
                    "effective_adapter_id": "deepseek_vscode_lm",
                    "model": "deepseek-v4-pro",
                    "enabled": True,
                    "launch_eligible": True,
                    "available": True,
                    "max_risk": "high",
                }
            ]
        },
    )

    result = core.reroute_launch_identity(
        task_id,
        from_runner="claude_sonnet-4.6",
        to_runner="deepseek_v4-pro",
        to_adapter_id="deepseek_vscode_lm",
        to_model="deepseek-v4-pro",
    )

    assert result["ok"] is True, result
    assert _row(coordinator_repo, task_id)["runner"] == "deepseek_v4-pro"


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


# Audit item #5.  The class vocabulary an unattended retry may not act on, and
# the reason each is refused.  Read from ``terminal_failure_classification``
# rather than restated, so a class renamed there fails this test instead of
# silently becoming a class nothing refuses.
_REFUSED_CLASSES = (
    terminal_failure_classification.FAILURE_CLASS_TRANSIENT,
    terminal_failure_classification.FAILURE_CLASS_CREDENTIAL,
    terminal_failure_classification.FAILURE_CLASS_DEFECT,
    terminal_failure_classification.FAILURE_CLASS_UNKNOWN,
)


@pytest.mark.parametrize("failure_class", _REFUSED_CLASSES)
def test_automatic_retry_refuses_every_class_the_classifier_placed(
    coordinator_repo: Path, failure_class: str
) -> None:
    task_id = f"AUTO_REFUSED_{failure_class.upper()}"
    request_id = (failure_class[0] * 32)[:32]
    _insert_blocked(
        coordinator_repo,
        task_id=task_id,
        request_id=request_id,
        substatus="finalize_failed",
        failure_class=failure_class,
    )

    result = core.retry_terminal_task(
        task_id, request_id, "finalize_failed", "reaper", automatic=True
    )

    assert result["ok"] is False, result
    assert "terminal_retry_automatic_refused" in result["stderr"]
    assert f"failure_class_{failure_class}" in result["stderr"]
    assert _row(coordinator_repo, task_id)["status"] == "blocked"


def test_automatic_retry_proceeds_when_no_classifier_ran(
    coordinator_repo: Path,
) -> None:
    """An absent class is not ``unknown``.

    Measured on this repository's canonical store: every reaper-minted terminal
    failure, and all 45 blocked ``review_protocol:*`` reviewer cards, carry no
    ``failure_class`` key at all -- the classifier never saw those outcomes.
    Refusing on absence would refuse exactly the class of card the automatic
    retry exists to recover.
    """

    task_id = "AUTO_NO_RECORDED_CLASS"
    request_id = "n" * 32
    _insert_blocked(
        coordinator_repo,
        task_id=task_id,
        request_id=request_id,
        substatus="finalize_failed",
    )

    result = core.retry_terminal_task(
        task_id, request_id, "finalize_failed", "reaper", automatic=True
    )

    assert result["ok"] is True, result
    assert _row(coordinator_repo, task_id)["status"] == "pending"


def test_an_unnamed_class_is_refused_rather_than_treated_as_permission(
    coordinator_repo: Path,
) -> None:
    task_id = "AUTO_UNPLACED_CLASS"
    request_id = "u" * 32
    _insert_blocked(
        coordinator_repo,
        task_id=task_id,
        request_id=request_id,
        substatus="finalize_failed",
        failure_class="a_class_nobody_reasoned_about",
    )

    result = core.retry_terminal_task(
        task_id, request_id, "finalize_failed", "reaper", automatic=True
    )

    assert result["ok"] is False, result
    assert "failure_class_unplaced" in result["stderr"]
    assert _row(coordinator_repo, task_id)["status"] == "blocked"


def test_a_manager_holding_the_evidence_is_not_gated(coordinator_repo: Path) -> None:
    """The gate is on unattended retries only, never on the manager surface.

    ``aiworkhub_task_retry_terminal`` is a verified manager acting with the
    evidence in hand; refusing it here would take away the only surface that
    can override a classification.
    """

    task_id = "MANUAL_DEFECT_RETRY"
    request_id = "m" * 32
    _insert_blocked(
        coordinator_repo,
        task_id=task_id,
        request_id=request_id,
        substatus="finalize_failed",
        failure_class=terminal_failure_classification.FAILURE_CLASS_DEFECT,
    )

    result = core.retry_terminal_task(task_id, request_id, "finalize_failed", "manual")

    assert result["ok"] is True, result
    assert _row(coordinator_repo, task_id)["status"] == "pending"


def test_recorded_failure_class_reads_the_card_and_never_recomputes() -> None:
    assert core.recorded_failure_class({}) == ""
    assert core.recorded_failure_class({"terminal_failure": "not-a-mapping"}) == ""
    assert core.recorded_failure_class({"terminal_failure": {"evidence": []}}) == ""
    assert (
        core.recorded_failure_class(
            {"terminal_failure": {"evidence": {"failure_class": "defect"}}}
        )
        == "defect"
    )
