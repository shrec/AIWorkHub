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
    assert result.metadata["optimization"]["zero_hit_suppression_count"] == 1


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
