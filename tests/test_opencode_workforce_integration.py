from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    model_settings,
    repo_policy,
    runtime_adapters,
    task_store,
    worker_ai_tools_mcp,
    worker_workspace,
    workforce_catalog,
    workforce_router,
)

_FREE = "opencode/fixture-nano-free"
_PAID = "openai/fixture-gpt"
_OTHER = "anthropic/fixture-sonnet"
_FUTURE = "opencode/future-nano-free"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


def _initialized_root(tmp_path: Path) -> Path:
    root = tmp_path / "initialized"
    root.mkdir()
    task_store.initialize_repository(root)
    return root


def _listing(*identities: str) -> list[str]:
    return list(identities)


def _preflight(
    identities: list[str],
    *,
    launchable: bool = True,
    access_observed: bool = False,
    status: str = "installed_unverified_access",
) -> dict[str, object]:
    return {
        "providers": [
            {
                "adapter_id": "opencode_cli",
                "launchable": launchable,
                "access_observed": access_observed,
                "status": status,
                "provider_observed_models": list(identities),
                "observed_models": list(identities),
            }
        ]
    }


def _snapshot(
    tmp_path: Path,
    identities: list[str],
    *,
    cards: list[dict[str, object]] | None = None,
    process_rows: list[dict[str, object]] | None = None,
    usage_rows: list[dict[str, object]] | None = None,
    launchable: bool = True,
    access_observed: bool = False,
    status: str = "installed_unverified_access",
    now_epoch: float | None = None,
) -> dict[str, object]:
    return workforce_catalog.build_catalog(
        _repo(tmp_path),
        cards=cards or [],
        process_rows=process_rows or [],
        usage_rows=usage_rows or [],
        preflight=_preflight(
            identities,
            launchable=launchable,
            access_observed=access_observed,
            status=status,
        ),
        now_epoch=now_epoch,
    )


def _opencode_rows(snapshot: dict[str, object]) -> list[dict[str, object]]:
    return [
        row
        for row in snapshot["workers"]
        if row["adapter_id"] == "opencode_cli"
    ]


def test_parse_strips_ansi_and_rejects_malformed_duplicate_and_oversized_rows() -> None:
    ansi = "\x1b[32m" + _FREE + "\x1b[0m"
    raw = "\n".join(
        [
            ansi,
            _FREE,
            "not-an-identity",
            "openai/",
            "/missing-provider",
            "has space/model",
            _PAID,
            "x" * 200,
            json.dumps(["ignored-because-not-json-document"]),
        ]
    )
    parsed = workforce_catalog.parse_opencode_models_output(raw)
    assert parsed == [_FREE, _PAID]
    assert workforce_catalog.parse_opencode_models_output("a" * (65 * 1024)) == []
    assert workforce_catalog.parse_opencode_models_output(
        json.dumps([_FUTURE, _FUTURE, "bad"])
    ) == [_FUTURE]


def test_parse_preserves_exact_provider_model_identities() -> None:
    parsed = workforce_catalog.parse_opencode_models_output(
        f"{_PAID}\n{_FREE}\n{_OTHER}\n"
    )
    assert parsed == [_PAID, _FREE, _OTHER]
    for identity in parsed:
        resolved, error = runtime_adapters.resolve_opencode_model(identity)
        assert error is None
        assert resolved == identity


def test_catalog_one_row_per_discovered_identity_with_stable_runners(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, [_FREE, _PAID, _FREE, _OTHER])
    rows = _opencode_rows(snapshot)
    models = [row["model"] for row in rows]
    assert models == [_FREE, _PAID, _OTHER]
    assert all(row["adapter_id"] == "opencode_cli" for row in rows)
    runners = [row["execution_runner"] for row in rows]
    assert len(set(runners)) == 3
    assert all(runner.startswith("opencode_") for runner in runners)
    by_model = {row["model"]: row for row in rows}
    assert by_model[_PAID]["provider"] == "openai"
    assert by_model[_FREE]["provider"] == "opencode"
    assert by_model[_PAID]["execution_runner"] != by_model[_FREE]["execution_runner"]


def test_listing_never_sets_round_trip_or_access_observation(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, [_FREE, _PAID], access_observed=False)
    rows = _opencode_rows(snapshot)
    assert rows
    assert all(row["round_trip_observed"] == "unknown" for row in rows)
    assert all(row["availability_observed"] is False for row in rows)
    assert all(
        row["route_observation"]["reason"] == repo_policy.ROUTE_OBSERVATION_NEVER_RECORDED
        for row in rows
    )
    assert snapshot["truth_contract"]["startability_never_sets_round_trip_observed"] is True
    assert snapshot["truth_contract"]["unknown_cost_never_ranks_as_free"] is True


def test_default_only_opencode_free_identities_are_launch_eligible(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, [_FREE, _PAID, _OTHER])
    by_model = {row["model"]: row for row in _opencode_rows(snapshot)}
    assert by_model[_FREE]["policy_enabled"] is True
    assert by_model[_FREE]["launch_eligible"] is True
    assert by_model[_PAID]["policy_enabled"] is False
    assert by_model[_PAID]["launch_eligible"] is False
    assert by_model[_OTHER]["policy_enabled"] is False
    assert by_model[_OTHER]["launch_eligible"] is False
    assert model_settings.opencode_identity_default_enabled(_FREE) is True
    assert model_settings.opencode_identity_default_enabled(_PAID) is False
    assert model_settings.opencode_identity_default_enabled("opencode/unknown") is False


def test_openai_identity_stays_disabled_until_owner_enables(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    assert model_settings.policy_identity_for_adapter("opencode_cli") == (
        "opencode",
        "opencode_cli",
    )
    assert (
        model_settings.evaluate(
            root,
            provider="opencode",
            adapter="opencode_cli",
            model=_PAID,
        )
        is False
    )
    model_settings.update(
        root,
        provider="opencode",
        adapter="opencode_cli",
        model=_PAID,
        enabled=True,
        expected_revision=0,
    )
    assert (
        model_settings.evaluate(
            root,
            provider="opencode",
            adapter="opencode_cli",
            model=_PAID,
        )
        is True
    )
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight=_preflight([_FREE, _PAID]),
    )
    by_model = {row["model"]: row for row in _opencode_rows(snapshot)}
    assert by_model[_PAID]["policy_enabled"] is True
    assert by_model[_PAID]["launch_eligible"] is True


def test_repository_policy_can_deny_opencode_adapter(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    model_settings.update(
        root,
        provider="opencode",
        adapter="opencode_cli",
        enabled=False,
        expected_revision=0,
    )
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight=_preflight([_FREE]),
    )
    free = next(row for row in _opencode_rows(snapshot) if row["model"] == _FREE)
    assert free["policy_enabled"] is False
    assert free["launch_eligible"] is False


def test_legacy_allow_all_policy_gains_opencode_but_custom_denial_does_not(
    tmp_path: Path,
) -> None:
    root = _initialized_root(tmp_path)
    legacy = json.loads(json.dumps(repo_policy.DEFAULT_POLICY))
    legacy["providers"]["allowed_adapters"].remove("opencode_cli")
    path = repo_policy.policy_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    migrated = repo_policy.load_policy(root)
    assert "opencode_cli" in migrated["providers"]["allowed_adapters"]

    legacy["providers"]["allowed_adapters"].remove("claude_cli")
    path.write_text(json.dumps(legacy), encoding="utf-8")
    customized = repo_policy.load_policy(root)
    assert "opencode_cli" not in customized["providers"]["allowed_adapters"]


def test_exact_route_circuit_is_isolated_across_provider_models(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    epoch = now.timestamp()
    failed = {
        "adapter_id": "opencode_cli",
        "model": _PAID,
        "state": "failed",
        "finished_at": now.isoformat(),
        "error": {"code": "unauthorized"},
    }
    snapshot = _snapshot(
        tmp_path,
        [_FREE, _PAID],
        process_rows=[failed, dict(failed)],
        now_epoch=epoch,
    )
    by_model = {row["model"]: row for row in _opencode_rows(snapshot)}
    assert by_model[_FREE]["route_health"]["state"] == "closed"
    assert by_model[_FREE]["model"] == _FREE
    assert by_model[_PAID]["model"] == _PAID


def test_unknown_cost_never_ranks_as_free() -> None:
    task = workforce_router.TaskRequirements.build(
        task_id="T-cost",
        repo_id="repo",
        kinds=["code"],
        tool_needs=["filesystem"],
    )
    known = workforce_router.WorkerCapability.build(
        worker_id="opencode_known",
        adapter_id="opencode_cli",
        model=_PAID,
        provider="openai",
        supports=["code"],
        tools=["filesystem"],
        evidence=workforce_router.OutcomeEvidence(cost_usd_per_1k_tokens=0.02),
    )
    unknown = workforce_router.WorkerCapability.build(
        worker_id="opencode_unknown",
        adapter_id="opencode_cli",
        model=_FREE,
        provider="opencode",
        supports=["code"],
        tools=["filesystem"],
        evidence=workforce_router.OutcomeEvidence(),
    )
    decision = workforce_router.rank_workforce(task, [unknown, known])
    assert decision.selected_worker_id == "opencode_known"
    unknown_candidate = next(
        item for item in decision.candidates if item.worker_id == "opencode_unknown"
    )
    assert unknown_candidate.score_components["cost_known"] is False
    assert unknown_candidate.score_components["estimated_cost_usd"] is None


def test_future_discovered_model_needs_no_source_edit(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, [_FUTURE])
    rows = _opencode_rows(snapshot)
    assert [row["model"] for row in rows] == [_FUTURE]
    assert rows[0]["policy_enabled"] is True
    assert rows[0]["launch_eligible"] is True


def test_external_canary_is_not_imported_as_round_trip(tmp_path: Path) -> None:
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    snapshot = _snapshot(
        tmp_path,
        [_FREE],
        process_rows=[
            {
                "adapter_id": "vscode_lm",
                "model": _FREE,
                "state": "accepted",
                "finished_at": now.isoformat(),
            }
        ],
        now_epoch=now.timestamp(),
    )
    free = next(row for row in _opencode_rows(snapshot) if row["model"] == _FREE)
    assert free["round_trip_observed"] == "unknown"
    assert free["availability_observed"] is False


def test_simulated_windows_discovers_identities_without_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: True)
    snapshot = _snapshot(
        tmp_path,
        [_FREE, _PAID],
        launchable=False,
        status="sandbox_unavailable",
    )
    rows = _opencode_rows(snapshot)
    assert {row["model"] for row in rows} == {_FREE, _PAID}
    assert all(row["launch_eligible"] is False for row in rows)
    parsed = workforce_catalog.parse_opencode_models_output(
        f"{_FREE}\r\n{_PAID}\r\n"
    )
    assert parsed == [_FREE, _PAID]


def test_listing_probe_does_not_mark_access_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        repo_policy,
        "_list_opencode_models",
        lambda _executable: [_FREE, _PAID],
    )
    resolution = runtime_adapters.ExecutableResolution(
        "opencode_cli", "/tmp/opencode", True, ""
    )
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: resolution,
    )
    status = repo_policy._provider_status(
        Path("."),
        "opencode_cli",
        {
            "providers": {
                "allowed_adapters": list(
                    repo_policy.DEFAULT_POLICY["providers"]["allowed_adapters"]
                )
            }
        },
        "bubblewrap",
        "",
        # This verifies default OpenCode filtering, so it must not inherit the
        # checkout owner's repository-local provider switches.
        model_policy=model_settings.load(_repo(tmp_path)),
    )
    assert status["installed"] is True
    assert status["access_observed"] is False
    assert status["provider_observed_models"] == [_FREE, _PAID]
    assert _FREE in status["observed_models"]
    assert _PAID not in status["observed_models"]
    assert "round_trip_observed" not in status


def test_opencode_worker_mcp_config_never_bakes_repository_identity() -> None:
    # The generated worker MCP config is request-local (see
    # test_opencode_worker_mcp_config_is_request_local_and_secret_free in
    # tests/test_opencode_runtime_adapter.py); it must also never carry a
    # repository-identity env var, the same repository-neutral contract the
    # VS Code extension's application-global OpenCode MCP registration
    # enforces for repairOpencodeConfigJsonObject.
    config = runtime_adapters.build_opencode_worker_mcp_config(["python3", "-m", "aiworkhub.server"])
    serialized = json.dumps(config)
    assert "AIWORKHUB_REPO" not in serialized


# ---------------------------------------------------------------------------
# The VS Code extension's application-global OpenCode MCP registration is
# JavaScript, so a Python string-slice assertion can only prove the source
# *looks* right. The helper below executes the shipped functions in a real
# ``node`` child and asserts on their actual output, under the pytest command
# this repository declares as validation (``node --test`` is never run).
# ---------------------------------------------------------------------------

_EXTENSION_JS_PATH = Path(__file__).resolve().parents[1] / "vscode-extension" / "extension.js"
_REPO_IDENTITY_ENV_KEYS = ("AIWORKHUB_REPO_ROOT", "AIWORKHUB_REPO", "AIWORKHUB_REPO_ID")

# extension.js requires("vscode") at module scope; that module exists only
# inside a running VS Code extension host. None of the pure functions driven
# here touch vscode.* at require time, so a minimal stub loads the real module.
_NODE_DRIVER_PRELUDE = r"""
"use strict";
const Module = require("node:module");
const VSCODE_STUB_ID = "\0aiworkhub-vscode-stub";
const originalResolveFilename = Module._resolveFilename;
Module._resolveFilename = function patchedResolveFilename(request, ...rest) {
  if (request === "vscode") return VSCODE_STUB_ID;
  return originalResolveFilename.call(this, request, ...rest);
};
require.cache[VSCODE_STUB_ID] = {
  id: VSCODE_STUB_ID,
  filename: VSCODE_STUB_ID,
  loaded: true,
  exports: {
    workspace: {
      getConfiguration: () => ({ get: (_key, fallback) => fallback }),
      workspaceFolders: [],
    },
    window: {},
    commands: {},
    extensions: { getExtension: () => null },
    ConfigurationTarget: { Global: 1 },
  },
};
"""


def _drive_extension_internals(tmp_path: Path, expression: str):
    """Run ``expression`` against extension.js's real exported internals."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - node ships with the extension toolchain
        pytest.skip("node is required to execute vscode-extension/extension.js")
    script = tmp_path / "drive_extension_internals.js"
    script.write_text(
        _NODE_DRIVER_PRELUDE
        + f"const {{ __testInternals }} = require({json.dumps(str(_EXTENSION_JS_PATH))});\n"
        + f"const main = {expression};\n"
        + "process.stdout.write(JSON.stringify(main(__testInternals)));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_opencode_global_mcp_entry_is_created_repository_neutral(tmp_path: Path) -> None:
    result = _drive_extension_internals(
        tmp_path,
        """(internals) => internals.repairOpencodeConfigJsonObject({}, ["python3", "/launcher.py"])""",
    )
    assert result["changed"] is True
    entry = result["document"]["mcp"]["awh"]
    assert "aiworkhub" not in result["document"]["mcp"]
    assert entry["type"] == "local"
    assert entry["command"] == ["python3", "/launcher.py"]
    assert entry["enabled"] is True
    for key in _REPO_IDENTITY_ENV_KEYS:
        assert key not in entry["environment"]


def test_opencode_global_mcp_repair_sanitizes_both_owned_entries_and_picks_the_canonical_one(
    tmp_path: Path,
) -> None:
    # Two AIWorkHub-owned entries coexist and "aiworkhub_ultrafast" is declared
    # FIRST on purpose: repair must sanitize BOTH, and must re-point the
    # canonical "awh" entry -- selected by name, never by whichever owned
    # entry happens to come first in Object.entries -- at the stable launcher.
    result = _drive_extension_internals(
        tmp_path,
        """(internals) => internals.repairOpencodeConfigJsonObject({
          theme: "dark",
          mcp: {
            aiworkhub_ultrafast: {
              type: "local",
              command: ["stale-python", "/old/ultrafast-launcher.py"],
              environment: {
                AIWORKHUB_REPO_ROOT: "/repo/a",
                AIWORKHUB_REPO: "/repo/a",
                AIWORKHUB_REPO_ID: "repo_deadbeef",
                KEEP_ME: "ultrafast-flag",
              },
            },
            awh: {
              type: "local",
              command: ["stale-python", "/old/launcher.py"],
              enabled: false,
              environment: {
                AIWORKHUB_REPO_ROOT: "/repo/a",
                AIWORKHUB_REPO: "/repo/a",
                AIWORKHUB_REPO_ID: "repo_deadbeef",
                AIWORKHUB_ALLOW_WRITES: "0",
                SOME_SECRET: "keep-me",
              },
            },
            "unrelated-server": {
              type: "local",
              command: ["node", "unrelated.js"],
              environment: { AIWORKHUB_REPO_ROOT: "/should/not/be/touched" },
            },
          },
        }, ["python3", "/new/launcher.py"])""",
    )
    assert result["changed"] is True
    document = result["document"]
    assert document["theme"] == "dark"
    servers = document["mcp"]

    # No AIWorkHub-owned entry retains ANY repository-identity key.
    for owned_name in ("awh", "aiworkhub_ultrafast"):
        environment = servers[owned_name]["environment"]
        for key in _REPO_IDENTITY_ENV_KEYS:
            assert key not in environment, f"{owned_name} still carries {key}"

    # The canonical entry is the one re-pointed at the stable launcher.
    assert servers["awh"]["command"] == ["python3", "/new/launcher.py"]
    assert servers["aiworkhub_ultrafast"]["command"] == [
        "stale-python",
        "/old/ultrafast-launcher.py",
    ]

    # Secrets, capability gates and an operator-disabled flag survive repair.
    assert servers["awh"]["environment"]["SOME_SECRET"] == "keep-me"
    assert servers["awh"]["environment"]["AIWORKHUB_ALLOW_WRITES"] == "0"
    assert servers["awh"]["enabled"] is False
    assert servers["aiworkhub_ultrafast"]["environment"]["KEEP_ME"] == "ultrafast-flag"

    # An unrelated MCP registration is never rewritten, not even its env.
    assert servers["unrelated-server"] == {
        "type": "local",
        "command": ["node", "unrelated.js"],
        "environment": {"AIWORKHUB_REPO_ROOT": "/should/not/be/touched"},
    }


def test_opencode_global_alias_does_not_migrate_existing_aiworkhub_entry(tmp_path: Path) -> None:
    result = _drive_extension_internals(
        tmp_path,
        """(internals) => internals.repairOpencodeConfigJsonObject({
          mcp: {
            aiworkhub: {
              type: "local",
              command: ["python3", "/legacy.py"],
              enabled: false,
              environment: { KEEP: "legacy" },
            },
          },
        }, ["python3", "/new/launcher.py"])""",
    )
    servers = result["document"]["mcp"]
    assert set(servers) == {"aiworkhub", "awh"}
    assert servers["aiworkhub"] == {
        "type": "local",
        "command": ["python3", "/legacy.py"],
        "enabled": False,
        "environment": {"KEEP": "legacy"},
    }
    assert servers["awh"]["command"] == ["python3", "/new/launcher.py"]


def test_opencode_global_mcp_repair_disables_duplicate_legacy_alias(tmp_path: Path) -> None:
    result = _drive_extension_internals(
        tmp_path,
        """(internals) => internals.repairOpencodeConfigJsonObject({
          mcp: {
            awh: {
              type: "local",
              command: ["python3", "/launcher.py"],
              enabled: true,
              environment: {
                AIWORKHUB_ALLOW_WRITES: "1",
                AIWORKHUB_ALLOW_LAUNCH: "1",
                AIWORKHUB_MCP_STDIO_BACKEND: "stdlib",
                AIWORKHUB_MAX_PROCESSES: "32",
              },
            },
            aiworkhub: {
              type: "local",
              command: ["python3", "/launcher.py"],
              enabled: true,
              environment: {
                AIWORKHUB_ALLOW_WRITES: "1",
                AIWORKHUB_ALLOW_LAUNCH: "1",
                AIWORKHUB_MCP_STDIO_BACKEND: "stdlib",
              },
            },
          },
        }, ["python3", "/launcher.py"])""",
    )
    servers = result["document"]["mcp"]
    assert result["changed"] is True
    assert servers["awh"]["enabled"] is True
    assert servers["aiworkhub"]["enabled"] is False
    assert servers["aiworkhub"]["command"] == ["python3", "/launcher.py"]
    assert servers["aiworkhub"]["environment"] == {
        "AIWORKHUB_ALLOW_WRITES": "1",
        "AIWORKHUB_ALLOW_LAUNCH": "1",
        "AIWORKHUB_MCP_STDIO_BACKEND": "stdlib",
    }


def test_opencode_global_mcp_repair_keeps_distinct_legacy_launcher_enabled(tmp_path: Path) -> None:
    result = _drive_extension_internals(
        tmp_path,
        """(internals) => internals.repairOpencodeConfigJsonObject({
          mcp: {
            aiworkhub: {
              type: "local",
              command: ["python3", "/separate-launcher.py"],
              enabled: true,
              environment: { KEEP: "separate" },
            },
          },
        }, ["python3", "/canonical-launcher.py"])""",
    )
    servers = result["document"]["mcp"]
    assert servers["awh"]["enabled"] is True
    assert servers["aiworkhub"] == {
        "type": "local",
        "command": ["python3", "/separate-launcher.py"],
        "enabled": True,
        "environment": {"KEEP": "separate"},
    }


def test_opencode_global_mcp_repair_preserves_same_launcher_with_distinct_permissions(
    tmp_path: Path,
) -> None:
    result = _drive_extension_internals(
        tmp_path,
        """(internals) => internals.repairOpencodeConfigJsonObject({
          mcp: {
            awh: {
              type: "local",
              command: ["python3", "/launcher.py"],
              enabled: true,
              environment: {
                AIWORKHUB_ALLOW_WRITES: "1",
                AIWORKHUB_ALLOW_LAUNCH: "1",
                AIWORKHUB_MCP_STDIO_BACKEND: "stdlib",
              },
            },
            aiworkhub: {
              type: "local",
              command: ["python3", "/launcher.py"],
              enabled: true,
              environment: {
                AIWORKHUB_ALLOW_WRITES: "0",
                AIWORKHUB_ALLOW_LAUNCH: "1",
                AIWORKHUB_MCP_STDIO_BACKEND: "stdlib",
              },
            },
          },
        }, ["python3", "/launcher.py"])""",
    )
    servers = result["document"]["mcp"]
    assert servers["awh"]["enabled"] is True
    assert servers["aiworkhub"]["enabled"] is True
    assert servers["aiworkhub"]["environment"]["AIWORKHUB_ALLOW_WRITES"] == "0"


def test_opencode_global_mcp_repair_is_idempotent(tmp_path: Path) -> None:
    result = _drive_extension_internals(
        tmp_path,
        """(internals) => {
          const first = internals.repairOpencodeConfigJsonObject({}, ["python3", "/launcher.py"]);
          const second = internals.repairOpencodeConfigJsonObject(first.document, ["python3", "/launcher.py"]);
          return { first: first.changed, second: second.changed };
        }""",
    )
    assert result == {"first": True, "second": False}


def test_opencode_registration_leaves_codex_and_claude_repair_helpers_untouched(
    tmp_path: Path,
) -> None:
    # Codex and Claude keep their own, deliberately repository-bound helpers;
    # the OpenCode work must not have merged them into one code path.
    result = _drive_extension_internals(
        tmp_path,
        """(internals) => Object.keys(internals).sort()""",
    )
    assert "repairOpencodeConfigJsonObject" in result
    assert "repairClaudeMcpConfigObject" in result


# ---------------------------------------------------------------------------
# Request-local awh worker MCP config: the generated runtime -> OpenCode launch.
# The generator itself needs ``chmod``, so most cases below write a
# generator-shaped runtime directly; one case drives the real generator.
# ---------------------------------------------------------------------------

_MCP = worker_ai_tools_mcp
_OC_ENV = runtime_adapters.OPENCODE_WORKER_CONFIG_ENV
_OC_NO_PROJECT = runtime_adapters.OPENCODE_DISABLE_PROJECT_CONFIG_ENV
_OC_BACKENDS = ("landlock", "bubblewrap", "windows_appcontainer")


def _opencode_request(
    tmp_path: Path,
    backend: str = "landlock",
    *,
    request_id: str = "R-oc-1",
    command: str | None = None,
) -> tuple[worker_workspace.WorkerWorkspace, SimpleNamespace, Path]:
    base = tmp_path.resolve()
    authority = base / "authority"
    root = base / request_id
    workspace = worker_workspace.WorkerWorkspace(
        request_id=request_id,
        repo=authority,
        path=root / "worktree",
        home=root / "home",
        allowed_writes=("src/a.py",),
        parent_baseline={},
        workspace_baseline={},
    )
    runtime_dir = workspace.home / "task_mcp_worker_runtime"
    for directory in (authority, workspace.path, runtime_dir):
        directory.mkdir(parents=True)
    ledger = runtime_dir / "audit_ledger.jsonl"
    key = runtime_dir / "audit_hmac.key"
    ledger.write_bytes(b"")
    key.write_bytes(b"k" * 32)
    aliased = backend == "bubblewrap"
    env = {
        _MCP.ENV_TASK_ID: "T-1",
        _MCP.ENV_RUNNER: "opencode-go",
        _MCP.ENV_TOPIC: "topic",
        _MCP.ENV_REQUEST_ID: request_id,
        _MCP.ENV_REPO: (
            worker_workspace.SANDBOX_WORKSPACE if aliased else str(workspace.path)
        ),
        _MCP.ENV_AUTHORITY_REPO: (
            worker_workspace.SANDBOX_AUTHORITY_REPO if aliased else str(authority)
        ),
        _MCP.ENV_SOURCE_GRAPH_TARGETS: json.dumps(["src/a.py"]),
        _MCP.ENV_ALLOWED_WRITES: json.dumps(["src/a.py"]),
        _MCP.ENV_SESSION_TOPIC: "topic",
        _MCP.ENV_AUDIT_LEDGER_PATH: str(ledger),
        _MCP.ENV_AUDIT_HMAC_KEY_PATH: str(key),
        _MCP.ENV_PYTHONPATH: (
            worker_workspace.SANDBOX_PACKAGE_IMPORT_ROOT
            if aliased
            else str(_MCP.resolve_host_package_import_root())
        ),
    }
    python = command or ("/usr/bin/env" if aliased else sys.executable)
    source = runtime_dir / "claude_mcp_config.json"
    source.write_text(
        json.dumps(
            {
                "mcpServers": {
                    _MCP.SERVER_NAME: {
                        "command": python,
                        "args": ["-m", "aiworkhub.worker_ai_tools_mcp"],
                        "env": env,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    runtime = SimpleNamespace(
        server_name=_MCP.SERVER_NAME,
        env=env,
        claude_mcp_config_path=source,
        audit_ledger_path=ledger,
        audit_hmac_key_path=key,
    )
    return workspace, runtime, authority


def _provision_opencode(
    workspace: worker_workspace.WorkerWorkspace,
    runtime: SimpleNamespace,
    authority: Path,
    backend: str = "landlock",
) -> dict[str, str]:
    return worker_workspace.provision_opencode_worker_config(
        workspace, runtime, backend=backend, authority_repo=authority
    )


@pytest.mark.parametrize("backend", _OC_BACKENDS)
def test_opencode_request_config_binds_the_exact_request_for_every_sandbox(
    tmp_path: Path, backend: str
) -> None:
    if backend == "bubblewrap" and os.name == "nt":
        pytest.skip("bubblewrap is a Linux sandbox")
    workspace, runtime, authority = _opencode_request(tmp_path, backend)
    before = sorted(str(path) for path in tmp_path.rglob("*"))

    delivered = _provision_opencode(workspace, runtime, authority, backend)

    # Environment only: two variables, and not one file written anywhere.
    assert set(delivered) == {_OC_ENV, _OC_NO_PROJECT}
    assert delivered[_OC_NO_PROJECT] == "1"
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    config = json.loads(delivered[_OC_ENV])
    assert list(config["mcp"]) == ["awh"]
    server = config["mcp"]["awh"]
    assert server["type"] == "local" and server["enabled"] is True
    source = json.loads(Path(runtime.claude_mcp_config_path).read_text(encoding="utf-8"))
    generated = source["mcpServers"][_MCP.SERVER_NAME]
    assert server["command"] == [generated["command"], *generated["args"]]
    expected = dict(runtime.env)
    if backend == "bubblewrap":
        alias = PurePosixPath(worker_workspace.bubblewrap_home_env_value())
        expected[_MCP.ENV_AUDIT_LEDGER_PATH] = str(
            alias / "task_mcp_worker_runtime" / "audit_ledger.jsonl"
        )
        expected[_MCP.ENV_AUDIT_HMAC_KEY_PATH] = str(
            alias / "task_mcp_worker_runtime" / "audit_hmac.key"
        )
    assert server["environment"] == expected
    assert server["environment"][_MCP.ENV_REQUEST_ID] == workspace.request_id
    permission = config["permission"]
    assert permission["*"] == runtime_adapters.OPENCODE_PERMISSION_DENY
    allowed = {
        name
        for name, action in permission.items()
        if action == runtime_adapters.OPENCODE_PERMISSION_ALLOW
    }
    assert allowed == {
        runtime_adapters.opencode_mcp_tool_name(tool)
        for tool in runtime_adapters.OPENCODE_WORKER_MCP_TOOLS
    }
    assert all(len(name) <= 64 for name in allowed) and len("awh") <= 64
    for denied in ("bash", "edit", "read", "awh_aiworkhub_manager_bootstrap", "unknown"):
        assert permission.get(denied, permission["*"]) == "deny"
    assert "model" not in config and "provider" not in config


def test_opencode_request_configs_never_cross_repositories(tmp_path: Path) -> None:
    first = _opencode_request(tmp_path / "repo-a", request_id="R-a")
    second = _opencode_request(tmp_path / "repo-b", request_id="R-b")
    texts = [
        _provision_opencode(workspace, runtime, authority)[_OC_ENV]
        for workspace, runtime, authority in (first, second)
    ]
    assert texts[0] != texts[1]
    assert str(tmp_path.resolve() / "repo-b") not in texts[0]
    assert str(tmp_path.resolve() / "repo-a") not in texts[1]
    for text, request_id in zip(texts, ("R-a", "R-b"), strict=True):
        environment = json.loads(text)["mcp"]["awh"]["environment"]
        assert environment[_MCP.ENV_REQUEST_ID] == request_id
        for identity_key in ("AIWORKHUB_REPO_ROOT", "AIWORKHUB_REPO", "AIWORKHUB_REPO_ID"):
            assert identity_key not in environment


def _rewrite_generated_entry(runtime: SimpleNamespace, mutate) -> None:
    path = Path(runtime.claude_mcp_config_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document["mcpServers"][_MCP.SERVER_NAME])
    path.write_text(json.dumps(document), encoding="utf-8")


def _tamper_missing(workspace, runtime, tmp_path, monkeypatch) -> None:
    Path(runtime.claude_mcp_config_path).unlink()


def _tamper_symlink(workspace, runtime, tmp_path, monkeypatch) -> None:
    if os.name == "nt":
        pytest.skip("symlink creation needs a privilege on Windows")
    source = Path(runtime.claude_mcp_config_path)
    real = tmp_path / "real_config.json"
    source.replace(real)
    source.symlink_to(real)


def _tamper_not_regular(workspace, runtime, tmp_path, monkeypatch) -> None:
    source = Path(runtime.claude_mcp_config_path)
    source.unlink()
    source.mkdir()


def _tamper_text(text: str):
    def _write(workspace, runtime, tmp_path, monkeypatch) -> None:
        Path(runtime.claude_mcp_config_path).write_text(text, encoding="utf-8")

    return _write


def _tamper_oversized(workspace, runtime, tmp_path, monkeypatch) -> None:
    Path(runtime.claude_mcp_config_path).write_bytes(b" " * 300_000)


def _tamper_unreadable(workspace, runtime, tmp_path, monkeypatch) -> None:
    source = Path(runtime.claude_mcp_config_path)
    real_open = os.open

    def _denied(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)) == source.name:
            raise PermissionError(13, "denied", os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", _denied)


def _tamper_foreign_owner(workspace, runtime, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        worker_workspace, "stat_owned_by_current_user", lambda *_a, **_k: False
    )


def _tamper_outside_home(workspace, runtime, tmp_path, monkeypatch) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    moved = elsewhere / "claude_mcp_config.json"
    Path(runtime.claude_mcp_config_path).replace(moved)
    runtime.claude_mcp_config_path = moved


def _tamper_env(key: str, value):
    def _set(workspace, runtime, tmp_path, monkeypatch) -> None:
        _rewrite_generated_entry(runtime, lambda entry: entry["env"].__setitem__(key, value))

    return _set


def _tamper_entry(key: str, value):
    def _set(workspace, runtime, tmp_path, monkeypatch) -> None:
        _rewrite_generated_entry(runtime, lambda entry: entry.__setitem__(key, value))

    return _set


def _tamper_audit_key_gone(workspace, runtime, tmp_path, monkeypatch) -> None:
    Path(runtime.audit_hmac_key_path).unlink()


def _tamper_repo_elsewhere(workspace, runtime, tmp_path, monkeypatch) -> None:
    other = tmp_path / "other-worktree"
    other.mkdir()
    _tamper_env(_MCP.ENV_REPO, str(other))(workspace, runtime, tmp_path, monkeypatch)


def _tamper_ledger_elsewhere(workspace, runtime, tmp_path, monkeypatch) -> None:
    _tamper_env(_MCP.ENV_AUDIT_LEDGER_PATH, str(tmp_path / "elsewhere" / "ledger"))(
        workspace, runtime, tmp_path, monkeypatch
    )


def _tamper_command_not_executable(workspace, runtime, tmp_path, monkeypatch) -> None:
    if os.name == "nt":
        pytest.skip("Windows has no execute permission bit")
    plain = tmp_path / "plain-python"
    plain.write_text("not a program\n", encoding="utf-8")
    if os.access(plain, os.X_OK):
        pytest.skip("this host marks new files executable")
    _tamper_entry("command", str(plain))(workspace, runtime, tmp_path, monkeypatch)


_OPENCODE_REFUSALS = (
    ("missing", _tamper_missing),
    ("symlink", _tamper_symlink),
    ("not_regular", _tamper_not_regular),
    ("malformed", _tamper_text("{not json")),
    ("malformed", _tamper_text("[]")),
    ("malformed", _tamper_text(json.dumps({"mcpServers": {}}))),
    ("malformed", _tamper_env(_MCP.ENV_TOPIC, 7)),
    ("malformed", _tamper_env("OPENAI_API_KEY", "sk-inherited")),
    ("malformed", _tamper_entry("args", ["-c", "print(1)"])),
    ("oversized", _tamper_oversized),
    ("unreadable", _tamper_unreadable),
    ("untrusted_owner", _tamper_foreign_owner),
    ("outside_home", _tamper_outside_home),
    ("outside_home", _tamper_ledger_elsewhere),
    ("binding_mismatch", _tamper_env(_MCP.ENV_REQUEST_ID, "R-other")),
    ("binding_mismatch", _tamper_repo_elsewhere),
    ("audit_binding_missing", _tamper_audit_key_gone),
    ("command_missing", _tamper_entry("command", "/nonexistent/aiworkhub-python")),
    ("command_not_executable", _tamper_command_not_executable),
)


@pytest.mark.parametrize(
    ("cause", "tamper"),
    _OPENCODE_REFUSALS,
    ids=[f"{cause}-{index}" for index, (cause, _) in enumerate(_OPENCODE_REFUSALS)],
)
def test_opencode_request_config_refuses_with_a_typed_cause_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str, tamper
) -> None:
    workspace, runtime, authority = _opencode_request(tmp_path)
    tamper(workspace, runtime, tmp_path, monkeypatch)

    with pytest.raises(runtime_adapters.OpenCodeWorkerConfigError) as excinfo:
        _provision_opencode(workspace, runtime, authority)

    assert excinfo.value.cause == cause
    assert str(excinfo.value).startswith(f"opencode_worker_mcp_config_{cause}")


@pytest.mark.skipif(os.name == "nt", reason="bubblewrap is a Linux sandbox")
def test_opencode_request_config_refuses_paths_the_bubblewrap_mount_cannot_show(
    tmp_path: Path,
) -> None:
    workspace, runtime, authority = _opencode_request(
        tmp_path, "bubblewrap", command="/home/nobody/.venv/bin/python"
    )
    with pytest.raises(runtime_adapters.OpenCodeWorkerConfigError) as excinfo:
        _provision_opencode(workspace, runtime, authority, "bubblewrap")
    assert excinfo.value.cause == "command_not_visible"

    workspace, runtime, authority = _opencode_request(
        tmp_path / "second", "bubblewrap", request_id="R-oc-2"
    )
    host_packet = workspace.home / "task_mcp_worker_runtime" / "quality_review_packet.json"
    _tamper_env(_MCP.ENV_QUALITY_REVIEW_PACKET_PATH, str(host_packet))(
        workspace, runtime, tmp_path, None
    )
    with pytest.raises(runtime_adapters.OpenCodeWorkerConfigError) as excinfo:
        _provision_opencode(workspace, runtime, authority, "bubblewrap")
    assert excinfo.value.cause == "outside_sandbox"


def test_opencode_request_config_refuses_an_unsupported_sandbox_backend(
    tmp_path: Path,
) -> None:
    workspace, runtime, authority = _opencode_request(tmp_path)
    with pytest.raises(runtime_adapters.OpenCodeWorkerConfigError) as excinfo:
        _provision_opencode(
            workspace, runtime, authority, worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND
        )
    assert excinfo.value.cause == "backend_unsupported"


def test_generated_worker_runtime_feeds_the_request_local_opencode_config(
    tmp_path: Path,
) -> None:
    base = tmp_path.resolve()
    authority = base / "authority"
    workspace = worker_workspace.WorkerWorkspace(
        request_id="R-gen-1",
        repo=authority,
        path=base / "R-gen-1" / "worktree",
        home=base / "R-gen-1" / "home",
        allowed_writes=("src/a.py",),
        parent_baseline={},
        workspace_baseline={},
    )
    for directory in (authority, workspace.path, workspace.home):
        directory.mkdir(parents=True)
    try:
        runtime = worker_workspace.provision_worker_mcp_runtime(
            workspace,
            request_id=workspace.request_id,
            task_id="T-1",
            runner="opencode-go",
            topic="topic",
            backend="landlock",
            source_graph_targets=["src/a.py"],
            session_topic="topic",
            allowed_writes=["src/a.py"],
        )
    except PermissionError as exc:
        pytest.skip(f"validation_unsupported_in_sandbox: chmod denied ({exc.filename})")

    delivered = _provision_opencode(workspace, runtime, authority)

    server = json.loads(delivered[_OC_ENV])["mcp"]["awh"]
    assert server["command"][1:] == ["-m", "aiworkhub.worker_ai_tools_mcp"]
    assert server["command"][0] == sys.executable
    assert server["environment"] == runtime.env
    assert server["environment"][_MCP.ENV_REQUEST_ID] == "R-gen-1"
    assert Path(server["environment"][_MCP.ENV_AUDIT_HMAC_KEY_PATH]).is_file()
