# Donor Capability Port

AIWorkHub selectively ports repository-neutral, proven capabilities from the
local UltrafastSecp256k1 engineering toolkit. The donor is evidence, not a
second runtime authority: every accepted capability is re-homed behind
AIWorkHub's repository identity, policy, MCP, audit and cross-platform
contracts.

## Port rule

1. Keep project-specific cryptography, benchmark data and product semantics in
   the donor repository.
2. Do not copy legacy task, session, memory or KB databases. AIWorkHub's
   `.aiworkhub/` stores remain canonical.
3. Port a generic capability only when it adds a missing contract. Reuse an
   existing AIWorkHub authority when the behavior already exists.
4. All destructive behavior is preview-first, repository-bound, explicitly
   confirmed, audited and reversible where practical.
5. Every port must qualify on Linux, Windows, macOS and Remote-SSH.

## Capability disposition

| Donor capability | AIWorkHub status | Disposition |
| --- | --- | --- |
| Polyglot structural index | Shipped in 0.8.24-0.8.26 | Ported as the canonical 33-family Source Graph registry and conservative semantic/file-evidence adapters. |
| `focus`, `slice`, `context`, `impact`, `trace`, `bundle` | Shipped in 0.8.26 | Ported with byte bounds, ranked symbols, calls, tests, risks and churn/ownership evidence. |
| Hotspots, complexity, coverage, ownership, review queue and bottlenecks | Partially represented inside compact insights | Keep one Source Graph authority; expose dedicated bounded analytics only where the current six modes cannot answer the question. |
| Decisions and invariants ledger | Already covered | Use canonical KB and Policy as Code; do not create a second Source Graph decision database. |
| Pipeline/repo map | Mostly covered | Use task-shaped bundles, Visual DAG and project context. Port only a measured missing view. |
| Bounded external build/scratch pool and rogue build-tree detection | Core port implemented | Workspace Build Hygiene now provides quotas, leases, real byte accounting, preview-first cleanup, cross-platform locking and bounded preflight evidence. Dashboard controls remain the next UI step. |
| Current project-state snapshot | Already covered | Dashboard summary, Review Inbox, task store and evidence bundles are the canonical live projection. |
| Parallel write collision guard | Already covered | Keep AIWorkHub's task-plan and allowed-write collision authority. |
| Completion bridge/watch | Already covered | Keep the durable callback outbox, dispatcher reconciliation and manager inbox. |
| Legacy Session/AI Memory/KB scripts | Superseded | Preserve useful schema ideas only; never import their SQLite files as a live model interface. |
| CUDA intrinsic checker | Initial generic gate implemented | Exact rotation-claim mismatches now enter the diff-scoped Known Bug Scanner and Quality Evidence; CUDA remains dependency-free and change-sensitive. |
| Crypto validators, key tools and benchmark generators | Donor-specific | Do not port into the generic product. They can be repository-local quality commands or task templates. |

## Ordered implementation

1. Reconcile Source Graph counters and finish dedicated analytics gaps using
   the existing index, never a parallel database.
2. Finish Workspace Build Hygiene UI: the allocator, real byte accounting,
   rogue detection, digest-bound cleanup and preflight evidence are present;
   add dashboard preview/apply controls and task-launch allocation.
3. Add optional compiler/CUDA quality adapters through Quality Evidence,
   activated only when the repository declares the relevant toolchain.
4. Measure model context use and task outcomes before accepting further donor
   complexity.
