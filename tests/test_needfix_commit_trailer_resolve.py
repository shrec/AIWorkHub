"""Tests for resolving NeedFixes from HEAD-reachable git commit trailers.

Covers NF-2026-01048(b): a bounded, idempotent read-time reconcile scans only
the unseen ``<last>..HEAD`` commit range for a structured ``Resolves:
NF-YYYY-NNNNN`` git trailer (never a free-text body mention) and resolves
that NeedFix exactly like a manager's verified direct resolution.
"""

from __future__ import annotations

import math
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

from aiworkhub import needfix_ingest, needfix_store, task_store


@pytest.fixture
def repo_root() -> Path:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def init_store(repo_root: Path) -> Path:
    result = needfix_store.initialize_repository(repo_root)
    assert result["initialized"] is True
    return repo_root


def _git(cwd: Path, *args: str) -> str:
    # cwd= as well as -C: git resolves getcwd() on the inherited directory
    # before applying -C, and a sandboxed pytest cwd may not be resolvable.
    result = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(init_store: Path) -> Path:
    try:
        _git(init_store, "init", "-q")
    except subprocess.CalledProcessError as exc:
        pytest.skip(
            f"git init unavailable (rc={exc.returncode}): "
            f"{(exc.stderr or '')[:300]}"
        )
    (init_store / "README.md").write_text("seed\n", encoding="utf-8")
    _git(init_store, "add", "README.md")
    _git(init_store, "commit", "-q", "-m", "seed")
    return init_store


def _commit(repo: Path, filename: str, message: str) -> str:
    """Write ``filename`` and commit it with ``message`` as the full commit
    message (preserving embedded blank lines / paragraph structure exactly,
    since no shell is involved). Returns the new commit's full oid."""
    path = repo / filename
    path.write_text(message, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _accepted(repo: Path, title: str) -> str:
    rec = needfix_store.capture_proposal(repo, title=title, description=title)
    needfix_store.triage_needfix(repo, rec["id"])
    return needfix_store.accept_needfix(repo, rec["id"])["id"]


# --- end-to-end: read reconcile scans HEAD-reachable commit trailers --------


def test_trailer_resolves_non_terminal_nf_on_next_read(git_repo: Path):
    nfid = _accepted(git_repo, "fixed-by-trailer")
    sha = _commit(
        git_repo, "fix.txt",
        f"Fix the thing\n\nExplains the change.\n\nResolves: {nfid}\n",
    )

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    row = needfix_store.get_needfix(git_repo, nfid)
    assert row["status"] == "resolved"
    assert row["resolved_at"] is not None
    assert f"git:{sha}" in row["evidence_refs"]
    events = needfix_store.list_events(git_repo, nfid)
    trailer_events = [e for e in events if e["event"] == "commit_trailer_resolved"]
    assert len(trailer_events) == 1
    assert trailer_events[0]["detail"]["commit"] == sha


def test_second_read_adds_nothing(git_repo: Path):
    nfid = _accepted(git_repo, "fixed-by-trailer-idempotent")
    _commit(git_repo, "fix.txt", f"Fix\n\nResolves: {nfid}\n")

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    events_after_first = needfix_store.list_events(git_repo, nfid)

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert needfix_store.list_events(git_repo, nfid) == events_after_first
    assert needfix_store.get_needfix(git_repo, nfid)["status"] == "resolved"


def test_body_mention_without_trailer_is_unchanged(git_repo: Path):
    nfid = _accepted(git_repo, "body-mention-only")
    _commit(
        git_repo, "mention.txt",
        f"Mentions {nfid} in prose\n\n"
        f"Resolves: {nfid} is discussed here as plain body text.\n\n"
        "This trailing paragraph pushes the mention out of trailer position, "
        "so git's own trailer parser never treats it as a Resolves trailer.\n",
    )

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    row = needfix_store.get_needfix(git_repo, nfid)
    assert row["status"] == "accepted"
    assert row["resolved_at"] is None


def test_unknown_nf_id_in_trailer_is_a_noop(git_repo: Path):
    _commit(git_repo, "fix.txt", "Fix\n\nResolves: NF-2099-99999\n")

    # Must not raise even though no such NeedFix exists in this repository.
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)


@pytest.mark.parametrize(
    "terminal_status", ["rejected", "duplicate", "resolved", "archived"]
)
def test_terminal_nf_is_unchanged(git_repo: Path, terminal_status: str):
    rec = needfix_store.add_needfix(
        git_repo, title="already-terminal", description="d", status=terminal_status
    )
    before = needfix_store.get_needfix(git_repo, rec["id"])
    _commit(git_repo, "fix.txt", f"Fix\n\nResolves: {rec['id']}\n")

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert needfix_store.get_needfix(git_repo, rec["id"]) == before


def test_converting_nf_is_unchanged(git_repo: Path):
    rec = needfix_store.add_needfix(
        git_repo, title="mid-convert", description="d", status="accepted"
    )
    conn = needfix_store._connect(git_repo)
    try:
        conn.execute(
            "UPDATE needfix SET status = 'converting' WHERE id = ?", (rec["id"],)
        )
    finally:
        conn.close()
    before = needfix_store.get_needfix(git_repo, rec["id"])
    _commit(git_repo, "fix.txt", f"Fix\n\nResolves: {rec['id']}\n")

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert needfix_store.get_needfix(git_repo, rec["id"]) == before


def test_repo_where_git_fails_is_a_noop_without_raising(init_store: Path):
    # init_store has no .git directory at all -- git commands fail closed.
    needfix_ingest._reconcile_commit_trailers_on_read(init_store)


def test_unchanged_head_runs_no_git_log_scan(git_repo: Path, monkeypatch):
    nfid = _accepted(git_repo, "watch-for-rescan")
    _commit(git_repo, "fix.txt", f"Fix\n\nResolves: {nfid}\n")
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    assert needfix_store.get_needfix(git_repo, nfid)["status"] == "resolved"

    original_run = subprocess.run
    calls: list[list[str]] = []

    def spy(args, **kwargs):
        calls.append(list(args))
        return original_run(args, **kwargs)

    monkeypatch.setattr(needfix_ingest.subprocess, "run", spy)
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert calls, "rev-parse HEAD must still run to detect nothing changed"
    assert all("log" not in call for call in calls)


def _meta(repo: Path) -> str | None:
    conn = needfix_store._connect(repo)
    try:
        return needfix_store._get_meta(conn, needfix_ingest._COMMIT_TRAILER_META_KEY)
    finally:
        conn.close()


def test_scan_is_capped(git_repo: Path, monkeypatch):
    monkeypatch.setattr(needfix_ingest, "_COMMIT_TRAILER_MAX_COUNT", 2)
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    for i in range(4):
        _commit(git_repo, f"c{i}.txt", f"unrelated change {i}, no trailer\n")
    logged: list[str] = []
    original_run = subprocess.run

    def spy(args, **kwargs):
        if "log" in args:
            logged.append(kwargs.get("input") or "")
        return original_run(args, **kwargs)

    monkeypatch.setattr(needfix_ingest.subprocess, "run", spy)

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert len(logged) == 1, "git log must have run for the first reconcile"
    assert len(logged[0].split()) == 2


def test_first_read_backfills_only_a_bounded_newest_window(
    git_repo: Path, monkeypatch
):
    monkeypatch.setattr(needfix_ingest, "_COMMIT_TRAILER_MAX_COUNT", 2)
    nf_old = _accepted(git_repo, "outside-backfill-window")
    nf_new = _accepted(git_repo, "inside-backfill-window")
    _commit(git_repo, "w0.txt", f"Oldest\n\nResolves: {nf_old}\n")
    _commit(git_repo, "w1.txt", "plain 1\n")
    c2 = _commit(git_repo, "w2.txt", "plain 2\n")
    c3 = _commit(git_repo, "w3.txt", f"Newest\n\nResolves: {nf_new}\n")
    assert _meta(git_repo) is None
    calls: list[list[str]] = []
    logged: list[str] = []
    original_run = subprocess.run

    def spy(args, **kwargs):
        calls.append(list(args))
        if "log" in args:
            logged.append(kwargs.get("input") or "")
        return original_run(args, **kwargs)

    monkeypatch.setattr(needfix_ingest.subprocess, "run", spy)

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    rev_lists = [call for call in calls if "rev-list" in call]
    assert rev_lists
    assert all("--max-count=2" in call for call in rev_lists)
    assert all("--topo-order" not in call for call in rev_lists)
    assert len(logged) == 1
    assert logged[0].split() == [c2, c3]
    assert needfix_store.get_needfix(git_repo, nf_new)["status"] == "resolved"
    assert needfix_store.get_needfix(git_repo, nf_old)["status"] == "accepted"
    assert _meta(git_repo) == c3 == _git(git_repo, "rev-parse", "HEAD")

    calls.clear()
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert all("log" not in call for call in calls)


def test_capped_scan_is_lossless_across_reads(git_repo: Path, monkeypatch):
    monkeypatch.setattr(needfix_ingest, "_COMMIT_TRAILER_MAX_COUNT", 2)
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    nfids = [_accepted(git_repo, f"capped-{i}") for i in range(5)]
    shas = [
        _commit(git_repo, f"fix{i}.txt", f"Fix {i}\n\nResolves: {nfid}\n")
        for i, nfid in enumerate(nfids)
    ]

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    statuses = [needfix_store.get_needfix(git_repo, n)["status"] for n in nfids]
    assert statuses == ["resolved", "resolved", "accepted", "accepted", "accepted"]
    assert _meta(git_repo) == shas[1]

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    assert _meta(git_repo) == shas[3]
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert all(
        needfix_store.get_needfix(git_repo, n)["status"] == "resolved" for n in nfids
    )
    assert _meta(git_repo) == shas[4] == _git(git_repo, "rev-parse", "HEAD")


def test_raising_resolve_does_not_hold_the_watermark(git_repo: Path, monkeypatch):
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    poisoned = _accepted(git_repo, "poisoned-row")
    healthy = _accepted(git_repo, "healthy-row")
    _commit(git_repo, "fix1.txt", f"Fix 1\n\nResolves: {poisoned}\n")
    _commit(git_repo, "fix2.txt", f"Fix 2\n\nResolves: {healthy}\n")
    original = needfix_store.resolve_from_commit_trailer

    def flaky(repo, nfid, sha):
        if nfid == poisoned:
            raise RuntimeError("corrupt evidence row")
        return original(repo, nfid, sha)

    monkeypatch.setattr(needfix_store, "resolve_from_commit_trailer", flaky)

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert needfix_store.get_needfix(git_repo, healthy)["status"] == "resolved"
    assert needfix_store.get_needfix(git_repo, poisoned)["status"] == "accepted"
    assert _meta(git_repo) == _git(git_repo, "rev-parse", "HEAD")


def test_transient_resolve_failure_holds_the_watermark(git_repo: Path, monkeypatch):
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    meta_before = _meta(git_repo)
    nfid = _accepted(git_repo, "locked-db-row")
    _commit(git_repo, "fix.txt", f"Fix\n\nResolves: {nfid}\n")
    original = needfix_store.resolve_from_commit_trailer
    raised = {"n": 0}

    def locked_once(repo, needfix_id, sha):
        if raised["n"] == 0:
            raised["n"] += 1
            raise sqlite3.OperationalError("database is locked")
        return original(repo, needfix_id, sha)

    monkeypatch.setattr(needfix_store, "resolve_from_commit_trailer", locked_once)

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert raised["n"] == 1
    assert _meta(git_repo) == meta_before
    row = needfix_store.get_needfix(git_repo, nfid)
    assert row["status"] == "accepted"
    assert row["resolved_at"] is None

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert needfix_store.get_needfix(git_repo, nfid)["status"] == "resolved"
    assert _meta(git_repo) == _git(git_repo, "rev-parse", "HEAD")


def test_rev_list_timeout_falls_back_to_newest_window(git_repo: Path, monkeypatch):
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    nfid = _accepted(git_repo, "range-timeout-row")
    sha = _commit(git_repo, "fix.txt", f"Fix\n\nResolves: {nfid}\n")
    real_run = subprocess.run
    calls: list[list[str]] = []

    def spy(args, *a, **kw):
        calls.append(list(args))
        if "rev-list" in args and "--topo-order" in args:
            raise subprocess.TimeoutExpired(args, kw.get("timeout") or 0)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(needfix_ingest.subprocess, "run", spy)

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    monkeypatch.setattr(needfix_ingest.subprocess, "run", real_run)

    assert any("rev-list" in c and "--topo-order" in c for c in calls)
    window_calls = [
        c for c in calls
        if "rev-list" in c and any(arg.startswith("--max-count") for arg in c)
    ]
    assert window_calls, calls
    assert _meta(git_repo) == sha == _git(git_repo, "rev-parse", "HEAD")
    assert needfix_store.get_needfix(git_repo, nfid)["status"] == "resolved"


def test_non_ascii_trailer_message_resolves(git_repo: Path, tmp_path: Path):
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    nfid = _accepted(git_repo, "non-ascii-message")
    (git_repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    _git(git_repo, "add", "fix.txt")
    message_file = tmp_path / "msg.txt"
    message_file.write_text(
        f"გამართვა — fix the thing\n\nResolves: {nfid}\n", encoding="utf-8"
    )
    _git(git_repo, "commit", "-q", "-F", str(message_file))

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert needfix_store.get_needfix(git_repo, nfid)["status"] == "resolved"
    assert _meta(git_repo) == _git(git_repo, "rev-parse", "HEAD")


def _unscanned(repo: Path) -> int:
    boundaries = (_meta(repo) or "").split()
    return int(_git(repo, "rev-list", "--count", "HEAD", "--not", *boundaries))


def test_capped_scan_makes_progress_across_merge(git_repo: Path, monkeypatch):
    cap = 2
    monkeypatch.setattr(needfix_ingest, "_COMMIT_TRAILER_MAX_COUNT", cap)
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    nf_a = _accepted(git_repo, "merge-side-a")
    nf_b = _accepted(git_repo, "merge-side-b")
    nf_m = _accepted(git_repo, "merge-commit")
    base = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "checkout", "-q", "-b", "side_a", base)
    _commit(git_repo, "a0.txt", "side a 0\n")
    _commit(git_repo, "a1.txt", f"Side a fix\n\nResolves: {nf_a}\n")
    _commit(git_repo, "a2.txt", "side a 2\n")
    _git(git_repo, "checkout", "-q", "-b", "side_b", base)
    _commit(git_repo, "b0.txt", "side b 0\n")
    _commit(git_repo, "b1.txt", "side b 1\n")
    _commit(git_repo, "b2.txt", f"Side b fix\n\nResolves: {nf_b}\n")
    _git(git_repo, "checkout", "-q", "side_a")
    _git(
        git_repo, "merge", "--no-ff", "-q",
        "-m", f"Merge side_b\n\nResolves: {nf_m}\n", "side_b",
    )
    head = _git(git_repo, "rev-parse", "HEAD")
    total = _unscanned(git_repo)
    assert total == 7

    remaining = total
    for _ in range(math.ceil(total / cap) + 1):
        if _meta(git_repo) == head:
            break
        needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
        after = _unscanned(git_repo)
        if remaining > cap:
            assert after == remaining - cap
        else:
            assert after == 0
        remaining = after

    assert _meta(git_repo) == head
    for nfid in (nf_a, nf_b, nf_m):
        assert needfix_store.get_needfix(git_repo, nfid)["status"] == "resolved"


def test_legacy_single_sha_meta_is_honoured_as_boundary(git_repo: Path, monkeypatch):
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    nf_before = _accepted(git_repo, "before-boundary")
    nf_after = _accepted(git_repo, "after-boundary")
    c0 = _commit(git_repo, "c0.txt", f"Before\n\nResolves: {nf_before}\n")
    c1 = _commit(git_repo, "c1.txt", "plain\n")
    c2 = _commit(git_repo, "c2.txt", f"After\n\nResolves: {nf_after}\n")
    conn = needfix_store._connect(git_repo)
    try:
        needfix_store._set_meta(conn, needfix_ingest._COMMIT_TRAILER_META_KEY, c0)
    finally:
        conn.close()
    logged: list[str] = []
    original_run = subprocess.run

    def spy(args, **kwargs):
        if "log" in args:
            logged.append(kwargs.get("input") or "")
        return original_run(args, **kwargs)

    monkeypatch.setattr(needfix_ingest.subprocess, "run", spy)

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert len(logged) == 1
    assert set(logged[0].split()) == {c1, c2}
    assert needfix_store.get_needfix(git_repo, nf_before)["status"] == "accepted"
    assert needfix_store.get_needfix(git_repo, nf_after)["status"] == "resolved"
    assert _meta(git_repo) == c2


def test_empty_range_sets_meta_without_git_log(git_repo: Path, monkeypatch):
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    head = _git(git_repo, "rev-parse", "HEAD")
    _commit(git_repo, "later.txt", "later\n")
    later = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "reset", "-q", "--hard", head)
    conn = needfix_store._connect(git_repo)
    try:
        # A last-seen commit that descends from HEAD makes <last>..HEAD empty.
        needfix_store._set_meta(conn, needfix_ingest._COMMIT_TRAILER_META_KEY, later)
    finally:
        conn.close()
    calls: list[list[str]] = []
    original_run = subprocess.run

    def spy(args, **kwargs):
        calls.append(list(args))
        return original_run(args, **kwargs)

    monkeypatch.setattr(needfix_ingest.subprocess, "run", spy)

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert all("log" not in call for call in calls)
    assert _meta(git_repo) == head


def test_stale_last_head_self_heals(git_repo: Path):
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    conn = needfix_store._connect(git_repo)
    try:
        needfix_store._set_meta(
            conn, needfix_ingest._COMMIT_TRAILER_META_KEY, "0" * 40
        )
    finally:
        conn.close()
    nfid = _accepted(git_repo, "stale-last-head")
    _commit(git_repo, "fix.txt", f"Fix\n\nResolves: {nfid}\n")

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert needfix_store.get_needfix(git_repo, nfid)["status"] == "resolved"
    conn = needfix_store._connect(git_repo)
    try:
        meta = needfix_store._get_meta(conn, needfix_ingest._COMMIT_TRAILER_META_KEY)
    finally:
        conn.close()
    assert meta == _git(git_repo, "rev-parse", "HEAD")


def _spy_calls(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    original_run = subprocess.run

    def spy(args, **kwargs):
        calls.append(list(args))
        return original_run(args, **kwargs)

    monkeypatch.setattr(needfix_ingest.subprocess, "run", spy)
    return calls


def _windowed(calls: list[list[str]]) -> bool:
    return any(
        "rev-list" in call and any(arg.startswith("--max-count") for arg in call)
        for call in calls
    )


def test_transient_rev_list_failure_keeps_the_boundary(git_repo: Path, monkeypatch):
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    seed_head = _meta(git_repo)
    assert seed_head == _git(git_repo, "rev-parse", "HEAD")
    nfid = _accepted(git_repo, "transient-failure")
    _commit(git_repo, "fix.txt", f"Fix\n\nResolves: {nfid}\n")
    original = needfix_ingest._git_rev_list_oldest_first
    monkeypatch.setattr(
        needfix_ingest, "_git_rev_list_oldest_first", lambda repo, rev_args: None
    )
    calls = _spy_calls(monkeypatch)

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert _meta(git_repo) == seed_head
    assert all("log" not in call for call in calls)
    assert not _windowed(calls)
    assert needfix_store.get_needfix(git_repo, nfid)["status"] == "accepted"

    monkeypatch.setattr(needfix_ingest, "_git_rev_list_oldest_first", original)
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert needfix_store.get_needfix(git_repo, nfid)["status"] == "resolved"
    assert _meta(git_repo) == _git(git_repo, "rev-parse", "HEAD")


def test_only_stale_boundaries_are_dropped(git_repo: Path, monkeypatch):
    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)
    seed_head = _git(git_repo, "rev-parse", "HEAD")
    conn = needfix_store._connect(git_repo)
    try:
        needfix_store._set_meta(
            conn, needfix_ingest._COMMIT_TRAILER_META_KEY, f"{seed_head} {'0' * 40}"
        )
    finally:
        conn.close()
    nfid = _accepted(git_repo, "mixed-boundaries")
    _commit(git_repo, "fix.txt", f"Fix\n\nResolves: {nfid}\n")
    calls = _spy_calls(monkeypatch)

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert needfix_store.get_needfix(git_repo, nfid)["status"] == "resolved"
    assert _meta(git_repo) == _git(git_repo, "rev-parse", "HEAD")
    assert not _windowed(calls)


def test_missing_meta_table_is_created_lazily(git_repo: Path):
    nfid = _accepted(git_repo, "no-meta-table")
    conn = needfix_store._connect(git_repo)
    try:
        conn.execute("DROP TABLE needfix_meta")
        conn.commit()
    finally:
        conn.close()
    _commit(git_repo, "fix.txt", f"Fix\n\nResolves: {nfid}\n")

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert needfix_store.get_needfix(git_repo, nfid)["status"] == "resolved"


def test_public_read_surface_runs_trailer_reconcile(git_repo: Path):
    task_store.initialize_repository(git_repo)
    nfid = _accepted(git_repo, "public-surface")
    before = needfix_ingest.list_active(git_repo)
    assert before["derived"] is True
    assert nfid in {item["id"] for item in before["items"]}
    _commit(git_repo, "fix.txt", f"Fix\n\nResolves: {nfid}\n")

    after = needfix_ingest.list_active(git_repo)

    assert after["derived"] is True
    assert nfid not in {item["id"] for item in after["items"]}
    assert needfix_store.get_needfix(git_repo, nfid)["status"] == "resolved"
    assert needfix_ingest.count_active(git_repo)["count"] == after["count"]


# --- direct store-level check: resolve_from_commit_trailer itself ----------


def test_resolve_from_commit_trailer_is_idempotent_on_evidence_refs(init_store: Path):
    nfid = _accepted(init_store, "direct-resolve")
    sha = "f" * 40

    first = needfix_store.resolve_from_commit_trailer(init_store, nfid, sha)
    assert first["status"] == "resolved"
    assert first["evidence_refs"].count(f"git:{sha}") == 1

    # Already resolved -> terminal -> silent no-op, not re-resolved.
    second = needfix_store.resolve_from_commit_trailer(init_store, nfid, sha)
    assert second is None


def test_resolve_from_commit_trailer_is_atomic_on_event_failure(
    init_store: Path, monkeypatch
):
    nfid = _accepted(init_store, "atomic-resolve")
    before = needfix_store.get_needfix(init_store, nfid)

    def boom(*args, **kwargs):
        raise RuntimeError("event insert failed")

    monkeypatch.setattr(needfix_store, "_record_event", boom)

    with pytest.raises(RuntimeError, match="event insert failed"):
        needfix_store.resolve_from_commit_trailer(init_store, nfid, "e" * 40)

    after = needfix_store.get_needfix(init_store, nfid)
    assert after["status"] == "accepted"
    assert after["resolved_at"] is None
    assert after["evidence_refs"] == before["evidence_refs"]


# --- trailer id matching is exact -------------------------------------------


def test_trailer_id_needs_word_boundaries(git_repo: Path):
    nfid = _accepted(git_repo, "boundary")
    _commit(git_repo, "a.txt", f"Longer id\n\nResolves: {nfid}1\n")
    _commit(git_repo, "b.txt", f"Prefixed id\n\nResolves: X{nfid}\n")

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    row = needfix_store.get_needfix(git_repo, nfid)
    assert row["status"] == "accepted"
    assert row["resolved_at"] is None


def test_comma_separated_trailer_resolves_each_id(git_repo: Path):
    first = _accepted(git_repo, "comma-a")
    second = _accepted(git_repo, "comma-b")
    _commit(git_repo, "fix.txt", f"Fix both\n\nResolves: {first}, {second}\n")

    needfix_ingest._reconcile_commit_trailers_on_read(git_repo)

    assert needfix_store.get_needfix(git_repo, first)["status"] == "resolved"
    assert needfix_store.get_needfix(git_repo, second)["status"] == "resolved"
