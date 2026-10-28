from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, development_rules as dr, project_context  # noqa: E402

_GLOBS = ["src/*.py", "src/a?.py", "src/[ab].py", "dashboard/**/popup*.js"]


def _repo(tmp_path: Path, *, manifest: bool) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    if manifest:
        body = {
            "schema": dr.SCHEMA_ID, "schema_version": dr.SCHEMA_VERSION,
            "languages": ["python"],
            "rules": [{
                "id": "baseline", "topic": "baseline", "kind": "coding_convention",
                "applicability": {}, "forbid": ["unsafe_authority"],
                "payload": {"guideline_ids": ["baseline"], "severity": "error"},
            }],
        }
        path = repo / ".aiworkhub/config/development_rules.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(body), encoding="utf-8")
    return repo


def _launch(repo: Path, allowed_writes: list[str]):
    card = {"task_id": "T", "allowed_writes": allowed_writes, "task_category": "bugfix", "risk_tier": "low"}
    return project_context._development_rules_section(repo, card, workspace_root=repo)


def _create(monkeypatch: pytest.MonkeyPatch, repo: Path, item: str):
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    return core._allowed_write_path_error(item)


@pytest.mark.parametrize("glob", _GLOBS)
def test_glob_allowed_write_is_refused_at_create_and_at_launch(tmp_path, monkeypatch, glob):
    repo = _repo(tmp_path, manifest=True)
    created = _create(monkeypatch, repo, glob)
    assert created is not None
    assert created["stderr"] == "allowed_write_refused_by_development_rules"
    assert created["reason"] == dr.ReasonCode.UNSAFE_PATH_SELECTOR.value
    with pytest.raises(project_context.ProjectContextError, match="development_rules_invalid"):
        _launch(repo, [glob])


def test_concrete_allowed_write_is_accepted_at_create_and_launch_builds_section(tmp_path, monkeypatch):
    repo = _repo(tmp_path, manifest=True)
    for item in ("src/a.py", ".github/workflows/ci.yml", "tests/test_x.py"):
        assert _create(monkeypatch, repo, item) is None
    section = _launch(repo, ["src/a.py", ".github/workflows/ci.yml"])
    assert section is not None


def test_create_does_not_refuse_globs_without_a_development_rules_manifest(tmp_path, monkeypatch):
    repo = _repo(tmp_path, manifest=False)
    assert _create(monkeypatch, repo, "src/*.py") is None
    assert _launch(repo, ["src/*.py"]) is None


def test_create_side_check_reuses_the_resolve_path_validator():
    for value in ("src/*.py", "src/ok.py", "a b.py", "-x.py", None):
        try:
            dr._validate_resolve_path(value)
            expected = None
        except dr.ManifestValidationError as exc:
            expected = exc.reason.value
        assert dr.concrete_path_refusal(value) == expected
