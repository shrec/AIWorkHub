"""NF-2026-02118: the accepted catalog never decodes every task card whole.

``sdlc_attribution.load_accepted_catalog`` (run by the reconciler's sdlc_sync
attribution phase) used to read up to 5000 full cards with
``task_store.list_task_cards`` and json-decode every card_json, while it reads
only the accepted request id and the sealed accepted-outcome receipt of the
manager-accepted cards. These tests pin that it now reads only
``task_store.ACCEPTED_RECEIPT_PROJECTION`` of the accepted ids, and that the
``AcceptedCard`` tuples it builds equal those the old full-decode loop builds
over the same store -- the expected value is computed by running that old loop
over ``task_store.list_task_cards`` full cards, never by the code under test.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import needfix_store, sdlc_attribution, sdlc_outcome_metrics, task_store

REPOSITORY_ID = "repo_nf02118_fixture"
_HISTORY = [{"event": "note", "body": "h" * 512} for _ in range(8)]
_T0 = "2026-11-06T00:00:0{}+00:00"


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _receipt(task_id: str, request_id: str, paths: list[str]) -> dict[str, Any]:
    promoted = sorted(set(paths))
    hashes = {path: hashlib.sha256(f"{task_id}:{path}".encode()).hexdigest() for path in promoted}
    base_oid = "b" * 40
    receipt: dict[str, Any] = {
        "schema_id": needfix_store.ACCEPTED_OUTCOME_RECEIPT_SCHEMA_ID,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": 1,
        "base_oid": base_oid,
        "promoted_paths": promoted,
        "changed_path_hashes": hashes,
        "attempt_artifact_manifest_id": _digest({"artifacts": ["metadata.json"]}),
        "repository_revision": "sha256:" + _digest(
            {"base_oid": base_oid, "changed_path_hashes": hashes}
        ),
    }
    receipt["receipt_id"] = "sha256:" + _digest(receipt)
    return receipt


def _insert_card(repo: Path, task_id: str, card_json: str, *, updated_at: str) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at) "
            "VALUES (?, 'fixture_runner', 'coding', 'finished', 'finished', '', '', ?, ?, ?)",
            (task_id, card_json, updated_at, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def _decide(repo: Path, task_id: str, event: str) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, ?, 'fixture_runner', '{}', ?)",
            (task_id, event, _T0.format(9)),
        )
        conn.commit()
    finally:
        conn.close()


def _card(task_id: str, **fields: Any) -> str:
    return json.dumps({"task_id": task_id, "history": _HISTORY, **fields})


def _accepted_card(task_id: str, paths: list[str], **extra: Any) -> str:
    request_id = f"req-{task_id}"
    return _card(
        task_id,
        accepted_request_id=request_id,
        accept_evidence={
            "accepted_outcome_receipt": _receipt(task_id, request_id, paths),
            "validation": {"stdout": "v" * 4096},
        },
        **extra,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    task_store.initialize_repository(root)
    needfix_store.initialize_repository(root)
    tampered = json.loads(_accepted_card("A-tampered", ["src/t.py"]))
    tampered["accept_evidence"]["accepted_outcome_receipt"]["receipt_id"] = "sha256:" + "0" * 64
    rows: list[tuple[str, str, str, str | None]] = [
        # (task_id, card_json, updated_at, latest decision event)
        ("A-old", _accepted_card("A-old", ["src/a.py"]), _T0.format(1), "accept_review"),
        # Two accepted cards promoting the same path with a tied updated_at:
        # their relative order is what explaining_card's first match sees.
        ("A-tie-1", _accepted_card("A-tie-1", ["src/b.py"]), _T0.format(2), "accept_review"),
        ("A-tie-2", _accepted_card("A-tie-2", ["src/b.py"]), _T0.format(2), "accept_review"),
        ("A-new", _accepted_card("A-new", ["src/a.py", "src/c.py"]), _T0.format(5), "accept_review"),
        ("R-rejected", _accepted_card("R-rejected", ["src/r.py"]), _T0.format(3), "reject_review"),
        ("P-pending", _accepted_card("P-pending", ["src/p.py"]), _T0.format(3), None),
        ("A-no-evidence", _card("A-no-evidence", accepted_request_id="req-x"), _T0.format(4),
         "accept_review"),
        ("A-tampered", json.dumps(tampered), _T0.format(4), "accept_review"),
        ("A-receipt-string", _card("A-receipt-string", accepted_request_id="req-s",
                                   accept_evidence={"accepted_outcome_receipt": "nope"}),
         _T0.format(4), "accept_review"),
        ("A-evidence-list", _card("A-evidence-list", accepted_request_id="req-l",
                                  accept_evidence=["not", "an", "object"]),
         _T0.format(4), "accept_review"),
        ("A-malformed", "{not json", _T0.format(6), "accept_review"),
        ("A-array", "[1, 2]", _T0.format(6), "accept_review"),
        # Python decodes Infinity, SQLite json_valid rejects it: ships whole.
        ("A-infinity", '{"accepted_request_id": Infinity}', _T0.format(6), "accept_review"),
    ]
    for task_id, card_json, updated_at, event in rows:
        _insert_card(root, task_id, card_json, updated_at=updated_at)
        if event is not None:
            _decide(root, task_id, event)
    return root


def _old_full_decode_cards(repo: Path) -> tuple[sdlc_attribution.AcceptedCard, ...]:
    """The pre-NF-2026-02118 loop, verbatim, over full ``list_task_cards`` cards."""
    cards = task_store.list_task_cards(repo, limit=sdlc_attribution.MAX_TASK_CARDS)
    decisions = task_store.latest_manager_decisions(repo)
    accepted: list[sdlc_attribution.AcceptedCard] = []
    for card in cards:
        task_id = str(card.get("task_id") or "").strip()
        if decisions.get(task_id, {}).get("decision") != "accepted":
            continue
        request_id = str(card.get("accepted_request_id") or "").strip()
        evidence = card.get("accept_evidence")
        receipt = (
            evidence.get("accepted_outcome_receipt")
            if isinstance(evidence, Mapping) else None
        )
        identity = sdlc_outcome_metrics.accepted_outcome_identity(
            {
                "event": "accept_review",
                "task_id": task_id,
                "payload": {
                    "request_id": request_id,
                    "accepted_outcome_receipt": receipt,
                },
            },
            REPOSITORY_ID,
        )
        if identity is None or not isinstance(receipt, Mapping):
            continue
        accepted.append(sdlc_attribution.AcceptedCard(
            task_id=task_id,
            request_id=request_id,
            base_oid=str(receipt.get("base_oid") or ""),
            promoted_paths=tuple(str(path) for path in receipt.get("promoted_paths") or ()),
            changed_path_hashes=dict(receipt.get("changed_path_hashes") or {}),
            identity=identity,
        ))
    return tuple(accepted)


# -- parity with the old full-decode loop -------------------------------------


def test_catalog_cards_equal_the_old_full_decode_loop(repo: Path) -> None:
    expected = _old_full_decode_cards(repo)
    # The fixture is not vacuous: the four well-formed accepted cards survive,
    # every rejected/pending/malformed shape is dropped by the old loop itself.
    assert {card.task_id for card in expected} == {"A-old", "A-tie-1", "A-tie-2", "A-new"}
    catalog = sdlc_attribution.load_accepted_catalog(repo, REPOSITORY_ID)
    assert catalog.cards == expected


def test_catalog_with_no_accepted_decision_is_empty_without_reading_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    task_store.initialize_repository(root)
    _insert_card(root, "P-only", _accepted_card("P-only", ["src/p.py"]), updated_at=_T0.format(1))

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no accepted decision: no card may be read")

    monkeypatch.setattr(task_store, "get_tasks", forbidden)
    monkeypatch.setattr(task_store, "list_task_cards", forbidden)
    assert sdlc_attribution.load_accepted_catalog(root, REPOSITORY_ID).cards == ()


# -- only the narrow projection is read ---------------------------------------


def test_catalog_reads_only_the_accepted_receipt_projection(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _old_full_decode_cards(repo)
    accepted_ids = {
        task_id
        for task_id, decision in task_store.latest_manager_decisions(repo).items()
        if decision["decision"] == "accepted"
    }
    calls: list[tuple[str, Any, tuple[str, ...]]] = []
    decoded: list[str] = []
    real_list = task_store.list_task_cards
    real_get_tasks = task_store.get_tasks
    real_get_task = task_store.get_task
    real_decode = task_store._decode_task_card

    def list_spy(root, **kwargs):
        calls.append(("list_task_cards", kwargs.get("projection"), ()))
        return real_list(root, **kwargs)

    def get_tasks_spy(root, task_ids, **kwargs):
        ids = tuple(task_ids)
        calls.append(("get_tasks", kwargs.get("projection"), ids))
        return real_get_tasks(root, ids, **kwargs)

    def get_task_spy(root, task_id, **kwargs):
        calls.append(("get_task", kwargs.get("projection"), (task_id,)))
        return real_get_task(root, task_id, **kwargs)

    def decode_spy(row, *args, **kwargs):
        decoded.append(str(row["task_id"]))
        return real_decode(row, *args, **kwargs)

    monkeypatch.setattr(task_store, "list_task_cards", list_spy)
    monkeypatch.setattr(task_store, "get_tasks", get_tasks_spy)
    monkeypatch.setattr(task_store, "get_task", get_task_spy)
    monkeypatch.setattr(task_store, "_decode_task_card", decode_spy)

    catalog = sdlc_attribution.load_accepted_catalog(repo, REPOSITORY_ID)

    assert catalog.cards == expected
    assert decoded == [], "a task card was decoded whole"
    assert all(name != "list_task_cards" or projection is not None
               for name, projection, _ids in calls), calls
    card_reads = [call for call in calls if call[0] in {"get_tasks", "get_task"}]
    assert [name for name, _p, _ids in card_reads] == ["get_tasks"], calls
    _name, projection, ids = card_reads[0]
    assert projection == task_store.ACCEPTED_RECEIPT_PROJECTION
    assert set(ids) == accepted_ids
    assert len(ids) == len(set(ids))


# -- the projection through every reader --------------------------------------


def _expected_projection(full: Mapping[str, Any]) -> dict[str, Any]:
    card: dict[str, Any] = {
        "task_id": full["task_id"],
        "accepted_request_id": full.get("accepted_request_id"),
    }
    evidence = full.get("accept_evidence")
    if isinstance(evidence, dict):
        card["accept_evidence"] = {
            "accepted_outcome_receipt": evidence.get("accepted_outcome_receipt"),
        }
    return card


def test_projection_works_through_list_get_task_and_get_tasks(repo: Path) -> None:
    projection = task_store.ACCEPTED_RECEIPT_PROJECTION
    full = task_store.list_task_cards(repo, limit=5000)
    scan = task_store.list_task_cards(repo, limit=5000, projection=projection)
    assert [card["task_id"] for card in scan] == [card["task_id"] for card in full]
    assert scan == [_expected_projection(card) for card in full]
    assert {card["task_id"]: card for card in scan}["A-evidence-list"] == {
        "task_id": "A-evidence-list", "accepted_request_id": "req-l",
    }
    assert {card["task_id"]: card for card in scan}["A-malformed"] == {
        "task_id": "A-malformed", "accepted_request_id": None,
    }
    by_id = {card["task_id"]: card for card in scan}
    assert task_store.get_tasks(repo, list(by_id), projection=projection) == by_id
    for task_id, card in by_id.items():
        assert task_store.get_task(repo, task_id, projection=projection) == card
    assert task_store.get_task(repo, "T-missing", projection=projection) is None
    assert task_store.get_tasks(repo, ["T-missing"], projection=projection) == {}


def test_task_ids_newest_first_matches_list_task_cards_order(repo: Path) -> None:
    for limit in (1, 3, 5000):
        full = task_store.list_task_cards(repo, limit=limit)
        assert task_store.list_task_ids_newest_first(repo, limit=limit) == [
            card["task_id"] for card in full
        ]


def test_unknown_projection_still_fails_closed(repo: Path) -> None:
    with pytest.raises(ValueError, match="unknown task card projection"):
        task_store.list_task_cards(repo, projection="accepted_receipts")
    with pytest.raises(ValueError, match="unknown task card projection"):
        task_store.get_task(repo, "A-old", projection="accepted_receipts")
    with pytest.raises(ValueError, match="unknown task card projection"):
        task_store.get_tasks(repo, ["A-old"], projection="accepted_receipts")
