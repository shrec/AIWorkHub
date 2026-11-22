"""NF-2026-02366: skill/recipe mining runs off the accept reply path.

Field 0.13.9 (GeoAI) measured every accepted ``learning_commit`` waiting
12-120 s for skill mining and 11-22 s for recipe mining, each producing the
identical result every time. These tests pin the new contract with FAKE
miners only -- never a real provider, never a manager loop:

(a) the reply returns while a slow mine is still running;
(b) an unchanged input digest skips the mine and reports ``skipped_unchanged``;
(c) a changed input digest re-mines;
(d) accepts arriving during one running mine coalesce into exactly one
    follow-up run;
(e) a mining exception never changes the commit's ``ok``/``failures``.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from _taskdb_compat import upsert_card
from aiworkhub import core, evidence_levels, manager_ai_tools, task_store

SESSION_ID = "019f5097-6dbe-7172-870a-945afc5f3bfb"
TASK_ID = "TASK-NF02366-1"
REQUEST_ID = "request-nf02366-0001"
KINDS = ("skill_mining", "recipe_mining")


def _route(root: Path) -> dict:
    return {
        "ok": True, "role": "manager", "provider": "codex", "repo": str(root),
        "manager_route": {"provider": "codex", "session_id": SESSION_ID, "thread_id": SESSION_ID},
    }


def _accepted_card(root: Path) -> None:
    record = evidence_levels.EvidenceRecord(
        evidence_level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
        severity="NONE", confidence="HIGH",
        reference=f"file:test-fixture/{REQUEST_ID}",
        verified_by="codex", message="fixture manager acceptance",
    ).to_dict()
    card = {
        "task_id": TASK_ID, "runner": "worker", "topic": "learning", "mode": "edit",
        "status": "finished", "worker_status": "done",
        "accepted_request_id": REQUEST_ID,
        "accept_evidence": {"acceptance_evidence_record": record},
    }
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        upsert_card(con, card)
    finally:
        con.close()


class FakeMiner:
    """One fake mining kind: controllable digests, a counted (optionally
    blocking or raising) mine."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.input = "input-1"
        self.state = "state-1"
        self.calls = 0
        self.started = threading.Event()
        self.release: threading.Event | None = None
        self.error: Exception | None = None
        self.module = SimpleNamespace(
            mining_input_digest=lambda _repo: self.input,
            mining_state_digest=lambda _repo: self.state,
        )

    def mine(self, _repo: Path) -> dict[str, Any]:
        self.calls += 1
        self.started.set()
        if self.release is not None:
            assert self.release.wait(30), "test never released the slow fake mine"
        if self.error is not None:
            raise self.error
        return {
            "candidates": 1, "proposed": [f"{self.kind}-{self.calls}"],
            "already_proposed": [], "refused_by_reason": {}, "elapsed_ms": 0.0,
        }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _route(root))
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    _accepted_card(root)
    yield root
    # Drain BEFORE monkeypatch undoes the route, so no mining thread outlives it.
    assert manager_ai_tools.wait_for_learning_mining(root, timeout=60)["idle"]


@pytest.fixture
def fakes(monkeypatch, repo):
    miners = {kind: FakeMiner(kind) for kind in KINDS}
    monkeypatch.setattr(
        manager_ai_tools, "_mining_kinds",
        lambda: tuple((kind, m.module, m.mine) for kind, m in miners.items()),
    )
    yield miners
    for miner in miners.values():
        if miner.release is not None:
            miner.release.set()


def _accept() -> dict:
    return manager_ai_tools.learning_commit(
        task_id=TASK_ID, request_id=REQUEST_ID, repo_area="src/aiworkhub",
        outcome="accepted", evidence_ids=["file:tests/test_mining_off_accept_path_nf02366.py"],
        idempotency_key="nf02366-accept-0001", provenance="nf02366 regression",
    )


def _wait(repo: Path) -> dict:
    waited = manager_ai_tools.wait_for_learning_mining(repo, timeout=30)
    assert waited["idle"] is True
    return waited


def test_a_reply_returns_before_a_slow_mine_finishes(repo, fakes):
    skill = fakes["skill_mining"]
    skill.release = threading.Event()

    result = _accept()

    assert result["ok"] is True
    for kind in KINDS:
        assert result[kind]["state"] == "scheduled"
        assert result[kind]["coalesced"] is False
    assert skill.started.wait(10)
    # The reply is already in hand while the mine is still blocked.
    assert manager_ai_tools.wait_for_learning_mining(repo, timeout=0.05)["idle"] is False
    skill.release.set()
    waited = _wait(repo)
    assert waited["skill_mining"]["state"] == "completed"
    assert waited["recipe_mining"]["state"] == "completed"


def test_b_unchanged_digest_skips_the_mine(repo, fakes):
    _accept()
    first = _wait(repo)
    assert first["skill_mining"]["state"] == "completed"
    assert (repo / ".aiworkhub" / "runtime" / "learning_mining_state.json").is_file()

    second_reply = _accept()
    assert second_reply["skill_mining"]["previous_state"] == "completed"
    second = _wait(repo)

    for kind in KINDS:
        assert fakes[kind].calls == 1
        assert second[kind]["state"] == "skipped_unchanged"
        assert second[kind]["result"] == first[kind]["result"]


def test_c_changed_input_reruns_the_mine(repo, fakes):
    _accept()
    _wait(repo)
    fakes["skill_mining"].input = "input-2"
    fakes["recipe_mining"].state = "state-2"

    _accept()
    second = _wait(repo)

    assert fakes["skill_mining"].calls == 2
    assert fakes["recipe_mining"].calls == 2
    assert second["skill_mining"]["state"] == "completed"
    assert second["recipe_mining"]["state"] == "completed"


def test_d_accepts_during_a_running_mine_coalesce_into_one_follow_up(repo, fakes):
    skill = fakes["skill_mining"]
    skill.release = threading.Event()
    # A different digest on every read, so the follow-up never skips and the
    # call count measures runs exactly.
    reads = iter(range(1000))
    skill.module.mining_input_digest = lambda _repo: f"input-{next(reads)}"

    _accept()
    assert skill.started.wait(10)
    second = _accept()
    third = _accept()
    assert second["skill_mining"]["coalesced"] is True
    assert third["skill_mining"]["coalesced"] is True
    skill.release.set()

    waited = _wait(repo)
    assert waited["runs"] == 2
    assert skill.calls == 2


def test_e_mining_exception_never_changes_commit_ok(repo, fakes):
    for miner in fakes.values():
        miner.error = RuntimeError("forced_mining_failure")

    result = _accept()

    assert result["ok"] is True
    assert result.get("failures", {}) == {}
    waited = _wait(repo)
    for kind in KINDS:
        assert waited[kind]["state"] == "failed"
        assert waited[kind]["result"]["refused_by_reason"]["mining"].startswith(f"{kind}_failed:")
    # A failed run is never persisted, so the next accept mines again.
    for miner in fakes.values():
        miner.error = None
    _accept()
    again = _wait(repo)
    assert all(again[kind]["state"] == "completed" for kind in KINDS)
    assert all(miner.calls == 2 for miner in fakes.values())


def test_unknown_digest_never_skips(repo, fakes):
    def _boom(_repo):
        raise OSError("store unreadable")

    fakes["skill_mining"].module.mining_input_digest = _boom
    _accept()
    _wait(repo)
    _accept()
    waited = _wait(repo)
    assert fakes["skill_mining"].calls == 2
    assert waited["skill_mining"]["digest_error"] == "digest_failed:OSError"
