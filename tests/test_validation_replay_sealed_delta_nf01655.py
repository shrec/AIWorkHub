"""NF-2026-01655: validation-only replay of a retention-collected predecessor.

Measured 2026-10-22 on needfix-NF-2026-01644: ``recover_blocked_rework(
validation_only_replay=True)`` refused with
``validation_only_replay_workspace_invalid`` because retention had collected
the predecessor's worktree (``workspace_gc_reason=sealed_rework_delta``) while
its sealed delta was still on disk and digest-valid.  The only way forward was
a full model relaunch that re-applied an unchanged delta to rerun validations.

Recovery now rebuilds that worktree at the predecessor's ``base_oid`` from the
authenticated delta, the writer lease re-authenticates that delta on the locked
card, every existing replay check runs unchanged on the rebuilt bytes, and the
launch takes the provider-free lane.  An absent, tampered or since-changed
delta, a rebuilt path whose hash differs, or an earlier rebuild that could not
be removed keeps the old refusal code.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from aiworkhub import core, process_launcher, task_store, worker_workspace

OUTPUT = "out/result.txt"
BASE_CONTENT = b"canonical base\n"
CANDIDATE = b"sealed predecessor candidate\n"
CANDIDATE_HASH = hashlib.sha256(CANDIDATE).hexdigest()
TASK_ID = "needfix-NF-2026-01655-replay"
TOPIC = "nf01655"
RUNNER = "codex"
FIRST_REQUEST = "a" * 32
PREDECESSOR = "b4def82dad73400fa266d9eff5f4e060"
CLAIM_EPOCH = 4
FEEDBACK = "NeedFix: rerun the declared validations on the sealed candidate"
NOW = "2026-10-22T00:00:00+00:00"
REFUSAL = (False, "validation_only_replay_workspace_invalid")
SENTINEL = "nf01655-validation-sentinel"


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def _sentinel(repo: Path) -> Path:
    return repo.parent / SENTINEL


def _validation(repo: Path) -> str:
    """The card's one declared validation: deterministic and provider-free, it
    copies the candidate it validated to ``_sentinel(repo)``, outside every
    worktree the replay provisions and removes.  Validation commands admit no
    ``;``, so it is one expression; the list display keeps the inline code
    from being read as a repository file operand."""
    return (
        "python -c \"[__import__('shutil').copyfile("
        f"'{OUTPUT}', '{_sentinel(repo).as_posix()}')]\""
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(worker_workspace.RUNTIME_ROOT_ENV, raising=False)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "NF-2026-01655 Tests")
    (root / "out").mkdir()
    (root / OUTPUT).write_bytes(BASE_CONTENT)
    _git(root, "add", OUTPUT)
    _git(root, "commit", "-qm", "baseline")
    task_store.initialize_repository(root)
    return root


def _seal_delta(repo: Path, entries: dict[str, bytes]) -> dict[str, Any]:
    """Seal ``entries`` as ``seal_rework_delta_artifact`` does, minus its POSIX
    mode hardening (not under test, and refused by chmod-denying validation
    sandboxes), and return the descriptor reject_review pins for it."""
    payload = {
        "schema_id": worker_workspace.REWORK_DELTA_ARTIFACT_SCHEMA_ID,
        "authority_repo": str(repo.resolve(strict=False)),
        "task_id": TASK_ID,
        "request_id": PREDECESSOR,
        "claim_epoch": CLAIM_EPOCH,
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_base64": base64.b64encode(data).decode("ascii"),
            }
            for path, data in sorted(entries.items())
        ],
    }
    packet = {
        **payload,
        "canonical_digest": worker_workspace._rework_delta_canonical_digest(payload),
    }
    encoded = json.dumps(packet, indent=2, ensure_ascii=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    artifact_dir = worker_workspace.configured_runtime_root(repo) / "rework_deltas"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / f"{digest}.json"
    artifact.write_bytes(encoded)
    return {
        "schema_id": "aiworkhub.rework_delta_descriptor.v1",
        "sealed": True,
        "authority_repo": str(repo.resolve(strict=False)),
        "task_id": TASK_ID,
        "request_id": PREDECESSOR,
        "claim_epoch": CLAIM_EPOCH,
        "artifact_path": str(artifact),
        "artifact_sha256": digest,
    }


def _pin_delta(predecessor: dict[str, Any], descriptor: dict[str, Any]) -> None:
    """Project a sealed descriptor onto the predecessor as reject_review does."""
    predecessor["rework_delta"] = descriptor
    predecessor["delta_artifact"] = {
        "path": descriptor["artifact_path"],
        "digest": descriptor["artifact_sha256"],
    }


def _predecessor(repo: Path, *, collected: bool = True) -> dict[str, Any]:
    """A pinned predecessor whose delta is sealed; retention then collects its
    worktree exactly as the measured GC did, unless ``collected`` is False."""
    workspace = worker_workspace.create_workspace(
        repo, PREDECESSOR, {"allowed_writes": [OUTPUT]}, "deterministic_validation"
    )
    (workspace.path / OUTPUT).write_bytes(CANDIDATE)
    predecessor: dict[str, Any] = {
        "schema_id": "aiworkhub.rework_predecessor.v1",
        "request_id": PREDECESSOR,
        "task_id": TASK_ID,
        "claim_epoch": CLAIM_EPOCH,
        "workspace": workspace.as_metadata(),
        "changed_path_hashes": {OUTPUT: CANDIDATE_HASH},
    }
    _pin_delta(predecessor, _seal_delta(repo, {OUTPUT: CANDIDATE}))
    blocked = {"status": "blocked", "worker_status": "blocked"}
    assert process_launcher.ProcessManager._gc_disposition(
        {**blocked, "rework_predecessor": predecessor}, PREDECESSOR, repo=repo
    ) == (True, "sealed_rework_delta")
    if collected:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)
        assert not workspace.path.exists()
    return predecessor


def _card(
    repo: Path, predecessor: dict[str, Any], *, claim_epoch: int
) -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "topic": TOPIC,
        "runner": RUNNER,
        "claim_epoch": claim_epoch,
        "allowed_writes": [OUTPUT],
        "required_outputs": [OUTPUT],
        "validation": [_validation(repo)],
        "rework_predecessor": predecessor,
        "reject_review": {"reason": FEEDBACK},
    }


def _insert_blocked(
    repo: Path, card: dict[str, Any], event: str, payload: dict[str, Any]
) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) "
            "VALUES (?, ?, ?, 'blocked', 'blocked', ?, ?, ?)",
            (TASK_ID, RUNNER, TOPIC, json.dumps(card, sort_keys=True), NOW, NOW),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (TASK_ID, event, RUNNER, json.dumps(payload, sort_keys=True), NOW),
        )
        conn.commit()
    finally:
        conn.close()


def _recover(repo: Path) -> tuple[bool, str]:
    return task_store.recover_blocked_rework(
        repo,
        TASK_ID,
        actor=core.CODEX_RUNNER,
        feedback_reason=FEEDBACK,
        validation_only_replay=True,
    )


def _blocked_replay(repo: Path, predecessor: dict[str, Any]) -> None:
    """Blocked after the predecessor failed validation: its reviewer transport
    pins the collected worktree together with the sealed delta."""
    identity = {"request_id": PREDECESSOR, "repo": str(repo)}
    _insert_blocked(
        repo,
        _card(repo, predecessor, claim_epoch=CLAIM_EPOCH),
        "terminal_review",
        {
            "substatus": "validation_failed",
            "runner": RUNNER,
            "request_id": PREDECESSOR,
            "task_id": TASK_ID,
            "claim_epoch": CLAIM_EPOCH,
            "recorded_at": NOW,
            "evidence": {
                "request_id": PREDECESSOR,
                "changed_path_hashes": dict(predecessor["changed_path_hashes"]),
                "request_identity": {**identity, "task_id": TASK_ID},
                "workspace": {**identity, "path": predecessor["workspace"]["path"]},
            },
        },
    )


def _pending_replay(repo: Path, predecessor: dict[str, Any]) -> None:
    """Pending again: an earlier replay was recovered, then rejected back with
    this predecessor newly pinned, as reject_review leaves the card."""
    first_hashes = {OUTPUT: hashlib.sha256(b"first generation\n").hexdigest()}
    first = {
        "request_id": FIRST_REQUEST,
        "task_id": TASK_ID,
        "changed_path_hashes": first_hashes,
        "workspace": "collected",
    }
    _insert_blocked(
        repo,
        _card(repo, first, claim_epoch=1),
        "terminal_failure",
        {
            "substatus": "validation_failed",
            "runner": RUNNER,
            "recorded_at": NOW,
            "claim_epoch": 1,
            "evidence": {"changed_path_hashes": dict(first_hashes)},
        },
    )
    assert _recover(repo) == (True, "recovered")
    card = task_store.get_task(repo, TASK_ID)
    assert card is not None and card["status"] == "pending"
    card.pop("validation_only_replay_authorization")
    card["claim_epoch"] = CLAIM_EPOCH
    card["rework_predecessor"] = predecessor
    card["operational_blocker"] = {
        "kind": "launch_blocked",
        "reason": "validation_only_replay_predecessor_mismatch",
    }
    _store_card(repo, card)


def _store_card(repo: Path, card: dict[str, Any]) -> None:
    """Persist ``card`` through a separate connection, as a concurrent writer would."""
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (
                json.dumps(task_store.persistable_card_payload(card), sort_keys=True),
                TASK_ID,
            ),
        )
        conn.commit()
    finally:
        conn.close()


_SETUP: dict[str, Callable[[Path, dict[str, Any]], None]] = {
    "blocked": _blocked_replay,
    "pending": _pending_replay,
}


def _state(repo: Path) -> tuple[object, ...]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, worker_status, claimed_by, card_json FROM tasks "
            "WHERE task_id=?",
            (TASK_ID,),
        ).fetchone()
        events = conn.execute(
            "SELECT event, payload_json FROM task_events WHERE task_id=? ORDER BY rowid",
            (TASK_ID,),
        ).fetchall()
    finally:
        conn.close()
    return tuple(row), tuple(events)


def _snapshot_row(repo: Path) -> tuple[dict[str, Any], int]:
    """The whole task row and its newest event rowid, for ``_restore_row``."""
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (TASK_ID,)).fetchone()
        newest = conn.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM task_events WHERE task_id=?",
            (TASK_ID,),
        ).fetchone()[0]
    finally:
        conn.close()
    return dict(row), int(newest)


def _restore_row(repo: Path, snapshot: tuple[dict[str, Any], int]) -> None:
    """Re-block the task exactly as ``_snapshot_row`` saw it."""
    row, newest = snapshot
    columns = [column for column in row if column != "task_id"]
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE tasks SET {', '.join(f'{column}=?' for column in columns)} "
            "WHERE task_id=?",
            (*(row[column] for column in columns), TASK_ID),
        )
        conn.execute(
            "DELETE FROM task_events WHERE task_id=? AND rowid>?", (TASK_ID, newest)
        )
        conn.commit()
    finally:
        conn.close()


def _spy_rebuilds(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record each rebuild: where, at which base and with which bytes, or why
    it failed."""
    attempts: list[dict[str, Any]] = []
    original = worker_workspace.rebuild_collected_rework_predecessor

    def spy(repo: Path, card: dict[str, Any]) -> worker_workspace.WorkerWorkspace:
        try:
            workspace = original(repo, card)
        except worker_workspace.WorkspaceError as exc:
            attempts.append({"error": str(exc)})
            raise
        attempts.append({
            "path": workspace.path,
            "base_oid": workspace.base_oid,
            "bytes": (workspace.path / OUTPUT).read_bytes(),
        })
        return workspace

    monkeypatch.setattr(worker_workspace, "rebuild_collected_rework_predecessor", spy)
    return attempts


def _launch_lane(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stub_replay: bool = True,
    adapter_id: str = "codex_cli",
) -> tuple[
    list[str], dict[str, Any], dict[str, Any], process_launcher.ProcessManager
]:
    """Select the launch lane through ProcessManager with the provider lane
    trapped.  With ``stub_replay`` a stub stands in for the replay: it
    provisions its own worktree through ``create_workspace`` and runs the
    replay finalizer's mandatory-output gate on it.  Without it the real
    ``_launch_validation_only_replay`` runs, finalizer included."""
    selected: list[str] = []
    replay: dict[str, Any] = {}

    def _preflight(
        self: Any, task_id: str, runner: str, topic: str, adapter_id: str
    ) -> dict[str, Any]:
        current = task_store.get_task(repo, task_id)
        assert current is not None
        return task_store.persistable_card_payload(current)

    def _replay(self: Any, **kwargs: Any) -> dict[str, Any]:
        selected.append("validation_only_replay")
        card = kwargs["card"]
        workspace = worker_workspace.create_workspace(
            self.repo, kwargs["request_id"], card, "deterministic_validation"
        )
        try:
            replay["bytes"] = (workspace.path / OUTPUT).read_bytes()
            records = worker_workspace.validate_required_outputs(
                workspace,
                card["required_outputs"],
                rework_predecessor=card["rework_predecessor"],
                strict_rework_inheritance=True,
                validation_only_replay=True,
            )
            replay["required_outputs"] = [record["path"] for record in records]
        finally:
            worker_workspace.cleanup_workspace(
                workspace.repo, workspace.path, workspace.home
            )
        return {
            "ok": True,
            "execution_mode": "validation_only_replay",
            "provider_launched": False,
        }

    def _provider_dirs(card: Any, adapter_id: str) -> Any:
        selected.append("provider")
        raise process_launcher.LaunchRejected("provider_lane_selected")

    monkeypatch.setattr(process_launcher, "launch_gates_open", lambda: True)
    monkeypatch.setattr(
        process_launcher, "_validate_adapter_identity", lambda *a, **k: None
    )
    monkeypatch.setattr(
        process_launcher, "_enforce_quality_review_launch_binding", lambda *a, **k: None
    )
    monkeypatch.setattr(process_launcher.ProcessManager, "_preflight_card", _preflight)
    if stub_replay:
        monkeypatch.setattr(
            process_launcher.ProcessManager, "_launch_validation_only_replay", _replay
        )
    monkeypatch.setattr(process_launcher, "_external_readonly_dirs", _provider_dirs)
    monkeypatch.setattr(
        process_launcher.task_engine, "record_launch_blocker", lambda *a, **k: {"ok": True}
    )
    manager = process_launcher.ProcessManager(
        repo=repo,
        isolation_enabled=False,
        process_dir=tmp_path / "nf01655-process",
        process_log_path=tmp_path / "nf01655-process.log",
        collision_guard=lambda **kwargs: {"returncode": 0},
    )
    result = manager._launch_isolated(
        task_id=TASK_ID,
        runner=RUNNER,
        topic=TOPIC,
        adapter_id=adapter_id,
        model=None,
        owner_prompt="",
        timeout_seconds=30,
    )
    return selected, result, replay, manager


def _finalized(
    manager: process_launcher.ProcessManager, request_id: str
) -> dict[str, Any]:
    """Wait for the replay's own finalizer thread; return its terminal event."""
    for thread in threading.enumerate():
        if thread.name == f"aiworkhub-validation-replay-{request_id[:8]}":
            thread.join(timeout=300)
            assert not thread.is_alive(), "the validation-only replay never finalized"
    events = [
        event for event in manager._events() if event.get("request_id") == request_id
    ]
    terminal = [
        event
        for event in events
        if event.get("state") in process_launcher.TERMINAL_PROCESS_STATES
    ]
    assert terminal, [event.get("state") for event in events]
    return terminal[-1]


@pytest.mark.parametrize(
    ("branch", "recovered"),
    [("blocked", "recovered"), ("pending", "recovered_validation_only_replay")],
)
def test_reproduction_collected_predecessor_replays_from_its_sealed_delta(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    recovered: str,
) -> None:
    predecessor = _predecessor(repo)
    _SETUP[branch](repo, predecessor)
    before = _state(repo)
    worktree = Path(predecessor["workspace"]["path"])

    # Before the fix nothing rebuilt the collected worktree, and the unchanged
    # gate refused exactly as measured on needfix-NF-2026-01644.
    with monkeypatch.context() as pre_fix:
        # Only the rebuild is switched off, so the unchanged gate is the sole
        # authority that refuses.
        pre_fix.setattr(
            task_store,
            "_collected_replay_predecessor_rebuild",
            lambda *a, **k: contextlib.nullcontext(),
        )
        assert _recover(repo) == REFUSAL
    assert _state(repo) == before

    attempts = _spy_rebuilds(monkeypatch)
    assert _recover(repo) == (True, recovered)

    # Rebuilt once at the predecessor's own path and base with exactly the
    # sealed bytes, then removed again: the sealed delta stays the authority.
    assert attempts == [{
        "path": worktree,
        "base_oid": predecessor["workspace"]["base_oid"],
        "bytes": CANDIDATE,
    }]
    assert not worktree.exists()
    assert not task_store._replay_rebuild_marker(str(worktree)).exists()
    card = task_store.get_task(repo, TASK_ID)
    assert card is not None and card["status"] == "pending"
    authorization = card["validation_only_replay_authorization"]
    assert authorization["predecessor_request_id"] == PREDECESSOR
    assert authorization["changed_path_hashes"] == {OUTPUT: CANDIDATE_HASH}
    assert authorization["next_claim_epoch"] == card["claim_epoch"]
    assert card["rework_predecessor"]["rework_delta"] == predecessor["rework_delta"]

    # The launch takes the provider-free replay lane: no model is launched, and
    # the replay's own worktree carries the sealed bytes past the finalizer's
    # mandatory-output gate.
    selected, result, replay, _manager = _launch_lane(repo, tmp_path, monkeypatch)
    assert selected == ["validation_only_replay"]
    assert result["ok"] is True
    assert result["provider_launched"] is False
    assert replay == {"bytes": CANDIDATE, "required_outputs": [OUTPUT]}


def test_retained_predecessor_is_verified_in_place_and_never_rebuilt(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    predecessor = _predecessor(repo, collected=False)
    _blocked_replay(repo, predecessor)
    attempts = _spy_rebuilds(monkeypatch)
    worktree = Path(predecessor["workspace"]["path"])

    assert _recover(repo) == (True, "recovered")

    assert attempts == []
    assert (worktree / OUTPUT).read_bytes() == CANDIDATE


@pytest.mark.parametrize(
    "damage", ["artifact_tampered", "artifact_missing", "delta_absent"]
)
def test_absent_or_tampered_delta_still_refuses_and_launches_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    predecessor = _predecessor(repo)
    artifact = Path(predecessor["rework_delta"]["artifact_path"])
    if damage == "artifact_tampered":
        artifact.write_bytes(artifact.read_bytes() + b"\n")  # digest mismatch
    elif damage == "artifact_missing":
        artifact.unlink()
    else:
        del predecessor["rework_delta"], predecessor["delta_artifact"]
    _blocked_replay(repo, predecessor)
    before = _state(repo)
    attempts = _spy_rebuilds(monkeypatch)

    assert _recover(repo) == REFUSAL

    assert attempts == []
    assert _state(repo) == before
    assert not Path(predecessor["workspace"]["path"]).exists()
    card = task_store.get_task(repo, TASK_ID)
    assert card is not None and card["status"] == "blocked"
    assert process_launcher._validation_only_replay_authorization(card, TASK_ID) is None


@pytest.mark.parametrize("mismatch", ["sealed_bytes", "materialized_bytes"])
def test_rebuilt_hash_mismatch_refuses_and_discards_the_rebuild(
    repo: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    predecessor = _predecessor(repo)
    if mismatch == "sealed_bytes":
        # A digest-valid delta that does not hold the pinned candidate bytes.
        _pin_delta(predecessor, _seal_delta(repo, {OUTPUT: b"regenerated bytes\n"}))
    else:
        # The materialized bytes drift from the pinned hash after the delta
        # verified; the rebuild's own re-hash must catch it.
        original = worker_workspace.materialize_rework_delta_artifact

        def drifting(**kwargs: Any) -> list[str]:
            written = original(**kwargs)
            (Path(kwargs["worktree"]) / OUTPUT).write_bytes(b"drifted\n")
            return written

        monkeypatch.setattr(worker_workspace, "materialize_rework_delta_artifact", drifting)
    _blocked_replay(repo, predecessor)
    before = _state(repo)
    attempts = _spy_rebuilds(monkeypatch)

    assert _recover(repo) == REFUSAL

    assert len(attempts) == 1
    assert attempts[0]["error"].startswith(f"rework_predecessor_hash_mismatch:{OUTPUT} ")
    assert _state(repo) == before
    assert not Path(predecessor["workspace"]["path"]).exists()
    assert not task_store._replay_rebuild_marker(
        predecessor["workspace"]["path"]
    ).exists()
    card = task_store.get_task(repo, TASK_ID)
    assert card is not None and card["status"] == "blocked"
    assert process_launcher._validation_only_replay_authorization(card, TASK_ID) is None


@pytest.mark.parametrize("branch", ["blocked", "pending"])
@pytest.mark.parametrize("race", ["artifact_overwritten", "delta_artifact_cleared"])
def test_delta_changed_after_the_rebuild_refuses_under_the_lease(
    repo: Path, monkeypatch: pytest.MonkeyPatch, branch: str, race: str
) -> None:
    predecessor = _predecessor(repo)
    _SETUP[branch](repo, predecessor)
    worktree = Path(predecessor["workspace"]["path"])
    original = worker_workspace.rebuild_collected_rework_predecessor
    expected: dict[str, Any] = {"state": _state(repo)}

    def racing(repo_arg: Path, card: dict[str, Any]) -> worker_workspace.WorkerWorkspace:
        # The rebuild authenticated the pinned delta; it changes before the
        # lease, so the rebuilt bytes no longer prove anything.
        workspace = original(repo_arg, card)
        if race == "artifact_overwritten":
            Path(predecessor["rework_delta"]["artifact_path"]).write_bytes(
                b'{"overwritten": "after the rebuild"}\n'
            )
        else:
            current = task_store.get_task(repo, TASK_ID)
            assert current is not None
            del current["rework_predecessor"]["delta_artifact"]
            _store_card(repo, current)
            expected["state"] = _state(repo)
        return workspace

    monkeypatch.setattr(worker_workspace, "rebuild_collected_rework_predecessor", racing)

    assert _recover(repo) == REFUSAL

    assert _state(repo) == expected["state"]
    assert not worktree.exists()
    assert not task_store._replay_rebuild_marker(str(worktree)).exists()


@pytest.mark.parametrize("cleanup", ["restored", "still_failing"])
def test_unremoved_rebuild_keeps_its_marker_and_is_never_trusted(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cleanup: str,
) -> None:
    predecessor = _predecessor(repo)
    _blocked_replay(repo, predecessor)
    blocked = _snapshot_row(repo)
    worktree = Path(predecessor["workspace"]["path"])
    marker = task_store._replay_rebuild_marker(str(worktree))
    real_cleanup = worker_workspace.cleanup_workspace

    def refused(repo_arg: Path, path: Path, home: Path, **kwargs: Any) -> None:
        raise OSError(f"cleanup refused for {path}")

    monkeypatch.setattr(worker_workspace, "cleanup_workspace", refused)
    with caplog.at_level(logging.WARNING, logger=task_store.__name__):
        assert _recover(repo) == (True, "recovered")

    # The failed cleanup is reported, and its marker outlives it.
    assert worktree.exists()
    assert marker.exists()
    assert any(
        record.levelno == logging.WARNING
        and TASK_ID in record.getMessage()
        and str(worktree) in record.getMessage()
        for record in caplog.records
    )

    # Re-blocked with the delta gone, the leftover's matching bytes must not
    # stand in for the retained predecessor.
    if cleanup == "restored":
        monkeypatch.setattr(worker_workspace, "cleanup_workspace", real_cleanup)
    Path(predecessor["rework_delta"]["artifact_path"]).unlink()
    _restore_row(repo, blocked)
    before = _state(repo)
    attempts = _spy_rebuilds(monkeypatch)

    assert _recover(repo) == REFUSAL

    assert attempts == []
    assert _state(repo) == before
    if cleanup == "restored":
        # Removed first, then refused exactly as if nothing had been rebuilt.
        assert not worktree.exists()
        assert not marker.exists()
    else:
        # Still present and still marked: refused without being trusted.
        assert worktree.exists()
        assert marker.exists()


def test_recovered_replay_runs_its_declared_validation_without_a_provider(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    predecessor = _predecessor(repo)
    _blocked_replay(repo, predecessor)
    assert _recover(repo) == (True, "recovered")
    assert not Path(predecessor["workspace"]["path"]).exists()
    assert not _sentinel(repo).exists()

    # The real, unpatched replay launcher: it materializes the sealed delta
    # into its own worktree, claims the task through the write gate and
    # finalizes through the normal finalizer, which runs the card's declared
    # validation.  Only the provider lane stays trapped.  An editor-hosted
    # adapter keeps that validation the trusted manager's shell-free
    # subprocess, so it never nests another OS sandbox inside the one that
    # runs this suite.
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    monkeypatch.setattr(
        process_launcher.core,
        "check_runner_topic_allowlist",
        lambda *a, **k: {"allowed": True},
    )
    # The real key file hardens its DACL, which the AppContainer lane denies.
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_terminal_authority_key",
        lambda self: b"test-key",
    )
    selected, result, _replay, manager = _launch_lane(
        repo, tmp_path, monkeypatch, stub_replay=False, adapter_id="vscode_lm"
    )
    launch_lanes = list(selected)
    assert result["ok"] is True, json.dumps(result, default=str)
    assert result["execution_mode"] == "validation_only_replay"
    assert result["provider_launched"] is False
    terminal = _finalized(manager, result["request_id"])

    assert launch_lanes == []
    assert terminal["execution_mode"] == "validation_only_replay"
    assert terminal["provider_launched"] is False
    assert [
        (row["declared_command"], row["returncode"]) for row in terminal["validation"]
    ] == [(_validation(repo), 0)], json.dumps(terminal, default=str)[:4000]
    # The declared command really ran, on the candidate rebuilt from the
    # sealed delta.
    assert _sentinel(repo).read_bytes() == CANDIDATE
