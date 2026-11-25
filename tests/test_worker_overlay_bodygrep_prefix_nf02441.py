"""Worker bodygrep honours directory/prefix targets for live-overlay files (NF-2026-02441).

Canonical bodygrep scopes a non-exact target to ``target`` itself or anything
beneath ``target/`` (SQL ``file_path LIKE target/%``).  The worker overlay used
an equality test, so a directory-targeted bodygrep never returned a file the
worker had created in its own live worktree.  These tests pin the prefix scope
for the overlay, the component-wise boundary, and the unchanged exact-path case.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import repository_state
from aiworkhub import source_graph
from aiworkhub import worker_ai_tools_mcp as w

_PKG = "src/pkg"
_UNRELATED = "src/unrelated"
_LIVE = "src/pkg/live_nf02441.py"
_TRACKED_EDIT = "src/pkg/canonical_nf02441.py"
_CANONICAL = {
    _TRACKED_EDIT: b"def canonical_symbol_nf02441():\n    return 'canonical'\n",
    "src/unrelated/other_nf02441.py": b"def other_symbol_nf02441():\n    return 'unrelated'\n",
}
_LIVE_BODY = b"def live_symbol_nf02441():\n    return 'live_marker_nf02441'\n"


@pytest.fixture(autouse=True)
def _isolated_caches():
    w._rework_overlay_view_cache_clear()
    w._source_graph_cache_clear()
    yield
    w._rework_overlay_view_cache_clear()
    w._source_graph_cache_clear()


def _ctx(tmp_path: Path) -> w.WorkerToolContext:
    authority = tmp_path / "authority"
    workspace = tmp_path / "workspace"
    for root in (authority, workspace):
        root.mkdir()
        for relative, data in _CANONICAL.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_bytes(data)
    repository_state.bootstrap_repository(authority)
    repository_state.bootstrap_repository(workspace)
    source_graph.build_index(authority, incremental=False)
    allowed = ("src/pkg/*.py",)
    return w.WorkerToolContext(
        task_id="nf02441-task",
        runner="runner",
        topic="task_mcp",
        request_id="nf02441-request",
        repo=workspace,
        authority_repo=authority,
        source_graph_targets=allowed,
        allowed_writes=allowed,
        session_topic="task_mcp",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        contract_packet_path=None,
        rework_overlay_packet=None,
        rework_overlay_packet_path=None,
    )


def _query(ctx: w.WorkerToolContext, mode: str, query: str, **kwargs) -> dict:
    reply = w.source_graph_query(ctx, mode=mode, query=query, budget=16, **kwargs)
    assert reply["ok"] is True, reply
    content = reply["content"]
    return content if isinstance(content, dict) else json.loads(content)


def _paths(content: dict) -> set[str]:
    return {
        str(row.get("file_path") or "")
        for row in content.get("matches") or []
        if isinstance(row, dict)
    }


def test_bodygrep_directory_target_returns_a_new_live_file(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (ctx.repo / _LIVE).write_bytes(_LIVE_BODY)

    grep = _query(ctx, "bodygrep", "live_marker_nf02441", target=f"{_PKG}/")

    assert _LIVE in _paths(grep)
    assert "live_marker_nf02441" in json.dumps(grep)


def test_bodygrep_directory_target_without_trailing_slash_returns_the_live_file(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    (ctx.repo / _LIVE).write_bytes(_LIVE_BODY)

    grep = _query(ctx, "bodygrep", "live_marker_nf02441", target=_PKG)

    assert _LIVE in _paths(grep)


def test_bodygrep_unrelated_directory_target_excludes_the_live_file(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    (ctx.repo / _LIVE).write_bytes(_LIVE_BODY)

    grep = _query(ctx, "bodygrep", "live_marker_nf02441", target=f"{_UNRELATED}/")

    assert _LIVE not in _paths(grep)


def test_bodygrep_exact_path_target_still_returns_the_live_edit(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (ctx.repo / _TRACKED_EDIT).write_bytes(
        b"def canonical_symbol_nf02441():\n    return 'edited_marker_nf02441'\n",
    )

    exact = _query(ctx, "bodygrep", "edited_marker_nf02441", target=_TRACKED_EDIT)
    assert _TRACKED_EDIT in _paths(exact)

    other = _query(ctx, "bodygrep", "edited_marker_nf02441", target=f"{_UNRELATED}/")
    assert _TRACKED_EDIT not in _paths(other)
