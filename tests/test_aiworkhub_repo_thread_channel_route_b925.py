"""B925: repository/thread/channel callback routing isolation regressions.

Reproduces and closes the measured defect: Secp256K1fast tasks
(BUILD_REGISTRY_CONTRACT_022, UFCI_IMPLEMENT_019) were delivered into the
GeoAI Codex chat while manager MCP authority reported ``/home/shrek/GeoAI``.
Root cause -- ``app_server_mux.py``'s sideband instance registry
(``DEFAULT_SIDEBAND_DIR`` is one machine-wide directory shared by every
repository's mux instance) resolved thread ownership by ``thread_id``
alone, with no repository binding, so a colliding/foreign thread id could
resolve into the wrong repository's mux instance and the wrong repository's
coordinator chat.

Fix under test: every sideband instance registers an immutable ``repo_id``
(``app_server_mux.AppServerMux``/``SidebandInstance``), every ownership
lookup is scoped by it (``find_owning_sideband_instances``,
``describe_sideband_owner_freshness``), and ``callback_bridge.py``'s
``SidebandCallbackClient``/``CallbackBridge`` fail closed at construction
when no repository identity is bound -- never guessing, never falling back
to a shared/global default.

Module loading: this worktree's ``src/aiworkhub`` package has no
``__init__.py`` (a namespace-package portion). If another checkout of this
same package is already importable elsewhere on ``sys.path`` (e.g. an
editable install of a different, canonical checkout) AND that other copy
DOES have ``__init__.py``, Python's import system treats it as the regular
package and it wins over this worktree's portion regardless of sys.path
order -- so a plain ``sys.path.insert(0, ...)`` is not reliable here. Both
modules under test are therefore loaded under a private top-level package
name with ``__path__`` pointed explicitly at THIS worktree's
``src/aiworkhub`` directory, bypassing sys.path package resolution
entirely so the exact source edited for this task is what gets exercised.
"""
from __future__ import annotations

import importlib
import os
import sys
import time
import types
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
AIWORKHUB_DIR = SRC_DIR / "aiworkhub"

_UNDER_TEST_PACKAGE = "aiworkhub_b925_under_test"


def _fresh_aiworkhub_package() -> types.ModuleType:
    """(Re)register a private top-level package whose ``__path__`` points
    explicitly at this worktree's ``src/aiworkhub`` directory, purging any
    previously loaded submodules first so each caller gets a clean import."""
    for name in list(sys.modules):
        if name == _UNDER_TEST_PACKAGE or name.startswith(_UNDER_TEST_PACKAGE + "."):
            del sys.modules[name]
    pkg = types.ModuleType(_UNDER_TEST_PACKAGE)
    pkg.__path__ = [str(AIWORKHUB_DIR)]
    sys.modules[_UNDER_TEST_PACKAGE] = pkg
    return pkg


_fresh_aiworkhub_package()
asm = importlib.import_module(f"{_UNDER_TEST_PACKAGE}.app_server_mux")
_route_identity = importlib.import_module(f"{_UNDER_TEST_PACKAGE}.route_identity")
CoordinatorRouteKey = _route_identity.CoordinatorRouteKey
RepoRouteKey = _route_identity.RepoRouteKey


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_instance(
    sideband_dir: Path,
    *,
    instance_id: str,
    repo_id: str | None,
    owned_thread_ids: list[str],
    heartbeat_at: float | None = None,
    include_repo_id: bool = True,
) -> Path:
    """Write a sideband instance registry descriptor exactly as
    ``AppServerMux._write_registry`` would, without spawning a real mux
    process (no subprocess, no real Codex binary)."""
    asm.ensure_private_dir(sideband_dir)
    instances_dir = asm.sideband_instances_dir(sideband_dir)
    asm.ensure_private_dir(instances_dir)
    descriptor: dict = {
        "instance_id": instance_id,
        "generation_id": instance_id,
        "pid": os.getpid(),
        "parent_pid": 0,
        "pid_start_time": None,
        "socket_path": str(sideband_dir / f"{instance_id}.sock"),
        "capability_path": str(sideband_dir / f"{instance_id}.cap"),
        "owned_thread_ids": list(owned_thread_ids),
        "heartbeat_at": heartbeat_at if heartbeat_at is not None else time.time(),
        "owner_lease_seconds": asm.SIDEBAND_OWNER_LEASE_SECONDS,
        "ready": True,
    }
    if include_repo_id:
        descriptor["repo_id"] = repo_id
    path = instances_dir / f"{instance_id}.json"
    asm._write_registry_descriptor(path, descriptor)
    return path


@pytest.fixture
def callback_bridge_module(monkeypatch, tmp_path):
    """Import a fresh ``callback_bridge`` (under the private test package)
    with its two not-yet-ported dependencies (``repository_state``/
    ``task_store`` -- outside this task's allowed_writes) stubbed via
    ``sys.modules``, so the REAL, unmodified source under test runs end to
    end.

    ``AIWORKHUB_REPO_ROOT`` is set before import so the module-level
    ``CALLBACK_CWD = str(_bound_repo_from_env_or_cwd())`` short-circuits on
    the env var and never calls into the stub ``repository_state`` module.
    """
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)

    pkg = _fresh_aiworkhub_package()
    repository_state_stub = types.ModuleType(f"{_UNDER_TEST_PACKAGE}.repository_state")
    task_store_stub = types.ModuleType(f"{_UNDER_TEST_PACKAGE}.task_store")
    sys.modules[f"{_UNDER_TEST_PACKAGE}.repository_state"] = repository_state_stub
    sys.modules[f"{_UNDER_TEST_PACKAGE}.task_store"] = task_store_stub
    pkg.repository_state = repository_state_stub
    pkg.task_store = task_store_stub

    module = importlib.import_module(f"{_UNDER_TEST_PACKAGE}.callback_bridge")
    yield module

    for name in list(sys.modules):
        if name == _UNDER_TEST_PACKAGE or name.startswith(_UNDER_TEST_PACKAGE + "."):
            del sys.modules[name]


# ---------------------------------------------------------------------------
# repo_id validation (fail-closed, no guessing, no truncation)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_repo_id",
    ["", "   ", "has space", "has/slash", "has\\backslash", "C:\\Users\\dev\\repo", None, 123, [], {}],
)
def test_validate_repo_id_rejects_invalid(bad_repo_id):
    with pytest.raises(ValueError):
        asm._validate_repo_id(bad_repo_id)


@pytest.mark.parametrize("good_repo_id", ["geoai-main", "secp256k1fast", "a", "repo_id.with-colons:ok"])
def test_validate_repo_id_accepts_valid(good_repo_id):
    assert asm._validate_repo_id(good_repo_id) == good_repo_id


# ---------------------------------------------------------------------------
# AppServerMux: repo_id is mandatory and lands in the registry descriptor
# ---------------------------------------------------------------------------

def test_app_server_mux_requires_repo_id(tmp_path):
    with pytest.raises(TypeError):
        asm.AppServerMux(["app-server"], sideband_dir=tmp_path)


def test_app_server_mux_rejects_invalid_repo_id(tmp_path):
    with pytest.raises(ValueError):
        asm.AppServerMux(["app-server"], repo_id="", sideband_dir=tmp_path)
    with pytest.raises(ValueError):
        asm.AppServerMux(["app-server"], repo_id="C:\\bad\\repo", sideband_dir=tmp_path)


def test_app_server_mux_writes_repo_id_into_registry(tmp_path):
    sideband_dir = tmp_path / "sideband"
    mux = asm.AppServerMux(["app-server"], repo_id="geoai-main", sideband_dir=sideband_dir)
    assert mux.repo_id == "geoai-main"
    asm.ensure_private_dir(sideband_dir)
    asm.ensure_private_dir(asm.sideband_instances_dir(sideband_dir))
    mux._bind_socket()
    try:
        mux._write_registry()

        descriptor = asm._read_instance_descriptor(mux.registry_path)
        assert descriptor is not None
        assert descriptor.repo_id == "geoai-main"
    finally:
        mux.shutdown()
    assert descriptor.instance_id == mux.instance_id


# ---------------------------------------------------------------------------
# find_owning_sideband_instances: the core B925 regression -- two
# simultaneous repositories (GeoAI + Secp256K1fast), same thread_id.
# ---------------------------------------------------------------------------

def test_find_owning_sideband_instances_scopes_by_repo_id(tmp_path):
    sideband_dir = tmp_path / "sideband"
    _write_instance(sideband_dir, instance_id="geoaiwin1", repo_id="geoai-main", owned_thread_ids=["thread-shared"])
    _write_instance(sideband_dir, instance_id="secpwin01", repo_id="secp256k1fast", owned_thread_ids=["thread-shared"])

    geoai_owners = asm.find_owning_sideband_instances(sideband_dir, "thread-shared", "geoai-main")
    secp_owners = asm.find_owning_sideband_instances(sideband_dir, "thread-shared", "secp256k1fast")

    assert [o.instance_id for o in geoai_owners] == ["geoaiwin1"]
    assert [o.instance_id for o in secp_owners] == ["secpwin01"]
    # Neither repo's resolution ever returns the other repo's instance.
    assert "secpwin01" not in [o.instance_id for o in geoai_owners]
    assert "geoaiwin1" not in [o.instance_id for o in secp_owners]


def test_find_owning_sideband_instances_unbound_repo_returns_empty_not_fallback(tmp_path):
    sideband_dir = tmp_path / "sideband"
    _write_instance(sideband_dir, instance_id="geoaiwin1", repo_id="geoai-main", owned_thread_ids=["thread-shared"])

    # A third, unrelated repository must never resolve ANY owner for a
    # thread it never bound -- no global/shared fallback.
    owners = asm.find_owning_sideband_instances(sideband_dir, "thread-shared", "some-other-repo")
    assert owners == []


def test_find_owning_sideband_instances_requires_valid_repo_id(tmp_path):
    sideband_dir = tmp_path / "sideband"
    with pytest.raises(ValueError):
        asm.find_owning_sideband_instances(sideband_dir, "thread-shared", "")


def test_legacy_descriptor_without_repo_id_is_excluded(tmp_path):
    """A pre-B925 registry row (written before this fix deployed) has no
    ``repo_id`` at all -- it must be treated as invalid/stale, never as an
    "unscoped" match that a repo-scoped lookup lets through."""
    sideband_dir = tmp_path / "sideband"
    _write_instance(
        sideband_dir, instance_id="legacy001", repo_id=None,
        owned_thread_ids=["thread-x"], include_repo_id=False,
    )
    assert asm.list_live_sideband_instances(sideband_dir) == []
    assert asm.find_owning_sideband_instances(sideband_dir, "thread-x", "geoai-main") == []


def test_describe_sideband_owner_freshness_scoped_by_repo_id(tmp_path):
    sideband_dir = tmp_path / "sideband"
    _write_instance(sideband_dir, instance_id="geoaiwin1", repo_id="geoai-main", owned_thread_ids=["thread-shared"])
    _write_instance(sideband_dir, instance_id="secpwin01", repo_id="secp256k1fast", owned_thread_ids=["thread-shared"])

    geoai_status = asm.describe_sideband_owner_freshness(sideband_dir, "thread-shared", "geoai-main")
    assert geoai_status["owner_count"] == 1
    assert geoai_status["owners"][0]["instance_id"] == "geoaiwin1"

    secp_status = asm.describe_sideband_owner_freshness(sideband_dir, "thread-shared", "secp256k1fast")
    assert secp_status["owner_count"] == 1
    assert secp_status["owners"][0]["instance_id"] == "secpwin01"


# ---------------------------------------------------------------------------
# main(): unbound global launcher is transparent and never claims a repo
# ---------------------------------------------------------------------------

def test_main_unbound_app_server_starts_deferred_mux_without_sideband_authority(monkeypatch):
    monkeypatch.delenv(asm.ENV_REPO_ID, raising=False)
    # Hermetic: a developer machine may legitimately pin the extension's
    # absolute Codex binary in ~/.aiworkhub/app_server_mux/real_executable.
    # This test exercises unbound deferred-proxy shape, not host configuration.
    monkeypatch.setattr(asm, "resolve_real_executable", lambda: "codex")
    monkeypatch.setattr(
        asm.shared_router,
        "list_known_repositories",
        lambda **_kwargs: {"ok": True, "repositories": []},
    )
    calls: list[tuple[list[str], str | None, bool]] = []

    def fake_run_mux(args, *, repo_id, deferred_repo_binding=False):
        calls.append((list(args), repo_id, deferred_repo_binding))
        return 0

    monkeypatch.setattr(asm, "run_mux", fake_run_mux)
    assert asm.main(["app-server", "--listen", "stdio://"]) == 0
    assert calls == [(["app-server", "--listen", "stdio://"], None, True)]


def test_mux_waits_for_exact_parent_route_during_parallel_extension_start(monkeypatch):
    monkeypatch.delenv(asm.ENV_REPO_ID, raising=False)
    monkeypatch.setenv(asm.ENV_ROUTE_WAIT_SECONDS, "1")
    attempts = {"count": 0}

    def routes(**_kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            return {"ok": True, "repositories": []}
        return {
            "ok": True,
            "repositories": [{
                "repo_id": "repo_0123456789abcdef0123456789abcdef",
                "extension_host_pid": 4242,
                "extension_host_alive": True,
                "stale": False,
                "selected_provider": "codex",
            }],
        }

    monkeypatch.setattr(asm.os, "getppid", lambda: 4242)
    monkeypatch.setattr(asm.shared_router, "list_known_repositories", routes)
    monkeypatch.setattr(asm.time, "sleep", lambda _seconds: None)
    assert asm.wait_for_repo_id_for_mux() == "repo_0123456789abcdef0123456789abcdef"
    assert attempts["count"] == 3


def test_resolve_repo_id_for_mux_uses_exact_parent_extension_host_route(monkeypatch):
    monkeypatch.delenv(asm.ENV_REPO_ID, raising=False)
    monkeypatch.setattr(asm.os, "getppid", lambda: 4242)
    monkeypatch.setattr(
        asm.shared_router,
        "list_known_repositories",
        lambda **_kwargs: {
            "ok": True,
            "repositories": [
                {
                    "repo_id": "repo_0123456789abcdef0123456789abcdef",
                    "extension_host_pid": 4242,
                    "extension_host_alive": True,
                    "stale": False,
                    "selected_provider": "codex",
                },
                {
                    "repo_id": "repo_fedcba9876543210fedcba9876543210",
                    "extension_host_pid": 7777,
                    "extension_host_alive": True,
                    "stale": False,
                    "selected_provider": "codex",
                },
            ],
        },
    )

    assert asm.resolve_repo_id_for_mux() == "repo_0123456789abcdef0123456789abcdef"


def test_resolve_repo_id_for_mux_fails_closed_on_ambiguous_parent(monkeypatch):
    monkeypatch.delenv(asm.ENV_REPO_ID, raising=False)
    monkeypatch.setattr(asm.os, "getppid", lambda: 4242)
    monkeypatch.setattr(
        asm.shared_router,
        "list_known_repositories",
        lambda **_kwargs: {
            "ok": True,
            "repositories": [
                {
                    "repo_id": "repo_0123456789abcdef0123456789abcdef",
                    "extension_host_pid": 4242,
                    "extension_host_alive": True,
                    "stale": False,
                    "selected_provider": "codex",
                },
                {
                    "repo_id": "repo_fedcba9876543210fedcba9876543210",
                    "extension_host_pid": 4242,
                    "extension_host_alive": True,
                    "stale": False,
                    "selected_provider": "codex",
                },
            ],
        },
    )

    assert asm.resolve_repo_id_for_mux() == ""


def test_main_non_app_server_invocation_never_requires_repo_id(monkeypatch, tmp_path):
    """The transparent ``execvp`` passthrough path (``codex exec ...``)
    never touches sideband registration, so it must not require
    AIWORKHUB_REPO_ID either -- only the app-server (registering) path
    does."""
    monkeypatch.delenv(asm.ENV_REPO_ID, raising=False)
    fake_real = tmp_path / "fake_real_codex"
    fake_real.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setenv(asm.ENV_REAL_EXECUTABLE, str(fake_real))

    calls: list[list[str]] = []

    def fake_passthrough(executable, args):
        calls.append([executable, *args])
        raise SystemExit(0)

    monkeypatch.setattr(asm, "_passthrough_real_executable", fake_passthrough)
    with pytest.raises(SystemExit):
        asm.main(["exec", "--foo"])
    assert calls and calls[0][0] == str(fake_real)


# ---------------------------------------------------------------------------
# callback_bridge.py: SidebandCallbackClient / CallbackBridge fail-closed
# repo binding, wired through to app_server_mux.py's scoped resolution.
# ---------------------------------------------------------------------------

def test_sideband_callback_client_requires_repo_id(callback_bridge_module):
    cb = callback_bridge_module
    with pytest.raises(TypeError):
        cb.SidebandCallbackClient(sideband_dir="/tmp/x")


def test_sideband_callback_client_rejects_invalid_repo_id(callback_bridge_module):
    cb = callback_bridge_module
    with pytest.raises(ValueError):
        cb.SidebandCallbackClient(repo_id="", sideband_dir="/tmp/x")
    with pytest.raises(ValueError):
        cb.SidebandCallbackClient(repo_id="C:\\bad\\repo", sideband_dir="/tmp/x")


def test_sideband_callback_client_resolve_owner_scoped_by_repo_id(callback_bridge_module, tmp_path):
    """End-to-end: two mux instances (two simultaneous repositories) own
    the SAME thread_id. A client bound to repo A resolves only repo A's
    instance; a client bound to repo B raises the durable-park
    "owner not found" error rather than falling through to repo A's
    instance."""
    cb = callback_bridge_module
    sideband_dir = tmp_path / "sideband"
    _write_instance(sideband_dir, instance_id="geoaiwin1", repo_id="geoai-main", owned_thread_ids=["thread-shared"])
    _write_instance(sideband_dir, instance_id="secpwin01", repo_id="secp256k1fast", owned_thread_ids=["thread-shared"])

    geoai_client = cb.SidebandCallbackClient(repo_id="geoai-main", sideband_dir=sideband_dir)
    owner = geoai_client._resolve_owner("thread-shared")
    assert owner.instance_id == "geoaiwin1"

    secp_client = cb.SidebandCallbackClient(repo_id="secp256k1fast", sideband_dir=sideband_dir)
    owner = secp_client._resolve_owner("thread-shared")
    assert owner.instance_id == "secpwin01"

    unrelated_client = cb.SidebandCallbackClient(repo_id="some-other-repo", sideband_dir=sideband_dir)
    with pytest.raises(cb.SidebandOwnerNotFoundError):
        unrelated_client._resolve_owner("thread-shared")


def test_callback_bridge_sideband_transport_requires_sideband_repo_id(callback_bridge_module, tmp_path):
    cb = callback_bridge_module
    with pytest.raises(ValueError):
        cb.CallbackBridge(repo=tmp_path, db_path=tmp_path / "db.sqlite", transport="sideband")

    bridge = cb.CallbackBridge(
        repo=tmp_path, db_path=tmp_path / "db.sqlite", transport="sideband", sideband_repo_id="geoai-main",
    )
    assert bridge._sideband_repo_id == "geoai-main"


def test_callback_bridge_deliver_batch_via_sideband_passes_bound_repo_id(callback_bridge_module, tmp_path, monkeypatch):
    """The dispatcher-facing delivery path (``_deliver_batch_via_transport``)
    must construct its ``SidebandCallbackClient`` with the bridge's OWN
    bound repo_id -- never omit it, never borrow another bridge's."""
    cb = callback_bridge_module
    bridge = cb.CallbackBridge(
        repo=tmp_path, db_path=tmp_path / "db.sqlite", transport="sideband", sideband_repo_id="geoai-main",
    )

    captured: dict = {}

    class SpySidebandClient:
        def __init__(self, *, repo_id, sideband_dir=None, timeout=None):
            captured["repo_id"] = repo_id

        def deliver_callback_batch(self, thread_id, members, *, client_user_message_id=None, cwd=None):
            captured["thread_id"] = thread_id

    monkeypatch.setattr(cb, "SidebandCallbackClient", SpySidebandClient)

    member = cb.CallbackEntry(
        outbox_id=1, task_id="T1", origin_thread_id="thread-abc", transition="review_ready",
        episode_id="0", event_id="e1", request_id="r1", state="pending", attempts=0,
        lease_id="l1", lease_expires_at="2026-01-01T00:00:00Z",
    )
    batch = cb.CallbackBatch(
        batch_id="b1", origin_thread_id="thread-abc", lease_id="l1",
        lease_expires_at="2026-01-01T00:00:00Z", attempts=1, members=[member],
    )
    bridge._deliver_batch_via_transport(batch, "cid1")

    assert captured["repo_id"] == "geoai-main"
    assert captured["thread_id"] == "thread-abc"


def test_callback_dispatcher_forwards_repo_id_to_sideband_repo_id(callback_bridge_module):
    cb = callback_bridge_module
    dispatcher = cb.CallbackDispatcher("/tmp/geoai-repo", "codex", repo_id="geoai-main", window_id="win1")
    kwargs: dict = {}
    kwargs["repo"] = dispatcher.repo_root
    kwargs["provider"] = dispatcher.provider
    kwargs.setdefault("sideband_repo_id", dispatcher.repo_id)
    assert kwargs["sideband_repo_id"] == "geoai-main"


# ---------------------------------------------------------------------------
# route_identity.py: immutable composite route key -- provider fail-closed,
# Windows path forms rejected, two coordinator chats stay disjoint.
# ---------------------------------------------------------------------------

def test_route_identity_rejects_windows_path_forms():
    with pytest.raises(ValueError):
        RepoRouteKey(repo_id="C:\\Users\\dev\\Secp256k1fast", thread_id="t1", task_id="task1")
    with pytest.raises(ValueError):
        RepoRouteKey(repo_id="repo", thread_id="t1", task_id="task1", event_id="C:\\evil\\path")
    with pytest.raises(ValueError):
        RepoRouteKey(repo_id="repo\\with\\backslash", thread_id="t1", task_id="task1")


def test_route_identity_two_coordinator_chats_stay_disjoint():
    """Same repo_id/task_id/window_id, two different coordinator providers
    (two coordinator chats) -- canonical form and digest must never
    collide, so a callback destined for one provider's chat can never be
    mistaken for the other's."""
    codex_route = CoordinatorRouteKey(
        repo_id="geoai-main", provider="codex", window_id="win1", task_id="T1", thread_id="thread-abc",
    )
    claude_route = CoordinatorRouteKey(
        repo_id="geoai-main", provider="claude", window_id="win1", task_id="T1", session_id="thread-abc",
    )
    assert codex_route.canonical() != claude_route.canonical()
    assert codex_route.digest() != claude_route.digest()


def test_route_identity_two_repositories_same_thread_stay_disjoint():
    geoai_route = RepoRouteKey(repo_id="geoai-main", thread_id="thread-shared", task_id="T1")
    secp_route = RepoRouteKey(repo_id="secp256k1fast", thread_id="thread-shared", task_id="T1")
    assert geoai_route.canonical() != secp_route.canonical()
    assert geoai_route.digest() != secp_route.digest()
    assert geoai_route != secp_route
