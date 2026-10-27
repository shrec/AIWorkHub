"""NF-2026-01742: a manager's ``confirm_high_risk`` is recorded as what it is.

Before this fix a manager seat passing ``confirm_high_risk=True`` to
``accept_review`` produced an accept record saying
``explicit_human_approval: true`` -- an owner approval that never happened.
The record now carries ``manager_high_risk_confirmation`` (confirmed, the
verified manager identity as actor, and the reviewer receipts the accept
binds), and ``explicit_human_approval`` is true only from an
owner-authenticated path, which no manager call has.

Gate semantics are unchanged by default: the manager confirmation still
clears ``explicit_human_approval_missing``. Only a canonical repository policy
declaring ``explicit_human_approval_required`` in ``.aiworkhub/quality.json``
makes the manager confirmation insufficient on its own.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_B919_PATH = (
    Path(__file__).resolve().parent
    / "test_aiworkhub_accept_review_input_hash_guard_b919.py"
)
_SPEC = importlib.util.spec_from_file_location("_nf01742_b919_accept_fixture", _B919_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_b919 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_b919)

process_launcher = _b919.process_launcher
process_launcher_accept_review = _b919.process_launcher_accept_review
quality_evidence = process_launcher.quality_evidence

# The same sandbox bridge the shared accept fixture relies on.
_bridge_chmod_sandbox_restriction = _b919._bridge_chmod_sandbox_restriction


def _write_policy(root: Path, payload: object) -> None:
    config = root / ".aiworkhub" / "quality.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The repository policy reader and the one shared approval predicate
# ---------------------------------------------------------------------------


def test_repository_policy_defaults_to_manager_confirmation(tmp_path: Path) -> None:
    assert quality_evidence.repository_requires_explicit_human_approval(tmp_path) is False
    _write_policy(tmp_path, {"checks": []})
    assert quality_evidence.repository_requires_explicit_human_approval(tmp_path) is False
    _write_policy(tmp_path, {"checks": [], "explicit_human_approval_required": False})
    assert quality_evidence.repository_requires_explicit_human_approval(tmp_path) is False


def test_repository_policy_declaring_the_field_requires_the_owner(tmp_path: Path) -> None:
    _write_policy(tmp_path, {"checks": [], "explicit_human_approval_required": True})
    assert quality_evidence.repository_requires_explicit_human_approval(tmp_path) is True


@pytest.mark.parametrize(
    "payload",
    [
        {"checks": [], "explicit_human_approval_required": "yes"},
        {"checks": [], "explicit_human_approval_required": 1},
        "{not json",
        [],
    ],
)
def test_repository_policy_fails_closed_on_unreadable_or_non_boolean(
    tmp_path: Path, payload: object
) -> None:
    _write_policy(tmp_path, payload)
    assert quality_evidence.repository_requires_explicit_human_approval(tmp_path) is True


@pytest.mark.parametrize(
    ("owner", "manager", "repo_requires", "expected"),
    [
        (False, False, False, False),
        (False, True, False, True),
        (False, True, True, False),
        (False, False, True, False),
        (True, False, False, True),
        (True, False, True, True),
        (True, True, True, True),
    ],
)
def test_high_risk_approval_predicate(
    owner: bool, manager: bool, repo_requires: bool, expected: bool
) -> None:
    assert quality_evidence.high_risk_approval_satisfied(
        explicit_human_approval=owner,
        manager_high_risk_confirmed=manager,
        repository_requires_explicit_human_approval=repo_requires,
    ) is expected


def _approval_blocked(**kwargs: object) -> bool:
    fold = process_launcher_accept_review.fold_accept_blockers(
        reviewers=[],
        reviewer_request_ids=None,
        risk_profile=quality_evidence.resolve_risk_profile("high"),
        terminal_substatus="review_ready",
        **kwargs,
    )
    return any(
        row["error"] == "explicit_human_approval_missing" for row in fold["blockers"]
    )


def test_fold_manager_confirmation_clears_the_blocker_by_default() -> None:
    assert _approval_blocked() is True
    assert _approval_blocked(confirm_high_risk=True) is False


def test_fold_repository_policy_makes_manager_confirmation_insufficient() -> None:
    assert _approval_blocked(
        confirm_high_risk=True, repository_requires_explicit_human_approval=True
    ) is True
    # Only an owner-authenticated approval clears it then.
    assert _approval_blocked(
        explicit_human_approval=True, repository_requires_explicit_human_approval=True
    ) is False


# ---------------------------------------------------------------------------
# The real accept path
# ---------------------------------------------------------------------------


def _high_risk_scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (
        manager, request_id, task_id, reviewer_request_id, repo,
        promote_calls, accept_review_calls,
    ) = _b919.server_resolved_reviewer_scenario(monkeypatch, tmp_path)
    # The high tier's reviewer census (a second, security lens) is not what
    # this card owns: drop only that refusal so the approval decision is the
    # one under test, and record what the accept path handed the fold.
    real_fold = process_launcher_accept_review.fold_accept_blockers
    fold_calls: list[dict] = []

    def _fold(**kwargs: object) -> dict:
        fold_calls.append(dict(kwargs))
        folded = real_fold(**kwargs)
        return {
            **folded,
            "blockers": [
                row
                for row in folded["blockers"]
                if row["error"] != "required_reviewer_missing:security"
            ],
        }

    monkeypatch.setattr(process_launcher_accept_review, "fold_accept_blockers", _fold)
    gate_calls: list[dict] = []

    def _gate(*_args: object, **kwargs: object) -> dict:
        gate_calls.append(dict(kwargs))
        return {"passed": True, "blocking_checks": [], "checks": []}

    monkeypatch.setattr(quality_evidence, "run_completion_quality_gate", _gate)
    return (
        manager, request_id, task_id, reviewer_request_id, repo,
        promote_calls, accept_review_calls, fold_calls, gate_calls,
    )


def _claims_owner_approval(value: object) -> bool:
    if isinstance(value, dict):
        return value.get("explicit_human_approval") is True or any(
            _claims_owner_approval(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_claims_owner_approval(item) for item in value)
    return False


def test_manager_confirmed_high_risk_accept_records_confirmation_not_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (
        manager, request_id, task_id, reviewer_request_id, _repo,
        promote_calls, accept_review_calls, fold_calls, gate_calls,
    ) = _high_risk_scenario(monkeypatch, tmp_path)

    result = manager.accept_review(
        request_id, task_id, requested_risk_tier="high", confirm_high_risk=True
    )

    # Accepted exactly as before: the manager confirmation clears the gate.
    assert result["ok"] is True, result.get("error")
    assert promote_calls == [["out/result.txt"]]
    assert gate_calls[-1]["human_approval"] is True
    assert fold_calls[-1]["confirm_high_risk"] is True
    assert fold_calls[-1]["explicit_human_approval"] is False
    assert fold_calls[-1]["repository_requires_explicit_human_approval"] is False

    provenance = result["accept_parameter_provenance"]
    assert provenance["explicit_human_approval"] is False
    assert provenance["explicit_human_approval_implied_by_caller"] is False
    assert provenance["explicit_human_approval_required_by_repository_policy"] is False
    confirmation = provenance["manager_high_risk_confirmation"]
    assert confirmation["confirmed"] is True
    assert confirmation["actor"] == result["accept_manager_identity"]
    assert "verified" in confirmation["actor"]
    assert reviewer_request_id in confirmation["evidence_ids"]
    receipt_ids = [
        value
        for value in confirmation["evidence_ids"]
        if value.startswith("quality_review_receipt_sha256:")
    ]
    assert len(receipt_ids) == 1
    assert receipt_ids[0].split(":", 1)[1]
    assert confirmation["satisfied_explicit_human_approval_requirement"] is True

    # The durable accept event says the same, and nothing a manager call wrote
    # claims an owner approval.
    recorded = accept_review_calls[-1]["evidence"]["quality_gate"]
    assert recorded["accept_parameter_provenance"] == provenance
    assert not _claims_owner_approval(accept_review_calls[-1]["evidence"])
    assert not _claims_owner_approval(result)


def test_high_risk_accept_without_confirmation_is_still_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (
        manager, request_id, task_id, _reviewer_request_id, _repo,
        promote_calls, accept_review_calls, _fold_calls, gate_calls,
    ) = _high_risk_scenario(monkeypatch, tmp_path)

    result = manager.accept_review(request_id, task_id, requested_risk_tier="high")

    assert result["ok"] is False
    assert result["error"] == "explicit_human_approval_missing"
    assert promote_calls == []
    assert accept_review_calls == []
    assert gate_calls == []


def test_repository_policy_requiring_human_approval_refuses_manager_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (
        manager, request_id, task_id, _reviewer_request_id, repo,
        promote_calls, accept_review_calls, fold_calls, gate_calls,
    ) = _high_risk_scenario(monkeypatch, tmp_path)
    _write_policy(repo, {"checks": [], "explicit_human_approval_required": True})

    result = manager.accept_review(
        request_id, task_id, requested_risk_tier="high", confirm_high_risk=True
    )

    assert result["ok"] is False
    assert result["error"] == "explicit_human_approval_missing"
    assert fold_calls[-1]["repository_requires_explicit_human_approval"] is True
    assert promote_calls == []
    assert accept_review_calls == []
    assert gate_calls == []


def test_accept_preview_answers_from_the_same_repository_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (
        manager, request_id, task_id, _reviewer_request_id, repo,
        _promote_calls, _accept_review_calls, _fold_calls, _gate_calls,
    ) = _high_risk_scenario(monkeypatch, tmp_path)

    def _approval_errors() -> list[str]:
        preview = process_launcher_accept_review.accept_preview(
            manager,
            request_id,
            task_id,
            requested_risk_tier="high",
            confirm_high_risk=True,
        )
        assert preview["evaluated"] is True, preview
        return [
            row["error"]
            for row in preview["blockers"]
            if row["error"] == "explicit_human_approval_missing"
        ]

    assert _approval_errors() == []
    _write_policy(repo, {"checks": [], "explicit_human_approval_required": True})
    assert _approval_errors() == ["explicit_human_approval_missing"]
