"""NF-2026-01304: card-scoped launch authority must compare canonical runners.

Measured: ``_check_card_scoped_write_authority`` (core.py) compared
``card.get("runner") != runner`` against the RAW supplied runner. Task
creation stores the canonical runner (``resolve_create_time_runner`` folds a
hyphenated workforce id such as ``deepseek-v4-pro`` onto its registered
spelling, e.g. ``deepseek_v4-pro``), so a launch-blocked/claim-start call that
supplies the live hyphenated worker id failed closed with
``card_scoped_identity_mismatch`` before any model ran, even though the
supplied runner and the card's runner name the exact same identity.

The fix folds the supplied runner onto the card's own stored runner with the
same normalizer task creation uses, ``runner_topic_policy.canonical_runner_id``,
before the identity comparison (and before the later ``claimed_by``
comparisons that must use the same canonical spelling).
"""

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

NOW = "2026-10-09T00:00:00+00:00"

# The card's stored runner is the CANONICAL spelling a hyphenated workforce id
# folds onto: the hyphen immediately after the family name becomes an
# underscore, while the trailing ``-pro`` is identity-bearing and stays a
# hyphen -- exactly the ``deepseek-v4-pro`` -> ``deepseek_v4-pro`` fold
# described in the measured defect.
CARD_RUNNER_CANONICAL = "deepseek_v4-pro"
CARD_RUNNER_RAW_SPELLING = "deepseek-v4-pro"
CARD_TOPIC = "task_mcp"


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


def _insert_task(root: Path, task_id: str, runner: str, topic: str) -> None:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    card_json = json.dumps({"runner": runner, "topic": topic}, ensure_ascii=False)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, runner, topic, "solo", "pending", "unclaimed", "normal",
             "objective", card_json, NOW, NOW, ""),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    yield root


# ---------------------------------------------------------------------------
# A pending card stored with the canonical runner must pass for either the
# raw hyphenated spelling or the canonical spelling itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "supplied_runner", [CARD_RUNNER_RAW_SPELLING, CARD_RUNNER_CANONICAL]
)
def test_launch_blocked_authority_accepts_raw_or_canonical_runner(repo, supplied_runner):
    _insert_task(repo, "TASK_NF01304_A", CARD_RUNNER_CANONICAL, CARD_TOPIC)
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF01304_A"], supplied_runner, CARD_TOPIC
    )
    assert decision == {"allowed": True, "reason": "card_scoped_launch_blocker_allowed"}


@pytest.mark.parametrize(
    "supplied_runner", [CARD_RUNNER_RAW_SPELLING, CARD_RUNNER_CANONICAL]
)
def test_claim_start_authority_accepts_raw_or_canonical_runner(repo, supplied_runner):
    _insert_task(repo, "TASK_NF01304_B", CARD_RUNNER_CANONICAL, CARD_TOPIC)
    decision = core._check_card_scoped_write_authority(
        ["claim-start", "TASK_NF01304_B"], supplied_runner, CARD_TOPIC
    )
    assert decision == {"allowed": True, "reason": "card_scoped_claim_start_allowed"}


# ---------------------------------------------------------------------------
# A runner that folds onto a DIFFERENT identity than the card's must still be
# refused -- canonicalization folds spellings, it never widens identity.
# ---------------------------------------------------------------------------


def test_runner_that_canonicalizes_to_a_different_runner_still_mismatches(repo):
    _insert_task(repo, "TASK_NF01304_C", CARD_RUNNER_CANONICAL, CARD_TOPIC)
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF01304_C"], "claude_opus-5", CARD_TOPIC
    )
    assert decision == {"allowed": False, "reason": "card_scoped_identity_mismatch"}


def test_near_miss_spelling_of_an_unrelated_runner_still_mismatches(repo):
    """``deepseek_v4_pro`` (underscore throughout) folds to the SAME key as the
    card's ``deepseek_v4-pro``, so it must pass -- but a spelling that is
    close by substring only (e.g. ``deepseek_v4`` without the trailing
    ``-pro``) is a different identity and must still mismatch."""
    _insert_task(repo, "TASK_NF01304_D", CARD_RUNNER_CANONICAL, CARD_TOPIC)
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF01304_D"], "deepseek_v4", CARD_TOPIC
    )
    assert decision == {"allowed": False, "reason": "card_scoped_identity_mismatch"}


def test_underscore_throughout_variant_still_folds_onto_the_card(repo):
    _insert_task(repo, "TASK_NF01304_E", CARD_RUNNER_CANONICAL, CARD_TOPIC)
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF01304_E"], "deepseek_v4_pro", CARD_TOPIC
    )
    assert decision == {"allowed": True, "reason": "card_scoped_launch_blocker_allowed"}


# ---------------------------------------------------------------------------
# Topic and task_id comparisons remain exact; malformed runner tokens are
# still refused on the raw input, before canonicalization is ever reached.
# ---------------------------------------------------------------------------


def test_topic_mismatch_is_still_exact(repo):
    _insert_task(repo, "TASK_NF01304_F", CARD_RUNNER_CANONICAL, CARD_TOPIC)
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF01304_F"], CARD_RUNNER_CANONICAL, "wrong_topic"
    )
    assert decision == {"allowed": False, "reason": "card_scoped_identity_mismatch"}


def test_task_id_mismatch_is_still_exact(repo):
    _insert_task(repo, "TASK_NF01304_G", CARD_RUNNER_CANONICAL, CARD_TOPIC)
    # ``_live_card`` resolves the task by this task_id via ``show_task`` and
    # already refuses to return a card whose own ``task_id`` field disagrees
    # with the lookup key (core.py ``_live_card``: ``card.get("task_id") !=
    # task_id`` there returns an unresolved error). A task_id that was never
    # inserted therefore never reaches the sibling
    # ``card.get("task_id") != task_id`` comparison inside
    # ``_check_card_scoped_write_authority`` itself -- it fails closed one
    # step earlier, as an unresolved card, before canonicalization is ever
    # considered.
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF01304_G_UNRESOLVED"], CARD_RUNNER_CANONICAL, CARD_TOPIC
    )
    assert decision == {"allowed": False, "reason": "card_scoped_task_unresolved"}


def test_live_card_whose_task_id_differs_is_identity_mismatch(repo, monkeypatch):
    _insert_task(repo, "TASK_NF01304_G2", CARD_RUNNER_CANONICAL, CARD_TOPIC)
    monkeypatch.setattr(
        core,
        "_live_card",
        lambda task_id: (
            {"task_id": "TASK_NF01304_OTHER", "runner": CARD_RUNNER_CANONICAL, "topic": CARD_TOPIC},
            None,
        ),
    )
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF01304_G2"], CARD_RUNNER_CANONICAL, CARD_TOPIC
    )
    assert decision == {"allowed": False, "reason": "card_scoped_identity_mismatch"}

def test_malformed_runner_token_is_still_refused_before_canonicalization(repo):
    _insert_task(repo, "TASK_NF01304_H", CARD_RUNNER_CANONICAL, CARD_TOPIC)
    decision = core._check_card_scoped_write_authority(
        ["launch-blocked", "TASK_NF01304_H"], "bad runner!", CARD_TOPIC
    )
    assert decision["allowed"] is False
    assert str(decision["reason"]).startswith("malformed_runner:")


def _insert_claimed_task(
    root: Path,
    task_id: str,
    runner: str,
    topic: str,
    claimed_by: str,
    launch_request_id: str = "",
) -> None:
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    card_json = json.dumps(
        {"runner": runner, "topic": topic, "launch_request_id": launch_request_id},
        ensure_ascii=False,
    )
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, runner, topic, "solo", "processing", "claimed", "normal",
             "objective", card_json, NOW, NOW, claimed_by),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# A claimed, processing card gates other actions (``review``,
# ``launch-failed``, ``usage``) on ``claimed_by == card_runner``. claimed_by
# is set to the canonical card runner by claim_start_exact's exact
# ``stored_runner != runner`` refusal, and by auto_pickup via
# eligible_dryrun_candidates' exact runner filter (``if c.get("runner") !=
# runner: continue``) -- never an independently-canonicalized or raw value.
# ---------------------------------------------------------------------------


def test_claimed_card_review_action_allowed_when_claimed_by_matches_card_runner(repo):
    _insert_claimed_task(
        repo,
        "TASK_NF01304_I",
        "claude_sonnet-5",
        CARD_TOPIC,
        claimed_by="claude_sonnet-5",
        launch_request_id="REQ-1",
    )
    decision = core._check_card_scoped_write_authority(
        ["review", "TASK_NF01304_I"], "claude-sonnet-5", CARD_TOPIC
    )
    assert decision == {"allowed": True, "reason": "card_scoped_review_allowed"}


def test_claimed_card_claimed_by_mismatch_is_refused(repo):
    _insert_claimed_task(
        repo,
        "TASK_NF01304_J",
        "claude_sonnet-5",
        CARD_TOPIC,
        claimed_by="codex_gpt-5",
        launch_request_id="REQ-2",
    )
    decision = core._check_card_scoped_write_authority(
        ["review", "TASK_NF01304_J"], "claude-sonnet-5", CARD_TOPIC
    )
    assert decision == {
        "allowed": False,
        "reason": "card_scoped_claimed_by_mismatch:codex_gpt-5",
    }


def test_claim_start_attach_branch_allowed_when_claimed_by_matches_card_runner(repo):
    _insert_claimed_task(
        repo,
        "TASK_NF01304_K",
        "claude_sonnet-5",
        CARD_TOPIC,
        claimed_by="claude_sonnet-5",
    )
    decision = core._check_card_scoped_write_authority(
        ["claim-start", "TASK_NF01304_K"], "claude-sonnet-5", CARD_TOPIC
    )
    assert decision == {"allowed": True, "reason": "card_scoped_launch_attach_allowed"}
