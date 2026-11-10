"""The repository is public: tracked code must not carry this host's paths.

Scans git-tracked text files under the roots below and fails on a user-home
path whose user segment is not a generic placeholder, and on a drive-letter
development root.  Binary files are skipped.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ROOTS = ("src/", "tests/", "scripts/", "examples/", "vscode-extension/test/", "eval/", "data/")
# Generic words only; a real login never belongs here.
ALLOWED_SEGMENTS = frozenset({"owner", "worker", "user", "dev", "u", "probe-user"})

# One path separator: single backslash, doubled (up to four, for escaped
# strings) backslash or forward slash.
_SEP = r"(?:\\{1,4}|/+)"
_SEGMENT = r"(?P<seg>\w[\w.\-]*)"
_DRIVE = r"(?<![A-Za-z0-9_])[A-Za-z]:"
_NOT_PATH_TAIL = r"(?<![A-Za-z0-9_.~}\)\]\\])"

_USER_HOME_PATTERNS = (
    ("windows user home", re.compile(_DRIVE + _SEP + "Users" + _SEP + _SEGMENT, re.IGNORECASE)),
    ("posix home", re.compile(_NOT_PATH_TAIL + "/home/" + _SEGMENT, re.IGNORECASE)),
    (
        "wsl user home",
        re.compile(
            _NOT_PATH_TAIL + "/mnt/[A-Za-z]/Users/" + _SEGMENT,
            re.IGNORECASE,
        ),
    ),
)
_DEV_ROOT = re.compile(_DRIVE + _SEP + "Dev(?![A-Za-z0-9_])", re.IGNORECASE)


def scan_text(text: str) -> list[str]:
    """Return one finding label per host-path hit in ``text``."""
    findings: list[str] = []
    for label, pattern in _USER_HOME_PATTERNS:
        for match in pattern.finditer(text):
            if match.group("seg").lower() not in ALLOWED_SEGMENTS:
                findings.append(label)
    findings.extend("development root" for _ in _DEV_ROOT.finditer(text))
    return findings


def _on_disk_files() -> list[str]:
    """Files under ROOTS on disk: a superset of the tracked set."""
    return sorted(
        path.relative_to(REPO).as_posix()
        for root in ROOTS
        for path in (REPO / root).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and "node_modules" not in path.parts
    )


def _tracked_files() -> list[str]:
    """Tracked paths under ROOTS.

    When git cannot answer (no git binary, or a worktree whose git directory is
    unreachable, as in a sandbox), or answers with no paths at all (exit 0 with
    empty stdout: an extracted archive such as `git archive HEAD` nested inside
    another work tree, which git resolves to the enclosing repository), the
    on-disk files under ROOTS are scanned instead, so the guard only gets stricter.
    """
    git = shutil.which("git")
    if git is None:
        return _on_disk_files()
    result = subprocess.run(
        [git, "-C", str(REPO), "ls-files", "-z", "--", *ROOTS],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        tracked = [name for name in result.stdout.decode("utf-8").split("\0") if name]
        if tracked:
            return tracked
    return _on_disk_files()


def _read_text(path: Path) -> str | None:
    """File text, or None for a binary or unreadable-as-text file."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _win(sep: str, seg: str, drive: str = "C") -> str:
    return f"{drive}:{sep}Users{sep}{seg}{sep}project"


@pytest.mark.parametrize("sep", ["\\", "\\\\", "/"])
@pytest.mark.parametrize("drive", ["C", "D", "z"])
def test_windows_home_with_unlisted_segment_is_a_hit(sep: str, drive: str) -> None:
    assert scan_text(_win(sep, "zed", drive)) == ["windows user home"]


@pytest.mark.parametrize("sep", ["\\", "\\\\", "/"])
@pytest.mark.parametrize("seg", sorted(ALLOWED_SEGMENTS))
def test_windows_home_with_allowlisted_segment_is_not_a_hit(sep: str, seg: str) -> None:
    assert scan_text(_win(sep, seg)) == []


def test_posix_and_wsl_homes_with_unlisted_segment_are_hits() -> None:
    assert scan_text("/home/" + "zed" + "/x") == ["posix home"]
    assert scan_text("/mnt/" + "c" + "/Users/" + "zed" + "/x") == ["wsl user home"]
    assert scan_text("/mnt/" + "d" + "/Users/" + "owner" + "/x") == []
    assert scan_text("/home/" + "owner" + "/x") == []


@pytest.mark.parametrize(
    ("prefix", "root", "label"),
    [
        ("file://", "/home/", "posix home"),
        ("artifact://", "/home/", "posix home"),
        ("ci://-", "/home/", "posix home"),
        ("rejected-", "/home/", "posix home"),
        ("rejected,", "/HOME/", "posix home"),
        ("file://", "/mnt/c/Users/", "wsl user home"),
    ],
)
def test_scheme_and_punctuation_adjacent_homes_hit(prefix: str, root: str, label: str) -> None:
    assert scan_text(prefix + root + "zed" + "/x") == [label]


def test_non_ascii_user_segment_is_a_hit() -> None:
    assert scan_text("/home/" + "жed" + "/x") == ["posix home"]


@pytest.mark.parametrize("text", ["tmp_path/home/zed", "https://host/home/zed", "../../home/zed"])
def test_path_tails_stay_non_hits(text: str) -> None:
    assert scan_text(text) == []


def test_bare_prefixes_are_not_hits() -> None:
    assert scan_text('assert "/home/" not in value') == []
    assert scan_text('assert "C:\\\\Users\\\\" not in value') == []
    assert scan_text("tmp_path/home/zed") == []
    assert scan_text("/home/{name}") == []


def test_drive_letter_development_root_is_a_hit() -> None:
    for sep in ("\\", "\\\\", "/"):
        for drive in ("C", "D", "e"):
            assert scan_text(f"{drive}:{sep}Dev{sep}project") == ["development root"]
    assert scan_text("C:\\Developer\\x") == []


def test_no_host_paths_in_tracked_files() -> None:
    tracked = [name for name in _tracked_files() if name.startswith(ROOTS)]
    assert tracked, "git ls-files returned nothing under the scanned roots"
    hits: list[str] = []
    scanned = 0
    for name in tracked:
        text = _read_text(REPO / name)
        if text is None:
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            hits.extend(f"{name}:{lineno}: {label}" for label in scan_text(line))
    assert scanned, "no tracked text file could be read"
    assert not hits, "host paths in tracked files:\n" + "\n".join(hits)


def test_guard_passes_on_its_own_source() -> None:
    text = _read_text(Path(__file__))
    assert text is not None
    assert scan_text(text) == []


def test_empty_ls_files_answer_falls_back_to_on_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _tracked_files() == _on_disk_files()
