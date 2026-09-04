from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub.worker_workspace import MAX_SEED_FILES, WorkspaceError, _resolve_local_python_imports


def _write(repo: Path, relative: str, source: str = "") -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _package(repo: Path, source: str) -> None:
    _write(repo, "src/pkg/__init__.py")
    _write(repo, "src/pkg/seed.py", source)


def test_whitelisted_dynamic_imports_are_transitive_and_deterministic(tmp_path: Path) -> None:
    _package(tmp_path, "import importlib\nimportlib.import_module('pkg.absolute')\nimportlib.import_module('.relative', package=__package__)\n")
    _write(tmp_path, "src/pkg/absolute.py", "from pkg import leaf\n")
    _write(tmp_path, "src/pkg/relative.py")
    _write(tmp_path, "src/pkg/leaf.py")
    first = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))
    assert first == _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))
    assert {"src/pkg/absolute.py", "src/pkg/relative.py", "src/pkg/leaf.py"}.issubset(first)


def test_unaliased_importlib_in_multi_import_establishes_binding(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "import os, importlib\nimportlib.import_module('pkg.hidden')\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" in closure


def test_unaliased_dotted_importlib_import_establishes_top_level_binding(
    tmp_path: Path,
) -> None:
    _package(
        tmp_path,
        "import importlib.util\nimportlib.import_module('pkg.hidden')\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" in closure


@pytest.mark.parametrize(
    "source",
    [
        "import importlib.util as util\nimportlib.import_module('pkg.hidden')\n",
        "import importlib.util as importlib\nimportlib.import_module('pkg.hidden')\n",
    ],
)
def test_aliased_dotted_importlib_import_does_not_authenticate_top_level_binding(
    tmp_path: Path, source: str
) -> None:
    _package(tmp_path, source)
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure


@pytest.mark.parametrize(
    "call",
    [
        "importlib.import_module(name='pkg.hidden')",
        "importlib.import_module(name='.hidden', package=__package__)",
    ],
)
def test_keyword_name_dynamic_imports_are_resolved(tmp_path: Path, call: str) -> None:
    _package(tmp_path, f"import importlib\n{call}\n")
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" in closure


def test_constant_package_keyword_resolves_relative_import(tmp_path: Path) -> None:
    _package(tmp_path, "import importlib\nimportlib.import_module('.hidden', package='pkg')\n")
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" in closure


@pytest.mark.parametrize("package", ["", ".pkg", "pkg.", "pkg..child", "../pkg", "pkg/child"])
def test_malformed_constant_package_fails_closed(tmp_path: Path, package: str) -> None:
    _package(tmp_path, f"import importlib\nimportlib.import_module('.hidden', package={package!r})\n")
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure


@pytest.mark.parametrize(
    "call",
    [
        "importlib.import_module('pkg.hidden', name='pkg.hidden')",
        "importlib.import_module(name='pkg.hidden', name='pkg.hidden')",
        "importlib.import_module(name='pkg.hidden', **options)",
        "importlib.import_module(name='pkg.hidden', unsupported=True)",
        "importlib.import_module(name=module_name)",
        "importlib.import_module(name='.hidden', package=module_name)",
    ],
)
def test_ambiguous_or_nonconstant_keyword_names_are_rejected(
    tmp_path: Path, call: str
) -> None:
    _package(
        tmp_path,
        "import importlib\nmodule_name = 'pkg.hidden'\noptions = {}\n" + call + "\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure


def test_assigned_direct_call_does_not_disable_following_direct_call(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "import importlib\nloaded = importlib.import_module('pkg.one')\nimportlib.import_module('pkg.two')\n",
    )
    _write(tmp_path, "src/pkg/one.py")
    _write(tmp_path, "src/pkg/two.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert {"src/pkg/one.py", "src/pkg/two.py"}.issubset(closure)


def test_read_only_importlib_alias_preserves_direct_canonical_trust(
    tmp_path: Path,
) -> None:
    _package(
        tmp_path,
        "import importlib\nalias = importlib\n"
        "importlib.import_module('pkg.hidden')\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" in closure


def test_importlib_mutation_taint_survives_canonical_reimport(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "import importlib\nimportlib.import_module = replacement\n"
        "import importlib\nimportlib.import_module('pkg.hidden')\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure


def test_package_conditional_import_seeds_only_the_live_branch(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "import importlib\n"
        "importlib.import_module('.real' if __package__ else 'pkg.shadow', "
        "package=__package__)\n",
    )
    _write(tmp_path, "src/pkg/real.py")
    _write(tmp_path, "src/pkg/shadow.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/real.py" in closure
    assert "src/pkg/shadow.py" not in closure


def test_nonpackage_conditional_import_seeds_only_the_else_branch(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/seed.py",
        "import importlib\n"
        "importlib.import_module('pkg.shadow' if __package__ else 'live')\n",
    )
    _write(tmp_path, "src/live.py")
    _write(tmp_path, "src/pkg/shadow.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/seed.py",))

    assert "src/live.py" in closure
    assert "src/pkg/shadow.py" not in closure


@pytest.mark.parametrize(
    "rebind",
    [
        "import os as importlib",
        "from os import path as importlib",
        "def importlib():\n    pass",
        "async def importlib():\n    pass",
        "class importlib:\n    pass",
    ],
)
def test_module_level_importlib_rebinding_invalidates_trust(
    tmp_path: Path, rebind: str
) -> None:
    _package(
        tmp_path,
        "import importlib\n"
        + rebind
        + "\nimportlib.import_module('pkg.hidden')\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure


def test_platform_io_closure_includes_platform_process() -> None:
    repo = Path(__file__).resolve().parents[1]
    closure = _resolve_local_python_imports(repo, ("src/aiworkhub/platform_io.py",))
    assert "src/aiworkhub/_platform_process.py" in closure


@pytest.mark.parametrize("source", [
    "import importlib\ndef load():\n    importlib.import_module('pkg.hidden')\n",
    "import importlib\nload = importlib.import_module\nload('pkg.hidden')\n",
    "import importlib\nif flag:\n    importlib.import_module('pkg.hidden')\n",
    "import importlib as loader\nloader.import_module('pkg.hidden')\n",
    "import importlib\nload = lambda: importlib.import_module('pkg.hidden')\n",
    "import importlib\nFalse and importlib.import_module('pkg.hidden')\n",
    "import importlib\nvalue = None if True else importlib.import_module('pkg.hidden')\n",
    "import importlib\nvalue = [importlib.import_module('pkg.hidden')]\n",
    "import importlib\nconsume(importlib.import_module('pkg.hidden'))\n",
])
def test_nested_alias_and_compound_calls_are_rejected(tmp_path: Path, source: str) -> None:
    _package(tmp_path, source); _write(tmp_path, "src/pkg/hidden.py")
    assert "src/pkg/hidden.py" not in _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))


@pytest.mark.parametrize("mutation", [
    "importlib.import_module = replacement",
    "del importlib.import_module",
    "setattr(importlib, 'import_module', replacement)",
    "importlib.__dict__['import_module'] = replacement",
    "importlib.__dict__.update(import_module=replacement)",
    "vars(importlib)['import_module'] = replacement",
    "alias = importlib\nalias.import_module = replacement",
])
def test_importlib_mutation_fails_closed(tmp_path: Path, mutation: str) -> None:
    _package(tmp_path, "import importlib\nreplacement = lambda name: None\n" + mutation + "\nimportlib.import_module('pkg.hidden')\n")
    _write(tmp_path, "src/pkg/hidden.py")
    assert "src/pkg/hidden.py" not in _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))


def test_package_rebinding_does_not_reject_absolute_call(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "import importlib\n__package__ = 'tainted'\n"
        "importlib.import_module('pkg.hidden', package=__package__)\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")
    assert "src/pkg/hidden.py" in _resolve_local_python_imports(
        tmp_path, ("src/pkg/seed.py",)
    )


def test_package_rebinding_rejects_relative_call(tmp_path: Path) -> None:
    _package(tmp_path, "import importlib\n__package__ = 'pkg'\nimportlib.import_module('.hidden', package=__package__)\n")
    _write(tmp_path, "src/pkg/hidden.py")
    assert "src/pkg/hidden.py" not in _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))


def test_package_rebinding_rejects_package_dependent_name_ifexp(
    tmp_path: Path,
) -> None:
    _package(
        tmp_path,
        "import importlib\n__package__ = 'tainted'\n"
        "importlib.import_module('pkg.hidden' if __package__ else 'pkg.other')\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")
    _write(tmp_path, "src/pkg/other.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure
    assert "src/pkg/other.py" not in closure


@pytest.mark.parametrize(
    ("mutation", "call"),
    [
        ("globals()['importlib'] = replacement", "importlib.import_module('pkg.hidden')"),
        ("locals()['importlib'] += replacement", "importlib.import_module('pkg.hidden')"),
        ("del globals()['importlib']", "importlib.import_module('pkg.hidden')"),
        ("module.__dict__['importlib'] = replacement", "importlib.import_module('pkg.hidden')"),
        ("globals()['__package__'] = 'pkg'", "importlib.import_module('.hidden', package=__package__)"),
        ("locals()['__package__'] += 'pkg'", "importlib.import_module('.hidden', package=__package__)"),
        ("del module.__dict__['__package__']", "importlib.import_module('.hidden', package=__package__)"),
    ],
)
def test_indirect_global_rebinding_fails_closed(
    tmp_path: Path, mutation: str, call: str
) -> None:
    _package(
        tmp_path,
        "import importlib\nreplacement = importlib\nmodule = importlib\n"
        + mutation
        + "\n"
        + call
        + "\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure


@pytest.mark.parametrize(
    "mutation",
    [
        "globals().update(importlib=replacement)",
        "locals().update(importlib=replacement)",
        "module.__dict__.update(importlib=replacement)",
        "module.__dict__.__setitem__('importlib', replacement)",
    ],
)
def test_namespace_update_calls_are_sticky_barriers(
    tmp_path: Path, mutation: str
) -> None:
    _package(
        tmp_path,
        "import importlib\nreplacement = importlib\nmodule = importlib\n"
        + mutation
        + "\nimportlib.import_module('pkg.hidden')\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure


def test_unknown_module_call_is_sticky_barrier(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "import importlib\nunknown_side_effect()\n"
        "importlib.import_module('pkg.hidden')\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure


def test_arbitrary_getattr_call_is_sticky_barrier(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "import importlib\ngetattr(arbitrary, 'constant')\n"
        "importlib.import_module('pkg.hidden')\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure


def test_constant_getattr_on_unaliased_stdlib_module_preserves_trust(
    tmp_path: Path,
) -> None:
    _package(
        tmp_path,
        "import importlib\nimport math\n"
        "fallback = getattr(math, 'tau', 6.28)\n"
        "importlib.import_module('pkg.hidden')\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" in closure


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimport math as numbers\n"
        "getattr(numbers, 'tau', 6.28)\n",
        "import importlib\nimport math\nmath = replacement\n"
        "getattr(math, 'tau', 6.28)\n",
        "import importlib\nimport math\nprior_side_effect()\n"
        "getattr(math, 'tau', 6.28)\n",
        "import importlib\nimport math\ngetattr = replacement\n"
        "getattr(math, 'tau', 6.28)\n",
        "import importlib\nimport math\ngetattr(math, attribute, 6.28)\n",
        "import importlib\nimport math\ngetattr(math, 'tau', fallback())\n",
    ],
)
def test_unproven_stdlib_getattr_remains_a_barrier(
    tmp_path: Path, source: str
) -> None:
    _package(
        tmp_path,
        source + "importlib.import_module('pkg.hidden')\n",
    )
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure


def test_repo_local_stdlib_name_does_not_authenticate_getattr(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "import importlib\nimport math\ngetattr(math, 'tau', 6.28)\n"
        "importlib.import_module('pkg.hidden')\n",
    )
    _write(tmp_path, "src/math.py")
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure


def test_repo_local_top_level_stdlib_package_blocks_dotted_import_getattr_trust(
    tmp_path: Path,
) -> None:
    _package(
        tmp_path,
        "import importlib\nimport os.path\ngetattr(os, 'fspath')\n"
        "importlib.import_module('pkg.hidden')\n",
    )
    _write(tmp_path, "src/os/__init__.py")
    _write(tmp_path, "src/pkg/hidden.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/hidden.py" not in closure


def test_runtime_third_party_and_missing_names_do_not_broaden(tmp_path: Path) -> None:
    _package(tmp_path, "import importlib\nname = 'pkg.hidden'\nimportlib.import_module(name)\nimportlib.import_module('third_party')\nimportlib.import_module('pkg.missing')\n")
    _write(tmp_path, "src/pkg/hidden.py")
    assert _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",)) == ("src/pkg/seed.py",)


def test_repo_local_importlib_shadow_rejects_authentication(tmp_path: Path) -> None:
    _package(tmp_path, "import importlib\nimportlib.import_module('pkg.hidden')\n")
    _write(tmp_path, "src/pkg/hidden.py"); _write(tmp_path, "src/importlib.py")
    assert "src/pkg/hidden.py" not in _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))


def test_dynamic_import_symlink_is_rejected(tmp_path: Path) -> None:
    _package(tmp_path, "import importlib\nimportlib.import_module('pkg.linked')\n")
    _write(tmp_path, "outside.py")
    (tmp_path / "src/pkg/linked.py").symlink_to(tmp_path / "outside.py")
    with pytest.raises(WorkspaceError, match="symlink"):
        _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))


def test_relative_dynamic_import_cannot_escape_package(tmp_path: Path) -> None:
    _package(tmp_path, "import importlib\nimportlib.import_module('..outside', package=__package__)\n")
    _write(tmp_path, "src/outside.py")
    with pytest.raises(WorkspaceError, match="relative_import_unresolved"):
        _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))


def test_dynamic_closure_preserves_seed_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import aiworkhub.worker_workspace as workspace
    _package(tmp_path, "import importlib\nimportlib.import_module('pkg.one')\n")
    _write(tmp_path, "src/pkg/one.py")
    monkeypatch.setattr(workspace, "MAX_SEED_FILES", 2)
    with pytest.raises(WorkspaceError, match="seed_file_limit_exceeded"):
        _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))
    assert MAX_SEED_FILES >= 2


def test_module_level_star_import_sticky_taints_dynamic_import_trust(
    tmp_path: Path,
) -> None:
    _package(
        tmp_path,
        "import importlib\n"
        "from pkg.exports import *\n"
        "import importlib\n"
        "importlib.import_module('pkg.absolute')\n"
        "importlib.import_module('.relative', package=__package__)\n",
    )
    _write(tmp_path, "src/pkg/exports.py")
    _write(tmp_path, "src/pkg/absolute.py")
    _write(tmp_path, "src/pkg/relative.py")

    closure = _resolve_local_python_imports(tmp_path, ("src/pkg/seed.py",))

    assert "src/pkg/exports.py" in closure
    assert "src/pkg/absolute.py" not in closure
    assert "src/pkg/relative.py" not in closure
