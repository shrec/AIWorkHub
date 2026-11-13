"""NF-2026-02251 / NF-2026-02258: Dart/Flutter worker trees and task template.

(1) A Dart/Flutter repository (``pubspec.yaml`` at the root) gets the full
checkout for a card that states no ``checkout``: the Python-import sparse
closure omitted ``lib/``, ``test/``, ``pubspec.*`` and ``analysis_options.yaml``
and ``flutter analyze`` reported hundreds of false errors.  An explicit
``checkout`` still wins, and Python repositories keep the sparse default.

(2) A ``lib/**.dart`` + ``test/**.dart`` card validated by ``flutter analyze``
and ``flutter test`` classifies to a registered template without the audited
custom escape, carrying the bugfix or generic role gate.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import dart_project, task_templates, worker_materialize, worker_workspace  # noqa: E402
from aiworkhub.task_templates import (  # noqa: E402
    DART_FLUTTER_TEMPLATE_NAME,
    TEMPLATE_IDS,
    TaskTemplateError,
    classify_task_card,
    expand_template,
)
from aiworkhub.worker_materialize import (  # noqa: E402
    CardCheckoutError,
    CheckoutPolicy,
    ExplicitCheckout,
)

DART_FILES = {
    "pubspec.yaml": "name: mego\n",
    "pubspec.lock": "packages: {}\n",
    "analysis_options.yaml": "include: package:flutter_lints/flutter.yaml\n",
    "lib/main.dart": "void main() {}\n",
    "lib/src/widget.dart": "class Widget {}\n",
    "test/widget_test.dart": "void main() {}\n",
    "README.md": "readme\n",
}
PYTHON_FILES = {
    "src/app.py": "VALUE = 1\n",
    "configs/a.json": '{"a": 1}\n',
    "README.md": "readme\n",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "tester")
    for relative, text in {**files, ".gitignore": ".aiworkhub/runtime/\n"}.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _card(allowed: str, **extra) -> dict:
    return {
        "allowed_writes": [allowed],
        "required_outputs": [],
        "read_first": [],
        "immutable_inputs": [],
        **extra,
    }


@pytest.fixture
def workspaces():
    created: list = []
    yield created
    for workspace in created:
        worker_workspace.cleanup_workspace(workspace.repo, workspace.path, workspace.home)


def _create(repo: Path, request_id: str, card: dict, created: list):
    workspace = worker_workspace.create_workspace(repo, request_id, card, "validation")
    created.append(workspace)
    return workspace


# --- (1) checkout resolution ------------------------------------------------


def test_dart_project_detection(tmp_path):
    assert dart_project.is_dart_project(tmp_path) is False
    (tmp_path / "pubspec.yaml").mkdir()
    assert dart_project.is_dart_project(tmp_path) is False  # a directory is not the marker
    (tmp_path / "pubspec.yaml").rmdir()
    (tmp_path / "pubspec.yaml").write_text("name: x\n", encoding="utf-8")
    assert dart_project.is_dart_project(tmp_path) is True


def test_dart_repo_defaults_to_full_and_explicit_checkout_wins(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: x\n", encoding="utf-8")
    card = _card("lib/main.dart", forbidden=["secrets/key.json"])
    policy = worker_materialize.checkout_policy(card, tmp_path)
    assert policy.checkout == "full"
    assert policy.forbidden == ("secrets/key.json",)
    assert policy.sparse_patterns(["/lib/main.dart"]) == ["/*", "!/secrets/key.json"]
    assert worker_materialize.checkout_policy({**card, "checkout": "sparse"}, tmp_path) == (
        CheckoutPolicy()
    )
    assert worker_materialize.checkout_policy({**card, "checkout": "full"}, tmp_path).checkout == (
        "full"
    )


def test_python_repo_keeps_the_sparse_default(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert worker_materialize.repo_default_checkout(tmp_path) == "sparse"
    assert worker_materialize.checkout_policy(_card("src/app.py"), tmp_path) == CheckoutPolicy()


def test_unreadable_repo_root_refuses(tmp_path, monkeypatch):
    def denied(_repo):
        raise PermissionError("denied")

    monkeypatch.setattr(worker_materialize, "is_dart_project", denied)
    with pytest.raises(CardCheckoutError, match="checkout_invalid:repo_probe_failed"):
        worker_materialize.checkout_policy(_card("lib/main.dart"), tmp_path)


def test_explicit_sparse_is_persisted_but_absent_is_not():
    _globs, absent = worker_materialize.validate_card_checkout(None, None)
    assert absent == "sparse" and not isinstance(absent, ExplicitCheckout)
    assert worker_materialize.card_checkout_fields([], absent) == {}
    _globs, stated = worker_materialize.validate_card_checkout(None, "SPARSE")
    assert stated == "sparse" and isinstance(stated, ExplicitCheckout)
    fields = worker_materialize.card_checkout_fields([], stated)
    assert fields == {"checkout": "sparse"} and type(fields["checkout"]) is str


def test_dart_worker_tree_has_the_whole_project(tmp_path, workspaces):
    repo = _repo(tmp_path, DART_FILES)
    workspace = _create(repo, "req-nf02251-dart", _card("lib/main.dart"), workspaces)
    for relative in DART_FILES:
        assert (workspace.path / relative).exists(), relative
    assert worker_workspace.changed_paths(workspace) == []
    (workspace.path / "test/widget_test.dart").write_text("// changed\n", encoding="utf-8")
    with pytest.raises(
        worker_workspace.WorkspaceError, match="scope_violation:test/widget_test.dart"
    ):
        worker_workspace.enforce_scope(workspace)


def test_dart_worker_tree_honours_explicit_sparse(tmp_path, workspaces):
    repo = _repo(tmp_path, DART_FILES)
    card = _card("lib/main.dart", checkout="sparse")
    workspace = _create(repo, "req-nf02251-sparse", card, workspaces)
    assert (workspace.path / "lib/main.dart").exists()
    assert not (workspace.path / "pubspec.yaml").exists()
    assert not (workspace.path / "test/widget_test.dart").exists()


def test_python_worker_tree_stays_sparse(tmp_path, workspaces):
    repo = _repo(tmp_path, PYTHON_FILES)
    workspace = _create(repo, "req-nf02251-py", _card("src/app.py"), workspaces)
    assert (workspace.path / "src/app.py").exists()
    assert not (workspace.path / "configs/a.json").exists()
    assert not (workspace.path / "README.md").exists()


# --- (2) Dart/Flutter template ----------------------------------------------

WRITES = ["lib/main.dart", "lib/src/widget.dart", "test/widget_test.dart"]
FLUTTER = ["flutter analyze", "flutter test"]


def _classify(work_kind: str, roles: list[str], **extra):
    return classify_task_card(
        allowed_writes=list(WRITES),
        required_outputs=list(WRITES),
        validation=list(FLUTTER),
        validation_roles=roles,
        work_kind=work_kind,
        read_only=False,
        read_first=list(WRITES),
        **extra,
    )


@pytest.mark.parametrize(
    ("work_kind", "roles"),
    [("bugfix", ["reproduction", "regression"]), ("generic", ["generic", "generic"])],
)
def test_dart_flutter_card_classifies_without_custom_escape(work_kind, roles):
    provenance = _classify(work_kind, roles)
    assert provenance["template_name"] == DART_FLUTTER_TEMPLATE_NAME
    assert provenance["classification_reason"] == "compatible_dart_flutter_with_tests"
    # The stored card re-authenticates against a fresh expansion.
    persisted = {
        "allowed_writes": list(WRITES),
        "required_outputs": list(WRITES),
        "validation": list(FLUTTER),
        "validation_roles": roles,
        "work_kind": work_kind,
        "read_only": False,
        "read_first": list(WRITES),
        "minimality_contract": task_templates.CANONICAL_MINIMALITY_CONTRACT,
    }
    assert classify_task_card(**persisted, template_provenance=provenance) == provenance


def test_dart_template_expansion_is_exact():
    card = expand_template(DART_FLUTTER_TEMPLATE_NAME, production_paths=WRITES, work_kind="bugfix")
    assert card["validation"] == FLUTTER
    assert card["validation_roles"] == ["reproduction", "regression"]
    assert card["allowed_writes"] == WRITES
    assert card["required_outputs"] == WRITES
    assert card["read_first"] == WRITES
    assert card["work_kind"] == "bugfix"
    generic = expand_template(DART_FLUTTER_TEMPLATE_NAME, production_paths=WRITES)
    assert generic["work_kind"] == "generic"
    assert generic["validation_roles"] == ["generic", "generic"]
    assert DART_FLUTTER_TEMPLATE_NAME not in TEMPLATE_IDS
    assert DART_FLUTTER_TEMPLATE_NAME not in task_templates.TEMPLATE_SPECS
    assert task_templates.template_full_id(DART_FLUTTER_TEMPLATE_NAME).startswith(
        f"{DART_FLUTTER_TEMPLATE_NAME}@v"
    )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"production_paths": ["lib/a.dart"]}, "missing_test_paths"),
        ({"production_paths": ["test/a_test.dart"]}, "missing_production_paths"),
        (
            {"production_paths": ["lib/a.dart", "test/a_test.dart", "tool/x.dart"]},
            "dart_flutter_path_outside_lib_or_test",
        ),
        (
            {"production_paths": ["lib/a.dart", "test/a_test.dart"], "work_kind": "refactor"},
            "dart_flutter_work_kind_unsupported",
        ),
        (
            {
                "production_paths": ["lib/a.dart", "test/a_test.dart"],
                "mandatory_changed_outputs": [],
            },
            "dart_flutter_requires_required_outputs",
        ),
        (
            {
                "production_paths": ["lib/a.dart", "test/a_test.dart"],
                "validation": ["flutter test"],
            },
            "template_validation_not_caller_supplied",
        ),
    ],
)
def test_dart_template_refuses_out_of_contract_cards(kwargs, reason):
    with pytest.raises(TaskTemplateError, match=reason):
        expand_template(DART_FLUTTER_TEMPLATE_NAME, **kwargs)


def test_builtin_templates_still_refuse_work_kind():
    with pytest.raises(TaskTemplateError, match="template_validation_not_caller_supplied"):
        expand_template(
            "bugfix_with_regression",
            production_paths=["src/a.py"],
            test_paths=["tests/test_a.py"],
            work_kind="bugfix",
        )


def test_python_card_keeps_its_builtin_identity():
    card = expand_template(
        "bugfix_with_regression", production_paths=["src/a.py"], test_paths=["tests/test_a.py"]
    )
    provenance = classify_task_card(
        **{
            key: card[key]
            for key in (
                "allowed_writes",
                "required_outputs",
                "validation",
                "validation_roles",
                "work_kind",
                "read_only",
                "read_first",
            )
        }
    )
    assert provenance["template_name"] == "bugfix_with_regression"
