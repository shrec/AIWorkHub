"""Regression coverage for the bounded finalization probe caches.

Both ``_FINALIZATION_PROBE_CACHE`` and ``_FINALIZATION_PROBE_FAILURES`` are keyed
by ``_finalization_probe_key(repo, adapter_id)`` whose third component is the git
HEAD OID, so before the fix a long-lived MCP server gained one permanent entry
per commit.  ``_FINALIZATION_PROBE_CACHE`` consulted its 300s window only on read
(never evicting), and ``_FINALIZATION_PROBE_FAILURES`` was a plain dict whose only
removal was a targeted success ``.pop`` -- neither was a size bound.

The reproduction tests drive the real write sites with monkeypatched, process-free
dependencies and injected distinct keys, then assert each cache stayed bounded.
On the pre-fix code the caches grow to one entry per distinct key and the bound
assertions fail; after the fix every insert evicts oldest-first under the lock.

None of these tests call ``chmod``/``fchmod`` or provision a real worktree.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

from aiworkhub import worker_workspace as mod


# Read the enforced bounds from the module when present; on the pre-fix code the
# constants do not exist yet, so fall back to the intended default of 32 so the
# reproduction still asserts against a concrete bound.
_CACHE_MAX = getattr(mod, "_FINALIZATION_PROBE_CACHE_MAX_ENTRIES", 32)
_FAILURES_MAX = getattr(mod, "_FINALIZATION_PROBE_FAILURES_MAX_ENTRIES", 32)


def _clear_probe_state() -> None:
    mod._FINALIZATION_PROBE_CACHE.clear()
    mod._FINALIZATION_PROBE_FAILURES.clear()
    mod._FINALIZATION_PROBE_ACTIVE.clear()


class _FakeWorkspace:
    """Minimal stand-in exposing only the attributes the probe path reads."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.path = repo / "worktree"
        self.home = repo / "home"
        self.provisioning_timings_ms: dict[str, float] = {}


def _distinct_head_oids(monkeypatch) -> None:
    """Make every ``_finalization_probe_key`` call yield a fresh HEAD OID."""

    counter = {"n": 0}

    def fake_head_oid(_repo: Path) -> str:
        counter["n"] += 1
        return f"oid{counter['n']:08d}"

    monkeypatch.setattr(mod, "_repository_head_oid", fake_head_oid)


def _stub_success_probe_deps(monkeypatch) -> dict[str, int]:
    """Route the blocking probe through a zero-diff, process-free success path."""

    calls = {"create": 0}

    def fake_create_workspace(repo, request_id, card, adapter_id):  # noqa: ANN001
        calls["create"] += 1
        return _FakeWorkspace(Path(repo))

    def fake_enforce_scope(_workspace, **_kwargs):  # noqa: ANN001
        return []

    def fake_cleanup_workspace(*_args, **_kwargs):  # noqa: ANN001
        return None

    monkeypatch.setattr(mod, "create_workspace", fake_create_workspace)
    monkeypatch.setattr(mod, "enforce_scope", fake_enforce_scope)
    monkeypatch.setattr(mod, "cleanup_workspace", fake_cleanup_workspace)
    return calls


def test_reproduction_probe_cache_stays_bounded(monkeypatch) -> None:
    """The success cache must not grow one permanent entry per distinct commit."""

    _clear_probe_state()
    _distinct_head_oids(monkeypatch)
    _stub_success_probe_deps(monkeypatch)
    repo = Path("/tmp/aiworkhub-probe-cache-bound")

    inserted = _CACHE_MAX * 3 + 5
    for _ in range(inserted):
        result = mod.finalization_preflight_probe(repo, "adapter-a")
        assert result["ok"] is True
        assert result["cache_hit"] is False

    # Pre-fix: a plain dict retains all ``inserted`` distinct-OID keys.
    assert len(mod._FINALIZATION_PROBE_CACHE) <= _CACHE_MAX
    _clear_probe_state()


def test_reproduction_failures_cache_stays_bounded(monkeypatch) -> None:
    """The failure cache must be size-bounded, not merely success-``pop``-ed."""

    _clear_probe_state()
    _distinct_head_oids(monkeypatch)

    # The nonblocking path starts its coalesced probe thread while holding
    # ``_FINALIZATION_PROBE_LOCK`` (a non-reentrant Lock), so the thread body
    # cannot run inline.  Record every real thread and join it, which drives the
    # actual ``run_probe`` failure-cache write deterministically.
    started: list[Any] = []
    real_thread_cls = mod.threading.Thread

    class _RecordingThread(real_thread_cls):  # type: ignore[valid-type,misc]
        def start(self) -> None:
            started.append(self)
            super().start()

    monkeypatch.setattr(mod.threading, "Thread", _RecordingThread)

    def fake_probe(_repo, _adapter, *, cache_seconds=None):  # noqa: ANN001
        return {
            "ok": False,
            "status": "blocked",
            "reason": "preflight_finalization_forced_failure",
            "phase": "preflight_finalization",
            "cache_hit": False,
        }

    monkeypatch.setattr(mod, "finalization_preflight_probe", fake_probe)
    repo = Path("/tmp/aiworkhub-probe-failures-bound")

    inserted = _FAILURES_MAX * 3 + 5
    for _ in range(inserted):
        mod.finalization_preflight_probe_nonblocking(repo, "adapter-b")
    for thread in started:
        thread.join(timeout=10)

    # Pre-fix: every failed distinct-OID probe accumulates without a size bound.
    assert len(mod._FINALIZATION_PROBE_FAILURES) <= _FAILURES_MAX
    _clear_probe_state()


def test_stale_entry_purged_at_insert_not_retained() -> None:
    """An entry older than the freshness window is dropped at write time."""

    cache: OrderedDict[tuple[str, str, str], tuple[float, dict[str, Any]]] = (
        OrderedDict()
    )
    old_key = ("/repo", "adapter", "oid-old")
    fresh_key = ("/repo", "adapter", "oid-new")

    mod._bounded_probe_cache_insert(
        cache,
        old_key,
        {"ok": True},
        now=100.0,
        window_seconds=300.0,
        max_entries=32,
    )
    assert old_key in cache

    # Insert a different key well past the 300s window relative to ``old_key``.
    mod._bounded_probe_cache_insert(
        cache,
        fresh_key,
        {"ok": True},
        now=100.0 + 301.0,
        window_seconds=300.0,
        max_entries=32,
    )

    assert old_key not in cache, "stale entry must be purged, not merely ignored"
    assert fresh_key in cache


def test_helper_evicts_oldest_first_at_bound() -> None:
    """The size bound is enforced oldest-first on every insert."""

    cache: OrderedDict[tuple[str, str, str], tuple[float, dict[str, Any]]] = (
        OrderedDict()
    )
    for index in range(10):
        mod._bounded_probe_cache_insert(
            cache,
            ("/repo", "adapter", f"oid{index:03d}"),
            {"ok": True, "n": index},
            now=float(index),
            window_seconds=1_000_000.0,
            max_entries=4,
        )

    assert len(cache) == 4
    surviving = [key[2] for key in cache]
    assert surviving == ["oid006", "oid007", "oid008", "oid009"]


def test_fresh_entry_still_reports_cache_hit(monkeypatch) -> None:
    """A within-window repeat of the same key still reports ``cache_hit`` True."""

    _clear_probe_state()

    # A single fixed HEAD OID keeps both calls on the same cache key.
    monkeypatch.setattr(mod, "_repository_head_oid", lambda _repo: "oid-fixed")
    calls = _stub_success_probe_deps(monkeypatch)
    repo = Path("/tmp/aiworkhub-probe-cache-hit")

    first = mod.finalization_preflight_probe(repo, "adapter-c")
    assert first["ok"] is True
    assert first["cache_hit"] is False
    assert calls["create"] == 1

    second = mod.finalization_preflight_probe(repo, "adapter-c")
    assert second["ok"] is True
    assert second["cache_hit"] is True
    # A fresh hit is served from cache and never re-provisions a workspace.
    assert calls["create"] == 1
    _clear_probe_state()
