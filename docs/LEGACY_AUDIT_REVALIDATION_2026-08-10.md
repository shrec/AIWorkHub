# Legacy audit revalidation — current HEAD

Date: 2026-08-10

This record prevents three historical reports from acting as a second, stale
bug tracker:

- `AUDIT_2026-08-03.md`
- `AUDIT_BUGS_AND_OPTIMIZATION_2026-08-06.md`
- `LIVE_BUG_BACKLOG_2026-08-03.md`

The canonical disposition surface is NeedFix. Historical line numbers,
version-specific readiness observations, and estimated performance multipliers
are not current product claims.

## Current disposition

| Historical family | Current disposition |
|---|---|
| Callback/terminal atomicity | Closed by the atomic task-state + callback-outbox transaction and current regression coverage. Callback commit failures now explicitly roll back the transaction they own. |
| Terminal failure shown as review work | Covered by the current canonical lifecycle, operational-failure inbox, reviewer-child disposition, and NF25/NF77/NF94 lineage. NF94 retains only its live Windows projection. |
| Retained rework uses the wrong tree | Closed by retained-worktree reconciliation, exact predecessor overlays, and Source Graph overlay invalidation (NF50/NF56/NF75/NF86/NF107). |
| Read-only reviewer acceptance and receipt truth | Closed by packet-bound authenticated submission, no-write acceptance, durable receipt sealing, meaningful findings, and bounded submit handling (NF34/NF49/NF52/NF53/NF54/NF60/NF108). |
| Validation evidence and provider-free replay | Closed by structured validation receipts and deterministic validation-only replay (NF27/NF64/NF73/NF98). |
| VS Code LM consent/auth/route readiness | Closed or narrowed to exact route state by NF11/NF66/NF69/NF72/NF74/NF80/NF84/NF92/NF103. Windows-only final checks remain explicitly accepted, not silently closed. |
| Source Graph standby, authority, worker re-query and stale overlays | Closed by repository authority, exact-target retrieval, recovery, single-file indexing, cache invalidation and retained overlay fixes (NF15/NF29/NF56/NF71/NF75/NF91/NF106). |
| AI Memory FTS and manager context projection | Current migration/read paths and truthful hit/run projection are covered by the full regression suite; old zero-hit/unavailable conflation is closed by NF109. |
| Token/cost and retry attribution | Durable provider usage is closed by NF31. Route-local economics remains an explicit research item (NF13); it is not represented as a correctness defect. |
| Structured stream flood and repeated context | Still deliberate optimization work in NF20/NF23/NF26. No historical estimate is promoted as a measured saving. |
| Behavioral adequacy and benchmark truth | Assurance and meaningful-output gates are implemented (NF1/NF4/NF7/NF9). Corpus calibration and matched A/B work remain explicit NF3/NF8/NF39/NF40/NF41/NF42 items. |
| Hard per-task token ceiling | Rejected as a default product policy. Tasks remain uncapped unless the owner supplies an exact budget or repository policy pre-registers one. Optimization targets necessary work, retries, context and model mix rather than an inferred cap. |

## Current-HEAD repairs made during revalidation

The revalidation found and fixed a bounded residual from the 2026-08-06 audit:

- task-store write connections now use WAL, `busy_timeout=5000`, and
  `synchronous=NORMAL`;
- canonical task status filtering and the result limit execute in SQLite rather
  than fetching the complete table into Python;
- `_atomic_write_json` closes its raw descriptor if `os.fdopen` fails;
- callback enqueue rolls back on integrity or other SQLite failures before
  returning or propagating the error;
- Source Graph lease cleanup tolerates an already-closed descriptor;
- the Webview boundary redacts POSIX, Windows drive-letter and UNC absolute
  paths;
- runtime-retention traversal tolerates files/directories disappearing during
  a concurrent sweep;
- VS Code LM quality-review requests use a review-only protocol, require the
  authenticated submit tool, and stop with a bounded actionable failure instead
  of entering the generic coding-edit loop.

## Verification

- Focused Python regression: 34 passed.
- Full VS Code extension test discovery: passed (32 test files; one native
  Windows-only environment probe remains skipped on Linux by design).
- Full repository suite after this revalidation slice: 3303 passed, 35 skipped.

The remaining canonical backlog must be read from NeedFix. In particular,
Windows-only live checks (NF59/NF94/NF102/NF105), Source Graph process-pool
cross-platform measurement (NF32), and the explicit benchmark/architecture
items are not declared complete here.
