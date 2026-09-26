"""Contract tests for the read-only Windows MXC runtime discovery primitive."""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest

from aiworkhub import windows_mxc
from aiworkhub.windows_mxc import (
    PINNED_MXC_SDK_NAME,
    PINNED_MXC_SDK_VERSION,
    MxcReadiness,
    probe_mxc_runtime,
    resolve_mxc_architecture,
)

_PE_STUB = b"MZ" + bytes(62)
_RUNTIME = "wxc-exec.exe"
_HOST_PREP = "wxc-host-prep.exe"

_UNSAFE_DECLARATIONS = [
    pytest.param("../outside/wxc-exec.exe", id="parent-traversal"),
    pytest.param("bin/../../outside/wxc-exec.exe", id="embedded-traversal"),
    pytest.param("./../outside/wxc-exec.exe", id="dot-prefixed-traversal"),
    pytest.param("..\\outside\\wxc-exec.exe", id="backslash-traversal"),
    pytest.param("/outside/wxc-exec.exe", id="posix-absolute"),
    pytest.param("\\outside\\wxc-exec.exe", id="rooted-backslash"),
    pytest.param("C:\\outside\\wxc-exec.exe", id="drive-absolute"),
    pytest.param("C:outside/wxc-exec.exe", id="drive-relative"),
    pytest.param("\\\\server\\share\\wxc-exec.exe", id="unc"),
    pytest.param("bin/x64/wxc-exec.exe:stream", id="alternate-data-stream"),
    pytest.param("bin/x64/wxc-exec.exe.", id="trailing-dot"),
    pytest.param("bin/x64/wxc-exec.exe ", id="trailing-space"),
    pytest.param("bin/x64/wxc-exec.exe\x00", id="nul-byte"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param(".", id="current-directory"),
    pytest.param("..", id="parent-directory"),
    pytest.param("bin/x64/", id="directory-only"),
    pytest.param("bin/x64/wxc-host-prep.exe", id="host-prep-as-runtime"),
    pytest.param("bin/x64/wxc.exe", id="legacy-name"),
    pytest.param("package.json", id="metadata-file"),
    pytest.param(123, id="integer"),
    pytest.param(None, id="null"),
    pytest.param(["bin/x64/wxc-exec.exe"], id="list"),
    pytest.param({"path": "bin/x64/wxc-exec.exe"}, id="object"),
]

_READ_ONLY_IMPORTS = {
    "__future__",
    "collections",
    "dataclasses",
    "json",
    "os",
    "pathlib",
    "platform",
    "re",
    "stat",
    "typing",
}
_MUTATING_ATTRIBUTES = {
    "Popen",
    "chmod",
    "hardlink_to",
    "mkdir",
    "rename",
    "rmdir",
    "startfile",
    "symlink_to",
    "system",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
}


def _write_metadata(root: Path, **fields: object) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    payload = {"name": PINNED_MXC_SDK_NAME, "version": PINNED_MXC_SDK_VERSION, **fields}
    package_json = root / "package.json"
    package_json.write_text(json.dumps(payload), encoding="utf-8")
    return package_json


def _write_binary(
    root: Path, directory: str, name: str = _RUNTIME, content: bytes = _PE_STUB
) -> Path:
    binary = root / "bin" / directory / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(content)
    return binary


def _recording_probe() -> tuple[list[Path], windows_mxc.BinaryProbe]:
    probed: list[Path] = []

    def probe(path: Path) -> bytes:
        probed.append(path)
        return _PE_STUB

    return probed, probe


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")


def _tree(root: Path) -> list[tuple[str, bytes | None]]:
    return [
        (path.relative_to(root).as_posix(), path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    ]


def test_pinned_contract_uses_the_measured_sdk_names() -> None:
    assert PINNED_MXC_SDK_NAME == "@microsoft/mxc-sdk"
    assert PINNED_MXC_SDK_VERSION == "0.7.0"
    assert windows_mxc.WXC_EXEC_BINARY_NAME == _RUNTIME
    assert windows_mxc.WXC_HOST_PREP_BINARY_NAME == _HOST_PREP
    assert windows_mxc.SUPPORTED_MXC_ARCHITECTURES == ("win32-x64", "win32-arm64")


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("AMD64", "win32-x64"),
        ("amd64", "win32-x64"),
        ("x86_64", "win32-x64"),
        ("x64", "win32-x64"),
        ("ARM64", "win32-arm64"),
        ("arm64", "win32-arm64"),
        (" aarch64 ", "win32-arm64"),
        ("x86", None),
        ("mips64", None),
        ("", None),
    ],
)
def test_host_machine_architecture_mapping(machine: str, expected: str | None) -> None:
    assert resolve_mxc_architecture(machine)[0] == expected


def test_default_architecture_comes_from_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_mxc.platform, "machine", lambda: "ARM64")

    assert resolve_mxc_architecture() == ("win32-arm64", "arm64")


def test_missing_sdk_metadata_fails_closed(tmp_path: Path) -> None:
    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert isinstance(result, MxcReadiness)
    assert result.ready is False
    assert result.wxc_path is None
    assert result.runtime_path is None
    assert result.host_prep_path is None
    assert result.sdk_name is None
    assert result.sdk_version is None
    assert result.pinned_sdk_version == PINNED_MXC_SDK_VERSION
    assert any("missing SDK package metadata" in item for item in result.failures)


def test_runtime_without_sdk_metadata_is_not_ready(tmp_path: Path) -> None:
    _write_binary(tmp_path, "x64")

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.wxc_path is None
    assert any("missing SDK package metadata" in item for item in result.failures)


def test_missing_package_root_fails_closed(tmp_path: Path) -> None:
    result = probe_mxc_runtime(tmp_path / "absent", host_machine="amd64")

    assert result.ready is False
    assert result.wxc_path is None
    assert any("missing SDK package metadata" in item for item in result.failures)


@pytest.mark.parametrize("root", ["", "   "])
def test_unconfigured_package_root_fails_closed(root: str) -> None:
    result = probe_mxc_runtime(root, host_machine="amd64")

    assert result.ready is False
    assert result.wxc_path is None
    assert any("package root is not configured" in item for item in result.failures)


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        pytest.param(b"{not valid json", "invalid SDK package metadata", id="invalid-json"),
        pytest.param(b"", "invalid SDK package metadata", id="empty-file"),
        pytest.param(b"\xef\xbb\xbf{}", "invalid SDK package metadata", id="utf8-bom"),
        pytest.param(b'{"name": "\xff\xfe"}', "not valid UTF-8", id="malformed-utf8"),
        pytest.param(b"[]", "invalid SDK package metadata shape", id="array"),
        pytest.param(b'"text"', "invalid SDK package metadata shape", id="string"),
        pytest.param(b"null", "invalid SDK package metadata shape", id="null"),
    ],
)
def test_malformed_sdk_metadata_fails_closed(tmp_path: Path, raw: bytes, fragment: str) -> None:
    (tmp_path / "package.json").write_bytes(raw)
    _write_binary(tmp_path, "x64")

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.wxc_path is None
    assert result.sdk_name is None
    assert any(fragment in item for item in result.failures)


def test_oversized_sdk_metadata_fails_closed(tmp_path: Path) -> None:
    payload = {
        "name": PINNED_MXC_SDK_NAME,
        "version": PINNED_MXC_SDK_VERSION,
        "padding": "x" * (256 * 1024),
    }
    (tmp_path / "package.json").write_text(json.dumps(payload), encoding="utf-8")
    _write_binary(tmp_path, "x64")

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.sdk_name is None
    assert any("SDK package metadata exceeds" in item for item in result.failures)


def test_inaccessible_sdk_metadata_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_metadata(tmp_path)
    _write_binary(tmp_path, "x64")
    original_open = Path.open

    def _deny_metadata(self: Path, *args: object, **kwargs: object):
        if self.name == "package.json":
            raise PermissionError("denied: package.json")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _deny_metadata)

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.sdk_name is None
    assert any("unable to read SDK package metadata" in item for item in result.failures)


def test_directory_in_place_of_sdk_metadata_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "package.json").mkdir()

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.sdk_name is None
    assert any("SDK package metadata is not a regular file" in item for item in result.failures)


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        pytest.param({"name": None}, "missing SDK name", id="null-name"),
        pytest.param({"name": ["@microsoft/mxc-sdk"]}, "missing SDK name", id="non-string-name"),
        pytest.param({"name": "not-the-mxc-sdk"}, "SDK name mismatch", id="wrong-name"),
        pytest.param({"name": "@microsoft/mxc-sdk "}, "SDK name mismatch", id="padded-name"),
        pytest.param({"version": None}, "missing SDK version", id="null-version"),
        pytest.param({"version": 7}, "missing SDK version", id="non-string-version"),
        pytest.param({"version": "0.6.0"}, "pinned SDK version mismatch", id="older-version"),
        pytest.param({"version": "0.7.1"}, "pinned SDK version mismatch", id="newer-version"),
        pytest.param({"version": "0.7.0-beta.1"}, "pinned SDK version mismatch", id="prerelease"),
        pytest.param({"version": " 0.7.0"}, "pinned SDK version mismatch", id="padded-version"),
        pytest.param({"version": "^0.7.0"}, "pinned SDK version mismatch", id="range"),
    ],
)
def test_sdk_identity_must_match_the_pin_exactly(
    tmp_path: Path, fields: dict[str, object], fragment: str
) -> None:
    _write_metadata(tmp_path, **fields)
    _write_binary(tmp_path, "x64")

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.wxc_path is None
    assert result.architecture is None
    assert any(fragment in item for item in result.failures)


def test_mismatched_identity_is_reported_as_found(tmp_path: Path) -> None:
    _write_metadata(tmp_path, name="not-the-mxc-sdk", version="0.6.0")

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.sdk_name == "not-the-mxc-sdk"
    assert result.sdk_version == "0.6.0"
    assert result.pinned_sdk_name == PINNED_MXC_SDK_NAME
    assert result.pinned_sdk_version == PINNED_MXC_SDK_VERSION


def test_unsupported_host_architecture_fails_closed(tmp_path: Path) -> None:
    _write_metadata(tmp_path)
    _write_binary(tmp_path, "x64")

    result = probe_mxc_runtime(tmp_path, host_machine="mips64")

    assert result.ready is False
    assert result.architecture is None
    assert result.wxc_path is None
    assert any("unsupported host architecture: mips64" in item for item in result.failures)


def test_missing_wxc_exec_fails_closed(tmp_path: Path) -> None:
    _write_metadata(tmp_path)

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.wxc_path is None
    assert result.architecture == "win32-x64"
    assert any(
        "no packaged wxc-exec.exe for architecture win32-x64: bin/x64/wxc-exec.exe" in item
        for item in result.failures
    )


@pytest.mark.parametrize(
    ("machine", "present", "expected", "other"),
    [
        ("amd64", "arm64", "win32-x64", "win32-arm64"),
        ("aarch64", "x64", "win32-arm64", "win32-x64"),
    ],
)
def test_wrong_architecture_wxc_exec_fails_closed(
    tmp_path: Path, machine: str, present: str, expected: str, other: str
) -> None:
    _write_metadata(tmp_path)
    _write_binary(tmp_path, present)

    result = probe_mxc_runtime(tmp_path, host_machine=machine)

    assert result.ready is False
    assert result.wxc_path is None
    assert result.architecture == expected
    assert any(
        f"exists only for architecture {other}" in item and f"expected {expected}" in item
        for item in result.failures
    )


@pytest.mark.parametrize(
    "relative",
    [
        "bin/win32-x64/wxc-exec.exe",
        "bin/win32-x64/wxc.exe",
        "platform/win32-x64/wxc.exe",
        "binaries/win32-x64/wxc.exe",
        "bin/x64/wxc.exe",
        "wxc-exec.exe",
    ],
)
def test_guessed_layouts_are_not_a_runtime(tmp_path: Path, relative: str) -> None:
    _write_metadata(tmp_path)
    guessed = tmp_path / relative
    guessed.parent.mkdir(parents=True, exist_ok=True)
    guessed.write_bytes(_PE_STUB)

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.wxc_path is None
    assert any("no packaged wxc-exec.exe" in item for item in result.failures)


def test_host_prep_is_never_accepted_as_the_runtime(tmp_path: Path) -> None:
    _write_metadata(tmp_path)
    host_prep = _write_binary(tmp_path, "x64", name=_HOST_PREP)

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.wxc_path is None
    assert result.host_prep_path == host_prep.resolve()
    assert any("no packaged wxc-exec.exe" in item for item in result.failures)


def test_inaccessible_wxc_exec_fails_closed(tmp_path: Path) -> None:
    _write_metadata(tmp_path)
    binary = _write_binary(tmp_path, "x64")

    def _denied(path: Path) -> bytes:
        raise PermissionError(f"denied: {path}")

    result = probe_mxc_runtime(tmp_path, host_machine="amd64", binary_probe=_denied)

    assert result.ready is False
    assert result.wxc_path is None
    assert any(
        "wxc binary is not accessible" in item and str(binary.resolve()) in item
        for item in result.failures
    )


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        pytest.param(b"", "wxc binary is empty", id="empty"),
        pytest.param(b"M", "missing MZ header", id="truncated-header"),
        pytest.param(b"PK\x03\x04", "missing MZ header", id="zip-archive"),
        pytest.param(b"version https://git-lfs.github.com/spec/v1\n", "missing MZ header", id="lfs"),
    ],
)
def test_unusable_wxc_exec_content_fails_closed(
    tmp_path: Path, content: bytes, fragment: str
) -> None:
    _write_metadata(tmp_path)
    _write_binary(tmp_path, "x64", content=content)

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.wxc_path is None
    assert any(fragment in item for item in result.failures)


def test_directory_in_place_of_wxc_exec_fails_closed(tmp_path: Path) -> None:
    _write_metadata(tmp_path)
    (tmp_path / "bin" / "x64" / _RUNTIME).mkdir(parents=True)

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.wxc_path is None
    assert any("wxc binary is not a regular file" in item for item in result.failures)


def test_valid_pinned_runtime_is_ready_for_x64(tmp_path: Path) -> None:
    _write_metadata(tmp_path)
    runtime = _write_binary(tmp_path, "x64")
    host_prep = _write_binary(tmp_path, "x64", name=_HOST_PREP)

    result = probe_mxc_runtime(tmp_path, host_machine="AMD64")

    assert result.ready is True
    assert result.pinned_sdk_name == PINNED_MXC_SDK_NAME
    assert result.pinned_sdk_version == "0.7.0"
    assert result.sdk_name == PINNED_MXC_SDK_NAME
    assert result.sdk_version == PINNED_MXC_SDK_VERSION
    assert result.architecture == "win32-x64"
    assert result.package_root == tmp_path.resolve()
    assert result.wxc_path == runtime.resolve()
    assert result.runtime_path == result.wxc_path
    assert result.host_prep_path == host_prep.resolve()
    assert result.failures == ()
    assert any(item.startswith("package_json=") for item in result.evidence)
    assert f"sdk_version={PINNED_MXC_SDK_VERSION}" in result.evidence
    assert "architecture=win32-x64" in result.evidence
    assert "runtime_binary_relative=bin/x64/wxc-exec.exe" in result.evidence
    assert f"wxc_binary_prefix_bytes={len(_PE_STUB)}" in result.evidence
    assert f"host_prep_binary={host_prep.resolve()}" in result.evidence


def test_valid_pinned_runtime_is_ready_for_arm64(tmp_path: Path) -> None:
    _write_metadata(tmp_path)
    runtime = _write_binary(tmp_path, "arm64")
    host_prep = _write_binary(tmp_path, "arm64", name=_HOST_PREP)

    result = probe_mxc_runtime(tmp_path, host_machine="ARM64")

    assert result.ready is True
    assert result.architecture == "win32-arm64"
    assert result.wxc_path == runtime.resolve()
    assert result.host_prep_path == host_prep.resolve()
    assert result.failures == ()
    assert "runtime_binary_relative=bin/arm64/wxc-exec.exe" in result.evidence


def test_runtime_is_ready_without_a_host_prep_binary(tmp_path: Path) -> None:
    _write_metadata(tmp_path)
    runtime = _write_binary(tmp_path, "x64")

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is True
    assert result.wxc_path == runtime.resolve()
    assert result.host_prep_path is None
    assert "host_prep_binary=absent" in result.evidence


@pytest.mark.parametrize(
    ("machine", "directory", "token"),
    [
        ("amd64", "x64", "win32-x64"),
        ("x86_64", "x64", "win32-x64"),
        ("arm64", "arm64", "win32-arm64"),
        ("aarch64", "arm64", "win32-arm64"),
    ],
)
def test_host_selects_its_own_architecture_directory(
    tmp_path: Path, machine: str, directory: str, token: str
) -> None:
    _write_metadata(tmp_path)
    expected = _write_binary(tmp_path, directory)
    _write_binary(tmp_path, "arm64" if directory == "x64" else "x64")

    result = probe_mxc_runtime(tmp_path, host_machine=machine)

    assert result.ready is True
    assert result.architecture == token
    assert result.wxc_path == expected.resolve()


def test_declared_runtime_path_inside_the_package_is_honored(tmp_path: Path) -> None:
    _write_metadata(tmp_path, mxcRuntime={"binaries": {"win32-x64": "vendor/mxc/x64/wxc-exec.exe"}})
    declared = tmp_path / "vendor" / "mxc" / "x64" / _RUNTIME
    declared.parent.mkdir(parents=True)
    declared.write_bytes(_PE_STUB)

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is True
    assert result.wxc_path == declared.resolve()
    assert "runtime_binary_relative=vendor/mxc/x64/wxc-exec.exe" in result.evidence


def test_declared_runtime_path_does_not_fall_back_to_the_default(tmp_path: Path) -> None:
    _write_metadata(tmp_path, mxcRuntime={"binaries": {"win32-x64": "vendor/x64/wxc-exec.exe"}})
    _write_binary(tmp_path, "x64")

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.wxc_path is None
    assert any(
        "no packaged wxc-exec.exe for architecture win32-x64: vendor/x64/wxc-exec.exe" in item
        for item in result.failures
    )


def test_declaration_for_another_architecture_is_ignored(tmp_path: Path) -> None:
    _write_metadata(tmp_path, mxcRuntime={"binaries": {"win32-arm64": "../escape/wxc-exec.exe"}})
    runtime = _write_binary(tmp_path, "x64")

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is True
    assert result.wxc_path == runtime.resolve()


@pytest.mark.parametrize(
    "section",
    [
        pytest.param("bin/x64/wxc-exec.exe", id="string"),
        pytest.param(["bin/x64/wxc-exec.exe"], id="list"),
        pytest.param(False, id="boolean"),
        pytest.param({"binaries": None}, id="null-binaries"),
        pytest.param({"binaries": "bin/x64/wxc-exec.exe"}, id="string-binaries"),
        pytest.param({"binaries": ["bin/x64/wxc-exec.exe"]}, id="list-binaries"),
    ],
)
def test_malformed_runtime_declaration_fails_closed(tmp_path: Path, section: object) -> None:
    _write_metadata(tmp_path, mxcRuntime=section)
    _write_binary(tmp_path, "x64")

    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert result.ready is False
    assert result.wxc_path is None
    assert any("invalid mxcRuntime declaration" in item for item in result.failures)


@pytest.mark.parametrize("declared", _UNSAFE_DECLARATIONS)
def test_unsafe_declared_runtime_paths_are_rejected_before_probing(
    tmp_path: Path, declared: object
) -> None:
    package = tmp_path / "package"
    _write_metadata(package, mxcRuntime={"binaries": {"win32-x64": declared}})
    _write_binary(package, "x64")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / _RUNTIME).write_bytes(_PE_STUB)
    probed, probe = _recording_probe()

    result = probe_mxc_runtime(package, host_machine="amd64", binary_probe=probe)

    assert result.ready is False
    assert result.wxc_path is None
    assert probed == []
    assert any(
        "invalid wxc-exec.exe path declared for win32-x64" in item for item in result.failures
    )


@pytest.mark.parametrize(
    "relative", ["../outside/wxc-exec.exe", "../package-evil/wxc-exec.exe"]
)
def test_containment_check_holds_without_the_syntactic_filter(
    tmp_path: Path, relative: str
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    for sibling in ("outside", "package-evil"):
        (tmp_path / sibling).mkdir()
        (tmp_path / sibling / _RUNTIME).write_bytes(_PE_STUB)

    located, problem = windows_mxc._locate_file(package.resolve(), relative, "wxc binary")

    assert located is None
    assert problem is not None
    assert "resolves outside the package root" in problem


def test_runtime_symlinked_outside_the_package_is_rejected(tmp_path: Path) -> None:
    package = tmp_path / "package"
    _write_metadata(package)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / _RUNTIME).write_bytes(_PE_STUB)
    (package / "bin").mkdir()
    _symlink_or_skip(package / "bin" / "x64", outside)
    probed, probe = _recording_probe()

    result = probe_mxc_runtime(package, host_machine="amd64", binary_probe=probe)

    assert result.ready is False
    assert result.wxc_path is None
    assert probed == []
    assert any(
        "wxc binary resolves outside the package root" in item for item in result.failures
    )


def test_sdk_metadata_symlinked_outside_the_package_is_rejected(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    external = _write_metadata(tmp_path / "external")
    _symlink_or_skip(package / "package.json", external)
    _write_binary(package, "x64")

    result = probe_mxc_runtime(package, host_machine="amd64")

    assert result.ready is False
    assert result.sdk_name is None
    assert any(
        "SDK package metadata resolves outside the package root" in item
        for item in result.failures
    )


@pytest.mark.parametrize("with_runtime", [True, False])
def test_probe_never_writes_to_the_package(tmp_path: Path, with_runtime: bool) -> None:
    _write_metadata(tmp_path)
    if with_runtime:
        _write_binary(tmp_path, "x64")
        _write_binary(tmp_path, "x64", name=_HOST_PREP)
    before = _tree(tmp_path)

    probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert _tree(tmp_path) == before


def test_readiness_is_an_immutable_typed_record(tmp_path: Path) -> None:
    result = probe_mxc_runtime(tmp_path, host_machine="amd64")

    assert dataclasses.is_dataclass(result)
    assert isinstance(result.failures, tuple)
    assert isinstance(result.evidence, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ready = True  # type: ignore[misc]


def test_module_imports_only_read_only_discovery_libraries() -> None:
    tree = ast.parse(Path(windows_mxc.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    attributes: set[str] = set()
    os_attributes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or "").split(".")[0])
        elif isinstance(node, ast.Attribute):
            attributes.add(node.attr)
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                os_attributes.add(node.attr)

    assert imported <= _READ_ONLY_IMPORTS
    assert os_attributes <= {"PathLike", "fspath"}
    assert not attributes & _MUTATING_ATTRIBUTES
