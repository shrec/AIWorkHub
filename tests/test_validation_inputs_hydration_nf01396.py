from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aiworkhub import worker_workspace as ww
from aiworkhub.validation_inputs import (
    CONFIG_RELATIVE_PATH,
    SCHEMA_ID,
    repo_validation_input_paths,
)

FIXTURE = "artifacts/fixtures/data.txt"
ALLOWED = "src/mod.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _write_config(repo: Path, payload: object) -> None:
    path = repo / CONFIG_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")


def _config(include: object) -> dict[str, object]:
    return {"schema_id": SCHEMA_ID, "include": include}


def _make_repo(tmp_path: Path, *, with_config: bool) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("artifacts/\n.aiworkhub/runtime/\n", encoding="utf-8")
    if with_config:
        _write_config(repo, _config(["artifacts/fixtures/**"]))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    fixture = repo / FIXTURE
    fixture.parent.mkdir(parents=True)
    fixture.write_text("fixture\n", encoding="utf-8")
    (fixture.parent / ".gitkeep").write_text("", encoding="utf-8")
    return repo


def _card() -> dict[str, object]:
    return {
        "task_id": "t",
        "allowed_writes": [ALLOWED],
        "read_first": [ALLOWED],
        "required_outputs": [ALLOWED],
        "validation": [],
    }


def _trees(repo: Path) -> tuple[ww.WorkerWorkspace, ww.WorkerWorkspace]:
    card = _card()
    worker = ww.create_workspace(repo, "nf01396worker", card, "validation")
    (worker.path / ALLOWED).write_text("X = 2\n", encoding="utf-8")
    combined, _ = ww.create_combined_validation_workspace(worker, card, [ALLOWED])
    return worker, combined


@pytest.mark.parametrize("with_config", [True, False])
def test_fixture_hydration_follows_repository_declaration(
    tmp_path: Path, with_config: bool
) -> None:
    repo = _make_repo(tmp_path, with_config=with_config)
    worker, combined = _trees(repo)
    try:
        assert (worker.path / FIXTURE).is_file() is with_config
        assert (combined.path / FIXTURE).is_file() is with_config
        # The dotfile beside the fixture neither aborts creation nor hydrates.
        assert not (worker.path / "artifacts/fixtures/.gitkeep").exists()
        assert not (combined.path / "artifacts/fixtures/.gitkeep").exists()
    finally:
        ww.cleanup_workspace(repo, combined.path, combined.home)
        ww.cleanup_workspace(repo, worker.path, worker.home)


def test_missing_config_is_noop(tmp_path: Path) -> None:
    assert repo_validation_input_paths(tmp_path) == []


def test_valid_config_returns_matching_files_without_dotfiles(tmp_path: Path) -> None:
    for relative in ("artifacts/fixtures/a.txt", "artifacts/fixtures/.gitkeep", "data/x.bin"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("x", encoding="utf-8")
    _write_config(tmp_path, _config(["artifacts/fixtures/**", "data/*.bin"]))
    assert repo_validation_input_paths(tmp_path) == [
        "artifacts/fixtures/a.txt",
        "data/x.bin",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        {"schema_id": "other.v1", "include": []},
        {"include": []},
        _config("artifacts/**"),
        _config({"a": "b"}),
        _config([1]),
        _config([None]),
        _config(["/etc/passwd"]),
        _config(["C:/Windows/x"]),
        _config(["../outside/**"]),
        _config(["a/../b"]),
        _config([".hidden/x"]),
        _config(["artifacts/.secret/**"]),
        _config([".aiworkhub/**"]),
        _config([""]),
        [],
    ],
)
def test_invalid_config_fails_closed(tmp_path: Path, payload: object) -> None:
    _write_config(tmp_path, payload)
    with pytest.raises(ww.WorkspaceError):
        repo_validation_input_paths(tmp_path)


def test_include_list_over_seed_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ww, "MAX_SEED_FILES", 2)
    _write_config(tmp_path, _config(["a", "b", "c"]))
    with pytest.raises(ww.WorkspaceError, match="seed_file_limit_exceeded"):
        repo_validation_input_paths(tmp_path)


def test_expanded_matches_over_seed_bound_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(3):
        (tmp_path / "fx").mkdir(exist_ok=True)
        (tmp_path / "fx" / f"{index}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(ww, "MAX_SEED_FILES", 2)
    _write_config(tmp_path, _config(["fx/**"]))
    with pytest.raises(ww.WorkspaceError, match="seed_file_limit_exceeded"):
        repo_validation_input_paths(tmp_path)


def test_symlink_match_escaping_repo_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / "fx").mkdir(parents=True)
    try:
        (repo / "fx" / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    _write_config(repo, _config(["fx/**"]))
    with pytest.raises(ww.WorkspaceError, match="validation_inputs_escapes_repo"):
        repo_validation_input_paths(repo)


def test_symlink_match_into_private_repo_state_is_refused(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")
    (tmp_path / "fx").mkdir()
    try:
        (tmp_path / "fx" / "leak").symlink_to(tmp_path / ".env")
    except OSError:
        pytest.skip("symlinks unavailable")
    _write_config(tmp_path, _config(["fx/**"]))
    with pytest.raises(ww.WorkspaceError, match="validation_inputs_private_target"):
        repo_validation_input_paths(tmp_path)
