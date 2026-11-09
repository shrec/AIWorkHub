"""Tests for the request_forensics recipe (NF-2026-02176).

The recipe is run the way the catalogue renders it -- ``python -m
aiworkhub.recipes.request_forensics`` in a fixture repository -- and every run
proves the fixture tree unchanged. The sections are the owner recipes' own
``main`` functions, so the identity checks pin that nothing was copied.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from aiworkhub import manager_recipe_tools as mrt
from aiworkhub import tool_recipes as tr
from aiworkhub.recipes import attempt_validation, process_liveness, request_log_tail
from aiworkhub.recipes import request_forensics as rf
from aiworkhub.recipes import worktree_diff

SRC = Path(__file__).resolve().parents[1] / "src"

RID = "0123456789abcdef0123456789abcdef"
RID_ALT = "fedcba9876543210fedcba9876543210"
UNKNOWN = "f" * 32


class Repo:
    """A throwaway repository with the runtime layout the owners read."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.processes = root / ".aiworkhub" / "runtime" / "process_logs" / "processes"
        self.artifacts = self.processes / "attempt-artifacts"
        self.worktrees = root / ".aiworkhub" / "runtime" / "worktrees"
        self.artifacts.mkdir(parents=True)

    def request(
        self,
        request_id: str,
        *,
        state: str = "succeeded",
        exit_code: int = 0,
        started: float | None = None,
        heartbeat: float | None = None,
        phase: str = "finalize",
        stderr: str = "",
        checks: list[dict[str, Any]] | None = None,
        validation: bool = True,
        changed: list[str] | None = None,
    ) -> None:
        started = time.time() - 60 if started is None else started
        supervisor = {
            "state": state,
            "exit_code": exit_code,
            "error": "",
            "child_pid": None,
            "supervisor_pid": None,
            "started_at_epoch": started,
            "finished_at_epoch": started + 5,
            "heartbeat_at_epoch": started + 4 if heartbeat is None else heartbeat,
            "timeout_seconds": 600,
            "timeout_enforced": True,
            "last_meaningful_phase": phase,
        }
        self.processes.mkdir(parents=True, exist_ok=True)
        (self.processes / f"{request_id}.supervisor.json").write_text(
            json.dumps(supervisor), encoding="utf-8"
        )
        (self.processes / f"{request_id}.stderr.log").write_text(stderr, encoding="utf-8")
        (self.processes / f"{request_id}.stdout.log").write_text("", encoding="utf-8")
        if validation:
            folder = self.artifacts / request_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "validation.json").write_text(
                json.dumps(
                    {
                        "schema_id": "aiworkhub.validation.v1",
                        "checks": checks or [],
                        "quality_gate": None,
                        "worker_mcp_gate": None,
                    }
                ),
                encoding="utf-8",
            )
        if changed is not None:
            folder = self.artifacts / request_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "diff.json").write_text(
                json.dumps({"changed_paths": changed}), encoding="utf-8"
            )
            worktree = self.worktrees / request_id / "worktree"
            for relative in changed:
                target = worktree / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("candidate\n", encoding="utf-8")


def failing(failure_class: str) -> dict[str, Any]:
    return {
        "command": "python -m pytest -q tests/test_x.py",
        "argv": ["python", "-m", "pytest", "-q", "tests/test_x.py"],
        "returncode": 1,
        "stdout_tail": "",
        "stderr_tail": "",
        "failure_receipt": {
            "failure_class": failure_class,
            "diagnostic_tail": "1 failed",
            "timed_out": False,
        },
    }


PASSING = {
    "command": "python -m ruff check src",
    "argv": ["python", "-m", "ruff", "check", "src"],
    "returncode": 0,
    "stdout_tail": "",
    "stderr_tail": "",
}


def run(root: Path, *args: str) -> tuple[int, dict[str, Any], str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "aiworkhub.recipes.request_forensics", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return proc.returncode, json.loads(proc.stdout), proc.stderr


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_request_answer_folds_every_owner_section(tmp_path):
    repo = Repo(tmp_path)
    repo.request(
        RID,
        state="failed",
        exit_code=1,
        phase="validation",
        heartbeat=1_000.0,
        stderr="line one\nboom at x\n",
        checks=[PASSING, failing("test_failure")],
        changed=["src/a.py"],
    )

    code, out, _ = run(tmp_path, "--request-id", RID)

    assert code == 0
    assert out["ok"] is True
    assert out["state"] == "failed"
    assert out["phase"] == "validation"
    assert out["last_heartbeat_epoch"] == 1_000.0
    assert out["failure_classes"] == ["test_failure"]
    assert out["validation"]["passed"] is False
    assert out["validation"]["failed_count"] == 1
    assert out["changed_paths"]["paths"] == ["src/a.py"]
    assert "boom at x" in out["stderr_tail"]["text"]


def test_missing_sections_are_unavailable_not_a_pass(tmp_path):
    repo = Repo(tmp_path)
    repo.request(RID, validation=False)

    code, out, _ = run(tmp_path, "--request-id", RID)

    assert code == 0
    assert out["validation"]["available"] is False
    assert out["validation"]["passed"] is None
    assert out["failure_classes"] is None
    assert out["changed_paths"]["available"] is False
    assert out["changed_paths"]["reason"] == "worktree_not_retained"


def test_a_run_with_no_checks_is_not_a_pass(tmp_path):
    repo = Repo(tmp_path)
    repo.request(RID, checks=[])

    code, out, _ = run(tmp_path, "--request-id", RID)

    assert code == 0
    assert out["validation"]["available"] is True
    assert out["validation"]["passed"] is None
    assert out["failure_classes"] is None


def test_unknown_request_id_exits_2_and_writes_nothing(tmp_path):
    Repo(tmp_path).request(RID, state="failed", exit_code=1)
    before = snapshot(tmp_path)

    code, out, _ = run(tmp_path, "--request-id", UNKNOWN)

    assert code == 2
    assert out["ok"] is False
    assert out["reason"] == "request_not_found"
    assert snapshot(tmp_path) == before


def test_malformed_request_id_is_refused(tmp_path):
    code, out, _ = run(tmp_path, "--request-id", "not-a-request")

    assert code == 2
    assert out["ok"] is False
    assert out["reason"] == "invalid_request_id"


def test_repository_rows_honour_state_and_limit(tmp_path):
    repo = Repo(tmp_path)
    for index in range(4):
        repo.request(f"{index:032x}", state="failed", exit_code=1)
    for index in range(2):
        repo.request(f"{index + 100:032x}", state="succeeded")

    code, out, _ = run(tmp_path, "--state", "failed", "--limit", "2")

    assert code == 0
    assert out["ok"] is True
    assert out["scanned_count"] == 6
    assert out["returned_count"] == 2
    assert out["truncated"] is True
    assert [row["claimed_state"] for row in out["rows"]] == ["failed", "failed"]


def test_failure_class_filter_selects_only_matching_requests(tmp_path):
    repo = Repo(tmp_path)
    repo.request(f"{1:032x}", state="failed", checks=[failing("test_failure")])
    repo.request(f"{2:032x}", state="failed", checks=[failing("test_failure")])
    repo.request(f"{3:032x}", state="failed", checks=[failing("sandbox")])
    repo.request(f"{4:032x}", state="failed", validation=False)

    code, out, _ = run(tmp_path, "--failure-class", "test_failure")

    assert code == 0
    assert out["returned_count"] == 2
    assert {row["request_id"] for row in out["rows"]} == {
        f"{1:032x}",
        f"{2:032x}",
    }
    assert all(row["failure_classes"] == ["test_failure"] for row in out["rows"])


def test_since_window_keeps_only_recent_starts(tmp_path):
    repo = Repo(tmp_path)
    now = time.time()
    repo.request(RID, started=now - 600)
    repo.request(RID_ALT, started=now - 3 * 86_400)

    code, out, _ = run(tmp_path, "--since", "1d")

    assert code == 0
    assert [row["request_id"] for row in out["rows"]] == [RID]
    assert out["filters"]["since_seconds"] == 86_400.0


def test_invalid_since_is_refused(tmp_path):
    code, out, _ = run(tmp_path, "--since", "2weeks")

    assert code == 2
    assert out["ok"] is False
    assert out["reason"] == "invalid_since"


def test_request_id_conflicts_with_repository_filters(tmp_path):
    Repo(tmp_path).request(RID)

    code, out, _ = run(tmp_path, "--request-id", RID, "--state", "failed")

    assert code == 2
    assert out["ok"] is False
    assert out["reason"] == "conflicting_arguments"


def test_sections_are_the_owner_recipes_not_copies():
    assert rf.attempt_validation is attempt_validation
    assert rf.process_liveness is process_liveness
    assert rf.request_log_tail is request_log_tail
    assert rf.worktree_diff is worktree_diff
    for owner_private in ("_entry", "_reconciler", "_fold_check", "_read_text"):
        assert not hasattr(rf, owner_private)


def test_catalogue_entry_is_read_only_and_renders_the_module():
    by_id = {r.id: r for r in mrt.CANONICAL_RECIPES}
    recipe = by_id["aiworkhub.operator.request_forensics"]

    assert recipe.capabilities == ()
    argv = tr.build_argv(
        recipe,
        {
            "request_id": "all",
            "state": "all",
            "failure_class": "all",
            "since": "all",
            "limit": 20,
        },
    )
    assert argv[:3] == ("python", "-m", "aiworkhub.recipes.request_forensics")
