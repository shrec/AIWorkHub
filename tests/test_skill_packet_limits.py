"""NF-2026-01416: one over-cap skill record must not zero out an entire
runtime packet, and a record that could never be emitted is rejected at
``propose``/``adopt`` instead of silently failing selection later.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import aiworkhub.project_context as project_context
import aiworkhub.skill_registry as sr
import aiworkhub.skill_registry_store as store

WORKER = sr.Authority(sr.AuthorityRole.WORKER, actor_id="worker-1", token="worker-token")

_BASE = {
    "version": "1.0.0",
    "scope": "repository",
    "task_family": "t",
    "path_or_symbol": "p",
    "risk": "low",
    "stage": "s",
    "triggers": (),
    "applicability": (),
    "procedure_steps": ("do the thing",),
    "avoid_rules": (),
    "preferred_tools": (),
    "confidence": 0.9,
}


def _proposed(identity, **overrides):
    data = dict(_BASE, identity=identity, lifecycle_state="proposed")
    data.update(overrides)
    return sr.SkillRecord.from_mapping(data)


def _active(identity, **overrides):
    data = dict(_BASE, identity=identity, lifecycle_state="active")
    data.update(overrides)
    return sr.SkillRecord.from_mapping(data)


def _ctx(**overrides):
    base = dict(
        task_family="t", path_or_symbol="p", risk="low", stage="s", triggers=(), applicability=()
    )
    base.update(overrides)
    return base


def assert_fails(code, fn):
    with pytest.raises(sr.SkillRegistryError) as excinfo:
        fn()
    assert excinfo.value.code == code


# ---------------------------------------------------------------------------
# propose()/adopt() reject a record the packet could never emit
# ---------------------------------------------------------------------------

_UNEMITTABLE_OVERRIDES = [
    pytest.param({"procedure_steps": ("x" * 339,)}, id="over_string_bytes"),
    pytest.param(
        {"avoid_rules": tuple(f"rule-{i}" for i in range(17))}, id="over_list_items"
    ),
    pytest.param(
        {
            "applicability": tuple("a" * 250 for _ in range(16)),
            "procedure_steps": tuple("a" * 250 for _ in range(16)),
            "avoid_rules": tuple("a" * 250 for _ in range(16)),
            "preferred_tools": tuple("a" * 250 for _ in range(16)),
        },
        id="over_packet_bytes",
    ),
]


@pytest.mark.parametrize("overrides", _UNEMITTABLE_OVERRIDES)
def test_propose_rejects_a_record_the_packet_can_never_emit(overrides):
    registry = sr.SkillRegistry()
    record = _proposed("unemittable", **overrides)
    assert_fails("skill_registry.packet_limit", lambda: registry.propose(record, WORKER))


@pytest.mark.parametrize("overrides", _UNEMITTABLE_OVERRIDES)
def test_adopt_rejects_a_record_the_packet_can_never_emit(overrides):
    registry = sr.SkillRegistry()
    record = _active("unemittable", **overrides)
    assert_fails("skill_registry.packet_limit", lambda: registry.adopt(record))


def test_validate_record_does_not_enforce_packet_bounds():
    record = _active("still-valid-by-validate-record", procedure_steps=("x" * 339,))
    assert sr.validate_record(record).identity == "still-valid-by-validate-record"


def test_load_registry_still_loads_an_existing_overcap_record(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    record = _active("legacy-overcap", procedure_steps=("x" * 339,))
    store.put_record(repo, record)

    registry = store.load_registry(repo)

    assert registry.get(record.identity, record.version) is not None


# ---------------------------------------------------------------------------
# _emit_runtime_packet excludes the offending row instead of raising
# ---------------------------------------------------------------------------


def test_one_overcap_record_is_excluded_and_the_other_survives():
    good = _active("good-skill")
    bad = _active("bad-skill", procedure_steps=("x" * 339,))
    receipt = sr.select([good, bad], _ctx(), limit=2)
    assert {item.identity for item in receipt.selected} == {"good-skill", "bad-skill"}

    packet = sr.build_runtime_packet([good, bad], receipt)

    assert [row.identity for row in packet.skills] == ["good-skill"]
    assert packet.excluded == (("bad-skill", "1.0.0", "packet_limit:procedure_steps"),)


def test_packet_bytes_cap_drops_lowest_ranked_rows_first():
    records = [_active(f"skill-{i}", procedure_steps=("step " * 20,)) for i in range(5)]
    receipt = sr.select(records, _ctx(), limit=5)
    assert len(receipt.selected) == 5

    full_packet = sr.build_runtime_packet(records, receipt)
    assert full_packet.excluded == ()
    full_bytes = len(sr.canonical_json(full_packet.as_mapping()).encode("utf-8"))

    capped = sr.build_runtime_packet(records, receipt, max_packet_bytes=full_bytes * 3 // 5)

    rank_order = [item.identity for item in receipt.selected]
    kept = len(capped.skills)
    assert 0 < kept < len(records)
    assert [row.identity for row in capped.skills] == rank_order[:kept]
    assert capped.excluded == tuple(
        (identity, "1.0.0", "packet_limit:packet_bytes") for identity in reversed(rank_order[kept:])
    )


def test_select_limit_and_reject_instruction_string_still_raise():
    good = _active("good-skill")
    bad = _active("bad-skill")
    receipt = sr.select([good, bad], _ctx(), limit=2)
    assert_fails(
        "skill_registry.packet_limit",
        lambda: sr.build_runtime_packet([good, bad], receipt, max_selected=1),
    )

    secret = _active("secret-skill", procedure_steps=("Authorization: Bearer leakedtoken",))
    secret_receipt = sr.select([secret], _ctx(), limit=1)
    assert_fails(
        "skill_registry.secret_rejected",
        lambda: sr.build_runtime_packet([secret], secret_receipt),
    )


# ---------------------------------------------------------------------------
# _skills_section surfaces the exclusion through the store and the section
# ---------------------------------------------------------------------------

_VOCAB_BASE = {
    "version": "1.0.0",
    "scope": "repository",
    "task_family": "bugfix",
    "path_or_symbol": "src/aiworkhub/*",
    "risk": "high",
    "stage": "review",
    "triggers": ("unknown_or_empty_result",),
    "applicability": ("observability_surface",),
    "procedure_steps": ("Read the diff before editing.",),
    "avoid_rules": (),
    "preferred_tools": (),
    "confidence": 0.9,
    "lifecycle_state": "active",
    "evidence": (
        sr.EvidenceRecord(
            source="commit-0",
            outcome=sr.EvidenceOutcome.ACCEPTED,
            authority=sr.AuthorityRole.MANAGER,
            actor_id="manager.claude.alpha",
        ),
        sr.EvidenceRecord(
            source="commit-1",
            outcome=sr.EvidenceOutcome.ACCEPTED,
            authority=sr.AuthorityRole.MANAGER,
            actor_id="manager.claude.beta",
        ),
    ),
    "accepted_count": 2,
}


def _vocab_record(identity, **overrides):
    data = dict(_VOCAB_BASE, identity=identity)
    data.update(overrides)
    return sr.SkillRecord.from_mapping(data)


def _vocab_card(**overrides):
    card = {
        "task_id": "TASK_SKILL_PACKET_LIMIT",
        "allowed_writes": [
            "src/aiworkhub/core.py",
            "src/aiworkhub/project_context.py",
        ],
        "risk_tier": "high",
        "skill_task_family": "bugfix",
        "skill_stage": "review",
        "skill_triggers": ["unknown_or_empty_result"],
        "skill_applicability": ["observability_surface"],
    }
    card.update(overrides)
    return card


def test_skills_section_over_tmp_store_surfaces_the_exclusion(tmp_path):
    good = _vocab_record("good-skill")
    bad = _vocab_record("bad-skill", procedure_steps=("x" * 339,))
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    store.put_record(repo, good)
    store.put_record(repo, bad)
    card = _vocab_card()

    section = project_context._skills_section(repo, card)

    assert section is not None
    assert section["truncated"] is True
    assert section["excluded"] == (("bad-skill", "1.0.0", "packet_limit:procedure_steps"),)
    skills = json.loads(section["content"])["skills"]
    assert [row["identity"] for row in skills] == ["good-skill"]

    metadata = project_context._skill_selection_metadata(section)
    assert metadata["measured"] is True
    assert metadata["excluded"] == section["excluded"]

    receipt = store.get_selection(repo, str(card["task_id"]))
    assert receipt is not None
    assert receipt["excluded"] == [["bad-skill", "1.0.0", "packet_limit:procedure_steps"]]


def test_an_old_receipt_row_without_the_excluded_column_still_reads(tmp_path):
    repo = tmp_path / "repo"
    path = store._db_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(store._SCHEMA_SQL)
        conn.execute(
            "INSERT INTO skill_selection_receipts "
            "(task_id,request_id,packet_sha256,selected_json,context_json,created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("LEGACY", "", "deadbeef", "[]", "{}", "2020-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    result = store.get_selection(repo, "LEGACY")

    assert result is not None
    assert result["excluded"] == []
