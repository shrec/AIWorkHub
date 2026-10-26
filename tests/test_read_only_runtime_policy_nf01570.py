"""NF-2026-01570: a read-only card's prompt must match its own tool list.

A read-only seat holds no ``aiworkhub_worker_validation_run`` -- its Claude tool
list never grants one -- yet the shared WORKER_RUNTIME_POLICY ordered every card
to run validation ONLY through that tool. The measured worker obeyed, was
refused, and stopped with zero audit content. The chosen fix shape is the
read-only prompt variant, not a tool grant: the sandbox/tool boundary is
unchanged, and every non-read-only render stays byte-identical.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import agent_tool_instructions as instr  # noqa: E402
from aiworkhub import process_launcher  # noqa: E402
from aiworkhub import runtime_adapters  # noqa: E402

# A copy of the old text: the exact validation sentence block that sat inline in
# WORKER_RUNTIME_POLICY before it moved into _WORKER_VALIDATION_RULE. The
# rendered default policy must still contain these bytes verbatim.
_OLD_VALIDATION_BLOCK = (
    "run validation ONLY through aiworkhub_worker_validation_run: it resolves the\n"
    "card's own declared command for you, executes it in this sandbox and returns the\n"
    "returncode, failure class, pytest short-summary lines and a bounded tail with\n"
    "the full log addressable by sha256. Never retype a validation command and never\n"
    "type pytest, ruff or mypy into Bash. On some transports Bash refuses those\n"
    "outright; on the rest nothing stops you, and typing one anyway produces an\n"
    "unreceipted run the supervisor cannot read -- so the result does not count\n"
    "either way. Re-running the tool on unchanged bytes returns the cached receipt;\n"
    "pass force=true to re-execute."
)

# Byte-identity is pinned by the copy of the old text above; see the final test.


def test_a_read_only_card_is_never_told_to_run_the_validation_tool() -> None:
    """NF-2026-01570: the read-only prompt names no validation tool at all."""
    policy = instr.render_worker_runtime_policy("claude_cli", read_only=True)
    assert "aiworkhub_worker_validation_run" not in policy
    assert "this card is read-only" in policy
    assert "aiworkhub_worker_ai_tools__nothing" not in policy


def test_the_default_seat_still_routes_validation_through_the_bounded_runner() -> None:
    for adapter_id in (None, "claude_cli", "opencode_cli"):
        default = instr.render_worker_runtime_policy(adapter_id)
        assert default == instr.render_worker_runtime_policy(
            adapter_id, read_only=False
        )
        assert "run validation ONLY through aiworkhub_worker_validation_run" in default


def test_the_read_only_text_agrees_with_the_read_only_tool_list() -> None:
    read_only = set(runtime_adapters.claude_allowed_tools(read_only=True))
    granted = set(runtime_adapters.claude_allowed_tools(read_only=False))
    assert not any(tool.endswith("validation_run") for tool in read_only)
    assert any(tool.endswith("validation_run") for tool in granted)


def test_build_worker_prompt_matches_the_card_seat() -> None:
    def _stable_prefix(card: dict) -> str:
        prompt = process_launcher.build_worker_prompt(
            task_id="T1", runner="claude_worker", topic="coding", card=card
        )
        return prompt.split("\n\nTASK_CONTRACT_JSON:\n", 1)[0]

    read_only = _stable_prefix({"read_only": True})
    assert "aiworkhub_worker_validation_run" not in read_only
    assert "this card is read-only" in read_only
    granted = _stable_prefix({"read_only": False})
    assert "run validation ONLY through aiworkhub_worker_validation_run" in granted


def test_the_default_render_is_byte_identical_after_the_move() -> None:
    """The move of the validation block must render byte-identically.

    Pinned by a copy of the old text rather than a pre-change sha256 literal:
    ``aiworkhub_worker_validation_run`` is tool_unavailable in this sandbox
    (outside the worker AppContainer), so no pre-change render could be
    measured here. ``_WORKER_VALIDATION_RULE == _OLD_VALIDATION_BLOCK`` plus a
    single verbatim occurrence in the render proves the interpolation replaced
    exactly the old bytes, and the hash pin is left to the coordinator's
    canonical validation run.
    """
    default = instr.render_worker_runtime_policy()
    assert instr._WORKER_VALIDATION_RULE == _OLD_VALIDATION_BLOCK
    assert _OLD_VALIDATION_BLOCK in default
    assert default.count(_OLD_VALIDATION_BLOCK) == 1
    assert default == instr.WORKER_RUNTIME_POLICY
