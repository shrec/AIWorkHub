"""The skill system reaching a worker, end to end.

Three things are proved here, in this order:

1. The controlled vocabulary is a *shared* closed set, locked to the upstream
   definitions it was transcribed from. A token added to
   ``quality_evidence._RISK_SIGNAL_FLOORS`` or to the template registry and not
   to ``skill_registry`` is a test failure, not a silent selection miss.
2. ``risk_tier`` is computed at CREATE from the declared write scope, using the
   same two functions accept_review uses, so it is available at injection time.
3. A card carrying real vocabulary produces a non-empty runtime packet AND that
   packet appears in the worker's context bundle. A card that declares nothing
   produces no packet at all -- selection is never made unconditional.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    core,
    project_context,
    quality_evidence,
    server,
    skill_registry,
    skill_registry_store,
    task_store,
    task_templates,
)

_CLAUDE_IDENTITY = {
    "provider": "claude",
    "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
    "window_id": "claude_vscode_4242",
}


@pytest.fixture
def coord(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token = tmp_path / "coordinator.token"
    token.write_text("coord-token\n", encoding="utf-8")
    os.chmod(token, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")
    monkeypatch.setattr(
        core, "_claude_manager_identity", lambda: dict(_CLAUDE_IDENTITY)
    )
    return root


def _create(**overrides):
    kwargs = dict(
        task_id="T_SKILL",
        title="skill vocabulary card",
        runner="claude_coding",
        topic="coding",
        objective="fix the reported defect",
        acceptance=["it works"],
        allowed_writes=["src/aiworkhub/foo.py"],
        required_outputs=["src/aiworkhub/foo.py"],
        validation=["pytest -q tests/test_foo.py"],
        callback_required=False,
        custom_template_escape="audited_custom_unclassified",
    )
    kwargs.update(overrides)
    return core.create_task(**kwargs)


# ---------------------------------------------------------------------------
# 1. The vocabulary is closed and locked to its sources
# ---------------------------------------------------------------------------


def test_risk_signal_triggers_are_transcribed_from_quality_evidence() -> None:
    """Every declarable risk signal exists upstream, and none is missing.

    ``quality_policy_self_weakened`` is deliberately excluded: it is an
    observation about a candidate diff and cannot be true when a card is
    created, so it is not something a card may declare.
    """
    upstream = set(quality_evidence._RISK_SIGNAL_FLOORS)
    assert skill_registry.SKILL_RISK_SIGNAL_TRIGGERS <= upstream
    assert upstream - skill_registry.SKILL_RISK_SIGNAL_TRIGGERS == {
        quality_evidence.QUALITY_POLICY_SELF_WEAKENED_SIGNAL
    }


def test_task_families_cover_every_real_work_kind_and_template_family() -> None:
    """The family vocabulary is the union of the two real sources.

    ``generic`` is absent on purpose: it is the absence of a family. Its
    presence in ``WORK_KINDS`` is what made 86% of live cards match no skill.
    """
    work_kinds = set(quality_evidence.WORK_KINDS)
    assert quality_evidence.WORK_KIND_GENERIC not in skill_registry.SKILL_TASK_FAMILIES
    assert (
        work_kinds - {quality_evidence.WORK_KIND_GENERIC}
        <= skill_registry.SKILL_TASK_FAMILIES
    )
    declared = {
        spec.work_kind for spec in task_templates.TEMPLATE_SPECS.values()
    }
    assert declared <= skill_registry.SKILL_TASK_FAMILIES


def test_stage_vocabulary_is_the_workflow_stage_set_without_unspecified() -> None:
    """The stage token is a workflow stage, never the card status."""
    assert skill_registry.SKILL_STAGES == {
        "orientation",
        "implementation",
        "validation",
        "review",
        "rework",
    }
    assert "unspecified" not in skill_registry.SKILL_STAGES
    for status in ("pending", "processing", "review_ready", "finished", "blocked"):
        assert status not in skill_registry.SKILL_STAGES


def test_unknown_token_is_a_refusal_not_a_silent_miss() -> None:
    for field, bad in (
        ("task_family", "generic"),
        ("stage", "review_ready"),
        ("triggers", "a surface reports unknown"),
        ("applicability", "reporting and observability surfaces"),
    ):
        with pytest.raises(skill_registry.SkillRegistryError) as caught:
            skill_registry.validate_vocabulary_token(bad, field)
        assert caught.value.code == "skill_registry.unknown_vocabulary_token"


def test_wildcard_is_never_a_declarable_token() -> None:
    """``*`` on triggers/applicability is inject-everything, so it is refused."""
    for field in ("task_family", "stage", "triggers", "applicability"):
        with pytest.raises(skill_registry.SkillRegistryError) as caught:
            skill_registry.validate_vocabulary_token(
                skill_registry.SELECT_WILDCARD, field
            )
        assert caught.value.code == "skill_registry.wildcard_not_declarable"


def test_template_families_are_no_longer_flattened_to_generic() -> None:
    """Five of seven templates declared a family work_kind erased."""
    families = {
        name: task_templates.skill_task_family(spec.work_kind)
        for name, spec in task_templates.TEMPLATE_SPECS.items()
    }
    assert families["read_only_analysis"] == "analysis"
    assert families["implementation_with_tests"] == "implementation"
    assert families["test_only"] == "test"
    assert families["docs_change"] == "docs"
    assert families["validation_replay"] == "replay"
    assert families["bugfix_with_regression"] == "bugfix"
    assert all(value for value in families.values())
    # ... while work_kind itself, the behavioral-contract key, is untouched.
    assert task_templates._canonical_work_kind("analysis") == "generic"


def test_expanded_template_card_carries_the_family_without_changing_digests() -> None:
    card = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["src/aiworkhub/core.py"],
        test_paths=["tests/test_x.py"],
    )
    assert card["skill_task_family"] == "implementation"
    assert card["work_kind"] == "generic"
    # The provenance digests must not move: they authenticate stored cards.
    assert card["template_full_id"] == task_templates.template_full_id(
        "implementation_with_tests"
    )
    provenance = task_templates.template_provenance_payload(
        card, classification_reason="test"
    )
    assert task_templates.validate_template_provenance(provenance, expanded_card=card)


# ---------------------------------------------------------------------------
# 2. A card that declares nothing selects nothing
# ---------------------------------------------------------------------------


def test_a_card_declaring_nothing_yields_no_selection_context() -> None:
    assert skill_registry.card_selection_context({}) is None
    assert (
        skill_registry.card_selection_context(
            {"allowed_writes": ["src/aiworkhub/core.py"], "risk_tier": "medium"}
        )
        is None
    )
    # Family + stage without the two dimensions that decide relevance is also
    # nothing: matching must not become unconditional on triggers/applicability.
    assert (
        skill_registry.card_selection_context(
            {
                "allowed_writes": ["src/aiworkhub/core.py"],
                "risk_tier": "medium",
                "skill_task_family": "bugfix",
                "skill_stage": "review",
            }
        )
        is None
    )


def test_multi_root_write_set_has_no_single_scope() -> None:
    assert (
        skill_registry.common_path_scope(
            ["src/aiworkhub/core.py", "src/aiworkhub/project_context.py"]
        )
        == "src/aiworkhub"
    )
    assert skill_registry.common_path_scope(["src/a.py", "tests/b.py"]) == ""
    assert skill_registry.common_path_scope([]) == ""


def test_a_production_plus_tests_card_still_resolves_one_scope() -> None:
    """The tests follow the code, so the production partition is the scope."""
    card = {
        "allowed_writes": [
            "src/aiworkhub/core.py",
            "src/aiworkhub/project_context.py",
            "tests/test_core.py",
        ],
        "risk_tier": "high",
        "skill_task_family": "bugfix",
        "skill_stage": "review",
        "skill_triggers": ["unknown_or_empty_result"],
        "skill_applicability": ["observability_surface"],
    }
    # The whole write set shares no prefix at all ...
    assert skill_registry.common_path_scope(card["allowed_writes"]) == ""
    # ... but the card is still about src/aiworkhub, without any wildcard.
    context = project_context._skill_selection_context(card)
    assert context is not None
    assert context["path_or_symbol"] == "src/aiworkhub"


def test_several_production_roots_still_require_an_explicit_scope() -> None:
    card = {
        "allowed_writes": ["src/aiworkhub/core.py", "vscode-extension/src/x.ts"],
        "risk_tier": "high",
        "skill_task_family": "bugfix",
        "skill_stage": "review",
        "skill_triggers": ["unknown_or_empty_result"],
        "skill_applicability": ["observability_surface"],
    }
    assert project_context._skill_selection_context(card) is None
    card["skill_path_scope"] = "src/aiworkhub"
    assert project_context._skill_selection_context(card) is not None


# ---------------------------------------------------------------------------
# 3. risk_tier is populated at create, from the declared write scope
# ---------------------------------------------------------------------------


def test_create_time_tier_matches_the_accept_time_computation() -> None:
    """The same two functions, on allowed_writes instead of the diff."""
    card = {"task_type": "code", "validation": ["pytest tests/test_x.py"]}
    writes = ["src/aiworkhub/process_launcher.py", "tests/test_x.py"]
    signals = quality_evidence.derive_risk_signals(card, writes)
    tier = quality_evidence.resolve_risk_profile(
        quality_evidence.RISK_LOW, signals=signals
    )["effective_tier"]
    # process_launcher.py trips the concurrency marker, which floors at high.
    assert "concurrency" in signals
    assert tier == quality_evidence.RISK_HIGH


def test_declared_tier_is_a_floor_and_never_a_ceiling() -> None:
    signals = quality_evidence.derive_risk_signals(
        {"task_type": "code", "validation": ["pytest tests/test_x.py"]},
        ["src/aiworkhub/repository_state.py", "tests/test_x.py"],
    )
    lowered = quality_evidence.resolve_risk_profile(
        quality_evidence.RISK_LOW, signals=signals
    )["effective_tier"]
    raised = quality_evidence.resolve_risk_profile(
        quality_evidence.RISK_CRITICAL, signals=signals
    )["effective_tier"]
    assert lowered != quality_evidence.RISK_LOW
    assert raised == quality_evidence.RISK_CRITICAL


def test_create_populates_risk_tier_on_the_card(coord) -> None:
    """The tier is on the card at create, not only after accept_review."""
    result = _create()
    assert result["ok"] is True, result
    card = task_store.get_task(coord, "T_SKILL")
    assert card is not None
    assert card["risk_tier"] in quality_evidence.RISK_TIERS
    assert card["risk_tier_origin"] == "derived"
    # A .py write in a code card is a code change; two declared outputs are a
    # combined change. Both floor at medium, so the tier is never a bare "low".
    assert "code_change" in card["risk_signals"]
    assert card["risk_tier"] != quality_evidence.RISK_LOW


def test_create_escalates_the_tier_from_the_declared_write_scope(coord) -> None:
    """allowed_writes IS the declared change set, so the paths decide."""
    result = _create(
        allowed_writes=["src/aiworkhub/process_launcher.py"],
        required_outputs=["src/aiworkhub/process_launcher.py"],
    )
    assert result["ok"] is True, result
    card = task_store.get_task(coord, "T_SKILL")
    assert "concurrency" in card["risk_signals"]
    assert card["risk_tier"] == quality_evidence.RISK_HIGH


def test_a_declared_tier_is_recorded_as_declared(coord) -> None:
    result = _create(risk_tier="critical")
    assert result["ok"] is True, result
    card = task_store.get_task(coord, "T_SKILL")
    assert card["risk_tier"] == "critical"
    assert card["risk_tier_origin"] == "declared"


def test_deriving_the_tier_does_not_break_create_idempotency(coord) -> None:
    """A same-payload retry must reconcile, not read as a payload conflict."""
    first = _create()
    assert first["created"] is True
    second = _create()
    assert second["ok"] is True, second
    assert second["created"] is False
    assert second["reconciled"] is True
    assert second["receipt_state"] == "existing_identical"


def test_create_refuses_an_unknown_skill_token(coord) -> None:
    result = _create(skill_triggers=["a surface reports unknown"])
    assert result["ok"] is False
    assert "invalid_skill_vocabulary" in result["stderr"]
    assert "unknown_or_empty_result" in result["allowed_skill_triggers"]
    assert task_store.get_task(coord, "T_SKILL") is None


def test_create_persists_declared_skill_vocabulary(coord) -> None:
    result = _create(
        skill_task_family="bugfix",
        skill_stage="review",
        skill_triggers=["unknown_or_empty_result", "unknown_or_empty_result"],
        skill_applicability=["quality_gate"],
    )
    assert result["ok"] is True, result
    card = task_store.get_task(coord, "T_SKILL")
    assert card["skill_task_family"] == "bugfix"
    assert card["skill_stage"] == "review"
    assert card["skill_triggers"] == ["unknown_or_empty_result"]
    assert card["skill_applicability"] == ["quality_gate"]
    assert skill_registry.card_selection_context(card) is not None
    assert skill_registry.card_selection_context(card) is not None


def test_the_mcp_create_tool_can_declare_the_vocabulary(coord) -> None:
    """The manager-facing tool, not only ``core.create_task``, carries it.

    Every test above this one creates through ``core.create_task`` directly,
    which is exactly why the gap survived: core accepted all five ``skill_*``
    fields while ``aiworkhub_task_create`` offered none, so no card created the
    only way a manager can create one could declare the vocabulary that
    ``select`` matches on -- 0 of 4,628 stored cards did.
    """
    result = server.aiworkhub_task_create(
        task_id="T_SKILL_MCP",
        title="skill vocabulary card over MCP",
        runner="claude_coding",
        topic="coding",
        objective="fix the reported defect",
        acceptance=["it works"],
        allowed_writes=["src/aiworkhub/foo.py"],
        required_outputs=["src/aiworkhub/foo.py"],
        validation=["pytest -q tests/test_foo.py"],
        custom_template_escape="audited_custom_unclassified",
        skill_task_family="bugfix",
        skill_stage="review",
        skill_triggers=["unknown_or_empty_result"],
        skill_applicability=["quality_gate"],
        skill_path_scope="src/aiworkhub",
    )
    assert result["ok"] is True, result
    card = task_store.get_task(coord, "T_SKILL_MCP")
    assert card["skill_task_family"] == "bugfix"
    assert card["skill_stage"] == "review"
    assert card["skill_triggers"] == ["unknown_or_empty_result"]
    assert card["skill_applicability"] == ["quality_gate"]
    assert card["skill_path_scope"] == "src/aiworkhub"

    context = skill_registry.card_selection_context(card)
    assert context is not None
    assert context["path_or_symbol"] == "src/aiworkhub"


def test_the_mcp_create_tool_refuses_an_unknown_skill_token(coord) -> None:
    """Validation is core's, and the tool must not route around it."""
    result = server.aiworkhub_task_create(
        task_id="T_SKILL_MCP_BAD",
        title="skill vocabulary card over MCP",
        runner="claude_coding",
        topic="coding",
        objective="fix the reported defect",
        acceptance=["it works"],
        allowed_writes=["src/aiworkhub/foo.py"],
        required_outputs=["src/aiworkhub/foo.py"],
        validation=["pytest -q tests/test_foo.py"],
        custom_template_escape="audited_custom_unclassified",
        skill_stage="review_ready",
    )
    assert result["ok"] is False
    assert "invalid_skill_vocabulary" in result["stderr"]
    assert task_store.get_task(coord, "T_SKILL_MCP_BAD") is None

# ---------------------------------------------------------------------------
# 4. End to end: card -> select -> packet -> worker context bundle
# ---------------------------------------------------------------------------


def _vocabulary_record(
    *,
    identity: str = "unknown_or_empty_is_not_measured",
    lifecycle: skill_registry.LifecycleState = skill_registry.LifecycleState.ACTIVE,
    evidence_actors: tuple[str, ...] = ("manager.claude.7e6e8a47", "worker.codex_gpt-5.5"),
    risk: skill_registry.RiskLevel = skill_registry.RiskLevel.HIGH,
) -> skill_registry.SkillRecord:
    """One ACTIVE record whose matching fields are all vocabulary tokens."""
    return skill_registry.validate_record(
        skill_registry.SkillRecord(
            identity=identity,
            version="1.0.0",
            scope=skill_registry.SkillScope.REPOSITORY,
            task_family="bugfix",
            path_or_symbol="src/aiworkhub/*",
            risk=risk,
            stage="review",
            triggers=("unknown_or_empty_result", "swallowed_exception_default"),
            applicability=("observability_surface", "quality_gate"),
            procedure_steps=(
                "Read what the surface does with a missing or unreadable input.",
                "Name the population actually examined before reporting a count.",
            ),
            avoid_rules=("Do not treat a green gate as evidence.",),
            preferred_tools=("aiworkhub_worker_source_graph_query",),
            confidence=0.9,
            evidence=tuple(
                skill_registry.EvidenceRecord(
                    source=f"commit-{index}",
                    outcome=skill_registry.EvidenceOutcome.ACCEPTED,
                    authority=skill_registry.AuthorityRole.MANAGER,
                    actor_id=actor,
                )
                for index, actor in enumerate(evidence_actors)
            ),
            lifecycle_state=lifecycle,
            accepted_count=len(evidence_actors),
        )
    )


def _skills_repo(tmp_path: Path, record: skill_registry.SkillRecord) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    skill_registry_store.put_record(repo, record)
    return repo


def _vocabulary_card(**overrides: object) -> dict:
    card = {
        "task_id": "TASK_SKILL_E2E",
        "runner": "claude_worker_ctx",
        "topic": "task_mcp",
        "status": "pending",
        "allowed_writes": [
            "src/aiworkhub/core.py",
            "src/aiworkhub/project_context.py",
        ],
        "risk_tier": "high",
        "skill_task_family": "bugfix",
        "skill_stage": "review",
        "skill_triggers": ["unknown_or_empty_result"],
        "skill_applicability": ["observability_surface"],
        "project_context": {
            "required": False,
            "source_graph": {
                "mode": "focus",
                "query": "collect_project_context",
                "budget": 16,
                "bundle_type": "explore",
            },
            "session": {"topic": "skill injection", "limit": 2},
        },
    }
    card.update(overrides)
    return card


def _stub_context_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_direct(repo, contract):
        return json.dumps({"tool": "source_graph", "matches": [{"name": "x"}]}), False

    def fake_session(ctx, *, limit: int = 12):
        return {
            "ok": True,
            "content": json.dumps({"topic": ctx.session_topic, "evidence": []}),
            "truncated": False,
            "hit_count": 0,
        }

    monkeypatch.setattr(project_context, "_source_graph_direct", fake_direct)
    monkeypatch.setattr(
        project_context._worker_tools, "session_current_state", fake_session
    )


def test_select_and_packet_are_non_empty_for_a_vocabulary_card(tmp_path: Path) -> None:
    record = _vocabulary_record()
    repo = _skills_repo(tmp_path, record)
    context = skill_registry.card_selection_context(_vocabulary_card())
    assert context is not None
    candidates = skill_registry_store.load_registry(repo).records()
    receipt = skill_registry.select(candidates, context, limit=4)
    assert [item.identity for item in receipt.selected] == [record.identity]
    packet = skill_registry.build_runtime_packet(candidates, receipt)
    assert len(packet.skills) == 1
    assert packet.skills[0].procedure_steps


def test_packet_reaches_the_worker_context_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The end-to-end claim: the packet is IN the bundle the worker receives."""
    record = _vocabulary_record()
    repo = _skills_repo(tmp_path, record)
    _stub_context_tools(monkeypatch)

    result = project_context.collect_project_context(repo, _vocabulary_card())
    assert result is not None

    payload = json.loads(result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    skills = payload["evidence"]["skills"]
    assert skills["schema_id"] == project_context.SKILL_PACKET_SCHEMA_ID
    assert [row["identity"] for row in skills["skills"]] == [record.identity]
    assert skills["skills"][0]["procedure_steps"]
    # The section is shaped exactly like its neighbours.
    section = next(
        item for item in result.metadata["sections"] if item["name"] == "skills"
    )
    assert section["executed"] is True
    assert section["hit_count"] == 1
    assert section["degraded_reason"] == ""
    assert section["bytes"] > 0
    assert result.metadata["section_count"] == 3


# ---------------------------------------------------------------------------
# 5. The declared skill tier is a FLOOR, proved through the same round trip
# ---------------------------------------------------------------------------


def test_a_lower_tier_skill_still_reaches_a_higher_risk_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A medium-tier rule holds on a high-risk card, and the packet says why.

    This is the whole round trip -- card vocabulary -> select -> packet ->
    worker bundle -- not a unit assertion on the comparison. Under exact
    matching the bundle carried no skills section content at all, because a
    card whose tier escalated (the only direction ``resolve_risk_profile``
    moves) stopped matching the rule mined for it.
    """
    record = _vocabulary_record(risk=skill_registry.RiskLevel.MEDIUM)
    repo = _skills_repo(tmp_path, record)
    _stub_context_tools(monkeypatch)
    card = _vocabulary_card(risk_tier="high")

    context = skill_registry.card_selection_context(card)
    assert context is not None
    assert context["risk"] is skill_registry.RiskLevel.HIGH

    candidates = skill_registry_store.load_registry(repo).records()
    receipt = skill_registry.select(candidates, context, limit=4)
    assert [item.identity for item in receipt.selected] == [record.identity]
    assert "risk:at_or_above" in receipt.selected[0].reasons

    result = project_context.collect_project_context(repo, card)
    assert result is not None
    payload = json.loads(result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    rows = payload["evidence"]["skills"]["skills"]
    assert [row["identity"] for row in rows] == [record.identity]
    assert "risk:at_or_above" in rows[0]["reasons"]
    assert rows[0]["procedure_steps"]


def test_every_tier_at_or_above_the_declared_floor_matches() -> None:
    """Monotone in the card tier, and equality still reports ``exact``."""
    record = _vocabulary_record(risk=skill_registry.RiskLevel.MEDIUM)
    expected = {
        "medium": "risk:exact",
        "high": "risk:at_or_above",
        "critical": "risk:at_or_above",
    }
    for tier, reason in expected.items():
        context = skill_registry.card_selection_context(_vocabulary_card(risk_tier=tier))
        assert context is not None, tier
        receipt = skill_registry.select([record], context, limit=4)
        assert [item.identity for item in receipt.selected] == [record.identity], tier
        assert reason in receipt.selected[0].reasons, tier


def test_a_higher_tier_skill_is_not_owed_by_a_lower_risk_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The relation is ordered, not symmetric: it must not match downward.

    Critical-only precautions are not automatically owed by medium-risk work.
    Without this the change would be "match anything", which is the failure
    mode a wildcard is deliberately refused for.
    """
    record = _vocabulary_record(risk=skill_registry.RiskLevel.CRITICAL)
    repo = _skills_repo(tmp_path, record)
    _stub_context_tools(monkeypatch)
    card = _vocabulary_card(risk_tier="medium")

    context = skill_registry.card_selection_context(card)
    assert context is not None
    assert skill_registry.select([record], context, limit=4).selected == ()

    result = project_context.collect_project_context(repo, card)
    assert result is not None
    payload = json.loads(result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    assert "skills" not in payload["evidence"]
    section = next(
        item for item in result.metadata["sections"] if item["name"] == "skills"
    )
    assert section["executed"] is True
    assert section["hit_count"] == 0


def test_zero_match_is_an_executed_suppressed_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A declared vocabulary that matches nothing still says it looked."""
    repo = _skills_repo(tmp_path, _vocabulary_record())
    _stub_context_tools(monkeypatch)

    card = _vocabulary_card(skill_stage="orientation")
    result = project_context.collect_project_context(repo, card)
    assert result is not None
    payload = json.loads(result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    assert "skills" not in payload["evidence"]
    section = next(
        item for item in result.metadata["sections"] if item["name"] == "skills"
    )
    assert section["executed"] is True
    assert section["hit_count"] == 0
    # Two suppressed sections: this zero-match skills packet and the stub's
    # zero-hit Session Manager state, which is suppressed the same way since
    # worker_prompt-2 (its metadata still says executed / hit_count 0).
    assert result.metadata["optimization"]["zero_hit_suppression_count"] == 2


def test_card_without_vocabulary_produces_no_skills_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Existing cards are unchanged: absent vocabulary is not an error."""
    repo = _skills_repo(tmp_path, _vocabulary_record())
    _stub_context_tools(monkeypatch)

    card = _vocabulary_card()
    for field in (
        "skill_task_family",
        "skill_stage",
        "skill_triggers",
        "skill_applicability",
        "risk_tier",
    ):
        card.pop(field)
    result = project_context.collect_project_context(repo, card)
    assert result is not None
    assert [item["name"] for item in result.metadata["sections"]] == [
        "source_graph",
        "session_current_state",
    ]


def test_a_self_certified_active_record_is_not_injected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One canonical actor is not two, so the record is demoted before select."""
    record = _vocabulary_record(
        evidence_actors=("manager.claude.7e6e8a47", "claude_manager_7e6e8a47")
    )
    repo = _skills_repo(tmp_path, record)
    _stub_context_tools(monkeypatch)

    result = project_context.collect_project_context(repo, _vocabulary_card())
    assert result is not None
    payload = json.loads(result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    assert "skills" not in payload["evidence"]


def test_a_stored_card_with_an_unknown_token_degrades_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _skills_repo(tmp_path, _vocabulary_record())
    _stub_context_tools(monkeypatch)

    card = _vocabulary_card(skill_triggers=["a trigger this build does not know"])
    result = project_context.collect_project_context(repo, card)
    assert result is not None
    section = next(
        item for item in result.metadata["sections"] if item["name"] == "skills"
    )
    assert section["degraded_reason"].startswith("skill_vocabulary_rejected:")


def test_a_template_card_gets_its_family_from_its_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A card created from a template never had to declare its own family."""
    bugfix_record = _vocabulary_record()
    repo = _skills_repo(tmp_path, bugfix_record)
    _stub_context_tools(monkeypatch)

    card = _vocabulary_card()
    card.pop("skill_task_family")
    card["work_kind"] = "generic"
    card["template_provenance"] = {"template_name": "bugfix_with_regression"}
    result = project_context.collect_project_context(repo, card)
    assert result is not None
    payload = json.loads(result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    assert [row["identity"] for row in payload["evidence"]["skills"]["skills"]] == [
        bugfix_record.identity
    ]


# ---------------------------------------------------------------------------
# 6. The surface cards are ACTUALLY created on: create_from_template
# ---------------------------------------------------------------------------


def _create_from_template(**overrides: object) -> dict:
    """Create one bugfix card through the real MCP template tool."""
    kwargs: dict = dict(
        task_id="T_SKILL_TEMPLATE",
        title="skill vocabulary card from a template",
        runner="claude_coding",
        topic="coding",
        objective="fix the reported defect",
        acceptance=["it works"],
        template_id="bugfix_with_regression",
        production_paths=["src/aiworkhub/foo.py"],
        test_paths=["tests/test_foo.py"],
        risk_tier="high",
    )
    kwargs.update(overrides)
    return server.aiworkhub_task_create_from_template(**kwargs)


def test_the_create_from_template_tool_can_declare_the_vocabulary(
    coord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole trip on the real surface: tool -> store -> packet -> bundle.

    ``aiworkhub_task_create`` is the legacy surface; cards are created from
    templates. Section 3 proved the legacy tool, and the template tool carried
    no ``skill_*`` parameter at all -- which is why 0 of 4,628 stored cards
    declare the vocabulary. Nothing here calls ``core.create_task``: the card
    is created the way cards are created and read back out of the store.
    """
    record = _vocabulary_record()
    skill_registry_store.put_record(coord, record)
    _stub_context_tools(monkeypatch)

    result = _create_from_template(
        skill_stage="review",
        skill_triggers=["unknown_or_empty_result"],
        skill_applicability=["quality_gate"],
        skill_path_scope="src/aiworkhub",
    )
    assert result["ok"] is True, result

    card = task_store.get_task(coord, "T_SKILL_TEMPLATE")
    assert card is not None
    # The template supplied the family; the card declared the other four.
    assert card["skill_task_family"] == "bugfix"
    assert card["skill_stage"] == "review"
    assert card["skill_triggers"] == ["unknown_or_empty_result"]
    assert card["skill_applicability"] == ["quality_gate"]
    assert card["skill_path_scope"] == "src/aiworkhub"

    context = skill_registry.card_selection_context(card)
    assert context is not None
    assert context["task_family"] == "bugfix"
    assert context["path_or_symbol"] == "src/aiworkhub"

    candidates = skill_registry_store.load_registry(coord).records()
    receipt = skill_registry.select(candidates, context, limit=4)
    assert [item.identity for item in receipt.selected] == [record.identity]
    packet = skill_registry.build_runtime_packet(candidates, receipt)
    assert [item.identity for item in packet.skills] == [record.identity]
    assert packet.skills[0].procedure_steps

    # ... and the packet is in the bundle the worker receives. The section
    # config is attached at launch, not by create, so it is added here; every
    # skill field comes from the stored card untouched.
    bundle_card = {
        **card,
        "project_context": {
            "required": False,
            "source_graph": {
                "mode": "focus",
                "query": "collect_project_context",
                "budget": 16,
                "bundle_type": "explore",
            },
            "session": {"topic": "skill injection", "limit": 2},
        },
    }
    bundle = project_context.collect_project_context(coord, bundle_card)
    assert bundle is not None
    payload = json.loads(bundle.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    rows = payload["evidence"]["skills"]["skills"]
    assert [row["identity"] for row in rows] == [record.identity]
    assert rows[0]["procedure_steps"]


def test_a_template_card_stores_the_family_its_template_declares(coord) -> None:
    """The template default is a stored fact, not only a read-side rescue.

    ``project_context`` can already recover the family from provenance, but
    ``card_selection_context`` reads the card alone: without the stored family
    a card that declares everything else still selects nothing.
    """
    result = _create_from_template(
        task_id="T_SKILL_TEMPLATE_DEFAULT",
        skill_stage="review",
        skill_triggers=["unknown_or_empty_result"],
        skill_applicability=["quality_gate"],
        skill_path_scope="src/aiworkhub",
    )
    assert result["ok"] is True, result
    card = task_store.get_task(coord, "T_SKILL_TEMPLATE_DEFAULT")
    assert card["skill_task_family"] == task_templates.skill_task_family(
        task_templates.TEMPLATE_SPECS["bugfix_with_regression"].work_kind
    )
    assert skill_registry.card_selection_context(card) is not None


def test_an_explicit_family_overrides_the_template_default(coord) -> None:
    """A default the card cannot override is not a default."""
    result = _create_from_template(
        task_id="T_SKILL_TEMPLATE_OVERRIDE",
        skill_task_family="refactor",
        skill_stage="review",
        skill_triggers=["unknown_or_empty_result"],
        skill_applicability=["quality_gate"],
    )
    assert result["ok"] is True, result
    card = task_store.get_task(coord, "T_SKILL_TEMPLATE_OVERRIDE")
    assert card["skill_task_family"] == "refactor"


def test_the_create_from_template_tool_refuses_an_unknown_skill_token(
    coord,
) -> None:
    """Validation is core's, and the template tool must not route around it."""
    result = _create_from_template(
        task_id="T_SKILL_TEMPLATE_BAD",
        skill_stage="review_ready",
    )
    assert result["ok"] is False
    assert "invalid_skill_vocabulary" in result["stderr"]
    assert task_store.get_task(coord, "T_SKILL_TEMPLATE_BAD") is None


# ---------------------------------------------------------------------------
# 7. An all-empty selection chain cannot stay silently healthy
#
# The three sections above prove selection WORKS. None of them could have
# noticed that it was, in production, returning nothing at all: 24 of 24
# measured card contexts selected zero skills while every surface reported a
# well-formed, bounded, entirely ordinary empty packet. What follows measures
# the emptiness itself -- why it happened, how long it has been happening, and
# whether the records the registry holds could ever have matched.
# ---------------------------------------------------------------------------


def _prose_record(**overrides) -> skill_registry.SkillRecord:
    """An ACTIVE record carrying free-text prose where tokens belong.

    This is the shape the store actually held: valid, digest-clean, ACTIVE, and
    unreachable by every card context that can legally exist.
    """
    fields = {
        "identity": "prose_vocabulary_skill",
        "triggers": ("a surface reports unknown or empty",),
        "applicability": ("reporting and observability surfaces",),
    }
    fields.update(overrides)
    return skill_registry.validate_record(
        skill_registry.replace(_vocabulary_record(), **fields)
    )


def test_prose_vocabulary_names_the_dimensions_no_card_can_match() -> None:
    record = _prose_record()
    assert record.lifecycle_state is skill_registry.LifecycleState.ACTIVE
    assert skill_registry.unreachable_selection_dimensions(record) == (
        "triggers",
        "applicability",
    )
    assert skill_registry.is_injectable(record) is False
    # The reachable record is the control: same lifecycle, same evidence.
    assert skill_registry.unreachable_selection_dimensions(_vocabulary_record()) == ()
    assert skill_registry.is_injectable(_vocabulary_record()) is True


def test_one_known_token_is_enough_and_the_wildcard_still_reaches() -> None:
    """Reachability follows the matcher, it does not invent a stricter rule.

    ``_tuple_match`` needs ONE intersecting token, and an unconstrained or
    wildcard dimension matches every card, so a record must not be reported
    unreachable for carrying prose ALONGSIDE a real token.
    """
    mixed = _prose_record(
        triggers=("a sentence nobody can match", "unknown_or_empty_result"),
        applicability=("*",),
    )
    assert skill_registry.unreachable_selection_dimensions(mixed) == ()
    unconstrained = _prose_record(triggers=(), applicability=())
    assert skill_registry.unreachable_selection_dimensions(unconstrained) == ()


def test_every_empty_selection_reports_which_link_of_the_chain_broke() -> None:
    """Four causes, four tokens, decided by how early the chain broke."""
    context = skill_registry.card_selection_context(_vocabulary_card())
    assert context is not None

    def reason(candidates):
        receipt = skill_registry.select(candidates, context, limit=4)
        return skill_registry.selection_empty_reason(candidates, receipt)

    assert reason([]) == skill_registry.SELECTION_EMPTY_NO_CANDIDATES
    proposed = _vocabulary_record(
        lifecycle=skill_registry.LifecycleState.PROPOSED
    )
    assert reason([proposed]) == skill_registry.SELECTION_EMPTY_NO_ACTIVE_RECORDS
    assert reason([_prose_record()]) == (
        skill_registry.SELECTION_EMPTY_ACTIVE_VOCABULARY_UNSELECTABLE
    )
    # Reachable, ACTIVE, and simply not a match for THIS card's stage.
    other_stage = skill_registry.validate_record(
        skill_registry.replace(_vocabulary_record(), stage="orientation")
    )
    assert reason([other_stage]) == skill_registry.SELECTION_EMPTY_NO_VOCABULARY_MATCH
    # A selection that found something has no reason to report.
    assert reason([_vocabulary_record()]) == ""
    assert skill_registry.SELECTION_EMPTY_REASONS == {
        skill_registry.SELECTION_EMPTY_NO_CANDIDATES,
        skill_registry.SELECTION_EMPTY_NO_ACTIVE_RECORDS,
        skill_registry.SELECTION_EMPTY_ACTIVE_VOCABULARY_UNSELECTABLE,
        skill_registry.SELECTION_EMPTY_NO_VOCABULARY_MATCH,
    }


def test_the_receipt_separates_what_was_selected_from_what_was_injected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Injection is the packet; selection is the decision. Both are recorded."""
    record = _vocabulary_record()
    repo = _skills_repo(tmp_path, record)
    _stub_context_tools(monkeypatch)

    assert project_context.collect_project_context(repo, _vocabulary_card()) is not None
    receipt = skill_registry_store.get_selection(repo, "TASK_SKILL_E2E")
    assert receipt is not None
    assert receipt["selected_count"] == 1
    assert receipt["injected_count"] == 1
    assert receipt["empty_reason"] == ""
    assert receipt["measured"] is True


def test_an_empty_receipt_records_why_rather_than_only_that(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 24/24 case: a receipt exists, and it now says what went wrong."""
    repo = _skills_repo(tmp_path, _prose_record())
    _stub_context_tools(monkeypatch)

    assert project_context.collect_project_context(repo, _vocabulary_card()) is not None
    receipt = skill_registry_store.get_selection(repo, "TASK_SKILL_E2E")
    assert receipt is not None
    assert receipt["selected_count"] == 0
    assert receipt["injected_count"] == 0
    assert receipt["empty_reason"] == (
        skill_registry.SELECTION_EMPTY_ACTIVE_VOCABULARY_UNSELECTABLE
    )
    assert receipt["measured"] is True
    # The same fact reaches the bundle metadata, as its OWN block rather than a
    # per-section column: a defaulted zero on every other section would read as
    # "that surface selected nothing" instead of "that surface has no selection".
    metadata = project_context.collect_project_context(
        repo, _vocabulary_card()
    ).metadata
    assert metadata["skill_selection"] == {
        "measured": True,
        "selected_count": 0,
        "injected_count": 0,
        "evidence_backed_count": 0,
        "empty_reason": (
            skill_registry.SELECTION_EMPTY_ACTIVE_VOCABULARY_UNSELECTABLE
        ),
        "failure_reason": "",
    }


def test_an_empty_receipt_written_without_a_reason_is_still_explicit(
    tmp_path: Path,
) -> None:
    """A blank reason keeps ONE meaning: the row predates the measurement."""
    repo = tmp_path / "repo"
    repo.mkdir()
    recorded = skill_registry_store.record_selection(
        repo,
        task_id="TASK_NO_REASON",
        packet={"version": 1, "skills": []},
    )
    assert recorded["empty_reason"] == (
        skill_registry_store.SELECTION_EMPTY_REASON_NOT_REPORTED
    )
    assert recorded["injected_count"] == 0


def test_coverage_reports_the_streak_and_refuses_to_call_it_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Totals, injectable, selection, injection, and the consecutive run."""
    repo = _skills_repo(tmp_path, _prose_record())
    _stub_context_tools(monkeypatch)
    for index in range(3):
        card = _vocabulary_card(task_id=f"TASK_EMPTY_{index}")
        assert project_context.collect_project_context(repo, card) is not None

    coverage = skill_registry_store.skill_coverage(repo)
    assert coverage["measured"] is True
    assert coverage["skills"]["total"] == 1
    assert coverage["skills"]["active"] == 1
    # ACTIVE, yet nothing a card could reach. Counting ACTIVE alone would have
    # reported this registry as ready to inject.
    assert coverage["skills"]["injectable"] == 0
    assert coverage["skills"]["active_unreachable_vocabulary"] == 1
    assert coverage["selection"]["receipts"] == 3
    assert coverage["selection"]["selection_count"] == 0
    assert coverage["selection"]["injection_count"] == 0
    assert coverage["selection"]["consecutive_empty_streak"] == 3
    assert coverage["selection"]["all_empty"] is True
    assert coverage["selection"]["empty_reasons"] == {
        skill_registry.SELECTION_EMPTY_ACTIVE_VOCABULARY_UNSELECTABLE: 3
    }
    # No raw rows: the projection is counts, a streak, and reason tallies.
    assert "skills" not in coverage["selection"]
    assert not any(
        isinstance(value, list) for value in coverage["selection"].values()
    )


def test_one_injected_card_breaks_the_streak(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The streak is a leading run over the newest receipts, not a total."""
    repo = _skills_repo(tmp_path, _vocabulary_record())
    _stub_context_tools(monkeypatch)
    # Oldest first: a miss, then a hit. The newest receipt injected, so the
    # streak is zero even though an empty receipt is still on record.
    assert project_context.collect_project_context(
        repo, _vocabulary_card(task_id="TASK_MISS", skill_stage="orientation")
    ) is not None
    assert project_context.collect_project_context(
        repo, _vocabulary_card(task_id="TASK_HIT")
    ) is not None

    coverage = skill_registry_store.skill_coverage(repo)
    assert coverage["selection"]["receipts"] == 2
    assert coverage["selection"]["injection_count"] == 1
    assert coverage["selection"]["empty_receipts"] == 1
    assert coverage["selection"]["consecutive_empty_streak"] == 0
    assert coverage["selection"]["all_empty"] is False
    assert coverage["skills"]["injectable"] == 1
    assert coverage["selection"]["empty_reasons"] == {
        skill_registry.SELECTION_EMPTY_NO_VOCABULARY_MATCH: 1
    }


def test_an_absent_skill_store_is_unmeasured_and_never_a_row_of_zeros(
    tmp_path: Path,
) -> None:
    """"Nobody looked" and "nothing was injected" must not render alike."""
    repo = tmp_path / "empty_repo"
    repo.mkdir()
    coverage = skill_registry_store.skill_coverage(repo)
    assert coverage["measured"] is False
    assert coverage["unavailable_reason"] == "skill_store_absent"
    assert coverage["skills"] == {}
    assert coverage["selection"] == {}


# ---------------------------------------------------------------------------
# 8. The evidence gate is what makes a selected skill worth injecting
# ---------------------------------------------------------------------------


def _reachable_proposal() -> skill_registry.SkillRecord:
    """The same reachable content, as an evidence-free PROPOSED record."""
    return skill_registry.validate_record(
        skill_registry.replace(
            _vocabulary_record(),
            identity="evidence_qualified_skill",
            lifecycle_state=skill_registry.LifecycleState.PROPOSED,
            evidence=(),
            accepted_count=0,
        )
    )


def test_accepted_evidence_from_two_actors_carries_a_skill_into_selection(
    tmp_path: Path,
) -> None:
    """The whole loop, through the canonical store and lifecycle path only.

    propose -> add_evidence (two DISTINCT canonical actors) -> activate, each
    step persisted with :func:`advance_record` under its compare-and-swap token,
    and only then does a deterministic card context select the skill. Nothing
    here writes the database directly and no evidence is invented: both entries
    are bound to an authenticated authority by ``add_evidence`` itself.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    proposal = _reachable_proposal()
    worker = skill_registry.Authority(
        skill_registry.AuthorityRole.WORKER, actor_id="worker.claude_coding", token=""
    )
    manager = skill_registry.Authority(
        skill_registry.AuthorityRole.MANAGER,
        actor_id="manager.claude.7e6e8a47",
        token="manager-secret",
    )
    registry = skill_registry.SkillRegistry(min_accepted_evidence=2)
    registry.propose(proposal, worker)
    skill_registry_store.put_record(repo, proposal)

    for authority, source in ((worker, "T_FIRST"), (manager, "T_SECOND")):
        expected = skill_registry_store.stored_state_digest(
            repo, proposal.identity, proposal.version
        )
        updated = registry.add_evidence(
            proposal.identity,
            proposal.version,
            {"source": source, "outcome": "accepted"},
            authority,
        )
        skill_registry_store.advance_record(
            repo, updated, expected_state_digest=expected
        )

    expected = skill_registry_store.stored_state_digest(
        repo, proposal.identity, proposal.version
    )
    activated = registry.activate(proposal.identity, proposal.version, manager)
    skill_registry_store.advance_record(repo, activated, expected_state_digest=expected)

    assert skill_registry.independent_accepted_evidence_count(activated) == 2
    assert skill_registry.is_injectable(activated) is True

    # Loaded back through load_registry, so the demotion rule is in force.
    candidates = skill_registry_store.load_registry(repo).records()
    context = skill_registry.card_selection_context(_vocabulary_card())
    assert context is not None
    receipt = skill_registry.select(candidates, context, limit=4)
    assert [item.identity for item in receipt.selected] == [proposal.identity]
    assert skill_registry.selection_empty_reason(candidates, receipt) == ""
    assert skill_registry_store.skill_coverage(repo)["skills"]["injectable"] == 1


def test_one_accepted_actor_never_activates_however_many_entries_it_files(
    tmp_path: Path,
) -> None:
    """The two-actor floor is unchanged, and unreachable by repetition."""
    repo = tmp_path / "repo"
    repo.mkdir()
    proposal = _reachable_proposal()
    worker = skill_registry.Authority(
        skill_registry.AuthorityRole.WORKER, actor_id="worker.claude_coding", token=""
    )
    manager = skill_registry.Authority(
        skill_registry.AuthorityRole.MANAGER,
        actor_id="manager.claude.7e6e8a47",
        token="manager-secret",
    )
    registry = skill_registry.SkillRegistry(min_accepted_evidence=2)
    registry.propose(proposal, worker)
    skill_registry_store.put_record(repo, proposal)
    for index in range(5):
        updated = registry.add_evidence(
            proposal.identity,
            proposal.version,
            {"source": f"T_{index}", "outcome": "accepted"},
            worker,
        )
    assert updated.accepted_count == 5
    assert skill_registry.independent_accepted_evidence_count(updated) == 1
    with pytest.raises(skill_registry.SkillRegistryError) as excinfo:
        registry.activate(proposal.identity, proposal.version, manager)
    assert excinfo.value.code == "skill_registry.insufficient_evidence"


def test_a_proposal_is_never_selected_and_never_counted_injectable(
    tmp_path: Path,
) -> None:
    """Nothing in this change auto-activates a proposal or invents evidence."""
    repo = tmp_path / "repo"
    repo.mkdir()
    proposal = _reachable_proposal()
    skill_registry_store.put_record(repo, proposal)
    assert skill_registry.is_injectable(proposal) is False

    candidates = skill_registry_store.load_registry(repo).records()
    context = skill_registry.card_selection_context(_vocabulary_card())
    assert context is not None
    receipt = skill_registry.select(candidates, context, limit=4)
    assert receipt.selected == ()
    assert skill_registry.selection_empty_reason(candidates, receipt) == (
        skill_registry.SELECTION_EMPTY_NO_ACTIVE_RECORDS
    )
    coverage = skill_registry_store.skill_coverage(repo)
    assert coverage["skills"]["injectable"] == 0
    assert coverage["skills"]["by_lifecycle"] == {"proposed": 1}


def test_reachability_never_becomes_substring_or_fuzzy_matching() -> None:
    """A near-miss token is a miss. Exactness is the property under test."""
    near_miss = _prose_record(
        triggers=("unknown_or_empty_result_x",),
        applicability=("observability_surfaces",),
    )
    assert skill_registry.unreachable_selection_dimensions(near_miss) == (
        "triggers",
        "applicability",
    )
    context = skill_registry.card_selection_context(_vocabulary_card())
    assert context is not None
    assert skill_registry.select([near_miss], context, limit=4).selected == ()


def test_the_audit_names_an_unreachable_active_record_for_a_manager(
    tmp_path: Path,
) -> None:
    """Verified evidence and impossible vocabulary are two different faults.

    The record below passes the activation rule on two independent actors and
    is still unreachable, so ``verified`` alone would report it as fine. The
    audit names the dimensions instead of quietly leaving it ACTIVE forever.
    """
    repo = _skills_repo(tmp_path, _prose_record())
    entry = skill_registry_store.audit_active_records(repo)[0]
    assert entry["verified"] is True
    assert entry["independent_accepted_actors"] == 2
    assert entry["injectable"] is False
    assert entry["unreachable_dimensions"] == ["triggers", "applicability"]


def test_retiring_the_prose_record_is_a_manager_transition_not_a_db_write(
    tmp_path: Path,
) -> None:
    """Replacement goes through the canonical lifecycle, and selection follows.

    The impossible record is retired by :meth:`SkillRegistry.retire` persisted
    with :func:`advance_record` -- the same runtime-advance path evidence uses,
    with the content digest untouched. Nothing writes ``skills.sqlite`` directly
    and the replacement earns ACTIVE on its own two-actor evidence.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    prose = _prose_record()
    skill_registry_store.put_record(repo, prose)
    manager = skill_registry.Authority(
        skill_registry.AuthorityRole.MANAGER,
        actor_id="manager.claude.7e6e8a47",
        token="manager-secret",
    )
    registry = skill_registry_store.load_registry(repo)
    expected = skill_registry_store.stored_state_digest(
        repo, prose.identity, prose.version
    )
    retired = registry.retire(prose.identity, prose.version, manager)
    skill_registry_store.advance_record(repo, retired, expected_state_digest=expected)
    assert skill_registry.skill_digest(retired) == skill_registry.skill_digest(prose)

    # The replacement: same subject, vocabulary tokens, already evidence-bearing.
    skill_registry_store.put_record(repo, _vocabulary_record())

    candidates = skill_registry_store.load_registry(repo).records()
    context = skill_registry.card_selection_context(_vocabulary_card())
    assert context is not None
    receipt = skill_registry.select(candidates, context, limit=4)
    assert [item.identity for item in receipt.selected] == ["unknown_or_empty_is_not_measured"]

    coverage = skill_registry_store.skill_coverage(repo)
    assert coverage["skills"]["by_lifecycle"] == {"active": 1, "retired": 1}
    assert coverage["skills"]["injectable"] == 1
    assert coverage["skills"]["active_unreachable_vocabulary"] == 0


# ---------------------------------------------------------------------------
# 9. The measurement must not lie about the population it measured
# ---------------------------------------------------------------------------


def test_coverage_discloses_the_population_its_totals_were_counted_over(
    tmp_path: Path,
) -> None:
    """A headline total is a claim about a population. Name the population.

    ``skills.total`` is counted from a bounded, identity-ordered page. Reported
    alone it reads as the whole registry, which is the same under-measurement
    this projection exists to expose -- one clamp away from a dashboard that
    confidently reports a registry it only saw the front of.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    for index in range(3):
        skill_registry_store.put_record(
            repo, _vocabulary_record(identity=f"bounded_skill_{index}")
        )

    clamped = skill_registry_store.skill_coverage(repo, record_limit=2)
    assert clamped["skills"]["total"] == 2
    assert clamped["skills"]["record_limit"] == 2
    # Measured by reading ONE row past the page, not guessed from a full page.
    assert clamped["skills"]["truncated"] is True

    whole = skill_registry_store.skill_coverage(repo)
    assert whole["skills"]["total"] == 3
    assert whole["skills"]["record_limit"] == skill_registry_store.MAX_LOAD_LIMIT
    assert whole["skills"]["truncated"] is False
    assert whole["selection"]["receipt_limit"] == (
        skill_registry_store.MAX_COVERAGE_RECEIPTS
    )
    assert whole["selection"]["truncated"] is False

    for index in range(2):
        skill_registry_store.record_selection(
            repo,
            task_id=f"TASK_BOUNDED_{index}",
            packet={"version": 1, "skills": []},
            empty_reason=skill_registry.SELECTION_EMPTY_NO_VOCABULARY_MATCH,
        )
    paged = skill_registry_store.skill_coverage(repo, limit=1)
    assert paged["selection"]["receipts"] == 1
    # The EFFECTIVE bound, not the module ceiling this call never reached.
    assert paged["selection"]["receipt_limit"] == 1
    assert paged["selection"]["truncated"] is True


def test_a_receipt_refuses_an_empty_reason_outside_the_closed_vocabulary(
    tmp_path: Path,
) -> None:
    """Coverage GROUPS BY this column, so free text is refused at the write."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert skill_registry_store.PERSISTABLE_EMPTY_REASONS == (
        skill_registry.SELECTION_EMPTY_REASONS
        | {skill_registry_store.SELECTION_EMPTY_REASON_NOT_REPORTED}
    )
    with pytest.raises(skill_registry_store.SkillStoreError):
        skill_registry_store.record_selection(
            repo,
            task_id="TASK_PROSE_REASON",
            packet={"version": 1, "skills": []},
            empty_reason="selection looked and did not care for the result",
        )
    # Refused, never persisted: there is no such row for coverage to group.
    assert skill_registry_store.get_selection(repo, "TASK_PROSE_REASON") is None
    # And the launcher path still reports rather than raises.
    reported = skill_registry_store.record_selection_reported(
        repo,
        task_id="TASK_PROSE_REASON",
        packet={"version": 1, "skills": []},
        empty_reason="selection looked and did not care for the result",
    )
    assert reported["ok"] is False
    assert reported["reason"].startswith("skill_selection_receipt_not_recorded:")


def test_a_persisted_reason_this_build_cannot_name_counts_in_a_closed_bucket(
    tmp_path: Path,
) -> None:
    """A row an older build wrote never opens the histogram's key space."""
    assert skill_registry_store._coverage_empty_reason("") == (
        skill_registry_store.SELECTION_EMPTY_REASON_NOT_REPORTED
    )
    assert skill_registry_store._coverage_empty_reason(
        skill_registry.SELECTION_EMPTY_NO_VOCABULARY_MATCH
    ) == skill_registry.SELECTION_EMPTY_NO_VOCABULARY_MATCH
    assert skill_registry_store._coverage_empty_reason("nothing matched, sorry") == (
        skill_registry_store.SELECTION_EMPTY_REASON_UNRECOGNIZED
    )
    # Every key a live projection can emit stays inside the closed set.
    repo = _skills_repo(tmp_path, _prose_record())
    skill_registry_store.record_selection(
        repo,
        task_id="TASK_CLOSED_KEYS",
        packet={"version": 1, "skills": []},
        empty_reason=skill_registry.SELECTION_EMPTY_ACTIVE_VOCABULARY_UNSELECTABLE,
    )
    coverage = skill_registry_store.skill_coverage(repo)
    assert set(coverage["selection"]["empty_reasons"]) <= (
        skill_registry_store.PERSISTABLE_EMPTY_REASONS
        | {skill_registry_store.SELECTION_EMPTY_REASON_UNRECOGNIZED}
    )


def test_selection_empty_reason_refuses_a_drained_stream_instead_of_guessing() -> None:
    """A one-shot iterator cannot be read twice, so it is not read at all.

    ``select`` drains the stream to produce the receipt. Re-reading it here
    would find nothing and report ``no_candidates`` -- the earliest link in the
    chain -- for a registry that was full. That is the one wrong answer nobody
    could tell apart from a right one, so it fails loudly.
    """
    context = skill_registry.card_selection_context(_vocabulary_card())
    assert context is not None
    other_stage = skill_registry.validate_record(
        skill_registry.replace(_vocabulary_record(), stage="orientation")
    )
    stream = iter([other_stage])
    receipt = skill_registry.select(stream, context, limit=4)
    assert receipt.selected == ()
    with pytest.raises(skill_registry.SkillRegistryError) as excinfo:
        skill_registry.selection_empty_reason(stream, receipt)
    assert excinfo.value.code == "skill_registry.invalid_type"
    # The same candidates, materialized once, still answer exactly.
    assert skill_registry.selection_empty_reason([other_stage], receipt) == (
        skill_registry.SELECTION_EMPTY_NO_VOCABULARY_MATCH
    )


class _StreamOnlyRegistry:
    """A registry whose ``records()`` hands back a ONE-SHOT stream."""

    def __init__(self, records) -> None:
        self._records = list(records)

    def records(self):
        return iter(self._records)


def test_a_one_shot_registry_stream_is_materialized_before_it_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Three readers, one candidate set: select, packet, and the reason."""
    repo = _skills_repo(tmp_path, _prose_record())
    _stub_context_tools(monkeypatch)
    real_load = skill_registry_store.load_registry
    monkeypatch.setattr(
        skill_registry_store,
        "load_registry",
        lambda *args, **kwargs: _StreamOnlyRegistry(
            real_load(*args, **kwargs).records()
        ),
    )

    result = project_context.collect_project_context(repo, _vocabulary_card())
    assert result is not None
    # The store held exactly one ACTIVE-but-unreachable record. A second read of
    # the stream would have seen an empty registry and blamed the store for a
    # vocabulary failure the record itself carries.
    assert result.metadata["skill_selection"]["empty_reason"] == (
        skill_registry.SELECTION_EMPTY_ACTIVE_VOCABULARY_UNSELECTABLE
    )
    receipt = skill_registry_store.get_selection(repo, "TASK_SKILL_E2E")
    assert receipt is not None
    assert receipt["empty_reason"] == (
        skill_registry.SELECTION_EMPTY_ACTIVE_VOCABULARY_UNSELECTABLE
    )
    assert receipt["measured"] is True


# ---------------------------------------------------------------------------
# A present store that cannot be read is not a healthy empty one
# ---------------------------------------------------------------------------


def test_a_corrupt_skill_store_is_unreadable_rather_than_measured_zeros(
    tmp_path: Path,
) -> None:
    """The page reader's empty-on-error answer must not reach the projection."""
    repo = _skills_repo(tmp_path, _vocabulary_record())
    # Healthy first, so the only difference below is the corruption itself.
    healthy = skill_registry_store.skill_coverage(repo)
    assert healthy["measured"] is True
    assert healthy["skills"]["total"] == 1

    skill_registry_store._db_path(repo).write_bytes(b"not a sqlite database at all")

    coverage = skill_registry_store.skill_coverage(repo)
    assert coverage["measured"] is False
    assert coverage["unavailable_reason"].startswith("skill_store_unreadable:")
    # Not zeros, and not a truncated=False claim about a page never read.
    assert coverage["skills"] == {}
    assert coverage["selection"] == {}


def test_a_skill_store_missing_its_tables_is_unreadable_not_empty(
    tmp_path: Path,
) -> None:
    """A schema-incomplete store answers differently from an unfilled one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    path = skill_registry_store._db_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    coverage = skill_registry_store.skill_coverage(repo)
    assert coverage["measured"] is False
    assert coverage["unavailable_reason"].startswith("skill_store_unreadable:")
    assert coverage["skills"] == {}
    assert coverage["selection"] == {}
    # The three store states stay three answers: absent is its own reason, and
    # only a store that opened and answered is allowed to publish zeros.
    absent = skill_registry_store.skill_coverage(tmp_path / "nowhere")
    assert absent["unavailable_reason"] == "skill_store_absent"


# ---------------------------------------------------------------------------
# A selection that was attempted and failed says so
# ---------------------------------------------------------------------------


def test_a_failed_selection_is_a_named_unmeasured_block_not_a_missing_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A store failure must not read like a card that declared no vocabulary."""
    repo = _skills_repo(tmp_path, _vocabulary_record())
    _stub_context_tools(monkeypatch)

    def boom(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(skill_registry_store, "load_registry", boom)
    metadata = project_context.collect_project_context(
        repo, _vocabulary_card()
    ).metadata
    block = metadata["skill_selection"]
    assert block["measured"] is False
    assert block["failure_reason"] == "skill_selection_failed:OperationalError"
    # None, never 0: a failed selection reporting zeros would be identical to a
    # selection that ran and legitimately injected nothing.
    assert block["selected_count"] is None
    assert block["injected_count"] is None
    assert block["empty_reason"] == ""


def test_a_rejected_vocabulary_still_reports_that_selection_was_attempted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Attempted-and-rejected and never-attempted are different facts."""
    repo = _skills_repo(tmp_path, _vocabulary_record())
    _stub_context_tools(monkeypatch)

    card = _vocabulary_card(skill_triggers=["a trigger this build does not know"])
    block = project_context.collect_project_context(repo, card).metadata[
        "skill_selection"
    ]
    assert block["measured"] is False
    assert block["failure_reason"].startswith("skill_vocabulary_rejected:")
    assert block["selected_count"] is None
    assert block["injected_count"] is None

    # A card that declared NO vocabulary at all still omits the block entirely.
    # That absence is only readable because the failure above stopped using it.
    bare = _vocabulary_card()
    for key in ("skill_triggers", "skill_applicability"):
        bare.pop(key)
    assert "skill_selection" not in (
        project_context.collect_project_context(repo, bare).metadata
    )
