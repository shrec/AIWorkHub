# Manager Chat (console W1) - manager handoff, 2026-10-03

Roadmap RM-2026-00067 (do not transition RM-2026-00076 to done). Owner priority: finish the
Manager Chat system completely, so callbacks arrive in the panel and polling ends.
"Finished" must be proven by a live seat end-to-end run, not by green tests alone.

- Spec: `docs/superpowers/specs/2026-09-26-manager-terminal-console-design.md`
- Plan: `docs/superpowers/plans/2026-09-27-manager-terminal-console-w1.md`

This note is an immutable input of the U2 card: do not edit it while that card is open.

## State

- main is 56 commits ahead of origin/main and is NOT pushed (the owner pushes; never force-push).
- Installed release: 0.12.17. It contains neither P1b, P2 nor G3b. Next release: 0.12.18, cut at M1.
- No worker or reviewer is running.
- Never commit: `.aiworkhub/config/models.json`, `.aiworkhub/eval-artifacts.json`, `.claude/settings.json`,
  `.cursor/`, `.mcp.json`, `.scratch_token_report.md`, `bash.exe.stackdump`, `code_audit_report_2026-09-24.md`,
  `docs/research/OH_MY_OPENAGENT_DEVELOPMENT_ASSESSMENT_2026-10-02.md`.

Card order: F0, M0, P1a, P1b, P2 (server side) and U1, U2, U3 (webview), then M1. U2 needs U1; U3 needs U2 and P2.

| Card | State | Commit |
|---|---|---|
| F0 stream capture | done | 61d8f7c |
| M0 fixtures | done | c1597aa |
| P1a event-log field bounds | done | 5cb2552 |
| P1b v3 translators (command, file_change, normalized usage) | done | 4cc8209 |
| P2 Claude deltas into an in-memory partial | done | 27f6484 |
| G3b wake re-binds a restored session (NF-2026-01246) | done | b0e8442 |
| U1 V2 renderer extract | review_ready, not accepted | - |
| U2 `AIWORKHUB_CONSOLE_W1_U2_GLYPH_BLOCKS_FOOTER_HAIRLINE_V1` | pending, not launched (needs U1) | - |
| U3 `AIWORKHUB_CONSOLE_W1_U3_MARKDOWN_PARTIAL_SCROLL_V1` | pending, not launched (needs U2) | - |
| M1 release 0.12.18 + live check | manager action, no card | - |

## Step 1 - accept U1 V2 (the only open review)

- Task `AIWORKHUB_CONSOLE_W1_U1_CONSOLE_RENDERER_EXTRACT_V2`, request `2a9a925cf177437f9d7dea0f6544cdf4`.
- The correctness reviewer accepted it; the earlier accept preview reported no blockers.
- It was not accepted only because the auto-mode classifier of the Claude host refused the accept call
  in that chat. This is not an AIWorkHub gate and no finding stands against the candidate.
- The candidate matched the pre-verified reference of the manager in all 12 files (base 042dca6).
  Expected git blob ids after the accept:

| Path | Lines | Blob |
|---|---|---|
| vscode-extension/media/manager_console.js | 213 | 525856f0c7623665533fad7ced9c5ba623b29c14 |
| vscode-extension/media/manager_console.css | 145 | 4e5edc1a41b6608fcd1d8b8196d570f2308854f7 |
| vscode-extension/media/app.js | 8295 | 9499a7ea868576f6b634632a724151709e4c9301 |
| vscode-extension/media/app.css | 4313 | 57f5155d221809fd60bc15c45273b7eb34990ae2 |
| vscode-extension/extension.js | 12784 | see note |
| vscode-extension/test/package-vsix.js | 646 | 1d19c8c452a241e91d21fcca5a5832a48cc52f0d |
| vscode-extension/test/manager-chat-panel.test.js | 1340 | ba6e330fb2e10efcda0e888be76003e736917c7d |
| vscode-extension/test/context-viewers.test.js | 1080 | d7dc51b08879c7b27d8cbe32e8c599069d5b151d |
| vscode-extension/test/claude-stream-events.test.js | 175 | a3a257d007aaca31368142a60eeda444848dd55b |
| vscode-extension/test/live-output-formatting.test.js | 516 | 1a313cab2ee6b28b66c9648593569bc9d5ad803a |
| vscode-extension/test/provider-event-shapes.test.js | 213 | 31cbdcafec1bf0df475a1226f98efd066019970e |
| tests/test_live_output_poll_rearm.py | 280 | e763b2be79764d243f75958dd8588ebf0da1b558 |

Note: after the card was launched, release 0.12.17 changed line 14 of `vscode-extension/extension.js` in the
canonical tree (the expected MCP package version, 0.12.16 to 0.12.17); no other U1 path changed. If the
accept reports a parent change on that file, relaunch the SAME card (never recreate the task id); the
expected result is the same U1 edit on top of the 0.12.17 line.

- The brief `docs/superpowers/plans/2026-10-02-manager-terminal-console-w1-amendments.md` is the immutable
  input of that card: do not edit it while the card is open.
- Commit subject after the accept:
  `refactor(console): manager chat block renderers and styles move to manager_console.js / manager_console.css`
- Then the learning commit for the accepted request.

## Step 2 - U2: glyph-gutter blocks, token footer, context hairline (plan lines 1349-1802)

Needs U1 landed. The plan carries the test file and the renderer code literally. These amendments were
measured against the tree with U1 applied; the full U2 edit itself has NOT been applied or run anywhere
yet, so the manager must run the Node tests on host against the candidate before the accept.

1. The assertions to change in `vscode-extension/test/manager-chat-panel.test.js` sit at lines
   501, 502, 511, 523, 536 and 555 after U1 (the 499-553 numbers in the plan are pre-U1).
2. `renderManagerChatEvents` returns early when there are no rows, so call `managerConsoleApplyHairline()`
   directly after the transcript guard at the top, not at the end as the plan says; otherwise an empty
   session keeps the hairline of the previous session.
3. Payload shapes match the committed fixtures in `tests/fixtures/manager_streams/*.expected.json`:
   `command` = call_id, command, cwd, exit_code, output_bytes, output_tail, status;
   `file_change` = call_id, path, kind, diff, added, removed; `turn_end.usage` = input, output, cache_read,
   cache_write, context_window, context_fill (null for codex and opencode), raw.
   A cut field is marked by `truncated: true` and `original_bytes` (`manager_loop._bounded_payload`).
4. Helpers exist where the plan expects them: `TASK_ID_RE` (app.js:15), `createElement`, `asArray`,
   `numberValue`, `limitText`, `requestTaskDetail`; `.sr-only` is in app.css. The media and test files
   are `eol=lf`, so newline-bearing slice markers in the tests hold.
5. `managerChatTurnCallCount` and `managerChatTurnEndNode` are referenced only inside
   `manager_console.js`; deleting them breaks nothing else.
6. Markup anchor: `#manager-chat-session-line` in `extension.js` (about line 11832 after U1); the
   `elements` map is app.js 328-353. The panel test harness has no hairline element, so
   `managerConsoleApplyHairline` must return quietly when the element is missing.
7. The new `test/manager-console.test.js` needs no registration in any test list.
8. Every harness that loads `manager_console.js` whole must be run by the manager on host, one file at a
   time with node --test: `manager-console`, `manager-chat-panel`, `context-viewers`, `claude-stream-events`,
   `live-output-formatting`, `provider-event-shapes`; plus `tests/test_live_output_poll_rearm.py`.
   Never run the whole extension suite (NF-2026-01190). Node tests and Python tests that spawn node do
   not run in the worker sandbox (NF-2026-01184): the sandbox validation of the card holds only
   sandbox-runnable Python files.
9. Other panel-test assertions on node tag or class may break once nodes are wrapped in a block; fix them
   by asserting on the wrapped node, never by deleting a test.
10. Security rule for every renderer: payloads are untrusted; `textContent` / `createTextNode` only,
   never `innerHTML`.

## Step 3 - U3: Markdown subset, live partial, block cap, announcements (plan lines 1806-2043)

Needs U2 and P2. Renders the `partial` returned by `aiworkhub_manager_loop_events`; deltas are display
only, never persisted, never cross a turn or session. Nothing of U3 has been measured against the tree
yet: re-measure the line positions the plan names after U2 lands, before launching.

## Step 4 - M1: release 0.12.18 and the live check

- Full suite green first; never cut a release on a red suite.
- Live end-to-end on claude, codex and opencode seats against the success criteria 1-5 of the spec.
- Before a live seat: no worker or reviewer running; clean the 15 stale `manager_chat` rows (G6).
- One manager per repository: a panel seat takes the manager route from the chat that holds it.

## After M1 (not carded)

- NF-2026-01238: context rotation at the measured 75 % fill with a clean context rebuilt from the
  Context Graph; MCP response diet. Codex and opencode report no `context_fill` yet.
- NF-2026-01231: the same translators for worker live output, server-side, before sanitization.
- G4 seat auth, G5 opencode seat identity re-verify, NF-2026-01243 (sub-agent lines in the partial).
- Known residual of G3b: a refused wake retries every `failed_turn_backoff_seconds` without end
  (NF-2026-01227); `manager_loop_service.start()` does not authorize its route.

## Method that returned byte-identical candidates on the first attempt (P2, G3b)

Write the literal unified diff, apply it in a full-repo overlay (a git archive of HEAD extracted into a
scratch directory, then git init there), run the changed tests plus `tests/test_declared_invariants.py`,
`tests/test_module_size_ratchet.py` and `tests/test_os_dependency_boundary.py` in that overlay, commit the
brief as the immutable input of the card, launch on the cheap model, then byte-compare the candidate
against the overlay.
