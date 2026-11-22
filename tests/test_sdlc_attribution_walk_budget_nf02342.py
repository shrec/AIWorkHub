"""NF-2026-02342: the holding-commit walk sees the pass deadline and converges.

Measured: one ``sdlc_sync`` pass ran 1,997,969 ms because
``first_holding_commit`` walked every descendant of ``base_oid`` (two git
subprocesses per (commit, path)) with no deadline, and its per-pass memo was
thrown away, so a deferred pass redid the same walk next time. The walk now
raises ``BudgetExceeded`` at the deadline, an aborted walk is never memoized,
and a walk store records how far a walk got so the next pass resumes after it.
Every answer equals the unbounded walk's.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _path in (_SRC, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from test_sdlc_attribution_pass_nf02068 import (  # noqa: E402
    FIX_TASKS,
    NEEDFIX_IDS,
    _accept_fixes,
    _commit,
    _git,
    _pending,
    _sha,
)
from test_sdlc_sync import _fresh_git_facts, repo  # noqa: E402,F401

from aiworkhub import sdlc_attribution, sdlc_sync  # noqa: E402

BASE = "b" * 40
PATH = "src/pkg/walked.py"
HISTORY = 250
REPOSITORY_ID = "repo-nf02342"


def _oid(index: int) -> str:
    return f"{index + 1:040x}"


def _rewritten_oid(index: int) -> str:
    return f"e{index + 1:039x}"


class Clock:
    """Injected monotonic clock; only the fake git lookups advance it."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def namespace(self) -> SimpleNamespace:
        return SimpleNamespace(monotonic=self.monotonic, time=time.time, sleep=time.sleep)


class History:
    """Fake git: an ancestry-path list and a (commit, path) lookup costing one second."""

    def __init__(self, clock: Clock, length: int, landings: dict[str, int]) -> None:
        self.clock = clock
        self.commits = [_oid(index) for index in range(length)]
        self.landings = dict(landings)  # digest -> index of the one commit holding it
        self.lookups: list[tuple[str, str]] = []
        self.available = True

    def descendant_commits(self, _repo, base_oid: str) -> list[str] | None:
        if not self.available:
            return None
        assert base_oid == BASE
        return list(self.commits)

    def commit_path_sha256(self, _repo, commit: str, path: str) -> str | None:
        self.lookups.append((commit, path))
        self.clock.now += 1.0
        index = self.commits.index(commit)
        for digest, landed in self.landings.items():
            if landed == index:
                return digest
        return f"other-{index}"

    def is_ancestor_of_head(self, _repo, commit: str) -> bool:
        return commit in self.commits


class DictStore:
    """In-memory ``HoldingWalkStore`` that keeps every write it was asked for."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], tuple[str, str]] = {}
        self.writes: list[tuple[tuple[str, str, str], str, str]] = []

    def read_walk(self, key):
        return self.rows.get(key)

    def record_walk(self, key, kind, commit):
        self.rows[key] = (kind, commit)
        self.writes.append((key, kind, commit))


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(sdlc_attribution, "time", fake.namespace())
    return fake


def _install(monkeypatch, history: History) -> None:
    monkeypatch.setattr(sdlc_attribution, "descendant_commits", history.descendant_commits)
    monkeypatch.setattr(sdlc_attribution, "commit_path_sha256", history.commit_path_sha256)
    monkeypatch.setattr(
        sdlc_attribution, "commit_is_ancestor_of_head", history.is_ancestor_of_head,
    )


def _card(task_id: str, digest: str) -> sdlc_attribution.AcceptedCard:
    return sdlc_attribution.AcceptedCard(
        task_id=task_id,
        request_id=f"r-{task_id}",
        base_oid=BASE,
        promoted_paths=(PATH,),
        changed_path_hashes={PATH: digest},
        identity={"task_id": task_id, "request_id": f"r-{task_id}"},
    )


def _walk_key(card: sdlc_attribution.AcceptedCard) -> tuple[str, str, str]:
    return (card.task_id, card.request_id, card.base_oid)


def _unbounded(card: sdlc_attribution.AcceptedCard) -> tuple[str | None, str]:
    return sdlc_attribution.first_holding_commit(
        Path("unused"), card.base_oid, card.promoted_paths, card.changed_path_hashes,
    )


# --------------------------------------------------------------------------
# BOUND: the walk stops within one lookup of the deadline
# --------------------------------------------------------------------------

def test_a_walk_past_its_deadline_raises_within_one_lookup(monkeypatch, clock):
    history = History(clock, HISTORY, {"h-late": HISTORY - 1})
    _install(monkeypatch, history)
    deadline = clock.now + 49.5

    with pytest.raises(sdlc_attribution.BudgetExceeded):
        sdlc_attribution.first_holding_commit(
            Path("unused"), BASE, (PATH,), {PATH: "h-late"}, deadline=deadline,
        )

    assert len(history.lookups) == 50  # not every one of the 250 descendants
    assert clock.now - deadline <= 1.0  # at most one lookup's cost past the deadline

    history.lookups.clear()
    assert sdlc_attribution.first_holding_commit(
        Path("unused"), BASE, (PATH,), {PATH: "h-late"},
    ) == (_oid(HISTORY - 1), "")
    assert len(history.lookups) == HISTORY  # deadline=None is the unbounded walk


def test_attribute_needfix_passes_its_deadline_to_every_walk(monkeypatch, clock):
    history = History(clock, HISTORY, {"h-fix": 10, "h-cause": 3})
    _install(monkeypatch, history)
    fix, cause = _card("fix-1", "h-fix"), _card("cause-1", "h-cause")
    seen: list[tuple[str, str, float | None]] = []
    holding = sdlc_attribution.AcceptedCatalog.holding_commit
    explaining = sdlc_attribution.AcceptedCatalog.explaining_card

    def spy_holding(self, card, *, deadline=None):
        seen.append(("holding_commit", card.task_id, deadline))
        return holding(self, card, deadline=deadline)

    def spy_explaining(self, path, commit, *, exclude, deadline=None):
        seen.append(("explaining_card", path, deadline))
        return explaining(self, path, commit, exclude=exclude, deadline=deadline)

    monkeypatch.setattr(sdlc_attribution.AcceptedCatalog, "holding_commit", spy_holding)
    monkeypatch.setattr(sdlc_attribution.AcceptedCatalog, "explaining_card", spy_explaining)
    monkeypatch.setattr(sdlc_attribution, "deleted_preimage_ranges", lambda *_a: [(1, 2)])
    monkeypatch.setattr(sdlc_attribution, "blamed_commits", lambda *_a: [_oid(3), _oid(3)])
    monkeypatch.setattr(
        sdlc_attribution.needfix_store, "get_needfix",
        lambda _repo, _needfix_id: {"converted_task_id": "fix-1"},
    )
    deadline = clock.now + 1e6

    outcome = sdlc_attribution.attribute_needfix(
        "unused", REPOSITORY_ID, "NF-1",
        catalog=sdlc_attribution.AcceptedCatalog(Path("unused"), [fix, cause]),
        write=False, deadline=deadline,
    )

    assert outcome.attributed is True and outcome.fix_commit == _oid(10)
    assert {name for name, _, _ in seen} == {"holding_commit", "explaining_card"}
    assert ("holding_commit", "cause-1", deadline) in seen
    assert all(passed == deadline for _, _, passed in seen)

    unbudgeted = sdlc_attribution.attribute_needfix(
        "unused", REPOSITORY_ID, "NF-1",
        catalog=sdlc_attribution.AcceptedCatalog(Path("unused"), [fix, cause]),
        write=False,
    )
    assert unbudgeted == outcome


def test_a_walk_inside_explaining_card_is_bounded_and_nothing_is_written(monkeypatch, clock):
    # The fix lands early; the cause card's own walk is the long one.
    history = History(clock, HISTORY, {"h-fix": 3, "h-cause": 200})
    _install(monkeypatch, history)
    catalog = sdlc_attribution.AcceptedCatalog(
        Path("unused"), [_card("fix-1", "h-fix"), _card("cause-1", "h-cause")],
    )
    writes: list[object] = []
    monkeypatch.setattr(sdlc_attribution, "deleted_preimage_ranges", lambda *_a: [(1, 1)])
    monkeypatch.setattr(sdlc_attribution, "blamed_commits", lambda *_a: [_oid(200)])
    monkeypatch.setattr(
        sdlc_attribution.needfix_store, "get_needfix",
        lambda _repo, _needfix_id: {"converted_task_id": "fix-1"},
    )
    monkeypatch.setattr(
        sdlc_attribution.needfix_store, "update_needfix",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    deadline = clock.now + 50.5

    with pytest.raises(sdlc_attribution.BudgetExceeded):
        sdlc_attribution.attribute_needfix(
            "unused", REPOSITORY_ID, "NF-1", catalog=catalog, deadline=deadline,
        )

    assert clock.now - deadline <= 1.0
    assert len(history.lookups) < HISTORY
    assert writes == []
    assert ("cause-1", "r-cause-1") not in catalog._holding  # the aborted walk is no result


# --------------------------------------------------------------------------
# PROGRESS: an aborted walk resumes after the last fully walked commit
# --------------------------------------------------------------------------

def test_an_aborted_walk_resumes_next_pass_without_repeating_a_lookup(monkeypatch, clock):
    history = History(clock, HISTORY, {"h-fix": 180})
    _install(monkeypatch, history)
    card = _card("T-FIX", "h-fix")
    store = DictStore()
    first = sdlc_attribution.AcceptedCatalog(Path("unused"), [card], walk_store=store)

    with pytest.raises(sdlc_attribution.BudgetExceeded):
        first.holding_commit(card, deadline=clock.now + 99.5)

    first_lookups = list(history.lookups)
    assert len(first_lookups) == 100
    assert card.key not in first._holding  # never memoized as a result
    assert store.rows[_walk_key(card)] == (sdlc_attribution.WALK_THROUGH, first_lookups[-1][0])

    second = sdlc_attribution.AcceptedCatalog(Path("unused"), [card], walk_store=store)
    answer = second.holding_commit(card, deadline=clock.now + 1e6)

    second_lookups = history.lookups[len(first_lookups):]
    assert not set(first_lookups) & set(second_lookups)  # no (commit, path) twice
    assert len(first_lookups) + len(second_lookups) == 181
    assert store.rows[_walk_key(card)] == (sdlc_attribution.WALK_FOUND, _oid(180))

    history.lookups.clear()
    assert answer == _unbounded(card) == (_oid(180), "")

    # A recorded found commit still on HEAD answers without any walk at all.
    history.lookups.clear()
    third = sdlc_attribution.AcceptedCatalog(Path("unused"), [card], walk_store=store)
    assert third.holding_commit(card) == answer
    assert history.lookups == []


def test_a_found_commit_no_longer_on_head_is_recomputed_from_base(monkeypatch, clock):
    history = History(clock, HISTORY, {"h-fix": 180})
    _install(monkeypatch, history)
    card = _card("T-FIX", "h-fix")
    store = DictStore()
    sdlc_attribution.AcceptedCatalog(Path("unused"), [card], walk_store=store).holding_commit(card)
    assert store.rows[_walk_key(card)] == (sdlc_attribution.WALK_FOUND, _oid(180))

    history.commits = [_rewritten_oid(index) for index in range(HISTORY)]
    history.lookups.clear()

    answer = sdlc_attribution.AcceptedCatalog(
        Path("unused"), [card], walk_store=store,
    ).holding_commit(card)

    assert answer == (_rewritten_oid(180), "")
    assert len(history.lookups) == 181  # restarted from base_oid
    assert store.rows[_walk_key(card)] == (sdlc_attribution.WALK_FOUND, _rewritten_oid(180))
    history.lookups.clear()
    assert answer == _unbounded(card)


def test_a_hash_mismatch_resumes_only_commits_added_after_it(monkeypatch, clock):
    history = History(clock, HISTORY, {})
    _install(monkeypatch, history)
    card = _card("T-LATE", "h-late")
    store = DictStore()

    def walk() -> tuple[str | None, str]:
        catalog = sdlc_attribution.AcceptedCatalog(Path("unused"), [card], walk_store=store)
        return catalog.holding_commit(card)

    assert walk() == (None, sdlc_attribution.HASH_MISMATCH)
    assert len(history.lookups) == HISTORY
    assert store.rows[_walk_key(card)] == (sdlc_attribution.WALK_THROUGH, _oid(HISTORY - 1))

    history.lookups.clear()
    history.commits += [_oid(index) for index in range(HISTORY, HISTORY + 3)]
    assert walk() == (None, sdlc_attribution.HASH_MISMATCH)
    assert history.lookups == [(commit, PATH) for commit in history.commits[-3:]]
    assert store.rows[_walk_key(card)] == (sdlc_attribution.WALK_THROUGH, _oid(HISTORY + 2))

    history.lookups.clear()
    history.landings["h-late"] = HISTORY + 4
    history.commits += [_oid(HISTORY + 3), _oid(HISTORY + 4)]
    assert walk() == (_oid(HISTORY + 4), "")
    assert history.lookups == [(_oid(HISTORY + 3), PATH), (_oid(HISTORY + 4), PATH)]
    history.lookups.clear()
    assert _unbounded(card) == (_oid(HISTORY + 4), "")


def test_a_recorded_commit_no_longer_on_head_restarts_from_base(monkeypatch, clock):
    history = History(clock, HISTORY, {"h-fix": 120})
    _install(monkeypatch, history)
    card = _card("T-FIX", "h-fix")
    store = DictStore()
    store.rows[_walk_key(card)] = (sdlc_attribution.WALK_THROUGH, "d" * 40)

    answer = sdlc_attribution.AcceptedCatalog(
        Path("unused"), [card], walk_store=store,
    ).holding_commit(card)

    assert answer == (_oid(120), "")
    assert len(history.lookups) == 121  # from base_oid, nothing skipped


def test_git_unavailable_is_never_persisted(monkeypatch, clock):
    history = History(clock, HISTORY, {"h-fix": 120})
    history.available = False
    _install(monkeypatch, history)
    card = _card("T-FIX", "h-fix")
    store = DictStore()

    answer = sdlc_attribution.AcceptedCatalog(
        Path("unused"), [card], walk_store=store,
    ).holding_commit(card, deadline=clock.now + 1e6)
    assert answer == (None, sdlc_attribution.GIT_UNAVAILABLE)
    assert store.writes == []

    store.rows[_walk_key(card)] = (sdlc_attribution.WALK_THROUGH, _oid(40))
    again = sdlc_attribution.AcceptedCatalog(
        Path("unused"), [card], walk_store=store,
    ).holding_commit(card)
    assert again == (None, sdlc_attribution.GIT_UNAVAILABLE)
    assert store.writes == []
    assert store.rows[_walk_key(card)] == (sdlc_attribution.WALK_THROUGH, _oid(40))


# --------------------------------------------------------------------------
# real git and the real state-DB store
# --------------------------------------------------------------------------

def _git_repo(tmp_path: Path, versions: int) -> tuple[Path, str, list[str]]:
    repo_dir = tmp_path / "git"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.email", "fixture@example.com")
    _git(repo_dir, "config", "user.name", "Fixture")
    _git(repo_dir, "config", "commit.gpgsign", "false")
    _git(repo_dir, "config", "core.autocrlf", "false")
    base_oid = _commit(repo_dir, {PATH: "base\n"}, "base")
    commits = [_commit(repo_dir, {PATH: f"v{n}\n"}, f"v{n}") for n in range(versions)]
    return repo_dir, base_oid, commits


def test_a_real_walk_resumes_from_the_state_db_store(tmp_path, monkeypatch, clock):
    repo_dir, base_oid, commits = _git_repo(tmp_path, 12)
    card = sdlc_attribution.AcceptedCard(
        task_id="T-REAL", request_id="r-real", base_oid=base_oid,
        promoted_paths=(PATH,), changed_path_hashes={PATH: _sha("v9\n")},
        identity={"task_id": "T-REAL"},
    )
    lookups: list[tuple[str, str]] = []
    real_lookup = sdlc_attribution.commit_path_sha256

    def counting(repo_path, commit, path):
        lookups.append((commit, path))
        clock.now += 1.0
        return real_lookup(repo_path, commit, path)

    monkeypatch.setattr(sdlc_attribution, "commit_path_sha256", counting)
    store = sdlc_sync.HoldingWalkStore(tmp_path / "state" / "sdlc_sync.sqlite")

    with pytest.raises(sdlc_attribution.BudgetExceeded):
        sdlc_attribution.AcceptedCatalog(repo_dir, [card], walk_store=store).holding_commit(
            card, deadline=clock.now + 4.5,
        )
    assert store.read_walk(_walk_key(card)) == (sdlc_attribution.WALK_THROUGH, commits[4])
    first = list(lookups)

    answer = sdlc_attribution.AcceptedCatalog(repo_dir, [card], walk_store=store).holding_commit(card)

    assert answer == (commits[9], "")
    assert not set(first) & set(lookups[len(first):])
    assert len(lookups) == 10
    assert store.read_walk(_walk_key(card)) == (sdlc_attribution.WALK_FOUND, commits[9])
    assert answer == sdlc_attribution.first_holding_commit(
        repo_dir, base_oid, card.promoted_paths, card.changed_path_hashes,
    )

    # A found record re-checked by git merge-base answers with no tree lookup.
    lookups.clear()
    assert sdlc_attribution.AcceptedCatalog(
        repo_dir, [card], walk_store=store,
    ).holding_commit(card) == answer
    assert lookups == []


def test_commit_is_ancestor_of_head_asks_git(tmp_path):
    repo_dir, base_oid, commits = _git_repo(tmp_path, 2)
    _git(repo_dir, "checkout", "-q", "-b", "side")
    side = _commit(repo_dir, {PATH: "side\n"}, "side")
    _git(repo_dir, "checkout", "-q", "-")

    assert sdlc_attribution.commit_is_ancestor_of_head(repo_dir, base_oid) is True
    assert sdlc_attribution.commit_is_ancestor_of_head(repo_dir, commits[-1]) is True
    assert sdlc_attribution.commit_is_ancestor_of_head(repo_dir, side) is False
    assert sdlc_attribution.commit_is_ancestor_of_head(repo_dir, "--all") is False


# --------------------------------------------------------------------------
# sync_once over a real state DB converges under a tiny budget
# --------------------------------------------------------------------------

def _holding_rows(root: Path) -> list[tuple[str, str, str]]:
    path = root.joinpath(*sdlc_sync.STATE_DB_REL)
    conn = sqlite3.connect(str(path))
    try:
        try:
            return [
                (str(task), str(kind), str(commit))
                for task, kind, commit in conn.execute(
                    "SELECT task_id, kind, commit_oid FROM holding_walk ORDER BY task_id"
                )
            ]
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()


def test_sync_passes_with_a_tiny_budget_make_strict_progress_and_converge(repo, monkeypatch):
    _accept_fixes(repo.root)
    clock = Clock()
    monkeypatch.setattr(sdlc_sync, "time", clock.namespace())
    monkeypatch.setattr(sdlc_attribution, "time", clock.namespace())
    landings = {"h-cause": 2, "h-fix-0": 40, "h-fix-1": 45, "h-fix-2": 50}
    history = History(clock, 60, landings)
    _install(monkeypatch, history)
    cards = [_card(task_id, f"h-fix-{n}") for n, task_id in enumerate(FIX_TASKS)]
    cards.append(_card("T-CAUSE", "h-cause"))
    monkeypatch.setattr(
        sdlc_sync.sdlc_attribution, "load_accepted_catalog",
        lambda root, _repository_id, *, deadline=None: sdlc_attribution.AcceptedCatalog(
            Path(root), cards,
        ),
    )
    monkeypatch.setattr(sdlc_attribution, "deleted_preimage_ranges", lambda *_a: [(1, 1)])
    monkeypatch.setattr(sdlc_attribution, "blamed_commits", lambda *_a: [_oid(2)])
    monkeypatch.setattr(
        sdlc_attribution.needfix_store, "get_needfix",
        lambda _repo, needfix_id: {
            "converted_task_id": FIX_TASKS[NEEDFIX_IDS.index(needfix_id)],
        },
    )
    monkeypatch.setattr(
        sdlc_attribution.needfix_store, "update_needfix",
        lambda *args, **kwargs: {"update_receipt": {"fields_changed": ["caused_by"]}},
    )
    monkeypatch.setattr(sdlc_sync.sdlc_control_bands, "evaluate", lambda _root, _rid: {})
    monkeypatch.setattr(
        sdlc_sync.sdlc_control_bands, "file_breaches", lambda _root, _rid, _report: [],
    )

    attributed: dict[str, str] = {}
    snapshots: list[tuple[list[tuple[str, str, str]], set[str]]] = []
    states: list[str] = []
    for _pass in range(40):
        result = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=clock.now + 10.5)
        assert result["failures"] == []
        part = result["parts"]["attribution"]
        states.append(part["state"])
        attributed.update(part.get("attributed", {}))
        snapshot = (_holding_rows(repo.root), _pending(repo.root))
        # Strict progress: every pass leaves a recorded walk or the pending set changed.
        assert not snapshots or snapshot != snapshots[-1]
        snapshots.append(snapshot)
        if part["state"] == "ok":
            break

    assert states[0] == "deferred"
    assert states[-1] == "ok"
    assert set(states[:-1]) == {"deferred"}
    assert attributed == {needfix_id: "attributed" for needfix_id in NEEDFIX_IDS}
    assert _pending(repo.root) == set()

    # Every recorded holding commit equals the unbounded walk's answer.
    history.lookups.clear()
    recorded = {task: (kind, commit) for task, kind, commit in _holding_rows(repo.root)}
    for card in cards:
        assert recorded[card.task_id] == (sdlc_attribution.WALK_FOUND, _unbounded(card)[0])


def test_a_multi_path_walk_checks_the_deadline_before_every_lookup(monkeypatch, clock):
    history = History(clock, HISTORY, {})
    _install(monkeypatch, history)
    paths = tuple(f"src/pkg/p{n}.py" for n in range(5))

    with pytest.raises(sdlc_attribution.BudgetExceeded):
        sdlc_attribution.first_holding_commit(
            Path("unused"), BASE, paths, {path: "h-none" for path in paths},
            deadline=clock.now + 2.5,
        )

    assert len(history.lookups) == 3  # stopped inside the first commit's five lookups


def test_a_failing_walk_store_never_replaces_the_walk_outcome(monkeypatch, clock):
    history = History(clock, HISTORY, {"h-late": HISTORY - 1})
    _install(monkeypatch, history)

    def locked(_commit: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sdlc_attribution.BudgetExceeded):
        sdlc_attribution.first_holding_commit(
            Path("unused"), BASE, (PATH,), {PATH: "h-late"},
            deadline=clock.now + 9.5, on_walked=locked,
        )
    assert sdlc_attribution.first_holding_commit(
        Path("unused"), BASE, (PATH,), {PATH: "h-missing"}, on_walked=locked,
    ) == (None, sdlc_attribution.HASH_MISMATCH)


def test_an_unreadable_walk_store_reads_as_nothing_recorded(tmp_path):
    path = tmp_path / "sdlc_state.sqlite"
    path.write_bytes(b"not a database " * 64)
    assert sdlc_sync.HoldingWalkStore(path).read_walk(("t", "r", BASE)) is None
