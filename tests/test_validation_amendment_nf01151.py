"""NF-2026-01151: validation_amendment on reject_review / recover_blocked_rework."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, task_store  # noqa: E402

_NOW = "2026-09-30T00:00:00+00:00"


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


def _insert_task(
    root: Path, task_id: str, runner: str, topic: str, extra_card: dict | None = None
) -> None:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    card = {"origin_thread_id": "nf01151-thread", **(extra_card or {})}
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, origin_thread_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, runner, topic, "solo", "pending", "unclaimed", "normal", "objective",
             json.dumps(card), _NOW, _NOW, None, "nf01151-thread"),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# task_store.apply_validation_amendment: the pure merge/dedup logic reused by
# both reject_review's inline transaction and recover_blocked_rework's.
# ---------------------------------------------------------------------------

def test_apply_validation_amendment_appends_in_order_and_dedupes():
    card = {"validation": ["pytest -q tests/a.py"]}

    applied = task_store.apply_validation_amendment(
        card,
        ["pytest -q tests/a.py", "ruff check src/x.py", "ruff check src/x.py"],
    )

    assert applied == ["ruff check src/x.py"]
    assert card["validation"] == ["pytest -q tests/a.py", "ruff check src/x.py"]


def test_apply_validation_amendment_adds_generic_role_only_when_roles_list_exists():
    card = {"validation": [], "validation_roles": []}
    task_store.apply_validation_amendment(card, ["pytest -q tests/a.py"])
    assert card["validation_roles"] == ["generic"]

    card_without_roles = {"validation": []}
    task_store.apply_validation_amendment(card_without_roles, ["pytest -q tests/a.py"])
    assert "validation_roles" not in card_without_roles


def test_apply_validation_amendment_empty_input_is_a_no_op():
    card = {"validation": ["pytest -q tests/a.py"]}
    assert task_store.apply_validation_amendment(card, []) == []
    assert card["validation"] == ["pytest -q tests/a.py"]


def test_apply_validation_amendment_handles_missing_validation_key():
    card: dict = {}
    applied = task_store.apply_validation_amendment(card, ["pytest -q tests/a.py"])
    assert applied == ["pytest -q tests/a.py"]
    assert card["validation"] == ["pytest -q tests/a.py"]


# ---------------------------------------------------------------------------
# core._validate_validation_amendment_commands: task_create's own rules,
# reused rather than reimplemented.
# ---------------------------------------------------------------------------

def test_validate_validation_amendment_commands_accepts_valid_commands():
    assert core._validate_validation_amendment_commands(
        ["pytest -q tests/test_x.py"]
    ) is None


def test_validate_validation_amendment_commands_refuses_invalid_command():
    amendment_error = core._validate_validation_amendment_commands(
        ["pytest -q tests/a.py | tee o"]
    )

    assert amendment_error is not None
    assert amendment_error["ok"] is False
    assert amendment_error["returncode"] == 2
    assert amendment_error["stderr"].startswith("invalid_validation_command:")


def test_validate_validation_amendment_commands_enforces_max_paths_per_field():
    from aiworkhub import task_templates

    oversized = ["pytest -q tests/test_x.py"] * (task_templates.MAX_PATHS_PER_FIELD + 1)

    amendment_error = core._validate_validation_amendment_commands(oversized)

    assert amendment_error == {
        "ok": False,
        "returncode": 2,
        "command": [],
        "stdout": "",
        "stderr": "invalid_validation",
    }


# ---------------------------------------------------------------------------
# core.reject_review wiring: refuse before any write; omission is unchanged.
# ---------------------------------------------------------------------------

def test_core_reject_review_refuses_invalid_amendment_before_write(monkeypatch):
    monkeypatch.setattr(
        core, "_live_card", lambda task_id: ({"task_id": task_id, "topic": "t"}, None)
    )
    monkeypatch.setattr(
        core,
        "_canonical_write_gate",
        lambda *a, **kw: pytest.fail("write gate must not run when amendment is invalid"),
    )

    result = core.reject_review(
        "T1", "reason", topic="t", validation_amendment=["pytest -q tests/a.py | tee o"]
    )

    assert result["ok"] is False
    assert result["stderr"].startswith("invalid_validation_command:")


# ---------------------------------------------------------------------------
# core.recover_blocked_rework wiring
# ---------------------------------------------------------------------------

def test_core_recover_blocked_rework_forwards_validation_amendment(monkeypatch):
    calls = []
    card = {"task_id": "T_BLOCKED", "topic": "blocked_rework"}
    monkeypatch.setattr(core, "_live_card", lambda task_id: (card, None))
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)

    def recover(
        root,
        task_id,
        *,
        actor,
        feedback_reason,
        validation_only_replay=False,
        clean_root_if_predecessor_missing=False,
        validation_amendment=None,
        applied_out=None,
    ):
        calls.append(validation_amendment)
        if applied_out is not None:
            applied_out.extend(validation_amendment or [])
        return True, "recovered"

    monkeypatch.setattr(task_store, "recover_blocked_rework", recover)
    post_card = {
        "task_id": "T_BLOCKED",
        "validation": ["pytest -q tests/test_new.py"],
    }
    monkeypatch.setattr(task_store, "get_task", lambda root, task_id: post_card)
    monkeypatch.setattr(core, "_reconcile_retained_workspaces", lambda result: result)

    result = core.recover_blocked_rework(
        "T_BLOCKED",
        topic="blocked_rework",
        validation_amendment=["pytest -q tests/test_new.py"],
    )

    assert result["ok"] is True
    assert calls == [["pytest -q tests/test_new.py"]]
    assert result["validation_amendment_applied"] == ["pytest -q tests/test_new.py"]


def test_core_recover_blocked_rework_forwards_amendment_with_validation_only_replay(
    monkeypatch,
):
    calls = []
    card = {"task_id": "T_BLOCKED", "topic": "blocked_rework", "status": "blocked"}
    monkeypatch.setattr(core, "_live_card", lambda task_id: (card, None))
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)
    monkeypatch.setattr(
        core, "_validation_replay_predecessor_gate_missing", lambda card, *, task_id: False
    )

    def recover(
        root,
        task_id,
        *,
        actor,
        feedback_reason,
        validation_only_replay=False,
        clean_root_if_predecessor_missing=False,
        validation_amendment=None,
        applied_out=None,
    ):
        calls.append((validation_only_replay, validation_amendment))
        return True, "recovered"

    monkeypatch.setattr(task_store, "recover_blocked_rework", recover)
    monkeypatch.setattr(task_store, "get_task", lambda root, task_id: card)
    monkeypatch.setattr(core, "_reconcile_retained_workspaces", lambda result: result)

    result = core.recover_blocked_rework(
        "T_BLOCKED",
        topic="blocked_rework",
        validation_only_replay=True,
        validation_amendment=["pytest -q tests/test_replay.py"],
    )

    assert result["ok"] is True
    assert calls == [(True, ["pytest -q tests/test_replay.py"])]


def test_core_recover_blocked_rework_refuses_invalid_amendment_before_write(monkeypatch):
    monkeypatch.setattr(
        core, "_live_card", lambda task_id: ({"task_id": task_id, "topic": "t"}, None)
    )
    monkeypatch.setattr(
        core,
        "_canonical_write_gate",
        lambda *a, **kw: pytest.fail("write gate must not run when amendment is invalid"),
    )

    result = core.recover_blocked_rework(
        "T_BLOCKED", topic="t", validation_amendment=["pytest -q tests/a.py | tee o"]
    )

    assert result["ok"] is False
    assert result["stderr"].startswith("invalid_validation_command:")


def test_core_recover_blocked_rework_omits_amendment_kwarg_when_not_provided(monkeypatch):
    calls = []
    card = {"task_id": "T_BLOCKED", "topic": "blocked_rework"}
    monkeypatch.setattr(core, "_live_card", lambda task_id: (card, None))
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)

    def recover(
        root,
        task_id,
        *,
        actor,
        feedback_reason,
        validation_only_replay=False,
        clean_root_if_predecessor_missing=False,
    ):
        calls.append("called-without-amendment-kwarg")
        return True, "recovered"

    monkeypatch.setattr(task_store, "recover_blocked_rework", recover)
    monkeypatch.setattr(task_store, "get_task", lambda root, task_id: card)
    monkeypatch.setattr(core, "_reconcile_retained_workspaces", lambda result: result)

    result = core.recover_blocked_rework("T_BLOCKED", topic="blocked_rework")

    assert result["ok"] is True
    assert calls == ["called-without-amendment-kwarg"]
    assert "validation_amendment_applied" not in result


# ---------------------------------------------------------------------------
# Real task_store transaction: reject_review actually persists the amendment.
# ---------------------------------------------------------------------------

def test_reject_review_real_transaction_appends_and_dedupes_validation_amendment(
    monkeypatch, tmp_path
):
    root = _init_repo(tmp_path)
    runner = "claude_task_mcp_runtime_wiring"
    topic = "task_mcp"
    task_id = "NF01151_REJECT_APPLY"
    _insert_task(
        root,
        task_id,
        runner,
        topic,
        extra_card={
            "validation": ["pytest -q tests/existing.py"],
            "validation_roles": ["generic"],
        },
    )

    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "nf01151-manager")
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)

    assert core.claim_start_exact(task_id, runner, topic)["ok"] is True
    assert core.mark_review(task_id, runner=runner, topic=topic)["ok"] is True

    result = core.reject_review(
        task_id,
        "add missing coverage",
        topic=topic,
        validation_amendment=[
            "pytest -q tests/existing.py",
            "pytest -q tests/test_extra_nf01151.py",
        ],
    )

    assert result["ok"] is True, result
    assert result["validation_amendment_applied"] == ["pytest -q tests/test_extra_nf01151.py"]
    card = task_store.get_task(root, task_id)
    assert card["validation"] == [
        "pytest -q tests/existing.py",
        "pytest -q tests/test_extra_nf01151.py",
    ]
    assert card["validation_roles"] == ["generic", "generic"]


def test_reject_review_real_transaction_omitted_amendment_is_unchanged(monkeypatch, tmp_path):
    root = _init_repo(tmp_path)
    runner = "claude_task_mcp_runtime_wiring"
    topic = "task_mcp"
    task_id = "NF01151_REJECT_OMIT"
    _insert_task(root, task_id, runner, topic)

    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "nf01151-manager")
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)

    assert core.claim_start_exact(task_id, runner, topic)["ok"] is True
    assert core.mark_review(task_id, runner=runner, topic=topic)["ok"] is True

    result = core.reject_review(task_id, "repair", topic=topic)

    assert result["ok"] is True, result
    assert "validation_amendment_applied" not in result
    card = task_store.get_task(root, task_id)
    assert "validation" not in card


def _seed_recoverable_blocked_card(root, task_id, *, validation):
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        row = conn.execute(
            "SELECT card_json, runner FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        blocked_card = json.loads(row[0])
        runner = row[1]
        changed_path_hashes = {"src/aiworkhub/core.py": "b" * 64}
        workspace_path = str(root / "nf01151_missing_workspace")
        blocked_card["validation"] = list(validation)
        blocked_card["rework_predecessor"] = {
            "request_id": "a" * 32,
            "changed_path_hashes": changed_path_hashes,
            "workspace": {
                # clean_root_predecessor_authority refuses with
                # clean_root_rework_identity_mismatch unless the retained
                # workspace carries the same request id as the predecessor.
                "request_id": "a" * 32,
                "repo": str(root.resolve()),
                "path": workspace_path,
            },
        }
        conn.execute(
            "UPDATE tasks SET card_json=?, status='blocked', worker_status='blocked', "
            "claimed_by=NULL, claimed_at=NULL WHERE task_id=?",
            (json.dumps(blocked_card), task_id),
        )
        terminal_review_payload = {
            "request_id": "a" * 32,
            "task_id": task_id,
            "claim_epoch": blocked_card.get("claim_epoch"),
            "evidence": {
                "request_identity": {
                    "request_id": "a" * 32,
                    "task_id": task_id,
                    "repo": str(root.resolve()),
                },
                "workspace": {
                    "request_id": "a" * 32,
                    "repo": str(root.resolve()),
                    "path": workspace_path,
                },
                "changed_path_hashes": changed_path_hashes,
            },
        }
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'terminal_review', ?, ?, ?)",
            (task_id, runner, json.dumps(terminal_review_payload), _NOW),
        )
        conn.commit()
    finally:
        conn.close()
    return blocked_card


def test_core_recover_blocked_rework_real_transaction_dedupes_against_stale_live_card_snapshot(
    monkeypatch, tmp_path
):
    root = _init_repo(tmp_path)
    runner = "claude_task_mcp_runtime_wiring"
    topic = "task_mcp"
    task_id = "NF01151_RECOVER_DEDUPE"
    existing_cmd = "pytest -q tests/test_existing_nf01151.py"
    _insert_task(root, task_id, runner, topic)

    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "nf01151-manager")
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)

    assert core.claim_start_exact(task_id, runner, topic)["ok"] is True
    assert core.mark_review(task_id, runner=runner, topic=topic)["ok"] is True
    reject_result = core.reject_review(task_id, "add missing coverage", topic=topic)
    assert reject_result["ok"] is True, reject_result

    # The DB row (read fresh inside the recovery transaction) already has the
    # amendment command applied -- e.g. by a concurrent writer. Point
    # core._live_card at a stale snapshot that lacks it, reproducing the race
    # between core's pre-transaction read and the task_store transaction.
    blocked_card = _seed_recoverable_blocked_card(
        root, task_id, validation=[existing_cmd]
    )
    stale_card = dict(blocked_card)
    stale_card["validation"] = []
    monkeypatch.setattr(core, "_live_card", lambda task_id: (stale_card, None))

    result = core.recover_blocked_rework(
        task_id,
        topic=topic,
        feedback_reason="addressed",
        clean_root_if_predecessor_missing=True,
        validation_amendment=[existing_cmd],
    )

    assert result["ok"] is True, result
    assert result["validation_amendment_applied"] == []
    card = task_store.get_task(root, task_id)
    assert card["validation"] == [existing_cmd]


def test_core_recover_blocked_rework_real_transaction_reports_genuinely_new_applied_commands(
    monkeypatch, tmp_path
):
    root = _init_repo(tmp_path)
    runner = "claude_task_mcp_runtime_wiring"
    topic = "task_mcp"
    task_id = "NF01151_RECOVER_NEW"
    new_cmd = "pytest -q tests/test_new_nf01151.py"
    _insert_task(root, task_id, runner, topic)

    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "nf01151-manager")
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)

    assert core.claim_start_exact(task_id, runner, topic)["ok"] is True
    assert core.mark_review(task_id, runner=runner, topic=topic)["ok"] is True
    reject_result = core.reject_review(task_id, "add missing coverage", topic=topic)
    assert reject_result["ok"] is True, reject_result

    blocked_card = _seed_recoverable_blocked_card(root, task_id, validation=[])
    stale_card = dict(blocked_card)
    monkeypatch.setattr(core, "_live_card", lambda task_id: (stale_card, None))

    result = core.recover_blocked_rework(
        task_id,
        topic=topic,
        feedback_reason="addressed",
        clean_root_if_predecessor_missing=True,
        validation_amendment=[new_cmd],
    )

    assert result["ok"] is True, result
    assert result["validation_amendment_applied"] == [new_cmd]
    card = task_store.get_task(root, task_id)
    assert card["validation"] == [new_cmd]


def test_core_recover_blocked_rework_idempotent_replay_reports_no_phantom_applied(
    monkeypatch, tmp_path
):
    """A second recovery of the same card takes the idempotent
    ``already_recovered`` branch, which returns before any commit.  Every
    command the receipt reports as applied must be present in the stored
    card's validation -- no phantom applied commands."""
    root = _init_repo(tmp_path)
    runner = "claude_task_mcp_runtime_wiring"
    topic = "task_mcp"
    task_id = "NF01151_RECOVER_TWICE"
    first_cmd = "pytest -q tests/test_first_nf01151.py"
    second_cmd = "pytest -q tests/test_second_nf01151.py"
    _insert_task(root, task_id, runner, topic)

    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "nf01151-manager")
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)

    _seed_recoverable_blocked_card(root, task_id, validation=[])

    first = core.recover_blocked_rework(
        task_id,
        topic=topic,
        feedback_reason="addressed",
        clean_root_if_predecessor_missing=True,
        validation_amendment=[first_cmd],
    )

    assert first["ok"] is True, first
    assert first["validation_amendment_applied"] == [first_cmd]
    assert task_store.get_task(root, task_id)["validation"] == [first_cmd]

    second = core.recover_blocked_rework(
        task_id,
        topic=topic,
        feedback_reason="addressed",
        clean_root_if_predecessor_missing=True,
        validation_amendment=[second_cmd],
    )

    assert second["ok"] is True, second
    persisted = task_store.get_task(root, task_id)["validation"]
    applied = second["validation_amendment_applied"]
    assert all(command in persisted for command in applied), (applied, persisted)
    assert applied == []
    assert persisted == [first_cmd]
    assert second_cmd not in persisted


def test_core_recover_blocked_rework_idempotent_replay_refuses_over_cap_amendment(
    monkeypatch, tmp_path
):
    """An amendment that would take the card over MAX_PATHS_PER_FIELD is
    refused even on the idempotent ``already_recovered`` branch, and the
    stored card is left unchanged."""
    from aiworkhub import task_templates

    root = _init_repo(tmp_path)
    runner = "claude_task_mcp_runtime_wiring"
    topic = "task_mcp"
    task_id = "NF01151_RECOVER_OVER_CAP"
    first_cmd = "pytest -q tests/test_first_nf01151.py"
    _insert_task(root, task_id, runner, topic)

    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "nf01151-manager")
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)

    _seed_recoverable_blocked_card(root, task_id, validation=[])

    first = core.recover_blocked_rework(
        task_id,
        topic=topic,
        feedback_reason="addressed",
        clean_root_if_predecessor_missing=True,
        validation_amendment=[first_cmd],
    )
    assert first["ok"] is True, first

    before = task_store.get_task(root, task_id)["validation"]
    over_cap = [
        f"pytest -q tests/test_cap_{index}_nf01151.py"
        for index in range(task_templates.MAX_PATHS_PER_FIELD)
    ]

    second = core.recover_blocked_rework(
        task_id,
        topic=topic,
        feedback_reason="addressed",
        clean_root_if_predecessor_missing=True,
        validation_amendment=over_cap,
    )

    assert second["ok"] is False, second
    assert "validation_amendment_exceeds_limit" in json.dumps(second), second
    assert task_store.get_task(root, task_id)["validation"] == before


def test_apply_validation_amendment_at_cap_with_fully_deduped_amendment_succeeds():
    from aiworkhub import task_templates

    existing_commands = [
        f"pytest -q tests/test_existing_{i}.py"
        for i in range(task_templates.MAX_PATHS_PER_FIELD)
    ]
    card = {"validation": list(existing_commands)}

    applied = task_store.apply_validation_amendment(card, [existing_commands[0]])

    assert applied == []
    assert card["validation"] == existing_commands


def test_recover_blocked_rework_refuses_combined_validation_amendment_over_limit(
    tmp_path,
):
    from aiworkhub import task_templates

    root = _init_repo(tmp_path)
    task_id = "NF01151_RECOVER_OVER_LIMIT"
    existing_commands = [
        f"pytest -q tests/test_existing_{i}.py"
        for i in range(task_templates.MAX_PATHS_PER_FIELD)
    ]
    _insert_task(root, task_id, "claude_task_mcp_runtime_wiring", "task_mcp")
    # Seed a card that is genuinely recoverable, so the refusal below can only
    # come from the combined-limit check.  A card refused earlier for
    # ineligibility would never reach it, and the no-partial-persist property
    # would not actually be exercised.
    _seed_recoverable_blocked_card(root, task_id, validation=existing_commands)

    before = task_store.get_task(root, task_id)

    ok, state = task_store.recover_blocked_rework(
        root,
        task_id,
        feedback_reason="addressed",
        clean_root_if_predecessor_missing=True,
        validation_amendment=["pytest -q tests/test_one_more_nf01151.py"],
    )

    assert ok is False
    assert state == "invalid_validation_amendment:validation_amendment_exceeds_limit"
    after = task_store.get_task(root, task_id)
    assert after == before

    # The same card recovers once the over-limit amendment is dropped: the
    # refusal above was the limit, not eligibility.
    eligible_ok, eligible_state = task_store.recover_blocked_rework(
        root,
        task_id,
        feedback_reason="addressed",
        clean_root_if_predecessor_missing=True,
    )

    assert (eligible_ok, eligible_state) == (True, "recovered")


def test_reject_review_refuses_combined_validation_amendment_over_limit(
    monkeypatch, tmp_path
):
    from aiworkhub import task_templates

    root = _init_repo(tmp_path)
    runner = "claude_task_mcp_runtime_wiring"
    topic = "task_mcp"
    task_id = "NF01151_REJECT_OVER_LIMIT"
    existing_commands = [
        f"pytest -q tests/test_existing_{i}.py"
        for i in range(task_templates.MAX_PATHS_PER_FIELD)
    ]
    _insert_task(
        root,
        task_id,
        runner,
        topic,
        extra_card={
            "validation": list(existing_commands),
            "validation_roles": ["generic"] * len(existing_commands),
        },
    )

    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "nf01151-manager")
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)

    assert core.claim_start_exact(task_id, runner, topic)["ok"] is True
    assert core.mark_review(task_id, runner=runner, topic=topic)["ok"] is True

    before = task_store.get_task(root, task_id)

    result = core.reject_review(
        task_id,
        "add missing coverage",
        topic=topic,
        validation_amendment=["pytest -q tests/test_extra_nf01151.py"],
    )

    assert result["ok"] is False, result
    assert result["stderr"] == "invalid_validation_amendment:validation_amendment_exceeds_limit"
    after = task_store.get_task(root, task_id)
    assert after == before
