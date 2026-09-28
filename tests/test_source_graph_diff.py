"""RM-2026-00071 core: Source Graph diff of a registered worker worktree."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from aiworkhub import source_graph_diff as sgd
from aiworkhub import worker_workspace

REQUEST_ID = "req_sgd_0001"

MOD_BEFORE = '''def stable_fn():
    return 1


def changed_fn():
    return 2


class Widget:
    def render(self):
        return "old"

    def keep(self):
        return "same"
'''

MOD_AFTER = MOD_BEFORE.replace("return 2", "return 3").replace('"old"', '"new"')


def _git(cwd: Path, *args: str, pin_lf: bool = True) -> str:
    # pin_lf=False lets the repository's own core.autocrlf decide checkouts.
    pinned = ["-c", "core.autocrlf=false"] if pin_lf else []
    completed = subprocess.run(
        [
            "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "-c", "commit.gpgsign=false", *pinned, *args,
        ],
        cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
    )
    return completed.stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    _write(root / ".gitignore", "__pycache__/\n.aiworkhub/\n")
    _write(root / "src/pkg/mod.py", MOD_BEFORE)
    _write(root / "src/pkg/old.py", "def gone():\n    return 'deleted'\n")
    _write(root / "src/pkg/moved.py", "def travels():\n    return 'renamed'\n")
    _write(root / "docs/outside.md", "outside the sparse set\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _sparse_worktree(root: Path) -> Path:
    path = root / ".aiworkhub" / "runtime" / "worktrees" / REQUEST_ID / "worktree"
    path.parent.mkdir(parents=True)
    _git(root, "worktree", "add", "-q", "--detach", "--no-checkout", str(path), "HEAD")
    _git(path, "sparse-checkout", "set", "--no-cone", "/.gitignore", "/src/")
    _git(path, "read-tree", "-mu", "HEAD")
    assert not (path / "docs").exists()
    return path


def _candidate(root: Path) -> tuple[Path, dict]:
    path = _sparse_worktree(root)
    # Coordinator seed overlay: the live parent's .gitignore replaces the base
    # copy before the worker starts; the workspace baseline records it.
    _write(path / ".gitignore", "__pycache__/\n.aiworkhub/\n*.spill\n")
    metadata = worker_workspace.WorkerWorkspace(
        request_id=REQUEST_ID, repo=root, path=path.resolve(),
        home=path.parent.resolve() / "home", allowed_writes=("src/pkg/*",),
        parent_baseline={},
        workspace_baseline={".gitignore": worker_workspace._hash_path(path / ".gitignore")},
        base_oid=_git(root, "rev-parse", "HEAD").strip(),
    ).as_metadata()
    # Worker edits.
    _write(path / "src/pkg/mod.py", MOD_AFTER)
    _write(path / "src/pkg/new.py", "def fresh():\n    return 'added'\n")
    (path / "src/pkg/old.py").unlink()
    os.replace(path / "src/pkg/moved.py", path / "src/pkg/renamed.py")
    # Worker-local noise.
    _write(path / "src/pkg/__pycache__/mod.cpython-312.pyc", "cache")
    _write(path / "result.spill", "spill")
    return path, metadata


def test_registered_worktree_diff_classifies_and_names_symbols(repo: Path) -> None:
    _path, metadata = _candidate(repo)
    result = sgd.diff_registered_worktree(repo, REQUEST_ID, workspace_metadata=metadata)

    assert result["schema_id"] == sgd.SCHEMA_ID
    assert result["base_oid"] == metadata["base_oid"]
    files = {row["path"]: row for row in result["files"]}
    assert files["src/pkg/mod.py"]["status"] == "modified"
    assert files["src/pkg/new.py"]["status"] == "added"
    assert files["src/pkg/old.py"]["status"] == "deleted"
    assert files["src/pkg/renamed.py"] == {
        "status": "renamed", "path": "src/pkg/renamed.py", "old_path": "src/pkg/moved.py",
    }
    assert "src/pkg/moved.py" not in files
    # Sparse-absent files are not deletions; seeded .gitignore, caches and
    # spill follow the promotion rules and never enter the diff.
    assert set(files) == {
        "src/pkg/mod.py", "src/pkg/new.py", "src/pkg/old.py", "src/pkg/renamed.py",
    }

    symbols = {(row["path"], row["symbol"]): row for row in result["symbols"]}
    assert symbols[("src/pkg/mod.py", "changed_fn")]["change"] == "modified"
    assert symbols[("src/pkg/mod.py", "changed_fn")]["qualname"] == "src/pkg/mod.py.changed_fn"
    assert symbols[("src/pkg/mod.py", "Widget.render")]["change"] == "modified"
    assert ("src/pkg/mod.py", "stable_fn") not in symbols
    assert ("src/pkg/mod.py", "Widget.keep") not in symbols
    assert symbols[("src/pkg/new.py", "fresh")]["change"] == "added"
    assert symbols[("src/pkg/old.py", "gone")]["change"] == "deleted"
    assert not [row for row in result["symbols"] if row["path"] == "src/pkg/renamed.py"]

    assert result["truncated"] is False
    assert result["next_cursor"] is None
    text = "".join(hunk["text"] for hunk in result["hunks"])
    assert '-        return "old"' in text and '+        return "new"' in text
    assert all(hunk["complete"] for hunk in result["hunks"])


def test_gitignore_rewrite_is_only_excluded_by_the_workspace_baseline(repo: Path) -> None:
    _path, metadata = _candidate(repo)
    without = sgd.diff_registered_worktree(repo, REQUEST_ID, base_oid=metadata["base_oid"])
    with_baseline = sgd.diff_registered_worktree(
        repo, REQUEST_ID, workspace_metadata=metadata
    )
    assert ".gitignore" in {row["path"] for row in without["files"]}
    assert ".gitignore" not in {row["path"] for row in with_baseline["files"]}
    for result in (without, with_baseline):
        paths = {row["path"] for row in result["files"]}
        assert not any("__pycache__" in p or p.endswith(".spill") for p in paths)


def test_hunks_truncate_at_byte_budget_and_resume_with_cursor(repo: Path) -> None:
    _path, metadata = _candidate(repo)
    full = sgd.diff_registered_worktree(repo, REQUEST_ID, workspace_metadata=metadata)
    expected = {hunk["index"]: hunk["text"] for hunk in full["hunks"]}
    # mod.py's two nearby edits share one hunk; new.py and old.py add one each.
    assert full["hunk_total"] == len(expected) == 3

    budget = 40
    pieces: dict[int, str] = {}
    cursor = None
    pages = 0
    while True:
        page = sgd.diff_registered_worktree(
            repo, REQUEST_ID, workspace_metadata=metadata, byte_budget=budget, cursor=cursor
        )
        pages += 1
        assert sum(len(h["text"].encode()) for h in page["hunks"]) <= budget
        for hunk in page["hunks"]:
            assert len(pieces.get(hunk["index"], "").encode()) == hunk["offset"]
            pieces[hunk["index"]] = pieces.get(hunk["index"], "") + hunk["text"]
        if not page["truncated"]:
            assert page["next_cursor"] is None and page["truncation_marker"] is None
            break
        assert page["truncation_marker"] and str(budget) in page["truncation_marker"]
        cursor = page["next_cursor"]
    assert pages > 2
    assert pieces == expected

    first = sgd.diff_registered_worktree(
        repo, REQUEST_ID, workspace_metadata=metadata, byte_budget=budget
    )
    stale = first["next_cursor"].rsplit(":", 1)[0] + ":0000000000000000"
    with pytest.raises(sgd.SourceGraphDiffError, match="cursor_stale"):
        sgd.diff_registered_worktree(
            repo, REQUEST_ID, workspace_metadata=metadata, byte_budget=budget, cursor=stale
        )
    with pytest.raises(sgd.SourceGraphDiffError, match="cursor_invalid"):
        sgd.diff_registered_worktree(repo, REQUEST_ID, cursor="garbage")


def _forbid_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args, **_kwargs):
        raise AssertionError("read before refusal")

    for name in ("hash_files", "_read_side", "_walk", "_base_blobs"):
        monkeypatch.setattr(sgd, name, _boom)
    monkeypatch.setattr(worker_workspace, "changed_paths", _boom)


def test_out_of_repository_directory_is_refused_without_reading(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "inside").mkdir()
    _forbid_reads(monkeypatch)
    for old, new in ((outside, repo / "inside"), (repo / "inside", "../outside")):
        with pytest.raises(sgd.SourceGraphDiffError, match="path_outside_repository"):
            sgd.diff_directories(repo, old, new)


def test_unregistered_request_id_is_refused_without_reading(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_reads(monkeypatch)
    with pytest.raises(sgd.SourceGraphDiffError, match="worktree_not_registered"):
        sgd.diff_registered_worktree(repo, "req_never_created")
    # A directory at the canonical location that git never registered.
    fake = repo / ".aiworkhub" / "runtime" / "worktrees" / "req_fake" / "worktree"
    fake.mkdir(parents=True)
    _write(fake / "src/x.py", "x = 1\n")
    with pytest.raises(sgd.SourceGraphDiffError, match="worktree_not_registered"):
        sgd.diff_registered_worktree(repo, "req_fake")
    with pytest.raises(sgd.SourceGraphDiffError, match="invalid_request_id"):
        sgd.diff_registered_worktree(repo, "../escape")


def test_parallel_and_sequential_hashing_are_identical(repo: Path) -> None:
    root = repo / "bulk"
    names = [f"d{i % 3}/f{i:02d}.txt" for i in range(24)]
    for index, name in enumerate(names):
        _write(root / name, f"payload {index}\n" * (index + 1))
    sequential = sgd.hash_files(root, names, max_workers=1)
    parallel = sgd.hash_files(root, names, max_workers=4)
    default = sgd.hash_files(root, names)
    assert sequential == parallel == default
    assert list(sequential) == sorted(names)
    assert all(value and value.startswith("file:") for value in sequential.values())


def test_directory_diff_inside_repository(repo: Path) -> None:
    old, new = repo / "cmp" / "a", repo / "cmp" / "b"
    _write(old / "m.py", MOD_BEFORE)
    _write(new / "m.py", MOD_AFTER)
    _write(old / "keep.py", "def same():\n    return 0\n")
    _write(new / "moved.py", "def same():\n    return 0\n")
    _write(new / "__pycache__/m.pyc", "cache")
    result = sgd.diff_directories(repo, old, "cmp/b")
    files = {row["path"]: row["status"] for row in result["files"]}
    assert files == {"m.py": "modified", "moved.py": "renamed"}
    assert {row["symbol"] for row in result["symbols"]} == {"changed_fn", "Widget", "Widget.render"}


def test_form_feed_line_keeps_entity_bodies_aligned_with_ast_lines(repo: Path) -> None:
    # str.splitlines breaks on \f, the AST does not: a later body must not shift.
    head = "def head():\n    return 0\n\f\n"
    old, new = repo / "ff" / "a", repo / "ff" / "b"
    _write(old / "m.py", head + "def tail():\n    x = 1\n    return 1\n")
    _write(new / "m.py", head + "def tail():\n    x = 1\n    return 2\n")
    result = sgd.diff_directories(repo, old, new)
    assert [row["path"] for row in result["files"]] == ["m.py"]
    changes = {row["symbol"]: row["change"] for row in result["symbols"]}
    assert changes == {"tail": "modified"}


def test_form_feed_inside_hunk_context_keeps_real_line_numbers(repo: Path) -> None:
    # Hunks split on \n only: a \f line is one line, never a false EOF marker.
    lines = [f"v{index} = {index}\n" for index in range(1, 13)]
    lines[5] = "v6 = 6  \x0c# ff\n"
    old, new = repo / "ffh" / "a", repo / "ffh" / "b"
    _write(old / "m.py", "".join(lines))
    lines[7] = "v8 = 80\n"
    _write(new / "m.py", "".join(lines))
    result = sgd.diff_directories(repo, old, new)
    assert [hunk["header"] for hunk in result["hunks"]] == ["@@ -5,7 +5,7 @@"]
    text = result["hunks"][0]["text"]
    assert "No newline" not in text
    assert "-v8 = 8\n+v8 = 80\n" in text


def test_change_only_in_invalid_utf8_bytes_is_reported_binary(repo: Path) -> None:
    old, new = repo / "bad" / "a", repo / "bad" / "b"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "data.txt").write_bytes(b"x\xff\n")
    (new / "data.txt").write_bytes(b"x\xfe\n")
    result = sgd.diff_directories(repo, old, new)
    assert [(row["path"], row["status"]) for row in result["files"]] == [
        ("data.txt", "modified")
    ]
    assert [hunk["header"] for hunk in result["hunks"]] == ["binary"]
    assert result["hunks"][0]["text"] == "Binary files differ\n"


@pytest.mark.parametrize(
    "base, candidate",
    [
        (b"a\xff\nb\n", b"a\xfe\nB\n"),
        (b"a\xff\nb\n", b"new\na\xfe\nb\n"),
    ],
    ids=["beside_a_visible_edit", "below_an_insertion"],
)
def test_invalid_utf8_change_beside_a_visible_edit_is_reported_binary(
    repo: Path, base: bytes, candidate: bytes
) -> None:
    old, new = repo / "mix" / "a", repo / "mix" / "b"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "data.txt").write_bytes(base)
    (new / "data.txt").write_bytes(candidate)
    result = sgd.diff_directories(repo, old, new)
    # "replace" decodes \xff and \xfe alike, so the visible edit must not be the
    # whole report; the two bad lines still pair up across an insertion above.
    assert [hunk["header"] for hunk in result["hunks"]] == ["binary"]
    assert result["hunks"][0]["text"] == "Binary files differ\n"


def test_unchanged_invalid_utf8_line_beside_a_visible_edit_stays_a_text_diff(repo: Path) -> None:
    old, new = repo / "keep" / "a", repo / "keep" / "b"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "data.txt").write_bytes(b"caf\xe9\nx\n")
    (new / "data.txt").write_bytes(b"caf\xe9\ny\n")
    result = sgd.diff_directories(repo, old, new)
    assert [hunk["header"] for hunk in result["hunks"]] == ["@@ -1,2 +1,2 @@"]
    assert "-x\n+y\n" in result["hunks"][0]["text"]


GUIDE = "".join(f"line {index}\n" for index in range(1, 21))


def test_crlf_checkout_keeps_one_hunk_edits_and_renames(tmp_path: Path) -> None:
    root = (tmp_path / "crlf").resolve()
    root.mkdir()
    _git(root, "init", "-q", pin_lf=False)
    _git(root, "config", "core.autocrlf", "true", pin_lf=False)
    _write(root / ".gitattributes", "* text=auto\n*.py text eol=lf\n")
    _write(root / "docs/guide.md", GUIDE)
    _write(root / "docs/moved.json", '{\n  "travels": true\n}\n')
    _git(root, "add", "-A", pin_lf=False)
    _git(root, "commit", "-q", "-m", "base", pin_lf=False)
    path = root / ".aiworkhub" / "runtime" / "worktrees" / REQUEST_ID / "worktree"
    path.parent.mkdir(parents=True)
    _git(root, "worktree", "add", "-q", "--detach", str(path), "HEAD", pin_lf=False)
    guide = path / "docs/guide.md"
    assert b"line 10\r\n" in guide.read_bytes()  # the checkout really is CRLF
    guide.write_bytes(guide.read_bytes().replace(b"line 10\r\n", b"line ten\r\n"))
    os.replace(path / "docs/moved.json", path / "docs/renamed.json")

    result = sgd.diff_registered_worktree(
        root, REQUEST_ID, base_oid=_git(root, "rev-parse", "HEAD").strip()
    )
    assert result["files"] == [
        {"status": "modified", "path": "docs/guide.md", "old_path": "docs/guide.md"},
        {"status": "renamed", "path": "docs/renamed.json", "old_path": "docs/moved.json"},
    ]
    # Markdown has no symbol extractor on either side: that is no failure.
    assert "symbols_unavailable" not in result["files"][0]
    assert result["symbols"] == []
    assert [hunk["path"] for hunk in result["hunks"]] == ["docs/guide.md"]
    body = result["hunks"][0]["text"].splitlines()[1:]
    assert [line for line in body if line.startswith("-")] == ["-line 10"]
    assert [line for line in body if line.startswith("+")] == ["+line ten"]


def test_oversized_file_is_skipped_while_the_rest_still_diffs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, metadata = _candidate(repo)
    # Both sides of mod.py exceed the limit; every other changed file fits.
    limit = len(MOD_BEFORE.encode()) - 1
    monkeypatch.setattr(sgd, "MAX_FILE_BYTES", limit)
    assert sgd._base_blobs(path, metadata["base_oid"], ["src/pkg/mod.py", "src/pkg/old.py"]) == {
        "src/pkg/mod.py": sgd.OVERSIZED,
        "src/pkg/old.py": b"def gone():\n    return 'deleted'\n",
    }

    result = sgd.diff_registered_worktree(repo, REQUEST_ID, workspace_metadata=metadata)
    files = {row["path"]: row for row in result["files"]}
    assert files["src/pkg/mod.py"] == {
        "status": "modified", "path": "src/pkg/mod.py", "old_path": "src/pkg/mod.py",
        "oversized": True,
    }
    assert result["skipped"] == [
        {"path": "src/pkg/mod.py", "reason": "file_over_byte_limit", "byte_limit": limit}
    ]
    assert files["src/pkg/new.py"]["status"] == "added"
    assert files["src/pkg/renamed.py"]["status"] == "renamed"
    assert {hunk["path"] for hunk in result["hunks"]} == {"src/pkg/new.py", "src/pkg/old.py"}
    assert not [row for row in result["symbols"] if row["path"] == "src/pkg/mod.py"]
    assert ("src/pkg/new.py", "fresh") in {
        (row["path"], row["symbol"]) for row in result["symbols"]
    }


def test_oversized_side_is_reported_under_its_own_directory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = repo / "big" / "a", repo / "big" / "b"
    monkeypatch.setattr(sgd, "MAX_FILE_BYTES", 32)
    big, small = "x" * 64 + "\n", "small\n"
    _write(old / "shrunk.txt", big)  # only the base side is over the limit
    _write(new / "shrunk.txt", small)
    _write(old / "gone.txt", big)  # deleted, so only the base side exists
    _write(old / "grew.txt", small)  # only the candidate side is over the limit
    _write(new / "grew.txt", big)
    result = sgd.diff_directories(repo, old, new)
    limit = {"reason": "file_over_byte_limit", "byte_limit": 32}
    assert result["skipped"] == [
        {"path": "big/a/gone.txt", **limit},
        {"path": "big/b/grew.txt", **limit},
        {"path": "big/a/shrunk.txt", **limit},
    ]


def test_skip_path_follows_the_side_its_reason_names(tmp_path: Path) -> None:
    # Base oversized and candidate unreadable: "unreadable" is the reason the row
    # reports, so it is the candidate's path that must carry it.
    result = sgd._assemble(
        tmp_path, tmp_path, {"m.txt": sgd.OVERSIZED}, {"m.txt": sgd.UNREADABLE},
        byte_budget=sgd.DEFAULT_BYTE_BUDGET, cursor=None,
        repo_prefixes=("old", "new"), extra={},
    )
    assert result["skipped"] == [{"path": "new/m.txt", "reason": "unreadable"}]


def test_unreadable_file_is_skipped_while_the_rest_still_diffs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, metadata = _candidate(repo)
    locked = (path / "src/pkg/mod.py").resolve()
    real_read_bytes = Path.read_bytes

    def _read_bytes(self: Path) -> bytes:
        if self.resolve() == locked:
            raise PermissionError(13, "locked", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)
    assert sgd._read_side(locked) is sgd.UNREADABLE

    result = sgd.diff_registered_worktree(repo, REQUEST_ID, workspace_metadata=metadata)
    files = {row["path"]: row for row in result["files"]}
    assert files["src/pkg/mod.py"] == {
        "status": "modified", "path": "src/pkg/mod.py", "old_path": "src/pkg/mod.py",
        "unreadable": True,
    }
    assert result["skipped"] == [{"path": "src/pkg/mod.py", "reason": "unreadable"}]
    assert files["src/pkg/new.py"]["status"] == "added"
    assert {hunk["path"] for hunk in result["hunks"]} == {"src/pkg/new.py", "src/pkg/old.py"}
    assert not [row for row in result["symbols"] if row["path"] == "src/pkg/mod.py"]


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").strip()


def test_line_endings_only_change_is_reported_without_hunks(repo: Path) -> None:
    (repo / "src/pkg/crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    _git(repo, "add", "src/pkg/crlf.txt")
    _git(repo, "commit", "-q", "-m", "crlf")
    path = _sparse_worktree(repo)
    assert (path / "src/pkg/crlf.txt").read_bytes() == b"one\r\ntwo\r\n"
    (path / "src/pkg/crlf.txt").write_bytes(b"one\ntwo\n")

    result = sgd.diff_registered_worktree(repo, REQUEST_ID, base_oid=_head(repo))
    assert result["files"] == [{
        "status": "modified", "path": "src/pkg/crlf.txt", "old_path": "src/pkg/crlf.txt",
        "line_endings_only": True,
    }]
    assert result["hunk_total"] == 0 and result["hunks"] == []
    assert result["symbols"] == [] and result["skipped"] == []


def _symlink_or_skip(target: str, link: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable on this host: {exc}")


def test_symlinks_are_described_alike_on_both_sides(repo: Path) -> None:
    _symlink_or_skip("mod.py", repo / "src/pkg/retarget.lnk")
    _symlink_or_skip("old.py", repo / "src/pkg/travel.lnk")
    _git(repo, "add", "src/pkg")
    _git(repo, "commit", "-q", "-m", "links")
    path = _sparse_worktree(repo)
    if not (path / "src/pkg/retarget.lnk").is_symlink():
        pytest.skip("git checks symlinks out as plain files on this host")
    (path / "src/pkg/retarget.lnk").unlink()
    os.symlink("moved.py", path / "src/pkg/retarget.lnk")
    os.replace(path / "src/pkg/travel.lnk", path / "src/pkg/arrived.lnk")

    result = sgd.diff_registered_worktree(repo, REQUEST_ID, base_oid=_head(repo))
    assert result["files"] == [
        {"status": "renamed", "path": "src/pkg/arrived.lnk", "old_path": "src/pkg/travel.lnk"},
        {"status": "modified", "path": "src/pkg/retarget.lnk", "old_path": "src/pkg/retarget.lnk"},
    ]
    assert [hunk["path"] for hunk in result["hunks"]] == ["src/pkg/retarget.lnk"]
    body = result["hunks"][0]["text"].splitlines()[1:]
    assert [line for line in body if line.startswith("-")] == ["-symlink:mod.py"]
    assert [line for line in body if line.startswith("+")] == ["+symlink:moved.py"]


@pytest.mark.parametrize("bad", ["²", "-1", "", "x"])
def test_malformed_cursor_numbers_are_cursor_invalid(repo: Path, bad: str) -> None:
    _path, metadata = _candidate(repo)
    full = sgd.diff_registered_worktree(repo, REQUEST_ID, workspace_metadata=metadata)
    prefix, fingerprint = sgd._CURSOR_PREFIX, full["fingerprint"]
    for cursor in (f"{prefix}:{bad}:0:{fingerprint}", f"{prefix}:0:{bad}:{fingerprint}"):
        with pytest.raises(sgd.SourceGraphDiffError, match="^cursor_invalid$"):
            sgd.diff_registered_worktree(
                repo, REQUEST_ID, workspace_metadata=metadata, cursor=cursor
            )


def test_symbol_extraction_failure_is_marked_without_a_spurious_delta(repo: Path) -> None:
    path, metadata = _candidate(repo)
    _write(path / "src/pkg/mod.py", MOD_AFTER + "\ndef broken(:\n    pass\n")
    result = sgd.diff_registered_worktree(repo, REQUEST_ID, workspace_metadata=metadata)
    files = {row["path"]: row for row in result["files"]}
    marker = files["src/pkg/mod.py"]["symbols_unavailable"]
    assert marker["side"] == "candidate" and marker["reason"]
    assert not [row for row in result["symbols"] if row["path"] == "src/pkg/mod.py"]
    assert "src/pkg/mod.py" in {hunk["path"] for hunk in result["hunks"]}
    # Other files keep their symbol deltas and carry no marker.
    assert ("src/pkg/new.py", "fresh") in {
        (row["path"], row["symbol"]) for row in result["symbols"]
    }
    assert "symbols_unavailable" not in files["src/pkg/new.py"]


def test_added_file_with_a_space_in_its_name_is_added(repo: Path) -> None:
    path, metadata = _candidate(repo)
    _write(path / "src/pkg/my blob", "payload\n")
    result = sgd.diff_registered_worktree(repo, REQUEST_ID, workspace_metadata=metadata)
    files = {row["path"]: row for row in result["files"]}
    assert files["src/pkg/my blob"]["status"] == "added"
    assert files["src/pkg/mod.py"]["status"] == "modified"


def test_fingerprint_tolerates_surrogate_escaped_paths() -> None:
    files = [{"status": "added", "path": "bad\udcff.py", "old_path": "bad\udcff.py"}]
    hunks = [{"path": "bad\udcff.py", "header": "@@", "text": "+x\n"}]
    first = sgd._fingerprint(files, hunks)
    assert first == sgd._fingerprint(files, hunks)
    assert len(first) == 16 and first != sgd._fingerprint(files[:0], hunks)
