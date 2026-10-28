"""NF-2026-01808: Source Graph health/refresh_now MCP replies default to a summary.

The daemon payload stays raw in core and in every in-process caller; only the
two MCP wrappers project it, and ``detail="full"`` returns it unchanged.
"""

from __future__ import annotations

import copy
import json

import pytest

from aiworkhub import mcp_summary_folds, server

_SUMMARY_MAX_BYTES = 2048

_TOOLS = (
    ("aiworkhub_source_graph_health", "source_graph_health"),
    ("aiworkhub_source_graph_refresh_now", "source_graph_refresh_now"),
)


def _health_payload() -> dict:
    """A payload shaped like the measured ~7 KB health reply."""

    return {
        "ok": True,
        "repo_id": "repo_be72b4028e3c4e789badc4d5d631d4bd",
        "status": "ready",
        "running": True,
        "indexing": False,
        "last_error": None,
        "last_run_at": "2026-10-29T10:00:00+00:00",
        "generation": 412,
        "language_capabilities": {
            lang: {"parser": "tree_sitter", "symbols": True, "calls": True, "imports": True, "version": "0.21.3"}
            for lang in ("python", "javascript", "typescript", "rust", "go", "c", "cpp", "java", "csharp", "ruby")
        },
        "indexed_extensions": [f".ext{i}" for i in range(60)],
        "index_quality": {
            "degraded": True,
            "degraded_reasons": ["unindexed_files_present"],
            "files": 613,
            "unindexed": 3,
            "per_language": {f"lang{i}": {"files": i * 7, "entities": i * 91} for i in range(20)},
        },
        "integrity": {
            "status": "ok",
            "checked_at": "2026-10-29T09:59:00+00:00",
            "tables": {f"table_{i}": {"rows": i * 1000, "sha256": "a" * 64} for i in range(10)},
        },
        "recommendation_resolvability": {
            "status": "resolvable",
            "sampled": 48,
            "unresolved": [],
            "samples": [{"qualname": f"src/m{i}.py.f{i}", "resolved": True} for i in range(20)],
        },
        "last_repair": {"state": "idle", "at": None, "details": {"steps": ["vacuum", "reindex"] * 5}},
        "last_known_good_generation": {"generation": 411, "sha256": "b" * 64, "at": "2026-10-29T09:00:00+00:00"},
        "last_report": {
            "ok": True,
            "files_indexed": 12,
            "changed": [f"src/aiworkhub/module_{i}.py" for i in range(40)],
            "timings_ms": {f"phase_{i}": i * 13 for i in range(15)},
        },
        "refresh_job": {
            "state": "succeeded",
            "job_id": "job-7f3a",
            "report": {"changed": [f"src/x{i}.py" for i in range(30)], "errors": []},
        },
    }


def _install_core(monkeypatch, payload, calls: list) -> None:
    for _tool, core_name in _TOOLS:

        def fake(core_name=core_name):
            calls.append(core_name)
            return copy.deepcopy(payload) if isinstance(payload, dict) else payload

        monkeypatch.setattr(server.core, core_name, fake)


def _compact_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))


def test_fixture_is_large_enough_to_need_a_summary() -> None:
    assert _compact_bytes(_health_payload()) > 5000


@pytest.mark.parametrize("tool,core_name", _TOOLS)
def test_full_returns_the_core_payload_unchanged(monkeypatch, tool, core_name) -> None:
    fixed = {"ok": True, "status": "ready", "index_quality": {"degraded": False, "x": [1, 2]}}
    calls: list = []
    _install_core(monkeypatch, fixed, calls)

    result = getattr(server, tool)(detail="full")

    assert result == fixed
    assert json.dumps(result, sort_keys=True) == json.dumps(fixed, sort_keys=True)
    assert calls == [core_name]


@pytest.mark.parametrize("tool,core_name", _TOOLS)
def test_summary_is_the_default_and_bounded(monkeypatch, tool, core_name) -> None:
    payload = _health_payload()
    calls: list = []
    _install_core(monkeypatch, payload, calls)

    result = getattr(server, tool)()

    assert calls == [core_name]
    assert result["detail"] == "summary"
    assert result["detail_request"] == {"full": {"tool": tool, "detail": "full"}}
    assert _compact_bytes(result) <= _SUMMARY_MAX_BYTES

    for key, value in payload.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            assert result[key] == value
        else:
            assert result[key]["summarized"] is True
            assert result[key] == {
                **mcp_summary_folds._fold_to_identity(value),
                **{k: result[key][k] for k in result[key] if k not in ("summarized", "bytes", "sha256")},
            }
            assert result[key]["sha256"] == mcp_summary_folds._fold_to_identity(value)["sha256"]

    assert result["index_quality"]["degraded"] is True
    assert result["index_quality"]["degraded_reasons"] == ["unindexed_files_present"]
    assert result["integrity"]["status"] == "ok"
    assert result["recommendation_resolvability"]["status"] == "resolvable"
    assert result["refresh_job"]["state"] == "succeeded"
    assert result["last_repair"]["state"] == "idle"
    assert result["last_report"]["ok"] is True
    assert "per_language" not in result["index_quality"]
    assert "report" not in result["refresh_job"]


def test_fold_keeps_only_scalar_status_members() -> None:
    folded = mcp_summary_folds.fold_source_graph_health_summary(
        {
            "block": {"status": {"nested": 1}, "state": None, "ok": False, "degraded_reasons": "not-a-list"},
            "items": [1, 2, 3],
        }
    )

    block = folded["block"]
    assert "status" not in block
    assert block["state"] is None
    assert block["ok"] is False
    assert "degraded_reasons" not in block
    assert folded["items"] == mcp_summary_folds._fold_to_identity([1, 2, 3])


@pytest.mark.parametrize("tool,core_name", _TOOLS)
@pytest.mark.parametrize("payload", [None, "raw", ["a", "b"]])
def test_non_dict_core_results_pass_through(monkeypatch, tool, core_name, payload) -> None:
    calls: list = []
    _install_core(monkeypatch, payload, calls)

    assert getattr(server, tool)() == payload
    assert calls == [core_name]


@pytest.mark.parametrize("tool,core_name", _TOOLS)
def test_unknown_detail_is_refused_without_calling_core(monkeypatch, tool, core_name) -> None:
    calls: list = []
    _install_core(monkeypatch, _health_payload(), calls)

    result = getattr(server, tool)(detail="bogus")

    assert result == server._detail_refusal("bogus")
    assert result["ok"] is False
    assert result["error"] == "invalid_detail"
    assert calls == []
