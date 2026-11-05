"""NF-2026-02068: the reconciler's attribution pass is budgeted and cheap.

Measured on 0.12.87: ``sdlc_sync:attribution`` ran for 1476 s inside the MCP
server. The catalog was reloaded per NeedFix, the part ignored the pass
deadline, and ``explaining_card`` walked every candidate card's history with
two git subprocesses per (commit, path). These tests pin the three fixes:
one catalog per pass, a deadline checked between NeedFixes, and an inverted
``explaining_card`` that returns exactly what the old loop returned.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _path in (_SRC, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import test_sdlc_sync as base  # noqa: E402
from test_sdlc_sync import _fresh_git_facts, repo, spies  # noqa: E402,F401

from aiworkhub import sdlc_attribution, sdlc_sync  # noqa: E402

FIX_TASKS = [f"needfix-NF-2026-0000{n}-r1" for n in (1, 2, 3)]
NEEDFIX_IDS = [f"NF-2026-0000{n}" for n in (1, 2, 3)]


def _pending(root: Path) -> set[str]:
    conn = sqlite3.connect(str(root.joinpath(*sdlc_sync.STATE_DB_REL)))
    try:
        return {row[0] for row in conn.execute("SELECT needfix_id FROM pending_attribution")}
    finally:
        conn.close()


def _accept_fixes(root: Path) -> None:
    for task_id in FIX_TASKS:
        base._create_task(root, task_id)
        base._append_task_event(root, task_id, "accept_review")


# --------------------------------------------------------------------------
# one catalog per pass
# --------------------------------------------------------------------------

def test_the_accepted_catalog_is_loaded_once_per_pass(repo, spies, monkeypatch):
    _accept_fixes(repo.root)
    sentinel = object()
    loads: list[tuple] = []
    seen: list[tuple[str, object]] = []

    def load_accepted_catalog(root, repository_id):
        loads.append((root, repository_id))
        return sentinel

    def attribute_needfix(root, repository_id, needfix_id, *, catalog):
        seen.append((needfix_id, catalog))
        return SimpleNamespace(attributed=True, reason="")

    monkeypatch.setattr(sdlc_sync.sdlc_attribution, "load_accepted_catalog", load_accepted_catalog)
    monkeypatch.setattr(sdlc_sync.sdlc_attribution, "attribute_needfix", attribute_needfix)

    result = sdlc_sync.sync_once(repo.root, repo.repo_id)

    assert result["failures"] == []
    assert result["parts"]["attribution"]["state"] == "ok"
    assert loads == [(repo.root, repo.repo_id)]
    assert seen == [(needfix_id, sentinel) for needfix_id in NEEDFIX_IDS]
    assert _pending(repo.root) == set()


# --------------------------------------------------------------------------
# the deadline is checked between NeedFixes
# --------------------------------------------------------------------------

def test_attribution_stops_at_the_deadline_and_leaves_the_rest_pending(repo, spies, monkeypatch):
    _accept_fixes(repo.root)
    clock = {"now": 1000.0}
    monkeypatch.setattr(sdlc_sync, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    monkeypatch.setattr(sdlc_sync.sdlc_attribution, "load_accepted_catalog", lambda r, i: "catalog")
    seen: list[str] = []

    def slow_attribute(root, repository_id, needfix_id, *, catalog):
        seen.append(needfix_id)
        clock["now"] += 60.0  # one NeedFix spends the whole budget
        return SimpleNamespace(attributed=True, reason="")

    monkeypatch.setattr(sdlc_sync.sdlc_attribution, "attribute_needfix", slow_attribute)

    first = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=clock["now"] + 30.0)

    part = first["parts"]["attribution"]
    assert part["state"] == "deferred"
    assert part["reason"] == sdlc_sync.PASS_BUDGET_EXHAUSTED
    assert part["remaining"] == 2
    assert part["attributed"] == {NEEDFIX_IDS[0]: "attributed"}
    assert seen == NEEDFIX_IDS[:1]
    assert _pending(repo.root) == set(NEEDFIX_IDS[1:])

    second = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=clock["now"] + 1000.0)

    assert second["parts"]["attribution"]["state"] == "ok"
    assert seen == NEEDFIX_IDS
    assert _pending(repo.root) == set()


def test_an_already_expired_pass_still_queues_the_window_for_the_next_pass(
    repo, spies, monkeypatch
):
    _accept_fixes(repo.root)
    clock = {"now": 1000.0}
    monkeypatch.setattr(sdlc_sync, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    loads: list[str] = []
    monkeypatch.setattr(
        sdlc_sync.sdlc_attribution, "load_accepted_catalog",
        lambda r, i: loads.append("load") or "catalog",
    )

    first = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=clock["now"] - 1.0)

    assert first["parts"]["attribution"] == {
        "state": "deferred", "reason": sdlc_sync.PASS_BUDGET_EXHAUSTED,
    }
    assert loads == [] and spies.attributed == []
    # Durable even though the part never started: no accept can be lost.
    assert _pending(repo.root) == set(NEEDFIX_IDS)


# --------------------------------------------------------------------------
# inverted explaining_card == the old loop
# --------------------------------------------------------------------------

P = "src/pkg/shared.py"
Q = "src/pkg/other.py"


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    for key in sdlc_attribution.GIT_ENV_BLOCKLIST:
        env.pop(key, None)
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env,
    )
    return done.stdout.decode().strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for relative, text in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    _git(repo, "add", "--", *files)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _card(task_id: str, base_oid: str, hashes: dict[str, str]) -> sdlc_attribution.AcceptedCard:
    return sdlc_attribution.AcceptedCard(
        task_id=task_id,
        request_id=f"req-{task_id}",
        base_oid=base_oid,
        promoted_paths=tuple(sorted(hashes)),
        changed_path_hashes=dict(hashes),
        identity={"task_id": task_id},
    )


def _old_explaining_card(repo: Path, cards, path: str, commit: str, exclude):
    """The pre-NF-02068 loop, verbatim, with an unmemoized holding commit."""
    for card in cards:
        if card.key == exclude or path not in card.promoted_paths:
            continue
        holding = sdlc_attribution.first_holding_commit(
            repo, card.base_oid, card.promoted_paths, card.changed_path_hashes,
        )
        if holding[0] == commit:
            return card
    return None


def test_inverted_explaining_card_matches_the_old_loop(tmp_path, monkeypatch):
    repo = tmp_path / "git"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.autocrlf", "false")
    base_oid = _commit(repo, {P: "base\n", Q: "q-base\n"}, "base")
    commit_a = _commit(repo, {P: "alpha\n"}, "a")
    commit_b = _commit(repo, {P: "beta\n"}, "b")
    commit_c = _commit(repo, {P: "alpha\n", Q: "q-gamma\n"}, "c")

    # ``collides`` carries P's bytes from commit A but only lands at C: it passes
    # the hash filter for A and must still be refused there. ``twin`` lands at A
    # like ``alpha`` and is the excluded fix card in one query.
    collides = _card("T-COLLIDES", base_oid, {P: _sha("alpha\n"), Q: _sha("q-gamma\n")})
    twin = _card("T-TWIN", base_oid, {P: _sha("alpha\n")})
    alpha = _card("T-ALPHA", base_oid, {P: _sha("alpha\n")})
    beta = _card("T-BETA", base_oid, {P: _sha("beta\n")})
    other = _card("T-OTHER", base_oid, {Q: _sha("q-gamma\n")})
    never = _card("T-NEVER", base_oid, {P: _sha("never landed\n")})
    cards = [collides, twin, alpha, beta, other, never]

    catalog = sdlc_attribution.AcceptedCatalog(repo, cards)
    assert catalog.holding_commit(collides)[0] == commit_c
    assert catalog.holding_commit(alpha)[0] == commit_a

    fresh = sdlc_attribution.AcceptedCatalog(repo, cards)
    walked: list[str] = []
    original = sdlc_attribution.AcceptedCatalog.holding_commit

    def counting(self, card):
        walked.append(card.task_id)
        return original(self, card)

    monkeypatch.setattr(sdlc_attribution.AcceptedCatalog, "holding_commit", counting)

    excludes = [("", ""), twin.key, alpha.key, collides.key]
    for path in (P, Q):
        for commit in (base_oid, commit_a, commit_b, commit_c):
            for exclude in excludes:
                expected = _old_explaining_card(repo, cards, path, commit, exclude)
                assert fresh.explaining_card(path, commit, exclude=exclude) == expected, (
                    path, commit, exclude,
                )

    # The named cases, so a vacuous all-None answer cannot pass.
    assert fresh.explaining_card(P, commit_a, exclude=("", "")) == twin
    assert fresh.explaining_card(P, commit_a, exclude=twin.key) == alpha
    assert fresh.explaining_card(P, commit_b, exclude=("", "")) == beta
    assert fresh.explaining_card(P, commit_c, exclude=("", "")) == collides
    assert fresh.explaining_card(Q, commit_c, exclude=collides.key) == other
    assert fresh.explaining_card(P, base_oid, exclude=("", "")) is None
    # ``never``'s hash matches no blamed commit, so its history is never walked.
    assert "T-NEVER" not in walked
    # The (commit, path) memo answered repeat lookups instead of new git calls.
    assert fresh.path_sha_hits > 0
    assert fresh.path_sha_misses == len(fresh._path_sha)
