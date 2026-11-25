"""scripts/release_cut.py: a cut prunes the host basetemps earlier cuts left (NF-2026-02429).

Each cut leaves its host suite's basetemps, b and br for the serial rerun, under
.scratch/release/VERSION, and nothing deleted them: 0.13.10/b alone held 5.0 GB. A cut
now removes every other version's on a daemon thread while its host suite runs
(BasetempPrune) and keeps its own. Every test here builds a fake release scratch tree
under tmp_path and runs only the prune: no cut, git, npm or WSL runs, and a failing
removal is an injected rmtree, so no test needs a read-only file.
"""

from __future__ import annotations

import errno
import importlib.util
import os
import shutil
import stat
import sys
import threading
from pathlib import Path
from typing import Callable

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_cut.py"
SPEC = importlib.util.spec_from_file_location("aiworkhub_release_cut_nf02429", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_cut = importlib.util.module_from_spec(SPEC)
# Its dataclasses resolve their string annotations through sys.modules.
sys.modules[SPEC.name] = release_cut
SPEC.loader.exec_module(release_cut)

RELEASE = ".scratch/release"
EARLIER = "0.1.0"
CURRENT = "0.1.1"
PRUNED = [f"{RELEASE}/{EARLIER}/b", f"{RELEASE}/{EARLIER}/br"]
# Every file of the fake tree that is no earlier basetemp's, which every prune keeps.
KEPT = {
    f"{RELEASE}/{EARLIER}/cut.log": "earlier cut",
    f"{RELEASE}/{EARLIER}/head.tar": "earlier archive",
    f"{RELEASE}/{EARLIER}/host_tests.log": "earlier suite",
    f"{RELEASE}/{EARLIER}/wsl_baseline.txt": "tests/test_x.py::test_y\n",
    f"{RELEASE}/{CURRENT}/b/popen-gw0/test_two0/out.txt": "this cut's run",
}
# A bound no passing test comes near: only a broken prune runs a wait out.
WAIT_SECONDS = 30.0
Rmtree = Callable[..., None]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _release_tree(root: Path) -> Path:
    """0.1.0 as an earlier cut left it, its b, br, cut.log and more, and 0.1.1, being cut."""

    release = root / RELEASE
    _write(release / EARLIER / "b" / "popen-gw0" / "test_one0" / "out.txt", "earlier run")
    _write(release / EARLIER / "br" / "test_one0" / "out.txt", "earlier rerun")
    for relative, text in KEPT.items():
        _write(root / relative, text)
    return release


def _files(root: Path) -> dict[str, str]:
    """Every file under the release scratch tree, by its POSIX path under ``root``."""

    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in (root / RELEASE).rglob("*")
        if path.is_file()
    }


def test_the_prune_removes_only_the_basetemps_of_other_versions(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)
    # A file named like a basetemp is none, and a version directory without one stays.
    _write(release / "0.0.9" / "b", "a file named b")
    (release / "0.0.8").mkdir()

    summary = release_cut.BasetempPrune(tmp_path, CURRENT).run()

    assert summary == {"state": "done", "removed": PRUNED, "count": 2, "errors": []}
    assert not (release / EARLIER / "b").exists()
    assert not (release / EARLIER / "br").exists()
    # The earlier cut's logs, archive and baseline stay byte for byte, and so does this
    # cut's own basetemp, which the diagnosis of its failure reads.
    assert _files(tmp_path) == {**KEPT, f"{RELEASE}/0.0.9/b": "a file named b"}
    assert (release / "0.0.8").is_dir()


def test_a_repository_without_release_scratch_prunes_nothing_and_creates_nothing(
    tmp_path: Path,
) -> None:
    summary = release_cut.BasetempPrune(tmp_path, CURRENT).run()

    assert summary == {"state": "done", "removed": [], "count": 0, "errors": []}
    assert list(tmp_path.iterdir()) == []


def _raises(path: str, *, onexc: Rmtree) -> None:
    raise PermissionError(errno.EACCES, "Access is denied", path)


def _hands_the_tree_to_onexc(path: str, *, onexc: Rmtree) -> None:
    # What shutil.rmtree does with what it cannot remove: it hands each failure to onexc
    # and goes on, and the tree stays. No PermissionError, so no read-only bit is touched.
    onexc(os.rmdir, path, OSError(errno.EBUSY, "Device or resource busy", path))


def _hands_an_entry_to_onexc(path: str, *, onexc: Rmtree) -> None:
    entry = os.path.join(path, "popen-gw0", "test_one0", "out.txt")
    onexc(os.unlink, entry, OSError(errno.EBUSY, "Device or resource busy", entry))
    onexc(os.rmdir, path, OSError(errno.ENOTEMPTY, "Directory not empty", path))


@pytest.mark.parametrize(
    ("failing", "why"),
    [
        (_raises, "PermissionError: Access is denied"),
        (
            _hands_the_tree_to_onexc,
            "1 entry not removed (first: OSError: Device or resource busy)",
        ),
        (
            _hands_an_entry_to_onexc,
            "2 entries not removed "
            "(first: popen-gw0/test_one0/out.txt: OSError: Device or resource busy)",
        ),
    ],
    ids=["rmtree_raises", "onexc_gets_the_tree", "onexc_gets_an_entry"],
)
def test_a_failing_rmtree_is_reported_never_raised_and_stops_no_other_removal(
    tmp_path: Path, failing: Rmtree, why: str
) -> None:
    release = _release_tree(tmp_path)
    basetemp = release / EARLIER / "b"

    def rmtree(path: str, *, onexc: Rmtree) -> None:
        # Only b fails; br, after it, goes through the real shutil.rmtree.
        (failing if Path(path) == basetemp else shutil.rmtree)(path, onexc=onexc)

    summary = release_cut.BasetempPrune(tmp_path, CURRENT, rmtree=rmtree).run()

    assert summary == {
        "state": "done",
        "removed": [f"{RELEASE}/{EARLIER}/br"],
        "count": 1,
        "errors": [f"{RELEASE}/{EARLIER}/b: {why}"],
    }
    assert not (release / EARLIER / "br").exists()
    assert _files(tmp_path) == {
        **KEPT,
        f"{RELEASE}/{EARLIER}/b/popen-gw0/test_one0/out.txt": "earlier run",
    }


def test_an_entry_gone_before_rmtree_reached_it_is_no_failure(tmp_path: Path) -> None:
    _release_tree(tmp_path)

    def rmtree(path: str, *, onexc: Rmtree) -> None:
        gone = os.path.join(path, "gone.txt")
        onexc(os.unlink, gone, FileNotFoundError(errno.ENOENT, "No such file", gone))
        shutil.rmtree(path, onexc=onexc)

    summary = release_cut.BasetempPrune(tmp_path, CURRENT, rmtree=rmtree).run()

    assert summary == {"state": "done", "removed": PRUNED, "count": 2, "errors": []}
    assert _files(tmp_path) == KEPT


def test_a_prune_never_started_reports_not_run_and_removes_nothing(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)

    summary = release_cut.BasetempPrune(tmp_path, CURRENT).join(0.0)

    assert summary == {"state": "not_run", "removed": [], "count": 0, "errors": []}
    assert (release / EARLIER / "b").is_dir() and (release / EARLIER / "br").is_dir()


def test_the_prune_runs_on_a_daemon_thread_and_a_join_past_its_bound_reports_it_running(
    tmp_path: Path,
) -> None:
    _release_tree(tmp_path)
    entered, released = threading.Event(), threading.Event()

    def held(path: str, *, onexc: Rmtree) -> None:
        entered.set()
        if not released.wait(WAIT_SECONDS):
            raise TimeoutError("the test never released the prune")
        shutil.rmtree(path, onexc=onexc)

    prune = release_cut.BasetempPrune(tmp_path, CURRENT, rmtree=held).start()
    try:
        assert prune.thread is not None and prune.thread.daemon
        assert entered.wait(WAIT_SECONDS), "the prune thread never reached rmtree"
        # The caller goes on while the prune works, and a join past its bound says so.
        running = prune.join(0.05)
        assert running == {"state": "running", "removed": [], "count": 0, "errors": []}
    finally:
        released.set()
    done = prune.join(WAIT_SECONDS)
    assert done == {"state": "done", "removed": PRUNED, "count": 2, "errors": []}
    assert not prune.thread.is_alive()
    assert _files(tmp_path) == KEPT


def _directory_link(link: Path, target: Path) -> None:
    """A link to a directory that needs no privilege: a junction on Windows, else a symlink."""

    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_the_prune_never_follows_a_link_out_of_the_release_scratch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    release = _release_tree(root)
    outside = tmp_path / "outside"
    _write(outside / "b" / "kept.txt", "outside")
    _write(outside / "elsewhere" / "kept.txt", "elsewhere")
    # A version directory that is a link, and a b that is one in a real version directory.
    _directory_link(release / "0.0.8", outside)
    (release / "0.0.9").mkdir()
    _directory_link(release / "0.0.9" / "b", outside / "elsewhere")

    summary = release_cut.BasetempPrune(root, CURRENT).run()

    assert summary == {"state": "done", "removed": PRUNED, "count": 2, "errors": []}
    assert (outside / "b" / "kept.txt").read_text(encoding="utf-8") == "outside"
    assert (outside / "elsewhere" / "kept.txt").read_text(encoding="utf-8") == "elsewhere"
    assert os.path.lexists(release / "0.0.8") and os.path.lexists(release / "0.0.9" / "b")


@pytest.mark.parametrize("linked", [".scratch", RELEASE], ids=["scratch", "release"])
def test_the_prune_never_follows_a_linked_release_scratch_out_of_the_repository(
    tmp_path: Path, linked: str
) -> None:
    outside = tmp_path / "outside"
    _release_tree(outside)
    before = _files(outside)
    root = tmp_path / "repo"
    (root / linked).parent.mkdir(parents=True)
    _directory_link(root / linked, outside / linked)

    summary = release_cut.BasetempPrune(root, CURRENT).run()

    assert summary == {"state": "done", "removed": [], "count": 0, "errors": []}
    assert _files(outside) == before


def test_a_permission_refusal_is_retried_with_the_read_only_bits_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basetemp = tmp_path / "b"
    locked = basetemp / "objects" / "ab"
    _write(locked, "a git object")
    chmods: list[tuple[str, int]] = []
    chmod = os.chmod

    def recording_chmod(path: str, mode: int) -> None:
        chmods.append((os.fspath(path), mode))
        chmod(path, mode)

    monkeypatch.setattr(os, "chmod", recording_chmod)

    def rmtree(path: str, *, onexc: Rmtree) -> None:
        # How Windows refuses a read-only file, as git makes its objects; no file here is.
        entry = os.fspath(locked)
        onexc(os.unlink, entry, PermissionError(errno.EACCES, "Access is denied", entry))
        shutil.rmtree(path, onexc=onexc)

    assert release_cut._remove_tree(basetemp, rmtree) is None
    assert not basetemp.exists()
    # The entry and its directory were made writable before the one retry unlinked it.
    assert (os.fspath(locked), stat.S_IRWXU) in chmods
    assert (os.fspath(locked.parent), stat.S_IRWXU) in chmods
