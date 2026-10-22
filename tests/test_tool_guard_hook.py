"""TOOL-GUARD-HOOK-01: the Claude Code PreToolUse tool guard and its installation.

The guard routes raw discovery, reads and edits of indexed source to the
AIWorkHub tools. A PreToolUse hook that exits 2 BLOCKS the tool call, so every
path through it -- deny, allow, malformed input, a missing script -- exits 0,
and its only refusal is one printed ``permissionDecision: deny`` object.
"""

from __future__ import annotations

import ast
import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from aiworkhub import provider_tool_guards, tool_guard_hook  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SOURCE = _SRC / "aiworkhub" / "tool_guard_hook.py"
HOOK_REL = provider_tool_guards.CLAUDE_TOOL_GUARD_HOOK_REL
SETTINGS_REL = provider_tool_guards.CLAUDE_SETTINGS_REL
GUARD_COMMAND = provider_tool_guards.CLAUDE_TOOL_GUARD_COMMAND
REMINDER_COMMAND = provider_tool_guards.CLAUDE_TOOL_GUARD_REMINDER_COMMAND
GUARD_MATCHER = "Bash|PowerShell|Read|Edit|Write"
MANAGED_COMMANDS = {"PreToolUse": [GUARD_COMMAND], "SessionStart": [REMINDER_COMMAND]}
DENIALS_REL = Path(".aiworkhub", "runtime", "tool_guard", "denials.jsonl")

_CORE = "".join(f"def function_{index}():\n    return {index}\n" for index in range(30))
_NEAR_COPY = _CORE.replace("return 7\n", "return 8\n")
_WALK_HEREDOC = (
    "python3 - <<'EOF'\n"
    "import os\n"
    "for folder, _, names in os.walk('src'):\n"
    "    print(folder, len(names))\n"
    "EOF\n"
)
_SQLITE_HEREDOC = (
    "python - <<'EOF'\n"
    "import sqlite3\n"
    "connection = sqlite3.connect('.aiworkhub/tasking/task_queue.sqlite')\n"
    "print(connection.execute('select count(*) from tasks').fetchone())\n"
    "print(open('.aiworkhub/tasking/task_queue.sqlite', 'rb').read(16))\n"
    "EOF\n"
)

# (tool, shell command or tool_input with a repository-relative file_path, rule)
DENIED: list[tuple[str, Any, str]] = [
    ("Bash", "grep -rn foo src", "R1"),
    ("Bash", "cd src && grep foo x.py", "R1"),
    ("Bash", "rg foo", "R1"),
    ("Bash", "git grep foo", "R1"),
    ("Bash", "findstr /s foo *.py", "R1"),
    ("PowerShell", "Get-ChildItem -Recurse src | Select-String foo", "R1"),
    ("Bash", 'find . -name "*.py"', "R2"),
    ("Bash", "tree src", "R2"),
    ("Bash", "ls -R src", "R2"),
    ("Bash", "cat src/aiworkhub/core.py", "R3"),
    ("Bash", "sed -n '1,50p' src/aiworkhub/core.py", "R3"),
    ("Bash", "head -100 tests/test_x.py", "R3"),
    ("PowerShell", "Get-Content src/aiworkhub/core.py", "R3"),
    ("Bash", "python -c \"open('src/aiworkhub/core.py').read()\"", "R4"),
    ("Bash", _WALK_HEREDOC, "R4"),
    ("Bash", "python -c \"import glob; print(glob.glob('src/**/*.py'))\"", "R4"),
    ("Read", {"file_path": "src/aiworkhub/core.py"}, "R5"),
    ("Read", {"file_path": "src/aiworkhub/core.py", "limit": 2000}, "R5"),
    ("Edit", {"file_path": "src/x.py", "old_string": "foo = 1", "new_string": "foo = 2"}, "R6"),
    ("Write", {"file_path": "src/aiworkhub/core.py", "content": _NEAR_COPY}, "R7"),
]

ALLOWED: list[tuple[str, Any]] = [
    ("Bash", "git log --oneline | grep fix"),
    ("Bash", "git status --short"),
    ("Bash", "grep foo .aiworkhub/runtime/process_logs/x.log"),
    ("Bash", "cat .aiworkhub/runtime/x.json"),
    ("Bash", _SQLITE_HEREDOC),
    ("Bash", "python -c \"import os; print('*' * 40, os.listdir('.aiworkhub'))\""),
    ("Bash", "python -m pytest tests/x.py -q"),
    ("Bash", "ls src"),
    ("PowerShell", "Get-ChildItem src"),
    ("Read", {"file_path": "src/aiworkhub/core.py", "offset": 20, "limit": 40}),
    ("Read", {"file_path": "src/aiworkhub/core.py", "offset": 1, "limit": 400}),
    ("Read", {"file_path": "notes.txt"}),
    ("Edit", {"file_path": "../outside.py", "old_string": "a = 1", "new_string": "a = 2"}),
    ("Edit", {"file_path": ".aiworkhub/runtime/x.json", "old_string": "{}", "new_string": "[]"}),
    ("Write", {"file_path": "src/new_module.py", "content": "x = 1\n"}),
    ("Write", {"file_path": "src/aiworkhub/core.py", "content": "rewritten = True\n"}),
]

_SOURCE_GRAPH = ("aiworkhub_manager_source_graph_query", "aiworkhub_worker_source_graph_query")
_READ_INSTEAD = (*_SOURCE_GRAPH, 'mode="body"', "offset+limit<=400")
_EDIT_INSTEAD = (
    "aiworkhub_worker_semantic_edit_prepare",
    "aiworkhub_worker_semantic_edit_apply",
    "scripts/manager_semantic_edit.py",
)
REPLACEMENTS = {
    "R1": (*_SOURCE_GRAPH, 'mode="bodygrep"', 'query="foo"'),
    "R2": (*_SOURCE_GRAPH, 'mode="slice"'),
    "R3": _READ_INSTEAD,
    "R4": _READ_INSTEAD,
    "R5": _READ_INSTEAD,
    "R6": _EDIT_INSTEAD,
    "R7": _EDIT_INSTEAD,
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """AIWorkHub state, indexed source, an unindexed file and a file beside the root."""
    root = tmp_path / "repo"
    files = {
        "src/aiworkhub/core.py": _CORE,
        "src/x.py": "foo = 1\n",
        "tests/test_x.py": "def test_x():\n    assert True\n",
        "notes.txt": "foo\n",
        ".aiworkhub/runtime/x.json": "{}\n",
        ".aiworkhub/tasking/task_queue.sqlite": "",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    (tmp_path / "outside.py").write_bytes(b"a = 1\n")
    return root.resolve()


def _tool_input(root: Path, spec: Any) -> dict[str, Any]:
    if isinstance(spec, str):
        return {"command": spec}
    return {**spec, "file_path": str(root / spec["file_path"])}


def _payload(root: Path, tool: str, spec: Any) -> bytes:
    return json.dumps({
        "session_id": "session-1",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": _tool_input(root, spec),
    }).encode("utf-8")


def _single_decision(out: str) -> dict[str, Any]:
    """The one deny object a refusal prints, and nothing else."""
    lines = out.splitlines()
    assert len(lines) == 1, out
    decision = json.loads(lines[0])
    assert list(decision) == ["hookSpecificOutput"]
    output = decision["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert len(output["permissionDecisionReason"]) <= tool_guard_hook.REASON_MAX_CHARS
    return output


def _commands(settings: dict[str, Any]) -> dict[str, list[str]]:
    return {
        event: [hook["command"] for row in rows for hook in row["hooks"]]
        for event, rows in settings["hooks"].items()
    }


@pytest.mark.parametrize(("tool", "spec", "rule"), DENIED)
def test_raw_access_to_indexed_source_is_denied_with_its_replacement(
    repo: Path, tool: str, spec: Any, rule: str
) -> None:
    verdict = tool_guard_hook.decide(tool, _tool_input(repo, spec), repo)

    assert verdict is not None, (tool, spec)
    rule_id, reason = verdict
    assert rule_id == rule, reason
    assert reason.startswith(f"AIWorkHub tool guard {rule}:")
    assert len(reason) <= tool_guard_hook.REASON_MAX_CHARS
    assert reason.endswith("reports the target unindexed.")
    for replacement in REPLACEMENTS[rule]:
        assert replacement in reason, (replacement, reason)


@pytest.mark.parametrize(("tool", "spec"), ALLOWED)
def test_bounded_unindexed_and_new_file_access_is_allowed(
    repo: Path, tool: str, spec: Any
) -> None:
    assert tool_guard_hook.decide(tool, _tool_input(repo, spec), repo) is None


def test_a_root_without_aiworkhub_state_allows_everything(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    (root / "src" / "aiworkhub").mkdir(parents=True)
    (root / "src" / "aiworkhub" / "core.py").write_bytes(_CORE.encode("utf-8"))
    (root / "src" / "x.py").write_bytes(b"foo = 1\n")

    for tool, spec, _rule in DENIED:
        assert tool_guard_hook.decide(tool, _tool_input(root, spec), root) is None, (tool, spec)


def test_relative_operands_resolve_against_the_call_cwd(repo: Path) -> None:
    package = repo / "src" / "aiworkhub"
    verdict = tool_guard_hook.decide("Bash", {"command": "cat core.py"}, repo, package)

    assert verdict is not None and verdict[0] == "R3"
    assert "src/aiworkhub/core.py" in verdict[1]
    runtime = repo / ".aiworkhub" / "runtime"
    assert tool_guard_hook.decide("Bash", {"command": "cat x.json"}, repo, runtime) is None


@pytest.mark.parametrize(
    "pattern", ["x" * 1000, "ფუნქცია_" * 80, "a\nb\tc\x1b[31m\x7f"]
)
def test_a_reason_stays_bounded_and_printable_whatever_the_pattern(
    repo: Path, pattern: str
) -> None:
    command = "grep -rn " + shlex.quote(pattern) + " src"
    verdict = tool_guard_hook.decide("Bash", {"command": command}, repo)

    assert verdict is not None and verdict[0] == "R1"
    reason = verdict[1]
    assert len(reason) <= tool_guard_hook.REASON_MAX_CHARS
    assert reason.endswith("reports the target unindexed.")
    assert not any(ord(char) < 32 or ord(char) == 127 for char in reason)


def test_decide_is_pure(repo: Path) -> None:
    assert tool_guard_hook.decide("Bash", {"command": "grep -rn foo src"}, repo) is not None
    assert not (repo / DENIALS_REL).exists()


def _feed_stdin(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"))


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{not json",
        b"[]",
        b'"text"',
        b"\xff\xfe\x00",
        b'{"tool_name": "Bash", "tool_input": "grep -rn foo src"}',
        b'{"tool_name": 7, "tool_input": {"command": "grep -rn foo src"}}',
    ],
)
def test_malformed_input_prints_nothing_and_exits_zero(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: bytes
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    _feed_stdin(monkeypatch, raw)

    assert tool_guard_hook.main([]) == 0
    assert capsys.readouterr().out == ""
    assert not (repo / DENIALS_REL).exists()


def test_a_deny_prints_one_decision_and_logs_no_command_text(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    needle = "needle_7f3a91"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    _feed_stdin(monkeypatch, _payload(repo, "Bash", f"grep -rn {needle} src"))

    assert tool_guard_hook.main([]) == 0

    output = _single_decision(capsys.readouterr().out)
    assert output["permissionDecisionReason"].startswith("AIWorkHub tool guard R1:")
    log = (repo / DENIALS_REL).read_text(encoding="utf-8")
    rows = [json.loads(line) for line in log.splitlines()]
    assert len(rows) == 1
    assert set(rows[0]) == {"ts", "rule", "tool", "target", "session_id"}
    assert [rows[0][key] for key in ("rule", "tool", "target", "session_id")] == [
        "R1", "Bash", "src", "session-1",
    ]
    assert needle not in log


def test_the_reminder_names_the_replacements_within_its_budget(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert tool_guard_hook.main(["--reminder"]) == 0

    out = capsys.readouterr().out
    assert out == tool_guard_hook.REMINDER + "\n"
    assert len(out.encode("utf-8")) <= tool_guard_hook.REMINDER_MAX_BYTES
    for name in (
        "aiworkhub_worker_source_graph_query",
        "aiworkhub_worker_semantic_edit_prepare",
        "scripts/manager_semantic_edit.py",
        "unindexed",
    ):
        assert name in out


def _install_script(root: Path) -> Path:
    script = root / HOOK_REL
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes(HOOK_SOURCE.read_bytes())
    return script


def _run(argv: list[str], project: Path, stdin: bytes) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    return subprocess.run(
        argv, input=stdin, capture_output=True, cwd=project, env=env, timeout=120, check=False
    )


def _settings_argv(command: str) -> list[str]:
    """The managed settings command, run with this interpreter as its ``python``."""
    argv = shlex.split(command)
    assert argv[:2] == ["python", "-c"], argv
    return [sys.executable, *argv[1:]]


def test_the_script_never_exits_2_in_its_own_process(repo: Path) -> None:
    script = [sys.executable, str(_install_script(repo))]

    denied = _run(script, repo, _payload(repo, "Bash", "cat src/aiworkhub/core.py"))
    assert denied.returncode == 0, denied.stderr
    output = _single_decision(denied.stdout.decode("utf-8"))
    assert output["permissionDecisionReason"].startswith("AIWorkHub tool guard R3:")

    for raw in (b"", b"{not json", b"\xff\xfe\x00"):
        quiet = _run(script, repo, raw)
        assert (quiet.returncode, quiet.stdout) == (0, b""), quiet.stderr


def test_the_settings_commands_are_inert_without_the_script(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".aiworkhub").mkdir(parents=True)
    (project / "src").mkdir()
    (project / "src" / "x.py").write_bytes(b"foo = 1\n")
    payload = _payload(project, "Bash", "grep -rn foo src")

    for command in (GUARD_COMMAND, REMINDER_COMMAND):
        result = _run(_settings_argv(command), project, payload)
        assert (result.returncode, result.stdout) == (0, b""), result.stderr


def test_the_settings_commands_run_the_installed_script(repo: Path) -> None:
    _install_script(repo)
    guard = _settings_argv(GUARD_COMMAND)

    denied = _run(guard, repo, _payload(repo, "Bash", "rg foo"))
    assert denied.returncode == 0, denied.stderr
    reason = _single_decision(denied.stdout.decode("utf-8"))["permissionDecisionReason"]
    assert reason.startswith("AIWorkHub tool guard R1:")
    allowed = _run(guard, repo, _payload(repo, "Bash", "git status --short"))
    assert (allowed.returncode, allowed.stdout) == (0, b""), allowed.stderr

    reminder = _run(_settings_argv(REMINDER_COMMAND), repo, b"")
    assert reminder.returncode == 0, reminder.stderr
    text = reminder.stdout.decode("utf-8").replace("\r\n", "\n")
    assert text == tool_guard_hook.REMINDER + "\n"


def test_the_hook_module_imports_only_the_standard_library() -> None:
    tree = ast.parse(HOOK_SOURCE.read_text(encoding="utf-8"))
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, ast.dump(node)
            imported.append(node.module or "")

    assert imported
    assert [name for name in imported if name.split(".")[0] not in stdlib] == []


def test_the_tracked_hook_is_the_module_byte_for_byte() -> None:
    assert (REPO_ROOT / HOOK_REL).read_bytes() == HOOK_SOURCE.read_bytes()


def test_the_tracked_settings_are_a_fixed_point_of_the_merge() -> None:
    path = REPO_ROOT / SETTINGS_REL
    settings, changed = provider_tool_guards._merge_claude_settings(path)

    assert changed is False
    rendered = json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert rendered == path.read_text(encoding="utf-8")
    assert _commands(settings) == MANAGED_COMMANDS
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == GUARD_MATCHER
    assert settings["hooks"]["SessionStart"][0]["matcher"] == "compact"
    assert settings["env"] and settings["permissions"]["deny"]


def test_the_merge_replaces_managed_hooks_in_place_and_keeps_owner_hooks(
    tmp_path: Path,
) -> None:
    def hook(command: str) -> dict[str, str]:
        return {"type": "command", "command": command}

    legacy = hook("python3 AITools/cgraph.py find x")
    stale = hook("python .claude/hooks/aiworkhub_tool_guard.py")
    mixed = [hook("echo mixed"), hook("py old/aiworkhub_tool_guard.py --old")]
    path = tmp_path / SETTINGS_REL
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [hook("echo owner")]},
                    {"matcher": "Bash", "hooks": [legacy]},
                    {"matcher": "Read", "hooks": [stale]},
                    {"matcher": "Write", "hooks": mixed},
                ],
                "SessionStart": [{"matcher": "startup", "hooks": [hook("echo start")]}],
                "PostToolUse": [{"matcher": "Edit", "hooks": [hook("echo post")]}],
            },
        }),
        encoding="utf-8",
    )

    first = provider_tool_guards.apply_workspace_guards(tmp_path, read_only=False)
    text = path.read_text(encoding="utf-8")
    second = provider_tool_guards.apply_workspace_guards(tmp_path, read_only=False)

    assert SETTINGS_REL.as_posix() in first["changed"]
    assert second["changed"] == []
    assert path.read_text(encoding="utf-8") == text
    settings = json.loads(text)
    assert _commands(settings) == {
        "PreToolUse": ["echo owner", GUARD_COMMAND, "echo mixed"],
        "SessionStart": ["echo start", REMINDER_COMMAND],
        "PostToolUse": ["echo post"],
    }
    pre_tool_use = settings["hooks"]["PreToolUse"]
    assert [row["matcher"] for row in pre_tool_use] == ["Bash", GUARD_MATCHER, "Write"]
    assert pre_tool_use[1]["hooks"] == [
        {"type": "command", "command": GUARD_COMMAND, "timeout": 10}
    ]


@pytest.mark.parametrize(
    ("hooks", "error"),
    [
        ([], "claude_hooks_object_required"),
        ({"SessionStart": {}}, "claude_hooks_list_required:SessionStart"),
    ],
)
def test_a_malformed_hooks_value_fails_closed(tmp_path: Path, hooks: Any, error: str) -> None:
    path = tmp_path / SETTINGS_REL
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    with pytest.raises(provider_tool_guards.ProviderGuardError, match=error):
        provider_tool_guards.apply_workspace_guards(tmp_path, read_only=False)


def test_workspace_guards_install_the_hook_and_name_it_a_baseline_path(tmp_path: Path) -> None:
    paths = [SETTINGS_REL.as_posix(), HOOK_REL.as_posix()]

    first = provider_tool_guards.apply_workspace_guards(tmp_path, read_only=True)
    second = provider_tool_guards.apply_workspace_guards(tmp_path, read_only=True)

    assert first["changed"] == paths
    assert second["changed"] == []
    assert first["baseline_paths"] == second["baseline_paths"] == paths
    script = tmp_path / HOOK_REL
    assert script.read_bytes() == HOOK_SOURCE.read_bytes()
    settings = json.loads((tmp_path / SETTINGS_REL).read_text(encoding="utf-8"))
    assert _commands(settings) == MANAGED_COMMANDS

    # A checkout carrying a stale copy gets this module's bytes back.
    script.write_bytes(b"# stale copy\n")
    third = provider_tool_guards.apply_workspace_guards(tmp_path, read_only=True)
    assert third["changed"] == [HOOK_REL.as_posix()]
    assert script.read_bytes() == HOOK_SOURCE.read_bytes()


def test_repository_guards_install_the_hook_once(tmp_path: Path) -> None:
    first = provider_tool_guards.apply_repository_guards(tmp_path)
    second = provider_tool_guards.apply_repository_guards(tmp_path)

    assert HOOK_REL.as_posix() in first["changed"]
    assert second["changed"] == []
    assert (tmp_path / HOOK_REL).read_bytes() == HOOK_SOURCE.read_bytes()


_MIRROR_A = "a" * 32
_MIRROR_B = "b" * 32
_CANONICAL = "src/aiworkhub/core.py"


def _mirror_rel(request_id: str, rest: str = _CANONICAL) -> str:
    return f".aiworkhub/runtime/worktrees/{request_id}/worktree/{rest}"


def _worktree_of(request_id: str) -> str:
    return f".aiworkhub/runtime/worktrees/{request_id}/worktree"


def _plant_mirror(repo: Path, request_id: str, rest: str = _CANONICAL) -> None:
    path = repo / _mirror_rel(request_id, rest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((repo / rest).read_bytes())


def test_a_worktree_mirror_of_indexed_source_matches_the_canonical_decision(
    repo: Path,
) -> None:
    mirror = _mirror_rel(_MIRROR_A)
    _plant_mirror(repo, _MIRROR_A)
    cases: list[tuple[str, Any, str, Any]] = [
        ("Bash", f"cat {mirror}", "Bash", f"cat {_CANONICAL}"),
        ("Bash", f"grep foo {mirror}", "Bash", f"grep foo {_CANONICAL}"),
        ("Bash", f"sed -n '1,50p' {mirror}", "Bash", f"sed -n '1,50p' {_CANONICAL}"),
        (
            "Bash",
            f"python -c \"open('{mirror}').read()\"",
            "Bash",
            f"python -c \"open('{_CANONICAL}').read()\"",
        ),
        ("Read", {"file_path": mirror}, "Read", {"file_path": _CANONICAL}),
        (
            "Read",
            {"file_path": mirror, "limit": 2000},
            "Read",
            {"file_path": _CANONICAL, "limit": 2000},
        ),
        (
            "Read",
            {"file_path": mirror, "offset": 1, "limit": 400},
            "Read",
            {"file_path": _CANONICAL, "offset": 1, "limit": 400},
        ),
        (
            "Read",
            {"file_path": _mirror_rel(_MIRROR_A, "notes.txt")},
            "Read",
            {"file_path": "notes.txt"},
        ),
    ]
    for tool, spec, canon_tool, canon_spec in cases:
        mirrored = tool_guard_hook.decide(tool, _tool_input(repo, spec), repo)
        canonical = tool_guard_hook.decide(canon_tool, _tool_input(repo, canon_spec), repo)
        mirrored_rule = None if mirrored is None else mirrored[0]
        canonical_rule = None if canonical is None else canonical[0]
        assert mirrored_rule == canonical_rule, (spec, mirrored, canonical)


def test_a_non_mirror_runtime_path_and_a_non_hex_id_stay_unindexed(repo: Path) -> None:
    commands = [
        "cat .aiworkhub/runtime/process_logs/x.json",
        f"grep foo .aiworkhub/runtime/worktrees/{'g' * 32}/worktree/{_CANONICAL}",
        f"cat .aiworkhub/runtime/worktrees/{'a' * 31}/worktree/{_CANONICAL}",
        f"cat .aiworkhub/runtime/worktrees/{'A' * 32}/worktree/{_CANONICAL}",
        f"sed -n '1,20p' .aiworkhub/runtime/worktrees/{_MIRROR_A}/checkout/{_CANONICAL}",
    ]
    for command in commands:
        assert tool_guard_hook.decide("Bash", {"command": command}, repo) is None, command


def test_git_diff_of_a_worktree_stays_allowed(repo: Path) -> None:
    command = f"git --no-optional-locks -C {_worktree_of(_MIRROR_A)} diff"
    assert tool_guard_hook.decide("Bash", {"command": command}, repo) is None


def test_a_mirror_deny_names_the_worktree_of_the_path_actually_denied(repo: Path) -> None:
    left = _mirror_rel(_MIRROR_A)
    right = _mirror_rel(_MIRROR_B)
    left_worktree = _worktree_of(_MIRROR_A)
    right_worktree = _worktree_of(_MIRROR_B)
    _plant_mirror(repo, _MIRROR_A)
    _plant_mirror(repo, _MIRROR_B)
    # A prior deny of the other mirror must not leak into this reason.
    other = tool_guard_hook.decide("Bash", {"command": f"cat {right}"}, repo)
    assert other is not None and right_worktree in other[1] and left_worktree not in other[1]

    for command in (f"cat {left} {right}", f"bash -c \"cat {left} {right}\""):
        verdict = tool_guard_hook.decide("Bash", {"command": command}, repo)
        assert verdict is not None, command
        rule, reason = verdict
        assert rule == "R3", command
        assert f"git --no-optional-locks -C {left_worktree} diff" in reason
        assert f"Source Graph body/slice on {_CANONICAL}" in reason
        assert "offset+limit<=400" in reason
        assert right_worktree not in reason, command
        assert len(reason) <= tool_guard_hook.REASON_MAX_CHARS
        assert reason.startswith("AIWorkHub tool guard R3:")

    read = tool_guard_hook.decide("Read", _tool_input(repo, {"file_path": left}), repo)
    assert read is not None and read[0] == "R5"
    assert f"git --no-optional-locks -C {left_worktree} diff" in read[1]
    assert right_worktree not in read[1]
