"""NF-2026-02356: reviewer candidate_overlay replies carry freshness for new,
unindexed and large files, and large files are readable in pages.

Measured root cause: a large ``mode=file`` window reply exceeds the outer cap
and is outer-paged, so ``content`` becomes an opaque byte slice.  The envelope
copied only the payload's bare string ``freshness`` ("fresh"), so no page --
and in particular no page after the first -- exposed ``freshness.state`` or a
source hash for the path, and the reviewer's step-1 check could not pass.

Fakes only: real temporary repos, no provider CLI and no manager loop.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from aiworkhub import quality_reviewer
from aiworkhub import repository_state
from aiworkhub import source_graph
from aiworkhub import worker_ai_tools_mcp as worker_tools

NEW_PY = "new_mod.py"
BIG_JSON = "evals/cases.json"


def _ctx(runtime: Path, *, repo: Path, authority_repo: Path, packet_path: Path):
    runtime.mkdir(parents=True, exist_ok=True)
    ledger = runtime / "audit.jsonl"
    fd = os.open(ledger, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.close(fd)
    key = runtime / "audit.key"
    key.write_bytes(b"k" * 32)
    return worker_tools.WorkerToolContext(
        task_id="REVIEW_TASK_1",
        runner="claude_sonnet5",
        topic="quality_review",
        request_id="c" * 32,
        repo=repo,
        authority_repo=authority_repo,
        source_graph_targets=(),
        session_topic="quality_review",
        audit_ledger_path=ledger,
        audit_hmac_key_path=key,
        quality_review_packet_path=packet_path,
    )


def _row(i: int) -> str:
    return json.dumps({"id": i, "prompt": f"eval case {i} " + "x" * 80, "answer": i * 7})


def _big_json(target_bytes: int) -> tuple[str, int]:
    rows: list[str] = []
    size = 2
    while size < target_bytes:
        rows.append(_row(len(rows)))
        size += len(rows[-1]) + 4
    # Line 1 is "[", row id k sits on line k + 2.
    return "[\n  " + ",\n  ".join(rows) + "\n]\n", len(rows)


def _setup(tmp_path: Path, json_bytes: int):
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    (canonical / "module.py").write_text("def base():\n    return 1\n", encoding="utf-8")
    repository_state.bootstrap_repository(canonical)
    source_graph.build_index(canonical, incremental=False)
    (candidate / "module.py").write_text("def base():\n    return 1\n", encoding="utf-8")
    (candidate / NEW_PY).write_bytes(b"def brand_new_symbol():\n    return 42\n")
    (candidate / "evals").mkdir()
    text, row_count = _big_json(json_bytes)
    (candidate / BIG_JSON).write_bytes(text.encode("utf-8"))
    hashes = {
        path: hashlib.sha256((candidate / path).read_bytes()).hexdigest()
        for path in (NEW_PY, BIG_JSON)
    }
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-2356",
        task_id="TARGET_TASK_2356",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes=hashes,
    )
    packet_path = runtime / "quality_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    prewarm = worker_tools.prewarm_quality_review_source_graph(
        packet_path, repo=candidate, authority_repo=canonical,
    )
    assert prewarm["ok"] is True
    ctx = _ctx(runtime, repo=candidate, authority_repo=canonical, packet_path=packet_path)
    worker_tools._CACHE.clear()
    return ctx, packet, hashes, row_count


def _assert_step1(reply: dict, packet: dict, path: str, sha: str) -> None:
    """The exact step-1 check of quality_reviewer._OVERLAY_INSPECTION_STEPS,
    applied to the envelope alone (what every page carries)."""

    assert reply["ok"] is True, reply
    assert reply["authority_source"] == "candidate_overlay"
    assert reply["packet_sha256"] == packet["packet_sha256"]
    fresh = reply.get("freshness")
    assert isinstance(fresh, dict), f"envelope freshness not a dict: {fresh!r}"
    assert fresh["state"] == "fresh"
    assert fresh["file_path"] == path
    assert fresh["indexed_source_hash"] == sha


def _read_pages(ctx, first: dict, **query) -> tuple[list[dict], bytes]:
    pages = [first]
    chunks = [first["content"].encode("utf-8")]
    cursor = first.get("continuation_cursor")
    while cursor:
        page = worker_tools.source_graph_query(ctx, continuation_cursor=cursor, **query)
        assert page["ok"] is True, page
        assert hashlib.sha256(page["content"].encode("utf-8")).hexdigest() == page["page_sha256"]
        pages.append(page)
        chunks.append(page["content"].encode("utf-8"))
        cursor = page.get("continuation_cursor")
        assert len(pages) <= 64
    return pages, b"".join(chunks)


@pytest.mark.parametrize("path", [NEW_PY, BIG_JSON])
def test_new_file_overlay_reply_carries_candidate_freshness(tmp_path: Path, path: str) -> None:
    ctx, packet, hashes, _rows = _setup(tmp_path, 300_000)

    reply = worker_tools.source_graph_query(ctx, mode="file", query=path, budget=64)

    _assert_step1(reply, packet, path, hashes[path])
    content = reply["content"]
    if isinstance(content, dict):
        context = content["contexts"][0]
        assert context["freshness"]["state"] == "fresh"
        assert context["file"]["source_hash"] == hashes[path]


@pytest.mark.parametrize("size", [300_000, 480_000])
def test_large_candidate_file_windows_are_paged_with_freshness_on_every_page(
    tmp_path: Path, size: int,
) -> None:
    ctx, packet, hashes, rows = _setup(tmp_path, size)
    sha = hashes[BIG_JSON]
    last_line = rows + 2
    windows = [(1, 120), (rows // 2, rows // 2 + 200), (last_line - 150, last_line)]
    for start, end in windows:
        query = {"mode": "file", "query": f"{start}-{end}", "target": BIG_JSON, "budget": 64}
        first = worker_tools.source_graph_query(ctx, **query)
        _assert_step1(first, packet, BIG_JSON, sha)
        # Measured: every window here exceeds the outer cap, so paging is asserted, not assumed.
        assert first.get("outer_truncated") and first.get("continuation_cursor"), first
        pages, full = _read_pages(ctx, first, **query)
        for page in pages:
            _assert_step1(page, packet, BIG_JSON, sha)
        assert len(pages) > 1, "a window this large must be outer-paged"
        assert hashlib.sha256(full).hexdigest() == first["content_sha256"]
        assert pages[-1].get("continuation_cursor") is None
        assert pages[-1]["page_count"] == len(pages)
        payload = json.loads(full)
        context = payload["contexts"][0]
        assert context["file"]["source_hash"] == sha
        preview = context["source_preview"]
        first_row = max(start - 2, 0)
        assert f"eval case {first_row} " in preview
        if context.get("source_preview_truncated"):
            nxt = context.get("source_preview_next_window")
            assert nxt and nxt["target"] == BIG_JSON, context
        else:
            last_row = min(end - 2, rows - 1)
            assert f"eval case {last_row} " in preview


def test_multi_file_or_canonical_reply_keeps_string_freshness() -> None:
    two_files = {
        "freshness": "fresh",
        "matches": [
            {"file_path": "a.py", "freshness": {"state": "fresh", "indexed_source_hash": "1"}},
            {"file_path": "b.py", "freshness": {"state": "fresh", "indexed_source_hash": "2"}},
        ],
    }
    assert worker_tools._single_file_freshness(two_files) is None
    no_hash = {"contexts": [{"file_path": "a.py", "freshness": {"state": "fresh"}}]}
    assert worker_tools._single_file_freshness(no_hash) is None
    elided = {
        "matches": [{"file_path": "a.py", "source_hash": "h", "freshness": {"state": "fresh"}}],
        "contexts": [{
            "file_path": "a.py",
            "file": {"source_hash": "h"},
            "freshness": {"state": "fresh", "indexed_source_hash": "h", "disk_source_hash": "h"},
        }],
    }
    assert worker_tools._single_file_freshness(elided) == {
        "state": "fresh", "file_path": "a.py",
        "indexed_source_hash": "h", "disk_source_hash": "h",
    }
    # Review finding: no row measured a disk hash, so none is reported (never copied from indexed).
    no_disk = {"contexts": [{"file_path": "a.py", "freshness": {"state": "fresh", "indexed_source_hash": "h"}}]}
    assert worker_tools._single_file_freshness(no_disk) == {
        "state": "fresh", "file_path": "a.py", "indexed_source_hash": "h",
    }
    two_disks = {"contexts": [
        {"file_path": "a.py", "freshness": {"state": "fresh", "indexed_source_hash": "h", "disk_source_hash": "d1"}},
        {"file_path": "a.py", "freshness": {"state": "fresh", "indexed_source_hash": "h", "disk_source_hash": "d2"}},
    ]}
    assert worker_tools._single_file_freshness(two_disks) is None
