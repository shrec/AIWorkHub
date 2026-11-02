"""NF-2026-02001: ``.inc`` is C/C++ and a gitignored changed path gets a partition row.

Covers: a textual C/C++ include (``.inc``) is indexed by the canonical build and
readable through file/bodygrep; ``build_partition`` indexes an untracked changed
path that the repository's ``.gitignore`` matches (``admitted_changed_path`` is
the one admit decision); a direct ``index_file`` call without
``admit_gitignored`` still refuses it; a tracked force-added file stays indexed
by the canonical full build.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import source_graph_languages, tool_guard_hook
from aiworkhub import source_graph_partition as sgp
from aiworkhub.repository_state import bootstrap_repository

_GIT = shutil.which("git")
needs_git = pytest.mark.skipif(_GIT is None, reason="git executable unavailable")

_INC_SOURCE = (
    "static int table_lookup_nf02001(int key)\n"
    "{\n"
    "    return key * 2; /* inc_marker_nf02001 */\n"
    "}\n"
)
_GEN_SOURCE = "def generated_nf02001():\n    return 1\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [_GIT or "git", "-C", str(root), *args],
        check=True, capture_output=True, timeout=60,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_repo(tmp_path: Path, name: str, ignore: str) -> Path:
    root = (tmp_path / name).resolve()
    root.mkdir()
    _git(root, "init", "-q")
    bootstrap_repository(root, repo_name=name)
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    gitignore.write_text(existing + ignore, encoding="utf-8")
    return root


def _indexed_files(repo: Path) -> set[str]:
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        return {str(row["file_path"]) for row in conn.execute("SELECT file_path FROM files")}
    finally:
        conn.close()


def test_inc_is_a_cpp_extension_on_both_sides() -> None:
    assert ".inc" in source_graph_languages.LANGUAGE_BY_ID["cpp"].extensions
    assert source_graph_languages.LANGUAGE_BY_EXTENSION[".inc"] == "cpp"
    assert ".inc" in tool_guard_hook.LANGUAGE_EXTENSIONS["cpp"]


def test_inc_file_is_indexed_and_readable(tmp_path: Path) -> None:
    repo = tmp_path / "repo_inc"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="repo_inc")
    _write(repo / "src" / "table.inc", _INC_SOURCE)
    _write(repo / "src" / "main.c", "int main(void)\n{\n    return 0;\n}\n")

    rels = {p.relative_to(repo).as_posix() for p in sg.iter_source_files(repo)}
    assert "src/table.inc" in rels

    sg.build_index(repo, incremental=False)
    assert "src/table.inc" in _indexed_files(repo)

    payload = sg.file_query(repo, "src/table.inc", budget=16)
    assert payload.get("found", True) is True, payload
    row = payload["matches"][0] if payload["matches"] else {}
    assert row.get("file_path") == "src/table.inc", payload
    assert row.get("language") == "cpp", payload
    assert row.get("status") == "ok", payload
    assert payload["matches"], payload

    literal = sg.bodygrep_query(repo, "inc_marker_nf02001", budget=16)
    assert any(
        match.get("file_path") == "src/table.inc" for match in literal["matches"]
    ), literal


@needs_git
def test_partition_indexes_untracked_gitignored_changed_path(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "repo_overlay", "*.py\n")
    _write(repo / "tools" / "gen.py", _GEN_SOURCE)
    partition = tmp_path / "partition.sqlite"

    report = sgp.build_partition(
        repo,
        [{"path": "tools/gen.py", "sha256": _sha256(repo / "tools" / "gen.py")}],
        partition,
    )

    assert not any(
        "source_graph_single_file_gitignored" in str(entry.get("reason", ""))
        for entry in report.failed_files
    ), report.failed_files
    assert report.failed_files == () or list(report.failed_files) == [], report.failed_files
    assert report.files_indexed == 1
    assert report.files_skipped == 0


@needs_git
def test_direct_index_file_still_refuses_untracked_gitignored_path(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "repo_direct", "*.py\n")
    _write(repo / "tools" / "gen.py", _GEN_SOURCE)

    with pytest.raises(sg.SourceGraphError, match="source_graph_single_file_gitignored"):
        sg.index_file(repo, "tools/gen.py", _sha256(repo / "tools" / "gen.py"))


@needs_git
def test_force_added_tracked_file_is_indexed_by_full_build(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "repo_force_added", "*.py\n")
    _write(repo / "tools" / "kept.py", _GEN_SOURCE)
    _write(repo / "tools" / "stray.py", _GEN_SOURCE)
    _git(repo, "add", "-f", "tools/kept.py")

    sg.build_index(repo, incremental=False)
    indexed = _indexed_files(repo)
    assert "tools/kept.py" in indexed
    assert "tools/stray.py" not in indexed
