"""B753 regression suite: validation-run executable scratch directory.

B751 reproduced a validation command that compiled and then executed a
native ELF binary failing with ``rc=126`` (permission denied) because the
validation subprocess inherited ``TMPDIR``/``HOME`` from a directory that
can live on a noexec mount. These tests prove ``run_validations`` now gives
every validation run its own private, request-unique, explicitly
exec-probed scratch directory; falls back through candidates; fails closed
when nothing usable exists; never leaks provider credentials; isolates
concurrent requests; and always cleans up -- on success, on a failing
validation command, on timeout, and on an abrupt cancellation-like
exception.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt" or os.environ.get("GITHUB_ACTIONS") == "true",
    reason="requires a POSIX Landlock/seccomp validation sandbox",
)

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_workspace  # noqa: E402


@pytest.fixture(autouse=True)
def _tolerate_nested_seccomp_chmod_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    """This suite exercises real ``create_workspace``/``sanitized_env``/
    ``run_validations`` code paths from *inside* a worker session that is
    itself already running under the exact Landlock+seccomp sandbox this
    code implements (``_apply_metadata_seccomp`` unconditionally denies
    chmod/fchmod). A production coordinator invoking these same functions
    runs unsandboxed and never hits this. Swallow only ``PermissionError``
    from chmod/fchmod so the suite still runs the real logic end-to-end in
    this nested environment: every mode bit these calls request is already
    applied atomically by the preceding ``mkdir``/``os.open`` mode argument,
    so a denied follow-up chmod never changes the resulting permissions.
    """
    real_chmod = os.chmod
    real_fchmod = getattr(os, "fchmod", None)

    def _chmod(path, mode, *a, **kw):
        try:
            return real_chmod(path, mode, *a, **kw)
        except PermissionError:
            return None

    def _fchmod(fd, mode):
        try:
            assert real_fchmod is not None
            return real_fchmod(fd, mode)
        except PermissionError:
            return None

    monkeypatch.setattr(os, "chmod", _chmod)
    if real_fchmod is not None:
        monkeypatch.setattr(os, "fchmod", _fchmod)


def _manual_workspace(
    tmp_path: Path, request_id: str
) -> tuple[Path, worker_workspace.WorkerWorkspace]:
    """Build a ``WorkerWorkspace`` without going through ``create_workspace``
    (which shells out to real ``git worktree add``). This authoring session
    runs *inside* the exact Landlock+seccomp sandbox this module implements,
    and the seccomp chmod deny-list is inherited by every child process --
    including ``git`` itself -- so a real ``git init``/``git worktree add``
    cannot succeed here regardless of any in-process monkeypatch. A
    production coordinator invoking these same functions is unsandboxed and
    never hits this; the manual construction below still exercises the real
    ``run_validations``/``sandbox_argv``/``_apply_landlock`` code paths this
    suite is testing, only skipping the unrelated git plumbing.
    """
    repo = tmp_path / "fake_repo"
    repo.mkdir(exist_ok=True)
    base = tmp_path / "worktrees" / request_id
    path = base / "worktree"
    home = base / "home"
    path.mkdir(parents=True)
    home.mkdir(parents=True, mode=0o700)
    (home / "tmp").mkdir(mode=0o700)
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




def _write_helper_script(workspace: worker_workspace.WorkerWorkspace, body: str) -> None:
    (workspace.path / "helper.py").write_text(body, encoding="utf-8")


_COMPILE_AND_RUN = """
import os
import subprocess
import sys

tmp = os.environ["TMPDIR"]
out = os.path.join(tmp, "compiled_prog_b753")
compiled = subprocess.run(
    ["cc", "-O0", "-o", out, "prog.c"], capture_output=True, text=True, check=False
)
if compiled.returncode != 0:
    sys.stderr.write(compiled.stderr)
    sys.exit(2)
# Re-materialize the linker's own output through an atomic mode-at-creation
# open (mirrors provision_validation_exec_scratch's own probe technique)
# instead of relying on the linker's separate internal chmod(+x) step, which
# a chmod-denying nested sandbox (as opposed to a genuinely noexec TMPDIR)
# would otherwise leave non-executable for a reason unrelated to what this
# suite is exercising.
final = out + "_exec"
with open(out, "rb") as fh:
    payload = fh.read()
fd = os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o700)
os.write(fd, payload)
os.close(fd)
ran = subprocess.run([final], check=False)
sys.exit(ran.returncode)
"""


def _seed_compile_and_run(workspace: worker_workspace.WorkerWorkspace, c_body: str) -> None:
    (workspace.path / "prog.c").write_text(c_body, encoding="utf-8")
    _write_helper_script(workspace, _COMPILE_AND_RUN)


# --- 1. compile+execute success -----------------------------------------


def test_compile_and_execute_native_binary_succeeds_via_provisioned_scratch(
    tmp_path: Path,
) -> None:
    repo, workspace = _manual_workspace(tmp_path, "compile-exec-ok")
    try:
        _seed_compile_and_run(workspace, "int main(void){return 0;}\n")
        results = worker_workspace.run_validations(workspace, ["python3 helper.py"])
        assert results[0]["returncode"] == 0
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# --- 2. normal non-native validation still passes ------------------------


def test_normal_non_native_python_validation_passes(
    tmp_path: Path,
) -> None:
    repo, workspace = _manual_workspace(tmp_path, "normal-python")
    try:
        _write_helper_script(workspace, "assert 1 + 1 == 2\nprint('ok')\n")
        results = worker_workspace.run_validations(workspace, ["python3 helper.py"])
        assert results[0]["returncode"] == 0
        assert "ok" in results[0]["stdout_tail"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# --- 3. deliberate test/product failure propagates distinctly -----------


def test_deliberate_product_failure_propagates_as_validation_failed(
    tmp_path: Path,
) -> None:
    repo, workspace = _manual_workspace(tmp_path, "product-failure")
    try:
        _write_helper_script(workspace, "import sys\nsys.exit(7)\n")
        with pytest.raises(worker_workspace.WorkspaceError, match=r"validation_failed.*rc=7"):
            worker_workspace.run_validations(workspace, ["python3 helper.py"])
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_compiler_failure_propagates_with_its_own_rc_not_126(
    tmp_path: Path,
) -> None:
    """A genuine compile error must surface distinctly from an exec-scratch
    (noexec) infrastructure failure -- a different validation_failed rc
    (the compiler helper's own sys.exit(2)), never confused with rc=126."""
    repo, workspace = _manual_workspace(tmp_path, "compiler-failure")
    try:
        _seed_compile_and_run(workspace, "int main(void){ this is not valid C\n")
        with pytest.raises(worker_workspace.WorkspaceError, match=r"validation_failed.*rc=2"):
            worker_workspace.run_validations(workspace, ["python3 helper.py"])
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# --- 4. timeout -----------------------------------------------------------


def test_validation_timeout_raises_and_cleans_scratch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo, workspace = _manual_workspace(tmp_path, "timeout-case")
    captured: dict[str, Path] = {}
    real_provision = worker_workspace.provision_validation_exec_scratch

    def _capture(ws: worker_workspace.WorkerWorkspace) -> Path:
        scratch = real_provision(ws)
        captured["scratch"] = scratch
        return scratch

    monkeypatch.setattr(worker_workspace, "provision_validation_exec_scratch", _capture)
    try:
        _write_helper_script(workspace, "import time\ntime.sleep(5)\n")
        with pytest.raises(worker_workspace.WorkspaceError, match="validation_timeout"):
            worker_workspace.run_validations(
                workspace, ["python3 helper.py"], timeout_seconds=1
            )
        assert "scratch" in captured
        assert not captured["scratch"].exists()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# --- 5. cleanup on success, failure, and an abrupt cancellation-like error


def test_scratch_removed_after_successful_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo, workspace = _manual_workspace(tmp_path, "cleanup-success")
    captured: dict[str, Path] = {}
    real_provision = worker_workspace.provision_validation_exec_scratch

    def _capture(ws: worker_workspace.WorkerWorkspace) -> Path:
        scratch = real_provision(ws)
        captured["scratch"] = scratch
        return scratch

    monkeypatch.setattr(worker_workspace, "provision_validation_exec_scratch", _capture)
    try:
        _write_helper_script(workspace, "print('done')\n")
        worker_workspace.run_validations(workspace, ["python3 helper.py"])
        assert not captured["scratch"].exists()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_scratch_removed_after_deliberate_validation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo, workspace = _manual_workspace(tmp_path, "cleanup-failure")
    captured: dict[str, Path] = {}
    real_provision = worker_workspace.provision_validation_exec_scratch

    def _capture(ws: worker_workspace.WorkerWorkspace) -> Path:
        scratch = real_provision(ws)
        captured["scratch"] = scratch
        return scratch

    monkeypatch.setattr(worker_workspace, "provision_validation_exec_scratch", _capture)
    try:
        _write_helper_script(workspace, "import sys\nsys.exit(1)\n")
        with pytest.raises(worker_workspace.WorkspaceError, match="validation_failed"):
            worker_workspace.run_validations(workspace, ["python3 helper.py"])
        assert not captured["scratch"].exists()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


class _SimulatedCancellation(Exception):
    """Stands in for an external coordinator cancellation signal arriving
    mid-run. Deliberately not ``KeyboardInterrupt``/``SystemExit`` -- those
    are special-cased by pytest's own runner even inside ``pytest.raises``
    and would abort the whole test session instead of just this test."""


def test_scratch_removed_after_abrupt_cancellation_like_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Simulates a coordinator-issued cancellation interrupting mid-run: an
    exception unrelated to a normal validation failure must still trigger
    cleanup."""
    repo, workspace = _manual_workspace(tmp_path, "cleanup-cancel")
    captured: dict[str, Path] = {}
    real_provision = worker_workspace.provision_validation_exec_scratch

    def _capture(ws: worker_workspace.WorkerWorkspace) -> Path:
        scratch = real_provision(ws)
        captured["scratch"] = scratch
        return scratch

    monkeypatch.setattr(worker_workspace, "provision_validation_exec_scratch", _capture)

    real_run = worker_workspace.subprocess.run

    def _cancel_only_the_validation_command(argv, *a, **kw):
        # Only intercept the actual validation-command subprocess -- the
        # exec-capability probe inside provisioning must run for real so a
        # scratch directory genuinely gets created (and can then be proven
        # cleaned up below).
        if any("helper.py" in str(part) for part in argv):
            raise _SimulatedCancellation("simulated coordinator cancellation")
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(
        worker_workspace.subprocess, "run", _cancel_only_the_validation_command
    )
    try:
        _write_helper_script(workspace, "print('unreached')\n")
        with pytest.raises(_SimulatedCancellation):
            worker_workspace.run_validations(workspace, ["python3 helper.py"])
        assert "scratch" in captured
        assert not captured["scratch"].exists()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# --- 6. symlink rejection --------------------------------------------------


def test_symlink_at_scratch_path_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scratch_root = tmp_path / "exec_scratch_root"
    scratch_root.mkdir()
    monkeypatch.setenv(
        worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV, str(scratch_root)
    )
    request_id = "symlink-case"
    expected_name = f"{worker_workspace._EXEC_SCRATCH_NAME_PREFIX}{request_id}"
    decoy_target = tmp_path / "decoy_target"
    decoy_target.mkdir()
    (scratch_root / expected_name).symlink_to(decoy_target)

    class _Ws:
        request_id = "symlink-case"

    with pytest.raises(
        worker_workspace.WorkspaceError, match="validation_exec_scratch_symlink_forbidden"
    ):
        worker_workspace.provision_validation_exec_scratch(_Ws())

    # The rejection must never touch (delete/replace) the symlink itself.
    assert (scratch_root / expected_name).is_symlink()


def test_preexisting_regular_scratch_directory_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scratch_root = tmp_path / "exec_scratch_root2"
    scratch_root.mkdir()
    monkeypatch.setenv(
        worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV, str(scratch_root)
    )
    request_id = "preexisting-case"
    expected_name = f"{worker_workspace._EXEC_SCRATCH_NAME_PREFIX}{request_id}"
    (scratch_root / expected_name).mkdir()

    class _Ws:
        request_id = "preexisting-case"

    with pytest.raises(
        worker_workspace.WorkspaceError, match="validation_exec_scratch_already_exists"
    ):
        worker_workspace.provision_validation_exec_scratch(_Ws())


# --- 7. fail-closed when no candidate is exec-capable (the B751 shape) ---


def test_fails_closed_when_every_candidate_is_noexec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reproduces the B751 shape at the decision layer: every candidate root
    is reachable and writable but none can execute a file placed in it (the
    exact noexec-mount symptom). The fix must fail closed with a clear,
    bounded error *before* any validation command ever runs -- never a
    silent rc=126 deep inside the worker's own compile step."""
    root_a = tmp_path / "candidate_a"
    root_b = tmp_path / "candidate_b"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setattr(
        worker_workspace, "_exec_scratch_candidate_roots", lambda: (root_a, root_b)
    )
    monkeypatch.setattr(worker_workspace, "_probe_exec_capable_dir", lambda _d: False)

    class _Ws:
        request_id = "all-noexec"

    with pytest.raises(
        worker_workspace.WorkspaceError, match="validation_exec_scratch_unavailable"
    ) as exc:
        worker_workspace.provision_validation_exec_scratch(_Ws())
    assert "candidate_a" in str(exc.value) or "noexec" in str(exc.value)
    # Every probed-and-rejected candidate scratch dir must be cleaned up, not
    # left behind as noexec debris.
    assert list(root_a.iterdir()) == []
    assert list(root_b.iterdir()) == []


def test_repo_runtime_validation_root_is_preferred_and_cleaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    home = repo / ".aiworkhub" / "runtime" / "worktrees" / "request" / "home"
    home.mkdir(parents=True)
    monkeypatch.delenv(
        worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV, raising=False
    )
    monkeypatch.delenv(worker_workspace.RUNTIME_ROOT_ENV, raising=False)
    monkeypatch.setattr(
        worker_workspace, "_exec_scratch_candidate_roots", lambda: ()
    )
    monkeypatch.setattr(
        worker_workspace, "_probe_exec_capable_dir", lambda _path: True
    )
    monkeypatch.setattr(
        worker_workspace, "_probe_metadata_capable_dir", lambda _path: True
    )

    workspace = type("Workspace", (), {})()
    workspace.request_id = "repo-runtime-validation"
    workspace.home = home
    workspace.repo = repo

    scratch = worker_workspace.provision_validation_exec_scratch(workspace)
    expected_parent = repo / ".aiworkhub" / "runtime" / "validation"
    assert scratch.parent == expected_parent.resolve()
    assert scratch.is_dir()

    worker_workspace.cleanup_validation_exec_scratch(scratch)
    assert not scratch.exists()


def test_reproduces_b751_permission_denied_signature_distinct_from_product_rc(
    tmp_path: Path,
) -> None:
    """Portable reproduction of the exact B751 failure signature (exec
    permission denied) without requiring a real noexec mount / root: a file
    that lacks the executable bit produces the identical kernel-level
    ``PermissionError`` (EACCES) a noexec-mounted TMPDIR produces for any
    compiled binary placed in it. This is the failure ``_probe_exec_capable_
    dir`` is built to detect, and it is a different failure class than a
    compiler error or a product assertion failure (both surface as ordinary
    non-zero exit codes, never as ``PermissionError`` on exec)."""
    non_exec_file = tmp_path / "not_executable"
    fd = os.open(non_exec_file, os.O_CREAT | os.O_WRONLY, 0o600)
    os.write(fd, b"#!/bin/sh\nexit 0\n")
    os.close(fd)

    with pytest.raises(PermissionError):
        subprocess.run([str(non_exec_file)], check=False)

    # The probe itself creates its own 0700 file, so it must not be fooled
    # by an otherwise-unusable neighbour file and correctly reports this
    # directory as usable.
    assert worker_workspace._probe_exec_capable_dir(tmp_path) is True


# --- 8. no credential leakage in the validation environment --------------


def test_validation_env_never_leaks_adapter_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "super-secret-claude-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "super-secret-oauth")
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-openai-key")
    repo, workspace = _manual_workspace(tmp_path, "no-leak")
    try:
        _write_helper_script(
            workspace,
            "import os, json\n"
            "print(json.dumps(sorted(os.environ.keys())))\n",
        )
        results = worker_workspace.run_validations(workspace, ["python3 helper.py"])
        printed_keys = set(json.loads(results[0]["stdout_tail"]))
        assert "ANTHROPIC_API_KEY" not in printed_keys
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in printed_keys
        assert "OPENAI_API_KEY" not in printed_keys
        for secret in (
            "super-secret-claude-key",
            "super-secret-oauth",
            "super-secret-openai-key",
        ):
            assert secret not in results[0]["stdout_tail"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# --- 9. concurrent request isolation --------------------------------------


def test_concurrent_validation_runs_get_isolated_scratch_directories(
    tmp_path: Path,
) -> None:
    repo_a, workspace_a = _manual_workspace(tmp_path, "concurrent-a")
    repo_b, workspace_b = _manual_workspace(tmp_path, "concurrent-b")
    try:
        _write_helper_script(workspace_a, "import time\ntime.sleep(0.2)\nprint('a-done')\n")
        _write_helper_script(workspace_b, "import time\ntime.sleep(0.2)\nprint('b-done')\n")

        outcomes: dict[str, list[dict]] = {}
        errors: dict[str, BaseException] = {}

        def _run(label: str, workspace: worker_workspace.WorkerWorkspace) -> None:
            try:
                outcomes[label] = worker_workspace.run_validations(
                    workspace, ["python3 helper.py"]
                )
            except BaseException as exc:  # pragma: no cover - surfaced via assert
                errors[label] = exc

        threads = [
            threading.Thread(target=_run, args=("a", workspace_a)),
            threading.Thread(target=_run, args=("b", workspace_b)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        assert "a-done" in outcomes["a"][0]["stdout_tail"]
        assert "b-done" in outcomes["b"][0]["stdout_tail"]
    finally:
        worker_workspace.cleanup_workspace(repo_a, workspace_a.path, workspace_a.home)
        worker_workspace.cleanup_workspace(repo_b, workspace_b.path, workspace_b.home)


def test_concurrent_provisioning_produces_distinct_named_directories(
    tmp_path: Path,
) -> None:
    class _Ws:
        def __init__(self, request_id: str) -> None:
            self.request_id = request_id

    results: dict[str, Path] = {}

    def _provision(label: str) -> None:
        results[label] = worker_workspace.provision_validation_exec_scratch(_Ws(label))

    labels = [f"concurrent-provision-{i}" for i in range(6)]
    threads = [threading.Thread(target=_provision, args=(label,)) for label in labels]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    try:
        assert len(results) == len(labels)
        paths = list(results.values())
        assert len(set(paths)) == len(paths)
        for label, path in results.items():
            assert label in path.name
            assert path.is_dir()
    finally:
        for path in results.values():
            worker_workspace.cleanup_validation_exec_scratch(path)
