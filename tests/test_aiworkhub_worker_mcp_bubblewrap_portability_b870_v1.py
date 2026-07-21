"""B870 V1 (REPAIRED by V2): bubblewrap package-import-root portability.

The original V1 fix rebased ``env[PYTHONPATH]`` onto whichever
``authority_repo`` binding the caller passed, using ``Path(__file__).parents``
to guess both the package's own offset AND a fixed ``tools/geoai-task-mcp``
repository-layout assumption. That guess breaks for a standalone
``<repo>/src/aiworkhub`` checkout, silently mis-resolves in a differently
laid-out monorepo, and cannot work at all when the package is bundled or
installed OUTSIDE the active project repository -- ``authority_repo`` may not
even contain the package. This V2 repair removes that guess entirely:
``generate_worker_mcp_runtime`` now takes an explicit ``package_import_root``
parameter and never derives it from ``authority_repo`` or a parent-depth
count. ``worker_workspace.provision_worker_mcp_runtime`` resolves the real
host package root via ``resolve_host_package_import_root()`` and substitutes
the dedicated ``SANDBOX_PACKAGE_IMPORT_ROOT`` alias only for the bubblewrap
backend; ``sandbox_argv`` binds that same real host directory read-only at
that exact alias, independent of the ``SANDBOX_AUTHORITY_REPO`` bind. These
tests check that substitution (without spawning a sandbox), then drive a real
``mcp.ClientSession`` handshake through an actual bubblewrap mount namespace
that binds the package import root ONLY at ``/aiworkhub-package-root`` --
deliberately kept OUTSIDE the bound authority repo, proving the two aliases
are independent.
"""

from __future__ import annotations

import asyncio
import json
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


def _write_tool(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _authority_repo(tmp_path: Path) -> Path:
    """Same fake-authority-repo shape as the B833/B834/B869 fixtures --
    deliberately WITHOUT any copy/symlink of the package source beneath it,
    since the package import root is no longer expressed as an offset inside
    authority_repo at all."""
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
        "import sys\nprint('[module] bounded KB context over real bubblewrap-alias pipe')\n",
    )
    return repo


def test_pythonpath_uses_dedicated_bubblewrap_package_alias_not_authority_repo(tmp_path: Path) -> None:
    """Core V2 repair, checked without spawning any sandbox:
    ``provision_worker_mcp_runtime`` must resolve PYTHONPATH to the dedicated
    ``SANDBOX_PACKAGE_IMPORT_ROOT`` alias under bubblewrap -- never an offset
    beneath ``SANDBOX_AUTHORITY_REPO`` (the rejected V1 shape), and never the
    raw ambient host ``__file__`` location this process happens to run from."""
    repo = tmp_path / "authority_repo"
    repo.mkdir(parents=True)
    workspace = ws.WorkerWorkspace(
        request_id="req_b870_alias",
        repo=repo,
        path=tmp_path / "worktree",
        home=tmp_path / "home",
        allowed_writes=("x.txt",),
        parent_baseline={},
        workspace_baseline={},
    )
    runtime = ws.provision_worker_mcp_runtime(
        workspace,
        request_id="req_b870_alias",
        task_id="TASK_B870",
        runner="claude_b870",
        topic="task_mcp",
        backend="bubblewrap",
        source_graph_targets=[],
        session_topic="AIWorkHub worker MCP bubblewrap portability B870",
    )
    config = json.loads(runtime.claude_mcp_config_path.read_text(encoding="utf-8"))
    env = config["mcpServers"][w.SERVER_NAME]["env"]
    assert env[w.ENV_PYTHONPATH] == ws.SANDBOX_PACKAGE_IMPORT_ROOT
    assert not env[w.ENV_PYTHONPATH].startswith(ws.SANDBOX_AUTHORITY_REPO)
    ambient_host_src = str(Path(__file__).resolve().parents[1] / "src")
    assert env[w.ENV_PYTHONPATH] != ambient_host_src


def test_pythonpath_unchanged_for_landlock_direct_real_host_package_root(tmp_path: Path) -> None:
    """Landlock/direct backends use the real, resolved host package import
    root directly -- byte-identical to this module's own real ``src``
    directory, with no rebasing onto ``authority_repo`` at all."""
    repo = tmp_path / "authority_repo"
    repo.mkdir(parents=True)
    workspace = ws.WorkerWorkspace(
        request_id="req_b870_direct",
        repo=repo,
        path=tmp_path / "worktree",
        home=tmp_path / "home",
        allowed_writes=("x.txt",),
        parent_baseline={},
        workspace_baseline={},
    )
    runtime = ws.provision_worker_mcp_runtime(
        workspace,
        request_id="req_b870_direct",
        task_id="TASK_B870",
        runner="claude_b870",
        topic="task_mcp",
        backend="landlock",
        source_graph_targets=[],
        session_topic="AIWorkHub worker MCP bubblewrap portability B870",
    )
    config = json.loads(runtime.claude_mcp_config_path.read_text(encoding="utf-8"))
    env = config["mcpServers"][w.SERVER_NAME]["env"]
    assert Path(env[w.ENV_PYTHONPATH]).resolve() == _SRC.resolve()


async def _run_handshake(params: StdioServerParameters) -> dict:
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=REQUEST_TIMEOUT) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            call = await session.call_tool(
                "aiworkhub_worker_kb_search", {"query": "bubblewrap alias smoke"},
                read_timeout_seconds=REQUEST_TIMEOUT,
            )
            return {"tool_names": names, "call_is_error": call.isError}


def _bubblewrap_argv(*, home: Path, authority_repo: Path, package_import_root: Path, inner_argv: list[str]) -> list[str]:
    """Mirror ``worker_workspace.sandbox_argv``'s bubblewrap branch: the host
    repo is bound at ``SANDBOX_AUTHORITY_REPO`` and the package import root is
    bound SEPARATELY at ``SANDBOX_PACKAGE_IMPORT_ROOT`` -- the real host path
    of either is absent inside this mount namespace, so a handshake that
    succeeds here proves both the alias substitution and their independence
    (neither is a subpath of the other)."""
    sandbox_home = ws.bubblewrap_home_env_value()
    return [
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
        "--ro-bind", str(authority_repo), ws.SANDBOX_AUTHORITY_REPO,
        "--ro-bind", str(package_import_root), ws.SANDBOX_PACKAGE_IMPORT_ROOT,
        "--chdir", ws.SANDBOX_AUTHORITY_REPO,
        "--", *inner_argv,
    ]


_BWRAP_USABLE = BWRAP.exists() and ws._bubblewrap_usable(BWRAP)


@pytest.mark.skipif(
    not _BWRAP_USABLE,
    reason="bubblewrap unusable on this host (unprivileged userns restricted -- "
    "same probe worker_workspace.select_sandbox_backend() uses to fall back "
    "to landlock in production)",
)
def test_worker_mcp_server_handshakes_over_real_bubblewrap_package_alias(tmp_path: Path) -> None:
    """Real stdio proof under an actual bubblewrap mount namespace: the
    package import root is bound ONLY at ``/aiworkhub-package-root`` -- a
    directory deliberately kept OUTSIDE the bound authority repo -- so the
    generated config's PYTHONPATH must resolve the ``aiworkhub`` package
    strictly through that dedicated alias for this handshake to succeed."""
    authority_repo = _authority_repo(tmp_path)
    package_root_outside_repo = tmp_path / "bundled_package_outside_repo" / "src"
    package_root_outside_repo.parent.mkdir(parents=True, exist_ok=True)
    package_root_outside_repo.symlink_to(_SRC, target_is_directory=True)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id="req_b870_bwrap", task_id="TASK_B870", runner="claude_b870",
        topic="task_mcp", repo=Path(ws.SANDBOX_AUTHORITY_REPO), authority_repo=Path(ws.SANDBOX_AUTHORITY_REPO),
        source_graph_targets=[], session_topic="AIWorkHub worker MCP bubblewrap portability B870",
        package_import_root=Path(ws.SANDBOX_PACKAGE_IMPORT_ROOT),
    )
    config = json.loads(runtime.claude_mcp_config_path.read_text(encoding="utf-8"))
    server = config["mcpServers"][w.SERVER_NAME]
    assert server["env"][w.ENV_PYTHONPATH] == ws.SANDBOX_PACKAGE_IMPORT_ROOT

    env = get_default_environment()
    env.update(server["env"])
    env["HOME"] = ws.bubblewrap_home_env_value()
    inner_argv = [server["command"], *server["args"]]
    argv = _bubblewrap_argv(
        home=home, authority_repo=authority_repo,
        package_import_root=package_root_outside_repo, inner_argv=inner_argv,
    )
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=env)

    result = asyncio.run(_run_handshake(params))
    assert result["tool_names"] == set(w.MCP_TOOL_NAMES)
    assert len(w.MCP_TOOL_NAMES) == 6
    assert result["call_is_error"] is not True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
