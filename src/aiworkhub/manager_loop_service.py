"""Process-wide manager loop registry: one :class:`ManagerOrchestrator` per repository.

``send`` and ``rotate`` run a manager turn on a background thread and return
at once; a concurrent call is refused with ``manager_turn_in_progress`` and
nothing is queued. ``status`` and ``events`` let a caller poll a running
session incrementally. Every :class:`~aiworkhub.manager_loop.ManagerLoopError`
becomes ``{"ok": False, "error": <reason code>}`` here -- nothing raises
across this module's boundary.

Apart from the first ``send`` to a passive conversation, this module resolves no
manager route and reads no repository default: every function takes the
repository root explicitly, so it is directly testable with a fake backend
factory and a ``tmp_path`` repo. That first ``send`` resolves the repository's one
configured, policy-authorized manager route under the turn lock, pins it and
starts the turn, so no explicit ``start`` is needed; with no such route it is
refused as ``no_manager_route_available`` and the conversation stays passive. An
explicit ``start`` stays authoritative and a pinned session is never re-routed.
The ``aiworkhub_manager_loop_*`` MCP tools in ``server.py`` resolve the
caller's repository through the shared manager route gate and pass it in.
"""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from .manager_loop import ManagerLoopError, ManagerOrchestrator, ManagerSession
from .manager_loop_backends import MANAGER_BACKEND_IDS, cli_discovers_model, manager_backend_factory
from . import callback_store, manager_loop_wake, model_settings, workforce_catalog

_REGISTRY_LOCK = threading.Lock()
_ENTRIES: dict[str, "_Entry"] = {}

WAKE_IDLE_POLL_SECONDS = manager_loop_wake.DEFAULT_IDLE_POLL_SECONDS
WAKE_RETRY_POLL_SECONDS = manager_loop_wake.DEFAULT_RETRY_POLL_SECONDS
default_callback_source = manager_loop_wake.default_callback_source

NO_MANAGER_ROUTE_ERROR = "no_manager_route_available"
NO_MANAGER_ROUTE_HINT = (
    "enable a manager-capable route in the repository model policy or workforce catalog, "
    "or start one explicitly"
)


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
    wake: manager_loop_wake.WakeConsumer | None = None
    wake_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    wake_cap_per_hour: int = manager_loop_wake.DEFAULT_CAP_PER_HOUR


def _seat_env_provider(repo_key: str) -> Callable[[str, str], Mapping[str, str]]:
    """Seat MCP bindings per backend, provisioned at backend build time.

    Runs inside start/pin (both launching-gated); provisioning writes only
    the seat's own 0600 configs under ``.aiworkhub/runtime/manager-seat``.
    Identity is never minted here -- it inherits from the gated child.
    """

    def provide(backend_id: str, model: str) -> Mapping[str, str]:
        import sys

        from . import manager_loop_backends, worker_ai_tools_mcp

        return manager_loop_backends.provision_manager_seat_env(
            repo_key,
            backend_id,
            python_executable=sys.executable,
            package_import_root=worker_ai_tools_mcp.resolve_host_package_import_root(),
        )

    return provide


def _entry_for(repo: str | Path) -> _Entry:
    key = str(Path(repo).resolve())
    with _REGISTRY_LOCK:
        entry = _ENTRIES.get(key)
        if entry is None:
            orchestrator = ManagerOrchestrator.for_repository(
                repo,
                manager_backend_factory(repo, seat_env_provider=_seat_env_provider(key)),
            )
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


def _route_authorized(policy: Mapping[str, Any], worker: Mapping[str, Any]) -> bool:
    provider, adapter, model = worker["provider"], worker["adapter_id"], worker["model"]
    owner, transport = workforce_catalog.policy_route_identity(provider, adapter)
    return model_settings.evaluate_state(
        policy, provider=owner, adapter=transport, model=model
    ) and model_settings.evaluate_state(policy, provider=provider, adapter=adapter, model=model)


def _eligible_manager_routes(
    repo: str | Path,
    *,
    load_catalog: Callable[[str | Path], Mapping[str, Any]] = workforce_catalog.load_catalog,
    load_policy: Callable[[str | Path], Mapping[str, Any]] = model_settings.load,
) -> list[dict[str, Any]]:
    """Every enabled, policy-authorized manager route, catalog order kept."""

    policy = load_policy(repo)
    return [
        worker
        for worker in load_catalog(repo)["workers"]
        if worker["manager"]
        and worker["enabled"]
        and worker["adapter_id"] in MANAGER_BACKEND_IDS
        and _route_authorized(policy, worker)
    ]


def resolve_manager_route(
    repo: str | Path,
    *,
    load_catalog: Callable[[str | Path], Mapping[str, Any]] = workforce_catalog.load_catalog,
    load_policy: Callable[[str | Path], Mapping[str, Any]] = model_settings.load,
) -> tuple[str, str] | None:
    """The highest-quality enabled, policy-authorized manager route (catalog order on ties)."""

    eligible = _eligible_manager_routes(repo, load_catalog=load_catalog, load_policy=load_policy)
    if not eligible:
        return None
    best = max(eligible, key=lambda worker: worker["quality_ceiling"])
    return best["adapter_id"], best["model"]


def authorize_selected_route(
    repo: str | Path,
    backend_id: str,
    model: str,
    *,
    load_catalog: Callable[[str | Path], Mapping[str, Any]] = workforce_catalog.load_catalog,
    load_policy: Callable[[str | Path], Mapping[str, Any]] = model_settings.load,
) -> tuple[str, str] | None:
    """The exact picker route when this repository may run it, else ``None``.

    A catalog-declared, policy-authorized route is accepted. A route the
    catalog does not declare is accepted only when the CLI itself lists it
    and repository model policy still enables that identity. A declared
    route that policy disabled is never re-authorized through CLI discovery.
    """

    for worker in _eligible_manager_routes(repo, load_catalog=load_catalog, load_policy=load_policy):
        if worker["adapter_id"] == backend_id and worker["model"] == model:
            return backend_id, model
    if any(
        worker["adapter_id"] == backend_id and worker["model"] == model
        for worker in load_catalog(repo)["workers"]
    ):
        return None
    if backend_id not in MANAGER_BACKEND_IDS or not cli_discovers_model(repo, backend_id, model):
        return None
    try:
        provider, adapter = model_settings.policy_identity_for_adapter(backend_id)
        allowed = model_settings.evaluate_state(
            load_policy(repo), provider=provider, adapter=adapter, model=model
        )
    except model_settings.ModelSettingsError:
        return None
    if not allowed:
        return None
    return backend_id, model


def _no_manager_route(detail: str = "") -> dict[str, Any]:
    refusal: dict[str, Any] = {
        "ok": False,
        "error": NO_MANAGER_ROUTE_ERROR,
        "hint": NO_MANAGER_ROUTE_HINT,
    }
    if detail:
        refusal["detail"] = detail
    return refusal


def _pin_first_route(
    entry: _Entry, repo: str | Path
) -> tuple[ManagerSession | None, dict[str, Any] | None]:
    """Pin the passive conversation to the resolved route; the caller holds ``turn_lock``."""

    try:
        route = resolve_manager_route(repo)
    except Exception as exc:  # noqa: BLE001 - an unreadable catalog or policy authorizes nothing
        return None, _no_manager_route(f"{type(exc).__name__}: {exc}"[:240])
    if route is None:
        return None, _no_manager_route()
    try:
        session = entry.orchestrator.start(*route)
        _ensure_wake_started(entry, repo)
    except ManagerLoopError as exc:
        return None, _error(exc)
    except Exception as exc:  # noqa: BLE001 - pinning must never cross the MCP boundary
        return None, {"ok": False, "error": f"manager_loop_unavailable:{type(exc).__name__}"}
    return session, None


def _turn_delivered(raw: Mapping[str, Any]) -> bool:
    """Whether the model received the turn's message.

    A turn is delivered when it is ok, or when every error it carries arose
    after the model was reached (the Context Graph turn write, the event cap).
    """
    if raw.get("ok"):
        return True
    errors = raw.get("errors") or ()
    return bool(errors) and all(
        isinstance(error, Mapping)
        and (
            error.get("source") == "context_graph_event_write"
            or (error.get("source"), error.get("error")) == ("loop", "turn_event_limit")
        )
        for error in errors
    )


def _dispatch_turn(
    repo: str | Path,
    action: Callable[[ManagerOrchestrator], dict[str, Any]],
    *,
    record_last_turn: bool,
    pin_passive_route: bool = False,
    route: tuple[str, str] | None = None,
    on_finished: Callable[[bool], None] | None = None,
) -> dict[str, Any]:
    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    if not entry.turn_lock.acquire(blocking=False):
        return {"ok": False, "error": "manager_turn_in_progress"}
    session = entry.orchestrator.session
    if route is not None:
        # Selected model: no session yet opens the first one. A loaded
        # session stays that session; the model is only rebound onto it.
        try:
            session = entry.orchestrator.continue_on_route(*route)
            _ensure_wake_started(entry, repo)
        except ManagerLoopError as exc:
            entry.turn_lock.release()
            return _error(exc)
        except Exception as exc:  # noqa: BLE001 - route binding must never cross the MCP boundary
            entry.turn_lock.release()
            return {"ok": False, "error": f"manager_loop_unavailable:{type(exc).__name__}"}
    elif pin_passive_route and session is None:
        try:
            session = entry.orchestrator.ensure()
        except ManagerLoopError as exc:
            entry.turn_lock.release()
            return _error(exc)
        except Exception as exc:  # noqa: BLE001 - ensure must never cross the MCP boundary
            entry.turn_lock.release()
            return {"ok": False, "error": f"manager_loop_unavailable:{type(exc).__name__}"}
    if route is None and pin_passive_route and session is not None and session.passive:
        session, err = _pin_first_route(entry, repo)
        if err is not None:
            entry.turn_lock.release()
            return err
    if session is None:
        entry.turn_lock.release()
        return {"ok": False, "error": "no_active_manager_session"}
    turn = session.turn_count + 1
    session_id = session.session_id

    def run() -> None:
        delivered = False
        try:
            raw = action(entry.orchestrator)
            delivered = _turn_delivered(raw)
            if record_last_turn:
                entry.last_turn = {
                    "turn": raw.get("turn", turn),
                    "ok": bool(raw.get("ok")),
                    "errors": list(raw.get("errors") or []),
                    "reply": str(raw.get("reply", ""))[:500],
                }
        except Exception as exc:  # noqa: BLE001 - a dead thread must still record the turn's outcome
            delivered = False
            if record_last_turn:
                detail = f"{type(exc).__name__}: {exc}"[:240]
                entry.last_turn = {"turn": turn, "ok": False, "errors": [detail], "reply": ""}
        finally:
            entry.turn_lock.release()
            # Report the started turn's outcome exactly once, after the lock is
            # free (a wake consumer may start its next member from the callback).
            # A failing callback must never skip the re-arm below.
            if on_finished is not None:
                try:
                    on_finished(delivered)
                except Exception:  # noqa: BLE001 - the outcome callback must never kill the turn thread
                    pass
            # Re-read the seat now the turn is over. A session that idled past the
            # seat lease got no wake consumer when it was loaded, and whichever send
            # revived it just logged its activity: that arms the consumer, leaves a
            # running one alone while the session still holds the seat, and stops it
            # when the turn ended the session (rotate). A close() that lands between
            # the release above and this call has already dropped the orchestrator's
            # session, so the re-arm reads none and starts nothing: a closed entry
            # does not get a consumer back from the turn that ended just before it.
            _ensure_wake_started(entry, repo)

    thread = threading.Thread(target=run, daemon=True)
    entry.thread = thread
    thread.start()
    return {"ok": True, "session_id": session_id, "turn": turn, "state": "running"}

def start(
    repo: str | Path,
    backend_id: str,
    model: str,
    *,
    wake_cap_per_hour: int = manager_loop_wake.DEFAULT_CAP_PER_HOUR,
) -> dict[str, Any]:
    """Open the repository's one active session; synchronous, not backgrounded."""

    try:
        cap = max(0, int(wake_cap_per_hour))
    except Exception as exc:  # noqa: BLE001 - a bad cap must not take the turn lock or cross the MCP boundary
        return {"ok": False, "error": f"manager_loop_unavailable:{type(exc).__name__}"}
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
    entry.wake_cap_per_hour = cap
    _ensure_wake_started(entry, repo)
    return {"ok": True, "session": session.to_json()}


def ensure(repo: str | Path) -> dict[str, Any]:
    """Attach to the repository's one passive conversation; synchronous, provider-free."""

    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    try:
        session = entry.orchestrator.ensure()
    except ManagerLoopError as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001 - ensure must never cross the MCP boundary
        return {"ok": False, "error": f"manager_loop_unavailable:{type(exc).__name__}"}
    return {"ok": True, "session": session.to_json(), "running": entry.turn_lock.locked()}


def restore(repo: str | Path) -> dict[str, Any]:
    """Attach the last saved conversation. Does not create one."""

    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    try:
        session = entry.orchestrator.restore_latest()
    except ManagerLoopError as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001 - restore must never cross the MCP boundary
        return {"ok": False, "error": f"manager_loop_unavailable:{type(exc).__name__}"}
    if session is not None:
        _ensure_wake_started(entry, repo)
    return {
        "ok": True,
        "session": session.to_json() if session is not None else None,
        "running": entry.turn_lock.locked(),
    }


def rename_session(repo: str | Path, session_id: str, title: str) -> dict[str, Any]:
    """Rename one saved conversation. Refused while a turn runs."""

    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    if not entry.turn_lock.acquire(blocking=False):
        return {"ok": False, "error": "manager_turn_in_progress"}
    try:
        session = entry.orchestrator.rename(session_id, title)
    except ManagerLoopError as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001 - rename must never cross the MCP boundary
        return {"ok": False, "error": f"manager_loop_unavailable:{type(exc).__name__}"}
    finally:
        entry.turn_lock.release()
    return {"ok": True, "session": session.to_json()}


def discard_session(repo: str | Path, session_id: str) -> dict[str, Any]:
    """Delete one saved conversation. Refused while a turn runs."""

    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    if not entry.turn_lock.acquire(blocking=False):
        return {"ok": False, "error": "manager_turn_in_progress"}
    try:
        entry.orchestrator.discard(session_id)
    except ManagerLoopError as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001 - discard must never cross the MCP boundary
        return {"ok": False, "error": f"manager_loop_unavailable:{type(exc).__name__}"}
    finally:
        entry.turn_lock.release()
    # Discarding the loaded conversation leaves none attached, and a consumer with
    # no seat to serve has nothing to wake.
    _ensure_wake_started(entry, repo)
    session = entry.orchestrator.session
    return {"ok": True, "session": session.to_json() if session is not None else None}


def send(
    repo: str | Path,
    text: str,
    backend_id: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
) -> dict[str, Any]:
    """Run one manager turn on a background thread.

    Refused with ``manager_turn_in_progress``, not queued, while one runs.
    With an explicit picker route the turn binds to it (same route is a
    no-op, any other state re-opens the selection with a mechanical
    handoff); without one, the first send to a passive conversation pins
    the one route :func:`resolve_manager_route` names. A named-but-unrunnable
    route is refused before anything spawns. ``reasoning`` is the owner's
    depth choice for this turn; blank keeps the provider default.
    """

    route: tuple[str, str] | None = None
    if backend_id is not None or model is not None:
        if not (backend_id or "").strip() or not (model or "").strip():
            return {"ok": False, "error": "manager_route_selection_incomplete"}
        route = authorize_selected_route(repo, backend_id.strip(), model.strip())
        if route is None:
            return {
                "ok": False,
                "error": f"manager_backend_unavailable:{backend_id.strip()}:{model.strip()}",
            }
    level = str(reasoning or "").strip().lower()
    return _dispatch_turn(
        repo,
        lambda orchestrator: orchestrator.send(text, reasoning=level),
        record_last_turn=True,
        pin_passive_route=bool(text.strip()),
        route=route,
    )


def rotate(repo: str | Path, reason: str) -> dict[str, Any]:
    """End the session with a handoff, on a background thread.

    Refused with ``manager_turn_in_progress``, not queued, while a turn runs.
    """

    return _dispatch_turn(
        repo, lambda orchestrator: orchestrator.rotate(reason), record_last_turn=False
    )


def _session_catalog(store: Any) -> list[dict[str, Any]]:
    """Newest saved conversations first, bounded, for the panel picker."""

    try:
        rows = store.sessions()
    except ManagerLoopError:
        return []
    catalog: list[dict[str, Any]] = []
    for session in reversed(rows):
        catalog.append({
            "session_id": session.session_id,
            "title": session.title,
            "status": session.status,
            "backend_id": session.backend_id,
            "model": session.model,
            "created_at": session.created_at,
            "turn_count": session.turn_count,
            "passive": session.passive,
        })
        if len(catalog) >= 24:
            break
    return catalog


def status(repo: str | Path) -> dict[str, Any]:
    """The active session, whether a turn is running, and the last turn's outcome."""

    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    session = entry.orchestrator.session
    return {
        "ok": True,
        "session": session.to_json() if session is not None else None,
        "sessions": _session_catalog(entry.orchestrator.store),
        "running": entry.turn_lock.locked(),
        "last_turn": entry.last_turn,
        "wake": _wake_status(entry),
    }


def continue_session(repo: str | Path, session_id: str) -> dict[str, Any]:
    """Attach the panel to one saved conversation. Refused while a turn runs."""

    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    if not entry.turn_lock.acquire(blocking=False):
        return {"ok": False, "error": "manager_turn_in_progress"}
    try:
        session = entry.orchestrator.attach(session_id)
    except ManagerLoopError as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001 - continue must never cross the MCP boundary
        return {"ok": False, "error": f"manager_loop_unavailable:{type(exc).__name__}"}
    finally:
        entry.turn_lock.release()
    # The consumer follows the attached conversation: one that holds the seat starts
    # it (a conversation restored after idling past the lease had none), and one that
    # fails the rule, passive or idle past the lease, stops a consumer that was running.
    _ensure_wake_started(entry, repo)
    return {"ok": True, "session": session.to_json()}


def begin_new(repo: str | Path) -> dict[str, Any]:
    """Close the current conversation and open a fresh passive one."""

    entry, err = _entry_or_error(repo)
    if err is not None:
        return err
    if not entry.turn_lock.acquire(blocking=False):
        return {"ok": False, "error": "manager_turn_in_progress"}
    try:
        session = entry.orchestrator.begin_new()
    except ManagerLoopError as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001 - a new session must never cross the MCP boundary
        return {"ok": False, "error": f"manager_loop_unavailable:{type(exc).__name__}"}
    finally:
        entry.turn_lock.release()
    # The fresh conversation is passive, so it does not hold the seat: the consumer
    # that woke the one just closed stops here instead of claiming for a session
    # with no route.
    _ensure_wake_started(entry, repo)
    return {"ok": True, "session": session.to_json()}


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
    partial = entry.orchestrator.partial
    mine = partial if partial and partial.get("session_id") == session_id else None
    return {"ok": True, "events": filtered, "partial": mine}


def close(repo: str | Path) -> dict[str, Any]:
    """Release the backend and the lock at shutdown; synchronous, not backgrounded.

    The orchestrator drops its session first and the wake consumer is retired after,
    under ``wake_lock``, the lock ``_ensure_wake_started`` judges the session under. A
    re-arm in flight, the one at the end of a turn, either finishes first and is retired
    here or runs after and reads no session: nothing outlives a successful close.
    """

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
    _stop_wake(entry)
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


def _wake_on_own_route(
    repo: str | Path, orchestrator: ManagerOrchestrator, member: Mapping[str, Any]
) -> dict[str, Any]:
    """Run one callback's turn; a session loaded from disk gets its backend back first.

    Restore and continue attach a conversation without a backend, and a wake has
    no owner present to pick one. So it binds only the route the session itself
    persisted, and only while this repository still authorizes that route. A
    refusal raises: the turn fails undelivered, its callback stays un-acked and
    the consumer backs off. A session that already has its backend is not asked.
    """

    session = orchestrator.session
    if session is not None and not session.passive and not orchestrator.bound:
        route = authorize_selected_route(repo, session.backend_id, session.model)
        if route is None:
            raise ManagerLoopError(
                f"manager_backend_unavailable:{session.backend_id}:{session.model}"
            )
        orchestrator.continue_on_route(*route)
    return orchestrator.wake(member)


def _wake_dispatch(
    repo: str | Path, member: Mapping[str, Any], done: Callable[[bool], None]
) -> bool:
    """Start one callback's turn through the SAME non-blocking lock ``send`` uses.

    Returns whether the turn STARTED; ``done(delivered)`` follows once it finishes.
    """

    result = _dispatch_turn(
        repo,
        lambda orchestrator: _wake_on_own_route(repo, orchestrator, member),
        record_last_turn=True,
        on_finished=done,
    )
    return bool(result.get("ok"))


def _session_holds_seat(entry: _Entry, session: ManagerSession | None) -> bool:
    """Whether ``session`` holds the manager seat right now; no session holds none.

    The rule is ``callback_store.manager_chat_record_holds_seat``, the one the
    callback origin and the bootstrap seat share, judged on the session's own
    record and on its event log under the store.
    """

    return session is not None and callback_store.manager_chat_record_holds_seat(
        session.to_json(), state_dir=entry.orchestrator.store.root
    )


def _ensure_wake_started(entry: _Entry, repo: str | Path) -> None:
    """Run this repository's one wake consumer exactly while its session holds the seat.

    A second call is a no-op. Only a session that holds the manager seat gets a
    consumer: a passive conversation has no route to wake, and one nobody has
    used within the seat lease is a leftover, not the manager
    (``callback_store.manager_chat_record_holds_seat``, the rule the callback
    origin and the bootstrap seat share). The same rule stops a consumer that is
    already running once the attached session is gone, passive or idle past the
    lease: left alone, its claim would keep copying pending callbacks onto a
    conversation that is not the manager.

    The session is read and judged inside ``wake_lock``, the lock that installs
    and retires the consumer. A swap that lands before the read is seen by it;
    one that lands after is followed by its own call, which waits for the lock
    and reads the new session. Either way the last call decides, so a consumer
    never outlives the session that failed the rule. A closed entry has no session
    (``ManagerOrchestrator.close`` drops it) and so fails the rule like any other: a
    re-arm that lands after ``close`` starts nothing.

    The claim origin is read at claim time, so a session switch rebinds
    pending callbacks onto whichever conversation is active, and it is empty
    (nothing is claimed) once the attached session no longer holds the seat.
    """

    with entry.wake_lock:
        if not _session_holds_seat(entry, entry.orchestrator.session):
            retired = _detach_wake(entry)
        else:
            retired = None
            if entry.wake is None:
                def active_session_id() -> str:
                    session = entry.orchestrator.session
                    return session.session_id if _session_holds_seat(entry, session) else ""

                claim, ack = default_callback_source(session_id=active_session_id)
                entry.wake = manager_loop_wake.WakeConsumer(
                    claim=claim,
                    ack=ack,
                    dispatch=lambda member, done: _wake_dispatch(repo, member, done),
                    cap_per_hour=entry.wake_cap_per_hour,
                    idle_poll_seconds=WAKE_IDLE_POLL_SECONDS,
                    retry_poll_seconds=WAKE_RETRY_POLL_SECONDS,
                )
            entry.wake.start()
    if retired is not None:
        retired.stop()


def _detach_wake(entry: _Entry) -> manager_loop_wake.WakeConsumer | None:
    """Forget this repository's wake consumer and return it; the caller holds ``wake_lock``.

    Stopping joins the consumer's thread, so the caller does that after releasing the lock.
    """

    wake, entry.wake = entry.wake, None
    return wake


def _stop_wake(entry: _Entry) -> None:
    """Stop and forget this repository's wake consumer, if any."""

    with entry.wake_lock:
        wake = _detach_wake(entry)
    if wake is not None:
        wake.stop()


def _wake_status(entry: _Entry) -> dict[str, Any]:
    with entry.wake_lock:
        wake = entry.wake
    if wake is None:
        return {
            "running": False,
            "queued": 0,
            "turns_this_hour": 0,
            "cap": entry.wake_cap_per_hour,
            "in_flight": 0,
            "failed_turns": 0,
            "retry_in_seconds": 0.0,
        }
    return wake.status()
