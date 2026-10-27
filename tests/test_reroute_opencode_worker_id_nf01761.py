"""NF-2026-01761: reroute accepts a discovered opencode catalog worker_id."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    core,
    opencode_auth,
    process_launcher,
    task_store,
    workforce_catalog,
)

_NOW = "2026-10-27T00:00:00+00:00"
_FROM_RUNNER = "claude_sonnet-4.6"
_WORKER_ID = "xai_grok-4.7"
_RUNNER = "opencode_xai_grok-4.7"
_ADAPTER = "opencode_cli"
_MODEL = "xai/grok-4.7"
_MANAGER = "nf01761-manager"


def _xai_row(**overrides) -> dict:
    row = {
        "worker_id": _WORKER_ID,
        "execution_runner": _RUNNER,
        "effective_adapter_id": _ADAPTER,
        "model": _MODEL,
        "implementation_worker": True,
        "enabled": True,
        "available": True,
        "launch_eligible": True,
        "max_risk": "high",
    }
    row.update(overrides)
    return row


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
        "objective": "reroute to the xai route",
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
             "normal", "reroute to the xai route",
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


def _reroute(task_id: str, to_runner: str) -> dict:
    return core.reroute_launch_identity(
        task_id,
        from_runner=_FROM_RUNNER,
        to_runner=to_runner,
        to_adapter_id=_ADAPTER,
        to_model=_MODEL,
        reason="owner reroute to xai",
    )


def _catalog(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(workforce_catalog, "build_catalog", lambda *_: {"workers": rows})


@pytest.mark.parametrize("to_runner", [_WORKER_ID, _RUNNER])
def test_reroute_accepts_worker_id_and_records_the_execution_runner(
    monkeypatch, repo, to_runner
):
    task_id = "NF01761_ACCEPT_" + to_runner.upper().replace("-", "_").replace(".", "_")
    _insert_pristine(repo, task_id)
    _catalog(monkeypatch, [_xai_row()])

    result = _reroute(task_id, to_runner)

    assert result["ok"] is True, result
    assert result["to_runner"] == _RUNNER
    assert result["to_model"] == _MODEL
    row = _row(repo, task_id)
    assert row["runner"] == _RUNNER
    card = json.loads(row["card_json"])
    assert card["runner"] == _RUNNER
    assert card["identity_reroute"]["to_runner"] == _RUNNER
    assert card["identity_reroute"]["to_adapter_id"] == _ADAPTER


def test_reroute_refuses_a_worker_id_that_matches_no_row(monkeypatch, repo):
    task_id = "NF01761_ABSENT"
    _insert_pristine(repo, task_id)
    _catalog(monkeypatch, [_xai_row(worker_id="other_worker")])
    before = _row(repo, task_id)

    result = _reroute(task_id, _WORKER_ID)

    assert result["ok"] is False
    assert result["stderr"] == (
        f"reroute_target_rejected:workforce_route_absent:runner={_WORKER_ID}:adapter={_ADAPTER}"
    )
    assert _row(repo, task_id) == before


def test_reroute_refuses_a_worker_id_that_matches_two_rows(monkeypatch, repo):
    task_id = "NF01761_AMBIGUOUS"
    _insert_pristine(repo, task_id)
    _catalog(monkeypatch, [_xai_row(), _xai_row(execution_runner="opencode_xai_grok-4.7b")])
    before = _row(repo, task_id)

    result = _reroute(task_id, _WORKER_ID)

    assert result["ok"] is False
    assert result["stderr"] == (
        f"reroute_target_rejected:workforce_route_ambiguous:runner={_WORKER_ID}:adapter={_ADAPTER}"
    )
    assert _row(repo, task_id) == before


def test_worker_id_on_another_adapter_does_not_match():
    matches, runner = workforce_catalog.match_reroute_routes(
        [_xai_row(effective_adapter_id="claude_cli")],
        _WORKER_ID,
        _ADAPTER,
        process_launcher.adapter_identity_tuple(_WORKER_ID),
    )

    assert matches == []
    assert runner == _WORKER_ID


def test_worker_id_row_without_execution_runner_does_not_match():
    matches, runner = workforce_catalog.match_reroute_routes(
        [_xai_row(execution_runner="")],
        _WORKER_ID,
        _ADAPTER,
        process_launcher.adapter_identity_tuple(_WORKER_ID),
    )

    assert matches == []
    assert runner == _WORKER_ID


def test_launch_identity_accepts_the_folded_runner():
    # Checked: validate_workforce_identity is unaffected by the fold. The
    # opencode_ family constrains the adapter to opencode_cli and has no
    # canonical table row, so the folded runner launches without
    # workforce_route_absent and the pinned model is returned unchanged.
    assert process_launcher.validate_workforce_identity(_RUNNER, _ADAPTER, _MODEL) == _MODEL


def test_xai_model_projects_provider_xai(monkeypatch):
    document = {
        "provider": {
            "xai": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "xAI",
                "options": {"baseURL": "https://api.x.ai/v1", "apiKey": "literal-test-key"},
                "models": {"grok-4.7": {"id": "grok-4.7"}},
            }
        }
    }
    monkeypatch.setattr(
        opencode_auth, "_owner_opencode_global_config_path", lambda: Path("opencode.json")
    )
    monkeypatch.setattr(opencode_auth, "_read_bounded_json", lambda _path: document)

    projected = opencode_auth.project_opencode_custom_provider(_MODEL, host_env={})

    assert projected is not None
    assert list(projected) == ["xai"]
    assert "opencode-go" not in projected
    assert list(projected["xai"]["models"]) == ["grok-4.7"]
