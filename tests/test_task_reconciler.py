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


class _Outbox:
    """Drain-shaped stand-in for the durable review action outbox.

    An action is leased exactly once and leaves the outbox when it is
    executed, which is the property that makes a repeated pass a no-op
    rather than a duplicate launch.
    """

    def __init__(self, pending):
        self.pending = list(pending)
        self.executed: list[str] = []
        self.budgets: list[int] = []

    def counts(self, _db_path):
        return {
            "pending": len(self.pending),
            "pending_parked": 0,
            "pending_reservable": len(self.pending),
            "completed": len(self.executed),
        }

    def driver(self):
        outbox = self

        class _Driver:
            def __init__(self, manager, *, db_path):
                self.manager = manager
                self.db_path = db_path

            def drain(self, *, max_actions, **_kwargs):
                outbox.budgets.append(max_actions)
                taken = outbox.pending[:max_actions]
                del outbox.pending[: len(taken)]
                outbox.executed.extend(taken)
                return SimpleNamespace(
                    as_dict=lambda: {
                        "attempted": len(taken),
                        "completed": len(taken),
                        "failed": 0,
                        "pending": 0,
                        "review_actions": outbox.counts(None),
                    }
                )

        return _Driver


def _install_recovery(monkeypatch, tmp_path, outbox, *, ensured=0, skipped=0):
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
            "review_recovery_scanned": ensured + skipped,
            "review_recovery_ensured": ensured,
            "review_recovery_skipped": skipped,
            "review_recovery_failed": 0,
            "review_recovery_reasons": {},
            "review_recovery_failures": [],
        },
    )
    monkeypatch.setattr(
        task_reconciler.review_lifecycle, "lifecycle_counts", outbox.counts,
    )
    monkeypatch.setattr(
        task_reconciler.review_orchestrator, "ReviewOrchestrator", outbox.driver(),
    )


def test_run_scan_drains_a_bounded_batch_and_records_the_budget(monkeypatch, tmp_path):
    mgr = _Mgr(tmp_path)
    outbox = _Outbox([f"action-{index}" for index in range(9)])
    _install_recovery(monkeypatch, tmp_path, outbox, ensured=2)

    result = task_reconciler.run_scan(mgr, include_gc=False)

    drain = result["review_recovery"]["review_recovery_drain"]
    # More than one action per pass, and never more than the headroom bound.
    assert task_reconciler.REVIEW_DRAIN_MIN_ACTIONS > 1
    assert outbox.budgets == [task_reconciler.REVIEW_DRAIN_MAX_ACTIONS]
    assert drain["budget"] == task_reconciler.REVIEW_DRAIN_MAX_ACTIONS
    # The receipt carries the whole truth of the pass: what it tried, what it
    # got, and what the outbox looked like before and after.
    assert drain["attempted"] == task_reconciler.REVIEW_DRAIN_MAX_ACTIONS
    assert drain["completed"] == task_reconciler.REVIEW_DRAIN_MAX_ACTIONS
    assert drain["pending"] == 0
    assert drain["ready"] == 9
    assert drain["backlog"]["pending_reservable"] == 9
    assert drain["backlog"]["pending_parked"] == 0
    assert drain["review_actions"]["pending"] == 3
    assert result["finalized"] == 3


def test_run_scan_drains_a_backlog_no_pass_ensured(monkeypatch, tmp_path):
    """Gating the drain on ``ensured`` was the other half of the starvation:
    a backlog nobody adds to must still be worked off."""
    mgr = _Mgr(tmp_path)
    outbox = _Outbox(["stale-0", "stale-1", "stale-2"])
    _install_recovery(monkeypatch, tmp_path, outbox, skipped=1)

    result = task_reconciler.run_scan(mgr, include_gc=False)

    assert outbox.executed == ["stale-0", "stale-1", "stale-2"]
    assert result["review_recovery"]["review_recovery_drain"]["budget"] == 3


def test_run_scan_drains_an_ensure_the_counts_have_not_caught_up_with(
    monkeypatch, tmp_path
):
    """An ensure still opens the gate on its own, so a chain written this pass
    is never left to the next one."""
    mgr = _Mgr(tmp_path)
    outbox = _Outbox([])
    _install_recovery(monkeypatch, tmp_path, outbox, ensured=1)

    result = task_reconciler.run_scan(mgr, include_gc=False)

    assert outbox.budgets == [task_reconciler.REVIEW_DRAIN_MIN_ACTIONS]
    assert result["review_recovery"]["review_recovery_drain"]["attempted"] == 0


def test_run_scan_skips_the_drain_without_ready_work(monkeypatch, tmp_path):
    mgr = _Mgr(tmp_path)
    outbox = _Outbox([])
    _install_recovery(monkeypatch, tmp_path, outbox, skipped=1)

    result = task_reconciler.run_scan(mgr, include_gc=False)

    drain = result["review_recovery"]["review_recovery_drain"]
    assert outbox.budgets == []
    assert drain["reason"] == "no_work"
    assert drain["budget"] == 0
    assert drain["backlog"]["pending_reservable"] == 0
    assert mgr.reconcile_calls == 1


def test_nf865_target_is_not_starved_behind_a_35_action_backlog(monkeypatch, tmp_path):
    """The 0.11.37 shape: 35 pending actions and a one-action budget, so a
    target ensured behind them waited one whole pass per backlog action. The
    bounded batch must reach it in passes proportional to the budget, and no
    number of passes may run an action twice."""
    mgr = _Mgr(tmp_path)
    outbox = _Outbox([f"backlog-{index:02d}" for index in range(35)])
    seeded = {"done": False}

    def _recover(_mgr, *, db_path):
        # The NF865 target becomes review_ready once, behind the whole
        # backlog; later passes only re-observe an already-present chain.
        first = not seeded["done"]
        if first:
            outbox.pending.append("NF865")
            seeded["done"] = True
        return {
            "state": "ok",
            "review_recovery_scanned": 1,
            "review_recovery_ensured": 1 if first else 0,
            "review_recovery_skipped": 0 if first else 1,
            "review_recovery_failed": 0,
            "review_recovery_reasons": {},
            "review_recovery_failures": [],
        }

    monkeypatch.setattr(
        task_reconciler.review_orchestrator,
        "canonical_review_db",
        lambda _mgr: tmp_path / "review.sqlite",
    )
    monkeypatch.setattr(
        task_reconciler.review_orchestrator, "recover_review_ready_targets", _recover,
    )
    monkeypatch.setattr(
        task_reconciler.review_lifecycle, "lifecycle_counts", outbox.counts,
    )
    monkeypatch.setattr(
        task_reconciler.review_orchestrator, "ReviewOrchestrator", outbox.driver(),
    )

    passes = 0
    while "NF865" not in outbox.executed:
        task_reconciler.run_scan(mgr, include_gc=False)
        passes += 1
        # The fuse is the old behaviour: one pass per backlog action.
        assert passes < 36, "NF865 starved behind the backlog"

    assert passes == 6
    assert outbox.pending == []
    assert len(outbox.executed) == 36
    assert len(set(outbox.executed)) == 36
    assert min(outbox.budgets) > 1
    assert max(outbox.budgets) <= task_reconciler.REVIEW_DRAIN_MAX_ACTIONS

    # A further pass over a drained outbox is a no-op, not a second launch.
    result = task_reconciler.run_scan(mgr, include_gc=False)
    assert len(outbox.executed) == 36
    assert result["review_recovery"]["review_recovery_drain"]["reason"] == "no_work"


def test_written_status_is_readable_back_on_this_host(tmp_path: Path) -> None:
    # NF-2026-00012: Windows synthesises 0o666 for every writable file, so the
    # POSIX ``mode & 0o077`` privacy test was ALWAYS true there and read_status
    # discarded the heartbeat write_status had just produced -- which is why
    # durable_status_present read false on a healthy host.
    task_reconciler.write_status(
        tmp_path, {"authority_state": "standby", "last_error": ""}
    )

    record = task_reconciler.read_status(tmp_path)

    assert record.get("schema_id") == "aiworkhub.task_reconciler_status.v1"
    assert record.get("authority_state") == "standby"
    assert bool(record) is True


def test_world_readable_status_is_still_discarded(tmp_path: Path) -> None:
    import os
    import stat as stat_module
    import subprocess

    task_reconciler.write_status(tmp_path, {"authority_state": "standby"})
    target = task_reconciler.status_path(tmp_path)
    if os.name == "nt":
        exposed = subprocess.run(
            ["icacls", str(target), "/grant", "*S-1-1-0:(R)"],
            capture_output=True,
            check=False,
        )
        if exposed.returncode != 0:
            import pytest

            pytest.skip("validation_unsupported_in_sandbox:cannot_expose_status")
    else:
        os.chmod(target, stat_module.S_IRUSR | stat_module.S_IWUSR | stat_module.S_IROTH)

    assert task_reconciler.read_status(tmp_path) == {}
