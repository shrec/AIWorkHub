# Universal development-skill pack: provenance-first audit

Audit date 2026-09-10. Research artifact only; no production skill records changed. No upstream prose is copied; all contracts are original AIWorkHub-native text. The sandbox has no egress, so commit pins were not fetched; coordinator-side adoption must pin each commit SHA and LICENSE sha256.

| Source / unit | Retrieved | Commit or tag | License path / identifier | Class |
|---|---|---|---|---|
| https://agentskills.io/specification | 2026-09-10 | unavailable | unavailable; page terms not verified | Reference-only: skill-file anatomy, progressive disclosure |
| https://github.com/anthropics/skills — README marks many skills Apache-2.0 (per-skill LICENSE paths) | 2026-09-10 | unavailable | Apache-2.0 per README metadata; per-path LICENSE text not fetched, digest unavailable | Open-source reusable only after each per-path LICENSE verifies at pin; translate patterns, no text copied |
| https://github.com/anthropics/skills — document skills docx, pdf, pptx, xlsx | 2026-09-10 | unavailable | Source-available per README metadata; not open source; text not fetched | Reference-only; no copying or vendoring |
| https://github.com/anthropics/skills — repository-wide license | 2026-09-10 | unavailable | none inferred; no repo-level LICENSE verified | Not classified; per-skill license paths govern |
| https://github.com/openai/plugins | 2026-09-10 | unavailable | unavailable; no verified LICENSE text | Reference-only: manifest structure concepts; treat as source-available |
| https://github.com/github/awesome-copilot | 2026-09-10 | unavailable (commit SHA not pinned) | root LICENSE / MIT per public metadata; LICENSE text not fetched, LICENSE sha256 unavailable | Reference-only index; linked skills need per-target license checks; popularity is not a criterion |
Evidence basis: license identifiers above are source metadata (README or listing statements read 2026-09-10), never verified license text; no commit/tag, license text or license digest was fetched in this sandbox, so each stays explicitly unavailable until coordinator-side pinning records the commit SHA and LICENSE sha256 per skill path. No repository-wide license is inferred for anthropics/skills.

## Rubric (weights sum to 100)

License safety 15; vendor neutrality 10; development-task frequency 15; quality impact 15; mechanical-failure prevention 10; token economy 10; deterministic validation 10; semantic-edit compatibility 10; cross-platform portability 5. Score 0-10 each; total = sum(score x weight)/10. Disqualify: any criterion below 4.

## Scored candidates (order as rubric)

- Codebase orientation 10/10/10/9/8/9/7/9/10 = 91.5 SELECT
- Evidence-first bug diagnosis 10/10/9/9/9/7/8/8/9 = 88.5 SELECT
- Implementation-with-tests 10/10/9/9/8/6/9/8/9 = 87.5 SELECT
- CI failure repair 10/10/8/8/9/8/9/7/8 = 86.0 SELECT
- Correctness and security review 10/9/7/9/7/6/8/8/9 = 81.5 DEFER
- Dependency and change-impact analysis 10/10/6/8/7/7/8/8/9 = 80.5 DEFER

Threshold: total >= 85 and no criterion below 6. Review defers on frequency and token cost; dependency analysis defers on lowest frequency and miner overlap. The pack ships four skills.

## Contracts (original; base applies to all four)

Base. Tool policy: Source Graph-only discovery with workflow_stage set; staged semantic edits on smallest ranges; markdown only; no scripts, no network, no inherited permissions. Failure behavior: structured blocker (prefix validation_unsupported_in_sandbox:) on missing evidence or denied tool; never stub or broaden scope. Validation: the card's declared validator via aiworkhub_worker_validation_run plus git diff --check. Telemetry: Source Graph receipts; aiworkhub.semantic_edit_runtime_evidence.v1 (range_count, old_region_bytes, replacement_bytes, model_reemitted_old_bytes); returncode, retries, wall time, tokens where exposed.

- C1 Codebase orientation. Trigger: first discovery turn on unfamiliar code. Required evidence: orientation-stage Source Graph receipt and indexed symbol list before any edit. Bounded workflow: focus query over read_first targets, body or file preview of top symbols, name candidate edit ranges.
- C2 Evidence-first bug diagnosis. Trigger: bug, NeedFix or failing test. Required evidence: failing-reproduction receipt (failure class, summary lines), body of suspect symbol, root cause citing line ranges. Bounded workflow: reproduce, locate, smallest edit, rerun validator, report or blocker; no fix without reproduction.
- C3 Implementation-with-tests. Trigger: feature card with testable contract. Required evidence: tests failing before and passing after, with receipts, plus edit coverage. Bounded workflow: read contract, stage failing tests first, minimal staged edits, run validator, summarize.
- C4 CI failure repair. Trigger: deterministic-gate or pipeline failure. Required evidence: failure class with sha256-addressed tail, corrected rerun receipt, retry count. Bounded workflow: classify mechanical versus semantic, smallest fix, rerun same validator, record retries.

## Controlled A/B protocol

Identical hidden tasks on GLM, DeepSeek, Claude and Codex; with-skill versus without-skill arms; same cards and snapshots; randomized task and arm order per model; arms blinded in analysis. Pin exact model and version, prompt sha256 and repository snapshot sha256 per run. Metrics per arm: pass rate; deterministic-gate rate; retries; mechanical failures; wall time; observed tokens and cost where exposed; Source Graph receipts; semantic-edit coverage. Report 95% Wilson and bootstrap intervals with paired McNemar or sign tests, never anecdotes. Minimum 30 pairs per skill per model before any read; promotion read at 120 pooled pairs; stop on harm (licensing or security incident), on futility (interval still spanning 0 below +2% at 60 pairs), or at completion. Contamination controls: fresh synthetic repositories absent from public corpora; no-headroom tasks excluded; n-gram screen of outputs for upstream-skill leakage. Promotion only if the improvement lower bound exceeds 0 with wall-time regression within 25% and token regression within 30%; no capability-equalization claim before results, and none is made here.

## Supply chain and prompt injection

- Markdown only; no executable or shell permission inherited from upstream skills; upstream scripts referenced, never vendored or executed; any script use requires local review and an owner-approved card.
- Pin upstream URL, retrieval date, commit SHA, LICENSE sha256 and material digest at adoption; re-verify before promotion.

## Ponytail adoption contract (added 2026-09-11)

Documentation-only extension of this artifact; no production skill record is changed and no upstream prose is copied or vendored. Upstream authority, pinned by the coordinator task contract because this sandbox has no egress (pins supplied, not fetched; re-verify before promotion): DietrichGebert/ponytail main commit 356918eba965ee1eac64bd3a7f0dd02108350de5; root LICENSE identifier MIT; LICENSE sha256 fb1bc6909ac3ef82d5c22106e32ef682b0cff66788fa915fb9b53b15c9d2f3ab. Upstream benchmark results are not AIWorkHub measurements; every value below stays unset until the A/B arms measure it.

### Concept ranking (artifact rubric; reuse before build)

Fit basis: the current skill registry (SkillSelection, SkillSelectionReceipt, SkillScope), the versioned manager recipe catalogue, semantic-edit runtime evidence (aiworkhub.semantic_edit_runtime_evidence.v1: range_count, old_region_bytes, replacement_bytes, model_reemitted_old_bytes) and the RM-2026-00050/RM-2026-00051 mechanical-automation roadmap goals as cited by the task contract. Fit values are rubric totals under the weights earlier in this artifact; no SELECT has any criterion below 6; every fit value carries sample count n=0 until the A/B arms run.

| Adapted concept (original AIWorkHub wording) | Fit | Existing equivalent to reuse | Verdict |
|---|---|---|---|
| Minimal-solution ladder: YAGNI first, then existing code, stdlib, native platform, installed dependency, minimum new code | 88 | C1 orientation receipts and the versioned recipe catalogue already force find-before-write; the ladder adds the explicit dependency rungs | REUSE, then extend |
| Root-cause repair at the shared callsite instead of per-symptom patches | 87 | C2 evidence-first diagnosis (failure class, sha256-addressed tail, corrected rerun receipt) | REUSE unchanged |
| Protected guards: safety, accessibility and trust boundaries are never simplified away | 90 | reviewer contract, readonly lifecycle gates, deferred security-review candidate | REUSE as hard gate |
| One smallest runnable check per fix | 85 | C3 implementation-with-tests and declared-validator reruns | REUSE unchanged |
| Over-engineering delete-list review | 83 | none; semantic-edit telemetry (model_reemitted_old_bytes) is the natural seed | MISSING, mechanical |
| Explicit simplification debt with ceiling and upgrade trigger | 80 | none in the registry or recipes | MISSING, mechanical |
| Intensity and scoping matched to task size | 74 | SkillScope and task routing already approximate this | DEFER, partial overlap |
| Isolated cross-model A/B measurement | 89 | the Controlled A/B protocol earlier in this artifact | REUSE and extend |

### Minimal implementation wave

W1, system-owned and mechanical, no model judgment: add the delete-list checklist and the simplification-debt ledger (ceiling plus upgrade trigger) as deterministic card fields; the correctness check is git diff --check plus the declared validator, and a field either parses or the card fails. W2, model-owned under contract: the ladder and shared-callsite repair ship as C1-C4-style skill-contract text gated by the quality reviewer. Protected guards are unchanged and non-negotiable: correctness (validator receipts), security (Source Graph-only discovery, staged semantic edits, no inherited permissions), accessibility and trust-boundary checks may not be deleted by any simplification pass.

### A/B measurement contract (extends the Controlled A/B protocol)

Arms: with-Ponytail-contract versus without, on identical hidden cards and repository snapshots, per model (GLM, DeepSeek, Claude and Codex), randomized task and arm order, blinded analysis. Report per arm where telemetry exists: LOC and diff size; tool calls (Source Graph and semantic-edit receipts); retries; tokens and cost; wall time; validation pass rate; safety failures (guard or trust-boundary violations). Explicitly unmeasured until telemetry exists: cross-provider cost parity and accessibility outcomes; they stay labeled unmeasured, never zero. Every reported rate carries its sample count. ROI gate: a slice ships only with one deterministic quality metric and one token-economy metric; otherwise it remains research.
- Verified MIT or CC0 material adapted with attribution kept; unverified stays reference-only: concepts translated, prose never copied.
- Upstream skill text is untrusted data, never instructions; instruction-like content is quoted, flagged and never executed.
