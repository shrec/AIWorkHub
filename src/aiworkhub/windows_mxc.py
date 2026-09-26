"""Read-only discovery of the pinned @microsoft/mxc-sdk 0.7.0 Windows runtime.

Reports typed readiness plus the architecture-correct wxc-exec.exe path. It never
prepares the host, grants ACLs or launches a process; launch and AppContainer
policy stay in aiworkhub.windows_appcontainer. Package metadata may only relocate
wxc-exec.exe inside the package root, through mxcRuntime.binaries.
"""

from __future__ import annotations

import json
import os
import platform
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

PINNED_MXC_SDK_NAME = "@microsoft/mxc-sdk"
PINNED_MXC_SDK_VERSION = "0.7.0"
WXC_EXEC_BINARY_NAME = "wxc-exec.exe"
WXC_HOST_PREP_BINARY_NAME = "wxc-host-prep.exe"

# Measured pinned layout: bin/<x64|arm64>/ holds both wxc-exec.exe and wxc-host-prep.exe.
_ARCHITECTURE_DIRECTORY: Mapping[str, str] = {"win32-x64": "x64", "win32-arm64": "arm64"}
SUPPORTED_MXC_ARCHITECTURES: tuple[str, ...] = tuple(_ARCHITECTURE_DIRECTORY)

_MACHINE_TO_ARCHITECTURE: Mapping[str, str] = {
    "amd64": "win32-x64",
    "x64": "win32-x64",
    "x86_64": "win32-x64",
    "arm64": "win32-arm64",
    "aarch64": "win32-arm64",
}

_RUNTIME_DECLARATION_KEY = "mxcRuntime"
_MAX_METADATA_BYTES = 256 * 1024
_BINARY_PROBE_BYTES = 4096
_MAX_SHOWN_CHARACTERS = 80
_UNSAFE_PATH_CHARACTERS = re.compile(r'[\x00-\x1f:*?"<>|]')

BinaryProbe = Callable[[Path], bytes]


@dataclass(frozen=True)
class MxcReadiness:
    """Typed, fail-closed readiness and runtime-path evidence for MXC."""

    ready: bool
    package_root: Path
    pinned_sdk_name: str
    pinned_sdk_version: str
    sdk_name: str | None
    sdk_version: str | None
    architecture: str | None
    wxc_path: Path | None
    host_prep_path: Path | None
    failures: tuple[str, ...]
    evidence: tuple[str, ...]

    @property
    def runtime_path(self) -> Path | None:
        """The wxc-exec.exe path, or ``None`` when the runtime is not ready."""
        return self.wxc_path


def resolve_mxc_architecture(host_machine: str | None = None) -> tuple[str | None, str]:
    """Map a ``platform.machine()`` value to a packaged platform token."""
    machine = (platform.machine() if host_machine is None else host_machine).strip().lower()
    return _MACHINE_TO_ARCHITECTURE.get(machine), machine


def _read_prefix(path: Path, limit: int = _BINARY_PROBE_BYTES) -> bytes:
    with path.open("rb") as stream:
        return stream.read(limit)


def _layout_path(architecture: str, binary_name: str) -> str:
    return f"bin/{_ARCHITECTURE_DIRECTORY[architecture]}/{binary_name}"


def _safe_relative_path(value: object) -> str | None:
    """Normalise a package-relative path, or return None when it could leave the package."""
    if not isinstance(value, str) or _UNSAFE_PATH_CHARACTERS.search(value):
        return None
    candidate = PureWindowsPath(value)
    if candidate.drive or candidate.root or not candidate.parts:
        return None
    if any(part != part.rstrip(" .") for part in candidate.parts):
        return None
    return "/".join(candidate.parts)


def _locate_file(root: Path, relative: str, label: str) -> tuple[Path | None, str | None]:
    """Return (file, None) when usable, (None, None) when absent, else (None, reason)."""
    try:
        resolved = root.joinpath(*relative.split("/")).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"{label} cannot be resolved: {relative}: {exc}"
    if not resolved.is_relative_to(root):
        return None, f"{label} resolves outside the package root: {relative} -> {resolved}"
    try:
        mode = resolved.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    except OSError as exc:
        return None, f"{label} is not accessible: {resolved}: {exc}"
    if not stat.S_ISREG(mode):
        return None, f"{label} is not a regular file: {resolved}"
    return resolved, None


def _resolve_package_root(package_root: str | os.PathLike[str], failures: list[str]) -> Path:
    raw = os.fspath(package_root)
    if not raw.strip():
        failures.append("package root is not configured")
        return Path(raw)
    try:
        return Path(raw).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        failures.append(f"package root cannot be resolved: {raw}: {exc}")
        return Path(raw)


def _load_package_metadata(
    root: Path, failures: list[str], evidence: list[str]
) -> Mapping[str, Any] | None:
    package_json, problem = _locate_file(root, "package.json", "SDK package metadata")
    if problem is not None:
        failures.append(problem)
        return None
    if package_json is None:
        failures.append(f"missing SDK package metadata: {root / 'package.json'}")
        return None
    evidence.append(f"package_json={package_json}")
    try:
        raw = _read_prefix(package_json, _MAX_METADATA_BYTES + 1)
    except OSError as exc:
        failures.append(f"unable to read SDK package metadata: {package_json}: {exc}")
        return None
    if len(raw) > _MAX_METADATA_BYTES:
        failures.append(
            f"SDK package metadata exceeds {_MAX_METADATA_BYTES} bytes: {package_json}"
        )
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        failures.append(f"SDK package metadata is not valid UTF-8: {package_json}")
        return None
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError) as exc:
        failures.append(f"invalid SDK package metadata: {package_json}: {exc}")
        return None
    if not isinstance(payload, dict):
        failures.append(f"invalid SDK package metadata shape: {package_json}")
        return None
    return payload


def _validate_sdk_identity(
    payload: Mapping[str, Any], failures: list[str], evidence: list[str]
) -> tuple[str | None, str | None]:
    name = payload.get("name")
    version = payload.get("version")
    if not isinstance(name, str) or not name.strip():
        failures.append("missing SDK name in package metadata")
        name = None
    elif name != PINNED_MXC_SDK_NAME:
        found = name[:_MAX_SHOWN_CHARACTERS]
        failures.append(f"SDK name mismatch: expected {PINNED_MXC_SDK_NAME}, found {found}")
    if not isinstance(version, str) or not version.strip():
        failures.append("missing SDK version in package metadata")
        version = None
    elif version != PINNED_MXC_SDK_VERSION:
        found = version[:_MAX_SHOWN_CHARACTERS]
        failures.append(
            f"pinned SDK version mismatch: expected {PINNED_MXC_SDK_VERSION}, found {found}"
        )
    if name is not None:
        evidence.append(f"sdk_name={name[:_MAX_SHOWN_CHARACTERS]}")
    if version is not None:
        evidence.append(f"sdk_version={version[:_MAX_SHOWN_CHARACTERS]}")
    return name, version


def _resolve_host_architecture(
    host_machine: str | None, failures: list[str], evidence: list[str]
) -> str | None:
    architecture, machine = resolve_mxc_architecture(host_machine)
    if architecture is None:
        failures.append(f"unsupported host architecture: {machine}")
        return None
    evidence.append(f"host_machine={machine}")
    evidence.append(f"architecture={architecture}")
    return architecture


def _runtime_relative_path(
    payload: Mapping[str, Any], architecture: str, failures: list[str]
) -> str | None:
    """Return the package-relative wxc-exec.exe path: the measured layout unless relocated."""
    default = _layout_path(architecture, WXC_EXEC_BINARY_NAME)
    section = payload.get(_RUNTIME_DECLARATION_KEY)
    if section is None:
        return default
    binaries = section.get("binaries", {}) if isinstance(section, Mapping) else None
    if not isinstance(binaries, Mapping):
        failures.append(f"invalid {_RUNTIME_DECLARATION_KEY} declaration in SDK package metadata")
        return None
    if architecture not in binaries:
        return default
    declared = binaries[architecture]
    relative = _safe_relative_path(declared)
    if relative is None or relative.rsplit("/", 1)[-1].lower() != WXC_EXEC_BINARY_NAME:
        failures.append(
            f"invalid {WXC_EXEC_BINARY_NAME} path declared for {architecture} in SDK package "
            f"metadata: {repr(declared)[:_MAX_SHOWN_CHARACTERS]}"
        )
        return None
    return relative


def _other_architecture_binary(root: Path, architecture: str) -> tuple[str, Path] | None:
    for other in SUPPORTED_MXC_ARCHITECTURES:
        if other == architecture:
            continue
        found, _ = _locate_file(root, _layout_path(other, WXC_EXEC_BINARY_NAME), "wxc binary")
        if found is not None:
            return other, found
    return None


def _discover_runtime_binary(
    root: Path,
    relative: str,
    architecture: str,
    probe: BinaryProbe,
    failures: list[str],
    evidence: list[str],
) -> Path | None:
    evidence.append(f"runtime_binary_relative={relative}")
    binary, problem = _locate_file(root, relative, "wxc binary")
    if problem is not None:
        failures.append(problem)
        return None
    if binary is None:
        other = _other_architecture_binary(root, architecture)
        if other is None:
            failures.append(
                f"no packaged {WXC_EXEC_BINARY_NAME} for architecture {architecture}: {relative}"
            )
        else:
            failures.append(
                f"{WXC_EXEC_BINARY_NAME} exists only for architecture {other[0]}; "
                f"expected {architecture}: {other[1]}"
            )
        return None
    try:
        prefix = probe(binary)
    except OSError as exc:
        failures.append(f"wxc binary is not accessible: {binary}: {exc}")
        return None
    if not prefix:
        failures.append(f"wxc binary is empty: {binary}")
        return None
    if not prefix.startswith(b"MZ"):
        failures.append(f"wxc binary is not a Windows executable (missing MZ header): {binary}")
        return None
    evidence.append(f"wxc_binary_prefix_bytes={len(prefix)}")
    return binary


def _discover_host_prep(root: Path, architecture: str, evidence: list[str]) -> Path | None:
    relative = _layout_path(architecture, WXC_HOST_PREP_BINARY_NAME)
    host_prep, problem = _locate_file(root, relative, "host preparation binary")
    if host_prep is None:
        evidence.append(f"host_prep_binary={problem or 'absent'}")
    else:
        evidence.append(f"host_prep_binary={host_prep}")
    return host_prep


def probe_mxc_runtime(
    package_root: str | os.PathLike[str],
    host_machine: str | None = None,
    *,
    binary_probe: BinaryProbe | None = None,
) -> MxcReadiness:
    """Probe the pinned MXC SDK runtime without preparing the host or launching anything."""
    probe = binary_probe or _read_prefix
    failures: list[str] = []
    evidence: list[str] = []
    root = _resolve_package_root(package_root, failures)
    payload = None if failures else _load_package_metadata(root, failures, evidence)
    sdk_name: str | None = None
    sdk_version: str | None = None
    architecture: str | None = None
    wxc_path: Path | None = None
    host_prep_path: Path | None = None
    if payload is not None:
        sdk_name, sdk_version = _validate_sdk_identity(payload, failures, evidence)
        if not failures:
            architecture = _resolve_host_architecture(host_machine, failures, evidence)
        if architecture is not None:
            relative = _runtime_relative_path(payload, architecture, failures)
            if relative is not None:
                wxc_path = _discover_runtime_binary(
                    root, relative, architecture, probe, failures, evidence
                )
            host_prep_path = _discover_host_prep(root, architecture, evidence)
    ready = wxc_path is not None and not failures
    return MxcReadiness(
        ready=ready,
        package_root=root,
        pinned_sdk_name=PINNED_MXC_SDK_NAME,
        pinned_sdk_version=PINNED_MXC_SDK_VERSION,
        sdk_name=sdk_name,
        sdk_version=sdk_version,
        architecture=architecture,
        wxc_path=wxc_path if ready else None,
        host_prep_path=host_prep_path,
        failures=tuple(failures),
        evidence=tuple(evidence),
    )
