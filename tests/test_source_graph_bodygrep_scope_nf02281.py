"""NF-2026-02281: repo-wide bodygrep spends its cap on git-tracked source.

A repo-wide (untargeted) bodygrep walks git-tracked first-party files first and
never opens paths git ignores, vendored ``third_party/`` trees or ``bin/`` and
``obj/`` build output; a target naming such a tree still scans it. File mode
with ``target="path:START-END"`` serves that exact window instead of a silent
zero-hit reply.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository

NEEDLE = "NF02281_NEEDLE_LITERAL"
TRACKED = "src/app.py"
VENDORED = "third_party/lib/vendored.py"
TRACKED_VENDORED = "third_party/kept/tracked.py"
TRACKED_BIN = "bin/tool.py"
# Each filler file is well under the 1 MiB page byte cap, but together the
# ignored and build-output files exceed it several times over.
FILLER_BYTES = 400 * 1024
APP_LINES = [
    "def app():",
    f"    return '{NEEDLE}'",
    "",
    "def other():",
    "    return 2",
]


def _filler(label: str) -> str:
    line = f"# {label} {NEEDLE} " + "x" * 64 + "\n"
    return line * (FILLER_BYTES // len(line))


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, timeout=60,
    )


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git is required to build a tracked/ignored fixture")
    root = tmp_path_factory.mktemp("bodygrep_scope")
    bootstrap_repository(root, repo_name="bodygrep_scope")
    _git(root, "init", "-q")
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    gitignore.write_text(existing + "\ndump/\n", encoding="utf-8")
    files: dict[str, str] = {
        TRACKED: "\n".join(APP_LINES) + "\n",
        TRACKED_VENDORED: "KEPT = 1\n",
        TRACKED_BIN: "TOOL = 1\n",
        VENDORED: _filler("vendored"),
    }
    for index in range(4):
        files[f"dump/client_{index}.py"] = _filler("ignored dump")
        files[f"bin/Debug/doc_{index}.xml"] = _filler("build xml")
        files[f"bin/Debug/gen_{index}.py"] = _filler("build py")
        files[f"obj/Release/gen_{index}.py"] = _filler("build obj")
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    # VENDORED filler stays untracked, so the repo-wide scan still excludes it.
    _git(root, "add", "--", TRACKED, TRACKED_VENDORED, TRACKED_BIN)
    sg.build_index(root, incremental=False)
    return root


def _record_reads(monkeypatch) -> list[str]:
    reads: list[str] = []
    original = sg._bodygrep_file_bytes

    def recording(candidate, file_stat):
        reads.append(Path(candidate).as_posix())
        return original(candidate, file_stat)

    monkeypatch.setattr(sg, "_bodygrep_file_bytes", recording)
    return reads


def _excluded(path: str) -> bool:
    if path.endswith(TRACKED_VENDORED) or path.endswith(TRACKED_BIN):
        return False
    return any(f"/{part}/" in path for part in ("dump", "bin", "obj", "third_party"))


def test_repo_wide_bodygrep_finds_the_tracked_hit_without_scanning_ignored_or_build(
    repo, monkeypatch,
):
    reads = _record_reads(monkeypatch)
    payload = sg.bodygrep_query(repo, NEEDLE, 2)
    hits = [(row["file_path"], row["line_start"]) for row in payload["matches"]]
    assert hits == [(TRACKED, 2)]
    assert payload["scan_scope"] == "git_tracked_first"
    assert payload["scan_truncated"] is False, payload
    assert payload["next_cursor"] is None
    assert payload["scan_byte_cap"] == 1_048_576
    scanned = [path for path in reads if _excluded(path)]
    assert scanned == [], scanned


def test_a_target_still_scans_the_tree_a_repo_wide_scan_skips(repo):
    payload = sg.bodygrep_query(repo, NEEDLE, 64, target="third_party")
    assert payload["scan_scope"] == "target"
    assert {row["file_path"] for row in payload["matches"]} == {VENDORED}
    exact = sg.bodygrep_query(repo, NEEDLE, 64, target=TRACKED)
    assert [row["line_start"] for row in exact["matches"]] == [2]


def test_a_repository_git_cannot_describe_keeps_the_indexed_walk(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    bootstrap_repository(root, repo_name="plain")
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text(f"X = '{NEEDLE}'\n", encoding="utf-8")
    sg.build_index(root, incremental=False)
    tier, scope = sg._bodygrep_repo_scope(root)
    if scope == "git_tracked_first":
        pytest.skip("tmp_path sits inside a git work tree on this host")
    assert scope == "indexed"
    assert tier is sg._bodygrep_scan_tier
    payload = sg.bodygrep_query(root, NEEDLE, 8)
    assert [row["file_path"] for row in payload["matches"]] == ["pkg/mod.py"]


def test_scope_tier_orders_tracked_before_untracked_and_excludes_the_rest(repo):
    tier, scope = sg._bodygrep_repo_scope(repo)
    assert scope == "git_tracked_first"
    assert tier(TRACKED) == 0
    # A git-tracked path is never excluded, even under bin/ or third_party/.
    assert tier(TRACKED_BIN) == 0
    assert tier(TRACKED_VENDORED) == 1
    assert tier("src/new_untracked.py") == 1
    excluded = sg.BODYGREP_SCOPE_EXCLUDED_TIER
    assert tier(VENDORED) == excluded
    assert tier("dump/client_0.py") == excluded
    assert tier("bin/Debug/doc_0.xml") == excluded
    assert tier("obj/Release/gen_0.py") == excluded


def test_gitignored_path_under_an_include_root_stays_scannable(repo, monkeypatch):
    """NF-2026-02041: a policy include_root keeps a gitignored tree discoverable."""

    monkeypatch.setattr(
        sg, "load_ignore_policy",
        lambda _root: SimpleNamespace(include_roots=("dump",)),
    )
    tier, scope = sg._bodygrep_repo_scope(repo)
    assert scope == "git_tracked_first"
    assert tier("dump/client_0.py") == 1
    assert tier(TRACKED) == 0
    assert tier("bin/Debug/doc_0.xml") == sg.BODYGREP_SCOPE_EXCLUDED_TIER


@pytest.mark.parametrize(
    ("suffix", "expected"), [(":2-4", (2, 4)), (":2", (2, 2))],
)
def test_file_target_with_a_line_suffix_serves_that_window(repo, suffix, expected):
    payload = sg.file_query(repo, TRACKED + suffix, 64)
    assert payload["contexts"], payload
    context = payload["contexts"][0]
    assert context["file"]["file_path"] == TRACKED
    start, end = expected
    assert context["source_preview_line_start"] == start
    assert context["source_preview_line_end"] == end
    assert context["source_preview"].splitlines() == APP_LINES[start - 1:end]


def test_file_target_with_an_inverted_range_names_the_right_syntax(repo):
    with pytest.raises(sg.SourceGraphError) as raised:
        sg.file_query(repo, TRACKED + ":4-2", 64)
    message = str(raised.value)
    assert f"target={TRACKED}" in message
    assert 'query="START-END"' in message


def test_a_ranged_target_on_a_missing_path_is_named_not_silent(repo):
    payload = sg.file_query(repo, "src/missing.py:1-3", 64)
    assert payload["contexts"] == []
    assert payload["reason"] == "not_found"
