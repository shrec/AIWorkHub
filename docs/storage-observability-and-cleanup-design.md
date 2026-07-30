# AIWorkHub Storage Observability and Cleanup

Status: retained-worktree inventory/preview/quarantine/restore/expired-purge is
implemented; terminal-log, superseded-graph and obsolete-runtime adapters are
still planned. Automatic cleanup remains off by default. This design is
local-only and assumes no collaboration, RBAC, cloud service, telemetry upload,
or external account.

## Safety contract

The scanner recognizes storage only from a versioned, product-owned root registry. A root is eligible only when its expected AIWorkHub marker/manifest, installation or repository identity, and resolved filesystem identity all agree. Discovery by name, broad home-directory search, or ancestry alone never establishes ownership. Unknown, overlapping, missing, inaccessible, mutable, or ambiguously resolved roots are `Protected`; scanning and cleanup fail closed.

Cleanup never mutates the canonical task queue, callback authority required by active or review work, credentials/tokens, retained accepted evidence, active worker workspaces, live process logs, current Source Graph authority, current extension runtime/VSIX, foreign repositories, or another plugin's files. The queue and callback stores may be measured read-only through their owning APIs or safe snapshots; the cleanup component never opens their databases for write.

## Authoritative owned-root inventory

Actual absolute paths are supplied by the owning component's registry/manifest and displayed after redaction where needed; the templates below are not permission to scan a matching directory. Scope is `R` repository-local, `W` isolated window/worker-local, or `S` shared installation/user-local.

| ID | Scope | Registered root / owner proof | Contents | Default class and cleanup |
|---|---|---|---|---|
| `task-authority` | R/S | task-store API returns canonical DB identity | task databases, journals, lock files | **Canonical / Protected**, never cleanup |
| `callback-authority` | R/S | callback service manifest plus task references | callback inbox/outbox, receipts, review handoff data | **Canonical / Protected** while referenced; terminal unreferenced receipts become retained evidence only by policy |
| `source-graph-current` | R/S | Source Graph reports current generation and root ID | current indexes, manifests | **Active runtime / Protected** |
| `source-graph-old` | R/S | same registry; generation is not current and no reader lease | superseded indexes/build scratch | **Cache / Reclaimable** after age and lease proof |
| `worker-active` | W | coordinator lease, process identity and worktree Git identity agree | active isolated worktree and control files | **Active runtime / Protected** |
| `worker-retained` | W/S | terminal task record links immutable run/worktree ID | accepted/rejected run evidence and retained worktrees | **Retained evidence / Stable** until retention expires; then reclaimable |
| `process-log-live` | W/S | process registry maps PID/start token to log generation | stdout/stderr/event logs for live workers/services | **Log / Active / Protected** |
| `process-log-terminal` | W/S | terminal run with no live process token | rotated/terminal logs | **Log / Stable**, reclaimable by retention |
| `runtime-cache` | W/S | AIWorkHub runtime manifest and namespace marker | parsed metadata, transport/download/build caches | **Cache / Reclaimable**, except files with live leases |
| `session-memory-kb` | S | each local store reports its own root and authority generation | session state, AI Memory, KB databases/indexes | **Canonical / Stable / Protected**; only owner-declared disposable derived indexes may be cache |
| `generated-artifacts` | R/W/S | artifact manifest links task/run and hashes | reports, previews, bundles, generated evidence | **Retained evidence / Stable** if accepted/referenced; otherwise temporary or reclaimable by policy |
| `extension-current` | S | extension manifest identifies active version/runtime | active extension runtime and current VSIX | **Active runtime / Protected** |
| `extension-cache-old` | S | same publisher/plugin ID; not current or rollback-protected | older AIWorkHub VSIX/download/runtime caches | **Cache / Reclaimable** after retention |
| `route-current` | W/S | route registry references live window/process/task | window-to-repo/task routing | **Active runtime / Protected** |
| `route-stale` | W/S | all lease/process/window checks prove stale | orphaned AIWorkHub route records | **Stale / Reclaimable** after minimum age |
| `platform-temp` | W/S | per-operation marker contains owner ID, nonce, created time and expected parent root | incomplete scans, atomic-write scratch, abandoned staging | **Temporary / Reclaimable** only when nonce has no live operation |
| `quarantine` | S | cleanup journal and manifest own every entry | recoverable cleanup batches | **Retained evidence / Stable**, purge only after undo window |

Credential stores, OS keychains, environment/config tokens, foreign repos, generic OS temp, Git object stores shared with a foreign repository, and other extensions/plugins are explicitly outside all roots. They contribute neither total nor reclaimable bytes.

### Classification and no-double-counting

`Protected` is a safety overlay; `Stable`, `Active`, and `Reclaimable` are mutually exclusive dashboard labels. Every object also has one category above. Files are attributed to the deepest verified owned root. The scanner records `(volume/file-system ID, stable file ID/inode)` where available; hard links count allocated bytes once globally and logical bytes may be shown separately. Nested roots are excluded from the parent traversal. Symlink/reparse entries themselves may be counted as metadata, but targets are never followed during enumeration. Sparse/compressed files report allocated bytes for totals and logical bytes as supplemental data. If stable identity is unavailable, overlapping candidates are Protected and excluded from reclaimable totals.

## Cross-platform filesystem rules

Each configured path passes lexical normalization, absolute conversion, handle-based open, final-path resolution, ownership-marker verification, and ancestor comparison. Cleanup reopens each candidate without following links and verifies root ID, device/volume ID, file ID, type, size, modification time, policy generation, and protection leases against the preview snapshot. Any mismatch skips the item and records a degraded reason.

- Windows: normalize drive letters and comparisons with invariant case-insensitive semantics without changing display case; reject drive-relative paths such as `C:foo`, device paths, alternate data streams, and unexpected 8.3 aliases. Canonicalize UNC to `\\server\share\...`; never equate mapped drives with UNC unless handle-derived volume identity proves it. Inspect every reparse point and never traverse junctions, mount points, or symlinks. Use handle final paths and volume/file IDs; reject roots crossing volumes or ambiguous case collisions.
- macOS: preserve display spelling but compare resolved component identities; account for usually case-insensitive and Unicode-normalizing volumes without assuming either. Do not follow symlinks or Finder aliases. A case/normalization collision is Protected.
- Linux: path comparison is byte/case-sensitive; do not follow symlinks, bind mounts, procfs-like magic links, or mount transitions. Compare device/inode identities and use directory-relative, no-follow operations.

Containment is component-based against the verified resolved root, never string-prefix based. The root directory itself is never a cleanup candidate. Permission errors, network/share instability, unavailable stable IDs, mount changes, marker changes, or TOCTOU uncertainty fail closed.

## Scanner and dashboard

The scanner is deterministic local code with zero model/API-token use. A low-priority incremental walker stores only metadata under an AIWorkHub-owned cache, keyed by root/generation/file identity. Filesystem notifications mark subtrees dirty; a periodic bounded reconciliation catches missed events. Each refresh has entry, byte-read, wall-time, depth, and open-handle budgets; cancellation checkpoints occur between directories. Unfinished roots retain the last complete snapshot, display `Partial/Stale`, and never increase reclaimable bytes from incomplete evidence.

The view shows:

```text
Storage & Cleanup                         Scanned 2026-07-27 12:34Z  [Refresh]
Total 18.4 GiB   Reclaimable 4.2 GiB   Files 81,204   Repo 7.1 | Global 11.3
[Stable ███████ 9.0] [Active ███ 5.2] [Reclaimable ██ 4.2] [Protected hatched]
Category             Scope     Label        Bytes   Files   Oldest      Newest
Task databases       Repo      Protected    82 MiB      9   2025-...    2026-...
Old Source Graph     Global    Reclaimable  1.8 GiB  8,102  ...
Retained worktrees   Repo      Stable       6.0 GiB  ...
[ ] Terminal logs  [ ] Old caches  [ ] Stale routes     [Preview cleanup]
Degraded: 1 root inaccessible; reclaimable estimate excludes it.    [Details]
```

Category bars and totals use allocated bytes. Cards include total bytes, reclaimable bytes, file count, oldest/newest modification timestamps, repo/global split, scan timestamp, completeness, and degraded reasons. Protected bytes can overlap a content category but not Stable/Active/Reclaimable totals. Refresh returns immediately with the last snapshot and job state; the UI polls with backoff or subscribes to local progress and never starts cleanup.

## API and data contract

All APIs are loopback/local extension APIs and return schema/version plus opaque IDs, never arbitrary client paths.

- `POST /storage/scans {scope, budget}` -> `202 {scan_id, snapshot_id, state}`.
- `GET /storage/scans/{id}` -> progress, cancellation state, per-root completeness, scan timestamp and degraded reason codes.
- `DELETE /storage/scans/{id}` requests cooperative cancellation; completed snapshot remains immutable.
- `GET /storage/snapshots/{id}/summary` -> totals and category/status/scope aggregates.
- `GET /storage/snapshots/{id}/items?category=&scope=&cursor=&limit=` -> at most 200 items and 256 KiB; opaque expiring cursor, stable `(sort_key,item_id)`.
- `POST /storage/cleanup-previews {snapshot_id,categories,policy}` -> immutable preview ID, selected count, estimated allocated/logical bytes, excluded/protected counts, reasons, expiry and confirmation digest.
- `POST /storage/cleanup-runs {preview_id,confirmation_digest,confirm:true}` -> explicit execution only; reject expired/stale previews and empty category selection.
- `GET /storage/cleanup-runs/{id}` -> planned/processed/quarantined/skipped/failed counts and bytes, current phase, audit cursor and before/after evidence.
- `DELETE /storage/cleanup-runs/{id}` requests cancellation; it stops before the next item and does not roll back completed quarantines.
- `POST /storage/cleanup-runs/{id}/restore {confirm:true}` restores non-conflicting quarantine entries using the same containment checks.

Summary fields include `snapshot_id`, `scan_started_at`, `scan_completed_at`, `status`, `allocated_bytes`, `logical_bytes`, `reclaimable_bytes`, `file_count`, `oldest_mtime`, `newest_mtime`, `repo_bytes`, `global_bytes`, `labels[]`, `categories[]`, `roots[]`, and `degraded_reasons[]`. Integers are unsigned 64-bit serialized as decimal strings if the UI cannot preserve precision. Timestamps are UTC RFC 3339. Paths are display-only and cannot be submitted back.

## Cleanup transaction and retention

Preview is mandatory and side-effect free. It identifies explicit categories, applies the latest policy, subtracts all protections, estimates allocated bytes, and signs a digest over snapshot/root/item identities and policy generation. Confirmation is a separate user gesture showing estimate, categories, exclusions, and recovery behavior. Scheduled cleanup is optional, separately enabled per category, uses stricter policies, emits its own preview/audit record, and is off by default; it never enables itself after upgrade.

Default policy values are configurable but safe defaults are: keep the last 10 terminal runs per task and last 3 extension/runtime versions; minimum age 14 days (30 days for retained evidence); cache cap 5 GiB using oldest-access/creation evidence without touching files; quarantine undo window 7 days. Protected task states are queued, claimed, running, blocked, awaiting callback, awaiting review, review, accepted-within-retention, and any unknown state. A lease is stale only when its expiry passed by a clock-skew margin, PID plus process-start token is absent, coordinator heartbeat is absent, no open/live-owner record exists, and a second check after a grace interval agrees. Age or PID absence alone is insufficient.

Execution acquires a per-root cleanup lease, revalidates candidates immediately before action, and moves them atomically into same-volume quarantine where practical. Cross-volume copying is not a cleanup fallback. OS trash may be offered only when it preserves ownership/audit identity; otherwise the item is skipped. Each quarantined entry has a durable manifest, original relative path, identities, hashes where bounded, size, timestamps and restore deadline. Purging quarantine is a distinct preview/confirmation after the undo window.

The append-only local audit records `scan_started/completed/degraded`, `preview_created/expired`, `cleanup_confirmed/started/item_quarantined/item_skipped/cancel_requested/completed/failed`, `restore_*`, and `quarantine_purge_*`, with operation/root/category IDs, policy version, actor=`local_user`, timestamps, reason codes, and byte counters—never secrets or file content. Completion records immutable before/after snapshot IDs, estimated bytes, bytes moved to quarantine, filesystem free-space delta (informational), skipped bytes, and eventually purged bytes. “Reclaimed” is labeled `quarantined` until purge actually releases storage.

Crash recovery replays the journal: an item is either at its original location, atomically quarantined with manifest, or marked ambiguous and Protected for manual inspection. Startup never resumes deletion automatically. It reconciles orphan staging, expires leases only with stale proof, and offers resume/restore after fresh preview. Concurrent file creation, lease renewal, task state change, extension update, Source Graph generation switch, or root remount invalidates affected items.

## Threat model

Defended threats include malicious symlink/junction swaps, path traversal and prefix confusion, hard-link double counting, forged/stale ownership markers, compromised preview payloads, TOCTOU races, PID reuse, clock rollback, mount/drive replacement, database sidecars mistaken for caches, another plugin mimicking names, huge-directory denial of service, integer overflow, cancellation/crash mid-batch, and logs containing secrets. Controls are handle-relative no-follow access, stable identity checks, signed/opaque server-side plans, bounded traversal/payloads, conservative arithmetic, lease/state revalidation, redacted paths, quarantine, and durable audit.

The local user or process with equivalent OS privileges remains in the trust boundary; this design does not claim protection from an administrator modifying both files and manifests.

## Test matrix

| Area | Required tests |
|---|---|
| Ownership | valid/missing/forged marker; nested and overlapping roots; foreign repo/plugin; root replaced after preview |
| Windows | drive case, `C:relative`, UNC/mapped drive, ADS, junction/reparse swap, case collision, volume change |
| macOS/Linux | symlink swap, Unicode/case collision, bind/mount boundary, device/inode reuse, broken link |
| Accounting | nested roots, hard links, sparse/compressed files, overflow, repo/global split, incomplete root |
| Protections | every protected task state; callback/review reference; live/reused PID; current graph/VSIX; credentials |
| Retention | last N, minimum age, cache cap, accepted evidence, quarantine window, manual versus scheduled |
| API/UI | cursor stability/expiry, 200-item and 256-KiB bounds, stale refresh, labels/bars/timestamps, decimal bytes |
| Cleanup | dry-run has no writes; category selection; digest mismatch; revalidation skip; atomic quarantine; restore conflict |
| Races/crash | lease renewal, process starts, graph/runtime switch, cancel every phase, crash before/after journal/move |
| Audit | ordered events, redaction, before/after byte evidence, partial/failure reason, quarantine versus purged bytes |
| Performance | million-entry tree under budgets, notification loss reconciliation, no model/network call, bounded memory |

## Rollout and implementation decomposition

1. Inventory-only feature flag: implement owned-root registry adapters and platform path verifier; ship read-only diagnostics and golden ownership tests.
2. Incremental scanner: metadata cache, deduplication/accounting, budgets, cancellation, degraded states and snapshot API.
3. Dashboard: summary/category bars, repo/global table, timestamps, labels, pagination and accessible degraded details.
4. Policy engine and preview: task/callback/process/graph/runtime protection adapters, retention evaluator, immutable digest.
5. Quarantine executor behind a second opt-in flag: per-item revalidation, journal, cancellation, restore and crash reconciler.
6. Manual cleanup pilot for terminal logs and disposable caches only; compare estimates with quarantined/purged evidence.
7. Expand categories after adversarial cross-platform tests; keep canonical/active categories non-actionable.
8. Offer scheduled cleanup as a separate default-off preference only after manual cleanup reliability gates pass.

Release gates require zero protected-object mutations in fault injection, complete audit evidence, bounded API/performance tests, successful restore tests, and independent security review. Rollback disables scan scheduling and execution endpoints without deleting snapshots, journals, or quarantine.
