"""NF-2026-02328: preview and convert refuse a runner that launch would refuse.

Measured 2026-11-21: a claude_* runner absent from the canonical workforce
table was accepted by preview and convert (convertible=true, no warning), then
the committed card died at launch with workforce_route_absent. The refusal now
happens before any card is created, and it names the registered routes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import core, needfix_store, server, workforce_catalog  # noqa: E402

REFUSED_RUNNER = "claude_sonnet"
ROUTABLE_RUNNER = "claude_sonnet-5"
SCOPE = "src/aiworkhub/target_output.py"


def _plan(runner: str | None) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "topic": "needfix_conversion",
        "allowed_writes": [SCOPE],
        "required_outputs": [SCOPE],
    }
    if runner is not None:
        plan["runner"] = runner
    return plan


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    needfix_store.initialize_repository(tmp_path)
    full = tmp_path / SCOPE
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("# fixture\n", encoding="utf-8")
    core.previewed_plan_cache_invalidate()
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def accepted(repo: Path) -> dict[str, Any]:
    return needfix_store.add_needfix(
        repo,
        title="NF-2026-02328 runner route preflight",
        description="Refuse a runner launch would refuse before creating a card.",
        status="accepted",
        kind="bug",
    )


def test_preview_refuses_a_runner_launch_would_refuse(accepted: dict[str, Any]) -> None:
    preview = core.needfix_preview_convert(accepted["id"], task_plan=_plan(REFUSED_RUNNER))

    assert preview["convertible"] is False
    reason = preview["refusal_reason"]
    assert reason.startswith(f"runner_route_absent:runner={REFUSED_RUNNER}:candidates=")
    assert ROUTABLE_RUNNER in reason.split("candidates=", 1)[1].split(",")


def test_convert_refuses_and_commits_nothing(
    repo: Path, accepted: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[dict[str, Any]] = []
    monkeypatch.setattr(
        core,
        "create_task",
        lambda **kwargs: created.append(kwargs) or {"ok": True, "task_id": kwargs["task_id"]},
    )
    plan = _plan(REFUSED_RUNNER)
    preview = core.needfix_preview_convert(accepted["id"], task_plan=plan)

    with pytest.raises(
        needfix_store.NeedFixValidationError,
        match=f"runner_route_absent:runner={REFUSED_RUNNER}:",
    ):
        core.needfix_convert(accepted["id"], task_plan=plan, plan_digest=preview["plan_digest"])

    assert created == []
    assert needfix_store.get_needfix(repo, accepted["id"])["status"] == "accepted"


def test_routable_runner_stays_convertible(accepted: dict[str, Any]) -> None:
    preview = core.needfix_preview_convert(accepted["id"], task_plan=_plan(ROUTABLE_RUNNER))

    assert preview["convertible"] is True
    assert "refusal_reason" not in preview


def test_omitted_runner_filled_from_workforce_default_stays_convertible(
    accepted: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # An omitted runner is filled by the server from the workforce rank top-1;
    # the rank is pinned here so the test does not depend on the host catalog.
    monkeypatch.setattr(
        server,
        "_needfix_rank_launch_contract",
        lambda _needfix_id, _plan: {"runner": ROUTABLE_RUNNER},
    )

    preview = server.needfix_preview_convert(accepted["id"], task_plan=_plan(None))

    assert preview["convertible"] is True
    assert "refusal_reason" not in preview


def test_unreadable_catalog_never_refuses_a_table_runner(
    accepted: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unreadable(_repo_root: Any) -> dict[str, Any]:
        raise OSError("workforce catalog unreadable")

    monkeypatch.setattr(workforce_catalog, "catalog_launch_identities", _unreadable)

    preview = core.needfix_preview_convert(accepted["id"], task_plan=_plan(ROUTABLE_RUNNER))

    assert preview["convertible"] is True
    assert "refusal_reason" not in preview
