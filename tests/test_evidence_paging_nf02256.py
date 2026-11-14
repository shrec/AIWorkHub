"""NF-2026-02256: detail="evidence" replies are bounded and paged by cursor.

aiworkhub_agent_task_status(detail="evidence") once returned 138,060
characters and aiworkhub_task_show(detail="evidence") about 80 KB. Both now
return at most EVIDENCE_PAGE_CAP_BYTES of evidence per call plus a
continuation_cursor; following the cursors reproduces every evidence key
exactly once and the reassembled evidence hashes to evidence_sha256.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import evidence_paging, mcp_summary_folds, server  # noqa: E402

CAP = evidence_paging.EVIDENCE_PAGE_CAP_BYTES
TASK_ID = "TASK_PAGING_FIXTURE"
REQUEST_ID = "req-paging-0001"
STATUS_SECTIONS = ("task_card", "latest_event")
_MAX_PAGES = 64


def _filler(seed: str, size: int) -> str:
    """Unique text of about ``size`` characters with quotes, newlines and non-ASCII."""

    parts: list[str] = []
    total = 0
    counter = 0
    while total < size:
        digest = hashlib.sha256(f"{seed}:{counter}".encode()).hexdigest()
        piece = f'{digest} "é\n\\ ü {counter}\t'
        parts.append(piece)
        total += len(piece)
        counter += 1
    return "".join(parts)


def _big_card(status: str = "finished") -> dict[str, Any]:
    card: dict[str, Any] = {
        "task_id": TASK_ID,
        "status": status,
        "substatus": "validation_failed",
        "title": "paging fixture",
        "runner": "claude_opus-5",
        "current_request_id": REQUEST_ID,
    }
    for index in range(14):
        card[f"note_{index:02d}"] = {"text": _filler(f"note-{index}", 6000), "index": index}
    # One entry larger than a page on its own: it must be split into fragments.
    card["terminal_review"] = {
        "verdict": "rejected",
        "evidence": {"validation": {"log": _filler("terminal-log", 110_000)}},
    }
    card["history"] = [{"event": f"step-{index}", "detail": _filler(f"hist-{index}", 900)} for index in range(8)]
    return card


def _small_card() -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "status": "finished",
        "substatus": "accepted",
        "title": "small fixture",
        "terminal_review": {"verdict": "accepted", "evidence": {"validation": {"returncode": 0}}},
    }


def _big_event() -> dict[str, Any]:
    return {
        "state": "exited",
        "exit_code": 1,
        "started_at": "2026-11-07T10:00:00+00:00",
        "validation": {"stdout_tail": _filler("event-validation", 30_000)},
        "quality_gate": {"report": _filler("event-quality", 12_000)},
    }


def _envelope(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "returncode": 0,
        "command": ["show", card.get("task_id", "")],
        "stdout": json.dumps(card),
        "stderr": "",
    }


def _status(card: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "request_id": REQUEST_ID, "state": "exited", "task_card": card, "latest_event": event}


@pytest.fixture
def holder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Patch both read paths onto a mutable card/event so a test can change the card."""

    state: dict[str, Any] = {"card": _big_card(), "event": _big_event()}

    class FakeManager:
        def status(self, request_id: str) -> dict[str, Any]:
            assert request_id == REQUEST_ID
            return json.loads(json.dumps(_status(state["card"], state["event"])))

    monkeypatch.setattr(server.core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        server.core, "show_task", lambda task_id, full=False: _envelope(state["card"])
    )
    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: FakeManager())
    return state


def _show(cursor: str = "") -> dict[str, Any]:
    return server.aiworkhub_task_show(TASK_ID, detail="evidence", cursor=cursor)


def _status_call(cursor: str = "") -> dict[str, Any]:
    return server.aiworkhub_agent_task_status(REQUEST_ID, detail="evidence", cursor=cursor)


def _show_page(reply: dict[str, Any]) -> dict[str, Any]:
    page = json.loads(reply["stdout"])
    assert len(reply["stdout"].encode("utf-8")) <= CAP
    return {"": page}


def _status_page(reply: dict[str, Any]) -> dict[str, Any]:
    page = {name: reply[name] for name in STATUS_SECTIONS if isinstance(reply.get(name), dict)}
    assert len(evidence_paging.canonical_bytes(page)) <= CAP
    return page


def _walk(
    call: Callable[[str], dict[str, Any]],
    page_of: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Follow every cursor; return the reassembled sections and the replies."""

    replies: list[dict[str, Any]] = []
    assembled: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    pending: tuple[tuple[str, str], str, int] | None = None  # path, text, next offset
    cursor = ""
    for _ in range(_MAX_PAGES):
        reply = call(cursor)
        assert reply.get("ok", True) is not False, reply
        replies.append(reply)
        assert reply["evidence_truncated"] is True
        assert reply["evidence_page_count"] >= 2
        page = page_of(reply)
        assert reply["evidence_page_bytes"] == len(
            evidence_paging.canonical_bytes(page[""] if "" in page else page)
        )
        fragment = reply.get("evidence_fragment")
        for section, entries in page.items():
            target = assembled.setdefault(section, {})
            for key, value in entries.items():
                path = (section, key)
                if fragment is not None:
                    frag_path = tuple(fragment["path"]) if section else ("", fragment["path"][-1])
                    assert path == frag_path, (path, fragment)
                    if pending is None:
                        assert fragment["offset"] == 0
                        assert path not in seen, path
                        seen.add(path)
                        pending = (path, "", 0)
                    assert pending[0] == path and pending[2] == fragment["offset"]
                    text = pending[1] + value
                    pending = (path, text, fragment["offset"] + fragment["bytes"])
                    if fragment["final"]:
                        assert pending[2] == fragment["value_bytes"]
                        target[key] = json.loads(text)
                        pending = None
                    continue
                assert path not in seen, f"evidence key repeated: {path}"
                seen.add(path)
                target[key] = value
        cursor = reply["continuation_cursor"]
        if cursor is None:
            assert "evidence_next" not in reply["detail_request"]
            break
        assert reply["detail_request"]["evidence_next"]["cursor"] == cursor
    else:  # pragma: no cover - a regression guard
        pytest.fail("cursor chain did not terminate")
    assert pending is None
    assert len(replies) == replies[0]["evidence_page_count"]
    assert any(reply.get("evidence_fragment") for reply in replies), "fixture must split one entry"
    return assembled, replies


def _check_digest(document: Any, replies: list[dict[str, Any]]) -> None:
    encoded = evidence_paging.canonical_bytes(document)
    first = replies[0]
    assert first["evidence_total_bytes"] == len(encoded) > CAP
    assert first["evidence_sha256"] == hashlib.sha256(encoded).hexdigest()
    for reply in replies:
        assert reply["evidence_sha256"] == first["evidence_sha256"]
        assert reply["evidence_total_bytes"] == first["evidence_total_bytes"]


def test_task_show_evidence_pages_reassemble_exactly_once(holder: dict[str, Any]) -> None:
    assembled, replies = _walk(_show, _show_page)

    card = assembled[""]
    _check_digest(card, replies)
    assert mcp_summary_folds.expand_same_as(card) == holder["card"]
    assert list(card) == list(holder["card"])
    for reply in replies:
        assert reply["detail"] == "evidence"
        assert reply["card_sha256"] == replies[0]["card_sha256"]


def test_agent_task_status_evidence_pages_reassemble_exactly_once(holder: dict[str, Any]) -> None:
    assembled, replies = _walk(_status_call, _status_page)

    document = {name: assembled.get(name, {}) for name in STATUS_SECTIONS}
    _check_digest(document, replies)
    assert mcp_summary_folds.expand_same_as(document["task_card"]) == holder["card"]
    assert mcp_summary_folds.expand_same_as(document["latest_event"]) == holder["event"]
    for reply in replies:
        assert reply["detail"] == "evidence"
        assert reply["request_id"] == REQUEST_ID
        assert reply["evidence_sections"] == list(STATUS_SECTIONS)


@pytest.mark.parametrize("call", [_show, _status_call], ids=["task_show", "agent_task_status"])
def test_cursor_for_changed_card_refuses_stale(
    holder: dict[str, Any], call: Callable[[str], dict[str, Any]]
) -> None:
    first = call("")
    cursor = first["continuation_cursor"]
    assert isinstance(cursor, str) and cursor.startswith(evidence_paging.CURSOR_PREFIX)

    holder["card"] = _big_card(status="review")
    refused = call(cursor)

    assert refused["ok"] is False
    assert refused["error"] == "evidence_cursor_stale"
    assert refused["card_sha256"] != refused["cursor_card_sha256"]
    assert "stdout" not in refused and "task_card" not in refused


def test_cursor_is_bound_to_its_tool_and_rejects_garbage(holder: dict[str, Any]) -> None:
    show_cursor = _show("")["continuation_cursor"]

    crossed = _status_call(show_cursor)
    assert crossed["ok"] is False and crossed["error"] == "evidence_cursor_mismatch"

    garbage = _show("evc1.not-base64-json")
    assert garbage["ok"] is False and garbage["error"] == "evidence_cursor_invalid"


@pytest.mark.parametrize("call", [_show, _status_call], ids=["task_show", "agent_task_status"])
def test_small_evidence_is_unchanged_and_has_no_cursor(
    holder: dict[str, Any], call: Callable[[str], dict[str, Any]]
) -> None:
    holder["card"] = _small_card()
    holder["event"] = {"state": "exited", "exit_code": 0}

    reply = call("")

    for name in ("evidence_truncated", "evidence_total_bytes", "evidence_sha256", "continuation_cursor"):
        assert name not in reply
    assert "evidence_next" not in reply["detail_request"]
    if "stdout" in reply:
        assert mcp_summary_folds.expand_same_as(json.loads(reply["stdout"])) == holder["card"]
    else:
        assert mcp_summary_folds.expand_same_as(reply["task_card"]) == holder["card"]
        assert reply["latest_event"] == holder["event"]


def test_other_details_never_page(holder: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("only detail=evidence may page")

    monkeypatch.setattr(server.evidence_paging, "page_evidence", _forbidden)

    for detail in ("brief", "summary"):
        shown = server.aiworkhub_task_show(TASK_ID, detail=detail)
        status = server.aiworkhub_agent_task_status(REQUEST_ID, detail=detail)
        for reply in (shown, status):
            assert reply["detail"] in (detail, "validation_failed_packet")
            assert "evidence_truncated" not in reply
            assert "continuation_cursor" not in reply
            assert "evidence_next" not in reply["detail_request"]

    full_status = server.aiworkhub_agent_task_status(REQUEST_ID, detail="full")
    assert full_status["task_card"] == holder["card"]
    assert full_status["latest_event"] == holder["event"]
    assert "evidence_truncated" not in full_status

    full_show = server.aiworkhub_task_show(TASK_ID, detail="full")
    assert "evidence_truncated" not in full_show
    assert "continuation_cursor" not in full_show


@pytest.mark.parametrize("detail", ["brief", "summary", "full"])
def test_cursor_with_other_detail_refuses(holder: dict[str, Any], detail: str) -> None:
    cursor = _show("")["continuation_cursor"]

    shown = server.aiworkhub_task_show(TASK_ID, detail=detail, cursor=cursor)
    status = server.aiworkhub_agent_task_status(REQUEST_ID, detail=detail, cursor=cursor)

    for reply in (shown, status):
        assert reply["ok"] is False
        assert reply["error"] == "evidence_cursor_requires_detail_evidence"


def test_page_evidence_cap_is_honoured_for_a_small_cap() -> None:
    document = {f"k{index}": _filler(f"cap-{index}", 300) for index in range(10)}
    document["huge"] = _filler("cap-huge", 5000)
    common = {"tool": "unit", "task_id": "T", "card_sha256": "c" * 64}

    pages: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(_MAX_PAGES):
        paged = evidence_paging.page_evidence(document, cap=1024, cursor=cursor, **common)
        assert paged["ok"] is True
        assert len(evidence_paging.canonical_bytes(paged["document"])) <= 1024
        pages.append(paged)
        cursor = paged["meta"]["continuation_cursor"]
        if cursor is None:
            break
    assert len(pages) == pages[0]["meta"]["evidence_page_count"]

    rebuilt: dict[str, Any] = {}
    texts: dict[str, str] = {}
    for paged in pages:
        fragment = paged["meta"].get("evidence_fragment")
        for key, value in paged["document"].items():
            if fragment is None:
                assert key not in rebuilt
                rebuilt[key] = value
            else:
                texts[key] = texts.get(key, "") + value
                if fragment["final"]:
                    rebuilt[key] = json.loads(texts[key])
    assert rebuilt == document
    assert list(rebuilt) == list(document)
    assert hashlib.sha256(evidence_paging.canonical_bytes(rebuilt)).hexdigest() == pages[0]["meta"][
        "evidence_sha256"
    ]
