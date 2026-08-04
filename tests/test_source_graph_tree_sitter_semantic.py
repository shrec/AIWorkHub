from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import source_graph_ast as sgast
from aiworkhub import source_graph_semantic as semantic
from aiworkhub import task_store


pytestmark = pytest.mark.skipif(
    not semantic.parser_capability("typescript")["available"],
    reason="tree-sitter-language-pack optional extra is not installed",
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_typescript_parser_extracts_exact_alias_inheritance_and_calls(tmp_path: Path) -> None:
    target = tmp_path / "src" / "widget.ts"
    _write(
        target,
        'import { helper as h } from "./util";\n'
        '// class Fake { ghost() { fakeCall(); } }\n'
        'const sample = "function fakeString() { fakeCall(); }";\n'
        "export interface Shape extends Base { area(): number; }\n"
        "export class Widget extends Parent implements Shape {\n"
        "  run(value: number) { return h(value); }\n"
        "}\n",
    )

    extraction = sgast.extract_file(tmp_path, target, build_revision="semantic-test")

    assert extraction.status == "ok"
    assert {entity.extractor for entity in extraction.entities} == {
        sgast.TREE_SITTER_JS_TS_EXTRACTOR_ID
    }
    names = {(entity.kind, entity.name) for entity in extraction.entities}
    assert {("class", "Shape"), ("class", "Widget"), ("method", "run")} <= names
    assert not any(entity.name in {"Fake", "ghost", "fakeString"} for entity in extraction.entities)
    assert any(edge.kind == "imports" and edge.dst_name == "./util" for edge in extraction.edges)
    assert any(edge.kind == "inherits" and edge.dst_name == "Parent" for edge in extraction.edges)
    assert any(edge.kind == "inherits" and edge.dst_name == "Shape" for edge in extraction.edges)
    alias_call = next(edge for edge in extraction.edges if edge.kind == "calls")
    assert alias_call.dst_name == "helper"
    assert alias_call.evidence_label == sgast.EXTRACTED


def test_typescript_import_disambiguates_duplicate_cross_file_target(tmp_path: Path) -> None:
    task_store.initialize_repository(tmp_path)
    _write(tmp_path / "a" / "math.ts", "export function helper() { return 1; }\n")
    _write(tmp_path / "b" / "math.ts", "export function helper() { return 2; }\n")
    _write(
        tmp_path / "app" / "main.ts",
        'import { helper } from "../b/math";\n'
        "export function run() { return helper(); }\n",
    )

    report = sg.build_index(tmp_path, incremental=False)
    assert report.errors == []
    conn = sg.connect(sg.resolve_db_path(tmp_path))
    try:
        edge = conn.execute(
            "SELECT dst_qualname, extractor FROM edges "
            "WHERE file_path='app/main.ts' AND kind='calls' AND dst_name='helper'"
        ).fetchone()
    finally:
        conn.close()

    assert edge is not None
    assert edge["extractor"] == sgast.TREE_SITTER_JS_TS_EXTRACTOR_ID
    assert edge["dst_qualname"].startswith("b/math.ts::helper")


def test_language_registry_reports_active_parser_backend() -> None:
    from aiworkhub import source_graph_languages as languages

    rows = {row["id"]: row for row in languages.public_registry()}
    assert rows["typescript"]["active_capability"] == "semantic_tree_sitter"
    assert rows["typescript"]["semantic_parser"]["available"] is True
    assert rows["python"]["active_capability"] == "semantic_ast"


def test_incremental_build_reindexes_when_optional_extractor_becomes_available(
    tmp_path: Path, monkeypatch,
) -> None:
    task_store.initialize_repository(tmp_path)
    target = tmp_path / "src" / "widget.ts"
    _write(target, "export function widget() { return 1; }\n")
    real_extract = semantic.extract_javascript_typescript
    monkeypatch.setattr(semantic, "extract_javascript_typescript", lambda **kwargs: None)

    lexical = sg.build_index(tmp_path, incremental=True)
    assert lexical.files_changed >= 1
    conn = sg.connect(sg.resolve_db_path(tmp_path))
    try:
        lexical_extractors = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT extractor FROM entities WHERE file_path='src/widget.ts'"
            )
        }
    finally:
        conn.close()
    assert lexical_extractors == {sgast.POLYGLOT_LEXICAL_EXTRACTOR_ID}

    monkeypatch.setattr(semantic, "extract_javascript_typescript", real_extract)
    upgraded = sg.build_index(tmp_path, incremental=True)
    assert upgraded.files_changed == 1
    conn = sg.connect(sg.resolve_db_path(tmp_path))
    try:
        upgraded_extractors = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT extractor FROM entities WHERE file_path='src/widget.ts'"
            )
        }
    finally:
        conn.close()
    assert upgraded_extractors == {sgast.TREE_SITTER_JS_TS_EXTRACTOR_ID}


def test_large_javascript_tree_lifetime_is_stable(tmp_path: Path) -> None:
    target = tmp_path / "extension.js"
    functions = "\n".join(
        f"export function handler{i}(value) {{ return helper(value, {i}); }}"
        for i in range(2500)
    )
    _write(target, functions + "\n")

    extraction = sgast.extract_file(tmp_path, target, build_revision="large-tree-test")

    assert extraction.status == "ok"
    assert sum(entity.kind == "function" for entity in extraction.entities) == 2500
    assert sum(edge.kind == "calls" for edge in extraction.edges) == 2500
