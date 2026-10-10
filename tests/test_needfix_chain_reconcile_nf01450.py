"""Acceptance tests for NF-2026-01450: NeedFix read-time reconcile closes
orphaned conversions, and a one-shot script backfills commits the bounded
read-time trailer reconcile can never reach.

Covers the task's four required scenarios:
1. A task_created NeedFix whose converted_task_id names a missing card
   resolves when a verifiable accepted learning-commit judgement exists for
   that (vanished) task id.
2. The same missing-card NeedFix, with no such evidence, returns to accepted
   with reason ``linked_card_missing`` instead of staying stranded.
3. A task_created NeedFix whose linked card is archived -- closed without
   ever running the accepted-task closure -- returns to accepted with reason
   ``linked_card_closed_unaccepted``.
4. scripts/needfix_commit_backfill.py previews subject-pattern and trailer
   ids from the full HEAD history, reports a "part" subject as skipped, and
   --confirm resolves only the non-skipped ids.

Also guards the scope boundary of (3): a directly-created merge/multi-finding
card legitimately closes its NeedFix records while canonical status
"finished" (see test_needfix_auto_link.py's explicit-reference-link tests);
that existing, intended behaviour must not regress.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import learning_commit_store, needfix_ingest, needfix_store, task_store

_BACKFILL_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "needfix_commit_backfill.py"
)


def _load_backfill():
    spec = importlib.util.spec_from_file_location(
        "needfix_commit_backfill", _BACKFILL_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill = _load_backfill()


@pytest.fixture
def repo_root() -> Path:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def init_store(repo_root: Path) -> Path:
    result = needfix_store.initialize_repository(repo_root)
    assert result["initialized"] is True
    return repo_root


def _make_task_created(repo_root: Path, *, title: str) -> dict[str, Any]:
    record = needfix_store.add_needfix(
        repo_root, title=title, description="d", status="accepted",
    )
    needfix_store.convert_needfix(
        repo_root, record["id"],
        lambda card: {"ok": True, "task_id": card["task_id"]},
    )
    updated = needfix_store.get_needfix(repo_root, record["id"])
    assert updated["status"] == "task_created"
    return updated


def _seed_accepted_learning_commit(repo_root: Path, task_id: str, request_id: str) -> None:
    """Seed an 'accepted' learning-commit judgement for ``task_id`` -- no
    ``tasks`` row is written, so the card stays verifiably missing.
    """
    task_store.initialize_repository(repo_root)
    db = task_store.canonical_db_path(repo_root)
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(learning_commit_store._SCHEMA)
        payload = {"failure_category": None, "edge_candidates": []}
        conn.execute(
            "INSERT OR REPLACE INTO learning_commits(commit_id,idempotency_key,task_id,"
            "request_id,repository_id,repo_area,outcome,payload_json,payload_sha256,"
            "projections_json,state,manager_id,manager_provider,provenance,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{task_id}:{request_id}", f"{task_id}:{request_id}", task_id, request_id,
                "repo", "src/aiworkhub", "accepted", json.dumps(payload), "sha",
                json.dumps({}), "completed", "m", "claude", "test",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# (1) + (2): converted_task_id names a missing card                           #
# --------------------------------------------------------------------------- #

def test_missing_card_with_learning_commit_resolves(init_store: Path):
    record = _make_task_created(init_store, title="orphan-with-evidence")
    task_id = record["converted_task_id"]
    _seed_accepted_learning_commit(init_store, task_id, request_id="req-1")

    report = needfix_store.reconcile_orphaned_conversions(
        init_store, get_task_fn=lambda _tid: None, canonical_status_fn=lambda _c: "",
    )
    assert report["resolved_count"] == 1
    assert report["resolved"][0]["needfix_id"] == record["id"]

    updated = needfix_store.get_needfix(init_store, record["id"])
    assert updated["status"] == "resolved"
    assert any(ref.startswith("learning_commit:") for ref in updated["evidence_refs"])

    events = needfix_store.list_events(init_store, record["id"])
    assert events[0]["event"] == "orphaned_conversion_resolved"
    assert events[0]["detail"]["reason"] == "linked_card_missing"

    # Idempotent: already terminal, a second run resolves/returns nothing more.
    again = needfix_store.reconcile_orphaned_conversions(
        init_store, get_task_fn=lambda _tid: None, canonical_status_fn=lambda _c: "",
    )
    assert again["resolved_count"] == 0
    assert again["returned_to_accepted_count"] == 0


def test_missing_card_without_evidence_returns_to_accepted(init_store: Path):
    record = _make_task_created(init_store, title="orphan-without-evidence")

    report = needfix_store.reconcile_orphaned_conversions(
        init_store, get_task_fn=lambda _tid: None, canonical_status_fn=lambda _c: "",
    )
    assert report["resolved_count"] == 0
    assert report["returned_to_accepted_count"] == 1
    assert report["returned_to_accepted"][0]["reason"] == "linked_card_missing"

    updated = needfix_store.get_needfix(init_store, record["id"])
    assert updated["status"] == "accepted"
    assert updated["converted_task_id"] is None

    events = needfix_store.list_events(init_store, record["id"])
    assert events[0]["event"] == "orphaned_conversion_returned_to_accepted"
    assert events[0]["detail"]["reason"] == "linked_card_missing"

    # Idempotent: no longer task_created/task_planned, so a second run is a no-op.
    again = needfix_store.reconcile_orphaned_conversions(
        init_store, get_task_fn=lambda _tid: None, canonical_status_fn=lambda _c: "",
    )
    assert again["returned_to_accepted_count"] == 0
    assert again["resolved_count"] == 0


def test_the_read_never_raises_on_a_get_task_fn_failure(init_store: Path):
    _make_task_created(init_store, title="get-task-raises")

    def boom(_task_id: str):
        raise RuntimeError("task store unavailable")

    report = needfix_store.reconcile_orphaned_conversions(
        init_store, get_task_fn=boom, canonical_status_fn=lambda _c: "",
    )
    assert report["resolved_count"] == 0
    assert report["returned_to_accepted_count"] == 0
    assert report["skipped_count"] == 1


def test_list_active_reconciles_missing_card_through_the_ingest_read_path(
    init_store: Path,
):
    """Proves the wiring: needfix_ingest.list_active actually invokes the new
    reconcile on its own read path, not just needfix_store in isolation.
    """
    task_store.initialize_repository(init_store)
    record = _make_task_created(init_store, title="wired-through-ingest")

    result = needfix_ingest.list_active(init_store)
    assert result["derived"] is True
    assert any(item["id"] == record["id"] for item in result["items"])

    updated = needfix_store.get_needfix(init_store, record["id"])
    assert updated["status"] == "accepted"
    assert updated["converted_task_id"] is None


# --------------------------------------------------------------------------- #
# (3): linked card closed without ever running the accepted-task closure      #
# --------------------------------------------------------------------------- #

def test_archived_unaccepted_card_returns_to_accepted(init_store: Path):
    record = _make_task_created(init_store, title="archived-no-accept")
    task_id = record["converted_task_id"]
    card = {"task_id": task_id, "status": "archived"}

    report = needfix_store.reconcile_orphaned_conversions(
        init_store,
        get_task_fn=lambda tid: card if tid == task_id else None,
        canonical_status_fn=lambda c: c["status"],
    )
    assert report["returned_to_accepted_count"] == 1
    assert report["returned_to_accepted"][0]["reason"] == "linked_card_closed_unaccepted"

    updated = needfix_store.get_needfix(init_store, record["id"])
    assert updated["status"] == "accepted"
    assert updated["converted_task_id"] is None

    events = needfix_store.list_events(init_store, record["id"])
    assert events[0]["event"] == "orphaned_conversion_returned_to_accepted"
    assert events[0]["detail"]["reason"] == "linked_card_closed_unaccepted"

    # Idempotent: no longer task_created, a second run changes nothing.
    again = needfix_store.reconcile_orphaned_conversions(
        init_store,
        get_task_fn=lambda tid: card if tid == task_id else None,
        canonical_status_fn=lambda c: c["status"],
    )
    assert again["returned_to_accepted_count"] == 0


def test_finished_card_is_left_alone_not_reverted(init_store: Path):
    """A directly-created merge/multi-finding card legitimately closes its
    NeedFix records while canonical status "finished"; this must stay
    untouched here, or that existing, intended behaviour regresses.
    """
    record = _make_task_created(init_store, title="finished-already-closed")
    task_id = record["converted_task_id"]
    card = {"task_id": task_id, "status": "finished"}

    report = needfix_store.reconcile_orphaned_conversions(
        init_store,
        get_task_fn=lambda tid: card if tid == task_id else None,
        canonical_status_fn=lambda c: c["status"],
    )
    assert report["returned_to_accepted_count"] == 0
    assert report["resolved_count"] == 0

    updated = needfix_store.get_needfix(init_store, record["id"])
    assert updated["status"] == "task_created"
    assert updated["converted_task_id"] == task_id


# --------------------------------------------------------------------------- #
# (4): scripts/needfix_commit_backfill.py                                     #
# --------------------------------------------------------------------------- #

def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )
    return result.stdout


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _run_git(repo, "add", filename)
    _run_git(repo, "commit", "-q", "-m", message)
    return _run_git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def git_repo() -> Path:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        _run_git(repo, "init", "-q")
        _run_git(repo, "config", "user.email", "test@example.com")
        _run_git(repo, "config", "user.name", "Test")
        needfix_store.initialize_repository(repo)
        yield repo


def test_backfill_preview_lists_trailer_and_subject_ids_and_skips_part(
    git_repo: Path,
):
    trailer_rec = needfix_store.add_needfix(
        git_repo, title="trailer-case", description="d", status="accepted",
    )
    subject_rec = needfix_store.add_needfix(
        git_repo, title="subject-case", description="d", status="accepted",
    )
    part_rec = needfix_store.add_needfix(
        git_repo, title="part-case", description="d", status="accepted",
    )

    sha_trailer = _commit(
        git_repo, "a.txt", "a",
        f"docs: unrelated subject text\n\nResolves: {trailer_rec['id']}\n",
    )
    sha_subject = _commit(
        git_repo, "b.txt", "b", f"fix: resolve the thing ({subject_rec['id']})",
    )

    _commit(git_repo, "c.txt", "c", f"fix: part 1 of the thing ({part_rec['id']})")

    eligible, skipped = backfill.collect_candidates(git_repo)
    eligible_triples = {(row["needfix_id"], row["source"], row["sha"]) for row in eligible}
    assert (trailer_rec["id"], "trailer", sha_trailer) in eligible_triples
    assert (subject_rec["id"], "subject", sha_subject) in eligible_triples

    skipped_ids = {row["needfix_id"] for row in skipped}
    assert part_rec["id"] in skipped_ids
    assert part_rec["id"] not in {row["needfix_id"] for row in eligible}

    # Preview (default) never mutates.
    exit_code = backfill.main(["--repo", str(git_repo)])
    assert exit_code == 0
    for rec in (trailer_rec, subject_rec, part_rec):
        assert needfix_store.get_needfix(git_repo, rec["id"])["status"] == "accepted"


def test_backfill_confirm_resolves_only_the_non_skipped_ids(git_repo: Path):
    trailer_rec = needfix_store.add_needfix(
        git_repo, title="trailer-case", description="d", status="accepted",
    )
    subject_rec = needfix_store.add_needfix(
        git_repo, title="subject-case", description="d", status="accepted",
    )
    part_rec = needfix_store.add_needfix(
        git_repo, title="part-case", description="d", status="accepted",
    )
    archived_rec = needfix_store.add_needfix(
        git_repo, title="already-archived", description="d", status="accepted",
    )
    needfix_store.archive_needfix(git_repo, archived_rec["id"])

    _commit(
        git_repo, "a.txt", "a",
        f"docs: unrelated subject text\n\nResolves: {trailer_rec['id']}\n",
    )
    _commit(git_repo, "b.txt", "b", f"fix: resolve the thing ({subject_rec['id']})")
    _commit(git_repo, "c.txt", "c", f"fix: part 1 of the thing ({part_rec['id']})")
    _commit(
        git_repo, "d.txt", "d",
        f"docs: unrelated subject text\n\nResolves: {archived_rec['id']}\n",
    )

    exit_code = backfill.main(["--repo", str(git_repo), "--confirm"])
    assert exit_code == 0

    assert needfix_store.get_needfix(git_repo, trailer_rec["id"])["status"] == "resolved"
    assert needfix_store.get_needfix(git_repo, subject_rec["id"])["status"] == "resolved"
    assert needfix_store.get_needfix(git_repo, part_rec["id"])["status"] == "accepted"
    assert needfix_store.get_needfix(git_repo, archived_rec["id"])["status"] == "archived"

    # Idempotent: a second --confirm run changes nothing further.
    exit_code_again = backfill.main(["--repo", str(git_repo), "--confirm"])
    assert exit_code_again == 0
    assert needfix_store.get_needfix(git_repo, trailer_rec["id"])["status"] == "resolved"


def test_archived_card_with_accepted_outcome_evidence_resolves(init_store: Path):
    """A card accepted then archived must RESOLVE its NeedFix, never reopen
    it to accepted -- the archived-without-evidence case stays covered by
    test_archived_unaccepted_card_returns_to_accepted above.
    """
    record = _make_task_created(init_store, title="archived-with-evidence")
    task_id = record["converted_task_id"]
    card = {"task_id": task_id, "status": "archived"}
    _seed_accepted_learning_commit(init_store, task_id, request_id="req-archived")

    report = needfix_store.reconcile_orphaned_conversions(
        init_store,
        get_task_fn=lambda tid: card if tid == task_id else None,
        canonical_status_fn=lambda c: c["status"],
    )
    assert report["resolved_count"] == 1
    assert report["resolved"][0]["needfix_id"] == record["id"]
    assert report["resolved"][0]["reason"] == "linked_card_archived_accepted"

    updated = needfix_store.get_needfix(init_store, record["id"])
    assert updated["status"] == "resolved"
    assert any(ref.startswith("learning_commit:") for ref in updated["evidence_refs"])

    events = needfix_store.list_events(init_store, record["id"])
    assert events[0]["event"] == "orphaned_conversion_resolved"
    assert events[0]["detail"]["reason"] == "linked_card_archived_accepted"

    # Idempotent: already terminal, a second run changes nothing further.
    again = needfix_store.reconcile_orphaned_conversions(
        init_store,
        get_task_fn=lambda tid: card if tid == task_id else None,
        canonical_status_fn=lambda c: c["status"],
    )
    assert again["resolved_count"] == 0
    assert again["returned_to_accepted_count"] == 0


# --------------------------------------------------------------------------- #
# git_scan_failed: distinguish a git failure from a truly empty history      #
# --------------------------------------------------------------------------- #

def _raise_git_timeout(repo: Path, rev_args: list[str]):
    raise subprocess.TimeoutExpired(cmd="git", timeout=1)


def test_collect_candidates_none_on_git_timeout(git_repo: Path, monkeypatch):
    monkeypatch.setattr(backfill, "_git_rev_list_oldest_first", _raise_git_timeout)
    assert backfill.collect_candidates(git_repo) is None


def test_collect_candidates_none_on_git_failure(git_repo: Path, monkeypatch):
    monkeypatch.setattr(
        backfill, "_git_rev_list_oldest_first", lambda repo, rev_args: None
    )
    assert backfill.collect_candidates(git_repo) is None


def test_main_exits_2_with_git_scan_failed_on_git_failure(
    git_repo: Path, monkeypatch, capsys
):
    monkeypatch.setattr(
        backfill, "_git_rev_list_oldest_first", lambda repo, rev_args: None
    )
    exit_code = backfill.main(["--repo", str(git_repo)])
    assert exit_code == 2
    assert "git_scan_failed" in capsys.readouterr().err


def test_main_exits_0_on_empty_but_successful_history(git_repo: Path, monkeypatch):
    monkeypatch.setattr(
        backfill, "_git_rev_list_oldest_first", lambda repo, rev_args: []
    )
    exit_code = backfill.main(["--repo", str(git_repo)])
    assert exit_code == 0


def test_main_exits_2_with_git_scan_failed_when_subjects_of_times_out(
    git_repo: Path, monkeypatch, capsys
):
    _commit(git_repo, "a.txt", "a", "chore: unrelated")

    def _raise_subjects_of_timeout(repo: Path, shas: list[str]):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(backfill, "_subjects_of", _raise_subjects_of_timeout)
    exit_code = backfill.main(["--repo", str(git_repo)])
    assert exit_code == 2
    assert "git_scan_failed" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Final-parenthesis subject ids: full multi-id form and short-suffix form    #
# --------------------------------------------------------------------------- #

def test_subject_trailing_parenthesis_reports_every_nf_id_both_shapes(
    git_repo: Path,
):
    rec_a = needfix_store.add_needfix(
        git_repo, title="multi-a", description="d", status="accepted",
    )
    rec_b = needfix_store.add_needfix(
        git_repo, title="multi-b", description="d", status="accepted",
    )
    rec_c = needfix_store.add_needfix(
        git_repo, title="short-a", description="d", status="accepted",
    )
    rec_d = needfix_store.add_needfix(
        git_repo, title="short-b", description="d", status="accepted",
    )

    sha_full = _commit(
        git_repo, "a.txt", "a",
        f"fix: two findings ({rec_a['id']}/{rec_b['id']})",
    )
    short_suffix = rec_d["id"].rsplit("-", 1)[-1]
    sha_short = _commit(
        git_repo, "b.txt", "b",
        f"fix: two findings ({rec_c['id']}/{short_suffix})",
    )

    eligible, _skipped = backfill.collect_candidates(git_repo)
    eligible_triples = {(row["needfix_id"], row["source"], row["sha"]) for row in eligible}
    assert (rec_a["id"], "subject", sha_full) in eligible_triples
    assert (rec_b["id"], "subject", sha_full) in eligible_triples
    assert (rec_c["id"], "subject", sha_short) in eligible_triples
    assert (rec_d["id"], "subject", sha_short) in eligible_triples
