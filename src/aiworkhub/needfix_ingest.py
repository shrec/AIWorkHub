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
import sqlite3
import stat
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import needfix_store, task_plan


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
    # Deliberately UNDERIVED: this is a fingerprint dedup index, not the
    # operator-facing active list. It must see every stored non-archived
    # record regardless of its derived active state -- a record whose linked
    # card has already landed (derived CLOSED) is exactly the one intake must
    # still recognise so the same finding is not re-created as fresh noise.
    # Deriving/active-filtering here would drop those landed records and
    # resurface them, the precise defect this feature removes. Operators read
    # active state through ``list_active``/``count_active`` (derived by
    # default); this map stays raw on purpose.
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


# ---------------------------------------------------------------------------
# Production active-NeedFix surface -- derived by DEFAULT.
#
# ``list_needfix``/``count_needfix`` only derive when both task-store hooks are
# supplied; the sole other in-package caller (``_existing_by_fingerprint``) is a
# raw dedup index by design. These two entry points close that gap: they resolve
# the canonical task-store read hooks themselves, so an operator listing/counting
# the live NeedFix set gets read-time derivation from each linked card without
# anyone remembering to pass hooks. When the repository has no ready canonical
# task store to derive against, the result is marked ``derived=False`` with a
# bounded reason instead of silently presenting stale rows as authoritative.
# ---------------------------------------------------------------------------


# Upper bound on the task cards a read-time reconcile will scan for the
# explicit-reference link route. It matches the dashboard's operator-board card
# bound (``task_store.list_task_cards(..., limit=5000)``): the same cards an
# operator already loads, and ``task_store.list_task_cards`` clamps to it. The
# store consults this at most once per read and only when an unlinked, accepted
# record did not bind by its deterministic id, so a fully-linked read scans no
# cards. A card beyond the cap simply leaves its record as reported residue --
# ``get_task`` still verifies every candidate, so a cap can never force a wrong
# link, only defer a correct one to a later read.
_RECONCILE_CARD_SCAN_LIMIT: int = 5000


def _wrap_canonical_status(canonical_status_fn, cards_by_id=None, ensure_cards=None):
    def wrapped(card):
        if ensure_cards is not None:
            ensure_cards()
        raw = task_plan.card_status_evidence(card)
        if raw in needfix_store.REOPEN_CARD_STATUSES:
            if task_plan.has_landed_successor(card, cards_by_id):
                return "finished"
            return raw
        if task_plan.is_terminal_target_status(raw):
            return "finished"
        if task_plan.is_rework_or_unresolved_status(raw):
            return raw
        status = str(canonical_status_fn(card) or "").strip().lower()
        if status in needfix_store.REOPEN_CARD_STATUSES:
            if task_plan.has_landed_successor(card, cards_by_id):
                return "finished"
            return status
        if task_plan.is_terminal_target_status(status):
            return "finished"
        return status

    return wrapped


def _cache_point_lookup_card(task_id: str, found: Any) -> Mapping[str, Any] | None:
    if not isinstance(found, Mapping):
        return None
    returned_id = str(found.get("task_id") or "").strip()
    if returned_id != str(task_id or "").strip():
        return None
    return found


def _index_identity_checked_card(
    cards_by_id: dict[str, Any],
    listed_id: str,
    card: Any,
) -> None:
    requested = str(listed_id or "").strip()
    if not requested:
        return
    bound = _cache_point_lookup_card(requested, card)
    if bound is None:
        cards_by_id.setdefault(requested, None)
        return
    cards_by_id[requested] = bound


def _index_listed_cards(cards: Iterable[Any]) -> dict[str, Any]:
    cards_by_id: dict[str, Any] = {}
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        listed_id = str(card.get("task_id") or "").strip()
        _index_identity_checked_card(cards_by_id, listed_id, card)
    return cards_by_id


def _prefetch_successor_chain(
    card: Mapping[str, Any],
    cards_by_id: dict[str, Any],
    load_one,
) -> None:
    task_plan.prefetch_unknown_terminal_cards(
        [card],
        cards_by_id,
        lambda successor_id: _cache_point_lookup_card(
            successor_id, load_one(successor_id)
        ),
    )


def _prefetch_terminal_lookups(cards_by_id: dict[str, Any], get_task_fn) -> bool:
    cards = [card for card in cards_by_id.values() if isinstance(card, Mapping)]
    return not task_plan.prefetch_unknown_terminal_cards(
        cards,
        cards_by_id,
        lambda target_id: _cache_point_lookup_card(target_id, get_task_fn(target_id)),
    )


def _snapshot_terminal_artifacts(
    list_task_cards_fn,
    get_task_fn=None,
) -> tuple[list[dict[str, Any]], set[str], str | None]:
    if list_task_cards_fn is None:
        return [], set(), "list_task_cards_unavailable"
    try:
        cards = list(list_task_cards_fn() or ())
    except Exception as exc:
        return [], set(), f"list_task_cards_failed:{type(exc).__name__}"
    cards_by_id = _index_listed_cards(cards)
    if get_task_fn is not None:
        try:
            for listed_id in list(cards_by_id):
                cached = cards_by_id.get(listed_id)
                bound = _cache_point_lookup_card(listed_id, cached)
                if bound is None:
                    cards_by_id[listed_id] = None
                    continue
                verified = _cache_point_lookup_card(listed_id, get_task_fn(listed_id))
                if verified is not None:
                    cards_by_id[listed_id] = verified
            if _prefetch_terminal_lookups(cards_by_id, get_task_fn):
                return [], set(), "terminal_projection_incomplete"
        except Exception as exc:
            return [], set(), f"get_task_failed:{type(exc).__name__}"
    rows, excluded_ids = task_plan.terminal_artifact_projection(cards, cards_by_id)
    return rows, excluded_ids, None


def _exclude_terminal_artifact_status(canonical_status_fn, excluded_ids: set[str]):
    def wrapped(card):
        if isinstance(card, Mapping):
            task_id = str(card.get("task_id") or "").strip()
            if task_id in excluded_ids:
                return "finished"
        return canonical_status_fn(card)

    return wrapped


def _resolve_active_state_hooks(
    repo: Path,
    *,
    task_cards_snapshot: Sequence[Mapping[str, Any]] | None = None,
    task_cards_snapshot_complete: bool = False,
):
    """Bind the canonical task-store read hooks for read-time active derivation.

    Returns ``(get_task_fn, canonical_status_fn, list_task_cards_fn,
    underived_reason)``. All three hooks are real callables bound to ``repo``
    when its canonical task store is ready (``underived_reason`` is ``None``).
    When no ready task store can resolve a linked card, every hook is ``None``
    and a bounded ``underived_reason`` is returned so the caller can state its
    result is underived rather than treat every linked record as a live problem
    by default. ``list_task_cards_fn`` powers the explicit-reference link route
    for directly created merge/multi-finding cards; it is bounded (see
    ``_RECONCILE_CARD_SCAN_LIMIT``) because it runs on the read an operator
    waits on. A caller may supply the exact cards already read for the same
    bounded snapshot; only an explicit complete flag may prove an absent id.
    A complete identity-checked snapshot is the single authority and is never
    re-read from the store. A partial snapshot still point-looks up missing
    successor ids, including those carried only in ``archive_reason``. A store
    miss fails closed instead of keeping stale cached data.
    """
    from . import task_store  # lazy: no import-time cost, no import cycle

    try:
        readiness = task_store.storage_readiness(repo)
        ready = bool(readiness.ready)
        reason = str(readiness.reason or "")
    except Exception as exc:  # storeless/unbootstrapped repo -> underived
        return None, None, None, f"task_store_unavailable:{type(exc).__name__}"
    if not ready:
        return None, None, None, f"task_store_not_ready:{reason}"

    task_cards: Sequence[Mapping[str, Any]] | None = task_cards_snapshot
    task_cards_complete = bool(task_cards_snapshot_complete)
    task_by_id: dict[str, Any] = _index_listed_cards(task_cards or ())

    def load_task_cards() -> Sequence[Mapping[str, Any]]:
        nonlocal task_cards, task_cards_complete
        if task_cards is None:
            task_cards = task_store.list_task_cards(
                repo, limit=_RECONCILE_CARD_SCAN_LIMIT
            )
            task_cards_complete = len(task_cards) < _RECONCILE_CARD_SCAN_LIMIT
            task_by_id.clear()
            task_by_id.update(_index_listed_cards(task_cards))
        return task_cards

    def get_task_fn(task_id: str):
        load_task_cards()
        requested = str(task_id or "").strip()
        if requested in task_by_id:
            return _cache_point_lookup_card(requested, task_by_id[requested])
        if task_cards_complete:
            return None
        found = _cache_point_lookup_card(requested, task_store.get_task(repo, requested))
        if found is None:
            return None
        task_by_id[requested] = found
        _prefetch_successor_chain(
            found,
            task_by_id,
            lambda successor_id: task_store.get_task(repo, successor_id),
        )
        return found

    def list_task_cards_fn():
        # Bounded, single-snapshot card read; the store calls this lazily and at
        # most once per reconcile, only to reach the explicit-reference route.
        return load_task_cards()

    return (
        get_task_fn,
        _wrap_canonical_status(
            task_store.canonical_status,
            task_by_id,
            ensure_cards=load_task_cards,
        ),
        list_task_cards_fn,
        None,
    )


def _reconcile_links_on_read(
    repo: Path,
    get_task_fn,
    canonical_status_fn,
    list_task_cards_fn,
    *,
    include_archived: bool,
) -> None:
    """Bind unlinked NeedFix records to their card before deriving the view.

    Runs the store's verifiable, idempotent reconciliation on the same read the
    operator waits on, so a NeedFix whose card exists is linked (and, when that
    card is finished, hidden) without any manager step -- the binding is done by
    the system, not remembered by a person.

    Both verifiable link routes are reachable here, not just one. The
    deterministic ``needfix-{NF-ID}`` id needs no card scan. The explicit
    reference a directly created merge/multi-finding card carries is reached
    through ``list_task_cards_fn``, which the store consults lazily -- at most
    once per read, and only when an unlinked, accepted record did not bind by
    its deterministic id. A fully-linked read therefore scans no cards and,
    after the first reconciling read, performs no further writes. The scan is
    bounded (``_RECONCILE_CARD_SCAN_LIMIT``) because this is a read an operator
    waits on. Best-effort: a reconcile failure must never break the listing,
    which self-heals on the next read.
    """
    try:
        needfix_store.reconcile_unlinked_needfix(
            repo,
            get_task_fn=get_task_fn,
            canonical_status_fn=canonical_status_fn,
            list_task_cards_fn=list_task_cards_fn,
            include_archived=include_archived,
        )
    except Exception:
        # The active view is derived; an unreconciled record simply stays
        # visible rather than corrupting the read. Never raise into a listing.
        pass


# Upper bound on commits scanned per read for the commit-trailer resolve
# reconcile, and the key ``needfix_meta`` stores the last reconciled HEAD
# under. Mirrors ``_RECONCILE_CARD_SCAN_LIMIT``'s bounded-per-read shape: the
# scan runs on the read an operator waits on, so it is capped and skipped
# entirely once the repository is at the last-seen HEAD.
_COMMIT_TRAILER_META_KEY = "commit_trailer_last_head"
_COMMIT_TRAILER_MAX_COUNT = 200
_COMMIT_TRAILER_GIT_TIMEOUT_SECONDS = 15
_COMMIT_TRAILER_NF_ID_RE = re.compile(r"\bNF-\d{4}-\d{5}\b")


def _git_rev_parse_head(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_COMMIT_TRAILER_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


def _git_rev_list_oldest_first(
    repo: Path, rev_args: list[str]
) -> list[tuple[str, tuple[str, ...]]] | None:
    """Return ``(sha, parents)`` for the commits selected by ``rev_args``,
    oldest-first in topological order, so every in-range ancestor of a commit
    precedes it. ``None`` on any git failure except a timeout:
    ``subprocess.TimeoutExpired`` propagates so the caller can tell an
    unbounded enumeration apart from a transient failure.
    """
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo), "rev-list", "--topo-order", "--reverse",
                "--parents", *rev_args,
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_COMMIT_TRAILER_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    commits: list[tuple[str, tuple[str, ...]]] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if fields:
            commits.append((fields[0], tuple(fields[1:])))
    return commits


def _git_known_commits(repo: Path, boundaries: list[str]) -> list[str] | None:
    """Return the ``boundaries`` git knows as commits, in input order, from one
    ``git cat-file --batch-check``; an output line ending in `` missing`` marks
    that boundary unknown. ``None`` on any git failure.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch-check"],
            input="".join(f"{sha}^{{commit}}\n" for sha in boundaries),
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_COMMIT_TRAILER_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    if len(lines) != len(boundaries):
        return None
    return [
        sha
        for sha, line in zip(boundaries, lines, strict=True)
        if not line.endswith(" missing")
    ]


def _git_rev_list_newest_window(repo: Path, head: str) -> list[str] | None:
    """Return at most ``_COMMIT_TRAILER_MAX_COUNT`` commits reachable from
    ``head``, oldest-first. No ``--topo-order``/``--reverse``, so git stops
    walking after the cap instead of enumerating the whole history; the
    reversal happens here. ``None`` on any git failure.
    """
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo), "rev-list",
                f"--max-count={_COMMIT_TRAILER_MAX_COUNT}", head,
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_COMMIT_TRAILER_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    newest_first = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return list(reversed(newest_first))


def _git_resolves_trailer_log(repo: Path, shas: list[str]) -> list[tuple[str, str]] | None:
    """Return ``(sha, trailer_value)`` pairs for exactly the commits ``shas``
    (no history walk), reading ONLY the structured ``Resolves`` git trailer --
    never the free-text commit body -- via git's own trailer parser. ``None``
    on any git failure.
    """
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo), "log", "--no-walk=unsorted", "--stdin",
                "--format=%x1e%H%x1f%(trailers:key=Resolves,valueonly,unfold)",
            ],
            input="".join(f"{sha}\n" for sha in shas),
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_COMMIT_TRAILER_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    entries: list[tuple[str, str]] = []
    for record in result.stdout.split("\x1e"):
        if not record:
            continue
        sha, _, trailer_value = record.partition("\x1f")
        sha = sha.strip()
        if sha:
            entries.append((sha, trailer_value))
    return entries


def _git_rev_list_unseen(
    repo: Path, head: str, boundaries: list[str]
) -> tuple[list[tuple[str, tuple[str, ...]]] | None, bool]:
    """List ``head --not <boundaries>`` oldest-first; the flag is True only
    when the listing timed out (as opposed to any other git failure).
    """
    try:
        return _git_rev_list_oldest_first(repo, [head, "--not", *boundaries]), False
    except subprocess.TimeoutExpired:
        return None, True


def _reconcile_commit_trailers_on_read(repo: Path) -> None:
    """Bounded, idempotent read-time reconcile from HEAD-reachable commit trailers.

    A ``Resolves: NF-YYYY-NNNNN`` git trailer resolves that NeedFix exactly
    like a manager's verified direct resolution
    (:func:`needfix_store.resolve_from_commit_trailer`) -- read via git's own
    trailer parser, so a free-text body mention never qualifies. Lists the
    unseen range ``HEAD --not <boundaries>`` oldest-first (topological) and
    reads trailers for at most the oldest ``_COMMIT_TRAILER_MAX_COUNT``
    commits per read. ``needfix_meta`` stores a space-separated set of
    boundary shas (a legacy single-sha value is a one-element set) and
    collapses to HEAD alone once the whole range fit. Otherwise it stores the
    old boundaries plus the batch heads (batch commits no other batch commit
    names as a parent); every batch commit is an ancestor-or-self of a stored
    boundary, so the next read excludes exactly the batch just scanned --
    progress is exactly the cap per read, with no rescans, nothing past the
    cap lost, and no livelock across merges whose sides each exceed the cap.
    An old boundary that is a parent of a batch commit is dropped: a stored
    batch head already covers it. An unchanged repository triggers no git log
    at all. When the incremental listing fails, the boundaries are classified
    with one ``git cat-file --batch-check``: only a boundary git reports
    unknown (rewritten history, or a value synced from another clone) is
    dropped and the listing retried with the known rest; a non-timeout
    transient failure (busy repository) with every boundary known leaves meta
    unchanged so the next read retries, and nothing unscanned is skipped.
    With no boundaries -- a first read, or every boundary unknown -- history
    is NOT enumerated: ``git rev-list
    --max-count`` lists only the newest ``_COMMIT_TRAILER_MAX_COUNT`` commits
    (git stops at the cap), their trailers are read, and HEAD is stored. The
    ``Resolves:`` trailer convention starts with this feature, so the first
    read backfills only that bounded window of the newest commits; older
    history is intentionally not scanned. The same bounded window is used
    when the incremental listing itself times out
    (``_COMMIT_TRAILER_GIT_TIMEOUT_SECONDS``) with every boundary known: the
    unbounded range would time out again on every later read, so the read
    scans the newest window and stores HEAD instead. Accepted loss: unseen
    commits older than that window are never scanned for trailers; the
    manager can still resolve those NeedFix records directly. A resolve that
    fails with ``sqlite3.OperationalError`` (say "database is locked") is
    retryable: the read stops and keeps meta unchanged, and the idempotent
    resolve re-runs on the next read. Any other resolve exception is
    deterministic and skipped so it cannot stall later trailers.
    Best-effort: any other git failure -- no repository, no git binary -- is
    a silent no-op, never raised into a read.
    """
    try:
        head = _git_rev_parse_head(repo)
        if not head:
            return
        conn = needfix_store._connect(repo)
        try:
            stored = needfix_store._get_meta(conn, _COMMIT_TRAILER_META_KEY)
        finally:
            conn.close()
        boundaries = list(dict.fromkeys((stored or "").split()))
        if boundaries == [head]:
            return
        commits = None
        if boundaries:
            commits, timed_out = _git_rev_list_unseen(repo, head, boundaries)
            if commits is None:
                known = _git_known_commits(repo, boundaries)
                if known is None:
                    # git itself failing: keep meta so the next read retries.
                    return
                if known != boundaries:
                    boundaries = known
                    if boundaries:
                        commits, timed_out = _git_rev_list_unseen(
                            repo, head, boundaries
                        )
                if commits is None and boundaries:
                    if not timed_out:
                        # A non-timeout transient failure with every boundary
                        # valid: keep meta so the next read retries.
                        return
                    # The range enumeration exceeded the timeout; retrying it
                    # would stall every later read, so take the bounded
                    # newest window below instead.
                    boundaries = []
        if not boundaries:
            window = _git_rev_list_newest_window(repo, head)
            if window is None:
                return
            commits = [(sha, ()) for sha in window]
        if commits is None:
            return
        batch = commits[:_COMMIT_TRAILER_MAX_COUNT]
        if batch:
            entries = _git_resolves_trailer_log(repo, [sha for sha, _ in batch])
            if entries is None:
                return
            for sha, trailer_value in entries:
                for nfid in _COMMIT_TRAILER_NF_ID_RE.findall(trailer_value):
                    # Two failure classes. sqlite3.OperationalError (a locked
                    # or busy database) is retryable: stop without advancing
                    # the watermark; the resolve is idempotent, so the next
                    # read re-scans this batch safely. Anything else (say a
                    # corrupt evidence row) is deterministic: skip it so it
                    # cannot hold the watermark and stall every later
                    # trailer; the manager can still resolve it through
                    # needfix_link_existing_task.
                    try:
                        needfix_store.resolve_from_commit_trailer(repo, nfid, sha)
                    except sqlite3.OperationalError:
                        return
                    except Exception:
                        continue
        if len(commits) <= _COMMIT_TRAILER_MAX_COUNT:
            new_value = head
        else:
            batch_parents = {parent for _, parents in batch for parent in parents}
            kept = [sha for sha in boundaries if sha not in batch_parents]
            batch_heads = [sha for sha, _ in batch if sha not in batch_parents]
            new_value = " ".join(dict.fromkeys([*kept, *batch_heads]))
        conn = needfix_store._connect(repo)
        try:
            needfix_store._set_meta(conn, _COMMIT_TRAILER_META_KEY, new_value)
        finally:
            conn.close()
    except Exception:
        # Best-effort, same as _reconcile_links_on_read: an unreconciled
        # commit simply stays unreconciled rather than corrupting the read.
        pass


def _derive_active_surface(
    repo: Path,
    *,
    task_cards_snapshot: Sequence[Mapping[str, Any]] | None = None,
    task_cards_snapshot_complete: bool = False,
    include_archived: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    get_task_fn, canonical_status_fn, list_task_cards_fn, underived_reason = (
        _resolve_active_state_hooks(
            repo,
            task_cards_snapshot=task_cards_snapshot,
            task_cards_snapshot_complete=task_cards_snapshot_complete,
        )
    )
    if underived_reason is not None:
        return None, underived_reason
    excluded, excluded_ids, snapshot_error = _snapshot_terminal_artifacts(
        list_task_cards_fn, get_task_fn
    )
    if snapshot_error is not None:
        return None, snapshot_error
    if excluded_ids:
        canonical_status_fn = _exclude_terminal_artifact_status(
            canonical_status_fn, excluded_ids
        )
    _reconcile_links_on_read(
        repo,
        get_task_fn,
        canonical_status_fn,
        list_task_cards_fn,
        include_archived=include_archived,
    )
    _reconcile_commit_trailers_on_read(repo)
    return {
        "get_task_fn": get_task_fn,
        "canonical_status_fn": canonical_status_fn,
        "excluded": excluded,
        "excluded_ids": excluded_ids,
    }, None


def list_active(
    repo_root: str | Path,
    *,
    task_cards_snapshot: Sequence[Mapping[str, Any]] | None = None,
    task_cards_snapshot_complete: bool = False,
    include_archived: bool = False,
    limit: int = needfix_store.DEFAULT_LIST_LIMIT,
    offset: int = 0,
    order_by: str = "created_at",
    order_dir: str = "DESC",
) -> dict[str, Any]:
    """Operator-facing active NeedFix listing, derived at read time by default.

    Resolves the canonical task-store hooks itself and forwards to
    ``needfix_store.list_active_needfix`` so a NeedFix whose linked card has
    landed (or is owned by an in-flight task) is hidden here -- derivation is
    the default, not an opt-in a caller can forget. ``count`` is the full active
    total (independent of pagination) and agrees with :func:`count_active` under
    every filter, ``include_archived`` included.

    ``task_cards_snapshot`` is an optional same-read-set optimization. It never
    proves absence unless ``task_cards_snapshot_complete`` is explicitly true;
    standalone callers therefore preserve the canonical point-lookup fallback.

    When the repository has no ready canonical task store the linked-card state
    cannot be resolved; the report is marked ``derived=False`` with a bounded
    ``underived_reason`` and carries the raw (non-derived) rows rather than
    passing them off as an authoritative active set.
    """
    repo = Path(repo_root).resolve()
    derived, underived_reason = _derive_active_surface(
        repo,
        task_cards_snapshot=task_cards_snapshot,
        task_cards_snapshot_complete=task_cards_snapshot_complete,
        include_archived=include_archived,
    )
    if underived_reason is not None:
        rows = needfix_store.list_needfix(
            repo,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
            order_by=order_by,
            order_dir=order_dir,
        )
        return {
            "derived": False,
            "underived_reason": underived_reason,
            "definition": needfix_store.ACTIVE_STATE_DEFINITION,
            "count": None,
            "items": rows,
            "terminal_artifacts_excluded": [],
            "terminal_artifacts_excluded_count": 0,
        }
    assert derived is not None
    report = needfix_store.list_active_needfix(
        repo,
        get_task_fn=derived["get_task_fn"],
        canonical_status_fn=derived["canonical_status_fn"],
        include_archived=include_archived,
        limit=limit,
        offset=offset,
        order_by=order_by,
        order_dir=order_dir,
    )
    report["derived"] = True
    report["underived_reason"] = None
    report["terminal_artifacts_excluded"] = derived["excluded"]
    report["terminal_artifacts_excluded_count"] = len(derived["excluded_ids"])
    return report


def count_active(
    repo_root: str | Path,
    *,
    task_cards_snapshot: Sequence[Mapping[str, Any]] | None = None,
    task_cards_snapshot_complete: bool = False,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Operator-facing active NeedFix count, derived at read time by default.

    Uses the exact same resolved hooks, optional same-read-set snapshot, and
    ``include_archived`` filter as :func:`list_active`, so the count and the
    list describe the same set on every axis (the count/list disagreement
    this closes). Underived (marked, never silently authoritative) when the
    repository has no ready task store.
    """
    repo = Path(repo_root).resolve()
    derived, underived_reason = _derive_active_surface(
        repo,
        task_cards_snapshot=task_cards_snapshot,
        task_cards_snapshot_complete=task_cards_snapshot_complete,
        include_archived=include_archived,
    )
    if underived_reason is not None:
        return {
            "derived": False,
            "underived_reason": underived_reason,
            "definition": needfix_store.ACTIVE_STATE_DEFINITION,
            "count": None,
            "raw_total": needfix_store.count_needfix(
                repo, include_archived=include_archived
            ),
            "terminal_artifacts_excluded": [],
            "terminal_artifacts_excluded_count": 0,
        }
    assert derived is not None
    active_count = needfix_store.count_needfix(
        repo,
        include_archived=include_archived,
        get_task_fn=derived["get_task_fn"],
        canonical_status_fn=derived["canonical_status_fn"],
        active_only=True,
    )
    return {
        "derived": True,
        "underived_reason": None,
        "definition": needfix_store.ACTIVE_STATE_DEFINITION,
        "count": active_count,
        "terminal_artifacts_excluded": derived["excluded"],
        "terminal_artifacts_excluded_count": len(derived["excluded_ids"]),
    }


__all__ = [
    "DEFAULT_SOURCE_PATHS", "NeedFixIngestError", "SCHEMA_ID", "commit", "count_active",
    "list_active", "preview",
]
