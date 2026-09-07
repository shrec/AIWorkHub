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
from pathlib import Path
from typing import Any

from . import core
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
    """
    root, manager = _manager_context()
    if root is None:
        return manager
    if not core.writes_allowed():
        return _write_gate(manager)
    registered: list[dict[str, str]] = []
    already: list[dict[str, str]] = []
    failed: list[dict[str, Any]] = []
    for recipe in CANONICAL_RECIPES:
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
        "registered": registered,
        "already_registered": already,
        "failed": failed,
        "manager": manager,
        "surface": "manager_mcp",
    }


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

_PATH_LIST = tr.ParamType.LIST


def _path_list(name: str, *, required: bool = True) -> tr.ParamSpec:
    return tr.ParamSpec(
        name=name,
        type=_PATH_LIST,
        required=required,
        item_type=tr.ParamType.PATH,
    )


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
)


__all__ = [
    "CANONICAL_RECIPES",
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "list_registered",
    "register",
    "seed_canonical",
    "show",
]
