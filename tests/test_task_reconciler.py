from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_reconciler  # noqa: E402


class _Mgr:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.reconcile_calls = 0

    def reconcile(self, *, include_gc: bool = True) -> dict:
        self.reconcile_calls += 1
        return {"finalized": 3, "watched": 1, "gc_included": include_gc}


def test_run_scan_exposes_review_recovery_and_does_not_block(monkeypatch, tmp_path):
    mgr = _Mgr(tmp_path)
    monkeypatch.setattr(
        task_reconciler.review_orchestrator,
        "canonical_review_db",
        lambda _mgr: None,
    )
    result = task_reconciler.run_scan(mgr, include_gc=False)
    assert result["ok"] is True
    assert result["finalized"] == 3
    assert mgr.reconcile_calls == 1
    assert result["review_recovery"]["state"] == "skipped"
    assert result["review_recovery"]["reason"] == "review_db_unavailable"
    assert result["callback_prune"]["reason"] == "gc_not_included"


def test_run_scan_recovery_error_does_not_block_worker_reconcile(monkeypatch, tmp_path):
    mgr = _Mgr(tmp_path)
    monkeypatch.setattr(
        task_reconciler.review_orchestrator,
        "canonical_review_db",
        lambda _mgr: tmp_path / "review.sqlite",
    )
    monkeypatch.setattr(
        task_reconciler.review_orchestrator,
        "recover_review_ready_targets",
        lambda _mgr, *, db_path: (_ for _ in ()).throw(RuntimeError("recovery_exploded")),
    )
    result = task_reconciler.run_scan(mgr, include_gc=False)
    assert result["finalized"] == 3
    assert result["review_recovery"]["state"] == "skipped"
    assert "RuntimeError" in result["review_recovery"]["reason"]


def test_run_scan_drains_only_after_ensured_recovery(monkeypatch, tmp_path):
    mgr = _Mgr(tmp_path)
    drained: list[Path] = []

    class _Driver:
        def __init__(self, manager, *, db_path):
            self.manager = manager
            self.db_path = db_path

        def drain(self, **_kwargs):
            drained.append(self.db_path)
            return SimpleNamespace(
                as_dict=lambda: {
                    "attempted": 2,
                    "completed": 2,
                    "failed": 0,
                    "pending": 0,
                    "review_actions": {},
                }
            )

    monkeypatch.setattr(
        task_reconciler.review_orchestrator,
        "canonical_review_db",
        lambda _mgr: tmp_path / "review.sqlite",
    )
    monkeypatch.setattr(
        task_reconciler.review_orchestrator,
        "recover_review_ready_targets",
        lambda _mgr, *, db_path: {
            "state": "ok",
            "review_recovery_scanned": 1,
            "review_recovery_ensured": 1,
            "review_recovery_skipped": 0,
            "review_recovery_failed": 0,
            "review_recovery_reasons": {"ensured": 1},
            "review_recovery_failures": [],
        },
    )
    monkeypatch.setattr(
        task_reconciler.review_orchestrator,
        "ReviewOrchestrator",
        _Driver,
    )
    result = task_reconciler.run_scan(mgr, include_gc=False)
    assert drained == [tmp_path / "review.sqlite"]
    assert result["review_recovery"]["review_recovery_ensured"] == 1
    assert result["finalized"] == 3

    drained.clear()
    monkeypatch.setattr(
        task_reconciler.review_orchestrator,
        "recover_review_ready_targets",
        lambda _mgr, *, db_path: {
            "state": "ok",
            "review_recovery_scanned": 1,
            "review_recovery_ensured": 0,
            "review_recovery_skipped": 1,
            "review_recovery_failed": 0,
            "review_recovery_reasons": {"already_present": 1},
            "review_recovery_failures": [],
        },
    )
    result = task_reconciler.run_scan(mgr, include_gc=False)
    assert drained == []
    assert result["review_recovery"]["review_recovery_drain"]["reason"] == "no_work"
    assert mgr.reconcile_calls == 2
