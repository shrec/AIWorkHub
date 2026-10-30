"""NF-2026-01843: registered compilers resolve, are granted and are probed (fakes only)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import aiworkhub.repository_state as repository_state
import aiworkhub.toolchain_registry as registry
import aiworkhub.windows_appcontainer as windows_appcontainer
import aiworkhub.worker_workspace as worker_workspace
from aiworkhub.worker_workspace import WorkspaceError


def _document(tools: Path, **entry_overrides) -> dict:
    entry = {
        "executables": ["xtensa-esp32-elf-gcc", "g++"],
        "roots": [str(tools / "esp")],
        "path_prepend": [str(tools / "esp" / "bin")],
        "env": {"IDF_PATH": str(tools / "esp" / "idf"), "CCACHE_DIR": "{request_temp}/cc"},
    }
    entry.update(entry_overrides)
    return {"schema_id": registry.REGISTRY_SCHEMA_ID, "toolchains": {"esp32": entry}}


@pytest.fixture
def env(tmp_path_factory, monkeypatch):
    repo = tmp_path_factory.mktemp("repo")
    tools = tmp_path_factory.mktemp("tools")
    (tools / "esp" / "bin").mkdir(parents=True)
    monkeypatch.setattr(registry, "canonical_repo_root", lambda: repo)
    monkeypatch.setattr(registry.os, "access", lambda *_a, **_k: True)
    registry._PROBES.clear()

    def write(document) -> None:
        path = repo / registry.REGISTRY_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")

    def make(relative: str) -> Path:
        path = tools / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return path

    return SimpleNamespace(repo=repo, tools=tools, write=write, make=make)


def _fake_which(found: Path | None, calls: list):
    def which(name, mode=os.F_OK | os.X_OK, path=None):
        calls.append((name, path))
        return str(found) if found else None

    return which


def test_missing_registry_is_empty_and_builtin_resolution_is_unchanged(env, monkeypatch) -> None:
    assert registry.load_registry().toolchains == ()
    assert registry.registered_toolchain("g++") is None
    git = env.make("sys/git")
    calls: list = []
    monkeypatch.setattr(registry.shutil, "which", _fake_which(git, calls))
    assert registry._resolve_trusted_system_validation_executable("git") == git.resolve()
    assert calls == [("git", None)]
    with pytest.raises(WorkspaceError, match="validation_executable_not_approved:g\\+\\+"):
        registry._resolve_trusted_system_validation_executable("g++")
    assert registry.toolchain_for_launch(str(git)) is None


def test_worker_workspace_reexports_the_moved_resolver() -> None:
    assert (
        worker_workspace._resolve_trusted_system_validation_executable
        is registry._resolve_trusted_system_validation_executable
    )


def test_registry_is_read_only_from_the_canonical_root(env, monkeypatch, tmp_path) -> None:
    env.write(_document(env.tools))
    decoy = tmp_path / "workspace"
    decoy_file = decoy / registry.REGISTRY_RELATIVE_PATH
    decoy_file.parent.mkdir(parents=True)
    decoy_file.write_text(
        json.dumps(
            {
                "schema_id": registry.REGISTRY_SCHEMA_ID,
                "toolchains": {"evil": {"executables": ["evil"], "roots": [str(env.tools)]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(decoy)
    loaded = registry.load_registry()
    assert [tc.id for tc in loaded.toolchains] == ["esp32"]
    assert registry.registered_toolchain("evil") is None


def test_canonical_root_prefers_core_then_env(monkeypatch, tmp_path) -> None:
    monkeypatch.undo()
    monkeypatch.setitem(sys.modules, f"{registry.__package__}.core", SimpleNamespace(repo_root=lambda: tmp_path))
    assert registry.canonical_repo_root() == tmp_path.resolve()
    monkeypatch.delitem(sys.modules, f"{registry.__package__}.core")
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path))
    assert registry.canonical_repo_root() == tmp_path.resolve()
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT")
    assert registry.canonical_repo_root() is None


def test_valid_registry_round_trips(env) -> None:
    env.write(_document(env.tools))
    toolchain = registry.registered_toolchain("g++")
    assert toolchain is not None and toolchain.id == "esp32"
    assert toolchain.executables == ("xtensa-esp32-elf-gcc", "g++")
    assert registry.registered_toolchain("pytest") is None


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda d, t, r: d.update(extra=1), "unknown_keys"),
        (lambda d, t, r: d.update(schema_id="other"), "schema_id_mismatch"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(cflags="x"), "unknown_keys:esp32"),
        (lambda d, t, r: d["toolchains"].update({"Bad": d["toolchains"].pop("esp32")}), "id_invalid"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(executables=["-x"]), "executable_invalid"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(executables=["a b"]), "executable_invalid"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(executables=[]), "executables_not_a_bounded_list"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(executables=["pytest"]), "executable_reserved"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(roots=["relative/dir"]), "roots_not_absolute"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(roots=[]), "roots_empty"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(path_prepend=["bin"]), "path_prepend_not_absolute"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(roots=[str(r / "tools")]), "roots_inside_repository"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(path_prepend=[str(r)]), "path_prepend_inside_repository"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(env={"1BAD": "x"}), "env_name_invalid"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(env={"Path": "x"}), "env_name_reserved"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(env={"TMP": "x"}), "env_name_reserved"),
        (lambda d, t, r: d["toolchains"]["esp32"].update(env={"OK": 1}), "env_value_invalid"),
        (
            lambda d, t, r: d["toolchains"].update(
                other={"executables": ["g++"], "roots": [str(t / "esp")]}
            ),
            "executable_in_two_toolchains",
        ),
    ],
)
def test_registry_refusals(env, mutate, needle) -> None:
    document = _document(env.tools)
    mutate(document, env.tools, env.repo)
    env.write(document)
    with pytest.raises(WorkspaceError, match=f"toolchain_registry_invalid:.*{needle}"):
        registry.load_registry()


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"\xff\xfe"])
def test_registry_garbage_refuses(env, raw) -> None:
    path = env.repo / registry.REGISTRY_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    with pytest.raises(WorkspaceError, match="toolchain_registry_invalid"):
        registry.load_registry()


def test_oversized_registry_refuses(env) -> None:
    path = env.repo / registry.REGISTRY_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(b" " * (registry.MAX_REGISTRY_BYTES + 1))
    with pytest.raises(WorkspaceError, match="toolchain_registry_invalid:too_large"):
        registry.load_registry()


def test_registered_name_resolves_only_under_its_roots(env, monkeypatch) -> None:
    env.write(_document(env.tools))
    inside = env.make("esp/bin/xtensa-esp32-elf-gcc")
    outside = env.make("elsewhere/xtensa-esp32-elf-gcc")
    calls: list = []
    monkeypatch.setattr(registry.shutil, "which", _fake_which(inside, calls))
    resolved = registry._resolve_trusted_system_validation_executable("xtensa-esp32-elf-gcc")
    assert resolved == inside.resolve()
    search = calls[0][1]
    assert search.split(os.pathsep)[0] == str(env.tools / "esp" / "bin")

    monkeypatch.setattr(registry.shutil, "which", _fake_which(outside, []))
    with pytest.raises(WorkspaceError, match="validation_executable_outside_toolchain_roots"):
        registry._resolve_trusted_system_validation_executable("xtensa-esp32-elf-gcc")

    monkeypatch.setattr(registry.shutil, "which", _fake_which(None, []))
    with pytest.raises(WorkspaceError, match="validation_executable_unavailable"):
        registry._resolve_trusted_system_validation_executable("g++")


def test_normalizer_accepts_a_registered_head(env, monkeypatch) -> None:
    env.write(_document(env.tools))
    gcc = env.make("esp/bin/g++")
    monkeypatch.setattr(registry.shutil, "which", _fake_which(gcc, []))
    argv, roots, authority = worker_workspace._normalize_trusted_validation_executable_argv_with_authority(
        ["g++", "--version"], env.repo
    )
    assert argv == [str(gcc.resolve()), "--version"]
    assert roots == () and authority is None


def test_launch_lookup_matches_name_and_directory(env) -> None:
    env.write(_document(env.tools))
    inside = env.make("esp/bin/g++.exe")
    other = env.make("other/g++.exe")
    assert registry.toolchain_for_launch(str(inside)).id == "esp32"
    assert registry.toolchain_for_launch(str(other)) is None
    assert registry.toolchain_for_launch("g++") is None


def test_launch_lookup_matches_a_registered_name_by_what_it_resolves_to(env, monkeypatch) -> None:
    # A registered name that symlinks to a differently named binary reaches the
    # launch as the resolved file, so its own name alone would miss the entry.
    env.write(_document(env.tools))
    target = env.make("esp/bin/xtensa-esp32-elf-gcc-13.2.0.exe")
    monkeypatch.setattr(registry.shutil, "which", _fake_which(target, []))
    assert registry.toolchain_for_launch(str(target)).id == "esp32"
    monkeypatch.setattr(registry.shutil, "which", _fake_which(None, []))
    assert registry.toolchain_for_launch(str(target)) is None


def test_probe_turns_any_launch_error_into_unreachable(env, monkeypatch) -> None:
    env.write(_document(env.tools))
    gcc = env.make("esp/bin/xtensa-esp32-elf-gcc")
    monkeypatch.setattr(registry.shutil, "which", _fake_which(gcc, []))

    def launch(_argv, _repo):
        raise TypeError("probe workspace drifted")

    probe = registry.probe_toolchain(env.repo, "esp32", launch=launch)
    assert probe.status == "unreachable"
    assert "probe workspace drifted" in probe.reason


def test_read_grants_refuse_a_root_linked_into_a_writable_tree(env, tmp_path) -> None:
    writable = tmp_path / "worktree"
    (writable / "planted").mkdir(parents=True)
    link = tmp_path / "esp-link"
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(writable / "planted"), str(link))
        else:
            os.symlink(writable / "planted", link, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create a directory link on this host")
    if os.path.normcase(os.path.realpath(link)) != os.path.normcase(os.path.realpath(writable / "planted")):
        # Registry grants are computed on the host; a sandbox that cannot follow the
        # link it just made (the validation lane) has nothing to measure here.
        pytest.skip("this host cannot resolve the link it created")
    env.write(_document(env.tools, roots=[str(link)], path_prepend=[]))
    toolchain = registry.load_registry().get("esp32")
    with pytest.raises(windows_appcontainer.AppContainerError):
        toolchain.read_grants(covered=[str(writable)])


def test_launch_env_is_scoped_and_expands_request_temp(env) -> None:
    env.write(_document(env.tools))
    toolchain = registry.registered_toolchain("g++")
    base = {"PATH": "C:\\base", "TMPDIR": "C:\\req\\tmp", "KEEP": "1"}
    out = toolchain.launch_env(base)
    assert base == {"PATH": "C:\\base", "TMPDIR": "C:\\req\\tmp", "KEEP": "1"}
    assert out["CCACHE_DIR"] == "C:\\req\\tmp/cc"
    assert out["IDF_PATH"] == str(env.tools / "esp" / "idf")
    assert out["PATH"] == os.pathsep.join([str(env.tools / "esp" / "bin"), "C:\\base"])
    assert out["KEEP"] == "1"
    with pytest.raises(WorkspaceError, match="toolchain_request_temp_unavailable:esp32"):
        toolchain.launch_env({"PATH": "x"})


def test_read_grants_are_exact_persistent_read_execute(env) -> None:
    env.write(_document(env.tools))
    toolchain = registry.registered_toolchain("g++")
    grants = toolchain.read_grants(covered=[str(env.repo)])
    assert grants == [
        windows_appcontainer.ContainerGrant(str(env.tools / "esp"), "read_execute", persistent=True)
    ]


def test_read_grants_refuse_writable_overlap_and_skip_missing(env) -> None:
    env.write(_document(env.tools))
    toolchain = registry.registered_toolchain("g++")
    for covered in (str(env.tools / "esp" / "bin"), str(env.tools / "esp"), str(env.tools)):
        with pytest.raises(windows_appcontainer.AppContainerError) as raised:
            toolchain.read_grants(covered=[covered])
        assert raised.value.reason is windows_appcontainer.AppContainerReason.INVALID_REQUEST
    (env.tools / "esp").rename(env.tools / "gone")
    assert toolchain.read_grants(covered=[]) == []


class _Launch:
    def __init__(self, returncode: int = 0, error: BaseException | None = None) -> None:
        self.calls: list[list[str]] = []
        self._returncode, self._error = returncode, error

    def __call__(self, argv, repo):
        self.calls.append(list(argv))
        if self._error is not None:
            raise self._error
        return subprocess.CompletedProcess(argv, self._returncode, "", "")


def test_probe_runs_version_and_memoizes_per_registry_hash(env, monkeypatch) -> None:
    env.write(_document(env.tools))
    gcc = env.make("esp/bin/xtensa-esp32-elf-gcc")
    monkeypatch.setattr(registry.shutil, "which", _fake_which(gcc, []))
    launch = _Launch(0)
    assert registry.probe_toolchain(env.repo, "esp32", launch=launch).status == "reachable"
    assert registry.probe_toolchain(env.repo, "esp32", launch=launch).status == "reachable"
    assert launch.calls == [[str(gcc.resolve()), "--version"]]
    env.write(_document(env.tools, env={}))
    registry.probe_toolchain(env.repo, "esp32", launch=launch)
    assert len(launch.calls) == 2


@pytest.mark.parametrize(
    ("launch", "reason"),
    [
        (_Launch(3), "exit_3"),
        (_Launch(error=OSError("windows_appcontainer_validation_launch_failed:denied")), "denied"),
        (_Launch(error=subprocess.TimeoutExpired("x", 1)), "timeout"),
    ],
)
def test_preflight_refuses_an_unreachable_toolchain_and_memoizes(env, monkeypatch, launch, reason) -> None:
    env.write(_document(env.tools))
    gcc = env.make("esp/bin/xtensa-esp32-elf-gcc")
    monkeypatch.setattr(registry.shutil, "which", _fake_which(gcc, []))
    monkeypatch.setattr(registry, "_is_windows", lambda: True)
    monkeypatch.setattr(registry, "_launch_in_appcontainer", launch)
    card = {"allowed_writes": [], "validation": ["xtensa-esp32-elf-gcc --version"]}
    first = worker_workspace.preflight_validation_capabilities(env.repo, card)
    second = worker_workspace.preflight_validation_capabilities(env.repo, card)
    refusal = [row for row in first if row.startswith("toolchain_unreachable:esp32:")]
    assert len(refusal) == 1 and reason in refusal[0]
    assert second == first
    assert len(launch.calls) == 1


def test_preflight_passes_a_reachable_toolchain_and_never_refuses_off_windows(env, monkeypatch) -> None:
    env.write(_document(env.tools))
    gcc = env.make("esp/bin/xtensa-esp32-elf-gcc")
    monkeypatch.setattr(registry.shutil, "which", _fake_which(gcc, []))
    card = {"allowed_writes": [], "validation": ["xtensa-esp32-elf-gcc --version"]}
    launch = _Launch(9)
    monkeypatch.setattr(registry, "_launch_in_appcontainer", launch)
    monkeypatch.setattr(registry, "_is_windows", lambda: False)
    assert registry.probe_toolchain(env.repo, "esp32").status == "backend_not_probed"
    assert not [r for r in worker_workspace.preflight_validation_capabilities(env.repo, card) if "toolchain_" in r]
    assert launch.calls == []
    monkeypatch.setattr(registry, "_is_windows", lambda: True)
    monkeypatch.setattr(registry, "_launch_in_appcontainer", _Launch(0))
    assert not [r for r in worker_workspace.preflight_validation_capabilities(env.repo, card) if "toolchain_" in r]


def test_unregistered_probe_target_is_unreachable(env) -> None:
    assert registry.probe_toolchain(env.repo, "nope", launch=_Launch(0)).reason == "not_registered"


class _AppContainerLaunch:
    def __init__(self, request) -> None:
        self.request = request

    def wait(self, timeout_ms, **_kwargs):
        return windows_appcontainer.AppContainerLifecycleResult(
            windows_appcontainer.AppContainerLifecycleState.EXITED, exit_code=0
        )

    def terminate(self, exit_code: int = 1):
        return None

    def close(self) -> None:
        return None


def _run(monkeypatch, workspace, argv, sink):
    msvcrt = pytest.importorskip("msvcrt")
    monkeypatch.setattr(msvcrt, "get_osfhandle", lambda fd: fd)
    monkeypatch.setattr(
        repository_state,
        "inspect_repository",
        lambda repo, **_kw: SimpleNamespace(manifest=SimpleNamespace(repo_id="repo_" + "0" * 32)),
    )
    monkeypatch.setattr(
        windows_appcontainer,
        "launch_appcontainer",
        lambda request: sink.append(request) or _AppContainerLaunch(request),
    )
    worker_workspace._run_appcontainer_validation(
        argv,
        workspace=workspace,
        adapter_id="claude_cli",
        cwd=workspace.path,
        env={"PATH": "x", "TMPDIR": str(workspace.path / "tmp")},
        timeout_seconds=30,
    )
    return sink[-1]


def test_appcontainer_launch_grants_and_env_only_for_the_registered_head(env, monkeypatch, tmp_path) -> None:
    env.write(_document(env.tools))
    gcc = env.make("esp/bin/g++.exe")
    workspace = SimpleNamespace(repo=tmp_path, path=tmp_path, home=tmp_path)
    sink: list = []
    request = _run(monkeypatch, workspace, [str(gcc), "--version"], sink)
    grant = windows_appcontainer.ContainerGrant(str(env.tools / "esp"), "read_execute", persistent=True)
    assert grant in request.filesystem_grants
    assert request.environment["IDF_PATH"] == str(env.tools / "esp" / "idf")
    assert request.environment["PATH"].startswith(str(env.tools / "esp" / "bin"))


def test_no_registry_leaves_a_pytest_launch_untouched(env, monkeypatch, tmp_path) -> None:
    workspace = SimpleNamespace(repo=tmp_path, path=tmp_path, home=tmp_path)
    sink: list = []
    plain = _run(monkeypatch, workspace, ["pytest", "-q"], sink)
    env.write(_document(env.tools))
    with_registry = _run(monkeypatch, workspace, ["pytest", "-q"], sink)
    assert plain.filesystem_grants == with_registry.filesystem_grants
    # Writing the registry touches a directory above the request root, so the
    # ancestors' lstat facts differ between the two launches by construction.
    ancestors = windows_appcontainer.APPCONTAINER_ANCESTORS_ENV
    assert plain.environment.keys() == with_registry.environment.keys()
    assert {k: v for k, v in plain.environment.items() if k != ancestors} == {
        k: v for k, v in with_registry.environment.items() if k != ancestors
    }
    assert "IDF_PATH" not in with_registry.environment
