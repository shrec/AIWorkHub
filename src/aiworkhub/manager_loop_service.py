"""Process-wide manager loop registry: one :class:`ManagerOrchestrator` per repository.

``send`` and ``rotate`` run a manager turn on a background thread and return
at once; a concurrent call is refused with ``manager_turn_in_progress`` and
nothing is queued. ``status`` and ``events`` let a caller poll a running
session incrementally. Every :class:`~aiworkhub.manager_loop.ManagerLoopError`
becomes ``{"ok": False, "error": <reason code>}`` here -- nothing raises
across this module's boundary.

This module resolves no manager route and reads no repository default: every
function takes the repository root explicitly, so it is directly testable
with a fake backend factory and a ``tmp_path`` repo. The six
``aiworkhub_manager_loop_*`` MCP tools in ``server.py`` resolve the caller's
repository through the shared manager route gate and pass it in.
"""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import Any, Callable

from .manager_loop import ManagerLoopError, ManagerOrchestrator
from .manager_loop_backends import manager_backend_factory

_REGISTRY_LOCK = threading.Lock()
_ENTRIES: dict[str, "_Entry"] = {}


@dataclasses.dataclass
class _Entry:
    """One repository's orchestrator plus the service's own turn-in-flight lock.

    ``turn_lock`` is deliberately separate from the orchestrator's own
    internal exclusion: it is acquired, non-blocking, at dispatch time --
    before a background thread is ever started -- so a concurrent caller is
    refused synchronously instead of racing to discover the refusal from
    inside a thread it can no longer observe.
    """

    orchestrator: ManagerOrchestrator
    turn_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    thread: threading.Thread | None = None
    last_turn: dict[str, Any] | None = None


def _entry_for(repo: str | Path) -> _Entry:
    key = str(Path(repo).resolve())
    with _REGISTRY_LOCK:
        entry = _ENTRIES.get(key)
        if entry is None:
            orchestrator = ManagerOrchestrator.for_repository(repo, manager_backend_factory(repo))
            entry = _Entry(orchestrator=orchestrator)
            _ENTRIES[key] = entry
        return entry


def _error(exc: ManagerLoopError) -> dict[str, Any]:
    return {"ok": False, "error": str(exc)}


def _entry_or_error(repo: str | Path) -> tuple[_Entry | None, dict[str, Any] | None]:
    try:
        return _entry_for(repo), None
    except ManagerLoopError as exc:
        return None, _error(exc)
    except Exception as exc:  # noqa: BLE001 - building the entry must never cross the MCP boundary
        return None, {"ok": False, "error": f"manager_loop_unavailable:{type(exc).__name__}"}


def _dispatch_turn(
    repo: str | Path,
    action: Callable[[ManagerOrchestrator], dict[str, Any]],
    *,
    record_last_turn: bool,
) -> dict[str, Any]:
    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    if not entry.turn_lock.acquire(blocking=False):
        return {"ok": False, "error": "manager_turn_in_progress"}
    session = entry.orchestrator.session
    if session is None:
        entry.turn_lock.release()
        return {"ok": False, "error": "no_active_manager_session"}
    turn = session.turn_count + 1
    session_id = session.session_id

    def run() -> None:
        try:
            raw = action(entry.orchestrator)
            if record_last_turn:
                entry.last_turn = {
                    "turn": raw.get("turn", turn),
                    "ok": bool(raw.get("ok")),
                    "errors": list(raw.get("errors") or []),
                    "reply": str(raw.get("reply", ""))[:500],
                }
        except Exception as exc:  # noqa: BLE001 - a dead thread must still record the turn's outcome
            if record_last_turn:
                detail = f"{type(exc).__name__}: {exc}"[:240]
                entry.last_turn = {"turn": turn, "ok": False, "errors": [detail], "reply": ""}
        finally:
            entry.turn_lock.release()

    thread = threading.Thread(target=run, daemon=True)
    entry.thread = thread
    thread.start()
    return {"ok": True, "session_id": session_id, "turn": turn, "state": "running"}


def start(repo: str | Path, backend_id: str, model: str) -> dict[str, Any]:
    """Open the repository's one active session; synchronous, not backgrounded."""

    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    if not entry.turn_lock.acquire(blocking=False):
        return {"ok": False, "error": "manager_turn_in_progress"}
    try:
        session = entry.orchestrator.start(backend_id, model)
    except ManagerLoopError as exc:
        return _error(exc)
    finally:
        entry.turn_lock.release()
    return {"ok": True, "session": session.to_json()}


def send(repo: str | Path, text: str) -> dict[str, Any]:
    """Run one manager turn on a background thread.

    Refused with ``manager_turn_in_progress``, not queued, while one runs.
    """

    return _dispatch_turn(
        repo, lambda orchestrator: orchestrator.send(text), record_last_turn=True
    )


def rotate(repo: str | Path, reason: str) -> dict[str, Any]:
    """End the session with a handoff, on a background thread.

    Refused with ``manager_turn_in_progress``, not queued, while a turn runs.
    """

    return _dispatch_turn(
        repo, lambda orchestrator: orchestrator.rotate(reason), record_last_turn=False
    )


def status(repo: str | Path) -> dict[str, Any]:
    """The active session, whether a turn is running, and the last turn's outcome."""

    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    session = entry.orchestrator.session
    return {
        "ok": True,
        "session": session.to_json() if session is not None else None,
        "running": entry.turn_lock.locked(),
        "last_turn": entry.last_turn,
    }


def events(
    repo: str | Path, session_id: str, after_seq: int = 0, limit: int = 200
) -> dict[str, Any]:
    """Events for ``session_id`` with ``seq`` greater than ``after_seq``, bounded to ``limit``.

    Lets a caller poll a running session incrementally.
    """

    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    try:
        all_events = entry.orchestrator.store.events(session_id)
    except ManagerLoopError as exc:
        return _error(exc)
    floor = int(after_seq)
    bound = max(0, int(limit))
    filtered = [event for event in all_events if int(event.get("seq", 0)) > floor][:bound]
    return {"ok": True, "events": filtered}


def close(repo: str | Path) -> dict[str, Any]:
    """Release the backend and the lock at shutdown; synchronous, not backgrounded."""

    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    if not entry.turn_lock.acquire(blocking=False):
        return {"ok": False, "error": "manager_turn_in_progress"}
    try:
        entry.orchestrator.close()
    except ManagerLoopError as exc:
        return _error(exc)
    finally:
        entry.turn_lock.release()
    return {"ok": True}


def wait_for_idle(repo: str | Path, timeout: float | None = None) -> bool:
    """Test/ops hook: join the repository's in-flight background turn, if any.

    Returns True once no turn is running (immediately if none ever started).
    """

    entry, err = _entry_or_error(repo)
    if err is not None:
        return True
    thread = entry.thread
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()
