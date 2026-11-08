"""Census of reviewer findings and reject_review reasons by defect category.

RM-2026-00086 phase 0. ``reject:candidate_code`` is the largest retry bucket in
the cost ledger, so before any rule is built this script measures which defect
classes recur and which of them a deterministic rule could catch.

Read-only by construction: the task store is opened through
``aiworkhub.sqlite_readonly.connect_readonly`` (``mode=ro`` plus
``PRAGMA query_only``), so a write through the opened connection fails. The
only thing ever written is the report file named by ``--out``.

Sources, all in the canonical task database:

* ``review_orchestrator_finding_dispositions`` -- the manager's recorded
  disposition (``confirmed`` / ``dismissed`` / ...) per reviewer finding.
* reviewer findings -- every ``findings`` list of mappings found in a stored
  ``task_events`` payload (the sealed reviewer report).
* ``task_events`` rows with ``event = 'reject_review'`` -- the reject reason.
  A rejection is the manager's own confirmed defect, so each reason counts as
  confirmed.

Every item is classified by the first matching row of ``CATEGORIES`` (an
ordered keyword/regex table); text matching no row lands in ``other``. The
report carries ids only, never finding or reason bodies (the repository is
public and the report is not committed).

Exit status: 0 report produced, 2 the store could not be opened or read.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_ID = "aiworkhub.review_finding_census.v1"
DEFAULT_ROW_LIMIT = 200_000
MAX_TEXT_CHARS = 4_000
MAX_EXAMPLES = 5
MAX_WALK_DEPTH = 6
OTHER = "other"
DISPOSITION_CONFIRMED = "confirmed"
# fixed_by_correction: the manager fixed the finding in the candidate itself
# (review_orchestrator SYSTEM_FINDING_DISPOSITIONS), so it was a real defect.
CONFIRMED_DISPOSITIONS = frozenset({DISPOSITION_CONFIRMED, "fixed_by_correction"})
REJECT_EVENT = "reject_review"
DISPOSITION_TABLE = "review_orchestrator_finding_dispositions"
EVENTS_TABLE = "task_events"
_FINDING_TEXT_KEYS = (
    "category",
    "title",
    "summary",
    "detail",
    "description",
    "failure_scenario",
    "message",
    "text",
    "body",
)
_REASON_KEYS = ("reason", "message", "review_reason", "rejection_reason", "note")


@dataclass(frozen=True)
class Category:
    name: str
    pattern: re.Pattern[str]
    mechanically_detectable: bool
    rule_idea: str


def _c(name: str, pattern: str, detectable: bool, idea: str) -> Category:
    return Category(name, re.compile(pattern, re.IGNORECASE), detectable, idea)


# Ordered: the first match wins, so specific classes precede broad ones.
CATEGORIES: tuple[Category, ...] = (
    _c(
        "unwired_production_code",
        r"\bunwired\b|not wired|never (?:called|invoked|used|imported)|\bdead code\b"
        r"|no (?:production )?callers?|not (?:referenced|registered|imported|reachable)"
        r"|unreachable from|not hooked up",
        True,
        "New top-level def/class with no reference outside its own file and tests "
        "(Source Graph callers/deps) fails the change.",
    ),
    _c(
        "swallowed_exception",
        r"swallow|bare except|except (?:Exception|BaseException)?\s*:\s*pass"
        r"|silently (?:ignor|drop|discard|catch|pass)"
        r"|suppress(?:es|ed)? (?:the )?(?:error|exception)",
        True,
        "AST: an except handler whose body is only pass/continue/return, with no "
        "raise, log or recorded cause.",
    ),
    _c(
        "path_or_option_injection",
        r"path traversal|\.\./|directory traversal"
        r"|(?:option|argument|command|shell|sql|argv) injection|\binjection\b"
        r"|leading dash|shell=True|escapes? the (?:worktree|root|sandbox)"
        r"|symlink|junction|unsanitiz|\bargv\b",
        True,
        "Subprocess argv built from a variable without a '--' separator, or a "
        "joined external path without resolve() plus is_relative_to().",
    ),
    _c(
        "concurrency_race",
        r"\brace\b|race condition|toctou|concurren|deadlock|\block(?:s|ed|ing)?\b"
        r"|\bthread|not atomic|non-atomic|parallel",
        False,
        "Needs reasoning about interleavings; at best flag read-then-write of "
        "shared state outside a lock for a human.",
    ),
    _c(
        "resource_leak",
        r"\bleak(?:s|ed|ing)?\b|never closed|not closed|unclosed|file handles?"
        r"|file descriptors?|\bzombie|orphan(?:ed)? process|not cleaned up"
        r"|temp(?:orary)? files? (?:left|remain)",
        True,
        "AST: open()/connect()/Popen() outside a with-block and without close() "
        "in a finally.",
    ),
    _c(
        "platform_portability",
        r"\bwindows\b|\bposix\b|\bchmod\b|backslash|cross-platform|portab"
        r"|os\.name|\bcp1252\b|\bcrlf\b|case-insensitive|line endings?|\bmacos\b"
        r"|win32",
        True,
        "Grep for os.chmod, hard-coded '/' joins, shell=True and text-mode open() "
        "without an explicit encoding.",
    ),
    _c(
        "missing_test",
        r"\b(?:no|missing|lacks?|lacking|without|add(?:ed)?|needs?)\b[^.\n]{0,40}?"
        r"\b(?:tests?|coverage|assertions?)\b"
        r"|\btests?\b[^.\n]{0,30}\b(?:missing|absent|does not|doesn't|never|not cover)"
        r"|\buntested\b|\bnot (?:covered|tested)\b",
        True,
        "A change touching src/ without any touched or new tests/ file mapped to "
        "it fails before review.",
    ),
    _c(
        "missing_input_validation",
        r"unvalidated|not validated|no validation|missing validation|without validat"
        r"|unbounded|no (?:bound|limit|cap)\b|unchecked|untrusted|\bint\("
        r"|sanitiz|type check|non-?string",
        False,
        "Semantic; the narrow mechanical slice is int()/json.loads over external "
        "text with no length bound.",
    ),
    _c(
        "wrong_error_handling",
        r"wrong error|error code|fail-?open|fails? open|misclassif|raises? instead"
        r"|returns? (?:ok|success|true) (?:on|when|even)|exit code|exception type"
        r"|error handling|too broad|mis-?reported",
        False,
        "Depends on the intended failure contract; a rule would need a per-module "
        "error taxonomy.",
    ),
    _c(
        "scope_or_contract_violation",
        r"allowed_writes|out of scope|outside (?:the )?(?:card|scope|contract)"
        r"|scope creep|contract violation|violates? the (?:card|contract|acceptance)"
        r"|acceptance criteri|overbuil|not requested|forbidden",
        True,
        "Diff paths against allowed_writes and required_outputs (the coordinator "
        "already enforces the first; extend to the second).",
    ),
    _c(
        "docs_drift",
        r"docstrings?|documentation|\bdocs\b|changelog|readme|stale comment"
        r"|comment (?:is |are )?(?:stale|wrong|outdated|misleading)|\bdrift\b"
        r"|out of date",
        True,
        "A changed public signature or behaviour without a touched docstring or "
        "CHANGELOG entry fails.",
    ),
    Category(
        OTHER,
        re.compile(r"(?!)"),
        False,
        "Unclassified text; sample the ids and promote a recurring phrase to a "
        "category.",
    ),
)


def classify(text: str) -> str:
    """The category name of ``text``; ``other`` when no table row matches."""
    bounded = str(text or "")[:MAX_TEXT_CHARS]
    for category in CATEGORIES:
        if category.pattern.search(bounded):
            return category.name
    return OTHER


def _connect_readonly_fallback(path: Path) -> sqlite3.Connection:
    """Read-only open for a checkout where ``aiworkhub`` is not importable.

    Reproduces the two guarantees of ``sqlite_readonly.connect_readonly``: the
    URI comes from ``Path.as_uri`` (so a ``#`` in the path cannot swallow the
    ``mode=ro`` query) and ``PRAGMA query_only = ON`` refuses any write.
    """
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    return conn


def default_db_path(repo: Path) -> Path:
    """The canonical task database of ``repo``."""
    try:
        from aiworkhub import task_store

        return Path(task_store.canonical_db_path(repo))
    except Exception:  # noqa: BLE001 -- fall back to the documented layout
        return repo / ".aiworkhub" / "tasking" / "task_queue.sqlite"


def open_store(db_path: Path) -> sqlite3.Connection:
    """Open the task store read-only, through the existing helper when present."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"task_store_absent:{path.name}")
    try:
        from aiworkhub import sqlite_readonly
    except ImportError:
        conn = _connect_readonly_fallback(path)
    else:
        conn = sqlite_readonly.connect_readonly(path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _loads(raw: object) -> Any:
    try:
        return json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return None


def _join_text(mapping: Mapping[str, Any], keys: Iterable[str]) -> str:
    parts = [
        str(mapping[key]) for key in keys if isinstance(mapping.get(key), (str, int, float))
    ]
    return "\n".join(parts)


def _walk_findings(node: Any, depth: int = 0) -> Iterable[Mapping[str, Any]]:
    """Yield mapping items of every ``findings`` list nested in ``node``."""
    if depth > MAX_WALK_DEPTH:
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key == "findings" and isinstance(value, list):
                for item in value:
                    if isinstance(item, Mapping):
                        yield item
            else:
                yield from _walk_findings(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_findings(value, depth + 1)


def _last_segment(finding_id: str) -> str:
    return finding_id.rsplit(":", 1)[-1]


def _is_confirmed(disposition: str) -> bool:
    return disposition in CONFIRMED_DISPOSITIONS


def _lookup_disposition(dispositions: Mapping[str, str], finding_id: str) -> str:
    """Exact-id lookup. A bare id resolves a prefixed disposition only because
    ``_read_dispositions`` indexes the bare id; a prefixed id never falls back
    to its bare id, so one lens's disposition is not handed to another lens."""
    return dispositions.get(finding_id, "")


@dataclass(frozen=True)
class Item:
    item_id: str
    kind: str  # "finding" | "reject_reason"
    text: str
    confirmed: bool


def _read_dispositions(
    conn: sqlite3.Connection, limit: int
) -> tuple[dict[str, str], list[tuple[str, str]], str]:
    """(disposition by finding id and by bare id, [(id, text)] with text, status)."""
    if not _table_exists(conn, DISPOSITION_TABLE):
        return {}, [], "absent"
    by_id: dict[str, str] = {}
    texted: list[tuple[str, str]] = []
    rows = conn.execute(
        f"SELECT * FROM {DISPOSITION_TABLE} LIMIT ?", (int(limit),)  # noqa: S608
    ).fetchall()
    for row in rows:
        keys = set(row.keys())
        finding_id = str(row["finding_id"]) if "finding_id" in keys else ""
        disposition = str(row["disposition"]) if "disposition" in keys else ""
        if not finding_id:
            continue
        by_id[finding_id] = disposition
        by_id.setdefault(_last_segment(finding_id), disposition)
        receipt = _loads(row["receipt_json"]) if "receipt_json" in keys else None
        if isinstance(receipt, Mapping):
            inner = receipt.get("finding")
            source = inner if isinstance(inner, Mapping) else receipt
            text = _join_text(source, _FINDING_TEXT_KEYS)
            if text:
                texted.append((finding_id, text))
    return by_id, texted, "ok"


def _read_events(
    conn: sqlite3.Connection, limit: int
) -> tuple[list[tuple[str, Mapping[str, Any]]], list[Item], str]:
    """Reviewer findings (id, mapping) and reject_review reason items."""
    if not _table_exists(conn, EVENTS_TABLE):
        return [], [], "absent"
    findings: list[tuple[str, Mapping[str, Any]]] = []
    rejects: list[Item] = []
    rows = conn.execute(
        "SELECT rowid AS rid, task_id, event, payload_json FROM task_events "
        "WHERE event = ? OR payload_json LIKE '%\"findings\"%' ORDER BY rowid LIMIT ?",
        (REJECT_EVENT, int(limit)),
    ).fetchall()
    for row in rows:
        payload = _loads(row["payload_json"])
        if not isinstance(payload, Mapping):
            continue
        task_id = str(row["task_id"] or "")
        rid = row["rid"]
        if str(row["event"]) == REJECT_EVENT:
            text = _join_text(payload, _REASON_KEYS)
            if text:
                rejects.append(
                    Item(f"{REJECT_EVENT}:{task_id}:{rid}", "reject_reason", text, True)
                )
        for index, finding in enumerate(_walk_findings(payload)):
            raw_id = finding.get("id") or finding.get("finding_id")
            finding_id = str(raw_id) if raw_id else f"{task_id}#{rid}#{index}"
            findings.append((finding_id, finding))
    return findings, rejects, "ok"


def collect_items(conn: sqlite3.Connection, limit: int = DEFAULT_ROW_LIMIT) -> tuple[list[Item], dict[str, Any]]:
    """Every classifiable finding / reject reason, plus per-source status."""
    sources: dict[str, str] = {}
    try:
        dispositions, texted, sources[DISPOSITION_TABLE] = _read_dispositions(conn, limit)
    except sqlite3.Error as exc:
        dispositions, texted, sources[DISPOSITION_TABLE] = {}, [], f"error:{type(exc).__name__}"
    try:
        findings, rejects, sources[EVENTS_TABLE] = _read_events(conn, limit)
    except sqlite3.Error as exc:
        findings, rejects, sources[EVENTS_TABLE] = [], [], f"error:{type(exc).__name__}"

    items: list[Item] = []
    # Global dedupe: the same finding is stored on the reviewer card and on the
    # target card, so the key is (bare id, text) and never includes the task.
    seen: set[tuple[str, str]] = set()
    seen_bare: set[str] = set()
    without_text = 0
    for finding_id, finding in findings:
        key = _last_segment(finding_id)
        text = _join_text(finding, _FINDING_TEXT_KEYS)
        if not text:
            without_text += 1
            continue
        if (key, text) in seen:
            continue
        seen.add((key, text))
        seen_bare.add(key)
        items.append(
            Item(finding_id, "finding", text, _is_confirmed(_lookup_disposition(dispositions, finding_id)))
        )
    for finding_id, text in texted:
        key = _last_segment(finding_id)
        if key in seen_bare or (key, text) in seen:
            continue
        seen.add((key, text))
        items.append(
            Item(finding_id, "finding", text, _is_confirmed(dispositions.get(finding_id, "")))
        )
    items.extend(rejects)
    return items, {"sources": sources, "findings_without_text": without_text}


def build_report(conn: sqlite3.Connection, limit: int = DEFAULT_ROW_LIMIT) -> dict[str, Any]:
    items, meta = collect_items(conn, limit)
    buckets: dict[str, list[Item]] = {category.name: [] for category in CATEGORIES}
    for item in items:
        buckets[classify(item.text)].append(item)
    total = len(items)
    rows = []
    for category in CATEGORIES:
        bucket = buckets[category.name]
        rows.append(
            {
                "category": category.name,
                "count": len(bucket),
                "confirmed_count": sum(1 for item in bucket if item.confirmed),
                "share": round(len(bucket) / total, 4) if total else 0.0,
                "mechanically_detectable": category.mechanically_detectable,
                "rule_idea": category.rule_idea,
                "examples": [item.item_id for item in bucket[:MAX_EXAMPLES]],
            }
        )
    rows.sort(key=lambda row: (-row["count"], row["category"]))
    detectable = sum(row["count"] for row in rows if row["mechanically_detectable"])
    return {
        "schema_id": SCHEMA_ID,
        "total_items": total,
        "finding_items": sum(1 for item in items if item.kind == "finding"),
        "reject_reason_items": sum(1 for item in items if item.kind == "reject_reason"),
        "confirmed_basis": (
            "finding disposition in {confirmed, fixed_by_correction}; "
            "every reject_review reason counts as confirmed"
        ),
        "mechanically_detectable_share": round(detectable / total, 4) if total else 0.0,
        "sources": meta["sources"],
        "findings_without_text": meta["findings_without_text"],
        "categories": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".", help="repository root (default: cwd)")
    parser.add_argument("--db", default="", help="task database path (default: canonical)")
    parser.add_argument("--limit", type=int, default=DEFAULT_ROW_LIMIT, help="max rows per source")
    parser.add_argument("--out", default="", help="also write the JSON report to PATH")
    args = parser.parse_args(argv)
    if args.limit < 1:
        print(json.dumps({"schema_id": SCHEMA_ID, "ok": False, "error": "limit_must_be_positive"}))
        return 2
    repo = Path(args.repo).resolve()
    db_path = Path(args.db) if args.db else default_db_path(repo)
    try:
        conn = open_store(db_path)
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps({"schema_id": SCHEMA_ID, "ok": False, "error": f"store_unreadable:{type(exc).__name__}"}))
        return 2
    try:
        report = build_report(conn, args.limit)
    finally:
        conn.close()
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
