"""Multi-repository window/session mux for AIWorkHub.

The mux is deliberately small and import-safe.  It models the invariants the
VS Code extension must preserve: a window has exactly one active repository
session, route identities include repository and claim-episode scope, and
callbacks are durable by origin instead of rebound to whichever repository is
currently selected.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .repository_state import RepositoryState, inspect_repository
from .route_identity import CoordinatorRouteKey, RepoRouteKey


@dataclass(frozen=True, slots=True)
class CompositeRouteIdentity:
    repo_id: str
    thread_id: str
    task_id: str
    claim_episode: str

    def __post_init__(self) -> None:
        route = RepoRouteKey(
            repo_id=self.repo_id,
            thread_id=self.thread_id,
            task_id=self.task_id,
            event_id=self.claim_episode,
        )
        object.__setattr__(self, "repo_id", route.repo_id)
        object.__setattr__(self, "thread_id", route.thread_id)
        object.__setattr__(self, "task_id", route.task_id)
        object.__setattr__(self, "claim_episode", route.event_id)

    def route_key(self, callback_event_id: str = "") -> RepoRouteKey:
        event = callback_event_id or self.claim_episode
        return RepoRouteKey(
            repo_id=self.repo_id,
            thread_id=self.thread_id,
            task_id=self.task_id,
            event_id=event,
        )

    def canonical(self) -> str:
        return self.route_key(self.claim_episode).canonical()

    def digest(self) -> str:
        return self.route_key(self.claim_episode).digest()

    def as_dict(self) -> dict[str, str]:
        return {
            "repo_id": self.repo_id,
            "thread_id": self.thread_id,
            "task_id": self.task_id,
            "claim_episode": self.claim_episode,
        }

    def coordinator_route(
        self,
        *,
        provider: str,
        window_id: str,
        session_id: str = "",
        callback_event_id: str = "",
    ) -> CoordinatorRouteKey:
        return CoordinatorRouteKey(
            repo_id=self.repo_id,
            provider=provider,
            window_id=window_id,
            thread_id=self.thread_id,
            session_id=session_id,
            task_id=self.task_id,
            event_id=callback_event_id or self.claim_episode,
        )


@dataclass(frozen=True, slots=True)
class RepositoryDescriptor:
    root: Path
    repo_id: str
    repo_name: str
    storage_ready: bool
    manifest_path: Path

    @classmethod
    def inspect(cls, root: str | Path) -> "RepositoryDescriptor":
        state = inspect_repository(root)
        return cls.from_state(state)

    @classmethod
    def from_state(cls, state: RepositoryState) -> "RepositoryDescriptor":
        return cls(
            root=state.root,
            repo_id=state.manifest.repo_id,
            repo_name=state.manifest.repo_name,
            storage_ready=all(path.is_dir() for path in state.durable_paths.values()),
            manifest_path=state.manifest_path,
        )

    def display(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "repo_name": self.repo_name,
            "storage_ready": self.storage_ready,
        }


@dataclass(slots=True)
class WindowRepositorySession:
    window_id: str
    repository: RepositoryDescriptor
    claim_episode: str
    child_id: str
    stopped: bool = False
    pending: list[RepoRouteKey | CoordinatorRouteKey] = field(default_factory=list)

    def identity(self, thread_id: str, task_id: str) -> CompositeRouteIdentity:
        if self.stopped:
            raise RuntimeError("repository_session_stopped")
        return CompositeRouteIdentity(
            repo_id=self.repository.repo_id,
            thread_id=thread_id,
            task_id=task_id,
            claim_episode=self.claim_episode,
        )

    def enqueue_callback(self, thread_id: str, task_id: str, callback_event_id: str) -> RepoRouteKey:
        route = self.identity(thread_id, task_id).route_key(callback_event_id)
        self.pending.append(route)
        return route

    def enqueue_coordinator_callback(
        self,
        *,
        provider: str,
        task_id: str,
        callback_event_id: str,
        thread_id: str = "",
        session_id: str = "",
    ) -> CoordinatorRouteKey:
        route = CoordinatorRouteKey(
            repo_id=self.repository.repo_id,
            provider=provider,
            window_id=self.window_id,
            thread_id=thread_id,
            session_id=session_id,
            task_id=task_id,
            event_id=callback_event_id,
        )
        self.pending.append(route)
        return route

    def drain(self) -> tuple[RepoRouteKey | CoordinatorRouteKey, ...]:
        drained = tuple(self.pending)
        self.pending.clear()
        self.stopped = True
        return drained


@dataclass(frozen=True, slots=True)
class DurableCallback:
    route: RepoRouteKey | CoordinatorRouteKey
    payload: dict[str, Any]


class RepositoryWindowMux:
    """Owns repo sessions and callback routing for multiple VS Code windows."""

    def __init__(self) -> None:
        self._sessions: dict[str, WindowRepositorySession] = {}
        self._durable_callbacks: dict[str, DurableCallback] = {}
        self._stopped_children: list[str] = []

    @staticmethod
    def new_window_id(label: str = "window") -> str:
        digest = hashlib.sha256(f"{label}:{uuid.uuid4().hex}".encode("utf-8")).hexdigest()[:16]
        return f"win_{digest}"

    @staticmethod
    def new_claim_episode() -> str:
        return f"episode_{uuid.uuid4().hex[:24]}"

    def open_repository(
        self,
        window_id: str,
        repository: RepositoryDescriptor | RepositoryState | str | Path,
        *,
        claim_episode: str | None = None,
    ) -> WindowRepositorySession:
        descriptor = self._descriptor(repository)
        current = self._sessions.get(window_id)
        if current and current.repository.repo_id == descriptor.repo_id and not current.stopped:
            return current
        if current:
            self._stopped_children.append(current.child_id)
            current.drain()
        episode = claim_episode or self.new_claim_episode()
        child_id = f"child_{window_id}_{descriptor.repo_id}_{episode}"
        session = WindowRepositorySession(
            window_id=window_id,
            repository=descriptor,
            claim_episode=episode,
            child_id=child_id,
        )
        self._sessions[window_id] = session
        return session

    def active_session(self, window_id: str) -> WindowRepositorySession | None:
        session = self._sessions.get(window_id)
        if session and not session.stopped:
            return session
        return None

    def route_identity(self, window_id: str, thread_id: str, task_id: str) -> CompositeRouteIdentity:
        session = self.active_session(window_id)
        if not session:
            raise RuntimeError("window_repository_not_selected")
        return session.identity(thread_id, task_id)

    def persist_callback(
        self,
        window_id: str,
        thread_id: str,
        task_id: str,
        callback_event_id: str,
        payload: dict[str, Any],
    ) -> RepoRouteKey:
        session = self.active_session(window_id)
        if not session:
            raise RuntimeError("window_repository_not_selected")
        route = session.enqueue_callback(thread_id, task_id, callback_event_id)
        self._durable_callbacks[route.canonical()] = DurableCallback(route=route, payload=dict(payload))
        return route

    def persist_coordinator_callback(
        self,
        window_id: str,
        *,
        provider: str,
        task_id: str,
        callback_event_id: str,
        payload: dict[str, Any],
        thread_id: str = "",
        session_id: str = "",
    ) -> CoordinatorRouteKey:
        session = self.active_session(window_id)
        if not session:
            raise RuntimeError("window_repository_not_selected")
        route = session.enqueue_coordinator_callback(
            provider=provider,
            thread_id=thread_id,
            session_id=session_id,
            task_id=task_id,
            callback_event_id=callback_event_id,
        )
        self._durable_callbacks[route.canonical()] = DurableCallback(route=route, payload=dict(payload))
        return route

    def close_window(self, window_id: str) -> None:
        session = self._sessions.pop(window_id, None)
        if session:
            self._stopped_children.append(session.child_id)
            session.drain()

    def deliver_callback(self, route: RepoRouteKey | CoordinatorRouteKey) -> DurableCallback | None:
        return self._durable_callbacks.get(route.canonical())

    def deliver_coordinator_callback(
        self,
        *,
        repo_id: str,
        provider: str,
        window_id: str,
        task_id: str,
        callback_event_id: str,
        thread_id: str = "",
        session_id: str = "",
    ) -> DurableCallback | None:
        route = CoordinatorRouteKey(
            repo_id=repo_id,
            provider=provider,
            window_id=window_id,
            thread_id=thread_id,
            session_id=session_id,
            task_id=task_id,
            event_id=callback_event_id,
        )
        return self.deliver_callback(route)

    def callbacks_for_repo_thread_episode(
        self,
        repo_id: str,
        thread_id: str,
        claim_episode: str,
    ) -> tuple[DurableCallback, ...]:
        return tuple(
            item
            for item in self._durable_callbacks.values()
            if item.route.repo_id == repo_id
            and item.route.thread_id == thread_id
            and item.payload.get("claim_episode") == claim_episode
        )

    @property
    def stopped_children(self) -> tuple[str, ...]:
        return tuple(self._stopped_children)

    @staticmethod
    def _descriptor(repository: RepositoryDescriptor | RepositoryState | str | Path) -> RepositoryDescriptor:
        if isinstance(repository, RepositoryDescriptor):
            return repository
        if isinstance(repository, RepositoryState):
            return RepositoryDescriptor.from_state(repository)
        return RepositoryDescriptor.inspect(repository)


__all__ = [
    "CompositeRouteIdentity",
    "CoordinatorRouteKey",
    "DurableCallback",
    "RepositoryDescriptor",
    "RepositoryWindowMux",
    "WindowRepositorySession",
]
