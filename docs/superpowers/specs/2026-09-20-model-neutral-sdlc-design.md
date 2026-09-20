# Model-neutral SDLC and evidence loop

Date: 2026-09-20
Status: design approved in conversation; implementation evidence pending

## Intent and success

AIWorkHub must enforce the complete Plan → Design → Build → Test → Deploy → Maintain loop for work performed by any supported model. The loop is a repository-bound service contract, not a collection of prompt or Markdown conventions. Managers and workers obtain stage data and gate decisions through MCP functions. Provider adapters may vary how they execute a worker, but must not vary the meaning of a stage, its evidence, or its acceptance criteria.

The same evidence loop must make Semantic Review delta-focused, enrich Source Graph with bounded LSP resolution, measure the effect of reasoning/context policy on outcomes, and qualify Muse Spark 1.3 Contributor as a development worker by observed performance. These are five deliverables sharing one evidence identity, not five independent truths.

Success is not a green dashboard alone. A completed case has attributable stage decisions, exact source and artifact identities, reproducible validation, and measured outcomes. Missing, stale, ambiguous, or mismatched evidence is `UNKNOWN` or a typed refusal, never success. Historical work is not backfilled with invented stage receipts.

## Scope and repository authority

- A case is scoped by verified `repo_id` and a durable `case_id`, with optional links to existing Roadmap outcomes, NeedFix entries, task cards, pull requests, and releases. These objects retain their existing authoritative lifecycles; the case is their evidence and transition envelope, not a replacement task queue.
- The canonical case and append-only stage receipts live under that repository's `.aiworkhub/` storage. No card or case is moved between repositories. Writes remain explicitly gated by `AIWORKHUB_ALLOW_WRITES=1` and existing manager/worker permissions.
- MCP read functions return bounded typed stage packets, source identities, provenance, and next permitted transitions. Markdown may be exported for humans, but no gate depends on an `.md` file existing or being read by a model.
- A model cannot self-attest a mechanical pass. Server-side gates consume recorded command, edit, review, source-graph, route, and release receipts. A manager records the final code disposition under existing authority. A human-approval switch is configurable per deployment environment; within explicitly configured targets the default is automatic, measurement-gated release/deploy. An unknown target or missing authorization is not implicitly approved.

## Stage contract

Every stage result has `schema_version`, `repo_id`, `case_id`, `stage`, `state` (`ready`, `blocked`, `unknown`, `not_applicable`), event time, producer identity, input digests, evidence references, gate findings, and an immutable receipt digest. A revision supersedes an earlier receipt without erasing it. A `not_applicable` stage carries a typed reason and policy proof; a small task may combine writing of packets but cannot silently omit a stage.

| Stage | Required data and exit condition |
| --- | --- |
| Plan | Intent, observed problem or opportunity, owner, expected outcome, risk, source links; approved intent is recorded. |
| Design | Falsifiable acceptance criteria, constraints, affected contracts and alternatives; the selected design is versioned. |
| Build | Task scope, allowed writes, route/effort/context decision, exact workspace and edit receipts; write conflicts are refused before launch. |
| Test | Tests and validation receipts, required-output deltas, source/eval coverage, failures and rework cause; mechanical checks run before model review. |
| Deploy | Accepted candidate identity, release/build provenance, target allowlist, measured gates, configured approval policy and rollback receipt. |
| Maintain | Observed outcome metrics, incidents/regressions, learning disposition, and a new intent when a control limit is breached. |

Transitions are idempotent on case, stage, input digest, and request identity. They fail closed on cross-repository evidence, missing predecessor, stale candidate, or contradictory receipts. Existing task event, NeedFix, Roadmap, and release records are linked rather than duplicated. The UI displays the same stage state and reason returned by MCP.

## Semantic Review

The default review input is the authenticated `semantic_edit_apply` delta joined to the exact task, claim, candidate digest, file paths, and changed ranges. The review builder maps those ranges to enclosing symbols, Source Graph callers/impact, affected contracts, and testmap entries. Mechanical validation and changed-hunk checks still run first. A reviewer sees the delta and only the bounded context needed to judge its effects, not an unrelated whole-file or whole-repository dump.

If an edit receipt is absent, stale, unjoinable, broader than the configured bound, or Source Graph impact is ambiguous, the system records a typed fallback and uses the existing diff/full-evidence path. It must not claim a narrow review occurred. Metrics include bounded bytes/tokens where observed, reviewer launches, latency, escaped defects, and fallback reasons; fewer tokens alone do not justify a quality regression.

## LSP-backed Source Graph

LSP is an index-build or incremental-refresh resolution backend for unresolved *repository-internal* edges. It does not expose unrestricted direct LSP calls to workers. Lexical/AST extraction remains the baseline; language-server results carry server identity/version, workspace configuration, source hash, location, confidence, and freshness into canonical Source Graph receipts. External stdlib/typeshed/third-party definitions are classified but do not inflate in-repository resolution quality.

The backend uses a bounded workspace excluding `.aiworkhub` and other generated storage, supports cancellation/timeouts, and produces deterministic persisted results for the same indexed tree and server version. Missing servers or failed queries leave explicit unresolved/unknown evidence and cannot make Source Graph falsely healthy. This repository must qualify Python and JavaScript/TypeScript paths end to end; the adapter protocol must support additional indexed languages without changing query semantics. A cross-repository C++ fixture exercises include-root and precompiled-header conditions without moving that repository's data.

Retrieval eval must include known `calls` and `impact` cases with exact expected edges and negative external-library controls. Gates report in-repo precision/recall and caller/impact correctness, not only the raw all-call resolved ratio. The previous Python LSP audit is a baseline hypothesis, not proof that a production backend has landed.

## Reasoning/context evaluation and Muse qualification

Route launch receipts record requested and *effective* effort, context window, model identity, adapter, capability source, and refusal/normalization reason. The existing reasoning policy tests establish functional wiring, not a quality gain. An isolated replay of accepted tasks compares settings within the same route and task family/risk tier. Report first-pass acceptance, severe review findings, validation/rework rate, elapsed time, token use, known cost, context truncation, and sample sizes. Missing prices or unmatched tasks remain unknown; association is not called causation. A settings change is retained only when paired evidence shows quality is not worse and the trade-off is visible.

`opencode-go/muse-spark-1.3-contributor` is the target Contributor route. `opencode/muse-spark-1.3-contributor-free` is a distinct route and cannot serve as its proof. Qualification proceeds through exact-route access and round-trip canary, low/medium/high difficulty tasks from the accepted-task corpus, tool/Source Graph/semantic-edit discipline checks, and manager-reviewed outcomes. It enters ordinary development-worker routing only for tiers that pass the published gate; an enabled catalog entry or one review-ready canary is insufficient. Regressions remove eligibility for the affected tier without erasing historical evidence.

## Failure handling and measurement

- A stage gate returns typed causes and the next actionable step; rework receives the prior failure delta. Infrastructure failures are separated from model-quality failures and never counted as a model rejection.
- Stage/candidate/reviewer identity mismatches, missing callbacks, stale Source Graph or missing LSP receipt, and deployment target mismatches stop transition with durable evidence. Recovery is idempotent and observable.
- The accepted-task eval corpus and live outcomes are distinct populations. Every metric states its denominator, coverage, truncation, sample period, and whether the result is observational or controlled. A claim of improvement requires matched evidence and a regression threshold fixed before comparing candidates.
- The release gate checks the case evidence, targeted tests, and rollback availability for the configured target. It does not infer deployment success from build success.

## Decomposition and ordering

1. **SDLC case protocol**: repository-local identity, stage receipts, MCP read/transition tools, UI projection, and fail-closed gates. This is the shared contract.
2. **Semantic Review**: exact edit-receipt join, impact-bounded packet, typed fallback, and quality/latency comparison. It consumes case/candidate identity.
3. **LSP Source Graph backend**: bounded language-server adapters, persisted edge provenance, `calls`/`impact` qualification cases. It strengthens the impact evidence used by review; the baseline review fallback works before LSP is ready.
4. **Reasoning experiment**: effective-setting receipts and paired outcome evaluation, independent of LSP implementation but linked to case identity.
5. **Muse 1.3 Contributor**: exact-route canaries and tiered worker qualification using the same stage and evaluation contracts.

The parts use separate write scopes where possible. Source Graph and Semantic Review changes to shared call sites are sequenced. Each part gets a focused implementation plan and tests; completion of an early part is not reported as completion of the full objective.

## Non-goals

Do not add a second task queue, depend on Markdown as the system of record, make all models use the same provider-specific prompt, route on unmeasured cost, count stdlib edges as internal LSP success, or weaken existing sandbox/permission boundaries for a benchmark.
