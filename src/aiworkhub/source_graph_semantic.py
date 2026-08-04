"""Optional tree-sitter semantic extraction for Source Graph.

The VSIX runtime must remain usable without site-packages, so this module has
no import-time dependency on tree-sitter.  When ``tree-sitter-language-pack``
is installed, JavaScript and TypeScript receive parser-backed declarations,
imports, inheritance and call evidence.  Otherwise the caller keeps the
existing conservative lexical extractor and reports that fallback truthfully.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any


EXTRACTOR_ID = "aiworkhub.source_graph.tree_sitter.javascript_typescript.v1"
SUPPORTED_LANGUAGES = frozenset({"javascript", "typescript"})


@dataclass(frozen=True, slots=True)
class SemanticExtraction:
    entities: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    parser_id: str
    has_parse_error: bool


def _parser_id(language: str, file_path: str = "") -> str:
    if language == "typescript" and Path(file_path).suffix.casefold() == ".tsx":
        return "tsx"
    return language


def parser_capability(language: str, *, file_path: str = "") -> dict[str, object]:
    """Return a truthful, non-throwing capability receipt for one language."""

    if language not in SUPPORTED_LANGUAGES:
        return {
            "available": False,
            "language": language,
            "backend": "tree_sitter_language_pack",
            "reason": "language_not_supported_by_semantic_adapter",
        }
    parser_id = _parser_id(language, file_path)
    try:
        from tree_sitter_language_pack import get_parser

        get_parser(parser_id)
        version = metadata.version("tree-sitter-language-pack")
    except (ImportError, metadata.PackageNotFoundError):
        return {
            "available": False,
            "language": language,
            "parser_id": parser_id,
            "backend": "tree_sitter_language_pack",
            "reason": "tree_sitter_language_pack_not_installed",
        }
    except Exception as exc:  # parser/ABI failures must degrade, never crash indexing
        return {
            "available": False,
            "language": language,
            "parser_id": parser_id,
            "backend": "tree_sitter_language_pack",
            "reason": f"parser_unavailable:{type(exc).__name__}",
        }
    return {
        "available": True,
        "language": language,
        "parser_id": parser_id,
        "backend": "tree_sitter_language_pack",
        "backend_version": version,
        "extractor": EXTRACTOR_ID,
        "reason": "",
    }


def _text(raw: bytes, node: Any) -> str:
    return raw[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _name(raw: bytes, node: Any, field: str = "name") -> str:
    child = node.child_by_field_name(field)
    return _text(raw, child).strip() if child is not None else ""


def _walk(node: Any):
    yield node
    for child in node.children:
        yield from _walk(child)


def _string_value(raw: bytes, node: Any | None) -> str:
    if node is None:
        return ""
    value = _text(raw, node).strip()
    if len(value) >= 2 and value[0] in {"'", '"', "`"} and value[-1] == value[0]:
        return value[1:-1]
    return value


def _signature(raw: bytes, node: Any) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    return " ".join(raw[node.start_byte:end].decode("utf-8", errors="replace").split())[:500]


def _unique_qualname(rel: str, semantic_name: str, counts: dict[str, int], line: int) -> str:
    base = f"{rel}::{semantic_name}"
    count = counts.get(base, 0)
    counts[base] = count + 1
    return base if count == 0 else f"{base}@{line}"


def _import_bindings(
    raw: bytes,
    root: Any,
    line_at: Any,
) -> tuple[list[dict[str, Any]], dict[str, str], set[str]]:
    imports: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    namespaces: set[str] = set()
    seen: set[tuple[str, int]] = set()
    for node in _walk(root):
        if node.type == "import_statement":
            target = _string_value(raw, node.child_by_field_name("source"))
            if target:
                line = line_at(node.start_byte)
                key = (target, line)
                if key not in seen:
                    seen.add(key)
                    imports.append({
                        "target": target,
                        "line": line,
                        "signature": " ".join(_text(raw, node).split())[:500],
                    })
            for descendant in _walk(node):
                if descendant.type == "import_specifier":
                    original = _name(raw, descendant)
                    alias = _name(raw, descendant, "alias") or original
                    if original and alias:
                        aliases[alias] = original
                elif descendant.type == "namespace_import":
                    identifiers = [
                        _text(raw, child) for child in descendant.named_children
                        if child.type == "identifier"
                    ]
                    if identifiers:
                        namespaces.add(identifiers[-1])
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if function is None or _text(raw, function) != "require" or arguments is None:
                continue
            strings = [child for child in arguments.named_children if child.type == "string"]
            if not strings:
                continue
            target = _string_value(raw, strings[0])
            line = line_at(node.start_byte)
            key = (target, line)
            if target and key not in seen:
                seen.add(key)
                imports.append({
                    "target": target,
                    "line": line,
                    "signature": _text(raw, node)[:500],
                })
    return imports, aliases, namespaces


def extract_javascript_typescript(
    *, file_path: str, raw: bytes, language: str,
) -> SemanticExtraction | None:
    """Return exact parser-backed evidence, or ``None`` for truthful fallback."""

    capability = parser_capability(language, file_path=file_path)
    if not capability["available"]:
        return None
    parser_id = str(capability["parser_id"])
    try:
        from tree_sitter_language_pack import get_parser

        # Keep the owning Tree alive for the complete traversal. Retaining
        # only ``parse(...).root_node`` leaves native node wrappers referring
        # to an already-finalized tree on some language-pack builds; small
        # fixtures may appear fine while a real, large extension.js crashes
        # the interpreter during traversal.
        # Keep both Parser and Tree alive.  Some language-pack builds expose
        # grammar/language storage through the Parser object; dropping the
        # temporary parser immediately after ``parse`` made traversal of a
        # larger tree nondeterministically dereference native state that had
        # already been released.
        parser = get_parser(parser_id)
        tree = parser.parse(raw)
        root = tree.root_node
    except Exception:
        return None

    # tree-sitter 0.26's Point accessor is unstable for some large language-
    # pack trees, while byte offsets remain stable.  Derive line numbers from
    # immutable source bytes; this is deterministic and avoids a native crash
    # in the indexer process.
    line_starts = [0]
    line_starts.extend(index + 1 for index, value in enumerate(raw) if value == 10)

    def line_at(byte_offset: int) -> int:
        return bisect_right(line_starts, int(byte_offset))

    line_count = max(1, len(line_starts))
    entities: list[dict[str, Any]] = [{
        "kind": "module", "name": file_path, "qualname": file_path,
        "line_start": 1, "line_end": line_count, "signature": "",
        "confidence": 1.0,
    }]
    edges: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    imports, aliases, namespaces = _import_bindings(raw, root, line_at)
    for row in imports:
        line = int(row["line"])
        target = str(row["target"])
        qualname = _unique_qualname(file_path, f"import::{target}", counts, line)
        entities.append({
            "kind": "import", "name": target.rsplit("/", 1)[-1],
            "qualname": qualname, "line_start": line, "line_end": line,
            "signature": row["signature"], "confidence": 1.0,
        })
        edges.append({
            "kind": "imports", "src_qualname": file_path,
            "dst_name": target, "dst_qualname": None, "line": line,
            "evidence_label": "EXTRACTED", "confidence": 1.0,
        })

    declarations = {
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "interface_declaration": "class",
        "enum_declaration": "enum",
        "type_alias_declaration": "struct",
    }
    callable_nodes = {
        "function_declaration", "generator_function_declaration", "method_definition",
        "method_signature", "function_expression", "generator_function",
    }
    type_qualnames: dict[int, str] = {}
    callable_qualnames: dict[int, str] = {}
    local_targets: dict[str, list[str]] = {}

    def add_entity(kind: str, name: str, node: Any, owner: str | None = None) -> str:
        semantic_name = f"{owner}::{name}" if owner else name
        line = line_at(node.start_byte)
        qualname = _unique_qualname(file_path, semantic_name, counts, line)
        entities.append({
            "kind": kind, "name": name, "qualname": qualname,
            "line_start": line, "line_end": line_at(node.end_byte),
            "signature": _signature(raw, node), "confidence": 1.0,
        })
        local_targets.setdefault(name, []).append(qualname)
        return qualname

    def collect(node: Any, owner_name: str | None = None, owner_qualname: str | None = None) -> None:
        current_owner_name = owner_name
        current_owner_qualname = owner_qualname
        if node.type in declarations:
            name = _name(raw, node)
            if name:
                qualname = add_entity(declarations[node.type], name, node)
                type_qualnames[node.id] = qualname
                edges.append({
                    "kind": "defines", "src_qualname": file_path,
                    "dst_name": name, "dst_qualname": qualname,
                    "line": line_at(node.start_byte),
                    "evidence_label": "EXTRACTED", "confidence": 1.0,
                })
                current_owner_name, current_owner_qualname = name, qualname
                for child in node.named_children:
                    if child.type not in {"class_heritage", "extends_type_clause"}:
                        continue
                    for descendant in _walk(child):
                        if descendant.type not in {"identifier", "type_identifier", "nested_type_identifier"}:
                            continue
                        base = _text(raw, descendant).split(".")[-1]
                        if base and base != name:
                            edges.append({
                                "kind": "inherits", "src_qualname": qualname,
                                "dst_name": base, "dst_qualname": None,
                                "line": line_at(descendant.start_byte),
                                "evidence_label": "EXTRACTED", "confidence": 1.0,
                            })
        elif node.type in callable_nodes:
            if node.parent is not None and node.parent.type in {
                "variable_declarator", "public_field_definition",
            }:
                for child in node.named_children:
                    collect(child, current_owner_name, current_owner_qualname)
                return
            name = _name(raw, node)
            if name:
                kind = "method" if current_owner_name or node.type in {"method_definition", "method_signature"} else "function"
                qualname = add_entity(kind, name, node, current_owner_name)
                callable_qualnames[node.id] = qualname
                edges.append({
                    "kind": "defines", "src_qualname": current_owner_qualname or file_path,
                    "dst_name": name, "dst_qualname": qualname,
                    "line": line_at(node.start_byte),
                    "evidence_label": "EXTRACTED", "confidence": 1.0,
                })
        elif node.type in {"variable_declarator", "public_field_definition"}:
            value = node.child_by_field_name("value")
            name = _name(raw, node)
            if value is not None and value.type in {"arrow_function", "function_expression", "generator_function"} and name:
                kind = "method" if current_owner_name else "function"
                qualname = add_entity(kind, name, node, current_owner_name)
                callable_qualnames[value.id] = qualname
                edges.append({
                    "kind": "defines", "src_qualname": current_owner_qualname or file_path,
                    "dst_name": name, "dst_qualname": qualname,
                    "line": line_at(node.start_byte),
                    "evidence_label": "EXTRACTED", "confidence": 1.0,
                })
        for child in node.named_children:
            collect(child, current_owner_name, current_owner_qualname)

    collect(root)

    def nearest_callable(node: Any) -> str | None:
        parent = node.parent
        while parent is not None:
            if parent.id in callable_qualnames:
                return callable_qualnames[parent.id]
            parent = parent.parent
        return None

    observed_calls: set[tuple[str, str, int]] = set()
    for node in _walk(root):
        if node.type not in {"call_expression", "new_expression"}:
            continue
        src = nearest_callable(node)
        if src is None:
            continue
        function = node.child_by_field_name("function") or node.child_by_field_name("constructor")
        if function is None:
            continue
        exact_binding = False
        if function.type in {"identifier", "type_identifier"}:
            observed_name = _text(raw, function)
            called = aliases.get(observed_name, observed_name)
            exact_binding = observed_name in aliases or called in local_targets
        elif function.type in {"member_expression", "subscript_expression"}:
            property_node = function.child_by_field_name("property")
            object_node = function.child_by_field_name("object")
            called = _text(raw, property_node) if property_node is not None else ""
            exact_binding = bool(
                object_node is not None and _text(raw, object_node) in namespaces and called
            )
        else:
            named = [child for child in function.named_children if child.type in {"identifier", "property_identifier", "type_identifier"}]
            called = _text(raw, named[-1]) if named else ""
        if not called or called in {"require", "if", "for", "while", "switch"}:
            continue
        line = line_at(node.start_byte)
        identity = (src, called, line)
        if identity in observed_calls:
            continue
        observed_calls.add(identity)
        targets = local_targets.get(called, [])
        dst_qualname = targets[0] if len(targets) == 1 else None
        label = "EXTRACTED" if dst_qualname is not None or exact_binding else "INFERRED"
        edges.append({
            "kind": "calls", "src_qualname": src, "dst_name": called,
            "dst_qualname": dst_qualname, "line": line,
            "evidence_label": label,
            "confidence": 1.0 if label == "EXTRACTED" else 0.75,
        })

    return SemanticExtraction(
        entities=tuple(entities), edges=tuple(edges), parser_id=parser_id,
        has_parse_error=bool(root.has_error),
    )


__all__ = [
    "EXTRACTOR_ID",
    "SUPPORTED_LANGUAGES",
    "SemanticExtraction",
    "extract_javascript_typescript",
    "parser_capability",
]
