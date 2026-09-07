"""Tests for the durable tool recipe manifest store.

The bug this store closes was not in ``tool_recipes``: that library was
complete and tested. It was that nothing could hold a manifest between two
calls, so ``dashboard.get_tool_recipes_projection_input`` built
``RecipeRegistry(())`` on every refresh and the panel then reported that the
registry was empty. These tests assert the store's own contract AND the
dashboard behaviour that depended on it, including the case that stays
``no_sample`` -- because that case must now be a measured fact about the store
rather than an artifact of the caller.
"""

from __future__ import annotations

import sqlite3

import pytest

from aiworkhub import dashboard, tool_recipes as tr, tool_recipes_store as store


def _recipe(
    recipe_id: str = "echo-tool",
    version: str = "1.0.0",
    *,
    task_kind: tr.TaskKind = tr.TaskKind.GENERATE,
    executable: str = "echo",
) -> tr.Recipe:
    return tr.Recipe(
        id=recipe_id,
        version=version,
        purpose="write a message to stdout",
        task_kind=task_kind,
        parameters=(
            tr.ParamSpec(name="message", type=tr.ParamType.STR, required=True),
            tr.ParamSpec(
                name="mode",
                type=tr.ParamType.ENUM,
                values=("quiet", "loud"),
                default="quiet",
            ),
        ),
        outputs=(tr.OutputSpec(name="text", type=tr.OutputType.STDOUT),),
        platforms=("linux",),
        capabilities=(),
        risk_class=tr.RiskClass.LOW,
        resource_bounds=tr.ResourceBounds(max_runtime_seconds=5),
        cache_policy=tr.CachePolicy.CACHEABLE,
        argv=(tr.lit(executable), tr.lit("--mode"), tr.slot("mode"), tr.slot("message")),
    )


def _db(repo_root):
    return repo_root.joinpath(*store.TOOL_RECIPES_DB_REL)


# ---------------------------------------------------------------------------
# Absent store: the honest empty case.
# ---------------------------------------------------------------------------


def test_absent_store_loads_an_empty_registry_and_creates_nothing(tmp_path):
    registry = store.load_registry(tmp_path)

    assert isinstance(registry, tr.RecipeRegistry)
    assert len(registry) == 0
    # A read must never bring the database into existence: an empty panel and a
    # freshly-created empty database are different facts about a repository.
    assert not _db(tmp_path).exists()


def test_reads_never_create_the_database(tmp_path):
    assert store.list_recipes(tmp_path) == []
    assert store.get_recipe(tmp_path, "echo-tool", "1.0.0") is None
    assert store.stored_digest(tmp_path, "echo-tool", "1.0.0") is None
    assert not _db(tmp_path).exists()


def test_initialize_repository_is_idempotent(tmp_path):
    first = store.initialize_repository(tmp_path)
    assert first["schema_id"] == store.SCHEMA_ID
    assert first["existing_count"] == 0
    assert _db(tmp_path).exists()

    store.put_recipe(tmp_path, _recipe())
    second = store.initialize_repository(tmp_path)
    assert second["existing_count"] == 1


# ---------------------------------------------------------------------------
# Round-trip fidelity.
# ---------------------------------------------------------------------------


def test_round_trip_preserves_the_manifest_digest(tmp_path):
    recipe = _recipe()
    receipt = store.put_recipe(tmp_path, recipe)
    assert receipt["digest"] == recipe.digest

    loaded = store.get_recipe(tmp_path, "echo-tool", "1.0.0")
    assert loaded is not None
    assert loaded.digest == recipe.digest


def test_round_trip_is_digest_stable_not_field_identical(tmp_path):
    """Unordered collections come back canonically sorted, and that is fine.

    ``recipe_digest`` hashes the same canonical order the payload stores, so a
    manifest whose ENUM values were written unsorted round-trips sorted with an
    unchanged digest. Asserting field identity instead would be asserting the
    caller's insertion order, which the manifest format deliberately does not
    preserve.
    """
    recipe = _recipe()
    assert recipe.parameters[1].values == ("quiet", "loud")

    store.put_recipe(tmp_path, recipe)
    loaded = store.get_recipe(tmp_path, "echo-tool", "1.0.0")

    assert loaded.parameters[1].values == ("loud", "quiet")
    assert loaded.digest == recipe.digest
    assert tr.recipe_payload(loaded) == tr.recipe_payload(recipe)


def test_round_trip_preserves_every_manifest_field(tmp_path):
    recipe = _recipe()
    store.put_recipe(tmp_path, recipe)
    loaded = store.load_registry(tmp_path).get("echo-tool")

    assert loaded.id == recipe.id
    assert loaded.version == recipe.version
    assert loaded.purpose == recipe.purpose
    assert loaded.task_kind is recipe.task_kind
    assert loaded.risk_class is recipe.risk_class
    assert loaded.cache_policy is recipe.cache_policy
    assert loaded.platforms == recipe.platforms
    assert loaded.capabilities == recipe.capabilities
    assert loaded.resource_bounds == recipe.resource_bounds
    assert loaded.outputs == recipe.outputs
    assert loaded.argv == recipe.argv
    assert [p.name for p in loaded.parameters] == [p.name for p in recipe.parameters]


def test_a_stored_manifest_still_renders_the_same_argv(tmp_path):
    """The stored manifest is a manifest, not a picture of one."""
    store.put_recipe(tmp_path, _recipe())
    registry = store.load_registry(tmp_path)

    assert registry.build_argv("echo-tool", {"message": "hello"}) == (
        "echo",
        "--mode",
        "quiet",
        "hello",
    )


def test_two_versions_of_one_recipe_persist_side_by_side(tmp_path):
    store.put_recipe(tmp_path, _recipe(version="1.0.0"))
    store.put_recipe(tmp_path, _recipe(version="2.0.0", executable="printf"))

    registry = store.load_registry(tmp_path)
    assert len(registry) == 2
    assert registry.versions("echo-tool") == ("1.0.0", "2.0.0")
    # Version resolution still defaults to the latest.
    assert registry.get("echo-tool").argv[0].text == "printf"


# ---------------------------------------------------------------------------
# Immutability and digest binding.
# ---------------------------------------------------------------------------


def test_an_exact_identity_can_never_be_rewritten(tmp_path):
    store.put_recipe(tmp_path, _recipe())
    with pytest.raises(store.ToolRecipeStoreConflictError):
        store.put_recipe(tmp_path, _recipe())


def test_a_digest_can_never_be_rebound_to_a_second_identity(tmp_path):
    """Two ids whose manifests differ only by id do not collide; a forced
    duplicate digest does, and is refused."""
    recipe = _recipe()
    store.put_recipe(tmp_path, recipe)

    conn = sqlite3.connect(str(_db(tmp_path)))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tool_recipes "
                "(recipe_id,version,digest,payload_json,created_at) "
                "VALUES (?,?,?,?,?)",
                ("other-tool", "1.0.0", recipe.digest, "{}", "now"),
            )
    finally:
        conn.close()


def test_put_recipe_rejects_a_non_recipe(tmp_path):
    with pytest.raises(store.ToolRecipeStoreError):
        store.put_recipe(tmp_path, {"id": "echo-tool", "version": "1.0.0"})


# ---------------------------------------------------------------------------
# Fail-closed reads.
# ---------------------------------------------------------------------------


def test_a_tampered_payload_fails_closed_on_read(tmp_path):
    """A row whose payload was edited without its digest is refused, not served."""
    store.put_recipe(tmp_path, _recipe())

    conn = sqlite3.connect(str(_db(tmp_path)))
    try:
        row = conn.execute("SELECT payload_json FROM tool_recipes").fetchone()[0]
        forged = row.replace('"purpose":"write a message to stdout"', '"purpose":"forged"')
        assert forged != row
        conn.execute("UPDATE tool_recipes SET payload_json=?", (forged,))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(store.ToolRecipeStoreIntegrityError):
        store.list_recipes(tmp_path)
    with pytest.raises(store.ToolRecipeStoreIntegrityError):
        store.load_registry(tmp_path)


def test_a_payload_that_would_build_an_unsafe_manifest_is_refused_on_read(tmp_path):
    """Manifest validation is not bypassed by having come off disk."""
    store.put_recipe(tmp_path, _recipe())

    conn = sqlite3.connect(str(_db(tmp_path)))
    try:
        conn.execute(
            "UPDATE tool_recipes SET payload_json=?",
            ('{"id":"x","version":"1","task_kind":"generate","risk_class":"none",'
             '"cache_policy":"never","argv":[["literal","rm -rf /"]]}',),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(tr.RecipeError) as excinfo:
        store.list_recipes(tmp_path)
    assert excinfo.value.reason == tr.REASON_UNSAFE_LITERAL


def test_a_corrupt_database_file_degrades_to_empty(tmp_path):
    path = _db(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a sqlite database" * 64)

    assert store.list_recipes(tmp_path) == []
    assert len(store.load_registry(tmp_path)) == 0
    assert store.get_recipe(tmp_path, "echo-tool", "1.0.0") is None


def test_a_database_without_the_table_degrades_to_empty(tmp_path):
    path = _db(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE unrelated (x TEXT)")
    conn.commit()
    conn.close()

    assert store.list_recipes(tmp_path) == []
    assert len(store.load_registry(tmp_path)) == 0


# ---------------------------------------------------------------------------
# Bounded reads.
# ---------------------------------------------------------------------------


def test_a_load_is_bounded(tmp_path):
    for index in range(5):
        store.put_recipe(tmp_path, _recipe(version=f"{index + 1}.0.0"))

    assert len(store.list_recipes(tmp_path, limit=2)) == 2
    assert len(store.load_registry(tmp_path, limit=3)) == 3
    # An over-large limit is clamped rather than trusted.
    assert len(store.list_recipes(tmp_path, limit=10**9)) == 5
    assert store.MAX_LOAD_LIMIT == 1000


def test_list_recipes_offsets_deterministically(tmp_path):
    store.put_recipe(tmp_path, _recipe(recipe_id="a-tool"))
    store.put_recipe(tmp_path, _recipe(recipe_id="b-tool", executable="printf"))

    assert [r.id for r in store.list_recipes(tmp_path)] == ["a-tool", "b-tool"]
    assert [r.id for r in store.list_recipes(tmp_path, offset=1)] == ["b-tool"]


# ---------------------------------------------------------------------------
# The dashboard behaviour this store exists for.
# ---------------------------------------------------------------------------


def _projection(repo_root, ownership: str = "full") -> dict:
    provider = dashboard.DashboardProvider(repo_root=repo_root)
    return dashboard._coding_foundation_projections(provider, ownership=ownership)[
        "tool_recipes"
    ]


def test_dashboard_reads_the_store(tmp_path):
    """The projection input is the store's registry, not a fresh empty one."""
    store.put_recipe(tmp_path, _recipe())
    provider = dashboard.DashboardProvider(repo_root=tmp_path)

    registry = provider.get_tool_recipes_projection_input()
    assert isinstance(registry, tr.RecipeRegistry)
    assert registry.ids() == ("echo-tool",)


def test_an_absent_store_still_reports_no_sample_and_never_raises(tmp_path):
    """The honest empty case.

    ``no_sample`` here is now a measured fact -- the store was consulted and
    holds nothing -- rather than the previous structural artifact where the
    dashboard handed the projection ``RecipeRegistry(())`` on every call.
    """
    projected = _projection(tmp_path)

    assert projected["state"] == "no_sample"
    assert projected["invocation"] == {"state": "no_sample"}
    assert projected["cache"] == {"state": "no_sample"}
    assert projected["context"] == {"state": "no_sample"}


def test_a_populated_store_projects_measured(tmp_path):
    store.put_recipe(tmp_path, _recipe())
    store.put_recipe(tmp_path, _recipe(recipe_id="fmt-tool", task_kind=tr.TaskKind.FORMAT))

    projected = _projection(tmp_path)

    assert projected["state"] == "measured"
    assert projected["availability"] == "available"
    assert projected["registry_count"] == 2
    assert projected["discovery_count"] == 2
    assert projected["count"] == 2


def test_a_corrupt_store_never_breaks_a_refresh(tmp_path):
    """A refusing store degrades the panel; it does not fail the refresh."""
    store.put_recipe(tmp_path, _recipe())
    conn = sqlite3.connect(str(_db(tmp_path)))
    try:
        conn.execute("UPDATE tool_recipes SET payload_json='{\"id\":\"nope\"}'")
        conn.commit()
    finally:
        conn.close()

    projected = _projection(tmp_path)
    assert projected["state"] == "no_sample"

    path = _db(tmp_path)
    path.write_bytes(b"not a database" * 64)
    assert _projection(tmp_path)["state"] == "no_sample"


def test_the_projection_keys_are_unchanged_by_the_new_source(tmp_path):
    """Same payload shape as an in-memory registry passed straight through.

    The VS Code extension renders these keys; this task changed only where the
    registry comes from, so a store-backed projection and a hand-built one must
    be key-for-key identical at every state.
    """
    recipe = _recipe()

    empty_direct = dashboard._project_tool_recipes(
        tr.RecipeRegistry(()), ownership="full", input_state="present"
    )
    empty_store = _projection(tmp_path)
    assert set(empty_store) == set(empty_direct)
    assert empty_store == empty_direct

    store.put_recipe(tmp_path, recipe)
    full_direct = dashboard._project_tool_recipes(
        tr.RecipeRegistry((recipe,)), ownership="full", input_state="present"
    )
    full_store = _projection(tmp_path)
    assert set(full_store) == set(full_direct)
    assert full_store == full_direct


def test_the_summary_projection_shape_is_unchanged(tmp_path):
    store.put_recipe(tmp_path, _recipe())

    summary_store = _projection(tmp_path, ownership="summary")
    summary_direct = dashboard._project_tool_recipes(
        tr.RecipeRegistry((_recipe(),)), ownership="summary", input_state="present"
    )
    assert summary_store == summary_direct


# ---------------------------------------------------------------------------
# Posture: the store persists manifests, it never runs anything.
# ---------------------------------------------------------------------------


def test_the_store_imports_no_execution_surface():
    """Measure the imports, not the prose.

    A substring scan over the source would match this module's own docstring
    explaining that it spawns no subprocess -- a detector counting its own
    vocabulary. Parse the actual import statements instead.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(store))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                imported.add(node.module.split(".")[0])

    forbidden = {"subprocess", "os", "socket", "shutil", "urllib", "http", "asyncio"}
    assert not (imported & forbidden), f"execution surface imported: {imported & forbidden}"
    assert "sqlite3" in imported
