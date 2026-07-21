"""B850: cross-repository dashboard authority fix -- explicit init, no legacy
fallback.

Covers:
  * task_store.storage_readiness never reports ready from directory
    existence alone, and is fail-closed for a missing/corrupt manifest,
    registry, or canonical DB.
  * task_store.initialize_repository is idempotent, fail-closed, refuses a
    repo-id mismatch, and never imports/reads a legacy database.
  * A repository whose legacy `data/tasking`, `bitnnv2/data/tasking`,
    `workingdocs/tasking`, or `AITools` fixtures are fully populated shows
    exactly zero rows before initialization -- and still zero rows (never
    the legacy counts) via dashboard.build_snapshot()/exact_status_counts()
    after initialization, since a fresh canonical DB never imports legacy
    rows.
  * dashboard.build_snapshot()/build_task_detail() fail closed to an empty
    UNINITIALIZED snapshot (zero counts, zero rows, no secondary provider
    call) when storage is not ready, and never fall back.

Sandbox note: this worktree is a sparse checkout of only the files this
task's contract touches. Sibling `aiworkhub` submodules that dashboard.py
imports at module scope (`completion_inbox`, `cost_ledger`,
`deepseek_credentials`, `process_launcher`, `core`) are not materialized
here, so this file pre-registers minimal stand-ins in ``sys.modules`` before
importing ``aiworkhub.dashboard`` -- purely a test-harness shim for this
sandbox, not production behavior. Everything under test
(``DashboardProvider``, ``build_snapshot``, ``build_task_detail``,
``exact_status_counts``) is the real, unmodified production code.
"""

from __future__ import annotations

import sqlite3
import sys
import types
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _install_sparse_worktree_stub_modules() -> None:
    """Register minimal stand-ins for aiworkhub submodules absent from this
    sparse worktree so ``aiworkhub.dashboard`` (unmodified production code)
    can be imported. Never touches ``task_store`` or ``repository_state``,
    which are the real modules under test."""
    package_root = SRC / "aiworkhub"
    sparse_dependencies = (
        "completion_inbox",
        "cost_ledger",
        "deepseek_credentials",
        "process_launcher",
        "core",
    )
    if all((package_root / f"{name}.py").is_file() for name in sparse_dependencies):
        return
    if "aiworkhub.completion_inbox" in sys.modules:
        return  # already installed by an earlier test in this process

    completion_inbox = types.ModuleType("aiworkhub.completion_inbox")
    completion_inbox.DEFAULT_STALE_PROCESSING_HOURS = 6.0
    completion_inbox.MAX_LIMIT = 2000
    completion_inbox.build_completion_inbox = lambda **_kwargs: {}

    cost_ledger = types.ModuleType("aiworkhub.cost_ledger")
    cost_ledger.build_cost_ledger = lambda **_kwargs: {}

    deepseek_credentials = types.ModuleType("aiworkhub.deepseek_credentials")
    deepseek_credentials.adapter_readiness = lambda **_kwargs: {}

    process_launcher = types.ModuleType("aiworkhub.process_launcher")
    process_launcher.PROCESS_LOG_ENV = "AIWORKHUB_TEST_PROCESS_LOG_STUB"
    process_launcher.LAUNCH_IMPLEMENTED = False
    process_launcher.ACTIVE_PROCESS_STATES = ()
    process_launcher.launch_gates_open = lambda: False
    process_launcher.read_supervisor_status = lambda _path: {}
    process_launcher._pid_matches = lambda *_args, **_kwargs: False
    process_launcher.derive_liveness_state = lambda **_kwargs: {
        "liveness_state": "unknown",
        "heartbeat_age_seconds": None,
        "activity_age_seconds": None,
    }

    core = types.ModuleType("aiworkhub.core")
    core.repo_root = lambda: Path(".")
    core.list_tasks = lambda **_kwargs: {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
    core.show_task = lambda *_args, **_kwargs: {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
    core.collision_guard = lambda **_kwargs: {
        "ok": True,
        "returncode": 0,
        "stdout": "No cards to scan.",
        "stderr": "",
    }
    core.callback_outbox_status = lambda: {"ok": True, "returncode": 0, "stdout": "{}", "stderr": ""}
    core.health = lambda: {"ok": True}

    for name, module in (
        ("aiworkhub.completion_inbox", completion_inbox),
        ("aiworkhub.cost_ledger", cost_ledger),
        ("aiworkhub.deepseek_credentials", deepseek_credentials),
        ("aiworkhub.process_launcher", process_launcher),
        ("aiworkhub.core", core),
    ):
        sys.modules[name] = module


_install_sparse_worktree_stub_modules()

from aiworkhub import repository_state, task_store  # noqa: E402
from aiworkhub import dashboard  # noqa: E402  (needs the stubs installed above)


def _make_legacy_fixture(root: Path) -> Path:
    """Populate every legacy path the objective names, each with real
    non-zero counts, mirroring the reported Secp256K1fast screenshot
    (72 finished, 6 blocked, 1 processing)."""
    (root / ".git").mkdir(parents=True, exist_ok=True)

    def _write_legacy_db(rel: str) -> None:
        legacy_dir = root / rel
        legacy_dir.mkdir(parents=True, exist_ok=True)
        db_path = legacy_dir / "task_queue_v1.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT, worker_status TEXT, archived_at TEXT)"
        )
        for i in range(72):
            conn.execute(
                "INSERT INTO tasks VALUES (?, 'finished', 'done', '')", (f"legacy_finished_{rel}_{i}",)
            )
        for i in range(6):
            conn.execute(
                "INSERT INTO tasks VALUES (?, 'blocked', 'blocked', '')", (f"legacy_blocked_{rel}_{i}",)
            )
        conn.execute("INSERT INTO tasks VALUES (?, 'processing', 'claimed', '')", (f"legacy_proc_{rel}",))
        conn.commit()
        conn.close()

    _write_legacy_db("data/tasking")
    _write_legacy_db("bitnnv2/data/tasking")
    (root / "workingdocs" / "tasking").mkdir(parents=True, exist_ok=True)
    (root / "workingdocs" / "tasking" / "queue.sqlite").write_bytes(
        (root / "data" / "tasking" / "task_queue_v1.sqlite").read_bytes()
    )
    aitools = root / "AITools"
    aitools.mkdir(parents=True, exist_ok=True)
    (aitools / "taskdb.py").write_text("# fixture: repository AITools taskdb, must never be read\n")
    (aitools / "taskctl.py").write_text("# fixture: repository AITools taskctl, must never be executed\n")
    return root


class StorageReadinessTests(unittest.TestCase):
    def test_uninitialized_repository_is_never_ready(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_legacy_fixture(Path(tmp))
            readiness = task_store.storage_readiness(root)
            self.assertFalse(readiness.ready)
            self.assertNotEqual(readiness.reason, "")

    def test_directory_existence_alone_is_insufficient(self) -> None:
        """Creating durable_layout directories WITHOUT a valid manifest and
        registry must still report not-ready -- this is the exact bug: the
        old JS-side check treated directory existence as sufficient."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            hub = root / ".aiworkhub"
            for rel in repository_state.DURABLE_LAYOUT.values():
                (hub / rel).mkdir(parents=True)
            (hub / "runtime").mkdir(parents=True)
            readiness = task_store.storage_readiness(root)
            self.assertFalse(readiness.ready)

    def test_initialize_repository_is_idempotent_and_verified_ready(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_legacy_fixture(Path(tmp))
            self.assertFalse(task_store.storage_readiness(root).ready)

            result = task_store.initialize_repository(root)
            self.assertTrue(result["ok"])
            self.assertTrue(result["created_canonical_db"])
            self.assertFalse(result["legacy_imported"])
            self.assertFalse(result["legacy_deleted"])

            readiness = task_store.storage_readiness(root)
            self.assertTrue(readiness.ready)
            self.assertEqual(readiness.reason, "ready")

            # Idempotent: a second call neither recreates the DB nor
            # re-activates authority, and still succeeds.
            result2 = task_store.initialize_repository(root, expected_repo_id=result["repo_id"])
            self.assertTrue(result2["ok"])
            self.assertFalse(result2["created_canonical_db"])
            self.assertFalse(result2["activated_canonical_authority"])

            # Legacy files were never touched or deleted.
            self.assertTrue((root / "data" / "tasking" / "task_queue_v1.sqlite").is_file())
            self.assertTrue((root / "bitnnv2" / "data" / "tasking" / "task_queue_v1.sqlite").is_file())
            self.assertTrue((root / "AITools" / "taskdb.py").is_file())

    def test_initialize_repository_refuses_repo_id_mismatch(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_legacy_fixture(Path(tmp))
            task_store.initialize_repository(root)
            with self.assertRaises(task_store.InitializationRefusedError):
                task_store.initialize_repository(root, expected_repo_id="repo_" + "0" * 32)

    def test_canonical_db_never_contains_legacy_rows_after_init(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_legacy_fixture(Path(tmp))
            task_store.initialize_repository(root)
            counts = task_store.exact_status_counts(root)
            self.assertEqual(counts, {status: 0 for status in task_store.CANONICAL_STATUSES})
            self.assertEqual(task_store.list_tasks(root), [])
            health = task_store.callback_bridge_health(root)
            self.assertEqual(health["total"], 0)
            self.assertEqual(health["bound_task_count"], 0)

    def test_reads_fail_closed_before_initialization(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_legacy_fixture(Path(tmp))
            with self.assertRaises(task_store.StorageNotReadyError):
                task_store.exact_status_counts(root)
            with self.assertRaises(task_store.StorageNotReadyError):
                task_store.list_tasks(root)


class _LegacyLeakageProbeProvider:
    """A provider stand-in that would leak legacy fixture rows if
    build_snapshot's storage gate were bypassed. get_storage_readiness
    always returns not-ready; every other method raises if ever called,
    so calling one proves the "no fallback" gate is broken."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def get_storage_readiness(self) -> task_store.StorageReadiness:
        return task_store.storage_readiness(self.repo_root)

    def __getattr__(self, name: str):  # pragma: no cover - defensive
        def _forbidden(*_args, **_kwargs):
            raise AssertionError(f"build_snapshot must not call {name} while storage is not ready")

        return _forbidden


class DashboardSnapshotTests(unittest.TestCase):
    def test_uninitialized_snapshot_is_empty_and_never_calls_other_providers(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_legacy_fixture(Path(tmp))
            provider = _LegacyLeakageProbeProvider(root)
            snapshot = dashboard.build_snapshot(provider)
            self.assertFalse(snapshot["storage"]["ready"])
            for status in dashboard.ALL_CANONICAL_STATUSES:
                self.assertEqual(snapshot["status_counts"][status], 0)
            self.assertEqual(snapshot["status_counts"]["active"], 0)
            for group in snapshot["tasks"].values():
                self.assertEqual(group, [])

    def test_uninitialized_task_detail_returns_none(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_legacy_fixture(Path(tmp))
            provider = _LegacyLeakageProbeProvider(root)
            self.assertIsNone(dashboard.build_task_detail("any_task_id", provider))

    def test_initialized_snapshot_reports_zero_counts_and_ready_true(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_legacy_fixture(Path(tmp))
            task_store.initialize_repository(root)
            provider = dashboard.DashboardProvider(repo_root=root)
            snapshot = dashboard.build_snapshot(provider)
            self.assertTrue(snapshot["storage"]["ready"])
            for status in dashboard.ALL_CANONICAL_STATUSES:
                self.assertEqual(snapshot["status_counts"][status], 0)
            self.assertEqual(snapshot["callback_bridge_health"]["total"], 0)
            self.assertTrue(snapshot["health"]["ok"])

    def test_exact_status_counts_free_function_uses_task_store(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_legacy_fixture(Path(tmp))
            task_store.initialize_repository(root)
            counts = dashboard.exact_status_counts(root)
            self.assertEqual(counts, {status: 0 for status in dashboard.ALL_CANONICAL_STATUSES})


if __name__ == "__main__":
    unittest.main()
