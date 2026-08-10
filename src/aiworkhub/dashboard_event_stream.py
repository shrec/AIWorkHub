'''Bounded, repository-scoped dashboard invalidation event stream.

Events are invalidation signals only; they never carry task truth. Consumers
use them only to decide when to re-fetch the canonical snapshot built by
aiworkhub.dashboard.
'''

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple

MIN_CAPACITY = 8
DEFAULT_CAPACITY = 64
_LOCAL_DEFAULT_REPO = '_local_default_'


@dataclass(frozen=True)
class DashboardEvent:
    seq: int
    repo: str
    kind: str
    payload: Mapping[str, Any]
    emitted_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'seq': self.seq,
            'repo': self.repo,
            'kind': self.kind,
            'payload': dict(self.payload or {}),
            'emitted_at': self.emitted_at,
        }


class DashboardEventStream:
    def __init__(self, repo: Optional[Any] = None, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity is None:
            capacity = DEFAULT_CAPACITY
        try:
            parsed = int(capacity)
        except (TypeError, ValueError):
            parsed = DEFAULT_CAPACITY
        self._capacity = max(MIN_CAPACITY, parsed)
        self._repo = str(Path(repo).resolve()) if repo is not None else _LOCAL_DEFAULT_REPO
        self._events: Deque[DashboardEvent] = deque(maxlen=self._capacity)
        self._seq = 0
        self._resync_required = False
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def repo(self) -> str:
        return self._repo

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def head_seq(self) -> int:
        return self._seq

    @property
    def first_seq(self) -> int:
        if not self._events:
            return self._seq
        return self._events[0].seq

    @property
    def resync_required(self) -> bool:
        return self._resync_required

    @property
    def events(self) -> Tuple[DashboardEvent, ...]:
        return tuple(self._events)

    def emit(
        self,
        kind: str,
        payload: Optional[Mapping[str, Any]] = None,
        repo: Optional[str] = None,
    ) -> DashboardEvent:
        with self._lock:
            self._seq += 1
            event_repo = self._repo if repo is None else str(Path(repo).resolve())
            event = DashboardEvent(
                seq=self._seq,
                repo=event_repo,
                kind=kind,
                payload=dict(payload or {}),
            )
            dropped = len(self._events) == self._capacity
            self._events.append(event)
            if dropped:
                self._resync_required = True
            return event

    append = emit

    def drain(
        self,
        after: Optional[int] = None,
        repo: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            after_seq = 0 if after is None else after
            if after_seq > self._seq:
                self._resync_required = True
                return []
            if after_seq < self.first_seq - 1:
                self._resync_required = True
            result = [
                event.to_dict()
                for event in self._events
                if event.seq > after_seq
                and (repo is None or event.repo == str(Path(repo).resolve()))
            ]
            if limit is not None and limit >= 0:
                result = result[:limit]
            return result

    def read(self, after: Optional[int] = None, repo: Optional[str] = None) -> Dict[str, Any]:
        return {
            'repo': self._repo,
            'events': self.drain(after=after, repo=repo),
            'last_seq': self.last_seq,
            'head_seq': self.head_seq,
            'first_seq': self.first_seq,
            'resync_required': self.resync_required,
            'capacity': self.capacity,
        }

    def clear_resync(self) -> None:
        with self._lock:
            self._resync_required = False

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._seq = 0
            self._resync_required = False


class DashboardEventRegistry:
    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = max(MIN_CAPACITY, int(capacity))
        self._streams: Dict[str, DashboardEventStream] = {}
        self._lock = threading.RLock()

    def stream(self, repo: Optional[Any] = None) -> DashboardEventStream:
        key = _LOCAL_DEFAULT_REPO if repo is None else str(Path(repo).resolve())
        with self._lock:
            stream = self._streams.get(key)
            if stream is None:
                stream = DashboardEventStream(repo=key, capacity=self._capacity)
                self._streams[key] = stream
            return stream

    def emit(
        self,
        repo: Optional[Any],
        kind: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> DashboardEvent:
        return self.stream(repo).emit(kind, payload, repo=repo)

    def drain(
        self,
        repo: Optional[Any],
        after: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return self.stream(repo).drain(after=after, repo=repo, limit=limit)

    def read(self, repo: Optional[Any]) -> Dict[str, Any]:
        return self.stream(repo).read(repo=repo)

    def clear_all(self) -> None:
        with self._lock:
            self._streams.clear()


_default_registry = DashboardEventRegistry()


def get_registry() -> DashboardEventRegistry:
    return _default_registry


def emit_dashboard_event(
    repo: Optional[Any],
    kind: str,
    payload: Optional[Mapping[str, Any]] = None,
) -> DashboardEvent:
    return _default_registry.emit(repo, kind, payload)


def drain_dashboard_events(
    repo: Optional[Any],
    after: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    return _default_registry.drain(repo, after=after, limit=limit)
