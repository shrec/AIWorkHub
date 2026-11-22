"""NF-2026-02203: the attribution pass budget also bounds its two slow steps.

The reconciler reported ``stale: true`` with ``scan_phase=sdlc_sync:attribution``
aged 542.8 s although the pass already had a wall-clock budget: the budget was
only checked before ``load_accepted_catalog`` and between NeedFix ids, so one
slow catalog load or one slow ``attribute_needfix`` ran unbounded. The deadline
now reaches both. A pass whose budget expires inside either step defers, the
unprocessed ids stay pending (never done, never failed), and enough passes
attribute exactly what one unbudgeted pass would.
"""

from __future__ import annotations

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
    NEEDFIX_IDS,
    _accept_fixes,
    _pending,
)
from test_sdlc_sync import _fresh_git_facts, repo, spies  # noqa: E402,F401

from aiworkhub import sdlc_attribution, sdlc_outcome_metrics, sdlc_sync  # noqa: E402

REPOSITORY_ID = "repo-nf02203"
BASE = "a" * 40
FIX_COMMIT = "f" * 40
CAUSE_COMMIT = "c" * 40
BUDGET = 30.0
#: Checked every this many cards inside the catalog loop (see load_accepted_catalog).
CATALOG_CHECK_STRIDE = 256


class Clock:
    """Injected monotonic clock; only the code under test advances it."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def namespace(self) -> SimpleNamespace:
        return SimpleNamespace(monotonic=self.monotonic, time=time.time, sleep=time.sleep)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(sdlc_attribution, "time", fake.namespace())
    return fake


# --------------------------------------------------------------------------
# load_accepted_catalog stops at the deadline and never returns a partial index
# --------------------------------------------------------------------------

def _fake_store(monkeypatch, count: int, *, on_get_tasks=None, on_identity=None) -> list[str]:
    ids = [f"T-{n:05d}" for n in range(count)]
    store = sdlc_attribution.task_store
    monkeypatch.setattr(
        store, "latest_manager_decisions",
        lambda _repo: {task_id: {"decision": "accepted"} for task_id in ids},
    )
    monkeypatch.setattr(store, "list_task_ids_newest_first", lambda _repo, limit: list(ids))

    def get_tasks(_repo, wanted, projection=None):
        if on_get_tasks is not None:
            on_get_tasks()
        return {
            task_id: {
                "task_id": task_id,
                "accepted_request_id": f"r-{task_id}",
                "accept_evidence": {"accepted_outcome_receipt": {
                    "base_oid": BASE, "promoted_paths": ["src/a.py"],
                    "changed_path_hashes": {"src/a.py": task_id},
                }},
            }
            for task_id in wanted
        }

    def identity(event, _repository_id):
        if on_identity is not None:
            on_identity()
        payload = event["payload"]
        return {"task_id": event["task_id"], "request_id": payload["request_id"]}

    monkeypatch.setattr(store, "get_tasks", get_tasks)
    monkeypatch.setattr(sdlc_outcome_metrics, "accepted_outcome_identity", identity)
    return ids


def test_a_catalog_load_that_spends_the_budget_in_the_store_read_returns_none(
    monkeypatch, clock
):
    _fake_store(monkeypatch, 5, on_get_tasks=lambda: setattr(clock, "now", clock.now + 60.0))
    start = clock.now

    loaded = sdlc_attribution.load_accepted_catalog(
        "unused", REPOSITORY_ID, deadline=start + BUDGET,
    )

    assert loaded is None  # never a partial catalog
    assert clock.now - start == 60.0  # one slow read, nothing after it


def test_a_catalog_load_stops_inside_the_card_loop_within_one_stride(monkeypatch, clock):
    tick = lambda: setattr(clock, "now", clock.now + 1.0)  # noqa: E731 -- one second per card
    _fake_store(monkeypatch, 5 * CATALOG_CHECK_STRIDE, on_identity=tick)
    start = clock.now

    loaded = sdlc_attribution.load_accepted_catalog(
        "unused", REPOSITORY_ID, deadline=start + 2 * CATALOG_CHECK_STRIDE - 10,
    )

    assert loaded is None
    spent = clock.now - start
    assert spent <= (2 * CATALOG_CHECK_STRIDE - 10) + CATALOG_CHECK_STRIDE  # budget + one stride


def test_without_a_deadline_the_catalog_holds_every_accepted_card(monkeypatch, clock):
    ids = _fake_store(monkeypatch, 3 * CATALOG_CHECK_STRIDE)

    full = sdlc_attribution.load_accepted_catalog("unused", REPOSITORY_ID)
    generous = sdlc_attribution.load_accepted_catalog(
        "unused", REPOSITORY_ID, deadline=clock.now + 1e9,
    )

    assert full is not None and generous is not None
    assert [card.task_id for card in full.cards] == ids
    assert [card.key for card in generous.cards] == [card.key for card in full.cards]


# --------------------------------------------------------------------------
# attribute_needfix stops inside the slow git step and the catalog is shared
# --------------------------------------------------------------------------

def _catalog_with_fix_and_cause() -> sdlc_attribution.AcceptedCatalog:
    def card(task_id: str, digest: str) -> sdlc_attribution.AcceptedCard:
        return sdlc_attribution.AcceptedCard(
            task_id=task_id,
            request_id=f"r-{task_id}",
            base_oid=BASE,
            promoted_paths=("src/a.py",),
            changed_path_hashes={"src/a.py": digest},
            identity={"task_id": task_id, "request_id": f"r-{task_id}"},
        )

    return sdlc_attribution.AcceptedCatalog(
        Path("unused"), [card("fix-1", "h-fix"), card("cause-1", "h-cause")],
    )


@pytest.fixture
def git_spies(monkeypatch):
    spent = SimpleNamespace(
        holding=[], blame=0, writes=[], blame_cost=0.0, clock=None,
    )

    def first_holding_commit(
        _repo, _base, _paths, hashes, *, path_sha256,
        deadline=None, resume_after=None, on_walked=None,
    ):
        spent.holding.append(tuple(hashes.values()))
        return (FIX_COMMIT if "h-fix" in hashes.values() else CAUSE_COMMIT), ""

    def blamed_commits(_repo, _rev, _path, start, end):
        spent.blame += 1
        if spent.clock is not None:
            spent.clock.now += spent.blame_cost  # one slow git blame
        return [CAUSE_COMMIT] * (end - start + 1)

    monkeypatch.setattr(sdlc_attribution, "first_holding_commit", first_holding_commit)
    monkeypatch.setattr(sdlc_attribution, "commit_path_sha256", lambda *_a: "h-cause")
    monkeypatch.setattr(sdlc_attribution, "deleted_preimage_ranges", lambda *_a: [(1, 2), (5, 6)])
    monkeypatch.setattr(sdlc_attribution, "blamed_commits", blamed_commits)
    monkeypatch.setattr(
        sdlc_attribution.needfix_store, "get_needfix",
        lambda _repo, _needfix_id: {"converted_task_id": "fix-1"},
    )
    monkeypatch.setattr(
        sdlc_attribution.needfix_store, "update_needfix",
        lambda *args, **kwargs: spent.writes.append((args, kwargs)),
    )
    return spent


def test_a_slow_git_step_raises_budget_exceeded_and_writes_nothing(git_spies, clock):
    git_spies.clock, git_spies.blame_cost = clock, 60.0
    catalog = _catalog_with_fix_and_cause()
    start = clock.now

    with pytest.raises(sdlc_attribution.BudgetExceeded):
        sdlc_attribution.attribute_needfix(
            "unused", REPOSITORY_ID, "NF-1", catalog=catalog, deadline=start + BUDGET,
        )

    assert git_spies.blame == 1  # stopped after the slow call, before the next range
    assert clock.now - start == 60.0
    assert git_spies.writes == []  # nothing recorded: the id stays pending


def test_a_deadline_already_past_does_no_work_at_all(git_spies, clock):
    catalog = _catalog_with_fix_and_cause()

    with pytest.raises(sdlc_attribution.BudgetExceeded):
        sdlc_attribution.attribute_needfix(
            "unused", REPOSITORY_ID, "NF-1", catalog=catalog, deadline=clock.now - 1.0,
        )

    assert git_spies.blame == 0 and git_spies.holding == [] and git_spies.writes == []


def test_a_budgeted_run_with_room_equals_the_unbudgeted_run(git_spies, clock):
    unbudgeted = sdlc_attribution.attribute_needfix(
        "unused", REPOSITORY_ID, "NF-1", catalog=_catalog_with_fix_and_cause(), write=False,
    )
    budgeted = sdlc_attribution.attribute_needfix(
        "unused", REPOSITORY_ID, "NF-1", catalog=_catalog_with_fix_and_cause(),
        write=False, deadline=clock.now + 1e9,
    )

    assert unbudgeted.attributed is True
    assert budgeted == unbudgeted


def test_the_holding_commit_memo_is_shared_across_ids_in_one_catalog(git_spies, clock):
    catalog = _catalog_with_fix_and_cause()

    for needfix_id in ("NF-1", "NF-2", "NF-3"):
        outcome = sdlc_attribution.attribute_needfix(
            "unused", REPOSITORY_ID, needfix_id, catalog=catalog, write=False,
        )
        assert outcome.attributed is True

    # One history walk per card for the whole pass, not one per NeedFix.
    assert sorted(git_spies.holding) == [("h-cause",), ("h-fix",)]
    assert catalog.path_sha_hits > 0


# --------------------------------------------------------------------------
# the pass: deferral leaves ids pending, and enough passes equal one full pass
# --------------------------------------------------------------------------

def test_a_catalog_load_past_the_budget_defers_every_id_and_later_passes_complete(
    repo, spies, monkeypatch
):
    _accept_fixes(repo.root)
    clock = Clock()
    monkeypatch.setattr(sdlc_sync, "time", clock.namespace())
    loads: list[float | None] = []

    def slow_load(_root, _repository_id, *, deadline=None):
        loads.append(deadline)
        if len(loads) == 1:  # the first load spends the budget and gives up
            clock.now = deadline + 5.0
            return None
        return "catalog"

    attributed: list[str] = []

    def attribute(_root, _repository_id, needfix_id, *, catalog, deadline=None):
        attributed.append(needfix_id)
        return SimpleNamespace(attributed=True, reason="")

    monkeypatch.setattr(sdlc_sync.sdlc_attribution, "load_accepted_catalog", slow_load)
    monkeypatch.setattr(sdlc_sync.sdlc_attribution, "attribute_needfix", attribute)
    start = clock.now

    first = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=start + BUDGET)

    part = first["parts"]["attribution"]
    assert part["state"] == "deferred"
    assert part["reason"] == sdlc_sync.PASS_BUDGET_EXHAUSTED
    assert part["remaining"] == len(NEEDFIX_IDS)
    assert part["attributed"] == {}
    assert first["failures"] == []
    assert loads == [start + BUDGET]  # the same pass deadline reached the loader
    assert attributed == []
    assert _pending(repo.root) == set(NEEDFIX_IDS)  # none done, none failed
    assert clock.now - start <= BUDGET + 5.0

    second = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=clock.now + 1000.0)

    assert second["parts"]["attribution"]["state"] == "ok"
    assert attributed == NEEDFIX_IDS
    assert _pending(repo.root) == set()


def test_one_slow_attribute_defers_its_id_and_the_union_of_passes_equals_a_full_pass(
    repo, spies, monkeypatch
):
    _accept_fixes(repo.root)
    clock = Clock()
    monkeypatch.setattr(sdlc_sync, "time", clock.namespace())
    slow_id = NEEDFIX_IDS[1]
    slow_once = {"armed": True}
    deadlines: list[float | None] = []
    attributed_calls: list[str] = []

    def attribute(_root, _repository_id, needfix_id, *, catalog, deadline=None):
        deadlines.append(deadline)
        if needfix_id == slow_id and slow_once["armed"]:
            slow_once["armed"] = False
            clock.now = deadline + 3.0  # the git step spends the budget mid-call
            raise sdlc_attribution.BudgetExceeded(needfix_id)
        attributed_calls.append(needfix_id)
        return SimpleNamespace(attributed=True, reason="")

    monkeypatch.setattr(
        sdlc_sync.sdlc_attribution, "load_accepted_catalog",
        lambda _r, _i, *, deadline=None: "catalog",
    )
    monkeypatch.setattr(sdlc_sync.sdlc_attribution, "attribute_needfix", attribute)
    start = clock.now

    first = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=start + BUDGET)

    part = first["parts"]["attribution"]
    assert part["state"] == "deferred"
    assert part["reason"] == sdlc_sync.PASS_BUDGET_EXHAUSTED
    assert part["remaining"] == 2
    assert part["attributed"] == {NEEDFIX_IDS[0]: "attributed"}
    assert "failed" not in part and first["failures"] == []
    assert deadlines[0] == start + BUDGET  # attribute_needfix received the pass deadline
    assert _pending(repo.root) == set(NEEDFIX_IDS[1:])  # the slow id is pending, not failed
    assert clock.now - start <= BUDGET + 3.0

    second = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=clock.now + 1000.0)

    assert second["parts"]["attribution"]["state"] == "ok"
    assert second["parts"]["attribution"]["attributed"] == {
        needfix_id: "attributed" for needfix_id in NEEDFIX_IDS[1:]
    }
    # The attributed set across passes is exactly what an unbudgeted pass yields.
    assert sorted(attributed_calls) == NEEDFIX_IDS
    assert _pending(repo.root) == set()
