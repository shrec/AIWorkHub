"""NF-2026-01997: an owner's uncommitted canonical edit is never worker output.

A real Git repository commits a tracked file ``F`` and then edits it in the
canonical working tree without committing -- the shape of the field report
(a shared header, routinely ``.aiworkhub/config/workforce.json``).  A
workspace is provisioned through ``create_workspace`` (live-canonical seeding,
NF-2026-00423) and run through ``enforce_scope``/``changed_paths``,
``promote`` and the accept auto-commit (``accept_commit.commit_promoted``) for:

(i)   F is a read_first input only,
(ii)  F is in the support closure only,
(iii) F is in allowed_writes and the worker leaves it untouched,
(iv)  F is in allowed_writes and the worker edits it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import dirty_canonical_seed  # noqa: E402
from aiworkhub import promotion_conflict_detail as pcd  # noqa: E402
from aiworkhub import worker_workspace  # noqa: E402

F = "include/shared.h"
RESULT = "out/result.txt"
INPUT = "read/input.txt"
COMMITTED = b"#define SHARED 1\n"
OWNER = b"#define SHARED 1\n#define OWNER_UNCOMMITTED 2\n"
TASK_ID = "needfix-NF-2026-01997"


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
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "parent"
    root.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "tests@example.invalid"),
        ("config", "user.name", "Task MCP Tests"),
        ("config", "core.autocrlf", "false"),
        ("config", "commit.gpgsign", "false"),
    ):
        assert _git(root, *args).returncode == 0
    for relative, data in (
        (F, COMMITTED),
        (INPUT, b"input-v1\n"),
        (RESULT, b"result-v1\n"),
    ):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(data)
    assert _git(root, "add", F, INPUT, RESULT).returncode == 0
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    return root


def _dirty_owner_edit(repo: Path) -> None:
    (repo / F).write_bytes(OWNER)
    assert _git(repo, "status", "--porcelain", "--", F).stdout.startswith(" M")


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _paths_committed_since(repo: Path, before: str) -> set[str]:
    log = _git(repo, "log", "--name-only", "--format=", f"{before}..HEAD")
    assert log.returncode == 0, log.stderr
    return {line for line in log.stdout.splitlines() if line}


def _auto_commit(repo: Path, promoted: list[str]) -> None:
    # The exact Git steps ``accept_commit.commit_promoted`` runs for the
    # receipt's ``promoted_paths``: only promoted paths are ever committed.
    for args in (
        ("--literal-pathspecs", "add", "-A", "--", *promoted),
        ("--literal-pathspecs", "commit", "--only", "-qm", "accept", "--", *promoted),
    ):
        step = _git(repo, *args)
        assert step.returncode == 0, step.stderr


def _support_only(monkeypatch: pytest.MonkeyPatch) -> None:
    real = worker_workspace._declared_workspace_seed_closure

    def closure(source_root: Path, card: Any, allowed: Any) -> Any:
        live, support, seeded = real(source_root, card, allowed)
        assert F not in live
        support = tuple(sorted(set(support) | {F}))
        return live, support, sorted(set(seeded) | {F})

    monkeypatch.setattr(worker_workspace, "_declared_workspace_seed_closure", closure)


_CASES = {
    "i_read_first_only": {"allowed_writes": [RESULT], "read_first": [F, INPUT]},
    "ii_support_closure_only": {"allowed_writes": [RESULT], "read_first": [INPUT]},
    "iii_allowed_untouched": {"allowed_writes": [RESULT, F], "read_first": [INPUT]},
}


def test_dirty_seed_is_visible_to_raw_git_diff_against_the_base(repo: Path) -> None:
    """Field-report mechanism: the live seed differs from the base commit."""
    _dirty_owner_edit(repo)
    card = {"allowed_writes": [F], "read_first": [INPUT]}
    workspace = worker_workspace.create_workspace(repo, "req-nf01997-raw", card, "validation")
    try:
        assert (workspace.path / F).read_bytes() == OWNER  # NF-00423 live seed kept
        raw = _git(workspace.path, "diff", "--name-only", workspace.base_oid or "HEAD")
        assert F in raw.stdout.splitlines()
        assert workspace.dirty_at_seed == (F,)
        # Measured against the bytes actually seeded, nothing changed yet.
        assert worker_workspace.changed_paths(workspace) == []
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.parametrize("case", sorted(_CASES))
def test_dirty_canonical_file_is_never_changed_promoted_or_committed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _dirty_owner_edit(repo)
    if case == "ii_support_closure_only":
        _support_only(monkeypatch)
    card = _CASES[case]
    request_id = f"req-nf01997-{case.replace('_', '-')}"
    workspace = worker_workspace.create_workspace(repo, request_id, card, "validation")
    try:
        assert (workspace.path / F).read_bytes() == OWNER
        assert F in workspace.dirty_at_seed
        (workspace.path / RESULT).write_bytes(b"result-v2\n")
        changed = worker_workspace.enforce_scope(workspace)
        assert changed == [RESULT]
        before = _head(repo)
        promoted = worker_workspace.promote(workspace, changed)
        assert promoted == [RESULT]
        _auto_commit(repo, promoted)
        assert _paths_committed_since(repo, before) == {RESULT}
        # The owner's uncommitted bytes are byte-identical and still uncommitted.
        assert (repo / F).read_bytes() == OWNER
        assert _git(repo, "status", "--porcelain", "--", F).stdout.startswith(" M")
        assert (repo / RESULT).read_bytes() == b"result-v2\n"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_worker_edit_of_a_dirty_at_seed_path_is_a_promotion_conflict(repo: Path) -> None:
    _dirty_owner_edit(repo)
    card = {"allowed_writes": [F], "read_first": [INPUT]}
    workspace = worker_workspace.create_workspace(repo, "req-nf01997-iv", card, "validation")
    try:
        (workspace.path / F).write_bytes(OWNER + b"#define WORKER 3\n")
        changed = worker_workspace.enforce_scope(workspace)
        assert changed == [F]
        before = _head(repo)
        with pytest.raises(worker_workspace.WorkspaceError) as raised:
            worker_workspace.promote(workspace, changed)
        error = str(raised.value)
        assert error.startswith(f"{dirty_canonical_seed.DIRTY_AT_SEED_CODE}:{F}:")
        assert "dirty_at_seed" in error
        reply = pcd.promotion_conflict_reply(error)
        assert reply is not None and reply["error_class"] == "promotion_conflict"
        assert [entry["path"] for entry in reply["promotion_conflicts"]] == [F]
        assert error.startswith(pcd.PROMOTION_CONFLICT_PREFIXES)
        # Nothing was written to canonical and nothing was committed.
        assert (repo / F).read_bytes() == OWNER
        assert _head(repo) == before
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_clean_canonical_path_promotes_exactly_as_before(repo: Path) -> None:
    card = {"allowed_writes": [F], "read_first": [INPUT]}
    workspace = worker_workspace.create_workspace(repo, "req-nf01997-clean", card, "validation")
    try:
        assert workspace.dirty_at_seed == ()
        (workspace.path / F).write_bytes(COMMITTED + b"#define WORKER 3\n")
        changed = worker_workspace.enforce_scope(workspace)
        assert changed == [F]
        assert worker_workspace.promote(workspace, changed) == [F]
        assert (repo / F).read_bytes() == COMMITTED + b"#define WORKER 3\n"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_dirty_at_seed_set_is_recorded_once_in_workspace_metadata(repo: Path) -> None:
    _dirty_owner_edit(repo)
    card = {"allowed_writes": [RESULT], "read_first": [F, INPUT]}
    workspace = worker_workspace.create_workspace(repo, "req-nf01997-meta", card, "validation")
    try:
        metadata = workspace.as_metadata()
        assert metadata["dirty_at_seed"] == [F]
        loaded = worker_workspace.WorkerWorkspace.from_metadata(metadata)
        assert loaded.dirty_at_seed == (F,)
        legacy = {key: value for key, value in metadata.items() if key != "dirty_at_seed"}
        assert worker_workspace.WorkerWorkspace.from_metadata(legacy).dirty_at_seed == ()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_failed_dirty_probe_refuses_rather_than_reading_clean(tmp_path: Path) -> None:
    class _Failed:
        returncode = 128
        stdout = b""
        stderr = b"fatal: probe"

    with pytest.raises(worker_workspace.WorkspaceError, match="dirty_canonical_seed_probe_failed"):
        dirty_canonical_seed.dirty_seeded_paths(tmp_path, [F], lambda argv, **kw: _Failed())


def test_dirty_probe_chunks_long_path_lists(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class _Clean:
        returncode = 0
        stdout = b" M " + F.encode() + b"\0"
        stderr = b""

    def run(argv: list[str], **kwargs: Any) -> _Clean:
        calls.append(argv)
        return _Clean()

    paths = [f"dir/{index:05d}/file.txt" for index in range(1200)] + [F]
    assert dirty_canonical_seed.dirty_seeded_paths(tmp_path, paths, run) == (F,)
    assert len(calls) > 1
    assert all(sum(len(arg) + 1 for arg in argv) < 9000 for argv in calls)
