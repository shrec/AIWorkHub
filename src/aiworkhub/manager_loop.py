"""Provider-neutral core of the AIWorkHub-owned manager agent loop.

RM-2026-00001, phase 1. Any model can act as a repository's manager through a
:class:`ManagerBackend`, so this module owns only what does not depend on the
provider: the persisted session record with its bounded event log, the bounded
rehydration brief a session starts from, and the orchestrator that keeps one
active session per repository and runs one turn at a time. Server wiring, the
MCP tool inventory, callback consumption and the dashboard panel are later
cards; nothing here launches a CLI or calls a model.

A conversation belongs to the repository, not to a provider: ``ensure`` attaches
to it, persisting a passive record with no backend or model when none exists,
while ``start`` still pins a route to a session. A pinned record left by an
older loop stays readable and is retired through its handoff, never reinterpreted.

Concurrency is refused, never queued: a second ``start`` while another session
holds the repository lock, and any operation while a turn is running, raise a
named :class:`ManagerLoopError`. The caller owns the retry, so a turn is never
silently parked behind another one. The one exception is ``ensure``, whose
concurrent callers wait for the one that persists the conversation and share it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, runtime_checkable

from . import platform_io, repository_state


# v1 records pin a route to the session and carry no successor link; they stay readable
# and are rewritten as v2 whenever their session is next saved.
SCHEMA_ID = "aiworkhub.manager_loop.v2"
LEGACY_SCHEMA_ID = "aiworkhub.manager_loop.v1"
STATE_DIRNAME = "manager_loop"
SESSION_STATUSES = frozenset({"active", "closed"})
EVENT_TYPES = frozenset({"assistant_text", "reasoning", "tool_call", "tool_result", "turn_end", "error"})
OPEN_CARD_STATUSES = ("pending", "processing", "review", "blocked")
DEFAULT_BRIEF_BYTES = 12 * 1024
MIN_BRIEF_BYTES = 256
DEFAULT_CONTEXT_WINDOW_BYTES = 512 * 1024
DEFAULT_ROTATE_FRACTION = 0.75
MAX_EVENT_PAYLOAD_BYTES = 4 * 1024
MAX_TURN_EVENTS = 1000
MAX_LOG_EVENTS = 500
MAX_HANDOFF_BYTES = 8 * 1024
MAX_TURN_EVENT_BYTES = 8 * 1024
MAX_BRIEF_CARDS = 25
KEEP_CLOSED_SESSIONS = 20
SESSION_TOPIC = "management"
CONTEXT_QUERY = "manager_loop"
TURN_EVENT_TYPE = "manager_loop_turn"
NO_HANDOFF = "(none: this is the first session)"
HANDOFF_PROMPT = (
    "Session rotation ({reason}). Write the handoff your successor session starts from, "
    "in exactly three sections: 'done:' what this session finished, 'open:' what is still "
    "in flight, 'next:' the first actions to take. Reply with the handoff only."
)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
_FILE_SUFFIXES = {"sessions": ".json", "events": ".jsonl", "handoffs": ".md"}
_CLIP_MARK = "\n[truncated]"

Source = Callable[[], object]
Writer = Callable[..., Mapping[str, Any]]


class ManagerLoopError(RuntimeError):
    """A named refusal of the manager loop; the message is the reason code."""


class ManagerLoopBusy(ManagerLoopError):
    """A turn is running, so the new operation is refused rather than queued."""


@dataclasses.dataclass(frozen=True)
class ManagerSession:
    """One repository manager conversation.

    A legacy session is pinned to the ``backend_id`` and ``model`` it started on; a
    passive one leaves both empty, because its route belongs to each turn.
    ``previous_session_id`` names the closed session whose handoff a session continues.
    """

    session_id: str
    repo_id: str
    backend_id: str
    model: str
    status: str
    created_at: str
    closed_at: str | None = None
    turn_count: int = 0
    context_estimate_bytes: int = 0
    handoff_ref: str | None = None
    previous_session_id: str | None = None
    title: str = ""

    @property
    def passive(self) -> bool:
        """True while no backend or model is pinned to the conversation."""
        return not self.backend_id and not self.model

    def to_json(self) -> dict[str, Any]:
        return {"schema_id": SCHEMA_ID, **dataclasses.asdict(self)}

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> ManagerSession:
        fields = dataclasses.fields(cls)
        missing = [
            field.name for field in fields
            if field.default is dataclasses.MISSING and field.name not in payload
        ]
        if missing or payload.get("schema_id") not in (SCHEMA_ID, LEGACY_SCHEMA_ID):
            raise ManagerLoopError(f"session_record_invalid:{','.join(missing) or 'schema_id'}")
        if payload.get("status") not in SESSION_STATUSES:
            raise ManagerLoopError("session_record_invalid:status")
        known = {field.name: payload[field.name] for field in fields if field.name in payload}
        return cls(**known)


@runtime_checkable
class ManagerBackend(Protocol):
    """What a provider adapter implements; the loop never sees provider detail.

    ``send`` yields JSON dicts ``{"type": ..., "payload": {...}}`` whose type is
    one of :data:`EVENT_TYPES`, and an ``assistant_text`` payload carries its
    text under ``text``. A backend may report a failure either by raising or by
    yielding an ``error`` event, and the loop records both the same way.
    """

    def start(self, brief: str) -> str:
        """Open a provider session rehydrated from ``brief``; return its reference."""
        ...

    def send(self, message: str) -> Iterable[Mapping[str, Any]]:
        """Run one turn for ``message`` and yield its events."""
        ...

    def close(self) -> None:
        """Release the provider session."""
        ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_session_id() -> str:
    return f"mls-{uuid.uuid4().hex}"


def _failure(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:240]


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def _clip(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes, marking whatever was cut."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    keep = limit - len(_CLIP_MARK)
    return data[:keep].decode("utf-8", "ignore") + _CLIP_MARK if keep > 0 else ""


def _bounded_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``payload`` as plain JSON, or a marked preview once it outgrows the bound."""
    text = _json(payload)
    size = len(text.encode("utf-8"))
    if size <= MAX_EVENT_PAYLOAD_BYTES:
        return dict(json.loads(text))
    preview = _clip(text, MAX_EVENT_PAYLOAD_BYTES // 2)
    return {"truncated": True, "original_bytes": size, "preview": preview}


def _normalize(raw: object) -> tuple[str, dict[str, Any]]:
    """One backend event as ``(type, payload)``; anything malformed becomes an error."""
    if not isinstance(raw, Mapping):
        return "error", {"source": "backend", "error": f"event_not_a_mapping:{type(raw).__name__}"}
    kind = str(raw.get("type") or "")
    if kind not in EVENT_TYPES:
        return "error", {"source": "backend", "error": f"unknown_event_type:{kind[:64]}"}
    payload = raw["payload"] if "payload" in raw else {k: v for k, v in raw.items() if k != "type"}
    return kind, dict(payload) if isinstance(payload, Mapping) else {"value": payload}


def _not_ok(source: str, result: Mapping[str, Any]) -> dict[str, str]:
    return {"source": source, "error": str(result.get("error") or "not_ok")[:240]}


def _publish(path: Path, text: str) -> None:
    """Stage ``text`` beside ``path``, then swap it in through the atomic writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        staging.write_text(text, encoding="utf-8", newline="\n")
        platform_io.atomic_replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


class SessionStore:
    """Session records, handoffs and bounded event logs for one repository.

    Everything lives under the repository's existing runtime state directory
    (``.aiworkhub/runtime/manager_loop``): ``sessions/<id>.json``,
    ``events/<id>.jsonl``, ``handoffs/<id>.md``, the ``active.lock`` an
    orchestrator holds for the life of a pinned session (and only while it
    persists a passive one), and the ``ensure.lock`` that queues concurrent
    ``ensure`` calls. Every file is replaced whole through
    :func:`platform_io.atomic_replace`, so a reader never sees a torn
    record. A log only appends and keeps its newest ``max_events`` lines, and
    ``seq`` keeps counting, so a first ``seq`` above 1 says how much was
    dropped. Closed sessions beyond ``keep_closed`` are pruned oldest first.
    """

    def __init__(
        self,
        root: Path,
        repo_id: str,
        *,
        max_events: int = MAX_LOG_EVENTS,
        keep_closed: int = KEEP_CLOSED_SESSIONS,
    ) -> None:
        if max_events < 1 or keep_closed < 1:
            raise ValueError("max_events and keep_closed must be at least 1")
        self.root = Path(root)
        self.repo_id = repo_id
        self.max_events = max_events
        self.keep_closed = keep_closed

    @classmethod
    def for_repository(
        cls, state: repository_state.RepositoryState, **options: Any
    ) -> SessionStore:
        """The store inside ``state``'s runtime directory, bound to its repository id."""
        return cls(state.runtime_path / STATE_DIRNAME, state.manifest.repo_id, **options)

    @property
    def lock_path(self) -> Path:
        return self.root / "active.lock"

    @property
    def ensure_lock_path(self) -> Path:
        return self.root / "ensure.lock"

    @property
    def selection_path(self) -> Path:
        return self.root / "selected.json"

    def read_selection(self) -> str | None:
        """The session the panel chose to continue, or None when unset or unreadable."""
        path = self.selection_path
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        session_id = str(payload.get("session_id") or "")
        if not _SESSION_ID_RE.fullmatch(session_id):
            return None
        return session_id

    def write_selection(self, session_id: str) -> None:
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise ManagerLoopError("session_id_invalid")
        _publish(
            self.selection_path,
            json.dumps({"session_id": session_id}, sort_keys=True) + "\n",
        )

    def clear_selection(self) -> None:
        self.selection_path.unlink(missing_ok=True)

    def _path(self, kind: str, session_id: str) -> Path:
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise ManagerLoopError("session_id_invalid")
        return self.root / kind / f"{session_id}{_FILE_SUFFIXES[kind]}"

    def save(self, session: ManagerSession) -> None:
        record = json.dumps(session.to_json(), indent=2, sort_keys=True)
        _publish(self._path("sessions", session.session_id), record + "\n")

    def sessions(self) -> list[ManagerSession]:
        """Every session record, oldest first; a foreign or renamed one fails closed."""
        directory = self.root / "sessions"
        found: list[ManagerSession] = []
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            session = ManagerSession.from_json(json.loads(path.read_text(encoding="utf-8")))
            if session.repo_id != self.repo_id or path.name != f"{session.session_id}.json":
                raise ManagerLoopError(f"session_record_mismatch:{path.name}")
            found.append(session)
        return sorted(found, key=lambda item: (item.created_at, item.session_id))

    def latest_closed(self) -> ManagerSession | None:
        closed = [item for item in self.sessions() if item.status == "closed"]
        return max(closed, key=lambda item: (item.closed_at or "", item.session_id), default=None)

    def save_handoff(self, session_id: str, text: str) -> None:
        _publish(self._path("handoffs", session_id), text)

    def read_handoff(self, session_id: str) -> str:
        path = self._path("handoffs", session_id)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def events(self, session_id: str) -> list[dict[str, Any]]:
        path = self._path("events", session_id)
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        return [json.loads(line) for line in lines if line.strip()]

    def append_event(self, session_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        """Append ``event`` with the next ``seq`` and a bounded payload; return it."""
        path = self._path("events", session_id)
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        seq = int(json.loads(lines[-1])["seq"]) + 1 if lines else 1
        record = {**event, "seq": seq, "payload": _bounded_payload(event.get("payload") or {})}
        lines.append(json.dumps(record, sort_keys=True))
        _publish(path, "\n".join(lines[-self.max_events:]) + "\n")
        return record

    def prune(self) -> list[str]:
        """Delete the oldest closed sessions beyond ``keep_closed``; return their ids."""
        closed = sorted(
            (item for item in self.sessions() if item.status == "closed"),
            key=lambda item: (item.closed_at or "", item.session_id),
        )
        doomed = [item.session_id for item in closed[: -self.keep_closed]]
        for session_id in doomed:
            self.delete_session(session_id)
        return doomed

    def delete_session(self, session_id: str) -> None:
        """Remove one conversation's record, events and handoff."""
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise ManagerLoopError("session_id_invalid")
        for kind in ("events", "handoffs", "sessions"):
            self._path(kind, session_id).unlink(missing_ok=True)
        if self.read_selection() == session_id:
            self.clear_selection()


# The production brief sources and writers below import on first use. The loop
# core must stay importable -- and testable against fakes -- without the manager
# tool stack, which resolves a verified manager identity at call time.


def _open_cards(repo: Path) -> str:
    from . import task_store

    lines: list[str] = []
    for status in OPEN_CARD_STATUSES:
        rows = sorted(
            task_store.list_tasks(repo, status=status, limit=MAX_BRIEF_CARDS),
            key=lambda row: (str(row.get("updated_at") or ""), str(row.get("task_id") or "")),
            reverse=True,
        )
        lines += [
            f"- [{status}] {row.get('task_id')} ({row.get('topic') or '?'}, {row.get('runner') or '?'})"
            for row in rows
        ]
    return "\n".join(lines) or "(no open cards)"


def _manager_state() -> object:
    from . import manager_ai_tools

    return manager_ai_tools.session_current_state()


def _manager_context() -> object:
    from . import manager_ai_tools

    return manager_ai_tools.context_graph_search(query=CONTEXT_QUERY, limit=8)


def _manager_rules() -> str:
    from . import agent_tool_instructions

    rules = agent_tool_instructions.POLICY.role
    return "\n".join(["Manager role:", *(f"- {rule}" for rule in rules)])

def _manager_event_write(**fields: Any) -> Mapping[str, Any]:
    """Bind this turn to the manager-chat thread, the way Codex binds its own.

    The audit turn stays one event. The user text and assistant reply are also
    ``chat_message`` rows on ``thread_id`` = the manager session id, so Context
    Graph lists that conversation beside Codex threads instead of folding it
    into the MCP manager episode.
    """
    from . import context_graph, manager_ai_tools

    thread_id = str(fields.pop("thread_id", "") or "")
    session_id = str(fields.pop("session_id", "") or "") or thread_id
    provider = str(fields.pop("provider", "") or "") or "manager_chat"
    user_text = str(fields.pop("user_text", "") or "")
    assistant_text = str(fields.pop("assistant_text", "") or "")
    written = manager_ai_tools.context_graph_event_write(
        thread_id=thread_id,
        session_id=session_id,
        provider=provider,
        **fields,
    )
    if not thread_id or not written.get("ok"):
        return written
    context, _manager = manager_ai_tools._manager_context()
    if context is None:
        return written
    turn_key = str(fields.get("idempotency_key") or thread_id)
    pairs = (("user", user_text), ("assistant", assistant_text))
    for role, text in pairs:
        if not text.strip():
            continue
        try:
            context_graph.append_event(
                context.authority_repo,
                thread_id=thread_id,
                session_id=session_id,
                provider=provider,
                role=role,
                event_type="chat_message",
                content=text,
                source_ref=f"manager_chat:{thread_id}:{role}",
                idempotency_key=f"{turn_key}-{role}",
                metadata={"manager_only": True, "surface": "manager_chat"},
            )
        except (context_graph.ContextGraphError, OSError):
            continue
    return written
    return manager_ai_tools.context_graph_event_write(**fields)


def _manager_session_write(**fields: Any) -> Mapping[str, Any]:
    from . import manager_ai_tools

    return manager_ai_tools.session_write(**fields)


def _render(source: Source) -> str:
    """A section's text; a failing source is stated in the brief, never dropped."""
    try:
        value = source()
    except Exception as exc:  # noqa: BLE001 - the new session must see what it was not told
        return f"unavailable: {_failure(exc)}"
    return value if isinstance(value, str) else _json(value)


class BriefBuilder:
    """The bounded, deterministic brief a new manager session is rehydrated from.

    Sections are filled in one fixed priority -- handoff, open cards, state,
    context, rules -- and bytes are cut from the lowest priority up, so the
    previous session's handoff is the last thing to lose any. A source is read
    only while its section still has room, and the output depends on nothing
    but the source values: anything that is not a string is rendered as
    sorted-key JSON.
    """

    def __init__(
        self,
        *,
        open_cards: Source,
        state: Source,
        context: Source,
        rules: Source,
        max_bytes: int = DEFAULT_BRIEF_BYTES,
    ) -> None:
        if max_bytes < MIN_BRIEF_BYTES:
            raise ValueError(f"max_bytes must be at least {MIN_BRIEF_BYTES}")
        self.max_bytes = max_bytes
        self._sources: tuple[tuple[str, Source], ...] = (
            ("open cards", open_cards),
            ("state", state),
            ("context", context),
            ("rules", rules),
        )

    @classmethod
    def for_repository(cls, repo: Path, *, max_bytes: int = DEFAULT_BRIEF_BYTES) -> BriefBuilder:
        """Production sources: the task store, the manager tools and the role policy."""
        return cls(
            open_cards=lambda: _open_cards(repo),
            state=_manager_state,
            context=_manager_context,
            rules=_manager_rules,
            max_bytes=max_bytes,
        )

    def build(self, handoff: str = "") -> str:
        parts: list[str] = []
        remaining = self.max_bytes
        sections = (("handoff", lambda: handoff.strip() or NO_HANDOFF), *self._sources)
        for title, source in sections:
            heading = f"## {title}\n"
            room = remaining - len(heading.encode("utf-8")) - 1
            if room <= len(_CLIP_MARK):
                break
            block = f"{heading}{_clip(_render(source), room)}\n"
            parts.append(block)
            remaining -= len(block.encode("utf-8"))
        return "".join(parts)


class ManagerOrchestrator:
    """One repository's manager loop: ``ensure``, ``start``, ``send``, ``wake`` and ``rotate``.

    ``ensure`` attaches to the repository's one conversation, persisting a passive
    one -- no backend, no model, no provider call -- when none exists.

    ``start`` takes the repository's session lock and holds it until the session
    is rotated or closed, so a second session -- from this process or another --
    is refused instead of running beside the first. Every backend event is
    logged as it arrives and every turn is recorded through the Context Graph
    event writer. Once ``context_estimate_bytes`` crosses ``rotate_fraction`` of
    ``context_window_bytes`` the session rotates and its successor starts on the
    same backend and model, rehydrated from the handoff; an explicit ``start``
    may pick any other backend and model.
    """

    def __init__(
        self,
        store: SessionStore,
        *,
        backend_factory: Callable[[str, str], ManagerBackend],
        brief_builder: BriefBuilder,
        event_writer: Writer = _manager_event_write,
        session_writer: Writer = _manager_session_write,
        context_window_bytes: int = DEFAULT_CONTEXT_WINDOW_BYTES,
        rotate_fraction: float = DEFAULT_ROTATE_FRACTION,
        clock: Callable[[], str] = _utc_now,
        new_id: Callable[[], str] = _new_session_id,
    ) -> None:
        if not 0 < rotate_fraction <= 1:
            raise ValueError("rotate_fraction must be in (0, 1]")
        self.rotate_at_bytes = int(context_window_bytes * rotate_fraction)
        if self.rotate_at_bytes <= brief_builder.max_bytes:
            # Otherwise a session would already be past its own rotation point
            # on the brief alone, and every turn would rotate.
            raise ValueError("the rotation threshold must exceed the brief budget")
        self.store = store
        self.brief_builder = brief_builder
        self._backend_factory = backend_factory
        self._event_writer = event_writer
        self._session_writer = session_writer
        self._clock = clock
        self._new_id = new_id
        self._slot = threading.Lock()
        self._lock_fd: int | None = None
        self._session: ManagerSession | None = None
        self._backend: ManagerBackend | None = None

    @classmethod
    def for_repository(
        cls,
        repo: str | Path,
        backend_factory: Callable[[str, str], ManagerBackend],
        *,
        brief_bytes: int = DEFAULT_BRIEF_BYTES,
        **options: Any,
    ) -> ManagerOrchestrator:
        """Production wiring for ``repo``: its runtime store and the existing tools."""
        state = repository_state.inspect_repository(repo)
        return cls(
            SessionStore.for_repository(state),
            backend_factory=backend_factory,
            brief_builder=BriefBuilder.for_repository(state.root, max_bytes=brief_bytes),
            **options,
        )

    @property
    def session(self) -> ManagerSession | None:
        """The active session, or ``None`` between sessions."""
        return self._session

    def ensure(self) -> ManagerSession:
        """Attach to the repository's one active conversation, persisting a passive one if none.

        Provider-free and idempotent: no backend is built, started or closed, and every call,
        from this orchestrator, another one or another process, returns the same record.
        That record is the persisted picker selection when one names a saved session;
        otherwise it is the oldest passive conversation. Concurrent callers queue for the
        moment persisting takes, so all of them get that one conversation. A session this
        orchestrator drives is returned as is, even mid-turn. Otherwise the session lock
        proves no owner is live: a pinned record a dead owner left active is retired through
        its mechanical handoff, unless it is the persisted selection, and its provider
        conversation id is never carried over. A live owner elsewhere refuses with
        ``manager_session_already_active``.
        """
        session, backend = self._session, self._backend
        if session is not None and backend is not None:
            return session
        with self._queued(), self._exclusive():
            return self._ensure()

    def start(self, backend_id: str, model: str) -> ManagerSession:
        """Open the repository's one active session, from the latest handoff."""
        with self._exclusive():
            return self._open(backend_id, model)

    def ensure_route(self, backend_id: str, model: str) -> ManagerSession:
        """Bind this turn to the exact selected route (panel picker).

        A session already bound to that route is returned unchanged, so
        same-route sends never rotate. Any other state -- none, passive,
        or active on a different route -- opens the selected route: the
        displaced record is retired through the same mechanical handoff a
        crashed session gets, and the successor rehydrates from it.
        Callers hold the service turn lock across this call.
        """
        if not backend_id.strip() or not model.strip():
            raise ValueError("backend_id and model are required")
        session, backend = self._session, self._backend
        if (
            session is not None
            and backend is not None
            and not session.passive
            and session.backend_id == backend_id
            and session.model == model
        ):
            return session
        if backend is not None:
            self.close()
        with self._exclusive():
            return self._open(backend_id, model)

    def send(self, text: str, *, reasoning: str = "") -> dict[str, Any]:
        """Run one turn; refused, not queued, while another turn is running."""
        if not text.strip():
            raise ValueError("the message is empty")
        if self._backend is not None:
            self._backend.reasoning_level = str(reasoning or "").strip().lower()
        with self._exclusive():
            return self._turn(text, "user_message", "")

    def wake(self, callback: Mapping[str, Any]) -> dict[str, Any]:
        """Run a task callback as a turn worded ``callback: <task_id> -> <state>``."""
        task_id = str(callback.get("task_id") or "").strip()
        state = str(callback.get("state") or callback.get("transition") or "").strip()
        if not task_id or not state:
            raise ValueError("a callback needs a task_id and a state")
        with self._exclusive():
            return self._turn(f"callback: {task_id} -> {state}", "callback", task_id)

    def rotate(self, reason: str) -> dict[str, Any]:
        """End the session: ask for a handoff, persist it, close, release the lock."""
        with self._exclusive():
            return self._rotate(reason, successor=False)

    def close(self) -> None:
        """Release the backend and the lock at shutdown, without a model turn.

        The record stays ``active``: the next ``start`` retires it with a mechanical
        handoff, exactly as it would after a crash, while the next ``ensure`` simply
        attaches a passive one again.
        """
        with self._exclusive():
            backend, self._backend, self._session = self._backend, None, None
            try:
                if backend is not None:
                    backend.close()
            finally:
                self._release_lock()

    def attach(self, session_id: str) -> ManagerSession:
        """Continue one saved conversation. No provider call and no Start.

        The chosen record becomes the repository's active session and the
        persisted selection, so a later ``ensure`` -- including from another
        process -- returns it instead of the oldest passive conversation.
        A closed record is reopened in place so its event log continues.
        Any other active record is closed through a mechanical handoff.
        Refused while this orchestrator is already inside a turn.
        """
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise ManagerLoopError("session_id_invalid")
        with self._queued(), self._exclusive():
            return self._attach(session_id)

    def begin_new(self) -> ManagerSession:
        """Close the current conversation and open a fresh passive one.

        Provider-free. The displaced record gets a mechanical handoff, and
        the selection pointer is cleared so ``ensure`` does not reopen it.
        """
        with self._queued(), self._exclusive():
            return self._begin_new()

    def restore_latest(self) -> ManagerSession | None:
        """Attach the last conversation. None when the repository has no sessions.

        Provider-free and does not create a session. An explicit selection wins;
        otherwise the newest active record, or the newest record if all are closed.
        """
        if self._session is not None:
            return self._session
        with self._queued(), self._exclusive():
            return self._restore_latest()

    def rename(self, session_id: str, title: str) -> ManagerSession:
        """Set the display name of one saved conversation. No provider call."""
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise ManagerLoopError("session_id_invalid")
        cleaned = " ".join(str(title).split())[:80]
        with self._exclusive():
            chosen = next(
                (item for item in self.store.sessions() if item.session_id == session_id),
                None,
            )
            if chosen is None:
                raise ManagerLoopError("session_not_found")
            renamed = dataclasses.replace(chosen, title=cleaned)
            self.store.save(renamed)
            if self._session is not None and self._session.session_id == renamed.session_id:
                self._session = renamed
            return renamed

    def discard(self, session_id: str) -> None:
        """Delete one saved conversation. No provider call.

        If it is the loaded session, the backend and lock are released first.
        """
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise ManagerLoopError("session_id_invalid")
        with self._exclusive():
            if not any(item.session_id == session_id for item in self.store.sessions()):
                raise ManagerLoopError("session_not_found")
            if self._session is not None and self._session.session_id == session_id:
                self._drop_backend()
            self.store.delete_session(session_id)

    def continue_on_route(self, backend_id: str, model: str) -> ManagerSession:
        """Attach the selected model to this conversation, or open the first one.

        The session belongs to the owner. The model is only the route for this
        turn. A different model rebinds this same session and does not open another.
        """
        if not backend_id.strip() or not model.strip():
            raise ValueError("backend_id and model are required")
        session, backend = self._session, self._backend
        if (
            session is not None
            and backend is not None
            and session.backend_id == backend_id
            and session.model == model
        ):
            self.store.write_selection(session.session_id)
            return session
        if backend is not None:
            self._backend = None
            try:
                backend.close()
            finally:
                self._release_lock()
        with self._exclusive():
            if self._session is None:
                opened = self._open(backend_id, model)
                self.store.write_selection(opened.session_id)
                return opened
            return self._bind_existing(backend_id, model)

    def _attach(self, session_id: str) -> ManagerSession:
        chosen = next(
            (item for item in self.store.sessions() if item.session_id == session_id),
            None,
        )
        if chosen is None:
            raise ManagerLoopError("session_not_found")
        if (
            self._session is not None
            and self._backend is not None
            and self._session.session_id == chosen.session_id
            and chosen.status == "active"
        ):
            self._activate_selected(chosen)
            self.store.write_selection(chosen.session_id)
            return self._session
        self._drop_backend()
        chosen = self._activate_selected(chosen)
        self.store.write_selection(chosen.session_id)
        self._session = chosen
        return chosen

    def _begin_new(self) -> ManagerSession:
        self._drop_backend()
        for current in [item for item in self.store.sessions() if item.status == "active"]:
            handoff = self._mechanical_handoff(
                current, "new_session", "the owner started another conversation"
            )
            self._finish(current, handoff, "new_session", mechanical=True)
        self.store.clear_selection()
        return self._ensure()

    def _latest_session(self) -> ManagerSession | None:
        rows = self.store.sessions()
        if not rows:
            return None
        active = [item for item in rows if item.status == "active"]
        pool = active or rows
        return max(pool, key=lambda item: (item.created_at, item.session_id))

    def _restore_latest(self) -> ManagerSession | None:
        self._acquire_lock()
        session: ManagerSession | None = None
        try:
            chosen = self._selected_session() or self._latest_session()
            if chosen is None:
                return None
            session = self._activate_selected(chosen)
            self.store.write_selection(session.session_id)
        finally:
            self._release_lock()
        self._session = session
        return session

    def _bind_existing(self, backend_id: str, model: str) -> ManagerSession:
        """Start the selected route on the loaded session. The session id does not change."""
        session = self._session
        if session is None:
            raise ManagerLoopError("no_active_manager_session")
        self._acquire_lock()
        backend: ManagerBackend | None = None
        try:
            previous = self.store.latest_closed()
            handoff = self.store.read_handoff(previous.session_id) if previous else ""
            if any(
                event.get("type") in ("user_message", "callback", "assistant_text")
                for event in self.store.events(session.session_id)
            ):
                # The new route continues this conversation, so it gets this
                # session's own turns first; the previous closed handoff yields.
                own = _clip(self._mechanical_handoff(
                    session, "route_bind", f"rebound onto {backend_id}/{model}",
                ), MAX_HANDOFF_BYTES)
                room = MAX_HANDOFF_BYTES - len(own.encode("utf-8")) - 2
                if handoff.strip() and room > len(_CLIP_MARK):
                    own = f"{own}\n\n{_clip(handoff, room)}"
                handoff = own
            brief = self.brief_builder.build(handoff)
            backend = self._backend_factory(backend_id, model)
            provider_ref = backend.start(brief)
            if session.passive or (session.backend_id, session.model) != (backend_id, model):
                session = dataclasses.replace(
                    session,
                    backend_id=backend_id,
                    model=model,
                    status="active",
                    closed_at=None,
                    context_estimate_bytes=session.context_estimate_bytes + len(brief.encode("utf-8")),
                )
            self.store.save(session)
            if not any(event.get("type") == "session_start" for event in self.store.events(session.session_id)):
                self._record(session, session.turn_count, "session_start", {
                    "provider_ref": str(provider_ref),
                    "bound_existing": True,
                    "backend_id": backend_id,
                    "model": model,
                })
        except BaseException:
            try:
                if backend is not None:
                    backend.close()
            finally:
                self._release_lock()
            raise
        self._session, self._backend = session, backend
        self.store.write_selection(session.session_id)
        return session

    def _drop_backend(self) -> None:
        backend = self._backend
        self._backend = None
        self._session = None
        try:
            if backend is not None:
                backend.close()
        finally:
            self._release_lock()

    def _selected_session(self) -> ManagerSession | None:
        session_id = self.store.read_selection()
        if not session_id:
            return None
        chosen = next(
            (item for item in self.store.sessions() if item.session_id == session_id),
            None,
        )
        if chosen is None:
            self.store.clear_selection()
        return chosen

    def _activate_selected(self, selected: ManagerSession) -> ManagerSession:
        """Reopen ``selected`` if needed and retire every other active record."""
        if selected.status != "active":
            selected = dataclasses.replace(selected, status="active", closed_at=None)
            self.store.save(selected)
        for other in self.store.sessions():
            if other.session_id == selected.session_id or other.status != "active":
                continue
            handoff = self._mechanical_handoff(
                other, "session_switch", "the owner continued another saved conversation"
            )
            self._finish(other, handoff, "session_switch", mechanical=True)
        return selected

    def __enter__(self) -> ManagerOrchestrator:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        if not self._slot.acquire(blocking=False):
            raise ManagerLoopBusy("manager_turn_in_progress")
        try:
            yield
        finally:
            self._slot.release()

    @contextlib.contextmanager
    def _queued(self) -> Iterator[None]:
        """Queue behind other ``ensure`` calls, here or in another process, on ``ensure.lock``.

        Waiting is bounded and safe because ``ensure`` holds this lock only while it persists.
        The session lock stays a non-blocking probe: a live pinned owner holds it for its life.
        """
        fd = platform_io.open_lock_file(self.store.ensure_lock_path)
        try:
            try:
                platform_io.lock_fd(fd, blocking=True)
            except TimeoutError as exc:
                raise ManagerLoopError("manager_ensure_timeout") from exc
            try:
                yield
            finally:
                platform_io.unlock_fd(fd)
        finally:
            os.close(fd)

    def _active(self) -> tuple[ManagerSession, ManagerBackend]:
        if self._session is None or self._backend is None:
            raise ManagerLoopError("no_active_manager_session")
        return self._session, self._backend

    def _record(
        self, session: ManagerSession, turn: int, kind: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        event = {"at": self._clock(), "turn": turn, "type": kind, "payload": payload}
        return self.store.append_event(session.session_id, event)

    @staticmethod
    def _call(writer: Writer, **fields: Any) -> dict[str, Any]:
        """Run an injected writer; a raise or a non-mapping becomes a failed result."""
        try:
            result = writer(**fields)
        except Exception as exc:  # noqa: BLE001 - returned and logged by the caller, never dropped
            return {"ok": False, "error": _failure(exc)}
        return dict(result) if isinstance(result, Mapping) else {"ok": False, "error": "not_a_mapping"}

    def _open(self, backend_id: str, model: str) -> ManagerSession:
        if self._backend is not None:
            raise ManagerLoopError("manager_session_already_active")
        if not backend_id.strip() or not model.strip():
            raise ValueError("backend_id and model are required")
        self._acquire_lock()
        backend: ManagerBackend | None = None
        try:
            self._session = None  # an attached passive conversation is retired just below
            self._retire_stale()
            previous = self.store.latest_closed()
            handoff = self.store.read_handoff(previous.session_id) if previous else ""
            brief = self.brief_builder.build(handoff)
            backend = self._backend_factory(backend_id, model)
            provider_ref = backend.start(brief)
            session = ManagerSession(
                session_id=self._new_id(),
                repo_id=self.store.repo_id,
                backend_id=backend_id,
                model=model,
                status="active",
                created_at=self._clock(),
                context_estimate_bytes=len(brief.encode("utf-8")),
                previous_session_id=previous.session_id if previous else None,
            )
            self.store.save(session)
            self._record(session, 0, "session_start", {
                "provider_ref": str(provider_ref),
                "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
                "previous_session_id": previous.session_id if previous else None,
            })
        except BaseException:
            try:
                if backend is not None:
                    backend.close()
            finally:
                self._release_lock()
            raise
        self._session, self._backend = session, backend
        # Follow an explicit picker selection onto this successor. Writing a
        # selection here when none existed would pin a dead owner and skip
        # the mechanical retire the next ensure owes that crash.
        if self.store.read_selection():
            self.store.write_selection(session.session_id)
        return session

    def _retire_stale(self, *, keep_passive: bool = False) -> ManagerSession | None:
        """Close what a dead owner left active: holding the lock proves none is live.

        ``keep_passive`` spares, and returns, the oldest passive conversation: it has no
        owner that could be dead, so it is not stale.
        """
        active = [item for item in self.store.sessions() if item.status == "active"]
        kept = next((item for item in active if keep_passive and item.passive), None)
        for stale in [item for item in active if item is not kept]:
            reason = "stale_active_session"
            handoff = self._mechanical_handoff(stale, reason, "its owner exited without rotating")
            self._finish(stale, handoff, reason, mechanical=True)
        return kept

    def _ensure(self) -> ManagerSession:
        """Under the session lock: continue the selected conversation, or the oldest passive.

        Holding the lock proves no pinned owner is live, so a pinned record left active is
        retired through the same mechanical handoff a crashed session gets -- unless the
        owner explicitly selected that record. A selection is the panel's continue target
        and survives a later ``ensure``, including from another process. With no selection
        the oldest passive conversation is kept, or a new one is persisted. The lock is held
        only while persisting: a passive conversation has no owner to hold it for.
        """
        self._acquire_lock()
        try:
            selected = self._selected_session()
            if selected is not None:
                session = self._activate_selected(selected)
            else:
                session = self._retire_stale(keep_passive=True) or self._create_passive()
        finally:
            self._release_lock()
        self._session = session
        return session

    def _create_passive(self) -> ManagerSession:
        """Persist a route-less conversation attached to the latest handoff; no provider runs."""
        previous = self.store.latest_closed()
        session = ManagerSession(
            session_id=self._new_id(),
            repo_id=self.store.repo_id,
            backend_id="",
            model="",
            status="active",
            created_at=self._clock(),
            previous_session_id=previous.session_id if previous else None,
        )
        self.store.save(session)
        self._record(session, 0, "session_start", {
            "passive": True,
            "provider_ref": None,
            "previous_session_id": session.previous_session_id,
            "handoff_ref": previous.handoff_ref if previous else None,
        })
        return session

    def _turn(self, message: str, inbound: str, task_id: str) -> dict[str, Any]:
        session, _ = self._active()
        turn = session.turn_count + 1
        asked = {"text": message, "task_id": task_id} if task_id else {"text": message}
        self._record(session, turn, inbound, asked)
        events, grown, reply = self._exchange(session, turn, message)
        errors = [event["payload"] for event in events if event["type"] == "error"]
        written = self._call(
            self._event_writer,
            role="manager",
            event_type=TURN_EVENT_TYPE,
            content=_clip(f"{message}\n\n{reply}", MAX_TURN_EVENT_BYTES),
            source_ref=f"manager_loop:{session.session_id}:turn:{turn}",
            idempotency_key=f"manager-loop-{session.session_id}-turn-{turn}",
            task_id=task_id,
            thread_id=session.session_id,
            session_id=session.session_id,
            provider=session.backend_id or "manager_chat",
            user_text=message,
            assistant_text=reply,
            metadata={"backend_id": session.backend_id, "model": session.model, "errors": len(errors)},
        )
        if not written.get("ok"):
            events.append(self._record(session, turn, "error", _not_ok("context_graph_event_write", written)))
            errors.append(events[-1]["payload"])
        grown += len(message.encode("utf-8"))
        session = dataclasses.replace(
            session, turn_count=turn, context_estimate_bytes=session.context_estimate_bytes + grown
        )
        self.store.save(session)
        self._session = session
        result: dict[str, Any] = {
            "ok": not errors,
            "session_id": session.session_id,
            "turn": turn,
            "reply": reply,
            "events": events,
            "errors": errors,
            "context_estimate_bytes": session.context_estimate_bytes,
            "rotation": None,
        }
        if session.context_estimate_bytes >= self.rotate_at_bytes:
            result["rotation"] = self._rotate("context_threshold", successor=True)
        return result

    def _exchange(
        self, session: ManagerSession, turn: int, message: str
    ) -> tuple[list[dict[str, Any]], int, str]:
        """Run one backend turn, logging every event as it arrives.

        Returns the logged events, the bytes they added to the model's context
        and the assistant text. A raise from the backend, at the call or
        mid-stream, is logged as an ``error`` event exactly like one the backend
        reports itself, and the session stays usable either way.
        """
        _, backend = self._active()
        events: list[dict[str, Any]] = []
        texts: list[str] = []
        grown = 0
        try:
            for raw in backend.send(message):
                if len(events) >= MAX_TURN_EVENTS:
                    capped = {"source": "loop", "error": "turn_event_limit"}
                    events.append(self._record(session, turn, "error", capped))
                    break
                kind, payload = _normalize(raw)
                grown += len(_json(payload).encode("utf-8"))
                if kind == "assistant_text":
                    texts.append(str(payload.get("text", "")))
                events.append(self._record(session, turn, kind, payload))
        except Exception as exc:  # noqa: BLE001 - a failed turn is an error event, not a dead session
            broke = {"source": "backend", "error": _failure(exc)}
            events.append(self._record(session, turn, "error", broke))
        return events, grown, "".join(texts)

    def _rotate(self, reason: str, *, successor: bool) -> dict[str, Any]:
        session, backend = self._active()
        reason = " ".join(str(reason).split())[:200] or "unspecified"
        turn = session.turn_count + 1
        self._record(session, turn, "handoff_request", {"reason": reason})
        events, _, reply = self._exchange(session, turn, HANDOFF_PROMPT.format(reason=reason))
        failures = [
            str(event["payload"].get("error") or event["payload"])
            for event in events if event["type"] == "error"
        ]
        mechanical = bool(failures) or not reply.strip()
        handoff = reply.strip()
        if mechanical:
            cause = failures[0] if failures else "empty_handoff"
            handoff = self._mechanical_handoff(session, reason, cause)
        try:
            try:
                backend.close()
            except Exception as exc:  # noqa: BLE001 - logged; the session closes regardless
                self._record(session, turn, "error", {"source": "backend_close", "error": _failure(exc)})
            closed, written = self._finish(
                dataclasses.replace(session, turn_count=turn), handoff, reason, mechanical=mechanical
            )
        finally:
            self._session = self._backend = None
            self._release_lock()
        rotation: dict[str, Any] = {
            "session": closed,
            "handoff": self.store.read_handoff(closed.session_id),
            "mechanical": mechanical,
            "writer": written,
            "successor": None,
        }
        try:
            rotation["pruned"] = self.store.prune()
        except OSError as exc:
            rotation["prune_error"] = _failure(exc)
        if successor:
            try:
                rotation["successor"] = self._open(session.backend_id, session.model)
            except Exception as exc:  # noqa: BLE001 - the rotation is done; say what did not follow
                rotation["successor_error"] = _failure(exc)
        return rotation

    def _finish(
        self, session: ManagerSession, handoff: str, reason: str, *, mechanical: bool
    ) -> tuple[ManagerSession, dict[str, Any]]:
        """Persist the handoff locally and through the session writer, then close."""
        text = _clip(handoff, MAX_HANDOFF_BYTES)
        self.store.save_handoff(session.session_id, text)
        written = self._call(
            self._session_writer,
            action="handoff",
            topic=SESSION_TOPIC,
            content=text,
            idempotency_key=f"manager-loop-{session.session_id}-handoff",
            provenance=f"manager_loop:{session.session_id}:{reason}",
        )
        if not written.get("ok"):
            self._record(session, session.turn_count, "error", _not_ok("session_write", written))
        document = written.get("document_id") if written.get("ok") else None
        ref = (
            f"session_document:{document}" if document is not None
            else f"manager_loop:{session.session_id}:handoff"
        )
        closed = dataclasses.replace(
            session, status="closed", closed_at=self._clock(), handoff_ref=ref
        )
        self.store.save(closed)
        if mechanical:
            self._mark_interrupted_turns(session, reason)
        ended = {"reason": reason, "mechanical": mechanical, "handoff_ref": ref}
        self._record(closed, closed.turn_count, "session_close", ended)
        return closed, written

    def _mark_interrupted_turns(self, session: ManagerSession, reason: str) -> None:
        """One terminal marker per turn its dead owner never closed.

        Resume decision (reference: claude-code-main conversationRecovery):
        an inbound turn (user_message/callback) with no later turn_end/error
        must never read as possibly-run to the successor. Only a mechanical
        retire writes this; a normally finished turn already has its marker.
        """
        terminal = {"turn_end", "error"}
        inbound = {"user_message", "callback"}
        orphaned: list[int] = []
        for event in self.store.events(session.session_id):
            turn = event["turn"]
            if event["type"] in inbound:
                if turn not in orphaned:
                    orphaned.append(turn)
            elif event["type"] in terminal:
                orphaned = [t for t in orphaned if t != turn]
        for turn in orphaned:
            self._record(
                session,
                turn,
                "error",
                {"source": "manager_turn_interrupted", "error": f"manager_turn_interrupted:{reason}"},
            )

    def _mechanical_handoff(self, session: ManagerSession, reason: str, cause: str) -> str:
        """The handoff written from the event log alone, when no model can write it."""
        events = self.store.events(session.session_id)
        handled = [
            str(event["payload"].get("text", ""))
            for event in events if event["type"] in ("user_message", "callback")
        ]
        replies = [
            str(event["payload"].get("text", ""))
            for event in events if event["type"] == "assistant_text"
        ]
        route = "no route" if session.passive else f"{session.backend_id}/{session.model}"
        return "\n".join([
            f"Mechanical handoff for {session.session_id} ({route}), "
            f"rotated for {reason} after {session.turn_count} turns: {cause}",
            "done:",
            *([f"- handled: {_clip(text, 300)}" for text in handled[-5:]] or ["- no turns ran"]),
            "open:",
            f"- last reply: {_clip(replies[-1], 600)}" if replies else "- no reply was recorded",
            "next:",
            "- Rebuild the picture from the open cards and state below before acting.",
        ])

    def _acquire_lock(self) -> None:
        fd = platform_io.open_lock_file(self.store.lock_path)
        try:
            platform_io.lock_fd(fd, blocking=False)
        except OSError as exc:
            os.close(fd)
            raise ManagerLoopError("manager_session_already_active") from exc
        self._lock_fd = fd

    def _release_lock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is None:
            return
        try:
            platform_io.unlock_fd(fd)
        finally:
            os.close(fd)
