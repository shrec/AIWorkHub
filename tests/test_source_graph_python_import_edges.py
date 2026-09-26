"""Dotted Python imports bind to one module. Bare names and stem collisions do not."""

from __future__ import annotations

from pathlib import Path

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository


def _import_target(repo: Path, file_path: str, dst_name: str) -> str | None:
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        row = conn.execute(
            "SELECT dst_qualname FROM edges WHERE kind='imports' AND file_path=? AND dst_name=?",
            (file_path, dst_name),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row["dst_qualname"]


def test_dotted_import_binds_symbol_and_bare_import_does_not(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo)
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "helpers.py").write_text(
        "def helper(value):\n    return value\n",
        encoding="utf-8",
    )
    (pkg / "source_graph_ast.py").write_text(
        "def parse(text):\n    return text\n",
        encoding="utf-8",
    )
    (pkg / "caller.py").write_text(
        "import os\n"
        "import ast\n"
        "from pkg.helpers import helper\n"
        "from .helpers import helper as local_helper\n",
        encoding="utf-8",
    )
    sg.build_index(repo, incremental=False)
    assert _import_target(repo, "pkg/caller.py", "os") is None
    assert _import_target(repo, "pkg/caller.py", "ast") is None
    assert _import_target(repo, "pkg/caller.py", "pkg.helpers.helper") == "pkg/helpers.py.helper"
    assert _import_target(repo, "pkg/caller.py", ".helpers.helper") == "pkg/helpers.py.helper"


def test_ambiguous_dotted_import_stays_unbound(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_repository(repo)
    for folder in ("a", "b"):
        path = repo / folder / "pkg"
        path.mkdir(parents=True)
        (path / "helpers.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (repo / "caller.py").write_text(
        "from pkg.helpers import helper\n",
        encoding="utf-8",
    )
    sg.build_index(repo, incremental=False)
    assert _import_target(repo, "caller.py", "pkg.helpers.helper") is None
