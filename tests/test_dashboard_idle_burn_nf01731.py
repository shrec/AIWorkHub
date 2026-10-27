"""NF-2026-01731: an idle dashboard tick must not rebuild or re-send the snapshot.

(c) ``snapshot_view`` memoizes the build on a cheap generation key: an
unchanged key does no ``build_snapshot`` work, a changed key or a key read
error rebuilds. (b) a full-mode caller that passes its per-section digests
receives only the changed sections; any digest miss returns the full snapshot.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, dashboard, dashboard_mcp_app  # noqa: E402

# Pinned in vscode-extension/test/dashboard-delta-channel.test.js as well: the
# extension's canonicalSnapshotJson/snapshotSectionDigest must agree byte-for-byte.
_CROSS_LANGUAGE_SAMPLE: dict[str, Any] = {
    "b": [1, 2.5, "é ✓", None, True],
    "a": {"z": 1, "y": 'x\n"q"'},
    "n": 3.0,
}
_CROSS_LANGUAGE_DIGEST = "ad9978065bfeb578a6a0c5ec2b201e594d04cc018d28635ea5ba304b9feddf32"


def _built_snapshot() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": "2026-10-27T10:00:00+00:00",
        "readonly": True,
        "storage": {"ready": True, "reason": "ready", "repo_id": "repo_nf01731"},
        "storage_usage": {"scan_status": "ready", "repo_data_bytes": 11},
        "health": {"ok": True, "degraded": False, "provider_error_count": 0},
        "status_counts": {"pending": 1, "processing": 0, "review": 0, "active": 1},
        "row_counts": {"pending": {"returned": 1, "exact": 1, "truncated": False}},
        "tasks": {"pending": [{"task_id": "T-NF01731", "topic": "dashboard"}], "processing": []},
        "agent_processes": {"processes": [{"request_id": f"r{index}"} for index in range(50)]},
        "kpi_analytics": {"acceptance_rate": 1.0, "ratio": 0.25},
        "errors": [],
    }


class _CountingBuilder:
    def __init__(self, factory=_built_snapshot) -> None:
        self.calls: list[dict[str, Any]] = []
        self._factory = factory

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._factory()


@pytest.fixture(autouse=True)
def hermetic_sources(monkeypatch):
    """A fresh build cache and fixed non-build sources for every case."""
    monkeypatch.setattr(dashboard_mcp_app, "_SNAPSHOT_BUILD_CACHE", {})
    monkeypatch.setattr(
        dashboard_mcp_app, "_SNAPSHOT_BUILD_CACHE_STATS", {"hit": 0, "miss": 0, "key_error": 0}
    )
    state = {"manager": {"ok": True, "role": "manager", "provider": "codex", "manager_route": {}}}
    monkeypatch.setattr(core, "manager_bootstrap", lambda: copy.deepcopy(state["manager"]))
    monkeypatch.setattr(core, "dispatcher_health", lambda: {"ok": True, "status": "running"})
    monkeypatch.setattr(core, "repo_root", lambda: Path("/"))
    monkeypatch.setattr(core, "read_selected_coordinator_target", lambda _root: {})
    monkeypatch.setattr(
        dashboard_mcp_app.shared_router, "list_known_repositories", lambda **_kwargs: {}
    )
    monkeypatch.setattr(dashboard_mcp_app.storage_observability, "snapshot", lambda _root: {})
    return state


def _fixed_key(monkeypatch, holder: dict[str, Any]) -> None:
    def key(_root):
        if isinstance(holder["key"], BaseException):
            raise holder["key"]
        return holder["key"]

    monkeypatch.setattr(dashboard_mcp_app, "_snapshot_generation_key", key)


# ------------------------------------------------------------ (c) memoize


def test_unchanged_generation_key_does_no_build_work(monkeypatch):
    builder = _CountingBuilder()
    monkeypatch.setattr(dashboard, "build_snapshot", builder)
    _fixed_key(monkeypatch, {"key": ("generation", 1)})

    first = dashboard_mcp_app.snapshot_view(full=True)
    second = dashboard_mcp_app.snapshot_view(full=True)

    assert builder.calls == [{"summary_only": False}]
    assert second == first
    assert dashboard_mcp_app._snapshot_build_cache_stats() == {"hit": 1, "miss": 1, "key_error": 0}


def test_each_mode_is_memoized_separately(monkeypatch):
    builder = _CountingBuilder()
    monkeypatch.setattr(dashboard, "build_snapshot", builder)
    _fixed_key(monkeypatch, {"key": ("generation", 1)})

    for _ in range(2):
        dashboard_mcp_app.snapshot_view(full=True)
        dashboard_mcp_app.snapshot_view()

    assert builder.calls == [{"summary_only": False}, {"summary_only": True}]
    assert len(dashboard_mcp_app._SNAPSHOT_BUILD_CACHE) == 2  # noqa: SLF001


def test_changed_generation_key_rebuilds(monkeypatch):
    builder = _CountingBuilder()
    monkeypatch.setattr(dashboard, "build_snapshot", builder)
    holder = {"key": ("generation", 1)}
    _fixed_key(monkeypatch, holder)

    dashboard_mcp_app.snapshot_view(full=True)
    holder["key"] = ("generation", 2)
    dashboard_mcp_app.snapshot_view(full=True)
    dashboard_mcp_app.snapshot_view(full=True)

    assert len(builder.calls) == 2


@pytest.mark.parametrize("error", [OSError("stat failed"), PermissionError("denied")])
def test_key_read_error_rebuilds_every_time(monkeypatch, error):
    builder = _CountingBuilder()
    monkeypatch.setattr(dashboard, "build_snapshot", builder)
    holder: dict[str, Any] = {"key": ("generation", 1)}
    _fixed_key(monkeypatch, holder)
    dashboard_mcp_app.snapshot_view(full=True)

    holder["key"] = error
    dashboard_mcp_app.snapshot_view(full=True)
    dashboard_mcp_app.snapshot_view(full=True)

    assert len(builder.calls) == 3
    assert dashboard_mcp_app._snapshot_build_cache_stats()["key_error"] == 2
    # The unprovable entry is dropped, not kept for a later key match.
    holder["key"] = ("generation", 1)
    dashboard_mcp_app.snapshot_view(full=True)
    assert len(builder.calls) == 4


def test_degraded_build_is_rechecked_not_cached(monkeypatch):
    def degraded() -> dict[str, Any]:
        snapshot = _built_snapshot()
        snapshot["errors"] = [{"source": "tasks.pending", "kind": "OperationalError", "message": "locked"}]
        return snapshot

    builder = _CountingBuilder(degraded)
    monkeypatch.setattr(dashboard, "build_snapshot", builder)
    _fixed_key(monkeypatch, {"key": ("generation", 1)})

    dashboard_mcp_app.snapshot_view(full=True)
    dashboard_mcp_app.snapshot_view(full=True)

    assert len(builder.calls) == 2


def test_ttl_backstop_and_a_replaced_builder_rebuild(monkeypatch):
    builder = _CountingBuilder()
    monkeypatch.setattr(dashboard, "build_snapshot", builder)
    _fixed_key(monkeypatch, {"key": ("generation", 1)})

    dashboard_mcp_app.snapshot_view(full=True)
    monkeypatch.setattr(dashboard_mcp_app, "SNAPSHOT_BUILD_CACHE_TTL_SECONDS", 0.0)
    dashboard_mcp_app.snapshot_view(full=True)
    assert len(builder.calls) == 2

    monkeypatch.setattr(dashboard_mcp_app, "SNAPSHOT_BUILD_CACHE_TTL_SECONDS", 60.0)
    other = _CountingBuilder()
    monkeypatch.setattr(dashboard, "build_snapshot", other)
    dashboard_mcp_app.snapshot_view(full=True)
    assert len(other.calls) == 1


def test_a_genuine_previous_snapshot_is_never_served_from_cache(monkeypatch):
    builder = _CountingBuilder()
    monkeypatch.setattr(dashboard, "build_snapshot", builder)
    _fixed_key(monkeypatch, {"key": ("generation", 1)})
    prior = {"skills": {"state": "ready"}}

    dashboard_mcp_app.snapshot_view(full=True, previous=prior)
    dashboard_mcp_app.snapshot_view(full=True, previous_snapshot=prior)

    assert builder.calls == [
        {"summary_only": False, "previous": prior},
        {"summary_only": False, "previous": prior},
    ]


def test_late_storage_refresh_never_mutates_the_memoized_build(monkeypatch):
    builder = _CountingBuilder()
    monkeypatch.setattr(dashboard, "build_snapshot", builder)
    _fixed_key(monkeypatch, {"key": ("generation", 1)})
    monkeypatch.setattr(
        dashboard_mcp_app.storage_observability,
        "snapshot",
        lambda _root: {"scan_status": "scanning", "repo_data_bytes": 99},
    )

    result = dashboard_mcp_app.snapshot_view(full=True)

    assert result["storage_usage"] == {"scan_status": "scanning", "repo_data_bytes": 99}
    cached = dashboard_mcp_app._SNAPSHOT_BUILD_CACHE[False][3]  # noqa: SLF001
    assert cached["storage_usage"] == {"scan_status": "ready", "repo_data_bytes": 11}


def _make_store(root: Path) -> Path:
    db = root.joinpath(*dashboard_mcp_app._TASK_QUEUE_DB_REL)  # noqa: SLF001
    db.parent.mkdir(parents=True)
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 84)
    return db


def test_generation_key_tracks_queue_commits_process_logs_and_stores(tmp_path):
    key = dashboard_mcp_app._snapshot_generation_key  # noqa: SLF001
    assert key(tmp_path) is None  # no queue: never cached

    db = _make_store(tmp_path)
    baseline = key(tmp_path)
    assert baseline is not None
    assert key(tmp_path) == baseline

    # A WAL commit rewrites the wal-index header in place at the same size.
    shm = Path(f"{db}-shm")
    shm.write_bytes(b"\x01" * 48)
    after_first_commit = key(tmp_path)
    assert after_first_commit != baseline
    shm.write_bytes(b"\x02" * 48)
    assert key(tmp_path) != after_first_commit

    with_shm = key(tmp_path)
    processes = tmp_path / ".aiworkhub" / "runtime" / "process_logs" / "processes"
    processes.mkdir(parents=True)
    assert key(tmp_path) != with_shm

    with_logs = key(tmp_path)
    needfix = tmp_path.joinpath(*dashboard_mcp_app.needfix_store.NEEDFIX_DB_REL)
    needfix.parent.mkdir(parents=True, exist_ok=True)
    needfix.write_bytes(b"needfix")
    assert key(tmp_path) != with_logs


def test_snapshot_view_rebuilds_after_a_real_store_change(monkeypatch, tmp_path):
    builder = _CountingBuilder()
    monkeypatch.setattr(dashboard, "build_snapshot", builder)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    db = _make_store(tmp_path)

    dashboard_mcp_app.snapshot_view(full=True)
    dashboard_mcp_app.snapshot_view(full=True)
    assert len(builder.calls) == 1

    Path(f"{db}-shm").write_bytes(b"\x07" * 48)
    dashboard_mcp_app.snapshot_view(full=True)
    assert len(builder.calls) == 2


def test_uninitialized_repository_is_never_cached(monkeypatch, tmp_path):
    builder = _CountingBuilder()
    monkeypatch.setattr(dashboard, "build_snapshot", builder)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)

    dashboard_mcp_app.snapshot_view(full=True)
    dashboard_mcp_app.snapshot_view(full=True)

    assert len(builder.calls) == 2


# ------------------------------------------------------------ (b) delta


_section_digest = dashboard_mcp_app._snapshot_section_digest  # noqa: SLF001


def _digests(result: dict[str, Any]) -> dict[str, str]:
    return {key: _section_digest(value) for key, value in result.items()}


def _merge(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    changed = delta["changed_sections"]
    return {key: changed[key] if key in changed else base[key] for key in delta["section_digests"]}


_REALISTIC_ROWS = 200


def _realistic_row(index: int, status: str) -> dict[str, Any]:
    return {
        "request_id": f"{index:032x}",
        "task_id": f"needfix-NF-2026-{index:05d}",
        "title": f"Dashboard follow-up {index}: bound the snapshot read path",
        "status": status,
        "runner": "claude_opus-5",
        "topic": "dashboard",
        "created_at": "2026-10-27T09:00:00+00:00",
        "updated_at": "2026-10-27T09:59:59+00:00",
        "summary": ("Measured idle tick rebuilt and re-sent the full snapshot; " * 4)[:200],
    }


def _realistic_snapshot() -> dict[str, Any]:
    """Production-shaped rows: the full snapshot is tens of KB, as on the canonical repo."""
    snapshot = _built_snapshot()
    snapshot["tasks"] = {
        "pending": [_realistic_row(index, "pending") for index in range(_REALISTIC_ROWS)],
        "processing": [],
    }
    snapshot["agent_processes"] = {
        "processes": [_realistic_row(index, "exited") for index in range(_REALISTIC_ROWS)]
    }
    return snapshot


def test_previous_digests_return_only_the_changed_sections(monkeypatch, hermetic_sources):
    monkeypatch.setattr(dashboard, "build_snapshot", _CountingBuilder(_realistic_snapshot))
    _fixed_key(monkeypatch, {"key": ("generation", 1)})
    full = dashboard_mcp_app.snapshot_view(full=True)
    full_bytes = dashboard_mcp_app._byte_len(full)  # noqa: SLF001
    assert full_bytes > 20_000

    idle = dashboard_mcp_app.snapshot_view(full=True, previous={"section_digests": _digests(full)})
    assert idle["snapshot_mode"] == "delta"
    assert idle["delta_schema"] == dashboard_mcp_app.SNAPSHOT_DELTA_SCHEMA
    assert idle["changed_sections"] == {}
    assert list(idle["section_digests"]) == list(full)
    idle_bytes = dashboard_mcp_app._byte_len(idle)  # noqa: SLF001
    # An idle reply is only the digest map plus the wrapper keys.
    assert idle_bytes <= len(idle["section_digests"]) * 100 + 256
    assert idle_bytes < full_bytes * 0.5

    hermetic_sources["manager"] = {**hermetic_sources["manager"], "role": "worker"}
    changed = dashboard_mcp_app.snapshot_view(full=True, previous={"section_digests": _digests(full)})
    assert set(changed["changed_sections"]) == {"manager_identity"}
    assert changed["changed_sections"]["manager_identity"]["role"] == "worker"
    fresh = dashboard_mcp_app.snapshot_view(full=True)
    assert _merge(full, changed) == fresh
    assert changed["section_digests"] == _digests(fresh)


@pytest.mark.parametrize(
    "digests",
    [
        {"tasks": "0" * 64, "status_counts": "f" * 64},  # matches no section
        {"tasks": "not-a-digest"},
        {"tasks": 7},
        "not-a-map",
        {f"s{index}": "0" * 64 for index in range(dashboard_mcp_app.MAX_SNAPSHOT_SECTION_DIGESTS + 1)},
    ],
    ids=["no-match", "not-hex", "not-str", "not-a-map", "oversized"],
)
def test_a_digest_miss_returns_the_full_snapshot(monkeypatch, digests):
    monkeypatch.setattr(dashboard, "build_snapshot", _CountingBuilder())
    _fixed_key(monkeypatch, {"key": ("generation", 1)})
    plain = dashboard_mcp_app.snapshot_view(full=True)

    result = dashboard_mcp_app.snapshot_view(full=True, previous={"section_digests": digests})

    assert result == plain
    assert result["snapshot_mode"] == "full"
    assert "section_digests" not in result


def test_a_plain_full_request_is_unchanged_and_compact_mode_ignores_digests(monkeypatch):
    monkeypatch.setattr(dashboard, "build_snapshot", _CountingBuilder())
    _fixed_key(monkeypatch, {"key": ("generation", 1)})
    full = dashboard_mcp_app.snapshot_view(full=True)
    assert "section_digests" not in full
    assert "changed_sections" not in full

    summary = dashboard_mcp_app.snapshot_view(previous={"section_digests": _digests(full)})
    assert summary["snapshot_mode"] == "summary"


def test_section_digest_matches_the_extension_canonical_form():
    assert _section_digest(_CROSS_LANGUAGE_SAMPLE) == _CROSS_LANGUAGE_DIGEST
    # An integral float digests like the int a JSON consumer re-reads.
    assert _section_digest({"n": 3.0}) == _section_digest({"n": 3})
    assert _section_digest({"n": 3.5}) != _section_digest({"n": 3})
