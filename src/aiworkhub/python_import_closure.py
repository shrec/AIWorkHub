"""Bounded static package-local import closure for sparse validation worktrees."""

from __future__ import annotations

import ast
import sys
from collections import deque
from collections.abc import Iterable
from pathlib import Path

from . import worker_workspace as _worker_workspace
from .worker_workspace import (
    WorkspaceError,
    _relative_repo_path,
    _require_beneath,
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


def resolve_local_python_imports(repo: Path, seeded: Iterable[str]) -> tuple[str, ...]:
    """Return the bounded static package-local import closure for Python seeds.

    Sparse validation must import candidate modules from the detached worktree,
    not fall through to the installed/canonical package.  Follow only imports
    that resolve to regular repository files; third-party and stdlib imports
    remain external.  Relative imports are unambiguously repository-local and
    therefore fail closed when their target cannot be resolved.
    """

    repo = repo.resolve()
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

    def candidate_is_file(candidate: Path) -> bool:
        key = str(candidate)
        verdict = candidate_verdicts.get(key)
        if verdict is None:
            try:
                _require_beneath(repo, candidate, root_resolved=True)
                if candidate.is_symlink():
                    raise WorkspaceError(
                        "validation_python_import_symlink:"
                        + candidate.relative_to(repo).as_posix()
                    )
            except WorkspaceError as exc:
                candidate_verdicts[key] = exc
                raise
            verdict = candidate.is_file()
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
            tree = ast.parse(source_path.read_bytes(), filename=relative)
        except (OSError, SyntaxError, ValueError) as exc:
            raise WorkspaceError(
                f"validation_python_import_parse_failed:{relative}"
            ) from exc
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
                queue.extend((child, facts) for child in ast.iter_child_nodes(node))
            elif isinstance(node, _STATEMENT_HOLDERS):
                queue.extend(
                    (child, None)
                    for child in ast.iter_child_nodes(node)
                    if isinstance(child, _STATEMENT_HOLDERS)
                )
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                import_nodes.append(node)

        # Ordinary imports retain their existing transitive closure behavior.
        for node in import_nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add_module(
                        tuple(alias.name.split(".")),
                        pending,
                        rows,
                        source_root,
                        allow_fallback=True,
                    )
            else:
                module_parts = tuple((node.module or "").split(".")) if node.module else ()
                if node.level:
                    if not package_parts or node.level > len(package_parts):
                        raise WorkspaceError(f"validation_python_relative_import_unresolved:{relative}:level={node.level}")
                    base = package_parts[: len(package_parts) - node.level + 1] + module_parts
                    if module_parts and not add_module(base, pending, rows, source_root):
                        raise WorkspaceError(f"validation_python_relative_import_unresolved:{relative}:" + ".".join(base))
                else:
                    base = module_parts
                    add_module(base, pending, rows, source_root, allow_fallback=True)
                for alias in node.names:
                    if alias.name != "*":
                        add_module(
                            base + tuple(alias.name.split(".")),
                            pending,
                            rows,
                            source_root,
                            allow_fallback=node.level == 0,
                        )

        # Dynamic imports are intentionally limited to direct canonical calls in
        # module-level statements, evaluated after an unaliased importlib import.
        for statement, facts in zip(tree.body, statement_facts):
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
    return tuple(sorted(rows))
