"""NF-2026-01964: a NeedFix fixed by a plain commit must have a discoverable
route to ``resolved``.

* A refused ``resolve`` names action=resolve_verified and the ``Resolves:``
  trailer after the byte-compatible allowed_next_transitions fields.
* ``resolve_verified_needfix`` also resolves from ``task_planned`` and records
  the real prior_status.
* Every ``commit:<sha>`` evidence ref must be an ancestor of HEAD.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

from aiworkhub import needfix_store


@pytest.fixture
def repo_root() -> Path:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert needfix_store.initialize_repository(root)["initialized"] is True
        yield root


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
def git_repo(repo_root: Path) -> Path:
    try:
        _git(repo_root, "init", "-q")
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"git init unavailable (rc={exc.returncode}): {(exc.stderr or '')[:300]}")
    (repo_root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-q", "-m", "seed")
    return repo_root


def _accepted(repo: Path, title: str = "plain commit fix") -> str:
    rec = needfix_store.capture_proposal(repo, title=title, description=title)
    needfix_store.triage_needfix(repo, rec["id"])
    return needfix_store.accept_needfix(repo, rec["id"])["id"]


def _ready(repo: Path, nf_id: str, refs: list[str]) -> None:
    needfix_store.update_needfix(
        repo,
        nf_id,
        readiness_score=100,
        evidence={"verified": "fixed on canonical HEAD"},
        evidence_refs=refs,
    )


def _events(repo: Path, nf_id: str) -> list[tuple[str, dict]]:
    db = repo.joinpath(*needfix_store.NEEDFIX_DB_REL)
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT event, detail_json FROM needfix_events WHERE needfix_id = ? ORDER BY rowid",
            (nf_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(kind, json.loads(detail or "{}")) for kind, detail in rows]


def test_resolve_refusal_names_resolve_verified_and_trailer(repo_root: Path) -> None:
    nf_id = _accepted(repo_root)
    with pytest.raises(needfix_store.NeedFixConflictError) as info:
        needfix_store.resolve_needfix(repo_root, nf_id, resolution_note="fixed")
    message = str(info.value)
    allowed = list(needfix_store.VALID_TRANSITIONS["accepted"])
    prefix = (
        "invalid transition: rejected='resolved' current_status='accepted' "
        f"allowed_next_transitions={allowed!r} "
        f"allowed_statuses={list(needfix_store.STATUSES)!r}"
    )
    assert message.startswith(prefix)
    assert "resolve_verified" in message
    assert "Resolves: NF-YYYY-NNNNN" in message
    assert needfix_store.get_needfix(repo_root, nf_id)["status"] == "accepted"


def test_non_resolve_refusal_is_unchanged(repo_root: Path) -> None:
    rec = needfix_store.capture_proposal(repo_root, title="t", description="t")
    with pytest.raises(needfix_store.NeedFixConflictError) as info:
        needfix_store.accept_needfix(repo_root, rec["id"])
    assert "resolve_verified" not in str(info.value)


def test_resolve_verified_from_task_planned_records_prior_status(git_repo: Path) -> None:
    head = _git(git_repo, "rev-parse", "HEAD")
    nf_id = _accepted(git_repo)
    needfix_store.mark_task_planned(git_repo, nf_id)
    _ready(git_repo, nf_id, [f"commit:{head}"])

    result = needfix_store.resolve_verified_needfix(
        git_repo, nf_id, resolution_note="fixed by plain commit"
    )

    assert result["status"] == "resolved"
    assert result["transition"]["status_before"] == "task_planned"
    assert result["transition"]["status_after"] == "resolved"
    events = [d for k, d in _events(git_repo, nf_id) if k == "manager_verified_resolved"]
    assert len(events) == 1
    assert events[0]["prior_status"] == "task_planned"


def test_resolve_verified_from_accepted_still_works(git_repo: Path) -> None:
    head = _git(git_repo, "rev-parse", "HEAD")
    nf_id = _accepted(git_repo)
    _ready(git_repo, nf_id, [f"commit:{head[:12]}", "file:docs/proof.md"])
    result = needfix_store.resolve_verified_needfix(git_repo, nf_id, resolution_note="ok")
    assert result["transition"]["status_before"] == "accepted"
    assert result["status"] == "resolved"


def test_resolve_verified_refuses_commit_not_in_history(git_repo: Path) -> None:
    bogus = "0123456789abcdef0123456789abcdef01234567"
    nf_id = _accepted(git_repo)
    _ready(git_repo, nf_id, [f"commit:{bogus}"])
    with pytest.raises(needfix_store.NeedFixConflictError) as info:
        needfix_store.resolve_verified_needfix(git_repo, nf_id, resolution_note="x")
    assert str(info.value) == f"needfix_resolve_commit_not_reachable:{bogus}"
    assert needfix_store.get_needfix(git_repo, nf_id)["status"] == "accepted"


def test_resolve_verified_refuses_commit_not_ancestor_of_head(git_repo: Path) -> None:
    base = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "checkout", "-q", "-b", "side")
    (git_repo / "side.txt").write_text("side\n", encoding="utf-8")
    _git(git_repo, "add", "side.txt")
    _git(git_repo, "commit", "-q", "-m", "side")
    side = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "checkout", "-q", base)
    nf_id = _accepted(git_repo)
    _ready(git_repo, nf_id, [f"commit:{side}"])
    with pytest.raises(needfix_store.NeedFixConflictError) as info:
        needfix_store.resolve_verified_needfix(git_repo, nf_id, resolution_note="x")
    assert str(info.value) == f"needfix_resolve_commit_not_reachable:{side}"


def test_resolve_verified_refusal_from_captured_names_routes(repo_root: Path) -> None:
    rec = needfix_store.capture_proposal(repo_root, title="t", description="t")
    with pytest.raises(needfix_store.NeedFixConflictError) as info:
        needfix_store.resolve_verified_needfix(repo_root, rec["id"], resolution_note="x")
    message = str(info.value)
    assert "current_status='captured'" in message
    assert "task_planned" in message
    assert "Resolves: NF-YYYY-NNNNN" in message


def test_git_check_uses_argv_and_repo_cwd(repo_root: Path, monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def spy(args, **kwargs):
        calls.append((list(args), kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(needfix_store.subprocess, "run", spy)
    sha = "a" * 40
    assert needfix_store._unreachable_commit_refs(
        repo_root, [f"commit:{sha}", "git:zzz", "file:x"]
    ) == []
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == "git"
    assert "merge-base" in argv and "--is-ancestor" in argv and argv[-1] == "HEAD"
    assert kwargs["cwd"] == str(repo_root)
    assert kwargs.get("shell") in (None, False)


def test_git_unavailable_fails_closed(repo_root: Path, monkeypatch) -> None:
    def boom(args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(needfix_store.subprocess, "run", boom)
    sha = "b" * 40
    assert needfix_store._unreachable_commit_refs(repo_root, [f"commit:{sha}"]) == [sha]
