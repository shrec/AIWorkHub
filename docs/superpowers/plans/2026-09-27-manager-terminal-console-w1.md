# Manager Terminal Console — W1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repository every task below is executed as an **AIWorkHub task card**: the manager seat creates it with `aiworkhub_task_create_from_template`, launches it on `claude_cli`, reviews the callback and accepts or returns it. A worker sees only its own card, so each task is self-contained.

**Goal:** Turn the manager chat panel's event stream into schema v3 (call ids, commands, file changes, normalized usage, streamed partials) and render it as a terminal-like console with a glyph gutter, token footer and context hairline.

**Architecture:** Python side — pure per-backend translators in `manager_loop_backends.py` emit v3 events; `manager_loop.py` persists final events append-only with per-field bounds and keeps streamed deltas in an in-memory `partial` that `manager_loop_service.events` returns beside `events`. Webview side — the block renderers move out of `media/app.js` into `media/manager_console.js` / `.css` (loaded before `app.js`, sharing its globals at call time) and grow the v3 blocks. W2–W5 build goals, GoalLoop, token economy and polish on top.

**Tech Stack:** Python 3.12 (pytest, ruff), plain browser JavaScript in a VS Code webview (no bundler, no framework), Node `node:test` + `node:vm` harnesses, VS Code extension host (`extension.js`).

**Spec:** `docs/superpowers/specs/2026-09-26-manager-terminal-console-design.md` (approved 2026-09-26). Read §3 (UI rules and blocks table), §4 (schema v3), §9 (testing) before any W1 card.

## Launch gate

- No card in this plan launches before the owner has reviewed this plan **and** the P0–P2 task-system trust cards are closed (Coding Factory order: P0 trust → P1/P2 → P3 = this console → P4/P5; tracker artifact `P6hVFLqk96kEP9vANV9oeA`).
- Every card pins `adapter_id="claude_cli"` (the `vscode_lm` route has no credits until 2026-10-01).
- Every card's worktree must land under `.aiworkhub/runtime/worktrees/…`; a launch that resolves to `C:` / `%TEMP%` is cancelled, not waited on.
- Card labels below (W1-F0 …) are plan labels, not task ids. The task system assigns ids at creation; nobody writes an id into a card by hand.

## How a card is created from a task below

| Card field | Taken from |
|---|---|
| template | `implementation_with_tests` unless the task says otherwise |
| objective | the task's **Objective** line, verbatim |
| production_paths | the task's **Files** list minus test files |
| test_paths | the task's test files (they are part of `allowed_writes`, never in `allow_unchanged_required_outputs`) |
| acceptance | the task's **Acceptance** list |
| validation | the template's generated pytest/ruff/diff checks, plus the task's **Validation** commands as an override pair that includes the `regression` role (the gate refuses `behavioral_validation_roles_missing:regression` otherwise) |
| adapter | `claude_cli`; no token cap (tasks are uncapped unless the owner sets one) |

Python commands run from the repository root with `.venv/Scripts/python.exe`; Node commands run from `vscode-extension/`.

## Global Constraints

- New events carry `"v": 3`; old JSONL (no `v`) must still render unchanged.
- Persisted bounds: `output_tail` ≤ 8 KB, `diff` ≤ 64 KB, generic `output` ≤ 16 KB; a cut payload carries `truncated: true` and the original byte count.
- Deltas are not events: nothing in `partial` is persisted, captured to Context Graph, or sent back to a model.
- An unrecognized provider line is skipped, never fatal.
- Reuse `app.css` tokens (`--canvas`, `--surface-subtle`, `--ink`, `--ink-soft`, `--line`, `--accent`, `--muted`, status hues and their `-ink` / `-soft` variants, `--fs-*`). No hex literal, no new palette, no gradient, no card-inside-card.
- Color means state, never brand. Provider identity is a monochrome text chip.
- Prose in `--vscode-font-family`; commands, output, diffs, ids in `--vscode-editor-font-family`.
- Glyph gutter, fixed width: `›` owner, `●` manager text, `∴` thinking, `$` command, `±` file change, `⚙` tool, `▣` task, `↯` callback, `◎` goal, `!` error.
- Every model string reaches the DOM through `textContent` / `createTextNode`; never `innerHTML`.
- `aria-live="polite"` announces final messages only, never deltas.
- Delta rendering: at most one DOM write per animation frame.
- The DOM keeps the latest 400 blocks; older ones sit behind "load earlier".
- Context hairline: 2 px under the header; hue `--stale` at ≥ 60 %, `--blocked` at ≥ 75 %.
- Command output collapses beyond 20 lines ("show all (N lines)"); diffs collapse beyond 40 lines.
- Turn footer: `turn 7 · 12 tools · in 3.1k · cache 88k · out 1.2k · 41s` (duration from the existing `managerChatFormatDuration`).
- Test fixtures never contain the owner's e-mail, home/repository paths, user name or real conversation ids.

## Review Focus

1. **Torn JSONL tail** — a crash mid-append leaves a half line; the next append and every read must skip it, and `seq` must stay strictly increasing. (Pinned in W1-P1a.)
2. **Non-string tool output** — Claude `tool_result.content` is a list of blocks, MCP output is a JSON string, Codex output can be absent; bounding and rendering must accept all three without a raise. (Pinned in W1-P1a and W1-U2.)
3. **Hostile payload in path / command / diff / task title** — `<img src=x onerror=…>` in any of them renders as text. (Pinned in W1-U2.)
4. **Fixture leak** — a captured stream carries e-mail, `C:\Users\…`, the repository path or a real session id. (Pinned in W1-F0; re-checked by the manager step W1-M0.)
5. **Partial across turns** — a delta from turn N must never show under turn N+1, and a finished item must not show twice (final block + stale partial). (Pinned in W1-P2 and W1-U3.)

Known W2 risk recorded here so it is not lost: `sanitizeWebviewPayload` (`extension.js`) redacts `/word` after whitespace, so a `/goal …` string would be mangled on its way to the webview. W2 card W2-S1 owns it.

---

## File structure

| File | Responsibility | Cards |
|---|---|---|
| `scripts/capture_manager_stream.py` (new) | record one real manager turn per backend as a redacted JSONL fixture | F0 |
| `tests/fixtures/manager_streams/{claude_cli,codex_cli,opencode_cli}.jsonl` (new) | recorded provider streams | M0 |
| `tests/fixtures/manager_streams/*.expected.json` (new) | exact v3 event lists per fixture | P1b |
| `src/aiworkhub/manager_loop.py` | event log (append-only, bounded), event types, `partial` | P1a, P1b, P2 |
| `src/aiworkhub/manager_loop_backends.py` | per-backend translators → v3, deltas | P1b, P2 |
| `src/aiworkhub/manager_loop_service.py`, `src/aiworkhub/server.py` | `events` returns `partial` | P2 |
| `vscode-extension/media/manager_console.js` / `.css` (new) | console block renderers and their styles | U1, U2, U3 |
| `vscode-extension/media/app.js` / `app.css` | wiring only (state, polling, messaging) | U1, U2, U3 |
| `vscode-extension/extension.js` | webview HTML: asset tags, hairline, announcer | U1, U2, U3 |
| `vscode-extension/test/package-vsix.js` | VSIX file allowlist | U1 |

## Card graph

```
F0 ──► M0 (manager: capture fixtures) ──┐
P1a ────────────────────────────────────┴─► P1b ──► P2 ──┐
U1 ──► U2 ─────────────────────────────────────────────── ┴─► U3 ──► M1 (manager: W1 release + live check)
```

- Parallel start: **F0 ∥ P1a ∥ U1** (disjoint `allowed_writes`).
- P1b waits for M0 (fixtures) and P1a (both write `manager_loop.py`).
- P2 waits for P1b (same files). U2 waits for U1 (same files). U3 waits for U2 and P2 (renders `partial`).
- One intermediate release (M1) after U3; no user sees a half-wired console in between.

---

### Task W1-F0: stream capture tool

**Objective:** Add `scripts/capture_manager_stream.py`, which runs one real manager turn on a named backend inside the repository and writes every raw provider line, redacted, to a JSONL fixture.

**Files:**
- Create: `scripts/capture_manager_stream.py`
- Test: `tests/test_capture_manager_stream.py` (new)

**Interfaces:**
- Consumes: `aiworkhub.manager_loop_backends.CliManagerBackend(backend_id, model, repo, *, plan_builder=…, spawn=…)`, `manager_loop_backends._spawn_cli(argv, cwd, stdin_text=None, env=None)`.
- Produces: `redact_line(line: str, spellings: list[tuple[str, str]], ids: dict[str, str]) -> str`, `path_spellings(*roots: tuple[Path, str]) -> list[tuple[str, str]]`, `capture(backend_id, model, out_path, *, workdir=None, prompt=PROMPT, spawn=_spawn_cli, **backend_options) -> list[dict]`, CLI `python scripts/capture_manager_stream.py <backend_id> <model> <out.jsonl>`.

**Acceptance:**
- A captured line never contains the repository path, the home directory, the user name, an e-mail address or an original UUID; the same UUID maps to the same placeholder within one capture.
- The spawned process is only wrapped: the backend receives the same lines, `wait`/`poll`/`kill`/`stderr` still reach the real process.
- The default workdir is `.aiworkhub/runtime/fixture_capture/<backend_id>` inside the repository.

**Validation:** `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_capture_manager_stream.py` (target) and `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_loop_backends.py` (regression).

- [ ] **Step 1: Write the failing tests**

```python
"""W1-F0: the manager stream capture tool records redacted provider lines."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("capture_manager_stream", ROOT / "scripts" / "capture_manager_stream.py")
cms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cms)


class Plan:
    def __init__(self, cwd: str) -> None:
        self.argv = ["fake-cli"]
        self.cwd = cwd
        self.stdin_text = None
        self.launchable = True
        self.validation_reason = ""


def replay(lines: list[str], tmp_path: Path):
    """A spawn whose real child prints ``lines`` from a file (no Windows argv quoting of JSON)."""
    source = tmp_path / "stream.jsonl"
    source.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    script = "import sys; sys.stdout.write(open(sys.argv[1], encoding='utf-8').read())"

    def spawn(argv, cwd, stdin_text=None, env=None):
        return subprocess.Popen(
            [sys.executable, "-c", script, str(source)],
            cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )

    return spawn


def test_redact_line_replaces_paths_email_user_and_ids_stably():
    repo = Path("D:/Work/Repo")
    spellings = cms.path_spellings((repo / "sub", "<workdir>"), (repo, "<repo>"))
    ids: dict[str, str] = {}
    raw = json.dumps({
        "cwd": str(repo / "sub"),
        "file": "d:/work/repo/src/x.py",
        "who": "owner@example.org",
        "session_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
        "again": "0F8FAD5B-D9CB-469F-A165-70867728950E",
    })
    line = cms.redact_line(raw, spellings, ids)
    data = json.loads(line)
    assert data["cwd"] == "<workdir>"
    assert data["file"] == "<repo>/src/x.py"
    assert data["who"] == "<email>"
    assert data["session_id"] == data["again"] == "00000000-0000-4000-8000-000000000001"
    assert "Repo" not in line and "example.org" not in line


def test_a_user_name_is_replaced_only_as_a_whole_word():
    line = cms.redact_line("ann wrote annotations for ann", [("ann", "<user>")], {})
    assert line == "<user> wrote annotations for <user>"


def test_capture_tees_redacted_lines_and_keeps_the_stream(tmp_path: Path):
    workdir = tmp_path / "work"
    out = tmp_path / "out" / "claude_cli.jsonl"
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "0f8fad5b-d9cb-469f-a165-70867728950e", "cwd": str(workdir)}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}),
        json.dumps({"type": "result", "usage": {"input_tokens": 3, "output_tokens": 1}}),
    ]
    events = cms.capture(
        "claude_cli", "fixture-model", out, workdir=workdir,
        spawn=replay(lines, tmp_path),
        plan_builder=lambda backend_id, prompt, repo, *, model="": Plan(str(repo)),
    )
    assert [event["type"] for event in events] == ["assistant_text", "turn_end"]
    written = out.read_text(encoding="utf-8").splitlines()
    assert len(written) == 3
    assert str(workdir) not in out.read_text(encoding="utf-8")
    assert json.loads(written[0])["cwd"] == "<workdir>"


def test_default_workdir_is_inside_the_repository():
    assert cms.default_workdir("codex_cli") == ROOT / ".aiworkhub" / "runtime" / "fixture_capture" / "codex_cli"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_capture_manager_stream.py`
Expected: FAIL — `FileNotFoundError` for `scripts/capture_manager_stream.py`.

- [ ] **Step 3: Write the implementation**

```python
"""Record one real manager turn per backend as a redacted stream fixture.

Usage: python scripts/capture_manager_stream.py <backend_id> <model> <out.jsonl>

The turn runs in .aiworkhub/runtime/fixture_capture/<backend_id>, inside the
repository and never under %TEMP%. Every raw provider line is written after
redaction: repository, workdir and home paths, the user name, e-mail
addresses and UUIDs are replaced, so a fixture never carries the owner's
identity.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiworkhub import manager_loop_backends as backends  # noqa: E402

PROMPT = (
    "Fixture capture. Think briefly first. Then: 1) run the shell command "
    "`git --version`; 2) create notes.txt containing the line alpha; "
    "3) edit notes.txt so the line reads beta; 4) reply with the single word done."
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def default_workdir(backend_id: str) -> Path:
    return ROOT / ".aiworkhub" / "runtime" / "fixture_capture" / backend_id


def path_spellings(*roots: tuple[Path, str]) -> list[tuple[str, str]]:
    """Every way a provider may print each root (native, POSIX, JSON-escaped), longest first."""
    found: dict[str, str] = {}
    for root, token in roots:
        native = str(root)
        for spelling in (native, root.as_posix(), native.replace("\\", "\\\\")):
            found.setdefault(spelling, token)
    return sorted(found.items(), key=lambda item: len(item[0]), reverse=True)


def redact_line(line: str, spellings: list[tuple[str, str]], ids: dict[str, str]) -> str:
    for spelling, token in spellings:
        # Bounded on both sides, so the user name never eats part of a longer word.
        line = re.sub(rf"(?<!\w){re.escape(spelling)}(?!\w)", token, line, flags=re.IGNORECASE)
    line = _EMAIL.sub("<email>", line)

    def stable(match: re.Match[str]) -> str:
        key = match.group(0).lower()
        if key not in ids:
            ids[key] = f"00000000-0000-4000-8000-{len(ids) + 1:012d}"
        return ids[key]

    return _UUID.sub(stable, line)


class _Tee:
    """The spawned process, with every stdout line also written (redacted) to ``sink``."""

    def __init__(self, process: Any, sink: Any, redact: Callable[[str], str]) -> None:
        self._process = process
        self.stdout = self._lines(process.stdout, sink, redact)

    @staticmethod
    def _lines(stream: Any, sink: Any, redact: Callable[[str], str]) -> Iterator[str]:
        for raw in stream or ():
            if raw.strip():
                sink.write(redact(raw.rstrip("\r\n")) + "\n")
                sink.flush()
            yield raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._process, name)


def capture(
    backend_id: str,
    model: str,
    out_path: Path,
    *,
    workdir: Path | None = None,
    prompt: str = PROMPT,
    spawn: Callable[..., Any] = backends._spawn_cli,
    **backend_options: Any,
) -> list[dict[str, Any]]:
    workdir = workdir or default_workdir(backend_id)
    workdir.mkdir(parents=True, exist_ok=True)
    home = Path.home()
    spellings = path_spellings((workdir, "<workdir>"), (ROOT, "<repo>"), (home, "<home>"))
    spellings.append((home.name, "<user>"))
    ids: dict[str, str] = {}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as sink:

        def tee_spawn(argv: Any, cwd: Any, stdin_text: Any = None, env: Any = None) -> Any:
            process = spawn(argv, cwd, stdin_text) if env is None else spawn(argv, cwd, stdin_text, env)
            return _Tee(process, sink, lambda line: redact_line(line, spellings, ids))

        backend = backends.CliManagerBackend(backend_id, model, workdir, spawn=tee_spawn, **backend_options)
        backend.start("")
        try:
            return list(backend.send(prompt))
        finally:
            backend.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("backend_id")
    parser.add_argument("model")
    parser.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    events = capture(args.backend_id, args.model, args.out)
    print(f"{len(events)} events; fixture: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_capture_manager_stream.py`
Expected: 4 passed.

- [ ] **Step 5: Lint and commit** (the worker stops at review; the manager commits on accept)

```bash
.venv/Scripts/python.exe -m ruff check scripts/capture_manager_stream.py tests/test_capture_manager_stream.py
git add scripts/capture_manager_stream.py tests/test_capture_manager_stream.py
git commit -m "feat(console): record redacted manager stream fixtures"
```

---

### Task W1-M0 (manager host step, not a card): capture the three fixtures

Runs after F0 is accepted. The manager seat runs it on the host because it spends real provider turns; no worker writes these files.

- [ ] **Step 1: Capture**

```bash
cd /d/Dev/AIWorkHub
model() { .venv/Scripts/python.exe -c "import json, sys; routes = {r: ms for p in json.load(open('.aiworkhub/config/models.json', encoding='utf-8'))['models'].values() for r, ms in p.items()}; print(next(m for m, on in routes[sys.argv[1]].items() if on))" "$1"; }
for backend in claude_cli codex_cli opencode_cli; do
  .venv/Scripts/python.exe scripts/capture_manager_stream.py "$backend" "$(model "$backend")" "tests/fixtures/manager_streams/$backend.jsonl"
done
```

Each route uses its first enabled model in `.aiworkhub/config/models.json` (`claude_cli` resolves to `sonnet` today); the model does not change the stream shape. If a backend refuses a tool (permission mode), the fixture records the refusal; record it as a NeedFix with the fixture lines as evidence instead of hand-editing the stream.

- [ ] **Step 2: Leak check** — must print nothing (e-mail shapes, the account name, home and repository paths):

```bash
awk -v user="$(basename "$HOME")" '{ l = tolower($0) } l ~ /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z][a-z]+/ || index(l, tolower(user)) || l ~ /users[\\\/]|dev[\\\/]+aiworkhub/' tests/fixtures/manager_streams/*.jsonl
```

- [ ] **Step 3: Content check** — each fixture has a shell command, a file write and an edit with paired ids (Claude `tool_use.id` ↔ `tool_result.tool_use_id`; Codex `item.id` on `item.started`/`item.completed`; OpenCode `callID`), and ends with the provider's turn-end line (`result` / `turn.completed` / `step_finish`). A fixture missing one of them is re-captured, not patched.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/manager_streams/
git commit -m "test(console): recorded claude, codex and opencode manager stream fixtures"
```

---

### Task W1-P1a: append-only event log with per-field bounds

**Objective:** Make `SessionStore.append_event` append one line instead of rewriting the log, tolerate a torn last line, and bound `output_tail` / `output` / `diff` per field instead of collapsing the whole payload.

**Files:**
- Modify: `src/aiworkhub/manager_loop.py` (constants near line 52; `_bounded_payload` at ~177; `SessionStore.events` / `append_event` at ~320–333)
- Test: `tests/test_manager_loop.py`

**Interfaces:**
- Produces: `FIELD_BOUNDS: Mapping[str, int]` (`{"output_tail": 8192, "output": 16384, "diff": 65536}`); `_bounded_payload(payload) -> dict` — a bounded field longer than its bound is cut to the bound and the payload gains `truncated: True` and `original_bytes: <int, the sum of the cut fields' original sizes>`; other fields keep today's rule (whole payload over `MAX_EVENT_PAYLOAD_BYTES` + the present fields' bounds → `{"truncated", "original_bytes", "preview"}`). `SessionStore.events(session_id)` returns at most `max_events` newest parseable records. `append_event` signature unchanged.

**Acceptance:**
- The existing test `test_the_event_log_is_bounded_and_payloads_are_capped` passes unchanged.
- Between compactions the log file only grows (append), and it is rewritten through `_publish` only when `seq % max_events == 0`.
- A torn last line is skipped by reads and by the next append; `seq` stays strictly increasing.
- A list-valued `output` is serialized before it is bounded; nothing raises for str / list / dict / None.

**Validation:** `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_loop.py` (target) and `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_loop_service.py tests/test_manager_loop_backends.py tests/test_manager_loop_wake.py` (regression).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_manager_loop.py`)

```python
def test_bounded_fields_are_cut_per_field_not_per_payload(tmp_path: Path) -> None:
    store = ml.SessionStore(tmp_path, REPO_ID)
    output = store.append_event("session-0001", {"type": "tool_result", "payload": {"call_id": "c1", "output": "o" * 20_000}})
    diff = store.append_event("session-0001", {"type": "file_change", "payload": {"path": "a.py", "diff": "d" * 70_000}})
    tail = store.append_event("session-0001", {"type": "command", "payload": {"command": "ls", "output_tail": "t" * 9_000}})

    assert output["payload"]["call_id"] == "c1"
    assert len(output["payload"]["output"].encode("utf-8")) <= ml.FIELD_BOUNDS["output"]
    assert output["payload"]["truncated"] is True and output["payload"]["original_bytes"] == 20_000
    assert diff["payload"]["path"] == "a.py"
    assert len(diff["payload"]["diff"].encode("utf-8")) <= ml.FIELD_BOUNDS["diff"]
    assert diff["payload"]["original_bytes"] == 70_000
    assert len(tail["payload"]["output_tail"].encode("utf-8")) <= ml.FIELD_BOUNDS["output_tail"]
    assert tail["payload"]["command"] == "ls"


def test_non_string_output_is_serialized_before_it_is_bounded(tmp_path: Path) -> None:
    store = ml.SessionStore(tmp_path, REPO_ID)
    blocks = [{"type": "text", "text": "x" * 9_000}, {"type": "text", "text": "y" * 9_000}]
    record = store.append_event("session-0001", {"type": "tool_result", "payload": {"output": blocks}})
    small = store.append_event("session-0001", {"type": "tool_result", "payload": {"output": [{"type": "text", "text": "ok"}]}})
    empty = store.append_event("session-0001", {"type": "tool_result", "payload": {"output": None}})

    assert isinstance(record["payload"]["output"], str) and record["payload"]["truncated"] is True
    assert small["payload"]["output"] == [{"type": "text", "text": "ok"}]
    assert empty["payload"]["output"] is None


def test_the_log_is_appended_between_compactions(tmp_path: Path) -> None:
    store = ml.SessionStore(tmp_path, REPO_ID, max_events=3)
    path = tmp_path / "events" / "session-0001.jsonl"
    sizes = []
    for index in range(7):
        store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": str(index)}})
        sizes.append(len(path.read_text(encoding="utf-8").splitlines()))

    # seq 3 and seq 6 compact the log to its newest 3 lines; every other append adds one line.
    assert sizes == [1, 2, 3, 4, 5, 3, 4]
    assert [event["seq"] for event in store.events("session-0001")] == [5, 6, 7]


def test_a_torn_tail_line_is_skipped_and_seq_keeps_increasing(tmp_path: Path) -> None:
    store = ml.SessionStore(tmp_path, REPO_ID)
    store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": "one"}})
    path = tmp_path / "events" / "session-0001.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "type": "assistant_te')  # crash mid-append, no newline

    after = store.append_event("session-0001", {"type": "assistant_text", "payload": {"text": "two"}})

    assert after["seq"] == 2
    assert [event["payload"]["text"] for event in store.events("session-0001")] == ["one", "two"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_loop.py -k "bounded_fields or non_string or appended_between or torn_tail"`
Expected: FAIL — `AttributeError: module 'aiworkhub.manager_loop' has no attribute 'FIELD_BOUNDS'`, and the append test sees `[1, 2, 3, 3, 3, 3, 3]` because today every append rewrites the whole log.

- [ ] **Step 3: Implement**

Add beside the other constants (and `from types import MappingProxyType` to the imports):

```python
FIELD_BOUNDS: Mapping[str, int] = MappingProxyType({"output_tail": 8 * 1024, "output": 16 * 1024, "diff": 64 * 1024})
_TAIL_READ_BYTES = 256 * 1024
```

Replace `_bounded_payload`:

```python
def _bounded_field(value: object, limit: int) -> tuple[object, int]:
    """``value`` cut to ``limit`` UTF-8 bytes (serialized first when not a string) and its original size."""
    text = value if isinstance(value, str) else _json(value)
    size = len(text.encode("utf-8"))
    if size <= limit:
        return value, 0
    return _clip(text, limit), size


def _bounded_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``payload`` as plain JSON with each bounded field cut to its own bound.

    Fields without a bound share ``MAX_EVENT_PAYLOAD_BYTES``; a payload that
    still outgrows that becomes a marked preview, as before.
    """
    fields = dict(payload)
    cut = 0
    for key in FIELD_BOUNDS.keys() & fields.keys():
        fields[key], size = _bounded_field(fields[key], FIELD_BOUNDS[key])
        cut += size
    if cut:
        fields["truncated"] = True
        fields["original_bytes"] = cut
    text = _json(fields)
    size = len(text.encode("utf-8"))
    allowance = MAX_EVENT_PAYLOAD_BYTES + sum(FIELD_BOUNDS[key] for key in FIELD_BOUNDS.keys() & fields.keys())
    if size <= allowance:
        return dict(json.loads(text))
    return {"truncated": True, "original_bytes": size, "preview": _clip(text, MAX_EVENT_PAYLOAD_BYTES // 2)}
```

Add the helpers above `class SessionStore`:

```python
def _parsed(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Every complete record; a torn or foreign line is skipped, never fatal."""
    found = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("seq"), int):
            found.append(record)
    return found


def _last_seq(path: Path) -> int:
    """The newest complete record's ``seq`` from the log's tail, 0 for an empty log."""
    if not path.is_file():
        return 0
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - _TAIL_READ_BYTES))
        tail = handle.read().decode("utf-8", "ignore").splitlines()
    records = _parsed(tail)
    return records[-1]["seq"] if records else 0


def _ends_with_newline(path: Path) -> bool:
    """Whether the log's last byte closes a line; one byte is read, never the whole file."""
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"
```

Replace `SessionStore.events` and `append_event`:

```python
    def events(self, session_id: str) -> list[dict[str, Any]]:
        path = self._path("events", session_id)
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        return _parsed(lines)[-self.max_events:]

    def append_event(self, session_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        """Append ``event`` with the next ``seq`` and a bounded payload; return it.

        One line is appended per event; every ``max_events`` events the log is
        compacted to its newest ``max_events`` records through ``_publish``.
        """
        path = self._path("events", session_id)
        seq = _last_seq(path) + 1
        record = {**event, "seq": seq, "payload": _bounded_payload(event.get("payload") or {})}
        path.parent.mkdir(parents=True, exist_ok=True)
        torn = path.is_file() and path.stat().st_size > 0 and not _ends_with_newline(path)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(("\n" if torn else "") + json.dumps(record, sort_keys=True) + "\n")
        if seq % self.max_events == 0:
            kept = self.events(session_id)
            _publish(path, "".join(json.dumps(item, sort_keys=True) + "\n" for item in kept))
        return record
```

`os`, `Iterable`, `Mapping` and `Path` are already imported by `manager_loop.py`; `MappingProxyType` is the only new import.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_loop.py`
Expected: all pass, including `test_the_event_log_is_bounded_and_payloads_are_capped`.

- [ ] **Step 5: Regression, lint, commit**

```bash
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_loop_service.py tests/test_manager_loop_backends.py tests/test_manager_loop_wake.py
.venv/Scripts/python.exe -m ruff check src/aiworkhub/manager_loop.py tests/test_manager_loop.py
git add src/aiworkhub/manager_loop.py tests/test_manager_loop.py
git commit -m "feat(console): append-only manager event log with per-field bounds"
```

---

### Task W1-P1b: v3 translators (call ids, commands, file changes, normalized usage)

**Objective:** Translate each backend's recorded stream into v3 events — `call_id` on every tool event, `command` for shell tools, `file_change` for edit/write tools, normalized `turn_end.usage`, `"v": 3` on new records — pinned by the W1-M0 fixtures.

**Files:**
- Modify: `src/aiworkhub/manager_loop_backends.py` (`_turn_end` ~98, `_claude_events` 119–143, `_codex_events` 146–164, `_opencode_events` 167–188, `_TRANSLATORS` ~228, `translate` 249–257, `CliManagerBackend._turn` 512–563)
- Modify: `src/aiworkhub/manager_loop.py` (`EVENT_TYPES` line 46; `_record` ~945)
- Test: `tests/test_manager_loop_stream.py`, `tests/test_manager_loop_backends.py`, `tests/test_manager_loop.py`, `tests/test_manager_stream_fixtures.py` (new), `tests/fixtures/manager_streams/{claude_cli,codex_cli,opencode_cli}.expected.json` (new)

**Interfaces:**
- Consumes: the fixtures from W1-M0; `FIELD_BOUNDS` from W1-P1a.
- Produces:
  - `TurnContext` dataclass with `calls: dict[str, dict[str, Any]]`.
  - `translate(backend_id: str, event: Any, context: TurnContext | None = None) -> list[dict]`.
  - Event payloads exactly as spec §4: `tool_call {call_id, name, input}`, `tool_result {call_id, name, output, is_error}`, `command {call_id, command, cwd, status, exit_code, output_tail, output_bytes}`, `file_change {call_id, path, kind, diff, added, removed}`, `turn_end {usage: {input, cache_read, cache_write, output, context_window, context_fill, raw}}` (usage only when the provider reported it).
  - `manager_loop.EVENT_TYPES` gains `"command"` and `"file_change"`; `_record` writes `"v": 3`.

**Acceptance:**
- Each fixture, run line by line through `translate` with one `TurnContext`, yields exactly its `*.expected.json`; the manager reviews the expected files against the fixtures, not against the code.
- Property checks hold on every fixture: every tool/command/file_change event has a non-empty `call_id`; a `git --version` `command` ends `completed`; a `file_change` for `notes.txt` exists; no event type outside `EVENT_TYPES`; `turn_end.usage` (when present) has all normalized keys.
- Existing tests change only where v3 changes shape on purpose (call ids added; `tool_result.name` is the tool name, not the `tool_use_id`; Bash → `command`; Edit/Write → `file_change`; usage normalized). Every other assertion stays.
- A field name the code reads that the fixture does not carry is taken from the fixture instead, and the difference is listed in the card result.

**Validation:** `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_stream_fixtures.py tests/test_manager_loop_stream.py` (target) and `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_loop.py tests/test_manager_loop_backends.py tests/test_manager_loop_service.py` (regression).

- [ ] **Step 0: Read the fixtures first.** For every field the code in Step 3 reads (`id`, `tool_use_id`, `is_error`, `input.command`, `input.file_path`, `old_string`/`new_string`, `edits`, `content`; Codex `item.id`, `command`, `aggregated_output`, `exit_code`, `changes[].path`, `changes[].kind`, `usage.cached_input_tokens`; OpenCode `callID`, `tool`, `state.status`, `state.input.command`, `state.input.filePath`, `oldString`/`newString`, `state.output`, `state.metadata`, `tokens.cache.read`/`write`; Claude `result.modelUsage.*.contextWindow`), confirm the fixture carries it under that name. Where it does not, use the fixture's name.

- [ ] **Step 1: Write the failing fixture test** (`tests/test_manager_stream_fixtures.py`)

```python
"""W1-P1b: recorded provider streams translate to exact v3 event lists."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import manager_loop as ml
from aiworkhub import manager_loop_backends as mlb

FIXTURES = Path(__file__).parent / "fixtures" / "manager_streams"
BACKENDS = ("claude_cli", "codex_cli", "opencode_cli")
USAGE_KEYS = {"input", "cache_read", "cache_write", "output", "context_window", "context_fill", "raw"}


def translated(backend_id: str) -> list[dict]:
    context = mlb.TurnContext()
    events: list[dict] = []
    for line in (FIXTURES / f"{backend_id}.jsonl").read_text(encoding="utf-8").splitlines():
        # Deltas (W1-P2) are display-only, never events, exactly as the orchestrator treats them.
        events.extend(e for e in mlb.translate(backend_id, json.loads(line), context) if e["type"] != "delta")
    return events


@pytest.mark.parametrize("backend_id", BACKENDS)
def test_fixture_translates_to_the_expected_v3_events(backend_id: str) -> None:
    expected = json.loads((FIXTURES / f"{backend_id}.expected.json").read_text(encoding="utf-8"))
    assert translated(backend_id) == expected


@pytest.mark.parametrize("backend_id", BACKENDS)
def test_fixture_events_keep_the_v3_invariants(backend_id: str) -> None:
    events = translated(backend_id)
    kinds = {event["type"] for event in events}
    assert kinds <= ml.EVENT_TYPES
    for event in events:
        if event["type"] in ("tool_call", "tool_result", "command", "file_change"):
            assert event["payload"]["call_id"], event
    commands = [e["payload"] for e in events if e["type"] == "command"]
    assert any("git --version" in c["command"] and c["status"] == "completed" for c in commands)
    assert any(e["payload"]["path"].endswith("notes.txt") for e in events if e["type"] == "file_change")
    ends = [e for e in events if e["type"] == "turn_end"]
    assert ends
    for end in ends:
        usage = end["payload"].get("usage")
        assert usage is None or set(usage) == USAGE_KEYS


def test_a_file_change_counts_content_lines_that_look_like_headers() -> None:
    change = mlb._file_change("e1", "notes.txt", [("alpha\n", "+++beta\n---gamma\n")], "update")
    assert change["payload"]["added"] == 2 and change["payload"]["removed"] == 1
    assert change["payload"]["diff"].splitlines()[:2] == ["--- a/notes.txt", "+++ b/notes.txt"]


def test_an_unpaired_result_without_context_still_carries_its_call_id() -> None:
    event = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}}
    assert mlb.translate("claude_cli", event) == [
        {"type": "tool_result", "payload": {"call_id": "toolu_1", "name": "", "output": "ok", "is_error": False}}
    ]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_stream_fixtures.py`
Expected: FAIL — `AttributeError: module 'aiworkhub.manager_loop_backends' has no attribute 'TurnContext'`.

- [ ] **Step 3: Implement the translators**

Add near the top of `manager_loop_backends.py` (imports: `dataclasses`, `difflib`, `json` if not present):

```python
OUTPUT_TAIL_BYTES = 8 * 1024
_CLAUDE_COMMAND_TOOLS = frozenset({"Bash"})
_CLAUDE_EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write"})


@dataclasses.dataclass
class TurnContext:
    """What one turn's translator remembers between lines: each open tool call by id."""

    calls: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)


def _count(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else 0


def _usage(input_: Any, cache_read: Any, cache_write: Any, output: Any, window: Any, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Provider usage in the one shape the console and rotation read (spec §4)."""
    counts = {"input": _count(input_), "cache_read": _count(cache_read), "cache_write": _count(cache_write), "output": _count(output)}
    size = _count(window)
    used = counts["input"] + counts["cache_read"] + counts["cache_write"]
    return {**counts, "context_window": size or None, "context_fill": round(used / size, 4) if size else None, "raw": dict(raw)}


def _tail(text: str, limit: int = OUTPUT_TAIL_BYTES) -> str:
    data = text.encode("utf-8")
    return text if len(data) <= limit else data[-limit:].decode("utf-8", "ignore")


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(block.get("text") or "") for block in content if isinstance(block, Mapping) and block.get("type") == "text")
    return "" if content is None else json.dumps(content, default=str)


def _command(call_id: str, command: str, *, status: str, exit_code: Any = None, output: str = "", cwd: str = "") -> dict[str, Any]:
    return {"type": "command", "payload": {
        "call_id": call_id, "command": command, "cwd": cwd, "status": status,
        "exit_code": exit_code if isinstance(exit_code, int) else None,
        "output_tail": _tail(output), "output_bytes": len(output.encode("utf-8")),
    }}


def _file_change(call_id: str, path: str, pairs: list[tuple[str, str]], kind: str) -> dict[str, Any]:
    """One file change; each (before, after) pair becomes one hunk of a unified diff."""
    lines: list[str] = []
    added = removed = 0
    for before, after in pairs:
        hunk = list(difflib.unified_diff(before.splitlines(), after.splitlines(), f"a/{path}", f"b/{path}", lineterm=""))
        body = hunk[2:]  # past the ---/+++ header; a content line "+++x" still counts as one added line
        added += sum(1 for line in body if line.startswith("+"))
        removed += sum(1 for line in body if line.startswith("-"))
        lines.extend(body if lines else hunk)
    return {"type": "file_change", "payload": {"call_id": call_id, "path": path, "kind": kind, "diff": "\n".join(lines), "added": added, "removed": removed}}


def _tool_call(call_id: str, name: str, request: Any) -> dict[str, Any]:
    return {"type": "tool_call", "payload": {"call_id": call_id, "name": name, "input": request}}


def _tool_result(call_id: str, name: str, output: Any, failed: bool) -> dict[str, Any]:
    return {"type": "tool_result", "payload": {"call_id": call_id, "name": name, "output": output, "is_error": failed}}
```

Replace `_turn_end` (callers now pass normalized usage or `None`):

```python
def _turn_end(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    """The turn's final event; ``usage`` appears only when the provider reported it."""
    return {"type": "turn_end", "payload": {"usage": dict(usage)} if usage else {}}
```

Claude:

```python
def _claude_usage(event: Mapping[str, Any]) -> dict[str, Any] | None:
    usage = _mapping(event.get("usage"))
    if not usage:
        return None
    windows = [_count(_mapping(entry).get("contextWindow")) for entry in _mapping(event.get("modelUsage")).values()]
    return _usage(usage.get("input_tokens"), usage.get("cache_read_input_tokens"),
                  usage.get("cache_creation_input_tokens"), usage.get("output_tokens"), max(windows, default=0), usage)


def _claude_tool_use(block: Mapping[str, Any], context: TurnContext) -> list[dict[str, Any]]:
    call_id, name = str(block.get("id") or ""), str(block.get("name") or "")
    context.calls[call_id] = {"name": name, "input": block.get("input")}
    if name in _CLAUDE_COMMAND_TOOLS:
        return [_command(call_id, str(_mapping(block.get("input")).get("command") or ""), status="running")]
    if name in _CLAUDE_EDIT_TOOLS:
        return []  # shown once its result proves the write happened
    return [_tool_call(call_id, name, block.get("input"))]


def _claude_file_changes(call_id: str, name: str, request: Mapping[str, Any], output: str) -> list[dict[str, Any]]:
    path = str(request.get("file_path") or "")
    if name == "Write":
        kind = "add" if output.startswith("File created") else "update"
        return [_file_change(call_id, path, [("", str(request.get("content") or ""))], kind)]
    edits = request.get("edits") if name == "MultiEdit" else [request]
    pairs = [(str(e.get("old_string") or ""), str(e.get("new_string") or "")) for e in edits or [] if isinstance(e, Mapping)]
    return [_file_change(call_id, path, pairs, "update")]


def _claude_tool_result(block: Mapping[str, Any], context: TurnContext) -> list[dict[str, Any]]:
    call_id = str(block.get("tool_use_id") or "")
    call = context.calls.pop(call_id, {"name": "", "input": None})
    name, request, failed = call["name"], _mapping(call["input"]), bool(block.get("is_error"))
    output = _result_text(block.get("content"))
    if name in _CLAUDE_COMMAND_TOOLS:
        status = "failed" if failed else "completed"
        return [_command(call_id, str(request.get("command") or ""), status=status, output=output)]
    if name in _CLAUDE_EDIT_TOOLS and not failed:
        return _claude_file_changes(call_id, name, request, output)
    shown = [_tool_call(call_id, name, call["input"])] if name in _CLAUDE_EDIT_TOOLS else []
    return [*shown, _tool_result(call_id, name, block.get("content"), failed)]


def _claude_events(event: Mapping[str, Any], context: TurnContext) -> list[dict[str, Any]]:
    """Claude ``stream-json``: assistant/user content blocks, then the result event."""
    if str(event.get("type") or "") == "result":
        return [_turn_end(_claude_usage(event))]
    content = _mapping(event.get("message")).get("content")
    events = _assistant_text(context_capture._message_text(content))
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "tool_use":
            events.extend(_claude_tool_use(block, context))
        elif block.get("type") == "tool_result":
            events.extend(_claude_tool_result(block, context))
        elif block.get("type") == "thinking":
            events.extend(_reasoning(block.get("thinking")))
    return events
```

Codex (`exec --json`):

```python
def _codex_usage(usage: Any) -> dict[str, Any] | None:
    usage = _mapping(usage)
    if not usage:
        return None
    cached = _count(usage.get("cached_input_tokens"))
    # Codex counts cached tokens inside input_tokens; the fixture confirms it when cached <= input.
    return _usage(max(_count(usage.get("input_tokens")) - cached, 0), cached, 0, usage.get("output_tokens"), 0, usage)


def _codex_events(event: Mapping[str, Any], context: TurnContext) -> list[dict[str, Any]]:
    """Codex ``exec --json``: item events, then the completed turn's usage."""
    kind = str(event.get("type") or "")
    if kind == "turn.completed":
        return [_turn_end(_codex_usage(event.get("usage")))]
    item = _mapping(event.get("item"))
    item_type = str(item.get("type") or "")
    if not kind.startswith("item.") or not item_type:
        return []
    if item_type in ("agent_message", "agentMessage"):
        return _assistant_text(item.get("text"))
    call_id, done = str(item.get("id") or ""), kind == "item.completed"
    if item_type == "command_execution":
        code = item.get("exit_code") if done else None
        status = "running" if not done else ("completed" if code == 0 else "failed")
        return [_command(call_id, str(item.get("command") or ""), status=status, exit_code=code,
                         output=str(item.get("aggregated_output") or ""))]
    if item_type == "file_change":
        changes = item.get("changes") if done else None
        return [
            {"type": "file_change", "payload": {"call_id": call_id, "path": str(change.get("path") or ""),
             "kind": str(change.get("kind") or "update"), "diff": "", "added": 0, "removed": 0}}
            for change in changes or [] if isinstance(change, Mapping)
        ]
    if item_type not in _CODEX_TOOL_ITEMS:
        return []
    if done:
        output = item.get("aggregated_output") or item.get("output") or item.get("result")
        return [_tool_result(call_id, item_type, output, str(item.get("status") or "") == "failed")]
    return [_tool_call(call_id, item_type, item.get("arguments"))]
```

OpenCode (`run --format json`):

```python
def _opencode_usage(tokens: Any) -> dict[str, Any] | None:
    tokens = _mapping(tokens)
    if not tokens:
        return None
    cache = _mapping(tokens.get("cache"))
    return _usage(tokens.get("input"), cache.get("read"), cache.get("write"), tokens.get("output"), 0, tokens)


def _opencode_tool(part: Mapping[str, Any]) -> list[dict[str, Any]]:
    state = _mapping(part.get("state"))
    name = str(part.get("tool") or part.get("name") or state.get("title") or "tool")
    call_id = str(part.get("callID") or part.get("id") or "")
    status = str(state.get("status") or "")
    done, failed = status in ("completed", "error"), status == "error"
    request = _mapping(state.get("input") or part.get("input"))
    output = str(state.get("output") or "")
    if name == "bash":
        code = _mapping(state.get("metadata")).get("exit") if done else None
        shown = "running" if not done else ("failed" if failed or (isinstance(code, int) and code != 0) else "completed")
        return [_command(call_id, str(request.get("command") or ""), status=shown, exit_code=code, output=output)]
    if name in ("edit", "write") and status == "completed":
        path = str(request.get("filePath") or "")
        if name == "write":
            kind = "update" if _mapping(state.get("metadata")).get("exists") else "add"
            return [_file_change(call_id, path, [("", str(request.get("content") or ""))], kind)]
        return [_file_change(call_id, path, [(str(request.get("oldString") or ""), str(request.get("newString") or ""))], "update")]
    if done:
        return [_tool_result(call_id, name, state.get("output"), failed)]
    return [_tool_call(call_id, name, state.get("input") or part.get("input"))]


def _opencode_events(event: Mapping[str, Any], context: TurnContext) -> list[dict[str, Any]]:
    """OpenCode ``run --format json`` and part updates: text, thinking, tools, then step finish."""
    kind = str(event.get("type") or "").strip().lower()
    part = _mapping(event.get("part"))
    part_type = str(part.get("type") or "").strip().lower().replace("-", "_")
    if kind.endswith("part.updated") or kind.endswith("part_updated"):
        kind = part_type
    elif part_type and kind not in ("text", "reasoning", "tool", "step_finish", "step_start"):
        kind = part_type
    if kind in ("step_finish", "stepfinish"):
        return [_turn_end(_opencode_usage(part.get("tokens") or event.get("tokens")))]
    if kind == "text":
        return _assistant_text(part.get("text") or event.get("text"))
    if kind in ("reasoning", "thinking"):
        return _reasoning(part.get("text") or part.get("thinking") or event.get("text"))
    return _opencode_tool(part) if kind == "tool" else []
```

Dispatch:

```python
_TRANSLATORS: Mapping[str, Callable[[Mapping[str, Any], TurnContext], list[dict[str, Any]]]] = MappingProxyType(
    {"claude_cli": _claude_events, "codex_cli": _codex_events, "opencode_cli": _opencode_events}
)


def translate(backend_id: str, event: Any, context: TurnContext | None = None) -> list[dict[str, Any]]:
    """One provider event as loop events; an unrecognized one yields nothing at all.

    ``context`` pairs a tool call with its result across lines; one turn shares one.
    """
    if not isinstance(event, Mapping):
        return []
    failure = _provider_error(event)
    if failure is not None:
        return [failure]
    translator = _TRANSLATORS.get(backend_id)
    return translator(event, context if context is not None else TurnContext()) if translator is not None else []
```

In `CliManagerBackend._turn`, create one context per turn before the stdout loop and pass it:

```python
        context = TurnContext()
        try:
            for raw in process.stdout or ():
                ...
                yield from translate(self.backend_id, event, context)
```

In `manager_loop.py`:

```python
EVENT_TYPES = frozenset({"assistant_text", "reasoning", "tool_call", "tool_result", "command", "file_change", "turn_end", "error"})
```

and in `_record`: `event = {"at": self._clock(), "turn": turn, "type": kind, "v": 3, "payload": payload}`.

- [ ] **Step 4: Generate the expected lists, then review them against the fixtures by hand**

```bash
.venv/Scripts/python.exe - <<'EOF'
import json
from pathlib import Path
from aiworkhub import manager_loop_backends as mlb
root = Path("tests/fixtures/manager_streams")
for backend in ("claude_cli", "codex_cli", "opencode_cli"):
    context, events = mlb.TurnContext(), []
    for line in (root / f"{backend}.jsonl").read_text(encoding="utf-8").splitlines():
        events.extend(e for e in mlb.translate(backend, json.loads(line), context) if e["type"] != "delta")
    (root / f"{backend}.expected.json").write_text(json.dumps(events, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
EOF
```

Read each `*.expected.json` next to its fixture: every tool line maps to the right event, ids pair, usage numbers equal the provider's. Fix the translator, not the expected file, when they disagree.

- [ ] **Step 5: Update the existing assertions that v3 changes on purpose**, run all target and regression tests, lint, commit

```bash
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_stream_fixtures.py tests/test_manager_loop_stream.py tests/test_manager_loop.py tests/test_manager_loop_backends.py tests/test_manager_loop_service.py
.venv/Scripts/python.exe -m ruff check src/aiworkhub/manager_loop_backends.py src/aiworkhub/manager_loop.py tests/test_manager_stream_fixtures.py
git add src/aiworkhub/manager_loop_backends.py src/aiworkhub/manager_loop.py tests/test_manager_stream_fixtures.py tests/test_manager_loop_stream.py tests/test_manager_loop_backends.py tests/test_manager_loop.py tests/fixtures/manager_streams/*.expected.json
git commit -m "feat(console): v3 manager events with call ids, commands, file changes and normalized usage"
```

---

### Task W1-P2: streamed partials (Claude deltas → in-memory `partial`)

**Objective:** Turn Claude `stream_event` text/thinking deltas into transient `delta` items that fill an in-memory per-session `partial`, return it from `aiworkhub_manager_loop_events`, and never persist it.

**Files:**
- Modify: `src/aiworkhub/manager_loop_backends.py` (`_claude_events`)
- Modify: `src/aiworkhub/manager_loop.py` (`ManagerOrchestrator.__init__`, new `partial` property, `_exchange` ~1108–1136)
- Modify: `src/aiworkhub/manager_loop_service.py` (`events` ~530)
- Modify: `src/aiworkhub/server.py` (`aiworkhub_manager_loop_events` docstring ~1939)
- Test: `tests/test_manager_loop.py`, `tests/test_manager_loop_stream.py`, `tests/test_manager_loop_service.py`

**Interfaces:**
- Consumes: `TurnContext`, `translate` (W1-P1b).
- Produces: backend item `{"type": "delta", "payload": {"kind": "text" | "reasoning", "text": str}}` (never recorded); `ManagerOrchestrator.partial -> dict | None` shaped `{"session_id", "turn", "text", "reasoning", "command_output"}`; `manager_loop_service.events(...)` result gains `"partial": dict | None` (only for the requested session).

**Acceptance:**
- A turn streaming `"Hel"`, `"lo"` then the final `"Hello"` shows `partial.text == "Hello"` before the final event, `""` after it, and `partial is None` after the turn; the JSONL holds no `delta` type and no event count change.
- A partial from turn N is never returned for turn N+1 or for another session id.
- The orchestrator replaces `self._partial` with a new dict on every change (never mutates it), so the unlocked poll thread always reads a consistent snapshot.
- Deltas do not count toward `MAX_TURN_EVENTS`; a partial field is capped at 64 KB, keeping its tail.

**Validation:** `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_loop.py tests/test_manager_loop_stream.py` (target) and `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_loop_service.py tests/test_manager_stream_fixtures.py tests/test_manager_loop_backends.py` (regression).

- [ ] **Step 1: Write the failing tests**

In `tests/test_manager_loop_stream.py`:

```python
def test_claude_stream_deltas_become_transient_delta_items() -> None:
    text = {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}}}
    think = {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hm"}}}
    other = {"type": "stream_event", "event": {"type": "message_start"}}
    assert translate("claude_cli", text) == [{"type": "delta", "payload": {"kind": "text", "text": "Hel"}}]
    assert translate("claude_cli", think) == [{"type": "delta", "payload": {"kind": "reasoning", "text": "hm"}}]
    assert translate("claude_cli", other) == []
```

In `tests/test_manager_loop.py`:

```python
def test_deltas_fill_partial_and_never_reach_the_log(make) -> None:
    harness = make()
    session = harness.orch.start("fake", "model-a")
    seen: list[Any] = []

    def stream():
        yield {"type": "delta", "payload": {"kind": "text", "text": "Hel"}}
        yield {"type": "delta", "payload": {"kind": "text", "text": "lo"}}
        seen.append(harness.orch.partial)
        yield {"type": "assistant_text", "payload": {"text": "Hello"}}
        seen.append(harness.orch.partial)
        yield {"type": "turn_end", "payload": {}}

    harness.backends[0].script.append(stream())
    result = harness.orch.send("hi")

    assert seen[0]["text"] == "Hello" and seen[0]["turn"] == result["turn"]
    assert seen[0]["session_id"] == session.session_id
    assert seen[1]["text"] == ""
    assert harness.orch.partial is None
    logged = harness.store.events(session.session_id)
    assert "delta" not in {event["type"] for event in logged}
    assert [event["type"] for event in logged][-2:] == ["assistant_text", "turn_end"]


def test_a_partial_never_crosses_into_the_next_turn(make) -> None:
    harness = make()
    harness.orch.start("fake", "model-a")
    seen: list[Any] = []

    def first():
        yield {"type": "delta", "payload": {"kind": "reasoning", "text": "old"}}
        yield {"type": "turn_end", "payload": {}}

    def second():
        seen.append(harness.orch.partial)
        yield {"type": "delta", "payload": {"kind": "text", "text": "new"}}
        seen.append(harness.orch.partial)
        yield {"type": "turn_end", "payload": {}}

    harness.backends[0].script.extend([first(), second()])
    harness.orch.send("one")
    harness.orch.send("two")

    assert seen[0] is None
    assert seen[1]["reasoning"] == "" and seen[1]["text"] == "new"
```

(`start` returns the session object; if the existing tests read the id differently, follow them — see `harness.orch.start("fake", "model-a")` uses around line 176.)

In `tests/test_manager_loop_service.py` (same fakes as the file's existing turn tests):

```python
def test_events_carry_a_null_partial_once_the_turn_is_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch)
    manager_loop_service.start(tmp_path, "fake", "model-a")
    manager_loop_service.send(tmp_path, "one")
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    session_id = manager_loop_service.status(tmp_path)["session"]["session_id"]

    result = manager_loop_service.events(tmp_path, session_id)

    assert "partial" in result
    assert result["partial"] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_loop.py tests/test_manager_loop_stream.py -k "delta or partial"`
Expected: FAIL — `translate` returns `[]` for `stream_event`; `ManagerOrchestrator` has no `partial`; the fake `delta` item is recorded as `unknown_event_type:delta`.

- [ ] **Step 3: Implement**

`manager_loop_backends.py`:

```python
def _delta(kind: str, text: Any) -> list[dict[str, Any]]:
    """A streamed fragment for display only; the orchestrator never records it."""
    text = str(text or "")
    return [{"type": "delta", "payload": {"kind": kind, "text": text}}] if text else []


def _claude_delta(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    inner = _mapping(event.get("event"))
    if inner.get("type") != "content_block_delta":
        return []
    delta = _mapping(inner.get("delta"))
    if delta.get("type") == "text_delta":
        return _delta("text", delta.get("text"))
    if delta.get("type") == "thinking_delta":
        return _delta("reasoning", delta.get("thinking"))
    return []
```

and at the top of `_claude_events`: `if str(event.get("type") or "") == "stream_event": return _claude_delta(event)`.

`manager_loop.py` — constants and a helper:

```python
PARTIAL_FIELD_BYTES = 64 * 1024
_PARTIAL_FIELDS = ("text", "reasoning", "command_output")
_SETTLES = {"assistant_text": "text", "reasoning": "reasoning", "command": "command_output"}


def _keep_tail(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    return text if len(data) <= limit else data[-limit:].decode("utf-8", "ignore")
```

In `ManagerOrchestrator.__init__`: `self._partial: dict[str, Any] | None = None`. Add:

```python
    @property
    def partial(self) -> dict[str, Any] | None:
        """The running turn's streamed, unfinished text; display only, never persisted."""
        return self._partial

    def _grow_partial(self, session: ManagerSession, turn: int, payload: Any) -> None:
        payload = payload if isinstance(payload, Mapping) else {}
        kind = str(payload.get("kind") or "")
        if kind not in _PARTIAL_FIELDS:
            return
        current = self._partial
        if current is None or current["turn"] != turn or current["session_id"] != session.session_id:
            current = {"session_id": session.session_id, "turn": turn, **{field: "" for field in _PARTIAL_FIELDS}}
        # ponytail: tail-only cap per field; the final event carries the full text.
        grown = _keep_tail(current[kind] + str(payload.get("text") or ""), PARTIAL_FIELD_BYTES)
        self._partial = {**current, kind: grown}  # replaced, never mutated: the poll reads it unlocked

    def _settle_partial(self, kind: str) -> None:
        field = _SETTLES.get(kind)
        if field and self._partial and self._partial[field]:
            self._partial = {**self._partial, field: ""}
```

In `_exchange`, first thing inside the loop, and a `finally`:

```python
        try:
            for raw in backend.send(message):
                if isinstance(raw, Mapping) and raw.get("type") == "delta":
                    self._grow_partial(session, turn, raw.get("payload"))
                    continue
                if len(events) >= MAX_TURN_EVENTS:
                    ...
                kind, payload = _normalize(raw)
                self._settle_partial(kind)
                ...
        except Exception as exc:  # noqa: BLE001 - a failed turn is an error event, not a dead session
            ...
        finally:
            self._partial = None
        return events, grown, "".join(texts)
```

`manager_loop_service.events` — add to the returned mapping:

```python
    partial = entry.orchestrator.partial
    ...
    return {"ok": True, "events": filtered, "partial": partial if partial and partial.get("session_id") == session_id else None}
```

`server.py` docstring:

```python
    """MANAGER READ: bounded session events, for incremental client polling.

    Events with seq greater than after_seq, bounded to limit, plus ``partial``:
    the running turn's streamed text (display only, never persisted), or null.
    """
```

- [ ] **Step 4: Run target and regression tests, lint, commit**

```bash
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_manager_loop.py tests/test_manager_loop_stream.py tests/test_manager_loop_service.py tests/test_manager_stream_fixtures.py tests/test_manager_loop_backends.py
.venv/Scripts/python.exe -m ruff check src/aiworkhub/manager_loop.py src/aiworkhub/manager_loop_backends.py src/aiworkhub/manager_loop_service.py src/aiworkhub/server.py
git add src/aiworkhub/manager_loop.py src/aiworkhub/manager_loop_backends.py src/aiworkhub/manager_loop_service.py src/aiworkhub/server.py tests/test_manager_loop.py tests/test_manager_loop_stream.py tests/test_manager_loop_service.py
git commit -m "feat(console): stream Claude deltas into an in-memory partial"
```

The W1-P1b fixture test and its generator already drop `delta` items, so a Claude fixture with `stream_event` lines keeps its expected list unchanged.

---

### Task W1-U1: extract the console renderers (no behaviour change)

**Objective:** Move the manager chat block renderers and their styles out of `app.js` / `app.css` into `media/manager_console.js` / `media/manager_console.css`, loaded by the webview before `app.js`, with no visible or behavioural change.

**Files:**
- Create: `vscode-extension/media/manager_console.js`, `vscode-extension/media/manager_console.css`
- Modify: `vscode-extension/media/app.js` (remove lines 6972–7134: the renderer comment header through `managerChatTurnEndNode`; remove `renderManagerChatEvents` 7245–7289)
- Modify: `vscode-extension/media/app.css` (remove 3275 `.manager-chat-bubble {` through the end of `.manager-chat-error { … }` ≈3413, and the line `.manager-chat-bubble { max-width: 100%; }` inside `@media (max-width: 620px)` ≈3698)
- Modify: `vscode-extension/extension.js` (`getHtmlForWebview` ~11200: asset URIs, `<link>`, `<script>`)
- Modify: `vscode-extension/test/package-vsix.js` (file allowlist ~512)
- Test: `vscode-extension/test/manager-chat-panel.test.js`, `vscode-extension/test/context-viewers.test.js`

**Interfaces:**
- Produces: `manager_console.js` — a classic script (no module syntax) declaring, unchanged, `managerChatEventNode`, `managerChatFormatDuration`, `managerChatEventTime`, `managerChatThoughtSummary`, `managerChatToolHint`, `appendManagerChatToolFields`, `managerChatLiveThinkingNode`, `managerChatTurnCallCount`, `managerChatTurnEndNode`, `renderManagerChatEvents`. It uses `app.js` globals (`createElement`, `state`, `elements`, `document`) only at call time.
- Load order in the webview: `app.css`, then `manager_console.css`; inline foundation script, then `manager_console.js`, then `app.js`.

**Acceptance:**
- The moved code is byte-identical apart from its new file (diff shows a pure move).
- `npm test` passes; every assertion that read the moved code from `appSource`/`cssSource` reads it from the new files instead, with the same expectations.
- No hex colour literal in `manager_console.css`; the narrow-width `max-width: 100%` override still wins over `max-width: 85%` (it moves with the bubble rule, after it, inside the same `@media`).
- The VSIX contains both new files (`npm run package`, then list the archive).

**Validation:** `node --test test/manager-chat-panel.test.js` (target) and `npm test` (regression), both from `vscode-extension/`.

- [ ] **Step 1: Point the tests at the new files first** (they fail until the move happens)

In `manager-chat-panel.test.js`:

```js
const consoleSource = fs.readFileSync(path.join(__dirname, "..", "media", "manager_console.js"), "utf8");
const consoleCss = fs.readFileSync(path.join(__dirname, "..", "media", "manager_console.css"), "utf8");
```

In `loadWebviewSlice`, change the `managerChat` slice's start marker from `"function managerChatEventNode(event) {"` to `"function managerChatTaskStatus(task) {"` (the first manager chat function that stays in `app.js`), and evaluate the console file before it:

```js
  vm.runInContext(
    `"use strict";\n${utilities}\n${constants}\n${consoleSource}\n${managerChat}\n${managerChatWiring}\n` +
      "this.api = { … unchanged … };",
    context,
  );
```

Split the CSS tests: `#manager-chat-model` field-sizing stays on the `app.css` slice; the `.manager-chat-tool-row > summary` / `.manager-chat-tool-row-body` / no-ellipsis / no-line-clamp assertions read `consoleCss`; the no-hex assertion runs on both the `app.css` slice and all of `consoleCss`. Add:

```js
test("manager console assets load before app.js and ship in the VSIX", () => {
  const html = extensionSource.slice(extensionSource.indexOf("function getHtmlForWebview("));
  const consoleScript = html.indexOf('src="${consoleScriptUri}"');
  const appScript = html.indexOf('src="${scriptUri}"');
  assert.ok(consoleScript !== -1 && consoleScript < appScript, "console script must load before app.js");
  assert.ok(html.indexOf('href="${styleUri}"') < html.indexOf('href="${consoleStyleUri}"'), "console css loads after app.css");
  const packager = fs.readFileSync(path.join(__dirname, "package-vsix.js"), "utf8");
  assert.match(packager, /"media\/manager_console\.js"/);
  assert.match(packager, /"media\/manager_console\.css"/);
  assert.doesNotMatch(appSource, /function managerChatEventNode\(/);
  assert.doesNotMatch(cssSource, /\.manager-chat-bubble \{/);
});
```

- [ ] **Step 2: Run to verify it fails**

Run (in `vscode-extension/`): `node --test test/manager-chat-panel.test.js`
Expected: FAIL — `ENOENT … manager_console.js`.

- [ ] **Step 3: Move the code**

1. Create `media/manager_console.js` starting with:

```js
"use strict";

// Manager console block renderers (spec 2026-09-26 §3). Loaded before app.js;
// every app.js global these use (createElement, state, elements) is read at
// call time, never at load time.
```

   then cut app.js lines 6972–7134 and 7245–7289 and paste them below, in that order, unchanged.
2. Create `media/manager_console.css`; cut app.css from `.manager-chat-bubble {` through the closing brace of `.manager-chat-error { … }` (keyframes and the reduced-motion block come with it), paste unchanged, then append:

```css
@media (max-width: 620px) {
  .manager-chat-bubble { max-width: 100%; }
}
```

   and delete that one line from app.css's `@media (max-width: 620px)` block.
3. `extension.js` `getHtmlForWebview`:

```js
  const consoleScriptUri = dashboardAssetUri(webview, mediaUri, "manager_console.js");
  const consoleStyleUri = dashboardAssetUri(webview, mediaUri, "manager_console.css");
```

```html
<link rel="stylesheet" href="${styleUri}">
<link rel="stylesheet" href="${consoleStyleUri}">
```

```html
  <script nonce="${nonceValue}">${codingFoundationDashboardSource()}</script>
  <script nonce="${nonceValue}" src="${consoleScriptUri}"></script>
  <script nonce="${nonceValue}" src="${scriptUri}"></script>
```

4. `test/package-vsix.js`: add `"media/manager_console.js",` and `"media/manager_console.css",` after `"media/app.css",`.
5. `test/context-viewers.test.js` runs the whole `app.js` in a vm (line ~470). Mirror the webview's load order there: directly before `vm.runInContext(app, context, { filename: "app.js" });` add `vm.runInContext(fs.readFileSync(path.join(root, "media", "manager_console.js"), "utf8"), context, { filename: "manager_console.js" });`.

- [ ] **Step 4: Run the tests**

Run (in `vscode-extension/`): `node --test test/manager-chat-panel.test.js`, then `npm test`
Expected: all pass.

- [ ] **Step 5: Package check and commit**

```bash
cd vscode-extension && npm run package && cd ..
git add vscode-extension/media/manager_console.js vscode-extension/media/manager_console.css vscode-extension/media/app.js vscode-extension/media/app.css vscode-extension/extension.js vscode-extension/test/package-vsix.js vscode-extension/test/manager-chat-panel.test.js vscode-extension/test/context-viewers.test.js
git commit -m "refactor(console): move manager chat renderers to manager_console.js/.css"
```

---

### Task W1-U2: glyph-gutter blocks, token footer, context hairline

**Objective:** Render every event as a glyph-gutter block per spec §3 — commands merged by `call_id` with collapsible output, file changes as coloured unified diffs, task/callback/error blocks, the `turn · tools · in · cache · out · time` footer for normalized and legacy usage — and fill the 2 px context hairline from the last `turn_end`.

**Files:**
- Modify: `vscode-extension/media/manager_console.js`, `vscode-extension/media/manager_console.css`
- Modify: `vscode-extension/extension.js` (hairline element after `#manager-chat-session-line`, ~11723–11732)
- Modify: `vscode-extension/media/app.js` (`elements` map ~341–351: add `managerChatHairline`, `managerChatContext`)
- Test: `vscode-extension/test/manager-console.test.js` (new), `vscode-extension/test/manager-chat-panel.test.js`

**Interfaces:**
- Consumes: v3 payloads (W1-P1b), `createElement`, `numberValue`, `TASK_ID_RE`, `requestTaskDetail(taskId)`, `managerChatFormatDuration`, `state.managerChatEvents`.
- Produces (in `manager_console.js`): `MANAGER_CONSOLE_GLYPHS`, `managerConsoleBlock(kind, body) -> Element`, `managerConsoleMergeCommands(events) -> events`, `managerConsoleCompact(n) -> string`, `managerConsoleUsage(usage) -> {input, cache, output} | null`, `managerConsoleFooterText(event) -> string`, `managerConsoleTaskOf(payload) -> {id, status, title} | null`, `managerConsoleContextFill(events) -> number | null`, `managerConsoleApplyHairline()`.

**Acceptance:**
- Each v3 type renders its §3 block; old events (no `v`) render with the same text as before under the new gutter.
- Two `command` events with one `call_id` render one block, at the first one's position, with the last one's status and output.
- Output over 20 lines shows the last 20 plus "show all (N lines)"; a diff over 40 lines shows the first 40 plus the same control; a `truncated` payload says so.
- Footer: `turn 7 · 12 tools · in 3.1k · cache 88k · out 1.2k · 41s` for normalized usage; the legacy keys give the same numbers; no usage → `turn 7 · 12 tools · 41s`.
- Hairline width = last reported `context_fill`; class `is-stale` at ≥ 0.60, `is-blocked` at ≥ 0.75; hidden when no fill is known.
- A hostile string in text, command, output, path, diff, task title or error renders as text: no element other than the renderer's own tags appears.

**Validation:** `node --test test/manager-console.test.js` (target) and `node --test test/manager-chat-panel.test.js` plus `npm test` (regression), from `vscode-extension/`.

- [ ] **Step 1: Write the failing tests** (`test/manager-console.test.js`)

```js
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.js"), "utf8");
const consoleSource = fs.readFileSync(path.join(__dirname, "..", "media", "manager_console.js"), "utf8");

function slice(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from);
  assert.ok(from !== -1 && to !== -1, `slice ${start}`);
  return source.slice(from, to + end.length);
}

function fakeElement(tag) {
  return {
    tag, className: "", textContent: "", children: [], attrs: {}, listeners: {}, style: {}, hidden: false,
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    classList: { values: new Set(), toggle(name, on) { on ? this.values.add(name) : this.values.delete(name); } },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...nodes) { this.children = nodes.length === 1 && nodes[0].__isFragment ? nodes[0].children.slice() : nodes; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    remove() { this.removed = true; },
  };
}

function load(events = []) {
  const elements = { managerChatTranscript: fakeElement("div"), managerChatHairline: fakeElement("div"), managerChatContext: fakeElement("span") };
  elements.managerChatHairline.children.push(fakeElement("span"));
  const opened = [];
  const context = {
    document: {
      createElement: (tag) => fakeElement(tag),
      createTextNode: (text) => ({ tag: "#text", textContent: String(text), children: [] }),
      createDocumentFragment: () => ({ __isFragment: true, children: [], appendChild(child) { this.children.push(child); return child; } }),
    },
    state: { managerChatEvents: events, managerChatSession: "mls-fixture", managerChatRunning: false },
    elements,
    requestTaskDetail: (id) => opened.push(id),
    Date,
  };
  vm.createContext(context);
  // The real app.js utilities and TASK_ID_RE, sliced by their own first and last lines.
  const utilities = [
    slice(appSource, "const TASK_ID_RE = ", ";\n"),
    slice(appSource, "function createElement(tag, className, text) {", "return element;\n}"),
    slice(appSource, "function asArray(value) {", "return Array.isArray(value) ? value : [];\n}"),
    slice(appSource, "function numberValue(value) {", "return Number.isFinite(parsed) ? parsed : 0;\n}"),
    slice(appSource, "function limitText(value, maxLength = 120) {", "return `${text.slice(0, Math.max(0, maxLength - 1)).trimEnd()}...`;\n}"),
  ].join("\n");
  vm.runInContext(`${utilities}\n${consoleSource}\nthis.api = { managerChatEventNode, renderManagerChatEvents, managerConsoleMergeCommands, managerConsoleCompact, managerConsoleFooterText, managerConsoleTaskOf, managerConsoleApplyHairline };`, context);
  return { api: context.api, elements, opened, state: context.state };
}

function flat(node, out = []) {
  if (!node) return out;
  out.push(node);
  for (const child of node.children || []) flat(child, out);
  return out;
}

const text = (node) => flat(node).map((item) => item.children && item.children.length ? "" : String(item.textContent || "")).join("");

test("commands merge by call id at the first position with the last payload", () => {
  const { api } = load();
  const merged = api.managerConsoleMergeCommands([
    { seq: 1, type: "command", payload: { call_id: "c1", command: "git --version", status: "running" } },
    { seq: 2, type: "assistant_text", payload: { text: "hi" } },
    { seq: 3, type: "command", payload: { call_id: "c1", command: "git --version", status: "completed", exit_code: 0, output_tail: "git version 2.46" } },
  ]);
  assert.deepEqual(merged.map((e) => e.seq), [3, 2]);
  assert.equal(merged[0].payload.status, "completed");
});

test("long command output collapses to its last 20 lines", () => {
  const { api } = load();
  const lines = Array.from({ length: 30 }, (_, i) => `line ${i + 1}`).join("\n");
  const node = api.managerChatEventNode({ type: "command", payload: { call_id: "c", command: "seq 30", status: "completed", exit_code: 0, output_tail: lines } });
  const all = flat(node);
  const pre = all.find((n) => n.tag === "pre");
  assert.ok(pre.textContent.startsWith("line 11") && pre.textContent.endsWith("line 30"));
  const more = all.find((n) => n.tag === "button");
  assert.equal(more.textContent, "show all (30 lines)");
  more.listeners.click[0]();
  assert.ok(pre.textContent.startsWith("line 1\n"));
});

test("a file change renders a coloured diff and collapses beyond 40 lines", () => {
  const { api } = load();
  const diff = ["--- a/x", "+++ b/x", "@@ -1 +1 @@", "-old", "+new", ...Array.from({ length: 40 }, (_, i) => ` ctx ${i}`)].join("\n");
  const node = api.managerChatEventNode({ type: "file_change", payload: { call_id: "e", path: "src/x.py", kind: "update", diff, added: 1, removed: 1 } });
  const all = flat(node);
  assert.ok(text(node).includes("src/x.py (+1 −1)"));
  assert.ok(all.some((n) => n.className === "mc-add" && n.textContent === "+new\n"));
  assert.ok(all.some((n) => n.className === "mc-del" && n.textContent === "-old\n"));
  assert.equal(all.filter((n) => /^mc-(add|del|hunk|ctx)$/.test(n.className)).length, 40);
});

test("footer text handles normalized, legacy and missing usage", () => {
  const events = [
    { seq: 1, turn: 7, at: "2026-09-27T10:00:00Z", type: "user_message", payload: { text: "go" } },
    ...Array.from({ length: 12 }, (_, i) => ({ seq: 2 + i, turn: 7, at: "2026-09-27T10:00:10Z", type: "tool_call", payload: { call_id: `t${i}`, name: "Read" } })),
  ];
  const { api } = load(events);
  const end = (usage) => ({ seq: 99, turn: 7, at: "2026-09-27T10:00:41Z", type: "turn_end", payload: usage ? { usage } : {} });
  assert.equal(api.managerConsoleFooterText(end({ input: 3100, cache_read: 80000, cache_write: 8000, output: 1200 })), "turn 7 · 12 tools · in 3.1k · cache 88k · out 1.2k · 41s");
  assert.equal(api.managerConsoleFooterText(end({ input_tokens: 3100, cache_read_input_tokens: 80000, cache_creation_input_tokens: 8000, output_tokens: 1200 })), "turn 7 · 12 tools · in 3.1k · cache 88k · out 1.2k · 41s");
  assert.equal(api.managerConsoleFooterText(end(null)), "turn 7 · 12 tools · 41s");
  assert.equal(api.managerConsoleCompact(950), "950");
  assert.equal(api.managerConsoleCompact(1_250_000), "1.3M");
});

test("a task id in a tool result becomes a task block that opens the task", () => {
  const { api, opened } = load();
  const output = [{ type: "text", text: JSON.stringify({ task_id: "T-2026-00042", status: "review", title: "Fix <b>it</b>" }) }];
  const node = api.managerChatEventNode({ type: "tool_result", payload: { call_id: "m", name: "aiworkhub_task_show", output, is_error: false } });
  assert.ok(text(node).includes("T-2026-00042"));
  assert.ok(text(node).includes("Fix <b>it</b>"));
  flat(node).find((n) => n.tag === "button").listeners.click[0]();
  assert.deepEqual(opened, ["T-2026-00042"]);
});

test("the hairline follows the last reported context fill", () => {
  const { api, elements, state } = load();
  state.managerChatEvents = [{ seq: 1, turn: 1, type: "turn_end", payload: { usage: { input: 1, cache_read: 0, cache_write: 0, output: 1, context_window: 100, context_fill: 0.8 } } }];
  api.managerConsoleApplyHairline();
  assert.equal(elements.managerChatHairline.children[0].style.width, "80%");
  assert.ok(elements.managerChatHairline.classList.values.has("is-blocked"));
  assert.equal(elements.managerChatContext.textContent, "80%");
  state.managerChatEvents = [];
  api.managerConsoleApplyHairline();
  assert.equal(elements.managerChatHairline.hidden, true);
});

test("hostile payloads render as text in every block", () => {
  const { api } = load();
  const evil = "<img src=x onerror=alert(1)>";
  const nodes = [
    { type: "assistant_text", payload: { text: evil } },
    { type: "command", payload: { call_id: "c", command: evil, status: "failed", exit_code: 1, output_tail: evil } },
    { type: "file_change", payload: { call_id: "f", path: evil, kind: "update", diff: `+${evil}`, added: 1, removed: 0 } },
    { type: "error", payload: { source: evil, error: evil } },
    { type: "tool_result", payload: { call_id: "t", name: evil, output: JSON.stringify({ task_id: "T-1", title: evil }), is_error: false } },
  ].map((event) => api.managerChatEventNode(event));
  for (const node of nodes) {
    assert.ok(!flat(node).some((n) => n.tag === "img"), "no element from payload");
    assert.ok(text(node).includes(evil));
  }
});
```

Also update `manager-chat-panel.test.js`, exactly these assertions (every other one stays):

| Line | Old | New |
|---|---|---|
| 499–500 | `/Turn completed/`, `/in 1,234/` | `/^turn · 0 tools/`, `/in 1\.2k/` (`/out 567/` stays) |
| 509 | `"Turn completed · 0 tool calls"` | `"turn · 0 tools"` |
| 521 | `"Turn 4 completed · 2 tool calls"` | `"turn 4 · 2 tools"` |
| 534 | `assert.equal(node.tag, "details", …)` | `assert.ok(allNodes.some((item) => item.tag === "details"), "reasoning collapses like tool rows")` (the block wraps the `details`) |
| 553 | `"Turn completed · 0 tool calls"` | `"turn · 0 tools"` (hostile usage yields no counts at all) |

- [ ] **Step 2: Run to verify it fails**

Run (in `vscode-extension/`): `node --test test/manager-console.test.js`
Expected: FAIL — `ReferenceError: managerConsoleMergeCommands is not defined`.

- [ ] **Step 3: Implement in `manager_console.js`**

```js
// Glyph per block kind; the gutter is fixed-width so blocks never shift.
const MANAGER_CONSOLE_GLYPHS = Object.freeze({
  user_message: "›", assistant_text: "●", reasoning: "∴", command: "$", file_change: "±",
  tool_call: "⚙", tool_result: "⚙", task: "▣", callback: "↯", goal: "◎", error: "!",
});
const MANAGER_CONSOLE_COMMAND_LINES = 20;
const MANAGER_CONSOLE_DIFF_LINES = 40;

function managerConsoleBlock(kind, body) {
  const block = createElement("div", "mc-block mc-" + kind.replace(/_/g, "-"));
  const gutter = createElement("span", "mc-gutter", MANAGER_CONSOLE_GLYPHS[kind] || "");
  gutter.setAttribute("aria-hidden", "true");
  block.appendChild(gutter);
  block.appendChild(body);
  return block;
}

function managerConsoleShowAll(label, onClick) {
  const button = createElement("button", "mc-show-all", label);
  button.type = "button";
  button.addEventListener("click", () => { onClick(); button.remove(); });
  return button;
}

function managerConsoleMergeCommands(events) {
  const latest = new Map();
  for (const event of events) {
    const id = event && event.type === "command" && event.payload && event.payload.call_id;
    if (id) latest.set(id, event);
  }
  const shown = new Set();
  const merged = [];
  for (const event of events) {
    const id = event && event.type === "command" && event.payload && event.payload.call_id;
    if (!id) { merged.push(event); continue; }
    if (shown.has(id)) continue;
    shown.add(id);
    merged.push(latest.get(id));
  }
  return merged;
}

function managerConsoleCommandChip(payload) {
  if (payload.status === "running") return createElement("span", "mc-chip is-running", "running");
  const code = Number.isInteger(payload.exit_code) ? payload.exit_code : null;
  const failed = payload.status === "failed" || (code !== null && code !== 0);
  const label = code !== null ? "exit " + code : failed ? "failed" : "done";
  return createElement("span", failed ? "mc-chip is-failed" : "mc-chip is-ok", label);
}

function managerConsoleCommandNode(payload) {
  const body = createElement("div", "mc-body");
  const head = createElement("div", "mc-head");
  head.appendChild(createElement("code", "mc-mono", String(payload.command || "")));
  head.appendChild(managerConsoleCommandChip(payload));
  body.appendChild(head);
  const lines = String(payload.output_tail || "").split("\n");
  if (payload.output_tail) {
    const pre = createElement("pre", "mc-output", lines.slice(-MANAGER_CONSOLE_COMMAND_LINES).join("\n"));
    if (lines.length > MANAGER_CONSOLE_COMMAND_LINES) {
      body.appendChild(managerConsoleShowAll(`show all (${lines.length} lines)`, () => { pre.textContent = lines.join("\n"); }));
    }
    body.appendChild(pre);
  }
  if (payload.truncated) body.appendChild(createElement("div", "mc-note", `output cut, ${numberValue(payload.original_bytes)} bytes in total`));
  return managerConsoleBlock("command", body);
}

function managerConsoleDiffNode(diff) {
  const lines = String(diff || "").split("\n");
  const pre = createElement("pre", "mc-diff");
  const paint = (count) => {
    const fragment = document.createDocumentFragment();
    for (const line of lines.slice(0, count)) {
      const kind = line.startsWith("@@") ? "mc-hunk"
        : line.startsWith("+") && !line.startsWith("+++") ? "mc-add"
        : line.startsWith("-") && !line.startsWith("---") ? "mc-del" : "mc-ctx";
      fragment.appendChild(createElement("span", kind, line + "\n"));
    }
    pre.replaceChildren(fragment);
  };
  paint(MANAGER_CONSOLE_DIFF_LINES);
  if (lines.length <= MANAGER_CONSOLE_DIFF_LINES) return pre;
  const wrap = createElement("div", "mc-collapsible");
  wrap.appendChild(managerConsoleShowAll(`show all (${lines.length} lines)`, () => paint(lines.length)));
  wrap.appendChild(pre);
  return wrap;
}

function managerConsoleFileChangeNode(payload) {
  const body = createElement("div", "mc-body");
  const head = createElement("div", "mc-head");
  head.appendChild(createElement("code", "mc-mono", String(payload.path || "")));
  head.appendChild(createElement("span", "mc-counts", ` (+${numberValue(payload.added)} −${numberValue(payload.removed)})`));
  body.appendChild(head);
  if (payload.diff) body.appendChild(managerConsoleDiffNode(payload.diff));
  if (payload.truncated) body.appendChild(createElement("div", "mc-note", `diff cut, ${numberValue(payload.original_bytes)} bytes in total`));
  return managerConsoleBlock("file_change", body);
}

function managerConsoleTaskFields(value) {
  let data = value;
  if (Array.isArray(data)) data = data.map((block) => (block && typeof block.text === "string" ? block.text : "")).join("");
  if (typeof data === "string") {
    if (data.length > 16384) return null;
    try { data = JSON.parse(data); } catch (_error) { return null; }
  }
  if (!data || typeof data !== "object") return null;
  const task = data.task && typeof data.task === "object" ? data.task : data;
  const id = task.task_id;
  if (typeof id !== "string" || !TASK_ID_RE.test(id)) return null;
  return { id, status: typeof task.status === "string" ? task.status : "", title: typeof task.title === "string" ? task.title : "" };
}

function managerConsoleTaskOf(payload) {
  if (!payload) return null;
  return managerConsoleTaskFields(payload.output) || managerConsoleTaskFields(payload.input);
}

function managerConsoleTaskNode(task, detail) {
  const body = createElement("div", "mc-body");
  const open = createElement("button", "mc-task-link mc-mono", task.id);
  open.type = "button";
  open.addEventListener("click", () => requestTaskDetail(task.id));
  body.appendChild(open);
  if (task.status) body.appendChild(createElement("span", "status-badge " + task.status, task.status));
  if (task.title) body.appendChild(createElement("span", "mc-task-title", task.title));
  if (detail) body.appendChild(detail);
  return managerConsoleBlock("task", body);
}

function managerConsoleCompact(value) {
  const n = numberValue(value);
  if (n < 1000) return String(n);
  if (n < 1000000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
}

function managerConsoleCount(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

// Normalized v3 usage, or the legacy provider keys; a hostile or empty usage gives no counts at all.
function managerConsoleUsage(usage) {
  if (!usage || typeof usage !== "object" || Array.isArray(usage)) return null;
  const legacy = !("input" in usage || "output" in usage);
  const pick = (key, legacyKey) => managerConsoleCount(legacy ? usage[legacyKey] : usage[key]);
  const input = pick("input", "input_tokens");
  const output = pick("output", "output_tokens");
  if (input === null && output === null) return null;
  const cache = (pick("cache_read", "cache_read_input_tokens") || 0) + (pick("cache_write", "cache_creation_input_tokens") || 0);
  return { input: input || 0, cache, output: output || 0 };
}

function managerConsoleTurnTools(turn) {
  if (!Number.isInteger(turn)) return 0;
  const ids = new Set();
  for (const item of state.managerChatEvents || []) {
    if (!item || item.turn !== turn || !["tool_call", "command", "file_change"].includes(item.type)) continue;
    ids.add((item.payload && item.payload.call_id) || "seq:" + item.seq);
  }
  return ids.size;
}

function managerConsoleFooterText(event) {
  const turn = event && event.turn;
  const tools = managerConsoleTurnTools(turn);
  const parts = [Number.isInteger(turn) ? `turn ${turn}` : "turn", `${tools} tool${tools === 1 ? "" : "s"}`];
  const usage = managerConsoleUsage(event && event.payload && event.payload.usage);
  if (usage) parts.push(`in ${managerConsoleCompact(usage.input)}`, `cache ${managerConsoleCompact(usage.cache)}`, `out ${managerConsoleCompact(usage.output)}`);
  const first = Number.isInteger(turn) ? (state.managerChatEvents || []).find((item) => item && item.turn === turn) : null;
  // managerChatEventTime is 0 without an `at`; no duration is better than a made-up one.
  const start = first ? managerChatEventTime(first) : 0;
  const end = managerChatEventTime(event);
  if (start > 0 && end > 0 && end >= start) parts.push(managerChatFormatDuration(end - start));
  return parts.join(" · ");
}

function managerConsoleContextFill(events) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const usage = events[index] && events[index].type === "turn_end" && events[index].payload && events[index].payload.usage;
    if (usage && typeof usage.context_fill === "number" && Number.isFinite(usage.context_fill)) return usage.context_fill;
  }
  return null;
}

function managerConsoleApplyHairline() {
  const line = elements.managerChatHairline;
  if (!line) return;
  const fill = managerConsoleContextFill(state.managerChatEvents || []);
  line.hidden = fill === null;
  if (elements.managerChatContext) elements.managerChatContext.textContent = fill === null ? "" : Math.round(fill * 100) + "%";
  if (fill === null) return;
  const percent = Math.min(100, Math.round(fill * 100));
  const bar = line.firstElementChild || line.children[0]; // the DOM has firstElementChild, the test fake has children
  bar.style.width = percent + "%";
  line.setAttribute("aria-valuenow", String(percent));
  line.classList.toggle("is-stale", fill >= 0.6 && fill < 0.75);
  line.classList.toggle("is-blocked", fill >= 0.75);
}
```

Wire them into the existing renderer:

- `managerChatEventNode`: at the top, `if (event && event.type === "command") return managerConsoleCommandNode(event.payload || {});`, the same for `file_change`; for `tool_result`, `const task = managerConsoleTaskOf(payload); if (task) return managerConsoleTaskNode(task, <the existing generic tool details node>);`; the `turn_end` branch returns `managerConsoleBlock("turn_end", createElement("div", "mc-footer", managerConsoleFooterText(event)))` with an empty gutter glyph; every other existing branch wraps its current node with `managerConsoleBlock(<type>, node)`.
- `renderManagerChatEvents`: iterate `managerConsoleMergeCommands(state.managerChatEvents)` instead of `state.managerChatEvents`, and call `managerConsoleApplyHairline()` at the end.
- The bubble label that `managerChatEventNode` builds (moved from app.js line 6981 by W1-U1) gets class `"manager-chat-bubble-label sr-only"`: the gutter glyph shows the role to the eye and is `aria-hidden`, so the label stays for screen readers only, reusing `app.css`'s existing `.sr-only`. The "Queued" label in `app.js` (~7525) stays visible.
- Delete `managerChatTurnCallCount` and `managerChatTurnEndNode` (replaced); the development-rules duplicate gate must stay green.

- [ ] **Step 4: Styles (`manager_console.css`) and header markup**

```css
.mc-block { display: grid; grid-template-columns: 2ch minmax(0, 1fr); column-gap: 6px; margin: 0 0 6px; }
.mc-gutter { color: var(--ink-soft); font-family: var(--vscode-editor-font-family); text-align: center; user-select: none; }
.mc-body { min-width: 0; }
.mc-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px; }
.mc-mono, .mc-block pre { font-family: var(--vscode-editor-font-family); font-size: var(--fs-2xs); }
.mc-block pre { margin: 4px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.mc-chip { color: var(--ink-soft); font-size: var(--fs-2xs); }
.mc-chip.is-ok { color: var(--review-ink); }
.mc-chip.is-failed { color: var(--blocked-ink); }
.mc-diff > span { display: block; }
.mc-diff .mc-add { background: var(--review-soft); }
.mc-diff .mc-del { background: var(--blocked-soft); }
.mc-diff .mc-hunk { color: var(--ink-soft); }
.mc-show-all { margin-top: 4px; font-size: var(--fs-2xs); }
.mc-note, .mc-footer { color: var(--muted); font-size: var(--fs-2xs); }
.mc-error .mc-gutter, .mc-error .mc-body { color: var(--blocked-ink); }
.mc-task-link { background: none; border: 0; padding: 0; color: var(--ink); cursor: pointer; text-decoration: underline; }
.mc-task-link:focus-visible, .mc-show-all:focus-visible { outline: 1px solid var(--accent); }
.mc-hairline { height: 2px; background: var(--line); }
.mc-hairline > span { display: block; height: 100%; width: 0; background: var(--ink-soft); transition: width 200ms ease; }
.mc-hairline.is-stale > span { background: var(--stale); }
.mc-hairline.is-blocked > span { background: var(--blocked); }
@media (prefers-reduced-motion: reduce) { .mc-hairline > span { transition: none; } }
```

`extension.js`, directly after the closing `</div>` of `#manager-chat-session-line`:

```html
        <div class="mc-hairline" id="manager-chat-hairline" role="meter" aria-label="Manager context use" aria-valuemin="0" aria-valuemax="100" hidden><span></span></div>
```

and inside `#manager-chat-session-line`, after the session select: `<span class="mc-mono" id="manager-chat-context" aria-label="Manager context percent"></span>`. In `app.js` `elements`: `managerChatHairline: document.querySelector("#manager-chat-hairline"),` and `managerChatContext: document.querySelector("#manager-chat-context"),`.

- [ ] **Step 5: Run the tests, commit**

Run (in `vscode-extension/`): `node --test test/manager-console.test.js`, `node --test test/manager-chat-panel.test.js`, `npm test` — all pass.

```bash
git add vscode-extension/media/manager_console.js vscode-extension/media/manager_console.css vscode-extension/media/app.js vscode-extension/extension.js vscode-extension/test/manager-console.test.js vscode-extension/test/manager-chat-panel.test.js
git commit -m "feat(console): glyph-gutter blocks, token footer and context hairline"
```

---

### Task W1-U3: Markdown subset, live partial, block cap, final-only announcements

**Objective:** Render manager text as a DOM-built Markdown subset, show the running turn's `partial` (text with caret, open thinking) coalesced to one DOM write per frame, cap the transcript at 400 blocks behind "load earlier", keep auto-scroll only while the owner is at the bottom ("↓ latest" otherwise), and announce only final messages to screen readers.

**Files:**
- Modify: `vscode-extension/media/manager_console.js`, `vscode-extension/media/manager_console.css`
- Modify: `vscode-extension/media/app.js` (`renderManagerChatEventsResponse` ~7625: keep `payload.partial`; call the frame scheduler instead of rendering directly; `elements` map: `managerChatAnnouncer`, `managerChatLatest`)
- Modify: `vscode-extension/extension.js` (transcript markup ~11733: drop `aria-live` from the transcript; add announcer and "↓ latest" button)
- Test: `vscode-extension/test/manager-console.test.js`, `vscode-extension/test/manager-chat-panel.test.js`

**Interfaces:**
- Consumes: `partial` from `aiworkhub_manager_loop_events` (W1-P2); block renderers (W1-U2).
- Produces: `managerConsoleMarkdown(text) -> DocumentFragment`, `managerConsolePartialNodes(partial) -> Element[]`, `managerConsoleScheduleRender()`, `MANAGER_CONSOLE_BLOCK_LIMIT = 400`, `state.managerChatPartial`, `state.managerChatRenderLimit`, `state.managerChatAnnouncedSeq`.

**Acceptance:**
- Markdown subset: paragraphs, `-`/`*`/`1.` lists, inline code, fenced code, `**bold**`, `*italic*`/`_italic_`, `[text](url)` rendered as `text (url)` text — all nodes built with `createElement`/`createTextNode`; a hostile string stays text; no `a` element is created.
- `partial` renders after the last final block: reasoning as an open `∴` block, text as a `●` block with a caret; a partial whose `turn` already has a `turn_end` (or whose final `assistant_text` arrived) is not rendered.
- Five `managerConsoleScheduleRender()` calls inside one frame produce one render.
- With 450 blocks the transcript holds 400 plus a "load earlier" button; clicking it raises the limit by 400.
- Scrolled up: a new render keeps `scrollTop` and shows "↓ latest"; at the bottom it follows the stream.
- The transcript has no `aria-live`; the announcer's text changes only when a new final `assistant_text` seq arrives.

**Validation:** `node --test test/manager-console.test.js` (target) and `node --test test/manager-chat-panel.test.js` plus `npm test` (regression), from `vscode-extension/`.

- [ ] **Step 1: Write the failing tests** (append to `test/manager-console.test.js`; extend `load()` so the context also has `window: { requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; } }` and `elements.managerChatAnnouncer = fakeElement("div")`, `elements.managerChatLatest = fakeElement("button")`, and returns `frames`)

```js
test("markdown subset is built from nodes and never creates links or html", () => {
  const { api } = load();
  const fragment = api.managerConsoleMarkdown("Intro **bold** and `code`\n\n- one\n- two\n\n```\n<img src=x>\n```\n\nsee [docs](https://example.test)");
  const all = flat(fragment);
  assert.ok(all.some((n) => n.tag === "strong" && text(n) === "bold"));
  assert.ok(all.some((n) => n.tag === "code" && text(n) === "code"));
  assert.equal(all.filter((n) => n.tag === "li").length, 2);
  assert.ok(all.some((n) => n.tag === "pre" && text(n).includes("<img src=x>")));
  assert.ok(!all.some((n) => n.tag === "a" || n.tag === "img"));
  assert.ok(text(fragment).includes("docs (https://example.test)"));
});

test("partial renders only for a turn that has not finished", () => {
  const { api, state } = load([{ seq: 1, turn: 3, type: "user_message", payload: { text: "go" } }]);
  const live = api.managerConsolePartialNodes({ turn: 3, text: "Hel", reasoning: "hm", command_output: "" });
  assert.equal(live.length, 2);
  assert.ok(flat(live[1]).some((n) => n.className === "mc-caret"));
  state.managerChatEvents.push({ seq: 2, turn: 3, type: "turn_end", payload: {} });
  assert.deepEqual(api.managerConsolePartialNodes({ turn: 3, text: "Hel", reasoning: "", command_output: "" }), []);
});

test("renders coalesce to one DOM write per animation frame", () => {
  const { api, frames, elements } = load([{ seq: 1, turn: 1, type: "assistant_text", payload: { text: "hi" } }]);
  let writes = 0;
  const original = elements.managerChatTranscript.replaceChildren;
  elements.managerChatTranscript.replaceChildren = function (...nodes) { writes += 1; return original.apply(this, nodes); };
  for (let i = 0; i < 5; i += 1) api.managerConsoleScheduleRender();
  assert.equal(frames.length, 1);
  frames[0]();
  assert.equal(writes, 1);
});

test("the transcript keeps the newest 400 blocks behind load earlier", () => {
  const events = Array.from({ length: 450 }, (_, i) => ({ seq: i + 1, turn: 1, type: "assistant_text", payload: { text: `m${i}` } }));
  const { api, elements, state } = load(events);
  api.renderManagerChatEvents();
  const rows = elements.managerChatTranscript.children;
  assert.equal(rows.length, 401);
  assert.equal(rows[0].tag, "button");
  rows[0].listeners.click[0]();
  assert.equal(state.managerChatRenderLimit, 800);
});

test("only a new final message is announced", () => {
  const { api, elements, state } = load([{ seq: 1, turn: 1, type: "assistant_text", payload: { text: "first" } }]);
  api.renderManagerChatEvents();
  assert.equal(elements.managerChatAnnouncer.textContent, "first");
  elements.managerChatAnnouncer.textContent = "";
  state.managerChatPartial = { turn: 2, text: "stream", reasoning: "", command_output: "" };
  api.renderManagerChatEvents();
  assert.equal(elements.managerChatAnnouncer.textContent, "");
});
```

Add `managerConsoleMarkdown, managerConsolePartialNodes, managerConsoleScheduleRender` to the `this.api` export line in `load()`.

- [ ] **Step 2: Run to verify they fail**

Run (in `vscode-extension/`): `node --test test/manager-console.test.js`
Expected: FAIL — `managerConsoleMarkdown is not defined`.

- [ ] **Step 3: Implement**

```js
const MANAGER_CONSOLE_BLOCK_LIMIT = 400;
const MANAGER_CONSOLE_INLINE = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_|\[[^\]]+\]\([^)\s]+\))/;

function managerConsoleInline(parent, text) {
  for (const part of String(text).split(MANAGER_CONSOLE_INLINE)) {
    if (!part) continue;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 1) parent.appendChild(createElement("code", "mc-mono", part.slice(1, -1)));
    else if (part.startsWith("**") && part.endsWith("**") && part.length > 4) parent.appendChild(createElement("strong", "", part.slice(2, -2)));
    else if (/^(\*[^*]+\*|_[^_]+_)$/.test(part)) parent.appendChild(createElement("em", "", part.slice(1, -1)));
    else if (/^\[[^\]]+\]\([^)\s]+\)$/.test(part)) {
      const cut = part.indexOf("](");
      parent.appendChild(document.createTextNode(`${part.slice(1, cut)} (${part.slice(cut + 2, -1)})`));
    } else parent.appendChild(document.createTextNode(part));
  }
}

function managerConsoleMarkdown(text) {
  const fragment = document.createDocumentFragment();
  const lines = String(text || "").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.startsWith("```")) {
      const code = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) code.push(lines[index++]);
      index += 1;
      fragment.appendChild(createElement("pre", "mc-code", code.join("\n")));
      continue;
    }
    const bullet = /^\s*([-*]|\d+\.)\s+/;
    if (bullet.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const list = createElement(ordered ? "ol" : "ul", "mc-list");
      while (index < lines.length && bullet.test(lines[index])) {
        const item = createElement("li", "");
        managerConsoleInline(item, lines[index++].replace(bullet, ""));
        list.appendChild(item);
      }
      fragment.appendChild(list);
      continue;
    }
    if (!line.trim()) { index += 1; continue; }
    const paragraph = createElement("p", "mc-p");
    const words = [];
    while (index < lines.length && lines[index].trim() && !lines[index].startsWith("```") && !bullet.test(lines[index])) words.push(lines[index++]);
    managerConsoleInline(paragraph, words.join("\n"));
    fragment.appendChild(paragraph);
  }
  return fragment;
}

function managerConsoleTurnFinished(turn) {
  return (state.managerChatEvents || []).some((item) => item && item.turn === turn && item.type === "turn_end");
}

function managerConsolePartialNodes(partial) {
  if (!partial || managerConsoleTurnFinished(partial.turn)) return [];
  const nodes = [];
  if (partial.reasoning) {
    const details = createElement("details", "mc-thinking");
    details.open = true;
    details.appendChild(createElement("summary", "", "Thinking"));
    details.appendChild(createElement("div", "mc-thinking-text", partial.reasoning));
    nodes.push(managerConsoleBlock("reasoning", details));
  }
  if (partial.text) {
    const body = createElement("div", "mc-body mc-streaming");
    body.appendChild(managerConsoleMarkdown(partial.text));
    const caret = createElement("span", "mc-caret");
    caret.setAttribute("aria-hidden", "true");
    body.appendChild(caret);
    nodes.push(managerConsoleBlock("assistant_text", body));
  }
  return nodes;
}

let managerConsoleFramePending = false;

function managerConsoleScheduleRender() {
  if (managerConsoleFramePending) return;
  managerConsoleFramePending = true;
  window.requestAnimationFrame(() => {
    managerConsoleFramePending = false;
    renderManagerChatEvents();
  });
}

function managerConsoleAnnounce(events) {
  if (!elements.managerChatAnnouncer) return;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (!event || event.type !== "assistant_text") continue;
    if (event.seq > (state.managerChatAnnouncedSeq || 0)) {
      state.managerChatAnnouncedSeq = event.seq;
      elements.managerChatAnnouncer.textContent = String((event.payload && event.payload.text) || "");
    }
    return;
  }
}
```

In `renderManagerChatEvents`:

1. Build `rows` as today from `managerConsoleMergeCommands(state.managerChatEvents)`, then `rows.push(...managerConsolePartialNodes(state.managerChatPartial))` in place of the live-thinking row when a partial exists (keep the live-thinking row when running without a partial).
2. Cap: `const limit = state.managerChatRenderLimit || MANAGER_CONSOLE_BLOCK_LIMIT;` — when `rows.length > limit`, keep `rows.slice(-limit)` and prepend a "load earlier" button whose click sets `state.managerChatRenderLimit = limit + MANAGER_CONSOLE_BLOCK_LIMIT` and calls `managerConsoleScheduleRender()`.
3. Scroll: before `replaceChildren`, `const box = elements.managerChatTranscript; const atBottom = box.scrollHeight - box.scrollTop - (box.clientHeight || 0) < 24;` after it, `if (atBottom) box.scrollTop = box.scrollHeight;` and `if (elements.managerChatLatest) elements.managerChatLatest.hidden = atBottom;`.
4. End with `managerConsoleAnnounce(state.managerChatEvents)` and `managerConsoleApplyHairline()`.
5. `assistant_text` blocks render `managerConsoleMarkdown(text)` in their body instead of a plain text node.

In `app.js` `renderManagerChatEventsResponse`: `state.managerChatPartial = payload && payload.partial ? payload.partial : null;` and replace its direct `renderManagerChatEvents()` call with `managerConsoleScheduleRender()`. Clear `state.managerChatPartial` wherever the session changes (the same places that reset `state.managerChatEvents`). Wire `elements.managerChatLatest` click → `box.scrollTop = box.scrollHeight; elements.managerChatLatest.hidden = true;`.

`extension.js` transcript area:

```html
        <div class="manager-chat-transcript" id="manager-chat-transcript">
          <div class="panel-list-empty compact" id="manager-chat-empty">Write on the selected model to open the first session</div>
        </div>
        <button type="button" class="mc-latest" id="manager-chat-latest" hidden>↓ latest</button>
        <div class="sr-only" id="manager-chat-announcer" aria-live="polite"></div>
```

`.sr-only` is `app.css`'s existing screen-reader utility (~line 260); no new class.

`test/manager-chat-panel.test.js`: `windowFake` (~line 454) gains `requestAnimationFrame: (fn) => { fn(); return 1; },` so the panel tests that go through `renderManagerChatEventsResponse` still render synchronously. The poll interval needs no change: it is already 400 ms while a turn runs.

CSS additions:

```css
.mc-caret { display: inline-block; width: 0.5ch; height: 1em; margin-left: 1px; vertical-align: text-bottom; background: var(--ink-soft); animation: mc-caret-blink 1s steps(1) infinite; }
@keyframes mc-caret-blink { 50% { opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .mc-caret { animation: none; } }
.mc-p { margin: 0 0 6px; }
.mc-list { margin: 0 0 6px; padding-left: 2ch; }
.mc-code { padding: 6px 8px; background: var(--surface-subtle); border: 1px solid var(--line); }
.mc-latest { position: sticky; bottom: 8px; margin-left: auto; font-size: var(--fs-2xs); }
.mc-thinking { color: var(--ink-soft); }
```

- [ ] **Step 4: Run the tests, commit**

Run (in `vscode-extension/`): `node --test test/manager-console.test.js`, `node --test test/manager-chat-panel.test.js`, `npm test` — all pass.

```bash
git add vscode-extension/media/manager_console.js vscode-extension/media/manager_console.css vscode-extension/media/app.js vscode-extension/extension.js vscode-extension/test/manager-console.test.js vscode-extension/test/manager-chat-panel.test.js
git commit -m "feat(console): markdown subset, live partial, block cap and final-only announcements"
```

---

### Task W1-M1 (manager step, not a card): W1 release and live measurement

- [ ] Full suites green: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests` and `cd vscode-extension && npm test`.
- [ ] Intermediate release per the repository procedure (bump `_version.py` → `scripts/release_metadata.py sync` → root and extension CHANGELOG + README "What's new" → commit `release: X` → `npm run package` → `code --install-extension vscode-extension/dist/aiworkhub-X.vsix --force`).
- [ ] After the owner's reload, measure on the real panel and record the numbers on the tracker: a Claude turn shows streamed text before its final block (spec success criterion 2: within 1 s; measure the gap between the first `partial` poll and the provider's first delta line); `aiworkhub_manager_loop_events` for that session returns no event of type `delta`; a `git --version` turn shows one `$` block with `exit 0`; the footer and hairline show numbers equal to the `turn_end` usage.

---

## Waves W2–W5 (card level)

W2–W5 depend on evidence W1 and the W2 fixture produce (Claude `/goal` stream shape, Codex app-server protocol), so their step-level TDD plans are written when their wave starts, from the measured fixtures; nothing below guesses a provider shape. Card fields are fixed now so the cards can be created without re-planning scope.

### W2 — native goals, steer, stop

| Card | Objective | allowed_writes (production + tests) | Acceptance | Validation |
|---|---|---|---|---|
| W2-S1 | Stop `sanitizeWebviewPayload` from mangling slash commands such as `/goal …` while still redacting path fragments | `vscode-extension/extension.js`; `vscode-extension/test/manager-chat-panel.test.js` (or the existing sanitize test file) | `/goal ship it`, `/stop`, `/model x` reach the webview unchanged; every existing path-redaction assertion still passes | `node --test` on both files; `npm test` |
| W2-G0 | Manager host step: capture a Claude `/goal` run with `scripts/capture_manager_stream.py` (prompt `/goal …`, then `/goal clear`) into `tests/fixtures/manager_streams/claude_cli_goal.jsonl`; leak check as in W1-M0 | manager commit only | fixture shows how Claude reports goal set / evaluation / met | leak awk prints nothing |
| W2-C1 | `codex_app_server` backend: one persistent `codex app-server` per session over `callback_bridge.AppServerClient`, thread start/resume, turn start, steer, interrupt, events → v3 (incl. `outputDelta` → `partial.command_output`, `turn/diff/updated` → `file_change`), CLI version recorded, unknown method → named error | new `src/aiworkhub/manager_loop_app_server.py`; `src/aiworkhub/manager_loop_service.py` (backend factory); `src/aiworkhub/manager_loop_backends.py` (registration); new `tests/test_manager_loop_app_server.py`; new fixture `tests/fixtures/manager_streams/codex_app_server.jsonl` | fixture → exact v3 list; process restarts once on death then errors (spec §8); no process per turn | new test file + `tests/test_manager_loop_service.py` |
| W2-C2 | Claude `/goal` mapping to `goal` events from the W2-G0 fixture; `goal` added to `EVENT_TYPES`; `/goal` / `/goal stop` sent as turn messages on `claude_cli` | `src/aiworkhub/manager_loop_backends.py`; `src/aiworkhub/manager_loop.py`; `tests/test_manager_stream_fixtures.py`; `tests/test_manager_loop.py`; expected JSON | goal status transitions equal the fixture's; nothing mapped that the fixture does not show | fixture tests + manager loop tests |
| W2-C3 | Service + MCP: `aiworkhub_manager_loop_goal` (set/status/stop), `aiworkhub_manager_loop_interrupt`, steer on send-while-running for `codex_app_server`, queued-and-labelled elsewhere | `src/aiworkhub/manager_loop.py`; `src/aiworkhub/manager_loop_service.py`; `src/aiworkhub/server.py`; `vscode-extension/extension.js` (`MANAGER_LOOP_TOOLS`); `tests/test_manager_loop_service.py`; server tool-list test file | a goal ends only in met / impossible / budget / owner stop (spec criterion 5); steer never starts a second writer | service tests; server tests |
| W2-C4 | Goal strip (sticky), `◎` inline transitions, Send↔Stop toggle, Esc stops, "queued" label | `media/manager_console.js/.css`; `media/app.js`; `extension.js` (markup); `test/manager-console.test.js`; `test/manager-chat-panel.test.js` | strip shows condition, status, turns/budget, tokens/budget, last verdict; Stop posts interrupt | node tests; `npm test` |

Order: W2-S1 ∥ W2-G0 ∥ W2-C1 → W2-C2 → W2-C3 → W2-C4 (C2/C3 share `manager_loop.py`; C4 needs C3's messages).

### W3 — GoalLoop for every other backend

| Card | Objective | allowed_writes | Acceptance | Validation |
|---|---|---|---|---|
| W3-G1 | `manager_goal_loop.py`: condition turn, optional argv check commands (repository cwd, timeout, no shell), evaluator on the cheapest `models.json` route returning `{verdict, reason}`, budgets (`max_turns` 20, optional tokens / wall clock), `unknown` pauses | new `src/aiworkhub/manager_goal_loop.py`; new `tests/test_manager_goal_loop.py` | fake backend + fake evaluator cover met, met-with-failing-check (continues), not_yet, impossible, unknown (pauses), each budget, owner stop (spec §9) | new tests |
| W3-G2 | Wire GoalLoop into the orchestrator/service for non-native backends; checks editor in the goal strip | `src/aiworkhub/manager_loop.py`; `src/aiworkhub/manager_loop_service.py`; `media/manager_console.js`; `media/app.js`; their tests | `/goal` on `opencode_cli` runs the loop and ends in a named state | manager loop + service tests; node tests |

### W4 — token economy

| Card | Objective | allowed_writes | Acceptance | Validation |
|---|---|---|---|---|
| W4-E1 | Provider-neutral transcript capture: every final `user_message` / `assistant_text` of every backend to Context Graph via `manager_transcript_capture.write_completed_message(provider=…)`; reasoning, tool output, deltas never captured | `src/aiworkhub/manager_transcript_capture.py`; `src/aiworkhub/manager_loop.py`; their tests | spec criterion 3 on fake backends of all three providers | capture + manager loop tests |
| W4-E2 | Token-based rotation: `context_fill` from the last `turn_end` ≥ 0.75 hands off; `context_window` added to routes in `.aiworkhub/config/models.json` where missing; 512 KB byte estimate stays the fallback | `src/aiworkhub/manager_loop.py`; `.aiworkhub/config/models.json`; model-config loader and its tests | rotation fires at measured 75 %; falls back to bytes without usage | manager loop tests |
| W4-E3 | Callback digest: one line per callback `callback T-… review_ready: <title ≤ 80>`; `callback.payload` = `{task_id, status, summary}` | `src/aiworkhub/manager_loop_wake.py`; `src/aiworkhub/manager_loop.py`; `tests/test_manager_loop_wake.py` | wake message equals the digest; the manager pulls details on demand | wake tests |

Order: W3-G2 → W4-E1 → W4-E2 → W4-E3 (all write `manager_loop.py`).

### W5 — polish

| Card | Objective | allowed_writes | Acceptance | Validation |
|---|---|---|---|---|
| W5-P1 | Slash menu (only commands valid for the current backend) and ↑/↓ history at the caret edge | `media/manager_console.js/.css`; `media/app.js`; node tests | `/` opens the filtered menu; history recalls without losing a draft | node tests |
| W5-P2 | Open in terminal: resumed provider CLI (`claude --resume`, `codex resume`, `opencode --session`) with `provision_manager_seat_env`; Send disabled while attached, re-enabled on `onDidCloseTerminal` | `vscode-extension/extension.js`; `media/manager_console.js`; `media/app.js`; extension + node tests | two writers never share one conversation | extension tests; `npm test` |
| W5-P3 | Poll failure: exponential backoff to 5 s and a "reconnecting" banner; lossless resume by `after_seq` | `media/app.js`; `media/manager_console.js/.css`; node tests | backoff sequence capped at 5 s; banner clears on the first good poll | node tests |

W5 cards touch the same webview files, so they run one after another.

---

## Self-review

- **Spec coverage:** §3 blocks → U2 (command, file change, tool, task, callback, error, footer, hairline), U3 (Markdown, partial caret, thinking open while streaming, 400 cap, scroll pill, aria-live, rAF); goal strip, Send/Stop, steer → W2-C4; slash menu, terminal, history, reconnect → W5. §4 → P1a (bounds), P1b (call ids, command, file_change, usage, `v`), P2 (`partial`); `goal` event → W2-C2; callback digest → W4-E3. §5 → W2/W3. §6 → W4 (+ app-server process per session in W2-C1). §7 → W5-P1/P2. §8 → P1b (unrecognized line skipped), P1a (bounds), W2-C1 (app-server restart), W3-G1 (evaluator/check failures), W5-P3 (poll backoff). §9 → F0/M0 fixtures, P1b fixture tests, P2 partial test, U2 hostile test, U3 coalescing test, W3-G1 GoalLoop matrix. §11 risks → W2-G0 fixture first; W2-C1 version record.
- **Placeholder scan:** W2–W5 are card-level on purpose (they depend on fixtures that do not exist yet); every W1 step carries its code and command.
- **Type consistency:** `TurnContext`, `translate(backend_id, event, context=None)`, `FIELD_BOUNDS`, `partial` shape `{session_id, turn, text, reasoning, command_output}`, `managerConsole*` names are used identically across P1b/P2/U2/U3.
- **Review Focus:** torn tail → P1a test; non-string output → P1a + U2 tests; hostile payload → U2 test; fixture leak → F0 test + M0 awk; partial across turns → P2 + U3 tests.
- **Fixed after checking the draft against the code:** the F0 replay child reads a file (Windows argv quoting would mangle JSON); redaction is word-bounded (new test) so a short user name cannot eat longer words; M0 takes each route's first enabled model from `models.json` and its leak check matches e-mail shapes and the account name instead of a bare `@`; the P1a compaction test uses `max_events=3` with the arithmetic spelled out, and the torn-tail check reads one byte; P1b diff counts skip only the real `---`/`+++` header (new test), Codex tool output reads `aggregated_output` first, and the fixture test plus its generator drop `delta` items, so P2 needs no expected-file regeneration; P2 has an exact service test; the U2 node harness slices the real `app.js` utilities and `TASK_ID_RE` instead of faking them; the footer shows a turn number, counts and duration only when they are real numbers (the existing panel tests are updated line by line); the bubble label and the announcer reuse `.sr-only`.
