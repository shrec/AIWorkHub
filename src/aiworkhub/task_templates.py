"""Pure deterministic registry of classified AIWorkHub task templates.

This module is additive and self-contained: it never imports or mutates
``core.py``, ``task_store.py`` or any other lifecycle module.  The output of
``expand_template`` is plain data that a manager may feed into the existing,
authoritative ``create_task`` card fields; no integration yet.

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
  absolute paths, ``.``/``..`` components, duplicates and any character
  outside the safe POSIX token set are rejected with stable reasons before
  any command is generated, so every generated validation command preserves
  exact, deterministic argv tokenization (single-space split, no quoting).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Sequence

__all__ = [
    "COMMAND_PYTHON",
    "DIFF_CHECK_COMMAND",
    "MAX_PATH_LENGTH",
    "MAX_PATHS_PER_FIELD",
    "REGISTRY_VERSION",
    "SCHEMA_ID",
    "TEMPLATE_IDS",
    "TEMPLATE_SPECS",
    "TaskTemplateError",
    "TaskTemplateSpec",
    "expand_template",
    "resolve_template",
    "split_command_argv",
    "template_full_id",
]

SCHEMA_ID = "aiworkhub.task_templates.v1"
REGISTRY_VERSION = 1
REGISTRY_VERSION_TOKEN = f"v{REGISTRY_VERSION}"

MAX_PATHS_PER_FIELD = 128
MAX_PATH_LENGTH = 500
MAX_TITLE_LENGTH = 300
MAX_OBJECTIVE_LENGTH = 2000

COMMAND_PYTHON = "python"
DIFF_CHECK_COMMAND = "git diff --check"

_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_TOKEN_RE = re.compile(r"v[0-9]+")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_GLOB_CHARS_RE = re.compile(r"[*?\[\]]")
_UNSAFE_PATH_CHARS_RE = re.compile(r"[^A-Za-z0-9._+/-]")


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
        task_type="docs",
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
    if not isinstance(value, str):
        raise TaskTemplateError(f"invalid_{field}")
    text = value.strip()
    if not text or len(text) > limit:
        raise TaskTemplateError(f"invalid_{field}")
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


def expand_template(
    template_id: Any,
    *,
    production_paths: Sequence[Any] | None = None,
    test_paths: Sequence[Any] | None = None,
    title: Any = None,
    objective: Any = None,
) -> dict[str, Any]:
    """Expand one template plus explicit bounded paths into card fields.

    Returns plain data for the existing authoritative ``create_task`` card:
    read-first targets, an atomic write-set checklist that the outputs and
    allowed writes cover exactly, and deterministic validation commands.
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
    read_first: list[str] = []
    for field_name in spec.read_first_fields:
        read_first.extend(production if field_name == "production" else tests)
    lint_targets = [*production, *tests]
    validation: list[str] = []
    if spec.generates_pytest and tests:
        validation.append(
            " ".join([COMMAND_PYTHON, "-m", "pytest", "-q", *tests])
        )
    if spec.generates_lint and lint_targets:
        validation.append(
            " ".join([COMMAND_PYTHON, "-m", "ruff", "check", *lint_targets])
        )
    if spec.generates_diff_check:
        validation.append(DIFF_CHECK_COMMAND)
    resolved_title = spec.title if title is None else title
    resolved_objective = spec.objective if objective is None else objective
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
        "work_kind": spec.work_kind,
        "read_only": spec.read_only,
        "read_first": read_first,
        "allowed_writes": list(write_set),
        "required_outputs": list(write_set),
        "write_set": list(write_set),
        "validation": validation,
    }
