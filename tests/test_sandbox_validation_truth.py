"""NF-WAVE-SANDBOX-TRUTH: validation terminal-state truth.

Two properties, tested at the classification layer so they need no real sandbox:

* "the command could not run here" is a terminal state DISTINCT from
  ``validation_failed`` that names the restriction and is operationally
  recoverable (no supersede) -- proven for four distinct restrictions.
* ``validation_failed`` keeps its exact acceptance-blocking meaning for a
  candidate that genuinely failed its gate, including when an environment
  restriction is also present in the same batch.

Plus the create-time contract: a card declaring a validation command that
cannot run in the sandbox is rejected at creation with the reason named.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import validation_runner  # noqa: E402


def _launch_row(command: str, *, launch_error: str, stderr: str, argv: list[str]) -> dict:
    return {
        "command": command,
        "argv": argv,
        "executed_argv": argv,
        "returncode": None,
        "timed_out": False,
        "launch_error": launch_error,
        "launch_error_message": stderr,
        "stderr_tail": stderr,
        "stdout_tail": "",
        "failure_receipt": {
            "failure_class": (
                "permission_denied"
                if launch_error == "PermissionError"
                else "executable_unavailable"
            )
        },
    }


def _nonzero_row(
    command: str,
    *,
    returncode: int,
    stderr: str,
    argv: list[str],
    failure_class: str,
    absent_validator_modules: tuple[str, ...] | None = None,
) -> dict:
    row = {
        "command": command,
        "argv": argv,
        "executed_argv": argv,
        "returncode": returncode,
        "timed_out": False,
        "stderr_tail": stderr,
        "stdout_tail": "",
        "failure_receipt": {"failure_class": failure_class},
    }
    if absent_validator_modules is not None:
        # The runner-authored import-probe result (run_validations proved the
        # module genuinely absent in the interpreter it invoked). It is a
        # STRUCTURAL field the candidate cannot author, unlike ``stderr``.
        row["absent_validator_modules"] = list(absent_validator_modules)
    return row


# ── ONE: distinct, recoverable environment-blocked state (NF-271/298) ────────


def test_four_distinct_restrictions_map_to_recoverable_environment_blocked() -> None:
    refused_chmod = _launch_row(
        "python setup.py install",
        launch_error="PermissionError",
        stderr="PermissionError: [Errno 1] Operation not permitted: chmod '/opt/x'",
        argv=["python", "setup.py", "install"],
    )
    absent_interpreter = _launch_row(
        "mypy src",
        launch_error="FileNotFoundError",
        stderr="[Errno 2] No such file or directory: 'mypy'",
        argv=["mypy", "src"],
    )
    forbidden_spawn = _launch_row(
        "bash -c 'exec true'",
        launch_error="PermissionError",
        stderr="seccomp denied clone(): Operation not permitted",
        argv=["bash", "-c", "exec true"],
    )
    missing_package = _nonzero_row(
        "python -m pytest -q tests/test_x.py",
        returncode=1,
        stderr="ModuleNotFoundError: No module named 'pytest'",
        argv=["python", "-m", "pytest", "-q", "tests/test_x.py"],
        failure_class="test_failure",
        # Decided by the runner's import probe, NOT by the stderr text above.
        absent_validator_modules=("pytest",),
    )

    cases = {
        validation_runner.RESTRICTION_REFUSED_CHMOD: refused_chmod,
        validation_runner.RESTRICTION_ABSENT_INTERPRETER: absent_interpreter,
        validation_runner.RESTRICTION_FORBIDDEN_SPAWN: forbidden_spawn,
        validation_runner.RESTRICTION_MISSING_PACKAGE: missing_package,
    }

    seen: set[str] = set()
    for expected_restriction, row in cases.items():
        terminal = validation_runner.classify_validation_results([row])
        assert terminal.state == validation_runner.VALIDATION_ENVIRONMENT_BLOCKED
        # Distinct from the failure state.
        assert terminal.state != validation_runner.VALIDATION_FAILED
        # Names the restriction...
        assert terminal.restriction == expected_restriction
        assert expected_restriction in terminal.detail
        # ...and is operationally recoverable rather than requiring a supersede.
        assert terminal.recoverable is True
        assert terminal.requires_supersede is False
        assert terminal.blocks_acceptance is True
        seen.add(terminal.restriction)

    # At least two genuinely distinct restrictions were exercised.
    assert len(seen) == 4


# ── TWO: validation_failed unchanged for a genuine gate failure ──────────────


def test_genuine_gate_failure_stays_validation_failed() -> None:
    row = _nonzero_row(
        "python -m pytest -q tests/test_x.py",
        returncode=1,
        stderr="E   assert 1 == 2\nE   AssertionError",
        argv=["python", "-m", "pytest", "-q", "tests/test_x.py"],
        failure_class="test_failure",
    )
    terminal = validation_runner.classify_validation_results([row])
    assert terminal.state == validation_runner.VALIDATION_FAILED
    assert terminal.restriction is None
    assert terminal.recoverable is False
    assert terminal.requires_supersede is True
    assert terminal.blocks_acceptance is True


def test_lint_failure_stays_validation_failed() -> None:
    row = _nonzero_row(
        "ruff check src",
        returncode=1,
        stderr="src/x.py:1:1: F401 imported but unused",
        argv=["ruff", "check", "src"],
        failure_class="lint_failure",
    )
    terminal = validation_runner.classify_validation_results([row])
    assert terminal.state == validation_runner.VALIDATION_FAILED


def test_environment_restriction_never_masks_a_real_failure_in_the_batch() -> None:
    # A batch with BOTH a refused spawn AND a genuine test failure must remain
    # validation_failed -- the recoverable state is claimed only when EVERY
    # failure is environmental, so a broken candidate is never let through.
    refused = _launch_row(
        "mypy src",
        launch_error="PermissionError",
        stderr="Operation not permitted",
        argv=["mypy", "src"],
    )
    genuine = _nonzero_row(
        "bash -c 'exit 1'",
        returncode=1,
        stderr="boom",
        argv=["bash", "-c", "exit 1"],
        failure_class="nonzero_exit",
    )
    terminal = validation_runner.classify_validation_results([refused, genuine])
    assert terminal.state == validation_runner.VALIDATION_FAILED
    assert terminal.requires_supersede is True


def test_passing_batch_is_not_blocked() -> None:
    row = {"command": "ruff check src", "returncode": 0, "timed_out": False}
    terminal = validation_runner.classify_validation_results([row])
    assert terminal.state == validation_runner.VALIDATION_PASSED
    assert terminal.blocks_acceptance is False


# ── FOUR: reject an unrunnable validation command at card creation (NF-267) ──


def test_full_repository_suite_command_is_flagged() -> None:
    assert validation_runner.sandbox_unrunnable_reason("python -m pytest") is not None
    assert validation_runner.sandbox_unrunnable_reason("pytest -q") is not None
    assert (
        validation_runner.RESTRICTION_FULL_REPOSITORY_SUITE
        in validation_runner.sandbox_unrunnable_reason("python3 -m pytest -q")
    )


def test_subprocess_spawning_pytest_command_is_flagged() -> None:
    reason = validation_runner.sandbox_unrunnable_reason(
        "python -m pytest -n auto tests/test_x.py"
    )
    assert reason is not None
    assert validation_runner.RESTRICTION_SUBPROCESS_PYTEST in reason
    assert (
        validation_runner.RESTRICTION_SUBPROCESS_PYTEST
        in validation_runner.sandbox_unrunnable_reason("pytest --forked tests/test_x.py")
    )


def test_targeted_and_non_pytest_commands_are_runnable() -> None:
    for command in (
        "python3 -m pytest -q tests/test_sandbox_validation_truth.py",
        "pytest tests/test_x.py::test_y",
        "ruff check src",
        "cd sub && python -m pytest tests/unit/test_x.py",
        "python3 tools/candidate_pytest.py tests/",
        "git diff --check",
    ):
        assert validation_runner.sandbox_unrunnable_reason(command) is None, command


def test_card_declaring_full_suite_is_rejected_at_creation() -> None:
    with pytest.raises(ValueError, match="full_repository_suite"):
        validation_runner.assert_card_validation_sandbox_runnable(
            {"validation": ["python -m pytest -q"]}
        )


def test_card_declaring_subprocess_pytest_is_rejected_at_creation() -> None:
    with pytest.raises(ValueError, match="subprocess_pytest"):
        validation_runner.assert_card_validation_sandbox_runnable(
            {"validation": ["python -m pytest -n 4 tests/test_x.py"]}
        )


def test_card_with_runnable_validation_is_accepted() -> None:
    # No raise: every command is sandbox-runnable.
    validation_runner.assert_card_validation_sandbox_runnable(
        {
            "validation": [
                "python3 -m pytest -q tests/test_sandbox_validation_truth.py",
                "ruff check src",
                "git diff --check",
            ]
        }
    )


def test_custom_error_type_is_raised() -> None:
    class _CardError(Exception):
        pass

    with pytest.raises(_CardError, match="validation_command_unrunnable_in_sandbox"):
        validation_runner.assert_card_validation_sandbox_runnable(
            {"validation": ["pytest"]}, error_type=_CardError
        )


# ── FOUR (rework): full-suite vs bounded-selection classification table ──────
# A bounded selection is NOT the full repository suite. Only a pytest run with
# no positional target AND no bounding selection flag is the full suite. This
# table pins every row the manager measured on the previous candidate -- plus
# the real card shapes written the same day -- in BOTH directions, so the guard
# can never silently re-classify a legitimate bounded selection as full-suite
# (which would reject those cards at creation).
@pytest.mark.parametrize(
    ("command", "is_full_suite"),
    [
        # --- measured directly on the predecessor worktree ---
        ("python3 -m pytest -q tests", False),  # bare directory is a target
        ("python3 -m pytest -q -k retention", False),  # -k bounds the run
        ("python3 -m pytest -q -k 'lifecycle or supervisor_loop'", False),
        ("python3 -m pytest -q tests/test_x.py", False),  # explicit path
        ("python3 -m pytest -q", True),  # no target, no selection -> full suite
        # --- the real cards written that day, all -k selections ---
        ("python3 -m pytest -k callback", False),
        ("python3 -m pytest -k 'lifecycle or supervisor_loop or auto_pickup'", False),
        ("python3 -m pytest -k 'adapter or preflight or workforce'", False),
        # --- every other bounding selection flag, incl. = forms ---
        ("python3 -m pytest -m slow", False),  # marker, not python's -m pytest
        ("python -m pytest -k=retention", False),
        ("python -m pytest -m=slow", False),
        ("python -m pytest --deselect tests/test_x.py::t", False),
        ("python -m pytest --deselect=tests/test_x.py::t", False),
        ("python -m pytest --lf", False),
        ("python -m pytest --ff", False),
        ("python -m pytest --last-failed", False),
        ("python -m pytest --failed-first", False),
        # --- more bare-directory / nested-directory targets ---
        ("pytest tests", False),
        ("pytest tests/unit", False),
        # --- genuine full-suite invocations, several spellings ---
        ("pytest", True),
        ("pytest -q", True),
        ("python -m pytest", True),
        ("python -m pytest -q -x", True),  # only non-selecting flags
        ("python3 -m pytest --tb=short -ra", True),
    ],
)
def test_full_suite_classification_table(command: str, is_full_suite: bool) -> None:
    reason = validation_runner.sandbox_unrunnable_reason(command)
    if is_full_suite:
        assert reason is not None, command
        assert validation_runner.RESTRICTION_FULL_REPOSITORY_SUITE in reason, command
    else:
        assert reason is None, command


def test_bounded_selection_cards_are_accepted_at_creation() -> None:
    # The four card shapes the guard would previously have refused at creation.
    validation_runner.assert_card_validation_sandbox_runnable(
        {
            "validation": [
                "python3 -m pytest -k callback",
                "python3 -m pytest -k retention",
                "python3 -m pytest -k 'lifecycle or supervisor_loop or auto_pickup'",
                "python3 -m pytest -k 'adapter or preflight or workforce'",
            ]
        }
    )


# ── code_quality: a full-suite pytest on EITHER side of '&&' is flagged ──────
def test_full_suite_pytest_before_and_after_the_ampersand_is_flagged() -> None:
    # Trailing pytest (the classic `cd ... && pytest` idiom): still flagged.
    reason = validation_runner.sandbox_unrunnable_reason("cd sub && python -m pytest")
    assert reason is not None
    assert validation_runner.RESTRICTION_FULL_REPOSITORY_SUITE in reason
    # Leading pytest, previously discarded because only the '&&' tail was kept:
    # a full-suite run must NOT slip through by putting a trailing command after
    # it.
    reason = validation_runner.sandbox_unrunnable_reason("python -m pytest && echo done")
    assert reason is not None
    assert validation_runner.RESTRICTION_FULL_REPOSITORY_SUITE in reason


def test_bounded_pytest_beside_another_command_across_the_ampersand_is_runnable() -> None:
    # A bounded pytest on either side of '&&' is not the full suite.
    assert (
        validation_runner.sandbox_unrunnable_reason(
            "python -m pytest tests/test_x.py && echo done"
        )
        is None
    )
    assert (
        validation_runner.sandbox_unrunnable_reason("echo start && pytest -k retention")
        is None
    )


# ── TWO (rework): the terminal state keys on STRUCTURAL signals, NEVER on the
# candidate's own output text ────────────────────────────────────────────────
# ``failure_class`` (worker_workspace._validation_failure_class) is derived
# partly from the candidate's stdout/stderr, so a genuine gate failure whose
# text merely CONTAINS "not found" or "permission denied" -- ordinary in real
# assertions -- must NOT be downgraded to the recoverable environment-blocked
# state (that would make validation_failed retryable, which the card forbids).
# The SAME text WITH a real STRUCTURAL signal -- a spawn-time ``launch_error``
# (the only evidence the command never reached its gate) -- IS environment-
# blocked. A process *return code* is NOT structural: commands run shell-free, so
# 126/127 is the invoked process's own exit status, which the candidate controls,
# and must stay validation_failed. Tabled in BOTH directions so the next change
# cannot silently re-key the terminal state on candidate-controlled signals.
@pytest.mark.parametrize(
    ("label", "row", "expected_state"),
    [
        # --- candidate output text ALONE must NOT downgrade a real failure ---
        (
            "pytest assertion literally says 'not found'",
            _nonzero_row(
                "python3 -m pytest -q tests/test_x.py",
                returncode=1,
                stderr="E   assert 'widget' not found in ['gadget']\nE   AssertionError",
                argv=["python3", "-m", "pytest", "-q", "tests/test_x.py"],
                # A misleading label: the OLD text heuristic would call this
                # executable_unavailable. It must be ignored now.
                failure_class="executable_unavailable",
            ),
            validation_runner.VALIDATION_FAILED,
        ),
        (
            "pytest KeyError message contains 'not found'",
            _nonzero_row(
                "python3 -m pytest -q tests/test_x.py",
                returncode=1,
                stderr="KeyError: \"config key 'timeout' not found\"",
                argv=["python3", "-m", "pytest", "-q", "tests/test_x.py"],
                failure_class="executable_unavailable",
            ),
            validation_runner.VALIDATION_FAILED,
        ),
        (
            "pytest asserts PermissionError text",
            _nonzero_row(
                "python3 -m pytest -q tests/test_x.py",
                returncode=1,
                stderr="E   PermissionError: [Errno 13] Permission denied: '/etc/shadow'",
                argv=["python3", "-m", "pytest", "-q", "tests/test_x.py"],
                failure_class="permission_denied",
            ),
            validation_runner.VALIDATION_FAILED,
        ),
        # --- the SAME text WITH a real STRUCTURAL signal IS environment-blocked ---
        (
            "spawn-time FileNotFoundError (structural launch_error)",
            _launch_row(
                "mypy src",
                launch_error="FileNotFoundError",
                stderr="[Errno 2] No such file or directory: 'mypy'",
                argv=["mypy", "src"],
            ),
            validation_runner.VALIDATION_ENVIRONMENT_BLOCKED,
        ),
        # --- a candidate-controlled returncode is NOT structural (rework fix) ---
        # These two asserted VALIDATION_ENVIRONMENT_BLOCKED before: the classifier
        # trusted rc 126/127 as a shell/loader signal. But commands run shell-free,
        # so those codes are the invoked process's own exit status; a pytest body
        # calling os._exit(127)/os._exit(126) could forge a recoverable verdict.
        # After the fix they stay VALIDATION_FAILED unless a spawn-time
        # launch_error proves the command never started.
        (
            "candidate process exits 127 (rc alone must not downgrade)",
            _nonzero_row(
                "python3 -m pytest -q tests/test_x.py",
                returncode=127,
                stderr="mypy: command not found",
                argv=["python3", "-m", "pytest", "-q", "tests/test_x.py"],
                failure_class="executable_unavailable",
            ),
            validation_runner.VALIDATION_FAILED,
        ),
        (
            "candidate test calls os._exit(126) (rc alone must not downgrade)",
            _nonzero_row(
                "python3 -m pytest -q tests/test_x.py",
                returncode=126,
                stderr="bash: ./run_checks.sh: Permission denied",
                argv=["python3", "-m", "pytest", "-q", "tests/test_x.py"],
                failure_class="permission_denied",
            ),
            validation_runner.VALIDATION_FAILED,
        ),
        (
            "spawn-time PermissionError (structural launch_error)",
            _launch_row(
                "bash -c 'exec true'",
                launch_error="PermissionError",
                stderr="seccomp denied clone(): Operation not permitted",
                argv=["bash", "-c", "exec true"],
            ),
            validation_runner.VALIDATION_ENVIRONMENT_BLOCKED,
        ),
    ],
)
def test_terminal_state_keys_on_structural_signal_not_candidate_text(
    label: str, row: dict, expected_state: str
) -> None:
    terminal = validation_runner.classify_validation_results([row])
    assert terminal.state == expected_state, label
    if expected_state == validation_runner.VALIDATION_FAILED:
        # A real gate failure stays acceptance-blocking and needs a supersede.
        assert terminal.requires_supersede is True, label
        assert terminal.recoverable is False, label
        assert terminal.restriction is None, label
    else:
        # A named, operationally recoverable restriction -- never a relabelled
        # real failure.
        assert terminal.requires_supersede is False, label
        assert terminal.recoverable is True, label
        assert terminal.restriction is not None, label


def test_candidate_controlled_returncode_never_downgrades_a_real_failure() -> None:
    # The exact trust-boundary defect this rework closes: a returncode is the
    # invoked process's own exit status, which the candidate controls. A pytest
    # body calling os._exit(126)/os._exit(127) -- with NO spawn-time launch_error
    # and NO runner import-probe proof -- must stay validation_failed, never the
    # recoverable, supersede-free environment-blocked state.
    for forged in (126, 127):
        row = _nonzero_row(
            "python3 -m pytest -q tests/test_evil.py",
            returncode=forged,
            stderr="(candidate output; the process merely exited with this code)",
            argv=["python3", "-m", "pytest", "-q", "tests/test_evil.py"],
            failure_class="nonzero_exit",
        )
        terminal = validation_runner.classify_validation_results([row])
        assert terminal.state == validation_runner.VALIDATION_FAILED, forged
        assert terminal.restriction is None, forged
        assert terminal.requires_supersede is True, forged
        assert terminal.recoverable is False, forged


# ── TWO (rework, HIGH): missing_package must be DECIDED by the runner's import
# probe, never by "No module named ..." text the candidate authors ────────────
# The candidate controls stdout/stderr but not the row's STRUCTURAL fields. A
# failing test whose captured output contains a ModuleNotFoundError for a
# validator (an import test, a vendored fixture, a deliberately-raised error, a
# traceback in an assertion message) must NOT become the recoverable, retryable
# validation_environment_blocked -- that is the trust boundary this card defends.


def test_missing_package_text_alone_does_not_downgrade_a_real_failure() -> None:
    # A genuine test failure whose output merely mentions a missing pytest, with
    # NO runner probe result attached: the runner never proved pytest absent, so
    # this stays validation_failed and requires a supersede.
    row = _nonzero_row(
        "python -m pytest -q tests/test_imports.py",
        returncode=1,
        stderr="E   ModuleNotFoundError: No module named 'pytest'\nE   AssertionError",
        argv=["python", "-m", "pytest", "-q", "tests/test_imports.py"],
        failure_class="test_failure",
        # absent_validator_modules deliberately omitted: the probe proved pytest
        # PRESENT, so the message is candidate text and must not decide anything.
    )
    terminal = validation_runner.classify_validation_results([row])
    assert terminal.state == validation_runner.VALIDATION_FAILED
    assert terminal.restriction is None
    assert terminal.requires_supersede is True


def test_missing_package_requires_the_runner_probe_proof() -> None:
    # The SAME text, but now the runner's import probe positively proved pytest
    # absent in the interpreter it invoked: only now is it environment-blocked.
    row = _nonzero_row(
        "python -m pytest -q tests/test_x.py",
        returncode=1,
        stderr="ModuleNotFoundError: No module named 'pytest'",
        argv=["python", "-m", "pytest", "-q", "tests/test_x.py"],
        failure_class="test_failure",
        absent_validator_modules=("pytest",),
    )
    terminal = validation_runner.classify_validation_results([row])
    assert terminal.state == validation_runner.VALIDATION_ENVIRONMENT_BLOCKED
    assert terminal.restriction == validation_runner.RESTRICTION_MISSING_PACKAGE
    assert terminal.requires_supersede is False


def test_probe_field_for_a_module_not_invoked_stays_validation_failed() -> None:
    # Defence in depth: even if the structural field named pytest, a command that
    # did not invoke pytest cannot be downgraded -- the decision is double-gated
    # on the invoked argv.
    row = _nonzero_row(
        "ruff check src",
        returncode=1,
        stderr="src/x.py:1:1: F401 imported but unused",
        argv=["ruff", "check", "src"],
        failure_class="lint_failure",
        absent_validator_modules=("pytest",),
    )
    terminal = validation_runner.classify_validation_results([row])
    assert terminal.state == validation_runner.VALIDATION_FAILED


def test_absent_non_validator_module_stays_validation_failed() -> None:
    # The explicit boundary: a NON-validator module going missing is the
    # candidate's OWN failure, so it stays validation_failed even if the probe
    # field named it.
    row = _nonzero_row(
        "python -m pytest -q tests/test_x.py",
        returncode=1,
        stderr="ModuleNotFoundError: No module named 'candidate_pkg'",
        argv=["python", "-m", "pytest", "-q", "tests/test_x.py"],
        failure_class="test_failure",
        absent_validator_modules=("candidate_pkg",),
    )
    terminal = validation_runner.classify_validation_results([row])
    assert terminal.state == validation_runner.VALIDATION_FAILED


def test_attached_xdist_short_form_spawns_and_is_flagged() -> None:
    # ``-n4`` (attached, no space) drives pytest-xdist exactly as ``-n 4`` does,
    # but the create-time preflight previously only recognised the spaced and
    # ``=`` forms, so a subprocess-spawning command slipped past card creation.
    for command in (
        "python3 -m pytest -n4 tests/test_x.py",
        "python3 -m pytest -nauto tests/test_x.py",
        "pytest -n2 tests/test_x.py",
    ):
        reason = validation_runner.sandbox_unrunnable_reason(command)
        assert reason is not None, command
        assert validation_runner.RESTRICTION_SUBPROCESS_PYTEST in reason, command
    # ``-n0`` disables distribution: not a spawn, and it carries a target, so
    # the command remains runnable.
    assert (
        validation_runner.sandbox_unrunnable_reason(
            "python3 -m pytest -n0 tests/test_x.py"
        )
        is None
    )


def test_attached_xdist_short_form_is_rejected_at_creation() -> None:
    with pytest.raises(ValueError, match="subprocess_pytest"):
        validation_runner.assert_card_validation_sandbox_runnable(
            {"validation": ["python3 -m pytest -n4 tests/test_x.py"]}
        )


# ── code_quality (7th round): ONE pytest argument model, decided in ONE place ─
# The create-time preflight (``sandbox_unrunnable_reason``) and the worker's
# ``-m`` validator-import probe (``dash_m_validator_modules``) now share a single
# pytest parser in validation_runner. This table pins BOTH of its answers on the
# shapes the reviewer called out, so the two can never drift apart again:
#   * the two meanings of ``-m`` -- python's module selector vs pytest's marker;
#   * the concatenated short forms ``-kfoo`` / ``-mslow`` (finding TWO, the
#     false-positive: previously misread as the full suite);
#   * an option the OLD allowlist did not know consumes its value, so its value
#     leaked through as a positional and let the full suite escape (finding ONE,
#     the false-negative);
#   * the bare full-suite form.
# ``validator_modules`` is what the SAME parser infers as python-``-m``-invoked,
# which the worker probe imports to prove ``missing_package``.
@pytest.mark.parametrize(
    ("command", "is_full_suite", "validator_modules"),
    [
        # pytest's ``-m`` is a MARKER selection, never python's module selector,
        # and a marker bounds the run. No python ``-m`` module -> nothing to probe
        # (finding THREE: ``-m coverage`` must NOT look for a module ``coverage``).
        ("pytest -m slow", False, ()),
        ("pytest -m coverage", False, ()),
        # concatenated short forms bound the run (finding TWO): ``-kfoo`` == ``-k
        # foo``, ``-mslow`` == ``-m slow``.
        ("pytest -kfoo", False, ()),
        ("pytest -mslow", False, ()),
        # python's ``-m pytest`` selects the pytest MODULE; the positional bounds
        # the run and pytest is the inferred validator module.
        ("python -m pytest tests/x.py", False, ("pytest",)),
        # finding ONE: options the OLD allowlist did not model consume their
        # value, so the value is NOT a positional -> these are the full suite.
        ("python -m pytest --durations 3", True, ("pytest",)),
        ("python -m pytest --junitxml report.xml", True, ("pytest",)),
        # the bare full-suite forms.
        ("pytest", True, ()),
        ("python -m pytest", True, ("pytest",)),
    ],
)
def test_one_pytest_argument_model_classifies_and_infers(
    command: str, is_full_suite: bool, validator_modules: tuple[str, ...]
) -> None:
    reason = validation_runner.sandbox_unrunnable_reason(command)
    if is_full_suite:
        assert reason is not None, command
        assert validation_runner.RESTRICTION_FULL_REPOSITORY_SUITE in reason, command
    else:
        assert reason is None, command
    # The SAME parser answers "which -m is a python module" for the worker probe.
    assert (
        validation_runner.dash_m_validator_modules(shlex.split(command))
        == validator_modules
    ), command


def test_pytest_marker_value_is_never_a_python_module() -> None:
    # finding THREE at the exact PRODUCTION input: run_validations normalizes
    # ``pytest -m coverage`` to ``python -m pytest -m coverage``. python selects
    # the pytest MODULE; the ``-m coverage`` marker is pytest's, so ``coverage``
    # is never probed as an importable module -- only ``pytest`` is inferred.
    assert validation_runner.dash_m_validator_modules(
        ["python", "-m", "pytest", "-m", "coverage"]
    ) == ("pytest",)
    assert validation_runner.dash_m_validator_modules(
        ["python3", "-m", "pytest", "-m", "mypy", "tests/test_x.py"]
    ) == ("pytest",)
    # a direct pytest console script has no python ``-m`` module at all.
    assert validation_runner.dash_m_validator_modules(["pytest", "-m", "slow"]) == ()
    # python ``-m <validator>`` genuinely selects that validator module, in both
    # the spaced and attached spellings.
    assert validation_runner.dash_m_validator_modules(
        ["python3", "-m", "ruff", "check", "src"]
    ) == ("ruff",)
    assert validation_runner.dash_m_validator_modules(
        ["python3", "-mmypy", "src"]
    ) == ("mypy",)
    # coverage wrapping pytest: python selects coverage (the interpreter's -m).
    assert validation_runner.dash_m_validator_modules(
        ["python3", "-m", "coverage", "run", "-m", "pytest"]
    ) == ("coverage",)
    # a non-validator python module is never inferred.
    assert validation_runner.dash_m_validator_modules(["python3", "-m", "build"]) == ()
