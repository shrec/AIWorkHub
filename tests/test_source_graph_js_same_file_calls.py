"""Bare same-file JS calls bind; member calls such as document.createElement do not."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import source_graph_semantic as sgsemantic
from aiworkhub.repository_state import bootstrap_repository


def test_member_call_is_not_a_bare_call() -> None:
    member = "  return document.createElement(tag);"
    bare = "  return createElement('div');"
    mixed = "  return createElement(document.createElement(tag));"
    assert sg._javascript_bare_call(member, "createElement") is False
    assert sg._javascript_bare_call(bare, "createElement") is True
    assert sg._javascript_bare_call(mixed, "createElement") is False
    bare_col = mixed.index("createElement(")
    member_col = mixed.index("document.createElement(") + len("document.")
    assert sg._javascript_bare_call(mixed, "createElement", bare_col) is True
    assert sg._javascript_bare_call(mixed, "createElement", member_col) is False


def test_semantic_member_call_does_not_bind_same_file_helper() -> None:
    raw = (
        "function createElement(tag) {\n"
        "  return document.createElement(tag);\n"
        "}\n"
        "function render() {\n"
        "  return createElement(document.createElement('div'));\n"
        "}\n"
    ).encode()
    extracted = sgsemantic.extract_javascript_typescript(
        file_path="app.js", raw=raw, language="javascript",
    )
    assert extracted is not None
    calls = [
        row for row in extracted.edges
        if row["kind"] == "calls" and row["dst_name"] == "createElement"
    ]
    bound = [row for row in calls if row["dst_qualname"] == "app.js::createElement"]
    unbound = [row for row in calls if row["dst_qualname"] is None]
    assert len(bound) == 1
    assert len(unbound) == 2
    assert bound[0]["line"] == 5


def _create_element_calls(repo: Path) -> list[dict[str, object]]:
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        rows = conn.execute(
            "SELECT file_path, line, source_col, dst_qualname FROM edges "
            "WHERE kind='calls' AND dst_name='createElement' "
            "ORDER BY file_path, line, source_col"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def test_same_file_bare_call_binds_and_member_call_does_not(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo)
    (repo / "app.js").write_text(
        "function createElement(tag) {\n"
        "  return document.createElement(tag);\n"
        "}\n"
        "function render() {\n"
        "  return createElement(document.createElement('div'));\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "other.js").write_text(
        "function render() {\n"
        "  return createElement('div');\n"
        "}\n",
        encoding="utf-8",
    )
    sg.build_index(repo, incremental=False)
    calls = _create_element_calls(repo)
    app = [row for row in calls if row["file_path"] == "app.js"]
    other = [row for row in calls if row["file_path"] == "other.js"]
    assert [row["dst_qualname"] for row in app] == [
        None,
        "app.js::createElement",
        None,
    ]
    assert other == [] or all(row["dst_qualname"] is None for row in other)


def test_bare_call_binds_when_a_same_named_method_also_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo)
    (repo / "app.js").write_text(
        "function createElement(tag) {\n"
        "  return tag;\n"
        "}\n"
        "class FakeDocument {\n"
        "  createElement(tag) {\n"
        "    return tag;\n"
        "  }\n"
        "}\n"
        "function render() {\n"
        "  return createElement(document.createElement('div'));\n"
        "}\n",
        encoding="utf-8",
    )
    sg.build_index(repo, incremental=False)
    calls = [
        row for row in _create_element_calls(repo)
        if row["file_path"] == "app.js"
    ]
    bound = [row for row in calls if row["dst_qualname"] == "app.js::createElement"]
    unbound = [row for row in calls if row["dst_qualname"] is None]
    assert len(bound) == 1
    assert bound[0]["line"] == 10
    assert unbound
    assert all(row["line"] == 10 for row in unbound)


def test_lexical_fallback_does_not_bind_member_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sgsemantic, "extract_javascript_typescript", lambda **_kwargs: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo)
    (repo / "app.js").write_text(
        "function createElement(tag) {\n"
        "  return tag;\n"
        "}\n"
        "function render() {\n"
        "  return document.createElement(tag);\n"
        "}\n"
        "function paint() {\n"
        "  return createElement('div');\n"
        "}\n",
        encoding="utf-8",
    )
    sg.build_index(repo, incremental=False)
    by_line = {
        int(row["line"]): row["dst_qualname"]
        for row in _create_element_calls(repo)
    }
    assert by_line[5] is None
    assert by_line[8] == "app.js::createElement"


def test_imported_bare_call_binds_and_unimported_call_does_not(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo)
    (repo / "target.js").write_text(
        "function helper() {\n  return 1;\n}\n",
        encoding="utf-8",
    )
    (repo / "main.js").write_text(
        "import { helper } from './target';\n"
        "function run() {\n"
        "  return helper(document.helper());\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "other.js").write_text(
        "function run() {\n  return helper();\n}\n",
        encoding="utf-8",
    )
    sg.build_index(repo, incremental=False)
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        rows = conn.execute(
            "SELECT file_path, line, dst_qualname FROM edges "
            "WHERE kind='calls' AND dst_name='helper' "
            "ORDER BY file_path, line, source_col"
        ).fetchall()
    finally:
        conn.close()
    main = [row["dst_qualname"] for row in rows if row["file_path"] == "main.js"]
    other = [row["dst_qualname"] for row in rows if row["file_path"] == "other.js"]
    assert main.count("target.js::helper") == 1
    assert None in main
    assert other
    assert all(qualname is None for qualname in other)
