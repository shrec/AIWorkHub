"""NF-2026-02240: coordinator-run host validation stage on a full candidate tree."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import host_validation_stage as hvs
from aiworkhub import hub_resource_queue as hrq
from aiworkhub import task_store

REQUEST = "req_0123456789abcdef"
TASK = "TASK_NF02240"
ALLOWED = ("src/mod.py", "src/generated.py", "tests/test_mod.py")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _alive(_pid: int) -> bool:
    return True


def _never(_changes, _event):
    raise AssertionError("reseal must not be reached")


class _FakeTree:
    """A directory tree whose delta is measured against its creation snapshot."""

    def __init__(self, root: Path, files: dict[str, bytes]) -> None:
        self.root = root
        for relative, data in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.baseline = self._hashes()
        self.cleaned = False

    def _hashes(self) -> dict[str, str]:
        return {
            path.relative_to(self.root).as_posix(): _sha(path.read_bytes())
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def changed(self) -> list[str]:
        now = self._hashes()
        return sorted(
            relative
            for relative in set(now) | set(self.baseline)
            if now.get(relative) != self.baseline.get(relative)
        )

    def cleanup(self) -> None:
        self.cleaned = True
        shutil.rmtree(self.root, ignore_errors=True)

    def handle(self) -> hvs.TreeHandle:
        return hvs.TreeHandle(
            path=self.root, record={"fake": True}, changed=self.changed, cleanup=self.cleanup
        )


def _pytest_output(failed: list[str], passed: int) -> str:
    lines = ["." * passed + "F" * len(failed)]
    lines += [f"FAILED {node} - assert 0" for node in failed]
    parts = ([f"{len(failed)} failed"] if failed else []) + ([f"{passed} passed"] if passed else [])
    lines.append(", ".join(parts) + " in 0.01s")
    return "\n".join(lines) + "\n"


class _Runner:
    """Fake host runner: host commands write files, pytest reports scripted failures."""

    def __init__(self, cand_fail=(), base_fail=(), command_rc=0, writes=None):
        self.cand_fail, self.base_fail = set(cand_fail), set(base_fail)
        self.command_rc = command_rc
        self.writes = writes or {}
        self.calls: list[tuple[list[str], Path]] = []
        self.base_root: Path | None = None

    def __call__(self, argv, cwd):
        argv = list(argv)
        self.calls.append((argv, Path(cwd)))
        if argv[1:3] == ["-m", "pytest"]:
            tests = [a for a in argv[3:] if not a.startswith("-") and a != "no:cacheprovider"]
            red = self.base_fail if Path(cwd) == self.base_root else self.cand_fail
            failed = sorted(t for t in tests if t in red)
            return {
                "argv": argv,
                "returncode": 1 if failed else 0,
                "timed_out": False,
                "stdout_tail": _pytest_output(failed, len(tests) - len(failed)),
                "stderr_tail": "",
            }
        for relative, data in self.writes.items():
            target = Path(cwd) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return {"argv": argv, "returncode": self.command_rc, "timed_out": False, "stdout_tail": "ok"}


def _db(tmp_path: Path, changed_hashes: dict[str, str]) -> Path:
    db = tmp_path / "runtime" / "task_queue.sqlite"
    db.parent.mkdir(parents=True)
    card = {
        "task_id": TASK,
        "allowed_writes": list(ALLOWED),
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {"request_id": REQUEST, "task_id": TASK},
                "changed_path_hashes": changed_hashes,
                "changed_paths": sorted(changed_hashes),
            },
        },
    }
    conn = sqlite3.connect(db)
    try:
        conn.executescript(task_store.SCHEMA)
        conn.execute(
            "INSERT INTO tasks (task_id, card_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (TASK, json.dumps(card), "t0", "t0"),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _setup(tmp_path: Path, **runner_kwargs):
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    mod = b"VALUE = 2\n"
    (candidate_root / "src").mkdir()
    (candidate_root / "src" / "mod.py").write_bytes(mod)
    canonical = {"src/mod.py": b"VALUE = 1\n", "tests/test_mod.py": b"def test_a(): pass\n"}
    cand_tree = _FakeTree(tmp_path / "cand_tree", canonical)
    (cand_tree.root / "src" / "mod.py").write_bytes(mod)  # candidate overlay
    base_trees: list[_FakeTree] = []
    runner = _Runner(**runner_kwargs)

    def build_base() -> hvs.TreeHandle:
        tree = _FakeTree(tmp_path / f"base_tree{len(base_trees)}", canonical)
        base_trees.append(tree)
        runner.base_root = tree.root
        return tree.handle()

    ops = hvs.TreeOps(build_candidate=cand_tree.handle, build_base=build_base)
    db = _db(tmp_path, {"src/mod.py": _sha(mod)})
    return candidate_root, cand_tree, base_trees, runner, ops, db


def _run(tmp_path, spec, ops, runner, candidate_root, db, **kwargs):
    return hvs.run_host_stage(
        repo_root=tmp_path,
        request_id=REQUEST,
        task_id=TASK,
        spec=spec,
        tree_ops=ops,
        base_oid="b" * 40,
        candidate_root=candidate_root,
        allowed_writes=ALLOWED,
        db_path=db,
        runner=runner,
        python="python",
        cpu_capacity=kwargs.pop("cpu_capacity", 8),
        resource_kwargs={"is_alive": _alive, "poll_seconds": 0.01},
        **kwargs,
    )


def _spec(card):
    spec = hvs.parse_host_spec(card)
    assert spec is not None
    return spec


# ------------------------------------------------------------------ opt-in


def test_a_card_without_the_new_fields_is_unchanged(tmp_path):
    card = {"task_id": TASK, "validation": ["git diff --check"]}
    assert hvs.card_requests_host_stage(card) is False
    assert hvs.parse_host_spec(card) is None
    assert hvs.card_host_fields(None, None) == {}
    assert hvs.accept_gate(tmp_path, card, REQUEST) == ""
    # workspace/evidence are never touched for a card without the stage.
    assert hvs.accept_step(tmp_path, card, REQUEST, TASK, None, {}) == ""
    assert hvs.review_packet_section(tmp_path, REQUEST) is None
    assert not (tmp_path / ".aiworkhub").exists()


# -------------------------------------------------------------------- spec


def test_spec_accepts_argv_lists_and_a_bounded_sweep():
    fields = hvs.card_host_fields(
        [["python", "-m", "pytest", "-q", "tests/test_mod.py"], ["git", "diff", "--check"]],
        {"tests": ["tests/test_mod.py::test_a", "tests/test_other.py"], "processes": 4},
    )
    assert fields["host_commands"][1] == ["git", "diff", "--check"]
    assert fields["host_sweep"] == {
        "tests": ["tests/test_mod.py::test_a", "tests/test_other.py"],
        "processes": 4,
    }


@pytest.mark.parametrize(
    ("commands", "sweep", "code"),
    [
        ("git diff", None, "host_commands_invalid"),
        ([[]], None, "host_command_invalid:0:argv"),
        ([["git", "diff --check"]], None, "host_command_invalid:0:argv"),
        ([["git"], [1]], None, "host_command_invalid:1:argv"),
        ([["x"]] * 17, None, "host_commands_invalid"),
        (None, {"tests": [], "processes": 1}, "host_sweep_tests_invalid"),
        (None, {"tests": ["tests/a.py"], "processes": 0}, "host_sweep_processes_invalid"),
        (None, {"tests": ["tests/a.py"], "processes": True}, "host_sweep_processes_invalid"),
        (None, {"tests": ["tests/a.py"], "processes": 1, "env": {}}, "host_sweep_invalid"),
        (None, {"tests": ["../a.py"], "processes": 1}, "host_sweep_test_invalid:0"),
        (None, {"tests": ["/abs/a.py"], "processes": 1}, "host_sweep_test_invalid:0"),
        (None, {"tests": ["C:/a.py"], "processes": 1}, "host_sweep_test_invalid:0"),
        (None, {"tests": ["-k"], "processes": 1}, "host_sweep_test_invalid:0"),
        (None, {"tests": ["@x"], "processes": 1}, "host_sweep_test_invalid:0"),
        (None, {"tests": ["a.py", "a.py"], "processes": 1}, "host_sweep_tests_duplicate"),
        ([], None, "host_stage_empty"),
    ],
)
def test_bad_host_fields_are_refused_by_name(commands, sweep, code):
    with pytest.raises(hvs.HostStageError, match=code):
        hvs.card_host_fields(commands, sweep)


@pytest.mark.parametrize(("cores", "bound"), [(1, 1), (2, 1), (4, 3), (8, 6), (16, 12), (64, 48)])
def test_sweep_processes_are_bounded_by_cores_with_headroom(cores, bound):
    assert hvs.sweep_process_bound(cores) == bound


def test_default_bound_follows_the_observed_core_count(monkeypatch):
    from aiworkhub import parallelism

    monkeypatch.setattr(parallelism, "get_cpu_capacity", lambda: 12)
    assert hvs.sweep_process_bound() == 9


# ------------------------------------------------------------------- stage


def test_the_stage_runs_commands_then_the_sweep_and_joins_generated_files(tmp_path):
    generated = b"GENERATED = True\n"
    candidate_root, cand_tree, base_trees, runner, ops, db = _setup(
        tmp_path, writes={"src/generated.py": generated, "build/out.bin": b"\x00"}
    )
    spec = _spec(
        {
            "host_commands": [["python", "-m", "tools.gen"], ["git", "diff", "--check"]],
            "host_sweep": {"tests": [f"tests/test_mod.py::test_{i}" for i in range(5)], "processes": 64},
        }
    )
    result = _run(tmp_path, spec, ops, runner, candidate_root, db, cpu_capacity=4)

    assert result["state"] == "passed", result
    assert [row["argv"][2] for row in result["host_commands"]] == ["tools.gen", "--check"]
    # Generated inside allowed_writes joins the candidate; outside is only reported.
    assert result["generated"]["joined"] == {"src/generated.py": _sha(generated)}
    assert result["generated"]["outside_allowed_writes"] == ["build/out.bin"]
    assert (candidate_root / "src" / "generated.py").read_bytes() == generated
    assert not (candidate_root / "build").exists()
    # The sweep ran sharded on the candidate tree with the bounded process count.
    sweep = result["sweep"]
    assert (sweep["requested_processes"], sweep["process_bound"], sweep["processes"]) == (64, 3, 3)
    pytest_calls = [c for c in runner.calls if c[0][1:3] == ["-m", "pytest"]]
    assert len(pytest_calls) == 3 and {c[1] for c in pytest_calls} == {cand_tree.root}
    assert sweep["comparison"]["outcome"] == "sweep_passed"
    assert base_trees == []  # a green sweep never builds a base tree
    # Sealed hashes were re-recorded with an audit event.
    conn = sqlite3.connect(db)
    try:
        card = json.loads(conn.execute("SELECT card_json FROM tasks").fetchone()[0])
        events = conn.execute("SELECT event, payload_json FROM task_events").fetchall()
    finally:
        conn.close()
    evidence = card["terminal_review"]["evidence"]
    assert evidence["changed_path_hashes"]["src/generated.py"] == _sha(generated)
    assert evidence["changed_paths"] == ["src/generated.py", "src/mod.py"]
    assert [event for event, _ in events] == [hvs.GENERATED_JOINED_EVENT]
    payload = json.loads(events[0][1])
    assert payload["previous"] == {} and payload["previously_absent"] == ["src/generated.py"]
    # The ready tree is reported and the resource was released.
    tree = hvs.candidate_tree(tmp_path, REQUEST)
    assert tree["ok"] and tree["ready"] and tree["path"] == str(cand_tree.root)
    assert hrq.status(db)["resources"][0]["holder"] is None
    # The review packet carries the stage as its own section.
    section = hvs.review_packet_section(tmp_path, REQUEST, {"host_sweep": {}})
    assert section["schema_id"] == hvs.SECTION_SCHEMA_ID and section["state"] == "passed"
    assert section["candidate_tree"]["ready"] is True
    assert hvs.accept_gate(tmp_path, {"host_sweep": {}}, REQUEST) == ""


def test_a_sweep_test_red_on_base_is_not_counted_against_the_candidate(tmp_path):
    red = "tests/test_mod.py::test_1"
    candidate_root, _tree, base_trees, runner, ops, db = _setup(
        tmp_path, cand_fail=[red], base_fail=[red]
    )
    spec = _spec({"host_sweep": {"tests": [f"tests/test_mod.py::test_{i}" for i in range(4)], "processes": 2}})
    result = _run(tmp_path, spec, ops, runner, candidate_root, db)

    comparison = result["sweep"]["comparison"]
    assert comparison["outcome"] == "baseline_no_new_failures"
    assert comparison["pre_existing"] == [red] and comparison["new_count"] == 0
    assert result["state"] == "passed"
    assert len(base_trees) == 1 and base_trees[0].cleaned
    base_calls = [c for c in runner.calls if c[1] == base_trees[0].root]
    assert base_calls and base_calls[0][0][-1] == red  # only the failing id ran on base


def test_a_new_sweep_failure_fails_the_stage(tmp_path):
    new, old = "tests/test_mod.py::test_0", "tests/test_mod.py::test_1"
    candidate_root, _tree, _bases, runner, ops, db = _setup(
        tmp_path, cand_fail=[new, old], base_fail=[old]
    )
    spec = _spec({"host_sweep": {"tests": [new, old], "processes": 1}})
    result = _run(tmp_path, spec, ops, runner, candidate_root, db)

    comparison = result["sweep"]["comparison"]
    assert comparison["outcome"] == "baseline_new_failures"
    assert comparison["new_failures"] == [new] and comparison["pre_existing"] == [old]
    assert result["state"] == "failed"
    assert hvs.accept_gate(tmp_path, {"host_sweep": {}}, REQUEST) == "host_validation_stage_failed"


def test_a_failing_test_absent_on_base_is_new_without_a_base_run(tmp_path):
    new = "tests/test_brand_new.py::test_x"
    candidate_root, _tree, base_trees, runner, ops, db = _setup(tmp_path, cand_fail=[new])
    spec = _spec({"host_sweep": {"tests": [new], "processes": 1}})
    result = _run(tmp_path, spec, ops, runner, candidate_root, db)

    assert result["sweep"]["comparison"]["new_failures"] == [new]
    assert result["state"] == "failed"
    assert not [c for c in runner.calls if c[1] == base_trees[0].root]


def test_an_unparseable_failing_shard_fails_closed(tmp_path):
    candidate_root, _tree, _bases, _runner, ops, db = _setup(tmp_path)

    def broken(argv, cwd):
        return {"argv": list(argv), "returncode": 2, "timed_out": False, "stdout_tail": "boom"}

    spec = _spec({"host_sweep": {"tests": ["tests/test_mod.py"], "processes": 1}})
    result = _run(tmp_path, spec, ops, broken, candidate_root, db)
    assert result["sweep"]["comparison"]["outcome"] == "sweep_unparsed"
    assert result["state"] == "failed"


def test_a_failing_host_command_stops_and_joins_nothing(tmp_path):
    candidate_root, _tree, _bases, runner, ops, db = _setup(
        tmp_path, command_rc=3, writes={"src/generated.py": b"x\n"}
    )
    spec = _spec(
        {
            "host_commands": [["python", "-m", "tools.gen"], ["git", "diff", "--check"]],
            "host_sweep": {"tests": ["tests/test_mod.py"], "processes": 1},
        }
    )
    result = _run(tmp_path, spec, ops, runner, candidate_root, db)

    assert result["state"] == "failed"
    assert len(result["host_commands"]) == 1 and result["host_commands"][0]["returncode"] == 3
    assert result["generated"]["joined"] == {}
    assert result["generated"]["not_joined"] == ["src/generated.py"]
    assert result["sweep"] is None
    assert not (candidate_root / "src" / "generated.py").exists()


def test_a_tree_build_failure_is_recorded_as_a_named_error(tmp_path):
    candidate_root, _tree, _bases, runner, _ops, db = _setup(tmp_path)

    def explode() -> hvs.TreeHandle:
        raise OSError("disk full")

    ops = hvs.TreeOps(build_candidate=explode, build_base=explode)
    spec = _spec({"host_commands": [["git", "diff", "--check"]]})
    result = _run(tmp_path, spec, ops, runner, candidate_root, db)

    assert result["state"] == "error" and result["error"] == "OSError:disk full"
    assert hvs.candidate_tree(tmp_path, REQUEST)["state"] == "failed"
    assert hvs.accept_gate(tmp_path, {"host_commands": []}, REQUEST) == "host_validation_stage_error"
    assert hrq.status(db)["resources"][0]["holder"] is None


def test_two_host_stages_on_cpu_heavy_queue_fifo(tmp_path):
    candidate_root, _tree, _bases, runner, ops, db = _setup(tmp_path)
    holder = hrq.acquire(db, hrq.CPU_HEAVY, "other-request", max_wait_seconds=1, is_alive=_alive)
    seen: list[dict] = []

    def observing_runner(argv, cwd):
        seen.append(hrq.status(db)["resources"][0])
        return runner(argv, cwd)

    import threading

    spec = _spec({"host_commands": [["git", "diff", "--check"]]})
    worker = threading.Thread(
        target=_run, args=(tmp_path, spec, ops, observing_runner, candidate_root, db)
    )
    worker.start()
    import time

    deadline = time.monotonic() + 5
    while not hrq.status(db)["resources"][0]["waiters"] and time.monotonic() < deadline:
        time.sleep(0.01)
    queued = hrq.status(db)["resources"][0]
    assert queued["holder"]["owner"] == "other-request"
    assert [w["owner"] for w in queued["waiters"]] == [REQUEST]
    assert seen == []  # nothing ran while the other stage held cpu_heavy
    hrq.release(db, holder.entry_id)
    worker.join(10)
    assert seen and seen[0]["holder"]["owner"] == REQUEST
    assert hvs.read_result(tmp_path, REQUEST)["state"] == "passed"


# ------------------------------------------------------------------ gates & views


def test_accept_gate_fails_closed_on_missing_unreadable_or_foreign_results(tmp_path):
    card = {"host_commands": [["git", "diff", "--check"]]}
    assert hvs.accept_gate(tmp_path, card, REQUEST) == "host_validation_stage_missing"
    directory = hvs.stage_dir(tmp_path, REQUEST)
    directory.mkdir(parents=True)
    (directory / hvs.RESULT_FILE).write_text("{not json", encoding="utf-8")
    assert hvs.accept_gate(tmp_path, card, REQUEST) == "host_validation_stage_unreadable"
    (directory / hvs.RESULT_FILE).write_text(
        json.dumps({"request_id": "someone_else", "state": "passed"}), encoding="utf-8"
    )
    assert hvs.accept_gate(tmp_path, card, REQUEST) == "host_validation_stage_identity_mismatch"
    assert hvs.accept_gate(tmp_path, card, "../escape") == "host_validation_stage_unreadable"


def test_candidate_tree_reports_absence_and_invalid_ids(tmp_path):
    assert hvs.candidate_tree(tmp_path, REQUEST)["error"] == "candidate_tree_not_found"
    assert hvs.candidate_tree(tmp_path, "../x")["error"] == "host_stage_request_id_invalid"
    directory = hvs.stage_dir(tmp_path, REQUEST)
    directory.mkdir(parents=True)
    (directory / hvs.TREE_FILE).write_text(
        json.dumps({"state": "ready", "path": str(tmp_path / "gone")}), encoding="utf-8"
    )
    view = hvs.candidate_tree(tmp_path, REQUEST)
    assert view["state"] == "missing_on_disk" and view["ready"] is False


def test_generated_partition_refuses_deletions_and_escapes(tmp_path):
    joinable, outside, refused = hvs.partition_generated(
        {"src/mod.py": None, "src/generated.py": "a" * 64, "docs/x.md": "b" * 64}, ALLOWED
    )
    assert joinable == {"src/generated.py": "a" * 64}
    assert outside == ["docs/x.md"]
    assert refused == {"src/mod.py": "deletion_not_joined"}
    with pytest.raises(hvs.HostStageError, match="host_generated_path_invalid"):
        hvs.join_and_reseal(tmp_path, tmp_path, {"../evil.py": "c" * 64}, reseal=_never)


def test_run_argv_runs_a_real_process_with_bounded_tails(tmp_path):
    row = hvs.run_argv([sys.executable, "-c", "print('host-stage-ok')"], tmp_path)
    assert row["returncode"] == 0 and row["timed_out"] is False
    assert "host-stage-ok" in row["stdout_tail"] and row["stdout_truncated"] is False
    missing = hvs.run_argv([str(tmp_path / "no-such-binary")], tmp_path)
    assert missing["returncode"] is None and missing["spawn_error"]


def test_card_fields_and_manager_read_tools_are_wired(tmp_path):
    import inspect

    from aiworkhub import core, server

    for function in (core.create_task, server.aiworkhub_task_create):
        assert {"host_commands", "host_sweep"} <= set(inspect.signature(function).parameters)
    assert callable(server.aiworkhub_manager_resource_status)
    assert callable(server.aiworkhub_manager_candidate_tree)
    # Outside a ready canonical repository the status reply is a named refusal.
    reply = hrq.status_reply(tmp_path)
    assert reply["ok"] is False and reply["readonly"] is True and reply["error"]


# ------------------------------------------------- rework: lane runner, async accept

CARD = {"task_id": TASK, "host_commands": [["git", "diff", "--check"]]}
EVIDENCE = {"changed_path_hashes": {"src/mod.py": "0" * 64}}


def _workspace(tmp_path: Path, candidate_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        base_oid="b" * 40,
        path=candidate_root,
        allowed_writes=ALLOWED,
        repo=tmp_path,
        home=tmp_path / "home",
    )


def _poll(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return bool(predicate())


class _BlockingStage:
    """Stands in for run_host_stage: records its call and blocks until released."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.release = threading.Event()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.release.wait(10)
        return {"state": "passed"}


@pytest.fixture
def staged_accept(tmp_path, monkeypatch):
    db = tmp_path / "runtime.sqlite"
    monkeypatch.setattr(hvs.hub_resource_queue, "default_db_path", lambda _root: db)
    stage = _BlockingStage()
    monkeypatch.setattr(hvs, "run_host_stage", stage)
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    yield stage, _workspace(tmp_path, candidate_root)
    stage.release.set()
    marker = hvs.stage_dir(tmp_path, REQUEST) / hvs.RUNNING_FILE
    assert _poll(lambda: not marker.exists())


def test_a_pytest_host_command_runs_with_the_lane_interpreter_safe_path_and_tree_pythonpath(
    tmp_path, monkeypatch
):
    # The hub's ambient secrets and pytest options never reach a host command.
    monkeypatch.setenv("AIWH_NF02240_SECRET_TOKEN", "do-not-leak")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--definitely-not-an-option")
    tree = tmp_path / "tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "probe_nf02240.py").write_text("WHERE = __file__\n", encoding="utf-8")
    (tree / "tests" / "test_probe.py").write_text(
        "import os\n"
        "import sys\n"
        "import probe_nf02240\n"
        f"TREE = {str(tree)!r}\n"
        "def real(path):\n"
        "    return os.path.normcase(os.path.realpath(path))\n"
        "def test_lane():\n"
        "    assert sys.flags.safe_path\n"
        "    assert os.path.dirname(real(probe_nf02240.WHERE)) == real(TREE)\n"
        "    assert TREE in os.environ['PYTHONPATH'].split(os.pathsep)\n"
        "    assert 'AIWH_NF02240_SECRET_TOKEN' not in os.environ\n"
        "    assert os.environ['PYTHONDONTWRITEBYTECODE'] == '1'\n",
        encoding="utf-8",
    )
    workspace = SimpleNamespace(path=tree, repo=tree)
    declared = ["pytest", "-q", "-p", "no:cacheprovider", "tests/test_probe.py"]
    argv, env = hvs.validation_command(workspace, declared)
    assert Path(argv[0]).is_absolute() and argv[1:4] == ["-P", "-m", "pytest"]
    (basetemp,) = [token for token in argv if token.startswith("--basetemp=")]
    assert Path(basetemp.split("=", 1)[1]).parent.parent == tree / hvs.HOST_SCRATCH_DIR
    assert str(tree) in env["PYTHONPATH"].split(os.pathsep)
    row = hvs.host_runner(workspace)(declared, tree)
    output = (row.get("stdout_tail") or "")[-1500:] + (row.get("stderr_tail") or "")[-800:]
    assert row["returncode"] == 0, output
    assert row["declared_argv"] == declared and row["argv"][1:4] == ["-P", "-m", "pytest"]
    # The scratch is the stage's own, never reported as a generated file.
    assert hvs.detect_generated({}, tree, [f"{hvs.HOST_SCRATCH_DIR}/x/b/f.txt"]) == {}


def test_a_lane_build_failure_is_a_failed_row_naming_its_cause(tmp_path, monkeypatch):
    from aiworkhub import worker_workspace

    def refuse(*_args, **_kwargs):
        raise worker_workspace.WorkspaceError("validation_environment:interpreter_missing")

    monkeypatch.setattr(worker_workspace, "_normalize_validation_interpreter_argv", refuse)
    row = hvs.host_runner(SimpleNamespace(path=tmp_path, repo=tmp_path))(["git", "status"], tmp_path)
    assert row["returncode"] is None and "interpreter_missing" in row["argv_error"]
    assert hvs._row_summary(row)["argv_error"] == row["argv_error"]


def test_accept_starts_the_stage_in_the_background_and_returns_pending(tmp_path, staged_accept):
    stage, workspace = staged_accept
    started = time.monotonic()
    refusal = hvs.accept_step(tmp_path, CARD, REQUEST, TASK, workspace, EVIDENCE)
    assert refusal == hvs.PENDING_REFUSAL
    assert time.monotonic() - started < 5
    assert _poll(lambda: len(stage.calls) == 1)
    assert not stage.release.is_set()  # accept returned while the stage still runs
    call = stage.calls[0]
    assert call["candidate_root"] == workspace.path and call["marker_token"]
    assert call["db_path"] == tmp_path / "runtime.sqlite"
    assert hvs.running_marker_live(hvs.stage_dir(tmp_path, REQUEST))


def test_a_second_accept_while_the_stage_runs_starts_nothing(tmp_path, staged_accept):
    stage, workspace = staged_accept
    assert hvs.accept_step(tmp_path, CARD, REQUEST, TASK, workspace, EVIDENCE) == "host_validation_pending"
    assert _poll(lambda: len(stage.calls) == 1)
    for _ in range(3):
        assert hvs.accept_step(tmp_path, CARD, REQUEST, TASK, workspace, EVIDENCE) == "host_validation_pending"
    assert len(stage.calls) == 1


def test_a_transient_error_is_rerun_but_a_failure_is_final(tmp_path, staged_accept):
    stage, workspace = staged_accept
    directory = hvs.stage_dir(tmp_path, REQUEST)
    directory.mkdir(parents=True)
    result = directory / hvs.RESULT_FILE
    result.write_text(json.dumps({"request_id": REQUEST, "state": "failed"}), encoding="utf-8")
    assert hvs.accept_step(tmp_path, CARD, REQUEST, TASK, workspace, EVIDENCE) == "host_validation_stage_failed"
    assert stage.calls == []
    result.write_text(
        json.dumps({"request_id": REQUEST, "state": "error", "error": "ResourceWaitTimeout:x"}),
        encoding="utf-8",
    )
    assert hvs.accept_step(tmp_path, CARD, REQUEST, TASK, workspace, EVIDENCE) == "host_validation_pending"
    assert _poll(lambda: len(stage.calls) == 1)


def test_a_missing_workspace_or_unusable_runtime_db_is_a_named_refusal(tmp_path, monkeypatch):
    refusal = hvs.accept_step(tmp_path, CARD, REQUEST, TASK, None, EVIDENCE)
    assert refusal.startswith("host_validation_error:AttributeError")

    def unusable(_root):
        raise RuntimeError("storage_not_ready")

    monkeypatch.setattr(hvs.hub_resource_queue, "default_db_path", unusable)
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    workspace = _workspace(tmp_path, candidate_root)
    refusal = hvs.accept_step(tmp_path, CARD, REQUEST, TASK, workspace, EVIDENCE)
    assert refusal == "host_validation_error:RuntimeError:storage_not_ready"
    assert not (hvs.stage_dir(tmp_path, REQUEST) / hvs.RUNNING_FILE).exists()
    assert hvs.accept_step(tmp_path, CARD, REQUEST, TASK, workspace, "not evidence") == (
        "host_validation_stage_candidate_empty"
    )


def test_a_stale_running_marker_is_reclaimed_and_a_live_one_is_not(tmp_path):
    directory = hvs.stage_dir(tmp_path, REQUEST)
    held = hvs.claim_running(directory)
    assert held is not None
    assert hvs.claim_running(directory, is_alive=_alive) is None
    reclaimed = hvs.claim_running(directory, is_alive=lambda _pid: False)
    assert reclaimed not in (None, held)
    hvs.release_running(directory, held)  # no longer the owner: the marker stays
    assert (directory / hvs.RUNNING_FILE).exists()
    hvs.release_running(directory, reclaimed)
    assert not (directory / hvs.RUNNING_FILE).exists()


def test_a_direct_run_while_another_holds_the_marker_does_nothing(tmp_path):
    candidate_root, _tree, _bases, runner, ops, db = _setup(tmp_path)
    directory = hvs.stage_dir(tmp_path, REQUEST)
    held = hvs.claim_running(directory)
    result = _run(tmp_path, _spec(CARD), ops, runner, candidate_root, db)
    assert result["state"] == "pending" and runner.calls == []
    assert not (directory / hvs.RESULT_FILE).exists()
    hvs.release_running(directory, held)
    assert _run(tmp_path, _spec(CARD), ops, runner, candidate_root, db)["state"] == "passed"
    assert not (directory / hvs.RUNNING_FILE).exists()  # released in finally


def test_a_rerun_discards_the_previous_tree_before_building(tmp_path):
    candidate_root, cand_tree, _bases, runner, ops, db = _setup(tmp_path)
    old = tmp_path / "old_tree"
    old.mkdir()
    (old / "x.txt").write_text("x", encoding="utf-8")
    directory = hvs.stage_dir(tmp_path, REQUEST)
    directory.mkdir(parents=True)
    (directory / hvs.TREE_FILE).write_text(
        json.dumps({"state": "failed", "path": str(old), "record": {"fake": True}}), encoding="utf-8"
    )
    discarded: list[str] = []

    def discard(state):
        discarded.append(state["path"])
        shutil.rmtree(state["path"])

    def build():
        assert not old.exists()  # the old tree is gone before the new one is built
        return cand_tree.handle()

    ops = hvs.TreeOps(build_candidate=build, build_base=ops.build_base, discard=discard)
    result = _run(tmp_path, _spec(CARD), ops, runner, candidate_root, db)
    assert result["state"] == "passed", result
    assert discarded == [str(old)]
    assert result["previous_tree"] == {"path": str(old), "discarded": True}


def test_a_failed_reseal_leaves_no_unsealed_file_in_the_candidate(tmp_path, monkeypatch):
    candidate_root, _tree, _bases, runner, ops, db = _setup(
        tmp_path, writes={"src/generated.py": b"GENERATED = 1\n"}
    )

    def refuse(*_args, **_kwargs):
        raise hvs.HostStageError("host_reseal_sealed_evidence_mismatch")

    monkeypatch.setattr(hvs, "reseal_generated", refuse)
    result = _run(tmp_path, _spec({"host_commands": [["python", "-m", "tools.gen"]]}), ops, runner, candidate_root, db)
    assert result["state"] == "error" and "host_reseal_sealed_evidence_mismatch" in result["error"]
    left = sorted(path.relative_to(candidate_root).as_posix() for path in candidate_root.rglob("*"))
    assert left == ["src", "src/mod.py"]  # neither the file nor a staging copy
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_failed_move_restores_files_and_reverts_the_seal(tmp_path, monkeypatch):
    tree, candidate = tmp_path / "tree", tmp_path / "candidate"
    (tree / "pkg" / "new").mkdir(parents=True)
    (tree / "pkg" / "new" / "a.py").write_bytes(b"A\n")
    (tree / "b.py").write_bytes(b"B\n")
    candidate.mkdir()
    (candidate / "b.py").write_bytes(b"old\n")
    joinable = {"b.py": _sha(b"B\n"), "pkg/new/a.py": _sha(b"A\n")}
    calls: list[tuple[dict, str]] = []

    def reseal(changes, event, absent=()):
        calls.append((dict(changes), event, list(absent)))
        return {"previous": {"b.py": "f" * 64}, "previously_absent": ["pkg/new/a.py"]}

    real_replace = os.replace
    moves = {"count": 0}

    def flaky_replace(source, target):
        moves["count"] += 1
        if moves["count"] == 2:
            raise OSError("disk full")
        return real_replace(source, target)

    monkeypatch.setattr(hvs.os, "replace", flaky_replace)
    with pytest.raises(OSError, match="disk full"):
        hvs.join_and_reseal(candidate, tree, joinable, reseal=reseal)
    monkeypatch.undo()
    assert (candidate / "b.py").read_bytes() == b"old\n"
    assert sorted(p.relative_to(candidate).as_posix() for p in candidate.rglob("*")) == ["b.py"]
    assert calls == [
        (joinable, hvs.GENERATED_JOINED_EVENT, []),
        ({"b.py": "f" * 64}, hvs.GENERATED_REVERTED_EVENT, ["pkg/new/a.py"]),
    ]


def test_reseal_drops_a_reverted_path_from_the_sealed_evidence(tmp_path):
    mod = _sha(b"VALUE = 2\n")
    db = _db(tmp_path, {"src/mod.py": mod})
    joined = hvs.reseal_generated(db, TASK, REQUEST, {"src/generated.py": "a" * 64})
    assert joined["previous"] == {} and joined["previously_absent"] == ["src/generated.py"]
    hvs.reseal_generated(
        db, TASK, REQUEST, joined["previous"], event=hvs.GENERATED_REVERTED_EVENT,
        absent=joined["previously_absent"],
    )
    conn = sqlite3.connect(db)
    try:
        card = json.loads(conn.execute("SELECT card_json FROM tasks").fetchone()[0])
        events = [row[0] for row in conn.execute("SELECT event FROM task_events ORDER BY rowid")]
    finally:
        conn.close()
    evidence = card["terminal_review"]["evidence"]
    assert evidence["changed_path_hashes"] == {"src/mod.py": mod}
    assert evidence["changed_paths"] == ["src/mod.py"]
    assert events == [hvs.GENERATED_JOINED_EVENT, hvs.GENERATED_REVERTED_EVENT]


def test_the_review_section_needs_an_opted_in_card(tmp_path):
    assert hvs.review_packet_section(tmp_path, "", None) is None
    assert hvs.review_packet_section(tmp_path, "../x", {"task_id": TASK}) is None
    assert hvs.review_packet_section(tmp_path, REQUEST, {"validation": ["git diff"]}) is None
    assert hvs.review_packet_section(tmp_path, REQUEST, CARD) is None  # opted in, never ran
    assert hvs.review_packet_section(tmp_path, "../x", CARD)["state"] == "unreadable"
    assert hvs.task_card(tmp_path, "") is None
    assert hvs.task_card(tmp_path, TASK) is None  # no ready canonical store here


# ------------------------- rework 3: marker exclusivity, reseal, node ids, sanitized env


def test_two_concurrent_reclaims_of_a_stale_marker_leave_exactly_one_owner(tmp_path, monkeypatch):
    directory = hvs.stage_dir(tmp_path, REQUEST)
    directory.mkdir(parents=True)
    (directory / hvs.RUNNING_FILE).write_text(
        json.dumps({"pid": 2**30, "token": "stale"}), encoding="utf-8"
    )

    def slow_liveness(marker, _is_alive):
        # Widen judge-then-remove: unserialized, the second reclaimer would
        # delete the marker the first one has just created and claim as well.
        payload = hvs._read_json(marker)
        time.sleep(0.2)
        return payload is not None and payload.get("token") != "stale"

    monkeypatch.setattr(hvs, "_marker_live", slow_liveness)
    barrier = threading.Barrier(2)
    tokens: list[str | None] = []

    def claim() -> None:
        barrier.wait(5)
        tokens.append(hvs.claim_running(directory))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    winners = [token for token in tokens if token is not None]
    assert len(tokens) == 2 and len(winners) == 1, tokens
    assert hvs.owns_running(directory, winners[0])


def test_a_handed_in_token_that_does_not_own_the_marker_runs_nothing(tmp_path):
    candidate_root, _tree, _bases, runner, ops, db = _setup(tmp_path)
    directory = hvs.stage_dir(tmp_path, REQUEST)
    held = hvs.claim_running(directory)
    assert held is not None
    foreign = _run(tmp_path, _spec(CARD), ops, runner, candidate_root, db, marker_token="other")
    assert foreign["state"] == "pending" and runner.calls == []
    assert hvs.owns_running(directory, held)  # the holder's marker is untouched
    hvs.release_running(directory, held)
    gone = _run(tmp_path, _spec(CARD), ops, runner, candidate_root, db, marker_token=held)
    assert gone["state"] == "pending" and runner.calls == []
    assert not (directory / hvs.RESULT_FILE).exists()


def _evidence(db: Path) -> dict:
    conn = sqlite3.connect(db)
    try:
        card = json.loads(conn.execute("SELECT card_json FROM tasks").fetchone()[0])
    finally:
        conn.close()
    return card["terminal_review"]["evidence"]


def test_reseal_keeps_a_sealed_deletion_and_restores_it_on_revert(tmp_path):
    mod = _sha(b"VALUE = 2\n")
    db = _db(tmp_path, {"src/mod.py": mod, "src/gone.py": None})
    hvs.reseal_generated(db, TASK, REQUEST, {"src/generated.py": "a" * 64})
    assert _evidence(db)["changed_path_hashes"] == {
        "src/generated.py": "a" * 64,
        "src/gone.py": None,  # the candidate's deletion is still sealed
        "src/mod.py": mod,
    }
    # A host command recreated the deleted path; the revert must put the
    # deletion back, never forget it.
    joined = hvs.reseal_generated(db, TASK, REQUEST, {"src/gone.py": "b" * 64})
    assert joined["previous"] == {"src/gone.py": None} and joined["previously_absent"] == []
    hvs.reseal_generated(
        db, TASK, REQUEST, joined["previous"], event=hvs.GENERATED_REVERTED_EVENT,
        absent=joined["previously_absent"],
    )
    final = _evidence(db)
    assert final["changed_path_hashes"]["src/gone.py"] is None
    assert set(final["changed_path_hashes"]) == set(final["changed_paths"])
    assert "src/gone.py" in final["changed_paths"]


@pytest.mark.parametrize(
    ("changes", "absent", "code"),
    [
        ({"src/generated.py": "not-a-digest"}, (), "host_reseal_digest_invalid"),
        ({"src/generated.py": "a" * 64}, ("src/generated.py",), "host_reseal_change_and_drop_overlap"),
    ],
)
def test_reseal_refuses_a_malformed_digest_or_an_ambiguous_drop(tmp_path, changes, absent, code):
    db = _db(tmp_path, {"src/mod.py": "0" * 64})
    with pytest.raises(hvs.HostStageError, match=code):
        hvs.reseal_generated(db, TASK, REQUEST, changes, absent=absent)
    assert _evidence(db)["changed_path_hashes"] == {"src/mod.py": "0" * 64}


@pytest.mark.parametrize(
    "reported",
    ["tests/test_other.py::test_z", "../outside.py::test_z", "/abs/test_x.py::test_z"],
)
def test_a_reported_failure_outside_the_shard_or_tree_fails_closed(tmp_path, reported):
    candidate_root, _tree, base_trees, _runner, ops, db = _setup(tmp_path)

    def foreign(argv, cwd):
        return {
            "argv": list(argv),
            "returncode": 1,
            "timed_out": False,
            "stdout_tail": _pytest_output([reported], 0),
        }

    spec = _spec({"host_sweep": {"tests": ["tests/test_mod.py"], "processes": 1}})
    result = _run(tmp_path, spec, ops, foreign, candidate_root, db)
    comparison = result["sweep"]["comparison"]
    assert comparison["outcome"] == "sweep_unparsed" and comparison["unparsed_shards"] == [0]
    assert result["state"] == "failed"
    assert base_trees == []  # never judged against base, never put on a base argv


def test_declared_tests_cover_only_their_own_node_ids():
    covers = hvs._declared_covers
    assert covers("tests", "tests/test_mod.py::test_a")
    assert covers("tests/test_mod.py", "tests/test_mod.py::test_a[1]")
    assert covers("tests/test_mod.py::test_a", "tests/test_mod.py::test_a[x-y]")
    assert covers("tests/test_mod.py::test_a", "tests/test_mod.py")  # its file's own error
    assert covers("tests\\test_mod.py::test_a", "tests/test_mod.py::test_a")
    assert not covers("tests/test_mod.py::test_a", "tests/test_mod.py::test_ab")
    assert not covers("tests/test_mod.py", "tests/test_mod.pyc::test_a")
    assert not covers("tests", "tests_other/test_x.py::test_a")
    assert hvs._node_id_path(Path("."), "-k") is None


def test_a_declared_test_whose_file_escapes_the_tree_is_a_named_error(tmp_path):
    candidate_root, cand_tree, _bases, runner, ops, db = _setup(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (cand_tree.root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation needs a privilege this host lacks")
    spec = _spec({"host_sweep": {"tests": ["linked/test_x.py"], "processes": 1}})
    result = _run(tmp_path, spec, ops, runner, candidate_root, db)
    assert result["state"] == "failed"
    assert result["detail"] == "sweep_test_outside_tree:linked/test_x.py"
    assert runner.calls == []


def test_the_host_env_is_the_lane_sanitized_env_not_the_hub_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-do-not-leak")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x")
    tree = tmp_path / "tree"
    tree.mkdir()
    workspace = SimpleNamespace(path=tree, repo=tree)
    _argv, env = hvs.validation_command(workspace, ["pytest", "-q", "tests"])
    assert "ANTHROPIC_API_KEY" not in env and "PYTEST_ADDOPTS" not in env
    assert Path(env["HOME"]) == (tree / hvs.HOST_SCRATCH_DIR / "home").resolve()
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_a_sweep_test_outside_the_tree_is_a_final_failure_not_an_error(tmp_path, monkeypatch):
    candidate_root, _tree, _bases, runner, ops, db = _setup(tmp_path)
    original = hvs._node_id_path
    monkeypatch.setattr(
        hvs,
        "_node_id_path",
        lambda root, node_id: None if node_id == "tests/escaped.py" else original(root, node_id),
    )
    tests = ["tests/test_mod.py", "tests/escaped.py"]
    spec = _spec({"host_sweep": {"tests": tests, "processes": 1}})
    result = _run(tmp_path, spec, ops, runner, candidate_root, db)

    assert result["state"] == "failed"
    assert result["detail"] == "sweep_test_outside_tree:tests/escaped.py"
    assert runner.calls == []
    # Final, so accept refuses as failed and never restarts the stage as pending.
    card = {"host_sweep": {"tests": tests, "processes": 1}}
    assert hvs.accept_gate(tmp_path, card, REQUEST) == "host_validation_stage_failed"
    assert hvs.accept_step(tmp_path, card, REQUEST, TASK, None, {}) == "host_validation_stage_failed"
    assert hvs.review_packet_section(tmp_path, REQUEST, card)["error"] == result["detail"]


def test_declared_paths_are_normalized_before_covering():
    covers = hvs._declared_covers
    assert covers("./tests/test_mod.py", "tests/test_mod.py::test_a")
    assert covers("tests/test_mod.py::test_a", "./tests/test_mod.py::test_a[1]")
    assert covers(".\\tests\\", "tests/test_mod.py::test_a")
    assert covers("tests//sub/../test_mod.py", "tests/test_mod.py")
    assert not covers("./tests/test_mod.py", "tests/test_other.py::test_a")


def test_a_tree_without_a_runner_never_reaches_run_argv_with_env_none(tmp_path, monkeypatch):
    candidate_root, cand_tree, _bases, _runner, ops, db = _setup(tmp_path)
    envs: list = []

    def recording_run_argv(argv, cwd, *, env=None, **_kwargs):
        envs.append(env)
        return {"argv": list(argv), "returncode": 0, "timed_out": False}

    monkeypatch.setattr(hvs, "run_argv", recording_run_argv)
    spec = _spec(
        {
            "host_commands": [["git", "diff", "--check"]],
            "host_sweep": {"tests": ["tests/test_mod.py"], "processes": 1},
        }
    )
    result = _run(tmp_path, spec, ops, None, candidate_root, db)

    assert result["state"] == "error"
    assert result["error"] == "HostStageError:host_validation_error:no_runner"
    assert envs == [] and cand_tree.cleaned
    assert hvs.candidate_tree(tmp_path, REQUEST)["state"] == "failed"
    # A tree's lane runner always hands run_argv the built env, never None.
    lane = tmp_path / "lane"
    lane.mkdir()
    workspace = SimpleNamespace(path=lane, repo=lane)
    hvs.host_runner(workspace)(["pytest", "-q", "tests"], lane)
    assert envs and all(env is not None for env in envs)


def test_discard_refuses_a_tree_record_outside_the_worktrees_root(tmp_path, monkeypatch):
    from aiworkhub import worker_workspace as ww

    root = tmp_path / "worktrees"
    outside = tmp_path / "elsewhere"
    for directory in (root, outside / "worktree", outside / "home"):
        directory.mkdir(parents=True)
    cleaned: list = []
    monkeypatch.setattr(ww, "configured_worktree_root", lambda repo=None: root)
    monkeypatch.setattr(ww, "cleanup_workspace", lambda *paths, **_kw: cleaned.append(paths))
    repo = tmp_path / "repo"
    ops = hvs.workspace_tree_ops(
        SimpleNamespace(repo=repo, base_oid="b" * 40), {}, [], state_dir=tmp_path / "state"
    )
    directory = hvs.stage_dir(tmp_path, REQUEST)

    def record_tree(where: Path) -> None:
        workspace = {"repo": str(repo), "path": str(where / "worktree"), "home": str(where / "home")}
        hvs._write_json(
            directory / hvs.TREE_FILE,
            {"request_id": REQUEST, "state": "ready", "path": workspace["path"],
             "record": {"workspace": workspace}},
        )

    record_tree(outside)
    previous = hvs._discard_previous_tree(directory, REQUEST, ops)
    assert previous == {
        "path": str(outside / "worktree"),
        "discarded": False,
        "reason": hvs.DISCARD_REFUSED,
    }
    assert hvs.DISCARD_REFUSED == "discard_refused_outside_root"
    assert cleaned == [] and (outside / "worktree").is_dir()
    # The same record under the worktrees root is discarded.
    inside = root / "req"
    (inside / "worktree").mkdir(parents=True)
    (inside / "home").mkdir()
    record_tree(inside)
    assert hvs._discard_previous_tree(directory, REQUEST, ops)["discarded"] is True
    assert len(cleaned) == 1
    # The worktrees root itself is never a removable tree (path or home).
    for key in ("path", "home"):
        workspace = {"repo": str(repo), "path": str(inside / "worktree"), "home": str(inside / "home")}
        workspace[key] = str(root)
        hvs._write_json(
            directory / hvs.TREE_FILE,
            {"request_id": REQUEST, "state": "ready", "path": workspace["path"],
             "record": {"workspace": workspace}},
        )
        refused = hvs._discard_previous_tree(directory, REQUEST, ops)
        assert refused["discarded"] is False and refused["reason"] == hvs.DISCARD_REFUSED
    assert len(cleaned) == 1 and root.is_dir()
