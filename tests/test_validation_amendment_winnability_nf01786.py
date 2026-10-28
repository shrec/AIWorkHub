"""NF-2026-01786: an amendment the launch contract would refuse is refused at
reject/recover time, before the append-only validation list is written."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _p in (_SRC, _TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from aiworkhub import core, task_store  # noqa: E402
from test_validation_amendment_nf01151 import (  # noqa: E402
    _init_repo,
    _insert_task,
)

_RUNNER = "claude_task_mcp_runtime_wiring"
_TOPIC = "task_mcp"
_NPM_AMENDMENT = ["npm --prefix ext test"]
_UNWINNABLE = "validation_amendment_unwinnable:validation_npm_dependency_tree_unbound:ext:1"


@pytest.fixture
def repo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "nf01786-manager")
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)
    return root


def _write_ext(root: Path, *, dependency_rows: bool) -> None:
    ext = root / "ext"
    ext.mkdir()
    (ext / "package.json").write_text(json.dumps({"name": "ext"}), encoding="utf-8")
    packages: dict = {"": {"name": "ext"}}
    if dependency_rows:
        packages["node_modules/left-pad"] = {"version": "1.3.0"}
    (ext / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8"
    )


def _state(root: Path, task_id: str) -> tuple[str, list[str]]:
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        row = conn.execute(
            "SELECT status, card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0], list(json.loads(row[1]).get("validation") or [])


def _review_task(root: Path, task_id: str) -> None:
    _insert_task(
        root,
        task_id,
        _RUNNER,
        _TOPIC,
        extra_card={
            "allowed_writes": ["out/result.txt"],
            "validation": ["pytest -q tests/test_existing.py"],
        },
    )
    assert core.claim_start_exact(task_id, _RUNNER, _TOPIC)["ok"] is True
    assert core.mark_review(task_id, runner=_RUNNER, topic=_TOPIC)["ok"] is True


def _blocked_task(root: Path, task_id: str) -> None:
    _insert_task(
        root,
        task_id,
        _RUNNER,
        _TOPIC,
        extra_card={"validation": ["pytest -q tests/test_existing.py"]},
    )
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute("UPDATE tasks SET status='blocked' WHERE task_id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()


def test_reject_review_refuses_unwinnable_npm_amendment(repo_env: Path) -> None:
    _write_ext(repo_env, dependency_rows=True)
    task_id = "NF01786_REJECT"
    _review_task(repo_env, task_id)
    before = _state(repo_env, task_id)

    result = core.reject_review(
        task_id, "rework", topic=_TOPIC, validation_amendment=_NPM_AMENDMENT
    )

    assert result["ok"] is False
    assert result["returncode"] == 2
    assert result["stderr"] == _UNWINNABLE
    assert result["validation_command"] == _NPM_AMENDMENT[0]
    assert _state(repo_env, task_id) == before


def test_recover_blocked_rework_refuses_unwinnable_npm_amendment(repo_env: Path) -> None:
    _write_ext(repo_env, dependency_rows=True)
    task_id = "NF01786_RECOVER"
    _blocked_task(repo_env, task_id)
    before = _state(repo_env, task_id)
    assert before[0] == "blocked"

    result = core.recover_blocked_rework(
        task_id, topic=_TOPIC, validation_amendment=_NPM_AMENDMENT
    )

    assert result["ok"] is False
    assert result["stderr"] == _UNWINNABLE
    assert result["validation_command"] == _NPM_AMENDMENT[0]
    assert _state(repo_env, task_id) == before


def _gate_reached(*_a, **_kw) -> dict:
    # The write gate runs only after every amendment check passed; stopping
    # there proves acceptance without depending on the rest of the transition.
    return {"ok": False, "returncode": 1, "stderr": "nf01786_gate_reached"}


def _amend(entry: str, task_id: str, commands: list[str]) -> dict:
    if entry == "reject_review":
        return core.reject_review(
            task_id, "rework", topic=_TOPIC, validation_amendment=commands
        )
    return core.recover_blocked_rework(
        task_id, topic=_TOPIC, validation_amendment=commands
    )


@pytest.mark.parametrize("entry", ["reject_review", "recover_blocked_rework"])
def test_npm_amendment_with_bound_lock_is_accepted(
    repo_env: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    _write_ext(repo_env, dependency_rows=False)
    task_id = f"NF01786_NPM_OK_{entry.upper()}"
    _blocked_task(repo_env, task_id)
    monkeypatch.setattr(core, "_canonical_write_gate", _gate_reached)

    assert core._validate_validation_amendment_commands(_NPM_AMENDMENT) is None
    result = _amend(entry, task_id, _NPM_AMENDMENT)

    assert result["stderr"] == "nf01786_gate_reached", result


@pytest.mark.parametrize("entry", ["reject_review", "recover_blocked_rework"])
def test_plain_pytest_amendment_is_still_accepted(
    repo_env: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    task_id = f"NF01786_PYTEST_OK_{entry.upper()}"
    _blocked_task(repo_env, task_id)
    monkeypatch.setattr(core, "_canonical_write_gate", _gate_reached)
    commands = ["pytest -q tests/test_added.py"]
    with monkeypatch.context() as scoped:
        # No npm gate: the repository binding is never even resolved.
        scoped.setattr(core, "repo_root", lambda: pytest.fail("repo_root resolved"))
        assert core._validate_validation_amendment_commands(commands) is None

    result = _amend(entry, task_id, commands)

    assert result["stderr"] == "nf01786_gate_reached", result
