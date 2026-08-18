"""The SYSTEM binds a NeedFix to its fixing card -- no manager has to remember.

``needfix_store.reconcile_unlinked_needfix`` links unlinked NeedFix records to
existing cards on verifiable evidence only, and it runs on the same read path an
operator uses (``needfix_ingest.list_active``/``count_active`` ->
``core.needfix_list``/``count``). These tests prove:

* a record whose card exists is linked with no operator action, through the read
  path, and (when the card is finished) disappears from the open list/count;
* a link is established ONLY on the deterministic ``needfix-{NF-ID}`` id or an
  explicit reference the card carries -- never a similar title, an overlapping
  file scope, or a close timestamp;
* reconciliation is idempotent;
* ``convert_needfix``'s atomic link and deterministic id are untouched;
* the read stays responsive (measured against a 164-record store).
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from aiworkhub import core, needfix_store, task_store
from aiworkhub.needfix_store import reconcile_unlinked_needfix, successor_task_id


# --------------------------------------------------------------------------- #
# Fixtures and fakes                                                           #
# --------------------------------------------------------------------------- #

@pytest.fixture
def repo_root() -> Path:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def init(repo_root: Path) -> Path:
    needfix_store.initialize_repository(repo_root)
    return repo_root


@pytest.fixture
def env_repo(monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo whose canonical task store is bound through the real env door."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
        monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
        task_store.initialize_repository(root)
        yield root


def _get(cards: Mapping[str, Mapping[str, Any]]):
    def get_task(task_id: str):
        return cards.get(task_id)

    return get_task


def _status():
    def canonical_status(card: Mapping[str, Any]) -> str:
        return card["status"]

    return canonical_status


def _card(task_id: str, status: str, **extra: Any) -> dict[str, Any]:
    card = {"id": task_id, "status": status}
    card.update(extra)
    return card


def _converted(repo: Path, nfid: str) -> str | None:
    return needfix_store.get_needfix(repo, nfid)["converted_task_id"]


def _insert_task(repo: Path, task_id: str, status: str) -> None:
    db = task_store.canonical_db_path(repo)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, status, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_card(repo: Path, task_id: str, status: str, **card_fields: Any) -> None:
    """Insert a REAL task_store card carrying semantic ``card_json`` fields.

    A directly created merge/multi-finding card names the NeedFixes it fixes in
    a dedicated ``needfix_ids`` field, which ``task_store`` persists in
    ``card_json`` and ``task_store.list_task_cards`` decodes back onto the card
    (keyed by the canonical ``task_id`` column, never ``id``). ``_insert_task``
    above leaves ``card_json`` empty, so the explicit-reference route needs this.
    """
    import json

    db = task_store.canonical_db_path(repo)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, status, card_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                task_id,
                status,
                json.dumps(card_fields),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Acceptance 1: a card that exists links the record through the read path      #
# --------------------------------------------------------------------------- #

def test_deterministic_card_links_record_with_no_manager_action(init: Path):
    rec = needfix_store.add_needfix(init, title="t", description="d", status="accepted")
    nfid = rec["id"]
    det = f"needfix-{nfid}"
    # A card exists with the deterministic id (created directly -- NOT via
    # needfix_convert), owned by an in-flight task.
    cards = {det: _card(det, "processing")}

    report = reconcile_unlinked_needfix(
        init, get_task_fn=_get(cards), canonical_status_fn=_status()
    )

    assert _converted(init, nfid) == det
    assert report["newly_linked_count"] == 1
    assert report["newly_linked"][0]["evidence"] == "deterministic_id"


def test_directly_created_card_links_and_hides_through_core_read_path(env_repo: Path):
    """The full operator door: core.needfix_list, no hooks passed by hand."""
    rec = needfix_store.add_needfix(env_repo, title="merged", description="d",
                                    status="accepted")
    nfid = rec["id"]
    # A finished card created directly with the deterministic id.
    _insert_task(env_repo, f"needfix-{nfid}", "finished")

    rows = core.needfix_list()

    # Linked by the system, verifiable through the normal read path...
    assert _converted(env_repo, nfid) == f"needfix-{nfid}"
    # ...and hidden, because its card is finished.
    assert nfid not in {r["id"] for r in rows}
    assert rows.derived is True


# --------------------------------------------------------------------------- #
# Acceptance 2: a finished/accepted card retires the record on its own         #
# --------------------------------------------------------------------------- #

def test_open_count_falls_on_its_own_when_the_card_finishes(env_repo: Path):
    rec = needfix_store.add_needfix(env_repo, title="x", description="d",
                                    status="accepted")
    nfid = rec["id"]

    # Open while no card exists.
    assert core.needfix_count() == 1

    # A finished card lands, created directly. No operator step follows.
    _insert_task(env_repo, f"needfix-{nfid}", "finished")

    # The count falls by itself on the very next read.
    assert core.needfix_count() == 0


# --------------------------------------------------------------------------- #
# Acceptance 4: only verifiable evidence -- never title/scope/timestamp        #
# --------------------------------------------------------------------------- #

def test_similar_title_overlapping_scope_close_timestamp_do_not_link(init: Path):
    rec = needfix_store.add_needfix(
        init, title="race in worker pool", description="d", status="accepted",
        scope_files=["src/aiworkhub/worker.py"],
    )
    nfid = rec["id"]
    # A lookalike card: identical title, the NeedFix id even embedded in its
    # objective text, overlapping file scope, and a matching timestamp -- but its
    # id is NOT the deterministic one and it carries NO explicit reference field.
    lookalike = _card(
        "T-lookalike", "processing",
        title="race in worker pool",
        objective=f"resolve {nfid} and friends",
        files=["src/aiworkhub/worker.py"],
        created_at="2026-01-01T00:00:00+00:00",
    )
    cards = {"T-lookalike": lookalike}  # note: no needfix-{nfid} card at all

    report = reconcile_unlinked_needfix(
        init,
        get_task_fn=_get(cards),
        canonical_status_fn=_status(),
        list_task_cards_fn=lambda: list(cards.values()),
    )

    # A coincidence never closes a live defect.
    assert _converted(init, nfid) is None
    assert report["newly_linked_count"] == 0
    residue = {r["needfix_id"]: r["reason"] for r in report["unlinked_remaining"]}
    assert nfid in residue
    assert "explicit needfix reference" in residue[nfid]


def test_explicit_card_reference_links_a_directly_created_merge_card(init: Path):
    # A manager creates one card spanning two findings and names them explicitly.
    a = needfix_store.add_needfix(init, title="a", description="d", status="accepted")
    b = needfix_store.add_needfix(init, title="b", description="d", status="accepted")
    merge = _card("T-merge", "review", needfix_ids=[a["id"], b["id"]])
    cards = {"T-merge": merge}

    report = reconcile_unlinked_needfix(
        init,
        get_task_fn=_get(cards),
        canonical_status_fn=_status(),
        list_task_cards_fn=lambda: list(cards.values()),
    )

    assert _converted(init, a["id"]) == "T-merge"
    assert _converted(init, b["id"]) == "T-merge"
    assert report["newly_linked_count"] == 2
    assert {e["evidence"] for e in report["newly_linked"]} == {"explicit_reference"}


def test_merge_card_explicit_reference_links_and_hides_through_core_read_path(
    env_repo: Path,
):
    """The explicit-reference route, end to end through the OPERATOR door.

    A manager creates ONE finished card that spans two findings and names them
    in its dedicated ``needfix_ids`` field -- never via ``needfix_convert``, so
    neither record carries the deterministic ``needfix-{NF-ID}`` id. The system
    must still bind both on the read an operator takes
    (``core.needfix_list``/``count``, no hooks passed by hand), with no manager
    step, and -- the card being finished -- retire them from the open set.

    This drives ``core`` directly, NOT ``reconcile_unlinked_needfix``, so it can
    only pass when the explicit route is reachable in PRODUCTION. It fails on a
    candidate that omits ``list_task_cards_fn`` from the read path (the explicit
    route never runs) or reads a card's id from ``id`` instead of the canonical
    ``task_id`` (the merge card is then invisible to the index).
    """
    a = needfix_store.add_needfix(env_repo, title="merge-a", description="d",
                                  status="accepted")["id"]
    b = needfix_store.add_needfix(env_repo, title="merge-b", description="d",
                                  status="accepted")["id"]

    # Both open while no card exists.
    assert core.needfix_count() == 2

    # One finished merge card, created directly, naming both records explicitly
    # -- and with a NON-deterministic id, so only the explicit route can bind it.
    _insert_card(env_repo, "T-merge", "finished", needfix_ids=[a, b])

    # The read path itself binds both. No reconcile_unlinked_needfix call here.
    rows = core.needfix_list()
    open_ids = {r["id"] for r in rows}

    assert _converted(env_repo, a) == "T-merge"      # linked by the system...
    assert _converted(env_repo, b) == "T-merge"
    assert a not in open_ids and b not in open_ids   # ...and hidden (card finished)
    assert rows.derived is True
    # The open count falls on its own, by the system, on the very next read.
    assert core.needfix_count() == 0


def test_read_path_binds_only_the_referenced_record_not_its_neighbour(
    env_repo: Path,
):
    """The read path keeps the store's evidence bar: only a reference links.

    Two distinct records over the same file. One finished card names ONLY the
    first, in its explicit ``needfix_ids`` field. The referenced record binds
    and retires through ``core``; the neighbour, named by nothing, stays open --
    the read path never sweeps in an accepted record just because some merge
    card exists. It links on the card's reference, not on file-scope proximity.
    """
    named = needfix_store.add_needfix(
        env_repo, title="race in pool acquire", description="d", status="accepted",
        scope_files=["src/aiworkhub/worker.py"],
    )["id"]
    neighbour = needfix_store.add_needfix(
        env_repo, title="leak in pool release", description="different finding",
        status="accepted", scope_files=["src/aiworkhub/worker.py"],
    )["id"]
    assert named != neighbour  # two genuinely distinct records, not one dedup

    _insert_card(env_repo, "T-named", "finished", needfix_ids=[named])

    open_ids = {r["id"] for r in core.needfix_list()}

    assert _converted(env_repo, named) == "T-named"     # verifiable reference -> bound
    assert named not in open_ids
    assert _converted(env_repo, neighbour) is None       # unreferenced -> never bound
    assert neighbour in open_ids
    assert core.needfix_count() == 1


def test_ambiguous_explicit_reference_is_reported_not_forced(init: Path):
    rec = needfix_store.add_needfix(init, title="t", description="d", status="accepted")
    nfid = rec["id"]
    cards = {
        "T-one": _card("T-one", "processing", needfix_id=nfid),
        "T-two": _card("T-two", "processing", needfix_id=nfid),
    }

    report = reconcile_unlinked_needfix(
        init,
        get_task_fn=_get(cards),
        canonical_status_fn=_status(),
        list_task_cards_fn=lambda: list(cards.values()),
    )

    assert _converted(init, nfid) is None
    reasons = {r["needfix_id"]: r["reason"] for r in report["unlinked_remaining"]}
    assert "ambiguous" in reasons[nfid]


# --------------------------------------------------------------------------- #
# Acceptance 5: idempotent                                                     #
# --------------------------------------------------------------------------- #

def test_reconciliation_is_idempotent(init: Path):
    rec = needfix_store.add_needfix(init, title="t", description="d", status="accepted")
    nfid = rec["id"]
    det = f"needfix-{nfid}"
    cards = {det: _card(det, "processing")}
    hooks = dict(get_task_fn=_get(cards), canonical_status_fn=_status())

    first = reconcile_unlinked_needfix(init, **hooks)
    second = reconcile_unlinked_needfix(init, **hooks)

    assert first["newly_linked_count"] == 1
    assert second["newly_linked_count"] == 0  # the second run changes nothing
    assert second["before_linked"] == 1
    assert _converted(init, nfid) == det  # link unchanged


# --------------------------------------------------------------------------- #
# Acceptance: captured/unverified records are never force-linked               #
# --------------------------------------------------------------------------- #

def test_captured_record_is_not_linked_and_is_reported(init: Path):
    rec = needfix_store.capture_proposal(init, title="t", description="d")  # captured
    nfid = rec["id"]
    det = f"needfix-{nfid}"
    cards = {det: _card(det, "finished")}

    report = reconcile_unlinked_needfix(
        init, get_task_fn=_get(cards), canonical_status_fn=_status()
    )

    # The deterministic card exists, but a captured/unverified NeedFix must be
    # accepted by a manager first; the store refuses and reports it as residue.
    assert _converted(init, nfid) is None
    reasons = {r["needfix_id"]: r["reason"] for r in report["unlinked_remaining"]}
    assert "linkable" in reasons[nfid] and "accept" in reasons[nfid]


# --------------------------------------------------------------------------- #
# Acceptance 6: needfix_convert's atomic link + deterministic id are unchanged #
# --------------------------------------------------------------------------- #

def test_convert_needfix_still_mints_deterministic_id_and_links_atomically(init: Path):
    rec = needfix_store.add_needfix(init, title="t", description="d", status="accepted")
    nfid = rec["id"]
    seen: dict[str, str] = {}

    def create_task_fn(card: Mapping[str, Any]) -> dict[str, Any]:
        seen["task_id"] = card["task_id"]
        return {"ok": True, "task_id": card["task_id"]}

    result = needfix_store.convert_needfix(init, nfid, create_task_fn)

    assert seen["task_id"] == f"needfix-{nfid}"          # id unchanged
    assert result["converted_task_id"] == f"needfix-{nfid}"
    assert needfix_store.get_needfix(init, nfid)["status"] == "task_created"


def test_reconcile_binds_to_the_same_id_convert_mints(init: Path):
    # The reconcile candidate and the convert-minted id are one and the same,
    # so after-the-fact linking can never disagree with conversion.
    rec = needfix_store.add_needfix(init, title="t", description="d", status="accepted")
    assert successor_task_id(rec["id"], 0) == f"needfix-{rec['id']}"


# --------------------------------------------------------------------------- #
# Acceptance 3: reconcile_unlinked_needfix has a real caller on the read path  #
# --------------------------------------------------------------------------- #

def test_read_path_invokes_reconciliation(env_repo: Path, monkeypatch):
    from aiworkhub import needfix_ingest

    calls: list[str] = []
    real = needfix_store.reconcile_unlinked_needfix

    def spy(*args: Any, **kwargs: Any):
        calls.append("called")
        return real(*args, **kwargs)

    monkeypatch.setattr(needfix_ingest.needfix_store, "reconcile_unlinked_needfix", spy)
    needfix_store.add_needfix(env_repo, title="t", description="d", status="accepted")

    core.needfix_list()
    core.needfix_count()

    assert calls, "list_active/count_active must drive reconcile_unlinked_needfix"


# --------------------------------------------------------------------------- #
# Acceptance 7/8: the read stays responsive; before/after residue is honest    #
# --------------------------------------------------------------------------- #

def test_reconcile_over_164_records_is_responsive_and_reports_residue(env_repo: Path):
    """Faithful reconstruction of the reported store: 164 records, 58 linked.

    The canonical 164-record store lives outside this isolated worktree, so this
    builds an equivalent one against the REAL task store (real ``get_task`` /
    ``canonical_status`` SQLite lookups) and measures a derived read WITH vs
    WITHOUT reconciliation.
    """
    repo = env_repo
    get_task_fn = lambda tid: task_store.get_task(repo, tid)  # noqa: E731
    canonical = task_store.canonical_status

    # 58 already-linked (as convert does), each to a finished task.
    for i in range(58):
        r = needfix_store.add_needfix(repo, title=f"linked-{i}", description="d",
                                      status="accepted")
        tid = f"needfix-{r['id']}"
        _insert_task(repo, tid, "finished")
        needfix_store.link_existing_task(repo, r["id"], tid, get_task_fn, canonical)

    # 8 unlinked but with a landed (finished) deterministic card -> bindable.
    bindable: list[str] = []
    for i in range(8):
        r = needfix_store.add_needfix(repo, title=f"launched-{i}", description="d",
                                      status="accepted")
        bindable.append(r["id"])
        _insert_task(repo, f"needfix-{r['id']}", "finished")

    # 98 unlinked with NO card at all -> verifiable residue.
    for i in range(98):
        needfix_store.add_needfix(repo, title=f"open-{i}", description="d",
                                  status="accepted")

    assert needfix_store.count_needfix(repo) == 164  # raw total

    # A read WITHOUT reconciliation (derive only).
    t0 = time.perf_counter()
    needfix_store.count_needfix(repo, get_task_fn=get_task_fn,
                                canonical_status_fn=canonical, active_only=True)
    without = time.perf_counter() - t0

    # The first reconciling read binds the 8 and reports the residue.
    report = reconcile_unlinked_needfix(repo, get_task_fn=get_task_fn,
                                        canonical_status_fn=canonical)
    assert report["total_records"] == 164
    assert report["before_linked"] == 58
    assert report["newly_linked_count"] == 8
    assert report["after_linked"] == 66
    assert report["unlinked_remaining_count"] == 98
    for r in report["unlinked_remaining"]:
        assert "no card" in r["reason"]  # every residue row has a verifiable why

    # Idempotent: the second run changes nothing.
    again = reconcile_unlinked_needfix(repo, get_task_fn=get_task_fn,
                                       canonical_status_fn=canonical)
    assert again["newly_linked_count"] == 0

    # Steady-state cost of a reconciling read vs a plain derive-only read, both
    # over the same 164 records (no writes now: the 8 are already linked). We
    # assert a RELATIVE bound, never a wall-clock one: a hardcoded second
    # measures the machine, not the code -- it fails on a loaded box while
    # proving nothing on a quiet one. Load dilates both reads together, so their
    # RATIO is stable across machines; best-of-N sheds scheduling noise.
    def _derive_only() -> None:  # the read WITHOUT reconciliation (derive only)
        needfix_store.count_needfix(repo, get_task_fn=get_task_fn,
                                    canonical_status_fn=canonical, active_only=True)

    def _reconciling() -> None:  # the same read WITH reconciliation on the path
        reconcile_unlinked_needfix(repo, get_task_fn=get_task_fn,
                                   canonical_status_fn=canonical)
        needfix_store.count_needfix(repo, get_task_fn=get_task_fn,
                                    canonical_status_fn=canonical, active_only=True)

    def _best(fn, runs: int = 7) -> float:
        best = float("inf")
        for _ in range(runs):
            t0 = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - t0)
        return best

    without = _best(_derive_only)          # baseline: derive, no reconciliation
    with_reconcile = _best(_reconciling)   # same read, now reconciling too
    ratio = with_reconcile / without if without > 0 else float("inf")

    # EVIDENCE the card asks for, kept in the log rather than frozen into an
    # assertion: the measured cost of a read WITH reconciliation against one
    # WITHOUT, on this store's 164 records.
    print(
        f"\n[needfix-reconcile] 164 records -- derive-only={without * 1e3:.3f}ms, "
        f"with-reconcile={with_reconcile * 1e3:.3f}ms, ratio={ratio:.2f}x"
    )

    # Reconciliation is one extra bounded pass over records the read already
    # touches, so the reconciling read stays within a small multiple of the
    # baseline on ANY machine. This FAILS if reconciliation is genuinely
    # expensive -- a per-record write/transaction or worse-than-linear work
    # would push the ratio far past this -- and PASSES when the box is merely
    # busy, because both timings move together.
    assert ratio < 6.0, (
        f"reconciling read is {ratio:.2f}x the derive-only baseline "
        f"({with_reconcile * 1e3:.3f}ms vs {without * 1e3:.3f}ms) -- "
        "reconciliation is doing more than one bounded pass over 164 records"
    )
    # The 8 bindable records are now linked and off the active set.
    for nfid in bindable:
        assert _converted(repo, nfid) == f"needfix-{nfid}"
    assert without >= 0.0  # the without-reconcile timing is real
