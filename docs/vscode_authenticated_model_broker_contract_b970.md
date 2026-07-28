# VS Code-authenticated model broker contract (B970)

Status: implementation-ready design; stop at verified Codex review. This document does not claim that a particular model or proposed VS Code API is present in any installed build.

## 1. Decision and invariants

AIWorkHub SHALL replace `claude_cli`, `codex_cli`, `deepseek_copilot_cli`, and `glm_copilot_cli` worker execution with one `vscode_lm` adapter hosted by the AIWorkHub VS Code extension. The primary and only inference path is the VS Code Language Model API in the active extension host: `vscode.lm.selectChatModels(...)`, `vscode.lm.onDidChangeChatModels`, the selected `LanguageModelChat.sendRequest(...)`, its streamed response, and `LanguageModelAccessInformation.canSendRequest` / `onDidChange` where exposed by the selected API version. MCP Sampling is only a documented comparison point; it is not an inference, authentication, or fallback path.

Authentication authority is exclusively the active VS Code/Copilot session in the exact VS Code window. AIWorkHub MUST NOT read, copy, persist, refresh, request, log, or transport provider API keys, Claude OAuth, GLM credential files, DeepSeek BYOK files, Codex CLI login, VS Code authentication tokens, or equivalent secrets. It MUST NOT launch any provider CLI, mutate another extension, reload or kill VS Code, or search credential files on any failure path.

Ownership is window-local: one extension-host broker instance owns only requests initiated in that window, for folders in that window, under that window's authenticated session. No catalog, access decision, circuit state, request route, cancellation token, or response stream is process-global or shared with another window.

## 2. Discovery, consent, and truthful selection

Discovery occurs only after an explicit user Start, Retry, or new-task action. For that episode the extension calls `selectChatModels` with the narrowest supported selectors and builds a fresh immutable catalog snapshot from models actually returned in the current window. Configuration aliases are preferences, never evidence of availability.

`onDidChangeChatModels` and `languageModelAccessInformation.onDidChange` are invalidation signals only. Their handlers MUST invalidate the catalog/access snapshot, cancel or mark affected in-flight requests degraded, and update the dashboard with sanitized state. They MUST NOT call `selectChatModels`, `sendRequest`, or otherwise spend model quota in the background. Fresh discovery and any request occur only downstream of a later explicit user Start, Retry, or new-task action. `canSendRequest !== true` is `access_not_granted`; undefined, false, error, and API absence are never treated as consent.

Aliases (`claude`, `glm-5.2`, `deepseek`, `codex`, `gpt`) resolve by declared model metadata/capabilities against that fresh snapshot. The resolver records the returned model identity and requested capabilities (for example tools, streaming, and context requirements). It MUST NOT infer availability from a provider setting or name alone, and MUST NOT silently substitute another family/model. Zero or ambiguous matches, missing required capability, access denial, or API/version mismatch yields a sanitized degraded reason such as `model_unavailable`, `capability_missing`, `access_not_granted`, or `api_unsupported`. Provider error text, prompts, tokens, paths, and credentials never enter UI reasons.

## 3. Repository/window/request-bound protocol

The Python supervisor starts a task-scoped child broker channel inherited only by the extension-host-owned task process (prefer connected stdio or anonymous inherited pipes; a per-task OS endpoint is acceptable only with owner-only permissions, random unguessable name, and unlink-on-close). Global ports, fixed sockets, shared mutable routing tables, discovery files, and cross-repository endpoints are forbidden.

Every envelope contains: protocol version; window-instance nonce; repository identity; canonical isolated workspace identity; task ID and authenticated Task MCP receipt digest; request/episode/correlation ID; monotonic sequence; message kind; bounded budget; and payload. The opening handshake mutually verifies every binding before content flows. Mismatch is terminal `isolation_violation`. Payloads contain normalized chat/tool data and receipt references/digests, never authentication material. Prompt/output logs are disabled by default and any diagnostics are metadata-only and sanitized.

Messages are `hello`, `catalog`, `request_start`, `stream_chunk`, `tool_call`, `tool_result`, `cancel`, `flow_credit`, `terminal`, and `shutdown`. Sequence checks, bounded frames, request-local queues, and correlation checks reject replay, stale, duplicate, or cross-request messages. The extension grants finite `flow_credit`; Python stops reading/generating upstream work when credit is exhausted. A slow/closed consumer causes bounded cancellation, not unbounded buffering.

## 4. Bounded agent and tool loop

An explicit user action starts one fresh bounded episode. The extension selects one actually available model and invokes `sendRequest` with a VS Code cancellation token. The Python supervisor may satisfy model tool calls only through the injected AIWorkHub worker MCP surface for this task. It preserves and returns Task MCP identity, project-context acknowledgement, Source Graph evidence when required, session identity, AI Memory and KB receipts, allowed-write checks, validation results, callback truth, and the manager review gate.

Before a tool runs, the supervisor validates request/task/repository/workspace binding, tool allowlist, JSON schema, remaining turn/tool/time budgets, and the canonical path against the isolated allowed workspace. Writes outside `allowed_writes`, symlink escapes, foreign repositories, lifecycle/credential operations, and untrusted tool names are rejected. Tool output is size-bounded and sanitized before returning to the model. No model response can declare acceptance, promotion, or manager review complete.

Default hard ceilings (configurable downward by the task card): 12 model turns, 32 tool calls, 10 minutes wall time, 60 seconds per model request, 30 seconds per tool call, 1 MiB total streamed text, and 256 KiB per tool result. Exhaustion produces a truthful terminal substatus; it never triggers another adapter.

## 5. Streaming, cancellation, retry, and terminal truth

The extension converts response stream parts to ordered `stream_chunk` records and honors backpressure credits. User cancel, task cancel, window disposal, repository removal, catalog/access invalidation affecting the request, timeout, protocol loss, or extension-host shutdown cancels the VS Code token and Python work, closes the request queue, and emits at most one terminal record when the channel permits.

Retries are bounded inside the same explicitly initiated episode and only for classified transient failures, with jittered backoff, remaining deadline/budget, idempotent pre-tool phase, and no duplicated write/tool side effects. Access denial, invalid selection, isolation failure, policy failure, and cancellation are not retryable.

Circuit breakers are keyed by exact `(window_instance, returned_model_id)`. After the configured consecutive transient failures the circuit is `open`. There is no timer-based half-open probe, automatic polling/request, event-triggered request, or inventory-generation reset. An open circuit remains open until an explicit user Retry or new-task action starts exactly one fresh bounded recovery episode. That episode may make its budgeted request after fresh discovery/access verification; only its successful model response closes the circuit. Failure leaves it open. Background events neither spend model quota nor reset attempts.

Terminal records are immutable and exactly once, with `status` plus precise `substatus`, including `completed`, `cancelled_by_user`, `deadline_exceeded`, `turn_budget_exhausted`, `tool_budget_exhausted`, `model_unavailable`, `capability_missing`, `access_not_granted`, `circuit_open`, `model_error`, `tool_error`, `protocol_error`, `isolation_violation`, or `extension_shutdown`. A callback/dashboard MUST show this truth and remain responsive; it must never relabel degraded/cancelled work as success.

On extension deactivation the broker stops accepting starts, cancels all owned requests, attempts bounded terminal delivery, closes child channels/endpoints, disposes event subscriptions, and clears in-memory catalogs/circuits. It does not kill VS Code or unrelated processes.

## 6. Migration and compatibility

1. Introduce `vscode_lm` behind the existing adapter boundary, protocol version negotiation, and feature flag; keep legacy adapter names parseable only to emit deprecation diagnostics.
2. Map `claude_cli`, `glm_copilot_cli`, `deepseek_copilot_cli`, and `codex_cli` preferences to truthful aliases handled by `vscode_lm`. Do not execute their old binaries or authentication logic.
3. Canary opt-in tasks, then make `vscode_lm` the default. An unavailable alias degrades explicitly; it never falls back to a legacy adapter, MCP Sampling, another model, BYOK, OAuth, or a CLI.
4. Remove provider-specific automatic adapters after a declared compatibility window. Old persisted task records remain readable and display their historical adapter; retry creates a new `vscode_lm` episode subject to current discovery.

Protocol compatibility is fail-closed: same major version required; optional fields are capability-negotiated; unknown required fields/kinds terminate `protocol_error`. No compatibility shim may weaken bindings, receipts, budgets, or review gates.

## 7. Threat model and isolation matrix

Threats include a second window/repository guessing a route, stale catalog reuse, forged correlation IDs, prompt/tool injection, symlink/path escape, response/tool-result exfiltration, unbounded streaming, replay after cancellation, extension shutdown races, and malicious provider error text. Controls are inherited private channels, complete binding checks, immutable request maps local to the extension host, canonical-path enforcement, MCP/tool allowlists and schemas, bounded queues/budgets, cancellation, sanitized metadata-only diagnostics, and exactly-once terminal state.

| Scenario | Required result |
|---|---|
| Window A/repo A request on A channel | Runs only in A workspace with A catalog/access/circuit |
| Window B/repo B request on B channel | Runs only in B workspace with B catalog/access/circuit |
| A envelope replayed to B | Reject `isolation_violation`; no discovery/request/tool |
| Same repo opened in A and B | Distinct window nonces, catalogs, circuits, channels, requests |
| Model changes in A | Invalidate/cancel/degrade A only; no background discovery/request; B unchanged |
| Access changes in B | Invalidate/cancel/degrade B only; `canSendRequest !== true`; no background discovery/request |
| A circuit opens | Only explicit Retry/new task in A can start one recovery episode; B unaffected |
| A shutdown while B streams | Cancel/close A only; B continues |

## 8. Verification plan

Mocked extension tests SHALL verify returned-model-only alias resolution; no substitution; `canSendRequest !== true`; invalidation handlers make zero `selectChatModels`/`sendRequest` calls; explicit Start/Retry/new-task discovery; streaming order/backpressure; cancellation/timeout; exactly-once terminals; manual-only open-circuit recovery; no timer, polling, inventory, or event reset; two-window isolation; shutdown; sanitized errors; and zero CLI/credential/Sampling fallback.

Python protocol tests SHALL verify handshake bindings, frame/sequence/correlation validation, replay rejection, two-repository isolation, allowed-path and symlink enforcement, receipt preservation, MCP tool allowlisting, turn/tool/time/size budgets, flow control, cancellation races, retry side-effect safety, terminal substatus truth, and manager-review blocking.

Manual live canary: use a disposable repository and synthetic non-secret prompt identifiers; in each of two VS Code windows explicitly Start one task, record only model IDs/capability/status metadata, verify unavailable aliases degrade, revoke/alter access and confirm no background discovery/request, open a circuit with mocked/transient failure and confirm only explicit Retry starts recovery, cancel and close one window, and confirm the other is unaffected. Do not record prompt/response content or inspect tokens/credentials.

## 9. Subsequent implementation file map and API gate

Expected production files: `vscode-extension/src/modelBroker/vscodeLmAdapter.ts`, `catalog.ts`, `brokerProtocol.ts`, `requestController.ts`, `circuitBreaker.ts`, `extensionLifecycle.ts`; the existing extension activation/worker-launch integration file identified by Source Graph; and Python `aiworkhub/vscode_lm_adapter.py`, `aiworkhub/vscode_lm_protocol.py`, plus the existing task supervisor/adapter registry files identified by Source Graph. Expected tests: `vscode-extension/src/test/modelBroker/*.test.ts`, `tests/test_vscode_lm_protocol.py`, `tests/test_vscode_lm_adapter.py`, and `tests/test_vscode_lm_isolation.py`. These are implementation targets, not changes authorized by B970.

Before implementation, pin a VS Code engine/type package version that actually exposes the required stable APIs. `selectChatModels`, `onDidChangeChatModels`, `LanguageModelChat.sendRequest`, response streaming, cancellation, and `LanguageModelAccessInformation` names/semantics MUST be verified against that pinned official `vscode.d.ts`; proposed API use requires the matching Insiders build and proposal declaration and MUST NOT ship as assumed stable. If access-change information is absent, the broker may conservatively treat access as not granted/degraded; it may not fabricate availability or poll. Exact existing integration filenames must be resolved by Source Graph in the implementation task rather than invented here.
