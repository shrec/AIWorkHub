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
import threading
from pathlib import Path

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
        files = [{"path": "src/mod.py", "sha256": "abc123", "content_base64": base64.b64encode(b"# overlay\n").decode()},
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
        [("src/a.py", "sha1", b"content")],
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
    with pytest.raises(ValueError):
        materialize_rework_overlay("r1", "same", "r2", "same", Path("/tmp"), [])


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
    assert overlay["src/mod.py"]["sha256"] == "abc123"
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
                              authority_repo=auth, files=[{"path": "a.py", "sha256": "shaA",
                                                           "content_base64": base64.b64encode(b"A").decode()}])
    packet_b = _sample_packet(successor_req="rb", successor_task="tb",
                              predecessor_req="pb", predecessor_task="ptb",
                              authority_repo=auth, files=[{"path": "b.py", "sha256": "shaB",
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
