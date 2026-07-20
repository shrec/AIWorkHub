from __future__ import annotations

import importlib
import sys
import types
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PYPROJECT = ROOT / "pyproject.toml"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class _FastMCPStub:
    def __init__(self, name: str):
        self.name = name
        self.registered_tools: list[str] = []
        self.run_called = False

    def tool(self, *args, **kwargs):
        def decorate(func):
            self.registered_tools.append(kwargs.get("name") or func.__name__)
            return func

        return decorate

    def run(self):
        self.run_called = True


def _install_server_import_stubs(monkeypatch):
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = _FastMCPStub
    server_pkg = types.ModuleType("mcp.server")
    server_pkg.fastmcp = fastmcp_mod
    mcp_pkg = types.ModuleType("mcp")
    mcp_pkg.server = server_pkg
    monkeypatch.setitem(sys.modules, "mcp", mcp_pkg)
    monkeypatch.setitem(sys.modules, "mcp.server", server_pkg)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_mod)

    dashboard_mod = types.ModuleType("geoai_task_mcp.dashboard_mcp_app")

    def register(mcp):
        @mcp.tool()
        def geoai_dashboard_snapshot():
            return {"ok": True}

        @mcp.tool()
        def geoai_dashboard_openapi():
            return {"ok": True}

        @mcp.tool()
        def geoai_dashboard_static_manifest():
            return {"ok": True}

    dashboard_mod.register = register
    monkeypatch.setitem(sys.modules, "geoai_task_mcp.dashboard_mcp_app", dashboard_mod)
    package = sys.modules.get("geoai_task_mcp")
    if package is not None:
        monkeypatch.setattr(package, "dashboard_mcp_app", dashboard_mod, raising=False)

    deepseek_mod = types.ModuleType("geoai_task_mcp.deepseek_credentials")
    deepseek_mod.adapter_readiness = lambda repo=None: {"ok": True, "repo": str(repo) if repo else None}
    deepseek_mod.main = lambda: None
    monkeypatch.setitem(sys.modules, "geoai_task_mcp.deepseek_credentials", deepseek_mod)
    if package is not None:
        monkeypatch.setattr(package, "deepseek_credentials", deepseek_mod, raising=False)


def _drop_imports():
    for name in [
        "aiworkinghub.server",
        "aiworkinghub",
        "geoai_task_mcp.server",
    ]:
        sys.modules.pop(name, None)
    legacy_package = sys.modules.get("geoai_task_mcp")
    if legacy_package is not None:
        legacy_package.__dict__.pop("server", None)
    branded_package = sys.modules.get("aiworkinghub")
    if branded_package is not None:
        branded_package.__dict__.pop("server", None)


@pytest.fixture(autouse=True)
def _isolate_server_alias_imports():
    yield
    _drop_imports()


def test_aiworkinghub_server_alias_is_same_fastmcp_server(monkeypatch):
    _drop_imports()
    _install_server_import_stubs(monkeypatch)

    legacy = importlib.import_module("geoai_task_mcp.server")
    branded = importlib.import_module("aiworkinghub.server")

    assert branded.mcp is legacy.mcp
    assert branded.main is legacy.main
    assert branded.geoai_task_health is legacy.geoai_task_health
    assert branded.dashboard_mcp_app is legacy.dashboard_mcp_app
    assert branded.core is legacy.core


def test_dashboard_mcp_app_registration_survives_legacy_import(monkeypatch):
    _drop_imports()
    _install_server_import_stubs(monkeypatch)

    legacy = importlib.import_module("geoai_task_mcp.server")

    assert hasattr(legacy, "dashboard_mcp_app")
    assert legacy.dashboard_mcp_app is sys.modules["geoai_task_mcp.dashboard_mcp_app"]
    assert "geoai_dashboard_snapshot" in legacy.mcp.registered_tools
    assert "geoai_dashboard_openapi" in legacy.mcp.registered_tools
    assert "geoai_dashboard_static_manifest" in legacy.mcp.registered_tools


def test_legacy_and_branded_console_entry_points_resolve_to_same_main():
    scripts = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["scripts"]

    assert scripts["geoai-task-mcp"] == "geoai_task_mcp.server:main"
    assert scripts["aiworkinghub"] == scripts["geoai-task-mcp"]
    assert scripts["aiwh"] == scripts["geoai-task-mcp"]
    assert scripts["aiworkinghub-mcp"] == scripts["geoai-task-mcp"]


def test_aiworkinghub_package_exports_canonical_version():
    import aiworkinghub
    import geoai_task_mcp

    assert aiworkinghub.__version__ == geoai_task_mcp.__version__
