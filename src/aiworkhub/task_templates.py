"""Pure deterministic registry of classified AIWorkHub task templates.

This module is additive and self-contained: it never imports or mutates
``core.py``, ``task_store.py`` or any other lifecycle module.  The output of
``expand_template`` is plain data for the existing authoritative
``create_task`` card, including one-to-one ``validation_roles``.

Determinism contract:

* Seven stable template IDs, each bound to one exact frozen definition.
* A full template ID is ``{name}@v{N}:{digest}`` where ``digest`` is the
  SHA-256 hex digest of the canonical definition JSON at registry version
  ``N``. Current full IDs are accepted, along with authenticated legacy-v1
  full IDs whose complete provenance and expanded payload still match exactly;
  stale or forged identities fail closed with a stable reason token.
* Expansion is a pure function of (template ID, explicit bounded paths):
  identical inputs always produce an identical payload.

Path contract (fail-closed, never coerced):

* Path entries must be actual ``str`` instances.
* Whitespace, control characters, backslashes, ``~``, glob characters,
  absolute paths, ``.``/``..`` components, leading-hyphen tokens,
  duplicates and any character outside the safe POSIX token set are
  rejected with stable reasons before any command is generated, so every
  generated validation command preserves exact, deterministic argv
  tokenization (single-space split, no quoting).

Scope vs. mandatory-change contract:

* ``production_paths``/``test_paths`` (and the ``allowed_writes``/``write_set``
  they expand into) are authenticated read/write *scope*: paths a worker is
  authorized to touch. They are never an implicit assertion that every one
  of them must change.
* ``required_outputs`` -- the set a downstream finalizer treats as
  ``required_output_unchanged``-eligible -- defaults to the complete write
  scope for ``bugfix_with_regression``, to the exact declared test paths
  for ``test_only`` (so its cards stay launch-valid), and to empty for
  every other template.
  An explicit ``mandatory_changed_outputs`` list overrides either default;
  every listed path must already be in scope or expansion fails closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

# skill_registry is a pure, standalone, side-effect-free module (no filesystem,
# store, or lifecycle import), so this keeps the self-contained contract above.
from . import skill_registry

__all__ = [
    "AUDITED_CUSTOM_ESCAPE",
    "CANONICAL_MINIMALITY_CONTRACT",
    "CANONICAL_VALIDATION_PYTHON",
    "COMMAND_NODE",
    "COMMAND_PYTHON",
    "CONTRACT_PATCH_RELATIVE_DIR",
    "CONTRACT_PATCH_SCHEMA_ID",
    "DIFF_CHECK_COMMAND",
    "MAX_OBJECTIVE_LENGTH",
    "MAX_PATH_LENGTH",
    "MAX_PATHS_PER_FIELD",
    "MAX_TITLE_LENGTH",
    "build_contract_patch",
    "canonical_validation_command",
    "contract_patch_digest",
    "load_contract_patch",
    "record_contract_patch",
    "validation_command_head_warnings",
    "PROVENANCE_SCHEMA_ID",
    "REGISTRY_VERSION",
    "SCHEMA_ID",
    "TEMPLATE_IDS",
    "TEMPLATE_SPECS",
    "TaskTemplateError",
    "TaskTemplateSpec",
    "VALIDATION_EXEMPTIONS",
    "VALIDATION_EXEMPTION_READ_ONLY",
    "classify_task_card",
    "expand_template",
    "expanded_contract_digest",
    "reject_unchanged_public_test_outputs",
    "resolve_template",
    "resolve_validation_exemption",
    "skill_task_family",
    "split_command_argv",
    "template_full_id",
    "template_provenance_payload",
    "validate_custom_validation_roles",
    "validate_template_provenance",
]

SCHEMA_ID = "aiworkhub.task_templates.v1"
PROVENANCE_SCHEMA_ID = "aiworkhub.task_template_provenance.v1"
REGISTRY_VERSION = 1
REGISTRY_VERSION_TOKEN = f"v{REGISTRY_VERSION}"

MAX_PATHS_PER_FIELD = 128
MAX_PATH_LENGTH = 500
# One shared card-text budget for BOTH creation paths. Measured over 66
# refused creates: 18 of them carried an objective that the raw
# ``core.create_task`` path accepted (its own inline limit was 4000) and the
# template path refused (this constant was 2000), so the same text passed one
# tool and failed the other with no statement that two limits existed.
# ``core.create_task`` now reads these two names instead of respelling its own
# numbers, so there is exactly one boundary to discover. The permissive value
# is the shared one: unifying downward would have refused card text that is
# accepted and stored today.
MAX_TITLE_LENGTH = 300
MAX_OBJECTIVE_LENGTH = 4000

# The canonical interpreter head for every generated validation command.
#
# Measured across 5,793 declared validation commands: 12 first-token spellings
# for three tools (``python3`` 1,340, ``python`` 1,172, ``.venv/bin/python``
# 779, an absolute venv interpreter 42, bare ``pytest`` 23). All of the BARE
# python spellings are already one thing at execution time --
# ``worker_workspace._normalize_trusted_validation_executable_argv_with_authority``
# matches ``^python(3(\.N)?)?(\.exe)?$`` and replaces the head with
# ``sys.executable`` -- so folding them onto one spelling changes the bytes and
# nothing else.  ``python3`` is the fold target rather than ``python`` for two
# measured reasons: it is the majority spelling in the corpus, and
# ``worker_workspace._is_candidate_pytest_wrapper_command`` recognizes the
# candidate pytest wrapper only for the exact head ``python3``.
#
# Deliberately NOT a symbolic ``${AIWORKHUB_CANONICAL_PYTHON}`` token: the
# finalizer's ``run_validations`` performs no environment expansion. Such a
# token survives ``shlex.split`` intact, matches none of the head branches in
# the resolver above, and would be handed to ``execvpe`` verbatim -- turning
# every acceptance run into ENOENT.  The declared command IS the acceptance
# evidence, so the only spelling that can be byte-identical in the stored card,
# the worker prompt and ``run_validations`` is one the resolver already knows.
CANONICAL_VALIDATION_PYTHON = "python3"
COMMAND_PYTHON = CANONICAL_VALIDATION_PYTHON
COMMAND_NODE = "node"
DIFF_CHECK_COMMAND = "git diff --check"
AUDITED_CUSTOM_ESCAPE = "audited_custom_unclassified"
CUSTOM_TEMPLATE_NAME = "custom"
CANONICAL_MINIMALITY_CONTRACT = (
    "Keep changes bounded to the exact card contract. When Source Graph is required, "
    "use a focus or slice query to check an existing repository symbol or primitive "
    "before introducing a new abstraction. For equivalent solutions, prefer in order "
    "an existing repository primitive, the standard library or platform facade, an "
    "already-installed dependency, then the smallest new implementation. Minimality "
    "must not weaken correctness, security, trust-boundary requirements, portability, "
    "accessibility, or any exact card requirement."
)

_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_TOKEN_RE = re.compile(r"v[0-9]+")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_GLOB_CHARS_RE = re.compile(r"[*?\[\]]")
_UNSAFE_PATH_CHARS_RE = re.compile(r"[^A-Za-z0-9._+/-]")
_PATH_LIKE_TOKEN_RE = re.compile(
    r"^(?:\.\.?)$|[/\\]|::|\.(?:py|js|mjs|cjs|ts|tsx|jsx|json|md)$"
)
_PYTEST_NODEID_SELECTOR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Compiler include-root option whose path payload may be joined (``-Idir``) or
# separated (``-I dir``); only the payload is validated, never the option prefix.
_INCLUDE_ROOT_OPTION = "-I"
_PYTHON_SUFFIXES = (".py",)
_NODE_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")
_NODE_TEST_MARKERS = (".test", ".spec")
_NODE_TEST_SUFFIXES = tuple(
    f"{marker}{suffix}" for marker in _NODE_TEST_MARKERS for suffix in _NODE_SUFFIXES
)


class TaskTemplateError(ValueError):
    """Fail-closed template rejection; ``str(exc)`` is a stable reason."""



@dataclass(frozen=True)
class _PathPolicy:
    required: bool
    allowed: bool


@dataclass(frozen=True)
class TaskTemplateSpec:
    """One exact frozen template definition."""

    name: str
    title: str
    objective: str
    task_type: str
    work_kind: str
    read_only: bool
    production_path_policy: _PathPolicy
    test_path_policy: _PathPolicy
    read_first_fields: tuple[str, ...]
    generates_pytest: bool
    generates_lint: bool
    generates_diff_check: bool


_REQUIRED = _PathPolicy(required=True, allowed=True)
_OPTIONAL = _PathPolicy(required=False, allowed=True)
_REJECTED = _PathPolicy(required=False, allowed=False)

_TEMPLATE_SPECS: dict[str, TaskTemplateSpec] = {
    "read_only_analysis": TaskTemplateSpec(
        name="read_only_analysis",
        title="Read-only analysis",
        objective=(
            "Analyze the explicit bounded production targets and report "
            "findings without writes, required outputs, or re-run validations."
        ),
        task_type="code",
        work_kind="analysis",
        read_only=True,
        production_path_policy=_OPTIONAL,
        test_path_policy=_REJECTED,
        read_first_fields=("production",),
        generates_pytest=False,
        generates_lint=False,
        generates_diff_check=False,
    ),
    "bugfix_with_regression": TaskTemplateSpec(
        name="bugfix_with_regression",
        title="Bugfix with regression test",
        objective=(
            "Fix the defect at the explicit production paths and cover it "
            "with regression tests at the explicit test paths; outputs "
            "exactly cover the atomic write set."
        ),
        task_type="code",
        work_kind="bugfix",
        read_only=False,
        production_path_policy=_REQUIRED,
        test_path_policy=_REQUIRED,
        read_first_fields=("production", "test"),
        generates_pytest=True,
        generates_lint=True,
        generates_diff_check=True,
    ),
    "implementation_with_tests": TaskTemplateSpec(
        name="implementation_with_tests",
        title="Implementation with tests",
        objective=(
            "Implement the requested behavior at the explicit production "
            "paths with explicit test coverage; outputs exactly cover the "
            "atomic write set."
        ),
        task_type="code",
        work_kind="implementation",
        read_only=False,
        production_path_policy=_REQUIRED,
        test_path_policy=_REQUIRED,
        read_first_fields=("production", "test"),
        generates_pytest=True,
        generates_lint=True,
        generates_diff_check=True,
    ),
    "test_only": TaskTemplateSpec(
        name="test_only",
        title="Test-only change",
        objective=(
            "Extend or repair tests at the explicit test paths only; "
            "production code is outside the write scope."
        ),
        task_type="code",
        work_kind="test",
        read_only=False,
        production_path_policy=_REJECTED,
        test_path_policy=_REQUIRED,
        read_first_fields=("test",),
        generates_pytest=True,
        generates_lint=True,
        generates_diff_check=True,
    ),
    "docs_change": TaskTemplateSpec(
        name="docs_change",
        title="Documentation change",
        objective=(
            "Update documentation at the explicit doc paths only; no code "
            "or test writes and no pytest or ruff re-run."
        ),
        task_type="code",
        work_kind="docs",
        read_only=False,
        production_path_policy=_REQUIRED,
        test_path_policy=_REJECTED,
        read_first_fields=("production",),
        generates_pytest=False,
        generates_lint=False,
        generates_diff_check=True,
    ),
    "validation_replay": TaskTemplateSpec(
        name="validation_replay",
        title="Validation replay",
        objective=(
            "Re-run targeted pytest, lint and diff-check validation over "
            "the explicit paths without any writes or required outputs."
        ),
        task_type="code",
        work_kind="replay",
        read_only=True,
        production_path_policy=_OPTIONAL,
        test_path_policy=_REQUIRED,
        read_first_fields=("production", "test"),
        generates_pytest=True,
        generates_lint=True,
        generates_diff_check=True,
    ),
    "cross_boundary_bugfix": TaskTemplateSpec(
        name="cross_boundary_bugfix",
        title="Cross-boundary bugfix",
        objective=(
            "Fix the defect across explicit Python and Node production "
            "paths and cover each language with its own tests; Python "
            "commands never include JavaScript paths."
        ),
        task_type="code",
        work_kind="bugfix",
        read_only=False,
        production_path_policy=_REQUIRED,
        test_path_policy=_REQUIRED,
        read_first_fields=("production", "test"),
        generates_pytest=True,
        generates_lint=True,
        generates_diff_check=True,
    ),
}

TEMPLATE_SPECS: MappingProxyType = MappingProxyType(_TEMPLATE_SPECS)
TEMPLATE_IDS: tuple[str, ...] = tuple(_TEMPLATE_SPECS)


def _canonical_definition_payload(spec: TaskTemplateSpec) -> str:
    """Canonical JSON binding name, registry version and full definition."""
    payload = {
        "name": spec.name,
        "registry_version": REGISTRY_VERSION,
        "definition": asdict(spec),
    }
    if not spec.read_only:
        payload["minimality_contract"] = CANONICAL_MINIMALITY_CONTRACT
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


_LEGACY_REGISTRY_VERSION = 1


def _legacy_definition_digest(spec: TaskTemplateSpec) -> str:
    """Return the definition digest emitted by the original v1 registry."""
    payload = {
        "name": spec.name,
        "registry_version": _LEGACY_REGISTRY_VERSION,
        "definition": asdict(spec),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _definition_digest(spec: TaskTemplateSpec) -> str:
    return hashlib.sha256(
        _canonical_definition_payload(spec).encode("utf-8")
    ).hexdigest()


def template_full_id(name: str) -> str:
    """Return the exact ``{name}@v{N}:{digest}`` full ID for a short name."""
    if not isinstance(name, str):
        raise TaskTemplateError("template_id_not_string")
    if not name:
        raise TaskTemplateError("template_id_empty")
    if name not in _TEMPLATE_SPECS:
        raise TaskTemplateError("template_unknown")
    spec = _TEMPLATE_SPECS[name]
    return f"{spec.name}@{REGISTRY_VERSION_TOKEN}:{_definition_digest(spec)}"


def resolve_template(template_id: Any) -> TaskTemplateSpec:
    """Accept an exact short name or exact current full ID; fail closed."""
    if not isinstance(template_id, str):
        raise TaskTemplateError("template_id_not_string")
    if not template_id:
        raise TaskTemplateError("template_id_empty")
    if template_id in _TEMPLATE_SPECS:
        return _TEMPLATE_SPECS[template_id]
    name, at_marker, rest = template_id.partition("@")
    if not at_marker:
        raise TaskTemplateError("template_unknown")
    version, colon_marker, digest = rest.partition(":")
    if not colon_marker:
        raise TaskTemplateError("template_id_malformed")
    if version != REGISTRY_VERSION_TOKEN:
        if _VERSION_TOKEN_RE.fullmatch(version):
            raise TaskTemplateError("template_version_stale")
        raise TaskTemplateError("template_id_malformed")
    if not _HEX64_RE.fullmatch(digest):
        raise TaskTemplateError("template_id_malformed")
    if name not in _TEMPLATE_SPECS:
        raise TaskTemplateError("template_unknown")
    spec = _TEMPLATE_SPECS[name]
    if digest != _definition_digest(spec):
        raise TaskTemplateError("template_digest_mismatch")
    return spec


def _validated_path(entry: Any, field: str) -> str:
    """Validate one path entry fail-closed with a stable reason."""
    if not isinstance(entry, str):
        raise TaskTemplateError(f"invalid_{field}_path_not_string")
    if not entry:
        raise TaskTemplateError(f"invalid_{field}_path_empty")
    if len(entry) > MAX_PATH_LENGTH:
        raise TaskTemplateError(f"invalid_{field}_path_too_long")
    if _CONTROL_CHARS_RE.search(entry):
        raise TaskTemplateError(f"invalid_{field}_path_control_character")
    if any(character.isspace() for character in entry):
        raise TaskTemplateError(f"invalid_{field}_path_whitespace")
    if "\\" in entry:
        raise TaskTemplateError(f"invalid_{field}_path_backslash")
    if "~" in entry:
        raise TaskTemplateError(f"invalid_{field}_path_home_token")
    if _GLOB_CHARS_RE.search(entry):
        raise TaskTemplateError(f"invalid_{field}_path_glob_character")
    if entry.startswith("/"):
        raise TaskTemplateError(f"invalid_{field}_path_absolute")
    if entry.endswith("/"):
        raise TaskTemplateError(f"invalid_{field}_path_not_normalized")
    if _UNSAFE_PATH_CHARS_RE.search(entry):
        raise TaskTemplateError(f"invalid_{field}_path_unsafe_token")
    for component in entry.split("/"):
        if component == "" or component == ".":
            raise TaskTemplateError(f"invalid_{field}_path_not_normalized")
        if component == "..":
            raise TaskTemplateError(f"invalid_{field}_path_escape")
        if component.startswith("-"):
            raise TaskTemplateError(f"invalid_{field}_path_leading_hyphen")
    return entry


def _bounded_paths(value: Any, field: str) -> list[str]:
    """Validate a bounded, ordered, duplicate-free list of safe paths."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise TaskTemplateError(f"invalid_{field}_paths")
    if len(value) > MAX_PATHS_PER_FIELD:
        raise TaskTemplateError(f"invalid_{field}_paths")
    paths: list[str] = []
    for entry in value:
        path = _validated_path(entry, field)
        if path in paths:
            raise TaskTemplateError(f"invalid_{field}_path_duplicate")
        paths.append(path)
    return paths


def _bounded_text(value: Any, field: str, limit: int) -> str:
    """Return the stripped text, naming exactly why it was refused.

    A bare ``invalid_<field>`` collapsed three different causes -- wrong type,
    empty, and over the limit -- into one word, and never stated the limit. A
    caller composing a card could only find the boundary by bisecting against
    the API. ``_validate_path`` a few lines above already names its cause
    (``_path_too_long``, ``_path_control_character``); this now matches it.
    """

    if not isinstance(value, str):
        raise TaskTemplateError(f"invalid_{field}:not_a_string:{type(value).__name__}")
    text = value.strip()
    if not text:
        raise TaskTemplateError(f"invalid_{field}:empty")
    if len(text) > limit:
        raise TaskTemplateError(
            f"invalid_{field}:too_long:{len(text)}_chars_exceeds_limit_{limit}"
        )
    return text


def _enforce_scope(
    spec: TaskTemplateSpec, production: list[str], tests: list[str]
) -> None:
    """Reject missing required paths and incompatible-scope paths."""
    for policy, paths, field in (
        (spec.production_path_policy, production, "production"),
        (spec.test_path_policy, tests, "test"),
    ):
        if policy.required and not paths:
            raise TaskTemplateError(f"missing_{field}_paths")
        if not policy.allowed and paths:
            raise TaskTemplateError(f"incompatible_scope_{field}_paths")


def split_command_argv(command: str) -> list[str]:
    """Split on single spaces only; validated paths never need quoting."""
    if not isinstance(command, str):
        raise TaskTemplateError("invalid_command")
    return command.split(" ")


# ---------------------------------------------------------------------------
# Canonical validation-command head (measured 2026-09-08).
#
# ``_BARE_PYTHON_INTERPRETER_RE`` below is the exact pattern
# ``worker_workspace`` uses to decide that a head is a bare python interpreter
# and must be replaced by ``sys.executable``. It is restated here rather than
# imported because ``task_templates`` is a pure, self-contained module with no
# lifecycle imports; ``tests/test_task_templates.py`` asserts the two patterns
# stay identical so the restatement cannot drift.
_BARE_PYTHON_INTERPRETER_RE = re.compile(r"^python(3(\.[0-9]+)?)?(\.[eE][xX][eE])?$")
# The exact literal ``worker_workspace._is_candidate_pytest_wrapper_command``
# recognizes, and only for the head ``python3``.
_CANDIDATE_PYTEST_WRAPPER = "tools/candidate_pytest.py"
# A repo-relative venv head. It resolves at finalization (the coordinator holds
# the real ``.venv``) but can never resolve for the WORKER: a worker worktree is
# a sparse checkout of tracked, card-declared files and ``.venv`` is not tracked.
_VENV_HEAD_PREFIXES = (".venv/bin/", ".venv/Scripts/")


def canonical_validation_command(command: Any) -> str:
    """Fold a validation command's head onto the canonical spelling.

    Only provably outcome-identical folds are performed, so the normalized
    command is acceptance evidence for exactly the same execution:

    * a bare python interpreter (``python``/``python3.12``/``python.exe``)
      becomes ``python3`` -- every one of those heads is already replaced by
      ``sys.executable`` by the finalizer's head resolver;
    * a bare ``pytest`` head becomes ``python3 -m pytest`` -- exactly the
      rewrite ``worker_workspace._normalize_pytest_validation_argv`` applies
      before execution.

    Everything else is returned unchanged, by design: an ABSOLUTE head is left
    exactly as declared, and so is a repo-relative head (``.venv/bin/python``,
    ``.venv/bin/ruff``) or a trusted bare validator (``ruff``/``mypy``/``node``
    /``git``), because those resolve through a different, root-bearing branch
    of the resolver and folding them would change which executable runs.

    Non-strings and unparseable commands are returned as-is; this function
    never refuses. Refusal remains ``worker_workspace.validation_argv``'s job.
    """
    if not isinstance(command, str):
        return command
    stripped = command.strip()
    if not stripped:
        return command
    argv = stripped.split(" ")
    # Preserve a leading supported env assignment / ``cd DIR &&`` prefix
    # untouched, and normalize only the executable head that follows it.
    prefix: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if "=" in token and token.split("=", 1)[0].isidentifier():
            prefix.append(token)
            index += 1
            continue
        if token == "cd" and index + 2 < len(argv) and argv[index + 2] == "&&":
            prefix.extend(argv[index : index + 3])
            index += 3
            continue
        break
    rest = argv[index:]
    if not rest:
        return command
    head, tail = rest[0], rest[1:]
    if head == CANONICAL_VALIDATION_PYTHON:
        return command
    if _BARE_PYTHON_INTERPRETER_RE.match(head):
        if tail[:1] == [_CANDIDATE_PYTEST_WRAPPER]:
            # The wrapper is recognized ONLY for the exact head ``python3``;
            # folding onto the canonical head is what makes it recognizable.
            pass
        rest = [CANONICAL_VALIDATION_PYTHON, *tail]
    elif head == "pytest":
        rest = [CANONICAL_VALIDATION_PYTHON, "-m", "pytest", *tail]
    else:
        return command
    return " ".join([*prefix, *rest])


def validation_command_head_warnings(command: Any) -> list[str]:
    """Advisory findings about a declared command head. Never a refusal.

    A ``.venv``-relative head is the measured landmine: it resolves for the
    coordinator at finalization and can never resolve inside the worker's
    sparse worktree, so the worker fails closed and submits an unvalidated
    candidate. Naming it at creation is the earliest point it can be seen.
    """
    if not isinstance(command, str) or not command.strip():
        return []
    argv = command.strip().split(" ")
    head = ""
    for token in argv:
        if "=" in token and token.split("=", 1)[0].isidentifier():
            continue
        if token in ("cd", "&&"):
            continue
        head = token
        break
    warnings: list[str] = []
    if head.startswith(_VENV_HEAD_PREFIXES):
        warnings.append(f"venv_relative_head_absent_from_worker_worktree:{head}")
    elif head.startswith("/") and "/.venv/" in head:
        warnings.append(f"absolute_venv_head_not_portable:{head}")
    return warnings


# ---------------------------------------------------------------------------
# Contract patch: an approve-by-reference channel for the unchanged-output set.
#
# 243 tasks ended on ``required_output_unchanged`` /
# ``residual_contract_file_unchanged`` / ``required_output_mismatch`` /
# ``required_output_zero_bytes``, yet only 32 of 4,681 cards ever declared
# ``allow_unchanged_required_outputs`` -- because declaring it means retyping
# paths that must match ``required_outputs`` AND ``allowed_writes``
# byte-for-byte (the manager got that wrong twice in the measured sample).
# The finalizer already HOLDS the exact unchanged-path list. These helpers let
# it publish that list once, addressed by a digest, so the manager approves the
# exception by reference instead of retyping it.
#
# This is a proposal channel, never an application: nothing here widens a card
# on its own. ``core.create_task`` resolves a digest the manager passes and
# then runs the same ``validate_required_output_exceptions`` checks it runs on
# a typed list.
CONTRACT_PATCH_SCHEMA_ID = "aiworkhub.task_contract_patch.v1"
CONTRACT_PATCH_RELATIVE_DIR = ".aiworkhub/tasking/contract_patches"
_CONTRACT_PATCH_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _contract_patch_payload(
    *,
    task_id: str,
    allow_unchanged_required_outputs: Sequence[str],
    required_outputs: Sequence[str],
    allowed_writes: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_id": CONTRACT_PATCH_SCHEMA_ID,
        "task_id": str(task_id),
        "allow_unchanged_required_outputs": [
            str(path) for path in allow_unchanged_required_outputs
        ],
        "required_outputs": [str(path) for path in required_outputs],
        "allowed_writes": [str(path) for path in allowed_writes],
    }


def contract_patch_digest(payload: Mapping[str, Any]) -> str:
    """Digest of one canonical contract-patch payload."""
    canonical = {
        key: payload.get(key)
        for key in (
            "schema_id",
            "task_id",
            "allow_unchanged_required_outputs",
            "required_outputs",
            "allowed_writes",
        )
    }
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_contract_patch(
    *,
    task_id: Any,
    unchanged_paths: Sequence[Any],
    required_outputs: Sequence[Any],
    allowed_writes: Sequence[Any],
) -> dict[str, Any]:
    """Build the machine-readable patch for one measured unchanged-output set.

    ``unchanged_paths`` is the exact list the finalizer computed. Only paths
    that are already BOTH a declared required output and inside the declared
    write scope are proposed -- the two byte-for-byte agreements the manager
    kept getting wrong -- so an accepted patch cannot widen the card.
    """
    outputs = [str(path) for path in required_outputs]
    writes = [str(path) for path in allowed_writes]
    output_set, write_set = set(outputs), set(writes)
    proposed: list[str] = []
    rejected: list[dict[str, str]] = []
    for raw in unchanged_paths:
        path = str(raw)
        if path in proposed:
            continue
        if path not in output_set:
            rejected.append({"path": path, "reason": "not_in_required_outputs"})
            continue
        if path not in write_set:
            rejected.append({"path": path, "reason": "not_in_allowed_writes"})
            continue
        proposed.append(path)
    payload = _contract_patch_payload(
        task_id=str(task_id),
        allow_unchanged_required_outputs=sorted(proposed),
        required_outputs=outputs,
        allowed_writes=writes,
    )
    payload["digest"] = contract_patch_digest(payload)
    payload["rejected"] = rejected
    payload["apply_hint"] = (
        "aiworkhub_task_create(..., apply_contract_patch="
        f"\"{payload['digest']}\") -- the manager still decides; nothing is "
        "applied automatically."
    )
    return payload


def _contract_patch_path(repo_root: Any, digest: str) -> Any:
    from pathlib import Path

    return Path(repo_root) / CONTRACT_PATCH_RELATIVE_DIR / f"{digest}.json"


def record_contract_patch(repo_root: Any, patch: Mapping[str, Any]) -> str:
    """Persist one patch under its own digest and return that digest.

    Idempotent by construction: the file name IS the digest of its content, so
    re-recording an identical patch rewrites identical bytes.
    """
    digest = str(patch.get("digest") or "")
    if not _CONTRACT_PATCH_DIGEST_RE.match(digest):
        raise TaskTemplateError("invalid_contract_patch_digest")
    expected = contract_patch_digest(patch)
    if digest != expected:
        raise TaskTemplateError("contract_patch_digest_mismatch")
    target = _contract_patch_path(repo_root, digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    temporary.write_text(
        json.dumps(dict(patch), sort_keys=True, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)
    return digest


def load_contract_patch(repo_root: Any, digest: Any) -> dict[str, Any]:
    """Load and re-authenticate one recorded patch by digest.

    Fails closed with a stable reason: an unknown digest, unreadable file, or
    content whose recomputed digest does not match its own name is refused
    rather than partially trusted.
    """
    token = str(digest or "").strip().lower()
    if not _CONTRACT_PATCH_DIGEST_RE.match(token):
        raise TaskTemplateError("invalid_contract_patch_digest")
    target = _contract_patch_path(repo_root, token)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskTemplateError(f"contract_patch_not_found:{token}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaskTemplateError(f"contract_patch_unreadable:{token}") from exc
    if not isinstance(payload, dict):
        raise TaskTemplateError(f"contract_patch_unreadable:{token}")
    if payload.get("schema_id") != CONTRACT_PATCH_SCHEMA_ID:
        raise TaskTemplateError(f"contract_patch_schema_mismatch:{token}")
    if contract_patch_digest(payload) != token:
        raise TaskTemplateError(f"contract_patch_digest_mismatch:{token}")
    return payload


_CANONICAL_WORK_KINDS = frozenset({
    "generic",
    "bugfix",
    "refactor",
    "performance",
    "security",
    "data_ml",
})
_REQUIRED_VALIDATION_ROLES: dict[str, tuple[str, ...]] = {
    "bugfix": ("reproduction", "regression"),
    "refactor": ("parity",),
    "performance": ("baseline", "delta"),
    "security": ("negative_fixture",),
    "data_ml": ("schema", "distribution"),
}


def _canonical_work_kind(work_kind: str) -> str:
    if work_kind in _CANONICAL_WORK_KINDS:
        return work_kind
    return "generic"


def skill_task_family(work_kind: str) -> str:
    """Return the skill task family a template's DECLARED work kind names.

    ``work_kind`` on the card is the behavioral-contract key, constrained to
    ``quality_evidence.WORK_KINDS``; five of the seven built-in templates
    declare a work kind outside that set, so ``_canonical_work_kind`` flattens
    ``analysis``, ``implementation``, ``test``, ``docs`` and ``replay`` to
    ``generic``. That flattening is correct for the behavioral contract and
    fatal for skill selection, because ``generic`` is not a family any skill
    can be about. This reads the declared value instead, before the flattening,
    and answers "" for anything the skill vocabulary does not name -- an absent
    family selects nothing rather than selecting everything.
    """
    declared = str(work_kind or "").strip().lower()
    return declared if declared in skill_registry.SKILL_TASK_FAMILIES else ""


def _validation_roles_for(work_kind: str, validation: Sequence[str]) -> list[str]:
    """Seed a one-to-one role list for the live expansion path.

    This helper only assigns roles; the authoritative behavioral-contract
    check is ``normalize_behavioral_contract`` in ``quality_evidence`` (the one
    normalized authority, exercised by the classify/create-task path and its
    tests).  It deliberately keeps no duplicate fail-closed guard of its own,
    so no dead security control lingers here.
    """
    required = _REQUIRED_VALIDATION_ROLES.get(work_kind, ())
    roles: list[str] = []
    for index, _command in enumerate(validation):
        roles.append(required[index] if index < len(required) else "generic")
    return roles


def _is_python_path(path: str) -> bool:
    return path.endswith(_PYTHON_SUFFIXES)


def _is_node_path(path: str) -> bool:
    return path.endswith(_NODE_SUFFIXES)


_TEST_ROOT = "tests"
# Deterministic path-kind authority.  Only these exact suffixless paths are real
# directory targets eligible for pytest/Ruff; every other suffixless path is
# treated as an ordinary file and never routed.  This is an explicit allowlist
# rather than a first-character casing heuristic, so a suffixless file (a
# lowercase leaf such as ``tests/data`` or an underscore-prefixed leaf such as
# ``tests/_helpers``) can never be mistaken for a directory, while the sanctioned
# ``tests`` and ``tests/unit`` directory targets stay supported.
_SUFFIXLESS_DIRECTORY_TARGETS = frozenset({_TEST_ROOT, f"{_TEST_ROOT}/unit"})


def _is_suffixless_directory_target(path: str) -> bool:
    """Return True only for an explicitly sanctioned suffixless directory target.

    Membership in :data:`_SUFFIXLESS_DIRECTORY_TARGETS` is the sole authority, so
    ordinary suffixless files (``tests/Makefile``, ``tests/LICENSE``), lowercase
    or underscore-prefixed suffixless leaves under ``tests`` (``tests/data``,
    ``tests/_helpers``) and any nested leaf (``tests/fixtures/sample``) are never
    directory targets and never reach pytest or Ruff, without touching the
    filesystem or guessing a path kind from its name casing.
    """
    return path in _SUFFIXLESS_DIRECTORY_TARGETS


def _is_python_toolchain_path(path: str) -> bool:
    """Return True when a target is eligible for pytest/Ruff.

    A Python file, or a real suffixless directory target rooted at ``tests``
    (``tests``, ``tests/unit``).  Suffixless ordinary files (Makefile, LICENSE,
    Dockerfile), suffixless files nested under ``tests`` (``tests/Makefile``,
    ``tests/fixtures/sample``) and non-Python assets (JSON, TOML, Markdown,
    images) are never Python-toolchain targets and never reach pytest or Ruff.
    """
    if _is_python_path(path):
        return True
    return _is_suffixless_directory_target(path)


def _canonical_repo_relative_path(path: str) -> str:
    """Collapse separators and resolve ``.``/``..`` traversal.

    Windows separators and duplicate or leading ``./`` slashes are folded, and a
    ``..`` segment is resolved against the preceding real segment so traversal
    aliases (``tests/../tests/x``) canonicalize onto their true target.  A ``..``
    that would escape the repo root is preserved verbatim so it can never
    silently alias an in-repo output.
    """
    parts: list[str] = []
    for part in path.replace("\\", "/").strip().split("/"):
        if part in {"", "."}:
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _is_public_test_path(path: str) -> bool:
    return path == _TEST_ROOT or path.startswith(f"{_TEST_ROOT}/")


def _looks_like_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        _is_public_test_path(path)
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(_NODE_TEST_SUFFIXES)
    )



# Repository gates that ANY change under the package trips, appended by the
# template so a card author never has to remember them. Both read only `src/`
# and `tests/`, so a worker's sparse-checkout worktree can actually run them.
#
# tests/test_os_dependency_boundary.py is deliberately ABSENT: it imports
# check_os_dependency_boundary from scripts/, which no worker worktree
# contains, so declaring it fails a card at collection with ModuleNotFoundError
# no matter how correct the work is. That gate belongs to the manager on the
# canonical tree. Measured: it cost AIWORKHUB_01079 a full rerun after 227 of
# its own tests had passed.
PACKAGE_ROOT = "src/aiworkhub/"
PACKAGE_GATE_TESTS: tuple[str, ...] = (
    "tests/test_module_size_ratchet.py",
    "tests/test_declared_invariants.py",
)


def _package_gate_command(production: Sequence[str]) -> str | None:
    """One pytest command for the gates a package change always trips."""
    if not any(str(path).startswith(PACKAGE_ROOT) for path in production):
        return None
    return " ".join([COMMAND_PYTHON, "-m", "pytest", "-q", *PACKAGE_GATE_TESTS])


def _validation_commands_for(
    spec: TaskTemplateSpec, production: list[str], tests: list[str]
) -> list[str]:
    """Language/toolchain-aware validation commands in one deterministic order.

    Python-compatible targets drive pytest/Ruff and applicable JavaScript
    targets drive ``node --test`` in a single consolidated builder shared by
    every template.  Suffixless ordinary files (Makefile, LICENSE, Dockerfile)
    and non-Python assets (JSON, TOML, Markdown, images) are never routed to
    pytest or Ruff, while real suffixless directory targets such as ``tests``
    and ``tests/unit`` stay supported.
    """
    py_production = [path for path in production if _is_python_toolchain_path(path)]
    py_tests = [path for path in tests if _is_python_toolchain_path(path)]
    node_tests = [path for path in tests if _is_node_path(path)]
    if spec.name == "cross_boundary_bugfix":
        node_production = [path for path in production if _is_node_path(path)]
        if not (py_production or py_tests) or not (node_production or node_tests):
            raise TaskTemplateError("missing_cross_boundary_languages")
    validation: list[str] = []
    if spec.generates_pytest and py_tests:
        validation.append(
            " ".join([COMMAND_PYTHON, "-m", "pytest", "-q", *py_tests])
        )
    if spec.generates_lint and (py_production or py_tests):
        validation.append(
            " ".join(
                [COMMAND_PYTHON, "-m", "ruff", "check", *py_production, *py_tests]
            )
        )
    if spec.generates_pytest and node_tests:
        validation.append(" ".join([COMMAND_NODE, "--test", *node_tests]))
    if spec.generates_diff_check:
        validation.append(DIFF_CHECK_COMMAND)
    gate = _package_gate_command(production)
    if gate is not None and gate not in validation:
        validation.append(gate)
    return validation


def expand_template(
    template_id: Any,
    *,
    production_paths: Sequence[Any] | None = None,
    test_paths: Sequence[Any] | None = None,
    title: Any = None,
    objective: Any = None,
    mandatory_changed_outputs: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Expand one template plus explicit bounded paths into card fields.

    Returns plain data for the existing authoritative ``create_task`` card:
    read-first targets, an allowed-write scope, and deterministic validation
    commands. ``allowed_writes``/``write_set`` is the full authorized scope.
    For ``bugfix_with_regression``, omitted ``mandatory_changed_outputs``
    makes that exact scope mandatory; for ``test_only`` it defaults to the
    exact test paths; for ``docs_change`` it defaults to the exact production
    (doc) paths (NF-2026-00772), since that field is its entire write
    contract and a docs card that requires nothing to change defeats its own
    purpose. Every other template keeps an empty default. An explicitly
    supplied mandatory list -- including an explicit empty one -- remains
    authoritative for every template and must be a subset of the write scope:
    only the unresolved DEFAULT was ever the bug, never a caller's deliberate
    choice.
    """
    spec = resolve_template(template_id)
    production = _bounded_paths(
        () if production_paths is None else production_paths, "production"
    )
    tests = _bounded_paths(() if test_paths is None else test_paths, "test")
    _enforce_scope(spec, production, tests)
    if set(production) & set(tests):
        raise TaskTemplateError("duplicate_path_across_fields")
    write_set: list[str] = [] if spec.read_only else [*production, *tests]
    if spec.name == "bugfix_with_regression":
        mandatory_default: Sequence[Any] = write_set
    elif spec.name == "test_only":
        mandatory_default = tests
    elif spec.name == "docs_change":
        mandatory_default = production
    else:
        mandatory_default = ()
    mandatory = _bounded_paths(
        mandatory_default
        if mandatory_changed_outputs is None
        else mandatory_changed_outputs,
        "mandatory_changed_output",
    )
    write_scope = set(write_set)
    for path in mandatory:
        if path not in write_scope:
            raise TaskTemplateError("mandatory_changed_output_out_of_scope")
    mandatory_set = set(mandatory)
    required_outputs = [path for path in write_set if path in mandatory_set]
    read_first: list[str] = []
    for field_name in spec.read_first_fields:
        read_first.extend(production if field_name == "production" else tests)
    validation = _validation_commands_for(spec, production, tests)
    resolved_title = spec.title if title is None else title
    resolved_objective = spec.objective if objective is None else objective
    work_kind = _canonical_work_kind(spec.work_kind)
    validation_roles = _validation_roles_for(work_kind, validation)
    return {
        "schema_id": SCHEMA_ID,
        "template_id": spec.name,
        "template_full_id": (
            f"{spec.name}@{REGISTRY_VERSION_TOKEN}:{_definition_digest(spec)}"
        ),
        "registry_version": REGISTRY_VERSION,
        "definition_digest": _definition_digest(spec),
        "title": _bounded_text(resolved_title, "title", MAX_TITLE_LENGTH),
        "objective": _bounded_text(
            resolved_objective, "objective", MAX_OBJECTIVE_LENGTH
        ),
        "task_type": spec.task_type,
        "work_kind": work_kind,
        # The family the template DECLARES, before work_kind is flattened to
        # the behavioral-contract vocabulary. Additive: expanded_contract_digest
        # hashes an explicit field whitelist, which this key is not part of, so
        # every stored template provenance keeps authenticating unchanged.
        "skill_task_family": skill_task_family(spec.work_kind),
        "read_only": spec.read_only,
        "read_first": read_first,
        "allowed_writes": list(write_set),
        "required_outputs": required_outputs,
        "write_set": list(write_set),
        "validation": validation,
        "validation_roles": validation_roles,
        **(
            {"minimality_contract": CANONICAL_MINIMALITY_CONTRACT}
            if not spec.read_only
            else {}
        ),
    }


def expanded_contract_digest(
    card: Mapping[str, Any], *, trusted_legacy_definition_digest: str | None = None
) -> str:
    """Hash one canonical expanded payload shape.

    ``trusted_legacy_definition_digest`` is retained for API compatibility but
    is deliberately not an authority.  Legacy-v1 hashing is selected only for
    a complete, internally consistent persisted provenance record.
    """
    minimality_contract = card.get("minimality_contract")
    del trusted_legacy_definition_digest
    legacy_v1 = _authenticated_legacy_v1_provenance(card) is not None
    provenance = card.get("template_provenance")
    current_builtin_claim = False
    if isinstance(provenance, Mapping):
        name = provenance.get("template_name")
        if isinstance(name, str) and name in _TEMPLATE_SPECS:
            current_builtin_claim = provenance.get("definition_digest") == (
                _definition_digest(_TEMPLATE_SPECS[name])
            )
            embedded = provenance.get("expanded_contract")
            if (
                minimality_contract is None
                and current_builtin_claim
                and isinstance(embedded, Mapping)
            ):
                minimality_contract = embedded.get("minimality_contract")
    if (
        minimality_contract is None
        and not bool(card.get("read_only"))
        and not current_builtin_claim
    ):
        minimality_contract = CANONICAL_MINIMALITY_CONTRACT
    payload = {
        "allowed_writes": list(card.get("allowed_writes") or []),
        "read_first": list(card.get("read_first") or []),
        "read_only": bool(card.get("read_only")),
        "required_outputs": list(card.get("required_outputs") or []),
        "validation": list(card.get("validation") or []),
        "validation_roles": list(card.get("validation_roles") or []),
        "work_kind": str(card.get("work_kind") or "generic"),
    }
    if not legacy_v1:
        payload["minimality_contract"] = str(minimality_contract or "")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _expansion_field_matches(field: str, declared: Any, expected: Any) -> bool:
    """Whether a persisted card field still matches a fresh template expansion.

    Every field but ``validation`` is compared exactly, as it always was.

    ``validation`` is compared on the CANONICAL command instead, and the reason
    is measured: 435 persisted cards in this repository carry template
    provenance and a bare ``python`` head, the spelling
    ``_validation_commands_for`` emitted before ``COMMAND_PYTHON`` became
    ``python3``. A byte-exact comparison against a fresh expansion would
    de-authenticate every one of them -- and a de-authenticated template card
    fails ``_validate_required_outputs_contract`` at launch with
    ``required_outputs_invalid``. Their stored ``expanded_contract_digest`` is
    unaffected either way: it is computed from the card's OWN fields, never from
    a re-expansion, so nothing here weakens the digest.

    This is not a relaxation of authority. ``canonical_validation_command``
    folds only heads that the finalizer's own resolver already maps to one
    executable, so two commands that compare equal here select the same
    interpreter and the same file arguments. It cannot make a card authenticate
    against a template that validates different files, different flags, or a
    different tool.
    """
    if field != "validation":
        return declared == expected
    if not isinstance(declared, (list, tuple)) or not isinstance(
        expected, (list, tuple)
    ):
        return declared == expected
    if len(declared) != len(expected):
        return False
    return all(
        canonical_validation_command(left) == canonical_validation_command(right)
        for left, right in zip(declared, expected)
    )


def _authenticated_legacy_v1_provenance(
    card: Mapping[str, Any],
) -> dict[str, Any] | None:
    provenance = card.get("template_provenance")
    if not isinstance(provenance, dict) or "minimality_contract" in card:
        return None
    try:
        validated = validate_template_provenance(provenance, expanded_card=card)
    except TaskTemplateError:
        return None
    name = validated["template_name"]
    if name not in _TEMPLATE_SPECS:
        return None
    spec = _TEMPLATE_SPECS[name]
    if validated["definition_digest"] != _legacy_definition_digest(spec):
        return None
    writes = list(card.get("allowed_writes") or [])
    input_paths = list(card.get("read_first") or []) if spec.read_only else writes
    production, tests = _partition_write_set(input_paths)
    try:
        expected = expand_template(
            name,
            production_paths=production,
            test_paths=tests,
            mandatory_changed_outputs=list(card.get("required_outputs") or []),
        )
    except TaskTemplateError:
        return None
    for field in (
        "allowed_writes",
        "read_first",
        "read_only",
        "required_outputs",
        "validation",
        "validation_roles",
        "work_kind",
    ):
        if not _expansion_field_matches(field, card.get(field), expected[field]):
            return None
    legacy_payload = {
        "allowed_writes": writes,
        "read_first": list(card.get("read_first") or []),
        "read_only": bool(card.get("read_only")),
        "required_outputs": list(card.get("required_outputs") or []),
        "validation": list(card.get("validation") or []),
        "validation_roles": list(card.get("validation_roles") or []),
        "work_kind": str(card.get("work_kind") or "generic"),
    }
    digest = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if validated["expanded_contract_digest"] != digest:
        return None
    return validated


def _authenticated_current_provenance(
    card: Mapping[str, Any],
) -> dict[str, Any] | None:
    provenance = card.get("template_provenance")
    if not isinstance(provenance, dict):
        return None
    if "minimality_contract" not in card:
        return _authenticated_legacy_v1_provenance(card)
    try:
        validated = validate_template_provenance(provenance, expanded_card=card)
    except TaskTemplateError:
        return None
    name = validated["template_name"]
    if name not in _TEMPLATE_SPECS:
        return None
    writes = list(card.get("allowed_writes") or [])
    spec = _TEMPLATE_SPECS[name]
    input_paths = list(card.get("read_first") or []) if spec.read_only else writes
    production, tests = _partition_write_set(input_paths)
    try:
        expected = expand_template(
            name,
            production_paths=production,
            test_paths=tests,
            mandatory_changed_outputs=list(card.get("required_outputs") or []),
        )
    except TaskTemplateError:
        return None
    fields = (
        "allowed_writes",
        "read_first",
        "read_only",
        "required_outputs",
        "validation",
        "validation_roles",
        "work_kind",
        "minimality_contract",
    )
    for field in fields:
        if not _expansion_field_matches(field, card.get(field), expected.get(field)):
            return None
    if validated["expanded_contract_digest"] != expanded_contract_digest(card):
        return None
    return validated


class _BoundTemplateProvenance(dict[str, Any]):
    """Ephemeral provenance carrying its independently expanded source card."""

    def __init__(self, payload: Mapping[str, Any], expanded_card: Mapping[str, Any]):
        super().__init__(payload)
        self.expanded_card = dict(expanded_card)


def template_provenance_payload(
    card: Mapping[str, Any], *, classification_reason: str
) -> dict[str, Any]:
    if not isinstance(classification_reason, str) or not classification_reason.strip():
        raise TaskTemplateError("classification_reason_invalid")
    name = str(card.get("template_id") or "")
    digest = str(card.get("definition_digest") or "")
    version = int(card.get("registry_version") or REGISTRY_VERSION)
    payload = {
        "schema_id": PROVENANCE_SCHEMA_ID,
        "template_name": name,
        "template_full_id": str(
            card.get("template_full_id") or f"{name}@v{version}:{digest}"
        ),
        "registry_version": version,
        "definition_digest": digest,
        "classification_reason": classification_reason.strip(),
        "expanded_contract_digest": expanded_contract_digest(card),
    }
    if name in _TEMPLATE_SPECS:
        payload["expanded_contract"] = {
            field: card.get(field)
            for field in (
                "allowed_writes",
                "read_first",
                "read_only",
                "required_outputs",
                "validation",
                "validation_roles",
                "work_kind",
                "minimality_contract",
            )
        }
        return _BoundTemplateProvenance(payload, card)
    return payload


def _validated_validation_token(token: str) -> None:
    path_token = token
    if "::" in token:
        path_token, selector = token.split("::", 1)
        if (
            not path_token
            or not selector
            or ":" in path_token
            or selector.startswith(":")
            or selector.endswith(":")
        ):
            raise TaskTemplateError("invalid_validation_path_unsafe_token")
        for part in selector.split("::"):
            if not part or not _PYTEST_NODEID_SELECTOR_RE.fullmatch(part):
                raise TaskTemplateError("invalid_validation_path_unsafe_token")
    _validated_path(path_token, "validation")


def validate_custom_validation_roles(
    validation: Any, validation_roles: Any
) -> None:
    if isinstance(validation, (str, bytes)) or (
        validation is not None and not isinstance(validation, (list, tuple))
    ):
        raise TaskTemplateError("invalid_validation_not_string")
    if isinstance(validation_roles, (str, bytes)) or (
        validation_roles is not None and not isinstance(validation_roles, (list, tuple))
    ):
        raise TaskTemplateError("invalid_validation_roles_not_string")
    if validation is not None and len(validation) > MAX_PATHS_PER_FIELD:
        raise TaskTemplateError("invalid_validation")
    if validation_roles is not None and len(validation_roles) > MAX_PATHS_PER_FIELD:
        raise TaskTemplateError("invalid_validation_roles")
    for item in () if validation is None else validation:
        if not isinstance(item, str):
            raise TaskTemplateError("invalid_validation_not_string")
        tokens = split_command_argv(item)
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if token == _INCLUDE_ROOT_OPTION:
                # Separated include root (``-I dir``): the next token is the real
                # path operand and is validated exactly like the joined ``-Idir``
                # payload. A dangling ``-I`` or one whose operand is itself an
                # option is malformed and rejected, never silently skipped.
                if index >= len(tokens) or tokens[index].startswith("-"):
                    raise TaskTemplateError("invalid_validation_embedded_path")
                payload = tokens[index]
                index += 1
            elif token.startswith(_INCLUDE_ROOT_OPTION):
                # Joined include root (``-Idir``): validate only the path payload
                # so it matches the separated ``-I dir`` form exactly.
                payload = token[len(_INCLUDE_ROOT_OPTION) :]
            elif not token or not _PATH_LIKE_TOKEN_RE.search(token):
                continue
            else:
                payload = token
            try:
                _validated_validation_token(payload)
            except TaskTemplateError as exc:
                raise TaskTemplateError("invalid_validation_embedded_path") from exc
    for item in () if validation_roles is None else validation_roles:
        if not isinstance(item, str):
            raise TaskTemplateError("invalid_validation_roles_not_string")


def reject_unchanged_public_test_outputs(
    allow_unchanged: Sequence[Any], required_outputs: Sequence[Any]
) -> None:
    required = {
        _canonical_repo_relative_path(item).lower()
        for item in required_outputs
        if isinstance(item, str)
    }
    required.discard("")
    for item in allow_unchanged:
        if not isinstance(item, str):
            raise TaskTemplateError("unchanged_required_public_test_output")
        normalized = _canonical_repo_relative_path(item).lower()
        if _is_public_test_path(normalized) and normalized in required:
            raise TaskTemplateError("unchanged_required_public_test_output")


# ---------------------------------------------------------------------------
# Zero-validation gate.
#
# The test gate used to be opt-in: a card that declared no validation commands
# ran zero tests and still reached ``review_ready`` with
# ``deterministic_verification.evidence_verdict.nothing_measured = true``.  A
# positive measured failure is short-circuited before a reviewer launch, but
# "we did not measure" deliberately falls through to a full review, so a card
# with nothing declared bypassed both gates.
#
# Every card now either declares validation commands or carries ONE explicit
# named exemption token.  The exemption is a name, never an absence: an empty
# list, an empty string or any non-string is refused, so a card can never
# become exempt by declaring nothing.
#
# There is exactly one legitimate reason, taken from the code rather than
# guessed.  A card with a write scope always has something to measure.  A
# read-only card with no write scope has nothing to run: that is the
# ``read_only_analysis`` template (the only registry template whose spec
# generates no pytest, lint or diff-check command), and it is also the exact
# shape every ``quality_review`` reviewer child is created with.  Reviewer
# children reach ``create_task`` classified as ``read_only_analysis`` with
# ``read_only=True`` and empty ``allowed_writes``/``required_outputs``, so
# naming this one exemption keeps every reviewer launch working.
#
# ``docs_change`` and ``validation_replay`` need no exemption and get none:
# ``docs_change`` always emits ``git diff --check`` and ``validation_replay``
# requires test paths, so neither can expand to an empty command list.
#
# Measured 2026-09-07 against the 4,628 cards in .aiworkhub/tasking/
# task_queue.sqlite: 1,607 declare validation, 2,982 empty-validation cards
# match this exemption (2,930 of them reviewer children), and 39 (0.84%) do
# not.  All 39 are legacy rows created before the ``read_only`` field existed
# and all 39 are already terminal (22 archived, 9 superseded, 8 finished); no
# pending or in-flight card is refused.
VALIDATION_EXEMPTION_READ_ONLY = "read_only_no_write_scope"
VALIDATION_EXEMPTIONS: tuple[str, ...] = (VALIDATION_EXEMPTION_READ_ONLY,)


def resolve_validation_exemption(
    *,
    validation: Any,
    read_only: Any,
    allowed_writes: Any,
    required_outputs: Any,
    declared: Any = None,
) -> str | None:
    """Name why a card may reach review with nothing measured, or fail closed.

    Returns ``None`` when the card declares validation commands, the exact
    exemption token when the card legitimately has nothing to run, and raises
    ``TaskTemplateError`` with a stable reason otherwise.  ``declared`` is an
    optional caller-supplied token; when omitted the exemption is derived from
    the card's own explicit ``read_only``/write-scope declarations and stamped
    on the card by name, so the reason a card measured nothing is always
    readable downstream instead of being inferred from an empty list.
    """
    declared_given = declared is not None
    if declared_given and (not isinstance(declared, str) or not declared.strip()):
        # An empty list, an empty string, ``False`` and ``0`` are all absences,
        # not names.  Refusing them here is what stops "declare nothing" from
        # quietly becoming "exempt from everything".
        raise TaskTemplateError("invalid_validation_exemption_not_named")
    if validation:
        if declared_given:
            raise TaskTemplateError("validation_exemption_with_validation")
        return None
    eligible = (
        read_only is True
        and not list(allowed_writes or [])
        and not list(required_outputs or [])
    )
    if declared_given:
        token = declared.strip()
        if token not in VALIDATION_EXEMPTIONS:
            raise TaskTemplateError("unknown_validation_exemption")
        if token == VALIDATION_EXEMPTION_READ_ONLY and not eligible:
            raise TaskTemplateError("validation_exemption_precondition_unmet")
        return token
    if eligible:
        return VALIDATION_EXEMPTION_READ_ONLY
    raise TaskTemplateError("validation_required")


def validate_template_provenance(
    payload: Any,
    *,
    expanded_card: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate provenance and bind built-ins to their exact expanded card.

    Built-in provenance is not self-authenticating: its expanded digest must be
    checked against the card fields it claims to describe.  Passing that card
    also enables the explicitly supported legacy-v1 upgrade path; callers no
    longer need a private compatibility flag.
    """
    if not isinstance(payload, dict):
        raise TaskTemplateError("template_provenance_invalid")
    if expanded_card is None and isinstance(payload, _BoundTemplateProvenance):
        expanded_card = payload.expanded_card
    required = (
        "schema_id",
        "template_name",
        "template_full_id",
        "registry_version",
        "definition_digest",
        "classification_reason",
        "expanded_contract_digest",
    )
    if any(key not in payload for key in required):
        raise TaskTemplateError("template_provenance_invalid")
    name = payload["template_name"]
    full_id = payload["template_full_id"]
    version = payload["registry_version"]
    definition_digest = payload["definition_digest"]
    reason = payload["classification_reason"]
    expanded_digest = payload["expanded_contract_digest"]
    if payload["schema_id"] != PROVENANCE_SCHEMA_ID:
        raise TaskTemplateError("template_provenance_schema_mismatch")
    if not isinstance(name, str) or not name:
        raise TaskTemplateError("template_provenance_invalid")
    if not isinstance(full_id, str) or not full_id:
        raise TaskTemplateError("template_provenance_invalid")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise TaskTemplateError("template_provenance_invalid")
    if not isinstance(definition_digest, str) or not _HEX64_RE.fullmatch(
        definition_digest
    ):
        raise TaskTemplateError("template_provenance_invalid")
    if not isinstance(reason, str) or not reason.strip():
        raise TaskTemplateError("classification_reason_invalid")
    if not isinstance(expanded_digest, str) or not _HEX64_RE.fullmatch(
        expanded_digest
    ):
        raise TaskTemplateError("template_provenance_invalid")
    expected_full_id = f"{name}@v{version}:{definition_digest}"
    if full_id != expected_full_id:
        raise TaskTemplateError("template_provenance_identity_mismatch")
    if name == CUSTOM_TEMPLATE_NAME:
        if version != REGISTRY_VERSION:
            raise TaskTemplateError("template_version_stale")
        embedded_contract = payload.get("expanded_contract")
        if expanded_card is None or not isinstance(embedded_contract, Mapping):
            raise TaskTemplateError("template_expanded_contract_required")
        bound_contract = {
            field: expanded_card.get(field)
            for field in (
                "allowed_writes",
                "read_first",
                "read_only",
                "required_outputs",
                "validation",
                "validation_roles",
                "work_kind",
                "minimality_contract",
            )
        }
        if dict(embedded_contract) != bound_contract:
            raise TaskTemplateError("template_expanded_contract_mismatch")
        digest_card = dict(expanded_card)
        digest_card.pop("template_provenance", None)
        if expanded_digest != expanded_contract_digest(digest_card):
            raise TaskTemplateError("template_expanded_contract_mismatch")
        canonical_definition = {
            "name": CUSTOM_TEMPLATE_NAME,
            "registry_version": REGISTRY_VERSION,
            "escape": AUDITED_CUSTOM_ESCAPE,
            "expanded_contract_digest": expanded_digest,
        }
        canonical_digest = hashlib.sha256(
            json.dumps(
                canonical_definition, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        if definition_digest != canonical_digest:
            raise TaskTemplateError("template_digest_mismatch")
        if reason != "audited_custom_escape":
            raise TaskTemplateError("template_provenance_invalid")
        return {
            "schema_id": PROVENANCE_SCHEMA_ID,
            "template_name": name,
            "template_full_id": full_id,
            "registry_version": version,
            "definition_digest": definition_digest,
            "classification_reason": reason,
            "expanded_contract_digest": expanded_digest,
            "expanded_contract": dict(embedded_contract),
        }
    if name not in _TEMPLATE_SPECS:
        raise TaskTemplateError("template_unknown")
    spec = _TEMPLATE_SPECS[name]
    current_digest = _definition_digest(spec)
    legacy_digest = _legacy_definition_digest(spec)
    is_current = version == REGISTRY_VERSION and definition_digest == current_digest
    is_legacy = (
        version == _LEGACY_REGISTRY_VERSION
        and definition_digest == legacy_digest
    )
    if not is_current and not is_legacy:
        if version not in {REGISTRY_VERSION, _LEGACY_REGISTRY_VERSION}:
            raise TaskTemplateError("template_version_stale")
        raise TaskTemplateError("template_digest_mismatch")
    embedded_contract = payload.get("expanded_contract")
    if expanded_card is None:
        raise TaskTemplateError("template_expanded_contract_required")
    if (
        embedded_contract is not None
        and embedded_contract
        != {
            field: expanded_card.get(field)
            for field in embedded_contract
        }
    ):
        raise TaskTemplateError("template_expanded_contract_mismatch")
    digest_payload = {
        "allowed_writes": list(expanded_card.get("allowed_writes") or []),
        "read_first": list(expanded_card.get("read_first") or []),
        "read_only": bool(expanded_card.get("read_only")),
        "required_outputs": list(expanded_card.get("required_outputs") or []),
        "validation": list(expanded_card.get("validation") or []),
        "validation_roles": list(expanded_card.get("validation_roles") or []),
        "work_kind": str(expanded_card.get("work_kind") or "generic"),
    }

    def _canonical_expanded_digest(include_minimality: bool) -> str:
        payload = dict(digest_payload)
        if include_minimality:
            minimality = expanded_card.get("minimality_contract")
            if minimality is None and not payload["read_only"]:
                minimality = CANONICAL_MINIMALITY_CONTRACT
            payload["minimality_contract"] = str(minimality or "")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    # A read-only template's current and legacy definition digests coincide, so
    # ``is_current`` and ``is_legacy`` can both hold for one authentic receipt.
    # The current registry hashes the empty minimality contract into the
    # expansion while the pre-minimality legacy registry omitted it. Authenticate
    # against the format each identity actually claims -- current (with the
    # minimality contract) or legacy (without) -- instead of dropping minimality
    # whenever the legacy digest merely happens to match. A forged expansion that
    # matches neither format still fails closed.
    authenticated = (
        is_current and expanded_digest == _canonical_expanded_digest(True)
    ) or (is_legacy and expanded_digest == _canonical_expanded_digest(False))
    if not authenticated:
        raise TaskTemplateError("template_expanded_contract_mismatch")
    validated = {
        "schema_id": PROVENANCE_SCHEMA_ID,
        "template_name": name,
        "template_full_id": full_id,
        "registry_version": version,
        "definition_digest": definition_digest,
        "classification_reason": reason.strip(),
        "expanded_contract_digest": expanded_digest,
    }
    if embedded_contract is not None:
        validated["expanded_contract"] = embedded_contract
    return validated


def _partition_write_set(paths: Sequence[str]) -> tuple[list[str], list[str]]:
    production: list[str] = []
    tests: list[str] = []
    for path in paths:
        if _looks_like_test_path(path):
            tests.append(path)
        else:
            production.append(path)
    return production, tests


def _custom_escape_provenance(card: Mapping[str, Any]) -> dict[str, Any]:
    expanded_digest = expanded_contract_digest(card)
    payload = {
        "name": CUSTOM_TEMPLATE_NAME,
        "registry_version": REGISTRY_VERSION,
        "escape": AUDITED_CUSTOM_ESCAPE,
        "expanded_contract_digest": expanded_digest,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_id": PROVENANCE_SCHEMA_ID,
        "template_name": CUSTOM_TEMPLATE_NAME,
        "template_full_id": f"{CUSTOM_TEMPLATE_NAME}@{REGISTRY_VERSION_TOKEN}:{digest}",
        "registry_version": REGISTRY_VERSION,
        "definition_digest": digest,
        "classification_reason": "audited_custom_escape",
        "expanded_contract_digest": expanded_digest,
        "expanded_contract": {
            field: card.get(field)
            for field in (
                "allowed_writes",
                "read_first",
                "read_only",
                "required_outputs",
                "validation",
                "validation_roles",
                "work_kind",
                "minimality_contract",
            )
        },
    }


def classify_task_card(
    *,
    allowed_writes: Sequence[str],
    required_outputs: Sequence[str],
    validation: Sequence[str],
    validation_roles: Sequence[str] | None = None,
    work_kind: str = "generic",
    read_only: bool = False,
    read_first: Sequence[str] | None = None,
    allow_unchanged_required_outputs: Sequence[str] | None = None,
    custom_escape: str | None = None,
    template_provenance: Mapping[str, Any] | None = None,
    minimality_contract: str | None = None,
) -> dict[str, Any]:
    validate_custom_validation_roles(validation, validation_roles)
    writes = list(allowed_writes)
    outputs = list(required_outputs)
    commands = list(validation)
    roles = list(validation_roles or [])
    first = list(read_first or [])
    unchanged = (
        ()
        if allow_unchanged_required_outputs is None
        else allow_unchanged_required_outputs
    )
    reject_unchanged_public_test_outputs(unchanged, outputs)
    production, tests = _partition_write_set(writes)
    has_python = any(_is_python_path(path) for path in writes)
    has_node = any(_is_node_path(path) for path in writes)
    candidates: list[tuple[str, str]] = []
    if has_python and has_node:
        candidates.append(
            ("cross_boundary_bugfix", "compatible_cross_boundary_bugfix")
        )
    if work_kind == "bugfix":
        candidates.append(
            ("bugfix_with_regression", "compatible_bugfix_with_regression")
        )
    if (
        work_kind in {"generic", "implementation"}
        and has_python
        and not has_node
        and production
        and tests
        and not read_only
    ):
        candidates.append(
            (
                "implementation_with_tests",
                "compatible_generic_python_production_plus_test",
            )
        )
    seen = {name for name, _reason in candidates}
    for name in TEMPLATE_IDS:
        if name not in seen:
            candidates.append((name, f"compatible_{name}"))
    card_view = {
        "allowed_writes": writes,
        "required_outputs": outputs,
        "validation": commands,
        "validation_roles": roles,
        "work_kind": work_kind,
        "read_only": read_only,
        "read_first": first,
    }
    if minimality_contract is not None:
        card_view["minimality_contract"] = minimality_contract
    if template_provenance is not None:
        card_view["template_provenance"] = template_provenance
        if template_provenance.get("template_name") == CUSTOM_TEMPLATE_NAME:
            if custom_escape != AUDITED_CUSTOM_ESCAPE:
                raise TaskTemplateError("custom_escape_invalid")
            return validate_template_provenance(
                dict(template_provenance), expanded_card=card_view
            )
        authenticated = _authenticated_current_provenance(card_view)
        if authenticated is None:
            authenticated = _authenticated_legacy_v1_provenance(card_view)
        if authenticated is None:
            raise TaskTemplateError("template_legacy_identity_invalid")
        return authenticated
    for name, reason in candidates:
        try:
            expanded = expand_template(
                name,
                production_paths=production,
                test_paths=tests,
                mandatory_changed_outputs=outputs,
            )
        except TaskTemplateError:
            continue
        if (
            expanded["allowed_writes"] != writes
            or expanded["required_outputs"] != outputs
            or expanded["validation"] != commands
            or expanded["read_only"] is not read_only
        ):
            continue
        if first != list(expanded["read_first"]):
            continue
        if roles != list(expanded["validation_roles"]):
            continue
        if (
            minimality_contract is not None
            and minimality_contract != expanded.get("minimality_contract")
        ):
            continue
        stored = dict(card_view)
        for field in (
            "template_id",
            "template_full_id",
            "registry_version",
            "definition_digest",
            "minimality_contract",
        ):
            if field in expanded:
                stored[field] = expanded[field]
        return template_provenance_payload(stored, classification_reason=reason)
    escape = "" if custom_escape is None else custom_escape
    if escape == AUDITED_CUSTOM_ESCAPE:
        # NF-2026-00772: no template matched (that path already enforces its
        # own required_outputs contract, empty-permitting only for the exact
        # templates that declare it -- e.g. implementation_with_tests), so
        # the audited escape is the sole remaining authority over this card.
        # It must not silently smuggle through a writable card nothing is
        # ever required to prove changed; a read-only card legitimately has
        # no required_outputs, but read_only can never be inferred here.
        if not read_only and not outputs:
            raise TaskTemplateError(
                "custom_escape_writable_requires_required_outputs"
            )
        return _custom_escape_provenance(card_view)
    if escape:
        raise TaskTemplateError("custom_escape_invalid")
    raise TaskTemplateError("template_unclassified")
