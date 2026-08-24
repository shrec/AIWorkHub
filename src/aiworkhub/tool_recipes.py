"""Standalone typed Tool Recipe Registry foundation (RM-2026-00030).

This module is the *description* and *validation* layer for tool invocations.
It defines immutable, versioned recipe manifests and turns a typed parameter
mapping into a deterministic normalized invocation receipt and an ``argv``
vector. It deliberately contains **no execution engine**: there is no
subprocess, no filesystem mutation, no network, no database, no task-store and
no model invocation anywhere in this module. A recipe describes *how* a tool
would be invoked; it never runs anything.

The security posture is structural rather than advisory:

* ``argv`` is built only from fixed literal tokens and typed scalar/path/list
  slots. Shell strings, operators, redirection, command substitution,
  environment expansion and arbitrary (user-chosen) executables are rejected at
  manifest-construction or validation time, so a shell composition downstream
  cannot be produced by this layer even by accident.
* Every scalar value that reaches ``argv`` must be free of shell metacharacters
  and control characters. Paths are additionally checked for a non-empty,
  non-NUL shape. Literal tokens must be single words.
* The executable (``argv[0]``) must be a *literal* token: the program identity
  is fixed by the manifest, never by a parameter.

Determinism is guaranteed by canonicalizing every unordered collection (dict
keys sorted, sets/tuples of tags sorted, parameters/outputs sorted by name)
before hashing, so recipe identity/version/digest and the invocation receipt do
not depend on mapping insertion order or on the host platform.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Stable reason codes.  Every failure raised by this module carries one of
# these exact strings in ``RecipeError.reason`` so callers can branch on a
# stable contract instead of matching message text.
# ---------------------------------------------------------------------------
REASON_UNKNOWN_RECIPE = "unknown_recipe"
REASON_UNKNOWN_VERSION = "unknown_version"
REASON_UNKNOWN_PARAMETER = "unknown_parameter"
REASON_MISSING_PARAMETER = "missing_parameter"
REASON_EXTRA_PARAMETER = "extra_parameter"
REASON_INVALID_TYPE = "invalid_type"
REASON_BOOL_AS_INT = "bool_as_int"
REASON_INVALID_ENUM = "invalid_enum"
REASON_OUT_OF_RANGE = "out_of_range"
REASON_INVALID_PATH = "invalid_path"
REASON_INVALID_LIST_ITEM = "invalid_list_item"
REASON_UNSUPPORTED_PLATFORM = "unsupported_platform"
REASON_UNSUPPORTED_CAPABILITY = "unsupported_capability"
REASON_UNSAFE_LITERAL = "unsafe_literal"
REASON_UNSAFE_VALUE = "unsafe_value"
REASON_NON_LITERAL_EXECUTABLE = "non_literal_executable"
REASON_EMPTY_ARGV = "empty_argv"
REASON_DUPLICATE = "duplicate_definition"
REASON_BAD_MANIFEST = "bad_manifest"

# Capability tags with security meaning.  Discovery and cache eligibility key
# on these exact strings.
CAPABILITY_WRITE = "write"
CAPABILITY_SECRET = "secret"
CAPABILITY_NETWORK = "network"

# Cache disqualification reasons.
CACHE_REASON_POLICY = "cache_policy_never"
CACHE_REASON_MUTABLE = "mutable_parameters"
CACHE_REASON_WRITE = "write_capable"
CACHE_REASON_SECRET = "secret_bearing"
CACHE_REASON_SECURITY_SENSITIVE = "security_sensitive"

# Default bound for discovery results so a caller can never receive an
# unbounded manifest set.
DEFAULT_DISCOVERY_LIMIT = 32

# Hard upper bound for discovery results. ``limit`` values above this are
# clamped and ``None`` is rejected, so discovery can never be unbounded.
MAX_DISCOVERY_LIMIT = 256

# Declared shape of changed-path evidence attached to a receipt.  The receipt
# never fabricates this evidence; it only declares the shape and binds whatever
# the caller supplies (empty by default).
CHANGED_PATH_SHAPE = "list[str] of repository-relative changed paths"


class RecipeError(Exception):
    """A fail-closed recipe failure carrying a stable ``reason`` string."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# Typed enums.
# ---------------------------------------------------------------------------


class ParamType(Enum):
    STR = "str"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    ENUM = "enum"
    PATH = "path"
    LIST = "list"


class OutputType(Enum):
    STDOUT = "stdout"
    FILE = "file"
    DIRECTORY = "directory"
    ARTIFACT = "artifact"


class RiskClass(Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_RISK_RANK = {
    RiskClass.NONE: 0,
    RiskClass.LOW: 1,
    RiskClass.MEDIUM: 2,
    RiskClass.HIGH: 3,
    RiskClass.CRITICAL: 4,
}

_SECURITY_SENSITIVE_RISK = frozenset({RiskClass.HIGH, RiskClass.CRITICAL})


class CachePolicy(Enum):
    CACHEABLE = "cacheable"
    NEVER = "never"


class TaskKind(Enum):
    BUILD = "build"
    TEST = "test"
    LINT = "lint"
    SCAN = "scan"
    FORMAT = "format"
    GENERATE = "generate"
    DEPLOY = "deploy"


# ---------------------------------------------------------------------------
# Shell-safety policy.
#
# ``argv`` is an execve-style vector, never a shell command line.  To make a
# shell composition *structurally impossible* even if a downstream caller
# misuses the vector, every token and every scalar value is rejected when it
# contains a shell metacharacter (operators, redirection, substitution,
# quoting, escaping, grouping, globbing, history expansion) or a control
# character.  Plain spaces are permitted inside scalar/path/list-item *data*
# (they are opaque execve arguments), but literal tokens must be single words.
# ---------------------------------------------------------------------------
_SHELL_METACHARS = frozenset(
    "|" "&" ";" "<" ">" "$" "`" "\\" '"' "'" "(" ")" "[" "]" "{" "}" "*" "?" "!" "~"
)
_CONTROL_CHARS = frozenset(chr(i) for i in range(32)) | {chr(127)}

_RECIPE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+~-]*$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _is_shell_safe(text: str) -> bool:
    """True when ``text`` is a non-empty string free of shell/control chars."""
    if not isinstance(text, str) or text == "":
        return False
    return not any(c in _SHELL_METACHARS or c in _CONTROL_CHARS for c in text)


def _is_safe_literal(text: str) -> bool:
    """True when ``text`` is a single-word literal token (no spaces either)."""
    return isinstance(text, str) and text != "" and " " not in text and _is_shell_safe(text)


def _require_shell_safe(text: str, reason: str, message: str) -> None:
    if not _is_shell_safe(text):
        raise RecipeError(reason, message)


# ---------------------------------------------------------------------------
# Canonicalization and hashing (order- and platform-independent).
# ---------------------------------------------------------------------------


def _canonicalize(value: Any) -> Any:
    """Return a JSON-serializable, order-independent projection of ``value``.

    Dict keys are sorted, sets are sorted by ``repr``, tuples/lists become
    lists, and enums project to their string value.  The result serializes to
    the same bytes on every platform and for every mapping insertion order.
    """
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _canonicalize(v) for k, v in sorted(asdict(value).items())}
    if isinstance(value, Mapping):
        return {k: _canonicalize(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_canonicalize(v) for v in sorted(value, key=repr)]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"cannot canonicalize {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize ``value`` to a deterministic, ASCII-safe JSON string."""
    return json.dumps(
        _canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def version_key(version: str) -> tuple[tuple[int, object], ...]:
    """Total-order key for a dotted version string.

    Numeric segments sort before (and below) non-numeric segments, and the
    numeric/non-numeric wrapper makes mixed tuples comparable deterministically.
    """
    key: list[tuple[int, object]] = []
    for part in version.split("."):
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


# ---------------------------------------------------------------------------
# Schema dataclasses.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """The typed schema of one recipe parameter."""

    name: str
    type: ParamType
    required: bool = False
    default: Any = None
    values: tuple[str, ...] = ()  # ENUM allowed values
    minimum: int | float | None = None  # INT/FLOAT inclusive lower bound
    maximum: int | float | None = None  # INT/FLOAT inclusive upper bound
    item_type: ParamType | None = None  # LIST element type
    item_values: tuple[str, ...] = ()  # LIST-of-ENUM allowed values

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _IDENTIFIER_RE.match(self.name):
            raise RecipeError(REASON_BAD_MANIFEST, f"invalid parameter name {self.name!r}")
        if not isinstance(self.type, ParamType):
            raise RecipeError(REASON_BAD_MANIFEST, f"parameter {self.name!r} has invalid type")
        if self.type is ParamType.ENUM:
            if not self.values:
                raise RecipeError(REASON_BAD_MANIFEST, f"ENUM parameter {self.name!r} needs values")
            for value in self.values:
                if not isinstance(value, str) or value == "":
                    raise RecipeError(
                        REASON_BAD_MANIFEST, f"ENUM parameter {self.name!r} has invalid value {value!r}"
                    )
                _require_shell_safe(
                    value,
                    REASON_BAD_MANIFEST,
                    f"ENUM parameter {self.name!r} value {value!r} is not shell-safe",
                )
        if self.type is ParamType.LIST:
            if self.item_type is None or self.item_type is ParamType.LIST:
                raise RecipeError(
                    REASON_BAD_MANIFEST, f"LIST parameter {self.name!r} needs a non-LIST item_type"
                )
            if self.item_type is ParamType.ENUM:
                if not self.item_values:
                    raise RecipeError(
                        REASON_BAD_MANIFEST, f"LIST-of-ENUM parameter {self.name!r} needs item_values"
                    )
                for value in self.item_values:
                    if not isinstance(value, str) or value == "":
                        raise RecipeError(
                            REASON_BAD_MANIFEST,
                            f"LIST-of-ENUM parameter {self.name!r} has invalid item value {value!r}",
                        )
                    _require_shell_safe(
                        value,
                        REASON_BAD_MANIFEST,
                        f"LIST-of-ENUM parameter {self.name!r} item value {value!r} is not shell-safe",
                    )
        if self.type in (ParamType.INT, ParamType.FLOAT):
            for bound in (self.minimum, self.maximum):
                if bound is not None and isinstance(bound, float) and not math.isfinite(bound):
                    raise RecipeError(REASON_BAD_MANIFEST, f"parameter {self.name!r} has a non-finite bound")
            if (
                self.minimum is not None
                and self.maximum is not None
                and self.minimum > self.maximum
            ):
                raise RecipeError(REASON_BAD_MANIFEST, f"parameter {self.name!r} min > max")
        if self.default is not None:
            _validate_value(self, self.default, self.name)


@dataclass(frozen=True)
class OutputSpec:
    """The typed schema of one recipe output."""

    name: str
    type: OutputType
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _IDENTIFIER_RE.match(self.name):
            raise RecipeError(REASON_BAD_MANIFEST, f"invalid output name {self.name!r}")
        if not isinstance(self.type, OutputType):
            raise RecipeError(REASON_BAD_MANIFEST, f"output {self.name!r} has invalid type")


@dataclass(frozen=True)
class ResourceBounds:
    """Optional resource limits a recipe declares for its (not-yet-run) invocation."""

    max_runtime_seconds: int | float | None = None
    max_memory_mb: int | None = None
    max_output_bytes: int | None = None

    def __post_init__(self) -> None:
        for label in ("max_runtime_seconds", "max_memory_mb", "max_output_bytes"):
            value = getattr(self, label)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RecipeError(REASON_BAD_MANIFEST, f"{label} must be a non-negative number")
            if isinstance(value, float) and not math.isfinite(value):
                raise RecipeError(REASON_BAD_MANIFEST, f"{label} must be finite")
            if value < 0:
                raise RecipeError(REASON_BAD_MANIFEST, f"{label} must be non-negative")


# ---------------------------------------------------------------------------
# Argv tokens: fixed literals and typed slots.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArgvLiteral:
    """An immutable argv token taken verbatim from the manifest."""

    text: str


@dataclass(frozen=True)
class ArgvSlot:
    """An argv position filled by one typed parameter (expanded for lists)."""

    param: str


def lit(text: str) -> ArgvLiteral:
    """Build a literal argv token."""
    return ArgvLiteral(text)


def slot(param: str) -> ArgvSlot:
    """Build a typed parameter slot."""
    return ArgvSlot(param)


# ---------------------------------------------------------------------------
# Value validation (fail-closed, stable reasons).
# ---------------------------------------------------------------------------


def _check_range(value: int | float, spec: ParamSpec, name: str) -> None:
    if spec.minimum is not None and value < spec.minimum:
        raise RecipeError(REASON_OUT_OF_RANGE, f"parameter {name!r} below minimum {spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        raise RecipeError(REASON_OUT_OF_RANGE, f"parameter {name!r} above maximum {spec.maximum}")


def _validate_list_item(
    item_type: ParamType, item_values: tuple[str, ...], value: Any, name: str
) -> Any:
    if item_type is ParamType.STR:
        if not isinstance(value, str):
            raise RecipeError(REASON_INVALID_LIST_ITEM, f"list item {name} must be str")
        _require_shell_safe(value, REASON_INVALID_LIST_ITEM, f"list item {name} is not shell-safe")
        return value
    if item_type is ParamType.PATH:
        if not isinstance(value, str) or value == "":
            raise RecipeError(REASON_INVALID_LIST_ITEM, f"list item {name} must be a non-empty path")
        _require_shell_safe(value, REASON_INVALID_LIST_ITEM, f"list item {name} is an unsafe path")
        return value
    if item_type is ParamType.INT:
        if isinstance(value, bool):
            raise RecipeError(REASON_BOOL_AS_INT, f"list item {name}: bool is not an int")
        if not isinstance(value, int):
            raise RecipeError(REASON_INVALID_LIST_ITEM, f"list item {name} must be int")
        return value
    if item_type is ParamType.FLOAT:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RecipeError(REASON_INVALID_LIST_ITEM, f"list item {name} must be a number")
        result = float(value)
        if not math.isfinite(result):
            raise RecipeError(REASON_INVALID_LIST_ITEM, f"list item {name} must be finite")
        return result
    if item_type is ParamType.BOOL:
        if not isinstance(value, bool):
            raise RecipeError(REASON_INVALID_LIST_ITEM, f"list item {name} must be bool")
        return value
    if item_type is ParamType.ENUM:
        if not isinstance(value, str) or value not in item_values:
            raise RecipeError(REASON_INVALID_ENUM, f"list item {name} is not an allowed value")
        _require_shell_safe(value, REASON_INVALID_LIST_ITEM, f"list item {name} is not shell-safe")
        return value
    raise RecipeError(REASON_INVALID_LIST_ITEM, f"list item {name} has an unsupported type")


def _validate_value(spec: ParamSpec, value: Any, name: str) -> Any:
    """Validate one value against ``spec`` and return its normalized form."""
    if spec.type is ParamType.STR:
        if not isinstance(value, str):
            raise RecipeError(REASON_INVALID_TYPE, f"parameter {name!r} must be str")
        _require_shell_safe(value, REASON_UNSAFE_VALUE, f"parameter {name!r} is not shell-safe")
        return value
    if spec.type is ParamType.PATH:
        if not isinstance(value, str) or value == "":
            raise RecipeError(REASON_INVALID_PATH, f"parameter {name!r} must be a non-empty path")
        _require_shell_safe(value, REASON_INVALID_PATH, f"parameter {name!r} is an unsafe path")
        return value
    if spec.type is ParamType.INT:
        if isinstance(value, bool):
            raise RecipeError(REASON_BOOL_AS_INT, f"parameter {name!r}: bool is not an int")
        if not isinstance(value, int):
            raise RecipeError(REASON_INVALID_TYPE, f"parameter {name!r} must be int")
        _check_range(value, spec, name)
        return value
    if spec.type is ParamType.FLOAT:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RecipeError(REASON_INVALID_TYPE, f"parameter {name!r} must be a number")
        result = float(value)
        if not math.isfinite(result):
            raise RecipeError(REASON_INVALID_TYPE, f"parameter {name!r} must be finite")
        _check_range(result, spec, name)
        return result
    if spec.type is ParamType.BOOL:
        if not isinstance(value, bool):
            raise RecipeError(REASON_INVALID_TYPE, f"parameter {name!r} must be bool")
        return value
    if spec.type is ParamType.ENUM:
        if not isinstance(value, str):
            raise RecipeError(REASON_INVALID_ENUM, f"parameter {name!r} must be one of {spec.values}")
        if value not in spec.values:
            raise RecipeError(REASON_INVALID_ENUM, f"parameter {name!r} must be one of {spec.values}")
        _require_shell_safe(value, REASON_UNSAFE_VALUE, f"parameter {name!r} is not shell-safe")
        return value
    if spec.type is ParamType.LIST:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise RecipeError(REASON_INVALID_TYPE, f"parameter {name!r} must be a list")
        item_type = spec.item_type
        if item_type is None:
            raise RecipeError(REASON_BAD_MANIFEST, f"LIST parameter {name!r} missing item_type")
        return tuple(
            _validate_list_item(item_type, spec.item_values, item, f"{name}[{index}]")
            for index, item in enumerate(value)
        )
    raise RecipeError(REASON_INVALID_TYPE, f"parameter {name!r} has an unsupported type")


# ---------------------------------------------------------------------------
# Recipe manifest.
# ---------------------------------------------------------------------------


def _param_payload(param: ParamSpec) -> dict[str, Any]:
    return {
        "name": param.name,
        "type": param.type.value,
        "required": param.required,
        "default": _canonicalize(param.default),
        "values": sorted(param.values),
        "minimum": param.minimum,
        "maximum": param.maximum,
        "item_type": param.item_type.value if param.item_type is not None else None,
        "item_values": sorted(param.item_values),
    }


def _output_payload(output: OutputSpec) -> dict[str, Any]:
    return {"name": output.name, "type": output.type.value, "description": output.description}


def _bounds_payload(bounds: ResourceBounds) -> dict[str, Any]:
    return {
        "max_runtime_seconds": bounds.max_runtime_seconds,
        "max_memory_mb": bounds.max_memory_mb,
        "max_output_bytes": bounds.max_output_bytes,
    }


def _argv_token_payload(token: ArgvLiteral | ArgvSlot) -> list[str]:
    if isinstance(token, ArgvLiteral):
        return ["literal", token.text]
    if isinstance(token, ArgvSlot):
        return ["slot", token.param]
    raise RecipeError(REASON_BAD_MANIFEST, "argv token must be ArgvLiteral or ArgvSlot")


def _recipe_payload(recipe: "Recipe") -> dict[str, Any]:
    return {
        "id": recipe.id,
        "version": recipe.version,
        "purpose": recipe.purpose,
        "task_kind": recipe.task_kind.value,
        "parameters": [_param_payload(p) for p in sorted(recipe.parameters, key=lambda p: p.name)],
        "outputs": [_output_payload(o) for o in sorted(recipe.outputs, key=lambda o: o.name)],
        "platforms": list(recipe.platforms),
        "capabilities": list(recipe.capabilities),
        "risk_class": recipe.risk_class.value,
        "resource_bounds": _bounds_payload(recipe.resource_bounds),
        "cache_policy": recipe.cache_policy.value,
        "argv": [_argv_token_payload(token) for token in recipe.argv],
    }


def recipe_digest(recipe: "Recipe") -> str:
    """Return the deterministic SHA-256 digest of a recipe's canonical form."""
    return _sha256(canonical_json(_recipe_payload(recipe)))


def _normalize_tags(tags: Iterable[str], label: str) -> tuple[str, ...]:
    normalized: list[str] = []
    for tag in tags:
        if not isinstance(tag, str) or not _TAG_RE.match(tag):
            raise RecipeError(REASON_BAD_MANIFEST, f"invalid {label} {tag!r}")
        if tag not in normalized:
            normalized.append(tag)
    normalized.sort()
    return tuple(normalized)


@dataclass(frozen=True)
class Recipe:
    """An immutable, versioned tool recipe manifest.

    Construction is fail-closed: an unsafe literal, an unknown slot, a
    non-literal executable or an invalid schema raises :class:`RecipeError`
    immediately, so an invalid manifest can never enter a registry.
    """

    id: str
    version: str
    purpose: str = ""
    task_kind: TaskKind = TaskKind.GENERATE
    parameters: tuple[ParamSpec, ...] = ()
    outputs: tuple[OutputSpec, ...] = ()
    platforms: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    risk_class: RiskClass = RiskClass.NONE
    resource_bounds: ResourceBounds = field(default_factory=ResourceBounds)
    cache_policy: CachePolicy = CachePolicy.NEVER
    argv: tuple[ArgvLiteral | ArgvSlot, ...] = ()
    digest: str = field(init=False, default="")

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _RECIPE_ID_RE.match(self.id):
            raise RecipeError(REASON_BAD_MANIFEST, f"invalid recipe id {self.id!r}")
        if (
            not isinstance(self.version, str)
            or not _VERSION_RE.match(self.version)
            or not any(c.isdigit() for c in self.version)
        ):
            raise RecipeError(REASON_BAD_MANIFEST, f"invalid recipe version {self.version!r}")
        if not isinstance(self.purpose, str):
            raise RecipeError(REASON_BAD_MANIFEST, "purpose must be str")
        if not isinstance(self.task_kind, TaskKind):
            raise RecipeError(REASON_BAD_MANIFEST, "task_kind must be a TaskKind")
        if not isinstance(self.risk_class, RiskClass):
            raise RecipeError(REASON_BAD_MANIFEST, "risk_class must be a RiskClass")
        if not isinstance(self.cache_policy, CachePolicy):
            raise RecipeError(REASON_BAD_MANIFEST, "cache_policy must be a CachePolicy")
        if not isinstance(self.resource_bounds, ResourceBounds):
            raise RecipeError(REASON_BAD_MANIFEST, "resource_bounds must be ResourceBounds")

        names: dict[str, ParamSpec] = {}
        for param in self.parameters:
            if not isinstance(param, ParamSpec):
                raise RecipeError(REASON_BAD_MANIFEST, "parameter must be a ParamSpec")
            if param.name in names:
                raise RecipeError(REASON_DUPLICATE, f"duplicate parameter {param.name!r}")
            names[param.name] = param
        for output in self.outputs:
            if not isinstance(output, OutputSpec):
                raise RecipeError(REASON_BAD_MANIFEST, "output must be an OutputSpec")
        output_names = [output.name for output in self.outputs]
        if len(output_names) != len(set(output_names)):
            raise RecipeError(REASON_DUPLICATE, "duplicate output name")

        if not self.argv:
            raise RecipeError(REASON_EMPTY_ARGV, "argv template must not be empty")
        first = self.argv[0]
        if not isinstance(first, ArgvLiteral):
            raise RecipeError(
                REASON_NON_LITERAL_EXECUTABLE, "argv[0] executable must be a literal token"
            )
        if not _is_safe_literal(first.text):
            raise RecipeError(
                REASON_UNSAFE_LITERAL, f"argv[0] literal {first.text!r} is not shell-safe"
            )
        for token in self.argv[1:]:
            if isinstance(token, ArgvLiteral):
                if not _is_safe_literal(token.text):
                    raise RecipeError(
                        REASON_UNSAFE_LITERAL, f"literal {token.text!r} is not shell-safe"
                    )
            elif isinstance(token, ArgvSlot):
                if token.param not in names:
                    raise RecipeError(
                        REASON_UNKNOWN_PARAMETER,
                        f"argv slot references unknown parameter {token.param!r}",
                    )
            else:
                raise RecipeError(REASON_BAD_MANIFEST, "argv token must be ArgvLiteral or ArgvSlot")

        object.__setattr__(self, "platforms", _normalize_tags(self.platforms, "platform"))
        object.__setattr__(self, "capabilities", _normalize_tags(self.capabilities, "capability"))
        object.__setattr__(self, "digest", recipe_digest(self))


# ---------------------------------------------------------------------------
# Parameter validation and argv rendering.
# ---------------------------------------------------------------------------


def validate_parameters(recipe: Recipe, params: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    """Validate ``params`` against ``recipe`` and return a sorted normalized tuple.

    The result is sorted by parameter name, so it (and anything derived from
    it) is independent of the caller's mapping insertion order.
    """
    if not isinstance(params, Mapping):
        raise RecipeError(REASON_INVALID_TYPE, "parameters must be a mapping")
    known = {param.name: param for param in recipe.parameters}
    for name in params:
        if name not in known:
            raise RecipeError(REASON_EXTRA_PARAMETER, f"extra/unknown parameter {name!r}")
    normalized: list[tuple[str, Any]] = []
    for name in sorted(known):
        spec = known[name]
        if name in params:
            normalized.append((name, _validate_value(spec, params[name], name)))
        elif spec.required:
            raise RecipeError(REASON_MISSING_PARAMETER, f"missing required parameter {name!r}")
        elif spec.default is not None:
            normalized.append((name, _validate_value(spec, spec.default, name)))
    return tuple(normalized)


def _render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    raise RecipeError(REASON_INVALID_TYPE, "cannot render value to argv")


def render_argv(recipe: Recipe, normalized: Mapping[str, Any]) -> tuple[str, ...]:
    """Render an argv vector from already-normalized parameter values."""
    out: list[str] = []
    for token in recipe.argv:
        if isinstance(token, ArgvLiteral):
            out.append(token.text)
        elif isinstance(token, ArgvSlot):
            if token.param not in normalized:
                spec = next((p for p in recipe.parameters if p.name == token.param), None)
                if spec is not None and spec.type is ParamType.LIST:
                    # An omitted optional list slot expands to zero arguments.
                    continue
                raise RecipeError(
                    REASON_MISSING_PARAMETER, f"slot parameter {token.param!r} has no value"
                )
            value = normalized[token.param]
            if isinstance(value, tuple):
                out.extend(_render_scalar(item) for item in value)
            else:
                out.append(_render_scalar(value))
        else:
            raise RecipeError(REASON_BAD_MANIFEST, "argv token must be ArgvLiteral or ArgvSlot")
    return tuple(out)


def build_argv(recipe: Recipe, params: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate ``params`` and build the argv vector in one step."""
    normalized = validate_parameters(recipe, params)
    return render_argv(recipe, dict(normalized))


# ---------------------------------------------------------------------------
# Invocation validation (platform / capability gating).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidatedInvocation:
    """A fully validated, normalized invocation ready for receipt binding."""

    recipe: Recipe
    normalized_parameters: tuple[tuple[str, Any], ...]
    argv: tuple[str, ...]
    platform: str | None = None
    capabilities: tuple[str, ...] = ()


def _coerce_capabilities(capabilities: Iterable[str] | None) -> frozenset[str]:
    if capabilities is None:
        return frozenset()
    if isinstance(capabilities, (str, bytes)):
        raise RecipeError(REASON_INVALID_TYPE, "capabilities must be an iterable of str, not a string")
    try:
        caps = frozenset(capabilities)
    except TypeError:
        raise RecipeError(REASON_INVALID_TYPE, "capabilities must be an iterable of str") from None
    for cap in caps:
        if not isinstance(cap, str):
            raise RecipeError(REASON_INVALID_TYPE, "each capability must be a str")
    return caps


def validate_invocation(
    recipe: Recipe,
    params: Mapping[str, Any],
    *,
    platform: str | None = None,
    capabilities: Iterable[str] | None = None,
) -> ValidatedInvocation:
    """Gate a recipe by platform/capability, then validate parameters and argv.

    Platform gating is checked only when ``platform`` is supplied; a recipe
    with no declared platforms runs anywhere.  Capability gating always fails
    closed: every capability the recipe requires must be present.
    """
    caps = _coerce_capabilities(capabilities)
    if platform is not None and recipe.platforms and platform not in recipe.platforms:
        raise RecipeError(
            REASON_UNSUPPORTED_PLATFORM,
            f"recipe {recipe.id} does not support platform {platform!r}",
        )
    missing = [cap for cap in recipe.capabilities if cap not in caps]
    if missing:
        raise RecipeError(
            REASON_UNSUPPORTED_CAPABILITY, f"recipe {recipe.id} requires capabilities {missing}"
        )
    normalized = validate_parameters(recipe, params)
    argv = render_argv(recipe, dict(normalized))
    return ValidatedInvocation(
        recipe=recipe,
        normalized_parameters=normalized,
        argv=argv,
        platform=platform,
        capabilities=tuple(sorted(caps)),
    )


# ---------------------------------------------------------------------------
# Registry, discovery and ranking.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParameterSignature:
    """The discovery-visible schema of one parameter (no default, no body)."""

    name: str
    type: ParamType
    required: bool


@dataclass(frozen=True)
class RecipeManifest:
    """A bounded, discovery-only projection of a recipe.

    It intentionally omits the argv template (the recipe "body"): discovery
    reveals identity and contract metadata, never executable content.
    """

    id: str
    version: str
    digest: str
    purpose: str
    task_kind: TaskKind
    platforms: tuple[str, ...]
    capabilities: tuple[str, ...]
    risk_class: RiskClass
    cache_policy: CachePolicy
    parameters: tuple[ParameterSignature, ...]

    @classmethod
    def from_recipe(cls, recipe: Recipe) -> "RecipeManifest":
        return cls(
            id=recipe.id,
            version=recipe.version,
            digest=recipe.digest,
            purpose=recipe.purpose,
            task_kind=recipe.task_kind,
            platforms=recipe.platforms,
            capabilities=recipe.capabilities,
            risk_class=recipe.risk_class,
            cache_policy=recipe.cache_policy,
            parameters=tuple(
                ParameterSignature(name=p.name, type=p.type, required=p.required)
                for p in recipe.parameters
            ),
        )


def discover(
    registry: "RecipeRegistry",
    *,
    task_kind: TaskKind | None = None,
    platform: str | None = None,
    capabilities: Iterable[str] | None = None,
    risk_max: RiskClass | None = None,
    limit: int = DEFAULT_DISCOVERY_LIMIT,
) -> tuple[RecipeManifest, ...]:
    """Return a bounded, deterministic, ranked set of relevant manifests.

    A recipe is relevant when its task kind, platform requirements,
    capability requirements and risk class all match the requested filters.
    Results are ranked by (ascending risk, id, version) and capped at
    ``limit``, which is itself clamped to ``MAX_DISCOVERY_LIMIT``; ``None`` is
    rejected so the returned set is deterministic and never unbounded.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise RecipeError(REASON_INVALID_TYPE, "limit must be a non-negative int")
    limit = min(limit, MAX_DISCOVERY_LIMIT)
    if task_kind is not None and not isinstance(task_kind, TaskKind):
        raise RecipeError(REASON_INVALID_TYPE, "task_kind must be a TaskKind or None")
    if risk_max is not None and not isinstance(risk_max, RiskClass):
        raise RecipeError(REASON_INVALID_TYPE, "risk_max must be a RiskClass or None")
    caps = _coerce_capabilities(capabilities)
    matched: list[Recipe] = []
    for recipe in registry:
        if task_kind is not None and recipe.task_kind is not task_kind:
            continue
        if platform is not None and recipe.platforms and platform not in recipe.platforms:
            continue
        if not caps.issuperset(recipe.capabilities):
            continue
        if risk_max is not None and _RISK_RANK[recipe.risk_class] > _RISK_RANK[risk_max]:
            continue
        matched.append(recipe)
    matched.sort(key=lambda r: (_RISK_RANK[r.risk_class], r.id, version_key(r.version)))
    matched = matched[:limit]
    return tuple(RecipeManifest.from_recipe(r) for r in matched)


class RecipeRegistry:
    """An immutable registry of recipes keyed by (id, version).

    Recipes are stored sorted by (id, version), so iteration and "latest
    version" resolution are deterministic regardless of insertion order.
    """

    def __init__(self, recipes: Iterable[Recipe]) -> None:
        seen: dict[tuple[str, str], Recipe] = {}
        for recipe in recipes:
            if not isinstance(recipe, Recipe):
                raise RecipeError(REASON_BAD_MANIFEST, "registry entries must be Recipe instances")
            key = (recipe.id, recipe.version)
            if key in seen:
                raise RecipeError(
                    REASON_DUPLICATE, f"duplicate recipe {recipe.id!r} version {recipe.version!r}"
                )
            seen[key] = recipe
        self._recipes = tuple(sorted(seen.values(), key=lambda r: (r.id, version_key(r.version))))

    def __iter__(self) -> Iterator[Recipe]:
        return iter(self._recipes)

    def __len__(self) -> int:
        return len(self._recipes)

    def __contains__(self, recipe_id: object) -> bool:
        return isinstance(recipe_id, str) and any(r.id == recipe_id for r in self._recipes)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted({r.id for r in self._recipes}))

    def versions(self, recipe_id: str) -> tuple[str, ...]:
        return tuple(sorted({r.version for r in self._recipes if r.id == recipe_id}, key=version_key))

    def get(self, recipe_id: str, version: str | None = None) -> Recipe:
        matches = [r for r in self._recipes if r.id == recipe_id]
        if not matches:
            raise RecipeError(REASON_UNKNOWN_RECIPE, f"unknown recipe {recipe_id!r}")
        if version is None:
            return max(matches, key=lambda r: version_key(r.version))
        for recipe in matches:
            if recipe.version == version:
                return recipe
        raise RecipeError(
            REASON_UNKNOWN_VERSION, f"unknown version {version!r} for recipe {recipe_id!r}"
        )

    def validate(
        self,
        recipe_id: str,
        params: Mapping[str, Any],
        *,
        version: str | None = None,
        platform: str | None = None,
        capabilities: Iterable[str] | None = None,
    ) -> ValidatedInvocation:
        recipe = self.get(recipe_id, version)
        return validate_invocation(recipe, params, platform=platform, capabilities=capabilities)

    def build_argv(
        self, recipe_id: str, params: Mapping[str, Any], *, version: str | None = None
    ) -> tuple[str, ...]:
        return build_argv(self.get(recipe_id, version), params)

    def discover(self, **kwargs: Any) -> tuple[RecipeManifest, ...]:
        return discover(self, **kwargs)


# ---------------------------------------------------------------------------
# Cache eligibility.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheDecision:
    """An explicit cache eligibility verdict with stable disqualifiers."""

    eligible: bool
    reasons: tuple[str, ...]


def cache_eligibility(recipe: Recipe) -> CacheDecision:
    """Decide cache eligibility from the manifest, never from runtime state.

    A recipe is cacheable only when it explicitly opts in AND is not mutable,
    write-capable, secret-bearing or security-sensitive.
    """
    reasons: list[str] = []
    if recipe.cache_policy is not CachePolicy.CACHEABLE:
        reasons.append(CACHE_REASON_POLICY)
    if any(p.type is ParamType.LIST for p in recipe.parameters):
        reasons.append(CACHE_REASON_MUTABLE)
    if CAPABILITY_WRITE in recipe.capabilities:
        reasons.append(CACHE_REASON_WRITE)
    if CAPABILITY_SECRET in recipe.capabilities:
        reasons.append(CACHE_REASON_SECRET)
    if recipe.risk_class in _SECURITY_SENSITIVE_RISK:
        reasons.append(CACHE_REASON_SECURITY_SENSITIVE)
    return CacheDecision(eligible=not reasons, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# Normalized invocation receipt (binds identity, params, context, evidence
# shape -- without fabricating any execution).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimingPlaceholder:
    """Timing placeholders; populated only by a (not-present) execution engine."""

    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class ExitPlaceholder:
    """Exit taxonomy placeholders; no exit is fabricated in this foundation."""

    status: str = "not_executed"
    exit_code: int | None = None


@dataclass(frozen=True)
class InvocationReceipt:
    """A deterministic receipt binding a validated invocation.

    It binds recipe version/digest, normalized parameters, the argv vector,
    repository/HEAD identity inputs, timing/exit *placeholders* and the shape
    (plus caller-supplied content) of changed-path evidence.  Nothing here is
    fabricated: timing/exit default to placeholders and changed paths are only
    whatever the caller supplied (empty by default).
    """

    recipe_id: str
    recipe_version: str
    recipe_digest: str
    normalized_parameters: tuple[tuple[str, str], ...]
    argv: tuple[str, ...]
    repository: str | None
    head: str | None
    platform: str | None
    capabilities: tuple[str, ...]
    timing: TimingPlaceholder
    exit: ExitPlaceholder
    changed_path_shape: str
    changed_paths: tuple[str, ...]
    digest: str


def _validate_changed_paths(changed_paths: Iterable[str]) -> tuple[str, ...]:
    """Validate caller-supplied changed-path evidence and sort it deterministically.

    ``changed_paths`` must be an iterable (never a bare string, which would be
    iterated per character) of non-empty, repository-relative, shell-safe
    paths.  Absolute paths, ``..``/``.``/empty segments, backslashes and shell
    metacharacters are rejected so a receipt can never bind ambiguous or
    unsafe evidence.
    """
    if isinstance(changed_paths, (str, bytes)):
        raise RecipeError(
            REASON_INVALID_TYPE, "changed_paths must be an iterable of paths, not a string"
        )
    try:
        entries = list(changed_paths)
    except TypeError:
        raise RecipeError(REASON_INVALID_TYPE, "changed_paths must be an iterable of paths") from None
    validated: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or entry == "":
            raise RecipeError(REASON_INVALID_PATH, "each changed path must be a non-empty str")
        if entry.startswith("/") or "\\" in entry or re.match(r"^[A-Za-z]:", entry):
            raise RecipeError(
                REASON_INVALID_PATH, f"changed path {entry!r} must be repository-relative"
            )
        if any(part in ("", ".", "..") for part in entry.split("/")):
            raise RecipeError(
                REASON_INVALID_PATH, f"changed path {entry!r} must be repository-relative"
            )
        _require_shell_safe(entry, REASON_INVALID_PATH, f"changed path {entry!r} is not shell-safe")
        validated.append(entry)
    return tuple(sorted(validated))


def build_receipt(
    validated: ValidatedInvocation,
    *,
    repository: str | None = None,
    head: str | None = None,
    changed_paths: Iterable[str] = (),
) -> InvocationReceipt:
    """Bind a receipt from a validated invocation plus caller identity inputs."""
    recipe = validated.recipe
    params = tuple(
        (name, canonical_json(value)) for name, value in validated.normalized_parameters
    )
    timing = TimingPlaceholder()
    exit_placeholder = ExitPlaceholder()
    paths = _validate_changed_paths(changed_paths)
    payload = {
        "recipe_id": recipe.id,
        "recipe_version": recipe.version,
        "recipe_digest": recipe.digest,
        "normalized_parameters": [list(pair) for pair in params],
        "argv": list(validated.argv),
        "repository": repository,
        "head": head,
        "platform": validated.platform,
        "capabilities": list(validated.capabilities),
        "timing": _canonicalize(timing),
        "exit": _canonicalize(exit_placeholder),
        "changed_path_shape": CHANGED_PATH_SHAPE,
        "changed_paths": list(paths),
    }
    digest = _sha256(canonical_json(payload))
    return InvocationReceipt(
        recipe_id=recipe.id,
        recipe_version=recipe.version,
        recipe_digest=recipe.digest,
        normalized_parameters=params,
        argv=validated.argv,
        repository=repository,
        head=head,
        platform=validated.platform,
        capabilities=validated.capabilities,
        timing=timing,
        exit=exit_placeholder,
        changed_path_shape=CHANGED_PATH_SHAPE,
        changed_paths=paths,
        digest=digest,
    )
