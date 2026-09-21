from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from aiworkhub import (
    core,
    dashboard_mcp_app,
    needfix_store,
    roadmap_store,
    server,
    task_store,
)


def _add(repo: Path, title: str = "Outcome", **kwargs):
    return roadmap_store.add_item(
        repo,
        title=title,
        outcome=f"Deliver {title}",
        acceptance=[f"{title} is verified"],
        evidence_refs=["docs/PRODUCT_ROADMAP.md"],
        **kwargs,
    )


def test_store_is_repo_local_and_records_bounded_events(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    initialized = roadmap_store.initialize_repository(repo)
    item = _add(repo, needfix_ids=["NF-2026-00001"])

    assert initialized["db_path"] == str(
        repo / ".aiworkhub" / "tasking" / "roadmap.sqlite"
    )
    assert item["id"] == "RM-2026-00001"
    assert item["status"] == "proposed"
    assert roadmap_store.list_items(repo) == [item]
    assert roadmap_store.list_events(repo, item["id"])[0]["event"] == "created"


def test_create_rolls_back_item_when_audit_event_cannot_be_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"

    def fail_event(*_args, **_kwargs) -> None:
        raise RuntimeError("event write failed")

    monkeypatch.setattr(roadmap_store, "_event", fail_event)
    with pytest.raises(RuntimeError, match="event write failed"):
        _add(repo)

    assert roadmap_store.list_items(repo) == []


def test_dependency_gate_blocks_promotion_until_predecessor_completed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    parent = _add(repo, "Parent")
    child = _add(repo, "Child", depends_on=[parent["id"]])

    with pytest.raises(roadmap_store.RoadmapConflictError, match="incomplete"):
        roadmap_store.transition_item(
            repo, child["id"], "approved", reason="manager approved"
        )

    roadmap_store.transition_item(
        repo, parent["id"], "approved", reason="manager approved"
    )
    roadmap_store.transition_item(
        repo, parent["id"], "in_progress", reason="execution started"
    )
    roadmap_store.transition_item(
        repo, parent["id"], "completed", reason="evidence verified"
    )
    approved = roadmap_store.transition_item(
        repo, child["id"], "approved", reason="dependency complete"
    )
    assert approved["status"] == "approved"


def test_missing_dependency_and_malformed_id_fail_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    with pytest.raises(roadmap_store.RoadmapNotFoundError):
        _add(repo, depends_on=["RM-2026-00099"])
    with pytest.raises(roadmap_store.RoadmapValidationError):
        _add(repo, needfix_ids=["not-a-needfix"])
    with pytest.raises(roadmap_store.RoadmapValidationError):
        roadmap_store.get_item(repo, "../../escape")


def test_link_task_is_idempotent_and_audited(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    item = _add(repo)
    first = roadmap_store.link_task(repo, item["id"], "task-one")
    second = roadmap_store.link_task(repo, item["id"], "task-one")

    assert first["task_ids"] == ["task-one"]
    assert second["task_ids"] == ["task-one"]
    assert [event["event"] for event in roadmap_store.list_events(repo, item["id"])].count(
        "task_linked"
    ) == 1


def test_core_requires_manager_accepted_needfix_before_roadmap_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    captured = needfix_store.add_needfix(
        repo,
        title="Captured",
        description="Not yet accepted",
        status="captured",
    )

    with pytest.raises(roadmap_store.RoadmapConflictError, match="accepted NeedFix"):
        core.roadmap_add(
            "Roadmap outcome", "Deliver it", needfix_ids=[captured["id"]]
        )

    needfix_store.triage_needfix(repo, captured["id"])
    needfix_store.accept_needfix(repo, captured["id"])
    item = core.roadmap_add(
        "Roadmap outcome",
        "Deliver it",
        acceptance=["Verified"],
        needfix_ids=[captured["id"]],
    )
    assert item["needfix_ids"] == [captured["id"]]
    assert item["provenance"]["verified"] is True


def test_core_completion_uses_canonical_task_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    item = _add(repo)
    item = roadmap_store.link_task(repo, item["id"], "task-one")
    roadmap_store.transition_item(repo, item["id"], "approved", reason="approved")
    roadmap_store.transition_item(
        repo, item["id"], "in_progress", reason="started"
    )
    monkeypatch.setattr(core.task_store, "get_task", lambda *_args: {"status": "processing"})
    monkeypatch.setattr(
        core.task_store, "canonical_status", lambda card: str(card["status"])
    )
    with pytest.raises(roadmap_store.RoadmapConflictError, match="unfinished"):
        core.roadmap_transition(item["id"], "completed", reason="done")

    monkeypatch.setattr(core.task_store, "get_task", lambda *_args: {"status": "finished"})
    completed = core.roadmap_transition(item["id"], "completed", reason="done")
    assert completed["status"] == "completed"


def test_snapshot_joins_tasks_and_dependency_blockers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    parent = _add(repo, "Parent")
    child = _add(repo, "Child", depends_on=[parent["id"]])
    roadmap_store.link_task(repo, child["id"], "task-child")
    monkeypatch.setattr(core.task_store, "get_task", lambda *_args: {"status": "pending"})
    monkeypatch.setattr(
        core.task_store, "canonical_status", lambda card: str(card["status"])
    )

    snapshot = core.roadmap_snapshot()
    child_row = next(row for row in snapshot["items"] if row["id"] == child["id"])
    assert snapshot["active"] == 2
    assert child_row["dependency_blockers"] == [parent["id"]]
    assert child_row["tasks"] == [{"task_id": "task-child", "status": "pending"}]


def test_snapshot_reuses_complete_caller_task_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    item = _add(repo, "Outcome")
    roadmap_store.link_task(repo, item["id"], "task-child")

    def unexpected_get(*_args, **_kwargs):
        raise AssertionError("complete caller snapshot must avoid point lookup")

    monkeypatch.setattr(core.task_store, "get_task", unexpected_get)
    snapshot = core.roadmap_snapshot(
        task_cards_snapshot=({"task_id": "task-child", "status": "pending"},),
        task_cards_snapshot_complete=True,
    )

    assert snapshot["items"][0]["tasks"] == [
        {"task_id": "task-child", "status": "pending"}
    ]


def test_snapshot_partial_caller_cards_keep_point_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    item = _add(repo, "Outcome")
    roadmap_store.link_task(repo, item["id"], "task-child")
    calls = {"get": 0}

    def counted_get(*_args, **_kwargs):
        calls["get"] += 1
        return {"status": "finished"}

    monkeypatch.setattr(core.task_store, "get_task", counted_get)
    snapshot = core.roadmap_snapshot(
        task_cards_snapshot=(),
        task_cards_snapshot_complete=False,
    )

    assert snapshot["items"][0]["tasks"] == [
        {"task_id": "task-child", "status": "finished"}
    ]
    assert calls == {"get": 1}


def test_snapshot_aggregates_are_not_limited_to_visible_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    _add(repo, "First")
    _add(repo, "Second")

    snapshot = core.roadmap_snapshot(limit=1)

    assert len(snapshot["items"]) == 1
    assert snapshot["total"] == 2
    assert snapshot["active"] == 2
    assert snapshot["status_counts"]["proposed"] == 2
    assert snapshot["truncated"] is True


def test_dashboard_roadmap_views_are_bounded_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    item = _add(repo)

    listing = dashboard_mcp_app.roadmap_list_view(limit=10)
    detail = dashboard_mcp_app.roadmap_detail_view(item["id"])

    assert listing["ok"] is True
    assert listing["entries"][0]["id"] == item["id"]
    # Deliberate contract change (mcp-output-9): one authority string replaces
    # the constant seven-flag block on every NeedFix/Roadmap dashboard reply.
    assert listing["authority"] == "readonly"
    assert detail["ok"] is True
    assert detail["item"]["outcome"] == item["outcome"]


def test_public_mcp_surface_exposes_roadmap_contract() -> None:
    for name in (
        "roadmap_list",
        "roadmap_show",
        "roadmap_events",
        "roadmap_snapshot",
        "roadmap_add",
        "roadmap_transition",
        "roadmap_link_task",
    ):
        assert hasattr(server, name)
    assert "needfix_ids" in inspect.signature(server.roadmap_add).parameters
    assert set(dashboard_mcp_app.ROADMAP_READ_TOOLS) == {
        "aiworkhub_dashboard_roadmap_list",
        "aiworkhub_dashboard_roadmap_detail",
    }


# --- Exact wave-goal successor binding -------------------------------------


def _wave(
    repo: Path,
    goals: list[dict],
    *,
    history: tuple[str, ...] = ("V1",),
    active: bool = True,
) -> dict:
    wave = _add(repo, "Wave", milestone="0.11.51", provenance={"wave_goals": goals})
    for task_id in history:
        roadmap_store.link_task(repo, wave["id"], task_id)
    roadmap_store.transition_item(repo, wave["id"], "approved", reason="planned")
    if active:
        roadmap_store.transition_item(repo, wave["id"], "in_progress", reason="started")
    return roadmap_store.get_item(repo, wave["id"])


def _binding(roadmap_id: str, goal_id: str = "lsp", predecessor: str = "V1") -> dict:
    return {
        "roadmap_id": roadmap_id,
        "goal_id": goal_id,
        "predecessor_task_id": predecessor,
    }


def _use_task_cards(
    monkeypatch: pytest.MonkeyPatch, cards_by_root: dict[Path, dict[str, dict]]
) -> None:
    """Stand in for each repository's OWN canonical task store."""
    monkeypatch.setattr(
        task_store,
        "get_task",
        lambda root, task_id: cards_by_root.get(Path(root), {}).get(task_id),
    )


def _bind(repo: Path, successor: str, binding: dict) -> dict:
    return roadmap_store.bind_goal_successor(
        repo, successor_task_id=successor, **binding
    )


def _roadmap_state(repo: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    items = roadmap_store.list_items(repo, include_archived=True)
    return items, {
        item["id"]: roadmap_store.list_events(repo, item["id"]) for item in items
    }


def _successor_events(repo: Path, roadmap_id: str) -> list[dict]:
    return [
        event["detail"]
        for event in roadmap_store.list_events(repo, roadmap_id)
        if event["event"] == roadmap_store.WAVE_GOAL_SUCCESSOR_EVENT
    ]


def test_exact_successor_replaces_one_goal_pointer_and_keeps_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    wave = _wave(
        repo,
        [
            {"id": "lsp", "label": "LSP index", "task_ids": ["V1", "LSP_DOCS"]},
            {"id": "playbook", "label": "Playbook", "task_ids": ["V1"]},
        ],
        history=("V1", "LSP_DOCS"),
    )
    binding = _binding(wave["id"])
    _use_task_cards(monkeypatch, {repo: {
        "V1": {"status": "archived"},
        "LSP_DOCS": {"status": "finished"},
        "V2": {"status": "pending", "wave_goal_binding": dict(binding)},
    }})

    applied = _bind(repo, "V2", binding)
    repeated = _bind(repo, "V2", binding)

    assert applied["state"] == "applied", applied
    goals = {
        goal["id"]: goal["task_ids"]
        for goal in applied["wave"]["provenance"]["wave_goals"]
    }
    # Only the named goal's single predecessor occurrence moves: that goal's
    # other prerequisite and the other goal sharing V1 are untouched.
    assert goals == {"lsp": ["V2", "LSP_DOCS"], "playbook": ["V1"]}
    # The predecessor stays in the outcome-wide history; the target does not move.
    assert applied["wave"]["task_ids"] == ["V1", "LSP_DOCS", "V2"]
    assert applied["wave"]["milestone"] == "0.11.51"
    assert applied["wave"]["status"] == "in_progress"
    assert repeated["state"] == "already_applied"
    assert repeated["wave"] == applied["wave"]
    assert _successor_events(repo, wave["id"]) == [
        {"goal_id": "lsp", "from": "V1", "to": "V2"}
    ]


def test_foreign_missing_ambiguous_or_undeclared_claims_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    active = _wave(repo, [
        {"id": "lsp", "label": "LSP", "task_ids": ["V1"]},
        {"id": "docs", "label": "Docs", "task_ids": ["DOCS_V1"]},
        {"id": "twice", "label": "Twice", "task_ids": ["V1", "V1"]},
        {"id": "dup", "label": "Dup A", "task_ids": ["V1"]},
        {"id": "dup", "label": "Dup B", "task_ids": ["V1"]},
    ])
    planned = _wave(repo, [{"id": "lsp", "label": "LSP", "task_ids": ["V1"]}], active=False)
    cards: dict[Path, dict[str, dict]] = {
        repo: {"V1": {}, "DOCS_V1": {}},
        other: {"V1": {}, "OTHER_V1": {}},
    }
    _use_task_cards(monkeypatch, cards)

    def refused(successor: str, binding: dict, *, root: Path = repo) -> str:
        cards[root][successor] = {"wave_goal_binding": dict(binding)}
        result = roadmap_store.bind_goal_successor(
            root, successor_task_id=successor, **binding
        )
        assert result["state"] == "refused", result
        return result["reason"]

    before = _roadmap_state(repo)
    reasons = {
        "foreign_predecessor": refused("S1", _binding(active["id"], predecessor="OTHER_V1")),
        "missing_predecessor": refused("S2", _binding(active["id"], predecessor="GHOST_V1")),
        "foreign_wave": refused("S3", _binding(active["id"]), root=other),
        "missing_goal": refused("S4", _binding(active["id"], "delta")),
        "wrong_goal": refused("S5", _binding(active["id"], "docs")),
        "duplicate_predecessor": refused("S6", _binding(active["id"], "twice")),
        "duplicate_goal": refused("S7", _binding(active["id"], "dup")),
        "inactive_wave": refused("S8", _binding(planned["id"])),
        "self_succession": refused("V1", _binding(active["id"])),
        "malformed": refused("S9", {**_binding(active["id"]), "roadmap_id": "../escape"}),
    }
    # A successor by name alone, and one that declared a different goal, are
    # never bound: only the card's own exact declaration counts.
    cards[repo]["V2"] = {"title": "LSP index integration V2"}
    by_name = _bind(repo, "V2", _binding(active["id"]))
    cards[repo]["V2_DOCS"] = {"wave_goal_binding": _binding(active["id"], "docs")}
    other_goal = _bind(repo, "V2_DOCS", _binding(active["id"]))

    assert reasons == {
        "foreign_predecessor": "predecessor_not_in_repository",
        "missing_predecessor": "predecessor_not_in_repository",
        "foreign_wave": "roadmap_not_found",
        "missing_goal": "goal_missing",
        "wrong_goal": "predecessor_not_current",
        "duplicate_predecessor": "predecessor_ambiguous",
        "duplicate_goal": "goal_ambiguous",
        "inactive_wave": "wave_not_active:approved",
        "self_succession": "self_succession",
        "malformed": "malformed_binding",
    }
    assert (by_name["state"], by_name["reason"]) == (
        "refused", "successor_binding_mismatch"
    )
    assert (other_goal["state"], other_goal["reason"]) == (
        "refused", "successor_binding_mismatch"
    )
    assert _roadmap_state(repo) == before
    assert not (other / ".aiworkhub").exists()


def test_concurrent_successor_claims_bind_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    wave = _wave(repo, [{"id": "lsp", "label": "LSP", "task_ids": ["V1"]}])
    binding = _binding(wave["id"])
    _use_task_cards(monkeypatch, {repo: {
        "V1": {},
        "V2_A": {"wave_goal_binding": dict(binding)},
        "V2_B": {"wave_goal_binding": dict(binding)},
    }})
    claims = ["V2_A", "V2_B"] * 3
    barrier = threading.Barrier(len(claims))
    results: list[dict] = []

    def claim(successor: str) -> None:
        barrier.wait(timeout=30)
        results.append(_bind(repo, successor, binding))

    threads = [threading.Thread(target=claim, args=(name,)) for name in claims]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == len(claims)
    applied = [result for result in results if result["state"] == "applied"]
    assert len(applied) == 1
    winner = applied[0]["successor_task_id"]
    loser = "V2_B" if winner == "V2_A" else "V2_A"
    assert sorted(
        (result["successor_task_id"], result["state"], result["reason"])
        for result in results
        if result is not applied[0]
    ) == sorted(
        [(winner, "already_applied", "")] * 2
        + [(loser, "refused", "predecessor_not_current")] * 3
    )
    final = roadmap_store.get_item(repo, wave["id"])
    assert final["provenance"]["wave_goals"][0]["task_ids"] == [winner]
    assert _successor_events(repo, wave["id"]) == [
        {"goal_id": "lsp", "from": "V1", "to": winner}
    ]


def test_goal_successor_preflight_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    wave = _wave(repo, [{"id": "lsp", "label": "LSP", "task_ids": ["V1"]}])
    _use_task_cards(monkeypatch, {repo: {"V1": {}}, fresh: {"V1": {}}})
    before = _roadmap_state(repo)

    ready = roadmap_store.goal_successor_preflight(
        repo, successor_task_id="V2", **_binding(wave["id"])
    )
    missing = roadmap_store.goal_successor_preflight(
        fresh, successor_task_id="V2", **_binding(wave["id"])
    )

    assert ready == {"state": "ready", "reason": ""}
    assert missing == {"state": "refused", "reason": "roadmap_not_found"}
    assert not (fresh / ".aiworkhub").exists()
    assert _roadmap_state(repo) == before


# --- Evidence-gated automatic wave completion ------------------------------


_RECEIPT_ID = "sha256:" + "a" * 64


def _accepted(task_id: str) -> dict:
    request_id = f"req-{task_id}"
    # The identity fields ``task_engine.accept_review`` seals into its receipt.
    receipt = {
        "schema_id": "aiworkhub.accepted_outcome_receipt.v1",
        "receipt_id": _RECEIPT_ID,
        "task_id": task_id,
        "request_id": request_id,
    }
    return {
        "task_id": task_id,
        "status": "finished",
        "worker_status": "done",
        "accepted_request_id": request_id,
        "accepted_by": "codex",
        "accepted_at": "2026-09-21T00:00:00+00:00",
        "accept_evidence": {"accepted_outcome_receipt": receipt},
    }


def _mapped_goals() -> list[dict]:
    return [
        {"id": "lsp", "label": "LSP", "task_ids": ["LSP_V2"], "acceptance_indices": [1]},
        {"id": "docs", "label": "Docs", "task_ids": ["DOCS_V1"], "acceptance_indices": [2]},
    ]


def _mapped_wave(repo: Path, goals: list[dict] | None = None) -> dict:
    wave = roadmap_store.add_item(
        repo,
        title="Wave",
        outcome="Deliver the wave",
        milestone="0.11.51",
        acceptance=["LSP integrated", "Docs published"],
        provenance={"wave_goals": _mapped_goals() if goals is None else goals},
    )
    for task_id in ("LSP_V1", "LSP_V2", "DOCS_V1"):
        roadmap_store.link_task(repo, wave["id"], task_id)
    roadmap_store.transition_item(repo, wave["id"], "approved", reason="planned")
    roadmap_store.transition_item(repo, wave["id"], "in_progress", reason="started")
    return roadmap_store.get_item(repo, wave["id"])


def _accepted_cards() -> dict[str, dict | None]:
    return {
        # The archived predecessor stays in history; only current tasks decide.
        "LSP_V1": {"task_id": "LSP_V1", "status": "finished", "archived_at": "2026-09-20"},
        "LSP_V2": _accepted("LSP_V2"),
        "DOCS_V1": _accepted("DOCS_V1"),
    }


def _completions(repo: Path, roadmap_id: str) -> list[dict]:
    return [
        event["detail"]
        for event in roadmap_store.list_events(repo, roadmap_id)
        if event["event"] == "transitioned" and event["detail"].get("to") == "completed"
    ]


def test_accepted_mapped_evidence_completes_the_wave_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import wave_roadmap

    repo = tmp_path / "repo"
    wave = _mapped_wave(repo)
    _use_task_cards(monkeypatch, {repo: _accepted_cards()})

    first = roadmap_store.reconcile_wave_completion(repo, wave["id"])
    again = roadmap_store.reconcile_wave_completion(repo, wave["id"])

    assert (first["state"], first["reason"]) == ("completed", "all_mapped_goals_accepted")
    assert first["wave"]["status"] == "completed"
    # Completion never moves the target or rewrites the task history.
    assert first["wave"]["milestone"] == "0.11.51"
    assert first["wave"]["task_ids"] == wave["task_ids"]
    assert (again["state"], again["reason"]) == ("completed", "already_completed")
    [completion] = _completions(repo, wave["id"])
    assert completion["from"] == "in_progress"
    assert completion["evidence"] == {
        "schema_id": roadmap_store.WAVE_COMPLETION_EVIDENCE_SCHEMA,
        "criteria": 2,
        "goals": [
            {
                "id": "lsp",
                "acceptance_indices": [1],
                "accepted": {"LSP_V2": {"request_id": "req-LSP_V2", "receipt_id": _RECEIPT_ID}},
            },
            {
                "id": "docs",
                "acceptance_indices": [2],
                "accepted": {"DOCS_V1": {"request_id": "req-DOCS_V1", "receipt_id": _RECEIPT_ID}},
            },
        ],
    }
    projected = wave_roadmap.project_current_wave(roadmap_store.list_items(repo), "0.11.53")
    assert projected["selection_reason"] == wave_roadmap.REASON_NO_ACTIVE_WAVE


@pytest.mark.parametrize(
    ("cards", "goals", "expected"),
    [
        (
            {"LSP_V2": {**_accepted("LSP_V2"), "archived_at": "2026-09-21"}},
            None,
            ("unknown", "task_evidence_unresolved"),
        ),
        ({"LSP_V2": None}, None, ("unknown", "task_evidence_unresolved")),
        (
            {"LSP_V2": {"task_id": "LSP_V2", "status": "blocked"}},
            None,
            ("unknown", "task_evidence_unresolved"),
        ),
        (
            {"LSP_V2": {"task_id": "LSP_V2", "status": "finished"}},
            None,
            ("unknown", "task_evidence_unresolved"),
        ),
        (
            {"LSP_V2": {"task_id": "LSP_V2", "status": "processing"}},
            None,
            ("pending_evidence", "task_evidence_pending"),
        ),
        # Another task's genuine receipt copied onto this card verifies nothing.
        (
            {"LSP_V2": {**_accepted("LSP_V2"), "accept_evidence": _accepted("DOCS_V1")["accept_evidence"]}},
            None,
            ("unknown", "task_evidence_unresolved"),
        ),
        ({}, [], ("unknown", "goals_missing")),
        ({}, _mapped_goals()[:1], ("unknown", "acceptance_unmapped")),
    ],
    ids=[
        "archived", "missing", "blocked", "unverified", "unfinished", "foreign_receipt",
        "no_goals", "unmapped_criterion",
    ],
)
def test_unresolved_or_unmapped_evidence_leaves_the_wave_in_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cards: dict,
    goals: list[dict] | None,
    expected: tuple[str, str],
) -> None:
    repo = tmp_path / "repo"
    wave = _mapped_wave(repo, goals)
    _use_task_cards(monkeypatch, {repo: {**_accepted_cards(), **cards}})
    before = _roadmap_state(repo)

    result = roadmap_store.reconcile_wave_completion(repo, wave["id"])

    assert (result["state"], result["reason"]) == expected
    assert "wave" not in result
    assert _roadmap_state(repo) == before


def test_a_release_alone_never_closes_or_retargets_a_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No version is an input: the only evidence is the exact current tasks.
    assert list(inspect.signature(roadmap_store.reconcile_wave_completion).parameters) == [
        "repo_root", "wave_id",
    ]
    repo = tmp_path / "repo"
    wave = _mapped_wave(repo)
    _use_task_cards(monkeypatch, {repo: {
        "LSP_V2": {"task_id": "LSP_V2", "status": "pending"},
        "DOCS_V1": {"task_id": "DOCS_V1", "status": "review"},
    }})
    before = _roadmap_state(repo)

    results = [roadmap_store.reconcile_wave_completion(repo, wave["id"]) for _ in range(3)]

    assert {result["state"] for result in results} == {"pending_evidence"}
    assert _roadmap_state(repo) == before


def test_concurrent_reconciles_complete_the_wave_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    wave = _mapped_wave(repo)
    _use_task_cards(monkeypatch, {repo: _accepted_cards()})
    barrier = threading.Barrier(6)
    results: list[dict] = []

    def reconcile() -> None:
        barrier.wait(timeout=30)
        results.append(roadmap_store.reconcile_wave_completion(repo, wave["id"]))

    threads = [threading.Thread(target=reconcile) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == 6
    assert {result["state"] for result in results} == {"completed"}
    assert len(_completions(repo, wave["id"])) == 1


def test_a_goal_rebound_after_the_verdict_refuses_the_stale_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    wave = _mapped_wave(repo)
    stale = roadmap_store.item_revision(wave)
    binding = _binding(wave["id"], "lsp", "LSP_V2")
    _use_task_cards(monkeypatch, {repo: {
        **_accepted_cards(),
        "LSP_V3": {"task_id": "LSP_V3", "status": "pending", "wave_goal_binding": binding},
    }})
    assert _bind(repo, "LSP_V3", binding)["state"] == "applied"

    with pytest.raises(roadmap_store.RoadmapConflictError, match="roadmap_revision_changed"):
        roadmap_store.transition_item(
            repo, wave["id"], "completed", reason="stale verdict", expected_revision=stale
        )
    result = roadmap_store.reconcile_wave_completion(repo, wave["id"])

    assert roadmap_store.get_item(repo, wave["id"])["status"] == "in_progress"
    assert (result["state"], result["task_ids"]) == ("pending_evidence", ["LSP_V3"])
    assert _completions(repo, wave["id"]) == []


def test_unreadable_or_absent_evidence_sources_are_unknown_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    wave = _mapped_wave(repo)

    def unreadable(_root, _task_id):
        raise RuntimeError("task store locked")

    monkeypatch.setattr(task_store, "get_task", unreadable)
    before = _roadmap_state(repo)

    locked = roadmap_store.reconcile_wave_completion(repo, wave["id"])
    ghost = roadmap_store.reconcile_wave_completion(repo, "RM-2026-00099")
    no_store = roadmap_store.reconcile_wave_completion(fresh, "RM-2026-00001")
    malformed = roadmap_store.reconcile_wave_completion(repo, "../escape")

    assert (locked["state"], locked["reason"]) == (
        "unknown", "task_store_unavailable:RuntimeError"
    )
    assert (ghost["state"], ghost["reason"]) == ("unknown", "roadmap_not_found")
    assert (no_store["state"], no_store["reason"]) == ("unknown", "roadmap_not_found")
    assert (malformed["state"], malformed["reason"]) == ("unknown", "malformed_roadmap_id")
    assert _roadmap_state(repo) == before
    assert not (fresh / ".aiworkhub").exists()


def test_active_wave_ids_is_bounded_and_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    first = _mapped_wave(repo)
    plain = _add(repo, "Plain")
    roadmap_store.transition_item(repo, plain["id"], "approved", reason="planned")
    roadmap_store.transition_item(repo, plain["id"], "in_progress", reason="started")
    _add(repo, "Planned wave", provenance={"wave_goals": _mapped_goals()})
    second = _mapped_wave(repo)
    before = _roadmap_state(repo)

    assert roadmap_store.active_wave_ids(repo) == {
        "wave_ids": [first["id"], second["id"]], "truncated": False,
    }
    assert roadmap_store.active_wave_ids(repo, limit=1) == {
        "wave_ids": [first["id"]], "truncated": True,
    }
    assert roadmap_store.active_wave_ids(fresh) == {"wave_ids": [], "truncated": False}
    assert not (fresh / ".aiworkhub").exists()
    assert _roadmap_state(repo) == before


def _canonically_accept(repo: Path, task_id: str) -> dict:
    """Drive one review-ready card through the REAL ``task_engine.accept_review``."""
    from aiworkhub import task_engine

    request_id = f"req-{task_id}"
    promoted = f"{task_id}.txt"
    (repo / promoted).write_bytes(b"accepted\n")
    hashes = {promoted: hashlib.sha256(b"accepted\n").hexdigest()}
    manifest = {"schema_id": "aiworkhub.attempt_artifact_manifest.v1", "entries": []}
    card = {
        "task_id": task_id,
        "runner": "worker",
        "topic": "wave",
        "claim_epoch": 1,
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {"request_id": request_id},
                "changed_paths": [promoted],
                "changed_path_hashes": hashes,
                "attempt_artifact_manifest": manifest,
                "workspace": {"base_oid": "base-oid"},
            },
        },
    }
    now = "2026-09-21T00:00:00+00:00"
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, "
            "started_at, origin_thread_id) "
            "VALUES (?, 'worker', 'wave', 'review', 'review', '', '', ?, ?, ?, 'worker', ?, ?, '')",
            (task_id, json.dumps(card), now, now, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    unsigned = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": 1,
        "base_oid": "base-oid",
        "promoted_paths": [promoted],
        "changed_path_hashes": hashes,
        "attempt_artifact_manifest_id": task_engine._canonical_json_hash(manifest),
        "repository_revision": "sha256:"
        + task_engine._canonical_json_hash({"base_oid": "base-oid", "changed_path_hashes": hashes}),
    }
    receipt = {**unsigned, "receipt_id": "sha256:" + task_engine._canonical_json_hash(unsigned)}
    accepted = task_engine.accept_review(
        repo,
        task_id,
        runner="worker",
        topic="wave",
        request_id=request_id,
        evidence={"promoted_paths": [promoted]},
        accepted_outcome_receipt=receipt,
    )
    assert accepted["ok"] is True, accepted
    return receipt


def test_the_real_accept_transaction_is_the_evidence_that_completes_a_wave(
    tmp_path: Path,
) -> None:
    # No stand-in store: the cards are the bytes the canonical accept wrote.
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    wave = _mapped_wave(repo)
    receipt = _canonically_accept(repo, "LSP_V2")

    # One goal accepted, the other goal's exact task not in the store at all.
    partial = roadmap_store.reconcile_wave_completion(repo, wave["id"])
    assert (partial["state"], partial["reason"]) == ("unknown", "task_evidence_unresolved")
    assert partial["task_ids"] == ["DOCS_V1"]
    assert roadmap_store.get_item(repo, wave["id"])["status"] == "in_progress"

    receipts = {"LSP_V2": receipt, "DOCS_V1": _canonically_accept(repo, "DOCS_V1")}
    completed = roadmap_store.reconcile_wave_completion(repo, wave["id"])

    assert (completed["state"], completed["wave"]["status"]) == ("completed", "completed")
    [completion] = _completions(repo, wave["id"])
    assert [goal["accepted"] for goal in completion["evidence"]["goals"]] == [
        {
            task_id: {"request_id": f"req-{task_id}", "receipt_id": receipts[task_id]["receipt_id"]}
        }
        for task_id in ("LSP_V2", "DOCS_V1")
    ]
