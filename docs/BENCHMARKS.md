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

## Current 0.8.82 optimization evidence

| Surface | Population | Measured result | Evidence grade | What it means |
| --- | ---: | --- | --- | --- |
| Context Bundle v2 encoding | One deterministic same-evidence representative fixture | 849 legacy-v1 bytes to 600 nested-v2 bytes; −249 bytes, **29.329% structural reduction** | Verified structural fixture | Nested JSON avoids string escaping and duplicate wrappers. Provider tokens, cost, latency and accepted quality remain unmeasured until live paired tasks run |

The machine-readable fixture is
[`benchmarks/context-envelope-encoding-v2.json`](../benchmarks/context-envelope-encoding-v2.json),
and CI recomputes its arithmetic and claim boundaries with
[`scripts/check_context_envelope_benchmark.py`](../scripts/check_context_envelope_benchmark.py).

## Source Graph exact-symbol slice precision

One deterministic noisy-file fixture contains 80 unrelated entry/helper call
pairs plus one target pair. The legacy file-scoped slice collected 100 edge
rows (`21,921` bytes); the exact-symbol slice collects the one relevant edge
(`277` bytes): **98.736% less edge payload, a 79.14× structural ratio**.

This result proves that an exact `focus → slice:<qualname>` transition no
longer pays for unrelated calls merely because they share a large file. It is
not a provider-token, latency, quality or end-to-end cost claim. The checked
fixture is
[`benchmarks/source-graph-slice-precision-v1.json`](../benchmarks/source-graph-slice-precision-v1.json),
reproduced by
[`scripts/check_source_graph_slice_benchmark.py`](../scripts/check_source_graph_slice_benchmark.py).

## Provider routing observation (65 real runs)

This 2026-08-04 audit used the retained provider streams under
`.aiworkhub/runtime/process_logs/processes`. The parser matched the terminal
aggregate on 63/65 runs; the remaining two differences were defects in the
independent reference extractor, not in AIWorkHub's parser. The checked
observation is
[`benchmarks/provider-routing-observation-v1.json`](../benchmarks/provider-routing-observation-v1.json),
with arithmetic and claim boundaries enforced by
[`scripts/check_provider_routing_observation.py`](../scripts/check_provider_routing_observation.py).

| Observed model | Runs | Cache hit | Observed effective $/M tokens | Observed cost |
| --- | ---: | ---: | ---: | ---: |
| Claude Sonnet 5 | 21 | 97.2% | $0.658 | $40.31 |
| Claude Opus 4.8 | 9 | 96.4% | $1.929 | $31.61 |
| Claude Haiku 4.5 | 6 | 95.0% | $0.238 | $1.79 |
| Codex CLI | 29 | 74–97% per run | Unknown | Unknown |

The result rejects the earlier cache hypothesis: the Claude CLI cohort was
already at 95–97.2% cache hit, so more prompt-prefix work has little measured
headroom. Model routing is the larger observed lever. Opus cost **2.93×**
Sonnet and **8.11×** Haiku per token; its nine runs were 19% of Claude tokens
but 42.9% of the observed Claude cost.

Pricing the same Opus token volume at the observed Sonnet rate gives $10.78
instead of $31.61, a possible $20.83 (28.3% of the $73.71 Claude total)
difference. This is an **unrealized counterfactual, not a savings claim**.
AIWorkHub must first compare `observed_model × manager acceptance` on fresh
durable rows. If Opus materially improves accepted outcomes, some or all of
that premium buys quality rather than waste.

The same audit found 54,243 Codex `reasoning_output_tokens`, 22.7% of Codex
output, omitted from the old durable ledger. Current code records visible and
reasoning output separately while including both in billed output totals. It
also recognizes `cache_write_input_tokens`; the audited runs contained zero,
so that field closes a latent accounting gap rather than claiming recovered
cost.

## Retry and reviewer observation (114 canonical attempts)

The 2026-08-05 canonical repo-local ledger contains 114 usage attempts across
67 tasks. Of those, 47 records (41.2%) are attempts after the first recorded
attempt for their task and account for 88,311,967 tokens. Only six retry rows
have known provider cost ($20.885962); 41 retry rows have unknown cost. Six of
the 26 tasks with retries were ultimately accepted (23.1%). This is a measured
location of spend, **not** proof that the retry tokens were avoidable or that a
retry caused acceptance.

Historical topic-based attribution identifies two reviewer records with
1,251,357 tokens and 112 worker records with 195,782,165 tokens. All 114 roles
in this frozen population are inferred because the old event schema did not
persist role. From 0.8.88 onward, usage events explicitly store
`role=worker|reviewer`, while the ledger reports old inferred rows separately.

The checked snapshot is
[`benchmarks/retry-role-observation-v1.json`](../benchmarks/retry-role-observation-v1.json),
and [`scripts/check_retry_role_observation.py`](../scripts/check_retry_role_observation.py)
verifies its populations, arithmetic and non-causal claim boundaries. Raw
repo-local task events are private and are not published, so this is a checked
snapshot rather than a fully reproducible public raw-event benchmark.

## Evidence matrix

Captured from AIWorkHub managing its own repository on 2026-08-04. The
machine-readable snapshot is
[`benchmarks/system-benefit-snapshot-v1.json`](../benchmarks/system-benefit-snapshot-v1.json)
and CI checks its denominators with
[`scripts/check_system_benefit_snapshot.py`](../scripts/check_system_benefit_snapshot.py).

| Surface | Population | Measured result | Evidence grade | What it means |
| --- | ---: | --- | --- | --- |
| Release quality | Python + extension qualification | 1,944 Python tests passed, 27 skipped; 29 extension test files passed; wheel, sdist and VSIX checks passed | Verified snapshot | The release artifact has broad automated regression coverage; this does not prove worker task quality |
| Runtime readiness | One canonical preflight snapshot | 6/7 execution routes launchable; 9/9 configured workers available; 7 had observed outcomes | Verified snapshot | Several model families can be routed from one repository control plane; one redundant route remained unavailable |
| Source Graph enforcement | 7 current gated tasks | 7/7 satisfied and 7/7 used live graph evidence; 13 calls, 0 failures, 846 hits | Verified snapshot | The current enforced cohort used live structural context instead of satisfying the gate with stale/injected-only evidence |
| Source Graph latency | 13 current calls | p50 15.024 ms; p95 39.014 ms | Verified snapshot | Graph lookup overhead was small in this repository/runtime snapshot |
| Source Graph historical coverage | 175-run KPI window | 92.2% live rate, 93.8% gate satisfaction, 97.3% useful-call rate | Verified snapshot with legacy rows | Older rows reduce mode/stage attribution; they are not equivalent to the cleaner 7-task cohort |
| Tool-use association | 170 runs across three comparable telemetry cohorts | `missing` 7.2% review-ready; `live_single_stage` 26.7%; `continuous_use` 33.3% | Observed association | Live/continuous Source Graph use coincided with higher review-ready rates, but task difficulty, model and era are confounders; this is not causality |
| Focused semantic edits | 3 authenticated edit receipts | 31,998 file bytes vs 531 replacement bytes; 1.66% replacement/file; 60.26× structural ratio; 0 old bytes re-emitted | Verified structural snapshot | This directly removes full-file code re-emission when that is the baseline and therefore reduces that code-output component; it is not a 60.26× claim for all provider output, reasoning, retries or total cost |
| Semantic-edit provider A/B | 2 historical Codex GPT-5.5 pairs | Observed 27.5% fewer total tokens, 24.9% fewer output tokens and 21.5% less elapsed time; uncached input **20.3% higher** | Invalid for an uncapped causal claim | Explicit caps were present and pair 1 mismatched (`20k` vs `200k`); retain as historical observation only and rerun uncapped |
| Provider read behavior | 5 tasks exposed usable provider evidence | 4 recognized read operations, all 4 bounded; 0 exact/overlap rereads; 15,820 bytes observed | Pilot telemetry | The measured Codex sample avoided whole-file/unbounded reads; other adapters were not observable, so no fleet-wide claim is allowed |
| Legacy Context Bundle v1 delivery | 156 tasks on 0.8.81 | Optional suppression removed 8,424 bytes (0.6%), but the signed v1 envelope added 308,843 bytes; delivered context expanded by **300,419 bytes (20.0%)** | Verified historical negative baseline | The nested compact v2 structural fix shipped in 0.8.82 and is checked separately above; the same live fleet population has not yet been remeasured, so this row is retained as history rather than presented as current behavior |
| Callback reliability | 271 events | 176 delivered, 95 superseded, 0 dead letters, backlog 0; 100% resolved without dead letter | Verified snapshot | Terminal notifications were durably delivered or intentionally superseded; this is not a latency SLA |
| Quality selectivity | 216 manager decisions | 25 accepted, 191 rejected; 11.6% acceptance | Verified snapshot | The manager gate is selective and catches non-acceptable work; it does not by itself show that AIWorkHub caused better code |
| Worker outcomes | 154 terminal runs | 23 review-ready (14.9%); 59 validation-failed (38.3%); 72 other non-green | Verified snapshot | The system records failure honestly. This also shows substantial remaining efficiency headroom |
| Provider usage | 110 usage records | 196.35M tokens observed; $63.82 known cost; 89 records and 121.14M tokens have unknown cost | Verified historical incomplete ledger | This frozen 0.8.81 snapshot predates durable observed-model/reasoning-output fields; no system-wide ROI or cost-savings claim is valid from it |

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
5. **A model portfolio instead of one premium default.** Codex, Claude,
   DeepSeek, GLM and Copilot are execution routes rather than products the
   control plane replaces. Bounded throughput can go to an economical capable
   route while frontier models remain available for difficult judgment and
   review. Savings count only when validation and manager acceptance preserve
   quality.
6. **No proven universal token multiplier yet.** The Source Graph's end-to-end
   raw-file counterfactual, accepted-quality parity and multi-model randomized
   ablation remain unmeasured.

## AIWorkHub and its adjacent execution/context tools

This is a capability comparison based on each project's linked official
documentation. These systems are not presented as direct competitors: coding
agents are potential execution routes, while graph, retrieval and editing
toolkits can be complementary context capabilities.

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
standalone agent/product ecosystem. **AIWorkHub does not need to outperform a
coding agent at coding or a graph toolkit at graph analysis. Its role is to
integrate those capabilities into one local, repository-scoped,
evidence-gated, quality-aware and cost-aware multi-model control loop.**

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
