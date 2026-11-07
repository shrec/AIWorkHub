"""NF-2026-01955: task_create warning noise.

- The runner-route advisory is reported in full once per (repo_root, runner)
  pair per process; later creates for the same pair get one short marker.
- An objective-prose path is a scope warning only when it names an existing
  repository file that is neither read-only evidence nor in allowed_writes.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import core, task_create_warnings, task_store  # noqa: E402

_CLAUDE_IDENTITY = {
    "provider": "claude",
    "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
    "window_id": "claude_vscode_4242",
}
_UNROUTABLE = "phantom_nf01955_runner"
_UNROUTABLE_OTHER = "phantom_nf01955_other"


@pytest.fixture(autouse=True)
def _fresh_runner_route_seen_set():
    task_create_warnings.reset_runner_route_dedupe_for_tests()
    yield
    task_create_warnings.reset_runner_route_dedupe_for_tests()


@pytest.fixture
def coord(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    (root / "src" / "aiworkhub").mkdir(parents=True)
    (root / "src" / "aiworkhub" / "context.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "aiworkhub" / "other.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    tok = tmp_path / "coordinator.token"
    tok.write_text("coord-token\n", encoding="utf-8")
    os.chmod(tok, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(tok))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: dict(_CLAUDE_IDENTITY))
    return root


def _create(coord, **overrides):
    kwargs = dict(
        task_id="T_NOISE_A",
        title="noise card",
        runner="claude_coding",
        topic="coding",
        objective="fix the bug",
        acceptance=["it works"],
        allowed_writes=["src/aiworkhub/foo.py"],
        required_outputs=["src/aiworkhub/foo.py"],
        validation=["pytest -q tests/test_foo.py"],
        callback_required=False,
        custom_template_escape="audited_custom_unclassified",
    )
    kwargs.update(overrides)
    return core.create_task(**kwargs)


def test_repeat_create_with_same_unroutable_runner_returns_marker(coord):
    first = _create(coord, task_id="T_NOISE_A", runner=_UNROUTABLE)
    assert first["ok"] is True, first
    assert any(
        w.startswith(f"workforce_route_absent:runner={_UNROUTABLE}")
        for w in first["runner_route_warnings"]
    )

    second = _create(coord, task_id="T_NOISE_B", runner=_UNROUTABLE)
    assert second["ok"] is True, second
    assert second["runner_route_warnings"] == [
        f"runner_route_warnings_repeated:runner={_UNROUTABLE}"
    ]


def test_different_unroutable_runner_warns_in_full(coord):
    _create(coord, task_id="T_NOISE_A", runner=_UNROUTABLE)
    other = _create(coord, task_id="T_NOISE_C", runner=_UNROUTABLE_OTHER)
    assert other["ok"] is True, other
    assert any(
        w.startswith(f"workforce_route_absent:runner={_UNROUTABLE_OTHER}")
        for w in other["runner_route_warnings"]
    )
    assert not any("repeated" in w for w in other["runner_route_warnings"])


def test_objective_mention_of_read_first_file_is_not_a_scope_warning(coord):
    res = _create(
        coord,
        task_id="T_NOISE_D",
        objective="use src/aiworkhub/context.py as the reference for foo",
        read_first=["src/aiworkhub/context.py"],
    )
    assert res["ok"] is True, res
    assert "src/aiworkhub/context.py" not in res["scope_warnings"]


def test_objective_mention_of_nonexistent_path_is_not_a_scope_warning(coord):
    res = _create(
        coord,
        task_id="T_NOISE_E",
        objective="the old layout lived in src/aiworkhub/ghost_nf01955.py, ignore it",
    )
    assert res["ok"] is True, res
    assert res["scope_warnings"] == [], res["scope_warnings"]


def test_objective_mention_of_existing_file_outside_scope_still_warns(coord):
    res = _create(
        coord,
        task_id="T_NOISE_F",
        objective="the regression is in src/aiworkhub/other.py, fix it",
    )
    assert res["ok"] is True, res
    assert "src/aiworkhub/other.py" in res["scope_warnings"]


def test_objective_noise_helper_policy(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "real.py").write_text("x = 1\n", encoding="utf-8")
    assert task_create_warnings.objective_path_is_scope_noise(
        "a/real.py", {"a/real.py"}, tmp_path
    )
    assert task_create_warnings.objective_path_is_scope_noise("a/missing.py", set(), tmp_path)
    assert not task_create_warnings.objective_path_is_scope_noise("a/real.py", set(), tmp_path)
