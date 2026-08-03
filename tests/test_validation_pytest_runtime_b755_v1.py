"""B755 regression suite: trusted pytest runtime root for isolated validation.

B753/B674 false negative: an isolated validation run's sanitized,
credential-free HOME has no ``~/.local/lib/pythonX/site-packages`` of its
own, so a ``pytest``/``python3 -m pytest`` validation command fails with
``ModuleNotFoundError: No module named 'pytest'`` even though the parent
host has pytest installed -- a false negative, not a real product failure.
``run_validations`` now detects a pytest invocation, resolves and trust-
validates the single canonical pytest package root
(``site.getusersitepackages()``), binds it read-only, and prepends only that
exact path to PYTHONPATH ahead of whatever relative project PYTHONPATH the
card already declared. Fails closed when no approved pytest runtime exists.

Standalone unittest module -- deliberately NOT pytest-based, so this task's
own validation does not depend on the exact bug it repairs (``python3 -m
pytest`` is unusable under this same sanitized HOME until the fix lands).
Run directly: ``python3 tools/geoai-task-mcp/tests/test_validation_pytest_runtime_b755_v1.py``
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_workspace  # noqa: E402


def _manual_workspace(tmp_path: Path, request_id: str) -> tuple[Path, worker_workspace.WorkerWorkspace]:
    """Build a ``WorkerWorkspace`` without shelling out to real ``git``.

    Mirrors ``test_validation_exec_scratch_b753_v1.py``'s helper: this suite
    itself runs from inside a worker session already confined by the exact
    Landlock+seccomp sandbox this module implements, so a real
    ``git worktree add`` cannot succeed here regardless of monkeypatching.
    Manual construction still exercises the real ``run_validations`` /
    ``resolve_trusted_pytest_runtime_root`` / ``sandbox_argv`` code paths.
    """
    repo = tmp_path / "fake_repo"
    repo.mkdir(exist_ok=True)
    base = tmp_path / "worktrees" / request_id
    path = base / "worktree"
    home = base / "home"
    path.mkdir(parents=True)
    home.mkdir(parents=True)
    workspace = worker_workspace.WorkerWorkspace(
        request_id=request_id,
        repo=repo,
        path=path,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    return repo, workspace


def _write_fake_pytest_package(root: Path, body: str = "print('FAKE_PYTEST_MAIN_OK ' + sys.argv[-1])\n") -> None:
    pkg = root / "pytest"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("__version__ = '0.0.0-fake'\n", encoding="utf-8")
    (pkg / "__main__.py").write_text("import sys\n" + body + "\nraise SystemExit(0)\n", encoding="utf-8")


class _TolerateNestedSeccompChmodDenial(unittest.TestCase):
    """Base class: swallow only ``PermissionError`` from chmod/fchmod so the
    real ``create_workspace``/``sanitized_env``/``run_validations`` code
    paths still run end-to-end from inside this already-sandboxed authoring
    session (see the identical rationale in ``test_validation_exec_scratch_b753_v1.py``).
    Every mode bit these calls request is already applied atomically by the
    preceding ``mkdir``/``os.open`` mode argument, so a denied follow-up
    chmod never changes the resulting permissions.
    """

    def setUp(self) -> None:
        self._real_chmod = os.chmod
        self._real_fchmod = os.fchmod

        def _chmod(path, mode, *a, **kw):
            try:
                return self._real_chmod(path, mode, *a, **kw)
            except PermissionError:
                return None

        def _fchmod(fd, mode):
            try:
                return self._real_fchmod(fd, mode)
            except PermissionError:
                return None

        self._chmod_patch = mock.patch("os.chmod", _chmod)
        self._fchmod_patch = mock.patch("os.fchmod", _fchmod)
        self._chmod_patch.start()
        self._fchmod_patch.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._chmod_patch.stop()
        self._fchmod_patch.stop()
        self._tmp.cleanup()


# --- 1. reproduce the exact B753/B674 false negative ------------------------


class TestReproducesSanitizedHomePytestImportFailure(_TolerateNestedSeccompChmodDenial):
    def test_pytest_module_invocation_fails_under_a_fresh_sanitized_home(self) -> None:
        """A ``python3 -m pytest`` command run with ``sanitized_env``'s own
        HOME (no PYTHONPATH override) fails with ModuleNotFoundError, even
        though this exact suite is proof the parent host has a real,
        importable pytest install (``site.getusersitepackages()`` resolves
        to a real ``pytest`` package whenever HOME is the operator's real
        home -- see ``TestResolveTrustedPytestRuntimeRoot`` below)."""
        home = self.tmp_path / "sanitized_home"
        env = worker_workspace.sanitized_env("validation", home=home)
        result = worker_workspace.subprocess.run(
            # ``-S`` makes the absent-runtime precondition deterministic even
            # when CI itself runs from a virtualenv that contains pytest.
            [sys.executable, "-S", "-m", "pytest", "--version"],
            env=env,
            cwd=str(self.tmp_path),
            text=True,
            stdout=worker_workspace.subprocess.PIPE,
            stderr=worker_workspace.subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No module named", result.stdout + result.stderr)
        self.assertIn("pytest", result.stdout + result.stderr)


# --- 2. trusted-root resolution: success, symlink/owner/world-writable -----


class TestResolveTrustedPytestRuntimeRoot(_TolerateNestedSeccompChmodDenial):
    def test_valid_trusted_root_with_importable_pytest_is_accepted(self) -> None:
        fake_root = self.tmp_path / "trusted_site_packages"
        fake_root.mkdir()
        os.chmod(fake_root, 0o755)
        _write_fake_pytest_package(fake_root)
        with mock.patch.object(worker_workspace.site, "getusersitepackages", return_value=str(fake_root)):
            resolved = worker_workspace.resolve_trusted_pytest_runtime_root()
        self.assertEqual(resolved, fake_root.resolve())

    def test_missing_root_fails_closed(self) -> None:
        missing = self.tmp_path / "does_not_exist"
        with mock.patch.object(worker_workspace.site, "getusersitepackages", return_value=str(missing)):
            with self.assertRaisesRegex(worker_workspace.WorkspaceError, "validation_pytest_runtime_unavailable"):
                worker_workspace.resolve_trusted_pytest_runtime_root()

    def test_symlinked_root_is_rejected(self) -> None:
        real_dir = self.tmp_path / "real_site_packages"
        real_dir.mkdir()
        _write_fake_pytest_package(real_dir)
        link = self.tmp_path / "site_packages_link"
        link.symlink_to(real_dir)
        with mock.patch.object(worker_workspace.site, "getusersitepackages", return_value=str(link)):
            with self.assertRaisesRegex(worker_workspace.WorkspaceError, "validation_pytest_runtime_symlink_forbidden"):
                worker_workspace.resolve_trusted_pytest_runtime_root()

    def test_world_writable_root_is_rejected(self) -> None:
        fake_root = self.tmp_path / "world_writable_site_packages"
        fake_root.mkdir()
        _write_fake_pytest_package(fake_root)
        os.chmod(fake_root, 0o777)
        try:
            mode = stat.S_IMODE(fake_root.stat().st_mode)
            if not (mode & 0o002):
                self.skipTest("chmod 0o777 denied in this sandbox; cannot force world-writable bit")
            with mock.patch.object(worker_workspace.site, "getusersitepackages", return_value=str(fake_root)):
                with self.assertRaisesRegex(worker_workspace.WorkspaceError, "validation_pytest_runtime_world_writable"):
                    worker_workspace.resolve_trusted_pytest_runtime_root()
        finally:
            os.chmod(fake_root, 0o755)

    def test_untrusted_owner_is_rejected(self) -> None:
        fake_root = self.tmp_path / "owner_mismatch_site_packages"
        fake_root.mkdir()
        _write_fake_pytest_package(fake_root)
        with mock.patch.object(worker_workspace.site, "getusersitepackages", return_value=str(fake_root)):
            with mock.patch.object(worker_workspace.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(worker_workspace.WorkspaceError, "validation_pytest_runtime_untrusted_owner"):
                    worker_workspace.resolve_trusted_pytest_runtime_root()

    def test_root_without_pytest_package_is_rejected(self) -> None:
        fake_root = self.tmp_path / "empty_site_packages"
        fake_root.mkdir()
        with mock.patch.object(worker_workspace.site, "getusersitepackages", return_value=str(fake_root)):
            with self.assertRaisesRegex(worker_workspace.WorkspaceError, "validation_pytest_runtime_missing_pytest"):
                worker_workspace.resolve_trusted_pytest_runtime_root()


# --- 3. pytest command detection --------------------------------------------


class TestIsPytestValidationCommand(unittest.TestCase):
    def test_module_invocation_variants(self) -> None:
        self.assertTrue(worker_workspace._is_pytest_validation_command(["python3", "-m", "pytest", "-q", "x.py"]))
        self.assertTrue(worker_workspace._is_pytest_validation_command(["python3", "-m", "pytest"]))
        self.assertTrue(worker_workspace._is_pytest_validation_command(["pytest", "-q"]))

    def test_non_pytest_commands_are_not_detected(self) -> None:
        self.assertFalse(worker_workspace._is_pytest_validation_command(["python3", "tools/x_test.py"]))
        self.assertFalse(worker_workspace._is_pytest_validation_command(["python3", "-m", "json.tool", "x.json"]))
        self.assertFalse(worker_workspace._is_pytest_validation_command(["python3", "AITools/taskctl.py", "verify"]))
        self.assertFalse(worker_workspace._is_pytest_validation_command([]))


# --- 4. run_validations end-to-end: success, fail-closed, unchanged non-pytest, credentials, cleanup ---


@unittest.skipIf(
    os.environ.get("GITHUB_ACTIONS") == "true",
    "GitHub hosted runners cannot execute nested Landlock validation sandboxes",
)
class TestRunValidationsPytestRepair(_TolerateNestedSeccompChmodDenial):
    def test_pytest_command_succeeds_with_trusted_root_prepended_and_project_pythonpath_preserved(self) -> None:
        fake_root = self.tmp_path / "trusted_site_packages"
        fake_root.mkdir()
        os.chmod(fake_root, 0o755)
        _write_fake_pytest_package(fake_root)
        repo, workspace = _manual_workspace(self.tmp_path, "b755-success")
        try:
            with mock.patch.object(worker_workspace.site, "getusersitepackages", return_value=str(fake_root)):
                results = worker_workspace.run_validations(
                    workspace, ["PYTHONPATH=. python3 -m pytest --version"]
                )
            record = results[0]
            self.assertEqual(record["returncode"], 0)
            self.assertIn("FAKE_PYTEST_MAIN_OK --version", record["stdout_tail"])
            self.assertEqual(record["env_override"]["variable"], "PYTHONPATH")
            components = record["env_override"]["components"]
            self.assertEqual(components[0], str(fake_root.resolve()))
            self.assertEqual(components[1:], ["."])
        finally:
            worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)

    def test_pytest_command_fails_closed_when_no_approved_pytest_runtime_exists(self) -> None:
        missing = self.tmp_path / "no_such_site_packages"
        repo, workspace = _manual_workspace(self.tmp_path, "b755-fail-closed")
        try:
            with mock.patch.object(worker_workspace.site, "getusersitepackages", return_value=str(missing)):
                with self.assertRaisesRegex(worker_workspace.WorkspaceError, "validation_pytest_runtime_unavailable"):
                    worker_workspace.run_validations(workspace, ["python3 -m pytest --version"])
        finally:
            worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)

    def test_non_pytest_command_env_and_argv_are_unchanged(self) -> None:
        """Non-pytest commands must never trigger trusted-root resolution at
        all -- proven by making resolution itself explode if ever called."""
        repo, workspace = _manual_workspace(self.tmp_path, "b755-non-pytest-unchanged")
        (workspace.path / "helper.py").write_text("print('non-pytest-ok')\n", encoding="utf-8")
        try:
            with mock.patch.object(
                worker_workspace,
                "resolve_trusted_pytest_runtime_root",
                side_effect=AssertionError("must not be called for a non-pytest command"),
            ):
                results = worker_workspace.run_validations(workspace, ["python3 helper.py"])
            self.assertEqual(results[0]["returncode"], 0)
            self.assertIsNone(results[0]["env_override"])
            self.assertIn("non-pytest-ok", results[0]["stdout_tail"])
        finally:
            worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)

    def test_pytest_command_does_not_leak_adapter_credentials(self) -> None:
        fake_root = self.tmp_path / "trusted_site_packages_creds"
        fake_root.mkdir()
        os.chmod(fake_root, 0o755)
        _write_fake_pytest_package(
            fake_root,
            body=(
                "import os, json\n"
                "print('ENV_KEYS ' + json.dumps(sorted(os.environ.keys())))"
            ),
        )
        repo, workspace = _manual_workspace(self.tmp_path, "b755-no-leak")
        old_env = dict(os.environ)
        try:
            os.environ["ANTHROPIC_API_KEY"] = "super-secret-claude-key"
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "super-secret-oauth"
            with mock.patch.object(worker_workspace.site, "getusersitepackages", return_value=str(fake_root)):
                results = worker_workspace.run_validations(workspace, ["python3 -m pytest --version"])
            record = results[0]
            self.assertEqual(record["returncode"], 0)
            printed_keys = set(json.loads(record["stdout_tail"].split("ENV_KEYS ", 1)[1]))
            self.assertNotIn("ANTHROPIC_API_KEY", printed_keys)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", printed_keys)
            self.assertNotIn("super-secret-claude-key", record["stdout_tail"])
            self.assertNotIn("super-secret-oauth", record["stdout_tail"])
        finally:
            os.environ.clear()
            os.environ.update(old_env)
            worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)

    def test_exec_scratch_still_provisioned_and_cleaned_up_around_a_pytest_command(self) -> None:
        fake_root = self.tmp_path / "trusted_site_packages_scratch"
        fake_root.mkdir()
        os.chmod(fake_root, 0o755)
        _write_fake_pytest_package(fake_root)
        repo, workspace = _manual_workspace(self.tmp_path, "b755-scratch-cleanup")
        captured: dict[str, Path] = {}
        real_provision = worker_workspace.provision_validation_exec_scratch

        def _capture(ws: worker_workspace.WorkerWorkspace) -> Path:
            scratch = real_provision(ws)
            captured["scratch"] = scratch
            return scratch

        try:
            with mock.patch.object(worker_workspace, "provision_validation_exec_scratch", _capture):
                with mock.patch.object(worker_workspace.site, "getusersitepackages", return_value=str(fake_root)):
                    results = worker_workspace.run_validations(workspace, ["python3 -m pytest --version"])
            self.assertEqual(results[0]["returncode"], 0)
            self.assertIn("scratch", captured)
            self.assertFalse(captured["scratch"].exists())
        finally:
            worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)

    def test_pytest_validation_disables_repo_local_cache_writes(self) -> None:
        fake_root = self.tmp_path / "trusted_site_packages_no_cache"
        fake_root.mkdir()
        os.chmod(fake_root, 0o755)
        _write_fake_pytest_package(
            fake_root,
            body=(
                "import os\n"
                "print('PYTEST_ADDOPTS=' + os.environ.get('PYTEST_ADDOPTS', ''))\n"
            ),
        )
        repo, workspace = _manual_workspace(self.tmp_path, "b755-no-cache-write")
        try:
            with mock.patch.object(
                worker_workspace.site,
                "getusersitepackages",
                return_value=str(fake_root),
            ):
                results = worker_workspace.run_validations(
                    workspace, ["python3 -m pytest --version"]
                )
            self.assertEqual(results[0]["returncode"], 0)
            self.assertIn(
                "PYTEST_ADDOPTS=-p no:cacheprovider",
                results[0]["stdout_tail"],
            )
        finally:
            worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


if __name__ == "__main__":
    unittest.main()
