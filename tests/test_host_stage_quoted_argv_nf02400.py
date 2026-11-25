"""NF-2026-02400: an env-skip-routed validation row with a quoted multi-word argument.

The declared row ``pytest -q tests/x.py -k "adapter or derive"`` parses to an argv
whose last token carries inner whitespace. The host stage runs argv shell-free, so
that token is one argument: ``_commands`` must accept it (refusing only empty and
NUL tokens) and round-trip it with a quoting join the validation parser reads back
unchanged. Every other refusal keeps its code.
"""

from __future__ import annotations

from typing import Any

import pytest

from aiworkhub import host_validation_stage as hvs
from aiworkhub import validation_skip_audit as audit
from aiworkhub import worker_workspace

COMMAND = 'pytest -q tests/x.py -k "adapter or derive"'
ARGV = ["pytest", "-q", "tests/x.py", "-k", "adapter or derive"]
SKIPPED = ["tests/x.py"]


def _routed_row(command: str = COMMAND) -> dict[str, Any]:
    return {
        "command": command,
        "declared_command": command,
        "returncode": 0,
        audit.ENV_SKIP_CANONICAL: list(SKIPPED),
        audit.HOST_STAGE_COMMAND: command,
    }


def test_declared_row_parses_to_spaced_token() -> None:
    assert worker_workspace.validation_argv(COMMAND) == ARGV


def test_env_skip_routed_quoted_row_yields_exact_host_argv() -> None:
    card = hvs.card_with_env_skip_host_commands(
        {"task_id": "t", "validation": [COMMAND]}, [_routed_row()]
    )
    assert card["host_commands"] == [ARGV]
    spec = hvs.parse_host_spec(card)
    assert spec is not None
    assert spec.commands == (tuple(ARGV),)
    assert spec.env_skip_paths == ((tuple(ARGV), tuple(SKIPPED)),)
    assert hvs.host_command_argv(spec, spec.commands[0]) == [*ARGV, audit.SKIP_REPORT_FLAG]


def test_candidate_changed_route_is_accepted_too() -> None:
    row = _routed_row()
    del row[audit.ENV_SKIP_CANONICAL]
    row[audit.ENV_SKIP_CANDIDATE_CHANGED] = list(SKIPPED)
    spec = hvs.parse_host_spec(
        hvs.card_with_env_skip_host_commands({"validation": [COMMAND]}, [row])
    )
    assert spec is not None and spec.commands == (tuple(ARGV),)


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "diff --check"],
        ["python", "-c", "print('a b')"],
        ["python", "-c", "x\ty"],
        ["pytest", "-k", "it's a or b"],
        ["pytest", "tests\\sub dir\\test_a.py"],
        ["pytest", "-k", '"quoted" and spaced'],
    ],
)
def test_spaced_and_quoted_tokens_round_trip(argv: list[str]) -> None:
    assert hvs.card_host_fields([argv], None) == {"host_commands": [argv]}


@pytest.mark.parametrize(
    ("commands", "code"),
    [
        ([["pytest", "-k", "a\0b"]], "host_command_invalid:0:argv"),
        ([["pytest", ""]], "host_command_invalid:0:argv"),
        ([["pytest"], ["pytest", "\0"]], "host_command_invalid:1:argv"),
        ([[]], "host_command_invalid:0:argv"),
        ([["pytest", 1]], "host_command_invalid:0:argv"),
        ([["x"] * (hvs.MAX_ARGV_TOKENS + 1)], "host_command_invalid:0:argv"),
        ([["x" * (hvs.MAX_TOKEN_CHARS + 1)]], "host_command_invalid:0:argv"),
        ([["x"]] * (hvs.MAX_HOST_COMMANDS + 1), "host_commands_invalid"),
        ("pytest -q", "host_commands_invalid"),
        ([["PYTHONPATH=src", "pytest", "-q"]], "host_command_invalid:0:prefix_form_unsupported"),
        ([["pytest", "a | b"]], "host_command_invalid:0:validation_shell_syntax_forbidden"),
        ([["pytest", "a\nb"]], "host_command_invalid:0:validation_shell_syntax_forbidden"),
    ],
)
def test_other_refusals_keep_their_codes(commands: Any, code: str) -> None:
    with pytest.raises(hvs.HostStageError, match=code):
        hvs.card_host_fields(commands, None)


def test_unparseable_declared_row_still_fails_closed() -> None:
    unparseable = 'pytest -k "adapter or derive'
    card = hvs.card_with_env_skip_host_commands(
        {"validation": [unparseable]}, [_routed_row(unparseable)]
    )
    assert hvs.card_requests_host_stage(card)
    with pytest.raises(hvs.HostStageError, match="host_command_invalid:0:argv"):
        hvs.parse_host_spec(card)
