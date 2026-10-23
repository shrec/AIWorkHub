"""NF-2026-01664: dashboard health and the compact snapshot carry the contract digest, not its prose."""

from __future__ import annotations

import copy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, dashboard, dashboard_mcp_app  # noqa: E402

_CONTRACT_KEYS = frozenset(core.MANAGER_CONTRACT)
_DELIVERY_FACTS = frozenset({"contract_delivered", "contract_delivery_reason"})
_OMISSION_FACTS = frozenset({"contract_omitted_fields", "contract_recall"})

_SNAPSHOT: dict[str, Any] = {
    "schema_version": 1,
    "readonly": True,
    "health": {"ok": True, "degraded": False, "provider_error_count": 0},
    "status_counts": {
        "pending": 1,
        "processing": 1,
        "review": 0,
        "blocked": 0,
        "finished": 0,
        "archived": 0,
        "stale": 0,
        "active": 2,
    },
    "row_counts": {"pending": {"returned": 1, "exact": 1, "truncated": False}},
    "tasks": {
        "pending": [{"task_id": "TASK_NF01664_PENDING_V1", "topic": "coding", "runner": "runner_a"}],
        "processing": [],
        "review": [],
        "blocked": [],
        "finished": [],
        "archived": [],
        "stale": [],
    },
    "summaries": {"topics": [], "runners": []},
    "completion_inbox": {},
    "cost_usage": {"totals": {"available": False}, "ledger": {}},
    "collision_report": {},
    "agent_processes": {"processes": []},
    "callback_bridge_health": {},
    "warnings": {"stale": [], "collisions": [], "runner_mismatches": []},
    "errors": [],
}


@pytest.fixture(autouse=True)
def _isolate_dashboard(monkeypatch):
    """Ensure no test reaches a real taskctl/SQLite call or host-owned provider state."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("dashboard data sources must be monkeypatched per test")

    monkeypatch.setattr(dashboard, "build_snapshot", _boom)
    monkeypatch.setattr(dashboard, "build_task_detail", _boom)
    monkeypatch.setattr(core, "health", _boom)
    monkeypatch.setattr(dashboard_mcp_app.cli_model_discovery, "codex_models", lambda **_kwargs: [])
    monkeypatch.setattr(dashboard_mcp_app.cli_model_discovery, "claude_models", lambda _root: [])


def _manager_reply() -> dict[str, Any]:
    """A manager_bootstrap route-gate reply: identity facts plus the contract prose."""

    reply: dict[str, Any] = {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo_id": "repo_canon",
        "storage_ready": True,
        "manager_verified": True,
        "manager_route": {"thread_id": "019f5097-6dbe-7172-870a-945afc5f3bfa"},
        "task_health": {"pending": 1, "processing": 1},
        "contract_version": core.MANAGER_CONTRACT_VERSION,
        "contract_sha256": core.MANAGER_CONTRACT_SHA256,
        "contract_delivered": True,
        "contract_delivery_reason": "route_gate_call",
    }
    reply.update(copy.deepcopy(core.MANAGER_CONTRACT))
    return reply


def _serving(reply: dict[str, Any]) -> Callable[[], dict[str, Any]]:
    return lambda: copy.deepcopy(reply)


def _stub_health_sources(monkeypatch) -> None:
    """Serve one hermetic health probe: no repository binding, SQLite or storage scan."""

    def no_repository() -> Path:
        raise RuntimeError("repository_root_not_found")

    monkeypatch.setattr(core, "repo_root", no_repository)
    monkeypatch.setattr(core, "dispatcher_health", lambda: {"ok": True, "status": "running"})


def _stub_snapshot_sources(monkeypatch) -> None:
    """Serve one hermetic snapshot: no SQLite, repository scan or router read."""

    monkeypatch.setattr(dashboard, "build_snapshot", lambda **_kwargs: dict(_SNAPSHOT))
    monkeypatch.setattr(core, "dispatcher_health", lambda: {"ok": True, "status": "running"})
    monkeypatch.setattr(core, "repo_root", lambda: Path("/"))
    monkeypatch.setattr(core, "read_selected_coordinator_target", lambda _root: {})
    monkeypatch.setattr(
        dashboard_mcp_app.shared_router, "list_known_repositories", lambda **_kwargs: {}
    )
    monkeypatch.setattr(dashboard_mcp_app.storage_observability, "snapshot", lambda _root: {})


def _health_identity(monkeypatch, bootstrap: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    monkeypatch.setattr(core, "manager_bootstrap", bootstrap)
    _stub_health_sources(monkeypatch)
    return dashboard_mcp_app.health_view()["manager_identity"]


def _snapshot_identity(
    monkeypatch, bootstrap: Callable[[], dict[str, Any]], **arguments: Any
) -> dict[str, Any]:
    monkeypatch.setattr(core, "manager_bootstrap", bootstrap)
    _stub_snapshot_sources(monkeypatch)
    return dashboard_mcp_app.snapshot_view(**arguments)["manager_identity"]


_SURFACES = {"health": _health_identity, "compact_snapshot": _snapshot_identity}


@pytest.fixture(params=list(_SURFACES))
def served(request, monkeypatch):
    """(identity, reply): what a model-facing surface returns, and the bootstrap reply behind it."""

    identity = _SURFACES[request.param](monkeypatch, _serving(_manager_reply()))
    return identity, core.manager_bootstrap()


def test_default_identity_has_no_contract_prose(served):
    identity, _reply = served

    assert _CONTRACT_KEYS
    assert not _CONTRACT_KEYS & set(identity)


def test_default_identity_keeps_the_digest_and_says_why_the_prose_is_missing(served):
    identity, _reply = served

    assert identity["contract_sha256"] == core.MANAGER_CONTRACT_SHA256
    assert identity["contract_version"] == core.MANAGER_CONTRACT_VERSION
    assert identity["contract_delivered"] is False
    assert identity["contract_delivery_reason"] == "dashboard_identity_only"
    assert identity["contract_omitted_fields"] == sorted(core.MANAGER_CONTRACT)
    assert identity["contract_recall"] == dashboard_mcp_app._CONTRACT_RECALL
    assert "aiworkhub_manager_bootstrap(include_contract=true)" in identity["contract_recall"]


def test_default_identity_keeps_role_route_and_every_other_fact_unchanged(served):
    identity, reply = served

    assert identity["role"] == reply["role"] == "manager"
    assert identity["manager_route"] == reply["manager_route"]
    kept = {
        key: value
        for key, value in reply.items()
        if key not in _CONTRACT_KEYS and key not in _DELIVERY_FACTS
    }
    assert {key: identity[key] for key in kept} == kept
    assert set(identity) == set(kept) | _DELIVERY_FACTS | _OMISSION_FACTS


def test_default_identity_sheds_the_contract_bytes(served):
    identity, reply = served

    shed = dashboard_mcp_app._byte_len(reply) - dashboard_mcp_app._byte_len(identity)

    assert shed > dashboard_mcp_app._byte_len(core.MANAGER_CONTRACT) * 0.8


@pytest.mark.parametrize("digest", [None, "", "   "])
def test_snapshot_without_a_usable_digest_is_identity_only(monkeypatch, digest):
    identity = _snapshot_identity(
        monkeypatch, _serving(_manager_reply()), known_contract_sha256=digest
    )

    assert not _CONTRACT_KEYS & set(identity)
    assert identity["contract_delivered"] is False
    assert identity["contract_delivery_reason"] == "dashboard_identity_only"


def test_full_snapshot_keeps_the_whole_contract(monkeypatch):
    reply = _manager_reply()

    for arguments in ({}, {"known_contract_sha256": core.MANAGER_CONTRACT_SHA256}):
        identity = _snapshot_identity(monkeypatch, _serving(reply), full=True, **arguments)

        assert identity == reply
        assert {key: identity[key] for key in _CONTRACT_KEYS} == core.MANAGER_CONTRACT
        assert identity["contract_delivered"] is True
        assert identity["contract_delivery_reason"] == "route_gate_call"


@pytest.mark.parametrize("stale", ["0" * 64, "deadbeef", "not a digest"])
def test_stale_known_digest_still_returns_the_contract(monkeypatch, stale):
    reply = _manager_reply()

    identity = _snapshot_identity(monkeypatch, _serving(reply), known_contract_sha256=stale)

    assert identity == {**reply, "contract_delivery_reason": "contract_sha_changed"}


@pytest.mark.parametrize(
    "spell",
    [str, str.upper, lambda digest: f"  {digest}\n"],
    ids=["exact", "uppercase", "padded"],
)
def test_matching_known_digest_still_reports_the_caller_holds_the_contract(monkeypatch, spell):
    identity = _snapshot_identity(
        monkeypatch,
        _serving(_manager_reply()),
        known_contract_sha256=spell(core.MANAGER_CONTRACT_SHA256),
    )

    assert not _CONTRACT_KEYS & set(identity)
    assert identity["contract_delivered"] is False
    assert identity["contract_delivery_reason"] == "caller_holds_current_contract"
    assert identity["contract_omitted_fields"] == sorted(core.MANAGER_CONTRACT)


@pytest.mark.parametrize("surface", list(_SURFACES))
def test_a_failed_bootstrap_identity_is_returned_untouched(monkeypatch, surface):
    def failing_bootstrap() -> dict[str, Any]:
        raise RuntimeError("bootstrap unavailable")

    identity = _SURFACES[surface](monkeypatch, failing_bootstrap)

    assert identity["ok"] is False
    assert identity["reason"] == "manager_bootstrap_failed:RuntimeError"
    assert not _CONTRACT_KEYS & set(identity)
    assert not (_DELIVERY_FACTS | _OMISSION_FACTS) & set(identity)


def test_known_contract_identity_defaults_to_dropping_the_prose_without_mutating_the_reply():
    reply = _manager_reply()
    before = copy.deepcopy(reply)

    identity = dashboard_mcp_app._known_contract_identity(reply)

    assert reply == before
    assert not _CONTRACT_KEYS & set(identity)
    assert identity["contract_delivery_reason"] == "dashboard_identity_only"


def test_known_contract_identity_leaves_a_reply_that_already_withheld_the_prose():
    reply = {key: value for key, value in _manager_reply().items() if key not in _CONTRACT_KEYS}
    reply.update(
        contract_delivered=False,
        contract_delivery_reason="already_delivered_this_session",
    )

    assert dashboard_mcp_app._known_contract_identity(reply) == reply
