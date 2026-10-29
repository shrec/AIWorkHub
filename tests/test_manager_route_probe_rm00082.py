"""RM-2026-00082: a Claude Code chat's MCP is never granted a Codex identity.

Measured on 0.12.74 (Windows, one Claude Code VS Code chat): bootstrap said
``manager_chat`` while accept_review and repo_current from the SAME chat
recorded ``{provider: codex}`` -- a stale Codex thread. The Claude route did
not verify, so ``_codex_manager_identity`` ran, and its Windows branch ended in
``_codex_shared_repo_route_manager_identity``, which granted Codex to ANY
repo-bound MCP whenever the shared VS Code registry held one live record with
``selected_provider=codex``. Nothing checked that the process descends from
Codex, and nothing said why the Claude route refused.

Run: python -m pytest -q tests/test_manager_route_probe_rm00082.py
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import callback_store, core, manager_route_probe, shared_router, task_store  # noqa: E402

_SESSION_ID = "9ea55703-f15e-4c35-8253-4e0c96516781"
_CODEX_THREAD = "019f5097-6dbe-7172-870a-945afc5f3bfa"
_ACTIVE_CHAT = "mls-" + "b0" * 16
_SAME_USER = "S-1-5-21-389243392-615521012-1854199069-1001"
_OTHER_USER = "S-1-5-21-000000000-000000000-000000000-1002"
_SERVER_PID = 2652
_STUB_PID = 38272
_CLAUDE_PID = 29612

_CLAUDE_CHAIN = {
    _SERVER_PID: (_STUB_PID, "python.exe"),
    _STUB_PID: (_CLAUDE_PID, "python.exe"),
    _CLAUDE_PID: (39124, "claude.exe"),
}
# A Codex chat's MCP: no claude anywhere in the same-user ancestry.
_CODEX_CHAIN = {
    _SERVER_PID: (_STUB_PID, "python.exe"),
    _STUB_PID: (5100, "python.exe"),
    5100: (5200, "codex.exe"),
    5200: (5300, "Code.exe"),
    5300: (4, "Code.exe"),
}


def _bind_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    tree: dict[int, tuple[int, str]],
    owners: dict[int, str] | None = None,
    parent_pid: int | None = None,
    descriptor_pid: int = _CLAUDE_PID,
    descriptor: dict | None = None,
) -> Path:
    """Wire the Windows walk to a fake Toolhelp tree on any host."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    home = tmp_path / "home"
    sessions = home / ".claude" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    body = {
        "pid": descriptor_pid,
        "kind": "interactive",
        "entrypoint": "claude-vscode",
        "sessionId": _SESSION_ID,
        "cwd": str(repo),
        **(descriptor or {}),
    }
    (sessions / f"{descriptor_pid}.json").write_text(json.dumps(body), encoding="utf-8")
    resolved_owners = owners if owners is not None else dict.fromkeys(tree, _SAME_USER)
    monkeypatch.setattr(manager_route_probe, "_windows", lambda: True)
    monkeypatch.setattr(
        manager_route_probe.os, "getppid",
        lambda: tree[_SERVER_PID][0] if parent_pid is None else parent_pid,
    )
    monkeypatch.setattr(manager_route_probe.os, "getpid", lambda: _SERVER_PID)
    monkeypatch.setattr(core, "_windows_process_tree", lambda: tree)
    monkeypatch.setattr(core, "_windows_process_owner_sid", lambda pid: resolved_owners.get(pid))
    monkeypatch.setattr(core, "repo_root", lambda: repo.resolve())
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(manager_route_probe.sys, "executable", r"D:\repo\.venv\Scripts\python.exe")
    monkeypatch.delenv("AIWORKHUB_MCP_SUPERVISED", raising=False)
    return repo


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for key, item in value.items() for s in _strings(key) + _strings(item)]
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in _strings(item)]
    return []


_REFUSALS = {
    "no_parent": dict(parent_pid=0),
    "owner_sid_mismatch": dict(owners={_SERVER_PID: _SAME_USER, _STUB_PID: _OTHER_USER, _CLAUDE_PID: _SAME_USER}),
    "hop_limit_exceeded": dict(tree={
        _SERVER_PID: (101, "python.exe"), 101: (102, "python.exe"), 102: (103, "py.exe"),
        103: (_CLAUDE_PID, "python.exe"), _CLAUDE_PID: (39124, "claude.exe"),
    }),
    "foreign_hop_image": dict(tree={
        _SERVER_PID: (_STUB_PID, "python.exe"), _STUB_PID: (_CLAUDE_PID, "cmd.exe"),
        _CLAUDE_PID: (39124, "claude.exe"),
    }),
    "no_claude_ancestor": dict(tree={_SERVER_PID: (_STUB_PID, "python.exe"), _STUB_PID: (777, "python.exe")}),
    "descriptor_unreadable": dict(descriptor_pid=999999),
    "descriptor_pid_mismatch": dict(descriptor={"pid": 4242}),
    "descriptor_kind": dict(descriptor={"kind": "headless-" + "x" * 200}),
    "descriptor_entrypoint": dict(descriptor={"entrypoint": "claude-cli"}),
    "descriptor_session_id": dict(descriptor={"sessionId": "not-a-uuid"}),
    "descriptor_cwd_mismatch": dict(descriptor={"cwd": "__elsewhere__"}),
}


@pytest.mark.parametrize("reason", sorted(_REFUSALS))
def test_the_probe_names_the_first_refusing_step(tmp_path, monkeypatch, reason):
    case = dict(_REFUSALS[reason])
    tree = case.pop("tree", _CLAUDE_CHAIN)
    if case.get("descriptor", {}).get("cwd") == "__elsewhere__":
        elsewhere = tmp_path / "other-repo"
        elsewhere.mkdir()
        case["descriptor"] = {"cwd": str(elsewhere)}
    repo = _bind_windows(monkeypatch, tmp_path, tree=tree, **case)

    probe = manager_route_probe.probe_claude_route()

    assert reason in manager_route_probe.REASONS
    assert (probe.identity, probe.reason) == (None, reason)
    assert core._claude_windows_manager_identity() is None
    assert core._claude_manager_identity() is None
    strings = _strings(probe.report())
    assert all(len(text) <= manager_route_probe.OBSERVATION_MAX_CHARS for text in strings)
    assert not any(str(repo) in text or str(tmp_path) in text or _SESSION_ID in text for text in strings)


def test_the_probe_verifies_with_the_same_identity_the_seat_uses(tmp_path, monkeypatch):
    _bind_windows(monkeypatch, tmp_path, tree=_CLAUDE_CHAIN)

    probe = manager_route_probe.probe_claude_route()

    expected = {"provider": "claude", "session_id": _SESSION_ID, "window_id": f"claude_vscode_{_CLAUDE_PID}"}
    assert (probe.reason, probe.identity) == ("verified", expected)
    assert core._claude_windows_manager_identity() == expected
    assert core._claude_manager_identity() == expected
    assert probe.observations == {
        "platform": "windows", "hop_count": 2, "hop_images": ["python.exe", "claude.exe"],
        "descriptor_kind": "interactive", "descriptor_entrypoint": "claude-vscode", "cwd_matches": True,
        "supervised": False,
    }


def test_the_posix_probe_reports_an_unreadable_parent(monkeypatch):
    monkeypatch.setattr(manager_route_probe, "_windows", lambda: False)
    monkeypatch.setattr(manager_route_probe.os, "getppid", lambda: 2_147_483_000)

    probe = manager_route_probe.probe_claude_route()

    assert (probe.identity, probe.reason) == (None, "no_parent")


@pytest.mark.parametrize("supervised, expected", [(True, "verified"), (False, "no_claude_ancestor")])
def test_the_supervised_posix_server_looks_past_the_supervisor(tmp_path, monkeypatch, supervised, expected):
    # NF-2026-01816: claude -> stable launcher supervisor -> server. Only the
    # server the supervisor marked steps over its parent.
    _bind_windows(monkeypatch, tmp_path, tree=_CLAUDE_CHAIN)
    proc = {
        (_STUB_PID, "status"): b"Name:\tpython3\nPPid:\t%d\n" % _CLAUDE_PID,
        (_STUB_PID, "cmdline"): b"/usr/bin/python3\0/x/aiworkhub-mcp-server.py\0",
        (_CLAUDE_PID, "cmdline"): b"/usr/local/bin/claude\0--ide\0",
    }
    monkeypatch.setattr(manager_route_probe, "_windows", lambda: False)
    monkeypatch.setattr(manager_route_probe, "_proc_read", lambda pid, name: proc[(pid, name)])
    if supervised:
        monkeypatch.setenv("AIWORKHUB_MCP_SUPERVISED", str(_SERVER_PID))

    probe = manager_route_probe.probe_claude_route()

    assert (probe.reason, probe.observations["supervised"]) == (expected, supervised)


# --- fail closed: a Claude chat is never granted Codex -----------------------


@pytest.mark.parametrize("tree, owners, expected", [
    (_CLAUDE_CHAIN, None, True),
    # A shell between the server and claude does not hide the Claude chat.
    ({_SERVER_PID: (300, "python.exe"), 300: (301, "cmd.exe"), 301: (1, "claude.exe")}, None, True),
    (_CODEX_CHAIN, None, False),
    # The walk stops at the first process another user owns.
    (_CLAUDE_CHAIN, {_SERVER_PID: _SAME_USER, _STUB_PID: _OTHER_USER, _CLAUDE_PID: _SAME_USER}, False),
])
def test_claude_ancestry_is_same_user_and_bounded(tmp_path, monkeypatch, tree, owners, expected):
    _bind_windows(monkeypatch, tmp_path, tree=tree, owners=owners)

    assert manager_route_probe.claude_ancestry_present() is expected


def test_unreadable_windows_ancestry_fails_closed(tmp_path, monkeypatch):
    _bind_windows(monkeypatch, tmp_path, tree=_CLAUDE_CHAIN)
    monkeypatch.setattr(core, "_windows_process_tree", lambda: None)

    assert manager_route_probe.claude_ancestry_present() is True


@pytest.fixture
def codex_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    repo_id = task_store.storage_readiness(root).repo_id
    monkeypatch.setattr(core, "repo_root", lambda: root.resolve())
    registry = {"ok": True, "repositories": [{
        "current_repo": True, "repo_id": repo_id, "extension_host_alive": True, "stale": False,
        "selected_provider": "codex", "window_id": "window_1",
        "targets": {"codex": {"capability_state": "ready", "route": {"thread_id": _CODEX_THREAD}}},
    }]}
    monkeypatch.setattr(shared_router, "list_known_repositories", lambda **_kw: registry)
    target = {
        "selected_provider": "codex", "repo_id": repo_id, "window_id": "window_1",
        "extension_host_pid": 5300,
        "targets": {"codex": {"capability_state": "ready", "route": {}}},
    }
    monkeypatch.setattr(core, "read_selected_coordinator_target", lambda _root: target)
    monkeypatch.setattr(core, "_pid_in_same_uid_ancestor_chain", lambda pid, *, max_depth: True)
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", "window_1")
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "codex_vscode")
    monkeypatch.setenv("CODEX_THREAD_ID", _CODEX_THREAD)
    monkeypatch.setenv("VSCODE_IPC_HOOK_CLI", "ipc")
    return root


@pytest.mark.parametrize("route", [
    "_codex_shared_repo_route_manager_identity",
    "_codex_extension_route_manager_identity",
    "_codex_vscode_env_manager_identity",
])
def test_a_claude_chat_is_never_granted_codex(codex_repo, tmp_path, monkeypatch, route):
    _bind_windows(monkeypatch, tmp_path, tree=_CODEX_CHAIN)
    codex_identity = getattr(core, route)()
    assert codex_identity is not None and codex_identity["provider"] == "codex"

    _bind_windows(monkeypatch, tmp_path, tree=_CLAUDE_CHAIN)

    assert getattr(core, route)() is None


def test_the_windows_codex_branch_refuses_a_claude_chat(codex_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "os", SimpleNamespace(name="nt", environ={}))
    monkeypatch.setattr(core, "_codex_route_manager_identity_from_parent_chain", lambda: None)
    _bind_windows(monkeypatch, tmp_path, tree=_CODEX_CHAIN)
    assert core._codex_manager_identity()["thread_id"] == _CODEX_THREAD

    _bind_windows(monkeypatch, tmp_path, tree=_CLAUDE_CHAIN)

    assert core._codex_manager_identity() is None


# --- bootstrap reports the probe ---------------------------------------------


@pytest.fixture
def boot_repo(tmp_path, monkeypatch):
    root = tmp_path / "boot"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "_CONTRACT_DELIVERIES", {})
    monkeypatch.setattr(core, "_schedule_task_hygiene", lambda **_k: {})
    monkeypatch.setattr(core, "_manager_chat_seat_identity", lambda: None)
    return root


_PROBE = manager_route_probe.ClaudeRouteProbe(
    None, "descriptor_entrypoint", {"platform": "windows", "hop_count": 3, "descriptor_entrypoint": "claude-cli"},
)


def test_bootstrap_reports_why_the_claude_route_did_not_verify(boot_repo, monkeypatch):
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: {
        "provider": "codex", "session_id": _CODEX_THREAD, "thread_id": _CODEX_THREAD, "window_id": "w",
    })
    monkeypatch.setattr(manager_route_probe, "probe_claude_route", lambda: _PROBE)

    reply = core.manager_bootstrap()

    assert reply["provider"] == "codex"
    assert reply["claude_route_probe"] == _PROBE.report()


def test_bootstrap_omits_the_probe_on_a_verified_claude_route(boot_repo, monkeypatch):
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: {
        "provider": "claude", "session_id": _SESSION_ID, "window_id": "claude_vscode_1",
    })
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    monkeypatch.setattr(manager_route_probe, "probe_claude_route", lambda: _PROBE)

    reply = core.manager_bootstrap()

    assert reply["provider"] == "claude"
    assert "claude_route_probe" not in reply


def test_bootstrap_ensures_the_inbox_of_the_resolved_manager_chat_route(boot_repo, monkeypatch):
    state = boot_repo / ".aiworkhub" / "runtime" / "manager_loop"
    (state / "sessions").mkdir(parents=True, exist_ok=True)
    (state / "selected.json").write_text(json.dumps({"session_id": _ACTIVE_CHAT}), encoding="utf-8")
    (state / "sessions" / f"{_ACTIVE_CHAT}.json").write_text(json.dumps({
        "session_id": _ACTIVE_CHAT, "status": "active", "backend_id": "claude_cli",
        "model": "opus", "created_at": callback_store.utc_now(),
    }), encoding="utf-8")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(core, "_bootstrap_dispatcher", lambda route, provider: seen.append(
        (provider, route["session_id"])) or {"ensured": True})

    reply = core.manager_bootstrap(bootstrap_call=True)

    assert reply["provider"] == "manager_chat"
    assert seen == [("manager_chat", _ACTIVE_CHAT)]
    assert reply["dispatcher"] == {"ensured": True}
    assert "claude_route_probe" in reply


# --- _bootstrap_dispatcher: every callback-capable route ---------------------


def test_the_extension_owned_codex_skip_is_gone():
    assert "extension_owns_codex_dispatch" not in inspect.getsource(core._bootstrap_dispatcher)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_claude_and_codex_run_the_shared_ensure(monkeypatch, provider):
    calls: list[str] = []
    monkeypatch.setattr(core, "dispatcher_ensure_started", lambda: calls.append(provider) or {
        "ok": True, "status": "manager_inbox", "provider": provider, "rebound_callback_count": 2,
        "seeded_review_callback_count": 1, "manager_route": {"ignored": True},
    })

    result = core._bootstrap_dispatcher({"provider": provider, "session_id": _SESSION_ID}, provider)

    assert calls == [provider]
    assert result == {
        "ensured": True, "ok": True, "status": "manager_inbox", "provider": provider,
        "rebound_callback_count": 2, "seeded_review_callback_count": 1,
    }


def test_a_manager_chat_seat_rebinds_and_seeds_its_own_lane(monkeypatch):
    closed: list[bool] = []
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(core, "_canonical_connect", lambda: SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(core, "dispatcher_ensure_started", lambda: pytest.fail("seat must not start a dispatcher"))
    monkeypatch.setattr(callback_store, "rebind_pending_callbacks",
                        lambda conn, **kw: calls.append(("rebind", kw)) or 3)
    monkeypatch.setattr(callback_store, "seed_missing_review_callbacks",
                        lambda conn, **kw: calls.append(("seed", kw)) or 4)

    result = core._bootstrap_dispatcher({"provider": "manager_chat", "session_id": _ACTIVE_CHAT}, "manager_chat")

    expected_kwargs = {"provider": "manager_chat", "origin_thread_id": _ACTIVE_CHAT}
    assert calls == [("rebind", expected_kwargs), ("seed", expected_kwargs)]
    assert closed == [True]
    assert result["ensured"] is True
    assert (result["rebound_callback_count"], result["seeded_review_callback_count"]) == (3, 4)


@pytest.mark.parametrize("identity, provider", [
    (None, "codex"),
    ({}, "claude"),
    ({"provider": "manager_chat", "session_id": "window_1"}, "manager_chat"),
    ({"provider": "worker", "session_id": _SESSION_ID}, "worker"),
])
def test_no_verified_route_ensures_nothing(monkeypatch, identity, provider):
    monkeypatch.setattr(core, "dispatcher_ensure_started", lambda: pytest.fail("unverified route"))

    result = core._bootstrap_dispatcher(identity, provider)

    assert result == {"ensured": False, "status": "not_applicable", "reason": "manager_route_unverified"}
