"""A card that names an active NeedFix in prose must declare it at create.

``aiworkhub_task_create`` and ``aiworkhub_task_create_from_template`` refuse a
card whose title or objective names a non-terminal NeedFix row without a whole
``Resolves:`` or ``Refs:`` line. Unknown and terminal ids never block.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import needfix_store, server, task_store, task_templates  # noqa: E402
from test_server_task_template_create import _ready_repo  # noqa: E402


def _raw_args(task_id: str, tail: str) -> dict:
    expanded = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    return {
        "task_id": task_id,
        "title": "Template first create",
        "runner": "codex_worker_needfix_decl",
        "topic": "task_mcp",
        "objective": f"{expanded['objective']}\n{tail}",
        "acceptance": ["classify generic python cards"],
        "allowed_writes": expanded["allowed_writes"],
        "required_outputs": expanded["required_outputs"],
        "validation": expanded["validation"],
        "validation_roles": expanded["validation_roles"],
        "read_first": expanded["read_first"],
        "work_kind": "generic",
    }


def _active_row(repo: Path, title: str) -> str:
    return needfix_store.add_needfix(repo, title=title, description="d")["id"]


def test_prose_named_active_row_refuses_the_raw_create(tmp_path, monkeypatch) -> None:
    repo = _ready_repo(tmp_path, monkeypatch)
    needfix_id = _active_row(repo, "Export drops rows")

    result = server.aiworkhub_task_create(
        **_raw_args("TASK_NF_DECL_PROSE", f"This also touches {needfix_id} in prose.")
    )

    assert result["ok"] is False
    assert result["error"] == "needfix_named_not_declared"
    assert result["needfix_ids"] == [needfix_id]
    assert "Resolves:" in result["stderr"] and "Refs:" in result["stderr"]
    assert task_store.get_task(repo, "TASK_NF_DECL_PROSE") is None


def test_prose_named_active_row_refuses_the_template_create(tmp_path, monkeypatch) -> None:
    repo = _ready_repo(tmp_path, monkeypatch)
    needfix_id = _active_row(repo, "Export drops rows")

    result = server.aiworkhub_task_create_from_template(
        task_id="TASK_NF_DECL_TEMPLATE",
        title="Template create with prose NeedFix",
        runner="codex_worker_needfix_decl",
        topic="task_mcp",
        objective=f"Fix the export; {needfix_id} is the row it drops.",
        acceptance=["export keeps every row"],
        template_id="implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )

    assert result["ok"] is False
    assert result["error"] == "needfix_named_not_declared"
    assert result["needfix_ids"] == [needfix_id]
    assert task_store.get_task(repo, "TASK_NF_DECL_TEMPLATE") is None


def test_a_resolves_line_allows_the_create(tmp_path, monkeypatch) -> None:
    repo = _ready_repo(tmp_path, monkeypatch)
    needfix_id = _active_row(repo, "Export drops rows")

    result = server.aiworkhub_task_create(
        **_raw_args("TASK_NF_DECL_RESOLVES", f"Resolves: {needfix_id}")
    )

    assert result["ok"] is True
    assert task_store.get_task(repo, "TASK_NF_DECL_RESOLVES") is not None


def test_a_refs_line_allows_the_create(tmp_path, monkeypatch) -> None:
    repo = _ready_repo(tmp_path, monkeypatch)
    needfix_id = _active_row(repo, "Export drops rows")

    result = server.aiworkhub_task_create(
        **_raw_args("TASK_NF_DECL_REFS", f"Refs: {needfix_id}")
    )

    assert result["ok"] is True
    assert task_store.get_task(repo, "TASK_NF_DECL_REFS") is not None


def test_a_resolved_or_unknown_id_named_in_prose_never_blocks(tmp_path, monkeypatch) -> None:
    repo = _ready_repo(tmp_path, monkeypatch)
    resolved_id = needfix_store.add_needfix(
        repo, title="Old export fault", description="d", status="resolved"
    )["id"]

    result = server.aiworkhub_task_create(
        **_raw_args(
            "TASK_NF_DECL_HISTORY",
            f"Historical context: {resolved_id} was fixed earlier; NF-2026-99999 is unknown.",
        )
    )

    assert result["ok"] is True
    assert task_store.get_task(repo, "TASK_NF_DECL_HISTORY") is not None
