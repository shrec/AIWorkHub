"""Candidate project XML, read as data, authored into one synthetic restore.

NF-2026-01366 follow-up.  A card that adds a project or a ``PackageReference``
can never be seeded from the canonical tree: the packages it needs are declared
only in the candidate worktree.  ``dotnet restore`` runs project targets, so the
candidate project files must never be handed to the host CLI either.  This
module therefore reads the project files reachable from a named candidate
target, plus every ``Directory.Packages.props`` and ``Directory.Build.props``
above them, as XML *data* -- ``PackageReference`` identities and versions,
central ``PackageVersion`` and repo-wide ``GlobalPackageReference`` rows, and
the declared ``TargetFramework``/``TargetFrameworks`` -- and authors
``AIWorkHubRestoreSeed.csproj`` under the request home from that data alone.
The generated file is the only project the host restore is ever given.

A seed is strict for a target the canonical tree lacks and supplementary for
one it carries, where the card merely adds a project or a ``PackageReference``:
the canonical restore has already seeded every canonical declaration there, so
a row needing MSBuild evaluation is dropped rather than refused, and only the
bounds on the read itself still refuse.

Every read is bounded and every refusal is named: a ``DOCTYPE`` or ``ENTITY``
declaration, an oversized file, a closure wider than the file bound, a parse
error, an MSBuild expression where a literal version or target framework was
required, and an unresolved central version each raise their own reason.  The
lane surfaces it as
``validation_executable_unavailable:dotnet_prerestore:<reason>``, so a host
seeding limit is an environment block and never a candidate validation failure.
No decision here reads the host CLI's stdout or stderr.
"""

from __future__ import annotations

import os
import re
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from xml.etree import ElementTree

SEED_DIRNAME = "dotnet-prerestore-seed"
SEED_PROJECT_NAME = "AIWorkHubRestoreSeed.csproj"

_MAX_PROJECT_FILES = 64
_MAX_PROJECT_BYTES = 512 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024
_CENTRAL_PACKAGES_NAME = "Directory.Packages.props"
# Read as DATA by the same upward walk, never imported: this is where a repo
# that sets one TargetFramework or one repo-wide analyzer declares it.
_DIRECTORY_BUILD_NAME = "Directory.Build.props"
_SOLUTION_SUFFIXES = (".sln", ".slnx")
_PROJECT_SUFFIXES = (".csproj", ".fsproj", ".vbproj", ".proj")
# ``<!DOCTYPE`` and ``<!ENTITY`` are refused outright rather than parsed: an
# entity declaration is the one XML construct that can make a bounded read
# expand without bound, or reach a file this module never opened.
_UNSAFE_XML = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
# A NuGet identity, a literal version (ranges included) and a target framework
# moniker.  ``$``, ``@`` and ``%`` are absent from every class, so an MSBuild
# property or item expression can never match -- and so are ``<``, ``>``, ``&``
# and both quotes, which is what makes interpolating a matched value into the
# generated project safe without escaping.
_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_PACKAGE_VERSION = re.compile(r"^[0-9A-Za-z.+*,()\[\]-]{1,64}$")
_TARGET_FRAMEWORK = re.compile(r"^[A-Za-z][A-Za-z0-9.-]{0,31}$")
# ``Project("{GUID}") = "Name", "relative\path.csproj", "{GUID}"``
_SLN_PROJECT_ROW = re.compile(
    r'^Project\("\{[^"}]*\}"\)\s*=\s*"[^"]*"\s*,\s*"([^"]+)"', re.MULTILINE
)


class _SeedRefusal(Exception):
    """A named reason the synthetic restore project cannot be authored."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Behind a SUCCESSFUL canonical restore these two reasons describe the candidate
# tree rather than a host seeding limit: a target kind this module reads only as
# a name (``.slnf``), and a project a candidate solution still lists after the
# card deleted it.  The container build decides both, as validation_failed, so a
# supplementary seed drops them instead of blocking the lane.  A strict seed
# still refuses: nothing else will have seeded that target at all.
_SUPPLEMENTARY_DROPPED_REASONS = frozenset(
    {"candidate_target_kind_unsupported", "candidate_project_missing"}
)


def _seed_outcome(reason: str, *, supplementary: bool) -> tuple[None, str | None]:
    """One named refusal, or nothing left to seed behind a canonical restore."""
    if supplementary and reason in _SUPPLEMENTARY_DROPPED_REASONS:
        return None, None
    return None, reason


def _is_within(path: str | Path, root: str | Path) -> bool:
    """True when ``path`` is ``root`` or below it, without resolving links.

    Owned here rather than in the lane module because the lane imports this
    one: the canonical-path decisions there and every candidate read below are
    gated by this single predicate.
    """
    child = os.path.normcase(os.path.abspath(str(path)))
    parent = os.path.normcase(os.path.abspath(str(root)))
    return child == parent or child.startswith(parent + os.sep)


def _local(tag: object) -> str:
    """An element name without its XML namespace."""
    return str(tag).rsplit("}", 1)[-1]


class _BoundedXml:
    """Bounded, DOCTYPE-free reads: the only way candidate bytes are read."""

    def __init__(self) -> None:
        self.files = 0
        self.remaining_bytes = _MAX_TOTAL_BYTES

    def text(self, path: Path) -> str:
        self.files += 1
        if self.files > _MAX_PROJECT_FILES:
            raise _SeedRefusal("project_file_limit")
        try:
            size = path.stat().st_size
        except OSError:
            raise _SeedRefusal("candidate_project_missing") from None
        if size > _MAX_PROJECT_BYTES or size > self.remaining_bytes:
            raise _SeedRefusal("project_xml_too_large")
        self.remaining_bytes -= size
        try:
            return path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            raise _SeedRefusal("project_xml_unreadable") from None

    def root(self, path: Path) -> ElementTree.Element:
        body = self.text(path)
        if _UNSAFE_XML.search(body):
            raise _SeedRefusal("project_xml_unsafe")
        try:
            return ElementTree.fromstring(body)
        except ElementTree.ParseError:
            raise _SeedRefusal("project_xml_parse_error") from None


def _solution_project_entries(solution: Path, reader: _BoundedXml) -> list[Path]:
    """The project paths a ``.sln`` or ``.slnx`` names, as data.

    A ``.slnx`` carries ``<Project Path="..."/>`` entries, possibly nested in
    ``<Folder>`` elements; a ``.sln`` carries the classic ``Project(...)`` rows.
    Solution folders and nested solutions are dropped by the suffix filter.
    """
    if solution.suffix.lower() == ".slnx":
        declared = [
            element.get("Path", "")
            for element in reader.root(solution).iter()
            if _local(element.tag) == "Project"
        ]
    else:
        body = reader.text(solution)
        if _UNSAFE_XML.search(body):
            raise _SeedRefusal("project_xml_unsafe")
        declared = _SLN_PROJECT_ROW.findall(body)
    entries: list[Path] = []
    for value in declared:
        relative = value.replace("\\", "/").strip()
        if relative.lower().endswith(_PROJECT_SUFFIXES):
            entries.append(solution.parent / relative)
    return entries


def _candidate_project_closure(
    entries: Sequence[Path], worktree: Path, reader: _BoundedXml
) -> list[tuple[Path, ElementTree.Element]]:
    """Every candidate project reachable from ``entries``, in breadth order.

    The ``ProjectReference`` closure stays inside ``worktree``: a reference
    that escapes it, or names a file that is not there, is a named refusal
    rather than a widened read.
    """
    pending = deque(Path(os.path.abspath(str(entry))) for entry in entries)
    seen: set[str] = set()
    projects: list[tuple[Path, ElementTree.Element]] = []
    while pending:
        current = pending.popleft()
        key = os.path.normcase(str(current))
        if key in seen:
            continue
        seen.add(key)
        if not _is_within(current, worktree):
            raise _SeedRefusal("candidate_project_outside_worktree")
        if not current.is_file():
            raise _SeedRefusal("candidate_project_missing")
        root = reader.root(current)
        projects.append((current, root))
        for element in root.iter():
            if _local(element.tag) != "ProjectReference":
                continue
            include = (element.get("Include") or "").replace("\\", "/").strip()
            if include:
                pending.append(
                    Path(os.path.abspath(str(current.parent / include)))
                )
    return projects


def _declared_version(element: ElementTree.Element) -> str | None:
    """A package row's declared version, from the attribute or the child."""
    declared = element.get("Version") or element.get("VersionOverride")
    if declared is None:
        for child in element:
            if _local(child.tag) in ("Version", "VersionOverride"):
                declared = child.text
                break
    declared = (declared or "").strip()
    return declared or None


def _declared_data(
    documents: Sequence[tuple[Path, ElementTree.Element]],
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """The ``(identity, version-or-None)`` rows and the declared frameworks.

    ``documents`` is the union of the project closure and every candidate
    ``Directory.Build.props`` above it.  A repository that declares
    ``<TargetFramework>`` or a repo-wide analyzer ``PackageReference`` once,
    there, has declared it for every project beneath it; reading the project
    files alone made such a project look as though it named no framework.

    Conditions are deliberately ignored: a seed may be a superset of what one
    configuration restores, and evaluating a ``Condition`` would mean
    evaluating candidate MSBuild.
    """
    packages: list[tuple[str, str | None]] = []
    frameworks: list[str] = []
    for _path, root in documents:
        for element in root.iter():
            name = _local(element.tag)
            if name == "PackageReference":
                identity = (
                    element.get("Include") or element.get("Update") or ""
                ).strip()
                if identity:
                    packages.append((identity, _declared_version(element)))
            elif name == "TargetFramework":
                frameworks.append((element.text or "").strip())
            elif name == "TargetFrameworks":
                frameworks.extend(
                    part.strip() for part in (element.text or "").split(";")
                )
    return packages, [value for value in frameworks if value]


def _ancestor_files(
    projects: Sequence[tuple[Path, ElementTree.Element]], worktree: Path, name: str
) -> list[Path]:
    """Every ``name`` above a project in the closure, shallowest first.

    ``Directory.Packages.props`` and ``Directory.Build.props`` are both located
    this way and both read as DATA -- never imported -- so a single upward walk
    owns the rule that an ancestor file counts only while it stays inside the
    candidate worktree.
    """
    root = Path(os.path.abspath(str(worktree)))
    found: dict[str, Path] = {}
    for path, _root in projects:
        current = path.parent
        while _is_within(current, root):
            candidate = current / name
            if candidate.is_file():
                found.setdefault(os.path.normcase(str(candidate)), candidate)
            if os.path.normcase(str(current)) == os.path.normcase(str(root)):
                break
            parent = current.parent
            if parent == current:
                break
            current = parent
    return sorted(found.values(), key=lambda value: (len(value.parts), str(value)))


def _central_data(
    props_files: Sequence[Path], reader: _BoundedXml
) -> tuple[dict[str, str | None], list[tuple[str, str | None]]]:
    """Central package management rows, read as data from the props files.

    ``PackageVersion`` supplies the version for a row a project declares
    without one, the nearest declaration winning.  ``GlobalPackageReference``
    is itself a package every project restores -- a repo-wide analyzer,
    typically -- so it comes back as a declared row rather than as a version.
    """
    declared: dict[str, str | None] = {}
    repository_wide: list[tuple[str, str | None]] = []
    for props in props_files:
        for element in reader.root(props).iter():
            name = _local(element.tag)
            if name not in ("PackageVersion", "GlobalPackageReference"):
                continue
            identity = (element.get("Include") or element.get("Update") or "").strip()
            if not identity:
                continue
            if name == "PackageVersion":
                declared[identity.lower()] = _declared_version(element)
            else:
                repository_wide.append((identity, _declared_version(element)))
    return declared, repository_wide


def _resolved_packages(
    packages: Sequence[tuple[str, str | None]],
    central: Mapping[str, str | None],
    *,
    supplementary: bool = False,
) -> dict[str, str]:
    """One literal version per identity, or the reason there is not one.

    A supplementary seed drops the row it cannot resolve rather than refusing,
    so one ``$(Version)`` beside a literal one still seeds the literal.
    """
    resolved: dict[str, str] = {}
    for identity, version in packages:
        declared = version if version is not None else central.get(identity.lower())
        if not _PACKAGE_ID.match(identity):
            reason = "non_literal_package_id"
        elif declared is None:
            reason = "package_version_unresolved"
        elif not _PACKAGE_VERSION.match(declared):
            reason = "non_literal_package_version"
        else:
            resolved[identity] = declared
            continue
        if not supplementary:
            raise _SeedRefusal(reason)
    return resolved


def _resolved_frameworks(
    frameworks: Sequence[str], *, supplementary: bool = False
) -> list[str]:
    """The declared target frameworks, deduplicated in first-seen order.

    A supplementary seed drops a non-literal moniker: the canonical restore it
    follows already seeded whatever that configuration resolves to.
    """
    ordered: list[str] = []
    for value in frameworks:
        if not _TARGET_FRAMEWORK.match(value):
            if supplementary:
                continue
            raise _SeedRefusal("non_literal_target_framework")
        if value not in ordered:
            ordered.append(value)
    return ordered


def _seed_project_xml(
    packages: Mapping[str, str], frameworks: Sequence[str]
) -> str:
    """The AIWorkHub-authored project, built only from validated literals."""
    lines = [
        "<!-- Generated by AIWorkHub from candidate project XML data only. -->",
        '<Project Sdk="Microsoft.NET.Sdk">',
        "  <PropertyGroup>",
        f"    <TargetFrameworks>{';'.join(frameworks)}</TargetFrameworks>",
        "    <EnableDefaultItems>false</EnableDefaultItems>",
        "    <ManagePackageVersionsCentrally>false</ManagePackageVersionsCentrally>",
        "    <NuGetAudit>false</NuGetAudit>",
        "  </PropertyGroup>",
        "  <ItemGroup>",
    ]
    lines.extend(
        f'    <PackageReference Include="{identity}" '
        f'Version="{packages[identity]}" />'
        for identity in sorted(packages)
    )
    lines.extend(["  </ItemGroup>", "</Project>", ""])
    return "\n".join(lines)


def _write_seed_project(
    home: Path, packages: Mapping[str, str], frameworks: Sequence[str]
) -> Path:
    """Author the seed under the request home and return the project path."""
    directory = Path(home) / SEED_DIRNAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Inert stubs so no Directory.Build.* above the request home can reach
        # the generated project: the seed restores exactly the data read from
        # the candidate, and nothing an unrelated host tree contributes.
        for stub in ("Directory.Build.props", "Directory.Build.targets"):
            (directory / stub).write_text("<Project />\n", encoding="utf-8")
        project = directory / SEED_PROJECT_NAME
        project.write_text(
            _seed_project_xml(packages, frameworks), encoding="utf-8"
        )
    except OSError:
        raise _SeedRefusal("synthetic_project_unwritable") from None
    return project


def synthetic_restore_project(
    project: str | Path,
    worktree: str | Path,
    home: str | Path,
    *,
    supplementary: bool = False,
) -> tuple[Path | None, str | None]:
    """The AIWorkHub-authored restore project for one candidate target.

    ``(path, None)`` is the only project the host restore may be given.
    ``(None, None)`` means there is nothing left to seed, so nothing blocks the
    container command.  ``(None, reason)`` is a named environment reason, never
    a candidate validation failure.

    A strict seed answers for a target the canonical tree lacks, so an
    unresolvable row is a refusal: nothing else will have seeded it.  A
    ``supplementary`` seed runs *behind* a canonical restore, for a card that
    adds a project or a ``PackageReference`` to a target the canonical tree
    already carries; every canonical declaration is seeded there already, so a
    row this module cannot resolve without evaluating candidate MSBuild is
    dropped instead, as is a target kind this module reads only as a name and a
    project the candidate solution still lists after the card deleted it: the
    container build decides both.  The bounds on the read itself -- unsafe,
    oversized, unparseable or escaping XML -- still refuse in both modes.
    """
    reader = _BoundedXml()
    target = Path(os.path.abspath(str(project)))
    root = Path(os.path.abspath(str(worktree)))
    suffix = target.suffix.lower()
    try:
        if not _is_within(target, root) or not target.is_file():
            return None, "candidate_target_missing"
        if suffix in _SOLUTION_SUFFIXES:
            entries = _solution_project_entries(target, reader)
        elif suffix in _PROJECT_SUFFIXES:
            entries = [target]
        else:
            return _seed_outcome(
                "candidate_target_kind_unsupported", supplementary=supplementary
            )
        projects = _candidate_project_closure(entries, root, reader)
        imported = [
            (path, reader.root(path))
            for path in _ancestor_files(projects, root, _DIRECTORY_BUILD_NAME)
        ]
        packages, frameworks = _declared_data([*projects, *imported])
        central, repository_wide = _central_data(
            _ancestor_files(projects, root, _CENTRAL_PACKAGES_NAME), reader
        )
        resolved = _resolved_packages(
            [*packages, *repository_wide], central, supplementary=supplementary
        )
        if not resolved:
            return None, None
        declared = _resolved_frameworks(frameworks, supplementary=supplementary)
        if not declared:
            if supplementary:
                return None, None
            return None, "target_framework_unresolved"
        return _write_seed_project(Path(home), resolved, declared), None
    except _SeedRefusal as refusal:
        return _seed_outcome(refusal.reason, supplementary=supplementary)
