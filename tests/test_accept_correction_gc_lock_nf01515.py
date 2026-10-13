"""NF-2026-01515: accept_review's writable path must hold the per-request
lock too, so the dispatcher's retention GC cannot quarantine the retained
review workspace while a manager correction is written and revalidated.

Measured: ``accept_review`` took only ``self._promotion_lock()`` on the
writable path. The retention sweep's ``_gc_finalized_workspace`` holds only
``self._request_lock(request_id)`` and re-checks
``_retained_review_workspace_integrity`` under it; mid-correction it observes
hashes that differ from the stored ``changed_path_hashes`` (the correction
bytes are legitimately there) and quarantines the workspace GC races against.
The fix nests ``self._request_lock(request_id)`` inside the promotion lock
for the whole writable acceptance; the read-only reviewer/research path
already held only the request lock and is unchanged.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher, process_launcher_accept_review  # noqa: E402

import test_accept_with_manager_correction_nf01436 as _nf01436  # noqa: E402

REQUEST_ID = _nf01436.REQUEST_ID
TASK_ID = _nf01436.TASK_ID


class _RealLockStubManager(_nf01436._StubManager):
    """The nf01436 writable-correction stub with the PRODUCTION file locks
    instead of ``nullcontext()``, so GC-vs-accept_review mutual exclusion
    over the same ``process_dir`` can be measured directly."""

    _request_lock = process_launcher.ProcessManager._request_lock
    _promotion_lock = process_launcher.ProcessManager._promotion_lock


def _accept_env_with_real_locks(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setattr(_nf01436, "_StubManager", _RealLockStubManager)
    return _nf01436._accept_env(tmp_path, monkeypatch, **kwargs)


def test_gc_lock_probe_cannot_acquire_request_lock_during_writable_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While accept_review is between writing the manager correction and
    finishing its revalidation, a non-blocking GC-style ``_request_lock``
    acquisition for the SAME request_id must fail. Without the fix (writable
    path holds only the promotion lock) this probe acquires the lock --
    proving GC could race in and quarantine the workspace exactly as
    NF-2026-01515 measured.
    """
    probe: dict[str, bool] = {}
    manager = _accept_env_with_real_locks(tmp_path, monkeypatch)

    def _probe_during_revalidation(workspace, card, latest, changed):
        try:
            with manager._request_lock(REQUEST_ID, blocking=False):
                probe["gc_could_acquire"] = True
        except OSError:
            probe["gc_could_acquire"] = False
        return [], {"combined_tree": {}}

    monkeypatch.setattr(
        process_launcher, "_run_full_snapshot_validations", _probe_during_revalidation
    )

    result = _nf01436._accept(
        manager, manager_correction=_nf01436._correction_payload()
    )

    assert result["ok"] is True, result.get("error")
    assert probe == {"gc_could_acquire": False}


def test_readonly_accept_path_holds_only_request_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read-only reviewer/research path is unchanged: it takes only the
    per-request lock and is never blocked by an unrelated held promotion
    lock, which this path must never touch.
    """

    class _ReadonlyManager:
        _request_lock = process_launcher.ProcessManager._request_lock
        _promotion_lock = process_launcher.ProcessManager._promotion_lock

        def __init__(self, process_dir: Path) -> None:
            self.process_dir = process_dir

        def _request_events(self, request_id: str) -> list[dict]:
            return [{"request_id": request_id, "task_id": TASK_ID}]

        def _show_task(self, task_id: str) -> dict:
            return {"returncode": 0, "stdout": "{}"}

    monkeypatch.setattr(
        process_launcher,
        "_parse_card",
        lambda raw, task_id: {"terminal_review": {"evidence": {"changed_paths": []}}},
    )
    monkeypatch.setattr(
        process_launcher, "_canonical_task_status", lambda card: "review"
    )
    monkeypatch.setattr(
        process_launcher, "_card_is_readonly_quality_review", lambda card: True
    )
    monkeypatch.setattr(
        process_launcher, "_card_is_readonly_research", lambda card: False
    )

    process_dir = tmp_path / "process_dir"
    process_dir.mkdir()
    manager = _ReadonlyManager(process_dir)
    outcome: dict = {}

    def _call_accept() -> None:
        outcome["result"] = process_launcher_accept_review.accept_review(
            manager, REQUEST_ID, TASK_ID, reviewer_reports=[{"id": "unverified"}]
        )

    with manager._promotion_lock():
        thread = threading.Thread(target=_call_accept, daemon=True)
        thread.start()
        thread.join(timeout=5)
        still_blocked = thread.is_alive()

    assert not still_blocked, (
        "read-only accept_review path blocked on an unrelated promotion lock"
    )
    thread.join(timeout=5)
    assert outcome["result"]["ok"] is False
    assert outcome["result"]["error"] == "unverified_reviewer_reports_forbidden"
