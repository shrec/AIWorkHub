# Manager terminal console — design

Date: 2026-09-26 · Program: Coding Factory, sub-project P3 · Roadmap: RM-2026-00067, RM-2026-00020, RM-2026-00047
Builds on: `2026-09-23-manager-chat-three-route-design.md` (the first vertical slice: session, route, turn event). This is the "later spec" that slice deferred.

## 1. Outcome

The AIWorkHub manager chat panel becomes the operator's terminal for a multi-model manager seat:

- any declared model (claude, codex, opencode, …) sits in the manager seat under one AIWorkHub `mls-…` session, with callbacks reaching it the same way for every provider;
- Claude Code's native `/goal` and Codex's native thread goals are driven from the panel; every other model gets an AIWorkHub GoalLoop;
- each model's stream is visible as it happens: thinking, tool calls, shell commands with output, file diffs, task cards, callbacks, token usage;
- the panel controls chat and session only (send, stop, steer, goal, model, rotate, open in terminal). Tasks are created, launched and reviewed by the manager model through its MCP tools, never by a human slash command;
- prompts stay lean: history lives in the provider's resumed conversation plus Context Graph / AI Memory / Session Manager, never replayed into the prompt.

Success criteria (measured, not asserted):

1. A claude, a codex and an opencode manager each complete a turn that launches a card and receive its review callback in the panel without owner action.
2. Streaming text appears within 1 s of the provider emitting it; no delta is ever written to the session JSONL.
3. Every final user/assistant message of every backend lands in Context Graph (today only the Codex mux path writes).
4. Rotation triggers from provider-reported tokens at 75 % of the model's context window.
5. A `/goal` on each of the three backends ends in `met`, `impossible`, `budget` or owner stop — never silently.

Non-goals: xterm.js / node-pty emulation; human task commands; a new design system; replacing the dashboard's other views.

## 2. Architecture (approved Section 1)

```
provider process ──► backend translator ──► v3 events ──► ManagerOrchestrator
 claude -p stream-json      (per backend)                      │
 codex app-server JSON-RPC                                     ├─ final events → SessionStore JSONL (+ Context Graph for user/assistant text)
 opencode run --format json                                    └─ deltas → in-memory `partial` (display only)
                                                                           │
                                          MCP aiworkhub_manager_loop_events(after_seq) → extension.js → webview renderers
```

- The session (`mls-…`) owns identity, callbacks, handoff and the brief. A backend is transport only; switching model is a handoff plus brief on the same session.
- Backends: `claude_cli` (stream-json, native `/goal`), `codex_app_server` (new, one persistent `codex app-server` per session, native goals + steer + interrupt), `opencode_cli` and any other CLI (AIWorkHub GoalLoop). `codex_cli` (exec --json) stays as the fallback route.
- Callbacks reach every backend through the existing `WakeConsumer` (`manager_loop_wake.py`).
- Approach A: one unified event schema (v3) and native provider protocols underneath it.

## 3. UI design direction

**Purpose:** drive and observe a manager model that runs the whole repository. **Audience:** the owner, many hours a day, scanning for "what is it doing, what did it change, what does it cost". **Tone:** technical, dense, quiet, VS Code-native — a terminal that happens to render structure. **Memorable detail:** a 2 px *context hairline* under the header that fills with the manager's real context use and shifts hue at 60 % (`--stale`) and 75 % (`--blocked`, the rotation point); the one element that makes the token economy visible at all times.

Rules:

- Reuse `app.css` tokens (`--canvas`, `--surface-subtle`, `--ink`, `--ink-soft`, `--line`, `--accent`, the status hues and their `-ink` text variants, the `--fs-*` type scale). No new palette, no gradients, no card-inside-card.
- **Color means state, never brand.** Provider identity is a monochrome text chip (`claude · opus-5.5`). Status hues are reserved for task/goal/command state.
- Prose in `--vscode-font-family`; commands, output, diffs and ids in `--vscode-editor-font-family`.
- A terminal feel comes from a **glyph gutter**, not emulation: `›` owner, `●` manager text, `∴` thinking, `$` command, `±` file change, `⚙` tool, `▣` task, `↯` callback, `◎` goal, `!` error. The gutter is a fixed-width column so blocks never shift.

Layout, single column:

1. **Header:** model chip · backend · session title · goal pill (when a goal exists) · context percent; the context hairline sits under it.
2. **Transcript:** blocks in event order. Auto-scroll only while the owner is at the bottom; otherwise a "↓ latest" pill appears.
3. **Sticky goal strip** (only while a goal exists): condition (one line, expandable) · status pill · turns used/budget · tokens used/budget · last evaluator verdict and reason · Stop.
4. **Composer:** auto-growing textarea; `/` opens the slash menu; Send becomes Stop while a turn runs; a text sent during a running turn is a *steer* on `codex_app_server` and a queued message elsewhere (labelled as such); an icon button opens the session in a VS Code terminal.

Blocks:

| Event | Rendering |
|---|---|
| owner message | `›` gutter, plain text, `--ink` |
| manager text | `●` gutter, safe Markdown subset (paragraphs, lists, inline code, fenced code, bold/italic, links as text) built with DOM nodes; while streaming, the `partial` text with a caret (static when `prefers-reduced-motion`) |
| thinking | `∴` gutter, `--ink-soft`, open while streaming, collapsed after the turn: "Thought for 12 s" |
| command | `$ <command>` header in mono, status chip (running / exit 0 in `--review-ink` / exit N in `--blocked-ink`), output tail in a `pre`, collapsed beyond 20 lines with "show all (N lines)" |
| file change | `± path (+a −d)` header; unified diff with `--review-soft` / `--blocked-soft` line backgrounds; collapsed beyond 40 lines; Claude `Edit` inputs render old/new as a two-hunk diff |
| generic tool | `⚙ name · hint` one line (hint = path/command/query as today), expands to the existing field tree |
| task | `▣ T-… · status pill · title` when a tool call/result or callback carries a `task_id`; click opens the existing task detail view |
| callback | `↯ T-… review_ready · one-line summary` |
| goal | `◎` status transitions inline; the sticky strip holds the live state |
| error | `!` gutter, `--blocked-ink`, source + message, actions: Retry (resend last owner message), Open in terminal |
| turn end | muted footer: `turn 7 · 12 tools · in 3.1k · cache 88k · out 1.2k · 41 s` |

Accessibility and performance: every model string goes through `textContent` / `createTextNode` (the existing rule; the Markdown subset builds nodes, never `innerHTML`); `aria-live="polite"` announces final messages only, never deltas; Enter sends, Shift+Enter is a newline, ↑/↓ recalls history when the caret is at the edge, Esc stops the running turn; focus rings use `--accent`. Delta rendering is coalesced to one DOM write per animation frame. The DOM keeps the latest 400 blocks; older ones collapse into "load earlier" (already paged by `after_seq`).

Code placement: the console moves out of the 7 700-line `app.js` into `vscode-extension/media/manager_console.js` and `manager_console.css`, loaded by the webview HTML in `extension.js`. `app.js` keeps only the wiring (state, polling, message posting). This keeps the console's cards from colliding with every other dashboard card on `app.js`.

## 4. Event schema v3

Today's events: `{seq, at, turn, type, payload}` with types `user_message`, `assistant_text`, `reasoning`, `tool_call`, `tool_result`, `callback`, `error`, `turn_end`, `session_start`, `handoff_request`, `session_close`. v3 keeps all of them (old JSONL renders unchanged) and adds `"v": 3` on new events.

Changes and additions:

- `tool_call` `{call_id, name, input}` and `tool_result` `{call_id, name, output, is_error}`. `call_id` is Claude's `tool_use.id`, Codex's item id, OpenCode's `callID`; this replaces today's `tool_use_id`-in-`name` pairing.
- `command` `{call_id, command, cwd, status: running|completed|failed, exit_code, output_tail, output_bytes}` — from Codex `commandExecution` (+ `outputDelta`), Claude `Bash` tool_use/tool_result pairs, OpenCode `bash` tool parts. Emitted instead of the generic pair for those tools.
- `file_change` `{call_id, path, kind: add|update|delete, diff, added, removed}` — from Codex `fileChange` / `turn/diff/updated`, Claude `Edit`/`Write`/`MultiEdit` inputs, OpenCode `edit`/`write` parts.
- `goal` `{status, condition, reason, turns, tokens, budget, source: claude|codex|goal_loop}`.
- `turn_end.payload.usage` normalized to `{input, cache_read, cache_write, output, context_window, context_fill}`, with the provider's mapping kept as `raw`.
- `callback.payload` becomes the digest `{task_id, status, summary}` (see §6).

Bounds on persisted payloads: `output_tail` ≤ 8 KB, `diff` ≤ 64 KB, generic `output` ≤ 16 KB; a cut payload carries `truncated: true` and the original byte count. The full content stays with the provider (resume) and, for tasks, in task evidence.

**Deltas are not events.** The orchestrator keeps one in-memory `partial` per session: `{turn, text, reasoning, command_output}` for the running turn. `aiworkhub_manager_loop_events` returns it beside `events`; it is cleared when the final event for that item arrives. Nothing in `partial` is persisted, captured or sent back to a model. Claude deltas come from the `stream_event` lines that `--include-partial-messages` already requests and the translator currently drops.

Translators stay pure functions per backend (`_claude_events`, `_codex_events`, `_opencode_events`, new `_codex_app_server_events`), each covered by recorded-fixture tests. An unrecognized line is skipped, never fatal (current rule).

## 5. Goals

Slash command `/goal <condition>` (plus `/goal status`, `/goal stop`). One goal per session at a time.

- **claude_cli:** the backend sends `/goal <condition>` as the turn message and `/goal clear` on stop; Claude's own evaluator (Haiku by default) decides met / not-yet-met / impossible. The first W2 card records a real stream-json fixture of a `/goal` run and maps what it actually emits into `goal` events. Until that fixture exists the mapping is unspecified; nothing is guessed.
- **codex_app_server:** `thread/goal/set` / `get` / `clear`; `thread/goal/updated` / `cleared` notifications map to `goal` events (`active | paused | blocked | usageLimited | budgetLimited | complete`).
- **every other backend: GoalLoop** (`src/aiworkhub/manager_goal_loop.py`):
  1. send the condition as a turn ("Work toward this goal: … Reply GOAL_DONE when you believe it is met.");
  2. after each turn run the optional check commands — argv lists typed by the owner, shown in the goal strip before the first run, executed in the repository with a timeout, no shell;
  3. ask the evaluator — the cheapest route declared in `.aiworkhub/config/models.json` — for JSON `{verdict: met|not_yet|impossible, reason}` given the condition, the last manager message and the check results;
  4. stop on `met` (only when every check also passed), `impossible`, budget (`max_turns` default 20, optional `max_tokens`, optional wall clock) or owner stop; otherwise send "Continue toward the goal. Evaluator: <reason>. Checks: <one line each>".
  An evaluator failure yields verdict `unknown`: the loop pauses and asks the owner; `unknown` never counts as met.

## 6. Token economy

- A turn sends only the new message; the provider's resume carries history (already true).
- The brief (≤ 12 KB) goes only on the first turn and after rotation (already true).
- A callback wake turn carries one digest line per callback — `callback T-… review_ready: <title ≤ 80>` — and the manager pulls details with `task_show` / `review_packet` on demand.
- Transcript capture becomes provider-neutral: the orchestrator writes every final `user_message` / `assistant_text` to Context Graph through a generalized `manager_transcript_capture.write_completed_message(provider=…)`. Reasoning, tool output and deltas are never captured.
- Rotation is measured: `context_fill = (input + cache_read + cache_write) / context_window` from the last `turn_end`; `context_window` comes from the route's entry in `.aiworkhub/config/models.json` (W4 adds the field where it is missing). At ≥ 0.75 the orchestrator hands off. When a provider reports no usage, the current 512 KB byte estimate remains the fallback.
- One persistent app-server process per Codex session; no process per turn.
- The panel shows per-turn usage and the context hairline.

## 7. Slash commands and terminal

Chat/session control only: `/goal`, `/stop`, `/model <route>`, `/effort <level>`, `/new`, `/rotate`, `/terminal`, `/help`. The menu lists only commands valid for the current backend. Steering is not a command: it is plain text sent while a turn runs (§3).

**Open in terminal** (`/terminal` or the composer button): `extension.js` opens a VS Code terminal running the provider CLI resumed on the same conversation — `claude --resume <id>`, `codex resume <id>`, `opencode --session <id>` — with the manager seat environment from `provision_manager_seat_env`. While that terminal is alive the panel shows "attached in terminal" and disables Send (two writers on one conversation corrupt its order); `onDidCloseTerminal` re-enables it.

## 8. Error handling

| Failure | Behavior |
|---|---|
| provider exits non-zero | existing single `error` event with `classify_provider_outcome`; panel offers Retry / Open in terminal; the session stays active |
| app-server process dies | restart once and resume the thread by id; a second death is an `error` event, the session stays, the next send restarts |
| stream line unrecognized | skipped (current rule) |
| payload over bound | truncated with `truncated: true` + byte count |
| goal evaluator fails | verdict `unknown`, loop pauses, owner decides |
| check command times out | counts as a failed check, not a loop error |
| panel poll fails | exponential backoff to 5 s, "reconnecting" banner; `after_seq` makes resume lossless |
| steer on a backend without steer | queued as the next turn and labelled "queued" |

## 9. Testing

- **Translators:** recorded stream fixtures per backend under `tests/fixtures/manager_streams/` (claude stream-json incl. partial + `/goal`, codex exec, codex app-server, opencode) → exact v3 event lists.
- **Orchestrator:** `partial` never reaches the JSONL; Context Graph receives final user/assistant text for every backend; rotation fires at 75 % measured fill and falls back to bytes without usage; callback wake message equals the digest.
- **GoalLoop:** fake backend + fake evaluator covering met, met-with-failing-check (continues), not_yet, impossible, unknown (pauses), each budget, owner stop.
- **Webview:** the existing `vscode-extension/test` harness renders each block type from fixture events; a hostile payload (`<img src=x onerror=…>` in text, command, diff, path) renders as text; delta coalescing writes at most one DOM update per frame.
- **Live acceptance:** success criteria 1–5 measured on the real panel per wave.

## 10. Waves

| Wave | Content | Main files |
|---|---|---|
| W1 foundation | v3 schema, `call_id`, command/file_change extraction, Claude partial deltas, `partial` in `loop_events`; console extracted to `manager_console.js/.css`, block renderers, token footer, context hairline | `manager_loop_backends.py`, `manager_loop.py`, `manager_loop_service.py`, `server.py`; `media/manager_console.*`, `app.js`, `extension.js` |
| W2 native goals | `codex_app_server` backend (new module over `callback_bridge.AppServerClient`), Claude `/goal` fixture + mapping, goal strip, steer/stop | new `manager_loop_app_server.py`, `manager_loop_backends.py`, `manager_console.js` |
| W3 GoalLoop | `manager_goal_loop.py`, evaluator route, check runner, `/goal` on other backends | new module, `manager_loop.py` |
| W4 economy | provider-neutral capture, token-based rotation, callback digest | `manager_loop.py`, `manager_loop_wake.py`, `manager_transcript_capture.py` |
| W5 polish | slash menu, Open in terminal, ↑/↓ history, reconnect banner | `extension.js`, `manager_console.js` |

W1 blocks everything. W1's Python and webview halves can run in parallel (disjoint files, schema fixed by §4). W3 and W4 both write `manager_loop.py`, so they run sequentially; the plan orders them. Each card's `allowed_writes` includes its tests and the call sites it wires.

## 11. Risks

- Claude `/goal` stream shape is unmeasured → first W2 card is a fixture capture; mapping follows the evidence.
- Codex app-server protocol drift across CLI versions → the backend records the CLI version at start and fails closed with a named error on an unknown method.
- Terminal and panel on one conversation → Send is disabled while attached (§7).
- `app.js` size → the console extraction in W1 is the mitigation; no other dashboard view moves.
