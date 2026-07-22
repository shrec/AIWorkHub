from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import repository_state


def test_repository_root_symlink_resolves_to_canonical_repo(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_repo = real_parent / "project"
    real_repo.mkdir(parents=True)
    repository_state.bootstrap_repository(real_repo, repo_name="project")

    link_parent = tmp_path / "links"
    link_parent.mkdir()
    link_repo = link_parent / "project-link"
    link_repo.symlink_to(real_repo, target_is_directory=True)

    state = repository_state.inspect_repository(link_repo)

    assert state.root == real_repo.resolve()
    assert state.manifest_path == real_repo.resolve() / ".aiworkhub" / "project.json"
    payload = json.loads(state.manifest_path.read_text(encoding="utf-8"))
    assert payload["repo_name"] == "project"
