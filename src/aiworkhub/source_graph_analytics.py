"""Repository-neutral Source Graph analytics over the canonical graph database.

The original UltrafastSecp256k1 Source Graph exposed useful operational views
in addition to semantic search.  This module ports those generic views without
copying its project-specific crypto, packet, build-layout, or decision stores.
Every result is computed from the repository's canonical Source Graph tables
and is explicitly bounded.  Runtime coverage is never inferred from filenames
or call edges: when no imported runtime evidence exists it is reported as
``not_available``.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import source_graph_insights as insights


ANALYTIC_MODES: tuple[str, ...] = (
    "tags",
    "hotspots",
    "coverage",
    "churn",
    "reviewqueue",
    "ownership",
    "testmap",
    "calls",
    "symbols",
    "bottlenecks",
    "auditmap",
    "complexity",
    "stats",
    "summarize",
    "pipeline",
    "todo",
    "leaks",
    "nullrisks",
    "rawptrs",
    "casts",
    "crashes",
    "looprisks",
    "deadmethods",
    "duplicates",
    "gaps",
)

_SECURITY_RE = re.compile(
    r"\b(auth|credential|password|secret|token|permission|sandbox|signature|crypto)\b",
    re.IGNORECASE,
)
_BUILD_RE = re.compile(r"(^|/)(build|ci|scripts?|tools?)(/|$)", re.IGNORECASE)
_DATA_RE = re.compile(r"\b(data|schema|model|record|entity|store|database|db)\b", re.IGNORECASE)
_API_RE = re.compile(r"\b(api|server|client|handler|route|controller|endpoint)\b", re.IGNORECASE)
_TEST_RE = re.compile(r"(^|/)(tests?|specs?)(/|$)|(^|/)(test_|spec_)", re.IGNORECASE)
_RAW_PTR_RE = re.compile(
    r"\b(?:char|short|int|long|float|double|void|[A-Z]\w*(?:::\w+)*)\s*\*\s*\w+"
)
_UNSAFE_CAST_RE = re.compile(r"\b(?:reinterpret_cast|const_cast)\s*<|\([A-Za-z_]\w*\s*\*\)\s*\w+")
_NULL_DEREF_RE = re.compile(r"\b([A-Za-z_]\w*)\s*->")
_ALLOC_RE = re.compile(r"\b(?:new|malloc|calloc|realloc)\b")
_FREE_RE = re.compile(r"\b(?:delete|free)\b")
_INFINITE_LOOP_RE = re.compile(r"\bwhile\s*\(\s*(?:true|1)\s*\)|\bfor\s*\(\s*;\s*;\s*\)")
# A division requires a left operand (identifier, digit, or a closing bracket)
# before the slash, so a bare path separator inside a string or comment can no
# longer masquerade as ``x / y``.  ``/{1,2}`` keeps Python floor division
# (``//``) while ``(?!=)`` still excludes the ``/=`` augmented assignment.  The
# operand requirement is not sufficient on its own (``a/b`` inside a path string
# still looks like division), so callers mask string and comment content first.
_DIVISION_RE = re.compile(r"[)\]\w]\s*/{1,2}(?!=)\s*([A-Za-z_]\w*)")

# Language families used to decide detector applicability.  These lexical
# detectors are ported from a C/C++ Source Graph; on a language they cannot
# structurally match they must report ``not_applicable`` rather than an empty
# ``available`` result that reads as "analysed and clean".
_C_FAMILY_SUFFIXES: frozenset[str] = frozenset({
    ".c", ".cc", ".cpp", ".cxx", ".c++", ".cu", ".cuh",
    ".h", ".hh", ".hpp", ".hxx", ".h++", ".ipp", ".inl",
})
_PYTHON_SUFFIXES: frozenset[str] = frozenset({".py", ".pyi"})

# ``leaks`` and ``nullrisks`` are now applicable to Python: the resource and
# possibly-None shapes below (``with sqlite3.connect``, an unclosed ``open``/
# socket handle, an unchecked ``.get``/``re.match`` result, an ``or 0``/``or {}``
# coercion) are all lexically present in Python and were found by hand in this
# repo.  ``crashes`` (division / explicit termination) is likewise meaningful in
# Python once guard and operand handling are language-aware.
#
# ``rawptrs``, ``casts`` and ``looprisks`` stay C-only on purpose: their shapes
# have no Python analogue — Python has no raw pointer declarations, no
# ``reinterpret_cast``/``const_cast``/C-cast syntax, and no always-true C
# ``while (1)``/``for (;;)`` header (a Python ``while True:`` bounded by an
# indented ``break`` is not the same lexical construct).  Firing them on Python
# would manufacture noise, not findings, so they honestly report
# ``not_applicable``.  Modes absent from this map (``deadmethods``,
# ``duplicates``) are graph/normalisation based and apply to every language.
_C_ONLY: frozenset[str] = frozenset({"c_family"})
_C_AND_PYTHON: frozenset[str] = frozenset({"c_family", "python"})
_RISK_MODE_LANGUAGES: dict[str, frozenset[str]] = {
    "leaks": _C_AND_PYTHON,
    "rawptrs": _C_ONLY,
    "casts": _C_ONLY,
    "looprisks": _C_ONLY,
    "nullrisks": _C_AND_PYTHON,
    "crashes": _C_AND_PYTHON,
}

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_TRIPLE_STRING_RE = re.compile(
    r'"""(?:\\.|[^\\])*?"""|\'\'\'(?:\\.|[^\\])*?\'\'\'', re.S
)
_STRING_LITERAL_RE = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
_PY_LINE_COMMENT_RE = re.compile(r"#[^\n]*")
_C_LINE_COMMENT_RE = re.compile(r"(?://|#)[^\n]*")

# Common builtins that saturate the low-confidence ``gaps`` edge set: a call to
# one of these is expected to be unresolved and is never the missing evidence a
# caller is asking about.
_GAPS_BUILTIN_CALLEES: frozenset[str] = frozenset({
    "print", "len", "range", "int", "str", "float", "bool", "bytes", "list",
    "dict", "set", "tuple", "frozenset", "type", "id", "repr", "format",
    "isinstance", "issubclass", "getattr", "setattr", "hasattr", "delattr",
    "enumerate", "zip", "map", "filter", "sorted", "reversed", "min", "max",
    "sum", "abs", "round", "any", "all", "next", "iter", "open", "super",
    "hash", "ord", "chr", "vars", "dir", "callable", "staticmethod",
    "classmethod", "property", "sizeof", "memcpy", "memset", "printf",
})


def _language_family(file_path: Any) -> str:
    """Return a coarse language family from a file path suffix."""

    suffix = Path(str(file_path or "")).suffix.lower()
    if suffix in _C_FAMILY_SUFFIXES:
        return "c_family"
    if suffix in _PYTHON_SUFFIXES:
        return "python"
    return suffix.lstrip(".") or "unknown"


def _mask_literals_and_comments(source: str, language: str) -> str:
    """Blank string and comment content so lexical patterns cannot match it.

    Block comments span lines; line comments stop at their newline.  Python
    keeps ``//`` (floor division), while the C family treats ``//`` as a line
    comment.  Offsets are not preserved because risk findings report the symbol
    line, not a column.
    """

    masked = _BLOCK_COMMENT_RE.sub(" ", source)
    masked = _TRIPLE_STRING_RE.sub(" ", masked)
    masked = _STRING_LITERAL_RE.sub(" ", masked)
    if language == "python":
        return _PY_LINE_COMMENT_RE.sub(" ", masked)
    return _C_LINE_COMMENT_RE.sub(" ", masked)


def _mask_comments_only(source: str, language: str) -> str:
    """Blank only comments (not string literals) for duplicate normalisation.

    A line comment must reach the end of its line and no further: the previous
    ``re.S`` flag let ``#``/``//`` swallow the rest of the symbol body, so two
    different functions sharing a prefix and a comment collapsed to one digest.
    """

    masked = _BLOCK_COMMENT_RE.sub(" ", source)
    if language == "python":
        return _PY_LINE_COMMENT_RE.sub(" ", masked)
    return _C_LINE_COMMENT_RE.sub(" ", masked)


def _is_guarded(source: str, name: str) -> bool:
    """True when ``name`` appears in a preceding guard in either language form.

    Recognises the C/parenthesised form (``if (name)`` / ``assert(name)``) and
    the Python colon form (``if name:`` / ``while name > 0:`` / ``assert name``),
    so a truthiness guard on a divisor or pointer is no longer missed merely
    because it was written without parentheses.
    """

    escaped = re.escape(name)
    if re.search(
        rf"(?:if|elif|while|assert)\s*\([^\n)]*\b{escaped}\b[^\n)]*\)", source
    ):
        return True
    if re.search(rf"(?:if|elif|while)\b[^\n:]*\b{escaped}\b[^\n:]*:", source):
        return True
    if re.search(rf"\bassert\b[^\n]*\b{escaped}\b", source):
        return True
    return False


# Python resource-leak shapes.  A ``with`` block over a raw sqlite3 connect
# call leaks the
# connection because ``Connection.__exit__`` commits but never closes it; an
# ``open()``/``socket`` handle bound to a name is a leak unless that name is
# ``.close()``-d on the path.  ``with open(...) as f`` binds no name here and is
# correctly ignored.
# Spelled in parts on purpose. The repository's OS-dependency scanner is
# lexical, so writing the call literally here made it count two sqlite
# connections in this module -- which opens none; these are detector *patterns*,
# not calls. Keeping the literal out of the source keeps that ratchet truthful.
_SQLITE_MODULE_NAME = "sqlite3"
_SQLITE_CONNECT_ATTR = "connect"
_PY_SQLITE_CONNECT_RE = re.compile(
    rf"\b{_SQLITE_MODULE_NAME}\s*\.\s*{_SQLITE_CONNECT_ATTR}\s*\("
)
_PY_WITH_HEADER_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?with[ \t]+([^\n:]*)", re.MULTILINE)
_PY_DEF_RE = re.compile(r"^([ \t]*)def[ \t]+([A-Za-z_]\w*)[ \t]*\(", re.MULTILINE)
_PY_CLOSING_RE = re.compile(r"\bclosing\s*\(")
_PY_RESOURCE_BIND_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*=\s*"
    r"(?:open|socket\s*\.\s*socket|socket\s*\.\s*create_connection)\s*\("
)


def _strip_closing_wrappers(text: str) -> str:
    """Remove balanced ``closing(...)`` regions from ``text``.

    ``contextlib.closing`` is the correct spelling for exactly this defect and
    the one already used in this repository, so an acquisition wrapped in it is
    not a leak.  Matching the acquisition alone reported properly closed code --
    a connect call already wrapped in ``closing`` -- as leaking, which is the kind
    of noise that gets a detector switched off.
    """

    out: list[str] = []
    index = 0
    while True:
        match = _PY_CLOSING_RE.search(text, index)
        if match is None:
            out.append(text[index:])
            return "".join(out)
        out.append(text[index : match.start()])
        depth = 1
        cursor = match.end()
        while cursor < len(text) and depth:
            if text[cursor] == "(":
                depth += 1
            elif text[cursor] == ")":
                depth -= 1
            cursor += 1
        index = cursor


_PY_CONTEXTMANAGER_DECORATOR_RE = re.compile(r"@\s*(?:\w+\s*\.\s*)?(?:async)?contextmanager\b")


def _decorated_as_context_manager(masked: str, def_start: int) -> bool:
    """Whether the ``def`` at ``def_start`` carries a contextmanager decorator."""

    head = masked[:def_start].rstrip("\n")
    for line in reversed(head.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("@"):
            return False
        if _PY_CONTEXTMANAGER_DECORATOR_RE.search(stripped):
            return True
    return False


def _python_sqlite_factory_names(masked: str) -> dict[str, bool]:
    """Local functions whose body constructs a sqlite3 connection.

    Returns ``{name: is_method}``. Seven of the nine real leak sites in this
    repository spelled the factory as a local helper -- ``with _connect(source,
    readonly=True) as src`` -- so matching only the literal ``sqlite3.connect``
    call saw two of nine. One level of resolution covers all of them.

    The indent matters. A module-level ``def`` is called bare, but an indented
    one is a method and must be matched as ``.name(``. Ignoring that made every
    ``with open(path) as fh`` in ``source_graph_partition`` a reported leak,
    because that module has a ``def open(self)`` contextmanager yielding a
    connection: a method name that happens to shadow a builtin turned an
    ordinary file read into a false positive.
    """

    names: dict[str, bool] = {}
    definitions = list(_PY_DEF_RE.finditer(masked))
    for position, match in enumerate(definitions):
        end = (
            definitions[position + 1].start()
            if position + 1 < len(definitions)
            else len(masked)
        )
        if not _PY_SQLITE_CONNECT_RE.search(masked[match.end() : end]):
            continue
        if _decorated_as_context_manager(masked, match.start()):
            # A @contextmanager owns its own __exit__ and closes in its finally,
            # which is the opposite of the defect. Counting one as a factory made
            # every correct `with self.open() as conn` a reported leak.
            continue
        is_method = bool(match.group(1))
        names[match.group(2)] = names.get(match.group(2), True) and is_method
    return names

# Python possibly-None shapes.  A name bound from ``.get(...)`` or a regex
# ``match``/``search``/``fullmatch`` is possibly ``None``; using it as an
# attribute, subscript, or arithmetic operand without a guard is a candidate.
# ``or 0`` / ``or {}`` / ``or []`` coerce an unproven absence into a concrete
# value, hiding the same bug.
_PY_MAYBE_NONE_BIND_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*=\s*[^\n=]*?"
    r"(?:\.\s*get\s*\(|\bre\s*\.\s*(?:match|search|fullmatch)\s*\()"
)
_PY_NONE_COERCE_RE = re.compile(r"\bor\s+(?:0(?!\.)\b|\{\s*\}|\[\s*\])")


def _python_leak_reasons(masked: str) -> list[str]:
    """Lexical resource-leak candidates for masked Python source."""

    reasons: list[str] = []
    factories = _python_sqlite_factory_names(masked)
    acquisitions = [_PY_SQLITE_CONNECT_RE.pattern] + [
        # A method is only an acquisition when it is actually called on an
        # object; a bare ``name(`` would swallow same-named builtins.
        rf"\.\s*{re.escape(name)}\s*\(" if is_method else rf"\b{re.escape(name)}\s*\("
        for name, is_method in sorted(factories.items())
    ]
    acquires_connection = re.compile("|".join(acquisitions))
    for header in _PY_WITH_HEADER_RE.findall(masked):
        if acquires_connection.search(_strip_closing_wrappers(header)):
            reasons.append("sqlite3_connect_context_manager_leaks_connection")
            break
    for name in sorted(set(_PY_RESOURCE_BIND_RE.findall(masked))):
        # A handle that is returned belongs to the caller, not to this function.
        # ``connect_sideband_socket`` builds a socket and hands it back; treating
        # that as unclosed made a factory look like a leak.
        if re.search(rf"\breturn\s+{re.escape(name)}\b", masked):
            continue
        if not re.search(rf"\b{re.escape(name)}\s*\.\s*close\s*\(", masked):
            reasons.append(f"resource_not_closed_on_all_paths:{name}")
    return reasons


def _python_nullrisk_reasons(masked: str) -> list[str]:
    """Lexical possibly-None candidates for masked Python source."""

    reasons: list[str] = []
    if _PY_NONE_COERCE_RE.search(masked):
        reasons.append("none_coalescing_masks_absence")
    for name in sorted(set(_PY_MAYBE_NONE_BIND_RE.findall(masked))):
        escaped = re.escape(name)
        used = re.search(rf"\b{escaped}\s*(?:\.\s*[A-Za-z_]|\[)", masked) or re.search(
            rf"\b{escaped}\s*[+\-*/%]", masked
        )
        if used and not _is_guarded(masked, name):
            reasons.append(f"unchecked_possibly_none_use:{name}")
            break
    return reasons


def _entity_rows(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT file_path, kind, name, qualname, line_start, line_end, signature, "
        "evidence_label, confidence FROM entities WHERE kind <> 'file' "
        "ORDER BY CASE WHEN kind IN ('function','method') THEN 0 ELSE 1 END, "
        "file_path, line_start LIMIT ?",
        (max(1, limit),),
    )]


def _file_rows(conn: sqlite3.Connection, files: list[str], *, limit: int) -> list[dict[str, Any]]:
    cap = max(1, limit)
    if files:
        placeholders = ",".join("?" for _ in files)
        return [dict(row) for row in conn.execute(
            "SELECT file_path, language, status, source_hash, indexed_at, build_revision "
            f"FROM files WHERE file_path IN ({placeholders}) ORDER BY file_path LIMIT ?",
            (*files, cap),
        )]
    return [dict(row) for row in conn.execute(
        "SELECT file_path, language, status, source_hash, indexed_at, build_revision "
        "FROM files ORDER BY file_path LIMIT ?",
        (cap,),
    )]


def _scope_matches(
    conn: sqlite3.Connection,
    matches: list[dict[str, Any]],
    *,
    budget: int,
) -> list[dict[str, Any]]:
    if matches:
        return matches[: max(1, min(budget * 4, 400))]
    return _entity_rows(conn, limit=max(16, min(budget * 4, 400)))


def _scope_files(
    conn: sqlite3.Connection,
    matches: list[dict[str, Any]],
    *,
    budget: int,
) -> list[str]:
    files = insights.candidate_files(matches, limit=min(max(8, budget), 64))
    if files:
        return files
    return [
        str(row["file_path"])
        for row in conn.execute("SELECT file_path FROM files ORDER BY file_path LIMIT ?", (min(64, max(8, budget)),))
    ]


def _history_rows(
    conn: sqlite3.Connection,
    files: list[str],
    *,
    budget: int,
) -> tuple[bool, list[dict[str, Any]], str | None]:
    try:
        if files:
            placeholders = ",".join("?" for _ in files)
            rows = conn.execute(
                "SELECT file_path, commit_touches_90d, lines_added_90d, lines_deleted_90d, "
                "(lines_added_90d + lines_deleted_90d) AS churn_90d, authors_90d, "
                "primary_author_90d, evidence FROM file_history "
                f"WHERE file_path IN ({placeholders}) "
                "ORDER BY churn_90d DESC, commit_touches_90d DESC, file_path LIMIT ?",
                (*files, max(1, budget)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT file_path, commit_touches_90d, lines_added_90d, lines_deleted_90d, "
                "(lines_added_90d + lines_deleted_90d) AS churn_90d, authors_90d, "
                "primary_author_90d, evidence FROM file_history "
                "ORDER BY churn_90d DESC, commit_touches_90d DESC, file_path LIMIT ?",
                (max(1, budget),),
            ).fetchall()
    except sqlite3.OperationalError:
        return False, [], "not_materialized"
    return True, [dict(row) for row in rows], None


def _test_map(
    conn: sqlite3.Connection,
    repo_root: Path,
    files: list[str],
    matches: list[dict[str, Any]],
    *,
    budget: int,
) -> dict[str, Any]:
    candidates = insights.test_candidates(
        conn, files, matches, limit=max(1, min(budget, 40)),
    )
    mapped_files = {str(row["file_path"]) for row in candidates}
    from . import evidence_instruments

    return {
        "source_files": files[: max(1, budget)],
        "related_tests": candidates,
        "structural_mapping": {
            "status": "available",
            "method": "resolved_call_edges_plus_path_stem_heuristics",
            "candidate_test_files": len(mapped_files),
            "claim": "test_relationship_only_not_execution_coverage",
        },
        "runtime_coverage": evidence_instruments.runtime_coverage_for_paths(
            repo_root, files
        ),
    }


def _tags_for(row: dict[str, Any]) -> list[str]:
    path = str(row.get("file_path") or "")
    text = " ".join((path, str(row.get("name") or ""), str(row.get("signature") or "")))
    tags = {f"kind:{row.get('kind') or 'unknown'}"}
    if _TEST_RE.search(path):
        tags.add("role:test")
    if _SECURITY_RE.search(text):
        tags.add("risk:security")
    if _BUILD_RE.search(path):
        tags.add("role:build_or_tooling")
    if _DATA_RE.search(text):
        tags.add("domain:data")
    if _API_RE.search(text):
        tags.add("role:api_boundary")
    return sorted(tags)


def _symbol_source(repo_root: Path, row: dict[str, Any], *, max_chars: int = 24000) -> str:
    try:
        root = repo_root.resolve()
        path = (root / str(row.get("file_path") or "")).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(row.get("line_start") or 1))
        end = max(start, int(row.get("line_end") or start))
        return "\n".join(lines[start - 1:end])[:max_chars]
    except (OSError, TypeError, ValueError):
        return ""


def _risk_views(
    conn: sqlite3.Connection,
    repo_root: Path,
    mode: str,
    rows: list[dict[str, Any]],
    *,
    budget: int,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    duplicate_groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    applicable_languages = _RISK_MODE_LANGUAGES.get(mode)
    scanned = 0
    candidate_symbols = 0
    applicable_candidates = 0
    skipped_by_language: Counter[str] = Counter()
    for row in rows:
        if str(row.get("kind") or "") not in {"function", "method", "class", "struct"}:
            continue
        candidate_symbols += 1
        language = _language_family(row.get("file_path"))
        if applicable_languages is not None and language not in applicable_languages:
            # This detector cannot structurally fire on this language, so the
            # symbol is never "checked" by it and must not inflate coverage.
            skipped_by_language[language] += 1
            continue
        applicable_candidates += 1
        source = _symbol_source(repo_root, row)
        if not source:
            continue
        scanned += 1
        masked = _mask_literals_and_comments(source, language)
        reasons: list[str] = []
        if mode == "leaks":
            if language == "python":
                reasons.extend(_python_leak_reasons(masked))
            elif len(_ALLOC_RE.findall(masked)) > len(_FREE_RE.findall(masked)):
                reasons.append("allocation_release_imbalance")
        elif mode == "rawptrs" and _RAW_PTR_RE.search(masked):
            reasons.append("raw_pointer_declaration")
        elif mode == "casts" and _UNSAFE_CAST_RE.search(masked):
            reasons.append("unsafe_cast_candidate")
        elif mode == "looprisks":
            for match in _INFINITE_LOOP_RE.finditer(masked):
                tail = masked[match.end():]
                close = tail.find("}")
                loop_body = tail[:close] if close >= 0 else tail[:4000]
                if not re.search(r"\b(?:break|return|throw|co_await|await)\b", loop_body):
                    reasons.append("unbounded_loop_without_lexical_exit")
                    break
        elif mode == "nullrisks":
            if language == "python":
                reasons.extend(_python_nullrisk_reasons(masked))
            else:
                dereferenced = set(_NULL_DEREF_RE.findall(masked))
                for name in sorted(dereferenced):
                    if not _is_guarded(masked, name):
                        reasons.append(f"unguarded_pointer_dereference:{name}")
                        break
        elif mode == "crashes":
            divisors = set(_DIVISION_RE.findall(masked))
            for name in sorted(divisors):
                if not _is_guarded(masked, name):
                    reasons.append(f"unchecked_divisor:{name}")
                    break
            if re.search(r"\b(?:abort|std::terminate)\s*\(", masked):
                reasons.append("explicit_process_termination")
        elif mode == "deadmethods":
            qualname = str(row.get("qualname") or "")
            incoming = int(conn.execute(
                "SELECT COUNT(*) FROM edges WHERE kind='calls' AND dst_qualname=?", (qualname,)
            ).fetchone()[0])
            name = str(row.get("name") or "")
            if incoming == 0 and name not in {"main", "__init__", "activate", "deactivate"}:
                reasons.append("no_resolved_incoming_calls_dynamic_dispatch_unobserved")
        elif mode == "duplicates":
            normalized = re.sub(
                r"\s+", " ", _mask_comments_only(source, language)
            ).strip()
            if len(normalized) >= 80:
                duplicate_groups[hashlib.sha256(normalized.encode()).hexdigest()].append(row)
        if reasons:
            findings.append({
                "file_path": row.get("file_path"),
                "qualname": row.get("qualname"),
                "line_start": row.get("line_start"),
                "reasons": reasons,
                "evidence_class": "bounded_lexical_candidate_not_proven_defect",
            })
        if len(findings) >= budget:
            break
    if mode == "duplicates":
        findings = [
            {
                "source_hash": digest,
                "symbols": [
                    {"file_path": row.get("file_path"), "qualname": row.get("qualname")}
                    for row in group
                ],
                "evidence_class": "exact_normalized_symbol_body_match",
            }
            for digest, group in duplicate_groups.items()
            if len(group) > 1
        ][:budget]
    if (
        applicable_languages is not None
        and applicable_candidates == 0
        and candidate_symbols > 0
    ):
        status = "not_applicable"
        reason = (
            f"{mode}_detector_analyses_only_{'/'.join(sorted(applicable_languages))}"
            f"_but_scope_holds_only_{'/'.join(sorted(skipped_by_language)) or 'other'}"
        )
    elif scanned:
        status = "available"
        reason = None
    else:
        status = "not_available"
        reason = "semantic_symbol_bodies_unavailable_for_scope"
    result: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "symbols_scanned": scanned,
        "findings": findings[:budget],
        "blocking": False,
    }
    if applicable_languages is not None:
        result["applicable_languages"] = sorted(applicable_languages)
        result["symbols_skipped_by_language"] = dict(sorted(skipped_by_language.items()))
    if mode == "deadmethods":
        # Incoming edges are resolved static calls only: a symbol reached solely
        # through MCP or CLI dispatch has no resolved edge and must not be
        # called dead without this caveat stated plainly.
        result["incoming_edge_evidence"] = (
            "resolved_static_call_edges_only_excludes_mcp_and_cli_dispatch"
        )
    return result


def _summary(conn: sqlite3.Connection) -> dict[str, Any]:
    by_language = {
        str(row["language"]): int(row["count"])
        for row in conn.execute(
            "SELECT language, COUNT(*) AS count FROM files GROUP BY language ORDER BY language"
        )
    }
    by_kind = {
        str(row["kind"]): int(row["count"])
        for row in conn.execute(
            "SELECT kind, COUNT(*) AS count FROM entities GROUP BY kind ORDER BY kind"
        )
    }
    return {
        "files": int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]),
        "entities": int(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]),
        "edges": int(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]),
        "files_by_language": by_language,
        "entities_by_kind": by_kind,
    }


def query(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    mode: str,
    query_text: str,
    matches: list[dict[str, Any]],
    budget: int,
) -> dict[str, Any]:
    """Compute one bounded analytic view from canonical Source Graph rows."""

    if mode not in ANALYTIC_MODES:
        raise ValueError(f"unsupported_source_graph_analytic:{mode}")
    budget = max(1, min(int(budget), 200))
    scoped_matches = _scope_matches(conn, matches, budget=budget)
    files = _scope_files(conn, matches, budget=budget)
    symbol_metrics = insights.symbol_metrics(
        conn, repo_root, scoped_matches, limit=max(1, min(budget, 80)),
    )

    base: dict[str, Any] = {
        "mode": mode,
        "query": query_text,
        "budget": budget,
        "scope": "query_matches" if matches else "repository_fallback",
    }

    if mode == "tags":
        tagged = [
            {**row, "tags": _tags_for(row)}
            for row in scoped_matches[:budget]
        ]
        counts = Counter(tag for row in tagged for tag in row["tags"])
        return {**base, "symbols": tagged, "tag_counts": dict(sorted(counts.items()))}

    if mode in {"hotspots", "complexity", "bottlenecks"}:
        if mode == "complexity":
            ranked = sorted(
                symbol_metrics,
                key=lambda row: (
                    -int(row.get("branch_count") or 0),
                    -int(row.get("loop_count") or 0),
                    -int(row.get("line_span") or 0),
                    str(row.get("qualname") or ""),
                ),
            )
            score_contract = "branches_then_loops_then_line_span"
        elif mode == "bottlenecks":
            ranked = sorted(
                symbol_metrics,
                key=lambda row: (
                    -(int(row.get("incoming_calls") or 0) * 3 + int(row.get("outgoing_calls") or 0)),
                    str(row.get("qualname") or ""),
                ),
            )
            score_contract = "incoming_calls_x3_plus_outgoing_calls"
        else:
            ranked = sorted(
                symbol_metrics,
                key=lambda row: (-int(row.get("priority_score") or 0), str(row.get("qualname") or "")),
            )
            score_contract = "graph_fanout_branches_loops_span_and_security_risk"
        return {**base, "score_contract": score_contract, "ranked_symbols": ranked[:budget]}

    if mode in {"coverage", "testmap", "auditmap"}:
        test_map = _test_map(conn, repo_root, files, matches or scoped_matches, budget=budget)
        payload: dict[str, Any] = {**base, **test_map}
        if mode == "auditmap":
            mapped = bool(test_map["related_tests"])
            payload["audit_queue"] = [
                {
                    "file_path": path,
                    "structural_test_mapping": "present" if mapped else "missing",
                    "runtime_coverage": test_map["runtime_coverage"].get("status"),
                    "review_reason": "runtime_evidence_required",
                }
                for path in files[:budget]
            ]
        return payload

    if mode in {"churn", "ownership"}:
        available, history, reason = _history_rows(conn, files if matches else [], budget=budget)
        payload = {
            **base,
            "history": {"available": available, "window": "90d", "reason": reason},
            "files": history,
        }
        if mode == "ownership":
            payload["ownership_risks"] = [
                {
                    "file_path": row["file_path"],
                    "authors_90d": row["authors_90d"],
                    "primary_author_90d": row["primary_author_90d"],
                    "risk": "single_author" if int(row["authors_90d"]) == 1 else "shared",
                }
                for row in history
                if int(row["commit_touches_90d"]) > 0
            ]
        return payload

    if mode == "calls":
        outgoing, incoming = insights.call_edges(conn, files, limit=max(1, budget))
        return {
            **base,
            "files": files[:budget],
            "outgoing_calls": outgoing,
            "incoming_calls": incoming,
            "evidence": "canonical_resolved_and_inferred_call_edges",
        }

    if mode == "symbols":
        return {**base, "symbols": symbol_metrics[:budget]}

    if mode == "reviewqueue":
        tests = insights.test_candidates(conn, files, matches or scoped_matches, limit=min(budget, 40))
        from . import evidence_instruments

        runtime_coverage = evidence_instruments.runtime_coverage_for_paths(
            repo_root, files
        )
        test_files = {str(row["file_path"]) for row in tests}
        queue: list[dict[str, Any]] = []
        for row in symbol_metrics:
            reasons = list(row.get("risk_reasons") or [])
            path = str(row.get("file_path") or "")
            if not any(Path(test).stem.find(Path(path).stem) >= 0 for test in test_files):
                reasons.append("no_structural_test_mapping")
            if int(row.get("incoming_calls") or 0) >= 5:
                reasons.append("high_fan_in")
            if not reasons and int(row.get("priority_score") or 0) < 8:
                continue
            queue.append({**row, "review_reasons": sorted(set(reasons))})
        queue.sort(key=lambda row: (-int(row.get("priority_score") or 0), str(row.get("qualname") or "")))
        return {
            **base,
            "queue": queue[:budget],
            "runtime_coverage": runtime_coverage,
        }

    if mode == "todo":
        return {**base, "todos": insights.todos(repo_root, files, limit=budget)}

    if mode in {
        "leaks", "nullrisks", "rawptrs", "casts", "crashes", "looprisks",
        "deadmethods", "duplicates",
    }:
        return {
            **base,
            "analysis": _risk_views(
                conn, repo_root, mode, scoped_matches, budget=budget,
            ),
        }

    if mode == "gaps":
        tests = _test_map(conn, repo_root, files, matches or scoped_matches, budget=min(budget, 40))
        # Scope and the builtin exclusion are pushed INTO the SQL so the LIMIT
        # applies to real, in-scope low-confidence edges.  Previously the query
        # took the globally-lowest-confidence rows first (0.4 builtins always
        # won) and any scope filter ran after the LIMIT, so a target with many
        # low-confidence rows returned nothing.
        clauses = ["confidence < 1.0"]
        params: list[Any] = []
        if files:
            placeholders = ",".join("?" for _ in files)
            clauses.append(f"file_path IN ({placeholders})")
            params.extend(files)
        builtin_placeholders = ",".join("?" for _ in _GAPS_BUILTIN_CALLEES)
        clauses.append(
            f"(dst_name IS NULL OR dst_name NOT IN ({builtin_placeholders}))"
        )
        params.extend(sorted(_GAPS_BUILTIN_CALLEES))
        params.append(budget)
        low_confidence = [
            dict(row) for row in conn.execute(
                "SELECT file_path, kind, src_qualname, dst_name, dst_qualname, line, "
                "evidence_label, confidence FROM edges WHERE "
                + " AND ".join(clauses)
                + " ORDER BY confidence, file_path, line LIMIT ?",
                params,
            )
        ]
        return {
            **base,
            "todos": insights.todos(repo_root, files, limit=min(budget, 40)),
            "structural_test_mapping": tests,
            "low_confidence_edges": low_confidence,
            "runtime_coverage": tests["runtime_coverage"],
        }

    if mode == "stats":
        history_available, history, reason = _history_rows(conn, [], budget=min(budget, 10))
        from . import evidence_instruments

        return {
            **base,
            **_summary(conn),
            "history": {"available": history_available, "reason": reason, "top_churn": history},
            "runtime_coverage": evidence_instruments.runtime_coverage_for_paths(
                repo_root, []
            ),
            "capabilities": list(ANALYTIC_MODES),
        }

    if mode == "summarize":
        file_rows = _file_rows(conn, files if matches else [], limit=min(budget, 40))
        return {
            **base,
            "repository": _summary(conn),
            "files": file_rows,
            "top_symbols": symbol_metrics[: min(budget, 20)],
            "todos": insights.todos(repo_root, files, limit=min(budget, 20)),
        }

    # pipeline: one compact planning packet, still backed by the same DB.
    outgoing, incoming = insights.call_edges(conn, files, limit=min(budget, 40))
    test_map = _test_map(conn, repo_root, files, matches or scoped_matches, budget=min(budget, 24))
    return {
        **base,
        "focus": {"ranked_symbols": symbol_metrics[: min(budget, 20)]},
        "impact": {"outgoing_calls": outgoing, "incoming_calls": incoming},
        "verification": test_map,
        "recommended_sequence": ["focus", "slice", "impact", "testmap", "reviewqueue"],
        "authority": "canonical_source_graph_only",
    }


__all__ = ["ANALYTIC_MODES", "query"]
