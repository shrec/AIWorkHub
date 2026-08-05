# Strix capability review

AIWorkHub reviewed [usestrix/strix](https://github.com/usestrix/strix) at
upstream commit `657aa5cbe687485135d1049450e36f296edb106d` on 2026-08-05. This is a
capability review, not a code import. AIWorkHub keeps its repository-native,
general software-engineering authority model and does not adopt Strix's
offensive-security runtime or product semantics.

## Adopted ideas

| Idea | AIWorkHub implementation | Truth boundary |
| --- | --- | --- |
| Recover from hallucinated tool names | Both dependency-free MCP dispatchers return bounded, deterministic `tools/list` recovery guidance and at most three close registered names | A suggestion never aliases or executes a tool; the model must retry with an exact registered name |
| Deduplicate recurring findings by root cause | Known Bug Scanner emits a stable `root_cause_fingerprint` over rule, repository-relative path and normalized source, alongside the existing location-sensitive fingerprint | Candidates remain `static_candidate`; deduplication never upgrades them to runtime-reproduced findings |

## Already covered by canonical AIWorkHub authorities

| Strix capability pattern | Existing AIWorkHub authority |
| --- | --- |
| Agent graph and parent/child coordination | Task DAG, dependency readiness, write-collision control, process ledger and callback inbox |
| Durable per-agent state and resume | Session Manager, Manager Context Graph, AI Memory, KB, task history and retained attempt receipts |
| Notes and todo state | Session events/checkpoints plus canonical task cards and plan DAG; no parallel JSON authority is needed |
| Local run viewer and live status | Repository-scoped dashboard, Logs, Sessions, Memory, KB, Operations and KPI views |
| Usage/cost tracking | Attempt-bound provider-usage capture, worker/reviewer role attribution, cache/reasoning fields and cost ledger |
| SARIF reporting | Diff-scoped Known Bug Scanner native/SARIF output with explicit static-versus-runtime evidence fields |
| Sandboxed execution | Isolated worker workspaces and platform-specific sandbox/preflight contracts |
| Bounded context and skills | Source Graph progressive disclosure, compact project context and generated provider/worker tool policy |

## Deliberately not adopted

- Automatic hard token or dollar stops remain owner opt-in. AIWorkHub optimizes
  the process and measures waste; it does not guess a task's required budget.
- Provider retries remain explicit and evidence-bearing when semantic work may
  be repeated. Hidden retries would blur attempt economics and can multiply
  cost.
- LLM-based finding deduplication is not a default authority. Deterministic
  fingerprints are cheaper and reproducible; an LLM may later adjudicate only
  unresolved groups without deciding PASS/FAIL.
- Browser exploitation, interception proxies and offensive payload runtimes are
  Strix domain capabilities, not general AI coding orchestration primitives.
- Conversation compaction is benchmark-first. AIWorkHub workers are task-scoped
  and current evidence does not show compaction as the dominant cost lever.

## Evidence still required before further adoption

Two ideas remain candidates rather than shipped claims:

1. Crash-stream salvage into a retry packet, after a paired failure/retry test
   proves it reduces repeated reads without leaking stale worktree state.
2. Runtime-validated scanner findings, after a coordinator-owned reproduction
   receipt can bind the exact finding fingerprint, revision, command, exit
   semantics and artifacts without accepting model self-attestation.

This review follows AIWorkHub's donor rule: preserve useful invariants, avoid a
parallel database or lifecycle, and measure the result before advertising a
benefit.
