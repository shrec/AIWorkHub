"""NF-2026-02394: the host stage's per-command scratch lives in the neutral temp area.

Fakes only: no provider and no real host stage runs on the canonical repository.
The scratch is ``<repo>/.aiworkhub/temp/host_stage/<request_id>``, so a test that
sets its own AIWORKHUB_WORKTREE_ROOT under its basetemp is not a foreign root.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import host_validation_stage as hvs
from aiworkhub import task_store
from aiworkhub import worker_workspace as ww
from aiworkhub import workspace_prepare

REQUEST = "req_neutral0123456789"
TASK = "TASK_NF02394"


def _project(repo: Path, repo_id: str) -> None:
    marker = repo / ".aiworkhub"
    marker.mkdir(parents=True)
    (marker / "project.json").write_text(f'{{"repo_id": "{repo_id}"}}', encoding="utf-8")


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "hub.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(task_store.SCHEMA)
    finally:
        conn.close()
    return db


def test_host_runner_receives_scratch_under_neutral_temp_area(tmp_path, monkeypatch):
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    seen: list[Path] = []
    monkeypatch.setattr(hvs, "host_runner", lambda _tree, root: seen.append(root) or (lambda *_a: {}))
    tree = SimpleNamespace(repo=repo, path=tmp_path / "cand", home=tmp_path / "home")
    monkeypatch.setattr(ww, "create_combined_validation_workspace", lambda *_a, **_kw: (tree, {}))
    monkeypatch.setattr(workspace_prepare, "prepare_once", lambda *_a, **_kw: "prepared")

    directory = hvs.stage_dir(repo, REQUEST)
    ops = hvs.workspace_tree_ops(
        SimpleNamespace(repo=repo, base_oid="b" * 40), {}, [], state_dir=directory
    )
    ops.build_candidate()

    expected = repo / ".aiworkhub" / "temp" / "host_stage" / REQUEST
    assert seen == [expected]
    assert expected.is_relative_to(repo / ".aiworkhub" / "temp")
    assert not expected.is_relative_to(repo / ".aiworkhub" / "runtime")


def test_neutral_scratch_is_not_a_foreign_worktree_root(tmp_path):
    repo = tmp_path.resolve() / "repo"
    other = tmp_path.resolve() / "other"
    _project(repo, "repo_owner")
    other.mkdir()

    scratch = hvs.neutral_scratch_root(repo, REQUEST)
    basetemp = scratch / "abc123def456" / "pytest-of-runner"
    assert ww._repository_owned_worktree_root(basetemp, other) == basetemp

    runtime_basetemp = (
        repo / ".aiworkhub" / "runtime" / "host_stage" / REQUEST / "abc123def456" / "pytest-of-runner"
    )
    try:
        resolved = ww._repository_owned_worktree_root(runtime_basetemp, other)
    except ww.WorkspaceError:
        resolved = None
    assert resolved != runtime_basetemp


def test_finished_stage_leaves_no_scratch_behind(tmp_path):
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    cand = tmp_path / "cand"
    cand.mkdir()
    scratch = hvs.neutral_scratch_root(repo, REQUEST)
    request_dir = repo / ".aiworkhub" / "temp" / "host_stage" / REQUEST

    def fake_runner(argv, _cwd):
        leftover = scratch / "home" / "left.txt"
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_text("x", encoding="utf-8")
        return {"argv": list(argv), "returncode": 0, "timed_out": False}

    handle = hvs.TreeHandle(
        path=cand,
        record={},
        changed=lambda: [],
        cleanup=lambda: None,
    )
    ops = hvs.TreeOps(build_candidate=lambda: handle, build_base=lambda: handle)
    spec = hvs.parse_host_spec({"host_commands": [["git", "diff", "--check"]]})
    assert spec is not None

    result = hvs.run_host_stage(
        repo_root=repo,
        request_id=REQUEST,
        task_id=TASK,
        spec=spec,
        tree_ops=ops,
        base_oid="b" * 40,
        candidate_root=tmp_path,
        allowed_writes=("src/mod.py",),
        db_path=_db(tmp_path),
        runner=fake_runner,
        python="python",
        cpu_capacity=8,
        resource_kwargs={"is_alive": lambda _pid: True, "poll_seconds": 0.01},
    )

    assert result["state"] == "passed", result
    assert not scratch.exists()
    assert not request_dir.exists()
    assert not (repo / ".aiworkhub" / "runtime" / "host_stage" / REQUEST / hvs.HOST_STAGE_SCRATCH).exists()


@pytest.mark.parametrize("bad", ["", "../escape", "a/b"])
def test_neutral_scratch_rejects_unsafe_request_ids(tmp_path, bad):
    with pytest.raises(hvs.HostStageError):
        hvs.neutral_scratch_root(tmp_path, bad)
