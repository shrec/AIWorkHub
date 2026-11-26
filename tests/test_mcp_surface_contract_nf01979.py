"""NF-2026-01979: MCP surface contract -- registry/profile parity, description references, ratchet, aliases.

First stage of the TDQS tool-surface consolidation. It renames nothing; it pins
the contract later rename and merge cards rely on:

* every MCP_TOOL_PROFILES key is a registered tool and every registered tool has
  a profile row, on the SDK listing, the write-gated (read-only) listing and the
  stdlib ``-S`` stdio listing;
* no listed tool's published description names a tool its profile does not list;
* the manager listed count never rises above MANAGER_LISTED_CEILING;
* MCP_TOOL_ALIASES entries are versioned records, resolve as before, and an alias
  still present once the package reaches its ``remove_in`` fails the suite.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import __version__, mcp_tool_metadata, server  # noqa: E402

_PAYLOAD_PREFIX = "NF01979_PAYLOAD="
_METADATA_PY = _SRC / "aiworkhub" / "mcp_tool_metadata.py"
_WORKER_MCP_PY = _SRC / "aiworkhub" / "worker_ai_tools_mcp.py"
_SKILL_PROPOSE = "aiworkhub_manager_skill_propose"
_TOOL_TOKEN = re.compile(r"\b(?:aiworkhub|needfix|roadmap)_[a-z0-9_]+")
# Profiles whose listed tools are scanned for references; worker tools live on the worker MCP server.
_SCANNED_PROFILES = (mcp_tool_metadata.MCP_PROFILE_MANAGER, mcp_tool_metadata.MCP_PROFILE_DASHBOARD)
# The aliases this card found, resolving exactly as they did when they were plain strings.
_EXISTING_ALIASES = {
    "aiworkhub_router_known_repositories": "aiworkhub_repo_list",
    "aiworkhub_manager_task_archive": "aiworkhub_task_archive",
    "aiworkhub_manager_task_supersede": "aiworkhub_task_supersede",
    # NF-2026-00964: pre-rename needfix_*/roadmap_* names alias their aiworkhub_-prefixed tools.
    **{
        name: f"aiworkhub_{name}"
        for name in (
            "needfix_list", "needfix_show", "needfix_add", "needfix_update", "needfix_transition",
            "needfix_archive", "needfix_restore", "needfix_purge", "needfix_events",
            "needfix_preview_convert", "needfix_convert", "needfix_link_existing_task",
            "needfix_reopen_superseded_task_link", "roadmap_list", "roadmap_show", "roadmap_events",
            "roadmap_snapshot", "roadmap_add", "roadmap_transition", "roadmap_link_task",
        )
    },
}

_CHILD_PREAMBLE = """
try:
    import sitecustomize  # noqa: F401 - -S skips site; the AppContainer lane shim must still load
except ImportError:
    pass
import asyncio
import json
import os
from aiworkhub import server
"""

# Registered under the all surface, then listed per profile by flipping the
# surface: both listings filter at call time against the same registry.
_STDIO_BODY = """
assert not server._MCP_SDK_AVAILABLE
tools = server.mcp._tools


def listing(surface):
    os.environ["AIWORKHUB_MCP_SURFACE"] = surface
    reply = server._stdio_dispatch("AIWorkHub MCP", tools, "tools/list", None, "", 1)
    return {entry["name"]: entry["description"] for entry in reply["tools"]}


print(PREFIX + json.dumps({
    "registered": list(tools),
    "write_gate_skipped": sorted(server._WRITE_GATE_SKIPPED),
    "listed": {surface: listing(surface) for surface in ("all", "manager", "dashboard")},
}))
"""

_SDK_BODY = """
assert server._MCP_SDK_AVAILABLE


def listing(surface):
    os.environ["AIWORKHUB_MCP_SURFACE"] = surface
    return {tool.name: tool.description or "" for tool in asyncio.run(server.mcp.list_tools())}


print(PREFIX + json.dumps({
    "registered": [tool.name for tool in server.mcp._tool_manager.list_tools()],
    "write_gate_skipped": sorted(server._WRITE_GATE_SKIPPED),
    "listed": {surface: listing(surface) for surface in ("all", "manager", "dashboard")},
}))
"""

_SDK_AVAILABLE = importlib.util.find_spec("mcp") is not None
_RUNS = {
    "stdio": {"backend": "stdlib", "writes": True, "body": _STDIO_BODY},
    "stdio_read_only": {"backend": "stdlib", "writes": False, "body": _STDIO_BODY},
    **({"sdk": {"backend": "sdk", "writes": True, "body": _SDK_BODY}} if _SDK_AVAILABLE else {}),
}


def _run_child(backend: str, writes: bool, body: str) -> dict:
    # Extend PYTHONPATH, never replace it; the -S stdlib child drops every entry holding the SDK.
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if backend == "stdlib":
        inherited = [p for p in inherited if not (Path(p, "mcp").is_dir() or Path(p, "pydantic").is_dir())]
    env = dict(
        os.environ,
        AIWORKHUB_MCP_STDIO_BACKEND=backend,
        AIWORKHUB_MCP_SURFACE="all",
        PYTHONPATH=os.pathsep.join([str(_SRC), *inherited]),
    )
    env.pop("AIWORKHUB_ALLOW_WRITES", None)
    if writes:
        env["AIWORKHUB_ALLOW_WRITES"] = "1"
    script = f"PREFIX = {_PAYLOAD_PREFIX!r}\n{_CHILD_PREAMBLE}{body}"
    argv = [sys.executable, *(["-S"] if backend == "stdlib" else []), "-c", script]
    completed = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr[-4000:]
    lines = [line for line in completed.stdout.splitlines() if line.startswith(_PAYLOAD_PREFIX)]
    assert len(lines) == 1, completed.stdout[-2000:]
    return json.loads(lines[0][len(_PAYLOAD_PREFIX):])


@pytest.fixture(scope="module")
def runs() -> dict:
    with ThreadPoolExecutor(max_workers=max(1, min(len(_RUNS), os.cpu_count() or 1))) as pool:
        futures = {
            label: pool.submit(_run_child, spec["backend"], spec["writes"], spec["body"])
            for label, spec in _RUNS.items()
        }
        return {label: future.result() for label, future in futures.items()}


def _worker_tool_names() -> frozenset:
    # The worker MCP server owns these rows; read its literal without importing it.
    for node in ast.parse(_WORKER_MCP_PY.read_text(encoding="utf-8")).body:
        targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        if any(getattr(target, "id", None) == "MCP_TOOL_NAMES" for target in targets):
            names = frozenset(ast.literal_eval(node.value))
            assert names, "worker MCP_TOOL_NAMES is empty"
            return names
    raise AssertionError(f"MCP_TOOL_NAMES is not assigned at module level in {_WORKER_MCP_PY}")


def _parity_problems(
    registered: set, profiles: Mapping, external: frozenset, gate_skipped: frozenset = frozenset()
) -> dict:
    """Profile rows no registry serves, and registered tools with no profile row.

    ``external`` rows are served by another MCP server (the worker one).
    ``gate_skipped`` rows are write-gated tools this read-only process skipped:
    they must still have a row, and are not missing registrations.
    """
    served = set(registered) | set(external) | set(gate_skipped)
    return {
        "unregistered_profile_keys": sorted(set(profiles) - served),
        "registered_without_profile_row": sorted(set(registered) - set(profiles)),
        "gate_skipped_without_profile_row": sorted(set(gate_skipped) - set(profiles)),
    }


_NO_PROBLEMS = {
    "unregistered_profile_keys": [],
    "registered_without_profile_row": [],
    "gate_skipped_without_profile_row": [],
}


def _listed_on(profile: str) -> set:
    return {
        name
        for name, entry in mcp_tool_metadata.MCP_TOOL_PROFILES.items()
        if entry.get(profile) == mcp_tool_metadata.MCP_VISIBILITY_LISTED
    }


def _reference_problems(listed: Mapping, known: set) -> dict:
    """For each listed tool, the known tool names its description names that ``listed`` does not hold."""
    problems = {}
    for name, description in listed.items():
        referenced = set(_TOOL_TOKEN.findall(description or "")) - {name}
        unlisted = sorted(ref for ref in referenced if ref in known and ref not in listed)
        if unlisted:
            problems[name] = unlisted
    return problems


def _version(text: str) -> tuple:
    parts = str(text).strip().split(".")
    assert parts and all(part.isdigit() for part in parts), f"unparseable release version {text!r}"
    return tuple(int(part) for part in parts)


# --- (1) ghost entry ---------------------------------------------------------


def test_skill_propose_is_listed_on_manager() -> None:
    entry = mcp_tool_metadata.MCP_TOOL_PROFILES[_SKILL_PROPOSE]
    assert entry[mcp_tool_metadata.MCP_PROFILE_MANAGER] == mcp_tool_metadata.MCP_VISIBILITY_LISTED
    assert server.mcp_tool_listed(_SKILL_PROPOSE, "manager") is True


def test_skill_propose_is_in_the_live_manager_listings(runs: dict) -> None:
    for label, run in runs.items():
        assert _SKILL_PROPOSE in run["listed"]["manager"], label


# --- (2) parity --------------------------------------------------------------


def test_parity_helper_fails_for_an_unregistered_key_and_for_a_rowless_tool() -> None:
    profiles = {"aiworkhub_a": {"manager": "listed"}, "aiworkhub_b": {"manager": "listed"}}
    external = frozenset({"aiworkhub_worker_x"})
    profiles_with_worker = {**profiles, "aiworkhub_worker_x": {"worker": "listed"}}
    assert _parity_problems({"aiworkhub_a", "aiworkhub_b"}, profiles_with_worker, external) == _NO_PROBLEMS
    assert _parity_problems({"aiworkhub_a"}, profiles_with_worker, external)["unregistered_profile_keys"] == [
        "aiworkhub_b"
    ]
    rowless = _parity_problems({"aiworkhub_a", "aiworkhub_b", "aiworkhub_c"}, profiles_with_worker, external)
    assert rowless["registered_without_profile_row"] == ["aiworkhub_c"]
    # A write-gated tool skipped while read-only is not a missing registration, but still needs its row.
    assert _parity_problems(
        {"aiworkhub_a"}, profiles_with_worker, external, frozenset({"aiworkhub_b"})
    ) == _NO_PROBLEMS
    assert _parity_problems(
        {"aiworkhub_a", "aiworkhub_b"}, profiles_with_worker, external, frozenset({"aiworkhub_z"})
    )["gate_skipped_without_profile_row"] == ["aiworkhub_z"]


@pytest.mark.parametrize("label", list(_RUNS))
def test_registry_and_profile_table_are_in_parity(runs: dict, label: str) -> None:
    run = runs[label]
    registered = set(run["registered"])
    assert registered and len(registered) == len(run["registered"])
    gate_skipped = frozenset(run["write_gate_skipped"])
    if _RUNS[label]["writes"]:
        assert gate_skipped == frozenset(), label
    else:
        assert gate_skipped, f"{label}: a read-only process skipped no write-gated tool"
        assert not gate_skipped & registered, label
    problems = _parity_problems(registered, mcp_tool_metadata.MCP_TOOL_PROFILES, _worker_tool_names(), gate_skipped)
    assert problems == _NO_PROBLEMS, (label, problems)
    # The all surface lists exactly what this process registered.
    assert set(run["listed"]["all"]) == registered, label


def test_sdk_and_stdio_register_the_same_tools(runs: dict) -> None:
    if "sdk" not in runs:
        pytest.skip("mcp SDK is not importable in this interpreter")
    assert set(runs["sdk"]["registered"]) == set(runs["stdio"]["registered"])
    for surface in ("manager", "dashboard"):
        assert set(runs["sdk"]["listed"][surface]) == set(runs["stdio"]["listed"][surface]), surface


def test_reference_helper_flags_a_description_naming_an_unlisted_tool() -> None:
    known = {"aiworkhub_a", "aiworkhub_b", "needfix_c"}
    listed = {"aiworkhub_a": "Then call aiworkhub_b or needfix_c; needfix_id is a parameter."}
    assert _reference_problems(listed, known) == {"aiworkhub_a": ["aiworkhub_b", "needfix_c"]}
    assert _reference_problems({**listed, "aiworkhub_b": "", "needfix_c": "see aiworkhub_a"}, known) == {}


@pytest.mark.parametrize("label", list(_RUNS))
def test_no_listed_description_names_a_tool_its_profile_does_not_list(runs: dict, label: str) -> None:
    run = runs[label]
    known = set(mcp_tool_metadata.MCP_TOOL_PROFILES) | set(run["registered"]) | set(
        mcp_tool_metadata.MCP_TOOL_ALIASES
    )
    for profile in _SCANNED_PROFILES:
        listed = run["listed"][profile]
        assert listed, (label, profile)
        assert set(listed) <= _listed_on(profile), (label, profile)
        assert _reference_problems(listed, known) == {}, (label, profile)


def _registered_functions() -> dict:
    # The registered name -> function map; a listed name need not be a server attribute
    # (NF-2026-00964: aiworkhub_needfix_* is registered from the Python function needfix_*).
    if server._MCP_SDK_AVAILABLE:
        return {name: tool.fn for name, tool in server.mcp._tool_manager._tools.items()}
    return dict(server.mcp._tools)


def test_no_listed_docstring_names_a_tool_its_profile_does_not_list() -> None:
    # In-process mirror of the published scan, over the raw docstrings this process holds.
    known = set(mcp_tool_metadata.MCP_TOOL_PROFILES) | set(mcp_tool_metadata.MCP_TOOL_ALIASES)
    registered = _registered_functions()
    assert registered
    for profile in _SCANNED_PROFILES:
        listed = {
            name: registered[name].__doc__ or ""
            for name in _listed_on(profile)
            if callable(registered.get(name))
        }
        assert listed, profile
        assert _reference_problems(listed, known) == {}, profile


# --- (3) ratchet -------------------------------------------------------------


def test_manager_listed_ceiling_is_one_int_literal() -> None:
    for node in ast.parse(_METADATA_PY.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "MANAGER_LISTED_CEILING" for target in node.targets
        ):
            assert isinstance(node.value, ast.Constant) and type(node.value.value) is int
            return
    raise AssertionError("MANAGER_LISTED_CEILING is not a module-level int literal")


def test_manager_listed_count_never_exceeds_the_ceiling(runs: dict) -> None:
    ceiling = mcp_tool_metadata.MANAGER_LISTED_CEILING
    measured = len(_listed_on(mcp_tool_metadata.MCP_PROFILE_MANAGER))
    assert measured <= ceiling
    # No slack: a card that unlists a manager tool lowers the ceiling with it.
    assert ceiling == measured
    for label, run in runs.items():
        assert len(run["listed"]["manager"]) <= ceiling, label


# --- (4) aliases -------------------------------------------------------------


def test_alias_entries_are_versioned_records() -> None:
    assert mcp_tool_metadata.MCP_ALIAS_RECORD_KEYS == ("canonical", "bound_kwargs", "since", "remove_in")
    assert isinstance(mcp_tool_metadata.MCP_TOOL_ALIASES, MappingProxyType)
    for alias, record in mcp_tool_metadata.MCP_TOOL_ALIASES.items():
        assert isinstance(record, Mapping), alias
        assert tuple(record) == mcp_tool_metadata.MCP_ALIAS_RECORD_KEYS, alias
        assert isinstance(record["canonical"], str) and record["canonical"] != alias, alias
        assert isinstance(record["bound_kwargs"], Mapping), alias
        assert _version(record["since"]) < _version(record["remove_in"]), alias


def test_existing_aliases_resolve_exactly_as_before() -> None:
    resolved = {alias: mcp_tool_metadata.alias_canonical(alias) for alias in mcp_tool_metadata.MCP_TOOL_ALIASES}
    assert resolved == _EXISTING_ALIASES
    for alias, canonical in _EXISTING_ALIASES.items():
        assert mcp_tool_metadata.canonical_tool_name(alias) == canonical
        assert dict(mcp_tool_metadata.MCP_TOOL_ALIASES[alias]["bound_kwargs"]) == {}
        assert mcp_tool_metadata.alias_description(alias, "Body.") == (
            f"Deprecated alias of {canonical}; call {canonical}. Body."
        )
    for name in ("aiworkhub_repo_list", "aiworkhub_no_such_tool", None, 5):
        assert mcp_tool_metadata.alias_canonical(name) is None
        assert mcp_tool_metadata.canonical_tool_name(name) == name
    assert mcp_tool_metadata.alias_description("aiworkhub_repo_list", "Body.") == "Body."


def test_no_alias_outlives_its_remove_in_release() -> None:
    current = _version(__version__)
    overdue = {
        alias: record["remove_in"]
        for alias, record in mcp_tool_metadata.MCP_TOOL_ALIASES.items()
        if current >= _version(record["remove_in"])
    }
    assert overdue == {}, f"package {__version__} reached remove_in for {overdue}: delete those aliases"


_TEMP_ALIAS = "aiworkhub_nf01979_temporary_alias"
_TEMP_CANONICAL = "aiworkhub_nf01979_temporary_canonical"
_TEMP_RECORD = MappingProxyType({
    "canonical": _TEMP_CANONICAL,
    "bound_kwargs": MappingProxyType({"mode": "bound", "flag": True}),
    "since": "0.13.7",
    "remove_in": "0.14.0",
})


def _temporary_canonical(item: str, mode: str = "default", flag: bool = False) -> dict:
    """READ-ONLY: temporary canonical tool for the NF-2026-01979 alias test."""
    return {"item": item, "mode": mode, "flag": flag}


def test_alias_delegate_reaches_the_canonical_with_bound_kwargs_and_stamps_dict_replies(monkeypatch) -> None:
    assert _TEMP_ALIAS not in mcp_tool_metadata.MCP_TOOL_ALIASES
    monkeypatch.setattr(
        mcp_tool_metadata,
        "MCP_TOOL_ALIASES",
        MappingProxyType({**mcp_tool_metadata.MCP_TOOL_ALIASES, _TEMP_ALIAS: _TEMP_RECORD}),
    )
    assert mcp_tool_metadata.canonical_tool_name(_TEMP_ALIAS) == _TEMP_CANONICAL
    stamp = {"alias": _TEMP_ALIAS, "canonical": _TEMP_CANONICAL, "remove_in": "0.14.0"}

    registered: list = []

    def registrar(**kwargs):
        def decorate(function):
            registered.append((kwargs, function))
            return function

        return decorate

    delegate = server.register_deprecated_alias(_TEMP_ALIAS, _temporary_canonical, registrar=registrar)
    assert registered == [({"name": _TEMP_ALIAS}, delegate)]
    assert delegate.__name__ == _TEMP_ALIAS

    reply = delegate(item="x")
    assert reply == {"item": "x", "mode": "bound", "flag": True, mcp_tool_metadata.MCP_ALIAS_REPLY_KEY: stamp}
    assert mcp_tool_metadata.MCP_ALIAS_REPLY_KEY == "deprecated_alias"
    # Caller kwargs win over bound_kwargs.
    assert delegate(item="y", mode="caller") == {
        "item": "y", "mode": "caller", "flag": True, "deprecated_alias": stamp,
    }
    # The canonical's own reply is not mutated, and a non-dict reply passes through unstamped.
    plain = server.deprecated_alias_delegate(_TEMP_ALIAS, lambda **kwargs: ["list", kwargs["mode"]])
    assert plain() == ["list", "bound"]


def test_alias_delegate_refuses_an_unrecorded_or_incomplete_alias() -> None:
    with pytest.raises(KeyError):
        server.deprecated_alias_delegate(_TEMP_ALIAS, _temporary_canonical)
    incomplete = MappingProxyType({"canonical": _TEMP_CANONICAL, "bound_kwargs": {}})
    with pytest.raises(ValueError, match="since"):
        server.deprecated_alias_delegate(_TEMP_ALIAS, _temporary_canonical, incomplete)
