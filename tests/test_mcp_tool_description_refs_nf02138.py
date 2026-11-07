"""NF-2026-02138: a tool description names only tools its own profile lists, and the manager bootstrap contract names only manager-listed tools.

Each profile is measured in a child process (the surface is read at import time), with the same environment
discipline as tests/test_mcp_tool_profiles_rm00078.py, and the full tools/list JSON is compared, not names alone.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

from aiworkhub import core, task_store  # noqa: E402

_PAYLOAD_PREFIX = "NF02138_PAYLOAD="
_PROFILES = ("manager", "worker", "dashboard", "all")

# A tool reference is any aiworkhub_* identifier, except dotted schema ids (aiworkhub_x.v1) and *_vN suffixes.
_TOOL_TOKEN = re.compile(r"\baiworkhub_[a-z0-9_]+\b(?![.][A-Za-z0-9])")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SCHEMA_VERSION = re.compile(r"_v\d+$")

# Suffix-collision allowlist: parameter/field names that happen to be a '_'-boundary suffix of a
# registered tool name but are not a tool reference in context. Each entry names its reason.
_SUFFIX_ALLOWLIST = frozenset({
    "review_queue",  # facet key of the aiworkhub_completion_inbox reply (the tool is named in full beside it)
    "task_health",  # section key of the aiworkhub_manager_bootstrap reply (task_hygiene/task_health bodies)
    "learning_commit",  # reply key under which accept/reject return the inline learning-commit reply
})

_CHILD = r"""
try:
    import sitecustomize  # noqa: F401 - -S skips site; the AppContainer lane shim must still load
except ImportError:
    pass
import json
from aiworkhub import server

assert not server._MCP_SDK_AVAILABLE
real = server.mcp._tools


def rpc(method, params=None):
    try:
        return {"result": server._stdio_dispatch("AIWorkHub MCP", real, method, params, "", 1)}
    except server._StdioProtocolError as exc:
        return {"error": {"code": exc.code, "message": exc.message}}


report = {"surface": server.mcp_surface(), "tools": rpc("tools/list")["result"]["tools"]}
print(PREFIX + json.dumps(report))
"""


def _run_profile(repo_root: Path, surface: str, writes: str = "1") -> list:
    # Extend PYTHONPATH, never replace it, and drop the SDK entries -S could not hide.
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    inherited = [p for p in inherited if not (Path(p, "mcp").is_dir() or Path(p, "pydantic").is_dir())]
    env = dict(
        os.environ,
        AIWORKHUB_MCP_STDIO_BACKEND="stdlib",
        AIWORKHUB_REPO_ROOT=str(repo_root),
        AIWORKHUB_REPO=str(repo_root),
        AIWORKHUB_ALLOW_WRITES=writes,
        AIWORKHUB_MCP_SURFACE=surface,
        PYTHONPATH=os.pathsep.join([str(_SRC), *inherited]),
    )
    script = f"PREFIX = {_PAYLOAD_PREFIX!r}\n{_CHILD}"
    completed = subprocess.run(
        [sys.executable, "-S", "-c", script], env=env, capture_output=True, text=True, timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    lines = completed.stdout.splitlines()
    assert len(lines) == 1 and lines[0].startswith(_PAYLOAD_PREFIX), completed.stdout[-2000:]
    report = json.loads(lines[0][len(_PAYLOAD_PREFIX):])
    assert report["surface"] == surface, report["surface"]
    return report["tools"]


@pytest.fixture(scope="module")
def listings(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("nf02138_repo")
    assert task_store.initialize_repository(root)["ok"]
    cases = [("manager", "1"), ("manager", "0"), ("worker", "1"), ("dashboard", "1"), ("all", "1")]
    with ThreadPoolExecutor(max_workers=max(1, min(len(cases), os.cpu_count() or 1))) as pool:
        futures = {key: pool.submit(_run_profile, root, key[0], key[1]) for key in cases}
        return {key: future.result() for key, future in futures.items()}


def _tool_tokens(text: str) -> set:
    return {
        match.group(0)
        for match in _TOOL_TOKEN.finditer(text)
        if not _SCHEMA_VERSION.search(match.group(0))
    }


def _suffix_refs(text: str, registered: frozenset) -> dict:
    # word -> the registered tool names it is a proper '_'-boundary suffix of; 1-segment words never count.
    found = {}
    for word in set(_WORD.findall(text)):
        if "_" not in word.strip("_") or word in _SUFFIX_ALLOWLIST:
            continue
        owners = sorted(name for name in registered if name != word and name.endswith("_" + word))
        if owners:
            found[word] = owners
    return found


def _references(text: str, registered: frozenset) -> set:
    # A bare word counts when it is the exact name of a registered tool (catches needfix_*/roadmap_* names).
    return _tool_tokens(text) | (set(_WORD.findall(text)) & registered)


def test_every_tool_description_names_only_tools_its_profile_lists(listings):
    registered = frozenset(entry["name"] for entry in listings[("all", "1")])
    violations = []
    for key, entries in listings.items():
        profile = key[0]
        listed = {entry["name"] for entry in entries}
        for entry in entries:
            # The whole published entry: description, parameter descriptions and annotations (values only).
            refs = set()
            for text in _string_values(entry):
                refs |= _references(text, registered)
            for ref in sorted(refs - listed):
                violations.append(f"{key}: {entry['name']} -> {ref}")
    assert not violations, (
        f"{len(violations)} description reference(s) name a tool the profile does not list:\n"
        + "\n".join(violations)
    )


def test_no_description_names_a_tool_by_a_short_suffix(listings):
    # A 2+-segment word that is a proper '_'-boundary suffix of a registered tool name ('all' profile) is a
    # short-form reference (review_finding_dispose for aiworkhub_manager_review_finding_dispose): unless the
    # word is itself a listed tool name or an allowlisted field name, the full listed name must be written.
    registered = frozenset(entry["name"] for entry in listings[("all", "1")])
    violations: dict[str, set] = {}
    for key, entries in listings.items():
        listed = {entry["name"] for entry in entries}
        for entry in entries:
            # inputSchema text is frozen (FROZEN_SCHEMA_FINGERPRINTS); the full-name test above still scans it.
            for text in _string_values({k: v for k, v in entry.items() if k != "inputSchema"}):
                for word, owners in _suffix_refs(text, registered).items():
                    if word not in listed:
                        label = f"{entry['name']} -> {word} (suffix of {', '.join(owners)})"
                        violations.setdefault(label, set()).add(f"{key[0]}/{key[1]}")
    if violations:
        pytest.fail(
            f"{len(violations)} short-form suffix reference(s):\n"
            + "\n".join(f"{label} [{' '.join(sorted(keys))}]" for label, keys in sorted(violations.items())),
            pytrace=False,
        )


def _string_values(node):
    # Dict keys are structural labels (the "aiworkhub_system" role group), not tool references: scan values only.
    if isinstance(node, dict):
        for value in node.values():
            yield from _string_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _string_values(item)
    elif isinstance(node, str):
        yield node


def test_manager_bootstrap_contract_names_only_manager_listed_tools(listings):
    manager = {entry["name"] for entry in listings[("manager", "1")]}
    named = set()
    for text in _string_values(core._manager_contract()):
        named |= _tool_tokens(text)
    missing = sorted(named - manager)
    assert not missing, (
        "manager bootstrap contract names tools the manager profile does not list: " + ", ".join(missing)
    )
