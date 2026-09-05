"""Regression coverage for the two storage-retention defects fixed today.

DEFECT 1 -- reclaim destroyed in-flight rework lineage. ``plan_worktree_reclaim``
treated a superseded attempt as an eligible candidate without checking whether a
successor attempt had actually run. A card rejected back to ``pending`` carries
``rework_predecessor.request_id`` pointing at the previous attempt's worktree,
and the launcher overlays that worktree's changed files into the new attempt;
reclaiming it stranded the card with ``rework_predecessor_workspace_missing``.
An attempt workspace referenced as ``rework_predecessor`` by any card not in a
finished lifecycle state must be protected exactly as a live worktree is, and
the preview must report *why* each worktree is kept.

DEFECT 2 -- the preview hung to the full request timeout because its on-disk
footprint walk was unbounded, where the dashboard snapshot serves the same
measurement bounded (background thread + cache). ``preview`` now runs the
measurement under a wall-clock deadline and returns a result explicitly
labelled incomplete rather than blocking or reporting a truncated footprint.

Also verified: the read-only lineage connection encodes '#' in the database
path (``Path.resolve().as_uri()``), so a '#' can never drop ``?mode=ro`` and
open the connection read-write/create-if-missing at a truncated path.

Age is exercised via an explicit ``now`` injected into ``preview()`` rather
than by mutating filesystem mtimes: workers run under a landlock sandbox that
forbids ``os.utime``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import needfix_store, storage_retention, task_store

_AGED_NOW_OFFSET_DAYS = 31


def _aged_now() -> float:
    return time.time() + _AGED_NOW_OFFSET_DAYS * 86400


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def repo_with_worktrees(tmp_path: Path) -> dict[str, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    base = tmp_path / "worktrees"
    base.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_path, "clone", str(remote), str(repo))
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "origin", "HEAD:refs/heads/main")
    _git(repo, "fetch", "origin")
    assert task_store.initialize_repository(repo)["ok"]
    return {"repo": repo, "base": base}


def _add_worktree(repo: Path, base: Path, entry_id: str, *, unpushed: bool = True) -> Path:
    entry = base / entry_id
    worktree = entry / "worktree"
    entry.mkdir()
    _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    if unpushed:
        # A deliberately-unpushed local commit: exactly what a rejected/
        # superseded rework attempt looks like on disk. Absent lineage
        # protection this would be an eligible reclaim candidate.
        (worktree / "note.txt").write_text("rework\n", encoding="utf-8")
        _git(worktree, "add", "note.txt")
        _git(worktree, "commit", "-m", "unpushed rework attempt")
    return entry


def _insert_card(
    repo: Path,
    task_id: str,
    *,
    status: str,
    launch_request_id: str = "",
    accepted_request_id: str = "",
    rework_predecessor_request_id: str = "",
    terminal_failure_substatus: str = "",
    terminal_failure_request_id: str = "",
    terminal_review_request_id: str = "",
    updated_at: str | None = None,
) -> None:
    """Insert one canonical task row directly for test purposes.

    ``rework_predecessor_request_id`` mirrors the durable pin the reject path
    writes into ``card_json`` when a card is rejected back to ``pending`` for
    rework: ``card_json["rework_predecessor"]["request_id"]`` names the prior
    attempt's worktree the launcher will overlay into the successor.

    ``terminal_failure_substatus`` mirrors the shape
    ``task_store.mark_terminal_failure`` writes when it parks a genuine
    post-launch operational failure (e.g. ``finalize_failed``) in the
    canonical ``blocked`` bucket: ``card_json["terminal_substatus"]`` and
    ``card_json["terminal_failure"]["request_id"]`` both still name the exact
    attempt that failed, and ``launch_request_id`` is never cleared.
    """
    db_path = task_store.canonical_db_path(repo)
    now = updated_at or datetime.now(timezone.utc).isoformat()
    card: dict[str, object] = {}
    if launch_request_id:
        card["launch_request_id"] = launch_request_id
    if accepted_request_id:
        card["accepted_request_id"] = accepted_request_id
    if rework_predecessor_request_id:
        card["rework_predecessor"] = {"request_id": rework_predecessor_request_id}
    if terminal_failure_substatus:
        card["terminal_substatus"] = terminal_failure_substatus
        card["terminal_failure"] = {
            "substatus": terminal_failure_substatus,
            "request_id": terminal_failure_request_id or launch_request_id,
        }
        if terminal_review_request_id:
            # A distinct ``terminal_review`` envelope alongside the failure and
            # launch ids, so a test can drive a three-way identity disagreement.
            card["terminal_review"] = {
                "substatus": terminal_failure_substatus,
                "request_id": terminal_review_request_id,
            }
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "claude", "storage", status, "unclaimed", json.dumps(card), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_needfix(repo: Path, needfix_id: str, task_id: str, *, status: str) -> None:
    needfix_store.initialize_repository(repo)
    path = repo / ".aiworkhub" / "tasking" / "needfix.sqlite"
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO needfix(id,dedupe_key,title,description,kind,severity,"
            "provenance_json,status,converted_task_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                needfix_id,
                f"dedupe-{needfix_id}",
                needfix_id,
                "storage lifecycle test",
                "bug",
                "medium",
                "{}",
                status,
                task_id,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _write_request_ledger(
    repo: Path, base: Path, request_id: str, task_id: str
) -> None:
    entry = base / request_id
    home = entry / "home"
    home.mkdir(parents=True, exist_ok=True)
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


def _nonfinished_predecessors(repo: Path) -> set[str]:
    """Every rework_predecessor request id pinned by a card that is not in a
    finished lifecycle state, read straight from the canonical task table."""
    db_path = task_store.canonical_db_path(repo)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT status, worker_status, archived_at, card_json FROM tasks"
        ).fetchall()
    finally:
        conn.close()
    pinned: set[str] = set()
    for row in rows:
        if task_store.canonical_status(dict(row)) in storage_retention._FINISHED_STATUSES:
            continue
        card = json.loads(row["card_json"] or "{}")
        predecessor = card.get("rework_predecessor") if isinstance(card, dict) else None
        if isinstance(predecessor, dict):
            request_id = str(predecessor.get("request_id") or "").strip()
            if request_id:
                pinned.add(request_id)
    return pinned


def test_blocked_finalize_failed_candidate_and_pinned_predecessor_stay_protected(
    repo_with_worktrees,
) -> None:
    """RM27 regression.

    A card ``blocked`` by its own genuine operational terminal failure
    (``finalize_failed``) still names the exact request id that failed via
    both ``launch_request_id`` and ``terminal_failure.request_id`` --
    ``_protected_attempt_ids`` must protect that CURRENT candidate exactly as
    a live worker, not merely the card's older ``rework_predecessor``. Both
    request ids stay out of the reclaim set even when the over-cap forcing
    pass would otherwise pull an aged, under-cap worktree back in, and even
    past the retention TTL -- while an unrelated, genuinely reclaimable
    worktree proves the reclaim pathway itself is still active."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    current_candidate = "a4b772bd2b2d4b1887393e8b7cfd25aa"
    older_predecessor = "af439d0c6d6e4a74a4abdffa50105989"
    _insert_card(
        repo,
        "needfix-NF-2026-00550",
        status="blocked",
        launch_request_id=current_candidate,
        rework_predecessor_request_id=older_predecessor,
        terminal_failure_substatus="finalize_failed",
    )

    now = _aged_now()
    scan = {
        "base": base,
        "worktrees": [
            {
                "id": current_candidate,
                "class": storage_retention.worktree_storage.CLASS_REMOVABLE_SAFE,
                "size_bytes": 1_000_000,
                "modified_at_epoch": now - 100 * 86400,
            },
            {
                "id": older_predecessor,
                "class": storage_retention.worktree_storage.CLASS_REMOVABLE_SAFE,
                "size_bytes": 1_000_000,
                "modified_at_epoch": now - 100 * 86400,
            },
            {
                "id": "unrelated-old-safe",
                "class": storage_retention.worktree_storage.CLASS_REMOVABLE_SAFE,
                "size_bytes": 1_000_000,
                "modified_at_epoch": now - 100 * 86400,
            },
        ],
    }

    plan = storage_retention.plan_worktree_reclaim(
        repo, scan, min_age_days=30, max_bytes=1, current_bytes=10_000_000, now=now,
    )

    kept_ids = {wt["id"] for wt in plan["would_keep"]}
    removed_ids = {wt["id"] for wt in plan["would_remove"]}
    assert current_candidate in kept_ids
    assert older_predecessor in kept_ids
    assert current_candidate not in removed_ids
    assert older_predecessor not in removed_ids
    # The reclaim pathway itself is exercised: an unrelated aged, safe, over-cap
    # worktree is still reclaimed, so the two assertions above are protection,
    # not an inert planner.
    assert "unrelated-old-safe" in removed_ids
    assert plan["protection_reasons"][current_candidate] == (
        "blocked_terminal_candidate_retained"
    )
    assert plan["protection_reasons"][older_predecessor] == "rework_predecessor_retained"


def test_blocked_finalize_failed_conflicting_identity_protects_both_fail_closed(
    repo_with_worktrees,
) -> None:
    """RM27 rework finding: ambiguous retained identity must fail CLOSED.

    When a ``blocked``/``finalize_failed`` card's recorded
    ``terminal_failure.request_id`` disagrees with its own
    ``launch_request_id``, the evidence cannot say which worktree holds the
    failed finalize attempt. Returning no protection there (the prior
    behaviour) converted that ambiguity into reclaim permission for BOTH
    worktrees. Protection must instead keep BOTH implicated ids out of the
    reclaim set -- even when the over-cap forcing pass would otherwise pull an
    aged, under-cap worktree in and even past the retention TTL. An unrelated
    aged worktree still proves the reclaim pathway itself is live."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    launch_candidate = "a4b772bd2b2d4b1887393e8b7cfd25aa"
    recorded_candidate = "af439d0c6d6e4a74a4abdffa50105989"
    _insert_card(
        repo,
        "needfix-NF-2026-00550",
        status="blocked",
        launch_request_id=launch_candidate,
        terminal_failure_substatus="finalize_failed",
        terminal_failure_request_id=recorded_candidate,
    )

    now = _aged_now()
    scan = {
        "base": base,
        "worktrees": [
            {
                "id": wt_id,
                "class": storage_retention.worktree_storage.CLASS_REMOVABLE_SAFE,
                "size_bytes": 1_000_000,
                "modified_at_epoch": now - 100 * 86400,
            }
            for wt_id in (launch_candidate, recorded_candidate, "unrelated-old-safe")
        ],
    }

    plan = storage_retention.plan_worktree_reclaim(
        repo, scan, min_age_days=30, max_bytes=1, current_bytes=10_000_000, now=now,
    )

    removed_ids = {wt["id"] for wt in plan["would_remove"]}
    kept_ids = {wt["id"] for wt in plan["would_keep"]}
    # Ambiguity protects BOTH implicated ids -- never manufactures reclaim.
    assert launch_candidate in kept_ids
    assert recorded_candidate in kept_ids
    assert launch_candidate not in removed_ids
    assert recorded_candidate not in removed_ids
    assert (
        plan["protection_reasons"][launch_candidate]
        == "blocked_terminal_candidate_retained"
    )
    assert (
        plan["protection_reasons"][recorded_candidate]
        == "blocked_terminal_candidate_retained"
    )
    # Not an inert planner: an unrelated aged, safe, over-cap worktree reclaims.
    assert "unrelated-old-safe" in removed_ids


def test_blocked_finalize_failed_three_way_identity_disagreement_protects_all_three(
    repo_with_worktrees,
) -> None:
    """RM27 final rework finding: three-way identity disagreement fails CLOSED.

    A ``blocked``/``finalize_failed`` card can name three DISTINCT request ids
    at once -- ``launch_request_id``, ``terminal_failure.request_id`` and
    ``terminal_review.request_id``. The prior fallback logic chose one
    ``recorded_request_id`` (``terminal_failure`` else ``terminal_review``) and
    unioned it only with the launch id, silently dropping the third envelope's
    id and converting that worktree into a reclaim candidate. Every nonempty id
    must instead be collected independently from all three envelopes, so all
    three implicated worktrees stay out of the reclaim set even when the
    over-cap forcing pass would otherwise pull an aged, under-cap worktree in
    and even past the retention TTL. An unrelated aged worktree still proves the
    reclaim pathway itself is live."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    launch_candidate = "a4b772bd2b2d4b1887393e8b7cfd25aa"
    failure_candidate = "af439d0c6d6e4a74a4abdffa50105989"
    review_candidate = "b7c99114e2f04c0aa1d3ee6655f0aa77"
    _insert_card(
        repo,
        "needfix-NF-2026-00550",
        status="blocked",
        launch_request_id=launch_candidate,
        terminal_failure_substatus="finalize_failed",
        terminal_failure_request_id=failure_candidate,
        terminal_review_request_id=review_candidate,
    )

    now = _aged_now()
    scan = {
        "base": base,
        "worktrees": [
            {
                "id": wt_id,
                "class": storage_retention.worktree_storage.CLASS_REMOVABLE_SAFE,
                "size_bytes": 1_000_000,
                "modified_at_epoch": now - 100 * 86400,
            }
            for wt_id in (
                launch_candidate,
                failure_candidate,
                review_candidate,
                "unrelated-old-safe",
            )
        ],
    }

    plan = storage_retention.plan_worktree_reclaim(
        repo, scan, min_age_days=30, max_bytes=1, current_bytes=10_000_000, now=now,
    )

    removed_ids = {wt["id"] for wt in plan["would_remove"]}
    kept_ids = {wt["id"] for wt in plan["would_keep"]}
    # All THREE independently-named ids are retained -- the third envelope
    # (terminal_review) is never dropped by fallback collapse.
    for candidate in (launch_candidate, failure_candidate, review_candidate):
        assert candidate in kept_ids
        assert candidate not in removed_ids
        assert (
            plan["protection_reasons"][candidate]
            == "blocked_terminal_candidate_retained"
        )
    # Not an inert planner: an unrelated aged, safe, over-cap worktree reclaims.
    assert "unrelated-old-safe" in removed_ids


def test_blocked_finalize_failed_malformed_missing_launch_still_protects_named_id(
    repo_with_worktrees,
) -> None:
    """A malformed terminal record must never manufacture reclaim permission.

    A ``blocked``/``finalize_failed`` card whose ``launch_request_id`` is
    absent (a malformed/partial record) still names its failed attempt via
    ``terminal_failure.request_id``. That named worktree is retained, not
    reclaimed -- fail closed on incomplete identity."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    named = "attempt-malformed-record"
    _insert_card(
        repo,
        "needfix-NF-2026-00552",
        status="blocked",
        terminal_failure_substatus="finalize_failed",
        terminal_failure_request_id=named,
    )

    now = _aged_now()
    scan = {
        "base": base,
        "worktrees": [
            {
                "id": named,
                "class": storage_retention.worktree_storage.CLASS_REMOVABLE_SAFE,
                "size_bytes": 1_000_000,
                "modified_at_epoch": now - 100 * 86400,
            }
        ],
    }

    plan = storage_retention.plan_worktree_reclaim(
        repo, scan, min_age_days=30, max_bytes=1, current_bytes=10_000_000, now=now,
    )

    assert named in {wt["id"] for wt in plan["would_keep"]}
    assert named not in {wt["id"] for wt in plan["would_remove"]}
    assert (
        plan["protection_reasons"][named] == "blocked_terminal_candidate_retained"
    )


def test_preview_to_commit_state_change_cannot_delete_newly_retained_candidate(
    repo_with_worktrees,
) -> None:
    """Preview and the quarantine commit recheck share ONE protection authority.

    A worktree that was a reclaim candidate at preview time becomes protected
    when a concurrent state change parks its card ``blocked`` by a genuine
    ``finalize_failed`` terminal failure naming it. The quarantine commit
    re-runs the SAME measurement, recomputes the digest over the now-shorter
    candidate set, and refuses the stale digest -- so the newly retained
    candidate is never deleted on preview-time evidence."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    request_id = "attempt-newly-retained"
    entry = _add_worktree(repo, base, request_id)

    preview = storage_retention.preview(repo, base=base, now=_aged_now())
    assert request_id in {item["id"] for item in preview["candidates"]}

    # Concurrent state change between preview and commit: the card is parked
    # blocked by its own finalize_failed terminal failure naming this worktree.
    _insert_card(
        repo,
        "needfix-NF-2026-00551",
        status="blocked",
        launch_request_id=request_id,
        terminal_failure_substatus="finalize_failed",
    )

    with pytest.raises(
        storage_retention.StorageRetentionError, match="retention_preview_stale"
    ):
        storage_retention.quarantine(
            repo,
            preview_digest=preview["preview_digest"],
            confirm=True,
            base=base,
            now=_aged_now(),
        )
    # The now-protected candidate was never moved.
    assert entry.is_dir()
    assert (entry / "worktree").is_dir()

    # A fresh preview confirms the shared authority now protects it.
    reconfirm = storage_retention.preview(repo, base=base, now=_aged_now())
    assert request_id not in {item["id"] for item in reconfirm["candidates"]}
    assert {"id": request_id, "reason": "blocked_terminal_candidate_retained"} in (
        reconfirm["protected"]
    )


def test_blocked_manager_park_without_terminal_failure_earns_no_protection(
    repo_with_worktrees,
) -> None:
    """A card merely parked ``blocked`` by a manager review (no genuine
    ``mark_terminal_failure`` record) must not blanket-protect its
    ``launch_request_id``: only a real operational terminal failure does."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    entry = _add_worktree(repo, base, "manager-parked")
    _insert_card(
        repo, "NF-PARKED", status="blocked", launch_request_id="manager-parked"
    )

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    assert "manager-parked" in {item["id"] for item in preview["candidates"]}
    assert entry.is_dir()


def test_rework_predecessor_of_pending_card_is_protected_and_reported(
    repo_with_worktrees,
) -> None:
    """A card rejected to ``pending`` keeps its predecessor attempt protected,
    absent from candidates, and visibly labelled in the preview."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    entry = _add_worktree(repo, base, "attempt-1")
    # Rejected back to pending for rework; the durable pin references attempt-1.
    _insert_card(repo, "NF-1", status="pending", rework_predecessor_request_id="attempt-1")

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    candidate_ids = {item["id"] for item in preview["candidates"]}
    assert "attempt-1" not in candidate_ids
    assert preview["candidate_count"] == 0
    assert {"id": "attempt-1", "reason": "rework_predecessor_retained"} in preview["protected"]
    assert entry.is_dir()  # preview never mutates anything


def test_terminal_needfix_releases_stale_blocked_task_predecessor_pin(
    repo_with_worktrees,
) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    entry = _add_worktree(repo, base, "attempt-closed-needfix")
    _insert_card(
        repo,
        "needfix-NF-2026-00999",
        status="blocked",
        rework_predecessor_request_id="attempt-closed-needfix",
    )
    _insert_needfix(
        repo,
        "NF-2026-00999",
        "needfix-NF-2026-00999",
        status="archived",
    )

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    assert {item["id"] for item in preview["candidates"]} == {
        "attempt-closed-needfix"
    }
    assert preview["pinned_predecessors"] == []
    assert entry.is_dir()


def test_nonterminal_needfix_keeps_blocked_task_predecessor_pin(
    repo_with_worktrees,
) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    _add_worktree(repo, base, "attempt-active-needfix")
    _insert_card(
        repo,
        "needfix-NF-2026-00998",
        status="blocked",
        rework_predecessor_request_id="attempt-active-needfix",
    )
    _insert_needfix(
        repo,
        "NF-2026-00998",
        "needfix-NF-2026-00998",
        status="task_created",
    )

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    assert preview["candidate_count"] == 0
    assert preview["pinned_predecessors"] == [
        {
            "id": "attempt-active-needfix",
            "size_bytes": preview["pinned_predecessor_bytes"],
            "pinned_by": ["needfix-NF-2026-00998"],
        }
    ]


def test_terminal_needfix_request_ledger_orphan_quarantines_and_restores(
    repo_with_worktrees, monkeypatch
) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    request_id = "attempt-terminal-orphan"
    task_id = "needfix-NF-2026-00997"
    entry = _add_worktree(repo, base, request_id)
    _write_request_ledger(repo, base, request_id, task_id)
    _insert_card(repo, task_id, status="blocked", launch_request_id=request_id)
    _insert_needfix(repo, "NF-2026-00997", task_id, status="archived")
    checkout = entry / "worktree"
    gitdir = Path(
        (checkout / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
    )
    shutil.rmtree(gitdir)
    monkeypatch.setattr(
        storage_retention.worktree_storage,
        "_repo_registered_worktrees",
        lambda _repo: {},
    )

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    assert [item["id"] for item in preview["candidates"]] == [request_id]
    assert preview["candidates"][0]["reclaim_authority"] == (
        "terminal_needfix_request_ledger"
    )
    result = storage_retention.quarantine(
        repo,
        preview_digest=preview["preview_digest"],
        confirm=True,
        base=base,
        now=_aged_now(),
    )
    assert result["quarantined"] == 1
    assert not entry.exists()
    restored = storage_retention.restore(
        repo, batch_id=result["batch_id"], confirm=True, base=base
    )
    assert restored["restored"] == 1
    assert entry.is_dir()


def test_nonterminal_request_ledger_orphan_remains_protected(
    repo_with_worktrees, monkeypatch
) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    request_id = "attempt-active-orphan"
    task_id = "needfix-NF-2026-00996"
    entry = _add_worktree(repo, base, request_id)
    _write_request_ledger(repo, base, request_id, task_id)
    _insert_card(repo, task_id, status="blocked", launch_request_id=request_id)
    _insert_needfix(repo, "NF-2026-00996", task_id, status="task_created")
    checkout = entry / "worktree"
    gitdir = Path(
        (checkout / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
    )
    shutil.rmtree(gitdir)
    monkeypatch.setattr(
        storage_retention.worktree_storage,
        "_repo_registered_worktrees",
        lambda _repo: {},
    )

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    assert preview["candidate_count"] == 0
    assert {item["id"]: item["reason"] for item in preview["protected"]}[
        request_id
    ] == "orphaned"


def test_archived_task_request_ledger_orphan_quarantines_and_restores(
    repo_with_worktrees, monkeypatch
) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    request_id = "attempt-archived-task-orphan"
    task_id = "TASK-ARCHIVED"
    entry = _add_worktree(repo, base, request_id)
    _write_request_ledger(repo, base, request_id, task_id)
    _insert_card(repo, task_id, status="blocked", launch_request_id=request_id)
    conn = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        conn.execute(
            "UPDATE tasks SET archived_at=? WHERE task_id=?",
            (datetime.now(timezone.utc).isoformat(), task_id),
        )
        conn.commit()
    finally:
        conn.close()
    checkout = entry / "worktree"
    gitdir = Path(
        (checkout / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
    )
    shutil.rmtree(gitdir)
    monkeypatch.setattr(
        storage_retention.worktree_storage,
        "_repo_registered_worktrees",
        lambda _repo: {},
    )

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    assert [item["id"] for item in preview["candidates"]] == [request_id]
    assert preview["candidates"][0]["reclaim_authority"] == (
        "terminal_task_request_ledger"
    )
    result = storage_retention.quarantine(
        repo,
        preview_digest=preview["preview_digest"],
        confirm=True,
        base=base,
        now=_aged_now(),
    )
    assert result["quarantined"] == 1
    assert not entry.exists()
    restored = storage_retention.restore(
        repo, batch_id=result["batch_id"], confirm=True, base=base
    )
    assert restored["restored"] == 1
    assert entry.is_dir()


def test_reclaim_leaves_no_launchable_card_without_its_predecessor(
    repo_with_worktrees,
) -> None:
    """Invariant over the canonical task table (not one hand-picked card):
    after reclaiming the eligible set, no pending/processing/blocked card is
    left pointing at a reclaimed predecessor worktree."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    _add_worktree(repo, base, "pred-pending")
    _add_worktree(repo, base, "pred-processing")
    _add_worktree(repo, base, "pred-blocked")
    _add_worktree(repo, base, "live-processing")
    superseded = _add_worktree(repo, base, "old-superseded")

    _insert_card(repo, "NF-pending", status="pending", rework_predecessor_request_id="pred-pending")
    _insert_card(
        repo,
        "NF-processing",
        status="processing",
        launch_request_id="live-processing",
        rework_predecessor_request_id="pred-processing",
    )
    _insert_card(repo, "NF-blocked", status="blocked", rework_predecessor_request_id="pred-blocked")
    # A genuinely superseded lineage: its successor was ACCEPTED and the card is
    # finished. Its old attempt must remain reclaimable (0.9.72 behaviour).
    _insert_card(repo, "DONE", status="finished", accepted_request_id="old-superseded")

    preview = storage_retention.preview(repo, base=base, now=_aged_now())
    candidate_ids = {item["id"] for item in preview["candidates"]}

    # The invariant: no live card's pinned predecessor is a reclaim candidate.
    assert _nonfinished_predecessors(repo).isdisjoint(candidate_ids)
    # ...and the live worker attempt itself is likewise protected.
    assert "live-processing" not in candidate_ids
    # ...while genuinely superseded lineage is still reclaimed (0.9.72 preserved).
    assert "old-superseded" in candidate_ids
    assert superseded.is_dir()  # preview is side-effect free


def test_ro_lineage_connection_encodes_hash_and_creates_no_file(
    tmp_path, monkeypatch
) -> None:
    """storage_retention.py's read-only lineage connect must percent-encode a
    '#' in the database path so ``?mode=ro`` is never dropped: no file is
    created and the read fails closed (verified=False)."""
    hashed = tmp_path / "db#frag.db"
    truncated = tmp_path / "db"  # what the buggy fragment-dropping URI created
    monkeypatch.setattr(task_store, "canonical_db_path", lambda _root: hashed)

    protected, verified, pinned_by, terminal_tasks = (
        storage_retention._protected_attempt_ids(tmp_path)
    )

    assert protected == {}
    assert pinned_by == {}
    assert terminal_tasks == set()
    assert verified is False  # unreadable lineage fails closed
    assert not truncated.exists()  # no read-write create at the truncated path
    assert not hashed.exists()  # mode=ro on a missing file creates nothing


def test_preview_is_bounded_on_150_worktrees(repo_with_worktrees, monkeypatch) -> None:
    """The preview returns within its deadline even when the footprint walk
    stalls, and reports an incomplete result rather than a truncated footprint.

    150 registered worktree entries stand in for the Windows repository whose
    unbounded per-file walk ran to the full 1800s request timeout. The bound is
    asserted directly (wall clock) rather than by observation: the measurement
    is forced to block so the assertion cannot pass by the scan merely being
    fast on this machine."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    for index in range(150):
        entry = base / f"wt-{index:03d}"
        (entry / "worktree").mkdir(parents=True)

    measurement_started = threading.Event()
    release = threading.Event()

    def _stalled_footprint(*_args, **_kwargs):
        measurement_started.set()
        release.wait(30.0)  # never released within the test's deadline
        return {}

    monkeypatch.setattr(storage_retention, "repo_storage_footprint", _stalled_footprint)

    started = time.monotonic()
    result = storage_retention.preview(repo, base=base, deadline_seconds=0.5)
    elapsed = time.monotonic() - started

    try:
        assert measurement_started.wait(5.0)  # the measurement genuinely ran
        assert elapsed < 10.0  # bounded well under the stalled 30s measurement
        assert result["complete"] is False
        assert result["incomplete"] is True
        assert result["incomplete_reason"] == "measurement_deadline_exceeded"
        assert "worktree_footprint_scan" in result["unmeasured"]
        # A partial scan must never publish a smaller footprint as if complete.
        assert result["current_bytes"] is None
        assert result["candidate_count"] is None
        assert result["candidates"] == []
        assert result["preview_digest"] == ""
    finally:
        # Release in finally so a failed assertion never leaves the background
        # measurement thread blocked on the 30s wait.
        release.set()


def test_preview_reports_complete_when_measurement_finishes(repo_with_worktrees) -> None:
    """The bounded wrapper is not always-incomplete: a normal repository still
    produces a complete, digest-bearing preview."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    _add_worktree(repo, base, "attempt-1")
    _insert_card(repo, "DONE", status="finished", accepted_request_id="attempt-2")

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    assert preview["complete"] is True
    assert not preview.get("incomplete")
    assert isinstance(preview["current_bytes"], int)
    assert preview["preview_digest"]
    assert preview["candidate_count"] == 1


# --- Rework findings: the bound must hold at every entry point, not only where
# --- it was first reported (preview). These cover quarantine, deadline
# --- validation, non-stacking single-flight, error re-raise, and schema shape.


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 0.0, -1.0])
def test_preview_rejects_nonfinite_or_nonpositive_deadline(repo_with_worktrees, bad) -> None:
    """A wall-clock bound an argument can switch off is not a bound.

    ``deadline_seconds=float('inf')`` would make the measurement wait block
    forever; NaN or a non-positive value degenerates the ceiling. Each is
    rejected rather than clamped."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    with pytest.raises(storage_retention.StorageRetentionError, match="retention_deadline_invalid"):
        storage_retention.preview(repo, base=base, deadline_seconds=bad)


def test_quarantine_is_bounded_and_refuses_incomplete_measurement(
    repo_with_worktrees, monkeypatch
) -> None:
    """``quarantine`` re-runs the footprint measurement to reconfirm the digest;
    it must share the preview's bound so the dashboard quarantine action can
    never hang on the identical slow worktree walk. A measurement that cannot
    finish in time makes the write refuse (never block, never proceed on an
    unverified footprint)."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    started = threading.Event()
    release = threading.Event()

    def _stalled_footprint(*_args, **_kwargs):
        started.set()
        release.wait(30.0)
        return {}

    monkeypatch.setattr(storage_retention, "repo_storage_footprint", _stalled_footprint)
    monkeypatch.setattr(storage_retention, "PREVIEW_DEADLINE_SECONDS", 0.5)

    t0 = time.monotonic()
    try:
        with pytest.raises(
            storage_retention.StorageRetentionError, match="retention_measurement_incomplete"
        ):
            storage_retention.quarantine(
                repo, preview_digest="anything", confirm=True, base=base
            )
        elapsed = time.monotonic() - t0
        assert started.wait(5.0)  # the measurement genuinely ran
        assert elapsed < 10.0  # bounded well under the stalled 30s walk
    finally:
        release.set()


def test_repeated_timed_out_previews_share_one_measurement(
    repo_with_worktrees, monkeypatch
) -> None:
    """Repeated/concurrent timed-out previews must not stack filesystem walks.

    Six previews issued while the walk is stalled attach to ONE single-flight
    measurement instead of each spawning its own thread and SQLite connection,
    so a stalled measurement can never be amplified into stacked disk I/O."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    release = threading.Event()
    calls_lock = threading.Lock()
    calls: list[int] = []

    def _stalled_footprint(*_args, **_kwargs):
        with calls_lock:
            calls.append(1)
        release.wait(30.0)
        return {}

    monkeypatch.setattr(storage_retention, "repo_storage_footprint", _stalled_footprint)

    results: list[dict] = []
    results_lock = threading.Lock()

    def _run() -> None:
        result = storage_retention.preview(repo, base=base, deadline_seconds=0.5)
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=_run) for _ in range(6)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10.0)
        assert len(results) == 6
        assert all(item["complete"] is False for item in results)
        with calls_lock:
            assert len(calls) == 1  # six previews, exactly one worktree walk
    finally:
        release.set()


def test_preview_reraises_measurement_error_not_partial_success(
    repo_with_worktrees, monkeypatch
) -> None:
    """A genuine measurement failure must never be presented as an incomplete
    ``ok: True`` partial success: the exception is re-raised on the caller."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]

    def _boom(*_args, **_kwargs):
        raise storage_retention.StorageRetentionError("measurement_boom")

    monkeypatch.setattr(storage_retention, "repo_storage_footprint", _boom)

    with pytest.raises(storage_retention.StorageRetentionError, match="measurement_boom"):
        storage_retention.preview(repo, base=base, now=_aged_now())


def test_incomplete_preview_keeps_the_complete_payload_schema(
    repo_with_worktrees, monkeypatch
) -> None:
    """The response must not change shape depending on whether the deadline was
    hit: every key the complete payload emits is also present when incomplete,
    with walk-dependent fields explicitly ``None`` and the cheap policy fields
    carrying their real values."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    _add_worktree(repo, base, "attempt-1")

    complete = storage_retention.preview(repo, base=base, now=_aged_now())
    assert complete["complete"] is True

    release = threading.Event()

    def _stalled_footprint(*_args, **_kwargs):
        release.wait(30.0)
        return {}

    monkeypatch.setattr(storage_retention, "repo_storage_footprint", _stalled_footprint)
    try:
        incomplete = storage_retention.preview(repo, base=base, deadline_seconds=0.5)
    finally:
        release.set()

    assert incomplete["complete"] is False
    # Same schema: no key the complete payload publishes is ever absent here.
    assert set(complete).issubset(set(incomplete))
    for key in ("policy_days", "max_bytes", "footprint", "registration_health"):
        assert key in incomplete
    assert isinstance(incomplete["policy_days"], int)
    assert isinstance(incomplete["max_bytes"], int)
    assert incomplete["footprint"] is None
    assert incomplete["registration_health"] is None
    assert "footprint" in incomplete["unmeasured"]
    assert "registration_health" in incomplete["unmeasured"]
    # Every field withheld because the walk did not finish is NAMED in
    # ``unmeasured`` -- not only footprint/registration_health but each measured
    # byte and candidate count, matching _incomplete_preview's docstring.
    for field in (
        "current_bytes",
        "worktree_current_bytes",
        "projected_bytes",
        "candidate_count",
        "candidate_bytes",
        "protected_count",
        "candidates",
        "protected",
        "pinned_predecessor_bytes",
        "pinned_predecessors",
        "preview_digest",
    ):
        assert field in incomplete["unmeasured"]
    # Pinned-predecessor accounting depends on the same unfinished walk: it must
    # be withheld, never published as a (smaller) zero when incomplete.
    assert incomplete["pinned_predecessor_bytes"] is None
    assert incomplete["pinned_predecessors"] == []


# --- Finding 3: the pinned-predecessor standoff must be VISIBLE and BOUNDED --
# --- reported as its own preview line naming the held bytes and the cards. ----


def test_preview_reports_pinned_predecessor_bytes_and_holding_cards(
    repo_with_worktrees,
) -> None:
    """The bytes held off-limits by in-flight rework lineage, and exactly which
    cards hold them, are a distinct preview line an operator can read directly
    -- so an unbounded pin can be seen and escalated, not silently absorbed."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    entry = _add_worktree(repo, base, "attempt-1")
    # Two non-finished cards both pin the same predecessor attempt.
    _insert_card(repo, "NF-1", status="pending", rework_predecessor_request_id="attempt-1")
    _insert_card(repo, "NF-2", status="blocked", rework_predecessor_request_id="attempt-1")

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    pinned = preview["pinned_predecessors"]
    assert len(pinned) == 1
    assert pinned[0]["id"] == "attempt-1"
    assert pinned[0]["size_bytes"] > 0
    assert pinned[0]["pinned_by"] == ["NF-1", "NF-2"]  # sorted, both holders named
    assert preview["pinned_predecessor_bytes"] == pinned[0]["size_bytes"]
    # Still protected, never a candidate, never mutated.
    assert "attempt-1" not in {item["id"] for item in preview["candidates"]}
    assert {"id": "attempt-1", "reason": "rework_predecessor_retained"} in preview["protected"]
    assert entry.is_dir()


def test_finished_lineage_reports_no_pinned_predecessor_bytes(
    repo_with_worktrees,
) -> None:
    """Once the naming card is finished the pin lifts: the lineage becomes a
    reclaim candidate and contributes zero pinned bytes (0.9.72 preserved)."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    _add_worktree(repo, base, "attempt-1")
    _insert_card(repo, "DONE", status="finished", accepted_request_id="attempt-2")

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    assert preview["pinned_predecessor_bytes"] == 0
    assert preview["pinned_predecessors"] == []
    assert "attempt-1" in {item["id"] for item in preview["candidates"]}


# --- Finding 1: a finite-but-absurd deadline must be rejected, not crash the --
# --- wait with OverflowError. --------------------------------------------------


def test_preview_rejects_absurd_but_finite_deadline(repo_with_worktrees) -> None:
    """``1e300`` is finite and passes ``math.isfinite``, but overflows the C
    timeout ``threading.Event.wait`` derives from it. It must be rejected as an
    invalid bound, never allowed through to crash the wait."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    with pytest.raises(
        storage_retention.StorageRetentionError, match="retention_deadline_invalid"
    ):
        storage_retention.preview(repo, base=base, deadline_seconds=1e300)


def test_resolve_deadline_bounds_the_accepted_range() -> None:
    """The ceiling itself is accepted; anything above it -- including a value a
    hair over -- is rejected rather than clamped or passed to the wait."""
    assert (
        storage_retention._resolve_deadline(storage_retention.MAX_DEADLINE_SECONDS)
        == storage_retention.MAX_DEADLINE_SECONDS
    )
    with pytest.raises(
        storage_retention.StorageRetentionError, match="retention_deadline_invalid"
    ):
        storage_retention._resolve_deadline(storage_retention.MAX_DEADLINE_SECONDS + 1.0)


# --- Finding 2: the measurement worker must not swallow control-flow ----------
# --- exceptions; only ordinary errors are captured for re-raise. --------------


def test_measure_worker_propagates_control_flow_exceptions() -> None:
    """SystemExit/KeyboardInterrupt/GeneratorExit derive from BaseException, not
    Exception, and must propagate out of the worker rather than being captured
    and later mis-reported as a measurement failure."""
    measurement = storage_retention._Measurement()

    def _control_flow():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        storage_retention._measure_worker("k-cf", measurement, _control_flow)
    assert measurement.error is None  # never captured
    assert measurement.done.is_set()  # cleanup still ran in ``finally``


def test_measure_worker_captures_ordinary_exception() -> None:
    """An ordinary error IS captured for the caller to re-raise as a genuine
    failure (never presented as an incomplete ``ok: True`` success)."""
    measurement = storage_retention._Measurement()

    def _boom():
        raise storage_retention.StorageRetentionError("measurement_boom")

    storage_retention._measure_worker("k-err", measurement, _boom)
    assert isinstance(measurement.error, storage_retention.StorageRetentionError)
    assert measurement.done.is_set()


# --- Final round: ONE validator gates every externally-supplied numeric input -
# --- (deadline and now alike); each rejected class asserted once. -------------


def test_require_finite_rejects_each_invalid_class() -> None:
    """The single numeric gate rejects every invalid class with the module
    domain error -- never leaking a raw ``ValueError``/``OverflowError``. One
    assertion per class: non-numeric, unconvertible, NaN, infinite, out-of-range.
    """
    err = storage_retention.StorageRetentionError
    with pytest.raises(err, match="bad_input"):  # non-numeric -> ValueError
        storage_retention._require_finite("not-a-number", "bad_input")
    with pytest.raises(err, match="bad_input"):  # unconvertible int -> OverflowError
        storage_retention._require_finite(10**400, "bad_input")
    with pytest.raises(err, match="bad_input"):  # NaN
        storage_retention._require_finite(float("nan"), "bad_input")
    with pytest.raises(err, match="bad_input"):  # infinite
        storage_retention._require_finite(float("inf"), "bad_input")
    with pytest.raises(err, match="bad_input"):  # out of range (above maximum)
        storage_retention._require_finite(2.0, "bad_input", maximum=1.0)
    # A finite, in-range value is returned as a float unchanged in magnitude.
    assert storage_retention._require_finite(3, "bad_input") == 3.0


@pytest.mark.parametrize("bad_now", ["nope", float("nan"), float("inf"), 10**400])
def test_preview_rejects_invalid_now_through_the_same_validator(
    repo_with_worktrees, bad_now
) -> None:
    """A caller-supplied ``now`` passes through the same gate as the deadline.
    A NaN in particular would compare unequal to itself in the single-flight key
    and let concurrent callers each start their own footprint walk -- the exact
    availability failure this card exists to prevent -- so every invalid class is
    rejected with the module domain error rather than silently accepted."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    with pytest.raises(
        storage_retention.StorageRetentionError, match="retention_now_invalid"
    ):
        storage_retention.preview(repo, base=base, now=bad_now)


def test_plan_worktree_reclaim_rejects_invalid_now(repo_with_worktrees) -> None:
    """The planner is a public entry point too: a NaN ``now`` there would make
    every age comparison silently False and keep everything, so it is rejected at
    the boundary through the same validator rather than deeper in the module."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    scan = {"base": base, "worktrees": []}
    with pytest.raises(
        storage_retention.StorageRetentionError, match="retention_now_invalid"
    ):
        storage_retention.plan_worktree_reclaim(
            repo, scan, min_age_days=30, max_bytes=1, current_bytes=0, now=float("nan")
        )


# --- Rework round 2, finding 1: a failure must never be representable as a -----
# --- success by ANY path. A BaseException tears the worker down leaving --------
# --- ``error`` None; success is keyed on an explicit flag, not error-is-None. --


def test_preview_reraises_baseexception_never_partial_success(
    repo_with_worktrees, monkeypatch
) -> None:
    """A control-flow BaseException from the measurement (NOT an ``Exception``
    subclass) propagates out of the worker leaving ``error`` None while the
    finally still sets ``done``. Keyed on ``error is None`` this would surface as
    a ``(None, True)`` success and crash ``preview`` with an ``AttributeError``.
    Keyed on the explicit success flag it is raised as a measurement failure."""

    class _Fatal(BaseException):
        pass

    def _boom(*_args, **_kwargs):
        raise _Fatal("fatal measurement teardown")

    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    monkeypatch.setattr(storage_retention, "repo_storage_footprint", _boom)

    with pytest.raises(
        storage_retention.StorageRetentionError, match="retention_measurement_failed"
    ):
        storage_retention.preview(repo, base=base, now=_aged_now())


def test_measure_worker_leaves_succeeded_false_on_baseexception() -> None:
    """At the construction site: a BaseException in the worker must NOT leave a
    measurement that looks both complete and successful. ``done`` is set (finally
    ran) and ``error`` is None (control-flow exceptions are not captured), so the
    only thing separating this failure from a success is ``succeeded`` staying
    False -- which is exactly what the caller keys on."""
    measurement = storage_retention._Measurement()
    assert measurement.succeeded is False

    def _control_flow():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        storage_retention._measure_worker("k-cf2", measurement, _control_flow)
    assert measurement.done.is_set()
    assert measurement.error is None
    assert measurement.succeeded is False  # never representable as success


# --- Rework round 2, finding 2: the single-flight key is set BEFORE eviction, --
# --- so a caller in the finish window attaches instead of walking again. -------


def test_second_caller_in_finish_window_does_not_duplicate_walk(monkeypatch) -> None:
    """Drive two callers through the completion/eviction window.

    The worker sets the completion event, THEN evicts the single-flight key. A
    second caller that arrives in that window still finds the entry (done already
    set) and attaches to the finished result instead of launching a duplicate
    filesystem walk. Evicting first (the pre-fix order) would drop the entry
    before ``done`` was set, and this same second caller would start a second
    walk -- the availability failure this card exists to prevent, via a race.

    The window is forced deterministically by hooking the completion event so a
    second caller runs the instant ``done`` is set, and the assertion is gated on
    that second caller finishing so it can never race past the duplicate."""
    key = ("finish-window-root", "finish-window-base", None)
    monkeypatch.setattr(storage_retention, "_measurements", {})

    calls_lock = threading.Lock()
    calls: list[int] = []

    def fn():
        with calls_lock:
            calls.append(1)
        return {"preview_digest": "d"}

    triggered = threading.Event()
    second_done = threading.Event()
    second_result: dict[str, object] = {}
    base_event_cls = threading.Event

    class _HookedEvent:
        def __init__(self) -> None:
            self._ev = base_event_cls()

        def set(self) -> None:
            self._ev.set()
            if not triggered.is_set():
                triggered.set()
                try:
                    value, complete = storage_retention._measure_within_deadline(
                        key, fn, 5.0
                    )
                    second_result["value"] = value
                    second_result["complete"] = complete
                finally:
                    second_done.set()

        def wait(self, timeout=None):
            return self._ev.wait(timeout)

        def is_set(self):
            return self._ev.is_set()

    constructed: list[int] = []

    class _HookedMeasurement:
        def __init__(self) -> None:
            # One measurement per single-flight walk. Counting constructions proves
            # the attaching caller did NOT build its own: it read the shared sink
            # off the measurement the starter created.
            constructed.append(1)
            self.done = _HookedEvent()
            self.value = None
            self.error = None
            self.succeeded = False
            # The shared partial-evidence sink _measure_within_deadline records on
            # the measurement and every caller (starter and attacher) reads back.
            # Absent it, the attaching caller would fall back to its own empty sink
            # -- the exact ``candidates=[]`` regression round two closed.
            self.progress = None

    monkeypatch.setattr(storage_retention, "_Measurement", _HookedMeasurement)

    value, complete = storage_retention._measure_within_deadline(key, fn, 5.0)
    assert complete is True
    assert value == {"preview_digest": "d"}
    assert second_done.wait(5.0)  # gate the assertion on the second caller
    with calls_lock:
        assert calls == [1]  # attached to the finished walk; never a second one
    assert second_result["complete"] is True
    # The attaching caller sees the SAME result as the starter: it read the shared
    # measurement's sink rather than starting its own walk. Exactly ONE measurement
    # (hence one sink, one walk) was constructed across both callers.
    assert second_result["value"] == {"preview_digest": "d"}
    assert constructed == [1]


# --- Rework round 2, finding 3: a worktree pinned for two reasons at once is ----
# --- counted under BOTH -- the pinned-predecessor bytes never drop the overlap. -


def test_worktree_pinned_as_live_worker_and_predecessor_counts_under_both(
    repo_with_worktrees,
) -> None:
    """A worktree that is simultaneously the live worker of one card AND the
    rework predecessor of another is protected (its displayed reason reads
    ``live_worker``) yet must still be counted in ``pinned_predecessors`` and
    ``pinned_predecessor_bytes``. Keying that accounting on the single displayed
    reason silently dropped exactly this overlap; a worktree can be pinned for two
    reasons at once and must be counted under both."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    entry = _add_worktree(repo, base, "dual")
    _insert_card(repo, "LIVE", status="processing", launch_request_id="dual")
    _insert_card(repo, "REWORK", status="pending", rework_predecessor_request_id="dual")

    preview = storage_retention.preview(repo, base=base, now=_aged_now())

    # Protected, with the live-worker reason visible to the operator...
    assert {"id": "dual", "reason": "live_worker"} in preview["protected"]
    # ...and STILL counted under the pinned-predecessor accounting.
    pinned = {item["id"]: item for item in preview["pinned_predecessors"]}
    assert "dual" in pinned
    assert pinned["dual"]["pinned_by"] == ["REWORK"]
    assert pinned["dual"]["size_bytes"] > 0
    assert preview["pinned_predecessor_bytes"] == pinned["dual"]["size_bytes"]
    # Never a candidate, never mutated.
    assert "dual" not in {item["id"] for item in preview["candidates"]}
    assert entry.is_dir()


# --- Rework round 2, observation: ``ok`` and ``complete`` must agree so a -------
# --- consumer keying on ``ok`` never reads a partial measurement as whole. ------


def test_preview_ok_agrees_with_complete(repo_with_worktrees, monkeypatch) -> None:
    """A complete preview is ``ok=True/complete=True``; an incomplete one is
    ``ok=False/complete=False``. The two never disagree, so a consumer keying on
    ``ok`` alone cannot read a truncated footprint as a whole one."""
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    _add_worktree(repo, base, "attempt-1")

    complete = storage_retention.preview(repo, base=base, now=_aged_now())
    assert complete["complete"] is True
    assert complete["ok"] is True
    assert complete["ok"] == complete["complete"]

    release = threading.Event()

    def _stalled_footprint(*_args, **_kwargs):
        release.wait(30.0)
        return {}

    monkeypatch.setattr(storage_retention, "repo_storage_footprint", _stalled_footprint)
    try:
        incomplete = storage_retention.preview(repo, base=base, deadline_seconds=0.5)
    finally:
        release.set()

    assert incomplete["complete"] is False
    assert incomplete["ok"] is False
    assert incomplete["ok"] == incomplete["complete"]
    assert incomplete["incomplete"] is True  # still distinguishable from a hard error


def _write_idle_process_identity(repo: Path, request_id: str) -> None:
    from aiworkhub import process_event_ledger, process_launcher

    log_path = repo / process_launcher.PROCESS_LOG_DEFAULT_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process_event_ledger.append_event(
        log_path,
        {
            "pid": 1_000_000_001,
            "pid_start_ticks": 1,
            "request_id": request_id,
            "timestamp": "2026-01-01T00:00:00+00:00",
        },
    )


def _accepted_cleanup_evidence(
    task_id: str, request_id: str, predecessor_request_id: str = ""
) -> dict[str, str]:
    from aiworkhub import task_retention

    predecessor = str(predecessor_request_id or "").strip()
    return {
        "schema_id": task_retention.ACCEPTED_CLEANUP_EVIDENCE_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "predecessor_request_id": predecessor,
        "canonical_digest": task_retention.canonical_acceptance_digest(
            accept_evidence={},
            accepted_request_id=request_id,
            predecessor_request_id=predecessor,
            request_id=request_id,
            status="finished",
            task_id=task_id,
        ),
    }


def test_cleanup_fail_closed_live_process_active_rework_and_symlink(repo_with_worktrees) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    live = _add_worktree(repo, base, "live-attempt")
    pinned = _add_worktree(repo, base, "pinned-pred")
    accepted = _add_worktree(repo, base, "accepted-succ")
    _write_idle_process_identity(repo, "live-attempt")
    _write_idle_process_identity(repo, "accepted-succ")
    _insert_card(repo, "TASK-LIVE", status="finished", accepted_request_id="live-attempt")
    _insert_card(repo, "TASK-LIVE-HOLDER", status="processing", launch_request_id="live-attempt")
    _insert_card(
        repo,
        "TASK-ACCEPTED",
        status="finished",
        accepted_request_id="accepted-succ",
    )
    _insert_card(
        repo,
        "TASK-REWORK",
        status="pending",
        rework_predecessor_request_id="accepted-succ",
    )
    live_result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_cleanup_evidence("TASK-LIVE", "live-attempt"),
        base=base,
    )
    rework_result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_cleanup_evidence("TASK-ACCEPTED", "accepted-succ"),
        base=base,
    )
    assert live_result["ok"] is False
    assert live_result["reason"] == "live_process"
    assert live_result["deleted"] is False
    assert live.is_dir()
    assert (live / "worktree").is_dir()
    assert rework_result["ok"] is False
    assert rework_result["reason"] == "active_rework"
    assert rework_result["deleted"] is False
    assert (accepted / "worktree").is_dir()
    assert (pinned / "worktree").is_dir()

    alias = base / "accepted-succ-alias"
    alias.symlink_to(accepted)
    _write_idle_process_identity(repo, "accepted-succ-alias")
    _insert_card(
        repo,
        "TASK-ALIAS",
        status="finished",
        accepted_request_id="accepted-succ-alias",
    )
    ambiguous = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_cleanup_evidence("TASK-ALIAS", "accepted-succ-alias"),
        base=base,
    )
    assert ambiguous["ok"] is False
    assert ambiguous["reason"] == "ambiguous_ownership"
    assert ambiguous["deleted"] is False
    assert accepted.is_dir()


def test_cleanup_fail_closed_claimed_supervisor_holder(repo_with_worktrees) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    accepted = _add_worktree(repo, base, "accepted-held")
    _write_idle_process_identity(repo, "accepted-held")
    _insert_card(
        repo,
        "TASK-ACCEPTED-HELD",
        status="finished",
        accepted_request_id="accepted-held",
    )
    db_path = task_store.canonical_db_path(repo)
    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "TASK-SUPERVISOR",
                "claude",
                "storage",
                "pending",
                "claimed",
                json.dumps({"launch_request_id": "accepted-held"}),
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_cleanup_evidence("TASK-ACCEPTED-HELD", "accepted-held"),
        base=base,
    )
    assert result["ok"] is False
    assert result["reason"] == "live_process"
    assert result["deleted"] is False
    assert (accepted / "worktree").is_dir()


def test_cleanup_fail_closed_finished_task_live_exact_process(repo_with_worktrees) -> None:
    import os

    from aiworkhub import process_event_ledger, process_launcher, runtime_temp

    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    accepted = _add_worktree(repo, base, "finished-live")
    pid = os.getpid()
    ticks = runtime_temp.process_start_ticks(pid)
    assert ticks is not None
    now = datetime.now(timezone.utc).isoformat()
    log_path = repo / process_launcher.PROCESS_LOG_DEFAULT_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = log_path.parent / "finished-live.metadata.json"
    supervisor_path = log_path.parent / "finished-live.supervisor.json"
    metadata_path.write_text(
        json.dumps({"provider_pid": pid, "provider_pid_start_ticks": ticks}),
        encoding="utf-8",
    )
    supervisor_path.write_text(
        json.dumps({"child_pid": pid, "child_pid_start_ticks": ticks}),
        encoding="utf-8",
    )
    supervisor_path.chmod(0o600)
    process_event_ledger.append_event(
        log_path,
        {
            "metadata_path": str(metadata_path),
            "pid": pid,
            "pid_start_ticks": ticks,
            "request_id": "finished-live",
            "supervisor_status_path": str(supervisor_path),
            "task_id": "TASK-FINISHED-LIVE",
            "timestamp": now,
        },
    )
    connection = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        connection.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "TASK-FINISHED-LIVE",
                "claude",
                "storage",
                "finished",
                "unclaimed",
                json.dumps({"accepted_request_id": "finished-live"}),
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_cleanup_evidence("TASK-FINISHED-LIVE", "finished-live"),
        base=base,
    )
    assert result["ok"] is False
    assert result["reason"] == "live_process"
    assert result["deleted"] is False
    assert accepted.is_dir()
    assert (accepted / "worktree").is_dir()


def _write_production_process_authority(
    repo: Path,
    request_id: str,
    task_id: str,
    *,
    supervisor: dict[str, object] | str,
) -> None:
    import os

    from aiworkhub import process_event_ledger, process_launcher, runtime_temp

    pid = os.getpid()
    ticks = runtime_temp.process_start_ticks(pid)
    assert ticks is not None
    now = datetime.now(timezone.utc).isoformat()
    log_path = repo / process_launcher.PROCESS_LOG_DEFAULT_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = log_path.parent / f"{request_id}.metadata.json"
    supervisor_path = log_path.parent / f"{request_id}.supervisor.json"
    metadata_path.write_text(
        json.dumps({"provider_pid": pid, "provider_pid_start_ticks": ticks}),
        encoding="utf-8",
    )
    if isinstance(supervisor, str):
        supervisor_path.write_text(supervisor, encoding="utf-8")
    else:
        supervisor_path.write_text(json.dumps(supervisor), encoding="utf-8")
        supervisor_path.chmod(0o600)
    process_event_ledger.append_event(
        log_path,
        {
            "metadata_path": str(metadata_path),
            "pid": pid,
            "pid_start_ticks": ticks,
            "request_id": request_id,
            "supervisor_status_path": str(supervisor_path),
            "task_id": task_id,
            "timestamp": now,
        },
    )


def test_cleanup_fail_closed_predecessor_live_process_identity(
    repo_with_worktrees,
) -> None:
    import os

    from aiworkhub import runtime_temp

    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    pred = _add_worktree(repo, base, "pred-live")
    succ = _add_worktree(repo, base, "succ-accepted")
    (pred / "manifest.json").write_text("{}", encoding="utf-8")
    (succ / "manifest.json").write_text("{}", encoding="utf-8")
    pid = os.getpid()
    ticks = runtime_temp.process_start_ticks(pid)
    assert ticks is not None
    _write_production_process_authority(
        repo,
        "pred-live",
        "TASK-PRED-LIVE",
        supervisor={"child_pid": pid, "child_pid_start_ticks": ticks},
    )
    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        connection.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "TASK-SUCC-LIVE-PRED",
                "claude",
                "storage",
                "finished",
                "unclaimed",
                json.dumps(
                    {
                        "accepted_request_id": "succ-accepted",
                        "rework_predecessor": {"request_id": "pred-live"},
                    }
                ),
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    _write_idle_process_identity(repo, "succ-accepted")

    result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_cleanup_evidence("TASK-SUCC-LIVE-PRED", "succ-accepted", "pred-live"),
        base=base,
    )
    assert result["ok"] is False
    assert result["reason"] == "live_process"
    assert result["deleted"] is False
    assert result["predecessor_unpinned"] == ""
    assert (pred / "worktree").is_dir()
    connection = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        stored = json.loads(
            connection.execute(
                "SELECT card_json FROM tasks WHERE task_id=?",
                ("TASK-SUCC-LIVE-PRED",),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert stored["rework_predecessor"]["request_id"] == "pred-live"


def test_cleanup_fail_closed_predecessor_unverified_process_identity(
    repo_with_worktrees,
) -> None:
    repo, base = repo_with_worktrees["repo"], repo_with_worktrees["base"]
    pred = _add_worktree(repo, base, "pred-unverified")
    succ = _add_worktree(repo, base, "succ-unverified")
    (pred / "manifest.json").write_text("{}", encoding="utf-8")
    (succ / "manifest.json").write_text("{}", encoding="utf-8")
    _write_production_process_authority(
        repo,
        "pred-unverified",
        "TASK-PRED-UNVERIFIED",
        supervisor="not-a-mapping",
    )
    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        connection.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "TASK-SUCC-UNVERIFIED-PRED",
                "claude",
                "storage",
                "finished",
                "unclaimed",
                json.dumps(
                    {
                        "accepted_request_id": "succ-unverified",
                        "rework_predecessor": {"request_id": "pred-unverified"},
                    }
                ),
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    _write_idle_process_identity(repo, "succ-unverified")

    result = storage_retention.cleanup_accepted_artifacts(
        repo,
        evidence=_accepted_cleanup_evidence(
            "TASK-SUCC-UNVERIFIED-PRED", "succ-unverified", "pred-unverified"
        ),
        base=base,
    )
    assert result["ok"] is False
    assert result["reason"] == "ambiguous_ownership"
    assert result["deleted"] is False
    assert result["predecessor_unpinned"] == ""
    assert (pred / "worktree").is_dir()
