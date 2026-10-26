"""NF-2026-01602: reroute accepts any adapter in the runner's identity tuple."""
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
_FROM_RUNNER = "claude_sonnet-4.6"
_TO_RUNNER = "glm_5.3"
_CANONICAL_ADAPTER = "glm_vscode_lm"
_MODEL = "glm-5.3"
_MANAGER = "nf01602-manager"


def _glm_route(**overrides) -> dict:
    route = {
        "execution_runner": _TO_RUNNER,
        "effective_adapter_id": _CANONICAL_ADAPTER,
        "model": _MODEL,
        "implementation_worker": True,
        "enabled": True,
        "available": True,
        "launch_eligible": True,
        "max_risk": "high",
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
    # The catalog path is the one under test: no static row may short-cut it.
    monkeypatch.setattr(
        process_launcher,
        "_CANONICAL_WORKFORCE",
        {
            key: value
            for key, value in process_launcher._CANONICAL_WORKFORCE.items()
            if key[0] != _TO_RUNNER
        },
    )
    return root


def _insert_pristine(root: Path, task_id: str) -> None:
    card = {
        "task_id": task_id,
        "runner": _FROM_RUNNER,
        "topic": "terminal_retry",
        "objective": "reroute to the glm route",
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
             "normal", "reroute to the glm route",
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


def _reroute_event(root: Path, task_id: str) -> dict | None:
    conn = sqlite3.connect(task_store.storage_readiness(root).canonical_db)
    try:
        row = conn.execute(
            "SELECT payload_json FROM task_events "
            "WHERE task_id=? AND event='reroute_launch_identity'",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def _reroute(task_id: str, adapter: str, model: str = _MODEL) -> dict:
    return core.reroute_launch_identity(
        task_id,
        from_runner=_FROM_RUNNER,
        to_runner=_TO_RUNNER,
        to_adapter_id=adapter,
        to_model=model,
        reason="owner quota reroute",
    )


def test_identity_tuple_names_both_spellings():
    identity = process_launcher.adapter_identity_tuple(_TO_RUNNER)

    assert "vscode_lm" in identity
    assert _CANONICAL_ADAPTER in identity


@pytest.mark.parametrize("adapter", ["vscode_lm", _CANONICAL_ADAPTER])
def test_reroute_accepts_a_tuple_adapter_and_records_the_catalog_adapter(
    monkeypatch, repo, adapter
):
    task_id = "NF01602_ALIAS_" + adapter.upper()
    _insert_pristine(repo, task_id)
    monkeypatch.setattr(
        workforce_catalog, "build_catalog", lambda *_: {"workers": [_glm_route()]}
    )

    result = _reroute(task_id, adapter)

    assert result["ok"] is True, result
    assert result["to_runner"] == _TO_RUNNER
    assert result["to_model"] == _MODEL
    row = _row(repo, task_id)
    assert row["runner"] == _TO_RUNNER
    assert (row["status"], row["worker_status"]) == ("pending", "unclaimed")
    receipt = json.loads(row["card_json"])["identity_reroute"]
    assert receipt["to_adapter_id"] == _CANONICAL_ADAPTER
    assert receipt["to_model"] == _MODEL
    assert receipt["from_runner"] == _FROM_RUNNER
    event = _reroute_event(repo, task_id)
    assert event is not None
    assert event["to_adapter_id"] == _CANONICAL_ADAPTER


@pytest.mark.parametrize("adapter", ["claude_cli", "codex_cli", "opencode_cli", "bogus"])
def test_reroute_refuses_an_adapter_outside_the_identity_tuple(monkeypatch, repo, adapter):
    task_id = "NF01602_OUTSIDE_" + adapter.upper()
    _insert_pristine(repo, task_id)
    monkeypatch.setattr(
        workforce_catalog, "build_catalog", lambda *_: {"workers": [_glm_route()]}
    )
    before = _row(repo, task_id)

    result = _reroute(task_id, adapter)

    assert result["ok"] is False
    assert result["stderr"] == (
        "reroute_target_rejected:workforce_route_absent:"
        f"runner={_TO_RUNNER}:adapter={adapter}"
    )
    assert _row(repo, task_id) == before
    assert _reroute_event(repo, task_id) is None


def test_alias_is_refused_when_the_runner_has_more_than_one_catalog_row(monkeypatch, repo):
    task_id = "NF01602_TWO_ROWS"
    _insert_pristine(repo, task_id)
    rows = [_glm_route(), _glm_route(effective_adapter_id="glm_copilot_cli")]
    monkeypatch.setattr(workforce_catalog, "build_catalog", lambda *_: {"workers": rows})
    before = _row(repo, task_id)

    result = _reroute(task_id, "vscode_lm")

    assert result["ok"] is False
    assert result["stderr"] == (
        f"reroute_target_rejected:workforce_route_absent:runner={_TO_RUNNER}:adapter=vscode_lm"
    )
    assert _row(repo, task_id) == before


def test_alias_is_refused_when_the_single_row_names_an_adapter_outside_the_tuple(
    monkeypatch, repo
):
    task_id = "NF01602_ROW_OUTSIDE"
    _insert_pristine(repo, task_id)
    monkeypatch.setattr(
        workforce_catalog,
        "build_catalog",
        lambda *_: {"workers": [_glm_route(effective_adapter_id="opencode_cli")]},
    )

    result = _reroute(task_id, "vscode_lm")

    assert result["ok"] is False
    assert "workforce_route_absent" in result["stderr"]


@pytest.mark.parametrize(
    ("overrides", "model", "reason"),
    [
        ({}, "glm-4.9", "workforce_model_mismatch"),
        ({"enabled": False}, _MODEL, "workforce_route_disabled"),
        ({"available": False}, _MODEL, "workforce_route_unavailable"),
        ({"launch_eligible": False}, _MODEL, "workforce_route_unavailable"),
        ({"max_risk": "invalid"}, _MODEL, "workforce_route_risk_incapable"),
    ],
)
def test_later_route_checks_still_apply_to_the_alias(
    monkeypatch, repo, overrides, model, reason
):
    task_id = "NF01602_LATER_" + reason.upper()
    _insert_pristine(repo, task_id)
    monkeypatch.setattr(
        workforce_catalog, "build_catalog", lambda *_: {"workers": [_glm_route(**overrides)]}
    )
    before = _row(repo, task_id)

    result = _reroute(task_id, "vscode_lm", model)

    assert result["ok"] is False
    assert result["stderr"].startswith(f"reroute_target_rejected:{reason}:"), result
    assert _row(repo, task_id) == before
