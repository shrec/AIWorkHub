"""Tests for the three read-only operator recipes added by NF-2026-01700.

``preflight_card_winnability``, ``accept_readiness_bundle`` and
``blocked_queue_triage`` are spawned the way the catalogue renders them --
``python -m aiworkhub.recipes.<name>`` in a minimal environment, with the
fixture repository as the working directory -- against a ``task_queue.sqlite``
built here. Every run also proves the module wrote nothing: the whole fixture
tree is hashed before and after.

Vocabulary the modules borrow or restate is pinned against its owner, so a
drift fails here instead of silently misclassifying a card.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import manager_recipe_tools as mrt
from aiworkhub import tool_recipes as tr
from aiworkhub.recipes import accept_readiness_bundle as bundle
from aiworkhub.recipes import blocked_queue_triage as triage

SRC = Path(__file__).resolve().parents[1] / "src"

PREFLIGHT = "preflight_card_winnability"
BUNDLE = "accept_readiness_bundle"
TRIAGE = "blocked_queue_triage"
MODULES = (PREFLIGHT, BUNDLE, TRIAGE)

TASKS_DDL = """
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY, runner TEXT, topic TEXT, mode TEXT, status TEXT,
    worker_status TEXT, priority INTEGER, objective TEXT, card_json TEXT,
    created_at TEXT, updated_at TEXT, claimed_by TEXT, claimed_at TEXT,
    started_at TEXT, completed_at TEXT, origin_thread_id TEXT, archived_at TEXT
);
CREATE TABLE task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, event TEXT,
    runner TEXT, payload_json TEXT, created_at TEXT
);
"""


def owner(name: str, *, optional: bool = False) -> Any:
    """``aiworkhub.<name>``; an optional owner that cannot load skips the test."""
    try:
        return importlib.import_module(f"aiworkhub.{name}")
    except Exception as exc:  # noqa: BLE001 - some owners resolve the home directory
        if optional:
            pytest.skip(f"aiworkhub.{name} is not importable here: {type(exc).__name__}")
        raise


@dataclass
class Result:
    code: int
    out: dict[str, Any]
    err: str


class Repo:
    """A throwaway repository with a hand-built task queue the recipes read."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = root / ".aiworkhub" / "tasking" / "task_queue.sqlite"
        self.artifacts = (
            root / ".aiworkhub" / "runtime" / "process_logs" / "processes" / "attempt-artifacts"
        )
        self.db.parent.mkdir(parents=True)
        self.artifacts.mkdir(parents=True)
        self._clock = 0
        self._exec(TASKS_DDL, script=True)

    def _exec(self, sql: str, params: tuple[Any, ...] = (), *, script: bool = False) -> None:
        conn = sqlite3.connect(self.db)
        try:
            if script:
                conn.executescript(sql)
            else:
                conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def add(
        self,
        task_id: str,
        status: str = "pending",
        card: dict[str, Any] | None = None,
        *,
        topic: str = "alpha",
        worker_status: str = "",
        archived_at: str | None = None,
        raw: str | None = None,
    ) -> str:
        """Insert one task; later inserts are newer, so ordering is deterministic."""
        self._clock += 1
        self._exec(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, archived_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                "claude",
                topic,
                "work",
                status,
                worker_status,
                1,
                "fixture",
                raw if raw is not None else json.dumps(card if card is not None else {}),
                "2026-01-01T00:00:00",
                f"2026-01-01T00:{self._clock // 60:02d}:{self._clock % 60:02d}",
                archived_at,
            ),
        )
        return task_id

    def event(self, task_id: str, event: str, payload: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, event, "claude", json.dumps(payload), "2026-01-01T00:00:00"),
        )

    def artifact(
        self, request_id: str, role: str, payload: Any = None, *, raw: str | None = None
    ) -> None:
        name = owner("attempt_artifacts").ROLE_FILENAMES[role]
        directory = self.artifacts / request_id
        directory.mkdir(parents=True, exist_ok=True)
        text = raw if raw is not None else json.dumps(payload)
        (directory / name).write_text(text, encoding="utf-8")

    def write(self, relative: str, text: str = "") -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def snapshot(self) -> dict[str, str]:
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def run(self, module: str, *args: str) -> Result:
        """Spawn the module as the catalogue does, and prove it changed nothing."""
        env = {"PYTHONPATH": str(SRC)}
        if sys.platform == "win32":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        before = self.snapshot()
        done = subprocess.run(
            [sys.executable, "-m", f"aiworkhub.recipes.{module}", *args],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert self.snapshot() == before, f"{module} modified the fixture tree"
        return Result(done.returncode, json.loads(done.stdout), done.stderr)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


# ---------------------------------------------------------------------------
# The catalogue entries
# ---------------------------------------------------------------------------


def _recipe(module: str) -> tr.Recipe:
    return next(r for r in mrt.CANONICAL_RECIPES if r.id == f"aiworkhub.operator.{module}")


@pytest.mark.parametrize("module", MODULES)
def test_the_recipe_is_catalogued_read_only_and_renders_a_module_argv(module):
    recipe = _recipe(module)

    assert recipe.capabilities == ()
    assert recipe in mrt.UNIVERSAL_RECIPES
    assert "src/aiworkhub/" in recipe.purpose
    head = recipe.argv[:3]
    assert [token.text for token in head] == ["python", "-m", f"aiworkhub.recipes.{module}"]
    assert importlib.util.find_spec(f"aiworkhub.recipes.{module}") is not None


@pytest.mark.parametrize("module", (PREFLIGHT, BUNDLE))
def test_a_task_recipe_takes_exactly_one_required_task_id(module):
    recipe = _recipe(module)

    assert list(tr.build_argv(recipe, {"task_id": "T-1"})) == [
        "python",
        "-m",
        f"aiworkhub.recipes.{module}",
        "--task-id",
        "T-1",
    ]
    for bad in ({}, {"task_id": 5}, {"task_id": "T-1", "extra": "x"}):
        with pytest.raises(tr.RecipeError):
            tr.build_argv(recipe, bad)


def test_the_triage_recipe_bounds_its_limit_and_defaults_its_filter():
    recipe = _recipe(TRIAGE)

    assert list(tr.build_argv(recipe, {})) == [
        "python",
        "-m",
        "aiworkhub.recipes.blocked_queue_triage",
        "--limit",
        "50",
        "--topic",
        "all",
    ]
    assert list(tr.build_argv(recipe, {"limit": 7, "topic": "alpha"}))[3:] == [
        "--limit",
        "7",
        "--topic",
        "alpha",
    ]
    for limit in (0, -1, 201, "x"):
        with pytest.raises(tr.RecipeError):
            tr.build_argv(recipe, {"limit": limit})


def test_the_rendered_argv_runs_and_answers_for_the_repository_it_runs_in(repo):
    repo.add("t-1", "blocked", {"blocker_reason": "quota"})
    argv = list(tr.build_argv(_recipe(TRIAGE), {"limit": 5, "topic": "alpha"}))

    result = repo.run(TRIAGE, *argv[3:])

    assert result.code == 0, result.err
    assert [card["task_id"] for card in result.out["cards"]] == ["t-1"]


@pytest.mark.parametrize("module", MODULES)
def test_every_module_leaves_the_fixture_database_byte_identical(repo, module):
    repo.add("t-1", "blocked", {"blocker_reason": "quota"})
    repo.add("t-2", "pending", {"allowed_writes": ["src/a.py"]})
    args = {PREFLIGHT: ("--task-id", "t-2"), BUNDLE: ("--task-id", "t-1"), TRIAGE: ()}[module]
    before = hashlib.sha256(repo.db.read_bytes()).hexdigest()

    result = repo.run(module, *args)

    assert result.code == 0, result.err
    assert result.out["ok"] is True
    assert result.out["schema_id"] == f"aiworkhub.recipe.{module}.v1"
    assert hashlib.sha256(repo.db.read_bytes()).hexdigest() == before
    assert sorted(path.name for path in repo.db.parent.iterdir()) == [repo.db.name]


@pytest.mark.parametrize("module", MODULES)
def test_a_missing_task_queue_is_a_named_refusal(tmp_path, module):
    bare = Repo(tmp_path / "bare")
    bare.db.unlink()
    args = () if module == TRIAGE else ("--task-id", "x")

    result = bare.run(module, *args)

    assert result.code == 2
    assert (result.out["ok"], result.out["reason"]) == (False, "task_queue_unavailable")


@pytest.mark.parametrize("module", (PREFLIGHT, BUNDLE))
def test_an_unknown_or_empty_task_id_is_a_named_refusal(repo, module):
    repo.add("t-1")

    unknown = repo.run(module, "--task-id", "no-such-task")
    empty = repo.run(module, "--task-id", "  ")

    assert (unknown.code, unknown.out["reason"], unknown.out["detail"]) == (
        2,
        "task_not_found",
        "no-such-task",
    )
    assert (empty.code, empty.out["reason"]) == (2, "invalid_task_id")


# ---------------------------------------------------------------------------
# preflight_card_winnability
# ---------------------------------------------------------------------------


def _pending(**over: Any) -> dict[str, Any]:
    card: dict[str, Any] = {
        "allowed_writes": ["src/a.py", "tests/test_a.py"],
        "required_outputs": ["src/a.py", "tests/test_a.py"],
        "validation": ["python -m pytest -q tests/test_a.py"],
    }
    card.update(over)
    return card


def _preflight(repo: Repo, task_id: str) -> dict[str, Any]:
    result = repo.run(PREFLIGHT, "--task-id", task_id)
    assert result.code == 0, result.err
    assert result.out["winnable"] is (not result.out["blockers"])
    return result.out


def test_a_card_with_covered_outputs_and_no_rivals_is_winnable(repo):
    repo.add("p-1", "pending", _pending())
    repo.add("done", "finished", {"allowed_writes": ["src/a.py"]})
    repo.add("waiting", "pending", {"allowed_writes": ["src/a.py"]})

    out = _preflight(repo, "p-1")

    assert out["winnable"] is True
    assert out["blockers"] == []
    assert out["status"] == "pending"
    assert out["required_outputs_not_covered"] == []
    assert out["write_overlaps"] == [] and out["write_overlap_count"] == 0
    assert out["validation_inputs_missing"] == []


def test_a_required_output_outside_allowed_writes_makes_the_card_unwinnable(repo):
    repo.add(
        "p-1",
        "pending",
        _pending(allowed_writes=["src/a.py"], required_outputs=["src/a.py", "docs/out.md"]),
    )

    out = _preflight(repo, "p-1")

    assert out["winnable"] is False
    assert out["required_outputs_not_covered"] == ["docs/out.md"]
    assert "required_output_not_covered:docs/out.md" in out["blockers"]
    assert not any("src/a.py" in blocker for blocker in out["blockers"])


def test_a_directory_entry_covers_nothing_beneath_it_but_a_glob_does(repo):
    repo.add("dir", "pending", _pending(allowed_writes=["src"], validation=[]))
    repo.add("glob", "pending", _pending(allowed_writes=["src/*"], validation=[]))

    directory = _preflight(repo, "dir")
    glob = _preflight(repo, "glob")

    assert "required_output_not_covered:src/a.py" in directory["blockers"]
    assert glob["required_outputs_not_covered"] == ["tests/test_a.py"]


def test_overlapping_allowed_writes_report_the_rival_cards_and_paths(repo):
    repo.add("p-1", "pending", {"allowed_writes": ["src/shared.py", "docs/x.md"]})
    repo.add("busy", "processing", {"allowed_writes": ["src/shared.py"]}, topic="beta")
    repo.add("held", "review", {"allowed_writes": ["src/*"]}, topic="gamma")
    repo.add("idle", "pending", {"allowed_writes": ["src/shared.py"]})
    repo.add("done", "finished", {"allowed_writes": ["src/shared.py"]})
    repo.add("apart", "processing", {"allowed_writes": ["elsewhere/y.py"]})

    out = _preflight(repo, "p-1")

    overlaps = {item["task_id"]: item for item in out["write_overlaps"]}
    assert sorted(overlaps) == ["busy", "held"]
    assert out["write_overlap_count"] == 2
    assert overlaps["busy"]["paths"] == [{"this": "src/shared.py", "other": "src/shared.py"}]
    assert overlaps["held"]["paths"] == [{"this": "src/shared.py", "other": "src/*"}]
    assert (overlaps["busy"]["topic"], overlaps["busy"]["status"]) == ("beta", "processing")
    assert overlaps["held"]["status"] == "review"
    assert out["winnable"] is False
    assert {"write_overlap:busy", "write_overlap:held"} <= set(out["blockers"])


def test_a_card_never_overlaps_itself(repo):
    repo.add("busy", "processing", {"allowed_writes": ["src/shared.py"]})

    out = _preflight(repo, "busy")

    assert out["write_overlaps"] == []
    assert out["blockers"] == ["task_not_pending:processing"]


def test_a_peer_whose_scope_cannot_be_read_blocks_instead_of_passing(repo):
    repo.add("p-1", "pending", {"allowed_writes": ["src/a.py"]})
    repo.add("broken", "processing", raw="{not json")
    repo.add("odd", "review", {"allowed_writes": "src/a.py"})

    out = _preflight(repo, "p-1")

    reasons = {item["task_id"]: item["reason"] for item in out["peers_unreadable"]}
    assert reasons == {"broken": "card_unreadable", "odd": "allowed_writes_malformed"}
    assert out["peers_unreadable_count"] == 2
    assert out["winnable"] is False
    assert {"peer_write_scope_unknown:broken", "peer_write_scope_unknown:odd"} <= set(
        out["blockers"]
    )


def test_a_validation_input_nobody_can_supply_is_a_blocker(repo):
    repo.write("tests/test_exists.py", "def test_x():\n    pass\n")
    repo.add(
        "missing",
        "pending",
        {
            "allowed_writes": ["src/m.py"],
            "validation": ["python -m pytest -q tests/test_missing.py tests/test_exists.py"],
        },
    )
    repo.add(
        "created",
        "pending",
        {
            "allowed_writes": ["src/c.py", "tests/test_new.py"],
            "validation": ["python -m pytest -q tests/test_new.py tests/test_exists.py"],
        },
    )

    missing = _preflight(repo, "missing")
    created = _preflight(repo, "created")

    assert [item["path"] for item in missing["validation_inputs_missing"]] == [
        "tests/test_missing.py"
    ]
    assert "validation_input_missing:tests/test_missing.py" in missing["blockers"]
    assert missing["winnable"] is False
    assert created["validation_inputs_missing"] == []
    assert created["winnable"] is True


def test_behavioral_work_kinds_must_declare_their_validation_roles(repo):
    repo.write("tests/test_exists.py", "def test_x():\n    pass\n")
    two = [
        "python -m pytest -q tests/test_exists.py",
        "python -m pytest -q tests/test_exists.py -k x",
    ]
    repo.add(
        "bare",
        "pending",
        {"allowed_writes": ["src/r.py"], "work_kind": "bugfix", "validation": two},
    )
    repo.add(
        "roled",
        "pending",
        {
            "allowed_writes": ["src/r2.py"],
            "work_kind": "bugfix",
            "validation": two,
            "validation_roles": ["reproduction", "regression"],
        },
    )
    repo.add("plain", "pending", {"allowed_writes": ["src/p.py"], "validation": two})

    bare = _preflight(repo, "bare")
    roled = _preflight(repo, "roled")
    plain = _preflight(repo, "plain")

    assert bare["winnable"] is False
    assert bare["validation_roles"]["required"] == ["reproduction", "regression"]
    assert bare["validation_roles"]["missing"] == ["reproduction", "regression"]
    assert bare["validation_roles"]["declared"] == []
    assert any(b.startswith("validation_roles_invalid:") for b in bare["blockers"])
    assert roled["validation_roles"]["missing"] == []
    assert roled["validation_roles"]["declared"] == ["reproduction", "regression"]
    assert roled["validation_roles"]["error"] is None and roled["winnable"] is True
    assert plain["validation_roles"]["stored"] is False
    assert plain["validation_roles"]["required"] == []


def test_a_card_that_is_not_pending_is_evaluated_but_never_winnable(repo):
    repo.add("busy", "processing", _pending())

    out = _preflight(repo, "busy")

    assert out["status"] == "processing"
    assert "task_not_pending:processing" in out["blockers"]
    assert out["winnable"] is False


def test_unreadable_and_malformed_cards_block_rather_than_pass(repo):
    repo.add("bad-json", "pending", raw="not json")
    repo.add(
        "malformed",
        "pending",
        {"allowed_writes": "src/x.py", "required_outputs": ["src/x.py"], "validation": "pytest"},
    )
    repo.add(
        "invalid",
        "pending",
        {
            "allowed_writes": ["/abs/x.py", "../up.py", "src/ok.py"],
            "required_outputs": ["src/ok.py"],
        },
    )

    bad = _preflight(repo, "bad-json")
    malformed = _preflight(repo, "malformed")
    invalid = _preflight(repo, "invalid")

    assert bad["winnable"] is False and "card_unreadable" in bad["blockers"]
    assert {"allowed_writes_malformed", "validation_malformed"} <= set(malformed["blockers"])
    assert malformed["winnable"] is False
    assert {"allowed_write_invalid:/abs/x.py", "allowed_write_invalid:../up.py"} <= set(
        invalid["blockers"]
    )


# ---------------------------------------------------------------------------
# accept_readiness_bundle
# ---------------------------------------------------------------------------

GOOD_CHECK = {
    "command": "python -m pytest -q tests/test_a.py",
    "returncode": 0,
    "failure_receipt": {},
}
GOOD_DIFF = {"changed_paths": ["src/a.py", "tests/test_a.py"]}
GOOD_VALIDATION = {"checks": [GOOD_CHECK]}


def _rid(number: int) -> str:
    return f"{number:032x}"


def _review_card(request_id: str, lenses: Any = ("correctness", "security"), **over: Any) -> dict:
    profile: dict[str, Any] = {
        "effective_tier": "standard",
        "explicit_human_approval_required": False,
    }
    if lenses is not None:
        profile["required_reviewer_lenses"] = list(lenses)
    card: dict[str, Any] = {
        "launch_request_id": request_id,
        "allowed_writes": ["src/a.py", "tests/test_a.py"],
        "validation": ["python -m pytest -q tests/test_a.py"],
        "terminal_review": {
            "substatus": "review_ready",
            "request_id": request_id,
            "evidence": {"quality_gate": {"review_risk_profile": profile}},
        },
    }
    card.update(over)
    return card


def _lens_reviewer(
    repo: Repo,
    task_id: str,
    target: str,
    request_id: str,
    lens: str,
    *,
    substatus: str = "review_ready",
    status: str = "review",
) -> None:
    repo.add(
        task_id,
        status,
        {
            "terminal_review": {
                "substatus": substatus,
                "evidence": {
                    "quality_review": {
                        "target_task_id": target,
                        "target_request_id": request_id,
                        "lens": lens,
                        "packet_sha256": "0" * 64,
                    },
                    "quality_review_receipt": {"ok": True},
                },
            }
        },
        topic="quality_review",
    )


def _attempt(
    repo: Repo,
    task_id: str = "r-1",
    number: int = 1,
    *,
    validation: Any = GOOD_VALIDATION,
    diff: Any = GOOD_DIFF,
    lenses: Any = ("correctness", "security"),
    reviewers: bool = True,
    **card_over: Any,
) -> str:
    """A review card with its artifacts and, by default, both lens reviewers usable."""
    request_id = _rid(number)
    repo.add(task_id, "review", _review_card(request_id, lenses, **card_over))
    if validation is not None:
        repo.artifact(request_id, "validation", validation)
    if diff is not None:
        repo.artifact(request_id, "diff", diff)
    if reviewers:
        _lens_reviewer(repo, f"{task_id}-c", task_id, request_id, "correctness")
        _lens_reviewer(
            repo, f"{task_id}-s", task_id, request_id, "security", substatus="accepted",
            status="finished",
        )
    return request_id


def _bundle(repo: Repo, task_id: str = "r-1") -> dict[str, Any]:
    result = repo.run(BUNDLE, "--task-id", task_id)
    assert result.code == 0, result.err
    assert result.out["ready"] is (not result.out["blockers"])
    return result.out


def test_a_review_card_with_every_piece_of_evidence_is_ready(repo):
    request_id = _attempt(repo)

    out = _bundle(repo)

    assert out["ready"] is True and out["blockers"] == []
    assert out["latest_request_id"] == request_id
    assert out["status"] == "review" and out["terminal_substatus"] == "review_ready"
    checks = out["validation"]["checks"]
    assert [(c["command"], c["exit_code"], c["passed"]) for c in checks] == [
        ("python -m pytest -q tests/test_a.py", 0, True)
    ]
    assert out["validation"]["counts"] == {"passed": 1, "failed": 0, "unproven": 0}
    assert {r["lens"]: r["usable"] for r in out["lens_reviewers"]} == {
        "correctness": True,
        "security": True,
    }
    assert out["required_lenses"] == ["correctness", "security"]
    assert out["changed_paths"] == ["src/a.py", "tests/test_a.py"]
    assert out["changed_paths_outside_allowed_writes"] == []
    assert out["risk_tier"] == "standard"
    assert out["explicit_human_approval_required"] is False


def test_the_validation_section_names_the_reader_that_folded_the_receipts(repo):
    _attempt(repo)
    sibling = importlib.util.find_spec("aiworkhub.recipes.attempt_validation") is not None

    out = _bundle(repo)

    assert out["validation"]["reader"] == ("attempt_validation" if sibling else "local")


def test_failed_timed_out_and_unproven_receipts_each_block(repo):
    checks = [
        {
            "command": "pytest-fail",
            "returncode": 1,
            "failure_receipt": {"failure_class": "test_failure"},
        },
        {"command": "ruff-timeout", "returncode": 0, "failure_receipt": {"timed_out": True}},
        {"command": "bool-code", "returncode": False},
        {"command": "no-code", "returncode": None},
        "not-a-check",
        {"declared_argv": ["python", "-m", "mypy"], "returncode": 0},
    ]
    _attempt(repo, validation={"checks": checks}, lenses=(), reviewers=False)

    out = _bundle(repo)

    by_command = {c["command"]: c for c in out["validation"]["checks"]}
    assert by_command["pytest-fail"]["exit_code"] == 1
    assert by_command["pytest-fail"]["passed"] is False
    assert by_command["pytest-fail"]["failure_class"] == "test_failure"
    assert by_command["ruff-timeout"]["timed_out"] is True
    assert by_command["ruff-timeout"]["passed"] is False
    assert by_command["bool-code"]["exit_code"] is None
    assert by_command["bool-code"]["passed"] is None
    assert by_command["no-code"]["passed"] is None
    assert by_command["python -m mypy"]["passed"] is True
    assert out["validation"]["counts"] == {"passed": 1, "failed": 2, "unproven": 3}
    assert out["ready"] is False
    assert "validation_check_failed:pytest-fail:exit_1" in out["blockers"]
    assert "validation_check_failed:ruff-timeout:timed_out" in out["blockers"]
    assert "validation_receipts_incomplete:3/6" in out["blockers"]


def test_no_receipts_is_not_a_pass_unless_the_card_declares_an_exemption(repo):
    empty = {"checks": []}
    _attempt(repo, "bare", 1, validation=empty, lenses=(), reviewers=False)
    repo.add(
        "exempt",
        "review",
        _review_card(_rid(2), (), validation=[], validation_exemption="docs only"),
    )
    repo.artifact(_rid(2), "validation", empty)
    repo.artifact(_rid(2), "diff", GOOD_DIFF)

    bare = _bundle(repo, "bare")
    exempt = _bundle(repo, "exempt")

    assert bare["ready"] is False and "validation_receipts_missing" in bare["blockers"]
    assert exempt["validation"]["exempt"] is True
    assert exempt["blockers"] == [] and exempt["ready"] is True


def test_a_declared_command_without_a_receipt_blocks(repo):
    commands = ["python -m pytest -q tests/test_a.py", "python -m mypy src"]
    repo.add("short", "review", _review_card(_rid(3), (), validation=commands))
    repo.artifact(_rid(3), "validation", GOOD_VALIDATION)
    repo.artifact(_rid(3), "diff", GOOD_DIFF)

    out = _bundle(repo, "short")

    assert out["validation"]["declared_command_count"] == 2
    assert out["validation"]["check_count"] == 1
    assert out["ready"] is False
    assert out["blockers"] == ["validation_receipts_short:1/2"]


def test_a_profile_that_demands_human_approval_is_not_ready(repo):
    card = _review_card(_rid(4), ())
    profile = card["terminal_review"]["evidence"]["quality_gate"]["review_risk_profile"]
    profile["explicit_human_approval_required"] = True
    repo.add("signoff", "review", card)
    repo.artifact(_rid(4), "validation", GOOD_VALIDATION)
    repo.artifact(_rid(4), "diff", GOOD_DIFF)

    out = _bundle(repo, "signoff")

    assert out["explicit_human_approval_required"] is True
    assert out["ready"] is False
    assert out["blockers"] == ["explicit_human_approval_required"]


def test_changed_paths_outside_allowed_writes_are_named(repo):
    diff = {"changed_paths": ["src/a.py", "src/evil.py", "../escape.py", "/abs/y.py"]}
    _attempt(repo, diff=diff)

    out = _bundle(repo)

    assert out["ready"] is False
    assert out["changed_paths_outside_allowed_writes"] == [
        "src/evil.py",
        "../escape.py",
        "/abs/y.py",
    ]
    assert out["out_of_scope_count"] == 3 and out["changed_path_count"] == 4
    assert {
        "changed_path_outside_allowed_writes:src/evil.py",
        "changed_path_outside_allowed_writes:../escape.py",
        "changed_path_outside_allowed_writes:/abs/y.py",
    } <= set(out["blockers"])
    assert not any(b.endswith(":src/a.py") for b in out["blockers"])


def test_a_directory_entry_does_not_cover_changes_beneath_it(repo):
    _attempt(repo, allowed_writes=["src", "tests/*"])

    out = _bundle(repo)

    assert out["changed_paths_outside_allowed_writes"] == ["src/a.py"]
    assert "changed_path_outside_allowed_writes:src/a.py" in out["blockers"]


def test_a_required_lens_without_a_usable_reviewer_blocks(repo):
    request_id = _attempt(repo, reviewers=False)
    _lens_reviewer(repo, "rv-c", "r-1", request_id, "correctness")
    _lens_reviewer(
        repo, "rv-s", "r-1", request_id, "security",
        substatus="blocked_review_unavailable", status="blocked",
    )

    out = _bundle(repo)

    states = {r["lens"]: (r["status"], r["state"], r["usable"]) for r in out["lens_reviewers"]}
    assert states == {
        "correctness": ("review", "review_ready", True),
        "security": ("blocked", "blocked_review_unavailable", False),
    }
    assert out["blockers"] == [
        "quality_reviewer_not_review_ready:security:blocked_review_unavailable"
    ]


def test_a_required_lens_with_no_reviewer_at_all_is_missing(repo):
    request_id = _attempt(repo, reviewers=False)
    _lens_reviewer(repo, "rv-c", "r-1", request_id, "correctness")

    out = _bundle(repo)

    assert out["blockers"] == ["required_reviewer_missing:security"]
    assert out["lens_reviewer_count"] == 1


def test_reviewers_bound_to_another_attempt_or_task_do_not_count(repo):
    _attempt(repo, reviewers=False)
    _lens_reviewer(repo, "old-c", "r-1", _rid(99), "correctness")
    _lens_reviewer(repo, "other-s", "other-task", _rid(1), "security")

    out = _bundle(repo)

    assert out["lens_reviewers"] == [] and out["lens_reviewer_count"] == 0
    assert out["blockers"] == [
        "required_reviewer_missing:correctness",
        "required_reviewer_missing:security",
    ]


def test_an_unknown_risk_profile_is_never_read_as_no_lenses_required(repo):
    card = _review_card(_rid(1))
    del card["terminal_review"]["evidence"]
    repo.add("r-1", "review", card)
    repo.artifact(_rid(1), "validation", {"checks": [GOOD_CHECK]})
    repo.artifact(_rid(1), "diff", GOOD_DIFF)

    out = _bundle(repo)

    assert out["required_lenses_known"] is False and out["required_lenses"] == []
    assert out["blockers"] == ["required_reviewer_lenses_unknown"]


def test_missing_and_unreadable_artifacts_block(repo):
    _attempt(repo, "gone", 1, validation=None, diff=None, lenses=(), reviewers=False)
    repo.add("bad", "review", _review_card(_rid(2), ()))
    repo.artifact(_rid(2), "validation", raw="{not json")
    repo.artifact(_rid(2), "diff", {"changed_paths": "src/a.py"})

    gone = _bundle(repo, "gone")
    bad = _bundle(repo, "bad")

    assert gone["validation"]["artifact"] == "missing" and gone["diff_artifact"] == "missing"
    assert gone["blockers"] == ["validation_artifact_missing", "diff_artifact_missing"]
    assert bad["validation"]["artifact"] == "unreadable" and bad["diff_artifact"] == "unreadable"
    assert bad["blockers"] == ["validation_artifact_unreadable", "diff_artifact_unreadable"]


def test_an_attempt_that_cannot_be_identified_reads_no_artifacts(repo):
    repo.add("odd-id", "review", _review_card("not-a-request-id", ()))
    repo.add("mismatch", "review", _review_card(_rid(1), (), launch_request_id=_rid(2)))

    odd = _bundle(repo, "odd-id")
    mismatch = _bundle(repo, "mismatch")

    assert odd["latest_request_id"] == "" and "latest_attempt_unknown" in odd["blockers"]
    assert odd["validation"]["artifact"] == "unknown_attempt"
    assert mismatch["latest_request_id"] == ""
    assert "launch_request_id_mismatch" in mismatch["blockers"]


def test_a_card_that_is_not_in_review_or_not_review_ready_is_flagged(repo):
    repo.add("pending", "pending", _review_card(_rid(1), ()))
    repo.add("bad-json", "review", raw="not json")
    card = _review_card(_rid(2), ())
    card["terminal_review"]["substatus"] = "blocked_x"
    repo.add("blocked-sub", "review", card)

    pending = _bundle(repo, "pending")
    bad = _bundle(repo, "bad-json")
    sub = _bundle(repo, "blocked-sub")

    assert "task_not_in_review:pending" in pending["blockers"]
    assert "card_unreadable" in bad["blockers"] and bad["ready"] is False
    assert "terminal_substatus_not_review_ready:blocked_x" in sub["blockers"]


def test_the_receipt_list_is_bounded_and_says_so(repo):
    checks = [dict(GOOD_CHECK, command=f"check-{i}") for i in range(bundle.MAX_CHECKS + 5)]
    _attempt(repo, validation={"checks": checks}, lenses=(), reviewers=False)

    out = _bundle(repo)

    assert len(out["validation"]["checks"]) == bundle.MAX_CHECKS
    assert out["validation"]["check_count"] == bundle.MAX_CHECKS + 5
    assert out["validation"]["checks_truncated"] is True
    assert f"validation_checks_truncated:{bundle.MAX_CHECKS + 5}" in out["blockers"]


# -- the reader the bundle chooses, and the vocabulary it restates ------------


def _sibling_stub(monkeypatch: pytest.MonkeyPatch, fold: Any) -> None:
    """Make only the ``attempt_validation`` import answer with ``fold`` as its reader."""
    real = importlib.import_module
    stub = type("StubValidation", (), {"_fold_check": staticmethod(fold)})

    def import_module(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.endswith(".attempt_validation"):
            if fold is None:
                raise ImportError(name)
            return stub
        return real(name, *args, **kwargs)

    monkeypatch.setattr(bundle.importlib, "import_module", import_module)


def test_the_sibling_reader_is_preferred_when_it_answers_the_probe(monkeypatch):
    seen: list[int] = []

    def fold(check: dict, tail: int) -> dict:
        seen.append(tail)
        return {**bundle.local_fold(check), "extra": True}

    _sibling_stub(monkeypatch, fold)

    name, reader = bundle.check_reader()

    assert name == bundle.READER_SIBLING
    assert reader({"returncode": 3})["extra"] is True
    assert seen == [1, 1]


def _wrong_arity(check: dict) -> dict:
    return {}


def _no_answer(check: dict, tail: int) -> None:
    return None


def _missing_receipt_fields(check: dict, tail: int) -> dict:
    return {"command": None}


def _changed_signature(check: dict, tail: int) -> dict:
    raise TypeError("changed signature")


@pytest.mark.parametrize(
    "broken", [None, _wrong_arity, _no_answer, _missing_receipt_fields, _changed_signature]
)
def test_a_sibling_reader_that_fails_its_probe_falls_back_to_the_local_one(monkeypatch, broken):
    _sibling_stub(monkeypatch, broken)

    assert bundle.check_reader() == (bundle.READER_LOCAL, bundle.local_fold)


def test_the_local_reader_agrees_with_attempt_validations_own_on_receipt_fields():
    reader = owner("recipes.attempt_validation", optional=True)._fold_check
    checks = [
        {},
        GOOD_CHECK,
        {
            "command": "x",
            "returncode": 2,
            "failure_receipt": {"timed_out": True, "failure_class": "t"},
        },
        {"argv": ["a", "b"], "declared_argv": ["c"], "returncode": None, "failure_receipt": "bad"},
    ]
    for check in checks:
        theirs, mine = reader(check, 1), bundle.local_fold(check)
        assert {key: theirs[key] for key in mine} == mine


def test_the_reviewer_states_it_restates_match_the_accept_review_owner():
    accept = owner("process_launcher_accept_review", optional=True)

    assert set(bundle.REVIEWER_USABLE) == set(accept._REVIEWER_USABLE_STATES)
    for kind in (
        "terminal_substatus_not_review_ready",
        "required_reviewer_missing",
        "quality_reviewer_not_review_ready",
    ):
        assert kind in accept.ACCEPT_BLOCKER_KINDS
    assert bundle.REVIEW_TOPIC in inspect.getsource(accept.bound_reviewer_rows)


# ---------------------------------------------------------------------------
# blocked_queue_triage
# ---------------------------------------------------------------------------


def _triage(repo: Repo, *args: str) -> dict[str, Any]:
    result = repo.run(TRIAGE, *args)
    assert result.code == 0, result.err
    return result.out


def test_a_blocked_card_with_a_route_unavailable_reason_is_classified_route_unavailable(repo):
    repo.add(
        "t-route",
        "blocked",
        {"blocker_reason": "RuntimeError:review_route_unavailable:no route for claude"},
    )

    out = _triage(repo)

    (card,) = out["cards"]
    assert (card["task_id"], card["class"], card["matched"]) == (
        "t-route",
        "route_unavailable",
        "route_unavailable",
    )
    assert card["reason_source"] == "blocker_reason"
    assert out["class_counts"]["route_unavailable"] == 1
    assert out["total_blocked_count"] == out["returned_count"] == 1
    assert out["truncated"] is False
    assert isinstance(out["next_actions"]["route_unavailable"], str)


def test_the_class_vocabulary_is_fixed_and_each_class_has_one_next_action():
    assert set(triage.CLASSES) == {
        "route_unavailable",
        "validation_failed",
        "scope_violation",
        "quota",
        "sandbox",
        "unknown",
    }
    assert set(triage.NEXT_ACTIONS) == set(triage.CLASSES)
    assert all(isinstance(text, str) and text.strip() for text in triage.NEXT_ACTIONS.values())
    assert len(set(triage.NEXT_ACTIONS.values())) == len(triage.CLASSES)


CLASSIFIED = {
    "route-reason": (
        {"blocker_reason": "RuntimeError:review_route_unavailable:no route"},
        "route_unavailable",
    ),
    "route-terminal-error": (
        {
            "terminal_failure": {
                "substatus": "launch_failed",
                "evidence": {"error": "RuntimeError:review_route_unavailable:x"},
            }
        },
        "route_unavailable",
    ),
    "validation": (
        {
            "terminal_failure": {
                "substatus": "validation_failed",
                "evidence": {"error": "validation_failed:python -m pytest tests/test_sandbox_x.py"},
            }
        },
        "validation_failed",
    ),
    "scope": ({"blocker_reason": "scope_violation:src/x.py"}, "scope_violation"),
    "scope-substatus": ({"terminal_substatus": "scope_rejected"}, "scope_violation"),
    "quota": (
        {
            "terminal_failure": {
                "substatus": "provider_refused",
                "evidence": {"failure_class": "rate_limited"},
            }
        },
        "quota",
    ),
    "sandbox": ({"blocker_reason": "unsupported_sandbox_backend:none"}, "sandbox"),
    "undetermined": ({"blocker_reason": "cause_undetermined:sandbox/path.py"}, "unknown"),
    "odd": ({"blocker_reason": "something odd happened"}, "unknown"),
    "silent": ({}, "unknown"),
    "non-string": ({"blocker_reason": {"nested": "quota"}}, "unknown"),
}


def test_each_blocked_card_is_classified_from_the_reason_it_recorded(repo):
    for task_id, (card, _expected) in CLASSIFIED.items():
        repo.add(task_id, "blocked", card)

    out = _triage(repo)

    assert {c["task_id"]: c["class"] for c in out["cards"]} == {
        task_id: expected for task_id, (_card, expected) in CLASSIFIED.items()
    }
    assert out["class_counts"] == {
        name: sum(1 for _c, expected in CLASSIFIED.values() if expected == name)
        for name in triage.CLASSES
    }
    by_id = {c["task_id"]: c for c in out["cards"]}
    assert by_id["undetermined"]["matched"] is None
    assert by_id["silent"]["reason_source"] == "none" and by_id["silent"]["reason"] == ""
    assert by_id["validation"]["matched"] == "validation_failed"
    assert by_id["validation"]["reason_source"] == "terminal_failure.evidence.error"


def test_a_card_that_names_no_reason_falls_back_to_its_newest_terminal_failure_event(repo):
    repo.add("t-1", "blocked", {})
    repo.event("t-1", "terminal_failure", {"evidence": {"error": "quota_exhausted:old"}})
    repo.event(
        "t-1",
        "terminal_failure",
        {"terminal_failure": {"evidence": {"error": "validation_failed:foo"}}},
    )
    repo.event(
        "t-1", "launch_started", {"evidence": {"error": "RuntimeError:review_route_unavailable"}}
    )

    (card,) = _triage(repo)["cards"]

    assert card["class"] == "validation_failed"
    assert card["reason_source"] == "task_events.terminal_failure.evidence.error"


def test_the_card_outranks_the_event_log(repo):
    repo.add("t-1", "blocked", {"blocker_reason": "scope_violation:a"})
    repo.event("t-1", "terminal_failure", {"evidence": {"error": "quota_exhausted"}})

    (card,) = _triage(repo)["cards"]

    assert (card["class"], card["reason_source"]) == ("scope_violation", "blocker_reason")


def test_an_unreadable_card_is_unknown_and_says_why(repo):
    repo.add("t-bad", "blocked", raw="not json")

    (card,) = _triage(repo)["cards"]

    assert (card["class"], card["reason_source"], card["reason"]) == (
        "unknown",
        "card_unreadable",
        "",
    )


def test_only_live_blocked_cards_are_triaged_newest_first(repo):
    repo.add("t-old", "blocked", {"blocker_reason": "quota"})
    repo.add(
        "t-ws", "in_progress", {"blocker_reason": "scope_violation:a"}, worker_status="blocked_x"
    )
    repo.add("t-archived", "blocked", {"blocker_reason": "quota"}, archived_at="2026-02-01")
    repo.add("t-pending", "pending", {"blocker_reason": "quota"})
    repo.add("t-done", "finished", {"blocker_reason": "quota"})
    repo.add("t-new", "blocked", {"blocker_reason": "sandbox"})

    out = _triage(repo)

    assert [c["task_id"] for c in out["cards"]] == ["t-new", "t-ws", "t-old"]
    assert out["total_blocked_count"] == 3


def test_limit_topic_and_truncation_are_reported(repo):
    for index in range(4):
        repo.add(f"a-{index}", "blocked", {"blocker_reason": "quota"}, topic="alpha")
    repo.add("b-0", "blocked", {"blocker_reason": "quota"}, topic="beta")

    limited = _triage(repo, "--limit", "2", "--topic", "alpha")
    everything = _triage(repo, "--topic", "all")
    huge = _triage(repo, "--limit", "100000")
    tiny = _triage(repo, "--limit", "-5")

    assert [c["task_id"] for c in limited["cards"]] == ["a-3", "a-2"]
    assert (limited["total_blocked_count"], limited["returned_count"]) == (4, 2)
    assert limited["truncated"] is True and limited["topic_filter"] == "alpha"
    assert everything["total_blocked_count"] == 5 and everything["truncated"] is False
    assert huge["limit"] == triage.MAX_LIMIT and tiny["limit"] == 1


def test_a_long_reason_is_clipped_and_flagged(repo):
    repo.add("t-long", "blocked", {"blocker_reason": "quota:" + "x" * 5000})

    (card,) = _triage(repo)["cards"]

    assert card["class"] == "quota"
    assert len(card["reason"]) == triage.DISPLAY_REASON_CHARS
    assert card["reason_truncated"] is True


# -- classification rules, and the owner vocabulary they extend ---------------


@pytest.fixture
def rules() -> triage.Rules:
    return triage.build_rules()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("RuntimeError:review_route_unavailable:x", "route_unavailable"),
        ("route_unavailable and quota", "route_unavailable"),
        ("sandbox_denied and validation_failed", "sandbox"),
        ("quota:validation_failed", "quota"),
        ("validation_failed:quota_exhausted", "validation_failed"),
        ("validation_failed:scope_violation", "validation_failed"),
        ("required_output_zero_bytes:src/a.py", "validation_failed"),
        ("research_result_missing", "validation_failed"),
        ("symlink_output:src/a.py", "scope_violation"),
        ("Rate Limit hit", "quota"),
        ("sandboxed_validation_ok", "unknown"),
        ("rate_limiter_config", "unknown"),
        ("", "unknown"),
        ("cause_undetermined:quota", "unknown"),
    ],
)
def test_the_earliest_segment_that_names_a_cause_decides(rules, text, expected):
    undetermined = owner("task_store")._UNDETERMINED_BLOCKER_PREFIX
    candidates = triage.reason_candidates({"blocker_reason": text}, undetermined)

    got = triage.classify(candidates[0][1], rules)[0] if candidates else "unknown"

    assert got == expected


def test_every_validation_environment_restriction_the_launcher_names_is_sandbox(rules):
    owned = owner("process_launcher_validation").VALIDATION_ENVIRONMENT_RESTRICTION_PREFIXES

    assert owned
    for prefix in owned:
        assert triage.classify(f"{prefix}detail", rules)[0] == "sandbox", prefix


def test_the_review_routes_own_failure_prefix_is_route_unavailable(rules):
    prefix = owner("review_lifecycle").ROUTE_UNAVAILABLE_FAILURE_PREFIX

    assert triage.classify(f"{prefix}no route", rules)[0] == "route_unavailable"


def test_the_provider_refusals_that_are_quota_are_classified_quota(rules):
    refusals = owner("terminal_failure_classification").PROVIDER_REFUSAL_KINDS
    quota = {"session_limit", "quota_exhausted", "balance_exhausted", "rate_limited"}
    other = {
        "credential_rejected",
        "provider_unavailable",
        "cause_not_distinguished",
        "model_not_found",
    }

    assert set(refusals) == quota | other
    for kind in quota:
        assert triage.classify(kind, rules)[0] == "quota", kind
    for kind in other:
        assert triage.classify(kind, rules)[0] != "quota", kind


@pytest.mark.parametrize(
    ("message", "state", "expected"),
    [
        ("scope_violation:src/x.py", "scope_rejected", "scope_violation"),
        ("symlink_output:src/x.py", "scope_rejected", "scope_violation"),
        ("required_output_missing:src/x.py", "validation_failed", "validation_failed"),
        ("quality_gate_failed:x", "validation_failed", "validation_failed"),
        ("behavioral_gate:x", "validation_failed", "validation_failed"),
        ("residual_contract_file_unchanged:x", "validation_failed", "validation_failed"),
        ("research_result_invalid:x", "validation_failed", "validation_failed"),
        ("invalid_validation_command:x", "validation_failed", "validation_failed"),
    ],
)
def test_scope_and_validation_reasons_agree_with_the_launchers_terminal_states(
    rules, message, state, expected
):
    launcher = owner("process_launcher_validation")
    error = owner("worker_workspace").WorkspaceError(message)

    assert launcher.terminal_state_for_workspace_error(error) == state
    assert triage.classify(message, rules)[0] == expected
