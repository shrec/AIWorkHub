"""NF-2026-01775: the Landlock short TMPDIR is minted once per validation batch.

``_landlock_exec`` used to ``mkdtemp`` a fresh short dir inside EVERY exec'd
validation child (and remove it on failure), so a later command of the same
``run_validations`` batch saw a different TMPDIR than the command that built a
tree there. The trusted parent now mints the dir once, every Landlock child of
the batch adopts that exact dir, and the batch's ``finally`` removes it once.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_workspace  # noqa: E402


def _workspace(tmp_path: Path) -> worker_workspace.WorkerWorkspace:
    root = tmp_path / "ws"
    workspace = worker_workspace.WorkerWorkspace(
        request_id="nf01775-batch",
        repo=root / "repo",
        path=root / "worktree",
        home=root / "home",
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    for path in (workspace.repo, workspace.path, workspace.home):
        path.mkdir(parents=True)
    return workspace


def _long_scratch(tmp_path: Path) -> Path:
    scratch = tmp_path / ("s" * 40)
    scratch.mkdir()
    return scratch


def _short_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    monkeypatch.setenv(worker_workspace.VALIDATION_SHORT_TMP_ROOT_ENV, str(root))
    return root


def _set_budget(monkeypatch: pytest.MonkeyPatch, scratch: Path, *, too_long: bool) -> None:
    # ``scratch`` + the 40-byte listener suffix exceeds the budget by one byte
    # when ``too_long``, and fits exactly otherwise; the short root always fits.
    budget = len(str(scratch)) + (39 if too_long else 40)
    monkeypatch.setattr(worker_workspace, "_LANDLOCK_SUN_PATH_BUDGET", budget)


def _patch_run_validations(
    monkeypatch: pytest.MonkeyPatch, scratch: Path
) -> tuple[list[Path | None], list[dict[str, str]]]:
    argv_short_tmps: list[Path | None] = []
    seen_envs: list[dict[str, str]] = []
    monkeypatch.setattr(
        worker_workspace, "provision_validation_exec_scratch", lambda _ws, **_: scratch
    )
    monkeypatch.setattr(worker_workspace, "cleanup_validation_exec_scratch", lambda _p: None)
    monkeypatch.setattr(worker_workspace, "sanitized_env", lambda *_a, **_k: {})
    monkeypatch.setattr(
        worker_workspace,
        "python_candidate_authority",
        lambda _ws: {"digest": "", "sources": []},
    )
    monkeypatch.setattr(
        worker_workspace,
        "_normalize_validation_interpreter_argv",
        lambda _ws, tokens, **_k: (tokens, None),
    )
    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_authority",
        lambda tokens, _repo: (tokens, (), None),
    )
    monkeypatch.setattr(
        worker_workspace, "_validation_needs_semlock_dev_shm", lambda *_a: False
    )

    def _sandbox_argv(_ws: object, _adapter: str, argv: list[str], **kwargs: object) -> list[str]:
        short_tmp = kwargs.get("validation_short_tmp")
        assert short_tmp is None or isinstance(short_tmp, Path)
        argv_short_tmps.append(short_tmp)
        return list(argv)

    monkeypatch.setattr(worker_workspace, "sandbox_argv", _sandbox_argv)

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        seen_envs.append({name: env[name] for name in ("TMPDIR", "TMP", "TEMP")})
        tree = Path(env["TMPDIR"]) / "nested-git" / "src"
        if argv[0] == "build-tree":
            tree.mkdir(parents=True)
            (tree / "marker").write_text("built\n", encoding="utf-8")
        else:
            marker = tree / "marker"
            if not marker.exists():
                return subprocess.CompletedProcess(argv, 1, "", "tree missing\n")
            # The command after the builder must still see the builder's tree.
            assert marker.read_text(encoding="utf-8") == "built\n"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worker_workspace.subprocess, "run", _run)
    return argv_short_tmps, seen_envs


def test_two_commands_of_one_batch_share_one_short_tmpdir_removed_after_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    scratch = _long_scratch(tmp_path)
    root = _short_root(tmp_path, monkeypatch)
    _set_budget(monkeypatch, scratch, too_long=True)
    argv_short_tmps, seen_envs = _patch_run_validations(monkeypatch, scratch)

    rows = worker_workspace.run_validations(
        workspace, ["build-tree --go", "read-tree --go"], backend="landlock"
    )

    assert [row["returncode"] for row in rows] == [0, 0]
    assert len(argv_short_tmps) == 2
    minted = argv_short_tmps[0]
    assert minted is not None and argv_short_tmps[1] == minted
    assert minted.parent == root.resolve()
    assert seen_envs == [{"TMPDIR": str(minted), "TMP": str(minted), "TEMP": str(minted)}] * 2
    # Removed exactly once when the batch ends; nothing else was minted.
    assert not minted.exists()
    assert list(root.iterdir()) == []


def test_batch_short_tmpdir_is_removed_when_a_command_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    scratch = _long_scratch(tmp_path)
    root = _short_root(tmp_path, monkeypatch)
    _set_budget(monkeypatch, scratch, too_long=True)
    argv_short_tmps, _seen = _patch_run_validations(monkeypatch, scratch)

    with pytest.raises(worker_workspace.ValidationRunError, match="validation_failed:read-tree"):
        # ``read-tree`` first: its tree was never built, so the fake child fails.
        worker_workspace.run_validations(workspace, ["read-tree --go"], backend="landlock")

    assert argv_short_tmps and argv_short_tmps[0] is not None
    assert not argv_short_tmps[0].exists()
    assert list(root.iterdir()) == []


def test_short_batch_tmpdir_mints_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    scratch = _long_scratch(tmp_path)
    root = _short_root(tmp_path, monkeypatch)
    _set_budget(monkeypatch, scratch, too_long=False)
    argv_short_tmps, seen_envs = _patch_run_validations(monkeypatch, scratch)

    worker_workspace.run_validations(
        workspace, ["build-tree --go", "read-tree --go"], backend="landlock"
    )

    assert argv_short_tmps == [None, None]
    assert seen_envs == [{"TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch)}] * 2
    assert list(root.iterdir()) == []


def test_mint_is_landlock_only_and_scoped_to_the_exec_scratch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scratch = _long_scratch(tmp_path)
    root = _short_root(tmp_path, monkeypatch)
    _set_budget(monkeypatch, scratch, too_long=True)
    outside = tmp_path / ("o" * 40)
    outside.mkdir()

    assert worker_workspace._mint_batch_short_tmpdir("bubblewrap", str(scratch), scratch) is None
    assert worker_workspace._mint_batch_short_tmpdir("landlock", "", scratch) is None
    assert worker_workspace._mint_batch_short_tmpdir("landlock", str(outside), scratch) is None
    assert list(root.iterdir()) == []

    minted = worker_workspace._mint_batch_short_tmpdir("landlock", str(scratch), scratch)
    assert minted is not None
    assert minted.parent == root.resolve() and minted.is_dir()
    if os.name == "posix":
        assert minted.stat().st_mode & 0o777 == 0o700


def test_sandbox_argv_passes_the_batch_dir_to_the_landlock_child(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    scratch = _long_scratch(tmp_path)
    short_tmp = tmp_path / "r"
    short_tmp.mkdir()

    argv = worker_workspace.sandbox_argv(
        workspace,
        "validation",
        ["tool"],
        backend="landlock",
        validation_exec_scratch=scratch,
        validation_short_tmp=short_tmp,
    )
    flag = argv.index("--short-tmp")
    assert argv[flag + 1] == str(short_tmp)
    assert flag < argv.index("--")

    plain = worker_workspace.sandbox_argv(
        workspace, "validation", ["tool"], backend="landlock", validation_exec_scratch=scratch
    )
    assert "--short-tmp" not in plain
    with pytest.raises(worker_workspace.WorkspaceError, match="landlock_short_tmp_without"):
        worker_workspace.sandbox_argv(
            workspace, "validation", ["tool"], backend="landlock", validation_short_tmp=short_tmp
        )


def test_landlock_child_adopts_the_batch_dir_without_creating_or_deleting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    scratch = _long_scratch(tmp_path)
    root = _short_root(tmp_path, monkeypatch)
    batch_dir = worker_workspace._landlock_short_tmpdir(
        str(scratch), [str(root)], sun_path_budget=len(str(scratch)) + 39
    )
    assert batch_dir is not None
    short_tmp = Path(batch_dir)
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(scratch))
    granted: list[Path | None] = []

    class _Stop(Exception):
        pass

    def _apply_landlock(*_args: object, short_tmp: Path | None = None, **_kwargs: object) -> None:
        granted.append(short_tmp)
        raise _Stop

    monkeypatch.setattr(worker_workspace, "_apply_landlock", _apply_landlock)
    monkeypatch.setattr(
        worker_workspace.tempfile,
        "mkdtemp",
        lambda *_a, **_k: pytest.fail("a Landlock child must never mint a short TMPDIR"),
    )

    with pytest.raises(_Stop):
        worker_workspace._landlock_exec(
            [
                "--landlock-exec",
                "--workspace", str(workspace.path),
                "--home", str(workspace.home),
                "--exec-scratch", str(scratch),
                "--short-tmp", str(short_tmp),
                "--",
                "tool",
            ]
        )

    assert granted == [short_tmp]
    for name in ("TMPDIR", "TMP", "TEMP", worker_workspace.VALIDATION_SHORT_TMP_ROOT_ENV):
        assert os.environ[name] == str(short_tmp)
    # The failing child left the batch's dir in place for the parent to remove.
    assert short_tmp.is_dir()
    assert list(root.iterdir()) == [short_tmp]


def test_landlock_child_refuses_a_short_tmp_without_exec_scratch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with pytest.raises(worker_workspace.WorkspaceError, match="landlock_short_tmp_without"):
        worker_workspace._landlock_exec(
            [
                "--landlock-exec",
                "--workspace", str(workspace.path),
                "--home", str(workspace.home),
                "--short-tmp", str(tmp_path),
                "--",
                "tool",
            ]
        )


def test_adopt_refuses_a_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(worker_workspace.WorkspaceError, match="landlock_short_tmp_not_directory"):
        worker_workspace._adopt_batch_short_tmpdir(str(tmp_path / "missing"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_adopt_refuses_a_non_private_dir(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    if shared.stat().st_mode & 0o077 == 0:
        pytest.skip("umask produced a private dir; no group/other bits to refuse")
    with pytest.raises(worker_workspace.WorkspaceError, match="landlock_short_tmp_not_private"):
        worker_workspace._adopt_batch_short_tmpdir(str(shared))
