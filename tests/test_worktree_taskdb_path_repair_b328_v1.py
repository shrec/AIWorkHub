"""B328: isolated-worktree AITools/taskdb.py DEFAULT_DB preflight repair.

Root cause (reproduced live, matches the B313 canary blocker recorded in
AITools/session_latest.md): ``AITools/taskdb.py``'s ``DEFAULT_DB`` resolves
relative to its own ``__file__``. Inside a git worktree that file is the
WORKTREE's own checked-out copy, so ``DEFAULT_DB`` silently points at a
worktree-local path that:

  1. is outside ``workspace.allowed_writes``, so Landlock refuses to create
     it -- ``sqlite3.OperationalError: unable to open database file`` at
     ``AITools/taskdb.py:65`` (``open_db``); and
  2. even when creation succeeds (bubblewrap, which binds the whole worktree
     read-write), ``taskctl._ensure_db_seeded()``'s from-empty auto-seed
     immediately tries to write back into the worktree's tracked
     ``machine_task_cards_v1.jsonl``/manifest -- Landlock refuses that too
     (``PermissionError``), and bubblewrap would silently succeed and
     corrupt the later git-diff-based scope check with a mutation the
     worker never made.

Fix under test: ``worker_workspace.provision_isolated_task_queue_db``
pre-seeds a disposable, non-authoritative queue-DB copy under
``workspace.home`` on the coordinator/host side (read-only against the
parent), and ``sanitized_env(..., isolated_task_queue_db=True)`` points
``BITNN_TASK_QUEUE_DB`` at that copy for both the sandboxed worker process
and every ``run_validations`` call, so ``taskctl.py``'s own
``_ensure_db_seeded`` always finds a non-empty DB and never takes the
seed-and-export branch inside the sandbox.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from geoai_task_mcp import worker_workspace  # noqa: E402

# The real GeoAI repo root: tests/ -> geoai-task-mcp/ -> tools/ -> GeoAI/.
_GEOAI_REPO = Path(__file__).resolve().parents[3]
_REAL_TASKDB = _GEOAI_REPO / "AITools" / "taskdb.py"
_REAL_TASKCTL = _GEOAI_REPO / "AITools" / "taskctl.py"

pytestmark = pytest.mark.skipif(
    not (_REAL_TASKDB.is_file() and _REAL_TASKCTL.is_file()),
    reason="real AITools/taskdb.py + taskctl.py not found relative to this test file",
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


def _unique_request_id(label: str) -> str:
    return f"b328{label}{uuid.uuid4().hex}"[:64]


def _real_repo_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, label: str
) -> worker_workspace.WorkerWorkspace:
    """An isolated worktree over the REAL GeoAI repo, scoped to a harmless
    throwaway eval path so no real production artifact is ever declared
    writable. Uses a per-test private worktree root (tmp_path) so concurrent
    test/worker activity on the shared repo cannot collide."""
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    scratch = f"tools/geoai-task-mcp/eval/_b328_test_scratch_{uuid.uuid4().hex}.json"
    return worker_workspace.create_workspace(
        _GEOAI_REPO,
        _unique_request_id(label),
        {
            "allowed_writes": [scratch],
            "read_first": ["AITools/taskdb.py"],
        },
        "validation",
    )


# ---------------------------------------------------------------------------
# Core regression: preflight verify passes inside the isolated worktree, and
# the fix never leaves a trace on the worktree's tracked queue files.
# ---------------------------------------------------------------------------


def test_taskctl_verify_passes_inside_isolated_worktree_and_leaves_tracked_files_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _real_repo_workspace(monkeypatch, tmp_path, "verify")
    try:
        # Provisioning happens synchronously inside create_workspace(), i.e.
        # strictly before any sandboxed process (worker or validation) runs.
        isolated_db = workspace.home / worker_workspace.TASK_QUEUE_ISOLATED_RELATIVE
        assert isolated_db.is_file()
        conn = sqlite3.connect(isolated_db)
        try:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            conn.close()
        assert task_count > 0, "provisioned isolated queue DB must not be empty"

        results = worker_workspace.run_validations(
            workspace, ["python3 AITools/taskctl.py verify"]
        )
        assert len(results) == 1
        record = results[0]
        assert record["returncode"] == 0, record["stderr_tail"]
        assert "VERIFY: PASS" in record["stdout_tail"]
        assert "Traceback" not in record["stderr_tail"]

        # The B313 second failure mode: taskctl's from-empty auto-seed
        # writing back into the worktree's tracked queue files. Assert it
        # never fired -- no tracked-file diff, no untracked queue file.
        diff = _git(workspace.path, "diff", "--name-only", "HEAD")
        assert diff.returncode == 0
        assert "bitnnv2/data/tasking/machine_task_cards" not in diff.stdout

        status = _git(workspace.path, "status", "--porcelain")
        assert status.returncode == 0
        assert "bitnnv2/data/tasking/machine_task_cards" not in status.stdout
    finally:
        worker_workspace.cleanup_workspace(_GEOAI_REPO, workspace.path, workspace.home)


def test_detached_worktree_invariant_holds_with_provisioning_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _real_repo_workspace(monkeypatch, tmp_path, "detach")
    try:
        assert workspace.path != _GEOAI_REPO
        assert _GEOAI_REPO not in workspace.path.parents
        assert _git(workspace.path, "symbolic-ref", "-q", "HEAD").returncode != 0
        top = _git(workspace.path, "rev-parse", "--show-toplevel")
        assert Path(top.stdout.strip()).resolve() == workspace.path
    finally:
        worker_workspace.cleanup_workspace(_GEOAI_REPO, workspace.path, workspace.home)


def test_cleanup_removes_the_isolated_queue_db_with_the_rest_of_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _real_repo_workspace(monkeypatch, tmp_path, "cleanup")
    isolated_db = workspace.home / worker_workspace.TASK_QUEUE_ISOLATED_RELATIVE
    assert isolated_db.is_file()
    worker_workspace.cleanup_workspace(_GEOAI_REPO, workspace.path, workspace.home)
    assert not workspace.home.exists()
    assert not isolated_db.exists()
    assert not workspace.path.exists()


# ---------------------------------------------------------------------------
# Prove the diagnosis: reproduce the exact B313 crash when the fix is
# deliberately bypassed (isolated_task_queue_db=False), then confirm the
# real run_validations() path (isolated_task_queue_db=True by construction)
# does not.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
def test_b313_root_cause_reproduces_when_the_env_fix_is_bypassed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _real_repo_workspace(monkeypatch, tmp_path, "repro")
    try:
        tokens = worker_workspace.validation_argv("python3 AITools/taskctl.py verify")
        wrapped = worker_workspace.sandbox_argv(
            workspace, "validation", tokens, backend="landlock"
        )
        # Deliberately the OLD (pre-B328) environment: no BITNN_TASK_QUEUE_DB
        # override, so AITools/taskdb.py's own DEFAULT_DB fallback resolves
        # inside the worktree, exactly as it did in the B313 live canary.
        env = worker_workspace.sanitized_env(
            "validation", home=workspace.home, isolated_task_queue_db=False
        )
        assert "BITNN_TASK_QUEUE_DB" not in env
        result = subprocess.run(
            wrapped,
            cwd="/",
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
            shell=False,
        )
        assert result.returncode != 0
        assert "sqlite3.OperationalError: unable to open database file" in result.stderr
        assert "AITools/taskdb.py" in result.stderr
    finally:
        worker_workspace.cleanup_workspace(_GEOAI_REPO, workspace.path, workspace.home)


# ---------------------------------------------------------------------------
# sanitized_env(): the isolated queue DB path is under HOME, never equal to
# any parent/production path, and the coordinator token/token-file env vars
# are never carried into the sandbox (they were never allowlisted -- this
# locks that in as an explicit, permanent regression guard).
# ---------------------------------------------------------------------------


def test_sanitized_env_isolated_queue_db_is_under_home_and_never_parent_or_coordinator_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "top-secret-coordinator-token")
    monkeypatch.setenv(
        "BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", "/home/shrek/.config/geoai/taskctl_coordinator.token"
    )
    monkeypatch.setenv("BITNN_TASK_QUEUE_DB", str(_GEOAI_REPO / "bitnnv2/data/tasking/task_queue_v1.sqlite"))
    monkeypatch.setenv("GEOAI_TASK_MCP_ALLOW_LAUNCH", "1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-allowed")

    home = tmp_path / "home"
    env = worker_workspace.sanitized_env(
        "claude_cli", home=home, isolated_task_queue_db=True
    )

    assert "BITNN_TASK_QUEUE_DB" in env
    db_path = Path(env["BITNN_TASK_QUEUE_DB"])
    assert home in db_path.parents
    assert db_path == (home / worker_workspace.TASK_QUEUE_ISOLATED_RELATIVE)
    # Never the real production queue DB path, regardless of what the
    # coordinator's own os.environ happened to contain.
    assert env["BITNN_TASK_QUEUE_DB"] != str(
        _GEOAI_REPO / "bitnnv2/data/tasking/task_queue_v1.sqlite"
    )

    assert "BITNN_TASKCTL_COORDINATOR_TOKEN" not in env
    assert "BITNN_TASKCTL_COORDINATOR_TOKEN_FILE" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GEOAI_TASK_MCP_ALLOW_LAUNCH" not in env


def test_sanitized_env_omits_isolated_queue_db_by_default(tmp_path: Path) -> None:
    """The direct (non-isolated, tests-only) launch path must keep its prior
    behavior: no BITNN_TASK_QUEUE_DB override unless explicitly requested."""
    env = worker_workspace.sanitized_env("claude_cli")
    assert "BITNN_TASK_QUEUE_DB" not in env

    home = tmp_path / "home"
    env_home = worker_workspace.sanitized_env("claude_cli", home=home)
    assert "BITNN_TASK_QUEUE_DB" not in env_home


# ---------------------------------------------------------------------------
# provision_isolated_task_queue_db(): absent parent directory, read-only
# against the parent DB, and graceful (never-raising) failure rollback.
# ---------------------------------------------------------------------------


def _seed_fixture_parent_db(taskdb_module, path: Path, task_id: str) -> None:
    conn = taskdb_module.open_db(path)
    try:
        taskdb_module.init_db(conn)
        taskdb_module.import_cards(
            conn,
            [
                {
                    "task_id": task_id,
                    "runner": "claude_coding",
                    "topic": "coding",
                    "status": "pending",
                    "allowed_writes": ["some/path.json"],
                    "forbidden": ["git_add_A"],
                }
            ],
            preserve_lifecycle=True,
        )
    finally:
        conn.close()


def test_provision_creates_absent_db_parent_directory_and_copies_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    taskdb = worker_workspace._load_repo_taskdb_module(_GEOAI_REPO)
    parent_db = tmp_path / "fixture_parent" / "task_queue_v1.sqlite"
    _seed_fixture_parent_db(taskdb, parent_db, "FIXTURE_PARENT_TASK_B328_001")
    before_bytes = parent_db.read_bytes()
    before_mtime_ns = parent_db.stat().st_mtime_ns

    monkeypatch.setenv("BITNN_TASK_QUEUE_DB", str(parent_db))

    home = tmp_path / "home"  # deliberately does not exist yet
    assert not home.exists()
    destination = worker_workspace.provision_isolated_task_queue_db(_GEOAI_REPO, home)

    assert destination == (home / worker_workspace.TASK_QUEUE_ISOLATED_RELATIVE).resolve()
    assert destination.is_file()
    assert destination.parent.is_dir()

    # Never mutated the parent -- byte-identical, same mtime.
    assert parent_db.read_bytes() == before_bytes
    assert parent_db.stat().st_mtime_ns == before_mtime_ns

    conn = sqlite3.connect(destination)
    try:
        row = conn.execute(
            "SELECT task_id FROM tasks WHERE task_id=?",
            ("FIXTURE_PARENT_TASK_B328_001",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


def test_provision_never_raises_and_degrades_to_a_usable_db_when_repo_has_no_taskdb(
    tmp_path: Path,
) -> None:
    empty_repo = tmp_path / "repo_without_taskdb"
    (empty_repo / "AITools").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()

    destination = worker_workspace.provision_isolated_task_queue_db(empty_repo, home)

    assert destination.exists()
    assert home in destination.parents
    # Must at least be a valid, openable sqlite file -- never a half-written
    # or missing file that would reproduce the original crash downstream.
    conn = sqlite3.connect(destination)
    conn.execute("SELECT 1")
    conn.close()


def test_create_workspace_still_succeeds_when_provisioning_source_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Failure rollback: a repo fixture with no AITools/taskdb.py at all must
    not prevent create_workspace() from succeeding -- provisioning is
    best-effort and must never destabilize workspace creation."""
    root = tmp_path / "parent"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    (root / "out").mkdir()
    (root / "out" / "result.txt").write_text("v1\n", encoding="utf-8")
    assert _git(root, "add", "out/result.txt").returncode == 0
    assert _git(root, "commit", "-qm", "fixture").returncode == 0

    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    workspace = worker_workspace.create_workspace(
        root,
        _unique_request_id("noTaskdb"),
        {"allowed_writes": ["out/result.txt"], "read_first": []},
        "validation",
    )
    try:
        isolated_db = workspace.home / worker_workspace.TASK_QUEUE_ISOLATED_RELATIVE
        assert isolated_db.exists()
    finally:
        worker_workspace.cleanup_workspace(root, workspace.path, workspace.home)
