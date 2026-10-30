"""NF-2026-01540: a truncated literal zero must keep scanning, not become noise.

``bodygrep_query`` caps a page by scanned bytes, so a literal that lives past
the first page came back as a zero with ``next_cursor`` set.  The server
zero-hit cascade treated that page-1 zero as absence and replaced it with a
token fallback whose cursor the tool then refused.  The tool now follows the
literal cursor server-side, never runs the token fallback before the literal
walk completed, and resumes a fallback cursor under the kind that minted it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph as sg  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as w  # noqa: E402
from aiworkhub.repository_state import bootstrap_repository  # noqa: E402
from source_graph_content_helpers import content_payload  # noqa: E402

_NEEDLE = "nf01540_far_literal_marker"
_PHRASE = "gamma delta"
_PHRASE_LINE = "# gamma then delta\n"
_HIT_FILE = "zz/hit.py"
_FILLER_FILES = 24
_FILLER_LINES = 3600
# The tool clamps budget to MIN_BUDGET=8, so its engine cap is 2 MiB and ~290 KB
# filler files put seven files on a tool page; the engine-direct setup check
# uses budget=1 (1 MiB, three files) and the far literal still lands later.
_BUDGET = 1


@pytest.fixture(autouse=True)
def _uncached_bodygrep(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests assert byte-cap paging; a cached file costs no disk bytes and
    # never pages (NF-2026-01820), so every file here is read from disk.
    monkeypatch.setattr(sg, "BODYGREP_FILE_CACHE_MAX_FILE_BYTES", 0)
    # The worker walk is under test, so the core never continues an empty
    # truncated page on its own (NF-2026-01851).
    monkeypatch.setattr(sg, "BODYGREP_EXTRA_PAGES", 0)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def far_literal_repo(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("bodygrep_cascade_nf01540") / "repo"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="bodygrep_cascade_nf01540")
    padding = "# " + "p" * 78 + "\n"
    for index in range(_FILLER_FILES):
        head = f"# filler {index:02d}\n"
        if index in (0, 7):
            head += _PHRASE_LINE
        _write(repo / f"pkg/fill_{index:02d}.py", head + padding * _FILLER_LINES)
    _write(repo / _HIT_FILE, f'MARKER = "{_NEEDLE}"\n')
    sg.build_index(repo, incremental=False)
    return repo


@pytest.fixture
def ctx(far_literal_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w._source_graph_cache_clear()
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    if hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)
    runtime = w.generate_worker_mcp_runtime(
        home=tmp_path / "home", request_id="req_nf01540", task_id="TASK_NF01540",
        runner="claude_nf01540", topic="task_mcp", repo=far_literal_repo,
        authority_repo=far_literal_repo, source_graph_targets=[],
        session_topic="NF-2026-01540 bodygrep cascade",
        package_import_root=w.resolve_host_package_import_root(),
    )
    return w.WorkerToolContext(
        task_id="TASK_NF01540", runner="claude_nf01540", topic="task_mcp",
        request_id="req_nf01540", repo=far_literal_repo,
        authority_repo=far_literal_repo, source_graph_targets=(),
        session_topic="NF-2026-01540 bodygrep cascade",
        audit_ledger_path=runtime.audit_ledger_path,
        audit_hmac_key_path=runtime.audit_hmac_key_path,
    )


def _query(ctx, query: str, cursor: str | None = None) -> dict:
    return w.source_graph_query(
        ctx, mode="bodygrep", query=query, budget=_BUDGET, cursor=cursor,
    )


def test_setup_puts_the_literal_beyond_the_first_page(far_literal_repo: Path) -> None:
    first = sg.bodygrep_query(far_literal_repo, _NEEDLE, _BUDGET)
    assert first["matches"] == []
    assert first["scan_truncated"] is True
    assert first["next_cursor"]


def test_literal_beyond_first_page_is_returned_as_a_literal_hit(ctx) -> None:
    result = _query(ctx, _NEEDLE)

    assert result["ok"] is True
    assert result["hit_count"] == 1
    assert result["retrieval_reason"].startswith("bodygrep_literal_resumed:")
    assert int(result["retrieval_reason"].rsplit(":", 1)[1]) >= 2
    rows = content_payload(result)["matches"]
    assert [row["file_path"] for row in rows] == [_HIT_FILE]
    assert "match_kind" not in rows[0]


def test_incomplete_bounded_walk_returns_resumable_scan_incomplete(
    ctx, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(w, "_BODYGREP_LITERAL_WALK_MAX_PAGES", 2)

    result = _query(ctx, _NEEDLE)

    assert result["ok"] is True
    assert result["hit_count"] == 0
    assert result["retrieval_reason"] == "bodygrep_literal_scan_incomplete"
    body = content_payload(result)
    assert body["matches"] == []
    assert "match_kind" not in body
    cursor = result["next_cursor"]
    assert cursor

    for _ in range(10):
        page = _query(ctx, _NEEDLE, cursor=cursor)
        assert page["ok"] is True
        rows = content_payload(page)["matches"]
        if rows:
            assert [row["file_path"] for row in rows] == [_HIT_FILE]
            assert "match_kind" not in rows[0]
            return
        assert page["retrieval_reason"] == "bodygrep_literal_scan_incomplete"
        cursor = page["next_cursor"]
        assert cursor
    pytest.fail("the literal was not reached by following the incomplete cursor")


def test_completed_literal_zero_still_runs_the_token_fallback(ctx) -> None:
    result = _query(ctx, _PHRASE)

    assert result["ok"] is True
    assert result["retrieval_reason"] == "fallback:bodygrep_token_and_line"
    rows = content_payload(result)["matches"]
    assert rows
    assert {row["match_kind"] for row in rows} == {"token_and_line"}
    assert rows[0]["file_path"] == "pkg/fill_00.py"


def test_token_fallback_cursor_resumes_through_the_tool(ctx) -> None:
    first = _query(ctx, _PHRASE)
    cursor = first["next_cursor"]
    assert cursor

    resumed = _query(ctx, _PHRASE, cursor=cursor)

    assert resumed["ok"] is True
    assert resumed["retrieval_reason"] == "fallback:bodygrep_token_and_line"
    rows = content_payload(resumed)["matches"]
    assert [row["file_path"] for row in rows] == ["pkg/fill_07.py"]
    assert rows[0]["match_kind"] == "token_and_line"


def test_cursor_no_kind_accepts_is_still_invalid_cursor(ctx) -> None:
    result = _query(ctx, _NEEDLE, cursor="00-0-0000000000000000")

    assert result["ok"] is False
    assert result["reason"] == "invalid_cursor"
