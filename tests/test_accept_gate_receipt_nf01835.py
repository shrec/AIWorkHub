"""Accept gate: a detached recipe run bound to the exact candidate (NF-2026-01835)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import aiworkhub.core as core
from aiworkhub import accept_gate as ag
from aiworkhub import manager_recipe_tools as mrt
from aiworkhub import process_launcher_accept_review as par
from aiworkhub import recipe_runner as rr
from aiworkhub import review_orchestrator
from aiworkhub import tool_recipes as tr
from aiworkhub import tool_recipes_store as store
from aiworkhub import worker_workspace

TASK = "TASK-1"
REQ = "req-abc123"
CAND = "c" * 64
PARAMS = {"mode": "ok"}
_REAL_POPEN = subprocess.Popen


def _recipe() -> tr.Recipe:
    return tr.Recipe(
        id="example.gate",
        version="1.0.0",
        purpose="A runnable probe used only by these tests.",
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            tr.ParamSpec(
                name="mode",
                type=tr.ParamType.ENUM,
                required=True,
                values=("ok", "other"),
            ),
        ),
        outputs=(tr.OutputSpec(name="report", type=tr.OutputType.STDOUT),),
        argv=(tr.lit("python"), tr.lit("probe.py"), tr.slot("mode")),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "probe.py").write_text('print("repo")\n', encoding="utf-8")
    return tmp_path


@pytest.fixture
def recipe(repo: Path) -> tr.Recipe:
    stored = _recipe()
    store.put_recipe(repo, stored)
    return stored


def _policy(repo: Path, **fields) -> None:
    body = {"recipe_id": "example.gate", "version": "1.0.0", "params": PARAMS}
    body.update(fields)
    path = repo.joinpath(*ag.POLICY_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _seed(
    repo: Path,
    recipe: tr.Recipe,
    *,
    state: str = "completed",
    returncode: int = 0,
    exit_status: str = tr.EXIT_STATUS_COMPLETED,
    pid: int | None = None,
    params: dict | None = None,
    queued_at: str | None = None,
    candidate: str = CAND,
) -> str:
    """A run record plus the receipt naming it, as ``start_accept_run`` leaves them."""
    run_id = uuid.uuid4().hex
    used = dict(PARAMS if params is None else params)
    record = {
        "schema_id": rr.DETACHED_SCHEMA_ID,
        "run_id": run_id,
        "state": state,
        "repository": str(repo),
        "recipe_id": recipe.id,
        "recipe_version": recipe.version,
        "recipe_digest": recipe.digest,
        "params": used,
        "queued_at": queued_at or rr._utcnow_iso(),
        "accept_binding": {
            "task_id": TASK,
            "request_id": REQ,
            "candidate_sha256": candidate,
        },
    }
    if pid is not None:
        record["pid"] = pid
    if state == "completed":
        record["digest"] = {"returncode": returncode, "exit_status": exit_status}
    rr._write_status(rr._status_path(repo, run_id), record)
    rr._write_status(
        ag.receipt_path(repo, REQ),
        {
            "schema_id": ag.RECEIPT_SCHEMA_ID,
            "task_id": TASK,
            "request_id": REQ,
            "run_id": run_id,
            "candidate_sha256": candidate,
            "recipe_id": recipe.id,
            "recipe_version": recipe.version,
            "recipe_digest": recipe.digest,
            "params_sha256": ag.params_sha256(used),
        },
    )
    return run_id


def _blocker(repo: Path, candidate: str = CAND):
    return ag.gate_blocker(repo, task_id=TASK, request_id=REQ, candidate_sha256=candidate)


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def test_no_policy_means_no_gate(repo, recipe, monkeypatch):
    monkeypatch.setattr(par, "_target_candidate_sha256", lambda card: CAND)
    assert ag.load_policy(repo) is None
    assert _blocker(repo) is None
    assert par.accept_gate_blockers(repo, task_id=TASK, request_id=REQ, card={}) == []


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[]",
        json.dumps({"version": "1.0.0"}),
        json.dumps({"recipe_id": "  "}),
        json.dumps({"recipe_id": "example.gate", "params": []}),
        json.dumps({"recipe_id": "does.not.exist"}),
        json.dumps({"recipe_id": "example.gate", "version": "9.9.9"}),
    ],
)
def test_malformed_or_unresolvable_policy_is_invalid(repo, recipe, raw):
    path = repo.joinpath(*ag.POLICY_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")

    blocker = _blocker(repo)

    assert blocker is not None
    assert blocker["kind"] == "gate_policy_invalid"
    assert blocker["error"].startswith("gate_policy_invalid:")


# ---------------------------------------------------------------------------
# The receipt predicate
# ---------------------------------------------------------------------------


def test_missing_receipt(repo, recipe):
    _policy(repo)
    blocker = _blocker(repo)
    assert blocker["kind"] == "gate_receipt_missing"


def test_pending_run_blocks_with_pending(repo, recipe):
    _policy(repo)
    _seed(repo, recipe, state="running", pid=os.getpid())
    assert _blocker(repo)["kind"] == "gate_receipt_pending"


def test_queued_run_without_a_pid_is_pending_only_inside_the_spawn_grace(repo, recipe):
    _policy(repo)
    _seed(repo, recipe, state="queued")
    assert _blocker(repo)["kind"] == "gate_receipt_pending"

    _seed(repo, recipe, state="queued", queued_at="2020-01-01T00:00:00+00:00")
    assert _blocker(repo)["kind"] == "gate_receipt_stale"


def test_stale_candidate(repo, recipe):
    _policy(repo)
    _seed(repo, recipe)
    assert _blocker(repo) is None
    blocker = _blocker(repo, candidate="d" * 64)
    assert blocker["kind"] == "gate_receipt_stale"
    assert "candidate_changed" in blocker["error"]


def test_stale_params(repo, recipe):
    _policy(repo)
    _seed(repo, recipe, params={"mode": "other"})
    blocker = _blocker(repo)
    assert blocker["kind"] == "gate_receipt_stale"
    assert "params_changed" in blocker["error"]


def test_a_run_binding_to_another_candidate_is_stale(repo, recipe):
    _policy(repo)
    run_id = _seed(repo, recipe)
    path = rr._status_path(repo, run_id)
    record = rr._load_status(path)
    record["accept_binding"]["candidate_sha256"] = "e" * 64
    rr._write_status(path, record)
    assert _blocker(repo)["kind"] == "gate_receipt_stale"


def test_lost_run_is_stale(repo, recipe):
    _policy(repo)
    _seed(repo, recipe, state="running", pid=_dead_pid())
    blocker = _blocker(repo)
    assert blocker["kind"] == "gate_receipt_stale"
    assert "run_lost" in blocker["error"]


def test_receipt_naming_an_unknown_run_is_missing(repo, recipe):
    _policy(repo)
    run_id = _seed(repo, recipe)
    rr._status_path(repo, run_id).unlink()
    assert _blocker(repo)["kind"] == "gate_receipt_missing"


@pytest.mark.parametrize(
    "fields",
    [
        {"returncode": 1},
        {"returncode": 0, "exit_status": tr.EXIT_STATUS_TIMEOUT},
        {"state": "failed"},
        {"state": "refused"},
    ],
)
def test_red_run(repo, recipe, fields):
    _policy(repo)
    _seed(repo, recipe, **fields)
    assert _blocker(repo)["kind"] == "gate_receipt_red"


def test_green_run_passes(repo, recipe):
    _policy(repo)
    _seed(repo, recipe)
    assert _blocker(repo) is None


def test_a_recipe_changed_after_queueing_is_stale(repo, recipe):
    _policy(repo)
    run_id = _seed(repo, recipe, state="failed")
    path = rr._status_path(repo, run_id)
    record = rr._load_status(path)
    record["reason"] = rr.REASON_RECIPE_DIGEST_CHANGED
    rr._write_status(path, record)
    assert _blocker(repo)["kind"] == "gate_receipt_stale"


def test_preview_and_accept_share_one_blocker_kind(repo, recipe, monkeypatch):
    monkeypatch.setattr(par, "_target_candidate_sha256", lambda card: CAND)
    _policy(repo)
    for kind in ag.GATE_BLOCKER_KINDS:
        assert kind in par.ACCEPT_BLOCKER_KINDS

    blockers = par.accept_gate_blockers(repo, task_id=TASK, request_id=REQ, card={})
    assert [b["kind"] for b in blockers] == ["gate_receipt_missing"]
    assert blockers[0]["error"].startswith("gate_receipt_missing:")

    _seed(repo, recipe, returncode=2)
    red = par.accept_gate_blockers(repo, task_id=TASK, request_id=REQ, card={})
    assert [b["kind"] for b in red] == ["gate_receipt_red"]
    assert red[0]["error"] == f"gate_receipt_red:{red[0]['detail']}"

    _seed(repo, recipe)
    assert par.accept_gate_blockers(repo, task_id=TASK, request_id=REQ, card={}) == []


def test_preview_and_accept_both_call_the_gate():
    source = Path(par.__file__).read_text(encoding="utf-8")
    assert source.count("accept_gate_blockers(") >= 3


# ---------------------------------------------------------------------------
# aiworkhub_manager_recipe_run with the accept binding
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(repo, monkeypatch):
    route = {
        "role": "manager",
        "provider": "claude",
        "repo": str(repo),
        "manager_route": {"thread_id": "sess-gate", "provider": "claude"},
    }
    monkeypatch.setattr(core, "manager_bootstrap", lambda: route)
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    return repo


@pytest.fixture
def candidate(repo, monkeypatch):
    """A review_ready target whose candidate bytes hash to ``CAND``."""
    state = {"sha": CAND}
    monkeypatch.setattr(
        ag,
        "_load_candidate",
        lambda root, task_id: ({"card": task_id}, SimpleNamespace(path=str(repo)), ["x.py"]),
    )
    monkeypatch.setattr(ag, "_candidate_sha256", lambda card: state["sha"])
    monkeypatch.setattr(
        review_orchestrator,
        "target_identity_from_card",
        lambda card: {"target_task_id": TASK, "target_request_id": REQ},
    )
    import aiworkhub.process_launcher as pl

    monkeypatch.setattr(pl, "_canonical_task_status", lambda card: "review_ready")
    return state


@pytest.fixture
def no_spawn(monkeypatch):
    spawned: list[object] = []

    def fake_popen(*args, **kwargs):
        spawned.append(args)
        return SimpleNamespace(pid=0)

    monkeypatch.setattr(rr.subprocess, "Popen", fake_popen)
    return spawned


def test_one_accept_id_alone_is_refused(manager, recipe, no_spawn):
    _policy(manager)
    for fields in (
        {"accept_task_id": TASK},
        {"accept_request_id": REQ},
        {"accept_task_id": TASK, "accept_request_id": "  "},
    ):
        result = mrt.run(recipe_id="example.gate", params=PARAMS, **fields)
        assert result["ok"] is False
        assert result["reason_code"] == "accept_binding_incomplete"
        assert result["error"].startswith("accept_binding_incomplete")
    assert no_spawn == []
    assert not manager.joinpath(*ag.RECEIPT_REL).exists()


def test_bound_run_must_equal_the_policy(manager, recipe, candidate, no_spawn):
    _policy(manager)
    result = mrt.run(
        recipe_id="example.gate",
        params={"mode": "other"},
        accept_task_id=TASK,
        accept_request_id=REQ,
    )
    assert result["ok"] is False
    assert result["reason_code"] == "gate_policy_mismatch"
    assert no_spawn == []


def test_bound_run_without_a_policy_is_refused(manager, recipe, candidate, no_spawn):
    result = mrt.run(
        recipe_id="example.gate",
        params=PARAMS,
        accept_task_id=TASK,
        accept_request_id=REQ,
    )
    assert result["ok"] is False
    assert result["reason_code"] == "gate_policy_invalid"
    assert no_spawn == []


def test_bound_run_needs_the_write_gate(manager, recipe, candidate, no_spawn, monkeypatch):
    _policy(manager)
    monkeypatch.setattr(core, "writes_allowed", lambda: False)
    result = mrt.run(
        recipe_id="example.gate",
        params=PARAMS,
        accept_task_id=TASK,
        accept_request_id=REQ,
    )
    assert result["error"] == "write_gate_closed"
    assert no_spawn == []


def test_bound_run_needs_a_review_ready_target(manager, recipe, candidate, no_spawn, monkeypatch):
    _policy(manager)
    import aiworkhub.process_launcher as pl

    monkeypatch.setattr(pl, "_canonical_task_status", lambda card: "running")
    result = mrt.run(
        recipe_id="example.gate",
        params=PARAMS,
        accept_task_id=TASK,
        accept_request_id=REQ,
    )
    assert result["ok"] is False
    assert result["reason_code"] == "accept_gate_target_not_review_ready"
    assert no_spawn == []


def test_bound_run_returns_at_once_writes_the_receipt_and_refuses_a_second(
    manager, recipe, candidate, no_spawn
):
    _policy(manager)

    first = mrt.run(
        recipe_id="example.gate",
        params=PARAMS,
        accept_task_id=TASK,
        accept_request_id=REQ,
    )

    assert first["ok"] is True, first
    assert first["state"] == "queued"
    assert len(no_spawn) == 1
    receipt = json.loads(ag.receipt_path(manager, REQ).read_text(encoding="utf-8"))
    assert receipt["task_id"] == TASK
    assert receipt["request_id"] == REQ
    assert receipt["run_id"] == first["run_id"]
    assert receipt["candidate_sha256"] == CAND
    assert receipt["recipe_id"] == "example.gate"
    assert receipt["recipe_version"] == "1.0.0"
    assert receipt["recipe_digest"] == recipe.digest
    assert receipt["params_sha256"] == ag.params_sha256(PARAMS)
    assert receipt["created_at"]
    assert receipt["actor"] == {"provider": "claude", "session_id": "sess-gate"}
    record = rr.read_detached_status(manager, first["run_id"])
    assert record["accept_binding"] == {
        "task_id": TASK,
        "request_id": REQ,
        "candidate_sha256": CAND,
    }
    assert _blocker(manager)["kind"] == "gate_receipt_pending"

    second = mrt.run(
        recipe_id="example.gate",
        params=PARAMS,
        accept_task_id=TASK,
        accept_request_id=REQ,
    )

    assert second["ok"] is False
    assert second["reason_code"] == "accept_gate_run_in_progress"
    assert len(no_spawn) == 1
    assert ag.receipt_path(manager, REQ).read_text(encoding="utf-8") == json.dumps(
        receipt, ensure_ascii=False, indent=1, sort_keys=True
    )


def test_an_unbound_detached_run_is_unchanged(manager, recipe, no_spawn):
    started = mrt.run(recipe_id="example.gate", params=PARAMS, detach=True)
    assert started["ok"] is True
    record = rr.read_detached_status(manager, started["run_id"])
    assert "accept_binding" not in record
    assert not manager.joinpath(*ag.RECEIPT_REL).exists()


# ---------------------------------------------------------------------------
# The detached child on the combined candidate tree
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_tree(tmp_path, monkeypatch):
    """Replace the combined-tree builder with a directory factory."""
    calls = {"built": [], "cleaned": []}

    def build(workspace, card, changed):
        tree = tmp_path / f"combined-{len(calls['built'])}"
        tree.mkdir()
        (tree / "probe.py").write_text('print("tree")\n', encoding="utf-8")
        calls["built"].append(tree)
        union = SimpleNamespace(path=str(tree), repo=str(tmp_path), home=str(tmp_path / "home"))
        return union, tree

    def cleanup(repo, path, home):
        calls["cleaned"].append(Path(path))
        shutil.rmtree(path)

    monkeypatch.setattr(worker_workspace, "create_combined_validation_workspace", build)
    monkeypatch.setattr(worker_workspace, "cleanup_workspace", cleanup)
    return calls


def test_detached_child_runs_on_the_combined_tree_and_removes_it(
    manager, recipe, candidate, no_spawn, fake_tree, monkeypatch
):
    _policy(manager)
    started = mrt.run(
        recipe_id="example.gate",
        params=PARAMS,
        accept_task_id=TASK,
        accept_request_id=REQ,
    )
    assert started["ok"] is True, started
    assert _blocker(manager)["kind"] == "gate_receipt_pending"

    monkeypatch.setattr(rr.subprocess, "Popen", _REAL_POPEN)
    assert rr._detached_main(["--detached-run", started["status_path"]]) == 0

    record = rr.read_detached_status(manager, started["run_id"])
    assert record["state"] == "completed", record
    assert record["digest"]["returncode"] == 0
    (tree,) = fake_tree["built"]
    assert record["digest"]["cwd"] == str(tree)
    assert "tree" in record["digest"]["stdout_tail"]
    assert "repo" not in record["digest"]["stdout_tail"]
    assert fake_tree["cleaned"] == [tree]
    assert not tree.exists()
    assert _blocker(manager) is None
    assert _blocker(manager, candidate="d" * 64)["kind"] == "gate_receipt_stale"


def test_a_candidate_changed_since_queueing_fails_the_child_without_a_tree(
    manager, recipe, candidate, no_spawn, fake_tree, monkeypatch
):
    _policy(manager)
    started = mrt.run(
        recipe_id="example.gate",
        params=PARAMS,
        accept_task_id=TASK,
        accept_request_id=REQ,
    )
    candidate["sha"] = "f" * 64

    monkeypatch.setattr(rr.subprocess, "Popen", _REAL_POPEN)
    assert rr._detached_main([started["status_path"]]) == 1

    record = rr.read_detached_status(manager, started["run_id"])
    assert record["state"] == "failed"
    assert "candidate_changed_since_queued" in record["error"]
    assert fake_tree["built"] == []
    assert _blocker(manager, candidate="f" * 64)["kind"] == "gate_receipt_stale"


def test_the_tree_is_removed_even_when_the_run_raises(
    repo, candidate, fake_tree
):
    binding = {"task_id": TASK, "request_id": REQ, "candidate_sha256": CAND}

    def boom(tree: Path):
        assert tree.is_dir()
        raise RuntimeError("suite crashed")

    with pytest.raises(RuntimeError, match="suite crashed"):
        ag.run_on_candidate_tree(repo, binding, boom)

    (tree,) = fake_tree["built"]
    assert fake_tree["cleaned"] == [tree]
    assert not tree.exists()


def test_an_incomplete_binding_never_builds_a_tree(repo, candidate, fake_tree):
    with pytest.raises(ag.AcceptGateError, match="accept_binding_incomplete"):
        ag.run_on_candidate_tree(repo, {"task_id": TASK}, lambda tree: None)
    assert fake_tree["built"] == []


def test_start_detached_run_refuses_an_incomplete_binding(repo, recipe, no_spawn):
    with pytest.raises(rr.RecipeRunError) as caught:
        rr.start_detached_run(repo, recipe, PARAMS, accept_binding={"task_id": TASK})
    assert caught.value.reason == rr.REASON_ACCEPT_BINDING_INCOMPLETE
    assert no_spawn == []


def test_unbound_detached_child_keeps_the_repository_cwd(
    repo, recipe, no_spawn, fake_tree, monkeypatch
):
    started = rr.start_detached_run(repo, recipe, PARAMS)
    monkeypatch.setattr(rr.subprocess, "Popen", _REAL_POPEN)
    assert rr._detached_main([started["status_path"]]) == 0

    record = rr.read_detached_status(repo, started["run_id"])
    assert record["state"] == "completed", record
    assert record["digest"]["cwd"] == str(repo.resolve())
    assert "repo" in record["digest"]["stdout_tail"]
    assert fake_tree["built"] == []


# ---------------------------------------------------------------------------
# run_recipe
# ---------------------------------------------------------------------------


def test_run_recipe_cwd_defaults_to_the_repository_root(repo, recipe):
    digest = rr.run_recipe(repo, recipe, PARAMS)
    assert digest["cwd"] == str(repo.resolve())
    assert digest["returncode"] == 0
    assert "repo" in digest["stdout_tail"]


def test_run_recipe_cwd_overrides_only_the_working_directory(repo, recipe, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "probe.py").write_text('print("elsewhere")\n', encoding="utf-8")

    digest = rr.run_recipe(repo, recipe, PARAMS, cwd=other)

    assert digest["cwd"] == str(other)
    assert "elsewhere" in digest["stdout_tail"]
    assert Path(digest["output_path"]).is_relative_to(repo.resolve())


def test_max_timeout_is_two_hours():
    assert rr.MAX_TIMEOUT_SECONDS == 7200.0
