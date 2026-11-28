"""NF-2026-01019: systemd/udev/INI config text is indexed file evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import source_graph_languages as sglanguages
from aiworkhub import tool_guard_hook
from aiworkhub.repository_state import bootstrap_repository

CONFIG_SUFFIXES = (
    ".toml", ".ini", ".cfg", ".conf", ".service", ".socket", ".timer", ".mount",
    ".path", ".target", ".rules", ".network", ".netdev", ".link", ".desktop",
)
SERVICE_PATH = "rootfs/etc/systemd/system/x.service"
RULES_PATH = "etc/udev/rules.d/99-x.rules"
CONF_PATH = "etc/modprobe.d/x.conf"
SERVICE_TEXT = "[Unit]\nDescription=Example\n\n[Service]\nExecStart=/usr/bin/example-daemon\n"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "field"
    root.mkdir()
    bootstrap_repository(root, repo_name="field")
    for relative, text in (
        (SERVICE_PATH, SERVICE_TEXT),
        (RULES_PATH, 'ACTION=="add", SUBSYSTEM=="usb", RUN+="/usr/bin/x"\n'),
        (CONF_PATH, "options x debug=1\n"),
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    config = sg.ignore_config_path(root)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps({
            "schema_id": sg.POLICY_SCHEMA_ID,
            "revision": 1,
            "exclude_dirs": [],
            "exclude_globs": [],
            "disabled_languages": [],
        }),
        encoding="utf-8",
    )
    sg.build_index(root, incremental=False)
    return root


@pytest.mark.parametrize("relative", [SERVICE_PATH, RULES_PATH, CONF_PATH])
def test_policy_admits_config_text(repo, relative):
    policy = sg.load_ignore_policy(repo)
    assert sg._policy_exclusion(policy, relative, repo) is None


def test_file_mode_serves_indexed_service_file(repo):
    result = sg.file_query(repo, SERVICE_PATH)
    assert result.get("reason") != "excluded_by_policy", result
    assert result["contexts"], result
    context = result["contexts"][0]
    assert context["file_path"] == SERVICE_PATH
    assert context["found"] is True
    assert "ExecStart=/usr/bin/example-daemon" in context["source_preview"]


def test_registry_keeps_34_families_without_collision():
    assert len(sglanguages.LANGUAGE_SPECS) == 34
    assert len(sglanguages.LANGUAGE_BY_ID) == 34
    all_extensions = [ext for spec in sglanguages.LANGUAGE_SPECS for ext in spec.extensions]
    assert len(all_extensions) == len(set(all_extensions))


def test_toml_family_keeps_id_and_widens_suffixes():
    spec = sglanguages.LANGUAGE_BY_ID["toml"]
    assert spec.label == "TOML / INI / systemd config"
    assert spec.extensions == CONFIG_SUFFIXES
    assert spec.capability == "file_evidence"
    for suffix in CONFIG_SUFFIXES:
        assert sglanguages.LANGUAGE_BY_EXTENSION[suffix] == "toml"
        assert suffix in sglanguages.INDEXED_EXTENSIONS
    assert ".env" not in sglanguages.LANGUAGE_BY_EXTENSION


def test_tool_guard_mirror_matches_registry():
    expected = {spec.id: spec.extensions for spec in sglanguages.LANGUAGE_SPECS}
    assert tool_guard_hook.LANGUAGE_EXTENSIONS == expected
    for suffix in CONFIG_SUFFIXES:
        assert suffix in tool_guard_hook.INDEXED_SUFFIXES
    assert ".env" not in tool_guard_hook.INDEXED_SUFFIXES


def test_claude_hook_copy_is_identical():
    root = Path(__file__).resolve().parents[1]
    mirror = root / ".claude" / "hooks" / "aiworkhub_tool_guard.py"
    if not mirror.is_file():
        pytest.skip("sparse checkout without the .claude hook copy")
    source = root / "src" / "aiworkhub" / "tool_guard_hook.py"
    assert mirror.read_bytes() == source.read_bytes()
