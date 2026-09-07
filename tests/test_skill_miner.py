"""Tests for the correction-record miner (RM-2026-00021 layers 2, 3, 4, 6).

The contract under test is not "it produces output". It is:

* clustering keys on the RULE and not on the file, so the same rule stated
  about two different modules lands in one family and two different rules
  stated about one module do not;
* the recurrence gate refuses one card corrected many times, which is the
  dominant shape in this repository's real ledger and the single easiest way
  to manufacture false recurrence;
* the pass produces PROPOSALS and can never activate anything;
* the retirement report never fabricates the injection denominator it does not
  have;
* and the whole thing is reachable from a registered MCP tool.

Every fixture writes into a real, isolated task store; nothing here asserts
against a local model of the schema.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3

import pytest

import aiworkhub.core as core
import aiworkhub.skill_registry as sr
from aiworkhub import learning_commit_store, manager_skill_tools as mst, skill_miner
from aiworkhub import task_store


# ---------------------------------------------------------------------------
# Fixtures: a real isolated task store seeded with real card / ledger shapes
# ---------------------------------------------------------------------------


def _db(root):
    return root / ".aiworkhub" / "tasking" / "task_queue.sqlite"


def _connect(root):
    task_store.initialize_repository(root)
    conn = sqlite3.connect(str(_db(root)))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS learning_commits(
            commit_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE NOT NULL,
            task_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            repository_id TEXT NOT NULL,
            repo_area TEXT NOT NULL,
            outcome TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            projections_json TEXT NOT NULL,
            state TEXT NOT NULL,
            manager_id TEXT NOT NULL,
            manager_provider TEXT NOT NULL,
            provenance TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(task_id, request_id)
        );
        """
    )
    return conn


def seed_card(root, task_id, *, runner="claude", paths=(), instruction=""):
    """Write one card carrying a real ``review_feedback`` rejection shape."""
    card = {
        "task_id": task_id,
        "runner": runner,
        "allowed_writes": list(paths),
    }
    if instruction:
        card["review_feedback"] = {
            "schema_id": "aiworkhub.rework_feedback_delta.v1",
            "instruction": instruction,
            "predecessor_request_id": f"req-{task_id}",
        }
    conn = _connect(root)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tasks (task_id,runner,topic,mode,status,worker_status,"
            "priority,objective,card_json,created_at,updated_at,claimed_by,claimed_at,"
            "started_at,completed_at,origin_thread_id,archived_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, runner, "code", "", "done", "done", "normal", "objective",
                json.dumps(card), "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:10:00+00:00", runner, "2026-01-01T00:01:00+00:00",
                "2026-01-01T00:01:00+00:00", "2026-01-01T00:10:00+00:00", "t", "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def seed_commit(root, task_id, request_id, *, invariant="", outcome="rejected",
                area="src/aiworkhub", failure_category="candidate_code"):
    payload = {
        "invariant_candidate": invariant,
        "lesson_candidate": "",
        "failure_category": failure_category,
    }
    conn = _connect(root)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO learning_commits(commit_id,idempotency_key,task_id,"
            "request_id,repository_id,repo_area,outcome,payload_json,payload_sha256,"
            "projections_json,state,manager_id,manager_provider,provenance,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{task_id}:{request_id}", f"{task_id}:{request_id}", task_id,
                request_id, "repo", area, outcome, json.dumps(payload), "sha",
                "{}", "completed", "m", "claude", "test",
                f"2026-01-0{len(request_id) % 9 + 1}T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def manager(tmp_path, monkeypatch):
    route = {
        "role": "manager",
        "provider": "claude",
        "repo": str(tmp_path),
        "manager_route": {"thread_id": "sess-1", "provider": "claude"},
    }
    monkeypatch.setattr(core, "manager_bootstrap", lambda: route)
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return tmp_path


def _doc(task_id, text, *, paths=(), actor="w1", kind="review_feedback.instruction"):
    terms = skill_miner.rule_terms(text, [p for p in paths])
    return skill_miner.CorrectionDocument(
        kind=kind, task_id=task_id, anchor=f"req-{task_id}", text=text,
        paths=tuple(paths), actor=actor, terms=terms,
    )


# ---------------------------------------------------------------------------
# Layer 2: clustering keys on the RULE, not on the file
# ---------------------------------------------------------------------------


def test_rule_terms_delete_the_instance_and_keep_the_rule():
    """The same rule about two different modules reduces to the same vocabulary.

    This is the whole mechanism that makes the output a rule family rather than
    a directory listing. If the path/identifier strippers are removed, the two
    residues diverge and this fails.
    """
    left = skill_miner.rule_terms(
        "A field added to src/aiworkhub/process_launcher.py is not delivered "
        "until it appears in the allowlist of every surface a reader consults.",
        ["src/aiworkhub/process_launcher.py"],
    )
    right = skill_miner.rule_terms(
        "A field added to src/aiworkhub/worker_workspace.py is not delivered "
        "until it appears in the allowlist of every surface a reader consults.",
        ["src/aiworkhub/worker_workspace.py"],
    )
    assert left == right
    # And what survived is the assertion, not the subject.
    assert {"field", "deliver", "allowlist", "surface", "reader"} <= left
    assert not any("launcher" in term or "workspace" in term for term in left)


def test_rule_terms_remove_the_cards_own_write_set():
    """A card can never match another merely by the module it was about."""
    text = "The gate must consult platform before it decides."
    with_scope = skill_miner.rule_terms(text, ["src/aiworkhub/platform_io.py"])
    without = skill_miner.rule_terms(text, [])
    assert "platform" in without
    assert "platform" not in with_scope


def test_rule_terms_delete_bare_identifiers_the_write_set_never_named():
    """A symbol mentioned only in prose is still an instance, and still goes.

    The write-set blocker cannot see ``commit.payload_json`` when no card path
    contains it, so the dotted-identifier stripper is the only thing standing
    between a rule family and the symbol one statement happened to cite.
    """
    cited = skill_miner.rule_terms(
        "The reader must consult commit.payload_json before the gate decides."
    )
    plain = skill_miner.rule_terms(
        "The reader must consult the payload before the gate decides."
    )
    assert cited == {"reader", "consult", "gate", "decid"}
    assert "payload" not in cited
    # The control shows the term is only absent because it was an identifier.
    assert "payload" in plain


def test_two_rules_about_one_file_do_not_become_one_family():
    """Same file, different rules: clustering must keep them apart."""
    docs = [
        _doc("c1", "A digest may only be used as an integrity check over exactly "
                   "the bytes it hashes and nothing beyond that coverage.",
             paths=["src/aiworkhub/store.py"]),
        _doc("c2", "Retention must be decided by the state of the thing retained "
                   "and every reader of it enumerated before release.",
             paths=["src/aiworkhub/store.py"]),
    ]
    clusters = skill_miner.cluster_by_rule(docs, threshold=0.22)
    assert len(clusters) == 2


def test_leader_clustering_does_not_chain_two_families_together():
    """A resembles B and B resembles C must not merge A with C.

    Single-link agglomeration does exactly that, and on the real record it
    collapsed 669 statements into one 207-member blob. Leader clustering scores
    against the family representative only.
    """
    bridge = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    docs = [
        _doc("c1", "alpha beta gamma delta epsilon zeta eta theta iota kappa"),
        _doc("c2", bridge + " lambda mu nu xi omicron pi rho sigma tau upsilon"),
        _doc("c3", "lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi"),
    ]
    clusters = skill_miner.cluster_by_rule(docs, threshold=0.30)
    # c1 opens a family and c2 joins it; c3 shares nothing with c1, the
    # representative, so it opens its own rather than chaining through c2.
    assert len(clusters) == 2
    assert [len(c.members) for c in clusters] == [2, 1]


def test_indexed_clustering_reproduces_the_pairwise_score_exactly():
    """The inverted index is an optimisation, never a different answer."""
    docs = [
        _doc(f"c{i}", f"the reader must enumerate every branch site {i} that emits "
                      f"a field before the guarantee holds across surfaces")
        for i in range(12)
    ] + [
        _doc(f"d{i}", f"a digest covers exactly the bytes it hashes {i} and never "
                      f"the persisted state stored beside them in one row")
        for i in range(12)
    ]
    idf = skill_miner.inverse_document_frequency(docs)
    indexed = skill_miner.cluster_by_rule(docs, threshold=0.25, idf=idf)

    leaders: list[list] = []
    for doc in docs:
        best, best_score = -1, 0.0
        for index, members in enumerate(leaders):
            score = skill_miner.similarity(doc, members[0], idf)
            if score >= 0.25 and score > best_score:
                best, best_score = index, score
        if best < 0:
            leaders.append([doc])
        else:
            leaders[best].append(doc)

    assert [[m.task_id for m in c.members] for c in indexed] == [
        [m.task_id for m in group] for group in leaders
    ]


# ---------------------------------------------------------------------------
# Layer 3: the recurrence gate, and what it refuses
# ---------------------------------------------------------------------------


RULE = ("the candidate must enumerate every emitting branch before the field "
        "guarantee is treated as delivered to a reader")


def test_gate_refuses_one_card_corrected_many_times():
    """Seven statements from ONE card are one incident, never a pattern.

    This is the shape that dominates the real ledger: 49 commits over 28 cards,
    one of which alone produced seven differently-worded invariants about a
    single afternoon. Counting statements instead of distinct cards would
    promote it, and the skill set would be folklore.
    """
    docs = [
        _doc("one-hard-card", f"{RULE} in round {i}", paths=[f"src/m{i}.py"])
        for i in range(7)
    ]
    clusters = skill_miner.cluster_by_rule(docs, threshold=0.22)
    decisions = skill_miner.apply_recurrence_gate(clusters)
    assert [d.promoted for d in decisions] == [False]
    assert decisions[0].reason == "single_incident"
    # Seven statements, one card: the statement count never substitutes.
    assert len(decisions[0].cluster.members) == 7
    assert decisions[0].distinct_cards == 1


def test_gate_refuses_recurrence_confined_to_too_few_files():
    docs = [
        _doc(f"card{i}", f"{RULE} seen again {i}", paths=["src/only_here.py"])
        for i in range(4)
    ]
    decisions = skill_miner.apply_recurrence_gate(
        skill_miner.cluster_by_rule(docs, threshold=0.22)
    )
    assert decisions[0].promoted is False
    assert decisions[0].reason == "below_file_floor"


def test_gate_refuses_two_cards_as_below_the_card_floor():
    docs = [
        _doc(f"card{i}", f"{RULE} again {i}", paths=[f"src/a{i}.py", f"src/b{i}.py"])
        for i in range(2)
    ]
    decisions = skill_miner.apply_recurrence_gate(
        skill_miner.cluster_by_rule(docs, threshold=0.22)
    )
    assert decisions[0].promoted is False
    assert decisions[0].reason == "below_card_floor"


def test_gate_promotes_three_distinct_cards_in_three_distinct_files():
    docs = [
        _doc(f"card{i}", f"{RULE} once more {i}", paths=[f"src/mod{i}.py"],
             actor=f"worker{i}")
        for i in range(3)
    ]
    decisions = skill_miner.apply_recurrence_gate(
        skill_miner.cluster_by_rule(docs, threshold=0.22)
    )
    assert [d.promoted for d in decisions] == [True]
    assert decisions[0].distinct_cards == 3
    assert decisions[0].distinct_files == 3


def test_actor_independence_is_reported_and_warned_not_enforced():
    """NF-2026-00312 says "ideally by distinct workers" -- ideally is not a gate."""
    docs = [
        _doc(f"card{i}", f"{RULE} yet again {i}", paths=[f"src/mod{i}.py"],
             actor="one-worker")
        for i in range(3)
    ]
    idf = skill_miner.inverse_document_frequency(docs)
    decisions = skill_miner.apply_recurrence_gate(
        skill_miner.cluster_by_rule(docs, threshold=0.22, idf=idf)
    )
    assert decisions[0].promoted is True
    assert decisions[0].distinct_actors == 1
    candidate = skill_miner._candidate(decisions[0], idf)
    assert any(w.startswith("weak_actor_independence") for w in candidate["warnings"])


# ---------------------------------------------------------------------------
# Layer 4: provenance, and the refusal to guess altitude
# ---------------------------------------------------------------------------


def test_candidate_carries_the_exact_cards_and_requests_it_was_learned_from():
    docs = [
        _doc(f"card{i}", f"{RULE} recorded {i}", paths=[f"src/mod{i}.py"],
             actor=f"worker{i}")
        for i in range(3)
    ]
    idf = skill_miner.inverse_document_frequency(docs)
    decision = skill_miner.apply_recurrence_gate(
        skill_miner.cluster_by_rule(docs, threshold=0.22, idf=idf)
    )[0]
    candidate = skill_miner._candidate(decision, idf)
    assert candidate["provenance"]["card_ids"] == ["card0", "card1", "card2"]
    assert candidate["provenance"]["request_ids"] == [
        "req-card0", "req-card1", "req-card2"
    ]
    assert len(candidate["statements"]) == 3
    # A skill without provenance cannot be audited, argued with, or retired.
    assert all(s["task_id"] and s["request_id"] for s in candidate["statements"])


def test_draft_leaves_the_closed_selection_vocabulary_to_the_manager():
    """Guessing a trigger token from prose is how a skill set drifts to instances."""
    docs = [
        _doc(f"card{i}", f"{RULE} drafted {i}", paths=[f"src/aiworkhub/m{i}.py"])
        for i in range(3)
    ]
    idf = skill_miner.inverse_document_frequency(docs)
    decision = skill_miner.apply_recurrence_gate(
        skill_miner.cluster_by_rule(docs, threshold=0.22, idf=idf)
    )[0]
    draft = skill_miner._candidate(decision, idf)
    assert draft["proposal_draft"]["triggers"] == []
    assert draft["proposal_draft"]["applicability"] == []
    assert "triggers" in draft["draft_incomplete"]
    assert "applicability" in draft["draft_incomplete"]
    assert draft["next_action"] == "aiworkhub_manager_skill_propose"


def test_candidate_identity_is_stable_and_registry_legal():
    docs = [
        _doc(f"card{i}", f"{RULE} stable {i}", paths=[f"src/mod{i}.py"])
        for i in range(3)
    ]
    idf = skill_miner.inverse_document_frequency(docs)
    decision = skill_miner.apply_recurrence_gate(
        skill_miner.cluster_by_rule(docs, threshold=0.22, idf=idf)
    )[0]
    first = skill_miner._candidate(decision, idf)["candidate_id"]
    second = skill_miner._candidate(decision, idf)["candidate_id"]
    assert first == second
    assert first.startswith("mined.")
    # Legality is proved through the registry's own public constructor, not by
    # re-asserting a copy of its private identity pattern here.
    record = sr.SkillRecord.from_mapping({
        "identity": first, "version": "0.1.0", "scope": "repository",
        "task_family": "bugfix", "path_or_symbol": "src/aiworkhub",
        "risk": "medium", "stage": "review", "triggers": ["t"], "confidence": 0.5,
    })
    assert record.identity == first


def test_candidate_identity_survives_a_family_whose_terms_are_all_punctuation():
    """A stem that filters to nothing must still yield a legal identity."""
    identity = skill_miner._candidate_identity(["...", "---"], ["card0"])
    assert identity.startswith("mined.rule.")
    sr.SkillRecord.from_mapping({
        "identity": identity, "version": "0.1.0", "scope": "repository",
        "task_family": "bugfix", "path_or_symbol": "src/aiworkhub",
        "risk": "medium", "stage": "review", "triggers": ["t"], "confidence": 0.5,
    })


# ---------------------------------------------------------------------------
# Authority: proposals only, never an activation
# ---------------------------------------------------------------------------


def test_mine_produces_proposals_and_can_never_activate(manager):
    for index in range(3):
        seed_card(
            manager, f"card{index}", runner=f"worker{index}",
            paths=[f"src/aiworkhub/mod{index}.py"],
            instruction=f"{RULE} observed in round {index}",
        )
    report = skill_miner.mine(manager)
    assert report["authority"] == {
        "produces": "proposals_only",
        "writes": "none",
        "activation": "manager_gated_two_distinct_actor_identities",
    }
    blob = json.dumps(report)
    assert '"active"' not in blob and '"retired"' not in blob
    for candidate in report["candidates"]:
        assert "lifecycle_state" not in candidate["proposal_draft"]
        assert "evidence" not in candidate["proposal_draft"]


def test_the_miner_never_opens_a_store_for_writing():
    source = inspect.getsource(skill_miner)
    assert "put_record" not in source
    assert "advance_record" not in source
    assert "sqlite3.connect" not in source
    for banned in ("registry.activate", ".propose(", "load_registry"):
        assert banned not in source


def test_mine_does_not_modify_the_task_store(manager):
    seed_card(manager, "card0", paths=["src/a.py"], instruction=RULE + " zero")
    seed_card(manager, "card1", paths=["src/b.py"], instruction=RULE + " one")
    before = _db(manager).read_bytes()
    skill_miner.mine(manager)
    assert _db(manager).read_bytes() == before


def test_mine_is_deterministic_across_repeated_calls(manager):
    for index in range(3):
        seed_card(
            manager, f"card{index}", runner=f"worker{index}",
            paths=[f"src/aiworkhub/mod{index}.py"],
            instruction=f"{RULE} observed in round {index}",
        )
    first = skill_miner.mine(manager)
    second = skill_miner.mine(manager)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_mine_reads_both_correction_sources(manager):
    seed_card(manager, "card0", paths=["src/a.py"], instruction=RULE + " from feedback")
    seed_commit(manager, "card0", "req0", invariant=RULE + " from the ledger")
    docs = skill_miner.load_correction_record(manager)
    kinds = {d.kind for d in docs}
    assert "review_feedback.instruction" in kinds
    assert "learning_commit.invariant_candidate" in kinds


def test_mine_on_an_empty_repository_yields_no_candidates(manager):
    report = skill_miner.mine(manager)
    assert report["candidates"] == []
    assert report["corpus"]["documents"] == 0


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5, True, "0.2"])
def test_threshold_is_validated_fail_closed(bad):
    with pytest.raises(skill_miner.SkillMinerError):
        skill_miner.cluster_by_rule([], threshold=bad)


# ---------------------------------------------------------------------------
# Layer 6: measure and retire, without fabricating a denominator
# ---------------------------------------------------------------------------


def test_retirement_reports_the_injection_denominator_as_unavailable(manager):
    seed_card(manager, "card0", paths=["src/a.py"])
    state = learning_commit_store.injection_ledger_state(manager)
    assert state["state"] == "unavailable"
    assert state["injected_cards"] == 0
    assert state["cards_with_persisted_packet"] == 0
    assert "no_card_persists_a_skill_packet" in state["reason"]


def test_retirement_never_recommends_removal_without_enough_anchors(manager):
    import aiworkhub.skill_registry as sr
    from aiworkhub import skill_registry_store as store

    record = sr.SkillRecord.from_mapping({
        "identity": "some-rule", "version": "1.0.0", "scope": "repository",
        "task_family": "bugfix", "path_or_symbol": "src/aiworkhub",
        "risk": "medium", "stage": "review", "triggers": ["t"], "confidence": 0.5,
    })
    store.put_record(manager, record)
    report = skill_miner.measure_retirement(manager)
    assert report["authority"]["writes"] == "none"
    assert report["authority"]["retirement"] == "manager_gated"
    entry = report["skills"][0]
    assert entry["verdict"] == "insufficient_evidence"
    assert entry["injected_cards"] == 0
    assert "retire" != entry["verdict"]


def test_retirement_measures_a_skill_on_its_own_anchor_cards(manager):
    import aiworkhub.skill_registry as sr
    from aiworkhub import skill_registry_store as store

    for index in range(3):
        seed_card(manager, f"anchor{index}", paths=[f"src/a{index}.py"])
        seed_commit(manager, f"anchor{index}", f"r{index}", invariant="x",
                    outcome="rejected")
    record = sr.SkillRecord.from_mapping({
        "identity": "anchored-rule", "version": "1.0.0", "scope": "repository",
        "task_family": "bugfix", "path_or_symbol": "src/aiworkhub",
        "risk": "medium", "stage": "review", "triggers": ["t"], "confidence": 0.5,
        "evidence": [
            {"source": f"anchor{i}", "outcome": "accepted", "authority": "worker",
             "actor_id": f"worker.w{i}"}
            for i in range(3)
        ],
        "accepted_count": 3,
    })
    store.put_record(manager, record)
    entry = skill_miner.measure_retirement(manager)["skills"][0]
    assert entry["resolvable_anchor_cards"] == 3
    assert entry["anchor_outcomes"] == {"rejected": 3}
    # It did not reduce its own failure class on the cards it was judged on.
    assert entry["verdict"] == "review_for_retirement"


# ---------------------------------------------------------------------------
# The call path: reachable from a real, registered manager surface
# ---------------------------------------------------------------------------


def test_manager_mine_requires_a_verified_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {"role": "worker"})
    result = mst.mine()
    assert result["ok"] is False
    assert result["error"] == "verified_manager_identity_required"


def test_manager_retirement_report_requires_a_verified_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {"role": "worker"})
    result = mst.retirement_report()
    assert result["ok"] is False
    assert result["error"] == "verified_manager_identity_required"


def test_manager_mine_returns_a_bound_receipt(manager):
    seed_card(manager, "card0", paths=["src/a.py"], instruction=RULE + " once")
    result = mst.mine()
    assert result["ok"] is True
    assert result["schema_id"] == skill_miner.SCHEMA_ID
    assert result["surface"] == "manager_mcp"
    assert result["manager"]["repo"] == str(manager)


def test_both_mining_tools_are_registered_on_the_mcp_server():
    """The miner must be callable from a real surface, not just importable."""
    import aiworkhub.server as server

    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert "aiworkhub_manager_skill_mine" in names
    assert "aiworkhub_manager_skill_retirement_report" in names
    # And the existing hand-off it feeds is still there.
    assert "aiworkhub_manager_skill_propose" in names
