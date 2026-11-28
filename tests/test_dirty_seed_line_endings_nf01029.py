"""NF-2026-01029: a line-ending-only seed difference is never dirty-at-seed.

``dirty_canonical_seed.dirty_seeded_paths`` runs ``git status`` in the seeded
worktree.  Git reports a size-changed tracked file as modified without
comparing content, and the NF-2026-01168 restat cannot clear a CRLF-committed
blob, an ambient ``core.autocrlf`` mismatch or a non-ASCII path.  The dirty set
therefore compares each listed-modified seed with its base blob modulo CRLF,
independent of any Git config, and keeps the path dirty on any probe failure.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import dirty_canonical_seed  # noqa: E402
from aiworkhub import worker_workspace  # noqa: E402

CRLF_FILE = "crlf/committed.txt"
LF_FILE = "lf/committed.txt"
UNICODE_FILE = "uni/fiché-ü.txt"
LF_BYTES = b"alpha\nbeta\ngamma\n"
CRLF_BYTES = b"alpha\r\nbeta\r\ngamma\r\n"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "seeded"
    root.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "tests@example.invalid"),
        ("config", "user.name", "Task MCP Tests"),
        ("config", "core.autocrlf", "false"),
        ("config", "core.quotepath", "true"),
        ("config", "commit.gpgsign", "false"),
    ):
        assert _git(root, *args).returncode == 0
    for relative, data in (
        (CRLF_FILE, CRLF_BYTES),
        (LF_FILE, LF_BYTES),
        (UNICODE_FILE, LF_BYTES),
    ):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(data)
    added = _git(root, "--literal-pathspecs", "add", "--", CRLF_FILE, LF_FILE, UNICODE_FILE)
    assert added.returncode == 0, added.stderr
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    return root


def _status_lists(repo: Path, relative: str) -> bool:
    listed = _git(repo, "--literal-pathspecs", "status", "--porcelain=v1", "-z", "--", relative)
    assert listed.returncode == 0, listed.stderr
    return any(len(entry) > 3 for entry in listed.stdout.split(b"\0"))


def _stat_only_listing_run(forced: list[str]) -> Any:
    """Real Git, but ``git status`` also lists ``forced`` as `` M``.

    Reproduces Git's stat-size-only report (the NF-2026-01168 mechanism) for a
    seed whose bytes Git would otherwise match by content in a fresh repo.
    """

    def run(argv: list[str], **kwargs: Any) -> Any:
        result = worker_workspace._run(argv, **kwargs)
        if "status" in argv and result.returncode == 0:
            present = {
                entry[3:] for entry in result.stdout.split(b"\0") if len(entry) > 3
            }
            extra = b"".join(
                b" M " + path.encode("utf-8") + b"\0"
                for path in forced
                if path.encode("utf-8") not in present and path in argv
            )
            result = subprocess.CompletedProcess(
                result.args, result.returncode, result.stdout + extra, result.stderr
            )
        return result

    return run


def test_crlf_committed_blob_seeded_with_same_crlf_bytes_is_not_dirty(repo: Path) -> None:
    (repo / CRLF_FILE).write_bytes(CRLF_BYTES)
    run = _stat_only_listing_run([CRLF_FILE])
    assert CRLF_FILE not in dirty_canonical_seed.dirty_seeded_paths(repo, [CRLF_FILE], run)


def test_lf_committed_seeded_crlf_with_autocrlf_false_is_not_dirty(repo: Path) -> None:
    assert _git(repo, "config", "core.autocrlf").stdout.strip() == b"false"
    (repo / LF_FILE).write_bytes(CRLF_BYTES)
    assert _status_lists(repo, LF_FILE)  # Git itself reports it modified.
    dirty = dirty_canonical_seed.dirty_seeded_paths(repo, [LF_FILE], worker_workspace._run)
    assert LF_FILE not in dirty


def test_non_ascii_tracked_file_seeded_crlf_is_not_dirty(repo: Path) -> None:
    (repo / UNICODE_FILE).write_bytes(CRLF_BYTES)
    assert _status_lists(repo, UNICODE_FILE)
    dirty = dirty_canonical_seed.dirty_seeded_paths(repo, [UNICODE_FILE], worker_workspace._run)
    assert UNICODE_FILE not in dirty


@pytest.mark.parametrize(
    "edited",
    [b"alpha\r\nBETA\r\ngamma\r\n", b"alpha\nBETA\ngamma\n"],
    ids=["crlf_edit", "lf_edit"],
)
def test_real_content_edit_and_untracked_seed_stay_dirty(repo: Path, edited: bytes) -> None:
    untracked = "new/untracked.txt"
    (repo / LF_FILE).write_bytes(edited)
    (repo / CRLF_FILE).write_bytes(edited)
    (repo / untracked).parent.mkdir(parents=True, exist_ok=True)
    (repo / untracked).write_bytes(LF_BYTES)
    paths = [LF_FILE, CRLF_FILE, untracked]
    dirty = dirty_canonical_seed.dirty_seeded_paths(repo, paths, worker_workspace._run)
    assert dirty == tuple(sorted(paths))


def test_many_line_ending_only_seeds_use_one_blob_batch(repo: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return worker_workspace._run(argv, **kwargs)

    for relative in (LF_FILE, UNICODE_FILE):
        (repo / relative).write_bytes(CRLF_BYTES)
    assert dirty_canonical_seed.dirty_seeded_paths(repo, [LF_FILE, UNICODE_FILE], run) == ()
    assert sum(1 for argv in calls if "cat-file" in argv) == 1


@pytest.mark.parametrize("failing", ["ls-files", "cat-file"])
def test_failed_blob_probe_keeps_listed_paths_dirty(repo: Path, failing: str) -> None:
    for relative in (LF_FILE, UNICODE_FILE):
        (repo / relative).write_bytes(CRLF_BYTES)

    def run(argv: list[str], **kwargs: Any) -> Any:
        if failing in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: probe")
        return worker_workspace._run(argv, **kwargs)

    dirty = dirty_canonical_seed.dirty_seeded_paths(repo, [LF_FILE, UNICODE_FILE], run)
    assert dirty == tuple(sorted([LF_FILE, UNICODE_FILE]))


def test_malformed_blob_batch_keeps_listed_paths_dirty(repo: Path) -> None:
    (repo / LF_FILE).write_bytes(CRLF_BYTES)

    def run(argv: list[str], **kwargs: Any) -> Any:
        if "cat-file" in argv:
            return subprocess.CompletedProcess(argv, 0, b"deadbeef missing\n", b"")
        return worker_workspace._run(argv, **kwargs)

    assert dirty_canonical_seed.dirty_seeded_paths(repo, [LF_FILE], run) == (LF_FILE,)
