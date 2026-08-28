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

import errno
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import project_context as pc
from aiworkhub import source_graph as sg
from aiworkhub import source_graph_ast as sgast
from aiworkhub import source_graph_migration as sgm
from aiworkhub import worker_ai_tools_mcp as w
import aiworkhub.source_graph_semantic as sgsemantic
from aiworkhub.repository_state import HUB_DIRNAME, bootstrap_repository, inspect_repository
from aiworkhub.storage_registry import load_storage_registry


def _new_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_javascript_fallback_masks_regex_literals_before_matching_braces(
    tmp_path, monkeypatch,
):
    repo = _new_repo(tmp_path, "javascript_regex_fallback")
    target = repo / "src" / "extension.js"
    _write(
        target,
        "function redactToolInputValue(value) {\n"
        "  return value.replace(/bearer\\s+[^\\s\"']+/gi, 'redacted');\n"
        "}\n\n"
        "function glmTextToolProtocolPrompt(prompt, pathContracts = {}) {\n"
        "  return 'bounded';\n"
        "}\n",
    )
    monkeypatch.setattr(sgsemantic, "extract_javascript_typescript", lambda **_kwargs: None)

    extraction = sgast.extract_file(repo, target, build_revision="test")
    functions = {
        entity.name: entity
        for entity in extraction.entities
        if entity.kind == "function"
    }

    assert functions["redactToolInputValue"].line_end == 3
    # The legacy fallback deliberately includes the immediately preceding
    # blank line because its anchored pattern uses ``\s*``.
    assert functions["glmTextToolProtocolPrompt"].line_start == 4
    assert functions["glmTextToolProtocolPrompt"].line_end == 7
    assert "pathContracts = {}" in functions["glmTextToolProtocolPrompt"].signature


# ---------------------------------------------------------------------------
# Repository-identity-bound database resolution
# ---------------------------------------------------------------------------

def test_db_path_resolves_under_repo_local_aiworkhub_dir(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    db_path = sg.resolve_db_path(repo)
    assert db_path == repo / HUB_DIRNAME / "source_graph" / "source_graph.sqlite"
    assert db_path.is_relative_to(repo)


def test_entity_qualname_join_has_a_dedicated_index(tmp_path):
    repo = _new_repo(tmp_path, "qualname_index")
    _write(repo / "pkg" / "mod.py", "def indexed_symbol():\n    return 1\n")

    sg.build_index(repo, incremental=False)

    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        indexes = {
            str(row["name"])
            for row in conn.execute("PRAGMA index_list(entities)")
        }
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM edges e "
            "WHERE EXISTS (SELECT 1 FROM entities d "
            "WHERE d.qualname=e.dst_qualname)"
        ).fetchall()
    finally:
        conn.close()

    assert "idx_entities_qualname" in indexes
    assert any("idx_entities_qualname" in str(row["detail"]) for row in plan)


def test_db_path_requires_manifest_never_falls_back_to_cwd(tmp_path, monkeypatch):
    unmanaged = tmp_path / "no_manifest"
    unmanaged.mkdir()
    monkeypatch.chdir(unmanaged)
    with pytest.raises(sg.RepositoryUnresolvedError):
        sg.resolve_db_path(unmanaged)


def test_migrate_legacy_db_reads_uri_quoted_source_without_wal_sidecars(tmp_path):
    repo = _new_repo(tmp_path, "readonly_migration")
    legacy = tmp_path / "legacy source #1?.sqlite"
    conn = sqlite3.connect(legacy)
    try:
        conn.execute("CREATE TABLE symbols (name TEXT NOT NULL)")
        conn.execute("INSERT INTO symbols (name) VALUES ('alpha')")
        conn.commit()
    finally:
        conn.close()

    sidecars = [Path(f"{legacy}-wal"), Path(f"{legacy}-shm")]
    for sidecar in sidecars:
        sidecar.unlink(missing_ok=True)
    before_bytes = legacy.read_bytes()
    before_mtime_ns = legacy.stat().st_mtime_ns

    report = sgm.migrate_legacy_db(
        repo, db_id="source_graph", legacy_source=legacy, dry_run=False,
    )

    assert report.status == "migrated_and_verified"
    assert report.parity_ok is True
    assert legacy.read_bytes() == before_bytes
    assert legacy.stat().st_mtime_ns == before_mtime_ns
    assert all(not sidecar.exists() for sidecar in sidecars)

    canonical = sg.resolve_db_path(repo)
    dest_conn = sqlite3.connect(canonical)
    try:
        assert dest_conn.execute("SELECT name FROM symbols").fetchall() == [("alpha",)]
    finally:
        dest_conn.close()


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


def test_unregistered_language_is_explicit_fail_closed_not_regex_approximated(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "pkg" / "native.unknown_extension"
    _write(target, "opaque\n")
    extraction = sgast.extract_file(repo, target, build_revision="test-rev")
    assert extraction.status == "unsupported_fail_closed"
    assert extraction.entities == ()
    assert extraction.edges == ()


def test_language_registry_exposes_all_34_families() -> None:
    assert len(sg.LANGUAGE_CAPABILITIES) == 34
    assert {"cpp", "json", "xml", "documentation"}.issubset(sg.LANGUAGE_CAPABILITIES)
    assert sg.LANGUAGE_CAPABILITIES["cpp"] == "semantic_lexical"


@pytest.mark.parametrize(
    "relative,expected_language",
    [
        ("config/runtime.json", "json"),
        ("data/events.jsonl", "json"),
        ("schemas/task.xml", "xml"),
    ],
)
def test_json_xml_are_indexed_as_truthful_file_evidence(
    tmp_path, relative, expected_language,
):
    repo = _new_repo(tmp_path, "repo")
    target = repo / relative
    _write(target, "{}\n" if relative.endswith(".json") else "<root/>\n")
    extraction = sgast.extract_file(repo, target, build_revision="test-rev")
    assert extraction.status == "file_evidence_only"
    assert extraction.language == expected_language
    assert len(extraction.entities) == 1
    assert extraction.entities[0].evidence_label == sgast.FILE_EVIDENCE
    assert extraction.edges == ()


def test_markdown_is_indexed_as_truthful_repository_document_evidence(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "docs" / "PRODUCT_ROADMAP.md"
    _write(target, "# Product roadmap\n\nStage attribution remains open.\n")

    extraction = sgast.extract_file(repo, target, build_revision="test-rev")

    assert extraction.status == "file_evidence_only"
    assert extraction.language == "documentation"
    assert len(extraction.entities) == 1
    assert extraction.entities[0].evidence_label == sgast.FILE_EVIDENCE

    report = sg.build_index(repo, incremental=False)
    assert report.files_seen == 1
    literal = sg.bodygrep_query(repo, "Stage attribution remains open", budget=16)
    assert literal["candidate_files"] == ["docs/PRODUCT_ROADMAP.md"]
    assert literal["matches"]


def test_bodygrep_target_is_scanned_before_global_byte_cap(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "000-large.md", "x" * (1024 * 1024 + 128))
    _write(repo / "README.md", "# AIWorkHub\n\nExact bounded target survives.\n")
    sg.build_index(repo, incremental=False)

    literal = sg.bodygrep_query(
        repo,
        "Exact bounded target survives",
        budget=4,
        target="README.md",
    )

    assert literal["target"] == "README.md"
    assert literal["files_scanned"] == 1
    assert literal["candidate_files"] == ["README.md"]
    assert literal["matches"][0]["file_path"] == "README.md"


def test_cpp_cuda_semantic_lexical_extraction_records_symbols_bodies_and_calls(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "native" / "engine.cu"
    _write(
        target,
        """#include <vector>\n
#include "engine.hpp"\n
// #include "fake.hpp"\n
#define BLOCKS 4\n
struct Engine : public Base { int run(int x) { return helper(x); } };\n
int helper(int x) { return x + 1; }\n
__global__ void kernel(int *out) { out[0] = helper(1); }\n
void launch(int *out) { kernel<<<BLOCKS, 32>>>(out); }\n
""",
    )
    extraction = sgast.extract_file(repo, target, build_revision="test-rev")
    assert extraction.status == "ok"
    assert extraction.language == "cpp"
    assert {
        ("struct", "Engine"), ("method", "run"),
        ("function", "helper"), ("function", "launch"),
    }.issubset(
        {(entity.kind, entity.name) for entity in extraction.entities}
    )
    assert any(edge.kind == "imports" and edge.dst_name == "vector" for edge in extraction.edges)
    assert any(edge.kind == "imports" and edge.dst_name == "engine.hpp" for edge in extraction.edges)
    assert not any(edge.kind == "imports" and edge.dst_name == "fake.hpp" for edge in extraction.edges)
    assert any(edge.kind == "calls" and edge.dst_name == "kernel" for edge in extraction.edges)
    assert any(entity.kind == "macro" and entity.name == "BLOCKS" for entity in extraction.entities)


def test_cpp_json_xml_build_and_query_are_non_empty(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "native" / "engine.cpp", "int main() { return 0; }\n")
    _write(repo / "config" / "runtime.json", '{"enabled": true}\n')
    _write(repo / "schemas" / "task.xml", "<task/>\n")
    report = sg.build_index(repo, incremental=True)
    assert report.files_seen == 3
    assert report.entities_written >= 4
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert sg.find(conn, "engine.cpp")
        assert sg.find(conn, "runtime.json")
        assert sg.find(conn, "task.xml")
        assert sg.func(conn, "main")
    finally:
        conn.close()


def test_build_report_counts_persisted_edges_after_deduplication(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "pkg" / "duplicate.py"
    _write(target, "pass\n")
    source_hash = sgast.sha256_bytes(target.read_bytes())
    edge = sgast.Edge(
        kind="calls",
        src_qualname="pkg/duplicate.py::caller",
        dst_name="target",
        dst_qualname=None,
        file_path="pkg/duplicate.py",
        line=1,
        evidence_label=sgast.AMBIGUOUS,
        extractor=sgast.EXTRACTOR_ID,
        confidence=0.5,
        source_hash=source_hash,
        build_revision=sg.BUILD_REVISION,
    )
    extraction = sgast.FileExtraction(
        file_path="pkg/duplicate.py",
        language="python",
        status="ok",
        source_hash=source_hash,
        edges=(edge, edge),
    )
    monkeypatch.setattr(sgast, "extract_file", lambda *_args, **_kwargs: extraction)

    report = sg.build_index(repo, incremental=False)
    assert report.edges_written == 1
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
    finally:
        conn.close()


def test_cpp_cross_file_calls_and_all_six_compact_query_modes(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "native" / "math.cpp", "int helper(int x) { return x + 1; }\n")
    _write(
        repo / "native" / "engine.cpp",
        '#include "math.hpp"\nint run_engine(int x) { return helper(x); }\n',
    )
    _write(
        repo / "tests" / "test_engine.cpp",
        "int test_engine() { return run_engine(1); }\n",
    )
    sg.build_index(repo, incremental=False)
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        edge = conn.execute(
            "SELECT dst_qualname FROM edges WHERE kind='calls' AND dst_name='helper'"
        ).fetchone()
        assert edge is not None
        assert edge["dst_qualname"].endswith("math.cpp::helper")
    finally:
        conn.close()

    focus = sg.focus(repo, "run_engine", 32)
    sliced = sg.slice_(repo, "run_engine", 32)
    contextual = sg.context_query(repo, "run_engine", 32)
    traced = sg.trace(repo, "helper", 32)
    impacted = sg.impact(repo, "helper", 32)
    dependencies = sg.deps_query(repo, "run_engine", 32)
    bundled = sg.bundle(repo, "bugfix", "run_engine", 32)

    assert focus["matches"] and focus["candidate_files"][0] == "native/engine.cpp"
    assert focus["ranked_symbols"]
    assert focus["ranked_symbols"][0]["metrics_evidence"] == "deterministic_lexical_and_graph"
    assert all(
        set(row) == {"qualname", "file_path", "priority_score"}
        for row in focus["hot_symbols"]
    )
    assert sliced["outgoing_calls"]
    assert any(row["file_path"] == "tests/test_engine.cpp" for row in sliced["related_tests"])
    assert contextual["contexts"][0]["entities"]
    assert contextual["contexts"][0]["file_path"] == "native/engine.cpp"
    assert all(
        row["file_path"] == "native/engine.cpp"
        for row in contextual["contexts"][0]["entities"]
    )
    assert all(
        row["file_path"] == "native/engine.cpp"
        for row in contextual["contexts"][0]["edges"]
    )
    assert contextual["insights"]["entry_symbols"]
    assert any(row["caller_symbol"].endswith("run_engine") for row in traced["incoming_calls"])
    assert {row["file_path"] for row in impacted["impacted_files"]} >= {"native/math.cpp"}
    assert impacted["impact_evidence"].startswith("bidirectional_resolved_calls")
    assert dependencies["mode"] == "deps"
    assert dependencies["dependency_kinds"] == ["calls", "imports", "inherits"]
    assert any(
        row["kind"] == "calls" and row["dst_name"] == "helper"
        for row in dependencies["dependency_edges"]
    )
    assert "outgoing_calls" not in dependencies
    assert bundled["sections"] and bundled["outgoing_calls"]
    assert bundled["insights"]["ranked_symbols"]


def test_every_focus_emitted_next_step_resolves_without_replacing_task_query(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "service.py",
        "def emitted_target(value):\n    return value + 1\n",
    )
    sg.build_index(repo, incremental=False)

    focused = sg.focus(repo, "emitted_target", 16)
    task_query = "investigate the service execution boundary"
    assert focused["recommended_next_steps"]
    for step in focused["recommended_next_steps"]:
        mode, target = step.split(":", 1)
        if mode == "slice":
            payload = sg.slice_(repo, task_query, 16, target=target)
            assert payload["query"] == task_query
            assert payload["target"] == target
            assert payload["query_tokens_source"] == "target"
            assert any(row["qualname"] == target for row in payload["matches"])
        elif mode == "context":
            payload = sg.context_query(repo, target, 16)
            assert payload["contexts"]
            assert payload["contexts"][0]["file_path"] == target
        else:  # pragma: no cover - emitted modes must be added explicitly
            pytest.fail(f"unhandled recommended step: {step}")


def test_index_quality_scorecard_is_generation_bound_and_recomputable(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "service.py",
        "def resolved_target():\n    return 1\n\n"
        "def caller():\n    resolved_target()\n    missing_target()\n",
    )

    report = sg.build_index(repo, incremental=False)
    quality = report.index_quality

    assert quality["schema_id"] == "aiworkhub.source_graph.index_quality.v1"
    assert quality["finished_at"] == report.finished_at
    assert quality["build_revision"] == report.build_revision
    assert quality["edges"]["total"] >= 2
    assert quality["edges"]["resolved"] >= 1
    assert quality["edges"]["unresolved"] >= 1
    assert quality["by_language"]["python"]["entities"] >= 2
    assert quality["storage"]["db_bytes"] > 0
    assert quality["measurement_boundary"] == (
        "structural_index_metrics_not_retrieval_or_token_savings"
    )

    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        persisted = sg.summary(conn)["index_quality"]
    finally:
        conn.close()
    assert persisted == quality


def test_recommendation_roundtrip_gate_uses_full_shared_wrapper(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "service.py", "def roundtrip_target():\n    return 1\n")
    sg.build_index(repo, incremental=False)
    ctx = w.WorkerToolContext(
        task_id="source-graph:selfcheck", runner="daemon", topic="health",
        request_id="generation-1", repo=repo, authority_repo=repo,
        source_graph_targets=(), session_topic="health",
        audit_ledger_path=None, audit_hmac_key_path=None,
    )

    result = w.source_graph_recommendation_roundtrip_gate(ctx, sample_limit=1)

    assert result["ok"] is True
    assert result["status"] == "ready"
    assert result["emitted"] >= 3
    assert result["resolved"] == result["emitted"]
    assert result["resolvability_ratio"] == 1.0
    assert result["failures"] == []


def test_recommendation_roundtrip_gate_attributes_wrapper_only_failure(
    tmp_path, monkeypatch,
):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "service.py", "def roundtrip_target():\n    return 1\n")
    sg.build_index(repo, incremental=False)
    ctx = w.WorkerToolContext(
        task_id="source-graph:selfcheck", runner="daemon", topic="health",
        request_id="generation-2", repo=repo, authority_repo=repo,
        source_graph_targets=(), session_topic="health",
        audit_ledger_path=None, audit_hmac_key_path=None,
    )
    real_query = w.source_graph_query

    def wrapper_with_slice_regression(context, *, mode, query, **kwargs):
        if mode == "slice":
            return {"ok": True, "hit_count": 0, "content": "{}"}
        return real_query(context, mode=mode, query=query, **kwargs)

    monkeypatch.setattr(w, "source_graph_query", wrapper_with_slice_regression)

    result = w.source_graph_recommendation_roundtrip_gate(ctx, sample_limit=1)

    assert result["ok"] is False
    assert result["status"] == "guidance_degraded"
    assert any(
        row["layer"] == "wrapper" and row["value"].startswith("slice:")
        for row in result["failures"]
    )


def test_slice_is_symbol_scoped_and_excludes_unrelated_same_file_calls(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "service.py",
        "def wanted_helper():\n"
        "    return 1\n\n"
        "def unrelated_helper():\n"
        "    return 2\n\n"
        "def wanted_entry():\n"
        "    return wanted_helper()\n\n"
        "def unrelated_entry():\n"
        "    return unrelated_helper()\n",
    )
    _write(
        repo / "tests" / "test_service.py",
        "from pkg.service import wanted_entry\n\n"
        "def test_wanted_entry():\n"
        "    assert wanted_entry() == 1\n",
    )
    sg.build_index(repo, incremental=False)

    focused = sg.focus(repo, "wanted_entry", 16)
    target = focused["ranked_symbols"][0]["qualname"]
    sliced = sg.slice_(repo, "change wanted behavior", 16, target=target)

    assert sliced["matches"][0]["qualname"] == target
    assert {row["callee_symbol"] for row in sliced["outgoing_calls"]} == {
        "wanted_helper"
    }
    assert all(
        row["callee_symbol"] != "unrelated_helper"
        for row in sliced["outgoing_calls"]
    )


def test_exact_qualname_ranking_and_body_are_deterministic_with_duplicate_names(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "alpha.py", "def shared():\n    return 'alpha'\n")
    _write(repo / "pkg" / "beta.py", "def shared():\n    return 'beta'\n")
    sg.build_index(repo, incremental=False)

    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        qualnames = [
            row["qualname"]
            for row in conn.execute(
                "SELECT qualname FROM entities WHERE name='shared' ORDER BY qualname"
            )
        ]
    finally:
        conn.close()
    target = next(item for item in qualnames if "beta" in item)

    focused = sg.focus(repo, target, 8)
    body = sg.body_query(repo, target, 8)

    assert focused["matches"][0]["qualname"] == target
    assert body["matches"][0]["qualname"] == target
    assert "return 'beta'" in body["matches"][0]["source"]


def test_cross_file_resolver_never_binds_calls_across_language_families(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "python_owner.py", "def shared_target():\n    return 1\n")
    _write(
        repo / "web" / "caller.js",
        "export function run() { return shared_target(); }\n",
    )
    sg.build_index(repo, incremental=False)

    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        edge = conn.execute(
            "SELECT dst_qualname FROM edges WHERE file_path='web/caller.js' "
            "AND kind='calls' AND dst_name='shared_target'"
        ).fetchone()
    finally:
        conn.close()

    assert edge is not None
    assert edge["dst_qualname"] is None


def test_focus_todos_require_comment_evidence_not_identifiers_or_strings(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "todos.py",
        "TODO_VALUE = 'TODO: not work evidence'\n"
        "def todo_probe():\n"
        "    value = 'FIXME: still a string'\n"
        "    return value  # TODO: replace fixture value\n",
    )
    sg.build_index(repo, incremental=False)

    payload = sg.focus(repo, "todo_probe", 16)

    assert payload["todos"] == [{
        "file_path": "pkg/todos.py",
        "line": 4,
        "marker": "TODO",
        "text": "replace fixture value",
    }]


def test_payload_trimming_preserves_query_receipt_metadata(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    body = "\n".join(
        f"def metadata_probe_{index}():\n    return '{'x' * 400}'\n"
        for index in range(24)
    )
    _write(repo / "pkg" / "metadata.py", body)
    sg.build_index(repo, incremental=False)

    payload = sg.focus(repo, "metadata_probe", budget=2)

    assert payload["query"] == "metadata_probe"
    assert payload["query_tokens"] == ["metadata", "probe"]
    assert payload["candidate_files"] == ["pkg/metadata.py"]
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 1024


def test_import_evidence_disambiguates_duplicate_cross_file_names(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "native" / "math_fast.cpp", "int helper(int x) { return x + 1; }\n")
    _write(repo / "legacy" / "math_slow.cpp", "int helper(int x) { return x - 1; }\n")
    _write(
        repo / "native" / "engine.cpp",
        '#include "math_fast.hpp"\nint run_engine(int x) { return helper(x); }\n',
    )

    sg.build_index(repo, incremental=False)
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        edge = conn.execute(
            "SELECT dst_qualname FROM edges WHERE file_path='native/engine.cpp' "
            "AND kind='calls' AND dst_name='helper'"
        ).fetchone()
        assert edge is not None
        assert edge["dst_qualname"].endswith("native/math_fast.cpp::helper")
    finally:
        conn.close()


def test_python_import_evidence_resolves_only_exact_function_calls(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "helpers.py",
        "def helper(value):\n    return value + 1\n\n"
        "def unrelated(value):\n    return value - 1\n",
    )
    _write(
        repo / "pkg" / "caller.py",
        "import pkg.helpers as helpers\n"
        "from pkg.helpers import unrelated\n\n"
        "def run(value):\n"
        "    helpers.helper(value)\n"
        "    unrelated(value)\n"
        "    obj.helper(value)\n",
    )

    sg.build_index(repo, incremental=False)
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        rows = conn.execute(
            "SELECT line, dst_name, dst_qualname FROM edges "
            "WHERE file_path='pkg/caller.py' AND kind='calls' ORDER BY line"
        ).fetchall()
        by_call: dict[tuple[int, str], set[str | None]] = {}
        for row in rows:
            by_call.setdefault(
                (int(row["line"]), str(row["dst_name"])), set(),
            ).add(row["dst_qualname"])
        assert set(by_call) == {
            (5, "helper"), (6, "unrelated"), (7, "helper"),
        }
        assert all(
            target and target.endswith("pkg/helpers.py.helper")
            for target in by_call[(5, "helper")]
        )
        assert all(
            target and target.endswith("pkg/helpers.py.unrelated")
            for target in by_call[(6, "unrelated")]
        )
        assert by_call[(7, "helper")] == {None}
    finally:
        conn.close()


def test_incremental_python_import_resolution_is_changed_scope_only(
    tmp_path, monkeypatch,
):
    repo = _new_repo(tmp_path, "repo")
    helper = repo / "pkg" / "helpers.py"
    _write(helper, "def helper(value):\n    return value + 1\n")
    _write(
        repo / "pkg" / "caller.py",
        "from pkg.helpers import helper\n\ndef run(value):\n    return helper(value)\n",
    )
    for index in range(20):
        _write(
            repo / "pkg" / f"unrelated_{index}.py",
            "import pkg.helpers as helpers\n\ndef run(value):\n"
            "    return helpers.never_resolves(value)\n",
        )
    sg.build_index(repo, incremental=False)

    original_read_text = Path.read_text
    unrelated_reads: list[str] = []

    def _tracked_read_text(path: Path, *args, **kwargs):
        if path.name.startswith("unrelated_"):
            unrelated_reads.append(path.name)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _tracked_read_text)
    _write(helper, "def helper_renamed(value):\n    return value + 2\n")

    report = sg.build_index(repo, incremental=True)

    assert report.files_changed == 1
    assert unrelated_reads == []
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        edge = conn.execute(
            "SELECT dst_qualname FROM edges WHERE file_path='pkg/caller.py' "
            "AND kind='calls' AND dst_name='helper'"
        ).fetchone()
        assert edge is not None
        assert edge["dst_qualname"] is None
    finally:
        conn.close()


def test_python_import_resolution_tokenizes_each_source_line_once(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "helpers.py",
        "def first():\n    return 1\n\ndef second():\n    return 2\n",
    )
    call_line = "    return helpers.first(), helpers.second()"
    _write(
        repo / "pkg" / "caller.py",
        "import pkg.helpers as helpers\n\ndef run():\n" + call_line + "\n",
    )
    original = sg._python_dotted_calls
    tokenized_lines: list[str] = []

    def tracked(source_line: str):
        tokenized_lines.append(source_line)
        return original(source_line)

    monkeypatch.setattr(sg, "_python_dotted_calls", tracked)

    sg.build_index(repo, incremental=False)

    assert tokenized_lines.count(call_line) == 1
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        rows = conn.execute(
            "SELECT dst_name, dst_qualname FROM edges "
            "WHERE file_path='pkg/caller.py' AND kind='calls' ORDER BY dst_name"
        ).fetchall()
        assert {str(row["dst_name"]) for row in rows} == {"first", "second"}
        assert all(row["dst_qualname"] for row in rows)
    finally:
        conn.close()


def test_python_import_resolution_stays_unresolved_when_target_is_ambiguous(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "a" / "helpers.py", "def helper():\n    return 1\n")
    _write(repo / "b" / "helpers.py", "def helper():\n    return 2\n")
    _write(
        repo / "caller.py",
        "import helpers\n\ndef run():\n    helpers.helper()\n",
    )

    sg.build_index(repo, incremental=False)
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        row = conn.execute(
            "SELECT dst_qualname FROM edges WHERE file_path='caller.py' "
            "AND kind='calls' AND dst_name='helper'"
        ).fetchone()
        assert row is not None
        assert row["dst_qualname"] is None
    finally:
        conn.close()


def test_empty_relative_import_module_does_not_abort_index(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "__init__.py", "")
    _write(repo / "pkg" / "helper.py", "def helper():\n    return 1\n")
    _write(
        repo / "pkg" / "caller.py",
        "from . import helper\n\ndef run():\n    return helper.helper()\n",
    )

    report = sg.build_index(repo, incremental=False)

    assert report.files_seen == 3
    assert sg._import_target_matches_file("", "pkg/helper.py") is False
    assert sg._import_target_matches_file(".", "pkg/helper.py") is False
    assert sg._import_target_matches_file("/", "pkg/helper.py") is False


def test_multi_term_find_and_indexed_body_modes_are_non_empty(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "telemetry.py",
        "def collect_graph_metrics():\n"
        "    live_source_graph_calls = 3\n"
        "    return live_source_graph_calls\n",
    )
    sg.build_index(repo, incremental=False)
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        assert sg.find(conn, "collect graph metrics")
    finally:
        conn.close()

    exact_body = sg.body_query(repo, "collect_graph_metrics", budget=16)
    assert exact_body["matches"]
    assert "live_source_graph_calls" in exact_body["matches"][0]["source"]

    literal = sg.bodygrep_query(repo, "live_source_graph_calls", budget=16)
    assert literal["matches"]
    assert literal["candidate_files"] == ["pkg/telemetry.py"]
    assert literal["files_scanned"] == 1
    assert literal["scan_truncated"] is False

    assert sg.file_query(repo, "pkg/telemetry.py", budget=16)["matches"]
    assert sg.function_query(repo, "collect_graph_metrics", budget=16)["matches"]
    assert sg.class_query(repo, "MissingClass", budget=16)["matches"] == []
    assert sg.deps_query(repo, "collect_graph_metrics", budget=16)["mode"] == "deps"


def test_file_mode_returns_bounded_source_preview_for_constant_only_file(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "_version.py",
        '"""Canonical version."""\n\n__version__ = "0.8.55"\n',
    )
    sg.build_index(repo, incremental=False)

    result = sg.file_query(repo, "pkg/_version.py", budget=8)

    assert result["candidate_files"] == ["pkg/_version.py"]
    context = result["contexts"][0]
    assert '__version__ = "0.8.55"' in context["source_preview"]
    assert context["source_preview_bytes"] <= 1024
    assert context["source_preview_truncated"] is False


# ---------------------------------------------------------------------------
# B881: truthful bounded JS/TS semantic lexical evidence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "suffix,expected_language",
    [
        (".js", "javascript"), (".jsx", "javascript"), (".mjs", "javascript"),
        (".cjs", "javascript"), (".ts", "typescript"), (".tsx", "typescript"),
    ],
)
def test_js_ts_family_gets_semantic_lexical_evidence(tmp_path, suffix, expected_language):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "pkg" / f"widget{suffix}"
    _write(target, "export function widget() { return 1; }\n")
    extraction = sgast.extract_file(repo, target, build_revision="test-rev")
    assert extraction.status == "ok"
    assert extraction.language == expected_language
    assert len(extraction.source_hash) == 64
    assert {entity.kind for entity in extraction.entities} >= {"module", "function"}
    function = next(entity for entity in extraction.entities if entity.kind == "function")
    assert function.name == "widget"
    assert function.evidence_label == sgast.EXTRACTED
    assert function.file_path == f"pkg/widget{suffix}"
    assert function.extractor in {
        sgast.POLYGLOT_LEXICAL_EXTRACTOR_ID,
        sgast.TREE_SITTER_JS_TS_EXTRACTOR_ID,
    }
    assert any(edge.kind == "defines" and edge.dst_name == "widget" for edge in extraction.edges)


def test_former_empty_result_regression_js_target_now_produces_non_empty_slice(tmp_path):
    """B880 regression: a JS/TS target must never come back empty merely
    because Python AST extraction was the only semantic extractor wired in."""

    repo = _new_repo(tmp_path, "repo")
    _write(repo / "extension" / "extension.js", "module.exports = function activate() {};\n")
    report = sg.build_index(repo, incremental=True)
    assert report.errors == []
    assert report.entities_written >= 2

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        matches = sg.find(conn, "extension.js")
        assert matches
        assert matches[0]["file_path"] == "extension/extension.js"
        assert matches[0]["evidence_label"] == sgast.EXTRACTED
        assert sg.func(conn, "activate")
    finally:
        conn.close()

    payload = sg.slice_(repo, "extension.js", budget=10)
    assert payload["matches"], "target slice must be non-empty for a real JS file"
    assert payload["matches"][0]["file_path"] == "extension/extension.js"

    bundle_payload = sg.bundle(repo, "refactor", "extension.js", max_lines=10)
    assert bundle_payload["sections"]
    assert bundle_payload["sections"][0]["file"]["language"] == "javascript"
    assert bundle_payload["sections"][0]["file"]["status"] == "ok"
    assert bundle_payload["sections"][0]["edges"]
    assert any(
        entity["kind"] == "function" and entity["name"] == "activate"
        for entity in bundle_payload["sections"][0]["entities"]
    )


def test_js_ts_family_incremental_rename_and_delete_no_stale_evidence(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    old_path = repo / "web" / "old_widget.ts"
    _write(old_path, "export const widget = 1;\n")
    r1 = sg.build_index(repo, incremental=True)
    assert r1.files_changed == 1

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert sg.context(conn, "web/old_widget.ts")["found"] is True
    finally:
        conn.close()

    r2 = sg.build_index(repo, incremental=True)
    assert r2.files_changed == 0 and r2.files_unchanged == 1

    old_path.unlink()
    new_path = repo / "web" / "new_widget.ts"
    _write(new_path, "export const widget = 1;\n")
    r3 = sg.build_index(repo, incremental=True)
    assert r3.files_removed == 1
    assert r3.files_changed == 1

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert sg.context(conn, "web/old_widget.ts")["found"] is False
        assert sg.context(conn, "web/new_widget.ts")["found"] is True
        stale = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE file_path='web/old_widget.ts'"
        ).fetchone()[0]
        assert stale == 0
    finally:
        conn.close()

    new_path.unlink()
    r4 = sg.build_index(repo, incremental=True)
    assert r4.files_removed == 1
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert sg.context(conn, "web/new_widget.ts")["found"] is False
    finally:
        conn.close()


def test_js_ts_family_respects_ignored_directories(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "src" / "real.tsx", "export const Real = () => null;\n")
    _write(repo / "node_modules" / "pkg" / "vendored.js", "module.exports = 1;\n")
    _write(repo / "dist" / "bundle.js", "console.log(1);\n")
    files = sg.iter_source_files(repo)
    rels = {p.relative_to(repo).as_posix() for p in files}
    assert "src/real.tsx" in rels
    assert not any(rel.startswith(("node_modules/", "dist/")) for rel in rels)


def test_archive_is_default_excluded_without_repo_config(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "src" / "live.py", "def live():\n    return 1\n")
    _write(repo / "archive" / "old.py", "def retired():\n    return 0\n")
    rels = {p.relative_to(repo).as_posix() for p in sg.iter_source_files(repo)}
    assert "src/live.py" in rels
    assert "archive/old.py" not in rels


def test_runtime_logs_are_default_excluded_without_repo_config(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "src" / "live.py", "def live():\n    return 1\n")
    _write(repo / "logs" / "worker.json", '{"event": "runtime"}\n')
    _write(repo / "logs" / "nested" / "trace.py", "def runtime_trace():\n    return 0\n")
    rels = {p.relative_to(repo).as_posix() for p in sg.iter_source_files(repo)}
    assert "src/live.py" in rels
    assert not any(rel.startswith("logs/") for rel in rels)


def test_new_repo_policy_excludes_eval_artifacts_without_disabling_json(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    sg.ensure_ignore_config(repo)
    _write(repo / "eval" / "generated.json", '{"measurement": 1}\n')
    _write(repo / "eval" / "nested" / "rows.jsonl", '{"row": 1}\n')
    _write(repo / "config" / "runtime.json", '{"enabled": true}\n')

    rels = {path.relative_to(repo).as_posix() for path in sg.iter_source_files(repo)}

    assert "eval/generated.json" not in rels
    assert "eval/nested/rows.jsonl" not in rels
    assert "config/runtime.json" in rels
    policy = sg.source_graph_policy_view(repo)
    assert policy["exclude_globs"] == list(sg.DEFAULT_CONFIG_EXCLUDE_GLOBS)


def test_repo_ignore_policy_extends_defaults_with_dirs_and_globs(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    config = sg.ensure_ignore_config(repo)
    config.write_text(json.dumps({
        "schema_id": sg.IGNORE_SCHEMA_ID,
        "exclude_dirs": ["vendor"],
        "exclude_globs": ["generated/**", "**/*.min.js"],
    }), encoding="utf-8")
    _write(repo / "src" / "live.py", "def live():\n    return 1\n")
    _write(repo / "vendor" / "copy.py", "def vendor_copy():\n    return 0\n")
    _write(repo / "generated" / "nested" / "auto.py", "def generated():\n    return 0\n")
    _write(repo / "web" / "bundle.min.js", "module.exports = 1;\n")
    rels = {p.relative_to(repo).as_posix() for p in sg.iter_source_files(repo)}
    assert rels == {"src/live.py"}


def test_repo_language_policy_disables_and_reenables_cpp_incrementally(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "native" / "engine.cpp", "int main() { return 0; }\n")
    _write(repo / "config" / "runtime.json", "{}\n")
    first = sg.build_index(repo, incremental=True)
    assert first.files_seen == 2

    initial = sg.source_graph_policy_view(repo)
    disabled = sg.update_language_policy(
        repo,
        language_changes={"cpp": False},
        expected_revision=initial["revision"],
    )
    assert disabled["enabled_count"] == 33
    second = sg.build_index(repo, incremental=True)
    assert second.files_seen == 1
    assert second.files_removed == 1

    enabled = sg.update_language_policy(
        repo,
        language_changes={"cpp": True},
        expected_revision=disabled["revision"],
    )
    assert enabled["enabled_count"] == 34
    third = sg.build_index(repo, incremental=True)
    assert third.files_seen == 2
    assert third.files_changed == 1


def test_legacy_ignore_v1_migrates_without_losing_owner_rules(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    config = sg.ensure_ignore_config(repo)
    config.write_text(json.dumps({
        "schema_id": sg.IGNORE_SCHEMA_ID,
        "exclude_dirs": ["vendor"],
        "exclude_globs": ["generated/**"],
    }), encoding="utf-8")
    legacy = sg.source_graph_policy_view(repo)
    assert legacy["revision"] == 0
    updated = sg.update_language_policy(
        repo,
        language_changes={"xml": False},
        expected_revision=0,
    )
    assert updated["revision"] == 1
    assert updated["exclude_dirs"] == ["vendor"]
    assert updated["exclude_globs"] == ["generated/**"]
    stored = json.loads(config.read_text(encoding="utf-8"))
    assert stored["schema_id"] == sg.POLICY_SCHEMA_ID
    assert stored["disabled_languages"] == ["xml"]


def test_malformed_repo_ignore_policy_fails_closed(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    config = sg.ensure_ignore_config(repo)
    config.write_text('{"schema_id":"wrong","exclude_dirs":[]}', encoding="utf-8")
    _write(repo / "src" / "must_not_be_indexed.py", "def hidden():\n    return 1\n")
    with pytest.raises(sg.SourceGraphError, match="source_graph_ignore_invalid"):
        sg.iter_source_files(repo)


def test_incremental_build_removes_entries_newly_covered_by_ignore_policy(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "generated" / "old.py", "def generated_probe():\n    return 1\n")
    first = sg.build_index(repo, incremental=True)
    assert first.files_seen == 1

    config = sg.ensure_ignore_config(repo)
    config.write_text(json.dumps({
        "schema_id": sg.IGNORE_SCHEMA_ID,
        "exclude_dirs": ["generated"],
        "exclude_globs": [],
    }), encoding="utf-8")
    second = sg.build_index(repo, incremental=True)
    assert second.files_removed == 1
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        assert sg.func(conn, "generated_probe") == []
    finally:
        conn.close()


def test_repository_writer_lease_rejects_overlapping_build(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "src" / "live.py", "def live():\n    return 1\n")
    with sg.index_write_lease(repo) as acquired:
        assert acquired is True
        with pytest.raises(sg.SourceGraphBuildInProgressError, match="source_graph_build_in_progress"):
            sg.build_index(repo, incremental=True)


def _assert_lock_unavailable(exc, *, errno_name, repo):
    assert isinstance(exc, sg.SourceGraphLockUnavailableError)
    text = str(exc)
    assert "source_graph_lock_unavailable" in text
    assert f"errno={errno_name}" in text
    assert "operation=" in text
    assert "phase=" in text
    assert str(repo.resolve()) not in text
    assert not any(part.startswith("/") and part != "/" for part in text.replace(",", " ").split())
    assert exc.errno_name == errno_name
    payload = exc.to_json()
    assert payload["reason"] == "source_graph_lock_unavailable"
    assert payload["errno"] == errno_name
    assert payload["operation"]
    assert payload["phase"]
    assert str(repo.resolve()) not in json.dumps(payload)


def test_posix_eagain_eacces_remain_build_in_progress(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "posix_contention")
    _write(repo / "src" / "live.py", "def live():\n    return 1\n")
    import fcntl

    def _raise_eagain(*_args, **_kwargs):
        raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(fcntl, "flock", _raise_eagain)
    with sg.index_write_lease(repo) as acquired:
        assert acquired is False
    with pytest.raises(sg.SourceGraphBuildInProgressError, match="source_graph_build_in_progress"):
        sg.build_index(repo, incremental=True)

    def _raise_eacces(*_args, **_kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(fcntl, "flock", _raise_eacces)
    with sg.index_write_lease(repo) as acquired:
        assert acquired is False
    with pytest.raises(sg.SourceGraphBuildInProgressError, match="source_graph_build_in_progress"):
        sg.build_index(repo, incremental=True)


@pytest.mark.parametrize(
    "err",
    [
        errno.ENOLCK,
        errno.EOPNOTSUPP,
        errno.ENOSYS,
        errno.EINVAL,
    ],
)
def test_posix_non_contention_lock_errors_are_lock_unavailable(tmp_path, monkeypatch, err):
    repo = _new_repo(tmp_path, f"posix_lock_{err}")
    _write(repo / "src" / "live.py", "def live():\n    return 1\n")
    import fcntl

    def _raise_lock(*_args, **_kwargs):
        raise OSError(err, os.strerror(err))

    monkeypatch.setattr(fcntl, "flock", _raise_lock)
    with pytest.raises(sg.SourceGraphLockUnavailableError) as lease_info:
        with sg.index_write_lease(repo) as acquired:
            raise AssertionError(f"must not proceed unlocked: {acquired}")
    _assert_lock_unavailable(lease_info.value, errno_name=errno.errorcode[err], repo=repo)
    with pytest.raises(sg.SourceGraphLockUnavailableError) as build_info:
        sg.build_index(repo, incremental=True)
    _assert_lock_unavailable(build_info.value, errno_name=errno.errorcode[err], repo=repo)


def test_windows_byte_range_contention_stays_bounded_build_in_progress(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "windows_contention")
    _write(repo / "src" / "live.py", "def live():\n    return 1\n")
    calls = {"n": 0}

    def locking(_fd, mode, _nbytes):
        if mode == 2:
            return None
        calls["n"] += 1
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(sg, "_index_write_lease_platform", lambda: "nt")
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    clock = {"now": 0.0}

    def monotonic():
        clock["now"] += 0.2
        return clock["now"]

    monkeypatch.setattr(time, "monotonic", monotonic)
    with sg.index_write_lease(repo) as acquired:
        assert acquired is False
    assert calls["n"] >= 1
    with pytest.raises(sg.SourceGraphBuildInProgressError, match="source_graph_build_in_progress"):
        sg.build_index(repo, incremental=True)


def test_windows_unsupported_lock_is_not_build_in_progress(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "windows_unsupported")
    _write(repo / "src" / "live.py", "def live():\n    return 1\n")

    def locking(_fd, mode, _nbytes):
        if mode == 2:
            return None
        raise OSError(errno.ENOSYS, "Function not implemented")

    monkeypatch.setattr(sg, "_index_write_lease_platform", lambda: "nt")
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking),
    )
    with pytest.raises(sg.SourceGraphLockUnavailableError) as info:
        with sg.index_write_lease(repo) as acquired:
            raise AssertionError(f"must not proceed unlocked: {acquired}")
    _assert_lock_unavailable(info.value, errno_name="ENOSYS", repo=repo)
    assert info.value.operation == "msvcrt.locking"


def test_wal_readonly_query_can_read_during_writer_transaction(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "src" / "live.py", "def live_probe():\n    return 1\n")
    sg.build_index(repo, incremental=True)
    writer = sg.connect(sg.resolve_db_path(repo))
    writer.execute("BEGIN IMMEDIATE")
    try:
        reader = sg.connect(sg.resolve_db_path(repo), read_only=True)
        try:
            assert sg.func(reader, "live_probe")
        finally:
            reader.close()
    finally:
        writer.rollback()
        writer.close()


def test_isolated_readonly_directory_supports_repeated_queries_without_wal_sidecars(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "src" / "live.py", "def repeated_probe():\n    return 1\n")
    sg.build_index(repo, incremental=True)
    db_path = sg.resolve_db_path(repo)
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        connection.close()

    graph_dir = db_path.parent
    original_mode = graph_dir.stat().st_mode
    try:
        graph_dir.chmod(0o555)
    except PermissionError:
        pytest.skip(
            "sandbox denies chmod(0o555) (EPERM/EACCES) — "
            "read-only directory capability unavailable"
        )
    except OSError as exc:
        if exc.errno in (1, 13):  # EPERM, EACCES
            pytest.skip(
                "sandbox denies chmod(0o555) (EPERM/EACCES) — "
                "read-only directory capability unavailable"
            )
        raise
    try:
        for _ in range(3):
            reader = sg.connect(db_path, read_only=True)
            try:
                assert sg.func(reader, "repeated_probe")
            finally:
                reader.close()
    finally:
        graph_dir.chmod(original_mode)


def test_incremental_build_defers_live_database_compaction(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "repo")
    for index in range(300):
        _write(
            repo / "generated" / f"module_{index:04d}.py",
            f"def probe_{index}():\n    return {index}\n",
        )
    sg.build_index(repo, incremental=True)
    config = sg.ensure_ignore_config(repo)
    config.write_text(json.dumps({
        "schema_id": sg.IGNORE_SCHEMA_ID,
        "exclude_dirs": ["generated"],
        "exclude_globs": [],
    }), encoding="utf-8")
    monkeypatch.setattr(sg, "SOURCE_GRAPH_COMPACT_MIN_BYTES", 0)
    monkeypatch.setattr(sg, "SOURCE_GRAPH_COMPACT_MIN_FREELIST_RATIO", 0.000001)
    report = sg.build_index(repo, incremental=True)
    assert report.files_removed == 300
    assert report.freelist_ratio_before_compaction > 0
    assert report.compaction_performed is False
    assert report.compaction_error == ""
    assert report.compaction_recommended is True
    assert report.compaction_deferred_reason == "live_generation_in_use"
    assert report.database_bytes_after_compaction == report.database_bytes_before_compaction


def test_committed_generation_remains_queryable_during_next_build_extraction(
    tmp_path, monkeypatch,
):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "src" / "worker.py"
    _write(target, "def stable_probe():\n    return 1\n")
    sg.build_index(repo, incremental=False)
    _write(target, "def stable_probe():\n    return 2\n")

    entered = threading.Event()
    release = threading.Event()
    original_extract = sgast.extract_file
    blocked_once = False

    def delayed_extract(*args, **kwargs):
        nonlocal blocked_once
        if not blocked_once:
            blocked_once = True
            entered.set()
            assert release.wait(5)
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(sgast, "extract_file", delayed_extract)
    outcome: list[object] = []

    def build() -> None:
        try:
            outcome.append(sg.build_index(repo, incremental=True))
        except Exception as exc:  # pragma: no cover - asserted below
            outcome.append(exc)

    thread = threading.Thread(target=build, daemon=True)
    thread.start()
    assert entered.wait(5)
    try:
        queries = [sg.focus(repo, "stable_probe", 8) for _ in range(3)]
        assert all(result["matches"] for result in queries)
    finally:
        release.set()
        thread.join(10)
    assert len(outcome) == 1
    assert isinstance(outcome[0], sg.BuildReport)


def test_cmake_generated_ts_timestamp_files_excluded_not_mislabeled_typescript(tmp_path):
    """CMake writes non-source ``.ts`` dependency-timestamp files under
    ``CMakeFiles/`` -- these must never be indexed as truthful "typescript"
    evidence, since they are not TypeScript source at all."""

    repo = _new_repo(tmp_path, "repo")
    _write(repo / "src" / "real.ts", "export const real = 1;\n")
    _write(
        repo / "build" / "CMakeFiles" / "target.dir" / "compiler_depend.ts",
        "# CMAKE generated file: DO NOT EDIT!\n",
    )
    files = sg.iter_source_files(repo)
    rels = {p.relative_to(repo).as_posix() for p in files}
    assert "src/real.ts" in rels
    assert not any("CMakeFiles" in rel for rel in rels)


def test_js_ts_family_deterministic_byte_cap_on_slice(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    for i in range(20):
        _write(repo / "pkg" / f"module_probe_{i}.js", f"module.exports = {i};\n")
    sg.build_index(repo, incremental=True)

    payload = sg.focus(repo, "module_probe", budget=5)
    assert payload["mode"] == "focus"
    assert len(payload["matches"]) <= 5
    encoded = json.dumps(payload).encode("utf-8")
    assert len(encoded) <= max(512, 5 * 512)
    for match in payload["matches"]:
        assert match["evidence_label"] == sgast.EXTRACTED


def test_python_ast_extraction_unchanged_alongside_js_ts_family(tmp_path):
    """Preserve the Python semantic graph when JS/TS semantics coexist."""

    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", "def caller():\n    return callee()\n\ndef callee():\n    return 1\n")
    _write(repo / "pkg" / "widget.ts", "export const widget = 1;\n")
    report = sg.build_index(repo, incremental=True)
    assert report.errors == []

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        py_matches = sg.func(conn, "caller")
        assert len(py_matches) == 1
        assert py_matches[0]["evidence_label"] == sgast.EXTRACTED
        edge = conn.execute(
            "SELECT dst_name, evidence_label FROM edges WHERE src_qualname=? AND kind='calls'",
            ("pkg/core.py.caller",),
        ).fetchone()
        assert edge["dst_name"] == "callee"
        assert edge["evidence_label"] == sgast.EXTRACTED

        ts_context = sg.context(conn, "pkg/widget.ts")
        assert ts_context["found"] is True
        assert ts_context["edges"] == []
        assert len(ts_context["entities"]) == 1
        assert ts_context["entities"][0]["kind"] == "module"
        assert ts_context["entities"][0]["evidence_label"] == sgast.EXTRACTED
    finally:
        conn.close()


@pytest.mark.parametrize(
    "filename,language,source,type_name,call_name",
    [
        (
            "engine.rs",
            "rust",
            "use crate::util;\n"
            "struct Engine {}\n"
            "impl Engine {\n"
            "    fn run(&self) { helper(); }\n"
            "}\n"
            "fn helper() {}\n",
            "Engine",
            "helper",
        ),
        (
            "engine.go",
            "go",
            "package engine\n"
            "import \"fmt\"\n"
            "type Engine struct {}\n"
            "func (e *Engine) Run() { helper() }\n"
            "func helper() {}\n",
            "Engine",
            "helper",
        ),
        (
            "Engine.java",
            "java",
            "import java.util.List;\n"
            "class Engine extends Base {\n"
            "    int run() { return helper(); }\n"
            "    int helper() { return 1; }\n"
            "}\n",
            "Engine",
            "helper",
        ),
        (
            "Engine.cs",
            "csharp",
            "using System;\n"
            "class Engine : Base {\n"
            "    int Run() { return Helper(); }\n"
            "    int Helper() { return 1; }\n"
            "}\n",
            "Engine",
            "Helper",
        ),
    ],
)
def test_polyglot_semantic_adapters_extract_types_functions_imports_and_calls(
    tmp_path, filename, language, source, type_name, call_name,
):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "src" / filename
    _write(target, source)

    extraction = sgast.extract_file(repo, target, build_revision="test-rev")

    assert extraction.status == "ok"
    assert extraction.language == language
    assert any(entity.name == type_name for entity in extraction.entities)
    assert any(entity.name == call_name for entity in extraction.entities)
    assert any(edge.kind == "imports" for edge in extraction.edges)
    assert any(
        edge.kind == "calls"
        and edge.dst_name == call_name
        and edge.evidence_label == sgast.EXTRACTED
        for edge in extraction.edges
    )
    assert all(
        entity.extractor in {
            sgast.POLYGLOT_LEXICAL_EXTRACTOR_ID,
            sgast.TREE_SITTER_JS_TS_EXTRACTOR_ID,
        }
        for entity in extraction.entities
    )


def test_typescript_semantic_adapter_extracts_class_method_arrow_and_import(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "web" / "widget.ts"
    _write(
        target,
        "import { helper } from './util';\n"
        "// import { fake } from './fake';\n"
        "export class Widget extends Base {\n"
        "    run() { return helper(); }\n"
        "}\n"
        "export const arrow = () => { return helper(); };\n",
    )

    extraction = sgast.extract_file(repo, target, build_revision="test-rev")

    names = {(entity.kind, entity.name) for entity in extraction.entities}
    assert extraction.status == "ok"
    assert {("class", "Widget"), ("method", "run"), ("function", "arrow")} <= names
    assert any(edge.kind == "imports" and edge.dst_name == "./util" for edge in extraction.edges)
    assert not any(edge.kind == "imports" and edge.dst_name == "./fake" for edge in extraction.edges)
    assert any(edge.kind == "inherits" and edge.dst_name == "Base" for edge in extraction.edges)
    assert sum(edge.kind == "calls" and edge.dst_name == "helper" for edge in extraction.edges) == 2


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

def test_incremental_build_skips_unchanged_without_reparsing(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "a.py", "def a():\n    return 1\n")
    original_extract = sgast.extract_file
    extracted: list[str] = []

    def counted_extract(repo_root, path, *, build_revision):
        extracted.append(path.relative_to(repo_root).as_posix())
        return original_extract(repo_root, path, build_revision=build_revision)

    monkeypatch.setattr(sgast, "extract_file", counted_extract)
    r1 = sg.build_index(repo, incremental=True)
    assert r1.files_changed == 1 and r1.files_unchanged == 0
    assert extracted == ["pkg/a.py"]

    extracted.clear()
    r2 = sg.build_index(repo, incremental=True)
    assert r2.files_changed == 0 and r2.files_unchanged == 1
    assert extracted == []

    _write(repo / "pkg" / "a.py", "def a():\n    return 2\n\ndef b():\n    return 3\n")
    r3 = sg.build_index(repo, incremental=True)
    assert r3.files_changed == 1 and r3.files_unchanged == 0
    assert extracted == ["pkg/a.py"]


def test_multicore_extraction_is_bounded_and_merge_order_is_deterministic(
    tmp_path, monkeypatch,
):
    repo = _new_repo(tmp_path, "parallel_extract")
    names = ["a.py", "b.py", "c.py", "d.py"]
    for name in names:
        _write(repo / "pkg" / name, f"def {name[0]}():\n    return 1\n")

    class _DeterministicProcessPoolExecutor:
        """Drives the real process_pool success branch in-process so this
        regression stays deterministic under sandboxes that deny process
        creation, instead of silently falling back to one worker."""

        def __init__(self, *, max_workers, mp_context=None):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def map(self, fn, iterable, chunksize=1):
            return [fn(item) for item in iterable]

    monkeypatch.setenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "2")
    monkeypatch.setattr(
        sg.concurrent.futures, "ProcessPoolExecutor", _DeterministicProcessPoolExecutor
    )
    original_write = sg._write_extraction
    write_order: list[str] = []

    def tracked_write(conn, extraction, *, file_size, mtime_ns):
        write_order.append(extraction.file_path)
        return original_write(
            conn, extraction, file_size=file_size, mtime_ns=mtime_ns
        )

    monkeypatch.setattr(sg, "_write_extraction", tracked_write)

    report = sg.build_index(repo, incremental=False)

    assert report.extraction_workers == 2
    assert report.extraction_backend == "process_pool"
    assert report.extraction_fallback_reason == ""
    assert report.extraction_seconds > 0
    assert report.files_changed == 4
    assert write_order == [f"pkg/{name}" for name in names]
    assert report.extraction_telemetry["selected_workers"] == 2
    assert report.extraction_telemetry["reason"] == "env_override"
    assert set(report.phase_seconds) == {
        "extraction", "git_metrics", "hash", "merge", "quality", "resolution", "total",
    }
    assert all(seconds >= 0 for seconds in report.phase_seconds.values())
    assert report.phase_seconds["total"] >= report.phase_seconds["extraction"]
    assert report.to_json()["phase_seconds"] == report.phase_seconds


def test_multicore_extraction_worker_configuration_is_bounded(monkeypatch):
    monkeypatch.setenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "999999")
    assert sg._source_graph_extract_workers(100) == sg.MAX_SOURCE_GRAPH_EXTRACT_WORKERS

    monkeypatch.setenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "invalid")
    assert sg._source_graph_extract_workers(5) == 1

    monkeypatch.setenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "8")
    assert sg._source_graph_extract_workers(1) == 1

    monkeypatch.delenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV)
    assert sg._source_graph_extract_workers(8, 255 * 1024) == 1
    with monkeypatch.context() as context:
        context.setattr(sg.parallelism, "get_cpu_capacity", lambda: 6)
        assert sg._source_graph_extract_workers(8, 256 * 1024) == 5


def test_source_graph_multicore_is_serial_when_nested(monkeypatch):
    monkeypatch.delenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, raising=False)
    monkeypatch.setattr(sg.parallelism, "get_cpu_capacity", lambda: 16)
    with sg.parallelism.worker_pool_scope():
        assert sg._source_graph_extract_workers(20, 512 * 1024) == 1


def test_source_graph_extraction_receipt_reports_capacity(monkeypatch):
    monkeypatch.delenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, raising=False)
    monkeypatch.setattr(sg.parallelism, "get_cpu_capacity", lambda: 8)
    workers = sg._source_graph_extract_workers(20, 512 * 1024)
    receipt = sg._source_graph_extraction_telemetry(workers, 20, 512 * 1024)
    assert receipt == {
        "available_cpus": 8,
        "selected_workers": 7,
        "reserve": 1,
        "ceiling": sg.MAX_SOURCE_GRAPH_EXTRACT_WORKERS,
        "nested": False,
        "reason": "capacity_based",
    }


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


def test_all_repository_neutral_analytics_are_bounded_and_canonical(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "service.py",
        "def important_service(value):\n"
        "    if value:\n"
        "        return helper(value)\n"
        "    return 0\n\n"
        "def helper(value):\n"
        "    return value + 1\n",
    )
    _write(
        repo / "tests" / "test_service.py",
        "from pkg.service import important_service\n\n"
        "def test_service():\n"
        "    assert important_service(1) == 2\n",
    )
    sg.build_index(repo, incremental=True)

    analytic_modes = set(sg.SOURCE_GRAPH_MODES) - {
        "focus", "slice", "context", "file", "function", "class", "body", "bodygrep",
        "impact", "trace", "deps", "bundle",
    }
    assert analytic_modes == {
        "tags", "hotspots", "coverage", "churn", "reviewqueue", "ownership",
        "testmap", "calls", "symbols", "bottlenecks", "auditmap", "complexity",
        "stats", "summarize", "pipeline",
        "todo", "leaks", "nullrisks", "rawptrs", "casts", "crashes",
        "looprisks", "deadmethods", "duplicates", "gaps",
    }
    for mode in sorted(analytic_modes):
        payload = sg.analytics_query(repo, mode, "important_service", budget=12)
        assert payload["mode"] == mode
        assert payload["query"] == "important_service"
        assert len(json.dumps(payload).encode("utf-8")) <= 12 * 768


def test_coverage_and_auditmap_never_fabricate_runtime_coverage(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", "def covered_target():\n    return 1\n")
    _write(
        repo / "tests" / "test_core.py",
        "from pkg.core import covered_target\n\ndef test_target():\n    assert covered_target() == 1\n",
    )
    sg.build_index(repo, incremental=True)

    for mode in ("coverage", "testmap", "auditmap"):
        payload = sg.analytics_query(repo, mode, "covered_target", budget=20)
        assert payload["structural_mapping"]["status"] == "available"
        assert payload["structural_mapping"]["claim"] == (
            "test_relationship_only_not_execution_coverage"
        )
        assert payload["runtime_coverage"] == {
            "status": "not_available",
            "line_coverage": None,
            "branch_coverage": None,
            "reason": "no_runtime_coverage_evidence_imported",
        }


def test_worker_mcp_exposes_dedicated_source_graph_analytics(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", "def analytics_probe():\n    return 1\n")
    sg.build_index(repo, incremental=True)
    ctx = w.WorkerToolContext(
        task_id="t-analytics", runner="r", topic="topic", request_id="req-analytics",
        repo=repo, authority_repo=repo, source_graph_targets=(),
        session_topic="topic", audit_ledger_path=None, audit_hmac_key_path=None,
    )
    result = w.source_graph_query(
        ctx, mode="complexity", query="analytics_probe", budget=12,
    )
    assert result["ok"] is True
    payload = json.loads(result["content"])
    assert payload["mode"] == "complexity"
    assert payload["ranked_symbols"][0]["name"] == "analytics_probe"


def test_source_graph_risk_views_are_explicit_nonblocking_candidates(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "native" / "lifetime.cpp",
        "int risky(Item *item, int divisor) {\n"
        "    while (true) { item->tick(); }\n"
        "    return 8 / divisor;\n"
        "}\n",
    )
    sg.build_index(repo, incremental=True)

    loop = sg.analytics_query(repo, "looprisks", "risky", budget=20)
    assert loop["analysis"]["blocking"] is False
    assert loop["analysis"]["findings"][0]["evidence_class"] == (
        "bounded_lexical_candidate_not_proven_defect"
    )
    crash = sg.analytics_query(repo, "crashes", "risky", budget=20)
    assert any(
        reason.startswith("unchecked_divisor")
        for row in crash["analysis"]["findings"]
        for reason in row["reasons"]
    )


# ---------------------------------------------------------------------------
# NF171: engine-authoritative analytics scope, cursor pagination, coverage truth
# ---------------------------------------------------------------------------

_NO_MATCH_QUERY = "zzz_does_not_match_anything_zzz"


def test_analytics_query_target_scope_filters_and_reports_coverage(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "in_scope" / "mod.py",
        "def alpha_symbol():\n    return 1\n\n\ndef beta_symbol():\n    return 2\n",
    )
    _write(
        repo / "pkg" / "other" / "mod.py",
        "def gamma_symbol():\n    return 3\n",
    )
    sg.build_index(repo, incremental=True)

    payload = sg.analytics_query(
        repo, "symbols", _NO_MATCH_QUERY, budget=10, target="pkg/in_scope",
    )
    names = {row["name"] for row in payload["symbols"]}
    assert names == {"alpha_symbol", "beta_symbol"}
    assert payload["target"] == "pkg/in_scope"
    assert payload["coverage"] == {
        "scanned": 2, "eligible": 2, "eligible_capped": False,
        "returned": 2, "requested_budget": 10, "effective_budget": 2,
    }
    assert payload["next_cursor"] is None
    assert payload["truncated"] is False

    unscoped = sg.analytics_query(repo, "symbols", _NO_MATCH_QUERY, budget=10)
    unscoped_names = {row["name"] for row in unscoped["symbols"]}
    assert unscoped_names >= {"alpha_symbol", "beta_symbol", "gamma_symbol"}


def test_analytics_query_empty_scope_never_falls_back_to_repository_wide(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "mod.py", "def only_symbol():\n    return 1\n")
    sg.build_index(repo, incremental=True)

    payload = sg.analytics_query(
        repo, "symbols", _NO_MATCH_QUERY, budget=10, target="pkg/does_not_exist",
    )
    assert payload.get("symbols") is None
    assert payload["scope"] == "target_scope_empty"
    assert payload["coverage"] == {
        "scanned": 0, "eligible": 0, "eligible_capped": False,
        "returned": 0, "requested_budget": 10, "effective_budget": 0,
    }
    assert payload["next_cursor"] is None


def test_analytics_query_cursor_paginates_through_scoped_corpus(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "mod.py",
        "def page_alpha():\n    return 1\n\n\n"
        "def page_beta():\n    return 2\n\n\n"
        "def page_gamma():\n    return 3\n",
    )
    sg.build_index(repo, incremental=True)

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        payload = sg.analytics_query(
            repo, "symbols", _NO_MATCH_QUERY, budget=1, target="pkg", cursor=cursor,
        )
        assert len(payload["symbols"]) == 1
        seen.append(payload["symbols"][0]["name"])
        assert payload["coverage"]["requested_budget"] == 1
        assert payload["coverage"]["effective_budget"] == 1
        pages += 1
        cursor = payload["next_cursor"]
        if cursor is None:
            assert payload["truncated"] is False
            break
        assert payload["truncated"] is True
        assert pages < 10  # fail-fast guard against a pagination loop bug

    assert pages == 3
    assert seen == ["page_alpha", "page_beta", "page_gamma"]


def test_analytics_query_rejects_stale_or_malformed_cursor(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "mod.py",
        "def cursor_alpha():\n    return 1\n\n\ndef cursor_beta():\n    return 2\n",
    )
    sg.build_index(repo, incremental=True)

    first = sg.analytics_query(repo, "symbols", _NO_MATCH_QUERY, budget=1)
    valid_cursor = first["next_cursor"]
    assert valid_cursor is not None

    with pytest.raises(sg.SourceGraphError):
        sg.analytics_query(repo, "symbols", "a_different_query", budget=1, cursor=valid_cursor)
    with pytest.raises(sg.SourceGraphError):
        sg.analytics_query(repo, "symbols", _NO_MATCH_QUERY, budget=2, cursor=valid_cursor)
    with pytest.raises(sg.SourceGraphError):
        sg.analytics_query(repo, "symbols", _NO_MATCH_QUERY, budget=1, cursor="not-a-cursor")
    with pytest.raises(sg.SourceGraphError):
        sg.analytics_query(repo, "symbols", _NO_MATCH_QUERY, budget=1, cursor="9999:deadbeefdeadbeef")


def test_analytics_query_empty_repository_reports_zero_coverage(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    sg.build_index(repo, incremental=True)

    payload = sg.analytics_query(repo, "symbols", _NO_MATCH_QUERY, budget=10)
    assert payload["symbols"] == []
    assert payload["coverage"] == {
        "scanned": 0, "eligible": 0, "eligible_capped": False,
        "returned": 0, "requested_budget": 10, "effective_budget": 0,
    }
    assert payload["next_cursor"] is None


def _analytics_nested_repo_wide_counts(node):
    """Every ``files``/``entities`` int counter anywhere in the payload."""

    found = []
    if isinstance(node, dict):
        if isinstance(node.get("files"), int):
            found.append(("files", node["files"]))
        if isinstance(node.get("entities"), int):
            found.append(("entities", node["entities"]))
        for value in node.values():
            found.extend(_analytics_nested_repo_wide_counts(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_analytics_nested_repo_wide_counts(item))
    return found


def test_analytics_query_stats_summary_gaps_never_leak_repository_wide_scope(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "inside" / "mod.py",
        "def alpha_symbol():\n    return 1\n\n\ndef beta_symbol():\n    return 2\n",
    )
    _write(repo / "pkg" / "other" / "mod.py", "def gamma_symbol():\n    return 3\n")
    sg.build_index(repo, incremental=True)

    byte_cap = max(512, 1 * 768)
    for mode in ("stats", "summarize", "gaps", "pipeline"):
        payload = sg.analytics_query(
            repo, mode, _NO_MATCH_QUERY, budget=1, target="pkg/inside",
        )
        # Byte cap: the fully assembled response -- content plus the
        # coverage/cursor block itself -- must fit under the cap, not just
        # the per-mode content that existed before coverage was attached.
        assert len(json.dumps(payload).encode("utf-8")) <= byte_cap
        assert payload["target"] == "pkg/inside"
        # Under the truthful-coverage contract (finding SEVEN) ``scanned``
        # reports what the analytic actually examined for this budget, not the
        # whole scoped corpus: budget=1 hands exactly one row to the mode, so
        # ``scanned`` is 1. It reported 2 (the whole scoped corpus) before this
        # fix; 2 was the count the tool never actually examined.
        assert payload["coverage"]["scanned"] == 1
        # ``eligible`` still reports the honest "how much could have been
        # scanned": ``pkg/inside`` owns exactly 2 scoped entities, and that
        # total stays 2 even though only 1 was examined on this page.
        assert payload["coverage"]["eligible"] == 2
        # Leak guard (the whole point of this test): every ``files``/
        # ``entities`` counter anywhere in the nested payload must reflect the
        # scope (<= 2) and may never surface the repository-wide entity total
        # (2 files / 5 entities). 1 is not 5, so the scope guard is intact.
        for label, value in _analytics_nested_repo_wide_counts(payload):
            assert value <= 2, f"{mode}.{label} leaked repository-wide count: {value}"
            assert value != 5, f"{mode}.{label} leaked repository-wide entity total: {value}"
        # ``effective_budget`` must describe what this page actually
        # delivered, not a generic corpus-page length independent of the
        # mode's own (possibly zero) result.
        assert payload["coverage"]["effective_budget"] == payload["coverage"]["returned"]

    # ``pkg/inside`` owns exactly 1 file, 2 functions, 1 module entity and
    # 2 "defines" edges. ``stats``/``summarize`` must report those *exact*
    # scoped values -- not merely "no bigger than 2" -- for every aggregate
    # a per-mode analytic nests (``files_by_language``, ``entities_by_kind``,
    # ``edges``), never the repository-wide totals (2 files, 3 functions,
    # 2 modules, 3 edges, ``files_by_language`` python=2).
    expected_scoped_summary = {
        "files": 1,
        "entities": 2,
        "entities_by_kind": {"function": 2, "module": 1},
        "files_by_language": {"python": 1},
        "edges": 2,
    }
    stats_payload = sg.analytics_query(
        repo, "stats", _NO_MATCH_QUERY, budget=1, target="pkg/inside",
    )
    for key, expected in expected_scoped_summary.items():
        assert stats_payload[key] == expected, f"stats.{key}"
    summarize_payload = sg.analytics_query(
        repo, "summarize", _NO_MATCH_QUERY, budget=1, target="pkg/inside",
    )
    for key, expected in expected_scoped_summary.items():
        assert summarize_payload["repository"][key] == expected, f"summarize.repository.{key}"

    # Scoping must never leak the other direction either: an unscoped call
    # still sees the true repository-wide totals.
    unscoped = sg.analytics_query(repo, "stats", _NO_MATCH_QUERY, budget=1)
    assert unscoped["files"] == 2
    assert unscoped["entities_by_kind"] == {"function": 3, "module": 2}
    assert unscoped["files_by_language"] == {"python": 2}
    assert unscoped["edges"] == 3


def test_scoped_repo_aggregates_matches_owning_file_boundary(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "inside" / "mod.py",
        "def alpha_symbol():\n    return 1\n\n\ndef beta_symbol():\n    return 2\n",
    )
    _write(repo / "pkg" / "other" / "mod.py", "def gamma_symbol():\n    return 3\n")
    sg.build_index(repo, incremental=True)

    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        aggregates = sg._scoped_repo_aggregates(conn, "pkg/inside")
    finally:
        conn.close()
    assert aggregates == {
        "files_by_language": {"python": 1},
        "entities_by_kind": {"function": 2, "module": 1},
        "edges": 2,
    }


def test_analytics_row_in_scope_keeps_out_of_scope_import_call_edges():
    # A row owned by an in-scope file must stay in scope even when its
    # target crosses outside that scope -- an in-scope file importing or
    # calling an out-of-scope symbol is exactly the evidence a bounded
    # scope query should surface for the file it owns, not evidence to
    # silently drop because the *target* happens to live elsewhere.
    owned_edge = {"file_path": "pkg/inside/mod.py", "dst_file_path": "pkg/other/mod.py"}
    assert sg._analytics_row_in_scope(owned_edge, "pkg/inside") is True

    # A row owned by an out-of-scope file must still be dropped, even when
    # its target happens to land inside the scope.
    foreign_edge = {"file_path": "pkg/other/mod.py", "dst_file_path": "pkg/inside/mod.py"}
    assert sg._analytics_row_in_scope(foreign_edge, "pkg/inside") is False

    src_field_variant = {
        "src_file_path": "pkg/inside/mod.py", "dst_file": "pkg/other/mod.py",
    }
    assert sg._analytics_row_in_scope(src_field_variant, "pkg/inside") is True


def test_analytics_query_never_pages_into_a_duplicate_result(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "pkg" / "inside" / "mod.py",
        "def alpha_symbol():\n    return 1\n\n\ndef beta_symbol():\n    return 2\n",
    )
    _write(repo / "pkg" / "other" / "mod.py", "def gamma_symbol():\n    return 3\n")
    sg.build_index(repo, incremental=True)

    for mode in ("stats", "summarize", "gaps", "pipeline"):
        payload = sg.analytics_query(
            repo, mode, _NO_MATCH_QUERY, budget=1, target="pkg/inside",
        )
        cursor = payload["next_cursor"]
        if cursor is None:
            # No second page was offered at all -- the safest way to never
            # repeat a result is to not paginate past it in the first place.
            continue
        payload2 = sg.analytics_query(
            repo, mode, _NO_MATCH_QUERY, budget=1, target="pkg/inside", cursor=cursor,
        )
        content1 = {k: v for k, v in payload.items() if k not in ("cursor", "next_cursor")}
        content2 = {k: v for k, v in payload2.items() if k not in ("cursor", "next_cursor")}
        assert content1 != content2, f"{mode} page 2 repeated page 1 verbatim"


def test_analytics_query_aggregate_modes_reject_a_forged_cursor(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "mod.py", "def only_symbol():\n    return 1\n")
    sg.build_index(repo, incremental=True)

    # ``stats``/``pipeline`` never mint a cursor of their own (see
    # ``_ANALYTICS_RESULT_KEYS``); a nonzero-offset cursor forged with the
    # engine's own encoder must still be rejected rather than silently
    # accepted as a second, identical page.
    for mode in ("stats", "pipeline"):
        forged = sg._encode_analytics_cursor(
            1, mode=mode, query=_NO_MATCH_QUERY, target="", budget=1,
        )
        with pytest.raises(sg.SourceGraphError):
            sg.analytics_query(repo, mode, _NO_MATCH_QUERY, budget=1, cursor=forged)


def test_javascript_same_file_import_bindings_resolve_deterministically(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "src" / "a" / "deep" / "target.js",
        "function helper() {\n  return 1;\n}\n",
    )
    _write(
        repo / "src" / "a" / "main.js",
        "import { helper } from './deep/target';\n\n"
        "function run() {\n  return helper();\n}\n",
    )
    _write(repo / "src" / "a" / "other.js", "function otherHelper() {\n  return 1;\n}\n")
    _write(repo / "src" / "b" / "other.js", "function otherHelper() {\n  return 2;\n}\n")
    _write(
        repo / "src" / "a" / "main2.js",
        "import { otherHelper } from './other';\n\n"
        "function run2() {\n  return otherHelper();\n}\n",
    )
    _write(repo / "src" / "a" / "unpackaged.js", "import react from 'react';\n")
    sg.build_index(repo, incremental=True)

    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        unique_import = conn.execute(
            "SELECT dst_qualname, evidence_label FROM edges WHERE kind='imports' "
            "AND file_path='src/a/main.js' AND dst_name='./deep/target'"
        ).fetchone()
        assert unique_import["dst_qualname"] == "src/a/deep/target.js"
        assert unique_import["evidence_label"] == sgast.EXTRACTED

        ambiguous_import = conn.execute(
            "SELECT dst_qualname, evidence_label FROM edges WHERE kind='imports' "
            "AND file_path='src/a/main2.js' AND dst_name='./other'"
        ).fetchone()
        assert ambiguous_import["dst_qualname"] is None
        assert ambiguous_import["evidence_label"] == sgast.AMBIGUOUS

        package_import = conn.execute(
            "SELECT dst_qualname, evidence_label FROM edges WHERE kind='imports' "
            "AND file_path='src/a/unpackaged.js' AND dst_name='react'"
        ).fetchone()
        assert package_import["dst_qualname"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration: verified copy, parity, rollback metadata, idempotent cutover
# ---------------------------------------------------------------------------

def test_migration_skips_cleanly_when_no_legacy_source_exists(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    absent_source = tmp_path / "external_migration_source" / "source_graph.db"
    report = sgm.migrate_legacy_db(repo, db_id="source_graph", legacy_source=absent_source, dry_run=True)
    assert report.status == "no_legacy_source_skip"
    assert report.parity_ok is True


def test_migration_dry_run_verifies_without_writing_canonical(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    legacy_path = tmp_path / "external_migration_source" / "source_graph.db"
    legacy_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(legacy_path))
    conn.execute("CREATE TABLE files(path TEXT)")
    conn.execute("INSERT INTO files VALUES ('a.py'), ('b.py')")
    conn.commit()
    conn.close()
    legacy_sha_before = sgm._sha256_file(legacy_path)

    report = sgm.migrate_legacy_db(repo, db_id="source_graph", legacy_source=legacy_path, dry_run=True)
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
    """B878: a freshly bootstrapped repository's registry entries are already
    ``canonical_active`` from birth (canonical-only storage, no shadow
    phase), so ``perform_cutover`` is a no-op ``already_cutover`` from the
    very first call -- there is nothing left to cut over to. The verified,
    read-only migration copy still runs and must still leave the canonical
    database populated and the legacy source untouched."""
    repo = _new_repo(tmp_path, "repo")
    legacy_path = tmp_path / "external_migration_source" / "source_graph.db"
    legacy_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(legacy_path))
    conn.execute("CREATE TABLE files(path TEXT)")
    conn.execute("INSERT INTO files VALUES ('a.py')")
    conn.commit()
    conn.close()

    registry_before = load_storage_registry(repo)
    assert registry_before.databases["source_graph"].canonical_active is True

    report = sgm.migrate_legacy_db(repo, db_id="source_graph", legacy_source=legacy_path, dry_run=False)
    assert report.status == "migrated_and_verified"
    assert Path(report.canonical_path).exists()
    assert Path(report.canonical_path).is_relative_to(repo / HUB_DIRNAME / "source_graph")

    cutover_1 = sgm.perform_cutover(repo, "source_graph", parity_ok=report.parity_ok)
    assert cutover_1["status"] == "already_cutover"
    assert cutover_1["generation"] == 1

    cutover_2 = sgm.perform_cutover(repo, "source_graph", parity_ok=report.parity_ok)
    assert cutover_2["status"] == "already_cutover"
    assert cutover_2["generation"] == 1  # idempotent: no double-increment

    registry = load_storage_registry(repo)
    db = registry.databases["source_graph"]
    assert db.canonical_active is True
    assert db.authority_state == "canonical_active"


def test_cutover_applies_and_is_then_idempotent_for_a_not_yet_canonical_entry(tmp_path):
    """A registry entry that is explicitly not yet canonical (the shape an
    older, richer migration/cutover tool would still produce) still takes
    the real ``cutover_applied`` -> ``already_cutover`` path."""
    repo = _new_repo(tmp_path, "repo")
    registry_path = repo / HUB_DIRNAME / "config" / "storage.json"
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    entry = next(item for item in payload["databases"] if item["id"] == "source_graph")
    entry["authority"] = {
        "state": "shadow", "canonical_active": False, "legacy_active": False, "live_cutover": False,
    }
    registry_path.write_text(json.dumps(payload), encoding="utf-8")

    cutover_1 = sgm.perform_cutover(repo, "source_graph", parity_ok=True)
    assert cutover_1["status"] == "cutover_applied"
    assert cutover_1["generation"] == 1

    cutover_2 = sgm.perform_cutover(repo, "source_graph", parity_ok=True)
    assert cutover_2["status"] == "already_cutover"
    assert cutover_2["generation"] == 1


def test_cutover_refuses_without_parity(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    with pytest.raises(sgm.MigrationError):
        sgm.perform_cutover(repo, "source_graph", parity_ok=False)


def test_migration_never_writes_legacy_path_even_on_real_run(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    legacy_path = tmp_path / "external_migration_source" / "source_graph_universal.db"
    legacy_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(legacy_path))
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    before = legacy_path.read_bytes()

    sgm.migrate_legacy_db(repo, db_id="universal", legacy_source=legacy_path, dry_run=False)
    assert legacy_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Production callers query aiworkhub.source_graph directly, in-process
# ---------------------------------------------------------------------------

def test_project_context_calls_canonical_module_without_subprocess(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", "def project_ctx_probe():\n    return 1\n")
    sg.build_index(repo, incremental=True)

    def _forbidden_subprocess_run(*args, **kwargs):
        raise AssertionError("project_context must not shell out for source_graph")

    monkeypatch.setattr(
        pc._worker_tools,
        "session_current_state",
        lambda ctx, limit=12: {
            "ok": True, "content": "{}", "truncated": False, "hit_count": 0,
        },
    )
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
    expected_repo_id = inspect_repository(repo).manifest.repo_id
    prompt_payload = json.loads(
        result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1]
    )
    assert prompt_payload["repo_identity"]["repo_id"] == expected_repo_id
    assert result.metadata["repo_identity"]["repo_id"] == expected_repo_id


def test_project_context_no_operational_dependency_on_repository_helper_scripts(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "core.py", "def only_probe():\n    return 1\n")
    sg.build_index(repo, incremental=True)
    assert not (repo / "AITools").exists()

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
    monkeypatch.setattr(
        pc_mod._worker_tools,
        "session_current_state",
        lambda ctx, limit=12: {
            "ok": True, "content": "{}", "truncated": False, "hit_count": 0,
        },
    )
    result = pc_mod.collect_project_context(repo, card)
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


def test_worker_exact_body_can_recover_within_declared_target_set(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(repo / "pkg" / "orientation.py", "def orientation_probe():\n    return 1\n")
    _write(repo / "pkg" / "status.py", "def DBAccountStatus():\n    return 'ok'\n")
    sg.build_index(repo, incremental=True)
    ctx = w.WorkerToolContext(
        task_id="t-body", runner="r", topic="topic", request_id="req-body",
        repo=repo, authority_repo=repo,
        source_graph_targets=("pkg/orientation.py", "pkg/status.py"),
        session_topic="topic", audit_ledger_path=None, audit_hmac_key_path=None,
    )

    result = w.source_graph_query(
        ctx,
        mode="body",
        query="DBAccountStatus",
        target="pkg/orientation.py",
        budget=10,
        workflow_stage="review",
    )

    assert result["ok"] is True
    assert result["workflow_stage"] == "review"
    payload = json.loads(result["content"])
    assert payload["scope"] == "declared_target_fallback"
    assert payload["requested_target"] == "pkg/orientation.py"
    assert any(row["file_path"] == "pkg/status.py" for row in payload["matches"])


def test_worker_cpp_body_matches_manager_exact_symbol_query(tmp_path):
    repo = _new_repo(tmp_path, "repo")
    _write(
        repo / "LoginServer" / "LoginQueue.cpp",
        "void DBAccountStatus(int account_id) {\n"
        "  update(account_id);\n"
        "}\n",
    )
    sg.build_index(repo, incremental=True)
    manager = sg.body_query(repo, "DBAccountStatus", 16)
    ctx = w.WorkerToolContext(
        task_id="t-cpp-body", runner="r", topic="topic", request_id="req-cpp-body",
        repo=repo, authority_repo=repo,
        source_graph_targets=("LoginServer/LoginQueue.cpp",),
        session_topic="topic", audit_ledger_path=None, audit_hmac_key_path=None,
    )

    worker = w.source_graph_query(
        ctx,
        mode="body",
        query="DBAccountStatus",
        target="LoginServer/LoginQueue.cpp",
        budget=16,
        workflow_stage="review",
    )

    assert manager["matches"]
    assert worker["ok"] is True
    payload = json.loads(worker["content"])
    assert payload["matches"] == manager["matches"]


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


# ---------------------------------------------------------------------------
# NF56 retained-rework overlay: materializer + consumer
# ---------------------------------------------------------------------------

import base64
import json
import hashlib
import threading
from aiworkhub.worker_workspace import (
    materialize_rework_overlay,
    ReworkOverlayPacket,
)
from aiworkhub.worker_ai_tools_mcp import (
    _verify_rework_overlay_packet,
    _build_rework_overlay_map,
    _apply_rework_overlay_query,
    WorkerToolContext,
    WorkerToolError,
)


def _sample_packet(successor_req="req-abc", successor_task="task-1",
                   predecessor_req="req-xyz", predecessor_task="task-2",
                   files=None, authority_repo=None):
    if authority_repo is None:
        authority_repo = Path("/tmp/authority")
    if files is None:
        overlay_content = b"# overlay\n"
        files = [{"path": "src/mod.py", "sha256": hashlib.sha256(overlay_content).hexdigest(), "content_base64": base64.b64encode(overlay_content).decode()},
                 {"path": "deleted.py", "deleted": True}]
    payload = {
        "successor_request_id": successor_req,
        "successor_task_id": successor_task,
        "predecessor_request_id": predecessor_req,
        "predecessor_task_id": predecessor_task,
        "authority_repo": str(Path(authority_repo).resolve()),
        "files": files,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return {**payload, "canonical_digest": digest}


def test_materializer_emits_canonical_digest(tmp_path):
    authority = tmp_path / "authority"
    authority.mkdir()
    data = materialize_rework_overlay(
        "req-s", "task-s", "req-p", "task-p", authority,
        [("src/a.py", hashlib.sha256(b"content").hexdigest(), b"content")],
    )
    packet = json.loads(data)
    assert packet["successor_request_id"] == "req-s"
    assert packet["predecessor_request_id"] == "req-p"
    assert packet["canonical_digest"] == hashlib.sha256(
        json.dumps({k: packet[k] for k in packet if k != "canonical_digest"}, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def test_materializer_identity_distinct_fails():
    with pytest.raises(ValueError):
        materialize_rework_overlay("same", "tsk", "same", "tsk2", Path("/tmp"), [])
    # Rework keeps the canonical task ID and distinguishes attempts by request.
    packet = json.loads(
        materialize_rework_overlay("r1", "same", "r2", "same", Path("/tmp"), [])
    )
    assert packet["successor_task_id"] == packet["predecessor_task_id"] == "same"


def test_consumer_verifies_identities():
    packet = _sample_packet()
    _verify_rework_overlay_packet(packet, "task-1", "req-abc", "runner", Path("/tmp/authority"))
    with pytest.raises(WorkerToolError):
        _verify_rework_overlay_packet(packet, "task-wrong", "req-abc", "runner", Path("/tmp/authority"))


def test_consumer_fails_closed_on_stale_digest():
    packet = _sample_packet()
    packet["canonical_digest"] = "deadbeef"
    with pytest.raises(WorkerToolError):
        _verify_rework_overlay_packet(packet, "task-1", "req-abc", "runner", Path("/tmp/authority"))


def test_consumer_fails_closed_on_foreign_identity():
    packet = _sample_packet()
    with pytest.raises(WorkerToolError):
        _verify_rework_overlay_packet(packet, "task-1", "req-abc", "runner", Path("/other/repo"))


def test_overlay_map_and_delete():
    packet = _sample_packet()
    overlay = _build_rework_overlay_map(packet)
    assert overlay["src/mod.py"]["sha256"] == hashlib.sha256(b"# overlay\n").hexdigest()
    assert overlay["deleted.py"]["deleted"] is True


def test_apply_overlay_returns_overlay_content():
    packet = _sample_packet()
    ctx = WorkerToolContext(
        task_id="task-1", runner="runner", topic="t", request_id="req-abc",
        repo=Path("/tmp/repo"), authority_repo=Path("/tmp/authority"),
        source_graph_targets=(), session_topic="t",
        audit_ledger_path=None, audit_hmac_key_path=None,
        rework_overlay_packet=packet,
    )
    result = _apply_rework_overlay_query(ctx, "body", "mod", "src/mod.py", 64)
    assert result is not None
    assert result.get("overlay") is True
    assert "# overlay" in result.get("source_preview", "")


def test_apply_overlay_returns_deleted():
    packet = _sample_packet()
    ctx = WorkerToolContext(
        task_id="task-1", runner="runner", topic="t", request_id="req-abc",
        repo=Path("/tmp/repo"), authority_repo=Path("/tmp/authority"),
        source_graph_targets=(), session_topic="t",
        audit_ledger_path=None, audit_hmac_key_path=None,
        rework_overlay_packet=packet,
    )
    result = _apply_rework_overlay_query(ctx, "file", "deleted", "deleted.py", 64)
    assert result is not None
    assert result.get("ok") is False
    assert "deleted" in result.get("reason", "")


def test_canonical_fallback_unshadowed():
    packet = _sample_packet()
    ctx = WorkerToolContext(
        task_id="task-1", runner="runner", topic="t", request_id="req-abc",
        repo=Path("/tmp/repo"), authority_repo=Path("/tmp/authority"),
        source_graph_targets=(), session_topic="t",
        audit_ledger_path=None, audit_hmac_key_path=None,
        rework_overlay_packet=packet,
    )
    result = _apply_rework_overlay_query(ctx, "focus", "unknown", "not_in_overlay.py", 64)
    assert result is None


def test_threaded_isolation_no_cross_contamination(tmp_path):
    auth = tmp_path / "auth"
    auth.mkdir()
    packet_a = _sample_packet(successor_req="ra", successor_task="ta",
                              predecessor_req="pa", predecessor_task="pta",
                              authority_repo=auth, files=[{"path": "a.py", "sha256": hashlib.sha256(b"A").hexdigest(),
                                                           "content_base64": base64.b64encode(b"A").decode()}])
    packet_b = _sample_packet(successor_req="rb", successor_task="tb",
                              predecessor_req="pb", predecessor_task="ptb",
                              authority_repo=auth, files=[{"path": "b.py", "sha256": hashlib.sha256(b"B").hexdigest(),
                                                           "content_base64": base64.b64encode(b"B").decode()}])
    results = []
    def query_ctx(packet, target):
        ctx = WorkerToolContext(
            task_id=packet["successor_task_id"], runner="r", topic="t",
            request_id=packet["successor_request_id"],
            repo=Path("/tmp/repo"), authority_repo=auth,
            source_graph_targets=(), session_topic="t",
            audit_ledger_path=None, audit_hmac_key_path=None,
            rework_overlay_packet=packet,
        )
        res = _apply_rework_overlay_query(ctx, "body", "x", target, 64)
        results.append(res)
    t1 = threading.Thread(target=query_ctx, args=(packet_a, "a.py"))
    t2 = threading.Thread(target=query_ctx, args=(packet_b, "b.py"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert any(r and r.get("overlay") and "A" in r.get("source_preview", "") for r in results)
    assert any(r and r.get("overlay") and "B" in r.get("source_preview", "") for r in results)


def test_cleanup_does_not_leak(tmp_path):
    """Verify that the overlay map is computed per-call and contexts don't cross-contaminate."""
    auth = tmp_path / "auth"
    auth.mkdir()
    packet = _sample_packet(authority_repo=auth)
    ctx = WorkerToolContext(
        task_id="task-1", runner="r", topic="t", request_id="req-abc",
        repo=Path("/tmp/repo"), authority_repo=auth,
        source_graph_targets=(), session_topic="t",
        audit_ledger_path=None, audit_hmac_key_path=None,
        rework_overlay_packet=packet,
    )
    res1 = _apply_rework_overlay_query(ctx, "body", "x", "src/mod.py", 64)
    assert res1 is not None
    assert res1.get("overlay") is True
    # Create a second context without overlay, ensure it doesn't leak
    ctx2 = WorkerToolContext(
        task_id="task-2", runner="r", topic="t", request_id="req-def",
        repo=Path("/tmp/repo"), authority_repo=auth,
        source_graph_targets=(), session_topic="t",
        audit_ledger_path=None, audit_hmac_key_path=None,
        rework_overlay_packet=None,
    )
    res2 = _apply_rework_overlay_query(ctx2, "body", "x", "src/mod.py", 64)
    assert res2 is None


def test_rework_overlay_is_wired_into_production_source_graph_query(tmp_path):
    authority = _new_repo(tmp_path, "rework_authority")
    workspace = _new_repo(tmp_path, "rework_workspace")
    _write(
        authority / "src" / "mod.py",
        "def canonical_only():\n    return 'old'\n",
    )
    sg.build_index(authority, incremental=False)

    updated = b"def retained_rework_symbol():\n    return 'new'\n"
    target = workspace / "src" / "mod.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(updated)
    packet = json.loads(
        materialize_rework_overlay(
            "successor-request",
            "same-task",
            "predecessor-request",
            "same-task",
            authority,
            [("src/mod.py", hashlib.sha256(updated).hexdigest(), updated)],
        )
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    packet_path = runtime / "rework_overlay.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    _verify_rework_overlay_packet(
        packet, "same-task", "successor-request", "runner", authority
    )
    ctx = WorkerToolContext(
        task_id="same-task",
        runner="runner",
        topic="task_mcp",
        request_id="successor-request",
        repo=workspace,
        authority_repo=authority,
        source_graph_targets=("src/mod.py",),
        allowed_writes=("src/mod.py",),
        session_topic="task_mcp",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        rework_overlay_packet=packet,
        rework_overlay_packet_path=packet_path,
    )

    first = w.source_graph_query(
        ctx,
        mode="body",
        query="retained_rework_symbol",
        target="src/mod.py",
        workflow_stage="rework",
    )
    assert first["ok"] is True
    assert first["hit_count"] >= 1
    assert first["authority_source"] == "rework_overlay"
    assert "retained_rework_symbol" in first["content"]

    # A later worker edit of the packet-bound path invalidates the private
    # overlay cache and is visible on the next query without touching the
    # canonical repository index.
    later = b"def later_rework_symbol():\n    return 'later'\n"
    target.write_bytes(later)
    second = w.source_graph_query(
        ctx,
        mode="body",
        query="later_rework_symbol",
        target="src/mod.py",
        workflow_stage="implementation",
    )
    assert second["ok"] is True
    assert second["hit_count"] >= 1
    assert "later_rework_symbol" in second["content"]

    canonical = sg.body_query(authority, "later_rework_symbol", 16)
    assert canonical["matches"] == []


# ---------------------------------------------------------------------------
# NF149: multicore extraction -- bounded worker selection, ordered
# single-writer merge, and a truthful sequential_fallback receipt when the
# sandbox denies process creation.  Determinism must not depend on whether
# the validation sandbox actually permits ``ProcessPoolExecutor`` to spawn
# real OS processes, so every test below replaces
# ``concurrent.futures.ProcessPoolExecutor`` with an in-process stand-in.
# ---------------------------------------------------------------------------


def test_extraction_worker_count_env_override_is_bounded_by_ceiling_and_candidates(
    monkeypatch,
):
    monkeypatch.setenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "999")
    assert (
        sg._source_graph_extract_workers(50, sg.MIN_PARALLEL_EXTRACTION_BYTES)
        == sg.MAX_SOURCE_GRAPH_EXTRACT_WORKERS
    )

    monkeypatch.setenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "4")
    assert (
        sg._source_graph_extract_workers(50, sg.MIN_PARALLEL_EXTRACTION_BYTES)
        == 4
    )

    # The override can never exceed the candidate count either -- a small
    # incremental refresh must not fan out more workers than files.
    monkeypatch.setenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "4")
    assert sg._source_graph_extract_workers(2, sg.MIN_PARALLEL_EXTRACTION_BYTES) == 2


def test_multicore_extraction_bounded_workers_and_ordered_single_writer_merge(
    tmp_path, monkeypatch,
):
    """Extraction width stays bounded and the SQLite merge preserves
    ``Executor.map``'s submission order even when workers "complete" in a
    scrambled order -- proven without depending on the sandbox's ability to
    spawn real OS processes."""
    repo = _new_repo(tmp_path, "multicore_ordered_merge")
    names = [f"m{i:02d}" for i in range(10)]
    for name in names:
        _write(repo / "src" / f"{name}.py", f"def {name}():\n    return 1\n")

    submitted_order = []

    class _ScrambledCompletionPool:
        def __init__(self, *, max_workers, mp_context=None):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def map(self, fn, iterable, chunksize=1):
            items = list(iterable)
            submitted_order.extend(item[2] for item in items)
            # Compute out of submission order to prove the merge relies on
            # ``Executor.map``'s order guarantee, not completion order.
            computed = {item[2]: fn(item) for item in reversed(items)}
            return [computed[item[2]] for item in items]

    monkeypatch.setenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "4")
    monkeypatch.setattr(sg.concurrent.futures, "ProcessPoolExecutor", _ScrambledCompletionPool)

    write_order = []
    real_write_extraction = sg._write_extraction

    def _recording_write_extraction(conn, extraction, **kwargs):
        write_order.append(extraction.file_path)
        return real_write_extraction(conn, extraction, **kwargs)

    monkeypatch.setattr(sg, "_write_extraction", _recording_write_extraction)

    report = sg.build_index(repo)

    assert report.errors == []
    assert report.extraction_workers == 4
    assert report.extraction_backend == "process_pool"
    assert report.extraction_fallback_reason == ""
    assert len(submitted_order) >= len(names)
    assert write_order == submitted_order
    expected_rel_paths = {f"src/{name}.py" for name in names}
    assert expected_rel_paths.issubset(set(write_order))


def test_multicore_extraction_denied_process_creation_yields_truthful_sequential_fallback(
    tmp_path, monkeypatch,
):
    """A sandbox that denies process creation degrades to a truthful
    sequential extraction, not a false build failure."""
    repo = _new_repo(tmp_path, "multicore_denied_spawn")
    names = [f"d{i:02d}" for i in range(10)]
    for name in names:
        _write(repo / "src" / f"{name}.py", f"def {name}():\n    return 1\n")

    class _DeniedProcessCreationPool:
        def __init__(self, *, max_workers, mp_context=None):
            raise PermissionError("process creation denied by sandbox")

    monkeypatch.setenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "4")
    monkeypatch.setattr(sg.concurrent.futures, "ProcessPoolExecutor", _DeniedProcessCreationPool)

    report = sg.build_index(repo)

    assert report.errors == []
    assert report.extraction_workers == 1
    assert report.extraction_backend == "sequential_fallback"
    assert report.extraction_fallback_reason == "PermissionError"
    assert report.extraction_telemetry.get("selected_workers") == 1
    assert report.extraction_telemetry.get("reason") == "fallback_PermissionError"

    # The fallback still produced a complete, queryable generation -- the
    # denied spawn degraded the extraction backend, not the product's truth.
    for name in names:
        assert sg.body_query(repo, name, 16)["matches"]


# ---------------------------------------------------------------------------
# NF32: constructing a nested ``ProcessPoolExecutor`` while the current process
# is still inside the multiprocessing spawn bootstrap handshake
# (``current_process()._inheriting``) would re-enter bootstrap and deadlock or
# corrupt the pickle protocol.  The production guard must short-circuit to a
# deterministic sequential extraction and publish the exact fallback telemetry.
# ---------------------------------------------------------------------------


def test_spawn_bootstrap_in_progress_prevents_nested_pool_and_yields_truthful_fallback(
    tmp_path, monkeypatch,
):
    """Unsafe spawn bootstrap prevents nested pool construction and publishes
    the exact fallback telemetry while still producing a complete, queryable
    generation."""
    repo = _new_repo(tmp_path, "spawn_bootstrap_blocked")
    names = [f"sb{i:02d}" for i in range(10)]
    for name in names:
        _write(repo / "src" / f"{name}.py", f"def {name}():\n    return 1\n")

    class _InheritingProcess:
        _inheriting = True

    class _PoolConstructionForbidden:
        def __init__(self, *, max_workers, mp_context=None):
            raise AssertionError(
                "nested pool construction must be prevented during spawn bootstrap"
            )

    monkeypatch.setenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "4")
    monkeypatch.setattr(sg.multiprocessing, "current_process", _InheritingProcess)
    monkeypatch.setattr(
        sg.concurrent.futures, "ProcessPoolExecutor", _PoolConstructionForbidden
    )

    report = sg.build_index(repo)

    assert report.errors == []
    assert report.extraction_workers == 1
    assert report.extraction_backend == "sequential_fallback"
    assert report.extraction_fallback_reason == "spawn_bootstrap_in_progress"
    assert report.extraction_telemetry.get("selected_workers") == 1
    assert (
        report.extraction_telemetry.get("reason")
        == "fallback_spawn_bootstrap_in_progress"
    )

    # The guard degraded the extraction backend, not the product's truth.
    for name in names:
        assert sg.body_query(repo, name, 16)["matches"]


def test_spawn_bootstrap_helper_reports_safe_unsafe_bool_and_exact_fallback_reason(
    tmp_path, monkeypatch,
):
    """``_spawn_bootstrap_in_progress`` is an exact bool (safe=False,
    unsafe=True) and the guard publishes the exact fallback reason string."""

    class _IdleProcess:
        pass

    class _InheritingProcess:
        _inheriting = True

    monkeypatch.setattr(sg.multiprocessing, "current_process", _IdleProcess)
    safe = sg._spawn_bootstrap_in_progress()
    assert isinstance(safe, bool)
    assert safe is False

    monkeypatch.setattr(sg.multiprocessing, "current_process", _InheritingProcess)
    unsafe = sg._spawn_bootstrap_in_progress()
    assert isinstance(unsafe, bool)
    assert unsafe is True

    repo = _new_repo(tmp_path, "spawn_bootstrap_helper_reason")
    names = [f"hr{i:02d}" for i in range(10)]
    for name in names:
        _write(repo / "src" / f"{name}.py", f"def {name}():\n    return 1\n")

    class _PoolConstructionForbidden:
        def __init__(self, *, max_workers, mp_context=None):
            raise AssertionError("pool must not be constructed in the unsafe state")

    monkeypatch.setenv(sg.SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "4")
    monkeypatch.setattr(sg.multiprocessing, "current_process", _InheritingProcess)
    monkeypatch.setattr(
        sg.concurrent.futures, "ProcessPoolExecutor", _PoolConstructionForbidden
    )

    report = sg.build_index(repo)

    assert report.extraction_backend == "sequential_fallback"
    assert report.extraction_fallback_reason == "spawn_bootstrap_in_progress"
    assert (
        report.extraction_telemetry.get("reason")
        == "fallback_spawn_bootstrap_in_progress"
    )


# ---------------------------------------------------------------------------
# NF-2026-00204: ``_index_quality_scorecard`` per-language aggregation must
# never join ``files`` to both ``entities`` and ``edges`` in one statement
# (that Cartesian product is what stalled a 691-file unchanged incremental
# refresh for ~11 CPU-minutes) and the separate pre-aggregated queries it
# replaced that join with must still add up to the same truth under
# high-fanout data and across multiple files sharing one language.
# ---------------------------------------------------------------------------


def test_index_quality_scorecard_never_joins_entities_and_edges_in_one_statement(
    tmp_path,
):
    """Query-plan regression: no captured statement may directly JOIN both
    ``entities`` and ``edges`` to ``files`` -- that shape is exactly the
    Cartesian fan-out this fix eliminated."""
    repo = _new_repo(tmp_path, "quality_no_cartesian_join")
    # A high-fanout file: several entities each with several outgoing edges,
    # so a reintroduced files->entities->edges join would multiply rows.
    lines = [f"def fn{i}():\n    return {i}\n" for i in range(6)]
    lines.append(
        "def caller():\n"
        + "".join(f"    fn{i}()\n" for i in range(6))
        + "    missing_one()\n    missing_two()\n"
    )
    _write(repo / "pkg" / "fanout.py", "".join(lines))
    sg.build_index(repo, incremental=False)

    db_path = sg.resolve_db_path(repo)
    conn = sg.connect(db_path, read_only=True)
    captured_sql: list[str] = []
    conn.set_trace_callback(captured_sql.append)
    try:
        quality = sg._index_quality_scorecard(
            conn, db_path, finished_at="2026-08-14T00:00:00+00:00", previous=None,
        )
    finally:
        conn.set_trace_callback(None)
        conn.close()

    assert captured_sql, "expected the scorecard to execute at least one query"
    for statement in captured_sql:
        normalized = " ".join(statement.split()).lower()
        if "join entities" in normalized and "join edges" in normalized:
            pytest.fail(
                "index quality scorecard rejoined files to both entities and "
                f"edges in one statement: {statement}"
            )

    # Sanity: the high-fanout data was actually measured, not silently
    # skipped by the rewritten aggregation.
    assert quality["by_language"]["python"]["entities"] >= 7
    assert quality["by_language"]["python"]["edges"] >= 8
    assert quality["edges"]["resolved"] >= 6
    assert quality["edges"]["unresolved"] >= 2


def test_index_quality_scorecard_by_language_matches_naive_per_table_counts(
    tmp_path,
):
    """Equivalence regression: the pre-aggregated by-language rollup must
    still equal independently computed, single-table ground truth -- across
    two files that share one language -- after replacing the joined query
    with separate per-table aggregations."""
    repo = _new_repo(tmp_path, "quality_equivalence")
    _write(
        repo / "pkg" / "a.py",
        "def a_one():\n    return 1\n\ndef a_two():\n    return 2\n",
    )
    _write(
        repo / "pkg" / "b.py",
        "def b_caller():\n    a_one()\n    a_two()\n    ghost_call()\n",
    )
    sg.build_index(repo, incremental=False)

    db_path = sg.resolve_db_path(repo)
    conn = sg.connect(db_path, read_only=True)
    try:
        quality = sg._index_quality_scorecard(
            conn, db_path, finished_at="2026-08-14T00:00:00+00:00", previous=None,
        )
        naive_files = int(
            conn.execute(
                "SELECT COUNT(*) FROM files WHERE language='python'"
            ).fetchone()[0]
        )
        naive_entities = int(
            conn.execute(
                "SELECT COUNT(*) FROM entities en JOIN files f "
                "ON f.file_path=en.file_path WHERE f.language='python'"
            ).fetchone()[0]
        )
        naive_edges = int(
            conn.execute(
                "SELECT COUNT(*) FROM edges e JOIN files f "
                "ON f.file_path=e.file_path WHERE f.language='python'"
            ).fetchone()[0]
        )
        naive_resolved = int(
            conn.execute(
                "SELECT COUNT(*) FROM edges e JOIN files f "
                "ON f.file_path=e.file_path WHERE f.language='python' "
                "AND e.dst_qualname IS NOT NULL AND e.dst_qualname != '' "
                "AND EXISTS (SELECT 1 FROM entities d WHERE d.qualname=e.dst_qualname)"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    python_row = quality["by_language"]["python"]
    assert python_row["files"] == naive_files == 2
    assert python_row["entities"] == naive_entities
    assert python_row["edges"] == naive_edges
    assert python_row["resolved_edges"] == naive_resolved


# ---------------------------------------------------------------------------
# NF-2026-00205: hash-authoritative bounded incremental indexing.  size/mtime
# are hints only -- content hashing (not the stat hint) decides changed vs.
# unchanged, delete/rename truth and the single-writer atomic merge are
# preserved, and a true no-op generation reuses its prior quality metrics
# instead of re-running graph-wide SQL.
# ---------------------------------------------------------------------------


def test_same_size_same_mtime_content_mutation_is_reindexed_via_hash(tmp_path):
    """A stat hint alone would call this file unchanged; content hashing
    must catch the mutation and force reindexing anyway."""

    repo = _new_repo(tmp_path, "same_size_same_mtime_mutation")
    target = repo / "pkg" / "a.py"
    _write(target, "def a():\n    return 1\n")
    r1 = sg.build_index(repo, incremental=True)
    assert r1.files_changed == 1
    assert r1.hash_candidates == 0

    new_content = "def a():\n    return 2\n"
    assert len(new_content) == len(target.read_text(encoding="utf-8"))
    target.write_text(new_content, encoding="utf-8")

    # Recreate the exact "same size, same mtime" hint condition without
    # depending on ``os.utime`` (unavailable/unpermitted under some
    # sandboxes and coarse filesystem timestamp resolutions): pin the
    # stored stat hint to the file's *current* on-disk stat directly, so
    # only content hashing -- never the hint -- can catch the mutation.
    current_stat = target.stat()
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        with conn:
            conn.execute(
                "UPDATE files SET file_size=?, mtime_ns=? WHERE file_path='pkg/a.py'",
                (int(current_stat.st_size), int(current_stat.st_mtime_ns)),
            )
    finally:
        conn.close()

    r2 = sg.build_index(repo, incremental=True)
    assert r2.hash_candidates == 1
    assert r2.hash_mismatched == 1
    assert r2.hash_reused == 0
    assert r2.files_changed == 1
    assert r2.files_unchanged == 0

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        row = conn.execute(
            "SELECT source_hash FROM files WHERE file_path='pkg/a.py'"
        ).fetchone()
        assert row["source_hash"] == sgast.sha256_bytes(new_content.encode("utf-8"))
    finally:
        conn.close()


def test_create_delete_rename_are_correct_under_hash_authoritative_reconciliation(
    tmp_path,
):
    repo = _new_repo(tmp_path, "create_delete_rename_hash")
    _write(repo / "pkg" / "keep.py", "def keep():\n    return 1\n")
    _write(repo / "pkg" / "old.py", "def old():\n    return 2\n")
    r1 = sg.build_index(repo, incremental=True)
    assert r1.files_changed == 2

    (repo / "pkg" / "old.py").rename(repo / "pkg" / "renamed.py")
    _write(repo / "pkg" / "new.py", "def brand_new():\n    return 3\n")

    r2 = sg.build_index(repo, incremental=True)
    assert r2.files_removed == 1
    assert r2.files_changed == 2
    assert r2.files_unchanged == 1
    # ``keep.py`` is the only file whose stat hint matched -- it must be
    # reconciled through the hash phase, not skipped for free.
    assert r2.hash_candidates == 1
    assert r2.hash_reused == 1

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        old_context = sg.context(conn, "pkg/old.py")
        assert old_context["found"] is False
        assert old_context["entities"] == []
        stale_rows = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE file_path='pkg/old.py'"
        ).fetchone()[0]
        assert stale_rows == 0
        renamed_matches = sg.func(conn, "old")
        assert len(renamed_matches) == 1
        assert renamed_matches[0]["file_path"] == "pkg/renamed.py"
        assert len(sg.func(conn, "brand_new")) == 1
        assert len(sg.func(conn, "keep")) == 1
    finally:
        conn.close()


def test_hash_phase_fail_closed_on_unstable_read_forces_extraction(
    tmp_path, monkeypatch,
):
    """A file racing a concurrent writer must never be trusted as
    unchanged by the fast hash path: an unstable stat straddle fails
    closed to full re-extraction, which then correctly reconfirms the
    file's true (unchanged) content instead of silently skipping it."""

    repo = _new_repo(tmp_path, "concurrent_mutation")
    target = repo / "pkg" / "a.py"
    _write(target, "def a():\n    return 1\n")
    r1 = sg.build_index(repo, incremental=True)
    assert r1.files_changed == 1

    real_stable_hash = sg._stable_content_hash

    def unstable_for_target(path):
        if path.name == "a.py":
            return None
        return real_stable_hash(path)

    monkeypatch.setattr(sg, "_stable_content_hash", unstable_for_target)

    original_extract = sgast.extract_file
    extracted: list[str] = []

    def counted_extract(repo_root, path, *, build_revision):
        extracted.append(path.relative_to(repo_root).as_posix())
        return original_extract(repo_root, path, build_revision=build_revision)

    monkeypatch.setattr(sgast, "extract_file", counted_extract)

    r2 = sg.build_index(repo, incremental=True)
    assert r2.hash_candidates == 1
    assert r2.hash_unstable == 1
    assert r2.hash_mismatched == 0
    # Fail closed: the unstable read forced a real re-extraction pass...
    assert extracted == ["pkg/a.py"]
    # ...which then correctly reconfirms the content is truly unchanged,
    # rather than fabricating a false "changed" count.
    assert r2.files_changed == 0
    assert r2.files_unchanged == 1


def test_hash_worker_count_env_override_is_bounded_by_ceiling_and_candidates(
    monkeypatch,
):
    monkeypatch.setenv(sg.SOURCE_GRAPH_HASH_WORKERS_ENV, "999")
    assert (
        sg._source_graph_hash_workers(50, sg.MIN_PARALLEL_HASH_BYTES)
        == sg.MAX_SOURCE_GRAPH_HASH_WORKERS
    )

    monkeypatch.setenv(sg.SOURCE_GRAPH_HASH_WORKERS_ENV, "4")
    assert sg._source_graph_hash_workers(50, sg.MIN_PARALLEL_HASH_BYTES) == 4

    # The override can never exceed the candidate count either.
    monkeypatch.setenv(sg.SOURCE_GRAPH_HASH_WORKERS_ENV, "4")
    assert sg._source_graph_hash_workers(2, sg.MIN_PARALLEL_HASH_BYTES) == 2


def test_hash_worker_count_below_threshold_stays_serial(monkeypatch):
    monkeypatch.delenv(sg.SOURCE_GRAPH_HASH_WORKERS_ENV, raising=False)
    assert sg._source_graph_hash_workers(3, sg.MIN_PARALLEL_HASH_BYTES) == 1
    assert sg._source_graph_hash_workers(50, 10) == 1


def test_unchanged_refresh_uses_bounded_parallel_hash_workers(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path, "hash_bounded_parallel")
    for i in range(12):
        _write(
            repo / "pkg" / f"m{i}.py",
            f"def fn_{i}():\n    return {i}\n" * 50,
        )
    r1 = sg.build_index(repo, incremental=False)
    assert r1.files_changed == 12

    monkeypatch.setenv(sg.SOURCE_GRAPH_HASH_WORKERS_ENV, "3")
    r2 = sg.build_index(repo, incremental=True)
    assert r2.hash_candidates == 12
    assert r2.hash_reused == 12
    assert r2.hash_mismatched == 0
    assert r2.hash_unstable == 0
    assert r2.hash_workers == 3
    assert r2.hash_backend == "thread_pool"
    assert r2.hash_telemetry["reason"] == "env_override"
    assert r2.files_changed == 0
    assert r2.files_unchanged == 12


def test_noop_refresh_reuses_quality_metrics_without_graph_wide_recompute(
    tmp_path, monkeypatch,
):
    repo = _new_repo(tmp_path, "quality_reuse_equivalence")
    _write(repo / "pkg" / "a.py", "def a():\n    return 1\n\ndef b():\n    a()\n")
    r1 = sg.build_index(repo, incremental=True)
    assert r1.quality_reused is False

    calls: list[int] = []
    original_scorecard = sg._index_quality_scorecard

    def tracked_scorecard(*args, **kwargs):
        calls.append(1)
        return original_scorecard(*args, **kwargs)

    monkeypatch.setattr(sg, "_index_quality_scorecard", tracked_scorecard)

    r2 = sg.build_index(repo, incremental=True)
    assert r2.files_changed == 0 and r2.files_removed == 0
    assert r2.quality_reused is True
    assert calls == []

    # Metric equivalence: every measured field carries forward unchanged
    # from the last real generation -- only the receipt timestamp advances.
    assert r2.index_quality["edges"] == r1.index_quality["edges"]
    assert r2.index_quality["by_language"] == r1.index_quality["by_language"]
    assert r2.index_quality["artifacts"] == r1.index_quality["artifacts"]
    assert r2.index_quality["finished_at"] == r2.finished_at
    assert r2.index_quality["finished_at"] != r1.index_quality["finished_at"]


def test_live_like_noop_refresh_stays_fast_with_hash_and_quality_reuse(tmp_path):
    """A no-op refresh over a moderately sized index must reach a truthful
    terminal report quickly: the hash phase only reads bytes and the
    quality scorecard is reused rather than recomputed graph-wide."""

    repo = _new_repo(tmp_path, "live_like_noop_latency")
    for i in range(60):
        callees = "".join(f"    fn_{j}_0()\n" for j in range(max(0, i - 1), i))
        _write(
            repo / f"mod_{i}.py",
            f"def fn_{i}_0():\n    return {i}\n"
            f"def fn_{i}_1():\n{callees}    missing_{i}()\n",
        )
    r1 = sg.build_index(repo, incremental=False)
    assert r1.files_changed == 60

    started = time.monotonic()
    r2 = sg.build_index(repo, incremental=True)
    elapsed = time.monotonic() - started

    assert r2.files_changed == 0
    assert r2.files_removed == 0
    assert r2.hash_candidates == 60
    assert r2.hash_reused == 60
    assert r2.quality_reused is True
    assert elapsed < 15.0, f"no-op refresh with hash reconciliation took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Bodygrep allocation-bounded hot path (needfix-NF-2026-00414)
# ---------------------------------------------------------------------------

def test_iter_splitlines_matches_str_splitlines_byte_for_byte():
    """The lazy splitter must reproduce ``str.splitlines()`` exactly.

    Line numbers and match text depend on this being byte-for-byte identical,
    including the ``\\r\\n`` single-boundary rule and the CPython-only single
    code-point boundaries (``\\x1c``..``\\x85`` and the Unicode line separators).
    """
    corpus = [
        "",
        "a",
        "a\n",
        "\n",
        "abc",
        "a\nb",
        "a\rb",
        "a\r\nb",
        "a\r\n",
        "\r\n",
        "a\vb\fc",
        "a\x1cb",
        "a\x1db",
        "a\x1eb",
        "a\x85b",
        "a\u2028b",
        "a\u2029b",
        "line1\nline2\nline3\n",
        "trailing\n",
        "\n\n\n",
        "x\r\ny\r\nz",
        "no newline at end",
        "mixed\r\nwindows\nunix\rmac",
    ]
    for text in corpus:
        assert list(sg._iter_splitlines(text)) == text.splitlines(), repr(text)

    boundaries = ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
    for left in boundaries:
        for right in boundaries:
            for prefix in ("", "x", "xy"):
                for suffix in ("", "z", "zz"):
                    text = prefix + left + "mid" + right + suffix
                    assert list(sg._iter_splitlines(text)) == text.splitlines(), repr(text)


def test_iter_splitlines_chunked_matches_str_splitlines_across_edges(monkeypatch):
    """Chunked C-level splitting preserves every boundary, including CRLF.

    The splitter chunks text and hands each chunk to ``str.splitlines``; a bare
    ``\r`` at a chunk edge must re-join the next chunk's ``\n`` into one
    ``\r\n`` boundary.  Tiny chunk sizes force boundaries onto chunk edges at
    many offsets while byte-for-byte parity with ``str.splitlines()`` holds.
    """
    import random

    boundaries = [
        "\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e",
        "\x85", "\u2028", "\u2029",
    ]
    atoms = ["", "a", "ab", "x", "yy", "\r", "\n", "\r\n", *boundaries]
    rng = random.Random(20260824)
    for chunk in (1, 2, 3, 4, 5, 7):
        monkeypatch.setattr(sg, "_BODYGREP_SPLIT_CHUNK_CHARS", chunk)
        for _ in range(2000):
            text = "".join(rng.choice(atoms) for _ in range(rng.randint(0, 40)))
            assert list(sg._iter_splitlines(text)) == text.splitlines(), repr(text)
    # A single very long line is sliced once, not rebuilt incrementally.
    long_line = "z" * 100_000 + "\n"
    assert list(sg._iter_splitlines(long_line)) == long_line.splitlines()


def test_iter_splitlines_boundary_set_property_tested_across_code_points():
    """The boundary set must be discovered from str.splitlines(), not mirrored.

    Every ASCII control code point plus the Unicode line/paragraph separators
    is fed through ``str.splitlines()`` and ``_iter_splitlines``; the lazy
    splitter must agree byte-for-byte on each, so the hard-coded boundary tuple
    can never drift from the C-level truth.  A deterministic random mix over a
    wider code-point alphabet then exercises multi-boundary and CRLF-adjacent
    combinations without hard-coding the boundary tuple again.
    """
    import random

    boundary_cps = list(range(0x00, 0x20)) + [0x7F, 0x85, 0x2028, 0x2029]
    for cp in boundary_cps:
        ch = chr(cp)
        for text in (ch, "a" + ch, ch + "b", "a" + ch + "b", ch + ch + "a"):
            assert list(sg._iter_splitlines(text)) == text.splitlines(), repr(text)

    rng = random.Random(20260824)
    alphabet = [chr(cp) for cp in boundary_cps] + list("abXY019 \t")
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        assert list(sg._iter_splitlines(text)) == text.splitlines(), repr(text)


def test_bodygrep_bytes_proves_no_match_is_sound():
    """The pre-decode reject must only fire when absence is provable."""
    # ASCII needle + ASCII bytes: exact case-insensitive byte search.
    assert sg._bodygrep_bytes_proves_no_match("needle", b"no match here") is True
    assert sg._bodygrep_bytes_proves_no_match("needle", b"the NEEDLE is here") is False
    # Non-ASCII bytes can casefold-expand, so absence is never provable.
    assert sg._bodygrep_bytes_proves_no_match("needle", "caf\u00e9".encode("utf-8")) is False
    # A casefolded needle that is ASCII but whose source text is non-ASCII is
    # not provable either (Kelvin sign U+212A casefolds to 'k').
    assert sg._bodygrep_bytes_proves_no_match("k", "300 \u212a".encode("utf-8")) is False


def test_bodygrep_large_no_match_file_rejected_before_decode_and_split(tmp_path, monkeypatch):
    """Large ASCII no-match bytes must be rejected before decode/split."""
    repo = _new_repo(tmp_path, "bodygrep_no_match_reject")
    big = repo / "big.md"
    big.write_text("padding line\n" * 120_000 + "still absent here\n", encoding="utf-8")
    _write(repo / "hit.md", "the needle appears here\n")
    sg.build_index(repo, incremental=False)

    seen = []
    real_iter = sg._iter_splitlines

    def tracking_iter(text):
        seen.append(len(text))
        yield from real_iter(text)

    monkeypatch.setattr(sg, "_iter_splitlines", tracking_iter)

    result = sg.bodygrep_query(repo, "needle", budget=16)

    assert [m["file_path"] for m in result["matches"]] == ["hit.md"]
    big_len = len(big.read_text(encoding="utf-8"))
    hit_len = len((repo / "hit.md").read_text(encoding="utf-8"))
    # The large no-match file never reached the decode/split path.
    assert big_len not in seen
    assert seen == [hit_len]


def test_bodygrep_unicode_casefold_fallback_decodes_when_bytes_cannot_prove_absence(
    tmp_path, monkeypatch,
):
    """Non-ASCII candidates must fall through to decode for exact casefold."""
    repo = _new_repo(tmp_path, "bodygrep_unicode_fallback")
    _write(repo / "u.md", "Die Stra\u00dfe ist lang\nplain\n")
    sg.build_index(repo, incremental=False)

    decoded = []
    real_iter = sg._iter_splitlines

    def tracking_iter(text):
        decoded.append(text)
        yield from real_iter(text)

    monkeypatch.setattr(sg, "_iter_splitlines", tracking_iter)

    assert sg.bodygrep_query(repo, "stra\u00dfe", budget=16)["matches"]
    assert sg.bodygrep_query(repo, "STRASSE", budget=16)["matches"]
    assert decoded, "Unicode-ambiguous candidates must reach the decode path"
    assert any("Stra\u00dfe" in text for text in decoded)


def test_bodygrep_unicode_casefold_equivalence_preserved(tmp_path):
    """Byte filtering must not weaken Unicode casefold equivalence."""
    repo = _new_repo(tmp_path, "bodygrep_unicode_casefold")
    # Kelvin sign U+212A casefolds to 'k'; sharp-s U+00DF casefolds to 'ss'.
    _write(repo / "k.md", "temperature 300 \u212a\n")
    _write(repo / "s.md", "Die Stra\u00dfe ist lang\n")
    sg.build_index(repo, incremental=False)

    assert [m["file_path"] for m in sg.bodygrep_query(repo, "k", budget=16)["matches"]] == ["k.md"]
    assert [m["file_path"] for m in sg.bodygrep_query(repo, "stra\u00dfe", budget=16)["matches"]] == ["s.md"]
    assert [m["file_path"] for m in sg.bodygrep_query(repo, "STRASSE", budget=16)["matches"]] == ["s.md"]


def test_bodygrep_match_output_byte_for_byte_parity(tmp_path):
    """Match rows must stay identical to the replaced list-based splitter."""
    repo = _new_repo(tmp_path, "bodygrep_parity")
    text = (
        "def find_needle():\n    return 'needle'\n\n"
        "another needle here\nlast line no match\n"
    )
    _write(repo / "code.py", text)
    sg.build_index(repo, incremental=False)

    result = sg.bodygrep_query(repo, "needle", budget=64)

    expected = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if "needle" in line.casefold():
            expected.append({
                "file_path": "code.py",
                "kind": "body_match",
                "name": "needle",
                "qualname": f"code.py:{line_number}",
                "line_start": line_number,
                "line_end": line_number,
                "signature": line.strip()[:320],
                "evidence_label": "EXTRACTED",
                "confidence": 1.0,
            })
    assert result["matches"] == expected


def test_bodygrep_invalid_utf8_file_is_skipped_but_counted(tmp_path):
    """Invalid UTF-8 still decodes-then-skips and is counted in files_scanned."""
    repo = _new_repo(tmp_path, "bodygrep_invalid_utf8")
    _write(repo / "good.md", "needle present\n")
    (repo / "bad.md").write_bytes(b"needle here\nbad \xff\xfe bytes\n")
    sg.build_index(repo, incremental=False)

    result = sg.bodygrep_query(repo, "needle", budget=16)

    assert [m["file_path"] for m in result["matches"]] == ["good.md"]
    assert result["files_scanned"] == 2


def test_bodygrep_oversized_file_cursor_advances_forward(tmp_path):
    """A file larger than the byte cap must still let the cursor move on."""
    repo = _new_repo(tmp_path, "bodygrep_oversized_cursor")
    (repo / "000-oversized.md").write_text("x" * (1024 * 1024 + 128), encoding="utf-8")
    _write(repo / "001-hit.md", "the needle is here\n")
    sg.build_index(repo, incremental=False)

    first = sg.bodygrep_query(repo, "needle", budget=2)
    assert first["files_scanned"] == 1
    assert first["scan_truncated"] is True
    assert first["matches"] == []
    assert first["next_cursor"]

    second = sg.bodygrep_query(repo, "needle", budget=2, cursor=first["next_cursor"])
    assert [m["file_path"] for m in second["matches"]] == ["001-hit.md"]


def test_bodygrep_cursor_rejects_unicode_digit_line_fields(tmp_path):
    """A cursor line field that isdigit()s but is not ASCII-decimal is invalid.

    ``str.isdigit()`` accepts superscript/circled digits (U+00B2, U+2464) that
    ``int()`` cannot convert; the decoder must reject them as invalid_cursor
    instead of leaking an uncaught ValueError.
    """
    repo = _new_repo(tmp_path, "bodygrep_cursor_unicode_digit")
    _write(repo / "a.md", "needle one\nneedle two\nneedle three\n")
    sg.build_index(repo, incremental=False)

    first = sg.bodygrep_query(repo, "needle", budget=1)
    assert first["next_cursor"]
    af_hex, _after_line_text, digest = first["next_cursor"].split("-")
    for bad in ("\u00b2", "\u2464", "\u00b3"):
        forged = f"{af_hex}-{bad}-{digest}"
        with pytest.raises(sg.SourceGraphError) as excinfo:
            sg.bodygrep_query(repo, "needle", budget=1, cursor=forged)
        assert str(excinfo.value) == "invalid_cursor"


def test_bodygrep_cursor_rejects_overlong_digit_line_field(tmp_path):
    """A line field longer than Python's int digit limit must be invalid.

    ``int()`` defaults to a 4300-digit cap on decimal strings; a forged cursor
    whose line field exceeds that must return ``invalid_cursor`` instead of
    leaking an uncaught ValueError.  The digest stays valid so the length guard
    is the only thing that rejects it.
    """
    repo = _new_repo(tmp_path, "bodygrep_cursor_overlong_digit")
    _write(repo / "a.md", "needle one\nneedle two\nneedle three\n")
    sg.build_index(repo, incremental=False)

    first = sg.bodygrep_query(repo, "needle", budget=1)
    assert first["next_cursor"]
    af_hex, _after_line_text, digest = first["next_cursor"].split("-")
    forged = f"{af_hex}-{'1' * 5000}-{digest}"
    with pytest.raises(sg.SourceGraphError) as excinfo:
        sg.bodygrep_query(repo, "needle", budget=1, cursor=forged)
    assert str(excinfo.value) == "invalid_cursor"


def test_bodygrep_hot_path_splitter_is_lazy_generator():
    """The hot loop streams lines lazily instead of materializing a list.

    The allocation shape is asserted behaviorally: the splitter must be a
    generator (so a full splitlines list is never held alongside the decoded
    text) and reproduce ``str.splitlines`` exactly.  Peak-memory shape is
    enforced separately by the multi-file and subprocess measurements.
    """
    gen = sg._iter_splitlines("a\nb\nc")
    assert inspect.isgenerator(gen)
    assert next(gen) == "a"
    assert list(gen) == ["b", "c"]


def test_bodygrep_repeated_calls_have_bounded_live_objects_subprocess(tmp_path):
    """Peak live objects stay bounded across repeated no-match scans.

    Runs in a subprocess so a memory regression cannot corrupt the pytest
    process.  tracemalloc (deterministic and platform-independent) is the
    primary signal; the candidate bytes may be held once, but a decoded string
    plus a splitlines list would push peak well past the tolerant bound.
    """
    repo = _new_repo(tmp_path, "bodygrep_memory_subprocess")
    big = repo / "big.md"
    big.write_text("padding line\n" * 200_000, encoding="utf-8")
    sg.build_index(repo, incremental=False)

    script = """
import sys
from pathlib import Path
import aiworkhub.source_graph as sg

root = Path(sys.argv[1])
size = (root / "big.md").stat().st_size
peak = -1
try:
    import tracemalloc
except ImportError:
    tracemalloc = None

if tracemalloc is not None:
    tracemalloc.start()
try:
    # The scan itself must always run: execution errors (including these
    # assertions) must fail the subprocess, not be swallowed by the import
    # guard above.
    for _ in range(5):
        result = sg.bodygrep_query(root, "needle", budget=16)
        assert result["matches"] == []
        assert result["files_scanned"] == 1
finally:
    if tracemalloc is not None:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
rss = -1
if sys.platform.startswith("linux"):
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        rss = -1
print(f"{size} {peak} {rss}")
"""
    src_dir = str(Path(sg.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", script, str(repo)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    size, peak, rss = (int(part) for part in proc.stdout.split())
    assert size > 1_000_000, "fixture must be meaningfully large"
    if peak >= 0:
        # Tolerant: one copy of the candidate bytes is allowed; adding a full
        # decoded string + splitlines list would exceed this by a wide margin.
        assert peak < size * 3, f"peak traced memory {peak} exceeds {size * 3}"
    if rss > 0:
        # ru_maxrss is KiB on Linux; a loose ceiling only catches catastrophic
        # regressions and is skipped on platforms without a KiB RSS figure.
        assert rss < 256 * 1024, f"peak RSS {rss} KiB unexpectedly large"


def test_bodygrep_multi_file_no_match_peak_keeps_single_raw_buffer(tmp_path):
    """The no-match byte filter must be copy-free and hold one raw buffer.

    Each file is rejected by the byte filter before decode; the prior
    candidate's raw bytes must be released before the next file is read, and
    the case-insensitive byte scan must not materialize a full-size lowercase
    copy of ``raw``.  A full-size lowered copy (or two simultaneously-live raw
    buffers) would push peak to ~2x one file, which this bound rejects.
    """
    import tracemalloc

    repo = _new_repo(tmp_path, "bodygrep_multi_file_peak")
    line = "padding line\n"
    per_file = 2 * 1024 * 1024
    for name in ("000-a.md", "001-b.md"):
        _write(repo / name, line * (per_file // len(line)))
    sg.build_index(repo, incremental=False)
    size = (repo / "000-a.md").stat().st_size

    tracemalloc.start()
    try:
        result = sg.bodygrep_query(repo, "zzzabsentneedle", budget=32)
    finally:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    assert result["matches"] == []
    assert result["files_scanned"] == 2
    assert size > 1_000_000, "fixture must be meaningfully large"
    assert peak < size * 1.5, f"peak traced memory {peak} exceeds {size * 1.5}"


def test_bodygrep_multi_file_matching_path_peak_releases_previous_text(tmp_path):
    """A matching file's decoded text must not outlive the next candidate.

    The matching path decodes each file; the previous file's full decoded
    string must be released before the next candidate's raw bytes (and then its
    decoded text) are allocated.  Holding the previous text alongside the next
    decode would push peak to ~3x one file, which this bound rejects.
    """
    import tracemalloc

    repo = _new_repo(tmp_path, "bodygrep_multi_file_match_peak")
    padding = "filler row with no match here\n"
    per_file = 2 * 1024 * 1024
    # Both files match exactly once, so both decode and the scan crosses from
    # the first decoded text into the second candidate's read/decode.
    body = "needle once here\n" + padding * (per_file // len(padding))
    for name in ("000-a.md", "001-b.md"):
        _write(repo / name, body)
    sg.build_index(repo, incremental=False)
    size = (repo / "000-a.md").stat().st_size

    tracemalloc.start()
    try:
        result = sg.bodygrep_query(repo, "needle", budget=32)
    finally:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    assert [m["file_path"] for m in result["matches"]] == ["000-a.md", "001-b.md"]
    assert result["files_scanned"] == 2
    assert size > 1_000_000, "fixture must be meaningfully large"
    assert peak < size * 2.5, f"peak traced memory {peak} exceeds {size * 2.5}"
