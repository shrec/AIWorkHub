from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, server, task_store, task_templates  # noqa: E402


def _patch_create(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo.resolve()))
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo.resolve()))
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        core, "_verify_coordinator_capability", lambda *args, **kwargs: (True, "")
    )
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {
            "provider": "claude",
            "session_id": "01234567-89ab-4def-8123-456789abcdef",
            "route_state": "verified",
        },
    )
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", repo.resolve())


def _ready_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _patch_create(monkeypatch, repo)
    return repo


def test_default_task_create_is_template_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _ready_repo(tmp_path, monkeypatch)
    expanded = task_templates.expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    result = server.aiworkhub_task_create(
        task_id="TASK_NF390_DEFAULT",
        title="Template first create",
        runner="codex_worker_nf390",
        topic="task_mcp",
        objective=expanded["objective"],
        acceptance=["classify generic python cards"],
        allowed_writes=expanded["allowed_writes"],
        required_outputs=expanded["required_outputs"],
        validation=expanded["validation"],
        validation_roles=expanded["validation_roles"],
        read_first=expanded["read_first"],
        work_kind="generic",
    )
    assert result["ok"] is True
    created = json.loads(result["stdout"])
    assert created["template_provenance"]["template_name"] == (
        "implementation_with_tests"
    )
    assert created["template_provenance"]["classification_reason"] == (
        "compatible_generic_python_production_plus_test"
    )
    stored = task_store.get_task(repo, "TASK_NF390_DEFAULT")
    assert stored is not None
    assert stored["template_provenance"] == created["template_provenance"]

def test_unclassified_raw_task_create_fails_without_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_repo(tmp_path, monkeypatch)
    result = server.aiworkhub_task_create(
        task_id="TASK_NF390_RAW",
        title="Raw unclassified",
        runner="codex_worker_nf390",
        topic="task_mcp",
        objective="Write an odd file",
        acceptance=["must fail closed"],
        allowed_writes=["src/odd.txt"],
        required_outputs=["src/odd.txt"],
        validation=["python -m pytest -q tests/test_missing.py"],
        work_kind="generic",
    )
    assert result["ok"] is False
    assert result["stderr"] == "template_unclassified"
    escaped = server.aiworkhub_task_create(
        task_id="TASK_NF390_RAW_OK",
        title="Raw audited escape",
        runner="codex_worker_nf390",
        topic="task_mcp",
        objective="Write an odd file",
        acceptance=["explicit audited escape"],
        allowed_writes=["src/odd.txt"],
        required_outputs=["src/odd.txt"],
        validation=["python -m pytest -q tests/test_missing.py"],
        work_kind="generic",
        custom_template_escape=task_templates.AUDITED_CUSTOM_ESCAPE,
    )
    assert escaped["ok"] is True
    created = json.loads(escaped["stdout"])
    assert created["template_provenance"]["classification_reason"] == (
        "audited_custom_escape"
    )


def test_task_show_returns_stored_provenance_not_live_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _ready_repo(tmp_path, monkeypatch)
    expanded = task_templates.expand_template(
        "bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
    )
    created = server.aiworkhub_task_create_from_template(
        task_id="TASK_NF390_SHOW",
        title=expanded["title"],
        runner="codex_worker_nf390",
        topic="task_mcp",
        objective=expanded["objective"],
        acceptance=["show stored provenance"],
        template_id="bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
    )
    assert created["ok"] is True
    created_return = created["template_provenance"]
    stored = task_store.get_task(repo, "TASK_NF390_SHOW")
    assert stored is not None
    persisted = dict(stored["template_provenance"])
    assert persisted == created_return
    monkeypatch.setattr(task_templates, "_definition_digest", lambda spec: "11" * 32)
    monkeypatch.setattr(
        task_templates,
        "classify_task_card",
        lambda **kwargs: {
            **persisted,
            "classification_reason": "poisoned_live_classifier",
            "definition_digest": "22" * 32,
            "template_full_id": "bugfix_with_regression@v1:" + ("22" * 32),
        },
    )
    shown = server.aiworkhub_task_show("TASK_NF390_SHOW")
    assert shown["ok"] is True
    reloaded = json.loads(shown["stdout"])
    assert reloaded["template_provenance"] == persisted
    assert reloaded["template_provenance"]["classification_reason"] == (
        "explicit_template"
    )
    assert reloaded["template_provenance"]["definition_digest"] == (
        expanded["definition_digest"]
    )


def test_from_template_serializes_full_provenance_into_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _ready_repo(tmp_path, monkeypatch)
    result = server.aiworkhub_task_create_from_template(
        task_id="TASK_NF390_FROM",
        title="From template",
        runner="codex_worker_nf390",
        topic="task_mcp",
        objective="Fix across languages",
        acceptance=["persist explicit template identity"],
        template_id="cross_boundary_bugfix",
        production_paths=["src/a.py", "src/a.js"],
        test_paths=["tests/test_a.py", "tests/a.test.js"],
    )
    assert result["ok"] is True
    provenance = result["template_provenance"]
    assert provenance["template_name"] == "cross_boundary_bugfix"
    assert provenance["classification_reason"] == "explicit_template"
    stored = task_store.get_task(repo, "TASK_NF390_FROM")
    assert stored is not None
    assert stored["template_provenance"] == provenance
    assert "node --test tests/a.test.js" in stored["validation"]
    assert all(
        ".js" not in command
        for command in stored["validation"]
        if command.startswith("python ")
    )


def _from_template_kwargs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_id": "TASK_NF390_ERR",
        "title": "From template error",
        "runner": "codex_worker_nf390",
        "topic": "task_mcp",
        "objective": "Fail closed",
        "acceptance": ["no raw exception"],
        "template_id": "bugfix_with_regression",
        "production_paths": ["src/a.py"],
        "test_paths": ["tests/test_a.py"],
    }
    payload.update(overrides)
    return payload


def _assert_lifecycle_error(result: dict[str, object], stderr: str) -> None:
    assert result["ok"] is False
    assert result["returncode"] == 2
    assert result["command"] == []
    assert result["stdout"] == ""
    assert result["stderr"] == stderr


def test_from_template_malformed_template_id_is_lifecycle_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_repo(tmp_path, monkeypatch)
    result = server.aiworkhub_task_create_from_template(
        **_from_template_kwargs(template_id="bugfix_with_regression@v1")  # type: ignore[arg-type]
    )
    _assert_lifecycle_error(result, "template_id_malformed")


def test_from_template_stale_template_version_is_lifecycle_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_repo(tmp_path, monkeypatch)
    result = server.aiworkhub_task_create_from_template(
        **_from_template_kwargs(  # type: ignore[arg-type]
            template_id="bugfix_with_regression@v0:" + ("ab" * 32)
        )
    )
    _assert_lifecycle_error(result, "template_version_stale")


def test_from_template_unsafe_path_is_lifecycle_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_repo(tmp_path, monkeypatch)
    result = server.aiworkhub_task_create_from_template(
        **_from_template_kwargs(production_paths=["src/a.py;rm"])  # type: ignore[arg-type]
    )
    _assert_lifecycle_error(result, "invalid_production_path_unsafe_token")


def test_from_template_normalize_valueerror_is_lifecycle_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_repo(tmp_path, monkeypatch)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise ValueError("invalid_work_kind")

    monkeypatch.setattr(server.quality_evidence, "normalize_behavioral_contract", _boom)
    result = server.aiworkhub_task_create_from_template(
        **_from_template_kwargs()  # type: ignore[arg-type]
    )
    _assert_lifecycle_error(result, "invalid_work_kind")
    assert result["allowed_work_kinds"] == list(server.quality_evidence.WORK_KINDS)
    assert result["allowed_validation_roles"] == list(
        server.quality_evidence.VALIDATION_ROLES
    )


def test_from_template_provenance_error_is_lifecycle_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_repo(tmp_path, monkeypatch)

    def _boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise task_templates.TaskTemplateError("classification_reason_invalid")

    monkeypatch.setattr(task_templates, "template_provenance_payload", _boom)
    result = server.aiworkhub_task_create_from_template(
        **_from_template_kwargs()  # type: ignore[arg-type]
    )
    _assert_lifecycle_error(result, "classification_reason_invalid")


def test_task_template_list_is_deterministic_and_repository_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_repo(tmp_path, monkeypatch)

    first = server.aiworkhub_task_template_list()
    second = server.aiworkhub_task_template_list()

    assert first == second
    assert first["ok"] is True
    assert first["registry_version"] == task_templates.REGISTRY_VERSION
    assert first["repository"] == core.repository_current()
    assert first["repository"]["repo_id"]
    templates = first["templates"]
    assert [item["name"] for item in templates] == list(
        task_templates.TEMPLATE_IDS
    )
    assert [item["full_id"] for item in templates] == [
        task_templates.template_full_id(name)
        for name in task_templates.TEMPLATE_IDS
    ]


def test_task_template_list_exposes_creation_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_repo(tmp_path, monkeypatch)
    item = server.aiworkhub_task_template_list()["templates"][0]
    assert {
        "name",
        "full_id",
        "definition_digest",
        "title",
        "objective",
        "task_type",
        "work_kind",
        "read_only",
        "production_path_policy",
        "test_path_policy",
        "read_first_fields",
        "generates_pytest",
        "generates_lint",
        "generates_diff_check",
    } <= item.keys()


def test_task_template_show_short_and_full_ids_are_equivalent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_repo(tmp_path, monkeypatch)
    name = "bugfix_with_regression"
    full_id = task_templates.template_full_id(name)

    short = server.aiworkhub_task_template_show(name)
    full = server.aiworkhub_task_template_show(full_id)

    assert short == full
    assert short["ok"] is True
    assert short["template"]["name"] == name
    assert short["template"]["full_id"] == full_id


@pytest.mark.parametrize(
    ("template_id", "reason"),
    [
        ("missing", "template_unknown"),
        ("bugfix_with_regression@v0:" + ("a" * 64), "template_version_stale"),
        (
            "bugfix_with_regression@v1:" + ("a" * 64),
            "template_digest_mismatch",
        ),
    ],
)
def test_task_template_show_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template_id: str,
    reason: str,
) -> None:
    _ready_repo(tmp_path, monkeypatch)
    result = server.aiworkhub_task_template_show(template_id)
    assert result["ok"] is False
    assert result["reason"] == reason
    assert result["repository"]["repo_id"]


def test_task_template_show_documents_deterministic_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_repo(tmp_path, monkeypatch)
    guidance = server.aiworkhub_task_template_show(
        "implementation_with_tests"
    )["creation_guidance"]
    assert "aiworkhub_task_create_from_template" in guidance["selection"]
    assert "read_first" in guidance["scope"]
    assert "not mandatory changed outputs" in guidance["scope"]
    assert "behavioral roles" in guidance["validation"]
    assert "uncapped by default" in guidance["token_budget"]
    assert task_templates.AUDITED_CUSTOM_ESCAPE in guidance["exception"]
