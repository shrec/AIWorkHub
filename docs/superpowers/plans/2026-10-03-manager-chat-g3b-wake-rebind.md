# Manager Chat G3b: a wake re-binds a restored session onto its own route

Base commit: `27f6484`. Card: `AIWORKHUB_CONSOLE_G3B_WAKE_REBIND_OWN_ROUTE_V1`.

## The defect (measured on the base commit)

`manager_loop_service.restore` and `continue_session` attach a saved conversation
without a backend: the orchestrator holds the session, `_backend` is `None`.
`_ensure_wake_started` still starts the wake consumer for such a session when it
holds the manager seat. When a callback is then claimed, `_wake_dispatch` runs
`orchestrator.wake(member)`, `_turn` calls `_active()` and that raises
`ManagerLoopError("no_active_manager_session")`. The turn counts as undelivered,
the callback stays un-acked, the consumer backs off and retries the same failure.
After every server restart or panel reload no callback reaches the model until
the owner types a message by hand.

Reproduction: the two new `rebinds` tests below fail on the base commit (the
route authority is never asked, `called.wait(timeout=5)` is False); the third
new test passes on the base commit and pins the behaviour that must not change.

## The change

1. `ManagerOrchestrator.bound` (new read-only property): whether the active
   session has its backend.
2. `manager_loop_service._wake_on_own_route` (new): before the wake turn, a
   non-passive session with no backend is re-bound through
   `orchestrator.continue_on_route` onto the route the session itself persisted,
   and only when `authorize_selected_route` still returns that route. A refusal
   raises `ManagerLoopError("manager_backend_unavailable:<backend>:<model>")`
   inside the turn thread, so the existing path applies unchanged: undelivered,
   un-acked, `failed_turns` grows, the consumer backs off.
3. `_wake_dispatch` passes `_wake_on_own_route` as the turn action. Nothing else
   in `_dispatch_turn` changes.

## What must stay exactly as it is

- A session that already has its backend is never sent through
  `authorize_selected_route` on a wake (sessions opened with `start` may run a
  route the catalog does not declare; asking would break their wakes).
- No route other than the session's own `backend_id`/`model` is ever bound by a
  wake. No call to `resolve_manager_route`, no fallback route.
- A callback is acknowledged only for a delivered turn.
- `_dispatch_turn`, `_ensure_wake_started`, `WakeConsumer` and every existing
  test stay untouched; no existing test name is removed or renamed.
- Do not edit `tests/test_manager_loop_service.py`; `_seat_record` is imported
  from it as it is.

## Verified result

Applied to a full archive of the base commit: `tests/test_manager_loop_wake.py`,
`tests/test_manager_loop_service.py`, `tests/test_manager_loop.py`,
`tests/test_manager_loop_backends.py`, `tests/test_server.py`,
`tests/test_declared_invariants.py`, `tests/test_module_size_ratchet.py`,
`tests/test_os_dependency_boundary.py` and
`tests/test_aiworkhub_dependency_autolaunch_b905_v7.py` all pass; `ruff check`
is clean on the three changed files. Resulting line counts:
`src/aiworkhub/manager_loop.py` 1427, `src/aiworkhub/manager_loop_service.py`
776, `tests/test_manager_loop_wake.py` 926.

## The literal change

Apply exactly this. Do not re-explore the design and do not reformat
neighbouring code.

```diff
--- a/src/aiworkhub/manager_loop.py
+++ b/src/aiworkhub/manager_loop.py
@@ -710,6 +710,11 @@
         """The active session, or ``None`` between sessions."""
         return self._session
 
+    @property
+    def bound(self) -> bool:
+        """Whether the active session has its backend; one loaded from disk has none yet."""
+        return self._session is not None and self._backend is not None
+
     def ensure(self) -> ManagerSession:
         """Attach to the repository's one active conversation, persisting a passive one if none.
 
--- a/src/aiworkhub/manager_loop_service.py
+++ b/src/aiworkhub/manager_loop_service.py
@@ -639,6 +639,29 @@
     return not thread.is_alive()
 
 
+def _wake_on_own_route(
+    repo: str | Path, orchestrator: ManagerOrchestrator, member: Mapping[str, Any]
+) -> dict[str, Any]:
+    """Run one callback's turn; a session loaded from disk gets its backend back first.
+
+    Restore and continue attach a conversation without a backend, and a wake has
+    no owner present to pick one. So it binds only the route the session itself
+    persisted, and only while this repository still authorizes that route. A
+    refusal raises: the turn fails undelivered, its callback stays un-acked and
+    the consumer backs off. A session that already has its backend is not asked.
+    """
+
+    session = orchestrator.session
+    if session is not None and not session.passive and not orchestrator.bound:
+        route = authorize_selected_route(repo, session.backend_id, session.model)
+        if route is None:
+            raise ManagerLoopError(
+                f"manager_backend_unavailable:{session.backend_id}:{session.model}"
+            )
+        orchestrator.continue_on_route(*route)
+    return orchestrator.wake(member)
+
+
 def _wake_dispatch(
     repo: str | Path, member: Mapping[str, Any], done: Callable[[bool], None]
 ) -> bool:
@@ -649,7 +672,7 @@
 
     result = _dispatch_turn(
         repo,
-        lambda orchestrator: orchestrator.wake(member),
+        lambda orchestrator: _wake_on_own_route(repo, orchestrator, member),
         record_last_turn=True,
         on_finished=done,
     )
--- a/tests/test_manager_loop_wake.py
+++ b/tests/test_manager_loop_wake.py
@@ -19,7 +19,7 @@
 from aiworkhub import manager_loop_wake  # noqa: E402
 from aiworkhub.manager_loop_wake import WakeConsumer  # noqa: E402
 
-from test_manager_loop_service import _install_fakes  # noqa: E402
+from test_manager_loop_service import _install_fakes, _seat_record  # noqa: E402
 
 
 class _FakeClock:
@@ -826,3 +826,101 @@
         assert status["wake"]["failed_turns"] == 0
     finally:
         assert manager_loop_service.close(tmp_path)["ok"] is True
+
+
+class _RouteAuthority:
+    """Stands in for ``authorize_selected_route``: records each ask, answers ``allowed``."""
+
+    def __init__(self, allowed: bool) -> None:
+        self.allowed = allowed
+        self.asked: list[tuple[str, str]] = []
+        self.called = threading.Event()
+
+    def __call__(self, repo: Any, backend_id: str, model: str) -> tuple[str, str] | None:
+        self.asked.append((backend_id, model))
+        self.called.set()
+        return (backend_id, model) if self.allowed else None
+
+
+def _claim_one_callback(monkeypatch: Any, authority: _RouteAuthority) -> tuple[list[Any], list[tuple[str, str]]]:
+    backends = _install_fakes(monkeypatch)
+    monkeypatch.setattr(manager_loop_service, "WAKE_IDLE_POLL_SECONDS", 0.02)
+    monkeypatch.setattr(manager_loop_service, "WAKE_RETRY_POLL_SECONDS", 0.02)
+    monkeypatch.setattr(manager_loop_service, "authorize_selected_route", authority)
+    ack_calls: list[tuple[str, str]] = []
+
+    def ack(batch_id: str, lease_id: str) -> bool:
+        ack_calls.append((batch_id, lease_id))
+        return True
+
+    _install_fake_wake_source(monkeypatch, _one_batch_claim(["CARD_A"]), ack)
+    return backends, ack_calls
+
+
+def test_a_wake_rebinds_a_restored_session_onto_its_own_authorized_route(
+    monkeypatch: Any, tmp_path: Path
+) -> None:
+    # A session loaded from disk (a server restart, a panel reload) holds the seat
+    # but has no backend yet. Its callback must still reach the model.
+    authority = _RouteAuthority(allowed=True)
+    backends, ack_calls = _claim_one_callback(monkeypatch, authority)
+    entry = manager_loop_service._entry_for(tmp_path)
+    restored = _seat_record(entry, "mls-" + "0d" * 16)
+
+    assert manager_loop_service.restore(tmp_path)["ok"] is True
+    try:
+        assert authority.called.wait(timeout=5)
+        assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
+        assert authority.asked == [("fake", "model-a")]
+        assert [(backend.backend_id, backend.model) for backend in backends] == [("fake", "model-a")]
+        assert backends[0].messages == ["callback: CARD_A -> s"]
+        assert ack_calls == [("b1", "l1")]
+        status = manager_loop_service.status(tmp_path)
+        assert status["last_turn"]["ok"] is True
+        assert status["session"]["session_id"] == restored.session_id
+        assert status["wake"]["failed_turns"] == 0
+    finally:
+        assert manager_loop_service.close(tmp_path)["ok"] is True
+
+
+def test_a_wake_never_rebinds_a_restored_session_onto_a_route_policy_no_longer_allows(
+    monkeypatch: Any, tmp_path: Path
+) -> None:
+    authority = _RouteAuthority(allowed=False)
+    backends, ack_calls = _claim_one_callback(monkeypatch, authority)
+    entry = manager_loop_service._entry_for(tmp_path)
+    _seat_record(entry, "mls-" + "0e" * 16)
+
+    assert manager_loop_service.restore(tmp_path)["ok"] is True
+    try:
+        assert authority.called.wait(timeout=5)
+        assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
+        assert backends == []
+        assert ack_calls == []
+        status = manager_loop_service.status(tmp_path)
+        assert status["last_turn"]["ok"] is False
+        assert "manager_backend_unavailable:fake:model-a" in status["last_turn"]["errors"][0]
+        assert status["wake"]["failed_turns"] == 1
+        assert status["wake"]["queued"] == 1
+        assert authority.asked == [("fake", "model-a")]
+    finally:
+        assert manager_loop_service.close(tmp_path)["ok"] is True
+    assert ack_calls == []
+
+
+def test_a_wake_on_a_session_that_already_has_its_backend_asks_for_no_authorization(
+    monkeypatch: Any, tmp_path: Path
+) -> None:
+    authority = _RouteAuthority(allowed=False)
+    backends, ack_calls = _claim_one_callback(monkeypatch, authority)
+
+    assert manager_loop_service.start(tmp_path, "fake", "model-a")["ok"] is True
+    try:
+        assert backends[0].entered.wait(timeout=5)
+        assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
+        assert authority.asked == []
+        assert len(backends) == 1
+        assert backends[0].messages == ["callback: CARD_A -> s"]
+        assert ack_calls == [("b1", "l1")]
+    finally:
+        assert manager_loop_service.close(tmp_path)["ok"] is True
```
