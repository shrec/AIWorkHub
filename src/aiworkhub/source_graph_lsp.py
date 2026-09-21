"""Bounded LSP transport and definition classification for Source Graph.

Task 2 only: stdio transport, private workspace, and fail-closed
classification. No production index writes. Call-site columns are 0-based
UTF-8 byte offsets (Task 1); convert to the negotiated LSP position
encoding at this boundary.
"""

from __future__ import annotations

import json
import os
import queue
import re
import select
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlparse

INPUT_COLUMN_UNIT = "utf8_byte"
DEFAULT_POSITION_ENCODING = "utf-16"
SUPPORTED_POSITION_ENCODINGS = ("utf-16", "utf-8", "utf-32")

REPO_INTERNAL = "repo_internal"
EXTERNAL_STDLIB = "external_stdlib"
EXTERNAL_DEPENDENCY = "external_dependency"
UNRESOLVED = "unresolved"
AMBIGUOUS = "ambiguous"
SERVER_UNAVAILABLE = "server_unavailable"
CLASSIFICATIONS = frozenset({
    REPO_INTERNAL,
    EXTERNAL_STDLIB,
    EXTERNAL_DEPENDENCY,
    UNRESOLVED,
    AMBIGUOUS,
    SERVER_UNAVAILABLE,
})

CORE_HEADROOM = 1
DEFAULT_REQUEST_TIMEOUT_S = 8.0
DEFAULT_BATCH_TIMEOUT_S = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_MAX_TOTAL_OUTPUT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_INPUT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_FILES = 4096
MAX_HEADER_BYTES = 2048
CLEANUP_GRACE_S = 0.5

EXCLUDED_WORKSPACE_DIR_NAMES = frozenset({
    ".aiworkhub",
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".claude",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".ruff_cache",
    "dist",
    "build",
    "archive",
    "logs",
    ".tmp",
    "CMakeFiles",
    ".hg",
    ".svn",
    ".cache",
    "worktrees",
})
GRAPH_DB_NAMES = frozenset({
    "source_graph.sqlite",
    "source_graph.sqlite-wal",
    "source_graph.sqlite-shm",
})
WORKSPACE_CONFIG_NAME = "pyrightconfig.json"
DEPENDENCY_PATH_PARTS = frozenset({
    "node_modules",
    "site-packages",
    "dist-packages",
})
PYRIGHT_EXCLUDE = (
    ".aiworkhub",
    "node_modules",
    ".claude",
    "**/node_modules",
    "**/__pycache__",
    "**/.aiworkhub",
    "**/source_graph.sqlite",
    "**/worktrees",
)


class LspTransportError(Exception):
    """Stdio framing, timeout, cancellation, or child-process failure."""


class LspTimeout(LspTransportError):
    pass


class LspCancelled(LspTransportError):
    pass


class LspMalformed(LspTransportError):
    pass


class LspUnbounded(LspTransportError):
    pass


@dataclass(frozen=True)
class LspServerSpec:
    command: tuple[str, ...]
    version: str
    language: str = "python"


@dataclass(frozen=True)
class IndexedSource:
    relative_path: str
    source_hash: str


@dataclass(frozen=True)
class DefinitionQuery:
    file_path: str
    source_hash: str
    line: int
    column: int


@dataclass(frozen=True)
class LspDefinitionResult:
    source_path: str
    source_hash: str
    source_line: int
    source_column: int
    target_uri: str | None
    target_range: tuple[int, int, int, int] | None
    server_command: str
    server_version: str
    config_digest: str
    classification: str
    # Non-empty only when no well-formed answer arrived (crash, timeout,
    # malformed frame, missing server). A valid null definition leaves it "".
    failure: str = ""


@dataclass(frozen=True)
class BoundedWorkspace:
    root: Path
    repo_root: Path
    included_relative_paths: tuple[str, ...]
    excluded_relative_paths: tuple[str, ...]
    config_path: Path
    config: dict[str, Any]
    config_digest: str


@dataclass(frozen=True)
class LspBatchOutcome:
    results: tuple[LspDefinitionResult, ...]
    child_pids: tuple[int, ...]
    children_reaped: bool
    position_encoding: str
    input_bytes: int
    output_bytes: int


def sha256_hex(data: bytes) -> str:
    return sha256(data).hexdigest()


def path_excluded_from_workspace(relative_path: str) -> bool:
    normalized = str(relative_path).replace("\\", "/")
    if not normalized or "\x00" in normalized:
        return True
    if normalized.startswith("/") or PureWindowsPath(normalized).drive:
        return True
    raw_parts = PurePosixPath(normalized).parts
    if not raw_parts or ".." in raw_parts:
        return True
    parts = tuple(part.casefold() for part in raw_parts)
    if any(part in EXCLUDED_WORKSPACE_DIR_NAMES for part in parts):
        return True
    return parts[-1] in GRAPH_DB_NAMES


def relative_includes_only(paths: Sequence[str]) -> tuple[str, ...]:
    trusted: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        text = str(raw).replace("\\", "/")
        if not text or Path(text).is_absolute() or text.startswith("/"):
            continue
        candidate = Path(text)
        if candidate.is_absolute() or ".." in candidate.parts:
            continue
        posix = candidate.as_posix()
        if posix in seen:
            continue
        seen.add(posix)
        trusted.append(posix)
    return tuple(trusted)


def make_pyright_config(include: Sequence[str]) -> dict[str, Any]:
    return {
        "include": list(relative_includes_only(include)),
        "exclude": list(PYRIGHT_EXCLUDE),
        "typeCheckingMode": "off",
        "venvPath": "",
        "venv": "",
    }


def lsp_process_count(
    unit_count: int,
    *,
    observed_cores: int | None = None,
) -> int:
    if unit_count <= 0:
        return 1
    cores = observed_cores if observed_cores is not None else os.cpu_count()
    cores = max(1, int(cores or 1))
    return max(1, min(int(unit_count), cores - CORE_HEADROOM))


def server_command_available(command: Sequence[str]) -> bool:
    if not command:
        return False
    executable = str(command[0])
    if not executable:
        return False
    if os.path.sep in executable or (os.path.altsep and os.path.altsep in executable):
        path = Path(executable)
        return path.is_file() and os.access(path, os.X_OK)
    found = shutil.which(executable)
    return found is not None and os.access(found, os.X_OK)


def graph_line_to_lsp(line: int) -> int:
    if line <= 0:
        raise ValueError("source line must be positive")
    return line - 1


def utf8_byte_offset_to_lsp_character(
    line_bytes: bytes,
    byte_offset: int,
    position_encoding: str,
) -> int:
    if byte_offset < 0 or byte_offset > len(line_bytes):
        raise ValueError("byte offset out of range")
    prefix = line_bytes[:byte_offset]
    encoding = position_encoding if position_encoding in SUPPORTED_POSITION_ENCODINGS else DEFAULT_POSITION_ENCODING
    if encoding == "utf-8":
        return byte_offset
    text = prefix.decode("utf-8")
    if encoding == "utf-32":
        return len(text)
    return len(text.encode("utf-16-le")) // 2


def config_digest(
    *,
    command: Sequence[str],
    version: str,
    include: Sequence[str],
    exclude: Sequence[str],
    position_encoding: str,
) -> str:
    payload = {
        "command": list(command),
        "exclude": list(exclude),
        "include": list(include),
        "position_encoding": position_encoding,
        "version": version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_hex(encoded)


def finalize_results(
    results: Sequence[LspDefinitionResult],
) -> tuple[LspDefinitionResult, ...]:
    return tuple(sorted(
        results,
        key=lambda item: (
            item.source_path,
            item.source_line,
            item.source_column,
            item.source_hash,
            item.classification,
            item.target_uri or "",
        ),
    ))


def _posix_relative(relative_path: str) -> str:
    return Path(str(relative_path).replace("\\", "/")).as_posix()


def _safe_repo_file(repo_root: Path, relative_path: str) -> Path | None:
    if path_excluded_from_workspace(relative_path):
        return None
    root = repo_root.resolve()
    try:
        resolved = (root / _posix_relative(relative_path)).resolve()
        inside = resolved.relative_to(root).as_posix()
        if path_excluded_from_workspace(inside) or not resolved.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def _is_junction(path: Path) -> bool:
    check = getattr(os.path, "isjunction", None)
    return bool(check(path)) if callable(check) else False


def _workspace_directory(
    workspace_root: Path,
    parts: Sequence[str],
    *,
    create: bool,
) -> Path | None:
    """Resolve ``parts`` below the workspace without ever crossing a link."""
    current = workspace_root
    for part in parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if not create:
                return None
            try:
                current.mkdir()
                mode = current.lstat().st_mode
            except OSError:
                return None
        except OSError:
            return None
        if not stat.S_ISDIR(mode) or _is_junction(current):
            return None
    return current


def _write_new_file(path: Path, payload: bytes) -> None:
    # Replace rather than truncate so an existing symlink or hard link is unlinked, never written through.
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as handle:
        handle.write(payload)


def _store_workspace_file(workspace_root: Path, relative_path: str, payload: bytes) -> bool:
    parts = PurePosixPath(relative_path).parts
    directory = _workspace_directory(workspace_root, parts[:-1], create=True)
    if directory is None:
        return False
    target = directory / parts[-1]
    try:
        _write_new_file(target, payload)
    except OSError:
        try:
            target.unlink()
        except OSError:
            pass
        return False
    return True


def _read_workspace_file(workspace_root: Path, relative_path: str) -> bytes | None:
    rel = _posix_relative(relative_path)
    if path_excluded_from_workspace(rel):
        return None
    parts = PurePosixPath(rel).parts
    directory = _workspace_directory(workspace_root, parts[:-1], create=False)
    if directory is None:
        return None
    path = directory / parts[-1]
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        with os.fdopen(os.open(path, flags), "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _reject_linked_destination(dest: Path) -> None:
    """Refuse a destination whose existing components cross a symlink/junction.

    ``dest.resolve()`` follows links, so a linked destination or parent would
    redirect every workspace write. Walk the lexical absolute path with
    ``lstat`` and reject any existing link component; missing trailing
    components are created later as ordinary directories.
    """
    lexical = Path(os.path.abspath(os.fspath(dest)))
    parts = lexical.parts
    if not parts:
        raise ValueError("private workspace destination is empty")
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return
        except OSError:
            raise ValueError("private workspace destination is inaccessible") from None
        if stat.S_ISLNK(mode) or _is_junction(current):
            raise ValueError(
                "private workspace destination must not cross a symlink or junction",
            )


def build_bounded_workspace(
    repo_root: Path,
    indexed_sources: Sequence[IndexedSource],
    dest: Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    server_spec: LspServerSpec | None = None,
    position_encoding: str = "",
) -> BoundedWorkspace:
    root = repo_root.resolve()
    _reject_linked_destination(dest)
    workspace = dest.resolve()
    if root.is_relative_to(workspace):
        raise ValueError("private workspace must not be, or contain, the repository root")
    if workspace.is_relative_to(root) and not path_excluded_from_workspace(
        workspace.relative_to(root).as_posix(),
    ):
        raise ValueError(
            "private workspace inside the repository must live in an excluded directory",
        )
    workspace.mkdir(parents=True, exist_ok=True)
    included: list[str] = []
    excluded: list[str] = []
    seen: set[str] = set()
    ordered = sorted(indexed_sources, key=lambda item: _posix_relative(item.relative_path))
    for source in ordered:
        rel = _posix_relative(source.relative_path)
        if rel in seen:
            continue
        seen.add(rel)
        if (
            rel == WORKSPACE_CONFIG_NAME
            or path_excluded_from_workspace(rel)
            or len(included) >= max_files
        ):
            excluded.append(rel)
            continue
        disk = _safe_repo_file(root, rel)
        if disk is None:
            excluded.append(rel)
            continue
        try:
            payload = disk.read_bytes()
        except OSError:
            excluded.append(rel)
            continue
        if sha256_hex(payload) != source.source_hash:
            excluded.append(rel)
            continue
        if not _store_workspace_file(workspace, rel, payload):
            excluded.append(rel)
            continue
        included.append(rel)
    include_roots = _include_roots(included)
    config = make_pyright_config(include_roots)
    config_path = workspace / WORKSPACE_CONFIG_NAME
    _write_new_file(
        config_path,
        (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    spec = server_spec or LspServerSpec(command=(), version="", language="python")
    digest = config_digest(
        command=spec.command,
        version=spec.version,
        include=config["include"],
        exclude=config["exclude"],
        position_encoding=position_encoding,
    )
    return BoundedWorkspace(
        root=workspace,
        repo_root=root,
        included_relative_paths=tuple(included),
        excluded_relative_paths=tuple(excluded),
        config_path=config_path,
        config=config,
        config_digest=digest,
    )


def _include_roots(relative_paths: Sequence[str]) -> tuple[str, ...]:
    roots: list[str] = []
    seen: set[str] = set()
    for rel in relative_paths:
        parts = Path(rel).parts
        name = parts[0] if parts else rel
        if name in seen:
            continue
        seen.add(name)
        roots.append(name)
    return tuple(roots)


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _uri_to_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or not parsed.path:
        return None
    path = unquote(parsed.path)
    netloc = unquote(parsed.netloc)
    if netloc and netloc.lower() != "localhost":
        return Path(f"//{netloc}{path}")
    if (
        len(path) >= 3
        and path[0] == "/"
        and path[2] == ":"
        and "A" <= path[1].upper() <= "Z"
        and (len(path) == 3 or path[3] == "/")
    ):
        return Path(path[1:])
    return Path(path)


def _line_bytes(content: bytes, lsp_line: int) -> bytes | None:
    lines = content.split(b"\n")
    if lsp_line < 0 or lsp_line >= len(lines):
        return None
    return lines[lsp_line].rstrip(b"\r")


def _language_id(language: str, relative_path: str) -> str:
    suffix = Path(relative_path).suffix.casefold()
    if language in {"javascript", "typescript"}:
        if suffix in {".ts", ".tsx"}:
            return "typescript"
        return "javascript"
    return "python"


def _range_tuple(range_obj: Mapping[str, Any] | None) -> tuple[int, int, int, int] | None:
    if not isinstance(range_obj, Mapping):
        return None
    start = range_obj.get("start")
    end = range_obj.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        return None
    values = (
        start.get("line"),
        start.get("character"),
        end.get("line"),
        end.get("character"),
    )
    if any(type(value) is not int or value < 0 for value in values):
        return None
    line, character, end_line, end_character = values
    if (end_line, end_character) < (line, character):
        return None
    return line, character, end_line, end_character


def _normalize_locations(payload: Any) -> list[tuple[str, tuple[int, int, int, int]]]:
    if isinstance(payload, Mapping):
        items: list[Any] = [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    found: list[tuple[str, tuple[int, int, int, int]]] = []
    for item in items:
        if not isinstance(item, Mapping):
            return []
        if "targetUri" in item:
            uri = item.get("targetUri")
            span = _range_tuple(item.get("targetSelectionRange")) or _range_tuple(
                item.get("targetRange"),
            )
        else:
            uri = item.get("uri")
            span = _range_tuple(item.get("range"))
        # One malformed entry makes the whole response untrustworthy.
        if not isinstance(uri, str) or not uri or span is None:
            return []
        found.append((uri, span))
    return found


def _unique_targets(
    locations: Sequence[tuple[str, tuple[int, int, int, int] | None]],
) -> list[tuple[str, tuple[int, int, int, int] | None]]:
    seen: set[tuple[str, tuple[int, int, int, int] | None]] = set()
    unique: list[tuple[str, tuple[int, int, int, int] | None]] = []
    for item in locations:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


_PYTHON_LIB_DIR = re.compile(r"python\d+(?:\.\d+)*[dmtu]*")
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")


def _layout_parts(path: PurePath | str) -> tuple[str, ...]:
    text = str(path).replace("\\", "/")
    return tuple(part for part in text.split("/") if part and part != ".")


def _windows_style(path: PurePath | str) -> bool:
    text = str(path)
    return "\\" in text or text.startswith("//") or _DRIVE_PREFIX.match(text) is not None


# Recognises POSIX lib/pythonX.Y and Windows PythonX.Y/Lib; dependency directories win.
def _external_classification(resolved: PurePath | str) -> str:
    raw = _layout_parts(resolved)
    parts = tuple(part.casefold() for part in raw)
    pairs = tuple(zip(parts, parts[1:]))
    if any(head.startswith("typeshed") and tail == "stdlib" for head, tail in pairs):
        return EXTERNAL_STDLIB
    if DEPENDENCY_PATH_PARTS.intersection(parts):
        return EXTERNAL_DEPENDENCY
    if any(
        head in {"lib", "lib64"} and _PYTHON_LIB_DIR.fullmatch(tail)
        for head, tail in pairs
    ):
        return EXTERNAL_STDLIB
    if _windows_style(resolved) and "lib" in parts[:-1]:
        return EXTERNAL_STDLIB
    if any(_PYTHON_LIB_DIR.fullmatch(head) and tail == "lib" for head, tail in pairs):
        return EXTERNAL_STDLIB
    return EXTERNAL_DEPENDENCY


def classify_definition_payload(
    payload: Any,
    *,
    query: DefinitionQuery,
    repo_root: Path,
    workspace: BoundedWorkspace,
    indexed_hashes: Mapping[str, str],
    source_bytes: bytes | None,
    server_spec: LspServerSpec,
    config_digest_value: str,
    unbounded: bool = False,
) -> LspDefinitionResult:
    command_text = " ".join(server_spec.command)
    base = dict(
        source_path=query.file_path,
        source_hash=query.source_hash,
        source_line=query.line,
        source_column=query.column,
        server_command=command_text,
        server_version=server_spec.version,
        config_digest=config_digest_value,
    )

    def finish(
        classification: str,
        uri: str | None = None,
        span: tuple[int, int, int, int] | None = None,
        failure: str = "",
    ) -> LspDefinitionResult:
        if unbounded or classification not in CLASSIFICATIONS:
            classification = UNRESOLVED
            uri = None
            span = None
        if classification == AMBIGUOUS:
            uri = None
            span = None
        return LspDefinitionResult(
            target_uri=uri,
            target_range=span,
            classification=classification,
            failure=failure,
            **base,
        )

    if unbounded:
        return finish(UNRESOLVED)
    source_rel = _posix_relative(query.file_path)
    expected = indexed_hashes.get(source_rel)
    if expected is None or expected != query.source_hash:
        return finish(UNRESOLVED)
    if source_bytes is not None and sha256_hex(source_bytes) != query.source_hash:
        return finish(UNRESOLVED)

    locations = _unique_targets(_normalize_locations(payload))
    if not locations:
        if payload is None or payload == []:
            return finish(UNRESOLVED)
        # A well-framed answer whose payload is no Location list is not a
        # server that answered "no definition": callers must never cache it.
        return finish(UNRESOLVED, failure="malformed_result")
    if len(locations) > 1:
        return finish(AMBIGUOUS)

    uri, span = locations[0]
    target = _uri_to_path(uri)
    # A drive-letter URI on a POSIX host parses to a relative path; never resolve it against the cwd.
    if target is None or not target.is_absolute():
        return finish(UNRESOLVED, uri, span)

    repo = repo_root.resolve()
    private = workspace.root.resolve()
    try:
        resolved = target.resolve()
        target_is_file = resolved.is_file()
    except (OSError, RuntimeError, ValueError):
        return finish(UNRESOLVED, uri, span)
    if not target_is_file:
        return finish(UNRESOLVED, uri, span)

    if resolved.is_relative_to(private):
        rel = resolved.relative_to(private).as_posix()
    elif resolved.is_relative_to(repo):
        rel = resolved.relative_to(repo).as_posix()
    else:
        return finish(_external_classification(resolved), uri, span)

    if path_excluded_from_workspace(rel):
        if Path(rel).name in GRAPH_DB_NAMES or ".aiworkhub" in Path(rel).parts:
            return finish(UNRESOLVED, uri, span)
        return finish(_external_classification(resolved), uri, span)
    expected_hash = indexed_hashes.get(rel)
    if expected_hash is None:
        return finish(UNRESOLVED, uri, span)
    try:
        disk_hash = sha256_hex(resolved.read_bytes())
    except OSError:
        return finish(UNRESOLVED, uri, span)
    if disk_hash != expected_hash:
        return finish(UNRESOLVED, uri, span)
    return finish(REPO_INTERNAL, uri, span)


def _result_for_status(
    query: DefinitionQuery,
    spec: LspServerSpec,
    digest: str,
    classification: str,
    *,
    failure: str = "",
) -> LspDefinitionResult:
    return LspDefinitionResult(
        source_path=query.file_path,
        source_hash=query.source_hash,
        source_line=query.line,
        source_column=query.column,
        target_uri=None,
        target_range=None,
        server_command=" ".join(spec.command),
        server_version=spec.version,
        config_digest=digest,
        classification=classification,
        failure=failure,
    )


def _encoded_lsp_frame(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
    return header + raw


class LspStdioSession:
    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        *,
        max_input_bytes: int,
        max_output_bytes: int,
        max_total_output_bytes: int,
        request_timeout_s: float,
        batch_deadline: float,
        cancel_event: threading.Event | None,
    ) -> None:
        self.proc = proc
        self.max_input_bytes = max_input_bytes
        self.max_output_bytes = max_output_bytes
        self.max_total_output_bytes = max_total_output_bytes
        self.request_timeout_s = request_timeout_s
        self.batch_deadline = batch_deadline
        self.cancel_event = cancel_event
        self.output_bytes = 0
        self.input_bytes = 0
        self._next_id = 1
        self._pending: dict[int, dict[str, Any] | None] = {}
        self._notifications: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._reader_error: BaseException | None = None
        self._write_error: BaseException | None = None
        self._write_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=1)
        self._writes_done = 0
        self._write_generation = 0
        self._reader = threading.Thread(target=self._read_loop, name="lsp-stdio", daemon=True)
        self._writer = threading.Thread(target=self._write_loop, name="lsp-stdio-w", daemon=True)
        self._reader.start()
        self._writer.start()

    @property
    def pid(self) -> int | None:
        return self.proc.pid

    def next_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def start_request(self, method: str, params: Any) -> int:
        ident = self.next_id()
        with self._cv:
            self._pending[ident] = None
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        return ident

    def request(self, method: str, params: Any) -> dict[str, Any]:
        return self._wait_response(self.start_request(method, params))

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, payload: dict[str, Any]) -> None:
        self._raise_if_cancelled()
        blob = _encoded_lsp_frame(payload)
        if self.input_bytes + len(blob) > self.max_input_bytes:
            raise LspUnbounded("input exceeded bound")
        deadline = min(self.batch_deadline, time.monotonic() + self.request_timeout_s)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_group(self.proc)
            raise LspTimeout("write timeout")
        with self._cv:
            if self._write_error is not None:
                raise self._write_error
            self._write_generation += 1
            generation = self._write_generation
        try:
            self._write_queue.put(blob, timeout=remaining)
        except queue.Full:
            _terminate_group(self.proc)
            raise LspTimeout("write timeout") from None
        with self._cv:
            while True:
                try:
                    self._raise_if_cancelled()
                except (LspCancelled, LspTimeout):
                    _terminate_group(self.proc)
                    raise
                if self._write_error is not None:
                    raise self._write_error
                if self._writes_done >= generation:
                    self.input_bytes += len(blob)
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_group(self.proc)
                    raise LspTimeout("write timeout")
                self._cv.wait(timeout=min(0.05, remaining))

    def _write_loop(self) -> None:
        try:
            while True:
                item = self._write_queue.get()
                if item is None:
                    return
                stdin = self.proc.stdin
                if stdin is None:
                    raise LspTransportError("stdio closed")
                try:
                    stdin.write(item)
                    stdin.flush()
                except BrokenPipeError as exc:
                    raise LspTransportError("stdio closed") from exc
                with self._cv:
                    self._writes_done += 1
                    self._cv.notify_all()
        except Exception as exc:
            wrapped = exc if isinstance(exc, LspTransportError) else LspTransportError("stdio closed")
            with self._cv:
                self._write_error = wrapped
                self._cv.notify_all()

    def _wait_response(self, ident: int) -> dict[str, Any]:
        deadline = min(self.batch_deadline, time.monotonic() + self.request_timeout_s)
        with self._cv:
            while True:
                self._raise_if_cancelled()
                if self._reader_error is not None:
                    raise self._reader_error
                message = self._pending.get(ident)
                if message is not None:
                    del self._pending[ident]
                    return message
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LspTimeout(f"timed out waiting for response {ident}")
                self._cv.wait(timeout=min(0.05, remaining))

    def _raise_if_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise LspCancelled("cancelled")
        if time.monotonic() >= self.batch_deadline:
            raise LspTimeout("batch deadline")

    def _read_loop(self) -> None:
        try:
            stdout = self.proc.stdout
            if stdout is None:
                raise LspTransportError("stdio closed")
            while True:
                message = self._read_message(stdout)
                if message is None:
                    break
                ident = message.get("id")
                with self._cv:
                    # Server-initiated requests have a method; only a method-less message answers ours.
                    matched = (
                        type(ident) is int
                        and "method" not in message
                        and ident in self._pending
                    )
                    if matched:
                        self._pending[ident] = message
                    else:
                        self._notifications.append(message)
                    self._cv.notify_all()
        except BaseException as exc:
            with self._cv:
                self._reader_error = exc
                self._cv.notify_all()

    def _read_message(self, stdout: Any) -> dict[str, Any] | None:
        header = bytearray()
        while b"\r\n\r\n" not in header:
            self._raise_if_cancelled()
            if self.output_bytes + 1 > self.max_total_output_bytes:
                raise LspUnbounded("output exceeded bound")
            chunk = self._read_ready(stdout, 1)
            if chunk is None:
                return None
            header.extend(chunk)
            self.output_bytes += 1
            if len(header) > MAX_HEADER_BYTES:
                raise LspMalformed("LSP header exceeded bound")
        try:
            header_text = bytes(header).decode("ascii")
        except UnicodeDecodeError as exc:
            raise LspMalformed("non-ASCII LSP header bytes") from exc
        fields = {}
        for line in header_text.split("\r\n"):
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.strip().casefold()] = value.strip()
        if "content-length" not in fields:
            raise LspMalformed("missing Content-Length")
        try:
            length = int(fields["content-length"])
        except ValueError as exc:
            raise LspMalformed("invalid Content-Length") from exc
        if length < 0 or length > self.max_output_bytes:
            raise LspUnbounded("Content-Length exceeded bound")
        if self.output_bytes + length > self.max_total_output_bytes:
            raise LspUnbounded("output exceeded bound")
        body = bytearray()
        while len(body) < length:
            self._raise_if_cancelled()
            piece = self._read_ready(stdout, length - len(body))
            if piece is None:
                raise LspMalformed("truncated LSP body")
            body.extend(piece)
        self.output_bytes += len(body)
        try:
            parsed = json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LspMalformed("invalid LSP JSON") from exc
        if not isinstance(parsed, dict):
            raise LspMalformed("LSP payload is not an object")
        return parsed

    def _read_ready(self, stdout: Any, size: int) -> bytes | None:
        remaining = min(self.batch_deadline, time.monotonic() + self.request_timeout_s) - time.monotonic()
        if remaining <= 0:
            raise LspTimeout("read timeout")
        if not _wait_stdio_readable(stdout, remaining):
            raise LspTimeout("read timeout")
        try:
            data = os.read(stdout.fileno(), size)
        except OSError as exc:
            raise LspTransportError("stdio closed") from exc
        if not data:
            return None
        return data

    def close(self, *, kill: bool) -> None:
        if kill or self.proc.poll() is None:
            _terminate_group(self.proc)
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self._write_queue.put_nowait(None)
        except queue.Full:
            pass
        self._writer.join(timeout=CLEANUP_GRACE_S)
        self._reader.join(timeout=CLEANUP_GRACE_S)


def _wait_stdio_readable(stream: Any, timeout: float) -> bool:
    try:
        ready, _, _ = select.select([stream], [], [], timeout)
    except (OSError, ValueError, TypeError):
        return True
    return bool(ready)


def _signal_group(proc: subprocess.Popen[bytes], *, graceful: bool) -> None:
    pid = proc.pid
    killpg = getattr(os, "killpg", None)
    if graceful:
        posix_sig = signal.SIGTERM
        fallback = proc.terminate
    else:
        posix_sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        fallback = proc.kill
    if pid is not None and callable(killpg):
        try:
            killpg(pid, posix_sig)
            return
        except (OSError, ProcessLookupError, AttributeError):
            pass
    try:
        fallback()
    except OSError:
        pass


def _terminate_group(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    _signal_group(proc, graceful=True)
    try:
        proc.wait(timeout=CLEANUP_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc, graceful=False)
    try:
        proc.wait(timeout=CLEANUP_GRACE_S)
    except subprocess.TimeoutExpired:
        pass


def _start_session(
    spec: LspServerSpec,
    workspace: BoundedWorkspace,
    *,
    env: Mapping[str, str] | None,
    max_input_bytes: int,
    max_output_bytes: int,
    max_total_output_bytes: int,
    request_timeout_s: float,
    batch_deadline: float,
    cancel_event: threading.Event | None,
) -> LspStdioSession:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = subprocess.Popen(
        list(spec.command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=str(workspace.root),
        env=merged,
        start_new_session=True,
    )
    return LspStdioSession(
        proc,
        max_input_bytes=max_input_bytes,
        max_output_bytes=max_output_bytes,
        max_total_output_bytes=max_total_output_bytes,
        request_timeout_s=request_timeout_s,
        batch_deadline=batch_deadline,
        cancel_event=cancel_event,
    )


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class _SessionOutcome:
    results: tuple[LspDefinitionResult, ...]
    pids: tuple[int, ...]
    encoding: str
    input_bytes: int
    output_bytes: int
    reaped: bool


def _partition_queries_by_file(
    queries: Sequence[DefinitionQuery],
    shard_count: int,
) -> tuple[tuple[DefinitionQuery, ...], ...]:
    """Split queries into `shard_count` file-disjoint, order-stable shards."""
    if shard_count <= 1:
        return (tuple(queries),)
    files: list[str] = []
    by_file: dict[str, list[DefinitionQuery]] = {}
    for query in queries:
        rel = _posix_relative(query.file_path)
        if rel not in by_file:
            by_file[rel] = []
            files.append(rel)
        by_file[rel].append(query)
    buckets: list[list[DefinitionQuery]] = [[] for _ in range(shard_count)]
    for index, rel in enumerate(files):
        buckets[index % shard_count].extend(by_file[rel])
    return tuple(tuple(bucket) for bucket in buckets)


def _run_session(
    *,
    repo_root: Path,
    workspace: BoundedWorkspace,
    spec: LspServerSpec,
    queries: Sequence[DefinitionQuery],
    hashes: Mapping[str, str],
    base_digest: str,
    request_timeout_s: float,
    batch_deadline: float,
    max_output_bytes: int,
    max_total_output_bytes: int,
    max_input_bytes: int,
    cancel_event: threading.Event | None,
    env: Mapping[str, str] | None,
) -> _SessionOutcome:
    """Run one LSP server session over `queries`; always returns a fail-closed outcome."""
    digest = base_digest
    session: LspStdioSession | None = None
    pids: list[int] = []
    encoding = DEFAULT_POSITION_ENCODING
    results: list[LspDefinitionResult] = []
    failed: str | None = None
    ordered = tuple(sorted(
        queries,
        key=lambda item: (item.file_path, item.line, item.column, item.source_hash),
    ))
    try:
        session = _start_session(
            spec,
            workspace,
            env=env,
            max_input_bytes=max_input_bytes,
            max_output_bytes=max_output_bytes,
            max_total_output_bytes=max_total_output_bytes,
            request_timeout_s=request_timeout_s,
            batch_deadline=batch_deadline,
            cancel_event=cancel_event,
        )
        if session.pid is not None:
            pids.append(session.pid)
        init = session.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": _file_uri(workspace.root),
                "capabilities": {
                    "general": {"positionEncodings": list(SUPPORTED_POSITION_ENCODINGS)},
                },
                "workspaceFolders": [{
                    "uri": _file_uri(workspace.root),
                    "name": "bounded-source-graph",
                }],
            },
        )
        result = init.get("result")
        if isinstance(result, Mapping):
            capabilities = result.get("capabilities")
            if isinstance(capabilities, Mapping):
                negotiated = capabilities.get("positionEncoding")
                if isinstance(negotiated, str) and negotiated in SUPPORTED_POSITION_ENCODINGS:
                    encoding = negotiated
        digest = config_digest(
            command=spec.command,
            version=spec.version,
            include=workspace.config.get("include", []),
            exclude=workspace.config.get("exclude", []),
            position_encoding=encoding,
        )
        session.notify("initialized", {})
        included = frozenset(workspace.included_relative_paths)
        opened: set[str] = set()
        contents: dict[str, bytes] = {}
        for query in ordered:
            rel = _posix_relative(query.file_path)
            if rel in opened or rel not in included:
                continue
            path = workspace.root / rel
            payload = _read_workspace_file(workspace.root, rel)
            if payload is None:
                continue
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                continue
            notify_params = {
                "textDocument": {
                    "uri": _file_uri(path),
                    "languageId": _language_id(spec.language, rel),
                    "version": 1,
                    "text": text,
                },
            }
            frame = _encoded_lsp_frame({
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": notify_params,
            })
            if session.input_bytes + len(frame) > max_input_bytes:
                continue
            contents[rel] = payload
            session.notify("textDocument/didOpen", notify_params)
            opened.add(rel)
        inflight: list[tuple[DefinitionQuery, int | None, bytes | None]] = []
        for query in ordered:
            rel = _posix_relative(query.file_path)
            source_bytes = contents.get(rel)
            if source_bytes is None:
                inflight.append((query, None, None))
                continue
            try:
                lsp_line = graph_line_to_lsp(query.line)
                line = _line_bytes(source_bytes, lsp_line)
                if line is None:
                    raise ValueError("line out of range")
                character = utf8_byte_offset_to_lsp_character(line, query.column, encoding)
            except (ValueError, UnicodeDecodeError):
                inflight.append((query, None, source_bytes))
                continue
            try:
                ident = session.start_request(
                    "textDocument/definition",
                    {
                        "textDocument": {"uri": _file_uri(workspace.root / rel)},
                        "position": {"line": lsp_line, "character": character},
                    },
                )
            except LspUnbounded:
                inflight.append((query, None, source_bytes))
                continue
            inflight.append((query, ident, source_bytes))
        # Every branch that did not receive a well-formed answer names its
        # ``failure``: a crash, timeout or malformed frame is not a server that
        # answered "no definition", and callers must never cache it as one.
        for query, ident, source_bytes in inflight:
            if ident is None or failed is not None:
                results.append(_result_for_status(
                    query, spec, digest, UNRESOLVED,
                    failure="not_sent" if failed is None else f"aborted_{failed}",
                ))
                continue
            try:
                response = session._wait_response(ident)
            except LspCancelled:
                failed = "cancelled"
                results.append(_result_for_status(
                    query, spec, digest, UNRESOLVED, failure=failed,
                ))
                continue
            except LspTimeout:
                failed = "timeout"
                results.append(_result_for_status(
                    query, spec, digest, UNRESOLVED, failure=failed,
                ))
                continue
            except LspUnbounded:
                failed = "unbounded"
                results.append(_result_for_status(
                    query, spec, digest, UNRESOLVED, failure=failed,
                ))
                continue
            except LspMalformed:
                failed = "malformed"
                results.append(_result_for_status(
                    query, spec, digest, UNRESOLVED, failure=failed,
                ))
                continue
            if response.get("id") != ident or "error" in response:
                results.append(_result_for_status(
                    query, spec, digest, UNRESOLVED, failure="server_error",
                ))
                continue
            results.append(classify_definition_payload(
                response.get("result"),
                query=query,
                repo_root=repo_root,
                workspace=workspace,
                indexed_hashes=hashes,
                source_bytes=source_bytes,
                server_spec=spec,
                config_digest_value=digest,
            ))
        if failed is None:
            try:
                session.request("shutdown", None)
                session.notify("exit", None)
                if session.proc.stdin is not None:
                    session.proc.stdin.close()
                session.proc.wait(timeout=CLEANUP_GRACE_S)
            except (LspTransportError, subprocess.TimeoutExpired):
                failed = UNRESOLVED
    except LspTransportError:
        for query in ordered[len(results):]:
            results.append(_result_for_status(
                query, spec, digest, UNRESOLVED, failure="transport",
            ))
    finally:
        if session is not None:
            session.close(kill=True)
            if session.pid is not None and session.pid not in pids:
                pids.append(session.pid)
    reaped = all(not _pid_alive(pid) for pid in pids)
    return _SessionOutcome(
        results=tuple(results),
        pids=tuple(pids),
        encoding=encoding,
        input_bytes=session.input_bytes if session is not None else 0,
        output_bytes=session.output_bytes if session is not None else 0,
        reaped=reaped,
    )


def resolve_definitions(
    *,
    repo_root: Path,
    workspace: BoundedWorkspace,
    spec: LspServerSpec,
    queries: Sequence[DefinitionQuery],
    indexed_hashes: Mapping[str, str] | None = None,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    batch_timeout_s: float = DEFAULT_BATCH_TIMEOUT_S,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_total_output_bytes: int = DEFAULT_MAX_TOTAL_OUTPUT_BYTES,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
    cancel_event: threading.Event | None = None,
    env: Mapping[str, str] | None = None,
    observed_cores: int | None = None,
) -> LspBatchOutcome:
    if not queries:
        return LspBatchOutcome(
            results=(),
            child_pids=(),
            children_reaped=True,
            position_encoding="",
            input_bytes=0,
            output_bytes=0,
        )
    hashes = {
        _posix_relative(key): value
        for key, value in dict(indexed_hashes or {}).items()
    }
    if not hashes:
        for rel in workspace.included_relative_paths:
            payload = _read_workspace_file(workspace.root, rel)
            if payload is not None:
                hashes[rel] = sha256_hex(payload)
    base_digest = config_digest(
        command=spec.command,
        version=spec.version,
        include=workspace.config.get("include", []),
        exclude=workspace.config.get("exclude", []),
        position_encoding="",
    )
    ordered_queries = tuple(sorted(
        queries,
        key=lambda item: (item.file_path, item.line, item.column, item.source_hash),
    ))
    if not server_command_available(spec.command):
        results = [
            _result_for_status(
                query, spec, base_digest, SERVER_UNAVAILABLE,
                failure=SERVER_UNAVAILABLE,
            )
            for query in ordered_queries
        ]
        return LspBatchOutcome(
            results=finalize_results(results),
            child_pids=(),
            children_reaped=True,
            position_encoding="",
            input_bytes=0,
            output_bytes=0,
        )
    distinct_files = len({_posix_relative(query.file_path) for query in ordered_queries})
    server_count = lsp_process_count(distinct_files, observed_cores=observed_cores)
    shards = _partition_queries_by_file(ordered_queries, server_count)
    batch_deadline = time.monotonic() + batch_timeout_s
    per_shard: list[_SessionOutcome | None] = [None] * len(shards)

    def _run_one(index: int, shard: tuple[DefinitionQuery, ...]) -> None:
        try:
            per_shard[index] = _run_session(
                repo_root=repo_root,
                workspace=workspace,
                spec=spec,
                queries=shard,
                hashes=hashes,
                base_digest=base_digest,
                request_timeout_s=request_timeout_s,
                batch_deadline=batch_deadline,
                max_output_bytes=max_output_bytes,
                max_total_output_bytes=max_total_output_bytes,
                max_input_bytes=max_input_bytes,
                cancel_event=cancel_event,
                env=env,
            )
        except Exception:
            # Fail closed: an unexpected worker error resolves its shard as
            # unresolved rather than failing the whole batch.
            per_shard[index] = _SessionOutcome(
                results=tuple(
                    _result_for_status(
                        query, spec, base_digest, UNRESOLVED, failure="worker_error",
                    )
                    for query in shard
                ),
                pids=(),
                encoding=DEFAULT_POSITION_ENCODING,
                input_bytes=0,
                output_bytes=0,
                reaped=True,
            )

    workers = [
        threading.Thread(target=_run_one, args=(index, shard), name=f"lsp-shard-{index}")
        for index, shard in enumerate(shards)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    merged: list[LspDefinitionResult] = []
    pids: list[int] = []
    encoding = DEFAULT_POSITION_ENCODING
    input_bytes = 0
    output_bytes = 0
    reaped = True
    for outcome in per_shard:
        if outcome is None:
            continue
        merged.extend(outcome.results)
        pids.extend(outcome.pids)
        input_bytes += outcome.input_bytes
        output_bytes += outcome.output_bytes
        reaped = reaped and outcome.reaped
        if encoding == DEFAULT_POSITION_ENCODING and outcome.encoding != DEFAULT_POSITION_ENCODING:
            encoding = outcome.encoding
    return LspBatchOutcome(
        results=finalize_results(merged),
        child_pids=tuple(pids),
        children_reaped=reaped,
        position_encoding=encoding,
        input_bytes=input_bytes,
        output_bytes=output_bytes,
    )
