"""NF-2026-02324: the reader's ``opencode models`` refresh is a polled child.

The dashboard reader is single-threaded (NF-2026-02287): its fence turns
repo_policy's refresh thread into a job drained inline after each reply. The
``opencode models`` probe used to run there with a blocking ``subprocess.run``
(measured 20.79 s per refresh), stalling every request meanwhile. It now
starts a child process and later drains collect it with ``poll()``.

Every test drives a fake ``opencode`` created in ``tmp_path`` (a tiny Python
script behind a ``.cmd`` shim on Windows, a shebang script on POSIX); the real
provider CLI is never run.

NF-2026-02338 extends the same polled child to the Claude and Codex auth
probes, with fake ``claude`` and ``codex`` CLIs that block until released.
"""

from __future__ import annotations

import collections
import importlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import (
    claude_auth,
    codex_auth,
    dashboard_mcp_app,
    dashboard_reader,
    deepseek_credentials,
    glm_credentials,
    kilo_auth,
    opencode_auth,
    platform_io,
    repo_policy,
    runtime_adapters,
    task_reconciler,
    vscode_lm_bridge,
    workforce_catalog,
)

MODEL_OUTPUT = "openai/gpt-4o\nanthropic/claude-sonnet-4\n"
OWNER_MODEL = "custom-nf02324/owner-model"
LAST_GOOD_MODEL = "openai/gpt-4o-mini"
PUBLISH_WAIT_SECONDS = 60.0


@pytest.fixture
def reader_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A known repository with the reader's single-thread fence installed.

    Every attribute the fence replaces is registered with ``monkeypatch``
    first, so the fence is undone after the test.
    """
    from aiworkhub import dashboard

    repo = tmp_path / "repo"
    (repo / ".aiworkhub").mkdir(parents=True)
    (repo / ".aiworkhub" / "project.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    # A job one test queued must never run in the next test's drain.
    monkeypatch.setattr(dashboard_reader, "_DEFERRED", collections.deque())
    for module_name in dashboard_reader.FENCED_THREAD_MODULES:
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "threading", module.threading)
    monkeypatch.setattr(dashboard, "_snapshot_read_worker_limit", dashboard._snapshot_read_worker_limit)
    monkeypatch.setattr(repo_policy, "_POLLED_PROBE_THREAD_CLASS", None)
    monkeypatch.setattr(repo_policy, "_OPENCODE_LAST_GOOD_MODELS", {})
    monkeypatch.setattr(repo_policy, "_OPENCODE_LAST_PROBE", {})
    monkeypatch.setattr(opencode_auth, "owner_custom_provider_models", lambda: [OWNER_MODEL])
    repo_policy.reset_provider_probe_caches()
    dashboard_reader.install_single_thread_fence()
    try:
        yield repo
    finally:
        dashboard_reader.stop_polled_work()
        repo_policy.reset_provider_probe_caches()


def _fake_opencode(
    tmp_path: Path, name: str, *, sleep: float, output: str, exit_code: int = 0
) -> tuple[str, Path]:
    """Create a fake ``opencode``; return (executable, file of child pids).

    POSIX: a shebang script. Windows: a console-script launcher ``.exe`` (the
    one pip installs scripts with: launcher stub + shebang + zipped
    ``__main__.py``), so no ``cmd.exe`` shim is involved and the launcher ->
    interpreter pair is a real process tree; a ``.cmd`` shim only when the
    launcher stub is unavailable.
    """
    folder = tmp_path / f"fake-{name}"
    folder.mkdir()
    pid_file = folder / "pids.txt"
    source = (
        "import os, sys, time\n"
        f"with open({str(pid_file)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(f'{os.getpid()}\\n')\n"
        f"time.sleep({sleep!r})\n"
        f"sys.stdout.write({output!r})\n"
        "sys.stdout.flush()\n"
        f"sys.exit({exit_code!r})\n"
    )
    return _fake_cli(folder, "opencode", source), pid_file


def _fake_cli(folder: Path, command: str, source: str) -> str:
    """Write ``source`` as the executable ``command`` in ``folder``."""
    if os.name != "nt":
        launcher = folder / command
        launcher.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        os.chmod(launcher, 0o755)
        return str(launcher)
    stub = _windows_launcher_stub()
    if stub is not None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("__main__.py", source)
        launcher = folder / f"{command}.exe"
        launcher.write_bytes(stub + b'#!"' + os.fsencode(sys.executable) + b'"\r\n' + archive.getvalue())
        return str(launcher)
    script = folder / f"fake_{command}.py"
    script.write_text(source, encoding="utf-8")
    launcher = folder / f"{command}.cmd"
    launcher.write_bytes(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n'.encode("utf-8"))
    return str(launcher)


def _windows_launcher_stub() -> bytes | None:
    try:
        from pip._vendor import distlib
    except ImportError:
        return None
    stub = Path(distlib.__file__).parent / "t64.exe"
    return stub.read_bytes() if stub.is_file() else None


def _lookup(executable: str) -> dict[str, Any]:
    """What a dashboard snapshot reads: the cached listing, never blocking."""
    with repo_policy.nonblocking_provider_probes():
        listed, record = repo_policy._cached_opencode_models(executable)
    return {"listed": listed, "record": record}


def _probing(result: dict[str, Any]) -> bool:
    return (result["record"] or {}).get("state") == repo_policy.OPENCODE_PROBE_STATE_PROBING


def _drain_until_published(executable: str) -> dict[str, Any]:
    deadline = time.monotonic() + PUBLISH_WAIT_SECONDS
    while time.monotonic() < deadline:
        dashboard_reader.drain_deferred_work()
        result = _lookup(executable)
        if not _probing(result):
            return result
        time.sleep(0.05)
    raise AssertionError("the polled opencode probe never published a result")


def _polling_jobs() -> list[Any]:
    return [job for job in dashboard_reader._DEFERRED if job.is_polling()]


def _runtime_files(repo: Path) -> list[Path]:
    root = repo / ".aiworkhub" / "runtime"
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return bool(platform_io.windows_pid_is_alive(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _assert_pids_gone(pid_file: Path) -> None:
    pids = [int(line) for line in pid_file.read_text(encoding="utf-8").split()] if pid_file.exists() else []
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and any(_pid_alive(pid) for pid in pids):
        time.sleep(0.05)
    assert not [pid for pid in pids if _pid_alive(pid)], pids


class _TimedStream(io.RawIOBase):
    """A reply stream that records when each reply line was written."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[tuple[float, dict[str, Any]]] = []

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        for raw in bytes(data).splitlines():
            if raw.strip():
                self.lines.append((time.monotonic(), json.loads(raw)))
        return len(data)


def test_serve_answers_the_next_request_while_the_probe_child_runs(
    reader_repo: Path, tmp_path: Path
) -> None:
    sleep = 6.0
    executable, pid_file = _fake_opencode(tmp_path, "slow", sleep=sleep, output=MODEL_OUTPUT)
    tools = {"probe": lambda: _lookup(executable)}
    requests = b"".join(
        json.dumps({"id": index, "tool": "probe"}).encode("utf-8") + b"\n" for index in (1, 2)
    )
    stdout = _TimedStream()
    threads_before = threading.active_count()
    started = time.monotonic()
    assert dashboard_reader.serve(io.BytesIO(requests), stdout, tools) == 0  # type: ignore[arg-type]
    assert threading.active_count() == threads_before
    assert [reply["id"] for _, reply in stdout.lines] == [1, 2]
    (_, first), (second_at, second) = stdout.lines
    # The drain after reply 1 started the child; reply 2 came while it ran,
    # served from the cache's existing (probing) value, never a guess.
    assert second_at - started < sleep / 3, second_at - started
    assert first["result"] == {"listed": [], "record": {"state": "probing"}}
    assert second["result"] == {"listed": [], "record": {"state": "probing"}}
    # serve() ending killed the unfinished child and removed its files.
    assert _polling_jobs() == []
    _assert_pids_gone(pid_file)
    assert _runtime_files(reader_repo) == []


def test_drain_starts_without_waiting_and_a_later_drain_publishes_rows(
    reader_repo: Path, tmp_path: Path
) -> None:
    sleep = 3.0
    executable, pid_file = _fake_opencode(tmp_path, "rows", sleep=sleep, output=MODEL_OUTPUT)
    expected_rows = workforce_catalog.parse_opencode_models_output(MODEL_OUTPUT.encode("utf-8"))
    assert expected_rows, "the fake output must parse into model rows"

    assert _probing(_lookup(executable))
    began = time.monotonic()
    dashboard_reader.drain_deferred_work()
    assert time.monotonic() - began < sleep / 3
    assert len(_polling_jobs()) == 1
    probe_dir = reader_repo / ".aiworkhub" / "runtime" / "opencode_models_probe"
    assert {path.parent for path in _runtime_files(reader_repo)} == {probe_dir}
    assert _probing(_lookup(executable))

    published = _drain_until_published(executable)
    assert published["record"] == {"state": "ok", "count": len(expected_rows)}
    assert published["listed"] == expected_rows + [OWNER_MODEL]
    assert repo_policy._OPENCODE_LAST_GOOD_MODELS[executable] == expected_rows
    assert repo_policy._opencode_probe_record(executable) == published["record"]
    # The next snapshot is served from the cache without another probe.
    assert _lookup(executable) == published
    assert _polling_jobs() == []
    _assert_pids_gone(pid_file)
    assert _runtime_files(reader_repo) == []


def test_a_hanging_probe_is_killed_and_last_known_good_is_served(
    reader_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repo_policy, "_OPENCODE_PROBE_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(repo_policy, "_OPENCODE_PROBE_RETRY_TIMEOUT_SECONDS", 1)
    executable, pid_file = _fake_opencode(tmp_path, "hang", sleep=120.0, output=MODEL_OUTPUT)
    repo_policy._OPENCODE_LAST_GOOD_MODELS[executable] = [LAST_GOOD_MODEL]

    assert _probing(_lookup(executable))
    published = _drain_until_published(executable)
    assert published["record"] == {
        "state": "failed",
        "reason": "timeout",
        "served_last_known_good": True,
    }
    assert published["listed"] == [LAST_GOOD_MODEL, OWNER_MODEL]
    assert repo_policy._opencode_probe_record(executable) == published["record"]
    assert _lookup(executable) == published
    _assert_pids_gone(pid_file)
    # The first attempt and its one retry each ran (and were killed).
    assert len(pid_file.read_text(encoding="utf-8").split()) == 2
    assert _runtime_files(reader_repo) == []


@pytest.mark.parametrize(
    ("output", "exit_code", "expected_record"),
    [
        (MODEL_OUTPUT, 0, {"state": "ok", "count": 2}),
        ("", 3, {"state": "failed", "reason": "exit_3", "served_last_known_good": False}),
        ("", 0, {"state": "failed", "reason": "empty", "served_last_known_good": False}),
    ],
    ids=["rows", "exit_3", "empty_with_retry"],
)
def test_polled_result_equals_the_blocking_result(
    reader_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    exit_code: int,
    expected_record: dict[str, Any],
) -> None:
    executable, pid_file = _fake_opencode(tmp_path, "same", sleep=0.0, output=output, exit_code=exit_code)
    blocking_listed, blocking_record = repo_policy._opencode_models_with_record(executable)
    assert blocking_record == expected_record
    blocking_last_good = dict(repo_policy._OPENCODE_LAST_GOOD_MODELS)
    blocking_runs = len(pid_file.read_text(encoding="utf-8").split())
    assert blocking_runs == (2 if expected_record.get("reason") == "empty" else 1)
    pid_file.unlink()
    monkeypatch.setattr(repo_policy, "_OPENCODE_LAST_GOOD_MODELS", {})
    monkeypatch.setattr(repo_policy, "_OPENCODE_LAST_PROBE", {})

    assert _probing(_lookup(executable))
    published = _drain_until_published(executable)
    assert published == {"listed": blocking_listed, "record": blocking_record}
    assert repo_policy._OPENCODE_LAST_GOOD_MODELS == blocking_last_good
    # The same one retry on empty, and no retry otherwise.
    assert len(pid_file.read_text(encoding="utf-8").split()) == blocking_runs
    assert _runtime_files(reader_repo) == []


def test_a_replaced_probe_keeps_the_blocking_path(
    reader_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, int]] = []

    def stub(executable: str, timeout: int) -> tuple[list[str], str | None]:
        calls.append((executable, timeout))
        return ["openai/gpt-4o"], None

    monkeypatch.setattr(repo_policy, "_opencode_models_probe_once", stub)
    executable, pid_file = _fake_opencode(tmp_path, "stubbed", sleep=0.0, output=MODEL_OUTPUT)
    assert _probing(_lookup(executable))
    published = _drain_until_published(executable)
    assert published["record"] == {"state": "ok", "count": 1}
    assert calls == [(executable, repo_policy._OPENCODE_PROBE_TIMEOUT_SECONDS)]
    assert not pid_file.exists(), "a replaced probe must never start the executable"
    assert _runtime_files(reader_repo) == []


def test_without_the_fence_the_refresh_is_never_polled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The threaded MCP-server path: the reader never enabled the polled probe.
    repo = tmp_path / "repo"
    (repo / ".aiworkhub").mkdir(parents=True)
    (repo / ".aiworkhub" / "project.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setattr(repo_policy, "_POLLED_PROBE_THREAD_CLASS", None)
    assert repo_policy._polled_probe_runtime_dir() is None
    monkeypatch.setattr(repo_policy, "_POLLED_PROBE_THREAD_CLASS", dashboard_reader._DeferredThread)
    # Enabled, but this module's ``threading`` is not the reader fence.
    assert repo_policy.threading is threading
    assert repo_policy._polled_probe_runtime_dir() is None


# ------------------------------------ Claude/Codex auth probes (NF-2026-02338)
#
# The fakes block until the test releases them, and the reader process is
# forbidden to start them itself, so "does not wait" never rests on a clock.

SECRET = "nf02338-must-not-escape"
CODEX_MODEL = "gpt-5.5"
CLAUDE_MODULE = "aiworkhub.claude_auth"
CODEX_MODULE = "aiworkhub.codex_auth"

_FAKE_AUTH_CLI = """\
import json, os, sys, time
folder = {folder!r}
def has(name):
    return os.path.exists(os.path.join(folder, name))
args = sys.argv[1:]
with open(os.path.join(folder, "pids.txt"), "a", encoding="utf-8") as handle:
    handle.write(str(os.getpid()) + "\\n")
limit = time.monotonic() + 120
while not has("release") and time.monotonic() < limit:
    time.sleep(0.02)
print({secret!r}, file=sys.stderr, flush=True)
logged_in = not has("logged_out")
if args[:1] == ["auth"]:
    print(json.dumps({{"loggedIn": logged_in, "authMethod": "claude.ai", "accessToken": {secret!r}}}))
elif args[:1] == ["login"]:
    sys.exit(0 if logged_in else 1)
else:
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        result = {{}}
        if request["method"] == "account/read":
            result = {{"account": {{"email": {secret!r}}}, "requiresOpenaiAuth": True}}
        elif request["method"] == "model/list":
            result = {{"data": [{{"id": {model!r}}}], "nextCursor": None}}
        print(json.dumps({{"id": request["id"], "result": result}}), flush=True)
"""


class _AuthCli:
    def __init__(self, tmp_path: Path, name: str) -> None:
        self.folder = tmp_path / f"fake-{name}"
        self.folder.mkdir()
        source = _FAKE_AUTH_CLI.format(folder=str(self.folder), secret=SECRET, model=CODEX_MODEL)
        self.executable = _fake_cli(self.folder, name, source)
        self.path = str(Path(self.executable).resolve())
        self.pid_file = self.folder / "pids.txt"

    def release(self, *, logged_in: bool = True) -> None:
        if not logged_in:
            (self.folder / "logged_out").touch()
        (self.folder / "release").touch()

    def hold(self) -> None:
        (self.folder / "release").unlink()

    def wait_started(self) -> None:
        deadline = time.monotonic() + PUBLISH_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self.pid_file.exists() and self.pid_file.read_text(encoding="utf-8").strip():
                return
            time.sleep(0.05)
        raise AssertionError("the fake CLI never started")


class _AuthEnv:
    def __init__(self, repo: Path, tmp_path: Path) -> None:
        self.repo = repo
        self.claude = _AuthCli(tmp_path, "claude")
        self.codex = _AuthCli(tmp_path, "codex")
        self.spawned: list[Any] = []
        self.started_by_reader: list[list[str]] = []
        self.reader_may_start_cli = False

    def children(self, module: str) -> list[Any]:
        return [child for child in self.spawned if module in list(child.args)]

    def read(self) -> dict[str, Any]:
        """What one snapshot reads: adapter readiness, then the preflight."""
        from aiworkhub import dashboard

        provider: Any = SimpleNamespace(repo_root=self.repo)
        adapters = dashboard.DashboardProvider.get_adapter_readiness(provider)
        preflight = dashboard.DashboardProvider._nonblocking_preflight(provider)

        def row(rows: list[dict[str, Any]], adapter: str) -> dict[str, Any]:
            return next(item for item in rows if item["adapter_id"] == adapter)

        return {
            "adapters": adapters,
            "preflight": preflight,
            "claude": row(preflight["providers"], "claude_cli"),
            "codex": row(preflight["providers"], "codex_cli"),
            "observed": row(preflight["provider_observability"]["adapters"], "claude_cli"),
            "adapter": row(adapters["adapters"], "claude_cli"),
        }

    def drain_until_landed(self) -> None:
        deadline = time.monotonic() + PUBLISH_WAIT_SECONDS
        dashboard_reader.drain_deferred_work()
        while _polling_jobs():
            assert time.monotonic() < deadline, "the polled auth probe never finished"
            time.sleep(0.05)
            dashboard_reader.drain_deferred_work()

    def probe_files(self) -> list[Path]:
        return [path for path in _runtime_files(self.repo) if "provider_auth_probe" in path.parts]


@pytest.fixture
def auth_env(reader_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Fake Claude and Codex CLIs; every other provider and store is a fake."""
    env = _AuthEnv(reader_repo, tmp_path)
    executables = {"claude_cli": env.claude.executable, "codex_cli": env.codex.executable}

    def resolve(adapter_id: str, *_args: Any) -> Any:
        executable = executables.get(adapter_id)
        # A placeholder, never None: that would look the real CLI up on PATH.
        return runtime_adapters.ExecutableResolution(
            adapter_id,
            executable or str(tmp_path / "not-installed"),
            executable is not None,
            "" if executable else "executable_not_found",
        )

    class GuardedPopen(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
            if not env.reader_may_start_cli and args[0] in (env.claude.path, env.codex.path):
                env.started_by_reader.append(list(args))
                raise OSError("the reader process must never start a provider CLI")
            super().__init__(args, *rest, **kwargs)
            env.spawned.append(self)

    unavailable = {"launchable": False, "blocker_reason": "fake_unavailable"}
    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    monkeypatch.setenv("AIWORKHUB_CLAUDE_AUTH_STATE_FILE", str(tmp_path / "claude-failure.json"))
    monkeypatch.setenv("NF02338_ENV_SECRET", SECRET)
    monkeypatch.setattr(runtime_adapters, "resolve_executable", resolve)
    monkeypatch.setattr(deepseek_credentials, "credential_path", lambda: tmp_path / "no-credential")
    for module in (deepseek_credentials, glm_credentials):
        monkeypatch.setattr(
            module,
            "credential_status",
            lambda repo=None: {**unavailable, "adapter_id": "fake_credentials"},
        )
    monkeypatch.setattr(vscode_lm_bridge, "bridge_readiness", lambda *_a, **_k: dict(unavailable))
    monkeypatch.setattr(kilo_auth, "auth_status", lambda **_k: dict(unavailable))
    monkeypatch.setattr(
        repo_policy.task_store,
        "storage_readiness",
        lambda _root: SimpleNamespace(ready=True, reason="ready", repo_id="repo_test"),
    )
    monkeypatch.setattr(
        repo_policy.task_store, "callback_bridge_health", lambda _root: {"ok": True}
    )
    monkeypatch.setattr(
        repo_policy.source_graph_daemon,
        "daemon_health",
        lambda _root: {
            "ok": True,
            "status": "ready",
            "running": True,
            "readable_generation": True,
            "last_success_at": "2026-11-22T00:00:00+00:00",
            "build_revision": "fake",
            "files_seen": 1,
        },
    )
    backend = repo_policy.WINDOWS_APPCONTAINER_BACKEND if os.name == "nt" else "bubblewrap"
    monkeypatch.setattr(repo_policy.worker_workspace, "select_sandbox_backend", lambda: backend)
    monkeypatch.setattr(
        repo_policy.worker_workspace,
        "finalization_preflight_probe_nonblocking",
        lambda *_a: {"ok": True, "status": "ready"},
    )
    monkeypatch.setattr(
        task_reconciler, "reconciler_health", lambda _root: {"ok": True, "running": True}
    )
    # The parent supplies the child's bounds; a loaded host must not trip them.
    monkeypatch.setattr(claude_auth, "STATUS_TIMEOUT_FLOOR_SECONDS", 60.0)
    monkeypatch.setattr(codex_auth, "LOGIN_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(codex_auth, "APP_SERVER_TIMEOUT_SECONDS", 60.0)
    claude_auth.invalidate()
    codex_auth.invalidate()
    try:
        yield env
        assert env.started_by_reader == []
    finally:
        dashboard_reader.stop_polled_work()
        for cli in (env.claude, env.codex):
            cli.release()
        claude_auth.invalidate()
        codex_auth.invalidate()


def _assert_claude_pending(read: dict[str, Any]) -> None:
    assert read["claude"]["status"] == "probing"
    assert read["claude"]["launchable"] is False
    assert read["claude"]["access_observed"] is False
    assert repo_policy._provider_probe_pending(read["claude"])
    assert read["observed"]["access_observed"] is False
    assert "subscription_auth_state" not in read["observed"]["observable_signals"]
    assert read["adapter"]["status"] == "probing"
    assert read["adapter"]["launchable"] is False
    assert "authentication" not in read["adapter"]["blocker_reason"]
    assert "authentication" not in read["claude"]["reason"]


def test_first_reads_return_pending_without_waiting_on_either_cli(auth_env: Any) -> None:
    threads = set(threading.enumerate())
    first = auth_env.read()
    # Nothing has run yet: the children start on the drain after the reply.
    assert auth_env.children(CLAUDE_MODULE) == auth_env.children(CODEX_MODULE) == []
    _assert_claude_pending(first)
    assert first["codex"]["status"] == "probing"
    assert first["codex"]["launchable"] is False
    assert first["codex"]["access_observed"] is False
    preflight = first["preflight"]
    assert preflight["status"] == "probing"
    assert preflight["ok"] is False
    assert preflight["errors"] == []
    assert preflight["provider_summary"]["coverage_status"] == "probing"

    dashboard_reader.drain_deferred_work()
    # Both CLIs are still blocked, and reads keep answering.
    for _ in range(3):
        _assert_claude_pending(auth_env.read())
        dashboard_reader.drain_deferred_work()
    # One child per probe key across all three Claude call sites.
    assert len(auth_env.children(CLAUDE_MODULE)) == 1
    assert len(auth_env.children(CODEX_MODULE)) == 1
    assert set(threading.enumerate()) == threads


def test_a_pending_result_is_never_memoized(auth_env: Any) -> None:
    read = auth_env.read()
    preflight = read["preflight"]
    for snapshot in (
        {"environment_preflight": preflight},
        {"environment_preflight": {"providers": [read["claude"]]}},
        {"environment_preflight": {"provider_observability": preflight["provider_observability"]}},
        {"adapter_readiness": read["adapters"]},
    ):
        assert not dashboard_mcp_app._cacheable_build(snapshot), list(snapshot)


def test_a_later_drain_publishes_positive_results_through_each_cache(auth_env: Any) -> None:
    auth_env.read()
    dashboard_reader.drain_deferred_work()
    auth_env.claude.release()
    auth_env.codex.release()
    auth_env.drain_until_landed()

    read = auth_env.read()
    assert read["claude"]["status"] == "ready_unverified"
    assert read["claude"]["access_observed"] is True
    assert read["observed"]["access_observed"] is True
    assert "subscription_auth_state" in read["observed"]["observable_signals"]
    assert read["adapter"]["launchable"] is True
    assert read["codex"]["status"] == "ready_unverified"
    assert read["codex"]["observed_models"] == [CODEX_MODEL]
    assert read["codex"]["cache_ttl_seconds"] == codex_auth.POSITIVE_CACHE_TTL_SECONDS
    assert read["preflight"]["status"] != "probing"
    assert dashboard_mcp_app._cacheable_build(
        {"environment_preflight": read["preflight"], "adapter_readiness": read["adapters"]}
    )
    assert auth_env.claude.path in claude_auth._cache
    # The child's elapsed time feeds the parent's latency-derived bounds.
    assert len(claude_auth._probe_latencies[auth_env.claude.path]) == 1
    dashboard_reader.drain_deferred_work()
    assert len(auth_env.children(CLAUDE_MODULE)) == 1
    assert len(auth_env.children(CODEX_MODULE)) == 1
    assert auth_env.probe_files() == []


def test_negatives_are_published_and_follow_each_modules_rules(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_env.read()
    auth_env.claude.release(logged_in=False)
    auth_env.codex.release(logged_in=False)
    auth_env.drain_until_landed()

    auth_env.claude.hold()
    auth_env.codex.hold()
    read = auth_env.read()
    # The finished negative reaches every call site of that read.
    assert read["claude"]["status"] == "access_unavailable"
    assert read["claude"]["reason"] == "claude_authentication_required"
    assert read["adapter"]["blocker_reason"] == "claude_authentication_required"
    assert "subscription_auth_state" in read["observed"]["observable_signals"]
    assert read["codex"]["reason"] == "codex_authentication_required"
    assert read["codex"]["cache_ttl_seconds"] == codex_auth.NEGATIVE_CACHE_TTL_SECONDS
    assert not repo_policy._provider_probe_pending(read["claude"])
    assert auth_env.claude.path not in claude_auth._cache

    # Claude negatives are never cached: that read asked for one new probe.
    dashboard_reader.drain_deferred_work()
    _assert_claude_pending(auth_env.read())
    assert len(auth_env.children(CLAUDE_MODULE)) == 2
    assert len(auth_env.children(CODEX_MODULE)) == 1

    # A Codex negative is served for 30 s, and then probed again.
    for key, (stamp, ttl, value) in list(codex_auth._cache.items()):
        monkeypatch.setitem(codex_auth._cache, key, (stamp - ttl - 1, ttl, value))
    assert auth_env.read()["codex"]["status"] == "probing"


def test_a_result_collected_after_its_refresh_window_is_still_published(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_env.read()
    auth_env.claude.release()
    auth_env.codex.release()
    auth_env.drain_until_landed()
    monkeypatch.setattr(claude_auth, "CACHE_REFRESH_SECONDS", 0.0)
    assert auth_env.read()["claude"]["status"] == "ready_unverified"


@pytest.mark.parametrize("cached", [False, True], ids=["transient", "transient_grace"])
def test_timeouts_publish_transient_and_keep_the_grace(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch, cached: bool
) -> None:
    monkeypatch.setattr(claude_auth, "STATUS_TIMEOUT_FLOOR_SECONDS", 0.5)
    auth_env.codex.release()
    if cached:
        positive = {"ok": True, "authenticated": True, "launchable": True, "status": "ready"}
        observed = time.monotonic() - claude_auth.CACHE_REFRESH_SECONDS - 5
        claude_auth._cache[auth_env.claude.path] = (observed, positive)
    _assert_claude_pending(auth_env.read())
    auth_env.drain_until_landed()

    read = auth_env.read()
    assert not repo_policy._provider_probe_pending(read["claude"])
    if cached:
        assert read["claude"]["status"] == "ready_unverified"
        assert claude_auth._cache[auth_env.claude.path][0] == observed
    else:
        assert read["claude"]["launchable"] is False
        assert read["claude"]["reason"] == "claude_auth_status_transient:TimeoutExpired"


def test_the_parent_supplies_the_latency_derived_bounds(auth_env: Any) -> None:
    claude_auth._record_probe_latency(auth_env.claude.path, 40.0)
    expected = claude_auth.status_probe_timeouts(auth_env.claude.path)
    auth_env.read()
    dashboard_reader.drain_deferred_work()
    argv = list(auth_env.children(CLAUDE_MODULE)[0].args)
    assert argv[-2:] == [repr(bound) for bound in expected]


def test_a_child_past_its_deadline_is_killed_and_published(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both bounds total 120 s; this puts the parent's own deadline at 3 s.
    monkeypatch.setattr(repo_policy, "_AUTH_PROBE_CHILD_GRACE_SECONDS", -117.0)
    monkeypatch.setattr(claude_auth, "status_probe_timeouts", lambda _path: (60.0, 60.0))
    auth_env.read()
    auth_env.drain_until_landed()
    read = auth_env.read()
    assert read["claude"]["reason"] == "claude_auth_status_transient:TimeoutExpired"
    assert read["codex"]["reason"] == "codex_capability_probe_timeout"
    for cli in (auth_env.claude, auth_env.codex):
        _assert_pids_gone(cli.pid_file)
    assert auth_env.probe_files() == []


def test_a_crashed_child_is_published_as_a_failure(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", str(auth_env.repo / "no-such-python"))
    auth_env.read()
    dashboard_reader.drain_deferred_work()
    read = auth_env.read()
    assert read["claude"]["status"] == read["codex"]["status"] == "access_unavailable"
    assert read["adapter"]["launchable"] is False


def test_the_child_writes_only_the_sanitized_result(auth_env: Any) -> None:
    auth_env.read()
    auth_env.claude.release()
    auth_env.codex.release()
    dashboard_reader.drain_deferred_work()
    for child in auth_env.children(CLAUDE_MODULE) + auth_env.children(CODEX_MODULE):
        assert child.wait(timeout=PUBLISH_WAIT_SECONDS) == 0
    files = auth_env.probe_files()
    assert len(files) == 2
    written = b"".join(path.read_bytes() for path in files)
    assert b"launchable" in written
    assert SECRET.encode() not in written
    auth_env.drain_until_landed()
    assert SECRET not in json.dumps(auth_env.read(), default=str)


def test_stop_kills_both_child_trees_and_publishes_nothing(auth_env: Any) -> None:
    auth_env.read()
    dashboard_reader.drain_deferred_work()
    auth_env.claude.wait_started()
    auth_env.codex.wait_started()
    assert dashboard_reader.stop_polled_work() == 2
    for cli in (auth_env.claude, auth_env.codex):
        _assert_pids_gone(cli.pid_file)
    assert auth_env.probe_files() == []
    assert claude_auth._cache == {}
    assert codex_auth._cache == {}
    # Nothing was published, and the keys are free to probe again.
    _assert_claude_pending(auth_env.read())
    dashboard_reader.drain_deferred_work()
    assert len(auth_env.children(CLAUDE_MODULE)) == 2


@pytest.mark.parametrize("denial", ["policy_denied", "repository_model_policy_disabled"])
def test_policy_keeps_precedence_over_pending(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch, denial: str
) -> None:
    native = ("claude_cli", "codex_cli")
    if denial == "policy_denied":
        policy = json.loads(json.dumps(repo_policy.load_policy(auth_env.repo)))
        policy["providers"]["allowed_adapters"] = [
            name for name in policy["providers"]["allowed_adapters"] if name not in native
        ]
        monkeypatch.setattr(repo_policy, "load_policy", lambda _root: policy)
    else:
        disabled = [repo_policy.model_settings.policy_identity_for_adapter(name) for name in native]
        monkeypatch.setattr(
            repo_policy.model_settings,
            "evaluate_state",
            lambda _policy, *, provider, adapter, model=None: (provider, adapter) not in disabled,
        )
    read = auth_env.read()
    for name in ("claude", "codex"):
        assert read[name]["status"] == denial
        assert not repo_policy._provider_probe_pending(read[name])
    assert read["preflight"]["status"] != "probing"
    assert "provider_models_probe_pending" not in read["preflight"]["warnings"]


def test_a_full_deferred_queue_still_reads_probing_and_retries(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patched:
        patched.setattr(dashboard_reader, "MAX_DEFERRED_QUEUE", 0)
        _assert_claude_pending(auth_env.read())
    auth_env.read()
    dashboard_reader.drain_deferred_work()
    assert len(auth_env.children(CLAUDE_MODULE)) == 1


def test_patched_callables_keep_the_blocking_path(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = {"launchable": True, "authenticated": True}
    monkeypatch.setattr(claude_auth, "auth_status", lambda executable: dict(ready))
    monkeypatch.setattr(codex_auth, "capability_status", lambda executable: dict(ready))
    read = auth_env.read()
    assert read["claude"]["launchable"] is read["codex"]["launchable"] is True
    assert read["adapter"]["launchable"] is True
    assert auth_env.children(CLAUDE_MODULE) == auth_env.children(CODEX_MODULE) == []


@pytest.mark.parametrize("caller", ["outside_the_scope", "without_the_fence"])
def test_unfenced_callers_stay_blocking(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    auth_env.reader_may_start_cli = True
    auth_env.claude.release()
    auth_env.codex.release()
    if caller == "without_the_fence":
        monkeypatch.setattr(repo_policy, "_POLLED_PROBE_THREAD_CLASS", None)
        read = auth_env.read()
        assert read["claude"]["launchable"] is read["codex"]["launchable"] is True
        assert read["adapter"]["launchable"] is True
    else:
        report = repo_policy.build_preflight(auth_env.repo)
        rows = {row["adapter_id"]: row for row in report["providers"]}
        assert rows["claude_cli"]["launchable"] is rows["codex_cli"]["launchable"] is True
        adapters = deepseek_credentials.adapter_readiness(repo=auth_env.repo)["adapters"]
        assert next(row for row in adapters if row["adapter_id"] == "claude_cli")["launchable"]
    assert auth_env.children(CLAUDE_MODULE) == auth_env.children(CODEX_MODULE) == []


def _advance_clock(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + seconds)


@pytest.mark.parametrize("executable_replaced", [False, True])
@pytest.mark.parametrize("recorded_by", ["this_process", "another_process"])
def test_a_late_positive_never_erases_a_newer_runtime_failure(
    auth_env: Any, recorded_by: str, executable_replaced: bool
) -> None:
    path = auth_env.claude.path
    auth_env.read()
    dashboard_reader.drain_deferred_work()
    auth_env.claude.wait_started()
    assert claude_auth.record_runtime_auth_failure(path, session_id="session-nf02338")
    if recorded_by == "another_process":
        # Only the persisted circuit reaches this process.
        claude_auth._runtime_failures.pop(path)
    auth_env.claude.release()
    auth_env.codex.release()
    moved = Path(f"{path}.updating")
    if executable_replaced:
        # An update swaps the binary: for a moment its path does not resolve.
        auth_env.children(CLAUDE_MODULE)[0].wait(timeout=PUBLISH_WAIT_SECONDS)
        Path(path).rename(moved)
    auth_env.drain_until_landed()
    if executable_replaced:
        moved.rename(path)

    assert (path in claude_auth._runtime_failures) is (recorded_by == "this_process")
    assert claude_auth._persisted_runtime_failure(path) is not None
    assert path not in claude_auth._cache
    read = auth_env.read()
    assert read["claude"]["launchable"] is False
    assert read["claude"]["reason"] == claude_auth.RUNTIME_AUTH_FAILURE_REASON
    assert read["adapter"]["launchable"] is False


# 156 s: how late the refresh cadence plus the build memo can first show a result.
@pytest.mark.parametrize("unread_seconds", [156.0, 301.0], ids=["within", "past"])
def test_a_finished_negative_is_shown_only_within_its_bound(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch, unread_seconds: float
) -> None:
    auth_env.read()
    auth_env.claude.release(logged_in=False)
    auth_env.codex.release(logged_in=False)
    auth_env.drain_until_landed()
    auth_env.claude.hold()
    auth_env.codex.hold()
    _advance_clock(monkeypatch, unread_seconds)

    read = auth_env.read()
    for name in ("claude", "codex"):
        row = read[name]
        if unread_seconds > 300:
            assert row["status"] == "probing"
            assert not dashboard_mcp_app._cacheable_build(
                {"environment_preflight": {"providers": [row]}}
            )
        else:
            # Also after Codex's 30 s negative TTL.
            assert row["reason"] == f"{name}_authentication_required"
            assert not repo_policy._provider_probe_pending(row)
    dashboard_reader.drain_deferred_work()
    again = auth_env.read()
    assert again["claude"]["status"] == again["codex"]["status"] == "probing"


@pytest.mark.parametrize("ending", ["recorded_failure", "expired"])
def test_a_valid_positive_is_served_while_it_is_refreshed(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    auth_env.read()
    auth_env.claude.release()
    auth_env.codex.release()
    auth_env.drain_until_landed()
    assert auth_env.read()["claude"]["launchable"] is True
    monkeypatch.setattr(claude_auth, "CACHE_REFRESH_SECONDS", 0.0)
    auth_env.claude.hold()

    # The first read precedes the refresh child, the others run beside it.
    for _ in range(3):
        read = auth_env.read()
        assert read["claude"]["launchable"] is True
        assert read["adapter"]["launchable"] is True
        assert not repo_policy._provider_probe_pending(read["claude"])
        assert dashboard_mcp_app._cacheable_build(
            {"environment_preflight": read["preflight"], "adapter_readiness": read["adapters"]}
        )
        dashboard_reader.drain_deferred_work()
        assert len(auth_env.children(CLAUDE_MODULE)) == 2

    if ending == "expired":
        _advance_clock(monkeypatch, claude_auth.CACHE_TTL_SECONDS + 1)
        _assert_claude_pending(auth_env.read())
    else:
        assert claude_auth.record_runtime_auth_failure(
            auth_env.claude.path, session_id="session-nf02338"
        )
        read = auth_env.read()
        assert read["claude"]["launchable"] is False
        assert read["claude"]["reason"] == claude_auth.RUNTIME_AUTH_FAILURE_REASON


@pytest.mark.parametrize("stopped_by", ["cancel", "nonzero_exit"])
@pytest.mark.parametrize(
    ("windows", "group_alive", "signalled"),
    [(False, True, True), (False, False, False), (True, True, False)],
    ids=["posix_live_group", "posix_empty_group", "windows_exited_root"],
)
def test_an_exited_roots_surviving_group_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stopped_by: str,
    windows: bool,
    group_alive: bool,
    signalled: bool,
) -> None:
    pid = 424242
    live_groups = {pid} if group_alive else set()
    terminated: list[int] = []

    def terminate(identity: int, **_kwargs: Any) -> bool:
        terminated.append(identity)
        live_groups.discard(identity)
        return True

    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: windows)
    monkeypatch.setattr(platform_io, "probe_process_group", lambda pgid: pgid in live_groups)
    monkeypatch.setattr(platform_io, "terminate_process_tree", terminate)
    key = (CLAUDE_MODULE, "fake-exited-root")
    failures: list[str | None] = []

    def publish(_payload: object, failure: str | None, _started: float) -> Any:
        failures.append(failure)
        return {"launchable": False}, 0.0

    job = repo_policy._PolledAuthProbe(SimpleNamespace(publish=publish), key, tmp_path)
    output = tmp_path / "auth-probe-fake.out"
    output.write_bytes(b"")
    exited: Any = SimpleNamespace(
        pid=pid,
        poll=lambda: 3,
        wait=lambda timeout=None: 3,
        kill=lambda: pytest.fail("an exited root must not be killed"),
    )
    job._process, job._outputs = exited, (output,)
    monkeypatch.setattr(repo_policy, "_AUTH_PROBE_JOBS", {key: job})
    monkeypatch.setattr(repo_policy, "_AUTH_PROBE_RESULTS", {})

    if stopped_by == "cancel":
        job.cancel()
        assert failures == []
    else:
        assert job.poll() is True
        assert failures == ["exit_3"]
    assert terminated == ([pid] if signalled else [])
    assert not output.exists()
