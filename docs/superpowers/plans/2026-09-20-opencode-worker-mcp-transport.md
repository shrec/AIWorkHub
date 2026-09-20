# OpenCode worker MCP transport plan

**Goal:** Make the exact OpenCode worker process receive only its request-local AIWorkHub worker MCP tools, then re-run the paid Muse Contributor qualification canary. This closes NF-2026-00919; NF-2026-00912 only shortened the tool alias.

**Spec:** `docs/superpowers/specs/2026-09-20-model-neutral-sdlc-design.md`

**Observed baseline:** Request `131fcb1951364d288cb5d87318ff8a22` reached `review_ready` on `opencode-go/muse-spark-1.3-contributor`, but its authenticated worker ledger contained zero live Source Graph calls and the worker reported AIWorkHub tools unavailable. `build_opencode_worker_mcp_config` has test callers but no observed production caller; `inject_worker_mcp_config` skips `opencode_cli`. The later alias-only candidate was accepted as `ed846a1`, with 47 focused tests; it does not establish live tool reachability. OpenCode 1.18.29 is installed. The [OpenCode configuration contract](https://dev.opencode.ai/docs/config/) documents `OPENCODE_CONFIG_CONTENT` as an inline runtime override, and the [MCP contract](https://dev.opencode.ai/docs/mcp-servers/) documents local stdio commands and prefixed tool names.

## Global constraints

- Do not edit a user's global OpenCode configuration or persist a repository-specific manager identity in it. The worker server is private to one exact request/workspace.
- Reuse the existing generated worker MCP runtime, authority repo binding, audit ledger, and `build_opencode_worker_mcp_config`; do not create a second MCP implementation.
- The inline or file config must use only the `awh` alias; the permission contract remains `* : deny` with exact worker tool allow entries. No manager MCP tool, built-in shell, or unverified repository becomes visible.
- Fail closed before provider launch if the request-local config or server command cannot be generated, resolved, or read inside the selected sandbox. Report a typed infrastructure cause, not a model-quality failure.
- Preserve the existing adapter model pin, reasoning variant and auth projection; do not weaken Landlock/AppContainer. One clean unit-test run is not live qualification.

## Task 1 — request-local launch wiring

**Files:** `src/aiworkhub/process_launcher.py`, `src/aiworkhub/runtime_adapters.py`, `src/aiworkhub/worker_workspace.py`, `src/aiworkhub/worker_ai_tools_mcp.py`; tests in `tests/test_opencode_runtime_adapter.py`, `tests/test_opencode_workforce_integration.py`, and the exact process-launcher tests covering MCP provisioning. Avoid edits to a file if its current helper already suffices.

1. Add a failing integration test that exercises the real OpenCode launch preparation path, not just the config builder. Assert that the exact request receives a JSON runtime config containing `mcp.awh` and its command, that every allowed generated name is ≤64 characters, and that unknown/manager/built-in tools stay denied. Assert the isolated HOME and selected sandbox can see the command and audit-key path. A missing config must refuse launch before token spend.
2. Run the named test and record the actual red failure. If it fails only because of a missing test fixture or toolchain, repair the fixture first; that is not a behavioral red.
3. Wire the existing generated runtime into OpenCode's request-local config path. `OPENCODE_CONFIG_CONTENT` is the first candidate because it is a documented high-precedence per-process override; an equivalent private `OPENCODE_CONFIG` file is acceptable if its provenance and sandbox visibility are stronger. Keep the config secret-free and bound to the request. Never allow inherited config to grant a tool outside the exact worker allowlist.
4. Cover Python unit, cross-repository identity, missing/symlink/oversized config, model pin, and permission regressions. Run `python3 -m pytest -q tests/test_opencode_runtime_adapter.py tests/test_opencode_workforce_integration.py` plus the process-launcher MCP provisioning tests touched by the implementation, Ruff on changed Python paths, and `git diff --check`.
5. Stop at manager review. The reviewer examines only the changed config/permission path and the affected launch contract, then the manager accepts or returns with a concrete finding.

## Task 2 — live exact-route proof and tiered Muse evaluation

1. After the replacement runtime is active, launch a read-only canary on **exactly** `opencode-go/muse-spark-1.3-contributor`, not `opencode/muse-spark-1.3-contributor-free`. Require live authenticated `source_graph` and `session_current_state` calls, plus the worker's exact model/adapter receipt. For a read-only task, semantic edit is not required; a later writable canary must exercise prepare/apply.
2. If a live tool is absent, block the canary as `provider_runtime`, retain exact request/ledger evidence, and refine Task 1. Do not score it as a Muse quality rejection.
3. Once tool reachability is proven, run low-, medium-, and high-risk corpus tasks with fixed acceptance and the same mechanical gates used for other routes. Record first-pass acceptance, severe review findings, rework/validation, latency, token/cost coverage and exact task-family denominators. Unknown cost remains UNKNOWN. Mark only the proven tier eligible for routing.

## Review focus

- A test-only config builder with no production caller is a failure, even if all helper tests pass.
- An injected orientation bundle is not a live worker tool call.
- `awh` length compliance does not establish config visibility; both are required.
- No global config mutation, other-repo identity, manager tool or raw shell exposure.
- No broad Muse quality conclusion from one canary.
