"""NF-2026-02256: bounded ``detail="evidence"`` replies with a continuation cursor.

``aiworkhub_agent_task_status(detail="evidence")`` once returned 138,060
characters and ``aiworkhub_task_show(detail="evidence")`` about 80 KB, so a
manager could not read either in one call. Both wrappers now pass their
evidence document through :func:`page_evidence`:

* evidence whose compact JSON fits ``EVIDENCE_PAGE_CAP_BYTES`` comes back
  unchanged, with no paging fields at all;
* larger evidence comes back as the leading top-level entries that fit (for
  ``agent_task_status`` the entries are the keys of its ``task_card`` and
  ``latest_event`` sections), plus ``evidence_truncated``,
  ``evidence_total_bytes``, ``evidence_sha256`` and an opaque
  ``continuation_cursor`` that the tool's ``cursor`` argument accepts for the
  next page (``None`` on the last page);
* one entry larger than a page on its own is split across consecutive pages:
  each such page carries that entry's value as a slice of its compact JSON
  text and names it in ``evidence_fragment`` (path, byte offset, bytes,
  value_bytes, final); concatenating the slices and parsing them restores the
  value.

Every entry appears on exactly one page (a split entry on its consecutive
fragment pages only), and the reassembled document's compact JSON hashes to
``evidence_sha256``. The cursor binds the tool, task_id, card_sha256, the
evidence_sha256 and the page's byte offset in the evidence stream; a cursor
for a card or evidence that has since changed refuses with
``evidence_cursor_stale`` rather than splicing pages of two documents.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

EVIDENCE_PAGE_CAP_BYTES = 64 * 1024
"""Byte cap of one ``detail="evidence"`` page's evidence (compact UTF-8 JSON).

64 KB pages the reported 80 KB card and 138 KB status replies while the
~52-54 KB evidence fixtures that pin "evidence carries every byte"
(NF-2026-01447 and the terminal-metadata compaction test) stay unpaged.
"""

CURSOR_PREFIX = "evc1."
CURSOR_MAX_CHARS = 1024
_CURSOR_VERSION = 1
_MIN_CHUNK_BUDGET = 64
_RESTART_HINT = "call again with detail=evidence and no cursor to restart from the first page"


class _KeyExceedsCap(Exception):
    def __init__(self, path: tuple[str, ...]) -> None:
        super().__init__(path)
        self.path = path


@dataclass(frozen=True)
class _Unit:
    path: tuple[str, ...]
    value: Any  # the original value when whole, else the chunk's JSON text slice
    offset: int  # byte offset of this unit in the evidence value stream
    value_offset: int
    chunk_bytes: int
    value_bytes: int
    whole: bool


def canonical_bytes(value: Any) -> bytes:
    """The compact JSON the read wrappers render, as UTF-8 bytes."""

    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return text.encode("utf-8", "surrogatepass")


def cursor_detail_refusal(detail: str) -> dict[str, Any]:
    """Refuse a ``cursor`` passed with any detail other than ``evidence``."""

    return {
        "ok": False,
        "error": "evidence_cursor_requires_detail_evidence",
        "reason": "cursor pages detail=evidence only",
        "received_detail": str(detail)[:40],
    }


def _refusal(error: str, reason: str, tool: str, task_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "reason": reason,
        "tool": tool,
        "task_id": task_id,
        "restart": _RESTART_HINT,
        **extra,
    }


def _evidence_document(
    document: Mapping[str, Any], sections: Sequence[str] | None
) -> dict[str, Any]:
    if sections is None:
        return dict(document)
    return {name: document[name] for name in sections if isinstance(document.get(name), dict)}


def _entries(evidence: Mapping[str, Any], sectioned: bool) -> list[tuple[tuple[str, ...], Any]]:
    if not sectioned:
        return [((str(key),), value) for key, value in evidence.items()]
    return [
        ((name, str(key)), value)
        for name, section in evidence.items()
        for key, value in section.items()
    ]


def _page_overhead(evidence: Mapping[str, Any], sectioned: bool) -> int:
    """Upper bound of a page's bytes beyond its entries (braces, section names)."""

    if not sectioned:
        return 2
    return 2 + sum(len(canonical_bytes(name)) + 4 for name in evidence)


def _escaped_cost(char: str) -> int:
    if char in '"\\':
        return 2
    if ord(char) < 0x20:
        return 6
    return len(char.encode("utf-8", "surrogatepass"))


def _chunks(text: str, budget: int) -> list[tuple[int, str, int]]:
    """Split ``text`` into (byte_offset, slice, slice_bytes) whose JSON string fits ``budget``."""

    out: list[tuple[int, str, int]] = []
    start = 0
    cost = 0
    byte_offset = 0
    chunk_start = 0
    for index, char in enumerate(text):
        char_cost = _escaped_cost(char)
        if cost + char_cost > budget and index > start:
            out.append((chunk_start, text[start:index], byte_offset - chunk_start))
            start = index
            chunk_start = byte_offset
            cost = 0
        cost += char_cost
        byte_offset += len(char.encode("utf-8", "surrogatepass"))
    out.append((chunk_start, text[start:], byte_offset - chunk_start))
    return out


def _layout(entries: list[tuple[tuple[str, ...], Any]], budget: int) -> list[list[_Unit]]:
    """Deterministic greedy pages: whole entries while they fit, else one entry split."""

    pages: list[list[_Unit]] = []
    current: list[_Unit] = []
    used = 0
    offset = 0
    for path, value in entries:
        encoded = canonical_bytes(value)
        key_bytes = len(canonical_bytes(path[-1]))
        cost = key_bytes + 2 + len(encoded)
        if current and used + cost > budget:
            pages.append(current)
            current = []
            used = 0
        if cost <= budget:
            current.append(_Unit(path, value, offset, 0, len(encoded), len(encoded), True))
            used += cost
        else:
            chunk_budget = budget - key_bytes - 4
            if chunk_budget < _MIN_CHUNK_BUDGET:
                raise _KeyExceedsCap(path)
            text = encoded.decode("utf-8", "surrogatepass")
            value_bytes = len(encoded)
            for value_offset, piece, piece_bytes in _chunks(text, chunk_budget):
                start = offset + value_offset
                unit = _Unit(path, piece, start, value_offset, piece_bytes, value_bytes, False)
                pages.append([unit])
        offset += len(encoded)
    if current:
        pages.append(current)
    return pages


def _page_document(
    units: Sequence[_Unit], evidence: Mapping[str, Any], sectioned: bool
) -> dict[str, Any]:
    page: dict[str, Any] = {name: {} for name in evidence} if sectioned else {}
    for unit in units:
        if sectioned:
            page[unit.path[0]][unit.path[1]] = unit.value
        else:
            page[unit.path[0]] = unit.value
    return page


def _encode_cursor(binding: Mapping[str, str], evidence_sha256: str, offset: int) -> str:
    payload = {
        "v": _CURSOR_VERSION, **binding, "evidence_sha256": evidence_sha256, "offset": offset
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: Any) -> dict[str, Any] | None:
    if not isinstance(cursor, str) or len(cursor) > CURSOR_MAX_CHARS:
        return None
    if not cursor.startswith(CURSOR_PREFIX):
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor[len(CURSOR_PREFIX) :].encode("ascii"))
        payload = json.loads(raw)
    except ValueError:  # binascii.Error, UnicodeError and JSONDecodeError are ValueErrors
        return None
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        return None
    for name in ("tool", "task_id", "card_sha256", "evidence_sha256"):
        if not isinstance(payload.get(name), str):
            return None
    offset = payload.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return None
    return payload


def page_evidence(
    document: Mapping[str, Any],
    *,
    tool: str,
    task_id: str,
    card_sha256: str,
    cursor: str = "",
    sections: Sequence[str] | None = None,
    cap: int | None = None,
) -> dict[str, Any]:
    """Bound one evidence render to ``cap`` bytes, paging the rest by cursor.

    ``document`` is the evidence-mode card (``sections=None``) or the status
    reply whose dict-valued ``sections`` are paged. Returns ``{"ok": True,
    "document", "meta"}`` -- the evidence itself when it fits (``meta`` empty),
    else the requested page and its paging fields -- or a named refusal.
    """

    limit = EVIDENCE_PAGE_CAP_BYTES if cap is None else cap
    sectioned = sections is not None
    evidence = _evidence_document(document, sections)
    encoded = canonical_bytes(evidence)
    if not cursor and len(encoded) <= limit:
        return {"ok": True, "document": evidence, "meta": {}}
    evidence_sha256 = hashlib.sha256(encoded).hexdigest()
    binding = {"tool": tool, "task_id": task_id, "card_sha256": card_sha256}
    offset = 0
    if cursor:
        payload = _decode_cursor(cursor)
        if payload is None:
            return _refusal(
                "evidence_cursor_invalid",
                "cursor is not an evidence continuation cursor",
                tool,
                task_id,
            )
        if payload["tool"] != tool or payload["task_id"] != task_id:
            return _refusal(
                "evidence_cursor_mismatch",
                "cursor was issued for another tool or task",
                tool,
                task_id,
                cursor_tool=payload["tool"][:80],
                cursor_task_id=payload["task_id"][:200],
            )
        if payload["card_sha256"] != card_sha256 or payload["evidence_sha256"] != evidence_sha256:
            return _refusal(
                "evidence_cursor_stale",
                "the card or its evidence changed since the cursor was issued",
                tool,
                task_id,
                card_sha256=card_sha256,
                cursor_card_sha256=payload["card_sha256"][:64],
                evidence_sha256=evidence_sha256,
                cursor_evidence_sha256=payload["evidence_sha256"][:64],
            )
        offset = payload["offset"]
    try:
        pages = _layout(_entries(evidence, sectioned), limit - _page_overhead(evidence, sectioned))
    except _KeyExceedsCap as exc:
        return _refusal(
            "evidence_key_exceeds_cap",
            "an evidence key name alone leaves no room for its value under the page cap",
            tool,
            task_id,
            path=[part[:200] for part in exc.path],
            cap_bytes=limit,
        )
    starts = {page[0].offset: index for index, page in enumerate(pages)}
    index = starts.get(offset)
    if index is None:
        return _refusal(
            "evidence_cursor_invalid", "cursor offset is not a page boundary", tool, task_id
        )
    units = pages[index]
    page = _page_document(units, evidence, sectioned)
    following = index + 1 < len(pages)
    meta: dict[str, Any] = {
        "evidence_truncated": True,
        "evidence_total_bytes": len(encoded),
        "evidence_sha256": evidence_sha256,
        "evidence_cap_bytes": limit,
        "evidence_page": index,
        "evidence_page_count": len(pages),
        "evidence_page_bytes": len(canonical_bytes(page)),
        "continuation_cursor": (
            _encode_cursor(binding, evidence_sha256, pages[index + 1][0].offset)
            if following
            else None
        ),
    }
    if sectioned:
        meta["evidence_sections"] = list(evidence)
    head = units[0]
    if not head.whole:
        meta["evidence_fragment"] = {
            "path": list(head.path),
            "offset": head.value_offset,
            "bytes": head.chunk_bytes,
            "value_bytes": head.value_bytes,
            "final": head.value_offset + head.chunk_bytes == head.value_bytes,
        }
    return {"ok": True, "document": page, "meta": meta}
