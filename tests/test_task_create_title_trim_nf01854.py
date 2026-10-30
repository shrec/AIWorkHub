"""An over-long card title is trimmed at a word boundary with a warning (NF-2026-01854)."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import core, task_store, task_templates  # noqa: E402

LIMIT = task_templates.MAX_TITLE_LENGTH
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
    (root / "src" / "aiworkhub").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "aiworkhub" / "widget.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "test_widget.py").write_text("def test_x(): pass\n", encoding="utf-8")
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token = tmp_path / "coordinator.token"
    token.write_text("coord-token\n", encoding="utf-8")
    os.chmod(token, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: dict(_CLAUDE_IDENTITY))
    return root


def _create(**overrides):
    kwargs = dict(
        task_id="T_TITLE_TRIM",
        title="short title",
        runner="claude_coding",
        topic="coding",
        objective="change the widget",
        acceptance=["it works"],
        allowed_writes=["src/aiworkhub/widget.py"],
        required_outputs=["src/aiworkhub/widget.py"],
        validation=["python3 -m pytest -q tests/test_widget.py"],
        callback_required=False,
        custom_template_escape="audited_custom_unclassified",
    )
    kwargs.update(overrides)
    return core.create_task(**kwargs)


def test_clip_title_unchanged_at_or_below_limit():
    assert task_templates.clip_title("x" * LIMIT) == ("x" * LIMIT, None)
    assert task_templates.clip_title("short") == ("short", None)


def test_clip_title_cuts_on_a_word_boundary():
    title = " ".join(["abcdefghi"] * 50)
    clipped, original = task_templates.clip_title(title)
    assert original == len(title)
    assert len(clipped) <= LIMIT
    assert title.startswith(clipped)
    assert title[len(clipped)] == " "


def test_clip_title_without_spaces_is_hard_cut():
    assert task_templates.clip_title("x" * 450) == ("x" * LIMIT, 450)


def test_expand_template_trims_instead_of_refusing():
    title = " ".join(["abcdefghi"] * 50)
    card = task_templates.expand_template(
        "docs_change", production_paths=["README.md"], title=title
    )
    assert len(card["title"]) <= LIMIT
    assert title.startswith(card["title"])


@pytest.mark.parametrize("bad", [7, "", "   "])
def test_expand_template_still_refuses_empty_or_non_string_title(bad):
    with pytest.raises(task_templates.TaskTemplateError, match="invalid_title"):
        task_templates.expand_template(
            "docs_change", production_paths=["README.md"], title=bad
        )


def test_task_create_trims_long_title_and_keeps_it_in_objective(coord):
    title = ("abcdefghi " * 50).strip()
    result = _create(title=title)
    assert result["ok"] is True, result
    assert result["warnings"] == [
        {"code": "title_truncated", "original_length": len(title), "limit": LIMIT}
    ]
    card = json.loads(result["stdout"])
    assert len(card["title"]) <= LIMIT
    assert title.startswith(card["title"])
    assert card["title"].endswith("abcdefghi")
    assert card["objective"] == f"Full title: {title}\n\nchange the widget"


def test_task_create_hard_cuts_a_spaceless_title(coord):
    result = _create(task_id="T_TITLE_NOSPACE", title="x" * 450)
    assert result["ok"] is True, result
    assert result["warnings"][0]["original_length"] == 450
    assert json.loads(result["stdout"])["title"] == "x" * LIMIT


def test_task_create_leaves_objective_alone_when_prefix_does_not_fit(coord):
    objective = "o" * (task_templates.MAX_OBJECTIVE_LENGTH - 10)
    result = _create(task_id="T_TITLE_BIGOBJ", title="x" * 450, objective=objective)
    assert result["ok"] is True, result
    assert json.loads(result["stdout"])["objective"] == objective


def test_task_create_title_at_limit_has_no_warning(coord):
    result = _create(task_id="T_TITLE_EXACT", title="x" * LIMIT)
    assert result["ok"] is True, result
    assert "warnings" not in result
    assert json.loads(result["stdout"])["title"] == "x" * LIMIT


def test_task_create_still_refuses_an_empty_title(coord):
    result = _create(task_id="T_TITLE_EMPTY", title="   ")
    assert result["ok"] is False
    assert "invalid_title" in result["stderr"]
