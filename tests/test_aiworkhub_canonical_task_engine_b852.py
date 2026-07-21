"""B852: canonical, repo-local task-engine coverage for core.py.

Every lifecycle path exercised here (health/list/show/auto_pickup/
claim_start_exact/mark_review/mark_done/reject_review/collision_guard/
usage_report/callback_outbox_status) must resolve exclusively through
``aiworkhub.task_store`` against a per-repo canonical
``.aiworkhub/tasking/task_queue.sqlite`` -- never a subprocess, never
``AITools/taskctl.py`` or ``AITools/taskdb.py``. A repo directory with no
``AITools/`` at all must still pass the full lifecycle.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import aiworkhub  # noqa: E402
from aiworkhub import core, task_store  # noqa: E402


NOW = "2026-07-20T00:00:00+00:00"


def _init_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    # Sanity: this test repo genuinely has no AITools/ directory at all.
    assert not (root / "AITools").exists()
    return root


def _insert_task(
    root: Path,
    task_id: str,
    runner: str,
    topic: str,
    *,
    worker_status: str = "unclaimed",
    status: str = "pending",
    claimed_by: str | None = None,
    card_extra: dict | None = None,
) -> None:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    card_json = json.dumps(card_extra or {}, ensure_ascii=False)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, runner, topic, "solo", status, worker_status, "normal",
                "objective", card_json, NOW, NOW, claimed_by,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row(root: Path, task_id: str) -> dict:
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return dict(row)


@pytest.fixture(autouse=True)
def _forbid_subprocess(monkeypatch):
    """No converted core.py lifecycle function may ever launch a subprocess."""

    def _forbid(*args, **kwargs):
        raise AssertionError(f"subprocess.run must not be called; got args={args!r}")

    monkeypatch.setattr(subprocess, "run", _forbid)
    yield


def _assert_no_legacy_module_imported(before: set[str]) -> None:
    after = set(sys.modules)
    leaked = {
        m for m in (after - before)
        if "taskctl" in m or ("taskdb" in m and "task_store" not in m)
    }
    assert not leaked, f"forbidden legacy module imported: {leaked}"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "repo_a")
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.delenv("AIWORKHUB_ALLOW_WRITES", raising=False)
    yield root


@pytest.fixture
def writable_repo(repo, monkeypatch):
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    yield repo


def _coordinator_env(tmp_path, monkeypatch, token="s3cr3t-coordinator-token"):
    token_path = tmp_path / "coordinator.token"
    token_path.write_text(token + "\n", encoding="utf-8")
    os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", token)
    return token_path


# ---------------------------------------------------------------------------
# Read-only paths
# ---------------------------------------------------------------------------


def test_health_no_aitools_dir(repo):
    before = set(sys.modules)
    result = core.health()
    _assert_no_legacy_module_imported(before)
    assert result["ok"] is True
    assert result["storage"]["ready"] is True
    assert result["writes_allowed"] is False


def test_list_and_show_task(repo):
    _insert_task(repo, "TASK_A", "claude_coding", "coding")
    before = set(sys.modules)
    listed = core.list_tasks(status="pending", topic="coding", limit=10)
    _assert_no_legacy_module_imported(before)
    assert listed["ok"] is True
    # Stdout stays taskctl's compact "[bucket] [topic] [runner] task_id" line
    # format -- dashboard.py / completion_inbox.py regex-parse this directly.
    assert "[pending] [coding] [claude_coding] TASK_A" in listed["stdout"]

    shown = core.show_task("TASK_A")
    assert shown["ok"] is True
    card = json.loads(shown["stdout"])
    assert card["task_id"] == "TASK_A"
    assert card["status"] == "pending"

    missing = core.show_task("NO_SUCH_TASK")
    assert missing["ok"] is True
    assert "Task not found:" in missing["stdout"]


def test_pending_for_runner_jsonl_rows(repo):
    _insert_task(repo, "TASK_B", "claude_coding", "coding")
    result = core.pending_for_runner("claude_coding", topic="coding")
    assert result["ok"] is True
    assert any(r["task_id"] == "TASK_B" for r in result["jsonl_rows"])


def test_callback_outbox_status_redacted(repo):
    result = core.callback_outbox_status()
    assert result["ok"] is True
    stats = json.loads(result["stdout"])
    assert "by_state" in stats and "bound_task_count" in stats


def test_collision_guard_no_active_cards(repo):
    result = core.collision_guard(print_json=True)
    assert result["ok"] is True
    assert result["stdout"] == "No cards to scan."


# ---------------------------------------------------------------------------
# Write gate
# ---------------------------------------------------------------------------


def test_write_gate_closed_blocks_auto_pickup(repo):
    _insert_task(repo, "TASK_C", "claude_coding", "coding")
    result = core.auto_pickup("claude_coding", "coding")
    assert result["ok"] is False
    assert result["returncode"] == 126
    row = _row(repo, "TASK_C")
    assert row["worker_status"] == "unclaimed"


def test_write_gate_denies_unknown_runner_topic(writable_repo):
    _insert_task(writable_repo, "TASK_D", "totally_unknown_runner", "unknown_topic")
    result = core.auto_pickup("totally_unknown_runner", "unknown_topic")
    assert result["ok"] is False
    assert "allowlist denied" in result["stderr"]


# ---------------------------------------------------------------------------
# Full lifecycle: auto_pickup -> mark_review -> mark_done
# ---------------------------------------------------------------------------


def test_full_lifecycle_auto_pickup_review_done(writable_repo, tmp_path, monkeypatch):
    _coordinator_env(tmp_path, monkeypatch)
    _insert_task(writable_repo, "TASK_E", "claude_coding", "coding")

    before = set(sys.modules)
    picked = core.auto_pickup("claude_coding", "coding")
    _assert_no_legacy_module_imported(before)
    assert picked["ok"] is True
    card = json.loads(picked["stdout"])
    assert card["task_id"] == "TASK_E"
    row = _row(writable_repo, "TASK_E")
    assert row["worker_status"] == "claimed"
    assert row["status"] == "processing"
    assert row["claimed_by"] == "claude_coding"

    reviewed = core.mark_review("TASK_E")
    assert reviewed["ok"] is True
    row = _row(writable_repo, "TASK_E")
    assert row["worker_status"] == "review"
    assert row["status"] == "review"

    done = core.mark_done("TASK_E", runner="codex")
    assert done["ok"] is True, done
    row = _row(writable_repo, "TASK_E")
    assert row["worker_status"] == "done"
    assert row["status"] == "finished"
    assert row["completed_at"]


def test_claim_start_exact_and_reject_review(writable_repo, tmp_path, monkeypatch):
    _coordinator_env(tmp_path, monkeypatch)
    _insert_task(writable_repo, "TASK_F", "claude_coding", "coding")

    claimed = core.claim_start_exact("TASK_F", "claude_coding", "coding")
    assert claimed["ok"] is True
    row = _row(writable_repo, "TASK_F")
    assert row["worker_status"] == "claimed"

    reviewed = core.mark_review("TASK_F")
    assert reviewed["ok"] is True

    rejected = core.reject_review("TASK_F", "not_good_enough")
    assert rejected["ok"] is True, rejected
    row = _row(writable_repo, "TASK_F")
    assert row["worker_status"] == "unclaimed"
    assert row["status"] == "pending"


def test_mark_done_requires_coordinator_token(writable_repo, tmp_path, monkeypatch):
    # The coordinator token cache is process-global by design (it is
    # scrubbed from os.environ exactly once); reset it explicitly so an
    # earlier test's configured token cannot leak into this negative case.
    monkeypatch.setattr(aiworkhub, "_coordinator_token", "", raising=False)
    monkeypatch.setattr(aiworkhub, "_coordinator_token_file", "", raising=False)
    _insert_task(writable_repo, "TASK_G", "claude_coding", "coding")
    picked = core.auto_pickup("claude_coding", "coding")
    assert picked["ok"] is True
    core.mark_review("TASK_G")

    # No coordinator token configured -> denied.
    denied = core.mark_done("TASK_G", runner="codex")
    assert denied["ok"] is False
    row = _row(writable_repo, "TASK_G")
    assert row["worker_status"] == "review"


def test_usage_report_empty_no_events(repo):
    result = core.usage_report()
    assert result["ok"] is True
    assert result["stdout"] == "No usage records."


def test_run_taskctl_compat_dispatcher_no_aitools_or_subprocess(writable_repo):
    _insert_task(writable_repo, "TASK_RUN_COMPAT", "claude_coding", "coding")

    verify = core.run_taskctl(["verify"])
    assert verify.returncode == 0
    assert "task_queue.sqlite" in verify.stdout
    assert "AITools" not in " ".join(verify.command)

    listed = core.run_taskctl(["list", "--status", "pending", "--topic", "coding"])
    assert listed.returncode == 0
    assert "[pending] [coding] [claude_coding] TASK_RUN_COMPAT" in listed.stdout
    assert "AITools" not in " ".join(listed.command)

    claimed = core.run_taskctl(
        [
            "claim-start",
            "TASK_RUN_COMPAT",
            "--runner",
            "claude_coding",
            "--topic",
            "coding",
        ],
        allow_write=True,
        runner="claude_coding",
        topic="coding",
    )
    assert claimed.returncode == 0, claimed.stderr

    reviewed = core.run_taskctl(
        ["review", "TASK_RUN_COMPAT", "--runner", "claude_coding", "--topic", "coding"],
        allow_write=True,
        runner="claude_coding",
        topic="coding",
    )
    assert reviewed.returncode == 0, reviewed.stderr

    queue = core.run_taskctl(["review-queue"])
    assert queue.returncode == 0
    assert "=== Codex Review Queue (1) ===" in queue.stdout
    assert "  [coding] [claude_coding] TASK_RUN_COMPAT" in queue.stdout
    assert "AITools" not in " ".join(queue.command)


def test_run_taskctl_usage_records_native_event(writable_repo):
    _insert_task(
        writable_repo,
        "TASK_USAGE_COMPAT",
        "claude_coding",
        "coding",
        worker_status="claimed",
        status="processing",
        claimed_by="claude_coding",
    )

    usage = core.run_taskctl(
        [
            "usage",
            "TASK_USAGE_COMPAT",
            "--runner",
            "claude_coding",
            "--topic",
            "coding",
            "--model",
            "test-model",
            "--provider",
            "test-provider",
            "--source",
            "test",
            "--input-tokens",
            "10",
            "--output-tokens",
            "5",
            "--total-tokens",
            "15",
            "--cost-usd",
            "0.01",
        ],
        allow_write=True,
        runner="claude_coding",
        topic="coding",
    )
    assert usage.returncode == 0, usage.stderr

    report = core.run_taskctl(["usage-report", "--runner", "claude_coding"])
    assert report.returncode == 0
    assert "TASK_USAGE_COMPAT" in report.stdout
    assert "tokens=15" in report.stdout


# ---------------------------------------------------------------------------
# Isolation across repos with identical task_ids
# ---------------------------------------------------------------------------


def test_two_repos_same_task_id_stay_isolated(tmp_path, monkeypatch):
    root_a = _init_repo(tmp_path, "repo_x")
    root_b = _init_repo(tmp_path, "repo_y")

    _insert_task(root_a, "SAME_TASK_ID", "claude_coding", "coding", card_extra={"marker": "a"})
    _insert_task(root_b, "SAME_TASK_ID", "claude_coding", "coding", card_extra={"marker": "b"})

    monkeypatch.setenv("AIWORKHUB_REPO", str(root_a))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    picked_a = core.auto_pickup("claude_coding", "coding")
    assert picked_a["ok"] is True
    card_a = json.loads(picked_a["stdout"])
    assert card_a["marker"] == "a"

    row_a = _row(root_a, "SAME_TASK_ID")
    assert row_a["worker_status"] == "claimed"
    row_b_untouched = _row(root_b, "SAME_TASK_ID")
    assert row_b_untouched["worker_status"] == "unclaimed"

    monkeypatch.setenv("AIWORKHUB_REPO", str(root_b))
    picked_b = core.auto_pickup("claude_coding", "coding")
    assert picked_b["ok"] is True
    card_b = json.loads(picked_b["stdout"])
    assert card_b["marker"] == "b"
    row_b = _row(root_b, "SAME_TASK_ID")
    assert row_b["worker_status"] == "claimed"
    # repo_a's row is unaffected by repo_b's claim.
    row_a_again = _row(root_a, "SAME_TASK_ID")
    assert row_a_again["worker_status"] == "claimed"
    assert row_a_again["claimed_by"] == "claude_coding"
