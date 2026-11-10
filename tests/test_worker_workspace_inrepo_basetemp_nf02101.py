"""NF-2026-02101: in-repo pytest basetemp, and base-red neighbour tests at accept.

Since NF-2026-02004 every temp root lives inside the repository, so a test's
``tmp_path`` sits inside the AIWorkHub checkout -- under ``.aiworkhub/temp`` or
a worker home ``.aiworkhub/runtime/worktrees/<id>/home``.  A test repository
built there, with ``AIWORKHUB_WORKTREE_ROOT`` at a sibling ``tmp_path``
directory, found the checkout as the root's owner and was refused
``worktree_root_foreign_repository``; accept's neighbour revalidation then
failed every candidate whose neighbours included ``test_worker_workspace.py``.

The owner's scratch -- its temp, a worker home, and anything outside the
runtime of an owner that encloses the repository -- is now neutral, and a
neighbour node still red after the serial rerun that also fails on the base
commit is recorded as ``base_red`` instead of refusing the candidate.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import worker_workspace

HUB_ID = "repo_02101000000000000000000000000h"
TEST_REPO_ID = "repo_02101000000000000000000000000t"
WORKER = "d2101aa0d2101aa0d2101aa0d2101aa0"
REFUSAL = worker_workspace.FOREIGN_WORKTREE_ROOT_REFUSAL
IGNORED = worker_workspace.FOREIGN_WORKTREE_ROOT_IGNORED


@pytest.fixture(autouse=True)
def _no_ambient_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        worker_workspace.WORKTREE_ROOT_ENV,
        worker_workspace.RUNTIME_ROOT_ENV,
        "AIWORKHUB_REPO_ROOT",
        "AIWORKHUB_REPO",
    ):
        monkeypatch.delenv(name, raising=False)


def _register(root: Path, repo_id: str | None) -> Path:
    hub = root / ".aiworkhub"
    hub.mkdir(parents=True, exist_ok=True)
    payload = {} if repo_id is None else {"repo_id": repo_id}
    (hub / "project.json").write_text(json.dumps(payload), encoding="utf-8")
    return root.resolve()


# The three in-repo basetemps a validation of this suite actually uses.
BASETEMPS = {
    "aiworkhub_temp": (".aiworkhub", "temp", "b"),
    "worker_home": (".aiworkhub", "runtime", "worktrees", WORKER, "home", "x", "b"),
    "scratch": (".scratch", "b"),
}


def _basetemp(hub: Path, kind: str) -> Path:
    base = hub.joinpath(*BASETEMPS[kind], "test_case0")
    base.mkdir(parents=True)
    return base.resolve()


@pytest.mark.parametrize("identity", [None, TEST_REPO_ID, "unregistered"])
@pytest.mark.parametrize("kind", sorted(BASETEMPS))
def test_sibling_root_of_a_repo_under_an_inrepo_basetemp_is_honored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    identity: str | None,
) -> None:
    hub = _register(tmp_path / "hub", HUB_ID)
    base = _basetemp(hub, kind)
    repo = base / "repo"
    repo.mkdir()
    if identity != "unregistered":
        _register(repo, identity)
    root = (base / "worktrees").resolve(strict=False)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(root))

    assert worker_workspace.configured_worktree_root(repo) == root
    assert not any(IGNORED in record.getMessage() for record in caplog.records)


def test_bare_tmp_path_repository_is_independent_of_where_basetemp_lives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exactly test_worker_workspace's shape, wherever this run's basetemp sits.
    repo = tmp_path / "repo"
    repo.mkdir()
    root = (tmp_path / "worktrees").resolve(strict=False)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(root))

    assert worker_workspace.configured_worktree_root(repo) == root


def test_a_root_inside_the_repo_is_left_to_create_workspaces_own_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A root inside the repository itself stays create_workspace's
    # worktree_root_inside_parent_repo refusal, not a foreign-owner one.
    hub = _register(tmp_path / "hub", HUB_ID)
    repo = _basetemp(hub, "worker_home") / "repo"
    repo.mkdir()
    root = (repo / "unsafe-worktrees").resolve(strict=False)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(root))

    assert worker_workspace.configured_worktree_root(repo) == root


def _git(cwd: Path, *args: str) -> str:
    # core.longpaths as worker_workspace's own Git environment sets it: the
    # nested in-repo layouts below sit deep under this run's own basetemp.
    done = subprocess.run(
        ["git", "-c", "core.longpaths=true", *args],
        cwd=cwd, capture_output=True, text=True, check=False, shell=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _commit(repo: Path, files: Mapping[str, bytes], message: str) -> str:
    for relative, data in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _git(repo, "add", *files)
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _git_repo(root: Path, files: Mapping[str, bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "NF-2026-02101 Tests")
    _commit(root, files, "fixture")
    return root.resolve()


# The same three in-repo basetemps, shortened so a full git checkout inside
# them stays inside Windows MAX_PATH under this run's own (in-repo) basetemp.
SHORT_BASETEMPS = {
    "aiworkhub_temp": (".aiworkhub", "temp"),
    "worker_home": (".aiworkhub", "runtime", "worktrees", "w", "home"),
    "scratch": (".scratch",),
}


@pytest.mark.parametrize("kind", sorted(SHORT_BASETEMPS))
def test_git_workspace_under_inrepo_basetemp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # test_worker_workspace's own shape end to end: its ``repo`` fixture and a
    # sibling worktree root, both under an in-repo basetemp of a registered
    # git checkout -- the layout accept's neighbour revalidation runs it in.
    hub = _register(tmp_path / "h", HUB_ID)
    _git(hub, "init", "-q")
    base = hub.joinpath(*SHORT_BASETEMPS[kind])
    repo = _git_repo(base / "p", {"read/input.txt": b"input-v1\n", "out/result.txt": b"r\n"})
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(base / "wt"))

    workspace = worker_workspace.create_workspace(
        repo, "r1", {"allowed_writes": ["out/result.txt"], "read_first": ["read/input.txt"]},
        "validation",
    )
    try:
        assert workspace.path == (base / "wt" / "r1" / "worktree").resolve()
        assert b"input-v1" in (workspace.path / "read" / "input.txt").read_bytes()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)
    assert not workspace.path.exists()


# --- what stays foreign -------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        ("worktrees",),  # the owner's own worker root
        ("worktrees", WORKER, "worktree", "w"),  # inside a live worker tree, not its home
        ("scratch", "w"),  # anywhere else in the owner's runtime
    ],
)
def test_owner_runtime_outside_a_worker_home_stays_foreign_for_a_nested_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    target: tuple[str, ...],
) -> None:
    hub = _register(tmp_path / "hub", HUB_ID)
    repo = _register(_basetemp(hub, "worker_home") / "repo", TEST_REPO_ID)
    root = (hub / ".aiworkhub" / "runtime").joinpath(*target).resolve(strict=False)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(root))

    own = (repo / ".aiworkhub" / "runtime" / "worktrees").resolve(strict=False)
    assert worker_workspace.configured_worktree_root(repo) == own
    assert any(record.getMessage().startswith(f"{IGNORED}:") for record in caplog.records)


def test_unknown_nested_identity_under_owner_runtime_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = _register(tmp_path / "hub", HUB_ID)
    repo = _basetemp(hub, "worker_home") / "repo"
    repo.mkdir()
    root = (hub / ".aiworkhub" / "runtime" / "worktrees").resolve(strict=False)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(root))

    with pytest.raises(worker_workspace.WorkspaceError, match=f"^{REFUSAL}:"):
        worker_workspace.configured_worktree_root(repo)


@pytest.mark.parametrize("kind", ["worker_home", "scratch"])
def test_a_sibling_repository_gains_nothing_from_another_repos_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # Scratch is neutral only for a repository the owner encloses, and a worker
    # home only for a repository inside that same home.
    hub = _register(tmp_path / "hub", HUB_ID)
    outsider = tmp_path / "outsider"
    outsider.mkdir()
    root = _basetemp(hub, kind) / "worktrees"
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(root))

    with pytest.raises(worker_workspace.WorkspaceError, match=f"^{REFUSAL}:"):
        worker_workspace.configured_worktree_root(outsider)


# Where a repository sits relative to the worker home that holds ``root``.
FOREIGN_TO_THE_HOME = {
    "outside_owner": ("outsider",),
    "other_worker_home": ("hub", ".aiworkhub", "runtime", "worktrees", "other", "home"),
    "owner_temp": ("hub", ".aiworkhub", "temp"),
    "owner_checkout": ("hub", "src"),
}


@pytest.mark.parametrize("identity", [TEST_REPO_ID, "unregistered"])
@pytest.mark.parametrize("where", sorted(FOREIGN_TO_THE_HOME))
def test_a_worker_home_root_is_foreign_for_a_repo_outside_that_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    where: str,
    identity: str,
) -> None:
    # A worker home is neutral only for the repository it holds: any other
    # repository is judged exactly as for any foreign root (NF-2026-01765).
    hub = _register(tmp_path / "hub", HUB_ID)
    root = (_basetemp(hub, "worker_home") / "worktrees").resolve(strict=False)
    repo = tmp_path.joinpath(*FOREIGN_TO_THE_HOME[where], "repo")
    repo.mkdir(parents=True)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(root))

    if identity == "unregistered":
        with pytest.raises(worker_workspace.WorkspaceError, match=f"^{REFUSAL}:"):
            worker_workspace.configured_worktree_root(repo)
        return
    repo = _register(repo, identity)
    own = (repo / ".aiworkhub" / "runtime" / "worktrees").resolve(strict=False)
    assert worker_workspace.configured_worktree_root(repo) == own
    assert any(record.getMessage().startswith(f"{IGNORED}:") for record in caplog.records)


def test_a_repo_inside_the_same_worker_home_keeps_that_homes_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    hub = _register(tmp_path / "hub", HUB_ID)
    home = _basetemp(hub, "worker_home")
    repo = _register(home / "deep" / "repo", TEST_REPO_ID)
    root = (home / "worktrees").resolve(strict=False)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(root))

    assert worker_workspace.configured_worktree_root(repo) == root
    assert not any(IGNORED in record.getMessage() for record in caplog.records)


# --- neighbour revalidation: base-red nodes do not fail the candidate ---------

TEST_FILE = "tests/test_neighbour_x.py"
NODE_A = f"{TEST_FILE}::test_a"
NODE_B = f"{TEST_FILE}::test_b"
CARD: dict[str, Any] = {
    "task_id": "nf02101",
    "validation": ["python -m pytest -q x"],
    "allowed_writes": [],
}


def _rows(failed: tuple[str, ...]) -> list[dict[str, Any]]:
    text = "\n".join(f"FAILED {node} - AssertionError" for node in failed)
    return [{"returncode": 1 if failed else 0, "stdout_tail": text or "1 passed"}]


class _Runner:
    """Candidate fails ``candidate``; the base fails ``base``; calls are recorded."""

    def __init__(self, candidate: tuple[str, ...], base: tuple[str, ...]) -> None:
        self.candidate, self.base = candidate, base
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, workspace: Any, authority: Mapping[str, Any], route: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        command = authority["validation"][0]
        self.calls.append((str(workspace), command))
        return _rows(self.base if workspace == "base" else self.candidate)


def _base_factory(log: list[Any], *, fail: bool = False):
    @contextmanager
    def factory(workspace: Any, card: Mapping[str, Any]) -> Iterator[str]:
        log.append(workspace)
        if fail:
            raise worker_workspace.WorkspaceError("base_unbuildable")
        yield "base"

    return factory


def _run(runner: _Runner, base_log: list[Any], **kwargs: Any) -> dict[str, Any]:
    selection = neighbours.NeighbourSelection(tests=(TEST_FILE,), reason="", limit=12)
    return neighbours.run_neighbour_tests(
        selection,
        CARD,
        run=runner,
        workspace="union",
        route_metadata={},
        base_workspace=_base_factory(base_log, **kwargs),
    )


def test_a_node_that_also_fails_on_the_base_is_base_red_not_a_refusal() -> None:
    runner, base_log = _Runner((NODE_A,), (NODE_A, NODE_B)), []

    evidence = _run(runner, base_log)

    assert base_log == ["union"]
    assert evidence["base_red"]["nodes"] == [NODE_A]
    assert evidence["base_red"]["command"] == neighbours.neighbour_command([NODE_A], serial=True)
    assert evidence["neighbour_rerun"]["nodes"] == [NODE_A]
    assert [workspace for workspace, _command in runner.calls] == ["union", "union", "base"]


def _base_red_refused(refused: pytest.ExceptionInfo[worker_workspace.WorkspaceError]) -> str:
    text = str(refused.value)
    assert text.startswith(neighbours.NEIGHBOUR_FAILURE_PREFIX)
    return str(json.loads(text[len(neighbours.NEIGHBOUR_FAILURE_PREFIX) :])["base_red_refused"])


@pytest.mark.parametrize(
    ("base", "green"), [((), (NODE_A, NODE_B)), ((NODE_B,), (NODE_A,))]
)
def test_a_node_green_on_the_base_still_refuses(
    base: tuple[str, ...], green: tuple[str, ...]
) -> None:
    runner, base_log = _Runner((NODE_A, NODE_B), base), []

    with pytest.raises(worker_workspace.WorkspaceError) as refused:
        _run(runner, base_log)

    assert NODE_A in str(refused.value)
    # The refusal names the nodes the base did not fail, not just "failed".
    assert _base_red_refused(refused) == "base_green:" + ",".join(green)


def test_an_unbuildable_base_refuses() -> None:
    runner, base_log = _Runner((NODE_A,), (NODE_A,)), []

    with pytest.raises(worker_workspace.WorkspaceError) as refused:
        _run(runner, base_log, fail=True)

    assert base_log == ["union"]
    assert _base_red_refused(refused) == "base_unavailable:WorkspaceError:base_unbuildable"


def test_a_base_refusal_that_is_not_a_test_verdict_refuses() -> None:
    def runner(workspace: Any, authority: Mapping[str, Any], route: Mapping[str, Any]):
        if workspace == "base":
            raise worker_workspace.WorkspaceError("validation_sandbox_unavailable")
        return _rows((NODE_A,))

    with pytest.raises(worker_workspace.WorkspaceError) as refused:
        _run(runner, [])  # type: ignore[arg-type]

    assert _base_red_refused(refused) == (
        "base_not_a_test_verdict:validation_sandbox_unavailable"
    )


def test_the_default_base_needs_a_canonical_head(tmp_path: Path) -> None:
    runner = _Runner((NODE_A,), (NODE_A,))
    selection = neighbours.NeighbourSelection(tests=(TEST_FILE,), reason="", limit=12)

    with pytest.raises(worker_workspace.WorkspaceError) as refused:
        neighbours.run_neighbour_tests(
            selection, CARD, run=runner, workspace="union", route_metadata={}
        )

    assert [workspace for workspace, _command in runner.calls] == ["union", "union"]
    assert _base_red_refused(refused) == (
        "base_unavailable:WorkspaceError:neighbour_canonical_head_unavailable"
    )
    # A worker base_oid is no substitute for the repository's HEAD.
    no_repo = SimpleNamespace(base_oid="a" * 40)
    with pytest.raises(
        worker_workspace.WorkspaceError, match="^neighbour_canonical_head_unavailable$"
    ):
        with neighbours.base_commit_workspace(no_repo, CARD):
            pass
    # A repository without a resolvable HEAD (no commit yet) refuses too.
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "-q")
    with pytest.raises(worker_workspace.WorkspaceError):
        with neighbours.base_commit_workspace(SimpleNamespace(repo=unborn), CARD):
            pass


def _moved_canonical_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SimpleNamespace, str, str, list[str | None]]:
    """A worker workspace on ``old`` whose repository's HEAD has moved to ``head``.

    Provisioning is faked: the base checkout records the oid it was pinned to.
    """
    repo = _git_repo(tmp_path / "r", {"a.txt": b"broken\n"})
    old = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"a.txt": b"fixed\n"}, "canonical fix")
    pinned: list[str | None] = []

    @dataclass(frozen=True)
    class _FakeWorkspace:
        repo: Path
        path: Path
        home: Path
        base_oid: str | None
        allowed_writes: tuple[str, ...] = ("x",)

    def create(
        repo_path: Path, request_id: str, card: Mapping[str, Any], role: str,
        *, pinned_base_oid: str | None = None,
    ) -> _FakeWorkspace:
        assert card["allowed_writes"] == [] and role == "validation"
        pinned.append(pinned_base_oid)
        return _FakeWorkspace(
            repo=Path(repo_path), path=tmp_path / request_id, home=tmp_path / "home",
            base_oid=pinned_base_oid,
        )

    monkeypatch.setattr(worker_workspace, "create_workspace", create)
    monkeypatch.setattr(worker_workspace, "_run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(worker_workspace, "cleanup_workspace", lambda *a, **k: None)
    return SimpleNamespace(repo=repo, base_oid=old), old, head, pinned


def _red_at(union: SimpleNamespace, red_oid: str):
    """The candidate fails NODE_A; a base fails it only when checked out at ``red_oid``."""

    def run(
        workspace: Any, authority: Mapping[str, Any], route: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        failed = workspace is union or workspace.base_oid == red_oid
        return _rows((NODE_A,) if failed else ())

    return run


def test_a_node_canonical_fixed_and_the_candidate_breaks_again_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Red at the worker's original base, green at canonical HEAD: the candidate
    # re-broke what canonical fixed, so this is not base_red.
    union, old, head, pinned = _moved_canonical_head(tmp_path, monkeypatch)
    selection = neighbours.NeighbourSelection(tests=(TEST_FILE,), reason="", limit=12)

    with pytest.raises(worker_workspace.WorkspaceError) as refused:
        neighbours.run_neighbour_tests(
            selection, CARD, run=_red_at(union, old), workspace=union, route_metadata={}
        )

    assert pinned == [head]
    assert _base_red_refused(refused) == f"base_green:{NODE_A}"


def test_base_red_records_the_canonical_head_it_ran_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    union, _old, head, pinned = _moved_canonical_head(tmp_path, monkeypatch)
    selection = neighbours.NeighbourSelection(tests=(TEST_FILE,), reason="", limit=12)

    evidence = neighbours.run_neighbour_tests(
        selection, CARD, run=_red_at(union, head), workspace=union, route_metadata={}
    )

    assert pinned == [head]
    assert evidence["base_red"]["nodes"] == [NODE_A]
    assert evidence["base_red"]["base_oid"] == head
    assert evidence["base_red"]["base_source"] == "canonical_head"


def test_base_commit_workspace_is_the_full_base_tree_and_is_removed(tmp_path: Path) -> None:
    # The default base: a real, non-sparse checkout of the repository's canonical
    # HEAD -- not the union's older worker base_oid -- removed when judged.
    repo = _git_repo(
        tmp_path / "repo",
        {"src/pkg/mod.py": b"VALUE = 1\n", "tests/test_mod.py": b"def test_x():\n    pass\n"},
    )
    worker_base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"src/pkg/mod.py": b"VALUE = 2\n", "docs/new.md": b"new\n"}, "canonical")
    union = SimpleNamespace(repo=repo, base_oid=worker_base)

    card = {"task_id": "nf02101", "validation": [], "allowed_writes": []}
    with neighbours.base_commit_workspace(union, card) as base:
        path = base.path
        assert _git(path, "rev-parse", "HEAD") == head == base.base_oid
        assert b"VALUE = 2" in (path / "src" / "pkg" / "mod.py").read_bytes()
        assert (path / "tests" / "test_mod.py").is_file()
        assert (path / "docs" / "new.md").is_file()

    assert not path.exists()


def test_base_commit_workspace_enters_for_finalization_metadata_without_allowed_writes(tmp_path: Path) -> None:
    # At finalization, card is route metadata: no allowed_writes key, possibly
    # required_outputs. create_workspace must not raise allowed_writes_missing;
    # base_card inherits allowed_writes from the workspace/union instead.
    repo = _git_repo(
        tmp_path / "repo",
        {"src/pkg/mod.py": b"VALUE = 1\n", "tests/test_mod.py": b"def test_x():\n    pass\n"},
    )
    worker_base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"src/pkg/mod.py": b"VALUE = 2\n"}, "canonical")

    union = SimpleNamespace(
        repo=repo, base_oid=worker_base, allowed_writes=("tests/test_new.py",)
    )
    card = {
        "task_id": "nf02101",
        "validation": [],
        "required_outputs": ["tests/test_new.py"],
    }
    with neighbours.base_commit_workspace(union, card) as base:
        assert base.allowed_writes == ()

    union_no_attr = SimpleNamespace(repo=repo, base_oid=worker_base)
    with neighbours.base_commit_workspace(union_no_attr, card) as base:
        assert base.allowed_writes == ()


def test_base_commit_workspace_enters_for_a_card_adding_its_own_test(tmp_path: Path) -> None:
    # A card whose validation names a test it adds itself (absent on HEAD, listed in
    # allowed_writes) must still build the base; the base itself stays write-free.
    repo = _git_repo(tmp_path / "repo", {"src/pkg/mod.py": b"VALUE = 1\n"})
    union = SimpleNamespace(repo=repo, base_oid=_git(repo, "rev-parse", "HEAD"))
    new_test = "tests/test_new_nf02185.py"
    card = {
        "task_id": "nf02185",
        "validation": [f"python -m pytest -q {new_test}"],
        "allowed_writes": [new_test],
    }

    with neighbours.base_commit_workspace(union, card) as base:
        assert base.allowed_writes == ()
        assert not (base.path / new_test).exists()


def test_finalization_records_base_red_as_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, base_log = _Runner((NODE_A,), (NODE_A,)), []
    selection = neighbours.NeighbourSelection(tests=(TEST_FILE,), reason="", limit=12)
    monkeypatch.setattr(neighbours, "select_neighbour_tests", lambda *_a, **_k: selection)
    monkeypatch.setattr(neighbours, "base_commit_workspace", _base_factory(base_log))

    record = neighbours.run_finalization_neighbours(
        ".", ["src/aiworkhub/x.py"], CARD, [], run=runner, workspace="union", route_metadata={}
    )

    assert record is not None and record["passed"] is True
    assert record["base_red"]["nodes"] == [NODE_A]
