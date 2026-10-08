"""Read-only mining of recurring accepted-card validation commands into
proposed tool recipes (NF-2026-01425).

Tool recipes grew only through the static seed catalogue
(``manager_recipe_tools.UNIVERSAL_RECIPES`` / ``CONDITIONAL_RECIPES``):
nothing observed that accepted cards keep repeating the same validation
command, card after card, and turned that recurrence into a proposal. This
module mirrors the skill-mining pipeline added for NF-2026-01412
(``skill_miner.mine`` + ``manager_ai_tools._auto_mine_and_propose_skills``),
applied to validation commands instead of correction statements.

Read-only end to end. :func:`mine` opens nothing for writing: it loads
bounded task cards (:func:`aiworkhub.task_store.list_task_cards`) and the
existing recipe registry (:func:`aiworkhub.tool_recipes_store.load_registry`,
read-only), and returns PROPOSALS. Nothing here ever registers, activates or
runs a mined recipe -- :func:`auto_mine_and_propose` is the only writer, and
it writes exclusively to the additive ``recipe_proposals`` table via
:func:`aiworkhub.tool_recipes_store.put_proposal`.

Normalization is structural, not a shell parser. Every validation command is
tokenized with :mod:`shlex` (POSIX rules) and turned into an argv TEMPLATE:
each path-like token collapses into one typed path-list slot (the narrowest
parameter type :mod:`aiworkhub.tool_recipes` already supports for a group of
caller-chosen files), and every other token stays a fixed literal. A command
carrying shell syntax, a leading environment assignment, or text
:mod:`shlex` cannot parse is refused with its own reason code rather than
guessed at -- this module never constructs a free-text string slot and never
accepts a command a shell could reinterpret.

Recurrence, not instance. A template is promoted only once at least
``MIN_DISTINCT_CARDS`` distinct accepted cards and ``MIN_DISTINCT_ACTORS``
distinct actors (a card's ``claimed_by``, falling back to ``runner``) have
produced it -- one hard card run many times must never manufacture a
recipe. A template already covered by a registered recipe's own head (its
literal tokens before ITS first slot) is refused ``covered_by_registered``
rather than proposed again.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from . import manager_recipe_tools, task_store, tool_recipes, tool_recipes_store
from .tool_recipes import (
    ArgvLiteral,
    ArgvSlot,
    ParamSpec,
    ParamType,
    Recipe,
    RecipeError,
)

SCHEMA_ID = "aiworkhub.recipe_miner.v1"

# Recurrence gate: below this, a recurring command is one hard card run many
# times, not a pattern -- counted in DISTINCT cards and DISTINCT actors, never
# raw occurrences.
MIN_DISTINCT_CARDS = 3
MIN_DISTINCT_ACTORS = 2

# Bounded read: list_task_cards itself defaults to 500 and clamps to 5000;
# this is the ceiling *this* miner asks for.
MAX_CORPUS_CARDS = 2000

# A mined report can propose at most this many candidates per call.
MAX_CANDIDATES = 32

# Provenance is evidence, not an audit log: bounded so a cluster with
# thousands of members still produces a small, stable payload.
MAX_PROVENANCE_CARD_IDS = 20

REASON_SHELL_SYNTAX = "shell_syntax"
REASON_ENV_ASSIGNMENT = "env_assignment"
REASON_UNPARSEABLE = "unparseable_command"
REASON_BELOW_RECURRENCE = "below_recurrence"
REASON_COVERED_BY_REGISTERED = "covered_by_registered"
REASON_NO_REFERENCE_RECIPE = "no_reference_recipe"
REASON_INTERLEAVED_PATHS = "interleaved_paths"

# Unconditional: refused wherever these characters appear in the raw command
# text, quoted or not. argv is an execve vector, never a shell command line,
# so a recipe built from a string a shell could reinterpret is refused before
# it is ever tokenized.
_SHELL_SYNTAX_CHARS = frozenset("|&;<>`$")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Source/test extensions this miner recognizes as path-like even without a
# '/' in the token (a bare "a.js" in a project root is still a path).
_PATH_EXTENSIONS = frozenset({".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"})

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_TemplateToken = tuple[str, str]  # ("lit", text) or ("slot", "paths")


def _is_path_like(token: str) -> bool:
    if token.startswith("-"):
        return False
    if "/" in token or "\\" in token:
        return True
    return any(token.endswith(ext) for ext in _PATH_EXTENSIONS)


def _actor_identity(card: dict[str, Any]) -> str:
    return str(card.get("claimed_by") or card.get("runner") or "").strip()


def _is_accepted(card: dict[str, Any]) -> bool:
    return (
        str(card.get("status") or "") == "finished"
        and bool(str(card.get("accepted_at") or "").strip())
    )


def _normalize_command(command: str) -> tuple[str | None, tuple[_TemplateToken, ...] | None]:
    """Return ``(refusal_reason, template)`` with exactly one side ``None``."""
    if any(ch in _SHELL_SYNTAX_CHARS for ch in command):
        return REASON_SHELL_SYNTAX, None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return REASON_UNPARSEABLE, None
    if not tokens:
        return REASON_UNPARSEABLE, None
    if _ENV_ASSIGNMENT_RE.match(tokens[0]):
        return REASON_ENV_ASSIGNMENT, None

    template: list[_TemplateToken] = []
    slot_added = False
    slot_closed = False
    for token in tokens:
        if _is_path_like(token):
            if slot_closed:
                return REASON_INTERLEAVED_PATHS, None
            if not slot_added:
                template.append(("slot", "paths"))
                slot_added = True
        else:
            if slot_added:
                slot_closed = True
            template.append(("lit", token))
    if template[0][0] != "lit":
        # argv[0] must be a literal executable; a command that opens on a
        # path-like token can never become a valid Recipe.
        return tool_recipes.REASON_NON_LITERAL_EXECUTABLE, None
    return None, tuple(template)


class _Cluster:
    """Accumulates provenance for one argv template across accepted cards."""

    __slots__ = ("cards", "actors", "first_accepted_at", "last_accepted_at", "sample_command")

    def __init__(self) -> None:
        self.cards: dict[str, str] = {}
        self.actors: set[str] = set()
        self.first_accepted_at = ""
        self.last_accepted_at = ""
        self.sample_command = ""

    def add(self, task_id: str, actor: str, accepted_at: str, raw_command: str) -> None:
        self.cards.setdefault(task_id, accepted_at)
        if actor:
            self.actors.add(actor)
        if not self.sample_command:
            self.sample_command = raw_command
        if accepted_at:
            if not self.first_accepted_at or accepted_at < self.first_accepted_at:
                self.first_accepted_at = accepted_at
            if not self.last_accepted_at or accepted_at > self.last_accepted_at:
                self.last_accepted_at = accepted_at

    @property
    def distinct_cards(self) -> int:
        return len(self.cards)

    @property
    def distinct_actors(self) -> int:
        return len(self.actors)


def _literal_head(template: Sequence[_TemplateToken]) -> tuple[str, ...]:
    head: list[str] = []
    for kind, value in template:
        if kind != "lit":
            break
        head.append(value)
    return tuple(head)


def _recipe_head(recipe: Recipe) -> tuple[str, ...]:
    head: list[str] = []
    for token in recipe.argv:
        if not isinstance(token, ArgvLiteral):
            break
        head.append(token.text)
    return tuple(head)


def _recipe_has_path_slot(recipe: Recipe) -> bool:
    by_name = {param.name: param for param in recipe.parameters}
    for token in recipe.argv:
        if not isinstance(token, ArgvSlot):
            continue
        spec = by_name.get(token.param)
        if spec is None:
            continue
        if spec.type is ParamType.PATH or (
            spec.type is ParamType.LIST and spec.item_type is ParamType.PATH
        ):
            return True
    return False


def _covered_by_registered(registry, head: tuple[str, ...]) -> bool:
    if not head:
        return False
    for recipe in registry:
        if head == _recipe_head(recipe) and _recipe_has_path_slot(recipe):
            return True
    return False


def _common_prefix_len(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    length = 0
    for left, right in zip(a, b):
        if left != right:
            break
        length += 1
    return length


def _reference_catalogue() -> tuple[Recipe, ...]:
    return tuple(manager_recipe_tools.UNIVERSAL_RECIPES) + tuple(
        manager_recipe_tools.CONDITIONAL_RECIPES
    )


def _pick_reference_recipe(head: tuple[str, ...]) -> Recipe | None:
    """Pick the canonical validation recipe the mined ``head`` most resembles.

    Scored by longest shared literal prefix with the candidate's own
    registered head; ties keep the FIRST catalogue entry at that score
    (``max`` over a fixed iteration order), preferring the simpler/earlier
    canonical definition over a more specific variant of the same family
    (e.g. the plain pytest recipe over the package's own gate variant).
    """
    best: Recipe | None = None
    best_score = 0
    for recipe in _reference_catalogue():
        score = _common_prefix_len(head, _recipe_head(recipe))
        if score > best_score:
            best = recipe
            best_score = score
    return best


def _slugify(head: Sequence[str]) -> str:
    slug = _SLUG_RE.sub("_", "_".join(head).lower()).strip("_")
    return slug or "cmd"


def _build_candidate(
    template: tuple[_TemplateToken, ...], head: tuple[str, ...], cluster: _Cluster
) -> dict[str, Any] | None:
    reference = _pick_reference_recipe(head)
    if reference is None:
        return None

    argv_tokens: list[ArgvLiteral | ArgvSlot] = []
    parameters: tuple[ParamSpec, ...] = ()
    for kind, value in template:
        if kind == "lit":
            argv_tokens.append(tool_recipes.lit(value))
        else:
            argv_tokens.append(tool_recipes.slot("paths"))
            parameters = (
                ParamSpec(name="paths", type=ParamType.LIST, required=True, item_type=ParamType.PATH),
            )

    template_digest = hashlib.sha256(
        tool_recipes.canonical_json([[kind, value] for kind, value in template]).encode("utf-8")
    ).hexdigest()
    recipe_id = f"mined.{_slugify(head)}.{template_digest[:8]}"
    purpose = (
        f"Recurring validation command {cluster.sample_command!r}, observed across "
        f"{cluster.distinct_cards} accepted cards and {cluster.distinct_actors} distinct "
        f"actors; resembles {reference.id!r}."
    )

    recipe = Recipe(
        id=recipe_id,
        version="1",
        purpose=purpose,
        task_kind=reference.task_kind,
        parameters=parameters,
        platforms=reference.platforms,
        capabilities=reference.capabilities,
        risk_class=reference.risk_class,
        resource_bounds=reference.resource_bounds,
        cache_policy=reference.cache_policy,
        argv=tuple(argv_tokens),
    )

    provenance = {
        "member_card_ids": sorted(cluster.cards)[:MAX_PROVENANCE_CARD_IDS],
        "distinct_cards": cluster.distinct_cards,
        "distinct_actors": cluster.distinct_actors,
        "first_accepted_at": cluster.first_accepted_at,
        "last_accepted_at": cluster.last_accepted_at,
        "sample_command": cluster.sample_command,
        "reference_recipe_id": reference.id,
        "template_digest": template_digest,
    }
    return {
        "recipe_id": recipe_id,
        "template_digest": template_digest,
        "head": list(head),
        "draft": tool_recipes.recipe_payload(recipe),
        "provenance": provenance,
    }


def mine(repo: str | Path, *, limit: int = MAX_CORPUS_CARDS) -> dict[str, Any]:
    """Mine accepted cards' validation commands into gated, bounded PROPOSALS.

    Read-only end to end: nothing in this call path opens a store for
    writing. A candidate becomes a stored proposal only when a caller passes
    its draft to :func:`aiworkhub.tool_recipes_store.put_proposal`, and a
    proposal is never a registry entry -- nothing here, or anywhere this
    module is called from, ever registers, activates or runs a recipe.
    """
    cards = [card for card in task_store.list_task_cards(repo, limit=limit) if _is_accepted(card)]
    commands = 0
    clusters: dict[tuple[_TemplateToken, ...], _Cluster] = {}
    refused: Counter[str] = Counter()

    for card in cards:
        validation = card.get("validation")
        if not isinstance(validation, list):
            continue
        task_id = str(card.get("task_id") or "")
        actor = _actor_identity(card)
        accepted_at = str(card.get("accepted_at") or "")
        for raw_command in validation:
            if not isinstance(raw_command, str) or not raw_command.strip():
                continue
            commands += 1
            reason, template = _normalize_command(raw_command)
            if reason is not None or template is None:
                refused[reason or REASON_UNPARSEABLE] += 1
                continue
            clusters.setdefault(template, _Cluster()).add(task_id, actor, accepted_at, raw_command)

    registry = tool_recipes_store.load_registry(repo)
    candidates: list[dict[str, Any]] = []
    for template, cluster in clusters.items():
        if cluster.distinct_cards < MIN_DISTINCT_CARDS or cluster.distinct_actors < MIN_DISTINCT_ACTORS:
            refused[REASON_BELOW_RECURRENCE] += 1
            continue
        head = _literal_head(template)
        if _covered_by_registered(registry, head):
            refused[REASON_COVERED_BY_REGISTERED] += 1
            continue
        try:
            candidate = _build_candidate(template, head, cluster)
        except RecipeError as exc:
            refused[f"invalid_recipe_draft:{exc.reason}"] += 1
            continue
        if candidate is None:
            refused[REASON_NO_REFERENCE_RECIPE] += 1
            continue
        candidates.append(candidate)

    candidates.sort(key=lambda candidate: candidate["recipe_id"])
    candidates = candidates[:MAX_CANDIDATES]

    return {
        "schema_id": SCHEMA_ID,
        "corpus": {"cards": len(cards), "commands": commands},
        "gate": {
            "min_distinct_cards": MIN_DISTINCT_CARDS,
            "min_distinct_actors": MIN_DISTINCT_ACTORS,
        },
        "candidates": candidates,
        "refused": dict(refused),
        "authority": {"produces": "proposals_only", "writes": "none"},
    }


def auto_mine_and_propose(repo: str | Path) -> dict[str, Any]:
    """Best-effort recipe mining after one accepted learning commit.

    Bounded and advisory: every failure here is caught and reported, never
    raised, so a mining or proposal defect can never flip the commit's own
    ``ok``/``failures`` -- the same contract ``skill_mining`` already holds.
    A cluster whose proposal already exists is not a failure: ``put_proposal``
    updates its provenance and reports ``already_proposed``.
    """
    started = time.monotonic()
    proposed: list[str] = []
    already_proposed: list[str] = []
    refused: dict[str, str] = {}
    candidate_count = 0
    try:
        report = mine(repo)
        candidates = report.get("candidates", [])
        candidate_count = len(candidates)
        for candidate in candidates:
            identity = str(candidate.get("recipe_id") or "")
            result = tool_recipes_store.put_proposal(
                repo, candidate["draft"], candidate["provenance"]
            )
            if result.get("status") == "already_proposed":
                already_proposed.append(identity)
            else:
                proposed.append(identity)
    except Exception as exc:  # noqa: BLE001 - mining is advisory, never fatal to the commit
        refused["mining"] = f"recipe_mining_failed:{type(exc).__name__}"
    return {
        "candidates": candidate_count,
        "proposed": proposed,
        "already_proposed": already_proposed,
        "refused_by_reason": refused,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }
