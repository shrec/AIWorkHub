"""AST-first evidence extraction for the AIWorkHub canonical Source Graph.

Python files are parsed with :mod:`ast` so every entity and
edge carries exact ``file:line`` evidence, an extractor identity, and a
confidence label. Every extracted item is one of three explicit classes:

  * ``EXTRACTED`` -- directly observed in the AST (a ``def``/``class``
    statement, a literal ``import``, a call whose callee resolves to a
    name bound in the same module).
  * ``INFERRED`` -- derived with a documented, non-syntactic rule (an
    attribute call such as ``self.foo()`` or ``obj.method()`` where the
    receiver's type is not statically known).
  * ``AMBIGUOUS`` -- the callee name is never bound in this module (e.g.
    it may come from a ``from x import *`` or is simply undefined here).

PHP files use a conservative, dependency-free lexical structural extractor.
It masks strings/comments before observing namespaces, imports, class-like
declarations, functions/methods and inheritance. It deliberately emits no
call edges, because resolving dynamic PHP calls without a full parser would
overstate authority. Unsupported files remain explicit fail-closed records.

The JavaScript/TypeScript family (``.js .jsx .mjs .cjs .ts .tsx``) is the
one documented exception: B881 gives these files a truthful *file-level*
authority record -- one ``kind="file"`` entity carrying the exact
language, path, byte size and content hash directly observed from disk
(``FILE_EVIDENCE``) -- without inventing any function/class/call/import
inside the file. No parser dependency is added; nothing about the file's
internal structure is claimed.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

EXTRACTOR_ID = "aiworkhub.source_graph_ast.python_stdlib_ast.v1"
FILE_EVIDENCE_EXTRACTOR_ID = "aiworkhub.source_graph_ast.file_evidence.v1"
PHP_LEXICAL_EXTRACTOR_ID = "aiworkhub.source_graph_ast.php_lexical.v1"

EXTRACTED = "EXTRACTED"
INFERRED = "INFERRED"
AMBIGUOUS = "AMBIGUOUS"
FILE_EVIDENCE = "FILE_EVIDENCE"

ENTITY_KINDS = ("module", "class", "function", "method", "import", "file")
EDGE_KINDS = ("imports", "calls", "defines", "inherits")

PYTHON_EXTENSIONS = (".py",)
PHP_EXTENSIONS = (".php", ".phtml", ".php3", ".php4", ".php5", ".php7", ".php8")

# JS/TS family languages get file-level (not semantic) evidence: real path,
# hash, size and language, no fabricated entities/edges inside the file.
JS_TS_LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}
JS_TS_EXTENSIONS = tuple(JS_TS_LANGUAGE_BY_EXTENSION)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class Entity:
    kind: str
    name: str
    qualname: str
    file_path: str
    line_start: int
    line_end: int
    signature: str
    evidence_label: str
    extractor: str
    confidence: float
    source_hash: str
    build_revision: str


@dataclass(frozen=True, slots=True)
class Edge:
    kind: str
    src_qualname: str
    dst_name: str
    dst_qualname: str | None
    file_path: str
    line: int
    evidence_label: str
    extractor: str
    confidence: float
    source_hash: str
    build_revision: str


@dataclass(frozen=True, slots=True)
class FileExtraction:
    file_path: str
    language: str
    status: str
    source_hash: str
    entities: tuple[Entity, ...] = field(default_factory=tuple)
    edges: tuple[Edge, ...] = field(default_factory=tuple)
    error: str = ""


def extract_file(repo_root: Path, file_path: Path, *, build_revision: str) -> FileExtraction:
    """Extract entities/edges for one file. Fail-closed for non-Python."""

    rel = file_path.relative_to(repo_root).as_posix()
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        return FileExtraction(
            file_path=rel, language="unknown", status="unreadable_fail_closed",
            source_hash="", error=str(exc),
        )
    source_hash = sha256_bytes(raw)

    js_ts_language = JS_TS_LANGUAGE_BY_EXTENSION.get(file_path.suffix)
    if js_ts_language is not None:
        return _extract_file_evidence(rel, raw, js_ts_language, source_hash, build_revision)

    if file_path.suffix.lower() in PHP_EXTENSIONS:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return FileExtraction(
                file_path=rel, language="php", status="decode_error_fail_closed",
                source_hash=source_hash, error=str(exc),
            )
        return _extract_php_lexical(rel, text, source_hash, build_revision)

    if file_path.suffix not in PYTHON_EXTENSIONS:
        return FileExtraction(
            file_path=rel, language=file_path.suffix.lstrip(".") or "unknown",
            status="unsupported_fail_closed", source_hash=source_hash,
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return FileExtraction(
            file_path=rel, language="python", status="decode_error_fail_closed",
            source_hash=source_hash, error=str(exc),
        )
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        return FileExtraction(
            file_path=rel, language="python", status="parse_error_fail_closed",
            source_hash=source_hash, error=f"{exc.msg}:{exc.lineno}",
        )

    return _extract_python_ast(rel, tree, source_hash, build_revision)


def _mask_php_non_code(text: str) -> str:
    """Mask PHP strings/comments while preserving byte positions/newlines."""

    chars = list(text)
    i = 0
    state = "code"
    quote = ""
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if state == "code":
            if ch in {"'", '"', "`"}:
                state, quote = "string", ch
                chars[i] = " "
            elif ch == "/" and nxt == "/":
                state = "line_comment"
                chars[i] = chars[i + 1] = " "
                i += 1
            elif ch == "#":
                state = "line_comment"
                chars[i] = " "
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                chars[i] = chars[i + 1] = " "
                i += 1
        elif state == "string":
            if ch == "\\" and i + 1 < len(chars):
                if chars[i] != "\n":
                    chars[i] = " "
                if chars[i + 1] != "\n":
                    chars[i + 1] = " "
                i += 1
            elif ch == quote:
                chars[i] = " "
                state = "code"
            elif ch != "\n":
                chars[i] = " "
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
            else:
                chars[i] = " "
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                chars[i] = chars[i + 1] = " "
                i += 1
                state = "code"
            elif ch != "\n":
                chars[i] = " "
        i += 1
    return "".join(chars)


def _matching_delimiter(text: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return len(text) - 1


def _php_line(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _php_signature(text: str, start: int, end: int) -> str:
    return " ".join(text[start:end].strip().split())[:400]


def _extract_php_lexical(
    rel: str, text: str, source_hash: str, build_revision: str,
) -> FileExtraction:
    """Extract conservative PHP declarations without external dependencies."""

    masked = _mask_php_non_code(text)
    namespace_match = re.search(r"\bnamespace\s+([A-Za-z_][A-Za-z0-9_\\]*)\s*[;{]", masked)
    namespace = namespace_match.group(1) if namespace_match else ""
    module_qualname = rel
    entities: list[Entity] = [Entity(
        kind="module", name=rel, qualname=module_qualname, file_path=rel,
        line_start=1, line_end=max(1, text.count("\n") + 1),
        signature=f"namespace {namespace}" if namespace else "",
        evidence_label=EXTRACTED, extractor=PHP_LEXICAL_EXTRACTOR_ID,
        confidence=1.0, source_hash=source_hash, build_revision=build_revision,
    )]
    edges: list[Edge] = []

    class_pattern = re.compile(
        r"\b(class|interface|trait|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b([^;{]*)\{",
        re.IGNORECASE,
    )
    class_ranges: list[tuple[int, int, str, str]] = []
    pending_inherits: list[tuple[str, str, int]] = []
    local_classes: dict[str, str] = {}
    for match in class_pattern.finditer(masked):
        kind_label, name, tail = match.group(1).lower(), match.group(2), match.group(3)
        open_brace = match.end() - 1
        close_brace = _matching_delimiter(masked, open_brace, "{", "}")
        qualname = f"{namespace}\\{name}" if namespace else f"{rel}::{name}"
        local_classes[name] = qualname
        class_ranges.append((match.start(), close_brace, name, qualname))
        signature = _php_signature(text, match.start(), open_brace)
        line = _php_line(text, match.start())
        entities.append(Entity(
            kind="class", name=name, qualname=qualname, file_path=rel,
            line_start=line, line_end=_php_line(text, close_brace),
            signature=f"{kind_label} {signature.split(None, 1)[-1]}",
            evidence_label=EXTRACTED, extractor=PHP_LEXICAL_EXTRACTOR_ID,
            confidence=0.98, source_hash=source_hash, build_revision=build_revision,
        ))
        edges.append(Edge(
            kind="defines", src_qualname=module_qualname, dst_name=name,
            dst_qualname=qualname, file_path=rel, line=line,
            evidence_label=EXTRACTED, extractor=PHP_LEXICAL_EXTRACTOR_ID,
            confidence=0.98, source_hash=source_hash, build_revision=build_revision,
        ))
        extends_match = re.search(r"\bextends\s+([A-Za-z_\\][A-Za-z0-9_\\]*)", tail, re.IGNORECASE)
        if extends_match:
            pending_inherits.append((qualname, extends_match.group(1), line))
        implements_match = re.search(r"\bimplements\s+([^\{]+)$", tail, re.IGNORECASE)
        if implements_match:
            for base in implements_match.group(1).split(","):
                base = base.strip()
                if re.fullmatch(r"[A-Za-z_\\][A-Za-z0-9_\\]*", base):
                    pending_inherits.append((qualname, base, line))

    def containing_class(position: int) -> tuple[int, int, str, str] | None:
        matches = [item for item in class_ranges if item[0] < position < item[1]]
        return min(matches, key=lambda item: item[1] - item[0]) if matches else None

    import_aliases: dict[str, str] = {}
    for match in re.finditer(r"\buse\s+([^;{}]+);", masked, re.IGNORECASE):
        if containing_class(match.start()) is not None:
            continue
        clause = match.group(1).strip()
        if clause.lower().startswith(("function ", "const ")):
            clause = clause.split(None, 1)[1]
        for item in clause.split(","):
            item = item.strip()
            alias_match = re.fullmatch(
                r"([A-Za-z_\\][A-Za-z0-9_\\]*)(?:\s+as\s+([A-Za-z_][A-Za-z0-9_]*))?",
                item,
                re.IGNORECASE,
            )
            if not alias_match:
                continue
            target, explicit_alias = alias_match.groups()
            alias = explicit_alias or target.rsplit("\\", 1)[-1]
            import_aliases[alias] = target
            line = _php_line(text, match.start())
            import_qualname = f"{rel}::import::{alias}::{line}"
            entities.append(Entity(
                kind="import", name=alias, qualname=import_qualname,
                file_path=rel, line_start=line, line_end=line, signature=target,
                evidence_label=EXTRACTED, extractor=PHP_LEXICAL_EXTRACTOR_ID,
                confidence=0.98, source_hash=source_hash, build_revision=build_revision,
            ))
            edges.append(Edge(
                kind="imports", src_qualname=module_qualname, dst_name=target,
                dst_qualname=None, file_path=rel, line=line,
                evidence_label=EXTRACTED, extractor=PHP_LEXICAL_EXTRACTOR_ID,
                confidence=0.98, source_hash=source_hash, build_revision=build_revision,
            ))

    function_pattern = re.compile(
        r"\bfunction\s+&?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        re.IGNORECASE,
    )
    seen_qualnames: dict[str, int] = {}
    for match in function_pattern.finditer(masked):
        name = match.group(1)
        open_paren = match.end() - 1
        close_paren = _matching_delimiter(masked, open_paren, "(", ")")
        terminators = [(masked.find(token, close_paren + 1), token) for token in ("{", ";")]
        terminators = [(pos, token) for pos, token in terminators if pos >= 0]
        body_pos, token = min(terminators, default=(close_paren, ";"))
        body_end = _matching_delimiter(masked, body_pos, "{", "}") if token == "{" else body_pos
        owner = containing_class(match.start())
        base_qualname = (
            f"{owner[3]}::{name}" if owner
            else f"{namespace}\\{name}" if namespace
            else f"{rel}::{name}"
        )
        qualname = _dedupe_qualname(seen_qualnames, base_qualname)
        line = _php_line(text, match.start())
        kind = "method" if owner else "function"
        signature = f"function {name}{_php_signature(text, open_paren, close_paren + 1)}"
        entities.append(Entity(
            kind=kind, name=name, qualname=qualname, file_path=rel,
            line_start=line, line_end=_php_line(text, body_end), signature=signature,
            evidence_label=EXTRACTED, extractor=PHP_LEXICAL_EXTRACTOR_ID,
            confidence=0.96, source_hash=source_hash, build_revision=build_revision,
        ))
        parent_qualname = owner[3] if owner else module_qualname
        edges.append(Edge(
            kind="defines", src_qualname=parent_qualname, dst_name=name,
            dst_qualname=qualname, file_path=rel, line=line,
            evidence_label=EXTRACTED, extractor=PHP_LEXICAL_EXTRACTOR_ID,
            confidence=0.96, source_hash=source_hash, build_revision=build_revision,
        ))

    for src, raw_base, line in pending_inherits:
        simple = raw_base.lstrip("\\").rsplit("\\", 1)[-1]
        resolved = local_classes.get(simple)
        target = import_aliases.get(simple, raw_base)
        edges.append(Edge(
            kind="inherits", src_qualname=src, dst_name=target,
            dst_qualname=resolved, file_path=rel, line=line,
            evidence_label=EXTRACTED if resolved else AMBIGUOUS,
            extractor=PHP_LEXICAL_EXTRACTOR_ID,
            confidence=0.98 if resolved else 0.7,
            source_hash=source_hash, build_revision=build_revision,
        ))

    return FileExtraction(
        file_path=rel, language="php", status="ok", source_hash=source_hash,
        entities=tuple(entities), edges=tuple(edges),
    )


def _extract_file_evidence(
    rel: str, raw: bytes, language: str, source_hash: str, build_revision: str,
) -> FileExtraction:
    """One truthful ``kind="file"`` entity: exact path/language/size/hash.

    No function/class/call/import is claimed for the file's contents --
    there is no JS/TS parser here, so nothing beyond directly observed
    file facts (line count, byte size, language, hash) is recorded.
    """

    line_count = raw.count(b"\n") + (1 if raw and not raw.endswith(b"\n") else 0)
    entity = Entity(
        kind="file", name=rel, qualname=rel, file_path=rel,
        line_start=1, line_end=max(1, line_count), signature=f"bytes={len(raw)}",
        evidence_label=FILE_EVIDENCE, extractor=FILE_EVIDENCE_EXTRACTOR_ID,
        confidence=1.0, source_hash=source_hash, build_revision=build_revision,
    )
    return FileExtraction(
        file_path=rel, language=language, status="file_evidence_only",
        source_hash=source_hash, entities=(entity,), edges=(),
    )


def _sig_of(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = [a.arg for a in node.args.args]
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(args)})"
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(b) for b in node.bases]
        return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
    return ""


def _end_line(node: ast.AST) -> int:
    return int(getattr(node, "end_lineno", None) or getattr(node, "lineno", 0))


def _dedupe_qualname(qualname_counts: dict[str, int], base_qualname: str) -> str:
    """Disambiguate repeated nested definitions sharing one lexical scope.

    Two sibling ``def``/``class`` statements with the same name under the
    same owner (e.g. a helper named ``_avg`` redefined twice inside the
    same function) would otherwise collide on the ``(file_path, qualname)``
    UNIQUE constraint. The first occurrence keeps the plain qualname
    (backward compatible); each further occurrence gets a stable ``~N``
    suffix driven by deterministic AST traversal order, so repeated
    incremental builds of unchanged source always assign the same
    disambiguated identity.
    """

    seen = qualname_counts.get(base_qualname, 0) + 1
    qualname_counts[base_qualname] = seen
    return base_qualname if seen == 1 else f"{base_qualname}~{seen}"


def _extract_python_ast(rel: str, tree: ast.Module, source_hash: str, build_revision: str) -> FileExtraction:
    entities: list[Entity] = []
    edges: list[Edge] = []
    module_qualname = rel

    entities.append(Entity(
        kind="module", name=rel, qualname=module_qualname, file_path=rel,
        line_start=1, line_end=_end_line(tree) or 1, signature="",
        evidence_label=EXTRACTED, extractor=EXTRACTOR_ID, confidence=1.0,
        source_hash=source_hash, build_revision=build_revision,
    ))

    # Module-wide bound-name table (flat, non-lexically-scoped by design):
    # collected in a first pass over the WHOLE file before any call is
    # resolved, so a function may call another defined later in the same
    # file (a forward reference) and still resolve as EXTRACTED. Calls
    # classify as EXTRACTED (name literally defined/imported in this
    # module -- ``bound_qualnames`` gives the exact internal target when
    # the name is a local def/class), INFERRED (attribute call on an
    # unresolved receiver), or AMBIGUOUS (bare name never bound in this
    # module, e.g. only reachable via `from x import *`).
    bound_names: set[str] = set()
    bound_qualnames: dict[str, str] = {}
    scopes: list[tuple[ast.AST, str]] = []  # (scope_node, owner_qualname) needing call extraction
    qualname_counts: dict[str, int] = {}

    def _collect(node: ast.AST, owner_qualname: str, kind_for_children: str) -> None:
        scopes.append((node, owner_qualname))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = _dedupe_qualname(qualname_counts, f"{owner_qualname}.{child.name}")
                bound_names.add(child.name)
                bound_qualnames[child.name] = qualname
                entities.append(Entity(
                    kind=kind_for_children, name=child.name, qualname=qualname,
                    file_path=rel, line_start=child.lineno, line_end=_end_line(child),
                    signature=_sig_of(child), evidence_label=EXTRACTED,
                    extractor=EXTRACTOR_ID, confidence=1.0, source_hash=source_hash,
                    build_revision=build_revision,
                ))
                edges.append(Edge(
                    kind="defines", src_qualname=owner_qualname, dst_name=child.name,
                    dst_qualname=qualname, file_path=rel, line=child.lineno,
                    evidence_label=EXTRACTED, extractor=EXTRACTOR_ID, confidence=1.0,
                    source_hash=source_hash, build_revision=build_revision,
                ))
                _collect(child, qualname, "method")
            elif isinstance(child, ast.ClassDef):
                qualname = _dedupe_qualname(qualname_counts, f"{owner_qualname}.{child.name}")
                bound_names.add(child.name)
                bound_qualnames[child.name] = qualname
                entities.append(Entity(
                    kind="class", name=child.name, qualname=qualname, file_path=rel,
                    line_start=child.lineno, line_end=_end_line(child),
                    signature=_sig_of(child), evidence_label=EXTRACTED,
                    extractor=EXTRACTOR_ID, confidence=1.0, source_hash=source_hash,
                    build_revision=build_revision,
                ))
                edges.append(Edge(
                    kind="defines", src_qualname=owner_qualname, dst_name=child.name,
                    dst_qualname=qualname, file_path=rel, line=child.lineno,
                    evidence_label=EXTRACTED, extractor=EXTRACTOR_ID, confidence=1.0,
                    source_hash=source_hash, build_revision=build_revision,
                ))
                _collect(child, qualname, "method")
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    dst = alias.name if isinstance(child, ast.Import) else f"{'.' * (child.level or 0)}{child.module or ''}.{alias.name}"
                    local_name = alias.asname or alias.name.split(".")[0]
                    bound_names.add(local_name)
                    import_qualname = _dedupe_qualname(
                        qualname_counts,
                        f"{module_qualname}::import::{local_name}::{child.lineno}",
                    )
                    entities.append(Entity(
                        kind="import", name=local_name,
                        qualname=import_qualname,
                        file_path=rel, line_start=child.lineno, line_end=child.lineno,
                        signature=dst, evidence_label=EXTRACTED, extractor=EXTRACTOR_ID,
                        confidence=1.0, source_hash=source_hash, build_revision=build_revision,
                    ))
                    edges.append(Edge(
                        kind="imports", src_qualname=owner_qualname, dst_name=dst,
                        dst_qualname=None, file_path=rel, line=child.lineno,
                        evidence_label=EXTRACTED, extractor=EXTRACTOR_ID, confidence=1.0,
                        source_hash=source_hash, build_revision=build_revision,
                    ))
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        bound_names.add(target.id)
            else:
                _collect(child, owner_qualname, kind_for_children)

    _collect(tree, module_qualname, "function")

    # Second pass: bound_names/bound_qualnames now cover the WHOLE file, so
    # resolve calls (and class bases) per collected scope with forward
    # references visible.
    for scope_node, owner_qualname in scopes:
        _extract_calls(scope_node, owner_qualname, edges, rel, source_hash, build_revision, bound_names, bound_qualnames)

    def _resolve_inherits(node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.ClassDef):
                qualname = bound_qualnames.get(child.name)
                if qualname is None:
                    continue
                for base in child.bases:
                    base_name = ast.unparse(base)
                    if not base_name:
                        continue
                    edges.append(Edge(
                        kind="inherits", src_qualname=qualname, dst_name=base_name,
                        dst_qualname=bound_qualnames.get(base_name), file_path=rel,
                        line=child.lineno,
                        evidence_label=EXTRACTED if base_name in bound_names else AMBIGUOUS,
                        extractor=EXTRACTOR_ID,
                        confidence=1.0 if base_name in bound_names else 0.5,
                        source_hash=source_hash, build_revision=build_revision,
                    ))

    _resolve_inherits(tree)

    return FileExtraction(
        file_path=rel, language="python", status="ok", source_hash=source_hash,
        entities=tuple(entities), edges=tuple(edges),
    )


def _extract_calls(
    scope_node: ast.AST,
    owner_qualname: str,
    edges: list[Edge],
    rel: str,
    source_hash: str,
    build_revision: str,
    bound_names: set[str],
    bound_qualnames: dict[str, str],
) -> None:
    """Record direct calls made in ``scope_node``'s own body (not nested defs)."""

    for node in ast.walk(scope_node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not scope_node:
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        dst_qualname: str | None = None
        if isinstance(func, ast.Name):
            dst_name = func.id
            label = EXTRACTED if dst_name in bound_names else AMBIGUOUS
            confidence = 1.0 if dst_name in bound_names else 0.4
            dst_qualname = bound_qualnames.get(dst_name)
        elif isinstance(func, ast.Attribute):
            dst_name = func.attr
            label = INFERRED
            confidence = 0.6
        else:
            continue
        edges.append(Edge(
            kind="calls", src_qualname=owner_qualname, dst_name=dst_name, dst_qualname=dst_qualname,
            file_path=rel, line=node.lineno, evidence_label=label, extractor=EXTRACTOR_ID,
            confidence=confidence, source_hash=source_hash, build_revision=build_revision,
        ))


__all__ = [
    "AMBIGUOUS",
    "EDGE_KINDS",
    "ENTITY_KINDS",
    "EXTRACTED",
    "EXTRACTOR_ID",
    "FILE_EVIDENCE",
    "FILE_EVIDENCE_EXTRACTOR_ID",
    "INFERRED",
    "JS_TS_EXTENSIONS",
    "JS_TS_LANGUAGE_BY_EXTENSION",
    "PHP_EXTENSIONS",
    "PHP_LEXICAL_EXTRACTOR_ID",
    "Edge",
    "Entity",
    "FileExtraction",
    "extract_file",
    "sha256_bytes",
]
