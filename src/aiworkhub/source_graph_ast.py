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
    "function", "method", "import", "file",
)
EDGE_KINDS = ("imports", "calls", "defines", "inherits")

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
