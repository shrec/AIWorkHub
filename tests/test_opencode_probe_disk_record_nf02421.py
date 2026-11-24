"""NF-2026-02421: the opencode ``models`` probe record survives the MCP process.

Every test uses a fake probe (no real ``opencode`` process).  The fake is made
the disk record's "original" by patching ``_OPENCODE_DISK_RECORD_ORIGINALS``
alongside it; a probe patched WITHOUT that keeps today's path and never touches
the file.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import opencode_auth, repo_policy


class FakeProbe:
    def __init__(self, rows: list[str], reason: str | None = None) -> None:
        self.rows = list(rows)
        self.reason = reason
        self.calls = 0
        self.gate: threading.Event | None = None

    def __call__(self, executable: str, timeout: int) -> tuple[list[str], str | None]:
        self.calls += 1
        if self.gate is not None:
            assert self.gate.wait(10)
        return list(self.rows), self.reason


def _owner_models() -> list[str]:
    return []


def _install(monkeypatch: pytest.MonkeyPatch, probe: FakeProbe) -> None:
    """Use ``probe`` as the unpatched probe the disk record trusts."""

    monkeypatch.setattr(repo_policy, "_opencode_models_probe_once", probe)
    monkeypatch.setattr(opencode_auth, "owner_custom_provider_models", _owner_models)
    monkeypatch.setattr(
        repo_policy,
        "_OPENCODE_DISK_RECORD_ORIGINALS",
        (repo_policy._list_opencode_models, probe, subprocess.run, _owner_models),
    )


def _fresh_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop everything a new MCP process would not have."""

    repo_policy.reset_provider_probe_caches()
    monkeypatch.setattr(repo_policy, "_OPENCODE_LAST_GOOD_MODELS", {})
    monkeypatch.setattr(repo_policy, "_OPENCODE_LAST_PROBE", {})


def _wait_idle() -> None:
    deadline = time.monotonic() + 10
    while repo_policy._OPENCODE_MODELS_CACHE.stats()["refreshing"]:
        assert time.monotonic() < deadline, "background refresh never finished"
        time.sleep(0.01)


def _record_path() -> Path:
    path = repo_policy._opencode_disk_record_path()
    assert path is not None
    return path


def _payload() -> dict[str, Any]:
    return json.loads(_record_path().read_text(encoding="utf-8"))


def _rewrite(**changes: Any) -> None:
    payload = _payload()
    payload.update(changes)
    _record_path().write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(repo_policy, "_POLLED_PROBE_THREAD_CLASS", None)
    monkeypatch.setattr(repo_policy, "_OPENCODE_DISK_RECORD_COUNTERS", {})
    monkeypatch.setattr(repo_policy._PROBE_MODE_LOCAL, "nonblocking", False, raising=False)
    _fresh_process(monkeypatch)
    yield
    _wait_idle()
    repo_policy.reset_provider_probe_caches()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub").mkdir(parents=True)
    (root / ".aiworkhub" / "project.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    return root


@pytest.fixture
def exe(tmp_path: Path) -> str:
    path = tmp_path / "bin" / "opencode-fake"
    path.parent.mkdir()
    path.write_text("fake", encoding="utf-8")
    return str(path)


def _first_process(monkeypatch: pytest.MonkeyPatch, exe: str) -> tuple[list[str], Any]:
    probe = FakeProbe(["acme/one", "acme/two"])
    _install(monkeypatch, probe)
    result = repo_policy._cached_opencode_models(exe)
    assert probe.calls == 1
    assert _record_path().is_file()
    return result


def test_ok_probe_record_is_written_atomically(repo, exe, monkeypatch):
    replaced: list[tuple[Any, Any]] = []
    real_replace = os.replace

    def spy(src: Any, dst: Any) -> None:
        replaced.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(repo_policy.os, "replace", spy)
    listed, record = _first_process(monkeypatch, exe)

    path = _record_path()
    assert path.is_relative_to(repo.resolve() / ".aiworkhub" / "runtime")
    assert len(replaced) == 1
    assert Path(replaced[0][1]) == path
    assert Path(replaced[0][0]).parent == path.parent
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]

    payload = _payload()
    assert listed == ["acme/one", "acme/two"]
    assert record == {"state": "ok", "count": 2}
    assert payload["listed"] == listed
    assert payload["record"] == record
    assert payload["executable"] == exe
    assert payload["stat_token"] == list(repo_policy._stat_token(exe))
    assert payload["env_digest"] == repo_policy._opencode_env_digest(
        repo_policy._probe_env_token()
    )
    assert abs(payload["probed_at"] - time.time()) < 60


def test_failed_probe_record_is_written(repo, exe, monkeypatch):
    probe = FakeProbe([], "exit_2")
    _install(monkeypatch, probe)
    _listed, record = repo_policy._cached_opencode_models(exe)
    assert record["state"] == "failed"
    assert _payload()["record"] == record


def test_no_environment_value_is_written(repo, exe, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "cfg-SENTINEL-9f3e.json"))
    _first_process(monkeypatch, exe)
    assert "SENTINEL-9f3e" not in _record_path().read_text(encoding="utf-8")


def test_probing_value_is_never_persisted(repo, exe, monkeypatch):
    probe = FakeProbe(["acme/one"])
    _install(monkeypatch, probe)
    key = (
        exe,
        repo_policy._stat_token(exe),
        repo_policy._probe_env_token(),
        *repo_policy._OPENCODE_DISK_RECORD_ORIGINALS,
    )
    repo_policy._persist_opencode_disk_record(
        key, ([], {"state": repo_policy.OPENCODE_PROBE_STATE_PROBING})
    )
    repo_policy._persist_opencode_disk_record(key, repo_policy.PROBE_PENDING)
    assert not _record_path().exists()


def test_fresh_record_is_served_by_a_new_process_without_probing(repo, exe, monkeypatch):
    first = _first_process(monkeypatch, exe)
    _fresh_process(monkeypatch)
    probe = FakeProbe(["other/model"])
    _install(monkeypatch, probe)

    assert repo_policy._cached_opencode_models(exe) == first
    assert repo_policy._cached_opencode_models(exe) == first
    assert probe.calls == 0
    stats = repo_policy.opencode_disk_record_stats()
    assert stats["seeded"] == 1
    assert stats["written"] == 1  # the first process only: a seed is no store


def test_stale_record_is_served_while_one_background_refresh_rewrites(
    repo, exe, monkeypatch
):
    first = _first_process(monkeypatch, exe)
    ttl = repo_policy.PROVIDER_PROBE_CACHE_TTL_SECONDS
    max_stale = repo_policy.PROVIDER_PROBE_MAX_STALE_SECONDS
    old_probed_at = time.time() - (ttl + max_stale) / 2
    _rewrite(probed_at=old_probed_at)
    _fresh_process(monkeypatch)
    probe = FakeProbe(["acme/new"])
    probe.gate = threading.Event()
    _install(monkeypatch, probe)

    try:
        assert repo_policy._cached_opencode_models(exe) == first
        assert repo_policy._cached_opencode_models(exe) == first
        assert _payload()["probed_at"] == old_probed_at
    finally:
        probe.gate.set()
    _wait_idle()

    assert probe.calls == 1
    payload = _payload()
    assert payload["listed"] == ["acme/new"]
    assert payload["probed_at"] > old_probed_at
    assert repo_policy._cached_opencode_models(exe) == (
        ["acme/new"],
        {"state": "ok", "count": 1},
    )
    assert probe.calls == 1
    assert repo_policy._OPENCODE_MODELS_CACHE.stats()["refreshes_started"] == 1


def _assert_falls_back(monkeypatch, exe: str) -> None:
    _fresh_process(monkeypatch)
    probe = FakeProbe(["acme/fallback"])
    _install(monkeypatch, probe)
    assert repo_policy._cached_opencode_models(exe) == (
        ["acme/fallback"],
        {"state": "ok", "count": 1},
    )
    assert probe.calls == 1
    assert "seeded" not in repo_policy.opencode_disk_record_stats()


def test_changed_executable_stat_falls_back(repo, exe, monkeypatch):
    _first_process(monkeypatch, exe)
    Path(exe).write_text("a different, longer fake executable", encoding="utf-8")
    _assert_falls_back(monkeypatch, exe)
    assert repo_policy.opencode_disk_record_stats()["read_rejected_key_mismatch"] == 1


def test_changed_probe_environment_falls_back(repo, exe, monkeypatch, tmp_path):
    _first_process(monkeypatch, exe)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "other-config"))
    _assert_falls_back(monkeypatch, exe)


@pytest.mark.parametrize(
    "damage",
    ["missing", "malformed_json", "wrong_schema", "bad_listing", "too_old", "future"],
)
def test_unusable_record_falls_back(repo, exe, monkeypatch, damage):
    _first_process(monkeypatch, exe)
    path = _record_path()
    if damage == "missing":
        path.unlink()
    elif damage == "malformed_json":
        path.write_text("{not json", encoding="utf-8")
    elif damage == "wrong_schema":
        _rewrite(schema="something.else.v0")
    elif damage == "bad_listing":
        _rewrite(listed=[1, 2])
    elif damage == "too_old":
        _rewrite(probed_at=time.time() - repo_policy.PROVIDER_PROBE_MAX_STALE_SECONDS - 5)
    else:
        _rewrite(probed_at=time.time() + 3600)
    _assert_falls_back(monkeypatch, exe)


def test_mismatch_keeps_the_nonblocking_path(repo, exe, monkeypatch):
    _first_process(monkeypatch, exe)
    before = _record_path().read_bytes()
    Path(exe).write_text("changed executable bytes", encoding="utf-8")
    _fresh_process(monkeypatch)
    probe = FakeProbe(["acme/bg"])
    probe.gate = threading.Event()
    _install(monkeypatch, probe)
    monkeypatch.setattr(repo_policy._PROBE_MODE_LOCAL, "nonblocking", True, raising=False)

    try:
        listed, record = repo_policy._cached_opencode_models(exe)
        assert listed == []
        assert record == {"state": repo_policy.OPENCODE_PROBE_STATE_PROBING}
        # The probing result is never persisted.
        assert _record_path().read_bytes() == before
    finally:
        probe.gate.set()
    _wait_idle()
    assert probe.calls == 1
    assert _payload()["listed"] == ["acme/bg"]


@pytest.mark.parametrize(
    "patched",
    [
        "_opencode_models_probe_once",
        "_list_opencode_models",
        "subprocess.run",
        "owner_custom_provider_models",
    ],
)
def test_patched_probe_neither_reads_nor_writes_the_record(
    repo, exe, monkeypatch, patched
):
    _first_process(monkeypatch, exe)
    path = _record_path()
    before = path.read_bytes()
    _fresh_process(monkeypatch)

    def refuse_read(_path: Path) -> None:
        raise AssertionError("disk record read with a patched probe")

    monkeypatch.setattr(repo_policy, "_read_opencode_disk_record", refuse_read)
    stub = FakeProbe(["stub/model"])
    if patched == "_opencode_models_probe_once":
        monkeypatch.setattr(repo_policy, "_opencode_models_probe_once", stub)
    elif patched == "_list_opencode_models":
        monkeypatch.setattr(
            repo_policy, "_list_opencode_models", lambda executable: ["stub/model"]
        )
    elif patched == "subprocess.run":
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    else:
        monkeypatch.setattr(
            opencode_auth, "owner_custom_provider_models", lambda: ["stub/owner"]
        )

    listed, _record = repo_policy._cached_opencode_models(exe)
    if patched in ("_opencode_models_probe_once", "_list_opencode_models"):
        assert listed == ["stub/model"]
    if patched == "_opencode_models_probe_once":
        assert stub.calls == 1
    assert path.read_bytes() == before
    stats = repo_policy.opencode_disk_record_stats()
    assert stats.get("written") == 1
    assert "seeded" not in stats


def test_no_known_repository_reads_and_writes_nothing(tmp_path, exe, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    monkeypatch.chdir(elsewhere)

    def refuse_read(_path: Path) -> None:
        raise AssertionError("disk record read with no known repository")

    monkeypatch.setattr(repo_policy, "_read_opencode_disk_record", refuse_read)
    probe = FakeProbe(["acme/one"])
    _install(monkeypatch, probe)

    assert repo_policy._opencode_disk_record_path() is None
    assert repo_policy._cached_opencode_models(exe) == (
        ["acme/one"],
        {"state": "ok", "count": 1},
    )
    assert probe.calls == 1
    assert not (elsewhere / ".aiworkhub").exists()
    assert repo_policy.opencode_disk_record_stats() == {}
