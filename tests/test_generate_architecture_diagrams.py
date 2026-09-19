"""Regression checks for the source-backed architecture diagrams."""

from __future__ import annotations

import runpy
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_index_facts_reads_hash_path_without_creating_stray_database(tmp_path: Path) -> None:
    repository = tmp_path / "project#diagrams"
    database = repository / ".aiworkhub" / "source_graph" / "source_graph.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE files (language TEXT);"
            "CREATE TABLE entities (id INTEGER);"
            "CREATE TABLE edges (id INTEGER);"
            "INSERT INTO files VALUES ('python');"
            "INSERT INTO entities VALUES (1);"
            "INSERT INTO edges VALUES (1);"
        )

    index_facts = runpy.run_path(
        str(ROOT / "scripts" / "generate_architecture_diagrams.py")
    )["index_facts"]

    assert index_facts(repository) == {
        "state": "live",
        "files": "1",
        "entities": "1",
        "edges": "1",
        "languages": "1",
    }
    assert not (tmp_path / "project").exists()


def test_diagram_check_ignores_checkout_local_index_presence() -> None:
    strip_volatile = runpy.run_path(
        str(ROOT / "scripts" / "generate_architecture_diagrams.py")
    )["_strip_volatile"]
    with_index = (
        "<text>aiworkhub.source_graph.semantic.v6 · this repository right now: "
        "1,027 files · 52,796 entities · 260,916 edges · 11 languages</text>"
    )
    without_index = (
        "<text>aiworkhub.source_graph.semantic.v6 · "
        "index not built in this checkout</text>"
    )

    assert strip_volatile(with_index) == strip_volatile(without_index)


def test_readme_full_resolution_source_graph_link_uses_shipped_svg() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert '<a href="site/assets/aiworkhub-source-graph-architecture.svg">' in readme
