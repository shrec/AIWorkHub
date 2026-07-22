from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import server  # noqa: E402


def test_manager_archive_and_supersede_tools_are_write_gated(monkeypatch) -> None:
    monkeypatch.setattr(server.core, "writes_allowed", lambda: False)

    archive = server.aiworkhub_manager_task_archive("TASK_B891", reason="done")
    supersede = server.aiworkhub_manager_task_supersede("TASK_B891", reason="orphan")

    assert archive == {"ok": False, "error": "write_gate_closed", "task_id": "TASK_B891"}
    assert supersede == {"ok": False, "error": "write_gate_closed", "task_id": "TASK_B891"}


def test_manager_archive_and_supersede_dispatch_to_repo_bound_engine(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(server.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(server.core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(server.core, "CODEX_RUNNER", "codex")

    def fake_archive(repo, task_id, *, actor, reason="", supersede=False):
        calls.append({
            "repo": repo,
            "task_id": task_id,
            "actor": actor,
            "reason": reason,
            "supersede": supersede,
        })
        return {"ok": True, "returncode": 0}

    monkeypatch.setattr(server.task_engine, "archive_task", fake_archive)

    assert server.aiworkhub_manager_task_archive("TASK_ARCHIVE", reason="done")["ok"] is True
    assert server.aiworkhub_manager_task_supersede("TASK_SUPERSEDE", reason="orphan")["ok"] is True
    assert calls == [
        {
            "repo": tmp_path,
            "task_id": "TASK_ARCHIVE",
            "actor": "codex",
            "reason": "done",
            "supersede": False,
        },
        {
            "repo": tmp_path,
            "task_id": "TASK_SUPERSEDE",
            "actor": "codex",
            "reason": "orphan",
            "supersede": True,
        },
    ]
