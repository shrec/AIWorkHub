"""NF-2026-01857: card ``read_roots`` validation, grants and contract wiring."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiworkhub import card_read_roots, task_store
from aiworkhub.card_read_roots import read_root_grants, validate_read_roots


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """tmp_path is the repo (so its ancestors never matter); home lives inside it."""
    repo = tmp_path.resolve()
    (repo / ".git").mkdir()
    home = repo / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return repo, home


def _dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reason(exc: pytest.ExceptionInfo[ValueError]) -> str:
    prefix, reason, _path = str(exc.value).split(":", 2)
    assert prefix == "read_root_invalid"
    return reason


def test_none_and_empty_list_mean_no_roots(world):
    repo, _ = world
    assert validate_read_roots(None, repo_root=repo) == []
    assert validate_read_roots([], repo_root=repo) == []


def test_valid_roots_are_normalized_deduped_and_nested_collapsed(world):
    repo, _ = world
    outer = _dir(repo / "ignored" / "data")
    inner = _dir(outer / "sub")
    other = _dir(repo / "ignored" / "other")
    raw = [
        str(inner),
        str(outer) + os.sep,
        str(outer / ".." / "data"),
        str(other),
        str(other),
    ]
    assert validate_read_roots(raw, repo_root=repo) == [str(outer), str(other)]


def test_gitignored_directory_inside_the_repo_is_allowed(world):
    repo, _ = world
    data = _dir(repo / "runtime" / "Data")
    assert validate_read_roots([str(data)], repo_root=repo) == [str(data)]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("not-a-list", "not_a_list"),
        ({"a": 1}, "not_a_list"),
        ([1], "not_a_string"),
        ([None], "not_a_string"),
        ([""], "empty"),
        (["   "], "empty"),
        (["C:\\data\x00evil"], "nul_character"),
        (["relative/dir"], "not_absolute"),
        (["\\\\server\\share\\dir"], "unc_or_device_path"),
        (["//server/share/dir"], "unc_or_device_path"),
        (["\\\\?\\C:\\data"], "unc_or_device_path"),
    ],
)
def test_malformed_input_names_its_reason(world, raw, reason):
    repo, _ = world
    with pytest.raises(ValueError) as exc:
        validate_read_roots(raw, repo_root=repo)
    assert _reason(exc) == reason


def test_more_than_sixteen_roots_refused(world):
    repo, _ = world
    roots = [str(_dir(repo / "d" / f"r{i}")) for i in range(17)]
    with pytest.raises(ValueError) as exc:
        validate_read_roots(roots, repo_root=repo)
    assert _reason(exc) == "too_many"
    assert validate_read_roots(roots[:16], repo_root=repo) == roots[:16]


def test_missing_path_and_plain_file_are_not_existing_directories(world):
    repo, _ = world
    missing = repo / "nope"
    afile = repo / "file.txt"
    afile.write_text("x")
    for target in (missing, afile):
        with pytest.raises(ValueError) as exc:
            validate_read_roots([str(target)], repo_root=repo)
        assert _reason(exc) == "not_an_existing_directory"


def test_symlinked_directory_is_a_reparse_point(world):
    repo, _ = world
    real = _dir(repo / "real")
    link = repo / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted here")
    with pytest.raises(ValueError) as exc:
        validate_read_roots([str(link)], repo_root=repo)
    assert _reason(exc) == "reparse_point"


def test_volume_root_refused(world):
    repo, _ = world
    with pytest.raises(ValueError) as exc:
        validate_read_roots([repo.anchor], repo_root=repo)
    assert _reason(exc) == "volume_root"


@pytest.mark.parametrize("name", [".git", ".GIT", ".aiworkhub", ".AIWorkHub"])
def test_git_and_aiworkhub_components_refused_anywhere_in_the_path(world, name):
    repo, _ = world
    deep = _dir(repo / "x" / name / "inner")
    with pytest.raises(ValueError) as exc:
        validate_read_roots([str(deep)], repo_root=repo)
    assert _reason(exc) == "forbidden_component"


def test_repo_root_and_its_parents_refused(world):
    repo, _ = world
    for target in (repo, repo.parent):
        with pytest.raises(ValueError) as exc:
            validate_read_roots([str(target)], repo_root=repo)
        assert _reason(exc) in {"contains_repo_root", "volume_root"}
    with pytest.raises(ValueError) as exc:
        validate_read_roots([str(repo)], repo_root=repo)
    assert _reason(exc) == "contains_repo_root"


def test_directory_inside_another_git_repository_refused(world):
    repo, _ = world
    other = _dir(repo / "other")
    (other / ".git").mkdir()
    inside = _dir(other / "Data")
    with pytest.raises(ValueError) as exc:
        validate_read_roots([str(inside)], repo_root=repo)
    assert _reason(exc) == "foreign_git_repository"


def test_home_and_its_parents_refused(world):
    repo, home = world
    unrelated_repo = Path(repo.anchor) / "no_such_repo_nf01857"
    for target in (home, repo):
        with pytest.raises(ValueError) as exc:
            validate_read_roots([str(target)], repo_root=unrelated_repo)
        assert _reason(exc) == "contains_home"


@pytest.mark.parametrize(
    "relative", list(card_read_roots.PROFILE_SECRET_RELATIVE_PATHS)
)
def test_profile_secret_directories_refused(world, relative):
    repo, home = world
    secret = _dir(home / relative)
    inner = _dir(secret / "inner")
    for target in (secret, inner):
        with pytest.raises(ValueError) as exc:
            validate_read_roots([str(target)], repo_root=repo)
        assert _reason(exc) == "profile_secret"


def test_first_invalid_root_refuses_the_whole_list(world):
    repo, _ = world
    good = _dir(repo / "d" / "good")
    with pytest.raises(ValueError):
        validate_read_roots([str(good), str(repo / "missing")], repo_root=repo)


def test_read_roots_are_part_of_the_prompt_contract():
    assert "read_roots" in task_store.TASK_CONTRACT_KEYS
    assert "read_roots" in task_store.CARD_CONTENT_IDENTITY_KEYS


def _appcontainer():
    return pytest.importorskip("aiworkhub.windows_appcontainer")


def test_read_root_grants_are_persistent_read_execute(world):
    ac = _appcontainer()
    repo, _ = world
    data = _dir(repo / "ignored" / "Data")
    worktree = _dir(repo / "wt")
    grants = read_root_grants([str(data)], covered=[str(worktree)])
    assert grants == [ac.ContainerGrant(str(data), "read_execute", persistent=True)]
    assert read_root_grants([], covered=[str(worktree)]) == []


def test_read_root_grants_refuse_overlap_with_a_writable_grant(world):
    ac = _appcontainer()
    repo, _ = world
    writable = _dir(repo / "wt")
    for root in (_dir(writable / "inside"), writable, repo):
        with pytest.raises(ac.AppContainerError):
            read_root_grants([str(root)], covered=[str(writable)])


def test_read_root_grants_refuse_a_root_that_became_a_reparse_point(world, monkeypatch):
    ac = _appcontainer()
    repo, _ = world
    data = _dir(repo / "ignored" / "Data")
    monkeypatch.setattr(card_read_roots, "_real_key", lambda path: "elsewhere")
    with pytest.raises(ac.AppContainerError):
        read_root_grants([str(data)], covered=[])


def test_supervisor_extends_grants_only_when_read_roots_present(world, monkeypatch):
    if os.name != "nt":
        pytest.skip("AppContainer launch path is Windows only")
    ac = pytest.importorskip("aiworkhub.windows_appcontainer")
    from aiworkhub import worker_supervisor

    repo, _ = world
    data = _dir(repo / "ignored" / "Data")
    wt = _dir(repo / "wt")
    base = [ac.ContainerGrant(str(wt), "modify")]
    seen: list[list] = []

    class _Stop(Exception):
        pass

    def fake_launch(request):
        seen.append(list(request.filesystem_grants))
        raise _Stop

    monkeypatch.setattr(worker_supervisor, "_worker_filesystem_grants", lambda *a: list(base))
    monkeypatch.setattr(worker_supervisor, "_native_handle", lambda fd: fd)
    monkeypatch.setattr(worker_supervisor, "_native_worker_argv", lambda argv: argv)
    monkeypatch.setattr(ac, "launch_appcontainer", fake_launch)
    spec = {"repo_id": "repo_x", "worker_kind": "claude_cli"}
    for extra in ({}, {"read_roots": [str(data)]}):
        with pytest.raises(_Stop):
            worker_supervisor._launch_appcontainer_process(
                ["tool.exe"], str(wt), {**spec, **extra}
            )
    assert seen[0] == base
    assert seen[1] == [
        *base,
        ac.ContainerGrant(str(data), "read_execute", persistent=True),
    ]
