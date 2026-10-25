"""NF-2026-01662: a retried attempt carries the failed attempt's retained changes.

``core.retry_terminal_task`` and ``task_store.mark_transient_retry`` used to
requeue the card with no ``rework_predecessor``, so the next launch started
from HEAD while the failed attempt's changed files sat in its retained
worktree.  Both now pin the attempt's in-scope, hash-verified changes through
the predecessor helper ``recover_blocked_rework`` shares, and launch
preparation materializes them exactly as it does for a recovered rework
(``test_rework_predecessor_retention``).  An attempt without changes retries
exactly as before; an out-of-scope change is recorded and never carried.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import (
    core,
    process_event_ledger,
    process_launcher,
    process_launcher_evidence,
    successful_rework_recovery,
    task_store,
    worker_workspace,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_rework_predecessor_retention import _git  # noqa: E402
from test_terminal_retry_reconciliation import (  # noqa: E402
    _insert_blocked,
    _row,
    coordinator_repo,  # noqa: F401 -- the fixture ``repo`` builds on
)

TASK_ID = "RETRY_CARRIES_ATTEMPT_DELTA"
RUNNER = "worker_runner"
TOPIC = "terminal_retry"
REQUEST_ID = "e" * 32
SUCCESSOR_REQUEST_ID = "f" * 32
IN_SCOPE = "out/result.txt"
OUT_OF_SCOPE = "out/stray.txt"
CANDIDATE = b"attempt candidate\n"


def _anchored_regular_read_denial() -> str | None:
    """Return why an anchored regular-file read is denied here, or ``None``.

    ``successful_rework_recovery._read_regular`` opens every parent from the
    drive root; a restricted token (the AppContainer worker lane) cannot open
    that root, so sealing a retained attempt fails there by sandbox policy,
    not by a defect.  Probe the capability instead of assuming it.
    """
    try:
        # The same temp base ``tmp_path`` (and so every retained worktree the
        # tests build) is created under, so the probe walks the same drive root.
        probe_dir = Path(tempfile.mkdtemp(prefix="anchored-read-probe-"))
    except OSError as exc:
        return f"probe_setup_failed:{type(exc).__name__}"
    try:
        probe = probe_dir / "probe.txt"
        probe.write_bytes(b"probe\n")
        try:
            successful_rework_recovery._read_regular(probe, probe_dir, 64)
        except successful_rework_recovery.SuccessfulReworkRecoveryError as exc:
            cause = type(exc.__cause__).__name__ if exc.__cause__ is not None else "none"
            return f"{exc}:{cause}"
        return None
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


_ANCHORED_READ_DENIAL = _anchored_regular_read_denial()
_requires_anchored_reads = pytest.mark.skipif(
    _ANCHORED_READ_DENIAL is not None,
    reason=f"validation_unsupported_in_sandbox:anchored_regular_read_denied:{_ANCHORED_READ_DENIAL}",
)


@pytest.fixture
def repo(coordinator_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The retained worktree must sit at the repository-owned default the
    # predecessor authentication requires, and the launch ledger is per test.
    monkeypatch.delenv(worker_workspace.WORKTREE_ROOT_ENV, raising=False)
    monkeypatch.delenv(worker_workspace.RUNTIME_ROOT_ENV, raising=False)
    monkeypatch.setenv(
        process_launcher.PROCESS_LOG_ENV,
        str(coordinator_repo.parent / "process-events.jsonl"),
    )
    monkeypatch.setenv(process_launcher.PROCESS_DIR_ENV, str(_process_dir(coordinator_repo)))
    assert _git(coordinator_repo, "init", "-q").returncode == 0
    assert _git(coordinator_repo, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(coordinator_repo, "config", "user.name", "Task MCP Tests").returncode == 0
    (coordinator_repo / "out").mkdir()
    (coordinator_repo / IN_SCOPE).write_bytes(b"result-v1\n")
    assert _git(coordinator_repo, "add", IN_SCOPE).returncode == 0
    assert _git(coordinator_repo, "commit", "-qm", "fixture").returncode == 0
    return coordinator_repo


def _insert_processing(repo: Path) -> None:
    now = "2026-10-25T00:00:00+00:00"
    card = {
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": TOPIC,
        "objective": "carry the attempt delta",
        "status": "processing",
        "worker_status": "claimed",
        "claimed_by": RUNNER,
        "claim_epoch": 1,
        "launch_request_id": REQUEST_ID,
        "allowed_writes": [IN_SCOPE],
    }
    conn = sqlite3.connect(task_store.storage_readiness(repo).canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,claimed_by,claimed_at,started_at,completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                TASK_ID, RUNNER, TOPIC, "solo", "processing", "claimed", "normal",
                card["objective"], json.dumps(card, sort_keys=True), now, now,
                RUNNER, now, now, None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _attempt(repo: Path, *, files: dict[str, bytes]) -> Any:
    _insert_processing(repo)
    workspace = worker_workspace.create_workspace(
        repo, REQUEST_ID, {"allowed_writes": [IN_SCOPE]}, "validation"
    )
    assert workspace.path == (
        repo.resolve() / ".aiworkhub" / "runtime" / "worktrees" / REQUEST_ID / "worktree"
    )
    for relative, content in files.items():
        (workspace.path / relative).write_bytes(content)
    return workspace


def _metadata() -> dict[str, Any]:
    return {"task_id": TASK_ID, "runner": RUNNER, "topic": TOPIC, "claim_epoch": 1}


def _block_timed_out(repo: Path, workspace: Any, changed: list[str]) -> None:
    """Terminalize the attempt with the evidence the launcher's timeout path records."""
    evidence = process_launcher_evidence.retained_candidate_seal_evidence(
        workspace, _metadata(), REQUEST_ID, changed, "processing"
    )
    assert task_store.mark_terminal_failure(
        repo,
        TASK_ID,
        runner=RUNNER,
        substatus="timed_out",
        evidence={
            **evidence,
            "request_id": REQUEST_ID,
            "error": "timed_out",
            "changed_paths": changed,
        },
        request_id=REQUEST_ID,
        claim_epoch=1,
    ) == (True, "blocked")


def _process_dir(repo: Path) -> Path:
    return repo.parent / "processes"


def _record_launch(
    repo: Path,
    workspace: Any,
    *,
    metadata_path: Path | None = None,
    workspace_metadata: dict[str, Any] | None = None,
) -> None:
    """The launch metadata a transient attempt is found by, as the launcher writes it."""
    if metadata_path is None:
        metadata_path = _process_dir(repo) / f"{REQUEST_ID}.request.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps({
            **_metadata(),
            "request_id": REQUEST_ID,
            "metadata_path": str(metadata_path),
            "workspace": workspace_metadata or workspace.as_metadata(),
        }),
        encoding="utf-8",
    )
    process_event_ledger.append_event(
        repo.parent / "process-events.jsonl",
        {
            "request_id": REQUEST_ID,
            "task_id": TASK_ID,
            "state": "running",
            "metadata_path": str(metadata_path),
        },
    )


def _card(repo: Path) -> dict[str, Any]:
    return json.loads(_row(repo, TASK_ID)["card_json"])


def _execute(repo: Path, sql: str, params: tuple[Any, ...]) -> None:
    conn = sqlite3.connect(task_store.storage_readiness(repo).canonical_db)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _write_card(repo: Path, card: dict[str, Any]) -> None:
    _execute(
        repo,
        "UPDATE tasks SET card_json=? WHERE task_id=?",
        (json.dumps(card, sort_keys=True), TASK_ID),
    )


def _retry_transient(repo: Path) -> None:
    assert task_store.mark_transient_retry(
        repo, TASK_ID, runner=RUNNER, reason="provider_at_capacity",
        request_id=REQUEST_ID,
    ) == (True, "pending")


def _assert_requeued_without_carry(card: dict[str, Any], receipt: str, reason: str) -> None:
    assert (card["status"], card["worker_status"]) == ("pending", "unclaimed")
    assert "rework_predecessor" not in card
    assert card[receipt]["predecessor_carry"] == reason


def _materialize(repo: Path, predecessor: dict[str, Any]) -> Any:
    return worker_workspace.create_workspace(
        repo,
        SUCCESSOR_REQUEST_ID,
        {"allowed_writes": [IN_SCOPE], "rework_predecessor": predecessor},
        "validation",
    )


def _cleanup(repo: Path, *workspaces: Any) -> None:
    for workspace in workspaces:
        if workspace is not None:
            worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_retry_terminal_carries_retained_in_scope_change(repo: Path) -> None:
    workspace = _attempt(repo, files={IN_SCOPE: CANDIDATE})
    successor = None
    try:
        _block_timed_out(repo, workspace, [IN_SCOPE])

        result = core.retry_terminal_task(TASK_ID, REQUEST_ID, "timed_out", "provider back")

        assert result["ok"] is True, result
        row = _row(repo, TASK_ID)
        assert (row["status"], row["worker_status"]) == ("pending", "unclaimed")
        card = json.loads(row["card_json"])
        assert card["terminal_retry"]["predecessor_carry"] == "carried"
        assert card["terminal_retry"]["carried_paths"] == [IN_SCOPE]
        predecessor = card["rework_predecessor"]
        assert predecessor["schema_id"] == "aiworkhub.rework_predecessor.v1"
        assert predecessor["request_id"] == REQUEST_ID
        assert predecessor["changed_path_hashes"] == {
            IN_SCOPE: hashlib.sha256(CANDIDATE).hexdigest()
        }

        successor = _materialize(repo, predecessor)
        assert (successor.path / IN_SCOPE).read_bytes() == CANDIDATE
        assert successor.inherited_rework_paths == (IN_SCOPE,)
    finally:
        _cleanup(repo, successor, workspace)


def test_retry_terminal_records_and_drops_out_of_scope_change(repo: Path) -> None:
    workspace = _attempt(repo, files={IN_SCOPE: CANDIDATE, OUT_OF_SCOPE: b"stray\n"})
    successor = None
    try:
        _block_timed_out(repo, workspace, [IN_SCOPE, OUT_OF_SCOPE])

        assert core.retry_terminal_task(TASK_ID, REQUEST_ID, "timed_out")["ok"] is True

        card = _card(repo)
        assert card["terminal_retry"]["predecessor_carry"] == "carried"
        assert card["terminal_retry"]["out_of_scope_paths"] == [OUT_OF_SCOPE]
        predecessor = card["rework_predecessor"]
        assert predecessor["changed_paths"] == [IN_SCOPE]
        assert set(predecessor["changed_path_hashes"]) == {IN_SCOPE}

        successor = _materialize(repo, predecessor)
        assert (successor.path / IN_SCOPE).read_bytes() == CANDIDATE
        assert not (successor.path / OUT_OF_SCOPE).exists()
    finally:
        _cleanup(repo, successor, workspace)


def test_retry_terminal_without_changes_retries_exactly_as_before(
    coordinator_repo: Path,
) -> None:
    _insert_blocked(
        coordinator_repo, task_id=TASK_ID, request_id=REQUEST_ID, substatus="worker_failed"
    )
    before = _card(coordinator_repo)

    assert core.retry_terminal_task(TASK_ID, REQUEST_ID, "worker_failed")["ok"] is True

    card = _card(coordinator_repo)
    assert card["status"] == "pending"
    assert card["rework_predecessor"] == before["rework_predecessor"]
    assert card["terminal_retry"]["predecessor_carry"] == "no_changes"


def test_transient_retry_carries_retained_in_scope_change(repo: Path) -> None:
    workspace = _attempt(repo, files={IN_SCOPE: CANDIDATE})
    successor = None
    try:
        _record_launch(repo, workspace)

        assert task_store.mark_transient_retry(
            repo, TASK_ID, runner=RUNNER, reason="provider_at_capacity",
            request_id=REQUEST_ID,
        ) == (True, "pending")

        card = _card(repo)
        assert card["transient_retry"]["predecessor_carry"] == "carried"
        predecessor = card["rework_predecessor"]
        assert predecessor["request_id"] == REQUEST_ID
        assert predecessor["changed_path_hashes"] == {
            IN_SCOPE: hashlib.sha256(CANDIDATE).hexdigest()
        }

        successor = _materialize(repo, predecessor)
        assert (successor.path / IN_SCOPE).read_bytes() == CANDIDATE
        assert successor.inherited_rework_paths == (IN_SCOPE,)
    finally:
        _cleanup(repo, successor, workspace)


def test_transient_retry_records_and_drops_out_of_scope_change(repo: Path) -> None:
    workspace = _attempt(repo, files={IN_SCOPE: CANDIDATE, OUT_OF_SCOPE: b"stray\n"})
    successor = None
    try:
        _record_launch(repo, workspace)

        assert task_store.mark_transient_retry(
            repo, TASK_ID, runner=RUNNER, reason="provider_at_capacity",
            request_id=REQUEST_ID,
        ) == (True, "pending")

        card = _card(repo)
        assert card["transient_retry"]["predecessor_carry"] == "carried"
        assert card["transient_retry"]["out_of_scope_paths"] == [OUT_OF_SCOPE]
        predecessor = card["rework_predecessor"]
        assert predecessor["changed_paths"] == [IN_SCOPE]

        successor = _materialize(repo, predecessor)
        assert (successor.path / IN_SCOPE).read_bytes() == CANDIDATE
        assert not (successor.path / OUT_OF_SCOPE).exists()
    finally:
        _cleanup(repo, successor, workspace)


def test_transient_retry_without_changes_retries_exactly_as_before(repo: Path) -> None:
    workspace = _attempt(repo, files={})
    try:
        _record_launch(repo, workspace)

        assert task_store.mark_transient_retry(
            repo, TASK_ID, runner=RUNNER, reason="provider_at_capacity",
            request_id=REQUEST_ID,
        ) == (True, "pending")

        card = _card(repo)
        assert card["status"] == "pending"
        assert card["transient_retry_attempts"] == 1
        assert "rework_predecessor" not in card
        assert card["transient_retry"]["predecessor_carry"] == "no_changes"
    finally:
        _cleanup(repo, workspace)


@pytest.mark.parametrize(
    ("forgery", "reason"),
    [
        ("metadata_outside_process_dir", "attempt_launch_metadata_unauthenticated"),
        ("workspace_outside_runtime_worktree", "attempt_workspace_unpinned"),
        ("workspace_names_foreign_repo", "attempt_workspace_unpinned"),
    ],
)
def test_transient_retry_never_reads_an_unauthenticated_attempt_workspace(
    repo: Path, monkeypatch: pytest.MonkeyPatch, forgery: str, reason: str
) -> None:
    workspace = _attempt(repo, files={IN_SCOPE: CANDIDATE})
    try:
        if forgery == "metadata_outside_process_dir":
            _record_launch(
                repo, workspace, metadata_path=repo.parent / f"{REQUEST_ID}.request.json"
            )
        else:
            foreign = repo.parent / "foreign" / "worktree"
            (foreign / "out").mkdir(parents=True)
            (foreign / IN_SCOPE).write_bytes(CANDIDATE)
            field = "path" if forgery == "workspace_outside_runtime_worktree" else "repo"
            _record_launch(
                repo,
                workspace,
                workspace_metadata={**workspace.as_metadata(), field: str(foreign)},
            )
        called: list[str] = []

        def forbidden(name: str) -> Any:
            def refuse(*_args: Any, **_kwargs: Any) -> Any:
                called.append(name)
                raise AssertionError(f"{name} read an unauthenticated workspace")

            return refuse

        with monkeypatch.context() as patch:
            patch.setattr(worker_workspace, "changed_paths", forbidden("changed_paths"))
            patch.setattr(
                process_launcher_evidence,
                "retained_candidate_seal_evidence",
                forbidden("retained_candidate_seal_evidence"),
            )
            _retry_transient(repo)

        assert called == []
        card = _card(repo)
        _assert_requeued_without_carry(card, "transient_retry", reason)
        assert card["transient_retry_attempts"] == 1
    finally:
        _cleanup(repo, workspace)


def test_transient_retry_reads_only_the_verified_launch_metadata(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger may spell the launcher's own file another way; only the
    verified launcher path is opened, never the ledger-named spelling."""
    from aiworkhub import task_retention

    workspace = _attempt(repo, files={IN_SCOPE: CANDIDATE})
    launch_metadata = _process_dir(repo) / f"{REQUEST_ID}.request.json"
    try:
        _record_launch(
            repo,
            workspace,
            metadata_path=_process_dir(repo) / "alias" / ".." / launch_metadata.name,
        )
        read = task_retention._read_request_metadata
        opened: list[str] = []

        def record(path: Path) -> Any:
            opened.append(str(path))
            return read(path)

        monkeypatch.setattr(task_retention, "_read_request_metadata", record)
        _retry_transient(repo)

        named = [path for path in opened if Path(path).name == launch_metadata.name]
        assert named and all(path == str(launch_metadata) for path in named), opened
        assert _card(repo)["transient_retry"]["predecessor_carry"] == "carried"
    finally:
        _cleanup(repo, workspace)


@pytest.mark.parametrize("change", ["claim_epoch", "request_id"])
def test_transient_retry_drops_a_carry_whose_attempt_changed_before_the_lease(
    repo: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    workspace = _attempt(repo, files={IN_SCOPE: CANDIDATE})
    try:
        _record_launch(repo, workspace)
        derive = task_store._transient_retry_rework_carry

        def raced(*args: Any, **kwargs: Any) -> Any:
            predecessor, receipt = derive(*args, **kwargs)
            assert receipt["predecessor_carry"] == "carried"
            if change == "claim_epoch":
                # Another claim episode began between the derivation and the lease.
                _write_card(repo, {**_card(repo), "claim_epoch": 2})
                return predecessor, receipt
            return {**predecessor, "request_id": SUCCESSOR_REQUEST_ID}, receipt

        monkeypatch.setattr(task_store, "_transient_retry_rework_carry", raced)
        _retry_transient(repo)

        _assert_requeued_without_carry(
            _card(repo), "transient_retry", "attempt_claim_changed"
        )
    finally:
        _cleanup(repo, workspace)


@pytest.mark.parametrize(
    ("refusal", "reason"),
    [
        ("bytes_rewritten_after_attempt", "retained_terminal_candidate_hash_mismatch"),
        ("authentication_raises", "predecessor_carry_failed:RuntimeError"),
    ],
)
def test_retry_terminal_records_an_attempt_carry_refusal(
    repo: Path, monkeypatch: pytest.MonkeyPatch, refusal: str, reason: str
) -> None:
    workspace = _attempt(repo, files={IN_SCOPE: CANDIDATE})
    try:
        _block_timed_out(repo, workspace, [IN_SCOPE])
        if refusal == "bytes_rewritten_after_attempt":
            (workspace.path / IN_SCOPE).write_bytes(b"rewritten after the attempt\n")
        else:
            def unavailable(*_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError("authentication unavailable")

            monkeypatch.setattr(
                task_store, "retained_terminal_rework_predecessor", unavailable
            )

        assert core.retry_terminal_task(TASK_ID, REQUEST_ID, "timed_out")["ok"] is True

        _assert_requeued_without_carry(_card(repo), "terminal_retry", reason)
    finally:
        _cleanup(repo, workspace)


@pytest.mark.parametrize(
    ("attestation", "reason"),
    [
        ("card_attestation_rewritten", "retained_terminal_candidate_event_mismatch"),
        ("newer_event_differs", "retained_terminal_candidate_event_mismatch"),
        ("newer_event_unreadable", "terminal_failure_event_unreadable"),
    ],
)
def test_retry_terminal_records_an_unattested_terminal_failure(
    repo: Path, attestation: str, reason: str
) -> None:
    workspace = _attempt(repo, files={IN_SCOPE: CANDIDATE})
    try:
        _block_timed_out(repo, workspace, [IN_SCOPE])
        if attestation == "card_attestation_rewritten":
            card = _card(repo)
            _write_card(
                repo, {**card, "terminal_failure": {**card["terminal_failure"], "note": "x"}}
            )
        else:
            payload = (
                json.dumps({"request_id": REQUEST_ID})
                if attestation == "newer_event_differs"
                else "{not json"
            )
            _execute(
                repo,
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                "VALUES (?, 'terminal_failure', ?, ?, ?)",
                (TASK_ID, RUNNER, payload, "2026-10-25T00:00:01+00:00"),
            )

        assert core.retry_terminal_task(TASK_ID, REQUEST_ID, "timed_out")["ok"] is True

        _assert_requeued_without_carry(_card(repo), "terminal_retry", reason)
    finally:
        _cleanup(repo, workspace)


@_requires_anchored_reads
@pytest.mark.parametrize("seal", ["pinned_at_retry", "sealed_by_janitor"])
def test_transient_retry_relaunch_materializes_only_the_sealed_attempt_bytes(
    repo: Path, monkeypatch: pytest.MonkeyPatch, seal: str
) -> None:
    """Retry, seal, release the attempt's worktree, relaunch: the bytes survive."""
    workspace = _attempt(repo, files={IN_SCOPE: CANDIDATE, OUT_OF_SCOPE: b"stray\n"})
    successor = None
    try:
        _record_launch(repo, workspace)
        with monkeypatch.context() as patch:
            if seal == "sealed_by_janitor":
                # The attempt's own seal is refused by name, as an unsafe or
                # over-limit capture is: only its hashes are pinned.
                patch.setattr(
                    process_launcher,
                    "_terminal_rework_delta_evidence",
                    lambda *_args, **_kwargs: {
                        "schema_id": "aiworkhub.rework_delta_seal.v1",
                        "sealed": False,
                        "reason": "rework_delta_seal_refused",
                    },
                )
            _retry_transient(repo)
        predecessor = _card(repo)["rework_predecessor"]
        assert predecessor["changed_paths"] == [IN_SCOPE]
        if seal == "sealed_by_janitor":
            assert "delta_artifact" not in predecessor
            # ``ProcessManager._seal_pinned_rework_predecessor``: one capture of
            # the pinned worktree, sealed, then attached to the exact predecessor.
            hashes = predecessor["changed_path_hashes"]
            descriptor = process_launcher._terminal_rework_delta_evidence(
                workspace,
                {"task_id": TASK_ID, "claim_epoch": predecessor["claim_epoch"]},
                REQUEST_ID,
                sorted(hashes),
                captured_entries=successful_rework_recovery.candidate_entries(
                    workspace.path, hashes
                ),
            )
            assert descriptor["sealed"] is True
            assert task_store.attach_rework_delta(
                repo,
                TASK_ID,
                predecessor_request_id=REQUEST_ID,
                claim_epoch=predecessor["claim_epoch"],
                descriptor=descriptor,
            ) == (True, "attached")
            predecessor = _card(repo)["rework_predecessor"]
        artifact = predecessor["delta_artifact"]
        assert hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest() == (
            artifact["digest"]
        )

        # The janitor releases the sealed worktree; the relaunch reads the seal only.
        released, workspace = workspace, None
        _cleanup(repo, released)
        assert not released.path.exists()
        successor = _materialize(repo, predecessor)

        carried = (successor.path / IN_SCOPE).read_bytes()
        assert carried == CANDIDATE
        assert hashlib.sha256(carried).hexdigest() == (
            predecessor["changed_path_hashes"][IN_SCOPE]
        )
        assert successor.inherited_rework_paths == (IN_SCOPE,)
        assert not (successor.path / OUT_OF_SCOPE).exists()
    finally:
        _cleanup(repo, successor, workspace)
