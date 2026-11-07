"""NF-2026-02154: one shared request.json size bound, enforced at write time."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import (
    capped_continuation,
    crash_retry_packet,
    request_metadata_limits,
    worker_validation_reuse,
    worker_workspace,
)
from aiworkhub.request_metadata_limits import (
    REQUEST_METADATA_MAX_BYTES,
    check_request_metadata_bytes,
    check_request_metadata_payload,
)
from aiworkhub.worker_workspace import WorkspaceError

SRC = Path(__file__).resolve().parents[1] / "src" / "aiworkhub"
PREDECESSOR_ID = "a" * 32
SUCCESSOR_ID = "b" * 32
TASK_ID = "needfix-NF-2026-02154"
FIELD_REPORT_BYTES = 2_500_000

# Every reader of ``{request_id}.request.json`` and how many size checks it holds.
READER_SITES = {
    "crash_retry_packet.py": 1,
    "process_launcher.py": 2,
    "process_launcher_accept_review.py": 2,
    "process_launcher_vscode_lm_tool.py": 1,
}
METADATA_PATH_NAMES = {"metadata_path", "reviewer_metadata_path"}


def _size_comparisons(source: str, names: set[str]) -> list[ast.Compare]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if (
            isinstance(left, ast.Attribute)
            and left.attr == "st_size"
            and isinstance(left.value, ast.Call)
            and isinstance(left.value.func, ast.Attribute)
            and left.value.func.attr == "stat"
            and isinstance(left.value.func.value, ast.Name)
            and left.value.func.value.id in names
        ):
            found.append(node)
    return found


def _predecessor(tmp_path: Path, monkeypatch, *, metadata_pad: int, status_pad: int = 0):
    monkeypatch.setattr(
        capped_continuation,
        "launch_predecessor_task_id",
        lambda card, task_id, authority_repo=None: task_id,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    metadata = {
        "request_id": PREDECESSOR_ID,
        "task_id": TASK_ID,
        "workspace": {"repo": str(repo)},
        "padding": "x" * metadata_pad,
    }
    metadata_path = process_dir / f"{PREDECESSOR_ID}.request.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    status = {"state": "failed", "exit_code": 1, "error": "boom", "pad": "y" * status_pad}
    (process_dir / f"{PREDECESSOR_ID}.supervisor.json").write_text(
        json.dumps(status), encoding="utf-8"
    )
    workspace = SimpleNamespace(
        repo=repo,
        request_id=SUCCESSOR_ID,
        home=tmp_path / "home",
        inherited_rework_paths=(),
    )
    card = {"rework_predecessor": {"request_id": PREDECESSOR_ID, "task_id": TASK_ID}}
    overlay = {
        "predecessor_request_id": PREDECESSOR_ID,
        "predecessor_task_id": TASK_ID,
        "canonical_digest": "0" * 64,
    }
    written: dict[Path, object] = {}

    def run():
        return crash_retry_packet.materialize_crash_retry_packet(
            process_dir,
            workspace,
            task_id=TASK_ID,
            card=card,
            rework_overlay_packet=overlay,
            safe_tail=lambda path, limit: "",
            write_json_0600=lambda path, payload: written.__setitem__(path, payload),
        )

    return metadata_path, run, written


def test_ceiling_is_64_mib():
    assert REQUEST_METADATA_MAX_BYTES == 64 * 1024 * 1024


def test_crash_retry_reads_a_field_report_sized_request_json(tmp_path, monkeypatch):
    metadata_path, run, written = _predecessor(
        tmp_path, monkeypatch, metadata_pad=FIELD_REPORT_BYTES
    )
    assert not metadata_path.is_symlink()
    assert metadata_path.stat().st_size > 2 * 1024 * 1024

    path, packet = run()

    assert packet is not None and path is not None
    assert packet["predecessor_request_id"] == PREDECESSOR_ID
    assert packet["predecessor_state"] == "failed"
    assert written == {path: packet}


def test_crash_retry_still_refuses_request_json_over_the_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(crash_retry_packet, "REQUEST_METADATA_MAX_BYTES", 1024)
    _, run, written = _predecessor(tmp_path, monkeypatch, metadata_pad=4096)

    with pytest.raises(WorkspaceError, match="^crash_retry_predecessor_artifact_unsafe$"):
        run()
    assert written == {}


def test_crash_retry_supervisor_json_keeps_its_one_mib_cap(tmp_path, monkeypatch):
    _, run, _ = _predecessor(
        tmp_path, monkeypatch, metadata_pad=16, status_pad=1024 * 1024 + 1
    )

    with pytest.raises(WorkspaceError, match="^crash_retry_predecessor_artifact_unsafe$"):
        run()


def test_validation_reuse_reads_a_field_report_sized_request_json(tmp_path):
    assert worker_validation_reuse.MAX_METADATA_BYTES is REQUEST_METADATA_MAX_BYTES
    path = tmp_path / f"{PREDECESSOR_ID}.request.json"
    path.write_bytes(b"{}" + b" " * FIELD_REPORT_BYTES)

    raw = worker_validation_reuse._read_bounded(path, worker_validation_reuse.MAX_METADATA_BYTES)

    assert raw is not None and len(raw) == FIELD_REPORT_BYTES + 2


def test_check_bytes_names_size_and_cap(monkeypatch):
    monkeypatch.setattr(request_metadata_limits, "REQUEST_METADATA_MAX_BYTES", 64)

    check_request_metadata_bytes(64)
    with pytest.raises(WorkspaceError) as excinfo:
        check_request_metadata_bytes(65)
    assert str(excinfo.value) == "request_metadata_too_large:65>64"


def test_writer_check_refuses_oversize_payload_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(request_metadata_limits, "REQUEST_METADATA_MAX_BYTES", 64)
    payload = {"request_id": PREDECESSOR_ID, "padding": "é" * 40}
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    target = tmp_path / f"{PREDECESSOR_ID}.request.json"

    def launch_writer_sequence() -> None:
        check_request_metadata_payload(payload)
        target.write_bytes(encoded)

    with pytest.raises(WorkspaceError) as excinfo:
        launch_writer_sequence()
    assert str(excinfo.value) == f"request_metadata_too_large:{len(encoded)}>64"
    assert not target.exists()

    check_request_metadata_payload({"a": 1})


def test_payload_check_measures_exactly_what_write_json_0600_writes():
    encoding = 'json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\\n"'
    assert encoding in inspect.getsource(worker_workspace.write_json_0600)
    assert encoding in inspect.getsource(check_request_metadata_payload)


def test_launch_writer_checks_immediately_before_writing_request_json():
    tree = ast.parse((SRC / "process_launcher_launch_isolated.py").read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for index, stmt in enumerate(body):
            if ast.unparse(stmt) == "write_json_0600(metadata_path, metadata)":
                hits.append((body, index))
    assert len(hits) == 1
    body, index = hits[0]
    assert ast.unparse(body[index - 1]) == "check_request_metadata_payload(metadata)"


@pytest.mark.parametrize("filename,expected", sorted(READER_SITES.items()))
def test_no_reader_compares_request_json_size_to_a_literal(filename, expected):
    source = (SRC / filename).read_text(encoding="utf-8")
    comparisons = _size_comparisons(source, METADATA_PATH_NAMES)

    assert len(comparisons) == expected
    for node in comparisons:
        for comparator in node.comparators:
            assert ast.unparse(comparator) not in {"1024 * 1024", "2 * 1024 * 1024"}
            assert isinstance(comparator, ast.Name)
            assert comparator.id == "REQUEST_METADATA_MAX_BYTES"


def test_validation_reuse_metadata_bound_is_the_shared_constant():
    tree = ast.parse((SRC / "worker_validation_reuse.py").read_text(encoding="utf-8"))
    assigned = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "MAX_METADATA_BYTES" for t in node.targets)
    ]
    assert [ast.unparse(value) for value in assigned] == ["REQUEST_METADATA_MAX_BYTES"]


def test_crash_retry_supervisor_json_cap_is_unchanged_in_source():
    source = (SRC / "crash_retry_packet.py").read_text(encoding="utf-8")
    comparisons = _size_comparisons(source, {"status_path"})

    assert [ast.unparse(c) for node in comparisons for c in node.comparators] == [
        "1024 * 1024"
    ]
