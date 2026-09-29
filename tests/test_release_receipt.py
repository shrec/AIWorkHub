import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "release_receipt.py"
_SPEC = importlib.util.spec_from_file_location("release_receipt", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
rr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rr)

_ISO8601_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")



def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    for key in rr._GIT_ENV_BLOCKLIST:
        env.pop(key, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=env,
    )
    return result.stdout.decode().strip()


def _set_version(repo: Path, version: str) -> None:
    version_dir = repo / "src" / "aiworkhub"
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "_version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    _git(repo, "add", "src/aiworkhub/_version.py")
    _git(repo, "commit", "-m", f"set version {version}")


def _init_repo(repo: Path, version: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.autocrlf", "false")
    _set_version(repo, version)


def _make_vsix(path: Path, content: bytes = b"vsix-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, "1.2.3")
    return repo_dir


def _ledger(repo_dir: Path) -> Path:
    return repo_dir / "ledger.jsonl"


def test_record_appends_one_well_formed_built_line(repo: Path) -> None:
    vsix = _make_vsix(repo / "dist" / "aiworkhub-1.2.3.vsix")
    ledger = _ledger(repo)

    exit_code = rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(vsix),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 0
    raw_lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 1
    entry = json.loads(raw_lines[0])
    assert entry["kind"] == "built"
    assert entry["version"] == "1.2.3"
    assert entry["vsix_sha256"] == rr._sha256_file(vsix)
    assert entry["release_commit"] == _git(repo, "rev-parse", "HEAD")
    assert _ISO8601_Z.fullmatch(entry["built_at"])
    assert entry["previous_vsix"] is None
    assert entry["target"] == "vscode_local"


def test_record_repeat_with_same_sha_is_a_noop(repo: Path) -> None:
    vsix = _make_vsix(repo / "dist" / "aiworkhub-1.2.3.vsix")
    ledger = _ledger(repo)
    argv = [
        "record",
        "--version",
        "1.2.3",
        "--vsix",
        str(vsix),
        "--ledger",
        str(ledger),
        "--repo-root",
        str(repo),
    ]

    assert rr.main(argv) == 0
    assert rr.main(argv) == 0

    assert len(rr.load_ledger(ledger)) == 1


def test_record_different_sha_for_same_version_is_refused(repo: Path) -> None:
    ledger = _ledger(repo)
    first = _make_vsix(repo / "dist" / "first.vsix", b"first-bytes")
    rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(first),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    second = _make_vsix(repo / "dist" / "second.vsix", b"second-bytes")
    exit_code = rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(second),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 2
    assert len(rr.load_ledger(ledger)) == 1


def test_record_refuses_on_version_mismatch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vsix = _make_vsix(repo / "dist" / "aiworkhub-9.9.9.vsix")
    ledger = _ledger(repo)

    exit_code = rr.main(
        [
            "record",
            "--version",
            "9.9.9",
            "--vsix",
            str(vsix),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 2
    assert rr.load_ledger(ledger) == []
    stderr_lines = capsys.readouterr().err.strip().splitlines()
    assert len(stderr_lines) == 1
    assert "error" in json.loads(stderr_lines[0])


def test_record_refuses_when_vsix_missing(repo: Path) -> None:
    ledger = _ledger(repo)

    exit_code = rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(repo / "dist" / "missing.vsix"),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 2
    assert rr.load_ledger(ledger) == []


def test_record_stores_previous_vsix_relative_to_repo_root(repo: Path) -> None:
    vsix = _make_vsix(repo / "dist" / "aiworkhub-1.2.3.vsix")
    previous = _make_vsix(repo / "dist" / "aiworkhub-1.2.2.vsix", b"previous-bytes")
    ledger = _ledger(repo)

    exit_code = rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(vsix),
            "--previous-vsix",
            str(previous),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 0
    entry = rr.load_ledger(ledger)[0]
    assert entry["previous_vsix"] == {
        "path": "dist/aiworkhub-1.2.2.vsix",
        "sha256": rr._sha256_file(previous),
    }
    assert not Path(entry["previous_vsix"]["path"]).is_absolute()
    assert str(Path.home()) not in json.dumps(entry)


def test_confirm_appends_an_installed_line(repo: Path) -> None:
    vsix = _make_vsix(repo / "dist" / "aiworkhub-1.2.3.vsix")
    ledger = _ledger(repo)
    rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(vsix),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    exit_code = rr.main(
        [
            "confirm",
            "--version",
            "1.2.3",
            "--server-version",
            "1.2.3",
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 0
    installed = [e for e in rr.load_ledger(ledger) if e["kind"] == "installed"]
    assert len(installed) == 1
    assert installed[0]["version"] == "1.2.3"
    assert installed[0]["server_version"] == "1.2.3"
    assert _ISO8601_Z.fullmatch(installed[0]["installed_at"])


def test_confirm_refuses_on_server_version_mismatch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vsix = _make_vsix(repo / "dist" / "aiworkhub-1.2.3.vsix")
    ledger = _ledger(repo)
    rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(vsix),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    exit_code = rr.main(
        [
            "confirm",
            "--version",
            "1.2.3",
            "--server-version",
            "1.2.4",
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 2
    assert all(e["kind"] != "installed" for e in rr.load_ledger(ledger))
    stderr_lines = capsys.readouterr().err.strip().splitlines()
    assert len(stderr_lines) == 1
    assert "error" in json.loads(stderr_lines[0])


def test_confirm_refuses_without_a_built_line(repo: Path) -> None:
    ledger = _ledger(repo)

    exit_code = rr.main(
        [
            "confirm",
            "--version",
            "1.2.3",
            "--server-version",
            "1.2.3",
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 2
    assert rr.load_ledger(ledger) == []


def test_confirm_repeat_is_idempotent(repo: Path) -> None:
    vsix = _make_vsix(repo / "dist" / "aiworkhub-1.2.3.vsix")
    ledger = _ledger(repo)
    rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(vsix),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )
    argv = [
        "confirm",
        "--version",
        "1.2.3",
        "--server-version",
        "1.2.3",
        "--ledger",
        str(ledger),
        "--repo-root",
        str(repo),
    ]

    assert rr.main(argv) == 0
    assert rr.main(argv) == 0

    installed = [e for e in rr.load_ledger(ledger) if e["kind"] == "installed"]
    assert len(installed) == 1


def test_load_ledger_returns_empty_list_for_missing_file(tmp_path: Path) -> None:
    assert rr.load_ledger(tmp_path / "missing.jsonl") == []


def test_load_ledger_parses_each_json_line(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"kind": "built", "version": "1.0.0"})
        + "\n"
        + json.dumps({"kind": "installed", "version": "1.0.0"})
        + "\n",
        encoding="utf-8",
    )

    entries = rr.load_ledger(ledger)

    assert entries == [
        {"kind": "built", "version": "1.0.0"},
        {"kind": "installed", "version": "1.0.0"},
    ]


def test_latest_confirmed_returns_none_without_any_fully_confirmed_version(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"kind": "built", "version": "1.0.0"}) + "\n", encoding="utf-8")

    assert rr.latest_confirmed(ledger) is None


def test_latest_confirmed_returns_the_newest_fully_confirmed_version(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    lines = [
        {"kind": "built", "version": "1.0.0"},
        {"kind": "installed", "version": "1.0.0", "server_version": "1.0.0"},
        {"kind": "built", "version": "1.2.0"},
        {"kind": "installed", "version": "1.2.0", "server_version": "1.2.0"},
        {"kind": "built", "version": "1.3.0"},
    ]
    ledger.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    result = rr.latest_confirmed(ledger)

    assert result == {"kind": "built", "version": "1.2.0"}


def test_latest_confirmed_ignores_a_confirmation_from_another_server_version(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    lines = [
        {"kind": "built", "version": "1.0.0"},
        {"kind": "installed", "version": "1.0.0", "server_version": "0.9.0"},
    ]
    ledger.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    assert rr.latest_confirmed(ledger) is None


def test_the_script_and_the_deploy_proof_share_one_ledger_parser() -> None:
    assert rr.sdlc_deploy_proof.LEDGER_REL == tuple(rr._LEDGER_RELATIVE.parts)
    assert rr._GIT_ENV_BLOCKLIST is rr.sdlc_deploy_proof.GIT_ENV_BLOCKLIST


def test_record_refuses_previous_vsix_outside_repo_root(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vsix = _make_vsix(repo / "dist" / "aiworkhub-1.2.3.vsix")
    outside = _make_vsix(tmp_path / "outside" / "aiworkhub-1.2.2.vsix", b"outside-bytes")
    ledger = _ledger(repo)
    ledger.write_text(json.dumps({"kind": "built", "version": "1.0.0"}) + "\n", encoding="utf-8")
    before = ledger.read_bytes()

    exit_code = rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(vsix),
            "--previous-vsix",
            str(outside),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 2
    assert ledger.read_bytes() == before
    stderr_lines = capsys.readouterr().err.strip().splitlines()
    assert len(stderr_lines) == 1
    assert "error" in json.loads(stderr_lines[0])


def test_record_refuses_on_malformed_ledger_line(repo: Path) -> None:
    vsix = _make_vsix(repo / "dist" / "aiworkhub-1.2.3.vsix")
    ledger = _ledger(repo)
    ledger.write_text("not-json\n", encoding="utf-8")
    before = ledger.read_bytes()

    exit_code = rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(vsix),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 2
    assert ledger.read_bytes() == before


def test_confirm_refuses_on_malformed_ledger_line(repo: Path) -> None:
    ledger = _ledger(repo)
    ledger.write_text("not-json\n", encoding="utf-8")
    before = ledger.read_bytes()

    exit_code = rr.main(
        [
            "confirm",
            "--version",
            "1.2.3",
            "--server-version",
            "1.2.3",
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 2
    assert ledger.read_bytes() == before


def test_record_uses_repo_root_argument_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, "1.2.3")
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    vsix = _make_vsix(repo_dir / "dist" / "aiworkhub-1.2.3.vsix")
    ledger = _ledger(repo_dir)

    exit_code = rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(vsix),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo_dir),
        ]
    )

    assert exit_code == 0
    assert len(rr.load_ledger(ledger)) == 1


def test_record_refuses_on_non_utf8_ledger_bytes(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vsix = _make_vsix(repo / "dist" / "aiworkhub-1.2.3.vsix")
    ledger = _ledger(repo)
    ledger.write_bytes(b"\xff\n")
    before = ledger.read_bytes()

    exit_code = rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(vsix),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 2
    assert ledger.read_bytes() == before
    stderr_lines = capsys.readouterr().err.strip().splitlines()
    assert len(stderr_lines) == 1
    assert "error" in json.loads(stderr_lines[0])


def test_confirm_refuses_on_non_utf8_ledger_bytes(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = _ledger(repo)
    ledger.write_bytes(b"\xff\n")
    before = ledger.read_bytes()

    exit_code = rr.main(
        [
            "confirm",
            "--version",
            "1.2.3",
            "--server-version",
            "1.2.3",
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 2
    assert ledger.read_bytes() == before
    stderr_lines = capsys.readouterr().err.strip().splitlines()
    assert len(stderr_lines) == 1
    assert "error" in json.loads(stderr_lines[0])


def test_record_refuses_on_non_object_json_ledger_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vsix = _make_vsix(repo / "dist" / "aiworkhub-1.2.3.vsix")
    ledger = _ledger(repo)
    ledger.write_text("[1]\n", encoding="utf-8")
    before = ledger.read_bytes()

    exit_code = rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(vsix),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 2
    assert ledger.read_bytes() == before
    stderr_lines = capsys.readouterr().err.strip().splitlines()
    assert len(stderr_lines) == 1
    assert "is not a JSON object" in json.loads(stderr_lines[0])["error"]


def test_ledger_with_int_version_is_refused_by_both_subcommands(repo: Path) -> None:
    vsix = _make_vsix(repo / "dist" / "aiworkhub-1.2.3.vsix")
    ledger = _ledger(repo)
    ledger.write_text(
        json.dumps({"kind": "built", "version": 1})
        + "\n"
        + json.dumps({"kind": "installed", "version": 1})
        + "\n",
        encoding="utf-8",
    )
    before = ledger.read_bytes()

    with pytest.raises(rr.ReceiptRefused):
        rr.load_ledger(ledger)

    record_exit = rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(vsix),
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )
    confirm_exit = rr.main(
        [
            "confirm",
            "--version",
            "1.2.3",
            "--server-version",
            "1.2.3",
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert record_exit == 2
    assert confirm_exit == 2
    assert ledger.read_bytes() == before


def test_confirm_refuses_on_oversized_integer_ledger_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = _ledger(repo)
    ledger.write_text("9" * 5000 + "\n", encoding="utf-8")

    exit_code = rr.main(
        [
            "confirm",
            "--version",
            "1.2.3",
            "--server-version",
            "1.2.3",
            "--ledger",
            str(ledger),
            "--repo-root",
            str(repo),
        ]
    )

    assert exit_code == 2
    stderr_lines = capsys.readouterr().err.strip().splitlines()
    assert len(stderr_lines) == 1
    error = json.loads(stderr_lines[0])["error"]
    assert str(ledger) in error
    assert "is not valid JSON" in error


def test_confirm_uses_repo_root_argument_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, "1.2.3")
    vsix = _make_vsix(repo_dir / "dist" / "aiworkhub-1.2.3.vsix")
    rr.main(
        [
            "record",
            "--version",
            "1.2.3",
            "--vsix",
            str(vsix),
            "--repo-root",
            str(repo_dir),
        ]
    )

    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    exit_code = rr.main(
        [
            "confirm",
            "--version",
            "1.2.3",
            "--server-version",
            "1.2.3",
            "--repo-root",
            str(repo_dir),
        ]
    )

    assert exit_code == 0
    ledger = repo_dir / rr._LEDGER_RELATIVE
    installed = [e for e in rr.load_ledger(ledger) if e["kind"] == "installed"]
    assert len(installed) == 1
    assert not (other_cwd / ".aiworkhub").exists()
