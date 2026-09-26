"""Annotations bind to a same-file type or one imported symbol, never by global name."""

from __future__ import annotations

from pathlib import Path

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository


def _annotation(repo: Path, file_path: str, name: str) -> str | None:
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        row = conn.execute(
            "SELECT dst_qualname FROM edges WHERE kind='annotates' "
            "AND file_path=? AND dst_name=?",
            (file_path, name),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row["dst_qualname"]


def test_same_file_and_imported_annotations_bind(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo)
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "helpers.py").write_text(
        "class Helper:\n    pass\n",
        encoding="utf-8",
    )
    (pkg / "caller.py").write_text(
        "from pkg.helpers import Helper\n"
        "\n"
        "class Local:\n"
        "    pass\n"
        "\n"
        "def run(item: Helper, local: Local, text: str) -> Helper:\n"
        "    return item\n",
        encoding="utf-8",
    )
    sg.build_index(repo, incremental=False)
    assert _annotation(repo, "pkg/caller.py", "Helper") == "pkg/helpers.py.Helper"
    assert _annotation(repo, "pkg/caller.py", "Local") == "pkg/caller.py.Local"
    assert _annotation(repo, "pkg/caller.py", "str") is None


def test_stdlib_annotation_does_not_bind_by_stem(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo)
    (repo / "source_graph_ast.py").write_text(
        "class Path:\n    pass\n",
        encoding="utf-8",
    )
    (repo / "caller.py").write_text(
        "import ast\n"
        "\n"
        "def run(node: Path) -> None:\n"
        "    return None\n",
        encoding="utf-8",
    )
    sg.build_index(repo, incremental=False)
    assert _annotation(repo, "caller.py", "Path") is None
