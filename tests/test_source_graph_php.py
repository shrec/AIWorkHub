"""PHP discovery and conservative structural Source Graph evidence."""

from __future__ import annotations

from pathlib import Path

from aiworkhub import source_graph as sg
from aiworkhub import source_graph_ast as sgast
from aiworkhub.repository_state import bootstrap_repository


PHP_SOURCE = """<?php
namespace App\\Service;

use Vendor\\BaseService as Base;
use JsonSerializable;

// class FakeComment { function fakeComment() {} }
$fake = "function fakeString() {}";

class Greeter extends Base implements JsonSerializable
{
    public function greet(string $name): string
    {
        return "Hello " . $name;
    }

    public function jsonSerialize(): array
    {
        return [];
    }
}

function helper(int $value): int
{
    return $value + 1;
}
"""


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "php_repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="php_repo")
    target = root / "src" / "Greeter.php"
    target.parent.mkdir()
    target.write_text(PHP_SOURCE, encoding="utf-8")
    return root


def test_php_extractor_emits_truthful_structural_evidence(tmp_path):
    repo = _repo(tmp_path)
    result = sgast.extract_file(
        repo, repo / "src" / "Greeter.php", build_revision=sg.BUILD_REVISION,
    )

    assert result.status == "ok"
    assert result.language == "php"
    assert all(item.extractor == sgast.PHP_LEXICAL_EXTRACTOR_ID for item in result.entities)
    by_name = {(item.kind, item.name): item for item in result.entities}
    assert ("class", "Greeter") in by_name
    assert ("method", "greet") in by_name
    assert ("method", "jsonSerialize") in by_name
    assert ("function", "helper") in by_name
    assert ("import", "Base") in by_name
    assert not any(item.name in {"FakeComment", "fakeComment", "fakeString"} for item in result.entities)

    assert any(edge.kind == "imports" and edge.dst_name == "Vendor\\BaseService" for edge in result.edges)
    assert any(edge.kind == "inherits" and edge.dst_name == "Vendor\\BaseService" for edge in result.edges)
    assert any(edge.kind == "inherits" and edge.dst_name == "JsonSerializable" for edge in result.edges)
    assert not any(edge.kind == "calls" for edge in result.edges)


def test_php_files_are_discovered_built_and_queryable(tmp_path):
    repo = _repo(tmp_path)

    report = sg.build_index(repo, incremental=True)
    assert report.files_seen == 1
    assert report.files_changed == 1
    assert report.entities_written >= 7

    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        assert sg.find(conn, "Greeter")
        assert sg.func(conn, "greet")
        file_row = conn.execute(
            "SELECT language, status, build_revision FROM files WHERE file_path=?",
            ("src/Greeter.php",),
        ).fetchone()
        assert tuple(file_row) == ("php", "ok", sg.BUILD_REVISION)
    finally:
        conn.close()

    unchanged = sg.build_index(repo, incremental=True)
    assert unchanged.files_seen == 1
    assert unchanged.files_unchanged == 1
    assert unchanged.files_changed == 0


def test_extractor_revision_change_forces_reindex_even_when_source_hash_matches(tmp_path):
    repo = _repo(tmp_path)
    first = sg.build_index(repo, incremental=True)
    assert first.files_changed == 1

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        with conn:
            conn.execute(
                "UPDATE files SET build_revision='legacy.python-only.v1' WHERE file_path=?",
                ("src/Greeter.php",),
            )
    finally:
        conn.close()

    rebuilt = sg.build_index(repo, incremental=True)
    assert rebuilt.files_changed == 1
    assert rebuilt.files_unchanged == 0
