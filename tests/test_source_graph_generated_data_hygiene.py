"""NF-2026-00573: root data/ JSONL must not steal Source Graph budgets."""

from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository

_TERM = "nf573hygieneliteral"
_NESTED_TERM = "nf573nestedpackagedata"


def _new_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _hygiene_repo(tmp_path: Path) -> Path:
    repo = _new_repo(tmp_path, "hygiene")
    row = json.dumps({"token": _TERM, "pad": "x" * 180}, separators=(",", ":"))
    _write(repo / "data" / "inventory.jsonl", (row + "\n") * 400)
    _write(
        repo / "src" / "pkg" / "mod.py",
        f"def probe():\n    return {_TERM!r}\n",
    )
    _write(
        repo / "src" / "pkg" / "data" / "nested.jsonl",
        json.dumps({"token": _NESTED_TERM}) + "\n",
    )
    sg.build_index(repo, incremental=False)
    return repo


def test_root_data_jsonl_does_not_consume_bodygrep_result_budget(tmp_path):
    repo = _hygiene_repo(tmp_path)
    result = sg.bodygrep_query(repo, _TERM, budget=20)
    paths = [row["file_path"] for row in result["matches"]]
    assert paths, result
    assert any(path == "src/pkg/mod.py" for path in paths)
    assert not any(path.startswith("data/") for path in paths)


def test_nested_package_data_jsonl_remains_discoverable(tmp_path):
    repo = _hygiene_repo(tmp_path)
    rels = {path.relative_to(repo).as_posix() for path in sg.iter_source_files(repo)}
    assert "src/pkg/data/nested.jsonl" in rels
    result = sg.bodygrep_query(repo, _NESTED_TERM, budget=20)
    paths = [row["file_path"] for row in result["matches"]]
    assert "src/pkg/data/nested.jsonl" in paths


def test_explicit_ignore_policy_override_still_excludes_nested_package_data(tmp_path):
    repo = _new_repo(tmp_path, "override")
    config = sg.ensure_ignore_config(repo)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["exclude_globs"] = list(payload.get("exclude_globs") or []) + [
        "src/pkg/data/**",
    ]
    config.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write(repo / "src" / "pkg" / "data" / "nested.jsonl", '{"keep": false}\n')
    _write(repo / "src" / "live.py", "def live():\n    return 1\n")
    rels = {path.relative_to(repo).as_posix() for path in sg.iter_source_files(repo)}
    assert "src/live.py" in rels
    assert "src/pkg/data/nested.jsonl" not in rels


def test_index_quality_classifies_root_data_artifacts(tmp_path):
    repo = _hygiene_repo(tmp_path)
    db_path = sg.resolve_db_path(repo)
    conn = sg.connect(db_path, read_only=True)
    try:
        quality = sg._index_quality_scorecard(
            conn, db_path, finished_at="2026-09-09T00:00:00+00:00", previous=None,
        )
    finally:
        conn.close()
    assert "data" in quality["artifacts"]["path_families"]
    assert quality["artifacts"]["entities"] > 0
    assert quality["artifacts"]["entity_share"] not in {None, 0, 0.0}
