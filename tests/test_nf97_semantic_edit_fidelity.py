from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from aiworkhub import vscode_lm_worker as worker


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _executable_text(size: int = 111) -> str:
    prefix = "def compute(value):\n    total = value + 1\n    return total\n"
    assert len(prefix.encode("utf-8")) < size
    return prefix + "#" + ("x" * (size - len(prefix.encode("utf-8")) - 1))


def _nested_fence_payload(body: str, depth: int = 8) -> str:
    for index in range(depth):
        fence = "```" if index % 2 == 0 else "~~~"
        body = f"{fence}\n{body}\n{fence}"
    return body


def _assert_rejected(
    reason: str,
    payload: str,
    *,
    old: str | None = None,
    path: str = "src/app.py",
    operation: str = "v3_range:0",
) -> None:
    expected = f"vscode_lm_edit_fidelity_rejected:{reason}:{path}:{operation}"
    with pytest.raises(RuntimeError) as caught:
        worker._check_edit_fidelity(
            _executable_text() if old is None else old,
            payload,
            path=path,
            operation=operation,
        )
    assert str(caught.value) == expected


@pytest.mark.parametrize("payload", ["...", "…"])
def test_authenticated_111_byte_executable_to_ellipsis_is_rejected(payload: str) -> None:
    old = _executable_text()
    assert len(old.encode("utf-8")) == 111
    _assert_rejected("ellipsis_only", payload, old=old)


@pytest.mark.parametrize(
    "payload",
    [
        "replacement code only",
        "file content",
        "TODO",
        "# FIXME: implement this",
        "implementation omitted",
        "code omitted for brevity",
        "<new validate_required_outputs code>",
    ],
)
def test_placeholder_dominant_payloads_have_stable_diagnostic(payload: str) -> None:
    _assert_rejected("placeholder_phrase", payload)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("```text\nreplacement code only\n```", "placeholder_phrase"),
        ("```\n...\n```", "ellipsis_only"),
        ("```\nreplacement code only\n````", "placeholder_phrase"),
        ("```python title=generated replacement\nfile content\n```", "placeholder_phrase"),
        ("~~~text title=generated replacement\nTODO\n~~~~", "placeholder_phrase"),
        ("```text\nreplacement code only\n   ```", "placeholder_phrase"),
        ("~~~text\nfile content\n  ~~~", "placeholder_phrase"),
        ("   ```text\nreplacement code only\n   ````", "placeholder_phrase"),
        ("  ~~~text\nfile content\n ~~~~", "placeholder_phrase"),
        ("```text\r\nreplacement code only\r\n   ```", "placeholder_phrase"),
        (_nested_fence_payload("replacement code only"), "placeholder_phrase"),
        ("/* file content */", "placeholder_phrase"),
        ("<!-- implementation omitted -->", "placeholder_phrase"),
        ("/// FIXME: implement this", "placeholder_phrase"),
        ("// …", "ellipsis_only"),
        ("．．．", "ellipsis_only"),
        ("ｒｅｐｌａｃｅｍｅｎｔ　ｃｏｄｅ　ｏｎｌｙ", "placeholder_phrase"),
    ],
)
def test_wrapped_and_nfkc_placeholders_reject_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
    reason: str,
) -> None:
    old = _executable_text()
    target = tmp_path / "app.py"
    target.write_bytes(old.encode("utf-8"))
    baseline = hashlib.sha256(target.read_bytes()).hexdigest()
    changed_paths: list[str] = []
    real_write = worker._write_atomic

    def recording_write(workspace: Path, relative: str, content: str) -> None:
        changed_paths.append(relative)
        real_write(workspace, relative, content)

    monkeypatch.setattr(worker, "_write_atomic", recording_write)

    def plan_then_apply() -> None:
        planned, _metrics = worker._v3_planned_outputs(
            tmp_path,
            {
                "edits": [{
                    "path": "app.py",
                    "current_sha256": _sha(old),
                    "ranges": [{"start_line": 1, "end_line": 4, "new": payload}],
                }],
                "creates": [],
            },
            ["app.py"],
        )
        for relative, content in planned:
            worker._write_atomic(tmp_path, relative, content)

    with pytest.raises(RuntimeError, match=rf"{reason}:app.py:v3_range:0"):
        plan_then_apply()
    assert changed_paths == []
    assert hashlib.sha256(target.read_bytes()).hexdigest() == baseline


def test_pass_only_and_destructive_prose_shrink_have_distinct_reasons() -> None:
    _assert_rejected("pass_only_nontrivial_replacement", "pass", old="return value\n")
    _assert_rejected("destructive_non_code_shrink", "Implemented requested behavior.")


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_v1_v2_v3_planners_reject_authenticated_placeholder(
    tmp_path: Path,
    version: str,
) -> None:
    old = _executable_text()
    target = tmp_path / "app.py"
    target.write_bytes(old.encode("utf-8"))
    if version == "v1":
        call = lambda: worker._v1_planned_outputs(  # noqa: E731
            tmp_path,
            {"files": [{"path": "app.py", "content": "file content"}]},
            ["app.py"],
        )
        operation = "v1_file"
    elif version == "v2":
        call = lambda: worker._v2_planned_outputs(  # noqa: E731
            tmp_path,
            {
                "edits": [{
                    "path": "app.py",
                    "current_sha256": _sha(old),
                    "replacements": [{
                        "old": old,
                        "new": "replacement code only",
                        "expected_count": 1,
                    }],
                }],
                "creates": [],
            },
            ["app.py"],
        )
        operation = "v2_replacement:0"
    else:
        call = lambda: worker._v3_planned_outputs(  # noqa: E731
            tmp_path,
            {
                "edits": [{
                    "path": "app.py",
                    "current_sha256": _sha(old),
                    "ranges": [{"start_line": 1, "end_line": 4, "new": "..."}],
                }],
                "creates": [],
            },
            ["app.py"],
        )
        operation = "v3_range:0"
    expected_reason = "ellipsis_only" if version == "v3" else "placeholder_phrase"
    with pytest.raises(
        RuntimeError,
        match=rf"^{re.escape('vscode_lm_edit_fidelity_rejected:' + expected_reason + ':app.py:' + operation)}$",
    ):
        call()


@pytest.mark.parametrize(
    ("planner", "operation"),
    [
        ("v1", "v1_file"),
        ("v2", "v2_create"),
        ("v3", "v3_create"),
    ],
)
def test_missing_required_create_is_rejected(
    tmp_path: Path,
    planner: str,
    operation: str,
) -> None:
    if planner == "v1":
        call = lambda: worker._v1_planned_outputs(  # noqa: E731
            tmp_path, {"files": []}, ["new.py"], {"new.py"}
        )
    elif planner == "v2":
        call = lambda: worker._v2_planned_outputs(  # noqa: E731
            tmp_path, {"edits": [], "creates": []}, ["new.py"], {"new.py"}
        )
    else:
        call = lambda: worker._v3_planned_outputs(  # noqa: E731
            tmp_path, {"edits": [], "creates": []}, ["new.py"], {"new.py"}
        )
    with pytest.raises(RuntimeError) as caught:
        call()
    assert str(caught.value) == (
        f"vscode_lm_edit_fidelity_rejected:missing_required_create:new.py:{operation}"
    )


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (" \r\n\t", "empty_required_create"),
        ("...", "ellipsis_only"),
        ("…", "ellipsis_only"),
        ("．．．", "ellipsis_only"),
        ("```python title=generated stub\n\n````", "empty_required_create"),
        ("~~~text title=generated stub\n\n~~~~", "empty_required_create"),
    ],
)
def test_required_pyi_create_rejects_ellipsis_and_wrapped_empty_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    payload: str,
    reason: str,
) -> None:
    anchor = tmp_path / "anchor.py"
    anchor.write_text("VALUE = 1\n", encoding="utf-8")
    baseline = hashlib.sha256(anchor.read_bytes()).hexdigest()
    changed_paths: list[str] = []
    real_write = worker._write_atomic

    def recording_write(workspace: Path, relative: str, content: str) -> None:
        changed_paths.append(relative)
        real_write(workspace, relative, content)

    monkeypatch.setattr(worker, "_write_atomic", recording_write)
    if version == "v1":
        call = lambda: worker._v1_planned_outputs(  # noqa: E731
            tmp_path,
            {"files": [{"path": "new.pyi", "content": payload}]},
            ["new.pyi"],
            {"new.pyi"},
        )
        operation = "v1_file"
    elif version == "v2":
        call = lambda: worker._v2_planned_outputs(  # noqa: E731
            tmp_path,
            {"edits": [], "creates": [{"path": "new.pyi", "content": payload}]},
            ["new.pyi"],
            {"new.pyi"},
        )
        operation = "v2_create"
    else:
        call = lambda: worker._v3_planned_outputs(  # noqa: E731
            tmp_path,
            {"edits": [], "creates": [{"path": "new.pyi", "content": payload}]},
            ["new.pyi"],
            {"new.pyi"},
        )
        operation = "v3_create"

    def plan_then_apply() -> None:
        planned, _metrics = call()
        for relative, content in planned:
            worker._write_atomic(tmp_path, relative, content)

    with pytest.raises(RuntimeError) as caught:
        plan_then_apply()
    assert str(caught.value) == (
        f"vscode_lm_edit_fidelity_rejected:{reason}:new.pyi:{operation}"
    )
    assert changed_paths == []
    assert hashlib.sha256(anchor.read_bytes()).hexdigest() == baseline
    assert not (tmp_path / "new.pyi").exists()


def test_v3_required_create_range_rejects_ellipsis_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "new.py"
    target.write_bytes(b"")
    changed_paths: list[str] = []
    real_write = worker._write_atomic

    def recording_write(workspace: Path, relative: str, content: str) -> None:
        changed_paths.append(relative)
        real_write(workspace, relative, content)

    monkeypatch.setattr(worker, "_write_atomic", recording_write)
    with pytest.raises(
        RuntimeError,
        match=r"ellipsis_only:new\.py:v3_range:0$",
    ):
        worker._v3_planned_outputs(
            tmp_path,
            {
                "edits": [{
                    "path": "new.py",
                    "current_sha256": _sha(""),
                    "ranges": [{"start_line": 1, "end_line": 1, "new": "..."}],
                }],
                "creates": [],
            },
            ["new.py"],
            {"new.py"},
        )
    assert changed_paths == []
    assert target.read_bytes() == b""


def test_retained_ellipsis_placeholder_cannot_validate_as_unchanged_stub() -> None:
    with pytest.raises(
        RuntimeError,
        match=r"ellipsis_only:new\.py:v3_range:0$",
    ):
        worker._check_edit_fidelity(
            "...",
            "...",
            path="new.py",
            operation="v3_range:0",
        )


def test_late_rejection_preflights_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _executable_text()
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_bytes(old.encode("utf-8"))
    second.write_bytes(old.encode("utf-8"))
    baselines = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)}
    changed_paths: list[str] = []
    real_write = worker._write_atomic

    def recording_write(workspace: Path, relative: str, content: str) -> None:
        changed_paths.append(relative)
        real_write(workspace, relative, content)

    monkeypatch.setattr(worker, "_write_atomic", recording_write)

    def plan_then_apply() -> None:
        planned, _metrics = worker._v3_planned_outputs(
            tmp_path,
            {
                "edits": [
                    {
                        "path": "first.py",
                        "current_sha256": _sha(old),
                        "ranges": [{
                            "start_line": 1,
                            "end_line": 4,
                            "new": "def compute(value):\n    return value + 2\n",
                        }],
                    },
                    {
                        "path": "second.py",
                        "current_sha256": _sha(old),
                        "ranges": [{"start_line": 1, "end_line": 4, "new": "file content"}],
                    },
                ],
                "creates": [],
            },
            ["*.py"],
        )
        for relative, content in planned:
            worker._write_atomic(tmp_path, relative, content)

    with pytest.raises(RuntimeError, match="placeholder_phrase:second.py:v3_range:0"):
        plan_then_apply()
    assert changed_paths == []
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)} == baselines


@pytest.mark.parametrize(
    ("old", "new", "path"),
    [
        (_executable_text(), "return value + 2", "src/app.py"),
        (_executable_text(), 'note = "TODO"\n# FIXME documents a supported token\nreturn note', "src/app.py"),
        (_executable_text(), "value = ...", "src/app.py"),
        ("def declared(value: int) -> int: ...\n", "...", "src/api.pyi"),
        ("pass\n", "...", "src/stub.py"),
        ("def pending():\n    pass\n", "...", "src/stub.py"),
        ("@abstractmethod\ndef run(self):\n    raise NotImplementedError\n", "...", "src/base.py"),
        ("@overload\ndef parse(value: str) -> str: ...\n", "...", "src/api.py"),
        ("class Reader(Protocol):\n    def read(self): ...\n", "...", "src/api.py"),
    ],
)
def test_legitimate_concise_code_and_stub_contexts_are_accepted(
    old: str,
    new: str,
    path: str,
) -> None:
    receipt = worker._check_edit_fidelity(
        old,
        new,
        path=path,
        operation="v3_range:0",
    )
    assert receipt["new_bytes"] == len(new.encode("utf-8"))


@pytest.mark.parametrize(
    "old",
    [
        "# @abstractmethod Protocol overload\nreturn value\n",
        '"""@abstractmethod Protocol overload"""\nreturn value\n',
    ],
)
def test_stub_keywords_in_comments_or_strings_do_not_grant_exemption(old: str) -> None:
    _assert_rejected("ellipsis_only", "...", old=old)


@pytest.mark.parametrize(
    "old",
    [
        "from abc import abstractmethod\n@abstractmethod\ndef run(self):\n    return 1\n",
        "from typing import overload\n@overload\ndef parse(value: str) -> str: ...\n",
        "from typing import Protocol\nclass Reader(Protocol):\n    def read(self): ...\n",
        "def pending():\n    pass\n",
    ],
)
def test_actual_python_stub_context_accepts_plain_ellipsis_in_v3_plan(
    tmp_path: Path,
    old: str,
) -> None:
    target = tmp_path / "stub.py"
    target.write_bytes(old.encode("utf-8"))
    planned, _metrics = worker._v3_planned_outputs(
        tmp_path,
        {
            "edits": [{
                "path": "stub.py",
                "current_sha256": _sha(old),
                "ranges": [{
                    "start_line": 1,
                    "end_line": len(old.splitlines()),
                    "new": "...",
                }],
            }],
            "creates": [],
        },
        ["stub.py"],
    )
    assert planned == [("stub.py", "...\n")]


@pytest.mark.parametrize(
    "new",
    [
        "```python title=reviewed replacement\n# TODO is a documented token\nreturn 2\n   ````",
        "  ~~~python title=reviewed replacement\n# TODO is a documented token\nreturn 2\n ~~~~",
        "   ```python title=reviewed replacement\r\n# TODO is a documented token\r\nreturn 2\r\n  ```",
    ],
)
def test_substantive_fenced_code_with_todo_comment_is_not_placeholder(new: str) -> None:
    receipt = worker._check_edit_fidelity(
        _executable_text(),
        new,
        path="src/app.py",
        operation="v3_range:0",
    )
    assert receipt["new_bytes"] == len(new.encode("utf-8"))


@pytest.mark.parametrize(
    "new",
    [
        "    ```text\nreplacement code only\n```",
        "```text\nreplacement code only\n    ```",
        "```text\nreplacement code only\n~~~",
        "````text\nreplacement code only\n```",
    ],
)
def test_non_commonmark_fence_shapes_are_not_unwrapped(new: str) -> None:
    candidate, wrapped, _unicode_ellipsis, _compatibility_changed = (
        worker._classification_payload(new)
    )
    assert wrapped is False
    assert "replacement code only" in candidate
    receipt = worker._check_edit_fidelity(
        _executable_text(),
        new,
        path="src/app.py",
        operation="v3_range:0",
    )
    assert receipt["new_bytes"] == len(new.encode("utf-8"))


def test_outer_comment_wrapper_preserves_four_space_inner_fence() -> None:
    new = "<!--\n    ```text\nreplacement code only\n```\n-->"
    candidate, wrapped, _unicode_ellipsis, _compatibility_changed = (
        worker._classification_payload(new)
    )
    assert wrapped is True
    assert candidate.startswith("```text")
    assert "replacement code only" in candidate
    receipt = worker._check_edit_fidelity(
        _executable_text(),
        new,
        path="src/app.py",
        operation="v3_range:0",
    )
    assert receipt["new_bytes"] == len(new.encode("utf-8"))


def test_substantive_code_with_inline_fence_text_is_not_unwrapped() -> None:
    new = 'marker = "```not a whole fence```"\nreturn marker\n'
    receipt = worker._check_edit_fidelity(
        _executable_text(),
        new,
        path="src/app.py",
        operation="v3_range:0",
    )
    assert receipt["new_bytes"] == len(new.encode("utf-8"))


def test_valid_multi_file_edit_and_required_create_plan(tmp_path: Path) -> None:
    old_a = "def a():\n    return 1\n"
    old_b = "def b():\n    return 2\n"
    (tmp_path / "a.py").write_bytes(old_a.encode("utf-8"))
    (tmp_path / "b.py").write_bytes(old_b.encode("utf-8"))
    planned, metrics = worker._v3_planned_outputs(
        tmp_path,
        {
            "edits": [
                {
                    "path": "a.py",
                    "current_sha256": _sha(old_a),
                    "ranges": [{"start_line": 1, "end_line": 2, "new": "def a():\n    return 10\n"}],
                },
                {
                    "path": "b.py",
                    "current_sha256": _sha(old_b),
                    "ranges": [{"start_line": 1, "end_line": 2, "new": "def b():\n    return 20\n"}],
                },
            ],
            "creates": [{"path": "new.py", "content": "VALUE = 30\n"}],
        },
        ["*.py"],
        {"new.py"},
    )
    assert [path for path, _content in planned] == ["a.py", "b.py", "new.py"]
    assert len(metrics) == 3
