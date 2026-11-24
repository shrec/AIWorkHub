"""NF-2026-02175: needfix_convert(launch=true) commits, then launches the new card.

The launch goes through server.aiworkhub_agent_launch_task, so its gates are the
same code path. A launch refusal or exception never undoes the conversion, and a
refused or already-converted NeedFix never launches.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import core, server  # noqa: E402

# The store double, the preview/commit fixtures and the plan are the owner's;
# importing them keeps one definition of each.
from test_needfix_one_call_convert_nf02150 import (  # noqa: E402
    NEEDFIX_ID,
    PLAN,
    promotions,
    store,
)

DIGEST = "c" * 64
CARD_ID = f"needfix-{NEEDFIX_ID}"
ADAPTER = "claude-adapter-nf02175"


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _launch(task_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"task_id": task_id, **kwargs})
        return {"ok": True, "task_id": task_id, "launched": True}

    monkeypatch.setattr(server, "aiworkhub_agent_launch_task", _launch)
    return calls


def test_launch_true_launches_the_new_card_once_with_the_adapter(
    store: Any, promotions: list, launches: list
) -> None:
    reply = server.needfix_convert(
        NEEDFIX_ID, task_plan=dict(PLAN), launch=True, launch_adapter_id=ADAPTER
    )

    assert reply["committed"] is True
    assert reply["converted_task_id"] == CARD_ID
    assert launches == [{"task_id": CARD_ID, "adapter_id": ADAPTER}]
    assert reply["launch"] == {"ok": True, "task_id": CARD_ID, "launched": True}


def test_launch_true_without_adapter_omits_adapter_id(
    store: Any, promotions: list, launches: list
) -> None:
    reply = server.needfix_convert(NEEDFIX_ID, task_plan=dict(PLAN), launch=True)

    assert reply["committed"] is True
    assert launches == [{"task_id": CARD_ID}]


def test_launch_false_adds_no_launch_key_and_never_launches(
    store: Any, promotions: list, launches: list
) -> None:
    reply = server.needfix_convert(NEEDFIX_ID, task_plan=dict(PLAN))

    assert reply["committed"] is True
    assert "launch" not in reply
    assert launches == []


def test_refused_preview_skips_launch_as_not_committed(
    monkeypatch: pytest.MonkeyPatch, store: Any, promotions: list, launches: list
) -> None:
    refusal = (
        f"needfix {NEEDFIX_ID} is 'rejected' -- only status 'accepted' "
        "(or 'task_planned') converts"
    )

    def _refused_preview(needfix_id: str, *, task_plan: Any = None) -> dict[str, Any]:
        return {
            "needfix_id": needfix_id,
            "claimable": False,
            "convertible": False,
            "current_status": "rejected",
            "refusal_reason": refusal,
            "plan_digest": DIGEST,
            "task_plan": dict(PLAN),
        }

    def _no_commit(*_a: Any, **_k: Any) -> None:
        raise AssertionError("convert must not run on a refused preview")

    monkeypatch.setattr(core, "needfix_preview_convert", _refused_preview)
    monkeypatch.setattr(core, "needfix_convert", _no_commit)

    reply = server.needfix_convert(NEEDFIX_ID, task_plan=dict(PLAN), launch=True)

    assert reply["committed"] is False
    assert reply["refusal_reason"] == refusal
    assert reply["launch"] == {"ok": False, "skipped": "not_committed"}
    assert launches == []


def test_already_converted_skips_launch(
    monkeypatch: pytest.MonkeyPatch, store: Any, promotions: list, launches: list
) -> None:
    def _converted_preview(needfix_id: str, *, task_plan: Any = None) -> dict[str, Any]:
        return {
            "needfix_id": needfix_id,
            "already_converted": True,
            "claimable": False,
            "convertible": False,
            "current_status": "task_created",
            "refusal_reason": f"needfix {NEEDFIX_ID} is already converted",
            "plan_digest": DIGEST,
            "task_plan": dict(PLAN),
            "converted_task_id": CARD_ID,
        }

    def _no_commit(*_a: Any, **_k: Any) -> None:
        raise AssertionError("convert must not run on an already-converted NeedFix")

    monkeypatch.setattr(core, "needfix_preview_convert", _converted_preview)
    monkeypatch.setattr(core, "needfix_convert", _no_commit)

    reply = server.needfix_convert(NEEDFIX_ID, task_plan=dict(PLAN), launch=True)

    assert reply["launch"] == {"ok": False, "skipped": "already_converted"}
    assert launches == []


def test_launch_exception_keeps_the_commit(
    monkeypatch: pytest.MonkeyPatch, store: Any, promotions: list
) -> None:
    def _boom(task_id: str, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("launch gate exploded")

    monkeypatch.setattr(server, "aiworkhub_agent_launch_task", _boom)

    reply = server.needfix_convert(NEEDFIX_ID, task_plan=dict(PLAN), launch=True)

    assert reply["ok"] is not False
    assert reply["committed"] is True
    assert reply["converted_task_id"] == CARD_ID
    assert reply["launch"]["ok"] is False
    assert reply["launch"]["error"] == "launch_exception"
    assert "launch gate exploded" in reply["launch"]["blocked_reason"]


def test_launch_refusal_keeps_the_commit_and_is_reported_verbatim(
    monkeypatch: pytest.MonkeyPatch, store: Any, promotions: list
) -> None:
    refusal = {"ok": False, "blocked_reason": "launch_gate_closed"}
    monkeypatch.setattr(
        server, "aiworkhub_agent_launch_task", lambda task_id, **_k: dict(refusal)
    )

    reply = server.needfix_convert(NEEDFIX_ID, task_plan=dict(PLAN), launch=True)

    assert reply["committed"] is True
    assert reply["converted_task_id"] == CARD_ID
    assert reply["launch"] == refusal


def test_launch_adapter_without_launch_is_refused_before_any_write(
    monkeypatch: pytest.MonkeyPatch, launches: list
) -> None:
    def _no_write(*_a: Any, **_k: Any) -> None:
        raise AssertionError("no preview or commit may run before the refusal")

    monkeypatch.setattr(core, "needfix_preview_convert", _no_write)
    monkeypatch.setattr(core, "needfix_convert", _no_write)

    reply = server.needfix_convert(NEEDFIX_ID, task_plan=dict(PLAN), launch_adapter_id=ADAPTER)

    assert reply == {
        "ok": False,
        "committed": False,
        "error": "launch_adapter_requires_launch",
        "needfix_id": NEEDFIX_ID,
    }
    assert launches == []


def test_plan_digest_path_launches_the_new_card(
    monkeypatch: pytest.MonkeyPatch, launches: list
) -> None:
    def _no_preview(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the plan_digest path must not preview")

    def _commit(needfix_id: str, *, task_plan: Any = None, plan_digest: Any = None) -> dict[str, Any]:
        return {"ok": True, "needfix_id": needfix_id, "converted_task_id": CARD_ID}

    monkeypatch.setattr(core, "needfix_preview_convert", _no_preview)
    monkeypatch.setattr(core, "needfix_convert", _commit)

    reply = server.needfix_convert(NEEDFIX_ID, plan_digest=DIGEST, launch=True)

    assert launches == [{"task_id": CARD_ID}]
    assert reply["launch"] == {"ok": True, "task_id": CARD_ID, "launched": True}
