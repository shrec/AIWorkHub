"""NF222 collision suite for the injective versioned route identity.

Verifies that the v2 length-prefixed canonical encoding is injective under
adversarial ``:`` placement inside identifier fields, that ``RepoRouteKey``
and ``CoordinatorRouteKey`` round-trip exactly, that digests and
client-user-message-ids inherit that injectivity, and that legacy parsing is
explicit (only accepting unambiguous forms).
"""
from __future__ import annotations

import hashlib

import pytest

from aiworkhub.route_identity import CoordinatorRouteKey, RepoRouteKey

# Tuples of (repo_id, thread_id, task_id, event_id). Several pairs would
# collapse to the same string under the legacy ``:``-join canonical form
# (e.g. ("a:b","c","d","e") vs ("a","b:c","d","e") both become "a:b:c:d:e").
_REPO_COLON_TUPLES: list[tuple[str, str, str, str]] = [
    ("a:b", "c", "d", "e"),
    ("a", "b:c", "d", "e"),
    ("a", "b", "c:d", "e"),
    ("a", "b", "c", "d:e"),
    ("a:b:c", "d", "e", "f"),
    ("a", "b:c:d", "e", "f"),
    ("a:b", "c:d", "e", "f"),
    ("a", "b", "c", ""),
    ("a:b", "c", "d", ""),
]

# Tuples of (repo_id, provider, window_id, thread_id, session_id, task_id,
# event_id) exercising embedded colons across every field boundary.
_COORDINATOR_COLON_TUPLES: list[tuple[str, str, str, str, str, str, str]] = [
    ("a:b", "codex", "w1", "t1", "", "task1", "e1"),
    ("a", "claude", "w1", "", "s1", "task1", "e1"),
    ("a:b", "copilot", "w:x", "t:x", "", "t:k", "e"),
    ("a", "codex", "w1", "t:x", "", "t:k", ""),
]


def _repo_key(t: tuple[str, str, str, str]) -> RepoRouteKey:
    return RepoRouteKey(repo_id=t[0], thread_id=t[1], task_id=t[2], event_id=t[3])


def test_repo_colon_placement_yields_distinct_identity() -> None:
    keys = [_repo_key(t) for t in _REPO_COLON_TUPLES]
    canonicals = [k.canonical() for k in keys]
    digests = [k.digest() for k in keys]
    client_ids = [k.to_client_user_message_id("review_ready", "ep1") for k in keys]
    assert len(set(canonicals)) == len(canonicals)
    assert len(set(digests)) == len(digests)
    assert len(set(client_ids)) == len(client_ids)


def test_repo_naive_v1_join_would_have_collided() -> None:
    # The whole point of the v2 contract: the legacy colon-join is ambiguous.
    left = _repo_key(("a:b", "c", "d", "e"))
    right = _repo_key(("a", "b:c", "d", "e"))
    naive_left = f"{left.repo_id}:{left.thread_id}:{left.task_id}:{left.event_id}"
    naive_right = f"{right.repo_id}:{right.thread_id}:{right.task_id}:{right.event_id}"
    assert naive_left == naive_right
    assert left.canonical() != right.canonical()
    assert left.digest() != right.digest()


def test_repo_v2_round_trip_exact() -> None:
    for t in _REPO_COLON_TUPLES:
        key = _repo_key(t)
        assert RepoRouteKey.parse(key.canonical()) == key


def test_coordinator_v2_round_trip_exact() -> None:
    for repo, provider, window, thread, session, task, event in _COORDINATOR_COLON_TUPLES:
        key = CoordinatorRouteKey(
            repo_id=repo,
            provider=provider,
            window_id=window,
            thread_id=thread,
            session_id=session,
            task_id=task,
            event_id=event,
        )
        assert CoordinatorRouteKey.parse(key.canonical()) == key


def test_coordinator_colon_placement_yields_distinct_identity() -> None:
    keys = [
        CoordinatorRouteKey(
            repo_id=repo,
            provider=provider,
            window_id=window,
            thread_id=thread,
            session_id=session,
            task_id=task,
            event_id=event,
        )
        for repo, provider, window, thread, session, task, event in _COORDINATOR_COLON_TUPLES
    ]
    assert len({k.canonical() for k in keys}) == len(keys)
    assert len({k.digest() for k in keys}) == len(keys)


def test_digest_is_versioned() -> None:
    key = RepoRouteKey(repo_id="r", thread_id="t", task_id="k", event_id="e")
    naive_v1 = hashlib.sha256(b"r:t:k:e").hexdigest()[:32]
    assert key.digest() != naive_v1
    assert key.digest() == hashlib.sha256(key.canonical().encode("utf-8")).hexdigest()[:32]
    assert key.canonical().startswith("v2")


def test_empty_field_boundaries_do_not_collide() -> None:
    empty_event = RepoRouteKey(repo_id="a", thread_id="b", task_id="c", event_id="")
    colon_event = RepoRouteKey(repo_id="a", thread_id="b", task_id="c", event_id=":")
    assert empty_event != colon_event
    assert empty_event.canonical() != colon_event.canonical()
    assert empty_event.digest() != colon_event.digest()
    assert RepoRouteKey.parse(empty_event.canonical()) == empty_event
    assert RepoRouteKey.parse(colon_event.canonical()) == colon_event


# ---------------------------------------------------------------------------
# Legacy parsing: explicit, unambiguous-only
# ---------------------------------------------------------------------------

def test_legacy_parse_accepts_unambiguous_four_field() -> None:
    assert RepoRouteKey.parse("a:b:c:d") == RepoRouteKey(
        repo_id="a", thread_id="b", task_id="c", event_id="d"
    )


def test_legacy_parse_accepts_trailing_empty_event() -> None:
    assert RepoRouteKey.parse("a:b:c:") == RepoRouteKey(
        repo_id="a", thread_id="b", task_id="c", event_id=""
    )


def test_legacy_parse_rejects_ambiguous_extra_colons() -> None:
    # Five fields in the legacy format means a field contained a colon ->
    # ambiguous identity, must never be silently accepted.
    with pytest.raises(ValueError):
        RepoRouteKey.parse("a:b:c:d:e")


def test_legacy_parse_rejects_too_few_fields() -> None:
    with pytest.raises(ValueError):
        RepoRouteKey.parse("a:b:c")


def test_legacy_parse_rejects_empty_repo() -> None:
    with pytest.raises(ValueError):
        RepoRouteKey.parse(":b:c:d")


def test_coordinator_legacy_parse_rejects_ambiguous() -> None:
    # Eight fields -> an embedded colon; must fail closed.
    with pytest.raises(ValueError):
        CoordinatorRouteKey.parse("a:b:c:d:e:f:g:h")
