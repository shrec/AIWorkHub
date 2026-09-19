#!/usr/bin/env python3
"""Generate repository architecture block diagrams as SVG.

The version and selected Source Graph constants and counts are read from the
source package and, where present, this repository's own index. Other labels
are maintained architecture snapshots; review them when the code moves:

    python3 scripts/generate_architecture_diagrams.py            # write SVGs
    python3 scripts/generate_architecture_diagrams.py --check    # fail if stale
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import source_graph as sg  # noqa: E402
from aiworkhub import source_graph_ast as sga  # noqa: E402
from aiworkhub._version import __version__ as VERSION  # noqa: E402
from aiworkhub.sqlite_readonly import connect_readonly  # noqa: E402

GENERATOR = "scripts/generate_architecture_diagrams.py"
SYSTEM_SVG = Path("site/assets/aiworkhub-system-architecture.svg")
SOURCE_GRAPH_SVG = Path("site/assets/aiworkhub-source-graph-architecture.svg")

SANS = "Ubuntu,Noto Sans,DejaVu Sans,Segoe UI,sans-serif"
MONO = "JetBrains Mono,DejaVu Sans Mono,Consolas,monospace"

INK = "#e2e8f0"
MUTED = "#94a3b8"
FAINT = "#64748b"
PANEL_FILL = "#0d1726"
PANEL_STROKE = "#1e3149"
ROW_FILL = "#101d30"

CYAN = "#22d3ee"
GREEN = "#a3e635"
MAGENTA = "#e879f9"
BLUE = "#60a5fa"
AMBER = "#fbbf24"
ROSE = "#fb7185"

W = 2000
MARGIN = 44
GAP = 18
BAND_TITLE_H = 30
PANEL_HEAD_H = 57
ROW_H = 34
PANEL_PAD = 12
BAND_GAP = 30

# Character-width factors measured against the DejaVu metrics the diagrams fall
# back to; only used to refuse a layout that would overflow its panel.
SANS_EM = 0.55
MONO_EM = 0.61


# Collected by _assert_panel_fits so one run reports every overflow, not the first.
_OVERFLOWS: list[str] = []


@dataclass(frozen=True)
class Row:
    label: str
    detail: str = ""


@dataclass(frozen=True)
class Panel:
    title: str
    accent: str
    subtitle: str = ""
    rows: tuple[Row, ...] = ()
    index: str = ""


@dataclass
class Canvas:
    parts: list[str] = field(default_factory=list)
    y: int = 0

    def add(self, markup: str) -> None:
        self.parts.append(markup)


def _text(x: float, y: float, value: str, *, size: float, fill: str, font: str = SANS,
          weight: str = "400", anchor: str = "start", spacing: str = "0") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-family="{font}" '
        f'font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" '
        f'letter-spacing="{spacing}">{escape(value)}</text>'
    )


def _fits(value: str, size: float, width: float, *, mono: bool) -> bool:
    em = MONO_EM if mono else SANS_EM
    return len(value) * size * em <= width


def _panel_height(panel: Panel) -> int:
    return PANEL_HEAD_H + len(panel.rows) * ROW_H + PANEL_PAD


def _render_panel(panel: Panel, x: float, y: float, width: float) -> tuple[str, int]:
    height = _panel_height(panel)
    inner = width - 2 * PANEL_PAD
    out = [
        f'<rect x="{x:.1f}" y="{y}" width="{width:.1f}" height="{height}" rx="12" '
        f'fill="{PANEL_FILL}" stroke="{panel.accent}" stroke-opacity="0.55" stroke-width="1.4"/>',
        f'<rect x="{x:.1f}" y="{y}" width="{width:.1f}" height="3" rx="1.5" fill="{panel.accent}"/>',
    ]
    tx = x + PANEL_PAD
    if panel.index:
        out.append(
            f'<rect x="{tx:.1f}" y="{y + 16}" width="22" height="22" rx="6" '
            f'fill="{panel.accent}" fill-opacity="0.16" stroke="{panel.accent}" stroke-opacity="0.7"/>'
        )
        out.append(_text(tx + 11, y + 32, panel.index, size=13, fill=panel.accent,
                         weight="700", anchor="middle"))
        tx += 32
    out.append(_text(tx, y + 32, panel.title, size=15.5, fill=INK, weight="700", spacing="0.4"))
    if panel.subtitle:
        out.append(_text(x + PANEL_PAD, y + 46, panel.subtitle, size=10.5, fill=MUTED, font=MONO))

    ry = y + PANEL_HEAD_H
    for row in panel.rows:
        out.append(
            f'<rect x="{x + PANEL_PAD:.1f}" y="{ry}" width="{inner:.1f}" height="{ROW_H - 6}" '
            f'rx="6" fill="{ROW_FILL}"/>'
        )
        out.append(
            f'<circle cx="{x + PANEL_PAD + 9:.1f}" cy="{ry + 11}" r="2.6" fill="{panel.accent}"/>'
        )
        out.append(_text(x + PANEL_PAD + 18, ry + 15, row.label, size=12.5, fill=INK, weight="600"))
        if row.detail:
            out.append(_text(x + PANEL_PAD + 18, ry + 26, row.detail, size=9.6, fill=MUTED, font=MONO))
        ry += ROW_H
    return "".join(out), height


def _render_band(canvas: Canvas, title: str, accent: str, panels: list[Panel],
                 *, note: str = "") -> None:
    y = canvas.y
    canvas.add(
        f'<rect x="{MARGIN}" y="{y}" width="{W - 2 * MARGIN}" height="{BAND_TITLE_H}" rx="8" '
        f'fill="{accent}" fill-opacity="0.10" stroke="{accent}" stroke-opacity="0.35"/>'
    )
    canvas.add(_text(MARGIN + 14, y + 20, title, size=13.5, fill=accent, weight="700", spacing="1.6"))
    if note:
        canvas.add(_text(W - MARGIN - 14, y + 20, note, size=10.5, fill=MUTED, font=MONO,
                         anchor="end"))
    y += BAND_TITLE_H + 12

    count = len(panels)
    width = (W - 2 * MARGIN - GAP * (count - 1)) / count
    tallest = 0
    for i, panel in enumerate(panels):
        markup, height = _render_panel(panel, MARGIN + i * (width + GAP), y, width)
        canvas.add(markup)
        tallest = max(tallest, height)
        _assert_panel_fits(panel, width)
    canvas.y = y + tallest + BAND_GAP


def _assert_panel_fits(panel: Panel, width: float) -> None:
    inner = width - 2 * PANEL_PAD - 18
    head = width - 2 * PANEL_PAD - (32 if panel.index else 0)
    problems = []
    if not _fits(panel.title, 15.5, head, mono=False):
        problems.append(f"title {panel.title!r}")
    if panel.subtitle and not _fits(panel.subtitle, 10.5, width - 2 * PANEL_PAD, mono=True):
        problems.append(f"subtitle {panel.subtitle!r}")
    for row in panel.rows:
        if not _fits(row.label, 12.5, inner, mono=False):
            problems.append(f"label {row.label!r}")
        if row.detail and not _fits(row.detail, 9.6, inner, mono=True):
            problems.append(f"detail {row.detail!r}")
    if problems:
        _OVERFLOWS.append(f"panel {panel.title!r} overflows {width:.0f}px: " + "; ".join(problems))


def _document(height: int, aria: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" '
        f'viewBox="0 0 {W} {height}" role="img" aria-label="{escape(aria)}">'
        '<defs>'
        '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#050a14"/><stop offset="0.55" stop-color="#071426"/>'
        '<stop offset="1" stop-color="#06131c"/></linearGradient>'
        '</defs>'
        f'<rect width="{W}" height="{height}" fill="url(#bg)"/>'
        f'{body}</svg>\n'
    )


def _header(canvas: Canvas, title: str, lede: str, chips: list[str]) -> None:
    canvas.add(_text(MARGIN, 58, title, size=34, fill="#f8fafc", weight="700", spacing="-0.6"))
    canvas.add(_text(MARGIN, 86, lede, size=13.5, fill=MUTED))
    x = W - MARGIN
    for chip in reversed(chips):
        width = len(chip) * 10.5 * MONO_EM + 22
        x -= width
        canvas.add(
            f'<rect x="{x:.1f}" y="40" width="{width:.1f}" height="26" rx="8" fill="#0d1726" '
            f'stroke="{CYAN}" stroke-opacity="0.4"/>'
        )
        canvas.add(_text(x + width / 2, 57, chip, size=10.5, fill=CYAN, font=MONO, anchor="middle"))
        x -= 10
    canvas.y = 108


def _footer(canvas: Canvas, note: str) -> None:
    canvas.add(_text(MARGIN, canvas.y + 6, note, size=10.5, fill=FAINT, font=MONO))
    canvas.add(_text(W - MARGIN, canvas.y + 6, f"AIWorkHub v{VERSION} · MIT · github.com/shrec/AIWorkHub",
                     size=10.5, fill=FAINT, font=MONO, anchor="end"))
    canvas.y += 26


def index_facts(root: Path = ROOT) -> dict[str, str]:
    """Return live index counts, or a truthful 'not built' marker."""

    db = root / ".aiworkhub" / "source_graph" / "source_graph.sqlite"
    if not db.is_file():
        return {"state": "index not built in this checkout"}
    try:
        with closing(connect_readonly(db)) as conn:
            files, entities, edges = conn.execute(
                "SELECT (SELECT COUNT(*) FROM files), (SELECT COUNT(*) FROM entities), "
                "(SELECT COUNT(*) FROM edges)"
            ).fetchone()
            languages = conn.execute("SELECT COUNT(DISTINCT language) FROM files").fetchone()[0]
    except sqlite3.Error as exc:  # pragma: no cover - environment dependent
        return {"state": f"index unreadable: {exc}"}
    return {
        "state": "live",
        "files": f"{files:,}",
        "entities": f"{entities:,}",
        "edges": f"{edges:,}",
        "languages": str(languages),
    }


def render_system_diagram(stamp: str) -> str:
    canvas = Canvas()
    _header(
        canvas,
        "AIWorkHub — System Block Diagram",
        "Observe → Decide → Delegate → Verify → Promote → Learn.  One repository-bound control plane: "
        "no cloud account, no HTTP service, MCP over stdio only.",
        [f"v{VERSION}", stamp, "code-backed architecture"],
    )

    _render_band(canvas, "SEATS  ·  WHO TALKS TO THE REPOSITORY", CYAN, [
        Panel("VS Code extension", CYAN, "vscode-extension/extension.js", (
            Row("Dashboard webview", "tasks · needfix · roadmap · cost · storage"),
            Row("Initialize AIWorkHub", "creates .aiworkhub/ and the first index"),
            Row("Hosts the MCP server", "stdio child process, one per repository"),
        )),
        Panel("Manager chat", CYAN, "Claude / Codex over MCP", (
            Row("Bootstrap first", "aiworkhub_manager_bootstrap verifies repo + route"),
            Row("Does not write code", "distributes, reviews, accepts or rejects"),
            Row("Is the independent reviewer", "independence = did not write the code"),
        )),
        Panel("Worker runtimes", CYAN, "runtime_adapters.py · process_launcher.py", (
            Row("One card, one process", "exact argv built per adapter id"),
            Row("Own MCP surface", "worker_ai_tools_mcp.py — 20 tools"),
            Row("Stops at review_ready", "a worker never finalizes its own card"),
        )),
    ], note="repository-bound · no global database")

    _render_band(canvas, "MCP SURFACE  ·  server.py — 143 registered tools, stdio only", BLUE, [
        Panel("Repository authority", BLUE, "repository_mux.py · storage_registry.py", (
            Row("Verified repo_id wins", "outranks cwd, workspace_roots, chat prose"),
            Row("Mismatch stops the seat", "switch or reload the route, never fall back"),
            Row("Per-repository storage", "resolved through the registry, never from cwd"),
        )),
        Panel("Manager tools", BLUE, "46 aiworkhub_manager_* + task / dashboard tools", (
            Row("Discovery", "source_graph_query · context_graph_* · kb_* · ai_memory_*"),
            Row("Lifecycle", "task_create · launch_task · accept_review · reject_review"),
            Row("Operations", "needfix · roadmap · cost ledger · storage retention"),
        )),
        Panel("Worker tools", BLUE, "worker_ai_tools_mcp.py", (
            Row("Bounded discovery", "worker_source_graph_query only — no Context Graph"),
            Row("Bounded editing", "semantic_edit_prepare → _apply on a hashed range"),
            Row("Write intents", "session / memory / KB never written directly"),
        )),
    ], note="MAX_TOOL_OUTPUT_BYTES 16 KiB · raw ceiling 512 KiB")

    _render_band(canvas, "CONTROL PLANE  ·  FIVE PILLARS", MAGENTA, [
        Panel("Intake & plan", ROSE, "what is worth doing", (
            Row("NeedFix ledger", "needfix_store.py — obstacles with measured evidence"),
            Row("Roadmap outcomes", "roadmap_store.py"),
            Row("Canonical card", "task_store.py · task_fsm.py — one id, real states"),
            Row("Dependency DAG", "task_plan.py · task_decomposition.py"),
            Row("Collision guard", "overlapping allowed_writes is sequential work"),
            Row("Template gate", "task_templates.py — required outputs must change"),
        ), index="1"),
        Panel("Context authorities", GREEN, "answer without re-reading the tree", (
            Row("Source Graph", "source_graph*.py — 37 modes, structural index"),
            Row("Context Graph", "context_graph.py — manager seat only"),
            Row("AI Memory + KB", "manager_ai_tools.py — durable decisions and contracts"),
            Row("Session Manager", "session_current_state / session_write"),
            Row("Write intents", "context_write_intents.py — manager accepts each one"),
            Row("Skills + lessons", "skill_registry.py · learning_commit.py"),
        ), index="2"),
        Panel("Routing & launch", MAGENTA, "who does it, and when", (
            Row("Workforce catalog", "workforce_catalog.py — runners the account can use"),
            Row("Outcome ranking", "workforce_router.py — cost and fit, not vendor"),
            Row("Launch queue", "launch_queue_contract.py · launch_replay_guard.py"),
            Row("Ready waves", "dependency_autolaunch.py — parallel only if disjoint"),
            Row("Claim then spawn", "process_launcher.py — pending → processing atomically"),
            Row("Liveness + finalize", "worker_supervisor.py · task_reconciler.py"),
        ), index="3"),
        Panel("Isolated execution", CYAN, "the only place code changes", (
            Row("Worktree per card", "worker_workspace.py · worktree_storage.py"),
            Row("Sandbox", "Landlock write-fencing · seccomp · Windows AppContainer"),
            Row("Semantic edit", "semantic_edit.py — one hash-verified line range"),
            Row("Validation", "validation_runner.py — exact commands the card named"),
            Row("Evidence capture", "attempt_artifacts.py · evidence_instruments.py"),
            Row("Stops at review", "task_mark_review, then the process exits"),
        ), index="4"),
        Panel("Review & promotion", AMBER, "measurement decides, not prose", (
            Row("Mechanical gates first", "quality_run_checks · declared_invariants.py"),
            Row("Risk-tiered lenses", "quality_review.py · quality_reviewer.py"),
            Row("Reviewer queue", "review_orchestrator.py — reservations, not a graveyard"),
            Row("Sealed receipts", "quality_review_receipt.py · sarif_contract.py"),
            Row("Manager decision", "accept_review → canonical tree, or reject_review"),
            Row("Learning owed", "learning_commit_store.py — every decision names one"),
        ), index="5"),
    ], note="cards that overlap in allowed_writes are never launched in parallel")

    _render_band(canvas, "DURABLE RUNTIME  ·  WHAT SURVIVES A CRASH", AMBER, [
        Panel("Callbacks", AMBER, "a wake-up, never an acceptance", (
            Row("callback_store / channel / bridge", "durable events, zero dead letters"),
            Row("completion_inbox.py", "the manager reads evidence, then decides"),
        )),
        Panel("Ledgers", AMBER, "measurement that cannot be faked", (
            Row("process_event_ledger.py", "exact pid identity, not a status string"),
            Row("cost_ledger.py · provider_usage.py", "tokens and cost per card"),
        )),
        Panel("Audit", AMBER, "HMAC-authenticated receipts", (
            Row("audit_system.py · scoped_audit.py", "injected · live · cache · zero-hit"),
            Row("read_efficiency.py", "what a seat actually acquired"),
        )),
        Panel("Retention", AMBER, "bounded local storage", (
            Row("storage_retention.py", "quarantine → restore → purge, never silent"),
            Row("task_retention.py · terminal_log_retention.py", "a pin has more than one reader"),
        )),
    ])

    _render_band(canvas, "STORAGE  ·  <repo>/.aiworkhub/ — repository-local, nothing leaves the machine", GREEN, [
        Panel("tasks", GREEN, "tasks.sqlite", (Row("cards, states, receipts", "one database per repository"),)),
        Panel("source graph", GREEN, "source_graph/source_graph.sqlite", (Row("entities · edges · fts", "see the Source Graph diagram"),)),
        Panel("context", GREEN, "context/ · skills.sqlite", (Row("session · memory · KB · skills", "write intents applied by the manager"),)),
        Panel("workspaces", GREEN, "worktrees/ · logs/", (Row("candidate trees + terminal logs", "hashes are the review evidence"),)),
        Panel("config", GREEN, "config/storage.json · config/source_graph.json", (Row("canonical_active binding", "credentials stay outside the repository"),)),
    ])

    _footer(canvas, f"Generated by {GENERATOR}; selected facts come from package code and the local index.")
    height = canvas.y + 12
    aria = (
        "AIWorkHub system block diagram: seats and MCP surface over a five-pillar control plane of "
        "intake and plan, context authorities, routing and launch, isolated execution, and review and "
        "promotion, above a durable runtime of callbacks, ledgers, audit and retention, all persisted "
        "in repository-local .aiworkhub storage."
    )
    return _document(height, aria, "".join(canvas.parts))


def render_source_graph_diagram(stamp: str) -> str:
    facts = index_facts()
    if facts["state"] == "live":
        scale = (
            f"this repository right now: {facts['files']} files · {facts['entities']} entities · "
            f"{facts['edges']} edges · {facts['languages']} languages"
        )
    else:
        scale = facts["state"]

    canvas = Canvas()
    _header(
        canvas,
        "Source Graph — AIWorkHub's structural index",
        "One repository-local SQLite graph. The write path turns files into bounded structural evidence; "
        "the read path answers a model from that evidence instead of re-scanning the tree.",
        [f"v{VERSION}", stamp, f"{len(sg.SOURCE_GRAPH_MODES)} modes"],
    )

    _render_band(canvas, "REFRESH CONTROL  ·  source_graph_daemon.py — one daemon per resolved repository root", AMBER, [
        Panel("A timer, not a watcher", AMBER, "no inotify import exists", (
            Row("Periodic refresh event", "wait loop with a floor, never a filesystem hook"),
        )),
        Panel("The build leaves the server", AMBER, "python -m aiworkhub.source_graph_daemon", (
            Row("--build-once --incremental", "an index build never blocks the MCP loop"),
        )),
        Panel("Concurrent refreshes coalesce", AMBER, "build lock acquired non-blocking", (
            Row("N requests, one follow-up build", "a scan cannot starve the interactive server"),
        )),
        Panel("Truthful lifecycle", AMBER, "refresh-job.json (O_EXCL temp + fsync + replace)", (
            Row("queued → running → succeeded | failed", "stopped indexing ready empty standby degraded stale"),
        )),
    ])

    _render_band(canvas, "WRITE PATH  ·  INDEXING", CYAN, [
        Panel("Discover", CYAN, "which files may enter the index at all", (
            Row("load_ignore_policy", ".aiworkhub/config/source_graph.json · fails closed"),
            Row(f"{len(sg.DEFAULT_EXCLUDE_DIR_NAMES)} non-bypassable excludes",
                "DEFAULT_EXCLUDE_DIR_NAMES cannot be widened away"),
            Row("iter_source_files", "os.walk(followlinks=False) · sorted · de-duplicated"),
            Row(f"{len(sg.LANGUAGE_CAPABILITIES)} language families",
                "LANGUAGE_CAPABILITIES · registry invariant on import"),
            Row(f"MAX_POLICY_BYTES {sg.MAX_POLICY_BYTES:,}", "an oversized policy is refused, not truncated"),
        ), index="1"),
        Panel("Triage", CYAN, "decide what actually changed", (
            Row("Prior generation read back", "files: source_hash · size · mtime_ns · build_revision"),
            Row("Stat gate", "revision + size + mtime_ns + extractor set must match"),
            Row(f"Hash pool (threads, ceiling {sg.MAX_SOURCE_GRAPH_HASH_WORKERS})",
                f"only above MIN_PARALLEL_HASH_BYTES {sg.MIN_PARALLEL_HASH_BYTES:,}"),
            Row(f"_stable_content_hash ×{sg.SOURCE_GRAPH_HASH_STABLE_READ_ATTEMPTS}",
                "stat / read / stat — pre must equal post"),
            Row("Unstable read is never trusted", "returns None → routed to full extraction"),
        ), index="2"),
        Panel("Extract", CYAN, "parse each file, touching no database", (
            Row(f"Extraction pool (procs, ceiling {sg.MAX_SOURCE_GRAPH_EXTRACT_WORKERS})",
                f"only above MIN_PARALLEL_EXTRACTION_BYTES {sg.MIN_PARALLEL_EXTRACTION_BYTES:,}"),
            Row("source_graph_ast.extract_file", "the single entry point for every adapter"),
            Row("Adapters", "python ast · tree-sitter JS/TS · polyglot · cpp · php"),
            Row(f"{len(sga.ENTITY_KINDS)} entity kinds · {len(sga.EDGE_KINDS)} edge kinds",
                "module namespace class struct enum macro fn method …"),
            Row("Evidence labels", "EXTRACTED · INFERRED · AMBIGUOUS · FILE_EVIDENCE"),
        ), index="3"),
        Panel("Merge", CYAN, "exactly one writer touches the database", (
            Row("index_write_lease", "flock LOCK_EX|LOCK_NB on source_graph/index.lock"),
            Row("Writer pragmas", "journal_mode=DELETE · synchronous=NORMAL · busy 30000ms"),
            Row("SAVEPOINT per file", "a torn file cannot poison the generation"),
            Row("_invalidate_file → _write_extraction", "old rows deleted, then files/entities/fts/edges"),
            Row("Deletions in the same pass", "vanished and renamed paths dropped, not stranded"),
        ), index="4"),
        Panel("Resolve & score", GREEN, "bind across files, then grade the generation", (
            Row("Cross-file binders", "cpp + JS/TS always; python binder only for .py"),
            Row("Ambiguity is preserved", ">1 candidate → AMBIGUOUS, never attached to a guess"),
            Row("materialize_git_metrics", "git log 90d --numstat — at index time only"),
            Row("History short-circuit", "skipped while meta.git_history_head == HEAD"),
            Row("_index_quality_scorecard", "written per generation into index_quality_history"),
        ), index="5"),
    ], note="source_graph.py · source_graph_ast.py · source_graph_languages.py · source_graph_insights.py")

    _render_band(canvas, f"THE INDEX  ·  <repo>/.aiworkhub/source_graph/source_graph.sqlite", GREEN, [
        Panel("entities", GREEN, "kind · qualname · lines · signature", (Row("evidence_label per row", "no global database, no cross-repository graph"),)),
        Panel("edges", GREEN, " · ".join(sga.EDGE_KINDS[:4]), (Row(" · ".join(sga.EDGE_KINDS[4:]), "every row stamped with the build revision"),)),
        Panel("entities_fts", GREEN, "fts5(name, qualname, signature, file_path)", (Row("bm25 weights 10.0 / 6.0 / 2.0 / 0.5", "retrieval is lexical, ranking is graph-derived"),)),
        Panel("files · file_history", GREEN, "source_hash · size · mtime_ns · build_revision", (Row("90-day churn, authors, ownership", "history is materialised, never computed per query"),)),
        Panel("meta · index_quality_history", GREEN, "last_build · index_quality · git_history_head", (Row("per-generation scorecard archive", "sidecars: index.lock · refresh-job.json"),)),
    ], note=f"{sg.SCHEMA_ID} · {sg.BUILD_REVISION} · {scale}")

    _render_band(canvas, "READ PATH  ·  ANSWERING A MODEL", MAGENTA, [
        Panel("Bind & gate", MAGENTA, "which database may answer, and for whom", (
            Row("_resolve_source_graph_db", "canonical → candidate_overlay → rework_overlay"),
            Row("Registry, never cwd", "config/storage.json must be canonical_active"),
            Row("A stale index refuses", "source_graph_db_wrong_revision:<rev>"),
            Row("Target allowlist", "outside the declared scope → target_not_allowed"),
            Row("Identity is closed over", "no tool argument names a repo, database or task"),
        ), index="1"),
        Panel("Retrieve", MAGENTA, "find candidates without scanning the tree", (
            Row("find() FTS5 escalation", "phrase-prefix → tokens AND → tokens OR · first wins"),
            Row("bm25 column weights", "bm25(entities_fts, 10.0, 6.0, 2.0, 0.5)"),
            Row("Tiered ORDER BY", "exact qualname → exact name → name prefix → rest"),
            Row("Exact modes bypass find", "file · function · class · body · bodygrep"),
            Row("composed_find", "review overlay: partition schemas first, base last"),
        ), index="2"),
        Panel("Rank", MAGENTA, "deterministic, from the graph — no model", (
            Row("priority_score", "in×4 + out×2 + loops×3 + branches + min(span//20, 10)"),
            Row("Call counts", "COUNT(*) on edges kind='calls' by dst_ and src_qualname"),
            Row("Branches / loops", "regex over a bounded slice of the symbol body"),
            Row("risk_reasons", "security_sensitive · branch_heavy · large_symbol"),
            Row(f"{len(sg.SOURCE_GRAPH_MODES)} modes", "focus/slice first, escalate only from returned evidence"),
        ), index="3"),
        Panel("Bound", MAGENTA, "every answer is row- and byte-capped", (
            Row(f"MAX_BUDGET_ROWS {sg.MAX_BUDGET_ROWS}", "re-clamped inside find(), not trusted from callers"),
            Row("Per-stage byte caps", "orientation 8 KiB · analysis 12 KiB · default 16 KiB"),
            Row("Overflow envelope", "aiworkhub.task_mcp.bounded_json_preview.v1"),
            Row("Last keys to be dropped", "ranked_symbols · related_tests · risks · todos"),
            Row("Truncation is reported", "truncated / next_cursor — never a silent short answer"),
        ), index="4"),
        Panel("Receipt & cache", BLUE, "what was really used, provably", (
            Row("_CACHE key", "includes build_revision + successful-build timestamp"),
            Row("Cache hit is declared", "cache_receipt {reuse_previous_result, content_sha256}"),
            Row("Signed ledger entry", "HMAC-SHA256 · worker_mcp_audit_entry.v1 · JSONL"),
            Row("Provenance", "prefetch | live | cache — injected never counts as live"),
            Row("Conformance gate", "workflow_stage per call · a green card can be refused"),
        ), index="5"),
    ], note="worker_ai_tools_mcp.source_graph_query · source_graph.find · source_graph_analytics · source_graph_partition")

    _render_band(canvas, "WHAT THE INDEX BUYS  ·  THE TWO CONSUMERS", CYAN, [
        Panel("Focused semantic edit", CYAN, "semantic_edit.py · WorkerSemanticEditSession", (
            Row("body(qualname) already carries the range", "exact file and 1-based line_start / line_end"),
            Row("prepare", "fragment + sha256(whole file) + sha256(fragment) — nothing is written"),
            Row("apply", "re-reads from disk and re-hashes: a stale range is refused, not merged"),
            Row("guards", "allowed_writes fnmatch · symlink walk · containment · UTF-8 · size cap"),
            Row("write", "assembled offline, then mkstemp + fsync + os.replace in one step"),
            Row("receipt", "file_bytes · old_region_bytes · replacement_bytes — bytes, not a token claim"),
        )),
        Panel("Review overlay — composed view", ROSE, "source_graph_partition.py · ComposedView", (
            Row("build_partition", "indexes only the review packet's changed_paths, fresh database"),
            Row("Base pin", "marker 'composed_view' pins the base by {db_path, size, mtime_ns}"),
            Row("Read-only attach", "the partition auto-attaches the base with ?mode=ro; base stays main"),
            Row("TEMP VIEWs", "partitions UNION ALL base WHERE file_path NOT IN temp._partition_scope"),
            Row("Scope collision", "bind refuses overlapping partition scopes (partition_scope_overlap)"),
            Row("Base moved underneath", "PartitionBaseShiftError — stale evidence is never served"),
        )),
    ])

    _footer(canvas, f"Generated by {GENERATOR}; selected constants come from package code at generation time.")
    height = canvas.y + 12
    aria = (
        "AIWorkHub Source Graph architecture: a daemon-driven refresh control, a five-stage write path of "
        "discover, triage, extract, merge and resolve, a repository-local SQLite index of entities, edges "
        "and full-text rows, and a five-stage read path of bind, retrieve, rank, bound and receipt that "
        "feeds focused semantic edits and the review overlay."
    )
    return _document(height, aria, "".join(canvas.parts))


def expected_diagrams(root: Path = ROOT, stamp: str | None = None) -> dict[Path, str]:
    stamp = stamp or f"cut {date.today().isoformat()}"
    _OVERFLOWS.clear()
    diagrams = {
        root / SYSTEM_SVG: render_system_diagram(stamp),
        root / SOURCE_GRAPH_SVG: render_source_graph_diagram(stamp),
    }
    if _OVERFLOWS:
        raise AssertionError("\n".join(_OVERFLOWS))
    return diagrams


def _strip_volatile(svg: str) -> str:
    """Blank the date and local index state so --check compares source, not host.

    The index ticks on every daemon refresh; drift that matters is a constant,
    path, mode or module that moved in the source. A clean checkout may have no
    index at all, and that must compare equal to an indexed developer machine.
    """

    import re

    svg = re.sub(r"cut \d{4}-\d{2}-\d{2}", "cut ----------", svg)
    return re.sub(
        r"(?:this repository right now:|index not built in this checkout|index unreadable:)[^<]*",
        "checkout-local index state",
        svg,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if a diagram is out of date")
    args = parser.parse_args(argv)

    expected = expected_diagrams()
    if args.check:
        stale = []
        for path, text in expected.items():
            if not path.is_file():
                stale.append(f"{path.relative_to(ROOT)}: missing")
            elif _strip_volatile(path.read_text(encoding="utf-8")) != _strip_volatile(text):
                stale.append(f"{path.relative_to(ROOT)}: out of date")
        for line in stale:
            print(line, file=sys.stderr)
        if stale:
            print(f"run: python3 {GENERATOR}", file=sys.stderr)
            return 1
        print(f"{len(expected)} diagrams up to date")
        return 0

    for path, text in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)} ({len(text):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
