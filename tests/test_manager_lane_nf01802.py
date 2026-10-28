"""NF-2026-01802: manager route health in the doctor and the audited callback-lane claim."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, doctor, manager_lane, mcp_tool_metadata, server, task_store  # noqa: E402

THREAD = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
OTHER_THREAD = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c"
CHAT = "mc_session_one"
OTHER_CHAT = "mc_session_two"

MANAGER_CHAT_CALLER = {"provider": "manager_chat", "session_id": CHAT, "thread_id": "", "route_state": "ready"}
LIVE_CODEX = {
    "provider": "codex",
    "session_id": THREAD,
    "thread_id": THREAD,
    "window_id": "codex_window",
    "route_state": "available",
    "callback_supported": "true",
}
PENDING_CODEX = {
    "provider": "codex",
    "session_id": "codex_window",
    "thread_id": "",
    "window_id": "codex_window",
    "route_state": "route_pending",
    "callback_supported": "false",
}


class Lane:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.identity: dict[str, Any] = {}
        self.chat: dict[str, str] | None = None
        self.codex: dict[str, str] | None = None
        self.cards: list[dict[str, Any]] = []
        self.audit_path = root / "audit.jsonl"
        self.targets_path = root / ".aiworkhub" / "config" / "routing" / "coordinator-targets.json"

    def write_targets(self, payload: Any) -> bytes:
        self.targets_path.parent.mkdir(parents=True, exist_ok=True)
        raw = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
        self.targets_path.write_text(raw, encoding="utf-8")
        return self.targets_path.read_bytes()

    def audit_rows(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def lane(monkeypatch, tmp_path) -> Lane:
    state = Lane(tmp_path)
    monkeypatch.setenv("AIWORKHUB_AUDIT_LOG_PATH", str(state.audit_path))
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "stamped_manager_identity", lambda: dict(state.identity))
    monkeypatch.setattr(core, "_active_manager_chat_record", lambda: state.chat)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: state.codex)

    def list_tasks(root, *, status=None, limit=500):
        assert root == tmp_path
        return [{"task_id": card["task_id"]} for card in state.cards if card["status"] == status][:limit]

    def get_task(root, task_id, *, readiness=None):
        return next((dict(card) for card in state.cards if card["task_id"] == task_id), None)

    monkeypatch.setattr(task_store, "storage_readiness", lambda root: object())
    monkeypatch.setattr(task_store, "list_tasks", list_tasks)
    monkeypatch.setattr(task_store, "get_task", get_task)
    return state


def _card(task_id: str, *, status: str = "pending", provider: str = "manager_chat", route_state: str = "ready") -> dict:
    return {
        "task_id": task_id,
        "status": status,
        "coordinator_provider": provider,
        "manager_route_state": route_state,
    }


# --- route_health -------------------------------------------------------------------------


def test_doctor_reports_the_peer_route_split_the_base_probes_miss(lane, monkeypatch) -> None:
    lane.identity = dict(MANAGER_CHAT_CALLER)
    lane.codex = dict(LIVE_CODEX)
    lane.cards = [_card("T-1", provider="codex", route_state="route_pending")]
    monkeypatch.setattr(server, "aiworkhub_task_health", lambda: {"ok": True})
    monkeypatch.setattr(core, "dispatcher_health", lambda: {"ok": True})
    monkeypatch.setattr(core, "source_graph_health", lambda: {"ok": True})

    base = doctor.diagnose(
        {
            "aiworkhub_task_health": server.aiworkhub_task_health,
            "aiworkhub_dispatcher_health": core.dispatcher_health,
            "aiworkhub_source_graph_health": core.source_graph_health,
        }
    )
    assert base["verdict"] == "ok"

    report = server.aiworkhub_doctor_diagnose()
    assert report["verdict"] == "problem"
    assert [entry["name"] for entry in report["problems"]] == ["aiworkhub_manager_route_health"]
    assert report["problems"][0]["summary"] == manager_lane.REASON_LANE_OWNER

    health = manager_lane.route_health()
    assert health["ok"] is False
    assert health["lane_owner"] == "codex"
    assert health["stamped_provider"] == "manager_chat"
    assert health["reasons"] == [
        manager_lane.REASON_LANE_OWNER,
        manager_lane.REASON_PENDING_CARDS,
        manager_lane.REASON_CARD_PROVIDER,
    ]
    assert health["route_pending_task_ids"] == ["T-1"]


def test_route_health_ok_when_provider_lane_and_target_agree(lane) -> None:
    lane.identity = dict(MANAGER_CHAT_CALLER)
    lane.chat = {"session_id": CHAT}
    lane.write_targets({"schema_id": manager_lane.TARGETS_SCHEMA_ID, "selected_provider": "manager_chat"})
    lane.cards = [_card("T-1"), _card("T-2", status="review"), _card("T-3", status="finished", provider="codex")]
    health = manager_lane.route_health()
    assert health["ok"] is True
    assert health["reason"] == ""
    assert health["route_pending_count"] == 0
    assert health["provider_mismatch_count"] == 0


@pytest.mark.parametrize(
    "setup, reason",
    [
        ("lane_owner", manager_lane.REASON_LANE_OWNER),
        ("selected", manager_lane.REASON_SELECTED_TARGET),
        ("pending", manager_lane.REASON_PENDING_CARDS),
        ("card_provider", manager_lane.REASON_CARD_PROVIDER),
        ("unreadable", manager_lane.REASON_CARDS_UNREADABLE),
        ("unverified", manager_lane.REASON_UNVERIFIED),
    ],
)
def test_each_mismatch_kind_has_its_own_stable_reason(lane, monkeypatch, setup, reason) -> None:
    lane.identity = dict(MANAGER_CHAT_CALLER)
    lane.chat = {"session_id": CHAT}
    if setup == "lane_owner":
        lane.chat = None
        lane.codex = dict(LIVE_CODEX)
    elif setup == "selected":
        lane.write_targets({"selected_provider": "claude"})
    elif setup == "pending":
        lane.cards = [_card("T-1", status="processing", route_state="route_pending")]
    elif setup == "card_provider":
        lane.cards = [_card("T-1", provider="codex")]
    elif setup == "unreadable":
        def broken(root, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(task_store, "list_tasks", broken)
    elif setup == "unverified":
        lane.identity = {}
    health = manager_lane.route_health()
    assert health["ok"] is False
    assert health["reason"] == reason
    assert health["reasons"] == [reason]


def test_verified_claude_owns_its_own_lane(lane) -> None:
    lane.identity = {"provider": "claude", "session_id": "claude-session"}
    lane.codex = dict(LIVE_CODEX)
    health = manager_lane.route_health()
    assert health["lane_owner"] == "claude"
    assert health["ok"] is True


def test_card_lists_are_bounded_with_an_exact_count(lane) -> None:
    lane.identity = dict(MANAGER_CHAT_CALLER)
    lane.chat = {"session_id": CHAT}
    lane.cards = [_card(f"T-{index:02d}", provider="codex", route_state="route_pending") for index in range(25)]
    health = manager_lane.route_health()
    assert health["route_pending_count"] == 25
    assert len(health["route_pending_task_ids"]) == manager_lane.CARD_IDS_MAX == 20
    assert health["provider_mismatch_count"] == 25
    assert len(health["provider_mismatch_task_ids"]) == 20


# --- claim_callback_lane ------------------------------------------------------------------


def test_claim_selects_the_caller_atomically_and_keeps_unrelated_keys(lane) -> None:
    lane.identity = dict(MANAGER_CHAT_CALLER)
    lane.chat = {"session_id": CHAT}
    lane.codex = dict(PENDING_CODEX)  # the peer's stale, non-live Codex route
    lane.write_targets(
        {
            "schema_id": manager_lane.TARGETS_SCHEMA_ID,
            "selected_provider": "codex",
            "repo_id": "repo_1",
            "extension_host_pid": 4242,
            "targets": {
                "codex": {"capability_state": "available", "route": {"thread_id": THREAD, "session_id": THREAD}},
                "manager_chat": {"capability_state": "available", "route": {"window_id": "w1"}},
            },
        }
    )

    result = manager_lane.claim_callback_lane()
    assert result["ok"] is True
    assert result["reason"] == manager_lane.CLAIM_OK
    assert result["previous"] == {"provider": "codex", "session_id": THREAD}
    assert result["selected"] == {"provider": "manager_chat", "session_id": CHAT}

    raw = json.loads(lane.targets_path.read_text(encoding="utf-8"))
    assert raw["selected_provider"] == "manager_chat"
    assert raw["repo_id"] == "repo_1"
    assert raw["extension_host_pid"] == 4242
    assert raw["targets"]["codex"]["route"] == {"thread_id": THREAD, "session_id": THREAD}
    assert raw["targets"]["manager_chat"]["capability_state"] == "available"
    assert raw["targets"]["manager_chat"]["route"]["window_id"] == "w1"
    assert raw["targets"]["manager_chat"]["route"]["session_id"] == CHAT
    assert sorted(path.name for path in lane.targets_path.parent.iterdir()) == ["coordinator-targets.json"]

    selected = core.read_selected_coordinator_target()
    assert selected["selected_provider"] == "manager_chat"
    assert selected["targets"]["manager_chat"]["route"]["session_id"] == CHAT

    (row,) = lane.audit_rows()
    assert row["tool_name"] == manager_lane.CLAIM_TOOL
    assert row["blocked_reason"] == ""
    assert row["timestamp"].endswith("+00:00")
    action = json.loads(row["action"])
    assert action["outcome"] == "claimed"
    assert action["reason"] == manager_lane.CLAIM_OK
    assert action["actor"] == {"provider": "manager_chat", "session_id": CHAT}
    assert action["previous"] == {"provider": "codex", "session_id": THREAD}
    assert action["new"] == {"provider": "manager_chat", "session_id": CHAT}


def test_claim_creates_the_target_file_when_absent(lane) -> None:
    lane.identity = dict(LIVE_CODEX)
    lane.codex = dict(LIVE_CODEX)
    result = manager_lane.claim_callback_lane()
    assert result["ok"] is True
    raw = json.loads(lane.targets_path.read_text(encoding="utf-8"))
    assert raw["schema_id"] == manager_lane.TARGETS_SCHEMA_ID
    selected = core.read_selected_coordinator_target()
    assert selected["selected_provider"] == "codex"
    assert selected["targets"]["codex"]["route"]["thread_id"] == THREAD


@pytest.mark.parametrize(
    "setup, reason",
    [
        ("unverified", manager_lane.CLAIM_UNVERIFIED),
        ("codex_not_live", manager_lane.CLAIM_CODEX_NOT_LIVE),
        ("other_chat", manager_lane.CLAIM_HELD_BY_MANAGER_CHAT),
        ("other_codex", manager_lane.CLAIM_HELD_BY_CODEX),
        ("corrupt_targets", manager_lane.CLAIM_TARGETS_UNREADABLE),
    ],
)
def test_claim_refusals_leave_the_file_unchanged_and_are_audited(lane, setup, reason) -> None:
    before = lane.write_targets(
        {"schema_id": manager_lane.TARGETS_SCHEMA_ID, "selected_provider": "codex", "keep": True}
    )
    lane.identity = dict(MANAGER_CHAT_CALLER)
    lane.chat = {"session_id": CHAT}
    if setup == "unverified":
        lane.identity = {}
    elif setup == "codex_not_live":
        lane.identity = dict(PENDING_CODEX)
        lane.chat = None
        lane.codex = dict(PENDING_CODEX)
    elif setup == "other_chat":
        lane.chat = {"session_id": OTHER_CHAT}
    elif setup == "other_codex":
        lane.codex = dict(LIVE_CODEX, session_id=OTHER_THREAD, thread_id=OTHER_THREAD)
    elif setup == "corrupt_targets":
        before = lane.write_targets("{not json")

    result = manager_lane.claim_callback_lane()
    assert result["ok"] is False
    assert result["reason"] == reason
    assert lane.targets_path.read_bytes() == before
    (row,) = lane.audit_rows()
    assert row["blocked_reason"] == reason
    action = json.loads(row["action"])
    assert action["outcome"] == "refused"
    assert action["reason"] == reason
    assert action["new"] == action["previous"]


def test_claim_reasons_are_distinct() -> None:
    reasons = [
        manager_lane.CLAIM_OK,
        manager_lane.CLAIM_UNVERIFIED,
        manager_lane.CLAIM_CODEX_NOT_LIVE,
        manager_lane.CLAIM_HELD_BY_MANAGER_CHAT,
        manager_lane.CLAIM_HELD_BY_CODEX,
        manager_lane.CLAIM_TARGETS_UNREADABLE,
        manager_lane.CLAIM_WRITE_FAILED,
    ]
    assert len(set(reasons)) == len(reasons)


# --- tool surface -------------------------------------------------------------------------


def test_claim_tool_is_a_manager_only_write_tool() -> None:
    assert dict(mcp_tool_metadata.MCP_TOOL_PROFILES[manager_lane.CLAIM_TOOL]) == {
        mcp_tool_metadata.MCP_PROFILE_MANAGER: mcp_tool_metadata.MCP_VISIBILITY_LISTED
    }
    doc = server.aiworkhub_manager_callback_lane_claim.__doc__ or ""
    assert doc.lstrip().startswith("MANAGER WRITE:")


def test_claim_tool_delegates_to_the_module(monkeypatch) -> None:
    monkeypatch.setattr(server.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(manager_lane, "claim_callback_lane", lambda: {"ok": True, "reason": "x"})
    assert server.aiworkhub_manager_callback_lane_claim() == {"ok": True, "reason": "x"}


def test_claim_tool_refuses_when_the_write_gate_is_closed(monkeypatch, lane) -> None:
    before = lane.write_targets({"selected_provider": "codex", "other": 1})

    def must_not_run() -> dict[str, Any]:
        raise AssertionError("claim_callback_lane ran behind a closed write gate")

    monkeypatch.setattr(server.core, "writes_allowed", lambda: False)
    monkeypatch.setattr(manager_lane, "claim_callback_lane", must_not_run)
    assert server.aiworkhub_manager_callback_lane_claim() == {"ok": False, "error": "write_gate_closed"}
    assert lane.targets_path.read_bytes() == before
