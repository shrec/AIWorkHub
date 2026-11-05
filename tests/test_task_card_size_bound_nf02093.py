"""NF-2026-02093: a task card and its detail=full reply stay under a fixed byte budget.

Old history entries and large evidence bodies fold to ``{sha256, bytes,
spill_locator}``; the exact bytes stay retrievable from the output spill store.
"""

from __future__ import annotations

import json
from typing import Any

from aiworkhub import mcp_summary_folds as folds
from aiworkhub import server


def _blob(tag: str, size: int) -> str:
    unit = f"{tag}-line-with-some-payload-text\n"
    return (unit * (size // len(unit) + 1))[:size]


def _card(reworks: int) -> dict[str, Any]:
    history = [
        {"event": "rework" if i % 2 else "launch", "index": i, "note": _blob(f"h{i}", 6000)}
        for i in range(reworks * 3 + 2)
    ]
    return {
        "task_id": "needfix-NF-TEST",
        "status": "accepted",
        "title": "bounded card",
        "history": history,
        "terminal_review": {
            "evidence": {
                "validation": {"verdict": "pass", "stdout": _blob("v", 40000)},
                "quality_gate": {"verdict": "pass", "rows": [_blob("q", 9000)]},
            }
        },
        "accept_evidence": {"validation": {"stdout": _blob("a", 30000)}},
        "objective": "small objective",
    }


def _size(card: Any) -> int:
    return len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def test_card_under_budget_is_returned_exact(tmp_path):
    card = {"task_id": "t", "status": "open", "history": [{"a": 1}]}
    assert folds.bound_task_card(card, repo=tmp_path) is card


def test_card_with_many_reworks_stays_under_budget(tmp_path):
    for reworks in (1, 5, 20):
        card = _card(reworks)
        assert _size(card) > folds.CARD_BYTE_BUDGET
        bounded = folds.bound_task_card(card, repo=tmp_path)
        assert _size(bounded) <= folds.CARD_BYTE_BUDGET
        assert not bounded["card_bound"].get("over_budget")
        assert bounded["task_id"] == card["task_id"]
        assert bounded["status"] == card["status"]
        assert bounded["objective"] == card["objective"]
        assert _size(card) > folds.CARD_BYTE_BUDGET  # input not mutated


def test_old_history_folds_and_recent_history_stays_exact(tmp_path):
    card = _card(5)
    bounded = folds.bound_task_card(card, repo=tmp_path)
    keep = folds.CARD_HISTORY_KEEP
    assert bounded["history"][1:] == card["history"][-keep:]
    stub = bounded["history"][0]
    assert stub["summarized"] is True
    assert stub["count"] == len(card["history"]) - keep
    assert set(stub) >= {"sha256", "bytes", "spill_locator"}
    restored = folds.retrieve_folded(stub["spill_locator"], repo=tmp_path)
    assert restored == card["history"][:-keep]


def test_evidence_bodies_fold_and_stay_retrievable(tmp_path):
    card = _card(1)
    bounded = folds.bound_task_card(card, repo=tmp_path)
    stub = bounded["terminal_review"]["evidence"]["validation"]["stdout"]
    assert stub["summarized"] is True
    assert stub["bytes"] > 2048
    restored = folds.retrieve_folded(stub["spill_locator"], repo=tmp_path)
    assert restored == card["terminal_review"]["evidence"]["validation"]["stdout"]
    assert bounded["terminal_review"]["evidence"]["validation"]["verdict"] == "pass"
    assert bounded["card_bound"]["original_bytes"] == _size(card)


def test_bounding_is_deterministic(tmp_path):
    card = _card(4)
    assert folds.bound_task_card(card, repo=tmp_path) == folds.bound_task_card(card, repo=tmp_path)


def test_task_show_full_pages_over_budget_reply_and_keeps_small_reply_exact(tmp_path, monkeypatch):
    show = getattr(server.aiworkhub_task_show, "fn", server.aiworkhub_task_show)
    cards = {"big": _card(8), "small": {"task_id": "small", "status": "open"}}

    def fake_show(task_id: str, *, full: bool = True) -> dict[str, Any]:
        return {"ok": True, "returncode": 0, "stdout": json.dumps(cards[task_id], indent=2)}

    monkeypatch.setattr(server.core, "show_task", fake_show)
    monkeypatch.setattr(server.core, "repo_root", lambda: tmp_path)

    big = show("big", detail="full")
    assert len(big["stdout"].encode("utf-8")) <= folds.CARD_BYTE_BUDGET
    assert big["bounded"] is True
    bounded = json.loads(big["stdout"])
    locator = bounded["history"][0]["spill_locator"]
    restored = folds.retrieve_folded(locator, repo=tmp_path)
    assert restored == cards["big"]["history"][: len(restored)]

    small = show("small", detail="full")
    assert "bounded" not in small
    assert json.loads(small["stdout"]) == cards["small"]
