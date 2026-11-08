"""NF-2026-02181: guard R8 lets a session Read its own rendered scratch images."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from aiworkhub import tool_guard_hook  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 16


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub").mkdir(parents=True)
    return root.resolve()


def _put(root: Path, rel: str, data: bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _decide(root: Path, tool: str, path: Path | str, session_id: object = "s1"):
    return tool_guard_hook.decide(tool, {"file_path": str(path)}, root, session_id=session_id)


def _rule(verdict: tuple[str, str] | None) -> str | None:
    return None if verdict is None else verdict[0]


@pytest.mark.parametrize(
    ("rel", "data"),
    [
        (".scratch/s1/shot.png", PNG),
        (".scratch/s1/shot.PNG", PNG),
        (".scratch/s1/shot.jpg", JPEG),
        (".scratch/s1/shot.jpeg", JPEG),
        (".scratch/s1/shot.webp", WEBP),
        (".scratch/s1/nested/deeper/shot.png", PNG),
    ],
)
def test_own_session_image_read_is_allowed(repo: Path, rel: str, data: bytes) -> None:
    path = _put(repo, rel, data)
    assert _decide(repo, "Read", path) is None


def test_relative_path_is_allowed_too(repo: Path) -> None:
    _put(repo, ".scratch/s1/shot.png", PNG)
    verdict = tool_guard_hook.decide(
        "Read", {"file_path": ".scratch/s1/shot.png"}, repo, cwd=str(repo), session_id="s1"
    )
    assert verdict is None


def test_other_session_directory_is_denied(repo: Path) -> None:
    path = _put(repo, ".scratch/other/shot.png", PNG)
    assert _rule(_decide(repo, "Read", path)) == "R8"


def test_session_id_prefix_directory_is_denied(repo: Path) -> None:
    path = _put(repo, ".scratch/s10/shot.png", PNG)
    assert _rule(_decide(repo, "Read", path, "s1")) == "R8"


@pytest.mark.parametrize("session_id", [None, "", 7, ["s1"]])
def test_missing_or_invalid_session_id_is_denied(repo: Path, session_id: object) -> None:
    path = _put(repo, ".scratch/s1/shot.png", PNG)
    assert _rule(_decide(repo, "Read", path, session_id)) == "R8"
    omitted = tool_guard_hook.decide("Read", {"file_path": str(path)}, repo)
    assert _rule(omitted) == "R8"


def test_text_file_and_text_bytes_with_image_suffix_are_denied(repo: Path) -> None:
    text = _put(repo, ".scratch/s1/notes.txt", b"hello\n")
    fake = _put(repo, ".scratch/s1/x.png", b"not really a png at all")
    assert _rule(_decide(repo, "Read", text)) == "R8"
    assert _rule(_decide(repo, "Read", fake)) == "R8"


def test_missing_png_and_image_bytes_without_image_suffix_are_denied(repo: Path) -> None:
    (repo / ".scratch" / "s1").mkdir(parents=True)
    assert _rule(_decide(repo, "Read", repo / ".scratch" / "s1" / "gone.png")) == "R8"
    blob = _put(repo, ".scratch/s1/shot.bin", PNG)
    noext = _put(repo, ".scratch/s1/shot", PNG)
    assert _rule(_decide(repo, "Read", blob)) == "R8"
    assert _rule(_decide(repo, "Read", noext)) == "R8"


def test_wrong_signature_for_the_suffix_is_denied(repo: Path) -> None:
    jpeg_as_png = _put(repo, ".scratch/s1/a.png", JPEG)
    riff_only = _put(repo, ".scratch/s1/b.webp", b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 8)
    short = _put(repo, ".scratch/s1/c.png", PNG[:4])
    assert _rule(_decide(repo, "Read", jpeg_as_png)) == "R8"
    assert _rule(_decide(repo, "Read", riff_only)) == "R8"
    assert _rule(_decide(repo, "Read", short)) == "R8"


@pytest.mark.parametrize("tool", ["Edit", "MultiEdit", "Write"])
def test_edit_multiedit_and_overwrite_stay_denied(repo: Path, tool: str) -> None:
    path = _put(repo, ".scratch/s1/shot.png", PNG)
    assert _rule(_decide(repo, tool, path)) == "R8"


def test_write_creating_a_new_scratch_file_is_still_allowed(repo: Path) -> None:
    assert _decide(repo, "Write", repo / ".scratch" / "s1" / "new.png") is None


def _feed(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> None:
    class _Stdin:
        buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(sys, "stdin", _Stdin())


def _main(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path: Path,
    session_id: str,
) -> str:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    _feed(
        monkeypatch,
        {
            "tool_name": "Read",
            "tool_input": {"file_path": str(path)},
            "cwd": str(repo),
            "session_id": session_id,
        },
    )
    assert tool_guard_hook.main([]) == 0
    return capsys.readouterr().out


def test_main_passes_the_payload_session_id(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    own = _put(repo, ".scratch/s1/shot.png", PNG)
    foreign = _put(repo, ".scratch/s2/shot.png", PNG)

    assert _main(repo, monkeypatch, capsys, own, "s1") == ""

    out = _main(repo, monkeypatch, capsys, foreign, "s1")
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"].startswith("AIWorkHub tool guard R8:")
