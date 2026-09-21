# Automatic Wave Mini-Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In AIWorkHub, workers stop at Codex review; the verified manager, not the worker, accepts into the canonical tree.

**Goal:** Show the current wave and its exact progress without manually rewriting release numbers or task links.

**Architecture:** A read-only projection separates installed runtime version from the Roadmap target milestone. Explicit task successor bindings update a wave goal's current task identity under a write gate. The reconciler closes a wave only after all declared evidence is accepted; release installation itself never moves a target or checks a goal.

**Tech Stack:** Python 3.12+, SQLite, AIWorkHub Task/Roadmap MCP, VS Code extension JavaScript, pytest, Node test runner.

**Spec:** `docs/superpowers/specs/2026-09-21-automatic-wave-mini-roadmap-design.md`

## Global Constraints

- Repository task and context truth stays in this repository's `.aiworkhub/`.
- Every write requires `AIWORKHUB_ALLOW_WRITES=1`; dashboard reads never repair state.
- No successor relation is inferred from titles, topics, version suffixes, or prose.
- A release version does not change a goal verdict or silently move a target milestone.
- Unknown, missing, archived, blocked, or ambiguous task evidence cannot check a goal.
- Existing-file edits use the smallest Source-Graph-verified semantic-edit range.

## Review Focus

- Two active waves at the same target must produce UNKNOWN, not an arbitrary winner (Task 1 test).
- A release past a target with unfinished work must say overdue, not retarget or complete (Task 1 test).
- A successor claiming a predecessor in another goal or repository must fail closed (Task 2 test).
- Repeated and concurrent binding scans must not duplicate task links/events (Task 2 test).
- A wave with unmapped acceptance criteria must not auto-complete (Task 4 test).

---

### Task 1: Read-only current-wave projection

**Files:**
- Create: `src/aiworkhub/wave_roadmap.py`
- Modify: `src/aiworkhub/dashboard_mcp_app.py` (`roadmap_list_view`, `roadmap_detail_view`)
- Test: `tests/test_wave_roadmap.py`, `tests/test_dashboard_mcp_app.py`

**Interfaces:**
- Consumes: `roadmap_store.list_items`, `core.roadmap_snapshot`, canonical runtime `__version__`.
- Produces: `project_current_wave(rows: Sequence[Mapping[str, Any]], installed_version: str) -> dict[str, Any]`; list/detail responses expose `current_wave`, `installed_version`, `target_milestone`, `overdue`, and typed `selection_reason`.

- [ ] **Step 1: Write failing tests** for installed `0.11.53` plus active target `0.11.51` with unfinished linked task: assert installed and target remain distinct, `overdue is True`, and no Roadmap rows/events change. Add two same-target active waves, invalid version, and no active wave: each returns `state == "UNKNOWN"`.
- [ ] **Step 2: Run red tests:** `python3 -m pytest -q tests/test_wave_roadmap.py tests/test_dashboard_mcp_app.py` must fail on missing projection fields before implementation.
- [ ] **Step 3: Implement the pure selector/projection.** Its shape is:

```python
import re

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

def _parts(value):
    match = _VERSION.fullmatch(str(value or ""))
    return tuple(map(int, match.groups())) if match else None

def project_current_wave(rows, installed_version):
    installed = _parts(installed_version)
    candidates = [(_parts(row.get("milestone")), row) for row in rows
                  if row.get("status") == "in_progress" and row.get("provenance", {}).get("wave_goals")]
    candidates = [(version, row) for version, row in candidates if version is not None]
    if installed is None or not candidates:
        return {"state": "UNKNOWN", "selection_reason": "missing_version_or_active_wave"}
    highest = max(version for version, _ in candidates)
    winners = [row for version, row in candidates if version == highest]
    if len(winners) != 1:
        return {"state": "UNKNOWN", "selection_reason": "ambiguous_active_wave"}
    selected = winners[0]
    status_by_id = {task["task_id"]: task["status"] for task in selected.get("tasks", [])}
    goals = []
    for goal in selected["provenance"]["wave_goals"]:
        states = [status_by_id.get(task_id, "missing") for task_id in goal.get("task_ids", [])]
        state = "UNKNOWN" if not states or "missing" in states else "checked" if all(value == "finished" for value in states) else "open"
        goals.append({"id": goal["id"], "label": goal["label"], "state": state})
    unfinished = any(goal["state"] != "checked" for goal in goals)
    return {"state": "ready", "wave_id": selected["id"], "installed_version": installed_version,
            "target_milestone": selected["milestone"], "overdue": installed > highest and unfinished,
            "goals": goals}
```

- [ ] **Step 4: Wire list/detail responses** to the same projection and keep existing bounded/truncated behavior; a truncated list must report UNKNOWN.
- [ ] **Step 5: Run green checks:** focused pytest, `python3 -m ruff check src/aiworkhub/wave_roadmap.py src/aiworkhub/dashboard_mcp_app.py tests/test_wave_roadmap.py tests/test_dashboard_mcp_app.py`, and `git diff --check`. Stop at manager review.

### Task 2: Exact successor-task goal binding

**Files:**
- Modify: `src/aiworkhub/core.py` (`create_task` contract)
- Modify: `src/aiworkhub/server.py` (`aiworkhub_task_create` surface)
- Modify: `src/aiworkhub/roadmap_store.py` (atomic goal binding)
- Modify: `src/aiworkhub/task_reconciler.py` (bounded pending-binding repair)
- Test: `tests/test_roadmap_store.py`, `tests/test_task_reconciler.py`, `tests/test_server_runtime_wiring.py`

**Interfaces:**
- Consumes: existing `provenance.wave_goals[{id,label,task_ids}]` and exact task IDs.
- Produces: optional `wave_goal_binding={"roadmap_id": str, "goal_id": str, "predecessor_task_id": str}` on a card; `bind_goal_successor(repo_root, *, roadmap_id, goal_id, predecessor_task_id, successor_task_id) -> dict` returns applied/already_applied/refused with a durable event. Replace the predecessor in one goal's task list, preserving its other prerequisites; keep the predecessor in the outcome-wide task history.

- [ ] **Step 1: Write failing tests.** Create a wave with goal `lsp` linked to old V1, then create V2 with the exact binding: assert V2 becomes the current task and V1 remains historical. A foreign wave, wrong goal, missing predecessor, or two successors for the same predecessor must refuse. Repeating the same request must add no second event. With writes disabled, card creation reports binding pending and no Roadmap mutation.
- [ ] **Step 2: Run red tests:** `python3 -m pytest -q tests/test_roadmap_store.py tests/test_task_reconciler.py tests/test_server_runtime_wiring.py`.
- [ ] **Step 3: Add one bounded, validated card field** at create time and pass it through the MCP surface without changing old callers. Store the exact binding on the canonical card before attempting the Roadmap update; do not infer a binding from card text.

```python
# New optional create_task / MCP argument; reject extra keys and foreign IDs.
wave_goal_binding = {
    "roadmap_id": "RM-2026-00066",
    "goal_id": "lsp",
    "predecessor_task_id": "AIWORKHUB_01151_LSP_INDEX_INTEGRATION_V3_GLM53_RECOVERED",
}
card["wave_goal_binding"] = wave_goal_binding
```

- [ ] **Step 4: Add an idempotent Roadmap transaction.** Check that predecessor is the goal's current task, task IDs are same-repository canonical records, and expected wave status is active; update goal task IDs plus outcome task IDs and write one event. A retry after a cross-store failure must converge to the same result.

```python
def bind_goal_successor(repo_root, *, roadmap_id, goal_id, predecessor_task_id, successor_task_id):
    predecessor = task_store.get_task(repo_root, predecessor_task_id)
    successor = task_store.get_task(repo_root, successor_task_id)
    if predecessor is None or successor is None:
        raise RoadmapConflictError("foreign_or_missing_task")
    conn = _connect(repo_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        wave = get_item(repo_root, roadmap_id, _connection=conn)
        if wave["status"] != "in_progress":
            raise RoadmapConflictError("wave_not_active")
        goals = wave["provenance"]["wave_goals"]
        matching = [row for row in goals if row.get("id") == goal_id]
        if len(matching) != 1:
            raise RoadmapConflictError("goal_missing_or_ambiguous")
        goal = matching[0]
        current = goal["task_ids"]
        if successor_task_id in current and predecessor_task_id not in current:
            conn.commit()
            return {"state": "already_applied", "wave": wave}
        if current.count(predecessor_task_id) != 1 or successor_task_id in current:
            raise RoadmapConflictError("predecessor_not_current")
        goal["task_ids"] = [successor_task_id if value == predecessor_task_id else value for value in current]
        history = list(dict.fromkeys([*wave["task_ids"], successor_task_id]))
        conn.execute("UPDATE roadmap_items SET provenance_json=?,task_ids_json=?,updated_at=? WHERE id=?",
                     (json.dumps(wave["provenance"]), json.dumps(history), _utcnow(), roadmap_id))
        _event(conn, roadmap_id, "wave_goal_successor_bound", {"goal_id": goal_id, "from": predecessor_task_id, "to": successor_task_id})
        result = get_item(repo_root, roadmap_id, _connection=conn)
        conn.commit()
        return {"state": "applied", "wave": result}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
```

- [ ] **Step 5: Reconcile only cards with durable unapplied bindings** under the write gate; do not scan unrelated historical cards on every cycle. Keep the read path side-effect free.
- [ ] **Step 6: Run green checks:** focused pytest, Ruff on changed Python files, `git diff --check`, and `python3 -m pytest -q tests/test_module_size_ratchet.py tests/test_declared_invariants.py`. Stop at manager review.

### Task 3: Render authoritative wave state in the popup

**Files:**
- Modify: `vscode-extension/media/app.js` (`renderWaveMiniRoadmap`, roadmap response handlers)
- Test: `vscode-extension/test/wave-mini-roadmap.test.js`

**Interfaces:**
- Consumes: Task 1 list/detail projection fields; exact task status rows from the server.
- Produces: version-neutral title, installed/target/overdue line, short goal checkboxes, UNKNOWN state when projection is ambiguous or unavailable.

- [ ] **Step 1: Write failing UI tests.** Feed the 0.11.51 target / 0.11.53 installed fixture and assert the popup does not call it an installed 0.11.51 wave. Feed two active waves, missing detail, and an archived predecessor with pending successor: assert UNKNOWN/open, never checked.
- [ ] **Step 2: Run red test:** `node --test vscode-extension/test/wave-mini-roadmap.test.js`.
- [ ] **Step 3: Replace local highest-semver selection** with the server's exact `current_wave.wave_id` receipt. Render `Installed 0.11.53 · Target 0.11.51 (overdue)` from separate fields. Do not derive target from installed version or synthesize a goal verdict in the UI.

```javascript
const current = response.current_wave;
if (!current || current.state !== "ready") return renderWaveMiniRoadmapState(content, "UNKNOWN", current?.selection_reason || "Wave unavailable");
const selected = entries.find((row) => row.id === current.wave_id);
if (!selected) return renderWaveMiniRoadmapState(content, "UNKNOWN", "Active wave detail unavailable");
const versionLine = `Installed ${current.installed_version} · Target ${current.target_milestone}${current.overdue ? " (overdue)" : ""}`;
```

- [ ] **Step 4: Run green checks:** the wave test, extension static tests required by the package, and `git diff --check`. Stop at manager review.

### Task 4: Evidence-gated automatic wave completion

**Files:**
- Modify: `src/aiworkhub/wave_roadmap.py`, `src/aiworkhub/roadmap_store.py`, `src/aiworkhub/task_reconciler.py`
- Test: `tests/test_wave_roadmap.py`, `tests/test_roadmap_store.py`, `tests/test_task_reconciler.py`

**Interfaces:**
- Consumes: Task 1 projection, Task 2 exact current-task bindings, and each goal's `acceptance_indices: list[int]` (1-based indexes into the Roadmap acceptance list). Every mapped criterion is proved by the goal's accepted task cards; measured checks belong in those cards' validation contracts.
- Produces: `reconcile_wave_completion(repo_root, wave_id) -> dict` with `completed`, `pending_evidence`, or `unknown`; only `completed` performs a guarded Roadmap transition.

- [ ] **Step 1: Write failing tests.** All mapped goal tasks accepted with complete verifier receipts closes the wave once; an archived predecessor, missing task, zero goals, or an acceptance criterion without a mapped verifier leaves it in progress with typed UNKNOWN. A later release alone never closes it.
- [ ] **Step 2: Run red tests:** `python3 -m pytest -q tests/test_wave_roadmap.py tests/test_roadmap_store.py tests/test_task_reconciler.py`.
- [ ] **Step 3: Add the guarded reconciliation** on the active owner path. Verify every declared acceptance criterion maps to a goal whose exact current tasks are all canonically accepted before calling `roadmap_store.transition_item(repo_root, wave_id, "completed", reason=bounded_evidence_summary)`. Make a repeated/concurrent scan idempotent.

```python
def reconcile_wave_completion(repo_root, wave_id):
    wave = roadmap_store.get_item(repo_root, wave_id)
    goals = wave.get("provenance", {}).get("wave_goals", [])
    covered = {index for goal in goals for index in goal.get("acceptance_indices", [])}
    if not goals or covered != set(range(1, len(wave["acceptance"]) + 1)):
        return {"state": "unknown", "reason": "acceptance_coverage_missing"}
    task_ids = {task_id for goal in goals for task_id in goal["task_ids"]}
    if not task_ids or any(task_store.canonical_status(task_store.get_task(repo_root, task_id)) != "finished" for task_id in task_ids):
        return {"state": "pending_evidence", "task_ids": sorted(task_ids)}
    result = roadmap_store.transition_item(repo_root, wave_id, "completed", reason="all mapped wave goals have accepted exact tasks")
    return {"state": "completed", "wave": result}
```

- [ ] **Step 4: Run green checks:** focused pytest, Ruff, module-size and declared-invariant ratchets, `git diff --check`, and a full dashboard read showing a completed wave no longer selected as active. Stop at manager review.

## Integration gate

After all four tasks are accepted, replay the 0.11.51→0.11.53 fixture and the live RM-2026-00066 read. Confirm installed/target are distinct, every checkbox traces to one exact current task, no release-only completion occurred, and the successor binding persists after a process restart. Build/install the next intermediate VSIX only after Python and extension tests pass; then verify the popup against the installed MCP runtime.
