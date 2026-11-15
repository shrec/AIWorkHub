"""NF-2026-02274: a full dashboard snapshot never blocks on probes or ledger copies.

(a) ``snapshot_view(full=True)`` serves the ``opencode models`` probe from its
background-refreshed cache: a never-probed provider reads ``probing`` and the
subprocess never runs on the snapshot thread.  (b) The dirty-seed git scan
(``dirty_seeded_paths`` / ``hide_live_gitignore_seeds``) is reached only from
the finalization probe's provisioning, which runs on that probe's own
background thread.  (c) The memoized cost ledger handed to the dashboard is
not deep-copied per call, and mutating it cannot corrupt the memo.
"""

from __future__ import annotations

import copy
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import (
    core,
    cost_ledger,
    dashboard,
    dashboard_mcp_app,
    dirty_canonical_seed,
    repo_policy,
    runtime_adapters,
    task_store,
    worker_workspace,
    worker_workspace_ignore_seed,
)

_ROWS = ["opencode/a-free", "opencode-go/b"]
_BLOCK_SECONDS = 30.0


def _wait(predicate, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def _ready_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    readiness = task_store.storage_readiness(repo)
    assert readiness.ready, readiness.reason
    task_store.reset_storage_readiness_cache()
    return repo


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    xdg = tmp_path / "xdg_empty"
    xdg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    repo_policy.reset_provider_probe_caches()
    monkeypatch.setattr(dashboard_mcp_app, "_SNAPSHOT_BUILD_CACHE", {})
    monkeypatch.setattr(
        dashboard_mcp_app,
        "_SNAPSHOT_BUILD_CACHE_STATS",
        {"hit": 0, "miss": 0, "key_error": 0},
    )
    yield
    repo_policy.reset_provider_probe_caches()


# ------------------------------------------------- (a) non-blocking probe cache


def test_nonblocking_get_never_computes_on_the_caller_and_is_single_flight() -> None:
    cache = repo_policy._TtlSingleFlightCache("nf02274")
    release = threading.Event()
    threads: list[int] = []

    def compute() -> dict[str, int]:
        threads.append(threading.get_ident())
        release.wait(_BLOCK_SECONDS)
        return {"value": 1}

    started = time.perf_counter()
    first = cache.get("k", compute, blocking=False)
    second = cache.get("k", compute, blocking=False)
    assert time.perf_counter() - started < 2.0
    assert first is repo_policy.PROBE_PENDING
    assert second is repo_policy.PROBE_PENDING, "in-flight probe: still pending"
    _wait(lambda: len(threads) == 1)
    assert cache.stats()["refreshes_started"] == 1, "one background probe only"
    release.set()
    _wait(lambda: cache.stats()["refreshing"] == 0)
    assert threads == [threads[0]] and threads[0] != threading.get_ident()
    assert cache.get("k", compute, blocking=False) == {"value": 1}
    assert len(threads) == 1, "the landed value is served from the cache"


def test_blocking_get_is_unchanged_for_other_callers() -> None:
    cache = repo_policy._TtlSingleFlightCache("nf02274")
    assert cache.get("k", lambda: {"v": 1}) == {"v": 1}


def test_rejected_background_probe_is_a_negative_entry_not_pending_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = repo_policy._TtlSingleFlightCache("nf02274")
    launches: list[int] = []
    failure = {"state": "failed", "reason": "exit_3"}

    def failing_probe() -> dict[str, str]:
        launches.append(threading.get_ident())
        return dict(failure)

    def reject(_value: Any) -> bool:
        return False

    assert cache.get("k", failing_probe, reject, blocking=False) is repo_policy.PROBE_PENDING
    _wait(lambda: cache.stats()["refreshing"] == 0)
    second = cache.get("k", failing_probe, reject, blocking=False)
    third = cache.get("k", failing_probe, reject, blocking=False)
    assert second == third == failure, "the real failure, never pending"
    assert len(launches) == 1, "no relaunch while the negative entry is fresh"
    assert cache.stats()["refreshes_started"] == 1
    assert cache.stats()["negative_entries"] == 1

    # Once the negative TTL expires exactly one new probe is launched, and a
    # get while it is in flight never starts a second one.
    release = threading.Event()

    def slow_failing_probe() -> dict[str, str]:
        launches.append(threading.get_ident())
        release.wait(_BLOCK_SECONDS)
        return dict(failure)

    monkeypatch.setattr(repo_policy, "PROVIDER_PROBE_NEGATIVE_TTL_SECONDS", 0.0)
    try:
        for _ in range(3):
            assert (
                cache.get("k", slow_failing_probe, reject, blocking=False)
                is repo_policy.PROBE_PENDING
            )
        _wait(lambda: len(launches) == 2)
        assert cache.stats()["refreshes_started"] == 2
    finally:
        release.set()
    _wait(lambda: cache.stats()["refreshing"] == 0)
    assert len(launches) == 2, "at most one in-flight probe per key"


def test_raising_background_probe_is_re_raised_without_relaunch() -> None:
    cache = repo_policy._TtlSingleFlightCache("nf02274")
    launches: list[int] = []

    def raising_probe() -> Any:
        launches.append(1)
        raise RuntimeError("nf02274_probe_failed")

    assert cache.get("k", raising_probe, blocking=False) is repo_policy.PROBE_PENDING
    _wait(lambda: cache.stats()["refreshing"] == 0)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="nf02274_probe_failed"):
            cache.get("k", raising_probe, blocking=False)
    assert launches == [1]


def _traceback_depth(exc: BaseException) -> int:
    depth = 0
    tb = exc.__traceback__
    while tb is not None:
        depth += 1
        tb = tb.tb_next
    return depth


def test_negative_cache_raises_a_fresh_sanitized_exception_per_get() -> None:
    cache = repo_policy._TtlSingleFlightCache("nf02274")

    def raising_probe() -> Any:
        raise subprocess.CalledProcessError(3, ["opencode", "models"], "out", "err")

    assert cache.get("k", raising_probe, blocking=False) is repo_policy.PROBE_PENDING
    _wait(lambda: cache.stats()["refreshing"] == 0)
    caught: list[BaseException] = []
    for _ in range(3):
        with pytest.raises(subprocess.CalledProcessError) as info:
            cache.get("k", raising_probe, blocking=False)
        caught.append(info.value)
    e1, e2, e3 = caught
    assert e1 is not e2 and e2 is not e3, "a fresh instance per raise"
    assert _traceback_depth(e2) <= _traceback_depth(e1)
    assert _traceback_depth(e3) <= _traceback_depth(e1), "traceback never grows"
    assert (e1.returncode, e1.cmd, e1.output, e1.stderr) == (
        3,
        ["opencode", "models"],
        "out",
        "err",
    )
    assert str(e1) == str(e2), "same type and message as the probe's exception"
    assert e1.__cause__ is None and e1.__context__ is None
    stored = cache._rejected["k"][2]
    assert stored is not None and stored.__traceback__ is None, (
        "the negative entry keeps no probe frames alive"
    )
    assert stored is not e1 and stored is not e2


def test_failing_opencode_probe_launches_once_and_then_reports_the_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = str(tmp_path / "opencode")
    monkeypatch.setattr(repo_policy.opencode_auth, "owner_custom_provider_models", lambda: [])
    runs: list[int] = []

    def failing_run(argv, **kwargs):  # noqa: ARG001
        runs.append(threading.get_ident())
        return SimpleNamespace(returncode=3, stdout=b"", stderr=b"")

    monkeypatch.setattr(repo_policy.subprocess, "run", failing_run)
    with repo_policy.nonblocking_provider_probes():
        first = repo_policy._cached_opencode_models(exe)
        _wait(lambda: repo_policy.provider_probe_cache_stats()[0]["refreshing"] == 0)
        launched = len(runs)
        second = repo_policy._cached_opencode_models(exe)
        third = repo_policy._cached_opencode_models(exe)
    assert first == ([], {"state": "probing"})
    assert second == third
    assert second[1] is not None and second[1]["state"] == "failed", second
    assert launched >= 1 and len(runs) == launched, "one background probe, no relaunch"
    assert threading.get_ident() not in runs
    assert repo_policy.provider_probe_cache_stats()[0]["refreshes_started"] == 1


def _preflight_with_opencode_row(
    monkeypatch: pytest.MonkeyPatch, repo: Path, probe: dict[str, Any] | None
) -> dict[str, Any]:
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: False)
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id, *_a, **_k: runtime_adapters.ExecutableResolution(
            adapter_id, None, False, "not_found"
        ),
    )
    original_status = repo_policy._provider_status

    def status(*args: Any, **kwargs: Any) -> dict[str, Any]:
        row = original_status(*args, **kwargs)
        is_opencode = row["adapter_id"] == runtime_adapters.OPENCODE_CLI_ADAPTER
        row["coverage_required"] = is_opencode
        if is_opencode:
            row["launchable"] = False
            if probe is not None:
                row["opencode_models_probe"] = dict(probe)
        return row

    monkeypatch.setattr(repo_policy, "_provider_status", status)
    return repo_policy.build_preflight(repo, runtime_adapters.OPENCODE_CLI_ADAPTER)


def test_a_probing_provider_contributes_probing_never_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _ready_repo(tmp_path)
    provider_errors = {"no_launchable_provider_routes", "selected_adapter_not_launchable"}

    measured = _preflight_with_opencode_row(monkeypatch, repo, {"state": "failed"})
    assert provider_errors <= set(measured["errors"]), "control: a measured miss blocks"

    probing = _preflight_with_opencode_row(monkeypatch, repo, {"state": "probing"})
    assert not provider_errors & set(probing["errors"]), probing["errors"]
    assert "provider_models_probe_pending" in probing["warnings"]
    assert probing["provider_summary"]["coverage_status"] == "probing"
    assert probing["ok"] is False
    if not probing["errors"]:
        assert probing["status"] == "probing"


def test_finalization_probe_pending_is_never_memoized() -> None:
    snapshot = {
        "environment_preflight": {
            "status": "blocked",
            "errors": ["source_graph_not_ready"],
            "warnings": ["worker_finalization_probe_pending"],
            "worker_finalization": {"ok": False, "status": "probing"},
            "providers": [],
        }
    }
    assert dashboard_mcp_app._probe_pending(snapshot)
    assert not dashboard_mcp_app._cacheable_build(snapshot)
    settled = copy.deepcopy(snapshot)
    settled["environment_preflight"]["warnings"] = []
    settled["environment_preflight"]["worker_finalization"] = {"ok": True, "status": "ready"}
    assert not dashboard_mcp_app._probe_pending(settled)


# ------------------------------------------------- (a)+(b) full snapshot_view


def _hermetic_snapshot_sources(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    monkeypatch.setattr(
        core,
        "manager_bootstrap",
        lambda: {"ok": True, "role": "manager", "provider": "codex", "manager_route": {}},
    )
    monkeypatch.setattr(core, "dispatcher_health", lambda: {"ok": True, "status": "running"})
    monkeypatch.setattr(core, "read_selected_coordinator_target", lambda _root: {})
    monkeypatch.setattr(
        dashboard_mcp_app.shared_router, "list_known_repositories", lambda **_kwargs: {}
    )
    monkeypatch.setattr(dashboard_mcp_app.storage_observability, "snapshot", lambda _root: {})


def _opencode_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    providers = snapshot["environment_preflight"]["providers"]
    return next(
        row for row in providers
        if row["adapter_id"] == runtime_adapters.OPENCODE_CLI_ADAPTER
    )


def test_full_snapshot_never_runs_probes_or_dirty_seed_scan_on_its_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _ready_repo(tmp_path)
    _hermetic_snapshot_sources(monkeypatch, repo)
    exe = tmp_path / "bin" / "opencode"
    exe.parent.mkdir()
    exe.write_bytes(b"")

    def fake_resolve(adapter_id: str, *_args: Any, **_kwargs: Any):
        if adapter_id == runtime_adapters.OPENCODE_CLI_ADAPTER:
            return runtime_adapters.ExecutableResolution(adapter_id, str(exe), True, "")
        return runtime_adapters.ExecutableResolution(adapter_id, None, False, "not_found")

    monkeypatch.setattr(repo_policy.runtime_adapters, "resolve_executable", fake_resolve)
    monkeypatch.setattr(repo_policy.opencode_auth, "owner_custom_provider_models", lambda: [])

    release = threading.Event()
    # The real probe runs: only the subprocess boundary itself is replaced, and
    # every provider-CLI launch (run or Popen) records the thread it ran on.
    probe_threads: list[int] = []
    popen_threads: list[int] = []
    original_run = subprocess.run
    original_popen = subprocess.Popen

    def _is_provider_cli(argv: Any) -> bool:
        if isinstance(argv, (str, bytes)) or not argv:
            return False
        return Path(str(list(argv)[0])) == exe

    def recording_run(argv, *args, **kwargs):
        if _is_provider_cli(argv):
            probe_threads.append(threading.get_ident())
            release.wait(_BLOCK_SECONDS)
            return SimpleNamespace(
                returncode=0, stdout="\n".join(_ROWS).encode("utf-8") + b"\n", stderr=b""
            )
        return original_run(argv, *args, **kwargs)

    class RecordingPopen(original_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *rest: Any, **kwargs: Any) -> None:
            if _is_provider_cli(args):
                popen_threads.append(threading.get_ident())
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)

    # Force the finalization probe on (it only runs for a launchable route on
    # Windows) and let its provisioning reach the dirty-seed scan seams.
    seed_threads: list[tuple[str, int]] = []
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: True)
    original_status = repo_policy._provider_status

    def launchable_status(*args: Any, **kwargs: Any) -> dict[str, Any]:
        row = original_status(*args, **kwargs)
        if row["adapter_id"] == runtime_adapters.OPENCODE_CLI_ADAPTER:
            row["launchable"] = True
            row["coverage_required"] = True
        return row

    monkeypatch.setattr(repo_policy, "_provider_status", launchable_status)

    def recording_dirty(worktree, seeded, run):  # noqa: ARG001
        seed_threads.append(("dirty_seeded_paths", threading.get_ident()))
        return ()

    def recording_hide(worktree, seeded, run):  # noqa: ARG001
        seed_threads.append(("hide_live_gitignore_seeds", threading.get_ident()))
        return (), ()

    monkeypatch.setattr(dirty_canonical_seed, "dirty_seeded_paths", recording_dirty)
    monkeypatch.setattr(
        worker_workspace_ignore_seed, "hide_live_gitignore_seeds", recording_hide
    )

    def provisioning(repo_arg, request_id, card, adapter_id, *a, **k):  # noqa: ARG001
        # The two provisioning steps create_workspace runs after seeding.
        dirty_canonical_seed.dirty_seeded_paths(repo_arg, [], None)
        worker_workspace_ignore_seed.hide_live_gitignore_seeds(repo_arg, [], None)
        release.wait(_BLOCK_SECONDS)
        raise worker_workspace.WorkspaceError("nf02274_fake_provisioning")

    monkeypatch.setattr(worker_workspace, "create_workspace", provisioning)

    snapshot_thread = threading.get_ident()
    try:
        started = time.perf_counter()
        first = dashboard_mcp_app.snapshot_view(full=True)
        cold = time.perf_counter() - started
        _wait(lambda: len(seed_threads) == 2 and probe_threads)
        started = time.perf_counter()
        dashboard_mcp_app.snapshot_view(full=True)
        warm = time.perf_counter() - started
    finally:
        release.set()
    assert cold < _BLOCK_SECONDS / 2 and warm < _BLOCK_SECONDS / 2
    assert probe_threads, "the real probe reached subprocess.run"
    assert snapshot_thread not in probe_threads, "provider CLI ran on the snapshot thread"
    assert snapshot_thread not in popen_threads, "provider CLI spawned on the snapshot thread"
    assert all(ident != snapshot_thread for _name, ident in seed_threads), seed_threads
    assert {name for name, _ident in seed_threads} == {
        "dirty_seeded_paths",
        "hide_live_gitignore_seeds",
    }
    row = _opencode_row(first)
    assert row["opencode_models_probe"] == {"state": "probing"}
    assert row["provider_observed_models"] == []
    preflight = first["environment_preflight"]
    assert preflight["status"] in {"probing", "blocked"}
    assert "provider_models_probe_pending" in preflight["warnings"]
    assert "no_launchable_provider_routes" not in preflight["errors"]
    assert "selected_adapter_not_launchable" not in preflight["errors"]
    assert not dashboard_mcp_app._cacheable_build(first), "a probing build is not memoized"

    _wait(lambda: repo_policy.provider_probe_cache_stats()[0]["refreshing"] == 0)
    _wait(lambda: not worker_workspace._FINALIZATION_PROBE_ACTIVE)
    settled = dashboard_mcp_app.snapshot_view(full=True)
    row = _opencode_row(settled)
    assert row["opencode_models_probe"] == {"state": "ok", "count": 2}
    assert row["provider_observed_models"] == _ROWS
    assert len(probe_threads) == 1, "the landed probe is served from the cache"


def test_cached_probe_is_served_without_blocking_inside_the_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = str(tmp_path / "opencode")
    calls: list[int] = []
    monkeypatch.setattr(repo_policy.opencode_auth, "owner_custom_provider_models", lambda: [])

    def probe(executable: str, timeout: float):  # noqa: ARG001
        calls.append(threading.get_ident())
        return list(_ROWS), None

    monkeypatch.setattr(repo_policy, "_opencode_models_probe_once", probe)
    synchronous = repo_policy._cached_opencode_models(exe)
    with repo_policy.nonblocking_provider_probes():
        served = repo_policy._cached_opencode_models(exe)
    assert served == synchronous == (_ROWS, {"state": "ok", "count": 2})
    assert len(calls) == 1


# ------------------------------------------------------- (c) shared cost ledger


class _CountingCopy(SimpleNamespace):
    pass


def test_dashboard_cost_ledger_is_not_deepcopied_and_cannot_corrupt_the_memo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _ready_repo(tmp_path)
    monkeypatch.setattr(cost_ledger, "_LEDGER_MEMO", type(cost_ledger._LEDGER_MEMO)())
    provider = dashboard.DashboardProvider(repo_root=repo)
    baseline = cost_ledger.build_cost_ledger(repo_root=repo, include_tasks=True)

    deepcopies: list[int] = []

    def counting_deepcopy(value: Any, memo: Any = None) -> Any:
        deepcopies.append(1)
        return copy.deepcopy(value, memo)

    monkeypatch.setattr(
        cost_ledger, "copy", _CountingCopy(deepcopy=counting_deepcopy, copy=copy.copy)
    )
    shared = provider._cost_ledger(include_tasks=True)
    assert deepcopies == [], "a memo hit handed to the dashboard is not deep-copied"

    volatile = {"claude_code_sessions", "cost_totals"}
    assert {k: v for k, v in shared.items() if k not in volatile} == {
        k: v for k, v in baseline.items() if k not in volatile
    }, "the shared ledger reports exactly what the copying path reports"

    nested = [key for key, value in shared.items() if isinstance(value, (dict, list))]
    nested = [key for key in nested if key not in volatile]
    assert nested, "the ledger has nested sections to protect"
    for key in nested:
        value = shared[key]
        with pytest.raises(TypeError):
            if isinstance(value, dict):
                value["nf02274"] = "corrupt"
            else:
                value.append("corrupt")
    shared["aggregates"] = "replaced"  # top level is the caller's own dict

    again = provider._cost_ledger(include_tasks=True)
    assert again.get("aggregates") == baseline.get("aggregates")
    assert {k: v for k, v in again.items() if k not in volatile} == {
        k: v for k, v in baseline.items() if k not in volatile
    }

    # Every other caller still receives an independent, mutable deep copy.
    monkeypatch.setattr(cost_ledger, "copy", copy)
    private = cost_ledger.build_cost_ledger(repo_root=repo, include_tasks=True)
    for key in nested:
        value = private[key]
        if isinstance(value, dict):
            value["nf02274"] = "mine"
            assert type(value) is dict
        else:
            value.append("mine")
            assert type(value) is list
    fresh = cost_ledger.build_cost_ledger(repo_root=repo, include_tasks=True)
    assert {k: v for k, v in fresh.items() if k not in volatile} == {
        k: v for k, v in baseline.items() if k not in volatile
    }


def _containers(value: Any) -> list[Any]:
    found: list[Any] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found.append(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            found.append(item)
            stack.extend(item)
    return found


def test_default_cost_ledger_callers_get_a_fully_mutable_independent_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _ready_repo(tmp_path)
    monkeypatch.setattr(cost_ledger, "_LEDGER_MEMO", type(cost_ledger._LEDGER_MEMO)())
    volatile = {"claude_code_sessions", "cost_totals"}
    baseline = cost_ledger.build_cost_ledger(repo_root=repo, include_tasks=True)
    private = cost_ledger.build_cost_ledger(repo_root=repo, include_tasks=True)
    nested = [
        container
        for key, value in private.items()
        if key not in volatile
        for container in _containers(value)
    ]
    assert nested, "the ledger has nested rows to mutate"
    for container in nested:
        # Plain mutable containers at every depth, never the memo's frozen ones.
        assert type(container) in (dict, list), type(container)
    for container in nested:
        if isinstance(container, dict):
            container["nf02274"] = "mine"
        else:
            container.append("mine")
    shared = cost_ledger.build_cost_ledger(
        repo_root=repo, include_tasks=True, shared_read_only=True
    )
    fresh = cost_ledger.build_cost_ledger(repo_root=repo, include_tasks=True)
    for result in (shared, fresh):
        assert {k: v for k, v in result.items() if k not in volatile} == {
            k: v for k, v in baseline.items() if k not in volatile
        }, "a default caller's nested mutation never reaches the memo"
