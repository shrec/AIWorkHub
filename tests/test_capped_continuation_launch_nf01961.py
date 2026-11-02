"""NF-2026-01961: a continue_capped successor launches with the predecessor candidate.

Field report: the successor of a reject-depth-capped card failed every launch
with ``crash_retry_predecessor_identity_mismatch``.  The inherited predecessor
stays bound to the capped card's id, but the crash-retry builder accepted that
id only through the successor's ``capped_continuation`` marker, while the
rework overlay builder bound ``rework_predecessor.task_id`` unconditionally.
A successor whose marker does not vouch for the predecessor (one continued
before the marker existed; ``inherit_capped_rework`` can never re-mark it)
therefore got an overlay naming the capped card and a crash-retry owner naming
itself: a mismatch on every relaunch.  Both builders now bind one owner, proven
by the marker or by the capped card's canonical supersede record.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from test_capped_continuation_nf01837 import (  # noqa: F401  (``repo`` is a fixture)
    _BASE_OID,
    _PRED_REQUEST,
    _REAL_HAS_VERIFIED_REWORK_DELTA,
    _capped,
    _card,
    _db,
    _rewrite_card,
    _sealed_predecessor,
    repo,
)
from aiworkhub import (  # noqa: E402
    capped_continuation,
    core,
    process_launcher,
    promotion_merge,
    rework_delta_artifact,
    worker_workspace,
)

_SUCCESSOR_REQUEST = "d" * 32


@pytest.fixture
def real_delta(monkeypatch):
    monkeypatch.setattr(
        rework_delta_artifact, "has_verified_rework_delta", _REAL_HAS_VERIFIED_REWORK_DELTA
    )
    # A plain directory is not an isolated worktree: pin its base to the
    # predecessor's so only the delta materialization runs, no drift rebase.
    monkeypatch.setattr(
        promotion_merge, "_isolated_worktree_base_oid", lambda *_a, **_k: _BASE_OID
    )


def _sealed_capped(repo: Path, task_id: str, scratch: Path) -> dict:
    _capped(repo, task_id, with_delta=False)
    predecessor = _sealed_predecessor(repo, task_id, scratch)
    _rewrite_card(repo, task_id, rework_predecessor=predecessor)
    return predecessor


def _continue(repo: Path, task_id: str, scratch: Path) -> tuple[str, dict]:
    predecessor = _sealed_capped(repo, task_id, scratch)
    result = core.continue_capped_task(task_id, reason="continue at the cap")
    assert result["ok"] is True
    return result["continuation"]["successor_task_id"], predecessor


def _drop_marker(repo: Path, task_id: str) -> None:
    """The successor shape a continuation made before the marker existed."""
    conn = sqlite3.connect(_db(repo))
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        card = json.loads(row[0])
        card.pop(capped_continuation.MARKER_KEY)
        conn.execute("UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id))
        conn.commit()
    finally:
        conn.close()


def _provision(repo: Path, card: dict, scratch: Path) -> worker_workspace.WorkerWorkspace:
    """Seed the successor worktree from the inherited delta, as create_workspace does."""
    worktree = scratch / "worktree"
    home = scratch / "home"
    worktree.mkdir(parents=True)
    home.mkdir(parents=True)
    allowed = tuple(card["allowed_writes"])
    seeded, _source = worker_workspace._materialize_rework_predecessor(
        repo.resolve(), worktree, card, allowed
    )
    return worker_workspace.WorkerWorkspace(
        request_id=_SUCCESSOR_REQUEST,
        repo=repo.resolve(),
        path=worktree,
        home=home,
        allowed_writes=allowed,
        parent_baseline={},
        workspace_baseline={},
        inherited_rework_paths=tuple(seeded),
    )


def _crashed_predecessor_logs(repo: Path, owner: str, scratch: Path) -> Path:
    process_dir = scratch / "processes"
    process_dir.mkdir()
    process_launcher.write_json_0600(
        process_dir / f"{_PRED_REQUEST}.request.json",
        {"request_id": _PRED_REQUEST, "task_id": owner, "workspace": {"repo": str(repo)}},
    )
    process_launcher.write_json_0600(
        process_dir / f"{_PRED_REQUEST}.supervisor.json",
        {"state": "supervisor_error", "exit_code": 1, "error": "bridge crashed"},
    )
    (process_dir / f"{_PRED_REQUEST}.stdout.log").write_text("step\n", encoding="utf-8")
    (process_dir / f"{_PRED_REQUEST}.stderr.log").write_text("predecessor-error\n", encoding="utf-8")
    return process_dir


def _launch(repo: Path, successor_id: str, owner: str, scratch: Path):
    """Launch preparation in ProcessManager._launch_isolated order."""
    card = _card(repo, successor_id)
    workspace = _provision(repo, card, scratch)
    _overlay_path, overlay = process_launcher._materialize_worker_rework_overlay(
        workspace, task_id=successor_id, card=card
    )
    process_dir = _crashed_predecessor_logs(repo, owner, scratch)
    _path, packet = process_launcher._materialize_crash_retry_packet(
        process_dir, workspace, task_id=successor_id, card=card, rework_overlay_packet=overlay
    )
    return card, workspace, overlay, packet


def _assert_launches_with_predecessor_bytes(
    successor_id: str, owner: str, predecessor: dict, workspace, overlay, packet
) -> None:
    hashes = predecessor["changed_path_hashes"]
    assert sorted(workspace.inherited_rework_paths) == sorted(hashes)
    for relative, expected in hashes.items():
        actual = hashlib.sha256((workspace.path / relative).read_bytes()).hexdigest()
        assert actual == expected, relative
    assert overlay["successor_task_id"] == successor_id
    assert overlay["predecessor_task_id"] == owner
    assert overlay["predecessor_request_id"] == _PRED_REQUEST
    assert packet is not None
    assert packet["successor_task_id"] == successor_id
    assert packet["predecessor_task_id"] == owner
    assert packet["predecessor_request_id"] == _PRED_REQUEST
    assert "predecessor-error" in packet["stderr_tail"]


def test_unmarked_successor_launches_with_the_sealed_candidate(repo, real_delta, tmp_path):
    successor_id, predecessor = _continue(repo, "NF1961_CAP", tmp_path / "pred")
    _drop_marker(repo, successor_id)
    card = _card(repo, successor_id)
    assert capped_continuation.MARKER_KEY not in card
    # The predecessor is still bound to the capped card, which the canonical
    # archive records as superseded into this successor.
    assert card["rework_predecessor"]["task_id"] == "NF1961_CAP"
    assert _card(repo, "NF1961_CAP")["superseded_by"] == successor_id

    _card_seen, workspace, overlay, packet = _launch(
        repo, successor_id, "NF1961_CAP", tmp_path / "successor"
    )

    _assert_launches_with_predecessor_bytes(
        successor_id, "NF1961_CAP", predecessor, workspace, overlay, packet
    )


def test_marked_successor_still_launches_with_the_sealed_candidate(repo, real_delta, tmp_path):
    successor_id, predecessor = _continue(repo, "NF1961_MARKED", tmp_path / "pred")
    assert _card(repo, successor_id)[capped_continuation.MARKER_KEY]["predecessor_task_id"] == (
        "NF1961_MARKED"
    )

    _card_seen, workspace, overlay, packet = _launch(
        repo, successor_id, "NF1961_MARKED", tmp_path / "successor"
    )

    _assert_launches_with_predecessor_bytes(
        successor_id, "NF1961_MARKED", predecessor, workspace, overlay, packet
    )


def test_an_unrelated_or_cross_request_predecessor_is_still_refused(repo, real_delta, tmp_path):
    successor_id, _predecessor = _continue(repo, "NF1961_OWN", tmp_path / "own")
    _drop_marker(repo, successor_id)
    # A different capped card, never superseded into this successor.
    unrelated = _sealed_capped(repo, "NF1961_UNRELATED", tmp_path / "unrelated")
    card = _card(repo, successor_id)
    workspace = worker_workspace.WorkerWorkspace(
        request_id=_SUCCESSOR_REQUEST,
        repo=repo.resolve(),
        path=tmp_path / "worktree",
        home=tmp_path / "home",
        allowed_writes=tuple(card["allowed_writes"]),
        parent_baseline={},
        workspace_baseline={},
        inherited_rework_paths=("src/existing.py",),
    )
    cross_task = {**card, "rework_predecessor": unrelated}
    assert capped_continuation.launch_predecessor_task_id(
        cross_task, successor_id, authority_repo=repo.resolve()
    ) == successor_id

    with pytest.raises(
        process_launcher.WorkspaceError, match="crash_retry_predecessor_identity_mismatch"
    ):
        process_launcher._materialize_crash_retry_packet(
            tmp_path / "processes",
            workspace,
            task_id=successor_id,
            card=cross_task,
            rework_overlay_packet={
                "predecessor_request_id": _PRED_REQUEST,
                "predecessor_task_id": "NF1961_UNRELATED",
            },
        )

    # The proven lineage still refuses a packet naming another request.
    with pytest.raises(
        process_launcher.WorkspaceError, match="crash_retry_predecessor_identity_mismatch"
    ):
        process_launcher._materialize_crash_retry_packet(
            tmp_path / "processes",
            workspace,
            task_id=successor_id,
            card=card,
            rework_overlay_packet={
                "predecessor_request_id": "7" * 32,
                "predecessor_task_id": "NF1961_OWN",
            },
        )
