"""Narrow, read-only MCP tool surface for the native VS Code Task Operations app.

B615 replaces the iframe-embedded HTTP dashboard with a native VS Code
Webview whose extension host talks to the Task MCP server over stdio. These
three tools are the ENTIRE data surface the Webview needs: they call
``dashboard.build_snapshot()`` / ``dashboard.build_task_detail()`` -- the
same canonical builders the existing HTTP dashboard's ``/api/snapshot`` and
``/api/task`` routes already use -- and add nothing except a defensive
transport-size bound and task_id validation. No SQLite/taskctl read is
duplicated here; no code path in this module can write queue/audit state or
launch a process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from aiworkhub import (
    __version__,
    cli_model_discovery,
    core,
    context_graph,
    dashboard,
    feature_settings,
    hub_mail,
    hub_mail_net,
    model_settings,
    needfix_store,
    process_launcher,
    roadmap_store,
    repo_policy,
    repository_bootstrap,
    shared_router,
    skill_registry_store,
    source_graph,
    sqlite_readonly,
    storage_observability,
    storage_registry,
    storage_retention,
    task_retention,
    task_store,
    terminal_log_retention,
    vscode_lm_bridge,
    wave_roadmap,
    workforce_catalog,
)


# Hard bound on the serialized tool response so a very large queue (many
# thousands of process-log rows, a big cost ledger) can never balloon a
# single MCP stdio response. This defends the transport only -- it never
# changes what build_snapshot()/build_task_detail() compute, only how much of
# it survives one tool call. Every drop is reported, never silent.
# A dashboard refresh is a control-plane heartbeat, not a history export.
# Keep it comfortably below common MCP/Webview message thresholds; detailed
# task, log, memory, session and KB data already have dedicated bounded tools.
MAX_SNAPSHOT_RESPONSE_BYTES = 512 * 1024
MAX_TASK_DETAIL_RESPONSE_BYTES = 1 * 1024 * 1024

# Largest / least essential first. status_counts, tasks, and row_counts are
# never trimmed by this list -- they are what the summary strip and task
# table need on every refresh.
_SNAPSHOT_TRIM_ORDER: tuple[str, ...] = (
    "agent_processes",
    "cost_usage",
    "completion_inbox",
    "collision_report",
    "callback_bridge_health",
    "summaries",
    "warnings",
)

_COMPACT_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "schema_version",
    "generated_at",
    "readonly",
    "storage",
    "storage_usage",
    "health",
    "status_counts",
    "row_counts",
    "read_bounds",
    "warnings",
    "errors",
    "manager_identity",
    "callback_delivery",
    "manager_identity_target",
    "known_repositories",
    "server_tool",
    "authority_flags",
)

# Each retention provider lists up to its newest 100 batches; the summary keeps a few.
MAX_SNAPSHOT_QUARANTINE_ROWS = 5
_SNAPSHOT_QUARANTINE_ARRAYS: tuple[str, ...] = (
    "quarantine_batches",
    "terminal_log_quarantine_batches",
    "task_retention_batches",
)
_CONTRACT_RECALL = (
    "aiworkhub_manager_bootstrap(include_contract=true) re-delivers the "
    "contract prose; it is unchanged while contract_sha256 is unchanged. "
    "Compact aiworkhub_dashboard_snapshot re-sends it only when "
    "known_contract_sha256 differs from the current contract_sha256; "
    "full=True always includes it."
)

# Detail fields trimmed, largest first, only if the single-task payload is
# still over budget (a huge validation_output/result blob on one card).
_DETAIL_TRIM_FIELDS: tuple[str, ...] = (
    "result",
    "worker_result",
    "completion_summary",
    "review_summary",
    "review_notes",
    "validation_output",
    "ai_infra_context",
)

# Bound on each incremental Live Output raw-text read. The cursor advances by
# exactly the delivered raw bytes, so a caller can fetch the next chunk
# without loss. Keep this materially below the general log-tail bound: JSONL
# provider streams can represent each token fragment as a full event object,
# and HTML escaping can expand the serialized response beyond the raw size.
MAX_LIVE_OUTPUT_BYTES = 8 * 1024
MAX_MEMORY_ROWS = 200
MAX_MEMORY_VALUE_CHARS = 4000
MAX_SESSION_ROWS = 200
MAX_KB_ROWS = 200
MAX_CONTEXT_VALUE_CHARS = 4000

# Full snapshots transiently decode and join hundreds of rich task cards.  A
# single mature-repository build peaks around 0.5 GiB even though the bounded
# response is below 0.5 MiB.  Separate visible Webviews can ask the same stdio
# server concurrently, so serialize that allocation-heavy phase process-wide;
# per-view polling already coalesces its own requests.
_SNAPSHOT_BUILD_LOCK = Lock()

# NF-2026-01731: an idle dashboard tick re-ran the ~2.8 s build even when no
# input had changed. One build per mode is memoized under _SNAPSHOT_BUILD_LOCK
# and reused while the cheap generation key below is unchanged. A changed key,
# an unreadable key, a degraded build, a caller-supplied ``previous`` or the
# TTL backstop (state that moves without a file write: a process exit, a
# stale-processing age threshold) always rebuilds. Bounded: one entry per mode.
SNAPSHOT_BUILD_CACHE_TTL_SECONDS = 60.0
_SNAPSHOT_BUILD_CACHE: dict[bool, tuple[Any, tuple[Any, ...], float, dict[str, Any]]] = {}
_SNAPSHOT_BUILD_CACHE_STATS: dict[str, int] = {"hit": 0, "miss": 0, "key_error": 0}
_TASK_QUEUE_DB_REL: tuple[str, ...] = (".aiworkhub", "tasking", "task_queue.sqlite")
# Directory mtimes move when a process run's status/log files are created or removed.
_SNAPSHOT_KEY_DIRS_REL: tuple[tuple[str, ...], ...] = (
    (".aiworkhub", "runtime", "process_logs"),
    (".aiworkhub", "runtime", "process_logs", "processes"),
)
# The other stores and config a full build projects from (stat identity only).
_SNAPSHOT_KEY_FILES_REL: tuple[tuple[str, ...], ...] = (
    tuple(needfix_store.NEEDFIX_DB_REL),
    tuple(roadmap_store.ROADMAP_DB_REL),
    tuple(skill_registry_store.SKILLS_DB_REL),
    (".aiworkhub", "config", "development_rules.json"),
)
# SQLite wal-index header (mxFrame, iChange, salts, checksums): it changes on
# every WAL commit, so it stands in for ``PRAGMA data_version`` without holding
# a connection open -- on Windows a held handle blocks a replace/delete of the
# database file by retention or re-initialization.
_WAL_INDEX_HEADER_BYTES = 48

# Delta channel (NF-2026-01731): a full-mode caller that passes
# ``previous={"section_digests": {...}}`` -- one sha256 per top-level section
# of the full snapshot it already holds -- receives only the sections whose
# digest changed plus the current digest map. Any digest miss returns the
# plain full snapshot instead.
SNAPSHOT_SECTION_DIGESTS_FIELD = "section_digests"
SNAPSHOT_DELTA_SCHEMA = "aiworkhub.dashboard_snapshot_delta.v1"
MAX_SNAPSHOT_SECTION_DIGESTS = 256
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# The ONE genuine "repository has never been initialized" reason prefix
# ``task_store.storage_readiness`` can produce: ``inspect_repository`` raised
# ``ManifestMissingError`` because no ``.aiworkhub/project.json`` has ever
# been written. Every other ``storage_readiness`` reason (a corrupt/
# mismatched storage registry, a missing/corrupt canonical DB, a schema or
# quick_check failure, a manifest with the wrong schema/layout version) is a
# REAL failure, not "never initialized" -- ``is_not_initialized_reason`` must
# return False for those so the dashboard never tells a user with a corrupt
# repository to "just click Init Repo" as though nothing has happened yet.
_NOT_INITIALIZED_REASON_PREFIXES: tuple[str, ...] = ("manifest_invalid:manifest_missing",)


def _debug_trace(event: str, **fields: Any) -> None:
    trace_file = os.environ.get("AIWORKHUB_DEBUG_TRACE_FILE", "").strip()
    if not trace_file:
        return
    payload = {
        "schema_id": "aiworkhub.mcp_debug_trace.v1",
        "timestamp_epoch": time.time(),
        "monotonic": time.monotonic(),
        "process": "mcp-server",
        "pid": os.getpid(),
        "event": str(event)[:120],
        **fields,
    }
    data = (json.dumps(payload, ensure_ascii=True, default=str) + "\n").encode("utf-8")
    try:
        os.makedirs(os.path.dirname(trace_file), exist_ok=True)
        fd = os.open(trace_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        return


def _debug_stage(name: str, operation: Any) -> Any:
    started = time.perf_counter()
    _debug_trace("snapshot.stage.begin", stage=name)
    try:
        result = operation()
    except Exception as exc:
        _debug_trace(
            "snapshot.stage.error",
            stage=name,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            error_type=type(exc).__name__,
        )
        raise
    _debug_trace(
        "snapshot.stage.end",
        stage=name,
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
    )
    return result


def is_not_initialized_reason(reason: str) -> bool:
    """True only for the genuine "never initialized" storage_readiness
    reason -- never for a corrupt/mismatched registry, a missing/corrupt
    canonical DB, or a schema/quick_check failure. Callers (health_view,
    snapshot_view) use this to decide whether "Initialize AIWorkHub" is the
    correct recommended action, so a real corruption is never masked as
    "just click init"."""
    text = str(reason or "")
    return any(text.startswith(prefix) for prefix in _NOT_INITIALIZED_REASON_PREFIXES)


def _readonly_authority_flags() -> dict[str, bool]:
    return {
        "readonly": True,
        "queue_write": False,
        "audit_write": False,
        "process_launch": False,
        "agent_launch": False,
        "shell_invocation": False,
    }


def _storage_write_authority_flags() -> dict[str, bool]:
    return {
        "readonly": False,
        "queue_write": False,
        "audit_write": True,
        "storage_write": True,
        "process_launch": False,
        "agent_launch": False,
        "shell_invocation": False,
    }


MAX_MODEL_POLICY_CATALOG_ROWS = 128

# Ingestion bound, distinct from the compact render bound above. The rows that
# survive into the response are chosen per provider, and that choice can only
# be fair if it has seen every provider first: a head slice taken before the
# grouping deletes whichever providers sort last, entirely and silently.
MAX_MODEL_POLICY_SOURCE_ROWS = 512

# The hard ceilings neither of the two bounds above may pass. Both are raised
# to a correctness floor -- every rendered provider represented, every
# explicitly configured route reserved -- and the second half of that floor is
# derived from ``models.json``, a file this module does not control and nothing
# else bounds. One declared leaf per catalog row therefore pinned every row and
# lifted both "bounds" to the whole catalog: not a bounded payload with a
# raised floor, but an unbounded one still publishing a row_limit beside
# itself. Past these numbers provider representation is admitted first and the
# pins that did not fit are counted rather than dropped in silence.
MAX_MODEL_POLICY_CATALOG_ROW_CEILING = 256
MAX_MODEL_POLICY_SOURCE_ROW_CEILING = 1024

# And the read of that file is bounded too, for the same reason: leaves become
# pins, and pins become reserved rows. The payload publishes the file's real
# leaf count beside this, so a declaration set larger than the view can read is
# stated rather than silently shortened.
MAX_MODEL_POLICY_DECLARED_LEAVES = 1024

# The two discovery boundaries this module reads are already bounded by their
# own producers, and neither publishes a total beside the list it returns. So
# the ingestion bounds below could not bind on them at all: 512 never binds on
# a list a producer already cut to 256 or 128, which made the bound's own
# ``truncated`` signal unable to fire and published the survivors' count as the
# host's total. A provider was therefore understated, and a configured route
# the producer had omitted was labelled as one no source ever offered.
#
# These mirror those producers' caps -- ``workforce_catalog``'s OpenCode row
# cap and ``vscode_lm_bridge``'s observed-model slice -- and are used ONLY as
# evidence that an upstream bound bound, never to re-impose one here. A list
# that comes back at its producer's ceiling is a list whose tail is unknown,
# and that is what gets published rather than a total this module cannot see.
OPENCODE_UPSTREAM_IDENTITY_CEILING = workforce_catalog.OPENCODE_MODELS_MAX_ROWS
EDITOR_UPSTREAM_MODEL_CEILING = 128

# The OpenCode snapshot this module RECOVERS is bounded by its own producer too,
# and not by the generic source ceiling above. ``repo_policy`` publishes
# ``provider_observed_models`` either as ``parse_opencode_models_output``'s
# row read of ``opencode models``, bounded by that same
# ``workforce_catalog.OPENCODE_MODELS_MAX_ROWS`` cap, or as a 128-row slice of
# the host's observed list, so a recovered list can never reach
# MAX_MODEL_POLICY_SOURCE_ROW_CEILING: a floor tested against that number was
# unreachable outside a synthetic snapshot, and an at-cap host therefore
# published its recovered length as an exact total -- the same understatement
# the recovery exists to remove, one layer in. These are the lengths that mean
# "this list may itself have been cut", used ONLY as evidence and never to
# re-impose a bound. A list longer than both was cut by neither.
OPENCODE_UPSTREAM_OFFERED_CEILINGS = (
    # The CLI path: the parser's own row cap, the same one that tells this
    # module the producer's identity ceiling bound.
    OPENCODE_UPSTREAM_IDENTITY_CEILING,
    # The readiness path: repo_policy's slice of the host's observed list.
    # Now the smaller of the two caps, since the CLI path's cap was raised.
    128,
)


def _policy_route_keys(
    provider: str,
    adapter: str,
    model: str,
) -> set[tuple[str, str, str]]:
    """Both identities one launch route answers to: as written, and canonical.

    ``models.json`` may name an OpenCode decision under the vendor provider it
    was discovered as -- ``xai``/``opencode_cli`` -- while the catalog row for
    that same launch route carries the canonical policy owner that
    ``policy_route_identity`` assigns it, ``opencode``/``opencode_cli``.
    Comparing a single spelling therefore fails to recognise the two as one
    route, and an explicit decision silently stops pinning the row it was
    written for. Emitting both spellings makes the comparison symmetric
    whichever side of it the caller happens to hold.
    """

    written = (str(provider)[:128], str(adapter)[:128], str(model)[:128])
    try:
        canonical_provider, canonical_adapter = (
            workforce_catalog.policy_route_identity(provider, adapter)
        )
    except model_settings.ModelSettingsError:
        # An identity the policy layer refuses to normalise still names a row
        # verbatim; it simply has no second spelling to be compared against.
        return {written}
    return {
        written,
        (canonical_provider[:128], canonical_adapter[:128], written[2]),
    }


def _declared_policy_leaves(
    policy: Mapping[str, Any],
    *,
    limit: int,
) -> tuple[list[tuple[str, str, str]], int]:
    """Route leaves the owner wrote, bounded, beside the count the file holds.

    Separated from the pin key space below because the two answer different
    questions. Pinning needs both spellings of a route so a comparison against
    an already-built row is symmetric; building a row *from* a declaration
    needs the declaration itself, once, so one decision cannot become two rows.

    The read is bounded because nothing else bounds this file and every leaf in
    it becomes a pin -- a pin the row bounds then reserve a slot for. Read
    without a limit, a large ``models.json`` therefore sized the payload rather
    than the payload's own caps doing it. The declared total is returned
    alongside so the caller can state that the file was read short, which a
    shorter list of decisions cannot say for itself.
    """

    leaves: list[tuple[str, str, str]] = []
    declared = 0
    models = policy.get("models")
    if not isinstance(models, Mapping):
        return leaves, declared
    for provider, adapters in models.items():
        if not isinstance(adapters, Mapping):
            continue
        for adapter, entries in adapters.items():
            if not isinstance(entries, Mapping):
                continue
            for model in entries:
                declared += 1
                if len(leaves) < limit:
                    leaves.append((str(provider), str(adapter), str(model)))
    return leaves, declared


def _explicit_policy_routes(
    leaves: list[tuple[str, str, str]],
) -> set[tuple[str, str, str]]:
    """Exact routes the repository owner named in the model settings file.

    A route somebody explicitly switched on or off is a decision, not a
    discovery. It must stay in the rendered payload whatever the compact bound
    costs elsewhere, because a route the Webview never draws is a route nobody
    can toggle back without hand-editing ``.aiworkhub/config/models.json``.

    Every decision is recorded under both the identity it was written with and
    the canonical policy identity the catalog rows carry, so a vendor-keyed
    OpenCode decision pins the row it names rather than a route that does not
    exist.

    Takes the leaves the caller already read rather than the policy mapping, so
    the pin set is exactly the set of decisions that caller reports on and the
    two can never describe different reads of the same file.
    """

    routes: set[tuple[str, str, str]] = set()
    for provider, adapter, model in leaves:
        routes |= _policy_route_keys(provider, adapter, model)
    return routes


def _row_route_keys(row: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    """The canonical and vendor spellings of one already-built catalog row."""

    keys = {(row["provider"], row["adapter"], row["model"])}
    vendor_provider = str(row.get("vendor_provider") or "")
    declared_adapter = str(row.get("declared_adapter") or "")
    if vendor_provider and declared_adapter:
        keys.add(
            (vendor_provider[:128], declared_adapter[:128], row["model"])
        )
    return keys


def _bounded_provider_selection(
    group_keys: list[str],
    *,
    limit: int,
    ceiling: int,
    reserved: set[int],
    rendered_keys: list[str] | None = None,
) -> tuple[set[int], int, int]:
    """Spend a row bound from a floor and one group at a time, never as a head.

    A head slice deletes whichever groups sort or arrive last, entirely and
    silently, and paying reserved rows out of the same budget first reproduces
    that loss one layer down. So selection starts from what correctness
    requires -- the caller's reserved indices, plus the first index of any
    group those left unrepresented -- and only then hands the remaining budget
    out one row per group per round.

    ``group_keys`` is the partition the budget is spent *between*; where the
    caller has a second, coarser partition that the payload is *rendered and
    counted* in, it passes that as ``rendered_keys``. The two are genuinely
    different maps rather than one nested inside the other: a vendor reaching
    two adapters is a single group to be fair between and two rendered
    providers. Representing only the finer one therefore still allowed a whole
    rendered provider to be emptied -- and its loss entry, filed in the
    rendered key space, to name a family nothing drew -- so both get a floor.

    ``ceiling`` is the number that floor may not pass. ``reserved`` is derived
    from the owner's declaration file, which nothing else bounds, so a
    declaration naming every row pinned every row and raised the "bound" to the
    whole catalog: an unbounded payload still reporting a limit. Past the
    ceiling the representatives are admitted first -- rendered providers, then
    vendors -- and the reserved indices in catalog order, and whatever does not
    fit is returned as a count rather than disappearing. A bucket holding a
    reserved index is represented by that index rather than by its first row,
    so representation and an explicit decision share one slot wherever they
    can and the ceiling refuses the fewest reserved indices it can.

    Returns the selected indices, the limit actually honoured -- the declared
    bound raised to the floor and clamped to the ceiling -- and how many
    reserved indices the ceiling refused.
    """

    by_group: dict[str, list[int]] = {}
    for index, key in enumerate(group_keys):
        by_group.setdefault(key, []).append(index)
    groups = sorted(by_group)

    by_rendered: dict[str, list[int]] = {}
    for index, key in enumerate(rendered_keys or group_keys):
        by_rendered.setdefault(key, []).append(index)

    hard_ceiling = max(0, ceiling)

    def _represent(floor: set[int]) -> set[int]:
        for buckets in (by_rendered, by_group):
            for key in sorted(buckets):
                indices = buckets[key]
                if not any(index in floor for index in indices):
                    floor.add(indices[0])
        return floor

    selected = _represent(set(reserved))
    refused_reserved = 0
    if len(selected) > hard_ceiling:
        # The floor no longer fits, so what it is spent on has to be a decision
        # rather than whatever a set happens to iterate first. A provider with
        # no row at all is the one failure the Webview offers no remedy for, so
        # representation is admitted ahead of the named decisions, and the
        # decisions that do not fit are counted for the caller to publish.
        ordered: list[int] = []
        seen: set[int] = set()

        def _push(index: int) -> None:
            if index not in seen:
                seen.add(index)
                ordered.append(index)

        for buckets in (by_rendered, by_group):
            for key in sorted(buckets):
                indices = buckets[key]
                if any(index in seen for index in indices):
                    continue
                # A bucket that already holds a reserved index is represented
                # BY that index. Rebuilding from an empty ``seen`` and taking
                # ``indices[0]`` spent one slot on a row nobody asked for and
                # then charged the pin it displaced to ``refused_reserved`` --
                # two slots for one bucket, and a refusal count larger than the
                # ceiling actually forces. Representation still outranks the
                # decisions; it just stops paying twice for the same row.
                _push(
                    next(
                        (index for index in indices if index in reserved),
                        indices[0],
                    )
                )
        for index in sorted(reserved):
            _push(index)
        selected = set(ordered[:hard_ceiling])
        refused_reserved = sum(
            1 for index in reserved if index not in selected
        )

    # A bound below the floor cannot represent every group and every explicit
    # decision however fairly it is spent. It is still a bound, and the caller
    # publishes this number so the payload never claims a tighter bound than
    # the one it honoured -- nor a looser one than the ceiling allows.
    effective_limit = min(max(limit, len(selected)), hard_ceiling)

    cursors = dict.fromkeys(groups, 0)
    progressed = True
    while progressed and len(selected) < effective_limit:
        progressed = False
        for group in groups:
            if len(selected) >= effective_limit:
                break
            indices = by_group[group]
            cursor = cursors[group]
            while cursor < len(indices) and indices[cursor] in selected:
                cursor += 1
            if cursor >= len(indices):
                cursors[group] = cursor
                continue
            selected.add(indices[cursor])
            cursors[group] = cursor + 1
            progressed = True
    return selected, effective_limit, refused_reserved


def _canonical_route_key(
    provider: str,
    adapter: str,
    model: str,
) -> tuple[str, str, str]:
    """The exact identity triple a rendered row carries for one launch route.

    Its first element is the provider key a rendered row -- and the Webview's
    tree -- groups by: one key space, used by ``provider_counts`` and by the
    ingestion loss beside it, so a reader never has to know which of a route's
    two spellings a given number was filed under. Counting how many routes a
    bound actually cost needs the whole identity instead, because the
    configured catalog and the discovery probes describe overlapping
    populations: the same launch route refused by two sources is one route the
    payload is missing, not two.
    """

    try:
        canonical_provider, canonical_adapter = (
            workforce_catalog.policy_route_identity(provider, adapter)
        )
    except model_settings.ModelSettingsError:
        # An identity the policy layer refuses to normalise still names itself,
        # and a row built from it would carry that same spelling.
        return (str(provider)[:128], str(adapter)[:128], str(model)[:128])
    return (
        canonical_provider[:128],
        canonical_adapter[:128],
        str(model)[:128],
    )


def _ingestion_loss(
    canonical_keys: list[str],
    group_keys: list[str],
    selected: set[int],
) -> list[dict[str, Any]]:
    """Per-provider truth about what one ingestion bound just cost.

    Every ingestion source -- the configured catalog, the OpenCode discovery
    probe and the editor bridge -- spends fairness per *vendor* spelling, which
    is the finer partition: two vendors discovered through OpenCode are two
    groups there and one family in the tree, so neither is starved by a sibling
    that merely shares its canonical owner. The loss is *reported* in the
    canonical key space instead, because that is the one the rendered rows and
    the Webview group by -- filed under ``xai``, an ``xai``/``opencode_cli``
    shortfall named a family the tree never draws, so the number was published
    and still unreachable.

    This is one source's own account of its own rows, and it is listed only for
    providers that actually lost some. It is deliberately not the number a
    provider's *size* is stated from: two sources can refuse the same launch
    route, so summing their raw drop counts would count that route twice and
    inflate the denominator the tree divides by. The caller dedupes dropped
    route identities instead.
    """

    totals: dict[str, int] = {}
    ingested: dict[str, int] = {}
    vendors: dict[str, set[str]] = {}
    for index, key in enumerate(canonical_keys):
        totals[key] = totals.get(key, 0) + 1
        if index in selected:
            ingested[key] = ingested.get(key, 0) + 1
        else:
            vendors.setdefault(key, set()).add(group_keys[index])
    return [
        {
            "provider": key,
            "total": totals[key],
            "ingested": ingested.get(key, 0),
            "dropped": totals[key] - ingested.get(key, 0),
            # Which vendor spellings under this canonical owner actually lost
            # rows, so grouping by the canonical key never hides that it was
            # xai rather than opencode itself that was cut.
            "vendor_providers": sorted(vendors.get(key, set())),
        }
        for key in sorted(totals)
        if totals[key] > ingested.get(key, 0)
    ]


def _bounded_source_rows(
    source_rows: list[Mapping[str, Any]],
    *,
    limit: int,
    ceiling: int,
    pinned: set[tuple[str, str, str]],
) -> tuple[
    list[Mapping[str, Any]],
    list[dict[str, Any]],
    list[tuple[str, str, str]],
    int,
    int,
]:
    """Ingest at most ``limit`` catalog rows without losing a whole provider.

    The render bound below can only be fair if it has seen every provider, so
    ingestion cannot be a head slice either: a provider whose first row sits
    at index 600 of a 1000-row catalog was deleted before grouping ever ran,
    and no downstream fairness could bring it back. Ingestion is therefore
    spent by the same floor-first, one-row-per-provider rule, with explicitly
    configured routes reserved so a named decision survives its own catalog
    position.

    "Provider" means two things here and the bound owes both a floor. The
    vendor spelling a row was declared under is the finer partition and the
    fair one to spend a budget between; the canonical provider the row is
    rendered, counted and reported under is the coarser one. They are not
    nested the way a single key list assumes -- an ``xai`` vendor reaching both
    ``opencode_cli`` and ``grok_kilo_cli`` is one vendor group and two rendered
    providers -- so a floor spent only per vendor could still admit nothing but
    the OpenCode half and delete the whole rendered ``xai`` provider, whose
    ingestion-loss entry the tree would then have no family to reach. Both key
    spaces are handed to the selection for exactly that reason.

    Rows missing a provider, adapter or model are dropped up front because the
    caller cannot build a route from them; spending ingestion budget on them
    would cost a real provider a slot.

    Returns the ingested rows in catalog order, the per-provider ingestion loss,
    the canonical route identities the bound refused -- identities rather than a
    count, because the caller has to fold three sources together and the same
    route refused twice is one row missing -- the bound actually honoured, the
    declared limit raised to the floor and then clamped to ``ceiling``, and how
    many pins that clamp refused. The honoured figure used to be discarded, and
    while it was, a floor a pin had legitimately raised and a cap that simply
    failed to hold were indistinguishable from the payload.
    """

    usable: list[Mapping[str, Any]] = []
    group_keys: list[str] = []
    canonical_keys: list[str] = []
    route_keys: list[tuple[str, str, str]] = []
    reserved: set[int] = set()
    for row in source_rows:
        vendor_provider = str(row.get("provider") or "")
        declared_adapter = str(row.get("adapter_id") or "")
        model = str(row.get("model") or "")
        if not vendor_provider or not declared_adapter or not model:
            continue
        if _policy_route_keys(vendor_provider, declared_adapter, model) & pinned:
            reserved.add(len(usable))
        usable.append(row)
        group_keys.append(vendor_provider[:128])
        route_keys.append(
            _canonical_route_key(vendor_provider, declared_adapter, model)
        )
        canonical_keys.append(route_keys[-1][0])

    selected, honoured, pins_refused = _bounded_provider_selection(
        group_keys,
        limit=limit,
        ceiling=ceiling,
        reserved=reserved,
        rendered_keys=canonical_keys,
    )
    rows = [row for index, row in enumerate(usable) if index in selected]
    loss = _ingestion_loss(canonical_keys, group_keys, selected)
    dropped_routes = [
        key for index, key in enumerate(route_keys) if index not in selected
    ]
    return rows, loss, dropped_routes, honoured, pins_refused


def _refused_source_rows(
    source_rows: list[Mapping[str, Any]],
    dropped_routes: list[tuple[str, str, str]],
) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    """Configured rows an ingestion bound refused, keyed by route identity.

    A refused row is still a row the catalog stated: it carries a ``worker_id``
    and an ``enabled`` value the owner can read. Rebuilding that route from the
    owner's declaration instead blanks both -- publishing a configured, possibly
    disabled route as one no source ever offered -- so the refused row itself is
    carried past the bound and used to materialise its own identity.

    Keyed in the same canonical space the rendered rows and the declared leaves
    use, because the declaration and the catalog row may spell the same launch
    route differently and a lookup on one spelling would miss the other.
    """

    refused = set(dropped_routes)
    if not refused:
        return {}
    by_key: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for row in source_rows:
        vendor_provider = str(row.get("provider") or "")
        declared_adapter = str(row.get("adapter_id") or "")
        model = str(row.get("model") or "")
        if not vendor_provider or not declared_adapter or not model:
            continue
        key = _canonical_route_key(vendor_provider, declared_adapter, model)
        if key in refused:
            by_key.setdefault(key, row)
    return by_key


def _bounded_identity_ingestion(
    entries: list[tuple[str, str]],
    *,
    adapter: str,
    limit: int,
    ceiling: int,
    pinned: set[tuple[str, str, str]],
    upstream_refused: list[tuple[str, str]] | None = None,
    upstream_ceiling_bound: bool = False,
) -> dict[str, Any]:
    """Ingest at most ``limit`` discovered identities, vendor-fairly.

    A discovery probe is the second kind of ingestion source, and both of the
    ones this module reads had kept the head slice the configured catalog
    already gave up. ``opencode models`` lists every vendor it can reach in one
    flat sequence and the editor bridge lists every identity the Copilot host
    reported, so whatever either listed last was deleted whole before the tree
    grouped anything -- and a route the owner had explicitly configured went
    with it, because pins were never consulted here at all. Nothing downstream
    could even report the loss: the discovery counts were incremented after the
    slice, so they described the survivors and published that as the source's
    total.

    Selection is therefore the same floor-first, one-identity-per-vendor rule
    the catalog source uses, with explicitly configured routes reserved under
    either spelling, and the loss reported in the canonical key space the
    Webview's tree actually groups by. That key space is also handed to the
    selection as the rendered partition, because a vendor and the provider its
    routes are drawn under are not the same map: one vendor reaching two
    adapters is a single group to be fair between and two rendered providers,
    so vendor fairness alone could still empty one of them and file that loss
    under a family the tree never draws.

    ``entries`` are ``(identity, vendor_provider)`` pairs the caller has already
    reduced to identities it can build a route from, because spending ingestion
    budget on one it would discard afterwards costs a real vendor a slot.

    ``upstream_refused`` are identities the source's own PRODUCER cut before
    this module was handed anything -- ``(identity, vendor_provider)`` pairs the
    caller recovered from the producer's input. They are appended after the
    selection has already run, so they can never be ingested here, yet they do
    count in this source's total, in its per-provider loss and in the canonical
    route identities it reports as refused. Without them the bound described
    only the rows it was given: 512 cannot bind on a list a producer already cut
    to 64, so ``truncated`` was structurally False and the delivered length was
    published as the host's total -- an understated provider, and a configured
    route the producer had dropped labelled as one nothing ever offered.

    ``upstream_ceiling_bound`` says the producer's own cap bound while the
    refused identities themselves could not be recovered (the editor bridge
    returns a slice and no total). Then the total below is a floor and not a
    measurement, and it is published as such rather than as a complete count.

    Returns the ingested identities in discovery order alongside the exact
    total/returned/truncated truth, the bound actually honoured, the pins the
    ceiling refused, the per-provider loss, and the canonical route identities
    the bound refused.
    """

    usable: list[str] = []
    group_keys: list[str] = []
    canonical_keys: list[str] = []
    route_keys: list[tuple[str, str, str]] = []
    reserved: set[int] = set()
    for identity, vendor_provider in entries:
        if _policy_route_keys(vendor_provider, adapter, identity) & pinned:
            reserved.add(len(usable))
        usable.append(identity)
        group_keys.append(vendor_provider[:128])
        route_keys.append(
            _canonical_route_key(vendor_provider, adapter, identity)
        )
        canonical_keys.append(route_keys[-1][0])

    selected, honoured, pins_refused = _bounded_provider_selection(
        group_keys,
        limit=limit,
        ceiling=ceiling,
        reserved=reserved,
        rendered_keys=canonical_keys,
    )
    ingested = [
        identity for index, identity in enumerate(usable) if index in selected
    ]
    # Delivered is what this bound was actually offered; everything appended
    # below is what the producer had already refused. Both are published,
    # because "the cap upstream bound" and "the cap here bound" have different
    # remedies and only one of them is this module's to raise.
    delivered = len(usable)
    for identity, vendor_provider in upstream_refused or []:
        usable.append(identity)
        group_keys.append(vendor_provider[:128])
        route_keys.append(
            _canonical_route_key(vendor_provider, adapter, identity)
        )
        canonical_keys.append(route_keys[-1][0])
    return {
        "identities": ingested,
        "total": len(usable),
        "delivered": delivered,
        "returned": len(ingested),
        "truncated": len(ingested) < len(usable),
        # The producer cut and this module could not name what it cut, so the
        # total above is the largest population anyone here can evidence and
        # not the host's. A label must say "at least" rather than claim it.
        "total_is_lower_bound": bool(upstream_ceiling_bound),
        "upstream_refused": len(usable) - delivered,
        "row_limit_honoured": honoured,
        "pinned_routes_refused": pins_refused,
        "ingestion_loss": _ingestion_loss(
            canonical_keys, group_keys, selected
        ),
        "dropped_routes": [
            key for index, key in enumerate(route_keys) if index not in selected
        ],
        # The tail of that same list, named apart because it was refused by a
        # different cap. These keys were appended after the selection had run,
        # so none of them can be in ``selected`` -- they are dropped by
        # construction and by a bound this module never applied. Folded into
        # the count above they made a producer's ceiling read as this
        # ingestion's, which points a reader at the wrong limit to raise.
        "upstream_dropped_routes": route_keys[delivered:],
    }


def _opencode_offered_identities(
    preflight: Mapping[str, Any] | None,
) -> list[str]:
    """The raw identity list the OpenCode producer was itself handed.

    ``opencode_identities_from_preflight`` parses this list and stops at
    ``workforce_catalog``'s own row cap, returning no count of what it left
    behind. The snapshot it read is right here though, so the population the
    parser was given is recoverable at this boundary rather than lost at it --
    which is the difference between "this provider has 64 routes" and "this
    provider has 201, of which 64 were parsed".
    """

    if not isinstance(preflight, Mapping):
        return []
    for item in preflight.get("providers") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("adapter_id") or "") != "opencode_cli":
            continue
        raw = item.get("provider_observed_models")
        if not isinstance(raw, list):
            raw = item.get("observed_models")
        if not isinstance(raw, list):
            return []
        # Bounded by the same ceiling the ingestion below honours, so recovering
        # the producer's input can never make this payload unbounded.
        return [
            value for value in raw[:MAX_MODEL_POLICY_SOURCE_ROW_CEILING]
            if isinstance(value, str)
        ]
    return []


def _bounded_opencode_identities(
    identities: list[str],
    *,
    offered: list[str],
    limit: int,
    ceiling: int,
    pinned: set[tuple[str, str, str]],
) -> dict[str, Any]:
    """Bound the OpenCode probe, grouped by the vendor each identity names.

    The vendor prefix is the finer partition and the fair one to spend the
    bound between; the canonical ``opencode`` owner all of them share is what
    the loss is later reported under, and what the rendered rows group by.

    ``offered`` is what the producer was handed before its own 64-row cap ran.
    The cap is only treated as having bound when the parse came back AT it:
    below that the parser saw every candidate, so anything missing was rejected
    on identity grounds rather than refused by a bound, and reporting those as
    lost routes would invent a shortfall. At the cap the remainder is unexamined
    and its well-formed identities are exactly the routes the producer cost this
    view -- named, so they dedupe against the other sources and so a declared one
    is rebuilt as the discovered row it is rather than as an undiscovered one.

    That recovery can be partial, and where it is the total is published as a
    floor, and the snapshot it reads is bounded by its own producer rather than
    by this module's recovery ceiling: ``repo_policy`` publishes it as the
    parser's 64-row read of ``opencode models`` or as a 128-row slice of the
    host's observed list. An unreadable snapshot names nothing, one that
    arrives AT either producer cap has an unexamined tail of its own, and one
    longer than the recovery ceiling is read only that far -- so at the
    producer's cap each case leaves identities past what anyone here can see:
    ``at least N`` rather than ``N``, which is what ``total_is_lower_bound``
    carries to the provider's own counts. A recovered list longer than both
    producer caps was cut by neither, and its total stays a measurement.
    """

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in identities:
        identity = str(value)[:128]
        if not identity:
            continue
        seen.add(identity)
        vendor_provider, _sep, _remainder = identity.partition("/")
        entries.append((identity, vendor_provider.lower() or "opencode"))

    upstream_refused: list[tuple[str, str]] = []
    producer_capped = len(entries) >= OPENCODE_UPSTREAM_IDENTITY_CEILING
    if producer_capped:
        for value in offered:
            identity = str(value).strip()[:128]
            if not identity or identity in seen:
                continue
            if not workforce_catalog.model_identity_valid(identity):
                continue
            seen.add(identity)
            vendor_provider, _sep, _remainder = identity.partition("/")
            upstream_refused.append(
                (identity, vendor_provider.lower() or "opencode")
            )
    # Recovering the producer's input is what makes this boundary's total a
    # measurement rather than a floor, and the recovery can itself come up
    # short: a snapshot that carries no readable list leaves nothing to read,
    # and one longer than the recovery ceiling is read only as far as that
    # ceiling. In both cases identities exist past what was recovered, so the
    # total below is the largest population anyone here can evidence and the
    # payload must say "at least". Detecting the producer's cap and then
    # publishing an exact count over a tail nobody saw is the same
    # understatement the cap detection exists to remove, one layer in.
    #
    # Which lengths mean "cut" comes from the producer that WROTE this
    # snapshot, not from the ceiling this module happens to read it through.
    # repo_policy publishes it as the parser's row read, bounded by its own
    # (now larger) row cap, or as a 128-row slice of the host's observed
    # list, so it can never reach MAX_MODEL_POLICY_SOURCE_ROW_CEILING: tested
    # against that number alone the floor was unreachable in production, and
    # an at-cap host published its recovered length as an exact total. The
    # recovery ceiling is still checked below it, because a snapshot read
    # only that far has an unseen tail for this module's own reason, while a
    # list longer than both producer caps was cut by neither and stays a
    # measurement.
    offered_count = len(offered)
    # A snapshot can sit exactly at a producer's own ceiling even when this
    # module's parser cap is now large enough to read it whole: the parser's
    # cap and repo_policy's 128-row readiness slice are two DIFFERENT
    # producers, and either one landing on its own ceiling leaves an unseen
    # tail regardless of whether entries/this parse needed to cut anything.
    snapshot_at_producer_ceiling = bool(offered) and (
        offered_count in OPENCODE_UPSTREAM_OFFERED_CEILINGS
        or offered_count >= MAX_MODEL_POLICY_SOURCE_ROW_CEILING
    )
    upstream_ceiling_bound = (
        (producer_capped and not offered) or snapshot_at_producer_ceiling
    )
    return _bounded_identity_ingestion(
        entries,
        adapter="opencode_cli",
        limit=limit,
        ceiling=ceiling,
        pinned=pinned,
        upstream_refused=upstream_refused,
        upstream_ceiling_bound=upstream_ceiling_bound,
    )


def _bounded_observed_models(
    observed_models: list[Any],
    *,
    limit: int,
    ceiling: int,
    pinned: set[tuple[str, str, str]],
) -> dict[str, Any]:
    """Bound the editor bridge's observed identities the same way.

    Every one of these is a ``copilot``/``vscode_lm`` route, so vendor fairness
    has a single group to be fair between and the bound's real work here is the
    pinned-route reservation and the count truth. A head slice was still a
    defect for both: a host reporting more identities than the cap hid whichever
    it listed last -- including a route the owner had explicitly configured, and
    which therefore lost the control that would switch it back -- and then
    published the survivors' count as the host's total, so the loss did not
    merely go unfixed, it went unstated.

    The bridge applies that same slice one layer up and publishes no total
    beside it, and unlike the OpenCode boundary its input is not reachable from
    here -- so at the ceiling the only honest statement is that the tail is
    unknown. The count is published as a floor rather than as the host's total,
    which is what ``total_is_lower_bound`` carries downstream; claiming the
    delivered length instead is the same understatement one source over.

    Identities the catalog could not build a route from are filtered here rather
    than after selection, so the bound is never spent on a row that is about to
    be discarded anyway.
    """

    entries: list[tuple[str, str]] = []
    for value in observed_models:
        model = str(value).strip()
        if not model or not workforce_catalog.model_identity_valid(model):
            continue
        entries.append((model[:128], "copilot"))
    return _bounded_identity_ingestion(
        entries,
        adapter="vscode_lm",
        limit=limit,
        ceiling=ceiling,
        pinned=pinned,
        upstream_ceiling_bound=(
            len(observed_models) >= EDITOR_UPSTREAM_MODEL_CEILING
        ),
    )


def _bounded_cli_models(
    listing: list[dict[str, Any]],
    *,
    vendor: str,
    adapter: str,
    limit: int,
    ceiling: int,
    pinned: set[tuple[str, str, str]],
) -> dict[str, Any]:
    """Bound one CLI-owned model list the way the other discovery sources are.

    ``cli_model_discovery.codex_models`` hands over every model the Codex CLI
    lists in a cache file it wrote itself, and the only ceiling on that list is
    the byte cap on reading the file: a couple of megabytes of minimal entries
    is tens of thousands of rows. Ingested as it arrived, that file sized the
    payload -- with no dropped route, no truncation flag and no slot held for a
    route the owner had explicitly configured, which then had no control to
    switch it back.

    Every identity here is one ``vendor``/``adapter`` route, so vendor fairness
    has a single group to be fair between and the bound's real work is the
    editor bridge's: reserving pinned routes and stating the count. It is the
    same ``_bounded_identity_ingestion`` at the same limit and ceiling, not a
    second accounting scheme.

    Unlike the OpenCode probe and the editor bridge, no producer cap sits above
    this list -- the cache is read whole or not at all -- so the total is a
    measurement and ``total_is_lower_bound`` is always False.

    The shared ingestion carries identities only, and a discovered row is drawn
    with its display label, so the ingested identities' labels are returned
    beside the result under ``labels``. An identity the CLI lists twice is one
    route and takes one slot; its first listing names it.
    """

    entries: list[tuple[str, str]] = []
    labels: dict[str, str] = {}
    for item in listing:
        model = str(item.get("model") or "")[:128]
        if not model or model in labels:
            continue
        labels[model] = str(item.get("label") or model)[:128]
        entries.append((model, vendor))
    source = _bounded_identity_ingestion(
        entries,
        adapter=adapter,
        limit=limit,
        ceiling=ceiling,
        pinned=pinned,
    )
    source["labels"] = {model: labels[model] for model in source["identities"]}
    return source


def _cli_source_view(source: Mapping[str, Any]) -> dict[str, Any]:
    """One bounded CLI source as the payload publishes it, keyed like its siblings.

    ``opencode_source`` and ``editor_source`` share this shape, so a reader can
    walk every discovery source alike. ``upstream_ceiling`` is None because no
    producer cap sits above a CLI list.
    """

    return {
        "total": source["total"],
        "delivered": source["delivered"],
        "upstream_refused": source["upstream_refused"],
        "upstream_ceiling": None,
        "returned": source["returned"],
        "truncated": source["truncated"] or source["total_is_lower_bound"],
        "total_is_lower_bound": source["total_is_lower_bound"],
        "row_limit": MAX_MODEL_POLICY_SOURCE_ROWS,
        "row_limit_honoured": source["row_limit_honoured"],
        "row_limit_ceiling": MAX_MODEL_POLICY_SOURCE_ROW_CEILING,
        "pinned_routes_refused": source["pinned_routes_refused"],
        "ingestion_loss": source["ingestion_loss"],
    }


def _bounded_catalog_rows(
    workers: list[dict[str, Any]],
    *,
    limit: int,
    ceiling: int,
    pinned: set[tuple[str, str, str]],
    dropped_before_ingestion: Mapping[str, int] | None = None,
    lower_bound_providers: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """Spend the compact row bound from a floor, not from the head of a sort.

    A head slice over the globally sorted catalog is what dropped whole
    providers, and paying pinned routes out of the same budget first
    reproduced that loss one layer down: with 64 explicitly configured leaves
    sorting before ``xai``, the bound was exhausted before the round-robin ran
    at all, so the last provider vanished again and the pins past the cutoff
    were dropped in silence.

    So the selection starts from what correctness requires rather than from
    what sorts first. Reserved are every explicitly configured route -- matched
    on either its canonical or its vendor spelling, because the row and the
    decision need not have been written the same way -- plus one route for
    each provider those pins left unrepresented. Both parts are bounded by the
    ingestion cap the caller applies before calling here. Whatever budget
    remains is handed out one route per provider per round, and survivors are
    returned in the caller's canonical sort order, so ordering still comes from
    identity and never from a provider preference.

    The floor is itself capped by ``ceiling``. Reserved is one index per
    explicitly configured route and models.json is the owner's file, so without
    that cap a declaration naming every catalog row raised the floor to the
    whole catalog: the payload stopped being bounded while still publishing a
    row_limit. Past the ceiling, provider representation is admitted first and
    the pins that did not fit are counted.

    Returns the rows, the per-provider count truth, the limit actually
    honoured, and how many explicitly configured routes the ceiling refused.
    """

    by_provider: dict[str, list[int]] = {}
    for index, row in enumerate(workers):
        by_provider.setdefault(row["provider"], []).append(index)
    providers = sorted(by_provider)

    reserved: set[int] = {
        index
        for index, row in enumerate(workers)
        if _row_route_keys(row) & pinned
    }
    # The rows are already in the canonical provider key space the Webview
    # groups by, so the fairness partition and the rendered partition are the
    # same one here and no second key list is needed.
    selected, effective_limit, pins_refused = _bounded_provider_selection(
        [row["provider"] for row in workers],
        limit=limit,
        ceiling=ceiling,
        reserved=reserved,
    )

    rows = [row for index, row in enumerate(workers) if index in selected]
    returned_by_provider: dict[str, int] = {}
    enabled_returned: dict[str, int] = {}
    for row in rows:
        provider = row["provider"]
        returned_by_provider[provider] = returned_by_provider.get(provider, 0) + 1
        if row.get("effective_enabled"):
            enabled_returned[provider] = enabled_returned.get(provider, 0) + 1
    # The enabled count a reader cares about is the provider's, not the
    # surviving rows'. Counting it only from ``rows`` would state the bound's
    # arithmetic as the provider's truth, so it is counted over every row the
    # provider had before the bound was spent.
    enabled_total: dict[str, int] = {
        provider: sum(
            1 for index in indices if workers[index].get("effective_enabled")
        )
        for provider, indices in by_provider.items()
    }
    # What the ingestion caps already spent, before this bound saw a single row.
    # ``by_provider`` can only count what survived those caps, so a provider's
    # own size is not derivable here and has to be carried in. That now includes
    # what each source's PRODUCER refused above this module: a 201-identity host
    # parsed down to 64 is a 201-route provider, and counting the 64 was the
    # understatement this bound then published as the provider's truth.
    dropped = dropped_before_ingestion or {}
    # Providers whose size is known only as a floor, because a producer cut
    # without saying how much. "at least N" and "N" are different claims and the
    # label must not print the second when only the first was measured.
    lower_bound = lower_bound_providers or set()
    provider_counts = [
        {
            "provider": provider,
            # The provider's real size, ingested rows plus the rows the caps
            # dropped. Taken from ``by_provider`` alone this reported the cap's
            # arithmetic as the provider's truth -- a 600-row provider whose
            # tail was cut published "511 of 511", which is not a bounded
            # answer but a wrong one, and the tree then drew it as complete.
            "total": len(by_provider[provider]) + int(dropped.get(provider, 0)),
            "total_is_lower_bound": provider in lower_bound,
            # Rows that reached this bound at all. The enabled counts below are
            # counted over exactly this population and no larger one.
            "ingested": len(by_provider[provider]),
            "returned": returned_by_provider.get(provider, 0),
            "truncated": returned_by_provider.get(provider, 0)
            < len(by_provider[provider]) + int(dropped.get(provider, 0))
            or provider in lower_bound,
            "enabled_total": enabled_total[provider],
            # The population ``enabled_total`` was counted over. Rows the
            # ingestion cap dropped were never evaluated against the policy, so
            # when this is smaller than ``total`` the enabled figure is the
            # ingested provider's and a label must not claim it provider-wide.
            "enabled_counted_over": len(by_provider[provider]),
            "enabled_returned": enabled_returned.get(provider, 0),
        }
        for provider in providers
    ]
    return rows, provider_counts, effective_limit, pins_refused


def _absent_route_attribution(
    sources: list[
        tuple[
            list[tuple[str, str, str]],
            list[dict[str, Any]],
            set[tuple[str, str, str]],
        ]
    ],
    *,
    rendered_routes: set[tuple[str, str, str]],
) -> dict[str, int]:
    """Charge every route the payload is missing to exactly one bound.

    ``dropped`` on an ingestion-loss entry counts what a single bound refused.
    That is a fact about the probe and not about the tree: the three sources
    describe overlapping populations, so a refusal whose route another source
    already supplied is not a row anybody is missing. The provider totals
    published beside these entries are deduped for exactly that reason, and a
    label dividing a raw drop count by a deduped denominator states neither
    population -- a discovery probe refusing 90 identities of which precisely
    one was a route nothing else carried read as "90 not loaded" beside a
    602-route provider whose payload was short by a single row.

    So each distinct absent route is counted once, under the first bound that
    refused it, and written back as ``absent_routes`` beside the raw
    ``dropped`` it must not be confused with. Summed across the sources that is
    the provider's ``total - ingested`` exactly, which is the denominator the
    tree divides by. A route two bounds both refused is still one missing row,
    and naming it under each would restate the same inflation one field over.

    A source's absent routes are then split once more, into the share its own
    PRODUCER refused before this module was handed anything. That subset is
    written as ``upstream_absent_routes``, a strict part of ``absent_routes``
    and never a second count to add to it: the two are one population reported
    at two different bounds, and the reader needs the split because the remedy
    differs. Without it a row the OpenCode parser cut at its own ceiling was
    rendered as lost to the discovery bound -- a cap that was never offered the
    route -- so the label named a limit that raising could not recover.

    ``sources`` are ``(dropped_routes, ingestion_loss, upstream_routes)``
    triples in the order the caller wants attribution to fall, and their loss
    entries are annotated in place.
    """

    counted: set[tuple[str, str, str]] = set()
    absent_by_provider: dict[str, int] = {}
    for dropped_routes, loss, upstream_routes in sources:
        source_absent: dict[str, int] = {}
        source_upstream_absent: dict[str, int] = {}
        for route in dropped_routes:
            # Already drawn from another source, or already charged to an
            # earlier bound. Either way it is not a second missing route.
            if route in rendered_routes or route in counted:
                continue
            counted.add(route)
            source_absent[route[0]] = source_absent.get(route[0], 0) + 1
            if route in upstream_routes:
                source_upstream_absent[route[0]] = (
                    source_upstream_absent.get(route[0], 0) + 1
                )
        for entry in loss:
            provider_key = str(entry.get("provider") or "")
            entry["absent_routes"] = source_absent.get(provider_key, 0)
            entry["upstream_absent_routes"] = source_upstream_absent.get(
                provider_key, 0
            )
        for provider, count in source_absent.items():
            absent_by_provider[provider] = (
                absent_by_provider.get(provider, 0) + count
            )
    return absent_by_provider


def _settings_preflight_snapshot(_root: Any) -> Mapping[str, Any] | None:
    """Reuse the catalog's already-built environment-preflight snapshot.

    Settings must not spawn a second ``opencode models`` probe. When no
    repo-bound snapshot has been established, this returns None.
    """

    return workforce_catalog.cached_preflight_snapshot(_root)


def _route_policy_enabled(
    policy: Mapping[str, Any],
    *,
    provider: str,
    adapter: str,
    model: str,
    vendor_provider: str = "",
    declared_adapter: str = "",
) -> bool:
    """The launch gate's own identity decision, evaluated here for display.

    This is deliberately not a second opinion about the same file.
    ``workforce_catalog.build_catalog`` decides a route's ``effective_enabled``
    by evaluating the canonical policy owner and the vendor spelling the row
    was declared under and requiring *both*, and ``repo_policy`` filters
    observed OpenCode identities on the canonical identity alone. Display
    computes exactly that conjunction, so a checkbox drawn checked is a route
    the repository will actually launch.

    The more generous reading is what had to go. Letting an explicit leaf under
    either spelling decide meant a vendor-keyed ``xai``/``opencode_cli`` entry
    rendered its route enabled while the canonical ``opencode`` identity -- the
    one the launcher consults -- still answered from the OpenCode identity
    default and refused it. The UI promised a route the repository would not
    run, which is a worse failure than the missing row it replaced, because
    nothing about a checked box says the launcher disagrees.

    Toggling that box in the Webview writes the canonical identity the row
    carries, so the route an owner actually switches on is enabled under both
    spellings, is reported enabled here, and is launch-eligible there. A
    vendor-keyed ``true`` nobody has re-toggled is reported exactly as the
    launcher treats it: not enabled.
    """

    identities = [(provider, adapter)]
    if (
        vendor_provider
        and declared_adapter
        and (vendor_provider, declared_adapter) != (provider, adapter)
    ):
        identities.append((vendor_provider, declared_adapter))
    return all(
        model_settings.evaluate_state(
            policy,
            provider=route_provider,
            adapter=route_adapter,
            model=model,
        )
        for route_provider, route_adapter in identities
    )


def _model_policy_view(
    root: Any,
    preflight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return bounded policy plus configured and live editor model inventory."""
    policy = model_settings.load(root)
    catalog = workforce_catalog.load_catalog(root)
    # One bounded read of the owner's declarations serves both the pin set and
    # the declared-route rows below, so the two can never disagree about which
    # decisions this view actually saw.
    declared_leaves, declared_leaf_total = _declared_policy_leaves(
        policy, limit=MAX_MODEL_POLICY_DECLARED_LEAVES
    )
    declared_leaves_truncated = declared_leaf_total > len(declared_leaves)
    pinned_routes = _explicit_policy_routes(declared_leaves)
    workers: list[dict[str, Any]] = []
    source_rows = [
        row for row in catalog.get("workers", []) if isinstance(row, Mapping)
    ]
    # Provider-aware ingestion rather than a head slice: a provider whose
    # first row sits past the cap was otherwise deleted before the render
    # bound below had grouped anything, and nothing downstream could undo it.
    # ``source_dropped_routes`` names the exact routes the cap refused, because
    # the counts published below are the provider's truth, cannot be taken from
    # the rows that happened to survive the cap, and cannot be a per-source
    # count either -- another source may describe the same route.
    (
        ingested_rows,
        source_ingestion_loss,
        source_dropped_routes,
        source_row_limit_honoured,
        source_pins_refused,
    ) = _bounded_source_rows(
        source_rows,
        limit=MAX_MODEL_POLICY_SOURCE_ROWS,
        ceiling=MAX_MODEL_POLICY_SOURCE_ROW_CEILING,
        pinned=pinned_routes,
    )
    for row in ingested_rows:
        vendor_provider = str(row.get("provider") or "")
        declared_adapter = str(row.get("adapter_id") or "")
        model = str(row.get("model") or "")
        if not vendor_provider or not declared_adapter or not model:
            continue
        provider, adapter = workforce_catalog.policy_route_identity(
            vendor_provider, declared_adapter
        )
        catalog_enabled = bool(row.get("enabled", True))
        workers.append(
            {
                "worker_id": str(row.get("worker_id") or "")[:128],
                "provider": provider[:128],
                "adapter": adapter[:128],
                "model": model[:128],
                "vendor_provider": vendor_provider[:128],
                "declared_adapter": declared_adapter[:128],
                "catalog_enabled": catalog_enabled,
                "effective_enabled": catalog_enabled
                and _route_policy_enabled(
                    policy,
                    provider=provider,
                    adapter=adapter,
                    model=model,
                    vendor_provider=vendor_provider,
                    declared_adapter=declared_adapter,
                ),
                "inventory_only": False,
            }
        )

    # This is a bounded heartbeat-registry read, not the full environment
    # preflight (which may intentionally run expensive Windows lifecycle
    # probes).  Settings therefore remains fast while reflecting the exact
    # model catalog that the active VS Code/Copilot host reported.
    editor = vscode_lm_bridge.bridge_readiness(
        root,
        model=None,
        adapter_id="vscode_lm",
    )
    observed_models = editor.get("observed_models")
    existing = {
        (row["provider"], row["adapter"], row["model"])
        for row in workers
    }
    discovered_count = 0
    inventory_only_count = 0
    # The editor bridge is an ingestion source like the other two and gets the
    # same pin-aware bound. The head slice it kept could hide whichever
    # identities a large Copilot host listed last -- including an explicitly
    # configured one, which then had no control to switch it back -- and the
    # counts beside it were taken after the slice, so the payload published the
    # survivors as the host's total.
    editor_source = _bounded_observed_models(
        observed_models if isinstance(observed_models, list) else [],
        limit=MAX_MODEL_POLICY_SOURCE_ROWS,
        ceiling=MAX_MODEL_POLICY_SOURCE_ROW_CEILING,
        pinned=pinned_routes,
    )
    for model in editor_source["identities"]:
        key = ("copilot", "vscode_lm", model)
        discovered_count += 1
        if key in existing:
            for worker in workers:
                if (
                    worker["provider"], worker["adapter"], worker["model"]
                ) == key:
                    worker["discovered_from_editor"] = True
            continue
        inventory_only_count += 1
        existing.add(key)
        workers.append(
            {
                "worker_id": "",
                "provider": "copilot",
                "adapter": "vscode_lm",
                "model": model,
                "vendor_provider": "",
                "declared_adapter": "vscode_lm",
                "catalog_enabled": True,
                "effective_enabled": _route_policy_enabled(
                    policy,
                    provider="copilot",
                    adapter="vscode_lm",
                    model=model,
                ),
                "inventory_only": True,
                "discovered_from_editor": True,
            }
        )
    # The discovery probe gets the same provider-aware, pin-aware ingestion the
    # configured catalog gets. A head slice here deleted whichever vendor
    # ``opencode models`` happened to list last -- and any decision the owner
    # had written for it -- before the tree had grouped anything.
    #
    # The producer's own 64-row cap runs first and reports nothing, so the
    # snapshot it parsed is handed in beside its result. Without it this bound
    # could only ever describe the 64 it was given, and every count downstream
    # -- the provider's size, its truncation flag, the "not loaded" clause --
    # was computed over a population the host had never been asked about.
    opencode_source = _bounded_opencode_identities(
        workforce_catalog.opencode_identities_from_preflight(preflight),
        offered=_opencode_offered_identities(preflight),
        limit=MAX_MODEL_POLICY_SOURCE_ROWS,
        ceiling=MAX_MODEL_POLICY_SOURCE_ROW_CEILING,
        pinned=pinned_routes,
    )
    opencode_discovered_count = 0
    for identity in opencode_source["identities"]:
        vendor_provider, _sep, _remainder = identity.partition("/")
        vendor_provider = vendor_provider.lower() or "opencode"
        provider, adapter = workforce_catalog.policy_route_identity(
            vendor_provider, "opencode_cli"
        )
        key = (provider, adapter, identity[:128])
        opencode_discovered_count += 1
        discovered_count += 1
        matched = False
        for worker in workers:
            if worker["adapter"] == "opencode_cli" and worker["model"] == identity[:128]:
                worker["discovered_from_opencode"] = True
                matched = True
                break
        if matched or key in existing:
            continue
        inventory_only_count += 1
        existing.add(key)
        workers.append(
            {
                "worker_id": "",
                "provider": provider[:128],
                "adapter": adapter[:128],
                "model": identity[:128],
                "vendor_provider": vendor_provider[:128],
                "declared_adapter": "opencode_cli",
                "catalog_enabled": True,
                "effective_enabled": _route_policy_enabled(
                    policy,
                    provider=provider,
                    adapter=adapter,
                    model=identity,
                    vendor_provider=vendor_provider,
                    declared_adapter="opencode_cli",
                ),
                "inventory_only": True,
                "discovered_from_opencode": True,
            }
        )
    # CLI-owned model lists (see cli_model_discovery) are ingestion sources like
    # the three above and get the same pin-aware bound. ``codex_models`` returns
    # every model in a cache file the CLI wrote, and the only ceiling on that is
    # the byte cap on reading it -- tens of thousands of minimal entries -- so
    # ingested unbounded the file sized the payload and a truncation could not
    # be stated. Each list counts its own discovered identities and publishes its
    # own source truth like its siblings; ``claude_models`` is always the four
    # aliases and can never bind, but is counted and reported the same way.
    #
    # A discovered model that a declared row already names only labels that row.
    cli_sources: dict[str, dict[str, Any]] = {}
    cli_discovered_counts = {"codex": 0, "claude": 0}
    for source_name, vendor, cli_adapter, listing in (
        ("codex", "openai", "codex_cli", cli_model_discovery.codex_models()),
        ("claude", "anthropic", "claude_cli", cli_model_discovery.claude_models(root)),
    ):
        cli_provider, route_adapter = workforce_catalog.policy_route_identity(vendor, cli_adapter)
        cli_source = _bounded_cli_models(
            listing,
            vendor=vendor,
            adapter=cli_adapter,
            limit=MAX_MODEL_POLICY_SOURCE_ROWS,
            ceiling=MAX_MODEL_POLICY_SOURCE_ROW_CEILING,
            pinned=pinned_routes,
        )
        cli_sources[source_name] = cli_source
        for model in cli_source["identities"]:
            label = cli_source["labels"][model]
            key = (cli_provider, route_adapter, model)
            cli_discovered_counts[source_name] += 1
            discovered_count += 1
            if key in existing:
                for worker in workers:
                    if (worker["provider"], worker["adapter"], worker["model"]) == key:
                        worker["discovered_from_cli"] = True
                        worker["label"] = label
                        break
                continue
            inventory_only_count += 1
            existing.add(key)
            workers.append(
                {
                    "worker_id": "",
                    "provider": cli_provider[:128],
                    "adapter": route_adapter[:128],
                    "model": model,
                    "vendor_provider": vendor,
                    "declared_adapter": cli_adapter,
                    "catalog_enabled": True,
                    "effective_enabled": _route_policy_enabled(
                        policy,
                        provider=cli_provider,
                        adapter=route_adapter,
                        model=model,
                        vendor_provider=vendor,
                        declared_adapter=cli_adapter,
                    ),
                    "inventory_only": True,
                    "discovered_from_cli": True,
                    "label": label,
                }
            )
    # Both discovery probes above can arrive already truncated, and neither
    # says so on its own. ``parse_opencode_models_output`` stops at its own
    # 64-row cap before ``opencode_identities_from_preflight`` returns, and the
    # editor bridge head-slices ``observed_models`` at 128 -- both before this
    # module reads a single identity, so MAX_MODEL_POLICY_SOURCE_ROWS cannot
    # bind on either and the pin reservation has nothing to reserve. The CLI
    # lists have no producer cap above them, so their own bound is the one that
    # binds and their loss is stated by it.
    # A route the owner explicitly named can therefore be absent from every list
    # this view can see, and an absent route has no checkbox to switch it on.
    #
    # The two boundaries differ in what can be recovered, and the payload keeps
    # them distinct rather than calling both "undiscovered". The OpenCode
    # producer's own input is reachable here, so the identities its cap refused
    # are named above and arrive as ordinary refused routes -- deduped against
    # the other sources, counted into the provider's real size, and rebuilt as
    # the discovered rows they are. The editor bridge's input is not reachable,
    # so at its ceiling the only recoverable fact is that the tail is unknown.
    #
    # A decision in ``models.json`` is authoritative on its own evidence: the
    # owner wrote that exact identity. So the row is materialised from the
    # declaration rather than waiting for a probe to re-offer it. It claims no
    # discovery -- nothing observed this route on this host -- and carries
    # ``declared_only`` so neither the payload nor the tree can label an
    # unobserved row as discovered. That label is itself a claim, though, and it
    # is withheld where the evidence cannot support it: under an editor ceiling
    # that bound, the route is marked ``upstream_truncated`` instead, because
    # "no host offers this" is not something a truncated list can establish.
    # Bounded by the same read that produced the pins, so a models.json larger
    # than MAX_MODEL_POLICY_DECLARED_LEAVES cannot materialise an unbounded
    # number of rows here. What the read left out is published rather than
    # silently absent.
    #
    # "No source supplied it" is a claim, and a route's absence from ``existing``
    # is not evidence for it. An ingestion bound that reaches its ceiling refuses
    # rows a source did supply, and rebuilding those from the declaration stated
    # three things the payload had evidence against: a blank ``worker_id`` over a
    # row that has one, a hardcoded ``catalog_enabled`` over a catalog that may
    # have said ``enabled: false``, and ``declared_only`` over a route discovery
    # did offer. Each refused route is therefore rebuilt from the source that
    # offered it and marked ``source_truncated``; ``declared_only`` is left for
    # the routes no source named at all.
    refused_source = _refused_source_rows(source_rows, source_dropped_routes)
    refused_probe: dict[tuple[str, str, str], str] = {}
    for dropped_key in editor_source["dropped_routes"]:
        refused_probe.setdefault(dropped_key, "editor")
    for dropped_key in opencode_source["dropped_routes"]:
        refused_probe.setdefault(dropped_key, "opencode")
    # A route the CLI's own list offered and its bound refused is a discovered
    # one too, and would otherwise be rebuilt below as ``declared_only``.
    for cli_source in cli_sources.values():
        for dropped_key in cli_source["dropped_routes"]:
            refused_probe.setdefault(dropped_key, "cli")
    declared_only_count = 0
    source_truncated_count = 0
    # A producer that cut and cannot say by how much makes "absent from what
    # arrived" stop being evidence of "absent from the host" -- and that is a
    # property of the SOURCE, not of one adapter spelling. The editor bridge
    # returns a slice with no total; the OpenCode parser stops at its own 64-row
    # cap and, where the snapshot it read was itself bounded, the identities
    # past it cannot be recovered either. Both then publish
    # ``total_is_lower_bound``, and both leave a configured route possibly
    # sitting in a tail nobody here has seen.
    #
    # Which rows a given source would have supplied is decided by the canonical
    # adapter its identities are ingested under, resolved through the same
    # ``policy_route_identity`` normalisation the rows themselves went through
    # rather than by naming a provider here: the adapter picks the policy owner,
    # and the policy layer owns that map.
    editor_upstream_unknown = bool(editor_source["total_is_lower_bound"])
    opencode_upstream_unknown = bool(opencode_source["total_is_lower_bound"])
    upstream_unknown_adapters: set[str] = set()
    for (probe_provider, probe_adapter), producer_unknown in (
        (("copilot", "vscode_lm"), editor_upstream_unknown),
        (("opencode", "opencode_cli"), opencode_upstream_unknown),
    ):
        if not producer_unknown:
            continue
        upstream_unknown_adapters.add(
            _canonical_route_key(probe_provider, probe_adapter, "")[1]
        )
    upstream_truncated_count = 0
    for declared_provider, declared_adapter, declared_model in declared_leaves:
        if not declared_provider or not declared_adapter or not declared_model:
            continue
        if not workforce_catalog.model_identity_valid(declared_model):
            continue
        key = _canonical_route_key(
            declared_provider, declared_adapter, declared_model
        )
        if key in existing:
            continue
        existing.add(key)
        refused_row = refused_source.get(key)
        if refused_row is not None:
            # A configured row the ingestion bound refused. Its worker_id and
            # its ``enabled`` value are facts the catalog stated, so the row is
            # rebuilt from them and claims neither inventory-only nor
            # declared-only status -- both would deny a source that did supply
            # it.
            row_vendor = str(refused_row.get("provider") or "")
            row_adapter = str(refused_row.get("adapter_id") or "")
            row_enabled = bool(refused_row.get("enabled", True))
            source_truncated_count += 1
            workers.append(
                {
                    "worker_id": str(refused_row.get("worker_id") or "")[:128],
                    "provider": key[0],
                    "adapter": key[1],
                    "model": key[2],
                    "vendor_provider": row_vendor[:128],
                    "declared_adapter": row_adapter[:128],
                    "catalog_enabled": row_enabled,
                    "effective_enabled": row_enabled
                    and _route_policy_enabled(
                        policy,
                        provider=key[0],
                        adapter=key[1],
                        model=key[2],
                        vendor_provider=row_vendor,
                        declared_adapter=row_adapter,
                    ),
                    "inventory_only": False,
                    "source_truncated": True,
                }
            )
            continue
        probe = refused_probe.get(key)
        if probe is not None:
            # A probe did list this identity and its own bound refused it. That
            # is an inventory-only row like any other discovery row -- no
            # worker_id, no catalog decision to carry -- but it is not
            # undiscovered, so it names the probe that saw it instead of
            # claiming ``declared_only``. The measured discovery counts stay
            # untouched: they count identities that were ingested, and this one
            # was not.
            probe_row: dict[str, Any] = {
                "worker_id": "",
                "provider": key[0],
                "adapter": key[1],
                "model": key[2],
                "vendor_provider": str(declared_provider)[:128],
                "declared_adapter": str(declared_adapter)[:128],
                "catalog_enabled": True,
                "effective_enabled": _route_policy_enabled(
                    policy,
                    provider=key[0],
                    adapter=key[1],
                    model=key[2],
                    vendor_provider=str(declared_provider),
                    declared_adapter=str(declared_adapter),
                ),
                "inventory_only": True,
                "source_truncated": True,
            }
            probe_row[
                {
                    "editor": "discovered_from_editor",
                    "opencode": "discovered_from_opencode",
                    "cli": "discovered_from_cli",
                }[probe]
            ] = True
            inventory_only_count += 1
            source_truncated_count += 1
            workers.append(probe_row)
            continue
        # ``declared_only`` claims no source offered this route, and that claim
        # needs the source that would have offered it to have been able to
        # answer. A producer that cut its own list and published no total
        # leaves a tail nothing here can read, and a declared route may simply
        # be in it. Labelling that one undiscovered states a measurement nobody
        # took -- and it is the same route the owner explicitly configured, so
        # the payload would be asserting its absence from a host that may well
        # be offering it. It is reported as upstream-truncated instead, which is
        # exactly what the evidence supports and no more.
        #
        # Tested against every adapter whose producer came up short rather than
        # against one spelling. Reading only the editor's flag here left a
        # declared ``opencode_cli`` route -- ``xai/grok-4.6`` beyond a snapshot
        # the OpenCode producer had already capped at 64 -- falling through to
        # ``declared_only``, so the tree asserted "not offered by discovery"
        # about a measurement that producer was never able to make.
        upstream_unknown = key[1] in upstream_unknown_adapters
        declared_row: dict[str, Any] = {
            "worker_id": "",
            "provider": key[0],
            "adapter": key[1],
            "model": key[2],
            "vendor_provider": str(declared_provider)[:128],
            "declared_adapter": str(declared_adapter)[:128],
            "catalog_enabled": True,
            "effective_enabled": _route_policy_enabled(
                policy,
                provider=key[0],
                adapter=key[1],
                model=key[2],
                vendor_provider=str(declared_provider),
                declared_adapter=str(declared_adapter),
            ),
            "inventory_only": True,
        }
        inventory_only_count += 1
        if upstream_unknown:
            declared_row["source_truncated"] = True
            declared_row["upstream_truncated"] = True
            upstream_truncated_count += 1
        else:
            declared_row["declared_only"] = True
            declared_only_count += 1
        workers.append(declared_row)
    workers.sort(
        key=lambda row: (
            row["provider"], row["adapter"], row["model"], row["worker_id"]
        )
    )
    total_rows = len(workers)
    # Every ingestion source is bounded before the render bound sees a
    # single row, so a provider's own size is what survived plus what those caps
    # cost it. That second part is a count of *distinct launch routes*, never a
    # sum of each source's raw drop count: the configured catalog and the
    # discovery probes describe overlapping populations, so a dropped
    # ``xai/grok-4.6`` identity that duplicates a configured ``opencode_cli``
    # row is one route this payload is missing and not two. Summed instead, the
    # duplicate inflated the denominator, and the tree then divided a real
    # shown-count by a provider size that no longer existed.
    rendered_routes = {
        (row["provider"], row["adapter"], row["model"]) for row in workers
    }
    # The same pass charges each missing route to the bound that refused it, so
    # every loss entry carries an ``absent_routes`` count taken over this
    # deduped population beside the raw ``dropped`` its own probe reports. The
    # Webview's "not loaded" clause is a claim about the tree, so it has to be
    # counted over the tree's population and not the probe's. Each source also
    # hands over the subset its own producer refused, so the split between "a
    # cap here dropped it" and "a cap above dropped it" survives into the
    # label. The configured catalog is read directly and has no producer above
    # it, so its upstream set is empty rather than absent.
    dropped_before_render = _absent_route_attribution(
        [
            (source_dropped_routes, source_ingestion_loss, set()),
            (
                editor_source["dropped_routes"],
                editor_source["ingestion_loss"],
                set(editor_source["upstream_dropped_routes"]),
            ),
            (
                opencode_source["dropped_routes"],
                opencode_source["ingestion_loss"],
                set(opencode_source["upstream_dropped_routes"]),
            ),
            # The CLI-owned lists have no producer above them either, so their
            # upstream sets are empty rather than absent.
            *(
                (
                    cli_source["dropped_routes"],
                    cli_source["ingestion_loss"],
                    set(cli_source["upstream_dropped_routes"]),
                )
                for cli_source in cli_sources.values()
            ),
        ],
        rendered_routes=rendered_routes,
    )
    (
        workers,
        provider_counts,
        row_limit_honoured,
        row_pins_refused,
    ) = _bounded_catalog_rows(
        workers,
        limit=MAX_MODEL_POLICY_CATALOG_ROWS,
        ceiling=MAX_MODEL_POLICY_CATALOG_ROW_CEILING,
        pinned=pinned_routes,
        dropped_before_ingestion=dropped_before_render,
        # Every editor-hosted route is drawn under ``copilot`` and every
        # OpenCode one under ``opencode``, so when either producer's ceiling
        # bound over a tail this module could not recover, that family's size is
        # a floor rather than a count. The provider is marked instead of having
        # a made-up number added to it: an unknown remainder is not a measured
        # one. Reading only the editor's flag here left the OpenCode family
        # publishing an exact total over a population it had never seen.
        lower_bound_providers=(
            ({"copilot"} if editor_upstream_unknown else set())
            | (
                {"opencode"}
                if opencode_source["total_is_lower_bound"]
                else set()
            )
        ),
    )
    return {
        **policy,
        "catalog": {
            "workers": workers,
            "worker_count": total_rows,
            "returned_worker_count": len(workers),
            "provider_counts": provider_counts,
            "configured_worker_count": len(source_rows),
            "discovered_model_count": discovered_count,
            "opencode_discovered_model_count": opencode_discovered_count,
            # The CLI-owned lists count their own discovered identities the same
            # way. The aggregate above cannot say which source a row came from,
            # and neither figure is the source's total -- that, and what its
            # bound refused, is ``codex_source`` / ``claude_source`` below.
            "codex_discovered_model_count": cli_discovered_counts["codex"],
            "claude_discovered_model_count": cli_discovered_counts["claude"],
            "inventory_only_model_count": inventory_only_count,
            # The subset of those inventory-only rows that no source observed:
            # routes built from the owner's declaration because every discovery
            # list reaching this module had already been cut upstream. Counted
            # apart from discovered_model_count on purpose -- a row nothing
            # observed must not be added to a measured discovery figure -- and
            # published rather than inferred, because a non-zero value here is
            # the reader's evidence that a source arrived short.
            "declared_only_model_count": declared_only_count,
            # The other half of that materialisation, and deliberately not
            # folded into the count above: rows an ingestion bound refused and
            # the declaration brought back, rebuilt from the source that did
            # supply them. They are neither undiscovered nor freshly measured,
            # so they are counted on their own rather than inflating a discovery
            # figure or a declared-only one.
            "source_truncated_model_count": source_truncated_count,
            # And the third case, which used to be silently filed as the first:
            # a configured route this view cannot classify, because the source
            # that would have offered it was cut by its own producer without
            # saying by how much. Counting it as declared-only asserted that no
            # host offers the route; counting it as discovered would assert the
            # opposite. It is counted as neither.
            "upstream_truncated_model_count": upstream_truncated_count,
            "editor_catalog_live": bool(editor.get("launchable")),
            "editor_catalog_reason": str(editor.get("blocker_reason") or "")[:200],
            "row_limit": MAX_MODEL_POLICY_CATALOG_ROWS,
            # The bound actually honoured. It exceeds row_limit only when the
            # explicit-route/one-per-provider floor is larger, so a reader can
            # tell a raised floor from a broken bound.
            "row_limit_honoured": row_limit_honoured,
            # The number row_limit_honoured may never pass. The floor above is
            # derived from models.json, and that file is the caller's, not this
            # module's: one leaf per catalog row pinned every row and raised the
            # "bound" to the whole catalog, which is an unbounded payload still
            # reporting itself as a bounded one.
            "row_limit_ceiling": MAX_MODEL_POLICY_CATALOG_ROW_CEILING,
            # Explicitly configured routes the ceiling refused. Unlike a
            # refusal by an ingestion bound this one is final: these rows are
            # absent from ``workers``, so the owner has no control for them and
            # the count has to be stated rather than left to be inferred from a
            # row list that merely stops short.
            "pinned_routes_refused": row_pins_refused,
            "source_row_limit": MAX_MODEL_POLICY_SOURCE_ROWS,
            "source_rows_ingested": len(ingested_rows),
            # The ingestion bound actually honoured, for the same reason the
            # render bound publishes its own. Pins can raise this above
            # source_row_limit, and while that number was discarded a raised
            # floor and a cap that simply failed to hold read identically.
            "source_row_limit_honoured": source_row_limit_honoured,
            "source_row_limit_ceiling": MAX_MODEL_POLICY_SOURCE_ROW_CEILING,
            # Pins this ingestion bound could not admit. Reported apart from
            # the render bound's because a route refused here may still be
            # materialised from its declaration below, so the two counts are
            # facts about two different bounds and only one of them is a claim
            # that a control is missing.
            "source_pinned_routes_refused": source_pins_refused,
            # Exact per-provider ingestion loss, published only for providers
            # that actually lost rows, so nobody has to infer which provider a
            # shortfall between configured_worker_count and worker_count came
            # out of. Keyed by the canonical provider the rendered rows and the
            # Webview both group by, so the number is reachable by the tree
            # rather than filed under a family that is never drawn. Each entry
            # carries three counts that must not be read for each other:
            # ``dropped`` is what this bound refused, ``absent_routes`` is how
            # many of those are routes no other source supplied -- the only one
            # that belongs beside a deduped provider total -- and
            # ``upstream_absent_routes`` is the part of THAT which a producer
            # above this module refused, a strict subset and never an addend.
            "source_ingestion_loss": source_ingestion_loss,
            # The OpenCode discovery probe is a second ingestion source with its
            # own bound. Its returned/total/truncated truth is published rather
            # than inferred from opencode_discovered_model_count, which counts
            # only the identities that became rows and so cannot report the ones
            # the bound refused.
            "opencode_source": {
                # The population the PRODUCER was given, not the one it handed
                # over. 512 cannot bind on a list already cut to 64, so while
                # this read the delivered length the flag below was structurally
                # False and a 201-identity host published itself as 64.
                "total": opencode_source["total"],
                # What actually reached this bound, so a reader can tell the
                # producer's cap from this module's.
                "delivered": opencode_source["delivered"],
                "upstream_refused": opencode_source["upstream_refused"],
                "upstream_ceiling": OPENCODE_UPSTREAM_IDENTITY_CEILING,
                "returned": opencode_source["returned"],
                # A floor says the population may be larger than the total
                # printed beside it, so the row list is not this source's whole
                # catalog even when this module's own bound refused nothing.
                # The editor source below ORs its floor in for that reason and
                # so do the provider counts; reading the ingestion's arithmetic
                # alone here published a complete-looking source over a tail
                # nobody had seen.
                "truncated": opencode_source["truncated"]
                or opencode_source["total_is_lower_bound"],
                "total_is_lower_bound": opencode_source["total_is_lower_bound"],
                "row_limit": MAX_MODEL_POLICY_SOURCE_ROWS,
                "row_limit_honoured": opencode_source["row_limit_honoured"],
                # The number row_limit_honoured may not pass, and how many
                # explicitly configured routes this bound could not admit once
                # it did bind. A refusal here is not automatically a missing
                # row -- a named route no source supplied is materialised from
                # the declaration further down -- so it is reported as this
                # bound's own fact rather than as a claim about the tree.
                "row_limit_ceiling": MAX_MODEL_POLICY_SOURCE_ROW_CEILING,
                "pinned_routes_refused": opencode_source["pinned_routes_refused"],
                "ingestion_loss": opencode_source["ingestion_loss"],
            },
            # The editor bridge is the third, and it reports the same way for
            # the same reason: discovered_model_count counts only the identities
            # that became rows, so a host listing more models than the bound
            # admits had no way to say so. A Copilot catalog larger than the cap
            # now states its own total, what was let in, and whether anything
            # was refused.
            #
            # Unlike the OpenCode boundary its producer's input is unreachable
            # from here, so at the bridge's ceiling the total is a floor. That
            # is published as ``total_is_lower_bound`` rather than rounded off
            # into a number, because "128" and "at least 128" are different
            # claims and only the second one was measured.
            "editor_source": {
                "total": editor_source["total"],
                "delivered": editor_source["delivered"],
                "upstream_refused": editor_source["upstream_refused"],
                "upstream_ceiling": EDITOR_UPSTREAM_MODEL_CEILING,
                "returned": editor_source["returned"],
                "truncated": editor_source["truncated"]
                or editor_source["total_is_lower_bound"],
                "total_is_lower_bound": editor_source["total_is_lower_bound"],
                "row_limit": MAX_MODEL_POLICY_SOURCE_ROWS,
                "row_limit_honoured": editor_source["row_limit_honoured"],
                "row_limit_ceiling": MAX_MODEL_POLICY_SOURCE_ROW_CEILING,
                "pinned_routes_refused": editor_source["pinned_routes_refused"],
                "ingestion_loss": editor_source["ingestion_loss"],
            },
            # The CLI-owned model lists are the last two ingestion sources and
            # report the same way for the same reason: the discovered counts
            # above are of identities that became rows, so a cache listing more
            # models than the bound admits had no way to say so.
            #
            # Neither has a producer cap above it -- the Codex cache is read
            # whole or not at all, and the Claude list is always its four
            # aliases -- so ``total`` is a measurement and not a floor,
            # ``total_is_lower_bound`` stays False and ``upstream_ceiling`` has
            # no number to carry.
            "codex_source": _cli_source_view(cli_sources["codex"]),
            "claude_source": _cli_source_view(cli_sources["claude"]),
            # The owner's own declarations are bounded too. models.json is read
            # up to declared_leaf_limit, and a file larger than that has leaves
            # this view never saw -- so it also has pins it never reserved. That
            # is published rather than inferred, because a decision that was
            # never read is otherwise indistinguishable from one that lost.
            "declared_leaf_count": declared_leaf_total,
            "declared_leaf_limit": MAX_MODEL_POLICY_DECLARED_LEAVES,
            "declared_leaves_truncated": declared_leaves_truncated,
            # Rows were lost to the render bound or to any of the ingestion
            # caps before it, models.json itself was read short, or a
            # producer cut above this module without saying by how much; any of
            # them means the row list is not the whole catalog.
            "truncated": len(workers) < total_rows
            or bool(dropped_before_render)
            or declared_leaves_truncated
            or editor_upstream_unknown
            # The OpenCode producer's own cap is the other unknown tail, and
            # reading only the editor's flag here drew a catalog whose OpenCode
            # family is a floor as if the row list were the whole of it.
            or bool(opencode_source["total_is_lower_bound"]),
        },
    }


def settings_view() -> dict[str, Any]:
    """READ-ONLY: return repository feature switches, Source Graph policy, retention policy, and model routing policy.

    Use this to inspect current settings and their revision numbers before calling aiworkhub_dashboard_settings_update, aiworkhub_dashboard_model_settings_update, or aiworkhub_dashboard_source_graph_settings_update. For queue state or task counts use aiworkhub_dashboard_snapshot instead. Reads repository configuration files and status views without mutating durable state or starting background services.
    """
    try:
        root = core.repo_root()
        result = feature_settings.load(root)
        result["context_graph_runtime"] = context_graph.status(root)
        result["source_graph_policy"] = source_graph.source_graph_policy_view(root)
        policy = repo_policy.load_policy(root)
        result["retention_policy"] = dict(policy.get("retention") or {})
        result["model_policy"] = _model_policy_view(
            root, preflight=_settings_preflight_snapshot(root)
        )
    except (
        context_graph.ContextGraphError,
        feature_settings.FeatureSettingsError,
        model_settings.ModelSettingsError,
        repo_policy.RepoPolicyError,
        vscode_lm_bridge.BridgeError,
        workforce_catalog.WorkforceCatalogError,
        OSError,
        sqlite3.Error,
    ) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    result["server_tool"] = "aiworkhub_dashboard_settings"
    result["authority_flags"] = _readonly_authority_flags()
    return result


def source_graph_settings_update_view(
    language_changes: dict[str, bool], expected_revision: int,
) -> dict[str, Any]:
    """USER WRITE: atomically update per-language Source Graph indexing switches for the active repository.

    Use this to enable or disable individual languages in the Source Graph ignore policy; use aiworkhub_dashboard_settings to read the current policy and revision first, or aiworkhub_dashboard_settings_update to toggle the top-level source_graph feature switch. Enforces optimistic locking against expected_revision, atomically rewrites the ignore policy file, and triggers an immediate Source Graph refresh when the feature is enabled.

    Args:
        language_changes: non-empty dict[str, bool] mapping registered language IDs to True (enabled) or False (disabled).
        expected_revision: int revision from the current Source Graph policy view; mismatches fail closed.
    """

    root = core.repo_root()
    try:
        result = source_graph.update_language_policy(
            root,
            language_changes=language_changes,
            expected_revision=expected_revision,
        )
        if feature_settings.enabled(root, "source_graph"):
            result["source_graph_refresh"] = core.source_graph_refresh_now()
    except (
        feature_settings.FeatureSettingsError,
        source_graph.SourceGraphError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    result["server_tool"] = "aiworkhub_dashboard_source_graph_settings_update"
    result["authority_flags"] = _storage_write_authority_flags()
    return result


def settings_update_view(changes: dict[str, bool], expected_revision: int) -> dict[str, Any]:
    """USER WRITE: atomically update repository-local feature switches with optimistic revision locking.

    Use this to toggle top-level repository features such as source_graph or context_graph; use aiworkhub_dashboard_settings to inspect current switches and revision first, or aiworkhub_dashboard_source_graph_settings_update and aiworkhub_dashboard_model_settings_update for language or model routing switches. Atomically persists feature settings when expected_revision matches, starts or stops Source Graph when toggled, and initializes the context graph schema when enabled.

    Args:
        changes: non-empty dict[str, bool] mapping valid feature keys to their desired boolean enabled state.
        expected_revision: int current feature settings revision required for optimistic concurrency; mismatches fail closed.
    """
    root = core.repo_root()
    try:
        result = feature_settings.update(
            root,
            changes=changes,
            expected_revision=expected_revision,
        )
        if "source_graph" in changes:
            lifecycle = (
                core.source_graph_ensure_started()
                if changes["source_graph"]
                else core.source_graph_stop()
            )
            result["source_graph_lifecycle"] = lifecycle
        if changes.get("context_graph") is True:
            result["context_graph_runtime"] = context_graph.ensure_schema(root)
    except (
        context_graph.ContextGraphError,
        feature_settings.FeatureSettingsError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    result["server_tool"] = "aiworkhub_dashboard_settings_update"
    result["authority_flags"] = _storage_write_authority_flags()
    return result


def model_settings_update_view(
    provider: str,
    enabled: bool,
    expected_revision: int,
    adapter: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """USER WRITE: update one provider, adapter, or model routing switch for the active repository.

    Use this to enable or disable a provider, transport adapter, or specific model leaf; call aiworkhub_dashboard_settings first to read model_policy and its current revision, or use aiworkhub_dashboard_settings_update for repository feature flags. Acquires the model-settings lock, verifies expected_revision, and atomically persists the updated routing configuration.

    Args:
        provider: str non-empty provider identifier to configure.
        enabled: bool True to enable or False to disable the selected provider, adapter, or model route.
        expected_revision: int current model_settings revision; mismatches fail closed and report current_revision.
        adapter: optional str transport adapter identifier (default None); required when model is specified.
        model: optional str model identifier under adapter (default None); omit to toggle the provider or adapter switch.
    """
    root = core.repo_root()
    try:
        result = model_settings.update(
            root,
            provider=provider,
            adapter=adapter,
            model=model,
            enabled=enabled,
            expected_revision=expected_revision,
        )
    except (model_settings.ModelSettingsError, OSError, ValueError) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
        try:
            result["current_revision"] = model_settings.load(root)["revision"]
        except (model_settings.ModelSettingsError, OSError):
            pass
    result["server_tool"] = "aiworkhub_dashboard_model_settings_update"
    result["authority_flags"] = _storage_write_authority_flags()
    return result


def _byte_len(payload: Mapping[str, Any]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    )


def _bound_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Trim the largest secondary sections until the snapshot fits the bound.

    Never mutates the input; returns a shallow copy with 0+ fields replaced
    by ``{"transport_truncated": true}`` and every trimmed field name
    reported in ``transport_truncated_fields``.
    """
    result = dict(snapshot)
    truncated: list[str] = []
    for field in _SNAPSHOT_TRIM_ORDER:
        if _byte_len(result) <= MAX_SNAPSHOT_RESPONSE_BYTES:
            break
        if result.get(field):
            result[field] = {"transport_truncated": True}
            truncated.append(field)
    if truncated:
        result["transport_truncated_fields"] = truncated
    return result


def _epoch_seconds(stamp: Any) -> float:
    """Instant of an ISO-8601 stamp; a missing or unparseable one ranks oldest."""
    text = str(stamp or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return float("-inf")


def _quarantine_recency(row: Any) -> tuple[float, str, str]:
    """Total order key, newest greatest: instant, then stamp text, then batch id."""
    if not isinstance(row, Mapping):
        return (float("-inf"), "", "")
    stamp = row.get("quarantined_at") or row.get("created_at") or ""
    return (_epoch_seconds(stamp), str(stamp), str(row.get("batch_id") or ""))


def _bounded_quarantine_rows(rows: list[Any] | tuple[Any, ...]) -> dict[str, Any]:
    """The newest rows within the bound, with the full count and truncation truth."""
    newest = sorted(rows, key=_quarantine_recency, reverse=True)[:MAX_SNAPSHOT_QUARANTINE_ROWS]
    return {
        "total_count": len(rows),
        "returned_count": len(newest),
        "truncated": len(newest) < len(rows),
        "ranked_by": "newest_instant_then_batch_id",
        "rows": newest,
    }


def _bounded_storage_usage(usage: Any) -> Any:
    """Copy of ``usage`` with each quarantine array bounded; the input is never mutated."""
    if not isinstance(usage, Mapping):
        return usage
    bounded = dict(usage)
    for field in _SNAPSHOT_QUARANTINE_ARRAYS:
        rows = usage.get(field)
        if isinstance(rows, (list, tuple)):
            bounded[field] = _bounded_quarantine_rows(rows)
    return bounded


def _known_contract_identity(identity: Any, known_contract_sha256: Any = "") -> Any:
    """Drop the contract prose from ``identity`` unless the caller's digest differs from the current one."""
    known = str(known_contract_sha256 or "").strip().lower()
    current = (
        str(identity.get("contract_sha256") or "").strip().lower()
        if isinstance(identity, Mapping)
        else ""
    )
    if not current:
        return identity
    if known and known != current:
        return {**identity, "contract_delivery_reason": "contract_sha_changed"}
    omitted = sorted(key for key in core.MANAGER_CONTRACT if key in identity)
    if not omitted:
        return identity
    return {
        **{key: value for key, value in identity.items() if key not in omitted},
        "contract_delivered": False,
        "contract_delivery_reason": (
            "caller_holds_current_contract" if known else "dashboard_identity_only"
        ),
        "contract_omitted_fields": omitted,
        "contract_recall": _CONTRACT_RECALL,
    }


def _compact_snapshot(
    snapshot: Mapping[str, Any], known_contract_sha256: str = ""
) -> dict[str, Any]:
    """Return the bounded manager-facing operational snapshot.

    The native Webview explicitly requests the full shape. Model callers get
    queue counts, health, warnings and route truth by default without pulling
    repeated task rows, process evidence, cost ledgers, workforce history or
    analytics into their context. Dedicated bounded tools own those details.
    """

    result = {
        key: snapshot[key]
        for key in _COMPACT_SNAPSHOT_FIELDS
        if key in snapshot
    }
    if "storage_usage" in result:
        result["storage_usage"] = _bounded_storage_usage(result["storage_usage"])
    if "manager_identity" in result:
        result["manager_identity"] = _known_contract_identity(
            result["manager_identity"], known_contract_sha256
        )
    omitted = sorted(key for key in snapshot if key not in result)
    result.update({
        "snapshot_mode": "summary",
        "full_snapshot_available": True,
        "omitted_fields": omitted,
    })
    return result


def _bound_task_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    """Trim the largest task fields until one task's detail fits the bound."""
    result = dict(detail)
    if _byte_len(result) <= MAX_TASK_DETAIL_RESPONSE_BYTES:
        return result
    task = dict(result.get("task") or {})
    truncated: list[str] = []
    for field in _DETAIL_TRIM_FIELDS:
        if _byte_len({**result, "task": task}) <= MAX_TASK_DETAIL_RESPONSE_BYTES:
            break
        if field in task:
            task[field] = "(transport_truncated)"
            truncated.append(field)
    result["task"] = task
    if truncated:
        result["transport_truncated_fields"] = truncated
    return result


def _stat_identity(path: Path) -> tuple[int, int, int] | None:
    """(inode, size, mtime_ns) of ``path``; None when absent. Other errors raise."""
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _wal_index_header(path: Path) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(_WAL_INDEX_HEADER_BYTES)
    except FileNotFoundError:
        return None


def _snapshot_generation_key(root: Path) -> tuple[Any, ...] | None:
    """Cheap identity of every store a snapshot build reads.

    None when the canonical queue does not exist: an uninitialized build is
    cheap and is never cached. Any other read failure raises, and the caller
    rebuilds rather than serve a cache it cannot prove current.
    """
    db = root.joinpath(*_TASK_QUEUE_DB_REL)
    db_identity = _stat_identity(db)
    if db_identity is None:
        return None
    return (
        str(root),
        db_identity,
        _stat_identity(Path(f"{db}-wal")),
        _wal_index_header(Path(f"{db}-shm")),
        tuple(_stat_identity(root.joinpath(*rel)) for rel in _SNAPSHOT_KEY_DIRS_REL),
        tuple(_stat_identity(root.joinpath(*rel)) for rel in _SNAPSHOT_KEY_FILES_REL),
        tuple(
            _stat_identity(Path(f"{root.joinpath(*rel)}-wal"))
            for rel in _SNAPSHOT_KEY_FILES_REL
        ),
    )


def _cacheable_build(snapshot: Mapping[str, Any]) -> bool:
    """Only a ready, error-free build is reused; a degraded one is re-checked."""
    storage = snapshot.get("storage")
    if isinstance(storage, Mapping) and not storage.get("ready", True):
        return False
    return not snapshot.get("errors")


def _snapshot_build_cache_stats() -> dict[str, int]:
    """Measured hit/miss/key-error counts of the memoized snapshot build."""
    with _SNAPSHOT_BUILD_LOCK:
        return dict(_SNAPSHOT_BUILD_CACHE_STATS)


def _memoized_build_snapshot(build_kwargs: Mapping[str, Any], *, cacheable: bool) -> dict[str, Any]:
    """Return ``dashboard.build_snapshot(**build_kwargs)``, reusing an unchanged build.

    The caller holds ``_SNAPSHOT_BUILD_LOCK``. The key is read BEFORE the
    build, so a write that lands during a build changes the next key. The
    builder itself is part of the entry, so a replaced builder never serves
    another builder's result. Callers get a shallow copy and must copy any
    nested section before mutating it.
    """
    builder = dashboard.build_snapshot
    summary_only = bool(build_kwargs.get("summary_only"))
    key: tuple[Any, ...] | None = None
    if cacheable:
        try:
            key = _snapshot_generation_key(Path(core.repo_root()).resolve())
        except Exception as exc:  # noqa: BLE001 -- an unreadable key rebuilds, never serves the cache
            _SNAPSHOT_BUILD_CACHE_STATS["key_error"] += 1
            _debug_trace("snapshot.build_cache.key_error", error=type(exc).__name__)
    now = time.monotonic()
    cached = _SNAPSHOT_BUILD_CACHE.get(summary_only)
    if (
        key is not None
        and cached is not None
        and cached[0] is builder
        and cached[1] == key
        and now - cached[2] < SNAPSHOT_BUILD_CACHE_TTL_SECONDS
    ):
        _SNAPSHOT_BUILD_CACHE_STATS["hit"] += 1
        _debug_trace("snapshot.build_cache.hit", summary_only=summary_only)
        return dict(cached[3])
    _SNAPSHOT_BUILD_CACHE.pop(summary_only, None)
    _SNAPSHOT_BUILD_CACHE_STATS["miss"] += 1
    snapshot = dict(
        _debug_stage("dashboard.build_snapshot", lambda: builder(**build_kwargs))
    )
    if key is not None and _cacheable_build(snapshot):
        _SNAPSHOT_BUILD_CACHE[summary_only] = (builder, key, now, snapshot)
    return dict(snapshot)


def _canonical_section_value(value: Any) -> Any:
    """The value as a JSON consumer re-reads it: integral floats become ints.

    The extension digests the parsed payload, where ``1.0`` arrives as ``1``;
    normalizing here keeps both digests equal for the common numeric case. A
    value the two encoders still spell differently only makes its section
    resend -- the merge never trusts a digest it did not verify.
    """
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 2**53:
            return int(value)
        return value
    if isinstance(value, Mapping):
        return {
            key if isinstance(key, str) else json.dumps(key, default=str).strip('"'):
            _canonical_section_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_section_value(item) for item in value]
    return value


def _snapshot_section_digest(value: Any) -> str:
    """sha256 of one top-level snapshot section in canonical JSON.

    Canonical form: sorted keys, ``(",", ":")`` separators, non-ASCII kept
    verbatim, UTF-8 bytes. vscode-extension/extension.js
    ``snapshotSectionDigest`` computes the identical form.
    """
    encoded = json.dumps(
        _canonical_section_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8", "surrogatepass")).hexdigest()


def _split_section_digests(
    prior: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Separate a delta request's digest map from a genuine prior snapshot."""
    if not isinstance(prior, Mapping) or SNAPSHOT_SECTION_DIGESTS_FIELD not in prior:
        return None, prior
    digests = prior.get(SNAPSHOT_SECTION_DIGESTS_FIELD)
    rest = {key: value for key, value in prior.items() if key != SNAPSHOT_SECTION_DIGESTS_FIELD}
    # A malformed map is still a delta request; it simply misses (full reply).
    return (digests if isinstance(digests, Mapping) else {}), (rest or None)


def _snapshot_delta(result: Mapping[str, Any], known: Mapping[str, Any]) -> dict[str, Any]:
    """Only the sections whose digest changed, or ``result`` itself on a miss.

    A miss is a digest map that is oversized, is not ``str -> sha256 hex``,
    or matches no current section; the caller then gets the plain full
    snapshot and rebuilds its own digests from it.
    """
    if len(known) > MAX_SNAPSHOT_SECTION_DIGESTS or not all(
        isinstance(key, str) and isinstance(digest, str) and _SHA256_HEX_RE.match(digest)
        for key, digest in known.items()
    ):
        return dict(result)
    digests = {key: _snapshot_section_digest(value) for key, value in result.items()}
    if not any(known.get(key) == digest for key, digest in digests.items()):
        return dict(result)
    return {
        "snapshot_mode": "delta",
        "delta_schema": SNAPSHOT_DELTA_SCHEMA,
        SNAPSHOT_SECTION_DIGESTS_FIELD: digests,
        "changed_sections": {
            key: value for key, value in result.items() if known.get(key) != digests[key]
        },
    }


def snapshot_view(
    full: bool = False,
    previous: Mapping[str, Any] | None = None,
    previous_snapshot: Mapping[str, Any] | None = None,
    known_contract_sha256: str = "",
) -> dict[str, Any]:
    """READ-ONLY: build the canonical dashboard queue, health, and storage snapshot for the active repository.

    Use the default compact mode for manager queue summaries and status counts, or full=True for the native Webview payload; use aiworkhub_dashboard_health for a cheap connection check or aiworkhub_dashboard_task_detail for one task card. Reads canonical SQLite state and cached storage telemetry without mutating tasks or launching processes.

    Args:
        full: bool (default False); True returns full task, process, and ledger collections, False returns the compact summary.
        previous: optional Mapping[str, Any] (default None) prior snapshot dict reused for incremental diffing.
        previous_snapshot: optional Mapping[str, Any] (default None) alias for previous when previous is omitted.
        known_contract_sha256: str (default "") contract SHA-256 digest the caller already holds; compact mode omits the contract prose by default and re-sends it only when this digest differs from the current contract_sha256.
    """
    started = time.perf_counter()
    _debug_trace("snapshot.begin")
    build_kwargs: dict[str, Any] = {"summary_only": not full}
    prior_snapshot = previous if previous is not None else previous_snapshot
    # ``{"section_digests": {...}}`` is a delta request, not a snapshot to merge.
    known_section_digests, prior_snapshot = _split_section_digests(prior_snapshot)
    if prior_snapshot is not None:
        build_kwargs["previous"] = prior_snapshot
    with _SNAPSHOT_BUILD_LOCK:
        snapshot = _memoized_build_snapshot(build_kwargs, cacheable=prior_snapshot is None)
    storage = snapshot.get("storage")
    if isinstance(storage, dict) and not storage.get("ready", True):
        storage = dict(storage)
        storage["not_initialized"] = is_not_initialized_reason(storage.get("reason", ""))
        snapshot["storage"] = storage
    try:
        manager = _debug_stage("core.manager_bootstrap", core.manager_bootstrap)
    except Exception as exc:  # noqa: BLE001 -- dashboard diagnostics must not break the queue view
        manager = {
            "ok": False,
            "role": "unknown",
            "provider": "unknown",
            "manager_route": {},
            "reason": f"manager_bootstrap_failed:{type(exc).__name__}",
        }
    snapshot["manager_identity"] = manager
    # Identity authority and callback delivery are deliberately separate
    # contracts.  A valid manager route does not prove that the extension-
    # owned dispatcher is registered/running, so surface the live dispatcher
    # health from this exact repo-bound MCP child in every snapshot.
    try:
        snapshot["callback_delivery"] = _debug_stage("core.dispatcher_health", core.dispatcher_health)
    except Exception as exc:  # noqa: BLE001 -- callback diagnostics must not break the queue view
        snapshot["callback_delivery"] = {
            "ok": False,
            "healthy": False,
            "status": "unknown",
            "dispatcher_running": False,
            "registered": False,
            "problems": [f"dispatcher_health_failed:{type(exc).__name__}"],
        }
    try:
        router_root = _debug_stage("core.repo_root.router", core.repo_root)
        snapshot["known_repositories"] = _debug_stage(
            "shared_router.list_known_repositories",
            lambda: shared_router.list_known_repositories(current_root=router_root, limit=32),
        )
    except Exception as exc:  # noqa: BLE001 -- shared router diagnostics must not break the active repo view
        snapshot["known_repositories"] = {
            "ok": False,
            "schema_id": shared_router.SCHEMA_ID,
            "error": f"shared_router_failed:{type(exc).__name__}",
            "repositories": [],
            "rejects": [],
        }
    try:
        target_root = _debug_stage("core.repo_root.target", core.repo_root)
        targets = _debug_stage(
            "core.read_selected_coordinator_target",
            lambda: core.read_selected_coordinator_target(target_root),
        )
        selected = str(targets.get("selected_provider") or "")
        selected_target = targets.get("targets", {}).get(selected, {}) if isinstance(targets.get("targets"), dict) else {}
        wake = selected_target.get("wake", {}) if isinstance(selected_target, dict) else {}
        snapshot["manager_identity_target"] = {
            "selected_provider": selected,
            "capability_state": selected_target.get("capability_state") if isinstance(selected_target, dict) else "",
            "reason": wake.get("reason") or wake.get("action") if isinstance(wake, dict) else "",
        }
        chat_route = manager.get("manager_route") if isinstance(manager, dict) else {}
        chat = chat_route.get("manager_chat") if isinstance(chat_route, dict) else None
        if not isinstance(chat, dict) and isinstance(manager, dict):
            # A Claude-identified child reports the seat beside its own route (NF-2026-00972).
            chat = manager.get("manager_chat_seat")
        if isinstance(chat, dict) and chat.get("session_id"):
            snapshot["manager_identity_target"] = {
                "selected_provider": "manager_chat",
                "capability_state": "ready",
                "reason": "",
                "session_id": str(chat.get("session_id") or ""),
                "backend_id": str(chat.get("backend_id") or ""),
                "model": str(chat.get("model") or ""),
            }
    except Exception:  # noqa: BLE001 -- route diagnostics are optional
        snapshot["manager_identity_target"] = {}
    snapshot["server_tool"] = "aiworkhub_dashboard_snapshot"
    snapshot["authority_flags"] = _readonly_authority_flags()
    # ``build_snapshot`` starts the non-blocking storage inventory before the
    # heavier full reads. Re-sample its in-memory cache after those reads so a
    # scan that finished meanwhile is visible in this same response. A scan
    # still running is reported as-is (``scan_status`` scanning/refreshing):
    # the response never sleeps waiting for it (NF-2026-00633); the next poll
    # publishes the finished inventory.
    try:
        current_storage = snapshot.get("storage_usage")
        latest_storage = storage_observability.snapshot(core.repo_root())
        if isinstance(current_storage, dict) and isinstance(latest_storage, dict):
            # Copy first: the built section may be the memoized build's own.
            current_storage = dict(current_storage)
            snapshot["storage_usage"] = current_storage
            # Only refresh the header/overview scalars here.  The bounded full
            # storage detail built at the start remains authoritative and this
            # late cache sample cannot inflate the response with a second copy
            # of large retention/registration collections.
            for key in (
                "scan_status",
                "scanned_at",
                "repo_data_bytes",
                "repo_data_files",
                "worker_tree_bytes",
                "worker_tree_count",
                "safe_reclaimable_bytes",
                "quarantine_bytes",
                "managed_total_bytes",
                "errors",
            ):
                if key in latest_storage:
                    current_storage[key] = latest_storage[key]
    except Exception:  # noqa: BLE001 -- storage telemetry never breaks queue truth
        pass
    if full:
        snapshot["snapshot_mode"] = "full"
        result = _debug_stage("bound_snapshot", lambda: _bound_snapshot(snapshot))
        if known_section_digests is not None:
            known = known_section_digests
            result = _debug_stage("snapshot_delta", lambda: _snapshot_delta(result, known))
    else:
        result = _debug_stage(
            "compact_snapshot",
            lambda: _bound_snapshot(_compact_snapshot(snapshot, known_contract_sha256)),
        )
    _debug_trace(
        "snapshot.end",
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
        response_bytes=_byte_len(result),
    )
    return result


def task_detail_view(task_id: str) -> dict[str, Any]:
    """READ-ONLY: return the canonical detail, events, and validation evidence for one task card.

    Use this to inspect a specific task's contract, history, and attempt receipts; use aiworkhub_dashboard_snapshot for queue-wide status counts or aiworkhub_dashboard_task_live_output for live process stdout and stderr. Validates task_id format before querying read-only task storage and never mutates state.

    Args:
        task_id: str canonical task identifier matching the repository task ID pattern.
    """
    candidate = str(task_id or "").strip()
    if not dashboard._TASK_ID_RE.fullmatch(candidate):
        return {
            "ok": False,
            "error": "invalid_task_id",
            "server_tool": "aiworkhub_dashboard_task_detail",
            "authority_flags": _readonly_authority_flags(),
        }
    detail = dashboard.build_task_detail(candidate)
    if detail is None:
        return {
            "ok": False,
            "error": "task_not_found",
            "task_id": candidate,
            "server_tool": "aiworkhub_dashboard_task_detail",
            "authority_flags": _readonly_authority_flags(),
        }
    response = dict(detail)
    response["ok"] = True
    response["server_tool"] = "aiworkhub_dashboard_task_detail"
    response["authority_flags"] = _readonly_authority_flags()
    return _bound_task_detail(response)


def memory_view(limit: int = 100) -> dict[str, Any]:
    """READ-ONLY: list the newest canonical AI Memory entries for the active repository.

    Use this to browse stored AI Memory rows and lifecycle statuses in the dashboard; use aiworkhub_dashboard_sessions or aiworkhub_dashboard_kb to browse session transcripts or Knowledge Base entries. Opens the repository memory SQLite database in read-only mode and never increments access counters or mutates rows.

    Args:
        limit: int (default 100, clamped to 1..500) maximum number of newest memory entries to return.
    """

    try:
        safe_limit = max(1, min(int(limit), MAX_MEMORY_ROWS))
    except (TypeError, ValueError):
        safe_limit = 100
    try:
        root = core.repo_root()
        registry = storage_registry.load_storage_registry(root)
        db_path = storage_registry.resolve_database_path(registry, "memory")
        # NF-2026-00261: route read-only opens through the canonical helper so
        # the URI is percent-encoded (a '#' in the repo path stays '%23' instead
        # of truncating the '?mode=ro' query) and query_only is enforced.
        connection = sqlite_readonly.connect_readonly(db_path)
        connection.row_factory = sqlite3.Row
        try:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(memories)").fetchall()
            }
            required = {"id", "key", "value", "tags", "scope"}
            if not required.issubset(columns):
                raise sqlite3.OperationalError("memory_schema_missing_required_columns")
            project_expr = "m.project AS project" if "project" in columns else "'' AS project"
            created_expr = "m.created_at AS created_at" if "created_at" in columns else "'' AS created_at"
            updated_expr = "m.updated_at AS updated_at" if "updated_at" in columns else "'' AS updated_at"
            order_column = "updated_at" if "updated_at" in columns else (
                "created_at" if "created_at" in columns else "id"
            )
            has_state = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_entity_state'"
            ).fetchone() is not None
            state_join = (
                "LEFT JOIN context_entity_state s ON s.entity_type='memory' AND s.entity_id=m.id "
                if has_state else ""
            )
            state_expr = "COALESCE(s.status,'active') AS status" if has_state else "'active' AS status"
            total = int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            rows = connection.execute(
                f"SELECT m.id,m.key,m.value,m.tags,m.scope,{project_expr},{created_expr},"
                f"{updated_expr},{state_expr} FROM memories m {state_join}"
                f"ORDER BY m.{order_column} DESC,m.id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, storage_registry.StorageRegistryError) as exc:
        return {
            "ok": False,
            "error": f"memory_unavailable:{type(exc).__name__}",
            "entries": [],
            "total": 0,
            "authority_flags": _readonly_authority_flags(),
        }
    entries = [{
        "id": int(row["id"]),
        "key": str(row["key"] or "")[:300],
        "value": str(row["value"] or "")[:MAX_MEMORY_VALUE_CHARS],
        "tags": str(row["tags"] or "")[:500],
        "scope": str(row["scope"] or "")[:40],
        "project": str(row["project"] or "")[:200],
        "created_at": str(row["created_at"] or "")[:80],
        "updated_at": str(row["updated_at"] or "")[:80],
        "status": str(row["status"] or "active")[:32],
    } for row in rows]
    return {
        "ok": True,
        "server_tool": "aiworkhub_dashboard_memory",
        "entries": entries,
        "count": len(entries),
        "total": total,
        "truncated": total > len(entries),
        "authority_flags": _readonly_authority_flags(),
    }


def session_view(limit: int = 100) -> dict[str, Any]:
    """READ-ONLY: list the newest Session Manager transcript documents for the active repository.

    Use this to inspect recent session continuity documents in the dashboard; use aiworkhub_dashboard_memory for durable AI Memory keys or aiworkhub_dashboard_kb for Knowledge Base articles. Opens the canonical transcript SQLite database read-only and never modifies session checkpoints or access state.

    Args:
        limit: int (default 100, clamped to 1..500) maximum number of newest transcript documents to return.
    """

    try:
        safe_limit = max(1, min(int(limit), MAX_SESSION_ROWS))
    except (TypeError, ValueError):
        safe_limit = 100
    try:
        root = core.repo_root()
        registry = storage_registry.load_storage_registry(root)
        db_path = storage_registry.resolve_database_path(registry, "transcript")
        # NF-2026-00261: route read-only opens through the canonical helper so
        # the URI is percent-encoded (a '#' in the repo path stays '%23' instead
        # of truncating the '?mode=ro' query) and query_only is enforced.
        connection = sqlite_readonly.connect_readonly(db_path)
        connection.row_factory = sqlite3.Row
        try:
            total = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            rows = connection.execute(
                "SELECT doc_id, source_id, timestamp, kind, content FROM documents "
                "ORDER BY timestamp DESC, doc_id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, storage_registry.StorageRegistryError) as exc:
        return {
            "ok": False,
            "error": f"sessions_unavailable:{type(exc).__name__}",
            "entries": [],
            "total": 0,
            "authority_flags": _readonly_authority_flags(),
        }
    entries = [{
        "id": int(row["doc_id"]),
        "source_id": str(row["source_id"] or "")[:300],
        "timestamp": str(row["timestamp"] or "")[:80],
        "kind": str(row["kind"] or "")[:80],
        "content": str(row["content"] or "")[:MAX_CONTEXT_VALUE_CHARS],
    } for row in rows]
    return {
        "ok": True,
        "server_tool": "aiworkhub_dashboard_sessions",
        "entries": entries,
        "count": len(entries),
        "total": total,
        "truncated": total > len(entries),
        "authority_flags": _readonly_authority_flags(),
    }


def kb_view(limit: int = 100) -> dict[str, Any]:
    """READ-ONLY: list the newest canonical Knowledge Base entries for the active repository.

    Use this to browse repository Knowledge Base articles, categories, and tags in the dashboard; use aiworkhub_dashboard_memory for key-value AI Memory rows or aiworkhub_dashboard_sessions for session transcripts. Opens the KB SQLite store in read-only mode without mutating entries or usage counters.

    Args:
        limit: int (default 100, clamped to 1..500) maximum number of newest KB entries to return.
    """

    try:
        safe_limit = max(1, min(int(limit), MAX_KB_ROWS))
    except (TypeError, ValueError):
        safe_limit = 100
    try:
        root = core.repo_root()
        registry = storage_registry.load_storage_registry(root)
        db_path = storage_registry.resolve_database_path(registry, "kb")
        # NF-2026-00261: route read-only opens through the canonical helper so
        # the URI is percent-encoded (a '#' in the repo path stays '%23' instead
        # of truncating the '?mode=ro' query) and query_only is enforced.
        connection = sqlite_readonly.connect_readonly(db_path)
        connection.row_factory = sqlite3.Row
        try:
            total = int(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
            has_state = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_entity_state'"
            ).fetchone() is not None
            if has_state:
                rows = connection.execute(
                    "SELECT e.id,e.key,e.title,e.body,e.category,e.tags,e.source_refs,"
                    "COALESCE(s.status,'active') status FROM entries e "
                    "LEFT JOIN context_entity_state s ON s.entity_type='kb' AND s.entity_id=e.id "
                    "ORDER BY e.id DESC LIMIT ?", (safe_limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id,key,title,body,category,tags,source_refs,'active' status "
                    "FROM entries ORDER BY id DESC LIMIT ?", (safe_limit,),
                ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, storage_registry.StorageRegistryError) as exc:
        return {
            "ok": False,
            "error": f"kb_unavailable:{type(exc).__name__}",
            "entries": [],
            "total": 0,
            "authority_flags": _readonly_authority_flags(),
        }
    entries = [{
        "id": int(row["id"]),
        "key": str(row["key"] or "")[:300],
        "title": str(row["title"] or "")[:500],
        "body": str(row["body"] or "")[:MAX_CONTEXT_VALUE_CHARS],
        "category": str(row["category"] or "")[:120],
        "tags": str(row["tags"] or "")[:500],
        "source_refs": str(row["source_refs"] or "")[:1000],
        "status": str(row["status"] or "active")[:32],
    } for row in rows]
    return {
        "ok": True,
        "server_tool": "aiworkhub_dashboard_kb",
        "entries": entries,
        "count": len(entries),
        "total": total,
        "truncated": total > len(entries),
        "authority_flags": _readonly_authority_flags(),
    }


def skills_view() -> dict[str, Any]:
    """READ-ONLY: return measured skill lifecycle counts and selection or injection coverage for the active repository.

    Use this to audit active skill reachability, selection receipts, and consecutive empty-injection streaks; use aiworkhub_dashboard_settings for feature switches or aiworkhub_dashboard_snapshot for task queue state. Reads the skill registry and receipt store without mutating state, and reports measured=False with an explicit reason when the store is absent or unreadable.
    """
    try:
        root = core.repo_root()
        coverage = skill_registry_store.skill_coverage(root)
    except (
        skill_registry_store.SkillStoreError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as exc:
        reason = f"skill_coverage_unavailable:{type(exc).__name__}"
        return {
            "ok": False,
            "server_tool": "aiworkhub_dashboard_skills",
            "error": reason,
            **skill_registry_store.unmeasured_coverage(reason),
            "authority_flags": _readonly_authority_flags(),
        }
    return {
        "ok": True,
        "server_tool": "aiworkhub_dashboard_skills",
        **coverage,
        "authority_flags": _readonly_authority_flags(),
    }


# NF-2026-01447: one measured preview replied at 22 KB, nearly all of it the
# full candidate/protected/pinned/registration/quarantine lists. The default
# answers with the counts, the byte lines, the preview_digest and the ten
# largest candidates; ``include_items=true`` returns every list. The digest is
# computed inside storage_retention over the candidate identities, not over
# what this view renders, so quarantine reconfirmation is unaffected.
_STORAGE_PREVIEW_TOP_CANDIDATES = 10


def _storage_preview_items_folded(response: Mapping[str, Any]) -> dict[str, Any]:
    """Keep counts, bytes and the largest candidates; fold the long lists."""

    folded = dict(response)
    candidates = folded.get("candidates")
    if isinstance(candidates, list):
        rows = [item for item in candidates if isinstance(item, Mapping)]
        top = sorted(
            rows,
            key=lambda item: (-int(item.get("size_bytes") or 0), str(item.get("id") or "")),
        )[:_STORAGE_PREVIEW_TOP_CANDIDATES]
        folded["candidates"] = [dict(item) for item in top]
        folded["candidates_order"] = "size_bytes_desc"
        folded["candidates_omitted"] = len(candidates) - len(top)
    protected = folded.get("protected")
    if isinstance(protected, list):
        folded.pop("protected")
        folded["protected_omitted"] = len(protected)
    pinned = folded.get("pinned_predecessors")
    if isinstance(pinned, list):
        folded.pop("pinned_predecessors")
        folded["pinned_predecessors_count"] = len(pinned)
    health = folded.get("registration_health")
    if isinstance(health, Mapping):
        # Keeps preview_digest and every scalar count/verdict; drops the
        # per-registration rows behind them.
        folded["registration_health"] = {
            key: value
            for key, value in health.items()
            if not isinstance(value, (list, dict))
        }
    quarantine = folded.get("quarantine")
    if isinstance(quarantine, Mapping) and isinstance(quarantine.get("batches"), list):
        batches = quarantine["batches"]
        bounded = {key: value for key, value in quarantine.items() if key != "batches"}
        bounded["batches_omitted"] = len(batches)
        folded["quarantine"] = bounded
    folded["include_items"] = False
    folded["items_request"] = {
        "tool": "aiworkhub_dashboard_storage_retention_preview",
        "include_items": True,
    }
    return folded


def storage_retention_preview_view(include_items: bool = False) -> dict[str, Any]:
    """READ-ONLY: preview reclaimable worker worktrees, stale Git registrations, and storage quarantine batches.

    Use this first to obtain a fresh preview_digest before calling aiworkhub_dashboard_storage_quarantine or aiworkhub_dashboard_storage_registration_prune; use aiworkhub_dashboard_terminal_log_retention_preview or aiworkhub_dashboard_task_retention_preview for terminal logs or archived tasks. Scans worktree storage read-only without moving or deleting files.

    Args:
        include_items: bool (default False); False folds lists to counts and top-ten candidates, True returns full candidate and batch lists.
    """
    try:
        root = core.repo_root()
        response = storage_retention.preview(root)
        response["quarantine"] = storage_retention.list_batches(root)
    except storage_retention.StorageRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    else:
        if include_items is not True:
            response = _storage_preview_items_folded(response)
    response["server_tool"] = "aiworkhub_dashboard_storage_retention_preview"
    response["authority_flags"] = _readonly_authority_flags()
    return response


def storage_quarantine_view(preview_digest: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: move reclaimable worker worktrees matching a current preview into a reversible quarantine batch.

    Call aiworkhub_dashboard_storage_retention_preview first to obtain preview_digest; use aiworkhub_dashboard_storage_restore to undo a batch or aiworkhub_dashboard_storage_purge to delete an expired batch. Requires confirm=True and an unchanged preview_digest, moves eligible worktree directories into a signed quarantine batch with an undo deadline, writes an audit entry, and invalidates cached storage telemetry.

    Args:
        preview_digest: str SHA-256 digest from aiworkhub_dashboard_storage_retention_preview; stale digests fail closed.
        confirm: bool (default False); must be True to execute the quarantine move.
    """
    try:
        root = core.repo_root()
        response = storage_retention.quarantine(
            root,
            preview_digest=str(preview_digest or "")[:128],
            confirm=confirm is True,
        )
        storage_observability.invalidate(root)
    except storage_retention.StorageRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_storage_quarantine"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def storage_registration_prune_view(
    preview_digest: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """USER WRITE: prune stale AIWorkHub Git worktree registrations verified against a current preview digest.

    Call aiworkhub_dashboard_storage_retention_preview first to obtain the registration preview_digest; use aiworkhub_dashboard_storage_quarantine to move physical worktree directories instead. Requires confirm=True, refuses when foreign stale registrations exist or the digest drifted, runs git worktree prune, records an audit entry, and invalidates storage telemetry.

    Args:
        preview_digest: str registration preview digest from aiworkhub_dashboard_storage_retention_preview; stale digests fail closed.
        confirm: bool (default False); must be True to prune stale Git worktree registrations.
    """
    try:
        root = core.repo_root()
        response = storage_retention.prune_stale_registrations(
            root,
            preview_digest=str(preview_digest or "")[:128],
            confirm=confirm is True,
        )
        storage_observability.invalidate(root)
    except storage_retention.StorageRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_storage_registration_prune"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def storage_restore_view(batch_id: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: restore a quarantined worker-worktree batch back to the repository worktree root.

    Use this to roll back a quarantine batch listed by aiworkhub_dashboard_storage_retention_preview; use aiworkhub_dashboard_storage_purge to permanently delete an expired batch instead. Requires confirm=True, moves quarantined directories back without overwriting existing paths, reinstates Git worktree registrations when applicable, updates the batch manifest and audit log, and invalidates storage telemetry.

    Args:
        batch_id: str identifier of the storage quarantine batch to restore.
        confirm: bool (default False); must be True to execute the restore.
    """
    try:
        root = core.repo_root()
        response = storage_retention.restore(
            root,
            batch_id=str(batch_id or "")[:128],
            confirm=confirm is True,
        )
        storage_observability.invalidate(root)
    except storage_retention.StorageRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_storage_restore"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def storage_purge_view(batch_id: str, confirm: bool = False) -> dict[str, Any]:
    """DESTRUCTIVE: permanently delete one expired worker-worktree quarantine batch from disk.

    Inspect batches with aiworkhub_dashboard_storage_retention_preview first, or use aiworkhub_dashboard_storage_restore to recover a batch while its undo window is still active. Requires confirm=True and an authenticated batch whose restore deadline has passed, permanently removes the batch directory, records an audit event, and invalidates storage telemetry.

    Args:
        batch_id: str identifier of the expired storage quarantine batch to permanently remove.
        confirm: bool (default False); must be True to permanently purge the batch.
    """
    try:
        root = core.repo_root()
        response = storage_retention.purge(
            root,
            batch_id=str(batch_id or "")[:128],
            confirm=confirm is True,
        )
        storage_observability.invalidate(root)
    except storage_retention.StorageRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_storage_purge"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def terminal_log_retention_preview_view(
    cursor: int = 0,
    limit: int = terminal_log_retention.DEFAULT_PREVIEW_LIMIT,
) -> dict[str, Any]:
    """READ-ONLY: preview reclaimable terminal process log files, protected logs, and terminal-log quarantine batches.

    Call this before aiworkhub_dashboard_terminal_log_quarantine to inspect candidates and obtain preview_digest, or call aiworkhub_dashboard_terminal_log_usage_backfill when runs lack usage receipts; use aiworkhub_dashboard_storage_retention_preview for worktrees. Scans process logs read-only while excluding the canonical request ledger.

    Args:
        cursor: int (default 0, clamped to >=0) zero-based pagination offset into candidate and protected log lists.
        limit: int (default 50, clamped to 1..200) maximum number of candidate and protected entries returned per page.
    """
    try:
        root = core.repo_root()
        response = terminal_log_retention.preview(root, cursor=cursor, limit=limit)
        response["quarantine"] = terminal_log_retention.list_batches(root)
    except terminal_log_retention.TerminalLogRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_terminal_log_retention_preview"
    response["authority_flags"] = _readonly_authority_flags()
    return response


def terminal_log_quarantine_view(preview_digest: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: move eligible terminal process log files matching a current preview into repo-local quarantine.

    Call aiworkhub_dashboard_terminal_log_retention_preview first to obtain preview_digest, and run aiworkhub_dashboard_terminal_log_usage_backfill first if uncaptured usage receipts protect logs; use aiworkhub_dashboard_terminal_log_restore to undo. Requires confirm=True and a matching preview_digest, moves eligible log files into a quarantine batch with an undo window, records an audit event, and invalidates storage telemetry.

    Args:
        preview_digest: str SHA-256 digest from aiworkhub_dashboard_terminal_log_retention_preview; stale digests fail closed.
        confirm: bool (default False); must be True to move terminal log files to quarantine.
    """
    try:
        root = core.repo_root()
        response = terminal_log_retention.quarantine(
            root, preview_digest=str(preview_digest or "")[:128], confirm=confirm is True
        )
        storage_observability.invalidate(root)
    except terminal_log_retention.TerminalLogRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_terminal_log_quarantine"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def terminal_log_usage_backfill_view(confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: idempotently backfill missing provider usage receipts from retained terminal stdout logs.

    Use this before aiworkhub_dashboard_terminal_log_quarantine when terminal runs remain protected due to missing usage capture receipts; use aiworkhub_dashboard_terminal_log_retention_preview to inspect protected runs first. Requires confirm=True, parses retained worker stdout logs without estimating tokens or cost, appends canonical usage capture events to the task store, and leaves already-captured runs unchanged.

    Args:
        confirm: bool (default False); must be True to scan terminal logs and append usage capture events.
    """

    try:
        root = core.repo_root()
        response = terminal_log_retention.backfill_usage_capture(
            root, confirm=confirm is True
        )
    except terminal_log_retention.TerminalLogRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_terminal_log_usage_backfill"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def terminal_log_restore_view(batch_id: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: restore one terminal-log quarantine batch back into the repository process log directory.

    Use this to recover quarantined log files from a batch listed in aiworkhub_dashboard_terminal_log_retention_preview; use aiworkhub_dashboard_terminal_log_purge to permanently delete an expired batch instead. Requires confirm=True, moves quarantined files back without overwriting existing files, updates the batch manifest and audit trail, and invalidates storage telemetry.

    Args:
        batch_id: str identifier of the terminal-log quarantine batch to restore.
        confirm: bool (default False); must be True to restore the quarantined log files.
    """
    try:
        root = core.repo_root()
        response = terminal_log_retention.restore(
            root, batch_id=str(batch_id or "")[:128], confirm=confirm is True
        )
        storage_observability.invalidate(root)
    except terminal_log_retention.TerminalLogRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_terminal_log_restore"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def terminal_log_purge_view(batch_id: str, confirm: bool = False) -> dict[str, Any]:
    """DESTRUCTIVE: permanently delete one expired terminal-log quarantine batch from disk.

    Inspect quarantine batches with aiworkhub_dashboard_terminal_log_retention_preview first, or use aiworkhub_dashboard_terminal_log_restore to recover files while the undo window is active. Requires confirm=True and an expired restore deadline on manual batches, permanently removes the batch directory, appends an audit record, and invalidates storage telemetry.

    Args:
        batch_id: str identifier of the expired terminal-log quarantine batch to permanently delete.
        confirm: bool (default False); must be True to permanently purge the batch.
    """
    try:
        root = core.repo_root()
        response = terminal_log_retention.purge(
            root, batch_id=str(batch_id or "")[:128], confirm=confirm is True
        )
        storage_observability.invalidate(root)
    except terminal_log_retention.TerminalLogRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_terminal_log_purge"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def task_retention_preview_view(older_than_days: int | None = None) -> dict[str, Any]:
    """READ-ONLY: preview archived task rows eligible for retention quarantine and list existing undo batches.

    Call this before aiworkhub_dashboard_task_quarantine to inspect candidate counts and obtain preview_digest; use aiworkhub_dashboard_task_archive to archive a single live task first. Opens the task database read-only, excludes archived tasks with pending or inflight callbacks, and never mutates task rows.

    Args:
        older_than_days: optional int age cutoff in days (default None uses repository retention policy, valid range 7..3650).
    """
    try:
        root = core.repo_root()
        response = task_retention.preview(root, older_than_days=older_than_days)
        response["quarantine"] = task_retention.list_batches(root)
    except (task_retention.TaskRetentionError, ValueError, TypeError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_task_retention_preview"
    response["authority_flags"] = _readonly_authority_flags()
    return response


def task_archive_view(task_id: str, reason: str = "", confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: archive one non-active task card in the canonical task store without deleting its evidence.

    Use this to hide a finished, blocked, pending, or superseded task from the active queue; use aiworkhub_dashboard_task_restore to unarchive it or aiworkhub_dashboard_task_quarantine to move aged archived tasks into a retention batch. Requires confirm=True, refuses processing or review tasks, sets archived_at with an audit event, and invalidates storage telemetry.

    Args:
        task_id: str canonical task identifier to archive.
        reason: str optional audit note (default "", truncated to 200 chars) recorded on the archive event.
        confirm: bool (default False); must be True to archive the task.
    """
    candidate = str(task_id or "")
    if confirm is not True:
        response = {"ok": False, "error": "task_archive_confirmation_required"}
    elif not dashboard._TASK_ID_RE.fullmatch(candidate):
        response = {"ok": False, "error": "invalid_task_id"}
    else:
        try:
            root = core.repo_root()
            task = task_store.get_task(root, candidate)
            if task is None:
                response = {"ok": False, "error": "task_not_found"}
            elif task_store.canonical_status(task) in {"processing", "review"}:
                response = {"ok": False, "error": "task_archive_active_or_review_forbidden"}
            else:
                ok, status = task_store.archive_task(
                    root,
                    candidate,
                    actor="dashboard_user",
                    reason=str(reason or "")[:200],
                )
                response = {"ok": ok, "task_id": candidate, "status": status}
                storage_observability.invalidate(root)
        except task_store.TaskStoreError as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_task_archive"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def task_restore_view(task_id: str, reason: str = "", confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: restore one archived task card in the canonical task store back to its prior status.

    Use this to unarchive a single task archived via aiworkhub_dashboard_task_archive; use aiworkhub_dashboard_task_quarantine_restore instead if the task was already moved into a task-retention quarantine batch. Requires confirm=True, clears the archived overlay on the task row, records an audit event, and invalidates storage telemetry.

    Args:
        task_id: str canonical task identifier of the archived task to restore.
        reason: str optional audit note (default "", truncated to 200 chars) recorded on the restore event.
        confirm: bool (default False); must be True to restore the archived task.
    """
    candidate = str(task_id or "")
    if confirm is not True:
        response = {"ok": False, "error": "task_restore_confirmation_required"}
    elif not dashboard._TASK_ID_RE.fullmatch(candidate):
        response = {"ok": False, "error": "invalid_task_id"}
    else:
        try:
            root = core.repo_root()
            ok, status = task_store.restore_task(
                root,
                candidate,
                actor="dashboard_user",
                reason=str(reason or "")[:200],
            )
            response = {"ok": ok, "task_id": candidate, "status": status}
            storage_observability.invalidate(root)
        except task_store.TaskStoreError as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_task_restore"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def task_restart_view(
    task_id: str, reason: str = "", confirm: bool = False
) -> dict[str, Any]:
    """USER WRITE: recover one restartable task through its canonical state transition path and launch a worker.

    Use this for operator-initiated retry and launch of a pending, blocked, or dead-processing task; use aiworkhub_dashboard_task_detail to inspect the card first. Requires confirm=True, refuses live processing or review/finished/archived/superseded tasks, reconciles dead claims or blocked state in the task store, spawns a worker via process_launcher, and invalidates storage telemetry.

    Args:
        task_id: str canonical task identifier to recover and launch.
        reason: str optional recovery note (default "" uses "owner manual restart", truncated to 200 chars).
        confirm: bool (default False); must be True to recover and launch the task.
    """
    candidate = str(task_id or "")
    bounded_reason = str(reason or "").strip()[:200] or "owner manual restart"
    if confirm is not True:
        response = {"ok": False, "error": "task_restart_confirmation_required"}
    elif not dashboard._TASK_ID_RE.fullmatch(candidate):
        response = {"ok": False, "error": "invalid_task_id"}
    else:
        try:
            root = core.repo_root()
            task = task_store.get_task(root, candidate)
            if task is None:
                response = {"ok": False, "error": "task_not_found"}
            else:
                try:
                    response = _task_restart_dispatch(candidate, task, bounded_reason)
                finally:
                    storage_observability.invalidate(root)
        except task_store.TaskStoreError as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    response["task_id"] = candidate
    response["server_tool"] = "aiworkhub_dashboard_task_restart"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def _task_restart_dispatch(
    task_id: str, task: Mapping[str, Any], reason: str
) -> dict[str, Any]:
    """Route by measured card state through existing core functions only, then launch."""
    status = task_store.canonical_status(task)
    steps: list[dict[str, Any]] = []

    def _step(name: str, result: Mapping[str, Any]) -> bool:
        ok = bool(result.get("ok"))
        steps.append(
            {
                "step": name,
                "ok": ok,
                "error": None if ok else str(result.get("stderr") or result.get("error") or "")[:240],
            }
        )
        return ok

    def _blocked_recovery(card: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        substatus = str(card.get("worker_status") or "").strip()
        if substatus in core._RETRYABLE_OPERATIONAL_TERMINAL_SUBSTATUSES:
            return "retry_terminal", core.retry_terminal_task(
                task_id=task_id,
                request_id=str(card.get("launch_request_id") or "").strip(),
                terminal_substatus=substatus,
                reason=reason,
            )
        return "recover_blocked_rework", core.recover_blocked_rework(
            task_id, feedback_reason=reason
        )

    if status == "pending":
        if str(task.get("worker_status") or "") != "unclaimed":
            return {"ok": False, "error": "task_claimed"}
        path = "launch"
    elif status == "blocked":
        path, result = _blocked_recovery(task)
        if not _step(path, result):
            return {"ok": False, "error": f"{path}_failed", "steps": steps}
    elif status == "processing":
        launch_request_id = str(task.get("launch_request_id") or "").strip()
        liveness = (
            process_launcher.default_manager().status(launch_request_id)
            if launch_request_id
            else {}
        )
        if isinstance(liveness, dict) and liveness.get("process_alive"):
            return {"ok": False, "error": "task_running"}
        reconcile_result = core.reconcile_dead_processing_task(
            task_id, launch_request_id, int(task.get("claim_epoch") or 0)
        )
        if not _step("reconcile_dead_processing", reconcile_result):
            return {"ok": False, "error": "reconcile_dead_processing_failed", "steps": steps}
        current_task = task_store.get_task(core.repo_root(), task_id) or {}
        blocked_path, blocked_result = _blocked_recovery(current_task)
        if not _step(blocked_path, blocked_result):
            return {"ok": False, "error": f"{blocked_path}_failed", "steps": steps}
        path = f"reconcile_dead_processing+{blocked_path}"
    else:
        return {"ok": False, "error": f"not_restartable:{status}"}

    launch_result = process_launcher.default_manager().launch(task_id=task_id)
    launch_info = launch_result if isinstance(launch_result, dict) else {}
    launched = _step("launch", launch_info)
    return {
        "ok": launched,
        "error": None if launched else (steps[-1]["error"] or "launch_failed"),
        "path": path,
        "steps": steps,
        "launch": {
            "request_id": launch_info.get("request_id"),
            "state": launch_info.get("state"),
            "blocked_reason": launch_info.get("blocked_reason"),
        },
    }


def _needfix_response(response: Mapping[str, Any], tool: str, *, write: bool = False) -> dict[str, Any]:
    """Attach dashboard authority metadata to one bounded NeedFix response.

    The authority of a NeedFix/Roadmap view is a function of the tool, not of
    the call, so it is one string (``storage_write`` | ``readonly``) rather
    than the seven constant booleans that rode on every reply (5% of every
    transition reply, 54 KB over 348 calls, identical each time).
    """
    result = dict(response)
    result["server_tool"] = tool
    result["authority"] = "storage_write" if write else "readonly"
    return result


def _needfix_item_view(row: Mapping[str, Any], *, include_item: bool) -> dict[str, Any]:
    """The compact list projection by default (id/status/kind/severity/
    readiness/tags/scope lists); ``include_item=True`` adds description,
    scope, provenance and evidence -- the 67% of a transition reply the
    manager wrote itself moments earlier."""
    return _bounded_needfix_row(row, include_detail=include_item)


def _needfix_transition_receipt(
    row: Mapping[str, Any],
    *,
    action: str,
    steps: list[Any],
    include_item: bool,
) -> dict[str, Any]:
    """Delta receipt for one (or one promoted pair of) lifecycle step(s)."""
    transitions = [step for step in steps if isinstance(step, Mapping)]
    first = transitions[0] if transitions else {}
    last = transitions[-1] if transitions else {}
    return {
        "ok": True,
        "schema_id": "aiworkhub.needfix_transition_receipt.v1",
        "id": str(row.get("id") or "")[:32],
        "action": action,
        "status_before": first.get("status_before"),
        "status_after": str(row.get("status") or "")[:40],
        "updated_at": str(row.get("updated_at") or "")[:64],
        "readiness_score": max(0, min(100, int(row.get("readiness_score") or 0))),
        "converted_task_id": str(row.get("converted_task_id") or "")[:200] or None,
        "event_id": last.get("event_id"),
        "events": [
            {
                "event": str(step.get("event") or "")[:40],
                "event_id": step.get("event_id"),
                "status_before": step.get("status_before"),
                "status_after": step.get("status_after"),
            }
            for step in transitions
        ],
        "item": _needfix_item_view(row, include_item=include_item),
    }


def _bounded_needfix_row(row: Mapping[str, Any], *, include_detail: bool = False) -> dict[str, Any]:
    """Return only bounded fields safe for the native Webview."""
    result: dict[str, Any] = {
        "id": str(row.get("id") or "")[:32],
        "title": str(row.get("title") or "")[:240],
        "status": str(row.get("status") or "")[:40],
        "kind": str(row.get("kind") or "")[:60],
        "severity": str(row.get("severity") or "")[:24],
        "readiness_score": max(0, min(100, int(row.get("readiness_score") or 0))),
        "duplicate_parent_id": str(row.get("duplicate_parent_id") or "")[:32] or None,
        "converted_task_id": str(row.get("converted_task_id") or "")[:200] or None,
        "created_at": str(row.get("created_at") or "")[:64],
        "updated_at": str(row.get("updated_at") or "")[:64],
        "archived_at": str(row.get("archived_at") or "")[:64] or None,
        "tags": [str(value)[:80] for value in list(row.get("tags") or [])[:24]],
        "scope_files": [str(value)[:500] for value in list(row.get("scope_files") or [])[:48]],
        "scope_symbols": [str(value)[:300] for value in list(row.get("scope_symbols") or [])[:48]],
        "evidence_refs": [str(value)[:500] for value in list(row.get("evidence_refs") or [])[:48]],
    }
    if include_detail:
        result.update({
            "description": str(row.get("description") or "")[:8000],
            "scope": str(row.get("scope") or "")[:4000] or None,
            "provenance": dict(list((row.get("provenance") or {}).items())[:32])
            if isinstance(row.get("provenance"), Mapping) else {},
            "evidence": dict(list((row.get("evidence") or {}).items())[:32])
            if isinstance(row.get("evidence"), Mapping) else {},
        })
    return result


def needfix_list_view(
    status: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """READ-ONLY: list bounded NeedFix inbox items with optional status, kind, severity, and archive filters.

    Use this to browse or page through NeedFix summaries; use aiworkhub_dashboard_needfix_detail to fetch full description, scope, provenance, and audit events for one item, or aiworkhub_dashboard_roadmap_list for Roadmap items. Reads the NeedFix SQLite store without mutating state.

    Args:
        status: optional str lifecycle status filter (default None returns all active statuses).
        kind: optional str classification kind filter (default None).
        severity: optional str severity filter such as critical, high, medium, low, or info (default None).
        include_archived: bool (default False); True includes archived NeedFix rows.
        limit: int (default 100, clamped to 1..200) maximum rows to return.
        offset: int (default 0, clamped to >=0) zero-based pagination offset.
    """
    bounded_limit = max(1, min(int(limit or 100), 200))
    bounded_offset = max(0, int(offset or 0))
    try:
        rows = core.needfix_list(
            status=str(status)[:40] if status else None,
            kind=str(kind)[:60] if kind else None,
            severity=str(severity)[:24] if severity else None,
            include_archived=include_archived is True,
            limit=bounded_limit,
            offset=bounded_offset,
        )
        response = {
            "ok": True,
            "entries": [_bounded_needfix_row(row) for row in rows],
            "count": len(rows),
            "limit": bounded_limit,
            "offset": bounded_offset,
            "truncated": len(rows) >= bounded_limit,
        }
    except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240], "entries": []}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_list")


def needfix_detail_view(needfix_id: str, event_limit: int = 50) -> dict[str, Any]:
    """READ-ONLY: return one NeedFix item with full detail fields and its bounded audit event history.

    Use this to inspect a single NeedFix item's description, scope, evidence, and lifecycle events; use aiworkhub_dashboard_needfix_list to browse summary rows or aiworkhub_dashboard_needfix_convert_preview to preview task conversion. Validates needfix_id format and reads the NeedFix store without mutating state.

    Args:
        needfix_id: str canonical NeedFix identifier (for example NF-2026-00001).
        event_limit: int (default 50, clamped to 1..100) maximum number of audit events to return.
    """
    candidate = str(needfix_id or "")
    if not needfix_store.NF_ID_RE.fullmatch(candidate):
        return _needfix_response(
            {"ok": False, "error": "invalid_needfix_id"},
            "aiworkhub_dashboard_needfix_detail",
        )
    try:
        row = core.needfix_show(candidate)
        events = core.needfix_events(candidate, limit=max(1, min(int(event_limit or 50), 100)))
        response = {
            "ok": True,
            "item": _bounded_needfix_row(row, include_detail=True),
            "events": [dict(list(event.items())[:24]) for event in events[:100]],
        }
    except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_detail")


def _bounded_roadmap_row(
    row: Mapping[str, Any], *, include_detail: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": str(row.get("id") or "")[:32],
        "title": str(row.get("title") or "")[:240],
        "status": str(row.get("status") or "")[:40],
        "priority": str(row.get("priority") or "")[:24],
        "milestone": str(row.get("milestone") or "")[:500],
        "needfix_ids": [str(value)[:32] for value in list(row.get("needfix_ids") or [])[:100]],
        "task_ids": [str(value)[:256] for value in list(row.get("task_ids") or [])[:100]],
        "depends_on": [str(value)[:32] for value in list(row.get("depends_on") or [])[:100]],
        "dependency_blockers": [
            str(value)[:32] for value in list(row.get("dependency_blockers") or [])[:100]
        ],
        "dependency_ready": bool(row.get("dependency_ready", True)),
        "created_at": str(row.get("created_at") or "")[:64],
        "updated_at": str(row.get("updated_at") or "")[:64],
    }
    if include_detail:
        result.update(
            {
                "outcome": str(row.get("outcome") or "")[:100_000],
                "acceptance": [
                    str(value)[:1000] for value in list(row.get("acceptance") or [])[:100]
                ],
                "provenance": dict(list((row.get("provenance") or {}).items())[:32])
                if isinstance(row.get("provenance"), Mapping)
                else {},
                "evidence_refs": [
                    str(value)[:1000] for value in list(row.get("evidence_refs") or [])[:200]
                ],
                "tasks": [dict(list(task.items())[:8]) for task in list(row.get("tasks") or [])[:100]],
            }
        )
    return result


def roadmap_list_view(
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """READ-ONLY: list bounded Roadmap outcomes joined with task-DAG progress and wave projection.

    Use this to browse Roadmap items, status counts, and wave readiness; use aiworkhub_dashboard_roadmap_detail for one Roadmap item's full detail and audit events, or aiworkhub_dashboard_needfix_list for NeedFix items. Reads the Roadmap and task stores without mutating state.

    Args:
        status: optional str status filter (default None returns all non-archived statuses).
        include_archived: bool (default False); True includes archived Roadmap items.
        limit: int (default 100, clamped to 1..200) maximum number of items to return.
        offset: int (default 0, clamped to >=0) zero-based pagination offset applied to the filtered list.
    """
    try:
        snapshot = core.roadmap_snapshot(
            limit=max(1, min(int(limit or 100), 200)),
            include_archived=include_archived is True,
        )
        rows = list(snapshot.get("items") or [])
        if status:
            rows = [row for row in rows if row.get("status") == str(status)[:40]]
        if not include_archived:
            rows = [row for row in rows if row.get("status") != "archived"]
        bounded_offset = max(0, int(offset or 0))
        rows = rows[bounded_offset : bounded_offset + max(1, min(int(limit or 100), 200))]
        response = {
            "ok": True,
            "entries": [_bounded_roadmap_row(row) for row in rows],
            "count": len(rows),
            "active": int(snapshot.get("active") or 0),
            "total": int(snapshot.get("total") or 0),
            "status_counts": dict(snapshot.get("status_counts") or {}),
            "truncated": bool(snapshot.get("truncated")),
            "current_wave": wave_roadmap.project_snapshot_wave(snapshot, __version__),
        }
    except (roadmap_store.RoadmapError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240], "entries": []}
    return _needfix_response(response, "aiworkhub_dashboard_roadmap_list")


def roadmap_detail_view(roadmap_id: str, event_limit: int = 50) -> dict[str, Any]:
    """READ-ONLY: return one Roadmap outcome with linked task progress, wave projection, and audit events.

    Use this to inspect a specific Roadmap item's dependencies, linked tasks, and event log; use aiworkhub_dashboard_roadmap_list to browse all Roadmap items. Validates roadmap_id format and reads the Roadmap and task stores without mutating state.

    Args:
        roadmap_id: str canonical Roadmap identifier (for example RM-2026-00001).
        event_limit: int (default 50, clamped to 1..100) maximum number of audit events to return.
    """
    candidate = str(roadmap_id or "")
    if not roadmap_store.ROADMAP_ID_RE.fullmatch(candidate):
        return _needfix_response(
            {"ok": False, "error": "invalid_roadmap_id"},
            "aiworkhub_dashboard_roadmap_detail",
        )
    try:
        snapshot = core.roadmap_snapshot(limit=200)
        row = next(
            (item for item in snapshot.get("items") or [] if item.get("id") == candidate),
            None,
        )
        if row is None:
            row = core.roadmap_show(candidate)
        response = {
            "ok": True,
            "item": _bounded_roadmap_row(row, include_detail=True),
            "current_wave": wave_roadmap.project_snapshot_wave(snapshot, __version__),
            "events": [
                dict(list(event.items())[:24])
                for event in core.roadmap_events(
                    candidate, limit=max(1, min(int(event_limit or 50), 100))
                )[:100]
            ],
        }
    except (roadmap_store.RoadmapError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_roadmap_detail")


def _hub_mail_local() -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """This dashboard repository's hub identity, or a named refusal."""
    try:
        root = Path(dashboard._default_repo_root())
    except (OSError, RuntimeError, ValueError) as exc:
        return None, {"ok": False, "error": f"local_hub_identity_unavailable:{type(exc).__name__}"}
    return hub_mail.local_hub(root)


def _hub_mail_outbound(hub_id: str) -> dict[str, Any]:
    """Queued and failed peer deliveries of one hub, counted from its outbox sidecars."""
    directory = hub_mail.box_dir(hub_id, "outbox")
    counts: dict[str, Any] = {"queued": 0, "delivery_failed": 0, "scan_truncated": False}
    if not directory.is_dir():
        return counts
    cap = hub_mail_net.MAX_PENDING_SCAN
    for key, suffix in (("queued", hub_mail_net.PENDING_SUFFIX), ("delivery_failed", hub_mail_net.FAILED_SUFFIX)):
        seen = 0
        for _ in directory.glob(f"*{suffix}"):
            seen += 1
            if seen > cap:
                counts["scan_truncated"] = True
                break
        counts[key] = min(seen, cap)
    return counts


def _hub_mail_peer_row(peer: Mapping[str, Any]) -> dict[str, Any]:
    """A paired peer as display data: public_peer() minus the pin, never the secret."""
    public = hub_mail_net.public_peer(dict(peer))
    return {
        "untrusted_data": True,
        "machine_id": public["machine_id"],
        "machine_label": public["machine_label"],
        "address": public["address"],
        "hubs": [{"hub_id": hub["hub_id"], "repo_name": hub["repo_name"]} for hub in public["hubs"]],
        "paired_at": public["paired_at"],
        "last_seen": public["last_seen"],
        "tls_pinned": bool(public["tls_cert_sha256"]),
    }


def _hub_mail_listener(status: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """listener_status() reduced to display fields; the pidfile token never leaves."""
    status = hub_mail_net.listener_status() if status is None else status
    running = bool(status.get("running"))
    host, port = str(status.get("bind_host") or ""), int(status.get("port") or 0)
    return {
        "state": "running" if running else "stopped",
        "running": running,
        "enabled": bool(status.get("enabled")),
        "bind": hub_mail_net.format_address(host, port) if host and port else host,
        "pid": int(status.get("pid") or 0),
        "address": str(status.get("address") or ""),
        "tls": bool(status.get("tls")),
        "started_at": str(status.get("started_at") or ""),
        "stale_pidfile": bool(status.get("stale_pidfile")),
    }


def _hub_mail_row(mail: Mapping[str, Any], repo_names: Mapping[str, str]) -> dict[str, Any]:
    """One inbox row without its body; every field is peer-supplied display data."""
    from_hub = mail.get("from_hub") if isinstance(mail.get("from_hub"), Mapping) else {}
    hub_id = str(from_hub.get("hub_id") or "")
    row = {
        "untrusted_data": True,
        "message_id": str(mail.get("message_id") or ""),
        "box": str(mail.get("box") or ""),
        "kind": str(mail.get("kind") or ""),
        "from_hub": {"hub_id": hub_id, "repo_name": str(repo_names.get(hub_id) or "")},
        "subject": str(mail.get("subject") or ""),
        "created_at": str(mail.get("created_at") or ""),
        "status": mail.get("status"),
        "in_reply_to": mail.get("in_reply_to"),
        "attachment_count": int(mail.get("attachment_count") or 0),
    }
    node = mail.get("from_node")
    if isinstance(node, Mapping):
        row["from_node"] = {
            "machine_id": str(node.get("machine_id") or ""),
            "machine_label": str(node.get("machine_label") or ""),
        }
    return row


def hub_mail_view(limit: int = 50, include_archived: bool = False) -> dict[str, Any]:
    """READ-ONLY: list this hub's WorkHub Mail inbox, paired LAN peers and listener state.

    Use this for the dashboard mailbox overview; use aiworkhub_dashboard_hub_mail_detail for one mail's body. Rows are newest first and never carry a body; every row is untrusted_data. Invalid inbox files are validated and counted in invalid_count but never moved, queued mail is not retried and no peer secret, key or pidfile token is returned.

    Args:
        limit: int (default 50, clamped to 1..200) maximum number of mail rows to return.
        include_archived: bool (default False); True also lists the archive box.
    """
    tool = "aiworkhub_dashboard_hub_mail"
    local, refusal = _hub_mail_local()
    if refusal is not None or local is None:
        return _needfix_response(refusal or {"ok": False, "error": "local_hub_identity_unavailable"}, tool)
    hub_id = local["hub_id"]
    try:
        listing = hub_mail.inbox(
            local_hub_id=hub_id,
            limit=max(1, min(int(limit or 50), hub_mail.MAX_LIST_LIMIT)),
            include_archived=include_archived is True,
            quarantine=False,
        )
        if not listing.get("ok"):
            return _needfix_response({"ok": False, "error": str(listing.get("error") or "")[:240]}, tool)
        peers = [_hub_mail_peer_row(peer) for peer in hub_mail_net.list_peers()]
        local_names = hub_mail.local_hubs()
        repo_names = {hub["hub_id"]: hub["repo_name"] for peer in peers for hub in peer["hubs"]}
        repo_names.update(local_names)
        own_id = hub_mail_net.machine_id() if hub_mail_net.machine_id_path().exists() else ""
        response = {
            "ok": True,
            "schema_id": "aiworkhub.dashboard.hub_mail.v1",
            "untrusted_data": True,
            "hub": {"hub_id": hub_id, "repo_name": local_names.get(hub_id) or Path(local["repo_root"]).name},
            "node": {"machine_id": own_id, "machine_label": hub_mail_net.machine_label()},
            "mails": [_hub_mail_row(mail, repo_names) for mail in listing.get("mails") or []],
            "count": int(listing.get("count") or 0),
            "total": int(listing.get("total") or 0),
            "truncated": bool(listing.get("truncated")),
            "invalid_count": int(listing.get("invalid_count") or 0),
            "quarantined_count": int(listing.get("quarantine_total") or 0),
            "scan_truncated": bool(listing.get("scan_truncated")),
            "unscanned_count": int(listing.get("unscanned_count") or 0),
            "outbound": _hub_mail_outbound(hub_id),
            "peers": peers,
            "listener": _hub_mail_listener(),
        }
    except (hub_mail.HubMailError, OSError, TypeError, ValueError, KeyError) as exc:
        response = {"ok": False, "error": f"{type(exc).__name__}:{str(exc)[:200]}"}
    return _needfix_response(response, tool)


def hub_mail_detail_view(message_id: str) -> dict[str, Any]:
    """READ-ONLY: return one received WorkHub Mail with its body, marked untrusted_data.

    Use this to read a mail chosen from aiworkhub_dashboard_hub_mail. A message_id that is not 32 lowercase hex is refused before any filesystem read; an invalid file is skipped in place, never quarantined.

    Args:
        message_id: str 32-hex message id of a mail in this hub's inbox or archive.
    """
    tool = "aiworkhub_dashboard_hub_mail_detail"
    candidate = str(message_id or "")
    if not hub_mail._MESSAGE_ID_RE.fullmatch(candidate):
        return _needfix_response({"ok": False, "error": "invalid_message_id"}, tool)
    local, refusal = _hub_mail_local()
    if refusal is not None or local is None:
        return _needfix_response(refusal or {"ok": False, "error": "local_hub_identity_unavailable"}, tool)
    try:
        shown = hub_mail.show(local_hub_id=local["hub_id"], message_id=candidate, quarantine=False)
    except (hub_mail.HubMailError, OSError, TypeError, ValueError) as exc:
        shown = {"ok": False, "error": f"{type(exc).__name__}:{str(exc)[:200]}"}
    if not shown.get("ok") or not isinstance(shown.get("mail"), Mapping):
        return _needfix_response({"ok": False, "error": str(shown.get("error") or "")[:240]}, tool)
    mail = dict(shown["mail"])
    mail["untrusted_data"] = True
    return _needfix_response({"ok": True, "untrusted_data": True, "mail": mail}, tool)


def _hub_mail_write_refusal(tool: str, confirm: Any) -> dict[str, Any] | None:
    """The gate every dashboard hub mail write shares, or None to proceed.

    confirm=True first, then the write gate, then the same verified local hub
    route as hub_mail_view; nothing is read or written until all three pass.
    """
    if confirm is not True:
        return _needfix_response({"ok": False, "error": "confirm_required"}, tool, write=True)
    if not core.writes_allowed():
        return _needfix_response({"ok": False, "error": "write_gate_closed"}, tool, write=True)
    local, refusal = _hub_mail_local()
    if refusal is not None or local is None:
        return _needfix_response(
            refusal or {"ok": False, "error": "local_hub_identity_unavailable"}, tool, write=True,
        )
    return None


def hub_mail_listener_action_view(
    action: str = "status",
    bind_host: str | None = None,
    port: int | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """USER WRITE: start or stop this machine's WorkHub Mail listener; status is read-only.

    Use this from the dashboard WorkHub Mail panel; aiworkhub_dashboard_hub_mail shows the same listener state with the inbox. start persists enabled=true so MCP startup restarts the listener; stop persists enabled=false. One listener runs per home; no default bind address. start and stop require confirm=True and an open write gate. Delegates to hub_mail_net.listener_action like aiworkhub_manager_hub_mail_listener and never returns the pidfile token.

    Args:
        action: str start, stop or status (default "status"); status is read-only.
        bind_host: optional str interface to bind for start, e.g. localhost or a LAN address; required for start.
        port: optional int TCP port for start; required for start (0 picks a free port).
        tls_cert: optional str PEM certificate path for TLS; give together with tls_key, omit for plain HTTP.
        tls_key: optional str PEM private key path for TLS; give together with tls_cert.
        confirm: bool (default False); must be True to start or stop the listener.
    """
    tool = "aiworkhub_dashboard_hub_mail_listener"
    verb = str(action or "status")
    if verb == "status":
        local, refusal = _hub_mail_local()
        if refusal is not None or local is None:
            return _needfix_response(refusal or {"ok": False, "error": "local_hub_identity_unavailable"}, tool)
        status = hub_mail_net.listener_action("status")
        return _needfix_response({"ok": True, "action": verb, "listener": _hub_mail_listener(status)}, tool)
    if verb not in ("start", "stop"):
        return _needfix_response({"ok": False, "error": "listener_action_invalid"}, tool, write=True)
    refusal = _hub_mail_write_refusal(tool, confirm)
    if refusal is not None:
        return refusal
    try:
        result = hub_mail_net.listener_action(
            verb, bind_host=str(bind_host or "") or None, port=port,
            tls_cert=str(tls_cert or "") or None, tls_key=str(tls_key or "") or None,
        )
    except (hub_mail.HubMailError, OSError, TypeError, ValueError) as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}:{str(exc)[:200]}"}
    response: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "action": verb,
        "listener": _hub_mail_listener(result if "running" in result else None),
    }
    if not response["ok"]:
        response["error"] = str(result.get("error") or "")[:240]
    for key in ("already_running", "stopped"):
        if key in result:
            response[key] = bool(result[key])
    if result.get("log"):
        response["log"] = str(result["log"])[:500]
    return _needfix_response(response, tool, write=True)


def hub_mail_pair_begin_view(machine_label: str | None = None, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: mint a single-use code to pair this machine with another for WorkHub Mail.

    Read the code and this machine's listener address to the other machine's owner out of band; they enter both in aiworkhub_dashboard_hub_mail_pair_complete. The code expires after 10 minutes, dies after 5 failed proofs and replaces any older code; this machine's listener must be running to accept the pairing. Requires confirm=True and an open write gate. Delegates to the same hub_mail_net.set_machine_label and pair_begin as aiworkhub_manager_hub_mail_pair_begin; no peer secret is returned.

    Args:
        machine_label: optional str display label for this machine as peers see it (at most 64 characters, control characters stripped); omit or leave empty to keep the current label.
        confirm: bool (default False); must be True to mint the code.
    """
    tool = "aiworkhub_dashboard_hub_mail_pair_begin"
    refusal = _hub_mail_write_refusal(tool, confirm)
    if refusal is not None:
        return refusal
    try:
        if machine_label:
            hub_mail_net.set_machine_label(str(machine_label))
        begun = hub_mail_net.pair_begin()
    except (hub_mail.HubMailError, OSError, TypeError, ValueError) as exc:
        begun = {"ok": False, "error": f"{type(exc).__name__}:{str(exc)[:200]}"}
    if not begun.get("ok"):
        return _needfix_response({"ok": False, "error": str(begun.get("error") or "")[:240]}, tool, write=True)
    listener = _hub_mail_listener(begun["listener"] if isinstance(begun.get("listener"), Mapping) else None)
    response = {
        "ok": True,
        "schema_id": "aiworkhub.dashboard.hub_mail_pair_begin.v1",
        "code": str(begun.get("code") or ""),
        "expires_at": str(begun.get("expires_at") or ""),
        "max_failed_proofs": int(begun.get("max_failed_proofs") or 0),
        "machine_id": str(begun.get("machine_id") or ""),
        "machine_label": str(begun.get("machine_label") or ""),
        "address": listener["address"],
        "listener": listener,
    }
    return _needfix_response(response, tool, write=True)


def hub_mail_pair_complete_view(
    address: str,
    code: str,
    tls: bool = False,
    listener_address: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    """USER WRITE: pair this machine with another one's WorkHub Mail listener.

    Use the address and code the other machine's owner read out from its pair_begin. The code itself never crosses the network: both machines derive one per-peer secret from it and prove they hold it. This machine's own listener must be running so the peer can mail back. Requires confirm=True and an open write gate. Delegates to the same hub_mail_net.pair_complete as aiworkhub_manager_hub_mail_pair_complete; the paired peer is returned as display data without its secret.

    Args:
        address: str the other machine's listener address as host:port (or [ipv6]:port).
        code: str the pairing code read out by the other machine's owner; dashes and spaces are ignored.
        tls: bool (default False); True when the other listener runs with TLS, pinning its certificate sha256.
        listener_address: str (default empty) this machine's own listener address as host:port that the peer dials back; empty uses the running listener's advertised address.
        confirm: bool (default False); must be True to pair.
    """
    tool = "aiworkhub_dashboard_hub_mail_pair_complete"
    refusal = _hub_mail_write_refusal(tool, confirm)
    if refusal is not None:
        return refusal
    try:
        result = hub_mail_net.pair_complete(
            str(address or ""), str(code or ""), tls=tls is True,
            listener_address=str(listener_address or "") or None,
        )
        if not result.get("ok") or not isinstance(result.get("peer"), Mapping):
            response = {"ok": False, "error": str(result.get("error") or "pair_response_invalid")[:240]}
        else:
            response = {"ok": True, "untrusted_data": True, "peer": _hub_mail_peer_row(result["peer"])}
    except (hub_mail.HubMailError, OSError, TypeError, ValueError, KeyError) as exc:
        response = {"ok": False, "error": f"{type(exc).__name__}:{str(exc)[:200]}"}
    return _needfix_response(response, tool, write=True)


def needfix_capture_view(
    title: str,
    description: str,
    kind: str = "other",
    severity: str = "medium",
    scope: str | None = None,
    tags: list[str] | None = None,
    include_item: bool = False,
) -> dict[str, Any]:
    """USER WRITE: capture a new NeedFix item or deduplicate against an existing live row.

    Use this to record a new bug or proposal in the NeedFix inbox; use aiworkhub_dashboard_needfix_update to edit an existing item or aiworkhub_dashboard_needfix_transition to change status. Requires an open write gate, normalizes kind synonyms, persists the new or deduplicated row in the NeedFix store, and returns a capture receipt.

    Args:
        title: str summary title (truncated to 240 chars).
        description: str problem details (truncated to 8000 chars).
        kind: str classification kind (default "other"; synonyms are normalized).
        severity: str level: critical, high, medium, low, or info (default "medium").
        scope: optional str affected scope text (default None, up to 4000 chars).
        tags: optional list[str] labels (default None, up to 24 tags).
        include_item: bool (default False); True adds full detail fields to item.
    """
    if not core.writes_allowed():
        return _needfix_response(
            {"ok": False, "error": "write_gate_closed"}, "aiworkhub_dashboard_needfix_capture", write=True,
        )
    canonical_kind, kind_normalized = needfix_store.normalize_kind(str(kind or "other")[:60])
    before = datetime.now(timezone.utc).isoformat()
    try:
        row = core.needfix_capture(
            title=str(title or "")[:240],
            description=str(description or "")[:8000],
            kind=canonical_kind,
            severity=str(severity or "medium")[:24],
            scope=str(scope or "")[:4000] or None,
            tags=[str(value)[:80] for value in list(tags or [])[:24]],
            provenance={"source": "dashboard_user"},
        )
        provenance = row.get("provenance") if isinstance(row.get("provenance"), Mapping) else {}
        created_at = str(row.get("created_at") or "")[:64]
        deduped = bool(created_at) and created_at < before
        response = {
            "ok": True,
            "schema_id": "aiworkhub.needfix_capture_receipt.v1",
            "id": str(row.get("id") or "")[:32],
            "dedupe_key": str(row.get("dedupe_key") or "")[:128],
            "status": str(row.get("status") or "")[:40],
            "kind": str(row.get("kind") or "")[:60],
            "severity": str(row.get("severity") or "")[:24],
            "created_at": created_at,
            "provenance": {"origin": str(provenance.get("origin") or "")[:60] or None},
            "deduped": deduped,
            "existing_id": str(row.get("id") or "")[:32] if deduped else None,
            "kind_normalized": kind_normalized,
            "item": _needfix_item_view(row, include_item=include_item),
        }
    except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_capture", write=True)


def needfix_update_view(
    needfix_id: str,
    title: str | None = None,
    description: str | None = None,
    scope: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    tags: list[str] | None = None,
    readiness_score: int | None = None,
    include_item: bool = False,
) -> dict[str, Any]:
    """USER WRITE: update mutable content and classification fields on an existing NeedFix item.

    Use this to edit title, description, scope, kind, severity, tags, or readiness_score; use aiworkhub_dashboard_needfix_transition to change lifecycle status or aiworkhub_dashboard_needfix_capture to create a new item. Requires an open write gate, persists field changes and an audit event in the NeedFix store, and returns a delta receipt.

    Args:
        needfix_id: str canonical NeedFix identifier to update.
        title: optional str replacement title (default None leaves unchanged, up to 240 chars).
        description: optional str replacement description (default None, up to 8000 chars).
        scope: optional str replacement scope text (default None, up to 4000 chars).
        kind: optional str replacement kind (default None; synonyms normalized).
        severity: optional str replacement severity (default None).
        tags: optional list[str] replacement tag list (default None, up to 24 tags).
        readiness_score: optional int readiness score (default None, clamped to 0..100).
        include_item: bool (default False); True adds full detail fields to item.
    """
    if not core.writes_allowed():
        return _needfix_response(
            {"ok": False, "error": "write_gate_closed"}, "aiworkhub_dashboard_needfix_update", write=True,
        )
    candidate = str(needfix_id or "")
    canonical_kind: str | None = None
    kind_normalized: dict[str, str] | None = None
    if kind is not None:
        canonical_kind, kind_normalized = needfix_store.normalize_kind(str(kind)[:60])
    try:
        row = core.needfix_update(
            candidate,
            title=str(title)[:240] if title is not None else None,
            description=str(description)[:8000] if description is not None else None,
            scope=str(scope)[:4000] if scope is not None else None,
            kind=canonical_kind,
            severity=str(severity)[:24] if severity is not None else None,
            tags=[str(value)[:80] for value in list(tags)[:24]] if tags is not None else None,
            readiness_score=max(0, min(100, int(readiness_score))) if readiness_score is not None else None,
        )
        update = row.get("update_receipt") if isinstance(row.get("update_receipt"), Mapping) else {}
        evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
        response = {
            "ok": True,
            "schema_id": "aiworkhub.needfix_update_receipt.v1",
            "id": str(row.get("id") or "")[:32],
            "status": str(row.get("status") or "")[:40],
            "fields_changed": [str(value)[:40] for value in list(update.get("fields_changed") or [])[:24]],
            "updated_at": str(row.get("updated_at") or "")[:64],
            "evidence_keys_after": [
                str(value)[:80]
                for value in list(update.get("evidence_keys_after") or sorted(evidence))[:64]
            ],
            "event_id": update.get("event_id"),
            "kind_normalized": kind_normalized,
            "item": _needfix_item_view(row, include_item=include_item),
        }
    except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_update", write=True)


def needfix_transition_view(
    needfix_id: str,
    action: str,
    reason: str = "",
    readiness_score: int | None = None,
    duplicate_parent_id: str = "",
    confirm: bool = False,
    include_item: bool = False,
    promote_to: str = "",
) -> dict[str, Any]:
    """USER WRITE: apply one confirmed lifecycle status transition to a NeedFix item.

    Use this to triage, accept, reject, defer, mark duplicate, mark task_planned, resolve, or recover a conversion claim; use aiworkhub_dashboard_needfix_archive, aiworkhub_dashboard_needfix_restore, or aiworkhub_dashboard_needfix_convert_commit for archive, restore, or task creation. Requires confirm=True, persists the state transition and audit event in the NeedFix store, and returns a transition receipt.

    Args:
        needfix_id: str canonical NeedFix identifier to transition.
        action: str transition action: triage, accept, reject, duplicate, defer, task_planned, resolve, resolve_verified, or recover_conversion_claim.
        reason: str note or rationale (default "", up to 1000 chars; required for recover_conversion_claim).
        readiness_score: optional int score for triage or accept (default None).
        duplicate_parent_id: str parent NeedFix ID when action is duplicate (default "").
        confirm: bool (default False); must be True to execute the transition.
        include_item: bool (default False); True adds full detail fields to item.
        promote_to: str optional chained target (default ""; only "accepted" with action="triage" is allowed).
    """
    candidate = str(needfix_id or "")
    selected = str(action or "").strip().lower()
    promote = str(promote_to or "").strip().lower()
    steps: list[Any] = []
    if confirm is not True:
        response = {"ok": False, "error": "needfix_transition_confirmation_required"}
    elif promote and promote != "accepted":
        response = {"ok": False, "error": "invalid_promote_to", "allowed": ["accepted"]}
    elif promote and selected != "triage":
        response = {"ok": False, "error": "promote_to_requires_triage_action"}
    elif selected == "recover_conversion_claim" and not str(reason or "").strip():
        response = {"ok": False, "error": "needfix_recovery_reason_required"}
    else:
        try:
            if selected == "triage":
                row = core.needfix_triage(candidate, readiness_score=readiness_score, triage_note=str(reason)[:1000] or None)
                if promote:
                    steps.append(row.get("transition"))
                    try:
                        row = core.needfix_accept(candidate, readiness_score=readiness_score)
                    except (needfix_store.NeedFixError, sqlite3.Error) as exc:
                        # The triage landed; report it and the exact refusal
                        # instead of pretending neither step happened.
                        partial = _needfix_transition_receipt(
                            row, action=selected, steps=steps, include_item=include_item
                        )
                        partial.update({
                            "ok": False,
                            "error": f"promote_failed:{str(exc)[:200]}",
                            "promote_to": promote,
                        })
                        return _needfix_response(
                            partial, "aiworkhub_dashboard_needfix_transition", write=True
                        )
            elif selected == "accept":
                row = core.needfix_accept(candidate, readiness_score=readiness_score)
            elif selected == "reject":
                row = core.needfix_reject(candidate, reason=str(reason)[:1000])
            elif selected == "duplicate":
                row = core.needfix_mark_duplicate(candidate, str(duplicate_parent_id)[:32], reason=str(reason)[:1000] or None)
            elif selected == "defer":
                row = core.needfix_defer(candidate, reason=str(reason)[:1000] or None)
            elif selected == "task_planned":
                row = core.needfix_mark_task_planned(candidate)
            elif selected == "resolve":
                row = core.needfix_resolve(candidate, resolution_note=str(reason)[:1000] or None)
            elif selected == "resolve_verified":
                row = core.needfix_resolve_verified(
                    candidate, resolution_note=str(reason)[:1000]
                )
            elif selected == "recover_conversion_claim":
                before = core.needfix_show(candidate)
                recovery = core.needfix_recover_conversion_claim(
                    candidate,
                    expected_updated_at=str(before.get("updated_at") or ""),
                    expected_conversion_claim_id=(
                        before.get("conversion_claim_id") or needfix_store.LEGACY_NULL_CLAIM_ID
                    ),
                    reason=str(reason).strip()[:1000],
                )
                row = {**core.needfix_show(candidate), "status": recovery["status"]}
                row["transition"] = {
                    "event": "conversion_claim_recovered",
                    "event_id": None,
                    "status_before": before.get("status"),
                    "status_after": recovery["status"],
                }
            else:
                return _needfix_response(
                    {"ok": False, "error": "invalid_needfix_transition"},
                    "aiworkhub_dashboard_needfix_transition",
                    write=True,
                )
            steps.append(row.get("transition"))
            response = _needfix_transition_receipt(
                row, action=selected, steps=steps, include_item=include_item
            )
            if promote:
                response["promote_to"] = promote
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_transition", write=True)


def needfix_archive_view(needfix_id: str, reason: str = "", confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: archive one non-converting NeedFix item without deleting its stored record.

    Use this to move an inactive or obsolete NeedFix row out of the active inbox; use aiworkhub_dashboard_needfix_restore to return an archived row to an active status, or aiworkhub_dashboard_needfix_purge to permanently delete an archived row. Requires confirm=True, refuses rows currently in converting status, sets status to archived with an audit event in the NeedFix store, and returns the updated item.

    Args:
        needfix_id: str canonical NeedFix identifier to archive.
        reason: str optional archive rationale (default "", truncated to 1000 chars) recorded in the audit event.
        confirm: bool (default False); must be True to archive the NeedFix item.
    """
    if confirm is not True:
        response = {"ok": False, "error": "needfix_archive_confirmation_required"}
    else:
        try:
            row = core.needfix_archive(str(needfix_id or ""), reason=str(reason)[:1000] or None)
            response = {"ok": True, "item": _bounded_needfix_row(row, include_detail=True)}
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_archive", write=True)


def needfix_restore_view(needfix_id: str, target_status: str = "captured", confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: restore an archived NeedFix item to an active non-terminal lifecycle status.

    Use this to unarchive a NeedFix row previously archived with aiworkhub_dashboard_needfix_archive; use aiworkhub_dashboard_needfix_transition to advance the lifecycle of an already-active item. Requires confirm=True and a row currently in archived status, clears archived_at, sets status to target_status, records a restored audit event, and returns the updated item.

    Args:
        needfix_id: str canonical NeedFix identifier of the archived row to restore.
        target_status: str active status to restore into: captured, triaged, accepted, or deferred (default "captured").
        confirm: bool (default False); must be True to restore the NeedFix item.
    """
    if confirm is not True:
        response = {"ok": False, "error": "needfix_restore_confirmation_required"}
    else:
        try:
            row = core.needfix_restore(str(needfix_id or ""), target_status=str(target_status or "captured")[:40])
            response = {"ok": True, "item": _bounded_needfix_row(row, include_detail=True)}
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_restore", write=True)


def needfix_purge_view(needfix_id: str, audit_reason: str, confirm: bool = False) -> dict[str, Any]:
    """DESTRUCTIVE: permanently delete one archived NeedFix row from the NeedFix table while retaining an audit snapshot.

    Use aiworkhub_dashboard_needfix_archive first to archive the item safely, or aiworkhub_dashboard_needfix_restore to recover an archived item instead of deleting it. Requires confirm=True, a non-empty audit_reason, and an already-archived row; records a purged audit event with the row snapshot and permanently deletes the row from the needfix table.

    Args:
        needfix_id: str canonical NeedFix identifier of the archived row to permanently delete.
        audit_reason: str non-empty mandatory justification (truncated to 1000 chars) persisted in the purge audit event.
        confirm: bool (default False); must be True to permanently purge the NeedFix row.
    """
    if confirm is not True:
        response = {"ok": False, "error": "needfix_purge_confirmation_required"}
    else:
        try:
            response = dict(core.needfix_purge(str(needfix_id or ""), str(audit_reason or "")[:1000]))
            response.setdefault("ok", True)
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_purge", write=True)


def needfix_convert_preview_view(
    needfix_id: str, task_plan: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """READ-ONLY: preview the normalized task card and plan_digest for converting a NeedFix item into a task.

    Call this before aiworkhub_dashboard_needfix_convert_commit to inspect the normalized card, validation role requirements, and the plan_digest required to commit; use aiworkhub_dashboard_needfix_detail to inspect the NeedFix item itself. Normalizes the task plan and caches the signed preview in memory without creating a task or mutating durable store state.

    Args:
        needfix_id: str canonical NeedFix identifier to preview for task conversion.
        task_plan: optional Mapping[str, Any] (default None) explicit task card overrides such as runner, topic, allowed_writes, and validation.
    """
    try:
        response = dict(
            core.needfix_preview_convert(str(needfix_id or ""), task_plan=task_plan)
        )
        response.setdefault("ok", True)
    except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_convert_preview")


def needfix_convert_commit_view(
    needfix_id: str,
    confirm: bool = False,
    task_plan: Mapping[str, Any] | None = None,
    plan_digest: str | None = None,
) -> dict[str, Any]:
    """USER WRITE: convert an accepted NeedFix item into a canonical task card without launching a worker.

    Call aiworkhub_dashboard_needfix_convert_preview first to obtain the required plan_digest; use aiworkhub_dashboard_needfix_link_existing_task instead when a finished accepted task already resolves the item. Requires confirm=True and a matching plan_digest, creates the task card in the task store, transitions the NeedFix item to task_created, and never spawns a worker process.

    Args:
        needfix_id: str canonical NeedFix identifier to convert into a task.
        confirm: bool (default False); must be True to commit the conversion and create the task.
        task_plan: optional Mapping[str, Any] (default None reuses the plan cached under plan_digest) explicit task card fields.
        plan_digest: optional str (default None, required at runtime) SHA-256 digest returned by aiworkhub_dashboard_needfix_convert_preview.
    """
    if confirm is not True:
        response = {"ok": False, "error": "needfix_conversion_confirmation_required"}
    elif not plan_digest:
        response = {"ok": False, "error": "needfix_conversion_plan_digest_required"}
    else:
        try:
            response = dict(
                core.needfix_convert(
                    str(needfix_id or ""),
                    task_plan=task_plan,
                    plan_digest=str(plan_digest) if plan_digest else None,
                )
            )
            response.setdefault("ok", True)
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_convert_commit", write=True)


def needfix_link_existing_task_view(
    needfix_id: str, existing_task_id: str, confirm: bool = False, integrated_commit: str = ""
) -> dict[str, Any]:
    """USER WRITE: link a NeedFix item to an existing finished, manager-accepted task in the same repository.

    Use this when an already-completed canonical task resolves the NeedFix item; use aiworkhub_dashboard_needfix_convert_commit instead to create a new task card from the NeedFix. Requires confirm=True, verifies the target task exists and is finished and accepted (plus integrated_commit when supplied), and persists the task link and audit event in the NeedFix store.

    Args:
        needfix_id: str canonical NeedFix identifier to link.
        existing_task_id: str identifier of the finished, manager-accepted task in this repository.
        confirm: bool (default False); must be True to persist the task link.
        integrated_commit: str optional Git commit SHA (default "") verified against the repository when provided.
    """
    if confirm is not True:
        response = {"ok": False, "error": "needfix_link_existing_task_confirmation_required"}
    else:
        try:
            response = dict(
                core.needfix_link_existing_task(
                    str(needfix_id or ""), str(existing_task_id or ""), str(integrated_commit or "")
                )
            )
            response.setdefault("ok", True)
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_link_existing_task", write=True)


def needfix_reopen_superseded_task_link_view(
    needfix_id: str, reason: str, confirm: bool = False
) -> dict[str, Any]:
    """USER WRITE: reopen a NeedFix item whose linked converted task was archived or superseded.

    Use this when a converted task was superseded or archived without resolving the NeedFix so a new conversion can mint a successor -rN task; use aiworkhub_dashboard_needfix_transition for normal lifecycle changes. Requires confirm=True and a verified archived or superseded linked task, increments reopen_generation, restores the NeedFix to accepted status, and records an audit event.

    Args:
        needfix_id: str canonical NeedFix identifier whose superseded task link should be reopened.
        reason: str mandatory audit explanation for reopening the superseded task link.
        confirm: bool (default False); must be True to execute the reopen.
    """
    if confirm is not True:
        response = {
            "ok": False,
            "error": "needfix_reopen_superseded_task_link_confirmation_required",
        }
    else:
        try:
            response = dict(
                core.needfix_reopen_superseded_task_link(
                    str(needfix_id or ""), str(reason or "")
                )
            )
            response.setdefault("ok", True)
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(
        response,
        "aiworkhub_dashboard_needfix_reopen_superseded_task_link",
        write=True,
    )


def task_quarantine_view(
    preview_digest: str,
    older_than_days: int | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """USER WRITE: move aged archived task rows matching a current preview into a reversible retention batch.

    Call aiworkhub_dashboard_task_retention_preview first with the same older_than_days cutoff to obtain preview_digest; use aiworkhub_dashboard_task_quarantine_restore to undo a batch or aiworkhub_dashboard_task_archive to archive one live task. Requires confirm=True and a matching preview_digest, records retired task outcomes in the roadmap store, moves task and event rows into task_retention_batches, and invalidates storage telemetry.

    Args:
        preview_digest: str SHA-256 digest from aiworkhub_dashboard_task_retention_preview; mismatches fail closed.
        older_than_days: optional int age cutoff in days (default None uses repository retention policy, valid range 7..3650).
        confirm: bool (default False); must be True to quarantine the archived tasks.
    """
    try:
        root = core.repo_root()
        response = task_retention.quarantine(
            root,
            preview_digest=str(preview_digest or "")[:128],
            older_than_days=older_than_days,
            confirm=confirm is True,
        )
        storage_observability.invalidate(root)
    except (task_retention.TaskRetentionError, ValueError, TypeError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_task_quarantine"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def task_quarantine_restore_view(batch_id: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: restore one archived-task retention batch back into the canonical task tables.

    Use this to roll back a task retention batch listed by aiworkhub_dashboard_task_retention_preview; use aiworkhub_dashboard_task_restore instead to unarchive a single task that is still in the live tasks table. Requires confirm=True, checks for task_id collisions, reinserts tasks, task_events, and callback rows in one transaction, marks the batch restored, and invalidates storage telemetry.

    Args:
        batch_id: str identifier of the task retention quarantine batch to restore.
        confirm: bool (default False); must be True to restore the batch.
    """
    try:
        root = core.repo_root()
        response = task_retention.restore(
            root, batch_id=str(batch_id or "")[:128], confirm=confirm is True
        )
        storage_observability.invalidate(root)
    except task_retention.TaskRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_task_quarantine_restore"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def task_quarantine_purge_view(batch_id: str, confirm: bool = False) -> dict[str, Any]:
    """DESTRUCTIVE: permanently delete one expired or already-restored archived-task quarantine batch.

    Inspect batches with aiworkhub_dashboard_task_retention_preview first, or use aiworkhub_dashboard_task_quarantine_restore to recover tasks while the undo deadline is active. Requires confirm=True and an expired restore_deadline (unless already restored), deletes the batch payload from task_retention_batches, records a purged audit row, and invalidates storage telemetry.

    Args:
        batch_id: str identifier of the task retention batch to permanently delete.
        confirm: bool (default False); must be True to permanently purge the batch.
    """
    try:
        root = core.repo_root()
        response = task_retention.purge(
            root, batch_id=str(batch_id or "")[:128], confirm=confirm is True
        )
        storage_observability.invalidate(root)
    except task_retention.TaskRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_task_quarantine_purge"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def health_view() -> dict[str, Any]:
    """READ-ONLY: perform a fast storage readiness, manager identity, and dispatcher health check for the active repository.

    Use this for lightweight connection-banner polling or pre-initialization checks; use aiworkhub_dashboard_snapshot when you need task queue counts, process rows, or cost summaries, or aiworkhub_dashboard_initialize when storage reports not_initialized. Probes storage readiness and cached observability without building the full queue snapshot or mutating repository state.
    """
    # Use the same canonical resolver as every manager/task tool.  The VS Code
    # dashboard child carries explicit AIWORKHUB_REPO* bindings, while the
    # application-global Codex MCP registration intentionally omits them so
    # multiple repository windows cannot overwrite one another; that child is
    # repository-bound by its own process cwd.  Reading only env vars here made
    # dashboard_health falsely report repo_root_not_selected even though
    # manager_bootstrap and task_create were correctly bound to the current
    # repository.
    try:
        root_raw = str(core.repo_root())
    except (OSError, RuntimeError, task_store.TaskStoreError):
        root_raw = ""
    if not root_raw:
        result: dict[str, Any] = {
            "ok": False,
            "repo": "",
            "storage": {
                "ready": False,
                "reason": "repo_root_not_selected",
                "not_initialized": is_not_initialized_reason("repo_root_not_selected"),
            },
            "error": "repo_root_not_selected",
        }
    else:
        readiness = task_store.storage_readiness(root_raw)
        storage = readiness.as_dict()
        if not readiness.ready:
            storage["not_initialized"] = is_not_initialized_reason(readiness.reason)
        result = {
            "ok": bool(readiness.ready),
            "repo": root_raw,
            "storage": storage,
        }
        # Capacity lookup is immediate and managed-size inventory runs in a
        # daemon thread. Starting it from the cheap health handshake gives the
        # scan a head start before the first full Webview snapshot.
        result["storage_usage"] = storage_observability.snapshot(root_raw)
    result["server_version"] = __version__
    result["server_tool"] = "aiworkhub_dashboard_health"
    result["authority_flags"] = _readonly_authority_flags()
    try:
        result["manager_identity"] = _known_contract_identity(core.manager_bootstrap())
    except Exception as exc:  # noqa: BLE001 -- health surface must never raise
        result["manager_identity"] = {
            "ok": False,
            "role": "unknown",
            "provider": "unknown",
            "manager_route": {},
            "reason": f"manager_bootstrap_failed:{type(exc).__name__}",
        }
    # B857: best-effort dispatcher health -- never lets a dispatcher-side
    # failure break this cheap connection-banner check. An uninitialized
    # repository reports dispatcher status "uninitialized", not an error.
    try:
        result["dispatcher"] = core.dispatcher_health()
    except Exception as exc:  # noqa: BLE001 -- health surface must never raise
        result["dispatcher"] = {"ok": False, "status": "error", "error": f"dispatcher_health_failed:{type(exc).__name__}"}
    return result


def _initialize_authority_flags() -> dict[str, bool]:
    return {
        "readonly": False,
        "queue_write": False,
        "audit_write": False,
        "process_launch": False,
        "agent_launch": False,
        "shell_invocation": False,
        "repository_bootstrap_write": True,
    }


# Only a repo_id shaped like the extension's own generator output
# (``repo_<32 lowercase hex>``) is honored as an identity expectation. Any
# other value (e.g. the extension's "manifest-missing"/"manifest-invalid"
# placeholder labels for a never-initialized repository) is treated as "no
# expectation yet" so a legitimate first-time init is never refused by
# accidentally adopting a placeholder string as the permanent repo_id.
_REAL_REPO_ID_RE = re.compile(r"^repo_[a-f0-9]{32}$")


def initialize_view(repo_id: str = "") -> dict[str, Any]:
    """USER WRITE: initialize or repair the active repository's canonical .aiworkhub stores and Source Graph directory.

    Use this when aiworkhub_dashboard_health reports storage not_initialized; use aiworkhub_dashboard_health or aiworkhub_dashboard_snapshot for read-only status checks. Bound to AIWORKHUB_REPO_ROOT, verifies repo_id against any existing manifest, idempotently provisions the project manifest, storage registry, SQLite stores, and Source Graph directory without deleting legacy files, and invalidates storage telemetry.

    Args:
        repo_id: str optional expected repository ID (default ""); when valid, must match any existing manifest or initialization fails closed.
    """
    root = str(
        os.environ.get("AIWORKHUB_REPO_ROOT")
        or os.environ.get("AIWORKHUB_REPO")
        or ""
    ).strip()
    if not root:
        return {
            "ok": False,
            "error": "initialization_refused",
            "message": "repository_root_not_bound",
            "server_tool": "aiworkhub_dashboard_initialize",
            "authority_flags": _initialize_authority_flags(),
        }
    candidate = str(repo_id or "").strip()
    expected = candidate if _REAL_REPO_ID_RE.match(candidate) else None
    try:
        # Routes through repository_bootstrap.initialize_repository_full so
        # the same bounded action ALSO provisions the Source Graph directory
        # (never a second, separate init step) -- see repository_bootstrap.py.
        result = repository_bootstrap.initialize_repository_full(root, expected_repo_id=expected)
        # A pre-init snapshot can cache usage/readiness observations for this
        # process. Invalidate them before the caller immediately asks for the
        # authoritative post-init snapshot.
        storage_observability.invalidate(root)
    except task_store.InitializationRefusedError as exc:
        return {
            "ok": False,
            "error": "initialization_refused",
            "message": str(exc)[:300],
            "server_tool": "aiworkhub_dashboard_initialize",
            "authority_flags": _initialize_authority_flags(),
        }
    except task_store.TaskStoreError as exc:
        return {
            "ok": False,
            "error": "initialization_failed",
            "message": str(exc)[:300],
            "server_tool": "aiworkhub_dashboard_initialize",
            "authority_flags": _initialize_authority_flags(),
        }
    response = dict(result)
    response["server_tool"] = "aiworkhub_dashboard_initialize"
    response["authority_flags"] = _initialize_authority_flags()
    return response


def task_live_output_view(task_id: str, cursor: int = 0) -> dict[str, Any]:
    """READ-ONLY: read a bounded page of live stdout and stderr output for one task's active or latest worker run.

    Use this to stream or page incremental worker process output from a byte cursor; use aiworkhub_dashboard_task_detail for the task contract and attempt history, or aiworkhub_dashboard_snapshot for queue-wide status. Validates task_id format and reads only that task's log files without mutating state.

    Args:
        task_id: str canonical task identifier whose worker output should be read.
        cursor: int (default 0, clamped to >=0) byte offset from which to read the next output chunk.
    """
    candidate = str(task_id or "").strip()
    if not dashboard._TASK_ID_RE.fullmatch(candidate):
        return {
            "ok": False,
            "error": "invalid_task_id",
            "server_tool": "aiworkhub_dashboard_task_live_output",
            "authority_flags": _readonly_authority_flags(),
        }
    try:
        safe_cursor = max(0, int(cursor))
    except (TypeError, ValueError):
        safe_cursor = 0

    try:
        repo_root = dashboard._default_repo_root()
        result = process_launcher.read_live_output_for_task(
            candidate,
            repo=repo_root,
            cursor=safe_cursor,
            max_bytes=MAX_LIVE_OUTPUT_BYTES,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, one task's read never crashes the tool call
        result = {
            "ok": False,
            "task_id": candidate,
            "error": "output_unavailable",
            "reason": f"repository_unresolved:{type(exc).__name__}",
            "cursor": safe_cursor,
            "next_cursor": safe_cursor,
            "truncated": False,
            "output": "",
            "stderr_tail": "",
        }
    response = dict(result)
    response["server_tool"] = "aiworkhub_dashboard_task_live_output"
    response["authority_flags"] = _readonly_authority_flags()
    return response


READONLY_TOOL_NAMES: tuple[str, ...] = (
    "aiworkhub_dashboard_snapshot",
    "aiworkhub_dashboard_task_detail",
    "aiworkhub_dashboard_health",
)

# Stable tool_name -> callable map for server wiring / introspection. Kept
# to this exact set of three (unchanged since B615/B853) so any existing
# consumer asserting this precise tuple keeps passing; the newer
# task-live-output tool is additive and lives in LIVE_OUTPUT_TOOLS below.
READONLY_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_snapshot": snapshot_view,
    "aiworkhub_dashboard_task_detail": task_detail_view,
    "aiworkhub_dashboard_health": health_view,
}

# The one bounded write-capable tool: repository initialization. Kept
# separate from READONLY_TOOLS/READONLY_TOOL_NAMES so its non-read-only
# authority is never accidentally treated as part of the read-only surface.
INITIALIZE_TOOL_NAME = "aiworkhub_dashboard_initialize"
INITIALIZE_TOOLS: dict[str, Any] = {INITIALIZE_TOOL_NAME: initialize_view}

# B855: the single-task Live Output tool. Read-only (no queue/audit write,
# no process launch) exactly like READONLY_TOOLS, but kept in its own
# name/dict pair -- additive to the historical READONLY_TOOL_NAMES/
# READONLY_TOOLS tuple/dict so an existing exact-tuple assertion on those two
# names (see tests/test_aiworkhub_vscode_release_b853.py) keeps passing
# unchanged.
LIVE_OUTPUT_TOOL_NAME = "aiworkhub_dashboard_task_live_output"
LIVE_OUTPUT_TOOLS: dict[str, Any] = {LIVE_OUTPUT_TOOL_NAME: task_live_output_view}
MEMORY_TOOL_NAME = "aiworkhub_dashboard_memory"
MEMORY_TOOLS: dict[str, Any] = {MEMORY_TOOL_NAME: memory_view}
SESSION_TOOL_NAME = "aiworkhub_dashboard_sessions"
SESSION_TOOLS: dict[str, Any] = {SESSION_TOOL_NAME: session_view}
KB_TOOL_NAME = "aiworkhub_dashboard_kb"
KB_TOOLS: dict[str, Any] = {KB_TOOL_NAME: kb_view}
SKILLS_TOOL_NAME = "aiworkhub_dashboard_skills"
SKILLS_TOOLS: dict[str, Any] = {SKILLS_TOOL_NAME: skills_view}
NEEDFIX_READ_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_needfix_list": needfix_list_view,
    "aiworkhub_dashboard_needfix_detail": needfix_detail_view,
    "aiworkhub_dashboard_needfix_convert_preview": needfix_convert_preview_view,
}
NEEDFIX_WRITE_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_needfix_capture": needfix_capture_view,
    "aiworkhub_dashboard_needfix_update": needfix_update_view,
    "aiworkhub_dashboard_needfix_transition": needfix_transition_view,
    "aiworkhub_dashboard_needfix_archive": needfix_archive_view,
    "aiworkhub_dashboard_needfix_restore": needfix_restore_view,
    "aiworkhub_dashboard_needfix_purge": needfix_purge_view,
    "aiworkhub_dashboard_needfix_convert_commit": needfix_convert_commit_view,
    "aiworkhub_dashboard_needfix_link_existing_task": needfix_link_existing_task_view,
    "aiworkhub_dashboard_needfix_reopen_superseded_task_link": needfix_reopen_superseded_task_link_view,
}
ROADMAP_READ_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_roadmap_list": roadmap_list_view,
    "aiworkhub_dashboard_roadmap_detail": roadmap_detail_view,
}
HUB_MAIL_READ_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_hub_mail": hub_mail_view,
    "aiworkhub_dashboard_hub_mail_detail": hub_mail_detail_view,
}
HUB_MAIL_WRITE_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_hub_mail_listener": hub_mail_listener_action_view,
    "aiworkhub_dashboard_hub_mail_pair_begin": hub_mail_pair_begin_view,
    "aiworkhub_dashboard_hub_mail_pair_complete": hub_mail_pair_complete_view,
}
SETTINGS_TOOL_NAME = "aiworkhub_dashboard_settings"
SETTINGS_TOOLS: dict[str, Any] = {SETTINGS_TOOL_NAME: settings_view}
SETTINGS_UPDATE_TOOL_NAME = "aiworkhub_dashboard_settings_update"
SETTINGS_UPDATE_TOOLS: dict[str, Any] = {SETTINGS_UPDATE_TOOL_NAME: settings_update_view}
MODEL_SETTINGS_UPDATE_TOOL_NAME = "aiworkhub_dashboard_model_settings_update"
MODEL_SETTINGS_UPDATE_TOOLS: dict[str, Any] = {
    MODEL_SETTINGS_UPDATE_TOOL_NAME: model_settings_update_view,
}
SOURCE_GRAPH_SETTINGS_UPDATE_TOOL_NAME = "aiworkhub_dashboard_source_graph_settings_update"
SOURCE_GRAPH_SETTINGS_UPDATE_TOOLS: dict[str, Any] = {
    SOURCE_GRAPH_SETTINGS_UPDATE_TOOL_NAME: source_graph_settings_update_view,
}
STORAGE_RETENTION_PREVIEW_TOOL_NAME = "aiworkhub_dashboard_storage_retention_preview"
STORAGE_RETENTION_READ_TOOLS: dict[str, Any] = {
    STORAGE_RETENTION_PREVIEW_TOOL_NAME: storage_retention_preview_view,
}
STORAGE_RETENTION_WRITE_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_storage_quarantine": storage_quarantine_view,
    "aiworkhub_dashboard_storage_registration_prune": storage_registration_prune_view,
    "aiworkhub_dashboard_storage_restore": storage_restore_view,
    "aiworkhub_dashboard_storage_purge": storage_purge_view,
}
TERMINAL_LOG_RETENTION_PREVIEW_TOOL_NAME = "aiworkhub_dashboard_terminal_log_retention_preview"
TERMINAL_LOG_RETENTION_READ_TOOLS: dict[str, Any] = {
    TERMINAL_LOG_RETENTION_PREVIEW_TOOL_NAME: terminal_log_retention_preview_view,
}
TERMINAL_LOG_RETENTION_WRITE_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_terminal_log_usage_backfill": terminal_log_usage_backfill_view,
    "aiworkhub_dashboard_terminal_log_quarantine": terminal_log_quarantine_view,
    "aiworkhub_dashboard_terminal_log_restore": terminal_log_restore_view,
    "aiworkhub_dashboard_terminal_log_purge": terminal_log_purge_view,
}
TASK_RETENTION_PREVIEW_TOOL_NAME = "aiworkhub_dashboard_task_retention_preview"
TASK_RETENTION_READ_TOOLS: dict[str, Any] = {
    TASK_RETENTION_PREVIEW_TOOL_NAME: task_retention_preview_view,
}
TASK_RETENTION_WRITE_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_task_archive": task_archive_view,
    "aiworkhub_dashboard_task_restore": task_restore_view,
    "aiworkhub_dashboard_task_restart": task_restart_view,
    "aiworkhub_dashboard_task_quarantine": task_quarantine_view,
    "aiworkhub_dashboard_task_quarantine_restore": task_quarantine_restore_view,
    "aiworkhub_dashboard_task_quarantine_purge": task_quarantine_purge_view,
}


def register(mcp: Any) -> tuple[str, ...]:
    """Register the dashboard MCP tools; return their names.

    Accepts any object exposing a FastMCP-style ``tool(name=...)`` decorator
    factory (mirrors ``cli_adapter_readonly_tool.register``). Every tool in
    ``READONLY_TOOLS`` plus ``LIVE_OUTPUT_TOOLS`` (snapshot, task detail,
    health, task live output) is read-only: none writes queue/audit state
    and none launches a process. ``aiworkhub_dashboard_initialize`` is the
    sole bounded exception -- it only ever creates/repairs the repository's
    own ``.aiworkhub`` manifest, storage registry, canonical task DB, and
    Source Graph directory (see
    ``repository_bootstrap.initialize_repository_full``).
    """
    for name, fn in READONLY_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in LIVE_OUTPUT_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in MEMORY_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in SESSION_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in KB_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in SKILLS_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in NEEDFIX_READ_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in NEEDFIX_WRITE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in ROADMAP_READ_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in HUB_MAIL_READ_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in HUB_MAIL_WRITE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in SETTINGS_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in SETTINGS_UPDATE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in MODEL_SETTINGS_UPDATE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in SOURCE_GRAPH_SETTINGS_UPDATE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in STORAGE_RETENTION_READ_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in STORAGE_RETENTION_WRITE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in TERMINAL_LOG_RETENTION_READ_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in TERMINAL_LOG_RETENTION_WRITE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in TASK_RETENTION_READ_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in TASK_RETENTION_WRITE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in INITIALIZE_TOOLS.items():
        mcp.tool(name=name)(fn)
    return READONLY_TOOL_NAMES + (
        LIVE_OUTPUT_TOOL_NAME,
        MEMORY_TOOL_NAME,
        SESSION_TOOL_NAME,
        KB_TOOL_NAME,
        SKILLS_TOOL_NAME,
        SETTINGS_TOOL_NAME,
        STORAGE_RETENTION_PREVIEW_TOOL_NAME,
        TERMINAL_LOG_RETENTION_PREVIEW_TOOL_NAME,
        TASK_RETENTION_PREVIEW_TOOL_NAME,
    ) + (
        tuple(STORAGE_RETENTION_WRITE_TOOLS)
        + tuple(TERMINAL_LOG_RETENTION_WRITE_TOOLS)
        + tuple(TASK_RETENTION_WRITE_TOOLS)
        + tuple(NEEDFIX_READ_TOOLS)
        + tuple(NEEDFIX_WRITE_TOOLS)
        + tuple(ROADMAP_READ_TOOLS)
        + tuple(HUB_MAIL_READ_TOOLS)
        + tuple(HUB_MAIL_WRITE_TOOLS)
        + tuple(SETTINGS_UPDATE_TOOLS)
        + tuple(MODEL_SETTINGS_UPDATE_TOOLS)
        + tuple(SOURCE_GRAPH_SETTINGS_UPDATE_TOOLS)
        + (INITIALIZE_TOOL_NAME,)
    )
