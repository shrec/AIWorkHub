"""NF-2026-01196: reroute a card the manager recovered out of blocked rework.

Field report EntryLink NF-2026-00046 (AIWorkHub 0.12.14): a card rejected for
rework, blocked by its rework attempt and recovered with
``recover_blocked_rework`` back to pending cannot be rerouted to another
runner/model -- ``aiworkhub_task_reroute_launch_identity`` answers
``reroute_manager_rejection_identity_mismatch`` -- while the direct
reject -> pending -> reroute path works.

Measured cause, derived from the canonical sources: the recovery keeps the
sealed manager-rejected predecessor at its own claim epoch N but produces epoch
N+2, because the rework attempt claimed N+1 in between.  Both existing recovery
rebinds in ``core._verified_manager_rejection_receipt`` require
``recovery_terminal_epoch == claim_epoch`` -- the recovered terminal episode
sitting AT the rejected candidate's epoch -- so neither can explain that
advance, no launch-failure receipt applies, and the clause at
``src/aiworkhub/core.py:8328-8338`` (pre-fix numbering) refuses.

Every transition under test is the canonical store transition, not a hand-built
card: ``task_engine.claim_start_exact`` / ``core.claim_start_exact``,
``task_store.mark_terminal_review``, ``core.reject_review``,
``task_store.mark_launch_failed``, ``core.recover_blocked_rework`` and
``core.reroute_launch_identity`` run in that order against one real repository,
so the card and the task_events the reroute authorizes against are exactly what
the field sequence produces.  Only the finalizer's terminal *evidence* payload
is constructed, because no provider runs in a unit test; it is an input to a
real transition, never a card state.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from aiworkhub import core, task_engine, task_store, worker_workspace

_TOPIC = "nf01196_reroute_after_blocked_recovery"
_RUNNER = "glm_5.3"
_TO_RUNNER = "claude_sonnet-5"
_TO_ADAPTER_ID = "claude_cli"
_TO_MODEL = "sonnet"
_CANONICAL_TO_MODEL = "claude-sonnet-5"
_SCOPE = ["out/result.json"]
_BASE_OID = "b" * 40
_REJECTED_REQUEST = "a" * 32
_REWORK_REQUESTS = ("c" * 32, "d" * 32)
_BASELINE = b"canonical baseline\n"
_CANDIDATE = b"rejected candidate\n"
_REJECT_REASON = "repair the rejected candidate through another provider"
_RECOVERY_REASON = "rework attempt terminalized operationally; recover and reroute"
_LAUNCH_ERROR = "provider authentication expired before any model work"
_MISMATCH = "reroute_manager_rejection_identity_mismatch"


@pytest.fixture
def coordinator_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token_path = tmp_path / "coordinator.token"
    token_path.write_text("coordinator-token\n", encoding="utf-8")
    if hasattr(os, "geteuid"):
        # The POSIX gate requires an owner-only 0600 token file. On Windows the
        # git-ignored repo-local file's ACL is the boundary and mode bits are
        # not meaningful, so the chmod is genuinely not applicable there.
        os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coordinator-token")
    baseline = repo / "out" / "result.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_bytes(_BASELINE)
    return repo


def _row(repo: Path, task_id: str) -> sqlite3.Row:
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def _card(repo: Path, task_id: str) -> dict:
    return json.loads(_row(repo, task_id)["card_json"])


def _write_card(repo: Path, task_id: str, card: dict) -> None:
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card, ensure_ascii=False, sort_keys=True), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_pending(repo: Path, task_id: str) -> None:
    """Seed one pending, unclaimed card: the state a worker claim starts from."""
    readiness = task_store.storage_readiness(repo)
    now = "2026-08-03T00:00:00+00:00"
    objective = "reroute a recovered blocked rework to another provider"
    card = {
        "task_id": task_id,
        "runner": _RUNNER,
        "topic": _TOPIC,
        "objective": objective,
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": None,
        "allowed_writes": list(_SCOPE),
        "required_outputs": list(_SCOPE),
        "forbidden": ["do not widen scope"],
        "validation": ["python -m pytest tests/test_process_launcher.py"],
    }
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,claimed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                _RUNNER,
                _TOPIC,
                "solo",
                "pending",
                "unclaimed",
                "normal",
                objective,
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                now,
                now,
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _reviewable_candidate_evidence(repo: Path, task_id: str) -> dict:
    """Write the retained worktree plus its delta artifact, return the evidence.

    This is the finalizer's output shape for a reviewable candidate: the exact
    retained worktree, the content-addressed delta artifact and the sealed
    descriptor the canonical transitions authenticate against.  The artifact is
    written directly rather than through ``seal_rework_delta_artifact`` so the
    fixture needs no mode-bit change; the retained worktree is the content
    authority on every path this test exercises.
    """
    request_id = _REJECTED_REQUEST
    workspace_root = worker_workspace.configured_worktree_root(repo) / request_id
    worktree = workspace_root / "worktree"
    home = workspace_root / "home"
    output = worktree / "out" / "result.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_CANDIDATE)
    digest = hashlib.sha256(_CANDIDATE).hexdigest()
    baseline_token = "file:664:" + hashlib.sha256(_BASELINE).hexdigest()

    artifact_bytes = json.dumps(
        {"schema_id": "aiworkhub.rework_delta_artifact.v1", "path": "out/result.json"},
        sort_keys=True,
    ).encode("utf-8")
    artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
    artifact_path = (
        worker_workspace.configured_runtime_root(repo)
        / "rework_deltas"
        / f"{artifact_digest}.json"
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(artifact_bytes)

    authority = {
        "schema_id": "aiworkhub.python_candidate_authority.v1",
        "sources": [
            {
                "path": "out/result.json",
                "state": "modified",
                "bytes_sha256": digest,
            }
        ],
    }
    # Exactly the keys ``WorkerWorkspace.from_metadata`` consumes: the reroute
    # parses this object straight out of the sealed predecessor.
    workspace = {
        "request_id": request_id,
        "repo": str(repo),
        "path": str(worktree),
        "home": str(home),
        "allowed_writes": list(_SCOPE),
        "parent_baseline": {"out/result.json": baseline_token},
        "workspace_baseline": {},
        "base_oid": _BASE_OID,
    }
    return {
        "request_id": request_id,
        "changed_paths": ["out/result.json"],
        "changed_path_hashes": {"out/result.json": digest},
        "required_outputs": [
            {"path": "out/result.json", "sha256": "file:664:" + digest}
        ],
        "request_identity": {
            "request_id": request_id,
            "task_id": task_id,
            "runner": _RUNNER,
            "topic": _TOPIC,
            "repo": str(repo),
            "claim_epoch": 1,
            "allowed_writes": list(_SCOPE),
            "parent_baseline": {"out/result.json": baseline_token},
            "base_oid": _BASE_OID,
        },
        "workspace": workspace,
        "python_candidate_authority": authority,
        "rework_delta": {
            "schema_id": "aiworkhub.rework_delta_descriptor.v1",
            "sealed": True,
            "authority_repo": str(repo.resolve()),
            "task_id": task_id,
            "request_id": request_id,
            "claim_epoch": 1,
            "artifact_path": str(artifact_path),
            "artifact_sha256": artifact_digest,
        },
    }


def _pin_retained_changed_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report the retained candidate's changed path without spawning git.

    The synthetic repository is not a git worktree, so the real
    ``changed_paths`` probe cannot run. Every byte-level hash check against the
    retained worktree still runs unchanged.
    """
    monkeypatch.setattr(
        worker_workspace,
        "changed_paths",
        lambda _workspace, **_kwargs: ["out/result.json"],
    )


def _reject_reviewed_candidate(repo: Path, task_id: str) -> dict:
    """Real claim -> review_ready -> manager ``reject_review`` to pending."""
    claim = task_engine.claim_start_exact(
        repo, task_id, _RUNNER, _TOPIC, request_id=_REJECTED_REQUEST
    )
    assert claim["ok"] is True, claim
    evidence = _reviewable_candidate_evidence(repo, task_id)
    assert task_store.mark_terminal_review(
        repo, task_id, runner=_RUNNER, substatus="review_ready", evidence=evidence
    ) == (True, "review")
    rejected = core.reject_review(task_id, _REJECT_REASON, to="pending")
    assert rejected["ok"] is True, rejected
    card = _card(repo, task_id)
    rejection = card["rejection_disposition"]
    predecessor = card["rework_predecessor"]
    assert rejection["request_id"] == _REJECTED_REQUEST
    assert rejection["failure_category"] == "candidate_code"
    assert predecessor["request_id"] == _REJECTED_REQUEST
    assert predecessor["claim_epoch"] == 1
    assert predecessor["pinned_at"] == rejection["pinned_at"]
    return predecessor


def _block_rework_attempt(
    repo: Path,
    task_id: str,
    *,
    request_id: str,
    claim_epoch: int,
    attach: bool = False,
) -> None:
    """Claim the rework attempt and terminalize it so the card is blocked.

    ``attach`` reproduces the other canonical launch lineage: auto-pickup takes
    the claim first (``claim_start`` with no request) and the launcher attaches
    its request afterwards (``launch_attach``), so the rework attempt's claim
    never names the request that failed.
    """
    if attach:
        picked = task_engine.claim_start_exact(repo, task_id, _RUNNER, _TOPIC)
        assert picked["ok"] is True, picked
    claim = task_engine.claim_start_exact(
        repo, task_id, _RUNNER, _TOPIC, request_id=request_id
    )
    assert claim["ok"] is True, claim
    assert _card(repo, task_id)["claim_epoch"] == claim_epoch
    assert task_store.mark_launch_failed(
        repo, task_id, runner=_RUNNER, reason=_LAUNCH_ERROR, request_id=request_id
    ) == (True, "blocked")


def _recover(repo: Path, task_id: str) -> dict:
    recovered = core.recover_blocked_rework(task_id, feedback_reason=_RECOVERY_REASON)
    assert recovered["ok"] is True, recovered
    row = _row(repo, task_id)
    assert row["status"] == "pending"
    assert row["worker_status"] == "unclaimed"
    return _card(repo, task_id)


def _field_sequence(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    task_id: str,
    cycles: int = 1,
    attach: bool = False,
) -> dict:
    """Drive the exact field sequence; return the recovered pending card."""
    _pin_retained_changed_paths(monkeypatch)
    _insert_pending(repo, task_id)
    _reject_reviewed_candidate(repo, task_id)
    card: dict = {}
    for cycle in range(cycles):
        _block_rework_attempt(
            repo,
            task_id,
            request_id=_REWORK_REQUESTS[cycle],
            claim_epoch=2 + cycle * 2,
            attach=attach,
        )
        card = _recover(repo, task_id)
    return card


def _reroute(task_id: str) -> dict:
    return core.reroute_launch_identity(
        task_id,
        from_runner=_RUNNER,
        to_runner=_TO_RUNNER,
        to_adapter_id=_TO_ADAPTER_ID,
        to_model=_TO_MODEL,
        reason="recovered rework needs a different provider",
    )


def _recovery_event_id(repo: Path, task_id: str) -> int:
    """The newest recovery event, ordered exactly as ``get_task_events`` does."""
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT event_id FROM task_events WHERE task_id=? "
            "AND event='blocked_rework_recovery' ORDER BY event_id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row["event_id"])


def _recovery_event_payload(repo: Path, task_id: str) -> dict:
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT payload_json FROM task_events WHERE event_id=?",
            (_recovery_event_id(repo, task_id),),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return json.loads(str(row["payload_json"]))


def _rewrite_recovery_event(
    repo: Path,
    task_id: str,
    *,
    payload: dict | None = None,
    runner: str | None = None,
) -> None:
    event_id = _recovery_event_id(repo, task_id)
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        if payload is not None:
            conn.execute(
                "UPDATE task_events SET payload_json=? WHERE event_id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), event_id),
            )
        if runner is not None:
            conn.execute(
                "UPDATE task_events SET runner=? WHERE event_id=?", (runner, event_id)
            )
        conn.commit()
    finally:
        conn.close()


def _append_later_claim_event(repo: Path, task_id: str) -> None:
    """Append one lineage event newer than the recovery (one-shot boundary)."""
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'claim_start', ?, ?, ?)",
            (
                task_id,
                _RUNNER,
                json.dumps({"runner": _RUNNER, "claim_epoch": 9}, sort_keys=True),
                "2026-08-04T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_nf01196_reroute_after_blocked_rework_recovery_preserves_identity(
    coordinator_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reject -> rework attempt terminal -> blocked -> recover -> reroute."""
    task_id = "NF01196_RECOVERED_REWORK_REROUTE"
    card = _field_sequence(coordinator_repo, monkeypatch, task_id=task_id)
    predecessor_before = card["rework_predecessor"]
    # The exact field state: the sealed rejected candidate is untouched, yet the
    # card sits two claim epochs past the rejection it is still bound to.
    assert predecessor_before["request_id"] == _REJECTED_REQUEST
    assert predecessor_before["claim_epoch"] == 1
    assert card["claim_epoch"] == 3
    assert card["recovery_epoch"] == 3
    rebind = card["blocked_recovery_rejection_rebind"]
    assert rebind["rejection_request_id"] == _REJECTED_REQUEST
    assert rebind["predecessor_claim_epoch"] == 1
    assert rebind["recovered_from_claim_epoch"] == 2
    assert rebind["recovered_claim_epoch"] == 3
    # The authenticated evidence is republished in canonical history.
    assert _recovery_event_payload(coordinator_repo, task_id)["rejection_rebind"] == (
        rebind
    )

    result = _reroute(task_id)

    assert result["ok"] is True, result
    assert result["to_model"] == _CANONICAL_TO_MODEL
    row = _row(coordinator_repo, task_id)
    assert row["task_id"] == task_id
    assert row["runner"] == _TO_RUNNER
    assert row["status"] == "pending"
    assert row["worker_status"] == "unclaimed"
    after = _card(coordinator_repo, task_id)
    # No card is cloned and the sealed predecessor is byte-identical.
    assert after["task_id"] == task_id
    assert after["rework_predecessor"] == predecessor_before
    assert after["rework_predecessor"]["request_id"] == _REJECTED_REQUEST
    assert (
        after["rework_predecessor"]["changed_path_hashes"]
        == predecessor_before["changed_path_hashes"]
    )
    authorization = result["manager_rejection_authorization"]
    assert authorization["request_id"] == _REJECTED_REQUEST
    assert authorization["claim_epoch"] == 1
    authenticated = authorization["rejection_recovery_rebind"]
    assert authenticated["schema_id"] == "aiworkhub.rejection_recovery_reroute.v1"
    assert authenticated["blocked_claim_epoch"] == 2
    assert authenticated["recovery_epoch"] == 3
    assert len(authenticated["recovery_event_sha256"]) == 64


@pytest.mark.parametrize(
    ("cycles", "attach", "expected_epoch"),
    [
        pytest.param(1, True, 3, id="failed_rework_launch_after_attach"),
        pytest.param(2, False, 5, id="two_blocked_recovery_cycles"),
    ],
)
def test_nf01196_reroute_after_blocked_recovery_variants(
    coordinator_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    cycles: int,
    attach: bool,
    expected_epoch: int,
) -> None:
    task_id = f"NF01196_VARIANT_{cycles}_{int(attach)}"
    card = _field_sequence(
        coordinator_repo, monkeypatch, task_id=task_id, cycles=cycles, attach=attach
    )
    predecessor_before = card["rework_predecessor"]
    assert card["claim_epoch"] == expected_epoch
    assert card["blocked_recovery_rejection_rebind"]["recovered_claim_epoch"] == (
        expected_epoch
    )

    result = _reroute(task_id)

    assert result["ok"] is True, result
    row = _row(coordinator_repo, task_id)
    assert row["task_id"] == task_id
    assert row["runner"] == _TO_RUNNER
    after = _card(coordinator_repo, task_id)
    assert after["rework_predecessor"] == predecessor_before
    rebind = result["manager_rejection_authorization"]["rejection_recovery_rebind"]
    assert rebind["recovery_epoch"] == expected_epoch
    assert rebind["claim_epoch"] == 1


@pytest.mark.parametrize(
    "tamper",
    [
        "other_task",
        "other_request",
        "stale_claim_epoch",
        "other_actor",
        "predecessor_hashes_differ",
        "superseded_by_later_claim",
    ],
)
def test_nf01196_tampered_recovery_does_not_authorize_reroute(
    coordinator_repo: Path, monkeypatch: pytest.MonkeyPatch, tamper: str,
) -> None:
    task_id = f"NF01196_TAMPERED_{tamper.upper()}"
    card = _field_sequence(coordinator_repo, monkeypatch, task_id=task_id)
    payload = _recovery_event_payload(coordinator_repo, task_id)

    if tamper == "other_task":
        payload["rejection_rebind"]["task_id"] = "NF01196_SOME_OTHER_TASK"
        _rewrite_recovery_event(coordinator_repo, task_id, payload=payload)
    elif tamper == "other_request":
        payload["predecessor"]["request_id"] = "e" * 32
        payload["rejection_rebind"]["predecessor_request_id"] = "e" * 32
        _rewrite_recovery_event(coordinator_repo, task_id, payload=payload)
    elif tamper == "stale_claim_epoch":
        payload["claim_epoch"] = int(payload["claim_epoch"]) + 1
        _rewrite_recovery_event(coordinator_repo, task_id, payload=payload)
    elif tamper == "other_actor":
        payload["actor"] = "not-the-verified-manager"
        _rewrite_recovery_event(
            coordinator_repo,
            task_id,
            payload=payload,
            runner="not-the-verified-manager",
        )
    elif tamper == "predecessor_hashes_differ":
        card["recovery_predecessor"] = {
            **card["recovery_predecessor"],
            "changed_path_hashes": {"out/result.json": "f" * 64},
        }
        _write_card(coordinator_repo, task_id, card)
    else:
        _append_later_claim_event(coordinator_repo, task_id)

    result = _reroute(task_id)

    assert result["ok"] is False, result
    assert _MISMATCH in result["stderr"]
    assert _row(coordinator_repo, task_id)["runner"] == _RUNNER
    # The refusal mutates nothing: the sealed candidate stays exactly as the
    # rejection pinned it.
    assert (
        _card(coordinator_repo, task_id)["rework_predecessor"]["request_id"]
        == _REJECTED_REQUEST
    )


def test_nf01196_recovery_without_a_current_rejection_seals_no_rebind(
    coordinator_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovery seals nothing when the rejection is not current for the
    sealed predecessor, so the gate keeps refusing the epoch change."""
    task_id = "NF01196_STALE_REJECTION"
    _pin_retained_changed_paths(monkeypatch)
    _insert_pending(coordinator_repo, task_id)
    _reject_reviewed_candidate(coordinator_repo, task_id)
    stale = _card(coordinator_repo, task_id)
    stale["rejection_disposition"]["pinned_at"] = "2026-01-01T00:00:00+00:00"
    _write_card(coordinator_repo, task_id, stale)
    _block_rework_attempt(
        coordinator_repo, task_id, request_id=_REWORK_REQUESTS[0], claim_epoch=2
    )

    card = _recover(coordinator_repo, task_id)

    assert "blocked_recovery_rejection_rebind" not in card
    assert "rejection_rebind" not in _recovery_event_payload(coordinator_repo, task_id)
    result = _reroute(task_id)
    assert result["ok"] is False, result
    assert _MISMATCH in result["stderr"]


def test_nf01196_recovery_without_a_rejection_request_id_on_either_side_does_not_raise(
    coordinator_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejection/predecessor pair that both lack ``request_id`` must not
    satisfy the rebind gate by comparing two empty strings: sealing the
    rebind body previously subscripted the missing key and raised
    ``KeyError``.  The recovery must still complete and seal no rebind."""
    task_id = "NF01196_MISSING_REQUEST_ID_BOTH_SIDES"
    _pin_retained_changed_paths(monkeypatch)
    _insert_pending(coordinator_repo, task_id)
    _reject_reviewed_candidate(coordinator_repo, task_id)
    stripped = _card(coordinator_repo, task_id)
    stripped["rejection_disposition"] = {
        key: value
        for key, value in stripped["rejection_disposition"].items()
        if key != "request_id"
    }
    stripped["rework_predecessor"] = {
        key: value
        for key, value in stripped["rework_predecessor"].items()
        if key != "request_id"
    }
    assert stripped["rejection_disposition"]["schema_id"] == (
        "aiworkhub.rejection_disposition.v1"
    )
    assert stripped["rejection_disposition"]["pinned_at"] == (
        stripped["rework_predecessor"]["pinned_at"]
    )
    assert stripped["rejection_disposition"]["pinned_at"]
    _write_card(coordinator_repo, task_id, stripped)
    _block_rework_attempt(
        coordinator_repo, task_id, request_id=_REWORK_REQUESTS[0], claim_epoch=2
    )

    card = _recover(coordinator_repo, task_id)

    assert "blocked_recovery_rejection_rebind" not in card
    assert "rejection_rebind" not in _recovery_event_payload(coordinator_repo, task_id)
