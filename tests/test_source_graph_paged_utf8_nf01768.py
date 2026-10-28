from __future__ import annotations

import base64
import hashlib
import json

import pytest

from aiworkhub import worker_ai_tools_mcp as w

_META = {"ok": True, "tool": "source_graph", "mode": "file", "internal_truncated": False}
_STORE = "e" * 32


def _canonical(text: str) -> bytes:
    return json.dumps({"mode": "file", "text": text}, ensure_ascii=False).encode("utf-8")


def _walk_pages(canonical: bytes, chunk_size: int) -> list[dict]:
    sha = hashlib.sha256(canonical).hexdigest()
    bounds = w._continuation_page_bounds(canonical, chunk_size)
    pages = []
    start = 0
    for index in range(len(bounds)):
        page, returned = w._continuation_page_result(
            store_id=_STORE,
            page_index=index,
            start=start,
            meta=_META,
            canonical_bytes=canonical,
            chunk_size=chunk_size,
            page_count=len(bounds),
            content_sha256=sha,
        )
        assert returned == page["bytes"]
        pages.append(page)
        cursor = page["continuation_cursor"]
        if cursor is None:
            assert index == len(bounds) - 1
            break
        fields = w._decode_continuation_cursor(cursor)
        assert fields["page_index"] == index + 1
        start = fields["offset"]
        assert start == bounds[index + 1][0]
    return pages


def _page_bytes(page: dict) -> bytes:
    if page["content_encoding"] == "utf-8":
        return page["content"].encode("utf-8")
    assert page["content_encoding"] == "base64"
    return base64.b64decode(page["content"], validate=True)


@pytest.mark.parametrize(
    "text",
    ["ქართული ტექსტი " * 400, "😀🚀🎉 emoji " * 400, "ა😀b" * 700],
    ids=["georgian_3_byte", "emoji_4_byte", "mixed"],
)
@pytest.mark.parametrize("chunk_size", [101, 512, 1000, 1001, 1002, 1003])
def test_multibyte_payload_pages_as_utf8_without_splitting_a_code_point(text, chunk_size):
    canonical = _canonical(text)
    assert len(canonical) > chunk_size
    pages = _walk_pages(canonical, chunk_size)

    assert len(pages) > 1
    assert all(page["page_count"] == len(pages) for page in pages)
    assert [page["page_index"] for page in pages] == list(range(len(pages)))
    for page in pages:
        assert page["content_encoding"] == "utf-8"
        raw = page["content"].encode("utf-8")
        assert len(raw) == page["bytes"] <= chunk_size
        assert hashlib.sha256(raw).hexdigest() == page["page_sha256"]
    assert pages[-1]["continuation_cursor"] is None

    reassembled = b"".join(_page_bytes(page) for page in pages)
    assert reassembled == canonical
    assert hashlib.sha256(reassembled).hexdigest() == pages[0]["content_sha256"]
    assert "".join(page["content"] for page in pages) == canonical.decode("utf-8")


def test_page_cut_moves_back_at_most_three_bytes():
    canonical = ("ა" * 100).encode("utf-8")
    for chunk_size in range(8, 40):
        for start, end in w._continuation_page_bounds(canonical, chunk_size):
            assert end - start >= chunk_size - 3 or end == len(canonical)
            assert end - start <= chunk_size


def test_page_offsets_follow_the_actual_cut_not_page_index_times_chunk_size():
    canonical = ("ა" * 100).encode("utf-8")
    bounds = w._continuation_page_bounds(canonical, 10)
    assert bounds[0] == (0, 9)
    assert bounds[1][0] == 9 != 1 * 10
    assert bounds[-1][1] == len(canonical)
    assert all(a[1] == b[0] for a, b in zip(bounds, bounds[1:]))


def test_non_utf8_payload_still_pages_as_base64():
    canonical = bytes(range(256)) * 8
    pages = _walk_pages(canonical, 300)

    assert len(pages) > 1
    assert all(page["content_encoding"] == "base64" for page in pages)
    reassembled = b"".join(_page_bytes(page) for page in pages)
    assert reassembled == canonical
    assert hashlib.sha256(reassembled).hexdigest() == pages[0]["content_sha256"]
    for page in pages:
        assert hashlib.sha256(_page_bytes(page)).hexdigest() == page["page_sha256"]


def test_chunk_size_for_cap_pages_fit_the_cap_as_utf8():
    canonical = _canonical("ქართული 😀 " * 600)
    sha = hashlib.sha256(canonical).hexdigest()
    cap = 2048
    chunk_size = w._continuation_chunk_size_for_cap(
        store_id=_STORE, meta=_META, canonical_bytes=canonical,
        content_sha256=sha, output_cap_bytes=cap,
    )
    pages = _walk_pages(canonical, chunk_size)
    assert len(pages) > 1
    assert all(w._serialized_response_bytes(page) <= cap for page in pages)
    assert b"".join(_page_bytes(page) for page in pages) == canonical


def test_source_graph_payload_accepts_utf8_and_base64_complete_pages():
    payload = {"mode": "body", "matches": [{"file_path": "a.py", "source": "ა = 1"}]}
    canonical = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sha = hashlib.sha256(canonical).hexdigest()
    page, _ = w._continuation_page_result(
        store_id=_STORE, page_index=0, start=0, meta=_META,
        canonical_bytes=canonical, chunk_size=len(canonical),
        page_count=1, content_sha256=sha,
    )
    assert page["content_encoding"] == "utf-8"
    assert w._source_graph_payload(page) == payload

    as_base64 = {
        **page,
        "content": base64.b64encode(canonical).decode("ascii"),
        "content_encoding": "base64",
    }
    assert w._source_graph_payload(as_base64) == payload

    partial, _ = w._continuation_page_result(
        store_id=_STORE, page_index=0, start=0, meta=_META,
        canonical_bytes=canonical, chunk_size=20,
        page_count=3, content_sha256=sha,
    )
    assert w._source_graph_payload(partial) is None
