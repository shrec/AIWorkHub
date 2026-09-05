from __future__ import annotations

import inspect
import json

import pytest

from aiworkhub import server
from aiworkhub.task_templates import (
    PROVENANCE_SCHEMA_ID,
    REGISTRY_VERSION,
    SCHEMA_ID,
    TEMPLATE_IDS,
    TaskTemplateError,
    expand_template,
    template_provenance_payload,
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
        "custom_template_escape",
    ]


def test_create_from_template_rejects_scope_but_accepts_validation_overrides():
    params = inspect.signature(server.aiworkhub_task_create_from_template).parameters
    for name in (
        "allowed_writes",
        "required_outputs",
        "read_first",
        "read_only",
        "task_type",
        "work_kind",
    ):
        assert name not in params
    assert params["validation"].default is None
    assert params["validation_roles"].default is None


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
    assert captured["required_outputs"] == ["src/a.py", "tests/test_a.py"]
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
    assert result["template_provenance"] == template_provenance_payload(
        card, classification_reason="explicit_template"
    )


def test_create_from_template_bugfix_preserves_explicit_required_output_subset(
    monkeypatch,
):
    captured = {}

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(server.core, "create_task", fake_create_task)
    result = server.aiworkhub_task_create_from_template(
        task_id="TASK_TEMPLATE_BUGFIX_SUBSET",
        title="Fix one production path",
        runner="codex_worker",
        topic="coding",
        objective="Keep the authenticated mandatory subset exact.",
        acceptance=["Only the declared output is mandatory."],
        template_id="bugfix_with_regression",
        production_paths=["src/a.py", "src/helper.py"],
        test_paths=["tests/test_a.py"],
        mandatory_changed_outputs=["src/a.py"],
    )
    assert result["ok"] is True
    assert captured["allowed_writes"] == [
        "src/a.py",
        "src/helper.py",
        "tests/test_a.py",
    ]
    assert captured["required_outputs"] == ["src/a.py"]


def test_create_from_template_rejects_duplicate_mandatory_outputs_before_create(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        server.core, "create_task", lambda **kwargs: calls.append(kwargs)
    )
    result = server.aiworkhub_task_create_from_template(
        task_id="TASK_TEMPLATE_BUGFIX_DUPLICATE",
        title="Reject duplicate outputs",
        runner="codex_worker",
        topic="coding",
        objective="Fail closed before persistence.",
        acceptance=["Duplicate paths are rejected."],
        template_id="bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
        mandatory_changed_outputs=["src/a.py", "src/a.py"],
    )
    assert result["ok"] is False
    assert result["stderr"] == "invalid_mandatory_changed_output_path_duplicate"
    assert calls == []


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
    assert result["template_provenance"]["schema_id"] == PROVENANCE_SCHEMA_ID


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
    malformed = server.aiworkhub_task_create_from_template(
        template_id="bugfix_with_regression@v1", **kwargs
    )
    assert malformed["ok"] is False
    assert malformed["stderr"] == "template_id_malformed"
    digest_mismatch = server.aiworkhub_task_create_from_template(
        template_id=forged, **kwargs
    )
    assert digest_mismatch["ok"] is False
    assert digest_mismatch["stderr"] == "template_digest_mismatch"
    stale_result = server.aiworkhub_task_create_from_template(
        template_id=stale, **kwargs
    )
    assert stale_result["ok"] is False
    assert stale_result["stderr"] == "template_version_stale"
    unsafe = server.aiworkhub_task_create_from_template(
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
    assert unsafe["ok"] is False
    assert unsafe["stderr"].startswith("invalid_production_path")
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
    "cross_boundary_bugfix": {
        "production_paths": ["src/a.py", "src/a.js"],
        "test_paths": ["tests/test_a.py", "tests/a.test.js"],
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


def _expected_template_card(template_id):
    paths = _TEMPLATE_PATHS[template_id]
    return expand_template(
        template_id,
        production_paths=paths.get("production_paths"),
        test_paths=paths.get("test_paths"),
        title="Roundtrip template card",
        objective="Authenticate a JSON round-tripped provenance receipt.",
    )


def _create_task_with_provenance(card, *, task_id, template_provenance):
    return server.core.create_task(
        task_id=task_id,
        title="Roundtrip template card",
        runner="codex_worker",
        topic="coding",
        objective="Authenticate a JSON round-tripped provenance receipt.",
        acceptance=["Card matches the template."],
        allowed_writes=list(card["allowed_writes"]),
        required_outputs=list(card["required_outputs"]),
        validation=list(card["validation"]),
        validation_roles=list(card["validation_roles"]),
        read_first=list(card["read_first"]),
        work_kind=card["work_kind"],
        read_only=card["read_only"],
        template_provenance=template_provenance,
        callback_required=False,
    )


def test_real_core_authenticates_json_roundtripped_provenance_every_template(
    monkeypatch, tmp_path
):
    # Every built-in template, including read_only_analysis, must create through
    # real core when its provenance is a plain JSON receipt (persisted and
    # reloaded, so the bound source card is gone). Core authenticates it against
    # the exact card fields it will create, restoring the writable card's
    # authoritative minimality contract before the embedded comparison.
    _enable_real_core_create(monkeypatch, tmp_path)
    for template_id in TEMPLATE_IDS:
        card = _expected_template_card(template_id)
        provenance = template_provenance_payload(
            card, classification_reason="explicit_template"
        )
        plain = json.loads(json.dumps(provenance))
        assert not hasattr(plain, "expanded_card")
        result = _create_task_with_provenance(
            card,
            task_id=f"TASK_ROUNDTRIP_{template_id.upper()}",
            template_provenance=plain,
        )
        assert result.get("ok") is True, (template_id, result)
        assert result.get("created") is True, (template_id, result)
        stored = json.loads(result["stdout"])
        bound = stored["template_provenance"]
        assert bound["template_name"] == template_id, (template_id, bound)
        assert bound["expanded_contract_digest"] == (
            provenance["expanded_contract_digest"]
        )


def test_real_core_rejects_tampered_minimality_roundtripped_provenance(
    monkeypatch, tmp_path
):
    _enable_real_core_create(monkeypatch, tmp_path)
    card = _expected_template_card("implementation_with_tests")
    provenance = template_provenance_payload(
        card, classification_reason="explicit_template"
    )
    plain = json.loads(json.dumps(provenance))
    # Strip the authoritative minimality contract from the embedded expansion:
    # the receipt no longer matches the writable card real core will create.
    plain["expanded_contract"]["minimality_contract"] = ""
    result = _create_task_with_provenance(
        card,
        task_id="TASK_ROUNDTRIP_TAMPER_MINIMALITY",
        template_provenance=plain,
    )
    assert result.get("ok") is False, result
    assert result.get("created") is not True, result
    assert result["stderr"] == "template_expanded_contract_mismatch", result


def test_real_core_rejects_tampered_paths_roundtripped_provenance(
    monkeypatch, tmp_path
):
    _enable_real_core_create(monkeypatch, tmp_path)
    card = _expected_template_card("implementation_with_tests")
    provenance = template_provenance_payload(
        card, classification_reason="explicit_template"
    )
    plain = json.loads(json.dumps(provenance))
    # Forge the receipt's declared write scope; it no longer matches the card.
    plain["expanded_contract"]["allowed_writes"] = ["src/evil.py"]
    result = _create_task_with_provenance(
        card,
        task_id="TASK_ROUNDTRIP_TAMPER_PATHS",
        template_provenance=plain,
    )
    assert result.get("ok") is False, result
    assert result.get("created") is not True, result
    assert result["stderr"] == "template_expanded_contract_mismatch", result


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
    production = server.aiworkhub_task_create_from_template(
            template_id="bugfix_with_regression",
            production_paths=[path],
            test_paths=["tests/test_a.py"],
            **kwargs,
    )
    assert production["ok"] is False
    assert production["stderr"] == "invalid_production_path_leading_hyphen"
    test_path = server.aiworkhub_task_create_from_template(
            template_id="bugfix_with_regression",
            production_paths=["src/a.py"],
            test_paths=[path],
            **kwargs,
    )
    assert test_path["ok"] is False
    assert test_path["stderr"] == "invalid_test_path_leading_hyphen"
    assert calls == []
