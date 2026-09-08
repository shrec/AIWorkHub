"""Read-only mining pass that clusters the correction record BY RULE.

This is layer two of RM-2026-00021 / NF-2026-00312. Capture already exists:
every manager rejection stores its exact instruction on the card, and every
adjudicated decision stores an ``invariant_candidate`` and ``lesson_candidate``
in the learning-commit ledger. Storage exists. Selection into the worker bundle
exists. Nothing *mined*, so every skill had to be hand-written, and the
repository held exactly one.

What this module does, and what it deliberately refuses to do
-------------------------------------------------------------
It reads the correction record, groups it into rule families, applies a
recurrence gate, and emits **proposals**. It never writes, never activates, and
never promotes. Activation stays where the registry already put it: behind a
verified manager and evidence from at least two distinct actor identities.

Clustering by RULE and not by FILE
----------------------------------
The naive grouping of a correction record is by file or by card, and it is
worthless: it re-discovers the directory tree. The rule is the thing that
recurs, and the rule is what survives when the instance is removed.

So before any similarity is computed, :func:`rule_terms` deletes every token
that names an instance -- file paths, dotted/snake/camel identifiers, SHOUTY
card and constant ids, digits, quoted literals, backticked code spans, and
every word appearing in the source card's own write set. A statement about
``process_launcher.py`` and a statement about ``worker_workspace.py`` reduce to
the same residue when they assert the same rule, and to different residues when
they do not. That residue is the principle vocabulary, and similarity is
computed over it alone. This is the mechanism that makes the output a rule
family rather than a directory listing.

Why leader clustering and not single-link
-----------------------------------------
Single-link agglomeration chains: A resembles B, B resembles C, and a corpus
of 669 correction statements collapses into one 207-member blob that means
nothing. Measured on this repository's own record at a 0.14 threshold, that is
exactly what happened. Leader clustering admits a statement only when it
matches a family's REPRESENTATIVE, so a family stays as coherent as the
statement that opened it. Documents are processed in a fixed order, so the
representatives -- and therefore the whole partition -- are deterministic.

The recurrence gate, and what it refuses
----------------------------------------
NF-2026-00312: below three distinct cards in distinct files, a correction is an
incident, not a pattern. The gate exists to stop the skill set becoming
folklore, and on this repository it does most of its work against ONE shape:
a single hard card rejected many times. The learning ledger holds 49 commits
drawn from only 28 distinct cards; one card alone contributed seven, each with
a differently-worded invariant about the same afternoon's work. Counting those
as seven independent confirmations would manufacture recurrence out of a single
incident, so the floor counts DISTINCT CARDS, never statements.

Mine principles, not instances
------------------------------
The clustering works at rule level, but the altitude of the final wording is
not a decision this module can make and it does not pretend to. It hands the
manager a family, its shared rule vocabulary, its full provenance, and a
proposal draft whose closed-vocabulary selection dimensions are left EMPTY and
named in ``draft_incomplete``. Guessing a trigger token from prose is how an
instance-level skill set gets built; the manager supplies those tokens or the
proposal does not go in.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import learning_commit_store, skill_registry, skill_registry_store

SCHEMA_ID = "aiworkhub.skill_miner.v1"
RETIREMENT_SCHEMA_ID = "aiworkhub.skill_retirement.v1"

# Measured on this repository's own correction record (669 documents, 585
# cards): the similarity threshold decides how many families clear the gate --
# 9 at 0.18, 5 at 0.22, 2 at 0.26, 1 at 0.30. 0.22 is the default because it is
# the highest threshold at which every surviving family was still judged a
# principle rather than one card's paraphrase, and the sensitivity curve is
# returned with every result so the caller can see the knob rather than trust it.
DEFAULT_THRESHOLD = 0.22
SENSITIVITY_THRESHOLDS = (0.18, 0.22, 0.26, 0.30)

# NF-2026-00312: "at least three distinct cards, in distinct files, ideally by
# distinct workers". The first two are floors. Actor independence is REPORTED
# and warned on, not enforced, because "ideally" is not a gate -- a real rule
# found twice by one worker and once by another is still a real rule.
MIN_DISTINCT_CARDS = 3
MIN_DISTINCT_FILES = 3
ADVISORY_MIN_DISTINCT_ACTORS = 2

# A statement shorter than this carries no rule; a corpus entry with fewer
# residual terms than this is indistinguishable from noise once its instance
# vocabulary is removed.
MIN_STATEMENT_CHARS = 40
MIN_RULE_TERMS = 8

MAX_CORPUS_DOCUMENTS = 5000
MAX_CANDIDATES = 40
MAX_EXCERPT_CHARS = 400

# Retirement will not recommend removing a skill on fewer resolvable anchor
# cards than this. Fail-closed: too little evidence yields "insufficient",
# never "retire".
MIN_RETIREMENT_ANCHORS = 3


class SkillMinerError(RuntimeError):
    """A fail-closed mining refusal (bad parameters or unreadable record)."""


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WORD_RE = re.compile(r"[A-Za-z]+")
_CODE_SPAN_RE = re.compile(r"`[^`]*`")
_QUOTED_RE = re.compile(r"\"[^\"]*\"")
_FILE_PATH_RE = re.compile(
    r"\b[\w./\\-]+\.(?:py|js|ts|json|toml|md|sqlite|yml|yaml|txt|cfg|ini|sh|ps1)\b"
)
_DOTTED_IDENT_RE = re.compile(r"\b\w+(?:[._][A-Za-z0-9]\w*)+\b")
_CAMEL_RE = re.compile(r"\b[a-z]+[A-Z]\w*\b")
_SHOUTY_RE = re.compile(r"\b[A-Z]{2,}[\w-]*\b")
_DIGIT_RE = re.compile(r"\d+")

# Function words plus the modal scaffolding every rule statement shares. These
# are removed because they are present in EVERY statement and so carry no
# discriminating signal, not because they are unimportant to the rule's meaning.
_STOPWORDS = frozenset(
    """
a an the and or but if then than that this these those of to in on at by for with from as
is are was were be been being it its not no nor never must may can cannot could should
would will shall do does did done has have had having so such which who whom whose what
when where while because into onto over under one two three both each every any all some
other another same only just also more most less least own very much many few here there
them they their he she his her you your we our us been now still yet per via up out off
down after before again once between through during about above below both same too
""".split()
)


def _lemma(word: str) -> str:
    """Collapse the few inflections that split an otherwise identical rule term.

    Deliberately crude and dependency-free: a real stemmer would be a new
    dependency for a gain this corpus cannot measure. Only suffixes whose
    removal leaves at least four characters are stripped, so ``passes``/``pass``
    merge while ``less`` and ``class`` survive intact.
    """
    for suffix in ("ing", "ers", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def rule_terms(text: str, instance_tokens: Iterable[str] = ()) -> frozenset[str]:
    """Reduce one correction statement to its RULE vocabulary.

    Every construct that names an instance is deleted first: backticked code
    spans, double-quoted literals, file paths, dotted/snake/camel identifiers,
    SHOUTY constant and card ids, and all digits. Then ``instance_tokens`` --
    the words of the source card's own write set -- are removed, so a statement
    can never be matched to another by the module it happened to be about.

    ``instance_tokens`` entries are tokenized on the way in, so a caller may
    pass whole paths (``src/aiworkhub/platform_io.py``) or bare words
    (``platform``) and get the same blocking either way. Accepting a path and
    then blocking nothing, because the path never equals a single word, is a
    silent no-op that would leave every card's own vocabulary in the residue.

    What is left is what the statement ASSERTS rather than what it is about,
    and that residue is the only thing similarity ever sees.
    """
    if not isinstance(text, str):
        return frozenset()
    stripped = _CODE_SPAN_RE.sub(" ", text)
    stripped = _QUOTED_RE.sub(" ", stripped)
    stripped = _FILE_PATH_RE.sub(" ", stripped)
    stripped = _DOTTED_IDENT_RE.sub(" ", stripped)
    stripped = _CAMEL_RE.sub(" ", stripped)
    stripped = _SHOUTY_RE.sub(" ", stripped)
    stripped = _DIGIT_RE.sub(" ", stripped)
    # Split on every non-letter, so "src/aiworkhub/platform_io.py" blocks
    # "platform" and "io" and not only the compound "platform_io" that no
    # prose token could ever equal.
    blocked = {
        word.lower()
        for token in instance_tokens
        for word in _WORD_RE.findall(str(token))
    }
    terms = set()
    for word in _IDENT_RE.findall(stripped.lower()):
        if len(word) < 3 or word in _STOPWORDS or word in blocked:
            continue
        terms.add(_lemma(word))
    return frozenset(terms)


def _instance_tokens(paths: Sequence[str], area: str) -> frozenset[str]:
    """Every word appearing in a card's own write set or repo area."""
    tokens: set[str] = set()
    for value in list(paths) + [area]:
        if isinstance(value, str):
            tokens.update(word.lower() for word in _IDENT_RE.findall(value))
    return frozenset(tokens)


@dataclass(frozen=True)
class CorrectionDocument:
    """One statement from the correction record, with its provenance.

    ``anchor`` is the request id (learning commit) or predecessor request id
    (rework feedback) that identifies the exact judgement this statement came
    from, so a mined skill can name the request as well as the card.
    """

    kind: str
    task_id: str
    anchor: str
    text: str
    paths: tuple[str, ...] = ()
    actor: str = ""
    failure_category: str = ""
    occurred_at: str = ""
    terms: frozenset[str] = field(default_factory=frozenset)

    @property
    def excerpt(self) -> str:
        return self.text[:MAX_EXCERPT_CHARS]


def load_correction_record(
    repo: str | Path, *, limit: int = MAX_CORPUS_DOCUMENTS
) -> tuple[CorrectionDocument, ...]:
    """Read the correction record read-only and reduce it to rule vocabulary.

    Two sources, both already captured with provenance:

    * the learning-commit ledger's ``invariant_candidate`` and
      ``lesson_candidate`` -- the manager's own statement of the rule, written
      at the decision and therefore already at principle altitude;
    * every card's ``review_feedback.instruction`` -- the manager's exact
      statement of what was wrong, written at instance altitude.

    Both are the correction record. The instance-altitude source is by far the
    larger one and is what gives the recurrence gate enough distinct cards to
    decide anything; the principle-altitude source is what makes a promoted
    family readable as a rule.
    """
    rows = learning_commit_store.read_correction_record(repo, limit=limit)
    documents: list[CorrectionDocument] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if len(text) < MIN_STATEMENT_CHARS:
            continue
        paths = tuple(
            str(item) for item in (row.get("paths") or ()) if isinstance(item, str)
        )
        terms = rule_terms(text, _instance_tokens(paths, str(row.get("area") or "")))
        if len(terms) < MIN_RULE_TERMS:
            continue
        documents.append(
            CorrectionDocument(
                kind=str(row.get("kind") or ""),
                task_id=str(row.get("task_id") or ""),
                anchor=str(row.get("anchor") or ""),
                text=text,
                paths=paths,
                actor=str(row.get("actor") or ""),
                failure_category=str(row.get("failure_category") or ""),
                occurred_at=str(row.get("occurred_at") or ""),
                terms=terms,
            )
        )
    # Fixed order: the leader-clustering representatives, and therefore the
    # whole partition, are a function of this order alone.
    documents.sort(
        key=lambda doc: (
            doc.task_id,
            doc.kind,
            hashlib.sha256(doc.text.encode("utf-8")).hexdigest(),
        )
    )
    return tuple(documents)


def inverse_document_frequency(
    documents: Sequence[CorrectionDocument],
) -> dict[str, float]:
    """IDF over the rule vocabulary, so shared scaffolding cannot drive a match.

    Without it, every statement in a correction record resembles every other:
    they all say "must", "test", "candidate", "validation". Weighting by
    rarity means a family is formed by the terms that distinguish its rule.
    """
    total = len(documents)
    frequency: Counter[str] = Counter()
    for document in documents:
        frequency.update(document.terms)
    return {
        term: math.log((total + 1) / (count + 1)) + 1.0
        for term, count in frequency.items()
    }


def similarity(
    left: CorrectionDocument,
    right: CorrectionDocument,
    idf: Mapping[str, float],
) -> float:
    """IDF-weighted Jaccard over rule terms; 0.0 when nothing is shared."""
    shared = left.terms & right.terms
    if not shared:
        return 0.0
    union = left.terms | right.terms
    denominator = sum(idf.get(term, 1.0) for term in union)
    if denominator <= 0.0:
        return 0.0
    return sum(idf.get(term, 1.0) for term in shared) / denominator


@dataclass(frozen=True)
class RuleCluster:
    """One rule family: a representative statement and its members."""

    representative: CorrectionDocument
    members: tuple[CorrectionDocument, ...]

    @property
    def distinct_cards(self) -> tuple[str, ...]:
        return tuple(sorted({m.task_id for m in self.members if m.task_id}))

    @property
    def distinct_files(self) -> tuple[str, ...]:
        files: set[str] = set()
        for member in self.members:
            files.update(member.paths)
        return tuple(sorted(files))

    @property
    def distinct_actors(self) -> tuple[str, ...]:
        return tuple(sorted({m.actor for m in self.members if m.actor}))

    def shared_terms(self, idf: Mapping[str, float], limit: int = 16) -> tuple[str, ...]:
        """The rule vocabulary every member of the family asserts."""
        if not self.members:
            return ()
        shared = set(self.members[0].terms)
        for member in self.members[1:]:
            shared &= member.terms
        return tuple(sorted(shared, key=lambda t: (-idf.get(t, 1.0), t))[:limit])


def cluster_by_rule(
    documents: Sequence[CorrectionDocument],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    idf: Mapping[str, float] | None = None,
) -> tuple[RuleCluster, ...]:
    """Group statements into rule families by deterministic leader clustering.

    A statement joins the family whose REPRESENTATIVE it most resembles, and
    only if that resemblance clears ``threshold``. It never joins a family
    merely because it resembles some member: that transitive step is what turns
    a correction record into one undifferentiated blob.

    The score is exactly :func:`similarity`, computed through an inverted index
    over rule terms rather than by comparing against every leader. Weighted
    Jaccard needs only the shared weight, because the union weight is
    ``w(document) + w(leader) - w(shared)``; accumulating shared weight from the
    postings of the document's own terms therefore visits only leaders that
    share at least one term, and reproduces the pairwise result exactly.
    ``tests/test_skill_miner.py`` asserts that agreement rather than assuming it.

    Sequential by measurement, not by omission. Measured on this repository's
    own 669-document correction record: the pairwise form took 7.10s per pass,
    the indexed form takes 0.33s. Distributing 0.33s of work whose operands are
    small frozensets would spend more on process setup than it could recover,
    and the leader algorithm is order-dependent by construction, so a parallel
    partition would have to be re-serialised to stay deterministic anyway. The
    independent work in this module is the sensitivity curve's several
    clustering passes; those are already cheap enough at this corpus size that
    a pool is not justified, and :func:`mine` exposes ``include_sensitivity`` so
    a caller who does not want them does not pay for them.
    """
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise SkillMinerError("threshold must be a float in (0, 1]")
    if not 0.0 < float(threshold) <= 1.0:
        raise SkillMinerError("threshold must be a float in (0, 1]")
    weights = inverse_document_frequency(documents) if idf is None else idf

    def total_weight(document: CorrectionDocument) -> float:
        return sum(weights.get(term, 1.0) for term in document.terms)

    leaders: list[list[CorrectionDocument]] = []
    leader_weight: list[float] = []
    postings: dict[str, list[int]] = {}
    for document in documents:
        document_weight = total_weight(document)
        shared: dict[int, float] = {}
        for term in document.terms:
            weight = weights.get(term, 1.0)
            for index in postings.get(term, ()):
                shared[index] = shared.get(index, 0.0) + weight
        best_index, best_score = -1, 0.0
        # Ascending leader index breaks a score tie toward the older family, so
        # the partition never depends on dict iteration order.
        for index in sorted(shared):
            overlap = shared[index]
            union = document_weight + leader_weight[index] - overlap
            if union <= 0.0:
                continue
            score = overlap / union
            if score >= threshold and score > best_score:
                best_index, best_score = index, score
        if best_index < 0:
            index = len(leaders)
            leaders.append([document])
            leader_weight.append(document_weight)
            for term in document.terms:
                postings.setdefault(term, []).append(index)
        else:
            leaders[best_index].append(document)
    return tuple(
        RuleCluster(representative=members[0], members=tuple(members))
        for members in leaders
    )


@dataclass(frozen=True)
class GateDecision:
    """Why one rule family was promoted to a candidate, or refused."""

    cluster: RuleCluster
    promoted: bool
    reason: str
    distinct_cards: int
    distinct_files: int
    distinct_actors: int


def apply_recurrence_gate(
    clusters: Sequence[RuleCluster],
    *,
    min_cards: int = MIN_DISTINCT_CARDS,
    min_files: int = MIN_DISTINCT_FILES,
) -> tuple[GateDecision, ...]:
    """Promote only families that recurred across distinct cards AND files.

    The refusal reasons are distinct on purpose, because they mean different
    things. ``single_incident`` is one card corrected repeatedly -- the
    dominant shape in this repository's ledger and the one that would most
    readily manufacture false recurrence. ``below_card_floor`` is a family that
    spans too few judgements to be a pattern. ``below_file_floor`` is a family
    that recurred but only ever in the same place, which is a property of that
    code and belongs on the card, not in a skill that claims to transfer.
    """
    decisions: list[GateDecision] = []
    for cluster in clusters:
        cards = cluster.distinct_cards
        files = cluster.distinct_files
        actors = cluster.distinct_actors
        if len(cards) <= 1:
            reason = "single_incident"
        elif len(cards) < min_cards:
            reason = "below_card_floor"
        elif len(files) < min_files:
            reason = "below_file_floor"
        else:
            reason = "promoted"
        decisions.append(
            GateDecision(
                cluster=cluster,
                promoted=reason == "promoted",
                reason=reason,
                distinct_cards=len(cards),
                distinct_files=len(files),
                distinct_actors=len(actors),
            )
        )
    return tuple(decisions)


def _candidate_identity(terms: Sequence[str], cards: Sequence[str]) -> str:
    """A stable, registry-legal identity built from the family's own rule terms.

    Prefixed ``mined.`` so a mined proposal is never mistaken in the store for
    one a human authored, and suffixed with a digest of the member cards so two
    different families can never collide on a shared vocabulary.
    """
    stem = "_".join(term for term in terms[:4] if term.isalnum())[:80].strip("_")
    digest = hashlib.sha256("\0".join(sorted(cards)).encode("utf-8")).hexdigest()[:8]
    identity = f"mined.{stem}.{digest}" if stem else f"mined.rule.{digest}"
    identity = identity.lower()
    return identity if skill_registry._IDENTITY_RE.match(identity) else f"mined.rule.{digest}"


def _proposal_draft(
    cluster: RuleCluster, identity: str
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Build the draft a manager passes to ``aiworkhub_manager_skill_propose``.

    Only dimensions derivable from measured card evidence are filled in. The
    two dimensions that decide relevance -- ``triggers`` and ``applicability``
    -- are drawn from closed vocabularies and are left EMPTY, because inferring
    a vocabulary token from prose is exactly how a mined skill set drifts to
    instance level. They are named in the returned ``draft_incomplete`` tuple
    so the caller is told what a human still owes.
    """
    scope = skill_registry.common_path_scope(list(cluster.distinct_files))
    draft = {
        "identity": identity,
        "version": "0.1.0",
        "scope": skill_registry.SkillScope.REPOSITORY.value,
        "task_family": "",
        "path_or_symbol": scope,
        "risk": skill_registry.RiskLevel.MEDIUM.value,
        # Every statement in this corpus was written at a review or rework
        # decision, so that is the only stage the evidence supports.
        "stage": "rework",
        "triggers": [],
        "applicability": [],
        "confidence": 0.0,
    }
    incomplete = ["task_family", "triggers", "applicability", "confidence"]
    if not scope:
        draft["path_or_symbol"] = ""
        incomplete.insert(0, "path_or_symbol")
    return draft, tuple(incomplete)


def _candidate(decision: GateDecision, idf: Mapping[str, float]) -> dict[str, Any]:
    cluster = decision.cluster
    terms = cluster.shared_terms(idf)
    cards = cluster.distinct_cards
    identity = _candidate_identity(terms, cards)
    draft, incomplete = _proposal_draft(cluster, identity)
    warnings: list[str] = []
    if decision.distinct_actors < ADVISORY_MIN_DISTINCT_ACTORS:
        warnings.append(
            "weak_actor_independence:"
            f"{decision.distinct_actors}<{ADVISORY_MIN_DISTINCT_ACTORS}"
        )
    failure_classes = Counter(
        member.failure_category for member in cluster.members if member.failure_category
    )
    return {
        "candidate_id": identity,
        "rule_terms": list(terms),
        "representative": cluster.representative.excerpt,
        "statements": [
            {
                "kind": member.kind,
                "task_id": member.task_id,
                "request_id": member.anchor,
                "excerpt": member.excerpt,
            }
            for member in cluster.members
        ],
        "recurrence": {
            "distinct_cards": decision.distinct_cards,
            "distinct_files": decision.distinct_files,
            "distinct_actors": decision.distinct_actors,
            "statements": len(cluster.members),
        },
        "provenance": {
            "card_ids": list(cards),
            "request_ids": sorted({m.anchor for m in cluster.members if m.anchor}),
            "files": list(cluster.distinct_files)[:40],
            "actors": list(cluster.distinct_actors),
        },
        "failure_classes": dict(failure_classes),
        "warnings": warnings,
        "proposal_draft": draft,
        "draft_incomplete": list(incomplete),
        "next_action": "aiworkhub_manager_skill_propose",
    }


def _sensitivity(
    documents: Sequence[CorrectionDocument],
    idf: Mapping[str, float],
    *,
    min_cards: int,
    min_files: int,
) -> list[dict[str, Any]]:
    """How many families clear the gate at each threshold.

    Returned with every result so the altitude knob is visible rather than
    hidden in a constant: a caller who thinks the default is too permissive or
    too strict can see what the alternative would yield before changing it.
    """
    curve: list[dict[str, Any]] = []
    for threshold in SENSITIVITY_THRESHOLDS:
        clusters = cluster_by_rule(documents, threshold=threshold, idf=idf)
        decisions = apply_recurrence_gate(
            clusters, min_cards=min_cards, min_files=min_files
        )
        curve.append(
            {
                "threshold": threshold,
                "candidates": sum(1 for d in decisions if d.promoted),
                "families": sum(1 for c in clusters if len(c.members) > 1),
            }
        )
    return curve


def mine(
    repo: str | Path,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_cards: int = MIN_DISTINCT_CARDS,
    min_files: int = MIN_DISTINCT_FILES,
    limit: int = MAX_CORPUS_DOCUMENTS,
    include_sensitivity: bool = True,
) -> dict[str, Any]:
    """Mine the correction record into gated, provenance-bound PROPOSALS.

    Read-only end to end: nothing in this call path opens a store for writing,
    and the result contains no lifecycle transition. A candidate becomes a
    stored skill only when a manager passes its draft to
    ``aiworkhub_manager_skill_propose``, and becomes ACTIVE only through the
    registry's existing two-distinct-actor evidence gate.
    """
    documents = load_correction_record(repo, limit=limit)
    idf = inverse_document_frequency(documents)
    clusters = cluster_by_rule(documents, threshold=threshold, idf=idf)
    decisions = apply_recurrence_gate(
        clusters, min_cards=min_cards, min_files=min_files
    )
    promoted = [d for d in decisions if d.promoted]
    promoted.sort(
        key=lambda d: (-d.distinct_cards, -d.distinct_files, d.cluster.representative.task_id)
    )
    refused: Counter[str] = Counter(d.reason for d in decisions if not d.promoted)
    return {
        "schema_id": SCHEMA_ID,
        "corpus": {
            "documents": len(documents),
            "distinct_cards": len({d.task_id for d in documents}),
            "sources": dict(Counter(d.kind for d in documents)),
        },
        "gate": {
            "threshold": float(threshold),
            "min_distinct_cards": min_cards,
            "min_distinct_files": min_files,
            "advisory_min_distinct_actors": ADVISORY_MIN_DISTINCT_ACTORS,
        },
        "families": len(clusters),
        "candidates": [_candidate(d, idf) for d in promoted[:MAX_CANDIDATES]],
        "refused": dict(refused),
        "sensitivity": (
            _sensitivity(documents, idf, min_cards=min_cards, min_files=min_files)
            if include_sensitivity
            else []
        ),
        "authority": {
            "produces": "proposals_only",
            "writes": "none",
            "activation": "manager_gated_two_distinct_actor_identities",
        },
    }


def injectability(record: Any) -> tuple[bool, str]:
    """Whether one stored record can be injected today, and the EXACT reason not.

    ``skill_registry.select`` serves ACTIVE records exclusively, and the store
    demotes an ACTIVE record whose own evidence no longer meets the rule in
    force. Both facts decide injectability, and reporting only one of them is
    how three PROPOSED records read as ``injectable: true`` while no worker
    could ever receive them.

    ``activation_evidence_below_two_distinct_actors`` is preserved verbatim: it
    is the measured reason the repository's one ACTIVE record is not injectable,
    and a reader who learned it must keep finding it.
    """
    lifecycle = record.lifecycle_state
    if lifecycle is skill_registry.LifecycleState.RETIRED:
        return False, "lifecycle_state_is_retired"
    if skill_registry.unresolved_negative_evidence(record):
        return False, "unresolved_negative_evidence"
    actors = skill_registry.independent_accepted_evidence_count(record)
    if actors < 2:
        return False, "activation_evidence_below_two_distinct_actors"
    if lifecycle is not skill_registry.LifecycleState.ACTIVE:
        return False, "lifecycle_state_is_proposed_not_active"
    return True, ""


def candidate_draft(
    repo: str | Path,
    candidate_id: str,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_cards: int = MIN_DISTINCT_CARDS,
    min_files: int = MIN_DISTINCT_FILES,
) -> dict[str, Any]:
    """Return ONE mined candidate's proposal draft by its ``candidate_id``.

    :func:`mine` already computes every mechanical dimension of a proposal from
    measured card evidence -- identity, version, scope, path_or_symbol, risk and
    stage -- and then hands the whole draft back for a manager to retype. That
    round trip is pure transcription risk: a mistyped identity silently proposes
    a different skill than the one the evidence supports.

    This is the server-side lookup that makes the hand-off exact. It is still
    read-only and still produces no lifecycle transition: what a manager owes is
    the judgement half (``task_family``, ``triggers``, ``applicability``,
    ``confidence``, and the procedure text), which is exactly what
    ``draft_incomplete`` names and what no measurement can supply.
    """
    wanted = str(candidate_id or "").strip()
    if not wanted:
        raise SkillMinerError("candidate_id_required")
    report = mine(
        repo,
        threshold=threshold,
        min_cards=min_cards,
        min_files=min_files,
        include_sensitivity=False,
    )
    for candidate in report.get("candidates", []):
        if str(candidate.get("candidate_id") or "") == wanted:
            return {
                "candidate_id": wanted,
                "proposal_draft": candidate_proposal_payload(candidate),
                "draft_incomplete": list(candidate.get("draft_incomplete") or []),
                "provenance": candidate.get("provenance") or {},
                "recurrence": candidate.get("recurrence") or {},
            }
    known = [str(c.get("candidate_id") or "") for c in report.get("candidates", [])]
    raise SkillMinerError(
        f"unknown_candidate_id:{wanted[:80]}; mined candidates: {known[:10]}"
    )


def measure_retirement(
    repo: str | Path, *, min_anchors: int = MIN_RETIREMENT_ANCHORS
) -> dict[str, Any]:
    """Measure each stored skill against its own failure class, or say it cannot.

    Layer six of NF-2026-00312 wants, per skill, the count of cards it was
    injected into and the rejection rate for its own failure class on those
    cards against the rate before it existed.

    **That injection denominator does not exist in this repository today, and
    this function refuses to invent one.** Two independent facts make it
    unavailable, and both are reported rather than papered over: the worker
    bundle builds the skill packet at prompt time and never persists it onto
    the card, and not one stored card carries the selection vocabulary that a
    packet requires, so replaying selection over history yields zero for every
    skill regardless of merit. A rate computed on that denominator would be
    fiction, and a retirement decision taken on it would be worse than none.

    What IS measurable is reported instead, from evidence that already exists:
    a skill's evidence entries anchor to real task cards, and those cards have
    real adjudicated outcomes. So each skill is measured on the cards where it
    was actually judged. Below ``min_anchors`` resolvable anchors the verdict
    is ``insufficient_evidence`` -- never ``retire``. Every verdict is a
    RECOMMENDATION: retirement, like activation, stays manager-gated.
    """
    repo_path = Path(repo)
    try:
        stored = skill_registry_store.list_records(repo_path)
    except (skill_registry_store.SkillStoreError, OSError, sqlite3.Error) as exc:
        raise SkillMinerError(f"skill_store_unreadable:{type(exc).__name__}") from exc

    outcomes = learning_commit_store.read_card_outcomes(repo_path)
    injection = learning_commit_store.injection_ledger_state(repo_path)
    try:
        per_skill_injection = skill_registry_store.injection_counts(repo_path)
    except (skill_registry_store.SkillStoreError, OSError, sqlite3.Error):
        per_skill_injection = {}

    skills: list[dict[str, Any]] = []
    for record in stored:
        anchors = sorted({item.source for item in record.evidence if item.source})
        resolved = {a: outcomes[a] for a in anchors if a in outcomes}
        decided = Counter(str(v.get("outcome") or "") for v in resolved.values())
        classes = Counter(
            str(v.get("failure_category") or "")
            for v in resolved.values()
            if v.get("failure_category")
        )
        injectable, injectable_reason = injectability(record)
        rejected = decided.get("rejected", 0)
        total = sum(decided.values())
        if len(resolved) < min_anchors:
            verdict, reason = (
                "insufficient_evidence",
                f"resolvable_anchor_cards={len(resolved)}<{min_anchors}",
            )
        elif rejected == 0:
            verdict, reason = "keep", "no_rejection_in_its_own_anchor_cards"
        else:
            verdict, reason = (
                "review_for_retirement",
                f"rejected_anchor_cards={rejected}/{total}",
            )
        skills.append(
            {
                "identity": record.identity,
                "version": record.version,
                "stored_lifecycle_state": record.lifecycle_state.value,
                "injectable": bool(injectable),
                "injectable_reason": injectable_reason,
                "evidence_anchors": anchors,
                "resolvable_anchor_cards": len(resolved),
                "anchor_outcomes": dict(decided),
                "anchor_failure_classes": dict(classes),
                "injected_cards": int(
                    per_skill_injection.get(
                        f"{record.identity}@{record.version}", {}
                    ).get("injected_cards", 0)
                ),
                "verdict": verdict,
                "reason": reason,
            }
        )
    skills.sort(key=lambda item: (item["identity"], item["version"]))
    return {
        "schema_id": RETIREMENT_SCHEMA_ID,
        "injection_ledger": injection,
        "skills": skills,
        "authority": {
            "produces": "recommendations_only",
            "writes": "none",
            "retirement": "manager_gated",
        },
    }


def candidate_proposal_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return one candidate's draft as the exact ``propose`` keyword payload.

    Kept separate from :func:`mine` so the hand-off is explicit: a caller has to
    take a draft, fill in what ``draft_incomplete`` names, and pass it on. There
    is no path from mining to a stored record that does not go through a
    manager typing the missing dimensions.
    """
    draft = candidate.get("proposal_draft")
    if not isinstance(draft, Mapping):
        raise SkillMinerError("candidate carries no proposal_draft")
    return json.loads(json.dumps(dict(draft)))
