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


# ---------------------------------------------------------------------------
# B881: truthful bounded JS/TS family file-level evidence (no fabrication)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "suffix,expected_language",
    [
        (".js", "javascript"), (".jsx", "javascript"), (".mjs", "javascript"),
        (".cjs", "javascript"), (".ts", "typescript"), (".tsx", "typescript"),
    ],
)
def test_js_ts_family_gets_file_level_evidence_not_fail_closed(tmp_path, suffix, expected_language):
    repo = _new_repo(tmp_path, "repo")
    target = repo / "pkg" / f"widget{suffix}"
    _write(target, "export function widget() { return 1; }\n")
    extraction = sgast.extract_file(repo, target, build_revision="test-rev")
    assert extraction.status == "file_evidence_only"
    assert extraction.language == expected_language
    assert len(extraction.source_hash) == 64
    assert len(extraction.entities) == 1
    entity = extraction.entities[0]
    assert entity.kind == "file"
    assert entity.evidence_label == sgast.FILE_EVIDENCE
    assert entity.file_path == f"pkg/widget{suffix}"
    assert entity.signature == "bytes=39"
    # No function/call/import/class is invented from JS/TS source text.
    assert extraction.edges == ()


def test_former_empty_result_regression_js_target_now_produces_non_empty_slice(tmp_path):
    """B880 regression: a JS/TS target must never come back empty merely
    because Python AST extraction was the only semantic extractor wired in."""

    repo = _new_repo(tmp_path, "repo")
    _write(repo / "extension" / "extension.js", "module.exports = function activate() {};\n")
    report = sg.build_index(repo, incremental=True)
    assert report.errors == []
    assert report.entities_written == 1

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        matches = sg.find(conn, "extension.js")
        assert matches
        assert matches[0]["file_path"] == "extension/extension.js"
        assert matches[0]["evidence_label"] == sgast.FILE_EVIDENCE
    finally:
        conn.close()

    payload = sg.slice_(repo, "extension.js", budget=10)
    assert payload["matches"], "target slice must be non-empty for a real JS file"
    assert payload["matches"][0]["file_path"] == "extension/extension.js"

    bundle_payload = sg.bundle(repo, "refactor", "extension.js", max_lines=10)
    assert bundle_payload["sections"]
    assert bundle_payload["sections"][0]["file"]["language"] == "javascript"
    assert bundle_payload["sections"][0]["file"]["status"] == "file_evidence_only"
    # No fabricated semantic entities/edges beyond the one file-evidence row.
    assert bundle_payload["sections"][0]["edges"] == []
    assert len(bundle_payload["sections"][0]["entities"]) == 1
    assert bundle_payload["sections"][0]["entities"][0]["kind"] == "file"


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
        assert match["evidence_label"] == sgast.FILE_EVIDENCE


def test_python_ast_extraction_unchanged_alongside_js_ts_family(tmp_path):
    """Preserve the Python semantic graph exactly when JS/TS files coexist
    in the same repository as the file-level (non-semantic) evidence."""

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
        assert ts_context["entities"][0]["kind"] == "file"
    finally:
        conn.close()


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
