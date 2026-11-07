"""NF-2026-02150: a task_plan alone converts a NeedFix in one MCP call.

Measured over 21 days of manager transcripts: 465 triage/accept transitions and
925 previews for 469 converts. A captured or triaged NeedFix whose own
task_plan names non-empty allowed_writes is promoted by convert with reason
auto_promoted_by_convert, and needfix_convert(task_plan=...) previews then
commits in one call. A refused preview commits nothing, and the plan_digest
path is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import card_contract_advice, core, needfix_store, server, task_store  # noqa: E402

NEEDFIX_ID = "NF-2026-02150"
SCOPE = "src/aiworkhub/example_nf02150.py"
RUNNER = "claude-worker-nf02150"
DIGEST = "c" * 64
PLAN: dict[str, Any] = {
    "runner": RUNNER,
    "topic": "needfix_conversion",
    "allowed_writes": [SCOPE],
    "required_outputs": [SCOPE],
}
# No evidence and no scope_files: only the caller's plan supplies the scope.
ROW: dict[str, Any] = {
    "id": NEEDFIX_ID,
    "title": "Convert captured rows in one call",
    "description": "An explicit plan scope promotes a captured row.",
    "scope_files": [],
    "evidence": [],
    "provenance": {"origin": "manager_add", "verified": False},
    "reopen_generation": 0,
    "kind": "improvement",
    "status": "captured",
}


class _Store:
    """The store calls core makes, over the real normalize/digest policy."""

    NeedFixError = needfix_store.NeedFixError
    NeedFixConflictError = needfix_store.NeedFixConflictError
    NeedFixValidationError = needfix_store.NeedFixValidationError
    normalize_task_plan = staticmethod(needfix_store.normalize_task_plan)
    plan_digest = staticmethod(needfix_store.plan_digest)

    def __init__(self) -> None:
        self.built_cards: list[dict[str, Any]] = []

    def preview_convert(self, repo_root: Any, needfix_id: str, task_plan: Any = None) -> dict[str, Any]:
        card = needfix_store.normalize_task_plan(ROW, task_plan)
        return {
            "needfix_id": needfix_id,
            "already_converted": False,
            "claimable": True,
            "convertible": True,
            "auto_promote": True,
            "task_plan": needfix_store._signed_task_plan(card),
            "plan_digest": needfix_store.plan_digest(card),
            "task_id": card["task_id"],
        }

    def convert_needfix(
        self, repo_root: Any, needfix_id: str, create_task_fn: Any, *, task_card_builder: Any
    ) -> dict[str, Any]:
        card = task_card_builder(dict(ROW))
        self.built_cards.append(card)
        return {"ok": True, "needfix_id": needfix_id, "converted_task_id": card["task_id"]}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Store:
    double = _Store()
    monkeypatch.setattr(core, "_needfix_store_module", lambda: double)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(card_contract_advice, "contract_advice", lambda *_a, **_k: {})
    return double


@pytest.fixture
def promotions(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _promote(repo_root: Any, needfix_id: str, task_plan: Any) -> dict[str, Any]:
        calls.append({"needfix_id": needfix_id, "task_plan": task_plan})
        return {"needfix_id": needfix_id, "status": "accepted", "auto_promoted": True}

    monkeypatch.setattr(needfix_store, "promote_for_scoped_convert", _promote)
    return calls


def test_task_plan_alone_previews_then_commits_in_one_call(store: _Store, promotions: list) -> None:
    reply = server.needfix_convert(NEEDFIX_ID, task_plan=dict(PLAN))

    assert reply["committed"] is True
    assert reply["converted_task_id"] == f"needfix-{NEEDFIX_ID}"
    assert len(reply["plan_digest"]) == 64
    assert isinstance(reply["plan_normalizations"], list)
    assert len(promotions) == 1
    assert promotions[0]["task_plan"]["allowed_writes"] == [SCOPE]
    assert store.built_cards[-1]["allowed_writes"] == [SCOPE]


def test_refused_preview_is_returned_and_nothing_is_committed(
    monkeypatch: pytest.MonkeyPatch, store: _Store, promotions: list
) -> None:
    refusal = (
        f"needfix {NEEDFIX_ID} is 'rejected' -- only status 'accepted' "
        "(or 'task_planned') converts"
    )

    def _refused_preview(needfix_id: str, *, task_plan: Any = None) -> dict[str, Any]:
        return {
            "needfix_id": needfix_id,
            "claimable": False,
            "convertible": False,
            "current_status": "rejected",
            "refusal_reason": refusal,
            "plan_digest": DIGEST,
            "task_plan": dict(PLAN),
        }

    def _no_commit(*_a: Any, **_k: Any) -> None:
        raise AssertionError("convert must not run on a refused preview")

    monkeypatch.setattr(core, "needfix_preview_convert", _refused_preview)
    monkeypatch.setattr(core, "needfix_convert", _no_commit)

    reply = server.needfix_convert(NEEDFIX_ID, task_plan=dict(PLAN))

    assert reply["committed"] is False
    assert reply["refusal_reason"] == refusal
    assert promotions == []
    assert store.built_cards == []


def test_auto_promote_runs_before_the_digest_commit(
    monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    order: list[tuple[str, Any]] = []

    def _preview(needfix_id: str, *, task_plan: Any = None) -> dict[str, Any]:
        return {
            "needfix_id": needfix_id,
            "claimable": True,
            "convertible": True,
            "auto_promote": True,
            "current_status": "captured",
            "plan_digest": DIGEST,
            "task_plan": dict(PLAN),
        }

    def _promote(repo_root: Any, needfix_id: str, task_plan: Any) -> dict[str, Any]:
        order.append(("promote", list(task_plan["allowed_writes"])))
        return {"needfix_id": needfix_id, "status": "accepted", "auto_promoted": True}

    def _commit(needfix_id: str, *, task_plan: Any = None, plan_digest: Any = None) -> dict[str, Any]:
        order.append(("commit", plan_digest))
        return {"ok": True, "needfix_id": needfix_id, "converted_task_id": "needfix-x"}

    monkeypatch.setattr(core, "needfix_preview_convert", _preview)
    monkeypatch.setattr(needfix_store, "promote_for_scoped_convert", _promote)
    monkeypatch.setattr(core, "needfix_convert", _commit)

    reply = server.needfix_convert(NEEDFIX_ID, task_plan=dict(PLAN))

    assert order == [("promote", [SCOPE]), ("commit", DIGEST)]
    assert reply["plan_digest"] == DIGEST
    assert reply["committed"] is True


def test_plan_digest_path_does_not_preview_and_passes_the_digest_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _no_preview(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the plan_digest path must not preview")

    def _commit(needfix_id: str, *, task_plan: Any = None, plan_digest: Any = None) -> dict[str, Any]:
        seen.update(task_plan=task_plan, plan_digest=plan_digest)
        return {"ok": True, "needfix_id": needfix_id, "converted_task_id": "needfix-x"}

    monkeypatch.setattr(core, "needfix_preview_convert", _no_preview)
    monkeypatch.setattr(core, "needfix_convert", _commit)

    server.needfix_convert(NEEDFIX_ID, plan_digest=DIGEST)

    assert seen == {"task_plan": None, "plan_digest": DIGEST}


@pytest.mark.parametrize("status", ["captured", "triaged"])
def test_scoped_promotion_applies_to_captured_and_triaged(status: str) -> None:
    assert needfix_store._scoped_promotable({"status": status}, PLAN) is True


@pytest.mark.parametrize(
    "status",
    ["rejected", "duplicate", "deferred", "archived", "accepted", "task_planned", "converting"],
)
def test_scoped_promotion_never_reopens_other_statuses(status: str) -> None:
    assert needfix_store._scoped_promotable({"status": status}, PLAN) is False


def test_scoped_promotion_needs_the_callers_own_allowed_writes() -> None:
    assert needfix_store._scoped_promotable({"status": "captured"}, None) is False
    assert needfix_store._scoped_promotable({"status": "captured"}, {**PLAN, "allowed_writes": []}) is False


@pytest.mark.parametrize("status", ["captured", "triaged"])
def test_store_preview_reports_auto_promote_for_a_scoped_row(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    monkeypatch.setattr(needfix_store, "get_needfix", lambda *_a: {**ROW, "status": status})
    monkeypatch.setattr(needfix_store, "_card_input_refusal_reason", lambda *_a: None)

    preview = needfix_store.preview_convert("unused", NEEDFIX_ID, PLAN)

    # Not convertible: a digest convert would refuse this row. auto_promote says
    # that one-call convert with this task_plan promotes it first.
    assert preview["auto_promote"] is True
    assert preview["claimable"] is False
    assert preview["convertible"] is False
    assert preview["refusal_reason"]


def test_store_preview_without_plan_scope_stays_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(needfix_store, "get_needfix", lambda *_a: dict(ROW))
    monkeypatch.setattr(needfix_store, "_card_input_refusal_reason", lambda *_a: None)

    preview = needfix_store.preview_convert(
        "unused", NEEDFIX_ID, {"runner": RUNNER, "topic": "needfix_conversion", "required_outputs": [SCOPE]}
    )

    assert preview["auto_promote"] is False
    assert preview["convertible"] is False
    assert preview["unverified"] is True


@pytest.mark.parametrize("status", ["rejected", "duplicate", "deferred", "archived"])
def test_store_preview_keeps_rejected_duplicate_deferred_and_archived_refused(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    monkeypatch.setattr(needfix_store, "get_needfix", lambda *_a: {**ROW, "status": status})
    monkeypatch.setattr(needfix_store, "_card_input_refusal_reason", lambda *_a: None)

    preview = needfix_store.preview_convert("unused", NEEDFIX_ID, PLAN)

    assert preview["auto_promote"] is False
    assert preview["claimable"] is False
    assert preview["convertible"] is False
    assert preview["refusal_reason"]


def test_promotion_without_plan_scope_refuses_before_any_write(tmp_path: Path) -> None:
    with pytest.raises(needfix_store.NeedFixValidationError):
        needfix_store.promote_for_scoped_convert(tmp_path, NEEDFIX_ID, None)
    assert not any(tmp_path.iterdir())


@pytest.fixture
def real_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A real NeedFix and task store in a tmp repo: no store or core doubles.

    AIWORKHUB_ALLOW_WRITES is set the way test_needfix_dashboard_mcp.py sets it.
    The manager identity is stubbed as in test_needfix_dashboard_mcp.py, because
    task_create requires a verified manager seat.
    """
    assert task_store.initialize_repository(tmp_path)["ok"]
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: {
        "provider": "codex",
        "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
        "thread_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
    })
    return tmp_path


def _events(repo: Path, needfix_id: str) -> list[tuple[str, str]]:
    conn = needfix_store._connect(repo)
    try:
        rows = conn.execute(
            "SELECT event, detail_json FROM needfix_events WHERE needfix_id = ? ORDER BY seq",
            (needfix_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(str(event), str(detail or "")) for event, detail in rows]


def test_real_captured_row_converts_in_one_call(real_repo: Path) -> None:
    created = needfix_store.capture_proposal(
        real_repo,
        title="Convert captured rows in one call",
        description="No evidence and no scope_files: the task_plan supplies the scope.",
        kind="improvement",
    )
    needfix_id = str(created["id"])

    # A code task must declare validation (code_task_validation_required). The
    # captured row has no evidence to derive it from, so the caller's plan supplies it.
    plan = {**PLAN, "validation": ["python -m pytest tests/test_needfix_one_call_convert_nf02150.py -q"]}
    reply = server.needfix_convert(needfix_id, task_plan=plan)

    assert reply["committed"] is True
    assert reply["auto_promoted"] is True
    assert needfix_store.get_needfix(real_repo, needfix_id)["status"] == "task_created"
    auto_events = {event for event, detail in _events(real_repo, needfix_id)
                   if "auto_promoted_by_convert" in detail}
    assert {"triaged", "accepted"} <= auto_events


def test_real_preview_of_a_captured_row_auto_promotes_but_writes_nothing(real_repo: Path) -> None:
    created = needfix_store.capture_proposal(
        real_repo,
        title="Preview a captured row",
        description="A preview is read-only.",
        kind="improvement",
    )
    needfix_id = str(created["id"])

    preview = needfix_store.preview_convert(real_repo, needfix_id, dict(PLAN))

    assert preview["auto_promote"] is True
    assert preview["convertible"] is False
    assert needfix_store.get_needfix(real_repo, needfix_id)["status"] == "captured"
    assert not any("auto_promoted_by_convert" in detail for _e, detail in _events(real_repo, needfix_id))


def test_real_verified_manager_add_converts_in_one_call_without_auto_promotion(
    real_repo: Path,
) -> None:
    created = needfix_store.add_needfix(
        real_repo,
        title="Verified manager add converts in one call",
        description="Verified provenance, evidence and scope_files already supply the scope.",
        kind="improvement",
        scope_files=[SCOPE],
        evidence={"proof": "manager-verified"},
    )
    needfix_id = str(created["id"])

    reply = server.needfix_convert(needfix_id, task_plan=dict(PLAN))

    assert reply["committed"] is True
    assert "auto_promoted" not in reply
    assert needfix_store.get_needfix(real_repo, needfix_id)["status"] == "task_created"
    assert not any("auto_promoted_by_convert" in detail for _e, detail in _events(real_repo, needfix_id))
