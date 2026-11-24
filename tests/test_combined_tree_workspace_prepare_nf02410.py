"""NF-2026-02410: combined validation trees run the repo ``workspace_prepare`` hook.

Accept revalidation, neighbour tests, the accept gate and finding reproduction
all validate on ``create_combined_validation_workspace``.  The hook that puts
untracked assets into the worker worktree must run there too -- exactly once,
with its receipt in the combined-tree record, its output kept out of the
candidate delta, and a failing hook refusing with the hook's exit code and tail.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import host_validation_stage as hvs
from aiworkhub import worker_workspace as ww
from aiworkhub import workspace_prepare

CARD = {
    "allowed_writes": ["shared.txt", "feature.txt"],
    "read_first": [],
    "immutable_inputs": [],
    "required_outputs": [],
}
CANDIDATE = ["feature.txt", "shared.txt"]
HOOK_SCRIPT = """\
import pathlib, sys
root, counter, mode = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
with counter.open("a", encoding="utf-8") as handle:
    handle.write(str(root) + "\\n")
if mode == "fail":
    print("boom: model download refused")
    sys.exit(3)
if mode == "touch":
    (root / "shared.txt").write_text("hooked\\n", encoding="utf-8")
(root / "models").mkdir(exist_ok=True)
(root / "models" / "weights.bin").write_bytes(b"asset")
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def _repo(tmp_path: Path, monkeypatch, mode: str | None) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "AIWorkHub Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    (repo / "shared.txt").write_text("old\n", encoding="utf-8")
    counter = tmp_path / "hook_runs.txt"
    if mode is not None:
        script = tmp_path / "hook" / "prepare.py"
        script.parent.mkdir()
        script.write_text(HOOK_SCRIPT, encoding="utf-8")
        config = repo.joinpath(*workspace_prepare.CONFIG_RELATIVE_PATH)
        config.parent.mkdir(parents=True)
        argv = [sys.executable, str(script), "{worktree}", str(counter), mode]
        config.write_text(json.dumps({"workspace_prepare": argv}), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    return repo, counter


def _candidate(repo: Path) -> ww.WorkerWorkspace:
    candidate = ww.create_workspace(repo, "candidate_request", CARD, "validation")
    (candidate.path / "shared.txt").write_text("candidate\n", encoding="utf-8")
    (candidate.path / "feature.txt").write_text("new\n", encoding="utf-8")
    return candidate


def _runs(counter: Path) -> list[str]:
    return counter.read_text(encoding="utf-8").splitlines() if counter.exists() else []


def _live_union_trees(tmp_path: Path) -> list[Path]:
    root = tmp_path / "worktrees"
    return [p for p in root.rglob("union2_*") if (p / "worktree").exists()]


def test_combined_tree_runs_the_hook_once_and_keeps_its_output_out_of_the_delta(
    tmp_path, monkeypatch
):
    repo, counter = _repo(tmp_path, monkeypatch, "ok")
    candidate = _candidate(repo)
    try:
        combined, record = ww.create_combined_validation_workspace(candidate, CARD, CANDIDATE)
        try:
            receipt = record["workspace_prepare"]
            assert receipt["status"] == "succeeded"
            assert receipt["returncode"] == 0
            assert receipt["baseline_paths"] == ["models/weights.bin"]
            assert _runs(counter) == [str(combined.path)]
            # The asset is present for validation ...
            assert (combined.path / "models" / "weights.bin").read_bytes() == b"asset"
            # ... but later scope checks still see only the candidate delta.
            assert ww.changed_paths(combined) == CANDIDATE
            assert record["observed_candidate_paths"] == CANDIDATE
            # The receipt lives in a directory owned by (and removed with) the tree.
            state = combined.home / workspace_prepare.COMBINED_STATE_DIRNAME
            assert workspace_prepare.receipt_path(state, combined.path).is_file()
        finally:
            ww.cleanup_workspace(combined.repo, combined.path, combined.home)
        assert not state.exists()
    finally:
        ww.cleanup_workspace(candidate.repo, candidate.path, candidate.home)


def test_repo_without_hook_builds_the_tree_exactly_as_before(tmp_path, monkeypatch):
    repo, counter = _repo(tmp_path, monkeypatch, None)
    candidate = _candidate(repo)
    try:
        combined, record = ww.create_combined_validation_workspace(candidate, CARD, CANDIDATE)
        try:
            assert record == {
                "schema_id": "aiworkhub.combined_tree.v1",
                "candidate_paths": CANDIDATE,
                "canonical_delta_paths": [],
                "observed_candidate_paths": CANDIDATE,
                "candidate_paths_already_in_canonical": [],
                "merged_paths": {},
                "workspace_prepare": {
                    "schema_id": workspace_prepare.RECEIPT_SCHEMA_ID,
                    "status": "not_configured",
                },
            }
            assert ww.changed_paths(combined) == CANDIDATE
            assert not (combined.home / workspace_prepare.COMBINED_STATE_DIRNAME).exists()
            assert _runs(counter) == []
        finally:
            ww.cleanup_workspace(combined.repo, combined.path, combined.home)
    finally:
        ww.cleanup_workspace(candidate.repo, candidate.path, candidate.home)


def test_failing_hook_removes_the_tree_and_refuses_with_exit_code_and_tail(
    tmp_path, monkeypatch
):
    repo, counter = _repo(tmp_path, monkeypatch, "fail")
    candidate = _candidate(repo)
    try:
        with pytest.raises(ww.WorkspaceError) as caught:
            ww.create_combined_validation_workspace(candidate, CARD, CANDIDATE)
        message = str(caught.value)
        assert message.startswith("combined_tree_workspace_prepare_failed:exit=3:")
        assert "boom: model download refused" in message
        assert len(_runs(counter)) == 1
        assert _live_union_trees(tmp_path) == []
    finally:
        ww.cleanup_workspace(candidate.repo, candidate.path, candidate.home)


def test_hook_that_rewrites_a_candidate_path_is_refused(tmp_path, monkeypatch):
    repo, _counter = _repo(tmp_path, monkeypatch, "touch")
    candidate = _candidate(repo)
    try:
        with pytest.raises(ww.WorkspaceError) as caught:
            ww.create_combined_validation_workspace(candidate, CARD, CANDIDATE)
        assert str(caught.value) == "combined_tree_workspace_prepare_touched_candidate:shared.txt"
        assert _live_union_trees(tmp_path) == []
    finally:
        ww.cleanup_workspace(candidate.repo, candidate.path, candidate.home)


def test_host_stage_candidate_tree_runs_the_hook_once_not_twice(tmp_path, monkeypatch):
    repo, counter = _repo(tmp_path, monkeypatch, "ok")
    candidate = _candidate(repo)
    calls: list[Path] = []
    real = workspace_prepare.prepare_once

    def counting(*args, **kwargs):
        calls.append(Path(args[1]))
        return real(*args, **kwargs)

    monkeypatch.setattr(workspace_prepare, "prepare_once", counting)
    monkeypatch.setattr(hvs, "host_runner", lambda _tree, _root: (lambda *_a: {}))
    try:
        ops = hvs.workspace_tree_ops(
            candidate, CARD, CANDIDATE, state_dir=hvs.stage_dir(repo, "req_nf02410")
        )
        handle = ops.build_candidate()
        try:
            assert handle.record["workspace_prepare"]["status"] == "succeeded"
            assert calls == [handle.path]
            assert _runs(counter) == [str(handle.path)]
            assert handle.changed() == CANDIDATE
        finally:
            handle.cleanup()
    finally:
        ww.cleanup_workspace(candidate.repo, candidate.path, candidate.home)


def test_host_stage_candidate_without_recorded_receipt_fails_closed(tmp_path, monkeypatch):
    tree = SimpleNamespace(repo=tmp_path, path=tmp_path / "cand", home=tmp_path / "home")
    cleaned: list[Path] = []
    monkeypatch.setattr(ww, "create_combined_validation_workspace", lambda *_a, **_kw: (tree, {}))
    monkeypatch.setattr(ww, "cleanup_workspace", lambda _r, path, _h: cleaned.append(path))
    monkeypatch.setattr(hvs, "host_runner", lambda _tree, _root: (lambda *_a: {}))

    def forbidden(*_a, **_kw):
        raise AssertionError("the candidate tree must not run the hook a second time")

    monkeypatch.setattr(workspace_prepare, "prepare_once", forbidden)
    ops = hvs.workspace_tree_ops(
        SimpleNamespace(repo=tmp_path, base_oid="b" * 40), {}, [], state_dir=tmp_path / "st"
    )
    unrecorded = "host_stage_candidate_workspace_prepare_unrecorded"
    with pytest.raises(hvs.HostStageError, match=unrecorded):
        ops.build_candidate()
    assert cleaned == [tree.path]
