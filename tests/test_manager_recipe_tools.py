"""Tests for the manager-bound tool recipe registration surface.

Covers the acceptance contract: registration is manager-gated and write-gated;
a manifest is built from caller fields only and an invalid one is refused with
``tool_recipes``' own stable reason code without any write; a duplicate
``(id, version)`` is refused and leaves the stored row untouched; the read
surfaces return what was stored; the canonical catalogue is installable,
idempotent and structurally honest (no execution, literal argv[0], no shell
metacharacters); and the dashboard's own ``tool_recipes`` projection flips from
``no_sample`` to ``measured``/``available`` with a real ``registry_count`` once
the store is populated.

Nothing here executes a described invocation. The recipes are descriptions and
these tests only ever construct, persist, read back and project them.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import aiworkhub.core as core
from aiworkhub import dashboard
from aiworkhub import manager_recipe_tools as mrt
from aiworkhub import tool_recipes as tr
from aiworkhub import tool_recipes_store as store

# One minimal, valid caller manifest in the exact shape ``recipe_payload``
# emits, so a test's fixture and the module's own round-trip agree.
BASE_MANIFEST = {
    "id": "example.echo",
    "version": "1.0.0",
    "purpose": "A caller-defined manifest used only by these tests.",
    "task_kind": "lint",
    "parameters": [
        {
            "name": "target",
            "type": "path",
            "required": True,
            "values": [],
            "item_values": [],
        }
    ],
    "outputs": [{"name": "report", "type": "stdout", "description": ""}],
    "platforms": [],
    "capabilities": [],
    "risk_class": "none",
    "resource_bounds": {},
    "cache_policy": "never",
    "argv": [["literal", "git"], ["literal", "diff"], ["slot", "target"]],
}


def _manifest(**overrides):
    payload = json.loads(json.dumps(BASE_MANIFEST))
    payload.update(overrides)
    return payload


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """Install a verified manager identity rooted at an isolated repository."""
    route = {
        "role": "manager",
        "provider": "claude",
        "repo": str(tmp_path),
        "manager_route": {"thread_id": "sess-recipes", "provider": "claude"},
    }
    monkeypatch.setattr(core, "manager_bootstrap", lambda: route)
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Manager gating
# ---------------------------------------------------------------------------


def test_registration_requires_a_verified_manager_route(tmp_path, monkeypatch):
    """An unverified route registers nothing and creates no database."""
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {"role": "worker"})
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)

    result = mrt.register(manifest=_manifest())

    assert result["ok"] is False
    assert result["error"] == "verified_manager_identity_required"
    assert not (tmp_path / ".aiworkhub" / "tasking" / "tool_recipes.sqlite").exists()


def test_registration_requires_a_manager_session_identity(tmp_path, monkeypatch):
    """A manager route with no session identity is refused, not defaulted."""
    monkeypatch.setattr(
        core,
        "manager_bootstrap",
        lambda: {"role": "manager", "repo": str(tmp_path), "manager_route": {}},
    )
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)

    result = mrt.register(manifest=_manifest())

    assert result["ok"] is False
    assert result["error"] == "manager_session_identity_missing"


def test_registration_respects_the_write_gate(manager, monkeypatch):
    """A closed write gate refuses before the store is touched."""
    monkeypatch.setattr(core, "writes_allowed", lambda: False)

    result = mrt.register(manifest=_manifest())

    assert result["ok"] is False
    assert result["error"] == "write_gate_closed"
    assert store.list_recipes(manager) == []


def test_read_surfaces_are_manager_gated(tmp_path, monkeypatch):
    """The read tools refuse an unverified route exactly as the write tool does."""
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {"role": "worker"})
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)

    assert mrt.list_registered()["error"] == "verified_manager_identity_required"
    assert mrt.show(recipe_id="example.echo")["error"] == (
        "verified_manager_identity_required"
    )


# ---------------------------------------------------------------------------
# Manifest validation: stable reason codes, nothing written
# ---------------------------------------------------------------------------


def test_valid_manifest_is_persisted_and_readable(manager):
    """A registered manifest round-trips with its digest unchanged."""
    result = mrt.register(manifest=_manifest())

    assert result["ok"] is True
    assert result["recipe_id"] == "example.echo"
    assert result["version"] == "1.0.0"

    stored = store.get_recipe(manager, "example.echo", "1.0.0")
    assert stored is not None
    assert stored.digest == result["digest"]
    assert store.stored_digest(manager, "example.echo", "1.0.0") == result["digest"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        # argv[0] must be a literal: the program identity is fixed by the
        # manifest, never chosen by a parameter.
        (
            {"argv": [["slot", "target"], ["literal", "diff"]]},
            tr.REASON_NON_LITERAL_EXECUTABLE,
        ),
        # A shell composition cannot be produced by this layer even by accident.
        (
            {"argv": [["literal", "git"], ["literal", "diff;rm"], ["slot", "target"]]},
            tr.REASON_UNSAFE_LITERAL,
        ),
        (
            {"argv": [["literal", "git"], ["literal", "$HOME"], ["slot", "target"]]},
            tr.REASON_UNSAFE_LITERAL,
        ),
        # An argv slot naming a parameter that does not exist.
        (
            {"argv": [["literal", "git"], ["slot", "nope"]]},
            tr.REASON_UNKNOWN_PARAMETER,
        ),
        ({"argv": []}, tr.REASON_EMPTY_ARGV),
        ({"version": "not-a-version"}, tr.REASON_BAD_MANIFEST),
        ({"task_kind": "teleport"}, tr.REASON_BAD_MANIFEST),
        ({"risk_class": "apocalyptic"}, tr.REASON_BAD_MANIFEST),
    ],
)
def test_invalid_manifest_is_refused_with_its_stable_reason(manager, overrides, reason):
    """Each refusal carries the tool_recipes reason code, and writes nothing."""
    result = mrt.register(manifest=_manifest(**overrides))

    assert result["ok"] is False
    assert result["reason_code"] == reason
    assert store.list_recipes(manager) == []


def test_a_non_mapping_manifest_is_a_bad_manifest(manager):
    result = mrt.register(manifest=["git", "diff"])

    assert result["ok"] is False
    assert result["reason_code"] == tr.REASON_BAD_MANIFEST
    assert store.list_recipes(manager) == []


def test_duplicate_version_is_refused_and_the_stored_row_is_untouched(manager):
    """A recipe version is immutable: a second write never overwrites it."""
    first = mrt.register(manifest=_manifest())
    assert first["ok"] is True

    second = mrt.register(
        manifest=_manifest(purpose="a different purpose, same identity")
    )

    assert second["ok"] is False
    assert second["reason_code"] == tr.REASON_DUPLICATE
    stored = store.get_recipe(manager, "example.echo", "1.0.0")
    assert stored is not None
    assert stored.purpose == BASE_MANIFEST["purpose"]
    assert stored.digest == first["digest"]
    assert len(store.list_recipes(manager)) == 1


def test_a_second_version_of_one_recipe_is_accepted(manager):
    """Immutability is per version, not per recipe id."""
    assert mrt.register(manifest=_manifest())["ok"] is True
    assert mrt.register(manifest=_manifest(version="1.1.0"))["ok"] is True

    listed = mrt.list_registered()
    assert listed["registry_count"] == 2
    assert {row["version"] for row in listed["recipes"]} == {"1.0.0", "1.1.0"}


# ---------------------------------------------------------------------------
# Read surfaces
# ---------------------------------------------------------------------------


def test_list_registered_reports_an_empty_store_as_zero(manager):
    listed = mrt.list_registered()

    assert listed["ok"] is True
    assert listed["registry_count"] == 0
    assert listed["recipes"] == []


def test_list_registered_omits_the_argv_body(manager):
    """Discovery-shaped: identity and contract, never the executable template."""
    mrt.register(manifest=_manifest())

    row = mrt.list_registered()["recipes"][0]

    assert row["recipe_id"] == "example.echo"
    assert row["parameters"] == [
        {"name": "target", "type": "path", "required": True}
    ]
    assert "argv" not in row


def test_show_returns_the_exact_canonical_manifest(manager):
    """The returned manifest is the payload the reported digest hashes."""
    registered = mrt.register(manifest=_manifest())

    shown = mrt.show(recipe_id="example.echo")

    assert shown["ok"] is True
    assert shown["digest"] == registered["digest"]
    rebuilt = tr.recipe_from_mapping(shown["manifest"])
    assert tr.recipe_digest(rebuilt) == registered["digest"]


def test_show_resolves_the_highest_version_by_version_key(manager):
    """Version resolution uses the registry's ordering, not string compare."""
    mrt.register(manifest=_manifest(version="1.9.0"))
    mrt.register(manifest=_manifest(version="1.10.0"))

    assert mrt.show(recipe_id="example.echo")["version"] == "1.10.0"


def test_show_reports_unknown_identity_with_a_stable_reason(manager):
    mrt.register(manifest=_manifest())

    assert mrt.show(recipe_id="nope")["reason_code"] == tr.REASON_UNKNOWN_RECIPE
    assert (
        mrt.show(recipe_id="example.echo", version="9.9.9")["reason_code"]
        == tr.REASON_UNKNOWN_VERSION
    )


# ---------------------------------------------------------------------------
# The canonical catalogue
# ---------------------------------------------------------------------------


def test_catalogue_is_non_empty_and_uniquely_identified():
    keys = [(r.id, r.version) for r in mrt.CANONICAL_RECIPES]

    assert len(keys) >= 10
    assert len(set(keys)) == len(keys)
    assert len({r.digest for r in mrt.CANONICAL_RECIPES}) == len(keys)


def test_every_catalogue_entry_names_its_call_site():
    """A recipe that cites no source file cannot be checked against reality."""
    for recipe in mrt.CANONICAL_RECIPES:
        assert "src/aiworkhub/" in recipe.purpose, recipe.id


def test_every_catalogue_argv_is_execve_shaped_and_shell_free():
    """The no-execution posture is structural: literal argv[0], no metachars."""
    forbidden = set("|&;<>$`\\\"'()[]{}*?!~")
    for recipe in mrt.CANONICAL_RECIPES:
        head = recipe.argv[0]
        assert isinstance(head, tr.ArgvLiteral), recipe.id
        assert head.text in {"git", "python", "node"}, recipe.id
        for token in recipe.argv:
            if isinstance(token, tr.ArgvLiteral):
                assert " " not in token.text, (recipe.id, token.text)
                assert not (forbidden & set(token.text)), (recipe.id, token.text)


def test_every_catalogue_entry_round_trips_through_the_store_form():
    """A catalogue entry read off disk is validated as strictly as one in code."""
    for recipe in mrt.CANONICAL_RECIPES:
        rebuilt = tr.recipe_from_mapping(
            json.loads(tr.canonical_json(tr.recipe_payload(recipe)))
        )
        assert tr.recipe_digest(rebuilt) == recipe.digest, recipe.id


def test_read_only_catalogue_entries_declare_no_write_capability():
    """The Git probes that only read must not claim they mutate anything."""
    read_only = {
        "aiworkhub.finalization.git_diff_name_only",
        "aiworkhub.finalization.git_diff_name_only_no_renames",
        "aiworkhub.finalization.git_ls_files_untracked",
        "aiworkhub.finalization.git_merge_base_is_ancestor",
        "aiworkhub.validation.git_diff_check",
        "aiworkhub.validation.ruff_check",
    }
    by_id = {r.id: r for r in mrt.CANONICAL_RECIPES}
    for recipe_id in read_only:
        assert by_id[recipe_id].capabilities == (), recipe_id
    for recipe in mrt.CANONICAL_RECIPES:
        if recipe.id not in read_only:
            assert tr.CAPABILITY_WRITE in recipe.capabilities, recipe.id


def test_catalogue_argv_matches_the_declared_validation_commands():
    """The validation recipes render the exact strings task_templates emits."""
    by_id = {r.id: r for r in mrt.CANONICAL_RECIPES}

    pytest_argv = tr.build_argv(
        by_id["aiworkhub.validation.pytest"], {"test_paths": ["tests/test_a.py"]}
    )
    assert pytest_argv == ("python", "-m", "pytest", "-q", "tests/test_a.py")

    ruff_argv = tr.build_argv(
        by_id["aiworkhub.validation.ruff_check"],
        {"check_paths": ["src/aiworkhub/x.py", "tests/test_a.py"]},
    )
    assert ruff_argv == (
        "python",
        "-m",
        "ruff",
        "check",
        "src/aiworkhub/x.py",
        "tests/test_a.py",
    )

    assert tr.build_argv(by_id["aiworkhub.validation.git_diff_check"], {}) == (
        "git",
        "diff",
        "--check",
    )
    assert tr.build_argv(by_id["aiworkhub.validation.package_gate_pytest"], {}) == (
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/test_module_size_ratchet.py",
        "tests/test_declared_invariants.py",
    )


def test_catalogue_argv_matches_the_worker_workspace_git_commands():
    """The workspace/finalization recipes render worker_workspace's own vectors."""
    by_id = {r.id: r for r in mrt.CANONICAL_RECIPES}

    assert tr.build_argv(
        by_id["aiworkhub.workspace.git_worktree_add_sparse"],
        {"worktree_path": "/tmp/wt"},
    ) == ("git", "worktree", "add", "--detach", "--no-checkout", "/tmp/wt", "HEAD")
    assert tr.build_argv(
        by_id["aiworkhub.workspace.git_worktree_add_pinned"],
        {"worktree_path": "/tmp/wt", "base_oid": "deadbeef"},
    ) == ("git", "worktree", "add", "--detach", "/tmp/wt", "deadbeef")
    assert tr.build_argv(
        by_id["aiworkhub.workspace.git_sparse_checkout_init"], {}
    ) == ("git", "sparse-checkout", "init", "--no-cone")
    assert tr.build_argv(
        by_id["aiworkhub.workspace.git_sparse_checkout_set"], {}
    ) == ("git", "sparse-checkout", "set", "--no-cone", "--stdin")
    assert tr.build_argv(by_id["aiworkhub.workspace.git_read_tree"], {}) == (
        "git",
        "read-tree",
        "-mu",
        "HEAD",
    )
    assert tr.build_argv(
        by_id["aiworkhub.workspace.git_sparse_checkout_disable"], {}
    ) == ("git", "sparse-checkout", "disable")
    assert tr.build_argv(
        by_id["aiworkhub.finalization.git_diff_name_only"], {}
    ) == ("git", "diff", "--name-only", "-z", "HEAD")
    assert tr.build_argv(
        by_id["aiworkhub.finalization.git_diff_name_only_no_renames"],
        {"diff_ref": "deadbeef"},
    ) == ("git", "diff", "--name-only", "--no-renames", "-z", "deadbeef")
    assert tr.build_argv(
        by_id["aiworkhub.finalization.git_ls_files_untracked"], {}
    ) == ("git", "ls-files", "--others", "--exclude-standard", "-z")
    assert tr.build_argv(
        by_id["aiworkhub.finalization.git_merge_base_is_ancestor"],
        {"base_oid": "deadbeef"},
    ) == ("git", "merge-base", "--is-ancestor", "deadbeef", "HEAD")


def test_seed_canonical_installs_the_catalogue_and_is_idempotent(manager):
    first = mrt.seed_canonical()

    assert first["ok"] is True
    assert first["failed"] == []
    assert len(first["registered"]) == len(mrt.CANONICAL_RECIPES)
    assert store.load_registry(manager).__len__() == len(mrt.CANONICAL_RECIPES)

    second = mrt.seed_canonical()

    assert second["ok"] is True
    assert second["registered"] == []
    assert len(second["already_registered"]) == len(mrt.CANONICAL_RECIPES)
    assert len(store.list_recipes(manager)) == len(mrt.CANONICAL_RECIPES)


def test_seed_canonical_is_manager_and_write_gated(manager, monkeypatch):
    monkeypatch.setattr(core, "writes_allowed", lambda: False)

    result = mrt.seed_canonical()

    assert result["ok"] is False
    assert result["error"] == "write_gate_closed"
    assert store.list_recipes(manager) == []


# ---------------------------------------------------------------------------
# The panel flips
# ---------------------------------------------------------------------------


def _projection(repo_root):
    """Run the dashboard's own provider + projection, editing neither."""
    provider = dashboard.DashboardProvider(repo_root=repo_root)
    state, payload = dashboard._provider_projection_input(
        provider, "get_tool_recipes_projection_input"
    )
    return dashboard._project_tool_recipes(
        payload, ownership="full", input_state=state
    )


def test_projection_reports_no_sample_on_an_unpopulated_store(manager):
    """The panel's current reading, reproduced: an empty store IS no_sample."""
    projected = _projection(manager)

    assert projected["state"] == "no_sample"


def test_projection_flips_to_measured_once_the_catalogue_is_registered(manager):
    """The acceptance measurement: registering real recipes flips the panel."""
    before = _projection(manager)
    assert before["state"] == "no_sample"

    seeded = mrt.seed_canonical()
    assert seeded["ok"] is True

    after = _projection(manager)

    assert after["state"] == "measured"
    assert after["availability"] == "available"
    assert after["returned_count"] > 0
    # The dashboard bounds every primary projection collection at
    # ``dashboard._PROJECTION_LIST_LIMIT`` (8). A registry larger than that is
    # reported truncated with an explicitly UNKNOWN total rather than a wrong
    # one -- "unknown", never 0 -- so the exact count assertions below run
    # against a registry inside that bound. This is the dashboard's own bound,
    # not a property of the store: ``store.load_registry`` returns every row.
    assert after["returned_count"] == dashboard._PROJECTION_LIST_LIMIT
    assert after["truncated"] is True
    assert after["count"] == "unknown"
    assert len(store.list_recipes(manager)) == len(mrt.CANONICAL_RECIPES)


def test_projection_reports_an_exact_registry_count_within_its_bound(manager):
    """Inside the dashboard's projection bound the count is exact, not unknown."""
    subset = mrt.CANONICAL_RECIPES[: dashboard._PROJECTION_LIST_LIMIT - 2]
    for recipe in subset:
        store.put_recipe(manager, recipe)

    projected = _projection(manager)

    assert projected["state"] == "measured"
    assert projected["availability"] == "available"
    assert projected["truncated"] is False
    assert projected["count"] == len(subset)
    assert projected["registry_count"] == len(subset)
    # Discovery with no granted capabilities returns exactly the recipes that
    # require none -- the read-only Git probes and the Ruff lint.
    assert projected["discovery_count"] == sum(
        1 for r in subset if not r.capabilities
    )


def test_projection_flips_for_a_single_hand_registered_recipe(manager):
    """One caller-registered manifest is enough; no catalogue is required."""
    assert mrt.register(manifest=_manifest())["ok"] is True

    projected = _projection(manager)

    assert projected["state"] == "measured"
    assert projected["registry_count"] == 1


def test_projection_degrades_rather_than_failing_on_a_tampered_row(manager):
    """A row whose digest no longer covers its payload must not be served."""
    mrt.register(manifest=_manifest())
    db = manager / ".aiworkhub" / "tasking" / "tool_recipes.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        payload = _manifest(purpose="silently rewritten")
        conn.execute(
            "UPDATE tool_recipes SET payload_json=? WHERE recipe_id=?",
            (json.dumps(payload), "example.echo"),
        )
        conn.commit()
    finally:
        conn.close()

    # The dashboard must render, and the manager read surface must say why.
    assert _projection(manager)["state"] == "no_sample"
    listed = mrt.list_registered()
    assert listed["ok"] is False
    assert "ToolRecipeStoreIntegrityError" in listed["error"]


# ---------------------------------------------------------------------------
# Reachability: the MCP surface actually calls this module
# ---------------------------------------------------------------------------


def test_the_mcp_server_exposes_the_recipe_tools():
    """Green tests do not prove the new code is ever called; this does."""
    from aiworkhub import server

    for name in (
        "aiworkhub_manager_recipe_register",
        "aiworkhub_manager_recipe_seed_canonical",
        "aiworkhub_manager_recipe_list",
        "aiworkhub_manager_recipe_show",
    ):
        assert callable(getattr(server, name)), name
    assert server.manager_recipe_tools is mrt


def test_server_startup_bootstraps_the_two_foundation_stores():
    """skill_registry_store/tool_recipes_store initializers now have a caller."""
    import inspect

    from aiworkhub import server

    source = inspect.getsource(server.main)
    assert "skill_registry_store" in source
    assert "tool_recipes_store" in source
    assert "initialize_repository" in source


def test_recipe_tools_delegate_to_the_driver(monkeypatch):
    """Each registered tool forwards to this module rather than reimplementing."""
    from aiworkhub import server

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        mrt, "register", lambda **kw: seen.setdefault("register", kw) or {"ok": True}
    )
    monkeypatch.setattr(
        mrt, "show", lambda **kw: seen.setdefault("show", kw) or {"ok": True}
    )

    server.aiworkhub_manager_recipe_register(manifest={"id": "x"})
    server.aiworkhub_manager_recipe_show(recipe_id="x", version="1.0.0")

    assert seen["register"] == {"manifest": {"id": "x"}}
    assert seen["show"] == {"recipe_id": "x", "version": "1.0.0"}
