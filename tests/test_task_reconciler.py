from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_reconciler  # noqa: E402

NOW = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)


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

    def projection(self, _db_path, *, max_actions, now=None):
        """One authenticated read answering depth AND this drain's targets.

        The groups are ordered exactly as the drain will reserve them, so a
        budget smaller than ``max_actions`` primes a prefix of what runs.
        """
        return task_reconciler.review_lifecycle.DrainProjection(
            counts=self.counts(_db_path),
            action_request_ids=tuple(
                (f"req-{name}",) for name in self.pending[:max_actions]
            ),
        )

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
        task_reconciler.review_lifecycle, "drain_projection", outbox.projection,
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
        task_reconciler.review_lifecycle, "drain_projection", outbox.projection,
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


# --- One bounded projection per drain (NF-853) -------------------------------


class _PrimingMgr(_Mgr):
    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self.primed: list[list[str]] = []

    def prime_request_events(self, request_ids) -> int:
        batch = list(request_ids)
        self.primed.append(batch)
        return len(batch)


def _projection(groups, counts=None):
    return task_reconciler.review_lifecycle.DrainProjection(
        counts=dict(counts or {}),
        action_request_ids=tuple(tuple(group) for group in groups),
    )


def test_drain_projects_every_target_request_in_one_batch(tmp_path):
    mgr = _PrimingMgr(tmp_path)
    # A chain yields far more than two request ids -- one target plus every
    # reviewer child an accept reads back -- so the batch is sized by the
    # actions this budget can reserve, not by a fixed per-action guess.
    groups = [
        (f"request-{index:02d}", f"child-{index:02d}-a", f"child-{index:02d}-b")
        for index in range(6)
    ]

    projected = task_reconciler._prime_drain_process_events(
        mgr, _projection(groups), 6
    )

    # One batch for the whole drain, in the order the drain will read them.
    assert len(mgr.primed) == 1
    assert mgr.primed[0] == [name for group in groups for name in group]
    assert projected == 18


def test_priming_never_exceeds_the_actions_the_budget_can_reserve(tmp_path):
    """A smaller budget primes a prefix of the drain's own reservation order.

    Never a differently chosen set: the projection is ordered by what
    ``reserve_next_action`` will hand out, so a prefix is exactly what a
    shorter pass consumes.
    """
    mgr = _PrimingMgr(tmp_path)
    groups = [(f"request-{index:02d}",) for index in range(6)]

    assert task_reconciler._prime_drain_process_events(mgr, _projection(groups), 2) == 2
    assert mgr.primed == [["request-00", "request-01"]]


def test_priming_is_advisory_and_never_blocks_the_drain(tmp_path):
    groups = [("request-00",)]

    # A manager that predates the batch projection.
    assert task_reconciler._prime_drain_process_events(
        SimpleNamespace(), _projection(groups), 6
    ) == 0

    class _Exploding(_PrimingMgr):
        def prime_request_events(self, request_ids) -> int:
            raise RuntimeError("ledger unreadable")

    assert task_reconciler._prime_drain_process_events(
        _Exploding(tmp_path), _projection(groups), 6
    ) == 0

    # Nothing to project is zero, not a failure and not a claimed success.
    mgr = _PrimingMgr(tmp_path)
    assert task_reconciler._prime_drain_process_events(mgr, _projection([]), 6) == 0
    assert mgr.primed == []


def test_a_22_action_backlog_primes_once_for_the_whole_pass(monkeypatch, tmp_path):
    """The measured NF-853 shape: 22 pending actions, one projection.

    Asserted as a bounded call count and never as wall-clock time. The defect
    was O(full ledger x action), so the property that matters is that the
    number of ledger passes does not grow with the backlog.
    """
    mgr = _PrimingMgr(tmp_path)
    outbox = _Outbox([f"action-{index:02d}" for index in range(22)])
    _install_recovery(monkeypatch, tmp_path, outbox, skipped=1)

    result = task_reconciler.run_scan(mgr, include_gc=False)
    drain = result["review_recovery"]["review_recovery_drain"]

    assert drain["budget"] == task_reconciler.REVIEW_DRAIN_MAX_ACTIONS
    assert len(mgr.primed) == 1
    assert mgr.primed[0] == [
        f"req-action-{index:02d}"
        for index in range(task_reconciler.REVIEW_DRAIN_MAX_ACTIONS)
    ]
    assert drain["process_events_projected"] == task_reconciler.REVIEW_DRAIN_MAX_ACTIONS


def test_one_recovery_pass_authenticates_the_review_store_once(monkeypatch, tmp_path):
    """Sizing the batch and naming its requests are one authenticated read.

    Two entry points each ran ``_verify_all_chains``, so every 30 s pass paid
    for a second whole-store authenticated scan of rows that had not moved in
    between.
    """
    review_lifecycle = task_reconciler.review_lifecycle
    review_db = tmp_path / "review.sqlite"
    for index in range(3):
        review_lifecycle.create_or_replay_chain(
            review_db,
            target_task_id=f"TASK_TARGET_{index}",
            target_request_id=f"req-{index}",
            claim_epoch="7",
            packet_sha256="a" * 64,
            candidate_sha256=f"{index:064x}",
            now=NOW,
        )

    calls: list[int] = []
    verify_all = review_lifecycle._verify_all_chains

    def _counted(conn) -> None:
        calls.append(1)
        verify_all(conn)

    monkeypatch.setattr(review_lifecycle, "_verify_all_chains", _counted)
    monkeypatch.setattr(
        task_reconciler.review_orchestrator,
        "canonical_review_db",
        lambda _mgr: review_db,
    )
    monkeypatch.setattr(
        task_reconciler.review_orchestrator,
        "recover_review_ready_targets",
        lambda _mgr, *, db_path: {
            "state": "ok",
            "review_recovery_scanned": 3,
            "review_recovery_ensured": 0,
            "review_recovery_skipped": 3,
            "review_recovery_failed": 0,
            "review_recovery_reasons": {},
            "review_recovery_failures": [],
        },
    )

    class _Driver:
        def __init__(self, manager, *, db_path) -> None:
            self.manager = manager
            self.db_path = db_path

        def drain(self, *, max_actions, **_kwargs):
            return SimpleNamespace(
                as_dict=lambda: {"attempted": 0, "completed": 0, "failed": 0}
            )

    monkeypatch.setattr(
        task_reconciler.review_orchestrator, "ReviewOrchestrator", _Driver
    )

    mgr = _PrimingMgr(tmp_path)
    recovery = task_reconciler._scan_review_ready_recovery(mgr)
    drain = recovery["review_recovery_drain"]

    # The pass really took the drain path, and paid for the store-wide chain
    # authentication exactly once while doing it.
    assert drain["state"] == "ok"
    assert drain["budget"] == task_reconciler.REVIEW_DRAIN_MAX_ACTIONS
    assert drain["backlog"]["pending_reservable"] == 3 * len(review_lifecycle.PLAN)
    assert calls == [1]
    # Lenses inside one parent stay sequential, so a fresh cursor spends the
    # whole budget on one chain and one request answers the batch.
    assert mgr.primed == [["req-0"]]
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
