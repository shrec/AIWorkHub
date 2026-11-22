"""NF-2026-02362: two oversized replies stay bounded.

A) ``aiworkhub_agent_task_status(detail="summary")`` folds by size: any card
   value whose compact JSON exceeds ``SUMMARY_VALUE_BYTE_LIMIT`` folds to
   ``{summarized, bytes, sha256}``; identity/lifecycle fields stay exact and
   detail=brief/evidence/full replies do not change.
B) ``source_graph`` diff pages: the files/symbols inventory leads the cursor
   chain instead of repeating on every page, and every page's encoded bytes
   stay within its ``byte_budget``; the hunks across pages equal the
   single-pass hunk list exactly.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from aiworkhub import core, mcp_summary_folds, server
from aiworkhub import source_graph_diff as sgd

SUMMARY_CAP_BYTES = 12 * 1024


def _filler(size: int) -> str:
    return ("lorem ipsum " * (size // 12 + 1))[:size]


def _compact(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


# ---------------------------------------------------------------------------
# A) summary fold
# ---------------------------------------------------------------------------


def _heavy_card() -> dict:
    return {
        "task_id": "NF02362_FIXTURE",
        "title": "Oversized terminal review fixture",
        "status": "review",
        "substatus": "awaiting_review",
        "runner": "claude_opus-5",
        "topic": "needfix_conversion",
        "current_request_id": "req-02362",
        "last_request_id": "req-02361",
        "claim_epoch": 7,
        "created_at": "2026-11-20T10:00:00+00:00",
        "updated_at": "2026-11-21T10:00:00+00:00",
        "allowed_writes": ["src/a.py", "src/b.py", "tests/test_a.py"],
        "small_note": {"kept": True, "count": 3},
        "terminal_review": {
            "verdict": "accepted",
            "blocker": None,
            "blocker_summary": "no blocker",
            "evidence": {
                "validation": [
                    {"command": f"pytest -q tests/test_{i}.py", "returncode": 0,
                     "stdout_tail": _filler(1800)}
                    for i in range(8)
                ],
                "quality_review": {
                    "status": "passed",
                    "findings": [{"id": i, "body": _filler(700)} for i in range(12)],
                },
                "acceptance_review": {"verdict": "pass", "notes": _filler(6000)},
            },
            "accept_evidence": {"status": "accepted", "receipts": [_filler(900) for _ in range(10)]},
        },
        "accept": {
            "promotion": {"paths": ["src/a.py"], "log": _filler(5000)},
            "accepted_by": "manager",
        },
    }


def test_summary_fold_bounds_oversized_evidence_and_keeps_lifecycle_exact() -> None:
    card = _heavy_card()
    before = copy.deepcopy(card)
    assert _compact(card["terminal_review"]) + _compact(card["accept"]) >= 40 * 1024

    folded = mcp_summary_folds.fold_task_card_for_process_status(card)

    assert card == before
    assert _compact(folded) <= SUMMARY_CAP_BYTES, _compact(folded)
    for key in ("task_id", "title", "status", "substatus", "runner", "topic",
                "current_request_id", "last_request_id", "claim_epoch",
                "created_at", "updated_at"):
        assert folded[key] == card[key]
    assert folded["allowed_writes"] == {"summarized": True, "count": 3}
    # Values that already fit stay exact.
    assert folded["small_note"] == card["small_note"]
    review = folded["terminal_review"]
    for key in ("verdict", "blocker", "blocker_summary"):
        assert review[key] == card["terminal_review"][key]
    # Existing residual fold shape is unchanged.
    assert review["evidence"]["acceptance_review"]["summarized"] is True
    # Oversized values fold to the existing identity shape.
    validation = card["terminal_review"]["evidence"]["validation"]
    size, digest = core._evidence_identity(validation)
    assert review["evidence"]["validation"] == {"summarized": True, "bytes": size, "sha256": digest}
    receipts = card["terminal_review"]["accept_evidence"]["receipts"]
    size, digest = core._evidence_identity(receipts)
    assert review["accept_evidence"] == {
        "status": "accepted", "receipts": {"summarized": True, "bytes": size, "sha256": digest},
    }
    assert folded["accept"]["accepted_by"] == "manager"
    assert folded["accept"]["promotion"]["log"]["summarized"] is True
    for value in (review["evidence"]["validation"], folded["accept"]["promotion"]["log"]):
        assert _compact(value) <= mcp_summary_folds.SUMMARY_VALUE_BYTE_LIMIT


def test_summary_fold_folds_any_oversized_unknown_value() -> None:
    blob = [_filler(400) for _ in range(10)]
    text = _filler(3000)
    card = {"task_id": "T", "title": _filler(2000), "mystery_blob": blob, "mystery_text": text,
            "nested": {"deep": {"deeper": [_filler(300) for _ in range(20)]}}}
    folded = mcp_summary_folds.fold_task_card_for_process_status(card)
    assert folded["title"] == card["title"]
    for key, value in (("mystery_blob", blob), ("mystery_text", text)):
        size, digest = core._evidence_identity(value)
        assert folded[key] == {"summarized": True, "bytes": size, "sha256": digest}
    assert folded["nested"]["deep"]["deeper"]["summarized"] is True


def test_summary_fold_bounds_the_total_when_every_value_fits() -> None:
    # Every value fits SUMMARY_VALUE_BYTE_LIMIT, yet together they exceed 40 KB.
    card = {
        "task_id": "T", "title": "t", "status": "review", "claim_epoch": 1,
        **{f"note_{i}": _filler(900) for i in range(25)},
        "terminal_review": {
            "verdict": "accepted", "blocker_summary": "none",
            "evidence": {f"lane_{i}": _filler(900) for i in range(20)},
        },
    }
    before = copy.deepcopy(card)
    assert _compact(card) >= 40 * 1024

    folded = mcp_summary_folds.fold_task_card_for_process_status(card)

    assert card == before
    assert _compact(folded) <= mcp_summary_folds.SUMMARY_TOTAL_BYTE_LIMIT == SUMMARY_CAP_BYTES
    for key in ("task_id", "title", "status", "claim_epoch"):
        assert folded[key] == card[key]
    for key in ("verdict", "blocker_summary"):
        assert folded["terminal_review"][key] == card["terminal_review"][key]
    for key in (f"note_{i}" for i in range(25)):
        assert folded[key] == card[key] or folded[key]["summarized"] is True


def test_server_folded_keys_match_the_server_owner() -> None:
    assert set(mcp_summary_folds.PROCESS_STATUS_SERVER_FOLDED_KEYS) == set(
        server._CARD_MANAGER_BODY_KEYS
    )


def _status_result() -> dict:
    card = _heavy_card()
    return {
        "ok": True,
        "request_id": "req-02362",
        "task_id": card["task_id"],
        "state": "finished",
        "task_card": card,
        "latest_event": {"event": "finished", "state": "finished", "exit_code": 0},
    }


def _install(monkeypatch: pytest.MonkeyPatch, status: dict) -> None:
    class FakeManager:
        def status(self, request_id: str) -> dict:
            assert request_id == "req-02362"
            return status

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: FakeManager())


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def test_agent_task_status_summary_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _status_result()
    before = copy.deepcopy(status)
    _install(monkeypatch, status)
    result = server.aiworkhub_agent_task_status("req-02362", detail="summary")
    assert result["detail"] == "summary"
    assert _compact(result) <= SUMMARY_CAP_BYTES, _compact(result)
    card = result["task_card"]
    assert card["task_id"] == "NF02362_FIXTURE"
    assert card["status"] == "review"
    assert card["claim_epoch"] == 7
    assert card["terminal_review"]["verdict"] == "accepted"
    assert status == before


def test_brief_evidence_and_full_replies_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _status_result()
    before = copy.deepcopy(status)
    _install(monkeypatch, status)
    replies = {
        detail: _canonical(server.aiworkhub_agent_task_status("req-02362", detail=detail))
        for detail in ("brief", "evidence", "full")
    }
    # brief projects the summary card through keys the size fold keeps exact,
    # so it equals the brief built with no card fold at all.
    monkeypatch.setattr(mcp_summary_folds, "fold_task_card_for_process_status", lambda card: card)
    assert _canonical(server.aiworkhub_agent_task_status("req-02362", detail="brief")) == replies["brief"]

    # evidence and full never run the summary fold.
    def _refuse(card: object) -> object:
        raise AssertionError("summary fold ran for a non-summary detail")

    monkeypatch.setattr(mcp_summary_folds, "fold_task_card_for_process_status", _refuse)
    for detail in ("evidence", "full"):
        assert _canonical(server.aiworkhub_agent_task_status("req-02362", detail=detail)) == replies[detail]
    assert json.loads(replies["full"])["task_card"] == before["task_card"]
    assert status == before


# ---------------------------------------------------------------------------
# B) diff pages
# ---------------------------------------------------------------------------


def _module(index: int, version: int) -> str:
    parts = []
    for fn in range(6):
        body = "".join(f"    v{line} = {line} + {version}  # é \"q\"\t\\\n" for line in range(10))
        parts.append(
            f"def fn_{index}_{fn}():\n    \"\"\"Doc {index}.\"\"\"\n{body}    return {version}\n\n\n"
        )
    return "".join(parts)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def wide_diff(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = (tmp_path / "repo").resolve()
    (root / ".git").mkdir(parents=True)
    old, new = root / "cmp" / "a", root / "cmp" / "b"
    for index in range(60):
        _write(old / f"pkg/m{index:02d}.py", _module(index, 0))
        _write(new / f"pkg/m{index:02d}.py", _module(index, 1))
    # One hunk larger than a whole page, so pieces split across pages.
    _write(old / "big.txt", "".join(f"row {i} ü\n" for i in range(3000)))
    _write(new / "big.txt", "".join(f"row {i} Ü\n" for i in range(3000)))
    return root, old, new


def _page_through(fetch, budget: int) -> list[dict]:
    pages: list[dict] = []
    cursor = None
    while True:
        page = fetch(budget, cursor)
        pages.append(page)
        if not page["truncated"]:
            assert page["next_cursor"] is None and page["truncation_marker"] is None
            return pages
        assert len(pages) < 10_000
        cursor = page["next_cursor"]


def _assert_pages_reassemble(pages: list[dict], single: dict) -> None:
    pieces: dict[int, str] = {}
    order: list[int] = []
    for page in pages:
        for hunk in page["hunks"]:
            assert len(pieces.get(hunk["index"], "").encode("utf-8", "surrogateescape")) == hunk["offset"]
            pieces[hunk["index"]] = pieces.get(hunk["index"], "") + hunk["text"]
            if not order or order[-1] != hunk["index"]:
                order.append(hunk["index"])
    assert order == [hunk["index"] for hunk in single["hunks"]]
    assert [pieces[index] for index in order] == [hunk["text"] for hunk in single["hunks"]]
    for key in ("files", "symbols", "skipped"):
        assert [row for page in pages for row in page[key]] == single[key]
        assert all(page["inventory_total"][key] == len(single[key]) for page in pages)
    assert all(page["fingerprint"] == single["fingerprint"] for page in pages)


def test_diff_pages_stay_within_the_page_cap(wide_diff: tuple[Path, Path, Path]) -> None:
    root, old, new = wide_diff
    single = sgd.diff_directories(root, old, new, byte_budget=1 << 30)
    assert single["truncated"] is False
    assert all(hunk["complete"] for hunk in single["hunks"])
    budget = sgd.DEFAULT_BYTE_BUDGET
    # The inventory alone overflows one page, and so do the hunks.
    assert len(json.dumps(single["symbols"])) > budget
    assert sum(len(hunk["text"]) for hunk in single["hunks"]) > 3 * budget

    pages = _page_through(
        lambda size, cursor: sgd.diff_directories(root, old, new, byte_budget=size, cursor=cursor),
        budget,
    )
    assert len(pages) > 3
    for page in pages:
        assert len(json.dumps(page)) <= budget, len(json.dumps(page))
        assert _compact(page) <= budget
        assert sgd.page_bytes(page) <= budget
    # Only the leading pages carry inventory rows; later ones carry counts.
    assert pages[0]["files"] == single["files"]
    assert pages[-1]["files"] == [] and pages[-1]["symbols"] == []
    _assert_pages_reassemble(pages, single)


def _synthetic() -> tuple[list[dict], dict, str]:
    hunks = [
        {"path": f"p{i}.txt", "header": "@@ -1 +1 @@",
         "text": ("é\"\t\\x\u0001" * 30 + "\U0001F600\n") * (i + 1)}
        for i in range(6)
    ]
    inventory = {
        "files": [{"status": "modified", "path": f"p{i}.txt", "old_path": f"p{i}.txt"} for i in range(6)],
        "symbols": [{"path": "p0.txt", "symbol": f"s{i}", "change": "added"} for i in range(4)],
        "skipped": [{"path": "huge.bin", "reason": "file_over_byte_limit"}],
    }
    return hunks, inventory, sgd._fingerprint(inventory["files"], hunks)


@pytest.mark.parametrize("budget", [1, 40, 300, 1200, 2500, 10_000])
def test_paginate_reassembles_at_every_budget(budget: int) -> None:
    hunks, inventory, fingerprint = _synthetic()
    head = {"schema_id": sgd.SCHEMA_ID}

    def fetch(size: int, cursor: str | None) -> dict:
        return sgd._paginate(hunks, inventory, head, size, cursor, fingerprint)

    single = fetch(1 << 30, None)
    assert single["truncated"] is False
    pages = _page_through(fetch, budget)
    _assert_pages_reassemble(pages, single)
    if budget >= 1200:  # room for one page's envelope plus a row or piece
        assert all(sgd.page_bytes(page) <= budget for page in pages)
    else:  # minimum progress: one row, or one piece of <= budget raw bytes
        for page in pages:
            rows = sum(len(page[key]) for key in inventory)
            assert rows + len(page["hunks"]) >= 1
            assert sum(len(h["text"].encode("utf-8")) for h in page["hunks"]) <= max(budget, 4)


def test_inventory_cursor_is_bound_to_the_fingerprint() -> None:
    hunks, inventory, fingerprint = _synthetic()
    first = sgd._paginate(hunks, inventory, {}, 40, None, fingerprint)
    assert first["next_cursor"].startswith("sgd1i:")
    stale = first["next_cursor"].rsplit(":", 1)[0] + ":0000000000000000"
    with pytest.raises(sgd.SourceGraphDiffError, match="cursor_stale"):
        sgd._paginate(hunks, inventory, {}, 40, stale, fingerprint)
    with pytest.raises(sgd.SourceGraphDiffError, match="cursor_invalid"):
        sgd._paginate(hunks, inventory, {}, 40, f"sgd1i:1:5:{fingerprint}", fingerprint)
    with pytest.raises(sgd.SourceGraphDiffError, match="cursor_invalid"):
        sgd._paginate(hunks, inventory, {}, 40, f"sgd1i:999:0:{fingerprint}", fingerprint)
