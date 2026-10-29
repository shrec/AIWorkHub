"""NF-2026-01825: a ranged Source Graph file read returns exactly the requested lines."""

import json

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import worker_ai_tools_mcp as wtm
from aiworkhub.repository_state import bootstrap_repository

MODULE = "pkg/window_mod.py"
WINDOW = (200, 209)
LEGACY_MARGIN = 20
IMPORT_LINES = [f"import module_{i:02d}_dependency" for i in range(1, 25)]
BODY_LINES = [f"VALUE_{i:04d} = {i}  # window payload row" for i in range(1, 377)]
MODULE_LINES = IMPORT_LINES + BODY_LINES


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("sg_window_bytes")
    bootstrap_repository(root, repo_name="sg_window_bytes")
    (root / "pkg").mkdir(exist_ok=True)
    (root / MODULE).write_text("\n".join(MODULE_LINES) + "\n", encoding="utf-8")
    sg.build_index(root, incremental=False)
    return root


def _reply_bytes(result):
    return len(json.dumps(result))


def _legacy_bytes(repo, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(sg, "FILE_LINE_RANGE_MARGIN", LEGACY_MARGIN)
        legacy = sg.file_query(repo, MODULE, 64, line_range=WINDOW, include_structure=True)
    return _reply_bytes(legacy)


def _emit(capsys, **fields):
    metric = {"metric": "sg_file_window_reply_bytes", "unit": "bytes", **fields}
    with capsys.disabled():
        print("\nAIWORKHUB_METRIC:" + json.dumps(metric))


def test_baseline_legacy_window_reply_bytes(repo, monkeypatch, capsys):
    assert len(MODULE_LINES) == 400
    value = _legacy_bytes(repo, monkeypatch)
    assert value > 0
    _emit(capsys, value=value)


def test_delta_exact_window_reply_bytes(repo, monkeypatch, capsys):
    result = sg.file_query(repo, MODULE, 64, line_range=WINDOW)
    context = result["contexts"][0]
    assert context["source_preview_line_start"] == 200
    assert context["source_preview_line_end"] == 209
    assert context["source_preview"].splitlines() == MODULE_LINES[199:209]
    assert context["file"]["file_path"] == MODULE
    assert "entities" not in context and "edges" not in context

    legacy = _legacy_bytes(repo, monkeypatch)
    value = _reply_bytes(result)
    assert value <= 0.5 * legacy
    _emit(capsys, value=value, direction="lower", max_regression_percent=0)


def test_include_structure_restores_module_entities_and_edges(repo):
    context = sg.file_query(
        repo, MODULE, 64, line_range=WINDOW, include_structure=True,
    )["contexts"][0]
    assert context["source_preview_line_start"] == 200
    assert context["source_preview_line_end"] == 209
    assert "entities" in context and "edges" in context


def test_unranged_file_read_still_carries_structure(repo):
    context = sg.file_query(repo, MODULE, 64)["contexts"][0]
    assert "entities" in context and "edges" in context
    assert "source_preview_line_start" not in context


def test_unindexed_declared_input_fallback_returns_only_the_requested_lines(tmp_path):
    root = tmp_path / "declared"
    (root / "data").mkdir(parents=True)
    lines = [f"row {i:03d}" for i in range(1, 101)]
    (root / "data" / "input.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ctx = wtm.WorkerToolContext(
        task_id="nf01825-task",
        runner="test",
        topic="sg_window_bytes",
        request_id="nf01825-request",
        repo=root,
        authority_repo=root,
        source_graph_targets=("data/input.txt",),
        allowed_writes=(),
        session_topic="sg_window_bytes",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        rework_overlay_packet=None,
    )

    ranged = wtm._declared_input_file_payload(ctx, "data/input.txt", (40, 44))
    row = ranged["matches"][0]
    assert row["kind"] == "declared_input_file"
    assert row["fallback_reason"] == "declared_input_unindexed"
    assert row["preview"].splitlines() == lines[39:44]
    assert row["source_preview_line_start"] == 40
    assert row["source_preview_line_end"] == 44
    assert row["source_preview_total_lines"] == 100
    assert ranged["truncated"] is False

    head = wtm._declared_input_file_payload(ctx, "data/input.txt")["matches"][0]
    assert head["preview"].splitlines() == lines
    assert "source_preview_line_start" not in head
