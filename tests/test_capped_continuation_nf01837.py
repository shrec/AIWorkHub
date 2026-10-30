"""NF-2026-01837: a reject-depth-capped card continues as a successor in one call."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    capped_continuation,
    core,
    launch_replay_guard,
    rework_delta_artifact,
    task_store,
    task_templates,
)
from aiworkhub.learning_commit import FailureCategory  # noqa: E402

_NOW = "2026-10-30T00:00:00+00:00"
_RUNNER = "claude_task_mcp_runtime_wiring"
_TOPIC = "task_mcp"
_MANAGER = "nf01837-manager"
_DIGEST = "ab" * 32
_CONTRACT = {
    "objective": "objective",
    "acceptance": ["the successor keeps the contract"],
    "allowed_writes": ["src/existing.py"],
    "validation": ["pytest -q tests/existing.py"],
    "required_outputs": ["src/existing.py"],
}


def _predecessor(task_id: str) -> dict:
    return {
        "request_id": "9" * 32,
        "task_id": task_id,
        "claim_epoch": 1,
        "changed_path_hashes": {"src/b.py": "2" * 64, "src/a.py": "1" * 64},
        "delta_artifact": {"path": f"/runtime/rework_deltas/{_DIGEST}.json", "digest": _DIGEST},
        "rework_delta": {
            "schema_id": "aiworkhub.rework_delta_descriptor.v1",
            "sealed": True,
            "task_id": task_id,
            "request_id": "9" * 32,
            "claim_epoch": 1,
            "artifact_path": f"/runtime/rework_deltas/{_DIGEST}.json",
            "artifact_sha256": _DIGEST,
        },
    }


def _db(root: Path) -> str:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    return readiness.canonical_db


def _insert_task(root: Path, task_id: str, extra_card: dict | None = None) -> None:
    card = {"origin_thread_id": "nf01837-thread", **_CONTRACT, **(extra_card or {})}
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, origin_thread_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, _RUNNER, _TOPIC, "solo", "pending", "unclaimed", "normal", "objective",
             json.dumps(card), _NOW, _NOW, None, "nf01837-thread"),
        )
        conn.commit()
    finally:
        conn.close()


def _rewrite_card(root: Path, task_id: str, **fields) -> None:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        card = json.loads(row[0])
        card.update(fields)
        conn.execute("UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id))
        conn.commit()
    finally:
        conn.close()


def _raw_card(root: Path, task_id: str) -> dict:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0])


def _archived(root: Path, task_id: str) -> bool:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute("SELECT archived_at FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return bool(row and row[0])


def _to_review(root: Path, task_id: str, request_id: str) -> None:
    assert core.claim_start_exact(task_id, _RUNNER, _TOPIC)["ok"] is True
    assert core.mark_review(task_id, runner=_RUNNER, topic=_TOPIC)["ok"] is True
    card = _raw_card(root, task_id)
    terminal = card.get("terminal_review")
    terminal = terminal if isinstance(terminal, dict) else {}
    evidence = terminal.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    evidence["request_identity"] = {"request_id": request_id, "task_id": task_id}
    terminal["evidence"] = evidence
    _rewrite_card(root, task_id, terminal_review=terminal)


def _snapshot(root: Path) -> tuple:
    conn = sqlite3.connect(_db(root))
    try:
        rows = conn.execute(
            "SELECT task_id, status, worker_status, card_json FROM tasks ORDER BY task_id"
        ).fetchall()
        events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()
    finally:
        conn.close()
    return tuple(map(tuple, rows)), tuple(events)


def _card(root: Path, task_id: str) -> dict:
    card = task_store.get_task(root, task_id)
    assert card is not None, task_id
    return card


@pytest.fixture
def repo(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: _MANAGER)
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)
    monkeypatch.setattr(
        core, "classify_terminal_disposition", lambda _card: FailureCategory.CANDIDATE_CODE
    )
    monkeypatch.setattr(
        rework_delta_artifact,
        "has_verified_rework_delta",
        lambda predecessor, *, authority_repo: isinstance(predecessor, dict)
        and isinstance(predecessor.get("rework_delta"), dict),
    )
    created: list[dict] = []

    def fake_create_task(task_id, **payload):
        created.append({"task_id": task_id, **payload})
        stored = {key: value for key, value in payload.items() if value not in ("", None)}
        _insert_task(root, task_id, {**stored, "task_id": task_id})
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(core, "create_task", fake_create_task)
    monkeypatch.setattr(core, "_created_for_test", created, raising=False)
    return root


def _capped(root: Path, task_id: str, *, with_delta: bool = True) -> dict:
    _insert_task(root, task_id)
    for request_id in ("a" * 32, "b" * 32):
        _to_review(root, task_id, request_id)
        assert core.reject_review(task_id, "still wrong", topic=_TOPIC)["ok"] is True
    if with_delta:
        _rewrite_card(root, task_id, rework_predecessor=_predecessor(task_id))
    card = _card(root, task_id)
    assert launch_replay_guard.reject_depth_refusal(card, repo=root)
    return card


def test_continuation_creates_successor_inheriting_the_sealed_delta(repo, monkeypatch):
    rebinds: list[tuple] = []
    monkeypatch.setattr(
        core,
        "_rebind_superseded_needfix",
        lambda task_id, by, **kw: rebinds.append((task_id, by)) or {"ok": True},
    )
    old = _capped(repo, "NF1837_CAP")

    result = core.continue_capped_task("NF1837_CAP", reason="decompose by module")

    assert result["ok"] is True
    assert result["continuation"] == {
        "successor_task_id": "NF1837_CAP-c1",
        "predecessor_request_id": "9" * 32,
        "delta_digest": _DIGEST,
    }
    assert _archived(repo, "NF1837_CAP")
    successor = _card(repo, "NF1837_CAP-c1")
    assert successor["status"] == "pending"
    for key in _CONTRACT:
        assert successor[key] == old[key]
    assert successor["rework_predecessor"] == old["rework_predecessor"]
    feedback = successor["review_feedback"]
    assert feedback == {
        "schema_id": "aiworkhub.rework_feedback_delta.v1",
        "instruction": "decompose by module",
        "predecessor_request_id": "9" * 32,
        "predecessor_changed_paths": ["src/a.py", "src/b.py"],
        "residual_identities": [],
    }
    assert launch_replay_guard.reject_depth_refusal(successor, repo=repo) == ""
    assert rebinds == [("NF1837_CAP", "NF1837_CAP-c1")]
    assert _DIGEST in task_store.referenced_rework_delta_digests(repo)


def test_successor_contract_equals_the_extracted_create_payload(repo):
    old = _capped(repo, "NF1837_PAYLOAD")

    core.continue_capped_task("NF1837_PAYLOAD")

    (call,) = core._created_for_test
    assert call == {"task_id": "NF1837_PAYLOAD-c1", **core._create_payload_from_card(old)}


def test_successor_id_takes_the_next_free_suffix_or_the_explicit_by(repo):
    _insert_task(repo, "NF1837_NAMED-c1")
    assert capped_continuation.successor_task_id("NF1837_NAMED") == "NF1837_NAMED-c2"
    _capped(repo, "NF1837_NAMED")
    _insert_task(repo, "NF1837_NAMED-c2")

    result = core.continue_capped_task("NF1837_NAMED")
    assert result["continuation"]["successor_task_id"] == "NF1837_NAMED-c3"

    _capped(repo, "NF1837_BY")
    result = core.continue_capped_task("NF1837_BY", by="NF1837_CHOSEN")
    assert result["continuation"]["successor_task_id"] == "NF1837_CHOSEN"
    assert _card(repo, "NF1837_CHOSEN")["rework_predecessor"]["request_id"] == "9" * 32


def test_a_card_that_is_not_capped_is_refused_without_changes(repo):
    _insert_task(repo, "NF1837_FRESH")
    before = _snapshot(repo)

    with pytest.raises(ValueError, match="continuation_not_capped"):
        core.continue_capped_task("NF1837_FRESH")

    assert _snapshot(repo) == before


def test_a_capped_card_without_a_verified_delta_is_refused_without_changes(repo):
    _capped(repo, "NF1837_NODELTA", with_delta=False)
    before = _snapshot(repo)

    with pytest.raises(ValueError, match="continuation_predecessor_unverified"):
        core.continue_capped_task("NF1837_NODELTA")

    assert _snapshot(repo) == before


def test_the_setter_only_writes_a_pristine_pending_successor(repo):
    _insert_task(repo, "NF1837_LAUNCHED", {"claim_epoch": 1})
    _insert_task(repo, "NF1837_HAS_PRED", {"rework_predecessor": _predecessor("other")})
    for task_id in ("NF1837_LAUNCHED", "NF1837_HAS_PRED"):
        before = _snapshot(repo)
        ok, state = task_store.inherit_capped_rework(
            repo, task_id, rework_predecessor=_predecessor(task_id),
            review_feedback={}, actor=_MANAGER,
        )
        assert (ok, state) == (False, "successor_not_pristine")
        assert _snapshot(repo) == before

    assert task_store.inherit_capped_rework(
        repo, "NF1837_MISSING", rework_predecessor={}, review_feedback={}, actor=_MANAGER
    ) == (False, "task_missing")


def test_reason_is_truncated_to_the_byte_limit():
    feedback = capped_continuation._feedback({"request_id": "r"}, "é" * 9000)

    assert len(feedback["instruction"].encode("utf-8")) <= capped_continuation.MAX_REASON_BYTES


def test_continue_capped_custom_escape_card_creates_successor(repo, monkeypatch):
    real_payload = core._create_payload_from_card
    custom = {"template_name": task_templates.CUSTOM_TEMPLATE_NAME, "expanded_contract": {"stale": True}}
    monkeypatch.setattr(core, "_create_payload_from_card", lambda card: {**real_payload(card), "template_provenance": custom})
    _capped(repo, "NF1837_CUSTOM")

    result = core.continue_capped_task("NF1837_CUSTOM", reason="decompose by module")

    assert result["ok"] is True
    (call,) = core._created_for_test
    assert call["task_id"] == "NF1837_CUSTOM-c1"
    assert call["template_provenance"] is None
    assert call["custom_template_escape"] == task_templates.AUDITED_CUSTOM_ESCAPE
    assert _archived(repo, "NF1837_CUSTOM")


def test_continue_capped_named_template_provenance_passes_through(repo, monkeypatch):
    real_payload = core._create_payload_from_card
    named = {"template_name": "bugfix_minimal", "expanded_contract": {"kept": True}}
    monkeypatch.setattr(core, "_create_payload_from_card", lambda card: {**real_payload(card), "template_provenance": named})
    _capped(repo, "NF1837_NAMED_TPL")

    core.continue_capped_task("NF1837_NAMED_TPL")

    (call,) = core._created_for_test
    assert call["template_provenance"] == named
    assert call.get("custom_template_escape") in (None, "")
