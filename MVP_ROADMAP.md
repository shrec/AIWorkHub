# AIWorkHub MCP MVP Roadmap

Goal: provide a repository-native, model-agnostic development orchestrator
whose task lifecycle, source context, sessions, memory, knowledge, review and
callback routing work consistently in every supported VS Code environment.

Current canonical baseline (2026-07-30): standalone GitHub repository,
release `0.8.0`, native repo-local `.aiworkhub` storage, multi-repository
manager routing, isolated workers, review-first promotion, callback outbox,
Source Graph daemon, Session/AI Memory/KB manager read-write surfaces, and a
native VS Code dashboard. Historical phase notes below remain as evidence but
do not override this current priority ledger.

## Current Priority Ledger — Post-0.8.0

### P0 — Stable product closure

- [x] Preserve archived/superseded lifecycle authority when an old child exits
      late; never recreate review/pending work from a finalized card.
- [x] Block destructive review false-greens using absolute/relative file-loss
      and Python public-API-loss evidence unless the verified manager supplies
      explicit destructive-change confirmation.
- [x] Expose manager Session Manager, AI Memory and KB canonical read/write
      tools with repo/session identity, idempotency, provenance and soft-delete
      audit.
- [x] Add bounded HMAC-authenticated worker Session/AI Memory/KB write intents;
      workers never open canonical context databases.
- [x] Add verified-manager intent inbox plus explicit accept/reject disposition;
      accepted proposals apply only through the canonical context mutation
      layer and unresolved proposals block task acceptance.
- [x] Add an idempotent, provenance-preserving importer for explicitly selected
      legacy Session/AI Memory/KB stores (dry-run, duplicate report, rollback;
      never implicit global-path discovery). Import sources are repo-relative,
      schema/quick-check validated and fingerprinted; conflicts never overwrite
      canonical rows, and rollback refuses to erase rows changed after import.
- [x] Finish atomic manager repository switching: switch only the manager's
      active repo binding, keep every task/context/log/index/callback in its
      origin repository, stop old repo daemons, start new repo daemons, and
      support deterministic A -> B -> A recovery without moving task cards.
      Switches are serialized; failed target convergence stops target services
      and restores the prior binding/daemons.
- [x] Clear stale terminal substatus, launch error and validation reason on a
      genuine new claim/relaunch episode while retaining the old episode in
      append-only audit history. Both exact-claim and auto-pickup now start a
      clean episode, clear stale completion time and retain a bounded prior
      episode summary on the immutable claim event.
- [x] Run and publish one fresh-install E2E qualification matrix for Linux,
      Remote-SSH, Windows and macOS: install, InitRepo, MCP registration,
      first index, worker launch, terminal review, callback, acceptance,
      reload/restart recovery and repository isolation.
      The portable lifecycle harness and four-lane GitHub matrix are in place.
      Canonical green evidence: GitHub Actions run `30548398008` on commit
      `5f57fa5` (Linux, Remote-SSH contract, Windows and macOS qualification
      artifacts, plus Python 3.10/3.11/3.12, VSIX and distribution jobs).
- [x] Cut the next stable release only after the matrix is green and the
      bundled runtime/extension/source version hashes agree. `v0.8.0` was
      published from commit `d0b1b7a` by release run `30549137880`; its four
      qualification lanes, version lock, complete package tests and final
      VSIX/wheel/sdist asset verification all passed.

### P1 — Context economy and orchestration UX

- [x] Source Graph language-complete first indexing (including PHP), automatic
      InitRepo indexing, bounded incremental file-change/periodic refresh,
      configurable generated/vendor/archive ignore rules and stale-index health.
      Python AST, conservative PHP structural extraction and truthful JS/TS
      file evidence are indexed on the first non-blocking InitRepo build; the
      per-repository daemon performs non-overlapping incremental refreshes and
      honors fail-closed repository-local ignore rules. Health now exposes
      language capability/extension inventory, last-success age, a bounded
      stale threshold and a non-green `stale` state. The formerly blanket-
      skipped hosted-CI daemon lifecycle suite now uses deterministic build
      completion synchronization and passes with `GITHUB_ACTIONS=true`.
      Canonical local evidence: 65 focused Source Graph tests and the complete
      suite (`1465 passed, 18 skipped`) on 2026-07-30.
- [x] Per-task tool-use telemetry: Source Graph calls/hits/zero-hits, bounded
      fallback reason, raw-discovery violations, bytes returned and conservative
      context-savings labels. Dashboard must distinguish measured bytes from
      token/cost truth.
      The worker HMAC ledger now independently measures total/live/cached/
      zero-hit/failed Source Graph calls, entity hits and bounded return bytes;
      latest-run task/adapter aggregation retains bounded gate blocker reasons.
      Provider JSON/JSONL permission-denial evidence is reduced to counts plus
      a fixed raw-discovery label allowlist, never raw commands, paths or prompt
      fragments. Missing provider denial fields remain explicitly unobserved,
      not a fabricated zero. Dashboard copy labels bytes as authenticated tool
      return bytes and makes no inferred token/cost-savings claim. Canonical
      local evidence: focused telemetry/worker tests (`64 passed`), complete
      Python suite (`1467 passed, 18 skipped`) and complete extension suite on
      2026-07-30.
- [x] Visual Plan DAG with dependencies, blockers, ready capacity and critical
      path; retain the existing dependency-safe autolaunch authority.
      The canonical pure task-plan projection now adds deterministic
      topological stages, node/edge/active/blocked counts, ready capacity,
      current critical path and explicit legacy-cycle validity without
      changing claim/autolaunch behavior. The native dashboard renders a
      horizontally scrollable `Plan DAG` with ready, blocked and critical
      nodes plus exact dependency/blocker labels. Canonical local evidence:
      task-plan/core/dashboard tests (`45 passed`), complete Python suite
      (`1469 passed, 18 skipped`) and complete extension suite on 2026-07-30.
- [x] Review Inbox 2.0: pagination, bounded filters, terminal summaries and a
      portable evidence bundle (diff, tests, logs, artifacts, approvals).
      The existing repository-bound inbox keeps bounded pages, filter state,
      recent terminal outcomes and callback-delivery observability. Task detail
      now projects `aiworkhub.review_evidence_bundle.v1` from canonical task,
      process and audit sources: portable diff identities, validation summaries,
      required outputs, artifacts, numeric usage and approval history. Host-local
      paths, raw process logs and obvious credential assignments are excluded or
      redacted. Canonical local evidence: dashboard/task/process tests (`40
      passed`), complete Python suite (`1470 passed, 18 skipped`) and complete
      extension suite on 2026-07-30.
- [x] Unified environment/provider preflight and repo-local Policy-as-Code for
      provider scope, forbidden commands, required validation and retention.
      InitRepo now creates one bounded, non-executable
      `.aiworkhub/config/policy.json`; subsequent initialization validates but
      never overwrites it. The launch gate enforces adapter scope, mandatory
      Source Graph context on canonical code cards and configured quality-check
      identities before claim/start. Mandatory `grep`/`rg`/`find`/`tree`
      discovery denies cannot be removed. Retention limits are schema-bounded
      and exposed to the storage lifecycle without accepting shell fragments.
      The unified MCP/dashboard preflight reconciles repository authority,
      policy, Source Graph freshness, quality config and every local adapter;
      an installed CLI remains `installed_unverified_access` until a bridge or
      credential authority actually observes access. Canonical local evidence:
      policy/init/launch/dashboard tests (`44 passed`), complete Python suite
      (`1475 passed, 18 skipped`) and complete extension suite on 2026-07-30.
- [x] Manager-editable model workforce inventory/scoring based on observed task
      outcomes, without fabricating provider quota/limit data unavailable from
      the provider extension API.
      InitRepo provisions a bounded repository-local workforce catalog covering
      the configured first-party Claude, Codex, DeepSeek VS Code LM and GLM VS
      Code LM routes. Manager MCP exposes audited catalog updates, evidence-
      backed inventory reads and explainable cheapest-capable ranking. Accepted,
      review-ready, validation-failure, retry, latency, token and cost evidence
      is joined only from the same repository's canonical cards and bounded
      process ledger; missing samples use an explicit conservative prior and
      unavailable provider quotas remain `unavailable_from_provider_api`.
      Dashboard `Workforce` shows readiness/access-observation truth, sample
      counts, acceptance/retry evidence and the bounded manager adjustment.
      Canonical local evidence: focused workforce/manager/dashboard tests (`60
      passed`), complete Python suite (`1482 passed, 18 skipped`) and complete
      extension suite on 2026-07-30.
- [x] Complete storage retention UX: size by component, archive/restore,
      age/size policy, dry-run cleanup and rollback-safe deletion.
      - [x] Repository-scoped retained-worktree lifecycle: exact Git-common-dir
        ownership, component inventory, age/size policy preview, same-volume
        quarantine, seven-day restore window and separately confirmed expired
        purge. Unsaved, unpushed, active, orphaned and foreign-repository trees
        fail closed. Canonical local evidence: storage/dashboard tests (`48
        passed`), complete Python suite (`1489 passed, 18 skipped`) and complete
        extension suite on 2026-07-30.
      - [x] Terminal-run log lifecycle: the append-only process ledger stays
        canonical; only exact per-request output/metadata files for finished or
        archived tasks become eligible after the repo policy age, while active,
        review, blocked, pending and unknown authority plus the latest ten runs
        per task stay protected. Preview digest, same-volume repo quarantine,
        restore and separately confirmed expired purge are exposed in MCP and
        the Storage dashboard. Canonical local evidence: complete Python suite
        (`1494 passed, 18 skipped`) and complete extension suite on 2026-07-30.
      - [x] Extension runtime generation lifecycle: every live VS Code window
        owns a heartbeat lease; current, latest-three rollback, live-lease and
        malformed-lease generations fail closed. A seven-day rollout grace
        protects pre-feature windows, then explicit preview/quarantine/restore
        and expired purge manage only AIWorkHub global-storage generations.
        VS Code-owned installed-extension directories remain out of scope.
        Canonical local evidence: runtime lifecycle/security regression plus
        the complete extension suite and portable VSIX build on 2026-07-30.
      - [x] Source Graph generation disposition: the production graph has one
        repository-bound canonical SQLite authority rather than retained index
        generations. It is measured as protected repository data; no synthetic
        generation lifecycle or false reclaimable population is created.

### P2 — Maintainability, distribution and ecosystem

- [ ] Close Quality Gate 2.0: pure six-lens verdict, risk-proportional profiles,
      read-only independent reviewer execution, combined-tree differential,
      negative-fixture calibration and Review Inbox lens/residual-risk display.
      Preserve the existing Quality Evidence Engine, exact scope/hashes,
      declared checks and destructive-diff guard as the deterministic floor;
      do not create a second review authority. See `docs/QUALITY_CONTROL.md`
      and ADR 0003.
      - [x] Pure `aiworkhub.quality_verdict.v2` fold, monotonic risk profiles,
            strict read-only reviewer schema, initial negative fixtures and
            bounded dashboard lens/risk/verdict projection.
      - [ ] Execute independent reviewer reports with verified provider/task
            identity and anti-anchored evidence packets.
      - [x] Produce and enforce combined-tree differential evidence before
            manager promotion for medium-and-higher risk tasks.
      - [ ] Expand calibration to one positive and targeted negative fixture
            for every blocking predicate and report false-green/false-red
            rates across the platform matrix.
- [ ] Split `core.py` and `process_launcher.py` along lifecycle, authority,
      review and context boundaries without changing public MCP contracts.
- [ ] Modularize the VS Code extension in TypeScript with deterministic bundle
      output and restored-tab/reload E2E coverage.
- [ ] Centralize pytest fixtures/state isolation and organize unit/integration/
      E2E suites; add Ruff, type checking and pre-commit quality gates.
- [ ] Close the 2026-07-30 operational-truth audit in measured order:
      - [x] Missing/empty repository quality policy is explicitly
            `unverified`; public check execution cannot return an empty green.
            AIWorkHub itself carries three portable declared checks.
      - [x] Account for canonical runtime, legacy logs, orphan request files,
            repository-owned and shared/unattributed worktree bytes in bounded
            retention previews. On the audited host this corrects 370 MB to
            7.45 GB observed. Aged legacy `logs/` now has preview/quarantine/
            restore/delayed-purge support with exact identity revalidation.
      - [ ] Attribute/prune stale shared-worktree registrations and execute the
            explicit legacy quarantine after owner confirmation.
      - [x] Rotate the active terminal ledger before 48 MiB, stream immutable
            rotations instead of whole-file reads, include every segment in
            storage accounting, and bound each production worker stdout/stderr
            tail to 16 MiB with explicit dropped-byte evidence. Canonical local
            evidence: focused ledger/supervisor/launcher/retention regression
            (`37 passed`), complete Python suite (`1530 passed, 18 skipped`)
            and complete 24-file extension suite on 2026-07-30.
      - [x] Bind deterministic verification and terminal evidence to the exact
            claim epoch, clear episode-local verdicts on reject/block/re-claim,
            and fail manager acceptance on epoch mismatch. Plan DAG now reports
            total, dependency and lifecycle blocked populations separately.
            Canonical local evidence: focused lifecycle/plan/acceptance
            regression (`100 passed`), complete Python suite (`1532 passed,
            18 skipped`) and complete 24-file extension suite on 2026-07-30.
      - [x] Discover and execute every extension `*.test.js` sequentially;
            CI no longer depends on a hand-maintained filename chain.
      - [ ] Add calibrated Ruff/type gates and close stale repository artifacts.
- [x] Make version metadata a single source of truth and enforce Linux/Windows/
      macOS release CI plus reproducible VSIX checksums. Canonical authority is
      `src/aiworkhub/_version.py`; projections, tag identity, repeated VSIX
      byte equality and sorted final-asset checksums are release-gated.
- [ ] Finish public documentation, ADRs, VS Code Marketplace/Open VSX
      publication, then consider GitHub issue/PR sync, webhooks/public API and
      historical reliability analytics.
      - [x] Establish the canonical AIWorkHub name, positioning, visual system,
        repository hero, Marketplace copy, support path, package discovery
        metadata and first authority ADR.
      - [ ] Add clean public product screenshots, complete the core ADR set,
        publish to VS Code Marketplace/Open VSX and qualify the published
        install artifacts independently of the source checkout.

## Phase 0 — Wrapper MVP

Status: complete for local stdio MVP.

- [x] Standalone Python package skeleton.
- [x] FastMCP stdio server.
- [x] Read-only task tools over `AITools/taskctl.py`.
- [x] Write-gated lifecycle tools.
- [x] Smoke test for health/read/write-gate.
- [x] MCP client smoke test over real stdio transport.

## Phase 1 — Safe Local Automation

- [x] Add tool-level audit log for every write-gated action.
- [x] Add dry-run mode for `auto_pickup`.
- [x] Add batch status/review tools for dashboard use.
- [x] Add task result handoff summarizer that reads review queue and returns Codex-ready report.
- [x] Add strict allow-list for runners/topics.
- [x] Add review artifact strictness guard for hollow worker submissions.

## Phase 2 — Agent Launcher MVP

- [x] Add local CLI adapter abstraction.
- [x] Add read-only/dry-run CLI adapter abstraction.
- [x] Add Claude/Codex adapter planning as read-only validated plans.
- [x] Add manual/external adapter planning for Copilot-hosted DeepSeek.
- [x] Evaluate DeepSeek worker surface candidates; retain manual/external mode until a stable local CLI exists.
- [x] Add launch queue request/audit protocol with process-state fields and logs.
- [x] Add stale recovery recommendations.
- [x] Add cost/usage ledger aggregation.
- [x] Keep all launch actions disabled unless both explicit env gates are enabled.
- [x] Add real local Claude/Codex process launch without shell invocation.
- [x] Add real local `deepseek_copilot_cli` adapter (official GitHub Copilot CLI
      in BYOK mode → DeepSeek OpenAI-compatible API). `deepseek_*` runners route
      to it; `deepseek_manual` stays an explicit non-launchable fallback. Secure
      one-time credential bootstrap (getpass + 0600 file outside the repo);
      the API key reaches only the child env as `COPILOT_PROVIDER_API_KEY`
      (never argv/logs/audit/dashboard/cards/Git); missing credential fails
      before `claim-start`. Verified by
      `tests/test_deepseek_copilot_adapter_b343_v1.py` (38 cases incl. a
      Landlock-sandboxed launch proving the key reaches the confined child).
- [x] Add exact-card preflight and atomic `taskctl claim-start` worker entry.
- [x] Add Codex-only `reject-review` requeue with feedback carried into the next worker contract.
- [x] Add PID/process-group tracking, stdout/stderr logs, timeout, cancel, duplicate guard, and process audit events.
- [x] Add completion collection and automatic token/cost extraction where CLI output provides it.
- [x] Add loopback-only, read-only VS Code dashboard.

### Phase 2 Source Notes

- Source: <https://github.com/deepseek-ai/awesome-deepseek-agent>
- Use: candidate catalog for DeepSeek-compatible worker surfaces and setup patterns.
- Initial candidates to benchmark: DeepSeek-TUI, Reasonix, Deep Code, OpenCode/Cline/Qwen Code/Codex/Copilot integration guides.
- Boundary: research/source marker only. No launch authority, write authority, or task authority is granted from this source.
- Required gates before any adapter launch: explicit allow-list, per-run audit log, process log capture, no default-on launch, `AIWORKHUB_ALLOW_LAUNCH=1`, and existing task write gates.

## Phase 3 — Project Switch Readiness

- [x] Run one full task wave dry-run through MCP without queue mutation or process launch.
- [x] Run one owner-approved real task through `aiworkhub_agent_launch_task` (`CLAUDE_TASK_MCP_LIVE_LAUNCH_CANARY_B313_V1`).
- [x] Validate collision guard before and after the live canary batch.
- [x] Validate no parent-repo task state corruption (`taskctl verify` PASS after completion).
- [x] Freeze MCP tool contract v1.
- [x] Convert this directory to a standalone GitHub repository (completed;
      the old submodule wording is superseded).

### MVP Finish Gate

- Default gates closed: required.
- Exact task/runner/topic claim with no fallback: implemented.
- Shell-free configured adapter command: implemented.
- Cross-process duplicate lock and concurrency cap: implemented.
- Review-ready, early-exit, timeout, cancel, and bounded output evidence: implemented.
- Dashboard and completion inbox expose process state: implemented.
- DeepSeek local autolaunch: **implemented and tested** via `deepseek_copilot_cli`
  (Copilot CLI BYOK → DeepSeek). Single-project operational closure: the launch
  plumbing, credential security, sandboxed env delivery, readiness surfacing, and
  no-claim-on-missing-credential gate are all covered by deterministic tests.
- Live DeepSeek API end-to-end: gated on a real DeepSeek credential being
  provisioned on the coordinator host. Until then the one bounded live canary is
  reported as `BLOCKED_CREDENTIAL` with an exact plan (see below); it is never
  faked to PASS. Every independent implementation/test gate passes now.
- Multi-project/network service, authenticated LAN exposure, and converting this
  directory to a real GitHub repo/submodule: intentionally deferred; this MVP is
  loopback/stdio and single-project.

### DeepSeek live canary (BLOCKED_CREDENTIAL plan)

To run the single bounded no-product-mutation live canary once a real key exists:

1. `aiworkhub-deepseek-credential set` (host, outside repo, 0600).
2. Confirm `aiworkhub_agent_adapter_readiness` shows `deepseek_copilot_cli.launchable=true`.
3. Start the MCP server with `AIWORKHUB_ALLOW_LAUNCH=1` and
   `AIWORKHUB_ALLOW_WRITES=1`.
4. `aiworkhub_agent_launch_task(task_id=<pending deepseek canary card>,
   runner=deepseek_*, topic=task_mcp, adapter_id=deepseek_copilot_cli,
   model=deepseek-v4-pro)`; collect and return the task to Codex review.
   The worker mutates no product source and touches only its allowed_writes.

## Phase 4 — Task MCP -> Originating Codex Thread Callback Bridge

Status: canonical bridge code accepted by Codex after trusted-host review;
the live daemon is enabled and its durable queue is observable.

- [x] Immutable `tasks.origin_thread_id` (idempotent SQLite migration,
      `AITools/taskdb.py`): validated UUID, preserved across reimport,
      conflicting rebinds rejected.
- [x] `taskctl.py add-card` auto-captures a valid `CODEX_THREAD_ID` at
      registration time when the card does not already declare one; a
      malformed value is skipped, never fails registration.
- [x] Coordinator-only `taskctl.py bind-thread <task_id> --runner codex` for
      legacy tasks (requires the same private coordinator capability as
      `done`/`reject-review`/`release-launch`).
- [x] Durable, deduplicated SQLite `callback_outbox`
      (`AITools/taskdb.py`): unique on `(task_id, transition,
      origin_thread_id, episode_id)` so a rejected/reclaimed task may emit one
      wake per claim episode without reviving an older episode; eligible
      transitions are exactly `review_ready`,
      `blocked`, `launch_failed`, `validation_failed`, `scope_rejected`,
      `timed_out`, `cancelled` (including `process_lost` normalized to the
      blocked outcome bucket). Pending/processing/Codex-done/Codex-reject
      never enqueue. Enqueue hooks: `upsert_card` (review/blocked-import
      transitions) and `cmd_release_launch` (the process-launcher terminal
      reason -- timed_out/cancelled/scope_rejected/validation_failed/
      worker_failed-as-launch_failed). Every claimed terminal result remains
      visible as top-level `review`; it never automatically requeues the card.
- [x] Local callback bridge (`aiworkhub/callback_bridge.py`) speaking
      the real `codex app-server --listen stdio://` newline-delimited
      JSON-RPC protocol: `initialize` -> `initialized` -> `thread/resume` ->
      `turn/start` -> wait `turn/completed`, with request-id correlation,
      busy-thread deferral, leases, bounded retry/backoff, and
      delivered/dead-letter terminal states. The rejected transport (`codex
      thread status`, `codex exec --thread-id/--client-id/--no-remote`) is
      never called or emulated.
- [x] Fixed coordinator-only turn prompt: validated `task_id`, normalized
      terminal transition, event id -- no worker output, logs, objectives,
      errors, artifacts, tool input, or full `origin_thread_id`.
- [x] `aiworkhub-callback-bridge` CLI: `run-once`, `daemon`, `status`,
      `dry-run`; user-systemd example in README.md.
- [x] Read-only, redacted dashboard section (`callback_bridge_health` via
      `taskctl.py callback-outbox-status`): bound/unbound task counts,
      pending/inflight/delivered/dead-letter counts, last delivery, last
      dead-letter error -- never a full `origin_thread_id`. No new MCP tool
      added; the frozen v1 tool contract stays at 33 tools.
  DeepSeek/Claude launcher, dashboard, and security behavior preserved: the
  DeepSeek `deepseek_copilot_cli` adapter (`runtime_adapters.py`,
  `deepseek_credentials.py`) and Claude/Codex isolated-launch paths
  (`process_launcher.py`, `worker_workspace.py`, `server.py`) were
  reconciled byte-for-byte against the trusted host's already-accepted state
  -- no blind overwrite of newer canonical content.
- [x] Fake executable App Server test harness
      (`tools/aiworkhub/tests/_fake_app_server.py`): a real subprocess
      speaking the actual wire, strictly enforcing sequence (rejects
      missing/out-of-order `initialize`/`initialized`/`thread/resume`/
      `turn/start`), plus busy/timeout/process-death/mismatched-id
      scenarios. Mocking `subprocess.run` returncode alone was not used as
      evidence anywhere in this suite.
- [x] Trusted-host independent review: 161/161 registered tests, real stdio
      smoke, taskctl verification, collision guard, live episode-key schema,
      exact App Server request/turn correlation and atomic two-connection
      outbox claim all pass. Evidence:
      `eval/task_mcp_callback_bridge_b384_coordinator_review_v3.json`.
- [x] Live daemon enablement: Codex superseded 24 stale pre-episode/tombstone
      outbox rows, enabled `aiworkhub-callback-bridge.service`, and verified
      it active with zero pending/inflight/dead-letter rows. No synthetic real
      callback was sent; the first wake will come from a genuine worker
      terminal event.

### Callback bridge canary plan (worker-safe dry-run -> Codex-owned live enable)

Disposable, safe to run anywhere -- builds the fixed prompt/argv and proves
the wire sequence against a fake executable App Server; never starts a real
`codex` process and never sends a real turn:

```bash
FAKE_APP_SERVER_LOG=/tmp/geoai_cb_canary.jsonl \
  python3 -c "
import sys; sys.path.insert(0, 'tools/aiworkhub/src')
from aiworkhub.callback_bridge import CallbackBridge
b = CallbackBridge(repo='.', executable=[sys.executable, 'tools/aiworkhub/tests/_fake_app_server.py'])
print(b.dry_run('SOME_TASK_ID', 'review_ready'))
"
```

Exact coordinator-approved command to enable **live** delivery once Codex has
reviewed this candidate on the trusted host (binds against the real installed
`codex` binary and the real outbox -- run only after review):

```bash
aiworkhub-callback-bridge --executable codex daemon
```

## Phase 5 — Operational Closure: Batched Delivery, Configurable Timeout/Lease

Status: candidate ready for Codex trusted-host review
(`CLAUDE_TASK_MCP_CALLBACK_BRIDGE_OPERATIONAL_CLOSURE_B402_V1`), fixing the
measured live-operation failure: eight near-simultaneous `review_ready`
events on one origin thread produced one inflight plus seven pending
single-item callbacks (up to eight separate Codex turns), and the
then-60-second-class App Server timeout caused dead letters on long CEO
review turns.

- [x] `AITools/taskdb.py::callback_batches` table: durable batch identity/
      lease/attempts/member_count, keyed by a fresh `batch_id` assigned once
      at formation time and persisted on every member `callback_outbox` row.
      `claim_pending_callback_batch` atomically coalesces every currently-
      unassigned pending row for ONE origin_thread_id (bounded by
      `DEFAULT_CALLBACK_BATCH_MAX_MEMBERS=25`) into one leased batch --
      never spans threads, never two inflight batches for the same thread.
      Membership is fixed at formation and never re-scanned on restart/
      lease-reclaim, only re-pruned for staleness
      (`_task_still_in_matching_terminal_state`, reused from B384) before
      every (re-)lease -- so a stale member can never reach an App Server
      turn, and an event arriving for the same thread mid-turn simply waits
      for the next batch instead of forcing a redundant/parallel wake.
- [x] `mark_batch_delivered`/`requeue_batch`/`mark_batch_dead_letter`: every
      member of a batch transitions together (bulk `UPDATE ... WHERE
      batch_id=? AND state='inflight'` -- scoped to still-inflight rows so
      an already-superseded sibling sharing the batch_id is never
      resurrected). Exactly-once terminal disposition per member, never a
      partial delivery.
- [x] `callback_bridge.py::CallbackBridge` now claims/delivers/finalizes one
      whole **batch** per `run_once()`/daemon iteration (`_process_batch`,
      `deliver_callback_batch`, `build_batch_callback_prompt`) instead of
      one outbox row. The batch prompt lists each member's validated
      `task_id`/transition/event id plus the bounded total count, and
      explicitly instructs Codex to inspect the complete trusted review
      queue so a task that changed state mid-turn needs no separate wake.
- [x] Configurable App Server timeout/lease: `--app-server-timeout-seconds`/
      `--lease-seconds`/`--max-batch-members` CLI flags (or
      `AIWORKHUB_CALLBACK_APP_SERVER_TIMEOUT_SECONDS`/`AIWORKHUB_CALLBACK_LEASE_SECONDS`/
      `AIWORKHUB_CALLBACK_MAX_BATCH_MEMBERS` env vars), validated at startup:
      `validate_lease_and_timeout` enforces `lease >= timeout + margin`
      (default margin 300s) and rejects an invalid combination immediately
      rather than silently running with one. No 60-second implicit timeout
      path remains anywhere in the module.
- [x] Coordinator-only, audited dead-letter recovery:
      `taskctl.py callback-recover-dead-letter <outbox_id> --runner codex`
      requeues one dead-lettered row (detached from its dead batch, so it
      forms a fresh batch on the next claim) only if its task is STILL in
      the matching eligible terminal state/episode right now, else
      supersedes it -- gated by the same private coordinator capability as
      `done`/`reject-review`/`release-launch`/`bind-thread`.
- [x] Dashboard/status batch observability: `callback_outbox_stats()` now
      nests `batches` (per-state batch counts, current inflight batch
      member count, oldest pending batch age in seconds, last dead-letter
      batch member count/error) alongside the existing per-row outbox
      counts -- surfaced automatically through the existing
      `callback_bridge_health`/`callback-outbox-status` path with zero
      dashboard.py code changes needed (verified end-to-end against a real
      SQLite DB, never a full `origin_thread_id`).
- [x] Real fake-App-Server integration coverage added to
      `tools/aiworkhub/tests/test_callback_bridge.py`: eight-event
      single-thread batching, an event arriving during an in-flight turn,
      two-thread separation (never coalesced), restart recovery (same
      durable `batch_id` reclaimed after an expired lease), busy-thread
      retry of the WHOLE batch, partial-stale-member pruning within a
      batch, all-members-stale zero-turn supersede, dead-letter recovery
      (requeue vs. supersede), timeout/lease validation (including CLI/env
      resolution), and batch-prompt bounded-count/injection-resistance.
- [x] Live activation of the batching build: Codex enabled the user service;
      genuine task terminal events now form durable per-thread batches. A
      delivery remains leased while its Codex review turn is active and is
      finalized when that turn completes.

## Phase 6 — Terminal Outcome Always Reaches Codex Review

Status: accepted and live (`CLAUDE_TASK_MCP_TERMINAL_REVIEW_ONLY_B404_V1`).

- [x] `pending/unclaimed` is reachable only for a new never-claimed card or
      an explicit coordinator `reject-review` retry.
- [x] Every claimed terminal result, including validation/scope/launch
      failure, timeout, cancellation, blocked/promotion conflict and
      missing/stale worker process, persists exactly `status=review`,
      `worker_status=review_ready` with normalized `review_outcome` and
      bounded `review_reason`.
- [x] `recover-stale`, owner-confirmed stale recovery,
      `owner-review-recover --apply`, and `release-launch` contain no
      automatic path back to pending and no claimed top-level blocked state.
- [x] Each terminal claim episode enqueues one durable callback; callback
      retry may requeue only the delivery record, never the task.
- [x] Independent trusted-host verification: required suite `147 passed`,
      DeepSeek adapter preservation suite `39 passed`, `taskctl verify` PASS,
      collision guard PASS. Evidence:
      `eval/task_mcp_terminal_review_only_b404_v1.json`.
- [ ] Follow-up operational hygiene: build the read-only pending-population
      migration ledger, expose repo/DB identity health, exclude archived/
      backlog rows from Active, and add authenticated owner Archive/Restore
      dashboard operations. These are not lifecycle-closure regressions and
      remain the next bounded Task MCP card.

## Phase 7 — VS Code-Owned App Server Mux + Sideband Callback Transport

Status: candidate ready for Codex trusted-host review
(`CLAUDE_SONNET5_TASK_MCP_VSCODE_OWNED_APP_SERVER_MUX_B409_V1`), fixing a
measured topology defect the B407 `turn/steer` repair could not: a
separately spawned bridge App Server (`AppServerClient`) can only ever see
the extension-owned thread from the OUTSIDE. The VS Code OpenAI extension
spawns and owns its own `codex app-server` child over a private stdio pipe;
`thread/resume`/`turn/*` are scoped to the App Server INSTANCE that owns the
turn, not to the thread id in the abstract, so a second instance can never
wake the visible chat -- it can only start a hidden turn on a hidden
connection. The live canary that measured this: a fresh bridge-owned App
Server saw the owner thread idle and sat in `turn/start` for 6+ minutes while
the real extension already had its own App Server child and the active
rollout open.

- [x] `aiworkhub/app_server_mux.py`: a transparent Codex CLI wrapper
      installable as the extension's `chatgpt.cliExecutable`. Non-app-server
      invocations `execvp` the real binary with the exact argv/exit
      behavior (full process-image replacement -- no proxying artifact
      survives this path). `app-server` invocations become `AppServerMux`:
      one real child `codex app-server` is spawned with the exact argv
      forwarded unchanged, stderr inherited untouched, and stdin/stdout
      proxied byte-for-byte in both directions between the extension and
      that child -- requests, responses, and server-originated
      notifications all pass through transparently so the visible chat UI
      is unaffected.
- [x] Authenticated local sideband: one Unix socket in a private (0700)
      owner directory, socket file 0600, `SO_PEERCRED` same-uid check where
      available, a separate 0600 capability file compared with
      `hmac.compare_digest`, an exact three-method allowlist
      (`thread/resume`/`turn/steer`/`turn/start` -- `initialize` and every
      other method rejected), a request-shape allowlist (`cap`/`method`/
      `params` only -- a client-supplied `id`, `jsonrpc`, or any other key
      claiming extra transport authority is rejected), bounded request/
      response sizes and deadlines, collision-free synthetic wire ids
      (`aiworkhub-sideband-<uuid4>-<seq>`) so a sideband response is routed
      back over the socket and never leaks into the extension's stdout,
      duplicate-request suppression (bounded TTL cache keyed by
      method+params), and readiness gated on passively observing the
      EXTENSION's own `initialize`->`initialized` handshake complete on the
      child connection -- never originated by the mux. Before readiness, on
      socket loss, or on protocol mismatch, a sideband call fails closed
      with a bounded `SidebandNotReady` deferral; this mux never spawns a
      second/separate App Server as a fallback.
- [x] `aiworkhub/callback_bridge.py::SidebandCallbackClient`: reaches
      the extension-owned App Server through the mux's socket instead of
      spawning a subprocess. Resumes the exact bound thread, reuses the
      SAME `select_steer_target` decision as `AppServerClient` (active with
      one in-progress turn -> `turn/steer`; idle -> `turn/start`), and
      treats the matching SYNCHRONOUS sideband response as full delivery
      acknowledgement -- never waiting for `turn/completed` (that
      notification flows to the extension's own stdout via the mux, not to
      this socket). `CallbackBridge(transport="sideband")` wires this in
      alongside the existing subprocess transport (default unchanged) while
      preserving deterministic `clientUserMessageId`, the durable batch/
      outbox/lease/retry/dead-letter machinery, and the fixed trusted
      prompt builders unchanged.
- [x] `scripts/install_vscode_app_server_mux.py`: dry-run/check/
      print-config only -- prints the exact `chatgpt.cliExecutable` value
      and rollback steps; never modifies VS Code settings, the installed
      extension, systemd, the live callback DB, or any process. Codex owns
      applying the setting, the extension host reload, the live canary
      against the extension-owned thread, recovery, and finally enabling
      `aiworkhub-callback-bridge.service`.
- [x] Fake child-App-Server (`tests/_fake_app_server.py`, unchanged, reused)
      plus fake extension-client (OS-pipe-driven, `tests/test_app_server_mux.py`)
      E2E coverage: transparent extension traffic, server-originated
      notifications reaching the extension, active-turn steer without
      leaking the sideband response to the extension, ID-collision
      resistance under concurrent extension+sideband traffic,
      unauthorized-capability/wrong-uid/malformed/oversized/forbidden-field
      rejection, initialize/arbitrary-method rejection, not-ready deferral
      with no second App Server spawn, duplicate-request suppression, and
      clean shutdown (socket + capability file removed). Mirrored
      integration coverage for `SidebandCallbackClient` lives in
      `tests/test_callback_bridge.py`.
- [x] Live wiring: Codex applied the mux/runtime integration, reloaded the
      extension host, runs the live canary against the extension-owned
      thread, and only then flips `CallbackBridge` to `transport="sideband"`
      and re-enables the callback service. Not performed by this worker
      (forbidden: `separate_app_server_fallback` stays forbidden even after
      this lands -- this mux IS the fallback-free replacement).
- [x] Historical isolated-worktree `AITools/taskdb.py` gap is superseded by the
      standalone canonical task store and durable callback implementation. The
      original observation was:
      `callback_outbox`/`callback_batches` functions (`claim_pending_callback_batch`,
      `callback_outbox_stats`, etc.) that `callback_bridge.py` already
      depends on for both transports -- a pre-existing environment gap
      outside this task's `allowed_writes`, not a regression from this
      change (`test_callback_bridge.py`'s outbox/batch-dependent tests fail
      identically with or without this task's edits; the 11 new
      `SidebandCallbackClient`/`AppServerMux` tests bypass that path
      entirely and are green). Codex should reconcile this worktree's
      `AITools/taskdb.py` against the trusted host's canonical copy before
      any live enablement.

### Phase 7b — B472: multi-instance sideband owner routing

Status: candidate ready for Codex review
(`CLAUDE_SONNET5_TASK_MCP_MULTI_INSTANCE_SIDEBAND_ROUTING_B471_V2`). The
B409/B471 live canary found a defect one step beyond the `select_steer_target`
selector: three concurrent VS Code extension-host mux PIDs were simultaneously
alive, all bound the SAME fixed `sideband.sock`/`sideband.cap` path, and the
newest process silently shadowed the origin thread owner's endpoint.

- [x] `AppServerMux` now binds a random per-instance `<id>.sock`/`<id>.cap`
      pair (never a fixed shared name) and never unlinks a pre-existing path
      -- a collision regenerates a fresh id and retries, bounded.
- [x] Each instance publishes an atomic, owner-only (0600) registry file
      under `sideband_dir/instances/<id>.json`: instance id, pid, pid
      start-time (`/proc/<pid>/stat` field 22 -- guards stale-PID-reuse),
      socket/capability paths, and the thread ids its OWN extension traffic
      has bound. No prompt/thread-content/secret is ever persisted.
- [x] Thread ownership is derived ONLY from the extension's own
      `thread/resume`/`turn/start`/`turn/steer` requests observed on the
      extension->child pump; a sideband-issued probe (what the callback
      bridge itself sends) never passes through that observer and can never
      claim or steal ownership.
- [x] `SidebandCallbackClient` resolves the unique live owning instance
      before every call (`find_owning_sideband_instances`); missing or
      ambiguous ownership durably parks the whole batch
      (`SidebandOwnerNotFoundError`/`SidebandOwnerAmbiguousError`, both
      `BusyThreadError`) instead of guessing, fanning out, or consuming the
      dead-letter hard-failure budget.
- [x] B471's selector/projection (`select_steer_target`, unique-newest-by-
      `startedAt`, the `thread/resume` id/status/startedAt projection) is
      unchanged.
- [x] Two-(and-more)-instance tests added: endpoints coexist without
      collision, separate threads resolve to their correct owner (never the
      wrong fake App Server), a sideband resume cannot steal ownership,
      stale/pid-reuse registry rows are ignored, and an ambiguous owner
      durably parks. Existing B409/B416/B471 security, dedup, redaction and
      batch-atomicity tests are unchanged and green.
- [x] Live multi-instance wiring was activated and is now covered by the
      current manager-route/callback watchdog architecture. Historical steps
      retained below: Codex owned applying
      `chatgpt.cliExecutable`, the extension host reload, the live canary,
      and flipping `CallbackBridge` to `transport="sideband"`.

### Phase 7c — B657 recursive required-output validation

Status: coordinator-accepted.

- [x] Terminal `/**` required-output declarations now enumerate nested regular
      files deterministically instead of receiving directory-only matches from
      `pathlib` and falsely failing `required_output_no_matches`.
- [x] Empty/directory-only trees still fail; matched symlinks, traversal,
      out-of-scope paths, zero-byte outputs and unchanged outputs retain their
      fail-closed behavior.
- [x] Focused regression passes `5/5`, the existing worker-workspace suite
      passes `35/35`, and exact replay over B656B finds all `231` required
      records including `224` shard files with zero duplicates.
- [x] The worker terminal `validation_failed` was a card-command error
      (`unittest` invoked a pytest-authored suite in a sandbox without pytest),
      not a product failure. Coordinator reran the suite with pytest before
      promotion.

## B753 Validation Executable Scratch

- Accept the request-private, exec-probed validation scratch after parent replay passes `67/67` tests, including `15/15` focused compile/execute, cleanup, timeout, isolation and credential checks. This closes the false native `rc=126` path without widening the worker worktree or exposing credentials.
- The worker terminal `validation_failed` is a second environment false negative: sanitized HOME cannot see the operator user-site `pytest` package. B755 owns only a read-only approved pytest-runtime binding; B753 is not rerun. The Task MCP server must reload before new launches use the accepted scratch implementation.

## B674 Distinct-Launch Callback Review

- Accept the reproduced root cause and the standalone `15/15` taskdb/taskctl evidence, but reject the runtime candidate. It deliberately bypasses `RUNNER_TOPIC_ALLOWLIST` by omitting runner/topic and treats launch-request binding as nonfatal, so a failed bind can still launch a worker and reproduce the lost-wake bug.
- No B674 runtime path is promoted. B756 owns one fail-closed, exactly allowlisted launch identity contract: binding must succeed before the child process starts, same-launch retries deduplicate, distinct launches get distinct durable wakes, and migration never replays historical rows.

## B755 Validation Pytest Runtime

- Accept the pytest-only approved-runtime binding after parent replay passes `14/14` focused tests and `50/50` worker-workspace/B753 regressions. Isolated validation can now import the operator-installed pytest package without exposing the real HOME, credentials, arbitrary host paths, copied packages or writable package state.
- Non-pytest validation preserves the previous environment/argv path, unavailable or untrusted pytest roots fail closed, and B753 request-private executable scratch cleanup remains intact. The Task MCP server must reload before future launches use this implementation. B756 remains the sole callback launch-identity dependency.

## Deferred Public Product Extraction — AI Working Hub

This is a future task only; it must not interrupt the current AIWorkHub roadmap.

- Public product/repository name: **AI Working Hub** (`AIWorkHub`).
- Positioning: repository-native, model-agnostic AI orchestration, source
  context, sessions, memory, knowledge and editor monitoring.
- Each opened repository owns its complete durable state under
  `.aiworkhub/`. On first attach the tool initializes that directory; on
  later attaches it discovers and resumes the existing state directly.
- MCP/VS Code installations remain replaceable and do not own project data.
  Credentials and disposable process/runtime files remain outside canonical
  repository state.
- No central multi-repository knowledge service is planned. Each editor window
  opens the dashboard for its active repository folder. The only shared layer
  is a multi-thread/VS Code-instance mux routing callbacks by stable
  `(repo_id, thread_id, task_id, event_id)` identity.
- Intended public identifiers: repository `AIWorkHub`, package/CLI
  `aiworkhub`, short CLI alias `awh`, MCP server `aiworkhub-mcp`, and
  VS Code extension `AI Working Hub`.
- Extraction begins only after the current Task MCP is operationally closed;
  project-specific AIWorkHub policy remains a repository profile rather than part
  of the generic public core.

## Non-goals For MVP

## B756 callback launch identity rebase

- The retained B756 candidate was recovered after its worker validation failed only because the old sandbox could not import pytest.
- Exact launch `request_id` is now validated and bound in the same transaction as claim-start; malformed identity fails closed before child spawn.
- Callback dedup now includes request identity while legacy delivered rows survive migration. Parent verification passes `37` callback and `52` launcher/workspace regressions.
- Reload Task MCP to activate the newly imported server code.

- No rewrite of `taskctl.py`.
- No central multi-project server yet.
- No direct model API orchestration until local task safety is proven.
- No default-on write actions.
