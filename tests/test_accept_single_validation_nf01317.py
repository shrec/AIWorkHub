"""NF-2026-01317: accept runs the declared validation at most once."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import process_launcher
from aiworkhub import process_launcher_accept_review as accept_review
from aiworkhub import process_launcher_validation as validation
from aiworkhub.worker_workspace import (
    cleanup_workspace,
    create_combined_validation_workspace,
    create_workspace,
)

_COMMAND = "python -c \"print('probe')\""
_CARD = {
    "allowed_writes": ["pkg/feature.py"],
    "read_first": [],
    "immutable_inputs": [],
    "required_outputs": [],
    "validation": [_COMMAND],
}
_CANDIDATE = ["pkg/feature.py"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "AIWorkHub Test")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    ws = create_workspace(repo, "single-request", _CARD, "validation")
    (ws.path / "pkg" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    yield ws
    cleanup_workspace(ws.repo, ws.path, ws.home)


def _route(metadata):
    return {"backend": metadata.get("backend")}


class _Counting:
    """Counts every declared command the runner is asked to execute."""

    def __init__(self) -> None:
        self.commands = 0
        self.cleaned = 0
        self.created = 0

    def run_validations(self, _workspace, commands, **_route):
        commands = list(commands)
        self.commands += len(commands)
        return [{"command": c, "returncode": 0, "timed_out": False} for c in commands]

    def run_declared(self):
        return self.run_validations(None, _CARD["validation"])

    def create_snapshot(self, ws, authority, candidate):
        self.created += 1
        return create_combined_validation_workspace(ws, authority, candidate)

    def cleanup(self, *args):
        self.cleaned += 1
        cleanup_workspace(*args)


def _identity(ws):
    return validation.combined_tree_identity(
        ws, _CARD, {}, _CANDIDATE, route_resolver=_route
    )


def _finalizer_record(ws, identity):
    return {
        "full_validation_snapshot": {
            "schema_id": "aiworkhub.full_validation_snapshot.v1",
            "request_id": ws.request_id,
            "repo": str(ws.repo),
            "source_base_oid": ws.base_oid,
            "snapshot_base_oid": ws.base_oid,
            "combined_tree": {"merged_paths": {}, "candidate_paths": _CANDIDATE},
            "tree_identity_sha256": identity,
            "validations_all_passed": True,
        },
        "validations": [{"command": _COMMAND, "returncode": 0, "timed_out": False}],
    }


def _union(ws, counting, reuse):
    return validation.run_union_validations_once(
        ws, _CARD, {}, _CANDIDATE,
        combined_tree={"merged_paths": {}},
        snapshot_base_oid=ws.base_oid,
        run_declared=counting.run_declared,
        route_resolver=_route,
        reuse=reuse,
    )


def _full(ws, counting, reuse):
    return validation.run_full_snapshot_validations(
        ws, _CARD, {}, _CANDIDATE,
        create_snapshot=counting.create_snapshot,
        cleanup_workspace=counting.cleanup,
        run_validations=counting.run_validations,
        route_resolver=_route,
        baseline_comparer=lambda *a, **k: [],
        reuse=reuse,
    )


def test_matching_finalizer_receipt_runs_zero_commands_on_both_paths(workspace) -> None:
    counting = _Counting()
    record = _finalizer_record(workspace, _identity(workspace))
    rows, handed_on = _union(workspace, counting, record)
    assert counting.commands == 0
    assert rows == record["validations"]
    rows_b, snapshot = _full(workspace, counting, handed_on)
    assert counting.commands == 0 and counting.created == 0
    assert rows_b == record["validations"]
    assert snapshot["reused"] is True and snapshot["reused_from"] == "finalization"


def test_no_finalizer_receipt_runs_commands_exactly_once_across_both_paths(
    workspace,
) -> None:
    counting = _Counting()
    rows, handed_on = _union(workspace, counting, None)
    assert counting.commands == 1
    assert handed_on is not None
    rows_b, snapshot = _full(workspace, counting, handed_on)
    assert counting.commands == 1 and counting.created == 0
    assert rows_b == rows
    assert snapshot["reused"] is True
    assert snapshot["reused_from"] == "accept_union"
    assert snapshot["tree_identity_sha256"] == _identity(workspace)


def test_candidate_drift_between_paths_reruns(workspace) -> None:
    counting = _Counting()
    _rows, handed_on = _union(workspace, counting, None)
    (workspace.path / "pkg" / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
    _full(workspace, counting, handed_on)
    assert counting.commands == 2 and counting.created == 1
    assert counting.cleaned == 1


def test_canonical_drift_against_finalizer_receipt_reruns(workspace) -> None:
    counting = _Counting()
    record = _finalizer_record(workspace, _identity(workspace))
    _git(workspace.repo, "commit", "--allow-empty", "-m", "moved head")
    _union(workspace, counting, record)
    assert counting.commands == 1


def test_manager_correction_never_reuses_the_finalizer_receipt(workspace) -> None:
    terminal = {
        "evidence": {
            "validation": [{"command": _COMMAND, "returncode": 0}],
            "quality_gate": {
                "full_validation_snapshot": {
                    "tree_identity_sha256": _identity(workspace)
                }
            },
        }
    }
    assert accept_review._finalizer_validation_reuse(terminal, correction_record=None)
    corrected = accept_review._finalizer_validation_reuse(
        terminal, correction_record={"paths": ["pkg/feature.py"]}
    )
    assert corrected is None
    counting = _Counting()
    _union(workspace, counting, corrected)
    assert counting.commands == 1


def test_failed_union_run_hands_nothing_on(workspace) -> None:
    def boom():
        raise validation.WorkspaceError("validation_failed:red")

    with pytest.raises(validation.WorkspaceError):
        validation.run_union_validations_once(
            workspace, _CARD, {}, _CANDIDATE,
            combined_tree={}, snapshot_base_oid=workspace.base_oid,
            run_declared=boom, route_resolver=_route, reuse=None,
        )


def test_unavailable_identity_hands_nothing_on_and_still_runs(workspace) -> None:
    def broken(_metadata):
        raise RuntimeError("route unavailable")

    counting = _Counting()
    rows, handed_on = validation.run_union_validations_once(
        workspace, _CARD, {}, _CANDIDATE,
        combined_tree={}, snapshot_base_oid=workspace.base_oid,
        run_declared=counting.run_declared, route_resolver=broken,
        reuse=_finalizer_record(workspace, _identity(workspace)),
    )
    assert counting.commands == 1 and handed_on is None and rows


def test_every_snapshot_created_is_cleaned_up_on_success_and_failure(workspace) -> None:
    counting = _Counting()
    _full(workspace, counting, None)
    assert (counting.created, counting.cleaned) == (1, 1)

    def failing(_workspace, _commands, **_route):
        raise validation.WorkspaceError("validation_failed:red")

    with pytest.raises(validation.WorkspaceError):
        validation.run_full_snapshot_validations(
            workspace, _CARD, {}, _CANDIDATE,
            create_snapshot=counting.create_snapshot,
            cleanup_workspace=counting.cleanup,
            run_validations=failing,
            route_resolver=_route,
            baseline_comparer=lambda *a, **k: [],
        )
    assert (counting.created, counting.cleaned) == (2, 2)


def test_accept_review_declares_the_union_helper_as_a_local_name() -> None:
    assert "_union_validations_once" in accept_review.ACCEPT_REVIEW_LOCAL_NAMES


def _accept_env_module():
    """The sibling accept-level seam harness, loaded by path (no package import)."""
    name = "_nf01317_accept_review_seams"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name("test_process_launcher_accept_review.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_ACCEPT_IDENTITY = "a" * 64


def _drive_accept(tmp_path: Path, monkeypatch, *, finalizer_receipt: bool):
    """Run ``accept_review`` down the combined-tree path; count declared runs."""
    seams = _accept_env_module()
    manager, _packet = seams._supplemental_env(
        tmp_path, monkeypatch,
        [{"task_id": "QR-C", "request_id": "rq-c", "lens": "correctness",
          "findings": [], "packet": "sealed"}],
    )
    if finalizer_receipt:
        evidence = manager.cards["T-1"]["terminal_review"]["evidence"]
        evidence["validation"] = [
            {"command": "true", "returncode": 0, "timed_out": False}
        ]
        evidence["quality_gate"] = {
            "full_validation_snapshot": {
                "schema_id": "aiworkhub.full_validation_snapshot.v1",
                "request_id": "R-1",
                "source_base_oid": "0" * 40,
                "tree_identity_sha256": _ACCEPT_IDENTITY,
                "validations_all_passed": True,
            }
        }
    monkeypatch.setattr(
        validation, "combined_tree_identity", lambda *a, **k: _ACCEPT_IDENTITY
    )
    declared_runs: list[int] = []
    snapshot_reuse: list[object] = []

    def declared(*_args, **_kwargs):
        declared_runs.append(1)
        return [{"command": "true", "returncode": 0, "timed_out": False}]

    def full_snapshot(*_args, **kwargs):
        snapshot_reuse.append(kwargs.get("reuse"))
        return [], {}

    monkeypatch.setattr(process_launcher, "_run_declared_validations", declared)
    monkeypatch.setattr(
        process_launcher, "_run_full_snapshot_validations", full_snapshot
    )
    seams._accept(manager)
    return len(declared_runs), snapshot_reuse


def test_accept_with_matching_finalizer_receipt_runs_zero_declared_commands(
    tmp_path: Path, monkeypatch
) -> None:
    runs, snapshot_reuse = _drive_accept(tmp_path, monkeypatch, finalizer_receipt=True)
    assert runs == 0
    assert len(snapshot_reuse) == 1 and snapshot_reuse[0]["validations"]


def test_accept_without_finalizer_receipt_runs_declared_commands_exactly_once(
    tmp_path: Path, monkeypatch
) -> None:
    runs, snapshot_reuse = _drive_accept(tmp_path, monkeypatch, finalizer_receipt=False)
    assert runs == 1
    assert len(snapshot_reuse) == 1
    assert snapshot_reuse[0]["reused_from"] == validation.ACCEPT_UNION_REUSE_SOURCE
    assert (
        snapshot_reuse[0]["full_validation_snapshot"]["tree_identity_sha256"]
        == _ACCEPT_IDENTITY
    )
