"""NF-2026-00258 / NF-2026-00266: card-creation scope diagnostics.

- Card creation WARNS by name when a path referenced in the card's own evidence
  (objective / scope_files) is absent from allowed_writes -- and never refuses;
  the manager may have a good reason.
- A directory in allowed_writes is REJECTED at creation with the reason (it
  cannot be promoted -- promotion materializes exact declared files).
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

from aiworkhub import core, task_store  # noqa: E402

_CLAUDE_IDENTITY = {
    "provider": "claude",
    "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
    "window_id": "claude_vscode_4242",
}


@pytest.fixture
def coord(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
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
        task_id="T_SCOPE",
        title="scope card",
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


# --- NF-2026-00258: warn by name, never refuse -----------------------------

def test_scope_warns_when_objective_path_absent_from_allowed_writes(coord):
    res = _create(
        coord,
        objective="the bug lives in src/aiworkhub/bug_holder.py and must be fixed",
    )
    assert res["ok"] is True, res
    assert res["created"] is True
    assert "src/aiworkhub/bug_holder.py" in res["scope_warnings"]


def test_scope_warning_is_not_a_refusal(coord):
    """The card is still created despite the warning -- the manager may override."""
    res = _create(
        coord,
        objective="edit src/aiworkhub/other.py to repair the regression",
    )
    assert res["ok"] is True, res
    assert res["created"] is True
    assert task_store.get_task(coord, "T_SCOPE") is not None
    assert "src/aiworkhub/other.py" in res["scope_warnings"]


def test_no_scope_warning_when_referenced_path_is_covered(coord):
    res = _create(
        coord,
        objective="repair the bug in src/aiworkhub/foo.py",
    )
    assert res["ok"] is True, res
    assert res["scope_warnings"] == []


def test_url_in_objective_is_not_warned_as_missing_path(coord):
    """A URL in objective prose is never a repository path: its host+path tail
    must not surface as a bogus missing-scope warning."""
    res = _create(
        coord,
        objective=(
            "repair src/aiworkhub/foo.py; see https://example.com/docs/a/b.py "
            "and http://host.test/x/y.cfg for context"
        ),
    )
    assert res["ok"] is True, res
    assert res["scope_warnings"] == [], res["scope_warnings"]


def test_read_only_evidence_path_is_not_warned(coord):
    """A path declared as read-only evidence is intentionally outside
    allowed_writes and must not raise a missing-scope warning."""
    res = _create(
        coord,
        objective="use src/aiworkhub/context.py as reference while editing foo",
        read_first=["src/aiworkhub/context.py"],
    )
    assert res["ok"] is True, res
    assert "src/aiworkhub/context.py" not in res["scope_warnings"]


# --- NF-2026-00266: a directory in allowed_writes is rejected --------------

def test_directory_allowed_write_trailing_slash_rejected(coord):
    res = _create(
        coord,
        allowed_writes=["src/aiworkhub/foo.py", "build/"],
    )
    assert res["ok"] is False
    assert "allowed_write_is_directory" in (res.get("stderr") or "")
    assert res.get("allowed_write") == "build/"


def test_directory_allowed_write_existing_dir_rejected(coord):
    (coord / "existing_dir").mkdir()
    res = _create(
        coord,
        allowed_writes=["existing_dir", "src/aiworkhub/foo.py"],
    )
    assert res["ok"] is False
    assert "allowed_write_is_directory" in (res.get("stderr") or "")
    assert res.get("allowed_write") == "existing_dir"


def test_file_allowed_write_is_accepted(coord):
    res = _create(coord)
    assert res["ok"] is True, res
    assert res["created"] is True
