"""Bounded preview/commit intake from explicit Markdown issue sections.

Markdown is untrusted input.  Preview extracts only list items from named
finding/recommendation/gap sections (plus unchecked roadmap boxes), seals the
exact source hashes and reports current dedupe matches.  Commit recomputes the
preview and may create or refresh only ``captured`` NeedFix proposals.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import needfix_store


SCHEMA_ID = "aiworkhub.needfix_markdown_intake.v1"
DEFAULT_SOURCE_PATHS = (
    "docs/reviews/README.md",
    "docs/PRODUCT_ROADMAP.md",
    "docs/AUDIT_BUGS_AND_OPTIMIZATION_2026-08-06.md",
)
MAX_INITIAL_SOURCES = 32
MAX_TOTAL_SOURCES = 64
MAX_SOURCE_BYTES = 512 * 1024
MAX_CANDIDATES = 200
MAX_ITEM_BYTES = 8 * 1024
MAX_OBSERVATIONS = 8

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+?)\s*$")
_CHECKBOX_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.+?)\s*$")
_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)#?]+\.md)(?:#[^)]+)?\)", re.IGNORECASE)
_MARKUP_RE = re.compile(r"[`*~]+")
_SPACE_RE = re.compile(r"\s+")

_SECTION_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("required a/b benchmark", "required benchmark"), "benchmark_gap", "medium"),
    (("benchmark gap", "benchmark gaps"), "benchmark_gap", "medium"),
    (("security finding", "security findings", "security risks"), "security_risk", "high"),
    (("documentation drift", "documentation gaps"), "documentation_drift", "medium"),
    (("roadmap gap", "roadmap gaps", "open roadmap"), "roadmap_candidate", "medium"),
    (("finding", "findings", "remaining issues", "open issues", "known issues"), "investigation", "medium"),
    (("recommendation", "recommendations", "recommended work", "proposed changes", "next steps"), "improvement", "medium"),
    (("optimization opportunities", "optimization opportunity"), "optimization", "medium"),
    (("gap", "gaps", "open items", "remaining work"), "roadmap_candidate", "medium"),
)
_IGNORE_SECTION = ("__ignore__", "info")
_IGNORE_SECTION_TERMS = ("positive finding", "positive findings", "strengths", "non-goals")


class NeedFixIngestError(RuntimeError):
    """Unsafe source, malformed receipt or invalid commit transition."""


def _clean(value: str) -> str:
    return _SPACE_RE.sub(" ", _MARKUP_RE.sub("", value)).strip()


def _source_path(repo: Path, relative: str) -> tuple[Path, str]:
    if not isinstance(relative, str) or not relative.strip() or "\x00" in relative:
        raise NeedFixIngestError("invalid_source_path")
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or rel.suffix.lower() != ".md":
        raise NeedFixIngestError(f"unsafe_source_path:{relative}")
    root = repo.resolve()
    path = root / rel
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise NeedFixIngestError(f"source_unavailable:{relative}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise NeedFixIngestError(f"source_not_regular:{relative}")
    if resolved != root and root not in resolved.parents:
        raise NeedFixIngestError(f"source_outside_repository:{relative}")
    if info.st_size > MAX_SOURCE_BYTES:
        raise NeedFixIngestError(f"source_too_large:{relative}")
    return resolved, resolved.relative_to(root).as_posix()


def _read(repo: Path, relative: str) -> tuple[str, str, str]:
    path, normalized = _source_path(repo, relative)
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise NeedFixIngestError(f"source_unreadable:{normalized}") from exc
    return normalized, text, hashlib.sha256(raw).hexdigest()


def _linked_sources(repo: Path, source_file: str, text: str) -> list[str]:
    parent = Path(source_file).parent
    linked: list[str] = []
    for match in _LINK_RE.finditer(text):
        raw = match.group(1).strip()
        candidate = (parent / raw).as_posix()
        try:
            _path, normalized = _source_path(repo, candidate)
        except NeedFixIngestError:
            continue
        linked.append(normalized)
    return linked


def _section_contract(heading: str) -> tuple[str, str] | None:
    normalized = _clean(heading).lower().rstrip(":")
    if normalized in _IGNORE_SECTION_TERMS or any(
        normalized.endswith(f" {term}") for term in _IGNORE_SECTION_TERMS
    ):
        return _IGNORE_SECTION
    for names, kind, severity in _SECTION_RULES:
        if normalized in names or any(normalized.endswith(f" {name}") for name in names):
            return kind, severity
    return None


def _item_title(item: str) -> str:
    clean = _clean(item)
    sentence = re.split(r"(?<=[.!?])\s+", clean, maxsplit=1)[0]
    title = sentence.rstrip(".:")
    if len(title) > 180:
        title = title[:177].rstrip() + "..."
    return title or "Markdown intake candidate"


def _candidate(
    *, source_file: str, source_sha256: str, section: str, line: int,
    raw_item: str, kind: str, severity: str,
) -> dict[str, Any]:
    description = _clean(raw_item)
    if not description or len(description.encode("utf-8")) > MAX_ITEM_BYTES:
        raise NeedFixIngestError("candidate_item_invalid_or_too_large")
    identity = f"{source_file}\0{_clean(section).lower()}\0{description}"
    fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        "source_fingerprint": fingerprint,
        "source_file": source_file,
        "source_section": section,
        "source_line": line,
        "source_sha256": source_sha256,
        "kind": kind,
        "severity": severity,
        "title": _item_title(description),
        "description": description,
        "scope": f"Markdown intake: {source_file}#{_clean(section)}",
        "evidence_ref": f"file:{source_file}",
    }


def _extract(source_file: str, text: str, source_sha256: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    candidates: list[dict[str, Any]] = []
    heading = ""
    contract: tuple[str, str] | None = None
    heading_stack: list[tuple[int, str, tuple[str, str] | None]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            heading = _clean(heading_match.group(2))
            level = len(heading_match.group(1))
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, heading, _section_contract(heading)))
            contract = None
            if not any(row[2] == _IGNORE_SECTION for row in heading_stack):
                for _parent_level, _parent_heading, inherited in reversed(heading_stack):
                    if inherited is not None:
                        contract = inherited
                        break
            index += 1
            continue
        checkbox = _CHECKBOX_RE.match(line)
        roadmap_unchecked = (
            checkbox is not None
            and checkbox.group(1) == " "
            and source_file.endswith("PRODUCT_ROADMAP.md")
        )
        bullet = checkbox if checkbox is not None else _BULLET_RE.match(line)
        if bullet is None or (contract is None and not roadmap_unchecked):
            index += 1
            continue
        item = bullet.group(2) if checkbox is not None else bullet.group(1)
        start_line = index + 1
        continuation: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            next_line = lines[cursor]
            if _HEADING_RE.match(next_line) or _BULLET_RE.match(next_line):
                break
            if next_line.strip():
                if not next_line.startswith((" ", "\t")):
                    break
                continuation.append(next_line.strip())
            cursor += 1
        if continuation:
            item = " ".join([item, *continuation])
        item_contract = ("roadmap_candidate", "medium") if roadmap_unchecked else contract
        assert item_contract is not None
        candidates.append(_candidate(
            source_file=source_file,
            source_sha256=source_sha256,
            section=(" > ".join(row[1] for row in heading_stack) or "Roadmap unchecked item"),
            line=start_line,
            raw_item=item,
            kind=item_contract[0],
            severity=item_contract[1],
        ))
        if len(candidates) >= MAX_CANDIDATES:
            break
        index = cursor
    return candidates


def _existing_by_fingerprint(repo: Path) -> dict[str, dict[str, Any]]:
    rows = needfix_store.list_needfix(
        repo, include_archived=False, limit=500, order_by="created_at", order_dir="ASC"
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        evidence = row.get("evidence")
        if not isinstance(evidence, dict):
            continue
        fingerprint = str(evidence.get("source_fingerprint") or "")
        if fingerprint:
            result[fingerprint] = row
    return result


def preview(
    repo_root: str | Path,
    *,
    source_paths: Sequence[str] | None = None,
    follow_links: bool = True,
) -> dict[str, Any]:
    repo = Path(repo_root).resolve()
    initial = list(source_paths or DEFAULT_SOURCE_PATHS)
    if not initial or len(initial) > MAX_INITIAL_SOURCES:
        raise NeedFixIngestError("invalid_source_count")
    queue = list(initial)
    seen: set[str] = set()
    sources: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    while queue and len(seen) < MAX_TOTAL_SOURCES:
        requested = queue.pop(0)
        normalized, text, source_sha = _read(repo, requested)
        if normalized in seen:
            continue
        seen.add(normalized)
        sources.append({"path": normalized, "sha256": source_sha})
        candidates.extend(_extract(normalized, text, source_sha))
        if len(candidates) > MAX_CANDIDATES:
            raise NeedFixIngestError("candidate_limit_exceeded")
        if follow_links:
            queue.extend(path for path in _linked_sources(repo, normalized, text) if path not in seen)
    if queue:
        raise NeedFixIngestError("linked_source_limit_exceeded")

    existing = _existing_by_fingerprint(repo)
    for row in candidates:
        match = existing.get(row["source_fingerprint"])
        row["dedupe_match"] = (
            {"needfix_id": match["id"], "status": match["status"]}
            if match is not None else None
        )
    identity = {
        "schema_id": SCHEMA_ID,
        "sources": sources,
        "candidate_fingerprints": [row["source_fingerprint"] for row in candidates],
    }
    preview_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "preview_id": preview_id,
        "source_count": len(sources),
        "candidate_count": len(candidates),
        "new_count": sum(row["dedupe_match"] is None for row in candidates),
        "matched_count": sum(row["dedupe_match"] is not None for row in candidates),
        "sources": sources,
        "candidates": candidates,
        "authority": "preview_only_no_write",
    }


def _merged_observations(existing: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    evidence = dict(existing.get("evidence") or {})
    observations = list(evidence.get("observations") or [])
    observation = {
        "source_file": candidate["source_file"],
        "source_section": candidate["source_section"],
        "source_line": candidate["source_line"],
        "source_sha256": candidate["source_sha256"],
    }
    observations = [row for row in observations if row != observation]
    observations.append(observation)
    evidence.update({
        "schema_id": SCHEMA_ID,
        "source_fingerprint": candidate["source_fingerprint"],
        "observations": observations[-MAX_OBSERVATIONS:],
    })
    return evidence


def commit(
    repo_root: str | Path,
    *,
    source_paths: Sequence[str] | None,
    preview_id: str,
    follow_links: bool = True,
) -> dict[str, Any]:
    repo = Path(repo_root).resolve()
    preview_receipt = preview(
        repo, source_paths=source_paths, follow_links=follow_links
    )
    if not isinstance(preview_id, str) or not preview_id or preview_id != preview_receipt["preview_id"]:
        raise NeedFixIngestError("preview_identity_mismatch")
    existing = _existing_by_fingerprint(repo)
    rows: list[dict[str, Any]] = []
    for candidate in preview_receipt["candidates"]:
        fingerprint = candidate["source_fingerprint"]
        match = existing.get(fingerprint)
        if match is not None:
            if match.get("status") != "captured":
                rows.append({
                    "source_fingerprint": fingerprint,
                    "needfix_id": match["id"],
                    "action": "skipped_non_captured",
                    "status": match["status"],
                })
                continue
            evidence = _merged_observations(match, candidate)
            refs = list(dict.fromkeys([*(match.get("evidence_refs") or []), candidate["evidence_ref"]]))
            updated = needfix_store.update_needfix(
                repo, match["id"], evidence=evidence, evidence_refs=refs,
            )
            rows.append({
                "source_fingerprint": fingerprint,
                "needfix_id": updated["id"],
                "action": "updated_captured",
                "status": updated["status"],
            })
            continue
        evidence = _merged_observations({}, candidate)
        created = needfix_store.capture_proposal(
            repo,
            title=candidate["title"],
            description=candidate["description"],
            scope=candidate["scope"],
            provenance={
                "origin": "markdown_intake",
                "schema_id": SCHEMA_ID,
                "preview_id": preview_id,
            },
            evidence=evidence,
            kind=candidate["kind"],
            severity=candidate["severity"],
            tags=["markdown_intake", "untrusted_prose"],
            scope_files=[candidate["source_file"]],
            evidence_refs=[candidate["evidence_ref"]],
            readiness_score=0,
        )
        existing[fingerprint] = created
        rows.append({
            "source_fingerprint": fingerprint,
            "needfix_id": created["id"],
            "action": "created_captured",
            "status": created["status"],
        })
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "preview_id": preview_id,
        "candidate_count": len(rows),
        "counts": counts,
        "results": rows,
        "promotion_boundary": "captured_only",
    }


__all__ = [
    "DEFAULT_SOURCE_PATHS", "NeedFixIngestError", "SCHEMA_ID", "commit", "preview",
]
