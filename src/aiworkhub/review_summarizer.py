"""READ-ONLY MCP review queue summarizer (B110 contract).

A read-only tool that inspects the taskctl review queue, groups finished
worker tasks by topic/runner, and returns a Codex-ready review checklist
without mutating the queue. Never calls done/review/start/add-card.
Subprocess launch tripwire is always 0.

Hard invariants (asserted by tests):
  * READ-ONLY: no code path writes the task queue OR the audit log.
  * NO LAUNCH: this module never imports process-spawn machinery beyond
    core.run_taskctl (which itself enforces the write gate).
  * NO MUTATION: never calls taskctl done/review/start/add-card.
  * SUBPROCESS TRIPWIRE: subprocess launch tripwire=0 always.
  * No secret/env value logging — env var NAMES only.
"""

from __future__ import annotations

import json
import re
from typing import Any

from aiworkhub import core


READONLY: bool = True
LAUNCH_IMPLEMENTED: bool = False
SUBPROCESS_LAUNCH_TRIPWIRE: int = 0

# Regex to parse review-queue lines: "  [topic] [runner] task_id"
_REVIEW_LINE_RE = re.compile(
    r"^\s*\[(?P<topic>[^\]]+)\]\s*\[(?P<runner>[^\]]+)\]\s*(?P<task_id>\S+)(?:\s+.*)?$"
)


def _authority_flags() -> dict[str, bool]:
    return {
        "write_gate_enabled": not core.writes_allowed(),
        "readonly": READONLY,
        "process_launch": False,
        "agent_launch": False,
        "shell_invocation": False,
        "queue_write": False,
        "audit_write": False,
        "subprocess_launch_tripwire_zero": SUBPROCESS_LAUNCH_TRIPWIRE == 0,
    }


def _parse_review_queue_lines(stdout: str) -> list[dict[str, str]]:
    """Parse taskctl review-queue output lines.

    Format:
      === Codex Review Queue (N) ===
        [topic] [runner] task_id
    """
    entries: list[dict[str, str]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("==="):
            continue
        m = _REVIEW_LINE_RE.match(line)
        if m:
            entries.append({
                "topic": m.group("topic"),
                "runner": m.group("runner"),
                "task_id": m.group("task_id"),
            })
    return entries


def _extract_result(res: Any) -> tuple[Any, Any, str | None]:
    """Duck-typed extraction of (returncode, raw_stdout, error).

    Accepts either a ``core.TaskCtlResult.as_dict()`` mapping or a
    ``TaskCtlResult``-like object with ``.stdout``/``.returncode``. On a
    non-zero return code or unexpected type, returns
    ``(None, None, error_message)``. READ-ONLY — inspects only.
    """
    if isinstance(res, dict):
        if res.get("returncode") != 0:
            return None, None, res.get("stderr", "unknown")
        return res.get("returncode", 0), res.get("stdout", "{}"), None
    if hasattr(res, "stdout"):
        if res.returncode != 0:
            return None, None, getattr(res, "stderr", "unknown")
        return res.returncode, res.stdout, None
    return None, None, f"unexpected result type: {type(res)}"


def _collect_review_cards(run, show) -> tuple[str, list[dict[str, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    """READ-ONLY: fetch the review queue and every task card.

    Issues only ``taskctl review-queue`` and ``taskctl show <task_id>``
    (both read-only). NEVER issues a write command (done/review/start/
    auto-pickup/export-jsonl/usage). Returns
    ``(review_stdout, entries, cards, fetch_errors)``.
    """
    rq = run(["review-queue"])
    review_stdout = rq.stdout if hasattr(rq, "stdout") else rq.get("stdout", "")
    entries = _parse_review_queue_lines(review_stdout)

    cards: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, Any]] = []
    for entry in entries:
        tid = entry["task_id"]
        st = show(tid)
        _rc, raw_stdout, err = _extract_result(st)
        if err is not None:
            fetch_errors.append({"task_id": tid, "error": err})
            continue
        try:
            card = json.loads(raw_stdout)
        except json.JSONDecodeError:
            fetch_errors.append({"task_id": tid, "error": "JSON parse error"})
            continue
        cards.append(card)
    return review_stdout, entries, cards, fetch_errors


def summarize_review_queue(
    *,
    _run_taskctl=None,
    _show_task=None,
) -> dict[str, Any]:
    """READ-ONLY: Inspect review queue, group tasks, return Codex checklist.

    Calls taskctl review-queue (read-only) then taskctl show for each task.
    Groups by topic/runner. Returns grouped tasks, validation commands,
    allowed_writes counts, and a Codex review checklist.
    Never mutates queue state.

    The _run_taskctl and _show_task kwargs exist ONLY for test stubbing;
    they are never used in production and do not weaken the read-only
    invariant — the stub is injected by the test harness, not by any
    runtime/env path.
    """
    run = _run_taskctl if _run_taskctl is not None else core.run_taskctl
    show = _show_task if _show_task is not None else core.show_task

    review_stdout, entries, tasks, fetch_errors = _collect_review_cards(run, show)

    if not entries:
        return {
            "ok": True,
            "review_queue_raw": review_stdout.strip(),
            "task_count": 0,
            "grouped_tasks": {},
            "codex_review_checklist": [],
            "summary": {
                "total_tasks": 0,
                "topics": {},
                "runners": {},
            },
            "fetch_errors": [],
            "authority_flags": _authority_flags(),
        }

    # tasks + fetch_errors were collected read-only by _collect_review_cards.

    # 4. Group by topic/runner
    grouped: dict[str, list[dict[str, Any]]] = {}
    topic_counts: dict[str, int] = {}
    runner_counts: dict[str, int] = {}

    for card in tasks:
        topic = str(card.get("topic", "unknown"))
        runner = str(card.get("runner", "unknown"))
        key = f"{topic}/{runner}"
        grouped.setdefault(key, []).append(card)
        topic_counts[topic] = topic_counts.get(topic, 0) + 1
        runner_counts[runner] = runner_counts.get(runner, 0) + 1

    # 5. Build Codex review checklist
    checklist: list[dict[str, Any]] = []
    for card in tasks:
        tid = card.get("task_id", "unknown")
        validation = card.get("validation", [])
        allowed_writes = card.get("allowed_writes", [])
        checklist.append({
            "task_id": tid,
            "runner": card.get("runner", "unknown"),
            "topic": card.get("topic", "unknown"),
            "status": card.get("status", "unknown"),
            "worker_status": card.get("worker_status", "unknown"),
            "validation_commands": validation,
            "allowed_writes_count": len(allowed_writes),
            "allowed_writes": allowed_writes,
            "commit_contract": card.get("commit_contract", "unknown"),
            "mode": card.get("mode", "unknown"),
            "priority": card.get("priority", "unknown"),
            "objective": card.get("objective", ""),
        })

    return {
        "ok": True,
        "review_queue_raw": review_stdout.strip(),
        "task_count": len(tasks),
        "fetch_errors": fetch_errors,
        "grouped_tasks": {
            key: [t.get("task_id", "?") for t in cards]
            for key, cards in sorted(grouped.items())
        },
        "codex_review_checklist": checklist,
        "summary": {
            "total_tasks": len(tasks),
            "topics": topic_counts,
            "runners": runner_counts,
            "fetch_error_count": len(fetch_errors),
        },
        "authority_flags": _authority_flags(),
    }


# ── B116: Codex-ready HANDOFF report (READ-ONLY) ────────────────────────
#
# Deterministic REVIEW-SUPPORT VALIDATOR over task-card metadata. This is a
# correctness/support-boundary check (allowed by the neural-control doctrine),
# NOT runtime model cognition: it never routes intent, selects ISA/tools, or
# decides an abstain — it only summarizes an already-finished worker card for
# a human/Codex reviewer. Strictly read-only over the parent task queue.

HANDOFF_CONTRACT: str = "B116_v1_codex_handoff_e2e_readonly"

# Substrings that flag a write path as touching shared / coordinator-owned
# state (higher review risk when several workers run in parallel).
_SHARED_WRITE_MARKERS: tuple[str, ...] = (
    "server.py",
    "__init__.py",
    "registry",
    "manifest",
    "machine_task_cards",
    "active_default",
    ".policy.json",
    ".active.json",
    "session.json",
    "corrections.sqlite",
    "lexicon_scorecard",
)

# taskctl subcommands that MUTATE the queue — the handoff builder must never
# emit or invoke these; used only to assert read-only behaviour in tests.
WRITE_TASKCTL_COMMANDS: frozenset[str] = frozenset(
    {"auto-pickup", "done", "review", "start", "export-jsonl", "usage"}
)

_SEVERITY_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3}


def _derive_risks(card: dict[str, Any]) -> list[dict[str, str]]:
    """Deterministic risk checklist derived from card metadata (read-only)."""
    risks: list[dict[str, str]] = []
    raw_allowed = card.get("allowed_writes")
    allowed = raw_allowed if isinstance(raw_allowed, list) else []
    validation = card.get("validation", []) or []
    forbidden = card.get("forbidden", []) or []

    if not validation:
        risks.append({
            "code": "missing_validation",
            "severity": "high",
            "detail": "card declares no validation commands",
        })
    # A genuinely absent allowed_writes key, or an empty scope on a card that
    # DOES declare required_outputs, is a real risk. An intentionally-empty
    # readonly/no-output card (allowed_writes: [] with no required_outputs) is
    # not flagged -- no NO_WRITES sentinel needed.
    if raw_allowed is None or (not allowed and (card.get("required_outputs") or [])):
        risks.append({
            "code": "missing_allowed_writes",
            "severity": "high",
            "detail": "card declares no allowed_writes scope",
        })
    for path in allowed:
        low = str(path).lower()
        if any(marker in low for marker in _SHARED_WRITE_MARKERS):
            risks.append({
                "code": "shared_file_write",
                "severity": "medium",
                "detail": str(path),
            })
    if len(allowed) > 12:
        risks.append({
            "code": "wide_write_scope",
            "severity": "medium",
            "detail": f"{len(allowed)} allowed_writes paths",
        })
    if not forbidden:
        risks.append({
            "code": "missing_forbidden_guards",
            "severity": "low",
            "detail": "card lists no forbidden operations",
        })

    hb = card.get("human_brief")
    if isinstance(hb, dict):
        declared = str(hb.get("risk", "")).strip().lower()
        if declared in _SEVERITY_RANK:
            risks.append({
                "code": "declared_risk",
                "severity": declared,
                "detail": f"human_brief.risk={declared}",
            })
    return risks


def _risk_level(risks: list[dict[str, str]]) -> str:
    """Highest severity among risks; 'low' when there are none."""
    level = 0
    for r in risks:
        level = max(level, _SEVERITY_RANK.get(r.get("severity", "low"), 1))
    for name, rank in _SEVERITY_RANK.items():
        if rank == level:
            return name
    return "low"


def _commit_hygiene(card: dict[str, Any]) -> dict[str, Any]:
    """Derive commit-hygiene status from commit_contract + forbidden list."""
    contract = str(card.get("commit_contract", ""))
    contract_l = contract.lower()
    forbidden_l = {str(f).lower() for f in (card.get("forbidden", []) or [])}

    no_commit_preferred = "no_commit" in contract_l or "no-commit" in contract_l
    no_git_add_all = (
        "git add -a" in contract_l
        or "git add ." in contract_l
        or "git_add_a" in forbidden_l
    )
    mixed_task_commit_forbidden = (
        "mixed_task_commit" in forbidden_l
        or "mixed-task" in contract_l
        or "mixed task" in contract_l
    )
    scoped_stage_only = (
        "allowed_writes" in contract_l or "scoped" in contract_l or no_git_add_all
    )

    notes: list[str] = []
    if not contract:
        notes.append("no commit_contract declared")
    if not no_git_add_all:
        notes.append("no explicit git-add-all prohibition")
    if not mixed_task_commit_forbidden:
        notes.append("mixed-task commit not explicitly forbidden")

    status = "ok" if (bool(contract) and no_git_add_all and mixed_task_commit_forbidden) else "warn"
    return {
        "contract": contract,
        "no_commit_preferred": no_commit_preferred,
        "no_git_add_all": no_git_add_all,
        "mixed_task_commit_forbidden": mixed_task_commit_forbidden,
        "scoped_stage_only": scoped_stage_only,
        "status": status,
        "notes": notes,
    }


def _build_handoff_for_card(card: dict[str, Any]) -> dict[str, Any]:
    """Build one Codex-ready handoff entry for a finished worker card."""
    tid = card.get("task_id", "unknown")
    allowed = list(card.get("allowed_writes", []) or [])
    validation = list(card.get("validation", []) or [])

    risks = _derive_risks(card)
    hygiene = _commit_hygiene(card)
    if hygiene["status"] == "warn":
        risks.append({
            "code": "commit_hygiene_warn",
            "severity": "medium",
            "detail": "; ".join(hygiene["notes"]) or "commit hygiene warning",
        })

    # Scoped finalize path recommended for Codex — strings only, never run
    # by this tool (it is read-only). Mirrors the repo commit policy:
    # stage explicit paths -> guard-staged -> verify diff -> done.
    recommended_stage_commands = [
        f"python3 AITools/taskctl.py stage {tid}",
        f"python3 AITools/taskctl.py guard-staged {tid}",
        "git diff --cached --name-only   # must equal allowed_writes",
        f"python3 AITools/taskctl.py done {tid}",
    ]

    return {
        "task_id": tid,
        "runner": card.get("runner", "unknown"),
        "topic": card.get("topic", "unknown"),
        "status": card.get("status", "unknown"),
        "worker_status": card.get("worker_status", "unknown"),
        "objective": card.get("objective", ""),
        "mode": card.get("mode", "unknown"),
        "priority": card.get("priority", "unknown"),
        "allowed_writes": allowed,
        "allowed_writes_count": len(allowed),
        "validation_commands": validation,
        "validation_present": bool(validation),
        "forbidden": list(card.get("forbidden", []) or []),
        "risks": risks,
        "risk_level": _risk_level(risks),
        "commit_hygiene": hygiene,
        "recommended_stage_commands": recommended_stage_commands,
        "scoped_paths": allowed,
    }


def build_codex_handoff_report(
    *,
    _run_taskctl=None,
    _show_task=None,
    task_ids: list[str] | None = None,
    batch_label: str | None = None,
) -> dict[str, Any]:
    """READ-ONLY: Codex-ready HANDOFF report over the review queue.

    For each task waiting on Codex review, summarizes task_id, runner, topic,
    allowed_writes, validation commands, derived risks, commit-hygiene status,
    and a scoped stage/finalize command list — WITHOUT mutating the queue.

    Reads via ``taskctl review-queue`` + ``taskctl show`` only. Never issues a
    write command, never launches a process, never enables the write gate.

    The ``_run_taskctl`` / ``_show_task`` kwargs exist ONLY for test stubbing
    (a fixture queue); they are never used in production and cannot weaken the
    read-only invariant — the stub is injected by the test harness.
    """
    run = _run_taskctl if _run_taskctl is not None else core.run_taskctl
    show = _show_task if _show_task is not None else core.show_task

    review_stdout, _entries, cards, fetch_errors = _collect_review_cards(run, show)

    if task_ids:
        wanted = set(task_ids)
        cards = [c for c in cards if c.get("task_id") in wanted]

    reports = [_build_handoff_for_card(c) for c in cards]

    grouped: dict[str, list[str]] = {}
    topic_counts: dict[str, int] = {}
    runner_counts: dict[str, int] = {}
    risk_counts: dict[str, int] = {"low": 0, "medium": 0, "high": 0}
    hygiene_warn: list[str] = []
    missing_validation: list[str] = []
    for r in reports:
        key = f"{r['topic']}/{r['runner']}"
        grouped.setdefault(key, []).append(r["task_id"])
        topic_counts[r["topic"]] = topic_counts.get(r["topic"], 0) + 1
        runner_counts[r["runner"]] = runner_counts.get(r["runner"], 0) + 1
        risk_counts[r["risk_level"]] = risk_counts.get(r["risk_level"], 0) + 1
        if r["commit_hygiene"]["status"] != "ok":
            hygiene_warn.append(r["task_id"])
        if not r["validation_present"]:
            missing_validation.append(r["task_id"])

    report: dict[str, Any] = {
        "ok": True,
        "contract": HANDOFF_CONTRACT,
        "readonly": READONLY,
        "mutation_performed": False,
        "review_queue_raw": review_stdout.strip(),
        "task_count": len(reports),
        "handoff_reports": reports,
        "grouped_tasks": {k: sorted(v) for k, v in sorted(grouped.items())},
        "summary": {
            "total_tasks": len(reports),
            "topics": topic_counts,
            "runners": runner_counts,
            "risk_levels": risk_counts,
            "commit_hygiene_ok": len(reports) - len(hygiene_warn),
            "commit_hygiene_warn": hygiene_warn,
            "tasks_missing_validation": missing_validation,
            "fetch_error_count": len(fetch_errors),
        },
        "fetch_errors": fetch_errors,
        "authority_flags": _authority_flags(),
    }
    if task_ids:
        report["filtered_by_task_ids"] = True
        report["requested_task_ids"] = list(task_ids)
    if batch_label:
        report["batch_label"] = batch_label
    return report


# ── B119: Codex-ready HANDOFF markdown render (READ-ONLY) ───────────────
#
# Pure-function renderer over build_codex_handoff_report output. Performs
# NO taskctl I/O of its own — it only formats an already-fetched report
# dict into a compact deterministic Markdown packet, so it cannot mutate
# the queue no matter how it is called. A thin wrapper below reuses
# build_codex_handoff_report (read-only, review-queue + show only) to
# produce the "render straight from the live/fixture queue" convenience
# path used by the MCP tool and tests.

MARKDOWN_RENDER_CONTRACT: str = "B119_v1_codex_handoff_markdown_render_readonly"


def render_codex_handoff_markdown(report: dict[str, Any]) -> str:
    """Deterministic, pure-function Markdown renderer.

    Input is exactly the dict returned by ``build_codex_handoff_report``
    (or ``build_codex_handoff_markdown_report``, which embeds the same
    shape). Does no I/O, no taskctl calls, no queue access — a plain data
    transform, so the read-only invariant holds unconditionally.
    """
    lines: list[str] = []
    contract = report.get("contract", "unknown")
    task_count = report.get("task_count", 0)
    batch_label = report.get("batch_label")

    lines.append(f"# Codex Handoff Review Packet ({contract})")
    if batch_label:
        lines.append(f"_batch: {batch_label}_")
    lines.append("")

    summary = report.get("summary", {}) or {}
    risk_levels = summary.get("risk_levels", {}) or {}
    hygiene_warn = summary.get("commit_hygiene_warn", []) or []
    missing_val = summary.get("tasks_missing_validation", []) or []

    lines.append(f"- tasks: {task_count}")
    lines.append(
        "- risk_levels: "
        + ", ".join(f"{k}={risk_levels.get(k, 0)}" for k in ("low", "medium", "high"))
    )
    lines.append(f"- commit_hygiene_ok: {summary.get('commit_hygiene_ok', 0)}")
    lines.append(
        "- commit_hygiene_warn: " + (", ".join(sorted(hygiene_warn)) if hygiene_warn else "none")
    )
    lines.append(
        "- missing_validation: " + (", ".join(sorted(missing_val)) if missing_val else "none")
    )
    lines.append("")

    for rep in report.get("handoff_reports", []) or []:
        tid = rep.get("task_id", "unknown")
        allowed = rep.get("allowed_writes", []) or []
        validation = rep.get("validation_commands", []) or []
        risks = rep.get("risks", []) or []

        lines.append(f"## {tid}")
        lines.append(f"- runner/topic: {rep.get('runner', '?')}/{rep.get('topic', '?')}")
        lines.append(
            f"- risk_level: {rep.get('risk_level', '?')}"
            f"  commit_hygiene: {rep.get('commit_hygiene', {}).get('status', '?')}"
        )
        lines.append(
            f"- allowed_writes ({rep.get('allowed_writes_count', len(allowed))}): "
            + (", ".join(allowed) if allowed else "none")
        )
        lines.append("- validation: " + (", ".join(validation) if validation else "NONE"))
        if risks:
            risk_str = "; ".join(f"{r.get('code')}[{r.get('severity')}]" for r in risks)
        else:
            risk_str = "none"
        lines.append(f"- risks: {risk_str}")
        stage_cmds = rep.get("recommended_stage_commands", []) or []
        lines.append("- next: " + (" && ".join(stage_cmds) if stage_cmds else "n/a"))
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def build_codex_handoff_markdown_report(
    *,
    _run_taskctl=None,
    _show_task=None,
    task_ids: list[str] | None = None,
    batch_label: str | None = None,
) -> dict[str, Any]:
    """READ-ONLY: build the B116 handoff report, then render it as Markdown.

    All taskctl I/O is delegated to ``build_codex_handoff_report`` (reads
    only ``review-queue`` + ``show``, never mutates). The Markdown itself is
    produced by the pure ``render_codex_handoff_markdown`` function, so this
    wrapper adds zero additional read-only surface beyond what B116 already
    proved safe. Returns the original report dict plus a ``markdown`` field
    and a ``markdown_contract`` label; every B116 field (task_count, risks,
    commit_hygiene, recommended_stage_commands, authority_flags, ...) stays
    present unchanged for programmatic callers.
    """
    report = build_codex_handoff_report(
        _run_taskctl=_run_taskctl,
        _show_task=_show_task,
        task_ids=task_ids,
        batch_label=batch_label,
    )
    result = dict(report)
    result["markdown"] = render_codex_handoff_markdown(result)
    result["markdown_contract"] = MARKDOWN_RENDER_CONTRACT
    return result
