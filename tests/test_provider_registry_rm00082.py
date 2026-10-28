"""RM-2026-00082: any model can be the manager; Codex is one provider, not the default.

Pins four invariants:
  1. ``route_identity.MANAGER_PROVIDERS`` is the only manager-provider set: no
     module under ``src/aiworkhub`` restates it as a literal.
  2. ``_current_chat_provider`` and ``read_selected_coordinator_target`` never
     answer ``"codex"`` without Codex evidence; no evidence selects nobody.
  3. A card owned by ``manager_chat`` keeps its review-callback binding and its
     callbacks are seeded and adopted like any other provider's.
  4. Accept records the verified manager -- a Manager Chat seat beats a stale
     Codex route file; a live Codex route with no seat is still Codex.

Run: python -m pytest -q tests/test_provider_registry_rm00082.py
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import callback_store, core, route_identity  # noqa: E402
from aiworkhub.process_launcher_accept_review import _accept_manager_identity  # noqa: E402

_PACKAGE = _SRC / "aiworkhub"
_LEGACY = frozenset({"codex", "claude", "copilot"})
_CHAT = "mls-" + "b0" * 16
_CODEX_THREAD = "019f5097-6dbe-7172-870a-945afc5f3bfa"
_CLAUDE_SESSION = "9ea55703-f15e-4c35-8253-4e0c96516781"
_SEAT = {"session_id": _CHAT, "backend_id": "claude_cli", "model": "opus", "status": "active"}


# --- 1. one registry ----------------------------------------------------------


def _provider_set_literals(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Tuple, ast.Set, ast.List)) or not node.elts:
            continue
        if not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
            continue
        names = {e.value for e in node.elts} - {""}
        if names in (_LEGACY, _LEGACY | {"manager_chat"}):
            lines.append(node.lineno)
    return lines


def test_manager_providers_is_the_one_registry_and_includes_manager_chat():
    assert route_identity.MANAGER_PROVIDERS == frozenset({"codex", "claude", "copilot", "manager_chat"})
    assert callback_store._CALLBACK_PROVIDERS == route_identity.MANAGER_PROVIDERS | {""}
    assert not hasattr(route_identity, "_SUPPORTED_PROVIDERS")


def test_no_module_restates_the_provider_set_outside_route_identity():
    offenders = {
        path.name: lines
        for path in sorted(_PACKAGE.glob("*.py"))
        if path.name != "route_identity.py"
        for lines in [_provider_set_literals(path)]
        if lines
    }
    assert offenders == {}


def test_the_scan_detects_a_restated_provider_set(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text('A = ("codex", "claude", "copilot")\nB = {"", "claude", "codex", "copilot", "manager_chat"}\n')
    assert _provider_set_literals(sample) == [1, 2]


def test_coordinator_route_key_accepts_manager_chat_with_a_session():
    key = route_identity.CoordinatorRouteKey(
        repo_id="repo_x", provider="manager_chat", window_id="w", task_id="T", session_id=_CHAT,
    )
    assert key.provider == "manager_chat"
    try:
        route_identity.CoordinatorRouteKey(repo_id="repo_x", provider="manager_chat", window_id="w", task_id="T")
    except ValueError as exc:
        assert "manager_chat callback route requires session_id" in str(exc)
    else:  # pragma: no cover - the assertion below names the failure
        raise AssertionError("manager_chat route without a session must fail closed")


# --- 2. no silent codex -------------------------------------------------------


def _no_route_evidence(monkeypatch, tmp_path, *, parent_cmdline: str = "/usr/bin/bash", seat=None):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    cmdline = tmp_path / "parent_cmdline"
    cmdline.write_bytes(parent_cmdline.encode("utf-8"))
    real_path = Path

    def _path(*parts):
        if parts and str(parts[0]).startswith("/proc/"):
            return cmdline
        return real_path(*parts)

    monkeypatch.setattr(core, "Path", _path)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_active_manager_chat_record", lambda: dict(seat) if seat else None)
    return root


def _write_route(root: Path, provider: str, thread_id: str = "") -> None:
    route_dir = root / ".aiworkhub" / "config" / "routing"
    route_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_id": "aiworkhub.coordinator_targets.v1",
        "selected_provider": provider,
        "targets": {"codex": {"provider": "codex", "capability_state": "available",
                              "route": {"thread_id": thread_id, "session_id": thread_id}}},
    }
    (route_dir / "coordinator-targets.json").write_text(json.dumps(payload), encoding="utf-8")


def test_current_chat_provider_is_nobody_without_any_evidence(monkeypatch, tmp_path):
    _no_route_evidence(monkeypatch, tmp_path)
    assert core._current_chat_provider() == ""
    assert core._current_chat_provider({"coordinator_provider": ""}) == ""


def test_current_chat_provider_is_manager_chat_when_only_a_seat_is_active(monkeypatch, tmp_path):
    _no_route_evidence(monkeypatch, tmp_path, seat=_SEAT)
    assert core._current_chat_provider() == "manager_chat"
    assert core._current_chat_provider({"coordinator_provider": "bogus"}) == "manager_chat"


def test_current_chat_provider_keeps_an_explicit_card_provider(monkeypatch, tmp_path):
    _no_route_evidence(monkeypatch, tmp_path, seat=_SEAT)
    assert core._current_chat_provider({"coordinator_provider": "codex"}) == "codex"
    assert core._current_chat_provider({"coordinator_provider": "Manager_Chat"}) == "manager_chat"


def test_current_chat_provider_is_codex_for_a_codex_parent_process(monkeypatch, tmp_path):
    _no_route_evidence(monkeypatch, tmp_path, parent_cmdline="codex app-server", seat=_SEAT)
    assert core._current_chat_provider() == "codex"


def test_current_chat_provider_is_codex_for_a_selected_codex_route(monkeypatch, tmp_path):
    root = _no_route_evidence(monkeypatch, tmp_path, seat=_SEAT)
    _write_route(root, "codex", _CODEX_THREAD)
    assert core._current_chat_provider() == "codex"


def test_current_chat_provider_never_guesses_codex_from_an_invalid_route(monkeypatch, tmp_path):
    root = _no_route_evidence(monkeypatch, tmp_path)
    _write_route(root, "not_a_provider", _CODEX_THREAD)
    assert core._current_chat_provider() == ""


def test_selected_target_names_nobody_for_a_missing_corrupt_or_invalid_route(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    root = tmp_path / "repo"
    root.mkdir()
    assert core.read_selected_coordinator_target(root)["selected_provider"] == ""
    route_dir = root / ".aiworkhub" / "config" / "routing"
    route_dir.mkdir(parents=True)
    route_file = route_dir / "coordinator-targets.json"
    route_file.write_text("{not json", encoding="utf-8")
    assert core.read_selected_coordinator_target(root)["selected_provider"] == ""
    route_file.write_text(json.dumps({"selected_provider": "bogus"}), encoding="utf-8")
    assert core.read_selected_coordinator_target(root)["selected_provider"] == ""
    route_file.write_text(json.dumps(["codex"]), encoding="utf-8")
    assert core.read_selected_coordinator_target(root)["selected_provider"] == ""


def test_selected_target_accepts_manager_chat_from_the_route_file(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    root = tmp_path / "repo"
    root.mkdir()
    _write_route(root, "manager_chat")
    assert core.read_selected_coordinator_target(root)["selected_provider"] == "manager_chat"


# --- 3. manager_chat cards bind, seed and adopt ------------------------------


def test_review_target_binding_accepts_a_manager_chat_card(monkeypatch, tmp_path):
    card = {
        "origin_thread_id": _CHAT,
        "coordinator_provider": "manager_chat",
        "manager_chat_session_id": _CHAT,
        "manager_route_state": "ready",
    }
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(core.task_store, "get_task", lambda root, task_id: dict(card))
    assert core._review_target_callback_binding("TASK_TARGET") == (_CHAT, _CHAT, "manager_chat", "ready")


def _make_db(tmp_path):
    conn = callback_store.open_db(tmp_path / "task_queue.sqlite")
    callback_store.init_db(conn)
    return conn


def _review_task(conn, task_id: str, origin: str, provider: str) -> None:
    now = callback_store.utc_now()
    card = {
        "task_id": task_id, "status": "review", "worker_status": "review",
        "coordinator_provider": provider, "origin_thread_id": origin, "claim_epoch": 1,
        "terminal_substatus": "review_ready",
    }
    conn.execute(
        "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, objective, "
        "card_json, created_at, updated_at, origin_thread_id, archived_at) "
        "VALUES (?, 'r', 'task_mcp', 'review', 'review', 'high', 'o', ?, ?, ?, ?, '')",
        (task_id, json.dumps(card), now, now, origin),
    )
    conn.commit()


def _pending(conn, task_id: str) -> list[tuple[str, str]]:
    return [
        (row["provider"], row["origin_thread_id"]) for row in conn.execute(
            "SELECT provider, origin_thread_id FROM callback_outbox "
            "WHERE task_id=? AND state='pending' ORDER BY outbox_id",
            (task_id,),
        )
    ]


def test_a_manager_chat_card_is_seeded_on_the_manager_chat_lane(tmp_path):
    conn = _make_db(tmp_path)
    _review_task(conn, "TASK_CHAT_SEED", _CHAT, "manager_chat")

    seeded = callback_store.seed_missing_review_callbacks(conn, provider="manager_chat")

    assert seeded == 1
    assert _pending(conn, "TASK_CHAT_SEED") == [("manager_chat", _CHAT)]


def test_adoption_takes_the_lane_wake_of_a_manager_chat_card(tmp_path):
    conn = _make_db(tmp_path)
    _review_task(conn, "TASK_CHAT_ADOPT", _CODEX_THREAD, "manager_chat")
    now = callback_store.utc_now()
    conn.execute(
        "INSERT INTO callback_outbox(task_id, provider, origin_thread_id, transition, episode_id, "
        "request_id, state, created_at, updated_at) "
        "VALUES ('TASK_CHAT_ADOPT', 'codex', ?, 'review_ready', '1', '', 'pending', ?, ?)",
        (_CODEX_THREAD, now, now),
    )
    conn.commit()

    result = callback_store.adopt_pending_callbacks(
        conn, provider="claude", origin_thread_id=_CLAUDE_SESSION,
    )

    assert result["adopted"] == 1
    assert _pending(conn, "TASK_CHAT_ADOPT") == [("claude", _CLAUDE_SESSION)]


def test_route_resolution_by_thread_accepts_manager_chat(monkeypatch):
    from aiworkhub import shared_router

    monkeypatch.setattr(shared_router, "list_known_repositories", lambda limit=256: {"ok": True, "repositories": []})
    assert shared_router.resolve_repository_route(provider="bogus", thread_id=_CHAT)["error"] == "provider_invalid"
    assert shared_router.resolve_repository_route(provider="manager_chat", thread_id=_CHAT).get("error") != "provider_invalid"


# --- 4. accept records the verified manager -----------------------------------


def _stale_codex_identity() -> dict[str, str]:
    return {
        "provider": "codex",
        "session_id": _CODEX_THREAD,
        "thread_id": _CODEX_THREAD,
        "window_id": "window_stale",
        "callback_supported": "true",
        "route_state": "available",
    }


def test_accept_records_the_manager_chat_seat_despite_a_stale_codex_route(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_route(root, "codex", _CODEX_THREAD)  # the stale route file the live accept read
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "_manager_chat_seat_identity", lambda: None)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", _stale_codex_identity)
    monkeypatch.setattr(core, "_active_manager_chat_record", lambda: dict(_SEAT))

    identity = _accept_manager_identity(core)

    assert identity["provider"] == "manager_chat"
    assert identity["provider"] != "codex"
    assert identity["session_id"] == _CHAT
    assert identity == core.stamped_manager_identity()


def test_accept_records_a_live_codex_manager_when_no_seat_is_active(monkeypatch):
    monkeypatch.setattr(core, "_manager_chat_seat_identity", lambda: None)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", _stale_codex_identity)
    monkeypatch.setattr(core, "_active_manager_chat_record", lambda: None)

    identity = _accept_manager_identity(core)

    assert identity["provider"] == "codex"
    assert identity["thread_id"] == _CODEX_THREAD


def test_accept_never_records_a_route_pending_codex_route_as_the_manager(monkeypatch):
    pending = {
        "provider": "codex", "session_id": "episode_pending", "thread_id": "",
        "window_id": "window_extension_owned", "callback_supported": "false",
        "route_state": "route_pending",
    }
    monkeypatch.setattr(core, "_manager_chat_seat_identity", lambda: None)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: dict(pending))
    monkeypatch.setattr(core, "_active_manager_chat_record", lambda: None)

    assert _accept_manager_identity(core) == {}


def test_accept_keeps_a_verified_claude_manager_over_an_open_seat(monkeypatch):
    claude = {"provider": "claude", "session_id": _CLAUDE_SESSION, "window_id": "claude_vscode_1"}
    monkeypatch.setattr(core, "_manager_chat_seat_identity", lambda: None)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: dict(claude))
    monkeypatch.setattr(core, "_active_manager_chat_record", lambda: dict(_SEAT))

    assert _accept_manager_identity(core) == claude
