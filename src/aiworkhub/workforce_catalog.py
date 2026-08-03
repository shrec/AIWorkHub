"""Repository-local model inventory backed by observed task outcomes.

Configuration declares capability only. Availability, quota and performance
are never invented: runtime readiness and canonical task/process evidence are
joined at read time, with missing observations labeled explicitly.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from . import repo_policy, task_store, workforce_router


SCHEMA_ID = "aiworkhub.workforce_catalog.v1"
CATALOG_RELATIVE_PATH = Path(".aiworkhub/config/workforce.json")
AUDIT_RELATIVE_PATH = Path(".aiworkhub/config/workforce.audit.jsonl")
MAX_CATALOG_BYTES = 256 * 1024
MAX_WORKERS = 64
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

# Keep the declared worker/provider/model identity stable while allowing an
# equivalent launch transport when the preferred VS Code LM surface is not
# visible in the current host.  The effective adapter remains explicit in the
# catalog response; this is routing evidence, not fabricated credential/quota
# evidence.
_WORKER_ADAPTER_FALLBACKS: dict[str, tuple[str, ...]] = {
    "claude_cli": ("vscode_lm",),
    "codex_cli": ("vscode_lm",),
    "deepseek_vscode_lm": ("vscode_lm", "deepseek_copilot_cli"),
    "glm_vscode_lm": ("vscode_lm", "glm_copilot_cli"),
}

_EDITOR_MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    "haiku": ("claude-haiku-4.5",),
    "sonnet": ("claude-sonnet-5", "claude-sonnet-4.6", "claude-sonnet-4.5"),
    "opus": ("claude-opus-5", "claude-opus-4.8-fast", "claude-opus-4.8"),
}


DEFAULT_WORKERS: tuple[dict[str, Any], ...] = (
    {"worker_id": "claude-haiku", "adapter_id": "claude_cli", "model": "haiku", "provider": "anthropic", "supports": ["mechanical", "code", "review"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 200_000, "max_risk": "medium", "quality_ceiling": 0.85},
    {"worker_id": "claude-sonnet-5", "adapter_id": "claude_cli", "model": "sonnet", "provider": "anthropic", "supports": ["mechanical", "code", "research", "linguistic", "review"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 1_000_000, "max_risk": "high", "quality_ceiling": 0.97},
    {"worker_id": "claude-opus-5", "adapter_id": "claude_cli", "model": "opus", "provider": "anthropic", "supports": ["code", "research", "linguistic", "review"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 1_000_000, "max_risk": "critical", "quality_ceiling": 1.0},
    {"worker_id": "gpt-5.5", "adapter_id": "codex_cli", "model": "gpt-5.5", "provider": "openai", "supports": ["code", "research", "linguistic", "review"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 921_000, "max_risk": "critical", "quality_ceiling": 1.0},
    {"worker_id": "gpt-5.3-codex", "adapter_id": "codex_cli", "model": "gpt-5.3-codex", "provider": "openai", "supports": ["mechanical", "code", "review"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 272_000, "max_risk": "high", "quality_ceiling": 0.96},
    {"worker_id": "gpt-5.3-codex-spark", "adapter_id": "codex_cli", "model": "gpt-5.3-codex-spark", "provider": "openai", "supports": ["mechanical", "code"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 272_000, "max_risk": "medium", "quality_ceiling": 0.88},
    {"worker_id": "deepseek-v4-pro", "adapter_id": "deepseek_vscode_lm", "model": "deepseek-v4-pro", "provider": "deepseek", "supports": ["mechanical", "code", "research", "review"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 1_000_000, "max_risk": "high", "quality_ceiling": 0.96},
    {"worker_id": "deepseek-v4-flash", "adapter_id": "deepseek_vscode_lm", "model": "deepseek-v4-flash", "provider": "deepseek", "supports": ["mechanical", "code"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 1_000_000, "max_risk": "medium", "quality_ceiling": 0.86},
    {"worker_id": "glm-5.2", "adapter_id": "glm_vscode_lm", "model": "glm-5.2", "provider": "zhipu", "supports": ["mechanical", "code", "research", "review"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 1_000_000, "max_risk": "high", "quality_ceiling": 0.95},
)


class WorkforceCatalogError(RuntimeError):
    pass


def catalog_path(repo_root: Path | str) -> Path:
    return Path(repo_root).resolve() / CATALOG_RELATIVE_PATH


def _tokens(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 32:
        raise WorkforceCatalogError(f"{field}_must_be_bounded_list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _TOKEN_RE.fullmatch(item):
            raise WorkforceCatalogError(f"{field}_invalid_token")
        normalized = item.strip().lower()
        if normalized not in out:
            out.append(normalized)
    return out


def _worker(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkforceCatalogError("worker_must_be_object")
    worker_id = str(value.get("worker_id") or "").strip()
    adapter_id = str(value.get("adapter_id") or "").strip()
    model = str(value.get("model") or "").strip()
    provider = str(value.get("provider") or "").strip().lower()
    if not all(_TOKEN_RE.fullmatch(item) for item in (worker_id, adapter_id, model, provider)):
        raise WorkforceCatalogError("worker_identity_invalid")
    if adapter_id not in repo_policy.DEFAULT_POLICY["providers"]["allowed_adapters"]:
        raise WorkforceCatalogError("worker_adapter_unsupported")
    max_context = value.get("max_context_tokens", 0)
    if isinstance(max_context, bool) or not isinstance(max_context, int) or not 0 <= max_context <= 10_000_000:
        raise WorkforceCatalogError("max_context_tokens_out_of_range")
    risk = str(value.get("max_risk") or "medium").strip().lower()
    if risk not in workforce_router.RISK_ORDER:
        raise WorkforceCatalogError("max_risk_invalid")
    try:
        ceiling = float(value.get("quality_ceiling", 1.0))
        adjustment = float(value.get("manager_score_adjustment", 0.0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise WorkforceCatalogError("worker_score_invalid") from exc
    if not math.isfinite(ceiling) or not 0.0 <= ceiling <= 1.0:
        raise WorkforceCatalogError("quality_ceiling_out_of_range")
    if not math.isfinite(adjustment) or not -20.0 <= adjustment <= 20.0:
        raise WorkforceCatalogError("manager_score_adjustment_out_of_range")
    supports = _tokens(value.get("supports"), "supports")
    if not supports or set(supports) - set(workforce_router.TASK_KINDS):
        raise WorkforceCatalogError("worker_supports_invalid")
    return {
        "worker_id": worker_id,
        "adapter_id": adapter_id,
        "model": model,
        "provider": provider,
        "enabled": bool(value.get("enabled", True)),
        "supports": supports,
        "tools": _tokens(value.get("tools", []), "tools"),
        "max_context_tokens": max_context,
        "max_risk": risk,
        "quality_ceiling": ceiling,
        "manager_score_adjustment": adjustment,
    }


def validate_catalog(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema_id") != SCHEMA_ID:
        raise WorkforceCatalogError("catalog_schema_invalid")
    raw_workers = value.get("workers")
    if not isinstance(raw_workers, list) or len(raw_workers) > MAX_WORKERS:
        raise WorkforceCatalogError("workers_must_be_bounded_list")
    workers = [_worker(item) for item in raw_workers]
    ids = [item["worker_id"] for item in workers]
    if len(ids) != len(set(ids)):
        raise WorkforceCatalogError("duplicate_worker_id")
    revision = value.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise WorkforceCatalogError("catalog_revision_invalid")
    return {"schema_id": SCHEMA_ID, "revision": revision, "workers": workers}


def _default_catalog() -> dict[str, Any]:
    return validate_catalog({"schema_id": SCHEMA_ID, "revision": 1, "workers": [dict(item, enabled=True, manager_score_adjustment=0.0) for item in DEFAULT_WORKERS]})


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_CATALOG_BYTES:
        raise WorkforceCatalogError("catalog_too_large")
    fd, name = tempfile.mkstemp(prefix=".workforce-", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


def load_catalog(repo_root: Path | str) -> dict[str, Any]:
    path = catalog_path(repo_root)
    if not path.exists():
        return {**_default_catalog(), "configured": False}
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CATALOG_BYTES:
        raise WorkforceCatalogError("catalog_file_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkforceCatalogError(f"catalog_invalid_json:{type(exc).__name__}") from exc
    return {**validate_catalog(value), "configured": True}


def ensure_catalog(repo_root: Path | str) -> tuple[Path, bool]:
    root = Path(repo_root).resolve()
    if not (root / ".aiworkhub/project.json").is_file():
        raise WorkforceCatalogError("repository_not_initialized")
    path = catalog_path(root)
    if path.exists():
        load_catalog(root)
        return path, False
    _atomic_write(path, _default_catalog())
    return path, True


def _append_audit(root: Path, actor: Mapping[str, str], action: str, worker_id: str, revision: int) -> None:
    path = root / AUDIT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "schema_id": "aiworkhub.workforce_audit.v1",
        "timestamp": datetime.now().astimezone().isoformat(),
        "action": action,
        "worker_id": worker_id,
        "revision": revision,
        "role": str(actor.get("role") or "manager")[:40],
        "provider": str(actor.get("provider") or "manager")[:80],
        "actor_id_suffix": str(actor.get("actor_id") or "")[-12:],
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def upsert_worker(repo_root: Path | str, worker: Mapping[str, Any], *, actor: Mapping[str, str]) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    ensure_catalog(root)
    catalog = load_catalog(root)
    normalized = _worker(worker)
    workers = [dict(item) for item in catalog["workers"]]
    index = next((idx for idx, item in enumerate(workers) if item["worker_id"] == normalized["worker_id"]), None)
    action = "updated" if index is not None else "created"
    if index is None:
        if len(workers) >= MAX_WORKERS:
            raise WorkforceCatalogError("worker_limit_reached")
        workers.append(normalized)
    else:
        workers[index] = normalized
    revision = int(catalog["revision"]) + 1
    payload = validate_catalog({"schema_id": SCHEMA_ID, "revision": revision, "workers": workers})
    _atomic_write(catalog_path(root), payload)
    _append_audit(root, actor, action, normalized["worker_id"], revision)
    return {"ok": True, "action": action, "worker_id": normalized["worker_id"], "revision": revision}


def _iso_seconds(start: Any, finish: Any) -> float | None:
    try:
        first = datetime.fromisoformat(str(start))
        last = datetime.fromisoformat(str(finish))
    except (TypeError, ValueError):
        return None
    return max(0.0, (last - first).total_seconds())


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return float(ordered[index])


def _canonical_cards(root: Path) -> list[dict[str, Any]]:
    return task_store.list_task_cards(root, limit=5000)


def _resolve_effective_adapter(
    worker: Mapping[str, Any],
    ready_by_adapter: Mapping[str, Mapping[str, Any]],
) -> tuple[str, Mapping[str, Any]]:
    def launchable(adapter_id: str, readiness: Mapping[str, Any]) -> bool:
        if not readiness.get("launchable"):
            return False
        if adapter_id not in {
            "vscode_lm",
            "deepseek_vscode_lm",
            "glm_vscode_lm",
        }:
            return True
        observed = readiness.get("observed_models")
        # Older/fake preflight receipts have no catalog. Preserve their
        # explicit launchable verdict; current receipts always include a
        # bounded observed_models list and therefore get exact model gating.
        if not isinstance(observed, list):
            return True
        visible = {str(value) for value in observed if isinstance(value, str)}
        model = str(worker.get("model") or "")
        accepted = {model, *_EDITOR_MODEL_ALIASES.get(model, ())}
        return bool(visible.intersection(accepted))

    declared = str(worker.get("adapter_id") or "")
    candidate = ready_by_adapter.get(declared, {})
    if launchable(declared, candidate):
        return declared, candidate
    for fallback in _WORKER_ADAPTER_FALLBACKS.get(declared, ()):
        candidate = ready_by_adapter.get(fallback, {})
        if launchable(fallback, candidate):
            return fallback, candidate
    return declared, ready_by_adapter.get(declared, {})


def build_catalog(
    repo_root: Path | str,
    *,
    cards: Iterable[Mapping[str, Any]] | None = None,
    process_rows: Iterable[Mapping[str, Any]] | None = None,
    usage_rows: Iterable[Mapping[str, Any]] | None = None,
    preflight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    catalog = load_catalog(root)
    task_cards = [dict(item) for item in (cards if cards is not None else _canonical_cards(root))]
    processes = [dict(item) for item in (process_rows or [])]
    usage = [dict(item) for item in (usage_rows or [])]
    card_by_task = {str(item.get("task_id") or ""): item for item in task_cards}
    identity_recovered = 0
    for process in processes:
        task_id = str(process.get("task_id") or "")
        card = card_by_task.get(task_id) or {}
        terminal = card.get("terminal_review") if isinstance(card, Mapping) else {}
        evidence = terminal.get("evidence") if isinstance(terminal, Mapping) else {}
        recovered_fields: list[str] = []
        if not str(process.get("model") or "") and isinstance(evidence, Mapping):
            model = str(evidence.get("model") or "")
            if model:
                process["model"] = model
                recovered_fields.append("model")
        if not str(process.get("adapter_id") or "") and isinstance(evidence, Mapping):
            adapter = str(evidence.get("adapter_id") or "")
            if adapter:
                process["adapter_id"] = adapter
                recovered_fields.append("adapter_id")
        if not str(process.get("runner") or ""):
            runner = str(card.get("runner") or "") if isinstance(card, Mapping) else ""
            if runner:
                process["runner"] = runner
                recovered_fields.append("runner")
        if recovered_fields:
            process["identity_evidence_source"] = "canonical_terminal_review"
            process["identity_recovered_fields"] = recovered_fields
            identity_recovered += 1
    readiness = preflight or repo_policy.build_preflight(root)
    ready_by_adapter = {
        str(item.get("adapter_id") or ""): item
        for item in readiness.get("providers") or []
        if isinstance(item, Mapping)
    }
    rows: list[dict[str, Any]] = []
    attributed_process_ids: set[int] = set()
    for worker in catalog["workers"]:
        effective_adapter, adapter_ready = _resolve_effective_adapter(
            worker, ready_by_adapter
        )
        adapter_ids_to_match = {worker["adapter_id"], effective_adapter}
        matched: list[dict[str, Any]] = []
        for index, process in enumerate(processes):
            if str(process.get("adapter_id") or "") not in adapter_ids_to_match:
                continue
            if str(process.get("model") or "") != worker["model"]:
                continue
            matched.append(process)
            attributed_process_ids.add(index)
        task_ids = {str(item.get("task_id") or "") for item in matched if item.get("task_id")}
        runners = {str(item.get("runner") or "") for item in matched if item.get("runner")}
        matched_usage = [
            item for item in usage
            if str(item.get("task_id") or "") in task_ids
            and (
                not str(item.get("model") or "")
                or str(item.get("model") or "") == worker["model"]
            )
            and (
                not runners
                or not str(item.get("runner") or "")
                or str(item.get("runner") or "") in runners
            )
        ]
        matched_cards = [card_by_task[task_id] for task_id in task_ids if task_id in card_by_task]
        sample_count = len(matched_cards)
        accepted = sum(1 for item in matched_cards if str(item.get("status") or "") == "finished")
        review_ready = sum(1 for item in matched_cards if str(item.get("status") or "") in {"review", "finished"})
        failed = 0
        for item in matched_cards:
            substatus = str(item.get("terminal_substatus") or "")
            verification = item.get("deterministic_verification")
            if substatus in {"validation_failed", "launch_failed", "timed_out"} or (
                isinstance(verification, Mapping) and verification.get("passed") is False
            ):
                failed += 1
        latencies = [
            value
            for value in (_iso_seconds(item.get("started_at"), item.get("finished_at")) for item in matched)
            if value is not None
        ]
        attempts = len(matched)
        retries = max(0, attempts - sample_count)
        usage_source = matched_usage or matched
        tokens = sum(int(item.get("total_tokens") or 0) for item in usage_source)
        known_cost_rows = [item for item in usage_source if item.get("cost_known") is not False]
        cost = sum(float(item.get("cost_usd") or 0.0) for item in known_cost_rows)
        unknown_cost_tokens = sum(
            int(item.get("total_tokens") or 0)
            for item in usage_source
            if item.get("cost_known") is False
        )
        accepted_rate = accepted / sample_count if sample_count else None
        review_rate = review_ready / sample_count if sample_count else None
        failure_rate = failed / sample_count if sample_count else None
        retry_rate = retries / attempts if attempts else None
        effective = (
            max(0.0, min(accepted_rate or 0.0, review_rate or 0.0) - (failure_rate or 0.0))
            if sample_count else None
        )
        observed_score = (
            round(100.0 * (0.8 * effective + 0.2 * (1.0 - (retry_rate or 0.0))), 2)
            if effective is not None else None
        )
        effective_score = max(0.0, min(100.0, (observed_score if observed_score is not None else 50.0) + worker["manager_score_adjustment"]))
        access_observed = bool(adapter_ready.get("access_observed"))
        rows.append({
            **worker,
            "effective_adapter_id": effective_adapter,
            "adapter_fallback_used": effective_adapter != worker["adapter_id"],
            "available": bool(worker["enabled"] and adapter_ready.get("launchable")),
            "availability_observed": access_observed or bool(sample_count > 0),
            "readiness_status": str(adapter_ready.get("status") or "unobserved"),
            "quota_observed": False,
            "quota_state": "unavailable_from_provider_api",
            "outcomes": {
                "sample_count": sample_count,
                "attempt_count": attempts,
                "retry_count": retries,
                "accepted_rate": accepted_rate,
                "review_ready_rate": review_rate,
                "validation_failure_rate": failure_rate,
                "retry_rate": retry_rate,
                "p50_latency_seconds": median(latencies) if latencies else None,
                "p95_latency_seconds": _percentile(latencies, 0.95),
                "total_tokens": tokens,
                "cost_usd": round(cost, 6) if cost else None,
                "tokens_with_unknown_cost": unknown_cost_tokens,
                "cost_usd_per_1k_tokens": round(cost * 1000.0 / tokens, 6) if cost and tokens else None,
                "evidence_source": "observed" if sample_count else "conservative_prior",
            },
            "observed_score": observed_score,
            "effective_score": round(effective_score, 2),
        })
    unattributed = [
        process for index, process in enumerate(processes)
        if index not in attributed_process_ids
    ]
    missing_model = sum(1 for process in unattributed if not str(process.get("model") or ""))
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "revision": catalog["revision"],
        "configured": bool(catalog.get("configured")),
        "workers": rows,
        "summary": {
            "workers": len(rows),
            "enabled": sum(1 for item in rows if item["enabled"]),
            "available": sum(1 for item in rows if item["available"]),
            "observed": sum(1 for item in rows if item["outcomes"]["sample_count"]),
            "unattributed_process_rows": len(unattributed),
            "unattributed_missing_model_rows": missing_model,
            "unattributed_unknown_adapter_or_model_rows": len(unattributed) - missing_model,
            "process_identity_recovered_rows": identity_recovered,
        },
        "truth_contract": {
            "provider_quota_fabricated": False,
            "missing_outcomes_use_labeled_prior": True,
            "manager_adjustment_range": [-20.0, 20.0],
        },
    }


def rank_task(repo_root: Path | str, task: workforce_router.TaskRequirements, *, catalog: Mapping[str, Any] | None = None) -> dict[str, Any]:
    snapshot = dict(catalog or build_catalog(repo_root))
    workers: list[workforce_router.WorkerCapability] = []
    for item in snapshot.get("workers") or []:
        outcomes = item.get("outcomes") if isinstance(item, Mapping) else {}
        if not isinstance(outcomes, Mapping):
            outcomes = {}
        workers.append(workforce_router.WorkerCapability.build(
            worker_id=item["worker_id"], adapter_id=item.get("effective_adapter_id") or item["adapter_id"], model=item["model"], provider=item["provider"],
            supports=item["supports"], tools=item["tools"], max_context_tokens=item["max_context_tokens"],
            max_risk=item["max_risk"], quality_ceiling=item["quality_ceiling"],
            available=bool(item["available"]), credential_ok=bool(item["available"]), quota_available=None,
            manager_score_adjustment=float(item.get("manager_score_adjustment") or 0.0),
            evidence=workforce_router.OutcomeEvidence(
                accepted_rate=outcomes.get("accepted_rate"), review_ready_rate=outcomes.get("review_ready_rate"),
                validation_failure_rate=outcomes.get("validation_failure_rate"), p50_latency_seconds=outcomes.get("p50_latency_seconds"),
                p95_latency_seconds=outcomes.get("p95_latency_seconds"), cost_usd_per_1k_tokens=outcomes.get("cost_usd_per_1k_tokens"),
                sample_count=int(outcomes.get("sample_count") or 0),
            ),
        ))
    return workforce_router.rank_workforce(task, workers).as_dict()


__all__ = ["AUDIT_RELATIVE_PATH", "CATALOG_RELATIVE_PATH", "DEFAULT_WORKERS", "SCHEMA_ID", "WorkforceCatalogError", "build_catalog", "catalog_path", "ensure_catalog", "load_catalog", "rank_task", "upsert_worker", "validate_catalog"]
