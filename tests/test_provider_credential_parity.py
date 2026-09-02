"""The two BYOK credential modules must not drift apart on security.

``deepseek_credentials`` and ``glm_credentials`` are the same module written
twice: 165 identical lines, nine shared public symbols, and every one of the
four ``PROVIDER_*_ENV`` constants defined the same way in both. Duplication on
its own is a maintenance cost. What made it a defect is that the copies had
already diverged, and only in one direction:

* ``glm_credentials`` gained ``_ALLOWED_CREDENTIAL_FIELDS``, refusing a
  credential file carrying an unrecognised key. ``deepseek_credentials`` had no
  such constant, so an unknown field was silently ignored.
* ``glm_credentials._validate_endpoint`` restricted ``base_url`` to an
  allow-list of paths and normalised a trailing slash.
  ``deepseek_credentials._validate_endpoint`` did neither: any path on the
  allowed host was accepted, so a credential could point requests at an
  arbitrary endpoint under a trusted hostname.

Both hardenings are now present in both modules. These tests assert the
security surface stays shared, so the next hardening applied to one provider
cannot quietly skip the other -- which is the failure this pair already had.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import deepseek_credentials as deepseek  # noqa: E402
from aiworkhub import glm_credentials as glm  # noqa: E402

_MODULES = (("deepseek", deepseek), ("glm", glm))


@pytest.mark.parametrize("name,module", _MODULES)
def test_every_provider_refuses_unrecognised_credential_fields(name, module):
    assert hasattr(module, "_ALLOWED_CREDENTIAL_FIELDS"), (
        f"{name} has no allowed-field allow-list; an unknown key would be ignored"
    )
    assert module._ALLOWED_CREDENTIAL_FIELDS == frozenset(
        {"provider", "provider_type", "base_url", "api_key"}
    )


def test_the_allow_lists_are_the_same_surface():
    assert (
        deepseek._ALLOWED_CREDENTIAL_FIELDS == glm._ALLOWED_CREDENTIAL_FIELDS
    ), "a field allowed for one provider but not the other is drift, not policy"


@pytest.mark.parametrize("name,module", _MODULES)
def test_every_provider_rejects_a_non_https_endpoint(name, module):
    with pytest.raises(module.CredentialError) as exc:
        module._validate_endpoint("http://example.com/v1")
    assert "non_https" in str(exc.value)


@pytest.mark.parametrize("name,module", _MODULES)
def test_every_provider_rejects_a_foreign_host(name, module):
    with pytest.raises(module.CredentialError):
        module._validate_endpoint("https://evil.example.com/v1")


@pytest.mark.parametrize(
    "name,module,hostile",
    [
        ("deepseek", deepseek, "https://api.deepseek.com/evil/path"),
        ("glm", glm, "https://open.bigmodel.cn/evil/path"),
    ],
)
def test_every_provider_rejects_an_unexpected_base_url_path(name, module, hostile):
    """An allowed host is not enough; the path has to be one this provider serves."""
    with pytest.raises(module.CredentialError) as exc:
        module._validate_endpoint(hostile)
    assert "unsupported_base_url_path" in str(exc.value)


@pytest.mark.parametrize(
    "name,module,canonical",
    [
        ("deepseek", deepseek, "https://api.deepseek.com/v1"),
        ("glm", glm, "https://open.bigmodel.cn/api/paas/v4"),
    ],
)
def test_every_provider_normalises_a_trailing_slash(name, module, canonical):
    """Otherwise one endpoint is recorded as two different strings."""
    assert module._validate_endpoint(canonical + "/") == canonical
    assert module._validate_endpoint(canonical) == canonical


@pytest.mark.parametrize(
    "name,module,canonical",
    [
        ("deepseek", deepseek, "https://api.deepseek.com/v1"),
        ("glm", glm, "https://open.bigmodel.cn/api/paas/v4"),
    ],
)
def test_every_provider_accepts_its_own_canonical_endpoint(name, module, canonical):
    assert module._validate_endpoint(canonical) == canonical


def test_the_shared_public_surface_stays_shared():
    """Nine symbols exist in both. A symbol dropped from one is drift."""
    shared = {
        "CredentialError",
        "CREDENTIAL_PATH_ENV",
        "MAX_CREDENTIAL_FILE_BYTES",
        "credential_path",
        "_validate_endpoint",
        "_within",
        "load_credential",
        "bootstrap_credential",
        "credential_status",
    }
    for name, module in _MODULES:
        missing = sorted(s for s in shared if not hasattr(module, s))
        assert not missing, f"{name} is missing shared credential surface: {missing}"
