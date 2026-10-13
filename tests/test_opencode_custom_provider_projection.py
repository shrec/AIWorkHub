"""Projection tests for owner-configured custom OpenCode providers (NF-2026-01502)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aiworkhub import opencode_auth, runtime_adapters

_SENTINEL = "sentinel-key-7f32b98a"


def _owner_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: dict | None = None,
    auth: dict | None = None,
) -> Path:
    config_home = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    opencode_dir = config_home / "opencode"
    opencode_dir.mkdir(parents=True)
    if provider is not None:
        (opencode_dir / "opencode.json").write_text(
            json.dumps({"provider": provider}), encoding="utf-8"
        )
    if auth is not None:
        (opencode_dir / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    return opencode_dir


def _provider_block(**options: object) -> dict:
    return {
        "omniroute": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "OmniRoute",
            "options": {"baseURL": "http://localhost:20128/v1", **options},
            "models": {"router-model": {"name": "Router Model"}},
        }
    }


@pytest.mark.parametrize("model", ["opencode/muse-spark", "opencode-go/muse-spark"])
def test_builtin_providers_project_nothing(model: str) -> None:
    assert opencode_auth.project_opencode_custom_provider(model) is None


@pytest.mark.parametrize("model", ["", "no-slash-model"])
def test_model_without_provider_prefix_projects_nothing(model: str) -> None:
    assert opencode_auth.project_opencode_custom_provider(model) is None


def test_projection_picks_only_the_named_provider_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider_block(apiKey="literal-key")
    provider["omniroute"]["models"]["other-model"] = {"name": "Other"}
    provider["acme"] = {"models": {"x": {}}}
    _owner_config(tmp_path, monkeypatch, provider=provider)

    projected = opencode_auth.project_opencode_custom_provider(
        "omniroute/router-model", host_env={}
    )

    assert projected == {
        "omniroute": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "OmniRoute",
            "options": {
                "baseURL": "http://localhost:20128/v1",
                "apiKey": "literal-key",
            },
            "models": {"router-model": {}},
        }
    }


def test_env_credential_resolves_from_host_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_config(
        tmp_path, monkeypatch, provider=_provider_block(apiKey="{env:OMNIROUTE_KEY}")
    )
    projected = opencode_auth.project_opencode_custom_provider(
        "omniroute/router-model", host_env={"OMNIROUTE_KEY": _SENTINEL}
    )
    assert projected["omniroute"]["options"]["apiKey"] == _SENTINEL


def test_env_credential_missing_from_host_env_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_config(
        tmp_path, monkeypatch, provider=_provider_block(apiKey="{env:OMNIROUTE_KEY}")
    )
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_env_unresolved$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}
        )


def test_missing_api_key_projects_without_apikey_and_ignores_auth_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_config(
        tmp_path,
        monkeypatch,
        provider=_provider_block(),
        auth={"omniroute": {"type": "api", "key": _SENTINEL}},
    )
    projected = opencode_auth.project_opencode_custom_provider(
        "omniroute/router-model", host_env={}
    )
    assert "apiKey" not in projected["omniroute"]["options"]


@pytest.mark.parametrize("api_key", ["{file:/etc/secret}", "{vault:omniroute}", "{}"])
def test_unsupported_credential_substitution_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    _owner_config(tmp_path, monkeypatch, provider=_provider_block(apiKey=api_key))
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_credential_unsupported$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}
        )


def test_missing_provider_block_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_config(tmp_path, monkeypatch, provider={})
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_not_configured$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}
        )


def test_missing_owner_config_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config-missing"))
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_not_configured$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}
        )


@pytest.mark.parametrize(
    "raw",
    [
        '{"provider": ' + "9" * 5000 + "}",
        "[" * 100_000,
        '{/* OmniRoute */ "provider": {}}',
        '{"provider": {},}',
    ],
    ids=["int-over-digit-limit", "deep-nesting", "jsonc-comment", "trailing-comma"],
)
def test_unparseable_owner_config_is_refused_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    opencode_dir = _owner_config(tmp_path, monkeypatch)
    (opencode_dir / "opencode.json").write_text(raw, encoding="utf-8")
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_config_json_invalid$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}
        )


def test_empty_owner_config_is_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opencode_dir = _owner_config(tmp_path, monkeypatch)
    (opencode_dir / "opencode.json").write_text("{}", encoding="utf-8")
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_not_configured$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}
        )


def _provider_with_model_id(model_id: str) -> dict:
    provider = _provider_block()
    provider["omniroute"]["models"]["router-model"]["id"] = model_id
    return provider


def test_model_id_env_template_is_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_config(
        tmp_path, monkeypatch, provider=_provider_with_model_id("{env:OMNI_MODEL}")
    )
    projected = opencode_auth.project_opencode_custom_provider(
        "omniroute/router-model", host_env={"OMNI_MODEL": "gpt-x"}
    )
    assert projected["omniroute"]["models"] == {"router-model": {"id": "gpt-x"}}
    runtime_adapters._check_opencode_worker_provider(projected)


@pytest.mark.parametrize(
    ("model_id", "reason"),
    [
        ("{env:OMNI_MODEL}", "opencode_provider_env_unresolved"),
        ("{file:/etc/x}", "opencode_provider_credential_unsupported"),
    ],
    ids=["env-unset", "file"],
)
def test_unresolvable_model_id_is_refused_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_id: str, reason: str
) -> None:
    _owner_config(tmp_path, monkeypatch, provider=_provider_with_model_id(model_id))
    with pytest.raises(opencode_auth.OpenCodeAuthError, match=f"^{reason}$"):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}
        )


@pytest.mark.parametrize("model_id", ["{env:OMNI_MODEL}", "{file:/etc/x}"])
def test_worker_provider_check_refuses_template_model_id(model_id: str) -> None:
    def block(value: str) -> dict:
        return {"omniroute": {"models": {"router-model": {"id": value}}}}

    runtime_adapters._check_opencode_worker_provider(block("gpt-x"))
    with pytest.raises(runtime_adapters.OpenCodeWorkerConfigError):
        runtime_adapters._check_opencode_worker_provider(block(model_id))


def test_oversized_owner_config_is_refused_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opencode_dir = _owner_config(tmp_path, monkeypatch)
    (opencode_dir / "opencode.json").write_bytes(
        b" " * (opencode_auth.MAX_SOURCE_BYTES + 1)
    )
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_config_unreadable$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_group_writable_owner_config_is_refused_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opencode_dir = _owner_config(
        tmp_path, monkeypatch, provider=_provider_block(apiKey="{env:HOME}")
    )
    (opencode_dir / "opencode.json").chmod(0o666)
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_config_writable_by_others$",
    ):
        opencode_auth.project_opencode_custom_provider("omniroute/router-model")


@pytest.mark.skipif(
    os.name != "nt", reason="only Windows environment names are case-insensitive"
)
def test_env_credential_lookup_is_case_insensitive_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIWORKHUB_TEST_OMNIROUTE_KEY", _SENTINEL)
    _owner_config(
        tmp_path,
        monkeypatch,
        provider=_provider_block(apiKey="{env:aiworkhub_test_omniroute_key}"),
    )

    projected = opencode_auth.project_opencode_custom_provider("omniroute/router-model")

    assert projected["omniroute"]["options"]["apiKey"] == _SENTINEL


def test_missing_named_model_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_config(tmp_path, monkeypatch, provider=_provider_block(apiKey="literal"))
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_not_configured$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/missing-model", host_env={}
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:20128/v1",
        "http://127.0.0.1:20128/v1",
        "http://[::1]:20128/v1",
        "http://127.5.6.7:1/v1",
        "http://127.1:20128/v1",
        "http://2130706433:20128/v1",
        "http://0x7f000001:20128/v1",
        "http://localhost.:20128/v1",
        "http://foo.localhost:20128/v1",
        "http://[::ffff:127.0.0.1]:20128/v1",
        "http://0.0.0.0:20128/v1",
        "http://[::]:20128/v1",
    ],
)
def test_appcontainer_refuses_every_loopback_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    _owner_config(
        tmp_path,
        monkeypatch,
        provider=_provider_block(apiKey="literal", baseURL=base_url),
    )
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_loopback_unreachable_in_appcontainer$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}, appcontainer=True
        )


def test_non_appcontainer_loopback_base_url_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_config(tmp_path, monkeypatch, provider=_provider_block(apiKey="literal"))
    projected = opencode_auth.project_opencode_custom_provider(
        "omniroute/router-model", host_env={}, appcontainer=False
    )
    assert projected["omniroute"]["options"]["baseURL"] == "http://localhost:20128/v1"


def test_appcontainer_allows_a_non_loopback_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_config(
        tmp_path,
        monkeypatch,
        provider=_provider_block(apiKey="literal", baseURL="https://api.example.com/v1"),
    )
    projected = opencode_auth.project_opencode_custom_provider(
        "omniroute/router-model", host_env={}, appcontainer=True
    )
    assert projected["omniroute"]["options"]["baseURL"] == "https://api.example.com/v1"


def test_appcontainer_loopback_check_runs_on_the_resolved_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_config(
        tmp_path,
        monkeypatch,
        provider=_provider_block(apiKey="literal", baseURL="{env:OMNIROUTE_URL}"),
    )
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_loopback_unreachable_in_appcontainer$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model",
            host_env={"OMNIROUTE_URL": "http://localhost:20128/v1"},
            appcontainer=True,
        )


def test_header_env_template_resolves_from_host_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_config(
        tmp_path,
        monkeypatch,
        provider=_provider_block(headers={"Authorization": "Bearer {env:OMNIROUTE_KEY}"}),
    )
    projected = opencode_auth.project_opencode_custom_provider(
        "omniroute/router-model", host_env={"OMNIROUTE_KEY": _SENTINEL}
    )
    assert projected["omniroute"]["options"]["headers"] == {
        "Authorization": f"Bearer {_SENTINEL}"
    }


def test_partial_api_key_env_template_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_config(
        tmp_path, monkeypatch, provider=_provider_block(apiKey="sk-{env:OMNIROUTE_KEY}")
    )
    projected = opencode_auth.project_opencode_custom_provider(
        "omniroute/router-model", host_env={"OMNIROUTE_KEY": _SENTINEL}
    )
    assert projected["omniroute"]["options"]["apiKey"] == f"sk-{_SENTINEL}"


@pytest.mark.parametrize(
    "options",
    [
        {"baseURL": "{file:/etc/omniroute-url}"},
        {"apiKey": "literal", "headers": {"x-key": "{file:/etc/omniroute-key}"}},
    ],
    ids=["base-url", "header"],
)
def test_file_reference_anywhere_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, options: dict
) -> None:
    _owner_config(tmp_path, monkeypatch, provider=_provider_block(**options))
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_credential_unsupported$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}
        )


def test_literal_braces_outside_api_key_are_projected_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = '{"team":"x"}'
    _owner_config(
        tmp_path,
        monkeypatch,
        provider=_provider_block(apiKey="literal", headers={"cf-aig-metadata": metadata}),
    )
    projected = opencode_auth.project_opencode_custom_provider(
        "omniroute/router-model", host_env={}
    )
    assert projected["omniroute"]["options"]["headers"] == {"cf-aig-metadata": metadata}
    runtime_adapters._check_opencode_worker_provider(projected)


def test_model_entry_keeps_only_id_limit_and_capability_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider_block(apiKey="literal")
    provider["omniroute"]["models"]["router-model"] = {
        "id": "upstream-router-model",
        "name": "Router Model",
        "limit": {"context": 128000, "output": 8192, "input": 120000},
        "tool_call": True,
        "options": {"reasoningEffort": "high"},
        "cost": {"input": 1, "output": 2},
    }
    _owner_config(tmp_path, monkeypatch, provider=provider)

    projected = opencode_auth.project_opencode_custom_provider(
        "omniroute/router-model", host_env={}
    )

    assert projected["omniroute"]["models"] == {
        "router-model": {
            "id": "upstream-router-model",
            "limit": {"context": 128000, "output": 8192},
            "tool_call": True,
        }
    }


def test_owner_config_with_utf8_bom_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opencode_dir = _owner_config(tmp_path, monkeypatch)
    (opencode_dir / "opencode.json").write_text(
        json.dumps({"provider": _provider_block(apiKey="literal")}), encoding="utf-8-sig"
    )
    projected = opencode_auth.project_opencode_custom_provider(
        "omniroute/router-model", host_env={}
    )
    assert projected["omniroute"]["options"]["apiKey"] == "literal"


def test_symlinked_owner_config_is_refused_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opencode_dir = _owner_config(tmp_path, monkeypatch)
    real = tmp_path / "real-opencode.json"
    real.write_text(
        json.dumps({"provider": _provider_block(apiKey="literal")}), encoding="utf-8"
    )
    try:
        (opencode_dir / "opencode.json").symlink_to(real)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable here: {exc}")
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_config_unreadable$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}
        )


def test_non_regular_owner_config_is_refused_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opencode_dir = _owner_config(tmp_path, monkeypatch)
    (opencode_dir / "opencode.json").mkdir()
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_config_unreadable$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}
        )


def test_owner_config_read_os_error_is_refused_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_config(tmp_path, monkeypatch, provider=_provider_block(apiKey="literal"))

    def _raise_io_error(*_args: object, **_kwargs: object) -> bytes:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(opencode_auth, "_read_verified_source", _raise_io_error)
    with pytest.raises(
        opencode_auth.OpenCodeAuthError,
        match="^opencode_provider_config_unreadable$",
    ):
        opencode_auth.project_opencode_custom_provider(
            "omniroute/router-model", host_env={}
        )
