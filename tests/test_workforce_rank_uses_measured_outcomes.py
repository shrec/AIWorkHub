"""The scorer must rank on the partition the catalog already measured.

``workforce_catalog.build_catalog`` fills ``cost_per_accepted_outcome`` with a
real matched population partitioned by task family and risk, while the per-worker
``outcomes`` block stays on a labelled conservative prior (``sample_count`` 0,
``accepted_rate`` null).  Before this card ``rank_task`` scored every candidate
on the prior and reported ``accepted_rate`` 0.5 identically, so the ranking used
no evidence.  These tests pin that the reported score now reads the measured
acceptance rate for the matched family/risk partition, keeps the labelled prior
where there is no matched population, and never lets that reading change which
worker policy selects, weaken UNKNOWN, or let unknown cost rank as free.
"""

from __future__ import annotations

from pathlib import Path

from aiworkhub import workforce_catalog, workforce_router


def _worker(
    worker_id: str,
    *,
    model: str | None = None,
    provider: str = "anthropic",
    adapter_id: str = "claude_cli",
    outcomes: dict | None = None,
    economics: dict | None = None,
    manager_score_adjustment: float = 0.0,
) -> dict:
    return {
        "worker_id": worker_id,
        "adapter_id": adapter_id,
        "model": model or worker_id,
        "provider": provider,
        "enabled": True,
        "supports": ["code", "research"],
        "tools": ["filesystem"],
        "max_context_tokens": 1000,
        "max_risk": "high",
        "quality_ceiling": 1.0,
        "manager_score_adjustment": manager_score_adjustment,
        "available": True,
        "outcomes": outcomes if outcomes is not None else {"sample_count": 0},
        "cost_per_accepted_outcome": economics or {},
    }


def _partition(
    *,
    matched: int,
    accepted: int,
    acceptance_rate: float,
    state: str = "MEASURED",
    cost_per_accepted_outcome_usd: float | None = 5.0,
    cost_coverage: float = 1.0,
) -> dict:
    return {
        "state": state,
        "matched_decided_tasks": matched,
        "accepted_outcomes": accepted,
        "acceptance_rate": acceptance_rate,
        "cost_coverage": cost_coverage,
        "cost_per_accepted_outcome_usd": cost_per_accepted_outcome_usd,
    }


def _code_task(risk: str = "high") -> workforce_router.TaskRequirements:
    return workforce_router.TaskRequirements.build(
        task_id="T-measured",
        repo_id="repo",
        kinds=["code"],
        risk=risk,
        tool_needs=["filesystem"],
    )


def _candidate(decision: dict, worker_id: str) -> dict:
    return next(
        item for item in decision["candidates"] if item["worker_id"] == worker_id
    )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


def test_ranked_candidate_scores_on_measured_partition_not_prior(tmp_path: Path) -> None:
    """The reproduction: the ranked candidate reports its measured acceptance
    rate for the code/high partition, not the identical 0.5 prior."""
    root = _root(tmp_path)
    workers = [
        _worker(
            "claude-opus-5",
            economics={"code": {"high": _partition(
                matched=31, accepted=21, acceptance_rate=0.677419,
            )}},
        ),
        _worker(
            "claude-sonnet-5",
            economics={"code": {"high": _partition(
                matched=7, accepted=2, acceptance_rate=0.285714,
                cost_per_accepted_outcome_usd=8.0,
            )}},
        ),
    ]

    decision = workforce_catalog.rank_task(root, _code_task(), catalog={"workers": workers})

    # Selection is the same tie-break winner it is today (opus < sonnet).
    assert decision["selected_worker_id"] == "claude-opus-5"

    opus = _candidate(decision, "claude-opus-5")
    assert opus["score_components"]["accepted_rate"] == 0.677419
    assert opus["score_components"]["accepted_rate"] != 0.5
    # NF-2026-00585 regression: the measured sample_count must ride the SAME
    # score_components object as the measured accepted_rate, never a zero from
    # the prior, so a downstream gate on sample_count > 0 keeps the rate.
    assert opus["score_components"]["sample_count"] == 31
    assert opus["score_components"]["sample_count"] != 0
    assert (
        opus["score_components"]["outcome_evidence"]["accepted_rate_source"]
        == "measured_cost_per_accepted_outcome_partition"
    )
    sonnet = _candidate(decision, "claude-sonnet-5")
    assert sonnet["score_components"]["accepted_rate"] == 0.285714
    assert sonnet["score_components"]["sample_count"] == 7


def test_measured_and_prior_evidence_sources_are_never_indistinguishable(
    tmp_path: Path,
) -> None:
    """A partition with a matched population names the measured source and its
    sample count; one without still names the conservative prior."""
    root = _root(tmp_path)
    workers = [
        _worker(
            "claude-opus-5",
            economics={"code": {"high": _partition(
                matched=31, accepted=21, acceptance_rate=0.677419,
            )}},
        ),
        # glm has history only for a different family/risk, so a code/high task
        # finds no matched population for it.
        _worker(
            "glm-5.2",
            model="glm-5.2",
            provider="zhipu",
            adapter_id="glm_vscode_lm",
            economics={"research": {"unknown": _partition(
                matched=13, accepted=12, acceptance_rate=0.923077,
            )}},
        ),
    ]

    decision = workforce_catalog.rank_task(root, _code_task(), catalog={"workers": workers})

    opus = _candidate(decision, "claude-opus-5")["score_components"]
    assert opus["evidence_sources"]["accepted_rate"] == "measured_cost_per_accepted_outcome_partition"
    assert opus["outcome_evidence"]["accepted_rate_source"] == "measured_cost_per_accepted_outcome_partition"
    assert opus["outcome_evidence"]["sample_count"] == 31
    assert opus["outcome_evidence"]["task_family"] == "code"
    assert opus["outcome_evidence"]["risk_tier"] == "high"

    glm = _candidate(decision, "glm-5.2")["score_components"]
    assert glm["evidence_sources"]["accepted_rate"] == "conservative_prior"
    assert glm["outcome_evidence"]["accepted_rate_source"] == "conservative_prior"
    assert glm["outcome_evidence"]["sample_count"] == 0


def test_scoring_is_matched_on_task_family_and_risk_tier(tmp_path: Path) -> None:
    """A code/high rate is never used to rank a research/low task and vice
    versa -- scoring partitions exactly as cost_per_accepted_outcome does."""
    root = _root(tmp_path)
    economics = {
        "code": {"high": _partition(matched=31, accepted=21, acceptance_rate=0.677419)},
        "research": {"low": _partition(matched=10, accepted=9, acceptance_rate=0.9)},
    }
    worker = _worker("claude-opus-5", economics=economics)

    code_high = workforce_catalog.rank_task(root, _code_task("high"), catalog={"workers": [worker]})
    research_low = workforce_catalog.rank_task(
        root,
        workforce_router.TaskRequirements.build(
            task_id="T-research", repo_id="repo", kinds=["research"], risk="low",
            tool_needs=["filesystem"],
        ),
        catalog={"workers": [worker]},
    )

    assert _candidate(code_high, "claude-opus-5")["score_components"]["accepted_rate"] == 0.677419
    assert _candidate(research_low, "claude-opus-5")["score_components"]["accepted_rate"] == 0.9


def test_no_matched_population_ranks_exactly_as_today(tmp_path: Path) -> None:
    """With no matched partition the candidate keeps the labelled conservative
    prior (0.5) unchanged -- byte-identical to the pre-card behaviour."""
    root = _root(tmp_path)
    worker = _worker("claude-opus-5", economics={})

    decision = workforce_catalog.rank_task(root, _code_task(), catalog={"workers": [worker]})

    components = _candidate(decision, "claude-opus-5")["score_components"]
    assert components["accepted_rate"] == 0.5
    assert components["evidence_sources"]["accepted_rate"] == "conservative_prior"
    assert components["outcome_evidence"]["sample_count"] == 0


def test_measured_reading_never_changes_which_worker_is_selected(tmp_path: Path) -> None:
    """A far better measured rate must not move selection: the score reports the
    measured rate while policy still selects the pre-card tie-break winner."""
    root = _root(tmp_path)
    workers = [
        _worker(
            "aaa",
            provider="openai",
            adapter_id="codex_cli",
            economics={"code": {"high": _partition(
                matched=10, accepted=1, acceptance_rate=0.1,
                cost_per_accepted_outcome_usd=None,
            )}},
        ),
        _worker(
            "bbb",
            provider="openai",
            adapter_id="codex_cli",
            economics={"code": {"high": _partition(
                matched=100, accepted=99, acceptance_rate=0.99,
                cost_per_accepted_outcome_usd=None,
            )}},
        ),
    ]

    decision = workforce_catalog.rank_task(root, _code_task(), catalog={"workers": workers})

    # If the measured rate drove selection, bbb (0.99) would win; policy still
    # selects the stable lexical tie-break winner "aaa".
    assert decision["selected_worker_id"] == "aaa"
    assert _candidate(decision, "bbb")["score_components"]["accepted_rate"] == 0.99
    assert _candidate(decision, "aaa")["score_components"]["accepted_rate"] == 0.1
    assert decision["economic_advisory"]["automatic_selection_changed"] is False


def test_unknown_cost_partition_stays_unknown_and_never_ranks_as_free(
    tmp_path: Path,
) -> None:
    """A partition with unknown cost keeps UNKNOWN economic state, is excluded
    from the economic advisory, and never fabricates a free cost -- even while
    its measured acceptance rate is now read into the reported score."""
    root = _root(tmp_path)
    workers = [
        _worker(
            "gpt-5.5",
            provider="openai",
            adapter_id="codex_cli",
            economics={"code": {"high": _partition(
                matched=8, accepted=1, acceptance_rate=0.125,
                state="UNKNOWN", cost_per_accepted_outcome_usd=None,
                cost_coverage=0.0,
            )}},
        ),
        _worker(
            "claude-opus-5",
            economics={"code": {"high": _partition(
                matched=31, accepted=21, acceptance_rate=0.677419,
            )}},
        ),
    ]

    decision = workforce_catalog.rank_task(root, _code_task(), catalog={"workers": workers})

    gpt = _candidate(decision, "gpt-5.5")["score_components"]
    assert gpt["economic_evidence_state"] == "UNKNOWN"
    assert gpt["cost_per_accepted_outcome_usd"] is None
    assert gpt["accepted_rate"] == 0.125  # measured population is still read
    advisory = decision["economic_advisory"]
    assert "gpt-5.5" not in advisory["ranked_worker_ids"]
    assert advisory["recommended_worker_id"] == "claude-opus-5"
    assert advisory["automatic_selection_changed"] is False


def test_built_catalog_partition_flows_into_rank_and_keeps_truth_contract(
    tmp_path: Path,
) -> None:
    """End to end: a catalog built with a measured cost_per_accepted_outcome
    partition ranks on it, and the truth-contract flags stay intact."""
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={"providers": [
            {"adapter_id": "glm_vscode_lm", "launchable": True, "status": "ready", "access_observed": True},
        ]},
        cost_per_accepted_outcome={"routes": {"glm-5.2": {"glm_vscode_lm": {
            "code": {"high": _partition(
                matched=13, accepted=12, acceptance_rate=0.923077, cost_per_accepted_outcome_usd=3.0,
            )},
        }}}},
    )

    truth = snapshot["truth_contract"]
    assert truth["economic_routing_is_advisory_only"] is True
    assert truth["unknown_cost_never_ranks_as_free"] is True
    assert truth["missing_outcomes_use_labeled_prior"] is True
    assert truth["provider_quota_fabricated"] is False

    glm_row = next(row for row in snapshot["workers"] if row["worker_id"] == "glm-5.2")
    assert glm_row["outcomes"]["sample_count"] == 0
    assert glm_row["outcomes"]["evidence_source"] == "conservative_prior"

    decision = workforce_catalog.rank_task(root, _code_task(), catalog=snapshot)
    glm = _candidate(decision, "glm-5.2")["score_components"]
    assert glm["accepted_rate"] == 0.923077
    assert glm["outcome_evidence"]["sample_count"] == 13
    assert glm["evidence_sources"]["accepted_rate"] == "measured_cost_per_accepted_outcome_partition"
