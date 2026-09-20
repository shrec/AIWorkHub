# Model-neutral SDLC Case Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every model the same repository-bound, durable Plan/Design/Build/Test/Deploy/Maintain case data and typed stage decisions through AIWorkHub MCP.

**Architecture:** A focused SQLite case store records append-only stage receipts and references existing task, NeedFix, Roadmap, and release identities without replacing their lifecycles. MCP wrappers verify the repository and manager capability, return bounded typed packets, and enforce stage ordering server-side. This is the first independently testable sub-project of the full design; Semantic Review, LSP, reasoning evaluation, and Muse qualification have separate plans.

**Tech Stack:** Python 3.12+, stdlib `sqlite3`/`hashlib`/`json`, existing FastMCP server, pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-model-neutral-sdlc-design.md`

## Global Constraints

- Canonical data lives only under the verified repository's `.aiworkhub/`; reject mismatched `repo_id` before any write.
- `AIWORKHUB_ALLOW_WRITES=1` and existing verified manager capability gate every write; no model/provider receives a private bypass.
- Stage evidence is append-only and idempotent by request identity; no historical or missing receipt is manufactured.
- `UNKNOWN` is not `ready`; `not_applicable` requires a typed reason and policy evidence.
- Return bounded stage packets with digests and source references; Markdown is optional export, never the gate.
- No task queue, NeedFix, or Roadmap lifecycle is copied into the new store.

## Review Focus

1. A case created for another `repo_id` must be refused before mutation (Task 1 cross-repo test).
2. A request ID replayed with different stage bytes must conflict rather than overwrite (Task 1 idempotency test).
3. A `deploy` receipt supplied before `test` is ready must be refused with the missing predecessor (Task 1 ordering test).
4. A `not_applicable` stage without a reason or policy reference must be refused (Task 1 typed-reason test).
5. An MCP write with `AIWORKHUB_ALLOW_WRITES` disabled must leave the case store absent/unchanged (Task 2 write-gate test).

---

### Task 1: Repository-bound append-only case store

**Files:**
- Create: `src/aiworkhub/sdlc_case_store.py`
- Create: `tests/test_sdlc_case_store.py`

**Interfaces:**
- Produces `create_case(repo_root: Path, repo_id: str, case_id: str, request_id: str, links: dict[str, str]) -> dict[str, Any]`.
- Produces `append_stage(repo_root: Path, repo_id: str, case_id: str, stage: str, state: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]`.
- Produces `read_case(repo_root: Path, repo_id: str, case_id: str) -> dict[str, Any]` and `stage_packet(repo_root: Path, repo_id: str, case_id: str, stage: str) -> dict[str, Any]`.
- Produces `SdlcCaseConflict` for identity/replay conflicts and `SdlcCaseValidationError` for invalid stage/state/payload or missing predecessor.

- [ ] **Step 1: Write the failing store tests.** Create a temporary repository root with `bootstrap_repository` and its canonical `repo_id`. Assert that `create_case` records one case; `append_stage(..., "plan", "ready", {"intent": "x", "evidence_refs": ["file:README.md"]}, "R1")` records a digest; repeating `R1` with identical bytes is idempotent; repeating `R1` with different bytes raises `SdlcCaseConflict`. Assert that a different `repo_id` cannot read or append, `deploy` before `test` is refused, and `not_applicable` without `reason`/`policy_ref` is refused. Use one pytest function per invariant, not one giant fixture assertion.

```python
@pytest.fixture
def case_repo(tmp_path):
    bootstrap_repository(tmp_path, repo_name="sdlc-case-test")
    readiness = task_store.storage_readiness(tmp_path)
    assert readiness.ready
    create_case(tmp_path, readiness.repo_id, "C1", "R-create", {})
    return SimpleNamespace(root=tmp_path, repo_id=readiness.repo_id)

def test_same_request_replay_is_idempotent(case_repo):
    first = append_stage(case_repo.root, case_repo.repo_id, "C1", "plan", "ready",
                         {"intent": "x", "evidence_refs": ["file:README.md"]}, "R1")
    second = append_stage(case_repo.root, case_repo.repo_id, "C1", "plan", "ready",
                          {"intent": "x", "evidence_refs": ["file:README.md"]}, "R1")
    assert second["receipt_sha256"] == first["receipt_sha256"]
    assert second["idempotent"] is True

def test_cross_repository_case_is_refused(case_repo):
    with pytest.raises(SdlcCaseConflict):
        read_case(case_repo.root, "repo_foreign", "C1")

def test_deploy_requires_test_predecessor(case_repo):
    for stage in ("plan", "design", "build"):
        append_stage(case_repo.root, case_repo.repo_id, "C1", stage, "ready",
                     {"evidence_refs": ["file:README.md"]}, "R-" + stage)
    with pytest.raises(SdlcCaseValidationError, match="test"):
        append_stage(case_repo.root, case_repo.repo_id, "C1", "deploy", "ready",
                     {"target": "staging"}, "R-deploy")

def test_not_applicable_requires_reason_and_policy(case_repo):
    with pytest.raises(SdlcCaseValidationError, match="policy_ref"):
        append_stage(case_repo.root, case_repo.repo_id, "C1", "plan", "not_applicable",
                     {"reason": "already approved"}, "R-na")
```

- [ ] **Step 2: Run the new tests red.** Run `python3 -m pytest -q tests/test_sdlc_case_store.py`; expected failure is the missing `sdlc_case_store` interface, not a missing fixture/toolchain.
- [ ] **Step 3: Implement the store.** Use `.aiworkhub/sdlc/cases.sqlite`, a `cases` table bound to `repo_id`, and an append-only `stage_receipts` table with unique `(case_id, request_id)`. Canonical JSON uses `sort_keys=True` and compact separators before SHA-256. Open a write transaction with `BEGIN IMMEDIATE`; validate case identity and predecessor before insertion; identical replay returns the stored digest, conflicting replay raises `SdlcCaseConflict`. `read_case` selects latest receipt per stage and limits payload size; `stage_packet` returns `unknown` when no receipt exists. The stage order is `plan, design, build, test, deploy, maintain`, with typed `not_applicable` receipt permitted only when both `reason` and `policy_ref` are non-empty.

```python
STAGES = ("plan", "design", "build", "test", "deploy", "maintain")
STATES = frozenset({"ready", "blocked", "unknown", "not_applicable"})
def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
```

- [ ] **Step 4: Run green and concurrency checks.** Run `python3 -m pytest -q tests/test_sdlc_case_store.py`; add two-process/simultaneous-request coverage proving a single durable receipt and no torn JSON, then rerun the exact command.
- [ ] **Step 5: Commit only the new module and tests.** `git add src/aiworkhub/sdlc_case_store.py tests/test_sdlc_case_store.py` then `git commit -m "feat: add repository-bound SDLC case receipts"`.

### Task 2: Manager MCP case surface with existing authority gate

**Files:**
- Modify: `src/aiworkhub/core.py` near existing manager task lifecycle wrappers.
- Modify: `src/aiworkhub/server.py` near `aiworkhub_manager_sdlc_outcome_metrics` (currently around line 4778).
- Create: `tests/test_sdlc_case_mcp.py`

**Interfaces:**
- Consumes the four Task 1 functions.
- Produces `aiworkhub_manager_sdlc_case_create(case_id, request_id, links)`, `aiworkhub_manager_sdlc_stage_record(case_id, stage, state, payload, request_id)`, `aiworkhub_manager_sdlc_case_get(case_id)`, and `aiworkhub_manager_sdlc_stage_packet(case_id, stage)`.
- Write wrappers bind `repo_id` from `task_store.storage_readiness(core.repo_root())`, not from caller arguments. They use the same `_canonical_write_gate` and verified manager-capability check as `core.create_task`; read wrappers do not mutate.

- [ ] **Step 1: Write failing MCP tests.** In a bootstrapped temp repo, invoke the server functions with write gate off and prove no `cases.sqlite` is created. With the gate on and verified manager identity mocked, create `C1`, record Plan ready, and get the same digest through case/packet reads. Verify the public tool schemas have no caller-controlled `repo_id` and that an unknown stage returns a typed refusal.

```python
@pytest.fixture
def case_repo(tmp_path, monkeypatch):
    bootstrap_repository(tmp_path, repo_name="sdlc-case-mcp-test")
    readiness = task_store.storage_readiness(tmp_path)
    assert readiness.ready
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return SimpleNamespace(root=tmp_path, repo_id=readiness.repo_id)

def test_manager_case_write_respects_write_gate(monkeypatch, case_repo):
    monkeypatch.delenv("AIWORKHUB_ALLOW_WRITES", raising=False)
    result = server.aiworkhub_manager_sdlc_case_create("C1", "R-create", {})
    assert result["ok"] is False
    assert not (case_repo.root / ".aiworkhub/sdlc/cases.sqlite").exists()
```

- [ ] **Step 2: Run red.** `python3 -m pytest -q tests/test_sdlc_case_mcp.py`; expected failure is absent MCP functions.
- [ ] **Step 3: Add thin wrappers.** Keep SQL in `sdlc_case_store.py`. In `core.py`, check verified manager identity/capability and `_canonical_write_gate("sdlc-case")` before calling store writes. In `server.py`, register the four typed tools; cap returned payloads and return exact `repo_id`, case ID, stage, state, digest, and refusal reason. No provider name branches are added to stage semantics.

```python
@mcp.tool()
def aiworkhub_manager_sdlc_stage_packet(case_id: str, stage: str) -> dict[str, Any]:
    """READ-ONLY: one bounded repository-bound stage packet."""
    return core.sdlc_stage_packet(case_id=case_id, stage=stage)
```

- [ ] **Step 4: Run green and existing gate regressions.** `python3 -m pytest -q tests/test_sdlc_case_mcp.py tests/test_sdlc_outcome_metrics.py tests/test_server.py tests/test_needfix_markdown_ingest.py`.
- [ ] **Step 5: Commit only the declared files.** `git add src/aiworkhub/core.py src/aiworkhub/server.py tests/test_sdlc_case_mcp.py` then `git commit -m "feat: expose SDLC case receipts through manager MCP"`.

## Follow-on boundaries

The next separate plans bind cases to task creation/launch and to deploy/maintain receipts, then add worker packet reads and the UI projection. OpenCode manager identity/callback (NF-2026-00901) must also close before any-model manager coverage is claimed. No transition in this plan should be described as full playbook enforcement. Semantic Review, LSP, reasoning evaluation, and Muse qualification have their own plans and outcome gates from the shared design spec.
