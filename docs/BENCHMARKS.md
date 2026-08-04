# AIWorkHub Benchmarks

AIWorkHub publishes measured evidence only when its population, denominator and
limitations are explicit. Structural bytes, provider tokens, elapsed time,
cost, task outcomes and manager acceptance are separate populations; one is
never substituted for another.

## Evidence grades

| Grade | Meaning |
| --- | --- |
| **Verified snapshot** | Canonical runtime ledger or deterministic release check; reproducible arithmetic, but not necessarily a causal comparison |
| **Observed association** | Real task cohorts with different outcomes; useful operational evidence, but tasks/models were not randomized |
| **Pilot A/B** | Matched variants exist, but the sample or controls are insufficient for a general product claim |
| **Unmeasured** | The required counterfactual does not exist; AIWorkHub reports no multiplier |

## Complete benefit snapshot for 0.8.81

Captured from AIWorkHub managing its own repository on 2026-08-04. The
machine-readable snapshot is
[`benchmarks/system-benefit-snapshot-v1.json`](../benchmarks/system-benefit-snapshot-v1.json)
and CI checks its denominators with
[`scripts/check_system_benefit_snapshot.py`](../scripts/check_system_benefit_snapshot.py).

| Surface | Population | Measured result | Evidence grade | What it means |
| --- | ---: | --- | --- | --- |
| Release quality | Python + extension qualification | 1,943 Python tests passed, 27 skipped; 29 extension test files passed; wheel, sdist and VSIX checks passed | Verified snapshot | The release artifact has broad automated regression coverage; this does not prove worker task quality |
| Runtime readiness | One canonical preflight snapshot | 6/7 execution routes launchable; 9/9 configured workers available; 7 had observed outcomes | Verified snapshot | Several model families can be routed from one repository control plane; one redundant route remained unavailable |
| Source Graph enforcement | 7 current gated tasks | 7/7 satisfied and 7/7 used live graph evidence; 13 calls, 0 failures, 846 hits | Verified snapshot | The current enforced cohort used live structural context instead of satisfying the gate with stale/injected-only evidence |
| Source Graph latency | 13 current calls | p50 15.024 ms; p95 39.014 ms | Verified snapshot | Graph lookup overhead was small in this repository/runtime snapshot |
| Source Graph historical coverage | 175-run KPI window | 92.2% live rate, 93.8% gate satisfaction, 97.3% useful-call rate | Verified snapshot with legacy rows | Older rows reduce mode/stage attribution; they are not equivalent to the cleaner 7-task cohort |
| Tool-use association | 170 runs across three comparable telemetry cohorts | `missing` 7.2% review-ready; `live_single_stage` 26.7%; `continuous_use` 33.3% | Observed association | Live/continuous Source Graph use coincided with higher review-ready rates, but task difficulty, model and era are confounders; this is not causality |
| Focused semantic edits | 3 authenticated edit receipts | 31,998 file bytes vs 531 replacement bytes; 1.66% replacement/file; 60.26× structural ratio; 0 old bytes re-emitted | Verified structural snapshot | Models returned small replacement bodies rather than full existing files; the 60.26× figure is a byte-shape ratio, not token savings |
| Semantic-edit provider A/B | 2 paired Codex GPT-5.5 tasks | 27.5% fewer total tokens, 24.9% fewer output tokens, 21.5% less elapsed time; uncached input **20.3% higher** | Pilot A/B | Promising end-to-end evidence, but too small, cache-confounded and not manager-acceptance matched |
| Provider read behavior | 5 tasks with usable provider traces | 4/4 recognized reads bounded; 0 exact/overlap rereads; 15,820 bytes observed | Pilot telemetry | The measured Codex sample avoided whole-file/unbounded reads; other adapters were not observable, so no fleet-wide claim is allowed |
| Context delivery | 156 tasks | Optional suppression removed 8,424 bytes (0.6%), but the signed envelope added 308,843 bytes; delivered context expanded by **300,419 bytes (20.0%)** | Verified negative result | Current context packaging is a measured optimization target, not a savings success; no raw-file or token counterfactual exists |
| Callback reliability | 271 events | 176 delivered, 95 superseded, 0 dead letters, backlog 0; 100% resolved without dead letter | Verified snapshot | Terminal notifications were durably delivered or intentionally superseded; this is not a latency SLA |
| Quality selectivity | 216 manager decisions | 25 accepted, 191 rejected; 11.6% acceptance | Verified snapshot | The manager gate is selective and catches non-acceptable work; it does not by itself show that AIWorkHub caused better code |
| Worker outcomes | 154 terminal runs | 23 review-ready (14.9%); 59 validation-failed (38.3%); 72 other non-green | Verified snapshot | The system records failure honestly. This also shows substantial remaining efficiency headroom |
| Provider usage | 110 usage records | 196.35M tokens observed; $63.82 known cost; 89 records and 121.14M tokens have unknown cost | Verified incomplete ledger | Token accounting is much more complete than dollar accounting; no system-wide ROI or cost-savings claim is valid yet |

## What users can reasonably expect today

1. **Less full-file regeneration on eligible existing-file edits.** The
   deterministic applier binds a Source Graph range to file/fragment hashes and
   rejects stale or ambiguous edits before writing the isolated worktree.
2. **One operational loop instead of disconnected agent utilities.** Planning,
   model routing, isolated workers, durable context, callbacks, validation,
   manager review and retention share repository identity and evidence.
3. **Fewer silent false greens.** Validation failures, launch failures,
   timeouts, token ceilings and manager rejection remain distinct outcomes.
4. **Observable tool economics.** AIWorkHub records what the worker actually
   queried/read/emitted where the provider exposes evidence, and labels missing
   evidence instead of turning it into zero.
5. **No proven universal token multiplier yet.** The Source Graph's end-to-end
   raw-file counterfactual, accepted-quality parity and multi-model randomized
   ablation remain unmeasured.

## How AIWorkHub differs from adjacent tools

This is a capability comparison based on each project's linked official
documentation, not a claim that AIWorkHub wins their benchmarks.

| Capability | AIWorkHub | Graphify | Serena | Aider | Cline |
| --- | --- | --- | --- | --- | --- |
| Primary scope | Repository-native multi-model engineering control plane | Queryable code/docs/media knowledge graph | MCP semantic retrieval, editing and refactoring toolkit | AI pair programmer with repository map | Coding agent plus multi-agent Kanban/SDK |
| Structural code intelligence | 34 configurable families, 31 bounded modes, continuous-use receipts | Tree-sitter graph across ~40 languages; communities, query/path/explain; docs/media graph | LSP/JetBrains symbol graph and references across 40+ languages | PageRank-selected repo map under a soft token budget | Public overview states cross-file understanding; graph implementation is not the comparison target |
| Focused deterministic edits | Hash-bound range preparation, replacement-only model output, atomic local apply | Retrieval product; editing is not its documented primary scope | Replace/insert symbol body, rename and other refactors | Model-specific edit/diff formats | Reviewed diffs and checkpoints |
| Multi-agent task DAG and isolation | Dependency DAG, collision checks, isolated workers and explicit manager finalization | Not documented as product scope | Not documented as product scope | Not documented as product scope | Kanban cards have worktrees and dependency chains |
| Durable context | Session Manager, AI Memory, KB and manager-only Context Graph with distinct authority | Persistent `graph.json` plus report | Project memory system | Chat context/history plus repo map | Persistent team/session state and checkpoints |
| Evidence-first acceptance | Required outputs, validation receipts, diffs, hashes, logs, callbacks and independent manager accept/reject | Provenance labels on graph edges | Tool-level semantic operations; no equivalent task acceptance loop documented | Git commits/tests in the coding loop; no equivalent manager evidence ledger documented | Human-reviewed diffs/checkpoints; no equivalent canonical evidence bundle documented in the overview |
| Built-in operations telemetry | Tool modes/stages, reads, context bytes, semantic edits, callbacks, outcomes, tokens/cost quality and retention | Publishes graph/memory benchmarks | Publishes an agent evaluation method | Publishes coding benchmarks and token-aware repo-map controls | Operational product features; no directly comparable evidence ledger claimed here |

Official comparison sources:

- [Graphify README](https://github.com/Graphify-Labs/graphify#readme)
- [Serena README](https://github.com/oraios/serena#readme)
- [Aider repository map](https://aider.chat/docs/repomap.html)
- [Cline README](https://github.com/cline/cline#readme)

Graphify currently documents richer graph-community/path analysis and broader
non-code ingestion. Serena documents deeper LSP/IDE refactoring. Aider has a
mature token-budgeted repo-map and coding benchmark program. Cline has a broader
standalone agent/product ecosystem. **AIWorkHub's present differentiation is
not proven superiority in those individual components; it is their integration
into one local, repository-scoped, evidence-gated multi-model control loop.**

## Promotion gate for a real product-economics claim

A result can move from pilot evidence to a product claim only after:

1. a preregistered task population and frozen pair protocol;
2. randomized or order-balanced variants across multiple task families;
3. identical model, adapter, context budget and validation contract per pair;
4. authenticated tool/read/edit receipts and provider usage;
5. explicit manager acceptance or blind quality adjudication;
6. cache-aware reporting, failure/retry accounting and confidence intervals;
7. enough independent pairs to report a stable distribution, not only a mean.

Until then, AIWorkHub reports the observed sample and its limitations verbatim.
