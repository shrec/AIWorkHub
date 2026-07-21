"""B821 activation tests: dry-run, first-write, idempotent, corrupt, path-escape."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
import sys

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import agent_tool_instruction_cli as cli
from aiworkhub import agent_tool_instruction_mcp as mcp_surface
from aiworkhub import agent_tool_instructions as instr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_repo():
    """Create a temporary directory that acts as a mock repository root."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def tmp_repo_with_owner(tmp_repo):
    """A repo root with an existing AGENTS.md carrying owner text."""
    agents = tmp_repo / "AGENTS.md"
    agents.write_text("# Owner notes\n\nKeep this.\n", encoding="utf-8")
    return tmp_repo


# ---------------------------------------------------------------------------
# Dry-run preview tests
# ---------------------------------------------------------------------------

def test_preview_all_providers_dry_run(tmp_repo):
    """Preview over all three providers is dry-run, writes nothing."""
    result = mcp_surface.preview(repo_root=str(tmp_repo))
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["write_performed"] is False

    for provider in instr.PROVIDERS:
        p = result["providers"][provider]
        assert "diff" in p
        assert instr.START in p["diff"]
        # No file was created
        assert not (tmp_repo / provider).exists()


def test_preview_single_provider_dry_run(tmp_repo):
    """Preview is fine for one provider too."""
    result = mcp_surface.preview(repo_root=str(tmp_repo), provider="AGENTS.md")
    assert result["ok"] is True
    assert "AGENTS.md" in result["providers"]
    assert len(result["providers"]) == 1


def test_inspect_on_missing_file(tmp_repo):
    """Inspect reports missing file without error."""
    result = mcp_surface.inspect_document(
        repo_root=str(tmp_repo), provider="AGENTS.md"
    )
    assert result["ok"] is True
    assert result["file_exists"] is False
    assert result["inspection"]["managed"] is False


def test_inspect_on_clean_owner_text(tmp_repo_with_owner):
    """Inspect sees a clean file (no markers)."""
    result = mcp_surface.inspect_document(
        repo_root=str(tmp_repo_with_owner), provider="AGENTS.md"
    )
    assert result["ok"] is True
    assert result["file_exists"] is True
    assert result["inspection"]["managed"] is False
    assert result["inspection"]["start_count"] == 0


# ---------------------------------------------------------------------------
# First-write and idempotent-write tests
# ---------------------------------------------------------------------------

@mock.patch("aiworkhub.agent_tool_instruction_mcp.core.writes_allowed", return_value=True)
def test_first_write_inserts_managed_block(_mock_gate, tmp_repo_with_owner):
    """First write with gate ON inserts the managed block, preserves owner text."""
    agents = tmp_repo_with_owner / "AGENTS.md"
    original = agents.read_text(encoding="utf-8")

    result = mcp_surface.apply(
        repo_root=str(tmp_repo_with_owner),
        provider="AGENTS.md",
        write=True,
    )
    assert result["ok"] is True
    assert result["write_performed"] is True
    assert result["reason"] in ("append_managed_block", "replace_managed_block")
    assert agents.exists()
    new_text = agents.read_text(encoding="utf-8")
    assert original in new_text  # owner text preserved
    assert instr.START in new_text
    assert instr.END in new_text


@mock.patch("aiworkhub.agent_tool_instruction_mcp.core.writes_allowed", return_value=True)
def test_second_write_is_idempotent(_mock_gate, tmp_repo_with_owner):
    """Writing twice produces the same file (idempotent)."""
    agents = tmp_repo_with_owner / "AGENTS.md"

    r1 = mcp_surface.apply(
        repo_root=str(tmp_repo_with_owner),
        provider="AGENTS.md",
        write=True,
    )
    assert r1["write_performed"] is True

    first_text = agents.read_text(encoding="utf-8")

    r2 = mcp_surface.apply(
        repo_root=str(tmp_repo_with_owner),
        provider="AGENTS.md",
        write=True,
    )
    assert r2["write_performed"] is True
    assert r2["reason"] == "replace_managed_block"

    second_text = agents.read_text(encoding="utf-8")
    assert second_text == first_text


@mock.patch("aiworkhub.agent_tool_instruction_mcp.core.writes_allowed", return_value=True)
def test_write_preserves_owner_text_outside_managed_block(_mock_gate, tmp_repo):
    """Owner text before/after the managed block survives a rewrite."""
    agents = tmp_repo / "AGENTS.md"
    owner_pre = "# Project\n\nCustom config here.\n\n"
    owner_post = "\n\n# Footer\nLast updated.\n"
    agents.write_text(owner_pre + owner_post, encoding="utf-8")

    r = mcp_surface.apply(
        repo_root=str(tmp_repo), provider="AGENTS.md", write=True,
    )
    assert r["write_performed"] is True
    new_text = agents.read_text(encoding="utf-8")
    assert "Custom config here." in new_text
    assert "Last updated." in new_text


# ---------------------------------------------------------------------------
# Corrupt-marker fail-closed tests
# ---------------------------------------------------------------------------

def test_corrupt_marker_fail_closed_dry_run(tmp_repo):
    """Apply dry-run returns ok=False on corrupt markers without mutation."""
    agents = tmp_repo / "AGENTS.md"
    agents.write_text(f"owner\n{instr.START}\nold\n", encoding="utf-8")

    result = mcp_surface.apply(
        repo_root=str(tmp_repo), provider="AGENTS.md", write=False,
    )
    assert result["ok"] is False
    assert result["error"] == "managed_block_marker_corrupt_or_duplicate"


@mock.patch("aiworkhub.agent_tool_instruction_mcp.core.writes_allowed", return_value=True)
def test_corrupt_marker_fail_closed_write(_mock_gate, tmp_repo):
    """Apply with write=True also refuses to write on corrupt markers."""
    agents = tmp_repo / "AGENTS.md"
    corrupt_text = f"owner\n{instr.START}\nold\n"
    agents.write_text(corrupt_text, encoding="utf-8")

    result = mcp_surface.apply(
        repo_root=str(tmp_repo), provider="AGENTS.md", write=True,
    )
    assert result["ok"] is False
    assert result["error"] == "managed_block_marker_corrupt_or_duplicate"
    # File unchanged
    assert agents.read_text(encoding="utf-8") == corrupt_text


def test_duplicate_marker_fail_closed(tmp_repo):
    """Duplicate markers are a corruption case, refused."""
    agents = tmp_repo / "CLAUDE.md"
    agents.write_text(
        f"{instr.START}\nold\n{instr.END}\n{instr.START}\nold2\n{instr.END}\n",
        encoding="utf-8",
    )
    result = mcp_surface.apply(
        repo_root=str(tmp_repo), provider="CLAUDE.md", write=False,
    )
    assert result["ok"] is False
    assert result["error"] == "managed_block_marker_corrupt_or_duplicate"


# ---------------------------------------------------------------------------
# Path escape rejection
# ---------------------------------------------------------------------------

def test_non_existent_repo_root_fails():
    """Nonexistent repo root is rejected immediately."""
    with pytest.raises(ValueError, match="repo_root_does_not_exist"):
        mcp_surface.preview(repo_root="/nonexistent/path/xyz")


def test_unsupported_provider_fails(tmp_repo):
    """Provider outside the three known names is rejected."""
    with pytest.raises(ValueError, match="unsupported_provider"):
        mcp_surface.preview(repo_root=str(tmp_repo), provider="README.md")


def test_path_traversal_provider_fails(tmp_repo):
    """A provider containing '..' is rejected at the provider-name gate."""
    with pytest.raises(ValueError, match="unsupported_provider"):
        mcp_surface.preview(repo_root=str(tmp_repo), provider="../etc/passwd")


def test_symlink_escape_fails(tmp_repo):
    """A provider resolving outside repo_root via symlink is rejected."""
    outside = tmp_repo.parent / "outside.txt"
    outside.write_text("danger", encoding="utf-8")
    link = tmp_repo / "AGENTS.md"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="provider_path_escapes_repo_root"):
        mcp_surface.preview(repo_root=str(tmp_repo), provider="AGENTS.md")


# ---------------------------------------------------------------------------
# Write-gate-off test
# ---------------------------------------------------------------------------

@mock.patch("aiworkhub.agent_tool_instruction_mcp.core.writes_allowed", return_value=False)
def test_write_refused_when_gate_off(_mock_gate, tmp_repo_with_owner):
    """When the write gate is OFF, apply with write=True is rejected."""
    result = mcp_surface.apply(
        repo_root=str(tmp_repo_with_owner),
        provider="AGENTS.md",
        write=True,
    )
    assert result["ok"] is False
    assert result["error"] == "write_gate_disabled"
    assert result["write_performed"] is False


# ---------------------------------------------------------------------------
# CLI surface tests
# ---------------------------------------------------------------------------

def test_cli_preview_all_dry_run(tmp_repo, capsys):
    """CLI preview exits 0 and prints JSON."""
    rc = cli.main(["--repo", str(tmp_repo), "preview"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True


def test_cli_inspect(tmp_repo_with_owner, capsys):
    """CLI inspect exits 0."""
    rc = cli.main(
        ["--repo", str(tmp_repo_with_owner), "inspect", "--provider", "AGENTS.md"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["ok"] is True


def test_cli_apply_dry_run_by_default(tmp_repo_with_owner, capsys):
    """CLI apply without --write is a dry-run."""
    rc = cli.main(
        ["--repo", str(tmp_repo_with_owner), "apply", "--provider", "AGENTS.md"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["write_performed"] is False


@mock.patch("aiworkhub.agent_tool_instruction_mcp.core.writes_allowed", return_value=True)
def test_cli_apply_with_write_flag(_mock_gate, tmp_repo_with_owner, capsys):
    """CLI apply --write performs the write."""
    rc = cli.main(
        ["--repo", str(tmp_repo_with_owner), "apply", "--provider", "AGENTS.md", "--write"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["write_performed"] is True


def test_cli_bad_provider_fails(tmp_repo, capsys):
    """CLI rejects an unknown provider with exit 1."""
    rc = cli.main(
        ["--repo", str(tmp_repo), "inspect", "--provider", "README.md"]
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# Eval artifact cross-check
# ---------------------------------------------------------------------------

def test_eval_artifact_activation_b821_v1():
    """B821 eval artifact is valid JSON and matches rendered policy."""
    path = Path(
        "tools/geoai-task-mcp/eval/aiworkhub_agent_tool_instruction_activation_b821_v1.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert (
        payload["schema_id"]
        == "aiworkhub.task_mcp.agent_tool_instruction_activation_b821_v1.eval.v1"
    )
    # Canonical policy matches the generator
    assert payload["canonical_bytes"] == len(
        instr.render_canonical().encode("utf-8")
    )
    # Each provider has a projection
    for provider in instr.PROVIDERS:
        assert provider in payload["projections"]
        assert payload["projections"][provider]["bytes"] == len(
            instr.render_projection(provider).encode("utf-8")
        )
    # MCP tools are enumerated
    assert set(payload["mcp_tool_names"]) == set(mcp_surface.MCP_TOOL_NAMES)
    # CLI commands are enumerated
    assert payload["cli_commands"] == ["preview", "inspect", "apply"]
