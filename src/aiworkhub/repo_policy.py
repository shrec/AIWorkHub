"""Repository-local launch policy and unified environment preflight.

The policy is data, never executable configuration: command rules are fixed
tokens, validations are named checks, and retention values are bounded
integers.  No shell fragment, Python expression, host path, or credential is
accepted or returned.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from . import (
    claude_auth,
    codex_auth,
    kilo_auth,
    model_settings,
    provider_route_contracts,
    quality_evidence,
    runtime_adapters,
    source_graph_daemon,
    task_store,
    worker_workspace,
    workspace_hygiene,
)

try:
    from . import deepseek_credentials, glm_credentials, vscode_lm_bridge
except ImportError:  # packaged/minimal runtime: report unavailable, never guess
    deepseek_credentials = None  # type: ignore[assignment]
    glm_credentials = None  # type: ignore[assignment]
    vscode_lm_bridge = None  # type: ignore[assignment]


SCHEMA_ID = "aiworkhub.repo_policy.v1"
PREFLIGHT_SCHEMA_ID = "aiworkhub.environment_preflight.v1"
POLICY_RELATIVE_PATH = Path(".aiworkhub/config/policy.json")
MAX_POLICY_BYTES = 64 * 1024
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
MANDATORY_RAW_DISCOVERY_DENIES = ("grep", "rg", "find", "tree")

# Workforce readiness vocabulary. A route must never be labeled "ready" when
# the provider quota that gates it has not actually been observed; the
# distinct value below lets a caller reading readiness alone route honestly.
READINESS_READY = "ready"
READINESS_READY_UNVERIFIED = "ready_unverified"
QUOTA_STATE_UNAVAILABLE = "unavailable_from_provider_api"

# ── Which question a route-status surface answered ─────────────────────────
# Two AIWorkHub surfaces publish a verdict about the same route and they do
# NOT answer the same question.  ``build_preflight`` measures whether the
# route can be STARTED here: a binary resolves, a credential file exists, an
# editor host answered, consent was granted.  ``workforce_catalog`` measures
# whether a round trip on that route has been OBSERVED to complete inside a
# bounded window.  Neither fact implies the other -- a startable route may
# never have completed anything, and a route that completed work yesterday
# may be unstartable today -- so a verdict shown without its question cannot
# be acted on, and two verdicts shown without their questions read to an
# operator as a contradiction (NF-2026-00669: `launchable=true,
# ready_unverified` beside `available=false, route_unobserved`).  Each
# surface now names the question it answered, so both facts can be true at
# once without either being wrong.
ROUTE_QUESTION_STARTABLE = "route_startable_here"
ROUTE_QUESTION_ROUND_TRIP_OBSERVED = "route_round_trip_observed_in_window"
# The third question, and the only other one selection is allowed to conjoin:
# has this route been measured FAILING recently enough to still be held open?
# It is answered from observed failures with a threshold and a cooldown, so a
# route nobody has run yet passes it -- unlike the round-trip question, which
# a never-run route can never pass and which therefore must never gate.
ROUTE_QUESTION_FAILURE_CIRCUIT_CLOSED = "route_failure_circuit_closed"
# The fourth question, named because it was previously published only as the
# bare boolean `availability_observed`, which reads as an answer to the
# round-trip question above and is not one: it is true when an adapter access
# probe succeeded OR when the route has quality cards on record at any time,
# and it is not windowed.
ROUTE_QUESTION_ACCESS_PROBE_OBSERVED = "route_access_probe_observed"

# Exact reasons for a round-trip verdict.  "Never observed" and "observed,
# but not inside the window" are different facts calling for different
# operator actions: the first says nobody has ever run this route, the second
# says the route has a decided history and has merely gone quiet.  Reporting
# the second as the first is precisely the defect these tokens exist to
# prevent -- measured evidence must never be published as unmeasured.
ROUTE_OBSERVATION_NEVER_RECORDED = "no_terminal_execution_ever_recorded"
ROUTE_OBSERVATION_OUTSIDE_WINDOW = "no_terminal_execution_inside_observation_window"
ROUTE_OBSERVATION_IN_WINDOW = "terminal_execution_observed_inside_observation_window"
ROUTE_OBSERVATION_CIRCUIT_OPEN = "route_failure_circuit_open"

_FINALIZATION_PREFLIGHT_WARMUP_SECONDS = 1.0

_POLICY_ALLOWED_ADAPTERS: tuple[str, ...] = (
    *runtime_adapters.LOCAL_ADAPTERS,
    runtime_adapters.OPENCODE_CLI_ADAPTER,
)
DEFAULT_POLICY: dict[str, Any] = {
    "schema_id": SCHEMA_ID,
    "providers": {"allowed_adapters": list(_POLICY_ALLOWED_ADAPTERS)},
    "tools": {
        "source_graph_required_for_code": True,
        "session_memory_kb_required_for_nontrivial": True,
        "raw_discovery_forbidden": list(MANDATORY_RAW_DISCOVERY_DENIES),
    },
    "validation": {"required_check_ids": []},
    "retention": {
        "logs_days": 7,
        "terminal_runs_days": 30,
        "archived_tasks_days": 90,
        "source_graph_generations": 3,
        "worktree_max_bytes": 5 * 1024 * 1024 * 1024,
    },
}
_PRE_GROK_LOCAL_ADAPTERS = frozenset(
    set(runtime_adapters.LOCAL_ADAPTERS) - {runtime_adapters.GROK_KILO_ADAPTER}
)
_PRE_OPENCODE_POLICY_ADAPTERS = frozenset(runtime_adapters.LOCAL_ADAPTERS)


class RepoPolicyError(RuntimeError):
    """A malformed or unsafe repository policy."""


def policy_path(repo_root: Path | str) -> Path:
    return Path(repo_root).resolve() / POLICY_RELATIVE_PATH


def _string_list(value: Any, field: str, *, maximum: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise RepoPolicyError(f"{field}_must_be_bounded_string_list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _TOKEN_RE.fullmatch(item):
            raise RepoPolicyError(f"{field}_contains_invalid_token")
        if item not in result:
            result.append(item)
    return result


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RepoPolicyError(f"{field}_out_of_range")
    return value


def validate_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RepoPolicyError("policy_must_be_object")
    if value.get("schema_id") != SCHEMA_ID:
        raise RepoPolicyError("policy_schema_id_invalid")
    providers = value.get("providers")
    tools = value.get("tools")
    validation = value.get("validation")
    retention = value.get("retention")
    if not all(isinstance(item, Mapping) for item in (providers, tools, validation, retention)):
        raise RepoPolicyError("policy_sections_invalid")

    allowed = _string_list(providers.get("allowed_adapters"), "allowed_adapters")
    unsupported = sorted(set(allowed) - set(_POLICY_ALLOWED_ADAPTERS))
    if unsupported or not allowed:
        raise RepoPolicyError("allowed_adapters_unsupported_or_empty")
    raw_denies = _string_list(tools.get("raw_discovery_forbidden"), "raw_discovery_forbidden")
    if not set(MANDATORY_RAW_DISCOVERY_DENIES).issubset(raw_denies):
        raise RepoPolicyError("mandatory_raw_discovery_denies_missing")
    for field in ("source_graph_required_for_code", "session_memory_kb_required_for_nontrivial"):
        if not isinstance(tools.get(field), bool):
            raise RepoPolicyError(f"{field}_must_be_bool")
    required_checks = _string_list(validation.get("required_check_ids"), "required_check_ids")
    return {
        "schema_id": SCHEMA_ID,
        "providers": {"allowed_adapters": allowed},
        "tools": {
            "source_graph_required_for_code": tools["source_graph_required_for_code"],
            "session_memory_kb_required_for_nontrivial": tools[
                "session_memory_kb_required_for_nontrivial"
            ],
            "raw_discovery_forbidden": raw_denies,
        },
        "validation": {"required_check_ids": required_checks},
        "retention": {
            "logs_days": _bounded_int(retention.get("logs_days"), "logs_days", 1, 7),
            "terminal_runs_days": _bounded_int(
                retention.get("terminal_runs_days"), "terminal_runs_days", 1, 365
            ),
            "archived_tasks_days": _bounded_int(
                retention.get(
                    "archived_tasks_days",
                    DEFAULT_POLICY["retention"]["archived_tasks_days"],
                ),
                "archived_tasks_days",
                7,
                3650,
            ),
            "source_graph_generations": _bounded_int(
                retention.get("source_graph_generations"),
                "source_graph_generations",
                1,
                20,
            ),
            "worktree_max_bytes": _bounded_int(
                retention.get(
                    "worktree_max_bytes",
                    DEFAULT_POLICY["retention"]["worktree_max_bytes"],
                ),
                "worktree_max_bytes",
                64 * 1024 * 1024,
                1024 * 1024 * 1024 * 1024,
            ),
        },
    }


def load_policy(repo_root: Path | str) -> dict[str, Any]:
    path = policy_path(repo_root)
    if not path.exists():
        return {**validate_policy(deepcopy(DEFAULT_POLICY)), "configured": False}
    try:
        info = path.lstat()
    except OSError as exc:
        raise RepoPolicyError(f"policy_unreadable:{type(exc).__name__}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RepoPolicyError("policy_must_be_regular_file")
    if info.st_size > MAX_POLICY_BYTES:
        raise RepoPolicyError("policy_too_large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepoPolicyError(f"policy_invalid_json:{type(exc).__name__}") from exc
    policy = validate_policy(value)
    allowed = list(policy["providers"]["allowed_adapters"])
    if (
        runtime_adapters.GROK_KILO_ADAPTER not in allowed
        and _PRE_GROK_LOCAL_ADAPTERS.issubset(allowed)
    ):
        # Backward-compatible built-in migration: repositories that allowed
        # every pre-Grok route receive the new route.  A repository that
        # deliberately omitted any legacy route remains untouched/fail-closed.
        policy["providers"]["allowed_adapters"] = [
            name for name in _POLICY_ALLOWED_ADAPTERS
            if name in allowed or name == runtime_adapters.GROK_KILO_ADAPTER
        ]
        allowed = list(policy["providers"]["allowed_adapters"])
    if (
        runtime_adapters.OPENCODE_CLI_ADAPTER not in allowed
        and _PRE_OPENCODE_POLICY_ADAPTERS.issubset(allowed)
    ):
        policy["providers"]["allowed_adapters"] = [
            name for name in _POLICY_ALLOWED_ADAPTERS
            if name in allowed or name == runtime_adapters.OPENCODE_CLI_ADAPTER
        ]
    return {**policy, "configured": True}


def ensure_policy(repo_root: Path | str) -> tuple[Path, bool]:
    """Create the immutable-safe default policy once; never overwrite edits."""
    root = Path(repo_root).resolve()
    if not (root / ".aiworkhub/project.json").is_file():
        raise RepoPolicyError("repository_not_initialized")
    path = policy_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        load_policy(root)
        return path, False
    payload = (json.dumps(DEFAULT_POLICY, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        load_policy(root)
        return path, False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    load_policy(root)
    return path, True


def _declared_check_ids(repo_root: Path) -> tuple[list[str], str]:
    try:
        config = quality_evidence.load_repo_config(repo_root)
    except quality_evidence.MalformedConfigError as exc:
        return [], str(exc)[:300]
    return [str(item.get("id")) for item in config.get("checks") or []], ""


def validate_launch(repo_root: Path | str, card: Mapping[str, Any], adapter_id: str) -> dict[str, Any]:
    """Apply policy before claim/start; returns a bounded, deterministic verdict."""
    root = Path(repo_root).resolve()
    try:
        policy = load_policy(root)
    except RepoPolicyError as exc:
        return {"ok": False, "reason": f"repo_policy_invalid:{exc}"}
    if adapter_id not in policy["providers"]["allowed_adapters"]:
        return {"ok": False, "reason": f"adapter_denied_by_repo_policy:{adapter_id}"}
    if card.get("callback_required") and (
        not card.get("callback_supported") or not str(card.get("origin_thread_id") or "").strip()
    ):
        return {"ok": False, "reason": "repo_policy_callback_route_required"}

    initialized = (root / ".aiworkhub/project.json").is_file()
    context = card.get("project_context")
    task_type = str(context.get("task_type") or "code") if isinstance(context, Mapping) else "code"
    is_code = task_type == "code" and bool(card.get("allowed_writes"))
    policy_applies_to_context = bool(policy["configured"]) or card.get("schema_id") == "aiworkhub.task_card.v1"
    if (
        initialized
        and policy_applies_to_context
        and is_code
        and policy["tools"]["source_graph_required_for_code"]
    ):
        source = context.get("source_graph") if isinstance(context, Mapping) else None
        if not isinstance(source, Mapping) or source.get("required") is not True:
            return {"ok": False, "reason": "repo_policy_source_graph_required_for_code"}

    declared, config_error = _declared_check_ids(root)
    if config_error:
        return {"ok": False, "reason": "repo_policy_quality_config_invalid"}
    missing = sorted(set(policy["validation"]["required_check_ids"]) - set(declared))
    if missing:
        return {"ok": False, "reason": "repo_policy_required_checks_missing:" + ",".join(missing)}
    return {"ok": True, "reason": "ready", "configured": bool(policy["configured"])}


_VSCODE_LM_IN_PROCESS_ADAPTERS = frozenset(
    {
        runtime_adapters.VSCODE_LM_ADAPTER,
        runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER,
        runtime_adapters.GLM_VSCODE_LM_ADAPTER,
    }
)


def _is_windows_host() -> bool:
    return os.name == "nt"


# ── Why native CLI execution is refused on a Windows host ──────────────────
# ``worker_workspace.select_sandbox_backend`` already MEASURES which of three
# things refused this host -- the AppContainer APIs did not resolve, the
# execution path is not wired to them, or the host is not Windows at all --
# and encodes that cause after the colon of its bounded error.  The route row
# used to drop the measurement and publish only the stable legacy blocker
# code, so an operator could not tell an unwired build from a host that
# genuinely cannot confine anything (NF-2026-00876).  Both facts travel now.
WINDOWS_APPCONTAINER_BACKEND = worker_workspace.WINDOWS_APPCONTAINER_BACKEND
WINDOWS_APPCONTAINER_SELECTION_PREFIX = "windows_appcontainer_sandbox_unavailable"
SANDBOX_CAUSE_HOST_APPCONTAINER_UNAVAILABLE = "win32_appcontainer_unavailable"
SANDBOX_CAUSE_EXECUTION_PATH_NOT_WIRED = "execution_path_not_wired"
SANDBOX_CAUSE_PLATFORM_NOT_WINDOWS = "platform_not_windows"
WINDOWS_APPCONTAINER_SELECTION_CAUSES = frozenset(
    {
        SANDBOX_CAUSE_HOST_APPCONTAINER_UNAVAILABLE,
        SANDBOX_CAUSE_EXECUTION_PATH_NOT_WIRED,
        SANDBOX_CAUSE_PLATFORM_NOT_WINDOWS,
    }
)
# A backend was selected and it is simply not the one native CLI execution
# requires -- a different fact from selection having failed outright.
SANDBOX_CAUSE_BACKEND_NOT_APPCONTAINER = "sandbox_backend_not_appcontainer"
# Selection refused without naming a cause, or named one this build does not
# recognise.  Both are stated, never guessed at or invented.  There is no
# shape-based admission: a token is recognised because it is a member of
# WINDOWS_APPCONTAINER_SELECTION_CAUSES above, never because it looks like one.
SANDBOX_CAUSE_UNREPORTED = "selection_cause_not_reported"
SANDBOX_CAUSE_UNRECOGNIZED = "selection_cause_unrecognized"
# Every family ``select_sandbox_backend`` refuses with.  Only the family token
# is publishable: it raises ``invalid_sandbox_backend:<env value>``,
# ``bubblewrap_unusable:<host path>`` and
# ``secure_sandbox_unavailable:bubblewrap_unusable:<probe detail>`` beside the
# Windows family, so the text after the family is host detail in three of four
# cases and the Windows cause is admitted only by membership above.
SANDBOX_FAMILY_INVALID_BACKEND = "invalid_sandbox_backend"
SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE = "bubblewrap_unusable"
SANDBOX_FAMILY_SECURE_SANDBOX_UNAVAILABLE = "secure_sandbox_unavailable"
SANDBOX_SELECTION_FAMILIES = frozenset(
    {
        WINDOWS_APPCONTAINER_SELECTION_PREFIX,
        SANDBOX_FAMILY_INVALID_BACKEND,
        SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE,
        SANDBOX_FAMILY_SECURE_SANDBOX_UNAVAILABLE,
    }
)
# ``secure_sandbox_unavailable`` is the one family whose text after the family
# token is itself a CLOSED vocabulary: the Linux probe names the exact boundary
# it could not establish and nothing host-specific.  Collapsing it to the bare
# family threw away the only fact an operator can act on -- a kernel without
# Landlock is a different repair from a host without seccomp -- so both tokens
# are admitted by membership, exactly as the Windows causes are.
SANDBOX_CAUSE_LANDLOCK_UNSUPPORTED = "landlock_unsupported"
SANDBOX_CAUSE_SECCOMP_UNAVAILABLE = "seccomp_unavailable"
SECURE_SANDBOX_PROBE_CAUSES = frozenset(
    {SANDBOX_CAUSE_LANDLOCK_UNSUPPORTED, SANDBOX_CAUSE_SECCOMP_UNAVAILABLE}
)
# The one route status that means "an enforceable sandbox could not be
# selected here".  It is spelled once so the rows that set it and the surfaces
# that recognise a sandbox blocker cannot drift apart.
SANDBOX_STATUS_UNAVAILABLE = "sandbox_unavailable"
# The stable compatibility blocker code for a non-Windows route whose selection
# refused.  Windows native CLI keeps its own long-standing code, so a caller
# matching on ``sandbox_blocker_code`` finds a non-empty token on either host
# instead of a blocker that exists in ``status`` and nowhere else.
SANDBOX_BLOCKER_ENFORCEABLE_SANDBOX_UNAVAILABLE = "enforceable_sandbox_unavailable"
# What the sandbox block says when a boundary WAS selected and the report is
# still not enforceable because the ROUTE is blocked -- an absent credential, a
# policy denial, no launchable route at all.  ``enforceable: false`` beside an
# empty reason stated a verdict and withheld its subject, and the credential
# text that would explain it must never be restated in sandbox vocabulary, so
# the block names the shape of the blocker and leaves the blocker itself on the
# route rows that own it.
SANDBOX_REASON_ROUTE_BLOCKED_OUTSIDE_SANDBOX = "route_blocked_outside_sandbox"
# The complete set of strings any surface may publish as a sandbox-selection
# reason or cause.  The publication guard gates on membership in this set and
# on nothing else, so a value is refused for not being here, never for looking
# wrong.
SANDBOX_PUBLISHABLE_SELECTION_REASONS = frozenset(
    {
        SANDBOX_CAUSE_UNREPORTED,
        SANDBOX_CAUSE_UNRECOGNIZED,
        SANDBOX_CAUSE_BACKEND_NOT_APPCONTAINER,
        SANDBOX_BLOCKER_ENFORCEABLE_SANDBOX_UNAVAILABLE,
        SANDBOX_REASON_ROUTE_BLOCKED_OUTSIDE_SANDBOX,
        runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER,
        *SANDBOX_SELECTION_FAMILIES,
        *WINDOWS_APPCONTAINER_SELECTION_CAUSES,
        *SECURE_SANDBOX_PROBE_CAUSES,
        *(
            f"{WINDOWS_APPCONTAINER_SELECTION_PREFIX}:{cause}"
            for cause in WINDOWS_APPCONTAINER_SELECTION_CAUSES
        ),
        *(
            f"{SANDBOX_FAMILY_SECURE_SANDBOX_UNAVAILABLE}"
            f":{SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE}:{cause}"
            for cause in SECURE_SANDBOX_PROBE_CAUSES
        ),
    }
)
# The backends the sandbox block may name.  ``select_sandbox_backend`` returns
# exactly one of the three real boundaries; the other two are this module's own
# published identifiers for routes that execute inside the editor host and for
# a block describing several launchable routes at once.  Closing the vocabulary
# is what makes the publication guard a membership test rather than a shape
# test -- an environment value carries no separator to be caught by.
SANDBOX_BACKEND_BUBBLEWRAP = "bubblewrap"
SANDBOX_BACKEND_LANDLOCK = "landlock"
SANDBOX_BACKEND_VSCODE_LM_IN_PROCESS = worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND
SANDBOX_BACKEND_ROUTE_SPECIFIC = "route_specific"
SANDBOX_PUBLISHABLE_BACKENDS = frozenset(
    {
        SANDBOX_BACKEND_BUBBLEWRAP,
        SANDBOX_BACKEND_LANDLOCK,
        SANDBOX_BACKEND_VSCODE_LM_IN_PROCESS,
        SANDBOX_BACKEND_ROUTE_SPECIFIC,
        WINDOWS_APPCONTAINER_BACKEND,
    }
)
# Keys whose value is a backend or an adapter IDENTIFIER rather than selection
# text.  They are named one by one, and each is gated on its own closed
# vocabulary, because the guard's default is refusal: every other string field
# is treated as selection-derived and must be a declared publishable reason, so
# a sandbox field added later fails closed even when its value carries no
# separator to notice it by.
_SANDBOX_BACKEND_FIELDS = frozenset(
    {"backend", "selected_backend", "native_cli_backend"}
)
_SANDBOX_ADAPTER_FIELDS = frozenset({"selected_adapter"})


def _bounded_selection_family(family: str, remainder: str) -> tuple[str, str]:
    """Reduce one selection family and its trailing text to publishable values.

    Two of the families ``select_sandbox_backend`` raises put an environment
    value or a host path after the family token, so for those the family IS the
    whole publishable fact.  ``secure_sandbox_unavailable`` is the exception:
    its trailing token comes from a CLOSED probe vocabulary, and a kernel
    without Landlock is a different repair from a host without seccomp.  The
    admitted detail is REBUILT from this module's own constants rather than
    sliced out of the error, so no selection byte reaches a surface even when
    the token matches.  A family this build does not name carries nothing over.
    """

    if family not in SANDBOX_SELECTION_FAMILIES:
        return SANDBOX_CAUSE_UNRECOGNIZED, SANDBOX_CAUSE_UNRECOGNIZED
    if family == SANDBOX_FAMILY_SECURE_SANDBOX_UNAVAILABLE:
        nested, _, probe = remainder.partition(":")
        probe = probe.strip()
        if nested.strip() == SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE and (
            probe in SECURE_SANDBOX_PROBE_CAUSES
        ):
            return probe, (
                f"{SANDBOX_FAMILY_SECURE_SANDBOX_UNAVAILABLE}"
                f":{SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE}:{probe}"
            )
    return family, family


def _windows_native_sandbox_cause(
    sandbox_backend: str, sandbox_error: str
) -> tuple[str, str]:
    """Name the measured cause native CLI execution is refused here.

    Returns ``(cause, detail)``, both empty when the AppContainer backend was
    actually selected.  Within the Windows family the vocabulary is closed by
    membership and nothing else: a cause this build does not already name is
    ``SANDBOX_CAUSE_UNRECOGNIZED`` however well shaped it looks, and it carries
    NO detail, because there is no measurement to describe.

    Selection can refuse a Windows host with a family that is not the Windows
    one -- reading the environment raises ``invalid_sandbox_backend:<env
    value>`` on every platform.  Collapsing those to
    ``selection_cause_unrecognized`` discarded a family this build DOES name
    and left a Windows host less informative than a POSIX host about the
    identical refusal, so the known family token is kept and only the host text
    after it is dropped -- through the same reduction the POSIX path uses, so
    the two can never disagree about the same error.
    """

    backend = str(sandbox_backend or "").strip()
    if backend == WINDOWS_APPCONTAINER_BACKEND:
        return "", ""
    text = str(sandbox_error or "").strip()
    if not text:
        return (
            SANDBOX_CAUSE_BACKEND_NOT_APPCONTAINER
            if backend
            else SANDBOX_CAUSE_UNREPORTED
        ), ""
    family, _, remainder = text.partition(":")
    family = family.strip()
    if family != WINDOWS_APPCONTAINER_SELECTION_PREFIX:
        return _bounded_selection_family(family, remainder)
    cause = remainder.strip()
    if not cause:
        return SANDBOX_CAUSE_UNREPORTED, ""
    if cause not in WINDOWS_APPCONTAINER_SELECTION_CAUSES:
        return SANDBOX_CAUSE_UNRECOGNIZED, ""
    return cause, f"{family}:{cause}"


def _bounded_sandbox_selection(
    sandbox_backend: str, sandbox_error: str
) -> tuple[str, str]:
    """Reduce a sandbox-selection refusal to the ``(cause, detail)`` published.

    The per-route cause was vouched first, but the global block kept handing the
    dashboard the raw error, so ``bubblewrap_unusable:/usr/bin/bwrap`` escaped
    anyway (NF-2026-00876).  Every family reduces through
    ``_bounded_selection_family`` except the Windows one, which keeps its own
    stricter membership rule on the cause it names.
    """

    if str(sandbox_backend or "").strip():
        return "", ""
    text = str(sandbox_error or "").strip()
    if not text:
        return SANDBOX_CAUSE_UNREPORTED, SANDBOX_CAUSE_UNREPORTED
    family, _, remainder = text.partition(":")
    family = family.strip()
    if family == WINDOWS_APPCONTAINER_SELECTION_PREFIX:
        cause, detail = _windows_native_sandbox_cause("", text)
        return cause, detail or cause
    return _bounded_selection_family(family, remainder)


def _native_cli_sandbox_diagnosis(
    sandbox_backend: str, sandbox_error: str
) -> tuple[str, str]:
    """Derive ONCE the ``(cause, detail)`` every native CLI surface publishes.

    The route row, the global sandbox block and the unavailable-route summary
    all answer the same question -- why native CLI execution is refused on this
    host -- so they must not each pick a derivation.  Recomputing only the
    cause with the Windows rule while the detail kept the general one published
    ``selection_cause_unrecognized`` beside ``invalid_sandbox_backend`` on the
    same host, with the route row carrying neither (NF-2026-00876 rework).

    Windows keeps its own stricter rule on purpose: its causes are admitted by
    membership in ``WINDOWS_APPCONTAINER_SELECTION_CAUSES`` and a cause this
    build cannot name carries NO detail at all, rather than borrowing a family
    token from a derivation that was never applied to it.  Every other host
    uses the general family reduction.  Both paths return only vouched values,
    so whichever applies, the surfaces agree by construction.
    """

    if _is_windows_host():
        return _windows_native_sandbox_cause(sandbox_backend, sandbox_error)
    return _bounded_sandbox_selection(sandbox_backend, sandbox_error)


def _native_cli_backend_enforceable(sandbox_backend: str) -> bool:
    """Can native CLI execution actually run under the backend that was selected?

    Selection succeeding is not the same fact as the selected boundary being
    usable for native CLI here.  Reporting enforceability from ``bool(backend)``
    alone told a Windows host with some other backend that its native CLI
    sandbox was enforceable while every native row on that same report was
    refused as ``platform_excluded`` -- the global/row contradiction this task
    exists to close.  Fail closed: on Windows only the AppContainer backend
    qualifies.
    """

    backend = str(sandbox_backend or "").strip()
    if not backend:
        return False
    if _is_windows_host():
        return backend == WINDOWS_APPCONTAINER_BACKEND
    return True


def _sandbox_block_reason(
    selected: Mapping[str, Any] | None,
    route_enforceable: bool,
    native_cli_reason: str,
) -> str:
    """Say WHY the sandbox block is not enforceable, in sandbox vocabulary only.

    Three facts can make the block unenforceable and they are not the same
    fact: the selected route carries a sandbox blocker, selection itself
    refused, or a boundary was selected and the route is blocked for a reason
    that has nothing to do with sandboxing.  The third produced ``enforceable:
    false`` beside an EMPTY reason -- a verdict with its subject withheld --
    because the native CLI derivation correctly has nothing to say once a
    backend was selected.

    The answer is a declared token, never the route's own text: a credential or
    consent blocker restated here would land on a surface whose whole
    vocabulary is sandbox selection, where the publication guard can only refuse
    it as an unrecognised cause.  The blocker itself stays on the route row that
    owns it, and this field points at that row's existence.
    """

    if route_enforceable:
        return ""
    row = selected or {}
    if row.get("status") == SANDBOX_STATUS_UNAVAILABLE:
        reason = str(row.get("reason") or "")[:200]
        if reason:
            return reason
    return native_cli_reason or SANDBOX_REASON_ROUTE_BLOCKED_OUTSIDE_SANDBOX


def _vouched_sandbox_block(block: dict[str, Any]) -> dict[str, Any]:
    """Enforce at the publication boundary what each derivation promised.

    Derivation by derivation is how the first repair passed its own tests and
    still leaked: one projection of ``sandbox_error`` was missed.  This is the
    single place every sandbox field the report publishes is checked, so a new
    field wired to raw selection text is scrubbed rather than shipped.

    The default is REFUSAL, and every key is decided by MEMBERSHIP in a closed
    vocabulary rather than by the shape of its value.  A backend field must
    name a backend this build can select, an adapter field must name an adapter
    this policy allows, and every other string must be a declared publishable
    reason.  Filtering on the path separator alone was not enough twice over: a
    leaked environment value reaches this boundary as
    ``invalid_sandbox_backend:s3cret-env-value`` with no separator to notice it
    by, and it would have passed an identifier field unchanged.  It scrubs
    instead of raising because a preflight that cannot render is a worse
    failure than one that reports an unrecognised cause.

    A refusal must leave the field inside ITS OWN vocabulary.  Writing the
    reason token ``selection_cause_unrecognized`` into a backend or adapter
    field replaced one unreadable value with another that no reader of those
    fields can resolve -- it names no backend and no adapter -- so an
    identifier this build cannot vouch for is emptied instead, which is the one
    value those fields already publish for "none".  Reason fields keep the
    reason vocabulary, where that token is a declared member.
    """

    for key, value in block.items():
        if not isinstance(value, str) or not value:
            continue
        if key in _SANDBOX_BACKEND_FIELDS:
            if value not in SANDBOX_PUBLISHABLE_BACKENDS:
                block[key] = ""
            continue
        if key in _SANDBOX_ADAPTER_FIELDS:
            if value not in _POLICY_ALLOWED_ADAPTERS:
                block[key] = ""
            continue
        if value not in SANDBOX_PUBLISHABLE_SELECTION_REASONS:
            block[key] = SANDBOX_CAUSE_UNRECOGNIZED
    return block


def _list_opencode_models(executable: str | None) -> list[str]:
    """Bound ``opencode models`` discovery; listing is not round-trip evidence."""

    if not isinstance(executable, str) or not executable.strip():
        return []
    from . import workforce_catalog

    try:
        completed = subprocess.run(
            [executable, "models"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    payload = completed.stdout or b""
    if not payload:
        payload = completed.stderr or b""
    if len(payload) > 64 * 1024:
        return []
    return workforce_catalog.parse_opencode_models_output(payload)


def _provider_status(
    repo_root: Path,
    adapter_id: str,
    policy: Mapping[str, Any],
    sandbox_backend: str,
    sandbox_error: str,
    model_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolution = runtime_adapters.resolve_executable(adapter_id)
    policy_allowed = adapter_id in policy["providers"]["allowed_adapters"]
    if model_policy is None:
        model_policy = model_settings.load(repo_root)
    policy_provider, policy_adapter = model_settings.policy_identity_for_adapter(
        adapter_id
    )
    repository_model_policy_enabled = model_settings.evaluate_state(
        model_policy,
        provider=policy_provider,
        adapter=policy_adapter,
    )
    native_windows_cli_without_broker = (
        _is_windows_host()
        and adapter_id not in _VSCODE_LM_IN_PROCESS_ADAPTERS
        and sandbox_backend != WINDOWS_APPCONTAINER_BACKEND
    )
    result: dict[str, Any] = {
        "adapter_id": adapter_id,
        # Name the question this row answers.  Every field below is a fact
        # about the route being STARTABLE from this host -- a resolved
        # binary, a present credential, an answering editor host, a granted
        # consent.  None of them is a fact about a round trip completing, so
        # a reader must never take `launchable` for "this route works"; the
        # workforce catalog answers that separate question and says so.
        "route_question": ROUTE_QUESTION_STARTABLE,
        "policy_allowed": policy_allowed,
        # Coverage describes routes this host and repository can actually
        # support. Windows native CLI routes remain fail-closed until an
        # AppContainer-grade broker exists, but they must not make healthy
        # editor-hosted routes look degraded merely by being in the portable
        # adapter catalog. Explicitly policy-denied routes are likewise not a
        # required coverage target.
        "coverage_required": bool(
            policy_allowed
            and repository_model_policy_enabled
            and not native_windows_cli_without_broker
        ),
        "platform_excluded": bool(native_windows_cli_without_broker),
        "model_policy_excluded": not repository_model_policy_enabled,
        "repository_model_policy_enabled": repository_model_policy_enabled,
        "policy_provider": policy_provider,
        "policy_adapter": policy_adapter,
        "installed": bool(resolution.ok),
        "launchable": bool(resolution.ok),
        "access_observed": False,
        # No credential/auth helper can observe a provider's metered quota
        # today, so the honest default is unobserved until a helper reports it.
        "quota_observed": False,
        "quota_state": QUOTA_STATE_UNAVAILABLE,
        "status": "installed_unverified_access" if resolution.ok else "not_installed",
        "reason": str(resolution.reason or "")[:200],
        # A sandbox blocker is two separate facts and the row states both, or
        # states neither.  The code is the stable token downstream callers
        # match on; the cause and its bounded detail are what was actually
        # measured about this host.  Empty means no sandbox blocker applies.
        "sandbox_blocker_code": "",
        "sandbox_unavailable_cause": "",
        "sandbox_unavailable_detail": "",
    }
    readiness: Mapping[str, Any] | None = None
    try:
        if adapter_id == runtime_adapters.DEEPSEEK_COPILOT_ADAPTER and deepseek_credentials:
            readiness = deepseek_credentials.credential_status(repo=repo_root)
        elif adapter_id == runtime_adapters.GLM_COPILOT_ADAPTER and glm_credentials:
            readiness = glm_credentials.credential_status(repo=repo_root)
        elif adapter_id == runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER and vscode_lm_bridge:
            readiness = vscode_lm_bridge.bridge_readiness(
                repo_root,
                model=runtime_adapters.DEEPSEEK_DEFAULT_MODEL,
                adapter_id=adapter_id,
            )
        elif adapter_id == runtime_adapters.GLM_VSCODE_LM_ADAPTER and vscode_lm_bridge:
            readiness = vscode_lm_bridge.bridge_readiness(
                repo_root,
                model=runtime_adapters.GLM_DEFAULT_MODEL,
                adapter_id=adapter_id,
            )
        elif adapter_id == runtime_adapters.VSCODE_LM_ADAPTER and vscode_lm_bridge:
            readiness = vscode_lm_bridge.bridge_readiness(
                repo_root,
                model=None,
                adapter_id=adapter_id,
            )
        elif adapter_id == "claude_cli":
            readiness = claude_auth.auth_status(resolution.executable)
        elif adapter_id == "codex_cli":
            readiness = codex_auth.capability_status(resolution.executable)
        elif adapter_id == runtime_adapters.GROK_KILO_ADAPTER:
            readiness = kilo_auth.auth_status(
                home=Path.home(),
                xdg_data_home=os.environ.get("XDG_DATA_HOME") or None,
                platform_name=os.name,
            )
    except (OSError, RuntimeError, ValueError):
        readiness = None
    if isinstance(readiness, Mapping):
        if adapter_id in _VSCODE_LM_IN_PROCESS_ADAPTERS:
            # Editor visibility/host presence is not consent or a completed
            # provider turn; the bridge must report its explicit observation.
            result["access_observed"] = bool(readiness.get("access_observed"))
        else:
            # Native credential/auth helpers have already validated their
            # exact local credential/subscription boundary before declaring
            # launchable. They do not use the editor consent state machine.
            result["access_observed"] = bool(
                readiness.get("access_observed")
                or readiness.get("authenticated")
                or readiness.get("credential_present")
                or readiness.get("launchable")
            )
        result["launchable"] = bool(resolution.ok and readiness.get("launchable"))
        # Quota is a separate axis from credential/access observation. A route
        # that is launchable and whose access is observed may still be
        # unverifiable because the provider's quota was never observed. Such a
        # route must not be labeled "ready"; it is "ready_unverified", and the
        # quota reason travels alongside the readiness value.
        quota_observed = bool(readiness.get("quota_observed"))
        quota_state = str(readiness.get("quota_state") or QUOTA_STATE_UNAVAILABLE)[:128]
        result["quota_observed"] = quota_observed
        result["quota_state"] = quota_state
        if result["launchable"] and result["access_observed"]:
            result["status"] = (
                READINESS_READY if quota_observed else READINESS_READY_UNVERIFIED
            )
        elif result["launchable"] and readiness.get("consent_required"):
            result["status"] = "consent_required"
        else:
            result["status"] = "access_unavailable"
        result["reason"] = str(readiness.get("blocker_reason") or readiness.get("reason") or "")[:200]
        if result["status"] == READINESS_READY_UNVERIFIED:
            result["reason"] = f"quota_unobserved:{quota_state}"
        # Preserve only bounded, secret-free broker observability.  The UI can
        # then report real editor model capacity instead of presenting
        # redundant transports as if they were distinct models.
        for key in (
            "window_id",
            "host_count",
            "live_host_count",
            "stale_host_count",
            "freshest_age_seconds",
            "access_state",
            "consent_required",
            "cache_hit",
            "cache_ttl_seconds",
            "model_catalog_complete",
        ):
            value = readiness.get(key)
            if value is not None:
                result[key] = value
        observed_models = readiness.get("observed_models")
        if isinstance(observed_models, list):
            provider_observed_models = [
                str(value)[:128]
                for value in observed_models[:128]
                if isinstance(value, str) and value
            ]
            eligible_observed_models = [
                value
                for value in provider_observed_models
                if model_settings.evaluate_state(
                    model_policy,
                    provider=policy_provider,
                    adapter=policy_adapter,
                    model=value,
                )
            ]
            result["provider_observed_models"] = provider_observed_models
            result["observed_models"] = eligible_observed_models
            excluded_count = len(provider_observed_models) - len(eligible_observed_models)
            if excluded_count:
                result[
                    "observed_models_excluded_by_repository_model_policy"
                ] = excluded_count
            if provider_observed_models and not eligible_observed_models:
                result["provider_launchable"] = result["launchable"]
                result["launchable"] = False
                result["status"] = "repository_model_policy_disabled"
                result["reason"] = "all_observed_models_disabled_by_repository_model_settings"
    if (
        adapter_id == runtime_adapters.OPENCODE_CLI_ADAPTER
        and result["installed"]
        and "observed_models" not in result
    ):
        listed = _list_opencode_models(resolution.executable)
        provider_observed_models = listed[:128]
        eligible_observed_models = [
            value
            for value in provider_observed_models
            if model_settings.evaluate_state(
                model_policy,
                provider=policy_provider,
                adapter=policy_adapter,
                model=value,
            )
        ]
        result["provider_observed_models"] = provider_observed_models
        result["observed_models"] = eligible_observed_models
        excluded_count = len(provider_observed_models) - len(eligible_observed_models)
        if excluded_count:
            result["observed_models_excluded_by_repository_model_policy"] = excluded_count
        if provider_observed_models and not eligible_observed_models:
            result["provider_launchable"] = result["launchable"]
            result["launchable"] = False
            result["status"] = "repository_model_policy_disabled"
            result["reason"] = "all_observed_models_disabled_by_repository_model_settings"
    if adapter_id in _VSCODE_LM_IN_PROCESS_ADAPTERS:
        result["sandbox_backend"] = SANDBOX_BACKEND_VSCODE_LM_IN_PROCESS
    else:
        result["sandbox_backend"] = sandbox_backend
        if native_windows_cli_without_broker:
            # Fail closed, and say exactly why.  ``reason`` keeps the stable
            # compatibility blocker code every existing caller and receipt
            # matches on, and the code is mirrored into its own field so a
            # reader never has to parse prose to find it.  The measured
            # selection/probe cause travels beside both, so a host whose
            # AppContainer APIs never resolved is no longer indistinguishable
            # from a build whose execution path was never wired.  ``installed``
            # is deliberately left alone: a missing binary stays its own fact.
            cause, cause_detail = _native_cli_sandbox_diagnosis(
                sandbox_backend, sandbox_error
            )
            result["launchable"] = False
            result["status"] = SANDBOX_STATUS_UNAVAILABLE
            result["reason"] = runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER
            result["sandbox_blocker_code"] = (
                runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER
            )
            result["sandbox_unavailable_cause"] = cause
            result["sandbox_unavailable_detail"] = cause_detail
            result["sandbox_backend"] = ""
        elif not sandbox_backend:
            # Every other host owes the reader the same three facts.  Setting
            # only ``reason`` here left a Linux route whose status said
            # ``sandbox_unavailable`` beside an empty code and an empty cause,
            # so the row and its summary projection disagreed about whether a
            # sandbox blocker existed at all.  The code is stable and the cause
            # is what was measured, exactly as on the Windows branch -- through
            # the same one derivation, so neither branch can drift from what
            # the global block publishes.
            cause, cause_detail = _native_cli_sandbox_diagnosis(
                sandbox_backend, sandbox_error
            )
            result["launchable"] = False
            result["status"] = SANDBOX_STATUS_UNAVAILABLE
            # The route row feeds the global block's ``reason`` when this
            # adapter is the selected one, so it is bounded here too rather
            # than echoing the selection exception.
            result["reason"] = cause_detail
            result["sandbox_blocker_code"] = (
                SANDBOX_BLOCKER_ENFORCEABLE_SANDBOX_UNAVAILABLE
            )
            result["sandbox_unavailable_cause"] = cause
            result["sandbox_unavailable_detail"] = cause_detail
    if not result["policy_allowed"]:
        result["launchable"] = False
        result["status"] = "policy_denied"
        result["reason"] = "adapter_denied_by_repo_policy"
    elif not repository_model_policy_enabled:
        result["launchable"] = False
        result["status"] = "repository_model_policy_disabled"
        result["reason"] = "route_disabled_by_repository_model_settings"
    return result


def route_observation_verdict(
    *,
    observed_in_window: bool,
    prior_observation_count: int,
    observation_window_seconds: float,
    circuit_open_failure_kind: str = "",
    evidence_sources: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Answer ``ROUTE_QUESTION_ROUND_TRIP_OBSERVED`` for exactly one route.

    Every surface that reports whether a route has been seen to work calls
    this, so the question has one predicate, one answer shape and one set of
    reasons.  A second surface computing "is it available" its own way is how
    the control plane came to contradict itself in the first place.

    ``prior_observation_count`` is how many terminal executions and decided
    tasks this exact route has on record AT ANY TIME, not only inside the
    window.  It is the number that separates a route nobody has ever run from
    one that has run and gone quiet.  It must therefore be derived from a
    ledger that RETAINS that history: the caller's decided/archived task cards
    and usage records, never the short-lived process log the window question
    reads.  A count taken from the process log answers a different question
    and silently republishes "aged out of a 7-day log" as "never happened".

    The three-valued state and the evidence classes are deliberately the
    repository's existing capability vocabulary from
    ``provider_route_contracts``: "measured and negative" versus "never
    measured" is exactly the distinction that module was added to carry, and
    a second spelling of it here would be the ``single_definition`` violation
    it exists to prevent.

    Fail-closed.  ``supported`` is returned ONLY for a round trip observed
    inside the window.  A route with a long history but nothing recent is
    ``unknown`` -- not ``supported`` (nothing recent proves it still works)
    and not ``unsupported`` either (nothing measured it failing).  Its
    evidence class is still ``observed_round_trip`` and the count travels with
    it, so a reader can tell "this route has never run" from "this route has
    53 decided tasks and has gone quiet" without going looking for the number.
    """

    window = max(0.0, float(observation_window_seconds))
    observed_before = max(0, int(prior_observation_count))
    if circuit_open_failure_kind:
        # Measured, and negative: an authenticated failure tripped the
        # circuit.  This is the one genuinely ``unsupported`` case.
        state = provider_route_contracts.CAPABILITY_UNSUPPORTED
        evidence_class = provider_route_contracts.EVIDENCE_OBSERVED_ROUND_TRIP
        reason = f"{ROUTE_OBSERVATION_CIRCUIT_OPEN}:{str(circuit_open_failure_kind)[:64]}"
    elif observed_in_window:
        state = provider_route_contracts.CAPABILITY_SUPPORTED
        evidence_class = provider_route_contracts.EVIDENCE_OBSERVED_ROUND_TRIP
        reason = ROUTE_OBSERVATION_IN_WINDOW
    elif observed_before:
        # The route HAS been observed; the observation is simply older than
        # the window.  Calling this "unobserved" publishes measured evidence
        # as unmeasured, which is the inverse of the repository doctrine and
        # the exact bug the owner reported twice.
        state = provider_route_contracts.CAPABILITY_UNKNOWN
        evidence_class = provider_route_contracts.EVIDENCE_OBSERVED_ROUND_TRIP
        reason = ROUTE_OBSERVATION_OUTSIDE_WINDOW
    else:
        state = provider_route_contracts.CAPABILITY_UNKNOWN
        evidence_class = provider_route_contracts.EVIDENCE_UNVERIFIED
        reason = ROUTE_OBSERVATION_NEVER_RECORDED
    return {
        "question": ROUTE_QUESTION_ROUND_TRIP_OBSERVED,
        "state": state,
        "evidence_class": evidence_class,
        "reason": reason,
        "observation_window_seconds": window,
        "prior_observation_count": observed_before,
        # Which retained ledger each prior observation came from.  The count
        # alone cannot be audited: a reader who sees 205 has no way to check
        # whether the join reached the 90-day card store or merely counted the
        # same short-lived process rows twice.  Publishing the per-source
        # breakdown is what makes the number falsifiable.
        "prior_observation_sources": {
            str(name): max(0, int(value))
            for name, value in (evidence_sources or {}).items()
        },
    }


def build_preflight(repo_root: Path | str, adapter_id: str | None = None) -> dict[str, Any]:
    """Return one portable readiness report across repository, tools and providers."""
    root = Path(repo_root).resolve()
    readiness = task_store.storage_readiness(root)
    errors: list[str] = []
    try:
        policy = load_policy(root)
        policy_error = ""
    except RepoPolicyError as exc:
        policy = {**validate_policy(deepcopy(DEFAULT_POLICY)), "configured": False}
        policy_error = str(exc)[:300]
        errors.append("repo_policy_invalid")
    try:
        repository_model_policy = model_settings.load(root)
        model_policy_error = ""
    except model_settings.ModelSettingsError as exc:
        repository_model_policy = {
            "providers": {}, "adapters": {}, "models": {}
        }
        model_policy_error = str(exc)[:300]
        errors.append("repository_model_settings_invalid")
    declared_checks, quality_error = _declared_check_ids(root)
    missing_checks = sorted(
        set(policy["validation"]["required_check_ids"]) - set(declared_checks)
    )
    if not readiness.ready:
        errors.append("repository_not_ready")
    if quality_error:
        errors.append("quality_config_invalid")
    if missing_checks:
        errors.append("required_validation_missing")
    source_health = source_graph_daemon.daemon_health(root)
    source_age = source_health.get("index_age_seconds")
    source_stale_after = source_health.get("stale_after_seconds")
    source_generation_fresh = not (
        source_age is not None
        and source_stale_after is not None
        and float(source_age) > float(source_stale_after)
    )
    source_refresh_job = source_health.get("refresh_job")
    source_refresh_failed = bool(
        isinstance(source_refresh_job, dict)
        and source_refresh_job.get("state") == "failed"
    )
    source_graph_refreshable = bool(
        source_health.get("running")
        and source_health.get("ok")
        and source_health.get("status")
        not in {
            source_graph_daemon.STATUS_STOPPED,
            source_graph_daemon.STATUS_DEGRADED,
            source_graph_daemon.STATUS_STALE,
            source_graph_daemon.STATUS_RECOVERY,
        }
        and source_health.get("refreshable", True) is not False
        and source_health.get("last_error") != "build_start_fenced"
        and not source_refresh_failed
    )
    source_graph_ready_for_code = (
        bool(source_health.get("readable_generation"))
        and bool(source_health.get("last_success_at"))
        and bool(source_health.get("build_revision"))
        and int(source_health.get("files_seen") or 0) > 0
        and source_generation_fresh
        and source_graph_refreshable
    )
    if policy["tools"]["source_graph_required_for_code"] and not source_graph_ready_for_code:
        errors.append("source_graph_not_ready")
    try:
        sandbox_backend = worker_workspace.select_sandbox_backend()
        sandbox_error = ""
    except worker_workspace.WorkspaceError as exc:
        sandbox_backend = ""
        sandbox_error = str(exc)[:200]
    # Measure the refusal once and hand the same answer to both surfaces: the
    # provider rows below and this report's sandbox block. Two independent
    # derivations of "why is native CLI refused here" is exactly how a global
    # blocker and a per-route blocker came to disagree (NF-2026-00876).  The
    # first repair still ran two: it took the cause from the Windows derivation
    # and left the reason on the general one, so an `invalid_sandbox_backend`
    # refusal on Windows published cause `selection_cause_unrecognized` beside
    # reason `invalid_sandbox_backend` while the route row carried no detail at
    # all.  Cause AND detail now come from the one derivation the rows use.
    native_cli_sandbox_cause, native_cli_sandbox_reason = _native_cli_sandbox_diagnosis(
        sandbox_backend, sandbox_error
    )
    providers = [
        _provider_status(
            root,
            name,
            policy,
            sandbox_backend,
            sandbox_error,
            model_policy=repository_model_policy,
        )
        for name in _POLICY_ALLOWED_ADAPTERS
    ]
    selected = next((item for item in providers if item["adapter_id"] == adapter_id), None)
    if adapter_id and selected is None:
        errors.append("selected_adapter_unsupported")
    elif selected is not None and not selected["launchable"]:
        errors.append("selected_adapter_not_launchable")
    eligible_routes = [item for item in providers if item.get("coverage_required", True)]
    excluded_routes = [item for item in providers if not item.get("coverage_required", True)]
    launchable_routes = [item for item in eligible_routes if item.get("launchable")]
    unavailable_routes = [item for item in eligible_routes if not item.get("launchable")]
    if not launchable_routes:
        errors.append("no_launchable_provider_routes")
    route_coverage_status = (
        "blocked"
        if not launchable_routes
        else ("degraded" if unavailable_routes else "full")
    )
    finalization_probe: dict[str, Any] = {
        "ok": True,
        "status": "not_required",
        "reason": "non_windows_host",
    }
    finalization_pending = False
    if _is_windows_host() and launchable_routes:
        probe_adapter = str(
            (selected or launchable_routes[0]).get("adapter_id") or "vscode_lm"
        )
        try:
            finalization_probe = worker_workspace.finalization_preflight_probe_nonblocking(
                root, probe_adapter
            )
            # A cold probe normally completes in a few hundred milliseconds.
            # Give that coalesced background probe one bounded warm-up window
            # so callers do not observe a transient hard Blocked result.
            deadline = time.monotonic() + _FINALIZATION_PREFLIGHT_WARMUP_SECONDS
            while (
                str(finalization_probe.get("status") or "") == "probing"
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
                finalization_probe = (
                    worker_workspace.finalization_preflight_probe_nonblocking(
                        root, probe_adapter
                    )
                )
        except (OSError, RuntimeError, ValueError, worker_workspace.WorkspaceError) as exc:
            finalization_probe = {
                "ok": False,
                "status": "blocked",
                "reason": f"preflight_finalization_probe_failed:{exc}"[:500],
                "phase": "preflight_finalization",
            }
        finalization_pending = (
            str(finalization_probe.get("status") or "") == "probing"
        )
        if not finalization_probe.get("ok") and not finalization_pending:
            errors.append("worker_finalization_not_ready")
    # A green aggregate over a dead reconciler is precisely the failure this
    # report exists to prevent.  The reconciler is the ONLY thing that
    # finalizes an exited worker, so "ready" while its authority is failing
    # means cards sit in `processing` forever while every surface says fine.
    # Imported inside the function on purpose: `repo_policy` is imported BY
    # `process_launcher`, which `task_reconciler` imports, so a module-level
    # import here is a cycle.
    from . import task_reconciler

    try:
        reconciler_report = task_reconciler.reconciler_health(root)
    except (OSError, RuntimeError, ValueError) as exc:
        reconciler_report = {
            "ok": False,
            "authority_state": "unknown",
            "last_error": f"reconciler_health_unavailable:{type(exc).__name__}",
        }
    reconciler_authority_state = str(reconciler_report.get("authority_state") or "")
    # Unmeasured is not failing.  A repository with no reconciler registered in
    # THIS process and no durable record has produced no evidence either way,
    # and the report already distinguishes that case elsewhere (`not_required`,
    # `ready_unverified`) rather than inventing a verdict from silence.
    reconciler_measured = bool(
        reconciler_report.get("running")
        or reconciler_report.get("durable_status_present")
    )
    reconciler_status = {
        "ok": bool(reconciler_report.get("ok")),
        "status": (
            "blocked"
            if reconciler_authority_state == "acquisition_failed"
            else (
                "not_measured"
                if not reconciler_measured
                else ("ready" if reconciler_report.get("ok") else "degraded")
            )
        ),
        "authority_state": reconciler_authority_state,
        "active_owner": bool(reconciler_report.get("active_owner")),
        "acquisition_attempts": int(reconciler_report.get("acquisition_attempts") or 0),
        "acquisition_backoff_seconds": float(
            reconciler_report.get("acquisition_backoff_seconds") or 0.0
        ),
        "last_acquisition_error": str(
            reconciler_report.get("last_acquisition_error") or ""
        )[:300],
        # Carried verbatim: a host that could only grant a weaker lock must
        # show that here, not have it flattened into a boolean.
        "parent_authority_backend": str(
            reconciler_report.get("parent_authority_backend") or ""
        ),
        "reduced_guarantees": list(reconciler_report.get("reduced_guarantees") or []),
        "durable_status_present": bool(reconciler_report.get("durable_status_present")),
        "durable_scan_stale": bool(reconciler_report.get("durable_scan_stale")),
        "last_error": str(reconciler_report.get("last_error") or "")[:300],
    }
    if reconciler_status["status"] == "blocked":
        errors.append("worker_reconciler_authority_failed")
    selected_route_backend = str((selected or {}).get("sandbox_backend") or "")
    route_enforceable = bool(
        selected_route_backend and (selected or {}).get("launchable")
    ) if selected is not None else bool(
        any(item.get("sandbox_backend") for item in launchable_routes)
    )
    try:
        callback_health = task_store.callback_bridge_health(root) if readiness.ready else {}
    except (OSError, RuntimeError, ValueError, task_store.TaskStoreError):
        callback_health = {"ok": False, "reason": "callback_health_unavailable"}
    try:
        # Preflight is called on every dashboard refresh. Use persisted slot
        # accounting only; explicit hygiene preview performs the potentially
        # expensive recursive byte/rogue-tree scan on demand.
        hygiene = workspace_hygiene.inventory(root, refresh_sizes=False)
        hygiene_status = {
            "ok": True,
            "slot_count": int(hygiene.get("slot_count") or 0),
            "total_bytes": int(hygiene.get("total_bytes") or 0),
            "effective_bytes": int(hygiene.get("effective_bytes") or 0),
            "sizes_refreshed": False,
            "explicit_preview_required": True,
        }
    except (OSError, RuntimeError, ValueError, workspace_hygiene.WorkspaceHygieneError) as exc:
        hygiene_status = {
            "ok": False,
            "reason": f"workspace_hygiene_unavailable:{type(exc).__name__}",
        }
    unique_errors = list(dict.fromkeys(errors))
    # One list decides both the warning set and the aggregate label, so a
    # component can never be reported degraded in its own block while the
    # overall status still reads "ready".
    degraded_reasons: list[str] = []
    if route_coverage_status == "degraded":
        degraded_reasons.append("provider_route_coverage_degraded")
    if reconciler_status["status"] == "degraded":
        degraded_reasons.append("worker_reconciler_degraded")
    if reconciler_status["status"] == "not_measured":
        # Absence of evidence is not evidence of health. The reconciler is the
        # only thing that finalizes an exited worker, so a report that has
        # never seen it -- no service in this process, no durable record -- has
        # not established that finalization works, and must not print "ready"
        # as though it had.
        degraded_reasons.append("worker_reconciler_unmeasured")
    warnings = list(degraded_reasons)
    if finalization_pending:
        warnings.append("worker_finalization_probe_pending")
    result = {
        "ok": not unique_errors and not finalization_pending,
        "schema_id": PREFLIGHT_SCHEMA_ID,
        "status": (
            "blocked"
            if unique_errors
            else (
                "probing"
                if finalization_pending
                else ("degraded" if degraded_reasons else "ready")
            )
        ),
        "errors": unique_errors,
        "warnings": warnings,
        "repository": {
            "ready": bool(readiness.ready),
            "reason": str(readiness.reason)[:200],
            "repo_id": str(readiness.repo_id),
        },
        "reconciler": reconciler_status,
        "policy": {
            "valid": not policy_error,
            "configured": bool(policy.get("configured")),
            "error": policy_error,
            "model_settings_valid": not model_policy_error,
            "model_settings_error": model_policy_error,
            "providers": dict(policy["providers"]),
            "tools": dict(policy["tools"]),
            "validation": {
                **dict(policy["validation"]),
                "declared_check_ids": declared_checks[:100],
                "missing_check_ids": missing_checks[:100],
                "config_error": quality_error,
            },
            "retention": dict(policy["retention"]),
        },
        "source_graph": {
            **{
                key: source_health.get(key)
                for key in (
                    "ok", "status", "running", "registered", "last_success_at",
                    "stale_reason", "build_revision", "files_seen",
                    "readable_generation", "last_error", "writer_state",
                    "refreshable", "refresh_job",
                )
            },
            "ready_for_code": source_graph_ready_for_code,
            "refreshable_for_code": source_graph_refreshable,
        },
        "sandbox": _vouched_sandbox_block(
            {
                # Primary fields describe the selected route (or the set of
                # launchable routes when no adapter is selected), not only
                # native CLI sandbox availability. This prevents a ready VS
                # Code LM in-process route from being displayed beside a
                # contradictory global ``sandbox unenforceable`` warning.
                "backend": (
                    selected_route_backend
                    if selected is not None
                    else (
                        sandbox_backend
                        or (
                            SANDBOX_BACKEND_ROUTE_SPECIFIC
                            if route_enforceable
                            else ""
                        )
                    )
                ),
                "enforceable": route_enforceable,
                # Only a SANDBOX blocker belongs in the sandbox block's reason,
                # and a "not enforceable" verdict always owes the reader one.
                "reason": _sandbox_block_reason(
                    selected, route_enforceable, native_cli_sandbox_reason
                ),
                "selected_adapter": str((selected or {}).get("adapter_id") or ""),
                "selected_backend": selected_route_backend,
                "native_cli_backend": sandbox_backend,
                # A backend having been SELECTED is not the same fact as one
                # native CLI execution may actually run under.  On Windows only
                # AppContainer qualifies, so reporting "enforceable" for any
                # non-empty token made this field contradict the very native
                # rows the same host refuses.
                "native_cli_enforceable": _native_cli_backend_enforceable(
                    sandbox_backend
                ),
                # Never the raw selection error: three of its four families
                # encode an environment value, a host path or a nested probe
                # detail after the family token (NF-2026-00876).
                "native_cli_reason": native_cli_sandbox_reason,
                # The same measured cause the native CLI rows carry, so the
                # global block and the per-route block never name it differently.
                "native_cli_cause": native_cli_sandbox_cause,
                "route_aware": True,
            }
        ),
        "callback": {
            key: callback_health.get(key)
            for key in (
                "ok",
                "backlog_count",
                "retry_count",
                "last_delivered_at",
                "last_dead_letter_at",
                "last_dead_letter_error",
                "reason",
            )
            if key in callback_health
        },
        "workspace_hygiene": hygiene_status,
        "worker_finalization": finalization_probe,
        "providers": providers,
        # Per-route observability so this surface reports, route by route, which
        # routes can never have quota verified here and the named reason why --
        # rather than leaving the universal ready_unverified an unexplained
        # blanket (NF-2026-00270).
        "provider_observability": provider_observability_report(root),
        # The capability registry itself, so a caller can see the whole
        # contract set -- transports, protocols, model families, documentation
        # provenance and what is still unknown -- and not merely the verdicts
        # it produced for this host's routes (RM-2026-00033 phase 1).
        "provider_route_contracts": provider_route_contracts.registry_report(),
        "provider_summary": {
            "route_count": len(providers),
            "eligible_route_count": len(eligible_routes),
            "launchable_route_count": len(launchable_routes),
            "unavailable_route_count": len(unavailable_routes),
            "excluded_route_count": len(excluded_routes),
            "coverage_status": route_coverage_status,
            "coverage_ratio": (
                round(len(launchable_routes) / len(eligible_routes), 6)
                if eligible_routes
                else 0.0
            ),
            "unavailable_routes": [
                {
                    "adapter_id": str(item.get("adapter_id") or "")[:128],
                    "status": str(item.get("status") or "unavailable")[:128],
                    "reason": str(item.get("reason") or "unavailable")[:200],
                    "sandbox_backend": str(item.get("sandbox_backend") or "")[:128],
                    # The same three facts the row states.  A projection that
                    # carried only ``reason`` left a reader unable to tell a
                    # sandbox blocker from a credential one, and left the
                    # non-Windows sandbox blocker with no code at all
                    # (NF-2026-00876).  Empty still means no sandbox blocker.
                    "blocker_code": str(item.get("sandbox_blocker_code") or "")[:128],
                    "cause": str(item.get("sandbox_unavailable_cause") or "")[:128],
                    "detail": str(item.get("sandbox_unavailable_detail") or "")[:200],
                }
                for item in unavailable_routes
            ],
            "excluded_routes": [
                {
                    "adapter_id": str(item.get("adapter_id") or "")[:128],
                    "status": str(item.get("status") or "excluded")[:128],
                    "reason": str(item.get("reason") or "excluded")[:200],
                    # The projection carries the same two facts the row does.
                    # A summary that kept only the legacy blocker text made a
                    # Windows host that cannot build an AppContainer look
                    # identical to a build whose execution path was never
                    # wired, so the two surfaces disagreed about the same
                    # route (NF-2026-00876).
                    "blocker_code": str(item.get("sandbox_blocker_code") or "")[:128],
                    "cause": str(item.get("sandbox_unavailable_cause") or "")[:128],
                    "detail": str(item.get("sandbox_unavailable_detail") or "")[:200],
                    "exclusion": (
                        "platform"
                        if item.get("platform_excluded")
                        else (
                            "repository_model_policy"
                            if item.get("model_policy_excluded")
                            else "policy"
                        )
                    ),
                }
                for item in excluded_routes
            ],
            # Launchable says a route can be STARTED, never that it can FINISH
            # a given job. For every capability in the registry vocabulary the
            # two maps below split the launchable routes into those that can
            # complete it and those that cannot -- each exclusion carrying the
            # exact state and reason. A route whose transport cannot carry a
            # reviewer submit is therefore visible here, instead of being
            # discovered after a card has been spent on it (NF-2026-00669).
            "capability_launchable_routes": {
                capability: [
                    str(item.get("adapter_id") or "")
                    for item in launchable_routes
                    if provider_route_contracts.adapter_can_complete(
                        str(item.get("adapter_id") or ""), capability
                    )
                ]
                for capability in provider_route_contracts.CAPABILITY_VOCABULARY
            },
            "capability_exclusions": {
                capability: provider_route_contracts.capability_exclusions(
                    [str(item.get("adapter_id") or "") for item in launchable_routes],
                    capability,
                )
                for capability in provider_route_contracts.CAPABILITY_VOCABULARY
            },
            # The control plane answers TWO questions about every route and
            # they must never be read as one.  Naming both here, on the
            # surface that answers only the first, is what stops a reader
            # taking `launchable`/`ready_unverified` for availability and
            # then seeing the workforce catalog's answer to the OTHER
            # question as a contradiction.
            "route_status_questions": {
                "answered_here": {
                    "question": ROUTE_QUESTION_STARTABLE,
                    "fields": ["launchable", "status", "access_observed"],
                    "asserts": (
                        "the route can be started from this host: launch "
                        "target resolved, credential or editor host present, "
                        "consent granted and repository policy allows it"
                    ),
                    "does_not_assert": (
                        "that any unit of work has ever completed on this route"
                    ),
                },
                "answered_by_workforce_catalog": {
                    "question": ROUTE_QUESTION_ROUND_TRIP_OBSERVED,
                    "fields": ["available", "route_observation", "route_health"],
                    "asserts": (
                        "a terminal execution on this exact route completed "
                        "inside the catalog's bounded observation window"
                    ),
                    "does_not_assert": (
                        "that the route can be started right now"
                    ),
                },
            },
        },
        "selected_adapter": selected,
    }
    # NF-2026-00... (OpenCode model discovery never reached settings):
    # ``workforce_catalog.cached_preflight_snapshot`` / ``_settings_preflight_
    # snapshot`` exist specifically to reuse THIS build so a settings read
    # never spawns a second ``opencode models`` probe -- but nothing ever
    # called the write side, ``remember_preflight_snapshot``, from production
    # code. Measured: ``resolve_executable`` and ``_list_opencode_models``
    # both discovered every OpenCode model correctly when called directly, and
    # a fresh ``build_preflight`` call here also carries them in
    # ``result["providers"]`` -- but ``aiworkhub_dashboard_settings`` kept
    # reporting zero OpenCode models regardless, because the snapshot it reads
    # was never written. This is the one place ``build_preflight`` always
    # returns from, so remembering it here reaches every caller, including the
    # ``environment_preflight`` MCP tool.
    from . import workforce_catalog

    workforce_catalog.remember_preflight_snapshot(root, result)
    return result


# ---------------------------------------------------------------------------
# Worker concurrency capacity.
#
# ``AIWORKHUB_MAX_PROCESSES`` is the authoritative cap on how many workers may
# run at once.  Capping by runner identity instead means a configured cap is
# not the cap that applies, and two cards that share a runner serialise even
# when the machine has spare capacity.  ``resolve_workforce_cap`` reports the
# effective cap from the configured value; ``workforce_admission`` admits a
# launch by total running count and never by runner serialisation.
# ---------------------------------------------------------------------------
WORKFORCE_SCHEMA_ID = "aiworkhub.workforce_capacity.v1"
MAX_PROCESSES_ENV = "AIWORKHUB_MAX_PROCESSES"
DEFAULT_MAX_PROCESSES = 4
MAX_PROCESSES_CEILING = 256


def resolve_workforce_cap(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Resolve the authoritative worker concurrency cap.

    ``AIWORKHUB_MAX_PROCESSES`` decides the cap; the returned ``effective_cap``
    is what actually applies.  A missing or malformed value falls back to
    ``DEFAULT_MAX_PROCESSES`` with a named reason, a below-minimum value is
    clamped up to 1, and an above-ceiling value is clamped down to
    ``MAX_PROCESSES_CEILING`` -- each stated in ``reason`` rather than silently.
    """

    source = os.environ if env is None else env
    raw = source.get(MAX_PROCESSES_ENV)
    if raw is None or str(raw).strip() == "":
        return {
            "schema_id": WORKFORCE_SCHEMA_ID,
            "effective_cap": DEFAULT_MAX_PROCESSES,
            "configured": False,
            "source": "default",
            "raw_value": None,
            "reason": "max_processes_unset_default_applied",
        }
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return {
            "schema_id": WORKFORCE_SCHEMA_ID,
            "effective_cap": DEFAULT_MAX_PROCESSES,
            "configured": False,
            "source": "default_invalid",
            "raw_value": str(raw)[:64],
            "reason": f"max_processes_invalid_default_applied:{str(raw)[:64]}",
        }
    if value < 1:
        return {
            "schema_id": WORKFORCE_SCHEMA_ID,
            "effective_cap": 1,
            "configured": True,
            "source": "env",
            "raw_value": str(raw)[:64],
            "reason": f"max_processes_below_minimum_clamped:{value}->1",
        }
    clamped = min(value, MAX_PROCESSES_CEILING)
    reason = (
        "max_processes_configured"
        if clamped == value
        else f"max_processes_above_ceiling_clamped:{value}->{clamped}"
    )
    return {
        "schema_id": WORKFORCE_SCHEMA_ID,
        "effective_cap": clamped,
        "configured": True,
        "source": "env",
        "raw_value": str(raw)[:64],
        "reason": reason,
    }


def workforce_admission(
    *,
    running_total: int,
    runner: str = "",
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Admit a worker launch by configured capacity, never by runner identity.

    Two cards that share a runner are both admissible while the total running
    worker count is below the authoritative ``AIWORKHUB_MAX_PROCESSES`` cap: a
    launch is never rejected merely because another worker with the same runner
    is already running.  ``effective_cap`` is reported so a caller sees the real
    cap that applied.
    """

    resolved = resolve_workforce_cap(env)
    effective = int(resolved["effective_cap"])
    running = max(0, int(running_total))
    admit = running < effective
    return {
        "schema_id": WORKFORCE_SCHEMA_ID,
        "admit": bool(admit),
        "effective_cap": effective,
        "cap_source": resolved["source"],
        "cap_reason": resolved["reason"],
        "running_total": running,
        "runner": str(runner or ""),
        "serialised_by_runner": False,
        "reason": (
            "workforce_capacity_available"
            if admit
            else f"workforce_cap_reached:running={running}>=cap={effective}"
        ),
    }


# ---------------------------------------------------------------------------
# Provider observability.
#
# No configured adapter exposes a metered provider-side quota to this control
# plane, so a blanket ``ready_unknown`` told the manager nothing.  This report
# states, per configured adapter, what IS observable here (executable install,
# credential presence, editor host reachability/consent) and NAMES the reason
# quota is not observable for that adapter family rather than returning a single
# opaque unknown.  For ``glm_vscode_lm`` it states whether the adapter is
# reachable in THIS installation, with evidence, before any code claim.
# ---------------------------------------------------------------------------
PROVIDER_OBSERVABILITY_SCHEMA_ID = "aiworkhub.provider_observability.v1"
PROVIDER_ROUTE_CONTRACT_SCHEMA_ID = (
    provider_route_contracts.PROVIDER_ROUTE_CONTRACT_SCHEMA_ID
)

# One protocol-first classifier, shared with the capability registry. These
# aliases keep the local vocabulary while runtime_adapters owns the mapping,
# so a route family cannot mean one thing in preflight and another in the
# contract registry that decides what the family can complete.
_ROUTE_FAMILY_EDITOR = runtime_adapters.ROUTE_FAMILY_EDITOR_VSCODE_LM
_ROUTE_FAMILY_COPILOT = runtime_adapters.ROUTE_FAMILY_COPILOT_BYOK_CLI
_ROUTE_FAMILY_CLAUDE = runtime_adapters.ROUTE_FAMILY_CLAUDE_CLI
_ROUTE_FAMILY_CODEX = runtime_adapters.ROUTE_FAMILY_CODEX_CLI
_ROUTE_FAMILY_KILO = runtime_adapters.ROUTE_FAMILY_KILO_XAI_CLI
_ROUTE_FAMILY_OPENCODE = runtime_adapters.ROUTE_FAMILY_OPENCODE_CLI
_ROUTE_FAMILY_UNKNOWN = runtime_adapters.ROUTE_FAMILY_UNKNOWN

_QUOTA_UNOBSERVABLE_REASON_BY_FAMILY: Mapping[str, str] = {
    _ROUTE_FAMILY_EDITOR: (
        "editor_hosted_route_exposes_host_reachability_and_consent_not_metered_quota"
    ),
    _ROUTE_FAMILY_COPILOT: (
        "copilot_byok_exposes_credential_presence_not_provider_side_quota"
    ),
    _ROUTE_FAMILY_CLAUDE: (
        "claude_cli_exposes_subscription_auth_not_metered_token_quota"
    ),
    _ROUTE_FAMILY_CODEX: "codex_cli_exposes_no_quota_endpoint_to_this_host",
    _ROUTE_FAMILY_KILO: (
        "kilo_xai_subscription_exposes_local_auth_not_provider_side_quota"
    ),
    _ROUTE_FAMILY_OPENCODE: (
        "opencode_cli_listing_is_not_quota_or_round_trip_evidence"
    ),
    _ROUTE_FAMILY_UNKNOWN: "adapter_family_unknown_quota_observability_undetermined",
}


def _adapter_route_family(adapter_id: str) -> str:
    """Delegate to the single protocol-first classifier.

    Route family decides which capability contract a route inherits, so
    preflight and ``provider_route_contracts`` must never be able to
    disagree about what family an adapter belongs to.
    """

    return runtime_adapters.route_family(adapter_id)


def describe_provider_observability(
    repo_root: Path | str, adapter_id: str
) -> dict[str, Any]:
    """Report what is observable for one adapter here, and quota's named reason.

    Every probe is bounded and guarded: a route is described whether or not it
    is serviceable in this environment, and ``reachability_evidence`` always
    states the ground truth (module present/absent, executable resolved,
    probe outcome) instead of a bare unknown.
    """

    root = Path(repo_root).resolve()
    resolution = runtime_adapters.resolve_executable(adapter_id)
    family = _adapter_route_family(adapter_id)
    installed = bool(resolution.ok)
    # The evidence names WHETHER the binary resolved, never WHERE.  An absolute
    # host path is machine-specific, and tests/test_repo_policy.py asserts the
    # preflight report is portable -- it bans both host paths AND the word
    # "executable", so the vocabulary here says "binary" and carries only the
    # basename.  The guard is deliberately stronger than "no host paths":
    # keeping it that way is cheaper than re-deciding it every time a new
    # field is added to this report.
    install_evidence = (
        f"binary_resolved:{Path(str(resolution.executable)).name}"
        if installed
        else f"not_installed:{resolution.reason}"
    )[:200]
    # Installability is observable for every adapter, installed or not; it is
    # the one signal that always names a real observation here.
    observable_signals: list[str] = ["binary_installability"]
    if installed:
        observable_signals.append("binary_installed")
    access_observed = False
    reachable = installed
    reachability_evidence = install_evidence

    if family == _ROUTE_FAMILY_EDITOR:
        if vscode_lm_bridge is None:
            reachable = False
            reachability_evidence = (
                "vscode_lm_bridge_module_unavailable_in_this_installation"
            )
        else:
            observable_signals.append("editor_host_bridge_module_present")
            if adapter_id == runtime_adapters.GLM_VSCODE_LM_ADAPTER:
                model: str | None = runtime_adapters.GLM_DEFAULT_MODEL
            elif adapter_id == runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER:
                model = runtime_adapters.DEEPSEEK_DEFAULT_MODEL
            else:
                model = None
            readiness: Mapping[str, Any] | None = None
            try:
                readiness = vscode_lm_bridge.bridge_readiness(
                    root, model=model, adapter_id=adapter_id
                )
            except Exception as exc:  # bounded probe: never fails the report
                readiness = None
                reachable = True  # module present + callable in this install
                reachability_evidence = (
                    "bridge_module_present_probe_unavailable_here:"
                    f"{type(exc).__name__}:{str(exc)[:140]}"
                )[:200]
                observable_signals.append("editor_host_bridge_callable")
            if isinstance(readiness, Mapping):
                reachable = True
                access_observed = bool(readiness.get("access_observed"))
                host_count = int(
                    readiness.get("host_count")
                    or readiness.get("live_host_count")
                    or 0
                )
                observable_signals.append("editor_host_reachability")
                if readiness.get("consent_required") is not None:
                    observable_signals.append("editor_access_consent_state")
                reachability_evidence = (
                    f"bridge_module_present:host_count={host_count}:"
                    f"launchable={bool(readiness.get('launchable'))}:"
                    f"access_observed={access_observed}"
                )[:200]
    elif family == _ROUTE_FAMILY_COPILOT:
        creds = (
            deepseek_credentials
            if adapter_id == runtime_adapters.DEEPSEEK_COPILOT_ADAPTER
            else glm_credentials
        )
        if creds is None:
            observable_signals.append("credential_module_unavailable")
            reachable = False
            reachability_evidence = (
                f"{install_evidence}:credential_module_unavailable"
            )[:200]
        else:
            observable_signals.append("credential_module_present")
            status: Mapping[str, Any] | None = None
            try:
                status = creds.credential_status(repo=root)
            except Exception as exc:  # bounded probe
                status = None
                reachability_evidence = (
                    f"{install_evidence}:credential_probe_error:{type(exc).__name__}"
                )[:200]
            if isinstance(status, Mapping):
                access_observed = bool(
                    status.get("credential_present")
                    or status.get("authenticated")
                    or status.get("launchable")
                )
                observable_signals.append("credential_presence")
                reachability_evidence = (
                    f"{install_evidence}:credential_present={access_observed}"
                )[:200]
    elif family == _ROUTE_FAMILY_CLAUDE and installed:
        status = None
        try:
            status = claude_auth.auth_status(resolution.executable)
        except Exception:  # bounded probe
            status = None
        if isinstance(status, Mapping):
            access_observed = bool(
                status.get("authenticated")
                or status.get("access_observed")
                or status.get("launchable")
            )
            observable_signals.append("subscription_auth_state")
    elif family == _ROUTE_FAMILY_KILO and installed:
        status = kilo_auth.auth_status(
            home=Path.home(),
            xdg_data_home=os.environ.get("XDG_DATA_HOME") or None,
            platform_name=os.name,
        )
        access_observed = bool(status.get("authenticated"))
        reachable = bool(status.get("launchable"))
        observable_signals.extend(
            ["xai_auth_record_presence", "subscription_auth_state"]
        )
        reachability_evidence = (
            f"{install_evidence}:xai_auth_present={access_observed}"
        )[:200]

    return {
        "schema_id": PROVIDER_OBSERVABILITY_SCHEMA_ID,
        "adapter_id": adapter_id,
        "route_family": family,
        "installed": installed,
        "install_evidence": install_evidence,
        "reachable": bool(reachable),
        "reachability_evidence": str(reachability_evidence)[:200],
        "access_observed": bool(access_observed),
        "quota_observable": False,
        "quota_observability_reason": _QUOTA_UNOBSERVABLE_REASON_BY_FAMILY[family],
        "observable_signals": observable_signals,
        # Every signal above is a fact about this route being PRESENT and
        # consented to here -- a binary resolving, a bridge module importing,
        # a host answering, consent granted. None of them is a fact about a
        # unit of work COMPLETING. The capability contract below states, per
        # named capability, what this route family can actually finish and
        # what class of evidence backs that, so no caller can read
        # reachability as a blanket yes (NF-2026-00669).
        "capabilities": provider_route_contracts.describe_adapter_capabilities(
            adapter_id
        )["capabilities"],
    }


def provider_observability_report(repo_root: Path | str) -> dict[str, Any]:
    """Per-adapter observability across every configured policy adapter.

    Each adapter names what is observable here and why its quota is not, so a
    caller reading this report can distinguish an available provider from an
    exhausted one where that is observable, and otherwise knows -- by name --
    exactly why it cannot.
    """

    root = Path(repo_root).resolve()
    adapters = [
        describe_provider_observability(root, name)
        for name in _POLICY_ALLOWED_ADAPTERS
    ]
    return {
        "schema_id": PROVIDER_OBSERVABILITY_SCHEMA_ID,
        "adapters": adapters,
        "quota_observable_any": any(item["quota_observable"] for item in adapters),
        "note": (
            "no configured adapter exposes a metered provider-side quota to this "
            "control plane; each adapter names what IS observable and the reason "
            "quota is not, rather than returning a single blanket unknown"
        ),
    }


__all__ = [
    "DEFAULT_POLICY",
    "POLICY_RELATIVE_PATH",
    "PREFLIGHT_SCHEMA_ID",
    "QUOTA_STATE_UNAVAILABLE",
    "READINESS_READY",
    "READINESS_READY_UNVERIFIED",
    "ROUTE_OBSERVATION_CIRCUIT_OPEN",
    "ROUTE_OBSERVATION_IN_WINDOW",
    "ROUTE_OBSERVATION_NEVER_RECORDED",
    "ROUTE_OBSERVATION_OUTSIDE_WINDOW",
    "ROUTE_QUESTION_ROUND_TRIP_OBSERVED",
    "ROUTE_QUESTION_STARTABLE",
    "RepoPolicyError",
    "SCHEMA_ID",
    "WORKFORCE_SCHEMA_ID",
    "MAX_PROCESSES_ENV",
    "DEFAULT_MAX_PROCESSES",
    "MAX_PROCESSES_CEILING",
    "PROVIDER_OBSERVABILITY_SCHEMA_ID",
    "PROVIDER_ROUTE_CONTRACT_SCHEMA_ID",
    "build_preflight",
    "describe_provider_observability",
    "ensure_policy",
    "load_policy",
    "policy_path",
    "provider_observability_report",
    "resolve_workforce_cap",
    "route_observation_verdict",
    "validate_policy",
    "workforce_admission",
]
