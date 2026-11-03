"""NF-2026-02004: with a repository known, nothing resolves outside it.

Seven production sites used to place temp, sandbox temp, caches or leases
outside the repository (system temp, ``%LOCALAPPDATA%``, ``~/.cache``).  Each
now resolves under ``<repo>/.aiworkhub`` -- runtime, cache or temp -- once a
repository is known, and a fallback outside it survives only for the case
where no repository is known, spelled NO_REPO in code.

(a) every listed site is driven with a repository known and must land under
    it; a static scan of the same sites allows an outside-repo spelling only
    on the explicit NO_REPO allow-list below.
(b) the Windows AppContainer validation lane's temp is per request, in the
    repository, and short enough that a union-named git worktree built under
    its pytest basetemp stays below git's measured ``$GIT_DIR`` limit.
"""

from __future__ import annotations

import inspect
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import app_server_mux
from aiworkhub import runtime_temp
from aiworkhub import tree_sitter_cache as tsc
from aiworkhub import windows_appcontainer as wac
from aiworkhub import worker_ai_tools_mcp
from aiworkhub import worker_workspace as ww
from aiworkhub import workspace_hygiene as hygiene
from aiworkhub.repository_state import bootstrap_repository

# The canonical checkout's root (``D:\Dev\AIWorkHub``): production lays the
# lane's temp out beneath a root this long.
_PRODUCTION_ROOT_LENGTH = len(r"D:\Dev\AIWorkHub")
# A union request id as the coordinator mints it.
_UNION_ID = "union2_" + "a" * 32 + "_" + "0123456789abcdef"
_SUFFIX = ".dll" if os.name == "nt" else ".so"

# The seven sites (module, qualname) and the only outside-repo spellings each
# may still carry: the NO_REPO fallbacks, used when no repository is known.
_OUTSIDE = re.compile(r"gettempdir\(|\"LOCALAPPDATA\"|Path\.home\(\)")
_SITES: dict[tuple[object, str], frozenset[str]] = {
    # (1) the AppContainer validation lane temp: no outside spelling at all.
    (ww, "provision_validation_exec_scratch"): frozenset(),
    (runtime_temp, "pytest_basetemp_argv"): frozenset(),
    # (2) sandbox drive leases.
    (wac, "_SandboxDriveLeases.place_for"): frozenset(),
    (wac, "_SandboxDriveLeases.repository_directory"): frozenset(),
    # (3) app_server_mux: ~/.aiworkhub is its machine-wide sideband (B925);
    # the temp branch is reached only without a home AND without a repo.
    (app_server_mux, "_default_sideband_dir"): frozenset({
        "home = Path.home()",
        'return Path(tempfile.gettempdir()) / ".aiworkhub" / leaf',
    }),
    # (4) worker_ai_tools_mcp validation run base.
    (worker_ai_tools_mcp, "_validation_run_dir"): frozenset({
        "base = Path(tempfile.gettempdir())  # NO_REPO: no repository is known",
    }),
    # (5) worker_workspace exec scratch roots and Windows temp_home.
    (ww, "sanitized_env"): frozenset(),
    (ww, "configured_runtime_root"): frozenset({
        'return (Path(tempfile.gettempdir()) / "aiworkhub-runtime").resolve()'
        "  # no repository known",
    }),
    (runtime_temp, "unhomed_child_temp"): frozenset({
        "return Path(tempfile.gettempdir())  # NO_REPO: no repository is known",
    }),
    # (6) workspace_hygiene build scratch base.
    (hygiene, "default_scratch_root"): frozenset({
        'base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()',
        'return (Path(base) if base else Path.home() / ".cache") / "aiworkhub" / "build-scratch"',
    }),
    # (7) tree-sitter cache: LOCALAPPDATA is a mirror source, never forwarded.
    (tsc, "trusted_tree_sitter_cache_root"): frozenset({
        'local_app_data = os.environ.get("LOCALAPPDATA", "").strip()',
    }),
    (tsc, "validation_cache_root"): frozenset(),
}


def _resolve(module: object, qualname: str) -> object:
    target = module
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


@pytest.mark.parametrize(
    ("module", "qualname"),
    sorted(_SITES, key=lambda site: (site[0].__name__, site[1])),  # type: ignore[attr-defined]
    ids=lambda value: value if isinstance(value, str) else value.__name__.rsplit(".", 1)[-1],
)
def test_outside_repo_spellings_are_only_the_named_no_repo_fallbacks(
    module: object, qualname: str
) -> None:
    source = inspect.getsource(_resolve(module, qualname))
    found = {line.strip() for line in source.splitlines() if _OUTSIDE.search(line)}
    assert found <= _SITES[(module, qualname)], found - _SITES[(module, qualname)]
    assert runtime_temp.NO_REPO_EXEC_SCRATCH_ROOTS == ww._DEFAULT_EXEC_SCRATCH_ROOTS


def _known_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    (repo / ".aiworkhub").mkdir(parents=True)
    (repo / ".aiworkhub" / "project.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.delenv(runtime_temp.TEMP_ROOT_ENV, raising=False)
    return repo.resolve()


def _under(path: Path, repo: Path) -> bool:
    return Path(path).resolve(strict=False).is_relative_to(repo.resolve())


# ── (b) and site 1: the AppContainer validation lane ───────────────────────


def _appcontainer_scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.delenv(ww.VALIDATION_EXEC_SCRATCH_ROOT_ENV, raising=False)
    monkeypatch.delenv(runtime_temp.TEMP_ROOT_ENV, raising=False)
    monkeypatch.setattr(ww, "is_windows", lambda *_a, **_k: True)
    # Under test is where the temp lives, not the host's exec/metadata policy.
    monkeypatch.setattr(ww, "_probe_exec_capable_dir", lambda _d: True)
    monkeypatch.setattr(ww, "_probe_metadata_capable_dir", lambda _d: True)
    repo = (tmp_path / "repo").resolve()
    request_dir = repo / ".aiworkhub" / "runtime" / "worktrees" / _UNION_ID
    worktree, home = request_dir / "worktree", request_dir / "home"
    worktree.mkdir(parents=True)
    home.mkdir()
    workspace = SimpleNamespace(request_id=_UNION_ID, repo=repo, path=worktree, home=home)
    scratch = ww.provision_validation_exec_scratch(
        workspace, backend=ww.WINDOWS_APPCONTAINER_BACKEND
    )
    return repo, scratch


def _basetemp(argv: list[str]) -> Path:
    (value,) = [token for token in argv if token.startswith("--basetemp=")]
    return Path(value.split("=", 1)[1])


def test_appcontainer_lane_temp_is_per_request_in_repo_and_below_the_git_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, scratch = _appcontainer_scratch(tmp_path, monkeypatch)
    try:
        # TMPDIR/TEMP/TMP are this scratch (run_validations); sitecustomize puts
        # TEMP/TMP back on it inside the container.  It is the request's own.
        assert _under(scratch, repo)
        assert _UNION_ID in scratch.parts
        basetemp = _basetemp(
            runtime_temp.pytest_basetemp_argv(["python", "-m", "pytest", "-q"], scratch)
        )
        assert basetemp.parent == scratch
        assert _under(basetemp, repo)

        # Measured as production lays it out beneath the canonical root.
        tail = str(basetemp)[len(str(repo)):]
        union_worktree = "r" * _PRODUCTION_ROOT_LENGTH + tail + os.sep + _UNION_ID
        assert len(union_worktree) < runtime_temp.GIT_DIR_PATH_LIMIT, len(union_worktree)
        # pytest's default basetemp would not fit: the explicit one is required.
        default = "r" * _PRODUCTION_ROOT_LENGTH + str(scratch)[len(str(repo)):]
        default += os.sep + "pytest-of-someuser" + os.sep + "pytest-12"
        assert len(default + os.sep + _UNION_ID) >= runtime_temp.GIT_DIR_PATH_LIMIT
    finally:
        ww.cleanup_validation_exec_scratch(scratch)
    # Terminal state: the request's temp subdirectory is gone.
    assert not scratch.exists()


def test_each_command_gets_its_own_basetemp_and_a_declared_one_wins(tmp_path: Path) -> None:
    scratch = tmp_path / "vx"
    scratch.mkdir()
    first = _basetemp(runtime_temp.pytest_basetemp_argv(["pytest"], scratch))
    first.mkdir()
    second = _basetemp(runtime_temp.pytest_basetemp_argv(["pytest"], scratch))
    assert first != second and first.parent == second.parent == scratch
    declared = ["pytest", "--basetemp=x"]
    assert runtime_temp.pytest_basetemp_argv(declared, scratch) == declared
    assert runtime_temp.pytest_basetemp_argv(["pytest"], None) == ["pytest"]
    # After "--" an option is a positional: the basetemp lands before it.
    split = runtime_temp.pytest_basetemp_argv(["pytest", "-q", "--", "-k"], scratch)
    assert split[:2] == ["pytest", "-q"] and split[3:] == ["--", "-k"]
    assert split[2] == f"--basetemp={first.parent / 'b1'}"


@pytest.mark.parametrize("part", ["C:", "C:evil", "leaf:stream", "..", "a/b", "a\\b", ""])
def test_repo_owned_dir_refuses_drive_qualified_and_escaping_parts(
    tmp_path: Path, part: str
) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    with pytest.raises(runtime_temp.RuntimeTempError, match="component_invalid"):
        runtime_temp.repo_runtime_dir(repo, part)
    assert runtime_temp.repo_runtime_dir(repo, "ok").parent == repo / ".aiworkhub" / "runtime"


# ── site 2: sandbox drive leases ────────────────────────────────────────────


def test_drive_leases_live_beside_the_repository_sandbox_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wac, "resolve_local_appdata", lambda: "")
    repo = (tmp_path / "repo").resolve()
    sandbox_root = repo / ".aiworkhub" / "runtime" / "worktrees"
    sandbox_root.mkdir(parents=True)

    built: list[tuple[Path, tuple[str, ...]]] = []
    real_runtime_dir = runtime_temp.repo_runtime_dir

    def _spy(repo_arg: Path, *parts: str) -> Path:
        built.append((Path(repo_arg), parts))
        return real_runtime_dir(repo_arg, *parts)

    monkeypatch.setattr(runtime_temp, "repo_runtime_dir", _spy)
    store = wac._SandboxDriveLeases.for_current_user()
    store.place_for(str(sandbox_root))

    assert store.directory == repo / ".aiworkhub" / "runtime" / "sandbox-drive-leases"
    # Built through the shared repo-runtime authority, not a hand-joined path.
    assert built == [(repo, ("sandbox-drive-leases",))]
    assert not wac._sandbox_root_of(str(store.directory))
    # An explicitly placed store never moves.
    explicit = wac._SandboxDriveLeases(tmp_path / "explicit")
    explicit.place_for(str(sandbox_root))
    assert explicit.directory == tmp_path / "explicit"
    # No repository known (no such sandbox root): the NO_REPO fallback stays.
    fallback = wac._SandboxDriveLeases.for_current_user()
    fallback.place_for(str(tmp_path / "missing" / ".aiworkhub" / "runtime" / "worktrees"))
    assert fallback.directory is None


# ── site 3: app_server_mux temp ─────────────────────────────────────────────


def test_mux_sideband_without_home_lives_in_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _known_repo(tmp_path, monkeypatch)

    def _no_home(cls: type) -> Path:
        raise RuntimeError("no home")

    monkeypatch.setattr(Path, "home", classmethod(_no_home))

    sideband = app_server_mux._default_sideband_dir()

    assert sideband == repo / ".aiworkhub" / "runtime" / "app_server_mux"


# ── site 4: worker_ai_tools_mcp validation run base ─────────────────────────


def test_mcp_validation_runs_live_in_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _known_repo(tmp_path, monkeypatch)
    ctx = SimpleNamespace(
        audit_ledger_path=None, contract_packet_path=None, request_id="req-nf2004"
    )

    run_dir = worker_ai_tools_mcp._validation_run_dir(ctx, repo)  # type: ignore[arg-type]

    owned = runtime_temp.request_dir(repo, "req-nf2004", "mcp")
    assert run_dir == owned / "validation_runs"
    assert run_dir.is_dir()
    # Owner-stamped, so the retention janitor can reclaim it once its owner dies.
    manifest = runtime_temp.read_owner_manifest(owned)
    assert manifest is not None and manifest["request_id"] == "req-nf2004"
    # A second call reuses the same directory and its stamp.
    assert worker_ai_tools_mcp._validation_run_dir(ctx, repo) == run_dir  # type: ignore[arg-type]


# ── site 5: worker_workspace exec scratch roots and temp_home ───────────────


def test_exec_scratch_with_a_repository_never_probes_a_no_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ww.VALIDATION_EXEC_SCRATCH_ROOT_ENV, raising=False)
    monkeypatch.delenv(runtime_temp.TEMP_ROOT_ENV, raising=False)
    monkeypatch.setattr(ww, "is_windows", lambda *_a, **_k: False)
    probed: list[Path] = []
    monkeypatch.setattr(ww, "_probe_exec_capable_dir", lambda d: probed.append(d) or True)
    monkeypatch.setattr(ww, "_probe_metadata_capable_dir", lambda _d: True)
    repo = (tmp_path / "repo").resolve()
    worktree = repo / "wt"
    worktree.mkdir(parents=True)
    workspace = SimpleNamespace(
        request_id="req-nf2004", repo=repo, path=worktree, home=repo / "home"
    )

    scratch = ww.provision_validation_exec_scratch(workspace)
    try:
        assert scratch.parent == runtime_temp.temp_root(repo) / "validation"
        assert probed and all(_under(path, repo) for path in probed)
    finally:
        ww.cleanup_validation_exec_scratch(scratch)
    assert not scratch.exists()


def test_unhomed_child_temp_is_in_the_repository_or_the_sandbox_private_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _known_repo(tmp_path, monkeypatch)
    temp = runtime_temp.unhomed_child_temp(repo)
    if os.name == "nt":
        assert temp == runtime_temp.temp_root(repo)
    else:
        # bubblewrap's own private /tmp: inside the sandbox, never a host path.
        assert temp == Path("/tmp")


# ── site 6: workspace_hygiene build scratch ─────────────────────────────────


def test_build_scratch_pool_lives_in_the_repository_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AIWORKHUB_BUILD_SCRATCH_ROOT", raising=False)
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    bootstrap_repository(repo, repo_name="repo")

    pool = hygiene.repository_pool(repo)

    assert pool.parent == repo / ".aiworkhub" / "cache" / "build-scratch"
    # The source tree itself is still refused.
    with pytest.raises(hygiene.WorkspaceHygieneError):
        hygiene.repository_pool(repo, scratch_root=repo / "cache")
    # Only .aiworkhub/cache is exempt: .aiworkhub/config is still refused.
    with pytest.raises(hygiene.WorkspaceHygieneError, match="outside_repository"):
        hygiene.repository_pool(repo, scratch_root=repo / ".aiworkhub" / "config")


# ── site 7: tree-sitter cache ───────────────────────────────────────────────


def _seed_grammars(root: Path) -> None:
    libs = root / "tree-sitter-language-pack" / "v1.20.0" / "libs"
    libs.mkdir(parents=True)
    for grammar in ("javascript", "typescript"):
        (libs / f"tree_sitter_{grammar}{_SUFFIX}").write_bytes(grammar.encode() * 64)


def test_profile_grammar_cache_is_only_a_mirror_source_with_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_app_data = tmp_path / "local"
    _seed_grammars(local_app_data)
    monkeypatch.setattr(tsc._platform_io, "is_windows", lambda *_a, **_k: True)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv(tsc.TREE_SITTER_CACHE_ROOT_ENV, raising=False)
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()

    assert tsc.validation_cache_root(repo) == tsc.repo_mirror_root(repo)

    blocked = (tmp_path / "blocked").resolve()
    mirror = tsc.repo_mirror_root(blocked)
    mirror.parent.mkdir(parents=True)
    mirror.write_bytes(b"not a directory")
    # The mirror cannot be written: forward nothing rather than the profile.
    assert tsc.validation_cache_root(blocked) is None
    # NO_REPO: the trusted source itself.
    assert tsc.validation_cache_root(None) == local_app_data.resolve()


def test_a_symlinked_repo_mirror_is_never_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tsc, "trusted_tree_sitter_cache_root", lambda: None)
    outside = (tmp_path / "outside").resolve()
    _seed_grammars(outside)
    repo = (tmp_path / "repo").resolve()
    mirror = tsc.repo_mirror_root(repo)
    mirror.parent.mkdir(parents=True)
    try:
        mirror.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    # The outside tree is complete, yet a link out of the repo is not forwarded.
    assert tsc._parser_complete(mirror)
    assert tsc.validation_cache_root(repo) is None


# ── cleanup: per-request removal and the janitor's by-age reclaim ───────────


def test_short_temp_is_owner_stamped_and_reclaimed_only_once_its_owner_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _known_repo(tmp_path, monkeypatch)
    short = runtime_temp.short_temp_dir(repo)
    assert short.parent == runtime_temp.temp_root(repo) / runtime_temp.SHORT_NAMESPACE
    assert runtime_temp.read_owner_manifest(short) is not None

    # A live owner keeps it; once the owner is dead the janitor may reclaim it.
    monkeypatch.setattr(runtime_temp, "request_alive", lambda _repo, _manifest: True)
    assert runtime_temp.identify_dead_owner_dirs(repo) == []
    monkeypatch.setattr(runtime_temp, "request_alive", lambda _repo, _manifest: False)
    dead = runtime_temp.identify_dead_owner_dirs(repo)
    assert [row["path"] for row in dead] == [short]

    assert runtime_temp.dispose_request_temp(short, repo=repo)
    assert not short.exists()


def test_short_tmp_fixture_uses_the_repo_owned_temp_root(
    short_tmp: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    try:
        owned = short_tmp.is_relative_to(runtime_temp.temp_root(repo_root))
    except runtime_temp.RuntimeTempError:
        owned = False
    # A sandbox that will not let this worktree write its own .aiworkhub
    # yields this run's pytest temp instead -- never tempfile.gettempdir().
    assert owned or short_tmp.is_relative_to(tmp_path_factory.getbasetemp())
    assert len(short_tmp.name) <= 8
