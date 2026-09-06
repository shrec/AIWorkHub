"""NF606: coordinator validation uses the complete repository snapshot."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import process_launcher
from aiworkhub import process_launcher_validation as validation
from aiworkhub.worker_workspace import (
    ValidationRunError,
    WorkspaceError,
    cleanup_workspace,
    create_combined_validation_workspace,
    create_workspace,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "AIWorkHub Test")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "runtime_helper.py").write_text(
        "MARKER = 'ok'\n", encoding="utf-8"
    )
    (repo / "data").mkdir()
    (repo / "data" / "fixture.json").write_text(
        json.dumps({"ok": True}), encoding="utf-8"
    )
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "child.py").write_text("VALUE = 'child'\n", encoding="utf-8")
    (repo / "pkg" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


_CARD = {
    "allowed_writes": ["pkg/feature.py"],
    "read_first": [],
    "immutable_inputs": [],
    "required_outputs": [],
    # No repository operand is named here: the regression is precisely that
    # inputs reached dynamically at runtime must still exist in validation.
    "validation": ["python -c \"print('probe')\""],
}


def _runtime_probe(workspace, commands, **_route):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace.path)
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,pathlib,sys\n"
            "sys.path.insert(0, 'scripts')\n"
            "import runtime_helper\n"
            "import pkg.child\n"
            "data=json.loads(pathlib.Path('data/fixture.json').read_text())\n"
            "assert runtime_helper.MARKER == 'ok'\n"
            "assert pkg.child.VALUE == 'child' and data['ok']\n",
        ],
        cwd=workspace.path,
        env=env,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    return [{"command": commands[0], "returncode": 0, "timed_out": False}]


def _candidate(tmp_path: Path, monkeypatch, request_id: str):
    repo = _repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    workspace = create_workspace(repo, request_id, _CARD, "validation")
    (workspace.path / "pkg" / "feature.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    return workspace


def test_full_snapshot_includes_runtime_only_inputs_and_is_cleaned(
    tmp_path: Path, monkeypatch
) -> None:
    candidate = _candidate(tmp_path, monkeypatch, "full-snapshot")
    created = []

    def create_snapshot(workspace, card, paths):
        snapshot, receipt = create_combined_validation_workspace(
            workspace, card, paths
        )
        created.append(snapshot)
        return snapshot, receipt

    try:
        assert not (candidate.path / "scripts").exists()
        assert not (candidate.path / "data").exists()
        assert not (candidate.path / "pkg" / "child.py").exists()
        rows, receipt = validation.run_full_snapshot_validations(
            candidate,
            _CARD,
            {"adapter_id": "claude_cli"},
            ["pkg/feature.py"],
            create_snapshot=create_snapshot,
            cleanup_workspace=cleanup_workspace,
            run_validations=_runtime_probe,
            route_resolver=lambda _metadata: {},
            baseline_comparer=lambda *args, **kwargs: pytest.fail(
                "baseline comparison is not applicable"
            ),
        )
    finally:
        cleanup_workspace(candidate.repo, candidate.path, candidate.home)

    assert rows[0]["returncode"] == 0
    assert receipt["schema_id"] == "aiworkhub.full_validation_snapshot.v1"
    assert receipt["request_id"] == candidate.request_id
    assert receipt["combined_tree"]["candidate_paths"] == ["pkg/feature.py"]
    assert created and not created[0].path.exists()


def test_empty_full_snapshot_candidate_fails_before_materialization(
    tmp_path: Path, monkeypatch
) -> None:
    candidate = _candidate(tmp_path, monkeypatch, "empty-snapshot")
    calls = []
    try:
        with pytest.raises(WorkspaceError, match="full_snapshot_candidate_empty"):
            validation.run_full_snapshot_validations(
                candidate,
                _CARD,
                {},
                [],
                create_snapshot=lambda *args: calls.append(args),
                cleanup_workspace=cleanup_workspace,
                run_validations=_runtime_probe,
                route_resolver=lambda _metadata: {},
                baseline_comparer=lambda *args, **kwargs: [],
            )
    finally:
        cleanup_workspace(candidate.repo, candidate.path, candidate.home)
    assert calls == []


def test_full_snapshot_is_cleaned_when_validation_fails(
    tmp_path: Path, monkeypatch
) -> None:
    candidate = _candidate(tmp_path, monkeypatch, "failing-snapshot")
    created = []

    def create_snapshot(workspace, card, paths):
        snapshot, receipt = create_combined_validation_workspace(
            workspace, card, paths
        )
        created.append(snapshot)
        return snapshot, receipt

    def fail(*_args, **_kwargs):
        raise ValidationRunError(
            "validation_failed:boom",
            [{"command": _CARD["validation"][0], "returncode": 1}],
        )

    def baseline_ineligible(*_args, **_kwargs):
        raise WorkspaceError("baseline_comparison_ineligible")

    try:
        with pytest.raises(ValidationRunError, match="validation_failed:boom"):
            validation.run_full_snapshot_validations(
                candidate,
                _CARD,
                {},
                ["pkg/feature.py"],
                create_snapshot=create_snapshot,
                cleanup_workspace=cleanup_workspace,
                run_validations=fail,
                route_resolver=lambda _metadata: {},
                baseline_comparer=baseline_ineligible,
            )
    finally:
        cleanup_workspace(candidate.repo, candidate.path, candidate.home)
    assert created and not created[0].path.exists()


def test_process_launcher_binds_real_full_snapshot_authority(
    tmp_path: Path, monkeypatch
) -> None:
    candidate = _candidate(tmp_path, monkeypatch, "production-snapshot")
    monkeypatch.setattr(process_launcher, "run_validations", _runtime_probe)
    try:
        rows, receipt = process_launcher._run_full_snapshot_validations(
            candidate,
            _CARD,
            {"adapter_id": "claude_cli"},
            ["pkg/feature.py"],
        )
    finally:
        cleanup_workspace(candidate.repo, candidate.path, candidate.home)
    assert rows[0]["returncode"] == 0
    assert receipt["combined_tree"]["observed_candidate_paths"] == [
        "pkg/feature.py"
    ]
