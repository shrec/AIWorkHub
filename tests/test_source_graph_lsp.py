from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path, PureWindowsPath

import pytest

from aiworkhub.source_graph_lsp import (
    AMBIGUOUS,
    CORE_HEADROOM,
    DEFAULT_POSITION_ENCODING,
    EXTERNAL_DEPENDENCY,
    EXTERNAL_STDLIB,
    INPUT_COLUMN_UNIT,
    REPO_INTERNAL,
    SERVER_UNAVAILABLE,
    UNRESOLVED,
    BoundedWorkspace,
    DefinitionQuery,
    IndexedSource,
    LspBatchOutcome,
    LspDefinitionResult,
    LspServerSpec,
    build_bounded_workspace,
    classify_definition_payload,
    config_digest,
    finalize_results,
    lsp_process_count,
    make_pyright_config,
    path_excluded_from_workspace,
    relative_includes_only,
    resolve_definitions,
    server_command_available,
    utf8_byte_offset_to_lsp_character,
)

FAKE_LSP_FLAG = "--fake-lsp"


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(repo: Path, relative: str, data: bytes) -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _hash(data)


def _spec(*_args: object) -> LspServerSpec:
    command = [sys.executable, str(Path(__file__).resolve()), FAKE_LSP_FLAG]
    return LspServerSpec(command=tuple(command), version="fake-1.0", language="python")


def _env(scenario_path: Path, log_path: Path | None = None) -> dict[str, str]:
    src = str(Path(__file__).resolve().parents[1] / "src")
    current = os.environ.get("PYTHONPATH", "")
    env = {
        "AIWORKHUB_FAKE_LSP_SCENARIO": str(scenario_path),
        "PYTHONPATH": src if not current else src + os.pathsep + current,
    }
    if log_path is not None:
        env["AIWORKHUB_FAKE_LSP_LOG"] = str(log_path)
    return env

def _write_scenario(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _internal_location(uri: str) -> dict[str, object]:
    return {
        "uri": uri,
        "range": {
            "start": {"line": 0, "character": 4},
            "end": {"line": 0, "character": 7},
        },
    }


def _workspace(tmp_path: Path, sources: list[tuple[str, bytes]]) -> tuple[Path, BoundedWorkspace, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    indexed: list[IndexedSource] = []
    hashes: dict[str, str] = {}
    for relative, data in sources:
        digest = _write(repo, relative, data)
        indexed.append(IndexedSource(relative, digest))
        hashes[relative] = digest
    dest = tmp_path / "workspace"
    workspace = build_bounded_workspace(repo, indexed, dest)
    return repo, workspace, hashes


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")


def _external_file(tmp_path: Path, *parts: str) -> Path:
    path = tmp_path / "external" / Path(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"pass\n")
    return path


def _target_classifier(tmp_path: Path) -> Callable[[str], LspDefinitionResult]:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    query = DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)
    spec = LspServerSpec(command=("python3",), version="fake-1.0")

    def classify(uri: str) -> LspDefinitionResult:
        return classify_definition_payload(
            _internal_location(uri),
            query=query,
            repo_root=repo,
            workspace=workspace,
            indexed_hashes=hashes,
            source_bytes=source,
            server_spec=spec,
            config_digest_value="x",
        )

    return classify


def test_input_columns_are_utf8_byte_offsets() -> None:
    assert INPUT_COLUMN_UNIT == "utf8_byte"


def test_utf8_byte_offset_converts_to_negotiated_encodings() -> None:
    line = "éfoo".encode("utf-8")
    assert utf8_byte_offset_to_lsp_character(line, 2, "utf-8") == 2
    assert utf8_byte_offset_to_lsp_character(line, 2, "utf-16") == 1
    assert utf8_byte_offset_to_lsp_character(line, 2, "utf-32") == 1
    emoji = "😀x".encode("utf-8")
    assert utf8_byte_offset_to_lsp_character(emoji, 4, "utf-16") == 2
    assert utf8_byte_offset_to_lsp_character(emoji, 4, "utf-8") == 4
    assert utf8_byte_offset_to_lsp_character(emoji, 4, "utf-32") == 1


def test_process_count_is_derived_from_cores_with_headroom() -> None:
    assert lsp_process_count(100, observed_cores=16) == 16 - CORE_HEADROOM
    assert lsp_process_count(100, observed_cores=64) == 64 - CORE_HEADROOM
    assert lsp_process_count(100, observed_cores=2) == 1
    assert lsp_process_count(3, observed_cores=16) == 3
    assert lsp_process_count(0, observed_cores=16) == 1


def test_absolute_include_paths_are_not_trusted(tmp_path: Path) -> None:
    config = make_pyright_config([
        "/etc/passwd",
        str(tmp_path / "abs"),
        "../escape",
        "src",
        "src",
    ])
    assert config["include"] == ["src"]
    assert relative_includes_only(["/var/tmp", "pkg"]) == ("pkg",)


def test_workspace_excludes_runtime_and_never_uses_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src_hash = _write(repo, "src/mod.py", b"def foo():\n    return 1\n")
    _write(repo, ".aiworkhub/source_graph/source_graph.sqlite", b"db")
    _write(repo, "node_modules/pkg/index.js", b"module.exports = 1\n")
    _write(repo, ".claude/worktrees/t/src/mod.py", b"stolen\n")
    _write(repo, "__pycache__/mod.cpython-312.pyc", b"pyc")
    dest = tmp_path / "workspace"
    workspace = build_bounded_workspace(
        repo,
        [
            IndexedSource("src/mod.py", src_hash),
            IndexedSource(".aiworkhub/source_graph/source_graph.sqlite", _hash(b"db")),
            IndexedSource("node_modules/pkg/index.js", _hash(b"module.exports = 1\n")),
            IndexedSource(".claude/worktrees/t/src/mod.py", _hash(b"stolen\n")),
            IndexedSource("__pycache__/mod.cpython-312.pyc", _hash(b"pyc")),
        ],
        dest,
    )
    assert workspace.root.resolve() != repo.resolve()
    assert (dest / "src" / "mod.py").is_file()
    assert not (dest / ".aiworkhub").exists()
    assert not (dest / "node_modules").exists()
    assert not (dest / ".claude").exists()
    assert not (dest / "__pycache__").exists()
    assert "src/mod.py" in workspace.included_relative_paths
    config = json.loads((dest / "pyrightconfig.json").read_text(encoding="utf-8"))
    assert config["include"] == ["src"]
    assert not any(Path(item).is_absolute() for item in config["include"])
    assert ".aiworkhub" in config["exclude"]
    assert "node_modules" in config["exclude"]


def test_path_exclusion_covers_graph_db_and_worktrees() -> None:
    assert path_excluded_from_workspace(".aiworkhub/runtime/worktrees/x/src/a.py")
    assert path_excluded_from_workspace("src/source_graph.sqlite")
    assert path_excluded_from_workspace("node_modules/x.js")
    assert not path_excluded_from_workspace("src/mod.py")


def test_path_exclusion_is_case_insensitive() -> None:
    assert path_excluded_from_workspace("NODE_MODULES/pkg/index.js")
    assert path_excluded_from_workspace("SRC/SOURCE_GRAPH.SQLITE")
    assert not path_excluded_from_workspace("SRC/mod.py")


def test_fake_server_handshake_location_and_cleanup(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    uri = (workspace.root / "src" / "mod.py").resolve().as_uri()
    log_path = tmp_path / "lsp.log"
    scenario = _write_scenario(tmp_path / "scenario.json", {
        "positionEncoding": "utf-16",
        "result": _internal_location(uri),
    })
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        batch_timeout_s=4.0,
        env=_env(scenario, log_path),
    )
    assert outcome.children_reaped
    assert outcome.position_encoding == "utf-16"
    assert len(outcome.results) == 1
    result = outcome.results[0]
    assert result.classification == REPO_INTERNAL
    assert result.source_path == "src/mod.py"
    assert result.source_hash == hashes["src/mod.py"]
    assert result.source_line == 2
    assert result.source_column == 11
    assert result.target_uri == uri
    assert result.target_range == (0, 4, 0, 7)
    assert result.server_version == "fake-1.0"
    assert result.config_digest == config_digest(
        command=_spec(scenario).command,
        version="fake-1.0",
        include=workspace.config["include"],
        exclude=workspace.config["exclude"],
        position_encoding="utf-16",
    )
    methods = json.loads(log_path.read_text(encoding="utf-8"))
    assert methods == [
        "initialize",
        "initialized",
        "textDocument/didOpen",
        "textDocument/definition",
        "shutdown",
        "exit",
    ]


@pytest.mark.parametrize(
    ("encoding", "expected_character"),
    [("utf-8", 10), ("utf-32", 9)],
)
def test_non_ascii_byte_column_converted_per_negotiated_encoding(
    tmp_path: Path,
    encoding: str,
    expected_character: int,
) -> None:
    source = "s = 'caf\xe9'\n".encode("utf-8")
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    position_path = tmp_path / "position.log"
    scenario = _write_scenario(tmp_path / "scenario.json", {
        "positionEncoding": encoding,
        "result": None,
    })
    env = _env(scenario)
    env["AIWORKHUB_FAKE_LSP_POSITION_LOG"] = str(position_path)
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 1, 10)],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        batch_timeout_s=4.0,
        env=env,
    )
    assert outcome.position_encoding == encoding
    assert outcome.results[0].classification == UNRESOLVED
    assert json.loads(position_path.read_text(encoding="utf-8")) == {
        "line": 0,
        "character": expected_character,
    }


def test_many_query_batch_survives_default_output_budget(tmp_path: Path) -> None:
    from aiworkhub.source_graph_lsp import (
        DEFAULT_MAX_OUTPUT_BYTES,
        DEFAULT_MAX_TOTAL_OUTPUT_BYTES,
    )

    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    uri = (workspace.root / "src" / "mod.py").resolve().as_uri()
    scenario = _write_scenario(tmp_path / "many.json", {"result": _internal_location(uri)})
    count = 500
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[
            DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)
            for _ in range(count)
        ],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        batch_timeout_s=60.0,
        env=_env(scenario),
    )
    assert len(outcome.results) == count
    assert all(item.classification == REPO_INTERNAL for item in outcome.results)
    assert DEFAULT_MAX_TOTAL_OUTPUT_BYTES > DEFAULT_MAX_OUTPUT_BYTES
    assert outcome.output_bytes > DEFAULT_MAX_OUTPUT_BYTES


def test_non_positive_source_line_fails_closed(tmp_path: Path) -> None:
    from aiworkhub.source_graph_lsp import graph_line_to_lsp

    for bad in (0, -1, -7):
        with pytest.raises(ValueError):
            graph_line_to_lsp(bad)

    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    uri = (workspace.root / "src" / "mod.py").resolve().as_uri()
    log_path = tmp_path / "lsp.log"
    scenario = _write_scenario(tmp_path / "scenario.json", {"result": _internal_location(uri)})
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 0, 0)],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        env=_env(scenario, log_path),
    )
    assert outcome.results[0].classification == UNRESOLVED
    assert outcome.results[0].classification != REPO_INTERNAL
    methods = json.loads(log_path.read_text(encoding="utf-8"))
    assert "textDocument/definition" not in methods


def test_empty_queries_spawn_no_child(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    scenario = _write_scenario(tmp_path / "scenario.json", {"result": None})
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[],
        indexed_hashes=hashes,
    )
    assert outcome.results == ()
    assert outcome.child_pids == ()
    assert outcome.children_reaped
    assert outcome.input_bytes == 0
    assert outcome.output_bytes == 0


def test_location_link_and_interleaved_notifications(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    uri = (workspace.root / "src" / "mod.py").resolve().as_uri()
    scenario = _write_scenario(tmp_path / "scenario.json", {
        "publish_diagnostics": True,
        "extra_notification": True,
        "result": [{
            "targetUri": uri,
            "targetRange": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 7},
            },
            "targetSelectionRange": {
                "start": {"line": 0, "character": 4},
                "end": {"line": 0, "character": 7},
            },
        }],
    })
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        batch_timeout_s=4.0,
        env=_env(scenario),
    )
    assert outcome.results[0].classification == REPO_INTERNAL
    assert outcome.results[0].target_uri == uri
    assert outcome.results[0].target_range == (0, 4, 0, 7)


def test_missing_server_is_unavailable(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    spec = LspServerSpec(command=("/nonexistent/pyright-langserver",), version="0", language="python")
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=spec,
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
    )
    assert outcome.results[0].classification == SERVER_UNAVAILABLE
    assert outcome.results[0].target_uri is None
    assert not server_command_available(spec.command)


def test_empty_definition_is_unresolved(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    scenario = _write_scenario(tmp_path / "scenario.json", {"result": None})
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        env=_env(scenario),
    )
    assert outcome.results[0].classification == UNRESOLVED


def test_multiple_targets_are_ambiguous_not_internal(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    uri = (workspace.root / "src" / "mod.py").resolve().as_uri()
    scenario = _write_scenario(tmp_path / "scenario.json", {
        "result": [
            _internal_location(uri),
            {
                "uri": uri,
                "range": {
                    "start": {"line": 3, "character": 0},
                    "end": {"line": 3, "character": 3},
                },
            },
        ],
    })
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        env=_env(scenario),
    )
    assert outcome.results[0].classification == AMBIGUOUS
    assert outcome.results[0].target_uri is None


def test_timeout_and_cancel_reap_child(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    hang = _write_scenario(tmp_path / "hang.json", {"hang": True})
    timed = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(hang),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=0.2,
        batch_timeout_s=1.0,
        env=_env(hang),
    )
    assert timed.results[0].classification == UNRESOLVED
    assert timed.children_reaped
    for pid in timed.child_pids:
        with pytest.raises(OSError):
            os.kill(pid, 0)

    cancel = threading.Event()
    threading.Timer(0.1, cancel.set).start()
    cancelled = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(hang),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=5.0,
        batch_timeout_s=5.0,
        cancel_event=cancel,
        env=_env(hang),
    )
    assert cancelled.results[0].classification == UNRESOLVED
    assert cancelled.children_reaped


def test_malformed_framing_reaps_child(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    scenario = _write_scenario(tmp_path / "bad.json", {"malformed": True})
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=1.0,
        env=_env(scenario),
    )
    assert outcome.results[0].classification == UNRESOLVED
    assert outcome.children_reaped


def test_unbounded_output_cannot_be_repo_internal(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    scenario = _write_scenario(tmp_path / "huge.json", {"huge": True})
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        max_output_bytes=64,
        env=_env(scenario),
    )
    assert outcome.results[0].classification != REPO_INTERNAL
    assert outcome.results[0].classification == UNRESOLVED


def test_tiny_max_input_bytes_fails_closed(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    scenario = _write_scenario(tmp_path / "tiny.json", {})
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        max_input_bytes=100,
        env=_env(scenario),
    )
    assert outcome.input_bytes <= 100
    assert outcome.results[0].classification in {UNRESOLVED, SERVER_UNAVAILABLE}
    assert outcome.results[0].classification != REPO_INTERNAL
    assert outcome.children_reaped
    for pid in outcome.child_pids:
        with pytest.raises(OSError):
            os.kill(pid, 0)


def test_windows_stdio_timeout_cancel_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import aiworkhub.source_graph_lsp as lsp

    def _windows_select(*_args: object, **_kwargs: object) -> tuple[list[object], list[object], list[object]]:
        raise OSError(10038, "An operation was attempted on something that is not a socket")

    def _missing_killpg(*_args: object, **_kwargs: object) -> None:
        raise AttributeError("killpg")

    monkeypatch.setattr(lsp.select, "select", _windows_select)
    monkeypatch.setattr(lsp.os, "killpg", _missing_killpg)

    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    hang = _write_scenario(tmp_path / "hang-win.json", {"hang": True})
    timed = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(hang),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=0.2,
        batch_timeout_s=1.0,
        env=_env(hang),
    )
    assert timed.results[0].classification == UNRESOLVED
    assert timed.children_reaped
    for pid in timed.child_pids:
        with pytest.raises(OSError):
            os.kill(pid, 0)

    cancel = threading.Event()
    threading.Timer(0.1, cancel.set).start()
    cancelled = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(hang),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=5.0,
        batch_timeout_s=5.0,
        cancel_event=cancel,
        env=_env(hang),
    )
    assert cancelled.results[0].classification == UNRESOLVED
    assert cancelled.children_reaped


def _stalled_large_didopen(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n" + (b"x" * (1024 * 1024))
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    stall = _write_scenario(tmp_path / "stall.json", {"stall_stdin_after_initialize": True})
    started = time.monotonic()
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(stall),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=0.15,
        batch_timeout_s=0.3,
        env=_env(stall),
    )
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert outcome.results[0].classification == UNRESOLVED
    assert outcome.children_reaped
    for pid in outcome.child_pids:
        with pytest.raises(OSError):
            os.kill(pid, 0)
    assert not any(
        thread.name == "lsp-stdio-w" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_didopen_write_respects_deadline_when_server_stops_reading(tmp_path: Path) -> None:
    _stalled_large_didopen(tmp_path)


def test_windows_didopen_write_respects_deadline_when_server_stops_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiworkhub.source_graph_lsp as lsp

    def _windows_select(*_args: object, **_kwargs: object) -> tuple[list[object], list[object], list[object]]:
        raise OSError(10038, "An operation was attempted on something that is not a socket")

    def _missing_killpg(*_args: object, **_kwargs: object) -> None:
        raise AttributeError("killpg")

    monkeypatch.setattr(lsp.select, "select", _windows_select)
    monkeypatch.setattr(lsp.os, "killpg", _missing_killpg)
    _stalled_large_didopen(tmp_path)


def test_stale_source_hash_cannot_be_repo_internal(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    uri = (workspace.root / "src" / "mod.py").resolve().as_uri()
    scenario = _write_scenario(tmp_path / "ok.json", {"result": _internal_location(uri)})
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", "0" * 64, 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        env=_env(scenario),
    )
    assert outcome.results[0].classification != REPO_INTERNAL
    assert outcome.results[0].classification == UNRESOLVED


def test_symlink_and_path_escape_cannot_be_repo_internal(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"def evil():\n    return 0\n")
    escaped = tmp_path / "escape.py"
    escaped.symlink_to(outside)
    query = DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)
    spec = LspServerSpec(command=("python3",), version="fake-1.0")
    digest = "abc"
    symlink_result = classify_definition_payload(
        _internal_location(escaped.resolve().as_uri()),
        query=query,
        repo_root=repo,
        workspace=workspace,
        indexed_hashes=hashes,
        source_bytes=source,
        server_spec=spec,
        config_digest_value=digest,
    )
    assert symlink_result.classification != REPO_INTERNAL
    path_result = classify_definition_payload(
        _internal_location(outside.as_uri()),
        query=query,
        repo_root=repo,
        workspace=workspace,
        indexed_hashes=hashes,
        source_bytes=source,
        server_spec=spec,
        config_digest_value=digest,
    )
    assert path_result.classification != REPO_INTERNAL


def test_external_stdlib_and_dependency_are_evidence(tmp_path: Path) -> None:
    classify = _target_classifier(tmp_path)
    stdlib = _external_file(
        tmp_path, "usr", "lib", "python3.12", "typeshed", "stdlib", "os.pyi",
    )
    dependency = _external_file(
        tmp_path, "usr", "lib", "python3.12", "site-packages", "requests", "api.py",
    )
    std = classify(stdlib.as_uri())
    dep = classify(dependency.as_uri())
    assert std.classification == EXTERNAL_STDLIB
    assert std.target_uri == stdlib.as_uri()
    assert std.target_range == (0, 4, 0, 7)
    assert dep.classification == EXTERNAL_DEPENDENCY
    assert dep.target_uri == dependency.as_uri()


def test_dist_packages_classified_as_dependency_not_stdlib(tmp_path: Path) -> None:
    classify = _target_classifier(tmp_path)
    debian = _external_file(
        tmp_path, "usr", "lib", "python3", "dist-packages", "requests", "api.py",
    )
    result = classify(debian.as_uri())
    assert result.classification == EXTERNAL_DEPENDENCY
    assert result.target_uri == debian.as_uri()


def test_existing_posix_stdlib_layout_is_stdlib_evidence(tmp_path: Path) -> None:
    classify = _target_classifier(tmp_path)
    stdlib = _external_file(tmp_path, "usr", "lib", "python3.12", "os.py")
    lookalike = _external_file(tmp_path, "home", "dev", "mylib", "python_projects", "util.py")
    assert classify(stdlib.as_uri()).classification == EXTERNAL_STDLIB
    assert classify(lookalike.as_uri()).classification == EXTERNAL_DEPENDENCY


def test_results_are_deterministic_across_completion_order() -> None:
    first = LspDefinitionResult(
        source_path="b.py",
        source_hash="b",
        source_line=1,
        source_column=0,
        target_uri=None,
        target_range=None,
        server_command="fake",
        server_version="1",
        config_digest="d",
        classification=UNRESOLVED,
    )
    second = LspDefinitionResult(
        source_path="a.py",
        source_hash="a",
        source_line=1,
        source_column=0,
        target_uri=None,
        target_range=None,
        server_command="fake",
        server_version="1",
        config_digest="d",
        classification=UNRESOLVED,
    )
    assert finalize_results([first, second]) == finalize_results([second, first])
    assert finalize_results([first, second])[0].source_path == "a.py"


def test_batch_order_independent_of_server_completion(tmp_path: Path) -> None:
    files = [
        ("src/a.py", b"def a():\n    return a()\n"),
        ("src/b.py", b"def b():\n    return b()\n"),
    ]
    repo, workspace, hashes = _workspace(tmp_path, files)
    uri_a = (workspace.root / "src" / "a.py").resolve().as_uri()
    uri_b = (workspace.root / "src" / "b.py").resolve().as_uri()
    scenario = _write_scenario(tmp_path / "rev.json", {
        "defer_definitions": 2,
        "results": [_internal_location(uri_b), _internal_location(uri_a)],
    })
    queries = [
        DefinitionQuery("src/b.py", hashes["src/b.py"], 2, 11),
        DefinitionQuery("src/a.py", hashes["src/a.py"], 2, 11),
    ]
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=queries,
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        observed_cores=2,
        env=_env(scenario),
    )
    assert [item.source_path for item in outcome.results] == ["src/a.py", "src/b.py"]


def test_non_utf8_source_fails_closed_per_query(tmp_path: Path) -> None:
    valid = b"def good():\n    return good()\n"
    invalid = b"def bad():\n    return bad()\xff\n"
    repo, workspace, hashes = _workspace(tmp_path, [
        ("src/good.py", valid),
        ("src/bad.py", invalid),
    ])
    uri = (workspace.root / "src" / "good.py").resolve().as_uri()
    scenario = _write_scenario(tmp_path / "nonutf8.json", {"result": _internal_location(uri)})
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[
            DefinitionQuery("src/good.py", hashes["src/good.py"], 2, 11),
            DefinitionQuery("src/bad.py", hashes["src/bad.py"], 2, 11),
        ],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        observed_cores=2,
        env=_env(scenario),
    )
    by_path = {item.source_path: item for item in outcome.results}
    assert by_path["src/good.py"].classification == REPO_INTERNAL
    assert by_path["src/bad.py"].classification == UNRESOLVED
    assert outcome.children_reaped


def test_file_sharded_execution_launches_bounded_servers(tmp_path: Path) -> None:
    files = [
        (f"src/{name}.py", b"def f():\n    return f()\n")
        for name in ("a", "b", "c", "d")
    ]
    repo, workspace, hashes = _workspace(tmp_path, files)
    target = (workspace.root / "src" / "a.py").resolve().as_uri()
    scenario = _write_scenario(tmp_path / "shard.json", {"result": _internal_location(target)})
    queries = [
        DefinitionQuery(f"src/{name}.py", hashes[f"src/{name}.py"], 2, 11)
        for name in ("a", "b", "c", "d")
    ]
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=queries,
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        observed_cores=4,
        env=_env(scenario),
    )
    assert len(outcome.child_pids) == lsp_process_count(4, observed_cores=4)
    assert outcome.children_reaped
    assert [item.source_path for item in outcome.results] == [
        "src/a.py",
        "src/b.py",
        "src/c.py",
        "src/d.py",
    ]
    assert all(item.classification == REPO_INTERNAL for item in outcome.results)


def test_fake_transport_does_not_claim_lsp_feature_ready() -> None:
    assert not hasattr(sys.modules["aiworkhub.source_graph_lsp"], "LSP_FEATURE_READY")
    assert "ready" not in LspBatchOutcome.__annotations__


def test_uri_to_path_windows_drive_uri_is_platform_correct():
    from aiworkhub.source_graph_lsp import _uri_to_path

    path = _uri_to_path("file:///C:/repo/pkg/mod.py")
    assert path is not None
    normalized = str(path).replace("\\", "/")
    assert normalized == "C:/repo/pkg/mod.py"
    assert not normalized.startswith("/C:")
    if os.name == "nt":
        expected = Path("C:/repo/pkg/mod.py")
        assert _uri_to_path(expected.as_uri()) == expected


def test_uri_to_path_windows_unc_uri_round_trip():
    from aiworkhub.source_graph_lsp import _uri_to_path

    path = _uri_to_path("file://server/share/pkg/mod.py")
    assert path is not None
    normalized = str(path).replace("\\", "/")
    assert normalized == "//server/share/pkg/mod.py"
    if os.name == "nt":
        expected = Path("//server/share/pkg/mod.py")
        assert _uri_to_path(expected.as_uri()) == expected


def test_uri_to_path_localhost_authority_is_treated_as_local():
    from aiworkhub.source_graph_lsp import _uri_to_path

    path = _uri_to_path("file://localhost/C:/repo/pkg/mod.py")
    assert path is not None
    assert str(path).replace("\\", "/") == "C:/repo/pkg/mod.py"


def test_windows_drive_uri_is_not_posix_absolute_root_path():
    from aiworkhub.source_graph_lsp import _uri_to_path

    if os.name == "nt":
        pytest.skip("POSIX-only regression for the former /C:/ root form")
    path = _uri_to_path("file:///C:/repo/pkg/mod.py")
    assert path is not None
    assert not path.is_absolute()


def test_read_message_non_ascii_header_raises_typed_malformed():
    from aiworkhub.source_graph_lsp import LspMalformed, LspStdioSession

    payload = bytearray(b"\xff\xfeContent-Length: 2\r\n\r\n{}")

    class _StubSession:
        output_bytes = 0
        max_output_bytes = 4096
        max_total_output_bytes = 4096

        def _raise_if_cancelled(self):
            return None

        def _read_ready(self, stdout, count):
            chunk = bytes(payload[:count])
            del payload[:count]
            return chunk or None

    with pytest.raises(LspMalformed):
        LspStdioSession._read_message(_StubSession(), None)


@pytest.mark.parametrize(
    ("link_parts", "to_outside"),
    [(("src",), True), (("src", "pkg"), True), (("src",), False)],
)
def test_workspace_never_writes_through_preexisting_directory_symlink(
    tmp_path: Path,
    link_parts: tuple[str, ...],
    to_outside: bool,
) -> None:
    repo = tmp_path / "repo"
    rel = "/".join((*link_parts, "mod.py"))
    digest = _write(repo, rel, b"def foo():\n    return 1\n")
    dest = tmp_path / "workspace"
    landing = tmp_path / "outside" if to_outside else dest / "elsewhere"
    landing.mkdir(parents=True)
    link = dest.joinpath(*link_parts)
    link.parent.mkdir(parents=True, exist_ok=True)
    _symlink(link, landing)
    workspace = build_bounded_workspace(repo, [IndexedSource(rel, digest)], dest)
    assert list(landing.iterdir()) == []
    assert workspace.included_relative_paths == ()
    assert workspace.excluded_relative_paths == (rel,)
    assert workspace.config["include"] == []


def test_workspace_rejects_linked_destination_and_parent_components(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    digest = _write(repo, "src/mod.py", b"def foo():\n    return 1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.py"
    victim.write_bytes(b"victim\n")
    linked_dest = tmp_path / "linked-dest"
    _symlink(linked_dest, outside)
    with pytest.raises(ValueError):
        build_bounded_workspace(repo, [IndexedSource("src/mod.py", digest)], linked_dest)
    assert victim.read_bytes() == b"victim\n"
    assert not (outside / "src" / "mod.py").exists()
    assert not (outside / "pyrightconfig.json").exists()
    linked_parent = tmp_path / "linked-parent"
    _symlink(linked_parent, outside)
    with pytest.raises(ValueError):
        build_bounded_workspace(
            repo,
            [IndexedSource("src/mod.py", digest)],
            linked_parent / "workspace",
        )
    assert victim.read_bytes() == b"victim\n"
    assert not (outside / "workspace" / "src" / "mod.py").exists()
    assert not (outside / "workspace" / "pyrightconfig.json").exists()


def test_workspace_replaces_preexisting_file_links_without_writing_through(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    payload = b"def foo():\n    return 1\n"
    digest = _write(repo, "src/mod.py", payload)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.py"
    victim.write_bytes(b"victim\n")
    config_victim = outside / "config.json"
    config_victim.write_bytes(b"{}\n")
    dest = tmp_path / "workspace"
    (dest / "src").mkdir(parents=True)
    _symlink(dest / "src" / "mod.py", victim)
    _symlink(dest / "pyrightconfig.json", config_victim)
    workspace = build_bounded_workspace(repo, [IndexedSource("src/mod.py", digest)], dest)
    assert victim.read_bytes() == b"victim\n"
    assert config_victim.read_bytes() == b"{}\n"
    copied = dest / "src" / "mod.py"
    assert not copied.is_symlink()
    assert copied.read_bytes() == payload
    assert not workspace.config_path.is_symlink()
    written = json.loads(workspace.config_path.read_text(encoding="utf-8"))
    assert written["include"] == ["src"]
    assert workspace.included_relative_paths == ("src/mod.py",)


def test_workspace_breaks_preexisting_hard_links_before_writing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    payload = b"def foo():\n    return 1\n"
    digest = _write(repo, "src/mod.py", payload)
    victim = tmp_path / "victim.py"
    victim.write_bytes(b"victim\n")
    dest = tmp_path / "workspace"
    (dest / "src").mkdir(parents=True)
    try:
        os.link(victim, dest / "src" / "mod.py")
    except (OSError, NotImplementedError):
        pytest.skip("hard links are not available on this platform")
    build_bounded_workspace(repo, [IndexedSource("src/mod.py", digest)], dest)
    assert victim.read_bytes() == b"victim\n"
    assert (dest / "src" / "mod.py").read_bytes() == payload


def test_workspace_rebuild_replaces_existing_nested_copies(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    dest = tmp_path / "workspace"
    digest = _write(repo, "pkg/sub/mod.py", b"def foo():\n    return 1\n")
    build_bounded_workspace(repo, [IndexedSource("pkg/sub/mod.py", digest)], dest)
    second = b"def foo():\n    return 2\n"
    digest = _write(repo, "pkg/sub/mod.py", second)
    workspace = build_bounded_workspace(repo, [IndexedSource("pkg/sub/mod.py", digest)], dest)
    assert (dest / "pkg" / "sub" / "mod.py").read_bytes() == second
    assert workspace.included_relative_paths == ("pkg/sub/mod.py",)
    assert workspace.config["include"] == ["pkg"]


def test_workspace_destination_must_not_be_or_contain_the_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    digest = _write(repo, "src/mod.py", b"x = 1\n")
    for dest in (repo, tmp_path):
        with pytest.raises(ValueError):
            build_bounded_workspace(repo, [IndexedSource("src/mod.py", digest)], dest)
    assert not (tmp_path / "src").exists()
    assert not (tmp_path / "pyrightconfig.json").exists()
    assert not (repo / "pyrightconfig.json").exists()


def test_workspace_inside_repository_must_use_an_excluded_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shadowed = b"def shadowed():\n    return 1\n"
    other = b"def other():\n    return 2\n"
    sources = [
        IndexedSource("mod.py", _write(repo, "mod.py", other)),
        IndexedSource("pkg/mod.py", _write(repo, "pkg/mod.py", shadowed)),
    ]
    with pytest.raises(ValueError):
        build_bounded_workspace(repo, sources, repo / "pkg")
    assert (repo / "pkg" / "mod.py").read_bytes() == shadowed
    assert not (repo / "pkg" / "pyrightconfig.json").exists()
    nested = repo / ".aiworkhub" / "lsp" / "workspace"
    workspace = build_bounded_workspace(repo, sources, nested)
    assert workspace.included_relative_paths == ("mod.py", "pkg/mod.py")
    assert (nested / "mod.py").read_bytes() == other
    assert (repo / "pkg" / "mod.py").read_bytes() == shadowed


def test_malformed_definition_payload_is_never_repo_internal(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    uri = (workspace.root / "src" / "mod.py").resolve().as_uri()
    query = DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)
    spec = LspServerSpec(command=("python3",), version="fake-1.0")

    def classify(payload: object) -> LspDefinitionResult:
        return classify_definition_payload(
            payload,
            query=query,
            repo_root=repo,
            workspace=workspace,
            indexed_hashes=hashes,
            source_bytes=source,
            server_spec=spec,
            config_digest_value="x",
        )

    def span(start: tuple[object, object], end: tuple[object, object]) -> dict[str, object]:
        return {
            "start": {"line": start[0], "character": start[1]},
            "end": {"line": end[0], "character": end[1]},
        }

    good = _internal_location(uri)
    assert classify(good).classification == REPO_INTERNAL
    malformed: dict[str, object] = {
        "missing range": {"uri": uri},
        "incomplete range": {"uri": uri, "range": {"start": {"line": 0, "character": 4}}},
        "negative position": {"uri": uri, "range": span((-1, 0), (0, 3))},
        "boolean position": {"uri": uri, "range": span((True, 0), (1, 3))},
        "reversed range": {"uri": uri, "range": span((1, 4), (0, 3))},
        "malformed sibling": [good, {"garbage": True}],
        "non-object sibling": [good, "not-a-location"],
        "link without ranges": [{"targetUri": uri}],
    }
    for name, payload in malformed.items():
        result = classify(payload)
        assert result.classification == UNRESOLVED, name
        assert result.target_uri is None, name


def test_absolute_drive_and_parent_paths_are_never_workspace_members(tmp_path: Path) -> None:
    for hostile in (
        "/srv/repo/src/mod.py",
        "//server/share/mod.py",
        "C:/repo/mod.py",
        "C:\\repo\\mod.py",
        "C:mod.py",
        "../outside.py",
        "src/../../outside.py",
        "",
    ):
        assert path_excluded_from_workspace(hostile), hostile
    repo = tmp_path / "repo"
    payload = b"x = 1\n"
    digest = _write(repo, "src/mod.py", payload)
    absolute = (repo / "src" / "mod.py").resolve().as_posix()
    workspace = build_bounded_workspace(
        repo,
        [IndexedSource(absolute, digest)],
        tmp_path / "workspace",
    )
    assert workspace.included_relative_paths == ()
    assert workspace.excluded_relative_paths == (absolute,)
    assert (repo / "src" / "mod.py").read_bytes() == payload


def test_indexed_symlink_into_excluded_directory_is_not_copied(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    digest = _write(repo, ".aiworkhub/state/secret.py", b"SECRET = 1\n")
    (repo / "src").mkdir()
    _symlink(repo / "src" / "link.py", repo / ".aiworkhub" / "state" / "secret.py")
    dest = tmp_path / "workspace"
    workspace = build_bounded_workspace(repo, [IndexedSource("src/link.py", digest)], dest)
    assert workspace.included_relative_paths == ()
    assert workspace.excluded_relative_paths == ("src/link.py",)
    assert not (dest / "src").exists()


def test_repository_pyrightconfig_never_replaces_the_generated_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    own_digest = _write(repo, "pyrightconfig.json", b'{"include": ["/abs"]}\n')
    mod_digest = _write(repo, "src/mod.py", b"x = 1\n")
    workspace = build_bounded_workspace(
        repo,
        [
            IndexedSource("pyrightconfig.json", own_digest),
            IndexedSource("src/mod.py", mod_digest),
        ],
        tmp_path / "workspace",
    )
    assert workspace.included_relative_paths == ("src/mod.py",)
    assert workspace.excluded_relative_paths == ("pyrightconfig.json",)
    assert workspace.config["include"] == ["src"]
    written = json.loads(workspace.config_path.read_text(encoding="utf-8"))
    assert written["include"] == ["src"]


def test_server_never_opens_a_symlinked_workspace_file(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    uri = (workspace.root / "src" / "mod.py").resolve().as_uri()
    outside = tmp_path / "outside.py"
    outside.write_bytes(source)
    copied = workspace.root / "src" / "mod.py"
    copied.unlink()
    _symlink(copied, outside)
    log_path = tmp_path / "lsp.log"
    scenario = _write_scenario(tmp_path / "scenario.json", {"result": _internal_location(uri)})
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        batch_timeout_s=4.0,
        env=_env(scenario, log_path),
    )
    assert outcome.results[0].classification == UNRESOLVED
    methods = json.loads(log_path.read_text(encoding="utf-8"))
    assert "textDocument/didOpen" not in methods
    assert "textDocument/definition" not in methods
    assert outcome.children_reaped


def test_definition_batch_defaults_to_verified_workspace_hashes(tmp_path: Path) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    uri = (workspace.root / "src" / "mod.py").resolve().as_uri()
    scenario = _write_scenario(tmp_path / "scenario.json", {"result": _internal_location(uri)})
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        request_timeout_s=2.0,
        batch_timeout_s=4.0,
        env=_env(scenario),
    )
    assert outcome.results[0].classification == REPO_INTERNAL


def test_server_request_with_a_colliding_id_is_not_a_definition_response(
    tmp_path: Path,
) -> None:
    source = b"def foo():\n    return foo()\n"
    repo, workspace, hashes = _workspace(tmp_path, [("src/mod.py", source)])
    uri = (workspace.root / "src" / "mod.py").resolve().as_uri()
    scenario = _write_scenario(tmp_path / "collision.json", {
        "request_id_collision": True,
        "result": _internal_location(uri),
    })
    outcome = resolve_definitions(
        repo_root=repo,
        workspace=workspace,
        spec=_spec(scenario),
        queries=[DefinitionQuery("src/mod.py", hashes["src/mod.py"], 2, 11)],
        indexed_hashes=hashes,
        request_timeout_s=2.0,
        batch_timeout_s=4.0,
        env=_env(scenario),
    )
    assert outcome.results[0].classification == REPO_INTERNAL
    assert outcome.results[0].target_uri == uri


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/usr/lib/python3.12/os.py", EXTERNAL_STDLIB),
        ("/usr/lib64/python3.12/json/decoder.py", EXTERNAL_STDLIB),
        ("/usr/local/lib/python3.13t/typing.py", EXTERNAL_STDLIB),
        ("/usr/lib/python3.12/typeshed/stdlib/os.pyi", EXTERNAL_STDLIB),
        ("/srv/node_modules/pyright/dist/typeshed-fallback/stdlib/os/__init__.pyi", EXTERNAL_STDLIB),
        ("C:/Python312/Lib/os.py", EXTERNAL_STDLIB),
        ("C:\\Python312\\Lib\\os.py", EXTERNAL_STDLIB),
        ("c:/users/dev/miniconda3/envs/x/lib/json/decoder.py", EXTERNAL_STDLIB),
        ("//server/share/Python312/Lib/os.py", EXTERNAL_STDLIB),
        ("/mnt/c/Python312/Lib/os.py", EXTERNAL_STDLIB),
        ("/usr/lib/python3.12/site-packages/requests/api.py", EXTERNAL_DEPENDENCY),
        ("/usr/lib/python3/dist-packages/requests/api.py", EXTERNAL_DEPENDENCY),
        ("C:/Python312/Lib/site-packages/requests/api.py", EXTERNAL_DEPENDENCY),
        ("/mnt/c/Python312/Lib/site-packages/requests/api.py", EXTERNAL_DEPENDENCY),
        ("C:\\proj\\.venv\\Lib\\site-packages\\requests\\api.py", EXTERNAL_DEPENDENCY),
        ("/srv/typeshed-fallback/stubs/requests/requests/api.pyi", EXTERNAL_DEPENDENCY),
        ("/home/dev/mylib/python_projects/util.py", EXTERNAL_DEPENDENCY),
        ("/usr/lib/other/util.py", EXTERNAL_DEPENDENCY),
    ],
)
def test_external_layout_classification_is_platform_independent(
    path: str,
    expected: str,
) -> None:
    from aiworkhub.source_graph_lsp import _external_classification

    assert _external_classification(path) == expected


def test_posix_capital_lib_directory_is_external_dependency() -> None:
    from aiworkhub.source_graph_lsp import _external_classification
    from pathlib import PurePosixPath

    assert (
        _external_classification(PurePosixPath("/opt/Lib/requests/__init__.py"))
        == EXTERNAL_DEPENDENCY
    )


def test_windows_layout_classifies_from_any_host_path_flavour() -> None:
    from aiworkhub.source_graph_lsp import _external_classification

    stdlib = "C:/Python312/Lib/os.py"
    dependency = "C:/Python312/Lib/site-packages/requests/api.py"
    for flavour in (PureWindowsPath, Path):
        assert _external_classification(flavour(stdlib)) == EXTERNAL_STDLIB
        assert _external_classification(flavour(dependency)) == EXTERNAL_DEPENDENCY


def test_windows_layout_real_files_are_classified_on_any_host(tmp_path: Path) -> None:
    classify = _target_classifier(tmp_path)
    stdlib = _external_file(tmp_path, "Python312", "Lib", "os.py")
    package_stdlib = _external_file(tmp_path, "Python312", "Lib", "json", "decoder.py")
    dependency = _external_file(
        tmp_path, "Python312", "Lib", "site-packages", "requests", "api.py",
    )
    assert classify(stdlib.as_uri()).classification == EXTERNAL_STDLIB
    assert classify(package_stdlib.as_uri()).classification == EXTERNAL_STDLIB
    assert classify(dependency.as_uri()).classification == EXTERNAL_DEPENDENCY


def test_missing_target_is_unresolved_not_external_evidence(tmp_path: Path) -> None:
    classify = _target_classifier(tmp_path)
    directory = tmp_path / "external" / "pkg"
    directory.mkdir(parents=True)
    for uri in (
        (tmp_path / "external" / "usr" / "lib" / "python3.12" / "os.py").as_uri(),
        (tmp_path / "external" / "site-packages" / "requests" / "api.py").as_uri(),
        "file:///nonexistent/dir/foo.py",
        directory.as_uri(),
    ):
        result = classify(uri)
        assert result.classification == UNRESOLVED, uri
        assert result.target_uri == uri


def test_foreign_platform_uri_is_unresolved_and_ignores_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("a drive-letter URI is native on Windows")
    classify = _target_classifier(tmp_path)
    decoy = tmp_path / "cwd" / "C:" / "Python312" / "Lib" / "os.py"
    decoy.parent.mkdir(parents=True)
    decoy.write_bytes(b"pass\n")
    monkeypatch.chdir(tmp_path / "cwd")
    result = classify("file:///C:/Python312/Lib/os.py")
    assert result.classification == UNRESOLVED


def _run_fake_lsp_server() -> int:
    scenario_path = os.environ.get("AIWORKHUB_FAKE_LSP_SCENARIO", "")
    scenario: dict[str, object] = {}
    if scenario_path:
        scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
    log_path = os.environ.get("AIWORKHUB_FAKE_LSP_LOG")
    position_log = os.environ.get("AIWORKHUB_FAKE_LSP_POSITION_LOG")
    methods: list[str] = []
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    pending: list[dict[str, object]] = []

    def read_message() -> dict[str, object] | None:
        header = b""
        while b"\r\n\r\n" not in header:
            byte = stdin.read(1)
            if not byte:
                return None
            header += byte
        fields = {}
        for line in header.decode("ascii").split("\r\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip().casefold()] = value.strip()
        length = int(fields["content-length"])
        body = stdin.read(length)
        return json.loads(body.decode("utf-8"))

    def write_message(payload: dict[str, object]) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        stdout.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
        stdout.flush()

    def definition_result() -> object:
        queued = scenario.get("results")
        if isinstance(queued, list) and queued:
            return queued.pop(0)
        return scenario.get("result")

    while True:
        message = read_message()
        if message is None:
            break
        method = str(message.get("method") or "")
        if method:
            methods.append(method)
            if log_path:
                Path(log_path).write_text(json.dumps(methods), encoding="utf-8")
        if method == "initialize":
            write_message({
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "capabilities": {
                        "textDocumentSync": 1,
                        "definitionProvider": True,
                        "positionEncoding": scenario.get("positionEncoding", DEFAULT_POSITION_ENCODING),
                    },
                    "serverInfo": {"name": "fake-lsp", "version": "1.0"},
                },
            })
            if scenario.get("stall_stdin_after_initialize"):
                time.sleep(30)
            continue
        if method == "initialized":
            continue
        if method == "textDocument/didOpen":
            if scenario.get("publish_diagnostics"):
                params = message.get("params")
                uri = ""
                if isinstance(params, dict):
                    document = params.get("textDocument")
                    if isinstance(document, dict):
                        uri = str(document.get("uri") or "")
                write_message({
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {"uri": uri, "diagnostics": [{"message": "n", "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 1},
                    }, "severity": 1}]},
                })
            continue
        if method == "textDocument/definition":
            if position_log:
                params = message.get("params")
                if isinstance(params, dict):
                    Path(position_log).write_text(
                        json.dumps(params.get("position")), encoding="utf-8"
                    )
            if scenario.get("hang"):
                time.sleep(30)
            if scenario.get("malformed"):
                stdout.write(b"not-a-valid-header\r\n\r\n{}")
                stdout.flush()
                continue
            if scenario.get("huge"):
                raw = b"{" + (b"x" * 128) + b"}"
                stdout.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
                stdout.flush()
                continue
            if scenario.get("extra_notification"):
                write_message({
                    "jsonrpc": "2.0",
                    "method": "window/logMessage",
                    "params": {"type": 3, "message": "ignore-me"},
                })
            if scenario.get("request_id_collision"):
                write_message({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "method": "workspace/configuration",
                    "params": {"items": []},
                })
            defer = int(scenario.get("defer_definitions") or 0)
            if defer:
                pending.append(message)
                if len(pending) >= defer:
                    for item in reversed(pending):
                        write_message({
                            "jsonrpc": "2.0",
                            "id": item["id"],
                            "result": definition_result(),
                        })
                    pending.clear()
                continue
            write_message({
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": definition_result(),
            })
            continue
        if method == "shutdown":
            write_message({"jsonrpc": "2.0", "id": message["id"], "result": None})
            continue
        if method == "exit":
            break
    if log_path:
        Path(log_path).write_text(json.dumps(methods), encoding="utf-8")
    return 0


if __name__ == "__main__":
    if FAKE_LSP_FLAG in sys.argv:
        raise SystemExit(_run_fake_lsp_server())
    raise SystemExit("fake LSP server requires --fake-lsp")
