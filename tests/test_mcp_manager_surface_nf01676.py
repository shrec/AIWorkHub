"""NF-2026-01676: AIWORKHUB_MCP_SURFACE=manager keeps webview-only tools and duplicate payloads away from LLM clients."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import server, task_store  # noqa: E402
from aiworkhub.tool_recovery import unknown_tool_message  # noqa: E402

_EXTENSION_JS = _ROOT / "vscode-extension" / "extension.js"
_PAYLOAD_PREFIX = "NF01676_PAYLOAD="
_DASHBOARD = "aiworkhub_dashboard_"
_KEEP = {"aiworkhub_dashboard_needfix_transition", "aiworkhub_dashboard_snapshot"}
_TEMPLATE_LIST = "aiworkhub_task_template_list"
_PROBE_ERROR = "unexpected_arguments:['__nf01676_probe__']"

_SYNTHETIC_HIDDEN = "aiworkhub_dashboard_synthetic_hidden"
_SYNTHETIC_ALL = [
    "synthetic_echo", "synthetic_accented", "synthetic_list", "synthetic_int", "synthetic_none",
    "synthetic_text", "synthetic_path", "synthetic_failing", _SYNTHETIC_HIDDEN,
    "aiworkhub_dashboard_snapshot", "aiworkhub_dashboard_needfix_transition",
    "aiworkhub_dashboard", "aiworkhub_manager_dashboard_view",
]
_SYNTHETIC_MANAGER = [name for name in _SYNTHETIC_ALL if name != _SYNTHETIC_HIDDEN]
_SYNTHETIC_RESULTS = {
    "synthetic_echo": {"echo": "plain", "nested": {"items": [1, 2, 3], "ratio": 0.5, "flag": True, "none": None}},
    "synthetic_accented": {"word": "café", "quote": 'say "hi"'},
    "synthetic_list": [1, "two", {"three": 3}],
    "synthetic_int": 7,
    "synthetic_none": None,
    "synthetic_text": "plain",
    "synthetic_path": {"path": str(Path("alpha") / "beta")},
    _SYNTHETIC_HIDDEN: {"tool": "hidden"},
    "aiworkhub_dashboard_snapshot": {"tool": "snapshot"},
    "aiworkhub_dashboard_needfix_transition": {"tool": "transition"},
    "aiworkhub_dashboard": {"tool": "bare"},
    "aiworkhub_manager_dashboard_view": {"tool": "view"},
}
_MANAGER_VISIBILITY = {
    "aiworkhub_dashboard_health": False,
    "aiworkhub_dashboard_needfix_transition": True,
    "aiworkhub_dashboard_snapshot": True,
    "aiworkhub_dashboard": True,
    "aiworkhub_manager_dashboard_view": True,
    "aiworkhub_task_create": True,
    "needfix_add": True,
}

_CHILD_PREAMBLE = """
try:
    import sitecustomize  # noqa: F401 - -S skips site; the AppContainer lane shim must still load
except ImportError:
    pass
import asyncio
import io
import json
import os
from aiworkhub import server
"""

_STDLIB_BODY = r"""
import hashlib
import sys
from pathlib import Path

ARGS = json.loads(os.environ["NF01676_ARGS"])
SERVER_NAME = "AIWorkHub MCP"
DASHBOARD = "aiworkhub_dashboard_"
KEEP = ("aiworkhub_dashboard_needfix_transition", "aiworkhub_dashboard_snapshot")
PROBE = {"__nf01676_probe__": 1}
assert not server._MCP_SDK_AVAILABLE
real = server.mcp._tools
probe_names = ARGS.get("probe_names") or [name for name in real if name.startswith(DASHBOARD)]
hidden_example = ARGS.get("hidden_example") or [name for name in probe_names if name not in KEEP][0]


def rpc(registry, method, params=None, request_id=1):
    try:
        result = server._stdio_dispatch(SERVER_NAME, registry, method, params, "", request_id)
        reply = {"jsonrpc": "2.0", "id": request_id, "result": result}
    except server._StdioProtocolError as exc:
        reply = {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message}}
    sink = io.BytesIO()
    server._stdio_write_message(sink, reply)
    return {"message": json.loads(sink.getvalue().decode("utf-8")), "wire_bytes": len(sink.getvalue())}


def call(registry, name, arguments=None):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return rpc(registry, "tools/call", params)


def list_summary(reply):
    entries = reply["message"]["result"]["tools"]
    return {
        "names": [entry["name"] for entry in entries],
        "wire_bytes": reply["wire_bytes"],
        "entries_sha256": hashlib.sha256(json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest(),
    }


def echo(text: str = "plain"):
    '''Echo the text inside a nested dict.'''
    return {"echo": text, "nested": {"items": [1, 2, 3], "ratio": 0.5, "flag": True, "none": None}}


def accented():
    '''Return non-ASCII and quoted text.'''
    return {"word": "caf" + chr(233), "quote": 'say "hi"'}


def as_list():
    '''Return a list.'''
    return [1, "two", {"three": 3}]


def as_int():
    '''Return an int.'''
    return 7


def as_none():
    '''Return None.'''
    return None


def as_text():
    '''Return a str.'''
    return "plain"


def as_path():
    '''Return a Path, which only default=str can encode.'''
    return {"path": Path("alpha") / "beta"}


def failing():
    '''Raise an error longer than the 2000 character cap.'''
    raise ValueError("x" * 2500)


def dashboard_hidden():
    '''A webview-only dashboard tool.'''
    return {"tool": "hidden"}


def dashboard_snapshot():
    '''A kept dashboard tool.'''
    return {"tool": "snapshot"}


def dashboard_transition():
    '''A kept dashboard tool.'''
    return {"tool": "transition"}


def dashboard_bare():
    '''No trailing underscore after dashboard, so not a dashboard-prefixed name.'''
    return {"tool": "bare"}


def manager_dashboard_view():
    '''Contains dashboard but does not start with the prefix.'''
    return {"tool": "view"}


synthetic = {
    "synthetic_echo": echo,
    "synthetic_accented": accented,
    "synthetic_list": as_list,
    "synthetic_int": as_int,
    "synthetic_none": as_none,
    "synthetic_text": as_text,
    "synthetic_path": as_path,
    "synthetic_failing": failing,
    "aiworkhub_dashboard_synthetic_hidden": dashboard_hidden,
    "aiworkhub_dashboard_snapshot": dashboard_snapshot,
    "aiworkhub_dashboard_needfix_transition": dashboard_transition,
    "aiworkhub_dashboard": dashboard_bare,
    "aiworkhub_manager_dashboard_view": manager_dashboard_view,
}


class Pipe:
    def __init__(self, buffer):
        self.buffer = buffer


def run_loop():
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "aiworkhub_task_template_list", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": hidden_example, "arguments": PROBE}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": KEEP[1], "arguments": PROBE}},
    ]
    data = b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in requests)
    sink = io.BytesIO()
    saved = (sys.stdin, sys.stdout)
    sys.stdin, sys.stdout = Pipe(io.BytesIO(data)), Pipe(sink)
    try:
        server._run_stdio_fallback_server(SERVER_NAME, real, "")
    finally:
        sys.stdin, sys.stdout = saved
    replies = {}
    for line in sink.getvalue().split(b"\n"):
        if line:
            message = json.loads(line.decode("utf-8"))
            replies[str(message["id"])] = {"message": message, "wire_bytes": len(line) + 1}
    replies["2"] = list_summary(replies["2"])
    return replies


def decorator_probe():
    sandbox = server.FastMCP("nf01676-sandbox")

    def aiworkhub_dashboard_decorated():
        '''Decorated hidden candidate.'''
        return 1

    def aiworkhub_dashboard_snapshot():
        '''Decorated kept candidate.'''
        return 2

    def plain_decorated():
        '''Decorated plain tool.'''
        return 3

    def renamed_plain():
        '''Decorated plain tool registered under a dashboard name.'''
        return 4

    report = {}
    for label, function, registration in (
        ("aiworkhub_dashboard_decorated", aiworkhub_dashboard_decorated, {}),
        ("aiworkhub_dashboard_snapshot", aiworkhub_dashboard_snapshot, {}),
        ("plain_decorated", plain_decorated, {}),
        ("renamed_plain", renamed_plain, {"name": "aiworkhub_dashboard_renamed"}),
    ):
        returned = sandbox.tool(**registration)(function)
        report[label] = {
            "same_object": returned is function,
            "result": returned(),
            "registered": registration.get("name", label) in sandbox._tools,
        }
    return report


def snapshot(full):
    listing = rpc(real, "tools/list")
    shape = {
        "surface": server.mcp_surface(),
        "env": os.environ.get("AIWORKHUB_MCP_SURFACE"),
        "list": list_summary(listing),
        "calls": {
            "aiworkhub_task_template_list": call(real, "aiworkhub_task_template_list", {}),
            "template_show_missing_argument": call(real, "aiworkhub_task_template_show", {}),
            "unknown": call(real, "aiworkhub_no_such_tool_nf01676", PROBE),
        },
        "probes": {name: call(real, name, PROBE)["message"] for name in probe_names},
        "synthetic": {
            "list": rpc(synthetic, "tools/list"),
            "calls": {name: call(synthetic, name, {}) for name in synthetic},
            "echo_with_text": call(synthetic, "synthetic_echo", {"text": "given"}),
            "no_arguments_key": call(synthetic, "synthetic_none"),
            "hidden_with_bad_arguments": call(synthetic, "aiworkhub_dashboard_synthetic_hidden", ["not", "an", "object"]),
            "visible_with_bad_arguments": call(synthetic, "synthetic_echo", ["not", "an", "object"]),
            "visible_with_unexpected_argument": call(synthetic, "synthetic_echo", {"nope": 1}),
        },
        "decorator": decorator_probe(),
        "loop": run_loop(),
    }
    if full:
        shape["list"]["entries"] = listing["message"]["result"]["tools"]
    return shape


snapshots = {ARGS["label"]: snapshot(True)}
if ARGS.get("flip"):
    os.environ["AIWORKHUB_MCP_SURFACE"] = "all"
    snapshots["explicit_all"] = snapshot(False)
    os.environ["AIWORKHUB_MCP_SURFACE"] = "manager"
    snapshots["late_manager"] = snapshot(False)
print(PREFIX + json.dumps({"registered": list(real), "hidden_example": hidden_example, "snapshots": snapshots}))
"""

_SDK_BODY = r"""
ARGS = json.loads(os.environ["NF01676_ARGS"])
assert server._MCP_SDK_AVAILABLE
listed = asyncio.run(server.mcp.list_tools())
manager = server.mcp._tool_manager
print(PREFIX + json.dumps({
    "surface": server.mcp_surface(),
    "names": [tool.name for tool in listed],
    "registered": {name: manager.get_tool(name) is not None for name in ARGS["probe_names"]},
}))
"""


def _run_child(backend: str, body: str, *, surface: str | None, repo_root: Path, args: dict):
    # Extend PYTHONPATH, never replace it: the AppContainer validation lane keeps its
    # path-resolution shim and site-packages there. The -S stdlib child drops every entry
    # holding the SDK, or -S could not hide mcp/pydantic.
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if backend == "stdlib":
        inherited = [p for p in inherited if not (Path(p, "mcp").is_dir() or Path(p, "pydantic").is_dir())]
    env = dict(
        os.environ,
        AIWORKHUB_ALLOW_WRITES="1",
        AIWORKHUB_MCP_STDIO_BACKEND=backend,
        AIWORKHUB_REPO_ROOT=str(repo_root),
        AIWORKHUB_REPO=str(repo_root),
        NF01676_ARGS=json.dumps(args),
        PYTHONPATH=os.pathsep.join([str(_SRC), *inherited]),
    )
    env.pop("AIWORKHUB_MCP_SURFACE", None)
    if surface is not None:
        env["AIWORKHUB_MCP_SURFACE"] = surface
    script = f"PREFIX = {_PAYLOAD_PREFIX!r}\n{_CHILD_PREAMBLE}{body}"
    argv = [sys.executable, *(["-S"] if backend == "stdlib" else []), "-c", script]
    completed = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr[-4000:]
    lines = [line for line in completed.stdout.splitlines() if line.startswith(_PAYLOAD_PREFIX)]
    assert len(lines) == 1, completed.stdout[-2000:]
    return json.loads(lines[0][len(_PAYLOAD_PREFIX):])


def _result(message: dict) -> dict:
    assert "error" not in message, message
    return message["result"]


def _error(message: dict) -> dict:
    assert "result" not in message, message
    return message["error"]


def _names(reply: dict) -> list:
    return [entry["name"] for entry in _result(reply["message"])["tools"]]


def _wire_len(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _hidden_names(registered: list) -> list:
    return [name for name in registered if name.startswith(_DASHBOARD) and name not in _KEEP]


def _expected_text(name: str) -> str:
    return json.dumps(_SYNTHETIC_RESULTS[name], ensure_ascii=False)


@pytest.fixture(scope="module")
def repo_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("nf01676_repo")
    assert task_store.initialize_repository(root)["ok"]
    return root


@pytest.fixture(scope="module")
def unset_run(repo_root: Path) -> dict:
    return _run_child(
        "stdlib", _STDLIB_BODY, surface=None, repo_root=repo_root, args={"label": "unset", "flip": True}
    )


@pytest.fixture(scope="module")
def manager_run(repo_root: Path, unset_run: dict) -> dict:
    probe_names = [name for name in unset_run["registered"] if name.startswith(_DASHBOARD)]
    return _run_child(
        "stdlib", _STDLIB_BODY, surface="manager", repo_root=repo_root,
        args={"label": "manager", "probe_names": probe_names, "hidden_example": unset_run["hidden_example"]},
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "all"),
        ("", "all"),
        ("all", "all"),
        ("ALL", "all"),
        ("manager", "manager"),
        ("MANAGER", "manager"),
        ("  Manager  ", "manager"),
        ("worker", "all"),
        ("managers", "all"),
        ("manager,all", "all"),
        ("1", "all"),
        ("none", "all"),
    ],
)
def test_surface_value_parsing(monkeypatch, value, expected) -> None:
    assert server.MCP_SURFACE_ENV == "AIWORKHUB_MCP_SURFACE"
    if value is None:
        monkeypatch.delenv(server.MCP_SURFACE_ENV, raising=False)
    else:
        monkeypatch.setenv(server.MCP_SURFACE_ENV, value)
    assert server.mcp_surface() == expected


def test_visibility_predicate_hides_only_webview_dashboard_tools(monkeypatch) -> None:
    monkeypatch.delenv(server.MCP_SURFACE_ENV, raising=False)
    assert all(server.mcp_tool_visible(name) for name in _MANAGER_VISIBILITY)
    monkeypatch.setenv(server.MCP_SURFACE_ENV, "manager")
    assert {name: server.mcp_tool_visible(name) for name in _MANAGER_VISIBILITY} == _MANAGER_VISIBILITY
    assert server.mcp_tool_visible("aiworkhub_dashboard_health", "all") is True
    monkeypatch.delenv(server.MCP_SURFACE_ENV)
    assert server.mcp_tool_visible("aiworkhub_dashboard_health", "manager") is False
    assert server.mcp_tool_visible("aiworkhub_dashboard_snapshot", "manager") is True
    assert server.mcp_tool_visible(None, "manager") is True
    assert server.mcp_tool_visible(5, "manager") is True


def test_documented_keep_set_is_the_two_manager_dashboard_tools(unset_run: dict, manager_run: dict) -> None:
    assert set(server.MANAGER_SURFACE_DASHBOARD_KEEP) == _KEEP
    assert set(server.MANAGER_SURFACE_DASHBOARD_KEEP_REASONS) == _KEEP
    assert all(reason.strip() for reason in server.MANAGER_SURFACE_DASHBOARD_KEEP_REASONS.values())
    assert _KEEP <= set(unset_run["registered"])
    assert _KEEP <= set(manager_run["registered"])


def test_unset_surface_is_the_all_surface(unset_run: dict) -> None:
    unset = unset_run["snapshots"]["unset"]
    explicit = unset_run["snapshots"]["explicit_all"]
    assert unset["surface"] == explicit["surface"] == "all"
    assert unset["env"] is None and explicit["env"] == "all"
    registered = unset_run["registered"]
    assert unset["list"]["names"] == explicit["list"]["names"] == registered
    assert len(registered) == len(set(registered)) >= 217
    assert len([name for name in registered if name.startswith(_DASHBOARD)]) >= 40
    assert unset["list"]["wire_bytes"] == explicit["list"]["wire_bytes"]
    assert unset["list"]["entries_sha256"] == explicit["list"]["entries_sha256"]
    for key in ("calls", "probes", "synthetic", "decorator", "loop"):
        assert unset[key] == explicit[key], key


def test_all_surface_results_carry_one_text_copy_and_no_structured_content(unset_run: dict) -> None:
    for label in ("unset", "explicit_all"):
        snap = unset_run["snapshots"][label]
        for name in _SYNTHETIC_RESULTS:
            assert _result(snap["synthetic"]["calls"][name]["message"]) == {
                "content": [{"type": "text", "text": _expected_text(name)}],
            }, (label, name)
        real = _result(snap["calls"][_TEMPLATE_LIST]["message"])
        assert set(real) == {"content"}
        [item] = real["content"]
        assert item["type"] == "text"
        assert json.loads(item["text"])["schema_id"] == "aiworkhub.task_template_list.v1"


def test_manager_list_drops_only_the_webview_dashboard_tools(unset_run: dict, manager_run: dict) -> None:
    full = unset_run["snapshots"]["unset"]["list"]
    trimmed = manager_run["snapshots"]["manager"]["list"]
    assert manager_run["snapshots"]["manager"]["surface"] == "manager"
    hidden = _hidden_names(full["names"])
    assert len(hidden) >= 40
    assert trimmed["names"] == [name for name in full["names"] if name not in set(hidden)]
    assert sorted(name for name in trimmed["names"] if name.startswith(_DASHBOARD)) == sorted(_KEEP)
    by_name = {entry["name"]: entry for entry in full["entries"]}
    assert len(by_name) == len(full["names"])
    assert [entry for entry in trimmed["entries"] if entry != by_name[entry["name"]]] == []


def test_manager_registers_only_visible_tools_and_matches_call_time_filtering(
    unset_run: dict, manager_run: dict
) -> None:
    imported = manager_run["snapshots"]["manager"]["list"]
    flipped = unset_run["snapshots"]["late_manager"]
    assert flipped["surface"] == "manager"
    assert manager_run["registered"] == imported["names"]
    assert set(manager_run["registered"]) == set(unset_run["registered"]) - set(_hidden_names(unset_run["registered"]))
    assert flipped["list"]["names"] == imported["names"]
    assert flipped["list"]["wire_bytes"] == imported["wire_bytes"]
    assert flipped["list"]["entries_sha256"] == imported["entries_sha256"]


def test_hidden_dashboard_tools_are_unknown_tools_on_manager_and_callable_on_all(
    unset_run: dict, manager_run: dict
) -> None:
    hidden = _hidden_names(unset_run["registered"])
    assert hidden
    for label, snap in (
        ("import_time", manager_run["snapshots"]["manager"]),
        ("late_flip", unset_run["snapshots"]["late_manager"]),
    ):
        visible = snap["list"]["names"]
        for name in hidden:
            error = _error(snap["probes"][name])
            assert error["code"] == -32602, (label, name)
            assert error["message"] == unknown_tool_message(name, visible), (label, name)
            payload = json.loads(error["message"])
            assert payload["error"] == "unknown_tool" and payload["requested"] == name
            assert not set(payload["suggestions"]) & set(hidden)
        for name in _KEEP:
            assert _error(snap["probes"][name])["message"] == _PROBE_ERROR, (label, name)
    for label in ("unset", "explicit_all"):
        snap = unset_run["snapshots"][label]
        for name in hidden:
            assert _error(snap["probes"][name]) == {"code": -32602, "message": _PROBE_ERROR}, (label, name)


def test_unknown_tool_error_is_unchanged_for_names_that_never_existed(unset_run: dict, manager_run: dict) -> None:
    for snap in (
        unset_run["snapshots"]["unset"],
        unset_run["snapshots"]["explicit_all"],
        unset_run["snapshots"]["late_manager"],
        manager_run["snapshots"]["manager"],
    ):
        error = _error(snap["calls"]["unknown"]["message"])
        assert error["code"] == -32602
        assert error["message"] == unknown_tool_message("aiworkhub_no_such_tool_nf01676", snap["list"]["names"])


def test_manager_results_carry_one_text_copy_and_no_structured_content(unset_run: dict, manager_run: dict) -> None:
    reference = _result(unset_run["snapshots"]["unset"]["calls"][_TEMPLATE_LIST]["message"])
    for label, snap in (
        ("import_time", manager_run["snapshots"]["manager"]),
        ("late_flip", unset_run["snapshots"]["late_manager"]),
    ):
        for name in _SYNTHETIC_RESULTS:
            if name == _SYNTHETIC_HIDDEN:
                continue
            assert _result(snap["synthetic"]["calls"][name]["message"]) == {
                "content": [{"type": "text", "text": _expected_text(name)}],
            }, (label, name)
        real = _result(snap["calls"][_TEMPLATE_LIST]["message"])
        assert list(real) == ["content"], label
        assert real["content"] == reference["content"], label
        assert json.loads(real["content"][0]["text"]) == json.loads(reference["content"][0]["text"]), label
        echoed = _result(snap["synthetic"]["echo_with_text"]["message"])
        assert list(echoed) == ["content"]
        assert json.loads(echoed["content"][0]["text"])["echo"] == "given"
        nothing = _result(snap["synthetic"]["no_arguments_key"]["message"])
        assert nothing == {"content": [{"type": "text", "text": "null"}]}


def test_tool_failures_are_identical_on_every_surface(unset_run: dict, manager_run: dict) -> None:
    snaps = [
        unset_run["snapshots"]["unset"],
        unset_run["snapshots"]["explicit_all"],
        unset_run["snapshots"]["late_manager"],
        manager_run["snapshots"]["manager"],
    ]
    for snap in snaps:
        failed = _result(snap["synthetic"]["calls"]["synthetic_failing"]["message"])
        assert failed == {"content": [{"type": "text", "text": "x" * 2000}], "isError": True}
        missing = _result(snap["calls"]["template_show_missing_argument"]["message"])
        assert list(missing) == ["content", "isError"] and missing["isError"] is True
        assert "template_id" in missing["content"][0]["text"]
    assert all(
        snap["calls"]["template_show_missing_argument"] == snaps[0]["calls"]["template_show_missing_argument"]
        for snap in snaps
    )


def test_synthetic_registry_filters_listing_and_calls_by_prefix_and_keep_set(
    unset_run: dict, manager_run: dict
) -> None:
    on_all = [unset_run["snapshots"]["unset"], unset_run["snapshots"]["explicit_all"]]
    on_manager = [unset_run["snapshots"]["late_manager"], manager_run["snapshots"]["manager"]]
    for snap in on_all:
        synthetic = snap["synthetic"]
        assert _names(synthetic["list"]) == _SYNTHETIC_ALL
        assert _error(synthetic["hidden_with_bad_arguments"]["message"])["message"] == "arguments_must_be_object"
        hidden_call = _result(synthetic["calls"][_SYNTHETIC_HIDDEN]["message"])
        assert list(hidden_call) == ["content"]
        assert json.loads(hidden_call["content"][0]["text"]) == {"tool": "hidden"}
    expected = unknown_tool_message(_SYNTHETIC_HIDDEN, _SYNTHETIC_MANAGER)
    for snap in on_manager:
        synthetic = snap["synthetic"]
        assert _names(synthetic["list"]) == _SYNTHETIC_MANAGER
        assert _error(synthetic["calls"][_SYNTHETIC_HIDDEN]["message"]) == {"code": -32602, "message": expected}
        assert _error(synthetic["hidden_with_bad_arguments"]["message"]) == {"code": -32602, "message": expected}
    for snap in on_all + on_manager:
        synthetic = snap["synthetic"]
        assert _error(synthetic["visible_with_bad_arguments"]["message"])["message"] == "arguments_must_be_object"
        assert _error(synthetic["visible_with_unexpected_argument"]["message"])["message"] == (
            "unexpected_arguments:['nope']"
        )


def test_decorator_skips_hidden_tools_yet_returns_the_function_unchanged(
    unset_run: dict, manager_run: dict
) -> None:
    hidden_on_manager = {"aiworkhub_dashboard_decorated", "renamed_plain"}
    for label in ("unset", "explicit_all"):
        for name, report in unset_run["snapshots"][label]["decorator"].items():
            assert report["same_object"] and report["registered"], (label, name)
    for decorated in (
        unset_run["snapshots"]["late_manager"]["decorator"],
        manager_run["snapshots"]["manager"]["decorator"],
    ):
        assert {name for name, report in decorated.items() if not report["registered"]} == hidden_on_manager
        assert all(report["same_object"] for report in decorated.values())
        assert {name: report["result"] for name, report in decorated.items()} == {
            "aiworkhub_dashboard_decorated": 1,
            "aiworkhub_dashboard_snapshot": 2,
            "plain_decorated": 3,
            "renamed_plain": 4,
        }


def test_real_stdio_loop_serves_the_same_surface(unset_run: dict, manager_run: dict) -> None:
    hidden_name = unset_run["hidden_example"]
    assert manager_run["hidden_example"] == hidden_name
    for snap, is_manager in (
        (unset_run["snapshots"]["unset"], False),
        (unset_run["snapshots"]["late_manager"], True),
        (manager_run["snapshots"]["manager"], True),
    ):
        loop = snap["loop"]
        assert _result(loop["1"]["message"])["serverInfo"]["name"] == "AIWorkHub MCP"
        assert loop["2"] == {key: snap["list"][key] for key in ("names", "wire_bytes", "entries_sha256")}
        template = _result(loop["3"]["message"])
        assert list(template) == ["content"]
        assert loop["3"]["wire_bytes"] == snap["calls"][_TEMPLATE_LIST]["wire_bytes"]
        assert _error(loop["5"]["message"])["message"] == _PROBE_ERROR
        expected_hidden = unknown_tool_message(hidden_name, snap["list"]["names"]) if is_manager else _PROBE_ERROR
        assert _error(loop["4"]["message"])["message"] == expected_hidden


def test_measured_manager_surface_is_smaller(unset_run: dict, manager_run: dict, capsys) -> None:
    full = unset_run["snapshots"]["unset"]
    trimmed = manager_run["snapshots"]["manager"]
    hidden = _hidden_names(full["list"]["names"])
    all_count, manager_count = len(full["list"]["names"]), len(trimmed["list"]["names"])
    assert all_count >= 217
    assert manager_count == all_count - len(hidden) < all_count
    all_bytes, manager_bytes = full["list"]["wire_bytes"], trimmed["list"]["wire_bytes"]
    assert manager_bytes < all_bytes
    assert (all_bytes - manager_bytes) / all_bytes >= 0.10
    hidden_entries = [entry for entry in full["list"]["entries"] if entry["name"] in set(hidden)]
    assert len(hidden_entries) == len(hidden)
    assert all_bytes - manager_bytes == sum(_wire_len(entry) for entry in hidden_entries) + 2 * len(hidden_entries)

    all_call, manager_call = full["calls"][_TEMPLATE_LIST], trimmed["calls"][_TEMPLATE_LIST]
    # NF-2026-00715: every surface now carries the single text copy, so a call costs the same.
    assert manager_call["wire_bytes"] == all_call["wire_bytes"]
    assert _result(all_call["message"]) == _result(manager_call["message"])
    with capsys.disabled():
        print(
            f"\nNF-2026-01676 measured: tools/list all={all_count} tools/{all_bytes} B, "
            f"manager={manager_count} tools/{manager_bytes} B; {_TEMPLATE_LIST} call "
            f"all={all_call['wire_bytes']} B, manager={manager_call['wire_bytes']} B"
        )


@pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp SDK is not importable, so the FastMCP registry cannot be compared",
)
def test_sdk_backend_registers_the_same_manager_surface(unset_run: dict, manager_run: dict, repo_root: Path) -> None:
    probe_names = [name for name in unset_run["registered"] if name.startswith(_DASHBOARD)]
    sdk = _run_child("sdk", _SDK_BODY, surface="manager", repo_root=repo_root, args={"probe_names": probe_names})
    assert sdk["surface"] == "manager"
    assert sorted(sdk["names"]) == sorted(manager_run["snapshots"]["manager"]["list"]["names"])
    assert sdk["registered"] == {name: name in _KEEP for name in probe_names}


def _launcher_source() -> str:
    text = _EXTENSION_JS.read_text(encoding="utf-8")
    start_marker = "const script = String.raw`"
    start = text.index(start_marker) + len(start_marker)
    return text[start:text.index("`;\n  fs.mkdirSync(binDir", start)]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(None, "manager stdlib"), ("all", "all stdlib"), ("manager", "manager stdlib")],
)
def test_stable_launcher_defaults_to_the_manager_surface(tmp_path, configured, expected) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "aiworkhub").mkdir(parents=True)
    (runtime / "aiworkhub" / "__init__.py").write_text("", encoding="utf-8")
    (runtime / "aiworkhub" / "server.py").write_text(
        "import os\n"
        "def main():\n"
        "    print(os.environ.get('AIWORKHUB_MCP_SURFACE'), os.environ.get('AIWORKHUB_MCP_STDIO_BACKEND'))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "aiworkhub-mcp-server.py"
    launcher.write_text(_launcher_source(), encoding="utf-8")
    env = dict(os.environ)
    env.pop("AIWORKHUB_MCP_SURFACE", None)
    if configured is not None:
        env["AIWORKHUB_MCP_SURFACE"] = configured
    completed = subprocess.run(
        [sys.executable, str(launcher), "--serve-runtime", str(runtime)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip() == expected
