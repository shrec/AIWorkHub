# Manager terminal console W1-P2: measured amendments

Card: `AIWORKHUB_CONSOLE_W1_P2_CLAUDE_DELTA_PARTIAL_V1`. Plan section: W1-P2 of
`docs/superpowers/plans/2026-09-27-manager-terminal-console-w1.md` (lines 1000-1222).

The plan's P2 code was written before W1-P1b landed. This brief is the plan's P2 code
applied to the current tree, run and corrected. It replaces the plan section for this card:
apply the six diffs below exactly. Nothing else in the six files changes.

## What the change does

- The Claude translator turns each `stream_event` / `content_block_delta` line into a
  transient `{"type": "delta", "payload": {"kind": "text" | "reasoning", "text": ...}}` item.
- `ManagerOrchestrator._exchange` never records a delta: it grows `self._partial`
  (one dict per running turn, replaced on every delta, never mutated) and continues.
  A delta does not count toward `MAX_TURN_EVENTS`.
- A final `assistant_text` / `reasoning` / `command` event clears its field of the partial;
  the end of the turn, also a failed one, clears the partial.
- `manager_loop_service.events` returns the partial of the asked session beside the events.

## Measured corrections to the plan's code

1. The plan adds `_keep_tail` to `manager_loop.py` while `manager_loop_backends.py`
   already holds the same body as `_tail`. Two identical bodies fail
   `tests/test_declared_invariants.py` (`copied_helpers_have_one_definition`). The helper
   therefore lives once, as `keep_tail` in `manager_loop.py`; `manager_loop_backends.py`
   imports it and its own `_tail` is removed.
2. `src/aiworkhub/server.py` is not changed: the plan's docstring edit is dropped.
3. Two tests are added to the plan's own: a delta flood does not hit the turn event limit
   and a partial keeps its tail, and a failed turn leaves no partial behind.

## Symbols that must survive unchanged

Everything not named in a diff, in particular `REASONING_LEVELS`, `manager_stream_tokens`,
`apply_manager_stream_tokens`, `TurnContext`, `OUTPUT_TAIL_BYTES`, `FIELD_BOUNDS`,
`_TAIL_READ_BYTES`, `_clip` and every existing test.

## Verified result

On the tree of this brief's commit with the six diffs applied: the six new tests fail
before the source diffs and pass after; `366 passed` on `tests/test_manager_loop.py`,
`tests/test_manager_loop_stream.py`, `tests/test_manager_loop_service.py`,
`tests/test_manager_stream_fixtures.py`, `tests/test_manager_loop_backends.py`,
`tests/test_capture_manager_stream.py`, `tests/test_aiworkhub_manager_ai_tools.py`,
`tests/test_os_dependency_boundary.py`, `tests/test_aiworkhub_dependency_autolaunch_b905_v7.py`,
`tests/test_module_size_ratchet.py` and `tests/test_declared_invariants.py`; ruff clean.
Line counts after: `manager_loop.py` 1422, `manager_loop_backends.py` 1013.

## The diffs

### `src/aiworkhub/manager_loop_backends.py`

```diff
--- a/src/aiworkhub/manager_loop_backends.py
+++ b/src/aiworkhub/manager_loop_backends.py
@@ -58,7 +58,7 @@
 from typing import Any, Callable, Iterator, Mapping, Sequence
 
 from . import cli_model_discovery, context_capture, platform_io, runtime_adapters, workforce_catalog
-from .manager_loop import ManagerLoopError
+from .manager_loop import ManagerLoopError, keep_tail
 
 MANAGER_BACKEND_IDS: tuple[str, ...] = ("claude_cli", "codex_cli", "opencode_cli")
 # The worker lane's own launch ceiling, reused as the manager turn default: a
@@ -140,11 +140,6 @@
     return {**counts, "context_window": size or None, "context_fill": fill_ratio, "raw": dict(raw)}
 
 
-def _tail(text: str, limit: int = OUTPUT_TAIL_BYTES) -> str:
-    data = text.encode("utf-8")
-    return text if len(data) <= limit else data[-limit:].decode("utf-8", "ignore")
-
-
 def _result_text(content: Any) -> str:
     if isinstance(content, str):
         return content
@@ -162,7 +157,7 @@
     return {"type": "command", "payload": {
         "call_id": call_id, "command": command, "cwd": cwd, "status": status,
         "exit_code": exit_code if isinstance(exit_code, int) else None,
-        "output_tail": _tail(output), "output_bytes": len(output.encode("utf-8")),
+        "output_tail": keep_tail(output, OUTPUT_TAIL_BYTES), "output_bytes": len(output.encode("utf-8")),
     }}
 
 
@@ -278,9 +273,29 @@
     return [*shown, _tool_result(call_id, name, block.get("content"), failed)]
 
 
+def _delta(kind: str, text: Any) -> list[dict[str, Any]]:
+    """A streamed fragment for display only; the orchestrator never records it."""
+    text = str(text or "")
+    return [{"type": "delta", "payload": {"kind": kind, "text": text}}] if text else []
+
+
+def _claude_delta(event: Mapping[str, Any]) -> list[dict[str, Any]]:
+    inner = _mapping(event.get("event"))
+    if inner.get("type") != "content_block_delta":
+        return []
+    delta = _mapping(inner.get("delta"))
+    if delta.get("type") == "text_delta":
+        return _delta("text", delta.get("text"))
+    if delta.get("type") == "thinking_delta":
+        return _delta("reasoning", delta.get("thinking"))
+    return []
+
+
 def _claude_events(event: Mapping[str, Any], context: TurnContext) -> list[dict[str, Any]]:
     """Claude ``stream-json``: assistant/user content blocks, then the result event."""
     kind = str(event.get("type") or "")
+    if kind == "stream_event":
+        return _claude_delta(event)
     if kind == "result":
         return [_turn_end(_claude_usage(event, context))]
     message = _mapping(event.get("message"))
```

### `src/aiworkhub/manager_loop.py`

```diff
--- a/src/aiworkhub/manager_loop.py
+++ b/src/aiworkhub/manager_loop.py
@@ -60,6 +60,9 @@
 MAX_LOG_EVENTS = 500
 MAX_HANDOFF_BYTES = 8 * 1024
 MAX_TURN_EVENT_BYTES = 8 * 1024
+PARTIAL_FIELD_BYTES = 64 * 1024
+_PARTIAL_FIELDS = ("text", "reasoning", "command_output")
+_SETTLES = {"assistant_text": "text", "reasoning": "reasoning", "command": "command_output"}
 MAX_BRIEF_CARDS = 25
 KEEP_CLOSED_SESSIONS = 20
 SESSION_TOPIC = "management"
@@ -171,6 +174,12 @@
     return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
 
 
+def keep_tail(text: str, limit: int) -> str:
+    """The last ``limit`` UTF-8 bytes of ``text``."""
+    data = text.encode("utf-8")
+    return text if len(data) <= limit else data[-limit:].decode("utf-8", "ignore")
+
+
 def _clip(text: str, limit: int) -> str:
     """Cut ``text`` to at most ``limit`` UTF-8 bytes, marking whatever was cut."""
     data = text.encode("utf-8")
@@ -654,6 +663,29 @@
         self._lock_fd: int | None = None
         self._session: ManagerSession | None = None
         self._backend: ManagerBackend | None = None
+        self._partial: dict[str, Any] | None = None
+
+    @property
+    def partial(self) -> dict[str, Any] | None:
+        """The running turn's streamed, unfinished text; display only, never persisted."""
+        return self._partial
+
+    def _grow_partial(self, session: ManagerSession, turn: int, payload: Any) -> None:
+        payload = payload if isinstance(payload, Mapping) else {}
+        kind = str(payload.get("kind") or "")
+        if kind not in _PARTIAL_FIELDS:
+            return
+        current = self._partial
+        if current is None or current["turn"] != turn or current["session_id"] != session.session_id:
+            current = {"session_id": session.session_id, "turn": turn, **{field: "" for field in _PARTIAL_FIELDS}}
+        # ponytail: tail-only cap per field; the final event carries the full text.
+        grown = keep_tail(current[kind] + str(payload.get("text") or ""), PARTIAL_FIELD_BYTES)
+        self._partial = {**current, kind: grown}  # replaced, never mutated: the poll reads it unlocked
+
+    def _settle_partial(self, kind: str) -> None:
+        field = _SETTLES.get(kind)
+        if field and self._partial and self._partial[field]:
+            self._partial = {**self._partial, field: ""}
 
     @classmethod
     def for_repository(
@@ -1227,11 +1259,15 @@
         grown = 0
         try:
             for raw in backend.send(message):
+                if isinstance(raw, Mapping) and raw.get("type") == "delta":
+                    self._grow_partial(session, turn, raw.get("payload"))
+                    continue
                 if len(events) >= MAX_TURN_EVENTS:
                     capped = {"source": "loop", "error": "turn_event_limit"}
                     events.append(self._record(session, turn, "error", capped))
                     break
                 kind, payload = _normalize(raw)
+                self._settle_partial(kind)
                 grown += len(_json(payload).encode("utf-8"))
                 if kind == "assistant_text":
                     texts.append(str(payload.get("text", "")))
@@ -1239,6 +1275,8 @@
         except Exception as exc:  # noqa: BLE001 - a failed turn is an error event, not a dead session
             broke = {"source": "backend", "error": _failure(exc)}
             events.append(self._record(session, turn, "error", broke))
+        finally:
+            self._partial = None
         return events, grown, "".join(texts)
 
     def _rotate(self, reason: str, *, successor: bool) -> dict[str, Any]:
```

### `src/aiworkhub/manager_loop_service.py`

```diff
--- a/src/aiworkhub/manager_loop_service.py
+++ b/src/aiworkhub/manager_loop_service.py
@@ -594,7 +594,9 @@
     floor = int(after_seq)
     bound = max(0, int(limit))
     filtered = [event for event in all_events if int(event.get("seq", 0)) > floor][:bound]
-    return {"ok": True, "events": filtered}
+    partial = entry.orchestrator.partial
+    mine = partial if partial and partial.get("session_id") == session_id else None
+    return {"ok": True, "events": filtered, "partial": mine}
 
 
 def close(repo: str | Path) -> dict[str, Any]:
```

### `tests/test_manager_loop.py`

```diff
--- a/tests/test_manager_loop.py
+++ b/tests/test_manager_loop.py
@@ -1305,3 +1305,86 @@
 
     assert session.repo_id == state.manifest.repo_id and session.passive
     assert built == []
+
+
+def test_deltas_fill_partial_and_never_reach_the_log(make) -> None:
+    harness = make()
+    session = harness.orch.start("fake", "model-a")
+    seen: list[Any] = []
+
+    def stream():
+        yield {"type": "delta", "payload": {"kind": "text", "text": "Hel"}}
+        yield {"type": "delta", "payload": {"kind": "text", "text": "lo"}}
+        seen.append(harness.orch.partial)
+        yield {"type": "assistant_text", "payload": {"text": "Hello"}}
+        seen.append(harness.orch.partial)
+        yield {"type": "turn_end", "payload": {}}
+
+    harness.backends[0].script.append(stream())
+    result = harness.orch.send("hi")
+
+    assert seen[0]["text"] == "Hello" and seen[0]["turn"] == result["turn"]
+    assert seen[0]["session_id"] == session.session_id
+    assert seen[1]["text"] == ""
+    assert harness.orch.partial is None
+    logged = harness.store.events(session.session_id)
+    assert "delta" not in {event["type"] for event in logged}
+    assert [event["type"] for event in logged][-2:] == ["assistant_text", "turn_end"]
+
+
+def test_a_partial_never_crosses_into_the_next_turn(make) -> None:
+    harness = make()
+    harness.orch.start("fake", "model-a")
+    seen: list[Any] = []
+
+    def first():
+        yield {"type": "delta", "payload": {"kind": "reasoning", "text": "old"}}
+        yield {"type": "turn_end", "payload": {}}
+
+    def second():
+        seen.append(harness.orch.partial)
+        yield {"type": "delta", "payload": {"kind": "text", "text": "new"}}
+        seen.append(harness.orch.partial)
+        yield {"type": "turn_end", "payload": {}}
+
+    harness.backends[0].script.extend([first(), second()])
+    harness.orch.send("one")
+    harness.orch.send("two")
+
+    assert seen[0] is None
+    assert seen[1]["reasoning"] == "" and seen[1]["text"] == "new"
+
+
+def test_deltas_do_not_count_toward_the_turn_event_limit_and_a_partial_keeps_its_tail(make, monkeypatch) -> None:
+    monkeypatch.setattr(ml, "MAX_TURN_EVENTS", 2)
+    monkeypatch.setattr(ml, "PARTIAL_FIELD_BYTES", 4)
+    harness = make()
+    session = harness.orch.start("fake", "model-a")
+    seen: list[Any] = []
+
+    def stream():
+        for piece in ("ab", "cd", "ef"):
+            yield {"type": "delta", "payload": {"kind": "text", "text": piece}}
+        seen.append(harness.orch.partial)
+        yield {"type": "assistant_text", "payload": {"text": "abcdef"}}
+        yield {"type": "turn_end", "payload": {}}
+
+    harness.backends[0].script.append(stream())
+    harness.orch.send("hi")
+
+    assert seen[0]["text"] == "cdef"
+    assert [event["type"] for event in harness.store.events(session.session_id)][-2:] == ["assistant_text", "turn_end"]
+
+
+def test_a_failed_turn_leaves_no_partial_behind(make) -> None:
+    harness = make()
+    harness.orch.start("fake", "model-a")
+
+    def stream():
+        yield {"type": "delta", "payload": {"kind": "text", "text": "half"}}
+        raise RuntimeError("backend died")
+
+    harness.backends[0].script.append(stream())
+    harness.orch.send("hi")
+
+    assert harness.orch.partial is None
```

### `tests/test_manager_loop_stream.py`

```diff
--- a/tests/test_manager_loop_stream.py
+++ b/tests/test_manager_loop_stream.py
@@ -52,3 +52,12 @@
     assert codex[-3] == "-c"
     assert codex[-2] == 'model_reasoning_effort="high"'
     assert codex[-1] == "-"
+
+
+def test_claude_stream_deltas_become_transient_delta_items() -> None:
+    text = {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}}}
+    think = {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hm"}}}
+    other = {"type": "stream_event", "event": {"type": "message_start"}}
+    assert translate("claude_cli", text) == [{"type": "delta", "payload": {"kind": "text", "text": "Hel"}}]
+    assert translate("claude_cli", think) == [{"type": "delta", "payload": {"kind": "reasoning", "text": "hm"}}]
+    assert translate("claude_cli", other) == []
```

### `tests/test_manager_loop_service.py`

```diff
--- a/tests/test_manager_loop_service.py
+++ b/tests/test_manager_loop_service.py
@@ -1457,3 +1457,16 @@
 
     assert entry.wake is None
     assert consumer.status()["running"] is False
+
+
+def test_events_carry_a_null_partial_once_the_turn_is_over(monkeypatch: Any, tmp_path: Path) -> None:
+    _install_fakes(monkeypatch)
+    manager_loop_service.start(tmp_path, "fake", "model-a")
+    manager_loop_service.send(tmp_path, "one")
+    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
+    session_id = manager_loop_service.status(tmp_path)["session"]["session_id"]
+
+    result = manager_loop_service.events(tmp_path, session_id)
+
+    assert "partial" in result
+    assert result["partial"] is None
```

