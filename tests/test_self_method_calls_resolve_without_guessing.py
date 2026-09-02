"""A self-call binds to the caller's own class, and to nothing else.

``self.method()`` names no module and has no import to agree with, so the
import resolver could never see one: every self-call stayed unresolved.

The tempting fix is uniqueness -- bind any unresolved call whose name is
defined exactly once in the repository. Measured on AIWorkHub that looked like
10,436 free edges, and the most frequent names in that set were ``exists``
(1132), ``join`` (928), ``sha256`` (836), ``stat`` (565), ``open`` (524, a
builtin), ``time``, ``sleep``, ``search``. Those are stdlib calls that happen to
collide with one repository definition. Binding on uniqueness alone would have
manufactured thousands of edges pointing at the wrong code, and a fabricated
edge is worse than an absent one: an absent edge is visibly absent, while a
fabricated one is confidently wrong.

So uniqueness is a precondition here, never the evidence. The evidence is the
receiver -- the line must literally read ``self.<name>(`` -- together with the
one definition being a method of the SAME class. 622 edges in this repository
satisfy both, and all 622 verify independently.

Run: python3 -m pytest -q tests/test_self_method_calls_resolve_without_guessing.py
"""

from __future__ import annotations

from pathlib import Path

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="repo")
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    sg.build_index(root, incremental=False)
    return root


def _bindings(repo: Path, file_path: str, dst_name: str) -> set[str]:
    """Every non-null dst_qualname recorded for this call.

    The extractor emits one call edge per enclosing scope -- module, class and
    method all record ``self._step()`` on the same line -- and only the
    innermost one can name a receiver. So the question is never "what did the
    first edge bind to" but "what, if anything, was bound at all".
    """
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        rows = conn.execute(
            "SELECT dst_qualname FROM edges WHERE file_path=? AND kind='calls' "
            "AND dst_name=?",
            (file_path, dst_name),
        ).fetchall()
        if not rows:
            return {"NO_EDGE"}
        return {r["dst_qualname"] for r in rows if r["dst_qualname"] is not None}
    finally:
        conn.close()


def test_a_self_call_binds_to_the_method_of_its_own_class(tmp_path):
    repo = _repo(tmp_path, {"a.py": (
        "class Worker:\n"
        "    def run(self):\n"
        "        return self._step()\n"
        "\n"
        "    def _step(self):\n"
        "        return 1\n"
    )})
    assert _bindings(repo, "a.py", "_step") == {"a.py.Worker._step"}


def test_a_name_that_collides_with_the_stdlib_is_not_bound(tmp_path):
    """The exact shape that made uniqueness the wrong evidence.

    ``exists`` is defined once here, and ``path.exists()`` is a pathlib call.
    Binding on uniqueness would point this at unrelated repository code.
    """
    repo = _repo(tmp_path, {"a.py": (
        "from pathlib import Path\n"
        "\n"
        "\n"
        "class Store:\n"
        "    def check(self, path: Path):\n"
        "        return path.exists()\n"
        "\n"
        "    def exists(self):\n"
        "        return True\n"
    )})
    assert _bindings(repo, "a.py", "exists") == set()


def test_a_receiver_that_is_not_self_is_not_bound(tmp_path):
    repo = _repo(tmp_path, {"a.py": (
        "class Worker:\n"
        "    def run(self, other):\n"
        "        return other._step()\n"
        "\n"
        "    def _step(self):\n"
        "        return 1\n"
    )})
    assert _bindings(repo, "a.py", "_step") == set()


def test_a_method_of_a_different_class_is_not_bound(tmp_path):
    """Unique in the repo, reached through self, still a different class."""
    repo = _repo(tmp_path, {"a.py": (
        "class Other:\n"
        "    def _step(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "class Worker:\n"
        "    def run(self):\n"
        "        return self._step()\n"
    )})
    assert _bindings(repo, "a.py", "_step") == set()


def test_a_module_level_function_is_not_bound_through_self(tmp_path):
    """``self.x()`` cannot reach a module-level function; the pass agrees."""
    repo = _repo(tmp_path, {"a.py": (
        "def _step():\n"
        "    return 1\n"
        "\n"
        "\n"
        "class Worker:\n"
        "    def run(self):\n"
        "        return self._step()\n"
    )})
    assert _bindings(repo, "a.py", "_step") == set()


def test_an_ambiguous_name_is_not_bound(tmp_path):
    """Two definitions means no unique target, whatever the receiver says."""
    repo = _repo(tmp_path, {
        "a.py": (
            "class Worker:\n"
            "    def run(self):\n"
            "        return self._step()\n"
            "\n"
            "    def _step(self):\n"
            "        return 1\n"
        ),
        "b.py": (
            "class Elsewhere:\n"
            "    def _step(self):\n"
            "        return 2\n"
        ),
    })
    assert _bindings(repo, "a.py", "_step") == set()


def test_a_binding_is_cleared_when_its_target_disappears(tmp_path):
    """A rename must not leave an edge pointing at an entity that is gone."""
    repo = _repo(tmp_path, {"a.py": (
        "class Worker:\n"
        "    def run(self):\n"
        "        return self._step()\n"
        "\n"
        "    def _step(self):\n"
        "        return 1\n"
    )})
    assert _bindings(repo, "a.py", "_step") == {"a.py.Worker._step"}

    (repo / "a.py").write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        return self._renamed()\n"
        "\n"
        "    def _renamed(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    sg.build_index(repo, incremental=True)
    assert _bindings(repo, "a.py", "_step") == {"NO_EDGE"}
    assert _bindings(repo, "a.py", "_renamed") == {"a.py.Worker._renamed"}


def test_the_owner_class_helper_refuses_a_module_level_qualname():
    assert sg._python_owner_class("a.py.Worker.run", "a.py") == "a.py.Worker"
    assert sg._python_owner_class("a.py.run", "a.py") is None
    assert sg._python_owner_class("b.py.Worker.run", "a.py") is None
