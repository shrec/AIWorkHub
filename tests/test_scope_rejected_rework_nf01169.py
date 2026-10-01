"""NF-2026-01169: an explicit, audited manager resolution of a scope_rejected
card recovers the same task ID from a clean root.  Without the opt-in every
call still fails closed with ``hard_blocker:scope_rejected``."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import core, task_store

_RUNNER = "codex_worker_test"
_TOPIC = "aiworkhub_blocked_rework_recovery"
_NOW = "2026-08-06T00:00:00+00:00"
_REQUEST_ID = "c" * 32
_SCOPE_ERROR = "scope_violation:.gitignore outside allowed_writes"
_FEEDBACK = "Manager resolved scope: never write .gitignore; edit task_store.py only"


def _setup_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    return repo


def _db_path(repo: Path) -> Path:
    _readiness, db_path = task_store._require_ready(repo)
    return Path(db_path)


def _workspace(repo: Path) -> Path:
    return repo / ".aiworkhub" / "runtime" / "worktrees" / _REQUEST_ID / "worktree"


_UNSET = object()


def _insert_terminal_task(
    repo: Path,
    task_id: str,
    *,
    terminal_substatus: str = "scope_rejected",
    request_id: str = _REQUEST_ID,
    reject_review_reason: str = "",
    with_predecessor: bool = False,
    terminal_request_id: object = _UNSET,
    evidence_request_id: object = _UNSET,
    request_identity: dict | None = None,
    card_substatus: object = _UNSET,
) -> None:
    """Insert a blocked card whose newest terminal_review is ``terminal_substatus``.

    The ``*_request_id``, ``request_identity`` and ``card_substatus`` overrides
    let a test make the episode evidence diverge from the card's live state.
    """
    evidence = {
        "error": _SCOPE_ERROR,
        "request_id": (
            request_id if evidence_request_id is _UNSET else evidence_request_id
        ),
        "changed_path_hashes": {"src/aiworkhub/task_store.py": "a" * 64},
    }
    if request_identity is not None:
        evidence["request_identity"] = request_identity
    terminal = {
        "substatus": terminal_substatus,
        "evidence": evidence,
        "recorded_at": _NOW,
        "runner": _RUNNER,
        "claim_epoch": 1,
        "request_id": (
            request_id if terminal_request_id is _UNSET else terminal_request_id
        ),
    }
    card = {
        "task_id": task_id,
        "runner": _RUNNER,
        "topic": _TOPIC,
        "mode": "",
        "allowed_writes": ["src/aiworkhub/task_store.py"],
        "objective": "Recover a scope-rejected card",
        "terminal_review": terminal,
        "terminal_substatus": (
            terminal_substatus if card_substatus is _UNSET else card_substatus
        ),
        "blocker_reason": _SCOPE_ERROR,
        "blocked_at": _NOW,
        "blocked_by": _RUNNER,
        "claim_epoch": 1,
        "launch_request_id": request_id,
    }
    if reject_review_reason:
        card["reject_review"] = {
            "to": "pending",
            "reason": reject_review_reason,
            "recorded_at": _NOW,
        }
    if with_predecessor:
        card["rework_predecessor"] = {
            "schema_id": "aiworkhub.rework_predecessor.v1",
            "request_id": request_id,
            "task_id": task_id,
            "repo": str(repo.resolve()),
            "claim_epoch": 1,
            "allowed_writes": ["src/aiworkhub/task_store.py"],
            "changed_paths": ["src/aiworkhub/task_store.py"],
            "changed_path_hashes": {"src/aiworkhub/task_store.py": "a" * 64},
            "workspace": {
                "request_id": request_id,
                "repo": str(repo.resolve()),
                "path": str(_workspace(repo)),
            },
        }
    conn = sqlite3.connect(_db_path(repo))
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, "
            "started_at, completed_at) "
            "VALUES (?, ?, ?, 'blocked', ?, '', '', ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, _RUNNER, _TOPIC, terminal_substatus, json.dumps(card),
                _NOW, _NOW, _RUNNER, _NOW, _NOW, _NOW,
            ),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'terminal_review', ?, ?, ?)",
            (task_id, _RUNNER, json.dumps(terminal), _NOW),
        )
        conn.commit()
    finally:
        conn.close()


def _row(repo: Path, task_id: str) -> tuple[str, dict]:
    conn = sqlite3.connect(_db_path(repo))
    try:
        status, card_json = conn.execute(
            "SELECT status, card_json FROM tasks WHERE task_id=?", (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return status, json.loads(card_json)


def _raw_card(repo: Path, task_id: str) -> str:
    conn = sqlite3.connect(_db_path(repo))
    try:
        return conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def _events(repo: Path, task_id: str) -> list[tuple]:
    conn = sqlite3.connect(_db_path(repo))
    try:
        return conn.execute(
            "SELECT rowid, event, runner, payload_json, created_at FROM task_events "
            "WHERE task_id=? ORDER BY rowid",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _assert_scope_recovery(
    repo: Path, task_id: str, before_events: list[tuple]
) -> dict:
    status, card = _row(repo, task_id)
    assert status == "pending"
    assert card["task_id"] == task_id
    assert "rework_predecessor" not in card
    assert card["claim_epoch"] == 2
    authorization = card["clean_root_recovery_authorization"]
    assert authorization["recovery_mode"] == "clean_root_scope_rejection_resolved"
    assert card["recovery_mode"] == "clean_root_scope_rejection_resolved"
    assert authorization["rejected_request_id"] == _REQUEST_ID
    assert authorization["feedback_sha256"] == hashlib.sha256(
        _FEEDBACK.encode("utf-8")
    ).hexdigest()
    assert authorization["scope_violation"] == _SCOPE_ERROR
    assert authorization["task_id"] == task_id
    assert authorization["claim_epoch"] == 2
    assert authorization["one_episode_binding"] is True
    assert card["recovery_feedback"] == _FEEDBACK

    after_events = _events(repo, task_id)
    assert after_events[: len(before_events)] == before_events
    appended = after_events[len(before_events):]
    assert [event[1] for event in appended] == ["blocked_rework_recovery"]
    payload = json.loads(appended[0][3])
    assert payload["terminal_substatus"] == "scope_rejected"
    assert payload["clean_root_recovery_authorization"] == authorization
    # Unverified digests from the rejected episode never land under the key
    # the verified clean-root modes use.
    assert "changed_path_hashes" not in authorization
    assert "changed_path_hashes" not in payload["clean_root_recovery_authorization"]
    return card


def test_nf01169_scope_rejected_missing_worktree_recovers_only_with_flag(
    tmp_path: Path,
) -> None:
    repo = _setup_repo(tmp_path)
    task_id = "SCOPE_REJECTED_NF01169"
    _insert_terminal_task(repo, task_id)
    assert not _workspace(repo).exists()
    before_card = _raw_card(repo, task_id)
    before_events = _events(repo, task_id)

    assert task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator", feedback_reason=_FEEDBACK,
    ) == (False, "hard_blocker:scope_rejected")
    assert _raw_card(repo, task_id) == before_card
    assert _events(repo, task_id) == before_events

    assert task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator", feedback_reason=_FEEDBACK,
        scope_rejection_resolved=True,
    ) == (True, "recovered")
    _assert_scope_recovery(repo, task_id, before_events)


@pytest.mark.parametrize("clean_root_too", [False, True])
def test_nf01169_present_worktree_is_untouched_and_not_materialized(
    tmp_path: Path, clean_root_too: bool,
) -> None:
    repo = _setup_repo(tmp_path)
    task_id = "SCOPE_REJECTED_PRESENT_NF01169"
    workspace = _workspace(repo)
    (workspace / "src" / "aiworkhub").mkdir(parents=True)
    (workspace / ".gitignore").write_bytes(b"out-of-scope write\n")
    (workspace / "src" / "aiworkhub" / "task_store.py").write_bytes(b"unsealed\n")
    _insert_terminal_task(repo, task_id, with_predecessor=True)
    before = _snapshot(workspace)
    before_events = _events(repo, task_id)

    assert task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator", feedback_reason=_FEEDBACK,
        scope_rejection_resolved=True,
        clean_root_if_predecessor_missing=clean_root_too,
    ) == (True, "recovered")

    card = _assert_scope_recovery(repo, task_id, before_events)
    assert _snapshot(workspace) == before
    assert not (repo / ".gitignore").exists()
    assert not (repo / "src" / "aiworkhub" / "task_store.py").exists()
    assert "rework_delta" not in json.dumps(card)


@pytest.mark.parametrize("feedback", ["", "   "])
def test_nf01169_empty_feedback_is_refused_despite_stored_reason(
    tmp_path: Path, feedback: str,
) -> None:
    repo = _setup_repo(tmp_path)
    task_id = "SCOPE_REJECTED_NO_FEEDBACK_NF01169"
    _insert_terminal_task(repo, task_id, reject_review_reason="stored residual")
    before_card = _raw_card(repo, task_id)
    before_events = _events(repo, task_id)

    assert task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator", feedback_reason=feedback,
        scope_rejection_resolved=True,
    ) == (False, "scope_rejection_resolution_requires_feedback")
    assert _raw_card(repo, task_id) == before_card
    assert _events(repo, task_id) == before_events


def test_nf01169_validation_only_replay_is_refused(tmp_path: Path) -> None:
    repo = _setup_repo(tmp_path)
    task_id = "SCOPE_REJECTED_REPLAY_NF01169"
    _insert_terminal_task(repo, task_id)
    before_card = _raw_card(repo, task_id)

    assert task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator", feedback_reason=_FEEDBACK,
        validation_only_replay=True, scope_rejection_resolved=True,
    ) == (False, "scope_rejection_resolution_incompatible_with_validation_replay")
    assert _row(repo, task_id)[0] == "blocked"
    assert _raw_card(repo, task_id) == before_card


@pytest.mark.parametrize(
    "substatus", ["dependency_blocked", "blocked", "validation_failed"]
)
def test_nf01169_other_substatuses_are_not_applicable(
    tmp_path: Path, substatus: str,
) -> None:
    repo = _setup_repo(tmp_path)
    task_id = f"NOT_SCOPE_{substatus.upper()}_NF01169"
    _insert_terminal_task(repo, task_id, terminal_substatus=substatus)
    before_card = _raw_card(repo, task_id)
    before_events = _events(repo, task_id)

    assert task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator", feedback_reason=_FEEDBACK,
        scope_rejection_resolved=True,
    ) == (False, f"scope_rejection_resolution_not_applicable:{substatus}")
    assert _row(repo, task_id)[0] == "blocked"
    assert _raw_card(repo, task_id) == before_card
    assert _events(repo, task_id) == before_events


def test_nf01169_missing_rejected_request_identity_is_refused(tmp_path: Path) -> None:
    repo = _setup_repo(tmp_path)
    task_id = "SCOPE_REJECTED_NO_IDENTITY_NF01169"
    _insert_terminal_task(repo, task_id, request_id="")

    assert task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator", feedback_reason=_FEEDBACK,
        scope_rejection_resolved=True,
    ) == (False, "scope_rejection_resolution_request_identity_missing")
    assert _row(repo, task_id)[0] == "blocked"


_OTHER_REQUEST_ID = "d" * 32


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"terminal_request_id": "", "evidence_request_id": _OTHER_REQUEST_ID},
            id="evidence_id_differs_from_launch",
        ),
        pytest.param(
            {"terminal_request_id": _OTHER_REQUEST_ID},
            id="terminal_id_differs_from_launch",
        ),
        pytest.param(
            {"request_id": "Z" * 32}, id="non_hex_id",
        ),
        pytest.param(
            {"request_id": "c" * 31}, id="wrong_length_id",
        ),
        pytest.param(
            {
                "request_identity": {
                    "request_id": _REQUEST_ID, "task_id": "ANOTHER_TASK",
                },
            },
            id="identity_of_another_task",
        ),
    ],
)
def test_nf01169_unverified_request_identity_is_refused(
    tmp_path: Path, overrides: dict,
) -> None:
    repo = _setup_repo(tmp_path)
    task_id = "SCOPE_REJECTED_BAD_IDENTITY_NF01169"
    _insert_terminal_task(repo, task_id, **overrides)
    before_card = _raw_card(repo, task_id)
    before_events = _events(repo, task_id)

    assert task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator", feedback_reason=_FEEDBACK,
        scope_rejection_resolved=True,
    ) == (False, "scope_rejection_resolution_request_identity_invalid")
    assert _row(repo, task_id)[0] == "blocked"
    assert _raw_card(repo, task_id) == before_card
    assert _events(repo, task_id) == before_events


def test_nf01169_live_card_substatus_overrides_stale_terminal_event(
    tmp_path: Path,
) -> None:
    repo = _setup_repo(tmp_path)
    task_id = "SCOPE_REJECTED_STALE_EVENT_NF01169"
    _insert_terminal_task(repo, task_id, card_substatus="dependency_blocked")
    before_card = _raw_card(repo, task_id)
    before_events = _events(repo, task_id)

    assert task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator", feedback_reason=_FEEDBACK,
        scope_rejection_resolved=True,
    ) == (False, "scope_rejection_resolution_not_applicable:dependency_blocked")
    assert _row(repo, task_id)[0] == "blocked"
    assert _raw_card(repo, task_id) == before_card
    assert _events(repo, task_id) == before_events


@pytest.mark.parametrize("clean_root_too", [False, True])
def test_nf01169_replay_is_idempotent_and_history_append_only(
    tmp_path: Path, clean_root_too: bool,
) -> None:
    repo = _setup_repo(tmp_path)
    task_id = "SCOPE_REJECTED_IDEMPOTENT_NF01169"
    _insert_terminal_task(repo, task_id)
    assert task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator", feedback_reason=_FEEDBACK,
        scope_rejection_resolved=True,
    ) == (True, "recovered")
    recovered_card = _raw_card(repo, task_id)
    recovered_events = _events(repo, task_id)

    assert task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator", feedback_reason=_FEEDBACK,
        scope_rejection_resolved=True,
        clean_root_if_predecessor_missing=clean_root_too,
    ) == (True, "already_recovered")
    assert _raw_card(repo, task_id) == recovered_card
    assert _events(repo, task_id) == recovered_events


def test_nf01169_core_forwards_flag_behind_manager_gate(monkeypatch) -> None:
    calls: list[tuple] = []
    card = {"task_id": "T_SCOPE", "topic": "blocked_rework"}
    monkeypatch.setattr(core, "_live_card", lambda task_id: (card, None))

    def gate(action, **kwargs):
        calls.append(("gate", action, kwargs))
        return None

    def recover(root, task_id, **kwargs):
        calls.append(("recover", task_id, kwargs))
        return True, "recovered"

    monkeypatch.setattr(core, "_canonical_write_gate", gate)
    monkeypatch.setattr(task_store, "recover_blocked_rework", recover)
    monkeypatch.setattr(task_store, "get_task", lambda root, task_id: card)
    monkeypatch.setattr(core, "_reconcile_retained_workspaces", lambda result: result)

    result = core.recover_blocked_rework(
        "T_SCOPE", feedback_reason=" resolved ", topic="blocked_rework",
        scope_rejection_resolved=True,
    )
    default = core.recover_blocked_rework(
        "T_SCOPE", feedback_reason="plain", topic="blocked_rework",
    )

    assert result["ok"] is True and default["ok"] is True
    gates = [call for call in calls if call[0] == "gate"]
    assert gates and all(
        call[1] == "recover-blocked-rework"
        and call[2]["coordinator_capability"] is True
        for call in gates
    )
    recovers = [call for call in calls if call[0] == "recover"]
    assert recovers[0][2]["scope_rejection_resolved"] is True
    assert recovers[0][2]["feedback_reason"] == "resolved"
    assert recovers[0][2]["actor"] == core.CODEX_RUNNER
    assert "scope_rejection_resolved" not in recovers[1][2]


def test_nf01169_core_refuses_without_verified_manager(monkeypatch) -> None:
    card = {"task_id": "T_SCOPE", "topic": "blocked_rework"}
    monkeypatch.setattr(core, "_live_card", lambda task_id: (card, None))
    refusal = {"ok": False, "returncode": 1, "stderr": "write_gate_refused"}
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **k: refusal)
    monkeypatch.setattr(
        task_store,
        "recover_blocked_rework",
        lambda *a, **k: pytest.fail("task store must not run when the gate refuses"),
    )

    assert core.recover_blocked_rework(
        "T_SCOPE", feedback_reason="resolved", scope_rejection_resolved=True,
    ) is refusal
