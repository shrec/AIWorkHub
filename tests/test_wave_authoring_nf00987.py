"""NF-2026-00987: wave authoring -- open, goal upsert/remove, attach, status, rollover.

Each write is one transaction with its one durable event, refuses with a typed
reason, and converges on ``already_applied`` when retried. No authoring tool
can complete a wave: completion stays ``reconcile_wave_completion`` evidence.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from aiworkhub import (
    mcp_tool_metadata,
    needfix_store,
    roadmap_store,
    server,
    task_store,
    wave_roadmap,
)

_RECEIPT_ID = "sha256:" + "a" * 64
_WAVE_TOOLS = {
    "aiworkhub_roadmap_wave_open": server.roadmap_wave_open,
    "aiworkhub_roadmap_wave_goal_upsert": server.roadmap_wave_goal_upsert,
    "aiworkhub_roadmap_wave_goal_remove": server.roadmap_wave_goal_remove,
    "aiworkhub_roadmap_wave_attach": server.roadmap_wave_attach,
    "aiworkhub_roadmap_wave_status": server.roadmap_wave_status,
    "aiworkhub_roadmap_wave_rollover": server.roadmap_wave_rollover,
}
_ACCEPTANCE = ["LSP integrated", "Docs published"]
_GOALS = [
    {"id": "lsp", "label": "LSP", "acceptance_indices": [1]},
    {"id": "docs", "label": "Docs", "acceptance_indices": [2]},
]
_ABSENT_WAVE = "RM-2026-09999"


def _accepted(task_id: str) -> dict:
    request_id = f"req-{task_id}"
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
        "accepted_at": "2026-11-27T00:00:00+00:00",
        "accept_evidence": {"accepted_outcome_receipt": receipt},
    }


def _pending(task_id: str) -> dict:
    return {"task_id": task_id, "status": "pending", "worker_status": "unclaimed"}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    roadmap_store.initialize_repository(root)
    return root


@pytest.fixture
def cards(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict]:
    """Stand in for the repository's own canonical task store."""
    store: dict[str, dict] = {}
    monkeypatch.setattr(task_store, "get_task", lambda _root, task_id: store.get(task_id))
    return store


def _open(repo: Path, **overrides) -> dict:
    args = {
        "milestone": "0.13.18",
        "title": "Wave",
        "goals": _GOALS,
        "acceptance": _ACCEPTANCE,
        **overrides,
    }
    return roadmap_store.open_wave(repo, **args)


def _attach(repo: Path, roadmap_id: str, goal_id: str, **kwargs) -> dict:
    return roadmap_store.attach_goal_task(repo, roadmap_id=roadmap_id, goal_id=goal_id, **kwargs)


def _events(repo: Path, roadmap_id: str, event: str) -> list[dict]:
    rows = reversed(roadmap_store.list_events(repo, roadmap_id, limit=500))
    return [row["detail"] for row in rows if row["event"] == event]


def _state(repo: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    items = roadmap_store.list_items(repo, include_archived=True, limit=500)
    return items, {
        item["id"]: roadmap_store.list_events(repo, item["id"], limit=500) for item in items
    }


def _goals(repo: Path, roadmap_id: str) -> list[dict]:
    return roadmap_store.get_item(repo, roadmap_id)["provenance"]["wave_goals"]


def _revision(repo: Path, roadmap_id: str) -> str:
    return roadmap_store.item_revision(roadmap_store.get_item(repo, roadmap_id))


# --- open ---------------------------------------------------------------------


def test_open_creates_an_in_progress_wave_and_a_retry_converges(repo, cards) -> None:
    opened = _open(repo)
    assert opened["state"] == "applied", opened
    wave = opened["wave"]
    assert (wave["status"], wave["milestone"], wave["acceptance"]) == (
        "in_progress", "0.13.18", _ACCEPTANCE,
    )
    assert wave["provenance"]["wave_goals"] == [
        {"id": "lsp", "label": "LSP", "task_ids": [], "acceptance_indices": [1]},
        {"id": "docs", "label": "Docs", "task_ids": [], "acceptance_indices": [2]},
    ]
    assert opened["item_revision"] == roadmap_store.item_revision(wave)
    assert _events(repo, wave["id"], "created") == [{"status": "in_progress", "origin": "wave_open"}]
    assert _events(repo, wave["id"], roadmap_store.WAVE_OPENED_EVENT) == [
        {"milestone": "0.13.18", "goal_ids": ["lsp", "docs"], "carried_goal_ids": [], "supersedes": None}
    ]

    before = _state(repo)
    retried = _open(repo)
    assert (retried["state"], retried["wave"]["id"]) == ("already_applied", wave["id"])
    assert _state(repo) == before

    # Another wave for the same version, however it is spelled, is refused.
    for milestone in ("0.13.18", "v0.13.18"):
        rival = _open(repo, milestone=milestone, title="Rival")
        assert (rival["state"], rival["reason"]) == ("refused", f"active_wave_exists:{wave['id']}")
    assert _state(repo) == before

    projection = wave_roadmap.project_current_wave([wave], "0.13.17")
    assert (projection["state"], projection["wave_id"]) == (wave_roadmap.READY, wave["id"])


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"milestone": "next"}, "milestone_invalid"),
        ({"title": "  "}, "title_invalid"),
        ({"acceptance": []}, "acceptance_missing"),
        ({"acceptance": ["x" * 1001]}, "acceptance_invalid"),
        ({"goals": []}, "goals_missing"),
        ({"goals": "lsp"}, "goals_malformed"),
        ({"goals": [{"id": "bad id", "label": "Bad"}]}, "goal_id_invalid"),
        ({"goals": [{"id": "g" * 65, "label": "Long"}]}, "goal_id_invalid"),
        ({"goals": [{"id": "a", "label": " "}]}, "goal_label_invalid"),
        ({"goals": [{"id": "a", "label": "A", "task_ids": ["T1"]}]}, "goals_malformed"),
        ({"goals": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]}, "goal_duplicate"),
        (
            {"goals": [{"id": f"g{n}", "label": "G"} for n in range(wave_roadmap.MAX_GOALS + 1)]},
            "goal_limit",
        ),
        (
            {"goals": [{"id": "a", "label": "A", "acceptance_indices": [3]}]},
            "acceptance_indices_invalid:acceptance_index_invalid",
        ),
        ({"supersedes": "RM-1"}, "malformed_supersedes"),
        ({"supersedes": _ABSENT_WAVE}, "predecessor_not_found"),
    ],
)
def test_open_refuses_with_a_typed_reason_and_writes_nothing(
    repo, cards, overrides, reason
) -> None:
    refused = _open(repo, **overrides)
    assert (refused["state"], refused["reason"], refused["wave"]) == ("refused", reason, None)
    assert _state(repo) == ([], {})


def test_supersedes_carries_unaccepted_goals_and_archives_the_predecessor(repo, cards) -> None:
    cards.update({"LSP_V1": _pending("LSP_V1"), "DOCS_V1": _accepted("DOCS_V1")})
    old = _open(repo, milestone="0.11.54", title="Old")["wave"]
    for goal_id, task_id in (("lsp", "LSP_V1"), ("docs", "DOCS_V1")):
        assert _attach(repo, old["id"], goal_id, task_id=task_id)["state"] == "applied"
    old_goals = _goals(repo, old["id"])
    args = {
        "milestone": "0.13.18",
        "title": "New",
        "goals": [{"id": "release", "label": "Release notes"}],
        "supersedes": old["id"],
    }

    opened = _open(repo, **args)

    assert opened["state"] == "applied", opened
    new = opened["wave"]
    # The accepted docs goal stays behind; the open LSP goal moves with its task
    # and keeps its criterion, whose text is the same in the successor.
    assert new["provenance"]["wave_goals"] == [
        {
            "id": "lsp",
            "label": "LSP",
            "task_ids": ["LSP_V1"],
            "carried_from": old["id"],
            "acceptance_indices": [1],
        },
        {"id": "release", "label": "Release notes", "task_ids": []},
    ]
    assert new["task_ids"] == ["LSP_V1"]
    assert opened["carried_goal_ids"] == ["lsp"]
    archived = roadmap_store.get_item(repo, old["id"])
    assert archived["status"] == "archived" and archived["archived_at"]
    assert archived["provenance"]["wave_goals"] == old_goals  # history kept
    assert opened["superseded"]["status"] == "archived"
    assert _events(repo, old["id"], roadmap_store.WAVE_SUPERSEDED_EVENT) == [
        {
            "superseded_by": new["id"],
            "milestone": "0.13.18",
            "carried_goal_ids": ["lsp"],
            "accepted_goal_ids": ["docs"],
        }
    ]
    assert _events(repo, old["id"], "transitioned")[-1]["to"] == "archived"

    before = _state(repo)
    retried = _open(repo, **args)
    assert (retried["state"], retried["wave"]["id"]) == ("already_applied", new["id"])
    assert _state(repo) == before

    again = _open(repo, **{**args, "title": "Newer"})
    assert (again["state"], again["reason"]) == ("refused", "predecessor_not_active:archived")


def test_supersedes_refuses_an_outcome_that_is_not_a_wave(repo, cards) -> None:
    plain = roadmap_store.add_item(repo, title="Plain", outcome="Plain outcome")
    refused = _open(repo, supersedes=plain["id"])
    assert (refused["state"], refused["reason"]) == ("refused", "predecessor_not_active:proposed")
    roadmap_store.transition_item(repo, plain["id"], "approved", reason="planned")
    roadmap_store.transition_item(repo, plain["id"], "in_progress", reason="started")
    refused = _open(repo, supersedes=plain["id"])
    assert (refused["state"], refused["reason"]) == ("refused", "predecessor_not_wave")


# --- attach -------------------------------------------------------------------


def test_attach_binds_a_first_task_once_in_one_event(repo, cards) -> None:
    cards["LSP_V1"] = _pending("LSP_V1")
    wave = _open(repo)["wave"]

    attached = _attach(repo, wave["id"], "lsp", task_id="LSP_V1")

    assert attached["state"] == "applied", attached
    assert attached["wave"]["provenance"]["wave_goals"][0]["task_ids"] == ["LSP_V1"]
    assert attached["wave"]["task_ids"] == ["LSP_V1"]
    assert attached["item_revision"] == _revision(repo, wave["id"])
    assert _events(repo, wave["id"], roadmap_store.WAVE_GOAL_TASK_ATTACHED_EVENT) == [
        {"goal_id": "lsp", "task_id": "LSP_V1"}
    ]
    before = _state(repo)
    retried = _attach(repo, wave["id"], "lsp", task_id="LSP_V1")
    assert retried["state"] == "already_applied"
    assert _state(repo) == before


def test_attach_resolves_a_needfix_to_its_converted_task(repo, cards, monkeypatch) -> None:
    cards["NF_TASK"] = _pending("NF_TASK")
    needfixes = {
        "NF-2026-00987": {"id": "NF-2026-00987", "converted_task_id": "NF_TASK"},
        "NF-2026-00988": {"id": "NF-2026-00988", "converted_task_id": None},
    }

    def get_needfix(_root, needfix_id):
        if needfix_id not in needfixes:
            raise needfix_store.NeedFixNotFoundError(needfix_id)
        return needfixes[needfix_id]

    monkeypatch.setattr(needfix_store, "get_needfix", get_needfix)
    wave = _open(repo)["wave"]

    applied = _attach(repo, wave["id"], "docs", needfix_id="NF-2026-00987")

    assert (applied["state"], applied["task_id"]) == ("applied", "NF_TASK")
    assert _goals(repo, wave["id"])[1]["task_ids"] == ["NF_TASK"]
    assert _events(repo, wave["id"], roadmap_store.WAVE_GOAL_TASK_ATTACHED_EVENT) == [
        {"goal_id": "docs", "task_id": "NF_TASK", "needfix_id": "NF-2026-00987"}
    ]
    before = _state(repo)
    for needfix_id, reason in (
        ("NF-2026-00988", "needfix_not_converted"),
        ("NF-2026-09999", "needfix_not_found"),
        ("NF-26", "malformed_needfix_id"),
    ):
        refused = _attach(repo, wave["id"], "lsp", needfix_id=needfix_id)
        assert (refused["state"], refused["reason"]) == ("refused", reason)
    assert _state(repo) == before


def test_attach_refusals_are_typed_and_write_nothing(repo, cards) -> None:
    limit = wave_roadmap.MAX_GOAL_TASKS
    cards.update({f"T{n}": _pending(f"T{n}") for n in range(limit + 1)})
    wave = _open(repo)["wave"]
    for n in range(limit):
        assert _attach(repo, wave["id"], "lsp", task_id=f"T{n}")["state"] == "applied"
    before = _state(repo)

    cases = {
        f"goal_task_limit:{limit}": _attach(repo, wave["id"], "lsp", task_id=f"T{limit}"),
        "task_already_attached:lsp": _attach(repo, wave["id"], "docs", task_id="T0"),
        "goal_missing": _attach(repo, wave["id"], "nope", task_id=f"T{limit}"),
        "task_not_found": _attach(repo, wave["id"], "docs", task_id="GHOST"),
        "roadmap_not_found": _attach(repo, _ABSENT_WAVE, "docs", task_id=f"T{limit}"),
        "malformed_roadmap_id": _attach(repo, "RM-x", "docs", task_id=f"T{limit}"),
        "goal_id_invalid": _attach(repo, wave["id"], "bad id", task_id=f"T{limit}"),
        "malformed_task_id": _attach(repo, wave["id"], "docs", task_id="bad id"),
        "task_or_needfix_required": _attach(repo, wave["id"], "docs"),
        "task_and_needfix_exclusive": _attach(
            repo, wave["id"], "docs", task_id="T0", needfix_id="NF-2026-00001"
        ),
    }
    for reason, receipt in cases.items():
        assert (receipt["state"], receipt["reason"]) == ("refused", reason), receipt
    assert _state(repo) == before

    roadmap_store.transition_item(repo, wave["id"], "blocked", reason="paused")
    inactive = _attach(repo, wave["id"], "docs", task_id=f"T{limit}")
    assert (inactive["state"], inactive["reason"]) == ("refused", "wave_not_active:blocked")


def test_open_refuses_a_rival_and_converges_while_the_wave_is_blocked(repo, cards) -> None:
    wave = _open(repo)["wave"]
    roadmap_store.transition_item(repo, wave["id"], "blocked", reason="paused")
    before = _state(repo)
    retried = _open(repo)
    assert (retried["state"], retried["wave"]["id"]) == ("already_applied", wave["id"])
    rival = _open(repo, title="Rival")
    assert (rival["state"], rival["reason"]) == ("refused", f"active_wave_exists:{wave['id']}")
    assert _state(repo) == before


# --- goal upsert / remove -----------------------------------------------------


def test_goal_upsert_is_revision_guarded_and_idempotent(repo, cards) -> None:
    opened = _open(repo)
    wave_id, revision = opened["wave"]["id"], opened["item_revision"]

    def upsert(**kwargs):
        return roadmap_store.upsert_goal(repo, roadmap_id=wave_id, **kwargs)

    created = upsert(goal_id="release", label="Release notes", expected_revision=revision)
    assert (created["state"], created["outcome"]) == ("applied", "created")
    assert _goals(repo, wave_id)[-1] == {"id": "release", "label": "Release notes", "task_ids": []}

    stale = upsert(goal_id="lsp", label="Language server", expected_revision=revision)
    assert (stale["state"], stale["reason"]) == ("refused", "stale_revision")
    assert stale["current_revision"] == created["item_revision"]

    # The same edit again converges even from the revision it was decided from.
    again = upsert(goal_id="release", label="Release notes", expected_revision=revision)
    assert again["state"] == "already_applied"

    relabelled = upsert(
        goal_id="lsp",
        label="Language server",
        acceptance_indices=[2, 1],
        expected_revision=created["item_revision"],
    )
    assert (relabelled["state"], relabelled["outcome"]) == ("applied", "updated")
    assert _goals(repo, wave_id)[0] == {
        "id": "lsp", "label": "Language server", "task_ids": [], "acceptance_indices": [1, 2],
    }
    current = relabelled["item_revision"]
    for kwargs, reason in (
        ({"acceptance_indices": [3]}, "acceptance_indices_invalid:acceptance_index_invalid"),
        ({"label": ""}, "goal_label_invalid"),
        ({"goal_id": "bad id"}, "goal_id_invalid"),
        ({"expected_revision": ""}, "expected_revision_required"),
    ):
        call = {"goal_id": "lsp", "label": "LSP", "expected_revision": current, **kwargs}
        refused = upsert(**call)
        assert (refused["state"], refused["reason"]) == ("refused", reason)
    assert [event["outcome"] for event in _events(repo, wave_id, roadmap_store.WAVE_GOAL_UPSERTED_EVENT)] == [
        "created", "updated",
    ]


def test_goal_remove_needs_force_and_a_reason_for_unaccepted_tasks(repo, cards) -> None:
    cards.update({"LSP_V1": _pending("LSP_V1"), "DOCS_V1": _accepted("DOCS_V1")})
    wave_id = _open(repo)["wave"]["id"]
    _attach(repo, wave_id, "lsp", task_id="LSP_V1")
    _attach(repo, wave_id, "docs", task_id="DOCS_V1")
    revision = _revision(repo, wave_id)

    def remove(**kwargs):
        return roadmap_store.remove_goal(repo, roadmap_id=wave_id, **kwargs)

    before = _state(repo)
    refused = remove(goal_id="lsp", expected_revision=revision)
    assert (refused["state"], refused["reason"]) == ("refused", "goal_has_unaccepted_tasks")
    assert refused["unaccepted_task_ids"] == ["LSP_V1"]
    no_reason = remove(goal_id="lsp", expected_revision=revision, force=True, reason=" ")
    assert no_reason["reason"] == "force_reason_required"
    stale = remove(goal_id="lsp", expected_revision="0" * 64, force=True, reason="moved")
    assert (stale["reason"], stale["current_revision"]) == ("stale_revision", revision)
    assert remove(goal_id="nope", expected_revision=revision)["reason"] == "goal_missing"
    assert _state(repo) == before

    forced = remove(goal_id="lsp", expected_revision=revision, force=True, reason="LSP moved to 0.14")
    assert forced["state"] == "applied", forced
    assert [goal["id"] for goal in _goals(repo, wave_id)] == ["docs"]
    assert "LSP_V1" in roadmap_store.get_item(repo, wave_id)["task_ids"]
    assert _events(repo, wave_id, roadmap_store.WAVE_GOAL_REMOVED_EVENT) == [
        {
            "goal_id": "lsp",
            "label": "LSP",
            "task_ids": ["LSP_V1"],
            "unaccepted_task_ids": ["LSP_V1"],
            "forced": True,
            "reason": "LSP moved to 0.14",
        }
    ]
    after = _state(repo)
    retried = remove(goal_id="lsp", expected_revision=revision, force=True, reason="LSP moved to 0.14")
    assert retried["state"] == "already_applied"
    assert _state(repo) == after
    last = remove(goal_id="docs", expected_revision=forced["item_revision"])
    assert (last["state"], last["reason"]) == ("refused", "wave_requires_one_goal")


# --- status -------------------------------------------------------------------


def test_status_is_read_only_and_reports_projection_and_verdict(repo, cards) -> None:
    cards.update({"LSP_V1": _accepted("LSP_V1"), "DOCS_V1": _pending("DOCS_V1")})
    wave_id = _open(repo)["wave"]["id"]
    _attach(repo, wave_id, "lsp", task_id="LSP_V1")
    _attach(repo, wave_id, "docs", task_id="DOCS_V1")
    before = _state(repo)

    status = roadmap_store.wave_status(repo, installed_version="0.13.17")

    assert (status["schema_id"], status["state"]) == (roadmap_store.WAVE_STATUS_SCHEMA, "ok")
    projection = status["current_wave"]
    assert (projection["state"], projection["wave_id"]) == (wave_roadmap.READY, wave_id)
    assert [(goal["id"], goal["state"]) for goal in projection["goals"]] == [
        ("lsp", "checked"), ("docs", "open"),
    ]
    assert (status["completion"]["state"], status["completion"]["task_ids"]) == (
        "pending_evidence", ["DOCS_V1"],
    )
    assert status["wave"]["item_revision"] == _revision(repo, wave_id)
    assert [goal["task_ids"] for goal in status["wave"]["goals"]] == [["LSP_V1"], ["DOCS_V1"]]

    cards["DOCS_V1"] = _accepted("DOCS_V1")
    done = roadmap_store.wave_status(repo, installed_version="0.13.17", wave_id=wave_id)
    assert done["completion"]["state"] == "completed"
    # The verdict alone never writes: only the reconciler completes the wave.
    assert roadmap_store.get_item(repo, wave_id)["status"] == "in_progress"
    assert _state(repo) == before
    assert roadmap_store.reconcile_wave_completion(repo, wave_id)["state"] == "completed"
    assert roadmap_store.get_item(repo, wave_id)["status"] == "completed"

    for wave_id_arg, reason in (("RM-x", "malformed_roadmap_id"), (_ABSENT_WAVE, "roadmap_not_found")):
        refused = roadmap_store.wave_status(repo, installed_version="0.13.17", wave_id=wave_id_arg)
        assert (refused["state"], refused["reason"]) == ("refused", reason)


def test_status_of_a_repository_without_a_roadmap_creates_nothing(tmp_path, cards) -> None:
    status = roadmap_store.wave_status(tmp_path, installed_version="0.13.17")
    assert status["state"] == "ok"
    assert status["current_wave"]["selection_reason"] == wave_roadmap.REASON_NO_ACTIVE_WAVE
    assert (status["wave"], status["completion"]["state"]) == (None, "unknown")
    assert not roadmap_store._db_path(tmp_path).exists()


# --- rollover -----------------------------------------------------------------


def test_rollover_is_wave_open_superseding_the_current_wave(repo, cards, tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    nothing = roadmap_store.rollover_wave(empty, next_milestone="0.13.18")
    assert (nothing["state"], nothing["reason"]) == ("refused", "no_current_wave:no_active_wave")

    cards.update({"LSP_V1": _pending("LSP_V1"), "DOCS_V1": _accepted("DOCS_V1")})
    old = _open(repo, milestone="0.11.54", title="Old")["wave"]
    _attach(repo, old["id"], "lsp", task_id="LSP_V1")
    _attach(repo, old["id"], "docs", task_id="DOCS_V1")
    earlier = roadmap_store.rollover_wave(repo, next_milestone="0.11.53")
    assert (earlier["state"], earlier["reason"]) == ("refused", "milestone_not_after_current")

    rolled = roadmap_store.rollover_wave(repo, next_milestone="0.13.18")

    assert (rolled["state"], rolled["supersedes"]) == ("applied", old["id"]), rolled
    new = rolled["wave"]
    assert (new["title"], new["milestone"], new["acceptance"]) == (
        "Wave 0.13.18", "0.13.18", _ACCEPTANCE,
    )
    assert new["provenance"]["wave_goals"] == [
        {
            "id": "lsp",
            "label": "LSP",
            "task_ids": ["LSP_V1"],
            "carried_from": old["id"],
            "acceptance_indices": [1],
        }
    ]
    assert roadmap_store.get_item(repo, old["id"])["status"] == "archived"

    before = _state(repo)
    again = roadmap_store.rollover_wave(repo, next_milestone="0.13.18")
    assert (again["state"], again["reason"], again["wave"]["id"]) == (
        "already_applied", "current_wave_targets_milestone", new["id"],
    )
    # Exactly wave_open(milestone, supersedes=current): that call names the same wave.
    direct = roadmap_store.open_wave(
        repo,
        milestone="0.13.18",
        title="Wave 0.13.18",
        acceptance=old["acceptance"],
        supersedes=old["id"],
    )
    assert (direct["state"], direct["wave"]["id"]) == ("already_applied", new["id"])
    assert _state(repo) == before


# --- evidence-only completion and the MCP surface -----------------------------


def _string_constants(function) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


@pytest.mark.parametrize(
    "function",
    [
        roadmap_store.open_wave,
        roadmap_store.attach_goal_task,
        roadmap_store.upsert_goal,
        roadmap_store.remove_goal,
        roadmap_store.wave_status,
        roadmap_store.rollover_wave,
        *_WAVE_TOOLS.values(),
    ],
)
def test_no_authoring_path_can_name_the_completed_status(function) -> None:
    assert "completed" not in _string_constants(function)
    assert "transition_item" not in inspect.getsource(function)


def test_wave_tools_are_manager_only_and_document_every_parameter() -> None:
    for name, function in _WAVE_TOOLS.items():
        assert mcp_tool_metadata.MCP_TOOL_PROFILES[name] == {"manager": "callable"}, name
        assert server.mcp_tool_callable(name, "manager") is True
        assert server.mcp_tool_listed(name, "manager") is False
        for profile in ("worker", "dashboard"):
            assert server.mcp_tool_callable(name, profile) is False, (name, profile)
        description, params = mcp_tool_metadata.split_docstring(function.__doc__)
        assert set(params) == set(inspect.signature(function).parameters), name
        assert "aiworkhub_" in description.replace(name, ""), name
        hints = mcp_tool_metadata.derive_annotations(name, description)
        assert hints["readOnlyHint"] is (name == "aiworkhub_roadmap_wave_status"), name
    assert len(mcp_tool_metadata.MCP_TOOL_PROFILES) == len(set(mcp_tool_metadata.MCP_TOOL_PROFILES))


def test_server_tools_reach_the_store_and_fold_outcome_bodies(repo, cards, monkeypatch) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    cards.update({"LSP_V1": _pending("LSP_V1"), "DOCS_V1": _accepted("DOCS_V1")})

    opened = server.roadmap_wave_open(
        milestone="0.13.18", title="Wave", goals=_GOALS, acceptance=_ACCEPTANCE
    )

    assert opened["state"] == "applied", opened
    assert opened["wave"]["outcome"]["summarized"] is True
    wave_id = opened["wave"]["id"]
    # A goal with no task yet is unresolved evidence, never a pass.
    assert server.roadmap_wave_status()["completion"]["reason"] == "goals_malformed"
    for goal_id, task_id in (("lsp", "LSP_V1"), ("docs", "DOCS_V1")):
        attached = server.roadmap_wave_attach(roadmap_id=wave_id, goal_id=goal_id, task_id=task_id)
        assert attached["state"] == "applied", attached
    status = server.roadmap_wave_status()
    assert status["wave"]["id"] == wave_id
    assert status["completion"]["state"] == "pending_evidence"
    assert status["completion"]["task_ids"] == ["LSP_V1"]
