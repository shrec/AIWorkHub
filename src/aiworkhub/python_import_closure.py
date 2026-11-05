"""Bounded static package-local import closure for sparse validation worktrees."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import threading
import uuid
from collections import OrderedDict, deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import worker_workspace as _worker_workspace
from .worker_workspace import (
    WorkspaceError,
    _relative_repo_path,
)


class _StatementFacts:
    """Facts derived from one walk of a module-level statement's subtree."""

    __slots__ = (
        "complex_store",
        "has_call",
        "references_import_module",
        "references_importlib",
        "stored_names",
    )

    def __init__(self) -> None:
        self.complex_store = False
        self.has_call = False
        self.references_import_module = False
        self.references_importlib = False
        self.stored_names: set[str] = set()

    def observe(self, node: ast.AST) -> None:
        if isinstance(node, ast.Call):
            self.has_call = True
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.stored_names.add(node.id)
            if node.id == "importlib":
                self.references_importlib = True
            elif node.id == "import_module":
                self.references_import_module = True
        elif isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            self.complex_store = True


# The only node types whose subtrees can contain a statement.
_STATEMENT_HOLDERS = (ast.stmt, ast.excepthandler, ast.match_case)

# NF-2026-02072: the import list of a module that binds neither ``importlib``
# nor ``import_module`` is a pure function of its bytes, so it is cached by the
# sha256 of those bytes (a changed file hashes differently and therefore always
# misses).  Records live in a bounded process-local LRU and, when the
# repository already has a real ``.aiworkhub/runtime`` directory, in one bounded
# JSON file there -- never outside the repository.  A missing, oversized,
# corrupt or concurrently replaced file degrades to parsing.
_CACHE_FILE = "import_closure_records.v1.json"
# The schema names the interpreter, so records parsed by another one never replay.
_CACHE_SCHEMA = "aiworkhub.import_closure_records.v1.py%d.%d" % sys.version_info[:2]
_CACHE_MAX_ENTRIES = 4096
_CACHE_MAX_BYTES = 16 * 1024 * 1024
_MEMORY_RECORDS: OrderedDict[str, tuple[list[Any], list[str]]] = OrderedDict()
_MEMORY_LOCK = threading.Lock()


def clear_process_import_records() -> None:
    """Drop the process-local import-record LRU (tests and diagnostics)."""

    with _MEMORY_LOCK:
        _MEMORY_RECORDS.clear()


def _import_spec(node: ast.Import | ast.ImportFrom) -> list[Any]:
    if isinstance(node, ast.Import):
        return ["i", [alias.name for alias in node.names]]
    return ["f", node.module, node.level, [alias.name for alias in node.names]]


def _stdlib_candidate_tops(tree: ast.Module) -> list[str]:
    return [
        alias.name.split(".", 1)[0]
        for statement in tree.body
        if isinstance(statement, ast.Import)
        for alias in statement.names
        if alias.asname is None
    ]


def _all_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _decode_record(raw: Any) -> tuple[list[Any], list[str]] | None:
    if not (isinstance(raw, list) and len(raw) == 2):
        return None
    specs, tops = raw
    if not (isinstance(specs, list) and _all_strings(tops)):
        return None
    for spec in specs:
        if not isinstance(spec, list):
            return None
        if len(spec) == 2 and spec[0] == "i" and _all_strings(spec[1]):
            continue
        if (
            len(spec) == 4
            and spec[0] == "f"
            and (spec[1] is None or isinstance(spec[1], str))
            and isinstance(spec[2], int)
            and not isinstance(spec[2], bool)
            and spec[2] >= 0
            and _all_strings(spec[3])
        ):
            continue
        return None
    return specs, tops


def _cache_path(repo: Path) -> Path | None:
    base = repo / ".aiworkhub"
    runtime = base / "runtime"
    path = runtime / _CACHE_FILE
    if base.is_symlink() or runtime.is_symlink() or not runtime.is_dir():
        return None
    return None if path.is_symlink() else path


def _load_disk_records(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _CACHE_MAX_BYTES:
            return {}
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        return {}
    if not (isinstance(payload, dict) and payload.get("schema") == _CACHE_SCHEMA):
        return {}
    entries = payload.get("entries")
    return entries if isinstance(entries, dict) else {}


class _ImportRecordStore:
    """Per-call view over the process LRU and the optional on-disk records."""

    def __init__(self, repo: Path, *, enabled: bool) -> None:
        self._enabled = enabled
        self._path = _cache_path(repo) if enabled else None
        self._disk: dict[str, Any] | None = None
        self._pending: dict[str, tuple[list[Any], list[str]]] = {}

    def _remember(self, digest: str, record: tuple[list[Any], list[str]]) -> None:
        with _MEMORY_LOCK:
            _MEMORY_RECORDS[digest] = record
            _MEMORY_RECORDS.move_to_end(digest)
            while len(_MEMORY_RECORDS) > _CACHE_MAX_ENTRIES:
                _MEMORY_RECORDS.popitem(last=False)

    def get(self, digest: str) -> tuple[list[Any], list[str]] | None:
        if not self._enabled:
            return None
        with _MEMORY_LOCK:
            record = _MEMORY_RECORDS.get(digest)
            if record is not None:
                _MEMORY_RECORDS.move_to_end(digest)
                return record
        if self._path is None:
            return None
        if self._disk is None:
            self._disk = _load_disk_records(self._path)
        decoded = _decode_record(self._disk.get(digest))
        if decoded is not None:
            self._remember(digest, decoded)
        return decoded

    def put(self, digest: str, record: tuple[list[Any], list[str]]) -> None:
        if not self._enabled:
            return
        self._remember(digest, record)
        if self._path is not None:
            self._pending[digest] = record

    def flush(self) -> None:
        """Merge new records into the file; any failure leaves it untouched."""

        if not self._pending or self._path is None:
            return
        merged: dict[str, Any] = {}
        for key, raw in _load_disk_records(self._path).items():
            if isinstance(key, str) and _decode_record(raw) is not None:
                merged[key] = raw
        for key, record in self._pending.items():
            merged.pop(key, None)
            merged[key] = [record[0], record[1]]
        while len(merged) > _CACHE_MAX_ENTRIES:
            del merged[next(iter(merged))]
        encoded = json.dumps(
            {"schema": _CACHE_SCHEMA, "entries": merged}, separators=(",", ":")
        ).encode("utf-8")
        self._pending.clear()
        if len(encoded) > _CACHE_MAX_BYTES:
            return
        temp = self._path.with_name(
            f"{self._path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp.write_bytes(encoded)
            os.replace(temp, self._path)
        except OSError:
            try:
                temp.unlink()
            except OSError:
                pass


def resolve_local_python_imports(
    repo: Path, seeded: Iterable[str], *, use_cache: bool = True
) -> tuple[str, ...]:
    """Return the bounded static package-local import closure for Python seeds.

    Sparse validation must import candidate modules from the detached worktree,
    not fall through to the installed/canonical package.  Follow only imports
    that resolve to regular repository files; third-party and stdlib imports
    remain external.  Relative imports are unambiguously repository-local and
    therefore fail closed when their target cannot be resolved.

    ``use_cache=False`` parses every file afresh and never reads or writes the
    import-record cache; the closure is byte-identical either way.
    """

    repo = repo.resolve()
    store = _ImportRecordStore(repo, enabled=use_cache)
    initial = sorted({_relative_repo_path(value) for value in seeded})
    roots: set[Path] = {repo}
    src_root = repo / "src"
    if src_root.is_dir() and not src_root.is_symlink():
        roots.add(src_root)

    # NF-2026-00993: every filesystem verdict below is memoised in a dict local
    # to this one call.  The tree changes between launches, so nothing outlives
    # the return; within the call each path is checked once and a refusal is
    # replayed with its original message, so fail-closed outcomes and their
    # precedence are those of the uncached checks.  Keys are exact path
    # strings, bounded by the distinct candidates this closure probes.
    init_states: dict[str, tuple[bool, bool]] = {}

    def init_state(package_init: Path) -> tuple[bool, bool]:
        key = str(package_init)
        state = init_states.get(key)
        if state is None:
            state = (package_init.is_symlink(), package_init.is_file())
            init_states[key] = state
        return state

    def package_context(relative: str) -> tuple[Path, tuple[str, ...]]:
        current = (repo / relative).parent
        parts: list[str] = []
        while current != repo and init_state(current / "__init__.py")[1]:
            parts.insert(0, current.name)
            current = current.parent
        if parts:
            roots.add(current)
        return current, tuple(parts)

    for relative in initial:
        if relative.endswith(".py") and (repo / relative).is_file():
            package_context(relative)

    candidate_verdicts: dict[str, bool | WorkspaceError] = {}
    # NF-2026-02072: ``_require_beneath`` re-walks and re-resolves every ancestor
    # for every candidate.  The same guard is replayed here with per-call memos:
    # one lstat/symlink probe per distinct path (a child of a missing directory
    # is missing without a syscall) and one resolution per distinct path, where
    # a missing path resolves as its resolved parent plus its name -- exactly
    # what a non-strict resolve returns once no component is a symlink, which
    # the component walk below has already enforced.  Existing paths are still
    # resolved in full.
    probes: dict[str, tuple[bool, bool]] = {}
    # ``repo`` was resolved on entry; seeding it keeps that the only resolution.
    resolved_paths: dict[str, Path] = {str(repo): repo}

    def probe(path: Path) -> tuple[bool, bool]:
        """Return ``(exists, is_symlink)`` for ``path``, memoised per call."""

        key = str(path)
        state = probes.get(key)
        if state is None:
            if path != repo and path.parent != path and not probe(path.parent)[0]:
                state = (False, False)
            else:
                # Only absence reads as missing: PermissionError and the like
                # propagate, as Path.is_symlink() did in the walk this replaces.
                try:
                    os.lstat(path)
                except (FileNotFoundError, NotADirectoryError):
                    state = (False, False)
                else:
                    state = (True, path.is_symlink())
            probes[key] = state
        return state

    def resolve_path(path: Path) -> Path:
        key = str(path)
        resolved = resolved_paths.get(key)
        if resolved is None:
            if path != repo and path.parent != path and not probe(path)[0]:
                resolved = resolve_path(path.parent) / path.name
            else:
                resolved = path.resolve(strict=False)
            resolved_paths[key] = resolved
        return resolved

    first_links: dict[str, Path | None] = {}

    def first_symlink(path: Path) -> Path | None:
        """Return the outermost symlink among repo-relative components of ``path``."""

        if path == repo:
            return None
        key = str(path)
        if key not in first_links:
            outer = first_symlink(path.parent)
            first_links[key] = (
                outer if outer is not None else path if probe(path)[1] else None
            )
        return first_links[key]

    repo_prefix = os.path.normcase(str(repo)).rstrip(os.sep) + os.sep

    def require_beneath(candidate: Path) -> None:
        text = str(candidate)
        if os.path.abspath(text) == text and os.path.normcase(text).startswith(
            repo_prefix
        ):
            # Already normalised and lexically beneath ``repo``: no ``..`` or
            # re-rooting, so the memoised chain checks apply directly.
            link = first_symlink(candidate)
            if link is not None:
                raise WorkspaceError(f"symlink_path_component_forbidden:{link}")
            resolved = resolve_path(candidate)
        else:
            lexical = Path(os.path.abspath(candidate))
            try:
                relative_parts = lexical.relative_to(repo).parts
            except ValueError as exc:
                raise WorkspaceError(f"path_escapes_workspace:{candidate}") from exc
            cursor = repo
            for part in relative_parts:
                cursor /= part
                if probe(cursor)[1]:
                    raise WorkspaceError(f"symlink_path_component_forbidden:{cursor}")
            resolved = candidate.resolve(strict=False)
        if resolved != repo and repo not in resolved.parents:
            raise WorkspaceError(f"path_escapes_workspace:{candidate}")

    def candidate_is_file(candidate: Path) -> bool:
        key = str(candidate)
        verdict = candidate_verdicts.get(key)
        if verdict is None:
            try:
                require_beneath(candidate)
                if probe(candidate)[1]:
                    raise WorkspaceError(
                        "validation_python_import_symlink:"
                        + candidate.relative_to(repo).as_posix()
                    )
            except WorkspaceError as exc:
                candidate_verdicts[key] = exc
                raise
            verdict = probe(candidate)[0] and candidate.is_file()
            candidate_verdicts[key] = verdict
        if isinstance(verdict, WorkspaceError):
            raise WorkspaceError(*verdict.args) from verdict
        return verdict

    # ``roots`` only ever grows, so its size identifies its exact contents.
    module_results: dict[
        tuple[tuple[str, ...], Path | None, int],
        tuple[Path, Path] | None | WorkspaceError,
    ] = {}

    def module_file(
        parts: tuple[str, ...], preferred_root: Path | None = None
    ) -> tuple[Path, Path] | None:
        if not parts:
            return None
        key = (parts, preferred_root, len(roots))
        if key in module_results:
            recorded = module_results[key]
            if isinstance(recorded, WorkspaceError):
                raise WorkspaceError(*recorded.args) from recorded
            return recorded
        ordered_roots = sorted(roots, key=lambda value: value.as_posix())
        if preferred_root is not None:
            ordered_roots = [preferred_root] + [
                root for root in ordered_roots if root != preferred_root
            ]
        result: tuple[Path, Path] | None = None
        try:
            for root in ordered_roots:
                module = root.joinpath(*parts).with_suffix(".py")
                package = root.joinpath(*parts, "__init__.py")
                if candidate_is_file(module):
                    result = root, module
                    break
                if candidate_is_file(package):
                    result = root, package
                    break
        except WorkspaceError as exc:
            module_results[key] = exc
            raise
        module_results[key] = result
        return result

    _fallback_cache: list[list[Path]] = []
    # ponytail: depth-1 directories only; deeper layouts need an explicit package root
    def fallback_dirs() -> list[Path]:
        if not _fallback_cache:
            _fallback_cache.append(sorted(
                (p for p in repo.iterdir()
                 if p.is_dir() and not p.is_symlink() and not p.name.startswith('.')
                 and p.name not in {'__pycache__', 'node_modules'} and p not in roots),
                key=lambda v: v.as_posix()))
        return _fallback_cache[0]

    def add_module(
        parts: tuple[str, ...],
        pending: list[str],
        rows: set[str],
        preferred_root: Path | None = None,
        *,
        allow_fallback: bool = False,
    ) -> bool:
        resolved = module_file(parts, preferred_root)
        if resolved is None and allow_fallback and parts and parts[0] not in sys.stdlib_module_names:
            for directory in fallback_dirs():
                try:
                    resolved = module_file(parts, directory)
                except WorkspaceError:
                    resolved = None
                    continue
                if resolved is not None:
                    break
        if resolved is None:
            return False
        root, candidate = resolved
        additions = [candidate]
        parent = candidate.parent
        while parent != root:
            package_init = parent / "__init__.py"
            init_is_symlink, init_is_file = init_state(package_init)
            if init_is_symlink:
                raise WorkspaceError(
                    "validation_python_import_symlink:"
                    + package_init.relative_to(repo).as_posix()
                )
            if not init_is_file:
                break
            additions.append(package_init)
            parent = parent.parent
        for path in additions:
            relative = _relative_repo_path(path.relative_to(repo).as_posix())
            if relative not in rows:
                rows.add(relative)
                pending.append(relative)
                # Read at call time: callers and tests patch worker_workspace.MAX_SEED_FILES.
                if len(rows) > _worker_workspace.MAX_SEED_FILES:
                    raise WorkspaceError(
                        f"seed_file_limit_exceeded:{len(rows)}"
                    )
        return True

    rows = set(initial)
    pending = [
        relative
        for relative in initial
        if relative.endswith(".py") and (repo / relative).is_file()
    ]
    parsed: set[str] = set()
    while pending:
        relative = pending.pop()
        if relative in parsed:
            continue
        parsed.add(relative)
        source_path = repo / relative
        if source_path.is_symlink() or not source_path.is_file():
            continue
        try:
            data = source_path.read_bytes()
        except OSError as exc:
            raise WorkspaceError(
                f"validation_python_import_parse_failed:{relative}"
            ) from exc
        # Content identity: an unchanged file is never re-parsed (NF-2026-02072).
        digest = hashlib.sha256(data).hexdigest()
        cached = store.get(digest)
        tree: ast.Module | None = None
        import_specs: list[Any]
        stdlib_tops: list[str]
        if cached is None:
            import_specs, stdlib_tops = [], []
            try:
                tree = ast.parse(data, filename=relative)
            except (SyntaxError, ValueError) as exc:
                raise WorkspaceError(
                    f"validation_python_import_parse_failed:{relative}"
                ) from exc
        else:
            import_specs, stdlib_tops = cached
        source_root, package_parts = package_context(relative)
        importlib_ok = False
        import_module_ok = False
        importlib_tainted = False
        importlib_aliases: set[str] = set()
        package_ok = True
        getattr_ok = True
        stdlib_bindings: set[str] = set()

        def importlib_binding(statement: ast.stmt) -> bool | None:
            canonical = False
            invalid = False
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    top_level_name = alias.name.split(".", 1)[0]
                    bound_name = alias.asname or top_level_name
                    if bound_name == "importlib":
                        canonical |= (
                            alias.asname is None and top_level_name == "importlib"
                        )
                        invalid |= (
                            alias.asname is not None
                            or top_level_name != "importlib"
                        )
            elif isinstance(statement, ast.ImportFrom):
                invalid = any(
                    (alias.asname or alias.name) == "importlib"
                    for alias in statement.names
                )
            elif isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                invalid = statement.name == "importlib"
            if invalid:
                return False
            return True if canonical else None

        def import_module_binding(statement: ast.stmt) -> bool | None:
            canonical = False
            invalid = False
            if isinstance(statement, ast.ImportFrom):
                if statement.module == "importlib" and not statement.level:
                    for alias in statement.names:
                        bound_name = alias.asname or alias.name
                        if bound_name != "import_module":
                            continue
                        canonical |= (
                            alias.name == "import_module" and alias.asname is None
                        )
                        invalid |= alias.name != "import_module"
                else:
                    invalid = any(
                        (alias.asname or alias.name) == "import_module"
                        for alias in statement.names
                    )
            elif isinstance(statement, ast.Import):
                invalid = any(
                    (alias.asname or alias.name.split(".", 1)[0]) == "import_module"
                    for alias in statement.names
                )
            elif isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                invalid = statement.name == "import_module"
            if invalid:
                return False
            return True if canonical else None

        def names(expr: ast.expr) -> tuple[str, ...]:
            if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                return (expr.value,)
            if (
                isinstance(expr, ast.IfExp)
                and package_ok
                and isinstance(expr.test, ast.Name)
                and expr.test.id == "__package__"
            ):
                value = expr.body if package_parts else expr.orelse
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    return (value.value,)
            return ()

        def package_arg_parts(expr: ast.expr | None) -> tuple[str, ...] | None:
            if isinstance(expr, ast.Name):
                return package_parts if package_ok and expr.id == "__package__" else None
            if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                parts = tuple(expr.value.split("."))
                return parts if parts and all(part.isidentifier() for part in parts) else None
            if package_ok and isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or) and len(expr.values) == 2 and isinstance(expr.values[0], ast.Name) and expr.values[0].id == "__package__" and isinstance(expr.values[1], ast.Constant):
                fallback = expr.values[1].value
                if fallback is None or package_parts:
                    return package_parts
                if isinstance(fallback, str):
                    parts = tuple(fallback.split("."))
                    return parts if parts and all(part.isidentifier() for part in parts) else None
            return None

        def add_call(call: ast.Call) -> bool:
            attribute_form = (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "import_module"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "importlib"
            )
            bare_form = (
                isinstance(call.func, ast.Name) and call.func.id == "import_module"
            )
            trusted = (importlib_ok and attribute_form) or (
                import_module_ok and bare_form
            )
            if not (
                trusted
                and len(call.args) <= 2
                and not any(kw.arg is None for kw in call.keywords)
            ):
                return False
            kwargs = {kw.arg: kw.value for kw in call.keywords}
            if (
                len(kwargs) != len(call.keywords)
                or set(kwargs) - {"name", "package"}
                or (call.args and "name" in kwargs)
                or (len(call.args) == 2 and "package" in kwargs)
            ):
                return False
            name_arg = call.args[0] if call.args else kwargs.get("name")
            if name_arg is None:
                return False
            module_names = names(name_arg)
            if not module_names:
                return False
            package_arg = call.args[1] if len(call.args) == 2 else kwargs.get("package")
            for module_name in module_names:
                level = len(module_name) - len(module_name.lstrip(".")); suffix = module_name[level:]
                suffix_parts = tuple(suffix.split(".")) if suffix else ()
                if any(not part.isidentifier() for part in suffix_parts):
                    continue
                if not level:
                    add_module(suffix_parts, pending, rows, source_root); continue
                relative_package_parts = package_arg_parts(package_arg)
                if relative_package_parts is None:
                    continue
                if not relative_package_parts or level > len(relative_package_parts):
                    raise WorkspaceError(f"validation_python_relative_import_unresolved:{relative}:level={level}")
                parts = relative_package_parts[: len(relative_package_parts) - level + 1] + suffix_parts
                if not add_module(parts, pending, rows, source_root):
                    raise WorkspaceError(f"validation_python_relative_import_unresolved:{relative}:" + ".".join(parts))
            return True

        # NF-2026-00993: walk the parsed tree at most once.  The queue mirrors
        # ast.walk(tree) breadth-first order, so ordinary imports are visited in
        # the same sequence as before (pending order, root growth and error
        # precedence are unchanged).  Statements only ever nest inside
        # statements, except handlers and match cases, so dropping every other
        # subtree keeps that relative order exactly.  The per-statement facts
        # read by the dynamic-import scan below only matter once a module-level
        # importlib/import_module binding can set importlib_ok/import_module_ok
        # (every other use is gated on those flags); only such modules pay for
        # the full walk, and each of their nodes is still visited once.
        statement_facts: list[_StatementFacts] = []
        if tree is not None:
            needs_facts = any(
                importlib_binding(statement) is not None
                or import_module_binding(statement) is not None
                for statement in tree.body
            )
            import_nodes: list[ast.Import | ast.ImportFrom] = []
            statement_facts = [_StatementFacts() for _ in tree.body]
            queue: deque[tuple[ast.AST, _StatementFacts | None]] = deque(
                (
                    child,
                    statement_facts[index]
                    if needs_facts and index < len(statement_facts)
                    else None,
                )
                for index, child in enumerate(ast.iter_child_nodes(tree))
            )
            while queue:
                node, facts = queue.popleft()
                if facts is not None:
                    facts.observe(node)
                    queue.extend(
                        (child, facts) for child in ast.iter_child_nodes(node)
                    )
                elif isinstance(node, _STATEMENT_HOLDERS):
                    queue.extend(
                        (child, None)
                        for child in ast.iter_child_nodes(node)
                        if isinstance(child, _STATEMENT_HOLDERS)
                    )
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    import_nodes.append(node)
            import_specs = [_import_spec(node) for node in import_nodes]
            stdlib_tops = _stdlib_candidate_tops(tree)
            if not needs_facts:
                store.put(digest, (import_specs, stdlib_tops))

        # Ordinary imports retain their existing transitive closure behavior.
        for spec in import_specs:
            if spec[0] == "i":
                for alias_name in spec[1]:
                    add_module(
                        tuple(alias_name.split(".")),
                        pending,
                        rows,
                        source_root,
                        allow_fallback=True,
                    )
            else:
                _kind, module_name, level, alias_names = spec
                module_parts = tuple(module_name.split(".")) if module_name else ()
                if level:
                    if not package_parts or level > len(package_parts):
                        raise WorkspaceError(f"validation_python_relative_import_unresolved:{relative}:level={level}")
                    base = package_parts[: len(package_parts) - level + 1] + module_parts
                    if module_parts and not add_module(base, pending, rows, source_root):
                        raise WorkspaceError(f"validation_python_relative_import_unresolved:{relative}:" + ".".join(base))
                else:
                    base = module_parts
                    add_module(base, pending, rows, source_root, allow_fallback=True)
                for alias_name in alias_names:
                    if alias_name != "*":
                        add_module(
                            base + tuple(alias_name.split(".")),
                            pending,
                            rows,
                            source_root,
                            allow_fallback=level == 0,
                        )

        # Dynamic imports are intentionally limited to direct canonical calls in
        # module-level statements, evaluated after an unaliased importlib import.
        for statement, facts in zip(
            tree.body if tree is not None else (), statement_facts
        ):
            if isinstance(statement, ast.ImportFrom) and any(
                alias.name == "*" for alias in statement.names
            ):
                # A star import may inject or replace either trusted binding.
                # Keep the importlib taint sticky across a later canonical reimport.
                importlib_tainted = True
                importlib_ok = False
                import_module_ok = False
                package_ok = False
                getattr_ok = False
                stdlib_bindings.clear()
                continue
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    bound_name = alias.asname or alias.name.split(".", 1)[0]
                    stdlib_bindings.discard(bound_name)
                    if (
                        alias.asname is None
                        and alias.name.split(".", 1)[0] in sys.stdlib_module_names
                        and module_file(
                            (alias.name.split(".", 1)[0],), source_root
                        )
                        is None
                    ):
                        stdlib_bindings.add(bound_name)
            elif isinstance(statement, ast.ImportFrom):
                for alias in statement.names:
                    stdlib_bindings.discard(alias.asname or alias.name)
            binding = importlib_binding(statement)
            if binding is not None:
                importlib_ok = (
                    binding
                    and not importlib_tainted
                    and module_file(("importlib",), source_root) is None
                )
                continue
            import_module_binding_result = import_module_binding(statement)
            if import_module_binding_result is not None:
                import_module_ok = (
                    import_module_binding_result
                    and not importlib_tainted
                    and module_file(("importlib",), source_root) is None
                )
                continue
            expression = (
                statement.value
                if isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign))
                else None
            )
            direct_import_module_call = isinstance(expression, ast.Call) and (
                (
                    isinstance(expression.func, ast.Attribute)
                    and expression.func.attr == "import_module"
                    and isinstance(expression.func.value, ast.Name)
                    and expression.func.value.id == "importlib"
                )
                or (
                    isinstance(expression.func, ast.Name)
                    and expression.func.id == "import_module"
                )
            )
            permitted_direct_call = bool(
                (importlib_ok or import_module_ok)
                and direct_import_module_call
                and add_call(expression)
            )
            safe_stdlib_getattr = bool(
                importlib_ok
                and getattr_ok
                and isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Name)
                and expression.func.id == "getattr"
                and len(expression.args) in (2, 3)
                and not expression.keywords
                and isinstance(expression.args[0], ast.Name)
                and expression.args[0].id in stdlib_bindings
                and isinstance(expression.args[1], ast.Constant)
                and isinstance(expression.args[1].value, str)
                and (
                    len(expression.args) == 2
                    or isinstance(expression.args[2], ast.Constant)
                )
            )
            harmless_alias = (
                importlib_ok
                and isinstance(statement, (ast.Assign, ast.AnnAssign))
                and isinstance(statement.value, ast.Name)
                and statement.value.id == "importlib"
                and (
                    (
                        isinstance(statement, ast.Assign)
                        and len(statement.targets) == 1
                        and isinstance(statement.targets[0], ast.Name)
                    )
                    or (
                        isinstance(statement, ast.AnnAssign)
                        and isinstance(statement.target, ast.Name)
                    )
                )
            )
            if harmless_alias:
                target = (
                    statement.targets[0]
                    if isinstance(statement, ast.Assign)
                    else statement.target
                )
                importlib_aliases.add(target.id)
            # Once canonical importlib trust exists, every other module-level
            # call is a sticky side-effect barrier.  This covers namespace
            # mutation APIs (including future ones) without trying to name them.
            unrecognized_call = (
                (importlib_ok or import_module_ok)
                and not permitted_direct_call
                and not safe_stdlib_getattr
                and facts.has_call
            )
            if unrecognized_call:
                importlib_ok = False
                import_module_ok = False
                package_ok = False
                getattr_ok = False
                stdlib_bindings.clear()
            references_importlib = facts.references_importlib
            references_import_module = facts.references_import_module
            complex_store = facts.complex_store
            stored_names = facts.stored_names
            if complex_store:
                importlib_tainted = True
            if "importlib" in stored_names or complex_store or (
                not permitted_direct_call
                and not harmless_alias
                and references_importlib
            ):
                importlib_ok = False
            if "import_module" in stored_names or complex_store or (
                not permitted_direct_call and references_import_module
            ):
                import_module_ok = False
            if "__package__" in stored_names or complex_store: package_ok = False
            if "getattr" in stored_names or complex_store:
                getattr_ok = False
            for bound_name in tuple(stdlib_bindings):
                if bound_name in stored_names or complex_store:
                    stdlib_bindings.discard(bound_name)
            is_type_checking_guard = (
                isinstance(statement, ast.If)
                and isinstance(statement.test, ast.Name)
                and statement.test.id == "TYPE_CHECKING"
                and not statement.orelse
            )
            if (
                isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.TryStar, ast.With, ast.AsyncWith, ast.Match))
                and not is_type_checking_guard
            ):
                importlib_ok = False
                import_module_ok = False
                package_ok = False
                getattr_ok = False
                stdlib_bindings.clear()
        if tree is None:
            # Cached record (no importlib binding): the dynamic scan above could
            # only probe stdlib-named top-level imports, in source order.
            for top_level_name in stdlib_tops:
                if top_level_name in sys.stdlib_module_names:
                    module_file((top_level_name,), source_root)
    store.flush()
    return tuple(sorted(rows))
