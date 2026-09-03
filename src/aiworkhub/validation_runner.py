"""Terminal-state truth for worker-sandbox validation (NF-WAVE-SANDBOX-TRUTH).

A worker's own validation can be wrong in two directions that ``validation_failed``
alone cannot express:

* the candidate genuinely failed its gate (a real test/lint/type failure), or
* the command could not run *here at all* -- a forbidden spawn, a refused chmod,
  an absent interpreter, or a missing validator package.

``validation_failed`` is non-operational: ``accept_review``, ``mark_done`` and
``retry_terminal`` all refuse it, so a card that reaches it for an environmental
reason can only be closed by archiving and reissuing (a supersede). This module
separates the two cases so the second becomes a distinct, operationally
recoverable terminal state that *names* the restriction, while the first keeps
its exact, acceptance-blocking meaning.

It is deliberately dependency-light (stdlib only) and never imports
``worker_workspace``: the import edge runs one way (``worker_workspace`` imports
this), so classification stays reusable and unit-testable without a sandbox.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# ---- terminal states --------------------------------------------------------
# ``validation_failed`` is unchanged: the candidate failed its gate and must not
# be accepted. It is retained here as the single source of truth for the string
# so callers never drift from core.py's spelling.
VALIDATION_FAILED = "validation_failed"
# NF-2026-00271 / NF-2026-00298: a NEW, additional terminal state -- never a
# relabelling of a real failure. The command could not run in this sandbox. It
# still blocks acceptance (an unvalidated candidate is not acceptable) but is
# recoverable by re-running in a corrected environment, so it does NOT require a
# supersede the way ``validation_failed`` does.
VALIDATION_ENVIRONMENT_BLOCKED = "validation_environment_blocked"
VALIDATION_PASSED = "validation_passed"

# ---- restriction taxonomy (the names a blocked state must carry) ------------
RESTRICTION_FORBIDDEN_SPAWN = "forbidden_spawn"
RESTRICTION_REFUSED_CHMOD = "refused_chmod"
RESTRICTION_ABSENT_INTERPRETER = "absent_interpreter"
RESTRICTION_MISSING_PACKAGE = "missing_package"
RESTRICTION_METADATA_BROKER_DENIAL = "metadata_broker_denial"
# create-time-only restrictions (a card that declares such a command can never
# succeed inside a worker sandbox and is rejected before a worker spends tokens)
RESTRICTION_FULL_REPOSITORY_SUITE = "full_repository_suite"
RESTRICTION_SUBPROCESS_PYTEST = "subprocess_pytest"

# ``worker_workspace._validation_failure_class`` is deliberately NOT consulted
# here. That heuristic derives its label partly from the candidate's own
# stdout/stderr ("permission denied", "not found"), which the candidate
# controls and which a genuine gate failure can legitimately contain -- an
# ``assert 'x' not found in ...``, a ``KeyError`` message, a test that asserts
# ``FileNotFoundError``/``PermissionError``. Letting that text pick the terminal
# state would let a real gate failure downgrade itself to the recoverable
# environment-blocked state, which is exactly the weakening of
# ``validation_failed`` this card forbids. ``row_restriction`` keys on
# STRUCTURAL signals only (see its docstring).

# Only a *validator* package going missing is an environment restriction; a
# module the candidate itself was supposed to provide is a genuine failure.
#
# Explicit boundary (raised on review): ``missing_package`` is INTENTIONALLY
# restricted to the validator toolchain the runner itself invokes -- pytest,
# ruff, mypy, coverage. This is not an accident of a hardcoded list waiting to be
# widened: it is the trust boundary. A NON-validator module being absent (a
# module the candidate was supposed to ship, a dependency its own tests import)
# is by definition the candidate's OWN failure, so it must stay
# ``validation_failed`` and require a supersede -- widening this set to "any
# absent module" would relabel real gate failures as recoverable, the one thing
# this card forbids. An absent validator outside this set therefore stays
# ``validation_failed`` on purpose, not by oversight.
_KNOWN_VALIDATOR_MODULES = frozenset({"pytest", "ruff", "mypy", "coverage"})

_CHMOD_TOKENS = (
    "chmod",
    "fchmod",
    "fchmodat",
    "chown",
    "fchown",
    "lchown",
    "setuid",
    "setgid",
    "suid",
    "sgid",
    "setxattr",
    "utimensat",
)
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class TerminalState:
    """The terminal disposition of one validation batch.

    ``requires_supersede`` is the operational distinction between the two
    terminal states: ``validation_failed`` requires archiving+reissuing to
    clear, while the environment-blocked state clears by re-running in a
    corrected environment.
    """

    state: str
    recoverable: bool
    requires_supersede: bool
    blocks_acceptance: bool
    restriction: str | None = None
    restrictions: tuple[str, ...] = ()
    command: str | None = None
    detail: str = ""


def _row_diagnostic(row: Mapping[str, Any]) -> str:
    return (
        str(row.get("stderr_tail") or "")
        + "\n"
        + str(row.get("stdout_tail") or "")
        + "\n"
        + str(row.get("stderr_head") or "")
        + "\n"
        + str(row.get("launch_error_message") or "")
    ).lower()


def _invoked_validator_modules(row: Mapping[str, Any]) -> set[str]:
    argv = row.get("executed_argv") or row.get("argv") or []
    if not isinstance(argv, (list, tuple)):
        argv = []
    joined = " ".join(str(token) for token in argv).lower()
    return {name for name in _KNOWN_VALIDATOR_MODULES if name in joined}


def _row_absent_validator_modules(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Validator modules the RUNNER proved genuinely absent, from a structural
    field it authored -- never from the candidate's output text.

    ``run_validations`` writes ``absent_validator_modules`` only after importing
    each ``-m``-invoked validator module in the SAME interpreter it ran the
    command with (``worker_workspace._probe_absent_validator_modules``): a signal
    the candidate cannot forge, since it controls stdout/stderr/exit code but not
    the row's structural fields. Double-gated here on the invoked argv and the
    known-validator set so a candidate-owned or non-validator module can never
    reach ``missing_package`` even if the field were somehow populated.
    """
    raw = row.get("absent_validator_modules")
    if not isinstance(raw, (list, tuple, set)):
        return ()
    invoked = _invoked_validator_modules(row)
    return tuple(
        name
        for name in raw
        if isinstance(name, str)
        and name in _KNOWN_VALIDATOR_MODULES
        and name in invoked
    )


def row_restriction(row: Mapping[str, Any]) -> str | None:
    """Name the sandbox restriction a *failing* row reflects, else ``None``.

    ``None`` means "this is a genuine gate failure", which keeps
    ``validation_failed`` intact.  Never call this on a passing row.

    The determination keys on STRUCTURAL signals only -- never on text found in
    the candidate's own stdout/stderr. That text is candidate-controlled and a
    real gate failure can legitimately contain "not found" / "permission
    denied" (``assert 'x' not found in ...``, a ``KeyError``, a test asserting
    ``FileNotFoundError``). Deriving the terminal state from it would let a real
    failure downgrade itself to the recoverable environment-blocked state,
    weakening ``validation_failed`` -- the one thing this card forbids. The
    three structural signals, none of which the candidate can forge from test
    output:

    * ``launch_error`` -- the exception TYPE the runner captured while *starting*
      the command (``PermissionError`` / ``FileNotFoundError`` / any other
      ``OSError``). Recorded at spawn time, never parsed from output. This is the
      only spawn-time signal, and the only evidence that the command never
      reached its gate.
    * a known *validator* module the RUNNER proved genuinely absent by importing
      it in the same interpreter it invoked (``row["absent_validator_modules"]``,
      written by ``run_validations``' import probe). Absence is DECIDED by that
      probe, never by matching "No module named ..." in the candidate's output --
      a failing test whose captured text merely contains that phrase (an import
      test, a vendored fixture, a deliberately-raised error, a traceback in an
      assertion message) stays ``validation_failed``.

    A process *return code* is deliberately NOT a structural signal. Commands run
    shell-free (``run_validations`` spawns argv with ``shell=False``), so there is
    no shell/loader to emit the classic 126 / 127 "not executable" / "command not
    found" codes on the runner's behalf; a returncode of 126 or 127 here is simply
    the invoked process's own exit status, which the candidate controls -- a test
    body that calls ``os._exit(126)`` would otherwise forge an environment-blocked
    verdict for a real gate failure (the trust-boundary defect this keys around).
    A genuine spawn/exec failure never yields a returncode at all: it raises at
    spawn time and surfaces as ``launch_error`` above.

    The one text test below (the chmod-vs-spawn token scan) only ever REFINES an
    already-established structural signal (a ``PermissionError`` at spawn); it
    never establishes a restriction on its own.
    """

    # The validation launcher receives these records over a private inherited
    # pipe which is deliberately not inherited by the validator.  Require the
    # complete structural envelope; candidate-authored lookalikes in output or
    # arbitrary row fields have no authority.
    denials = row.get("metadata_broker_denials")
    if row.get("metadata_broker_denial_attributed") is True and isinstance(
        denials, (list, tuple)
    ) and denials and all(
        isinstance(item, Mapping)
        and item.get("schema") == "aiworkhub.metadata_broker_denial.v1"
        and item.get("authenticated") is True
        and item.get("terminal") is True
        and isinstance(item.get("reason"), str)
        and isinstance(item.get("syscall_nr"), int)
        for item in denials
    ):
        return RESTRICTION_METADATA_BROKER_DENIAL

    diagnostic = _row_diagnostic(row)
    launch_error = row.get("launch_error")

    # Structural: a real exception raised while *starting* the command.
    if launch_error == "PermissionError":
        # Distinguish a refused chmod (setuid/mode) from a forbidden spawn so
        # the recovered card names the exact restriction. The token scan only
        # refines an already-structural PermissionError.
        if any(token in diagnostic for token in _CHMOD_TOKENS):
            return RESTRICTION_REFUSED_CHMOD
        return RESTRICTION_FORBIDDEN_SPAWN
    if launch_error == "FileNotFoundError":
        return RESTRICTION_ABSENT_INTERPRETER
    if isinstance(launch_error, str) and launch_error:
        # Any other exception type captured at spawn time (a bare ``OSError``,
        # the old ``launch_failed`` case): the command never reached its gate.
        return RESTRICTION_FORBIDDEN_SPAWN

    # Structural: a missing *validator* package the runner proved genuinely
    # absent by importing it in the SAME interpreter it invoked. Decided from the
    # runner-authored ``absent_validator_modules`` field, NEVER from candidate
    # output text -- so "No module named 'pytest'" appearing in a failing test's
    # captured stdout cannot downgrade a real gate failure to the recoverable
    # state.
    if _row_absent_validator_modules(row):
        return RESTRICTION_MISSING_PACKAGE

    return None


# ---- exec-scratch provisioning restriction (NF-2026-00458) -----------------
# ``provision_validation_exec_scratch`` (worker_workspace.py) fails closed with
# one aggregate ``tried`` detail describing every candidate root's rejection
# reason. This is the single source of truth for reading that detail: when
# every candidate was rejected because the exact metadata syscalls git's own
# ``config.lock`` chmod/utime needs are themselves denied by the OUTER sandbox
# hosting the worker process -- ``no_metadata`` from
# ``_probe_metadata_capable_dir``, or an EPERM/"Permission denied" ``OSError``
# on the candidate directory's own ``mkdir`` -- this is infrastructure
# evidence, never a candidate gate failure. worker_workspace delegates to this
# function instead of re-deriving the same token list, so the two modules
# cannot drift on what counts as a refused chmod.
def exec_scratch_denied_restriction(detail: str) -> str | None:
    """Classify a scratch-provisioning rejection ``detail``, else ``None``.

    Returns ``None`` for any other rejection reason (a genuinely noexec-only
    root, a missing directory, ...) so the caller re-raises its original error
    unchanged rather than mis-classifying an unrelated provisioning failure.
    """
    prefix = "validation_exec_scratch_unavailable:"
    lowered = detail.strip().lower()
    if not lowered.startswith(prefix):
        return None
    candidates = [row.strip() for row in lowered[len(prefix) :].split(";") if row.strip()]
    if not candidates:
        return None

    def _metadata_denied(row: str) -> bool:
        if row.endswith(":no_metadata"):
            return True
        return ":mkdir_failed:" in row and (
            "eperm" in row or "permission denied" in row
        )

    denied = all(_metadata_denied(row) for row in candidates)
    return RESTRICTION_REFUSED_CHMOD if denied else None


def _is_failing_row(row: Mapping[str, Any]) -> bool:
    # Mirror ``run_validations``: a row failed if it timed out or exited nonzero
    # (launch/timeout rows carry returncode ``None``).
    return bool(row.get("timed_out")) or row.get("returncode") != 0


def classify_validation_results(results: Iterable[Mapping[str, Any]]) -> TerminalState:
    """Map a validation batch to its terminal disposition.

    Environment-blocked is claimed ONLY when *every* failing row is an
    environment restriction. If any failing row is a genuine gate failure the
    batch is ``validation_failed`` -- a candidate that truly failed its gate is
    never let through as "merely blocked", so ``validation_failed`` keeps its
    exact meaning (NF forbidden: do not make it weaker).
    """

    rows = list(results)
    failing = [row for row in rows if _is_failing_row(row)]
    if not failing:
        return TerminalState(
            state=VALIDATION_PASSED,
            recoverable=True,
            requires_supersede=False,
            blocks_acceptance=False,
        )

    restrictions: list[str] = []
    all_environment = True
    for row in failing:
        restriction = row_restriction(row)
        if restriction is None:
            all_environment = False
        else:
            restrictions.append(restriction)

    first = failing[0]
    command = None
    raw_command = first.get("command")
    if isinstance(raw_command, str):
        command = raw_command

    if all_environment and restrictions:
        ordered_unique = tuple(dict.fromkeys(restrictions))
        primary = row_restriction(first) or ordered_unique[0]
        return TerminalState(
            state=VALIDATION_ENVIRONMENT_BLOCKED,
            recoverable=True,
            requires_supersede=False,
            blocks_acceptance=True,
            restriction=primary,
            restrictions=ordered_unique,
            command=command,
            detail=(
                "validation could not run in this sandbox: "
                + ", ".join(ordered_unique)
            ),
        )

    return TerminalState(
        state=VALIDATION_FAILED,
        recoverable=False,
        requires_supersede=True,
        blocks_acceptance=True,
        restriction=None,
        restrictions=(),
        command=command,
        detail="candidate failed its validation gate",
    )


# ---- one pytest argument model (NF-2026-00267 + rework) ---------------------
# ONE place decides what pytest's command line means, shared by the create-time
# preflight (``sandbox_unrunnable_reason``) AND the worker's ``-m`` validator-
# import probe (``worker_workspace._probe_absent_validator_modules``, via
# ``dash_m_validator_modules`` below). The two once disagreed about what ``-m``
# means (python's module selector vs pytest's marker flag) and about how short
# options attach their values (``-kfoo``, ``-mslow``, ``-n4``); both are now
# resolved here, once, so a full-suite run cannot escape and a bounded run
# cannot be misread as full-suite.

# python interpreter basenames: python / python3 / python3.11 / ...
_PYTHON_INTERPRETER_RE = re.compile(r"python[0-9.]*$")
# python interpreter options that consume the following token as their value, so
# a later ``-m`` is not mistaken for one hidden behind them.
_PYTHON_INTERP_VALUE_FLAGS = frozenset({"-W", "-X", "--check-hash-based-pycs"})

# pytest SHORT options that consume a value, spelled ``-k EXPR`` or attached
# ``-kEXPR``. ``-n`` is xdist's process count.
_PYTEST_SHORT_VALUE_OPTIONS = frozenset(
    {"-k", "-m", "-p", "-o", "-c", "-W", "-r", "-n"}
)
# pytest LONG options that consume the following token as a value (``--opt val``;
# the ``--opt=val`` form carries its own value). Kept deliberately broad: a value
# option NOT modelled here makes its value look like a positional test target,
# which lets a full-suite run escape the preflight -- the false-negative this set
# closes (an unknown-to-the-old-parser value option was exactly finding ONE).
_PYTEST_LONG_VALUE_OPTIONS = frozenset(
    {
        "--deselect",
        "--ignore",
        "--ignore-glob",
        "--rootdir",
        "--confcutdir",
        "--basetemp",
        "--maxfail",
        "--numprocesses",
        "--durations",
        "--durations-min",
        "--junitxml",
        "--junit-xml",
        "--junit-prefix",
        "--result-log",
        "--override-ini",
        "--assert",
        "--tb",
        "--capture",
        "--import-mode",
        "--color",
        "--code-highlight",
        "--log-level",
        "--log-file",
        "--log-file-level",
        "--log-cli-level",
        "--log-format",
        "--log-date-format",
        "--pdbcls",
        "--dist",
        "--tx",
        "--max-worker-restart",
    }
)
# Selection flags that BOUND the set of tests pytest runs, so a command carrying
# one is a bounded selection -- never the full repository suite -- even with no
# positional path. ``-k``/``-m``/``--deselect`` take a value; ``--lf``/``--ff``
# (and their long forms) take none. ``-m`` here is pytest's marker flag; python's
# ``-m pytest`` is peeled off by ``_pytest_args`` first, so the two never collide.
_PYTEST_SELECTION_SHORT = frozenset({"-k", "-m"})
_PYTEST_SELECTION_LONG = frozenset(
    {"--deselect", "--lf", "--last-failed", "--ff", "--failed-first"}
)
# pytest options that spawn worker subprocesses the sandbox forbids.
_PYTEST_SPAWN_FLAGS = frozenset({"--forked", "--boxed"})
_PYTEST_XDIST_SHORT = "-n"
_PYTEST_XDIST_LONG = frozenset({"--numprocesses"})


def _iter_pytest_args(args):
    """Yield ``(kind, name, value)`` for each of pytest's own args, resolving
    value consumption exactly ONCE so the selection check and the positional
    check can never disagree about what is an option value and what is a test
    target. ``kind`` is ``"option"`` or ``"positional"``; ``name`` is the
    canonical option (``-k``, ``--deselect``) or the positional token; ``value``
    is the consumed value when one applies.
    """

    index = 0
    total = len(args)
    while index < total:
        token = args[index]
        if token.startswith("--"):
            name, sep, attached = token.partition("=")
            if sep:
                yield ("option", name, attached)
                index += 1
            elif name in _PYTEST_LONG_VALUE_OPTIONS and index + 1 < total:
                yield ("option", name, args[index + 1])
                index += 2
            else:
                yield ("option", name, None)
                index += 1
        elif token.startswith("-") and token != "-":
            short = token[:2]
            if short in _PYTEST_SHORT_VALUE_OPTIONS:
                if len(token) > 2:  # attached: -kEXPR / -mslow / -n4
                    yield ("option", short, token[2:])
                    index += 1
                elif index + 1 < total:  # spaced: -k EXPR
                    yield ("option", short, args[index + 1])
                    index += 2
                else:
                    yield ("option", short, None)
                    index += 1
            else:  # value-less short flag, possibly bundled (-xvs)
                yield ("option", token, None)
                index += 1
        else:
            yield ("positional", token, None)
            index += 1


def _module_basename(module: str) -> str:
    return module.rsplit("/", 1)[-1].split(".")[0]


def _python_module_selection(tokens: list[str]) -> tuple[str | None, list[str]]:
    """If ``tokens`` invoke a python interpreter's ``-m <module>`` selector,
    return ``(module, args-after-the-module)``; otherwise ``(None, [])``.

    ``-m`` is python's module selector ONLY as an argument to the interpreter --
    before the selected module begins running. Once the module runs, its own
    ``-m`` (pytest's marker flag) is NOT a python module. Discriminated by
    position: the first ``-m`` seen while still parsing the interpreter's own
    options. A command whose program is not a python interpreter (a bare
    ``pytest`` console script, ``ruff``, ``git``) has no python ``-m`` at all.
    """

    if not tokens:
        return None, []
    head = tokens[0].rsplit("/", 1)[-1]
    if not _PYTHON_INTERPRETER_RE.match(head):
        return None, []
    index = 1
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if token == "-m" and index + 1 < total:
            return tokens[index + 1], tokens[index + 2 :]
        if token.startswith("-m") and not token.startswith("--") and len(token) > 2:
            return token[2:], tokens[index + 1 :]
        if token == "-c":
            return None, []  # runs a command string, never a module
        if token in _PYTHON_INTERP_VALUE_FLAGS and index + 1 < total:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return None, []  # a script path ends the interpreter-option phase
    return None, []


def dash_m_validator_modules(tokens: list[str]) -> tuple[str, ...]:
    """Known validator modules invoked via a python interpreter's ``-m <module>``.

    This is the single recognition authority shared by execution classification
    and launch-time capability preflight. pytest's own ``-m <marker>`` (including
    ``pytest -m coverage``) is not python's module selector and is therefore never
    probed as an importable validator module.
    """

    module, _ = _python_module_selection(list(tokens))
    if module is None:
        return ()
    top = _module_basename(module)
    return (top,) if top in _KNOWN_VALIDATOR_MODULES else ()


def _command_segments(command: str) -> list[list[str]]:
    """Lex ``command`` into shell-chain argv segments without executing it.

    ``punctuation_chars`` makes adjacent operators such as ``pytest;echo`` and
    ``cd x||pytest`` visible as standalone tokens. Quoted operator-like values
    remain ordinary arguments, so ``pytest -k 'a || b' tests/x.py`` is not
    split. Every operator run is only a boundary; this parser never invokes a
    shell or interprets the command.
    """

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.commenters = ""
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and set(token) <= {";", "&", "|"}:
            segments.append(current)
            current = []
            continue
        current.append(token)
    segments.append(current)
    stripped: list[list[str]] = []
    for segment in segments:
        index = 0
        while index < len(segment) and _ENV_ASSIGN_RE.match(segment[index]):
            index += 1
        stripped.append(segment[index:])
    return stripped


def _pytest_args(tokens: list[str]) -> list[str] | None:
    """Return the arguments passed to pytest, or ``None`` when ``tokens`` is not
    a pytest invocation.

    Two spellings: ``python -m pytest <args>`` (python's ``-m`` selects the
    pytest module -- see ``_python_module_selection``) and a direct
    ``pytest`` / ``.../pytest`` console script. Any other ``python -m <module>``
    is not pytest. Returning the args -- not just a bool -- lets the caller
    reason about pytest's own ``-m`` (marker) without confusing it with python's
    ``-m`` (module), because that split happened here.
    """

    module, rest = _python_module_selection(tokens)
    if module is not None:
        return rest if _module_basename(module) == "pytest" else None
    if tokens and tokens[0].rsplit("/", 1)[-1] == "pytest":
        return list(tokens[1:])
    return None


def _pytest_has_selection(args: list[str]) -> bool:
    """True when pytest's own args carry a selection flag that bounds the run."""

    return any(
        kind == "option"
        and (name in _PYTEST_SELECTION_SHORT or name in _PYTEST_SELECTION_LONG)
        for kind, name, _value in _iter_pytest_args(args)
    )


def _pytest_has_test_target(args: list[str]) -> bool:
    """True when pytest's own args carry a positional test target.

    A pytest positional is a ``file_or_dir`` by definition, so *any* positional
    -- a nodeid, a ``.py`` file, or a bare directory such as ``tests`` -- bounds
    the run to that path. ``_iter_pytest_args`` already peeled off every option's
    value, so a value that merely LOOKS like a path (``--junitxml report.xml``,
    ``--durations 3``) is never mistaken for a target. Recognising this lexically
    keeps it filesystem-free: no probe of the repository is needed (and none is
    wanted -- probing would reintroduce the environment coupling this module
    exists to remove, and would be wrong at card-creation time when the tree may
    not be checked out yet).
    """

    return any(kind == "positional" for kind, _name, _value in _iter_pytest_args(args))


def _pytest_spawns_subprocess(args: list[str]) -> bool:
    for kind, name, value in _iter_pytest_args(args):
        if kind != "option":
            continue
        if name in _PYTEST_SPAWN_FLAGS:
            return True
        if name == _PYTEST_XDIST_SHORT or name in _PYTEST_XDIST_LONG:
            # ``-n``/``--numprocesses`` process count, in any spelling
            # (``-n 4``, ``-n4``, ``-nauto``, ``--numprocesses=4``). ``-n 0``
            # disables distribution, so it is not a spawn.
            if (value or "").strip() not in ("", "0"):
                return True
    return False


def _segment_unrunnable_reason(args: list[str]) -> str | None:
    """Named reason one chained segment cannot run in the sandbox, else ``None``."""

    pytest_args = _pytest_args(args)
    if pytest_args is None:
        return None
    if _pytest_spawns_subprocess(pytest_args):
        return RESTRICTION_SUBPROCESS_PYTEST
    if _pytest_has_selection(pytest_args) or _pytest_has_test_target(pytest_args):
        return None
    return RESTRICTION_FULL_REPOSITORY_SUITE


def sandbox_unrunnable_reason(command: str) -> str | None:
    """Return a named reason a declared command can never run in a worker
    sandbox, or ``None`` when it can.

    Two provable cases, both about pytest because that is what cards declare:

    * ``full_repository_suite`` -- a pytest invocation with NO positional test
      target AND no bounding selection flag runs the entire repository, which
      the manager runs OUTSIDE the sandbox on purpose.
    * ``subprocess_pytest`` -- ``--forked`` / ``--boxed`` / xdist ``-n`` spawn
      pytest worker subprocesses the sandbox forbids.

    Every shell-chain segment is inspected, so a full-suite pytest call cannot
    hide beside ``&&``, ``||``, ``;``, or an adjacent operator spelling.
    Conservative by design: a command that bounds its run -- a concrete path, a
    bare directory, or a ``-k``/``-m``/``--deselect``/``--lf``/``--ff``
    selection -- is never flagged, so genuinely runnable cards are never
    rejected.
    """

    for segment in _command_segments(str(command)):
        restriction = _segment_unrunnable_reason(segment)
        if restriction is not None:
            return f"{restriction}:{str(command).strip()[:200]}"
    return None


def assert_card_validation_sandbox_runnable(
    card: Mapping[str, Any], *, error_type: type[Exception] = ValueError
) -> None:
    """Reject a card whose declared validation cannot run in a worker sandbox.

    Intended to be called at card *creation* (the earliest point), so the
    contract is explicit up front instead of surfacing as a terminal failure
    twenty minutes into a worker run. ``error_type`` lets the caller raise its
    own domain exception (core.py raises its card-validation error).
    """

    commands = card.get("validation") or []
    if not isinstance(commands, (list, tuple)):
        raise error_type("validation_declaration_invalid")
    for command in commands:
        reason = sandbox_unrunnable_reason(str(command))
        if reason is not None:
            raise error_type(f"validation_command_unrunnable_in_sandbox:{reason}")


__all__ = [
    "RESTRICTION_ABSENT_INTERPRETER",
    "RESTRICTION_FORBIDDEN_SPAWN",
    "RESTRICTION_FULL_REPOSITORY_SUITE",
    "RESTRICTION_MISSING_PACKAGE",
    "RESTRICTION_REFUSED_CHMOD",
    "RESTRICTION_SUBPROCESS_PYTEST",
    "TerminalState",
    "VALIDATION_ENVIRONMENT_BLOCKED",
    "VALIDATION_FAILED",
    "VALIDATION_PASSED",
    "assert_card_validation_sandbox_runnable",
    "classify_validation_results",
    "dash_m_validator_modules",
    "exec_scratch_denied_restriction",
    "row_restriction",
    "sandbox_unrunnable_reason",
]
