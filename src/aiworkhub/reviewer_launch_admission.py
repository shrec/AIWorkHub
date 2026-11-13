"""NF-2026-02260: reviewer launch admission accounting, moved out of process_launcher.

NF-2026-02248 (NF-2026-00034): a reviewer launch deferred for memory or
concurrency capacity waits in ``launch_admission`` for up to its own
configured ceiling (1800 s by default), far past the 300 s owner ceiling
(``ProcessManager._QUALITY_REVIEW_LAUNCH_OWNER_SECONDS``), so the owner used to
terminalize every capacity-deferred reviewer as a pid-null timeout before it
could spawn.  Each live reviewer admission wait is registered here by thread
ident with its (start, deadline) window; the owner join accounts for it while
that bounded window is live.  Entries are removed on every exit of the wait.

``process_launcher`` imports every name back unchanged and binds the helpers
onto ``ProcessManager`` under their original attribute names, so callers keep
going through ``self`` / ``ProcessManager`` and every monkeypatch target holds.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from . import launch_admission

if TYPE_CHECKING:
    from .process_launcher import ProcessManager

_REVIEWER_LAUNCH_STATE_LOCK = threading.Lock()
_REVIEWER_ADMISSION_WAIT_WINDOWS: dict[int, tuple[float, float]] = {}
# Launcher ident -> monotonic moment its last admission wait ended (``None``
# until one ends).  Keys exist only while an owner join is live for that
# launcher, so post-admission work gets a fresh ceiling from admission.
_REVIEWER_ADMISSION_ENDED_AT: dict[int, float | None] = {}
# One admission poll (``launch_admission.DEFAULT_POLL_SECONDS``) of grace.
_REVIEWER_ADMISSION_WAIT_GRACE_SECONDS = float(
    launch_admission.DEFAULT_POLL_SECONDS
)


def _reviewer_launch_owner_join(
    self: ProcessManager, launcher: threading.Thread, request_id: str
) -> str:
    """Wait for one bounded reviewer launch owner to finish.

    Returns ``"completed"`` when the launcher thread finished (its result is
    ready), ``"provider_committed"`` when a real provider process already
    exists (never time-limited), or ``"timeout"`` when the still-live owner
    should be terminalized.  A live owned Source Graph prewarm extends the
    owner ceiling, but only until it outlives the preparation stall ceiling,
    so a hung build can no longer hold its owner forever.

    NF-2026-02248: a live reviewer admission wait on the launcher thread
    (capacity deferral, bounded by its own configured ceiling) is accounted
    pre-provider time too.  The owner keeps joining while that bounded wait
    is live and heartbeats the reservation so expiry reconciliation still
    reads it as live.  When the wait ends, post-admission work gets a fresh
    owner ceiling measured from the admission moment rather than the rest
    of the current join slice.  Admission-explained extensions stay inside
    a separate outer bound: one pre-admission owner ceiling, the configured
    admission wait plus one poll of grace, and one post-admission ceiling.
    """

    ceiling = self._QUALITY_REVIEW_LAUNCH_OWNER_SECONDS
    admission_span = (
        launch_admission.configured_wait_seconds()
        + _REVIEWER_ADMISSION_WAIT_GRACE_SECONDS
    )
    ident = getattr(launcher, "ident", None)
    now = time.monotonic()
    slice_deadline = now + ceiling
    outer_deadline = now + ceiling + admission_span + ceiling
    if ident is not None:
        with _REVIEWER_LAUNCH_STATE_LOCK:
            _REVIEWER_ADMISSION_ENDED_AT.setdefault(ident, None)
    try:
        while launcher.is_alive():
            launcher.join(max(0.0, slice_deadline - time.monotonic()))
            if not launcher.is_alive():
                return "completed"
            if self._reviewer_provider_committed(request_id):
                return "provider_committed"
            now = time.monotonic()
            if self._reviewer_source_graph_prewarm_live(request_id):
                # The prewarm is bounded by its own stall ceiling; the
                # admission outer bound restarts after it.
                slice_deadline = now + ceiling
                outer_deadline = max(
                    outer_deadline, now + ceiling + admission_span + ceiling
                )
                continue
            if now >= outer_deadline:
                return "timeout"
            waited = self._reviewer_admission_wait_seconds(launcher)
            if waited is not None:
                self._publish_reviewer_progress(
                    request_id, "admission_waiting", f"waited_seconds={waited:.1f}"
                )
                slice_deadline = min(now + ceiling, outer_deadline)
                continue
            ended_at = self._reviewer_admission_ended_at(launcher)
            if ended_at is not None and now < ended_at + ceiling:
                # Fresh spawn ceiling from the admission moment.
                slice_deadline = min(ended_at + ceiling, outer_deadline)
                continue
            return "timeout"
        return "completed"
    finally:
        if ident is not None:
            with _REVIEWER_LAUNCH_STATE_LOCK:
                _REVIEWER_ADMISSION_ENDED_AT.pop(ident, None)


def _reviewer_admission_ended_at(launcher: threading.Thread) -> float | None:
    """Monotonic moment the launcher's last admission wait ended, else ``None``."""

    ident = getattr(launcher, "ident", None)
    if ident is None:
        return None
    with _REVIEWER_LAUNCH_STATE_LOCK:
        return _REVIEWER_ADMISSION_ENDED_AT.get(ident)


def _reviewer_admission_wait_seconds(launcher: threading.Thread) -> float | None:
    """Seconds a live bounded admission wait on ``launcher`` has run, else ``None``.

    A wait whose own deadline (plus one poll of grace) has passed is no
    longer an explanation: the admission call returns on its own by then,
    so an owner still alive past it is stalled elsewhere.
    """

    ident = getattr(launcher, "ident", None)
    if ident is None:
        return None
    with _REVIEWER_LAUNCH_STATE_LOCK:
        window = _REVIEWER_ADMISSION_WAIT_WINDOWS.get(ident)
    if window is None:
        return None
    started, deadline = window
    now = time.monotonic()
    if now > deadline + _REVIEWER_ADMISSION_WAIT_GRACE_SECONDS:
        return None
    return max(0.0, now - started)


def _publish_reviewer_launch_timeout_evidence(
    self: ProcessManager, request_id: str, adapter_id: str
) -> None:
    """Publish a pre-provider launch timeout's adapter and attempt evidence.

    The evidence is a separate ``launch_timeout_evidence`` progress event
    (JSON in ``preparation_detail``); the terminal reason itself stays the
    exact ``quality_review_launch_timeout`` token consumers match.
    """

    latest = self._latest_by_request().get(request_id) or {}
    evidence = {
        "adapter_id": (adapter_id or str(latest.get("adapter_id") or ""))[:80],
        "last_phase": str(latest.get("preparation_phase") or "")[:80],
        "owner_ceiling_seconds": self._QUALITY_REVIEW_LAUNCH_OWNER_SECONDS,
        "provider_spawn_committed": False,
        "retryable": True,
    }
    self._publish_reviewer_progress(
        request_id,
        "launch_timeout_evidence",
        json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        adapter_id=adapter_id or None,
    )


def _await_reviewer_admission(
    self: ProcessManager, probe: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    # NF-2026-02248: register this bounded wait under the calling thread so
    # the reviewer launch owner join accounts for it instead of reading a
    # capacity-deferred launch as a stalled pre-provider owner.
    wait_seconds = launch_admission.configured_wait_seconds()
    ident = threading.get_ident()
    started = time.monotonic()
    with _REVIEWER_LAUNCH_STATE_LOCK:
        _REVIEWER_ADMISSION_WAIT_WINDOWS[ident] = (
            started, started + wait_seconds,
        )
    try:
        return launch_admission.await_memory_admission(
            kind="reviewer",
            probe=probe,
            wait_seconds=wait_seconds,
        )
    finally:
        ended = time.monotonic()
        with _REVIEWER_LAUNCH_STATE_LOCK:
            _REVIEWER_ADMISSION_WAIT_WINDOWS.pop(ident, None)
            # Only a launcher whose owner join registered it is recorded,
            # so this map stays bounded by the live owner joins.
            if ident in _REVIEWER_ADMISSION_ENDED_AT:
                _REVIEWER_ADMISSION_ENDED_AT[ident] = ended
