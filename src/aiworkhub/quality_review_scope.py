"""Build bounded Source-Graph-backed scopes for independent reviewers.

The durable :mod:`aiworkhub.scoped_audit` module deliberately contains only
the packet contract.  This adapter is the production bridge: it extracts the
candidate symbols intersecting changed hunks, joins bounded caller/callee and
test evidence from the canonical Source Graph, and renders one immutable
scope per risk-selected reviewer lens.

No model prose is consumed here.  Every row comes from candidate bytes,
terminal validation receipts, the task contract, or the canonical graph.
Missing graph evidence is represented as a known unknown rather than a pass.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import source_graph
from . import source_graph_ast as sgast
from .evidence_levels import EvidenceLevel
from .scoped_audit import (
    ChangedPath,
    EvidenceReference,
    KnownUnknown,
    ReviewLens,
    ScopedAuditPacket,
    TargetSymbol,
    ValidationExpectation,
    packet_fingerprint,
)


MAX_SCOPE_ROWS = 64

_LENS_RATIONALE = {
    "correctness": "Verify behavior, invariants, contracts and validation evidence.",
    "security": "Verify trust boundaries, unsafe data flow and authorization invariants.",
    "code_quality": "Verify maintainability, bounded complexity and regression risk.",
}

_SYMBOL_KIND_MAP = {
    "module": "module",
    "file": "module",
    "class": "class",
    "struct": "class",
    "enum": "class",
    "union": "class",
    "function": "function",
    "method": "method",
    "attribute": "attribute",
    "variable": "attribute",
    "constant": "attribute",
    "type_alias": "type_alias",
    "protocol": "protocol",
}


class ReviewScopeBuildError(ValueError):
    """The exact candidate scope could not be represented safely."""


def _stable_id(prefix: str, *parts: object) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:20]}"


def _strings(values: Iterable[object], *, limit: int = MAX_SCOPE_ROWS) -> tuple[str, ...]:
    rows: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in rows:
            rows.append(text[:1024])
        if len(rows) >= limit:
            break
    return tuple(rows)


def _line_span(row: Mapping[str, Any]) -> tuple[int, int]:
    segments = row.get("segments") or []
    starts: list[int] = []
    ends: list[int] = []
    if isinstance(segments, list):
        for segment in segments[:MAX_SCOPE_ROWS]:
            if not isinstance(segment, Mapping):
                continue
            start = segment.get("candidate_start_line") or segment.get(
                "baseline_start_line"
            )
            end = segment.get("candidate_end_line") or segment.get(
                "baseline_end_line"
            )
            if isinstance(start, int) and not isinstance(start, bool) and start > 0:
                starts.append(start)
            if isinstance(end, int) and not isinstance(end, bool) and end > 0:
                ends.append(end)
    if not starts or not ends:
        return 1, 1
    return min(starts), max(max(ends), min(starts))


def _change_kind(
    path: str,
    digest: str | None,
    graph_files: set[str],
) -> str:
    if digest is None:
        return "removed"
    return "modified" if path in graph_files else "added"


def _command_text(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)[:1024]
    if isinstance(value, Mapping):
        return str(value.get("command") or value.get("declared_command") or "")[:1024]
    return str(value or "")[:1024]


def _row_value(row: Any, field: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(field, default)
    try:
        return row[field]
    except (KeyError, IndexError, TypeError):
        return getattr(row, field, default)


def _graph_connection(authority_repo: Path) -> sqlite3.Connection | None:
    try:
        db_path = source_graph.resolve_db_path(authority_repo)
        if not db_path.is_file() or db_path.stat().st_size <= 0:
            return None
        return source_graph.connect(db_path, read_only=True)
    except (OSError, sqlite3.Error, source_graph.SourceGraphError):
        return None


def build_scoped_audits(
    *,
    authority_repo: Path,
    candidate_repo: Path,
    task_id: str,
    packet_seed: str,
    created_at: str,
    changed_path_hashes: Mapping[str, str | None],
    source_evidence: Mapping[str, Mapping[str, Any]],
    acceptance: Iterable[object] = (),
    forbidden_changes: Iterable[object] = (),
    required_outputs: Iterable[object] = (),
    validation: Iterable[object] = (),
    terminal_validation: Iterable[object] = (),
    lenses: Iterable[str] = ("correctness", "security", "code_quality"),
) -> dict[str, dict[str, Any]]:
    """Return deterministic scoped-audit payloads for selected lenses.

    Candidate entities/edges are extracted directly from the exact retained
    workspace bytes.  The canonical Source Graph contributes bounded incoming
    callers and related tests.  This avoids mutating either repository while
    still covering newly added symbols that are absent from the canonical
    generation.
    """

    authority_repo = authority_repo.resolve()
    candidate_repo = candidate_repo.resolve()
    if not changed_path_hashes:
        raise ReviewScopeBuildError("review_scope_changed_paths_missing")
    if len(changed_path_hashes) > MAX_SCOPE_ROWS:
        raise ReviewScopeBuildError("review_scope_changed_paths_overflow")
    if set(source_evidence) != set(changed_path_hashes):
        raise ReviewScopeBuildError("review_scope_source_evidence_mismatch")

    conn = _graph_connection(authority_repo)
    try:
        graph_files: set[str] = set()
        if conn is not None:
            changed_names = sorted(changed_path_hashes)
            placeholders = ",".join("?" for _ in changed_names)
            graph_files = {
                str(row[0])
                for row in conn.execute(
                    f"SELECT file_path FROM files WHERE file_path IN ({placeholders})",
                    changed_names,
                )
            }
        changed_paths: list[ChangedPath] = []
        targets: dict[str, TargetSymbol] = {}
        impact: dict[str, EvidenceReference] = {}
        tests: dict[str, EvidenceReference] = {}
        contracts: dict[str, EvidenceReference] = {}
        unknowns: dict[str, KnownUnknown] = {}
        required_output_paths = {str(value) for value in required_outputs}

        target_names: set[str] = set()
        for path, digest_value in sorted(changed_path_hashes.items()):
            digest = None if digest_value is None else str(digest_value)
            evidence_row = source_evidence[path]
            line_start, line_end = _line_span(evidence_row)
            changed_paths.append(
                ChangedPath(
                    path=path,
                    change_kind=_change_kind(path, digest, graph_files),
                    line_start=line_start,
                    line_end=line_end,
                )
            )

            extraction = None
            candidate = candidate_repo / path
            if digest is not None and candidate.is_file() and not candidate.is_symlink():
                extraction = sgast.extract_file(
                    candidate_repo,
                    candidate,
                    build_revision=source_graph.BUILD_REVISION,
                )
                if extraction.source_hash != digest:
                    raise ReviewScopeBuildError(
                        f"review_scope_candidate_hash_mismatch:{path}"
                    )

            entity_rows: list[Any] = []
            edge_rows: list[Any] = []
            if extraction is not None:
                entity_rows = list(extraction.entities)
                edge_rows = list(extraction.edges)
            elif conn is not None:
                entity_rows = list(
                    conn.execute(
                        "SELECT kind,name,qualname,file_path,line_start,line_end "
                        "FROM entities WHERE file_path=? ORDER BY line_start,qualname",
                        (path,),
                    )
                )

            selected = [
                entity
                for entity in entity_rows
                if int(_row_value(entity, "line_end", 0)) >= line_start
                and int(_row_value(entity, "line_start", 0)) <= line_end
            ]
            if not selected and entity_rows:
                selected = [entity_rows[0]]
            for entity in selected[:MAX_SCOPE_ROWS]:
                kind = _SYMBOL_KIND_MAP.get(str(_row_value(entity, "kind", "")))
                if kind is None:
                    continue
                entity_name = str(_row_value(entity, "name", ""))
                qualname = str(_row_value(entity, "qualname", "") or entity_name)
                if not qualname:
                    continue
                targets.setdefault(qualname, TargetSymbol(qualname, kind))
                target_names.add(entity_name or qualname.rsplit(".", 1)[-1])

            for edge in edge_rows[:MAX_SCOPE_ROWS]:
                edge_line = max(1, int(edge.line or 1))
                if edge.src_qualname not in targets and not (
                    line_start <= edge_line <= line_end
                ):
                    continue
                description = (
                    f"Candidate Source Graph {edge.kind}: {edge.src_qualname} -> "
                    f"{edge.dst_qualname or edge.dst_name}; "
                    f"evidence={edge.evidence_label}; confidence={edge.confidence:.3f}"
                )
                identity = _stable_id(
                    "impact", path, edge.kind, edge.src_qualname, edge.dst_name, edge_line
                )
                impact[identity] = EvidenceReference(
                    identity=identity,
                    evidence_kind=(
                        "call_graph" if edge.kind in {"calls", "inherits"} else "data_flow"
                    ),
                    evidence_level=EvidenceLevel.STATIC_EVIDENCE,
                    path=path,
                    line_start=edge_line,
                    line_end=edge_line,
                    description=description[:1024],
                )

            if "test" in path.casefold() or "spec" in path.casefold():
                identity = _stable_id("test", path, line_start, line_end)
                tests[identity] = EvidenceReference(
                    identity=identity,
                    evidence_kind="test_target",
                    evidence_level=EvidenceLevel.STATIC_EVIDENCE,
                    path=path,
                    line_start=line_start,
                    line_end=line_end,
                    description="Changed test target extracted from exact candidate bytes.",
                )

            if path in required_output_paths and digest is not None:
                identity = _stable_id("contract", path, digest)
                contracts[identity] = EvidenceReference(
                    identity=identity,
                    evidence_kind="contract",
                    evidence_level=EvidenceLevel.STATIC_EVIDENCE,
                    path=path,
                    line_start=line_start,
                    line_end=line_end,
                    description=f"Required output is present with candidate sha256={digest}.",
                )

        if not targets:
            first = changed_paths[0]
            targets[first.path] = TargetSymbol(first.path, "module")
            unknowns["symbols-unresolved"] = KnownUnknown(
                identity="symbols-unresolved",
                question="Which semantic symbol owns the changed range?",
                why_relevant="The candidate extractor returned no supported symbol identity.",
            )

        if conn is not None and target_names:
            placeholders = ",".join("?" for _ in sorted(target_names))
            rows = conn.execute(
                "SELECT file_path,kind,src_qualname,dst_name,dst_qualname,line,"
                "evidence_label,confidence FROM edges WHERE dst_name IN ("
                + placeholders
                + ") ORDER BY file_path,line,src_qualname LIMIT ?",
                (*sorted(target_names), MAX_SCOPE_ROWS),
            ).fetchall()
            for row in rows:
                path = str(row["file_path"])
                line = max(1, int(row["line"] or 1))
                identity = _stable_id(
                    "caller", path, row["src_qualname"], row["dst_name"], line
                )
                impact[identity] = EvidenceReference(
                    identity=identity,
                    evidence_kind="callers",
                    evidence_level=EvidenceLevel.STATIC_EVIDENCE,
                    path=path,
                    line_start=line,
                    line_end=line,
                    description=(
                        f"Canonical Source Graph caller {row['src_qualname']} -> "
                        f"{row['dst_qualname'] or row['dst_name']}; "
                        f"evidence={row['evidence_label']}; confidence={float(row['confidence']):.3f}"
                    )[:1024],
                )

            stems = {Path(path).stem.casefold() for path in changed_path_hashes}
            test_rows = conn.execute(
                "SELECT file_path,MIN(line_start) AS line_start,MAX(line_end) AS line_end "
                "FROM entities WHERE lower(file_path) LIKE '%test%' "
                "OR lower(file_path) LIKE '%spec%' GROUP BY file_path "
                "ORDER BY file_path LIMIT 256"
            ).fetchall()
            for row in test_rows:
                test_path = str(row["file_path"])
                lowered = test_path.casefold()
                if stems and not any(stem in lowered for stem in stems):
                    continue
                identity = _stable_id("testmap", test_path)
                tests[identity] = EvidenceReference(
                    identity=identity,
                    evidence_kind="test_target",
                    evidence_level=EvidenceLevel.STATIC_EVIDENCE,
                    path=test_path,
                    line_start=max(1, int(row["line_start"] or 1)),
                    line_end=max(1, int(row["line_end"] or row["line_start"] or 1)),
                    description="Canonical Source Graph related-test target.",
                )
                if len(tests) >= MAX_SCOPE_ROWS:
                    break

        terminal_rows = list(terminal_validation)[:MAX_SCOPE_ROWS]
        for index, row in enumerate(terminal_rows):
            if not isinstance(row, Mapping):
                continue
            command = _command_text(row)
            returncode = row.get("returncode")
            if not command or not isinstance(returncode, int) or isinstance(returncode, bool):
                continue
            changed = changed_paths[index % len(changed_paths)]
            identity = _stable_id("validation", index, command, returncode)
            tests[identity] = EvidenceReference(
                identity=identity,
                evidence_kind="test_target",
                evidence_level=(
                    EvidenceLevel.TESTED
                    if returncode == 0
                    else EvidenceLevel.OBSERVATION
                ),
                path=changed.path,
                line_start=changed.line_start,
                line_end=changed.line_end,
                description=f"Executed validation returned {returncode}: {command}"[:1024],
                supports_blocker=returncode != 0,
            )

        if not impact:
            unknowns["impact-unresolved"] = KnownUnknown(
                identity="impact-unresolved",
                question="Are there callers, callees or state consumers outside the changed paths?",
                why_relevant="No bounded impact edge was available from candidate or canonical graph evidence.",
            )
        if not tests:
            unknowns["tests-unresolved"] = KnownUnknown(
                identity="tests-unresolved",
                question="Which tests exercise the changed behavior?",
                why_relevant="No executed validation or Source Graph test target was available.",
            )
        if conn is None:
            unknowns["canonical-graph-unavailable"] = KnownUnknown(
                identity="canonical-graph-unavailable",
                question="What canonical callers and related tests exist outside the candidate paths?",
                why_relevant="The canonical Source Graph database was unavailable for the bounded join.",
            )

        expectations: list[ValidationExpectation] = []
        for index, declared in enumerate(list(validation)[:MAX_SCOPE_ROWS]):
            command = _command_text(declared)
            if not command:
                continue
            expectations.append(
                ValidationExpectation(
                    identity=_stable_id("expect", index, command),
                    validation_kind="unit",
                    command=command,
                    expected_outcome="return code 0",
                )
            )
        if not expectations:
            expectations.append(
                ValidationExpectation(
                    identity="expect-candidate-integrity",
                    validation_kind="manual",
                    command="verify packet-bound candidate hashes and scoped evidence",
                    expected_outcome="all changed-path hashes match and every finding is grounded",
                )
            )

        scoped: dict[str, dict[str, Any]] = {}
        for lens in sorted(dict.fromkeys(str(value) for value in lenses)):
            rationale = _LENS_RATIONALE.get(lens)
            if rationale is None:
                raise ReviewScopeBuildError(f"review_scope_lens_unsupported:{lens}")
            packet = ScopedAuditPacket(
                packet_id=_stable_id("scope", packet_seed, lens),
                task_id=task_id,
                created_at=created_at or "unknown",
                target_symbols=tuple(list(targets.values())[:MAX_SCOPE_ROWS]),
                changed_paths=tuple(changed_paths[:MAX_SCOPE_ROWS]),
                forbidden_changes=_strings(forbidden_changes),
                invariants=_strings(acceptance),
                impact_evidence=tuple(list(impact.values())[:MAX_SCOPE_ROWS]),
                test_evidence=tuple(list(tests.values())[:MAX_SCOPE_ROWS]),
                contract_evidence=tuple(list(contracts.values())[:MAX_SCOPE_ROWS]),
                prior_lessons=(),
                review_lens=ReviewLens(
                    lens_kind=lens,
                    rationale=rationale,
                    required_evidence_level=EvidenceLevel.STATIC_EVIDENCE,
                ),
                unknowns=tuple(list(unknowns.values())[:MAX_SCOPE_ROWS]),
                validation_expectations=tuple(expectations[:MAX_SCOPE_ROWS]),
            )
            scoped[lens] = {
                "schema_id": "aiworkhub.scoped_audit.v1",
                "fingerprint": packet_fingerprint(packet),
                "packet": packet.as_json(),
            }
        return scoped
    finally:
        if conn is not None:
            conn.close()


__all__ = ["ReviewScopeBuildError", "build_scoped_audits"]
