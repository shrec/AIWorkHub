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

C/C++/CUDA/OpenCL/Metal files use a conservative lexical adapter derived from
the proven UltrafastSecp256k1 Source Graph design. It records includes,
namespaces, classes/structs/enums/macros, function bodies and observed call
syntax (including CUDA ``<<<...>>>`` launches). Cross-file call targets are
resolved later by the canonical index builder and stay explicitly inferred
when the target is ambiguous.

JavaScript/TypeScript, Rust, Go, Java and C# use the same donor-proven adapter
model with language-specific declaration/import rules and conservative call
syntax. Every other language registered by Source Graph receives one truthful
*file-level* authority record -- a ``kind="file"`` entity carrying the exact
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

from . import source_graph_languages as languages

EXTRACTOR_ID = "aiworkhub.source_graph_ast.python_stdlib_ast.v1"
FILE_EVIDENCE_EXTRACTOR_ID = "aiworkhub.source_graph_ast.file_evidence.v1"
PHP_LEXICAL_EXTRACTOR_ID = "aiworkhub.source_graph_ast.php_lexical.v1"
CPP_LEXICAL_EXTRACTOR_ID = "aiworkhub.source_graph_ast.cpp_lexical.v1"
POLYGLOT_LEXICAL_EXTRACTOR_ID = "aiworkhub.source_graph_ast.polyglot_lexical.v1"
TREE_SITTER_JS_TS_EXTRACTOR_ID = (
    "aiworkhub.source_graph.tree_sitter.javascript_typescript.v1"
)

EXTRACTED = "EXTRACTED"
INFERRED = "INFERRED"
AMBIGUOUS = "AMBIGUOUS"
FILE_EVIDENCE = "FILE_EVIDENCE"

ENTITY_KINDS = (
    "module", "namespace", "class", "struct", "enum", "macro",
    "function", "method", "import", "file", "attribute", "decorator", "annotation",
)
EDGE_KINDS = (
    "imports", "calls", "defines", "inherits", "writes", "references",
    "decorates", "annotates",
)

PYTHON_EXTENSIONS = tuple(languages.LANGUAGE_BY_ID["python"].extensions)
PHP_EXTENSIONS = (".php", ".phtml", ".php3", ".php4", ".php5", ".php7", ".php8")
CPP_EXTENSIONS = tuple(languages.LANGUAGE_BY_ID["cpp"].extensions)
POLYGLOT_LEXICAL_LANGUAGES = frozenset({
    "javascript", "typescript", "rust", "go", "java", "csharp",
})


def expected_extractor_ids(file_path: Path) -> frozenset[str]:
    """Return the extractor generation expected for an unchanged file.

    Incremental indexing may skip reading a file only when its persisted
    extractor set still matches the currently available parser backend.  This
    keeps optional Tree-sitter installation/removal observable even though the
    repository file's size and mtime did not change.
    """

    suffix = file_path.suffix.casefold()
    language = languages.language_for_path(file_path)
    if suffix in PHP_EXTENSIONS:
        return frozenset({PHP_LEXICAL_EXTRACTOR_ID})
    if suffix in CPP_EXTENSIONS:
        return frozenset({CPP_LEXICAL_EXTRACTOR_ID})
    if language in {"javascript", "typescript"}:
        from . import source_graph_semantic

        capability = source_graph_semantic.parser_capability(
            language, file_path=file_path.as_posix(),
        )
        return frozenset({
            TREE_SITTER_JS_TS_EXTRACTOR_ID
            if capability.get("available")
            else POLYGLOT_LEXICAL_EXTRACTOR_ID
        })
    if language in POLYGLOT_LEXICAL_LANGUAGES:
        return frozenset({POLYGLOT_LEXICAL_EXTRACTOR_ID})
    if suffix in PYTHON_EXTENSIONS:
        return frozenset({EXTRACTOR_ID})
    if language is not None:
        return frozenset({FILE_EVIDENCE_EXTRACTOR_ID})
    return frozenset()


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
    source_col: int = -1
    receiver_name: str = ""


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
    """Extract semantic or truthful file-level evidence for one known file."""

    rel = file_path.relative_to(repo_root).as_posix()
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        return FileExtraction(
            file_path=rel, language="unknown", status="unreadable_fail_closed",
            source_hash="", error=str(exc),
        )
    return extract_file_from_bytes(
        repo_root,
        file_path,
        raw,
        build_revision=build_revision,
    )


def extract_file_from_bytes(
    repo_root: Path,
    file_path: Path,
    raw: bytes,
    *,
    build_revision: str,
) -> FileExtraction:
    """Extract evidence from an authenticated byte snapshot."""

    rel = file_path.relative_to(repo_root).as_posix()
    source_hash = sha256_bytes(raw)

    suffix = file_path.suffix.lower()
    language = languages.language_for_path(file_path)

    if suffix in PHP_EXTENSIONS:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return FileExtraction(
                file_path=rel, language="php", status="decode_error_fail_closed",
                source_hash=source_hash, error=str(exc),
            )
        return _extract_php_lexical(rel, text, source_hash, build_revision)

    if suffix in CPP_EXTENSIONS:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return FileExtraction(
                file_path=rel, language="cpp", status="decode_error_fail_closed",
                source_hash=source_hash, error=str(exc),
            )
        return _extract_cpp_lexical(rel, text, source_hash, build_revision)

    if language in POLYGLOT_LEXICAL_LANGUAGES:
        if language in {"javascript", "typescript"}:
            from . import source_graph_semantic

            semantic = source_graph_semantic.extract_javascript_typescript(
                file_path=rel, raw=raw, language=language,
            )
            if semantic is not None:
                return FileExtraction(
                    file_path=rel,
                    language=language,
                    status="ok",
                    source_hash=source_hash,
                    entities=tuple(Entity(
                        kind=str(row["kind"]),
                        name=str(row["name"]),
                        qualname=str(row["qualname"]),
                        file_path=rel,
                        line_start=int(row["line_start"]),
                        line_end=int(row["line_end"]),
                        signature=str(row["signature"]),
                        evidence_label=EXTRACTED,
                        extractor=source_graph_semantic.EXTRACTOR_ID,
                        confidence=float(row["confidence"]),
                        source_hash=source_hash,
                        build_revision=build_revision,
                    ) for row in semantic.entities),
                    edges=tuple(Edge(
                        kind=str(row["kind"]),
                        src_qualname=str(row["src_qualname"]),
                        dst_name=str(row["dst_name"]),
                        dst_qualname=(
                            str(row["dst_qualname"])
                            if row.get("dst_qualname") is not None else None
                        ),
                        file_path=rel,
                        line=int(row["line"]),
                        evidence_label=str(row["evidence_label"]),
                        extractor=source_graph_semantic.EXTRACTOR_ID,
                        confidence=float(row["confidence"]),
                        source_hash=source_hash,
                        build_revision=build_revision,
                    ) for row in semantic.edges),
                )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return FileExtraction(
                file_path=rel, language=language, status="decode_error_fail_closed",
                source_hash=source_hash, error=str(exc),
            )
        return _extract_polyglot_lexical(
            rel, text, source_hash, build_revision, language=language,
        )

    if suffix not in PYTHON_EXTENSIONS:
        if language is not None:
            return _extract_file_evidence(rel, raw, language, source_hash, build_revision)
        return FileExtraction(
            file_path=rel, language=suffix.lstrip(".") or "unknown",
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
    # One per-file qualname counter shared by EVERY declaration kind this
    # extractor emits -- class-like declarations, imports and functions/methods
    # -- mirroring the C family's single ``counts`` dict threaded through
    # _cpp_qualname. Two same-named class/interface/trait declarations, or a
    # class and a free function that resolve to one namespaced name, would
    # otherwise emit an identical ``(file_path, qualname)`` pair and abort the
    # whole index on the UNIQUE constraint. The ``module`` entity deliberately
    # keeps the bare relative path: it is unique per file and can never collide
    # with any ``::``/``\\``-qualified declaration, matching the C family.
    seen_qualnames: dict[str, int] = {}

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
        qualname = _dedupe_qualname(
            seen_qualnames, f"{namespace}\\{name}" if namespace else f"{rel}::{name}"
        )
        # First-wins for the bare-name -> qualname table: when a file declares
        # the same class-like name twice, a same-file ``extends``/``implements``
        # edge must resolve to the FIRST (primary) declaration, never the
        # ``~N``-suffixed duplicate. PHP itself would fatal on a genuine
        # redeclaration, so the first lexical occurrence is the only sensible
        # owner of the bare name; keeping it stable also avoids re-pointing
        # every inheritance edge whenever a duplicate is added or removed.
        local_classes.setdefault(name, qualname)
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
            import_qualname = _dedupe_qualname(
                seen_qualnames, f"{rel}::import::{alias}::{line}"
            )
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


_CPP_CONTROL_NAMES = frozenset({
    "if", "for", "while", "switch", "catch", "sizeof", "alignof",
    "decltype", "return", "new", "delete", "static_assert", "requires",
})


_JAVASCRIPT_REGEX_PREFIX_CHARS = frozenset("=(:,[!&|?{};+-*%^~<>")
_JAVASCRIPT_REGEX_PREFIX_WORDS = frozenset({
    "await", "case", "delete", "in", "instanceof", "new", "of", "return",
    "throw", "typeof", "void", "yield",
})


def _javascript_regex_literal_starts(chars: list[str], index: int) -> bool:
    """Conservatively distinguish a JS regex literal from division."""

    previous = index - 1
    while previous >= 0 and chars[previous].isspace():
        previous -= 1
    if previous < 0 or chars[previous] in _JAVASCRIPT_REGEX_PREFIX_CHARS:
        return True
    if chars[previous].isalnum() or chars[previous] in {"_", "$"}:
        end = previous + 1
        while previous >= 0 and (
            chars[previous].isalnum() or chars[previous] in {"_", "$"}
        ):
            previous -= 1
        return "".join(chars[previous + 1:end]) in _JAVASCRIPT_REGEX_PREFIX_WORDS
    return False


def _mask_c_family_non_code(
    text: str, *, javascript_regex_literals: bool = False,
) -> str:
    """Mask C-family strings/comments while preserving offsets and lines."""

    chars = list(text)
    i = 0
    state = "code"
    quote = ""
    regex_character_class = False
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line_comment"
                chars[i] = chars[i + 1] = " "
                i += 1
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                chars[i] = chars[i + 1] = " "
                i += 1
            elif (
                javascript_regex_literals
                and ch == "/"
                and _javascript_regex_literal_starts(chars, i)
            ):
                state = "regex"
                regex_character_class = False
                chars[i] = " "
            elif ch in {"'", '"', "`"}:
                state, quote = "string", ch
                chars[i] = " "
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
        elif state == "regex":
            if ch == "\n":
                state = "code"
                regex_character_class = False
            elif ch == "\\" and i + 1 < len(chars):
                chars[i] = " "
                if chars[i + 1] != "\n":
                    chars[i + 1] = " "
                i += 1
            elif ch == "[":
                regex_character_class = True
                chars[i] = " "
            elif ch == "]" and regex_character_class:
                regex_character_class = False
                chars[i] = " "
            elif ch == "/" and not regex_character_class:
                chars[i] = " "
                state = "code"
                while i + 1 < len(chars) and chars[i + 1].isalpha():
                    i += 1
                    chars[i] = " "
            else:
                chars[i] = " "
        i += 1
    return "".join(chars)


def _mask_comments_preserve_strings(text: str) -> str:
    """Mask line/block comments but preserve quoted import/include targets."""

    chars = list(text)
    i = 0
    state = "code"
    quote = ""
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line_comment"
                chars[i] = chars[i + 1] = " "
                i += 1
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                chars[i] = chars[i + 1] = " "
                i += 1
            elif ch in {"'", '"', "`"}:
                state, quote = "string", ch
        elif state == "string":
            if ch == "\\" and i + 1 < len(chars):
                i += 1
            elif ch == quote:
                state = "code"
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


def _line_for_offset(text: str, position: int) -> int:
    return text.count("\n", 0, max(0, position)) + 1


def _cpp_signature(text: str, start: int, opening_brace: int) -> str:
    return " ".join(text[start:opening_brace].strip().split())[:500]


def _cpp_qualname(rel: str, name: str, counts: dict[str, int]) -> str:
    normalized = re.sub(r"\s+", "", name)
    base = f"{rel}::{normalized}"
    seen = counts.get(base, 0) + 1
    counts[base] = seen
    return base if seen == 1 else f"{base}~{seen}"


def _extract_cpp_lexical(
    rel: str, text: str, source_hash: str, build_revision: str,
) -> FileExtraction:
    """Conservatively extract C/C++/CUDA declarations and observed calls.

    This is deliberately lexical: declarations/call syntax are directly
    observed, while cross-file target identity remains inferred until the
    canonical builder can prove one unique matching entity.
    """

    masked = _mask_c_family_non_code(text)
    line_count = max(1, text.count("\n") + 1)
    entities: list[Entity] = [Entity(
        kind="module", name=rel, qualname=rel, file_path=rel,
        line_start=1, line_end=line_count, signature="",
        evidence_label=EXTRACTED, extractor=CPP_LEXICAL_EXTRACTOR_ID,
        confidence=1.0, source_hash=source_hash, build_revision=build_revision,
    )]
    edges: list[Edge] = []
    counts: dict[str, int] = {}

    import_text = _mask_comments_preserve_strings(text)
    for match in re.finditer(
        r"(?m)^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]", import_text,
    ):
        name = match.group(1).strip()
        line = _line_for_offset(masked, match.start())
        qualname = _cpp_qualname(rel, f"include::{name}@{line}", counts)
        entities.append(Entity(
            kind="import", name=name, qualname=qualname, file_path=rel,
            line_start=line, line_end=line, signature=f"#include {name}",
            evidence_label=EXTRACTED, extractor=CPP_LEXICAL_EXTRACTOR_ID,
            confidence=1.0, source_hash=source_hash, build_revision=build_revision,
        ))
        edges.append(Edge(
            kind="imports", src_qualname=rel, dst_name=name, dst_qualname=None,
            file_path=rel, line=line, evidence_label=EXTRACTED,
            extractor=CPP_LEXICAL_EXTRACTOR_ID, confidence=1.0,
            source_hash=source_hash, build_revision=build_revision,
        ))

    for match in re.finditer(r"(?m)^\s*#\s*define\s+([A-Za-z_]\w*)\b([^\n]*)", masked):
        name = match.group(1)
        line = _line_for_offset(masked, match.start())
        entities.append(Entity(
            kind="macro", name=name, qualname=_cpp_qualname(rel, name, counts),
            file_path=rel, line_start=line, line_end=line,
            signature=f"#define {name}{match.group(2)}"[:500],
            evidence_label=EXTRACTED, extractor=CPP_LEXICAL_EXTRACTOR_ID,
            confidence=1.0, source_hash=source_hash, build_revision=build_revision,
        ))

    namespace_ranges: list[tuple[int, int, str]] = []
    for match in re.finditer(r"\bnamespace\s+([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*\{", masked):
        name = match.group(1)
        opening = masked.find("{", match.start(), match.end())
        ending = _matching_delimiter(masked, opening, "{", "}")
        line = _line_for_offset(masked, match.start())
        qualname = _cpp_qualname(rel, name, counts)
        namespace_ranges.append((opening, ending, qualname))
        entities.append(Entity(
            kind="namespace", name=name, qualname=qualname, file_path=rel,
            line_start=line, line_end=_line_for_offset(masked, ending),
            signature=f"namespace {name}", evidence_label=EXTRACTED,
            extractor=CPP_LEXICAL_EXTRACTOR_ID, confidence=1.0,
            source_hash=source_hash, build_revision=build_revision,
        ))

    type_ranges: list[tuple[int, int, str, str]] = []
    type_pattern = re.compile(
        r"\b(class|struct|union|enum(?:\s+class)?)\s+([A-Za-z_]\w*)\b([^;{]*)\{"
    )
    for match in type_pattern.finditer(masked):
        raw_kind, name, tail = match.group(1), match.group(2), match.group(3)
        kind = "enum" if raw_kind.startswith("enum") else ("struct" if raw_kind in {"struct", "union"} else "class")
        opening = masked.find("{", match.start(), match.end())
        ending = _matching_delimiter(masked, opening, "{", "}")
        line = _line_for_offset(masked, match.start())
        qualname = _cpp_qualname(rel, name, counts)
        type_ranges.append((opening, ending, name, qualname))
        entities.append(Entity(
            kind=kind, name=name, qualname=qualname, file_path=rel,
            line_start=line, line_end=_line_for_offset(masked, ending),
            signature=_cpp_signature(text, match.start(), opening),
            evidence_label=EXTRACTED, extractor=CPP_LEXICAL_EXTRACTOR_ID,
            confidence=1.0, source_hash=source_hash, build_revision=build_revision,
        ))
        inheritance = tail.split(":", 1)[1] if ":" in tail and kind in {"class", "struct"} else ""
        for base in re.findall(r"(?:public|protected|private|virtual|\s)*\b([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)", inheritance):
            edges.append(Edge(
                kind="inherits", src_qualname=qualname, dst_name=base.split("::")[-1],
                dst_qualname=None, file_path=rel, line=line,
                evidence_label=INFERRED, extractor=CPP_LEXICAL_EXTRACTOR_ID,
                confidence=0.8, source_hash=source_hash, build_revision=build_revision,
            ))

    function_pattern = re.compile(
        r"(?m)(?<![\w])(?:template\s*<[^;{}]*>\s*)?"
        r"(?:(?:[A-Za-z_][\w:<>,\s*&\[\]~]*?)\s+)?"
        r"(?P<name>(?:[A-Za-z_]\w*::)*~?[A-Za-z_]\w*|operator\s*[^\s(]+)\s*"
        r"\((?P<params>[^;{}]*)\)\s*"
        r"(?:const\b\s*)?(?:noexcept(?:\s*\([^)]*\))?\s*)?"
        r"(?:override\b\s*)?(?:final\b\s*)?(?:->\s*[^;{]+)?"
        r"(?:\s*:\s*[^;{}]+)?\s*\{"
    )
    call_patterns = (
        re.compile(r"\b([A-Za-z_]\w*(?:::[A-Za-z_]\w*)+)\s*\("),
        re.compile(r"(?:->|\.)\s*([A-Za-z_]\w*)\s*\("),
        re.compile(r"\b([A-Za-z_]\w*)\s*<<<[^;{}]*?>>>\s*\("),
        re.compile(r"\b([A-Za-z_]\w*)\s*\("),
    )
    for match in function_pattern.finditer(masked):
        full_name = re.sub(r"\s+", "", match.group("name"))
        short_name = full_name.split("::")[-1]
        if short_name in _CPP_CONTROL_NAMES:
            continue
        opening = masked.rfind("{", match.start(), match.end())
        ending = _matching_delimiter(masked, opening, "{", "}")
        if ending <= opening:
            continue
        owner = next((row for row in type_ranges if row[0] < match.start() < row[1]), None)
        kind = "method" if "::" in full_name or owner is not None else "function"
        semantic_name = full_name if "::" in full_name else (f"{owner[2]}::{short_name}" if owner else short_name)
        qualname = _cpp_qualname(rel, semantic_name, counts)
        start_line = _line_for_offset(masked, match.start())
        end_line = _line_for_offset(masked, ending)
        entities.append(Entity(
            kind=kind, name=short_name, qualname=qualname, file_path=rel,
            line_start=start_line, line_end=end_line,
            signature=_cpp_signature(text, match.start(), opening),
            evidence_label=EXTRACTED, extractor=CPP_LEXICAL_EXTRACTOR_ID,
            confidence=0.95, source_hash=source_hash, build_revision=build_revision,
        ))
        edges.append(Edge(
            kind="defines", src_qualname=owner[3] if owner else rel,
            dst_name=short_name, dst_qualname=qualname, file_path=rel,
            line=start_line, evidence_label=EXTRACTED,
            extractor=CPP_LEXICAL_EXTRACTOR_ID, confidence=1.0,
            source_hash=source_hash, build_revision=build_revision,
        ))
        body_text = masked[opening + 1:ending]
        observed: set[tuple[str, int]] = set()
        for pattern in call_patterns:
            for call in pattern.finditer(body_text):
                called = call.group(1).split("::")[-1]
                if called in _CPP_CONTROL_NAMES or called == short_name:
                    continue
                absolute = opening + 1 + call.start()
                call_line = _line_for_offset(masked, absolute)
                key = (called, call_line)
                if key in observed:
                    continue
                observed.add(key)
                edges.append(Edge(
                    kind="calls", src_qualname=qualname, dst_name=called,
                    dst_qualname=None, file_path=rel, line=call_line,
                    evidence_label=INFERRED, extractor=CPP_LEXICAL_EXTRACTOR_ID,
                    confidence=0.7, source_hash=source_hash,
                    build_revision=build_revision,
                ))

    return FileExtraction(
        file_path=rel, language="cpp", status="ok", source_hash=source_hash,
        entities=tuple(entities), edges=tuple(edges),
    )


_POLYGLOT_CONTROL_NAMES = frozenset({
    "if", "else", "for", "foreach", "while", "switch", "catch", "case",
    "return", "throw", "new", "delete", "typeof", "sizeof", "match", "loop",
    "select", "defer", "go", "function", "class", "interface", "struct",
})


def _split_rust_use_body(body: str) -> list[str]:
    """Split a Rust use-tree only at commas outside nested braces."""

    rows: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(body):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return []
        elif char == "," and depth == 0:
            rows.append(body[start:index].strip())
            start = index + 1
    if depth != 0:
        return []
    rows.append(body[start:].strip())
    return [row for row in rows if row]


def _expand_rust_use_clause(clause: str, prefix: str = "") -> list[str]:
    """Expand one balanced Rust use-tree into deterministic leaf targets."""

    clause = clause.strip()
    if not clause:
        return []
    opening = clause.find("{")
    if opening < 0:
        if clause == "self":
            return [prefix] if prefix else []
        return [f"{prefix}::{clause}" if prefix else clause]

    depth = 0
    closing = -1
    for index in range(opening, len(clause)):
        if clause[index] == "{":
            depth += 1
        elif clause[index] == "}":
            depth -= 1
            if depth == 0:
                closing = index
                break
    if closing < 0 or clause[closing + 1:].strip():
        return []
    branch = clause[:opening].rstrip(":").strip()
    branch_prefix = f"{prefix}::{branch}" if prefix and branch else branch or prefix
    items = _split_rust_use_body(clause[opening + 1:closing])
    if not items:
        return []
    leaves: list[str] = []
    for item in items:
        leaves.extend(_expand_rust_use_clause(item, branch_prefix))
    return leaves


def _rust_imports(text: str) -> list[tuple[str, int, str]]:
    """Extract balanced Rust use-trees, visibility prefixes and aliases."""

    rows: list[tuple[str, int, str]] = []
    pattern = re.compile(
        r"(?m)^\s*(?:pub(?:\s*\([^)]*\))?\s+)?use\s+([^;]+);"
    )
    for match in pattern.finditer(text):
        signature = " ".join(match.group(0).split())[:500]
        for target in _expand_rust_use_clause(match.group(1)):
            rows.append((target, match.start(), signature))
    return rows


def _polyglot_imports(language: str, text: str) -> list[tuple[str, int, str]]:
    """Return directly observed import target, offset and signature."""

    if language == "rust":
        return _rust_imports(text)

    patterns: dict[str, tuple[re.Pattern[str], ...]] = {
        "javascript": (
            re.compile(r"(?m)^\s*import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]"),
            re.compile(r"(?m)^\s*(?:const|let|var)\s+\w+\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        ),
        "typescript": (
            re.compile(r"(?m)^\s*import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]"),
            re.compile(r"(?m)^\s*import\s+['\"]([^'\"]+)['\"]"),
        ),
        "go": (re.compile(r"(?m)^\s*(?:import\s+)?(?:[A-Za-z_.]\w*\s+)?\"([\w./-]+)\""),),
        "java": (re.compile(r"(?m)^\s*import\s+(?:static\s+)?([\w.]+)\s*;"),),
        "csharp": (re.compile(r"(?m)^\s*using\s+(?:static\s+)?([\w.]+)\s*;"),),
    }
    rows: list[tuple[str, int, str]] = []
    for pattern in patterns.get(language, ()):
        for match in pattern.finditer(text):
            rows.append((match.group(1), match.start(), " ".join(match.group(0).split())[:500]))
    rows.sort(key=lambda row: row[1])
    return rows


def _polyglot_type_patterns(language: str) -> tuple[re.Pattern[str], ...]:
    if language in {"javascript", "typescript"}:
        return (
            re.compile(r"\b(?P<kind>class|interface|enum)\s+(?P<name>[$A-Za-z_]\w*)\b(?P<tail>[^;{]*)\{"),
            re.compile(r"\b(?P<kind>type)\s+(?P<name>[$A-Za-z_]\w*)\b(?P<tail>[^;{=]*)=\s*\{"),
        )
    if language == "rust":
        return (
            re.compile(r"\b(?P<kind>struct|enum|trait)\s+(?P<name>[A-Za-z_]\w*)\b(?P<tail>[^;{]*)\{"),
            re.compile(
                r"\b(?P<kind>impl)(?:\s*<[^>{}]*>)?\s+"
                r"(?:(?P<trait>[A-Za-z_]\w*(?:::\w+)*)\s+for\s+)?"
                r"(?P<name>[A-Za-z_]\w*(?:::\w+)*)\b(?P<tail>[^;{]*)\{"
            ),
        )
    if language == "go":
        return (
            re.compile(r"\btype\s+(?P<name>[A-Za-z_]\w*)\s+(?P<kind>struct|interface)\s*\{"),
        )
    if language == "java":
        return (
            re.compile(
                r"\b(?P<kind>class|interface|enum|record)\s+"
                r"(?P<name>[A-Za-z_]\w*)\b(?P<tail>[^;{]*)\{"
            ),
        )
    return (
        re.compile(
            r"\b(?P<kind>class|interface|struct|enum|record)\s+"
            r"(?P<name>[A-Za-z_]\w*)\b(?P<tail>[^;{]*)\{"
        ),
    )


def _polyglot_function_patterns(language: str) -> tuple[re.Pattern[str], ...]:
    if language in {"javascript", "typescript"}:
        return (
            re.compile(
                r"(?m)^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+"
                r"(?P<name>[$A-Za-z_]\w*)\s*(?:<[^>{}]*>)?\s*"
                r"\((?:[^;(){}]|\{[^;(){}]*\})*\)\s*"
                r"(?:\:\s*[^={]+)?\{"
            ),
            re.compile(
                r"(?m)^\s*(?:module\.exports|exports\.[$A-Za-z_]\w*|[$A-Za-z_]\w*(?:\.[$A-Za-z_]\w*)*)"
                r"\s*=\s*(?:async\s+)?function\s+(?P<name>[$A-Za-z_]\w*)\s*"
                r"\((?:[^;(){}]|\{[^;(){}]*\})*\)\s*\{"
            ),
            re.compile(
                r"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[$A-Za-z_]\w*)"
                r"(?:\s*:\s*[^=]+)?\s*=\s*(?:async\s+)?"
                r"(?:\((?:[^;(){}]|\{[^;(){}]*\})*\)|[$A-Za-z_]\w*)"
                r"\s*=>\s*\{"
            ),
            re.compile(
                r"(?m)^\s*(?:(?:public|private|protected|static|async|abstract|override|readonly)\s+)*"
                r"(?P<name>[$A-Za-z_]\w*)\s*(?:<[^>{}]*>)?\s*"
                r"\((?:[^;(){}]|\{[^;(){}]*\})*\)\s*"
                r"(?:\:\s*[^={]+)?\{"
            ),
        )
    if language == "rust":
        return (
            re.compile(
                r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?"
                r"(?:extern\s+\"[^\"]+\"\s+)?fn\s+(?P<name>[A-Za-z_]\w*)"
                r"\s*(?:<[^>{}]*>)?\s*\([^;{}]*\)[^{;]*\{"
            ),
        )
    if language == "go":
        return (
            re.compile(
                r"(?m)^\s*func\s+(?:\([^)]*\)\s*)?(?P<name>[A-Za-z_]\w*)"
                r"\s*\([^;{}]*\)[^{;]*\{"
            ),
        )
    modifiers = (
        r"(?:(?:public|private|protected|internal|static|final|abstract|synchronized|"
        r"native|default|virtual|override|sealed|async|partial|extern|unsafe|readonly)\s+)*"
    )
    return (
        re.compile(
            rf"(?m)^\s*(?:\[[^\]]+\]\s*|@\w+(?:\([^)]*\))?\s*)*{modifiers}"
            r"(?:<[^>{}]*>\s*)?(?:[A-Za-z_]\w*(?:[<>,.?\[\]\s]*\w)?\s+)?"
            r"(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*"
            r"(?:throws\s+[\w,.\s]+)?\{"
        ),
    )


def _extract_polyglot_lexical(
    rel: str,
    text: str,
    source_hash: str,
    build_revision: str,
    *,
    language: str,
) -> FileExtraction:
    """Extract donor-proven structural surfaces for six brace languages."""

    masked = _mask_c_family_non_code(
        text, javascript_regex_literals=language in {"javascript", "typescript"},
    )
    line_count = max(1, text.count("\n") + 1)
    entities: list[Entity] = [Entity(
        kind="module", name=rel, qualname=rel, file_path=rel,
        line_start=1, line_end=line_count, signature="",
        evidence_label=EXTRACTED, extractor=POLYGLOT_LEXICAL_EXTRACTOR_ID,
        confidence=1.0, source_hash=source_hash, build_revision=build_revision,
    )]
    edges: list[Edge] = []
    counts: dict[str, int] = {}

    import_text = _mask_comments_preserve_strings(text)
    for target, position, signature in _polyglot_imports(language, import_text):
        line = _line_for_offset(text, position)
        qualname = _cpp_qualname(rel, f"import::{target}@{line}", counts)
        entities.append(Entity(
            kind="import", name=target.split(".")[-1].split("::")[-1],
            qualname=qualname, file_path=rel, line_start=line, line_end=line,
            signature=signature, evidence_label=EXTRACTED,
            extractor=POLYGLOT_LEXICAL_EXTRACTOR_ID, confidence=1.0,
            source_hash=source_hash, build_revision=build_revision,
        ))
        edges.append(Edge(
            kind="imports", src_qualname=rel, dst_name=target, dst_qualname=None,
            file_path=rel, line=line, evidence_label=EXTRACTED,
            extractor=POLYGLOT_LEXICAL_EXTRACTOR_ID, confidence=1.0,
            source_hash=source_hash, build_revision=build_revision,
        ))

    type_ranges: list[tuple[int, int, str, str]] = []
    pending_inherits: list[tuple[str, str, int]] = []
    seen_type_positions: set[tuple[int, str]] = set()
    type_qualnames: dict[str, str] = {}
    for pattern in _polyglot_type_patterns(language):
        for match in pattern.finditer(masked):
            name = match.group("name").split("::")[-1]
            identity = (match.start(), name)
            if identity in seen_type_positions:
                continue
            seen_type_positions.add(identity)
            raw_kind = match.group("kind")
            opening = masked.rfind("{", match.start(), match.end())
            ending = _matching_delimiter(masked, opening, "{", "}")
            line = _line_for_offset(masked, match.start())
            if raw_kind == "impl":
                # Rust ``impl Type`` is an owner scope, not a second type
                # declaration. Reuse the declared type identity when it is
                # present and keep a stable inferred owner otherwise.
                qualname = type_qualnames.get(name, f"{rel}::{name}")
                type_ranges.append((opening, ending, name, qualname))
                trait = match.groupdict().get("trait")
                if trait:
                    pending_inherits.append((qualname, trait.split("::")[-1], line))
                continue
            kind = "enum" if raw_kind == "enum" else (
                "struct" if raw_kind in {"struct", "record", "type"} else "class"
            )
            qualname = _cpp_qualname(rel, name, counts)
            type_qualnames.setdefault(name, qualname)
            type_ranges.append((opening, ending, name, qualname))
            entities.append(Entity(
                kind=kind, name=name, qualname=qualname, file_path=rel,
                line_start=line, line_end=_line_for_offset(masked, ending),
                signature=_cpp_signature(text, match.start(), opening),
                evidence_label=EXTRACTED, extractor=POLYGLOT_LEXICAL_EXTRACTOR_ID,
                confidence=0.96, source_hash=source_hash, build_revision=build_revision,
            ))
            edges.append(Edge(
                kind="defines", src_qualname=rel, dst_name=name,
                dst_qualname=qualname, file_path=rel, line=line,
                evidence_label=EXTRACTED, extractor=POLYGLOT_LEXICAL_EXTRACTOR_ID,
                confidence=1.0, source_hash=source_hash, build_revision=build_revision,
            ))
            tail = match.groupdict().get("tail") or ""
            trait = match.groupdict().get("trait")
            if trait:
                pending_inherits.append((qualname, trait.split("::")[-1], line))
            for clause in re.findall(r"\b(?:extends|implements)\s+([^\{]+)", tail):
                for base in re.findall(r"[A-Za-z_]\w*(?:::\w+|\.\w+)*", clause):
                    pending_inherits.append((qualname, base.split("::")[-1].split(".")[-1], line))
            if language == "csharp" and ":" in tail:
                for base in re.findall(r"[A-Za-z_]\w*(?:\.\w+)*", tail.split(":", 1)[1]):
                    pending_inherits.append((qualname, base.split(".")[-1], line))

    def owner_for(position: int) -> tuple[int, int, str, str] | None:
        owners = [row for row in type_ranges if row[0] < position < row[1]]
        return min(owners, key=lambda row: row[1] - row[0]) if owners else None

    function_ranges: list[tuple[int, int, str, str]] = []
    seen_functions: set[tuple[int, str]] = set()
    for pattern in _polyglot_function_patterns(language):
        for match in pattern.finditer(masked):
            name = match.group("name")
            if language != "rust" and name in _POLYGLOT_CONTROL_NAMES:
                continue
            identity = (match.start(), name)
            if identity in seen_functions:
                continue
            seen_functions.add(identity)
            opening = masked.rfind("{", match.start(), match.end())
            ending = _matching_delimiter(masked, opening, "{", "}")
            if opening < 0 or ending <= opening:
                continue
            owner = owner_for(match.start())
            semantic_name = f"{owner[2]}::{name}" if owner else name
            qualname = _cpp_qualname(rel, semantic_name, counts)
            line = _line_for_offset(masked, match.start())
            entities.append(Entity(
                kind="method" if owner else "function", name=name,
                qualname=qualname, file_path=rel, line_start=line,
                line_end=_line_for_offset(masked, ending),
                signature=_cpp_signature(text, match.start(), opening),
                evidence_label=EXTRACTED, extractor=POLYGLOT_LEXICAL_EXTRACTOR_ID,
                confidence=0.93, source_hash=source_hash, build_revision=build_revision,
            ))
            edges.append(Edge(
                kind="defines", src_qualname=owner[3] if owner else rel,
                dst_name=name, dst_qualname=qualname, file_path=rel, line=line,
                evidence_label=EXTRACTED, extractor=POLYGLOT_LEXICAL_EXTRACTOR_ID,
                confidence=1.0, source_hash=source_hash, build_revision=build_revision,
            ))
            function_ranges.append((opening, ending, name, qualname))

    local_targets: dict[str, list[str]] = {}
    for entity in entities:
        if entity.kind in {"function", "method", "class", "struct", "enum"}:
            local_targets.setdefault(entity.name, []).append(entity.qualname)

    for src_qualname, base, line in pending_inherits:
        targets = local_targets.get(base, [])
        edges.append(Edge(
            kind="inherits", src_qualname=src_qualname, dst_name=base,
            dst_qualname=targets[0] if len(targets) == 1 else None,
            file_path=rel, line=line,
            evidence_label=EXTRACTED if len(targets) == 1 else INFERRED,
            extractor=POLYGLOT_LEXICAL_EXTRACTOR_ID,
            confidence=1.0 if len(targets) == 1 else 0.75,
            source_hash=source_hash, build_revision=build_revision,
        ))

    call_patterns = (
        re.compile(r"(?:\.|->|::)\s*([$A-Za-z_]\w*)\s*[!(]?\s*\("),
        re.compile(r"\b([$A-Za-z_]\w*)\s*[!(]?\s*\("),
    )
    for opening, ending, function_name, qualname in function_ranges:
        body_text = masked[opening + 1:ending]
        observed: set[tuple[str, int]] = set()
        for pattern in call_patterns:
            for call in pattern.finditer(body_text):
                called = call.group(1)
                if called in _POLYGLOT_CONTROL_NAMES or called == function_name:
                    continue
                line = _line_for_offset(masked, opening + 1 + call.start())
                key = (called, line)
                if key in observed:
                    continue
                observed.add(key)
                targets = local_targets.get(called, [])
                edges.append(Edge(
                    kind="calls", src_qualname=qualname, dst_name=called,
                    dst_qualname=targets[0] if len(targets) == 1 else None,
                    file_path=rel, line=line,
                    evidence_label=EXTRACTED if len(targets) == 1 else INFERRED,
                    extractor=POLYGLOT_LEXICAL_EXTRACTOR_ID,
                    confidence=1.0 if len(targets) == 1 else 0.65,
                    source_hash=source_hash, build_revision=build_revision,
                ))

    return FileExtraction(
        file_path=rel, language=language, status="ok", source_hash=source_hash,
        entities=tuple(entities), edges=tuple(edges),
    )


def _extract_file_evidence(
    rel: str, raw: bytes, language: str, source_hash: str, build_revision: str,
) -> FileExtraction:
    """One truthful ``kind="file"`` entity: exact path/language/size/hash.

    No function/class/call/import is claimed for the file's contents. This is
    used only for registered language families without a semantic adapter, so
    nothing beyond directly observed file facts (line count, byte size,
    language, hash) is recorded.
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


def _walk_lexical_scope(scope_node: ast.AST):
    """Yield nodes owned by one lexical scope, excluding nested scopes."""

    yield scope_node
    children = (
        scope_node.body
        if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        else ast.iter_child_nodes(scope_node)
    )
    for child in children:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Definition-time expressions execute in the enclosing lexical
            # scope.  The definition body itself belongs to the child scope.
            expressions: list[ast.AST] = list(child.decorator_list)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                expressions.extend(child.args.defaults)
                expressions.extend(
                    default for default in child.args.kw_defaults if default is not None
                )
                expressions.extend(
                    arg.annotation
                    for arg in (
                        *child.args.posonlyargs,
                        *child.args.args,
                        *child.args.kwonlyargs,
                    )
                    if arg.annotation is not None
                )
                if child.args.vararg and child.args.vararg.annotation is not None:
                    expressions.append(child.args.vararg.annotation)
                if child.args.kwarg and child.args.kwarg.annotation is not None:
                    expressions.append(child.args.kwarg.annotation)
                if child.returns is not None:
                    expressions.append(child.returns)
            else:
                expressions.extend(child.bases)
                expressions.extend(keyword.value for keyword in child.keywords)
            for expression in expressions:
                yield from _walk_lexical_scope(expression)
            continue
        yield from _walk_lexical_scope(child)


def _walk_enclosing_bindings(scope_node: ast.AST):
    """Yield enclosing-scope nodes without comprehension-local targets."""

    yield scope_node
    if isinstance(scope_node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        for generator in scope_node.generators:
            yield from _walk_enclosing_bindings(generator.iter)
            for condition in generator.ifs:
                yield from _walk_enclosing_bindings(condition)
        if isinstance(scope_node, ast.DictComp):
            yield from _walk_enclosing_bindings(scope_node.key)
            yield from _walk_enclosing_bindings(scope_node.value)
        else:
            yield from _walk_enclosing_bindings(scope_node.elt)
        return
    for child in ast.iter_child_nodes(scope_node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from _walk_enclosing_bindings(child)


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

    bound_names: set[str] = set()
    bound_qualnames: dict[str, str] = {}
    import_targets: dict[str, str] = {}
    scopes: list[tuple[ast.AST, str, str | None]] = []
    qualname_counts: dict[str, int] = {}
    node_qualnames: dict[ast.AST, str] = {}
    method_receivers: dict[ast.AST, str] = {}

    def _entity(kind: str, name: str, qualname: str, node: ast.AST, signature: str = "") -> None:
        entities.append(Entity(
            kind=kind, name=name, qualname=qualname, file_path=rel,
            line_start=getattr(node, "lineno", 1), line_end=_end_line(node),
            signature=signature, evidence_label=EXTRACTED, extractor=EXTRACTOR_ID,
            confidence=1.0, source_hash=source_hash, build_revision=build_revision,
        ))

    def _edge(kind: str, owner: str, name: str, qualname: str | None, node: ast.AST,
              label: str = EXTRACTED, confidence: float = 1.0) -> None:
        receiver_name = (
            node.value.id
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            else ""
        )
        edges.append(Edge(
            kind=kind, src_qualname=owner, dst_name=name, dst_qualname=qualname,
            file_path=rel, line=getattr(node, "lineno", 1), evidence_label=label,
            extractor=EXTRACTOR_ID, confidence=confidence, source_hash=source_hash,
            build_revision=build_revision,
            source_col=int(getattr(node, "col_offset", -1)),
            receiver_name=receiver_name,
        ))

    def _collect(node: ast.AST, owner: str, child_kind: str, class_owner: str | None) -> None:
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scopes.append((node, owner, class_owner))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = _dedupe_qualname(qualname_counts, f"{owner}.{child.name}")
                node_qualnames[child] = qualname
                bound_names.add(child.name)
                bound_qualnames[child.name] = qualname
                direct_method = isinstance(node, ast.ClassDef)
                kind = "method" if direct_method else "function"
                _entity(kind, child.name, qualname, child, _sig_of(child))
                _edge("defines", owner, child.name, qualname, child)
                decorator_nodes = (
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                    for decorator in child.decorator_list
                )
                decorators = {
                    decorator.id if isinstance(decorator, ast.Name) else decorator.attr
                    for decorator in decorator_nodes
                    if isinstance(decorator, (ast.Name, ast.Attribute))
                }
                positional = (*child.args.posonlyargs, *child.args.args)
                if direct_method and "staticmethod" not in decorators and positional:
                    receiver = positional[0].arg
                    if receiver in {"self", "cls"}:
                        method_receivers[child] = receiver
                _collect(child, qualname, "function", class_owner if direct_method else None)
            elif isinstance(child, ast.ClassDef):
                qualname = _dedupe_qualname(qualname_counts, f"{owner}.{child.name}")
                node_qualnames[child] = qualname
                bound_names.add(child.name)
                bound_qualnames[child.name] = qualname
                _entity("class", child.name, qualname, child, _sig_of(child))
                _edge("defines", owner, child.name, qualname, child)
                _collect(child, qualname, "method", qualname)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    dst = (alias.name if isinstance(child, ast.Import)
                           else f"{'.' * (child.level or 0)}{child.module or ''}.{alias.name}")
                    local_name = alias.asname or alias.name.split(".")[0]
                    bound_names.add(local_name)
                    import_targets[local_name] = dst
                    qualname = _dedupe_qualname(
                        qualname_counts, f"{module_qualname}::import::{local_name}::{child.lineno}"
                    )
                    _entity("import", local_name, qualname, child, dst)
                    _edge("imports", owner, dst, None, child)
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        bound_names.add(target.id)
                _collect(child, owner, child_kind, class_owner)
            else:
                _collect(child, owner, child_kind, class_owner)

    _collect(tree, module_qualname, "function", None)

    member_entities: dict[str, Entity] = {}
    exact_member_destinations: dict[ast.Attribute, str] = {}
    direct_member_identities: set[str] | None = None

    def _member_destination(
        attribute: ast.Attribute, class_owner: str | None, scope_node: ast.AST
    ) -> tuple[str | None, bool]:
        destination = exact_member_destinations.get(attribute)
        if (
            destination is not None
            and direct_member_identities is not None
            and destination not in direct_member_identities
        ):
            destination = None
        return destination, destination is not None

    def _define_member(
        owner: str, class_owner: str | None, scope_node: ast.AST, target: ast.AST
    ) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                _define_member(owner, class_owner, scope_node, element)
            return
        if isinstance(target, ast.Starred):
            _define_member(owner, class_owner, scope_node, target.value)
            return
        if isinstance(target, ast.Name):
            destination = f"{owner}.{target.id}" if owner == module_qualname or class_owner == owner else None
            name = target.id
        elif isinstance(target, ast.Attribute):
            destination, exact = _member_destination(target, class_owner, scope_node)
            destination = destination if exact else None
            name = target.attr
        else:
            return
        if destination is not None:
            member_entities.setdefault(destination, Entity(
                kind="attribute", name=name, qualname=destination, file_path=rel,
                line_start=getattr(target, "lineno", 1), line_end=_end_line(target),
                signature="definition", evidence_label=EXTRACTED, extractor=EXTRACTOR_ID,
                confidence=1.0, source_hash=source_hash, build_revision=build_revision,
            ))
        _edge(
            "writes", owner, name, destination, target,
            EXTRACTED if destination else INFERRED, 1.0 if destination else 0.5,
        )
        if destination is not None:
            _edge("defines", owner, name, destination, target)

    shadowed_receivers: dict[ast.Attribute, bool] = {}
    exact_direct_destinations: dict[ast.Name, str] = {}
    shadowed_direct_calls: set[ast.Name] = set()
    inherited_bindings: dict[ast.AST, dict[str, str]] = {tree: {}}
    class_owner_by_scope = {scope_node: class_owner for scope_node, _, class_owner in scopes}

    def _analyze_scope(scope_node: ast.AST) -> None:
        current_types = dict(inherited_bindings.get(scope_node, {}))
        rebound_names: set[str] = set()
        if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = scope_node.args
            parameter_names = {
                arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
            }
            if args.vararg:
                parameter_names.add(args.vararg.arg)
            if args.kwarg:
                parameter_names.add(args.kwarg.arg)
            # Python decides function locals for the whole body. A later
            # binding, including a class statement, therefore prevents an
            # earlier access from borrowing a same-named outer class. The
            # forward pass makes the local class authoritative only after its
            # statement executes.
            for statement in scope_node.body:
                if isinstance(
                    statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    parameter_names.add(statement.name)
                    continue
                parameter_names.update(
                    child.id for child in _walk_enclosing_bindings(statement)
                    if isinstance(child, ast.Name)
                    and isinstance(child.ctx, (ast.Store, ast.Del))
                )
                parameter_names.update(
                    child.name for child in _walk_lexical_scope(statement)
                    if isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name
                )
                parameter_names.update(
                    child.rest for child in _walk_lexical_scope(statement)
                    if isinstance(child, ast.MatchMapping) and child.rest
                )
                parameter_names.update(
                    child.name for child in _walk_lexical_scope(statement)
                    if isinstance(child, ast.ExceptHandler) and child.name
                )
                if isinstance(statement, (ast.Import, ast.ImportFrom)):
                    parameter_names.update(
                        alias.asname or alias.name.split(".")[0]
                        for alias in statement.names
                    )
            for name in parameter_names:
                current_types.pop(name, None)
                rebound_names.add(name)
            class_owner = class_owner_by_scope.get(scope_node)
            receiver = method_receivers.get(scope_node)
            if class_owner and receiver:
                current_types[receiver] = class_owner
                rebound_names.discard(receiver)

        def _invalidate(
            nodes: list[ast.AST], types: dict[str, str], rebound: set[str]
        ) -> None:
            names = {
                child.id for node in nodes for child in ast.walk(node)
                if isinstance(child, ast.Name)
                and isinstance(child.ctx, (ast.Store, ast.Del))
            }
            for name in names:
                types.pop(name, None)
                rebound.add(name)

        def _bind_expression_target(
            target: ast.AST,
            value: ast.AST,
            types: dict[str, str],
            rebound: set[str],
        ) -> None:
            _invalidate([target], types, rebound)
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and (class_qualname := types.get(value.func.id))
                and not class_qualname.startswith("::")
            ):
                types[target.id] = class_qualname
                rebound.discard(target.id)

        def _observe(node: ast.AST, types: dict[str, str], rebound: set[str]) -> None:
            """Observe expressions in evaluation order, applying local bindings."""

            if isinstance(node, ast.Lambda):
                # Lambdas are implicit function scopes.  In particular, a lambda
                # created in a class body does not close over that class namespace.
                parent_types = (
                    inherited_bindings.get(scope_node, {})
                    if isinstance(scope_node, ast.ClassDef)
                    else types
                )
                local_types, local_rebound = dict(parent_types), set(rebound)
                arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
                argument_names = {argument.arg for argument in arguments}
                if node.args.vararg:
                    argument_names.add(node.args.vararg.arg)
                if node.args.kwarg:
                    argument_names.add(node.args.kwarg.arg)
                for name in argument_names:
                    local_types.pop(name, None)
                    local_rebound.add(name)
                _observe(node.body, local_types, local_rebound)
                return
            if isinstance(node, ast.NamedExpr):
                _observe(node.value, types, rebound)
                _bind_expression_target(node.target, node.value, types, rebound)
                return
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                generators = node.generators
                if not generators:
                    return
                # The first iterable belongs to the surrounding scope. Everything
                # after it executes in the comprehension's implicit child scope.
                _observe(generators[0].iter, types, rebound)
                parent_types = (
                    inherited_bindings.get(scope_node, {})
                    if isinstance(scope_node, ast.ClassDef)
                    else types
                )
                local_types, local_rebound = dict(parent_types), set(rebound)
                for index, generator in enumerate(generators):
                    if index:
                        _observe(generator.iter, local_types, local_rebound)
                    _invalidate([generator.target], local_types, local_rebound)
                    for condition in generator.ifs:
                        _observe(condition, local_types, local_rebound)
                if isinstance(node, ast.DictComp):
                    _observe(node.key, local_types, local_rebound)
                    _observe(node.value, local_types, local_rebound)
                else:
                    _observe(node.elt, local_types, local_rebound)
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
            ):
                destination = types.get(node.func.id)
                if destination is not None and destination.startswith("::callable::"):
                    exact_direct_destinations[node.func] = destination.removeprefix(
                        "::callable::"
                    )
                elif destination is not None and not destination.startswith("::"):
                    exact_direct_destinations[node.func] = destination
                elif node.func.id in rebound:
                    shadowed_direct_calls.add(node.func)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                receiver_name = node.value.id
                destination = types.get(receiver_name)
                if destination is not None and not destination.startswith("::"):
                    exact_member_destinations[node] = f"{destination}.{node.attr}"
                shadowed_receivers[node] = receiver_name in rebound
            for child in ast.iter_child_nodes(node):
                _observe(child, types, rebound)

        def _process_block(
            statements: list[ast.stmt], types: dict[str, str], rebound: set[str]
        ) -> None:
            for statement in statements:
                _process_statement(statement, types, rebound)

        def _merge_flow_states(
            states: list[tuple[dict[str, str], set[str]]],
        ) -> tuple[dict[str, str], set[str]]:
            """Keep only type facts that agree on every reachable loop path."""

            if not states:
                return {}, set()
            common = dict(states[0][0])
            for name, destination in list(common.items()):
                if any(types.get(name) != destination for types, _ in states[1:]):
                    common.pop(name)
            rebound = set().union(*(names for _, names in states))
            rebound.update(
                name
                for types, _ in states
                for name in types
                if name not in common
            )
            return common, rebound

        def _process_statement(
            statement: ast.stmt, types: dict[str, str], rebound: set[str]
        ) -> None:
            # Nested scope bodies are analyzed exactly once in their own state.
            # Walking a definition as a statement would otherwise stamp its
            # body with the parent's receiver bindings before that child scope
            # gets a chance to apply parameters and local rebinding.
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                definition_expressions: list[ast.AST] = list(statement.decorator_list)
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    definition_expressions.extend(statement.args.defaults)
                    definition_expressions.extend(
                        default
                        for default in statement.args.kw_defaults
                        if default is not None
                    )
                    definition_expressions.extend(
                        argument.annotation
                        for argument in (
                            *statement.args.posonlyargs,
                            *statement.args.args,
                            *statement.args.kwonlyargs,
                        )
                        if argument.annotation is not None
                    )
                    if statement.args.vararg and statement.args.vararg.annotation:
                        definition_expressions.append(statement.args.vararg.annotation)
                    if statement.args.kwarg and statement.args.kwarg.annotation:
                        definition_expressions.append(statement.args.kwarg.annotation)
                    if statement.returns is not None:
                        definition_expressions.append(statement.returns)
                else:
                    definition_expressions.extend(statement.bases)
                    definition_expressions.extend(
                        keyword.value for keyword in statement.keywords
                    )
                for expression in definition_expressions:
                    _observe(expression, types, rebound)
                child_bindings = types
                if isinstance(scope_node, ast.ClassDef):
                    # Nested function and class bodies both skip their containing
                    # class namespace. Preserve only bindings that entered the
                    # class from its lexical parent; self/cls is seeded separately.
                    child_bindings = inherited_bindings.get(scope_node, {})
                inherited_bindings[statement] = dict(child_bindings)

            compound_blocks: list[list[ast.stmt]] = []
            header_nodes: list[ast.AST] = []
            body_targets: list[ast.AST] = []
            if isinstance(statement, ast.While):
                _observe(statement.test, types, rebound)
                zero_state = (dict(types), set(rebound))
                body_state = (dict(types), set(rebound))
                _process_block(statement.body, *body_state)
                else_types, else_rebound = _merge_flow_states(
                    [zero_state, body_state]
                )
                _process_block(statement.orelse, else_types, else_rebound)
                merged_types, merged_rebound = _merge_flow_states(
                    [zero_state, body_state, (else_types, else_rebound)]
                )
                types.clear()
                types.update(merged_types)
                rebound.clear()
                rebound.update(merged_rebound)
                return
            if isinstance(statement, (ast.For, ast.AsyncFor)):
                _observe(statement.iter, types, rebound)
                zero_state = (dict(types), set(rebound))
                body_types, body_rebound = dict(types), set(rebound)
                _invalidate([statement.target], body_types, body_rebound)
                _process_block(statement.body, body_types, body_rebound)
                body_state = (body_types, body_rebound)
                else_types, else_rebound = _merge_flow_states(
                    [zero_state, body_state]
                )
                _process_block(statement.orelse, else_types, else_rebound)
                merged_types, merged_rebound = _merge_flow_states(
                    [zero_state, body_state, (else_types, else_rebound)]
                )
                types.clear()
                types.update(merged_types)
                rebound.clear()
                rebound.update(merged_rebound)
                return
            if isinstance(statement, ast.If):
                header_nodes = [statement.test]
                compound_blocks = [statement.body, statement.orelse]
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                # With-items execute left to right: each context expression is
                # evaluated before that item's optional target is bound.
                for item in statement.items:
                    _observe(item.context_expr, types, rebound)
                    if item.optional_vars is not None:
                        _invalidate([item.optional_vars], types, rebound)
                _process_block(statement.body, dict(types), set(rebound))
                _invalidate([statement], types, rebound)
                return
            elif isinstance(statement, ast.Try):
                compound_blocks = [statement.body, statement.orelse]
                for handler in statement.handlers:
                    handler_types, handler_rebound = dict(types), set(rebound)
                    if handler.name:
                        handler_types.pop(handler.name, None)
                        handler_rebound.add(handler.name)
                    _observe(handler.type, handler_types, handler_rebound) if handler.type else None
                    _process_block(handler.body, handler_types, handler_rebound)
                compound_blocks.append(statement.finalbody)
            elif isinstance(statement, ast.Match):
                _observe(statement.subject, types, rebound)
                for case in statement.cases:
                    case_types, case_rebound = dict(types), set(rebound)
                    captured = [
                        child
                        for child in ast.walk(case.pattern)
                        if (
                            isinstance(child, (ast.MatchAs, ast.MatchStar))
                            and child.name
                        )
                        or (isinstance(child, ast.MatchMapping) and child.rest)
                    ]
                    for capture in captured:
                        name = capture.rest if isinstance(capture, ast.MatchMapping) else capture.name
                        case_types.pop(name, None)
                        case_rebound.add(name)
                    if case.guard is not None:
                        _observe(case.guard, case_types, case_rebound)
                    _process_block(case.body, case_types, case_rebound)
                _invalidate([statement], types, rebound)
                return

            if compound_blocks:
                for node in header_nodes:
                    _observe(node, types, rebound)
                if body_targets:
                    _invalidate(body_targets, types, rebound)
                for block in compound_blocks:
                    _process_block(block, dict(types), set(rebound))
                # A compound statement can take multiple paths.  Unless all
                # exits are proven equal, retaining an incoming exact type is
                # unsafe; invalidate every name any nested path may bind.
                _invalidate([statement], types, rebound)
                return

            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                _observe(statement, types, rebound)

            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                assigned = {
                    child.id
                    for target in targets
                    for child in ast.walk(target)
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
                }
                for name in assigned:
                    types.pop(name, None)
                    rebound.add(name)
                value = statement.value
                if (
                    len(assigned) == 1
                    and isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and (class_qualname := types.get(value.func.id))
                    and not class_qualname.startswith("::")
                ):
                    assigned_name = next(iter(assigned))
                    types[assigned_name] = class_qualname
                    rebound.discard(assigned_name)
            elif isinstance(statement, ast.ClassDef):
                types[statement.name] = node_qualnames[statement]
                rebound.discard(statement.name)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # The definition makes direct calls exact from this point on,
                # but a function result is not constructor/type evidence.
                types[statement.name] = f"::callable::{node_qualnames[statement]}"
                rebound.discard(statement.name)
            elif isinstance(statement, (ast.Import, ast.ImportFrom)):
                for alias in statement.names:
                    local_name = alias.asname or alias.name.split(".")[0]
                    destination = (
                        alias.name
                        if isinstance(statement, ast.Import)
                        else f"{'.' * (statement.level or 0)}"
                        f"{statement.module or ''}.{alias.name}"
                    )
                    # Keep imports in the lexical flow state for direct-name
                    # calls, but do not mistake module aliases for proven
                    # class receivers. Qualified import members are resolved
                    # by the bounded import reparser in source_graph.py.
                    types[local_name] = f"::import::{destination}"
                    rebound.discard(local_name)
            else:
                stored = {
                    child.id for child in _walk_enclosing_bindings(statement)
                    if isinstance(child, ast.Name)
                    and isinstance(child.ctx, (ast.Store, ast.Del))
                }
                if isinstance(statement, (ast.Import, ast.ImportFrom)):
                    stored.update(
                        alias.asname or alias.name.split(".")[0]
                        for alias in statement.names
                    )
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    stored.add(statement.name)
                stored.update(
                    child.name for child in _walk_lexical_scope(statement)
                    if isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name
                )
                stored.update(
                    child.rest for child in _walk_lexical_scope(statement)
                    if isinstance(child, ast.MatchMapping) and child.rest
                )
                stored.update(
                    child.name for child in _walk_lexical_scope(statement)
                    if isinstance(child, ast.ExceptHandler) and child.name
                )
                for name in stored:
                    types.pop(name, None)
                    rebound.add(name)

        _process_block(list(getattr(scope_node, "body", ())), current_types, rebound_names)

    for scope_node, _, _ in scopes:
        _analyze_scope(scope_node)

    # A proven receiver does not prove that an arbitrary member is defined
    # directly on that type.  Keep exact destinations only for identities
    # already extracted (methods/class attributes), or for exact member writes
    # that will themselves produce an attribute entity below.
    direct_member_identities = {entity.qualname for entity in entities}
    direct_member_identities.update(
        destination
        for attribute, destination in exact_member_destinations.items()
        if isinstance(attribute.ctx, (ast.Store, ast.Del))
    )

    for scope_node, owner, class_owner in scopes:
        instance_types_by_line: dict[int, dict[str, str]] = {}
        shadowed_names_by_line = {
            node.lineno: {node.func.value.id}
            for node in _walk_lexical_scope(scope_node)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and shadowed_receivers.get(node.func, False)
            )
        }
        _extract_calls(
            scope_node, owner, edges, rel, source_hash, build_revision,
            bound_names, bound_qualnames, class_owner, import_targets,
            instance_types_by_line, shadowed_names_by_line,
            {
                node: destination
                for node in _walk_lexical_scope(scope_node)
                if isinstance(node, ast.Attribute)
                for destination, exact in [_member_destination(node, class_owner, scope_node)]
                if exact and destination is not None
            },
            exact_direct_destinations,
            shadowed_direct_calls,
        )
        for node in _walk_lexical_scope(scope_node):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    _define_member(owner, class_owner, scope_node, target)
                if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
                    destination, exact = _member_destination(
                        node.target, class_owner, scope_node
                    )
                    _edge(
                        "references", owner, node.target.attr, destination, node.target,
                        EXTRACTED if exact else INFERRED, 1.0 if exact else 0.5,
                    )
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                destination, exact = _member_destination(node, class_owner, scope_node)
                _edge(
                    "references", owner, node.attr, destination, node,
                    EXTRACTED if exact else INFERRED, 1.0 if exact else 0.5,
                )

    entities.extend(member_entities.values())

    def _semantic_roles(node: ast.AST, lexical_owner: str) -> None:
        owner = node_qualnames.get(node, lexical_owner)

        def _role_names(expression: ast.AST, *, include_root: bool) -> list[tuple[str, ast.AST]]:
            attributes = [
                child for child in ast.walk(expression) if isinstance(child, ast.Attribute)
            ]
            facts = [(child.attr, child) for child in attributes]
            root = expression.func if isinstance(expression, ast.Call) else expression
            if include_root and isinstance(root, ast.Name):
                facts.insert(0, (root.id, root))
            elif not facts:
                if isinstance(root, ast.Attribute):
                    facts.append((root.attr, root))
                elif isinstance(root, ast.Name):
                    facts.append((root.id, root))
            return facts

        def _emit_role(kind: str, edge_kind: str, expression: ast.AST,
                       *, include_root: bool = False) -> None:
            for name, fact_node in _role_names(expression, include_root=include_root):
                line = getattr(fact_node, "lineno", getattr(expression, "lineno", 1))
                _edge(edge_kind, owner, name, None, fact_node, EXTRACTED, 1.0)
                _entity(
                    kind, name, f"{owner}::{kind}::{name}::{line}", fact_node,
                )

        for decorator in getattr(node, "decorator_list", ()):
            _emit_role("decorator", "decorates", decorator, include_root=True)

        annotations: list[ast.AST] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                annotations.append(node.returns)
            args = node.args
            annotations.extend(
                arg.annotation
                for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
                if arg.annotation is not None
            )
            if args.vararg and args.vararg.annotation:
                annotations.append(args.vararg.annotation)
            if args.kwarg and args.kwarg.annotation:
                annotations.append(args.kwarg.annotation)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)

        for annotation in annotations:
            _emit_role("annotation", "annotates", annotation)

        for child in ast.iter_child_nodes(node):
            _semantic_roles(child, owner)

    _semantic_roles(tree, module_qualname)

    for child in ast.walk(tree):
        if isinstance(child, ast.ClassDef):
            qualname = node_qualnames.get(child)
            if qualname is None:
                continue
            for base in child.bases:
                base_name = ast.unparse(base)
                if base_name:
                    _edge(
                        "inherits", qualname, base_name, bound_qualnames.get(base_name), child,
                        EXTRACTED if base_name in bound_names else AMBIGUOUS,
                        1.0 if base_name in bound_names else 0.5,
                    )

    unique_entities = {entity.qualname: entity for entity in entities}
    unique_edges = {
        (
            edge.kind,
            edge.src_qualname,
            edge.dst_name,
            edge.dst_qualname,
            edge.line,
            edge.source_col,
            edge.receiver_name,
        ): edge
        for edge in edges
    }
    return FileExtraction(
        file_path=rel, language="python", status="ok", source_hash=source_hash,
        entities=tuple(unique_entities[key] for key in sorted(unique_entities)),
        edges=tuple(unique_edges[key] for key in sorted(
            unique_edges,
            key=lambda item: (
                item[0], item[1], item[2], item[3] or "", item[4], item[5], item[6]
            ),
        )),
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
    class_owner: str | None = None,
    import_targets: dict[str, str] | None = None,
    instance_types_by_line: dict[int, dict[str, str]] | None = None,
    shadowed_names_by_line: dict[int, set[str]] | None = None,
    exact_member_destinations: dict[ast.Attribute, str] | None = None,
    exact_direct_destinations: dict[ast.Name, str] | None = None,
    shadowed_direct_calls: set[ast.Name] | None = None,
) -> None:
    """Record direct calls, resolving members only when receiver identity is proven."""

    import_targets = import_targets or {}
    instance_types_by_line = instance_types_by_line or {}
    shadowed_names_by_line = shadowed_names_by_line or {}
    exact_member_destinations = exact_member_destinations or {}
    exact_direct_destinations = exact_direct_destinations or {}
    shadowed_direct_calls = shadowed_direct_calls or set()
    lexical_import_names = {
        alias.asname or alias.name.split(".")[0]
        for statement in _walk_lexical_scope(scope_node)
        if isinstance(statement, (ast.Import, ast.ImportFrom))
        for alias in statement.names
    }
    for node in _walk_lexical_scope(scope_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        dst_qualname: str | None = None
        if isinstance(func, ast.Name):
            dst_name = func.id
            dst_qualname = exact_direct_destinations.get(func)
            if (
                dst_qualname is None
                and func not in shadowed_direct_calls
                and dst_name not in lexical_import_names
            ):
                dst_qualname = bound_qualnames.get(dst_name)
            exact = dst_qualname is not None
            label = EXTRACTED if exact else AMBIGUOUS
            confidence = 1.0 if exact else 0.4
        elif isinstance(func, ast.Attribute):
            dst_name = func.attr
            receiver = func.value
            dst_qualname = exact_member_destinations.get(func)
            if dst_qualname is None and isinstance(receiver, ast.Name):
                instance_type = instance_types_by_line.get(node.lineno, {}).get(receiver.id)
                if instance_type:
                    dst_qualname = f"{instance_type}.{dst_name}"
            label = EXTRACTED if dst_qualname else INFERRED
            confidence = 1.0 if dst_qualname else 0.6
        else:
            continue
        edges.append(Edge(
            kind="calls", src_qualname=owner_qualname, dst_name=dst_name,
            dst_qualname=dst_qualname, file_path=rel, line=node.lineno,
            evidence_label=label, extractor=EXTRACTOR_ID, confidence=confidence,
            source_hash=source_hash, build_revision=build_revision,
            source_col=int(getattr(func, "col_offset", -1)),
            receiver_name=(
                func.value.id
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                else ""
            ),
        ))


__all__ = [
    "AMBIGUOUS",
    "CPP_EXTENSIONS",
    "CPP_LEXICAL_EXTRACTOR_ID",
    "EDGE_KINDS",
    "ENTITY_KINDS",
    "EXTRACTED",
    "EXTRACTOR_ID",
    "FILE_EVIDENCE",
    "FILE_EVIDENCE_EXTRACTOR_ID",
    "INFERRED",
    "PHP_EXTENSIONS",
    "PHP_LEXICAL_EXTRACTOR_ID",
    "POLYGLOT_LEXICAL_EXTRACTOR_ID",
    "POLYGLOT_LEXICAL_LANGUAGES",
    "Edge",
    "Entity",
    "FileExtraction",
    "extract_file",
    "sha256_bytes",
]
