# Oh My OpenAgent: Competitive Assessment and Development Handoff

Date: 2026-10-02
Target: AIWorkHub
Upstream: https://github.com/code-yeongyu/oh-my-openagent
Status: research and proposed development backlog, not an approved implementation plan
Scope: documentation-only handoff; no production changes or benchmark execution

## Executive Decision

Treat OmO as a serious competitor in multi-model coding-agent orchestration,
and as a source of design ideas, not as a code donor by default.

AIWorkHub should remain the repository-native control plane for existing agents:
repository identity, dependency and write-scope control, isolated execution,
independent evidence review, canonical promotion, and durable learning.
Do not build another general-purpose agent harness merely to match OmO's breadth.

Recommended order: unified diagnostics and onboarding, proactive memory recall,
then measured read-only tool batching. Lifecycle chaos tests can accompany the
existing hardening work. Routing changes need their own policy review.

## Evidence and Limitations

This report uses upstream README, feature/configuration references, the Senpi
task-engine architecture notes, a model-routing source excerpt, and Context7
documentation. Local evidence includes AIWorkHub's canonical README, donor-port
contract, manager bootstrap, Source Graph, and bounded memory/context queries.

Upstream's inspected default branch was `dev`; the repository page displayed
commit `61086739c3b00f0990cbcdcdfde9146791e243db` and latest release `v5.1.10`.
The raw document requests used moving `dev` URLs, not an immutable snapshot.
Revalidate upstream sources before implementation; do not assume every fetched
document corresponds exactly to that commit or release.

At inspection, GitHub displayed approximately 69.8k stars and 337 contributors.
These indicate reach, not correctness, active-user count, or engineering quality.

No OmO installation, end-to-end run, security audit, or comparative benchmark was
performed. Upstream capability descriptions are documented claims, not locally
verified behavior. Local documentation also distinguishes shipped foundations,
active hardening, and planned features; do not treat all of them as complete.

OmO Native/Senpi, its OpenCode edition, and its Codex adapter have different
contracts and defaults. Never combine their capabilities into one assumed API.

## Competitive Position

| Area | Overlap and implication |
| --- | --- |
| Multi-model delegation and parallel agents | Direct competition; not a unique AIWorkHub feature. |
| Task DAG, persistent state, recovery | Significant overlap; upstream Native documents WAL/checkpoints and recovery. |
| Memory and review workflows | Both exist upstream; distinguish enforceable contracts, not feature names. |
| Setup and daily ergonomics | OmO emphasizes a single install/run path and integrated diagnostics. |
| Repository-bound authority and promotion | AIWorkHub's strongest positioning hypothesis; demonstrate it with tests and receipts. |
| Research, browser and desktop automation | OmO targets broader work; matching all of it would expand AIWorkHub's scope. |
| Source intelligence | AIWorkHub has a canonical Source Graph; OmO documents LSP and AST tooling. No comparative retrieval benchmark was run. |

Do not claim that OmO is only prompts, lacks isolation, has no durable state, or
has no review. Its Native task-engine notes explicitly describe checkout
isolation, state transitions, leases, recovery, and QA/reviewer agents.
Nor does this assessment prove AIWorkHub is more reliable or cheaper.

## Existing AIWorkHub Authorities to Reuse

Use the existing Task DAG, allowed-write collision checks, callback delivery,
workforce routing, isolated workspaces, Quality Evidence, semantic edits,
Session Manager, AI Memory, KB, and Source Graph. Do not introduce competing
task, memory, decision, or context databases.

Canonical references:

- [AIWorkHub overview](../../README.md)
- [Donor capability port contract](../DONOR_CAPABILITY_PORT.md)
- [Architecture](../ARCHITECTURE.md)
- [Tool-use policy](../AIWORKHUB_TOOL_USE_POLICY.md)

## Proposed Work Packages

The identifiers below are report labels, not canonical task IDs. Each package
requires a Source Graph-first gap assessment before a manager creates a card.
Candidate boundaries are discovery hints, not approved write scopes.

### R1: Unified Diagnostics and Onboarding

Priority: first. Prefer composing existing diagnostics over new health logic.

Upstream idea: `omo doctor` explains configuration, provider availability,
model resolution, migration leftovers, and recovery steps in one place.

AIWorkHub proposal: one repository-bound projection of existing preflight,
health, provider/model availability, and route diagnostics, usable from the
existing MCP/dashboard surfaces. A new CLI command is optional, not required.

First investigation: identify current health/preflight projections and determine
which user-visible failures require consulting several separate surfaces.

Acceptance criteria:

- Every status carries repository identity and distinguishes configured,
  detected, authenticated, and actually launchable states.
- Unknown/unmeasured states are explicit; a saved login is not proof of health.
- The default diagnostic path is read-only, bounded, and secret-redacted.
- It never installs tools, changes credentials, switches routes, or repairs
  configuration without a separate authorized action.
- Tests cover absent executables, stale credentials, unavailable models,
  repository mismatch, and supported local/Remote-SSH platform differences.
- Measure time and user actions needed to diagnose representative setup failures.

### R2: Proactive Memory Recall

Priority: second, after verifying whether equivalent recall already exists.

Upstream idea: Kibitzer is a resident, cheap, read-only sidecar that sees bounded,
redacted events and offers a short reminder only when memory changes the next
decision. Unchanged candidates do not repeatedly wake it.

AIWorkHub proposal: surface relevant accepted lessons and constraints through
existing AI Memory/KB authorities. Begin with deterministic candidate selection;
add model judging only if a measured relevance gap justifies its cost.

First investigation: map learning receipts, current retrieval, and manager/worker
context injection. Replay historical repeated-error cases with and without hints.

Acceptance criteria:

- The helper cannot edit files, launch tasks, mutate memory, or accept work.
- Hints identify their source and are explicitly advice, not current-state truth.
- A hint cannot override current repository state or manager authority.
- Events are redacted before transmission; project memories remain repo-scoped.
- Candidate deduplication, bounded queues, cancellation, failure backoff, and
  capability-unavailable behavior have focused tests.
- Measure useful-hint rate, false-positive rate, repeated-error rate, latency,
  and actual structured provider usage when available. Report unknown usage.
- Durable new lessons still use existing write-intent and manager review gates.

### R3: Read-Only Tool Batching

Priority: third; start smaller than a general JavaScript/Python execution kernel.

Upstream idea: CodeMode combines independent tool operations into one program
and round trip. Documentation describes scoped child permissions for nested
calls. Published context-saving figures are upstream observations only.

AIWorkHub proposal: assess a typed, allowlisted batch for independent read-only
MCP operations. First establish whether existing clients already batch these
operations, and whether server-side batching improves measured overhead.

First investigation: select one real orientation/review workflow with repeated
independent reads. Compare serial calls, current client parallelism, and a
bounded batch using the same requested evidence and output limits.

Acceptance criteria:

- Each item passes the existing role, repository, and permission checks.
- Preserve per-item provenance, audit receipts, and errors; batch success must
  not hide failed operations or imply receipt acknowledgement.
- An item cannot change the active repository or gain the parent's privileges.
- Writes, shell execution, task transitions, and promotion are excluded initially.
- Enforce item/output bounds, cancellation, and a deterministic result mapping.
- Cached and spilled results retain existing retrieval/provenance contracts.
- Measure elapsed time, round trips, bytes, and provider tokens only when directly
  observable. Do not infer token savings from byte reductions.

### R4: Category Routing and Skill Selection

Priority: follow-up, only for a demonstrated gap in existing workforce routing.

Upstream idea: categories describe the kind of work; skills describe needed
knowledge/tools. Model/provider chains and concurrency govern execution.

AIWorkHub proposal: reuse the workforce catalog to expose work presets and
required capabilities separately from model names. Prefer measured accepted
outcomes over copying upstream's hardcoded model preferences.

Acceptance criteria:

- Explicit owner model/provider constraints take precedence.
- Provider changes require policy permission; queue pressure alone must not
  silently change pricing or the data recipient.
- Diagnostics explain selection, capability gaps, and fallback eligibility.
- Scheduling respects task dependencies and write collisions before capacity.
- No automatic token cap is introduced without an exact authorized budget.
- Tests cover unavailable chains, provider denial, exhausted lanes, cancellation,
  and supported platform/adapter differences.

### R5: Lifecycle Chaos and Failure-Injection Tests

Priority: alongside the current lifecycle hardening, not a replacement lifecycle.

Upstream idea: seeded chaos tests assert notification deduplication, terminal
idempotence, no concurrency-slot leaks, and no unhandled asynchronous failures.

AIWorkHub proposal: extend neighboring lifecycle/callback tests with reproducible
crash and race schedules. Reuse existing fixtures rather than adding a new suite
framework by default.

Acceptance criteria:

- Exercise crash-before/after-claim, delivery-before/after-acknowledgement,
  manager transfer, cancellation versus completion, and finalization retry.
- Reconcile the same task identity; do not create replacement IDs after lost ACKs.
- Repeated delivery never causes repeated canonical acceptance or promotion.
- Worker completion is never treated as manager acceptance.
- No capacity/lease leak, cross-repository mutation, or cancelled-worker revival.
- Failed schedules print a reproducible seed and bounded evidence.
- Check transient and recovered states, not only the eventual happy-path result.

## Licensing and Reuse Gate

Upstream's main license is Sustainable Use License 1.0, not MIT. Its text limits
use/modification to internal business, non-commercial, or personal purposes,
and distribution to free non-commercial purposes. Third-party components retain
their own licenses. This is a risk assessment, not legal advice.

Do not copy upstream implementation, prompts, skills, tests, or assets into
AIWorkHub's MIT distribution without a component-specific license determination
and, where necessary, permission/legal review. Public availability is not a
permission to redistribute under MIT.

Prefer independent implementation of behavioral ideas using our existing
contracts. Record sources and provenance. Before any component reuse, identify
its exact version, applicable license, attribution requirements, and permitted
distribution. Do not infer that a vendored component has the repository license.

## Coding Model Execution Contract

1. Read this report as a research input, not as blanket authorization to implement
   all packages. Ask the manager for the selected canonical card.
2. Bootstrap/verify the assigned repository and follow the role-specific MCP
   protocol. Use Source Graph at the active boundary before filesystem discovery.
3. Establish what already exists; select one missing behavior and one focused
   validation that can disprove the proposed implementation.
4. Keep tests and required production call sites inside the card's allowed writes.
   Independent cards may run together only after dependency/collision checks.
5. Use the prescribed bounded semantic-edit workflow for existing files. This
   report itself was added as a new file, which is the new-file exception.
6. Validate immediately after the first substantive edit, then repair or narrow
   the hypothesis before expanding scope.
7. Submit measured evidence and limitations. Stop at manager/Codex review;
   do not self-accept, promote, publish, or change repository authorities.

Suggested evidence table for each experiment:

| Criterion | Baseline | Candidate | Evidence | Outcome |
| --- | --- | --- | --- | --- |
| Correctness/invariants | Record observed result | Same contract | Test/artifact identity | Pass/fail/unknown |
| Latency and overhead | Same workload/environment | Same requested evidence | Timings and call counts | Measured delta |
| Provider usage | Structured usage or unknown | Structured usage or unknown | Provider receipt | No estimates disguised as measurements |
| Platform scope | Actually exercised surfaces | Actually exercised surfaces | Platform-specific logs | Untested surfaces listed |

## Deferred Direction: OmO as an Executor

An OmO runtime adapter is a hypothesis, not an approved feature. Investigate only
if owner demand justifies it. Verify noninteractive invocation, structured output,
cancellation/process identity, scoped MCP access, workspace isolation, usage
reporting, and licensing before promising compatibility. Avoid double task
schedulers or competing managers; AIWorkHub must retain canonical task and
acceptance authority. Existing OpenCode support does not prove OmO Native support.

## Sources

Accessed 2026-10-02. Recheck moving upstream references before implementation.

- [Repository and README](https://github.com/code-yeongyu/oh-my-openagent)
- [Features and edition caveats](https://github.com/code-yeongyu/oh-my-openagent/blob/dev/docs/reference/features.md)
- [Configuration, Kibitzer, concurrency and fallback](https://github.com/code-yeongyu/oh-my-openagent/blob/dev/docs/reference/configuration.md)
- [Native configuration and admission behavior](https://github.com/code-yeongyu/oh-my-openagent/blob/dev/docs/reference/omo-json.md)
- [Senpi task lifecycle, isolation, DAG and QA](https://github.com/code-yeongyu/oh-my-openagent/blob/dev/packages/senpi-task/AGENTS.md)
- [Agent fallback-chain implementation](https://github.com/code-yeongyu/oh-my-openagent/blob/dev/packages/senpi-task/src/agents/builtin/fallback-chains.ts)
- [CodeMode release descriptions](https://github.com/code-yeongyu/oh-my-openagent/blob/dev/CHANGELOG.md)
- [Scoped kernel-tool change notes](https://github.com/code-yeongyu/oh-my-openagent/blob/dev/changes.md)
- [License](https://github.com/code-yeongyu/oh-my-openagent/blob/dev/LICENSE.md)

Research context: Context7 library `/code-yeongyu/oh-my-openagent`; verified local
repository `repo_be72b4028e3c4e789badc4d5d631d4bd`; manager session
`01a0f701-35a9-7141-a63d-861414d9fb7e`. These identify the research context,
not a task claim, implementation approval, benchmark, or acceptance receipt.