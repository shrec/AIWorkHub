"""NF-2026-02415: the Claude manager contract, health and docs name the live wake.

A Claude Code manager fell back to a slow poll because nothing it reads named
``python -m aiworkhub.manager_callback_watch``.  This locks:

* the bootstrap ``callback["claude"]`` contract names the watcher command and
  the wake -> callback_wait -> ack loop, and still names callback_wait;
* ``dispatcher_health`` carries ``remediation`` (the single
  ``callback_store.MANAGER_INBOX_REMEDIATION``) exactly when the problem
  ``manager_inbox_no_live_delivery`` is present, and no such key otherwise;
* the Claude Code chat bullets in docs/CALLBACKS.md and docs/GETTING_STARTED.md
  name the watcher command and the loop.
"""
from __future__ import annotations

import re
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import callback_store, core  # noqa: E402

# The docs keep the plain `python -m ...` form; the published contract and
# remediation name the running interpreter (NF-2026-02438), so pin the module.
_WATCHER = "-m aiworkhub.manager_callback_watch"
_UNREGISTERED = {"dispatcher_running": False, "registered": False, "repo_id": "", "last_start_error": ""}
_BOUND = callback_store.CALLBACK_BACKLOG_MAX_AGE_SECONDS


def _patch_claude_inbox(monkeypatch, *, backlog: int, oldest: str) -> None:
    identity = {"provider": "claude", "session_id": str(uuid.uuid4()), "window_id": "claude_vscode_1"}
    readiness = types.SimpleNamespace(ready=True, repo_id="repo_canon", reason="")
    monkeypatch.setattr(core, "repo_root", lambda: Path("/tmp/aiworkhub-nf02415-repo"))
    monkeypatch.setattr(core.task_store, "storage_readiness", lambda root: readiness)
    monkeypatch.setattr(core.task_store, "callback_bridge_health",
                        lambda root: {"backlog_count": 0, "oldest_pending_at": ""})
    monkeypatch.setattr(core, "_verified_manager_backlog",
                        lambda provider, origin: {"backlog_count": backlog, "oldest_pending_at": oldest})
    fake_bridge = types.SimpleNamespace(dispatcher_health=lambda root: dict(_UNREGISTERED))
    monkeypatch.setattr(core, "_callback_bridge_module", lambda: fake_bridge)
    monkeypatch.setattr(core, "read_selected_coordinator_target",
                        lambda root=None: {"selected_provider": "claude"})
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: identity)
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", "")
    monkeypatch.setenv("AIWORKHUB_CALLBACK_TRANSPORT", "")


def _claude_contract_line() -> str:
    # The bootstrap dict's callback["claude"] is this one constant (NF-2026-02438).
    return core.CLAUDE_CALLBACK_CONTRACT


def test_claude_callback_contract_names_the_watcher_and_the_loop():
    contract = _claude_contract_line()
    assert "aiworkhub_dispatcher_ensure_started" in contract
    assert _WATCHER in contract
    assert "--repo <repo> --provider <provider> --origin-thread-id <manager session id>" in contract
    assert "once per session after bootstrap" in contract
    assert "aiworkhub_claude_callback_wait" in contract
    assert "immediately ack" in contract
    assert contract.index("aiworkhub_claude_callback_wait") > contract.index(_WATCHER)
    assert contract.endswith("then restart the watcher")


def test_remediation_names_the_watcher_and_the_loop():
    text = callback_store.MANAGER_INBOX_REMEDIATION
    assert _WATCHER in text
    assert "aiworkhub_claude_callback_wait" in text
    assert "aiworkhub_claude_callback_ack" in text
    assert "restart the watcher" in text


def test_health_with_no_live_delivery_carries_remediation(monkeypatch):
    stale = (datetime.now(timezone.utc) - timedelta(seconds=_BOUND + 3600)).isoformat()
    _patch_claude_inbox(monkeypatch, backlog=26, oldest=stale)
    h = core.dispatcher_health()
    assert h["problems"] == ["manager_inbox_no_live_delivery"]
    assert h["remediation"] == callback_store.MANAGER_INBOX_REMEDIATION
    assert "manager_callback_watch" in h["remediation"]


def test_health_without_that_problem_has_no_remediation_key(monkeypatch):
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _patch_claude_inbox(monkeypatch, backlog=1, oldest=fresh)
    h = core.dispatcher_health()
    assert h["problems"] == []
    assert "remediation" not in h


def test_docs_claude_code_bullets_name_the_watcher_and_the_loop():
    for name in ("CALLBACKS.md", "GETTING_STARTED.md"):
        text = (_ROOT / "docs" / name).read_text(encoding="utf-8")
        start = text.index("**Claude Code chat")
        end = text.index("- **Codex VS Code sidebar chat", start)
        bullet = " ".join(text[start:end].split())
        assert _WATCHER in bullet, name
        assert "--repo <repo> --provider claude --origin-thread-id <manager session id>" in bullet, name
        assert "once per session" in bullet.lower(), name
        assert "aiworkhub_claude_callback_wait" in bullet, name
        assert "aiworkhub_claude_callback_ack" in bullet, name
        assert "restart the watcher" in bullet, name


def test_codex_manager_inbox_gets_no_claude_remediation(monkeypatch):
    stale = (datetime.now(timezone.utc) - timedelta(seconds=_BOUND + 3600)).isoformat()
    _patch_claude_inbox(monkeypatch, backlog=0, oldest="")
    monkeypatch.setattr(core.task_store, "callback_bridge_health",
                        lambda root: {"backlog_count": 3, "oldest_pending_at": stale})
    monkeypatch.setattr(core, "read_selected_coordinator_target",
                        lambda root=None: {"selected_provider": "codex"})
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", "codex_window_1")
    monkeypatch.setenv("AIWORKHUB_CALLBACK_TRANSPORT", "manager_inbox")
    h = core.dispatcher_health()
    assert h["problems"] == ["manager_inbox_no_live_delivery"]
    assert "remediation" not in h
