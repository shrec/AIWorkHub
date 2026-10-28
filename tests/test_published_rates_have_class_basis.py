"""Published workforce rates exclude inconclusive outcomes and carry a basis.

RM-2026-00015 criterion 5.  A card whose terminal cause the canonical taxonomy
cannot establish (``FailureCategory.INCONCLUSIVE``) and that reached no outcome
any rate counts used to stay in every rate's denominator, so a route was
penalised for outcomes nobody could attribute.  These tests pin, on one mixed
fixture of candidate, infrastructure and inconclusive cards, the exact rates,
the explicit exclusion count, and that every published rate key carries a
machine-readable basis a reader can re-derive the value from.
"""

from __future__ import annotations

from pathlib import Path

from aiworkhub import core, learning_commit, workforce_catalog, workforce_router


ROUTE = {"adapter_id": "glm_vscode_lm", "model": "glm-5.2"}

# One card per outcome class, each tagged with what the fixture expects.
CARDS = [
    # Candidate outcomes: these stay in every card-rate denominator.
    {"task_id": "accepted", "status": "finished", "terminal_substatus": "review_ready"},
    {"task_id": "review-rejected", "status": "review", "terminal_substatus": "validation_failed"},
    {"task_id": "candidate-failed", "status": "blocked", "terminal_substatus": "validation_failed"},
    # Inconclusive cause, but deterministic verification attributes the failure.
    {
        "task_id": "verification-failed",
        "status": "blocked",
        "deterministic_verification": {"passed": False},
    },
    # Infrastructure: excluded as before.
    {"task_id": "infra", "status": "blocked", "terminal_substatus": "launch_failed"},
    # Inconclusive and unattributed: excluded and counted.
    {"task_id": "inconclusive-empty", "status": "blocked"},
    {"task_id": "inconclusive-unknown", "status": "failed", "terminal_substatus": "unrecognised_terminal_state"},
]
# Two attempts on the accepted card and on one inconclusive card.
PROCESS_TASK_IDS = [
    "accepted", "accepted", "review-rejected", "candidate-failed",
    "verification-failed", "infra",
    "inconclusive-empty", "inconclusive-empty", "inconclusive-unknown",
]


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


def _snapshot(tmp_path: Path) -> dict:
    return workforce_catalog.build_catalog(
        _root(tmp_path),
        cards=CARDS,
        process_rows=[
            {"request_id": f"r{index}", "task_id": task_id, **ROUTE}
            for index, task_id in enumerate(PROCESS_TASK_IDS)
        ],
        preflight={"providers": [
            {"adapter_id": ROUTE["adapter_id"], "launchable": True, "status": "ready"},
        ]},
    )


def _glm(snapshot: dict) -> dict:
    return next(item for item in snapshot["workers"] if item["worker_id"] == "glm-5.2")


def test_fixture_classes_are_what_the_canonical_taxonomy_says() -> None:
    by_id = {card["task_id"]: core.classify_terminal_disposition(card) for card in CARDS}
    inconclusive = learning_commit.FailureCategory.INCONCLUSIVE
    assert by_id["inconclusive-empty"] is inconclusive
    assert by_id["inconclusive-unknown"] is inconclusive
    assert by_id["verification-failed"] is inconclusive
    assert by_id["infra"] in learning_commit.INFRASTRUCTURE_FAILURE_CATEGORIES
    assert by_id["candidate-failed"] in learning_commit.CODE_QUALITY_FAILURE_CATEGORIES


def test_inconclusive_outcomes_leave_the_denominator_and_are_counted(tmp_path: Path) -> None:
    outcomes = _glm(_snapshot(tmp_path))["outcomes"]

    assert outcomes["attempted_task_count"] == 7
    assert outcomes["infrastructure_failure_count"] == 1
    assert outcomes["inconclusive_outcome_count"] == 2
    # 7 attempted - 1 infrastructure - 2 inconclusive.  Before this card the
    # denominator was 6 and accepted_rate read 1/6.
    assert outcomes["sample_count"] == 4
    assert outcomes["accepted_rate"] == 0.25
    assert outcomes["review_ready_rate"] == 0.5
    assert outcomes["validation_failure_rate"] == 0.75
    # Raw counts are unchanged; the retry rate drops the 3 attempts on
    # inconclusive cards: 6 rated attempts over 5 rated cards -> 1 retry.
    assert outcomes["attempt_count"] == 9
    assert outcomes["retry_count"] == 2
    assert outcomes["retry_rate"] == 1 / 6


def test_every_published_rate_carries_its_class_basis(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    for worker in snapshot["workers"]:
        outcomes = worker["outcomes"]
        published = {key for key in outcomes if key.endswith("_rate")}
        assert published == set(workforce_catalog.PUBLISHED_RATE_KEYS)
        assert set(outcomes["rate_basis"]) == published
        for key in published:
            basis = outcomes["rate_basis"][key]
            assert basis["numerator_classes"]
            assert basis["denominator_classes"]
            assert isinstance(basis["excluded_inconclusive_count"], int)
            expected = (
                basis["numerator"] / basis["denominator"] if basis["denominator"] else None
            )
            assert outcomes[key] == expected, (worker["worker_id"], key)


def test_basis_pins_exact_classes_and_counts(tmp_path: Path) -> None:
    basis = _glm(_snapshot(tmp_path))["outcomes"]["rate_basis"]
    denominator_classes = [
        "failure_category:candidate_code",
        "failure_category:policy_or_scope",
        "failure_category:inconclusive:attributed_outcome",
    ]
    excluded_classes = [
        "failure_category:cancellation_or_timeout",
        "failure_category:dependency_or_route",
        "failure_category:provider_runtime",
        "failure_category:validation_environment",
        "failure_category:inconclusive:unattributed_outcome",
    ]
    card_rate = {
        "unit": "task_card",
        "denominator": 4,
        "denominator_classes": denominator_classes,
        "excluded_classes": excluded_classes,
        "excluded_inconclusive_count": 2,
    }
    assert basis["accepted_rate"] == {
        **card_rate, "numerator": 1, "numerator_classes": ["status:finished"],
    }
    assert basis["review_ready_rate"] == {
        **card_rate, "numerator": 2,
        "numerator_classes": ["status:finished", "status:review"],
    }
    assert basis["validation_failure_rate"] == {
        **card_rate, "numerator": 3,
        "numerator_classes": [
            "deterministic_verification:failed",
            "failure_category:candidate_code:not_accepted",
        ],
    }
    assert basis["retry_rate"] == {
        "unit": "process_attempt",
        "numerator": 1,
        "denominator": 6,
        "numerator_classes": ["process_attempt:retry"],
        "denominator_classes": ["process_attempt:any"],
        "excluded_classes": ["process_attempt:inconclusive_unattributed_card"],
        "excluded_inconclusive_count": 3,
    }


def test_ranked_accepted_rate_carries_the_basis_it_was_read_from(tmp_path: Path) -> None:
    glm_outcomes = _glm(_snapshot(tmp_path))["outcomes"]

    def worker(worker_id: str, outcomes: dict, economics: dict) -> dict:
        return {
            "worker_id": worker_id, "adapter_id": "claude_cli", "model": worker_id,
            "provider": "anthropic", "enabled": True, "supports": ["code"],
            "tools": ["filesystem"], "max_context_tokens": 1000, "max_risk": "high",
            "quality_ceiling": 1.0, "manager_score_adjustment": 0.0, "available": True,
            "outcomes": outcomes, "cost_per_accepted_outcome": economics,
        }

    measured = {"code": {"high": {
        "state": "MEASURED", "matched_decided_tasks": 8, "accepted_outcomes": 6,
        "acceptance_rate": 0.75, "cost_coverage": 1.0, "cost_per_accepted_outcome_usd": 5.0,
    }}}
    decision = workforce_catalog.rank_task(
        _root(tmp_path / "rank"),
        workforce_router.TaskRequirements.build(
            task_id="T-basis", repo_id="repo", kinds=["code"], risk="high",
            tool_needs=["filesystem"],
        ),
        catalog={"workers": [
            worker("claude-opus-5", {"sample_count": 0}, measured),
            worker("claude-sonnet-5", glm_outcomes, {}),
        ]},
    )
    evidence = {
        item["worker_id"]: item["score_components"]["outcome_evidence"]
        for item in decision["candidates"]
    }
    prior = evidence["claude-sonnet-5"]
    assert prior["accepted_rate"] == 0.25
    assert prior["accepted_rate_basis"] == glm_outcomes["rate_basis"]["accepted_rate"]
    partition = evidence["claude-opus-5"]["accepted_rate_basis"]
    assert partition["numerator"] == 6
    assert partition["denominator"] == 8
    assert partition["numerator_classes"] == ["cost_ledger:accepted_outcome"]
    assert partition["denominator_classes"] == ["cost_ledger:matched_decided_task"]
    # cost_ledger does not measure inconclusive exclusion: unmeasured, not zero.
    assert partition["excluded_inconclusive_count"] is None
