"""Tests for the standalone typed Tool Recipe Registry foundation (RM-2026-00030)."""

from __future__ import annotations

import pytest

from aiworkhub.tool_recipes import (
    CHANGED_PATH_SHAPE,
    MAX_DISCOVERY_LIMIT,
    REASON_BAD_MANIFEST,
    REASON_BOOL_AS_INT,
    REASON_EXTRA_PARAMETER,
    REASON_INVALID_ENUM,
    REASON_INVALID_LIST_ITEM,
    REASON_INVALID_PATH,
    REASON_INVALID_TYPE,
    REASON_MISSING_PARAMETER,
    REASON_NON_LITERAL_EXECUTABLE,
    REASON_OUT_OF_RANGE,
    REASON_UNKNOWN_PARAMETER,
    REASON_UNKNOWN_RECIPE,
    REASON_UNKNOWN_VERSION,
    REASON_UNSAFE_LITERAL,
    REASON_UNSAFE_VALUE,
    REASON_UNSUPPORTED_CAPABILITY,
    REASON_UNSUPPORTED_PLATFORM,
    ArgvLiteral,
    ArgvSlot,
    CachePolicy,
    OutputSpec,
    OutputType,
    ParamSpec,
    ParamType,
    Recipe,
    RecipeError,
    RecipeRegistry,
    RiskClass,
    TaskKind,
    build_argv,
    build_receipt,
    cache_eligibility,
    discover,
    lit,
    recipe_digest,
    render_argv,
    slot,
    validate_invocation,
    validate_parameters,
)


# ---------------------------------------------------------------------------
# Fixture recipes.
# ---------------------------------------------------------------------------


def make_echo_recipe(**overrides):
    defaults = dict(
        id="echo",
        version="1.0.0",
        purpose="Echo a message",
        task_kind=TaskKind.GENERATE,
        parameters=(
            ParamSpec("message", ParamType.STR, required=True),
            ParamSpec("count", ParamType.INT, required=False, default=1, minimum=1, maximum=10),
            ParamSpec("level", ParamType.ENUM, required=False, values=("info", "warn", "error")),
        ),
        argv=(lit("echo"), slot("message")),
        cache_policy=CachePolicy.CACHEABLE,
    )
    defaults.update(overrides)
    return Recipe(**defaults)


def make_full_recipe(**overrides):
    defaults = dict(
        id="full",
        version="2.3.1",
        purpose="Full-featured fixture",
        task_kind=TaskKind.BUILD,
        parameters=(
            ParamSpec("input", ParamType.PATH, required=True),
            ParamSpec("jobs", ParamType.INT, required=False, default=2, minimum=1, maximum=16),
            ParamSpec("targets", ParamType.LIST, required=False, item_type=ParamType.STR),
            ParamSpec("verbose", ParamType.BOOL, required=False),
        ),
        platforms=("linux", "darwin"),
        capabilities=("write",),
        risk_class=RiskClass.MEDIUM,
        argv=(lit("build"), lit("--jobs"), slot("jobs"), slot("input"), slot("targets")),
        cache_policy=CachePolicy.NEVER,
    )
    defaults.update(overrides)
    return Recipe(**defaults)


# ---------------------------------------------------------------------------
# Determinism: digest / argv / receipt across mapping order and platform.
# ---------------------------------------------------------------------------


def test_digest_is_stable_across_parameter_order():
    a = make_echo_recipe()
    b = make_echo_recipe()
    assert a.digest == b.digest
    assert a.digest == recipe_digest(b)


def test_digest_is_stable_across_construction_order():
    params_ab = (ParamSpec("a", ParamType.STR), ParamSpec("b", ParamType.STR))
    params_ba = (ParamSpec("b", ParamType.STR), ParamSpec("a", ParamType.STR))
    recipe_a = Recipe(
        id="order",
        version="1.0.0",
        parameters=params_ab,
        argv=(lit("tool"), slot("a"), slot("b")),
    )
    recipe_b = Recipe(
        id="order",
        version="1.0.0",
        parameters=params_ba,
        argv=(lit("tool"), slot("a"), slot("b")),
    )
    assert recipe_a.digest == recipe_b.digest


def test_argv_independent_of_mapping_order():
    recipe = make_echo_recipe()
    argv_a = build_argv(recipe, {"message": "hello", "count": 3})
    argv_b = build_argv(recipe, {"count": 3, "message": "hello"})
    assert argv_a == argv_b == ("echo", "hello")


def test_receipt_deterministic_across_mapping_order():
    recipe = make_echo_recipe()
    inv_a = validate_invocation(recipe, {"message": "hi", "count": 2})
    inv_b = validate_invocation(recipe, {"count": 2, "message": "hi"})
    rec_a = build_receipt(inv_a, repository="repo", head="abc123")
    rec_b = build_receipt(inv_b, repository="repo", head="abc123")
    assert rec_a.digest == rec_b.digest
    assert rec_a.normalized_parameters == rec_b.normalized_parameters


def test_receipt_digest_is_hex_and_binds_escaped_params():
    recipe = make_echo_recipe()
    inv = validate_invocation(recipe, {"message": "café", "count": 2})
    rec = build_receipt(inv, repository="repo", head="head", changed_paths=["src/x.py"])
    # Digest is a stable 64-char hex string.
    assert len(rec.digest) == 64
    assert all(c in "0123456789abcdef" for c in rec.digest)
    # Non-ASCII parameter values are bound in their canonical ensure_ascii form.
    assert dict(rec.normalized_parameters)["message"] == '"caf\\u00e9"'
    # Rebuilding the same invocation yields the same digest (determinism).
    inv_again = validate_invocation(recipe, {"count": 2, "message": "café"})
    assert build_receipt(inv_again, repository="repo", head="head", changed_paths=["src/x.py"]).digest == rec.digest


# ---------------------------------------------------------------------------
# Fail-closed validation with stable reasons.
# ---------------------------------------------------------------------------


def test_unknown_recipe_fails_closed():
    registry = RecipeRegistry([make_echo_recipe()])
    with pytest.raises(RecipeError) as exc:
        registry.get("missing")
    assert exc.value.reason == REASON_UNKNOWN_RECIPE


def test_unknown_version_fails_closed():
    registry = RecipeRegistry([make_echo_recipe()])
    with pytest.raises(RecipeError) as exc:
        registry.get("echo", "99.0.0")
    assert exc.value.reason == REASON_UNKNOWN_VERSION


def test_missing_required_parameter():
    with pytest.raises(RecipeError) as exc:
        validate_parameters(make_echo_recipe(), {})
    assert exc.value.reason == REASON_MISSING_PARAMETER


def test_extra_parameter_rejected():
    with pytest.raises(RecipeError) as exc:
        validate_parameters(make_echo_recipe(), {"message": "hi", "extra": 1})
    assert exc.value.reason == REASON_EXTRA_PARAMETER


def test_bool_as_int_rejected():
    with pytest.raises(RecipeError) as exc:
        validate_parameters(make_echo_recipe(), {"message": "hi", "count": True})
    assert exc.value.reason == REASON_BOOL_AS_INT


def test_invalid_enum_rejected():
    with pytest.raises(RecipeError) as exc:
        validate_parameters(make_echo_recipe(), {"message": "hi", "level": "bogus"})
    assert exc.value.reason == REASON_INVALID_ENUM


def test_out_of_range_rejected():
    with pytest.raises(RecipeError) as exc:
        validate_parameters(make_echo_recipe(), {"message": "hi", "count": 99})
    assert exc.value.reason == REASON_OUT_OF_RANGE


def _float_list_recipe():
    return Recipe(
        id="floats",
        version="1.0.0",
        parameters=(
            ParamSpec("values", ParamType.LIST, required=True, item_type=ParamType.FLOAT),
        ),
        argv=(lit("tool"), slot("values")),
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_list_float_rejects_non_finite_item(bad):
    with pytest.raises(RecipeError) as exc:
        validate_parameters(_float_list_recipe(), {"values": [1.0, bad]})
    assert exc.value.reason == REASON_INVALID_LIST_ITEM


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_list_float_fails_closed_before_receipt(bad):
    # A non-finite LIST[FLOAT] item must fail validation with a stable
    # RecipeError, never reach build_receipt's allow_nan=False JSON encoding
    # (which would surface a raw ValueError instead of a stable reason).
    with pytest.raises(RecipeError) as exc:
        validate_invocation(_float_list_recipe(), {"values": [bad]})
    assert exc.value.reason == REASON_INVALID_LIST_ITEM


def test_list_float_accepts_finite_values_and_builds_receipt():
    recipe = _float_list_recipe()
    inv = validate_invocation(recipe, {"values": [1.5, 2, -0.25]})
    assert inv.argv == ("tool", "1.5", "2.0", "-0.25")
    rec = build_receipt(inv, repository="repo", head="abc123")
    assert len(rec.digest) == 64
    assert all(c in "0123456789abcdef" for c in rec.digest)


def test_scalar_float_rejects_non_finite():
    recipe = Recipe(
        id="scalar-float",
        version="1.0.0",
        parameters=(ParamSpec("factor", ParamType.FLOAT, required=True),),
        argv=(lit("tool"), slot("factor")),
    )
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(RecipeError) as exc:
            validate_parameters(recipe, {"factor": bad})
        assert exc.value.reason == REASON_INVALID_TYPE


def test_invalid_path_rejected():
    recipe = make_full_recipe()
    with pytest.raises(RecipeError) as exc:
        validate_parameters(recipe, {"input": ""})
    assert exc.value.reason == REASON_INVALID_PATH


def test_wrong_type_rejected():
    with pytest.raises(RecipeError) as exc:
        validate_parameters(make_echo_recipe(), {"message": 123})
    assert exc.value.reason == REASON_INVALID_TYPE


def test_unsupported_platform_rejected():
    recipe = make_full_recipe()
    with pytest.raises(RecipeError) as exc:
        validate_invocation(recipe, {"input": "src"}, platform="win32")
    assert exc.value.reason == REASON_UNSUPPORTED_PLATFORM


def test_unsupported_capability_rejected():
    recipe = make_full_recipe()
    with pytest.raises(RecipeError) as exc:
        validate_invocation(recipe, {"input": "src"}, capabilities=("network",))
    assert exc.value.reason == REASON_UNSUPPORTED_CAPABILITY


def test_platform_gating_is_skipped_when_recipe_has_no_platforms():
    recipe = make_echo_recipe()
    inv = validate_invocation(recipe, {"message": "hi"}, platform="plan9")
    assert inv.argv == ("echo", "hi")


# ---------------------------------------------------------------------------
# Shell-safety: malicious parameter / operator cases are structurally rejected.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        "hello; rm -rf /",
        "hello && echo pwned",
        "hello | cat /etc/passwd",
        "hello $(whoami)",
        "hello `id`",
        "hello > /tmp/x",
        "hello < /etc/passwd",
        'hello"',
        "hello'",
        "hello\\",
        "hello;",
        "hello$HOME",
        "hello*",
        "hello?",
        "hello!",
        "hello[0]",
        "hello{x}",
        "hello\n",
        "hello\x00",
    ],
)
def test_malicious_string_parameter_rejected(bad_value):
    with pytest.raises(RecipeError):
        validate_parameters(make_echo_recipe(), {"message": bad_value})


@pytest.mark.parametrize(
    "bad_path",
    [
        "/tmp/x; rm -rf /",
        "/tmp/$(whoami)",
        "/tmp/`id`",
        "/tmp/a|b",
        "/tmp/a>b",
        "/tmp/a&b",
        "/tmp/a\nb",
    ],
)
def test_malicious_path_parameter_rejected(bad_path):
    recipe = make_full_recipe()
    with pytest.raises(RecipeError):
        validate_parameters(recipe, {"input": bad_path})


@pytest.mark.parametrize(
    "bad_item",
    ["x; rm -rf /", "x$(id)", "x`id`", "x|y", "x>y", "x&y", "x\n"],
)
def test_malicious_list_item_rejected(bad_item):
    recipe = make_full_recipe()
    with pytest.raises(RecipeError):
        validate_parameters(recipe, {"input": "src", "targets": ["ok", bad_item]})


def test_malicious_literal_rejected_at_construction():
    with pytest.raises(RecipeError) as exc:
        Recipe(id="bad", version="1.0.0", argv=(lit("echo"), lit("x; rm -rf /")))
    assert exc.value.reason == REASON_UNSAFE_LITERAL


def test_executable_must_be_literal():
    with pytest.raises(RecipeError) as exc:
        Recipe(
            id="bad",
            version="1.0.0",
            parameters=(ParamSpec("exe", ParamType.STR),),
            argv=(slot("exe"),),
        )
    assert exc.value.reason == REASON_NON_LITERAL_EXECUTABLE


def test_argv_slot_must_reference_known_parameter():
    with pytest.raises(RecipeError) as exc:
        Recipe(id="bad", version="1.0.0", argv=(lit("echo"), slot("nope")))
    assert exc.value.reason == REASON_UNKNOWN_PARAMETER


def test_unsafe_value_reason_code():
    with pytest.raises(RecipeError) as exc:
        validate_parameters(make_echo_recipe(), {"message": "x;y"})
    assert exc.value.reason == REASON_UNSAFE_VALUE


def _raw_param_spec(**overrides):
    """Build a ParamSpec that bypasses ``__post_init__``.

    This simulates a hand-assembled manifest (e.g. one deserialized without
    revalidation) so the *defensive* invocation-time shell-safety checks can be
    exercised independently of the manifest-time gate.
    """
    spec = object.__new__(ParamSpec)
    defaults = dict(
        name="choice",
        type=ParamType.ENUM,
        required=True,
        default=None,
        values=("safe",),
        minimum=None,
        maximum=None,
        item_type=None,
        item_values=(),
    )
    defaults.update(overrides)
    for key, value in defaults.items():
        object.__setattr__(spec, key, value)
    return spec


@pytest.mark.parametrize(
    "bad_value",
    ["x; rm -rf /", "x$(id)", "x`id`", "x|y", "x>y", "x&y", "x\n", "x\x00", "x\\"],
)
def test_malicious_enum_value_rejected_at_manifest(bad_value):
    with pytest.raises(RecipeError) as exc:
        ParamSpec("level", ParamType.ENUM, required=True, values=(bad_value, "safe"))
    assert exc.value.reason == REASON_BAD_MANIFEST


@pytest.mark.parametrize(
    "bad_item",
    ["x; rm -rf /", "x$(id)", "x`id`", "x|y", "x>y", "x&y", "x\n", "x\x00", "x\\"],
)
def test_malicious_list_enum_item_value_rejected_at_manifest(bad_item):
    with pytest.raises(RecipeError) as exc:
        ParamSpec(
            "modes",
            ParamType.LIST,
            required=True,
            item_type=ParamType.ENUM,
            item_values=(bad_item, "safe"),
        )
    assert exc.value.reason == REASON_BAD_MANIFEST


def test_enum_value_reaching_argv_is_shell_safe_even_when_manifest_bypassed():
    raw = _raw_param_spec(name="level", values=("a;b",))
    recipe = Recipe(
        id="raw-enum",
        version="1.0.0",
        parameters=(raw,),
        argv=(lit("tool"), slot("level")),
    )
    with pytest.raises(RecipeError) as exc:
        validate_parameters(recipe, {"level": "a;b"})
    assert exc.value.reason == REASON_UNSAFE_VALUE


def test_list_enum_item_reaching_argv_is_shell_safe_even_when_manifest_bypassed():
    raw = _raw_param_spec(
        name="modes",
        type=ParamType.LIST,
        required=True,
        values=(),
        item_type=ParamType.ENUM,
        item_values=("a|b",),
    )
    recipe = Recipe(
        id="raw-list-enum",
        version="1.0.0",
        parameters=(raw,),
        argv=(lit("tool"), slot("modes")),
    )
    with pytest.raises(RecipeError) as exc:
        validate_parameters(recipe, {"modes": ["a|b"]})
    assert exc.value.reason == REASON_INVALID_LIST_ITEM


# ---------------------------------------------------------------------------
# Malformed manifest schema (attribute-before-type safety).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_output", ["not-a-spec", 42, object(), {"name": "x"}])
def test_malformed_output_raises_bad_manifest(bad_output):
    with pytest.raises(RecipeError) as exc:
        Recipe(id="bad-output", version="1.0.0", outputs=(bad_output,), argv=(lit("tool"),))
    assert exc.value.reason == REASON_BAD_MANIFEST


@pytest.mark.parametrize("bad_id", [123, None, object()])
def test_malformed_recipe_id_raises_bad_manifest(bad_id):
    with pytest.raises(RecipeError) as exc:
        Recipe(id=bad_id, version="1.0.0", argv=(lit("tool"),))
    assert exc.value.reason == REASON_BAD_MANIFEST


@pytest.mark.parametrize("bad_version", [123, None, ["1", "0"]])
def test_malformed_recipe_version_raises_bad_manifest(bad_version):
    with pytest.raises(RecipeError) as exc:
        Recipe(id="bad-version", version=bad_version, argv=(lit("tool"),))
    assert exc.value.reason == REASON_BAD_MANIFEST


@pytest.mark.parametrize("bad_name", [123, None, object()])
def test_malformed_param_name_raises_bad_manifest(bad_name):
    with pytest.raises(RecipeError) as exc:
        ParamSpec(bad_name, ParamType.STR)
    assert exc.value.reason == REASON_BAD_MANIFEST


@pytest.mark.parametrize("bad_name", [123, None, object()])
def test_malformed_output_name_raises_bad_manifest(bad_name):
    with pytest.raises(RecipeError) as exc:
        OutputSpec(bad_name, OutputType.FILE)
    assert exc.value.reason == REASON_BAD_MANIFEST


# ---------------------------------------------------------------------------
# Argv construction.
# ---------------------------------------------------------------------------


def test_argv_mixes_literals_and_slots():
    recipe = make_full_recipe()
    argv = build_argv(recipe, {"input": "src/main.py", "jobs": 4, "targets": ["a", "b"]})
    assert argv == ("build", "--jobs", "4", "src/main.py", "a", "b")


def test_argv_bool_renders_as_true_false():
    recipe = Recipe(
        id="flags",
        version="1.0.0",
        parameters=(ParamSpec("verbose", ParamType.BOOL),),
        argv=(lit("tool"), lit("--verbose"), slot("verbose")),
    )
    assert build_argv(recipe, {"verbose": True}) == ("tool", "--verbose", "true")
    assert build_argv(recipe, {"verbose": False}) == ("tool", "--verbose", "false")


def test_render_argv_requires_value_for_scalar_slot():
    recipe = make_full_recipe()
    with pytest.raises(RecipeError) as exc:
        render_argv(recipe, {"input": "src"})  # 'jobs' scalar slot has no value
    assert exc.value.reason == REASON_MISSING_PARAMETER


def test_list_slot_expands_but_omitted_when_optional():
    recipe = make_full_recipe()
    argv = build_argv(recipe, {"input": "src"})
    assert argv == ("build", "--jobs", "2", "src")


def test_optional_default_applied():
    recipe = make_echo_recipe()
    normalized = validate_parameters(recipe, {"message": "hi"})
    assert dict(normalized) == {"message": "hi", "count": 1}


# ---------------------------------------------------------------------------
# Discovery and bounded ranking.
# ---------------------------------------------------------------------------


def _registry_with_variety():
    echo = make_echo_recipe()
    high = make_full_recipe(
        id="high",
        version="1.0.0",
        task_kind=TaskKind.SCAN,
        platforms=("linux",),
        capabilities=("network",),
        risk_class=RiskClass.HIGH,
    )
    low = make_full_recipe(
        id="low",
        version="1.0.0",
        task_kind=TaskKind.TEST,
        risk_class=RiskClass.LOW,
        capabilities=(),
        platforms=(),
    )
    return RecipeRegistry([echo, high, low])


def test_discovery_returns_only_relevant_kinds():
    registry = _registry_with_variety()
    results = discover(registry, task_kind=TaskKind.TEST)
    assert [r.id for r in results] == ["low"]


def test_discovery_filters_by_platform():
    registry = _registry_with_variety()
    # 'high' requires the 'network' capability, so the caller must supply it
    # for the capability gate to admit the recipe alongside the platform match.
    results = discover(registry, platform="linux", capabilities=("network",))
    ids = {r.id for r in results}
    assert "high" in ids
    assert "echo" in ids
    assert "low" in ids


def test_discovery_omits_recipe_when_required_capability_missing():
    registry = _registry_with_variety()
    # Same platform filter but no capabilities: the fail-closed capability
    # gate must exclude 'high' (which requires 'network') rather than admit it.
    results = discover(registry, platform="linux")
    ids = {r.id for r in results}
    assert "high" not in ids
    assert "echo" in ids
    assert "low" in ids


def test_discovery_filters_by_capability():
    registry = _registry_with_variety()
    results = discover(registry, capabilities=("network",))
    ids = [r.id for r in results]
    # 'echo' and 'low' have no required capabilities (runnable anywhere),
    # 'high' needs 'network'. Risk-ascending order: echo (NONE), low (LOW),
    # high (HIGH).
    assert ids == ["echo", "low", "high"]


def test_discovery_filters_by_risk_max():
    registry = _registry_with_variety()
    results = discover(registry, risk_max=RiskClass.LOW)
    assert {r.id for r in results} == {"echo", "low"}


def test_discovery_ranked_by_risk_then_id():
    registry = _registry_with_variety()
    results = discover(registry)
    rank_order = {
        RiskClass.NONE: 0,
        RiskClass.LOW: 1,
        RiskClass.MEDIUM: 2,
        RiskClass.HIGH: 3,
        RiskClass.CRITICAL: 4,
    }
    ranks = [rank_order[r.risk_class] for r in results]
    assert ranks == sorted(ranks)  # non-decreasing risk
    for i in range(len(results) - 1):
        if ranks[i] == ranks[i + 1]:
            assert results[i].id < results[i + 1].id  # id ascending within a rank


def test_discovery_is_bounded():
    recipes = [
        make_echo_recipe(id=f"r{i}", version="1.0.0") for i in range(50)
    ]
    registry = RecipeRegistry(recipes)
    results = discover(registry, limit=10)
    assert len(results) == 10
    default_results = discover(registry)
    assert len(default_results) <= 32


def test_discovery_limit_rejects_none():
    registry = _registry_with_variety()
    with pytest.raises(RecipeError) as exc:
        discover(registry, limit=None)
    assert exc.value.reason == REASON_INVALID_TYPE


def test_discovery_limit_rejects_bool_and_negative():
    registry = _registry_with_variety()
    for bad in (True, False, -1):
        with pytest.raises(RecipeError):
            discover(registry, limit=bad)


def test_discovery_limit_zero_is_empty():
    registry = _registry_with_variety()
    assert discover(registry, limit=0) == ()


def test_discovery_limit_clamps_to_max():
    recipes = [
        make_echo_recipe(id=f"r{i}", version="1.0.0") for i in range(500)
    ]
    registry = RecipeRegistry(recipes)
    results = discover(registry, limit=MAX_DISCOVERY_LIMIT + 1000)
    assert len(results) == MAX_DISCOVERY_LIMIT


def test_discovery_never_injects_argv_body():
    registry = _registry_with_variety()
    results = discover(registry)
    for manifest in results:
        assert not hasattr(manifest, "argv")


def test_latest_version_resolution_deterministic():
    registry = RecipeRegistry(
        [
            make_echo_recipe(version="1.0.0"),
            make_echo_recipe(version="1.10.0"),
            make_echo_recipe(version="2.0.0"),
        ]
    )
    assert registry.get("echo").version == "2.0.0"


# ---------------------------------------------------------------------------
# Cache policy.
# ---------------------------------------------------------------------------


def test_cache_policy_explicit_never():
    recipe = make_echo_recipe(cache_policy=CachePolicy.NEVER)
    decision = cache_eligibility(recipe)
    assert decision.eligible is False
    assert "cache_policy_never" in decision.reasons


def test_cacheable_simple_recipe():
    decision = cache_eligibility(make_echo_recipe())
    assert decision.eligible is True
    assert decision.reasons == ()


def test_cache_rejects_mutable_list_params():
    recipe = make_full_recipe(cache_policy=CachePolicy.CACHEABLE, capabilities=())
    decision = cache_eligibility(recipe)
    assert decision.eligible is False
    assert "mutable_parameters" in decision.reasons


def test_cache_rejects_write_capable():
    recipe = make_echo_recipe(capabilities=("write",))
    decision = cache_eligibility(recipe)
    assert decision.eligible is False
    assert "write_capable" in decision.reasons


def test_cache_rejects_secret_bearing():
    recipe = make_echo_recipe(capabilities=("secret",))
    decision = cache_eligibility(recipe)
    assert decision.eligible is False
    assert "secret_bearing" in decision.reasons


def test_cache_rejects_security_sensitive_risk():
    recipe = make_echo_recipe(risk_class=RiskClass.CRITICAL)
    decision = cache_eligibility(recipe)
    assert decision.eligible is False
    assert "security_sensitive" in decision.reasons


# ---------------------------------------------------------------------------
# Receipt binding.
# ---------------------------------------------------------------------------


def test_receipt_binds_version_digest_and_params():
    recipe = make_echo_recipe()
    inv = validate_invocation(recipe, {"message": "hi", "count": 2})
    rec = build_receipt(inv, repository="repo", head="abc123")
    assert rec.recipe_id == "echo"
    assert rec.recipe_version == "1.0.0"
    assert rec.recipe_digest == recipe.digest
    assert rec.normalized_parameters == (("count", "2"), ("message", '"hi"'))
    assert rec.argv == ("echo", "hi")
    assert rec.repository == "repo"
    assert rec.head == "abc123"


def test_receipt_does_not_fabricate_execution():
    recipe = make_echo_recipe()
    inv = validate_invocation(recipe, {"message": "hi"})
    rec = build_receipt(inv)
    assert rec.timing.started_at is None
    assert rec.timing.finished_at is None
    assert rec.exit.status == "not_executed"
    assert rec.exit.exit_code is None
    assert rec.changed_paths == ()
    assert rec.changed_path_shape == CHANGED_PATH_SHAPE


def test_receipt_binds_changed_path_evidence():
    recipe = make_echo_recipe()
    inv = validate_invocation(recipe, {"message": "hi"})
    rec = build_receipt(inv, changed_paths=["b.py", "a.py"])
    assert rec.changed_paths == ("a.py", "b.py")


def test_receipt_rejects_bare_string_changed_paths():
    recipe = make_echo_recipe()
    inv = validate_invocation(recipe, {"message": "hi"})
    with pytest.raises(RecipeError) as exc:
        build_receipt(inv, changed_paths="a.py")
    assert exc.value.reason == REASON_INVALID_TYPE


def test_receipt_rejects_unsafe_or_non_relative_changed_paths():
    recipe = make_echo_recipe()
    inv = validate_invocation(recipe, {"message": "hi"})
    for bad in (
        "/etc/passwd",
        "../secret",
        "a/../b",
        "C:/Windows/System32",
        "a;b",
        "a|b",
        "a\x00b",
    ):
        with pytest.raises(RecipeError) as exc:
            build_receipt(inv, changed_paths=[bad])
        assert exc.value.reason == REASON_INVALID_PATH


# ---------------------------------------------------------------------------
# Registry integrity.
# ---------------------------------------------------------------------------


def test_registry_rejects_duplicate_id_version():
    with pytest.raises(RecipeError):
        RecipeRegistry([make_echo_recipe(), make_echo_recipe()])


def test_registry_allows_same_id_different_versions():
    registry = RecipeRegistry(
        [make_echo_recipe(version="1.0.0"), make_echo_recipe(version="2.0.0")]
    )
    assert registry.versions("echo") == ("1.0.0", "2.0.0")


# ---------------------------------------------------------------------------
# No execution / no side effects in the module.
# ---------------------------------------------------------------------------


def test_module_has_no_execution_primitives():
    import ast
    import inspect

    import aiworkhub.tool_recipes as mod

    # Parse the module into an AST and assert only over *actual* import
    # statements and call sites. Prose (docstrings, comments, string literals)
    # is never treated as executable authority.
    tree = ast.parse(inspect.getsource(mod))
    forbidden_modules = frozenset(
        {
            "subprocess",
            "os",
            "sys",
            "socket",
            "requests",
            "urllib",
            "sqlite3",
            "threading",
            "multiprocessing",
        }
    )
    forbidden_builtins = frozenset({"eval", "exec", "open", "compile", "__import__", "input"})

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden_modules, (
                    f"module must not import {alias.name!r}"
                )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in forbidden_modules, f"module must not import from {node.module!r}"
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                assert func.id not in forbidden_builtins, f"module must not call {func.id!r}"
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                assert func.value.id not in forbidden_modules, (
                    f"module must not call {func.value.id}.{func.attr!r}"
                )


def test_argv_tokens_are_immutable_typed():
    assert isinstance(lit("x"), ArgvLiteral)
    assert isinstance(slot("x"), ArgvSlot)
    with pytest.raises(AttributeError):
        lit("x").text = "y"  # frozen dataclass
