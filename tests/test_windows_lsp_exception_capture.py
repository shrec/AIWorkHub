"""Compact Windows LSP exception-capture diagnostic (NF980).

Loads the unchanged reference fixture module
``tests/test_source_graph_lsp_integration.py`` straight from disk, prints the
observed TEMP/TMP, tmp_path, Source Graph root and private LSP cwd string and
realpath lengths, then drives one real ``sg.build_index`` full build against
the scripted fake LSP server and prints the first actual exception
class/errno/winerror raised at ``build_bounded_workspace``, ``_write_new_file``
or ``subprocess.Popen``, or an explicit no-exception status.  Observations are
printed, never judged: no root cause is inferred from path length alone and a
cause is claimed only if an actual exception is captured.  The diagnostic
targets the win32 long-temp-path failure; on other platforms it still runs and
prints the control observation so both declared validation commands always
produce output past pytest startup.
"""

import importlib.util
import os
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

import aiworkhub.source_graph as sg
import aiworkhub.source_graph_lsp as sg_lsp

REFERENCE_PATH = Path(__file__).with_name("test_source_graph_lsp_integration.py")
PREFIX = "[wincap]"


def _report_path(label: str, value) -> None:
    text = str(value)
    if text.startswith("<"):
        print(f"{PREFIX} {label}: sentinel={text}")
        return
    real = os.path.realpath(text)
    print(f"{PREFIX} {label}: len={len(text)} realpath_len={len(real)} path={text}")


def _describe(exc: BaseException) -> str:
    return (
        f"class={type(exc).__name__} errno={getattr(exc, 'errno', None)} "
        f"winerror={getattr(exc, 'winerror', None)} repr={exc!r}"
    )


def _classify(exc: BaseException) -> str:
    frames = {frame.name for frame in traceback.extract_tb(exc.__traceback__)}
    for site in ("build_bounded_workspace", "_write_new_file"):
        if site in frames:
            return site
    if "Popen" in frames or any("spawn" in name for name in frames):
        return "subprocess.Popen"
    return "other:" + ",".join(sorted(frames)[-2:])


@pytest.fixture
def reference():
    assert REFERENCE_PATH.is_file(), "reference fixture module missing"
    spec = importlib.util.spec_from_file_location(
        "test_source_graph_lsp_integration_reference", REFERENCE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_lsp_exception_capture(reference, tmp_path, monkeypatch):
    fixture = reference._Lsp(tmp_path, monkeypatch, "wincap")
    reference._python_pair(fixture)

    observations: dict = {}

    def note(key: str, text: str) -> None:
        observations.setdefault(key, text)
        print(f"{PREFIX} {key}: {text}")

    print(f"{PREFIX} platform={sys.platform} win32={sys.platform == 'win32'}")
    for name in ("TEMP", "TMP"):
        _report_path(name, os.environ.get(name, "<unset>"))
    _report_path("tmp_path", tmp_path)
    _report_path("source_graph_root", fixture.root)
    _report_path("worktree_root", REFERENCE_PATH.parent.parent.resolve())

    real_popen = subprocess.Popen

    def spying_popen(*args, **kwargs):
        cwd = kwargs.get("cwd")
        if cwd is not None:
            note("private_lsp_cwd", f"len={len(str(cwd))} path={cwd}")
        try:
            return real_popen(*args, **kwargs)
        except BaseException as exc:
            note("first_exception", f"{_describe(exc)} site=subprocess.Popen")
            raise

    monkeypatch.setattr(subprocess, "Popen", spying_popen)

    for attr in ("build_bounded_workspace", "_write_new_file"):
        original = getattr(sg_lsp, attr, None)
        if original is None:
            print(f"{PREFIX} {attr}: absent from aiworkhub.source_graph_lsp")
            continue

        def make_spy(bound_attr, bound_original):
            def spy(*args, **kwargs):
                try:
                    return bound_original(*args, **kwargs)
                except BaseException as exc:
                    note("first_exception", f"{_describe(exc)} site={bound_attr}")
                    raise

            return spy

        monkeypatch.setattr(sg_lsp, attr, make_spy(attr, original))

    try:
        report = sg.build_index(fixture.root)
        print(
            f"{PREFIX} build_index: returned "
            f"db_path={getattr(report, 'db_path', None)}"
        )
    except BaseException as exc:
        note("first_exception", f"{_describe(exc)} site={_classify(exc)}")

    if "first_exception" not in observations:
        print(
            f"{PREFIX} status: explicit no-exception at "
            "build_bounded_workspace/_write_new_file/subprocess.Popen"
        )
    if "private_lsp_cwd" not in observations:
        print(f"{PREFIX} private_lsp_cwd: not observed (no Popen reached)")
    print(
        f"{PREFIX} differential: rerun with --basetemp=.pt and compare printed "
        "lengths; path length alone is not a cause claim"
    )
    assert fixture.root.is_dir()
