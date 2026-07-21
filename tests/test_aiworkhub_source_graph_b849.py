"""Regression checks for CLAUDE_SONNET5_AIWORKHUB_SOURCE_GRAPH_CANONICAL_MIGRATION_B849_V1.

Verifies the canonical AIWorkHub Source Graph (tools/geoai-task-mcp/src/
aiworkhub/source_graph.py + source_graph_ast.py + source_graph_migration.py)
is the sole implementation and storage authority: repository-identity-bound
database resolution under ``<repo>/.aiworkhub/source_graph``, AST-first
evidence extraction with EXTRACTED/INFERRED/AMBIGUOUS labels, incremental
indexing that removes stale entities/edges on rename/delete, bounded graph
traversal, verified migration + idempotent cutover, and that the two
production callers (``project_context.py``, ``worker_ai_tools_mcp.py``) query
it directly in-process rather than shelling out to ``AITools/source_graph.py``.

Every test isolates state under its own ``tmp_path`` (a fresh bootstrapped
repository per test, never a shared database or shared files), so this file
is safe to run in a parallel test pool.

Run: PYTHONPATH=tools/geoai-task-mcp/src python3 -m pytest -q
     tools/geoai-task-mcp/tests/test_aiworkhub_source_graph_b849.py
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from aiworkhub import project_context as pc
from aiworkhub import source_graph as sg
from aiworkhub import source_graph_ast as sgast
from aiworkhub import source_graph_migration as sgm
from aiworkhub import worker_ai_tools_mcp as w
from aiworkhub.repository_state import HUB_DIRNAME, bootstrap_repository
from aiworkhub.storage_registry import load_storage_registry


def _new_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Repository-identity-bound database resolution
# ---------------------------------------------------------------------------

def test_db_path_resolves_under_repo_local_aiworkhub_dir(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    db_path = sg.resolve_db_path(repo)
    assert db_path == repo / HUB_DIRNAME / "source_graph" / "source_graph.sqlite"
    assert db_path.is_relative_to(repo)


def test_db_path_requires_manifest_never_falls_back_to_cwd(tmp_path, monkeypatch):
    unmanaged = tmp_path / "no_manifest"
    unmanaged.mkdir()
    monkeypatch.chdir(unmanaged)
    with pytest.raises(sg.RepositoryUnresolvedError):
        sg.resolve_db_path(unmanaged)


def test_multi_repository_isolation_distinct_paths_no_cross_contamination(tmp_path):
    repo_a = _new_repo(tmp_path, "repo_a")
    repo_b = _new_repo(tmp_path, "repo_b")
    _write(repo_a / "pkg" / "a_only.py", "def only_in_a():\n    return 1\n")
    _write(repo_b / "pkg" / "b_only.py", "def only_in_b():\n    return 2\n")

    sg.build_index(repo_a, incremental=True)
    sg.build_index(repo_b, incremental=True)

    db_a = sg.resolve_db_path(repo_a)
    db_b = sg.resolve_db_path(repo_b)
    assert db_a != db_b
    assert db_a.is_relative_to(repo_a)
    assert db_b.is_relative_to(repo_b)

    conn_a = sg.connect(db_a)
    conn_b = sg.connect(db_b)
    try:
        assert sg.find(conn_a, "only_in_a")
        assert not sg.find(conn_a, "only_in_b")
        assert sg.find(conn_b, "only_in_b")
        assert not sg.find(conn_b, "only_in_a")
    finally:
        conn_a.close()
        conn_b.close()


# ---------------------------------------------------------------------------
# AST-first evidence: EXTRACTED / INFERRED / AMBIGUOUS, provenance fields
# ---------------------------------------------------------------------------

def test_ast_extraction_labels_evidence_with_full_provenance(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "pkg" / "core.py"
    _write(target, (
        "def resolvable():\n"
        "    return 1\n\n"
        "def caller():\n"
        "    resolvable()\n"          # EXTRACTED: name bound in this module
        "    unknown_symbol()\n"      # AMBIGUOUS: never bound in this module
        "    obj.method_call()\n"     # INFERRED: attribute call, receiver unknown
        "    return None\n"
    ))
    extraction = sgast.extract_file(repo, target, build_revision="test-rev")
    assert extraction.status == "ok"
    assert extraction.language == "python"
    assert len(extraction.source_hash) == 64

    call_edges = {e.dst_name: e for e in extraction.edges if e.kind == "calls"}
    assert call_edges["resolvable"].evidence_label == sgast.EXTRACTED
    assert call_edges["resolvable"].confidence == 1.0
    assert call_edges["unknown_symbol"].evidence_label == sgast.AMBIGUOUS
    assert call_edges["method_call"].evidence_label == sgast.INFERRED

    for edge in extraction.edges:
        assert edge.extractor == sgast.EXTRACTOR_ID
        assert edge.file_path == "pkg/core.py"
        assert edge.line >= 1
        assert edge.source_hash == extraction.source_hash
        assert edge.build_revision == "test-rev"
    for entity in extraction.entities:
        assert entity.extractor == sgast.EXTRACTOR_ID
        assert entity.line_start >= 1
        assert entity.evidence_label == sgast.EXTRACTED  # def/class/import are always directly observed


def test_unsupported_language_is_explicit_fail_closed_not_regex_approximated(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "pkg" / "native.c"
    _write(target, "int main(void) { return 0; }\n")
    extraction = sgast.extract_file(repo, target, build_revision="test-rev")
    assert extraction.status == "unsupported_fail_closed"
    assert extraction.entities == ()
    assert extraction.edges == ()


def test_python_syntax_error_is_explicit_fail_closed(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "pkg" / "broken.py"
    _write(target, "def broken(:\n    pass\n")
    extraction = sgast.extract_file(repo, target, build_revision="test-rev")
    assert extraction.status == "parse_error_fail_closed"
    assert extraction.entities == ()
    assert extraction.edges == ()


def test_no_llm_network_or_second_graph_product_import(tmp_path):
    import ast as _ast

    import aiworkhub.source_graph as sg_mod
    import aiworkhub.source_graph_ast as sgast_mod
    import aiworkhub.source_graph_migration as sgm_mod

    banned_modules = {"requests", "urllib", "urllib3", "openai", "httpx", "socket", "http.client"}
    for mod in (sg_mod, sgast_mod, sgm_mod):
        tree = _ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, _ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not (names & banned_modules), (mod.__name__, names)


# ---------------------------------------------------------------------------
# Incremental indexing: stale-edge invalidation on change / rename / delete
# ---------------------------------------------------------------------------

def test_incremental_build_skips_unchanged_reindexes_changed(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "a.py", "def a():\n    return 1\n")
    r1 = sg.build_index(repo, incremental=True)
    assert r1.files_changed == 1 and r1.files_unchanged == 0

    r2 = sg.build_index(repo, incremental=True)
    assert r2.files_changed == 0 and r2.files_unchanged == 1

    _write(repo / "pkg" / "a.py", "def a():\n    return 2\n\ndef b():\n    return 3\n")
    r3 = sg.build_index(repo, incremental=True)
    assert r3.files_changed == 1 and r3.files_unchanged == 0


def test_rename_regression_no_stale_edge_survives(tmp_path):
    """The exact regression this card guards against: renaming or deleting a
    file must never leave its old entities/edges queryable afterward."""

    repo = _new_repo(tmp_path, "repo")
    old_path = repo / "pkg" / "old_name.py"
    _write(old_path, "def renamed_function():\n    return 1\n")
    sg.build_index(repo, incremental=True)

    db_path = sg.resolve_db_path(repo)
    conn = sg.connect(db_path)
    try:
        assert sg.func(conn, "renamed_function")
        assert sg.context(conn, "pkg/old_name.py")["found"] is True
    finally:
        conn.close()

    old_path.unlink()
    new_path = repo / "pkg" / "new_name.py"
    _write(new_path, "def renamed_function():\n    return 1\n")
    report = sg.build_index(repo, incremental=True)
    assert report.files_removed == 1
    assert report.files_changed == 1

    conn = sg.connect(db_path)
    try:
        old_context = sg.context(conn, "pkg/old_name.py")
        assert old_context["found"] is False
        assert old_context["entities"] == []
        assert old_context["edges"] == []
        matches = sg.func(conn, "renamed_function")
        assert len(matches) == 1
        assert matches[0]["file_path"] == "pkg/new_name.py"

        stale_rows = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE file_path = 'pkg/old_name.py'"
        ).fetchone()[0]
        assert stale_rows == 0
        stale_edges = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE file_path = 'pkg/old_name.py'"
        ).fetchone()[0]
        assert stale_edges == 0
    finally:
        conn.close()


def test_delete_without_replacement_removes_entities_and_edges(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    doomed = repo / "pkg" / "doomed.py"
    _write(doomed, "def about_to_vanish():\n    return 1\n")
    sg.build_index(repo, incremental=True)
    doomed.unlink()
    report = sg.build_index(repo, incremental=True)
    assert report.files_removed == 1

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert sg.func(conn, "about_to_vanish") == []
        assert conn.execute("SELECT COUNT(*) FROM files WHERE file_path='pkg/doomed.py'").fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bounded traversal: neighbors / shortest_path / component_summary
# ---------------------------------------------------------------------------

def _build_chain_repo(repo: Path, length: int) -> None:
    lines = []
    for i in range(length):
        if i + 1 < length:
            lines.append(f"def step_{i}():\n    return step_{i + 1}()\n")
        else:
            lines.append(f"def step_{i}():\n    return 0\n")
    _write(repo / "pkg" / "chain.py", "\n".join(lines))
    sg.build_index(repo, incremental=True)


def test_neighbors_enforces_explicit_depth_and_result_caps(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _build_chain_repo(repo, 10)
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        result = sg.neighbors(conn, "pkg/chain.py.step_0", depth=999, limit=999999)
        assert result["depth"] == sg.MAX_DEPTH
        assert result["limit"] == sg.MAX_NEIGHBOR_RESULTS
        assert len(result["neighbors"]) <= sg.MAX_NEIGHBOR_RESULTS
    finally:
        conn.close()


def test_shortest_path_finds_path_and_respects_max_depth(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _build_chain_repo(repo, 6)
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        found = sg.shortest_path(conn, "pkg/chain.py.step_0", "pkg/chain.py.step_3", max_depth=6)
        assert found["found"] is True
        assert found["path"][0] == "pkg/chain.py.step_0"
        assert found["path"][-1] == "pkg/chain.py.step_3"

        too_shallow = sg.shortest_path(conn, "pkg/chain.py.step_0", "pkg/chain.py.step_5", max_depth=1)
        assert too_shallow["found"] is False

        clamped = sg.shortest_path(conn, "pkg/chain.py.step_0", "pkg/chain.py.step_1", max_depth=999)
        assert clamped["found"] is True
    finally:
        conn.close()


def test_component_summary_deterministic_and_bounded(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _build_chain_repo(repo, 8)
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        first = sg.component_summary(conn, "pkg/chain.py.step_0", max_depth=999, max_nodes=3)
        second = sg.component_summary(conn, "pkg/chain.py.step_0", max_depth=999, max_nodes=3)
        assert first == second  # deterministic for identical inputs
        assert first["max_depth"] == sg.MAX_DEPTH
        assert first["max_nodes"] == 3
        assert first["member_count"] <= 3
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# focus / slice / bundle: backward-compatible budget contract
# ---------------------------------------------------------------------------

def test_focus_respects_row_and_byte_budget(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    body = "\n".join(f"def budget_probe_{i}():\n    return {i}\n" for i in range(30))
    _write(repo / "pkg" / "many.py", body)
    sg.build_index(repo, incremental=True)

    payload = sg.focus(repo, "budget_probe", budget=5)
    assert payload["mode"] == "focus"
    assert len(payload["matches"]) <= 5
    encoded = json.dumps(payload).encode("utf-8")
    assert len(encoded) <= max(512, 5 * 512)


def test_slice_includes_bounded_neighbors(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", "def hub():\n    return spoke()\n\ndef spoke():\n    return 1\n")
    sg.build_index(repo, incremental=True)
    payload = sg.slice_(repo, "hub", budget=10)
    assert payload["mode"] == "slice"
    assert "neighbors" in payload


def test_bundle_validates_bundle_type_and_stays_bounded(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", "def bundled_target():\n    return 1\n")
    sg.build_index(repo, incremental=True)

    payload = sg.bundle(repo, "refactor", "bundled_target", max_lines=10)
    assert payload["mode"] == "bundle"
    assert payload["bundle_type"] == "refactor"
    assert len(payload["sections"]) <= 10

    with pytest.raises(sg.SourceGraphError):
        sg.bundle(repo, "not_a_real_bundle_type", "bundled_target", max_lines=10)


# ---------------------------------------------------------------------------
# Migration: verified copy, parity, rollback metadata, idempotent cutover
# ---------------------------------------------------------------------------

def test_migration_skips_cleanly_when_no_legacy_source_exists(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    report = sgm.migrate_legacy_db(repo, db_id="source_graph", legacy_rel="AITools/source_graph.db", dry_run=True)
    assert report.status == "no_legacy_source_skip"
    assert report.parity_ok is True


def test_migration_dry_run_verifies_without_writing_canonical(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    legacy_path = repo / "AITools" / "source_graph.db"
    legacy_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(legacy_path))
    conn.execute("CREATE TABLE files(path TEXT)")
    conn.execute("INSERT INTO files VALUES ('a.py'), ('b.py')")
    conn.commit()
    conn.close()
    legacy_sha_before = sgm._sha256_file(legacy_path)

    report = sgm.migrate_legacy_db(repo, db_id="source_graph", legacy_rel="AITools/source_graph.db", dry_run=True)
    assert report.status == "verified_dry_run"
    assert report.parity_ok is True
    assert report.legacy_integrity_check == "ok"
    assert report.legacy_tables == {"files": 2}
    assert report.copy_tables == report.legacy_tables
    assert not Path(report.canonical_path).exists()
    assert Path(report.rollback_manifest_path).exists()
    manifest = json.loads(Path(report.rollback_manifest_path).read_text(encoding="utf-8"))
    assert manifest["parity_ok"] is True
    assert manifest["legacy_sha256"] == legacy_sha_before
    # Legacy source is read-only: migration never wrote it.
    assert sgm._sha256_file(legacy_path) == legacy_sha_before


def test_migration_real_run_then_idempotent_cutover(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    legacy_path = repo / "AITools" / "source_graph.db"
    legacy_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(legacy_path))
    conn.execute("CREATE TABLE files(path TEXT)")
    conn.execute("INSERT INTO files VALUES ('a.py')")
    conn.commit()
    conn.close()

    report = sgm.migrate_legacy_db(repo, db_id="source_graph", legacy_rel="AITools/source_graph.db", dry_run=False)
    assert report.status == "migrated_and_verified"
    assert Path(report.canonical_path).exists()
    assert Path(report.canonical_path).is_relative_to(repo / HUB_DIRNAME / "source_graph")

    cutover_1 = sgm.perform_cutover(repo, "source_graph", parity_ok=report.parity_ok)
    assert cutover_1["status"] == "cutover_applied"
    assert cutover_1["generation"] == 1

    cutover_2 = sgm.perform_cutover(repo, "source_graph", parity_ok=report.parity_ok)
    assert cutover_2["status"] == "already_cutover"
    assert cutover_2["generation"] == 1  # idempotent: no double-increment

    registry = load_storage_registry(repo)
    db = registry.databases["source_graph"]
    assert db.canonical_active is True
    assert db.authority_state == "canonical_active"


def test_cutover_refuses_without_parity(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    with pytest.raises(sgm.MigrationError):
        sgm.perform_cutover(repo, "source_graph", parity_ok=False)


def test_migration_never_writes_legacy_path_even_on_real_run(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    legacy_path = repo / "AITools" / "source_graph_universal.db"
    legacy_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(legacy_path))
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    before = legacy_path.read_bytes()

    sgm.migrate_legacy_db(repo, db_id="universal", legacy_rel="AITools/source_graph_universal.db", dry_run=False)
    assert legacy_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Production callers query aiworkhub.source_graph directly, in-process
# ---------------------------------------------------------------------------

def test_project_context_calls_canonical_module_without_subprocess(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", "def project_ctx_probe():\n    return 1\n")
    sg.build_index(repo, incremental=True)
    _write(repo / "AITools" / "transcript_graph.py", "print('{}')\n")

    def _forbidden_subprocess_run(*args, **kwargs):
        raise AssertionError("project_context must not shell out for source_graph")

    def _fake_run_fixed_argv(argv, cwd):
        # Only the session_current_state call is still subprocess-based here;
        # simulate it succeeding so the rest of the bundle assembles.
        return "{}", False

    monkeypatch.setattr(pc, "_run_fixed_argv", _fake_run_fixed_argv)
    monkeypatch.setattr(subprocess, "run", _forbidden_subprocess_run)

    card = {
        "project_context": {
            "required": True,
            "task_type": "code",
            "source_graph": {
                "mode": "focus", "query": "project_ctx_probe", "budget": 10,
                "required": True, "targets": [],
            },
            "session": {"topic": "t", "limit": 5},
        }
    }
    result = pc.collect_project_context(repo, card)
    assert result is not None
    assert "project_ctx_probe" in result.prompt_bundle


def test_project_context_no_operational_dependency_on_aitools_source_graph_py(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", "def only_probe():\n    return 1\n")
    sg.build_index(repo, incremental=True)
    _write(repo / "AITools" / "transcript_graph.py", "print('{}')\n")
    # AITools/source_graph.py is deliberately absent from this fixture repo --
    # if project_context.py still depended on it operationally this would fail.
    assert not (repo / "AITools" / "source_graph.py").exists()

    card = {
        "project_context": {
            "required": True,
            "task_type": "code",
            "source_graph": {
                "mode": "focus", "query": "only_probe", "budget": 10,
                "required": True, "targets": [],
            },
            "session": {"topic": "t", "limit": 5},
        }
    }
    import aiworkhub.project_context as pc_mod
    orig = pc_mod._run_fixed_argv
    pc_mod._run_fixed_argv = lambda argv, cwd: ("{}", False)
    try:
        result = pc_mod.collect_project_context(repo, card)
    finally:
        pc_mod._run_fixed_argv = orig
    assert result is not None
    assert "only_probe" in result.prompt_bundle


def test_worker_mcp_source_graph_query_is_canonical_and_repo_bound(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", "def worker_probe():\n    return 1\n")
    sg.build_index(repo, incremental=True)

    ctx = w.WorkerToolContext(
        task_id="t1", runner="r", topic="topic", request_id="req1",
        repo=repo, authority_repo=repo, source_graph_targets=(),
        session_topic="topic", audit_ledger_path=None, audit_hmac_key_path=None,
    )
    result = w.source_graph_query(ctx, mode="focus", query="worker_probe", budget=10)
    assert result["ok"] is True
    assert result["authority_source"] == "canonical"
    assert "worker_probe" in result["content"]


def test_worker_mcp_source_graph_query_fails_closed_when_unindexed(tmp_path):
    repo = _new_repo(tmp_path, "repo")  # bootstrapped but never built
    ctx = w.WorkerToolContext(
        task_id="t1", runner="r", topic="topic", request_id="req1",
        repo=repo, authority_repo=repo, source_graph_targets=(),
        session_topic="topic", audit_ledger_path=None, audit_hmac_key_path=None,
    )
    result = w.source_graph_query(ctx, mode="focus", query="anything", budget=10)
    assert result["ok"] is False
    assert "authority_db_absent_or_empty" in result["reason"]


def test_worker_mcp_source_graph_never_touches_legacy_aitools_db(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", "def isolated_probe():\n    return 1\n")
    sg.build_index(repo, incremental=True)
    legacy_db = repo / "AITools" / "source_graph.db"
    assert not legacy_db.exists()

    ctx = w.WorkerToolContext(
        task_id="t1", runner="r", topic="topic", request_id="req1",
        repo=repo, authority_repo=repo, source_graph_targets=(),
        session_topic="topic", audit_ledger_path=None, audit_hmac_key_path=None,
    )
    w.source_graph_query(ctx, mode="focus", query="isolated_probe", budget=10)
    assert not legacy_db.exists()  # build/query path never created it


def test_no_build_or_query_path_writes_legacy_aitools_databases(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", "def probe():\n    return 1\n")
    sg.build_index(repo, incremental=True)
    payload = sg.focus(repo, "probe", 10)
    assert payload["matches"]
    assert not (repo / "AITools" / "source_graph.db").exists()
    assert not (repo / "AITools" / "source_graph_universal.db").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
