"""NF-2026-01030: a manager may re-MEASURE a candidate in the host lane.

The measured problem: a candidate whose declared validations fail only because
the Windows AppContainer validation lane cannot execute them (symlink WinError
1314, SemLock/fork-pool multiprocessing, dedicated-subprocess builds) ends
``validation_failed`` or ``finalize_failed``, and from there nothing the hub
measures can move it -- ``quality_reviewer_launch`` wants review_ready,
``accept_preview``/``accept_review`` want review_ready, ``retry_finalization``
refused every validation failure except ``validation_exec_scratch_unavailable``
and re-ran ``finalize_failed`` in the SAME sandbox lane, and the accept-time
revalidation routed through that lane too. Managers closed such cards by hand
integration plus supersede.

``validation_runner.row_restriction`` deliberately cannot attribute an in-test
failure to the environment, so the fix is a MEASUREMENT in another lane and
never a classification of candidate output. These tests pin exactly that: the
default lane is byte-for-byte unchanged, the opt-in is explicit, the retained
candidate seal is verified before anything transitions, the forced route is the
existing no-sandbox backend token, and every receipt row carries the lane plus
a digest of the lane it left.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    process_launcher,
    process_launcher_accept_review,
    process_launcher_validation as plv,
    worker_workspace,
)

_REQUEST_ID = "d" * 32
_TASK_ID = "TASK_NF01030"
_RUNNER = "claude_worker_nf01030"
_TOPIC = "review-gate"
_CANDIDATE = "out/result.json"
_CANDIDATE_BYTES = b'{"nf01030": true}\n'
_PRIOR_BACKEND = "windows_appcontainer"
_VALIDATION_COMMAND = f"{sys.executable} -m compileall -q src"
_PRIOR_ROWS = [
    {
        "command": _VALIDATION_COMMAND,
        "returncode": 1,
        "timed_out": False,
        "execution_boundary": _PRIOR_BACKEND,
    }
]


@pytest.fixture(autouse=True)
def _isolate_runtime_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Audit and toolchain-authority state stay inside this test's tmp_path."""
    monkeypatch.setenv(
        "AIWORKHUB_AUDIT_LOG_PATH",
        str(tmp_path / "isolated-runtime" / "process_logs" / "audit.jsonl"),
    )
    monkeypatch.setenv(
        "AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY", "hex:" + ("5a" * 32)
    )
    monkeypatch.setattr(
        process_launcher.storage_retention,
        "schedule_repository_cleanup",
        lambda *_args, **_kwargs: None,
    )


def _plan(argv, repo):
    def build(**_):
        return SimpleNamespace(
            argv=list(argv), cwd=str(repo), launchable=True, reason=""
        )

    return build


class _Harness:
    """One terminalized, workspace-retained request plus its sealed card."""

    def __init__(self, manager, metadata_path, workspace, card, transitions, finalized):
        self.manager = manager
        self.metadata_path = metadata_path
        self.workspace = workspace
        self.card = card
        self.transitions = transitions
        self.finalized = finalized

    @property
    def metadata_on_disk(self) -> dict:
        return json.loads(self.metadata_path.read_text(encoding="utf-8"))

    @property
    def latest_event(self) -> dict:
        return self.manager._request_events(_REQUEST_ID)[-1]


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    terminal_state: str = "validation_failed",
    terminal_error: str = "validation_failed:python -m pytest:rc=1",
    seal: bool = True,
    finalized_state: str = "review_ready",
    finalized_error: str = "",
    # Declared in scope but NOT sealed: the test writes it after the seal.
    extra_allowed_writes: tuple[str, ...] = (),
) -> _Harness:
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    monkeypatch.setattr(
        process_launcher.claude_auth,
        "auth_status",
        lambda: {"launchable": True, "blocker_reason": ""},
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    card: dict = {
        "task_id": _TASK_ID,
        "runner": _RUNNER,
        "topic": _TOPIC,
        "status": "review",
        "worker_status": "review",
        "claimed_by": _RUNNER,
        "allowed_writes": [_CANDIDATE, *extra_allowed_writes],
        "priority": "high",
    }

    def show_task(task_id: str):
        assert task_id == _TASK_ID
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=show_task,
        collision_guard=lambda **_: {
            "returncode": 0,
            "stdout": '{"collision_free":true}',
            "stderr": "",
        },
        adapter_builder=_plan([sys.executable, "-c", "pass"], repo),
        isolation_enabled=False,
    )

    worktree_root = tmp_path / "worktrees"
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(worktree_root))
    workspace_path = worktree_root / _REQUEST_ID / "worktree"
    home_path = worktree_root / _REQUEST_ID / "home"
    (workspace_path / "out").mkdir(parents=True)
    home_path.mkdir()
    (workspace_path / _CANDIDATE).write_bytes(_CANDIDATE_BYTES)
    workspace = worker_workspace.WorkerWorkspace(
        request_id=_REQUEST_ID,
        repo=manager.repo,
        path=workspace_path,
        home=home_path,
        allowed_writes=(_CANDIDATE, *extra_allowed_writes),
        parent_baseline={},
        workspace_baseline={},
    )

    status_path = manager.process_dir / f"{_REQUEST_ID}.supervisor.json"
    metadata_path = manager.process_dir / f"{_REQUEST_ID}.request.json"
    worker_workspace.write_json_0600(status_path, {"state": "exited", "exit_code": 0})
    metadata = {
        "request_id": _REQUEST_ID,
        "task_id": _TASK_ID,
        "runner": _RUNNER,
        "topic": _TOPIC,
        "adapter_id": "claude_cli",
        "sandbox_backend": _PRIOR_BACKEND,
        "supervisor_status_path": str(status_path),
        "workspace": workspace.as_metadata(),
        "validation": [_VALIDATION_COMMAND],
        "allowed_writes": [_CANDIDATE, *extra_allowed_writes],
    }
    worker_workspace.write_json_0600(metadata_path, metadata)

    evidence: dict = {
        "request_id": _REQUEST_ID,
        "changed_paths": [_CANDIDATE],
        "validation": _PRIOR_ROWS,
        "request_identity": {
            "request_id": _REQUEST_ID,
            "task_id": _TASK_ID,
            "runner": _RUNNER,
            "topic": _TOPIC,
        },
    }
    if seal:
        evidence["changed_path_hashes"] = dict(
            process_launcher._changed_path_hashes(workspace, [_CANDIDATE])
        )
    card["terminal_review"] = {"substatus": terminal_state, "evidence": evidence}

    manager._append_event(
        {
            "request_id": _REQUEST_ID,
            "task_id": _TASK_ID,
            "runner": _RUNNER,
            "topic": _TOPIC,
            "adapter_id": "claude_cli",
            "sandbox_backend": _PRIOR_BACKEND,
            "state": terminal_state,
            "error": terminal_error,
            "validation": _PRIOR_ROWS,
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
            "workspace_retained": True,
        }
    )

    transitions: list = []
    monkeypatch.setattr(
        process_launcher.task_engine,
        "retry_finalize_failed",
        lambda *args, **kwargs: transitions.append((args, kwargs))
        or {"ok": True, "stderr": ""},
    )
    finalized: list = []

    def finalize(request_id_arg, supervisor_returncode=None):
        assert supervisor_returncode == 0
        finalized.append(
            {
                "event": manager._request_events(request_id_arg)[-1],
                "metadata": json.loads(metadata_path.read_text(encoding="utf-8")),
            }
        )
        return {
            "request_id": request_id_arg,
            "task_id": _TASK_ID,
            "state": finalized_state,
            "workspace_retained": True,
            "error": finalized_error,
        }

    monkeypatch.setattr(manager, "_finalize_isolated_request", finalize)
    return _Harness(manager, metadata_path, workspace, card, transitions, finalized)


# --------------------------------------------------------------------------- #
# the default lane is unchanged
# --------------------------------------------------------------------------- #


def test_without_the_lane_a_product_validation_failure_is_still_refused(
    monkeypatch, tmp_path
):
    harness = _harness(monkeypatch, tmp_path)
    before = harness.metadata_path.read_bytes()

    result = harness.manager.retry_finalization(_REQUEST_ID, _TASK_ID)

    assert result == {
        "ok": False,
        "request_id": _REQUEST_ID,
        "task_id": _TASK_ID,
        "error": "request_not_retryable_finalization_failure:validation_failed",
    }
    assert harness.transitions == [] and harness.finalized == []
    assert harness.metadata_path.read_bytes() == before
    assert harness.latest_event["state"] == "validation_failed"


def test_an_unsupported_lane_is_refused_by_name(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path)
    before = harness.metadata_path.read_bytes()

    result = harness.manager.retry_finalization(
        _REQUEST_ID, _TASK_ID, validation_lane="host"
    )

    assert result["ok"] is False
    assert result["error"] == "validation_lane_unsupported:host"
    assert harness.transitions == [] and harness.finalized == []
    assert harness.metadata_path.read_bytes() == before


@pytest.mark.parametrize("state", ["cancelled", "worker_failed", "review_ready"])
def test_other_states_keep_todays_refusal_even_with_the_lane(
    monkeypatch, tmp_path, state
):
    harness = _harness(monkeypatch, tmp_path, terminal_state=state, terminal_error="")

    result = harness.manager.retry_finalization(
        _REQUEST_ID, _TASK_ID, validation_lane="manager_host"
    )

    assert result["error"] == f"request_not_retryable_finalization_failure:{state}"
    assert harness.transitions == [] and harness.finalized == []


def test_a_request_without_the_lane_leaves_every_receipt_row_unstamped(
    monkeypatch, tmp_path
):
    rows = _run_declared_validations(monkeypatch, tmp_path, lane_metadata={})

    assert rows == [{"command": _VALIDATION_COMMAND, "returncode": 0,
                     "behavioral_role": "generic"}]


# --------------------------------------------------------------------------- #
# the forced route
# --------------------------------------------------------------------------- #


def test_the_host_lane_forces_the_existing_no_sandbox_backend_token(monkeypatch):
    """The lane reuses the one host backend token; it resolves no sandbox."""

    def refuse() -> str:
        raise worker_workspace.WorkspaceError(
            "windows_appcontainer_sandbox_unavailable:probe"
        )

    monkeypatch.setattr(process_launcher, "select_sandbox_backend", refuse)

    route = process_launcher._validation_route_kwargs(
        {
            "adapter_id": "claude_cli",
            "sandbox_backend": _PRIOR_BACKEND,
            "validation_lane": "manager_host",
        }
    )

    assert route == {
        "backend": plv.HOST_VALIDATION_LANE_BACKEND,
        "adapter_id": "claude_cli",
    }
    assert plv.HOST_VALIDATION_LANE_BACKEND == (
        worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND
    )


def test_without_the_lane_the_route_is_still_the_launch_bound_backend(monkeypatch):
    monkeypatch.setattr(
        process_launcher, "select_sandbox_backend", lambda: _PRIOR_BACKEND
    )

    assert process_launcher._validation_route_kwargs(
        {"adapter_id": "claude_cli", "sandbox_backend": _PRIOR_BACKEND}
    ) == {"backend": _PRIOR_BACKEND, "adapter_id": "claude_cli"}

    with pytest.raises(worker_workspace.WorkspaceError, match="backend_mismatch"):
        process_launcher._validation_route_kwargs(
            {"adapter_id": "claude_cli", "sandbox_backend": "landlock"}
        )


def test_accept_time_revalidation_follows_the_lane_recorded_on_the_request(
    monkeypatch,
):
    """``accept_review`` routes through the latest event, which carries the lane.

    All three accept-time revalidations pass that event as the validation route
    metadata, and the finalization receipt stamps the recorded lane onto it --
    so a host-lane request revalidates in the host lane and every other request
    keeps the adapter sandbox backend.
    """
    source = inspect.getsource(process_launcher_accept_review)
    assert source.count(", card, latest") >= 3
    assert "host_lane_receipt_stamp(metadata)" in inspect.getsource(
        process_launcher.ProcessManager._finalize_isolated_request
    )

    monkeypatch.setattr(
        process_launcher, "select_sandbox_backend", lambda: _PRIOR_BACKEND
    )
    terminal_event = {
        "adapter_id": "claude_cli",
        "sandbox_backend": _PRIOR_BACKEND,
        "state": "review_ready",
        **plv.host_lane_receipt_stamp(
            {"validation_lane": "manager_host", "prior_validation_lane": _PRIOR_BACKEND}
        ),
    }

    assert process_launcher._validation_route_kwargs(terminal_event)["backend"] == (
        plv.HOST_VALIDATION_LANE_BACKEND
    )
    assert process_launcher._validation_route_kwargs(
        {k: v for k, v in terminal_event.items() if k != "validation_lane"}
    )["backend"] == _PRIOR_BACKEND


# --------------------------------------------------------------------------- #
# the measurement
# --------------------------------------------------------------------------- #


def _run_declared_validations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, lane_metadata: dict
) -> list[dict]:
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace_path = tmp_path / "wt" / _REQUEST_ID / "worktree"
    home_path = tmp_path / "wt" / _REQUEST_ID / "home"
    workspace_path.mkdir(parents=True)
    home_path.mkdir()
    workspace = worker_workspace.WorkerWorkspace(
        request_id=_REQUEST_ID,
        repo=repo,
        path=workspace_path,
        home=home_path,
        allowed_writes=(_CANDIDATE,),
        parent_baseline={},
        workspace_baseline={},
    )
    observed: list[dict] = []

    def fake_run_validations(target, commands, **kwargs):
        observed.append(dict(kwargs))
        return [{"command": command, "returncode": 0} for command in commands]

    monkeypatch.setattr(process_launcher, "run_validations", fake_run_validations)
    monkeypatch.setattr(
        process_launcher, "select_sandbox_backend", lambda: _PRIOR_BACKEND
    )
    route_metadata = {
        "adapter_id": "claude_cli",
        "sandbox_backend": _PRIOR_BACKEND,
        **lane_metadata,
    }
    rows = process_launcher._run_declared_validations(
        workspace, {"validation": [_VALIDATION_COMMAND]}, route_metadata
    )
    expected_backend = (
        plv.HOST_VALIDATION_LANE_BACKEND
        if lane_metadata.get("validation_lane") == "manager_host"
        else _PRIOR_BACKEND
    )
    assert observed == [{"backend": expected_backend, "adapter_id": "claude_cli"}]
    return rows


def test_every_host_lane_receipt_row_carries_the_lane_and_the_prior_digest(
    monkeypatch, tmp_path
):
    digest = plv.prior_lane_failure_digest(_PRIOR_ROWS, backend=_PRIOR_BACKEND)

    rows = _run_declared_validations(
        monkeypatch,
        tmp_path,
        lane_metadata={
            "validation_lane": "manager_host",
            "prior_validation_lane": _PRIOR_BACKEND,
            "prior_lane_failure_digest": digest,
        },
    )

    assert rows == [
        {
            "command": _VALIDATION_COMMAND,
            "returncode": 0,
            "behavioral_role": "generic",
            "validation_lane": "manager_host",
            "prior_validation_lane": _PRIOR_BACKEND,
            "prior_lane_failure_digest": digest,
        }
    ]


def test_the_prior_lane_digest_covers_only_the_failing_rows(monkeypatch, tmp_path):
    passing = {"command": "other", "returncode": 0}
    assert plv.prior_lane_failure_digest(
        [*_PRIOR_ROWS, passing], backend=_PRIOR_BACKEND
    ) == plv.prior_lane_failure_digest(_PRIOR_ROWS, backend=_PRIOR_BACKEND)
    assert plv.prior_lane_failure_digest(
        _PRIOR_ROWS, backend="landlock"
    ) != plv.prior_lane_failure_digest(_PRIOR_ROWS, backend=_PRIOR_BACKEND)


# --------------------------------------------------------------------------- #
# the host-lane retry itself
# --------------------------------------------------------------------------- #


def test_a_sealed_validation_failure_reaches_review_ready_in_the_host_lane(
    monkeypatch, tmp_path
):
    harness = _harness(monkeypatch, tmp_path)

    result = harness.manager.retry_finalization(
        _REQUEST_ID, _TASK_ID, validation_lane="manager_host"
    )

    assert result["ok"] is True, result
    assert result["state"] == "review_ready"
    assert result["provider_relaunched"] is False
    assert harness.transitions and harness.transitions[0][0][1:4] == (
        _TASK_ID,
        _RUNNER,
        _REQUEST_ID,
    )
    expected = {
        "validation_lane": "manager_host",
        "prior_validation_lane": _PRIOR_BACKEND,
        "prior_lane_failure_digest": plv.prior_lane_failure_digest(
            _PRIOR_ROWS, backend=_PRIOR_BACKEND
        ),
    }
    # The lane is persisted on the request, so the rerun AND the accept-time
    # revalidation resolve the same route.
    persisted = harness.finalized[0]["metadata"]
    assert {key: persisted[key] for key in expected} == expected
    assert harness.metadata_on_disk["sandbox_backend"] == _PRIOR_BACKEND
    finalizing = harness.finalized[0]["event"]
    assert finalizing["state"] == "finalizing"
    assert finalizing["finalization_retry"] is True
    assert {key: finalizing[key] for key in expected} == expected


def test_a_real_failure_in_the_host_lane_ends_validation_failed_again(
    monkeypatch, tmp_path
):
    harness = _harness(
        monkeypatch,
        tmp_path,
        finalized_state="validation_failed",
        finalized_error="validation_failed:host-lane:rc=1",
    )

    result = harness.manager.retry_finalization(
        _REQUEST_ID, _TASK_ID, validation_lane="manager_host"
    )

    assert result["ok"] is False
    assert result["state"] == "validation_failed"
    assert result["error"] == "validation_failed:host-lane:rc=1"
    # It still ran: the measurement happened, in the host lane.
    assert harness.finalized[0]["metadata"]["validation_lane"] == "manager_host"


def test_a_finalize_failed_request_is_retryable_in_the_host_lane(monkeypatch, tmp_path):
    harness = _harness(
        monkeypatch,
        tmp_path,
        terminal_state="finalize_failed",
        terminal_error="finalize_failed:validation_unsupported_in_sandbox:semlock",
    )

    result = harness.manager.retry_finalization(
        _REQUEST_ID, _TASK_ID, validation_lane="manager_host"
    )

    assert result["ok"] is True, result
    assert harness.finalized[0]["metadata"]["validation_lane"] == "manager_host"


# --------------------------------------------------------------------------- #
# the seal is the authority for re-measuring these exact bytes
# --------------------------------------------------------------------------- #


def test_drifted_retained_bytes_are_refused_with_the_state_unchanged(
    monkeypatch, tmp_path
):
    harness = _harness(monkeypatch, tmp_path)
    before = harness.metadata_path.read_bytes()
    (harness.workspace.path / _CANDIDATE).write_bytes(b"rewritten by something else\n")

    result = harness.manager.retry_finalization(
        _REQUEST_ID, _TASK_ID, validation_lane="manager_host"
    )

    assert result["ok"] is False
    assert result["error"] == (
        f"{plv.HOST_LANE_SEAL_UNVERIFIED}:retained_bytes_changed:{_CANDIDATE}"
    )
    assert harness.transitions == [] and harness.finalized == []
    assert harness.metadata_path.read_bytes() == before
    assert harness.latest_event["state"] == "validation_failed"


def test_an_unsealed_candidate_is_refused_rather_than_measured(monkeypatch, tmp_path):
    harness = _harness(monkeypatch, tmp_path, seal=False)

    result = harness.manager.retry_finalization(
        _REQUEST_ID, _TASK_ID, validation_lane="manager_host"
    )

    assert result["error"] == f"{plv.HOST_LANE_SEAL_UNVERIFIED}:seal_missing"
    assert harness.transitions == [] and harness.finalized == []


def test_a_seal_recorded_for_another_request_never_authorizes_this_one(
    monkeypatch, tmp_path
):
    harness = _harness(monkeypatch, tmp_path)
    harness.card["terminal_review"]["evidence"]["request_identity"]["request_id"] = (
        "f" * 32
    )
    harness.card["terminal_review"]["evidence"]["request_id"] = "f" * 32

    result = harness.manager.retry_finalization(
        _REQUEST_ID, _TASK_ID, validation_lane="manager_host"
    )

    assert result["error"] == f"{plv.HOST_LANE_SEAL_UNVERIFIED}:seal_missing"
    assert harness.transitions == [] and harness.finalized == []


def test_a_seal_whose_hashes_were_refused_is_not_a_pass(monkeypatch, tmp_path):
    """``changed_path_hashes`` carries ``None`` for a path it could not hash."""
    harness = _harness(monkeypatch, tmp_path)
    harness.card["terminal_review"]["evidence"]["changed_path_hashes"] = {
        _CANDIDATE: None
    }

    result = harness.manager.retry_finalization(
        _REQUEST_ID, _TASK_ID, validation_lane="manager_host"
    )

    assert result["error"] == f"{plv.HOST_LANE_SEAL_UNVERIFIED}:seal_incomplete"
    assert harness.transitions == [] and harness.finalized == []


def test_the_seal_is_read_from_the_terminal_failure_envelope_too(monkeypatch, tmp_path):
    harness = _harness(
        monkeypatch, tmp_path, terminal_state="finalize_failed", terminal_error="x"
    )
    harness.card["terminal_failure"] = harness.card.pop("terminal_review")

    result = harness.manager.retry_finalization(
        _REQUEST_ID, _TASK_ID, validation_lane="manager_host"
    )

    assert result["ok"] is True, result


def test_a_path_changed_after_the_seal_is_refused_as_unsealed(monkeypatch, tmp_path):
    """A file the retained worktree started changing later is not the candidate.

    ``changed_path_hashes`` covers only what was already changed when the seal
    was taken, so a new in-scope file written afterwards is invisible to the
    per-path comparison. Re-measuring then would measure bytes the seal cannot
    speak for, which is drift and is refused by name with the state unchanged.
    """
    extra = "out/written-after-the-seal.json"
    harness = _harness(monkeypatch, tmp_path, extra_allowed_writes=(extra,))
    before = harness.metadata_path.read_bytes()
    (harness.workspace.path / extra).write_bytes(b'{"sealed": false}\n')

    result = harness.manager.retry_finalization(
        _REQUEST_ID, _TASK_ID, validation_lane="manager_host"
    )

    assert result["ok"] is False
    assert result["error"] == f"{plv.HOST_LANE_SEAL_UNVERIFIED}:unsealed_paths:{extra}"
    assert harness.transitions == [] and harness.finalized == []
    assert harness.metadata_path.read_bytes() == before
    assert harness.latest_event["state"] == "validation_failed"


def test_an_unreadable_current_changed_set_refuses_rather_than_measures(
    monkeypatch, tmp_path
):
    """Fail closed: an unreadable drift check is never a pass."""
    harness = _harness(monkeypatch, tmp_path)

    def denied(_workspace):
        raise OSError("changed-path enumeration denied")

    monkeypatch.setattr(process_launcher, "changed_allowed_write_paths", denied)

    result = harness.manager.retry_finalization(
        _REQUEST_ID, _TASK_ID, validation_lane="manager_host"
    )

    assert result["error"] == (
        f"{plv.HOST_LANE_SEAL_UNVERIFIED}:changed_paths_unreadable:OSError"
    )
    assert harness.transitions == [] and harness.finalized == []


def test_host_lane_seal_refusal_names_current_paths_the_seal_never_covered():
    """The unit contract: current minus sealed is drift, and the set is required."""
    seal = {"changed_path_hashes": {_CANDIDATE: "a" * 64}}

    def observed(paths):
        return {path: "a" * 64 for path in paths}

    assert plv.host_lane_seal_refusal(seal, observed, [_CANDIDATE]) == ""
    assert plv.host_lane_seal_refusal(
        seal, observed, ["out/b.json", _CANDIDATE, "out/a.json"]
    ) == f"{plv.HOST_LANE_SEAL_UNVERIFIED}:unsealed_paths:out/a.json,out/b.json"
    # Required, not defaulted: no caller can skip the drift check by omission.
    with pytest.raises(TypeError):
        plv.host_lane_seal_refusal(seal, observed)


# --------------------------------------------------------------------------- #
# the lane vocabulary itself
# --------------------------------------------------------------------------- #


def test_only_two_lanes_exist_and_the_stamp_is_empty_outside_the_host_lane():
    assert plv.VALIDATION_LANES == ("", "manager_host")
    assert plv.normalized_validation_lane(None) == ""
    assert plv.normalized_validation_lane(" manager_host ") == "manager_host"
    with pytest.raises(worker_workspace.WorkspaceError, match="validation_lane_unsupported"):
        plv.normalized_validation_lane("appcontainer")
    assert plv.host_lane_receipt_stamp({}) == {}
    assert plv.host_lane_receipt_stamp({"validation_lane": ""}) == {}
    assert plv.HOST_LANE_RETRYABLE_STATES == {"validation_failed", "finalize_failed"}
