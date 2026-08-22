from __future__ import annotations

import inspect

import pytest

from aiworkhub import server
from aiworkhub.task_templates import (
    REGISTRY_VERSION,
    SCHEMA_ID,
    TEMPLATE_IDS,
    TaskTemplateError,
    expand_template,
    template_full_id,
)


def test_task_create_forwards_required_output_exception_contract(monkeypatch):
    captured = {}

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(server.core, "create_task", fake_create_task)

    result = server.aiworkhub_task_create(
        task_id="TASK_REQUIRED_OUTPUT_MCP",
        title="Preserve accepted evidence",
        runner="codex_worker",
        topic="coding",
        objective="Update readiness without rewriting accepted evidence.",
        acceptance=["Evidence stays valid."],
        allowed_writes=["out/evidence.json", "out/READY.md"],
        required_outputs=["out/evidence.json", "out/READY.md"],
        allow_empty_required_outputs=["out/READY.md"],
        allow_unchanged_required_outputs=["out/evidence.json"],
        validation=["python -m pytest -q"],
    )

    assert result == {"ok": True}
    assert captured["allow_empty_required_outputs"] == ["out/READY.md"]
    assert captured["allow_unchanged_required_outputs"] == ["out/evidence.json"]


def test_task_create_schema_remains_unchanged():
    assert list(inspect.signature(server.aiworkhub_task_create).parameters) == [
        "task_id",
        "title",
        "runner",
        "topic",
        "objective",
        "acceptance",
        "allowed_writes",
        "forbidden",
        "required_outputs",
        "allow_empty_required_outputs",
        "allow_unchanged_required_outputs",
        "validation",
        "priority",
        "task_type",
        "depends_on",
        "read_first",
        "immutable_inputs",
        "read_only",
        "max_live_tokens",
        "work_kind",
        "validation_roles",
        "risk_tier",
    ]


def test_create_from_template_rejects_scope_and_validation_overrides():
    params = inspect.signature(server.aiworkhub_task_create_from_template).parameters
    for name in (
        "allowed_writes",
        "required_outputs",
        "read_first",
        "read_only",
        "task_type",
        "work_kind",
        "validation",
        "validation_roles",
    ):
        assert name not in params


def test_create_from_template_bugfix_forwards_generated_fields(monkeypatch):
    captured = {}

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "task_id": kwargs["task_id"]}

    monkeypatch.setattr(server.core, "create_task", fake_create_task)
    card = expand_template(
        "bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
        title="Fix the leak",
        objective="Close the leak and add a regression.",
    )
    result = server.aiworkhub_task_create_from_template(
        task_id="TASK_TEMPLATE_BUGFIX",
        title="Fix the leak",
        runner="codex_worker",
        topic="coding",
        objective="Close the leak and add a regression.",
        acceptance=["Leak is gone."],
        template_id="bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
        forbidden=[".aiworkhub/**"],
        priority="high",
        risk_tier="high",
    )
    assert captured["allowed_writes"] == card["allowed_writes"]
    assert captured["required_outputs"] == card["required_outputs"]
    assert captured["read_first"] == card["read_first"]
    assert captured["read_only"] is card["read_only"] is False
    assert captured["task_type"] == card["task_type"]
    assert captured["work_kind"] == card["work_kind"] == "bugfix"
    assert captured["validation"] == card["validation"]
    assert captured["validation_roles"] == card["validation_roles"]
    assert captured["forbidden"] == [".aiworkhub/**"]
    assert captured["priority"] == "high"
    assert captured["risk_tier"] == "high"
    assert result["ok"] is True
    assert result["template_provenance"] == {
        "schema_id": SCHEMA_ID,
        "template_full_id": template_full_id("bugfix_with_regression"),
        "registry_version": REGISTRY_VERSION,
        "definition_digest": template_full_id("bugfix_with_regression").split(":", 1)[1],
    }


def test_create_from_template_read_only_analysis(monkeypatch):
    captured = {}

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(server.core, "create_task", fake_create_task)
    result = server.aiworkhub_task_create_from_template(
        task_id="TASK_TEMPLATE_ANALYSIS",
        title="Inspect the module",
        runner="codex_worker",
        topic="analysis",
        objective="Report findings without writing.",
        acceptance=["Findings are bounded."],
        template_id="read_only_analysis",
        production_paths=["src/a.py"],
    )
    assert captured["read_only"] is True
    assert captured["allowed_writes"] == []
    assert captured["required_outputs"] == []
    assert captured["validation"] == []
    assert captured["validation_roles"] == []
    assert captured["work_kind"] == "generic"
    assert captured["read_first"] == ["src/a.py"]
    assert result["template_provenance"]["schema_id"] == SCHEMA_ID


def test_create_from_template_rejects_malformed_and_unsafe_before_create(monkeypatch):
    calls = []

    def fake_create_task(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(server.core, "create_task", fake_create_task)
    digest = template_full_id("bugfix_with_regression").split(":", 1)[1]
    flipped = "0" if digest[-1] != "0" else "1"
    forged = f"bugfix_with_regression@v{REGISTRY_VERSION}:{digest[:-1]}{flipped}"
    stale = f"bugfix_with_regression@v0:{digest}"
    kwargs = {
        "task_id": "TASK_TEMPLATE_REJECT",
        "title": "Should fail",
        "runner": "codex_worker",
        "topic": "coding",
        "objective": "Must not persist.",
        "acceptance": ["No card."],
        "production_paths": ["src/a.py"],
        "test_paths": ["tests/test_a.py"],
    }
    with pytest.raises(TaskTemplateError, match="template_id_malformed"):
        server.aiworkhub_task_create_from_template(
            template_id="bugfix_with_regression@v1", **kwargs
        )
    with pytest.raises(TaskTemplateError, match="template_digest_mismatch"):
        server.aiworkhub_task_create_from_template(template_id=forged, **kwargs)
    with pytest.raises(TaskTemplateError, match="template_version_stale"):
        server.aiworkhub_task_create_from_template(template_id=stale, **kwargs)
    with pytest.raises(TaskTemplateError, match="invalid_production_path"):
        server.aiworkhub_task_create_from_template(
            template_id="bugfix_with_regression",
            task_id="TASK_TEMPLATE_REJECT",
            title="Should fail",
            runner="codex_worker",
            topic="coding",
            objective="Must not persist.",
            acceptance=["No card."],
            production_paths=["../escape.py"],
            test_paths=["tests/test_a.py"],
        )
    assert calls == []


@pytest.mark.parametrize(
    ("template_id", "production_paths", "test_paths"),
    [
        ("read_only_analysis", ["src/a.py"], None),
        ("bugfix_with_regression", ["src/a.py"], ["tests/test_a.py"]),
        ("implementation_with_tests", ["src/mod.py"], ["tests/test_mod.py"]),
        ("test_only", None, ["tests/test_a.py"]),
        ("docs_change", ["docs/guide.md"], None),
        ("validation_replay", ["src/a.py"], ["tests/test_a.py"]),
    ],
)
def test_create_from_template_supports_each_template_class(
    monkeypatch, template_id, production_paths, test_paths
):
    captured = {}

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(server.core, "create_task", fake_create_task)
    card = expand_template(
        template_id,
        production_paths=production_paths,
        test_paths=test_paths,
        title="Template class card",
        objective="Create from the authenticated template.",
    )
    result = server.aiworkhub_task_create_from_template(
        task_id=f"TASK_TEMPLATE_{template_id.upper()}",
        title="Template class card",
        runner="codex_worker",
        topic="coding",
        objective="Create from the authenticated template.",
        acceptance=["Card matches the template."],
        template_id=template_id,
        production_paths=production_paths,
        test_paths=test_paths,
    )
    assert captured["allowed_writes"] == card["allowed_writes"]
    assert captured["required_outputs"] == card["required_outputs"]
    assert captured["read_first"] == card["read_first"]
    assert captured["read_only"] is card["read_only"]
    assert captured["task_type"] == card["task_type"]
    assert captured["work_kind"] == card["work_kind"]
    assert captured["validation"] == card["validation"]
    assert captured["validation_roles"] == card["validation_roles"]
    assert result["template_provenance"]["template_full_id"] == template_full_id(
        template_id
    )


_TEMPLATE_PATHS = {
    "read_only_analysis": {"production_paths": ["src/a.py"]},
    "bugfix_with_regression": {
        "production_paths": ["src/a.py"],
        "test_paths": ["tests/test_a.py"],
    },
    "implementation_with_tests": {
        "production_paths": ["src/mod.py"],
        "test_paths": ["tests/test_mod.py"],
    },
    "test_only": {"test_paths": ["tests/test_a.py"]},
    "docs_change": {"production_paths": ["docs/guide.md"]},
    "validation_replay": {
        "production_paths": ["src/a.py"],
        "test_paths": ["tests/test_a.py"],
    },
}

_CANONICAL_TASK_TYPES = ("code", "data_classification", "research")
_ORIGIN_THREAD_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _enable_real_core_create(monkeypatch, tmp_path):
    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(server.core, "_canonical_db_path", lambda: db_path)
    monkeypatch.setattr(
        server.core,
        "_claude_manager_identity",
        lambda: {
            "provider": "codex",
            "thread_id": _ORIGIN_THREAD_ID,
            "route_state": "ready",
        },
    )
    monkeypatch.setattr(server.core, "_codex_manager_identity", lambda: None)
    monkeypatch.setattr(server.core, "_canonical_write_gate", lambda *a, **k: None)
    monkeypatch.setattr(
        server.core, "_verify_coordinator_capability", lambda runner: (True, "ok")
    )


def test_create_from_template_real_core_accepts_every_template_id(
    monkeypatch, tmp_path
):
    _enable_real_core_create(monkeypatch, tmp_path)
    created = []
    for template_id in TEMPLATE_IDS:
        paths = _TEMPLATE_PATHS[template_id]
        result = server.aiworkhub_task_create_from_template(
            task_id=f"TASK_TEMPLATE_REAL_{template_id.upper()}",
            title="Template class card",
            runner="codex_worker",
            topic="coding",
            objective="Create from the authenticated template.",
            acceptance=["Card matches the template."],
            template_id=template_id,
            production_paths=paths.get("production_paths"),
            test_paths=paths.get("test_paths"),
        )
        assert result.get("ok") is True, result
        assert result.get("created") is True, result
        assert result.get("stderr") != "invalid_task_type"
        card = expand_template(
            template_id,
            production_paths=paths.get("production_paths"),
            test_paths=paths.get("test_paths"),
            title="Template class card",
            objective="Create from the authenticated template.",
        )
        assert card["task_type"] in _CANONICAL_TASK_TYPES
        created.append(template_id)
    assert created == list(TEMPLATE_IDS)


@pytest.mark.parametrize("path", ["--collect-only", "--exit-zero"])
def test_create_from_template_rejects_leading_hyphen_paths_before_create(
    monkeypatch, path
):
    calls = []

    def fake_create_task(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(server.core, "create_task", fake_create_task)
    kwargs = {
        "task_id": "TASK_TEMPLATE_HYPHEN",
        "title": "Should fail",
        "runner": "codex_worker",
        "topic": "coding",
        "objective": "Must not persist.",
        "acceptance": ["No card."],
    }
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_leading_hyphen"
    ):
        server.aiworkhub_task_create_from_template(
            template_id="bugfix_with_regression",
            production_paths=[path],
            test_paths=["tests/test_a.py"],
            **kwargs,
        )
    with pytest.raises(TaskTemplateError, match="invalid_test_path_leading_hyphen"):
        server.aiworkhub_task_create_from_template(
            template_id="bugfix_with_regression",
            production_paths=["src/a.py"],
            test_paths=[path],
            **kwargs,
        )
    assert calls == []
