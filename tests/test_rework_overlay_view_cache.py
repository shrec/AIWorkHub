"""Prepared rework overlay view memoization (NF-2026-01875).

Every worker ``source_graph_query`` during a rework used to rebuild the overlay
view from scratch, re-running ``source_graph_ast.extract_file`` for every
packet file differing from canonical.  Measured in the worker sandbox on three
repository files (441 KB + 105 KB + 51 KB): 8.1-11.6 s per uncached prepare,
almost all AST extraction (``_walk_lexical_scope``), against ~2.5 ms to read
and hash the same files.  These tests pin the memoized replacement: an
unchanged snapshot never re-extracts, any modified file rebuilds, the cached
reply equals the uncached one, a refused admission is never cached, and the
cache stays bounded.  :func:`test_rework_overlay_view_cache_timing_report`
prints the before/after per-call numbers.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from aiworkhub import repository_state
from aiworkhub import source_graph
from aiworkhub import source_graph_ast
from aiworkhub import worker_ai_tools_mcp as w
from aiworkhub.worker_workspace import materialize_rework_overlay

_ROOT = Path(__file__).resolve().parents[1]
_MOD_A = "src/mod_a.py"
_MOD_B = "src/mod_b.py"


@pytest.fixture(autouse=True)
def _isolated_caches():
    w._rework_overlay_view_cache_clear()
    w._source_graph_cache_clear()
    yield
    w._rework_overlay_view_cache_clear()
    w._source_graph_cache_clear()


def _build(
    tmp_path: Path,
    canonical: dict[str, bytes],
    edited: dict[str, bytes],
    *,
    sealed: bool = True,
) -> w.WorkerToolContext:
    authority = tmp_path / "authority"
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    for path in (authority, workspace, runtime):
        path.mkdir()
    for relative, data in canonical.items():
        (authority / relative).parent.mkdir(parents=True, exist_ok=True)
        (authority / relative).write_bytes(data)
    repository_state.bootstrap_repository(authority)
    repository_state.bootstrap_repository(workspace)
    source_graph.build_index(authority, incremental=False)
    entries: list[tuple[str, str | None, bytes | None]] = []
    for relative, data in sorted(edited.items()):
        (workspace / relative).parent.mkdir(parents=True, exist_ok=True)
        (workspace / relative).write_bytes(data)
        entries.append((
            relative, hashlib.sha256(data).hexdigest(), data if sealed else None,
        ))
    packet = json.loads(materialize_rework_overlay(
        "successor-request", "same-task", "predecessor-request", "same-task",
        authority, entries,
    ))
    packet_path = runtime / "rework_overlay.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    paths = tuple(sorted(edited))
    return w.WorkerToolContext(
        task_id="same-task",
        runner="runner",
        topic="task_mcp",
        request_id="successor-request",
        repo=workspace,
        authority_repo=authority,
        source_graph_targets=paths,
        allowed_writes=paths,
        session_topic="task_mcp",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        rework_overlay_packet=packet,
        rework_overlay_packet_path=packet_path,
    )


def _small_ctx(tmp_path: Path, *, sealed: bool = True) -> w.WorkerToolContext:
    return _build(
        tmp_path,
        {
            _MOD_A: b"def canonical_alpha():\n    return 'a'\n",
            _MOD_B: b"def canonical_beta():\n    return 'b'\n",
        },
        {
            _MOD_A: b"def retained_alpha_symbol():\n    return 'a2'\n",
            _MOD_B: b"def retained_beta_symbol():\n    return 'b2'\n",
        },
        sealed=sealed,
    )


def _count_extracts(
    monkeypatch: pytest.MonkeyPatch, workspace: Path,
) -> list[str]:
    """Record every overlay extraction of a workspace file, by relative path."""

    calls: list[str] = []
    original = source_graph_ast.extract_file
    root = workspace.resolve()

    def counting(repo_root, file_path, *, build_revision):
        resolved = Path(file_path).resolve()
        if resolved.is_relative_to(root):
            calls.append(resolved.relative_to(root).as_posix())
        return original(repo_root, file_path, build_revision=build_revision)

    monkeypatch.setattr(source_graph_ast, "extract_file", counting)
    return calls


def _query(ctx: w.WorkerToolContext, query: str, *, mode: str = "focus") -> dict:
    reply = w.source_graph_query(ctx, mode=mode, query=query, workflow_stage="rework")
    assert reply["ok"] is True, reply
    return reply


def _content(reply: dict) -> dict:
    content = reply["content"]
    return content if isinstance(content, dict) else json.loads(content)


def _canonical_db(ctx: w.WorkerToolContext) -> Path:
    return w._canonical_source_graph_binding(ctx).db_path


def test_unchanged_snapshot_reextracts_nothing_on_second_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _small_ctx(tmp_path)
    calls = _count_extracts(monkeypatch, ctx.repo)

    first = _query(ctx, "retained_alpha_symbol")
    assert sorted(calls) == [_MOD_A, _MOD_B]
    assert "retained_alpha_symbol" in json.dumps(_content(first))

    calls.clear()
    second = _query(ctx, "retained_beta_symbol")
    assert calls == []
    assert "retained_beta_symbol" in json.dumps(_content(second))
    assert w._rework_overlay_view_cache_stats()["hits"] >= 1


def test_modified_overlay_file_rebuilds_and_reply_reflects_new_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _small_ctx(tmp_path)
    calls = _count_extracts(monkeypatch, ctx.repo)
    _query(ctx, "retained_alpha_symbol")

    calls.clear()
    (ctx.repo / _MOD_A).write_bytes(
        b"def later_alpha_symbol():\n    return 'a3'\n",
    )
    reply = _query(ctx, "later_alpha_symbol")
    assert _MOD_A in calls
    rendered = json.dumps(_content(reply))
    assert "later_alpha_symbol" in rendered

    # The rebuilt view is itself reused while nothing changes again.
    calls.clear()
    again = _query(ctx, "retained_beta_symbol")
    assert calls == []
    assert "retained_beta_symbol" in json.dumps(_content(again))


def test_cached_reply_equals_uncached_reply_for_same_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _small_ctx(tmp_path)
    calls = _count_extracts(monkeypatch, ctx.repo)

    uncached = _query(ctx, "retained_alpha_symbol")
    assert sorted(calls) == [_MOD_A, _MOD_B]
    # Drop only the reply cache so the second call recomposes the reply from
    # the memoized view rather than replaying the first reply's bytes.
    w._source_graph_cache_clear()
    calls.clear()
    cached = _query(ctx, "retained_alpha_symbol")
    assert calls == []
    assert w._rework_overlay_view_cache_stats()["hits"] >= 1
    assert _content(cached) == _content(uncached)

    db_path = _canonical_db(ctx)
    hit_view = w._prepare_rework_overlay_view(
        ctx, db_path, build_revision=source_graph.BUILD_REVISION,
    )
    w._rework_overlay_view_cache_clear()
    fresh_view = w._prepare_rework_overlay_view(
        ctx, db_path, build_revision=source_graph.BUILD_REVISION,
    )
    assert hit_view == fresh_view
    for mode, query, target in (
        ("focus", "retained", None),
        ("body", "retained_beta_symbol", None),
        ("file", _MOD_A, _MOD_A),
        ("bodygrep", "return", None),
    ):
        from_hit, _ = w._merge_rework_overlay_payload(
            ctx, {"matches": []}, hit_view,
            mode=mode, query=query, target=target, budget=16,
        )
        from_fresh, _ = w._merge_rework_overlay_payload(
            ctx, {"matches": []}, fresh_view,
            mode=mode, query=query, target=target, budget=16,
        )
        assert json.dumps(from_hit, sort_keys=True, default=str) == json.dumps(
            from_fresh, sort_keys=True, default=str,
        )


def test_failed_admission_is_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _small_ctx(tmp_path, sealed=False)
    good = (ctx.repo / _MOD_A).read_bytes()
    (ctx.repo / _MOD_A).write_bytes(b"def tampered():\n    return 0\n")
    calls = _count_extracts(monkeypatch, ctx.repo)
    db_path = _canonical_db(ctx)

    with pytest.raises(w.WorkerToolError, match="rework_overlay_hash_mismatch"):
        w._prepare_rework_overlay_view(
            ctx, db_path, build_revision=source_graph.BUILD_REVISION,
        )
    refused = w.source_graph_query(
        ctx, mode="focus", query="retained_alpha_symbol", workflow_stage="rework",
    )
    assert refused["ok"] is False
    assert "rework_overlay_hash_mismatch" in json.dumps(refused)
    assert w._rework_overlay_view_cache_stats()["entries"] == 0

    (ctx.repo / _MOD_A).write_bytes(good)
    calls.clear()
    reply = _query(ctx, "retained_alpha_symbol")
    assert sorted(calls) == [_MOD_A, _MOD_B]
    assert "retained_alpha_symbol" in json.dumps(_content(reply))
    assert w._rework_overlay_view_cache_stats()["entries"] == 1

    # A cached success never masks a later refusal: admission re-runs.
    (ctx.repo / _MOD_A).write_bytes(b"def tampered_again():\n    return 0\n")
    with pytest.raises(w.WorkerToolError, match="rework_overlay_hash_mismatch"):
        w._prepare_rework_overlay_view(
            ctx, db_path, build_revision=source_graph.BUILD_REVISION,
        )


def test_file_rewritten_during_build_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _small_ctx(tmp_path)
    original = source_graph_ast.extract_file
    target = (ctx.repo / _MOD_A).resolve()

    def racing(repo_root, file_path, *, build_revision):
        # A write landing between admission and extraction: the built view
        # holds the new bytes while the key still names the admitted ones.
        if Path(file_path).resolve() == target:
            target.write_bytes(b"def raced_alpha_symbol():\n    return 'r'\n")
        return original(repo_root, file_path, build_revision=build_revision)

    monkeypatch.setattr(source_graph_ast, "extract_file", racing)
    w._prepare_rework_overlay_view(
        ctx, _canonical_db(ctx), build_revision=source_graph.BUILD_REVISION,
    )
    assert w._rework_overlay_view_cache_stats()["entries"] == 0


def test_view_cache_is_bounded_and_keyed_on_build_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _small_ctx(tmp_path)
    calls = _count_extracts(monkeypatch, ctx.repo)
    db_path = _canonical_db(ctx)
    limit = w._MAX_REWORK_OVERLAY_VIEW_CACHE_ENTRIES
    for index in range(limit + 1):
        w._prepare_rework_overlay_view(ctx, db_path, build_revision=f"rev-{index}")
    assert len(calls) == 2 * (limit + 1)
    assert w._rework_overlay_view_cache_stats()["entries"] == limit

    calls.clear()
    w._prepare_rework_overlay_view(ctx, db_path, build_revision=f"rev-{limit}")
    assert calls == []
    w._prepare_rework_overlay_view(ctx, db_path, build_revision="rev-0")
    assert sorted(calls) == [_MOD_A, _MOD_B]


def test_rework_overlay_view_cache_timing_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    relative = "src/aiworkhub/worker_ai_tools_mcp.py"
    data = (_ROOT / relative).read_bytes()
    ctx = _build(
        tmp_path,
        {relative: data},
        {relative: data + b"\ndef overlay_marker_symbol():\n    return 1\n"},
    )
    calls = _count_extracts(monkeypatch, ctx.repo)
    db_path = _canonical_db(ctx)

    def timed() -> float:
        started = time.perf_counter()
        w._prepare_rework_overlay_view(
            ctx, db_path, build_revision=source_graph.BUILD_REVISION,
        )
        return time.perf_counter() - started

    uncached = timed()
    assert calls == [relative]
    cached = [timed() for _ in range(3)]
    assert calls == [relative]
    w._source_graph_cache_clear()
    started = time.perf_counter()
    _query(ctx, "overlay_marker_symbol")
    query_cached = time.perf_counter() - started
    assert calls == [relative]
    assert max(cached) < uncached
    with capsys.disabled():
        print(
            "\nrework overlay view prepare "
            f"({len(data)} byte file): uncached {uncached:.3f}s "
            f"(extract_file dominated), cached "
            f"{', '.join(f'{value * 1000:.1f}ms' for value in cached)}; "
            f"full source_graph_query on a cached view {query_cached:.3f}s"
        )
