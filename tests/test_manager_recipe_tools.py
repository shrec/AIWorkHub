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

import hashlib
import importlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

import aiworkhub.core as core
from aiworkhub import dashboard
from aiworkhub import manager_recipe_tools as mrt
from aiworkhub import recipe_runner
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
        # The packaged operator modules under aiworkhub.recipes open the task
        # queue with ``mode=ro&immutable=1`` and read runtime artifacts; only
        # ``repo_test_subset`` executes repository code, and it is the one
        # entry of that family that declares ``write``.
        "aiworkhub.operator.attempt_validation",
        "aiworkhub.operator.process_liveness",
        "aiworkhub.operator.request_log_tail",
        "aiworkhub.operator.task_events",
        "aiworkhub.operator.usage_rollup",
        "aiworkhub.operator.worktree_diff",
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


def test_seed_canonical_installs_what_the_project_has_and_is_idempotent(manager):
    """Seeding installs the plan for THIS project, and converges on a re-run.

    The count is the plan's, not the catalogue's: an empty ``tmp_path``
    repository does not have this repository's two gate test files, so
    ``validation.package_gate_pytest`` is withheld with a measured reason
    rather than registered as a manifest that could only fail.
    """
    plan = mrt.seeding_plan(manager)
    expected = len(plan["selected"])

    first = mrt.seed_canonical()

    assert first["ok"] is True
    assert first["failed"] == []
    assert first["selected_count"] == expected
    assert len(first["registered"]) == expected
    assert len(store.load_registry(manager)) == expected
    assert expected < len(mrt.CANONICAL_RECIPES)
    # The withheld SET is measured, not fixed, and pinning it here pins the
    # runner's own toolchain. Measured 2026-09-08: this assertion read
    # {package_gate_pytest} locally, where ruff is installed, and
    # {package_gate_pytest, pytest, ruff_check} on the CI Python job, which
    # installs the package plus pytest and pytest-xdist and no ruff -- so the
    # release went out green locally and red on all three Python versions.
    # Withholding ruff_check from a project without ruff is the behaviour this
    # feature exists for; asserting it away was the defect.
    #
    # What IS invariant in any environment: an empty tmp_path repository does
    # not have this repository's two gate test files, so the package gate is
    # withheld for a PATH reason and never for a toolchain one; every withheld
    # entry names why; and nothing outside the conditional set is ever
    # withheld, which is what would catch a universal recipe being dropped.
    withheld = {entry["recipe_id"]: list(entry["reasons"]) for entry in first["withheld"]}
    gate = "aiworkhub.validation.package_gate_pytest"
    assert gate in withheld
    assert [reason for reason in withheld[gate] if reason.startswith("path_absent:")]
    assert all(reasons for reasons in withheld.values())
    assert set(withheld) <= {
        requirement.recipe_id for requirement in mrt.CONDITIONAL_REQUIREMENTS
    }
    assert set(withheld) == {
        entry["recipe_id"] for entry in plan["withheld"]
    }

    second = mrt.seed_canonical()

    assert second["ok"] is True
    assert second["registered"] == []
    assert len(second["already_registered"]) == expected
    assert len(store.list_recipes(manager)) == expected


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
    # ``dashboard._PROJECTION_LIST_LIMIT`` (8), so a registry larger than that
    # returns 8 items and says so. The bound is on the ITEMS, not on the count:
    # the registry knows its own length and the panel reports it exactly.
    # NF-2026-00668 -- it used to answer "unknown" here, telling the operator
    # there are recipes and simultaneously that their number was unknowable,
    # about a store that could answer for free. This is the dashboard's own
    # bound, not a property of the store: ``store.load_registry`` returns every
    # row, and the two counts below must agree.
    assert after["returned_count"] == dashboard._PROJECTION_LIST_LIMIT
    assert after["truncated"] is True
    seeded = len(seeded["registered"])
    assert after["count"] == seeded
    assert after["registry_count"] == seeded
    assert len(store.list_recipes(manager)) == seeded


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
# The run surface
#
# Registration alone could never produce a receipt, so the dashboard's
# invocation/cache/context sections read ``no_sample`` structurally. These
# exercise the only code path that runs a persisted manifest: the manager
# gate, the capability gate reused from ``tool_recipes`` (not re-implemented
# here), the repository write gate for a write/network recipe, the bounded
# digest, and the receipt the store finally has something to hold.
# ---------------------------------------------------------------------------

# A stdlib-only probe, written into the isolated repository so a run has
# something real to execute. It is spawned as an execve vector -- ``python``
# plus a literal relative path plus one enum slot -- because a manifest literal
# may not contain a space or a shell metacharacter, so ``python -c <source>``
# is not expressible here by construction.
_PROBE_SOURCE = '''\
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
if mode == "big":
    sys.stdout.write("x" * 12000)
elif mode == "fail":
    sys.stderr.write("boom\\n")
    raise SystemExit(3)
elif mode == "slow":
    time.sleep(30)
else:
    sys.stdout.write("ok\\n")
'''


def _probe_recipe(
    *,
    recipe_id: str = "example.probe",
    version: str = "1.0.0",
    capabilities: tuple[str, ...] = (),
    max_runtime_seconds: float | None = None,
) -> tr.Recipe:
    return tr.Recipe(
        id=recipe_id,
        version=version,
        purpose="A runnable probe used only by these tests.",
        task_kind=tr.TaskKind.SCAN,
        parameters=(
            tr.ParamSpec(
                name="mode",
                type=tr.ParamType.ENUM,
                required=True,
                values=("ok", "big", "fail", "slow"),
            ),
        ),
        outputs=(tr.OutputSpec(name="report", type=tr.OutputType.STDOUT),),
        capabilities=capabilities,
        resource_bounds=tr.ResourceBounds(max_runtime_seconds=max_runtime_seconds),
        argv=(tr.lit("python"), tr.lit("probe.py"), tr.slot("mode")),
    )


@pytest.fixture
def probe(manager):
    """Install the probe script and its manifest in the manager's repository."""
    (manager / "probe.py").write_text(_PROBE_SOURCE, encoding="utf-8")

    def install(**kwargs) -> tr.Recipe:
        recipe = _probe_recipe(**kwargs)
        store.put_recipe(manager, recipe)
        return recipe

    return install


def test_run_is_manager_gated(tmp_path, monkeypatch):
    """Execution is refused for an unverified route before anything is read."""
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {"role": "worker"})
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)

    result = mrt.run(recipe_id="example.probe")

    assert result["ok"] is False
    assert result["error"] == "verified_manager_identity_required"


def test_run_refuses_an_unregistered_recipe(manager):
    """What runs is a STORED manifest; an unknown id is refused, not invented."""
    result = mrt.run(recipe_id="example.probe")

    assert result["ok"] is False
    assert result["reason_code"] == tr.REASON_UNKNOWN_RECIPE


def test_run_refuses_an_invalid_invocation_without_executing(probe, manager):
    """Parameter validation is tool_recipes', and it happens before the spawn."""
    probe()

    missing = mrt.run(recipe_id="example.probe", params={})
    extra = mrt.run(
        recipe_id="example.probe", params={"mode": "ok", "sneak": "x"}
    )
    bad_enum = mrt.run(recipe_id="example.probe", params={"mode": "teleport"})

    assert missing["reason_code"] == tr.REASON_MISSING_PARAMETER
    assert extra["reason_code"] == tr.REASON_EXTRA_PARAMETER
    assert bad_enum["reason_code"] == tr.REASON_INVALID_ENUM
    for refusal in (missing, extra, bad_enum):
        assert refusal["ok"] is False
    # Nothing ran, so nothing was receipted.
    assert store.receipt_count(manager) == 0
    assert not (manager / ".aiworkhub" / "runtime" / "recipe_runs").exists()


def test_run_refuses_an_ungranted_capability(probe, manager):
    """The capability predicate is tool_recipes' own, not a second one here."""
    probe(recipe_id="example.writer", capabilities=(tr.CAPABILITY_WRITE,))

    refused = mrt.run(recipe_id="example.writer", params={"mode": "ok"})

    assert refused["ok"] is False
    assert refused["reason_code"] == tr.REASON_UNSUPPORTED_CAPABILITY
    assert refused["required_capabilities"] == [tr.CAPABILITY_WRITE]
    assert refused["granted_capabilities"] == []
    assert store.receipt_count(manager) == 0


def test_run_refuses_a_capability_string_rather_than_a_list(probe, manager):
    """A bare string is an iterable of characters; that must not read as a grant."""
    probe(recipe_id="example.writer", capabilities=(tr.CAPABILITY_WRITE,))

    refused = mrt.run(
        recipe_id="example.writer", params={"mode": "ok"}, grant_capabilities="write"
    )

    assert refused["ok"] is False
    assert refused["reason_code"] == tr.REASON_INVALID_TYPE


def test_the_two_recipe_errors_stay_independently_catchable(
    probe, manager, monkeypatch
):
    """The validation error and the run error must never catch one another.

    ``run`` catches :class:`tool_recipes.RecipeError` FIRST and
    :class:`recipe_runner.RecipeRunError` second, returning a different reply
    shape for each. Both inherit their ``(reason, message)`` construction from
    a single definition, ``tool_recipes.RecipeErrorBase``, as SIBLINGS. Were
    the run error ever made a subclass of the validation error to share that
    construction, every run failure would match the earlier branch and come
    back wearing the validation reply -- and nothing about the class hierarchy
    would look wrong on its own. So the relationship and the routing are pinned
    together here, by the same test.
    """
    assert issubclass(tr.RecipeError, tr.RecipeErrorBase)
    assert issubclass(recipe_runner.RecipeRunError, tr.RecipeErrorBase)
    assert not issubclass(recipe_runner.RecipeRunError, tr.RecipeError)
    assert not issubclass(tr.RecipeError, recipe_runner.RecipeRunError)

    probe()

    def _unavailable(*args, **kwargs):
        raise recipe_runner.RecipeRunError(
            recipe_runner.REASON_EXECUTABLE_UNAVAILABLE, "no interpreter"
        )

    monkeypatch.setattr(recipe_runner, "run_recipe", _unavailable)

    refused = mrt.run(recipe_id="example.probe", params={"mode": "ok"})

    assert refused["ok"] is False
    assert refused["reason_code"] == recipe_runner.REASON_EXECUTABLE_UNAVAILABLE
    assert refused["error"] == "no interpreter"
    assert refused["recipe_id"] == "example.probe"
    # The run branch reports the recipe identity and stops there. Capability
    # evaluation belongs to the validation branch, which is the reply
    # ``test_run_refuses_an_ungranted_capability`` pins; seeing those keys here
    # would mean the run error had been routed into the wrong except clause.
    assert "granted_capabilities" not in refused
    assert "required_capabilities" not in refused


def test_a_write_capable_recipe_needs_the_repository_write_gate(
    probe, manager, monkeypatch
):
    """A granted capability is not enough: the repository gate applies on top."""
    probe(recipe_id="example.writer", capabilities=(tr.CAPABILITY_WRITE,))
    monkeypatch.setattr(core, "writes_allowed", lambda: False)

    refused = mrt.run(
        recipe_id="example.writer",
        params={"mode": "ok"},
        grant_capabilities=[tr.CAPABILITY_WRITE],
    )

    assert refused["ok"] is False
    assert refused["error"] == "write_gate_closed"
    assert refused["required_capabilities"] == [tr.CAPABILITY_WRITE]
    assert store.receipt_count(manager) == 0


def test_a_read_only_recipe_does_not_consult_the_write_gate(
    probe, manager, monkeypatch
):
    """The default posture is read-only, and a closed gate must not block it."""
    probe()
    monkeypatch.setattr(core, "writes_allowed", lambda: False)

    result = mrt.run(recipe_id="example.probe", params={"mode": "ok"})

    assert result["ok"] is True
    assert result["returncode"] == 0


def test_run_returns_a_bounded_digest_with_the_full_output_on_disk(probe, manager):
    """A digest carries tails and a verifiable file, never the whole stream."""
    probe()

    result = mrt.run(recipe_id="example.probe", params={"mode": "big"})

    assert result["ok"] is True
    assert result["exit_status"] == tr.EXIT_STATUS_COMPLETED
    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert result["duration_seconds"] >= 0.0
    assert result["argv"] == ["python", "probe.py", "big"]

    # The bound is the point: 12,000 characters of stdout come back as the
    # last 4,096, flagged, with the true length reported alongside.
    assert result["stdout_chars"] == 12000
    assert result["stdout_truncated"] is True
    assert len(result["stdout_tail"]) == recipe_runner.MAX_TAIL_CHARS
    assert result["stderr_truncated"] is False
    assert result["stderr_tail"] == ""

    # ...and the full stream is retrievable and verifiable from the digest.
    output_path = Path(result["output_path"])
    assert output_path.is_file()
    raw = output_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == result["output_sha256"]
    assert len(raw) == result["output_bytes"]
    written = json.loads(raw.decode("utf-8"))
    assert written["stdout"] == "x" * 12000
    assert written["run_id"] == result["run_id"]
    assert written["executed_argv"][1:] == ["probe.py", "big"]


def test_a_non_zero_exit_is_a_measured_result_not_an_error(probe, manager):
    """"The tool ran and said no" is exactly the evidence the caller asked for."""
    probe()

    result = mrt.run(recipe_id="example.probe", params={"mode": "fail"})

    assert result["ok"] is True
    assert result["returncode"] == 3
    assert result["exit_status"] == tr.EXIT_STATUS_COMPLETED
    assert result["stderr_tail"] == "boom\n"


def test_a_timeout_is_reported_as_a_measured_exit_status(probe, manager):
    """The manifest's declared runtime bound is what the runner enforces."""
    probe(max_runtime_seconds=1)

    result = mrt.run(recipe_id="example.probe", params={"mode": "slow"})

    assert result["ok"] is True
    assert result["timed_out"] is True
    assert result["exit_status"] == tr.EXIT_STATUS_TIMEOUT
    assert result["returncode"] is None
    assert result["timeout_seconds"] == 1.0
    assert result["timeout_source"] == "recipe_resource_bounds"


def test_a_run_persists_a_receipt_the_store_reads_back(probe, manager):
    """The store finally has receipts, and each one covers its own payload."""
    recipe = probe()

    result = mrt.run(recipe_id="example.probe", params={"mode": "ok"})

    assert result["receipt_persisted"] is True
    assert store.receipt_count(manager) == 1
    receipts = store.list_receipts(manager)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["recipe_id"] == "example.probe"
    assert receipt["recipe_version"] == "1.0.0"
    assert receipt["recipe_digest"] == recipe.digest
    assert receipt["argv"] == ["python", "probe.py", "ok"]
    assert receipt["repository"] == str(manager)
    assert receipt["exit"]["status"] == tr.EXIT_STATUS_COMPLETED
    assert receipt["exit"]["exit_code"] == 0
    assert receipt["digest"] == result["receipt_digest"]
    # The stored digest covers the stored bytes, so the receipt is verifiable
    # without trusting either this surface or the store.
    verifiable = {k: v for k, v in receipt.items() if k not in ("digest", "created_at")}
    assert tr.receipt_digest(verifiable) == receipt["digest"]


def test_two_runs_of_one_recipe_are_two_receipts(probe, manager):
    """A receipt records a RUN; identical argv twice is still two runs."""
    probe()

    mrt.run(recipe_id="example.probe", params={"mode": "ok"})
    mrt.run(recipe_id="example.probe", params={"mode": "ok"})

    assert store.receipt_count(manager) == 2


# ---------------------------------------------------------------------------
# The catalogue's scripts exist
# ---------------------------------------------------------------------------


def test_every_catalogue_entry_constructs_and_its_module_imports():
    """A recipe naming a module nobody wrote makes the panel lie.

    Stronger than the file check this replaces: a module that exists but does
    not import is exactly as unrunnable as one that does not exist, and the
    ``from . import _common`` these modules use only resolves inside a package.
    """
    scripted = 0
    for recipe in mrt.CANONICAL_RECIPES:
        assert isinstance(recipe, tr.Recipe), recipe
        assert recipe.digest, recipe.id
        for token in recipe.argv:
            if not isinstance(token, tr.ArgvLiteral):
                continue
            if not token.text.startswith(mrt.OPERATOR_MODULE_PREFIX):
                continue
            scripted += 1
            assert importlib.util.find_spec(token.text) is not None, (
                recipe.id,
                token.text,
            )
            assert importlib.import_module(token.text) is not None, recipe.id
    # The packaged operator modules are the reason this catalogue exists at
    # all; a catalogue that referenced none of them would pass vacuously.
    assert scripted >= 7


def test_no_catalogue_entry_names_a_path_outside_the_installed_package():
    """The portability contract, asserted rather than described.

    ``python scripts/recipes/<name>.py`` named a path that exists only in
    AIWorkHub's own checkout, so every operator recipe was unrunnable in the
    repositories AIWorkHub manages -- while the ``.aiworkhub/`` data those
    modules read was per-project and correct. A literal that looks like a
    repository-relative script path must never come back.
    """
    for recipe in mrt.CANONICAL_RECIPES:
        for token in recipe.argv:
            if not isinstance(token, tr.ArgvLiteral):
                continue
            assert not token.text.startswith("scripts/"), (recipe.id, token.text)
            assert not token.text.endswith(".py") or recipe.id.startswith(
                "aiworkhub.validation."
            ), (recipe.id, token.text)


def test_the_scripted_catalogue_entries_run_the_repository_interpreter():
    """Their argv[0] is the ``python`` literal the runner resolves to .venv."""
    for recipe in mrt.CANONICAL_RECIPES:
        if not any(
            isinstance(token, tr.ArgvLiteral)
            and token.text.startswith(mrt.OPERATOR_MODULE_PREFIX)
            for token in recipe.argv
        ):
            continue
        assert recipe.argv[0] == tr.lit("python"), recipe.id
        assert recipe.argv[1] == tr.lit("-m"), recipe.id


# ---------------------------------------------------------------------------
# The panel's evidence sections
# ---------------------------------------------------------------------------


def test_projection_invocation_sections_flip_once_a_run_exists(probe, manager):
    """The measurement the owner asked for: no_sample becomes measured."""
    probe()

    before = _projection(manager)
    assert before["state"] == "measured"  # the registry is populated
    assert before["invocation"] == {"state": "no_sample"}
    assert before["cache"] == {"state": "no_sample"}
    assert before["context"] == {"state": "no_sample"}

    assert mrt.run(recipe_id="example.probe", params={"mode": "ok"})["ok"] is True

    after = _projection(manager)

    assert after["invocation"]["state"] == "measured"
    assert after["invocation"]["count"] == 1
    assert after["invocation"]["returned_count"] == 1
    assert after["cache"]["state"] == "measured"
    assert after["cache"]["eligible_count"] + after["cache"]["ineligible_count"] == 1
    assert after["context"] == {"state": "measured", "receipt_count": 1}
    assert after["receipts"][0]["recipe_id"] == "example.probe"


def test_cache_eligibility_is_answerable_beyond_the_display_bound(probe, manager):
    """A DISPLAY bound must not decide whether a measurement is possible.

    The registry the projection renders is capped at
    ``dashboard._PROJECTION_LIST_LIMIT`` items. Cache eligibility is a property
    of the RECIPE, so resolving it only inside that window made a receipt for
    the 12th registered recipe report ``unknown`` for a reason that has nothing
    to do with the evidence. The provider carries the exact manifests its
    receipts name, so the cap stays a rendering bound.
    """
    probe(recipe_id="zz.probe")  # sorts last, far outside the rendered window
    assert mrt.seed_canonical()["ok"] is True
    bounded = store.list_recipes(manager, limit=dashboard._PROJECTION_LIST_LIMIT)
    assert "zz.probe" not in {recipe.id for recipe in bounded}

    assert mrt.run(recipe_id="zz.probe", params={"mode": "ok"})["ok"] is True

    provider = dashboard.DashboardProvider(repo_root=manager)
    payload = provider.get_tool_recipes_projection_input()
    assert [recipe.id for recipe in payload["receipt_recipes"]] == ["zz.probe"]

    projected = _projection(manager)
    assert projected["cache"]["state"] == "measured"
    assert projected["cache"]["ineligible_count"] == 1
    assert projected["cache"]["eligible_count"] == 0


def test_projection_reports_the_exact_receipt_total_beyond_its_item_bound(
    probe, manager
):
    """The bound is on the ITEMS; the store counts its receipts exactly."""
    probe()
    runs = dashboard._PROJECTION_LIST_LIMIT + 3
    for _ in range(runs):
        assert mrt.run(recipe_id="example.probe", params={"mode": "ok"})["ok"] is True

    projected = _projection(manager)

    assert store.receipt_count(manager) == runs
    assert projected["invocation"]["returned_count"] == dashboard._PROJECTION_LIST_LIMIT
    assert projected["invocation"]["count"] == runs
    assert projected["invocation"]["truncated"] is True
    assert projected["context"] == {"state": "measured", "receipt_count": runs}


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
        "aiworkhub_manager_recipe_run",
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
    monkeypatch.setattr(
        mrt, "run", lambda **kw: seen.setdefault("run", kw) or {"ok": True}
    )

    server.aiworkhub_manager_recipe_register(manifest={"id": "x"})
    server.aiworkhub_manager_recipe_show(recipe_id="x", version="1.0.0")
    server.aiworkhub_manager_recipe_run(recipe_id="x")

    assert seen["register"] == {"manifest": {"id": "x"}}
    assert seen["show"] == {"recipe_id": "x", "version": "1.0.0"}
    # The MCP boundary defaults are normalized here, not inside the driver:
    # an omitted params/grant_capabilities arrives as an empty collection.
    assert seen["run"] == {
        "recipe_id": "x",
        "version": None,
        "params": {},
        "grant_capabilities": [],
    }


# ---------------------------------------------------------------------------
# Actor provenance: WHO ran it, derived from the verified route
# ---------------------------------------------------------------------------


def test_a_run_records_the_verified_manager_route_as_its_actor(probe, manager):
    """The gap the owner named: a receipt now says who ran the invocation.

    Before this, ``tool_recipe_receipts`` carried no actor field of any kind --
    no session, provider, role, runner, task or request -- so "Measured" could
    only ever mean "rows exist".
    """
    probe()

    assert mrt.run(recipe_id="example.probe", params={"mode": "ok"})["ok"] is True

    receipt = store.list_receipts(manager)[0]
    actor = receipt["actor"]

    # Exactly the route the ``manager`` fixture installed, and nothing typed.
    assert actor["kind"] == tr.ACTOR_KIND_MANAGER
    assert actor["provider"] == "claude"
    assert actor["session_id"] == "sess-recipes"
    assert actor["key"] == "manager:claude:sess-recipes"
    assert actor["request_id"] is None
    assert actor["task_id"] is None
    assert actor["runner"] is None


def test_the_actor_cannot_be_supplied_through_the_mcp_surface():
    """Reachability, not prose: no recipe tool has a parameter for the actor."""
    import inspect

    from aiworkhub import server

    for name in (
        "aiworkhub_manager_recipe_run",
        "aiworkhub_manager_recipe_register",
        "aiworkhub_manager_recipe_seed_canonical",
        "aiworkhub_manager_recipe_usage",
    ):
        parameters = set(inspect.signature(getattr(server, name)).parameters)
        assert not parameters & {
            "actor",
            "actor_id",
            "provider",
            "session_id",
            "runner",
            "task_id",
            "request_id",
        }, name


def test_build_receipt_refuses_an_actor_that_is_not_a_verified_identity(probe, manager):
    """A mapping or a string is refused, not coerced into an identity."""
    recipe = probe()
    validated = tr.validate_invocation(recipe, {"mode": "ok"})

    for supplied in ({"kind": "manager", "provider": "x"}, "manager:claude:me", 7):
        with pytest.raises(tr.RecipeError) as excinfo:
            tr.build_receipt(validated, actor=supplied)
        assert excinfo.value.reason == tr.REASON_INVALID_TYPE


@pytest.mark.parametrize(
    "fields, reason",
    [
        ({"kind": tr.ACTOR_KIND_MANAGER, "provider": "claude"}, "no session"),
        ({"kind": tr.ACTOR_KIND_WORKER, "runner": "opus"}, "no request or task"),
        ({"kind": tr.ACTOR_KIND_UNATTRIBUTED, "provider": "claude"}, "named nobody"),
    ],
)
def test_an_actor_missing_its_route_evidence_is_refused(fields, reason):
    """Half a route is not an identity; it is refused rather than defaulted."""
    with pytest.raises(tr.RecipeError) as excinfo:
        tr.ActorIdentity(**fields)

    assert excinfo.value.reason == tr.REASON_UNVERIFIED_ACTOR, reason


def test_an_actor_key_cannot_be_forged_or_made_ambiguous():
    """The distinct-actor count groups on ``key``, so ``key`` is derived.

    This repository already learned that counting DISTINCT free-text actor
    strings lets one caller who can type two strings look like two identities.
    """
    with pytest.raises(tr.RecipeError) as forged:
        tr.ActorIdentity(
            kind=tr.ACTOR_KIND_MANAGER,
            provider="claude",
            session_id="s",
            key="manager:someone:else",
        )
    assert forged.value.reason == tr.REASON_UNVERIFIED_ACTOR

    # ``a:b`` + ``c`` and ``a`` + ``b:c`` would derive one key from two routes.
    with pytest.raises(tr.RecipeError) as ambiguous:
        tr.ActorIdentity(
            kind=tr.ACTOR_KIND_MANAGER, provider="a:b", session_id="c"
        )
    assert ambiguous.value.reason == tr.REASON_UNSAFE_VALUE


def test_a_run_with_no_verified_route_is_unattributed_not_invented(probe, manager):
    """A receipt built outside the manager surface names nobody."""
    recipe = probe()

    digest = recipe_runner.run_recipe(manager, recipe, {"mode": "ok"})

    assert digest["actor_kind"] == tr.ACTOR_KIND_UNATTRIBUTED
    receipt = store.list_receipts(manager)[0]
    assert receipt["actor"]["key"] == ""
    assert receipt["actor"]["provider"] is None


# ---------------------------------------------------------------------------
# Usage: which registered recipes are actually used
# ---------------------------------------------------------------------------


def test_usage_reports_every_registered_recipe_including_the_unused(probe, manager):
    """The distinction the panel exists to make, measured end to end."""
    probe(recipe_id="example.used")
    probe(recipe_id="example.never")

    assert mrt.run(recipe_id="example.used", params={"mode": "ok"})["ok"] is True
    assert mrt.run(recipe_id="example.used", params={"mode": "fail"})["ok"] is True

    report = mrt.usage()

    assert report["ok"] is True
    assert report["registered_count"] == 2
    assert report["used_count"] == 1
    assert report["unused_count"] == 1
    assert report["run_count"] == 2
    assert report["distinct_actor_count"] == 1
    assert report["attributed_run_count"] == 2
    assert report["unattributed_run_count"] == 0

    rows = {row["recipe_id"]: row for row in report["recipes"]}
    used = rows["example.used"]
    assert used["used"] is True
    assert used["runs"] == 2
    assert used["distinct_actors"] == 1
    assert used["first_run"] and used["last_run"]
    assert used["returned_digest_bytes"] > 0
    # A non-zero exit is a RESULT: the two outcomes stay distinguishable.
    assert used["exit_distribution"] == {"completed:0": 1, "completed:3": 1}

    never = rows["example.never"]
    assert never["used"] is False
    assert never["runs"] == 0
    assert never["distinct_actors"] == 0
    assert never["first_run"] == ""
    assert never["last_run"] == ""
    assert never["exit_distribution"] == {}


def test_usage_counts_one_session_running_two_recipes_as_one_actor(probe, manager):
    """The repository-wide actor total is not the sum of the per-recipe ones."""
    probe(recipe_id="example.a")
    probe(recipe_id="example.b")

    assert mrt.run(recipe_id="example.a", params={"mode": "ok"})["ok"] is True
    assert mrt.run(recipe_id="example.b", params={"mode": "ok"})["ok"] is True

    report = mrt.usage()

    assert report["distinct_actor_count"] == 1
    assert sum(row["distinct_actors"] for row in report["recipes"]) == 2


def test_usage_on_a_populated_registry_with_no_runs_is_measured_not_absent(manager):
    """"Registered and never run" is a measurement, not a missing sample."""
    assert mrt.seed_canonical()["ok"] is True

    report = mrt.usage()

    assert report["ok"] is True
    assert report["registered_count"] > 0
    assert report["used_count"] == 0
    assert report["unused_count"] == report["registered_count"]
    assert report["run_count"] == 0
    assert all(row["used"] is False for row in report["recipes"])


def test_usage_is_manager_gated(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {"role": "worker"})
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)

    assert mrt.usage()["error"] == "verified_manager_identity_required"


def test_usage_since_bounds_the_evidence_and_says_so(probe, manager):
    """A window narrows the runs, never the registry it is a fraction of."""
    probe()
    assert mrt.run(recipe_id="example.probe", params={"mode": "ok"})["ok"] is True

    unbounded = mrt.usage()
    future = mrt.usage(since="2999-01-01T00:00:00+00:00")

    assert unbounded["run_count"] == 1
    assert unbounded["window_only"] is False
    assert future["run_count"] == 0
    assert future["window_only"] is True
    # The denominator is still every registered recipe, and the asymmetry is
    # declared rather than hidden behind a smaller registry count.
    assert future["registered_count"] == unbounded["registered_count"]
    assert future["used_count"] == 0


def test_the_panel_distinguishes_registered_from_used(probe, manager):
    """The owner's question, answered by the projection the dashboard renders."""
    probe(recipe_id="example.used")
    probe(recipe_id="example.never")

    before = _projection(manager)
    assert before["usage"]["state"] == "measured"
    assert before["usage"]["registered_count"] == 2
    assert before["usage"]["used_count"] == 0
    assert before["usage"]["unused_count"] == 2

    assert mrt.run(recipe_id="example.used", params={"mode": "ok"})["ok"] is True

    after = _projection(manager)

    assert after["usage"]["used_count"] == 1
    assert after["usage"]["unused_count"] == 1
    assert after["usage"]["distinct_actor_count"] == 1
    assert after["usage"]["attributed_run_count"] == 1
    assert after["usage"]["unattributed_run_count"] == 0
    assert [row["recipe_id"] for row in after["usage"]["items"]] == ["example.used"]


# ---------------------------------------------------------------------------
# Per-project seeding
# ---------------------------------------------------------------------------


def _project(root: Path, *, baseline, tests=()):
    """A synthetic managed project declaring exactly ``baseline`` toolchains."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".aiworkhub").mkdir(exist_ok=True)
    tools = {
        "python": {"minimum_version": "", "candidates": [{"executable": "python3"}]},
        "ruff": {"minimum_version": "", "candidates": [{"executable": "ruff"}]},
        "node": {"minimum_version": "", "candidates": [{"executable": "node"}]},
    }
    (root / "aiworkhub.toolchain.json").write_text(
        json.dumps(
            {
                "schema_id": "aiworkhub.toolchain_registry.v1",
                "version": 1,
                "baseline": list(baseline),
                "tools": {name: tools[name] for name in baseline},
                "sandbox_capabilities": ["validation_subprocess"],
            }
        ),
        encoding="utf-8",
    )
    for relative in tests:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return root


def _evidence(
    *,
    declared: tuple[str, ...],
    modules: tuple[str, ...] = (),
    resolved: tuple[str, ...] = (),
    missing_modules: tuple[str, ...] = (),
    missing_executables: tuple[str, ...] = (),
) -> dict[str, object]:
    """One measured toolchain, stated rather than sampled from this host.

    The seeding verdict needs BOTH halves -- a project that declares a tool and
    a host that actually has it -- so a test that declares ruff and then asks
    the machine whether ruff exists is testing the machine. Measured
    2026-09-08: that is exactly how v0.12.0 went out green here, where ruff is
    installed, and red on all three CI Python jobs, which install the package
    plus pytest and no ruff. Stating the evidence tests the decision.
    """
    return {
        "available": True,
        "declared": declared,
        "declares_toolchain": bool(declared),
        "modules": frozenset(modules),
        "resolved": frozenset(resolved),
        "missing_modules": frozenset(missing_modules),
        "missing_executables": frozenset(missing_executables),
        "absent_paths": frozenset(),
        "error": "",
    }


def test_a_project_without_node_is_not_seeded_the_node_recipe(tmp_path, monkeypatch):
    """The owner's example: pytest + ruff declared and present, no Node.

    Node is withheld for the DECLARATION reason, not for absence on this host:
    a tool the project never declared is a fact about the machine, and seeding
    from it is how a Python-only repository ends up owning a Node recipe it
    will never run.
    """
    root = _project(tmp_path / "no_node", baseline=("python", "ruff"))
    monkeypatch.setattr(
        mrt,
        "_project_evidence",
        lambda _root: _evidence(
            declared=("python", "ruff"),
            modules=("pytest", "ruff"),
            resolved=("python", "ruff"),
        ),
    )

    plan = mrt.seeding_plan(root)
    selected = {recipe.id for recipe in plan["selected"]}
    withheld = {entry["recipe_id"]: entry["reasons"] for entry in plan["withheld"]}

    assert "aiworkhub.validation.node_test" not in selected
    assert "toolchain_not_declared:node" in withheld["aiworkhub.validation.node_test"]
    # The toolchains it DOES have -- declared AND present -- are seeded.
    assert "aiworkhub.validation.pytest" in selected
    assert "aiworkhub.validation.ruff_check" in selected
    # And every universal entry, unconditionally.
    assert {recipe.id for recipe in mrt.UNIVERSAL_RECIPES} <= selected


def test_a_declared_toolchain_this_host_lacks_is_still_withheld(tmp_path, monkeypatch):
    """Declaration alone does not seed: the host must actually have the tool.

    This is the half that made the first release red, so it is pinned in its
    own right rather than left to whichever machine runs the suite. ruff is a
    MODULE requirement here, matching the recipe's own ``python -m ruff``
    argv, so its absence reads as ``module_absent`` rather than a missing
    executable -- the distinction the reason strings exist to keep.
    """
    root = _project(tmp_path / "declared_absent", baseline=("python", "ruff"))
    monkeypatch.setattr(
        mrt,
        "_project_evidence",
        lambda _root: _evidence(
            declared=("python", "ruff"),
            modules=("pytest",),
            resolved=("python",),
            missing_modules=("ruff",),
        ),
    )

    plan = mrt.seeding_plan(root)
    selected = {recipe.id for recipe in plan["selected"]}
    withheld = {entry["recipe_id"]: entry["reasons"] for entry in plan["withheld"]}

    assert "aiworkhub.validation.pytest" in selected
    assert "aiworkhub.validation.ruff_check" not in selected
    assert "module_absent:ruff" in withheld["aiworkhub.validation.ruff_check"]

def test_an_unmeasurable_toolchain_withholds_every_conditional_recipe(
    tmp_path, monkeypatch
):
    """Fail closed: an unseeded recipe is a missing affordance, a wrongly
    seeded one is a manifest that lies."""
    root = _project(tmp_path / "unmeasurable", baseline=("python",))
    monkeypatch.setattr(
        mrt,
        "_project_evidence",
        lambda _root: {
            "available": False,
            "declared": (),
            "declares_toolchain": False,
            "modules": frozenset(),
            "resolved": frozenset(),
            "missing_modules": frozenset(),
            "missing_executables": frozenset(),
            "absent_paths": frozenset(),
            "error": "OSError: toolchain registry unreadable",
        },
    )

    plan = mrt.seeding_plan(root)
    selected = {recipe.id for recipe in plan["selected"]}
    withheld = {entry["recipe_id"]: entry["reasons"] for entry in plan["withheld"]}

    assert selected == {recipe.id for recipe in mrt.UNIVERSAL_RECIPES}
    assert set(withheld) == {
        requirement.recipe_id for requirement in mrt.CONDITIONAL_REQUIREMENTS
    }
    assert all(
        reasons == ["toolchain_unmeasurable:OSError: toolchain registry unreadable"]
        for reasons in withheld.values()
    )


def test_a_project_that_declares_node_is_seeded_the_node_recipe(tmp_path, monkeypatch):
    """The same decision in the other direction, so it is not vacuous."""
    root = _project(tmp_path / "with_node", baseline=("python", "ruff", "node"))
    monkeypatch.setattr(
        mrt,
        "_project_evidence",
        lambda _root: _evidence(
            declared=("python", "ruff", "node"),
            modules=("pytest", "ruff"),
            resolved=("python", "ruff", "node"),
        ),
    )

    selected = {recipe.id for recipe in mrt.seeding_plan(root)["selected"]}

    assert "aiworkhub.validation.node_test" in selected


def test_the_package_gate_is_withheld_when_the_tests_it_names_are_absent(
    tmp_path, monkeypatch
):
    """Its argv names THIS repository's two gate files as literals.

    The evidence is stated rather than sampled, for the same reason as the
    tests above: this recipe needs a module AND two paths, so asking the host
    about the module would make a path assertion fail wherever that module
    resolves differently. Measured 2026-09-08: the exact-list form read two
    path reasons here and three reasons on the CI Python jobs, where the probe
    left pytest unresolved. What this test is about is the PATHS.
    """
    absent = _project(tmp_path / "absent", baseline=("python",))
    present = _project(
        tmp_path / "present",
        baseline=("python",),
        tests=(
            "tests/test_module_size_ratchet.py",
            "tests/test_declared_invariants.py",
        ),
    )
    monkeypatch.setattr(
        mrt,
        "_project_evidence",
        lambda _root: _evidence(
            declared=("python",), modules=("pytest",), resolved=("python",)
        ),
    )

    absent_plan = mrt.seeding_plan(absent)
    present_plan = mrt.seeding_plan(present)

    withheld = {
        entry["recipe_id"]: entry["reasons"] for entry in absent_plan["withheld"]
    }
    assert withheld["aiworkhub.validation.package_gate_pytest"] == [
        "path_absent:tests/test_module_size_ratchet.py",
        "path_absent:tests/test_declared_invariants.py",
    ]
    assert "aiworkhub.validation.package_gate_pytest" in {
        recipe.id for recipe in present_plan["selected"]
    }
    # The paths are the whole difference between the two projects: same stated
    # toolchain, one seeds the gate and the other does not.
    assert {recipe.id for recipe in present_plan["selected"]} - {
        recipe.id for recipe in absent_plan["selected"]
    } == {"aiworkhub.validation.package_gate_pytest"}


def test_the_universal_half_of_the_catalogue_is_git_and_package_only(tmp_path):
    """Every always-seeded entry runs git, or a module of the installed package."""
    for recipe in mrt.UNIVERSAL_RECIPES:
        head = recipe.argv[0]
        assert isinstance(head, tr.ArgvLiteral), recipe.id
        if head.text == "git":
            continue
        assert head.text == "python", recipe.id
        assert recipe.argv[1] == tr.lit("-m"), recipe.id
        module = recipe.argv[2]
        assert isinstance(module, tr.ArgvLiteral), recipe.id
        assert module.text.startswith(mrt.OPERATOR_MODULE_PREFIX), recipe.id
    assert len(mrt.UNIVERSAL_RECIPES) + len(mrt.CONDITIONAL_RECIPES) == len(
        mrt.CANONICAL_RECIPES
    )


def test_seed_plan_reports_without_writing(manager):
    """Onboarding can see the decision before anything is stored."""
    plan = mrt.seed_plan()

    assert plan["ok"] is True
    assert "selected" not in plan  # Recipe objects are not JSON-safe evidence
    assert plan["eligible"] and plan["withheld"]
    assert store.list_recipes(manager) == []


# ---------------------------------------------------------------------------
# The packaged operator modules actually run
# ---------------------------------------------------------------------------


def test_the_packaged_operator_modules_run_as_module_arguments(manager):
    """The argv the catalogue renders is spawned for real, not just shaped.

    ``python -m aiworkhub.recipes.<name>`` against an empty repository: the
    module must start, resolve THAT repository (its cwd, never AIWorkHub's own
    checkout via ``__file__``) and refuse with its declared exit 2 because the
    store is absent -- which is a measured answer, not a failure to run.
    """
    import os
    import pathlib
    import subprocess
    import sys

    recipe = next(
        r
        for r in mrt.CANONICAL_RECIPES
        if r.id == "aiworkhub.operator.task_events"
    )
    argv = list(
        tr.build_argv(recipe, {"task_id": "T-1", "event": "all", "limit": 5})
    )
    assert argv[:3] == ["python", "-m", "aiworkhub.recipes.task_events"]

    # Import the recipe through the trusted source checkout's own package
    # root instead of whatever ambient cwd, PYTHONPATH or installation
    # happened to make this interpreter importable. The projection is a
    # fresh minimal mapping, so no inherited HOME or other caller state
    # reaches the measured child; SYSTEMROOT is the one variable a win32
    # interpreter cannot start without.
    child_env = {
        "PYTHONPATH": str(pathlib.Path(__file__).resolve().parents[1] / "src"),
    }
    if sys.platform == "win32":
        child_env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")

    # The empty manager repository needs its own `.aiworkhub` boundary
    # directory: the recipe walks ancestors for the nearest `.aiworkhub` to
    # find its repository, and without one the child climbs out of the
    # scratch manager into this checkout's real repository and live task
    # DB. The boundary stays empty -- no tasking/task_queue.sqlite -- so
    # the measured answer stays the declared exit 2.
    (manager / ".aiworkhub").mkdir(exist_ok=True)

    completed = subprocess.run(
        [sys.executable, *argv[1:]],
        cwd=str(manager),
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert completed.returncode == 2, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "task_queue_unavailable"
    # It answered about the repository it was RUN in, not the one it lives in.
    assert str(manager) in payload["detail"]


def test_the_operator_modules_resolve_the_repository_they_are_run_in(tmp_path):
    """The portability fix itself: the root comes from the cwd, not __file__."""
    from aiworkhub.recipes import _common

    managed = tmp_path / "managed"
    (managed / ".aiworkhub").mkdir(parents=True)
    nested = managed / "src" / "deep"
    nested.mkdir(parents=True)

    assert _common.resolve_repo_root(managed) == managed.resolve()
    # From a subdirectory, the nearest ancestor holding .aiworkhub/ wins.
    assert _common.resolve_repo_root(nested) == managed.resolve()
    # With no .aiworkhub anywhere above, the cwd stands rather than a guess.
    outside = tmp_path / "outside"
    outside.mkdir()
    assert _common.resolve_repo_root(outside) == outside.resolve()


def test_usage_counts_two_versions_of_one_recipe_separately(probe, manager):
    """Registry and usage must agree on identity, or ``used_count`` inflates.

    ``list_recipes`` returns one entry per ``(id, version)``, so usage grouped
    by id alone would attach one row to both versions and report two used
    recipes for one used manifest. This is live rather than theoretical: the
    operator recipes exist at 1.0.0 and 2.0.0 because their argv changed, and
    seeing that the old version stopped being run is the point.
    """
    old = probe(recipe_id="example.two", version="1.0.0")
    probe(recipe_id="example.two", version="2.0.0")
    assert len(store.list_recipes(manager)) == 2

    assert mrt.run(
        recipe_id="example.two", version="1.0.0", params={"mode": "ok"}
    )["ok"] is True

    report = mrt.usage()

    assert report["registered_count"] == 2
    assert report["used_count"] == 1
    assert report["unused_count"] == 1
    rows = {(row["recipe_id"], row["version"]): row for row in report["recipes"]}
    assert rows[("example.two", "1.0.0")]["used"] is True
    assert rows[("example.two", "1.0.0")]["runs"] == 1
    assert rows[("example.two", "2.0.0")]["used"] is False
    assert rows[("example.two", "2.0.0")]["runs"] == 0
    assert old.version == "1.0.0"

    projected = _projection(manager)
    assert projected["usage"]["used_count"] == 1
    assert projected["usage"]["unused_count"] == 1


def test_usage_does_not_credit_a_run_whose_manifest_is_gone(probe, manager):
    """A receipt outlives the manifest it names; ``used_count`` must not.

    Deriving the split from two lengths would report six used recipes for five
    used manifests plus one orphan. Membership is tested, so the orphan is
    reported under its own key and never inflates the numerator.
    """
    probe(recipe_id="example.kept")
    assert mrt.run(recipe_id="example.kept", params={"mode": "ok"})["ok"] is True

    # A receipt for a manifest that is not (or is no longer) registered.
    orphan = _probe_recipe(recipe_id="example.orphan")
    validated = tr.validate_invocation(orphan, {"mode": "ok"})
    store.put_receipt(manager, tr.build_receipt(validated))

    report = mrt.usage()

    assert report["registered_count"] == 1
    assert report["used_count"] == 1
    assert report["unused_count"] == 0
    assert [row["recipe_id"] for row in report["unregistered_usage"]] == [
        "example.orphan"
    ]

    usage = _projection(manager)["usage"]

    assert usage["state"] == "measured"
    assert usage["registered_count"] == 1
    assert usage["used_count"] == 1
    assert usage["unused_count"] == 0
    assert usage["unregistered_used_count"] == 1
    # The orphan ran with no verified route, so it is counted as nobody.
    assert usage["run_count"] == 2
    assert usage["attributed_run_count"] == 1
    assert usage["unattributed_run_count"] == 1


def test_the_panel_usage_section_is_unknown_when_the_provider_measured_none():
    """A projection that cannot measure says so instead of reporting zeros."""
    unknown = dashboard._project_recipe_usage({"registry": None}, ownership="full")

    assert unknown == {"state": "unknown", "denominator": "unknown"}
