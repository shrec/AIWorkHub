"""NF-2026-02364: the worker-lane full snapshot writes only beneath its own root."""

from __future__ import annotations

import errno
import inspect
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# Imported before any audit window so lazy imports never write bytecode inside it.
from aiworkhub import accept_neighbour_tests  # noqa: F401
from aiworkhub import process_launcher
from aiworkhub import process_launcher_validation  # noqa: F401
from aiworkhub import promotion_merge  # noqa: F401
from aiworkhub import repository_state  # noqa: F401
from aiworkhub import storage_registry  # noqa: F401
from aiworkhub import worker_materialize  # noqa: F401
from aiworkhub import worker_snapshot_tree as tree
from aiworkhub import worker_validation_reuse as reuse_mod
from aiworkhub.worker_workspace import WorkspaceError, cleanup_workspace, create_workspace

_COMMAND = "python -c \"print('probe')\""
_CARD = {
    "allowed_writes": ["pkg/feature.py"],
    "read_first": [],
    "immutable_inputs": [],
    "required_outputs": [],
    "validation": [_COMMAND],
}
_REQUEST = "fedcba9876543210fedcba9876543210"
_READ_ONLY_GIT = {"rev-parse", "ls-tree", "cat-file", "diff", "ls-files"}

_AUDIT: dict[str, list] = {"active": [], "writes": [], "spawns": []}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_DESTINATION_EVENTS = {"os.rename": 1, "os.replace": 1, "os.symlink": 1, "os.link": 1,
                       "shutil.copyfile": 1, "shutil.copytree": 1}
_PATH_EVENTS = {"os.mkdir", "os.remove", "os.rmdir", "shutil.rmtree", "os.truncate"}


def _audit(event: str, args: tuple) -> None:
    if not _AUDIT["active"]:
        return
    path: object = None
    if event == "open":
        name, mode, flags = args[0], args[1], args[2]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS
        )
        path = name if writes else None
    elif event in _DESTINATION_EVENTS:
        path = args[_DESTINATION_EVENTS[event]]
    elif event in _PATH_EVENTS:
        # A dir_fd-relative entry (rmtree's own walk) is beneath the rmtree root.
        relative_to_fd = len(args) > 1 and isinstance(args[-1], int) and args[-1] >= 0
        path = None if relative_to_fd and event != "shutil.rmtree" else args[0]
    elif event == "subprocess.Popen":
        command = args[1]
        if isinstance(command, bytes):
            command = os.fsdecode(command)
        # Windows audits the joined command line; split it back into argv.
        argv = (
            [token.strip('"') for token in shlex.split(command, posix=False)]
            if isinstance(command, str)
            else [os.fsdecode(token) for token in command]
        )
        _AUDIT["spawns"].append((argv, args[2]))
    elif event in {"os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.startfile"}:
        _AUDIT["spawns"].append(([repr(args)], None))
    if isinstance(path, (str, bytes, os.PathLike)):
        _AUDIT["writes"].append(os.path.abspath(os.fsdecode(path)))


sys.addaudithook(_audit)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _route(metadata):
    return {"backend": metadata.get("backend")}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "AIWorkHub Test")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "pkg").mkdir()
    (repo / "docs").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "pkg" / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    (repo / "docs" / "readme.txt").write_text("head docs\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (repo / "keep.log").write_text("tracked although ignored\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "add", "-f", "keep.log")
    _git(repo, "commit", "-m", "base")
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    from aiworkhub.process_launcher import PROCESS_DIR_ENV

    monkeypatch.delenv(PROCESS_DIR_ENV, raising=False)
    ws = create_workspace(repo, _REQUEST, _CARD, "validation")
    (ws.path / "pkg" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    # The canonical worktree delta: one tracked edit and one untracked file.
    (repo / "pkg" / "other.py").write_text("OTHER = 2\n", encoding="utf-8")
    (repo / "pkg" / "new_canon.py").write_text("NEW = 1\n", encoding="utf-8")
    granted = tmp_path / "granted"
    granted.mkdir()
    # Inside the worker sandbox TMPDIR is the granted request temp: mirror that.
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(granted / "tmp"))
    (granted / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", None)
    private = tmp_path / "private" / "task_mcp_worker_runtime"
    private.mkdir(parents=True)
    key = private / "audit_hmac.key"
    key.write_bytes(b"k" * 32)
    ledger = private / "audit_ledger.jsonl"
    ledger.write_bytes(b"")
    metadata = {
        **_CARD,
        "request_id": ws.request_id,
        "claim_epoch": 3,
        "worker_mcp": {"audit_hmac_key_path": str(key), "audit_ledger_path": str(ledger)},
        "workspace": ws.as_metadata(),
    }
    yield SimpleNamespace(
        ws=ws, repo=repo, granted=granted, key=key, ledger=ledger, metadata=metadata
    )
    cleanup_workspace(ws.repo, ws.path, ws.home)


def _listing(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")} if root.exists() else set()


def _snapshots(granted: Path) -> list[Path]:
    return [p for p in granted.iterdir() if p.name.startswith(tree.SNAPSHOT_ID_PREFIX)]


def _beneath(path: str | Path, root: Path) -> bool:
    resolved = Path(path).resolve(strict=False)
    base = root.resolve()
    return resolved == base or base in resolved.parents


def _git_call(argv: list[str], cwd: object) -> tuple[str, Path | None]:
    """The Git subcommand and the directory it acts on (``-C`` wins over cwd)."""

    where = Path(os.fsdecode(cwd)) if cwd is not None else None
    position = 1
    while position < len(argv) and argv[position].startswith("-"):
        if argv[position] in {"-c", "-C"} and position + 1 < len(argv):
            if argv[position] == "-C":
                where = Path(argv[position + 1])
            position += 2
            continue
        position += 1
    return (argv[position] if position < len(argv) else ""), where


def _fake_validations(seen: list[dict]):
    def run_validations(workspace, commands, **_route_kwargs):
        root = Path(workspace.path)
        seen.append({
            "path": root,
            "feature": (root / "pkg" / "feature.py").read_text(encoding="utf-8"),
            "other": (root / "pkg" / "other.py").read_text(encoding="utf-8"),
            "new_canon": (root / "pkg" / "new_canon.py").is_file(),
            "docs": (root / "docs" / "readme.txt").read_text(encoding="utf-8"),
        })
        return [{"command": c, "returncode": 0, "timed_out": False} for c in commands]

    return run_validations


def _worker_runner(env, seen: list[dict]):
    return reuse_mod.worker_full_snapshot_runner(
        writable_root=env.granted,
        run_validations=_fake_validations(seen),
        route_resolver=_route,
        baseline_comparer=lambda *a, **k: [],
    )


def test_snapshot_is_head_plus_canonical_delta_plus_candidate(env) -> None:
    snapshot, combined = tree.create_worker_snapshot(
        env.ws, env.metadata, ["pkg/feature.py"], writable_root=env.granted
    )
    try:
        root = snapshot.path
        assert _beneath(root, env.granted)
        assert (root / "docs" / "readme.txt").read_text(encoding="utf-8") == "head docs\n"
        assert (root / "keep.log").is_file()  # tracked-but-ignored HEAD content
        assert (root / "pkg" / "other.py").read_text(encoding="utf-8") == "OTHER = 2\n"
        assert (root / "pkg" / "new_canon.py").read_text(encoding="utf-8") == "NEW = 1\n"
        assert (root / "pkg" / "feature.py").read_text(encoding="utf-8") == "VALUE = 2\n"
        assert combined["schema_id"] == "aiworkhub.combined_tree.v1"
        assert combined["canonical_delta_paths"] == ["pkg/new_canon.py", "pkg/other.py"]
        assert combined["observed_candidate_paths"] == ["pkg/feature.py"]
        assert combined["candidate_paths"] == ["pkg/feature.py"]
        assert snapshot.repo == env.ws.repo
        assert snapshot.base_oid and len(snapshot.base_oid) in {40, 64}
        assert set(snapshot.allowed_writes) == {
            "pkg/feature.py", "pkg/new_canon.py", "pkg/other.py"
        }
    finally:
        assert tree.cleanup_worker_snapshot(snapshot.repo, snapshot.path, snapshot.home) is None
    assert not _snapshots(env.granted)


def test_builder_writes_only_beneath_its_root_and_never_runs_git_worktree(env) -> None:
    git_dir_before = _listing(env.repo / ".git")
    runtime_before = _listing(env.repo / ".aiworkhub" / "runtime")
    _AUDIT["writes"].clear()
    _AUDIT["spawns"].clear()
    _AUDIT["active"].append(True)
    try:
        snapshot, _combined = tree.create_worker_snapshot(
            env.ws, env.metadata, ["pkg/feature.py"], writable_root=env.granted
        )
        tree.cleanup_worker_snapshot(snapshot.repo, snapshot.path, snapshot.home)
    finally:
        _AUDIT["active"].clear()
    devnull = os.path.normcase(os.path.abspath(os.devnull))  # subprocess.DEVNULL, not a file
    writes = [
        w for w in _AUDIT["writes"]
        if "__pycache__" not in Path(w).parts and os.path.normcase(w) != devnull
    ]
    spawns = list(_AUDIT["spawns"])
    assert writes, "the audit hook recorded no write at all"
    assert [w for w in writes if not _beneath(w, env.granted)] == []
    assert spawns, "the audit hook recorded no subprocess at all"
    for argv, cwd in spawns:
        assert "worktree" not in argv, argv
        assert Path(argv[0]).stem.lower() == "git", argv
        subcommand, where = _git_call(argv, cwd)
        assert where is not None, argv
        if not _beneath(where, env.granted):
            assert subcommand in _READ_ONLY_GIT, (argv, cwd)
    assert _listing(env.repo / ".git") == git_dir_before
    # No new administrative record: the source workspace's own one predates the build.
    assert {p for p in _listing(env.repo / ".git") if p.startswith("worktrees/")} == {
        p for p in git_dir_before if p.startswith("worktrees/")
    }
    assert _listing(env.repo / ".aiworkhub" / "runtime") == runtime_before
    assert not _snapshots(env.granted)


def test_worker_lane_validations_run_on_the_snapshot_and_return_full_snapshot_rows(env) -> None:
    seen: list[dict] = []
    out = reuse_mod.run_worker_full_snapshot(
        metadata=env.metadata, commands=[_COMMAND], candidate_digest="d" * 64,
        key_path=env.key, ledger_path=env.ledger,
        run_full_snapshot=_worker_runner(env, seen),
    )
    assert out["ok"] is True, out
    assert out["reuse_record_written"] is True and out["tree_identity_sha256"]
    assert [row["lane"] for row in out["results"]] == [reuse_mod.FULL_SNAPSHOT_LANE]
    assert out["results"][0]["failure_class"] == "passed"
    assert len(seen) == 1
    assert _beneath(seen[0]["path"], env.granted)
    assert seen[0]["feature"] == "VALUE = 2\n" and seen[0]["other"] == "OTHER = 2\n"
    assert seen[0]["new_canon"] is True and seen[0]["docs"] == "head docs\n"
    assert not _snapshots(env.granted)
    # The sealed record is the one finalization reuses, exactly as on the host lane.
    reuse = reuse_mod.load_verified_reuse(env.metadata)
    assert reuse is not None
    assert reuse["full_validation_snapshot"]["combined_tree"]["observed_candidate_paths"] == [
        "pkg/feature.py"
    ]


def test_permission_error_names_step_errno_and_repo_relative_path(env, monkeypatch) -> None:
    def denied(source_root, _target_root, relative):
        raise PermissionError(
            errno.EACCES, "Permission denied", str(Path(source_root) / relative)
        )

    monkeypatch.setattr(tree, "_overlay_regular_path", denied)
    out = reuse_mod.run_worker_full_snapshot(
        metadata=env.metadata, commands=[_COMMAND], candidate_digest="d" * 64,
        key_path=env.key, ledger_path=env.ledger,
        run_full_snapshot=_worker_runner(env, []),
    )
    assert out == {
        "ok": False,
        "reason": (
            f"full_snapshot_failed:canonical_overlay:PermissionError:{errno.EACCES}:"
            "pkg/new_canon.py"
        ),
    }
    assert not _snapshots(env.granted)  # the failed build cleaned up its root


def test_failure_reason_shape_is_bounded_and_carries_no_text(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / ("x" * 400)
    reason = tree.failure_reason(
        tree.SnapshotStepError("create_root", PermissionError(13, "secret text", str(outside))),
        repo=tmp_path / "repo",
    )
    step, cls, code, path = reason.split(":", 1)[1].split(":", 3)
    assert (step, cls, code) == ("create_root", "PermissionError", "13")
    assert len(path) == tree.MAX_REASON_PATH_CHARS and "secret" not in reason
    assert tree.failure_reason(RuntimeError("/host/secret"), repo=None) == (
        "full_snapshot_failed:run:RuntimeError:-:-"
    )
    wrapped = tree.SnapshotStepError("observe", WorkspaceError("/host/secret"))
    assert tree.failure_reason(wrapped, repo=None) == (
        "full_snapshot_failed:observe:WorkspaceError:-:-"
    )


def test_cleanup_refuses_a_tree_it_did_not_create(tmp_path: Path) -> None:
    foreign = tmp_path / "wsnap_foreign"
    (foreign / "worktree").mkdir(parents=True)
    (foreign / "home").mkdir()
    with pytest.raises(WorkspaceError):
        tree.cleanup_worker_snapshot(tmp_path, foreign / "worktree", foreign / "home")
    assert foreign.is_dir()


def test_host_lane_keeps_the_git_worktree_builder() -> None:
    host = inspect.getsource(process_launcher._run_full_snapshot_validations)
    assert "create_snapshot=create_combined_validation_workspace" in host
    assert "cleanup_workspace=cleanup_workspace" in host
    # The worker lane's default runner is the worker builder, not the host one.
    worker = inspect.getsource(reuse_mod.worker_full_snapshot_runner)
    assert "worker_snapshot_tree.create_worker_snapshot" in worker
    assert "create_combined_validation_workspace" not in worker
    assert "worker_full_snapshot_runner()" in inspect.getsource(
        reuse_mod._default_full_snapshot_runner
    )
