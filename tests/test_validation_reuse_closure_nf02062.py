"""NF-2026-02062: validation reuse is keyed by the validation closure, not the exact HEAD."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import process_launcher_validation as validation
from aiworkhub import worker_workspace
from aiworkhub.worker_workspace import cleanup_workspace, create_workspace

_COMMAND = "python -c \"print('probe')\""
_CARD = {
    "allowed_writes": ["pkg/feature.py"],
    "read_first": ["scripts/helper.py"],
    "immutable_inputs": [],
    "required_outputs": [],
    "validation": [_COMMAND],
}
_CANDIDATE = ["pkg/feature.py"]
_TESTS = ["tests/test_a.py", "tests/test_b.py"]


class _Ran(Exception):
    """Raised by the fake snapshot factory: the validations would have run."""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _commit(repo: Path, relative: str, text: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", relative)
    _git(repo, "commit", "-m", f"touch {relative}")


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
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
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    ws = create_workspace(repo, "closure-request", _CARD, "validation")
    (ws.path / "pkg" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    yield ws
    cleanup_workspace(ws.repo, ws.path, ws.home)


def _route(metadata):
    return {"backend": metadata.get("backend")}


def _identity(ws, authority=None, route_metadata=None):
    return validation.combined_tree_identity(
        ws,
        _CARD if authority is None else authority,
        {} if route_metadata is None else route_metadata,
        _CANDIDATE,
        route_resolver=_route,
    )


def _recorded(ws, identity, **snapshot_extra):
    return {
        "full_validation_snapshot": {
            "schema_id": "aiworkhub.full_validation_snapshot.v1",
            "request_id": ws.request_id,
            "repo": str(ws.repo),
            "source_base_oid": ws.base_oid,
            "snapshot_base_oid": ws.base_oid,
            "combined_tree": {"merged_paths": {}, "candidate_paths": _CANDIDATE},
            "tree_identity_sha256": str(identity),
            "tree_identity_key": identity.key,
            "tree_identity_head_oid": identity.head_oid,
            "validations_all_passed": True,
            **snapshot_extra,
        },
        "validations": [{"command": _COMMAND, "returncode": 0, "timed_out": False}],
    }


def _run_full(ws, reuse, *, authority=None, route_metadata=None):
    ran: list[int] = []

    def create_snapshot(*_args):
        ran.append(1)
        raise _Ran

    result = None
    try:
        result = validation.run_full_snapshot_validations(
            ws,
            _CARD if authority is None else authority,
            {} if route_metadata is None else route_metadata,
            _CANDIDATE,
            create_snapshot=create_snapshot,
            cleanup_workspace=lambda *args: None,
            run_validations=lambda *_a, **_k: [],
            route_resolver=_route,
            baseline_comparer=lambda *a, **k: [],
            reuse=reuse,
        )
    except _Ran:
        pass
    return result, bool(ran)


def test_head_moved_outside_the_closure_reuses_and_says_so(workspace) -> None:
    before = _identity(workspace)
    assert before.key == validation.CLOSURE_KEY == "closure_unchanged"
    assert "scripts/helper.py" in before.closure and "pkg/feature.py" in before.closure
    reuse = _recorded(workspace, before)
    _commit(workspace.repo, "docs/unrelated.txt", "not read by the validation\n")
    after = _identity(workspace)
    assert after == before and after.head_oid != before.head_oid
    result, ran = _run_full(workspace, reuse)
    assert not ran
    rows, snapshot = result
    assert rows == reuse["validations"]
    assert snapshot["reused"] is True
    assert snapshot["tree_identity_key"] == "closure_unchanged"
    assert snapshot["tree_identity_head_moved"] is True
    assert snapshot["moved_paths_outside_closure"] == 1


def test_unmoved_head_reports_zero_moved_paths(workspace) -> None:
    identity = _identity(workspace)
    result, ran = _run_full(workspace, _recorded(workspace, identity))
    assert not ran
    snapshot = result[1]
    assert snapshot["tree_identity_head_moved"] is False
    assert snapshot["moved_paths_outside_closure"] == 0


@pytest.mark.parametrize(
    "mutate",
    ["closure_path", "command", "toolchain", "route"],
)
def test_any_closure_command_toolchain_or_route_change_misses(workspace, mutate) -> None:
    authority = {**_CARD, "toolchain_authority_receipt": {"digest": "a"}}
    before = _identity(workspace, authority=authority)
    reuse = _recorded(workspace, before)
    route_metadata: dict[str, Any] = {}
    if mutate == "closure_path":
        _commit(workspace.repo, "scripts/helper.py", "MARKER = 'moved'\n")
    elif mutate == "command":
        authority = {**authority, "validation": [_COMMAND, "python -c \"pass\""]}
    elif mutate == "toolchain":
        authority = {**authority, "toolchain_authority_receipt": {"digest": "b"}}
    else:
        route_metadata = {"backend": "other"}
    after = _identity(workspace, authority=authority, route_metadata=route_metadata)
    assert after != before
    result, ran = _run_full(
        workspace, reuse, authority=authority, route_metadata=route_metadata
    )
    assert ran and result is None


def test_interpreter_change_misses(workspace, monkeypatch) -> None:
    before = _identity(workspace)
    monkeypatch.setattr(validation.sys, "executable", str(workspace.path / "other-python"))
    assert _identity(workspace) != before


def test_candidate_change_misses_under_the_closure_key(workspace) -> None:
    before = _identity(workspace)
    (workspace.path / "pkg" / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert _identity(workspace) != before


@pytest.mark.parametrize("failure", ["raises", "empty"])
def test_underivable_closure_falls_back_to_the_exact_head(
    workspace, monkeypatch, failure
) -> None:
    def broken(*_args):
        if failure == "raises":
            raise worker_workspace.WorkspaceError("closure_unavailable")
        return [], (), []

    monkeypatch.setattr(worker_workspace, "_declared_workspace_seed_closure", broken)
    before = _identity(workspace)
    assert before.key == validation.EXACT_HEAD_KEY == "exact_head"
    assert before.closure == frozenset()
    reuse = _recorded(workspace, before)
    _commit(workspace.repo, "docs/unrelated.txt", "anything\n")
    assert _identity(workspace) != before
    result, ran = _run_full(workspace, reuse)
    assert ran and result is None


def test_the_closure_comes_from_one_call_to_the_seed_closure(workspace, monkeypatch) -> None:
    real = worker_workspace._declared_workspace_seed_closure
    calls: list[tuple[Any, ...]] = []

    def spy(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(worker_workspace, "_declared_workspace_seed_closure", spy)
    identity = _identity(workspace)
    assert len(calls) == 1
    _live, _support, seeded = real(*calls[0])
    assert identity.closure == frozenset(seeded)


def test_union_reuse_carries_a_receipt_beside_an_unchanged_snapshot(workspace) -> None:
    before = _identity(workspace)
    reuse = _recorded(workspace, before)
    _commit(workspace.repo, "docs/unrelated.txt", "x\n")
    rows, union = validation.run_union_validations_once(
        workspace, _CARD, {}, _CANDIDATE,
        combined_tree={"merged_paths": {}}, snapshot_base_oid=workspace.base_oid,
        run_declared=lambda: pytest.fail("the declared validations must be reused"),
        route_resolver=_route, reuse=reuse,
    )
    assert rows == reuse["validations"]
    assert union["full_validation_snapshot"] == reuse["full_validation_snapshot"]
    assert union["tree_identity_receipt"] == {
        "key": "closure_unchanged", "moved_paths_outside_closure": 1, "head_moved": True,
    }


@pytest.mark.parametrize(
    ("closure", "covered"),
    [(set(_TESTS), True), ({_TESTS[0]}, False), (set(), False)],
)
def test_finalization_records_whether_the_closure_covers_the_neighbours(
    tmp_path: Path, monkeypatch, closure: set[str], covered: bool
) -> None:
    identity = validation.TreeIdentity(
        "d" * 64, key=validation.CLOSURE_KEY, head_oid="h" * 40, closure=closure
    )
    monkeypatch.setattr(validation, "combined_tree_identity", lambda *a, **k: identity)
    monkeypatch.setattr(
        neighbours, "run_finalization_neighbours",
        lambda *a, **k: {"passed": True, "selected": list(_TESTS)},
    )
    ws = SimpleNamespace(
        request_id="req", repo=tmp_path / "repo", base_oid="b" * 40,
        path=tmp_path / "ws", home=tmp_path / "home",
    )
    snap = SimpleNamespace(
        request_id="req", repo=tmp_path / "repo", base_oid="s" * 40,
        path=tmp_path / "snap", home=tmp_path / "snap-home",
    )
    _rows, record = validation.run_full_snapshot_validations(
        ws, _CARD, {}, _CANDIDATE,
        create_snapshot=lambda *_a: (snap, {"merged_paths": {}}),
        cleanup_workspace=lambda *_a: None,
        run_validations=lambda _ws, commands, **_r: [
            {"command": c, "returncode": 0, "timed_out": False} for c in commands
        ],
        route_resolver=_route, baseline_comparer=lambda *a, **k: [],
    )
    assert record["tree_identity_key"] == "closure_unchanged"
    assert record["tree_identity_head_oid"] == "h" * 40
    assert record["tree_identity_sha256"] == "d" * 64
    assert record["neighbour_tests"]["closure_covers_selected"] is covered


_SELECTION = neighbours.NeighbourSelection(tests=tuple(_TESTS), reason="", limit=12)


def _neighbour_reuse(covered: bool, receipt: Any) -> Any:
    snapshot = {
        "neighbour_tests": {
            "passed": True, "selected": list(_TESTS), "closure_covers_selected": covered,
        },
    }
    finalizer = {"full_validation_snapshot": snapshot, "reused_from": "finalization"}
    union = dict(finalizer)
    if receipt is not None:
        union["tree_identity_receipt"] = receipt
    return neighbours.reused_finalization_neighbours(finalizer, union, _SELECTION)


@pytest.mark.parametrize(
    ("covered", "receipt", "reused"),
    [
        (False, None, True),  # exact-HEAD union record: unchanged behaviour
        (False, {"key": "exact_head", "head_moved": False}, True),
        (False, {"key": "closure_unchanged", "head_moved": False}, True),
        (False, {"key": "closure_unchanged", "head_moved": True}, False),
        (False, {"key": "closure_unchanged", "head_moved": None}, False),
        (True, {"key": "closure_unchanged", "head_moved": True}, True),
    ],
)
def test_neighbour_evidence_crosses_a_moved_head_only_when_covered(
    covered: bool, receipt: Any, reused: bool
) -> None:
    evidence = _neighbour_reuse(covered, receipt)
    assert (evidence is not None) is reused


def test_non_hex_recorded_head_never_reaches_git_argv(
    workspace, tmp_path: Path, monkeypatch
) -> None:
    before = _identity(workspace)
    pwned = tmp_path / "pwned"
    reuse = _recorded(workspace, before)
    reuse["full_validation_snapshot"]["tree_identity_head_oid"] = "--output=" + str(pwned)
    _commit(workspace.repo, "docs/unrelated.txt", "not read by the validation\n")
    seen: list[list[str]] = []
    real = worker_workspace._run

    def spy(argv, *args, **kwargs):
        seen.append([str(item) for item in argv])
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(worker_workspace, "_run", spy)
    result, ran = _run_full(workspace, reuse)
    assert not ran
    snapshot = result[1]
    assert snapshot["reused"] is True
    assert snapshot["moved_paths_outside_closure"] is None
    assert not any(item.startswith("--output") for argv in seen for item in argv)
    assert not pwned.exists()


def test_no_identity_records_no_key() -> None:
    assert validation._identity_record_fields(None) == {
        "tree_identity_key": None,
        "tree_identity_head_oid": None,
        "moved_paths_outside_closure": None,
    }
