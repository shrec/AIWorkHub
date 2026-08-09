"""B882: repair exact-claim launch + auto-pickup for a card-scoped runner.

Two reproducible defects observed on B880, both fixed here without touching
``core.py`` (outside this task's ``allowed_writes``):

1. ``aiworkhub_agent_launch_task`` -> ``ProcessManager._launch_isolated`` ->
   (previously) ``core.claim_start_exact`` compared the bare ``tasks.topic``
   SQL column against the caller's exact topic. A task whose canonical topic
   lives only in ``card_json`` (the same class of task ``task_store.get_task``
   / ``list_tasks`` already tolerate via a COALESCE fallback -- see
   ``task_store.py``'s own migration comment) read back as an empty topic
   there, so an exact, fully-matching launch failed
   ``claim_start_failed:identity_mismatch``. Fixed by
   ``task_engine.claim_start_exact``, a repo-bound claim that normalizes the
   same way reads already do, called from
   ``ProcessManager._launch_isolated`` in place of the ambient
   ``core.claim_start_exact``.

2. ``aiworkhub_task_auto_pickup``'s public schema is ``(runner, topic=None)``
   -- it has no ``task_id`` parameter. For a card-scoped one-off runner/topic
   (not on the static ``core.RUNNER_TOPIC_ALLOWLIST``, authorized only to
   claim-start the exact pending card that already names it), ``core.auto_pickup``
   always failed ``card_scoped_task_id_required`` -- a denial reason the
   public tool can never satisfy by construction. Fixed in
   ``server.aiworkhub_task_auto_pickup``: on that exact denial it resolves
   the single eligible pending task_id itself (the same candidate
   ``aiworkhub_task_auto_pickup_dryrun`` already reports, read-only) and
   claims it through claim-start, the authority path this identity already
   qualifies for.

Every process-launch test here uses a fake/injected Popen -- ``python -c
'pass'`` or an in-repo script -- never a real Claude/Codex CLI adapter or any
paid model call.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, process_launcher, server, task_engine, task_store, worker_workspace  # noqa: E402

NOW = "2026-07-21T00:00:00+00:00"

# Deliberately absent from core.RUNNER_TOPIC_ALLOWLIST -- exercises the
# card-scoped (exact one-off) authority path, not the static allowlist.
CARD_RUNNER = "claude_sonnet5_aiworkhub_exact_task_launch_claim_b882_test"
CARD_TOPIC = "task_mcp"


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


def _insert_task(
    root: Path,
    task_id: str,
    runner: str,
    topic: str,
    *,
    worker_status: str = "unclaimed",
    status: str = "pending",
    claimed_by: str | None = "",
    topic_column: str | None = None,
    card_extra: dict | None = None,
) -> None:
    """Insert a canonical row. ``topic_column`` lets a test simulate the
    older-writer defect: the SQL ``topic`` column left at its schema default
    ('') while the real topic only lives in ``card_json`` -- exactly the
    shape ``task_store.get_task``/``list_tasks`` already tolerate via a
    COALESCE fallback."""
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    card_json = json.dumps({**(card_extra or {}), "topic": topic}, ensure_ascii=False)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, runner, topic_column if topic_column is not None else topic, "solo",
                status, worker_status, "normal", "objective", card_json, NOW, NOW, claimed_by,
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


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    yield root


# ---------------------------------------------------------------------------
# 1. task_engine.claim_start_exact -- repo-bound identity + topic-fallback fix
# ---------------------------------------------------------------------------


def test_exact_claim_topic_only_in_card_json_no_longer_false_identity_mismatch(repo):
    """B880 repro: a task written with an empty tasks.topic SQL column but
    the real topic in card_json used to fail claim_start_failed:identity_mismatch
    even though task_id/runner/topic were an exact match."""
    _insert_task(repo, "TASK_B882", CARD_RUNNER, CARD_TOPIC, topic_column="")
    row_before = _row(repo, "TASK_B882")
    assert row_before["topic"] == "", "fixture must reproduce the empty-column shape"

    claimed = task_engine.claim_start_exact(repo, "TASK_B882", CARD_RUNNER, CARD_TOPIC)
    assert claimed["ok"] is True, claimed
    row = _row(repo, "TASK_B882")
    assert row["worker_status"] == "claimed"
    assert row["status"] == "processing"
    assert row["claimed_by"] == CARD_RUNNER


def test_exact_claim_succeeds_when_topic_column_populated(repo):
    _insert_task(repo, "TASK_B882B", CARD_RUNNER, CARD_TOPIC)
    claimed = task_engine.claim_start_exact(repo, "TASK_B882B", CARD_RUNNER, CARD_TOPIC)
    assert claimed["ok"] is True, claimed


@pytest.mark.parametrize(
    ("runner", "topic"),
    [("wrong_runner", CARD_TOPIC), (CARD_RUNNER, "wrong_topic")],
)
def test_exact_claim_still_fails_closed_on_real_mismatch(repo, runner, topic):
    """The topic-fallback fix must never widen identity: a genuinely
    mismatched runner or topic is still rejected."""
    _insert_task(repo, "TASK_B882C", CARD_RUNNER, CARD_TOPIC)
    claimed = task_engine.claim_start_exact(repo, "TASK_B882C", runner, topic)
    assert claimed["ok"] is False
    row = _row(repo, "TASK_B882C")
    assert row["worker_status"] == "unclaimed"


def test_exact_claim_fails_closed_on_non_pending_task(repo):
    _insert_task(
        repo, "TASK_B882D", CARD_RUNNER, CARD_TOPIC,
        worker_status="claimed", status="processing", claimed_by=CARD_RUNNER,
    )
    claimed = task_engine.claim_start_exact(repo, "TASK_B882D", CARD_RUNNER, CARD_TOPIC)
    assert claimed["ok"] is False
    assert "claimed_task_requires_launch_request_id" in str(claimed.get("stderr") or "")


def test_exact_claim_is_idempotent_never_double_claims(repo):
    _insert_task(repo, "TASK_B882E", CARD_RUNNER, CARD_TOPIC)
    first = task_engine.claim_start_exact(repo, "TASK_B882E", CARD_RUNNER, CARD_TOPIC, request_id="req-1")
    assert first["ok"] is True
    second = task_engine.claim_start_exact(repo, "TASK_B882E", CARD_RUNNER, CARD_TOPIC, request_id="req-2")
    assert second["ok"] is False
    assert "card_scoped_claim_start_ineligible:processing" in str(second.get("stderr") or "")
    row = _row(repo, "TASK_B882E")
    assert row["worker_status"] == "claimed"
    card = json.loads(row["card_json"])
    assert card["claim_epoch"] == 1
    assert card["launch_request_id"] == "req-1"


def test_new_claim_clears_stale_episode_metadata_but_preserves_audit(repo):
    stale_fields = {
        "terminal_review": {"substatus": "validation_failed"},
        "terminal_substatus": "validation_failed",
        "deterministic_verification": {"ok": False},
        "review_requested_by": CARD_RUNNER,
        "validation_status": "failed",
        "validation_error": "old failure",
        "blocker_reason": "old blocker",
        "launch_error": "old launch error",
        "terminal_outcome": "old outcome",
    }
    _insert_task(
        repo,
        "TASK_B882_REWORK",
        CARD_RUNNER,
        CARD_TOPIC,
        card_extra=stale_fields,
    )
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "UPDATE tasks SET completed_at=? WHERE task_id=?",
            (NOW, "TASK_B882_REWORK"),
        )
        conn.execute(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (
                "TASK_B882_REWORK",
                "terminal_review",
                CARD_RUNNER,
                json.dumps({"substatus": "validation_failed"}),
                NOW,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    claimed = task_engine.claim_start_exact(
        repo,
        "TASK_B882_REWORK",
        CARD_RUNNER,
        CARD_TOPIC,
        request_id="req-new-episode",
    )

    assert claimed["ok"] is True, claimed
    row = _row(repo, "TASK_B882_REWORK")
    assert row["completed_at"] is None
    card = json.loads(row["card_json"])
    assert card["claim_epoch"] == 1
    for key in stale_fields:
        assert key not in card
    events = task_store.get_task_events(repo, "TASK_B882_REWORK", limit=10)
    assert [event["event"] for event in events] == ["claim_start", "terminal_review"]
    claim_payload = json.loads(events[0]["payload"])
    assert claim_payload["prior_episode"]["terminal_substatus"] == "validation_failed"
    assert claim_payload["prior_episode"]["validation_error"] == "old failure"


def test_exact_claim_write_gate_closed_blocks_claim(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.delenv("AIWORKHUB_ALLOW_WRITES", raising=False)
    _insert_task(root, "TASK_B882F", CARD_RUNNER, CARD_TOPIC)
    claimed = task_engine.claim_start_exact(root, "TASK_B882F", CARD_RUNNER, CARD_TOPIC)
    assert claimed["ok"] is False
    assert claimed["returncode"] == 126
    row = _row(root, "TASK_B882F")
    assert row["worker_status"] == "unclaimed"


def test_exact_claim_repo_bound_two_repos_same_task_id_stay_isolated(tmp_path, monkeypatch):
    """The explicit-repo binding (the actual B880 fix) must not regress the
    existing per-repo isolation invariant (mirrors B852's ambient-repo
    version of this same guarantee)."""
    root_a = _init_repo(tmp_path, "repo_x")
    root_b = _init_repo(tmp_path, "repo_y")
    _insert_task(root_a, "SAME_ID", CARD_RUNNER, CARD_TOPIC, card_extra={"marker": "a"})
    _insert_task(root_b, "SAME_ID", CARD_RUNNER, CARD_TOPIC, card_extra={"marker": "b"})
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")

    monkeypatch.setenv("AIWORKHUB_REPO", str(root_a))
    claimed_a = task_engine.claim_start_exact(root_a, "SAME_ID", CARD_RUNNER, CARD_TOPIC)
    assert claimed_a["ok"] is True
    assert _row(root_a, "SAME_ID")["worker_status"] == "claimed"
    assert _row(root_b, "SAME_ID")["worker_status"] == "unclaimed"


# ---------------------------------------------------------------------------
# 2. server.aiworkhub_task_auto_pickup -- card_scoped_task_id_required fallback
# ---------------------------------------------------------------------------


def test_auto_pickup_card_scoped_runner_reproduces_then_is_fixed_by_fallback(repo):
    _insert_task(repo, "TASK_B882G", CARD_RUNNER, CARD_TOPIC)

    # Reproduce the raw defect: the public auto_pickup contract cannot supply
    # a task_id, so the card-scoped identity is denied outright.
    raw = core.auto_pickup(runner=CARD_RUNNER, topic=CARD_TOPIC)
    assert raw["ok"] is False
    assert "card_scoped_task_id_required" in raw["stderr"]
    assert _row(repo, "TASK_B882G")["worker_status"] == "unclaimed"

    # The MCP tool itself resolves and claims it via the fallback.
    fixed = server.aiworkhub_task_auto_pickup(runner=CARD_RUNNER, topic=CARD_TOPIC)
    assert fixed["ok"] is True, fixed
    row = _row(repo, "TASK_B882G")
    assert row["worker_status"] == "claimed"
    assert row["claimed_by"] == CARD_RUNNER


def test_auto_pickup_card_scoped_runner_with_no_eligible_task_stays_denied(repo):
    """No unearned widening: an identity with zero eligible pending cards
    still fails closed, unchanged."""
    result = server.aiworkhub_task_auto_pickup(runner=CARD_RUNNER, topic=CARD_TOPIC)
    assert result["ok"] is False
    assert "card_scoped_task_id_required" in str(result.get("stderr") or "")


def test_auto_pickup_allowlisted_runner_is_unaffected_by_fallback(repo):
    """The static allowlist path (claude_coding/coding) must keep working
    exactly as before -- no fallback claim-start call is ever reached."""
    _insert_task(repo, "TASK_B882H", "claude_coding", "coding")
    result = server.aiworkhub_task_auto_pickup(runner="claude_coding", topic="coding")
    assert result["ok"] is True
    card = json.loads(result["stdout"])
    assert card["task_id"] == "TASK_B882H"


def test_auto_pickup_dryrun_still_never_mutates_queue(repo):
    _insert_task(repo, "TASK_B882I", CARD_RUNNER, CARD_TOPIC)
    preview = core.auto_pickup_dryrun(runner=CARD_RUNNER, topic=CARD_TOPIC)
    assert preview["would_claim_task_id"] == "TASK_B882I"
    assert _row(repo, "TASK_B882I")["worker_status"] == "unclaimed"


# ---------------------------------------------------------------------------
# 3. Full isolated-launch flow -- fake/injected process only, no paid model
# ---------------------------------------------------------------------------


def _plan(argv):
    def build(**kwargs):
        return SimpleNamespace(argv=list(argv), cwd=str(kwargs["repo"]), launchable=True, reason="")

    return build


def _collision(**_kwargs):
    return {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, shell=False,
    )


@pytest.fixture
def git_task_repo(tmp_path, monkeypatch):
    """A repo that is both a real git worktree (workspace/promotion needs
    this) and a real initialized aiworkhub task_store (claim_start_exact
    needs this) -- the exact combination ``aiworkhub_agent_launch_task``
    runs against in production."""
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    (root / "out").mkdir()
    (root / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(root, "add", "out/result.txt").returncode == 0
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setattr(
        process_launcher.claude_auth,
        "auth_status",
        lambda: {"launchable": True, "blocker_reason": ""},
    )
    monkeypatch.setenv(worker_workspace.SANDBOX_BACKEND_ENV, "landlock")
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    return root


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub hosted runners SIGSEGV in nested Landlock execution",
)
def test_agent_launch_task_exact_claim_no_longer_identity_mismatch(
    monkeypatch, tmp_path, git_task_repo
):
    """End-to-end regression for the exact bug named in the task objective:
    aiworkhub_agent_launch_task -> ProcessManager.launch (isolation_enabled=
    True, the real ``_launch_isolated`` production path) with an exact
    pending task/runner/topic, a real repo-bound canonical task_store, and no
    ``show_task`` fake -- must claim successfully through the real
    task_engine.claim_start_exact instead of
    claim_start_failed:identity_mismatch. Popen only ever runs a bounded
    POSIX-shell command; no Claude/Codex CLI adapter or paid model call happens
    on this path."""
    _insert_task(
        git_task_repo, "TASK_B882J", CARD_RUNNER, CARD_TOPIC, topic_column="",
        card_extra={"allowed_writes": ["out/result.txt"], "priority": "high"},
    )
    manager = process_launcher.ProcessManager(
        repo=git_task_repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        collision_guard=_collision,
        # This smoke validates the isolated launch/claim/promotion contract,
        # not a Python interpreter.  GitHub runner Python binaries can receive
        # SIGSEGV under nested Landlock, so use the minimal stable POSIX writer.
        adapter_builder=_plan(["/bin/sh", "-c", "printf 'worker-result\\n' > out/result.txt"]),
    )
    launched = manager.launch(
        task_id="TASK_B882J",
        runner=CARD_RUNNER,
        topic=CARD_TOPIC,
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert launched["ok"] is True, launched
    row = _row(git_task_repo, "TASK_B882J")
    assert row["worker_status"] == "claimed"
    assert row["claimed_by"] == CARD_RUNNER

    deadline_result = None
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        deadline_result = manager.collect(launched["request_id"])
        if deadline_result.get("terminal"):
            break
        time.sleep(0.02)
    assert deadline_result is not None and deadline_result.get("terminal")
    assert deadline_result["state"] == "review_ready", json.dumps(
        deadline_result, sort_keys=True, default=str
    )
    # Worker success only validates in the isolated workspace and transitions
    # the canonical task to review_ready -- the canonical file must stay
    # byte-unchanged until the coordinator explicitly accepts the review.
    assert (git_task_repo / "out" / "result.txt").read_text(encoding="utf-8") == "baseline\n"
    review_row = _row(git_task_repo, "TASK_B882J")
    assert review_row["worker_status"] == "review"

    accepted = manager.accept_review(launched["request_id"], "TASK_B882J")
    assert accepted["ok"] is True, accepted
    assert (git_task_repo / "out" / "result.txt").read_text(encoding="utf-8") == "worker-result\n"
    final_row = _row(git_task_repo, "TASK_B882J")
    assert final_row["worker_status"] == "done"
