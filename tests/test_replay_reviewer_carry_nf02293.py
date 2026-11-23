"""NF-2026-02293: a byte-identical replay keeps its predecessor's verified reviewers.

A validation-only replay / rework of the SAME task whose candidate bytes AND
contract are identical to its authenticated predecessor must not lose every
verified quality-reviewer receipt: accept must not report
``required_reviewer_missing:<lens>`` and auto-seeding must not launch a fresh
reviewer for those lenses. Everything weaker -- one differing byte, another
task, an absent / malformed / caller-shaped link, an unknown or amended
contract, a lineage on another epoch, or a predecessor receipt that is not
sealed to that predecessor's request, epoch and candidate digest -- keeps
today's refusal.

The link is never trusted on its own: every carry is cross-checked against
the predecessor reviewer receipt's sealed binding (receipt target + the exact
packet it is bound to), which no worker authors.

No provider CLI, no live manager loop, no git.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import core
from aiworkhub import process_launcher_accept_review as par
from aiworkhub import quality_reviewer
from aiworkhub import review_orchestrator as ro
from aiworkhub import task_store

TASK = "T_REPLAY"
REQUEST = "REQ_REPLAY"
PREDECESSOR = "REQ_PREDECESSOR"
HASHES = {"src/a.py": "c" * 64, "tests/test_a.py": "d" * 64}
LENS = "correctness"
EPOCH = 2


# --- sealed evidence: the packet a reviewer read and its stored receipt -----


def _packet(
    request_id: str = PREDECESSOR,
    epoch: int = EPOCH,
    hashes: dict[str, str] | None = None,
    task_id: str = TASK,
) -> dict[str, Any]:
    body = {
        "schema_id": "aiworkhub.quality_review.packet.v1",
        "target": {
            "request_id": request_id, "task_id": task_id,
            "claim_epoch": epoch, "worker_provider": "codex_cli",
        },
        "candidate": {"changed_paths": [
            {"path": path, "sha256": digest}
            for path, digest in sorted((hashes or HASHES).items())
        ]},
    }
    return {**body, "packet_sha256": quality_reviewer._canonical_digest(body)}


def _receipt(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_id": quality_reviewer.RECEIPT_SCHEMA_ID,
        "packet_sha256": packet["packet_sha256"],
        "target": {
            key: packet["target"][key] for key in ("request_id", "task_id", "claim_epoch")
        },
        "report": {"lens": LENS, "findings": []},
    }


def _sealed(**packet_kwargs: Any) -> dict[str, Any]:
    packet = _packet(**packet_kwargs)
    return ro.sealed_reviewer_binding(_receipt(packet), packet)


def _card(**overrides: Any) -> dict[str, Any]:
    card: dict[str, Any] = {
        "task_id": TASK,
        "topic": "code",
        "objective": "carry verified reviewers across a byte-identical replay",
        "acceptance": ["no required_reviewer_missing on identical bytes"],
        "required_outputs": ["src/a.py"],
        "validation": ["python -m pytest tests/test_a.py -q"],
        "forbidden": [],
        "terminal_substatus": "review_ready",
        "terminal_review": {"evidence": {
            "request_identity": {"request_id": REQUEST, "task_id": TASK},
            "changed_path_hashes": dict(HASHES),
        }},
    }
    card["rework_predecessor"] = {
        "schema_id": ro.REWORK_PREDECESSOR_SCHEMA,
        "request_id": PREDECESSOR,
        "task_id": TASK,
        "claim_epoch": EPOCH,
        "changed_path_hashes": dict(HASHES),
        "contract_identity_sha256": ro.contract_identity_digest(card),
    }
    card.update(overrides)
    return card


def _lineage(**overrides: Any) -> dict[str, Any]:
    lineage = {
        "schema_id": ro.VALIDATION_REPLAY_LINEAGE_SCHEMA,
        "task_id": TASK,
        "predecessor_request_id": PREDECESSOR,
        "predecessor_claim_epoch": EPOCH,
        "changed_path_hashes": dict(HASHES),
        "contract_identity_sha256": ro.contract_identity_digest(_card()),
    }
    lineage.update(overrides)
    return lineage


# --- the sealed binding -------------------------------------------------------


def test_sealed_reviewer_binding_reads_target_epoch_and_candidate_digest():
    assert _sealed() == {
        "request_id": PREDECESSOR, "task_id": TASK, "claim_epoch": EPOCH,
        "candidate_sha256": ro.candidate_digest(HASHES),
    }


@pytest.mark.parametrize("case", [
    "packet_digest_differs", "packet_target_epoch_differs",
    "packet_target_request_differs", "epoch_is_bool", "no_candidate_rows",
    "duplicate_path", "no_packet",
])
def test_sealed_reviewer_binding_fails_closed(case):
    packet = _packet()
    receipt = _receipt(packet)
    if case == "packet_digest_differs":
        receipt["packet_sha256"] = "0" * 64
    elif case == "packet_target_epoch_differs":
        packet["target"]["claim_epoch"] = EPOCH + 1
    elif case == "packet_target_request_differs":
        packet["target"]["request_id"] = "REQ_OTHER"
    elif case == "epoch_is_bool":
        receipt["target"]["claim_epoch"] = True
        packet["target"]["claim_epoch"] = True
    elif case == "no_candidate_rows":
        packet["candidate"]["changed_paths"] = []
    elif case == "duplicate_path":
        packet["candidate"]["changed_paths"].append(
            dict(packet["candidate"]["changed_paths"][0])
        )
    elif case == "no_packet":
        packet = None
    assert ro.sealed_reviewer_binding(receipt, packet) == {}


def test_retained_sealed_binding_reads_the_packet_inside_the_reviewer_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    packet = _packet()
    packet_path = home / "packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    receipt = _receipt(packet)

    assert par.retained_sealed_binding(
        {"packet_path": str(packet_path)}, {"home": str(home)}, receipt
    ) == _sealed()
    # Outside the recorded home, or with no home at all: nothing is sealed.
    other = tmp_path / "other"
    other.mkdir()
    assert par.retained_sealed_binding(
        {"packet_path": str(packet_path)}, {"home": str(other)}, receipt
    ) == {}
    assert par.retained_sealed_binding(
        {"packet_path": str(packet_path)}, {}, receipt
    ) == {}


# --- the one predicate ------------------------------------------------------


def test_identical_bytes_predecessor_admits_exact_double_match():
    carried = ro.identical_bytes_predecessor(_card(), REQUEST, _sealed())
    assert carried == {
        "request_id": PREDECESSOR,
        "task_id": TASK,
        "claim_epoch": str(EPOCH),
        "candidate_sha256": ro.candidate_digest(HASHES),
        "contract_identity_sha256": ro.contract_identity_digest(_card()),
    }


def test_identical_bytes_predecessor_admits_matching_validation_replay_lineage():
    card = _card(validation_only_replay_lineage=_lineage())
    assert ro.identical_bytes_predecessor(card, REQUEST, _sealed())["request_id"] == (
        PREDECESSOR
    )


def _mutate(card: dict[str, Any], case: str) -> dict[str, Any]:
    predecessor = card["rework_predecessor"]
    evidence = card["terminal_review"]["evidence"]
    if case == "one_byte_differs":
        evidence["changed_path_hashes"] = {**HASHES, "src/a.py": "e" * 64}
    elif case == "extra_changed_path":
        evidence["changed_path_hashes"] = {**HASHES, "src/b.py": "f" * 64}
    elif case == "predecessor_other_task":
        predecessor["task_id"] = "T_OTHER"
    elif case == "request_identity_other_task":
        evidence["request_identity"]["task_id"] = "T_OTHER"
    elif case == "link_absent":
        card.pop("rework_predecessor")
    elif case == "link_wrong_schema":
        predecessor["schema_id"] = "caller.supplied.v1"
    elif case == "link_not_a_mapping":
        card["rework_predecessor"] = PREDECESSOR
    elif case == "predecessor_is_self":
        predecessor["request_id"] = REQUEST
    elif case == "predecessor_epoch_invalid":
        predecessor["claim_epoch"] = True
    elif case == "contract_unknown":
        predecessor.pop("contract_identity_sha256")
    elif case == "contract_changed":
        card["objective"] = "a different objective"
    elif case == "lineage_names_other_predecessor":
        card["validation_only_replay_lineage"] = _lineage(
            predecessor_request_id="REQ_SOMEONE_ELSE"
        )
    elif case == "lineage_epoch_mismatch":
        card["validation_only_replay_lineage"] = _lineage(
            predecessor_claim_epoch=EPOCH + 1
        )
    elif case == "lineage_epoch_missing":
        lineage = _lineage()
        lineage.pop("predecessor_claim_epoch")
        card["validation_only_replay_lineage"] = lineage
    elif case == "lineage_contract_disagrees":
        card["validation_only_replay_lineage"] = _lineage(
            contract_identity_sha256="0" * 64
        )
    elif case == "empty_hashes":
        predecessor["changed_path_hashes"] = {}
        evidence["changed_path_hashes"] = {}
    else:  # pragma: no cover - a typo in the parametrization
        raise AssertionError(case)
    return card


NEGATIVE_CASES = [
    "one_byte_differs", "extra_changed_path", "predecessor_other_task",
    "request_identity_other_task", "link_absent", "link_wrong_schema",
    "link_not_a_mapping", "predecessor_is_self", "predecessor_epoch_invalid",
    "contract_unknown", "contract_changed", "lineage_names_other_predecessor",
    "lineage_epoch_mismatch", "lineage_epoch_missing",
    "lineage_contract_disagrees", "empty_hashes",
]


@pytest.mark.parametrize("case", NEGATIVE_CASES)
def test_identical_bytes_predecessor_fails_closed(case):
    assert ro.identical_bytes_predecessor(
        _mutate(_card(), case), REQUEST, _sealed()
    ) == {}


SEALED_NEGATIVES = {
    "card_alone": None,
    "empty_binding": {},
    "receipt_other_request": _sealed(request_id="REQ_OTHER"),
    "receipt_other_epoch": _sealed(epoch=EPOCH + 1),
    "receipt_other_candidate": _sealed(hashes={**HASHES, "src/a.py": "e" * 64}),
    "receipt_other_task": _sealed(task_id="T_OTHER"),
}


@pytest.mark.parametrize("case", sorted(SEALED_NEGATIVES))
def test_identical_bytes_predecessor_requires_the_sealed_receipt_binding(case):
    assert ro.identical_bytes_predecessor(
        _card(), REQUEST, SEALED_NEGATIVES[case]
    ) == {}


def test_identical_bytes_predecessor_refuses_another_request():
    assert ro.identical_bytes_predecessor(_card(), "REQ_SOMEONE_ELSE", _sealed()) == {}
    assert ro.identical_bytes_predecessor(_card(), "", _sealed()) == {}
    assert ro.identical_bytes_predecessor(None, REQUEST, _sealed()) == {}


def test_predicate_has_exactly_one_definition():
    assert not hasattr(par, "identical_bytes_predecessor")
    assert not hasattr(par, "sealed_reviewer_binding")
    for function in (par.carried_reviewer_evidence, par._admitted_reviewer_target):
        assert "review_orchestrator.identical_bytes_predecessor(" in (
            inspect.getsource(function)
        )
    # The accept loop admits through the one admission rule; recovery's lens
    # coverage reuses the accept fold's carry, so it reaches the same predicate.
    accept = inspect.getsource(par.accept_review)
    assert accept.count("_admitted_reviewer_target(") == 2
    assert "_reviewer_target_split(" in accept
    assert "_carried_lens_lookup(" in inspect.getsource(ro.recover_review_ready_targets)
    assert "carried_reviewer_evidence(" in inspect.getsource(ro._carried_lens_lookup)


# --- accept fold: the predecessor's sealed rows are selected ----------------


def _row(
    request_id: str = PREDECESSOR,
    *,
    usable: bool = True,
    receipt: bool = True,
    sealed: Any = "default",
) -> dict[str, Any]:
    packet = _packet(request_id=request_id)
    return {
        "task_id": f"REVIEWER_{request_id}",
        "lens": LENS,
        "packet_sha256": packet["packet_sha256"],
        "terminal_substatus": "review_ready" if usable else "",
        "receipt": _receipt(packet) if receipt else None,
        "sealed_binding": _sealed(request_id=request_id) if sealed == "default" else sealed,
        "request_id": request_id + "_REVIEWER",
        "state": "review_ready" if usable else "worker_failed",
        "usable": usable,
        "attempt_count": 1,
        "running": False,
        "running_request_id": "",
        "state_unknown": False,
        "state_unknown_cause": "",
    }


def _patch_reviewer_evidence(monkeypatch, rows_by_parent):
    asked: list[str] = []

    def fake_reviewer_evidence(_self, task_id, parent_request_id, *, with_sealed_binding=False):
        assert task_id == TASK
        assert with_sealed_binding is True
        asked.append(parent_request_id)
        return [dict(row) for row in rows_by_parent.get(parent_request_id, [])]

    monkeypatch.setattr(par, "reviewer_evidence", fake_reviewer_evidence)
    return asked


def _carried(monkeypatch, card, rows_by_parent):
    asked = _patch_reviewer_evidence(monkeypatch, rows_by_parent)
    return par.carried_reviewer_evidence(object(), card, TASK, REQUEST), asked


def _missing(reviewers):
    fold = par.fold_accept_blockers(
        reviewers=reviewers,
        reviewer_request_ids=None,
        risk_profile={"required_reviewer_lenses": [LENS]},
        terminal_substatus="review_ready",
    )
    return [b for b in fold["blockers"] if b["kind"] == "required_reviewer_missing"], fold


def test_fold_accepts_identical_bytes_replay_with_predecessor_receipts(monkeypatch):
    carried, asked = _carried(monkeypatch, _card(), {PREDECESSOR: [_row()]})

    assert asked == [PREDECESSOR]
    assert [row["carried_from_request_id"] for row in carried] == [PREDECESSOR]
    missing, fold = _missing(carried)
    assert missing == []
    assert fold["reviewer_request_ids"] == [PREDECESSOR + "_REVIEWER"]


def test_fold_without_carry_still_reports_required_reviewer_missing():
    missing, _fold = _missing([])
    assert [b["lens"] for b in missing] == [LENS]


@pytest.mark.parametrize("case", ["one_byte_differs", "predecessor_other_task",
                                  "contract_changed", "lineage_epoch_mismatch"])
def test_fold_keeps_required_reviewer_missing_without_identical_bytes(monkeypatch, case):
    card = _mutate(_card(), case)
    carried, _asked = _carried(monkeypatch, card, {PREDECESSOR: [_row()]})

    assert carried == []
    missing, _fold = _missing(carried)
    assert [b["lens"] for b in missing] == [LENS]


def test_fold_without_a_link_asks_for_no_predecessor_evidence(monkeypatch):
    carried, asked = _carried(
        monkeypatch, _mutate(_card(), "link_absent"), {PREDECESSOR: [_row()]}
    )
    assert carried == [] and asked == []


@pytest.mark.parametrize("row", [
    _row(usable=False),
    _row(receipt=False),
    # Right target, but sealed to different candidate bytes.
    _row(sealed=_sealed(hashes={**HASHES, "src/a.py": "e" * 64})),
    # Right target and bytes, but reviewed on another claim epoch.
    _row(sealed=_sealed(epoch=EPOCH + 1)),
    # No sealed binding at all: the card link alone is never sufficient.
    _row(sealed={}),
], ids=["unusable", "no_receipt", "other_candidate", "other_epoch", "unsealed"])
def test_unverified_predecessor_receipt_is_not_carried(monkeypatch, row):
    carried, _asked = _carried(monkeypatch, _card(), {PREDECESSOR: [row]})

    assert carried == []
    missing, _fold = _missing(carried)
    assert [b["lens"] for b in missing] == [LENS]


# --- accept loop: one admitted target ---------------------------------------


def test_reviewer_target_admits_only_this_request_or_sealed_predecessor():
    admit = par._admitted_reviewer_target
    predecessor_receipt = _receipt(_packet())
    this_receipt = _receipt(_packet(request_id=REQUEST))
    assert admit(_card(), REQUEST, this_receipt, {}) == REQUEST
    assert admit(_card(), REQUEST, predecessor_receipt, _sealed()) == PREDECESSOR
    # A predecessor receipt whose seal disagrees is judged against THIS request,
    # so the caller's comparison raises quality_reviewer_target_mismatch.
    assert admit(_card(), REQUEST, predecessor_receipt, _sealed(epoch=EPOCH + 1)) == REQUEST
    assert admit(_card(), REQUEST, predecessor_receipt, {}) == REQUEST
    assert admit(_mutate(_card(), "contract_changed"), REQUEST,
                 predecessor_receipt, _sealed()) == REQUEST
    other = _receipt(_packet(request_id="REQ_OTHER"))
    assert admit(_card(), REQUEST, other, _sealed(request_id="REQ_OTHER")) == REQUEST


def test_split_admission_is_a_target_mismatch():
    split = par._reviewer_target_split
    predecessor_receipt = _receipt(_packet())
    this_receipt = _receipt(_packet(request_id=REQUEST))
    admitted = par._admitted_reviewer_target(
        _card(), REQUEST, predecessor_receipt, _sealed()
    )
    assert admitted == PREDECESSOR
    assert not split(PREDECESSOR, predecessor_receipt, admitted)
    # Receipt sealed to the predecessor, metadata naming this request.
    assert split(REQUEST, predecessor_receipt, admitted)
    # The reverse: metadata naming the predecessor, receipt sealed to this request.
    reverse = par._admitted_reviewer_target(_card(), REQUEST, this_receipt, _sealed())
    assert reverse == REQUEST
    assert split(PREDECESSOR, this_receipt, reverse)
    # This request's own reviewer is unchanged.
    assert not split(REQUEST, this_receipt, REQUEST)
    assert split(REQUEST, {"report": {}}, REQUEST)


# --- auto-seeding: no new reviewer for a carried lens -----------------------


class _RecoveryManager:
    def __init__(self, repo, card):
        self.repo = repo
        self._card = card
        self.lookups: list[dict[str, str]] = []
        self.events: list[dict[str, Any]] = []

    def status(self, request_id):
        assert request_id == REQUEST
        return {"ok": True, "state": "review_ready", "task_card": self._card}

    def existing_lens_reviewer(self, **kwargs):
        self.lookups.append(dict(kwargs))
        return None

    def _append_event(self, event):
        self.events.append(dict(event))


def _run_recovery(monkeypatch, tmp_path, card, rows):
    identity = {
        "target_task_id": TASK, "target_request_id": REQUEST, "claim_epoch": "3",
        "packet_sha256": "a" * 64, "candidate_sha256": ro.candidate_digest(HASHES),
    }
    manager = _RecoveryManager(tmp_path, card)
    registered: list[Any] = []
    _patch_reviewer_evidence(monkeypatch, {PREDECESSOR: rows})
    monkeypatch.setattr(ro.task_store, "list_review_queue_cards", lambda *_a, **_k: [card])
    monkeypatch.setattr(ro.task_store, "manager_ready_marker", lambda _card: None)
    monkeypatch.setattr(ro.task_store, "get_task", lambda *_a, **_k: None)
    monkeypatch.setattr(ro.review_lifecycle, "_chain_identity", lambda **kw: dict(kw))
    monkeypatch.setattr(ro, "resolve_target_identity", lambda _status: {
        "identity": dict(identity), "conflict": "",
        "identity_source": "terminal_review_evidence", "workspace_identity": "",
    })
    monkeypatch.setattr(ro, "candidate_registration", lambda **_kw: {
        "candidate_sha256": identity["candidate_sha256"],
        "required_reviewer_lenses": [LENS],
    })
    monkeypatch.setattr(
        ro, "register_candidate",
        lambda _manager, **kw: registered.append(kw) or {"ok": True},
    )
    result = ro.recover_review_ready_targets(manager, db_path=tmp_path / "lifecycle.db")
    return result, registered, manager


def test_seeding_launches_no_reviewer_for_lens_carried_from_predecessor(
    monkeypatch, tmp_path,
):
    result, registered, _manager = _run_recovery(monkeypatch, tmp_path, _card(), [_row()])

    assert registered == [], "a carried lens was re-seeded on identical bytes"
    assert result["review_recovery_reasons"] == {"already_present": 1}


@pytest.mark.parametrize("row", [
    _row(usable=False),
    _row(sealed=_sealed(epoch=EPOCH + 1)),
    _row(sealed=_sealed(hashes={**HASHES, "src/a.py": "e" * 64})),
], ids=["running_or_failed", "other_epoch", "other_candidate"])
def test_seeding_does_not_carry_an_unsealed_predecessor_reviewer(
    monkeypatch, tmp_path, row,
):
    _result, registered, _manager = _run_recovery(monkeypatch, tmp_path, _card(), [row])
    assert len(registered) == 1


@pytest.mark.parametrize("case", ["one_byte_differs", "predecessor_other_task",
                                  "link_absent", "contract_changed"])
def test_seeding_still_launches_without_identical_bytes(monkeypatch, tmp_path, case):
    card = _mutate(_card(), case)
    _result, registered, manager = _run_recovery(monkeypatch, tmp_path, card, [_row()])

    assert len(registered) == 1
    assert all(row["target_request_id"] == REQUEST for row in manager.lookups)


def test_seeding_coverage_carries_only_a_sealed_lookup():
    held = ro._lens_held_elsewhere
    sealed = {"task_id": "R", "request_id": "RQ", "state": "sealed"}
    assert held(lambda _lens: sealed, LENS, sealed_only=True)
    assert not held(lambda _lens: {**sealed, "state": "running"}, LENS, sealed_only=True)
    assert not held(lambda _lens: None, LENS, sealed_only=True)
    # The NF-2026-02388 same-request rule is unchanged: live still counts there.
    assert held(lambda _lens: {**sealed, "state": "running"}, LENS)


# --- end to end: the link is written by the REAL reject path ----------------

E2E_TASK = "T_E2E_REPLAY"
NOW = "2026-11-23T00:00:00+00:00"


def _db(root: Path) -> sqlite3.Connection:
    return sqlite3.connect(task_store.storage_readiness(root).canonical_db)


def _insert(root: Path, task_id: str, *, topic: str, card: dict[str, Any]) -> None:
    conn = _db(root)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "claude_coding", topic, "solo", "review", "review", "normal",
             "obj", json.dumps(card), NOW, NOW, "claude_coding"),
        )
        conn.commit()
    finally:
        conn.close()


def _stored_card(root: Path, task_id: str) -> dict[str, Any]:
    conn = _db(root)
    try:
        row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    card = json.loads(row[0])
    card.setdefault("task_id", task_id)
    return card


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token = tmp_path / "coordinator.token"
    token.write_text("coord-token\n", encoding="utf-8")
    os.chmod(token, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")
    return root


class _Ledger:
    """The manager surface ``reviewer_evidence`` reads, over a fixed ledger."""

    def __init__(self, repo: Path, latest: dict[str, dict[str, Any]]):
        self.repo = repo
        self._latest = latest

    def _latest_by_request(self):
        return dict(self._latest)

    def _request_events(self, request_id):
        event = self._latest.get(request_id)
        return [dict(event)] if event else []


def _rejected_then_replayed(store: Path, tmp_path: Path) -> tuple[dict[str, Any], _Ledger]:
    contract = {
        "objective": "carry verified reviewers across a byte-identical replay",
        "acceptance": ["no required_reviewer_missing on identical bytes"],
        "required_outputs": ["src/a.py"],
        "validation": ["python -m pytest tests/test_a.py -q"],
        "forbidden": [],
    }
    workspace = {
        "request_id": PREDECESSOR, "repo": str(store),
        "path": str(tmp_path / "wt" / PREDECESSOR / "worktree"),
        "home": str(tmp_path / "wt" / PREDECESSOR / "home"),
        "allowed_writes": sorted(HASHES), "parent_baseline": {},
        "workspace_baseline": {},
    }
    _insert(store, E2E_TASK, topic="code", card={
        **contract,
        "allowed_writes": sorted(HASHES),
        "claim_epoch": EPOCH,
        "terminal_review": {
            "substatus": "review_ready",
            "claim_epoch": EPOCH,
            "evidence": {
                "request_identity": {
                    "request_id": PREDECESSOR, "task_id": E2E_TASK,
                    "claim_epoch": EPOCH,
                },
                "workspace": workspace,
                "changed_path_hashes": dict(HASHES),
            },
        },
    })
    # The REAL writer: a manager reject back to pending pins the link.
    result = core.reject_review(E2E_TASK, "replay the same bytes", to="pending")
    assert result["ok"] is True, result
    card = _stored_card(store, E2E_TASK)
    link = card["rework_predecessor"]
    assert link["task_id"] == E2E_TASK
    assert link["claim_epoch"] == EPOCH
    assert link["contract_identity_sha256"] == ro.contract_identity_digest(card)

    # The predecessor's reviewer, sealed to the predecessor's exact packet.
    home = tmp_path / "reviewer_home"
    home.mkdir()
    packet = _packet(task_id=E2E_TASK)
    packet_path = home / "packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    receipt = _receipt(packet)
    _insert(store, "REVIEWER_E2E", topic="quality_review", card={
        "terminal_review": {"substatus": "review_ready", "evidence": {
            "quality_review": {
                "target_task_id": E2E_TASK, "target_request_id": PREDECESSOR,
                "target_claim_epoch": EPOCH, "lens": LENS,
                "packet_sha256": packet["packet_sha256"],
                "packet_path": str(packet_path),
            },
            "workspace": {"home": str(home)},
            "quality_review_receipt": receipt,
        }},
    })
    ledger = _Ledger(store, {"REQ_REVIEWER_E2E": {
        "task_id": "REVIEWER_E2E", "state": "review_ready",
        "finished_at": NOW,
    }})
    # The replay re-presents the identical bytes as its own terminal evidence.
    card["terminal_review"] = {"evidence": {
        "request_identity": {"request_id": REQUEST, "task_id": E2E_TASK},
        "changed_path_hashes": dict(HASHES),
    }}
    return card, ledger


def test_end_to_end_real_reject_writer_carries_the_sealed_reviewer(store, tmp_path):
    card, ledger = _rejected_then_replayed(store, tmp_path)

    carried = par.carried_reviewer_evidence(ledger, card, E2E_TASK, REQUEST)

    assert [row["request_id"] for row in carried] == ["REQ_REVIEWER_E2E"]
    missing, _fold = _missing(carried)
    assert missing == []
    lookup = ro._carried_lens_lookup(ledger, card, E2E_TASK, REQUEST)
    assert lookup is not None
    assert ro._lens_held_elsewhere(lookup, LENS, sealed_only=True), (
        "seeding would launch a reviewer for a lens the replay carries"
    )


def test_end_to_end_contract_amended_after_reject_fails_closed(store, tmp_path):
    card, ledger = _rejected_then_replayed(store, tmp_path)
    card["acceptance"] = [*card["acceptance"], "an amended criterion"]

    assert par.carried_reviewer_evidence(ledger, card, E2E_TASK, REQUEST) == []
    assert ro._carried_lens_lookup(ledger, card, E2E_TASK, REQUEST) is None
    missing, _fold = _missing([])
    assert [b["lens"] for b in missing] == [LENS]


def test_end_to_end_one_byte_difference_fails_closed(store, tmp_path):
    card, ledger = _rejected_then_replayed(store, tmp_path)
    card["terminal_review"]["evidence"]["changed_path_hashes"] = {
        **HASHES, "src/a.py": "e" * 64,
    }

    assert par.carried_reviewer_evidence(ledger, card, E2E_TASK, REQUEST) == []
    assert ro._carried_lens_lookup(ledger, card, E2E_TASK, REQUEST) is None


# --- standalone-accepted reviewer: the carried receipt keeps its own epoch ---


def test_accepted_carried_receipt_is_verified_against_its_own_epoch(monkeypatch):
    from aiworkhub import process_launcher as pl

    monkeypatch.setattr(pl, "_enforce_quality_review_receipt_schema", lambda r, _p: r)
    monkeypatch.setattr(pl, "_enforce_readonly_retained_workspace", lambda _e: None)
    receipt = {
        **_receipt(_packet()),
        "reviewer": {"request_id": "REQ_REVIEWER", "task_id": "T_REVIEWER"},
        "authority": {},
    }
    reviewer_card = {
        "task_id": "T_REVIEWER",
        "topic": "quality_review",
        "archived_at": "2026-11-23T00:00:00Z",
        "accepted_request_id": "REQ_REVIEWER",
        "allowed_writes": [],
        "terminal_review": {"evidence": {
            "quality_review_receipt": receipt,
            "quality_review": {"target_claim_epoch": EPOCH, "adapter_id": "claude_cli"},
        }},
        "accept_evidence": {"quality_review_receipt": receipt},
    }
    latest = {
        "state": "accepted", "accepted": True, "task_id": "T_REVIEWER",
        "adapter_id": "claude_cli", "quality_review_receipt": receipt,
    }
    # The replay runs at a later claim epoch; the carried receipt stays sealed to
    # the predecessor's, and only the reviewer's own binding is compared with it.
    replay = _card(claim_epoch=EPOCH + 1)
    admitted = par._admitted_reviewer_target(replay, REQUEST, receipt, _sealed())
    assert admitted == PREDECESSOR
    verified = pl._verified_accepted_quality_review_receipt(
        latest, reviewer_card, "REQ_REVIEWER", admitted, TASK
    )
    assert verified["target"] == {
        "request_id": PREDECESSOR, "task_id": TASK, "claim_epoch": EPOCH,
    }
    with pytest.raises(pl.WorkspaceError, match="accepted_target_mismatch"):
        pl._verified_accepted_quality_review_receipt(
            latest, reviewer_card, "REQ_REVIEWER", REQUEST, TASK
        )
