"""NF-2026-01572: launch must accept the card's own workforce worker_id.

Measured: ``ProcessManager.launch`` passed the raw catalog worker_id
``opencode-go_mimo-v2.6-pro`` straight to ``_preflight_card``, whose exact
``card.get("runner") != runner`` compare refused the card's own stored
canonical spelling ``opencode_go_mimo-v2.6-pro`` with
``runner_mismatch:opencode_go_mimo-v2.6-pro``. Task creation stores the
canonical spelling (``core.canonical_runner_id`` folds the hyphenated
workforce id onto it), so a card could never launch itself.

``launch`` now folds an explicit runner onto the card's stored spelling
before any downstream path sees it, and ``_preflight_card`` compares with the
same fold, so a runner that folds onto nothing is still refused exactly as
before.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher  # noqa: E402

TASK_ID = "NF01572_LAUNCH_RUNNER_FOLD"
CARD_RUNNER = "opencode_go_mimo-v2.6-pro"
CATALOG_RUNNER = "opencode-go_mimo-v2.6-pro"
UNRELATED_RUNNER = "deepseek_v4-pro"
CARD_TOPIC = "task_mcp"
ADAPTER_ID = "opencode_cli"
MODEL = "opencode-go/mimo-v2.6-pro"


def _card() -> dict:
    return {
        "task_id": TASK_ID,
        "runner": CARD_RUNNER,
        "topic": CARD_TOPIC,
        "status": "pending",
    }


def _manager(tmp_path: Path) -> process_launcher.ProcessManager:
    card = _card()

    def _show_task(task_id: str) -> dict:
        return {"returncode": 0, "stdout": json.dumps(card)}

    return process_launcher.ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "p.jsonl",
        process_dir=tmp_path / "proc",
        show_task=_show_task,
    )


def _record_launches(monkeypatch: pytest.MonkeyPatch, manager) -> list[dict]:
    recorded: list[dict] = []

    def _fake(**kwargs) -> dict:
        recorded.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(manager, "_launch_isolated", _fake)
    monkeypatch.setattr(manager, "_launch_direct_for_tests", _fake)
    return recorded


# (1) The card's own catalog worker_id folds onto the card's stored spelling,
#     so every downstream path (the launch routes here) sees the stored form.


def test_launch_folds_the_catalog_worker_id_onto_the_card(monkeypatch, tmp_path):
    # NF-2026-01572: an explicit tuple must not read the card inside launch,
    # so this omits the topic and folds in the launch derivation branch.
    manager = _manager(tmp_path)
    recorded = _record_launches(monkeypatch, manager)
    manager.launch(
        task_id=TASK_ID,
        runner=CATALOG_RUNNER,
        adapter_id=ADAPTER_ID,
    )
    assert len(recorded) == 1
    assert recorded[0]["runner"] == CARD_RUNNER


# (2) A runner that folds onto nothing keeps its raw spelling.


def test_launch_keeps_a_runner_that_folds_onto_nothing(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    recorded = _record_launches(monkeypatch, manager)
    manager.launch(
        task_id=TASK_ID,
        runner=UNRELATED_RUNNER,
        topic=CARD_TOPIC,
        adapter_id=ADAPTER_ID,
        model=MODEL,
    )
    assert len(recorded) == 1
    assert recorded[0]["runner"] == UNRELATED_RUNNER


# (3) The catalog worker_id clears the identity compare; a later, non-identity
#     refusal is acceptable, but it must never be runner_mismatch.


def test_preflight_accepts_the_catalog_worker_id(tmp_path):
    manager = _manager(tmp_path)
    try:
        manager._preflight_card(TASK_ID, CATALOG_RUNNER, CARD_TOPIC, ADAPTER_ID)
    except Exception as exc:
        assert "runner_mismatch" not in str(exc)


# (4) A genuine mismatch is still refused, naming the card's stored runner.


def test_preflight_still_refuses_a_runner_that_folds_onto_nothing(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(process_launcher.LaunchRejected) as excinfo:
        manager._preflight_card(TASK_ID, UNRELATED_RUNNER, CARD_TOPIC, ADAPTER_ID)
    assert str(excinfo.value).startswith(f"runner_mismatch:{CARD_RUNNER}")