"""NF-2026-02270: MCP tool surface inventory and the phase-1 facet folds.

needfix_count folds into aiworkhub_needfix_list(count_only=true[, breakdown]) and
aiworkhub_task_codex_handoff_markdown into aiworkhub_task_codex_handoff
(format='markdown'). The old names stay one release as thin aliases that
delegate and name the surviving tool in deprecated_alias_of, and
mcp_tool_metadata marks them for a later card to drop. NF-2026-00964 unlisted
both aliases on manager (still callable by exact name).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import (  # noqa: E402
    core,
    dashboard_mcp_app,
    mcp_tool_metadata,
    needfix_store,
    review_summarizer,
    server,
    task_store,
)

SCRIPT = ROOT / "scripts" / "mcp_tool_inventory.py"
ALIAS_KEYS = frozenset({"server_tool", mcp_tool_metadata.DEPRECATED_ALIAS_REPLY_KEY})
COUNT_FILTERS = (
    {},
    {"status": "captured"},
    {"status": "archived"},
    {"kind": "bug"},
    {"severity": "high"},
)


def _without_alias_keys(reply: dict[str, Any]) -> bytes:
    kept = {key: value for key, value in reply.items() if key not in ALIAS_KEYS}
    return json.dumps(kept, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")


def _load_inventory_module() -> Any:
    spec = importlib.util.spec_from_file_location("mcp_tool_inventory_nf02270", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    task_store.initialize_repository(tmp_path)
    needfix_store.capture_proposal(tmp_path, title="live one", description="open")
    needfix_store.capture_proposal(tmp_path, title="live two", description="open")
    return tmp_path


@pytest.mark.parametrize("filters", COUNT_FILTERS, ids=lambda f: ",".join(f) or "unfiltered")
def test_count_only_returns_the_needfix_count_integer_and_breakdown(repo: Path, filters: dict) -> None:
    surviving = server.needfix_list(count_only=True, **filters)
    alias = server.needfix_count(**filters)

    assert isinstance(surviving, int) and not isinstance(surviving, bool)
    assert int(surviving) == int(core.needfix_count(**filters)) == alias["count"]
    assert alias["derived"] == surviving.derived
    assert alias["underived_reason"] == surviving.underived_reason
    assert alias["deprecated_alias_of"] == "aiworkhub_needfix_list"
    assert alias["server_tool"] == "needfix_count"
    if filters in ({}, {"status": "captured"}):
        assert int(surviving) == 2

    surviving_breakdown = server.needfix_list(count_only=True, breakdown=True, **filters)
    alias_breakdown = server.needfix_count(breakdown=True, **filters)
    assert surviving_breakdown == core.needfix_count(breakdown=True, **filters)
    assert {k: v for k, v in alias_breakdown.items() if k not in ALIAS_KEYS} == surviving_breakdown
    assert alias_breakdown["deprecated_alias_of"] == "aiworkhub_needfix_list"
    assert surviving_breakdown["count"] == int(surviving)


def test_count_only_refuses_filters_it_cannot_honour() -> None:
    assert server.needfix_list(count_only=True, query="x") == {
        "ok": False,
        "error": "count_only_filter_unsupported",
        "unsupported": ["query"],
    }
    assert server.needfix_list(count_only=True, include_archived=True)["unsupported"] == ["include_archived"]
    assert server.needfix_list(breakdown=True) == {"ok": False, "error": "breakdown_requires_count_only"}


def test_default_needfix_list_still_returns_rows(repo: Path) -> None:
    rows = server.needfix_list()
    assert isinstance(rows, list) and len(rows) == 2
    assert {row["title"] for row in rows} == {"live one", "live two"}


def test_dashboard_work_state_caller_still_works(repo: Path) -> None:
    # dashboard_mcp_app._needfix_by_work_state is the core.needfix_count(breakdown=True) caller.
    counts, error = dashboard_mcp_app._needfix_by_work_state(None, None, None)
    assert error is None
    assert set(counts) == set(needfix_store.WORK_STATES)
    assert counts["untriaged"] == 2


@pytest.fixture
def empty_review_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        review_summarizer,
        "_collect_review_cards",
        lambda run, show: ("TASK review queue: empty", [], [], []),
    )


def test_codex_handoff_markdown_format_is_byte_identical_to_the_alias(empty_review_queue: None) -> None:
    kwargs = {"task_ids": ["T-nf02270"], "batch_label": "nf02270"}
    surviving = server.aiworkhub_task_codex_handoff(format="markdown", **kwargs)
    alias = server.aiworkhub_task_codex_handoff_markdown(**kwargs)

    assert surviving["server_tool"] == "aiworkhub_task_codex_handoff"
    assert alias["server_tool"] == "aiworkhub_task_codex_handoff_markdown"
    assert alias["deprecated_alias_of"] == "aiworkhub_task_codex_handoff"
    assert isinstance(surviving["markdown"], str) and surviving["markdown"]
    assert alias["markdown"].encode("utf-8") == surviving["markdown"].encode("utf-8")
    assert _without_alias_keys(alias) == _without_alias_keys(surviving)
    # The folded path is the pre-fold markdown report, unchanged.
    direct = review_summarizer.build_codex_handoff_markdown_report(**kwargs)
    assert _without_alias_keys(surviving) == _without_alias_keys(direct)


def test_codex_handoff_default_format_is_unchanged_and_unknown_format_refused(empty_review_queue: None) -> None:
    plain = server.aiworkhub_task_codex_handoff(task_ids=["T-nf02270"])
    assert "markdown" not in plain and plain["server_tool"] == "aiworkhub_task_codex_handoff"
    direct = review_summarizer.build_codex_handoff_report(task_ids=["T-nf02270"])
    assert _without_alias_keys(plain) == _without_alias_keys(direct)
    refused = server.aiworkhub_task_codex_handoff(format="html")
    assert refused == {"ok": False, "error": "unknown_format", "format": "html", "accepted": ["json", "markdown"]}


def _server_function(tool_name: str) -> Any:
    # NF-2026-00964: a prefix-renamed tool's Python function keeps its pre-rename name.
    renamed = tool_name.removeprefix("aiworkhub_")
    if renamed in mcp_tool_metadata.MCP_PREFIX_RENAMED_TOOLS:
        return getattr(server, renamed)
    return getattr(server, tool_name)


def test_aliases_are_marked_in_metadata_and_are_manager_callable_but_unlisted() -> None:
    marks = mcp_tool_metadata.MCP_DEPRECATED_FACET_ALIASES
    assert {alias: spec["surviving_tool"] for alias, spec in marks.items()} == {
        "needfix_count": "aiworkhub_needfix_list",
        "aiworkhub_task_codex_handoff_markdown": "aiworkhub_task_codex_handoff",
    }
    for alias, spec in marks.items():
        surviving = spec["surviving_tool"]
        assert alias not in mcp_tool_metadata.MCP_TOOL_ALIASES
        # NF-2026-00964: unlisted on manager, still callable by exact name until removal.
        assert dict(mcp_tool_metadata.MCP_TOOL_PROFILES[alias]) == {"manager": "callable"}
        assert server.mcp_tool_callable(alias, "manager") is True
        assert server.mcp_tool_listed(alias, "manager") is False
        assert server.mcp_tool_listed(surviving, "manager") is True
        assert set(spec["facet"]) <= set(inspect.signature(_server_function(surviving)).parameters)
        assert f"Deprecated alias of {surviving}" in (getattr(server, alias).__doc__ or "")


def test_deprecated_alias_reply_refuses_unmarked_names_and_non_mappings() -> None:
    with pytest.raises(KeyError):
        mcp_tool_metadata.deprecated_alias_reply("aiworkhub_needfix_list", {})
    with pytest.raises(TypeError):
        mcp_tool_metadata.deprecated_alias_reply("needfix_count", 3)  # type: ignore[arg-type]
    stamped = mcp_tool_metadata.deprecated_alias_reply("needfix_count", {"count": 1})
    assert stamped == {
        "count": 1, "server_tool": "needfix_count", "deprecated_alias_of": "aiworkhub_needfix_list",
    }


def test_dashboard_snapshot_stays_with_a_recorded_reason() -> None:
    reason = mcp_tool_metadata.MCP_FOLD_KEPT_REASONS["aiworkhub_dashboard_snapshot"]
    assert "not a duplicate" in reason
    assert "aiworkhub_dashboard_snapshot" not in mcp_tool_metadata.MCP_DEPRECATED_FACET_ALIASES
    assert server.mcp_tool_listed("aiworkhub_dashboard_snapshot", "manager") is False
    assert server.mcp_tool_listed("aiworkhub_dashboard_snapshot", "dashboard") is True


def test_inventory_ledger_counts_are_null_when_unmeasured(tmp_path: Path) -> None:
    inventory = _load_inventory_module()
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    names = ["a_tool", "b_tool", "c_tool"]
    counts, status = inventory.ledger_call_counts(tmp_path / "absent.jsonl", names, now=now)
    assert counts == {"a_tool": None, "b_tool": None, "c_tool": None}
    assert status["state"] == "absent"

    ledger = tmp_path / "audit.jsonl"
    entries = [
        {"timestamp": (now - timedelta(days=1)).isoformat(), "tool_name": "a_tool"},
        {"timestamp": (now - timedelta(days=29)).isoformat(), "tool_name": "a_tool"},
        {"timestamp": (now - timedelta(days=31)).isoformat(), "tool_name": "b_tool"},
        {"timestamp": (now - timedelta(days=1)).isoformat(), "tool_name": "unregistered"},
    ]
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n{not json\n", encoding="utf-8")
    counts, status = inventory.ledger_call_counts(ledger, names, now=now)
    assert counts == {"a_tool": 2, "b_tool": 0, "c_tool": None}
    assert status["state"] == "read" and status["entries_malformed"] == 1


def test_inventory_refuses_a_report_outside_scratch(tmp_path: Path) -> None:
    inventory = _load_inventory_module()
    assert inventory._scratch_target(tmp_path, "r.json") == (tmp_path / ".scratch" / "r.json").resolve()
    for out in ("../escape.json", ".", str(tmp_path / "elsewhere.json")):
        with pytest.raises(SystemExit):
            inventory._scratch_target(tmp_path, out)


def test_inventory_script_lists_every_registered_tool(tmp_path: Path) -> None:
    ledger = tmp_path / ".aiworkhub" / "runtime" / "process_logs" / "audit.jsonl"
    ledger.parent.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    entries = [
        {"timestamp": (now - timedelta(days=1)).isoformat(), "tool_name": "aiworkhub_needfix_list"},
        {"timestamp": (now - timedelta(days=2)).isoformat(), "tool_name": "aiworkhub_needfix_list"},
        {"timestamp": (now - timedelta(days=45)).isoformat(), "tool_name": "aiworkhub_needfix_show"},
    ]
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("AIWORKHUB_AUDIT_LOG_PATH", None)
    env["AIWORKHUB_REPO_ROOT"] = str(tmp_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(tmp_path), "--writes", "on"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads((tmp_path / ".scratch" / "mcp_tool_inventory.json").read_text(encoding="utf-8"))

    rows = {row["name"]: row for row in report["tools"]}
    assert len(rows) == len(report["tools"]) == report["registered_tool_count"] > 100
    assert report["totals"]["all"]["tool_count"] == report["registered_tool_count"]
    for name, row in rows.items():
        assert isinstance(row["schema_bytes"], int) and row["schema_bytes"] > 0, name
        assert isinstance(row["role"], dict) and row["subsystem"], name
        assert row["call_count_30d"] is None or isinstance(row["call_count_30d"], int), name
        assert name in proc.stdout
    assert rows["aiworkhub_needfix_list"]["call_count_30d"] == 2
    assert rows["aiworkhub_needfix_show"]["call_count_30d"] == 0
    assert rows["needfix_list"]["call_count_30d"] is None
    assert rows["needfix_count"]["call_count_30d"] is None
    assert rows["needfix_count"]["deprecated_alias_of"] == "aiworkhub_needfix_list"
    assert rows["aiworkhub_task_codex_handoff_markdown"]["deprecated_alias_of"] == "aiworkhub_task_codex_handoff"
    assert rows["aiworkhub_dashboard_snapshot"]["fold_kept_reason"]
    assert rows["aiworkhub_needfix_list"]["role"] == {"manager": "listed"}
    assert rows["needfix_list"]["role"] == {"manager": "callable"}
    assert rows["needfix_count"]["role"] == {"manager": "callable"}
    assert rows["aiworkhub_needfix_list"]["subsystem"] == "aiworkhub_needfix"
    assert rows["aiworkhub_task_codex_handoff"]["subsystem"] == "aiworkhub_task"

    manager = report["totals"]["manager"]
    manager_listed = [row for row in rows.values() if row["role"].get("manager") == "listed"]
    assert manager["tool_count"] == len(manager_listed)
    assert manager["schema_bytes"] == sum(row["schema_bytes"] for row in manager_listed)
