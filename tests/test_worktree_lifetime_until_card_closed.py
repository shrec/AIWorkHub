"""A worker worktree lives until its card is closed (NF-2026-01661).

A worker that dies -- restart, power loss, network drop, token expiry, rate limit,
``transient_retry``, dead-process reconcile -- leaves its card pending or blocked,
never finished. That worktree is the only copy of the attempt's evidence and the
seed a relaunch builds on, so it is deleted only once the card is finished,
archived or superseded, or its NeedFix is terminal.

Two sweepers enforce the rule and both are exercised here: the finalized-workspace
GC in ``process_launcher`` (``ProcessManager._gc_finalized_workspaces``) and the
reclaim plan in ``storage_retention`` (``plan_worktree_reclaim``, ``preview`` and
``quarantine``). Age is injected through ``now=``: workers run under a sandbox that
forbids ``os.utime``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _StubCredentialError(Exception):
    def __init__(self, reason: str = "deepseek_credential_stub_environment") -> None:
        super().__init__(reason)
        self.reason = reason


class _StubProjectContextResult:
    def __init__(self, metadata=None, prompt_bundle: str = "") -> None:
        self.metadata = metadata or {}
        self.prompt_bundle = prompt_bundle


def _no_credential(repo=None):  # noqa: ANN001, ARG001
    raise _StubCredentialError()


def _stub_if_unimportable(name: str, **attributes: object) -> None:
    """Some isolated worktrees lack ``deepseek_credentials.py`` and
    ``project_context.py`` although ``process_launcher`` imports both at module
    scope. Install a minimal stand-in only when the module file is genuinely
    absent: a real module that fails to import must fail the run, not hide
    behind the stub."""
    import importlib.util
    import types

    if importlib.util.find_spec(name) is not None:
        return
    stub = types.ModuleType(name)
    vars(stub).update(attributes)
    sys.modules[name] = stub


_stub_if_unimportable(
    "aiworkhub.deepseek_credentials",
    CredentialError=_StubCredentialError,
    load_credential=_no_credential,
    adapter_readiness=lambda repo=None: {"ok": True, "readonly": True, "adapters": []},
)
_stub_if_unimportable(
    "aiworkhub.project_context",
    ProjectContextError=type("ProjectContextError", (Exception,), {}),
    ProjectContextResult=_StubProjectContextResult,
    collect_project_context=lambda repo, card: None,
    RECEIPT_SCHEMA_ID="aiworkhub.task_mcp.project_context_receipt.v1",
)

from aiworkhub import (  # noqa: E402
    needfix_store,
    process_launcher,
    storage_retention,
    task_store,
    worker_workspace,
    worktree_storage,
)
from support.retention import add_clean_worktree, aged_now, repository  # noqa: E402


def _chmod_blocked_by_sandbox() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as name:
        try:
            os.chmod(name, 0o700)
        except PermissionError:
            return True
    return False


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize ``os.chmod``/``os.fchmod`` only where the sandbox rejects the
    bare syscall (probed once); a no-op everywhere else."""
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


# --- finalized-workspace GC (process_launcher) -------------------------------

_KEPT = {"gc_scanned": 1, "gc_cleaned": 0, "gc_skipped": 1}
_SWEPT = {"gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0}
_DEAD_PID = 2_147_483_100
_DEAD_PID_START_TICKS = 999_999_100


def _card(status: str, worker_status: str) -> dict:
    return {
        "task_id": "TASK_LIFETIME",
        "runner": "claude_worker_lifetime",
        "topic": "task_mcp",
        "status": status,
        "worker_status": worker_status,
        "claimed_by": "",
        "archived_at": "",
        "allowed_writes": ["out/result.txt"],
    }


def _build_manager(tmp_path: Path, card: dict) -> process_launcher.ProcessManager:
    def show_task(_task_id: str) -> dict:
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    return process_launcher.ProcessManager(
        repo=tmp_path / "repo",
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=show_task,
        collision_guard=lambda **_kwargs: {
            "returncode": 0, "stdout": '{"collision_free":true}', "stderr": "",
        },
        adapter_builder=lambda **_k: SimpleNamespace(
            argv=[], cwd=str(tmp_path), launchable=True, reason=""
        ),
        isolation_enabled=True,
    )


def _seed_dead_attempt(
    manager: process_launcher.ProcessManager,
    tmp_path: Path,
    card: dict,
    *,
    request_id: str,
    state: str,
) -> tuple[Path, Path]:
    """Persist one retained attempt whose worker process is gone, at the exact
    ``<root>/<request_id>/{worktree,home}`` shape the sweep accepts."""
    path = tmp_path / "wtroot" / request_id / "worktree"
    home = path.parent / "home"
    for directory in (path, home):
        directory.mkdir(parents=True)
        (directory / "evidence.txt").write_text("retained worker output")
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = process_dir / f"{request_id}.request.json"
    worker_workspace.write_json_0600(metadata_path, {
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "model": None,
        "stdout_path": str(process_dir / f"{request_id}.stdout.log"),
        "stderr_path": str(process_dir / f"{request_id}.stderr.log"),
        "supervisor_status_path": str(process_dir / f"{request_id}.supervisor.json"),
        "cancel_path": str(process_dir / f"{request_id}.cancel.json"),
        "metadata_path": str(metadata_path),
        "validation": [],
        "sandbox_backend": "landlock",
        "workspace": {
            "request_id": request_id,
            "repo": str(tmp_path / "repo"),
            "path": str(path),
            "home": str(home),
            "allowed_writes": list(card["allowed_writes"]),
            "parent_baseline": {},
            "workspace_baseline": {},
        },
    })
    manager._append_event({
        "request_id": request_id,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "state": state,
        "pid": _DEAD_PID,
        "pid_start_ticks": _DEAD_PID_START_TICKS,
        "metadata_path": str(metadata_path),
        "workspace_retained": True,
        "error": "",
    })
    return path, home


def test_worker_failed_attempt_of_a_blocked_card_survives_the_sweep(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card("blocked", "blocked")
    manager = _build_manager(tmp_path, card)
    path, home = _seed_dead_attempt(
        manager, tmp_path, card, request_id="req-blocked", state="worker_failed"
    )

    assert manager._gc_finalized_workspaces() == _KEPT
    assert manager._gc_finalized_workspaces() == _KEPT
    assert path.exists() and home.exists()
    assert "workspace_gc" not in manager._request_events("req-blocked")[-1]
    assert process_launcher.ProcessManager._gc_disposition(
        card, "req-blocked", process_state="worker_failed"
    ) == (False, "task_not_disposed:blocked")


@pytest.mark.parametrize("state", ["worker_failed", "exited_without_review", "timed_out"])
def test_dead_worker_attempt_of_a_pending_card_survives_the_sweep(tmp_path, monkeypatch, state):
    """``transient_retry`` requeues the card to ``pending`` and keeps its workspace
    (``task_store.mark_transient_retry``, ``cleanup=False``). The attempt's last
    process state stays terminal, so the sweep meets a dead worker on a pending card.
    """
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    request_id = f"req-pending-{state}"
    card = _card("pending", "unclaimed")
    manager = _build_manager(tmp_path, card)
    path, home = _seed_dead_attempt(
        manager, tmp_path, card, request_id=request_id, state=state
    )

    assert manager._gc_finalized_workspaces() == _KEPT
    assert path.exists() and home.exists()
    assert "workspace_gc" not in manager._request_events(request_id)[-1]
    assert process_launcher.ProcessManager._gc_disposition(
        card, request_id, process_state=state
    ) == (False, "task_not_disposed:pending")


@pytest.mark.parametrize(
    ("closed", "reason"),
    [
        (
            {"status": "finished", "worker_status": "done"},
            "disposed_task_status:finished",
        ),
        (
            {"status": "finished", "worker_status": "done", "archived_at": "2026-07-16T00:00:00Z"},
            "disposed_task_status:archived",
        ),
    ],
    ids=["finished", "archived"],
)
def test_attempt_is_collected_once_its_card_is_closed(tmp_path, monkeypatch, closed, reason):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _card("blocked", "blocked")
    manager = _build_manager(tmp_path, card)
    path, home = _seed_dead_attempt(
        manager, tmp_path, card, request_id="req-closed", state="worker_failed"
    )
    assert manager._gc_finalized_workspaces() == _KEPT

    card.update(closed)

    assert manager._gc_finalized_workspaces() == _SWEPT
    assert not path.exists() and not home.exists()
    latest = manager._request_events("req-closed")[-1]
    assert latest["state"] == "worker_failed"
    assert latest["workspace_gc_reason"] == reason


# --- storage_retention reclaim plan ------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_card(repo: Path, task_id: str, *, status: str, launch_request_id: str = "") -> None:
    card = {"launch_request_id": launch_request_id} if launch_request_id else {}
    with closing(sqlite3.connect(str(task_store.canonical_db_path(repo)))) as conn:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, "claude", "storage", status, "unclaimed",
                json.dumps(card), _now_iso(), _now_iso(),
            ),
        )
        conn.commit()


def _set_archived_at(repo: Path, task_id: str, archived_at: str) -> None:
    with closing(sqlite3.connect(str(task_store.canonical_db_path(repo)))) as conn:
        conn.execute(
            "UPDATE tasks SET archived_at=? WHERE task_id=?", (archived_at, task_id)
        )
        conn.commit()


def _insert_needfix(repo: Path, needfix_id: str, task_id: str, *, status: str) -> None:
    needfix_store.initialize_repository(repo)
    path = repo / ".aiworkhub" / "tasking" / "needfix.sqlite"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute(
            "INSERT INTO needfix(id,dedupe_key,title,description,kind,severity,"
            "provenance_json,status,converted_task_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                needfix_id, f"dedupe-{needfix_id}", needfix_id, "worktree lifetime test",
                "bug", "medium", "{}", status, task_id, _now_iso(), _now_iso(),
            ),
        )
        conn.commit()


def _entry(worktree_id: str, now: float, *, owner: str = "", age_days: int = 100) -> dict:
    """One synthetic scan entry; ``owner`` makes it a registration-less worktree
    that the request ledger ties to that card, as the real scan reports it."""
    entry = {
        "id": worktree_id,
        "class": worktree_storage.CLASS_REMOVABLE_SAFE,
        "size_bytes": 1_000_000,
        "modified_at_epoch": now - age_days * 86400,
    }
    if owner:
        entry.update(
            {
                "class": worktree_storage.CLASS_RETAINED_UNSAVED,
                "ownership_source": "request_ledger",
                "owner_task_id": owner,
            }
        )
    return entry


def _plan(env: dict[str, Path], worktrees: list[dict], now: float) -> dict:
    """Always over the byte cap, so the pull-forward pass is exercised too."""
    return storage_retention.plan_worktree_reclaim(
        env["repo"],
        {"base": env["base"], "worktrees": worktrees},
        min_age_days=30,
        max_bytes=1,
        current_bytes=10_000_000,
        now=now,
    )


def _ids(items: list[dict]) -> set[str]:
    return {item["id"] for item in items}


@pytest.mark.parametrize("status", ["pending", "blocked"])
def test_plan_keeps_every_attempt_of_an_unfinished_card_whatever_its_age(tmp_path, status):
    env = repository(tmp_path)
    now = aged_now()
    _insert_card(env["repo"], "TASK-OPEN", status=status)

    plan = _plan(env, [
        _entry("owned-aged", now, owner="TASK-OPEN", age_days=100),
        _entry("owned-young", now, owner="TASK-OPEN", age_days=1),
        _entry("unowned-aged", now, age_days=100),
        _entry("unowned-young", now, age_days=1),
    ], now)

    assert plan["protection_reasons"]["owned-aged"] == "unfinished_card_attempt"
    assert plan["protection_reasons"]["owned-young"] == "unfinished_card_attempt"
    assert {"owned-aged", "owned-young"} <= _ids(plan["would_keep"])
    assert _ids(plan["would_remove"]) == {"unowned-aged", "unowned-young"}


def test_plan_keeps_the_older_attempt_of_a_relaunched_card(tmp_path):
    env = repository(tmp_path)
    now = aged_now()
    _insert_card(env["repo"], "TASK-RELAUNCHED", status="processing", launch_request_id="req-new")

    plan = _plan(env, [
        _entry("req-new", now),
        _entry("req-old", now, owner="TASK-RELAUNCHED"),
        _entry("req-control", now),
    ], now)

    assert plan["protection_reasons"]["req-new"] == "live_worker"
    assert plan["protection_reasons"]["req-old"] == "unfinished_card_attempt"
    assert "req-old" in _ids(plan["would_keep"])
    assert _ids(plan["would_remove"]) == {"req-control"}


def test_plan_reclaims_the_attempt_once_its_card_is_archived(tmp_path):
    env = repository(tmp_path)
    now = aged_now()
    _insert_card(env["repo"], "TASK-DONE", status="pending")
    worktrees = [_entry("req-old", now, owner="TASK-DONE")]

    open_plan = _plan(env, worktrees, now)
    assert _ids(open_plan["would_keep"]) == {"req-old"}
    assert open_plan["would_remove"] == []

    _set_archived_at(env["repo"], "TASK-DONE", _now_iso())
    plan = _plan(env, worktrees, now)

    assert _ids(plan["would_remove"]) == {"req-old"}
    assert plan["would_remove"][0]["reclaim_authority"] == "terminal_task_request_ledger"
    assert "req-old" not in plan["protection_reasons"]


@pytest.mark.parametrize(
    ("needfix_status", "released"), [("archived", True), ("task_created", False)]
)
def test_terminal_needfix_releases_the_attempts_of_its_open_card(
    tmp_path, needfix_status, released
):
    env = repository(tmp_path)
    now = aged_now()
    task_id = "needfix-NF-2026-00994"
    _insert_card(env["repo"], task_id, status="blocked")
    _insert_needfix(env["repo"], "NF-2026-00994", task_id, status=needfix_status)

    plan = _plan(env, [_entry("req-old", now, owner=task_id)], now)

    if released:
        assert _ids(plan["would_remove"]) == {"req-old"}
        assert plan["would_remove"][0]["reclaim_authority"] == "terminal_needfix_request_ledger"
    else:
        assert plan["would_remove"] == []
        assert plan["protection_reasons"] == {"req-old": "unfinished_card_attempt"}


# --- preview and quarantine over a real worktree -----------------------------


def _ledger_owned_worktree(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, request_id: str, task_id: str
) -> Path:
    """A real worktree git no longer lists, whose request ledger names its card."""
    repo = env["repo"]
    entry = add_clean_worktree(repo, env["base"], request_id)
    home = entry / "home"
    home.mkdir()
    process_dir = repo / ".aiworkhub" / "runtime" / "process_logs" / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    (process_dir / f"{request_id}.request.json").write_text(
        json.dumps(
            {
                "schema_id": "aiworkhub.task_mcp.isolated_request.v1",
                "request_id": request_id,
                "task_id": task_id,
                "workspace": {
                    "repo": str(repo.resolve()),
                    "request_id": request_id,
                    "path": str((entry / "worktree").resolve()),
                    "home": str(home.resolve()),
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(worktree_storage, "_repo_registered_worktrees", lambda _repo: {})
    return entry


def test_preview_and_quarantine_keep_a_ledger_owned_worktree_until_its_card_closes(
    tmp_path, monkeypatch
):
    env = repository(tmp_path)
    repo, base = env["repo"], env["base"]
    request_id, task_id = "attempt-open-card", "TASK-OPEN-PREVIEW"
    entry = _ledger_owned_worktree(env, monkeypatch, request_id, task_id)
    _insert_card(repo, task_id, status="pending")

    preview = storage_retention.preview(repo, base=base, now=aged_now())

    assert preview["candidate_count"] == 0
    assert {"id": request_id, "reason": "unfinished_card_attempt"} in preview["protected"]
    kept = storage_retention.quarantine(
        repo, preview_digest=preview["preview_digest"], confirm=True, base=base, now=aged_now()
    )
    assert kept["quarantined"] == 0
    assert entry.is_dir()

    _set_archived_at(repo, task_id, _now_iso())
    preview = storage_retention.preview(repo, base=base, now=aged_now())

    assert [item["id"] for item in preview["candidates"]] == [request_id]
    assert preview["candidates"][0]["reclaim_authority"] == "terminal_task_request_ledger"
    moved = storage_retention.quarantine(
        repo, preview_digest=preview["preview_digest"], confirm=True, base=base, now=aged_now()
    )
    assert moved["quarantined"] == 1
    assert not entry.exists()


def test_quarantine_recheck_skips_a_card_reopened_after_the_preview(tmp_path, monkeypatch):
    env = repository(tmp_path)
    repo, base = env["repo"], env["base"]
    request_id, task_id = "attempt-reopened", "TASK-REOPENED"
    entry = _ledger_owned_worktree(env, monkeypatch, request_id, task_id)
    _insert_card(repo, task_id, status="pending")
    _set_archived_at(repo, task_id, _now_iso())
    preview = storage_retention.preview(repo, base=base, now=aged_now())
    assert [item["id"] for item in preview["candidates"]] == [request_id]

    real = storage_retention._protected_attempt_ids

    def reopen_before_the_move(root):
        # Only the commit-time read inside quarantine(); the digest scan is untouched.
        if sys._getframe(1).f_code.co_name == "quarantine":
            _set_archived_at(repo, task_id, "")
        return real(root)

    monkeypatch.setattr(storage_retention, "_protected_attempt_ids", reopen_before_the_move)
    result = storage_retention.quarantine(
        repo, preview_digest=preview["preview_digest"], confirm=True, base=base, now=aged_now()
    )

    assert result["quarantined"] == 0
    assert result["no_op"] is True
    assert entry.is_dir()
