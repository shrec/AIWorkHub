"""NF-2026-02114: read-only summary surfaces never decode whole task cards.

``task_store.list_task_cards`` ran ``SELECT t.*`` and json-decoded every
card_json (history, evidence, review packets, launch receipts) for up to 5000
rows, while ``task_plan_snapshot``, the dashboard snapshot panels and the
workforce rank read only the columns plus a few card_json keys. These tests
pin that those paths now read ``task_store.TASK_SUMMARY_PROJECTION``, that
their replies are identical to the full-card read on a seeded store (golden
comparison), that ``_decode_task_card`` is never called for their scan, and
that the summary read's Python peak is a small fraction of the full read's.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    core,
    dashboard,
    needfix_store,
    task_store,
    workforce_catalog,
    workforce_router,
)

_NOW = "2026-11-06T00:00:00+00:00"
_HISTORY = [{"event": "note", "body": "h" * 512} for _ in range(8)]


def _insert(
    repo: Path,
    task_id: str,
    *,
    card_json: str | None = None,
    runner: str = "glm",
    topic: str = "coding",
    status: str = "pending",
    worker_status: str = "unclaimed",
    priority: str = "",
    archived_at: str = "",
    updated_at: str = _NOW,
    **card: Any,
) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    if card_json is None:
        card_json = json.dumps({
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "status": status,
            "worker_status": worker_status,
            "priority": priority,
            "history": _HISTORY,
            **card,
        })
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, mode, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, origin_thread_id, archived_at) "
            "VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                task_id, runner, topic, status, worker_status, priority,
                f"objective {task_id}", card_json, _NOW, updated_at, archived_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed(repo: Path) -> None:
    ref_key = sorted(needfix_store._EXPLICIT_NEEDFIX_REF_KEYS)[0]
    big = {"stdout": "v" * 4096}
    _insert(repo, "T-root", status="pending", priority="high", allowed_writes=["src/a.py"])
    _insert(
        repo, "T-child", depends_on=["T-root"], allowed_writes=["src/b.py"],
        rework_predecessor={"task_id": "T-prev", "reason": "rework"}, claim_epoch=2,
    )
    _insert(
        repo, "T-proc", status="processing", worker_status="claimed",
        allowed_writes=["src/**"], launch_request_id="",
    )
    _insert(repo, "T-launched", status="processing", worker_status="claimed",
            allowed_writes=["src/c.py"], launch_request_id="req-1")
    _insert(repo, "T-opblock", operational_blocker={"reason": "adapter_down", "kind": "x"},
            allowed_writes=["docs/x.md"])
    _insert(repo, "T-collide", operational_blocker={"reason": "collision_guard_failed"},
            allowed_writes=["src/a.py"])
    _insert(
        repo, "T-old", status="archived", archived_at=_NOW,
        archive_operation="superseded", superseded_by="T-new",
    )
    _insert(repo, "T-new", status="finished", worker_status="done", allowed_writes=["src/n.py"])
    _insert(repo, "T-on-old", depends_on=["T-old"], allowed_writes=["src/o.py"])
    _insert(repo, "T-bad-dep", depends_on="not-a-list")
    _insert(
        repo, "QR-1", topic="quality_review",
        quality_review={"target_task_id": "T-new", "target_status": "finished", "body": big},
    )
    _insert(
        repo, "T-review", status="review", worker_status="review",
        terminal_review={
            "substatus": "review_ready",
            "evidence": {
                "model": "glm-5.2", "adapter_id": "glm_vscode_lm",
                "validation": big, "quality_review": {"target_task_id": "", "log": big},
            },
        },
        accepted_request_id="req-a", accepted_by="manager", accepted_at=_NOW,
        accept_evidence={"validation": big},
        project_context={"task_type": "code", "sections": [big]}, risk_tier="high",
    )
    _insert(
        repo, "T-fail", status="blocked", worker_status="blocked",
        terminal_failure={
            "substatus": "process_lost",
            "evidence": {"provider_error": {"kind": "quota", "status": 429}, "log": big},
        },
        terminal_review={"substatus": "", "evidence": {"model": "", "adapter_id": "glm_vscode_lm"}},
        deterministic_verification={"passed": False, "detail": big},
        project_context={"task_type": "research"}, risk_tier="low",
        **{ref_key: "NF-2026-00001"},
    )
    _insert(repo, "T-topic-fallback", topic="", origin_thread_id="thread-1",
            card_json=json.dumps({"topic": "fallback-topic", "origin_thread_id": "thread-1"}))
    _insert(repo, "T-malformed", card_json="{not json")
    _insert(repo, "T-array", card_json="[1, 2]")
    _insert(repo, "T-weird", terminal_review="not-an-object", quality_review=5,
            retry_target={"task_id": "T-new", "kind": "reviewer"})
    _insert(repo, "T-null-evidence", status="finished", worker_status="done",
            terminal_review={"substatus": "review_ready", "evidence": None})
    _insert(repo, "T-bool-epoch", claim_epoch=True, rework_predecessor={})
    _insert(repo, "T-column-wins", runner="codex", priority="low",
            card_json=json.dumps({"runner": "stale", "priority": "critical", "topic": "x"}))


@pytest.fixture()
def repo(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    task_store.initialize_repository(root)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    _seed(root)
    return root


def _full_cards(repo: Path) -> list[dict[str, Any]]:
    return task_store.list_task_cards(repo, limit=5000)


def _summary_cards(repo: Path) -> list[dict[str, Any]]:
    return task_store.list_task_cards(
        repo, limit=5000, projection=task_store.TASK_SUMMARY_PROJECTION
    )


def _canon(value: Any) -> str:
    def normalize(node: Any) -> Any:
        if isinstance(node, dict):
            return {repr(key) if not isinstance(key, str) else key: normalize(item)
                    for key, item in node.items()}
        if isinstance(node, (list, tuple)):
            return [normalize(item) for item in node]
        return node

    return json.dumps(normalize(value), sort_keys=True, default=str)


class _DecodeSpy:
    def __init__(self, monkeypatch) -> None:
        self.task_ids: list[str] = []
        real = task_store._decode_task_card

        def spy(row, *args, **kwargs):
            self.task_ids.append(str(row["task_id"]))
            return real(row, *args, **kwargs)

        monkeypatch.setattr(task_store, "_decode_task_card", spy)


# -- the projection itself ---------------------------------------------------


def test_summary_cards_answer_every_projected_read_like_the_full_card(repo) -> None:
    full = _full_cards(repo)
    summary = _summary_cards(repo)
    assert [c["task_id"] for c in summary] == [c["task_id"] for c in full]
    value_paths, typed_paths = task_store._task_summary_layout()

    def walk(card: dict[str, Any], path: tuple[str, ...]) -> Any:
        node: Any = card
        for key in path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

    for full_card, summary_card in zip(full, summary):
        for column in task_store._TASK_SUMMARY_COLUMNS:
            assert summary_card[column] == full_card[column], (full_card["task_id"], column)
        for path in value_paths:
            assert walk(summary_card, path) == walk(full_card, path), (full_card["task_id"], path)
        for path in typed_paths:
            assert isinstance(walk(summary_card, path), dict) == isinstance(
                walk(full_card, path), dict
            ), (full_card["task_id"], path)
        assert "history" not in summary_card
        assert len(_canon(summary_card)) < len(_canon(full_card)) or full_card["task_id"] in {
            "T-malformed", "T-array", "T-topic-fallback", "T-column-wins",
        }


def test_summary_projection_point_and_bulk_reads_match_the_scan(repo) -> None:
    scan = {c["task_id"]: c for c in _summary_cards(repo)}
    projection = task_store.TASK_SUMMARY_PROJECTION
    bulk = task_store.get_tasks(repo, list(scan), projection=projection)
    assert bulk == scan
    assert task_store.get_task(repo, "T-review", projection=projection) == scan["T-review"]
    assert task_store.get_task(repo, "T-missing", projection=projection) is None


def test_projection_reads_fail_closed_on_unknown_or_combined_arguments(repo) -> None:
    with pytest.raises(ValueError, match="unknown task card projection"):
        task_store.list_task_cards(repo, projection="everything")
    with pytest.raises(ValueError, match="decode_errors"):
        task_store.list_task_cards(
            repo, projection=task_store.TASK_SUMMARY_PROJECTION, decode_errors=[]
        )


def test_summary_projection_keeps_status_and_topic_filters(repo) -> None:
    projection = task_store.TASK_SUMMARY_PROJECTION
    for status in ("pending", "processing", "review", "blocked", "finished", "archived"):
        full = task_store.list_task_cards(repo, status=status)
        narrow = task_store.list_task_cards(repo, status=status, projection=projection)
        assert [c["task_id"] for c in narrow] == [c["task_id"] for c in full]
        assert {c["status"] for c in narrow} <= {status}
    narrow = task_store.list_task_cards(repo, topic="fallback-topic", projection=projection)
    assert [c["task_id"] for c in narrow] == ["T-topic-fallback"]


# -- golden tool replies -----------------------------------------------------


def test_task_plan_snapshot_reply_is_identical_and_decodes_no_card(repo, monkeypatch) -> None:
    from aiworkhub import server

    monkeypatch.setattr(core, "_summary_cards_for_plan", core._full_cards_for_plan)
    before = (server.aiworkhub_task_plan_snapshot(), server.aiworkhub_task_plan_snapshot(full=True))
    monkeypatch.undo()
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)

    spy = _DecodeSpy(monkeypatch)
    after = (server.aiworkhub_task_plan_snapshot(), server.aiworkhub_task_plan_snapshot(full=True))

    assert _canon(after) == _canon(before)
    assert before[1]["dependencies"]["T-child"] == ["T-root"]
    assert before[1]["dependency_replacements"]["T-on-old"]["T-old"]["resolved_to"] == "T-new"
    assert "QR-1" not in before[1]["task_ids"]
    assert spy.task_ids == []


def test_task_health_and_task_list_replies_never_decode_cards(repo, monkeypatch) -> None:
    from aiworkhub import server

    # This card does not change task_health/task_list, so there is no
    # full-card reply to compare against; what is pinned is that neither
    # reply decodes a whole card.
    spy = _DecodeSpy(monkeypatch)
    server.aiworkhub_task_health()
    listing = server.aiworkhub_task_list(status="all", limit=80)
    server.aiworkhub_task_list(status="pending", topic="coding")
    assert "T-root" in listing["stdout"]
    assert spy.task_ids == []


def _catalog_inputs() -> dict[str, Any]:
    processes = [
        {"request_id": f"r-{tid}", "task_id": tid, "adapter_id": "glm_vscode_lm",
         "model": "glm-5.2", "started_at": "2026-11-05T10:00:00+00:00",
         "finished_at": "2026-11-05T10:01:00+00:00", "total_tokens": 100}
        for tid in ("T-review", "T-fail", "T-null-evidence", "T-root", "T-weird")
    ] + [{"request_id": "r-orphan", "task_id": "T-fail", "adapter_id": "", "model": ""}]
    preflight = {"providers": [
        {"adapter_id": "glm_vscode_lm", "launchable": True, "status": "ready"},
    ]}
    return {"process_rows": processes, "preflight": preflight, "now_epoch": 1_793_000_000.0}


def test_workforce_catalog_and_rank_are_identical_and_decode_no_card(repo, monkeypatch) -> None:
    inputs = _catalog_inputs()
    monkeypatch.setattr(workforce_catalog, "_canonical_cards", _full_cards)
    before = workforce_catalog.build_catalog(repo, **inputs)
    monkeypatch.undo()

    spy = _DecodeSpy(monkeypatch)
    after = workforce_catalog.build_catalog(repo, **inputs)
    assert _canon(after) == _canon(before)
    glm = next(w for w in before["workers"] if w["model"] == "glm-5.2")
    assert glm["outcomes"]["attempted_task_count"] >= 3

    task = workforce_router.TaskRequirements.build(
        task_id="T-rank", repo_id="repo", kinds=["code"], risk="high",
        tool_needs=["filesystem"],
    )
    assert _canon(workforce_catalog.rank_task(repo, task, catalog=after)) == _canon(
        workforce_catalog.rank_task(repo, task, catalog=before)
    )
    assert spy.task_ids == []


def _dashboard_panels(provider: dashboard.DashboardProvider) -> dict[str, Any]:
    with provider.snapshot_read_scope():
        return {
            "plan": provider.get_task_plan(),
            "needfix": provider.get_needfix_snapshot(),
            "roadmap": provider.get_roadmap_snapshot(),
            "workforce": provider.get_workforce_catalog(),
        }


def test_dashboard_snapshot_panels_are_identical_and_decode_no_card(repo, monkeypatch) -> None:
    inputs = _catalog_inputs()
    monkeypatch.setattr(dashboard, "read_process_runs",
                        lambda **_k: {"processes": inputs["process_rows"]})
    monkeypatch.setattr(dashboard.cost_ledger, "build_cost_ledger",
                        lambda **_k: {"tasks": [], "cost_per_accepted_outcome": {}})
    monkeypatch.setattr(dashboard.repo_policy, "build_preflight", lambda _r: inputs["preflight"])
    real_build_catalog = workforce_catalog.build_catalog
    monkeypatch.setattr(
        dashboard.workforce_catalog, "build_catalog",
        lambda root, **kw: real_build_catalog(root, **{**kw, "now_epoch": inputs["now_epoch"]}),
    )

    full_provider = dashboard.DashboardProvider(repo_root=repo)
    monkeypatch.setattr(
        full_provider, "_task_cards",
        lambda: tuple(task_store.list_task_cards(repo, limit=dashboard.TASK_CARD_SNAPSHOT_LIMIT)),
    )
    before = _dashboard_panels(full_provider)

    spy = _DecodeSpy(monkeypatch)
    after = _dashboard_panels(dashboard.DashboardProvider(repo_root=repo))

    assert _canon(after) == _canon(before)
    assert before["plan"]["ready"]
    assert spy.task_ids == []


# -- measured memory ---------------------------------------------------------


def test_summary_read_python_peak_is_a_fraction_of_the_full_read(tmp_path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    task_store.initialize_repository(root)
    history = [{"event": "evidence", "body": "e" * 4096} for _ in range(48)]  # ~200 KB/card
    for index in range(60):
        _insert(
            root, f"T-big-{index:03d}", allowed_writes=[f"src/m{index}.py"],
            terminal_review={"substatus": "review_ready",
                             "evidence": {"model": "glm-5.2", "validation": history}},
            history=history, updated_at=f"2026-11-06T00:{index:02d}:00+00:00",
        )

    def peak(read) -> int:
        tracemalloc.start()
        try:
            cards = read()
            assert len(cards) == 60
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    full_peak = peak(lambda: _full_cards(root))
    summary_peak = peak(lambda: _summary_cards(root))
    # Measured 2026-11-06 on this fixture's rows: full read 49.2 MB Python peak,
    # summary read 0.13 MB (in-memory store, Python 3.12, SQLite 3.45.3).
    assert full_peak > 10 * 1024 * 1024
    assert summary_peak * 20 < full_peak, (full_peak, summary_peak)
