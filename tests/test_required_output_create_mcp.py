from __future__ import annotations

from aiworkhub import server


def test_task_create_forwards_required_output_exception_contract(monkeypatch):
    captured = {}

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(server.core, "create_task", fake_create_task)

    result = server.aiworkhub_task_create(
        task_id="TASK_REQUIRED_OUTPUT_MCP",
        title="Preserve accepted evidence",
        runner="codex_worker",
        topic="coding",
        objective="Update readiness without rewriting accepted evidence.",
        acceptance=["Evidence stays valid."],
        allowed_writes=["out/evidence.json", "out/READY.md"],
        required_outputs=["out/evidence.json", "out/READY.md"],
        allow_empty_required_outputs=["out/READY.md"],
        allow_unchanged_required_outputs=["out/evidence.json"],
        validation=["python -m pytest -q"],
    )

    assert result == {"ok": True}
    assert captured["allow_empty_required_outputs"] == ["out/READY.md"]
    assert captured["allow_unchanged_required_outputs"] == ["out/evidence.json"]
