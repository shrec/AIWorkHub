"""Creation-time contract advisories and the launch identity the server owns.

Five measured gaps, one test file:

1. Test-scope gaps.  ``card_scope_warnings`` cross-referenced only objective
   path tokens against ``scope_files``; it never looked at the card's tests.
   Measured over 1,624 writable worker cards: 225 name a test in a validation
   command that ``allowed_writes`` does not cover, 571 declare a production
   path whose same-stem test exists on disk and is absent from the scope.
2. One shared card-text limit.  ``core.create_task`` spelled its own 300/4000
   beside ``task_templates.MAX_OBJECTIVE_LENGTH``'s 2000, so the same objective
   passed one tool and failed the other.
3. A canonical validation-command head.  5,793 declared commands used 12 first
   tokens for three tools.
4. A launch tuple derived from the card instead of retyped by the caller.
5. An unchanged-required-output exception approved by digest instead of retyped.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import (  # noqa: E402
    core,
    process_launcher,
    task_store,
    task_templates,
    toolchain_authority,
    worker_workspace,
)

_CLAUDE_IDENTITY = {
    "provider": "claude",
    "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
    "window_id": "claude_vscode_4242",
}


@pytest.fixture
def coord(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    (root / "src" / "aiworkhub").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "aiworkhub" / "widget.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "test_widget.py").write_text("def test_x(): pass\n", encoding="utf-8")
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token = tmp_path / "coordinator.token"
    token.write_text("coord-token\n", encoding="utf-8")
    os.chmod(token, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: dict(_CLAUDE_IDENTITY))
    return root


def _create(**overrides):
    kwargs = dict(
        task_id="T_ADVISORY",
        title="advisory card",
        runner="claude_coding",
        topic="coding",
        objective="change the widget",
        acceptance=["it works"],
        allowed_writes=["src/aiworkhub/widget.py"],
        required_outputs=["src/aiworkhub/widget.py"],
        validation=["python3 -m pytest -q tests/test_widget.py"],
        callback_required=False,
        custom_template_escape="audited_custom_unclassified",
    )
    kwargs.update(overrides)
    return core.create_task(**kwargs)


# --- 1. test-scope gaps ----------------------------------------------------

def test_validation_named_test_outside_allowed_writes_is_warned(coord):
    result = _create()
    assert result["ok"] is True, result
    paths = [row["path"] for row in result["test_scope_warnings"]]
    assert paths == ["tests/test_widget.py"]
    assert result["test_scope_warnings"][0]["source"] == "validation_command"
    # Advisory, never applied: the stored card keeps the declared scope.
    assert result["suggested_allowed_writes"] == ["tests/test_widget.py"]
    card = json.loads(result["stdout"])
    assert card["allowed_writes"] == ["src/aiworkhub/widget.py"]


def test_same_stem_test_that_exists_on_disk_is_warned(coord):
    result = _create(
        task_id="T_SAME_STEM",
        validation=["python3 -m ruff check src/aiworkhub/widget.py"],
    )
    assert result["ok"] is True, result
    assert result["test_scope_warnings"] == [
        {
            "path": "tests/test_widget.py",
            "source": "same_stem",
            "detail": (
                "exists on disk and asserts the contract of "
                "src/aiworkhub/widget.py"
            ),
        }
    ]


def test_same_stem_test_absent_from_disk_is_not_warned(coord):
    (coord / "src" / "aiworkhub" / "lonely.py").write_text("y = 2\n", encoding="utf-8")
    result = _create(
        task_id="T_NO_SIBLING",
        allowed_writes=["src/aiworkhub/lonely.py"],
        required_outputs=["src/aiworkhub/lonely.py"],
        validation=["python3 -m ruff check src/aiworkhub/lonely.py"],
    )
    assert result["ok"] is True, result
    assert result["test_scope_warnings"] == []
    assert result["suggested_allowed_writes"] == []


def test_declared_read_only_test_is_suppressed_not_warned(coord):
    result = _create(
        task_id="T_READ_ONLY_TEST",
        immutable_inputs=["tests/test_widget.py"],
    )
    assert result["ok"] is True, result
    assert result["test_scope_warnings"] == []
    assert result["suppressed_read_only"] == ["tests/test_widget.py"]


def test_covered_test_produces_no_warning(coord):
    result = _create(
        task_id="T_COVERED",
        allowed_writes=["src/aiworkhub/widget.py", "tests/test_widget.py"],
    )
    assert result["ok"] is True, result
    assert result["test_scope_warnings"] == []


def test_card_test_scope_warnings_is_a_pure_read(tmp_path):
    """It reports; it never mutates the card it was handed."""
    card = {
        "allowed_writes": ["src/aiworkhub/widget.py"],
        "validation": ["python3 -m pytest -q tests/test_widget.py"],
    }
    before = json.dumps(card, sort_keys=True)
    out = core.card_test_scope_warnings(card, repo=tmp_path)
    assert json.dumps(card, sort_keys=True) == before
    assert set(out) == {
        "test_scope_warnings",
        "suggested_allowed_writes",
        "suppressed_read_only",
    }


# --- 2. one shared card-text limit ----------------------------------------

def test_objective_limit_is_one_constant_for_both_creation_paths():
    assert task_templates.MAX_OBJECTIVE_LENGTH == 4000
    assert task_templates.MAX_TITLE_LENGTH == 300


def test_objective_accepted_by_the_template_path_is_accepted_by_raw_create(coord):
    objective = "o" * (task_templates.MAX_OBJECTIVE_LENGTH - 1)
    # The template path accepts it ...
    assert (
        task_templates._bounded_text(
            objective, "objective", task_templates.MAX_OBJECTIVE_LENGTH
        )
        == objective
    )
    # ... and so does raw create, because both read the same constant.
    result = _create(task_id="T_LONG_OBJECTIVE", objective=objective)
    assert result["ok"] is True, result


def test_over_limit_objective_names_field_length_and_limit(coord):
    limit = task_templates.MAX_OBJECTIVE_LENGTH
    result = _create(task_id="T_TOO_LONG", objective="o" * (limit + 5))
    assert result["ok"] is False
    assert result["limits"]["objective"] == limit
    violation = next(
        row for row in result["violations"] if row["field"] == "objective"
    )
    assert violation["length"] == limit + 5
    assert violation["limit"] == limit
    assert f"{limit + 5}_chars_exceeds_limit_{limit}" in violation["code"]


# --- 3. canonical validation-command head ----------------------------------

def test_canonical_head_matches_the_finalizer_bare_interpreter_pattern():
    """The restated pattern must stay identical to the resolver's own."""
    assert (
        task_templates._BARE_PYTHON_INTERPRETER_RE.pattern
        == worker_workspace._BARE_PYTHON_INTERPRETER_RE.pattern
    )
    # And the canonical spelling is one that pattern accepts, so the finalizer
    # replaces it with sys.executable rather than exec'ing it verbatim.
    assert task_templates._BARE_PYTHON_INTERPRETER_RE.match(
        task_templates.CANONICAL_VALIDATION_PYTHON
    )


@pytest.mark.parametrize(
    ("declared", "canonical"),
    [
        ("python -m pytest -q tests/test_a.py", "python3 -m pytest -q tests/test_a.py"),
        ("python3.11 -m ruff check src/a.py", "python3 -m ruff check src/a.py"),
        ("pytest -q tests/test_a.py", "python3 -m pytest -q tests/test_a.py"),
        (
            "PYTHONPATH=. python -m pytest -q tests/test_a.py",
            "PYTHONPATH=. python3 -m pytest -q tests/test_a.py",
        ),
        (
            "cd sub && python -m pytest -q tests/test_a.py",
            "cd sub && python3 -m pytest -q tests/test_a.py",
        ),
        # A candidate pytest wrapper is recognized ONLY for the head python3.
        (
            "python tools/candidate_pytest.py -q",
            "python3 tools/candidate_pytest.py -q",
        ),
    ],
)
def test_provably_equivalent_heads_fold_onto_the_canonical_spelling(declared, canonical):
    assert task_templates.canonical_validation_command(declared) == canonical


@pytest.mark.parametrize(
    "command",
    [
        # Absolute heads are left exactly as declared.
        "/usr/bin/python3.12 -m pytest -q tests/test_a.py",
        # A repo-relative head resolves through a different, root-bearing
        # branch of the finalizer's resolver; folding it would change which
        # executable runs.
        ".venv/bin/python -m pytest -q tests/test_a.py",
        ".venv/bin/ruff check src/a.py",
        # Trusted bare validators and system tools are not interpreters.
        "ruff check src/a.py",
        "mypy src/a.py",
        "node --test tests/a.test.js",
        "git diff --check",
    ],
)
def test_non_equivalent_heads_are_never_rewritten(command):
    assert task_templates.canonical_validation_command(command) == command


def test_templates_emit_the_canonical_head():
    card = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    heads = {command.split(" ")[0] for command in card["validation"]}
    assert "python" not in heads
    assert task_templates.CANONICAL_VALIDATION_PYTHON in heads


def test_a_persisted_legacy_python_card_still_authenticates():
    """435 stored cards carry template provenance and a bare ``python`` head.

    Byte-exact re-expansion would de-authenticate every one of them, and a
    de-authenticated template card fails ``_validate_required_outputs_contract``
    at launch with ``required_outputs_invalid``.
    """
    expected = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    legacy = [
        command.replace("python3 -m ", "python -m ", 1)
        for command in expected["validation"]
    ]
    assert legacy != expected["validation"]
    assert task_templates._expansion_field_matches(
        "validation", legacy, expected["validation"]
    )
    # Every other field is still compared exactly.
    assert not task_templates._expansion_field_matches(
        "allowed_writes", ["src/other.py"], expected["allowed_writes"]
    )


def test_create_reports_a_non_canonical_head_without_rewriting_it(coord):
    result = _create(
        task_id="T_NON_CANONICAL",
        validation=["pytest -q tests/test_widget.py"],
    )
    assert result["ok"] is True, result
    assert result["validation_normalization"] == [
        {
            "declared": "pytest -q tests/test_widget.py",
            "canonical": "python3 -m pytest -q tests/test_widget.py",
        }
    ]
    # The stored command is untouched: the declared command IS the acceptance
    # evidence, and a template card's provenance digest hashes this exact list.
    card = json.loads(result["stdout"])
    assert card["validation"] == ["pytest -q tests/test_widget.py"]


def test_create_warns_about_a_venv_head_the_worker_worktree_cannot_supply(coord):
    result = _create(
        task_id="T_VENV_HEAD",
        validation=[".venv/bin/python -m pytest -q tests/test_widget.py"],
    )
    assert result["ok"] is True, result
    assert result["validation_head_warnings"] == [
        "venv_relative_head_absent_from_worker_worktree:.venv/bin/python"
    ]


# --- 3b. card-contract inputs, named at creation ---------------------------

def test_missing_validation_input_is_named_at_creation(coord):
    result = _create(
        task_id="T_MISSING_INPUT",
        validation=["python3 -m pytest -q tests/test_absent.py"],
    )
    # Advisory, not a refusal: dependency_autolaunch classifies
    # workspace_required_input_missing as TRANSIENT because a sibling card's
    # accept can promote the file into the canonical tree.
    assert result["ok"] is True, result
    assert result["contract_warnings"] == [
        "workspace_required_input_missing:tests/test_absent.py"
    ]
    assert "the launcher refuses this contract" in result["contract_hint"]


def test_a_declared_output_is_never_a_missing_input(coord):
    result = _create(
        task_id="T_TO_BE_CREATED",
        allowed_writes=["src/aiworkhub/widget.py", "tests/test_absent.py"],
        required_outputs=["tests/test_absent.py"],
        validation=["python3 -m pytest -q tests/test_absent.py"],
    )
    assert result["ok"] is True, result
    assert "contract_warnings" not in result


def test_card_contract_missing_inputs_ignores_host_capability(tmp_path):
    """It answers only about the card and the repository, never the host."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_present.py").write_text("", encoding="utf-8")
    card = {
        "validation": [
            "python3 -m pytest -q tests/test_present.py",
            "some_tool_that_does_not_exist --check",
        ],
        "allowed_writes": [],
        "required_outputs": [],
    }
    assert toolchain_authority.card_contract_missing_inputs(tmp_path, card) == ()


# --- 4. launch identity derived from the card ------------------------------

def _launchable_card(**overrides):
    card = {
        "task_id": "T_LAUNCH",
        "runner": "claude_opus-5",
        "topic": "coding",
        "task_type": "code",
        "allowed_writes": ["src/a.py"],
    }
    card.update(overrides)
    return card


def test_runner_and_topic_are_derived_from_the_card(tmp_path):
    derived = process_launcher.derive_launch_identity(tmp_path, _launchable_card())
    assert derived["runner"] == "claude_opus-5"
    assert derived["topic"] == "coding"
    assert derived["derived_from"]["runner"] == "card"
    assert derived["derived_from"]["topic"] == "card"


def test_adapter_is_the_first_pinnable_launchable_in_canonical_tuple_order(tmp_path):
    derived = process_launcher.derive_launch_identity(tmp_path, _launchable_card())
    tuple_order = process_launcher.adapter_identity_tuple("claude_opus-5")
    assert derived["adapter_candidates"] == list(tuple_order)
    # claude_opus-5's tuple leads with vscode_lm, which owns NO canonical
    # workforce row; deriving it would pin a model that validate_workforce_
    # identity refuses with workforce_route_absent one line later.
    assert tuple_order[0] == "vscode_lm"
    assert ("claude_opus-5", "vscode_lm") not in process_launcher._CANONICAL_WORKFORCE
    assert derived["adapter_id"] == "claude_cli"
    assert (
        derived["derived_from"]["adapter_id"]
        == "first_pinnable_launchable_in_tuple_order"
    )


def test_a_derived_adapter_always_survives_the_identity_validation(tmp_path):
    """Derivation may never produce a tuple the next check refuses."""
    for runner in ("claude_opus-5", "claude_sonnet-5", "claude_haiku", "codex_gpt-5.5"):
        derived = process_launcher.derive_launch_identity(
            tmp_path, _launchable_card(runner=runner)
        )
        assert process_launcher.validate_workforce_identity(
            derived["runner"], derived["adapter_id"], derived["model"]
        ) == derived["model"]


def test_a_runner_with_no_canonical_row_keeps_the_plain_policy_walk(tmp_path):
    derived = process_launcher.derive_launch_identity(
        tmp_path, _launchable_card(runner="glm_5")
    )
    tuple_order = process_launcher.adapter_identity_tuple("glm_5")
    assert derived["adapter_id"] == tuple_order[0]
    assert derived["derived_from"]["adapter_id"] == "first_launchable_in_tuple_order"


def test_derivation_is_deterministic_across_repeated_calls(tmp_path):
    """Two launches of one card can never pick different routes."""
    first = process_launcher.derive_launch_identity(tmp_path, _launchable_card())
    second = process_launcher.derive_launch_identity(tmp_path, _launchable_card())
    assert first == second


def test_an_explicit_value_always_wins_over_derivation(tmp_path):
    derived = process_launcher.derive_launch_identity(
        tmp_path, _launchable_card(), adapter_id="claude_cli"
    )
    assert derived["adapter_id"] == "claude_cli"
    assert "adapter_id" not in derived["derived_from"]


def test_model_is_pinned_from_the_canonical_workforce_table(tmp_path):
    derived = process_launcher.derive_launch_identity(
        tmp_path, _launchable_card(), adapter_id="claude_cli"
    )
    assert derived["model"] == "claude-opus-5"
    assert derived["derived_from"]["model"] == "canonical_workforce"
    # And the derived tuple still passes the unchanged identity validation.
    assert (
        process_launcher.validate_workforce_identity(
            derived["runner"], derived["adapter_id"], derived["model"]
        )
        == "claude-opus-5"
    )


def test_a_card_without_a_runner_is_refused_not_guessed(tmp_path):
    with pytest.raises(process_launcher.LaunchRejected) as excinfo:
        process_launcher.derive_launch_identity(
            tmp_path, _launchable_card(runner="", topic="")
        )
    assert "launch_identity_underivable" in str(excinfo.value)


def test_a_runner_family_with_no_adapter_tuple_is_refused_not_guessed(tmp_path):
    with pytest.raises(process_launcher.LaunchRejected) as excinfo:
        process_launcher.derive_launch_identity(
            tmp_path, _launchable_card(runner="mystery_worker")
        )
    assert "launch_adapter_underivable" in str(excinfo.value)


def test_adapter_identity_tuple_is_the_validator_table(tmp_path):
    """The extracted tuple and the validator can never disagree."""
    for runner in (
        "claude",
        "claude_opus-5",
        "codex_gpt-5.5",
        "deepseek_v4",
        "glm_5",
        "copilot_x",
        "opencode_openai_gpt-5.6-sol",
    ):
        allowed = process_launcher.adapter_identity_tuple(runner)
        assert allowed
        for adapter in allowed:
            process_launcher._validate_adapter_identity(runner, adapter)
        with pytest.raises(process_launcher.LaunchRejected):
            process_launcher._validate_adapter_identity(runner, "not_an_adapter")


def _derivation_manager(tmp_path, card):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)

    def show_task(task_id, *_args, **_kwargs):
        return {
            "returncode": 0,
            "stdout": json.dumps(card),
            "stderr": "",
        }

    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=show_task,
        isolation_enabled=False,
    )


def test_launch_derives_the_whole_tuple_when_the_caller_omits_it(tmp_path, monkeypatch):
    card = _launchable_card(task_id="T_DERIVED")
    manager = _derivation_manager(tmp_path, card)
    seen: dict[str, object] = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "task_id": kwargs["task_id"]}

    monkeypatch.setattr(manager, "_launch_direct_for_tests", _capture)
    result = manager.launch(task_id="T_DERIVED")

    assert result["ok"] is True
    assert seen["runner"] == "claude_opus-5"
    assert seen["topic"] == "coding"
    assert seen["adapter_id"] == "claude_cli"
    assert seen["model"] == "claude-opus-5"
    # The receipt records HOW the identity was decided.
    assert result["launch_identity_derivation"]["derived_from"] == {
        "runner": "card",
        "topic": "card",
        "adapter_id": "first_pinnable_launchable_in_tuple_order",
        "model": "canonical_workforce",
    }


def test_launch_with_an_explicit_tuple_derives_nothing(tmp_path, monkeypatch):
    card = _launchable_card(task_id="T_EXPLICIT")
    manager = _derivation_manager(tmp_path, card)

    def _capture(**kwargs):
        return {"ok": True, "task_id": kwargs["task_id"]}

    monkeypatch.setattr(manager, "_launch_direct_for_tests", _capture)
    result = manager.launch(
        task_id="T_EXPLICIT",
        runner="claude_opus-5",
        topic="coding",
        adapter_id="claude_cli",
    )
    assert result["ok"] is True
    assert "launch_identity_derivation" not in result


def _refuse_any_claim(manager, monkeypatch):
    """Both launch paths own the claim; reaching either would be a post-claim refusal."""
    def _claimed(**_kwargs):
        raise AssertionError("an underivable identity reached the claim")

    monkeypatch.setattr(manager, "_launch_direct_for_tests", _claimed)
    monkeypatch.setattr(manager, "_launch_isolated", _claimed)


@pytest.mark.parametrize(
    ("runner", "topic", "reason"),
    [
        ("", "", "launch_identity_underivable"),
        ("mystery_worker", "coding", "launch_adapter_underivable"),
    ],
)
def test_launch_refuses_an_underivable_identity_before_the_claim(
    tmp_path, monkeypatch, runner, topic, reason,
):
    card = _launchable_card(
        task_id="T_UNDERIVABLE",
        runner=runner,
        topic=topic,
        status="pending",
        worker_status="unclaimed",
        claim_epoch=0,
    )
    before = json.loads(json.dumps(card))
    manager = _derivation_manager(tmp_path, card)
    _refuse_any_claim(manager, monkeypatch)
    result = manager.launch(task_id="T_UNDERIVABLE")
    assert result["ok"] is False
    assert reason in result["blocked_reason"]
    # A refusal, not a blocked transition the event ledger projects onto the card.
    assert result["state"] == "refused"
    assert result["state"] != "blocked"
    # Pre-claim: the card (status, worker_status, claim_epoch) is untouched.
    assert card == before
    shown = json.loads(manager._show_task("T_UNDERIVABLE")["stdout"])
    assert shown["status"] == "pending"
    assert shown["worker_status"] == "unclaimed"
    assert shown["claim_epoch"] == 0


def test_launch_derives_claude_cli_for_the_bare_claude_runner(tmp_path, monkeypatch):
    """NF-2026-01125: runner ``claude`` with no adapter_id launches on claude_cli."""
    card = _launchable_card(task_id="T_BARE_CLAUDE", runner="claude")
    manager = _derivation_manager(tmp_path, card)
    seen: dict[str, object] = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "task_id": kwargs["task_id"]}

    monkeypatch.setattr(manager, "_launch_direct_for_tests", _capture)
    result = manager.launch(task_id="T_BARE_CLAUDE")

    assert result["ok"] is True, result
    assert seen["runner"] == "claude"
    assert seen["adapter_id"] == "claude_cli"
    derived_from = result["launch_identity_derivation"]["derived_from"]
    assert derived_from["adapter_id"] == "first_launchable_in_tuple_order"


def test_an_explicit_adapter_for_the_bare_claude_runner_is_kept_and_validated(tmp_path):
    derived = process_launcher.derive_launch_identity(
        tmp_path, _launchable_card(runner="claude"), adapter_id="claude_cli"
    )
    assert derived["adapter_id"] == "claude_cli"
    assert "adapter_id" not in derived["derived_from"]
    process_launcher._validate_adapter_identity("claude", derived["adapter_id"])
    # The editor bridge stays acceptable, exactly as for the ``claude_`` family.
    process_launcher._validate_adapter_identity("claude", "vscode_lm")
    with pytest.raises(process_launcher.LaunchRejected):
        process_launcher._validate_adapter_identity("claude", "codex_cli")


def test_both_new_launch_denials_are_classified_for_autolaunch():
    from aiworkhub import dependency_autolaunch

    for reason in ("launch_identity_underivable", "launch_adapter_underivable"):
        assert reason in dependency_autolaunch.TRANSIENT_DENIAL_REASONS


# --- 4b. derivation skips a route whose failure circuit is open --------------
#
# NF-2026-01036 part A: an unpinned claude_* launch derived ``vscode_lm`` while
# that route's credit balance was exhausted, because derivation consulted only
# ``repo_policy``.  The sealed refusal below is exactly what the launcher records
# for the measured extension-host credit-limit error.

def _sealed_balance_observations(monkeypatch, *, open_adapter):
    from datetime import datetime, timedelta, timezone

    from aiworkhub import workforce_catalog

    failed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    reset_at = datetime.now(timezone.utc) + timedelta(days=3)

    def observations(_root, *, adapter_id, model, since_iso):
        if adapter_id != open_adapter:
            return []
        return [{
            "adapter_id": adapter_id,
            "model": model,
            "state": "worker_failed",
            "error": "provider_refused_balance_exhausted_recoverable_after_reported_window",
            "finished_at": failed_at.isoformat(),
            "provider_error": {
                "schema_id": "aiworkhub.provider_route_error.v1",
                "owner": "provider",
                "sealed": True,
                "source": "vscode_lm_extension_response",
                "code": "insufficient_balance",
                "http_status": 402,
                "reset_at": reset_at.isoformat(),
            },
        }]

    monkeypatch.setattr(
        workforce_catalog.task_store, "route_terminal_observations", observations
    )
    monkeypatch.setattr(
        process_launcher.repo_policy,
        "validate_launch",
        lambda _repo, _card, _adapter: {"ok": True},
    )


def test_derivation_skips_an_open_route_circuit_and_says_why(tmp_path, monkeypatch):
    card = _launchable_card(runner="claude_sonnet-5")
    assert process_launcher.adapter_identity_tuple("claude_sonnet-5")[:2] == (
        "vscode_lm", "claude_cli",
    )
    _sealed_balance_observations(monkeypatch, open_adapter="nobody")
    baseline = process_launcher.derive_launch_identity(tmp_path, card)
    assert baseline["adapter_id"] == "vscode_lm"
    assert "adapter_circuit_skipped" not in baseline["derived_from"]

    _sealed_balance_observations(monkeypatch, open_adapter="vscode_lm")
    derived = process_launcher.derive_launch_identity(tmp_path, card)

    assert derived["adapter_id"] == "claude_cli"
    assert derived["model"] == "claude-sonnet-5"
    assert (
        derived["derived_from"]["adapter_id"]
        == "first_pinnable_launchable_in_tuple_order"
    )
    skipped = derived["derived_from"]["adapter_circuit_skipped"]
    assert skipped.startswith("vscode_lm:route_circuit_open:quota:reset_at=")
    assert any(
        rejection.startswith("vscode_lm:route_circuit_open")
        for rejection in derived["adapter_rejections"]
    )


def test_an_explicit_adapter_pin_is_never_overridden_by_the_circuit(
    tmp_path, monkeypatch,
):
    _sealed_balance_observations(monkeypatch, open_adapter="vscode_lm")
    derived = process_launcher.derive_launch_identity(
        tmp_path, _launchable_card(runner="claude_sonnet-5"), adapter_id="vscode_lm",
    )
    assert derived["adapter_id"] == "vscode_lm"
    assert "adapter_id" not in derived["derived_from"]
    assert "adapter_circuit_skipped" not in derived["derived_from"]


def test_every_candidate_open_is_refused_with_the_reason(tmp_path, monkeypatch):
    from aiworkhub import workforce_catalog

    _sealed_balance_observations(monkeypatch, open_adapter="vscode_lm")
    real = workforce_catalog.task_store.route_terminal_observations

    def both_open(root, *, adapter_id, model, since_iso):
        return real(root, adapter_id="vscode_lm", model=model, since_iso=since_iso)

    monkeypatch.setattr(
        workforce_catalog.task_store, "route_terminal_observations", both_open
    )
    with pytest.raises(process_launcher.LaunchRejected) as excinfo:
        process_launcher.derive_launch_identity(
            tmp_path, _launchable_card(runner="claude_sonnet-5")
        )
    assert "launch_adapter_underivable" in str(excinfo.value)
    assert "route_circuit_open" in str(excinfo.value)


def test_create_warns_when_a_folded_runner_has_no_route(coord):
    result = _create(task_id="T_NO_ROUTE", runner="phantom_model_9")
    assert result["ok"] is True, result
    assert any(
        warning.startswith("workforce_route_absent:runner=phantom_model_9")
        for warning in result["runner_route_warnings"]
    )


def test_create_does_not_warn_for_a_registered_route(coord):
    result = _create(task_id="T_ROUTE_OK", runner="claude_opus-5")
    assert result["ok"] is True, result
    assert "runner_route_warnings" not in result


# --- 5. contract patch: approve by reference -------------------------------

def test_contract_patch_only_proposes_paths_already_in_both_declarations():
    patch = task_templates.build_contract_patch(
        task_id="T_PATCH",
        unchanged_paths=["src/a.py", "src/b.py", "src/c.py"],
        required_outputs=["src/a.py", "src/b.py"],
        allowed_writes=["src/a.py", "src/c.py"],
    )
    # src/b.py is a required output but outside the write scope; src/c.py is in
    # the write scope but is not a required output. Neither can be proposed.
    assert patch["allow_unchanged_required_outputs"] == ["src/a.py"]
    assert patch["rejected"] == [
        {"path": "src/b.py", "reason": "not_in_allowed_writes"},
        {"path": "src/c.py", "reason": "not_in_required_outputs"},
    ]
    assert patch["digest"] == task_templates.contract_patch_digest(patch)


def test_contract_patch_roundtrips_and_reauthenticates(tmp_path):
    patch = task_templates.build_contract_patch(
        task_id="T_PATCH",
        unchanged_paths=["src/a.py"],
        required_outputs=["src/a.py"],
        allowed_writes=["src/a.py"],
    )
    digest = task_templates.record_contract_patch(tmp_path, patch)
    assert digest == patch["digest"]
    loaded = task_templates.load_contract_patch(tmp_path, digest)
    assert loaded["allow_unchanged_required_outputs"] == ["src/a.py"]


def test_a_tampered_contract_patch_is_refused(tmp_path):
    patch = task_templates.build_contract_patch(
        task_id="T_PATCH",
        unchanged_paths=["src/a.py"],
        required_outputs=["src/a.py"],
        allowed_writes=["src/a.py"],
    )
    digest = task_templates.record_contract_patch(tmp_path, patch)
    target = tmp_path / task_templates.CONTRACT_PATCH_RELATIVE_DIR / f"{digest}.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["allow_unchanged_required_outputs"] = ["src/secret.py"]
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(task_templates.TaskTemplateError) as excinfo:
        task_templates.load_contract_patch(tmp_path, digest)
    assert "contract_patch_digest_mismatch" in str(excinfo.value)


def test_create_applies_a_recorded_patch_by_digest(coord):
    patch = task_templates.build_contract_patch(
        task_id="T_PATCH_CREATE",
        unchanged_paths=["src/aiworkhub/widget.py"],
        required_outputs=["src/aiworkhub/widget.py"],
        allowed_writes=["src/aiworkhub/widget.py"],
    )
    digest = task_templates.record_contract_patch(coord, patch)
    result = _create(task_id="T_PATCH_CREATE", apply_contract_patch=digest)
    assert result["ok"] is True, result
    assert result["applied_contract_patch"]["digest"] == digest
    card = json.loads(result["stdout"])
    assert card["allow_unchanged_required_outputs"] == ["src/aiworkhub/widget.py"]


def test_create_refuses_a_digest_and_an_explicit_list_together(coord):
    patch = task_templates.build_contract_patch(
        task_id="T_PATCH_BOTH",
        unchanged_paths=["src/aiworkhub/widget.py"],
        required_outputs=["src/aiworkhub/widget.py"],
        allowed_writes=["src/aiworkhub/widget.py"],
    )
    digest = task_templates.record_contract_patch(coord, patch)
    result = _create(
        task_id="T_PATCH_BOTH",
        apply_contract_patch=digest,
        allow_unchanged_required_outputs=["src/aiworkhub/widget.py"],
    )
    assert result["ok"] is False
    assert "contract_patch_conflicts_with_explicit_list" in result["stderr"]


def test_create_refuses_a_patch_recorded_for_another_task(coord):
    patch = task_templates.build_contract_patch(
        task_id="SOME_OTHER_TASK",
        unchanged_paths=["src/aiworkhub/widget.py"],
        required_outputs=["src/aiworkhub/widget.py"],
        allowed_writes=["src/aiworkhub/widget.py"],
    )
    digest = task_templates.record_contract_patch(coord, patch)
    result = _create(task_id="T_PATCH_WRONG", apply_contract_patch=digest)
    assert result["ok"] is False
    assert "contract_patch_task_mismatch:SOME_OTHER_TASK" in result["stderr"]


def test_create_refuses_an_unknown_digest(coord):
    result = _create(task_id="T_PATCH_UNKNOWN", apply_contract_patch="0" * 64)
    assert result["ok"] is False
    assert "invalid_contract_patch:contract_patch_not_found" in result["stderr"]


def test_a_patched_list_still_faces_the_normal_exception_validation(coord):
    """Approving by reference does not bypass a single existing check."""
    patch = task_templates.build_contract_patch(
        task_id="T_PATCH_SCOPE",
        unchanged_paths=["src/aiworkhub/widget.py"],
        required_outputs=["src/aiworkhub/widget.py"],
        allowed_writes=["src/aiworkhub/widget.py"],
    )
    digest = task_templates.record_contract_patch(coord, patch)
    # The card created now declares a DIFFERENT required-output set, so the
    # resolved list must fail validate_required_output_exceptions.
    (coord / "src" / "aiworkhub" / "other.py").write_text("z = 3\n", encoding="utf-8")
    result = _create(
        task_id="T_PATCH_SCOPE",
        allowed_writes=["src/aiworkhub/other.py"],
        required_outputs=["src/aiworkhub/other.py"],
        validation=["python3 -m ruff check src/aiworkhub/other.py"],
        apply_contract_patch=digest,
    )
    assert result["ok"] is False
    assert "allow_unchanged_required_output_not_in_required_outputs" in result["stderr"]
