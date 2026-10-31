"""NF-2026-01895: preview_convert refuses a chained validation command like convert/launch."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import core, needfix_store

TARGET = "src/aiworkhub/near.py"


@pytest.fixture
def needfix_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    needfix_store.initialize_repository(tmp_path)
    target = tmp_path / TARGET
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def f():\n    pass\n", encoding="utf-8")
    core.previewed_plan_cache_invalidate()
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    yield tmp_path
    core.previewed_plan_cache_invalidate()


def _accepted_needfix(repo: Path) -> dict[str, Any]:
    return needfix_store.add_needfix(
        repo,
        title="NF-2026-01895 preview shell chain",
        description="Preview must refuse a chained validation command.",
        status="accepted",
        kind="bug",
    )


def _plan(validation: list[str]) -> dict[str, Any]:
    return {
        "runner": "opencode_go_gemini-4-argon-contributor",
        "topic": "needfix-convert",
        "allowed_writes": [TARGET],
        "required_outputs": [TARGET],
        "validation": validation,
    }


def test_chained_validation_command_is_not_convertible(needfix_repo: Path) -> None:
    rec = _accepted_needfix(needfix_repo)
    chained = "python -m pytest a.py -q && python -m pytest b.py -q"

    preview = needfix_store.preview_convert(needfix_repo, rec["id"], _plan([chained]))

    assert preview["convertible"] is False, preview
    assert "validation_shell_chain_forbidden" in preview["refusal_reason"]


def test_separate_validation_entries_stay_convertible(needfix_repo: Path) -> None:
    rec = _accepted_needfix(needfix_repo)
    plan = _plan(["python -m pytest a.py -q", "python -m pytest b.py -q"])

    preview = needfix_store.preview_convert(needfix_repo, rec["id"], plan)

    assert preview["convertible"] is True, preview
    assert "refusal_reason" not in preview


def test_allowed_cd_prefix_stays_convertible(needfix_repo: Path) -> None:
    rec = _accepted_needfix(needfix_repo)
    plan = _plan(["cd src && python -m pytest x.py -q"])

    preview = needfix_store.preview_convert(needfix_repo, rec["id"], plan)

    assert preview["convertible"] is True, preview
