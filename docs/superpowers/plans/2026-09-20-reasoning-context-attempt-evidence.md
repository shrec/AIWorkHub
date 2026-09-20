# Reasoning and Context Attempt Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record which reasoning option and context capacity an editor-hosted worker actually sent, then compare outcomes only across complete, matched task cohorts.

**Architecture:** The existing bridge request carries the policy decision and discovered model context. The VS Code host owns the `sendRequest` boundary, so it records a bounded, request-local receipt there and publishes it in the owner-only terminal response; the isolated worker validates and forwards that receipt. A later task persists the receipt under the exact process-attempt identity and joins it to complete accepted-task cohorts. A sent option is not proof of a provider's internal reasoning level.

**Tech Stack:** VS Code extension JavaScript, Python 3.12+, repository task-event ledger, pytest, Node test harness.

**Spec:** `docs/superpowers/specs/2026-09-20-model-neutral-sdlc-design.md`

## Global Constraints

- Keep `repo_id`, `request_id`, selected model, and attempt identity exact; a foreign, stale, or malformed receipt is `UNKNOWN` or refused, never applied.
- Never infer provider-internal thinking from an option passed to `sendRequest` or from a model name.
- No quota, price, or token-spend assertion is derived from `model.maxInputTokens`; it is context capacity only.
- Do not change model-selection, effort-normalization, sandbox, callback, or task-finalization policy while adding evidence.
- Task 1 must not edit `process_launcher.py` or `sdlc_outcome_metrics.py`, so it can run beside the already-pending cohort task.

## Review Focus

1. A provider error before the first `sendRequest` records `not_sent`, not `applied`.
2. A request with no declared effort control records `unsupported` or `provider_default` and no invented option key.
3. Text and native-tool protocols produce the same receipt semantics, including multi-turn requests.
4. A malformed or mismatched host receipt cannot be laundered into a successful worker result.
5. A context-capacity number is reported with its source and never relabeled as a consumed-token budget.

### Task 1: Capture the actual VS Code LM send boundary

**Files:**
- Modify: `vscode-extension/extension.js` at `vscodeLmLanguageModelRequestOptions`, both text/native `sendRequest` call paths, and `VscodeLmBridgeHost` terminal response publication.
- Modify: `src/aiworkhub/vscode_lm_bridge.py` at `create_request` to pin the requested model in the private worker spec.
- Modify: `src/aiworkhub/vscode_lm_worker.py` at `run` terminal response validation/result construction.
- Test: `vscode-extension/test/glm-vscode-lm-bridge.test.js` in `nf897EffortContextChecks` and the host-response fixture.
- Test: `tests/test_vscode_lm_bridge.py` at the request/spec publication boundary.
- Test: `tests/test_vscode_lm_worker.py` near `TestRunSpecPathIntegration`.

**Interfaces:**
- Consumes: bridge `reasoning_decision`, `model_context`, `request_id`, `repo_id`, and the selected host model's capability metadata.
- Produces: a bounded `reasoning_context_attempt` object in the owner-only host response and validated isolated-worker result. Its schema is `aiworkhub.reasoning_context_attempt.v1`; required keys are exact `request_id`, `repo_id`, bridge-requested model, host-selected model identity, requested profile, send state (`sent` or `not_sent`), normalized option status, actual option key/value or null, context capacity/source, send-turn count, and `provider_internal_state: "unknown"`.

- [ ] **Step 1: Write failing host tests.** Extend the existing `nf897EffortContextChecks` fixture with a fake model whose `sendRequest` records its options. Assert the terminal response's receipt says `send_state: "sent"`, the exact `reasoningEffort: "high"` option, `send_turn_count >= 1`, and model context from `maxInputTokens`. Repeat with a fake `sendRequest` that throws before returning: it may have an attempted call, but must not claim provider acknowledgement. A model with no effort capability must have null option key/value. Run `node vscode-extension/test/glm-vscode-lm-bridge.test.js` and observe these new assertions fail before implementation.

- [ ] **Step 2: Implement request-local host capture.** Capture the canonical effort option immediately at each actual `model.sendRequest` invocation in both protocol paths, not when `vscodeLmLanguageModelRequestOptions` merely computes options. Keep a bounded count and one stable option identity; if turns disagree, emit `unknown` with a typed mismatch reason. Attach the host-generated receipt to the existing atomic terminal `responsePayload` at the same commit point as `text`, `error`, and `decision`. Use only literal scalars from validated request/model/option metadata; do not copy arbitrary provider text into the receipt. Run the Step 1 Node test green.

- [ ] **Step 3: Write failing bridge/worker tests.** In `tests/test_vscode_lm_bridge.py`, assert `create_request` pins the requested model in the private worker spec. In `tests/test_vscode_lm_worker.py`, use the existing `_make_spec_and_response` fixture to assert a well-formed host receipt reaches `run()`'s result. Add malformed schema, wrong request ID, wrong repo ID, wrong requested model, non-finite/negative capacity, unbounded string, and `sent` without an actual option/status consistency cases. Each must refuse or return typed unknown without presenting a false applied value. Run `python3 -m pytest -q tests/test_vscode_lm_bridge.py tests/test_vscode_lm_worker.py` and observe the new tests fail before implementation.

- [ ] **Step 4: Validate and forward the host receipt.** Add the bridge-requested model to `create_request`'s private worker spec. Keep a small validator in `vscode_lm_worker.py` that accepts only the fixed schema/vocabulary and exact spec identities, including that model. Do not parse a model-authored final envelope for this field. Forward the bounded value as `reasoning_context_attempt` in the successful worker result. A terminal error still raises as today; its owner-only host response retains the diagnostic receipt without changing cancellation arbitration. Run the Python tests green.

- [ ] **Step 5: Verify the boundary.** Run `node vscode-extension/test/glm-vscode-lm-bridge.test.js`, `python3 -m pytest -q tests/test_vscode_lm_worker.py tests/test_vscode_lm_bridge.py tests/test_reasoning_runtime_wiring.py`, `python3 -m ruff check src/aiworkhub/vscode_lm_bridge.py src/aiworkhub/vscode_lm_worker.py tests/test_vscode_lm_bridge.py tests/test_vscode_lm_worker.py`, and `git diff --check`. Stop at manager review; accept only after inspecting the actual send sites and both protocol paths.

### Task 2: Persist the exact attempt receipt

**Depends on:** Task 1 accepted. This task does not depend on the cohort query implementation and should not edit its files.

**Files:**
- Modify: `src/aiworkhub/process_launcher.py` at the parsed VS Code LM worker result and terminal process-event emission.
- Test: `tests/test_process_launcher.py` and `tests/test_process_launcher_security.py`.

**Interfaces:**
- Consumes: Task 1's validated `reasoning_context_attempt` from the isolated-worker result.
- Produces: one durable process-attempt event keyed by canonical task, request, repo, adapter, selected model, and attempt identity; `sent`, `unsupported`, `not_sent`, and `unknown` remain distinct.

- [ ] **Step 1: Add failing tests.** Use the existing VS Code LM worker-result fixture to assert that a verified `reasoning_context_attempt` survives terminalization into the canonical process-event record with the same request ID and model. Assert absent, foreign, or malformed receipts become `UNKNOWN` and do not affect task disposition. Run `python3 -m pytest -q tests/test_process_launcher.py tests/test_process_launcher_security.py` red on the new assertions.
- [ ] **Step 2: Persist only verified scalars.** Read the field from the already-validated worker result, bind it to the launcher-owned attempt identity, and emit it through the existing durable event path. Do not infer it from prompt text, request policy, cost ledger, or model name. Preserve existing result/error and callback behavior. Run the Step 1 tests green.
- [ ] **Step 3: Verify.** Run `python3 -m pytest -q tests/test_process_launcher.py tests/test_process_launcher_security.py tests/test_reasoning_runtime_wiring.py`, `python3 -m ruff check src/aiworkhub/process_launcher.py tests/test_process_launcher.py tests/test_process_launcher_security.py`, and `git diff --check`. Stop at manager review.

### Task 3: Matched outcome comparison

**Depends on:** Task 2 and the accepted complete-cohort fix for `NF-2026-00925`.

**Files:**
- Modify: `src/aiworkhub/sdlc_outcome_metrics.py` to join complete task histories to exact attempt receipts.
- Test: `tests/test_sdlc_outcome_metrics.py` with same-route matched and unmatched cohorts.

**Interfaces:**
- Consumes: complete decided-task cohort rows, exact accepted-outcome identity, and Task 2's durable attempt receipts.
- Produces: bounded per-route, task-family, risk-tier, and effort-setting denominators plus first-pass acceptance, severe findings, validation/rework, elapsed time, observed token/cost fields, and explicit unknown/excluded counts. No causal claim follows from observational cohorts.

- [ ] **Step 1: Add failing fixtures.** Build two task families and two route identities; only same-route/family/risk records with exact attempt receipts enter one comparison. Include one missing receipt, one incomplete history, one 402 provider failure, one unknown price, and one stale/foreign identity. Assert each excluded reason and denominator. Run `python3 -m pytest -q tests/test_sdlc_outcome_metrics.py` red on the new cases.
- [ ] **Step 2: Implement the bounded join.** Reuse the complete-cohort query and identity predicates accepted under NF-925. Join on exact task/request/attempt identity, retain non-provider failures as separate classes, and report coverage plus sample sizes. Do not turn an observed association into a statement that effort caused better quality. Run the Step 1 tests green.
- [ ] **Step 3: Verify and measure live data.** Run `python3 -m pytest -q tests/test_sdlc_outcome_metrics.py tests/test_reasoning_runtime_wiring.py`, `python3 -m ruff check src/aiworkhub/sdlc_outcome_metrics.py tests/test_sdlc_outcome_metrics.py`, and `git diff --check`. Query `aiworkhub_manager_sdlc_outcome_metrics` on the installed build; publish only observed matched cohorts, coverage, truncation, and `UNKNOWN` where evidence is insufficient. Stop at manager review.

## Completion boundary

The policy's functional tests are not outcome evidence. This plan is complete only when the three tasks are accepted, the installed runtime emits a real exact-attempt receipt, the cohort query includes complete histories, and a live comparison reports its sample size and uncertainty. Muse qualification and the rest of the SDLC, Semantic Review, and LSP deliverables remain separate parts of the approved spec.
