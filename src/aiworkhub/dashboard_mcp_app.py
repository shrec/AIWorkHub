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

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Mapping

from aiworkhub import (
    __version__,
    core,
    context_graph,
    dashboard,
    feature_settings,
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


MAX_MODEL_POLICY_CATALOG_ROWS = 64

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
# a list a producer already cut to 64 or 128, which made the bound's own
# ``truncated`` signal unable to fire and published the survivors' count as the
# host's total. A provider was therefore understated, and a configured route
# the producer had omitted was labelled as one no source ever offered.
#
# These mirror those producers' caps -- ``workforce_catalog``'s OpenCode row
# cap and ``vscode_lm_bridge``'s observed-model slice -- and are used ONLY as
# evidence that an upstream bound bound, never to re-impose one here. A list
# that comes back at its producer's ceiling is a list whose tail is unknown,
# and that is what gets published rather than a total this module cannot see.
OPENCODE_UPSTREAM_IDENTITY_CEILING = 64
EDITOR_UPSTREAM_MODEL_CEILING = 128

# The OpenCode snapshot this module RECOVERS is bounded by its own producer too,
# and not by the generic source ceiling above. ``repo_policy`` publishes
# ``provider_observed_models`` either as ``parse_opencode_models_output``'s
# 64-row read of ``opencode models`` or as a 128-row slice of the host's
# observed list, so a recovered list can never reach
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
    # repo_policy publishes it as the parser's 64-row read or as a 128-row
    # slice of the host's observed list, so it can never reach
    # MAX_MODEL_POLICY_SOURCE_ROW_CEILING: tested against that number alone the
    # floor was unreachable in production, and an at-cap host published its
    # recovered length as an exact total. The recovery ceiling is still checked
    # below it, because a snapshot read only that far has an unseen tail for
    # this module's own reason, while a list longer than both producer caps was
    # cut by neither and stays a measurement.
    offered_count = len(offered)
    upstream_ceiling_bound = producer_capped and (
        not offered
        or offered_count in OPENCODE_UPSTREAM_OFFERED_CEILINGS
        or offered_count >= MAX_MODEL_POLICY_SOURCE_ROW_CEILING
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
    # Every ingestion source above can arrive already truncated, and none of
    # them says so on its own. ``parse_opencode_models_output`` stops at its own
    # 64-row cap before ``opencode_identities_from_preflight`` returns, and the
    # editor bridge head-slices ``observed_models`` at 128 -- both before this
    # module reads a single identity, so MAX_MODEL_POLICY_SOURCE_ROWS cannot
    # bind on a discovery source and the pin reservation has nothing to reserve.
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
                "discovered_from_opencode"
                if probe == "opencode"
                else "discovered_from_editor"
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
    # All three ingestion sources are bounded before the render bound sees a
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
            # The owner's own declarations are bounded too. models.json is read
            # up to declared_leaf_limit, and a file larger than that has leaves
            # this view never saw -- so it also has pins it never reserved. That
            # is published rather than inferred, because a decision that was
            # never read is otherwise indistinguishable from one that lost.
            "declared_leaf_count": declared_leaf_total,
            "declared_leaf_limit": MAX_MODEL_POLICY_DECLARED_LEAVES,
            "declared_leaves_truncated": declared_leaves_truncated,
            # Rows were lost to the render bound or to any of the three
            # ingestion caps before it, models.json itself was read short, or a
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
    """READ-ONLY: repository-local feature switches and capabilities."""
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
    """USER WRITE: atomically update repository Source Graph languages."""

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
    """USER WRITE: atomically update bounded repository feature switches."""
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
    """USER WRITE: update one repository-local model routing switch."""
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


def _compact_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
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


def snapshot_view(
    full: bool = False,
    previous: Mapping[str, Any] | None = None,
    previous_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """READ-ONLY: canonical dashboard snapshot for the native Webview.

    Calls ``dashboard.build_snapshot()`` -- the SAME builder the HTTP
    dashboard's ``/api/snapshot`` route uses -- and returns the identical
    ``status_counts``, ``tasks``, ``row_counts``, ``summaries``,
    ``cost_usage``, ``agent_processes``, and ``warnings`` shape the existing
    dashboard.js already renders when ``full=true``. The default manager call
    is a bounded operational summary; the native Webview requests full mode
    explicitly. Adds no second SQLite/taskctl read.
    """
    started = time.perf_counter()
    _debug_trace("snapshot.begin")
    build_kwargs: dict[str, Any] = {"summary_only": not full}
    prior_snapshot = previous if previous is not None else previous_snapshot
    if prior_snapshot is not None:
        build_kwargs["previous"] = prior_snapshot
    with _SNAPSHOT_BUILD_LOCK:
        snapshot = dict(
            _debug_stage(
                "dashboard.build_snapshot",
                lambda: dashboard.build_snapshot(**build_kwargs),
            )
        )
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
    except Exception:  # noqa: BLE001 -- route diagnostics are optional
        snapshot["manager_identity_target"] = {}
    snapshot["server_tool"] = "aiworkhub_dashboard_snapshot"
    snapshot["authority_flags"] = _readonly_authority_flags()
    # ``build_snapshot`` starts the non-blocking storage inventory before the
    # heavier full reads. Re-sample its in-memory cache after those reads so a
    # scan that finished meanwhile is visible in this same response instead of
    # leaving the Webview on "Calculating" until a later polling interval.
    try:
        current_storage = snapshot.get("storage_usage")
        latest_storage = storage_observability.snapshot(core.repo_root())
        if full and isinstance(current_storage, dict):
            # The normal inventory is ~3.5 s on the live 19.9 GB store while
            # the full snapshot now hydrates in ~3 s. Give that already-running
            # thread one small bounded settle window so the same response can
            # publish its result. Never wait for an actually slow filesystem.
            settle_deadline = time.monotonic() + 3.0
            while (
                latest_storage.get("scan_status") in {"scanning", "refreshing"}
                and time.monotonic() < settle_deadline
            ):
                time.sleep(0.05)
                latest_storage = storage_observability.snapshot(core.repo_root())
        if isinstance(current_storage, dict) and isinstance(latest_storage, dict):
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
    else:
        result = _debug_stage(
            "compact_snapshot",
            lambda: _bound_snapshot(_compact_snapshot(snapshot)),
        )
    _debug_trace(
        "snapshot.end",
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
        response_bytes=_byte_len(result),
    )
    return result


def task_detail_view(task_id: str) -> dict[str, Any]:
    """READ-ONLY: canonical detail for exactly one bounded task_id.

    Validates ``task_id`` with the identical pattern the HTTP dashboard's
    ``/api/task`` route enforces (``dashboard._TASK_ID_RE``) BEFORE ever
    calling the provider, then calls ``dashboard.build_task_detail()`` -- the
    same canonical builder ``/api/task`` uses. An invalid or unknown task_id
    returns a bounded ``ok: false`` object; this never raises and never
    reaches a write path.
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
    """READ-ONLY: newest canonical AI Memory rows for this repository.

    The database path is resolved exclusively through the repo-bound storage
    registry and opened read-only. Browsing never mutates access counters.
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
    """READ-ONLY: newest canonical Session Manager transcript evidence.

    Session continuity is represented by the canonical transcript graph's
    bounded ``documents`` rows. The dashboard never opens a legacy session
    file and never mutates access/checkpoint state while browsing.
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
    """READ-ONLY: newest canonical repository KB entries."""

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
    """READ-ONLY: measured skill selection and injection coverage.

    Counts only, never rows: skill totals by lifecycle, how many ACTIVE records
    a card context could actually reach, how many recorded receipts selected and
    how many injected, and the consecutive run of newest receipts that injected
    nothing. The streak is the point. A skill system whose every selection comes
    back empty is indistinguishable from a quiet one until something counts the
    run, and this repository measured 24 empty selections in a row while every
    surface reported a well-formed, bounded, entirely healthy-looking packet.

    An absent or unreadable store reports ``measured: False`` with a reason. It
    does NOT report zeros -- "no skills were injected" and "nobody looked" are
    different facts and a panel that renders them identically is the defect.

    A failure ABOVE the projection answers in that identical shape. Dropping
    ``schema_id``/``unavailable_reason``/``skills``/``selection`` here would
    make a renderer branch on WHICH layer failed before it could tell whether
    the surface was measured, and a missing block reads as an absent one.
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


def storage_retention_preview_view() -> dict[str, Any]:
    """READ-ONLY: fresh repository-scoped cleanup preview and batch list."""
    try:
        root = core.repo_root()
        response = storage_retention.preview(root)
        response["quarantine"] = storage_retention.list_batches(root)
    except storage_retention.StorageRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_storage_retention_preview"
    response["authority_flags"] = _readonly_authority_flags()
    return response


def storage_quarantine_view(preview_digest: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: quarantine one still-current preview after explicit consent."""
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
    """USER WRITE: prune exact stale AIWorkHub Git registrations."""
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
    """USER WRITE: restore one repository-owned quarantine batch."""
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
    """USER WRITE: permanently purge one expired quarantine batch."""
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
    """READ-ONLY: terminal-run log preview; the canonical ledger is excluded."""
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
    """USER WRITE: move one exact set of terminal files to repo-local quarantine."""
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
    """USER WRITE: recover canonical usage receipts before log retention."""

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
    """USER WRITE: restore one terminal-log quarantine batch without overwrite."""
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
    """USER WRITE: permanently purge one expired terminal-log batch."""
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
    """READ-ONLY: old archived-task cleanup preview and undo batches."""
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
    """USER WRITE: archive one non-active task without deleting its evidence."""
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
    """USER WRITE: restore one still-live archived task to its prior state."""
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
    """READ-ONLY: bounded canonical NeedFix inbox for the native Webview."""
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
    """READ-ONLY: one bounded NeedFix row plus its audit events."""
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
    """READ-ONLY: bounded Roadmap outcomes joined to Task-DAG truth."""
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
        }
    except (roadmap_store.RoadmapError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240], "entries": []}
    return _needfix_response(response, "aiworkhub_dashboard_roadmap_list")


def roadmap_detail_view(roadmap_id: str, event_limit: int = 50) -> dict[str, Any]:
    """READ-ONLY: one Roadmap outcome with task/dependency and audit truth."""
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


def needfix_capture_view(
    title: str,
    description: str,
    kind: str = "other",
    severity: str = "medium",
    scope: str | None = None,
    tags: list[str] | None = None,
    include_item: bool = False,
) -> dict[str, Any]:
    """USER WRITE: explicitly capture one dashboard-authored proposal.

    ``kind`` vocabulary: bug, feature, improvement, idea, technical_debt,
    optimization, benchmark_gap, documentation_drift, security_risk,
    investigation, roadmap_candidate, refactor, security, docs, other
    (synonyms gap/defect/performance/design/dead_code/test_coverage are
    normalised and reported as ``kind_normalized``). ``severity``: critical,
    high, medium, low, info. Replies with a capture receipt: id, dedupe_key,
    status, created_at, provenance.origin, ``deduped``/``existing_id`` when
    the dedupe key matched a live row, plus the compact item
    (``include_item=True`` adds description/scope/provenance/evidence).
    """
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
    """USER WRITE: update bounded mutable NeedFix fields.

    Replies with a delta receipt: id, status, fields_changed, updated_at,
    evidence_keys_after, event_id, kind_normalized, plus the compact item
    (``include_item=True`` adds description/scope/provenance/evidence).
    """
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
    """USER WRITE: one explicit, confirmed NeedFix lifecycle transition.

    Replies with a delta receipt (``aiworkhub.needfix_transition_receipt.v1``):
    id, action, status_before, status_after, updated_at, readiness_score,
    converted_task_id, event_id, the recorded ``events``, and the compact
    item (``include_item=True`` adds description/scope/provenance/evidence).
    ``promote_to="accepted"`` with ``action="triage"`` records the triage and
    the accept as two audit events in one call, with one reason; the
    confirm gate and the state machine are unchanged (92 of 113 triages in
    27 sessions were followed by an accept within six calls).
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
    """READ-ONLY: normalized executable conversion card plus its plan_digest."""
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
    """USER WRITE: explicitly create a task card; never launch a worker.

    Every confirmed conversion must carry the exact ``plan_digest`` the
    matching preview returned, binding the commit to what the manager
    actually inspected. The rule is identical for an explicit
    ``task_plan`` and for the default scope-derived card: a missing
    digest, or a digest that no longer matches the freshly normalized
    card, fails closed instead of silently converting a plan nobody
    confirmed. Core's digest check runs only on the create path, so
    already-task_created retries keep short-circuiting without forcing a
    new create.
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
    needfix_id: str, existing_task_id: str, confirm: bool = False
) -> dict[str, Any]:
    """USER WRITE: manager-only, explicitly link a NeedFix to an existing, finished, accepted task."""
    if confirm is not True:
        response = {"ok": False, "error": "needfix_link_existing_task_confirmation_required"}
    else:
        try:
            response = dict(
                core.needfix_link_existing_task(str(needfix_id or ""), str(existing_task_id or ""))
            )
            response.setdefault("ok", True)
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_link_existing_task", write=True)


def needfix_reopen_superseded_task_link_view(
    needfix_id: str, reason: str, confirm: bool = False
) -> dict[str, Any]:
    """USER WRITE: reopen only an exact archived/superseded converted-task link."""
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
    """USER WRITE: quarantine a still-current old archived-task preview."""
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
    """USER WRITE: restore one archived-task quarantine batch."""
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
    """USER WRITE: permanently purge one expired archived-task batch."""
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
    """READ-ONLY: cheap liveness check for the Webview's connection banner.

    Reads canonical repo-local storage directly.  The native dashboard must
    not depend on a repository shipping ``AITools/taskctl.py`` merely to
    render its connection banner.  Never calls ``dashboard.build_snapshot``:
    polling stays cheap and cannot pull the queue/cost/process payload.
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
        result["manager_identity"] = core.manager_bootstrap()
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
    """WRITE (bootstrap-only): the one bounded, idempotent, fail-closed
    initialization action the Webview's "Initialize AIWorkHub" button
    invokes.

    Bound to the active repository via ``AIWORKHUB_REPO_ROOT`` (the same env
    var the extension host spawns this child process with -- never a fixed
    path relative to this package's own install location) and, when the
    caller supplies one, an expected ``repo_id`` that must match any existing
    manifest exactly. Creates/repairs the repository-local canonical stores.
    Fresh repositories receive empty compatible databases; an older registry
    may migrate only its explicitly declared repo-local legacy SQLite files
    with a consistent backup. Legacy files are never deleted.
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
    """READ-ONLY: bounded, single-task Live Output read for the dashboard's
    selected-task panel.

    Validates ``task_id`` with the same pattern every other task-scoped tool
    enforces (``dashboard._TASK_ID_RE``) BEFORE calling
    ``process_launcher.read_live_output_for_task`` -- the ONLY task this call
    ever reads process-log/stdout/stderr evidence for; there is no
    dashboard-wide fan-out across other tasks. An invalid task_id, or any
    unexpected failure resolving the active repository, returns a bounded
    ``ok: false`` object; this never raises.
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
        + tuple(SETTINGS_UPDATE_TOOLS)
        + tuple(MODEL_SETTINGS_UPDATE_TOOLS)
        + tuple(SOURCE_GRAPH_SETTINGS_UPDATE_TOOLS)
        + (INITIALIZE_TOOL_NAME,)
    )
