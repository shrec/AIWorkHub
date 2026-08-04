from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_workspace  # noqa: E402


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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    (root / "read").mkdir()
    (root / "out").mkdir()
    (root / "read" / "input.txt").write_text("input-v1\n", encoding="utf-8")
    (root / "out" / "result.txt").write_text("result-v1\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("agents-v1\n", encoding="utf-8")
    (root / "parent-secret.txt").write_text("secret\n", encoding="utf-8")
    assert (
        _git(
            root,
            "add",
            "read/input.txt",
            "out/result.txt",
            "AGENTS.md",
            "parent-secret.txt",
        ).returncode
        == 0
    )
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    return root


def _workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path, request: str):
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "worktrees"),
    )
    return worker_workspace.create_workspace(
        repo,
        request,
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["read/input.txt"],
        },
        "validation",
    )


def test_workspace_is_detached_and_seeded_parent_changes_are_not_worker_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    (repo / "read" / "input.txt").write_text("parent-dirty-input\n", encoding="utf-8")
    (repo / "out" / "result.txt").write_text("parent-dirty-result\n", encoding="utf-8")
    workspace = _workspace(monkeypatch, tmp_path, repo, "seeded")
    try:
        assert workspace.path != repo
        assert repo not in workspace.path.parents
        assert _git(workspace.path, "symbolic-ref", "-q", "HEAD").returncode != 0
        assert worker_workspace.enforce_scope(workspace) == []

        (workspace.path / "read" / "input.txt").write_text("worker-outside\n", encoding="utf-8")
        with pytest.raises(worker_workspace.WorkspaceError, match="scope_violation"):
            worker_workspace.enforce_scope(workspace)
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_rework_workspace_materializes_hash_pinned_predecessor_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = worker_workspace.create_workspace(
        repo,
        "predecessor",
        {"allowed_writes": ["out/result.txt"]},
        "validation",
    )
    successor = None
    try:
        candidate = predecessor.path / "out" / "result.txt"
        candidate.write_text("reviewed candidate\n", encoding="utf-8")
        candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        successor = worker_workspace.create_workspace(
            repo,
            "successor",
            {
                "allowed_writes": ["out/result.txt"],
                "rework_predecessor": {
                    "schema_id": "aiworkhub.rework_predecessor.v1",
                    "request_id": "predecessor",
                    "workspace": predecessor.as_metadata(),
                    "changed_path_hashes": {"out/result.txt": candidate_hash},
                },
            },
            "validation",
        )
        successor_output = successor.path / "out" / "result.txt"
        assert successor_output.read_text(encoding="utf-8") == "reviewed candidate\n"
        assert successor.workspace_baseline["out/result.txt"].endswith(candidate_hash)
        assert worker_workspace.enforce_scope(successor) == []

        successor_output.write_text("reworked candidate\n", encoding="utf-8")
        assert worker_workspace.enforce_scope(successor) == ["out/result.txt"]
    finally:
        if successor is not None:
            worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


def test_residual_contract_allows_only_declared_json_pointer_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = worker_workspace.create_workspace(
        repo,
        "json-predecessor",
        {"allowed_writes": ["out/result.json"]},
        "validation",
    )
    successor = None
    try:
        candidate = predecessor.path / "out" / "result.json"
        candidate.write_text(
            json.dumps({"rows": [{"id": 1, "value": "keep"}, {"id": 2, "value": "bad"}]}),
            encoding="utf-8",
        )
        candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        card = {
            "allowed_writes": ["out/result.json"],
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "json-predecessor",
                "workspace": predecessor.as_metadata(),
                "changed_path_hashes": {"out/result.json": candidate_hash},
                "residual_identities": [
                    {"path": "out/result.json", "pointer": "/rows/1"},
                ],
            },
        }
        successor = worker_workspace.create_workspace(
            repo, "json-successor", card, "validation"
        )
        manifest = worker_workspace.build_residual_contract_manifest(successor, card)
        output = successor.path / "out" / "result.json"
        payload = json.loads(output.read_text(encoding="utf-8"))
        payload["rows"][1]["value"] = "fixed"
        output.write_text(json.dumps(payload), encoding="utf-8")
        assert worker_workspace.validate_residual_contract(successor, manifest)[0]["pass"]

        payload["rows"][0]["value"] = "unexpected"
        output.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="residual_contract_non_residual_changed",
        ):
            worker_workspace.validate_residual_contract(successor, manifest)
    finally:
        if successor is not None:
            worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


def test_residual_contract_supports_whole_file_code_rework(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    predecessor = worker_workspace.create_workspace(
        repo,
        "code-predecessor",
        {"allowed_writes": ["src/repair.py"]},
        "validation",
    )
    successor = None
    try:
        candidate = predecessor.path / "src" / "repair.py"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("VALUE = 'bad'\n", encoding="utf-8")
        candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        card = {
            "allowed_writes": ["src/repair.py"],
            "rework_predecessor": {
                "schema_id": "aiworkhub.rework_predecessor.v1",
                "request_id": "code-predecessor",
                "workspace": predecessor.as_metadata(),
                "changed_path_hashes": {"src/repair.py": candidate_hash},
                "residual_identities": [
                    {
                        "path": "src/repair.py",
                        "pointer": "/CORRECTNESS-REPAIR-001",
                    },
                ],
            },
        }
        successor = worker_workspace.create_workspace(
            repo, "code-successor", card, "validation"
        )
        manifest = worker_workspace.build_residual_contract_manifest(successor, card)
        assert manifest[0]["scope"] == "whole_file"
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="residual_contract_file_unchanged",
        ):
            worker_workspace.validate_residual_contract(successor, manifest)

        output = successor.path / "src" / "repair.py"
        output.write_text("VALUE = 'fixed'\n", encoding="utf-8")
        result = worker_workspace.validate_residual_contract(successor, manifest)[0]
        assert result["pass"] is True
        assert result["scope"] == "whole_file"
        assert result["observed_file_hash"] != result["predecessor_file_hash"]
    finally:
        if successor is not None:
            worker_workspace.cleanup_workspace(repo, successor.path, successor.home)
        worker_workspace.cleanup_workspace(repo, predecessor.path, predecessor.home)


def test_claude_workspace_preseeds_exact_project_trust_without_parent_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    workspace = worker_workspace.create_workspace(
        repo,
        "claude-trust",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["read/input.txt"],
        },
        "claude_cli",
    )
    try:
        config_path = workspace.home / ".claude.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config == {
            "projects": {
                str(repo.resolve()): {
                    "hasTrustDialogAccepted": True,
                    "projectOnboardingSeenCount": 1,
                }
            }
        }
        assert os.name == "nt" or stat.S_IMODE(config_path.stat().st_mode) == 0o600
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_promotion_is_scope_checked_parent_guarded_and_restart_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "promote")
    try:
        (workspace.path / "out" / "result.txt").write_text("worker-result\n", encoding="utf-8")
        changed = worker_workspace.enforce_scope(workspace)
        assert changed == ["out/result.txt"]
        assert worker_workspace.promote(workspace, changed) == changed
        assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "worker-result\n"
        assert worker_workspace.promote(workspace, changed) == changed
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)

    conflict = _workspace(monkeypatch, tmp_path, repo, "conflict")
    try:
        (conflict.path / "out" / "result.txt").write_text("worker-two\n", encoding="utf-8")
        (repo / "out" / "result.txt").write_text("owner-two\n", encoding="utf-8")
        with pytest.raises(worker_workspace.WorkspaceError, match="parent_changed_since_launch"):
            worker_workspace.promote(conflict, ["out/result.txt"])
        assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "owner-two\n"
    finally:
        worker_workspace.cleanup_workspace(repo, conflict.path, conflict.home)


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub hosted runners cannot execute nested Landlock workers",
)
def test_landlock_fallback_allows_declared_output_and_denies_parent_and_git_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "landlock")
    parent_secret = repo / "parent-secret.txt"
    parent_mode = stat.S_IMODE(parent_secret.stat().st_mode)
    script = """
from pathlib import Path
import os
import sys
Path('out/result.txt').write_text('allowed\\n', encoding='utf-8')
denied = 0
for target in (Path(sys.argv[1]), Path('.git')):
    try:
        target.write_text('forbidden\\n', encoding='utf-8')
    except PermissionError:
        denied += 1
try:
    os.chmod(sys.argv[1], 0o600)
except PermissionError:
    denied += 1
if denied != 3:
    raise SystemExit(17)
print('landlock-denied-parent-git-and-metadata')
"""
    argv = worker_workspace.sandbox_argv(
        workspace,
        "validation",
        [sys.executable, "-c", script, str(parent_secret)],
        backend="landlock",
    )
    try:
        result = subprocess.run(
            argv,
            cwd="/",
            env=worker_workspace.sanitized_env("validation", home=workspace.home),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
        )
        assert result.returncode == 0, result.stderr
        assert "landlock-denied-parent-git-and-metadata" in result.stdout
        assert (workspace.path / "out" / "result.txt").read_text(encoding="utf-8") == "allowed\n"
        assert parent_secret.read_text(encoding="utf-8") == "secret\n"
        assert stat.S_IMODE(parent_secret.stat().st_mode) == parent_mode
        assert (workspace.path / ".git").is_file()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 3,
    reason="Root-file replacement requires Landlock truncate support",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub hosted runners cannot execute nested Landlock workers",
)
def test_landlock_root_file_allows_in_place_save_but_denies_sibling_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    workspace = worker_workspace.create_workspace(
        repo,
        "landlock-root-file",
        {
            "allowed_writes": ["AGENTS.md"],
            "read_first": ["read/input.txt"],
        },
        "validation",
    )
    script = """
from pathlib import Path
Path('AGENTS.md').write_text('agents-v2\\n', encoding='utf-8')
denied = 0
for target in (Path('.AGENTS.md.editor-temp'), Path('.git')):
    try:
        target.write_text('forbidden\\n', encoding='utf-8')
    except PermissionError:
        denied += 1
if denied != 2:
    raise SystemExit(17)
print('landlock-root-file-bounded')
"""
    argv = worker_workspace.sandbox_argv(
        workspace,
        "validation",
        [sys.executable, "-c", script],
        backend="landlock",
    )
    try:
        result = subprocess.run(
            argv,
            cwd="/",
            env=worker_workspace.sanitized_env("validation", home=workspace.home),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
        )
        assert result.returncode == 0, result.stderr
        assert "landlock-root-file-bounded" in result.stdout
        assert (workspace.path / "AGENTS.md").read_text(encoding="utf-8") == "agents-v2\n"
        assert not (workspace.path / ".AGENTS.md.editor-temp").exists()
        assert (workspace.path / ".git").is_file()
        assert worker_workspace.enforce_scope(workspace) == ["AGENTS.md"]
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_secure_sandbox_selection_fails_closed_without_bwrap_or_landlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(worker_workspace.SANDBOX_BACKEND_ENV, "auto")
    monkeypatch.setattr(worker_workspace, "_bubblewrap_usable", lambda _path: False)
    monkeypatch.setattr(worker_workspace, "landlock_abi_version", lambda: 0)
    with pytest.raises(worker_workspace.WorkspaceError, match="secure_sandbox_unavailable"):
        worker_workspace.select_sandbox_backend()


def test_workspace_rejects_git_metadata_as_an_allowed_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    with pytest.raises(worker_workspace.WorkspaceError, match="git_metadata_write_forbidden"):
        worker_workspace.create_workspace(
            repo,
            "git-metadata",
            {"allowed_writes": [".git/config"]},
            "validation",
        )


def test_sanitized_env_is_allowlisted_and_json_files_are_0600(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "allowed-adapter-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-allowed")
    monkeypatch.setenv("AIWORKHUB_REPO", "/sensitive/parent")
    monkeypatch.setenv("AIWORKHUB_ALLOW_LAUNCH", "1")
    home = tmp_path / "home"
    if os.name != "nt":
        home.mkdir(mode=0o700)
        (home / "tmp").mkdir(mode=0o700)
    env = worker_workspace.sanitized_env("claude_cli", home=home)
    assert env["HOME"] == str(home)
    assert env["ANTHROPIC_API_KEY"] == "allowed-adapter-secret"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "AIWORKHUB_REPO" not in env
    assert "AIWORKHUB_ALLOW_LAUNCH" not in env

    target = tmp_path / "private" / "request.json"
    target.parent.mkdir()
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o644)
    worker_workspace.write_json_0600(target, {"ok": True})
    assert os.name == "nt" or stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps provisioning behavior")
def test_sanitized_env_verifies_preprovisioned_home_without_chmod(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    tmp = home / "tmp"
    home.mkdir(mode=0o700)
    tmp.mkdir(mode=0o700)

    def _deny_chmod(_path: object, _mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(worker_workspace.os, "chmod", _deny_chmod)

    env = worker_workspace.sanitized_env(
        "validation", home=home, verify_preprovisioned_home=True
    )

    assert env["HOME"] == str(home.resolve())
    assert env["TMPDIR"] == str(tmp.resolve())
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(tmp.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps provisioning behavior")
@pytest.mark.parametrize(
    "shape,match",
    [
        ("missing_home", "sanitized_home_missing"),
        ("home_file", "sanitized_home_not_directory"),
        ("home_symlink", "sanitized_home_symlink_forbidden"),
        ("home_group_readable", "sanitized_home_not_private"),
        ("missing_tmp", "sanitized_tmp_missing"),
        ("tmp_file", "sanitized_tmp_not_directory"),
        ("tmp_symlink", "sanitized_tmp_symlink_forbidden"),
        ("tmp_group_readable", "sanitized_tmp_not_private"),
    ],
)
def test_sanitized_env_rejects_unsafe_posix_home_shapes(
    tmp_path: Path,
    shape: str,
    match: str,
) -> None:
    home = tmp_path / "home"
    tmp = home / "tmp"
    if shape == "missing_home":
        pass
    elif shape == "home_file":
        home.write_text("not a directory", encoding="utf-8")
    elif shape == "home_symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        home.symlink_to(target, target_is_directory=True)
    else:
        home.mkdir(mode=0o700)
        if shape == "home_group_readable":
            os.chmod(home, 0o750)
        elif shape == "missing_tmp":
            pass
        elif shape == "tmp_file":
            tmp.write_text("not a directory", encoding="utf-8")
        elif shape == "tmp_symlink":
            target = tmp_path / "target_tmp"
            target.mkdir(mode=0o700)
            tmp.symlink_to(target, target_is_directory=True)
        else:
            tmp.mkdir(mode=0o700)
            os.chmod(tmp, 0o750)

    with pytest.raises(worker_workspace.WorkspaceError, match=match):
        worker_workspace.sanitized_env(
            "validation", home=home, verify_preprovisioned_home=True
        )


def test_bubblewrap_home_string_is_single_sourced_for_env_and_bind_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B314_F004 regression: sanitized_env(home=None) (used for the
    bubblewrap backend) and sandbox_argv's bwrap bind-mount target used to
    each call Path.home() independently and only *happened* to agree. Both
    must now derive from the one shared bubblewrap_home_env_value() so they
    can never silently diverge -- a real divergence would break credential
    lookups for a worker running under bubblewrap.
    """
    shared = worker_workspace.bubblewrap_home_env_value()

    env = worker_workspace.sanitized_env("claude_cli", home=None)
    assert env["HOME"] == shared

    workspace = worker_workspace.WorkerWorkspace(
        request_id="bwrap-home-check",
        repo=tmp_path,
        path=tmp_path / "worktree",
        home=tmp_path / "home",
        allowed_writes=("out/result.txt",),
        parent_baseline={},
        workspace_baseline={},
    )
    (workspace.path / "out").mkdir(parents=True)
    (workspace.path / "out" / "result.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv(worker_workspace.BWRAP_ENV, "/usr/bin/bwrap")
    argv = worker_workspace.sandbox_argv(
        workspace, "claude_cli", ["/bin/true"], backend="bubblewrap"
    )
    bind_index = argv.index("--bind")
    assert argv[bind_index + 1] == str(workspace.home)
    assert argv[bind_index + 2] == shared


def test_unlink_if_regular_removes_files_but_never_follows_symlinks(
    tmp_path: Path,
) -> None:
    """B314_F007 regression: the spec/cancel-marker cleanup helper must
    remove a plain regular file, but must never act through a symlink --
    neither deleting the symlink's target nor silently no-oping in a way
    that could be mistaken for success on the wrong path."""
    regular = tmp_path / "spec.json"
    regular.write_text("{}", encoding="utf-8")
    worker_workspace.unlink_if_regular(regular)
    assert not regular.exists()

    # Missing path is a silent no-op (mirrors the old missing_ok=True call).
    worker_workspace.unlink_if_regular(regular)

    target = tmp_path / "sensitive_target.txt"
    target.write_text("do-not-delete", encoding="utf-8")
    link = tmp_path / "spec_symlink.json"
    link.symlink_to(target)

    worker_workspace.unlink_if_regular(link)

    assert link.is_symlink(), "a symlinked spec path must be left untouched"
    assert target.exists() and target.read_text(encoding="utf-8") == "do-not-delete"


# ── B561: ignored required-output promotion regression ──────────────────


def _repo_with_gitignore(tmp_path: Path) -> Path:
    """Fixture repo with a .gitignore that excludes ``*.bin`` files."""
    root = tmp_path / "parent"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    (root / "read").mkdir()
    (root / "out").mkdir()
    (root / "read" / "input.txt").write_text("input-v1\n", encoding="utf-8")
    (root / "out" / "result.txt").write_text("result-v1\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.bin\n", encoding="utf-8")
    assert _git(root, "add", "read/input.txt", "out/result.txt", ".gitignore").returncode == 0
    assert _git(root, "commit", "-qm", "fixture-with-gitignore").returncode == 0
    return root


def _ignored_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
    request: str,
    extra_allowed: str | None = None,
) -> worker_workspace.WorkerWorkspace:
    allowed = ["out/result.txt"]
    if extra_allowed is not None:
        allowed.append(extra_allowed)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV,
        str(tmp_path / "worktrees"),
    )
    return worker_workspace.create_workspace(
        repo,
        request,
        {"allowed_writes": allowed, "read_first": ["read/input.txt"]},
        "validation",
    )


def test_validate_required_outputs_finds_gitignored_file_missed_by_changed_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561: a gitignored required output (e.g. ``out/data.bin`` matched by
    glob ``out/*.bin``) is found and validated by validate_required_outputs
    even though changed_paths (git-diff + git-ls-files --exclude-standard)
    does not discover it."""
    repo = _repo_with_gitignore(tmp_path)
    workspace = _ignored_workspace(
        monkeypatch, tmp_path, repo, "b561-ignored", extra_allowed="out/*.bin"
    )
    try:
        # Worker writes a real binary payload (not a placeholder).
        (workspace.path / "out").mkdir(parents=True, exist_ok=True)
        (workspace.path / "out" / "data.bin").write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08")

        # changed_paths should NOT see the gitignored file.
        changed = worker_workspace.changed_paths(workspace)
        assert "out/data.bin" not in changed, (
            "gitignored .bin must not appear in git-diff-derived changed_paths"
        )

        # validate_required_outputs MUST find and record it.
        records = worker_workspace.validate_required_outputs(
            workspace, ["out/*.bin"]
        )
        assert len(records) == 1
        assert records[0]["path"] == "out/data.bin"
        assert records[0]["bytes"] == 8
        assert records[0]["sha256"] is not None
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_promotion_includes_validated_required_output_not_in_changed_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561: when a validated required output path is missing from
    changed_paths (gitignored), the caller unions it into the promotion set
    and promote() succeeds with all fail-closed guards intact."""
    repo = _repo_with_gitignore(tmp_path)
    workspace = _ignored_workspace(
        monkeypatch, tmp_path, repo, "b561-promote", extra_allowed="out/*.bin"
    )
    try:
        (workspace.path / "out").mkdir(parents=True, exist_ok=True)
        (workspace.path / "out" / "data.bin").write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08")
        (workspace.path / "out" / "result.txt").write_text("worker-result\n", encoding="utf-8")

        changed = worker_workspace.enforce_scope(workspace)
        assert "out/data.bin" not in changed  # gitignored

        required_records = worker_workspace.validate_required_outputs(
            workspace, ["out/*.bin"]
        )
        assert len(required_records) == 1

        # Simulate the B561 union fix the coordinator applies.
        validated_paths = {rec["path"] for rec in required_records}
        changed = sorted(set(changed) | validated_paths)

        promoted = worker_workspace.promote(workspace, changed)
        assert "out/data.bin" in promoted
        assert "out/result.txt" in promoted
        assert (repo / "out" / "data.bin").read_bytes() == b"\x01\x02\x03\x04\x05\x06\x07\x08"
        assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "worker-result\n"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_required_output_symlink_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561 guard: a required output that is a symlink must be rejected."""
    repo = _repo_with_gitignore(tmp_path)
    workspace = _ignored_workspace(
        monkeypatch, tmp_path, repo, "b561-symlink", extra_allowed="out/*.bin"
    )
    try:
        (workspace.path / "out").mkdir(parents=True, exist_ok=True)
        target = workspace.path / "out" / "real.bin"
        target.write_bytes(b"\x01")
        link = workspace.path / "out" / "link.bin"
        link.symlink_to(target)

        with pytest.raises(worker_workspace.WorkspaceError, match="required_output_symlink"):
            worker_workspace.validate_required_outputs(workspace, ["out/link.bin"])
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_required_output_unchanged_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561 guard: a required output identical to workspace_baseline is rejected."""
    repo = _repo_with_gitignore(tmp_path)
    workspace = _ignored_workspace(
        monkeypatch, tmp_path, repo, "b561-unchanged", extra_allowed="out/data.bin"
    )
    try:
        (workspace.path / "out").mkdir(parents=True, exist_ok=True)
        # _touch_placeholder created out/data.bin with 0 bytes.
        # validate_required_outputs must reject zero-byte (not allow_empty)
        # OR unchanged-from-placeholder. The placeholder is in workspace_baseline
        # with its empty-file hash, and the file is still empty, so the hash
        # matches → required_output_unchanged.
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="required_output_(unchanged|zero_bytes)",
        ):
            worker_workspace.validate_required_outputs(workspace, ["out/data.bin"])
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_required_output_parent_changed_during_promotion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561 guard: parent-baseline hash protection still rejects a promoted
    required output when the parent file was modified concurrently."""
    repo = _repo_with_gitignore(tmp_path)
    workspace = _ignored_workspace(
        monkeypatch, tmp_path, repo, "b561-parent-changed", extra_allowed="out/*.bin"
    )
    try:
        (workspace.path / "out").mkdir(parents=True, exist_ok=True)
        (workspace.path / "out" / "data.bin").write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08")

        required_records = worker_workspace.validate_required_outputs(
            workspace, ["out/*.bin"]
        )
        validated_paths = {rec["path"] for rec in required_records}
        changed = sorted({"out/result.txt"} | validated_paths)

        # Concurrently modify the parent repo.
        (repo / "out" / "result.txt").write_text("owner-edit\n", encoding="utf-8")

        with pytest.raises(
            worker_workspace.WorkspaceError, match="parent_changed_since_launch"
        ):
            worker_workspace.promote(workspace, changed)

        # Parent must be unchanged after rejection.
        assert (repo / "out" / "result.txt").read_text(encoding="utf-8") == "owner-edit\n"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_required_output_out_of_scope_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B561 guard: a required output not in allowed_writes is rejected immediately."""
    repo = _repo_with_gitignore(tmp_path)
    workspace = _ignored_workspace(
        monkeypatch, tmp_path, repo, "b561-out-of-scope"
    )
    try:
        with pytest.raises(
            worker_workspace.WorkspaceError, match="required_output_not_allowed"
        ):
            worker_workspace.validate_required_outputs(workspace, ["secret/escape.txt"])
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_allow_unchanged_required_outputs_accepts_exact_baseline_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "allow-unchanged")
    try:
        records = worker_workspace.validate_required_outputs(
            workspace,
            ["out/result.txt"],
            allow_unchanged=("out/result.txt",),
        )
        assert records == [
            {
                "pattern": "out/result.txt",
                "path": "out/result.txt",
                "bytes": len((workspace.path / "out" / "result.txt").read_bytes()),
                "sha256": workspace.workspace_baseline["out/result.txt"],
                "unchanged_allowed": True,
            }
        ]
        changed = worker_workspace.enforce_scope(workspace)
        promotable = sorted(
            set(changed) | {rec["path"] for rec in records if not rec["unchanged_allowed"]}
        )
        assert promotable == []
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_allow_unchanged_required_outputs_rejects_changed_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "allow-unchanged-changed")
    try:
        (workspace.path / "out" / "result.txt").write_text("worker-result\n", encoding="utf-8")
        with pytest.raises(
            worker_workspace.WorkspaceError,
            match="allow_unchanged_required_output_changed",
        ):
            worker_workspace.validate_required_outputs(
                workspace,
                ["out/result.txt"],
                allow_unchanged=("out/result.txt",),
            )
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_all_unchanged_required_outputs_is_no_effect_for_caller(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "all-unchanged")
    try:
        records = worker_workspace.validate_required_outputs(
            workspace,
            ["out/result.txt"],
            allow_unchanged=("out/result.txt",),
        )
        changed = worker_workspace.enforce_scope(workspace)
        promotable = sorted(
            set(changed) | {rec["path"] for rec in records if not rec["unchanged_allowed"]}
        )
        with pytest.raises(worker_workspace.WorkspaceError, match="no_effect"):
            if not promotable:
                raise worker_workspace.WorkspaceError("no_effect")
            worker_workspace.promote(workspace, promotable)
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_validation_pythonpath_prefix_is_removed_from_executable_argv() -> None:
    argv, components = worker_workspace.parse_validation_command(
        "PYTHONPATH=tools/geoai-task-mcp/src python3 -m pytest -q tools/x_test.py"
    )
    assert argv == ["python3", "-m", "pytest", "-q", "tools/x_test.py"]
    assert components == ("tools/geoai-task-mcp/src",)
    assert worker_workspace.validation_argv("python3 AITools/taskctl.py verify") == [
        "python3", "AITools/taskctl.py", "verify"
    ]


@pytest.mark.parametrize(
    "command,match",
    [
        ("FOO=bar python3 x.py", "validation_env_assignment_not_supported"),
        ("PYTHONPATH=a PYTHONPATH=b python3 x.py", "validation_env_assignment_multiple"),
        ("PYTHONPATH=a FOO=bar python3 x.py", "validation_env_assignment_multiple"),
        ("PYTHONPATH=only", "validation_env_assignment_without_executable"),
    ],
)
def test_validation_pythonpath_rejects_unknown_or_multiple_assignments(
    command: str, match: str
) -> None:
    with pytest.raises(worker_workspace.WorkspaceError, match=match):
        worker_workspace.parse_validation_command(command)


@pytest.mark.parametrize(
    "command,match",
    [
        ("PYTHONPATH=../escape python3 x.py", "traversal_forbidden"),
        ("PYTHONPATH=a/../../escape python3 x.py", "traversal_forbidden"),
        ("PYTHONPATH= python3 x.py", "pythonpath_empty"),
        ("PYTHONPATH=a::b python3 x.py", "empty_component"),
        ("PYTHONPATH=tools/* python3 x.py", "forbidden_char"),
        ("PYTHONPATH=$(whoami) python3 x.py", "forbidden_char"),
        ("PYTHONPATH=${HOME} python3 x.py", "forbidden_char"),
    ],
)
def test_validation_pythonpath_rejects_unsafe_values(command: str, match: str) -> None:
    with pytest.raises(worker_workspace.WorkspaceError, match=match):
        worker_workspace.parse_validation_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "PYTHONPATH=read; rm -rf / python3 x.py",
        "PYTHONPATH=read python3 x.py | rm -rf /",
        "PYTHONPATH=read python3 x.py > /tmp/out",
    ],
)
def test_validation_pythonpath_preserves_shell_metacharacter_rejection(command: str) -> None:
    with pytest.raises(worker_workspace.WorkspaceError, match="validation_shell_syntax_forbidden"):
        worker_workspace.parse_validation_command(command)


def test_validation_pythonpath_resolution_is_beneath_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    (worktree / "read").mkdir(parents=True)
    workspace = worker_workspace.WorkerWorkspace(
        request_id="pythonpath-resolve",
        repo=tmp_path,
        path=worktree,
        home=tmp_path / "home",
        allowed_writes=("out/result.txt",),
        parent_baseline={},
        workspace_baseline={},
    )
    assert worker_workspace.resolve_validation_pythonpath(
        workspace, "landlock", ("read",)
    ) == str(worktree / "read")
    assert worker_workspace.resolve_validation_pythonpath(
        workspace, "bubblewrap", ("read",)
    ) == f"{worker_workspace.SANDBOX_WORKSPACE}/read"
    if os.name == "nt":
        return
    approved_site = tmp_path / "approved-site-packages"
    approved_site.mkdir()
    monkeypatch.setattr(
        worker_workspace.site, "getusersitepackages", lambda: str(approved_site)
    )
    approved_site = approved_site.resolve()
    components = worker_workspace.parse_validation_command(
        f"PYTHONPATH={os.pathsep.join((str(approved_site), '.', 'read'))} "
        "python3 -m pytest -q x.py"
    )[1]
    assert worker_workspace.resolve_validation_pythonpath(
        workspace, "landlock", components
    ) == os.pathsep.join((str(approved_site), str(worktree), str(worktree / "read")))
    assert worker_workspace.resolve_validation_pythonpath(
        workspace, "bubblewrap", components
    ) == os.pathsep.join(
        ("/validation-pythonpath/0", worker_workspace.SANDBOX_WORKSPACE,
         f"{worker_workspace.SANDBOX_WORKSPACE}/read")
    )
    bubblewrap_argv = worker_workspace.sandbox_argv(
        workspace,
        "validation",
        ["python3", "x.py"],
        backend="bubblewrap",
        validation_readonly_dirs=(approved_site,),
    )
    bind = ["--ro-bind", str(approved_site), "/validation-pythonpath/0"]
    assert any(bubblewrap_argv[i:i + 3] == bind for i in range(len(bubblewrap_argv) - 2))
    with pytest.raises(worker_workspace.WorkspaceError, match="absolute_component_forbidden"):
        worker_workspace.resolve_validation_pythonpath(
            workspace, "landlock", ("/etc",)
        )
    with pytest.raises(worker_workspace.WorkspaceError, match="not_directory"):
        worker_workspace.resolve_validation_pythonpath(
            workspace, "landlock", ("missing",)
        )


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub hosted runners cannot execute nested Landlock validations",
)
def test_validation_pythonpath_override_is_scoped_to_one_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    workspace = _workspace(monkeypatch, tmp_path, repo, "pythonpath-scope")
    (workspace.path / "check_pythonpath.py").write_text(
        "import os\nprint('PP=' + os.environ.get('PYTHONPATH', ''))\n",
        encoding="utf-8",
    )
    try:
        backend = worker_workspace.select_sandbox_backend()
        approved_site = Path(worker_workspace.site.getusersitepackages()).resolve()
        components = (str(approved_site), ".", "read")
        expected = worker_workspace.resolve_validation_pythonpath(
            workspace, backend, components
        )
        first, second = worker_workspace.run_validations(
            workspace,
            [
                f"PYTHONPATH={approved_site}:.:read python3 check_pythonpath.py",
                "python3 check_pythonpath.py",
            ],
        )
        assert first["argv"][0] == "python3"
        assert first["env_override"] == {
            "variable": "PYTHONPATH", "components": list(components)
        }
        assert f"PP={expected}" in first["stdout_tail"]
        assert second["env_override"] is None
        assert expected not in second["stdout_tail"]
        assert "PYTHONPATH" not in os.environ
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_validation_cd_prefix_is_removed_from_executable_argv() -> None:
    argv, components, tmpdir_override, cwd = worker_workspace._parse_validation_command_detailed(
        "cd read && python3 x.py"
    )
    assert argv == ["python3", "x.py"]
    assert components == ()
    assert tmpdir_override is None
    assert cwd == "read"
    assert worker_workspace.parse_validation_command("cd read && python3 x.py") == (
        ["python3", "x.py"], ()
    )


def test_validation_cd_prefix_combines_with_env_assignment() -> None:
    argv, components, tmpdir_override, cwd = worker_workspace._parse_validation_command_detailed(
        "PYTHONPATH=tools/src cd sub/dir && python3 -m pytest -q x_test.py"
    )
    assert argv == ["python3", "-m", "pytest", "-q", "x_test.py"]
    assert components == ("tools/src",)
    assert tmpdir_override is None
    assert cwd == "sub/dir"


@pytest.mark.parametrize(
    "command,match",
    [
        ("cd /abs && python3 x.py", "invalid_repo_path"),
        ("cd ../escape && python3 x.py", "unsafe_repo_path"),
        ("cd . && python3 x.py", "unsafe_repo_path"),
        ("cd sub &&", "validation_cd_command_empty"),
        ("cd sub python3 x.py", "validation_cd_prefix_malformed"),
        ("cd && python3 x.py", "validation_cd_prefix_malformed"),
        ("python3 x.py && python3 y.py", "validation_shell_chain_forbidden"),
        ("cd sub && python3 x.py && python3 y.py", "validation_shell_chain_forbidden"),
    ],
)
def test_validation_cd_prefix_rejects_unsafe_forms(command: str, match: str) -> None:
    with pytest.raises(worker_workspace.WorkspaceError, match=match):
        worker_workspace._parse_validation_command_detailed(command)


def test_validation_cd_prefix_rejects_symlink_escape_at_sandbox_argv_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "cd-symlink")
    try:
        outside = tmp_path / "outside"
        outside.mkdir()
        (workspace.path / "escape-link").symlink_to(outside, target_is_directory=True)
        with pytest.raises(worker_workspace.WorkspaceError, match="symlink_path_component_forbidden"):
            worker_workspace.sandbox_argv(
                workspace,
                "validation",
                ["python3", "x.py"],
                backend="landlock",
                validation_cwd="escape-link",
            )
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_validation_cd_prefix_rejects_non_directory_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "cd-not-dir")
    try:
        with pytest.raises(worker_workspace.WorkspaceError, match="validation_cwd_not_directory"):
            worker_workspace.sandbox_argv(
                workspace,
                "validation",
                ["python3", "x.py"],
                backend="landlock",
                validation_cwd="out/result.txt",
            )
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_validation_cd_prefix_sets_bubblewrap_chdir_beneath_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "cd-bwrap-chdir")
    try:
        argv = worker_workspace.sandbox_argv(
            workspace,
            "validation",
            ["python3", "x.py"],
            backend="bubblewrap",
            validation_cwd="read",
        )
        assert "--chdir" in argv
        assert argv[argv.index("--chdir") + 1] == f"{worker_workspace.SANDBOX_WORKSPACE}/read"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.skipif(
    worker_workspace.landlock_abi_version() < 1,
    reason="Landlock is not supported by this kernel",
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub hosted runners cannot execute nested Landlock validations",
)
def test_validation_cd_prefix_changes_child_cwd_under_landlock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "cd-landlock-exec")
    try:
        result, = worker_workspace.run_validations(
            workspace,
            ["cd read && python3 -c \"print(__import__('os').getcwd())\""],
        )
        assert result["returncode"] == 0, result["stderr_tail"]
        assert result["stdout_tail"].strip() == str((workspace.path / "read").resolve())
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_validation_batch_retains_each_failed_command_and_bounded_streams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path
) -> None:
    workspace = _workspace(monkeypatch, tmp_path, repo, "validation-evidence")
    scratch = tmp_path / "validation-scratch"
    scratch.mkdir()
    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", lambda: "landlock")
    monkeypatch.setattr(
        worker_workspace, "provision_validation_exec_scratch", lambda _workspace: scratch
    )
    monkeypatch.setattr(worker_workspace, "cleanup_validation_exec_scratch", lambda _path: None)
    monkeypatch.setattr(
        worker_workspace,
        "sandbox_argv",
        lambda _workspace, _adapter, argv, **_kwargs: list(argv),
    )
    monkeypatch.setattr(worker_workspace, "sanitized_env", lambda *_args, **_kwargs: {})
    outputs = iter(
        [
            subprocess.CompletedProcess([], 1, "A" * 9_000, "first-error"),
            subprocess.CompletedProcess([], 2, "second-out", "B" * 9_000),
        ]
    )
    monkeypatch.setattr(worker_workspace.subprocess, "run", lambda *_args, **_kwargs: next(outputs))

    with pytest.raises(worker_workspace.ValidationRunError) as caught:
        worker_workspace.run_validations(
            workspace,
            ["python3 -c 'raise SystemExit(1)'", "python3 -c 'raise SystemExit(2)'"],
        )

    rows = caught.value.results
    assert [row["returncode"] for row in rows] == [1, 2]
    assert [row["command"] for row in rows] == [
        "python3 -c 'raise SystemExit(1)'",
        "python3 -c 'raise SystemExit(2)'",
    ]
    assert rows[0]["stdout_truncated"] is True
    assert len(rows[0]["stdout_head"]) == 4_096
    assert len(rows[0]["stdout_tail"]) == 4_096
    assert rows[1]["stderr_truncated"] is True
