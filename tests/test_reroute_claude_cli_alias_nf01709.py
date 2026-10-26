"""NF-2026-01709: a pristine reroute matches the catalog model after canonicalization."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, process_launcher, task_store, workforce_catalog  # noqa: E402

_NOW = "2026-10-26T00:00:00+00:00"
_FROM_RUNNER = "claude_opus-5"
_TO_RUNNER = "claude_sonnet-5"
_ADAPTER = "claude_cli"
_ALIAS = "sonnet"
_MANAGER = "nf01709-manager"


def _claude_route(**overrides) -> dict:
    route = {
        "execution_runner": _TO_RUNNER,
        "effective_adapter_id": _ADAPTER,
        "model": _ALIAS,
        "implementation_worker": True,
        "enabled": True,
        "available": True,
        "launch_eligible": True,
        "max_risk": "critical",
    }
    route.update(overrides)
    return route


@pytest.fixture
def repo(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: _MANAGER)
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)
    return root


def _insert_pristine(root: Path, task_id: str) -> None:
    card = {
        "task_id": task_id,
        "runner": _FROM_RUNNER,
        "topic": "terminal_retry",
        "objective": "reroute to the claude sonnet route",
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": None,
        "allowed_writes": ["out/result.json"],
        "risk_tier": "low",
        "history": [],
    }
    conn = sqlite3.connect(task_store.storage_readiness(root).canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,claimed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, _FROM_RUNNER, "terminal_retry", "solo", "pending", "unclaimed",
             "normal", "reroute to the claude sonnet route",
             json.dumps(card, ensure_ascii=False, sort_keys=True), _NOW, _NOW, None),
        )
        conn.commit()
    finally:
        conn.close()


def _row(root: Path, task_id: str) -> dict:
    conn = sqlite3.connect(task_store.storage_readiness(root).canonical_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return dict(row)


def _reroute(task_id: str, model: str) -> dict:
    return core.reroute_launch_identity(
        task_id,
        from_runner=_FROM_RUNNER,
        to_runner=_TO_RUNNER,
        to_adapter_id=_ADAPTER,
        to_model=model,
        reason="owner quota reroute",
    )


def test_alias_canonicalizes_to_the_pinned_model():
    pinned = process_launcher._normalize_workforce_model(_ALIAS, _ADAPTER)

    assert pinned != _ALIAS
    assert process_launcher._normalize_workforce_model(pinned, _ADAPTER) == pinned


@pytest.mark.parametrize("spelling", ["alias", "canonical"])
def test_pristine_card_reroutes_onto_a_route_whose_catalog_model_is_the_alias(
    monkeypatch, repo, spelling
):
    pinned = process_launcher._normalize_workforce_model(_ALIAS, _ADAPTER)
    to_model = _ALIAS if spelling == "alias" else pinned
    task_id = "NF01709_REROUTE_" + spelling.upper()
    _insert_pristine(repo, task_id)
    monkeypatch.setattr(
        workforce_catalog, "build_catalog", lambda *_: {"workers": [_claude_route()]}
    )

    result = _reroute(task_id, to_model)

    assert result["ok"] is True, result
    assert result["to_runner"] == _TO_RUNNER
    row = _row(repo, task_id)
    assert row["runner"] == _TO_RUNNER
    assert (row["status"], row["worker_status"]) == ("pending", "unclaimed")
    receipt = json.loads(row["card_json"])["identity_reroute"]
    assert receipt["to_adapter_id"] == _ADAPTER
    assert receipt["to_model"] == pinned


def test_a_route_whose_canonical_model_differs_is_still_refused(monkeypatch, repo):
    task_id = "NF01709_DIFFERENT_MODEL"
    _insert_pristine(repo, task_id)
    monkeypatch.setattr(
        workforce_catalog,
        "build_catalog",
        lambda *_: {"workers": [_claude_route(model="opus")]},
    )
    before = _row(repo, task_id)

    result = _reroute(task_id, _ALIAS)

    assert result["ok"] is False
    assert result["stderr"] == "reroute_target_rejected:implementation_worker_role_required"
    assert _row(repo, task_id) == before
