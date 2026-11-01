"""Project tool library: reusable scripts in repository-root ``tools/``.

A top-level regular file in ``tools/`` is a tool only when it carries header
lines ``<prefix> aiworkhub-tool <key>: <value>`` (prefix ``#``). A file with no
marker is skipped silently; a file with a marker and an invalid header is refused
with a stable reason code. ``.cmd``/``.bat`` files with a marker are always
refused (``batch_file_unsupported``): batch files are not a safe interpreter.

Keys: ``id`` (slug), ``purpose``, ``usage`` (one example line) and ``param``
(repeatable ``name:type``, positional; type ``str|int|path|path_list``). A
``risk`` key is refused: every project tool runs repository code, so each
recipe is MEDIUM risk with the ``write`` capability, like the validation recipes.

Header parsing is stdlib-only and the recipe stack is imported lazily inside
:func:`_recipe`. :func:`scan`, :func:`library_paths` and :func:`prompt_listing`
all go through one helper that keeps only tools whose recipe builds, so they
always agree on the accepted set.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOLS_DIR = "tools"
RECIPE_ID_PREFIX = "project."
MAX_TOOL_BYTES = 64 * 1024
MAX_TOOL_FILES = 256
MAX_PARAMS = 8

REASON_INVALID_HEADER = "invalid_header"
REASON_DUPLICATE_ID = "duplicate_id"
REASON_UNKNOWN_TYPE = "unknown_type"
REASON_SYMLINK = "symlink"
REASON_TOO_LARGE = "too_large"
REASON_UNSUPPORTED_EXTENSION = "unsupported_extension"
REASON_UNREADABLE = "unreadable"
REASON_BATCH_FILE_UNSUPPORTED = "batch_file_unsupported"
REASON_UNSAFE_FILENAME = "unsafe_filename"
REASON_TOO_MANY_FILES = "too_many_files"
REASON_SCAN_FAILED = "scan_failed"

PARAM_TYPES = ("str", "int", "path", "path_list")

_INTERPRETERS: dict[str, tuple[str, ...]] = {
    ".py": ("python",),
    ".ps1": ("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"),
    ".sh": ("bash",),
}
# cmd.exe reads '/x' in a relative path as a switch and a batch file re-parses its own
# arguments (& | ^ %VAR%), so batch files are never a recipe interpreter.
_BATCH_SUFFIXES = frozenset({".cmd", ".bat"})
_SINGLE_KEYS = frozenset({"id", "purpose", "usage"})
_PREFIX = r"(?:#|::|[Rr][Ee][Mm])"
_MARKER_RE = re.compile(rf"^[ \t]*{_PREFIX}[ \t]+aiworkhub-tool\b", re.MULTILINE)
_HEADER_RE = re.compile(
    rf"^[ \t]*({_PREFIX})[ \t]+aiworkhub-tool[ \t]+([a-z]+):[ \t]*(.*?)[ \t]*$"
)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LISTING_PURPOSE_CHARS = 160
_LISTING_USAGE_CHARS = 240


@dataclass(frozen=True)
class _Tool:
    file: str
    id: str
    purpose: str
    usage: str
    params: tuple[tuple[str, str], ...]
    interpreter: tuple[str, ...]
    digest: str


def _parse(path: Path, name: str) -> _Tool | str | None:
    """Return a tool, a refusal reason, or ``None`` for a marker-less file."""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_TOOL_BYTES + 1)
    except OSError:
        return REASON_UNREADABLE
    # Probe on the first 64 KiB with errors='replace': a cut that splits a
    # multibyte character must not hide the marker or turn into a decode error.
    if _MARKER_RE.search(data[:MAX_TOOL_BYTES].decode("utf-8-sig", errors="replace")) is None:
        return None
    if len(data) > MAX_TOOL_BYTES:
        return REASON_TOO_LARGE
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return REASON_UNREADABLE
    suffix = Path(name).suffix.lower()
    if suffix in _BATCH_SUFFIXES:
        return REASON_BATCH_FILE_UNSUPPORTED
    interpreter = _INTERPRETERS.get(suffix)
    if interpreter is None:
        return REASON_UNSUPPORTED_EXTENSION
    if _FILENAME_RE.match(name) is None:
        return REASON_UNSAFE_FILENAME
    prefixes = {"#"}
    fields: dict[str, str] = {}
    params: list[tuple[str, str]] = []
    for line in text.splitlines():
        if _MARKER_RE.match(line) is None:
            continue
        match = _HEADER_RE.match(line)
        if match is None or match.group(1).upper() not in prefixes or not match.group(3):
            return REASON_INVALID_HEADER
        key, value = match.group(2), match.group(3)
        if key == "param":
            param_name, separator, param_type = (part.strip() for part in value.partition(":"))
            if not separator or _PARAM_NAME_RE.match(param_name) is None or not param_type:
                return REASON_INVALID_HEADER
            if param_type not in PARAM_TYPES:
                return REASON_UNKNOWN_TYPE
            if any(param_name == known for known, _ in params) or len(params) >= MAX_PARAMS:
                return REASON_INVALID_HEADER
            params.append((param_name, param_type))
        elif key in _SINGLE_KEYS and key not in fields:
            fields[key] = value
        else:
            return REASON_INVALID_HEADER
    tool_id = fields.get("id", "")
    if _ID_RE.match(tool_id) is None or not fields.get("purpose") or not fields.get("usage"):
        return REASON_INVALID_HEADER
    return _Tool(
        file=name,
        id=tool_id,
        purpose=fields["purpose"],
        usage=fields["usage"],
        params=tuple(params),
        interpreter=interpreter,
        digest=hashlib.sha256(data).hexdigest(),
    )


def _load(repo_root: str | Path) -> tuple[list[_Tool], dict[str, str]]:
    """Parse ``tools/`` into accepted tools and a ``{path: reason}`` refusal map.

    Sequential on purpose: at most MAX_TOOL_FILES top-level files of at most
    64 KiB each, so a worker pool would cost more than the reads and make the
    duplicate-id winner (first by file name) depend on scheduling.
    """
    directory = Path(repo_root) / TOOLS_DIR
    tools: list[_Tool] = []
    refused: dict[str, str] = {}
    try:
        # is_symlink() is False for a Windows junction, so also require the
        # resolved directory to stay inside the repository.
        if directory.is_symlink() or not directory.resolve().is_relative_to(
            Path(repo_root).resolve()
        ):
            return tools, {TOOLS_DIR: REASON_SYMLINK}
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except (FileNotFoundError, NotADirectoryError):
        return tools, refused
    except OSError:
        return tools, {TOOLS_DIR: REASON_UNREADABLE}
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        relative = f"{TOOLS_DIR}/{entry.name}"
        if index >= MAX_TOOL_FILES:
            refused[TOOLS_DIR] = REASON_TOO_MANY_FILES
            break
        try:
            if entry.is_symlink():
                refused[relative] = REASON_SYMLINK
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            refused[relative] = REASON_UNREADABLE
            continue
        outcome = _parse(Path(entry.path), entry.name)
        if outcome is None:
            continue
        if isinstance(outcome, str):
            refused[relative] = outcome
        elif outcome.id in seen:
            refused[relative] = REASON_DUPLICATE_ID
        else:
            seen.add(outcome.id)
            tools.append(outcome)
    return tools, refused


def _recipe(tool: _Tool) -> Any:
    from . import manager_recipe_tools as mrt
    from . import tool_recipes as tr

    scalar = {"str": tr.ParamType.STR, "int": tr.ParamType.INT, "path": tr.ParamType.PATH}
    argv = [tr.lit(token) for token in tool.interpreter]
    argv.append(tr.lit(f"{TOOLS_DIR}/{tool.file}"))
    parameters = []
    for name, kind in tool.params:
        if kind == "path_list":
            parameters.append(mrt._path_list(name))
        else:
            parameters.append(tr.ParamSpec(name=name, type=scalar[kind], required=True))
        argv.append(tr.slot(name))
    return tr.Recipe(
        id=f"{RECIPE_ID_PREFIX}{tool.id}",
        version=f"1.0.0+{tool.digest[:16]}",
        purpose=tool.purpose,
        task_kind=tr.TaskKind.GENERATE,
        parameters=tuple(parameters),
        outputs=(mrt._STDOUT_REPORT,),
        risk_class=tr.RiskClass.MEDIUM,
        capabilities=(tr.CAPABILITY_WRITE,),
        cache_policy=tr.CachePolicy.NEVER,
        argv=tuple(argv),
    )


def _accepted(repo_root: str | Path) -> tuple[list[tuple[_Tool, Any]], dict[str, str]]:
    """The one accepted set: parsed tools whose recipe also builds, plus refusals."""
    tools, refused = _load(repo_root)
    accepted: list[tuple[_Tool, Any]] = []
    for tool in tools:
        try:
            accepted.append((tool, _recipe(tool)))
        except Exception:  # noqa: BLE001 - one bad manifest must not hide the others
            refused[f"{TOOLS_DIR}/{tool.file}"] = REASON_INVALID_HEADER
    return accepted, refused


def scan(repo_root: str | Path) -> tuple[list[Any], dict[str, str]]:
    """Return ``(recipes, refused)`` for ``repo_root``; never raises."""
    try:
        accepted, refused = _accepted(repo_root)
    except Exception:  # noqa: BLE001 - a library failure must never break callers
        return [], {TOOLS_DIR: REASON_SCAN_FAILED}
    return [recipe for _, recipe in accepted], refused


def library_paths(repo_root: str | Path) -> list[str]:
    """Sorted repo-relative paths of accepted tools; never raises."""
    try:
        accepted, _ = _accepted(repo_root)
    except Exception:  # noqa: BLE001
        return []
    return sorted(f"{TOOLS_DIR}/{tool.file}" for tool, _ in accepted)


def prompt_listing(repo_root: str | Path, limit: int) -> list[dict[str, str]]:
    """Bounded ``[{id, purpose, usage}]`` for the worker prompt; never raises."""
    try:
        accepted, _ = _accepted(repo_root)
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "id": f"{RECIPE_ID_PREFIX}{tool.id}",
            "purpose": tool.purpose[:_LISTING_PURPOSE_CHARS],
            "usage": tool.usage[:_LISTING_USAGE_CHARS],
        }
        for tool, _ in accepted[: max(0, int(limit))]
    ]


def verify_recipe_file(run_cwd: str | Path, recipe: Any) -> bool:
    """True when the tool file named by ``recipe`` still matches its version digest."""
    try:
        prefix = f"{TOOLS_DIR}/"
        names = [
            token.text
            for token in recipe.argv
            if isinstance(getattr(token, "text", None), str) and token.text.startswith(prefix)
        ]
        _, separator, expected = str(recipe.version).partition("+")
        if len(names) != 1 or not separator or not expected:
            return False
        with (Path(run_cwd) / names[0]).open("rb") as handle:
            data = handle.read(MAX_TOOL_BYTES + 1)
        return hashlib.sha256(data).hexdigest()[: len(expected)] == expected
    except Exception:  # noqa: BLE001 - missing or unreadable means not verified
        return False
