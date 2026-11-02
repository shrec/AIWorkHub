"""JSX component usage (<Header />) is a ``calls`` edge, so calls/impact see render sites."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import source_graph_ast as sgast
from aiworkhub import source_graph_semantic as semantic
from aiworkhub import task_store


pytestmark = pytest.mark.skipif(
    not semantic.parser_capability("javascript")["available"],
    reason="tree-sitter-language-pack optional extra is not installed",
)


def _calls(file_path: str, source: str, language: str) -> list[dict[str, object]]:
    extracted = semantic.extract_javascript_typescript(
        file_path=file_path, raw=source.encode("utf-8"), language=language,
    )
    assert extracted is not None
    assert extracted.has_parse_error is False
    return [row for row in extracted.edges if row["kind"] == "calls"]


def test_js_component_usage_binds_local_component_and_skips_intrinsics() -> None:
    source = (
        "const Header = () => <h1>x</h1>;\n"
        "export default function App() {\n"
        '  return <div><Header title="a" /><Header>b</Header></div>;\n'
        "}\n"
    )
    calls = _calls("app.js", source, "javascript")
    assert len(calls) == 2
    assert all(row["src_qualname"] == "app.js::App" for row in calls)
    assert all(row["dst_name"] == "Header" for row in calls)
    assert all(row["dst_qualname"] == "app.js::Header" for row in calls)
    assert all(row["evidence_label"] == sgast.EXTRACTED for row in calls)
    line = source.splitlines()[2].encode("utf-8")
    first = line.find(b"Header")
    second = line.find(b"Header", first + 1)
    assert sorted(int(row["source_col"]) for row in calls) == [first, second]
    assert {int(row["line"]) for row in calls} == {3}


def test_tsx_typed_props_component_usage() -> None:
    source = (
        "const Card = ({t}: {t: string}) => <p>{t}</p>;\n"
        'export function List() { return <Card t="a" />; }\n'
    )
    calls = _calls("list.tsx", source, "typescript")
    assert [(row["src_qualname"], row["dst_name"], row["dst_qualname"]) for row in calls] == [
        ("list.tsx::List", "Card", "list.tsx::Card"),
    ]


def test_namespace_member_component_requires_imported_namespace() -> None:
    source = (
        "import * as UI from './ui';\n"
        "export function F() { return <UI.Button />; }\n"
        "export function G() { return <Ctx.Provider value={1}><span/></Ctx.Provider>; }\n"
        "export function H() { return <><b/></>; }\n"
    )
    calls = _calls("f.jsx", source, "javascript")
    assert [(row["src_qualname"], row["dst_name"], row["evidence_label"]) for row in calls] == [
        ("f.jsx::F", "Button", sgast.EXTRACTED),
    ]


def test_default_imported_component_and_top_level_jsx() -> None:
    source = (
        "import Header from './Header';\n"
        "const page = <Header />;\n"
        "export function App() { return <Header />; }\n"
    )
    calls = _calls("app.js", source, "javascript")
    assert [(row["src_qualname"], row["dst_name"], int(row["line"])) for row in calls] == [
        ("app.js::App", "Header", 3),
    ]


def _write_component_repo(repo: Path) -> None:
    task_store.initialize_repository(repo)
    (repo / "App.js").write_text(
        "import Header from './Header';\n"
        "export default function App() {\n"
        "  return <Header />;\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "Header.js").write_text(
        "export default function Header() {\n"
        "  return <h1>title</h1>;\n"
        "}\n",
        encoding="utf-8",
    )


def test_calls_query_lists_render_site_as_caller(tmp_path: Path) -> None:
    _write_component_repo(tmp_path)
    report = sg.build_index(tmp_path, incremental=False)
    assert report.errors == []

    payload = sg.analytics_query(tmp_path, "calls", "Header")

    assert payload["query_symbol"]["resolution"] == "exact_symbol_match"
    callers = {
        (row["caller_file"], int(row["line"]), row["callee_symbol"])
        for row in payload["incoming_calls"]
    }
    assert ("App.js", 3, "Header") in callers


def _js_jsx_edges(conn) -> list[tuple[str, str]]:
    return [
        (str(row["extractor"]), str(row["src_qualname"])) for row in conn.execute(
            "SELECT extractor, src_qualname FROM edges "
            "WHERE file_path='App.js' AND kind='calls' AND dst_name='Header'"
        )
    ]


def test_incremental_refresh_reextracts_v1_js_rows_but_not_python(tmp_path: Path) -> None:
    _write_component_repo(tmp_path)
    (tmp_path / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    assert sg.build_index(tmp_path, incremental=False).errors == []

    old_id = "aiworkhub.source_graph.tree_sitter.javascript_typescript.v1"
    assert sgast.TREE_SITTER_JS_TS_EXTRACTOR_ID != old_id
    conn = sg.connect(sg.resolve_db_path(tmp_path))
    try:
        # Rewrite App.js into what the v1 extractor persisted: same rows under
        # the old id, and no JSX render edge.
        with conn:
            conn.execute(
                "DELETE FROM edges WHERE file_path='App.js' AND kind='calls' "
                "AND dst_name='Header'"
            )
            for table in ("entities", "edges"):
                conn.execute(
                    f"UPDATE {table} SET extractor=? WHERE extractor=?",
                    (old_id, sgast.TREE_SITTER_JS_TS_EXTRACTOR_ID),
                )
        assert _js_jsx_edges(conn) == []
        python_rows = sorted(
            tuple(row) for row in conn.execute(
                "SELECT id, qualname, extractor FROM entities WHERE file_path='util.py'"
            )
        )
        python_indexed_at = conn.execute(
            "SELECT indexed_at FROM files WHERE file_path='util.py'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert python_rows

    sg.build_index(tmp_path, incremental=True)

    conn = sg.connect(sg.resolve_db_path(tmp_path))
    try:
        assert _js_jsx_edges(conn) == [
            (sgast.TREE_SITTER_JS_TS_EXTRACTOR_ID, "App.js::App"),
        ]
        assert sorted(
            tuple(row) for row in conn.execute(
                "SELECT id, qualname, extractor FROM entities WHERE file_path='util.py'"
            )
        ) == python_rows
        assert conn.execute(
            "SELECT indexed_at FROM files WHERE file_path='util.py'"
        ).fetchone()[0] == python_indexed_at
        stale = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE extractor=?", (old_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert stale == 0
