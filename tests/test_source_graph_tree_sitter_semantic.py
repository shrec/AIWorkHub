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


def test_js_ts_call_source_col_is_utf8_byte_offset_of_called_identifier(tmp_path: Path) -> None:
    source = (
        'import { helper as h } from "./util";\n'
        "function helper() { return 0; }\n"
        "export function run(obj: { method: () => number }) {\n"
        '  const prefix = "é"; obj.method(); obj.method(); h(); helper();\n'
        "  return new Widget();\n"
        "}\n"
    )
    target = tmp_path / "app" / "main.ts"
    _write(target, source)
    raw = semantic.extract_javascript_typescript(
        file_path="app/main.ts", raw=source.encode("utf-8"), language="typescript",
    )
    assert raw is not None
    call_rows = [row for row in raw.edges if row["kind"] == "calls"]
    call_line = source.splitlines()[3]
    encoded = call_line.encode("utf-8")
    first_method = encoded.find(b"method")
    second_method = encoded.find(b"method", first_method + 1)
    alias_col = encoded.find(b"h()")
    shadow_col = encoded.find(b"helper")
    assert first_method != call_line.find("method")
    method_cols = sorted(
        int(row["source_col"])
        for row in call_rows
        if row["dst_name"] == "method" and int(row["line"]) == 4
    )
    assert method_cols == [first_method, second_method]
    assert encoded.find(b"obj") not in method_cols
    assert encoded.find(b"(") not in method_cols
    alias_row = next(
        row for row in call_rows
        if row["dst_name"] == "helper" and int(row["source_col"]) == alias_col
    )
    shadow_row = next(
        row for row in call_rows
        if row["dst_name"] == "helper" and int(row["source_col"]) == shadow_col
    )
    assert int(alias_row["line"]) == 4
    assert int(shadow_row["line"]) == 4
    assert int(alias_row["source_col"]) != int(shadow_row["source_col"])
    widget_line = source.splitlines()[4]
    widget_col = widget_line.encode("utf-8").find(b"Widget")
    new_row = next(row for row in call_rows if row["dst_name"] == "Widget")
    assert int(new_row["line"]) == 5
    assert int(new_row["source_col"]) == widget_col
    assert widget_col != widget_line.encode("utf-8").find(b"new")
    projected = sgast.extract_file(tmp_path, target, build_revision="coord-test")
    projected_calls = [edge for edge in projected.edges if edge.kind == "calls"]
    assert {(edge.dst_name, edge.line, edge.source_col) for edge in projected_calls} == {
        (row["dst_name"], int(row["line"]), int(row["source_col"])) for row in call_rows
    }
    task_store.initialize_repository(tmp_path)
    _write(tmp_path / "app" / "util.ts", "export function helper() { return 1; }\n")
    report = sg.build_index(tmp_path, incremental=False)
    assert report.errors == []
    conn = sg.connect(sg.resolve_db_path(tmp_path))
    try:
        persisted = conn.execute(
            "SELECT dst_name, line, source_col FROM edges "
            "WHERE file_path='app/main.ts' AND kind='calls' "
            "ORDER BY line, source_col, dst_name"
        ).fetchall()
    finally:
        conn.close()
    expected = sorted(
        (int(row["line"]), int(row["source_col"]), row["dst_name"]) for row in call_rows
    )
    assert [(row["line"], row["source_col"], row["dst_name"]) for row in persisted] == expected


def test_js_ts_parser_fallback_leaves_call_source_col_unknown(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(semantic, "extract_javascript_typescript", lambda **kwargs: None)
    target = tmp_path / "src" / "widget.js"
    _write(target, "export function run() { return helper(); }\n")
    extraction = sgast.extract_file(tmp_path, target, build_revision="fallback-col")
    assert extraction.status == "ok"
    assert all(edge.source_col == -1 for edge in extraction.edges)
