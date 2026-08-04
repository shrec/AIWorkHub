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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import aiworkhub  # noqa: E402
from aiworkhub import callback_store, core, task_store  # noqa: E402


NOW = "2026-07-20T00:00:00+00:00"


def test_manager_origin_identity_accepts_modern_uuid_versions():
    assert core._UUID_RE.fullmatch("019f5097-6dbe-7172-870a-945afc5f3bfa")
    assert core._UUID_RE.fullmatch("019f5097-6dbe-8172-870a-945afc5f3bfa")
    assert not core._UUID_RE.fullmatch("019f5097-6dbe-9172-870a-945afc5f3bfa")


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


def test_collision_guard_uses_native_aiworkhub_store_not_repo_scripts(repo):
    scripts = repo / "scripts"
    scripts.mkdir()
    poison = scripts / "build_tasking_parallel_group_collision_guard_v1.py"
    poison.write_text(
        "def scan_collisions(cards):\n"
        "    raise RuntimeError('repo script must not be imported')\n",
        encoding="utf-8",
    )
    _insert_task(
        repo,
        "TASK_NATIVE_COLLISION",
        "claude_sonnet5",
        "aiworkhub_canary",
        card_extra={"allowed_writes": ["research/native_collision.json"]},
    )

    result = core.collision_guard(print_json=True)

    assert result["ok"] is True
    payload = json.loads(result["stdout"].split("\n\n", 1)[1])
    assert payload["schema_id"] == "aiworkhub.task_collision_report.v1"
    assert payload["source"] == "canonical_task_store"
    assert payload["cards_source"].endswith("/.aiworkhub/tasking/task_queue.sqlite")
    assert "bitnnv2/data/tasking" not in payload["cards_source"]


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


def test_mark_review_enqueues_repo_local_callback(writable_repo):
    thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    _insert_task(
        writable_repo,
        "TASK_CALLBACK",
        "claude_coding",
        "coding",
        card_extra={"origin_thread_id": thread_id},
    )

    picked = core.auto_pickup("claude_coding", "coding")
    assert picked["ok"] is True
    reviewed = core.mark_review("TASK_CALLBACK")
    assert reviewed["ok"] is True
    assert reviewed["callback_enqueued"] is True

    db_path = callback_store.resolve_db_path(writable_repo)
    conn = callback_store.open_db(db_path)
    try:
        row = conn.execute(
            "SELECT task_id, origin_thread_id, transition, state, episode_id "
            "FROM callback_outbox WHERE task_id=?",
            ("TASK_CALLBACK",),
        ).fetchone()
        assert callback_store._task_still_in_matching_terminal_state(
            conn, "TASK_CALLBACK", "review_ready", row["episode_id"]
        ) is True
    finally:
        conn.close()
    assert row is not None
    assert dict(row) == {
        "task_id": "TASK_CALLBACK",
        "origin_thread_id": thread_id,
            "transition": "review_ready",
            "state": "pending",
            "episode_id": "1",
        }


def test_mark_review_preserves_immutable_task_origin_across_reviewer_identity(
    writable_repo, monkeypatch
):
    """The task's persisted origin_thread_id names the chat that authored
    it. A different Claude manager session merely reviewing the task must
    never overwrite that origin with its own session id -- doing so would
    let one window silently steal another window's callback ownership."""
    author_thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    _insert_task(
        writable_repo,
        "TASK_ORIGIN_REVIEW",
        "claude_coding",
        "coding",
        card_extra={"origin_thread_id": author_thread_id},
    )
    picked = core.auto_pickup("claude_coding", "coding")
    assert picked["ok"] is True

    reviewer_session_id = "5be44029-03da-4683-aae3-c68ecb07b1a4"
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {
            "provider": "claude",
            "session_id": reviewer_session_id,
            "window_id": "claude_vscode_other_window",
        },
    )
    reviewed = core.mark_review("TASK_ORIGIN_REVIEW")
    assert reviewed["ok"] is True
    assert reviewed["callback_enqueued"] is True

    db_path = callback_store.resolve_db_path(writable_repo)
    conn = callback_store.open_db(db_path)
    try:
        outbox_row = conn.execute(
            "SELECT origin_thread_id FROM callback_outbox WHERE task_id=?",
            ("TASK_ORIGIN_REVIEW",),
        ).fetchone()
    finally:
        conn.close()
    assert outbox_row is not None
    assert outbox_row["origin_thread_id"] == author_thread_id
    assert outbox_row["origin_thread_id"] != reviewer_session_id


def test_release_launch_preserves_immutable_task_origin_across_reviewer_identity(
    writable_repo, tmp_path, monkeypatch
):
    _coordinator_env(tmp_path, monkeypatch)
    author_thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    _insert_task(
        writable_repo,
        "TASK_ORIGIN_RELEASE",
        "claude_coding",
        "coding",
        card_extra={"origin_thread_id": author_thread_id},
    )
    picked = core.auto_pickup("claude_coding", "coding")
    assert picked["ok"] is True

    releaser_session_id = "6c7cbfc4-98c1-4ad6-9d47-24e5a3f1a002"
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {
            "provider": "claude",
            "session_id": releaser_session_id,
            "window_id": "claude_vscode_releaser_window",
        },
    )
    released = core.release_launch(
        "TASK_ORIGIN_RELEASE", "claude_coding", "blocked_on_dependency"
    )
    assert released["ok"] is True, released
    assert released["callback_enqueued"] is True

    db_path = callback_store.resolve_db_path(writable_repo)
    conn = callback_store.open_db(db_path)
    try:
        outbox_row = conn.execute(
            "SELECT origin_thread_id FROM callback_outbox WHERE task_id=?",
            ("TASK_ORIGIN_RELEASE",),
        ).fetchone()
    finally:
        conn.close()
    assert outbox_row is not None
    assert outbox_row["origin_thread_id"] == author_thread_id
    assert outbox_row["origin_thread_id"] != releaser_session_id


def test_callback_batches_are_partitioned_by_originating_provider(writable_repo):
    _insert_task(writable_repo, "TASK_CODEX_ROUTE", "codex_worker", "coding")
    _insert_task(writable_repo, "TASK_CLAUDE_ROUTE", "claude_worker", "coding")
    conn = callback_store.open_db(callback_store.resolve_db_path(writable_repo))
    try:
        callback_store.init_db(conn)
        conn.execute(
            "UPDATE tasks SET status='review', worker_status='review' "
            "WHERE task_id IN ('TASK_CODEX_ROUTE','TASK_CLAUDE_ROUTE')"
        )
        conn.commit()
        assert callback_store.enqueue_callback(
            conn, "TASK_CODEX_ROUTE", "codex-thread", "review_ready", provider="codex"
        )
        assert callback_store.enqueue_callback(
            conn, "TASK_CLAUDE_ROUTE", "claude-session", "review_ready", provider="claude"
        )
        claude = callback_store.claim_pending_callback_batch(conn, provider="claude")
        assert claude is not None
        assert [m["task_id"] for m in claude["members"]] == ["TASK_CLAUDE_ROUTE"]
        callback_store.mark_batch_delivered(conn, claude["batch_id"])
        codex = callback_store.claim_pending_callback_batch(conn, provider="codex")
        assert codex is not None
        assert [m["task_id"] for m in codex["members"]] == ["TASK_CODEX_ROUTE"]
    finally:
        conn.close()


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


def test_claim_start_exact_repairs_empty_denormalized_topic(writable_repo):
    _insert_task(
        writable_repo,
        "TASK_CARD_IDENTITY",
        "claude_coding",
        "",
        card_extra={"runner": "claude_coding", "topic": "coding"},
    )

    claimed = core.claim_start_exact(
        "TASK_CARD_IDENTITY", "claude_coding", "coding"
    )
    assert claimed["ok"] is True
    row = _row(writable_repo, "TASK_CARD_IDENTITY")
    assert row["runner"] == "claude_coding"
    assert row["topic"] == "coding"
    assert row["status"] == "processing"
    assert json.loads(row["card_json"])["claim_epoch"] == 1


def test_mark_done_requires_coordinator_token(writable_repo, tmp_path, monkeypatch):
    # The coordinator token cache is process-global by design (it is
    # scrubbed from os.environ exactly once); reset it explicitly so an
    # earlier test's configured token cannot leak into this negative case.
    monkeypatch.setattr(aiworkhub, "_coordinator_token", "", raising=False)
    monkeypatch.setattr(aiworkhub, "_coordinator_token_file", "", raising=False)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    # Issue 1: Init Repo now seeds a repo-local coordinator token that would
    # itself grant capability. Remove it too so this negative case has NEITHER
    # a repo-local nor an env token -> genuinely denied.
    (writable_repo / ".aiworkhub" / "runtime" / "coordinator.token").unlink(missing_ok=True)
    _insert_task(writable_repo, "TASK_G", "claude_coding", "coding")
    picked = core.auto_pickup("claude_coding", "coding")
    assert picked["ok"] is True
    core.mark_review("TASK_G")

    # No coordinator token configured -> denied.
    denied = core.mark_done("TASK_G", runner="codex")
    assert denied["ok"] is False
    row = _row(writable_repo, "TASK_G")
    assert row["worker_status"] == "review"


def test_manager_create_task_derives_route_and_never_overwrites(writable_repo, monkeypatch):
    session_id = "5be44029-03da-4683-aae3-c68ecb07b1a4"
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {"provider": "claude", "session_id": session_id, "window_id": "claude_vscode_123"},
    )
    created = core.create_task(
        task_id="TASK_MANAGER_CREATE",
        title="Manager-created task",
        runner="claude_worker",
        topic="coding",
        objective="Prove canonical MCP task creation.",
        acceptance=["created once"],
        allowed_writes=["src/example.py"],
        forbidden=["secrets/**"],
        validation=["python -m pytest -q"],
        max_live_tokens=250_000,
    )
    assert created["ok"] is True, created
    card = json.loads(created["stdout"])
    assert card["coordinator_provider"] == "claude"
    assert card["origin_thread_id"] == session_id
    assert card["token_budget"] == {
        "schema_id": "aiworkhub.task_token_budget.v1",
        "cap_tokens": 250_000,
        "enforcement": "live_when_provider_reports_usage",
    }
    stored = _row(writable_repo, "TASK_MANAGER_CREATE")
    assert stored["worker_status"] == "unclaimed"
    assert stored["origin_thread_id"] == session_id

    reconciled = core.create_task(
        task_id="TASK_MANAGER_CREATE",
        title="Manager-created task",
        runner="claude_worker",
        topic="coding",
        objective="Prove canonical MCP task creation.",
        acceptance=["created once"],
        allowed_writes=["src/example.py"],
        forbidden=["secrets/**"],
        validation=["python -m pytest -q"],
        max_live_tokens=250_000,
    )
    assert reconciled["ok"] is True, reconciled
    assert reconciled["created"] is False
    assert reconciled["reconciled"] is True
    assert reconciled["receipt_state"] == "existing_identical"

    duplicate = core.create_task(
        task_id="TASK_MANAGER_CREATE",
        title="Must not overwrite",
        runner="claude_worker",
        topic="coding",
        objective="Must be rejected.",
        acceptance=["rejected"],
        allowed_writes=[],
        validation=["python -m pytest -q"],
    )
    assert duplicate["ok"] is False
    assert "task_already_exists" in duplicate["stderr"]
    assert "title" in duplicate["conflict_fields"]


def test_manager_create_rejects_mutating_code_task_without_validation(
    writable_repo, monkeypatch
):
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {
            "provider": "claude",
            "session_id": "5be44029-03da-4683-aae3-c68ecb07b1a4",
            "window_id": "claude_vscode_123",
        },
    )

    result = core.create_task(
        task_id="TASK_CODE_WITHOUT_VALIDATION",
        title="Unsafe task",
        runner="claude_worker",
        topic="coding",
        objective="Change code without a behavioral check.",
        acceptance=["changed"],
        allowed_writes=["src/example.py"],
    )

    assert result["ok"] is False
    assert result["stderr"] == "code_task_validation_required"


def test_manager_create_rejects_validation_syntax_worker_cannot_execute(
    writable_repo, monkeypatch
):
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {
            "provider": "claude",
            "session_id": "5be44029-03da-4683-aae3-c68ecb07b1a4",
            "window_id": "claude_vscode_123",
        },
    )

    result = core.create_task(
        task_id="TASK_INVALID_VALIDATION_SYNTAX",
        title="Reject impossible validation",
        runner="claude_worker",
        topic="coding",
        objective="Fail before spending a provider run.",
        acceptance=["Rejected at creation."],
        allowed_writes=["docs/result.md"],
        validation=[
            "python -c \"from pathlib import Path; assert Path('docs/result.md').exists()\""
        ],
    )

    assert result["ok"] is False
    assert result["stderr"].startswith(
        "invalid_validation_command:validation_shell_syntax_forbidden:"
    )
    assert result["validation_index"] == 0
    assert result["supported_validation_examples"]
    assert task_store.get_task(writable_repo, "TASK_INVALID_VALIDATION_SYNTAX") is None


def test_manager_create_rejects_required_output_prose_before_provider_launch(
    writable_repo, monkeypatch
):
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {
            "provider": "claude",
            "session_id": "5be44029-03da-4683-aae3-c68ecb07b1a4",
            "window_id": "claude_vscode_123",
        },
    )

    result = core.create_task(
        task_id="TASK_REQUIRED_OUTPUT_PROSE",
        title="Reject prose output contract",
        runner="claude_worker",
        topic="coding",
        objective="Do not spend a provider run on a malformed card.",
        acceptance=["Write a useful report."],
        allowed_writes=["reports/result.md"],
        required_outputs=["A concise report explaining the result"],
        validation=["python -m pytest -q"],
    )

    assert result["ok"] is False
    assert result["stderr"].startswith("required_output_not_allowed:")
    assert result["required_output_index"] == 0
    assert "put prose requirements in acceptance" in result["contract_hint"]
    assert task_store.get_task(writable_repo, "TASK_REQUIRED_OUTPUT_PROSE") is None


def test_concurrent_create_and_lost_ack_retry_reconcile_once(writable_repo, monkeypatch):
    session_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {"provider": "claude", "session_id": session_id, "window_id": "claude_vscode_123"},
    )

    def create_once():
        return core.create_task(
            task_id="TASK_LOST_ACK_RETRY",
            title="Idempotent transport recovery",
            runner="claude_worker",
            topic="coding",
            objective="Commit once and reconcile every identical retry.",
            acceptance=["one canonical row", "durable receipt"],
            allowed_writes=["src/example.py"],
            validation=["python -m pytest -q"],
        )

    # Model three overlapping MCP writes.  Exactly one creates the row; the
    # other callers receive successful reconciliation receipts.
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: create_once(), range(3)))
    assert all(result["ok"] is True for result in results), results
    assert sum(result["created"] is True for result in results) == 1
    assert sum(result["reconciled"] is True for result in results) == 2

    # Drop/ignore the first acknowledgement and retry the same request, as a
    # client must after Transport closed.  The retry is a success receipt and
    # the database still contains one task and one created event.
    retry = create_once()
    assert retry["ok"] is True
    assert retry["created"] is False
    assert retry["receipt_state"] == "existing_identical"

    readiness = task_store.storage_readiness(writable_repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_id='TASK_LOST_ACK_RETRY'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id='TASK_LOST_ACK_RETRY' AND event='created'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_manager_bootstrap_advertises_create_and_callback_contract(writable_repo, monkeypatch):
    monkeypatch.setattr(
        core,
        "_codex_manager_identity",
        lambda: {
            "provider": "codex",
            "session_id": "5be44029-03da-4683-aae3-c68ecb07b1a4",
            "thread_id": "5be44029-03da-4683-aae3-c68ecb07b1a4",
            "window_id": "codex_vscode_123",
        },
    )
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    result = core.manager_bootstrap()
    assert result["role"] == "manager"
    contract = result["operating_contract"]
    assert contract["schema_id"] == "aiworkhub.manager_operating_contract.v1"
    assert contract["mandatory"] is True
    assert "Creating a task leaves it pending" in contract["banner"]
    assert "only then may runtime truth become processing" in contract["task_state_machine"]["launch"]
    assert "Parallel launch is encouraged" in contract["parallelism"][0]
    assert "task category does not suppress delivery" in contract["callbacks_and_review"][0]
    assert "same task_id" in contract["recovery"][1]
    assert result["workflow"][0].startswith("aiworkhub_manager_source_graph_query")
    assert "aiworkhub_task_create" in result["workflow"]
    assert "aiworkhub_claude_callback_wait" in result["callback"]["claude"]


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
            "--cached-input-tokens",
            "4",
            "--cache-creation-input-tokens",
            "2",
            "--cache-metrics-observed",
            "--cost-usd",
            "0.01",
            "--cost-observed",
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
    [event] = task_store.list_usage_events(writable_repo)
    assert event["cached_input_tokens"] == 4
    assert event["cache_creation_input_tokens"] == 2
    assert event["cache_metrics_observed"] is True
    assert event["cost_observed"] is True


def test_run_taskctl_usage_preserves_unobserved_attempt_without_fake_zero(writable_repo):
    _insert_task(
        writable_repo,
        "TASK_USAGE_UNKNOWN",
        "glm_worker",
        "coding",
        worker_status="claimed",
        status="processing",
        claimed_by="glm_worker",
    )

    usage = core.run_taskctl(
        [
            "usage",
            "TASK_USAGE_UNKNOWN",
            "--runner",
            "glm_worker",
            "--topic",
            "coding",
            "--model",
            "glm-5.2",
            "--provider",
            "vscode_lm",
            "--source",
            "task_mcp_launcher",
            "--note",
            "task_mcp_request:req-unknown",
        ],
        allow_write=True,
        runner="glm_worker",
        topic="coding",
    )
    assert usage.returncode == 0, usage.stderr

    report = core.usage_report(runner="glm_worker")
    assert report["record_count"] == 1
    assert report["usage_observed_records"] == 0
    assert report["usage_unknown_records"] == 1
    assert report["cost_observed_records"] == 0
    assert "tokens=unknown" in report["stdout"]
    assert "cost=unknown" in report["stdout"]
    assert "$0.0000" not in report["stdout"]
    [event] = task_store.list_usage_events(writable_repo)
    assert event["usage_observed"] is False
    assert event["cost_observed"] is False


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
