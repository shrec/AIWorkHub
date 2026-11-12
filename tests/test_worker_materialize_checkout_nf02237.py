"""NF-2026-02237: card ``materialize`` globs and ``checkout`` mode.

Worker, validation and reviewer-reproduction trees share one rule
(``aiworkhub.worker_materialize``): extra committed files may be present on
disk, writes stay limited to ``allowed_writes``, forbidden paths are never
materialized, and a card using neither option keeps the exact legacy sparse
pattern list.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    accept_neighbour_tests,
    core,
    finding_reproduction,
    task_store,
    worker_materialize,
    worker_workspace,
)
from aiworkhub.worker_materialize import (  # noqa: E402
    EMPTY_SPARSE_PATTERN,
    MAX_MATERIALIZE,
    CardCheckoutError,
    CheckoutPolicy,
)

FILES = {
    "src/app.py": "VALUE = 1\n",
    "configs/a.json": '{"a": 1}\n',
    "configs/b.json": '{"b": 2}\n',
    "configs/secret.json": '{"token": "x"}\n',
    "data/model.bin": "weights\n",
    "README.md": "readme\n",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _repo(tmp_path: Path, *, project_config: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "tester")
    files = dict(FILES)
    files[".gitignore"] = ".aiworkhub/runtime/\n"
    if project_config is not None:
        files[".aiworkhub/config/project.json"] = json.dumps(project_config)
    for relative, text in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _card(**extra) -> dict:
    return {
        "allowed_writes": ["src/app.py"],
        "required_outputs": [],
        "read_first": [],
        "immutable_inputs": [],
        **extra,
    }


@pytest.fixture
def workspaces():
    created: list = []
    yield created
    for workspace in created:
        worker_workspace.cleanup_workspace(workspace.repo, workspace.path, workspace.home)


def _create(repo: Path, request_id: str, card: dict, created: list):
    workspace = worker_workspace.create_workspace(repo, request_id, card, "validation")
    created.append(workspace)
    return workspace


def _sparse_file(path: Path) -> list[str]:
    location = _git(path, "rev-parse", "--git-path", "info/sparse-checkout").strip()
    target = Path(location)
    if not target.is_absolute():
        target = path / target
    return target.read_text(encoding="utf-8").splitlines()


def _present(path: Path, relative: str) -> bool:
    return (path / relative).exists()


# --- field validation -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (["/etc/passwd"], "absolute"),
        (["C:/data"], "absolute"),
        (["../outside"], "dot_or_empty_component"),
        (["configs/../../x"], "dot_or_empty_component"),
        ([".git/config"], "git_metadata"),
        (["sub/.GIT/hooks"], "git_metadata"),
        (["!configs"], "negation"),
        (["configs\\a.json"], "backslash"),
        ([""], "empty"),
        ([3], "not_a_string"),
        ("configs/**", "not_a_list"),
        (["x"] * (MAX_MATERIALIZE + 1), "too_many"),
    ],
)
def test_invalid_materialize_entries_are_refused_with_typed_errors(raw, reason):
    with pytest.raises(CardCheckoutError) as caught:
        worker_materialize.validate_materialize(raw)
    assert caught.value.field == "materialize"
    assert caught.value.reason == reason
    assert str(caught.value).startswith(f"materialize_invalid:{reason}:")
    assert isinstance(caught.value, ValueError)


def test_valid_materialize_and_checkout_normalize():
    assert worker_materialize.validate_materialize(None) == []
    assert worker_materialize.validate_materialize(
        ["configs/**", "models/", "configs/**"]
    ) == ["configs/**", "models"]
    assert worker_materialize.validate_checkout(None) == "sparse"
    assert worker_materialize.validate_checkout("FULL") == "full"
    with pytest.raises(CardCheckoutError) as caught:
        worker_materialize.validate_checkout("partial")
    assert str(caught.value).startswith("checkout_invalid:unknown_value:partial")


def test_only_non_default_fields_are_persisted():
    assert worker_materialize.card_checkout_fields([], "sparse") == {}
    assert worker_materialize.card_checkout_fields(["configs/**"], "full") == {
        "materialize": ["configs/**"],
        "checkout": "full",
    }


# --- task_create ------------------------------------------------------------


@pytest.fixture
def _task_repo(tmp_path, monkeypatch):
    repo = tmp_path / "taskrepo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    # Manager identity, write gate and runner routing are not under test: admit
    # them so the create reaches the field validation this card adds.
    monkeypatch.setattr(core, "_live_manager_identity", lambda: {"manager": "test"})
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *_a, **_k: None)
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(core, "resolve_create_time_runner", lambda runner: (runner, None))
    return repo


def _create_task(**extra):
    return core.create_task(
        task_id="NF02237_CARD",
        title="materialize card",
        runner="codex_a",
        topic="coding",
        objective="exercise materialize validation",
        acceptance=["refused"],
        allowed_writes=["src/app.py"],
        **extra,
    )


@pytest.mark.parametrize(
    ("extra", "prefix"),
    [
        ({"materialize": ["/abs/configs"]}, "materialize_invalid:absolute:"),
        ({"materialize": ["../configs"]}, "materialize_invalid:dot_or_empty_component:"),
        ({"materialize": [".git/objects"]}, "materialize_invalid:git_metadata:"),
        ({"materialize": ["c"] * 65}, "materialize_invalid:too_many:"),
        ({"checkout": "shallow"}, "checkout_invalid:unknown_value:"),
    ],
)
def test_task_create_refuses_invalid_fields(_task_repo, extra, prefix):
    result = _create_task(**extra)
    assert result["ok"] is False, result
    assert result["stderr"].startswith(prefix), result["stderr"][:300]
    assert result["returncode"] == 2
    assert result["stderr"].startswith(prefix)


# --- legacy byte-identical sparse definition ---------------------------------


def _legacy_patterns(seeded: list[str], allowed: tuple[str, ...]) -> list[str]:
    """The pre-NF-2026-02237 algorithm, restated verbatim for comparison."""
    patterns = sorted({worker_workspace._sparse_checkout_pattern(r) for r in seeded})
    patterns.extend(f"/{relative}" for relative in allowed)
    patterns = sorted(set(patterns))
    if not patterns:
        patterns = ["/__AIWORKHUB_EMPTY_SPARSE_WORKTREE__"]
    return patterns


@pytest.mark.parametrize(
    ("seeded", "allowed"),
    [
        (["src/a b.py", "lib/x*.py", "pkg/[x].py"], ("out/*.json", "src/a b.py")),
        ([], ()),
        ([], ("only/**",)),
    ],
)
def test_card_without_new_fields_keeps_the_exact_sparse_stdin(
    tmp_path, monkeypatch, seeded, allowed
):
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("input_text")))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worker_workspace, "_run", fake_run)
    policy = worker_materialize.checkout_policy(_card(forbidden=["src/x.py"]), tmp_path)
    assert policy == CheckoutPolicy()
    worker_workspace._prepare_sparse_worktree(
        tmp_path, list(seeded), tuple(allowed), timeout=1, policy=policy
    )
    expected = "\n".join(_legacy_patterns(list(seeded), tuple(allowed))) + "\n"
    assert [argv[1:3] for argv, _ in calls] == [
        ["sparse-checkout", "init"],
        ["sparse-checkout", "set"],
        ["read-tree", "-mu"],
    ]
    assert calls[1][1] == expected
    assert CheckoutPolicy().sparse_patterns([]) == [EMPTY_SPARSE_PATTERN]


def test_legacy_full_tree_expansion_is_exactly_sparse_checkout_disable(tmp_path):
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    worker_materialize.expand_full_tree(
        tmp_path, _card(forbidden=["configs/secret.json"]), tmp_path,
        run=fake_run, timeout=5, failure=RuntimeError,
    )
    assert calls == [
        (["git", "sparse-checkout", "disable"],
         {"cwd": tmp_path, "timeout": 5, "phase": "workspace_provision"}),
    ]


def test_opted_in_patterns_put_forbidden_negatives_last():
    policy = CheckoutPolicy(("configs/**",), "sparse", ("configs/secret.json",))
    assert policy.sparse_patterns(["/src/app.py"]) == [
        "/configs/**",
        "/src/app.py",
        "!/configs/secret.json",
    ]
    full = CheckoutPolicy((), "full", ("data",))
    assert full.sparse_patterns(["/src/app.py"]) == ["/*", "!/data"]


# --- real worker worktrees --------------------------------------------------


def test_sparse_default_leaves_configs_absent(tmp_path, workspaces):
    repo = _repo(tmp_path)
    workspace = _create(repo, "req-nf02237-default", _card(), workspaces)
    assert _present(workspace.path, "src/app.py")
    for relative in ("configs/a.json", "data/model.bin", "README.md"):
        assert not _present(workspace.path, relative)
    lines = _sparse_file(workspace.path)
    assert lines == sorted(set(lines))
    assert not any(line.startswith("!") or line.startswith("/configs") for line in lines)


def test_materialize_glob_puts_configs_on_disk_but_writes_stay_scoped(tmp_path, workspaces):
    repo = _repo(tmp_path)
    workspace = _create(
        repo, "req-nf02237-mat", _card(materialize=["configs/**"]), workspaces
    )
    assert (workspace.path / "configs/a.json").read_text(encoding="utf-8") == FILES[
        "configs/a.json"
    ]
    assert _present(workspace.path, "configs/secret.json")
    assert not _present(workspace.path, "data/model.bin")
    assert worker_workspace.changed_paths(workspace) == []
    (workspace.path / "configs/a.json").write_text("{}", encoding="utf-8")
    with pytest.raises(worker_workspace.WorkspaceError, match="scope_violation:configs/a.json"):
        worker_workspace.enforce_scope(workspace)


def test_forbidden_is_absent_even_when_a_materialize_glob_covers_it(tmp_path, workspaces):
    repo = _repo(tmp_path)
    card = _card(materialize=["configs/**"], forbidden=["configs/secret.json"])
    workspace = _create(repo, "req-nf02237-forb", card, workspaces)
    assert _present(workspace.path, "configs/a.json")
    assert not _present(workspace.path, "configs/secret.json")
    assert _sparse_file(workspace.path)[-1] == "!/configs/secret.json"
    # The excluded file is skip-worktree, never reported as a deletion.
    assert worker_workspace.changed_paths(workspace) == []


def test_checkout_full_materializes_everything_and_scope_still_rejects(tmp_path, workspaces):
    repo = _repo(tmp_path)
    workspace = _create(repo, "req-nf02237-full", _card(checkout="full"), workspaces)
    for relative in FILES:
        assert _present(workspace.path, relative), relative
    assert worker_workspace.changed_paths(workspace) == []
    (workspace.path / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert worker_workspace.enforce_scope(workspace) == ["src/app.py"]
    (workspace.path / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(worker_workspace.WorkspaceError, match="scope_violation:README.md"):
        worker_workspace.enforce_scope(workspace)


def test_checkout_full_removes_forbidden_paths(tmp_path, workspaces):
    repo = _repo(tmp_path)
    card = _card(checkout="full", forbidden=["configs/secret.json", "data/", "never mind prose"])
    workspace = _create(repo, "req-nf02237-fullf", card, workspaces)
    assert _present(workspace.path, "configs/a.json")
    assert _present(workspace.path, "README.md")
    assert not _present(workspace.path, "configs/secret.json")
    assert not _present(workspace.path, "data/model.bin")
    assert worker_workspace.changed_paths(workspace) == []


def test_forbidden_removal_failure_fails_closed(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "secret.json").write_text("x", encoding="utf-8")
    policy = CheckoutPolicy(("configs/**",), "sparse", ("configs/secret.json",))

    def refuse(self, *args, **kwargs):
        raise PermissionError("denied")

    original = Path.unlink
    Path.unlink = refuse  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="forbidden_removal_failed:configs/secret.json"):
            policy.remove_forbidden(tmp_path, failure=RuntimeError)
    finally:
        Path.unlink = original  # type: ignore[method-assign]
    assert policy.remove_forbidden(tmp_path, failure=RuntimeError) == ["configs/secret.json"]


def test_repo_default_materialize_from_project_config(tmp_path, workspaces):
    repo = _repo(tmp_path, project_config={"worker_materialize": ["configs/a.json"]})
    assert worker_materialize.repo_default_materialize(repo) == ["configs/a.json"]
    workspace = _create(repo, "req-nf02237-repo", _card(), workspaces)
    assert _present(workspace.path, "configs/a.json")
    assert not _present(workspace.path, "configs/b.json")


def test_repo_default_absent_or_malformed(tmp_path):
    assert worker_materialize.repo_default_materialize(tmp_path) == []
    config = tmp_path / ".aiworkhub" / "config" / "project.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    assert worker_materialize.repo_default_materialize(tmp_path) == []
    config.write_text("{not json", encoding="utf-8")
    with pytest.raises(CardCheckoutError, match="worker_materialize_invalid:config_invalid_json"):
        worker_materialize.repo_default_materialize(tmp_path)
    config.write_text(json.dumps({"worker_materialize": ["../x"]}), encoding="utf-8")
    with pytest.raises(CardCheckoutError, match="worker_materialize_invalid"):
        worker_materialize.repo_default_materialize(tmp_path)


def test_invalid_card_field_refuses_workspace_creation(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(worker_workspace.WorkspaceError, match="checkout_invalid:unknown_value"):
        worker_workspace.create_workspace(repo, "req-nf02237-bad", _card(checkout="x"), "v")


# --- validation and reproduction trees --------------------------------------


def test_validation_tree_honours_forbidden_rule(tmp_path, workspaces):
    repo = _repo(tmp_path)
    card = _card(materialize=["configs/a.json"], forbidden=["configs/secret.json"])
    source = _create(repo, "req-nf02237-src", card, workspaces)
    (source.path / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
    combined, _info = worker_workspace.create_combined_validation_workspace(
        source, card, ["src/app.py"]
    )
    workspaces.append(combined)
    assert _present(combined.path, "data/model.bin")
    assert _present(combined.path, "configs/b.json")
    assert not _present(combined.path, "configs/secret.json")
    assert (combined.path / "src/app.py").read_text(encoding="utf-8") == "VALUE = 3\n"


def test_neighbour_base_tree_honours_forbidden_rule(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    card = _card(checkout="full", forbidden=["configs/secret.json"])
    holder = SimpleNamespace(repo=repo, allowed_writes=("src/app.py",))
    real_create = worker_workspace.create_workspace
    pinned: list[str | None] = []

    def unpinned_create(repo_path, request_id, base_card, adapter_id, *, pinned_base_oid=None):
        # A pinned ``git worktree add`` does a full checkout first, which some
        # nested sandboxes refuse ("'$GIT_DIR' too big"); the tree rule under
        # test is the expansion that follows, identical for an unpinned HEAD.
        pinned.append(pinned_base_oid)
        return real_create(repo_path, request_id, base_card, adapter_id)

    monkeypatch.setattr(accept_neighbour_tests._workspace, "create_workspace", unpinned_create)
    with accept_neighbour_tests.base_commit_workspace(holder, card) as base:
        assert _present(base.path, "data/model.bin")
        assert _present(base.path, "configs/a.json")
        assert not _present(base.path, "configs/secret.json")
    assert pinned and pinned[0] == _git(repo, "rev-parse", "HEAD").strip()


def test_every_tree_routes_through_the_one_helper():
    for module in (accept_neighbour_tests, finding_reproduction):
        source = inspect.getsource(module)
        assert "worker_materialize.expand_full_tree(" in source
        assert '"sparse-checkout", "disable"' not in source
    combined = inspect.getsource(worker_workspace.create_combined_validation_workspace)
    assert "worker_materialize.expand_full_tree(" in combined
    assert '"sparse-checkout", "disable"' not in combined
