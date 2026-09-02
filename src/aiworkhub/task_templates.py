"""Pure deterministic registry of classified AIWorkHub task templates.

This module is additive and self-contained: it never imports or mutates
``core.py``, ``task_store.py`` or any other lifecycle module.  The output of
``expand_template`` is plain data for the existing authoritative
``create_task`` card, including one-to-one ``validation_roles``.

Determinism contract:

* Six stable template IDs, each bound to one exact frozen definition.
* A full template ID is ``{name}@v{N}:{digest}`` where ``digest`` is the
  SHA-256 hex digest of the canonical definition JSON at registry version
  ``N``.  Only the current registry version is ever accepted: a stale
  version or a forged digest fails closed with a stable reason token.
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
  scope for ``bugfix_with_regression`` and to empty for every other template.
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

__all__ = [
    "AUDITED_CUSTOM_ESCAPE",
    "COMMAND_NODE",
    "COMMAND_PYTHON",
    "DIFF_CHECK_COMMAND",
    "MAX_PATH_LENGTH",
    "MAX_PATHS_PER_FIELD",
    "PROVENANCE_SCHEMA_ID",
    "REGISTRY_VERSION",
    "SCHEMA_ID",
    "TEMPLATE_IDS",
    "TEMPLATE_SPECS",
    "TaskTemplateError",
    "TaskTemplateSpec",
    "classify_task_card",
    "expand_template",
    "expanded_contract_digest",
    "reject_unchanged_public_test_outputs",
    "resolve_template",
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
MAX_TITLE_LENGTH = 300
MAX_OBJECTIVE_LENGTH = 2000

COMMAND_PYTHON = "python"
COMMAND_NODE = "node"
DIFF_CHECK_COMMAND = "git diff --check"
AUDITED_CUSTOM_ESCAPE = "audited_custom_unclassified"
CUSTOM_TEMPLATE_NAME = "custom"

_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_TOKEN_RE = re.compile(r"v[0-9]+")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_GLOB_CHARS_RE = re.compile(r"[*?\[\]]")
_UNSAFE_PATH_CHARS_RE = re.compile(r"[^A-Za-z0-9._+/-]")
_PATH_LIKE_TOKEN_RE = re.compile(
    r"^(?:\.\.?)$|[/\\]|::|\.(?:py|js|mjs|cjs|ts|tsx|jsx|json|md)$"
)
_PYTEST_NODEID_SELECTOR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


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
    return path.startswith("tests/")


def _looks_like_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        _is_public_test_path(path)
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(_NODE_TEST_SUFFIXES)
    )



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
    makes that exact scope mandatory; every other template keeps an empty
    default. An explicitly supplied mandatory list is authoritative for every
    template and must be a subset of the write scope.
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
    mandatory_default: Sequence[Any] = (
        write_set if spec.name == "bugfix_with_regression" else ()
    )
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
        "read_only": spec.read_only,
        "read_first": read_first,
        "allowed_writes": list(write_set),
        "required_outputs": required_outputs,
        "write_set": list(write_set),
        "validation": validation,
        "validation_roles": validation_roles,
    }


def expanded_contract_digest(card: Mapping[str, Any]) -> str:
    payload = {
        "allowed_writes": list(card.get("allowed_writes") or []),
        "read_first": list(card.get("read_first") or []),
        "read_only": bool(card.get("read_only")),
        "required_outputs": list(card.get("required_outputs") or []),
        "validation": list(card.get("validation") or []),
        "validation_roles": list(card.get("validation_roles") or []),
        "work_kind": str(card.get("work_kind") or "generic"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def template_provenance_payload(
    card: Mapping[str, Any], *, classification_reason: str
) -> dict[str, Any]:
    if not isinstance(classification_reason, str) or not classification_reason.strip():
        raise TaskTemplateError("classification_reason_invalid")
    name = str(card.get("template_id") or "")
    digest = str(card.get("definition_digest") or "")
    version = int(card.get("registry_version") or REGISTRY_VERSION)
    return {
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
        for token in item.split(" "):
            if not token or not _PATH_LIKE_TOKEN_RE.search(token):
                continue
            try:
                _validated_validation_token(token)
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
        if normalized.startswith(f"{_TEST_ROOT}/") and normalized in required:
            raise TaskTemplateError("unchanged_required_public_test_output")


def validate_template_provenance(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TaskTemplateError("template_provenance_invalid")
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
        }
    if name not in _TEMPLATE_SPECS:
        raise TaskTemplateError("template_unknown")
    if version != REGISTRY_VERSION:
        raise TaskTemplateError("template_version_stale")
    spec = _TEMPLATE_SPECS[name]
    if definition_digest != _definition_digest(spec):
        raise TaskTemplateError("template_digest_mismatch")
    return {
        "schema_id": PROVENANCE_SCHEMA_ID,
        "template_name": name,
        "template_full_id": full_id,
        "registry_version": version,
        "definition_digest": definition_digest,
        "classification_reason": reason.strip(),
        "expanded_contract_digest": expanded_digest,
    }


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
    payload = {
        "name": CUSTOM_TEMPLATE_NAME,
        "registry_version": REGISTRY_VERSION,
        "escape": AUDITED_CUSTOM_ESCAPE,
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
        "expanded_contract_digest": expanded_contract_digest(card),
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
        stored = dict(expanded)
        stored.update(card_view)
        return template_provenance_payload(stored, classification_reason=reason)
    escape = "" if custom_escape is None else custom_escape
    if escape == AUDITED_CUSTOM_ESCAPE:
        return _custom_escape_provenance(card_view)
    if escape:
        raise TaskTemplateError("custom_escape_invalid")
    raise TaskTemplateError("template_unclassified")
