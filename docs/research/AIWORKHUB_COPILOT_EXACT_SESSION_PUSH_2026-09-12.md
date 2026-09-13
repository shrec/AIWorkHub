# AIWorkHub research — Copilot "exact-session" turn push feasibility

Date: 2026-09-12 · Task: `DEEPSEEK_AIWORKHUB_COPILOT_SESSION_PUSH_RESEARCH_V4` · Read-only research; no production code created or modified.

Two questions are answered with concrete, verifiable evidence (VS Code's shipped
`vscode.d.ts` and proposed-API `.d.ts` files on the `microsoft/vscode` `main`
branch, the Model Context Protocol specification, and the repo's own transport
code):

1. Can a third-party VS Code extension identify **which specific chat/conversation**
   triggered an incoming MCP `tools/call` that reaches AIWorkHub's Python MCP server?
2. Can a third-party VS Code extension programmatically start a **new agent turn in
   that exact existing Copilot chat session** (not "the last focused chat")?

Short answer: **No for both**, through generic MCP tool-calling. See the
Recommendation for the precise verdict.

## Findings

### What AIWorkHub already has (the contrast to reach parity with)

AIWorkHub's Codex/Claude delivery already does exactly what is being asked for,
because those products expose a thread identity and turn primitives in their
wire protocol:

- `src/aiworkhub/callback_bridge.py` speaks the Codex App Server protocol:
  `initialize` → `initialized` → `thread/resume` for the bound origin thread, then
  `turn/start` (idle) or `turn/steer` (already-active) against that exact thread.
  See the lifecycle doc at `src/aiworkhub/callback_bridge.py:1112`, the resume call
  at `:1324`, and the idle `turn/start` path at `:1452` / `:1057`.
- `vscode-extension/extension.js` is an MCP *client*: it sends
  `tools/call` with `{ name, arguments }` and nothing else
  (`vscode-extension/extension.js:2284`, `:2570`). That is the request shape the
  Python MCP server receives, and it contains no conversation identity.

### What VS Code's **stable** API exposes

- `namespace chat` (stable) contains **only** `createChatParticipant(id, handler)`.
  There is no `chat.request`, no `sendRequestToParticipant`, and no session-enumeration
  or turn-injection entry point.
- `namespace lm` (stable) contains `registerTool`, `invokeTool`, `tools`,
  `registerMcpServerDefinitionProvider`, `registerLanguageModelChatProvider`, and
  related model/tool registry functions — none of which carry a per-request
  conversation/session identifier.
- The stable tool-invocation payload `LanguageModelToolInvocationOptions<T>` is only
  `{ toolInvocationToken, input, tokenizationOptions? }`. `toolInvocationToken` is an
  opaque `ChatParticipantToolToken` (`never`), obtainable only while handling a
  `ChatRequest`; it cannot be fabricated and it does not name a conversation.

### What VS Code's **proposed** API exposes

- `chatParticipantAdditions` (proposal) adds `LanguageModelToolInvocationStreamOptions<T>`
  with `readonly chatRequestId?`, `readonly chatSessionResource?: Uri`, and
  `readonly chatInteractionId?`, delivered only to `LanguageModelTool.handleToolStream`
  of an **extension-registered `vscode.lm` tool**. These are read-only and never reach
  a generic (Python) MCP server's `tools/call`.
- `mcpServerDefinitions` (proposal) adds `lm.startMcpGateway(chatSessionResource?)`, an
  extension-created MCP gateway whose tool calls can be *associated with* a chat session
  for "inline elicitation UI". This is one-directional (extension→editor), requires the
  extension to already hold the session `Uri`, and neither forwards origin identity into
  the server's `tools/call` nor starts a turn.
- `chatSessionsProvider` (proposal) lets an extension provide its **own** chat sessions
  and handle requests for them (`ChatSession.requestHandler`), but that handler is
  invoked *by the editor on user input*, not callable by the extension; there is no
  primitive to inject a turn into an existing session.

## Verified claims

Every claim states its stability tier and a concrete, checkable source.

1. **Stable (MCP protocol).** The MCP `tools/call` request parameters are only
   `{ name, arguments }`; the protocol defines no conversation/session/request-identity
   field for tool calls.
   Source: https://modelcontextprotocol.io/specification/2025-06-18/server/tools
   ("Calling Tools" section).

2. **Stable (VS Code API).** `export namespace chat` exposes only
   `createChatParticipant(id: string, handler: ChatRequestHandler): ChatParticipant`.
   Source: https://raw.githubusercontent.com/microsoft/vscode/main/src/vscode-dts/vscode.d.ts
   (`namespace chat`).

3. **Stable (VS Code API).** `LanguageModelToolInvocationOptions<T>` is
   `{ toolInvocationToken: ChatParticipantToolToken | undefined; input: T;
   tokenizationOptions?: LanguageModelToolTokenizationOptions }` — no conversation,
   session, or request identifier.
   Source: same `vscode.d.ts` (`interface LanguageModelToolInvocationOptions`).

4. **Stable (VS Code API).** `ChatRequest` carries `prompt`, `command`, `references`,
   `toolReferences`, `toolInvocationToken`, and `model`, but no conversation/session ID.
   Source: same `vscode.d.ts` (`interface ChatRequest`).

5. **Stable (VS Code API).** `toolInvocationToken` is `ChatParticipantToolToken` (typed
   `never`), i.e. an unforgeable token obtained only while serving a `ChatRequest`.
   Source: same `vscode.d.ts` (`ChatParticipantToolToken`).

6. **Proposed — `chatParticipantAdditions` (flag: `enabledApiProposals: ["chatParticipantAdditions"]`).**
   `LanguageModelToolInvocationStreamOptions<T>` exposes `readonly chatRequestId?: string`,
   `readonly chatSessionResource?: Uri`, and `readonly chatInteractionId?: string`, but
   only via `LanguageModelTool.handleToolStream` for an extension-registered `vscode.lm`
   tool — not on `tools/call` of an external MCP server.
   Source: https://raw.githubusercontent.com/microsoft/vscode/main/src/vscode-dts/vscode.proposed.chatParticipantAdditions.d.ts
   (`interface LanguageModelToolInvocationStreamOptions`, `interface LanguageModelTool`).

7. **Proposed — `mcpServerDefinitions` (flag: `enabledApiProposals: ["mcpServerDefinitions"]`).**
   `lm.startMcpGateway(chatSessionResource?: Uri)` associates MCP tool calls routed
   through an extension-created gateway with a chat session for inline elicitation UI.
   It requires the session `Uri` up front, is one-directional, and does not (a) place
   origin identity into the MCP server's `tools/call` nor (b) start a turn.
   Source: https://raw.githubusercontent.com/microsoft/vscode/main/src/vscode-dts/vscode.proposed.mcpServerDefinitions.d.ts
   (`namespace lm { export function startMcpGateway }`, tracked in microsoft/vscode#288777).

8. **Proposed — `mcpToolDefinitions` (flag: `enabledApiProposals: ["mcpToolDefinitions"]`).**
   `McpServerMetadata` / `McpStdioServerDefinition2` / `McpHttpServerDefinition2` only
   pre-declare static tool metadata for deferred server start; they add no per-request
   identity.
   Source: https://raw.githubusercontent.com/microsoft/vscode/main/src/vscode-dts/vscode.proposed.mcpToolDefinitions.d.ts
   (tracked in microsoft/vscode#272000).

9. **Proposed — `chatSessionsProvider` (flag: `enabledApiProposals: ["chatSessionsProvider"]`).**
   `ChatSessionItemController` / `ChatSession` let an extension own custom chat sessions
   and handle requests for them, but `requestHandler` is editor-invoked and there is no
   API to start a turn in an existing session; sessions are addressed by a `Uri` the
   extension must already possess.
   Source: https://raw.githubusercontent.com/microsoft/vscode/main/src/vscode-dts/vscode.proposed.chatSessionsProvider.d.ts
   (`interface ChatSession`, `interface ChatSessionItemController`).

10. **Proposed — `interactive` (flag: `enabledApiProposals: ["interactive"]`).**
    `interactive.transferActiveChat(toWorkspace: Uri)` only transfers the *active* chat to
    a workspace; it does not target or re-initiate a turn in an existing conversation.
    Source: https://raw.githubusercontent.com/microsoft/vscode/main/src/vscode-dts/vscode.proposed.interactive.d.ts

11. **Internal context (not an API tier).** AIWorkHub's Codex/Claude transport targets an
    exact thread with `thread/resume` + `turn/start`/`turn/steer`
    (`src/aiworkhub/callback_bridge.py:10`, `:1112`, `:1452`), and its own extension MCP
    client sends `tools/call { name, arguments }`
    (`vscode-extension/extension.js:2284`, `:2570`) — confirming both the parity goal and
    the absence of conversation identity in the MCP request.

## Unverified or rejected claims

- **Unverified:** "VS Code's Copilot forwards conversation identity to third-party MCP
  servers (e.g. via the MCP `_meta` extension field)." The generic MCP spec has no such
  field, and no public VS Code documentation or source confirms Copilot injects one.
  Excluded from the recommendation.
- **Rejected:** "A stable `vscode.chat.request` / `vscode.chat.sendRequestToParticipant`
  can target an arbitrary existing session." The stable `namespace chat` contains only
  `createChatParticipant` (verified claim 2).
- **Rejected:** "An extension can programmatically start a turn in the last-focused
  Copilot chat." Even the weaker "last focused chat" form has no stable (or proposed)
  entry point; and it would not satisfy the exact-session requirement anyway.
- **Rejected:** "`chatSessionResource` can be used to inject a turn." It is read-only on
  the proposed tool-stream options (claim 6) and a one-directional association target on
  the proposed gateway (claim 7); no primitive consumes it to initiate a turn.
- **Not relied upon (undocumented/internal):** VS Code private proposed surfaces such as
  `vscode.proposed.chatParticipantPrivate.d.ts`. No claim in this report depends on them.

## Recommendation

Verdict: **(c) not currently buildable through generic MCP tool-calling — a chat
participant or another approach would be required.**

Justification. A generic MCP `tools/call` arriving at AIWorkHub's Python server carries
only `{ name, arguments }` (verified claim 1; independently confirmed by the repo's own
client at `vscode-extension/extension.js:2284`), so the server cannot learn which
conversation triggered it. The only extension-visible per-request session identity is the
**proposed** `LanguageModelToolInvocationStreamOptions.chatSessionResource` /
`chatRequestId` (claim 6), and it exists only inside an extension-registered `vscode.lm`
tool's streaming handler — never server-side. Separately, no stable or proposed API lets an
extension start a new turn in an existing chat session: the stable `chat` namespace has only
`createChatParticipant`, and the proposed `chatSessionsProvider`/`mcpServerDefinitions`
surfaces expose a session `Uri` (read) or accept one (gateway association) but offer no
turn-injection primitive. The Codex `thread/resume` + `turn/steer` capability therefore has
**no Copilot-chat equivalent**, so the exact-session continuation AIWorkHub does for
Codex/Claude is not achievable today through generic MCP tool-calling; it would require a
chat participant (or another approach) — explicitly out of scope for this task and not
scoped here.

No claim in this report is treated as fact until independently spot-checked by the manager
before any implementation card is built on top of it.
