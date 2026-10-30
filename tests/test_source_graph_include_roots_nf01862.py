"""Opt-in ``include_roots`` index gitignored source trees in Source Graph."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository
from aiworkhub.source_graph import SourceGraphError, iter_source_files, load_ignore_policy


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _policy(repo: Path, **fields: object) -> None:
    payload = {
        "schema_id": sg.POLICY_SCHEMA_ID,
        "revision": 1,
        "exclude_dirs": [],
        "exclude_globs": [],
        "disabled_languages": [],
    }
    payload.update(fields)
    _write(sg.ignore_config_path(repo), json.dumps(payload))


def _repo(tmp_path: Path, gitignore: str = "vendor/\n") -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    bootstrap_repository(tmp_path, repo_name="nf01862")
    _write(tmp_path / ".gitignore", gitignore)
    _write(tmp_path / "pkg" / "keep.py", "def keep():\n    return 1\n")
    _write(tmp_path / "vendor" / "lib" / "mod.py", "def vendored_fn():\n    return 2\n")
    _write(tmp_path / "vendor" / "other" / "skip.py", "def skipped_fn():\n    return 3\n")
    _write(tmp_path / "vendor" / "library" / "near.py", "def near_fn():\n    return 4\n")
    return tmp_path


def _discovered(repo: Path) -> set[str]:
    return {p.relative_to(repo).as_posix() for p in iter_source_files(repo.resolve())}


def test_default_policy_has_no_include_roots_and_ignores_vendor(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert load_ignore_policy(repo).include_roots == ()
    assert _discovered(repo) == {"pkg/keep.py"}


def test_include_root_walks_gitignored_tree_but_not_siblings(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _policy(repo, include_roots=["vendor/lib/"])
    assert load_ignore_policy(repo).include_roots == ("vendor/lib",)
    assert _discovered(repo) == {"pkg/keep.py", "vendor/lib/mod.py"}


def test_include_root_with_partially_ignored_siblings(tmp_path: Path) -> None:
    repo = _repo(tmp_path, gitignore="vendor/*\n")
    _policy(repo, include_roots=["vendor/lib"])
    assert _discovered(repo) == {"pkg/keep.py", "vendor/lib/mod.py"}


def test_exclusions_still_apply_inside_include_root(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo / "vendor" / "lib" / "node_modules" / "x.py", "X = 1\n")
    _write(repo / "vendor" / "lib" / "gen" / "g.py", "X = 1\n")
    _write(repo / "vendor" / "lib" / "gen2" / "g.py", "X = 1\n")
    _policy(repo, include_roots=["vendor/lib"], exclude_dirs=["gen"], exclude_globs=["vendor/lib/gen2/**"])
    assert _discovered(repo) == {"pkg/keep.py", "vendor/lib/mod.py"}


@pytest.mark.parametrize(
    "bad",
    ["/abs", "../up", "a/../b", "C:/x", "c:rel", "vendor\\lib", "./vendor", "vendor//lib", "."],
)
def test_unsafe_include_root_is_refused(tmp_path: Path, bad: str) -> None:
    _policy(tmp_path, include_roots=[bad])
    with pytest.raises(SourceGraphError, match="include_roots_must_be_relative"):
        load_ignore_policy(tmp_path)


def test_include_roots_capped_at_64(tmp_path: Path) -> None:
    _policy(tmp_path, include_roots=[f"d{i}" for i in range(65)])
    with pytest.raises(SourceGraphError, match="include_roots"):
        load_ignore_policy(tmp_path)


def test_language_toggle_keeps_include_roots(tmp_path: Path) -> None:
    _policy(tmp_path, include_roots=["vendor/lib"])
    sg.update_language_policy(tmp_path, language_changes={"python": False}, expected_revision=1)
    payload = json.loads(sg.ignore_config_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["include_roots"] == ["vendor/lib"]
    assert sg.source_graph_policy_view(tmp_path)["include_roots"] == ["vendor/lib"]


def test_index_file_accepts_gitignored_file_under_include_root(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = repo / "vendor" / "lib" / "mod.py"
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(SourceGraphError, match="source_graph_single_file_gitignored"):
        sg.index_file(repo, "vendor/lib/mod.py", digest)
    _policy(repo, include_roots=["vendor/lib"])
    assert sg.index_file(repo, "vendor/lib/mod.py", digest)["ok"] is True
    other = repo / "vendor" / "other" / "skip.py"
    with pytest.raises(SourceGraphError, match="source_graph_single_file_gitignored"):
        sg.index_file(repo, "vendor/other/skip.py", hashlib.sha256(other.read_bytes()).hexdigest())


def test_body_marks_untracked_only_for_ignored_include_root_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _policy(repo, include_roots=["vendor/lib"])
    sg.build_index(repo, incremental=False)
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        vendored = sg.body(conn, repo, "vendored_fn")
        tracked = sg.body(conn, repo, "keep")
        assert sg.body(conn, repo, "skipped_fn") is None
        assert sg.body(conn, repo, "near_fn") is None
    finally:
        conn.close()
    assert vendored is not None and vendored["untracked"] is True
    assert "vendored_fn" in vendored["source"]
    assert tracked is not None and "untracked" not in tracked
