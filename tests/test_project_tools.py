"""Project tool library: scan, library paths, and the four shipped tools."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import project_tools
from aiworkhub.validation_inputs import repo_validation_input_paths

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
SHIPPED = ("pyrun", "json_get", "sqlite_peek", "script_census")


def _header(tool_id: str, *extra: str, prefix: str = "#") -> str:
    lines = [
        f"{prefix} aiworkhub-tool id: {tool_id}",
        f"{prefix} aiworkhub-tool purpose: Purpose of {tool_id}.",
        f"{prefix} aiworkhub-tool usage: python tools/{tool_id}.py",
        *(f"{prefix} aiworkhub-tool {line}" for line in extra),
    ]
    return "\n".join(lines) + "\n"


def _write(repo: Path, name: str, content: str | bytes) -> Path:
    path = repo / "tools" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    return path


def _token(token) -> str:
    return token.text if hasattr(token, "text") else f"${token.param}"


def _mixed_repo(tmp_path: Path) -> tuple[Path, bool]:
    repo = tmp_path / "repo"
    _write(repo, "alpha.py", _header("alpha", "param: target:path", "param: names:path_list"))
    _write(repo, "beta.ps1", _header("beta", "param: count:int"))
    _write(repo, "plain.py", "print('no marker')\n")
    _write(repo, "broken.py", "# aiworkhub-tool id: Not A Slug\n")
    _write(repo, "dupe.py", _header("alpha"))
    _write(repo, "latin.py", _header("latin").encode("utf-8") + b"# caf\xe9\n")
    head = _header("big").encode("utf-8")
    padding = b"#" + b"x" * (project_tools.MAX_TOOL_BYTES - len(head) - 2)
    _write(repo, "big.py", head + padding + "é".encode("utf-8") + b"\n")
    _write(repo, "unknown.py", _header("unknown", "param: x:float"))
    _write(repo, "notes.txt", _header("notes"))
    _write(repo, "sub/inner.py", _header("inner"))
    linked = False
    try:
        os.symlink(repo / "tools" / "alpha.py", repo / "tools" / "link.py")
        linked = True
    except (OSError, NotImplementedError):
        pass
    return repo, linked


def test_scan_accepts_valid_tools_and_names_each_refusal(tmp_path):
    repo, linked = _mixed_repo(tmp_path)
    big = (repo / "tools" / "big.py").read_bytes()
    assert len(big) > project_tools.MAX_TOOL_BYTES
    assert big[project_tools.MAX_TOOL_BYTES - 1 : project_tools.MAX_TOOL_BYTES] == b"\xc3"

    recipes, refused = project_tools.scan(repo)

    assert [recipe.id for recipe in recipes] == ["project.alpha", "project.beta"]
    alpha, beta = recipes
    assert [_token(t) for t in alpha.argv] == ["python", "tools/alpha.py", "$target", "$names"]
    assert [_token(t) for t in beta.argv] == [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "tools/beta.ps1",
        "$count",
    ]
    assert beta.risk_class.value == "medium"
    expected = {
        "tools/broken.py": "invalid_header",
        "tools/dupe.py": "duplicate_id",
        "tools/latin.py": "unreadable",
        "tools/big.py": "too_large",
        "tools/unknown.py": "unknown_type",
        "tools/notes.txt": "unsupported_extension",
    }
    if linked:
        expected["tools/link.py"] = "symlink"
    assert refused == expected
    assert "tools/plain.py" not in refused
    assert not any("inner" in key for key in refused)


def test_scan_never_raises_on_missing_or_empty_tools(tmp_path):
    assert project_tools.scan(tmp_path) == ([], {})
    (tmp_path / "tools").mkdir()
    assert project_tools.scan(tmp_path) == ([], {})
    assert project_tools.library_paths(tmp_path / "absent") == []
    assert project_tools.prompt_listing(tmp_path, 5) == []


def test_changing_one_byte_changes_the_recipe_version(tmp_path):
    path = _write(tmp_path, "a.py", _header("a") + "print(1)\n")
    first = project_tools.scan(tmp_path)[0][0]
    path.write_text(_header("a") + "print(2)\n", encoding="utf-8")
    second = project_tools.scan(tmp_path)[0][0]
    again = project_tools.scan(tmp_path)[0][0]

    assert first.version != second.version
    assert second.version == again.version
    assert second.version.startswith("1.0.0+")


def test_batch_files_are_refused_whatever_their_prefix(tmp_path):
    _write(tmp_path, "run.cmd", _header("run", prefix="::"))
    _write(tmp_path, "run2.bat", _header("run2", prefix="REM"))
    _write(tmp_path, "wrong.cmd", _header("wrong", prefix="#"))

    recipes, refused = project_tools.scan(tmp_path)

    assert recipes == []
    assert refused == {
        "tools/run.cmd": "batch_file_unsupported",
        "tools/run2.bat": "batch_file_unsupported",
        "tools/wrong.cmd": "batch_file_unsupported",
    }


def test_prompt_listing_is_bounded(tmp_path):
    for index in range(5):
        _write(tmp_path, f"t{index}.py", _header(f"t{index}"))
    listing = project_tools.prompt_listing(tmp_path, 3)

    assert [entry["id"] for entry in listing] == ["project.t0", "project.t1", "project.t2"]
    assert set(listing[0]) == {"id", "purpose", "usage"}
    assert project_tools.prompt_listing(tmp_path, 0) == []


def test_real_repository_tools_are_all_accepted():
    recipes, refused = project_tools.scan(REPO)

    assert {f"project.{name}" for name in SHIPPED} <= {recipe.id for recipe in recipes}
    assert not [path for path in refused if path.startswith("tools")]
    paths = project_tools.library_paths(REPO)
    assert all(f"tools/{name}.py" in paths for name in SHIPPED)


def test_validation_inputs_include_the_library_without_a_config(tmp_path):
    _write(tmp_path, "b.py", _header("b"))
    _write(tmp_path, "a.py", _header("a"))
    assert repo_validation_input_paths(tmp_path) == ["tools/a.py", "tools/b.py"]

    _write(tmp_path, "broken.py", "# aiworkhub-tool id: BAD\n")
    assert repo_validation_input_paths(tmp_path) == ["tools/a.py", "tools/b.py"]


def test_validation_inputs_survive_a_library_failure(tmp_path, monkeypatch):
    def explode(_root):
        raise RuntimeError("boom")

    monkeypatch.setattr(project_tools, "library_paths", explode)
    assert repo_validation_input_paths(tmp_path) == []


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd or REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def test_pyrun_puts_repo_src_first_and_runs_code_modules_and_scripts(tmp_path):
    code = _run(str(TOOLS / "pyrun.py"), "-c", "import sys; print(sys.path[0]); print(sys.argv[1:])", "x")
    assert code.returncode == 0, code.stderr
    first, second = code.stdout.splitlines()
    assert Path(first) == REPO / "src"
    assert second == "['x']"

    module = _run(str(TOOLS / "pyrun.py"), "-m", "platform")
    assert module.returncode == 0 and module.stdout.strip()

    script = tmp_path / "s.py"
    script.write_text("import sys\nprint('ran', sys.argv[1:])\nsys.exit(3)\n", encoding="utf-8")
    ran = _run(str(TOOLS / "pyrun.py"), str(script), "a", cwd=tmp_path)
    assert ran.stdout.strip() == "ran ['a']"
    assert ran.returncode == 3

    assert _run(str(TOOLS / "pyrun.py")).returncode == 2


def test_json_get_reads_json_and_jsonl(tmp_path):
    doc = tmp_path / "d.json"
    doc.write_text(json.dumps({"items": [{"name": "a"}, {"name": "b"}], "n": 7}), encoding="utf-8")
    lines = tmp_path / "d.jsonl"
    lines.write_text('{"k": 1}\n{"k": {"deep": [10, 20]}}\n', encoding="utf-8")

    assert _run(str(TOOLS / "json_get.py"), str(doc), "items.1.name").stdout.strip() == "b"
    assert _run(str(TOOLS / "json_get.py"), str(doc), "n").stdout.strip() == "7"
    assert _run(str(TOOLS / "json_get.py"), str(lines), "1.k.deep.0").stdout.strip() == "10"
    missing = _run(str(TOOLS / "json_get.py"), str(doc), "items.9")
    assert missing.returncode == 1 and "path_not_found" in missing.stderr

    big = tmp_path / "big.json"
    big.write_text(json.dumps({"text": "x" * 20000}), encoding="utf-8")
    out = _run(str(TOOLS / "json_get.py"), str(big), "text").stdout
    assert len(out) < 5000 and "truncated" in out


def _database(path: Path, rows: int = 60) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, label TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"r{i}") for i in range(rows)])
    conn.commit()
    conn.close()
    return path


def test_sqlite_peek_lists_selects_and_cannot_write(tmp_path):
    db = _database(tmp_path / "x.db")
    peek = str(TOOLS / "sqlite_peek.py")

    tables = json.loads(_run(peek, str(db)).stdout)
    assert tables == {"tables": {"t": 60}}

    selected = json.loads(_run(peek, str(db), "SELECT id FROM t ORDER BY id").stdout)
    assert len(selected["rows"]) == 50 and selected["truncated"] is True
    assert selected["columns"] == ["id"]

    for statement in (
        "DELETE FROM t",
        "DROP TABLE t",
        "WITH x AS (SELECT 1) INSERT INTO t VALUES (99, 'z')",
        "SELECT 1; DELETE FROM t",
        "PRAGMA user_version = 5",
    ):
        result = _run(peek, str(db), statement)
        assert result.returncode != 0, statement
    check = sqlite3.connect(db)
    assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 60
    check.close()


def _claude(command: str) -> str:
    block = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    return json.dumps({"type": "assistant", "message": {"content": [block]}})


def _codex(command: str) -> str:
    item = {"type": "command_execution", "command": command}
    return json.dumps({"type": "item.completed", "item": item})


def test_script_census_counts_adhoc_scripts_and_project_tool_runs(tmp_path):
    adhoc = [
        "python -c \"print('x')\"",
        "cat <<'EOF' > a.py\nprint(1)\nEOF",
        "echo a\necho b\necho c",
    ]
    claude = [
        _claude("git status"),
        _claude(adhoc[0]),
        _claude(adhoc[1]),
        _claude("python tools/pyrun.py -c 1"),
        _claude("python src/tools/x.py"),
    ]
    codex = [
        _codex(adhoc[2]),
        _codex("python .\\tools\\pyrun.py -c 1"),
        _codex("./tools/x.sh"),
        "not json",
    ]
    (tmp_path / "a.stdout.log").write_text("\n".join(claude) + "\n", encoding="utf-8")
    (tmp_path / "b.stdout.log").write_text("\n".join(codex) + "\n", encoding="utf-8")
    (tmp_path / "empty.stdout.log").write_text("", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text(_claude("python -c 1"), encoding="utf-8")

    result = _run(str(TOOLS / "script_census.py"), str(tmp_path))

    assert result.returncode == 0, result.stderr
    census = json.loads(result.stdout)
    assert census["logs"] == 3
    assert census["logs_with_commands"] == 2
    assert census["commands"] == 8
    assert census["adhoc_scripts"] == 3
    assert census["adhoc_chars"] == sum(len(command) for command in adhoc)
    assert census["project_tool_invocations"] == 3
    assert _run(str(TOOLS / "script_census.py"), str(tmp_path / "missing")).returncode == 2


@pytest.mark.parametrize("name", SHIPPED)
def test_shipped_tools_carry_a_valid_header(name):
    assert f"tools/{name}.py" in project_tools.library_paths(REPO)


def test_cmd_tool_is_refused_not_a_recipe(tmp_path):
    _write(tmp_path, "hi.cmd", _header("hi", prefix="::") + "@echo off\necho tokenhi\n")

    recipes, refused = project_tools.scan(tmp_path)

    assert recipes == []
    assert refused == {"tools/hi.cmd": "batch_file_unsupported"}


def _run_cp1252(*args: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    return subprocess.run(
        [sys.executable, *args], cwd=REPO, capture_output=True, env=env, timeout=60
    )


def test_json_get_and_sqlite_peek_emit_utf8_whatever_the_pipe_encoding(tmp_path):
    value = "ქართული"
    doc = tmp_path / "d.jsonl"
    doc.write_text(json.dumps({"v": value}, ensure_ascii=False) + "\n", encoding="utf-8")
    got = _run_cp1252(str(TOOLS / "json_get.py"), str(doc), "0.v")
    assert got.returncode == 0, got.stderr
    assert value in got.stdout.decode("utf-8")

    db = tmp_path / "u.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (label TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.commit()
    conn.close()
    peek = _run_cp1252(str(TOOLS / "sqlite_peek.py"), str(db), "SELECT label FROM t")
    assert peek.returncode == 0, peek.stderr
    assert value in peek.stdout.decode("utf-8")


def test_one_accepted_set_for_scan_library_paths_and_prompt_listing(tmp_path, monkeypatch):
    _write(tmp_path, "good.py", _header("good"))
    _write(tmp_path, "bad.py", _header("bad"))
    real = project_tools._recipe

    def flaky(tool):
        if tool.id == "bad":
            raise RuntimeError("no recipe")
        return real(tool)

    monkeypatch.setattr(project_tools, "_recipe", flaky)
    recipes, refused = project_tools.scan(tmp_path)

    assert [recipe.id for recipe in recipes] == ["project.good"]
    assert refused == {"tools/bad.py": "invalid_header"}
    assert project_tools.library_paths(tmp_path) == ["tools/good.py"]
    assert [entry["id"] for entry in project_tools.prompt_listing(tmp_path, 10)] == ["project.good"]


def test_a_risk_header_is_refused_and_every_recipe_is_medium_with_write(tmp_path):
    _write(tmp_path, "low.py", _header("low", "risk: low"))
    _write(tmp_path, "ok.py", _header("ok"))
    recipes, refused = project_tools.scan(tmp_path)

    assert refused == {"tools/low.py": "invalid_header"}
    assert recipes[0].risk_class.value == "medium"
    assert "write" in recipes[0].capabilities


def test_run_refuses_a_tool_whose_bytes_changed_since_the_scan(tmp_path):
    from aiworkhub import recipe_runner

    body = _header("marker") + "open('ran.txt', 'w').write('x')\n"
    path = _write(tmp_path, "marker.py", body)
    recipe = project_tools.scan(tmp_path)[0][0]

    first = recipe_runner.run_recipe(
        tmp_path, recipe, {}, grant_capabilities=("write",), persist_receipt=False
    )
    assert first["returncode"] == 0
    assert (tmp_path / "ran.txt").is_file()
    (tmp_path / "ran.txt").unlink()

    path.write_bytes(body.replace("'x'", "'y'").encode("utf-8"))
    with pytest.raises(recipe_runner.RecipeRunError) as caught:
        recipe_runner.run_recipe(
            tmp_path, recipe, {}, grant_capabilities=("write",), persist_receipt=False
        )
    assert caught.value.reason == recipe_runner.REASON_PROJECT_TOOL_CHANGED
    assert not (tmp_path / "ran.txt").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are a Windows feature")
def test_a_junctioned_tools_directory_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.py").write_text(_header("evil"), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(repo / "tools"), str(outside)],
        check=True,
        capture_output=True,
    )

    recipes, refused = project_tools.scan(repo)

    assert recipes == [] and refused == {"tools": "symlink"}
    assert project_tools.library_paths(repo) == []
