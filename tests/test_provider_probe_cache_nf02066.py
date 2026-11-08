"""NF-2026-02066: provider/executable probes are cached between dashboard polls.

Every cache here is in-process only; each test resets the caches it touches so
no test depends on the order it runs in.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import (
    deepseek_credentials,
    repo_policy,
    repository_state,
    runtime_adapters,
    vscode_lm_bridge,
)

_ROWS = ["opencode/a-free", "opencode-go/b"]


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    xdg = tmp_path / "xdg_empty"
    xdg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    runtime_adapters.reset_executable_resolution_cache()
    vscode_lm_bridge.reset_repo_id_memo()
    repo_policy.reset_provider_probe_caches()
    yield
    runtime_adapters.reset_executable_resolution_cache()
    vscode_lm_bridge.reset_repo_id_memo()
    repo_policy.reset_provider_probe_caches()


def _executable_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Mode is set at creation: chmod is not available in every sandbox.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    try:
        os.write(fd, b"#!/bin/sh\nexit 0\n")
    finally:
        os.close(fd)
    return path


def _counting_which(monkeypatch: pytest.MonkeyPatch, target: Path | None) -> list[str]:
    calls: list[str] = []

    def fake_which(name, mode=os.F_OK | os.X_OK, path=None):  # noqa: ARG001
        calls.append(name)
        return str(target) if target is not None else None

    monkeypatch.setattr(runtime_adapters.shutil, "which", fake_which)
    return calls


# ---------------------------------------------------------------------------
# runtime_adapters.resolve_executable
# ---------------------------------------------------------------------------


def test_resolve_executable_is_memoized_and_identical_to_uncached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = _executable_file(tmp_path / "bin" / "claude")
    calls = _counting_which(monkeypatch, exe)

    first = runtime_adapters.resolve_executable("claude_cli")
    second = runtime_adapters.resolve_executable("claude_cli")
    uncached = runtime_adapters._resolve_executable_uncached("claude_cli")

    assert first.ok
    assert first == second == uncached
    assert calls == ["claude", "claude"], "one memoized call + one explicit uncached"


def test_resolve_executable_memo_key_tracks_path_and_which_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = _executable_file(tmp_path / "bin" / "claude")
    calls = _counting_which(monkeypatch, exe)
    runtime_adapters.resolve_executable("claude_cli")
    monkeypatch.setenv("PATH", str(tmp_path / "other"))
    runtime_adapters.resolve_executable("claude_cli")
    monkeypatch.setenv("PATHEXT", ".EXE;.NF02066")
    runtime_adapters.resolve_executable("claude_cli")
    assert len(calls) == 3, "PATH and PATHEXT are part of the key"

    missing_calls = _counting_which(monkeypatch, None)
    resolution = runtime_adapters.resolve_executable("claude_cli")
    assert missing_calls == ["claude"], "a new shutil.which bypasses the memo"
    assert not resolution.ok


def test_resolve_executable_override_is_part_of_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _counting_which(monkeypatch, None)
    one = _executable_file(tmp_path / "one" / "claude")
    two = _executable_file(tmp_path / "two" / "claude")
    first = runtime_adapters.resolve_executable("claude_cli", {"claude_cli": one})
    second = runtime_adapters.resolve_executable("claude_cli", {"claude_cli": two})
    assert first.executable == str(one.resolve())
    assert second.executable == str(two.resolve())


def test_resolve_executable_re_resolves_a_vanished_path_and_after_ttl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = _executable_file(tmp_path / "bin" / "claude")
    calls = _counting_which(monkeypatch, exe)
    assert runtime_adapters.resolve_executable("claude_cli").ok
    exe.unlink()
    after_delete = runtime_adapters.resolve_executable("claude_cli")
    assert len(calls) == 2, "a memoized path that no longer exists re-resolves"
    assert not after_delete.ok
    assert after_delete == runtime_adapters._resolve_executable_uncached("claude_cli")

    _executable_file(exe)
    monkeypatch.setattr(runtime_adapters, "EXECUTABLE_RESOLUTION_TTL_SECONDS", 0.0)
    calls.clear()
    assert runtime_adapters.resolve_executable("claude_cli").ok
    assert runtime_adapters.resolve_executable("claude_cli").ok
    assert len(calls) == 2, "an expired entry is never served"


def test_resolve_executable_memo_is_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _counting_which(monkeypatch, None)
    monkeypatch.setattr(runtime_adapters, "_EXECUTABLE_RESOLUTION_MAX_ENTRIES", 2)
    for index in range(4):
        monkeypatch.setenv("PATH", str(tmp_path / f"p{index}"))
        runtime_adapters.resolve_executable("claude_cli")
    assert len(runtime_adapters._EXECUTABLE_RESOLUTION_MEMO) == 2
    runtime_adapters.reset_executable_resolution_cache()
    assert not runtime_adapters._EXECUTABLE_RESOLUTION_MEMO


# ---------------------------------------------------------------------------
# repository_state / vscode_lm_bridge._repo_id
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path, name: str, repo_id: str) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    repository_state.bootstrap_repository(root, repo_id=repo_id)
    return root


def test_inspect_repository_maps_each_root_once_and_is_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _repo(tmp_path, "r", "repo_a0000000000000000000000000002066")
    baseline = repository_state.inspect_repository(root)

    resolved: list[Path] = []
    original = Path.resolve
    # Patched process-wide, so count only this thread: a daemon thread left
    # running by a neighbouring test must not inflate the count.
    counting_thread = threading.get_ident()

    def counting_resolve(self: Path, strict: bool = False) -> Path:
        if threading.get_ident() == counting_thread:
            resolved.append(self)
        return original(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    state = repository_state.inspect_repository(root)
    monkeypatch.setattr(Path, "resolve", original)

    assert state == baseline
    # Uncached shape, child by child, on the same inputs.
    hub = repository_state._assert_relative_child(root, repository_state.HUB_DIRNAME)
    assert state.hub_dir == hub
    assert state.durable_paths == {
        name: repository_state._assert_relative_child(hub, rel)
        for name, rel in repository_state.DURABLE_LAYOUT.items()
    }
    # Root of each base mapped once: 1 explicit-root resolve + repo + hub
    # + one per child (hub, manifest, durable dirs, runtime).
    children = 2 + len(repository_state.DURABLE_LAYOUT) + 1
    assert len(resolved) <= 1 + 2 + children
    assert len(resolved) < 1 + 2 * children


def test_repo_id_is_memoized_under_the_manifest_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _repo(tmp_path, "r", "repo_b0000000000000000000000000002066")
    calls: list[Path] = []
    original = repository_state.inspect_repository

    def counting_inspect(repo, *args, **kwargs):
        calls.append(Path(repo))
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(repository_state, "inspect_repository", counting_inspect)
    first = vscode_lm_bridge._repo_id(root)
    second = vscode_lm_bridge._repo_id(root)
    assert first == second == vscode_lm_bridge._repo_id_uncached(root)
    assert first == "repo_b0000000000000000000000000002066"
    assert len(calls) == 2, "one memoized verification + one explicit uncached"

    manifest = root / repository_state.PROJECT_MANIFEST_REL
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["repo_id"] = "repo_c0000000000000000000000000002066"
    replacement = manifest.with_name("project.json.new")
    replacement.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(replacement, manifest)
    calls.clear()
    assert vscode_lm_bridge._repo_id(root) == "repo_c0000000000000000000000000002066"
    assert len(calls) == 1, "a changed fingerprint re-verifies identity"


def test_repo_id_failure_is_not_memoized(tmp_path: Path) -> None:
    root = _repo(tmp_path, "r", "repo_d0000000000000000000000000002066")
    manifest = root / repository_state.PROJECT_MANIFEST_REL
    manifest.write_text("{not json", encoding="utf-8")
    with pytest.raises(repository_state.RepositoryStateError):
        vscode_lm_bridge._repo_id(root)
    assert not vscode_lm_bridge._REPO_ID_MEMO


# ---------------------------------------------------------------------------
# repo_policy TTL single-flight caches
# ---------------------------------------------------------------------------


def _wait_idle(cache: repo_policy._TtlSingleFlightCache) -> None:
    deadline = time.monotonic() + 10.0
    while cache.stats()["refreshing"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache.stats()["refreshing"] == 0


def test_single_flight_serves_stale_while_exactly_one_refresh_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = repo_policy._TtlSingleFlightCache("nf02066")
    release = threading.Event()
    calls: list[int] = []

    def compute() -> dict:
        calls.append(len(calls))
        if len(calls) > 1:
            release.wait(10.0)
        return {"value": len(calls)}

    assert cache.get("k", compute) == {"value": 1}, "first call is synchronous"
    assert cache.get("k", compute) == {"value": 1}
    assert calls == [0], "inside the TTL the cached value is served"

    monkeypatch.setattr(repo_policy, "PROVIDER_PROBE_CACHE_TTL_SECONDS", 0.0)
    served = [cache.get("k", compute) for _ in range(5)]
    assert served == [{"value": 1}] * 5, "stale value served meanwhile"
    assert cache.stats()["refreshes_started"] == 1
    release.set()
    _wait_idle(cache)
    assert len(calls) == 2, "exactly one background refresh ran"
    monkeypatch.setattr(repo_policy, "PROVIDER_PROBE_CACHE_TTL_SECONDS", 60.0)
    assert cache.get("k", compute) == {"value": 2}
    assert cache.observed_at("k") is not None


def test_single_flight_never_serves_beyond_max_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = repo_policy._TtlSingleFlightCache("nf02066")
    calls: list[int] = []

    def compute() -> int:
        calls.append(1)
        return len(calls)

    assert cache.get("k", compute) == 1
    monkeypatch.setattr(repo_policy, "PROVIDER_PROBE_CACHE_TTL_SECONDS", 0.0)
    monkeypatch.setattr(repo_policy, "PROVIDER_PROBE_MAX_STALE_SECONDS", 0.0)
    assert cache.get("k", compute) == 2, "too-old value recomputes synchronously"
    cache.reset()
    assert cache.observed_at("k") is None


def test_cached_value_is_a_copy() -> None:
    cache = repo_policy._TtlSingleFlightCache("nf02066")
    first = cache.get("k", lambda: {"rows": [1]})
    first["rows"].append(2)
    assert cache.get("k", lambda: {"rows": [9]}) == {"rows": [1]}


def _fake_opencode(monkeypatch: pytest.MonkeyPatch, results: list) -> list[int]:
    calls: list[int] = []

    def fake_run(argv, **kwargs):  # noqa: ARG001
        calls.append(1)
        return results[min(len(calls), len(results)) - 1]

    monkeypatch.setattr(repo_policy.subprocess, "run", fake_run)
    return calls


def _ok(rows: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        returncode=0, stdout="\n".join(rows).encode("utf-8") + b"\n", stderr=b""
    )


def test_opencode_models_cache_matches_uncached_and_skips_reprobe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = str(tmp_path / "opencode")
    calls = _fake_opencode(monkeypatch, [_ok(_ROWS)])

    uncached = repo_policy._opencode_models_with_record(exe)
    cached = repo_policy._cached_opencode_models(exe)
    again = repo_policy._cached_opencode_models(exe)

    assert cached == again == uncached == (_ROWS, {"state": "ok", "count": 2})
    assert len(calls) == 2, "uncached probe + first cached probe; third is a hit"
    assert repo_policy._OPENCODE_PROBE_LOCAL.record == {"state": "ok", "count": 2}


def test_opencode_failed_probe_is_rechecked_not_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = str(tmp_path / "opencode")
    failed = SimpleNamespace(returncode=3, stdout=b"", stderr=b"")
    calls = _fake_opencode(monkeypatch, [failed, _ok(_ROWS)])

    first = repo_policy._cached_opencode_models(exe)
    assert first[1] == {
        "state": "failed",
        "reason": "exit_3",
        "served_last_known_good": False,
    }
    second = repo_policy._cached_opencode_models(exe)
    assert second == (_ROWS, {"state": "ok", "count": 2})
    assert len(calls) == 2


def test_deepseek_status_cache_matches_uncached_and_tracks_credential_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(deepseek_credentials.CREDENTIAL_PATH_ENV, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()

    original = deepseek_credentials.credential_status
    calls: list[int] = []

    def counting_status(**kwargs):
        calls.append(1)
        return original(**kwargs)

    monkeypatch.setattr(deepseek_credentials, "credential_status", counting_status)
    uncached = original(repo=repo)
    first = repo_policy._cached_deepseek_credential_status(repo)
    second = repo_policy._cached_deepseek_credential_status(repo)
    assert first == second == uncached
    assert len(calls) == 1, "second call served from the cache"

    credential = deepseek_credentials.credential_path()
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("{}", encoding="utf-8")
    third = repo_policy._cached_deepseek_credential_status(repo)
    assert len(calls) == 2, "a changed credential file is re-read"
    assert third == original(repo=repo)


def test_reset_functions_drop_every_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = str(tmp_path / "opencode")
    _fake_opencode(monkeypatch, [_ok(_ROWS)])
    repo_policy._cached_opencode_models(exe)
    assert repo_policy.provider_probe_cache_stats()[0]["entries"] == 1
    repo_policy.reset_provider_probe_caches()
    assert all(stat["entries"] == 0 for stat in repo_policy.provider_probe_cache_stats())


def test_repo_id_fingerprint_tracks_hub_children_with_hub_mtime_restored(
    tmp_path: Path,
) -> None:
    hub = tmp_path / repository_state.HUB_DIRNAME
    runtime_dir = hub / repository_state.RUNTIME_DIRNAME
    runtime_dir.mkdir(parents=True)
    (tmp_path / repository_state.PROJECT_MANIFEST_REL).write_text("{}", encoding="utf-8")
    before = vscode_lm_bridge._repo_id_fingerprint(tmp_path)
    assert before is not None
    hub_stat = os.stat(hub)
    runtime_dir.rmdir()
    runtime_dir.write_text("not a directory", encoding="utf-8")
    # A swap that leaves the hub's own stat unchanged must still re-verify.
    os.utime(hub, ns=(hub_stat.st_atime_ns, hub_stat.st_mtime_ns))
    assert vscode_lm_bridge._repo_id_fingerprint(tmp_path) != before
