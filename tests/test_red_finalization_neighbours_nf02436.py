"""NF-2026-02436: red finalization neighbours stop the card before review_ready.

Before this fix a red neighbour run at finalization was stored on the
full-validation snapshot with ``passed: False`` and nothing read it: the card
reached review, both reviewers spent a round, and accept re-ran the known red
suite in full only to refuse on it.
"""

from __future__ import annotations

import ast
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import process_launcher_accept_review as accept_review
from aiworkhub import process_launcher_validation as validation
from aiworkhub import review_packet
from aiworkhub.worker_workspace import (
    ValidationEnvironmentBlocked,
    ValidationRunError,
    WorkspaceError,
)

_DECLARED = "python -m pytest tests/test_declared.py -q"
_AUTHORITY = {"allowed_writes": ["src/aiworkhub/engine.py"], "validation": [_DECLARED]}
_CANDIDATE = ["src/aiworkhub/engine.py"]
_TESTS = ("tests/test_hooks.py", "tests/test_other.py")
_NODE = "tests/test_hooks.py::test_tracked_hook_copy_synced"
_SELECTION = neighbours.NeighbourSelection(
    tests=_TESTS, reason="", limit=12,
    production_paths=("src/aiworkhub/engine.py",), related_count=2,
)
_GREEN = {"returncode": 0, "timed_out": False, "stdout_tail": "2 passed"}
_RED = {
    "returncode": 1,
    "timed_out": False,
    "stdout_tail": f"FAILED {_NODE} - AssertionError: hook copy differs",
    "pytest_failure_digest": {
        "failures": [{"test": _NODE, "kind": "failed", "message": "hook copy differs"}],
        "omitted": 0,
    },
}


@contextlib.contextmanager
def _no_base(_workspace: Any, _card: Any) -> Iterator[Any]:
    raise WorkspaceError("neighbour_canonical_head_unavailable")
    yield  # pragma: no cover


@pytest.fixture(autouse=True)
def _selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(neighbours, "select_neighbour_tests", lambda *a, **k: _SELECTION)
    monkeypatch.setattr(neighbours, "base_commit_workspace", _no_base)


class _Snapshot:
    """Fake run_full_snapshot_validations collaborators."""

    def __init__(self, tmp_path: Path, neighbour: Any) -> None:
        self.workspace = SimpleNamespace(
            request_id="req-1", repo=tmp_path / "repo", base_oid="b" * 40,
            path=tmp_path / "ws", home=tmp_path / "home",
        )
        self.snapshot = SimpleNamespace(
            request_id="req-1", repo=tmp_path / "repo", base_oid="s" * 40,
            path=tmp_path / "snap", home=tmp_path / "snap-home",
        )
        self.neighbour = neighbour
        self.neighbour_runs = 0
        self.cleaned = 0

    def run_validations(self, _workspace: Any, commands: list[str], **_route: Any) -> Any:
        if commands == [_DECLARED]:
            return [{"command": _DECLARED, **_GREEN}]
        self.neighbour_runs += 1
        return [{"command": commands[0], **self.neighbour}]

    def call(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        def cleanup(*_args: Any) -> None:
            self.cleaned += 1

        return validation.run_full_snapshot_validations(
            self.workspace, _AUTHORITY, {}, _CANDIDATE,
            create_snapshot=lambda *_a: (self.snapshot, {"merged_paths": {}}),
            cleanup_workspace=cleanup,
            run_validations=self.run_validations,
            route_resolver=lambda _metadata: {},
            baseline_comparer=lambda *_a, **_k: [],
        )


# --- finalization: routed like a failing declared validation ------------------------


def test_red_finalization_neighbours_fail_validation_naming_the_tests(tmp_path: Path) -> None:
    fake = _Snapshot(tmp_path, _RED)
    with pytest.raises(ValidationRunError) as caught:
        fake.call()
    exc = caught.value
    assert not isinstance(exc, ValidationEnvironmentBlocked)
    # The same terminal state a failing declared validation finalizes with.
    assert validation.terminal_state_for_workspace_error(exc) == "validation_failed"
    message = str(exc)
    assert message.startswith("validation_failed:" + neighbours.FINALIZATION_FAILURE_PREFIX)
    detail = json.loads(message.split(neighbours.FINALIZATION_FAILURE_PREFIX, 1)[1])
    assert detail["failed"] == [_NODE]
    assert detail["tests"] == list(_TESTS)
    assert detail["failure_kind"] == neighbours.FAILURE_KIND_TEST
    # Evidence rows: the declared row, then the neighbour row whose digest names the test.
    assert exc.results[0]["command"] == _DECLARED
    neighbour_rows = [row for row in exc.results if row.get("finalization_neighbour")]
    assert neighbour_rows
    names = [
        name for row in neighbour_rows
        for name, _line in neighbours._digest_entries(row)[0]
    ]
    assert _NODE in names
    assert fake.cleaned == 1


def test_green_finalization_neighbours_are_recorded_as_before(tmp_path: Path) -> None:
    fake = _Snapshot(tmp_path, _GREEN)
    rows, record = fake.call()
    assert rows[0]["command"] == _DECLARED
    assert record["neighbour_tests"]["passed"] is True
    assert "failed" not in record["neighbour_tests"]
    assert neighbours.finalization_neighbour_refusal(record["neighbour_tests"], rows) is None


def test_environment_blocked_neighbours_route_like_a_blocked_declared_validation() -> None:
    recorded = {
        "selected": list(_TESTS), "passed": False, "error": "x",
        "failed": [], "first_failure": "spawn denied",
        "failure_kind": neighbours.FAILURE_KIND_ENVIRONMENT, "restriction": "spawn_denied",
        "validation": [{"command": "n", "returncode": None, "timed_out": False}],
    }
    exc = neighbours.finalization_neighbour_refusal(recorded, [{"command": _DECLARED}])
    assert isinstance(exc, ValidationEnvironmentBlocked)
    assert exc.restriction == "spawn_denied"
    assert validation.terminal_state_for_workspace_error(exc) == "finalize_failed"


def test_unrunnable_neighbours_never_reach_review_ready(tmp_path: Path) -> None:
    fake = _Snapshot(tmp_path, _GREEN)
    fake.run_validations = lambda _ws, commands, **_r: (  # type: ignore[method-assign]
        [{"command": _DECLARED, **_GREEN}] if commands == [_DECLARED]
        else (_ for _ in ()).throw(RuntimeError("runner crashed"))
    )
    with pytest.raises(WorkspaceError) as caught:
        fake.call()
    assert not isinstance(caught.value, ValidationRunError)
    assert str(caught.value).startswith(neighbours.FINALIZATION_UNRUNNABLE_PREFIX)
    assert validation.terminal_state_for_workspace_error(caught.value) == "finalize_failed"


# --- the recorded red result, read by review_packet / accept_preview -----------------


def _red_record(**overrides: Any) -> dict[str, Any]:
    return {
        "selected": list(_TESTS), "command": "c", "passed": False,
        "error": "neighbour_tests_failed:{}", "failed": [_NODE],
        "first_failure": f"FAILED {_NODE}", "failure_kind": neighbours.FAILURE_KIND_TEST,
        **overrides,
    }


def _snapshot(neighbour: Any) -> dict[str, Any]:
    return {
        "schema_id": "aiworkhub.full_validation_snapshot.v1",
        "request_id": "req-1",
        "source_base_oid": "b" * 40,
        "tree_identity_sha256": "id-1",
        "validations_all_passed": True,
        "neighbour_tests": neighbour,
    }


def _evidence(neighbour: Any) -> dict[str, Any]:
    return {
        "validation": [{"command": _DECLARED, "returncode": 0, "timed_out": False}],
        "quality_gate": {"full_validation_snapshot": _snapshot(neighbour)},
    }


def test_review_packet_names_the_failed_tests_of_a_recorded_red_run() -> None:
    view = review_packet.red_finalization_neighbours(_evidence(_red_record()))
    assert view is not None
    assert view["failed"] == [_NODE] and view["selected"] == list(_TESTS)


def test_review_packet_reads_a_legacy_record_from_its_refusal_text() -> None:
    legacy = {
        "selected": list(_TESTS), "passed": False,
        "error": neighbours.NEIGHBOUR_FAILURE_PREFIX + json.dumps(
            {"tests": list(_TESTS), "failed": [_NODE], "first_failure": "boom"}
        ),
    }
    view = review_packet.red_finalization_neighbours(_evidence(legacy))
    assert view is not None and view["failed"] == [_NODE] and view["first_failure"] == "boom"


@pytest.mark.parametrize(
    "neighbour",
    [{"selected": list(_TESTS), "passed": True}, None, {"passed": "false"}],
    ids=["green", "absent", "not_strict_false"],
)
def test_review_packet_is_silent_without_a_red_record(neighbour: Any) -> None:
    assert review_packet.red_finalization_neighbours(_evidence(neighbour)) is None
    assert review_packet.red_finalization_neighbours({}) is None


def test_accept_preview_lists_the_red_record_as_a_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import process_launcher as pl
    from aiworkhub import quality_evidence

    card = {"terminal_review": {"substatus": "review_ready", "evidence": {
        **_evidence(_red_record()), "changed_paths": _CANDIDATE,
    }}}
    monkeypatch.setattr(pl, "_parse_card", lambda _raw, _tid: card)
    monkeypatch.setattr(pl, "_canonical_task_status", lambda _card: "review_ready")
    monkeypatch.setattr(pl, "WorkerWorkspace", SimpleNamespace(
        from_metadata=lambda _m: (_ for _ in ()).throw(ValueError("no workspace"))
    ))
    monkeypatch.setattr(accept_review, "accept_in_flight", lambda *_a: None)
    monkeypatch.setattr(accept_review, "effective_requested_risk_tier", lambda *_a: "low")
    monkeypatch.setattr(quality_evidence, "derive_risk_signals", lambda *_a: [])
    monkeypatch.setattr(quality_evidence, "resolve_risk_profile", lambda *_a, **_k: {
        "requested_tier": "low", "effective_tier": "low", "signals": [],
        "required_reviewer_lenses": [], "combined_tree_required": False,
        "explicit_human_approval_required": False,
    })
    monkeypatch.setattr(
        quality_evidence, "repository_requires_explicit_human_approval", lambda _repo: False
    )
    for name, value in {
        "reviewer_evidence": [], "carried_reviewer_evidence": [],
        "bound_finding_dispositions": {}, "queued_reviewer_actions": [],
        "accept_gate_blockers": [], "server_derived_risk_tier": "low", "_base_ci_advisory": {},
    }.items():
        monkeypatch.setattr(accept_review, name, lambda *_a, _v=value, **_k: _v)
    monkeypatch.setattr(accept_review, "fold_accept_blockers", lambda **_k: {"blockers": []})
    manager = SimpleNamespace(
        repo=tmp_path, _show_task=lambda _tid: "",
        _context_write_intent_snapshot=lambda _rid: {"ok": False},
    )
    preview = accept_review.accept_preview(manager, "req-1", "T-1")
    assert preview["evaluated"] is True and preview["blocked"] is True
    [blocker] = [row for row in preview["blockers"] if row["kind"] == "neighbour_tests_failed"]
    assert blocker["kind"] in accept_review.ACCEPT_BLOCKER_KINDS
    assert _NODE in blocker["detail"]
    assert blocker["finalization_neighbour_tests"]["failed"] == [_NODE]


# --- accept: refuse from the record on the same tree, re-run on another --------------


def _terminal(neighbour: Any) -> dict[str, Any]:
    return {"evidence": _evidence(neighbour)}


def _union(monkeypatch: pytest.MonkeyPatch, identity: str, reuse: Any) -> tuple[Any, list[int]]:
    monkeypatch.setattr(validation, "combined_tree_identity", lambda *a, **k: identity)
    ran: list[int] = []

    def run_declared() -> list[dict[str, Any]]:
        ran.append(1)
        return [{"command": _DECLARED, "returncode": 0, "timed_out": False}]

    workspace = SimpleNamespace(request_id="req-1", repo="repo", base_oid="b" * 40)
    _rows, union_reuse = validation.run_union_validations_once(
        workspace, _AUTHORITY, {}, _CANDIDATE,
        combined_tree={"merged_paths": {}}, snapshot_base_oid="s" * 40,
        run_declared=run_declared, route_resolver=lambda _m: {}, reuse=reuse,
    )
    return union_reuse, ran


def test_accept_on_the_same_tree_refuses_from_the_recorded_red_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalizer_reuse = accept_review._finalizer_validation_reuse(
        _terminal(_red_record()), correction_record=None
    )
    union_reuse, ran = _union(monkeypatch, "id-1", finalizer_reuse)
    assert ran == []
    with pytest.raises(WorkspaceError) as caught:
        neighbours.refuse_recorded_red_neighbours(finalizer_reuse, union_reuse, _SELECTION)
    message = str(caught.value)
    assert message.startswith(neighbours.NEIGHBOUR_FAILURE_PREFIX)
    detail = json.loads(message[len(neighbours.NEIGHBOUR_FAILURE_PREFIX):])
    assert detail["failed"] == [_NODE]
    assert detail["reused_from"] == neighbours.FINALIZATION_REUSE_SOURCE
    # A red record is never reused as green evidence.
    assert neighbours.reused_finalization_neighbours(
        finalizer_reuse, union_reuse, _SELECTION
    ) is None


def test_accept_on_a_different_tree_identity_runs_the_neighbours_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalizer_reuse = accept_review._finalizer_validation_reuse(
        _terminal(_red_record()), correction_record=None
    )
    union_reuse, ran = _union(monkeypatch, "id-2", finalizer_reuse)
    assert ran == [1]
    assert neighbours.refuse_recorded_red_neighbours(
        finalizer_reuse, union_reuse, _SELECTION
    ) is None


def test_green_record_neither_refuses_nor_stops_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    green = {"selected": list(_TESTS), "command": "c", "passed": True}
    finalizer_reuse = accept_review._finalizer_validation_reuse(
        _terminal(green), correction_record=None
    )
    union_reuse, _ran = _union(monkeypatch, "id-1", finalizer_reuse)
    assert neighbours.refuse_recorded_red_neighbours(
        finalizer_reuse, union_reuse, _SELECTION
    ) is None
    reused = neighbours.reused_finalization_neighbours(finalizer_reuse, union_reuse, _SELECTION)
    assert reused is not None and reused["reused_from"] == neighbours.FINALIZATION_REUSE_SOURCE


def test_accept_refuses_from_the_record_before_the_quality_gates() -> None:
    tree = ast.parse(Path(accept_review.__file__).read_text(encoding="utf-8"))
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "accept_review"
    )
    [call] = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith("refuse_recorded_red_neighbours")
    ]
    assert [ast.unparse(arg) for arg in call.args] == [
        "validation_reuse", "union_validation_reuse", "neighbour_selection",
    ]
    gates = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(
            "run_completion_quality_gate"
        ) and "combined_tree_scope" in ast.unparse(node)
    ]
    assert gates and call.lineno < min(node.lineno for node in gates)


# --- only a recorded test verdict refuses; an evidence-free refusal is unrunnable ----


@pytest.mark.parametrize(
    "record",
    [
        _red_record(failure_kind=neighbours.FAILURE_KIND_UNRUNNABLE, failed=[]),
        _red_record(failure_kind=neighbours.FAILURE_KIND_ENVIRONMENT, failed=[]),
        {"selected": list(_TESTS), "command": "c", "passed": False, "error": "runner crashed"},
    ],
    ids=["unrunnable", "environment_blocked", "legacy_without_failed_names"],
)
def test_red_record_without_a_test_verdict_lets_accept_rerun(
    monkeypatch: pytest.MonkeyPatch, record: dict[str, Any]
) -> None:
    finalizer_reuse = accept_review._finalizer_validation_reuse(
        _terminal(record), correction_record=None
    )
    union_reuse, _ran = _union(monkeypatch, "id-1", finalizer_reuse)
    assert neighbours.refuse_recorded_red_neighbours(
        finalizer_reuse, union_reuse, _SELECTION
    ) is None
    assert neighbours.reused_finalization_neighbours(
        finalizer_reuse, union_reuse, _SELECTION
    ) is None


def test_matched_red_test_failure_record_refuses_naming_the_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalizer_reuse = accept_review._finalizer_validation_reuse(
        _terminal(_red_record()), correction_record=None
    )
    union_reuse, _ran = _union(monkeypatch, "id-1", finalizer_reuse)
    with pytest.raises(WorkspaceError) as caught:
        neighbours.refuse_recorded_red_neighbours(finalizer_reuse, union_reuse, _SELECTION)
    message = str(caught.value)
    assert message.startswith("neighbour_tests_failed")
    assert _NODE in message


def test_refusal_naming_no_failed_test_is_unrunnable_not_a_test_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = SimpleNamespace(request_id="req-1", path=tmp_path)

    def fake_run_neighbour_tests(
        selection: Any, card: Any, *, run: Any, workspace: Any, route_metadata: Any, clock: Any
    ) -> dict[str, Any]:
        run(workspace, {}, {})
        raise WorkspaceError(
            neighbours.NEIGHBOUR_FAILURE_PREFIX
            + json.dumps({"tests": list(selection.tests), "failed": [], "first_failure": "none"})
        )

    monkeypatch.setattr(neighbours, "run_neighbour_tests", fake_run_neighbour_tests)
    recorded = neighbours.run_finalization_neighbours(
        tmp_path, _CANDIDATE, {}, [_DECLARED],
        run=lambda _ws, _authority, _meta: [{"command": "n", **_RED}],
        workspace=workspace, route_metadata={},
    )
    assert recorded is not None and recorded["passed"] is False
    assert recorded["failed"] == []
    assert recorded["failure_kind"] == neighbours.FAILURE_KIND_UNRUNNABLE == "unrunnable"
    exc = neighbours.finalization_neighbour_refusal(recorded, [{"command": _DECLARED}])
    assert exc is not None and not isinstance(exc, ValidationRunError)
