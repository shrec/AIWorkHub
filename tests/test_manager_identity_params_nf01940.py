"""NF-2026-01940: manager tools name one identity one way.

agent_task_status, review_packet and accept_preview accept task_id alone and
resolve it deterministically to the card's current or latest request, naming
``request_id_source``; task_reject_review takes ``request_id`` as an optional
identity assertion; task_supersede takes ``successor_task_id`` as an alias of
``by``. A schema scan over every registered manager tool lists the parameters
that spell a shared identity differently; it must be empty or exempted below.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import core, mcp_tool_metadata, process_launcher, server, task_store  # noqa: E402
from aiworkhub.tool_recovery import resolve_tool_arguments  # noqa: E402

TASK_ID = "NF01940-TASK"
REVIEW_REQUEST = "req-review-nf01940"
LAUNCH_REQUEST = "req-launch-nf01940"

# A parameter spelled like this names the identity on the right under a
# non-canonical name. Role-qualified names (target_task_id, reviewer_request_id,
# accept_task_id, ...) end in the canonical name and are consistent.
NON_CANONICAL_IDENTITY_NAMES = {
    "id": "a named <entity>_id",
    "nf_id": "needfix_id",
    "need_fix_id": "needfix_id",
    "needfix": "needfix_id",
    "task": "task_id",
    "taskid": "task_id",
    "tid": "task_id",
    "card_id": "task_id",
    "card": "task_id",
    "request": "request_id",
    "requestid": "request_id",
    "req_id": "request_id",
    "rid": "request_id",
    "launch_id": "request_id",
    "successor": "successor_task_id",
    "replacement_task_id": "successor_task_id",
    "by": "successor_task_id",
}

# (tool, parameter) -> why the non-canonical name stays. Every exemption must
# still be live; a stale one fails the scan so this list cannot rot.
DOCUMENTED_EXEMPTIONS = {
    ("aiworkhub_task_supersede", "by"): (
        "public name since the tool shipped; successor_task_id is accepted as "
        "its alias by tool_recovery.resolve_tool_arguments (NF-2026-01940)"
    ),
    ("aiworkhub_manager_task_supersede", "by"): (
        "delegates to aiworkhub_task_supersede; same successor_task_id alias"
    ),
}

TASK_RESOLVABLE_TOOLS = (
    "aiworkhub_agent_task_status",
    "aiworkhub_review_packet",
    "aiworkhub_agent_accept_preview",
)


def _manager_tools() -> dict[str, Any]:
    tools: dict[str, Any] = {}
    for name, profiles in mcp_tool_metadata.MCP_TOOL_PROFILES.items():
        if mcp_tool_metadata.MCP_PROFILE_MANAGER not in profiles:
            continue
        function = getattr(server, name, None)
        if callable(function):
            tools[name] = function
    return tools


def _inconsistent_identity_params() -> list[tuple[str, str, str]]:
    found = []
    for name, function in sorted(_manager_tools().items()):
        for parameter in inspect.signature(function).parameters:
            canonical = NON_CANONICAL_IDENTITY_NAMES.get(parameter)
            if canonical is not None:
                found.append((name, parameter, canonical))
    return found


def test_registered_manager_tools_name_each_identity_consistently() -> None:
    tools = _manager_tools()
    assert len(tools) > 50, "the manager profile scan found too few tools to be meaningful"

    findings = _inconsistent_identity_params()
    unexempted = [item for item in findings if (item[0], item[1]) not in DOCUMENTED_EXEMPTIONS]
    assert unexempted == [], unexempted

    live = {(tool, parameter) for tool, parameter, _ in findings}
    stale = sorted(set(DOCUMENTED_EXEMPTIONS) - live)
    assert stale == [], f"exemptions no longer needed: {stale}"


def test_task_resolvable_tools_take_task_id_and_an_optional_request_id() -> None:
    tools = _manager_tools()
    for name in TASK_RESOLVABLE_TOOLS:
        parameters = inspect.signature(tools[name]).parameters
        assert "task_id" in parameters, name
        assert parameters["request_id"].default == "", name


def test_successor_task_id_is_an_alias_of_by_on_both_supersede_tools() -> None:
    for function in (server.aiworkhub_task_supersede, server.aiworkhub_manager_task_supersede):
        resolved, problem = resolve_tool_arguments(
            function, {"task_id": "OLD", "successor_task_id": "NEW"}
        )
        assert problem is None
        assert resolved == {"task_id": "OLD", "by": "NEW"}

        _, conflict = resolve_tool_arguments(
            function, {"task_id": "OLD", "successor_task_id": "NEW", "by": "OTHER"}
        )
        assert conflict is not None and "alias_conflict" in conflict


def test_supersede_called_with_the_alias_records_the_successor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def _supersede(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return {"ok": False, "error": "stub"}

    monkeypatch.setattr(core, "supersede_task", _supersede)
    arguments, problem = resolve_tool_arguments(
        server.aiworkhub_task_supersede, {"task_id": "OLD", "successor_task_id": "NEW"}
    )
    assert problem is None
    server.aiworkhub_task_supersede(**arguments)

    assert seen == [{"task_id": "OLD", "reason": "", "by": "NEW"}]


# --- task_id -> request_id resolution --------------------------------------


def _card(**overrides: Any) -> dict[str, Any]:
    card: dict[str, Any] = {
        "task_id": TASK_ID,
        "launch_request_id": LAUNCH_REQUEST,
        "terminal_review": {"evidence": {"request_identity": {"request_id": REVIEW_REQUEST}}},
    }
    card.update(overrides)
    return card


class _Manager:
    def __init__(self) -> None:
        self.status_calls: list[str] = []
        self.preview_calls: list[tuple[str, str]] = []

    def status(self, request_id: str) -> dict[str, Any]:
        self.status_calls.append(request_id)
        return {"ok": True, "request_id": request_id, "raw": True}

    def accept_preview(self, request_id: str, task_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.preview_calls.append((request_id, task_id))
        return {"ok": True, "blocked": False}


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Manager:
    fake = _Manager()
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(task_store, "get_task_events", lambda *_a, **_k: [])
    return fake


def _use_card(monkeypatch: pytest.MonkeyPatch, card: dict[str, Any] | None) -> None:
    monkeypatch.setattr(
        task_store, "get_task", lambda _root, task_id: card if task_id == TASK_ID else None
    )


def test_agent_task_status_with_task_id_only_reads_the_latest_launch(
    manager: _Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_card(monkeypatch, _card())

    reply = server.aiworkhub_agent_task_status(task_id=TASK_ID, detail="full")

    assert manager.status_calls == [LAUNCH_REQUEST]
    assert reply["request_id_source"] == "card_latest_launch"
    assert reply["request_id"] == LAUNCH_REQUEST


def test_agent_task_status_with_request_id_is_unchanged(
    manager: _Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_card(monkeypatch, _card())

    reply = server.aiworkhub_agent_task_status("explicit-req", detail="full")

    assert manager.status_calls == ["explicit-req"]
    assert reply == {"ok": True, "request_id": "explicit-req", "raw": True}


def test_accept_preview_with_task_id_only_uses_the_current_review_request(
    manager: _Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_card(monkeypatch, _card())

    reply = server.aiworkhub_agent_accept_preview(task_id=TASK_ID)

    assert manager.preview_calls == [(REVIEW_REQUEST, TASK_ID)]
    assert reply["request_id_source"] == "card_current_review"


def test_accept_preview_falls_back_to_the_launch_request_without_review_evidence(
    manager: _Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_card(monkeypatch, _card(terminal_review=None))

    reply = server.aiworkhub_agent_accept_preview(task_id=TASK_ID)

    assert manager.preview_calls == [(LAUNCH_REQUEST, TASK_ID)]
    assert reply["request_id_source"] == "card_latest_launch"


def test_review_packet_with_task_id_only_uses_the_current_review_request(
    manager: _Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_card(monkeypatch, _card())
    seen: list[tuple[str, str, int]] = []

    def _one(request_id: str, task_id: str, max_bytes: int) -> dict[str, Any]:
        seen.append((request_id, task_id, max_bytes))
        return {"ok": True}

    monkeypatch.setattr(server, "_review_packet_one", _one)

    reply = server.aiworkhub_review_packet(task_id=TASK_ID)

    assert seen == [(REVIEW_REQUEST, TASK_ID, 90_000)]
    assert reply["request_id_source"] == "card_current_review"
    assert reply["request_id"] == REVIEW_REQUEST


def test_an_unknown_card_refuses_and_names_the_cause(
    manager: _Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_card(monkeypatch, None)

    for reply in (
        server.aiworkhub_agent_task_status(task_id="MISSING"),
        server.aiworkhub_agent_accept_preview(task_id="MISSING"),
        server.aiworkhub_review_packet(task_id="MISSING"),
    ):
        assert reply["ok"] is False
        assert reply["error"] == "request_id_unresolved_for_task:MISSING"
        assert reply["request_id_source"] == "unresolved"
    assert manager.status_calls == [] and manager.preview_calls == []


def test_no_identity_at_all_is_refused(manager: _Manager) -> None:
    assert server.aiworkhub_agent_task_status()["error"] == "request_id_or_task_id_required"
    assert server.aiworkhub_agent_accept_preview()["error"] == "task_id_required"
    assert server.aiworkhub_review_packet()["error"] == "request_id_or_request_ids_required"


# --- task_reject_review request_id assertion --------------------------------


@pytest.fixture
def reject_calls(manager: _Manager, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _reject(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ok": False, "error": "stub_reject"}

    monkeypatch.setattr(core, "reject_review", _reject)
    _use_card(monkeypatch, _card())
    return calls


def test_reject_review_refuses_a_request_id_that_is_not_the_current_review(
    reject_calls: list[dict[str, Any]],
) -> None:
    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason="rework", request_id=LAUNCH_REQUEST
    )

    assert reply["ok"] is False
    assert reply["error"] == "request_id_mismatch"
    assert reply["current_request_id"] == REVIEW_REQUEST
    assert reply["request_id_source"] == "card_current_review"
    assert reject_calls == []


def test_reject_review_refuses_an_overlong_id_that_starts_with_the_current_one(
    reject_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin the echo cap to the current id's length so a comparison made on the
    # truncated echo would wrongly match; the full asserted id must be compared.
    monkeypatch.setattr(server, "_MAX_ECHOED_ID_CHARS", len(REVIEW_REQUEST))
    asserted = REVIEW_REQUEST + "x" * (200 - len(REVIEW_REQUEST))
    assert len(asserted) == 200

    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason="rework", request_id=asserted
    )

    assert reply["ok"] is False
    assert reply["error"] == "request_id_mismatch"
    assert reply["current_request_id"] == REVIEW_REQUEST
    assert reject_calls == []


def test_reject_review_accepts_a_matching_request_id(reject_calls: list[dict[str, Any]]) -> None:
    server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason="rework", request_id=REVIEW_REQUEST
    )

    assert len(reject_calls) == 1
    assert reject_calls[0]["task_id"] == TASK_ID
    assert "request_id" not in reject_calls[0]


def test_reject_review_without_request_id_skips_the_check(
    reject_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_card(monkeypatch, None)

    server.aiworkhub_task_reject_review(task_id=TASK_ID, reason="rework")

    assert len(reject_calls) == 1


def test_reject_review_refuses_an_unverifiable_or_empty_assertion(
    reject_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = server.aiworkhub_task_reject_review(task_id=TASK_ID, reason="rework", request_id="")
    assert empty["error"].startswith("request_id_empty")

    _use_card(monkeypatch, None)
    unverifiable = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason="rework", request_id=REVIEW_REQUEST
    )
    assert unverifiable["error"] == "request_id_unverifiable"
    assert reject_calls == []
