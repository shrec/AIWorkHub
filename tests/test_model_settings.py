from __future__ import annotations

import json
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import model_settings, task_store  # noqa: E402


def test_grok_kilo_has_repository_local_xai_policy_identity() -> None:
    assert model_settings.policy_identity_for_adapter("grok_kilo_cli") == (
        "xai",
        "grok_kilo_cli",
    )


def _repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    task_store.initialize_repository(root)
    return root


def _write_settings(root: Path, text: str) -> None:
    path = model_settings.settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_absent_settings_enable_arbitrary_routes_without_writing(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, "root")
    payload = model_settings.load(root)
    assert payload["ok"] is True
    assert payload["schema_id"] == model_settings.SCHEMA_ID
    assert payload["revision"] == 0
    assert payload["configured"] is False
    assert payload["providers"] == {}
    assert payload["adapters"] == {}
    assert payload["models"] == {}
    assert not model_settings.settings_path(root).exists()

    assert model_settings.evaluate(root, provider="azure.example") is True
    assert (
        model_settings.evaluate(root, provider="azure.example", adapter="http+retry")
        is True
    )
    assert (
        model_settings.evaluate(
            root, provider="azure.example", adapter="http+retry", model="gpt-4o@2024"
        )
        is True
    )


def test_provider_disable_cascades_and_adapter_disable_stays_scoped(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, "root")
    disabled = model_settings.update(
        root, provider="openai", enabled=False, expected_revision=0
    )
    assert disabled["revision"] == 1
    assert disabled["providers"] == {"openai": False}
    assert disabled["configured"] is True

    assert model_settings.evaluate(root, provider="openai") is False
    assert model_settings.evaluate(root, provider="openai", adapter="stdio") is False
    assert (
        model_settings.evaluate(root, provider="openai", adapter="stdio", model="fresh")
        is False
    )
    assert (
        model_settings.evaluate(root, provider="anthropic", adapter="stdio", model="claude")
        is True
    )

    scoped = model_settings.update(
        root, provider="vertex", adapter="grpc", enabled=False, expected_revision=1
    )
    assert scoped["adapters"] == {"vertex": {"grpc": False}}
    assert model_settings.evaluate(root, provider="vertex") is True
    assert model_settings.evaluate(root, provider="vertex", adapter="grpc") is False
    assert model_settings.evaluate(root, provider="vertex", adapter="http") is True
    assert model_settings.evaluate(root, provider="anthropic", adapter="grpc") is True


def test_exact_model_override_refines_only_enabled_ancestors(tmp_path: Path) -> None:
    root = _repo(tmp_path, "root")
    model_settings.update(root, provider="openai", enabled=True, expected_revision=0)
    model_settings.update(
        root, provider="openai", adapter="stdio", enabled=True, expected_revision=1
    )
    override = model_settings.update(
        root,
        provider="openai",
        adapter="stdio",
        model="gpt-turbo",
        enabled=False,
        expected_revision=2,
    )
    assert override["revision"] == 3
    assert override["models"] == {"openai": {"stdio": {"gpt-turbo": False}}}

    assert (
        model_settings.evaluate(root, provider="openai", adapter="stdio", model="gpt-turbo")
        is False
    )
    assert (
        model_settings.evaluate(root, provider="openai", adapter="stdio", model="gpt-other")
        is True
    )
    assert (
        model_settings.evaluate(root, provider="openai", adapter="http", model="gpt-turbo")
        is True
    )

    model_settings.update(root, provider="openai", enabled=False, expected_revision=3)
    model_settings.update(
        root,
        provider="openai",
        adapter="stdio",
        model="gpt-turbo",
        enabled=True,
        expected_revision=4,
    )
    assert not model_settings.evaluate(
        root, provider="openai", adapter="stdio", model="gpt-turbo"
    )

    model_settings.update(
        root, provider="a/b", adapter="c", model="d/e", enabled=False, expected_revision=5
    )
    assert model_settings.evaluate(root, provider="a/b", adapter="c", model="d/e") is False
    assert model_settings.evaluate(root, provider="a", adapter="b/c", model="d/e") is True


def test_discovered_models_inherit_until_explicitly_overridden(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, "root")
    model_settings.update(root, provider="bedrock", enabled=True, expected_revision=0)
    model_settings.update(
        root,
        provider="bedrock",
        adapter="http",
        model="titan-pro",
        enabled=False,
        expected_revision=1,
    )

    assert (
        model_settings.evaluate(root, provider="bedrock", adapter="http", model="titan-pro")
        is False
    )
    assert (
        model_settings.evaluate(root, provider="bedrock", adapter="http", model="titan-new")
        is True
    )
    assert (
        model_settings.evaluate(root, provider="bedrock", adapter="smtp", model="titan-pro")
        is True
    )
    assert model_settings.evaluate(root, provider="fresh", adapter="any", model="any") is True


def test_update_cas_and_identity_validation_fail_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path, "root")
    model_settings.update(root, provider="openai", enabled=False, expected_revision=0)

    with pytest.raises(model_settings.ModelSettingsError, match="revision_conflict"):
        model_settings.update(root, provider="openai", enabled=True, expected_revision=0)
    with pytest.raises(model_settings.ModelSettingsError, match="revision_conflict"):
        model_settings.update(root, provider="openai", enabled=True, expected_revision=True)
    with pytest.raises(model_settings.ModelSettingsError, match="enabled_must_be_bool"):
        model_settings.update(root, provider="openai", enabled="on", expected_revision=1)
    with pytest.raises(model_settings.ModelSettingsError, match="requires_adapter"):
        model_settings.update(
            root, provider="openai", model="gpt-x", enabled=True, expected_revision=1
        )
    with pytest.raises(model_settings.ModelSettingsError, match="identity_invalid"):
        model_settings.update(root, provider=123, enabled=True, expected_revision=1)
    with pytest.raises(model_settings.ModelSettingsError, match="identity_invalid"):
        model_settings.update(root, provider="", enabled=True, expected_revision=1)
    with pytest.raises(model_settings.ModelSettingsError, match="identity_too_long"):
        model_settings.update(
            root,
            provider="x" * (model_settings.MAX_IDENTITY_CHARS + 1),
            enabled=True,
            expected_revision=1,
        )
    with pytest.raises(
        model_settings.ModelSettingsError, match="control_characters"
    ):
        model_settings.update(root, provider="bad\x00id", enabled=True, expected_revision=1)
    with pytest.raises(model_settings.ModelSettingsError, match="identity_invalid"):
        model_settings.evaluate(root, provider=None)
    with pytest.raises(model_settings.ModelSettingsError, match="requires_adapter"):
        model_settings.evaluate(root, provider="openai", model="gpt-x")


def test_update_requires_initialized_repository(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(
        model_settings.ModelSettingsError, match="repository_not_initialized"
    ):
        model_settings.update(bare, provider="openai", enabled=False, expected_revision=0)


def test_persisted_schema_is_strict_bounded_and_regular_only(tmp_path: Path) -> None:
    root = _repo(tmp_path, "root")
    schema = model_settings.SCHEMA_ID

    _write_settings(root, "{}")
    with pytest.raises(model_settings.ModelSettingsError, match="schema_invalid"):
        model_settings.load(root)

    _write_settings(
        root,
        json.dumps(
            {
                "schema_id": schema,
                "revision": 1,
                "updated_at": "",
                "providers": {},
                "adapters": {},
                "models": {},
                "api_key": "secret-value",
            }
        ),
    )
    with pytest.raises(model_settings.ModelSettingsError, match="unknown_field"):
        model_settings.load(root)

    _write_settings(
        root,
        json.dumps(
            {
                "schema_id": schema,
                "revision": 1,
                "providers": {"openai": "off"},
                "adapters": {},
                "models": {},
            }
        ),
    )
    with pytest.raises(model_settings.ModelSettingsError, match="not_boolean"):
        model_settings.load(root)

    _write_settings(
        root, f'{{"schema_id": "{schema}", "schema_id": "{schema}", "revision": 1}}'
    )
    with pytest.raises(model_settings.ModelSettingsError, match="duplicate_key"):
        model_settings.load(root)

    _write_settings(
        root,
        json.dumps(
            {
                "schema_id": schema,
                "revision": 0,
                "providers": {},
                "adapters": {},
                "models": {},
            }
        ),
    )
    with pytest.raises(model_settings.ModelSettingsError, match="revision_invalid"):
        model_settings.load(root)

    overlong = "p" * (model_settings.MAX_IDENTITY_CHARS + 1)
    _write_settings(
        root,
        json.dumps(
            {
                "schema_id": schema,
                "revision": 1,
                "providers": {overlong: False},
                "adapters": {},
                "models": {},
            }
        ),
    )
    with pytest.raises(model_settings.ModelSettingsError, match="identity_too_long"):
        model_settings.load(root)

    path = model_settings.settings_path(root)
    path.unlink()
    path.write_text("x" * (model_settings.MAX_SETTINGS_BYTES + 1), encoding="utf-8")
    with pytest.raises(model_settings.ModelSettingsError, match="too_large"):
        model_settings.load(root)

    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    path.symlink_to(outside)
    with pytest.raises(model_settings.ModelSettingsError, match="regular_file"):
        model_settings.load(root)

    path.unlink()
    path.mkdir()
    with pytest.raises(model_settings.ModelSettingsError, match="regular_file"):
        model_settings.load(root)


def test_atomic_write_and_repository_isolation(tmp_path: Path) -> None:
    first = _repo(tmp_path, "first")
    second = _repo(tmp_path, "second")

    one = model_settings.update(
        first, provider="openai", enabled=False, expected_revision=0
    )
    two = model_settings.update(
        second, provider="anthropic", adapter="http", enabled=False, expected_revision=0
    )
    assert one["revision"] == 1
    assert two["revision"] == 1

    stored = json.loads(
        model_settings.settings_path(first).read_text(encoding="utf-8")
    )
    assert stored["schema_id"] == model_settings.SCHEMA_ID
    assert set(stored) == {
        "schema_id",
        "revision",
        "updated_at",
        "providers",
        "adapters",
        "models",
    }
    assert stored["providers"] == {"openai": False}
    assert stat.S_IMODE(model_settings.settings_path(first).stat().st_mode) == 0o600

    config_dir = model_settings.settings_path(first).parent
    leftovers = [entry.name for entry in config_dir.iterdir() if ".tmp" in entry.name]
    assert leftovers == []

    assert model_settings.load(first)["configured"] is True
    assert model_settings.load(second)["providers"] == {}
    assert model_settings.load(second)["adapters"] == {"anthropic": {"http": False}}

    bumped_first = model_settings.update(
        first, provider="openai", enabled=True, expected_revision=1
    )
    bumped_second = model_settings.update(
        second, provider="anthropic", adapter="http", enabled=True, expected_revision=1
    )
    assert bumped_first["revision"] == 2
    assert bumped_second["revision"] == 2
    with pytest.raises(model_settings.ModelSettingsError, match="revision_conflict"):
        model_settings.update(
            second, provider="anthropic", enabled=False, expected_revision=1
        )


def test_cross_process_style_concurrent_cas_has_one_winner(tmp_path: Path) -> None:
    root = _repo(tmp_path, "root")
    model_settings.update(root, provider="seed", enabled=True, expected_revision=0)
    barrier = Barrier(2)

    def write(provider: str) -> str:
        barrier.wait(timeout=5)
        try:
            model_settings.update(
                root, provider=provider, enabled=False, expected_revision=1
            )
        except model_settings.ModelSettingsError as exc:
            return str(exc)
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ("first", "second")))

    assert results.count("ok") == 1
    loser = next(result for result in results if result != "ok")
    assert loser in {"model_settings_revision_conflict", "model_settings_update_busy"}
    state = model_settings.load(root)
    assert state["revision"] == 2
    assert sum(key in state["providers"] for key in ("first", "second")) == 1
