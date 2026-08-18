"""NF-2026-00244 / NF-2026-00280: manager-action attribution and the
previously-dead archive-repair surfaces.

- A coordinator/manager action (reject_review, archive, ...) must be attributed
  to the verified manager route, not to a hardcoded ``codex`` runner. Proven
  with a Claude manager route.
- An unattributable action (no verifiable coordinator capability) fails rather
  than defaulting to a name.
- The archive-reconciliation report and the transactional archive repair added
  by NF-2026-00276 are now reachable through the taskctl-compat dispatcher.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import aiworkhub  # noqa: E402
from aiworkhub import core, task_store  # noqa: E402

NOW = "2026-07-20T00:00:00+00:00"
_CLAUDE_IDENTITY = {
    "provider": "claude",
    "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
    "window_id": "claude_vscode_4242",
}


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    return root


def _insert(root: Path, task_id: str, *, worker_status: str = "review",
            status: str = "review", topic: str = "coding") -> None:
    r = task_store.storage_readiness(root)
    conn = sqlite3.connect(r.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "claude_coding", topic, "solo", status, worker_status, "normal",
             "obj", json.dumps({}), NOW, NOW, "claude_coding"),
        )
        conn.commit()
    finally:
        conn.close()


def _event_runners(root: Path, task_id: str, event: str) -> list[str]:
    r = task_store.storage_readiness(root)
    conn = sqlite3.connect(r.canonical_db)
    try:
        rows = conn.execute(
            "SELECT runner FROM task_events WHERE task_id=? AND event=? ORDER BY event_id",
            (task_id, event),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def _row(root: Path, task_id: str) -> dict:
    r = task_store.storage_readiness(root)
    conn = sqlite3.connect(r.canonical_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return dict(row)


@pytest.fixture
def coord(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    tok = tmp_path / "coordinator.token"
    tok.write_text("coord-token\n", encoding="utf-8")
    os.chmod(tok, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(tok))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")
    return root


def _use_claude_route(monkeypatch) -> None:
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: dict(_CLAUDE_IDENTITY))


# --- NF-2026-00244: attribution to the verified manager route --------------

def test_reject_review_attributes_to_verified_claude_manager_route(coord, monkeypatch):
    """Before the fix the reject_review event and receipt read ``codex`` even
    for a Claude manager. After it, both name the verified Claude route."""
    _use_claude_route(monkeypatch)
    _insert(coord, "T_REJECT_CLAUDE")

    res = core.reject_review("T_REJECT_CLAUDE", "rework this", to="pending")

    assert res["ok"] is True, res
    assert "--runner" in res["command"]
    runner_arg = res["command"][res["command"].index("--runner") + 1]
    assert runner_arg == "claude"
    assert _event_runners(coord, "T_REJECT_CLAUDE", "reject_review") == ["claude"]


def test_reject_review_manager_route_defaults_to_codex_without_route(coord):
    """With no verified manager route (token-only coordinator seat) the
    historical ``codex`` attribution is preserved."""
    _insert(coord, "T_REJECT_CODEX")

    res = core.reject_review("T_REJECT_CODEX", "rework this", to="pending")

    assert res["ok"] is True, res
    runner_arg = res["command"][res["command"].index("--runner") + 1]
    assert runner_arg == "codex"
    assert _event_runners(coord, "T_REJECT_CODEX", "reject_review") == ["codex"]


def test_archive_attributes_to_verified_claude_manager_route(coord, monkeypatch):
    _use_claude_route(monkeypatch)
    _insert(coord, "T_ARCH_CLAUDE", worker_status="unclaimed", status="pending")

    res = core.archive_task("T_ARCH_CLAUDE", reason="stale")

    assert res["ok"] is True, res
    runner_arg = res["command"][res["command"].index("--runner") + 1]
    assert runner_arg == "claude"
    # task_store records the audit event runner from the actor it was given.
    assert _event_runners(coord, "T_ARCH_CLAUDE", "archived") == ["claude"]


def test_verified_manager_actor_resolves_route_over_hardcoded_name(coord, monkeypatch):
    assert core._verified_manager_actor() == "codex"
    _use_claude_route(monkeypatch)
    assert core._verified_manager_actor() == "claude"


def test_archive_unattributable_action_fails_rather_than_defaulting(tmp_path, monkeypatch):
    """No coordinator token and no verified manager route -> the action is
    unattributable and must fail, never silently attribute to a name."""
    root = _init_repo(tmp_path, "repo_noc")
    (root / ".aiworkhub" / "runtime" / "coordinator.token").unlink()
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.delenv("BITNN_TASKCTL_COORDINATOR_TOKEN", raising=False)
    monkeypatch.delenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", raising=False)
    monkeypatch.setattr(aiworkhub, "_coordinator_token", "", raising=False)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    _insert(root, "T_NOAUTH", worker_status="unclaimed", status="pending")

    res = core.archive_task("T_NOAUTH", reason="x")

    assert res["ok"] is False
    assert not str(_row(root, "T_NOAUTH")["archived_at"]).strip()


# --- NF-2026-00280: dead archive-repair surfaces are now reachable ---------

def test_archive_reconciliation_report_reachable_via_taskctl(coord):
    result = core.run_taskctl(["archive-reconciliation-report"])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)


def test_repair_archive_inconsistencies_reachable_via_taskctl(coord, monkeypatch):
    """The repair mutation is reachable through the dispatcher, but only for a
    verified coordinator: a live Claude manager route clears
    ``_verify_coordinator_capability`` and the repair runs."""
    _use_claude_route(monkeypatch)
    result = core.run_taskctl(
        ["repair-archive-inconsistencies", "--actor", "operator"],
        allow_write=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)


def test_repair_archive_inconsistencies_refused_without_coordinator_capability(coord, monkeypatch):
    """The mutating repair must be refused when nothing proves coordinator
    capability -- even though ``AIWORKHUB_ALLOW_WRITES`` is set. Before the fix
    the bare write gate let this through; now the coordinator capability check
    (no Claude route, runner is not codex) blocks it with a named reason."""
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    result = core.run_taskctl(
        ["repair-archive-inconsistencies", "--actor", "operator"],
        allow_write=True,
    )
    assert result.returncode == 126, result.stdout
    assert "coordinator" in result.stderr.lower(), result.stderr
