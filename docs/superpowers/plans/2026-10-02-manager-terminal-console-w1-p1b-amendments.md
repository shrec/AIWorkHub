# W1-P1b: measured amendments (v3 translators)

Base: plan lines 594-996 of `2026-09-27-manager-terminal-console-w1.md`. Implement the plan's code and
tests with ONLY the changes listed here. Everything below was measured on the committed fixtures in
`tests/fixtures/manager_streams/` (commit `c1597aa`); where the plan and this section disagree, this
section wins. Current positions in `src/aiworkhub/manager_loop_backends.py` (727 lines):
`_turn_end` 98-101, `_claude_events` 119-143, `_codex_events` 146-164, `_opencode_events` 167-188,
`_TRANSLATORS` 228-234, `translate` 257-265, `CliManagerBackend._turn` 525-576.

## P1b-1. Four fixtures, not three

| fixture stem | backend_id | what it is |
| --- | --- | --- |
| `claude_cli` | `claude_cli` | widened tools: Bash, Write (created), Edit, text `done` |
| `claude_cli_seat` | `claude_cli` | seat-exact argv: Write is denied (`is_error` true), Bash fallbacks |
| `codex_cli` | `codex_cli` | `gpt-5.5`: command_execution, file_change add + update, 3 agent messages |
| `opencode_cli` | `opencode_cli` | 4 steps: shell, write, edit, final text; 3 `step_finish` |

The fixture test iterates `(stem, backend_id)` pairs and each stem gets `<stem>.expected.json`. The
property "a `file_change` whose path ends with `notes.txt` exists" holds for every stem except
`claude_cli_seat`; for that stem assert instead that the denied Write yields a `tool_call` followed by
a `tool_result` with `is_error` true and the same `call_id`, and that no `file_change` exists.

## P1b-2. Claude: `context_fill` is the LAST call's context, not the turn total

Measured (`claude_cli.jsonl`): `result.usage` is cumulative over the turn's four API calls
(`cache_read_input_tokens` 120535 = 0 + 40005 + 40147 + 40383; `cache_creation_input_tokens` 40648 =
40005 + 142 + 236 + 265). The plan's `used = input + cache_read + cache_write` on `result.usage` gives
161191 tokens for a context that really holds 40650. Rotation reads this number, so it must be right.

- `TurnContext` gains `last_usage: dict` and `model: str`, refreshed from every `assistant` line's
  `message.usage` / `message.model` (several lines can share one message id and carry the same usage).
- `turn_end.usage` counts (`input`, `cache_read`, `cache_write`, `output`) stay the turn totals from
  `result.usage`; `raw` stays `result.usage`.
- `context_window` = `result.modelUsage[<context.model>].contextWindow`; when that model has no entry,
  the plan's max over `modelUsage`; else `None`. (Fixture: `claude-sonnet-5` 1000000 next to an
  auxiliary `claude-haiku-4-5-20251001` entry of 200000.)
- `context_fill` = (`input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens` of
  `context.last_usage`) / `context_window`, rounded to 4 places; `None` when either part is missing.
  Fixture values: `claude_cli` 40650 / 1000000, `claude_cli_seat` 40380 / 1000000.
- A `translate` call without a context keeps working and reports `context_fill: None`.

## P1b-3. Codex

As the plan, plus: `cache_write` reads `usage.cache_write_input_tokens` (the fixture carries it, 0).
`input` = `input_tokens` - `cached_input_tokens` (58122 - 42496 = 15626 in the committed fixture). The stream reports no window
and no per-call usage, so `context_window` and `context_fill` are `None`. `command` keeps the
provider's full command string (the fixture wraps it in a pwsh invocation); `exit_code` is `None` on
`item.started`. `file_change` events are emitted on `item.completed` only, one per `changes[]` entry.

## P1b-4. OpenCode: the real field names

The plan's names (`callID`, tool `bash`, `filePath`, `state.metadata.exit`) do not occur in the real
stream. Measured:

- top-level `type` is `tool_use`, `part.type` is `tool`; one line per tool, already at
  `state.status == "completed"` (no earlier running line).
- `call_id` = `part.id` (shape `call-<uuid>-N`); fall back to `part.callID`, then `part.partID`.
- `part.tool` is `shell` (accept `bash` too) -> one `command` event: `command` = `state.input.command`,
  output = `state.output`, exit code = `state.metadata.metadata.exit` (fall back to
  `state.metadata.exit`); `completed` when the exit code is 0 or absent with status `completed`,
  `failed` when status is `error` or the exit code is non-zero.
- `part.tool` is `write` -> `file_change`, path = `state.input.path` (fall back to `filePath`),
  pairs `[("", state.input.content)]`, kind `add` when `state.output` starts with `Created`, else
  `update`. `edit` -> `file_change` kind `update`, pairs `[(oldString, newString)]`. The diff is built
  by the plan's `_file_change` from the pairs; the provider's own patch text is not used.
- any other tool at `completed` / `error` -> `tool_call` then `tool_result` (same `call_id`), unless a
  running line for that `call_id` was already seen in `context.calls`, then only the `tool_result`;
  a running / pending line -> `tool_call` only.
- `step_finish` (`part.tokens` = `{input, output, reasoning, cache: {read, write}}`, one per step, and
  the FINAL step has none) no longer emits `turn_end`. It adds to the context: `input`, `cache_read`,
  `cache_write`, `output` are summed over the steps; `raw` = `{"steps": <count>, "last": <the last
  step's tokens mapping>}`; `context_window` and `context_fill` are `None`.

## P1b-5. `flush`: one `turn_end` per OpenCode turn

New pure function `flush(backend_id: str, context: TurnContext) -> list[dict]`: for `opencode_cli` it
returns exactly one `turn_end` (the summed usage, or an empty payload when no `step_finish` was seen);
for every other backend it returns `[]` (Claude and Codex end their turn with their own final event).
`CliManagerBackend._turn` yields `flush(...)` after the stdout loop only when the process exited
cleanly (not timed out, exit code 0) and the turn yielded no `error` event, as the turn's last
events: a turn ends with exactly one terminal event, and a failed turn keeps yielding only its error
event. The fixture test's `translated()` helper appends `flush(backend_id, context)` after the last
line. `tests/test_manager_loop_backends.py` gains a test through `_turn` with a replayed three-step
OpenCode stream: exactly one `turn_end`, last event, usage summed.

## P1b-6. Record and consumers

- `manager_loop.py`: `EVENT_TYPES` gains `command` and `file_change`; `_record` writes `"v": 3` on
  every new record; an old record without `v` still reads. `tests/test_manager_loop.py` gains a test
  for both.
- Before finishing, query Source Graph (`bodygrep`, target `src/aiworkhub`) for the literals
  `"tool_call"` and `"tool_result"`: every Python consumer that counts or summarises tool events must
  treat `command` and `file_change` the same way. List each consumer and what was done in the card
  result. No webview file changes in this card (renderers are W1-U2).

## P1b-7. Checks

- Sandbox validation (each file proven green in a sparse `src/ tests/ docs/` tree at `63c6d5c`,
  `160 passed`): `python -m pytest -q -p no:cacheprovider tests/test_manager_stream_fixtures.py
  tests/test_manager_loop_stream.py tests/test_manager_loop.py tests/test_manager_loop_backends.py
- Existing tests: `tests/test_manager_loop_backends.py` lines 254-287 assert the old raw usage
  mappings and a `turn_end` per OpenCode `step_finish`; update exactly those assertions to the v3
  shapes. No existing test function is removed or renamed. `tests/test_manager_loop_stream.py` and
  `tests/test_manager_loop_service.py` pass unchanged and are not modified.
- The `*.expected.json` files are generated by the plan's Step 4 generator from the final code and are
- The `*.expected.json` files are generated by the plan's Step 4 generator from the final code and are
  reviewed by the manager against the fixtures line by line. When they disagree, the translator is
  fixed, never the expected file.
- No fixture file is edited. No real provider CLI is started by any test.
