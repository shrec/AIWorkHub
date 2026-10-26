"""NF-2026-01670: accept reuses the finalizer's combined-tree validation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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


class _Ran(Exception):
    """Raised by the fake snapshot factory: the validations would have run."""


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
    (repo / "scripts" / "helper.py").write_text("MARKER = 'ok'\n", encoding="utf-8")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    ws = create_workspace(repo, "reuse-request", _CARD, "validation")
    (ws.path / "pkg" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    yield ws
    cleanup_workspace(ws.repo, ws.path, ws.home)


def _route(metadata):
    return {"backend": metadata.get("backend")}


def _identity(ws, authority=None, route_metadata=None, route_resolver=_route):
    return validation.combined_tree_identity(
        ws,
        _CARD if authority is None else authority,
        {} if route_metadata is None else route_metadata,
        _CANDIDATE,
        route_resolver=route_resolver,
    )


def _recorded(ws, identity, **overrides):
    snapshot = {
        "schema_id": "aiworkhub.full_validation_snapshot.v1",
        "request_id": ws.request_id,
        "repo": str(ws.repo),
        "source_base_oid": ws.base_oid,
        "snapshot_base_oid": ws.base_oid,
        "combined_tree": {"merged_paths": {}, "candidate_paths": _CANDIDATE},
        "tree_identity_sha256": identity,
        "validations_all_passed": True,
    }
    rows = [{"command": _COMMAND, "returncode": 0, "timed_out": False}]
    snapshot.update(overrides.pop("snapshot", {}))
    rows = overrides.pop("rows", rows)
    return {"full_validation_snapshot": snapshot, "validations": rows}


class _Counters:
    def __init__(self) -> None:
        self.snapshots = 0
        self.runs = 0

    def create_snapshot(self, *_args):
        self.snapshots += 1
        raise _Ran

    def run_validations(self, _workspace, commands, **_route):
        self.runs += 1
        return [{"command": c, "returncode": 0, "timed_out": False} for c in commands]


def _call(ws, counters, *, reuse=None, authority=None, route_metadata=None,
          route_resolver=_route):
    return validation.run_full_snapshot_validations(
        ws,
        _CARD if authority is None else authority,
        {} if route_metadata is None else route_metadata,
        _CANDIDATE,
        create_snapshot=counters.create_snapshot,
        cleanup_workspace=lambda *args: None,
        run_validations=counters.run_validations,
        route_resolver=route_resolver,
        baseline_comparer=lambda *a, **k: [],
        reuse=reuse,
    )


def _assert_runs(ws, reuse, **kwargs):
    counters = _Counters()
    with pytest.raises(_Ran):
        _call(ws, counters, reuse=reuse, **kwargs)
    assert counters.snapshots == 1


def test_identity_is_stable_and_present(workspace) -> None:
    first = _identity(workspace)
    assert isinstance(first, str) and len(first) == 64
    assert _identity(workspace) == first


def test_identical_identity_reuses_without_snapshot_or_runner(workspace) -> None:
    identity = _identity(workspace)
    reuse = _recorded(workspace, identity)
    counters = _Counters()
    rows, snapshot = _call(workspace, counters, reuse=reuse)
    assert counters.snapshots == 0 and counters.runs == 0
    assert rows == reuse["validations"]
    assert snapshot["reused"] is True
    assert snapshot["reused_from"] == "finalization"
    assert snapshot["combined_tree"] == reuse["full_validation_snapshot"]["combined_tree"]
    assert snapshot["tree_identity_sha256"] == identity


def test_real_run_records_identity_and_then_is_reused(workspace) -> None:
    counters = _Counters()

    def run(ws, authority, *, reuse=None):
        return validation.run_full_snapshot_validations(
            ws, authority, {}, _CANDIDATE,
            create_snapshot=create_combined_validation_workspace,
            cleanup_workspace=cleanup_workspace,
            run_validations=counters.run_validations,
            route_resolver=_route,
            baseline_comparer=lambda *a, **k: [],
            reuse=reuse,
        )

    rows, snapshot = run(workspace, _CARD)
    assert counters.runs == 1
    assert snapshot["tree_identity_sha256"] == _identity(workspace)
    assert snapshot["validations_all_passed"] is True
    assert "reused" not in snapshot

    again_rows, again = run(
        workspace, _CARD,
        reuse={"full_validation_snapshot": snapshot, "validations": rows},
    )
    assert counters.runs == 1
    assert again["reused"] is True and again_rows == rows


def _mutate_head(ws, _ctx):
    _git(ws.repo, "commit", "--allow-empty", "-m", "moved head")


def _mutate_canonical_delta(ws, _ctx):
    (ws.repo / "scripts" / "helper.py").write_text("MARKER = 'changed'\n", encoding="utf-8")


def _mutate_candidate(ws, _ctx):
    (ws.path / "pkg" / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")


def _mutate_command(_ws, ctx):
    ctx["authority"] = {**_CARD, "validation": [_COMMAND, "python -c \"pass\""]}


def _mutate_toolchain(_ws, ctx):
    ctx["authority"] = {**_CARD, "toolchain_authority_receipt": {"digest": "b"}}


def _mutate_route(_ws, ctx):
    ctx["route_metadata"] = {"backend": "other"}


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_head,
        _mutate_canonical_delta,
        _mutate_candidate,
        _mutate_command,
        _mutate_toolchain,
        _mutate_route,
    ],
)
def test_every_identity_input_change_forces_a_real_run(workspace, mutate) -> None:
    (workspace.repo / "scripts" / "helper.py").write_text(
        "MARKER = 'dirty'\n", encoding="utf-8"
    )
    base_authority = {**_CARD, "toolchain_authority_receipt": {"digest": "a"}}
    before = _identity(workspace, authority=base_authority)
    reuse = _recorded(workspace, before)
    ctx = {"authority": base_authority, "route_metadata": {}}
    mutate(workspace, ctx)
    assert _identity(
        workspace, authority=ctx["authority"], route_metadata=ctx["route_metadata"]
    ) != before
    _assert_runs(
        workspace, reuse,
        authority=ctx["authority"], route_metadata=ctx["route_metadata"],
    )


def test_recorded_red_row_forces_a_real_run(workspace) -> None:
    identity = _identity(workspace)
    red = [{"command": _COMMAND, "returncode": 1, "timed_out": False}]
    _assert_runs(workspace, _recorded(workspace, identity, rows=red))


def test_recorded_all_passed_flag_false_forces_a_real_run(workspace) -> None:
    identity = _identity(workspace)
    _assert_runs(
        workspace,
        _recorded(workspace, identity, snapshot={"validations_all_passed": False}),
    )


def test_row_count_mismatch_forces_a_real_run(workspace) -> None:
    identity = _identity(workspace)
    green = {"command": _COMMAND, "returncode": 0}
    _assert_runs(workspace, _recorded(workspace, identity, rows=[green, green]))
    _assert_runs(workspace, _recorded(workspace, identity, rows=[]))


def test_request_id_or_base_oid_mismatch_forces_a_real_run(workspace) -> None:
    identity = _identity(workspace)
    _assert_runs(
        workspace, _recorded(workspace, identity, snapshot={"request_id": "other"})
    )
    _assert_runs(
        workspace,
        _recorded(workspace, identity, snapshot={"source_base_oid": "0" * 40}),
    )


def test_identity_computation_failure_forces_a_real_run(workspace) -> None:
    def broken(_metadata):
        raise RuntimeError("route unavailable")

    assert _identity(workspace, route_resolver=broken) is None
    identity = _identity(workspace)
    _assert_runs(workspace, _recorded(workspace, identity), route_resolver=broken)
    _assert_runs(workspace, _recorded(workspace, None), route_resolver=broken)


def test_no_reuse_argument_behaves_as_before(workspace) -> None:
    _assert_runs(workspace, None)


_TERMINAL = {
    "evidence": {
        "validation": [{"command": _COMMAND, "returncode": 0}],
        "quality_gate": {"full_validation_snapshot": {"tree_identity_sha256": "ab"}},
    }
}


def test_accept_builds_reuse_only_without_manager_correction() -> None:
    built = accept_review._finalizer_validation_reuse(_TERMINAL, correction_record=None)
    assert built == {
        "full_validation_snapshot": {"tree_identity_sha256": "ab"},
        "validations": [{"command": _COMMAND, "returncode": 0}],
    }
    assert (
        accept_review._finalizer_validation_reuse(
            _TERMINAL, correction_record={"paths": ["pkg/feature.py"]}
        )
        is None
    )


@pytest.mark.parametrize(
    "terminal",
    [
        {},
        {"evidence": None},
        {"evidence": {"validation": [], "quality_gate": _TERMINAL["evidence"]["quality_gate"]}},
        {"evidence": {"validation": _TERMINAL["evidence"]["validation"]}},
        {"evidence": {"validation": _TERMINAL["evidence"]["validation"],
                      "quality_gate": {"full_validation_snapshot": {}}}},
        {"evidence": {"validation": "rows",
                      "quality_gate": _TERMINAL["evidence"]["quality_gate"]}},
    ],
)
def test_accept_passes_nothing_for_missing_or_malformed_records(terminal) -> None:
    assert accept_review._finalizer_validation_reuse(terminal, correction_record=None) is None
