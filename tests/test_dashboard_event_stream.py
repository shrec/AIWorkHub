from __future__ import annotations

from pathlib import Path

from aiworkhub.dashboard_event_stream import (
    DashboardEventRegistry,
    DashboardEventStream,
    drain_dashboard_events,
    emit_dashboard_event,
    get_registry,
)


def test_capacity_floor_is_eight() -> None:
    assert DashboardEventStream(capacity=0).capacity == 8
    assert DashboardEventStream(capacity=4).capacity == 8
    assert DashboardEventStream(capacity=8).capacity == 8
    assert DashboardEventStream(capacity=64).capacity == 64


def test_overflow_sets_sticky_resync_and_clear_resync_clears() -> None:
    stream = DashboardEventStream(capacity=8)
    for index in range(9):
        stream.emit('task_updated', {'index': index})
    assert stream.resync_required is True
    assert len(stream.events) == 8
    stream.clear_resync()
    assert stream.resync_required is False


def test_default_streams_do_not_mix_instances() -> None:
    first = DashboardEventStream()
    second = DashboardEventStream()
    first.emit('task_updated', {'id': 1})
    assert second.drain() == []
    assert [event['payload']['id'] for event in first.drain()] == [1]


def test_drain_after_head_is_empty() -> None:
    stream = DashboardEventStream()
    stream.emit('task_updated', {'id': 1})
    stream.emit('task_updated', {'id': 2})
    assert stream.drain(after=stream.last_seq) == []


def test_gap_overflow_marks_resync() -> None:
    stream = DashboardEventStream(capacity=8)
    for index in range(9):
        stream.emit('task_updated', {'index': index})
    stream.clear_resync()
    assert stream.resync_required is False
    stream.drain(after=0)
    assert stream.resync_required is True


def test_repository_scope_follows_delivered_repo() -> None:
    registry = DashboardEventRegistry()
    registry.emit(Path('repo-a'), 'task_updated', {'id': 1})
    registry.emit(Path('repo-b'), 'task_updated', {'id': 2})
    repo_a = Path('repo-a').resolve()
    events = registry.drain(repo_a)
    assert [event['repo'] for event in events] == [str(repo_a)]
    assert [event['payload']['id'] for event in events] == [1]
    repo_b_events = registry.drain(Path('repo-b'), after=0)
    assert [event['payload']['id'] for event in repo_b_events] == [2]


def test_global_emit_and_drain_helpers_are_repository_scoped() -> None:
    get_registry().clear_all()
    repo = Path.cwd().resolve()
    emit_dashboard_event(repo, 'task_updated', {'id': 7})
    events = drain_dashboard_events(repo, after=0)
    assert events[-1]['kind'] == 'task_updated'
    assert events[-1]['repo'] == str(repo)
    get_registry().clear_all()
