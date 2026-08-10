from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiworkhub import core, launch_queue_persist, task_store


USAGE_LINE_RE = re.compile(
    r"^(?P<task_id>\S+) \| runner=(?P<runner>[^|]+) \| topic=(?P<topic>[^|]+) "
    r"\| records=(?P<records>\d+) \| tokens=(?P<tokens>\d+) "
    r"\| in=(?P<input_tokens>\d+) out=(?P<output_tokens>\d+) \| cost=\$(?P<cost>[0-9.]+)"
)
_ECONOMIC_MODEL_ALIASES: tuple[tuple[str, str], ...] = (
    ("gpt-5.3-codex-spark", "gpt-5.3-codex-spark"),
    ("gpt-5.3-codex", "gpt-5.3-codex"),
    ("gpt-5.5", "gpt-5.5"),
    ("deepseek-v4-flash", "deepseek-v4-flash"),
    ("deepseek-v4-pro", "deepseek-v4-pro"),
    ("glm-5.2", "glm-5.2"),
    ("claude-sonnet-5", "sonnet"),
    ("claude-sonnet", "sonnet"),
    ("claude-opus-5", "opus"),
    ("claude-opus", "opus"),
    ("claude-haiku-4-5", "haiku"),
    ("claude-haiku-4.5", "haiku"),
    ("claude-haiku", "haiku"),
)


def _parse_usage_stdout(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        match = USAGE_LINE_RE.match(line.strip())
        if not match:
            continue
        data = match.groupdict()
        rows.append({
            "source": "taskctl_usage_report",
            "task_id": data["task_id"],
            "runner": data["runner"].strip(),
            "topic": data["topic"].strip(),
            "model": "",
            "role": "unknown",
            "role_observed": False,
            "records": int(data["records"]),
            "input_tokens": int(data["input_tokens"]),
            "output_tokens": int(data["output_tokens"]),
            "total_tokens": int(data["tokens"]),
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "cache_metrics_observed": False,
            "cost_usd": float(data["cost"]),
            "cost_known": not (
                int(data["tokens"]) > 0 and float(data["cost"]) == 0.0
            ),
            "day": "",
        })
    return rows


def _launch_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in summary.get("last_entries", []) or []:
        task_id = entry.get("task_id")
        if not task_id:
            continue
        ts = entry.get("requested_at") or entry.get("ts") or ""
        day = ""
        if isinstance(ts, str) and len(ts) >= 10:
            day = ts[:10]
        elif isinstance(ts, (float, int)):
            day = datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()
        rows.append({
            "source": "launch_queue_audit",
            "task_id": task_id,
            "runner": entry.get("runner") or "",
            "topic": entry.get("topic") or "",
            "model": entry.get("model") or "",
            "role": "unknown",
            "role_observed": False,
            "records": 1,
            "input_tokens": int(entry.get("usage_input_tokens") or 0),
            "output_tokens": int(entry.get("usage_output_tokens") or 0),
            "total_tokens": int(entry.get("usage_total_tokens") or 0),
            "cached_input_tokens": int(entry.get("usage_cached_input_tokens") or 0),
            "cache_creation_input_tokens": int(entry.get("usage_cache_creation_input_tokens") or 0),
            "cache_write_input_tokens": int(entry.get("usage_cache_write_input_tokens") or 0),
            "cache_metrics_observed": bool(entry.get("usage_cache_metrics_observed")),
            "cost_usd": float(entry.get("cost_usd") or 0.0),
            "cost_known": not (
                int(entry.get("usage_total_tokens") or 0) > 0
                and float(entry.get("cost_usd") or 0.0) == 0.0
            ),
            "day": day,
        })
    return rows


def _canonical_usage_rows(repo_root: Path | str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in task_store.list_usage_events(repo_root, limit=10_000):
        created_at = str(entry.get("created_at") or "")
        topic = str(entry.get("topic") or "")
        explicit_role = str(entry.get("role") or "").strip().lower()
        role = (
            explicit_role
            if explicit_role in {"worker", "reviewer"}
            else "reviewer" if topic == "quality_review" else "worker"
        )
        total_tokens = int(entry.get("total_tokens") or 0)
        cost_usd = float(entry.get("cost_usd") or 0.0)
        cost_observed = entry.get("cost_observed")
        usage_observed = entry.get("usage_observed")
        if usage_observed is None:
            usage_observed = bool(
                total_tokens
                or entry.get("cache_metrics_observed")
                or cost_observed
            )
        note = str(entry.get("note") or "")
        rows.append({
            "source": "canonical_usage_event",
            "task_id": str(entry.get("task_id") or ""),
            "runner": str(entry.get("runner") or ""),
            "topic": topic,
            "model": str(entry.get("model") or ""),
            "role": role,
            "role_observed": explicit_role in {"worker", "reviewer"},
            "requested_model": str(entry.get("requested_model") or ""),
            "observed_model": str(entry.get("observed_model") or ""),
            "model_observed": bool(entry.get("model_observed")),
            "provider": str(entry.get("provider") or ""),
            "records": int(entry.get("records") or 1),
            "input_tokens": int(entry.get("input_tokens") or 0),
            "output_tokens": int(entry.get("output_tokens") or 0),
            "visible_output_tokens": int(entry.get("visible_output_tokens") or 0),
            "reasoning_output_tokens": int(entry.get("reasoning_output_tokens") or 0),
            "total_tokens": total_tokens,
            "cached_input_tokens": int(entry.get("cached_input_tokens") or 0),
            "cache_creation_input_tokens": int(entry.get("cache_creation_input_tokens") or 0),
            "cache_write_input_tokens": int(entry.get("cache_write_input_tokens") or 0),
            "cache_metrics_observed": bool(entry.get("cache_metrics_observed")),
            "usage_observed": bool(usage_observed),
            "telemetry_reason": str(entry.get("telemetry_reason") or ""),
            "cost_usd": cost_usd,
            "cost_observed": bool(cost_observed),
            "cost_known": (
                bool(cost_observed)
                if cost_observed is not None
                else not (total_tokens > 0 and cost_usd == 0.0)
            ),
            "source_detail": str(entry.get("source") or ""),
            "note": note,
            "attempt_id": note.removeprefix("task_mcp_request:") if note.startswith("task_mcp_request:") else "",
            "created_at": created_at,
            "day": created_at[:10] if len(created_at) >= 10 else "",
        })
    return rows


def _aggregate(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "records": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "visible_output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "usage_observed_records": 0,
        "usage_unknown_records": 0,
        "cache_observed_records": 0,
        "cache_eligible_input_tokens": 0,
        "cache_hit_ratio_numerator": 0,
        "cost_usd": 0.0,
        "cost_known_records": 0,
        "cost_unknown_records": 0,
        "tokens_with_unknown_cost": 0,
    })
    for row in rows:
        bucket = str(row.get(key) or "unknown")
        out[bucket]["records"] += int(row.get("records") or 0)
        out[bucket]["input_tokens"] += int(row.get("input_tokens") or 0)
        out[bucket]["output_tokens"] += int(row.get("output_tokens") or 0)
        out[bucket]["visible_output_tokens"] += int(
            row.get("visible_output_tokens") or 0
        )
        out[bucket]["reasoning_output_tokens"] += int(
            row.get("reasoning_output_tokens") or 0
        )
        out[bucket]["total_tokens"] += int(row.get("total_tokens") or 0)
        out[bucket]["cached_input_tokens"] += int(row.get("cached_input_tokens") or 0)
        out[bucket]["cache_creation_input_tokens"] += int(
            row.get("cache_creation_input_tokens") or 0
        )
        out[bucket]["cache_write_input_tokens"] += int(
            row.get("cache_write_input_tokens") or 0
        )
        if row.get("usage_observed"):
            out[bucket]["usage_observed_records"] += int(row.get("records") or 0)
        else:
            out[bucket]["usage_unknown_records"] += int(row.get("records") or 0)
        if row.get("cache_metrics_observed"):
            out[bucket]["cache_observed_records"] += int(row.get("records") or 0)
            out[bucket]["cache_eligible_input_tokens"] += int(row.get("input_tokens") or 0)
            out[bucket]["cache_hit_ratio_numerator"] += int(row.get("cached_input_tokens") or 0)
        out[bucket]["cost_usd"] = round(out[bucket]["cost_usd"] + float(row.get("cost_usd") or 0.0), 6)
        if row.get("cost_known"):
            out[bucket]["cost_known_records"] += int(row.get("records") or 0)
        else:
            out[bucket]["cost_unknown_records"] += int(row.get("records") or 0)
            out[bucket]["tokens_with_unknown_cost"] += int(row.get("total_tokens") or 0)
    for aggregate in out.values():
        denominator = int(aggregate["cache_eligible_input_tokens"] or 0)
        numerator = int(aggregate.pop("cache_hit_ratio_numerator", 0) or 0)
        aggregate["cache_hit_ratio"] = (
            round(numerator / denominator, 6)
            if denominator > 0
            else None
        )
    return dict(sorted(out.items()))


def _model_outcome_matrix(
    usage_rows: list[dict[str, Any]],
    decisions: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Associate each task's latest recorded model attempt with its decision."""

    usage_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in usage_rows:
        task_id = str(row.get("task_id") or "")
        if task_id:
            usage_by_task[task_id].append(row)
    matrix: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "decided_tasks": 0,
        "accepted": 0,
        "rejected": 0,
        "acceptance_rate_percent": None,
        "usage_observed_tasks": 0,
        "cost_observed_tasks": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    })
    unmatched_decisions = 0
    for task_id, decision_row in decisions.items():
        candidates = usage_by_task.get(task_id) or []
        if not candidates:
            unmatched_decisions += 1
            continue
        decision_at = str(decision_row.get("created_at") or "")
        eligible = [
            row
            for row in candidates
            if not decision_at
            or not str(row.get("created_at") or "")
            or str(row.get("created_at") or "") <= decision_at
        ]
        if not eligible:
            unmatched_decisions += 1
            continue
        usage = max(eligible, key=lambda row: str(row.get("created_at") or ""))
        model = str(
            usage.get("observed_model")
            or usage.get("model")
            or usage.get("requested_model")
            or "unknown"
        )
        bucket = matrix[model]
        decision = str(decision_row.get("decision") or "")
        bucket["decided_tasks"] += 1
        if decision == "accepted":
            bucket["accepted"] += 1
        elif decision == "rejected":
            bucket["rejected"] += 1
        if usage.get("usage_observed"):
            bucket["usage_observed_tasks"] += 1
            bucket["total_tokens"] += int(usage.get("total_tokens") or 0)
        if usage.get("cost_known"):
            bucket["cost_observed_tasks"] += 1
            bucket["cost_usd"] = round(
                float(bucket["cost_usd"]) + float(usage.get("cost_usd") or 0.0),
                6,
            )
    for bucket in matrix.values():
        decided = int(bucket["decided_tasks"] or 0)
        bucket["acceptance_rate_percent"] = (
            round(100.0 * int(bucket["accepted"] or 0) / decided, 1)
            if decided
            else None
        )
    return {
        "schema_id": "aiworkhub.model_outcome_matrix.v1",
        "association_only": True,
        "attribution": "latest_usage_attempt_at_or_before_latest_manager_decision",
        "models": dict(sorted(matrix.items())),
        "unmatched_decisions": unmatched_decisions,
    }


def _task_family(card: dict[str, Any]) -> str:
    """Return the explicit routing family recorded on a canonical card.

    Historical cards do not persist the workforce ``kinds`` argument.  The
    project-context task type is the closest durable authority and is mapped
    without inspecting prose.  Missing/unsupported values remain ``unknown``
    instead of being guessed from titles or topics.
    """

    if str(card.get("topic") or "").strip() == "quality_review":
        return "review"
    project_context = card.get("project_context")
    task_type = (
        str(project_context.get("task_type") or "").strip().lower()
        if isinstance(project_context, dict)
        else ""
    )
    return {
        "code": "code",
        "research": "research",
        "data_classification": "linguistic",
    }.get(task_type, "unknown")


def _task_risk(card: dict[str, Any]) -> str:
    risk = str(card.get("risk_tier") or "").strip().lower()
    return risk if risk in {"low", "medium", "high", "critical"} else "unknown"


def _economic_model_identity(row: dict[str, Any]) -> str:
    """Normalize only declared provider model aliases, never adapter names."""

    candidates = (
        row.get("observed_model"),
        row.get("model"),
        row.get("requested_model"),
    )
    for candidate in candidates:
        raw = str(candidate or "").strip()
        folded = raw.casefold()
        if not raw:
            continue
        for marker, canonical in _ECONOMIC_MODEL_ALIASES:
            if marker in folded:
                return canonical
        if folded not in {
            "claude_cli", "codex_cli", "vscode_lm", "glm_vscode_lm",
            "deepseek_vscode_lm", "deepseek_copilot_cli", "glm_copilot_cli",
        }:
            return raw
    return ""


def _economic_route_identity(row: dict[str, Any]) -> str:
    """Return the recorded adapter/transport authority for one usage row."""

    explicit = str(row.get("adapter_id") or "").strip()
    if explicit:
        return explicit
    provider = str(row.get("provider") or "").strip().lower()
    return {
        "claude": "claude_cli",
        "codex": "codex_cli",
        "deepseek_copilot": "deepseek_copilot_cli",
        "glm_copilot": "glm_copilot_cli",
    }.get(provider, provider or "unknown")


def cost_per_accepted_outcome_view(
    usage_rows: list[dict[str, Any]],
    decisions: dict[str, dict[str, str]],
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Join exact task cost and manager decisions on one matched population.

    A task is attributable to a model only when every worker attempt for that
    task reports the same non-empty model identity.  Reviewer usage is not
    charged to the worker model.  Any unknown-cost attempt makes that exact
    model/family bucket ``UNKNOWN``; zero accepted outcomes makes it
    ``UNMEASURED``.  Consequently neither missing telemetry nor a zero-dollar
    placeholder can rank as free.
    """

    card_by_task = {
        str(card.get("task_id") or ""): card
        for card in cards
        if str(card.get("task_id") or "")
    }
    usage_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in usage_rows:
        task_id = str(row.get("task_id") or "")
        if task_id and str(row.get("role") or "worker") == "worker":
            usage_by_task[task_id].append(row)

    buckets: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(lambda: {
        "matched_decided_tasks": 0,
        "accepted_outcomes": 0,
        "rejected_outcomes": 0,
        "attempt_records": 0,
        "cost_known_attempts": 0,
        "cost_unknown_attempts": 0,
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    })
    unmatched_decisions = 0
    unknown_model_tasks = 0
    mixed_model_tasks = 0
    for task_id, decision_row in decisions.items():
        decision = str(decision_row.get("decision") or "").strip().lower()
        if decision not in {"accepted", "rejected"}:
            continue
        rows = usage_by_task.get(task_id) or []
        if not rows:
            unmatched_decisions += 1
            continue
        models = {_economic_model_identity(row) for row in rows}
        models.discard("")
        if not models:
            unknown_model_tasks += 1
            continue
        if len(models) != 1:
            mixed_model_tasks += 1
            continue
        routes = {_economic_route_identity(row) for row in rows}
        if len(routes) != 1:
            mixed_model_tasks += 1
            continue
        model = next(iter(models))
        route = next(iter(routes))
        family = _task_family(card_by_task.get(task_id) or {})
        risk = _task_risk(card_by_task.get(task_id) or {})
        bucket = buckets[(model, route, family, risk)]
        bucket["matched_decided_tasks"] += 1
        bucket[f"{decision}_outcomes"] += 1
        bucket["attempt_records"] += len(rows)
        for row in rows:
            bucket["total_tokens"] += int(row.get("total_tokens") or 0)
            if row.get("cost_known") is True:
                bucket["cost_known_attempts"] += 1
                bucket["total_cost_usd"] = round(
                    float(bucket["total_cost_usd"])
                    + float(row.get("cost_usd") or 0.0),
                    6,
                )
            else:
                bucket["cost_unknown_attempts"] += 1

    routes: dict[str, dict[str, dict[str, dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(dict))
    )
    for (model, route, family, risk), raw in sorted(buckets.items()):
        row = dict(raw)
        attempts = int(row["attempt_records"])
        accepted = int(row["accepted_outcomes"])
        unknown_cost = int(row["cost_unknown_attempts"])
        if unknown_cost:
            state = "UNKNOWN"
            reason = "one_or_more_matched_attempt_costs_unknown"
            cost_per_accepted: float | None = None
        elif accepted == 0:
            state = "UNMEASURED"
            reason = "no_accepted_outcome_in_matched_population"
            cost_per_accepted = None
        else:
            state = "MEASURED"
            reason = "complete_matched_cost_and_acceptance_population"
            cost_per_accepted = round(
                float(row["total_cost_usd"]) / accepted, 6
            )
        row.update({
            "state": state,
            "reason": reason,
            "cost_coverage": (
                round(int(row["cost_known_attempts"]) / attempts, 6)
                if attempts else None
            ),
            "acceptance_rate": (
                round(accepted / int(row["matched_decided_tasks"]), 6)
                if row["matched_decided_tasks"] else None
            ),
            "cost_per_accepted_outcome_usd": cost_per_accepted,
            "risk_partition": risk,
            "risk_evidence": (
                "explicit_task_card"
                if risk != "unknown"
                else "not_persisted_on_historical_task_card"
            ),
        })
        routes[model][route][family][risk] = row

    return {
        "schema_id": "aiworkhub.cost_per_accepted_outcome.v1",
        "mode": "advisory_only",
        "automatic_routing": False,
        "attribution": "single_worker_model_per_task_all_attempt_costs",
        "routes": {
            model: {
                route: {
                    family: dict(sorted(risks.items()))
                    for family, risks in sorted(families.items())
                }
                for route, families in sorted(model_routes.items())
            }
            for model, model_routes in sorted(routes.items())
        },
        "unmatched_decisions": unmatched_decisions,
        "unknown_model_tasks": unknown_model_tasks,
        "mixed_model_tasks": mixed_model_tasks,
        "claim_boundary": (
            "Measured rows are repository associations, not causal savings. "
            "Shadow or canary activation requires matched uncapped parity "
            "evidence partitioned by task family and risk."
        ),
    }


def _retry_economics(
    usage_rows: list[dict[str, Any]],
    decisions: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Measure durable repeated attempts without calling activity savings."""

    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in usage_rows:
        task_id = str(row.get("task_id") or "")
        if task_id:
            by_task[task_id].append(row)
    retry_rows: list[dict[str, Any]] = []
    retried_tasks: set[str] = set()
    for task_id, rows in by_task.items():
        ordered = sorted(rows, key=lambda row: str(row.get("created_at") or ""))
        if len(ordered) > 1:
            retried_tasks.add(task_id)
            retry_rows.extend(ordered[1:])
    accepted_retried = sum(
        1
        for task_id in retried_tasks
        if str((decisions.get(task_id) or {}).get("decision") or "") == "accepted"
    )
    return {
        "schema_id": "aiworkhub.retry_economics.v1",
        "association_only": True,
        "tasks_with_usage": len(by_task),
        "attempt_records": len(usage_rows),
        "tasks_with_retries": len(retried_tasks),
        "retry_records": len(retry_rows),
        "retry_record_rate_percent": (
            round(100.0 * len(retry_rows) / len(usage_rows), 1)
            if usage_rows else None
        ),
        "retry_tokens": sum(int(row.get("total_tokens") or 0) for row in retry_rows),
        "retry_cost_usd": round(
            sum(
                float(row.get("cost_usd") or 0.0)
                for row in retry_rows
                if row.get("cost_known")
            ),
            6,
        ),
        "retry_cost_unknown_records": sum(
            1 for row in retry_rows if not row.get("cost_known")
        ),
        "accepted_retried_tasks": accepted_retried,
        "accepted_rate_among_retried_tasks_percent": (
            round(100.0 * accepted_retried / len(retried_tasks), 1)
            if retried_tasks else None
        ),
        "claim_boundary": (
            "Repeated usage records are measured attempts. They do not prove "
            "that the retry caused acceptance or that its tokens were avoidable."
        ),
    }


def build_cost_ledger(
    *,
    repo_root: Path | str | None = None,
    runner: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    include_tasks: bool = False,
) -> dict[str, Any]:
    if repo_root is not None:
        usage_rows = _canonical_usage_rows(repo_root)
        try:
            manager_decisions = task_store.latest_manager_decisions(repo_root)
        except task_store.TaskStoreError:
            manager_decisions = {}
        if runner:
            usage_rows = [row for row in usage_rows if row.get("runner") == runner]
        if topic:
            usage_rows = [row for row in usage_rows if row.get("topic") == topic]
        if status:
            usage_rows = [row for row in usage_rows if row.get("status") == status]
        scoped_task_ids = {str(row.get("task_id") or "") for row in usage_rows}
        manager_decisions = {
            task_id: decision
            for task_id, decision in manager_decisions.items()
            if task_id in scoped_task_ids
        }
        # The persisted launch queue is process-global and carries no complete
        # repository identity.  Never mix it into an explicitly repo-bound
        # catalog; canonical usage events are the sole authority here.
        usage = {"ok": True}
        launch_summary = {"ok": True}
        launch_rows: list[dict[str, Any]] = []
    else:
        manager_decisions = {}
        usage = core.usage_report(runner=runner, topic=topic, status=status)
        usage_stdout = usage.get("stdout", "")
        usage_rows = _parse_usage_stdout(
            usage_stdout if isinstance(usage_stdout, str) else ""
        )
        launch_summary = launch_queue_persist.read_persisted_log(max_entries=10_000)
        launch_rows = _launch_rows(launch_summary)

    if repo_root is not None:
        # Every canonical usage event is a real attempt. Collapsing on
        # task/runner/model hides retries and under-reports their cost.
        union_rows = list(usage_rows)
    else:
        seen: set[tuple[str, str, str]] = set()
        union_rows = []
        for row in usage_rows + launch_rows:
            ident = (
                str(row.get("task_id") or ""),
                str(row.get("runner") or ""),
                str(row.get("model") or ""),
            )
            if ident in seen:
                continue
            seen.add(ident)
            union_rows.append(row)

    try:
        cards = (
            task_store.list_task_cards(repo_root, limit=5000)
            if repo_root is not None
            else []
        )
    except task_store.TaskStoreError:
        cards = []
    accepted_economics = cost_per_accepted_outcome_view(
        usage_rows, manager_decisions, cards
    )

    return {
        "tool": "aiworkhub_task_cost_ledger",
        "contract": "B288_v1_readonly_cost_ledger",
        "readonly": True,
        "filters": {"runner": runner, "topic": topic, "status": status},
        "counts": {
            "usage_rows": len(usage_rows),
            "launch_rows": len(launch_rows),
            "union_rows": len(union_rows),
        },
        "cost_quality": {
            "known_records": sum(int(row.get("records") or 0) for row in union_rows if row.get("cost_known")),
            "unknown_records": sum(int(row.get("records") or 0) for row in union_rows if not row.get("cost_known")),
            "tokens_with_unknown_cost": sum(
                int(row.get("total_tokens") or 0)
                for row in union_rows if not row.get("cost_known")
            ),
            "zero_cost_is_free": False,
            "reason": "provider_cost_absence_is_unknown_not_zero",
        },
        "cache_quality": {
            "observed_records": sum(
                int(row.get("records") or 0)
                for row in union_rows
                if row.get("cache_metrics_observed")
            ),
            "cached_input_tokens": sum(
                int(row.get("cached_input_tokens") or 0) for row in union_rows
            ),
            "cache_creation_input_tokens": sum(
                int(row.get("cache_creation_input_tokens") or 0) for row in union_rows
            ),
            "cache_write_input_tokens": sum(
                int(row.get("cache_write_input_tokens") or 0) for row in union_rows
            ),
            "absent_metrics_are_unknown_not_zero": True,
        },
        "role_quality": {
            "explicit_records": sum(
                int(row.get("records") or 0)
                for row in union_rows
                if row.get("role_observed")
            ),
            "legacy_inferred_records": sum(
                int(row.get("records") or 0)
                for row in union_rows
                if not row.get("role_observed")
            ),
            "legacy_inference": "quality_review_topic_is_reviewer_otherwise_worker",
        },
        "aggregates": {
            "by_topic": _aggregate(union_rows, "topic"),
            "by_runner": _aggregate(union_rows, "runner"),
            "by_model": _aggregate(union_rows, "model"),
            "by_provider": _aggregate(union_rows, "provider"),
            "by_role": _aggregate(union_rows, "role"),
            "by_day": _aggregate(union_rows, "day"),
        },
        "model_outcomes": _model_outcome_matrix(usage_rows, manager_decisions),
        "cost_per_accepted_outcome": accepted_economics,
        "retry_economics": _retry_economics(usage_rows, manager_decisions),
        "tasks": union_rows if include_tasks else [],
        "authority_flags": {
            "runtime_authority": False,
            "support_authority": False,
            "queue_write": False,
            "audit_write": False,
            "process_launch": False,
            "agent_launch": False,
        },
        "source_status": {
            "usage_report_ok": bool(usage.get("ok")),
            "launch_log_ok": bool(launch_summary.get("ok")),
        },
    }
