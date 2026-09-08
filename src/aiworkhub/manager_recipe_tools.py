"""Manager-bound driver and canonical catalogue for the tool recipe registry.

``tool_recipes`` owns a complete, tested description and validation layer for
tool invocations, and ``tool_recipes_store`` (landed alongside it) owns durable
persistence for that layer -- but nothing ever put a manifest into the store.
The dashboard therefore loaded a store that had never been written and reported
``no_sample``: a true statement about an empty repository, and a useless one.

This module closes that gap from both ends:

* a manager-gated registration surface (:func:`register`) that takes a manifest
  mapping from the CALLER and persists it through the store's public API, plus
  two read surfaces (:func:`list_registered`, :func:`show`); and
* :data:`CANONICAL_RECIPES`, a catalogue describing the tool invocations this
  repository's own machinery actually performs, each entry annotated with the
  exact call site it describes, installable through :func:`seed_canonical`.

**No execution, at all.** ``tool_recipes`` states that it "deliberately contains
no execution engine ... it never runs anything", and giving it a registration
surface and real content does not add one. Nothing here spawns a subprocess,
opens a socket, or renders an argv vector for execution: a manifest DESCRIBES an
invocation the launcher independently performs elsewhere. Every ``argv[0]``
below is a literal token, every literal is shell-safe by the module's own
construction-time check, and every fail-closed reason code is surfaced verbatim.

Simpler than the skills driver it follows. ``manager_skill_tools`` needs a
compare-and-swap token because a skill record carries runtime state that is
advanced in place. A :class:`~aiworkhub.tool_recipes.Recipe` is immutable
content: the store's primary key is ``(recipe_id, version)`` and a stored
version can never be rewritten, so registration is a pure insert -- there is no
load-modify-write to lose, and no token to carry.

Nothing in a manifest is inferred. :func:`register` passes the caller's mapping
straight to :func:`tool_recipes.recipe_from_mapping`, so every manifest -- one
typed by a manager and one in the catalogue below alike -- is built by the
ordinary constructors and refused with the module's own stable ``reason`` when
it is invalid.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import core
from . import recipe_runner
from . import tool_recipes as tr
from . import tool_recipes_store as store

# Bound every read surface so a caller can never receive an unbounded result.
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = store.MAX_LOAD_LIMIT


def _manager_context() -> tuple[Path | None, dict[str, Any]]:
    """Resolve the verified manager identity and repository, or an error payload.

    Returns ``(root, manager)``. On failure ``root`` is ``None`` and ``manager``
    is the fail-closed error result to return verbatim. Unlike the skills
    driver this returns no gate token: recipe registration is an insert of
    immutable content and the registry it writes through has no authority
    object of its own, so the manager session id is used only to prove the
    route is verified and is never persisted.
    """
    route = core.manager_bootstrap()
    identity = route.get("manager_route") if isinstance(route, dict) else None
    if (
        not isinstance(route, dict)
        or route.get("role") != "manager"
        or not isinstance(identity, dict)
    ):
        return None, {
            "ok": False,
            "error": "verified_manager_identity_required",
            "surface": "manager_mcp",
        }
    session_id = str(identity.get("thread_id") or identity.get("session_id") or "").strip()
    if not session_id:
        return None, {
            "ok": False,
            "error": "manager_session_identity_missing",
            "surface": "manager_mcp",
        }
    provider = str(identity.get("provider") or route.get("provider") or "manager").strip()
    root = Path(str(route.get("repo") or core.repo_root())).resolve()
    return root, {
        "provider": provider,
        "session_id": session_id,
        "repo": str(root),
    }


def _write_gate(manager: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "write_gate_closed",
        "surface": "manager_mcp",
        "manager": manager,
    }


def _actor(manager: dict[str, Any]) -> tr.ActorIdentity:
    """Bind the receipt's actor from the route this module already verified.

    ``_manager_context`` has already refused an unverified route and a missing
    session identity, so by here ``provider``/``session_id`` are the verified
    manager route's own fields -- not a name a caller typed. There is no MCP
    parameter that reaches this: ``aiworkhub_manager_recipe_run`` exposes
    ``recipe_id``, ``version``, ``params`` and ``grant_capabilities``, and none
    of them can name an actor.

    :class:`tool_recipes.ActorIdentity` may still refuse the pair -- a provider
    or session id containing a space or a ``:`` would make the derived key
    ambiguous. That is raised, not swallowed: recording such a run as
    ``unattributed`` would quietly hide a route this surface DID verify, and
    the usage numbers would then under-count a real user.
    """
    return tr.ActorIdentity(
        kind=tr.ACTOR_KIND_MANAGER,
        provider=str(manager["provider"]),
        session_id=str(manager["session_id"]),
    )


def _build(manifest: Mapping[str, Any]) -> tr.Recipe:
    """Build one Recipe from a caller mapping, never inferring a field.

    ``recipe_from_mapping`` runs the ordinary dataclass constructors, so an
    unsafe literal, a non-literal executable, an unknown argv slot, a duplicate
    parameter or an invalid schema raises :class:`tool_recipes.RecipeError`
    with its stable ``reason`` rather than yielding a manifest.
    """
    if not isinstance(manifest, Mapping):
        raise tr.RecipeError(tr.REASON_BAD_MANIFEST, "manifest must be a mapping")
    return tr.recipe_from_mapping(manifest)


def _summary(recipe: tr.Recipe) -> dict[str, Any]:
    """Bounded, JSON-safe identity/contract projection of one stored manifest.

    Mirrors :class:`tool_recipes.RecipeManifest`: identity and contract
    metadata, never the argv template. A caller that needs the body asks for
    one exact recipe through :func:`show`.
    """
    return {
        "recipe_id": recipe.id,
        "version": recipe.version,
        "digest": recipe.digest,
        "purpose": recipe.purpose,
        "task_kind": recipe.task_kind.value,
        "risk_class": recipe.risk_class.value,
        "cache_policy": recipe.cache_policy.value,
        "platforms": list(recipe.platforms),
        "capabilities": list(recipe.capabilities),
        "parameters": [
            {"name": p.name, "type": p.type.value, "required": p.required}
            for p in recipe.parameters
        ],
    }


def _store_failure(exc: Exception, manager: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "error": f"tool_recipe_store_failed:{type(exc).__name__}",
        "detail": str(exc)[:240],
        "manager": manager,
        "surface": "manager_mcp",
    }


def _persist(root: Path, recipe: tr.Recipe, manager: dict[str, Any]) -> dict[str, Any]:
    """Insert one manifest, mapping the store's refusals onto stable reasons.

    The store refuses a second write to the same ``(id, version)`` and refuses
    binding a stored digest to a different identity. Both are the same fact for
    a caller -- this exact manifest identity is already taken and immutable --
    so both surface ``tool_recipes.REASON_DUPLICATE``, the module's own stable
    string for a duplicate definition, with the store's message alongside it.
    """
    try:
        receipt = store.put_recipe(root, recipe)
    except store.ToolRecipeStoreConflictError as exc:
        return {
            "ok": False,
            "error": str(exc)[:240],
            "reason_code": tr.REASON_DUPLICATE,
            "recipe_id": recipe.id,
            "version": recipe.version,
            "manager": manager,
            "surface": "manager_mcp",
        }
    except (store.ToolRecipeStoreError, OSError, sqlite3.Error) as exc:
        return _store_failure(exc, manager)
    return {
        "ok": True,
        "schema_id": receipt["schema_id"],
        "recipe_id": receipt["recipe_id"],
        "version": receipt["version"],
        "digest": receipt["digest"],
        "manager": manager,
        "surface": "manager_mcp",
    }


def register(*, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """MANAGER WRITE: persist one caller-defined tool recipe manifest.

    Every field of the manifest is the caller's; nothing is inferred, defaulted
    into plausibility or generated here. The manifest is reconstructed through
    :func:`tool_recipes.recipe_from_mapping`, so an invalid one is refused with
    its stable ``reason`` before any write, and a manifest whose ``(id,
    version)`` is already stored is refused without touching the stored row --
    a version, once written, is immutable.

    This registers a DESCRIPTION. It never runs the invocation it describes.
    """
    root, manager = _manager_context()
    if root is None:
        return manager
    if not core.writes_allowed():
        return _write_gate(manager)
    try:
        recipe = _build(manifest)
    except tr.RecipeError as exc:
        return {
            "ok": False,
            "error": exc.message[:240],
            "reason_code": exc.reason,
            "manager": manager,
            "surface": "manager_mcp",
        }
    except Exception as exc:  # noqa: BLE001 -- an unmodelled payload is a bad manifest
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:240],
            "reason_code": tr.REASON_BAD_MANIFEST,
            "manager": manager,
            "surface": "manager_mcp",
        }
    return _persist(root, recipe, manager)


def seed_canonical() -> dict[str, Any]:
    """MANAGER WRITE: install :data:`CANONICAL_RECIPES` into this repository.

    Idempotent by the store's own immutability rather than by a check here: a
    catalogue entry whose ``(id, version)`` is already stored is reported as
    ``already_registered`` and its stored row is left untouched, so running this
    twice converges instead of failing.

    Every entry describes an invocation this repository genuinely performs; the
    call site is named in the manifest's ``purpose`` and in the comment above
    its definition. This function persists those descriptions and runs none of
    them.

    **Per-project, not a fixed list.** The 18 universal entries -- the git
    workspace/finalization/diff probes and the packaged operator modules -- are
    true of any repository AIWorkHub manages and are always installed. The four
    that depend on a toolchain or on paths that must exist in the TARGET project
    are installed only when :func:`seeding_plan` measures them present, so a
    project without Node never receives ``validation.node_test`` and a project
    that does not have this repository's two gate tests never receives
    ``validation.package_gate_pytest``. ``withheld`` reports every entry that
    was skipped and the measured reason, so a thin seed is legible rather than
    mysterious.
    """
    root, manager = _manager_context()
    if root is None:
        return manager
    if not core.writes_allowed():
        return _write_gate(manager)
    plan = seeding_plan(root)
    registered: list[dict[str, str]] = []
    already: list[dict[str, str]] = []
    failed: list[dict[str, Any]] = []
    for recipe in plan["selected"]:
        result = _persist(root, recipe, manager)
        entry = {"recipe_id": recipe.id, "version": recipe.version}
        if result.get("ok"):
            registered.append({**entry, "digest": str(result["digest"])})
        elif result.get("reason_code") == tr.REASON_DUPLICATE:
            already.append(entry)
        else:
            failed.append({**entry, "error": result.get("error")})
    return {
        "ok": not failed,
        "catalogue_size": len(CANONICAL_RECIPES),
        "selected_count": len(plan["selected"]),
        "registered": registered,
        "already_registered": already,
        "failed": failed,
        "withheld": plan["withheld"],
        "toolchain": plan["toolchain"],
        "manager": manager,
        "surface": "manager_mcp",
    }


def run(
    *,
    recipe_id: str,
    version: str | None = None,
    params: Mapping[str, Any] | None = None,
    grant_capabilities: Any = (),
) -> dict[str, Any]:
    """MANAGER EXECUTE: run one PERSISTED recipe and return a bounded digest.

    Read-only by default, and that is not a second policy invented here:
    ``tool_recipes.validate_invocation`` refuses any recipe whose declared
    capabilities the caller has not granted, exactly as ``discover`` does, so a
    recipe declaring ``write`` or ``network`` simply does not validate unless
    ``grant_capabilities`` names it. The repository write gate is applied on
    top of that for the same recipes -- a read-only recipe needs neither.

    What runs is the immutable stored manifest, not a mapping typed at call
    time. A non-zero exit is a RESULT, not an error: the digest carries the
    return code, the bounded tails and the path of the full output.
    """
    root, manager = _manager_context()
    if root is None:
        return manager
    wanted = str(recipe_id or "").strip()
    if not wanted:
        return {
            "ok": False,
            "error": "recipe_id must be a non-empty string",
            "reason_code": tr.REASON_UNKNOWN_RECIPE,
            "manager": manager,
            "surface": "manager_mcp",
        }
    if isinstance(grant_capabilities, (str, bytes)):
        return {
            "ok": False,
            "error": "grant_capabilities must be a list of strings, not a string",
            "reason_code": tr.REASON_INVALID_TYPE,
            "manager": manager,
            "surface": "manager_mcp",
        }
    granted = tuple(str(cap) for cap in (grant_capabilities or ()))

    try:
        registry = store.load_registry(root)
        recipe = registry.get(wanted, version)
    except tr.RecipeError as exc:
        return {
            "ok": False,
            "error": exc.message[:240],
            "reason_code": exc.reason,
            "manager": manager,
            "surface": "manager_mcp",
        }
    except (store.ToolRecipeStoreError, OSError, sqlite3.Error) as exc:
        return _store_failure(exc, manager)

    privileged = {tr.CAPABILITY_WRITE, tr.CAPABILITY_NETWORK} & set(recipe.capabilities)
    if privileged and not core.writes_allowed():
        gate = _write_gate(manager)
        gate["recipe_id"] = recipe.id
        gate["required_capabilities"] = sorted(privileged)
        return gate

    try:
        digest = recipe_runner.run_recipe(
            root,
            recipe,
            params or {},
            grant_capabilities=granted,
            actor=_actor(manager),
        )
    except tr.RecipeError as exc:
        return {
            "ok": False,
            "error": exc.message[:240],
            "reason_code": exc.reason,
            "recipe_id": recipe.id,
            "version": recipe.version,
            "required_capabilities": list(recipe.capabilities),
            "granted_capabilities": list(granted),
            "manager": manager,
            "surface": "manager_mcp",
        }
    except recipe_runner.RecipeRunError as exc:
        return {
            "ok": False,
            "error": exc.message[:240],
            "reason_code": exc.reason,
            "recipe_id": recipe.id,
            "version": recipe.version,
            "manager": manager,
            "surface": "manager_mcp",
        }
    return {"ok": True, **digest, "manager": manager, "surface": "manager_mcp"}


def list_registered(*, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0) -> dict[str, Any]:
    """MANAGER READ: bounded identity/contract listing of persisted manifests.

    An absent or empty store is a legitimate repository state and returns an
    empty list with ``registry_count`` zero. A stored row whose digest does not
    cover its payload is a different fact entirely, and the store raises rather
    than serving it; that surfaces here as an explicit store failure.
    """
    root, manager = _manager_context()
    if root is None:
        return manager
    bounded = max(1, min(int(limit), MAX_LIST_LIMIT))
    try:
        recipes = store.list_recipes(root, limit=bounded, offset=max(0, int(offset)))
    except (store.ToolRecipeStoreError, tr.RecipeError, OSError, sqlite3.Error) as exc:
        return _store_failure(exc, manager)
    return {
        "ok": True,
        "schema_id": store.SCHEMA_ID,
        "registry_count": len(recipes),
        "limit": bounded,
        "offset": max(0, int(offset)),
        "recipes": [_summary(recipe) for recipe in recipes],
        "manager": manager,
        "surface": "manager_mcp",
    }


def usage(
    *, limit: int = DEFAULT_LIST_LIMIT, since: str | None = None
) -> dict[str, Any]:
    """MANAGER READ: which registered recipes are actually being used, by whom.

    The registry could always say how many recipes existed, and once receipts
    landed the panel said ``measured`` -- of ROWS. It could not say whether
    anything USED them. Measured on this repository when that gap was found:
    22 recipes registered, 7 with any receipt at all, and every one of those
    receipts produced by verification agents exercising the plumbing within
    minutes of each other -- none by a model doing real work, and none
    attributable to anyone, because a receipt bound no actor.

    This joins the two halves that answer the real question. Every REGISTERED
    recipe appears, whether or not it has ever run, because "registered and
    never run" is the actionable state and is invisible in a list built from
    receipts alone. Per recipe: runs, how many DISTINCT verified actors ran it,
    first and last run, the exit-code distribution, and the bytes those runs
    returned to their callers.

    ``since`` is compared against the stored ISO-8601 UTC ``created_at``, so it
    bounds the window without a second time vocabulary. Note the asymmetry it
    creates and does not hide: ``registered_count`` is every stored manifest,
    while the run evidence is only the window -- a recipe last used before
    ``since`` reads as unused WITHIN THAT WINDOW, and ``window_only`` says so.
    """
    root, manager = _manager_context()
    if root is None:
        return manager
    bounded = max(1, min(int(limit), MAX_LIST_LIMIT))
    window = str(since).strip() if since is not None and str(since).strip() else None
    try:
        recipes = store.list_recipes(root, limit=MAX_LIST_LIMIT)
        measured = store.usage_by_recipe(
            root, limit=store.MAX_USAGE_LIMIT, since=window
        )
        totals = store.actor_totals(root, since=window)
    except (store.ToolRecipeStoreError, tr.RecipeError, OSError, sqlite3.Error) as exc:
        return _store_failure(exc, manager)

    # Keyed on (id, version), the store's own manifest identity. Keying on the
    # id alone would attach one usage row to every registered version of that
    # recipe and count a single used recipe as many times as it has versions --
    # and the operator recipes exist at two versions precisely because their
    # argv changed, so this case is live rather than theoretical.
    by_key = {(row["recipe_id"], row["version"]): row for row in measured}
    rows: list[dict[str, Any]] = []
    for recipe in recipes:
        row = by_key.get((recipe.id, recipe.version))
        rows.append(
            {
                "recipe_id": recipe.id,
                "version": recipe.version,
                "task_kind": recipe.task_kind.value,
                "risk_class": recipe.risk_class.value,
                # The distinction the panel exists to make. A registered recipe
                # nobody has run is not a degraded reading or a missing sample:
                # it is a measured, actionable fact, and folding it into a
                # green "measured" is the same defect as the "no sample" one.
                "used": row is not None,
                "runs": int(row["runs"]) if row else 0,
                "distinct_actors": int(row["distinct_actors"]) if row else 0,
                "attributed_runs": int(row["attributed_runs"]) if row else 0,
                "unattributed_runs": int(row["unattributed_runs"]) if row else 0,
                "first_run": row["first_run"] if row else "",
                "last_run": row["last_run"] if row else "",
                "returned_digest_bytes": (
                    int(row["returned_digest_bytes"]) if row else 0
                ),
                "exit_distribution": dict(row["exit_distribution"]) if row else {},
            }
        )
    # Receipts can outlive the manifest they name -- a version is immutable, but
    # nothing forces a store to keep a manifest a receipt refers to. Those runs
    # are real evidence and are reported rather than dropped, under an
    # explicitly separate key so they can never be mistaken for a registration.
    known = {(recipe.id, recipe.version) for recipe in recipes}
    unregistered = [
        row for row in measured if (row["recipe_id"], row["version"]) not in known
    ]

    used = sum(1 for row in rows if row["used"])
    rows.sort(key=lambda row: (-row["runs"], row["recipe_id"], row["version"]))
    return {
        "ok": True,
        "schema_id": store.RECEIPT_SCHEMA_ID,
        "registered_count": len(recipes),
        "used_count": used,
        "unused_count": len(recipes) - used,
        "run_count": int(totals["runs"]),
        "distinct_actor_count": int(totals["distinct_actors"]),
        "attributed_run_count": int(totals["attributed_runs"]),
        "unattributed_run_count": int(totals["unattributed_runs"]),
        "since": window or "",
        "window_only": window is not None,
        "limit": bounded,
        "returned_count": min(len(rows), bounded),
        "truncated": len(rows) > bounded,
        "recipes": rows[:bounded],
        "unregistered_usage": unregistered,
        "manager": manager,
        "surface": "manager_mcp",
    }


def show(*, recipe_id: str, version: str | None = None) -> dict[str, Any]:
    """MANAGER READ: one persisted manifest in its exact canonical form.

    With ``version`` omitted the highest stored version of ``recipe_id`` wins,
    resolved by the registry's own :func:`tool_recipes.version_key` ordering
    rather than by string comparison. The returned ``manifest`` is exactly the
    payload :func:`tool_recipes.recipe_digest` hashes, so a caller can verify
    the reported digest without trusting this surface.
    """
    root, manager = _manager_context()
    if root is None:
        return manager
    wanted = str(recipe_id or "").strip()
    if not wanted:
        return {
            "ok": False,
            "error": "recipe_id must be a non-empty string",
            "reason_code": tr.REASON_UNKNOWN_RECIPE,
            "manager": manager,
            "surface": "manager_mcp",
        }
    try:
        registry = store.load_registry(root)
        recipe = registry.get(wanted, version)
    except tr.RecipeError as exc:
        return {
            "ok": False,
            "error": exc.message[:240],
            "reason_code": exc.reason,
            "manager": manager,
            "surface": "manager_mcp",
        }
    except (store.ToolRecipeStoreError, OSError, sqlite3.Error) as exc:
        return _store_failure(exc, manager)
    return {
        "ok": True,
        "schema_id": store.SCHEMA_ID,
        **_summary(recipe),
        "manifest": tr.recipe_payload(recipe),
        "manager": manager,
        "surface": "manager_mcp",
    }


# ---------------------------------------------------------------------------
# The canonical catalogue.
#
# Every entry below describes an argv vector this repository's own machinery
# actually builds and hands to ``subprocess`` elsewhere.  The call site is named
# in each ``purpose`` string so the claim is checkable, and a reader who cannot
# find the named line should treat the entry as wrong rather than as documented
# intent: a recipe that does not match a real invocation makes the panel lie.
#
# Declaring these does not execute them.  Each is built here by the ordinary
# ``Recipe`` constructor, which enforces the module's shell-safety policy at
# import time -- an unsafe literal or a non-literal ``argv[0]`` in this file
# fails the import rather than reaching the store.
#
# ``capabilities`` is declared where the invocation genuinely mutates the
# repository or executes repository code.  That is load-bearing, not decorative:
# ``tool_recipes.discover`` refuses a recipe whose required capabilities the
# caller has not granted, so the read-only entries are the ones a zero-capability
# discovery returns.
#
# ``resource_bounds`` carries the timeout the calling code actually applies:
# ``worker_workspace.WORKTREE_CREATE_TIMEOUT_SECONDS`` (600s) for provisioning
# and ``worker_workspace.DEFAULT_FINALIZATION_GIT_TIMEOUT_SECONDS`` (5s) for the
# finalization probes.  The validation commands are run under a caller-supplied
# budget, so those entries declare no runtime bound rather than inventing one.
# ---------------------------------------------------------------------------

_WORKTREE_TIMEOUT_SECONDS = 600  # worker_workspace.WORKTREE_CREATE_TIMEOUT_SECONDS
_FINALIZATION_GIT_TIMEOUT_SECONDS = 5.0  # DEFAULT_FINALIZATION_GIT_TIMEOUT_SECONDS

_STDOUT_REPORT = tr.OutputSpec(
    name="report",
    type=tr.OutputType.STDOUT,
    description="Captured stdout/stderr of the invocation.",
)
_STDOUT_PATHS = tr.OutputSpec(
    name="paths",
    type=tr.OutputType.STDOUT,
    description="NUL-separated repository-relative paths on stdout.",
)
_EXIT_STATUS = tr.OutputSpec(
    name="exit_status",
    type=tr.OutputType.STDOUT,
    description="Process exit status; the invocation reports through it alone.",
)

_STDOUT_JSON = tr.OutputSpec(
    name="report",
    type=tr.OutputType.STDOUT,
    description="One bounded JSON object on stdout; exit 2 when unanswerable.",
)

_PATH_LIST = tr.ParamType.LIST


def _path_list(name: str, *, required: bool = True) -> tr.ParamSpec:
    return tr.ParamSpec(
        name=name,
        type=_PATH_LIST,
        required=required,
        item_type=tr.ParamType.PATH,
    )


def _limit(name: str = "limit", *, default: int, maximum: int) -> tr.ParamSpec:
    return tr.ParamSpec(
        name=name,
        type=tr.ParamType.INT,
        default=default,
        minimum=1,
        maximum=maximum,
    )


def _optional_filter(name: str) -> tr.ParamSpec:
    """An optional filter, spelled as a value rather than as an absent flag.

    ``render_argv`` expands an omitted LIST slot to zero arguments but REFUSES
    an omitted scalar slot, so an optional scalar filter needs a default or its
    flag would have no value to render. Every script here reads the literal
    ``all`` as "no filter", which keeps the argv shape fixed: the same token
    count every time, with the filter visible in the vector rather than
    inferred from its absence.
    """
    return tr.ParamSpec(name=name, type=tr.ParamType.STR, default="all")


CANONICAL_RECIPES: tuple[tr.Recipe, ...] = (
    # ---- Card validation commands -------------------------------------
    # task_templates._validation_commands_for builds each of these as a
    # space-joined string that the launcher later splits and runs.
    #
    # `" ".join([COMMAND_PYTHON, "-m", "pytest", "-q", *py_tests])`
    #   -- src/aiworkhub/task_templates.py:619-621 (COMMAND_PYTHON = "python",
    #   task_templates.py:93).
    tr.Recipe(
        id="aiworkhub.validation.pytest",
        version="1.0.0",
        purpose=(
            "Run a card's declared Python test paths. Emitted by "
            "task_templates._validation_commands_for (src/aiworkhub/"
            "task_templates.py:619-621) and executed by the launcher's "
            "validation runner."
        ),
        task_kind=tr.TaskKind.TEST,
        parameters=(_path_list("test_paths"),),
        outputs=(_STDOUT_REPORT,),
        capabilities=(tr.CAPABILITY_WRITE,),
        risk_class=tr.RiskClass.MEDIUM,
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("python"),
            tr.lit("-m"),
            tr.lit("pytest"),
            tr.lit("-q"),
            tr.slot("test_paths"),
        ),
    ),
    # `" ".join([COMMAND_PYTHON, "-m", "ruff", "check", *py_production, *py_tests])`
    #   -- src/aiworkhub/task_templates.py:623-627.  ``check`` without ``--fix``
    #   reads and reports only, so no capability is required.
    tr.Recipe(
        id="aiworkhub.validation.ruff_check",
        version="1.0.0",
        purpose=(
            "Lint a card's declared Python production and test paths. Emitted "
            "by task_templates._validation_commands_for (src/aiworkhub/"
            "task_templates.py:623-627). Read-only: no --fix is ever passed."
        ),
        task_kind=tr.TaskKind.LINT,
        parameters=(_path_list("check_paths"),),
        outputs=(_STDOUT_REPORT,),
        risk_class=tr.RiskClass.LOW,
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("python"),
            tr.lit("-m"),
            tr.lit("ruff"),
            tr.lit("check"),
            tr.slot("check_paths"),
        ),
    ),
    # `" ".join([COMMAND_NODE, "--test", *node_tests])`
    #   -- src/aiworkhub/task_templates.py:629 (COMMAND_NODE = "node",
    #   task_templates.py:94).
    tr.Recipe(
        id="aiworkhub.validation.node_test",
        version="1.0.0",
        purpose=(
            "Run a card's declared Node test paths with the Node built-in test "
            "runner. Emitted by task_templates._validation_commands_for "
            "(src/aiworkhub/task_templates.py:629)."
        ),
        task_kind=tr.TaskKind.TEST,
        parameters=(_path_list("test_paths"),),
        outputs=(_STDOUT_REPORT,),
        capabilities=(tr.CAPABILITY_WRITE,),
        risk_class=tr.RiskClass.MEDIUM,
        cache_policy=tr.CachePolicy.NEVER,
        argv=(tr.lit("node"), tr.lit("--test"), tr.slot("test_paths")),
    ),
    # `DIFF_CHECK_COMMAND = "git diff --check"`
    #   -- src/aiworkhub/task_templates.py:95, appended by
    #   _validation_commands_for (task_templates.py:631) for every template
    #   that declares generates_diff_check.
    tr.Recipe(
        id="aiworkhub.validation.git_diff_check",
        version="1.0.0",
        purpose=(
            "Reject whitespace errors and conflict markers in a card's diff. "
            "task_templates.DIFF_CHECK_COMMAND (src/aiworkhub/"
            "task_templates.py:95), appended at task_templates.py:631."
        ),
        task_kind=tr.TaskKind.LINT,
        outputs=(_EXIT_STATUS,),
        risk_class=tr.RiskClass.NONE,
        cache_policy=tr.CachePolicy.NEVER,
        argv=(tr.lit("git"), tr.lit("diff"), tr.lit("--check")),
    ),
    # `" ".join([COMMAND_PYTHON, "-m", "pytest", "-q", *PACKAGE_GATE_TESTS])`
    #   -- src/aiworkhub/task_templates.py:595, with PACKAGE_GATE_TESTS fixed at
    #   task_templates.py:585-588.  The targets are literal, so this recipe has
    #   no parameters at all and its argv is entirely literal tokens.
    tr.Recipe(
        id="aiworkhub.validation.package_gate_pytest",
        version="1.0.0",
        purpose=(
            "Run the two repository gates any src/aiworkhub/ change trips. "
            "task_templates._package_gate_command (src/aiworkhub/"
            "task_templates.py:595) over PACKAGE_GATE_TESTS "
            "(task_templates.py:585-588)."
        ),
        task_kind=tr.TaskKind.TEST,
        outputs=(_STDOUT_REPORT,),
        capabilities=(tr.CAPABILITY_WRITE,),
        risk_class=tr.RiskClass.MEDIUM,
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("python"),
            tr.lit("-m"),
            tr.lit("pytest"),
            tr.lit("-q"),
            tr.lit("tests/test_module_size_ratchet.py"),
            tr.lit("tests/test_declared_invariants.py"),
        ),
    ),
    # ---- Worker workspace provisioning --------------------------------
    # `worktree_argv = ["git", "worktree", "add", "--detach"]`, then
    # `worktree_argv.append("--no-checkout")` when no base OID is pinned, then
    # `worktree_argv.extend((str(path), pinned_base_oid or "HEAD"))`
    #   -- src/aiworkhub/worker_workspace.py:4355-4358.  The two branches are two
    #   different argv vectors, so they are two recipes rather than one recipe
    #   with a conditional literal.
    tr.Recipe(
        id="aiworkhub.workspace.git_worktree_add_sparse",
        version="1.0.0",
        purpose=(
            "Register a detached, unchecked-out worktree at HEAD for a worker, "
            "before the sparse definition is applied. The pinned_base_oid-is-"
            "None branch of src/aiworkhub/worker_workspace.py:4355-4358."
        ),
        task_kind=tr.TaskKind.GENERATE,
        parameters=(
            tr.ParamSpec(name="worktree_path", type=tr.ParamType.PATH, required=True),
        ),
        outputs=(_EXIT_STATUS,),
        capabilities=(tr.CAPABILITY_WRITE,),
        risk_class=tr.RiskClass.MEDIUM,
        resource_bounds=tr.ResourceBounds(
            max_runtime_seconds=_WORKTREE_TIMEOUT_SECONDS
        ),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("git"),
            tr.lit("worktree"),
            tr.lit("add"),
            tr.lit("--detach"),
            tr.lit("--no-checkout"),
            tr.slot("worktree_path"),
            tr.lit("HEAD"),
        ),
    ),
    tr.Recipe(
        id="aiworkhub.workspace.git_worktree_add_pinned",
        version="1.0.0",
        purpose=(
            "Register a detached worktree checked out at an exact pinned commit "
            "for a comparator tree. The pinned_base_oid-is-not-None branch of "
            "src/aiworkhub/worker_workspace.py:4355-4358."
        ),
        task_kind=tr.TaskKind.GENERATE,
        parameters=(
            tr.ParamSpec(name="worktree_path", type=tr.ParamType.PATH, required=True),
            tr.ParamSpec(name="base_oid", type=tr.ParamType.STR, required=True),
        ),
        outputs=(_EXIT_STATUS,),
        capabilities=(tr.CAPABILITY_WRITE,),
        risk_class=tr.RiskClass.MEDIUM,
        resource_bounds=tr.ResourceBounds(
            max_runtime_seconds=_WORKTREE_TIMEOUT_SECONDS
        ),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("git"),
            tr.lit("worktree"),
            tr.lit("add"),
            tr.lit("--detach"),
            tr.slot("worktree_path"),
            tr.slot("base_oid"),
        ),
    ),
    # The three-command sparse preparation sequence, in order:
    #   (["git", "sparse-checkout", "init", "--no-cone"], None)
    #   (["git", "sparse-checkout", "set", "--no-cone", "--stdin"], patterns)
    #   (["git", "read-tree", "-mu", "HEAD"], None)
    #   -- src/aiworkhub/worker_workspace.py:697-703, run by
    #   _prepare_sparse_worktree.
    tr.Recipe(
        id="aiworkhub.workspace.git_sparse_checkout_init",
        version="1.0.0",
        purpose=(
            "Initialise a non-cone sparse definition in a fresh worker "
            "worktree. First command of the sequence at "
            "src/aiworkhub/worker_workspace.py:698."
        ),
        task_kind=tr.TaskKind.GENERATE,
        outputs=(_EXIT_STATUS,),
        capabilities=(tr.CAPABILITY_WRITE,),
        risk_class=tr.RiskClass.LOW,
        resource_bounds=tr.ResourceBounds(
            max_runtime_seconds=_WORKTREE_TIMEOUT_SECONDS
        ),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("git"),
            tr.lit("sparse-checkout"),
            tr.lit("init"),
            tr.lit("--no-cone"),
        ),
    ),
    tr.Recipe(
        id="aiworkhub.workspace.git_sparse_checkout_set",
        version="1.0.0",
        purpose=(
            "Write the worker's exact sparse pattern set. Second command of the "
            "sequence at src/aiworkhub/worker_workspace.py:699-702; the patterns "
            "arrive on stdin (--stdin), never as argv, so this manifest declares "
            "no path parameter."
        ),
        task_kind=tr.TaskKind.GENERATE,
        outputs=(_EXIT_STATUS,),
        capabilities=(tr.CAPABILITY_WRITE,),
        risk_class=tr.RiskClass.MEDIUM,
        resource_bounds=tr.ResourceBounds(
            max_runtime_seconds=_WORKTREE_TIMEOUT_SECONDS
        ),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("git"),
            tr.lit("sparse-checkout"),
            tr.lit("set"),
            tr.lit("--no-cone"),
            tr.lit("--stdin"),
        ),
    ),
    tr.Recipe(
        id="aiworkhub.workspace.git_read_tree",
        version="1.0.0",
        purpose=(
            "Materialise the sparse working tree at HEAD. Third command of the "
            "sequence at src/aiworkhub/worker_workspace.py:701."
        ),
        task_kind=tr.TaskKind.GENERATE,
        outputs=(_EXIT_STATUS,),
        capabilities=(tr.CAPABILITY_WRITE,),
        risk_class=tr.RiskClass.MEDIUM,
        resource_bounds=tr.ResourceBounds(
            max_runtime_seconds=_WORKTREE_TIMEOUT_SECONDS
        ),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("git"),
            tr.lit("read-tree"),
            tr.lit("-mu"),
            tr.lit("HEAD"),
        ),
    ),
    # `["git", "sparse-checkout", "disable"]`
    #   -- src/aiworkhub/worker_workspace.py:4980, expanding the combined
    #   review tree to the full union before the overlays are applied.
    tr.Recipe(
        id="aiworkhub.workspace.git_sparse_checkout_disable",
        version="1.0.0",
        purpose=(
            "Expand a combined review tree from its sparse definition to the "
            "full HEAD union before candidate overlays are applied. "
            "src/aiworkhub/worker_workspace.py:4979-4984."
        ),
        task_kind=tr.TaskKind.GENERATE,
        outputs=(_EXIT_STATUS,),
        capabilities=(tr.CAPABILITY_WRITE,),
        risk_class=tr.RiskClass.MEDIUM,
        resource_bounds=tr.ResourceBounds(
            max_runtime_seconds=_WORKTREE_TIMEOUT_SECONDS
        ),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(tr.lit("git"), tr.lit("sparse-checkout"), tr.lit("disable")),
    ),
    # ---- Finalization probes (read-only) ------------------------------
    # `_run(["git", "diff", "--name-only", "-z", "HEAD"], cwd=repo)`
    #   -- src/aiworkhub/worker_workspace.py:4895, in
    #   _canonical_worktree_delta_paths.
    tr.Recipe(
        id="aiworkhub.finalization.git_diff_name_only",
        version="1.0.0",
        purpose=(
            "Read the tracked delta of the live canonical tree against HEAD. "
            "src/aiworkhub/worker_workspace.py:4895 "
            "(_canonical_worktree_delta_paths)."
        ),
        task_kind=tr.TaskKind.SCAN,
        outputs=(_STDOUT_PATHS,),
        risk_class=tr.RiskClass.NONE,
        resource_bounds=tr.ResourceBounds(
            max_runtime_seconds=_FINALIZATION_GIT_TIMEOUT_SECONDS
        ),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("git"),
            tr.lit("diff"),
            tr.lit("--name-only"),
            tr.lit("-z"),
            tr.lit("HEAD"),
        ),
    ),
    # `_run(["git", "diff", "--name-only", "--no-renames", "-z", diff_ref], ...)`
    #   -- src/aiworkhub/worker_workspace.py:5257.  ``--no-renames`` is
    #   load-bearing: without it a staged rename hides its deleted source from
    #   the write-scope check (comment at worker_workspace.py:5251-5256).
    tr.Recipe(
        id="aiworkhub.finalization.git_diff_name_only_no_renames",
        version="1.0.0",
        purpose=(
            "Read a candidate worktree's tracked delta against its base ref "
            "with rename detection off, so a staged rename reports both sides. "
            "src/aiworkhub/worker_workspace.py:5256-5261."
        ),
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            tr.ParamSpec(name="diff_ref", type=tr.ParamType.STR, required=True),
        ),
        outputs=(_STDOUT_PATHS,),
        risk_class=tr.RiskClass.NONE,
        resource_bounds=tr.ResourceBounds(
            max_runtime_seconds=_FINALIZATION_GIT_TIMEOUT_SECONDS
        ),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("git"),
            tr.lit("diff"),
            tr.lit("--name-only"),
            tr.lit("--no-renames"),
            tr.lit("-z"),
            tr.slot("diff_ref"),
        ),
    ),
    # `_run(["git", "ls-files", "--others", "--exclude-standard", "-z"], ...)`
    #   -- src/aiworkhub/worker_workspace.py:4899 and 5265.
    tr.Recipe(
        id="aiworkhub.finalization.git_ls_files_untracked",
        version="1.0.0",
        purpose=(
            "Read the untracked, non-ignored files of a tree as the other half "
            "of its delta. src/aiworkhub/worker_workspace.py:4899 and "
            "worker_workspace.py:5265."
        ),
        task_kind=tr.TaskKind.SCAN,
        outputs=(_STDOUT_PATHS,),
        risk_class=tr.RiskClass.NONE,
        resource_bounds=tr.ResourceBounds(
            max_runtime_seconds=_FINALIZATION_GIT_TIMEOUT_SECONDS
        ),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("git"),
            tr.lit("ls-files"),
            tr.lit("--others"),
            tr.lit("--exclude-standard"),
            tr.lit("-z"),
        ),
    ),
    # `_run(["git", "merge-base", "--is-ancestor", workspace.base_oid, "HEAD"], ...)`
    #   -- src/aiworkhub/worker_workspace.py:5240.  Reached only when the
    #   worktree's administrative HEAD no longer equals its recorded base OID.
    tr.Recipe(
        id="aiworkhub.finalization.git_merge_base_is_ancestor",
        version="1.0.0",
        purpose=(
            "Explain a moved worktree HEAD by proving the recorded base OID is "
            "still an ancestor of it; a non-zero status is an unexplained move. "
            "src/aiworkhub/worker_workspace.py:5239-5244."
        ),
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            tr.ParamSpec(name="base_oid", type=tr.ParamType.STR, required=True),
        ),
        outputs=(_EXIT_STATUS,),
        risk_class=tr.RiskClass.NONE,
        resource_bounds=tr.ResourceBounds(
            max_runtime_seconds=_FINALIZATION_GIT_TIMEOUT_SECONDS
        ),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("git"),
            tr.lit("merge-base"),
            tr.lit("--is-ancestor"),
            tr.slot("base_oid"),
            tr.lit("HEAD"),
        ),
    ),
    # ---- Packaged operator modules -------------------------------------
    #
    # The entries above describe invocations AIWorkHub performs on its own.
    # These describe invocations a MODEL performs, and until now performed by
    # typing a fresh Python heredoc every single time. Measured over 27 manager
    # transcripts: 4,962 Bash calls, of which 2,404 (48%) were ad-hoc Python
    # (1,646 heredocs plus 758 ``python -c``) and only 164 (3%) reused any
    # checked-in script. The targets of those ad-hoc scripts were, in order:
    # runtime logs (589), the task queue database (517), repository files
    # (388), importing aiworkhub modules (195), running tests (139) and
    # parsing spilled MCP JSON (42).
    #
    # Each recipe below runs a module of the ``aiworkhub.recipes`` package that
    # does one of those jobs once, correctly, with explicit typed arguments and
    # bounded JSON output. Every module was verified against this repository's
    # REAL on-disk schema and layout before it was registered -- the task queue
    # opened read-only and its columns read, one process-log directory listed,
    # one attempt-artifacts tree opened -- because a recipe that does not match
    # reality makes the panel lie, which is the exact failure this catalogue
    # exists to end.
    #
    # ``python -m aiworkhub.recipes.<name>``, at version 2.0.0, because 1.0.0
    # rendered ``python scripts/recipes/<name>.py`` -- a path relative to
    # AIWORKHUB'S OWN checkout. In a repository AIWorkHub merely MANAGES that
    # file does not exist, so every one of these recipes registered, validated,
    # rendered a correct-looking argv and could only fail at run time, while
    # the per-project data the module reads (``.aiworkhub/tasking/
    # task_queue.sqlite``, ``.aiworkhub/runtime/``) was sitting there correct.
    # A module path travels with the installed package, so the same manifest is
    # valid everywhere. The version bump is not cosmetic: a stored ``(id,
    # version)`` is immutable, so re-registering 1.0.0 with a different argv
    # would be REFUSED and every store seeded before this change would keep
    # serving the unrunnable vector forever.
    #
    # ``argv[0]`` is the literal ``python``; ``recipe_runner.resolve_executable``
    # binds that name to the repository's own ``.venv`` interpreter, because
    # ``python`` is not on PATH on this host and ``python3`` would run outside
    # the virtualenv.
    #
    # All are read-only and declare no capability EXCEPT
    # ``aiworkhub.operator.repo_test_subset``, which executes repository code.
    tr.Recipe(
        id="aiworkhub.operator.task_events",
        version="2.0.0",
        purpose=(
            "Bounded task_events rows for one card. Replaces the hand-typed "
            "sqlite heredoc against .aiworkhub/tasking/task_queue.sqlite (517 "
            "of 4,962 measured manager Bash calls). Runs "
            "src/aiworkhub/recipes/task_events.py as a package module, so it is "
            "valid in any managed repository; read-only via an immutable=1 "
            "connection. See src/aiworkhub/manager_recipe_tools.py for the "
            "catalogue this belongs to."
        ),
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            tr.ParamSpec(name="task_id", type=tr.ParamType.STR, required=True),
            _optional_filter("event"),
            _limit(default=50, maximum=500),
        ),
        outputs=(_STDOUT_JSON,),
        risk_class=tr.RiskClass.NONE,
        resource_bounds=tr.ResourceBounds(max_runtime_seconds=60),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("python"),
            tr.lit("-m"),
            tr.lit("aiworkhub.recipes.task_events"),
            tr.lit("--task-id"),
            tr.slot("task_id"),
            tr.lit("--event"),
            tr.slot("event"),
            tr.lit("--limit"),
            tr.slot("limit"),
        ),
    ),
    tr.Recipe(
        id="aiworkhub.operator.usage_rollup",
        version="2.0.0",
        purpose=(
            "Aggregate usage_record rows by task, runner, topic, model, role or "
            "day. Replaces the hand-typed cost roll-up, which repeatedly guesses "
            "a usage_record TABLE: it is an EVENT KIND in task_events (6,660 "
            "rows measured). Runs src/aiworkhub/recipes/usage_rollup.py as a "
            "package module; read-only. "
            "Catalogue: src/aiworkhub/manager_recipe_tools.py."
        ),
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            _optional_filter("task_id"),
            _optional_filter("since"),
            tr.ParamSpec(
                name="group_by",
                type=tr.ParamType.ENUM,
                default="runner",
                values=("task", "runner", "topic", "model", "role", "day"),
            ),
            _limit(default=25, maximum=200),
        ),
        outputs=(_STDOUT_JSON,),
        risk_class=tr.RiskClass.NONE,
        resource_bounds=tr.ResourceBounds(max_runtime_seconds=120),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("python"),
            tr.lit("-m"),
            tr.lit("aiworkhub.recipes.usage_rollup"),
            tr.lit("--task-id"),
            tr.slot("task_id"),
            tr.lit("--since"),
            tr.slot("since"),
            tr.lit("--group-by"),
            tr.slot("group_by"),
            tr.lit("--limit"),
            tr.slot("limit"),
        ),
    ),
    tr.Recipe(
        id="aiworkhub.operator.request_log_tail",
        version="2.0.0",
        purpose=(
            "Bounded tail of one request's captured stream plus its supervisor "
            "summary. Replaces the hand-typed tail of "
            ".aiworkhub/runtime/process_logs/processes/<id>.<stream>.log (589 "
            "of 4,962 measured manager Bash calls targeted runtime logs). Runs "
            "src/aiworkhub/recipes/request_log_tail.py as a package module; "
            "read-only. Catalogue: src/aiworkhub/manager_recipe_tools.py."
        ),
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            tr.ParamSpec(name="request_id", type=tr.ParamType.STR, required=True),
            tr.ParamSpec(
                name="stream",
                type=tr.ParamType.ENUM,
                default="stdout",
                values=("stdout", "stderr"),
            ),
            _limit("tail_bytes", default=8192, maximum=131072),
        ),
        outputs=(_STDOUT_JSON,),
        risk_class=tr.RiskClass.NONE,
        resource_bounds=tr.ResourceBounds(max_runtime_seconds=60),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("python"),
            tr.lit("-m"),
            tr.lit("aiworkhub.recipes.request_log_tail"),
            tr.lit("--request-id"),
            tr.slot("request_id"),
            tr.lit("--stream"),
            tr.slot("stream"),
            tr.lit("--bytes"),
            tr.slot("tail_bytes"),
        ),
    ),
    tr.Recipe(
        id="aiworkhub.operator.attempt_validation",
        version="2.0.0",
        purpose=(
            "One request's validation payload folded to per-check argv, "
            "returncode, failure_class and diagnostic_tail. Replaces reading a "
            "405 KB attempt-artifacts/<id>/validation.json by hand; "
            "failure_class lives inside each check's failure_receipt and is "
            "absent on a passing check. Runs "
            "src/aiworkhub/recipes/attempt_validation.py as a package module; "
            "read-only. Catalogue: src/aiworkhub/manager_recipe_tools.py."
        ),
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            tr.ParamSpec(name="request_id", type=tr.ParamType.STR, required=True),
            _limit("tail_chars", default=2000, maximum=20000),
        ),
        outputs=(_STDOUT_JSON,),
        risk_class=tr.RiskClass.NONE,
        resource_bounds=tr.ResourceBounds(max_runtime_seconds=60),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("python"),
            tr.lit("-m"),
            tr.lit("aiworkhub.recipes.attempt_validation"),
            tr.lit("--request-id"),
            tr.slot("request_id"),
            tr.lit("--tail-chars"),
            tr.slot("tail_chars"),
        ),
    ),
    tr.Recipe(
        id="aiworkhub.operator.worktree_diff",
        version="2.0.0",
        purpose=(
            "Bounded unified diff of a retained candidate worktree against the "
            "canonical tree, from the launcher's own changed_paths. Replaces "
            "reading candidate files by hand (388 of 4,962 measured manager "
            "Bash calls read repository files) and never edits the worktree, "
            "whose hashes are the review evidence. Runs "
            "src/aiworkhub/recipes/worktree_diff.py as a package module; "
            "read-only. Catalogue: src/aiworkhub/manager_recipe_tools.py."
        ),
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            tr.ParamSpec(name="request_id", type=tr.ParamType.STR, required=True),
            _optional_filter("path"),
            _limit("max_bytes", default=60000, maximum=400000),
        ),
        outputs=(_STDOUT_JSON,),
        risk_class=tr.RiskClass.NONE,
        resource_bounds=tr.ResourceBounds(max_runtime_seconds=120),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("python"),
            tr.lit("-m"),
            tr.lit("aiworkhub.recipes.worktree_diff"),
            tr.lit("--request-id"),
            tr.slot("request_id"),
            tr.lit("--path"),
            tr.slot("path"),
            tr.lit("--max-bytes"),
            tr.slot("max_bytes"),
        ),
    ),
    tr.Recipe(
        id="aiworkhub.operator.process_liveness",
        version="2.0.0",
        purpose=(
            "Measured pid liveness for launched requests plus the reconciler "
            "heartbeat age from .aiworkhub/runtime/task_reconciler_status.json. "
            "Replaces the hand-typed check that a 'processing' card has a "
            "running process; a claimed state is not a measurement. Runs "
            "src/aiworkhub/recipes/process_liveness.py as a package module; "
            "read-only. Catalogue: src/aiworkhub/manager_recipe_tools.py."
        ),
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            _optional_filter("request_id"),
            _limit("scan", default=200, maximum=2000),
        ),
        outputs=(_STDOUT_JSON,),
        risk_class=tr.RiskClass.NONE,
        resource_bounds=tr.ResourceBounds(max_runtime_seconds=120),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("python"),
            tr.lit("-m"),
            tr.lit("aiworkhub.recipes.process_liveness"),
            tr.lit("--request-id"),
            tr.slot("request_id"),
            tr.lit("--scan"),
            tr.slot("scan"),
        ),
    ),
    tr.Recipe(
        id="aiworkhub.operator.repo_test_subset",
        version="2.0.0",
        purpose=(
            "Run the canonical .venv interpreter's pytest on exactly the given "
            "paths with -q --tb=short. Replaces the hand-typed pytest command "
            "(139 of 4,962 measured manager Bash calls ran tests), whose "
            "recurring failure is the interpreter: 'python' is not on PATH here "
            "and 'python3' runs outside the repository .venv. Runs "
            "src/aiworkhub/recipes/repo_test_subset.py as a package module; "
            "declares CAPABILITY_WRITE because it executes repository code. "
            "Catalogue: src/aiworkhub/manager_recipe_tools.py."
        ),
        task_kind=tr.TaskKind.TEST,
        parameters=(_path_list("paths"),),
        outputs=(_STDOUT_JSON,),
        capabilities=(tr.CAPABILITY_WRITE,),
        risk_class=tr.RiskClass.MEDIUM,
        resource_bounds=tr.ResourceBounds(max_runtime_seconds=900),
        cache_policy=tr.CachePolicy.NEVER,
        argv=(
            tr.lit("python"),
            tr.lit("-m"),
            tr.lit("aiworkhub.recipes.repo_test_subset"),
            tr.lit("--paths"),
            tr.slot("paths"),
        ),
    ),
)

# The module path prefix every packaged operator recipe runs through. A recipe
# is "operator" exactly when its argv is ``python -m <this>.<name>``; nothing
# else in the catalogue uses ``-m``.
OPERATOR_MODULE_PREFIX = "aiworkhub.recipes."

# Every recipe whose argv names a packaged operator module, mapped to that
# module's dotted name. A test walks this to prove each module both exists and
# IMPORTS: a manifest naming a module nobody wrote would register, validate,
# render a perfectly shaped argv and then fail only at run time -- which is
# precisely what the ``scripts/recipes/<name>.py`` argv this replaced did in
# every repository except AIWorkHub's own.
OPERATOR_SCRIPT_PATHS: tuple[tuple[str, str], ...] = tuple(
    (recipe.id, token.text)
    for recipe in CANONICAL_RECIPES
    for token in recipe.argv[2:3]
    if isinstance(token, tr.ArgvLiteral)
    and token.text.startswith(OPERATOR_MODULE_PREFIX)
)


# ---------------------------------------------------------------------------
# Per-project seeding.
#
# ``seed_canonical`` used to install this whole catalogue into whatever
# repository it was pointed at. That is right for the 18 entries whose
# invocation is true of any git repository AIWorkHub manages, and wrong for the
# four that are not:
#
#   * ``validation.pytest`` / ``validation.ruff_check`` describe commands whose
#     TOOLCHAIN the target project may not have. Seeding them into a project
#     without pytest registers a manifest that can only fail.
#   * ``validation.node_test`` describes a Node command. A Python-only project
#     that never emits it gets a permanently unused recipe -- and an unused
#     recipe is exactly what the usage surface above exists to expose, so
#     manufacturing one at seed time would be poisoning the measurement.
#   * ``validation.package_gate_pytest`` names THIS repository's own two test
#     files as argv literals. In any other project those paths do not exist.
#
# The question "does this project have that" is already answered by
# ``toolchain_authority``, which resolves a card's declared validation commands
# against the target repository, reports which validator MODULES the resolved
# interpreter actually supplies, and reports repository-relative inputs that are
# absent. It is asked here rather than re-implemented: a second detector would
# be a second opinion, and the launcher's opinion is the one that decides
# whether a validation command can run at all.
#
# The project's toolchain registry (``aiworkhub.toolchain.json``, the same file
# ``toolchain_authority`` reads) is treated as the project's DECLARATION. A tool
# that merely happens to be on the host PATH is not enough: node is installed on
# this developer machine, so a Python-only project probed here resolves ``node``
# and would still be wrong to receive ``validation.node_test``. Declared AND
# resolvable, or the recipe is not seeded.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecipeRequirement:
    """What one catalogue entry needs from a project before it may be seeded.

    ``probe_command`` is the validation command handed to
    ``toolchain_authority`` -- the head of the exact argv the recipe renders,
    without the caller-supplied paths, so the probe measures the toolchain and
    never a parameter nobody has supplied yet.
    """

    recipe_id: str
    probe_command: str
    modules: tuple[str, ...] = ()
    executables: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


CONDITIONAL_REQUIREMENTS: tuple[RecipeRequirement, ...] = (
    RecipeRequirement(
        recipe_id="aiworkhub.validation.pytest",
        probe_command="python -m pytest -q",
        modules=("pytest",),
    ),
    RecipeRequirement(
        recipe_id="aiworkhub.validation.ruff_check",
        probe_command="python -m ruff check",
        modules=("ruff",),
    ),
    RecipeRequirement(
        recipe_id="aiworkhub.validation.node_test",
        probe_command="node --test",
        executables=("node",),
    ),
    # The one entry whose argv names files rather than a toolchain. Its two
    # literals are this repository's own gates; a project that does not have
    # them would register a manifest guaranteed to fail on its first run.
    RecipeRequirement(
        recipe_id="aiworkhub.validation.package_gate_pytest",
        probe_command=(
            "python -m pytest -q tests/test_module_size_ratchet.py "
            "tests/test_declared_invariants.py"
        ),
        modules=("pytest",),
        paths=(
            "tests/test_module_size_ratchet.py",
            "tests/test_declared_invariants.py",
        ),
    ),
)

CONDITIONAL_RECIPE_IDS: frozenset[str] = frozenset(
    requirement.recipe_id for requirement in CONDITIONAL_REQUIREMENTS
)

# Valid in any git repository AIWorkHub manages: the git workspace,
# finalization and diff probes, plus the operator modules, which travel with the
# installed package and read per-project ``.aiworkhub/`` data.
UNIVERSAL_RECIPES: tuple[tr.Recipe, ...] = tuple(
    recipe for recipe in CANONICAL_RECIPES if recipe.id not in CONDITIONAL_RECIPE_IDS
)
CONDITIONAL_RECIPES: tuple[tr.Recipe, ...] = tuple(
    recipe for recipe in CANONICAL_RECIPES if recipe.id in CONDITIONAL_RECIPE_IDS
)

_MISSING_PATH_MARKER = ":path="


def _project_evidence(root: Path) -> dict[str, Any]:
    """Measure the target project's toolchain once, through the authority.

    One evaluation for every conditional probe command together, because
    ``ToolchainAuthority.evaluate`` caches per (registry, command-set) identity
    and four separate evaluations would be four cache misses measuring the same
    host.

    Never raises: a project whose toolchain cannot be evaluated at all is
    reported with ``available`` false and every conditional recipe is then
    withheld, which is the fail-closed direction -- an unseeded recipe is a
    missing affordance, a wrongly seeded one is a manifest that lies.
    """
    from . import toolchain_authority

    evidence: dict[str, Any] = {
        "available": False,
        "declared": (),
        "declares_toolchain": False,
        "modules": frozenset(),
        "resolved": frozenset(),
        "missing_modules": frozenset(),
        "missing_executables": frozenset(),
        "absent_paths": frozenset(),
        "error": "",
    }
    try:
        registry = toolchain_authority._load_project_registry(root)
        snapshot = toolchain_authority.ToolchainAuthority(root).evaluate(
            {"validation": [r.probe_command for r in CONDITIONAL_REQUIREMENTS]}
        )
    except Exception as exc:  # noqa: BLE001 - an unmeasurable host withholds, never guesses
        evidence["error"] = f"{type(exc).__name__}: {exc}"[:240]
        return evidence
    missing_modules: set[str] = set()
    missing_executables: set[str] = set()
    absent_paths: set[str] = set()
    for entry in snapshot.missing:
        value = str(entry.value)
        if entry.kind == "module":
            missing_modules.add(value.rsplit(":", 1)[-1])
        elif entry.kind in ("executable", "version"):
            missing_executables.add(value.split(">=", 1)[0].rsplit(":", 1)[-1])
        elif entry.kind == "repository_input" and _MISSING_PATH_MARKER in value:
            absent_paths.add(value.split(_MISSING_PATH_MARKER, 1)[1])
    evidence.update(
        available=True,
        declared=tuple(sorted(item.name for item in registry.requirements)),
        declares_toolchain=bool(registry.fingerprint),
        modules=frozenset(snapshot.modules),
        resolved=frozenset(fact.requested for fact in snapshot.executables),
        missing_modules=frozenset(missing_modules),
        missing_executables=frozenset(missing_executables),
        absent_paths=frozenset(absent_paths),
    )
    return evidence


def _requirement_verdict(
    root: Path, requirement: RecipeRequirement, evidence: Mapping[str, Any]
) -> tuple[bool, list[str]]:
    """Decide one conditional recipe, returning ``(seed, reasons_not_to)``."""
    if not evidence["available"]:
        return False, [f"toolchain_unmeasurable:{evidence['error'] or 'unknown'}"]
    reasons: list[str] = []
    for module in requirement.modules:
        if module in evidence["missing_modules"]:
            reasons.append(f"module_absent:{module}")
        elif module not in evidence["modules"]:
            reasons.append(f"module_unresolved:{module}")
    for program in requirement.executables:
        # Declaration first, availability second. A tool on the host PATH that
        # the project never declared is a fact about this machine, not about
        # the project, and seeding from it is how a Python-only repository ends
        # up owning a Node recipe it will never run.
        if evidence["declares_toolchain"] and program not in evidence["declared"]:
            reasons.append(f"toolchain_not_declared:{program}")
        elif program in evidence["missing_executables"]:
            reasons.append(f"executable_missing:{program}")
        elif program not in evidence["resolved"]:
            reasons.append(f"executable_unresolved:{program}")
    for relative in requirement.paths:
        if relative in evidence["absent_paths"] or not (root / relative).is_file():
            reasons.append(f"path_absent:{relative}")
    return not reasons, reasons


def seeding_plan(root: str | Path) -> dict[str, Any]:
    """What ``seed_canonical`` would install into ``root``, and why not the rest.

    Pure measurement: reads the target project's toolchain and paths and writes
    nothing. Exposed separately from :func:`seed_canonical` so "what would a new
    project receive" is answerable without seeding it -- and so the decision can
    be tested for a project with and without each toolchain.
    """
    target = Path(root).resolve()
    evidence = _project_evidence(target)
    selected: list[tr.Recipe] = list(UNIVERSAL_RECIPES)
    eligible: list[dict[str, Any]] = [
        {"recipe_id": recipe.id, "version": recipe.version, "reason": "universal"}
        for recipe in UNIVERSAL_RECIPES
    ]
    withheld: list[dict[str, Any]] = []
    by_id = {recipe.id: recipe for recipe in CANONICAL_RECIPES}
    for requirement in CONDITIONAL_REQUIREMENTS:
        recipe = by_id[requirement.recipe_id]
        seed, reasons = _requirement_verdict(target, requirement, evidence)
        entry = {"recipe_id": recipe.id, "version": recipe.version}
        if seed:
            selected.append(recipe)
            eligible.append({**entry, "reason": "toolchain_present"})
        else:
            withheld.append({**entry, "reasons": reasons})
    return {
        "repo": str(target),
        "catalogue_size": len(CANONICAL_RECIPES),
        "universal_count": len(UNIVERSAL_RECIPES),
        "conditional_count": len(CONDITIONAL_REQUIREMENTS),
        "selected": tuple(selected),
        "eligible": eligible,
        "withheld": withheld,
        "toolchain": {
            "measured": bool(evidence["available"]),
            "declares_toolchain": bool(evidence["declares_toolchain"]),
            "declared": list(evidence["declared"]),
            "modules": sorted(evidence["modules"]),
            "missing_modules": sorted(evidence["missing_modules"]),
            "missing_executables": sorted(evidence["missing_executables"]),
            "error": evidence["error"],
        },
    }


def seed_plan() -> dict[str, Any]:
    """MANAGER READ: which catalogue entries this repository would receive.

    The same decision :func:`seed_canonical` makes, without making it. A
    manager onboarding a new project can see exactly what that project gets and
    the measured reason for every entry it does not, before anything is written.
    """
    root, manager = _manager_context()
    if root is None:
        return manager
    plan = seeding_plan(root)
    plan.pop("selected", None)
    return {"ok": True, **plan, "manager": manager, "surface": "manager_mcp"}


__all__ = [
    "CANONICAL_RECIPES",
    "CONDITIONAL_RECIPES",
    "CONDITIONAL_REQUIREMENTS",
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "OPERATOR_MODULE_PREFIX",
    "OPERATOR_SCRIPT_PATHS",
    "UNIVERSAL_RECIPES",
    "RecipeRequirement",
    "list_registered",
    "register",
    "run",
    "seed_canonical",
    "seed_plan",
    "seeding_plan",
    "show",
    "usage",
]
