"""One anchor completes a single-line range; client repos get the MCP edit pair.

NF-2026-01743 (GeoAI NF-00203), two defects:

1. ``semantic_edit._validated_anchors`` refused ``expected_first_line`` alone
   (or ``expected_last_line`` alone) even when ``start_line == end_line``,
   where the first and last line are the same line.  A multi-line range with
   one anchor is still refused, now naming the missing field after the
   unchanged ``semantic_edit_anchor_incomplete:<path>`` prefix.
2. The managed CLAUDE.md preamble told every client repository's manager to use
   ``scripts/manager_semantic_edit.py``, which exists only in AIWorkHub.  A
   repository without it is pointed at the manager MCP edit pair; with it the
   rendered text is byte-identical to before.

Run: python -m pytest -q tests/test_semantic_edit_single_line_anchor_nf01743.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from aiworkhub import agent_tool_instruction_mcp as instruction_mcp
from aiworkhub import agent_tool_instructions as instr
from aiworkhub import evidence_instruments, provider_tool_guards, semantic_edit
from aiworkhub import worker_ai_tools_mcp as worker_tools

_PATH = "src/module.py"
_LINES = [f"line_{number:02d} = {number}\n" for number in range(1, 21)]
_ROOT = Path(__file__).resolve().parents[1]
# The exact preamble line every checked-in CLAUDE.md carries today.
_SCRIPT_LINE = (
    "- Make the seat's small corrections with scripts/manager_semantic_edit.py"
    " --path --start --end (replacement on stdin): it replaces one hash-verified"
    " line range and never rewrites a file. The manager does not correct by"
    " whole-string rewrite."
)


def _write(root: Path) -> Path:
    target = root / _PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes("".join(_LINES).encode("utf-8"))
    return target


def _prepare(root: Path, start: int, end: int, **anchors: Any) -> semantic_edit.PreparedLineTarget:
    return semantic_edit.prepare_line_target(
        root, path=_PATH, start_line=start, end_line=end,
        allowed_writes=("src/*.py",), **anchors,
    )


class _RecordingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, name: str | None = None, **_options: Any) -> Callable[..., Any]:
        def register(function: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name or function.__name__] = function
            return function

        return register


def _tools(root: Path) -> dict[str, Callable[..., Any]]:
    mcp = _RecordingMCP()
    worker_tools.register_tools(mcp, worker_tools.WorkerToolContext(
        task_id="TASK", runner="runner", topic="topic", request_id="request",
        repo=root, authority_repo=root, source_graph_targets=(_PATH,),
        session_topic="topic", audit_ledger_path=None, audit_hmac_key_path=None,
        allowed_writes=("src/*.py",),
    ))
    return mcp.tools


# ---------------------------------------------------------------------------
# 1. One anchor on a single-line range.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["expected_first_line", "expected_last_line"])
def test_one_anchor_on_a_single_line_range_prepares_and_applies(tmp_path: Path, field: str) -> None:
    target = _write(tmp_path)
    both = _prepare(tmp_path, 10, 10, expected_first_line="line_10 = 10", expected_last_line="line_10 = 10")
    one = _prepare(tmp_path, 10, 10, **{field: "line_10 = 10"})
    assert one == both
    assert (one.start_line, one.end_line, one.fragment) == (10, 10, "line_10 = 10\n")

    tools = _tools(tmp_path)
    prepared = tools["aiworkhub_worker_semantic_edit_prepare"](
        file_path=_PATH, start_line=10, end_line=10, **{field: "line_10 = 10"},
    )
    assert prepared["ok"] is True, prepared
    applied = tools["aiworkhub_worker_semantic_edit_apply"](
        target_id=prepared["target_id"], new="line_10 = 100\n", idempotency_key=f"nf01743-{field}",
    )
    assert applied["ok"] is True, applied
    expected = [*_LINES[:9], "line_10 = 100\n", *_LINES[10:]]
    assert target.read_bytes() == "".join(expected).encode("utf-8")


@pytest.mark.parametrize("field", ["expected_first_line", "expected_last_line"])
def test_one_anchor_still_relocates_a_shifted_single_line(tmp_path: Path, field: str) -> None:
    _write(tmp_path)
    moved = _prepare(tmp_path, 8, 8, **{field: "line_10 = 10"})
    assert (moved.start_line, moved.end_line, moved.anchor_relocated_from) == (10, 10, (8, 8))


@pytest.mark.parametrize("field", ["expected_first_line", "expected_last_line"])
def test_one_wrong_anchor_on_a_single_line_is_a_mismatch_not_a_bind(tmp_path: Path, field: str) -> None:
    _write(tmp_path)
    with pytest.raises(semantic_edit.SemanticEditAnchorMismatch):
        _prepare(tmp_path, 10, 10, **{field: "absent"})


def test_one_non_text_anchor_on_a_single_line_is_invalid(tmp_path: Path) -> None:
    _write(tmp_path)
    with pytest.raises(semantic_edit.SemanticEditError) as excinfo:
        _prepare(tmp_path, 10, 10, expected_first_line=10)
    assert str(excinfo.value) == f"semantic_edit_anchor_invalid:{_PATH}"


@pytest.mark.parametrize(
    ("anchors", "missing"),
    [
        ({"expected_first_line": "line_10 = 10"}, "expected_last_line"),
        ({"expected_last_line": "line_11 = 11"}, "expected_first_line"),
    ],
)
def test_one_anchor_on_a_multi_line_range_is_refused_naming_the_missing_field(
    tmp_path: Path, anchors: dict[str, Any], missing: str,
) -> None:
    target = _write(tmp_path)
    with pytest.raises(semantic_edit.SemanticEditError) as excinfo:
        _prepare(tmp_path, 10, 11, **anchors)
    reason = str(excinfo.value)
    assert reason.startswith(f"semantic_edit_anchor_incomplete:{_PATH}")
    assert reason == f"semantic_edit_anchor_incomplete:{_PATH}:missing={missing}"

    refused = _tools(tmp_path)["aiworkhub_worker_semantic_edit_prepare"](
        file_path=_PATH, start_line=10, end_line=11, **anchors,
    )
    assert refused["ok"] is False
    assert refused["reason"] == reason
    assert target.read_bytes() == "".join(_LINES).encode("utf-8")


# ---------------------------------------------------------------------------
# 2. The managed manager preamble names what the target repository has.
# ---------------------------------------------------------------------------

def test_a_repo_without_the_script_is_told_the_mcp_edit_pair() -> None:
    rendered = instr.render_projection("CLAUDE.md", manager_edit_script=False)
    assert "aiworkhub_manager_semantic_edit_prepare" in rendered
    assert "aiworkhub_manager_semantic_edit_apply" in rendered
    assert instr.MANAGER_EDIT_SCRIPT not in rendered
    assert "manager_semantic_edit.py" not in rendered
    assert len(rendered.encode("utf-8")) <= instr.PROJECTION_MAX_BYTES

    plan = instr.build_apply_plan("CLAUDE.md", "# Owner\n", manager_edit_script=False)
    assert plan["ok"] is True
    assert "manager_semantic_edit.py" not in plan["planned_text"]
    assert "aiworkhub_manager_semantic_edit_prepare" in plan["planned_text"]
    diff = instr.diff_projection("CLAUDE.md", "# Owner\n", manager_edit_script=False)
    assert "manager_semantic_edit.py" not in diff


def test_a_repo_with_the_script_renders_byte_identically_to_today() -> None:
    assert (_ROOT / instr.MANAGER_EDIT_SCRIPT).is_file()
    preamble_lines = instr.CLAUDE_MANAGER_PREAMBLE.splitlines()
    assert preamble_lines[7] == _SCRIPT_LINE
    assert "aiworkhub_manager_semantic_edit_prepare" not in instr.CLAUDE_MANAGER_PREAMBLE
    for provider in instr.PROVIDERS:
        default = instr.render_projection(provider)
        assert instr.render_projection(provider, manager_edit_script=True) == default
        assert (
            instr.build_apply_plan(provider, "# Owner\n", manager_edit_script=True)
            == instr.build_apply_plan(provider, "# Owner\n")
        )
    assert instr.render_projection("CLAUDE.md").count(_SCRIPT_LINE) == 1
    assert instr.claude_manager_preamble() == instr.CLAUDE_MANAGER_PREAMBLE


def test_only_the_edit_line_differs_between_the_two_variants() -> None:
    with_script = instr.render_projection("CLAUDE.md").splitlines()
    without = instr.render_projection("CLAUDE.md", manager_edit_script=False).splitlines()
    changed = [(a, b) for a, b in zip(with_script, without, strict=True) if a != b]
    assert len(changed) == 1
    assert changed[0][0] == _SCRIPT_LINE
    for provider in ("AGENTS.md", ".github/copilot-instructions.md"):
        assert instr.render_projection(provider, manager_edit_script=False) == instr.render_projection(provider)


def test_either_variant_parked_outside_the_block_is_a_managed_rule_line() -> None:
    rules = instr.managed_policy_rule_lines()
    mcp_line = next(
        line for line in instr.claude_manager_preamble(manager_edit_script=False).splitlines()
        if "aiworkhub_manager_semantic_edit_prepare" in line
    )
    assert _SCRIPT_LINE in rules
    assert mcp_line in rules


# ---------------------------------------------------------------------------
# 3. Production callers pass whether the target repository has the script.
# ---------------------------------------------------------------------------

def _client_repo(root: Path, *, script: bool) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if script:
        (root / instr.MANAGER_EDIT_SCRIPT).parent.mkdir(parents=True, exist_ok=True)
        (root / instr.MANAGER_EDIT_SCRIPT).write_bytes(b"")
    return root


def _apply_all(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(instruction_mcp.core, "writes_allowed", lambda: True)
    written: dict[str, str] = {}
    for provider in instr.PROVIDERS:
        result = instruction_mcp.apply(repo_root=str(root), provider=provider, write=True)
        assert result["ok"] is True and result["write_performed"] is True, result
        written[provider] = (root / provider).read_text(encoding="utf-8")
    return written


def test_manager_edit_script_present_reads_the_target_repository(tmp_path: Path) -> None:
    assert instr.manager_edit_script_present(_client_repo(tmp_path / "bare", script=False)) is False
    assert instr.manager_edit_script_present(_client_repo(tmp_path / "self", script=True)) is True
    assert instr.manager_edit_script_present(_ROOT) is True


def test_instruction_mcp_apply_names_the_mcp_pair_in_a_repo_without_the_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _client_repo(tmp_path, script=False)
    dry = instruction_mcp.apply(repo_root=str(root), provider="CLAUDE.md")
    assert "aiworkhub_manager_semantic_edit_prepare" in dry["diff"]
    assert instr.MANAGER_EDIT_SCRIPT not in dry["diff"]
    planned = instruction_mcp._write_plan(root, "CLAUDE.md")["planned_text"]
    assert "aiworkhub_manager_semantic_edit_prepare" in planned
    assert instr.MANAGER_EDIT_SCRIPT not in planned
    previewed = instruction_mcp.preview(repo_root=str(root), provider="CLAUDE.md")
    assert instr.MANAGER_EDIT_SCRIPT not in previewed["providers"]["CLAUDE.md"]["diff"]

    text = _apply_all(root, monkeypatch)["CLAUDE.md"]
    assert "aiworkhub_manager_semantic_edit_prepare" in text
    assert "aiworkhub_manager_semantic_edit_apply" in text
    assert instr.MANAGER_EDIT_SCRIPT not in text


def test_provider_guards_name_the_mcp_pair_in_a_repo_without_the_script(tmp_path: Path) -> None:
    root = _client_repo(tmp_path, script=False)
    provider_tool_guards.apply_repository_guards(root)
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "aiworkhub_manager_semantic_edit_prepare" in text
    assert instr.MANAGER_EDIT_SCRIPT not in text


def test_a_repo_with_the_script_gets_todays_bytes_from_every_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _client_repo(tmp_path / "mcp", script=True)
    for provider, text in _apply_all(root, monkeypatch).items():
        assert text == instr.build_apply_plan(provider, "")["planned_text"]
        assert instr.render_projection(provider) in text
    assert instruction_mcp.apply(repo_root=str(root), provider="CLAUDE.md")["diff"] == ""

    guarded = _client_repo(tmp_path / "guards", script=True)
    provider_tool_guards.apply_repository_guards(guarded)
    for provider in instr.PROVIDERS:
        assert (guarded / provider).read_text(encoding="utf-8") == (root / provider).read_text(encoding="utf-8")


@pytest.mark.parametrize("script", [False, True])
def test_contract_consistency_has_no_projection_drift_right_after_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: bool,
) -> None:
    root = _client_repo(tmp_path, script=script)
    _apply_all(root, monkeypatch)
    report = evidence_instruments.contract_consistency_check(root)
    drift = [blocker for blocker in report["blockers"] if blocker.startswith("projection_drift:")]
    assert drift == []
    assert all(report["carriers"][provider]["exact"] for provider in instr.PROVIDERS)
