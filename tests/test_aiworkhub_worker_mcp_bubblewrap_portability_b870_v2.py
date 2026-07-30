"""B870 V2: repository-layout-independent worker MCP import-root contract.

The rejected V1 change derived the AIWorkHub package's PYTHONPATH by counting
a fixed number of ``Path(__file__).parents`` (``parents[4]``) and assuming a
``tools/geoai-task-mcp/src`` monorepo subpath, then rejoining that guessed
offset onto ``authority_repo``. That breaks for a standalone
``<repo>/src/aiworkhub`` checkout, silently mis-resolves under a differently
shaped monorepo, and cannot work when the package is bundled/installed
entirely OUTSIDE the active project repository (``authority_repo`` may not
even contain it).

This module proves the V2 repair end to end:

  * ``worker_ai_tools_mcp.resolve_host_package_import_root()`` derives the
    import root purely from the running module's own ``__file__`` -- proven
    here under three real, independently-spawned process layouts: a
    standalone ``<repo>/src/aiworkhub`` checkout, a differently-shaped nested
    monorepo path, and a bundled package with no enclosing project repo at
    all.
  * ``generate_worker_mcp_runtime`` never derives ``package_import_root``
    itself and never rebases it onto ``authority_repo`` -- proven by passing
    two entirely unrelated fabricated paths and checking PYTHONPATH echoes
    the package root verbatim.
  * ``worker_workspace.provision_worker_mcp_runtime`` + ``sandbox_argv``
    together bind the resolved host package root at the dedicated
    ``SANDBOX_PACKAGE_IMPORT_ROOT`` alias under bubblewrap, independent of the
    ``SANDBOX_AUTHORITY_REPO`` bind, and pass the real host path straight
    through for landlock/direct.
  * A real bubblewrap handshake proves the nested-monorepo shape (the
    package's resolved root living BENEATH the bound authority repo, as in
    this project's actual current layout) still works end to end.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import get_default_environment, stdio_client  # noqa: E402

from aiworkhub import repository_state  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as w  # noqa: E402
from aiworkhub import worker_workspace as ws  # noqa: E402

REQUEST_TIMEOUT = timedelta(seconds=30)
BWRAP = Path("/usr/bin/bwrap")
_PACKAGE_FILES = ("__init__.py", "_version.py", "repository_state.py", "worker_ai_tools_mcp.py")


def _write_tool(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _fake_authority_repo(tmp_path: Path) -> Path:
    """Self-contained fake authority repo (same shape as the B833/B834/B869/
    B870-V1 fixtures) -- deliberately never the live, possibly-concurrently-
    mutated project checkout, so this test's outcome depends only on the code
    under test, not on the shared repo's current dirty/registry state."""
    repo = tmp_path / "authority_repo"
    (repo / "AITools" / "ai_memory").mkdir(parents=True)
    for relative in (
        "AITools/session.db", "AITools/transcript_graph.db",
        "AITools/ai_memory/ai_memory.db", "AITools/kb.db",
    ):
        (repo / relative).write_bytes(b"SQLite format 3\x00fake-non-empty-authority-db")
    repository_state.bootstrap_repository(repo)
    source_graph_db = repo / ".aiworkhub" / "source_graph" / "source_graph.sqlite"
    source_graph_db.parent.mkdir(parents=True, exist_ok=True)
    source_graph_db.write_bytes(b"SQLite format 3\x00fake-non-empty-source-graph-db")
    _write_tool(
        repo / "AITools/transcript_graph.py",
        "import json, sys\n"
        "print(json.dumps({'state': 'partial_state', 'evidence': [{'id': 1}], 'topic': sys.argv[2]}))\n",
    )
    _write_tool(
        repo / "AITools/ai_memory/ai_memory.py",
        "import json\nprint(json.dumps({'count': 1, 'results': [{'key': 'ctx', 'value': 'bounded'}]}))\n",
    )
    _write_tool(
        repo / "AITools/kb.py",
        "import sys\nprint('[module] bounded KB context over nested-layout bubblewrap pipe')\n",
    )
    return repo


def _copy_minimal_package(dest_src_root: Path) -> Path:
    """Copy (never symlink) the minimal files needed to import
    ``aiworkhub.worker_ai_tools_mcp`` into a fresh ``<dest_src_root>/aiworkhub``
    directory, so a subprocess importing from there sees a genuinely
    different ``__file__`` location -- a symlink would resolve straight back
    to this checkout's real path and defeat the point of the test."""
    package_dir = dest_src_root / "aiworkhub"
    package_dir.mkdir(parents=True, exist_ok=True)
    for name in _PACKAGE_FILES:
        shutil.copy2(_SRC / "aiworkhub" / name, package_dir / name)
    return dest_src_root


def _resolved_root_in_subprocess(src_root: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-c", "from aiworkhub import worker_ai_tools_mcp as w; print(w.resolve_host_package_import_root())"],
        env={"PYTHONPATH": str(src_root), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_resolve_host_package_import_root_matches_real_src_in_process() -> None:
    assert w.resolve_host_package_import_root() == _SRC.resolve()


def test_resolve_host_package_import_root_standalone_repo_src_aiworkhub_layout(tmp_path: Path) -> None:
    """Standalone ``<repo>/src/aiworkhub`` checkout shape."""
    src_root = _copy_minimal_package(tmp_path / "standalone_repo" / "src")
    assert _resolved_root_in_subprocess(src_root) == str(src_root.resolve())


def test_resolve_host_package_import_root_nested_monorepo_layout(tmp_path: Path) -> None:
    """A monorepo shape at a DIFFERENT depth than this project's own
    ``tools/geoai-task-mcp/src`` (four parents) -- proving no fixed parent
    count is assumed."""
    src_root = _copy_minimal_package(
        tmp_path / "monorepo" / "apps" / "geoai" / "packages" / "task_mcp" / "python" / "src"
    )
    assert _resolved_root_in_subprocess(src_root) == str(src_root.resolve())


def test_resolve_host_package_import_root_bundled_outside_any_repo(tmp_path: Path) -> None:
    """A bundled/installed package with no enclosing project repository and
    no ``src`` wrapper directory at all -- the import root is simply whatever
    directory directly contains ``aiworkhub/``."""
    src_root = _copy_minimal_package(tmp_path / "opt" / "vendor" / "site-packages")
    assert _resolved_root_in_subprocess(src_root) == str(src_root.resolve())


def test_generate_worker_mcp_runtime_never_rebases_package_import_root_onto_authority_repo(
    tmp_path: Path,
) -> None:
    """Regression guard for the rejected V1 shape: passing two entirely
    unrelated ``authority_repo`` / ``package_import_root`` values must leave
    PYTHONPATH exactly equal to the given ``package_import_root`` -- no
    ``.relative_to()``, no rejoin, no dependence on ``authority_repo`` at
    all."""
    home = tmp_path / "home"
    authority_repo = tmp_path / "some_unrelated_authority_repo"
    package_import_root = tmp_path / "completely_different_bundled_location" / "src"
    assert not str(package_import_root).startswith(str(authority_repo))
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id="req_b870v2_norebase", task_id="TASK_B870_V2", runner="claude_b870v2",
        topic="task_mcp", repo=authority_repo, authority_repo=authority_repo,
        source_graph_targets=[], session_topic="AIWorkHub worker MCP bubblewrap portability B870 V2",
        package_import_root=package_import_root,
    )
    config = json.loads(runtime.claude_mcp_config_path.read_text(encoding="utf-8"))
    env = config["mcpServers"][w.SERVER_NAME]["env"]
    assert env[w.ENV_PYTHONPATH] == str(package_import_root)
    assert runtime.package_import_root == package_import_root


def _fake_workspace(tmp_path: Path) -> ws.WorkerWorkspace:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "worktree"
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    return ws.WorkerWorkspace(
        request_id="req_b870v2_argv",
        repo=repo,
        path=path,
        home=tmp_path / "home",
        allowed_writes=("x.txt",),
        parent_baseline={},
        workspace_baseline={},
    )


def test_sandbox_argv_binds_package_import_root_at_dedicated_alias_independent_of_authority_repo(
    tmp_path: Path,
) -> None:
    workspace = _fake_workspace(tmp_path)
    host_package_root = tmp_path / "host_package_root_outside_repo"
    host_package_root.mkdir(parents=True, exist_ok=True)
    argv = ws.sandbox_argv(
        workspace, "validation", ["true"], backend="bubblewrap",
        package_import_root=host_package_root,
    )
    assert "--ro-bind" in argv
    package_bind_index = argv.index(str(host_package_root))
    assert argv[package_bind_index - 1] == "--ro-bind"
    assert argv[package_bind_index + 1] == ws.SANDBOX_PACKAGE_IMPORT_ROOT
    authority_bind_index = argv.index(str(workspace.repo))
    assert argv[authority_bind_index + 1] == ws.SANDBOX_AUTHORITY_REPO
    assert ws.SANDBOX_PACKAGE_IMPORT_ROOT != ws.SANDBOX_AUTHORITY_REPO


def test_sandbox_argv_omits_package_alias_bind_when_not_provided(tmp_path: Path) -> None:
    workspace = _fake_workspace(tmp_path)
    argv = ws.sandbox_argv(workspace, "validation", ["true"], backend="bubblewrap")
    assert ws.SANDBOX_PACKAGE_IMPORT_ROOT not in argv


def test_provisioned_bubblewrap_pythonpath_alias_matches_resolver(tmp_path: Path) -> None:
    """Cross-check: the alias ``provision_worker_mcp_runtime`` writes into
    PYTHONPATH is the exact constant ``sandbox_argv`` binds the REAL resolved
    host package root to -- the two independent call sites
    (``worker_workspace.provision_worker_mcp_runtime`` at config-generation
    time, ``process_launcher.py``'s own ``sandbox_argv`` call at adapter
    launch time) can never silently disagree on the alias string."""
    workspace = _fake_workspace(tmp_path)
    runtime = ws.provision_worker_mcp_runtime(
        workspace, request_id="req_b870v2_consistency", task_id="TASK_B870_V2",
        runner="claude_b870v2", topic="task_mcp", backend="bubblewrap",
        source_graph_targets=[], session_topic="AIWorkHub worker MCP bubblewrap portability B870 V2",
    )
    config = json.loads(runtime.claude_mcp_config_path.read_text(encoding="utf-8"))
    provisioned_alias = config["mcpServers"][w.SERVER_NAME]["env"][w.ENV_PYTHONPATH]

    argv = ws.sandbox_argv(
        workspace, "validation", ["true"], backend="bubblewrap",
        package_import_root=w.resolve_host_package_import_root(),
    )
    bound_alias = argv[argv.index(str(w.resolve_host_package_import_root())) + 1]
    assert provisioned_alias == bound_alias == ws.SANDBOX_PACKAGE_IMPORT_ROOT


async def _run_handshake(params: StdioServerParameters) -> dict:
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=REQUEST_TIMEOUT) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            call = await session.call_tool(
                "aiworkhub_worker_kb_search", {"query": "nested layout smoke"},
                read_timeout_seconds=REQUEST_TIMEOUT,
            )
            return {"tool_names": names, "call_is_error": call.isError}


def _real_repo_root() -> Path:
    # tests/<this file> -> tools/geoai-task-mcp -> tools -> <repo root>
    return Path(__file__).resolve().parents[3]


_BWRAP_USABLE = BWRAP.exists() and ws._bubblewrap_usable(BWRAP)


@pytest.mark.skipif(
    not _BWRAP_USABLE,
    reason="bubblewrap unusable on this host (unprivileged userns restricted -- "
    "same probe worker_workspace.select_sandbox_backend() uses to fall back "
    "to landlock in production)",
)
def test_real_bubblewrap_handshake_over_current_nested_monorepo_layout(tmp_path: Path) -> None:
    """Live proof for THIS project's actual current layout: the resolved
    package import root lives nested BENEATH this project's real repository
    root (``<repo>/tools/geoai-task-mcp/src``) -- asserted against the real
    checkout paths -- while the sandboxed authority-repo content itself is a
    self-contained fake fixture (never the live, concurrently-mutated
    checkout), so the handshake's success depends only on the alias/bind
    wiring under test, not on the shared repo's current registry state.
    Bound at its own dedicated alias alongside (not instead of) the
    authority-repo bind."""
    real_repo = _real_repo_root()
    real_package_root = w.resolve_host_package_import_root()
    assert real_repo in real_package_root.parents

    fake_repo = _fake_authority_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id="req_b870v2_nested_bwrap", task_id="TASK_B870_V2", runner="claude_b870v2",
        topic="task_mcp", repo=Path(ws.SANDBOX_AUTHORITY_REPO), authority_repo=Path(ws.SANDBOX_AUTHORITY_REPO),
        source_graph_targets=[], session_topic="AIWorkHub worker MCP bubblewrap portability B870 V2",
        package_import_root=Path(ws.SANDBOX_PACKAGE_IMPORT_ROOT),
    )
    config = json.loads(runtime.claude_mcp_config_path.read_text(encoding="utf-8"))
    server = config["mcpServers"][w.SERVER_NAME]
    assert server["env"][w.ENV_PYTHONPATH] == ws.SANDBOX_PACKAGE_IMPORT_ROOT

    sandbox_home = ws.bubblewrap_home_env_value()
    env = get_default_environment()
    env.update(server["env"])
    env["HOME"] = sandbox_home
    inner_argv = [server["command"], *server["args"]]
    argv = [
        str(BWRAP),
        "--new-session", "--die-with-parent",
        "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/sbin", "/sbin",
        "--ro-bind", "/etc", "/etc",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--dir", "/home",
        "--bind", str(home), sandbox_home,
        "--ro-bind", str(fake_repo), ws.SANDBOX_AUTHORITY_REPO,
        "--ro-bind", str(real_package_root), ws.SANDBOX_PACKAGE_IMPORT_ROOT,
        "--chdir", ws.SANDBOX_AUTHORITY_REPO,
        "--", *inner_argv,
    ]
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=env)

    result = asyncio.run(_run_handshake(params))
    assert result["tool_names"] == set(w.MCP_TOOL_NAMES)
    assert len(w.MCP_TOOL_NAMES) == 6
    assert result["call_is_error"] is not True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
